"""Tests for per-category edge threshold overrides.

This feature is strictly additive: when MIN_EDGE_BY_CATEGORY is empty
(the default), behaviour is identical to the existing global gate.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.strategy.base import Action, Signal


def _cfg(**overrides) -> Config:
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


class TestEffectiveMinEdge:
    def test_default_empty_dict(self):
        cfg = _cfg()
        assert cfg.min_edge_by_category == {}
        # Always falls back to global
        assert cfg.effective_min_edge("politics") == cfg.min_edge_for_trade

    def test_per_category_override_resolves(self):
        cfg = _cfg(
            MIN_EDGE_FOR_TRADE="0.02",
            MIN_EDGE_BY_CATEGORY='{"politics": 0.05, "sports": 0.08}',
        )
        assert cfg.effective_min_edge("politics") == pytest.approx(0.05)
        assert cfg.effective_min_edge("sports") == pytest.approx(0.08)
        # Unknown category falls back to global
        assert cfg.effective_min_edge("crypto") == pytest.approx(0.02)
        # Empty category falls back to global
        assert cfg.effective_min_edge("") == pytest.approx(0.02)

    def test_malformed_json_silently_ignored(self):
        """Bad JSON never crashes config loading."""
        cfg = _cfg(
            MIN_EDGE_FOR_TRADE="0.03",
            MIN_EDGE_BY_CATEGORY='{bad json',
        )
        assert cfg.min_edge_by_category == {}
        assert cfg.effective_min_edge("politics") == pytest.approx(0.03)

    def test_non_dict_json_silently_ignored(self):
        """JSON that isn't a dict (e.g. an array) is rejected."""
        cfg = _cfg(
            MIN_EDGE_FOR_TRADE="0.03",
            MIN_EDGE_BY_CATEGORY='[0.05, 0.08]',
        )
        assert cfg.min_edge_by_category == {}


class TestRiskManagerUsesPerCategory:
    def test_global_threshold_preserved_when_no_override(self):
        cfg = _cfg(MIN_EDGE_FOR_TRADE="0.05")
        rm = RiskManager(cfg, PortfolioTracker())
        sig = Signal(Action.BUY, 0.8, "t", features={"edge": 0.03})
        # Under global threshold → rejected
        v = rm.check("tok1", sig, 10, 0.5, spread=0.02, category="politics")
        assert not v.allowed
        assert "below min 0.05" in v.reason

    def test_category_relaxes_threshold(self):
        """A lower per-category threshold allows trades rejected by global."""
        cfg = _cfg(
            MIN_EDGE_FOR_TRADE="0.08",
            MIN_EDGE_BY_CATEGORY='{"sports": 0.02}',
        )
        rm = RiskManager(cfg, PortfolioTracker())
        sig = Signal(Action.BUY, 0.8, "t", features={"edge": 0.03})
        # sports: threshold 0.02, edge 0.03 → allowed
        v = rm.check("tok1", sig, 10, 0.5, spread=0.02, category="sports")
        assert v.allowed

    def test_category_tightens_threshold(self):
        """A higher per-category threshold blocks trades the global would allow."""
        cfg = _cfg(
            MIN_EDGE_FOR_TRADE="0.02",
            MIN_EDGE_BY_CATEGORY='{"politics": 0.10}',
        )
        rm = RiskManager(cfg, PortfolioTracker())
        sig = Signal(Action.BUY, 0.8, "t", features={"edge": 0.05})
        # politics: threshold 0.10, edge 0.05 → rejected even though global would pass
        v = rm.check("tok1", sig, 10, 0.5, spread=0.02, category="politics")
        assert not v.allowed
        assert "politics" in v.reason
        assert "below min 0.1" in v.reason

    def test_uncategorised_uses_global(self):
        """Empty/missing category resolves to global threshold."""
        cfg = _cfg(
            MIN_EDGE_FOR_TRADE="0.02",
            MIN_EDGE_BY_CATEGORY='{"politics": 0.10}',
        )
        rm = RiskManager(cfg, PortfolioTracker())
        sig = Signal(Action.BUY, 0.8, "t", features={"edge": 0.03})
        # No category → uses global 0.02
        v = rm.check("tok1", sig, 10, 0.5, spread=0.02, category="")
        assert v.allowed
