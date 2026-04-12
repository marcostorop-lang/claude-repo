"""Tests for the market resolution tracker."""

from src.storage.sqlite_store import SQLiteStore
from src.utils.time_utils import iso_now


class TestResolutionStorage:
    def test_insert_and_query_resolution(self):
        store = SQLiteStore(":memory:")
        store.insert_resolution(
            condition_id="cid1",
            token_id="tok1",
            question="Will X happen?",
            outcome="YES",
            resolved_price=1.0,
            resolution_ts="2026-01-15T00:00:00",
            our_side="BUY",
            our_entry_price=0.55,
            our_exit_price=0.70,
            our_pnl=1.50,
            prediction_correct=True,
            checked_at=iso_now(),
        )
        rows = store.get_resolutions()
        assert len(rows) == 1
        assert rows[0]["condition_id"] == "cid1"
        assert rows[0]["prediction_correct"] == 1
        store.close()

    def test_resolution_stats(self):
        store = SQLiteStore(":memory:")
        now = iso_now()
        # Correct prediction
        store.insert_resolution("c1", "t1", "Q1", "YES", 1.0, now,
                                "BUY", 0.55, 0.70, 1.50, True, now)
        # Correct prediction
        store.insert_resolution("c2", "t2", "Q2", "YES", 1.0, now,
                                "BUY", 0.60, 0.80, 2.00, True, now)
        # Wrong prediction
        store.insert_resolution("c3", "t3", "Q3", "NO", 0.0, now,
                                "BUY", 0.55, 0.30, -2.50, False, now)
        stats = store.get_resolution_stats()
        assert stats["total_resolved"] == 3
        assert stats["correct_predictions"] == 2
        assert stats["accuracy"] == 2 / 3
        assert stats["total_pnl"] == 1.50 + 2.00 - 2.50
        store.close()

    def test_get_traded_condition_ids(self):
        store = SQLiteStore(":memory:")
        store.insert_trade("o1", "t1", "cid_a", "BUY", 10, 0.50, "test", "paper", "2026-01-01T00:00:00")
        store.insert_trade("o2", "t2", "cid_b", "BUY", 10, 0.60, "test", "paper", "2026-01-01T00:00:00")
        store.insert_trade("o3", "t3", "cid_a", "SELL", 10, 0.55, "test", "paper", "2026-01-01T01:00:00")
        cids = store.get_traded_condition_ids()
        assert cids == {"cid_a", "cid_b"}
        store.close()

    def test_empty_stats(self):
        store = SQLiteStore(":memory:")
        stats = store.get_resolution_stats()
        assert stats["total_resolved"] == 0
        assert stats["accuracy"] == 0.0
        store.close()
