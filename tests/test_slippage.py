"""Tests for the realised-vs-predicted slippage tracker.

Covers:
* Row-level extraction from decision_log features.
* BUY vs SELL direction convention.
* Skipping of rows missing required fields.
* Aggregate statistics: mean / p50 / p90 / underestimate_rate.
* ``min_samples`` gating for per-strategy breakdowns.
* Edge cases: empty input, zero midpoint, malformed features.
"""

from __future__ import annotations

import json

import pytest

from src.analysis.slippage import (
    _percentile,
    _row_slippage,
    compute_slippage_report,
)


# -- Row extraction --------------------------------------------------------


def _row(action="ENTRY_BUY", strategy="s", **feat_overrides) -> dict:
    feats = {
        "slippage_pct": 0.002,      # predicted 20 bps
        "midpoint_at_entry": 0.50,
        "fill_price": 0.5015,       # realised 30 bps for BUY
    }
    feats.update(feat_overrides)
    return {
        "action": action, "strategy": strategy,
        "features": json.dumps(feats),
    }


class TestRowSlippage:
    def test_buy_realised_computed_correctly(self):
        r = _row(action="ENTRY_BUY")
        result = _row_slippage(r)
        assert result is not None
        predicted, realised = result
        assert predicted == pytest.approx(0.002)
        assert realised == pytest.approx(0.003)  # (0.5015-0.5)/0.5

    def test_sell_direction_flipped(self):
        r = _row(
            action="ENTRY_SELL",
            midpoint_at_entry=0.5, fill_price=0.495,  # we sold *below* mid
        )
        result = _row_slippage(r)
        assert result is not None
        _, realised = result
        assert realised == pytest.approx(0.01)  # (0.5-0.495)/0.5

    def test_non_entry_row_skipped(self):
        assert _row_slippage(_row(action="EXIT_STOP_LOSS")) is None
        assert _row_slippage(_row(action="RISK_REJECTED")) is None

    def test_missing_midpoint_skipped(self):
        r = _row()
        feats = json.loads(r["features"])
        feats.pop("midpoint_at_entry")
        r["features"] = json.dumps(feats)
        assert _row_slippage(r) is None

    def test_zero_midpoint_skipped(self):
        assert _row_slippage(_row(midpoint_at_entry=0.0)) is None

    def test_non_numeric_features_skipped(self):
        r = _row(slippage_pct="not-a-number")
        assert _row_slippage(r) is None

    def test_features_as_dict_not_str_also_works(self):
        r = _row()
        r["features"] = json.loads(r["features"])
        assert _row_slippage(r) is not None


# -- Aggregation -----------------------------------------------------------


class TestAggregate:
    def test_empty_input_returns_empty_report(self):
        rpt = compute_slippage_report([])
        assert rpt.overall is None
        assert rpt.per_strategy == []
        assert rpt.skipped == 0

    def test_all_skipped_returns_empty(self):
        rows = [{"action": "RISK_REJECTED", "strategy": "s", "features": "{}"}]
        rpt = compute_slippage_report(rows)
        assert rpt.overall is None
        assert rpt.skipped == 1

    def test_overall_aggregates_all_rows_even_below_min_samples(self):
        rows = [_row() for _ in range(3)]
        rpt = compute_slippage_report(rows, min_samples=10)
        assert rpt.overall is not None
        assert rpt.overall.n_samples == 3
        # Below min_samples → no per-strategy block.
        assert rpt.per_strategy == []

    def test_per_strategy_emitted_at_min_samples(self):
        rows = [_row(strategy="a") for _ in range(12)]
        rpt = compute_slippage_report(rows, min_samples=10)
        assert len(rpt.per_strategy) == 1
        assert rpt.per_strategy[0].strategy == "a"
        assert rpt.per_strategy[0].n_samples == 12

    def test_multiple_strategies_grouped(self):
        rows = (
            [_row(strategy="a") for _ in range(12)]
            + [_row(strategy="b") for _ in range(12)]
            + [_row(strategy="c") for _ in range(3)]  # below threshold
        )
        rpt = compute_slippage_report(rows, min_samples=10)
        strategies = {s.strategy for s in rpt.per_strategy}
        assert strategies == {"a", "b"}
        assert rpt.overall.n_samples == 27

    def test_drift_metric_matches_manual(self):
        # Predicted 20 bps, realised 30 bps → drift = +10 bps each row.
        rows = [_row() for _ in range(20)]
        rpt = compute_slippage_report(rows, min_samples=5)
        assert rpt.overall.drift_mean_bps == pytest.approx(10.0, abs=0.01)
        assert rpt.overall.predicted_mean_bps == pytest.approx(20.0, abs=0.01)
        assert rpt.overall.realised_mean_bps == pytest.approx(30.0, abs=0.01)

    def test_underestimate_rate(self):
        # Half the rows have realised > predicted; half equal-or-less.
        rows_over = [_row() for _ in range(10)]  # realised=30 > predicted=20
        rows_under = [
            _row(fill_price=0.5005)  # realised=10 bps < predicted=20 bps
            for _ in range(10)
        ]
        rpt = compute_slippage_report(rows_over + rows_under, min_samples=5)
        assert rpt.overall.underestimate_rate == pytest.approx(0.5)

    def test_skipped_count_reported(self):
        good = [_row() for _ in range(5)]
        bad = [{"action": "ENTRY_BUY", "strategy": "s", "features": "{}"}
               for _ in range(3)]
        rpt = compute_slippage_report(good + bad, min_samples=5)
        assert rpt.skipped == 3
        assert rpt.overall.n_samples == 5


# -- Percentile helper ----------------------------------------------------


class TestPercentile:
    def test_empty(self):
        assert _percentile([], 50.0) == 0.0

    def test_singleton(self):
        assert _percentile([7.0], 50.0) == 7.0

    def test_median_of_evens(self):
        assert _percentile([1.0, 2.0, 3.0, 4.0], 50.0) == pytest.approx(2.5)

    def test_p90(self):
        vals = [float(i) for i in range(1, 11)]  # 1..10
        # p90 of 1..10 (linear interp) = 9.1
        assert _percentile(vals, 90.0) == pytest.approx(9.1)
