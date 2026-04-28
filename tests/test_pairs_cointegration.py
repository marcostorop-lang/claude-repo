"""Tests for the pairs / cointegration strategy."""

from __future__ import annotations

import pytest

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.base import Action
from src.strategy.pairs_cointegration import PairsCointegrationStrategy


def _snap(token_id: str, price: float, condition_id: str = "c1",
          spread: float = 0.02) -> MarketSnapshot:
    return MarketSnapshot(
        condition_id=condition_id, question="Q?", token_id=token_id,
        outcome="YES", price=price, spread=spread,
        volume=10000, liquidity=5000, active=True,
    )


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


class TestColdStart:
    def test_no_price_holds(self):
        s = PairsCointegrationStrategy(Config())
        snap = _snap("y", 0.5)
        snap.price = None
        sig = s.evaluate(snap, [])
        assert sig.action == Action.HOLD

    def test_no_condition_id_holds(self):
        s = PairsCointegrationStrategy(Config())
        snap = _snap("y", 0.5, condition_id="")
        sig = s.evaluate(snap, [])
        assert sig.action == Action.HOLD

    def test_warmup_holds(self, monkeypatch):
        monkeypatch.setenv("PAIRS_WINDOW", "30")
        s = PairsCointegrationStrategy(Config())
        # Feed only a handful of legs across a few ticks.
        for _ in range(3):
            s.evaluate(_snap("y", 0.6), [])
            s.evaluate(_snap("n", 0.4), [])
        sig = s.evaluate(_snap("y", 0.6), [])
        assert sig.action == Action.HOLD


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------


def _seed_window(s: PairsCointegrationStrategy, *, sum_value: float, n: int) -> None:
    """Push n residuals corresponding to a fixed sum-of-prices."""
    for _ in range(n):
        s.evaluate(_snap("y", sum_value - 0.4), [])
        s.evaluate(_snap("n", 0.4), [])


class TestSignals:
    def test_sum_above_band_sells_bull_leg(self, monkeypatch):
        monkeypatch.setenv("PAIRS_WINDOW", "30")
        monkeypatch.setenv("PAIRS_ENTRY_Z", "1.5")
        monkeypatch.setenv("STRATEGY_NET_EDGE_GATE_ENABLED", "false")
        s = PairsCointegrationStrategy(Config())
        # Seed 30 residuals near 0 (sum ≈ 1.0): yes=0.6, no=0.4.
        _seed_window(s, sum_value=1.0, n=30)
        # Now spike sum to 1.10 (yes=0.7, no=0.4) → residual ≈ +0.10,
        # well above the in-sample stdev (≈ 0).  z very large positive.
        s.evaluate(_snap("y", 0.70), [])
        # Snap on the bull leg ("y", price>0.5) should now SELL.
        sig = s.evaluate(_snap("y", 0.70), [])
        assert sig.action == Action.SELL
        assert sig.features["z_score"] > 1.5

    def test_sum_below_band_buys_cheap_leg(self, monkeypatch):
        monkeypatch.setenv("PAIRS_WINDOW", "30")
        monkeypatch.setenv("PAIRS_ENTRY_Z", "1.5")
        monkeypatch.setenv("STRATEGY_NET_EDGE_GATE_ENABLED", "false")
        s = PairsCointegrationStrategy(Config())
        _seed_window(s, sum_value=1.0, n=30)
        # Now drop the cheap-leg price so sum drops to 0.90.
        s.evaluate(_snap("n", 0.30), [])
        sig = s.evaluate(_snap("n", 0.30), [])
        assert sig.action == Action.BUY
        assert sig.features["z_score"] < -1.5

    def test_within_band_holds(self, monkeypatch):
        monkeypatch.setenv("PAIRS_WINDOW", "30")
        monkeypatch.setenv("PAIRS_ENTRY_Z", "5.0")  # very tight band
        s = PairsCointegrationStrategy(Config())
        _seed_window(s, sum_value=1.0, n=30)
        # Mild spike: residual moves but z stays under 5.
        s.evaluate(_snap("y", 0.62), [])
        sig = s.evaluate(_snap("y", 0.62), [])
        assert sig.action == Action.HOLD

    def test_wrong_side_leg_holds(self, monkeypatch):
        # Residual is high (sum > 1) → expect SELL on the bull leg
        # (price > 0.5).  But if we evaluate the *cheap* leg at the
        # same tick, the strategy must HOLD it (don't buy something
        # whose pair partner is overpriced — that strengthens the
        # mispricing).
        monkeypatch.setenv("PAIRS_WINDOW", "30")
        monkeypatch.setenv("PAIRS_ENTRY_Z", "1.5")
        monkeypatch.setenv("STRATEGY_NET_EDGE_GATE_ENABLED", "false")
        s = PairsCointegrationStrategy(Config())
        _seed_window(s, sum_value=1.0, n=30)
        s.evaluate(_snap("y", 0.70), [])
        # Now look at the cheap leg.  z is still positive → would be
        # SELL on the bull leg, but the cheap leg's sign is wrong.
        sig = s.evaluate(_snap("n", 0.40), [])
        assert sig.action == Action.HOLD


# ---------------------------------------------------------------------------
# Pathological inputs are filtered
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_extreme_residual_is_dropped(self, monkeypatch):
        # If max_abs_residual is 0.05, a tick that pushes the sum to
        # 1.4 (utterly broken) must NOT enter the rolling window.
        monkeypatch.setenv("PAIRS_WINDOW", "30")
        monkeypatch.setenv("PAIRS_MAX_ABS_RESIDUAL", "0.05")
        s = PairsCointegrationStrategy(Config())
        _seed_window(s, sum_value=1.0, n=20)
        before_len = len(s._states["c1"].residuals)
        # A single "y" leg push to 1.0 would yield sum=1.4 (n still
        # at 0.4), residual 0.4, well over the 0.05 cap → dropped.
        s.evaluate(_snap("y", 1.0), [])
        after_len = len(s._states["c1"].residuals)
        assert after_len == before_len
