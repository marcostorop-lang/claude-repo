"""Tests for src.analysis.brier."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.brier import (
    bootstrap_brier_ci,
    brier_stats,
    compute_brier,
    temporal_brier,
)


def _row(conf: float, win: bool, exit_iso: str = "") -> dict:
    return {
        "confidence": conf,
        "return_pct": 0.05 if win else -0.05,
        "exit_timestamp": exit_iso,
    }


class TestComputeBrier:
    def test_empty(self):
        assert math.isnan(compute_brier([]))

    def test_perfect_calibration(self):
        # p=1 → win, p=0 → loss → Brier = 0
        rows = [_row(1.0, True), _row(0.0, False)]
        assert compute_brier(rows) == pytest.approx(0.0)

    def test_worst_calibration(self):
        # p=1 → loss, p=0 → win → Brier = (1-0)^2 + (0-1)^2 = 2 → mean = 1
        rows = [_row(1.0, False), _row(0.0, True)]
        assert compute_brier(rows) == pytest.approx(1.0)

    def test_neutral_calibration(self):
        # p=0.5 → equal mix → Brier = mean((0.5-0)^2, (0.5-1)^2) = 0.25
        rows = [_row(0.5, True), _row(0.5, False)]
        assert compute_brier(rows) == pytest.approx(0.25)

    def test_missing_confidence_treated_as_neutral(self):
        rows = [{"confidence": None, "return_pct": 0.05}]
        assert compute_brier(rows) == pytest.approx(0.25)

    def test_confidence_clamped_to_unit(self):
        # Out-of-range confidence (>1) should be clamped, not crash.
        rows = [_row(1.5, True), _row(-0.2, False)]
        assert compute_brier(rows) == pytest.approx(0.0)


class TestBootstrap:
    def test_too_few_rows_returns_nan(self):
        lo, hi = bootstrap_brier_ci([_row(0.5, True)], n_resamples=100, seed=1)
        assert math.isnan(lo) and math.isnan(hi)

    def test_zero_resamples_returns_nan(self):
        rows = [_row(0.5, True), _row(0.5, False)]
        lo, hi = bootstrap_brier_ci(rows, n_resamples=0, seed=1)
        assert math.isnan(lo) and math.isnan(hi)

    def test_ci_brackets_point_estimate(self):
        # 50/50 wins at p=0.5 → point estimate 0.25; CI should bracket it.
        rows = [_row(0.5, True), _row(0.5, False)] * 50
        point = compute_brier(rows)
        lo, hi = bootstrap_brier_ci(rows, n_resamples=500, seed=42)
        assert lo <= point <= hi
        assert hi - lo < 0.05  # CI tightens with N

    def test_ci_seeded_reproducible(self):
        rows = [_row(0.6, True), _row(0.4, False)] * 30
        a = bootstrap_brier_ci(rows, n_resamples=200, seed=7)
        b = bootstrap_brier_ci(rows, n_resamples=200, seed=7)
        assert a == b


class TestStats:
    def test_empty_returns_zero_n(self):
        s = brier_stats([])
        assert s.n == 0
        assert math.isnan(s.brier)

    def test_summary_fields_filled(self):
        rows = [_row(0.7, True), _row(0.3, False)] * 20
        s = brier_stats(rows, n_resamples=200, seed=1)
        assert s.n == 40
        assert 0 < s.brier < 0.25
        assert s.ci_low <= s.brier <= s.ci_high


class TestTemporalDrift:
    def _build(self, recent_brier_target: float, hist_brier_target: float, n: int = 40):
        """Build rows where recent (last 3 days) has worse calibration."""
        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
        rows = []
        # Historical: well calibrated (p=0.7 wins ~70%) — add older timestamps
        for i in range(n):
            win = i % 10 < 7  # 70% win rate
            ts = (now - timedelta(days=30 - i % 20)).isoformat()
            rows.append(_row(0.7, win, ts))
        # Recent (within 3 days): poorly calibrated (p=0.7 wins only 30%)
        for i in range(n // 2):
            win = i % 10 < 3
            ts = (now - timedelta(hours=2 + i)).isoformat()
            rows.append(_row(0.7, win, ts))
        return rows, now

    def test_recent_window_isolates_recent_rows(self):
        rows, now = self._build(0.4, 0.15)
        t = temporal_brier(rows, recent_days=3, now=now, n_resamples=200, seed=42)
        assert t.recent.n > 0
        assert t.historical.n > 0
        # recent and historical are mutually exclusive (see implementation).
        assert t.recent.n + t.historical.n == len(rows)

    def test_drift_detected_when_recent_worse(self):
        rows, now = self._build(0.4, 0.15)
        t = temporal_brier(rows, recent_days=3, now=now, n_resamples=500, seed=42)
        assert t.delta > 0  # recent worse than historical
        # With this much divergence and this many bootstrap samples, the
        # recent CI should sit above historical CI.
        assert t.drift_significant

    def test_drift_not_flagged_when_consistent(self):
        # Stable calibration over time → no drift flag
        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
        rows = []
        for i in range(60):
            win = i % 10 < 6  # uniform 60% win rate across all timestamps
            ts = (now - timedelta(days=30 - i % 30)).isoformat()
            rows.append(_row(0.6, win, ts))
        t = temporal_brier(rows, recent_days=7, now=now, n_resamples=500, seed=1)
        assert not t.drift_significant
