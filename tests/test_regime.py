"""Tests for the correlated-event / regime-shift detector."""

from __future__ import annotations

import math

import pytest

from src.risk.regime import (
    RegimeVerdict,
    detect_regime_shift,
    returns_from_price_history,
)


# -- detect_regime_shift ---------------------------------------------------


class TestDetector:
    def test_empty_universe_returns_no_shift(self):
        v = detect_regime_shift({})
        assert v.shift_detected is False
        assert v.universe_size == 0
        assert "empty" in v.reason

    def test_small_universe_suppressed(self):
        # 10 tokens all moving big, but universe < min_universe (default 20).
        returns = {f"t{i}": 0.10 for i in range(10)}
        v = detect_regime_shift(returns, min_universe=20)
        assert v.shift_detected is False
        assert "too small" in v.reason
        # Diagnostics still populated.
        assert v.big_move_count == 10
        assert v.mean_abs_return == pytest.approx(0.10)

    def test_calm_universe_below_fraction(self):
        # 20 tokens, only 2 moving big (10% < 25% threshold).
        returns = {f"t{i}": 0.0 for i in range(20)}
        returns["big1"] = 0.08
        returns["big2"] = -0.08
        v = detect_regime_shift(returns)
        assert v.shift_detected is False
        assert v.big_move_count == 2
        assert "calm" in v.reason

    def test_shift_triggers_above_fraction(self):
        # 20 tokens, all moving >5% → 100% > 25% threshold.
        returns = {f"t{i}": 0.10 for i in range(20)}
        v = detect_regime_shift(returns)
        assert v.shift_detected is True
        assert v.big_move_fraction == 1.0
        assert "regime shift" in v.reason
        assert "up" in v.reason  # all positive → up bias

    def test_down_bias_reported(self):
        returns = {f"t{i}": -0.10 for i in range(20)}
        v = detect_regime_shift(returns)
        assert v.shift_detected is True
        assert v.directional_bias < 0
        assert "down" in v.reason

    def test_mixed_bias_reported(self):
        half_up = {f"u{i}": 0.10 for i in range(10)}
        half_down = {f"d{i}": -0.10 for i in range(10)}
        v = detect_regime_shift({**half_up, **half_down})
        assert v.shift_detected is True
        assert "mixed" in v.reason
        assert abs(v.directional_bias) < 0.01

    def test_thresholds_are_configurable(self):
        # 20 tokens, half at +3% → below default 5% threshold but
        # above a custom 2% threshold.
        returns = {f"t{i}": 0.03 for i in range(20)}
        default = detect_regime_shift(returns)
        loosened = detect_regime_shift(
            returns, move_threshold=0.02, fraction_threshold=0.25,
        )
        assert default.shift_detected is False
        assert loosened.shift_detected is True

    def test_nan_inputs_skipped(self):
        returns = {f"t{i}": 0.10 for i in range(20)}
        returns["bad"] = float("nan")
        returns["bad2"] = float("inf")
        returns["bad3"] = "not-a-number"  # type: ignore[assignment]
        v = detect_regime_shift(returns)
        # 20 good + 3 bad → 20 good in universe.
        assert v.universe_size == 20
        assert v.shift_detected is True


# -- returns_from_price_history -------------------------------------------


class TestReturnsHelper:
    def test_basic_return_computation(self):
        hist = {"t1": [1.0, 1.01, 1.02, 1.03, 1.04, 1.05]}
        out = returns_from_price_history(hist, lookback_points=5)
        # (1.05 - 1.0) / 1.0 = 0.05
        assert out["t1"] == pytest.approx(0.05)

    def test_insufficient_history_skipped(self):
        hist = {"t1": [1.0, 1.01, 1.02]}
        out = returns_from_price_history(hist, lookback_points=5)
        assert out == {}

    def test_zero_base_price_skipped(self):
        hist = {"t1": [0.0, 0.0, 0.0, 0.0, 0.0, 0.5]}
        out = returns_from_price_history(hist, lookback_points=5)
        assert out == {}

    def test_negative_base_skipped(self):
        hist = {"t1": [-0.1, -0.1, -0.1, -0.1, -0.1, 0.5]}
        out = returns_from_price_history(hist, lookback_points=5)
        assert out == {}

    def test_non_numeric_skipped(self):
        hist = {"t1": [1.0, 1.0, 1.0, 1.0, 1.0, "abc"]}  # type: ignore[list-item]
        out = returns_from_price_history(hist, lookback_points=5)
        assert out == {}


# -- Round-trip ----------------------------------------------------------


class TestEndToEnd:
    def test_coordinated_down_move_flagged(self):
        # Build 25 tokens, all dropping from 0.50 to 0.40 (= -20%) over 6 ticks.
        hist = {
            f"t{i}": [0.50, 0.50, 0.48, 0.45, 0.42, 0.40]
            for i in range(25)
        }
        rets = returns_from_price_history(hist, lookback_points=5)
        v = detect_regime_shift(rets, move_threshold=0.05,
                                fraction_threshold=0.25, min_universe=20)
        assert v.shift_detected is True
        assert v.directional_bias < 0
        assert v.universe_size == 25
