"""Tests for the rotating file log handler."""

from __future__ import annotations

import logging
import logging.handlers

import pytest

from src.logger import setup_logging


@pytest.fixture(autouse=True)
def _reset_root():
    """Make test ordering irrelevant — start with a clean root each time."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    yield
    for h in list(root.handlers):
        root.removeHandler(h)


def _file_handlers():
    return [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.FileHandler)
    ]


def test_rotating_handler_installed_by_default(tmp_path):
    log_path = tmp_path / "bot.log"
    setup_logging(level="INFO", log_file=str(log_path))
    handlers = _file_handlers()
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)
    assert handlers[0].maxBytes == 10 * 1024 * 1024
    assert handlers[0].backupCount == 5


def test_rotation_creates_backup_files(tmp_path):
    log_path = tmp_path / "bot.log"
    setup_logging(level="INFO", log_file=str(log_path),
                  max_bytes=512, backup_count=3)

    logger = logging.getLogger("rot_test")
    # Write enough to exceed 512 bytes several times over.
    for i in range(200):
        logger.info("padding line %d — %s", i, "x" * 40)

    # Active file still exists.
    assert log_path.exists()
    # At least one rotated backup .1 exists; may have .2 / .3 too.
    assert (tmp_path / "bot.log.1").exists()
    # Never exceeds backup_count rotated siblings.
    rotated = sorted(tmp_path.glob("bot.log.*"))
    assert len(rotated) <= 3


def test_max_bytes_zero_falls_back_to_plain(tmp_path):
    log_path = tmp_path / "bot.log"
    setup_logging(level="INFO", log_file=str(log_path), max_bytes=0)
    handlers = _file_handlers()
    assert len(handlers) == 1
    # A plain FileHandler, NOT rotating.
    assert not isinstance(handlers[0], logging.handlers.RotatingFileHandler)


def test_repeat_setup_does_not_stack_handlers(tmp_path):
    log_path = tmp_path / "bot.log"
    setup_logging(level="INFO", log_file=str(log_path))
    setup_logging(level="INFO", log_file=str(log_path))
    setup_logging(level="DEBUG", log_file=str(log_path))
    # Exactly 1 console + 1 file handler after multiple calls.
    root = logging.getLogger()
    managed = [h for h in root.handlers if getattr(h, "_claude_managed", False)]
    assert len(managed) == 2


def test_no_log_file_disables_file_handler(tmp_path):
    setup_logging(level="INFO", log_file=None)
    assert _file_handlers() == []
