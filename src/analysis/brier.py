"""Brier score computation with temporal windows and bootstrap CIs.

The existing :mod:`src.analysis.calibration` module aggregates win-rate
into confidence buckets — useful but coarse.  This module adds a
single scalar that summarises calibration quality (Brier score), splits
it across a *recent* vs *historical* window so we can detect drift, and
attaches non-parametric confidence intervals via bootstrap resampling.

Brier = mean((p - y)^2), with p = predicted probability (confidence),
y = realised outcome ∈ {0, 1}.  Lower is better; a perfectly calibrated
random predictor at p=0.5 scores 0.25, and a perfect predictor scores 0.

Pure-Python implementation, no numpy dependency.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class BrierStats:
    n: int
    brier: float
    # Bootstrap-derived 95% confidence interval bounds for the mean Brier.
    # Both NaN if n < 2 or n_resamples == 0.
    ci_low: float
    ci_high: float


@dataclass
class TemporalBrier:
    recent: BrierStats   # last ``recent_days`` days
    historical: BrierStats   # full window
    delta: float   # recent.brier - historical.brier (positive = drift worse)
    drift_significant: bool  # recent CI does not overlap historical


def _outcome(row: dict) -> int:
    """Binary outcome: 1 if win (return_pct > 0), 0 otherwise."""
    return 1 if (row.get("return_pct") or 0.0) > 0 else 0


def _confidence(row: dict) -> float:
    c = row.get("confidence")
    if c is None:
        return 0.5  # neutral prior when missing
    return max(0.0, min(1.0, float(c)))


def compute_brier(rows: list[dict]) -> float:
    """Mean Brier score over rows.  Returns NaN on empty input."""
    if not rows:
        return float("nan")
    return sum((_confidence(r) - _outcome(r)) ** 2 for r in rows) / len(rows)


def bootstrap_brier_ci(
    rows: list[dict],
    n_resamples: int = 1000,
    alpha: float = 0.05,
    seed: int | None = None,
) -> tuple[float, float]:
    """Bootstrap CI for the mean Brier score.

    Returns ``(ci_low, ci_high)`` such that
    ``P(brier ∈ [ci_low, ci_high]) ≈ 1 - alpha``.  Returns ``(nan, nan)``
    if there are fewer than two observations.

    Uses the percentile bootstrap — robust enough for reporting; for
    formal hypothesis testing one would prefer a BCa or studentised
    bootstrap, but the marginal accuracy is overkill at the sample
    sizes we typically see here.
    """
    n = len(rows)
    if n < 2 or n_resamples <= 0:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    samples: list[float] = []
    n_rows = len(rows)
    for _ in range(n_resamples):
        # Sample with replacement.
        resampled = [rows[rng.randrange(n_rows)] for _ in range(n_rows)]
        samples.append(compute_brier(resampled))
    samples.sort()
    lo_idx = max(0, int(math.floor((alpha / 2) * n_resamples)))
    hi_idx = min(n_resamples - 1, int(math.ceil((1 - alpha / 2) * n_resamples)) - 1)
    return samples[lo_idx], samples[hi_idx]


def brier_stats(
    rows: list[dict],
    n_resamples: int = 1000,
    alpha: float = 0.05,
    seed: int | None = None,
) -> BrierStats:
    if not rows:
        return BrierStats(n=0, brier=float("nan"),
                          ci_low=float("nan"), ci_high=float("nan"))
    brier = compute_brier(rows)
    lo, hi = bootstrap_brier_ci(rows, n_resamples=n_resamples, alpha=alpha, seed=seed)
    return BrierStats(n=len(rows), brier=brier, ci_low=lo, ci_high=hi)


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    # Accept both ``2026-04-08T12:34:56`` and ``...+00:00``.
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def temporal_brier(
    rows: list[dict],
    recent_days: int = 7,
    *,
    now: datetime | None = None,
    n_resamples: int = 1000,
    seed: int | None = None,
) -> TemporalBrier:
    """Split rows into recent vs historical and compute Brier on each.

    ``rows`` must include an ``exit_timestamp`` field (ISO 8601).  Rows
    without one fall into the historical window (we treat undated entries
    as "old" — they're definitely not "recent").

    ``drift_significant`` is True iff the recent 95% CI sits entirely
    above the historical 95% CI — i.e. recent calibration is reliably
    worse.  This is a conservative, non-parametric check that doesn't
    assume normality.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=recent_days)
    recent: list[dict] = []
    historical: list[dict] = []  # older than cutoff (mutually exclusive
                                  # with ``recent`` so drift comparisons
                                  # are honest — see test_drift_detected*)
    for r in rows:
        ts = _parse_iso(r.get("exit_timestamp", "") or "")
        if ts is None:
            historical.append(r)
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            recent.append(r)
        else:
            historical.append(r)
    rs = brier_stats(recent, n_resamples=n_resamples, seed=seed)
    hs = brier_stats(historical, n_resamples=n_resamples, seed=seed)
    if math.isnan(rs.brier) or math.isnan(hs.brier):
        return TemporalBrier(recent=rs, historical=hs, delta=float("nan"),
                             drift_significant=False)
    delta = rs.brier - hs.brier
    drift = (
        not math.isnan(rs.ci_low) and not math.isnan(hs.ci_high)
        and rs.ci_low > hs.ci_high
    )
    return TemporalBrier(recent=rs, historical=hs, delta=delta,
                         drift_significant=drift)
