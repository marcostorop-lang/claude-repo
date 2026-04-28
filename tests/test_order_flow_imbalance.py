"""Tests for the Order-Flow Imbalance strategy.

OFI is a microstructure signal — it requires the order book, not
just the midpoint.  These tests inject a fake ``book_provider`` so
they're deterministic and do not need a live market.
"""

from __future__ import annotations

import pytest

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.base import Action
from src.strategy.order_flow_imbalance import OrderFlowImbalanceStrategy


def _snap(token_id: str = "tok1", price: float = 0.5, spread: float = 0.02) -> MarketSnapshot:
    return MarketSnapshot(
        condition_id="c", question="Q?", token_id=token_id, outcome="YES",
        price=price, spread=spread, volume=10000, liquidity=5000, active=True,
    )


class _FakeBook:
    """Programmable book provider returning a queue of dicts."""

    def __init__(self, frames: list[dict | None]):
        self.frames = list(frames)

    def __call__(self, token_id: str) -> dict | None:
        if not self.frames:
            return None
        return self.frames.pop(0)


def _balanced(n: int) -> list[dict]:
    return [{"bid_depth_usd": 1000, "ask_depth_usd": 1000} for _ in range(n)]


def _bid_heavy(n: int) -> list[dict]:
    return [{"bid_depth_usd": 1500, "ask_depth_usd": 500} for _ in range(n)]


def _ask_heavy(n: int) -> list[dict]:
    return [{"bid_depth_usd": 500, "ask_depth_usd": 1500} for _ in range(n)]


# ---------------------------------------------------------------------------
# Cold start / missing inputs
# ---------------------------------------------------------------------------


class TestColdStart:
    def test_no_book_provider_returns_hold(self, monkeypatch):
        cfg = Config()
        s = OrderFlowImbalanceStrategy(cfg)
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.HOLD
        assert "No book provider" in sig.reason

    def test_no_price_returns_hold(self, monkeypatch):
        cfg = Config()
        s = OrderFlowImbalanceStrategy(cfg, book_provider=_FakeBook(_bid_heavy(10)))
        snap = _snap()
        snap.price = None
        sig = s.evaluate(snap, [])
        assert sig.action == Action.HOLD
        assert "No price" in sig.reason

    def test_warmup_window_returns_hold(self, monkeypatch):
        monkeypatch.setenv("OFI_WINDOW", "5")
        monkeypatch.setenv("OFI_MIN_DEPTH_USD", "100")
        cfg = Config()
        s = OrderFlowImbalanceStrategy(cfg, book_provider=_FakeBook(_bid_heavy(10)))
        # Each evaluate call records one window entry; first 4 → HOLD
        for _ in range(4):
            sig = s.evaluate(_snap(), [])
            assert sig.action == Action.HOLD
            assert "Warming up" in sig.reason

    def test_thin_book_returns_hold(self, monkeypatch):
        monkeypatch.setenv("OFI_WINDOW", "3")
        monkeypatch.setenv("OFI_MIN_DEPTH_USD", "1000")
        cfg = Config()
        provider = _FakeBook([
            {"bid_depth_usd": 50, "ask_depth_usd": 50},
        ])
        s = OrderFlowImbalanceStrategy(cfg, book_provider=provider)
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.HOLD
        assert "thin" in sig.reason.lower()


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------


class TestSignals:
    def test_sustained_bid_pressure_buys(self, monkeypatch):
        monkeypatch.setenv("OFI_WINDOW", "3")
        monkeypatch.setenv("OFI_THRESHOLD", "0.30")
        monkeypatch.setenv("OFI_MIN_DEPTH_USD", "100")
        # Disable downstream gates so the test isolates the OFI decision.
        monkeypatch.setenv("STRATEGY_NET_EDGE_GATE_ENABLED", "false")
        cfg = Config()
        s = OrderFlowImbalanceStrategy(cfg, book_provider=_FakeBook(_bid_heavy(3)))
        # Three ticks with imbalance ~0.5 each → mean 0.5 → BUY.
        for _ in range(2):
            s.evaluate(_snap(), [])
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.BUY
        assert sig.features["mean_imbalance"] > 0.30
        assert sig.features["all_same_sign"] is True
        assert sig.confidence > 0

    def test_sustained_ask_pressure_sells(self, monkeypatch):
        monkeypatch.setenv("OFI_WINDOW", "3")
        monkeypatch.setenv("OFI_THRESHOLD", "0.30")
        monkeypatch.setenv("OFI_MIN_DEPTH_USD", "100")
        monkeypatch.setenv("STRATEGY_NET_EDGE_GATE_ENABLED", "false")
        cfg = Config()
        s = OrderFlowImbalanceStrategy(cfg, book_provider=_FakeBook(_ask_heavy(3)))
        for _ in range(2):
            s.evaluate(_snap(), [])
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.SELL
        assert sig.features["mean_imbalance"] < -0.30

    def test_balanced_book_holds(self, monkeypatch):
        monkeypatch.setenv("OFI_WINDOW", "3")
        monkeypatch.setenv("OFI_THRESHOLD", "0.30")
        monkeypatch.setenv("OFI_MIN_DEPTH_USD", "100")
        cfg = Config()
        s = OrderFlowImbalanceStrategy(cfg, book_provider=_FakeBook(_balanced(3)))
        for _ in range(2):
            s.evaluate(_snap(), [])
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.HOLD

    def test_mixed_signs_holds_even_above_threshold(self, monkeypatch):
        # A whip-saw window: +0.5, -0.5, +0.5 → mean 0.17 below
        # threshold AND signs mixed.  Must HOLD.
        monkeypatch.setenv("OFI_WINDOW", "3")
        monkeypatch.setenv("OFI_THRESHOLD", "0.10")
        monkeypatch.setenv("OFI_MIN_DEPTH_USD", "100")
        cfg = Config()
        provider = _FakeBook([
            {"bid_depth_usd": 1500, "ask_depth_usd": 500},  # +0.5
            {"bid_depth_usd": 500, "ask_depth_usd": 1500},  # -0.5
            {"bid_depth_usd": 1500, "ask_depth_usd": 500},  # +0.5
        ])
        s = OrderFlowImbalanceStrategy(cfg, book_provider=provider)
        for _ in range(2):
            s.evaluate(_snap(), [])
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.HOLD
        assert sig.features["all_same_sign"] is False


# ---------------------------------------------------------------------------
# Net edge gate integration
# ---------------------------------------------------------------------------


class TestNetEdgeGate:
    def test_gate_rejects_when_spread_eats_imbalance(self, monkeypatch):
        # Strong imbalance (0.5) but a 1.0 spread → half_spread 0.5
        # consumes the entire mean imbalance → net_edge ~ 0 → HOLD.
        monkeypatch.setenv("OFI_WINDOW", "3")
        monkeypatch.setenv("OFI_THRESHOLD", "0.30")
        monkeypatch.setenv("OFI_MIN_DEPTH_USD", "100")
        monkeypatch.setenv("STRATEGY_NET_EDGE_GATE_ENABLED", "true")
        monkeypatch.setenv("STRATEGY_MIN_NET_EDGE", "0.10")
        cfg = Config()
        s = OrderFlowImbalanceStrategy(cfg, book_provider=_FakeBook(_bid_heavy(3)))
        for _ in range(2):
            s.evaluate(_snap(spread=1.0), [])
        sig = s.evaluate(_snap(spread=1.0), [])
        assert sig.action == Action.HOLD
        assert "net edge" in sig.reason.lower()


# ---------------------------------------------------------------------------
# Per-token isolation
# ---------------------------------------------------------------------------


class TestPerTokenIsolation:
    def test_windows_are_per_token(self, monkeypatch):
        monkeypatch.setenv("OFI_WINDOW", "3")
        monkeypatch.setenv("OFI_THRESHOLD", "0.30")
        monkeypatch.setenv("OFI_MIN_DEPTH_USD", "100")
        cfg = Config()
        # First token bid-heavy; second balanced.  Both must keep
        # independent windows — second token's HOLD shouldn't reset
        # first token's progress.
        s = OrderFlowImbalanceStrategy(cfg)

        def book(tok: str):
            if tok == "t1":
                return {"bid_depth_usd": 1500, "ask_depth_usd": 500}
            return {"bid_depth_usd": 1000, "ask_depth_usd": 1000}

        s.book_provider = book
        for _ in range(3):
            s.evaluate(_snap(token_id="t2"), [])
            s.evaluate(_snap(token_id="t1"), [])
        # t1 reached 3 bid-heavy entries.
        sig_t1 = s.evaluate(_snap(token_id="t1"), [])
        assert sig_t1.action == Action.BUY
