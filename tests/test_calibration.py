"""Tests for the confidence calibration analysis."""

from src.analysis.calibration import calibration_report
from src.storage.sqlite_store import SQLiteStore


def _make_store() -> SQLiteStore:
    return SQLiteStore(":memory:")


class TestCalibrationReport:
    def test_empty_store_returns_empty_list(self):
        store = _make_store()
        assert calibration_report(store) == []
        store.close()

    def test_only_open_entries_are_ignored(self):
        store = _make_store()
        store.insert_calibration_entry(
            entry_timestamp="2026-01-01T00:00:00",
            token_id="t1", strategy="s", confidence=0.8, entry_price=0.5,
        )
        # No exit recorded — should not appear in report
        assert calibration_report(store) == []
        store.close()

    def test_closed_entries_appear_in_correct_bucket(self):
        store = _make_store()
        # One high-confidence winner
        store.insert_calibration_entry("t0", "tok1", "s", 0.9, 0.50, {})
        store.update_calibration_exit("tok1", "t1", 0.55, "take_profit", 5.0, 0.10)
        # One low-confidence loser
        store.insert_calibration_entry("t2", "tok2", "s", 0.2, 0.50, {})
        store.update_calibration_exit("tok2", "t3", 0.45, "stop_loss", -5.0, -0.10)

        report = calibration_report(store, n_bins=5)
        assert len(report) == 5  # 5 buckets
        total = sum(b["n"] for b in report)
        assert total == 2
        total_wins = sum(b["wins"] for b in report)
        assert total_wins == 1
        # High-confidence bucket (0.8-1.0) should have 1 win
        high = [b for b in report if b["bucket"] == "[0.80, 1.00)"][0]
        assert high["n"] == 1
        assert high["wins"] == 1
        # Low-confidence bucket (0.2-0.4) should have the loss
        low = [b for b in report if b["bucket"] == "[0.20, 0.40)"][0]
        assert low["n"] == 1
        assert low["wins"] == 0
        store.close()

    def test_win_rate_and_avg_return_are_computed(self):
        store = _make_store()
        # Three trades at confidence 0.8 — two wins, one loss
        for i, ret in enumerate([0.05, 0.10, -0.05]):
            tok = f"tok{i}"
            store.insert_calibration_entry(f"t{i}", tok, "s", 0.8, 0.5)
            store.update_calibration_exit(tok, f"t{i+10}", 0.5 * (1 + ret), "exit", ret * 50, ret)
        report = calibration_report(store, n_bins=5)
        high_bucket = [b for b in report if b["bucket"] == "[0.80, 1.00)"][0]
        assert high_bucket["n"] == 3
        assert high_bucket["wins"] == 2
        assert abs(high_bucket["win_rate"] - 2/3) < 1e-6
        assert abs(high_bucket["avg_return_pct"] - (0.05 + 0.10 - 0.05) / 3) < 1e-6
        store.close()
