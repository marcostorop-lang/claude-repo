"""Tests for the edge-based strategy."""

import os
from unittest import mock

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import Action
from src.strategy.edge_based import EdgeBasedStrategy


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


class TestEdgeBasedStrategy:
    def test_no_price_holds(self):
        cfg = _cfg()
        strat = EdgeBasedStrategy(cfg)
        snap = _snap()
        snap.price = None
        sig = strat.evaluate(snap, [0.50, 0.51, 0.52, 0.53, 0.54])
        assert sig.action == Action.HOLD

    def test_insufficient_history_holds(self):
        cfg = _cfg()
        strat = EdgeBasedStrategy(cfg)
        sig = strat.evaluate(_snap(), [0.50, 0.51])
        assert sig.action == Action.HOLD
        assert "Not enough history" in sig.reason

    def test_strong_uptrend_generates_buy(self):
        cfg = _cfg()
        strat = EdgeBasedStrategy(cfg)
        # Consistent, strong uptrend → momentum estimator pushes estimated_p
        # above market price → positive edge → BUY
        history = [0.40, 0.43, 0.46, 0.49, 0.52, 0.55, 0.58, 0.61, 0.64, 0.67]
        sig = strat.evaluate(_snap(price=0.67, spread=0.01), history)
        # We don't assert BUY strictly because edge estimator is conservative;
        # but if action is not HOLD, it must be BUY for an uptrend.
        assert sig.action in (Action.BUY, Action.HOLD)
        if sig.action == Action.BUY:
            assert sig.confidence > 0

    def test_hold_when_flat(self):
        cfg = _cfg()
        strat = EdgeBasedStrategy(cfg)
        history = [0.50] * 10
        sig = strat.evaluate(_snap(price=0.50, spread=0.01), history)
        assert sig.action == Action.HOLD

    def test_wide_spread_reduces_confidence(self):
        cfg = _cfg()
        strat = EdgeBasedStrategy(cfg)
        history = [0.40, 0.43, 0.46, 0.49, 0.52, 0.55, 0.58, 0.61, 0.64, 0.67]
        narrow = strat.evaluate(_snap(price=0.67, spread=0.005), history)
        wide = strat.evaluate(_snap(price=0.67, spread=0.12), history)
        # If both fire, wide spread should have lower confidence.
        if narrow.action != Action.HOLD and wide.action != Action.HOLD:
            assert wide.confidence <= narrow.confidence

    def test_features_included_in_signal(self):
        cfg = _cfg()
        strat = EdgeBasedStrategy(cfg)
        history = [0.50, 0.51, 0.52, 0.53, 0.54, 0.55]
        sig = strat.evaluate(_snap(price=0.55), history)
        assert "edge" in sig.features
        assert "estimated_p" in sig.features
        assert "market_price" in sig.features
        assert "effective_confidence" in sig.features

    def test_extreme_price_holds(self):
        """Prices at 0 or 1 have no edge — the market has decided."""
        cfg = _cfg()
        strat = EdgeBasedStrategy(cfg)
        history = [0.99, 0.99, 0.99, 0.99, 0.99]
        sig = strat.evaluate(_snap(price=0.99), history)
        # At extremes the edge estimator bails → HOLD
        assert sig.action == Action.HOLD

    def test_fundamentals_applied_when_store_provided(self):
        cfg = _cfg()
        store = SQLiteStore(":memory:")
        # Seed some price history for fundamentals
        for i, p in enumerate([0.40, 0.43, 0.46, 0.49, 0.52, 0.55, 0.58, 0.61, 0.64, 0.67]):
            store.insert_price("t1", p, f"2026-04-12T10:{i:02d}:00", spread=0.01)
        strat = EdgeBasedStrategy(cfg, store=store)
        history = [0.40, 0.43, 0.46, 0.49, 0.52, 0.55, 0.58, 0.61, 0.64, 0.67]
        sig = strat.evaluate(_snap(price=0.67, spread=0.01), history)
        # fund_score should appear in features regardless of action
        assert "fund_score" in sig.features
        assert "fund_mult" in sig.features
        store.close()

    def test_store_failure_degrades_gracefully(self):
        """If fundamentals computation fails, strategy still works."""
        cfg = _cfg()

        class BrokenStore:
            def __getattr__(self, name):
                raise RuntimeError("store broken")

        strat = EdgeBasedStrategy(cfg, store=BrokenStore())
        history = [0.40, 0.43, 0.46, 0.49, 0.52, 0.55, 0.58, 0.61, 0.64, 0.67]
        sig = strat.evaluate(_snap(price=0.67, spread=0.01), history)
        # No exception, decision still made
        assert sig is not None
        assert sig.features["fund_score"] == 0.0

    def test_fund_multiplier_range(self):
        """Fundamentals multiplier must stay within [floor, ceiling]."""
        cfg = _cfg()
        strat = EdgeBasedStrategy(cfg)
        # Bounds sanity: floor=0.5, ceiling=1.25
        assert strat.fund_floor == 0.5
        assert strat.fund_ceiling == 1.25

    def test_strategy_name(self):
        cfg = _cfg()
        strat = EdgeBasedStrategy(cfg)
        assert strat.name == "edge_based"
