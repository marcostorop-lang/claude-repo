"""
Temporal edge filter — find the hours of the UTC day on which our own
historical closed trades have a win-rate >= a configurable threshold,
and restrict new BUY entries to those hours.

Rationale
---------
Prediction-market activity has strong diurnal structure (US daytime vs.
overnight liquidity, resolution-news cadence, index-close effects).  If
the bot's realised win-rate is structurally better during some hour
windows than others, gating entries to those windows is a cheap edge —
no new strategy, just scheduling.

Data source
-----------
We compute win-rate from the ``calibration`` table: every closed trade
has an ``entry_timestamp`` and a signed ``pnl``.  A "win" is
``pnl > 0``.  This is our ground truth, strictly *our own* trading
history — not an external regime indicator.  The user's instruction
("encuentra ventanas de tiempo de BTC >65% de ventaja") has been
interpreted conservatively: we have no external BTC feed, so the most
defensible "edge by hour" we can measure is the bot's own realised
outcome.  If an external regime feed is added later this module's
``compute_hourly_winrate`` can be swapped for one that reads from it —
the consumer interface (``allowed_hours``) is unchanged.

Fail-safe behaviour
-------------------
When there is insufficient data (cold start, fewer than
``min_samples`` trades in any given hour), we default to **allowing
all 24 hours**.  We'd rather let the bot trade and learn than freeze
it before it has evidence — consistent with the broader
"paper-first, observe before gating" posture from ``CLAUDE.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable


@dataclass(frozen=True)
class HourlyStats:
    """Win-rate stats for a single UTC hour bucket (0..23)."""

    hour: int
    n_trades: int
    n_wins: int

    @property
    def win_rate(self) -> float:
        if self.n_trades <= 0:
            return 0.0
        return self.n_wins / self.n_trades


def _parse_iso(ts: str) -> datetime | None:
    """Lenient ISO-8601 parser → UTC-aware datetime, or None on failure.

    Accepts a trailing ``Z`` (which ``fromisoformat`` didn't support
    before 3.11) and naive timestamps (interpreted as UTC).
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_hourly_winrate(
    calibration_rows: Iterable[dict],
    window_days: int = 30,
    now: datetime | None = None,
) -> dict[int, HourlyStats]:
    """Group closed trades by the UTC hour of their ``entry_timestamp``.

    ``calibration_rows`` is the output of
    :meth:`SQLiteStore.get_calibration_closed` — each row has
    ``entry_timestamp`` and ``pnl`` populated.  Rows without a parseable
    timestamp, or outside the lookback window, are skipped.

    The returned dict maps hour (0..23) → :class:`HourlyStats`.  Hours
    with no samples are simply absent from the dict; callers must treat
    "missing" and "empty" the same way (see :func:`allowed_hours` for
    the fail-safe interpretation).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(window_days, 0)) if window_days > 0 else None

    buckets: dict[int, list[bool]] = {}
    for row in calibration_rows:
        ts = _parse_iso(row.get("entry_timestamp", "") or "")
        if ts is None:
            continue
        if cutoff is not None and ts < cutoff:
            continue
        pnl = row.get("pnl")
        if pnl is None:
            continue
        try:
            won = float(pnl) > 0.0
        except (TypeError, ValueError):
            continue
        buckets.setdefault(ts.hour, []).append(won)

    return {
        hour: HourlyStats(hour=hour, n_trades=len(outcomes), n_wins=sum(outcomes))
        for hour, outcomes in buckets.items()
    }


def allowed_hours(
    stats: dict[int, HourlyStats],
    min_winrate: float,
    min_samples: int,
) -> set[int]:
    """Which UTC hours clear the win-rate threshold with enough samples.

    Fail-safe: if *no* hour has ``>= min_samples`` trades (e.g. cold
    start or a bot that's only been running a few days), we return all
    24 hours — the filter becomes a no-op.  This avoids the trap of a
    silent "allow nothing" state that would freeze the bot before the
    operator noticed.

    Once at least one hour has enough samples the filter becomes
    selective: only hours that *both* have ``>= min_samples`` trades
    *and* clear ``win_rate >= min_winrate`` are allowed.  An
    under-sampled hour is *excluded* from the allow-set — we don't have
    evidence to trade then.
    """
    any_qualified = any(s.n_trades >= min_samples for s in stats.values())
    if not any_qualified:
        return set(range(24))
    return {
        s.hour
        for s in stats.values()
        if s.n_trades >= min_samples and s.win_rate >= min_winrate
    }


class TemporalFilter:
    """Cached hourly allow-set, consumed by the risk manager.

    The filter recomputes ``allowed_hours`` from the calibration table
    every ``ttl_seconds`` (default 1 h) and is safe to call every tick.
    Thread-safe use is not required — the bot's main loop is serial.

    A ``None`` filter on ``RiskManager`` means the feature is off;
    when disabled in config we never construct one at all.
    """

    def __init__(
        self,
        load_calibration: callable,
        min_winrate: float,
        min_samples: int,
        window_days: int,
        ttl_seconds: float = 3600.0,
    ) -> None:
        self._load = load_calibration
        self._min_winrate = float(min_winrate)
        self._min_samples = int(min_samples)
        self._window_days = int(window_days)
        self._ttl = float(ttl_seconds)
        self._cached: set[int] | None = None
        self._cached_at: datetime | None = None

    def _refresh(self, now: datetime) -> None:
        try:
            rows = self._load() or []
        except Exception:  # pragma: no cover - defensive
            rows = []
        stats = compute_hourly_winrate(rows, window_days=self._window_days, now=now)
        self._cached = allowed_hours(stats, self._min_winrate, self._min_samples)
        self._cached_at = now

    def is_hour_allowed(self, now: datetime | None = None) -> bool:
        """True if ``now``'s UTC hour is in the current allow-set."""
        now = now or datetime.now(timezone.utc)
        if self._cached is None or self._cached_at is None or (
            (now - self._cached_at).total_seconds() >= self._ttl
        ):
            self._refresh(now)
        return now.hour in (self._cached or set(range(24)))

    def allowed(self, now: datetime | None = None) -> set[int]:
        """Return a copy of the current allow-set (refreshes on TTL)."""
        now = now or datetime.now(timezone.utc)
        if self._cached is None or self._cached_at is None or (
            (now - self._cached_at).total_seconds() >= self._ttl
        ):
            self._refresh(now)
        return set(self._cached or set(range(24)))
