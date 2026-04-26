"""
Risk-adjusted performance metrics for strategy validation.

The functions here are deliberately *simple, dependency-free, and testable*.
They take a sequence of per-trade or per-period returns and produce
single-number summaries plus distributional / significance tests.  Use
them inside the walk-forward harness (``src/backtest/walk_forward.py``)
or directly from the CLI ``validate-strategy`` command — never inside
the live trading loop.

References
----------
* Bailey, D. & López de Prado, M. (2012). *The Sharpe Ratio Efficient
  Frontier*.  Defines Probabilistic and Deflated Sharpe ratios.
* López de Prado (2018). *Advances in Financial Machine Learning*,
  Ch. 7-9 — purged k-fold CV, walk-forward, and DSR.
* Politis & Romano (1994). *The stationary bootstrap*.  Block bootstrap
  for serially-correlated time series.

Conventions
-----------
* ``returns`` are *per-period* simple returns expressed as decimals
  (e.g. 0.012 = +1.2%).  Aggregation to annualized values uses the
  ``periods_per_year`` argument (defaults to 252 trading days).
* All routines treat empty / single-point inputs by returning 0.0 or a
  zero-filled namedtuple — never raise — so a calling pipeline that
  produces no closed trades does not crash the whole report.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence


# ---------------------------------------------------------------------------
# Basic moments (kept here, not in math_utils, so risk_metrics is
# self-contained and the function list is auditable from one file).
# ---------------------------------------------------------------------------


def _mean(xs: Sequence[float]) -> float:
    n = len(xs)
    if n == 0:
        return 0.0
    return sum(xs) / n


def _var(xs: Sequence[float], *, ddof: int = 1) -> float:
    n = len(xs)
    if n - ddof <= 0:
        return 0.0
    mu = _mean(xs)
    return sum((x - mu) ** 2 for x in xs) / (n - ddof)


def _std(xs: Sequence[float], *, ddof: int = 1) -> float:
    return math.sqrt(_var(xs, ddof=ddof))


def _skew(xs: Sequence[float]) -> float:
    """Sample skewness (Fisher-Pearson, population denominator).

    Returns 0.0 when there is too little data for a stable estimate
    (n < 3) or when the sample has zero variance — both edge cases the
    DSR / PSR formulas would otherwise divide by zero.
    """
    n = len(xs)
    if n < 3:
        return 0.0
    mu = _mean(xs)
    s = _std(xs, ddof=0)
    if s == 0:
        return 0.0
    return sum((x - mu) ** 3 for x in xs) / (n * s ** 3)


def _kurt(xs: Sequence[float]) -> float:
    """Excess kurtosis (Fisher).  Same n-floor rules as ``_skew``."""
    n = len(xs)
    if n < 4:
        return 0.0
    mu = _mean(xs)
    s = _std(xs, ddof=0)
    if s == 0:
        return 0.0
    return sum((x - mu) ** 4 for x in xs) / (n * s ** 4) - 3.0


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via ``math.erf`` — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Per-period summary statistics
# ---------------------------------------------------------------------------


@dataclass
class ReturnsSummary:
    n: int
    mean: float
    std: float
    skew: float
    excess_kurtosis: float
    min: float
    max: float
    sharpe_per_period: float
    sharpe_annualized: float
    sortino_annualized: float
    max_drawdown: float
    calmar: float


def returns_summary(
    returns: Sequence[float],
    *,
    periods_per_year: int = 252,
    risk_free_per_period: float = 0.0,
) -> ReturnsSummary:
    """Single-pass summary of a returns series.

    Annualization assumes IID returns (the standard simplifying
    assumption).  Real trade-PnL streams are autocorrelated, so callers
    that care about that should also run the block bootstrap and quote
    the bootstrap CI alongside the point estimate.
    """
    n = len(returns)
    if n == 0:
        return ReturnsSummary(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    excess = [r - risk_free_per_period for r in returns]
    mu = _mean(excess)
    sd = _std(excess, ddof=1)
    sk = _skew(excess)
    kurt = _kurt(excess)
    # ``sd > 0`` alone is unsafe: floating-point noise on a constant
    # series produces sd ≈ 1e-18 (not zero) and the Sharpe blows up to
    # ~1e16.  Guard with a numeric floor relative to the magnitude
    # of mu and the absolute scale.
    sd_floor = max(1e-12, 1e-9 * (abs(mu) + 1.0))
    sharpe_pp = mu / sd if sd > sd_floor else 0.0
    sharpe_an = sharpe_pp * math.sqrt(periods_per_year)
    # Sortino uses downside deviation
    downside = [min(0.0, x) for x in excess]
    dd_var = sum(d ** 2 for d in downside) / max(1, n - 1)
    dd = math.sqrt(dd_var)
    sortino_an = (mu / dd) * math.sqrt(periods_per_year) if dd > sd_floor else 0.0
    max_dd = max_drawdown(returns)
    # Calmar: annualized return / max DD (positive number).  Returns 0
    # when DD is zero (no losing streak yet) — avoids division blow-ups.
    annual_return = (1.0 + mu) ** periods_per_year - 1.0
    calmar = annual_return / max_dd if max_dd > 0 else 0.0
    return ReturnsSummary(
        n=n,
        mean=mu,
        std=sd,
        skew=sk,
        excess_kurtosis=kurt,
        min=min(returns),
        max=max(returns),
        sharpe_per_period=sharpe_pp,
        sharpe_annualized=sharpe_an,
        sortino_annualized=sortino_an,
        max_drawdown=max_dd,
        calmar=calmar,
    )


def max_drawdown(returns: Sequence[float]) -> float:
    """Maximum peak-to-trough drawdown of the cumulative-return curve.

    Returned as a positive fraction (e.g. 0.18 = 18 percent decline
    from the equity peak).  ``0.0`` on empty input or strictly
    monotonic-up curves.
    """
    if not returns:
        return 0.0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        cum += r
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ---------------------------------------------------------------------------
# Probabilistic / Deflated Sharpe Ratio (Bailey & López de Prado)
# ---------------------------------------------------------------------------


def probabilistic_sharpe_ratio(
    returns: Sequence[float],
    *,
    benchmark_sharpe: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Probability that the *true* (unobserved) Sharpe exceeds a benchmark.

    Implements PSR per Bailey & López de Prado (2012), correcting the
    naive Sharpe distribution for non-normal skew/kurtosis.  The
    ``benchmark_sharpe`` is in **annualized** units (e.g. 1.0 = a
    Sharpe of 1.0 we want to beat).

    Returns a probability in ``[0, 1]``.  Below ~0.95 the observation
    is not statistically distinguishable from the benchmark — typical
    promote-to-live thresholds are ≥0.95 *and* a deflated SR > 0.
    """
    n = len(returns)
    if n < 4:
        return 0.0
    summary = returns_summary(returns, periods_per_year=periods_per_year)
    sharpe_pp = summary.sharpe_per_period
    sk = summary.skew
    kurt = summary.excess_kurtosis
    # benchmark expressed in per-period units to match sharpe_pp
    bench_pp = benchmark_sharpe / math.sqrt(periods_per_year)
    # PSR = Phi( (SR - SR*) * sqrt(n - 1) / sqrt(1 - skew*SR + (kurt/4) * SR^2) )
    denom_inner = 1.0 - sk * sharpe_pp + (kurt / 4.0) * sharpe_pp ** 2
    if denom_inner <= 0:
        # Pathological sample: skew/kurtosis combo makes the variance
        # estimator non-positive.  Bail out rather than emit NaN.
        return 0.0
    z = (sharpe_pp - bench_pp) * math.sqrt(n - 1) / math.sqrt(denom_inner)
    return _norm_cdf(z)


def deflated_sharpe_ratio(
    returns: Sequence[float],
    *,
    n_trials: int,
    sharpe_estimates: Sequence[float] | None = None,
    periods_per_year: int = 252,
) -> float:
    """Deflated Sharpe Ratio: PSR adjusted for multiple-testing inflation.

    When you try ``n_trials`` configurations and pick the best, the
    expected best-Sharpe under the null is *not* zero but ~ √(2 ln n).
    DSR uses that expectation as the benchmark in the PSR formula, so
    a "winning" backtest must clear the bar that random search
    would meet by chance alone.

    Parameters
    ----------
    n_trials :
        Number of configurations / backtests evaluated to arrive at
        ``returns``.  This must include all the dropped attempts, not
        just the survivors — that's the whole point.
    sharpe_estimates :
        Optional collection of *per-period* Sharpe estimates from the
        full search.  When provided, the expected-best-Sharpe uses the
        observed cross-sectional std-dev of these estimates instead of
        the asymptotic 1.0; otherwise we fall back to López de Prado's
        formula with V[SR] = 1 (conservative).

    Returns
    -------
    Probability in ``[0, 1]`` that the strategy's *true* Sharpe exceeds
    the random-search benchmark.  ``≥ 0.95`` is the conventional
    "statistically valid" threshold.
    """
    if n_trials < 1 or len(returns) < 4:
        return 0.0
    # Expected maximum of N i.i.d. SR estimates under the null
    # (López de Prado 2018, Eq. 8.6):
    #   E[max SR] ≈ V[SR]^{1/2} * ((1 - γ) Φ⁻¹(1 - 1/N) + γ Φ⁻¹(1 - 1/(N e)))
    # where γ ≈ 0.5772 (Euler-Mascheroni).
    if sharpe_estimates is not None and len(sharpe_estimates) > 1:
        v_sr = _var(list(sharpe_estimates), ddof=1)
        v_sr = max(v_sr, 1e-12)
    else:
        v_sr = 1.0
    gamma = 0.5772156649  # Euler-Mascheroni
    # Stable inverse-normal via bisection on _norm_cdf — avoids importing
    # scipy.  Inputs are bounded in (0, 1) so this is well-behaved.
    z1 = _inv_norm_cdf(1.0 - 1.0 / n_trials)
    z2 = _inv_norm_cdf(1.0 - 1.0 / (n_trials * math.e))
    e_max = math.sqrt(v_sr) * ((1.0 - gamma) * z1 + gamma * z2)
    # e_max is per-period when v_sr was computed from per-period SRs
    # (which is the assumption).  Pass the *annualized* benchmark
    # accordingly.
    benchmark_annual = e_max * math.sqrt(periods_per_year)
    return probabilistic_sharpe_ratio(
        returns,
        benchmark_sharpe=benchmark_annual,
        periods_per_year=periods_per_year,
    )


def _inv_norm_cdf(p: float, *, tol: float = 1e-9) -> float:
    """Bisection-based inverse normal CDF for ``p ∈ (0, 1)``.

    Good enough for DSR (we only ever evaluate at p ∈ [0.99, 1.0)
    for realistic n_trials).  Avoids a heavy dependency.
    """
    p = max(min(p, 1.0 - 1e-15), 1e-15)
    lo, hi = -8.0, 8.0
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if _norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# Block bootstrap
# ---------------------------------------------------------------------------


@dataclass
class BootstrapCI:
    point: float
    lower: float
    upper: float
    n_resamples: int
    block_size: int
    confidence: float


def block_bootstrap_sharpe(
    returns: Sequence[float],
    *,
    n_resamples: int = 1000,
    block_size: int | None = None,
    confidence: float = 0.95,
    periods_per_year: int = 252,
    seed: int | None = 0,
) -> BootstrapCI:
    """Block-bootstrap CI for the annualized Sharpe ratio.

    Trade-PnL series have meaningful autocorrelation (e.g. consecutive
    losing trades during a drawdown), so a plain i.i.d. bootstrap
    underestimates the SR variance and produces over-confident CIs.
    Sampling **contiguous blocks** preserves short-range dependence.

    ``block_size`` defaults to ``ceil(n^{1/3})`` per Politis-White's
    rule of thumb when not supplied — works well for series in the
    100-1000 range we expect from realistic backtests.

    Returns a CI in *annualized* units.  Empty / tiny inputs degrade to
    ``BootstrapCI(0, 0, 0, …)`` rather than raising.
    """
    n = len(returns)
    point = returns_summary(returns, periods_per_year=periods_per_year).sharpe_annualized
    if n < 4:
        return BootstrapCI(point, point, point, 0, 0, confidence)
    if block_size is None:
        block_size = max(2, int(math.ceil(n ** (1.0 / 3.0))))
    block_size = min(block_size, n)
    rng = random.Random(seed)
    samples: list[float] = []
    n_blocks = math.ceil(n / block_size)
    for _ in range(n_resamples):
        resample: list[float] = []
        for _ in range(n_blocks):
            start = rng.randint(0, n - block_size)
            resample.extend(returns[start:start + block_size])
        # Trim to exact length so each resample is comparable.
        resample = resample[:n]
        samples.append(
            returns_summary(resample, periods_per_year=periods_per_year).sharpe_annualized,
        )
    samples.sort()
    alpha = 1.0 - confidence
    lo_idx = max(0, int(math.floor(alpha / 2 * n_resamples)))
    hi_idx = min(n_resamples - 1, int(math.ceil((1.0 - alpha / 2) * n_resamples)) - 1)
    return BootstrapCI(
        point=point,
        lower=samples[lo_idx],
        upper=samples[hi_idx],
        n_resamples=n_resamples,
        block_size=block_size,
        confidence=confidence,
    )
