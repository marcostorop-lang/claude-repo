"""
Logging configuration.

Call ``setup_logging()`` once at startup. All modules should use:

    import logging
    logger = logging.getLogger(__name__)
"""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Configure root logger with console and optional file handler."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # File handler
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
