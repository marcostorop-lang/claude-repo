"""Tests for the edge detection model."""

from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.edge import EdgeEstimate, estimate_edge


def _future(hours: float) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.isoformat()


class TestEdgeEstimate:
    def test_has_edge_requires_minimum(self):
        e = EdgeEstimate(0.50, 0.53, 0.03, 0.5)
        assert not e.has_edge  # edge == 0.03, not > 0.03

        e2 = EdgeEstimate(0.50, 0.55, 0.05, 0.5)
        assert e2.has_edge  # edge > 0.03 and confidence > 0.3

    def test_direction_buy_sell_hold(self):
        assert EdgeEstimate(0.50, 0.60, 0.10, 0.5).direction == "BUY"
        assert EdgeEstimate(0.50, 0.40, -0.10, 0.5).direction == "SELL"
        assert EdgeEstimate(0.50, 0.51, 0.01, 0.5).direction == "HOLD"


class TestEstimateEdge:
    def test_insufficient_data_returns_zero_edge(self):
        e = estimate_edge(0.50, [0.50], spread=0.02)
        assert e.edge == 0.0
        assert e.edge_confidence == 0.0

    def test_rising_prices_estimate_higher(self):
        # Strong uptrend: momentum estimator should push estimated_p > market
        history = [0.40, 0.42, 0.44, 0.46, 0.48, 0.50]
        e = estimate_edge(0.50, history, spread=0.02)
        assert e.estimated_p > 0.50  # model thinks price is headed higher
        assert e.edge > 0

    def test_falling_prices_estimate_lower(self):
        history = [0.60, 0.58, 0.56, 0.54, 0.52, 0.50]
        e = estimate_edge(0.50, history, spread=0.02)
        # MR estimator pulls toward mean (0.55), momentum pulls down
        # Overall should be near or below 0.50
        assert e.estimated_p <= 0.52

    def test_stable_prices_near_zero_edge(self):
        history = [0.50] * 10
        e = estimate_edge(0.50, history, spread=0.02)
        # Stable market → estimated_p ≈ market price → small edge
        assert abs(e.edge) < 0.05

    def test_wide_spread_lowers_confidence(self):
        history = [0.40, 0.42, 0.44, 0.46, 0.48, 0.50]
        e_narrow = estimate_edge(0.50, history, spread=0.01)
        e_wide = estimate_edge(0.50, history, spread=0.14)
        assert e_narrow.edge_confidence > e_wide.edge_confidence

    def test_near_expiry_adds_convergence(self):
        history = [0.50, 0.52, 0.54, 0.56, 0.58, 0.60]
        e_far = estimate_edge(0.60, history, spread=0.02, end_date=_future(30 * 24))
        e_near = estimate_edge(0.60, history, spread=0.02, end_date=_future(6))
        # Near expiry with price at 0.60 → convergence pushes toward 1
        # But confidence drops due to time decay
        assert e_near.edge_confidence < e_far.edge_confidence

    def test_signals_dict_populated(self):
        history = [0.40, 0.42, 0.44, 0.46, 0.48, 0.50]
        e = estimate_edge(0.50, history, spread=0.02)
        for key in ("estimated_p", "edge", "edge_confidence", "n_estimators"):
            assert key in e.signals

    def test_estimated_p_bounded(self):
        # Extreme trend shouldn't produce estimated_p outside [0.01, 0.99]
        history = [0.01, 0.02, 0.05, 0.10, 0.20, 0.40, 0.80, 0.95]
        e = estimate_edge(0.95, history, spread=0.01)
        assert 0.01 <= e.estimated_p <= 0.99

    def test_edge_at_boundary_prices(self):
        # Price at 0.01 — should not crash, edge should be near zero
        e = estimate_edge(0.01, [0.02, 0.01, 0.01, 0.01], spread=0.01)
        assert abs(e.edge) < 0.05  # near-zero edge at boundary

        # Price at 0.99
        e2 = estimate_edge(0.99, [0.98, 0.99, 0.99, 0.99], spread=0.01)
        assert abs(e2.edge) < 0.05

        # Price at exactly 0 or 1 → immediate bail
        e3 = estimate_edge(0.0, [0.01, 0.0], spread=0.01)
        assert e3.edge == 0.0
        e4 = estimate_edge(1.0, [0.99, 1.0], spread=0.01)
        assert e4.edge == 0.0
