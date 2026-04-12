"""Tests for the edge-model calibration analyzer."""

import json

from src.analysis.edge_calibration import (
    analyze_edge_calibration,
    format_report_markdown,
)
from src.storage.sqlite_store import SQLiteStore
from src.utils.time_utils import iso_now


def _seed_entry_and_resolution(
    store: SQLiteStore,
    token_id: str,
    confidence: float,
    edge: float,
    correct: bool,
    pnl: float,
):
    """Helper to record an entry decision + a resolution for the same token."""
    now = iso_now()
    features = {"edge": edge, "estimated_p": 0.6, "edge_confidence": confidence}
    store.insert_decision(
        timestamp=now,
        token_id=token_id,
        condition_id=f"c_{token_id}",
        action="ENTRY_BUY",
        reason="test",
        strategy="edge_based",
        confidence=confidence,
        price=0.55,
        spread=0.02,
        features=features,
    )
    store.insert_resolution(
        condition_id=f"c_{token_id}",
        token_id=token_id,
        question="Q?",
        outcome="YES" if correct else "NO",
        resolved_price=1.0 if correct else 0.0,
        resolution_ts=now,
        our_side="BUY",
        our_entry_price=0.55,
        our_exit_price=1.0 if correct else 0.0,
        our_pnl=pnl,
        prediction_correct=correct,
        checked_at=now,
    )


class TestEdgeCalibration:
    def test_empty_store_returns_empty_report(self):
        store = SQLiteStore(":memory:")
        report = analyze_edge_calibration(store)
        assert report.total_predictions == 0
        assert report.overall_accuracy == 0.0
        assert "No resolved markets yet" in format_report_markdown(report)
        store.close()

    def test_single_correct_prediction(self):
        store = SQLiteStore(":memory:")
        _seed_entry_and_resolution(store, "t1", confidence=0.7, edge=0.05,
                                    correct=True, pnl=2.0)
        report = analyze_edge_calibration(store)
        assert report.total_predictions == 1
        assert report.total_correct == 1
        assert report.overall_accuracy == 1.0
        store.close()

    def test_confidence_buckets_populated(self):
        store = SQLiteStore(":memory:")
        # 3 low-confidence wrong, 3 high-confidence correct
        for i in range(3):
            _seed_entry_and_resolution(store, f"low_{i}", confidence=0.25,
                                        edge=0.04, correct=False, pnl=-1.0)
        for i in range(3):
            _seed_entry_and_resolution(store, f"high_{i}", confidence=0.85,
                                        edge=0.08, correct=True, pnl=3.0)
        report = analyze_edge_calibration(store)
        assert report.total_predictions == 6
        # Bucket 0.2-0.4 should have 3 wrong
        low_bucket = next(b for b in report.buckets if b.low == 0.2)
        assert low_bucket.count == 3
        assert low_bucket.correct == 0
        # Bucket 0.8-1.0 should have 3 correct
        high_bucket = next(b for b in report.buckets if b.low == 0.8)
        assert high_bucket.count == 3
        assert high_bucket.correct == 3
        # Positive correlation: confidence should correlate with correctness
        assert report.correlation_conf_accuracy > 0.5
        store.close()

    def test_edge_magnitude_buckets(self):
        store = SQLiteStore(":memory:")
        _seed_entry_and_resolution(store, "small", confidence=0.6,
                                    edge=0.01, correct=False, pnl=-0.5)
        _seed_entry_and_resolution(store, "medium", confidence=0.6,
                                    edge=0.04, correct=True, pnl=1.0)
        _seed_entry_and_resolution(store, "large", confidence=0.6,
                                    edge=0.12, correct=True, pnl=4.0)
        report = analyze_edge_calibration(store)
        # 3 different edge buckets should each have 1 entry
        counts = [b.count for b in report.edge_magnitude_buckets if b.count > 0]
        assert sum(counts) == 3

    def test_no_entry_decision_skips_resolution(self):
        """If we have a resolution but no matching decision_log entry, skip it."""
        store = SQLiteStore(":memory:")
        now = iso_now()
        store.insert_resolution(
            condition_id="orphan", token_id="orphan_tok", question="Q",
            outcome="YES", resolved_price=1.0, resolution_ts=now,
            our_side="BUY", our_entry_price=0.5, our_exit_price=1.0,
            our_pnl=1.0, prediction_correct=True, checked_at=now,
        )
        report = analyze_edge_calibration(store)
        assert report.total_predictions == 0
        store.close()

    def test_markdown_output_contains_sections(self):
        store = SQLiteStore(":memory:")
        _seed_entry_and_resolution(store, "t1", confidence=0.7, edge=0.05,
                                    correct=True, pnl=2.0)
        report = analyze_edge_calibration(store)
        md = format_report_markdown(report)
        assert "Edge Calibration Report" in md
        assert "Confidence" in md
        assert "Correlations" in md
        store.close()

    def test_handles_missing_features(self):
        """Decisions with no features JSON should not crash the analyzer."""
        store = SQLiteStore(":memory:")
        now = iso_now()
        store._conn.execute(
            "INSERT INTO decision_log (timestamp, token_id, condition_id, action, "
            "reason, strategy, confidence, price, spread, signal_detail, risk_detail, features) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, "t_no_feat", "c_no_feat", "ENTRY_BUY", "test", "simple",
             0.5, 0.5, 0.01, "", "", None),
        )
        store._conn.commit()
        store.insert_resolution(
            condition_id="c_no_feat", token_id="t_no_feat", question="Q",
            outcome="YES", resolved_price=1.0, resolution_ts=now,
            our_side="BUY", our_entry_price=0.5, our_exit_price=1.0,
            our_pnl=1.0, prediction_correct=True, checked_at=now,
        )
        report = analyze_edge_calibration(store)
        assert report.total_predictions == 1
        assert report.total_correct == 1
        store.close()
