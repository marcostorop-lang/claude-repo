"""Tests for daily-PnL reconstruction across bot restarts.

The bug this module guards against: if the bot crashes after losing
$40 on a $50 daily-loss limit, a naive restart re-seeds the circuit
breaker to zero — giving the operator a fresh $50 of headroom that
the original day never would have had.  Accounting-incorrect in the
exact direction the circuit breaker is meant to prevent.

These tests exercise:

* ``PortfolioTracker.reconstruct_from_trades`` returns
  ``realised_pnl_today`` counting only exits booked on the current UTC
  day (not yesterday, not last week, not mangled timestamps),
* ``RiskManager.seed_daily_pnl`` restores the counter and trips the
  breaker immediately when the reconstructed loss already breaches
  the limit,
* the two compose correctly through a realistic trade sequence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager


# -- Helpers ---------------------------------------------------------------


def _cfg(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


def _today_utc(h=12, m=0) -> str:
    now = datetime.now(timezone.utc).replace(hour=h, minute=m, second=0, microsecond=0)
    return now.isoformat()


def _yesterday_utc(h=12) -> str:
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).replace(hour=h, minute=0, second=0, microsecond=0)
    return yesterday.isoformat()


def _trade(token="t1", cond="c1", side="BUY", size=10.0, price=0.50,
           timestamp="", order_id="o") -> dict:
    return {
        "token_id": token, "condition_id": cond, "side": side,
        "size": size, "price": price, "timestamp": timestamp,
        "order_id": order_id, "strategy": "s", "category": "",
    }


# -- Reconstruct returns today's PnL ---------------------------------------


class TestReconstructTodayPnl:
    def test_empty_trades(self):
        pt = PortfolioTracker()
        stats = pt.reconstruct_from_trades([])
        assert stats["realised_pnl_today"] == 0.0
        assert stats["realised_pnl"] == 0.0

    def test_open_only_no_pnl_yet(self):
        pt = PortfolioTracker()
        stats = pt.reconstruct_from_trades([
            _trade(side="BUY", size=10, price=0.50, timestamp=_today_utc()),
        ])
        assert stats["realised_pnl_today"] == pytest.approx(0.0)
        assert stats["open_positions"] == 1

    def test_close_today_booked_to_today(self):
        pt = PortfolioTracker()
        stats = pt.reconstruct_from_trades([
            _trade(side="BUY", size=10, price=0.50, timestamp=_today_utc(h=9)),
            _trade(side="SELL", size=10, price=0.45, timestamp=_today_utc(h=11)),
        ])
        # Lost $0.05 * 10 = $0.50 today.
        assert stats["realised_pnl"] == pytest.approx(-0.50)
        assert stats["realised_pnl_today"] == pytest.approx(-0.50)

    def test_close_yesterday_not_counted_today(self):
        pt = PortfolioTracker()
        stats = pt.reconstruct_from_trades([
            _trade(side="BUY", size=10, price=0.50, timestamp=_yesterday_utc(h=9)),
            _trade(side="SELL", size=10, price=0.60, timestamp=_yesterday_utc(h=11)),
        ])
        # Realised yesterday; today's counter must not include it.
        assert stats["realised_pnl"] == pytest.approx(1.00)
        assert stats["realised_pnl_today"] == pytest.approx(0.0)

    def test_mixed_days(self):
        """BUY yesterday, SELL today → today's counter captures the loss."""
        pt = PortfolioTracker()
        stats = pt.reconstruct_from_trades([
            _trade(side="BUY", size=10, price=0.50, timestamp=_yesterday_utc(h=9)),
            _trade(side="BUY", size=5, price=0.60, timestamp=_yesterday_utc(h=15)),
            # SELL today: closes the (weighted-avg entry 0.533...) long.
            _trade(side="SELL", size=15, price=0.40, timestamp=_today_utc(h=10)),
        ])
        # Weighted entry = (10*0.50 + 5*0.60)/15 = 0.5333...
        # Close @ 0.40 → pnl = (0.40 - 0.5333...) * 15 = -2.0
        assert stats["realised_pnl"] == pytest.approx(-2.0, abs=1e-6)
        # The close happened today, so the full pnl is today's.
        assert stats["realised_pnl_today"] == pytest.approx(-2.0, abs=1e-6)

    def test_mangled_timestamp_is_safe(self):
        """Bad timestamp string must not crash — just excluded from 'today'."""
        pt = PortfolioTracker()
        stats = pt.reconstruct_from_trades([
            _trade(side="BUY", size=10, price=0.50, timestamp="not-an-iso-date"),
            _trade(side="SELL", size=10, price=0.60, timestamp=""),
        ])
        # PnL is still accumulated (trade happened, side is opposing),
        # it just doesn't count as "today" since timestamps are unparseable.
        assert stats["realised_pnl"] == pytest.approx(1.0)
        assert stats["realised_pnl_today"] == pytest.approx(0.0)

    def test_partial_close_today_counted_pro_rata(self):
        pt = PortfolioTracker()
        stats = pt.reconstruct_from_trades([
            _trade(side="BUY", size=10, price=0.50, timestamp=_yesterday_utc()),
            _trade(side="SELL", size=4, price=0.60, timestamp=_today_utc(h=9)),
        ])
        # Closing 4 @ 0.60 against entry 0.50 → +0.40 realised today.
        assert stats["realised_pnl"] == pytest.approx(0.40)
        assert stats["realised_pnl_today"] == pytest.approx(0.40)
        # 6 units still open.
        assert pt.positions["t1"].size == pytest.approx(6.0)


# -- seed_daily_pnl on RiskManager -----------------------------------------


class TestSeedDailyPnl:
    def test_seeds_counter(self):
        cfg = _cfg(max_daily_loss=50.0)
        rm = RiskManager(cfg, PortfolioTracker())
        rm.seed_daily_pnl(-20.0)
        assert rm.daily_pnl == pytest.approx(-20.0)
        assert rm.is_circuit_breaker_active is False

    def test_trips_breaker_when_reconstructed_loss_exceeds_limit(self):
        cfg = _cfg(max_daily_loss=50.0)
        rm = RiskManager(cfg, PortfolioTracker())
        rm.seed_daily_pnl(-60.0)
        assert rm.is_circuit_breaker_active is True

    def test_subsequent_record_accumulates(self):
        cfg = _cfg(max_daily_loss=50.0)
        rm = RiskManager(cfg, PortfolioTracker())
        rm.seed_daily_pnl(-30.0)
        rm.record_realized_pnl(-25.0)   # total -55 → breach
        assert rm.is_circuit_breaker_active is True

    def test_repeat_seed_overwrites_not_stacks(self):
        """Operator re-running reconstruction mid-day must not double-count."""
        cfg = _cfg(max_daily_loss=50.0)
        rm = RiskManager(cfg, PortfolioTracker())
        rm.seed_daily_pnl(-20.0)
        rm.seed_daily_pnl(-20.0)
        assert rm.daily_pnl == pytest.approx(-20.0)


# -- End-to-end: reconstruction → seeding → breaker ------------------------


class TestEndToEnd:
    def test_crash_then_restart_preserves_daily_breaker(self):
        """Happy-path: a crashed day is fully restored."""
        cfg = _cfg(max_daily_loss=50.0)
        pt = PortfolioTracker()
        trades = [
            _trade(side="BUY", size=100, price=0.50, timestamp=_today_utc(h=9)),
            _trade(side="SELL", size=100, price=0.40, timestamp=_today_utc(h=10)),  # -10
            _trade(side="BUY", size=100, price=0.50, timestamp=_today_utc(h=11)),
            _trade(side="SELL", size=100, price=0.05, timestamp=_today_utc(h=12)),  # -45
        ]
        stats = pt.reconstruct_from_trades(trades)
        assert stats["realised_pnl_today"] == pytest.approx(-55.0)

        rm = RiskManager(cfg, pt)
        rm.seed_daily_pnl(stats["realised_pnl_today"])
        # Today already blew through the -$50 limit → breaker stays tripped
        # until UTC midnight, exactly as it would have been pre-crash.
        assert rm.is_circuit_breaker_active is True
        assert rm.daily_pnl == pytest.approx(-55.0)
