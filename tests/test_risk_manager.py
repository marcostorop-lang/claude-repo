"""Tests for src.risk.manager."""

import os
from unittest import mock

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker, Position
from src.risk.manager import RiskManager
from src.strategy.base import Action, Signal


def _cfg(**overrides):
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "MAX_POSITION_SIZE": "50",
        "MAX_TOTAL_EXPOSURE": "200",
        "STOP_LOSS_PCT": "0.10",
        "TAKE_PROFIT_PCT": "0.20",
        "MAX_OPEN_POSITIONS": "3",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


class TestRiskManager:
    def test_hold_is_denied(self):
        cfg = _cfg()
        rm = RiskManager(cfg, PortfolioTracker())
        verdict = rm.check("tok1", Signal(Action.HOLD, 0.0), 10, 0.5)
        assert not verdict.allowed

    def test_buy_allowed_when_under_limits(self):
        cfg = _cfg()
        rm = RiskManager(cfg, PortfolioTracker())
        verdict = rm.check("tok1", Signal(Action.BUY, 0.8), 10, 0.5)
        assert verdict.allowed
        assert verdict.adjusted_size <= cfg.max_position_size

    def test_max_positions_blocks_buy(self):
        cfg = _cfg(MAX_OPEN_POSITIONS="1")
        portfolio = PortfolioTracker()
        portfolio.open_position(Position("t1", "c1", "BUY", 10, 0.5, "test", "o1"))
        rm = RiskManager(cfg, portfolio)
        verdict = rm.check("tok2", Signal(Action.BUY, 0.9), 10, 0.5)
        assert not verdict.allowed

    def test_exposure_cap_reduces_size(self):
        cfg = _cfg(MAX_TOTAL_EXPOSURE="100", MAX_POSITION_SIZE="200")
        portfolio = PortfolioTracker()
        portfolio.open_position(Position("t1", "c1", "BUY", 150, 0.5, "test", "o1"))
        rm = RiskManager(cfg, portfolio)
        # Existing exposure = 150 * 0.5 = 75.  Max = 100.  Available = 25.
        # Use price=0.50 (within default min/max price bounds)
        verdict = rm.check("tok2", Signal(Action.BUY, 0.8), 200, 0.50)
        assert verdict.allowed
        assert verdict.adjusted_size == pytest.approx(50.0)  # available=25, size=25/0.50=50

    def test_compute_position_size_basic(self):
        cfg = _cfg(MAX_POSITION_SIZE="100")
        rm = RiskManager(cfg, PortfolioTracker())
        size = rm.compute_position_size(price=0.50, confidence=0.8)
        assert size == pytest.approx(200.0)  # 100 / 0.50

    def test_compute_position_size_confidence_scaling(self):
        cfg = _cfg(MAX_POSITION_SIZE="100", SIZING_CONFIDENCE_SCALE="true")
        rm = RiskManager(cfg, PortfolioTracker())
        size = rm.compute_position_size(price=0.50, confidence=0.6)
        # 100 * 0.6 = 60 USD -> 60 / 0.50 = 120 shares
        assert size == pytest.approx(120.0)

    def test_compute_position_size_liquidity_cap(self):
        cfg = _cfg(MAX_POSITION_SIZE="100", MAX_LIQUIDITY_FRACTION="0.01")
        rm = RiskManager(cfg, PortfolioTracker())
        # Liquidity = 1000, max fraction = 1%, so max USD = 10
        size = rm.compute_position_size(price=0.50, confidence=0.8, liquidity=1000)
        assert size == pytest.approx(20.0)  # 10 / 0.50

    def test_compute_position_size_zero_price(self):
        cfg = _cfg()
        rm = RiskManager(cfg, PortfolioTracker())
        assert rm.compute_position_size(price=0.0, confidence=0.8) == 0.0

    def test_stop_loss(self):
        cfg = _cfg(STOP_LOSS_PCT="0.10")
        rm = RiskManager(cfg, PortfolioTracker())
        assert rm.check_stop_loss(1.0, 0.89) is True
        assert rm.check_stop_loss(1.0, 0.95) is False

    def test_take_profit(self):
        cfg = _cfg(TAKE_PROFIT_PCT="0.20")
        rm = RiskManager(cfg, PortfolioTracker())
        assert rm.check_take_profit(1.0, 1.21) is True
        assert rm.check_take_profit(1.0, 1.10) is False


class TestDrawdownBreaker:
    """MAX_DRAWDOWN_PCT — block new BUYs when equity falls past peak limit."""

    def test_cold_start_no_breaker(self):
        cfg = _cfg(MAX_DRAWDOWN_PCT="0.20")
        rm = RiskManager(cfg, PortfolioTracker())
        rm.update_equity(0.0)
        assert not rm.is_drawdown_breaker_active
        rm.update_equity(-5.0)  # negative cold-start: still no breaker
        assert not rm.is_drawdown_breaker_active

    def test_new_peak_updates_state(self):
        cfg = _cfg(MAX_DRAWDOWN_PCT="0.20")
        rm = RiskManager(cfg, PortfolioTracker())
        rm.update_equity(100.0)
        assert rm.peak_equity == 100.0
        rm.update_equity(120.0)
        assert rm.peak_equity == 120.0
        assert rm.current_drawdown_pct == 0.0

    def test_breaker_trips_below_threshold(self):
        cfg = _cfg(MAX_DRAWDOWN_PCT="0.20")
        rm = RiskManager(cfg, PortfolioTracker())
        rm.update_equity(100.0)
        rm.update_equity(85.0)  # 15% dd
        assert not rm.is_drawdown_breaker_active
        rm.update_equity(79.99)  # 20.01% dd
        assert rm.is_drawdown_breaker_active
        assert rm.current_drawdown_pct == pytest.approx(0.2001, rel=1e-3)

    def test_breaker_blocks_new_buys_only(self):
        cfg = _cfg(MAX_DRAWDOWN_PCT="0.20")
        rm = RiskManager(cfg, PortfolioTracker())
        rm.update_equity(100.0)
        rm.update_equity(70.0)  # 30% dd → tripped
        assert rm.is_drawdown_breaker_active
        # BUY rejected
        v_buy = rm.check("tok", Signal(Action.BUY, 0.8), 10, 0.5)
        assert not v_buy.allowed
        assert "Drawdown breaker" in v_buy.reason
        # SELL exits still allowed (can't strand positions)
        v_sell = rm.check("tok", Signal(Action.SELL, 0.8), 10, 0.5)
        assert v_sell.allowed or "Drawdown" not in v_sell.reason

    def test_breaker_resets_on_new_peak(self):
        cfg = _cfg(MAX_DRAWDOWN_PCT="0.20")
        rm = RiskManager(cfg, PortfolioTracker())
        rm.update_equity(100.0)
        rm.update_equity(70.0)
        assert rm.is_drawdown_breaker_active
        rm.update_equity(105.0)  # full recovery + new peak
        assert not rm.is_drawdown_breaker_active
        assert rm.peak_equity == 105.0

    def test_disabled_when_pct_zero(self):
        cfg = _cfg(MAX_DRAWDOWN_PCT="0")
        rm = RiskManager(cfg, PortfolioTracker())
        rm.update_equity(100.0)
        rm.update_equity(50.0)  # 50% dd, but feature off
        assert not rm.is_drawdown_breaker_active
