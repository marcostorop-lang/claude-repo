"""Tests for the price-volatility filter.

Covers:
* :func:`price_volatility` — pure stddev calculation and the
  "not enough data → None" contract,
* :func:`is_too_volatile` — the decision tuple including cold-start,
* :meth:`RiskManager.check` integration — BUY rejection, SELL never
  gated, missing loader = feature off, loader exceptions fail open.
"""

from __future__ import annotations

import pytest

from src.analysis.volatility import is_too_volatile, price_volatility
from src.config import Config
from src.portfolio.tracker import PortfolioTracker, Position
from src.risk.manager import RiskManager
from src.strategy.base import Action, Signal


def _cfg(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


# -- price_volatility pure math --------------------------------------------


class TestPriceVolatility:
    def test_insufficient_data_returns_none(self):
        assert price_volatility([], window=10) is None
        assert price_volatility([0.5] * 9, window=10) is None

    def test_constant_prices_zero(self):
        assert price_volatility([0.5] * 10, window=10) == pytest.approx(0.0)

    def test_known_stddev(self):
        # Prices 0.4, 0.5, 0.6 → mean 0.5, var = (0.01+0+0.01)/2 = 0.01
        # stddev = 0.1
        assert price_volatility([0.4, 0.5, 0.6], window=3) == pytest.approx(0.1)

    def test_uses_last_window_elements(self):
        # Old wild prices shouldn't count when we ask for the last 3.
        prices = [0.1, 0.9, 0.1, 0.5, 0.5, 0.5]
        assert price_volatility(prices, window=3) == pytest.approx(0.0)

    def test_window_below_two_returns_none(self):
        assert price_volatility([0.5], window=1) is None


class TestIsTooVolatile:
    def test_cold_start_not_flagged(self):
        too_vol, measured = is_too_volatile([], window=10, max_vol=0.05)
        assert too_vol is False
        assert measured is None

    def test_calm_prices_not_flagged(self):
        too_vol, measured = is_too_volatile(
            [0.50, 0.50, 0.51, 0.50, 0.49, 0.50, 0.50, 0.50, 0.50, 0.50],
            window=10, max_vol=0.05,
        )
        assert too_vol is False
        assert measured < 0.05

    def test_choppy_prices_flagged(self):
        too_vol, measured = is_too_volatile(
            [0.1, 0.9, 0.2, 0.8, 0.1, 0.9, 0.3, 0.7, 0.2, 0.8],
            window=10, max_vol=0.05,
        )
        assert too_vol is True
        assert measured > 0.05


# -- RiskManager integration -----------------------------------------------


class TestRiskManagerIntegration:
    def _make(self, cfg, history_by_token: dict[str, list[float]]):
        rm = RiskManager(cfg, PortfolioTracker())
        rm.get_price_history = lambda token_id, limit: history_by_token.get(
            token_id, []
        )
        return rm

    def test_buy_rejected_when_too_volatile(self):
        cfg = _cfg(
            volatility_filter_enabled=True,
            volatility_window=10,
            max_price_volatility=0.05,
        )
        rm = self._make(cfg, {"tok1": [0.1, 0.9, 0.2, 0.8, 0.1, 0.9, 0.3, 0.7, 0.2, 0.8]})
        sig = Signal(action=Action.BUY, confidence=0.9, reason="test", features={})
        v = rm.check("tok1", sig, proposed_size=10, price=0.5)
        assert v.allowed is False
        assert "Volatility" in v.reason

    def test_buy_allowed_when_calm(self):
        cfg = _cfg(
            volatility_filter_enabled=True,
            volatility_window=10,
            max_price_volatility=0.05,
        )
        rm = self._make(cfg, {"tok1": [0.50] * 10})
        sig = Signal(action=Action.BUY, confidence=0.9, reason="test", features={})
        v = rm.check("tok1", sig, proposed_size=10, price=0.5)
        assert "Volatility" not in v.reason

    def test_cold_start_not_blocked(self):
        cfg = _cfg(
            volatility_filter_enabled=True,
            volatility_window=10,
            max_price_volatility=0.05,
        )
        rm = self._make(cfg, {})  # no history for tok1
        sig = Signal(action=Action.BUY, confidence=0.9, reason="test", features={})
        v = rm.check("tok1", sig, proposed_size=10, price=0.5)
        assert "Volatility" not in v.reason

    def test_sell_never_gated_by_volatility(self):
        """SELL exits must always fire — even on a chaotic market."""
        cfg = _cfg(
            volatility_filter_enabled=True,
            volatility_window=10,
            max_price_volatility=0.05,
        )
        rm = self._make(cfg, {"tok1": [0.1, 0.9, 0.2, 0.8, 0.1, 0.9, 0.3, 0.7, 0.2, 0.8]})
        rm.portfolio.open_position(Position(
            token_id="tok1", condition_id="c1", side="BUY", size=10,
            entry_price=0.5, strategy="s", order_id="o",
        ))
        sig = Signal(action=Action.SELL, confidence=0.9, reason="exit", features={})
        v = rm.check("tok1", sig, proposed_size=10, price=0.5)
        assert "Volatility" not in v.reason

    def test_no_loader_no_gating(self):
        """Loader None → feature off, BUYs pass."""
        cfg = _cfg(
            volatility_filter_enabled=True,
            volatility_window=10,
            max_price_volatility=0.05,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        assert rm.get_price_history is None
        sig = Signal(action=Action.BUY, confidence=0.9, reason="test", features={})
        v = rm.check("tok1", sig, proposed_size=10, price=0.5)
        assert "Volatility" not in v.reason

    def test_loader_exception_fails_open(self):
        """A loader error must not freeze trading — log + allow."""
        cfg = _cfg(
            volatility_filter_enabled=True,
            volatility_window=10,
            max_price_volatility=0.05,
        )
        rm = RiskManager(cfg, PortfolioTracker())

        def bad(token_id, limit):
            raise RuntimeError("db error")

        rm.get_price_history = bad
        sig = Signal(action=Action.BUY, confidence=0.9, reason="test", features={})
        v = rm.check("tok1", sig, proposed_size=10, price=0.5)
        assert "Volatility" not in v.reason

    def test_disabled_flag_short_circuits(self):
        """With the flag False, we don't even call the loader."""
        cfg = _cfg(volatility_filter_enabled=False)
        rm = RiskManager(cfg, PortfolioTracker())
        called = {"n": 0}

        def tracker(token_id, limit):
            called["n"] += 1
            return []

        rm.get_price_history = tracker
        sig = Signal(action=Action.BUY, confidence=0.9, reason="test", features={})
        rm.check("tok1", sig, proposed_size=10, price=0.5)
        assert called["n"] == 0
