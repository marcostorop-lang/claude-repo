"""Tests for the new bot risk manager."""

from __future__ import annotations

import pytest

from bot.core.risk_manager import RiskManager, Position
from bot.core.utils import Side, TradeSignal, kelly_size


class TestKellySize:
    def test_positive_edge_returns_positive(self):
        s = kelly_size(edge=0.10, win_prob=0.60, fraction=0.5, bankroll=1000, max_bet=100)
        assert s > 0
        assert s <= 100

    def test_zero_edge_returns_zero(self):
        assert kelly_size(edge=0, win_prob=0.6) == 0

    def test_negative_edge_returns_zero(self):
        assert kelly_size(edge=-0.05, win_prob=0.5) == 0

    def test_extreme_win_prob_returns_zero(self):
        assert kelly_size(edge=0.1, win_prob=0.0) == 0
        assert kelly_size(edge=0.1, win_prob=1.0) == 0

    def test_max_bet_cap(self):
        s = kelly_size(edge=0.5, win_prob=0.9, fraction=1.0, bankroll=10000, max_bet=50)
        assert s <= 50


class TestRiskManager:
    def _make_signal(self, **overrides) -> TradeSignal:
        defaults = dict(
            strategy="test",
            token_id="tok1",
            condition_id="cond1",
            side=Side.BUY,
            price=0.50,
            edge=0.10,
            confidence=0.70,
            probability=0.60,
        )
        defaults.update(overrides)
        return TradeSignal(**defaults)

    def test_approve_valid_signal(self):
        rm = RiskManager()
        sig = self._make_signal()
        v = rm.approve(sig)
        assert v.approved is True
        assert v.adjusted_size_usd > 0

    def test_reject_duplicate_position(self):
        rm = RiskManager()
        sig = self._make_signal()
        rm.positions["tok1"] = Position(
            token_id="tok1", condition_id="cond1",
            side=Side.BUY, size=10, entry_price=0.5, strategy="test",
        )
        v = rm.approve(sig)
        assert v.approved is False
        assert "open position" in v.reason.lower()

    def test_circuit_breaker(self):
        rm = RiskManager()
        rm.record_pnl(-60.0)  # exceeds default $50 limit
        v = rm.approve(self._make_signal())
        assert v.approved is False
        assert "halted" in v.reason.lower()

    def test_drawdown_stop(self):
        rm = RiskManager()
        rm._current_equity = 750
        rm._peak_equity = 1000
        assert rm.drawdown_pct == pytest.approx(0.25)
        assert rm.is_halted is True

    def test_max_positions(self):
        rm = RiskManager()
        for i in range(10):
            rm.positions[f"tok{i}"] = Position(
                token_id=f"tok{i}", condition_id=f"c{i}",
                side=Side.BUY, size=1, entry_price=0.5, strategy="t",
            )
        v = rm.approve(self._make_signal(token_id="tok99"))
        assert v.approved is False
        assert "max positions" in v.reason.lower()

    def test_close_position_records_pnl(self):
        rm = RiskManager()
        sig = self._make_signal()
        rm.open_position(sig, fill_price=0.50, fill_size=20)
        pnl = rm.close_position("tok1", exit_price=0.60)
        assert pnl == pytest.approx(2.0)  # (0.60 - 0.50) * 20
        assert rm._total_realized_pnl == pytest.approx(2.0)

    def test_daily_reset(self):
        from datetime import date, timedelta
        rm = RiskManager()
        rm.record_pnl(-40)
        rm._daily_date = date.today() - timedelta(days=1)
        rm._maybe_reset_daily()
        assert rm._daily_pnl == 0.0
        assert rm._circuit_breaker is False

    def test_zero_edge_signal_rejected(self):
        rm = RiskManager()
        sig = self._make_signal(edge=0.0)
        v = rm.approve(sig)
        assert v.approved is False

    def test_exposure_tracking(self):
        rm = RiskManager()
        rm.positions["t1"] = Position(
            token_id="t1", condition_id="c1",
            side=Side.BUY, size=100, entry_price=0.5, strategy="t",
        )
        assert rm.total_exposure == pytest.approx(50.0)
        assert rm.exposure_for_condition("c1") == pytest.approx(50.0)
        assert rm.exposure_for_condition("c2") == pytest.approx(0.0)
