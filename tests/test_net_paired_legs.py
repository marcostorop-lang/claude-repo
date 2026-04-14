"""Tests for net-paired-legs accounting and depth-aware sizing.

These cover the two risk-manager additions introduced to support
neg-risk and multi-leg strategies (like the semantic engine) without
breaking the default single-leg accounting.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker, Position
from src.risk.manager import RiskManager
from src.strategy.base import Action, Signal


def _cfg(**overrides) -> Config:
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "MAX_POSITION_SIZE": "50",
        "MAX_TOTAL_EXPOSURE": "500",
        "MAX_OPEN_POSITIONS": "2",
        "MAX_EXPOSURE_PER_EVENT": "100",
        "MIN_PRICE": "0.05",
        "MAX_PRICE": "0.95",
        "MAX_SPREAD": "0.20",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


def _pos(token_id: str, condition_id: str, side: str = "BUY",
         size: float = 100.0, entry: float = 0.40,
         category: str = "politics") -> Position:
    return Position(
        token_id=token_id,
        condition_id=condition_id,
        side=side,
        size=size,
        entry_price=entry,
        strategy="semantic_mispricing",
        order_id="",
        category=category,
    )


def _sig(action: Action = Action.BUY, condition_id: str | None = None,
         edge: float = 0.05, confidence: float = 0.8) -> Signal:
    feats = {"edge": edge}
    if condition_id:
        feats["condition_id"] = condition_id
    return Signal(action=action, confidence=confidence, reason="t", features=feats)


# ---------------------------------------------------------------------------
# Portfolio helpers
# ---------------------------------------------------------------------------

class TestPortfolioPairedLegs:
    def test_event_slot_count_groups_by_condition(self):
        pt = PortfolioTracker()
        pt.positions["yes"] = _pos("yes", "cA")
        pt.positions["no"] = _pos("no", "cA")
        pt.positions["other"] = _pos("other", "cB")
        # 2 distinct condition_ids -> 2 slots, even though 3 positions exist.
        assert pt.event_slot_count() == 2
        assert pt.open_position_count() == 3

    def test_net_exposure_by_condition_cancels_opposing(self):
        pt = PortfolioTracker()
        # BUY Yes + SELL Yes on the same condition are opposing → near-zero net.
        pt.positions["a"] = _pos("a", "c1", side="BUY", size=100, entry=0.40)
        pt.positions["b"] = _pos("b", "c1", side="SELL", size=100, entry=0.40)
        assert pt.net_exposure_by_condition("c1") == pytest.approx(0.0, abs=1e-6)

    def test_net_exposure_by_condition_sums_same_side(self):
        pt = PortfolioTracker()
        pt.positions["a"] = _pos("a", "c1", side="BUY", size=100, entry=0.40)
        pt.positions["b"] = _pos("b", "c1", side="BUY", size=50, entry=0.40)
        # Both BUYs stack: 100*0.40 + 50*0.40 = 60
        assert pt.net_exposure_by_condition("c1") == pytest.approx(60.0, abs=1e-6)

    def test_is_paired_leg_detects_second_yesno_leg(self):
        pt = PortfolioTracker()
        pt.positions["yes"] = _pos("yes", "cA", side="BUY")
        # Opening BUY on 'no' of the same condition → paired leg.
        assert pt.is_paired_leg_buy("cA", "no") is True
        # Same token isn't a paired leg (it's a scale-in).
        assert pt.is_paired_leg_buy("cA", "yes") is False
        # Different condition isn't a paired leg.
        assert pt.is_paired_leg_buy("cB", "other") is False


# ---------------------------------------------------------------------------
# Risk manager: max_open_positions with paired-legs
# ---------------------------------------------------------------------------

class TestPairedLegRiskCheck:
    def test_paired_leg_does_not_consume_slot(self):
        cfg = _cfg(MAX_OPEN_POSITIONS="1", NET_PAIRED_LEGS="true")
        pt = PortfolioTracker()
        pt.positions["yes"] = _pos("yes", "cA", side="BUY")
        rm = RiskManager(cfg, pt)

        # MAX_OPEN_POSITIONS=1 is already saturated, BUT the incoming signal
        # is a paired leg on the same condition → should be allowed.
        sig = _sig(Action.BUY, condition_id="cA")
        verdict = rm.check("no", sig, proposed_size=50.0, price=0.40, category="politics")
        assert verdict.allowed is True

    def test_unpaired_second_event_blocked(self):
        cfg = _cfg(MAX_OPEN_POSITIONS="1", NET_PAIRED_LEGS="true")
        pt = PortfolioTracker()
        pt.positions["yes"] = _pos("yes", "cA", side="BUY")
        rm = RiskManager(cfg, pt)

        sig = _sig(Action.BUY, condition_id="cB")  # different event
        verdict = rm.check("zzz", sig, proposed_size=50.0, price=0.40, category="politics")
        assert verdict.allowed is False
        assert "Max open events" in verdict.reason

    def test_legacy_behaviour_when_flag_off(self):
        """Default-off: paired legs still consume a slot, preserving old behaviour."""
        cfg = _cfg(MAX_OPEN_POSITIONS="1")  # NET_PAIRED_LEGS defaults to false
        pt = PortfolioTracker()
        pt.positions["yes"] = _pos("yes", "cA", side="BUY")
        rm = RiskManager(cfg, pt)

        sig = _sig(Action.BUY, condition_id="cA")
        verdict = rm.check("no", sig, proposed_size=50.0, price=0.40, category="politics")
        assert verdict.allowed is False
        assert "Max open positions" in verdict.reason


# ---------------------------------------------------------------------------
# Depth-aware sizing
# ---------------------------------------------------------------------------

class TestDepthAwareSizing:
    def test_depth_cap_shrinks_base_size(self):
        cfg = _cfg(
            MAX_POSITION_SIZE="100",
            MAX_BOOK_DEPTH_FRACTION="0.25",
        )
        rm = RiskManager(cfg, PortfolioTracker())
        # With depth_5pct = 100 USD and cap 0.25 → max $25 of notional.
        size = rm.compute_position_size(
            price=0.50, confidence=1.0, liquidity=10_000,
            book_depth_usd=100.0,
        )
        # shares = 25 / 0.50 = 50
        assert size == pytest.approx(50.0, rel=1e-3)

    def test_depth_cap_skipped_when_disabled(self):
        """MAX_BOOK_DEPTH_FRACTION=0 (default) never caps even with tiny depth."""
        cfg = _cfg(MAX_POSITION_SIZE="100")  # depth cap defaults to 0
        rm = RiskManager(cfg, PortfolioTracker())
        # Even with depth = $1, without the flag the base size is unchanged.
        size = rm.compute_position_size(
            price=0.50, confidence=1.0, liquidity=10_000,
            book_depth_usd=1.0,
        )
        # shares = 100 / 0.50 = 200 (not capped)
        assert size == pytest.approx(200.0, rel=1e-3)

    def test_depth_cap_tightens_beyond_liquidity_cap(self):
        """Book-depth cap is stricter than the flat liquidity cap."""
        cfg = _cfg(
            MAX_POSITION_SIZE="100",
            MAX_LIQUIDITY_FRACTION="1.0",      # effectively no liq cap
            MAX_BOOK_DEPTH_FRACTION="0.10",    # strict 10% of depth
        )
        rm = RiskManager(cfg, PortfolioTracker())
        size = rm.compute_position_size(
            price=0.50, confidence=1.0, liquidity=10_000,
            book_depth_usd=200.0,
        )
        # min($100, 10% of $200) = $20 → shares = 40
        assert size == pytest.approx(40.0, rel=1e-3)

    def test_zero_depth_leaves_sizing_alone(self):
        """If book_depth_usd=0 (fetch failed), fall back cleanly."""
        cfg = _cfg(MAX_POSITION_SIZE="100", MAX_BOOK_DEPTH_FRACTION="0.25")
        rm = RiskManager(cfg, PortfolioTracker())
        size = rm.compute_position_size(
            price=0.50, confidence=1.0, liquidity=10_000,
            book_depth_usd=0.0,
        )
        # shares = 100 / 0.50 = 200 (not capped, depth unknown)
        assert size == pytest.approx(200.0, rel=1e-3)
