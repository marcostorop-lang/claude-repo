"""Tests for the anti-pump filter.

Covers:
* Pump detected (up/down) when abs return >= threshold.
* No block when return is within threshold.
* Cold-start: insufficient history → allow.
* Non-numeric and zero-base edge cases.
* Configurable window and threshold.
* RiskManager integration: BUY blocked, SELL allowed, feature off.
"""

from __future__ import annotations

import pytest

from src.risk.anti_pump import AntiPumpVerdict, check_pump


# -- check_pump standalone ---------------------------------------------------


class TestCheckPump:
    def test_pump_up_detected(self):
        prices = [0.50] * 10 + [0.60]  # +20% over 10 points
        v = check_pump(prices, window_points=10, threshold=0.10)
        assert v.blocked is True
        assert v.abs_return == pytest.approx(0.20)
        assert "up" in v.reason

    def test_pump_down_detected(self):
        prices = [0.50] * 10 + [0.40]  # -20%
        v = check_pump(prices, window_points=10, threshold=0.10)
        assert v.blocked is True
        assert v.abs_return == pytest.approx(0.20)
        assert "down" in v.reason

    def test_within_threshold_not_blocked(self):
        prices = [0.50] * 10 + [0.54]  # +8%
        v = check_pump(prices, window_points=10, threshold=0.10)
        assert v.blocked is False
        assert v.abs_return == pytest.approx(0.08)

    def test_exactly_at_threshold_blocked(self):
        prices = [0.50] * 10 + [0.55]  # +10% == threshold
        v = check_pump(prices, window_points=10, threshold=0.10)
        assert v.blocked is True

    def test_insufficient_history_not_blocked(self):
        prices = [0.50] * 5  # need 11
        v = check_pump(prices, window_points=10, threshold=0.10)
        assert v.blocked is False
        assert "insufficient" in v.reason

    def test_zero_base_not_blocked(self):
        prices = [0.0] * 10 + [0.50]
        v = check_pump(prices, window_points=10, threshold=0.10)
        assert v.blocked is False
        assert "zero" in v.reason

    def test_custom_window(self):
        prices = [0.50, 0.50, 0.50, 0.60]  # 3-point window: +20%
        v = check_pump(prices, window_points=3, threshold=0.10)
        assert v.blocked is True

    def test_custom_threshold(self):
        prices = [0.50] * 10 + [0.52]  # +4%
        v = check_pump(prices, window_points=10, threshold=0.03)
        assert v.blocked is True

    def test_non_numeric_not_blocked(self):
        prices = ["abc"] * 10 + [0.5]  # type: ignore[list-item]
        v = check_pump(prices, window_points=10, threshold=0.10)
        assert v.blocked is False


# -- RiskManager integration -------------------------------------------------


class TestRiskManagerIntegration:
    def _make_rm(self, *, enabled=True, threshold=0.10, window=10):
        from src.config import Config
        from src.portfolio.tracker import PortfolioTracker
        from src.risk.manager import RiskManager
        cfg = Config()
        object.__setattr__(cfg, "max_position_size", 100.0)
        object.__setattr__(cfg, "max_total_exposure", 500.0)
        object.__setattr__(cfg, "anti_pump_enabled", enabled)
        object.__setattr__(cfg, "anti_pump_threshold", threshold)
        object.__setattr__(cfg, "anti_pump_window_points", window)
        rm = RiskManager(cfg, PortfolioTracker())
        return rm

    def test_buy_blocked_when_pumped(self):
        from src.strategy.base import Action, Signal
        rm = self._make_rm()
        rm.get_price_history = lambda tid, limit: [0.50] * 10 + [0.60]
        sig = Signal(action=Action.BUY, confidence=1.0, reason="t", features={})
        v = rm.check("tok1", sig, proposed_size=10.0, price=0.5)
        assert v.allowed is False
        assert "Anti-pump" in v.reason

    def test_buy_allowed_when_calm(self):
        from src.strategy.base import Action, Signal
        rm = self._make_rm()
        rm.get_price_history = lambda tid, limit: [0.50] * 10 + [0.51]
        sig = Signal(action=Action.BUY, confidence=1.0, reason="t", features={})
        v = rm.check("tok1", sig, proposed_size=10.0, price=0.5)
        assert v.allowed is True

    def test_sell_never_blocked(self):
        from src.strategy.base import Action, Signal
        rm = self._make_rm()
        rm.get_price_history = lambda tid, limit: [0.50] * 10 + [0.60]
        sig = Signal(action=Action.SELL, confidence=1.0, reason="exit", features={})
        v = rm.check("tok1", sig, proposed_size=10.0, price=0.5)
        assert v.allowed is True

    def test_disabled_no_block(self):
        from src.strategy.base import Action, Signal
        rm = self._make_rm(enabled=False)
        rm.get_price_history = lambda tid, limit: [0.50] * 10 + [0.60]
        sig = Signal(action=Action.BUY, confidence=1.0, reason="t", features={})
        v = rm.check("tok1", sig, proposed_size=10.0, price=0.5)
        assert v.allowed is True

    def test_no_loader_no_block(self):
        from src.strategy.base import Action, Signal
        rm = self._make_rm()
        rm.get_price_history = None
        sig = Signal(action=Action.BUY, confidence=1.0, reason="t", features={})
        v = rm.check("tok1", sig, proposed_size=10.0, price=0.5)
        assert v.allowed is True

    def test_loader_exception_fails_open(self):
        from src.strategy.base import Action, Signal
        rm = self._make_rm()
        def _bad_loader(tid, limit):
            raise RuntimeError("boom")
        rm.get_price_history = _bad_loader
        sig = Signal(action=Action.BUY, confidence=1.0, reason="t", features={})
        v = rm.check("tok1", sig, proposed_size=10.0, price=0.5)
        assert v.allowed is True
