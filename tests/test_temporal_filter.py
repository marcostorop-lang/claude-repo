"""Tests for the temporal edge filter.

Covers three layers:

* ``compute_hourly_winrate`` — grouping closed trades into UTC-hour
  buckets and counting wins/losses,
* ``allowed_hours`` — turning those buckets into an allow-set, with
  the cold-start fail-safe (no data → all 24 hours),
* ``TemporalFilter`` wiring into ``RiskManager.check`` so BUY signals
  fire in allowed hours and are rejected in disallowed ones, while
  SELL signals are never gated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.temporal_edge import (
    HourlyStats,
    TemporalFilter,
    allowed_hours,
    compute_hourly_winrate,
)
from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.strategy.base import Action, Signal


# -- Helpers ---------------------------------------------------------------


def _cfg(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


def _cal(entry_hour: int, pnl: float, days_ago: int = 0) -> dict:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    ts = now - timedelta(days=days_ago)
    ts = ts.replace(hour=entry_hour)
    return {"entry_timestamp": ts.isoformat(), "pnl": pnl}


# -- compute_hourly_winrate -------------------------------------------------


class TestComputeHourlyWinrate:
    def test_empty(self):
        assert compute_hourly_winrate([]) == {}

    def test_single_win(self):
        stats = compute_hourly_winrate([_cal(entry_hour=14, pnl=1.0)])
        assert 14 in stats
        assert stats[14].n_trades == 1 and stats[14].n_wins == 1
        assert stats[14].win_rate == pytest.approx(1.0)

    def test_mixed_same_hour(self):
        rows = [
            _cal(entry_hour=9, pnl=2.0),
            _cal(entry_hour=9, pnl=-1.0),
            _cal(entry_hour=9, pnl=3.0),
            _cal(entry_hour=9, pnl=-0.5),
        ]
        stats = compute_hourly_winrate(rows)
        assert stats[9].n_trades == 4
        assert stats[9].n_wins == 2
        assert stats[9].win_rate == pytest.approx(0.5)

    def test_window_cutoff_excludes_old(self):
        rows = [
            _cal(entry_hour=12, pnl=1.0, days_ago=0),
            _cal(entry_hour=12, pnl=1.0, days_ago=100),  # older than window
        ]
        stats = compute_hourly_winrate(rows, window_days=30)
        assert stats[12].n_trades == 1

    def test_mangled_timestamp_skipped(self):
        rows = [
            _cal(entry_hour=10, pnl=1.0),
            {"entry_timestamp": "not-iso", "pnl": 1.0},
            {"entry_timestamp": "", "pnl": 1.0},
            {"entry_timestamp": None, "pnl": 1.0},
        ]
        stats = compute_hourly_winrate(rows)
        assert sum(s.n_trades for s in stats.values()) == 1

    def test_missing_pnl_skipped(self):
        rows = [
            _cal(entry_hour=10, pnl=1.0),
            {"entry_timestamp": _cal(10, 0)["entry_timestamp"], "pnl": None},
        ]
        stats = compute_hourly_winrate(rows)
        assert stats[10].n_trades == 1

    def test_zero_pnl_is_not_a_win(self):
        stats = compute_hourly_winrate([_cal(entry_hour=3, pnl=0.0)])
        assert stats[3].n_trades == 1 and stats[3].n_wins == 0


# -- allowed_hours ---------------------------------------------------------


class TestAllowedHours:
    def test_cold_start_returns_all_24(self):
        """No qualifying hour (all buckets under min_samples) → allow all."""
        stats = {9: HourlyStats(9, 5, 5)}  # 100% but only 5 samples
        out = allowed_hours(stats, min_winrate=0.65, min_samples=20)
        assert out == set(range(24))

    def test_selective_once_any_hour_qualifies(self):
        stats = {
            9: HourlyStats(9, 30, 22),   # 73% → allowed
            10: HourlyStats(10, 30, 15),  # 50% → blocked
            11: HourlyStats(11, 5, 5),    # too few samples → blocked
        }
        out = allowed_hours(stats, min_winrate=0.65, min_samples=20)
        assert out == {9}

    def test_empty_stats_allows_all(self):
        assert allowed_hours({}, min_winrate=0.65, min_samples=20) == set(range(24))

    def test_exact_threshold_allowed(self):
        stats = {5: HourlyStats(5, 20, 13)}  # 0.65 exactly
        out = allowed_hours(stats, min_winrate=0.65, min_samples=20)
        assert 5 in out


# -- TemporalFilter wiring -------------------------------------------------


class TestTemporalFilter:
    def test_cold_start_allows_every_hour(self):
        tf = TemporalFilter(
            load_calibration=lambda: [],
            min_winrate=0.65, min_samples=20, window_days=30,
        )
        assert tf.allowed() == set(range(24))
        assert tf.is_hour_allowed() is True

    def test_cache_refreshes_on_ttl(self):
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return []

        tf = TemporalFilter(loader, 0.65, 20, 30, ttl_seconds=3600)
        tf.is_hour_allowed()
        tf.is_hour_allowed()
        assert calls["n"] == 1  # second call used cache

        # Force TTL expiry by rewinding the cache timestamp.
        tf._cached_at = datetime.now(timezone.utc) - timedelta(hours=2)
        tf.is_hour_allowed()
        assert calls["n"] == 2

    def test_loader_exception_is_swallowed(self):
        def bad():
            raise RuntimeError("db down")

        tf = TemporalFilter(bad, 0.65, 20, 30)
        # Exception is caught — fail open (allow all hours).
        assert tf.is_hour_allowed() is True


# -- RiskManager integration -----------------------------------------------


class _StubFilter:
    def __init__(self, allowed: bool):
        self._allowed = allowed

    def is_hour_allowed(self, now=None):
        return self._allowed


class TestRiskManagerIntegration:
    def _make(self, allowed: bool):
        cfg = _cfg(
            temporal_filter_enabled=True,
            temporal_min_winrate=0.65,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        rm.temporal_filter = _StubFilter(allowed=allowed)
        return rm

    def test_buy_rejected_in_disallowed_hour(self):
        rm = self._make(allowed=False)
        sig = Signal(action=Action.BUY, confidence=0.9, reason="test", features={})
        v = rm.check("tok1", sig, proposed_size=10, price=0.5)
        assert v.allowed is False
        assert "Temporal filter" in v.reason

    def test_buy_allowed_in_allowed_hour(self):
        rm = self._make(allowed=True)
        sig = Signal(action=Action.BUY, confidence=0.9, reason="test", features={})
        v = rm.check("tok1", sig, proposed_size=10, price=0.5)
        # May still pass other checks — key is it's not the temporal reject.
        assert "Temporal filter" not in v.reason

    def test_sell_not_gated_by_temporal_filter(self):
        """SELL exits must always fire — never strand a position on the clock."""
        rm = self._make(allowed=False)
        # Open a position so SELL is meaningful.
        from src.portfolio.tracker import Position
        rm.portfolio.open_position(Position(
            token_id="tok1", condition_id="c1", side="BUY", size=10,
            entry_price=0.5, strategy="s", order_id="o",
        ))
        sig = Signal(action=Action.SELL, confidence=0.9, reason="test", features={})
        v = rm.check("tok1", sig, proposed_size=10, price=0.6)
        assert "Temporal filter" not in v.reason

    def test_no_filter_no_gating(self):
        """When temporal_filter is None (feature off), BUYs pass the hour check."""
        cfg = _cfg(temporal_filter_enabled=False)
        rm = RiskManager(cfg, PortfolioTracker())
        assert rm.temporal_filter is None
        sig = Signal(action=Action.BUY, confidence=0.9, reason="test", features={})
        v = rm.check("tok1", sig, proposed_size=10, price=0.5)
        assert "Temporal filter" not in v.reason
