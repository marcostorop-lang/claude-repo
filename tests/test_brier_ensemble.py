"""Tests for the Brier-weighted ensemble strategy."""

from __future__ import annotations

import pytest

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.base import Action, BaseStrategy, Signal
from src.strategy.brier_ensemble import BrierEnsembleStrategy


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _StaticMember(BaseStrategy):
    def __init__(self, name: str, action: Action, confidence: float = 0.7) -> None:
        self.name = name
        self._a = action
        self._c = confidence

    def evaluate(self, snapshot, price_history):
        return Signal(self._a, self._c, f"{self.name}-fixed")


class _FakeCalibrator:
    """Deterministic per-strategy weight."""

    def __init__(self, weights: dict[str, float]) -> None:
        self.weights = weights

    def calibration_multiplier(self, strategy: str, category: str = "") -> float:
        return float(self.weights.get(strategy, 1.0))


def _snap() -> MarketSnapshot:
    return MarketSnapshot(
        condition_id="c", question="Q?", token_id="t", outcome="YES",
        price=0.5, spread=0.02, volume=10000, liquidity=5000, active=True,
    )


# ---------------------------------------------------------------------------
# Empty / degenerate ensembles
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_no_members_holds(self):
        s = BrierEnsembleStrategy(Config(), members=[])
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.HOLD
        assert "No ensemble members" in sig.reason

    def test_all_members_hold(self):
        members = [
            _StaticMember("a", Action.HOLD),
            _StaticMember("b", Action.HOLD),
        ]
        s = BrierEnsembleStrategy(Config(), members=members)
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.HOLD


# ---------------------------------------------------------------------------
# Vote aggregation
# ---------------------------------------------------------------------------


class TestVotes:
    def test_unanimous_buy_returns_buy(self, monkeypatch):
        monkeypatch.setenv("ENSEMBLE_MIN_NET_SCORE", "0.0")
        monkeypatch.setenv("ENSEMBLE_MIN_TOTAL_WEIGHT", "0.0")
        members = [
            _StaticMember("a", Action.BUY, 0.6),
            _StaticMember("b", Action.BUY, 0.7),
            _StaticMember("c", Action.BUY, 0.8),
        ]
        s = BrierEnsembleStrategy(Config(), members=members)
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.BUY
        # All weights default to 1 → net score is the unweighted mean
        # of the 3 confidences.
        assert sig.confidence == pytest.approx((0.6 + 0.7 + 0.8) / 3, abs=0.01)

    def test_split_zero_weight_holds(self, monkeypatch):
        monkeypatch.setenv("ENSEMBLE_MIN_NET_SCORE", "0.10")
        monkeypatch.setenv("ENSEMBLE_MIN_TOTAL_WEIGHT", "0.0")
        members = [
            _StaticMember("a", Action.BUY, 0.7),
            _StaticMember("b", Action.SELL, 0.7),
        ]
        s = BrierEnsembleStrategy(Config(), members=members)
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.HOLD
        assert "net score" in sig.reason.lower()

    def test_brier_weight_breaks_tie(self, monkeypatch):
        monkeypatch.setenv("ENSEMBLE_MIN_NET_SCORE", "0.0")
        monkeypatch.setenv("ENSEMBLE_MIN_TOTAL_WEIGHT", "0.0")
        members = [
            _StaticMember("a", Action.BUY, 0.7),
            _StaticMember("b", Action.SELL, 0.7),
        ]
        s = BrierEnsembleStrategy(Config(), members=members)
        # Calibrator says "a" is well-calibrated (1.5×), "b" is poor
        # (0.3×).  Net score should land positive → BUY.
        s.calibrator = _FakeCalibrator({"a": 1.5, "b": 0.3})
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.BUY

    def test_total_weight_floor_holds(self, monkeypatch):
        monkeypatch.setenv("ENSEMBLE_MIN_NET_SCORE", "0.0")
        monkeypatch.setenv("ENSEMBLE_MIN_TOTAL_WEIGHT", "5.0")
        members = [_StaticMember("a", Action.BUY, 0.9)]
        s = BrierEnsembleStrategy(Config(), members=members)
        # Default weight is 1; total = 1 < 5 → HOLD.
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.HOLD
        assert "weight" in sig.reason.lower()

    def test_net_score_floor_holds_low_conviction(self, monkeypatch):
        monkeypatch.setenv("ENSEMBLE_MIN_TOTAL_WEIGHT", "0.0")
        monkeypatch.setenv("ENSEMBLE_MIN_NET_SCORE", "0.40")
        members = [
            _StaticMember("a", Action.BUY, 0.20),  # very weak
            _StaticMember("b", Action.BUY, 0.20),
            _StaticMember("c", Action.HOLD),
        ]
        s = BrierEnsembleStrategy(Config(), members=members)
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.HOLD
        assert "net score" in sig.reason.lower()


# ---------------------------------------------------------------------------
# Member exception isolation
# ---------------------------------------------------------------------------


class _BrokenMember(BaseStrategy):
    name = "broken"

    def evaluate(self, snapshot, price_history):
        raise RuntimeError("kaboom")


class TestRobustness:
    def test_broken_member_does_not_crash_ensemble(self, monkeypatch):
        monkeypatch.setenv("ENSEMBLE_MIN_NET_SCORE", "0.0")
        monkeypatch.setenv("ENSEMBLE_MIN_TOTAL_WEIGHT", "0.0")
        members = [
            _BrokenMember(),
            _StaticMember("good", Action.BUY, 0.7),
        ]
        s = BrierEnsembleStrategy(Config(), members=members)
        sig = s.evaluate(_snap(), [])
        assert sig.action == Action.BUY


# ---------------------------------------------------------------------------
# Config-driven member resolution
# ---------------------------------------------------------------------------


class TestMemberResolution:
    def test_unknown_member_dropped(self, monkeypatch):
        monkeypatch.setenv("ENSEMBLE_MEMBERS", "simple_momentum,not_a_strategy")
        s = BrierEnsembleStrategy(Config())
        names = {m.name for m in s.members}
        assert "simple_momentum" in names
        assert "not_a_strategy" not in names

    def test_csv_whitespace_tolerated(self, monkeypatch):
        monkeypatch.setenv("ENSEMBLE_MEMBERS", " simple_momentum , mean_reversion ")
        s = BrierEnsembleStrategy(Config())
        names = {m.name for m in s.members}
        assert names == {"simple_momentum", "mean_reversion"}
