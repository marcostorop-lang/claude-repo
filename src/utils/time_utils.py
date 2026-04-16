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


def parse_end_date(end_date: str) -> datetime | None:
    """Parse an end-date string (ISO-8601) into a timezone-aware datetime."""
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def hours_until(end_date: str, now: datetime | None = None) -> float | None:
    """Return hours until *end_date*, or None if unparseable."""
    dt = parse_end_date(end_date)
    if dt is None:
        return None
    now = now or utc_now()
    delta = (dt - now).total_seconds() / 3600.0
    return max(delta, 0.0)


def capital_efficiency_factor(
    end_date: str,
    target_days: float = 14.0,
    min_factor: float = 0.25,
    now: datetime | None = None,
) -> float:
    """Shrinks position sizing for *long-dated* markets.

    The opportunity cost of capital scales with time-to-resolution:
    a 60-day market with the same per-share edge as a 6-day market
    earns the same dollars but ties capital up 10x longer.  This
    factor caps a position at ``target_days / days_to_resolution``
    once the market is longer than ``target_days``, floored at
    ``min_factor`` so we still take a meaningful nibble on
    long-dated bets.

    A short-dated market (≤ target_days) returns 1.0 — this factor
    intentionally does NOT shrink near-resolution markets; that's
    handled by :func:`time_decay_factor`.  The two are complementary
    and can both be active at the same time.

    Empty / unparseable ``end_date`` → 1.0 (no penalty when we don't
    know — fail-safe consistent with the rest of the bot's posture).
    """
    h = hours_until(end_date, now)
    if h is None:
        return 1.0
    days = h / 24.0
    if days <= target_days:
        return 1.0
    factor = target_days / days
    return max(min_factor, min(1.0, factor))


def time_decay_factor(end_date: str, now: datetime | None = None) -> float:
    """Return a [0, 1] multiplier that captures how close a market is to resolution.

    The curve is designed for prediction market trading:

    - > 30 days out:  1.0 (no penalty — long-dated, signals are normal)
    - 7-30 days:      1.0 (sweet spot for trading)
    - 1-7 days:       linear ramp 1.0 → 0.7 (approaching resolution, reduce size)
    - 6-24 hours:     0.5  (high urgency, halve confidence)
    - < 6 hours:      0.2  (very close to resolution, almost no position)

    Returns 1.0 if end_date is empty/unparseable (no penalty when we don't know).
    """
    h = hours_until(end_date, now)
    if h is None:
        return 1.0
    if h > 7 * 24:
        return 1.0
    if h > 24:
        # 7 days → 1 day: linear 1.0 → 0.7
        days_left = h / 24.0
        return 0.7 + 0.3 * (days_left - 1.0) / 6.0
    if h > 6:
        return 0.5
    return 0.2
