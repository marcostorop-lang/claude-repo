"""Tests for the new bot risk manager."""

from __future__ import annotations

import asyncio
import time

import pytest

from bot.core.risk_manager import RiskManager, Position
from bot.core.utils import BookSnapshot, Side, TradeSignal, kelly_size


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


class TestPositionExits:
    """Tests for stop-loss, take-profit, and max-hold exit logic."""

    def _make_signal(self, **overrides) -> TradeSignal:
        defaults = dict(
            strategy="test", token_id="tok1", condition_id="cond1",
            side=Side.BUY, price=0.50, edge=0.10, confidence=0.70,
            probability=0.60,
        )
        defaults.update(overrides)
        return TradeSignal(**defaults)

    @staticmethod
    def _book_at(price: float):
        async def _get_book(token_id: str) -> BookSnapshot:
            return BookSnapshot(
                token_id=token_id,
                best_bid=price - 0.01,
                best_ask=price + 0.01,
            )
        return _get_book

    def test_stop_loss_triggers_exit(self):
        rm = RiskManager()
        sig = self._make_signal()
        rm.open_position(sig, fill_price=0.50, fill_size=20)
        # Price dropped to 0.40 → -20% loss > 15% stop-loss
        exits = asyncio.run(rm.check_exits(self._book_at(0.40)))
        assert len(exits) == 1
        assert "stop_loss" in exits[0]["reason"]
        assert exits[0]["pnl"] < 0
        assert "tok1" not in rm.positions

    def test_take_profit_triggers_exit(self):
        rm = RiskManager()
        sig = self._make_signal()
        rm.open_position(sig, fill_price=0.50, fill_size=20)
        # Price rose to 0.65 → +30% gain > 25% take-profit
        exits = asyncio.run(rm.check_exits(self._book_at(0.65)))
        assert len(exits) == 1
        assert "take_profit" in exits[0]["reason"]
        assert exits[0]["pnl"] > 0

    def test_max_hold_triggers_exit(self):
        rm = RiskManager()
        sig = self._make_signal()
        rm.open_position(sig, fill_price=0.50, fill_size=20)
        # Backdate the position to 80 hours ago (> 72h default)
        rm.positions["tok1"].timestamp = time.time() - 80 * 3600
        exits = asyncio.run(rm.check_exits(self._book_at(0.50)))
        assert len(exits) == 1
        assert "max_hold" in exits[0]["reason"]

    def test_no_exit_within_thresholds(self):
        rm = RiskManager()
        sig = self._make_signal()
        rm.open_position(sig, fill_price=0.50, fill_size=20)
        # Price at 0.52 → +4% gain, well within thresholds
        exits = asyncio.run(rm.check_exits(self._book_at(0.52)))
        assert len(exits) == 0
        assert "tok1" in rm.positions

    def test_sell_side_stop_loss(self):
        rm = RiskManager()
        sig = self._make_signal(side=Side.SELL)
        rm.open_position(sig, fill_price=0.50, fill_size=20)
        # Price rose to 0.60 → SELL position loses 20% > 15% stop-loss
        exits = asyncio.run(rm.check_exits(self._book_at(0.60)))
        assert len(exits) == 1
        assert "stop_loss" in exits[0]["reason"]

    def test_multiple_positions_checked(self):
        rm = RiskManager()
        rm.open_position(self._make_signal(token_id="t1"), fill_price=0.50, fill_size=10)
        rm.open_position(self._make_signal(token_id="t2"), fill_price=0.50, fill_size=10)
        # t1 hits stop-loss, t2 is fine
        async def mixed_book(token_id):
            if token_id == "t1":
                return BookSnapshot(token_id=token_id, best_bid=0.39, best_ask=0.41)
            return BookSnapshot(token_id=token_id, best_bid=0.49, best_ask=0.51)
        exits = asyncio.run(rm.check_exits(mixed_book))
        assert len(exits) == 1
        assert exits[0]["token_id"].startswith("t1")
        assert "t2" in rm.positions


class TestCalibrationGate:
    """Tests for the Brier gate on live trading."""

    def test_paper_mode_always_passes(self):
        rm = RiskManager()
        ok, msg = rm.check_calibration_gate()
        assert ok is True
        assert "paper" in msg.lower()


class TestBudgetTracker:
    """Tests for the Claude API budget cap."""

    def test_record_usage_tracks_cost(self):
        from bot.core.claude_oracle import _BudgetTracker
        bt = _BudgetTracker()
        bt.record_usage(input_tokens=1000, output_tokens=1000, cached_tokens=500)
        assert bt.daily_spend > 0
        assert bt.total_calls == 1

    def test_budget_exceeded_flag(self):
        from bot.core.claude_oracle import _BudgetTracker
        bt = _BudgetTracker()
        # Simulate massive usage to exceed default $10 budget
        for _ in range(1000):
            bt.record_usage(input_tokens=10000, output_tokens=5000)
        assert bt.is_budget_exceeded is True
        assert bt.budget_remaining == 0

    def test_daily_reset(self):
        from bot.core.claude_oracle import _BudgetTracker
        bt = _BudgetTracker()
        bt.record_usage(input_tokens=1000, output_tokens=500)
        assert bt.daily_spend > 0
        # Force a date change
        bt._daily_date = "2020-01-01"
        assert bt.daily_spend == 0.0

    def test_cost_calculation_accuracy(self):
        from bot.core.claude_oracle import _BudgetTracker, _INPUT_COST_PER_1K, _OUTPUT_COST_PER_1K, _CACHED_INPUT_COST_PER_1K
        bt = _BudgetTracker()
        bt.record_usage(input_tokens=2000, output_tokens=1000, cached_tokens=500)
        expected = (
            1500 / 1000 * _INPUT_COST_PER_1K
            + 500 / 1000 * _CACHED_INPUT_COST_PER_1K
            + 1000 / 1000 * _OUTPUT_COST_PER_1K
        )
        assert bt.daily_spend == pytest.approx(expected)

    def test_summary_dict(self):
        from bot.core.claude_oracle import _BudgetTracker
        bt = _BudgetTracker()
        bt.record_usage(input_tokens=1000, output_tokens=500)
        s = bt.summary()
        assert "daily_spend_usd" in s
        assert "budget_limit_usd" in s
        assert "budget_remaining_usd" in s
        assert "budget_exceeded" in s
        assert "total_calls" in s
        assert s["total_calls"] == 1
