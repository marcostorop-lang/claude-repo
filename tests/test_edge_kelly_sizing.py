"""Tests for edge-aware Kelly sizing and min-edge gate."""

import os
from unittest import mock

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.strategy.base import Action, Signal


def _cfg(**overrides):
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "MAX_POSITION_SIZE": "100",
        "MAX_TOTAL_EXPOSURE": "500",
        "MAX_OPEN_POSITIONS": "5",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


class TestEdgeKellySizing:
    def test_edge_kelly_scales_with_edge_magnitude(self):
        cfg = _cfg(SIZING_EDGE_KELLY="true", KELLY_FRACTION="0.25")
        rm = RiskManager(cfg, PortfolioTracker())
        price = 0.5

        # Small edge → small position
        small = rm.compute_position_size(price=price, confidence=0.8, edge=0.02)
        # Large edge → larger position
        large = rm.compute_position_size(price=price, confidence=0.8, edge=0.15)

        assert large > small

    def test_edge_kelly_scales_with_confidence(self):
        cfg = _cfg(SIZING_EDGE_KELLY="true", KELLY_FRACTION="0.25")
        rm = RiskManager(cfg, PortfolioTracker())

        low_conf = rm.compute_position_size(price=0.5, confidence=0.3, edge=0.08)
        high_conf = rm.compute_position_size(price=0.5, confidence=0.9, edge=0.08)

        assert high_conf > low_conf

    def test_edge_kelly_capped_at_max(self):
        """Kelly multiplier cannot exceed 1.0 — very high edge * confidence
        should never produce a position larger than max_position_size."""
        cfg = _cfg(SIZING_EDGE_KELLY="true", KELLY_FRACTION="1.0",
                   MAX_POSITION_SIZE="100")
        rm = RiskManager(cfg, PortfolioTracker())
        # Extreme edge and confidence — but capped at base_usd
        size = rm.compute_position_size(price=0.5, confidence=1.0, edge=0.99)
        # At kelly_fraction=1.0, mult = 0.99 * 1.0 * 1.0 = 0.99 (clamped at 1.0)
        # So position_size = 100 / 0.5 = 200 shares max
        assert size <= 200.0 + 1e-6

    def test_edge_kelly_tiny_edge_produces_tiny_position(self):
        """No artificial floor: tiny edges produce tiny positions, to be
        filtered by MIN_EDGE_FOR_TRADE if desired."""
        cfg = _cfg(SIZING_EDGE_KELLY="true", KELLY_FRACTION="0.25",
                   MAX_POSITION_SIZE="100")
        rm = RiskManager(cfg, PortfolioTracker())
        size = rm.compute_position_size(price=0.5, confidence=0.1, edge=0.01)
        # kelly_mult = 0.01 * 0.1 * 0.25 = 0.00025
        # base_usd = 100 * 0.00025 = 0.025 → 0.025 / 0.5 = 0.05 shares
        assert size == pytest.approx(0.05, rel=0.01)

    def test_edge_kelly_disabled_falls_back_to_confidence(self):
        """When SIZING_EDGE_KELLY is False, edge parameter is ignored."""
        cfg = _cfg(SIZING_EDGE_KELLY="false", SIZING_CONFIDENCE_SCALE="true",
                   MAX_POSITION_SIZE="100")
        rm = RiskManager(cfg, PortfolioTracker())
        with_edge = rm.compute_position_size(price=0.5, confidence=0.5, edge=0.10)
        without_edge = rm.compute_position_size(price=0.5, confidence=0.5)
        assert with_edge == pytest.approx(without_edge)

    def test_edge_kelly_no_edge_parameter(self):
        """Without edge supplied, edge-Kelly is skipped, falls back to confidence."""
        cfg = _cfg(SIZING_EDGE_KELLY="true", SIZING_CONFIDENCE_SCALE="true",
                   MAX_POSITION_SIZE="100", KELLY_FRACTION="0.25")
        rm = RiskManager(cfg, PortfolioTracker())
        size = rm.compute_position_size(price=0.5, confidence=0.5)
        # Falls back to confidence-only: 100 * 0.5 / 0.5 = 100 shares
        assert size == pytest.approx(100.0)

    def test_edge_kelly_respects_liquidity_cap(self):
        """Liquidity cap applies regardless of sizing mode."""
        cfg = _cfg(SIZING_EDGE_KELLY="true", KELLY_FRACTION="1.0",
                   MAX_POSITION_SIZE="1000", MAX_LIQUIDITY_FRACTION="0.01")
        rm = RiskManager(cfg, PortfolioTracker())
        # Liquidity = $500 → cap = $5 → 5/0.5 = 10 shares max
        size = rm.compute_position_size(
            price=0.5, confidence=1.0, edge=0.5, liquidity=500.0,
        )
        assert size <= 10.0 + 1e-6


class TestMinEdgeGate:
    def test_buy_rejected_below_min_edge(self):
        cfg = _cfg(MIN_EDGE_FOR_TRADE="0.05")
        rm = RiskManager(cfg, PortfolioTracker())
        sig = Signal(Action.BUY, 0.8, "test", features={"edge": 0.02})
        verdict = rm.check("tok1", sig, 10, 0.5, spread=0.02)
        assert not verdict.allowed
        assert "Edge" in verdict.reason and "below min" in verdict.reason

    def test_buy_allowed_above_min_edge(self):
        cfg = _cfg(MIN_EDGE_FOR_TRADE="0.02")
        rm = RiskManager(cfg, PortfolioTracker())
        sig = Signal(Action.BUY, 0.8, "test", features={"edge": 0.08})
        verdict = rm.check("tok1", sig, 10, 0.5, spread=0.02)
        assert verdict.allowed

    def test_no_edge_gate_when_config_zero(self):
        """MIN_EDGE_FOR_TRADE=0 (default) → no edge gate, old behaviour."""
        cfg = _cfg(MIN_EDGE_FOR_TRADE="0")
        rm = RiskManager(cfg, PortfolioTracker())
        sig = Signal(Action.BUY, 0.8, "test", features={"edge": 0.0001})
        verdict = rm.check("tok1", sig, 10, 0.5, spread=0.02)
        assert verdict.allowed

    def test_no_edge_in_features_skips_gate(self):
        """Strategies without edge features (e.g. momentum) are not blocked."""
        cfg = _cfg(MIN_EDGE_FOR_TRADE="0.05")
        rm = RiskManager(cfg, PortfolioTracker())
        sig = Signal(Action.BUY, 0.8, "test", features={"momentum_score": 0.6})
        verdict = rm.check("tok1", sig, 10, 0.5, spread=0.02)
        assert verdict.allowed

    def test_negative_edge_magnitude_considered(self):
        """|edge| is compared, so -0.08 passes a 0.05 threshold."""
        cfg = _cfg(MIN_EDGE_FOR_TRADE="0.05")
        rm = RiskManager(cfg, PortfolioTracker())
        sig = Signal(Action.SELL, 0.8, "test", features={"edge": -0.08})
        verdict = rm.check("tok1", sig, 10, 0.5, spread=0.02)
        assert verdict.allowed
