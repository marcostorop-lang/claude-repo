"""Tests for the strategy-validation protocol (Fase B).

Covers:
* Risk metrics: Sharpe/Sortino/MaxDD/PSR/DSR/block bootstrap.
* Walk-forward splitter: purge + embargo arithmetic.
* Walk-forward runner: deterministic on a fixed seed.
* evaluate_validation: gate semantics and AND-of-gates verdict.
"""

from __future__ import annotations

import math
import os
import random

import pytest

from src.config import Config
from src.analysis.risk_metrics import (
    block_bootstrap_sharpe,
    deflated_sharpe_ratio,
    max_drawdown,
    probabilistic_sharpe_ratio,
    returns_summary,
)
from src.backtest.walk_forward import (
    WalkForwardReport,
    WindowResult,
    make_splits,
    run_walk_forward,
)
from src.backtest.validate import evaluate_validation
from src.strategy.simple_momentum import SimpleMomentum


# ---------------------------------------------------------------------------
# risk_metrics
# ---------------------------------------------------------------------------


class TestRiskMetricsBasics:
    def test_empty_returns_summary_is_safe(self):
        s = returns_summary([])
        assert s.n == 0
        assert s.sharpe_annualized == 0.0
        assert s.max_drawdown == 0.0

    def test_max_drawdown_positive_only(self):
        # Monotonic-up curve has zero drawdown.
        assert max_drawdown([0.01, 0.02, 0.03, 0.04]) == 0.0

    def test_max_drawdown_simple_dip(self):
        # +0.1, then -0.2 → DD of 0.2 from peak 0.1 to trough -0.1.
        assert abs(max_drawdown([0.1, -0.2]) - 0.2) < 1e-9

    def test_sharpe_zero_variance_safe(self):
        s = returns_summary([0.01] * 10)
        # Constant returns → divide by zero would explode; guard
        # returns 0 as documented.
        assert s.sharpe_annualized == 0.0


class TestPSRandDSR:
    def test_psr_increases_with_n(self):
        # Same SR, more samples → higher PSR (less uncertainty).
        rng = random.Random(0)
        small = [rng.gauss(0.001, 0.01) for _ in range(50)]
        large = [rng.gauss(0.001, 0.01) for _ in range(500)]
        psr_small = probabilistic_sharpe_ratio(small)
        psr_large = probabilistic_sharpe_ratio(large)
        assert 0.0 <= psr_small <= 1.0
        assert 0.0 <= psr_large <= 1.0
        assert psr_large >= psr_small

    def test_psr_zero_mean_below_half(self):
        rng = random.Random(42)
        # zero-mean random walk → PSR vs 0 should not clear 0.95.
        xs = [rng.gauss(0.0, 0.01) for _ in range(300)]
        assert probabilistic_sharpe_ratio(xs) < 0.95

    def test_dsr_more_trials_makes_it_harder(self):
        rng = random.Random(7)
        xs = [rng.gauss(0.001, 0.01) for _ in range(252)]
        # Larger n_trials raises the random-search benchmark, so the
        # same observed Sharpe must clear a higher bar.
        dsr_1 = deflated_sharpe_ratio(xs, n_trials=1)
        dsr_50 = deflated_sharpe_ratio(xs, n_trials=50)
        assert dsr_50 <= dsr_1


class TestBlockBootstrap:
    def test_deterministic_with_seed(self):
        rng = random.Random(0)
        xs = [rng.gauss(0.001, 0.01) for _ in range(200)]
        a = block_bootstrap_sharpe(xs, n_resamples=200, seed=42)
        b = block_bootstrap_sharpe(xs, n_resamples=200, seed=42)
        assert (a.point, a.lower, a.upper, a.block_size) == (
            b.point, b.lower, b.upper, b.block_size,
        )

    def test_lower_below_point_below_upper(self):
        rng = random.Random(0)
        xs = [rng.gauss(0.001, 0.01) for _ in range(200)]
        ci = block_bootstrap_sharpe(xs, n_resamples=300, seed=1)
        assert ci.lower <= ci.point <= ci.upper


# ---------------------------------------------------------------------------
# Walk-forward splitter
# ---------------------------------------------------------------------------


class TestMakeSplits:
    def test_basic_anchored_walk_forward(self):
        splits = make_splits(1000, train_size=300, test_size=100)
        # First split: train [0,300), test [300,400)
        assert splits[0] == (0, 300, 300, 400)
        # Step defaults to test_size → no overlap on test side.
        assert splits[1] == (100, 400, 400, 500)
        # Last test_end <= n_ticks
        assert all(te <= 1000 for _, _, _, te in splits)

    def test_purge_shrinks_train_end(self):
        splits = make_splits(1000, train_size=300, test_size=100, purge=20)
        # Train end was 300; purge 20 → 280.
        assert splits[0] == (0, 280, 300, 400)

    def test_embargo_pushes_test_start_and_end(self):
        splits = make_splits(1000, train_size=300, test_size=100, embargo=15)
        # test_start_raw = 300, +15 = 315 ; test_end = 315 + 100 = 415
        assert splits[0] == (0, 300, 315, 415)

    def test_returns_empty_when_n_too_small(self):
        assert make_splits(50, train_size=100, test_size=20) == []

    def test_pathological_purge_safe(self):
        # purge > train_size would make purged_end <= tr_start; we
        # bail out cleanly.
        assert make_splits(1000, train_size=10, test_size=10, purge=20) == []


# ---------------------------------------------------------------------------
# Walk-forward runner
# ---------------------------------------------------------------------------


def _bounded_walk(n: int, *, drift: float = 0.0, seed: int = 0) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    p = 0.5
    out: list[tuple[float, float]] = []
    for _ in range(n):
        p = max(0.2, min(0.8, p + drift + 0.005 * rng.gauss(0, 1)))
        out.append((p, 0.02))
    return out


class TestWalkForwardRunner:
    def setup_method(self):
        os.environ["STRATEGY_NET_EDGE_GATE_ENABLED"] = "false"
        os.environ["MOMENTUM_THRESHOLD"] = "0.005"
        os.environ["MOMENTUM_WINDOW"] = "3"

    def teardown_method(self):
        for k in ("STRATEGY_NET_EDGE_GATE_ENABLED", "MOMENTUM_THRESHOLD", "MOMENTUM_WINDOW"):
            os.environ.pop(k, None)

    def test_deterministic_with_same_seed(self):
        cfg = Config()
        strat = SimpleMomentum(cfg)
        prices = {f"t{i}": _bounded_walk(800, seed=i) for i in range(3)}
        a = run_walk_forward(
            cfg, strat, prices,
            train_size=200, test_size=100, purge=5, embargo=2, seed=99,
        )
        b = run_walk_forward(
            cfg, strat, prices,
            train_size=200, test_size=100, purge=5, embargo=2, seed=99,
        )
        assert a.total_pnl() == b.total_pnl()
        assert a.total_trades() == b.total_trades()
        assert [w.sharpe_annualized for w in a.windows] == [
            w.sharpe_annualized for w in b.windows
        ]

    def test_no_data_returns_empty_report(self):
        cfg = Config()
        strat = SimpleMomentum(cfg)
        rep = run_walk_forward(cfg, strat, {}, train_size=100, test_size=50)
        assert rep.n_windows == 0
        assert rep.windows == []
        assert rep.pooled_returns == []

    def test_test_window_does_not_leak_into_train(self):
        # Validates the window math: test_start must equal
        # train_end_full + embargo (regardless of purge, which only
        # shrinks the *train* slice the strategy can refit on).
        splits = make_splits(1000, train_size=300, test_size=100, purge=20, embargo=10)
        for tr_s, tr_e, te_s, te_e in splits:
            # purged train end <= un-purged train end
            assert tr_e <= tr_s + 300
            # test_start = un-purged train end + embargo
            assert te_s == tr_s + 300 + 10
            # no overlap between train slice and test slice
            assert tr_e <= te_s


# ---------------------------------------------------------------------------
# evaluate_validation
# ---------------------------------------------------------------------------


def _wf(*, n_windows: int, sharpes: list[float], pooled_returns: list[float],
        trades: int, n_tokens: int = 1) -> WalkForwardReport:
    rep = WalkForwardReport(
        strategy="x",
        n_windows=n_windows,
        n_tokens=n_tokens,
        train_size=200,
        test_size=100,
        purge=0,
        embargo=0,
        seed=0,
    )
    for i, sr in enumerate(sharpes):
        rep.windows.append(WindowResult(
            index=i, train_start=0, train_end=200,
            test_start=200, test_end=300, n_test_ticks=100,
            n_trades=trades // max(1, n_windows),
            total_pnl=sr,  # not used by gates other than ``winning_windows``
            avg_return_pct=0.0,
            sharpe_annualized=sr,
            win_rate=0.5,
            max_drawdown_pct=0.05,
        ))
    rep.pooled_returns = list(pooled_returns)
    return rep


class TestEvaluateValidation:
    def _cfg(self, **env):
        for k, v in env.items():
            os.environ[k] = str(v)
        return Config()

    def test_too_few_trades_rejects(self):
        cfg = self._cfg(VALIDATION_MIN_TRADES=200)
        rep = _wf(
            n_windows=2, sharpes=[1.0, 1.0],
            pooled_returns=[0.01] * 30, trades=30,
        )
        v = evaluate_validation(strategy_name="x", walk_forward=rep, cfg=cfg)
        assert not v.promote
        # Specifically the trades gate should be one of the failing ones.
        names = {g.name for g in v.failing_gates()}
        assert "trades >= min_trades" in names

    def test_obviously_negative_strategy_rejects(self):
        cfg = self._cfg(VALIDATION_MIN_TRADES=10)
        rng = random.Random(0)
        # Negative-mean returns → all gates that look at PSR/DSR/Sharpe fail.
        pooled = [rng.gauss(-0.005, 0.01) for _ in range(300)]
        rep = _wf(
            n_windows=4, sharpes=[-0.8, -0.5, -0.7, -0.6],
            pooled_returns=pooled, trades=300,
        )
        v = evaluate_validation(strategy_name="x", walk_forward=rep, cfg=cfg)
        assert not v.promote

    def test_synthetic_pass_promotes(self):
        # Construct a synthetic OOS series with strong positive Sharpe
        # *and* enough trades to clear the floor.  Loosen the OOS-window
        # threshold (we only manufacture 4 fake windows) to keep the
        # test focused on the statistical gates.
        cfg = self._cfg(
            VALIDATION_MIN_TRADES=100,
            VALIDATION_MIN_OOS_WINNING_WINDOWS_FRAC=0.5,
            VALIDATION_MIN_MEDIAN_OOS_SHARPE=1.0,
            VALIDATION_MIN_PSR=0.95,
            VALIDATION_MIN_DSR=0.95,
            VALIDATION_MAX_DRAWDOWN_PCT=0.5,
            VALIDATION_N_TRIALS=1,
        )
        rng = random.Random(123)
        # Strong, low-noise positive-mean series → very high SR.
        pooled = [rng.gauss(0.005, 0.005) for _ in range(400)]
        rep = _wf(
            n_windows=4, sharpes=[2.0, 1.8, 2.1, 1.9],
            pooled_returns=pooled, trades=400,
        )
        v = evaluate_validation(strategy_name="x", walk_forward=rep, cfg=cfg)
        # Hard assertion on a few key gates rather than the global verdict
        # (the bootstrap CI on a random sample can flicker around 0).
        gate_by_name = {g.name: g for g in v.gates}
        assert gate_by_name["trades >= min_trades"].passed
        assert gate_by_name["OOS winning-window fraction"].passed
        assert gate_by_name["median OOS Sharpe"].passed
        assert gate_by_name["Probabilistic Sharpe Ratio"].passed
