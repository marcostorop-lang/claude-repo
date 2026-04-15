"""Tests for the Bayesian calibration module.

Covers:
* :class:`Posterior` mean and Beta-update math,
* :class:`BayesianCalibrator` online updates, cold-start posture,
  seeding from closed trades, persistence round-trip,
* Integration with :meth:`RiskManager.compute_position_size` —
  multiplier kicks in only after ``min_samples`` trades and only
  shrinks (never amplifies).
"""

from __future__ import annotations

import pytest

from src.analysis.bayesian_calibrator import BayesianCalibrator, Posterior
from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager


def _cfg(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


# -- Posterior math --------------------------------------------------------


class TestPosterior:
    def test_uniform_prior_mean_is_half(self):
        assert Posterior("s", 1.0, 1.0, 0).mean == pytest.approx(0.5)

    def test_mean_after_wins(self):
        # Beta(1,1) → 5 wins → Beta(6,1) → mean 6/7
        p = Posterior("s", 6.0, 1.0, 5)
        assert p.mean == pytest.approx(6.0 / 7.0)

    def test_degenerate_alpha_plus_beta_zero_is_half(self):
        assert Posterior("s", 0.0, 0.0, 0).mean == 0.5


# -- BayesianCalibrator ----------------------------------------------------


class TestCalibrator:
    def test_record_win_increments_alpha(self):
        c = BayesianCalibrator(store=None)
        c.record_outcome("simple_momentum", won=True)
        p = c.posterior("simple_momentum")
        assert p.alpha == 2.0 and p.beta == 1.0 and p.n_trades == 1

    def test_record_loss_increments_beta(self):
        c = BayesianCalibrator(store=None)
        c.record_outcome("simple_momentum", won=False)
        p = c.posterior("simple_momentum")
        assert p.alpha == 1.0 and p.beta == 2.0 and p.n_trades == 1

    def test_cold_start_multiplier_is_one(self):
        c = BayesianCalibrator(store=None, min_samples=30)
        assert c.size_multiplier("simple_momentum") == 1.0

    def test_multiplier_after_enough_samples(self):
        c = BayesianCalibrator(
            store=None, min_samples=10, min_multiplier=0.3,
        )
        for _ in range(5):
            c.record_outcome("s", won=True)
        for _ in range(5):
            c.record_outcome("s", won=False)
        # Beta(6,6) mean = 0.5 → multiplier 0.5
        assert c.size_multiplier("s") == pytest.approx(0.5)

    def test_multiplier_clamped_to_floor(self):
        c = BayesianCalibrator(
            store=None, min_samples=5, min_multiplier=0.3,
        )
        for _ in range(20):
            c.record_outcome("terrible", won=False)
        # posterior heavily pessimistic → mean ≈ 0.05, clamped up to 0.3
        assert c.size_multiplier("terrible") == pytest.approx(0.3)

    def test_multiplier_never_exceeds_one(self):
        """Asymmetric-by-design: winners never size up past baseline."""
        c = BayesianCalibrator(
            store=None, min_samples=5, min_multiplier=0.3,
        )
        for _ in range(50):
            c.record_outcome("golden", won=True)
        # Posterior mean ≈ 51/52, clamped to 1.0 (the max).
        assert c.size_multiplier("golden") == pytest.approx(51.0 / 52.0)
        assert c.size_multiplier("golden") <= 1.0

    def test_seed_from_closed_trades(self):
        c = BayesianCalibrator(store=None)
        rows = [
            {"strategy": "a", "pnl": 1.0},
            {"strategy": "a", "pnl": -0.5},
            {"strategy": "b", "pnl": 2.0},
            {"strategy": "", "pnl": 1.0},   # no strategy → ignored
            {"strategy": "c", "pnl": None}, # no pnl → ignored
        ]
        c.seed_from_closed_trades(rows)
        pa = c.posterior("a")
        pb = c.posterior("b")
        assert pa.n_trades == 2 and pa.alpha == 2.0 and pa.beta == 2.0
        assert pb.n_trades == 1 and pb.alpha == 2.0 and pb.beta == 1.0


# -- Persistence round-trip ------------------------------------------------


class _FakeStore:
    """In-memory stand-in for SQLiteStore's bayesian helpers."""

    def __init__(self):
        self._rows: dict[str, dict] = {}

    def upsert_bayesian_posterior(self, strategy, alpha, beta, n_trades, updated_at):
        self._rows[strategy] = {
            "strategy": strategy, "alpha": alpha, "beta": beta,
            "n_trades": n_trades, "updated_at": updated_at,
        }

    def get_all_bayesian_posteriors(self):
        return list(self._rows.values())

    def get_bayesian_posterior(self, strategy):
        return self._rows.get(strategy)


class TestPersistence:
    def test_roundtrip(self):
        store = _FakeStore()
        c1 = BayesianCalibrator(store=store)
        c1.record_outcome("s", won=True)
        c1.record_outcome("s", won=True)
        c1.record_outcome("s", won=False)

        # A fresh calibrator on the same store reconstructs the state.
        c2 = BayesianCalibrator(store=store)
        p = c2.posterior("s")
        assert p.n_trades == 3
        assert p.alpha == 3.0 and p.beta == 2.0


# -- RiskManager integration -----------------------------------------------


class TestRiskManagerIntegration:
    def _with_calibrator(self, calibrator=None, **overrides):
        cfg = _cfg(
            bayesian_sizing_enabled=True,
            max_position_size=100.0,
            max_liquidity_fraction=0.0,
            **overrides,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        rm.bayesian_calibrator = calibrator
        return rm

    def test_multiplier_applied_on_sized_down_strategy(self):
        c = BayesianCalibrator(
            store=None, min_samples=5, min_multiplier=0.3,
        )
        for _ in range(10):
            c.record_outcome("loser", won=False)
        rm = self._with_calibrator(c)
        # base = $100 → $100 * 0.3 (clamped) = $30 → /0.5 = 60 shares
        size = rm.compute_position_size(
            price=0.5, confidence=1.0, strategy="loser",
        )
        assert size == pytest.approx(60.0)

    def test_no_strategy_no_multiplier(self):
        """Empty strategy string → Bayesian branch skipped entirely."""
        c = BayesianCalibrator(
            store=None, min_samples=1, min_multiplier=0.3,
        )
        c.record_outcome("s", won=False)
        rm = self._with_calibrator(c)
        size = rm.compute_position_size(price=0.5, confidence=1.0, strategy="")
        # Untouched base: $100 / 0.5 = 200 shares
        assert size == pytest.approx(200.0)

    def test_flag_off_no_multiplier(self):
        c = BayesianCalibrator(
            store=None, min_samples=1, min_multiplier=0.3,
        )
        c.record_outcome("loser", won=False)
        cfg = _cfg(
            bayesian_sizing_enabled=False,   # flag OFF
            max_position_size=100.0, max_liquidity_fraction=0.0,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        rm.bayesian_calibrator = c
        size = rm.compute_position_size(
            price=0.5, confidence=1.0, strategy="loser",
        )
        assert size == pytest.approx(200.0)

    def test_cold_start_no_multiplier(self):
        """Under min_samples → multiplier = 1.0 (no effect)."""
        c = BayesianCalibrator(
            store=None, min_samples=100, min_multiplier=0.3,
        )
        c.record_outcome("s", won=False)
        rm = self._with_calibrator(c)
        size = rm.compute_position_size(price=0.5, confidence=1.0, strategy="s")
        assert size == pytest.approx(200.0)
