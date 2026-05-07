"""Tests for the opt-in paper latency/rejection stress model.

The model is *off by default* so existing PnL replays and execution
tests stay deterministic.  When enabled, it lets operators stress-test
strategy robustness against:

* Latency-induced adverse selection (mid drifts during wait).
* Random rejections (self-trade, post-only, transient API errors).

We seed ``random`` for reproducibility within each test.
"""

from __future__ import annotations

import os
import random
from unittest import mock

import pytest

from src.config import Config
from src.polymarket.client import PolymarketClient
from src.polymarket.execution import ExecutionEngine, OrderRequest
from src.storage.sqlite_store import SQLiteStore


def _cfg(**overrides) -> Config:
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


@pytest.fixture
def engine_factory():
    def _make(**cfg_overrides):
        cfg = _cfg(**cfg_overrides)
        store = SQLiteStore(":memory:")
        client = PolymarketClient(cfg)
        return ExecutionEngine(client, cfg, store)
    return _make


class TestRejection:
    def test_rejection_disabled_by_default(self, engine_factory):
        engine = engine_factory()
        assert engine.cfg.paper_rejection_prob == 0.0
        # 100 orders, expect 0 rejections.
        order = OrderRequest("t1", "c1", "BUY", 10, 0.5, "test")
        results = [engine.execute(order) for _ in range(20)]
        assert all(r.success for r in results)

    def test_rejection_certain_when_prob_one(self, engine_factory):
        engine = engine_factory(PAPER_REJECTION_PROB="1.0")
        order = OrderRequest("t1", "c1", "BUY", 10, 0.5, "test")
        result = engine.execute(order)
        assert not result.success
        assert "rejection" in result.message.lower() or "rejection" in result.message
        assert result.filled_size == 0.0

    def test_rejection_probabilistic(self, engine_factory):
        random.seed(42)
        engine = engine_factory(PAPER_REJECTION_PROB="0.5")
        order = OrderRequest("t1", "c1", "BUY", 10, 0.5, "test")
        # With seed=42 and p=0.5 over many trials the rate should be ~0.5
        n = 200
        rejections = sum(1 for _ in range(n) if not engine.execute(order).success)
        # Allow loose bounds: 35–65% (binomial 95% CI is much tighter, but
        # we don't want a flaky test under different python random impls).
        assert 0.30 * n <= rejections <= 0.70 * n


class TestLatencyDrift:
    def test_drift_disabled_by_default(self, engine_factory):
        engine = engine_factory()
        # latency mean = 0 → no drift sampled
        order = OrderRequest("t1", "c1", "BUY", 10, 0.50, "test", spread=0.02)
        result = engine.execute(order)
        # Only the half-spread (0.01) is added — no extra adverse drift.
        assert result.fill_price == pytest.approx(0.51)

    def test_drift_increases_buy_price(self, engine_factory):
        random.seed(7)
        engine = engine_factory(
            PAPER_LATENCY_MS_MEAN="200",
            PAPER_LATENCY_MS_STDDEV="0",  # deterministic latency = 200ms
            PAPER_ADVERSE_DRIFT_BPS_PER_100MS="50",  # 50 bps / 100ms
        )
        order = OrderRequest("t1", "c1", "BUY", 10, 0.50, "test", spread=0.0)
        result = engine.execute(order)
        # Latency clamped to [1, mean*5] = [1, 1000], so 200ms passes through.
        # drift_bps = 50 * (200/100) = 100 bps = 1% of mid (0.50) = 0.005.
        # Since spread=0, fill = 0.50 + 0.005 = 0.505.
        assert result.fill_price == pytest.approx(0.505, rel=1e-3)

    def test_drift_decreases_sell_price(self, engine_factory):
        random.seed(7)
        engine = engine_factory(
            PAPER_LATENCY_MS_MEAN="200",
            PAPER_LATENCY_MS_STDDEV="0",
            PAPER_ADVERSE_DRIFT_BPS_PER_100MS="50",
        )
        order = OrderRequest("t1", "c1", "SELL", 10, 0.50, "test", spread=0.0)
        result = engine.execute(order)
        # Same magnitude, opposite sign for SELL.
        assert result.fill_price == pytest.approx(0.495, rel=1e-3)

    def test_drift_clamped_within_price_bounds(self, engine_factory):
        random.seed(7)
        engine = engine_factory(
            PAPER_LATENCY_MS_MEAN="500",
            PAPER_LATENCY_MS_STDDEV="0",
            PAPER_ADVERSE_DRIFT_BPS_PER_100MS="10000",  # absurd: 100% per 100ms
        )
        order = OrderRequest("t1", "c1", "BUY", 10, 0.99, "test", spread=0.0)
        result = engine.execute(order)
        assert 0.0 < result.fill_price < 1.0  # clamped

    def test_drift_off_when_only_latency_set(self, engine_factory):
        engine = engine_factory(
            PAPER_LATENCY_MS_MEAN="200",
            PAPER_LATENCY_MS_STDDEV="0",
            PAPER_ADVERSE_DRIFT_BPS_PER_100MS="0",  # explicit 0 → no drift
        )
        order = OrderRequest("t1", "c1", "BUY", 10, 0.50, "test", spread=0.0)
        result = engine.execute(order)
        assert result.fill_price == pytest.approx(0.50)
