"""SQLite online backup + rotation.

The bot accumulates ground truth in ``polymarket_bot.db`` — every trade,
every decision, every tick stat.  A disk failure, accidental ``rm``, or
corrupted shutdown can wipe weeks of learning.  This module takes
periodic safe snapshots.

Why the online backup API and not ``shutil.copy``?

    The DB is open (the bot holds a connection with a write lock
    outstanding for most of the tick).  A plain file copy can capture
    a torn write — the copied file might look fine but be corrupt, and
    we won't know until we try to restore from it.  SQLite's
    ``Connection.backup()`` works at the page level inside a read lock
    and yields a fully consistent copy even under concurrent writes.

Rotation: keep the N most recent snapshots (default 7 → one week of
dailies).  Older files are pruned; if you need longer retention, copy
the backup directory to object storage out-of-band.

Never raises out of :func:`snapshot` — a failed backup is logged and
the tick loop continues.  A bot that stops trading because of a
backup error is worse than a bot that trades without a backup for
one more cycle.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def snapshot(
    src_db: str | Path,
    dst_dir: str | Path,
    *,
    keep: int = 7,
    filename_prefix: str = "polymarket_bot",
    now_ts: float | None = None,
) -> Path | None:
    """Write one consistent online-backup of ``src_db`` into ``dst_dir``.

    Parameters
    ----------
    src_db
        Path to the live SQLite file.  Must exist.
    dst_dir
        Directory to place the backup into.  Created if missing.
    keep
        After writing, prune all but the ``keep`` most recent matching
        snapshots.  0 disables pruning.
    filename_prefix
        File naming scheme is ``{prefix}-{YYYYMMDD-HHMMSS}.db``.
    now_ts
        Clock injection for deterministic tests; falls back to
        ``time.time()``.

    Returns
    -------
    Path of the new backup, or ``None`` if the backup failed.
    """
    src = Path(src_db)
    if not src.exists():
        logger.warning("backup: source DB %s missing — skipping.", src)
        return None
    dst_root = Path(dst_dir)
    try:
        dst_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("backup: cannot create %s — skipping.", dst_root)
        return None

    ts = time.gmtime(now_ts if now_ts is not None else time.time())
    stamp = time.strftime("%Y%m%d-%H%M%S", ts)
    dst_file = dst_root / f"{filename_prefix}-{stamp}.db"

    # Online backup via the sqlite3 module — safe under concurrent writes.
    # We open a *fresh* connection to the source so we never touch the
    # bot's working connection (simpler lifecycle; no lock contention).
    try:
        src_conn = sqlite3.connect(str(src))
        try:
            dst_conn = sqlite3.connect(str(dst_file))
            try:
                # Page-at-a-time backup holds a read lock briefly; the
                # bot's writes serialise through WAL as normal.
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    except sqlite3.Error:
        logger.exception("backup: sqlite backup failed for %s", dst_file)
        # Best-effort cleanup of the partially-written target so future
        # prune logic doesn't keep a corrupt file.
        try:
            if dst_file.exists():
                dst_file.unlink()
        except OSError:
            pass
        return None

    logger.info("backup: wrote %s (%.1f KiB)",
                dst_file, dst_file.stat().st_size / 1024.0)

    if keep > 0:
        _prune(dst_root, filename_prefix, keep)

    return dst_file


def _prune(dst_dir: Path, prefix: str, keep: int) -> None:
    """Keep only the ``keep`` most-recent snapshots matching ``prefix-*.db``."""
    try:
        candidates = sorted(
            dst_dir.glob(f"{prefix}-*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        logger.exception("backup: failed to list %s for pruning.", dst_dir)
        return
    for stale in candidates[keep:]:
        try:
            stale.unlink()
            logger.debug("backup: pruned %s", stale)
        except OSError:
            logger.exception("backup: failed to prune %s", stale)


def should_run(
    last_backup_epoch: float,
    interval_hours: float,
    now: float | None = None,
) -> bool:
    """Return True iff ``interval_hours`` has elapsed since the last backup.

    ``last_backup_epoch == 0`` → run immediately (first tick after start).
    Negative or zero ``interval_hours`` disables scheduling (never runs).
    """
    if interval_hours <= 0:
        return False
    t = now if now is not None else time.time()
    return (t - last_backup_epoch) >= (interval_hours * 3600.0)
