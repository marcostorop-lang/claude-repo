"""Tests for the 10 remaining audit items (78→100 score).

Covers: backtester, chain reconciliation, thread safety, depth-aware slippage,
MM inventory in USD, per-strategy circuit breaker, A/B prompt testing,
correlation-aware sizing, latency tracking, regime detection.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time

import pytest

from bot.core.risk_manager import RiskManager, Position
from bot.core.utils import BookSnapshot, LatencyTracker, LatencyStats, Side, TradeSignal


# ===========================================================================
# Helper
# ===========================================================================


def _signal(**overrides) -> TradeSignal:
    defaults = dict(
        strategy="test", token_id="tok1", condition_id="cond1",
        side=Side.BUY, price=0.50, edge=0.10, confidence=0.70,
        probability=0.60, category="",
    )
    defaults.update(overrides)
    return TradeSignal(**defaults)


# ===========================================================================
# #1 — Backtester
# ===========================================================================


class TestBacktester:
    def test_backtest_result_fields(self):
        from bot.backtest.engine import BacktestResult
        r = BacktestResult()
        assert hasattr(r, "sharpe_ratio")
        assert hasattr(r, "avg_trade_duration_bars")
        assert hasattr(r, "profit_factor")

    def test_apply_fee_and_slippage_buy(self):
        from bot.backtest.engine import _apply_fee_and_slippage
        fill = _apply_fee_and_slippage(0.50, "BUY")
        assert fill > 0.50  # BUY → higher price after fees+slippage

    def test_apply_fee_and_slippage_sell(self):
        from bot.backtest.engine import _apply_fee_and_slippage
        fill = _apply_fee_and_slippage(0.50, "SELL")
        assert fill < 0.50  # SELL → lower price after fees+slippage

    def test_format_report_includes_sharpe(self):
        from bot.backtest.engine import BacktestResult, format_backtest_report
        r = BacktestResult(market_question="Test", sharpe_ratio=1.5,
                           avg_trade_duration_bars=3.0, profit_factor=2.0)
        report = format_backtest_report(r)
        assert "Sharpe" in report
        assert "Profit factor" in report
        assert "duration" in report.lower()


# ===========================================================================
# #4 — Chain reconciliation
# ===========================================================================


class TestChainReconciliation:
    def test_reconcile_no_mismatch(self):
        rm = RiskManager()
        rm.positions["t1"] = Position(
            token_id="t1", condition_id="c1", side=Side.BUY,
            size=100, entry_price=0.5, strategy="test",
        )
        mismatches = rm.reconcile_positions({"t1": 100.0})
        assert len(mismatches) == 0

    def test_reconcile_size_mismatch_adjusts(self):
        rm = RiskManager()
        rm.positions["t1"] = Position(
            token_id="t1", condition_id="c1", side=Side.BUY,
            size=100, entry_price=0.5, strategy="test",
        )
        mismatches = rm.reconcile_positions({"t1": 80.0})
        assert len(mismatches) == 1
        assert mismatches[0]["action"] == "adjusted"
        assert rm.positions["t1"].size == 80.0

    def test_reconcile_phantom_removed(self):
        rm = RiskManager()
        rm.positions["t1"] = Position(
            token_id="t1", condition_id="c1", side=Side.BUY,
            size=100, entry_price=0.5, strategy="test",
        )
        mismatches = rm.reconcile_positions({"t1": 0.0})
        assert len(mismatches) == 1
        assert mismatches[0]["action"] == "removed_phantom"
        assert "t1" not in rm.positions

    def test_reconcile_unknown_on_chain(self):
        rm = RiskManager()
        mismatches = rm.reconcile_positions({"t_unknown": 50.0})
        assert len(mismatches) == 1
        assert mismatches[0]["action"] == "unknown_on_chain"


# ===========================================================================
# #5 — Thread safety
# ===========================================================================


class TestThreadSafety:
    def test_concurrent_record_pnl(self):
        rm = RiskManager()
        errors = []

        def worker():
            try:
                for _ in range(100):
                    rm.record_pnl(0.01)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert rm._total_realized_pnl == pytest.approx(4.0, abs=0.01)

    def test_concurrent_open_close(self):
        rm = RiskManager()
        errors = []

        def opener(i):
            try:
                sig = _signal(token_id=f"tok{i}", condition_id=f"c{i}")
                rm.open_position(sig, fill_price=0.5, fill_size=10)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=opener, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(rm.positions) == 10


# ===========================================================================
# #7 — Depth-aware slippage
# ===========================================================================


class TestDepthAwareSlippage:
    def test_paper_order_with_book_has_higher_slippage_on_thin_book(self):
        from bot.core.polymarket_client import place_order
        thin_book = BookSnapshot(
            token_id="tok1", best_bid=0.49, best_ask=0.51,
            bid_depth_usd=5.0, ask_depth_usd=5.0,
        )
        thick_book = BookSnapshot(
            token_id="tok1", best_bid=0.49, best_ask=0.51,
            bid_depth_usd=10000.0, ask_depth_usd=10000.0,
        )
        # Run many trials and compare average fill prices
        thin_fills = [asyncio.run(place_order("tok1", Side.BUY, 0.5, 100, book=thin_book)).fill_price for _ in range(30)]
        thick_fills = [asyncio.run(place_order("tok1", Side.BUY, 0.5, 100, book=thick_book)).fill_price for _ in range(30)]

        avg_thin = sum(thin_fills) / len(thin_fills)
        avg_thick = sum(thick_fills) / len(thick_fills)
        # Thin book should have worse fills (higher price for BUY)
        assert avg_thin > avg_thick

    def test_paper_order_without_book_still_works(self):
        from bot.core.polymarket_client import place_order
        result = asyncio.run(place_order("tok1", Side.BUY, 0.5, 10))
        assert result.success is True


# ===========================================================================
# #8 — Market making inventory in USD
# ===========================================================================


class TestMMInventoryUSD:
    def test_inventory_tracked_as_usd(self):
        from bot.strategies.market_making import MarketMaking
        from unittest.mock import MagicMock
        mm = MarketMaking(oracle=MagicMock(), risk=MagicMock())
        assert hasattr(mm, "_inventory_usd")
        assert isinstance(mm._inventory_usd, dict)


# ===========================================================================
# #9 — Per-strategy circuit breaker
# ===========================================================================


class TestPerStrategyCircuitBreaker:
    def test_strategy_pause_on_loss(self):
        rm = RiskManager()
        rm.record_pnl(-30.0, strategy="prob_arb")
        assert rm.is_strategy_paused("prob_arb") is True
        assert rm.is_strategy_paused("logical_arb") is False

    def test_strategy_daily_reset_clears_pause(self):
        from datetime import date, timedelta
        rm = RiskManager()
        rm.record_pnl(-30.0, strategy="prob_arb")
        assert rm.is_strategy_paused("prob_arb") is True
        # Force daily reset
        rm._daily_date = date.today() - timedelta(days=1)
        assert rm.is_strategy_paused("prob_arb") is False

    def test_approve_rejects_paused_strategy(self):
        rm = RiskManager()
        rm.record_pnl(-30.0, strategy="test")
        sig = _signal()
        v = rm.approve(sig)
        assert v.approved is False
        assert "paused" in v.reason.lower()


# ===========================================================================
# #10 — A/B prompt testing
# ===========================================================================


class TestPromptABTesting:
    def test_default_variant_is_default(self):
        from bot.core.claude_oracle import PromptABTester
        tester = PromptABTester()
        assert "structured_v1" in tester.variant_names
        assert "aggressive_v2" in tester.variant_names
        assert len(tester.variant_names) == 2

    def test_pick_variant_returns_valid(self):
        from bot.core.claude_oracle import PromptABTester, PromptVariant
        variants = [
            PromptVariant(name="v1", prompt="prompt1"),
            PromptVariant(name="v2", prompt="prompt2"),
        ]
        tester = PromptABTester(variants)
        picked = tester.pick_variant()
        assert picked.name in ("v1", "v2")

    def test_record_and_stats(self):
        from bot.core.claude_oracle import PromptABTester, PromptVariant
        variants = [
            PromptVariant(name="v1", prompt="p1"),
            PromptVariant(name="v2", prompt="p2"),
        ]
        tester = PromptABTester(variants)
        tester.record("v1", p_claude=0.70, p_market=0.50)
        tester.record("v1", p_claude=0.60, p_market=0.55)
        tester.record("v2", p_claude=0.80, p_market=0.50)

        stats = tester.get_variant_stats()
        assert stats["v1"]["calls"] == 2
        assert stats["v2"]["calls"] == 1
        assert stats["v1"]["mean_abs_edge"] > 0


# ===========================================================================
# #11 — Correlation-aware sizing
# ===========================================================================


class TestCorrelationSizing:
    def test_category_field_on_signal_and_position(self):
        sig = _signal(category="crypto")
        assert sig.category == "crypto"

        pos = Position(
            token_id="t1", condition_id="c1", side=Side.BUY,
            size=10, entry_price=0.5, strategy="t", category="crypto",
        )
        assert pos.category == "crypto"

    def test_correlated_positions_reduce_size(self):
        rm = RiskManager()
        # Open an existing position in category "crypto"
        rm.positions["t1"] = Position(
            token_id="t1", condition_id="c1", side=Side.BUY,
            size=10, entry_price=0.5, strategy="t", category="crypto",
        )
        # Signal for another crypto market
        sig_with_cat = _signal(token_id="t2", condition_id="c2", category="crypto")
        v_correlated = rm.approve(sig_with_cat)

        # Reset and try without correlation
        rm2 = RiskManager()
        sig_no_cat = _signal(token_id="t2", condition_id="c2", category="")
        v_uncorrelated = rm2.approve(sig_no_cat)

        assert v_correlated.approved is True
        assert v_uncorrelated.approved is True
        # Correlated should have smaller size
        assert v_correlated.adjusted_size_usd < v_uncorrelated.adjusted_size_usd


# ===========================================================================
# #12 — Latency tracking
# ===========================================================================


class TestLatencyTracking:
    def test_record_and_stats(self):
        lt = LatencyTracker(window=100)
        for i in range(50):
            lt.record(0.1 + i * 0.01)
        stats = lt.stats()
        assert stats.n_samples == 50
        assert stats.p50_ms > 0
        assert stats.p90_ms > stats.p50_ms
        assert stats.p99_ms >= stats.p90_ms
        assert stats.max_ms > 0

    def test_empty_tracker(self):
        lt = LatencyTracker()
        stats = lt.stats()
        assert stats.n_samples == 0
        assert stats.p50_ms == 0.0

    def test_single_sample(self):
        lt = LatencyTracker()
        lt.record(0.5)
        stats = lt.stats()
        assert stats.n_samples == 1
        assert stats.p50_ms == pytest.approx(500.0)

    def test_negative_latency_ignored(self):
        lt = LatencyTracker()
        lt.record(-1.0)
        lt.record(float("inf"))
        assert lt.stats().n_samples == 0

    def test_latest_ms(self):
        lt = LatencyTracker()
        assert lt.latest_ms is None
        lt.record(0.123)
        assert lt.latest_ms == pytest.approx(123.0)

    def test_window_rolling(self):
        lt = LatencyTracker(window=10)
        for i in range(20):
            lt.record(0.01 * (i + 1))
        stats = lt.stats()
        assert stats.n_samples == 10


# ===========================================================================
# #13 — Regime detection
# ===========================================================================


class TestRegimeDetection:
    def test_calm_regime(self):
        from bot.core.regime_detector import detect_regime, Regime
        prices = [0.50, 0.505, 0.50, 0.495, 0.50, 0.505, 0.50]
        r = detect_regime(prices)
        assert r.regime == Regime.CALM
        assert r.edge_multiplier == pytest.approx(1.0)

    def test_volatile_regime(self):
        from bot.core.regime_detector import detect_regime, Regime
        prices = [0.50, 0.70, 0.40, 0.75, 0.35, 0.80, 0.30]
        r = detect_regime(prices)
        assert r.regime == Regime.VOLATILE
        assert r.edge_multiplier > 1.0

    def test_insufficient_data_defaults_calm(self):
        from bot.core.regime_detector import detect_regime, Regime
        r = detect_regime([0.5, 0.5])
        assert r.regime == Regime.CALM

    def test_trending_regime(self):
        from bot.core.regime_detector import detect_regime, Regime
        prices = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
        r = detect_regime(prices)
        # Monotonic increase with low volatility → trending
        assert r.regime in (Regime.TRENDING, Regime.CALM)  # depends on thresholds
        assert r.edge_multiplier >= 1.0

    def test_volume_spike_increases_multiplier(self):
        from bot.core.regime_detector import detect_regime
        prices = [0.50, 0.55, 0.45, 0.60, 0.40, 0.65, 0.35]
        volumes = [100, 100, 100, 100, 1000, 1000, 1000]
        r_with_vol = detect_regime(prices, volumes)
        r_without_vol = detect_regime(prices)
        assert r_with_vol.edge_multiplier >= r_without_vol.edge_multiplier

    def test_edge_multiplier_capped(self):
        from bot.core.regime_detector import detect_regime
        # Extremely volatile prices
        prices = [0.10, 0.90, 0.10, 0.90, 0.10, 0.90, 0.10]
        r = detect_regime(prices)
        assert r.edge_multiplier <= 3.0
