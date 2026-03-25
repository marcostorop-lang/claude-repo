"""Math helpers used by strategies and risk management."""

from __future__ import annotations

import math
from typing import Sequence


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean. Returns 0.0 for empty sequences."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def stdev(values: Sequence[float]) -> float:
    """Population standard deviation. Returns 0.0 for fewer than 2 values."""
    if len(values) < 2:
        return 0.0
    m = mean(values)
    variance = sum((v - m) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def z_score(value: float, values: Sequence[float]) -> float:
    """Z-score of *value* relative to *values*. Returns 0.0 if stdev is zero."""
    s = stdev(values)
    if s == 0.0:
        return 0.0
    return (value - mean(values)) / s


def pct_change(old: float, new: float) -> float:
    """Percentage change from *old* to *new*. Returns 0.0 if old is zero."""
    if old == 0.0:
        return 0.0
    return (new - old) / old
