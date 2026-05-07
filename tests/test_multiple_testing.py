"""Tests for src.analysis.multiple_testing."""

from __future__ import annotations

import math

import pytest

from src.analysis.multiple_testing import benjamini_hochberg, binomial_p_one_sided


# -- Binomial p-value -------------------------------------------------------

class TestBinomialP:
    def test_zero_trials_returns_one(self):
        assert binomial_p_one_sided(wins=0, n=0) == 1.0

    def test_zero_wins_returns_one(self):
        assert binomial_p_one_sided(wins=0, n=10) == 1.0

    def test_more_wins_than_trials_returns_zero(self):
        assert binomial_p_one_sided(wins=11, n=10) == 0.0

    def test_all_wins_is_minimal_p(self):
        # P(X >= 10 | n=10, p=0.5) = (0.5)^10 ≈ 0.0009765625.
        assert binomial_p_one_sided(wins=10, n=10) == pytest.approx(0.5 ** 10)

    def test_half_wins_is_around_one_half(self):
        # Symmetry: P(X >= n/2 | n=10, p=0.5) ≈ 0.623 (includes the median).
        p = binomial_p_one_sided(wins=5, n=10)
        assert 0.5 < p <= 0.65

    def test_returns_in_unit_interval(self):
        for wins in range(0, 21):
            p = binomial_p_one_sided(wins=wins, n=20)
            assert 0.0 <= p <= 1.0


# -- BH-FDR -----------------------------------------------------------------

class TestBenjaminiHochberg:
    def test_empty_returns_empty(self):
        out = benjamini_hochberg([])
        assert out == {"order": [], "adjusted": [], "rejected": []}

    def test_all_significant_when_p_tiny(self):
        out = benjamini_hochberg([0.001, 0.002, 0.003], fdr=0.05)
        assert all(out["rejected"])
        # Adjusted q's bounded by 1.0
        assert all(0 <= q <= 1 for q in out["adjusted"])

    def test_none_significant_when_p_large(self):
        out = benjamini_hochberg([0.5, 0.6, 0.7, 0.8], fdr=0.05)
        assert not any(out["rejected"])

    def test_partial_significance(self):
        # 5 hypotheses; p = [0.001, 0.01, 0.04, 0.5, 0.9]
        # BH thresholds at α=0.05: rank k → (k/5) * 0.05
        # k=1: 0.01; k=2: 0.02; k=3: 0.03; k=4: 0.04; k=5: 0.05
        # Sorted p:  [0.001, 0.01, 0.04, 0.5, 0.9]
        # The largest k with p_(k) ≤ k/5*0.05 is... none of them
        # (k=1: 0.001 ≤ 0.01 ✓; k=2: 0.01 ≤ 0.02 ✓; k=3: 0.04 > 0.03;
        #  k=4: 0.5 > 0.04; k=5: 0.9 > 0.05) — so cutoff = k=2.
        out = benjamini_hochberg([0.001, 0.01, 0.04, 0.5, 0.9], fdr=0.05)
        # The first two p-values pass; the rest don't.
        assert out["rejected"][0] is True
        assert out["rejected"][1] is True
        assert out["rejected"][2] is False
        assert out["rejected"][3] is False
        assert out["rejected"][4] is False

    def test_adjusted_monotonic_after_sort(self):
        pvals = [0.04, 0.001, 0.5, 0.01, 0.9]
        out = benjamini_hochberg(pvals, fdr=0.05)
        sorted_pairs = sorted(
            zip(pvals, out["adjusted"]), key=lambda pq: pq[0]
        )
        sorted_q = [q for _, q in sorted_pairs]
        for prev, cur in zip(sorted_q, sorted_q[1:]):
            assert prev <= cur + 1e-12  # non-decreasing

    def test_input_order_preserved(self):
        out = benjamini_hochberg([0.5, 0.001, 0.9], fdr=0.05)
        # Index 1 had the smallest p; rejected list still indexed by
        # original input order.
        assert out["rejected"][1] is True
        assert out["rejected"][0] is False
        assert out["rejected"][2] is False
