"""Tests for exact-formula Kelly sizing.

Covers:
* :func:`kelly_fraction` — the pure math, including edge cases like
  zero / negative edge, extreme prices, and the ``p >= 1`` guard.
* :meth:`RiskManager.compute_position_size` — the integration path
  that multiplies the Kelly fraction by ``confidence`` and
  ``kelly_fraction`` and compares it against the legacy proxy.
"""

from __future__ import annotations

import pytest

from src.analysis.kelly import kelly_fraction
from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager


def _cfg(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


# -- kelly_fraction pure math ----------------------------------------------


class TestKellyFraction:
    def test_zero_edge_returns_zero(self):
        assert kelly_fraction(price=0.5, edge=0.0) == 0.0

    def test_negative_edge_returns_zero(self):
        assert kelly_fraction(price=0.5, edge=-0.1) == 0.0

    def test_extreme_prices_return_zero(self):
        assert kelly_fraction(price=0.0, edge=0.1) == 0.0
        assert kelly_fraction(price=1.0, edge=0.1) == 0.0
        assert kelly_fraction(price=-0.01, edge=0.1) == 0.0
        assert kelly_fraction(price=1.5, edge=0.1) == 0.0

    def test_50c_edge_5pct(self):
        # price=0.5, p=0.55 → f* = (0.55 - 0.5)/(1 - 0.5) = 0.10
        assert kelly_fraction(0.50, 0.05) == pytest.approx(0.10, rel=1e-9)

    def test_30c_edge_10pct(self):
        # price=0.30, p=0.40 → f* = 0.10/0.70 ≈ 0.142857
        assert kelly_fraction(0.30, 0.10) == pytest.approx(0.10 / 0.70, rel=1e-9)

    def test_90c_edge_5pct(self):
        # price=0.90, p=0.95 → f* = 0.05/0.10 = 0.5
        assert kelly_fraction(0.90, 0.05) == pytest.approx(0.5, rel=1e-9)

    def test_clamped_to_one_when_p_reaches_one(self):
        """An edge that implies p>=1 is clamped to full-bankroll Kelly."""
        assert kelly_fraction(0.50, 0.50) == 1.0
        assert kelly_fraction(0.80, 0.25) == 1.0  # implied p=1.05, clamped

    def test_strictly_monotonic_in_edge(self):
        """More edge at the same price → larger f*."""
        assert kelly_fraction(0.50, 0.01) < kelly_fraction(0.50, 0.05) < kelly_fraction(0.50, 0.10)


# -- RiskManager integration -----------------------------------------------


class TestComputePositionSizeKellyProper:
    def test_disabled_behaves_like_base(self):
        """With proper-Kelly off, sizing equals max/price (no scaling)."""
        cfg = _cfg(sizing_kelly_proper=False, sizing_edge_kelly=False,
                   sizing_confidence_scale=False, max_position_size=100.0,
                   max_liquidity_fraction=0.0)
        rm = RiskManager(cfg, PortfolioTracker())
        size = rm.compute_position_size(price=0.5, confidence=0.8, edge=0.05)
        assert size == pytest.approx(100.0 / 0.5)

    def test_enabled_with_edge_scales_by_kelly_times_conf_times_frac(self):
        cfg = _cfg(sizing_kelly_proper=True, kelly_fraction=0.25,
                   max_position_size=100.0, max_liquidity_fraction=0.0)
        rm = RiskManager(cfg, PortfolioTracker())
        # price=0.5, edge=0.10 → f*=0.20; conf=0.80; frac=0.25
        # mult = 0.20 * 0.80 * 0.25 = 0.04 → base_usd = $4.00 → size = 8 shares
        size = rm.compute_position_size(price=0.5, confidence=0.8, edge=0.10)
        assert size == pytest.approx(8.0)

    def test_negative_edge_produces_zero_size(self):
        cfg = _cfg(sizing_kelly_proper=True, kelly_fraction=0.25,
                   max_position_size=100.0, max_liquidity_fraction=0.0)
        rm = RiskManager(cfg, PortfolioTracker())
        size = rm.compute_position_size(price=0.5, confidence=0.8, edge=-0.05)
        assert size == 0.0

    def test_no_edge_supplied_falls_through_to_legacy_path(self):
        """When proper-Kelly is on but no edge is provided, legacy path runs."""
        cfg = _cfg(sizing_kelly_proper=True, sizing_edge_kelly=False,
                   sizing_confidence_scale=True, max_position_size=100.0,
                   max_liquidity_fraction=0.0)
        rm = RiskManager(cfg, PortfolioTracker())
        # confidence-scale path: base_usd *= 0.8
        size = rm.compute_position_size(price=0.5, confidence=0.8)
        assert size == pytest.approx((100.0 * 0.8) / 0.5)

    def test_proper_wins_over_legacy_when_both_enabled(self):
        """Both flags true → proper-Kelly path takes precedence."""
        cfg = _cfg(sizing_kelly_proper=True, sizing_edge_kelly=True,
                   kelly_fraction=0.25, max_position_size=100.0,
                   max_liquidity_fraction=0.0)
        rm = RiskManager(cfg, PortfolioTracker())
        # Legacy proxy: |edge|*conf*frac = 0.10*0.8*0.25 = 0.02 → $2 → 4 shares.
        # Proper:       f*(0.5,0.10)=0.20 * 0.8 * 0.25 = 0.04 → $4 → 8 shares.
        size = rm.compute_position_size(price=0.5, confidence=0.8, edge=0.10)
        assert size == pytest.approx(8.0)

    def test_liquidity_cap_still_applies(self):
        cfg = _cfg(sizing_kelly_proper=True, kelly_fraction=1.0,
                   max_position_size=100.0, max_liquidity_fraction=0.01)
        rm = RiskManager(cfg, PortfolioTracker())
        # Without cap: f*(0.5,0.10)=0.20 * 1.0 * 1.0 = 0.20 → $20 → 40 shares
        # Liquidity cap: 1% of $1000 = $10 → $10/0.5 = 20 shares
        size = rm.compute_position_size(
            price=0.5, confidence=1.0, edge=0.10, liquidity=1000.0,
        )
        assert size == pytest.approx(20.0)


# -- Edge-uncertainty multiplier --------------------------------------------

class TestEdgeUncertaintyMultiplier:
    def test_zero_sigma_returns_one(self):
        from src.analysis.kelly import edge_uncertainty_multiplier
        assert edge_uncertainty_multiplier(0.05, 0.0) == 1.0

    def test_none_sigma_returns_one(self):
        from src.analysis.kelly import edge_uncertainty_multiplier
        assert edge_uncertainty_multiplier(0.05, None) == 1.0

    def test_signal_equals_noise_zero(self):
        from src.analysis.kelly import edge_uncertainty_multiplier
        # σ == |edge| → cv=1 → 1 - 1 = 0
        assert edge_uncertainty_multiplier(0.05, 0.05) == 0.0

    def test_pure_noise_clamped_zero(self):
        from src.analysis.kelly import edge_uncertainty_multiplier
        # σ > |edge| → cv > 1 → mult would go negative → clamp 0
        assert edge_uncertainty_multiplier(0.02, 0.10) == 0.0

    def test_quadratic_shrinkage(self):
        from src.analysis.kelly import edge_uncertainty_multiplier
        # σ = |edge|/2 → cv = 0.5 → mult = 1 - 0.25 = 0.75
        assert edge_uncertainty_multiplier(0.10, 0.05) == pytest.approx(0.75)


class TestRiskManagerWithUncertainty:
    def test_uncertainty_off_by_default(self):
        cfg = _cfg(
            max_position_size=100.0,
            kelly_fraction=1.0,
            sizing_kelly_proper=True,
            sizing_kelly_uncertainty=False,
            sizing_edge_kelly=False,
            sizing_confidence_scale=False,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        # With sigma_edge supplied but feature off → identical sizing.
        size_no_unc = rm.compute_position_size(
            price=0.5, confidence=1.0, edge=0.10, edge_stddev=0.05,
        )
        size_no_arg = rm.compute_position_size(
            price=0.5, confidence=1.0, edge=0.10,
        )
        assert size_no_unc == pytest.approx(size_no_arg)

    def test_uncertainty_shrinks_size_when_on(self):
        cfg = _cfg(
            max_position_size=100.0,
            kelly_fraction=1.0,
            sizing_kelly_proper=True,
            sizing_kelly_uncertainty=True,
            sizing_edge_kelly=False,
            sizing_confidence_scale=False,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        # |edge|=0.10, σ=0.05 → cv=0.5 → mult=0.75
        size_unc = rm.compute_position_size(
            price=0.5, confidence=1.0, edge=0.10, edge_stddev=0.05,
        )
        size_clean = rm.compute_position_size(
            price=0.5, confidence=1.0, edge=0.10, edge_stddev=0.0,
        )
        assert size_unc == pytest.approx(0.75 * size_clean, rel=1e-3)

    def test_pure_noise_zeroes_size(self):
        cfg = _cfg(
            max_position_size=100.0,
            kelly_fraction=1.0,
            sizing_kelly_proper=True,
            sizing_kelly_uncertainty=True,
            sizing_edge_kelly=False,
            sizing_confidence_scale=False,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        size = rm.compute_position_size(
            price=0.5, confidence=1.0, edge=0.05, edge_stddev=0.10,
        )
        assert size == 0.0
