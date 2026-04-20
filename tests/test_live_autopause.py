"""Tests for the first-N live-trades autopause gate.

Covers:
* Disabled when threshold is 0 (default) — all BUYs allowed.
* Threshold counts only live BUYs, not SELLs.
* Block activates exactly at the threshold.
* Ack file presence unblocks.
* RiskManager integration: BUY gated, SELL never gated.
* Initial count seeding behaviour.
"""

from __future__ import annotations

import os
import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker, Position
from src.risk.live_autopause import LiveTradeAutopauseGate
from src.risk.manager import RiskManager
from src.strategy.base import Action, Signal


def _cfg(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


def _buy() -> Signal:
    return Signal(action=Action.BUY, confidence=1.0, reason="t", features={})


def _sell() -> Signal:
    return Signal(action=Action.SELL, confidence=1.0, reason="exit", features={})


# -- Pure gate -------------------------------------------------------------


class TestGate:
    def test_disabled_when_threshold_zero(self):
        g = LiveTradeAutopauseGate(threshold=0, ack_file="/tmp/ack")
        assert g.enabled is False
        assert g.check_buy_allowed().allowed is True

    def test_allows_below_threshold(self):
        g = LiveTradeAutopauseGate(threshold=3, ack_file="/tmp/missing.ack")
        assert g.check_buy_allowed().allowed is True
        g.record_live_fill("BUY")
        g.record_live_fill("BUY")
        assert g.count == 2
        assert g.check_buy_allowed().allowed is True

    def test_blocks_at_threshold(self, tmp_path):
        ack = tmp_path / "no_ack_yet.ack"
        g = LiveTradeAutopauseGate(threshold=2, ack_file=str(ack))
        g.record_live_fill("BUY")
        g.record_live_fill("BUY")
        v = g.check_buy_allowed()
        assert v.allowed is False
        assert "hand-verify" in v.reason
        assert str(ack) in v.reason

    def test_ack_file_unblocks(self, tmp_path):
        ack = tmp_path / "ack.ack"
        g = LiveTradeAutopauseGate(threshold=2, ack_file=str(ack))
        g.record_live_fill("BUY")
        g.record_live_fill("BUY")
        assert g.check_buy_allowed().allowed is False
        ack.write_text("ok")
        v = g.check_buy_allowed()
        assert v.allowed is True
        assert "acknowledged" in v.reason

    def test_sell_does_not_count(self):
        g = LiveTradeAutopauseGate(threshold=2, ack_file="/tmp/noack")
        g.record_live_fill("SELL")
        g.record_live_fill("SELL")
        g.record_live_fill("SELL")
        # Only SELLs so far → counter unchanged.
        assert g.count == 0
        assert g.check_buy_allowed().allowed is True

    def test_case_insensitive_side(self):
        g = LiveTradeAutopauseGate(threshold=5, ack_file="/tmp/x")
        g.record_live_fill("buy")
        g.record_live_fill("Buy")
        assert g.count == 2

    def test_initial_count_seeds_counter(self, tmp_path):
        ack = tmp_path / "no.ack"
        g = LiveTradeAutopauseGate(
            threshold=3, ack_file=str(ack), initial_count=5,
        )
        # Initial count already over threshold → blocked without any
        # new fills this session.
        assert g.check_buy_allowed().allowed is False


# -- RiskManager integration ----------------------------------------------


class TestRiskManagerIntegration:
    def test_gate_blocks_buy_when_threshold_reached(self, tmp_path):
        ack = tmp_path / "no.ack"
        cfg = _cfg(max_position_size=100.0, max_total_exposure=500.0)
        rm = RiskManager(cfg, PortfolioTracker())
        rm.live_autopause_gate = LiveTradeAutopauseGate(
            threshold=1, ack_file=str(ack), initial_count=1,
        )
        v = rm.check("tok1", _buy(), proposed_size=10.0, price=0.5)
        assert v.allowed is False
        assert "autopause" in v.reason

    def test_gate_allows_sell_even_when_paused(self, tmp_path):
        ack = tmp_path / "no.ack"
        cfg = _cfg(max_position_size=100.0, max_total_exposure=500.0)
        pt = PortfolioTracker()
        pt.open_position(Position(
            token_id="tok1", condition_id="c1", side="BUY",
            size=10.0, entry_price=0.5, strategy="s", order_id="o",
        ))
        rm = RiskManager(cfg, pt)
        rm.live_autopause_gate = LiveTradeAutopauseGate(
            threshold=1, ack_file=str(ack), initial_count=99,
        )
        v = rm.check("tok1", _sell(), proposed_size=10.0, price=0.5)
        assert v.allowed is True

    def test_gate_allows_buy_after_ack(self, tmp_path):
        ack = tmp_path / "ok.ack"
        ack.write_text("")
        cfg = _cfg(max_position_size=100.0, max_total_exposure=500.0)
        rm = RiskManager(cfg, PortfolioTracker())
        rm.live_autopause_gate = LiveTradeAutopauseGate(
            threshold=1, ack_file=str(ack), initial_count=1,
        )
        v = rm.check("tok1", _buy(), proposed_size=10.0, price=0.5)
        assert v.allowed is True

    def test_disabled_gate_is_noop(self):
        cfg = _cfg(max_position_size=100.0, max_total_exposure=500.0)
        rm = RiskManager(cfg, PortfolioTracker())
        rm.live_autopause_gate = LiveTradeAutopauseGate(
            threshold=0, ack_file="/tmp/na", initial_count=999,
        )
        v = rm.check("tok1", _buy(), proposed_size=10.0, price=0.5)
        assert v.allowed is True

    def test_no_gate_attached_is_noop(self):
        cfg = _cfg(max_position_size=100.0, max_total_exposure=500.0)
        rm = RiskManager(cfg, PortfolioTracker())
        assert rm.live_autopause_gate is None
        v = rm.check("tok1", _buy(), proposed_size=10.0, price=0.5)
        assert v.allowed is True
