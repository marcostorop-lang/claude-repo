"""Tests for src.analysis.performance_metrics.

Pure analytics — no trading logic touched.  We verify:
- Empty input returns an all-zero report (dashboards must not crash).
- Sharpe/Sortino react correctly to a known return series.
- Expectancy, profit factor, Calmar match hand-calculated values.
- build_trade_pnls_from_store pairs BUYs with SELLs FIFO.
"""

from __future__ import annotations

import math

import pytest

from src.analysis.performance_metrics import (
    compute_performance,
    build_trade_pnls_from_store,
    format_report_markdown,
)


class TestEmptyInput:
    def test_empty_report_is_all_zero(self):
        r = compute_performance([])
        assert r.n_trades == 0
        assert r.win_rate == 0.0
        assert r.sharpe_daily == 0.0
        assert r.sortino_daily == 0.0
        assert r.calmar == 0.0

    def test_empty_markdown_renders(self):
        out = format_report_markdown(compute_performance([]))
        assert "No closed trades" in out


class TestBasicMetrics:
    def test_expectancy_and_win_rate(self):
        trades = [
            {"pnl": 10.0, "exit_timestamp": "2026-01-01T00:00:00"},
            {"pnl": -5.0, "exit_timestamp": "2026-01-02T00:00:00"},
            {"pnl": 15.0, "exit_timestamp": "2026-01-03T00:00:00"},
        ]
        r = compute_performance(trades)
        assert r.n_trades == 3
        assert r.n_wins == 2
        assert r.n_losses == 1
        assert r.win_rate == pytest.approx(2 / 3)
        # expectancy = (10 - 5 + 15) / 3 = 20 / 3
        assert r.expectancy == pytest.approx(20 / 3)
        # profit factor = 25 / 5 = 5
        assert r.profit_factor == pytest.approx(5.0)
        assert r.total_pnl == pytest.approx(20.0)
        assert r.best_trade == 15.0
        assert r.worst_trade == -5.0

    def test_all_wins_profit_factor_infinite(self):
        trades = [
            {"pnl": 1.0, "exit_timestamp": "2026-01-01T00:00:00"},
            {"pnl": 2.0, "exit_timestamp": "2026-01-02T00:00:00"},
        ]
        r = compute_performance(trades)
        assert math.isinf(r.profit_factor)

    def test_all_losses_profit_factor_zero(self):
        trades = [
            {"pnl": -1.0, "exit_timestamp": "2026-01-01T00:00:00"},
            {"pnl": -2.0, "exit_timestamp": "2026-01-02T00:00:00"},
        ]
        r = compute_performance(trades)
        assert r.profit_factor == 0.0


class TestDrawdown:
    def test_max_drawdown(self):
        # Cumulative: 10, 15, 5, 12, -3  → peak=15, trough=-3 → dd=18
        trades = [
            {"pnl": 10.0, "exit_timestamp": "2026-01-01T00:00:00"},
            {"pnl": 5.0, "exit_timestamp": "2026-01-02T00:00:00"},
            {"pnl": -10.0, "exit_timestamp": "2026-01-03T00:00:00"},
            {"pnl": 7.0, "exit_timestamp": "2026-01-04T00:00:00"},
            {"pnl": -15.0, "exit_timestamp": "2026-01-05T00:00:00"},
        ]
        r = compute_performance(trades)
        assert r.max_drawdown == pytest.approx(18.0)

    def test_calmar_when_no_drawdown(self):
        trades = [
            {"pnl": 1.0, "exit_timestamp": "2026-01-01T00:00:00"},
            {"pnl": 2.0, "exit_timestamp": "2026-01-02T00:00:00"},
        ]
        r = compute_performance(trades)
        # No drawdown → calmar defined as 0 (avoid div-by-zero)
        assert r.calmar == 0.0


class TestSharpeSortino:
    def test_positive_daily_returns_positive_sharpe(self):
        # 10 days of steady +1.0 returns → very high Sharpe (zero vol denominator → 0)
        # Use slightly varying returns so stdev > 0
        trades = [
            {"pnl": 1.0 + (i % 3) * 0.1, "exit_timestamp": f"2026-01-{i+1:02d}T00:00:00"}
            for i in range(10)
        ]
        r = compute_performance(trades)
        assert r.sharpe_daily > 0
        # Sortino should be >= Sharpe when all returns are positive
        # (downside stdev is 0-ish, numerator positive → very large or 0 if dsd=0)
        assert r.sortino_daily >= 0

    def test_mixed_returns_sortino_higher_than_sharpe(self):
        """With some positive and some negative daily returns, Sortino
        penalises only the downside so tends to differ from Sharpe."""
        trades = [
            {"pnl": 5.0, "exit_timestamp": "2026-01-01T00:00:00"},
            {"pnl": -2.0, "exit_timestamp": "2026-01-02T00:00:00"},
            {"pnl": 3.0, "exit_timestamp": "2026-01-03T00:00:00"},
            {"pnl": -1.0, "exit_timestamp": "2026-01-04T00:00:00"},
            {"pnl": 4.0, "exit_timestamp": "2026-01-05T00:00:00"},
        ]
        r = compute_performance(trades)
        assert r.sharpe_daily > 0
        assert r.sortino_daily > 0
        # Because only losses affect Sortino denominator, it's typically larger
        assert r.sortino_daily >= r.sharpe_daily

    def test_single_day_no_sharpe(self):
        """With only one daily bucket, stdev is undefined → Sharpe = 0."""
        trades = [
            {"pnl": 5.0, "exit_timestamp": "2026-01-01T00:00:00"},
            {"pnl": 3.0, "exit_timestamp": "2026-01-01T12:00:00"},  # same day
        ]
        r = compute_performance(trades)
        assert r.sharpe_daily == 0.0


class TestBuildFromStore:
    def test_fifo_pairing(self):
        class FakeStore:
            def get_all_trades(self):
                return [
                    {"token_id": "t1", "side": "BUY", "size": 10, "price": 0.50,
                     "timestamp": "2026-01-01T10:00:00", "strategy": "s"},
                    {"token_id": "t1", "side": "BUY", "size": 10, "price": 0.55,
                     "timestamp": "2026-01-01T11:00:00", "strategy": "s"},
                    {"token_id": "t1", "side": "SELL", "size": 10, "price": 0.60,
                     "timestamp": "2026-01-02T10:00:00", "strategy": "s"},
                    {"token_id": "t1", "side": "SELL", "size": 10, "price": 0.52,
                     "timestamp": "2026-01-02T11:00:00", "strategy": "s"},
                ]
        closed = build_trade_pnls_from_store(FakeStore())
        assert len(closed) == 2
        # First pair: BUY 0.50, SELL 0.60 → pnl 1.0
        assert closed[0]["pnl"] == pytest.approx(1.0)
        # Second pair: BUY 0.55, SELL 0.52 → pnl -0.30
        assert closed[1]["pnl"] == pytest.approx(-0.30)

    def test_unmatched_buys_skipped(self):
        class FakeStore:
            def get_all_trades(self):
                return [
                    {"token_id": "t1", "side": "BUY", "size": 10, "price": 0.50,
                     "timestamp": "2026-01-01T10:00:00", "strategy": "s"},
                ]
        closed = build_trade_pnls_from_store(FakeStore())
        assert closed == []

    def test_empty_store(self):
        class FakeStore:
            def get_all_trades(self):
                return []
        assert build_trade_pnls_from_store(FakeStore()) == []


class TestBootstrapCI:
    def test_sharpe_ci_brackets_point_estimate(self):
        from src.analysis.performance_metrics import _sharpe_from_daily, sharpe_ci
        daily = [1.0, -0.5, 0.8, -0.3, 1.2, -0.4, 0.9] * 5
        point = _sharpe_from_daily(daily)
        lo, hi = sharpe_ci(daily, n_resamples=500, seed=42)
        assert lo <= point <= hi

    def test_sharpe_ci_too_few_samples_returns_nan(self):
        import math
        from src.analysis.performance_metrics import sharpe_ci
        lo, hi = sharpe_ci([1.0], n_resamples=100, seed=1)
        assert math.isnan(lo) and math.isnan(hi)

    def test_win_rate_ci_brackets_truth(self):
        from src.analysis.performance_metrics import win_rate_ci, _win_rate
        # 60 wins out of 100 → win rate 0.60
        pnls = [1.0] * 60 + [-1.0] * 40
        lo, hi = win_rate_ci(pnls, n_resamples=500, seed=42)
        assert lo <= _win_rate(pnls) <= hi
        # CI tightens with N — should not span the whole [0, 1].
        assert hi - lo < 0.30

    def test_win_rate_ci_seeded_reproducible(self):
        from src.analysis.performance_metrics import win_rate_ci
        pnls = [1.0, -1.0] * 30
        a = win_rate_ci(pnls, n_resamples=200, seed=7)
        b = win_rate_ci(pnls, n_resamples=200, seed=7)
        assert a == b

    def test_compute_ci_full_report(self):
        from src.analysis.performance_metrics import compute_ci
        trades = [
            {"pnl": 1.0, "exit_timestamp": "2026-01-01T12:00:00"},
            {"pnl": -0.5, "exit_timestamp": "2026-01-02T12:00:00"},
            {"pnl": 0.8, "exit_timestamp": "2026-01-03T12:00:00"},
        ] * 10
        ci = compute_ci(trades, n_resamples=200, seed=1)
        assert ci.n_resamples == 200
        assert ci.alpha == 0.05
        assert ci.win_rate_low <= ci.win_rate_high
        assert ci.sharpe_low <= ci.sharpe_high
