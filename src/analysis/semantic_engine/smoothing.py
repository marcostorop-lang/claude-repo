"""Cross-tick EMA smoothing for synthetic fair prices.

A single tick's synthetic price can be contaminated by a stale sibling:
if one leg of a neg-risk hasn't updated in 30 seconds, ``1 - Σ siblings``
drifts with it, producing a phantom edge.  The fix is an exponential
moving average across ticks, which:

* lets a genuinely mispriced market still get picked up (edge persists),
* dampens single-tick spikes that revert on the next read,
* degrades gracefully when the engine is re-enabled (state is empty at
  boot — the first tick passes through unchanged until enough samples
  accumulate).

The smoother is **per-token** and bounded (drops stale entries) so a
long-running bot doesn't balloon memory as markets resolve and retire.

Kept deliberately framework-free — no dependency on pandas, numpy, or
any datetime math that requires timezones.  The caller supplies timestamps
as ISO strings for freshness decisions only.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterator


@dataclass
class SmoothedPoint:
    """A single EMA reading."""

    value: float
    n: int          # Number of raw samples ingested so far
    last_ts: str    # ISO timestamp of the most recent update


class EMASmoother:
    """Bounded per-key EMA with TTL-based eviction.

    Parameters
    ----------
    alpha
        Smoothing factor in ``(0, 1]``.  ``alpha=1`` degenerates to the
        raw signal (no smoothing); ``alpha=0.3`` gives a moderate trailing
        average ~3 ticks deep.  Defaults to ``0.3``.
    max_keys
        Hard cap on tracked keys — after this, the oldest entry is evicted
        when a new key is added.  Prevents unbounded growth as markets
        come and go.

    The smoother is *not* thread-safe; the tick loop is single-threaded,
    so this is fine.
    """

    def __init__(self, alpha: float = 0.3, max_keys: int = 2048) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self.alpha = alpha
        self.max_keys = max_keys
        # OrderedDict preserves insertion order so we can pop the oldest.
        self._state: OrderedDict[str, SmoothedPoint] = OrderedDict()

    # ------------------------------------------------------------------
    # Core update
    # ------------------------------------------------------------------

    def update(self, key: str, value: float, timestamp: str = "") -> float:
        """Feed a raw value and return the smoothed one.

        Idempotent: calling with the same value twice yields the same
        EMA (no timestamp-dependence on the value — timestamps are
        metadata for eviction only).
        """
        cur = self._state.get(key)
        if cur is None:
            new = SmoothedPoint(value=float(value), n=1, last_ts=timestamp)
            self._state[key] = new
            self._evict_if_needed()
            return new.value

        # EMA: y_t = α·x + (1-α)·y_{t-1}
        smoothed = self.alpha * float(value) + (1.0 - self.alpha) * cur.value
        cur.value = smoothed
        cur.n += 1
        cur.last_ts = timestamp or cur.last_ts
        # Touch recency so eviction picks stale keys first.
        self._state.move_to_end(key)
        return cur.value

    def get(self, key: str) -> float | None:
        pt = self._state.get(key)
        return pt.value if pt is not None else None

    def samples(self, key: str) -> int:
        """How many raw values have been ingested for ``key``."""
        pt = self._state.get(key)
        return pt.n if pt is not None else 0

    def clear(self) -> None:
        self._state.clear()

    def forget(self, key: str) -> None:
        self._state.pop(key, None)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> None:
        while len(self._state) > self.max_keys:
            self._state.popitem(last=False)

    def __len__(self) -> int:
        return len(self._state)

    def __iter__(self) -> Iterator[str]:
        return iter(self._state)
