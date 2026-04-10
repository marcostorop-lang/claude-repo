"""Tests for the backtesting engine."""

import os
from unittest import mock

import pytest

from src.backtest.engine import Backtester, BacktestReport
from src.config import Config
from src.strategy.simple_momentum import SimpleMomentum
from src.strategy.mean_reversion import MeanReversion


def _cfg(**overrides):
    env = {
        "MOMENTUM_WINDOW": "3",
        "MOMENTUM_THRESHOLD": "0.02",
        "STOP_LOSS_PCT": "0.10",
        "TAKE_PROFIT_PCT": "0.10",
        "MIN_PRICE": "0.05",
        "MAX_PRICE": "0.95",
        "MAX_POSITION_SIZE": "50",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


class TestBacktester:
    def test_empty_history_produces_empty_report(self):
        cfg = _cfg()
        bt = Backtester(cfg, SimpleMomentum(cfg))
        report = bt.run({})
        assert isinstance(report, BacktestReport)
        assert report.total_ticks == 0
        assert report.signals_generated == 0
        assert report.trades_opened == 0

    def test_rising_prices_generate_buy_and_take_profit(self):
        cfg = _cfg(MOMENTUM_WINDOW="3", MOMENTUM_THRESHOLD="0.02", TAKE_PROFIT_PCT="0.05")
        bt = Backtester(cfg, SimpleMomentum(cfg), assumed_spread=0.0)
        # Upward ramp: strategy should fire and TP should trigger
        history = [0.40, 0.42, 0.44, 0.46, 0.50, 0.55, 0.60]
        report = bt.run({"token_a": history})
        assert report.signals_generated >= 1
        assert report.trades_opened >= 1
        # With TP=5% and prices continuing up, we expect a take_profit exit
        tp_exits = [t for t in report.trades if t.exit_reason == "take_profit"]
        assert len(tp_exits) >= 1

    def test_falling_prices_generate_stop_loss(self):
        # High TP so only SL can trigger on this history
        cfg = _cfg(MOMENTUM_WINDOW="3", MOMENTUM_THRESHOLD="0.02", STOP_LOSS_PCT="0.05", TAKE_PROFIT_PCT="0.50")
        bt = Backtester(cfg, SimpleMomentum(cfg), assumed_spread=0.0)
        # Ramp up → crash
        history = [0.40, 0.42, 0.44, 0.46, 0.45, 0.40, 0.30]
        report = bt.run({"token_a": history})
        sl_exits = [t for t in report.trades if t.exit_reason == "stop_loss"]
        assert len(sl_exits) >= 1

    def test_price_boundary_filter_blocks_extreme_prices(self):
        cfg = _cfg(MIN_PRICE="0.20", MAX_PRICE="0.80", MOMENTUM_WINDOW="3", MOMENTUM_THRESHOLD="0.02")
        bt = Backtester(cfg, SimpleMomentum(cfg), assumed_spread=0.0)
        # Prices stay near 0.05 — strategy may fire but boundary filter rejects
        history = [0.03, 0.04, 0.05, 0.07, 0.09]
        report = bt.run({"token_a": history})
        # No trades should open because all prices are below min_price
        assert report.trades_opened == 0

    def test_report_metrics_are_consistent(self):
        cfg = _cfg(MOMENTUM_WINDOW="3", MOMENTUM_THRESHOLD="0.02")
        bt = Backtester(cfg, SimpleMomentum(cfg), assumed_spread=0.0)
        history = [0.40, 0.42, 0.44, 0.46, 0.50, 0.55, 0.60]
        report = bt.run({"token_a": history})
        # Basic consistency: wins + losses == trades_closed
        assert report.wins + report.losses == report.trades_closed
        # Win rate bounds
        assert 0.0 <= report.win_rate <= 1.0
        # Total return == sum of individual returns
        expected = sum(t.return_pct for t in report.trades if t.exit_price is not None)
        assert report.total_return_pct == pytest.approx(expected)

    def test_mean_reversion_strategy_runs(self):
        cfg = _cfg(MEAN_REVERSION_WINDOW="5", MEAN_REVERSION_ENTRY_Z="1.0")
        bt = Backtester(cfg, MeanReversion(cfg), assumed_spread=0.0)
        # Oscillating price
        history = [0.50, 0.52, 0.48, 0.51, 0.49, 0.30, 0.50]
        report = bt.run({"token_a": history})
        # Should not crash; may or may not trade
        assert report.strategy == "mean_reversion"
        assert report.total_ticks > 0

    def test_per_tick_spread_affects_slippage(self):
        """When per-tick (price, spread) tuples are provided, slippage varies."""
        cfg = _cfg(MOMENTUM_WINDOW="3", MOMENTUM_THRESHOLD="0.02", TAKE_PROFIT_PCT="0.10")
        # Wide spread history — 10% spread per tick
        wide = [(0.40, 0.10), (0.42, 0.10), (0.44, 0.10), (0.46, 0.10), (0.50, 0.10), (0.55, 0.10), (0.60, 0.10)]
        bt_wide = Backtester(cfg, SimpleMomentum(cfg))
        report_wide = bt_wide.run({"tok": wide})

        # Narrow spread history — 1% spread per tick
        narrow = [(0.40, 0.01), (0.42, 0.01), (0.44, 0.01), (0.46, 0.01), (0.50, 0.01), (0.55, 0.01), (0.60, 0.01)]
        bt_narrow = Backtester(cfg, SimpleMomentum(cfg))
        report_narrow = bt_narrow.run({"tok": narrow})

        # Both should trade; narrow spread should produce better PnL
        if report_wide.trades_closed > 0 and report_narrow.trades_closed > 0:
            assert report_narrow.total_pnl > report_wide.total_pnl

    def test_multiple_tokens_are_independent(self):
        cfg = _cfg()
        bt = Backtester(cfg, SimpleMomentum(cfg), assumed_spread=0.0)
        histories = {
            "token_a": [0.40, 0.42, 0.44, 0.46, 0.50],
            "token_b": [0.50, 0.50, 0.50, 0.50, 0.50],
        }
        report = bt.run(histories)
        assert report.total_tokens == 2
        # Token B is flat — should not generate signals
        token_b_trades = [t for t in report.trades if t.token_id == "token_b"]
        assert len(token_b_trades) == 0
