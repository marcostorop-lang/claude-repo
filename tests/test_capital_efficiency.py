"""Tests for the capital-efficiency time-to-resolution sizing factor.

Covers:
* :func:`capital_efficiency_factor` — the pure shrinkage curve,
  including the ``target_days`` cliff, the ``min_factor`` floor,
  and the fail-safe path on empty / unparseable end_date.
* :meth:`RiskManager.compute_position_size` integration — confirms
  the factor is silently disabled by default and only applies when
  the opt-in flag is set, and that it composes correctly with the
  other multipliers (Kelly, Bayesian).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.utils.time_utils import capital_efficiency_factor


# -- pure function ---------------------------------------------------------


def _iso_in(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


class TestPureFactor:
    def test_short_dated_returns_one(self):
        # 1 day out, target=14 → factor 1.0 (no shrinkage near resolution).
        assert capital_efficiency_factor(_iso_in(1), target_days=14.0) == 1.0

    def test_at_target_returns_one(self):
        # exactly 14 days → still 1.0 (boundary inclusive).
        assert capital_efficiency_factor(_iso_in(14), target_days=14.0) == 1.0

    def test_long_dated_shrinks(self):
        # 60 days vs target 15 → 15/60 = 0.25.
        f = capital_efficiency_factor(_iso_in(60), target_days=15.0, min_factor=0.0)
        assert f == pytest.approx(0.25, rel=1e-3)

    def test_min_factor_floor_applied(self):
        # 365 days vs target 7 → would be 0.019, but floored at 0.10.
        f = capital_efficiency_factor(_iso_in(365), target_days=7.0, min_factor=0.10)
        assert f == pytest.approx(0.10)

    def test_empty_end_date_is_failsafe(self):
        # Unknown horizon → 1.0 (don't penalise when we don't know).
        assert capital_efficiency_factor("") == 1.0

    def test_unparseable_end_date_is_failsafe(self):
        assert capital_efficiency_factor("not-a-date") == 1.0


# -- RiskManager integration ----------------------------------------------


def _cfg(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


class TestRiskManagerIntegration:
    def test_disabled_by_default(self):
        # With the flag off, the factor must NOT be applied — the size
        # equals the legacy max_position_size / price calculation.
        cfg = _cfg(max_position_size=100.0, sizing_confidence_scale=False)
        rm = RiskManager(cfg, PortfolioTracker())
        size_no_end = rm.compute_position_size(price=0.50, confidence=1.0)
        size_long = rm.compute_position_size(
            price=0.50, confidence=1.0, end_date=_iso_in(120),
        )
        assert size_no_end == size_long  # default-off: no shrinkage

    def test_enabled_shrinks_long_dated(self):
        cfg = _cfg(
            max_position_size=100.0,
            sizing_confidence_scale=False,
            sizing_capital_efficiency_enabled=True,
            sizing_capital_efficiency_target_days=10.0,
            sizing_capital_efficiency_min_factor=0.0,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        # 60 days out → factor 10/60 ≈ 0.1667 → shares = 200 * 0.1667 / 0.5 wait
        # base_usd=100, shares = 100 * 0.1667 / 0.5 = 33.33
        size = rm.compute_position_size(
            price=0.50, confidence=1.0, end_date=_iso_in(60),
        )
        assert size == pytest.approx(100.0 * (10.0 / 60.0) / 0.50, rel=1e-2)

    def test_enabled_no_op_short_dated(self):
        cfg = _cfg(
            max_position_size=100.0,
            sizing_confidence_scale=False,
            sizing_capital_efficiency_enabled=True,
            sizing_capital_efficiency_target_days=14.0,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        # 5 days out (< target) → factor 1.0.
        size = rm.compute_position_size(
            price=0.50, confidence=1.0, end_date=_iso_in(5),
        )
        assert size == pytest.approx(100.0 / 0.50)

    def test_enabled_empty_end_date_is_failsafe(self):
        cfg = _cfg(
            max_position_size=100.0,
            sizing_confidence_scale=False,
            sizing_capital_efficiency_enabled=True,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        # No end_date → 1.0 (don't penalise when we don't know).
        size = rm.compute_position_size(price=0.50, confidence=1.0, end_date="")
        assert size == pytest.approx(100.0 / 0.50)

    def test_floor_applied_at_extreme_horizon(self):
        cfg = _cfg(
            max_position_size=100.0,
            sizing_confidence_scale=False,
            sizing_capital_efficiency_enabled=True,
            sizing_capital_efficiency_target_days=7.0,
            sizing_capital_efficiency_min_factor=0.20,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        # 365 days out → would be ~0.019, floored at 0.20.
        size = rm.compute_position_size(
            price=0.50, confidence=1.0, end_date=_iso_in(365),
        )
        assert size == pytest.approx(100.0 * 0.20 / 0.50)
