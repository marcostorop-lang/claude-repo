"""Tests for src.storage.sqlite_store — including new decision_log and migrations."""

import os
import pytest

from src.storage.sqlite_store import SQLiteStore


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test.db")
    s = SQLiteStore(db_path)
    yield s
    s.close()


class TestSQLiteStore:
    def test_insert_and_get_trade(self, store):
        store.insert_trade(
            order_id="o1", token_id="t1", condition_id="c1",
            side="BUY", size=10.0, price=0.50,
            strategy="test", mode="paper", timestamp="2026-01-01T00:00:00Z",
        )
        trades = store.get_trades(limit=10)
        assert len(trades) == 1
        assert trades[0]["order_id"] == "o1"
        assert trades[0]["side"] == "BUY"

    def test_insert_trade_with_exit_reason(self, store):
        store.insert_trade(
            order_id="o1", token_id="t1", condition_id="c1",
            side="SELL", size=10.0, price=0.55,
            strategy="test", mode="paper", timestamp="2026-01-01T00:00:00Z",
            exit_reason="stop_loss", spread_at_entry=0.02,
        )
        trades = store.get_trades(limit=10)
        assert trades[0]["exit_reason"] == "stop_loss"
        assert trades[0]["spread_at_entry"] == pytest.approx(0.02)

    def test_get_all_trades_ordered(self, store):
        store.insert_trade("o1", "t1", "c1", "BUY", 10, 0.50, "test", "paper", "2026-01-01T00:00:00Z")
        store.insert_trade("o2", "t2", "c2", "BUY", 20, 0.60, "test", "paper", "2026-01-01T01:00:00Z")
        trades = store.get_all_trades()
        assert len(trades) == 2
        assert trades[0]["order_id"] == "o1"  # first by timestamp

    def test_insert_and_get_decision(self, store):
        store.insert_decision(
            timestamp="2026-01-01T00:00:00Z",
            token_id="t1", condition_id="c1",
            action="RISK_REJECTED", reason="Spread too high",
            strategy="test", confidence=0.8,
            price=0.50, spread=0.20,
            signal_detail="Momentum +5%", risk_detail="Spread 0.20 > max 0.15",
        )
        decisions = store.get_decisions(limit=10)
        assert len(decisions) == 1
        assert decisions[0]["action"] == "RISK_REJECTED"
        assert decisions[0]["spread"] == pytest.approx(0.20)

    def test_insert_price_and_history(self, store):
        store.insert_price("t1", 0.50, "2026-01-01T00:00:00Z")
        store.insert_price("t1", 0.51, "2026-01-01T01:00:00Z")
        history = store.get_price_history("t1")
        assert history == [0.50, 0.51]

    def test_upsert_market(self, store):
        store.upsert_market("c1", "Will X?", '{"data": true}', "2026-01-01T00:00:00Z")
        store.upsert_market("c1", "Will X happen?", '{"data": true}', "2026-01-02T00:00:00Z")
        markets = store.get_cached_markets()
        assert len(markets) == 1
        assert markets[0]["question"] == "Will X happen?"

    def test_migration_idempotent(self, store):
        # Running _migrate again should not fail
        store._migrate()
        store._migrate()
        trades = store.get_trades(limit=1)
        assert isinstance(trades, list)
