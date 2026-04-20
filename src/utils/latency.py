"""
Latency telemetry.

Every live trade has three timestamps the operator cares about:

* ``t_signal``  — the moment the strategy produced a BUY/SELL signal,
* ``t_submit``  — the moment the executor posted the order,
* ``t_fill``    — the moment the fill was confirmed.

The gap between them is the bot's true time-to-market, and it's the
single hidden variable that causes paper → live divergence.  A bot
that fills 500 ms after signal in backtest but 5 s after signal in
live is not the same bot, and a strategy whose edge decays over
seconds will silently die in production.

This module is pure-data: it records each observation, computes the
rolling percentile stats, and tells the caller when latency crossed
an alert threshold.  The bot wiring calls ``observe_*`` at the three
points above; everything else (alerting, dashboard, metrics) reads
the rolled-up stats.

Kept dependency-free (no numpy) because the history is bounded to a
few hundred points per session — sorted+interp is plenty fast and
there's no reason to pull in a heavy dep just for p95.
"""

from __future__ import annotations

import bisect
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LatencyStats:
    """Rolling percentile stats across the recorded window."""

    n_samples: int
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


class LatencyTracker:
    """Rolling-window recorder for the three tick-to-fill intervals.

    ``window`` caps memory; typical tick loops running at 60 s
    intervals stay under a few hundred points per day.
    """

    def __init__(self, window: int = 500) -> None:
        self._window = max(10, int(window))
        self._signal_to_submit: Deque[float] = deque(maxlen=self._window)
        self._submit_to_fill: Deque[float] = deque(maxlen=self._window)
        self._signal_to_fill: Deque[float] = deque(maxlen=self._window)

    # -- recording ---------------------------------------------------------

    def observe(
        self,
        *,
        t_signal: float,
        t_submit: float,
        t_fill: float,
    ) -> None:
        """Record one complete tick-to-fill trajectory, in seconds."""
        if t_signal > 0 and t_submit >= t_signal:
            self._signal_to_submit.append((t_submit - t_signal) * 1000.0)
        if t_submit > 0 and t_fill >= t_submit:
            self._submit_to_fill.append((t_fill - t_submit) * 1000.0)
        if t_signal > 0 and t_fill >= t_signal:
            self._signal_to_fill.append((t_fill - t_signal) * 1000.0)

    # -- stats -------------------------------------------------------------

    def stats(self) -> dict[str, LatencyStats]:
        return {
            "signal_to_submit": _summarise(list(self._signal_to_submit)),
            "submit_to_fill": _summarise(list(self._submit_to_fill)),
            "signal_to_fill": _summarise(list(self._signal_to_fill)),
        }

    def latest_signal_to_fill_ms(self) -> float | None:
        """Most recent end-to-end latency (for quick alerting)."""
        return self._signal_to_fill[-1] if self._signal_to_fill else None


# -- helpers ---------------------------------------------------------------


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _summarise(values: list[float]) -> LatencyStats:
    if not values:
        return LatencyStats(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    xs = sorted(values)
    return LatencyStats(
        n_samples=len(xs),
        p50_ms=_percentile(xs, 50.0),
        p90_ms=_percentile(xs, 90.0),
        p95_ms=_percentile(xs, 95.0),
        p99_ms=_percentile(xs, 99.0),
        max_ms=xs[-1],
    )


# -- alert helper ---------------------------------------------------------


def should_alert_latency(
    latest_ms: float | None,
    *,
    threshold_ms: float,
) -> bool:
    """Return True when ``latest_ms`` exceeds the configured threshold.

    Trivial wrapper — kept separate so the caller can test alert
    policy without instantiating a tracker.
    """
    if threshold_ms <= 0 or latest_ms is None:
        return False
    return latest_ms >= threshold_ms
