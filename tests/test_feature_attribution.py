"""Tests for feature attribution analysis.

Covers:
* Insufficient data returns empty report.
* Numeric features are extracted from win/loss splits.
* Cohen's d computation and direction.
* Win-rate above/below median split.
* Non-numeric and missing features are skipped.
* min_feature_count filtering.
* Overall winrate computation.
"""

from __future__ import annotations

import json

import pytest

from src.analysis.feature_attribution import (
    AttributionReport,
    FeatureAttribution,
    compute_attribution,
)


def _row(pnl: float, **features) -> dict:
    return {"pnl": pnl, "features": json.dumps(features)}


class TestComputeAttribution:
    def test_insufficient_data(self):
        rows = [_row(1.0, edge=0.05)] * 5
        report = compute_attribution(rows, min_samples=20)
        assert report.total_trades == 5
        assert report.features == []

    def test_basic_attribution(self):
        rows = []
        for i in range(30):
            if i < 20:
                rows.append(_row(1.0, edge=0.06 + i * 0.001))
            else:
                rows.append(_row(-1.0, edge=0.02 + (i - 20) * 0.001))
        report = compute_attribution(rows, min_samples=10, min_feature_count=5)
        assert report.total_trades == 30
        assert report.overall_winrate == pytest.approx(20 / 30)
        assert len(report.features) >= 1
        edge_attr = [f for f in report.features if f.feature == "edge"]
        assert len(edge_attr) == 1
        attr = edge_attr[0]
        assert attr.n_win == 20
        assert attr.n_loss == 10
        assert attr.win_mean > attr.loss_mean
        assert attr.cohens_d > 0

    def test_non_numeric_skipped(self):
        rows = [
            _row(1.0, label="good", edge=0.05),
            _row(-1.0, label="bad", edge=0.01),
        ] * 15
        report = compute_attribution(rows, min_samples=10, min_feature_count=5)
        feature_names = [f.feature for f in report.features]
        assert "label" not in feature_names
        assert "edge" in feature_names

    def test_bool_skipped(self):
        rows = [_row(1.0, flag=True, edge=0.05)] * 15 + \
               [_row(-1.0, flag=False, edge=0.01)] * 15
        report = compute_attribution(rows, min_samples=10, min_feature_count=5)
        feature_names = [f.feature for f in report.features]
        assert "flag" not in feature_names

    def test_min_feature_count_filtering(self):
        rows = [_row(1.0, edge=0.05)] * 15 + [_row(-1.0, edge=0.01)] * 15
        rows[0] = _row(1.0, edge=0.05, rare_feat=99.0)
        report = compute_attribution(rows, min_samples=10, min_feature_count=10)
        feature_names = [f.feature for f in report.features]
        assert "rare_feat" not in feature_names

    def test_winrate_above_below_median(self):
        rows = []
        for i in range(20):
            rows.append(_row(1.0, score=0.8 + i * 0.01))
        for i in range(20):
            rows.append(_row(-1.0, score=0.3 + i * 0.01))
        report = compute_attribution(rows, min_samples=10, min_feature_count=5)
        score_attr = [f for f in report.features if f.feature == "score"][0]
        assert score_attr.winrate_above_median > score_attr.winrate_below_median

    def test_sorted_by_abs_cohens_d(self):
        rows = []
        for i in range(25):
            rows.append(_row(1.0, strong=0.90, weak=0.51))
        for i in range(25):
            rows.append(_row(-1.0, strong=0.10, weak=0.49))
        report = compute_attribution(rows, min_samples=10, min_feature_count=5)
        assert len(report.features) == 2
        assert report.features[0].feature == "strong"

    def test_features_as_dict(self):
        rows = [{"pnl": 1.0, "features": {"edge": 0.05}}] * 15 + \
               [{"pnl": -1.0, "features": {"edge": 0.01}}] * 15
        report = compute_attribution(rows, min_samples=10, min_feature_count=5)
        assert len(report.features) >= 1

    def test_missing_pnl_skipped(self):
        rows = [_row(1.0, edge=0.05)] * 15 + [_row(-1.0, edge=0.01)] * 15
        rows.append({"features": json.dumps({"edge": 0.03})})
        report = compute_attribution(rows, min_samples=10, min_feature_count=5)
        assert report.total_trades == 30

    def test_zero_pnl_treated_as_loss(self):
        rows = [_row(1.0, edge=0.05)] * 15 + [_row(0.0, edge=0.01)] * 15
        report = compute_attribution(rows, min_samples=10, min_feature_count=5)
        assert report.overall_winrate == pytest.approx(0.5)

    def test_empty_input(self):
        report = compute_attribution([], min_samples=1)
        assert report.total_trades == 0
        assert report.features == []
