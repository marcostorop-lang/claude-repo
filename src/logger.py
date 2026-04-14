"""
Logging configuration.

Call ``setup_logging()`` once at startup. All modules should use:

    import logging
    logger = logging.getLogger(__name__)

The file handler rotates by size so a long-running bot never fills the
disk silently — the runbook lists "rotate bot.log" as a known gap, and
unbounded log growth has masked real crashes in the past.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure root logger with console and optional *rotating* file handler.

    Parameters
    ----------
    level
        Log level name (``"INFO"``, ``"DEBUG"``, ...).
    log_file
        Path to the log file.  ``None`` or empty disables the file handler.
    max_bytes
        Rotate once the active file grows past this.  Default 10 MB.
        Set to 0 to disable rotation (old behaviour — not recommended).
    backup_count
        How many rotated files to keep alongside the active one.
        Default 5 → at most ``(backup_count + 1) * max_bytes`` of disk
        used by logs.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Idempotency: repeated calls (e.g. in tests) must not stack handlers.
    # We replace only the handlers this function owns — leave others alone.
    for h in list(root.handlers):
        if getattr(h, "_claude_managed", False):
            root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console._claude_managed = True  # type: ignore[attr-defined]
    root.addHandler(console)

    # File handler — rotating unless ``max_bytes == 0`` (legacy mode).
    if log_file:
        if max_bytes > 0:
            fh: logging.Handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=int(max_bytes),
                backupCount=int(backup_count),
                encoding="utf-8",
            )
        else:
            fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh._claude_managed = True  # type: ignore[attr-defined]
        root.addHandler(fh)

