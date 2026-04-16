"""Tests for the portfolio tail-risk module.

Covers:
* :func:`compute_tail_risk` on the empty / single / two-position
  exact-enumeration paths (deterministic, easy to reason about),
* the Monte-Carlo path crossing the threshold (roughly converges to
  the exact answer on a known small portfolio),
* sign conventions: VaR/CVaR are positive USD-of-loss, worst_case
  matches the sum of "lose-everything" amounts.
"""

from __future__ import annotations

import pytest

from src.analysis.tail_risk import compute_tail_risk
from src.portfolio.tracker import Position


def _pos(token, side="BUY", size=10.0, entry=0.50, cond=None) -> Position:
    return Position(
        token_id=token, condition_id=cond or token + "-c",
        side=side, size=size, entry_price=entry,
        strategy="s", order_id="o",
    )


# -- empty + single positions ----------------------------------------------


class TestEdgeCases:
    def test_empty_portfolio(self):
        m = compute_tail_risk([])
        assert m.var_95 == 0.0 and m.cvar_95 == 0.0
        assert m.worst_case == 0.0 and m.expected_loss == 0.0
        assert m.n_positions == 0 and m.method == "empty"

    def test_single_long_50c_size10(self):
        # BUY 10 @ 0.50 → loss_if_lose = 5.0 (lose the premium),
        #                gain_if_win  = 5.0 (the other half pays out)
        # p_win = market price = 0.5 (no price_fn → falls back to entry).
        m = compute_tail_risk([_pos("t1")])
        assert m.method == "exact"
        assert m.worst_case == pytest.approx(5.0)
        # 95% VaR with two equally-likely outcomes: the worse one
        # (loss = +5.0) sits in the 50% tail → it's the VaR.
        assert m.var_95 == pytest.approx(5.0)
        assert m.cvar_95 == pytest.approx(5.0)

    def test_single_short_30c_size10(self):
        # SELL 10 @ 0.30 → loss_if_lose = (1-0.30)*10 = 7.0
        #                  gain_if_win  = 0.30 * 10  = 3.0
        # p_win for short = 1 - market = 1 - 0.30 = 0.70
        m = compute_tail_risk([_pos("t1", side="SELL", entry=0.30)])
        assert m.worst_case == pytest.approx(7.0)
        # var_95 captures the 30% tail → the only "lose" branch which
        # has 30% probability falls inside the 5% tail → VaR=7.0.
        assert m.var_95 == pytest.approx(7.0)


# -- two positions, exact enumeration --------------------------------------


class TestTwoPositions:
    def test_independent_pair(self):
        # Two BUYs @ 0.5 size=10 → losses ∈ {-5, -5+5, +5-5, +10}
        # sorted by P&L worst→best: +10 (both lose), 0, 0, -10 (both win)
        # Each combo has probability 0.25.
        m = compute_tail_risk([_pos("a"), _pos("b")])
        assert m.method == "exact"
        assert m.worst_case == pytest.approx(10.0)
        # 5% tail captures only the worst branch (prob=0.25 ≥ 0.05) → VaR=10
        assert m.var_95 == pytest.approx(10.0)
        # CVaR = expected loss in the worst 5% mass → only the worst
        # branch contributes (its weight clipped to 0.05) → 10.0
        assert m.cvar_95 == pytest.approx(10.0)

    def test_expected_loss_zero_for_efficient_market(self):
        """If price_fn = entry, expected P&L is zero (binary fair price)."""
        m = compute_tail_risk([_pos("a"), _pos("b")])
        assert m.expected_loss == pytest.approx(0.0, abs=1e-9)

    def test_price_fn_lifts_p_win(self):
        # BUY @ 0.50 but live price has moved to 0.80 → p_win = 0.80
        # Worst-case still 5 (we still bought 10 @ 0.50), but the
        # *probability* of that worst case dropped to 0.20 — so
        # 95% VaR may now sit in a different bucket.
        m = compute_tail_risk(
            [_pos("a")], price_fn=lambda tid: 0.80,
        )
        # Two outcomes: lose (prob 0.20, P&L +5) and win (prob 0.80, P&L -5).
        # 5% tail is wholly inside the lose branch → VaR=5.
        assert m.var_95 == pytest.approx(5.0)
        # Expected loss = -(gain*p_win - loss*p_lose) = -(5*0.8 - 5*0.2) = -3.0
        assert m.expected_loss == pytest.approx(-3.0)


# -- Monte Carlo path ------------------------------------------------------


class TestMonteCarlo:
    def test_mc_invoked_above_threshold(self):
        # 13 positions → enumeration would be 8192 combos; we set the
        # threshold at 12 so MC is used.
        positions = [_pos(f"t{i}") for i in range(13)]
        m = compute_tail_risk(
            positions, monte_carlo_threshold=12,
            monte_carlo_trials=5000, rng_seed=42,
        )
        assert m.method == "monte_carlo"
        # Worst case = 13 * 5 = 65.
        assert m.worst_case == pytest.approx(65.0)
        # VaR should be positive but not the worst case (very tiny tail).
        assert 0.0 < m.var_95 <= m.worst_case
        # CVaR ≥ VaR by definition.
        assert m.cvar_95 >= m.var_95

    def test_mc_deterministic_with_seed(self):
        positions = [_pos(f"t{i}") for i in range(13)]
        a = compute_tail_risk(positions, monte_carlo_threshold=12,
                              monte_carlo_trials=2000, rng_seed=7)
        b = compute_tail_risk(positions, monte_carlo_threshold=12,
                              monte_carlo_trials=2000, rng_seed=7)
        assert a.var_95 == b.var_95
        assert a.cvar_95 == b.cvar_95
