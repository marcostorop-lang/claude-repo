"""Time-related helpers."""

from __future__ import annotations

import time
from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def utc_timestamp() -> float:
    """Return current UNIX timestamp in seconds."""
    return time.time()


def iso_now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return utc_now().isoformat()
