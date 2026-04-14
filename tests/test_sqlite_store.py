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


class TestSemanticSignalsSummary:
    """Read-only aggregate used by the dashboard / bot_state export."""

    def _fake_mispricing(
        self,
        token_id: str = "t1",
        side: str = "BUY",
        method: str = "structural_complement",
        score: float = 0.8,
        net_edge: float = 0.03,
        synth_conf: float = 0.9,
    ):
        from src.analysis.semantic_engine.types import (
            MarketRelation,
            RelationKind,
            SemanticMispricing,
            SyntheticPrice,
        )

        synth = SyntheticPrice(
            point=0.50, lower=0.49, upper=0.51,
            confidence=synth_conf, method=method,
            contributors=("sib1",), n_contributors=1,
        )
        rel = MarketRelation(
            target_token_id=token_id,
            sibling_token_id="sib1",
            kind=RelationKind.INVERSE_OUTCOME,
            confidence=0.99,
            reason="binary pair",
            evidence={},
        )
        return SemanticMispricing(
            token_id=token_id,
            condition_id="c1",
            question="Q?",
            category="politics",
            best_bid=0.45, best_ask=0.47, midpoint=0.46,
            spread=0.02, liquidity=1000.0,
            synthetic=synth,
            side=side,
            gross_edge=0.04, net_edge=net_edge, score=score,
            relations=(rel,),
            features={},
        )

    def test_summary_empty(self, store):
        """No rows — all counters zero, well-formed shape."""
        s = store.semantic_signals_summary()
        assert s["count"] == 0
        assert s["by_side"] == {"BUY": 0, "SELL": 0}
        assert s["by_method"] == {}
        assert s["by_mode"] == {}
        assert s["avg_score"] == 0.0
        assert s["avg_net_edge"] == 0.0
        assert s["last_timestamp"] is None

    def test_summary_aggregates_sides_methods_modes(self, store):
        store.insert_semantic_signals(
            "2026-04-10T10:00:00Z",
            [
                self._fake_mispricing(side="BUY", method="structural_complement",
                                       score=0.80, net_edge=0.02),
                self._fake_mispricing(side="SELL", method="weighted_avg_equivalent",
                                       score=0.60, net_edge=0.04),
            ],
            mode="shadow",
        )
        store.insert_semantic_signals(
            "2026-04-10T11:00:00Z",
            [self._fake_mispricing(side="BUY", method="structural_complement",
                                    score=0.90, net_edge=0.05, synth_conf=0.80)],
            mode="live",
        )
        s = store.semantic_signals_summary()
        assert s["count"] == 3
        assert s["by_side"] == {"BUY": 2, "SELL": 1}
        assert s["by_method"]["structural_complement"] == 2
        assert s["by_method"]["weighted_avg_equivalent"] == 1
        assert s["by_mode"]["shadow"] == 2
        assert s["by_mode"]["live"] == 1
        assert s["avg_score"] == pytest.approx((0.80 + 0.60 + 0.90) / 3, abs=1e-4)
        assert s["avg_net_edge"] == pytest.approx((0.02 + 0.04 + 0.05) / 3, abs=1e-6)
        # Last timestamp == most recent insert
        assert s["last_timestamp"] == "2026-04-10T11:00:00Z"

    def test_summary_respects_limit(self, store):
        for i in range(5):
            store.insert_semantic_signals(
                f"2026-04-10T{10+i:02d}:00:00Z",
                [self._fake_mispricing(side="BUY", score=0.5 + i * 0.1)],
                mode="shadow",
            )
        s = store.semantic_signals_summary(limit=3)
        assert s["count"] == 3
        # The three most recent scores are 0.7, 0.8, 0.9
        assert s["avg_score"] == pytest.approx((0.7 + 0.8 + 0.9) / 3, abs=1e-4)

    def test_summary_survives_missing_table(self, tmp_path):
        """Old DB without the table: summary must not raise."""
        import sqlite3
        db_path = str(tmp_path / "legacy.db")
        # Create a DB without semantic_signals then open through the store
        # with the table dropped to simulate legacy shape.
        con = sqlite3.connect(db_path)
        con.close()
        s = SQLiteStore(db_path)
        try:
            s._conn.execute("DROP TABLE semantic_signals")
            s._conn.commit()
            summary = s.semantic_signals_summary()
            assert summary["count"] == 0
            assert summary["by_side"] == {"BUY": 0, "SELL": 0}
        finally:
            s.close()
