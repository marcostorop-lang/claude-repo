"""Tests for the composite multi-factor strategy."""

import os
from unittest import mock

import pytest

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.composite import CompositeStrategy
from src.strategy.base import Action


def _cfg(**overrides):
    env = {
        "MOMENTUM_WINDOW": "3",
        "MOMENTUM_THRESHOLD": "0.02",
        "MEAN_REVERSION_WINDOW": "5",
        "MEAN_REVERSION_ENTRY_Z": "1.5",
        "MAX_SPREAD": "0.15",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


def _snap(price=0.50, spread=0.02, end_date=""):
    return MarketSnapshot(
        condition_id="c1", question="Q?", token_id="t1",
        outcome="Yes", price=price, spread=spread,
        volume=1e6, liquidity=1e5, active=True, end_date=end_date,
    )


class TestCompositeStrategy:
    def test_insufficient_history_holds(self):
        cfg = _cfg()
        strat = CompositeStrategy(cfg)
        sig = strat.evaluate(_snap(), [0.50, 0.51])
        assert sig.action == Action.HOLD
        assert "Not enough history" in sig.reason

    def test_strong_uptrend_generates_buy(self):
        cfg = _cfg()
        strat = CompositeStrategy(cfg)
        # Strong uptrend: last 3 prices surge from 0.50 → 0.70 (40% momentum)
        # MR window (5) sees gradual rise so won't fight as hard
        history = [0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.65, 0.68, 0.70]
        sig = strat.evaluate(_snap(price=0.70, spread=0.01), history)
        # Composite score should be positive (momentum dominates)
        assert sig.features["composite_score"] > 0
        assert sig.features["momentum_score"] > 0
        assert "composite_score" in sig.features
        # With 60% weight on momentum that's at 1.0, we expect BUY
        assert sig.action == Action.BUY

    def test_flat_market_holds(self):
        cfg = _cfg()
        strat = CompositeStrategy(cfg)
        history = [0.50] * 10
        sig = strat.evaluate(_snap(price=0.50), history)
        assert sig.action == Action.HOLD

    def test_wide_spread_reduces_score(self):
        cfg = _cfg()
        strat = CompositeStrategy(cfg)
        history = [0.40, 0.41, 0.42, 0.43, 0.45, 0.48, 0.50, 0.53, 0.56, 0.60]

        sig_narrow = strat.evaluate(_snap(price=0.60, spread=0.01), history)
        sig_wide = strat.evaluate(_snap(price=0.60, spread=0.14), history)

        # Both may or may not trigger BUY, but narrow should have higher confidence
        assert sig_narrow.features["spread_score"] > sig_wide.features["spread_score"]

    def test_features_are_populated(self):
        cfg = _cfg()
        strat = CompositeStrategy(cfg)
        history = [0.40, 0.42, 0.44, 0.46, 0.48, 0.50, 0.52, 0.54, 0.56, 0.60]
        sig = strat.evaluate(_snap(price=0.60), history)
        for key in ("composite_score", "momentum_score", "mr_score", "spread_score", "decay_score", "time_decay"):
            assert key in sig.features

    def test_composite_runs_in_backtester(self):
        """Composite strategy can be used with the backtesting engine."""
        from src.backtest.engine import Backtester
        cfg = _cfg()
        strat = CompositeStrategy(cfg)
        bt = Backtester(cfg, strat, assumed_spread=0.02)
        history = [0.40, 0.42, 0.44, 0.46, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
        report = bt.run({"tok": history})
        assert report.strategy == "composite"
        assert report.total_ticks > 0
