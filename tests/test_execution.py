"""Tests for src.polymarket.execution — paper execution with slippage."""

import os
from unittest import mock

import pytest

from src.config import Config
from src.polymarket.execution import ExecutionEngine, OrderRequest, OrderResult
from src.storage.sqlite_store import SQLiteStore


def _cfg(**overrides):
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


@pytest.fixture
def engine(tmp_path):
    cfg = _cfg()
    store = SQLiteStore(str(tmp_path / "test.db"))
    # Mock client — not needed for paper execution
    client = mock.MagicMock()
    client.cfg = cfg
    eng = ExecutionEngine(client, cfg, store)
    yield eng, store
    store.close()


class TestPaperExecution:
    def test_paper_buy_records_trade(self, engine):
        eng, store = engine
        order = OrderRequest("t1", "c1", "BUY", 10.0, 0.50, "test")
        result = eng.execute(order)
        assert result.success
        assert result.mode == "paper"
        trades = store.get_trades(limit=1)
        assert len(trades) == 1
        assert trades[0]["side"] == "BUY"

    def test_paper_buy_applies_slippage(self, engine):
        eng, store = engine
        order = OrderRequest("t1", "c1", "BUY", 10.0, 0.50, "test", spread=0.04)
        result = eng.execute(order)
        assert result.success
        trades = store.get_trades(limit=1)
        # BUY at mid + half_spread = 0.50 + 0.02 = 0.52
        assert trades[0]["price"] == pytest.approx(0.52)
        assert trades[0]["spread_at_entry"] == pytest.approx(0.04)

    def test_paper_sell_applies_slippage(self, engine):
        eng, store = engine
        order = OrderRequest("t1", "c1", "SELL", 10.0, 0.50, "test", spread=0.04)
        result = eng.execute(order)
        assert result.success
        trades = store.get_trades(limit=1)
        # SELL at mid - half_spread = 0.50 - 0.02 = 0.48
        assert trades[0]["price"] == pytest.approx(0.48)

    def test_paper_no_spread_no_slippage(self, engine):
        eng, store = engine
        order = OrderRequest("t1", "c1", "BUY", 10.0, 0.50, "test", spread=0.0)
        result = eng.execute(order)
        trades = store.get_trades(limit=1)
        assert trades[0]["price"] == pytest.approx(0.50)

    def test_paper_exit_reason_stored(self, engine):
        eng, store = engine
        order = OrderRequest("t1", "c1", "SELL", 10.0, 0.50, "test", exit_reason="stop_loss")
        result = eng.execute(order)
        trades = store.get_trades(limit=1)
        assert trades[0]["exit_reason"] == "stop_loss"

    def test_paper_book_price_skips_extra_slippage(self, engine):
        """When the caller supplies an already-book-side price, paper execution
        must NOT add another half-spread on top (avoids double-counting)."""
        eng, store = engine
        # order.price is already best_ask = 0.52, spread is the real book spread.
        order = OrderRequest(
            "t1", "c1", "BUY", 10.0, 0.52, "test",
            spread=0.04, is_book_price=True,
        )
        result = eng.execute(order)
        trades = store.get_trades(limit=1)
        # Fill must be exactly the supplied price, not 0.52 + 0.02
        assert trades[0]["price"] == pytest.approx(0.52)

    def test_paper_book_price_sell_exact(self, engine):
        eng, store = engine
        order = OrderRequest(
            "t1", "c1", "SELL", 10.0, 0.48, "test",
            spread=0.04, is_book_price=True,
        )
        result = eng.execute(order)
        trades = store.get_trades(limit=1)
        assert trades[0]["price"] == pytest.approx(0.48)

    def test_live_blocked_in_paper_mode(self, engine):
        eng, store = engine
        # Force config to not-live
        eng.cfg = _cfg(TRADING_MODE="live", ALLOW_LIVE_TRADING="false")
        order = OrderRequest("t1", "c1", "BUY", 10.0, 0.50, "test")
        result = eng.execute(order)
        # Should fall through to paper execution since is_live is False
        assert result.success
        assert result.mode == "paper"
