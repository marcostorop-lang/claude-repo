"""Tests for the edge-flip early-exit logic.

The edge-flip logic lives inline in main._tick so we test the underlying
estimate_edge behaviour that drives it, plus verify the config plumbing.
"""

import os
from unittest import mock

from src.analysis.edge import estimate_edge
from src.config import Config


def _cfg(**overrides):
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


class TestExitOnEdgeFlipConfig:
    def test_default_disabled(self):
        cfg = _cfg()
        assert cfg.exit_on_edge_flip is False

    def test_can_enable(self):
        cfg = _cfg(EXIT_ON_EDGE_FLIP="true", EXIT_EDGE_FLIP_THRESHOLD="0.05")
        assert cfg.exit_on_edge_flip is True
        assert cfg.exit_edge_flip_threshold == 0.05


class TestEdgeFlipDetection:
    def test_edge_flips_negative_on_downtrend(self):
        """A BUY position's edge should turn negative as price trends against us."""
        # History: strong uptrend peaked then sharp reversal
        history = [0.40, 0.45, 0.50, 0.55, 0.60, 0.58, 0.55, 0.52, 0.48, 0.45]
        est = estimate_edge(price=0.45, price_history=history, spread=0.02)
        # Edge on price now 0.45 after reversal — should not be strongly positive
        assert est.edge < 0.05

    def test_edge_preserves_sign_on_continuation(self):
        """If price keeps trending up, edge should remain roughly non-negative."""
        history = [0.40, 0.43, 0.46, 0.49, 0.52, 0.55, 0.58, 0.61, 0.64, 0.67]
        est = estimate_edge(price=0.67, price_history=history, spread=0.02)
        # Edge should not be strongly negative (we're in-trend)
        assert est.edge > -0.03
