"""Multiple-testing corrections for experiment selection.

The experiment runner sweeps many strategy variants in one go.  Reading
each one's win-rate and picking the "significant" ones at α = 0.05
without a correction is exactly the data-snooping trap the audit
flagged: with k = 20 independent tests, ~1 in 20 is "significant" by
chance even when no variant is genuinely better than coin-flipping.

This module provides:

* :func:`binomial_p_one_sided` — exact one-sided p-value for
  ``X >= wins`` under H0: win-rate = 0.5.  Pure Python via ``math.comb``.
* :func:`benjamini_hochberg` — BH-FDR procedure: returns adjusted
  q-values and a boolean mask of "rejected at FDR ≤ q*".

We deliberately do *not* use Bonferroni — at the typical k we sweep
(5–50 variants) Bonferroni is too conservative and rejects almost all
true findings.  BH controls the *false discovery rate* and is the
standard choice for strategy-selection workflows.
"""

from __future__ import annotations

import math


def binomial_p_one_sided(wins: int, n: int, p0: float = 0.5) -> float:
    """One-sided p-value for the test  H0: p = p0  vs.  H1: p > p0.

    Returns ``P(X >= wins | n, p0)`` under the binomial null.  Edge
    cases:

    * ``n <= 0`` → returns ``1.0`` (no evidence to reject).
    * ``wins <= 0`` → returns ``1.0`` (always trivially as-extreme-as).
    * ``wins > n`` → returns ``0.0`` (impossible-strict bound).

    Exact, no normal approximation; ``math.comb`` makes this fast even
    at n in the thousands.
    """
    if n <= 0 or wins <= 0:
        return 1.0
    if wins > n:
        return 0.0
    p = max(0.0, min(1.0, p0))
    q = 1.0 - p
    total = 0.0
    for k in range(int(wins), int(n) + 1):
        total += math.comb(n, k) * (p ** k) * (q ** (n - k))
    # Numerical safety: clamp to [0, 1].
    return max(0.0, min(1.0, total))


def benjamini_hochberg(pvalues: list[float], fdr: float = 0.05) -> dict:
    """Benjamini-Hochberg FDR procedure.

    Parameters
    ----------
    pvalues
        List of raw p-values (one per hypothesis / experiment).
    fdr
        Target false-discovery rate.  Default 0.05.

    Returns
    -------
    dict with:
        * ``order`` — indices of inputs sorted ascending by p-value,
        * ``adjusted`` — list of BH-adjusted q-values aligned to the
          original input order,
        * ``rejected`` — boolean list aligned to the original input
          order; True means "reject H0 at the given FDR".

    Implementation note: BH defines the largest k such that
    ``p_(k) <= (k/m) * α`` and rejects all hypotheses with
    p ≤ that threshold.  Adjusted p-values use the standard step-up
    formulation::

        q_i = min over j>=i of  (m / j) * p_(j)

    so q-values are monotonically non-decreasing in rank.
    """
    if not pvalues:
        return {"order": [], "adjusted": [], "rejected": []}
    m = len(pvalues)
    indexed = sorted(range(m), key=lambda i: pvalues[i])
    sorted_p = [pvalues[i] for i in indexed]

    # Step-up adjusted q-values: traverse from the largest rank back.
    adjusted_sorted: list[float] = [0.0] * m
    running_min = 1.0
    for rank in range(m, 0, -1):
        idx = rank - 1
        q = (m / rank) * sorted_p[idx]
        running_min = min(running_min, q)
        adjusted_sorted[idx] = max(0.0, min(1.0, running_min))

    # Map adjusted values back to the original input order.
    adjusted = [0.0] * m
    for sorted_i, original_i in enumerate(indexed):
        adjusted[original_i] = adjusted_sorted[sorted_i]

    # Rejection: standard BH thresholding (largest k with
    # p_(k) ≤ (k/m) * α), then everything at or below that rank rejects.
    cutoff_rank = 0
    for rank in range(m, 0, -1):
        idx = rank - 1
        if sorted_p[idx] <= (rank / m) * fdr:
            cutoff_rank = rank
            break
    rejected_sorted = [r <= cutoff_rank for r in range(1, m + 1)]
    rejected = [False] * m
    for sorted_i, original_i in enumerate(indexed):
        rejected[original_i] = rejected_sorted[sorted_i]

    return {
        "order": indexed,
        "adjusted": adjusted,
        "rejected": rejected,
    }
