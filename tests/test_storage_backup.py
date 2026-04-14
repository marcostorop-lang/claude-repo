"""Tests for the SQLite online-backup helper."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src.storage.backup import _prune, should_run, snapshot


def _seed_db(path: Path, rows: int = 10) -> None:
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    c.executemany("INSERT INTO t(v) VALUES (?)", [(f"v{i}",) for i in range(rows)])
    c.commit()
    c.close()


def _row_count(path: Path) -> int:
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        c.close()


class TestSnapshotHappyPath:
    def test_writes_consistent_copy(self, tmp_path):
        src = tmp_path / "bot.db"
        _seed_db(src, rows=42)
        dst_dir = tmp_path / "backups"

        out = snapshot(src, dst_dir, now_ts=1700000000.0)
        assert out is not None
        assert out.exists()
        assert out.parent == dst_dir
        assert _row_count(out) == 42

    def test_filename_contains_timestamp(self, tmp_path):
        src = tmp_path / "bot.db"
        _seed_db(src)
        out = snapshot(src, tmp_path / "bk",
                       filename_prefix="mybot", now_ts=0.0)
        assert out is not None
        # Epoch 0 UTC → 19700101-000000
        assert out.name == "mybot-19700101-000000.db"

    def test_creates_destination_dir(self, tmp_path):
        src = tmp_path / "bot.db"
        _seed_db(src)
        dst = tmp_path / "nested" / "bk"
        assert not dst.exists()
        out = snapshot(src, dst)
        assert out is not None and dst.is_dir()


class TestSnapshotFailureModes:
    def test_missing_source_returns_none(self, tmp_path):
        out = snapshot(tmp_path / "nope.db", tmp_path / "bk")
        assert out is None

    def test_unreachable_dst_returns_none(self, tmp_path):
        src = tmp_path / "bot.db"
        _seed_db(src)
        # Use a path where the parent is a file, so mkdir will fail.
        blocker = tmp_path / "blocker"
        blocker.write_text("I'm a file, not a directory")
        out = snapshot(src, blocker / "bk")
        assert out is None


class TestPruning:
    def test_keeps_only_n_most_recent(self, tmp_path):
        src = tmp_path / "bot.db"
        _seed_db(src)
        dst = tmp_path / "bk"

        # Create 5 backups with increasing timestamps.
        paths = []
        for i in range(5):
            p = snapshot(src, dst, keep=0, now_ts=1_700_000_000 + i * 60)
            assert p is not None
            # Tweak mtime to force ordering (stat.mtime is what _prune
            # compares, not filename).
            import os
            os.utime(p, (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))
            paths.append(p)

        # Now force a prune down to 2.
        _prune(dst, "polymarket_bot", keep=2)

        survivors = sorted(dst.glob("polymarket_bot-*.db"))
        assert len(survivors) == 2
        # The two newest (last two in paths) must still be present.
        assert paths[-1] in survivors
        assert paths[-2] in survivors
        # The three oldest should be gone.
        for p in paths[:3]:
            assert not p.exists()

    def test_snapshot_triggers_prune(self, tmp_path):
        src = tmp_path / "bot.db"
        _seed_db(src)
        dst = tmp_path / "bk"

        import os
        # Create 4 older backups first (keep=0 disables pruning per-call).
        for i in range(4):
            p = snapshot(src, dst, keep=0, now_ts=1_700_000_000 + i * 60)
            assert p is not None
            os.utime(p, (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))

        # A new snapshot with keep=2 should write the 5th and then prune
        # the three oldest in one shot.
        latest = snapshot(src, dst, keep=2, now_ts=1_700_000_400)
        assert latest is not None
        survivors = sorted(dst.glob("polymarket_bot-*.db"))
        assert len(survivors) == 2
        assert latest in survivors


class TestShouldRun:
    def test_zero_interval_never_runs(self):
        assert should_run(0.0, 0.0, now=1_000.0) is False
        assert should_run(0.0, -5.0, now=1_000.0) is False

    def test_first_ever_run_goes_immediately(self):
        # last=0.0 + interval=24h → any real "now" triggers.
        assert should_run(0.0, 24.0, now=1_700_000_000.0) is True

    def test_waits_for_interval(self):
        last = 1_700_000_000.0
        # 23h later → not yet.
        assert should_run(last, 24.0, now=last + 23 * 3600) is False
        # Exactly 24h → yes.
        assert should_run(last, 24.0, now=last + 24 * 3600) is True
