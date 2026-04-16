"""
Portfolio tail-risk analytics — Value-at-Risk, Conditional VaR, and
full-loss stress scenario for the open positions.

Why custom math (and not a pandas/numpy black-box)?
---------------------------------------------------
Polymarket outcomes are *binary*: at resolution each YES share pays
either $1 or $0.  That means the bot's aggregate P&L across N open
positions lives on a discrete, closed support (it's the sum of N
shifted Bernoulli variables weighted by position size).  With
``size_i`` shares bought at ``entry_i``, the loss contribution of
position ``i`` is::

    loss_i = entry_i * size_i      if the market resolves against us
    loss_i = -(1 - entry_i) * size_i  if it resolves in our favour

For small portfolios (the N=1..20 case that's default-bounded by
``MAX_OPEN_POSITIONS=5``) we can enumerate every 2^N outcome exactly.
For larger portfolios we fall back to a Monte-Carlo estimator using
the market's own price as the probability of YES resolving (our
best *unbiased* prior — the model edge isn't a true probability,
it's what we bet *against* the market).  Both paths return the
same quantities:

* ``var_95``        — 95% VaR, i.e. the loss level breached in ≤ 5%
                     of scenarios (a POSITIVE number = dollars lost).
* ``cvar_95``      — Conditional VaR (a.k.a. Expected Shortfall):
                     the average loss *given* the 5% worst tail —
                     always ≥ var_95.
* ``worst_case``    — the maximum possible loss across all
                     scenarios (every position resolves against us,
                     subject to sign/side).
* ``expected_loss`` — the portfolio's expected P&L (a useful sanity
                     check: should be close to ``sum_i edge_i`` in
                     dollars if the strategy is actually +EV).

Cold-start / empty portfolio
----------------------------
With zero open positions everything is 0.  With one position the
enumeration is trivial (two outcomes).  None of the metrics depend
on trade history — only on the current snapshot.
"""

from __future__ import annotations

import itertools
import logging
import random
from dataclasses import dataclass
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskMetrics:
    var_95: float
    cvar_95: float
    worst_case: float
    expected_loss: float
    n_positions: int
    method: str  # "exact" | "monte_carlo" | "empty"


def _position_loss_if_lose(entry_price: float, size: float, side: str) -> float:
    """Dollars lost if the market resolves against this position.

    For a BUY (long YES): loses the premium = entry_price * size.
    For a SELL (short YES): loses (1 - entry_price) * size (the max
    payoff gap when the short goes in-the-money).
    """
    if side == "BUY":
        return entry_price * size
    return max(0.0, (1.0 - entry_price) * size)


def _position_gain_if_win(entry_price: float, size: float, side: str) -> float:
    """Dollars gained if the market resolves in our favour."""
    if side == "BUY":
        return max(0.0, (1.0 - entry_price) * size)
    return entry_price * size


def compute_tail_risk(
    positions: Iterable,
    price_fn: Callable[[str], float | None] | None = None,
    *,
    confidence: float = 0.95,
    monte_carlo_threshold: int = 12,
    monte_carlo_trials: int = 20_000,
    rng_seed: int | None = None,
) -> RiskMetrics:
    """Compute VaR/CVaR/worst-case for the given open positions.

    ``positions`` is an iterable of :class:`src.portfolio.tracker.Position`
    objects.  ``price_fn`` maps ``token_id -> current market price``
    and is used as the Bernoulli probability for each position's
    favourable outcome.  When ``price_fn`` returns None (or isn't
    supplied at all), we fall back to the position's ``entry_price``
    — i.e. "assume the market is efficient at entry".

    The function is deterministic when ``rng_seed`` is provided, so
    the tick stats keep a stable number across re-ticks without
    jitter noise.
    """
    pos_list = list(positions)
    n = len(pos_list)
    if n == 0:
        return RiskMetrics(0.0, 0.0, 0.0, 0.0, 0, "empty")

    # Per-position (loss_if_lose, gain_if_win, p_win)
    triples = []
    for p in pos_list:
        loss_amt = _position_loss_if_lose(p.entry_price, p.size, p.side)
        gain_amt = _position_gain_if_win(p.entry_price, p.size, p.side)
        # p_win is the probability our side resolves favourably.  The
        # market's current price is our best unbiased estimator:
        # for a long BUY, p_win = P(YES) = market price; for a short
        # SELL, p_win = P(NO) = 1 - market price.
        mkt = None
        if price_fn is not None:
            try:
                mkt = price_fn(p.token_id)
            except Exception:
                mkt = None
        mkt = mkt if mkt is not None else p.entry_price
        mkt = min(max(float(mkt), 0.0), 1.0)
        p_win = mkt if p.side == "BUY" else (1.0 - mkt)
        triples.append((loss_amt, gain_amt, p_win))

    worst_case = sum(t[0] for t in triples)
    expected_loss = -sum(t[1] * t[2] - t[0] * (1 - t[2]) for t in triples)

    if n <= monte_carlo_threshold:
        # Exact enumeration of all 2^n outcomes.
        losses = []
        weights = []
        for bits in itertools.product((0, 1), repeat=n):
            # bit=1 means WIN for that position, 0 means LOSE.
            loss = 0.0
            w = 1.0
            for (loss_amt, gain_amt, p_win), b in zip(triples, bits):
                if b == 1:
                    loss -= gain_amt
                    w *= p_win
                else:
                    loss += loss_amt
                    w *= (1.0 - p_win)
            losses.append(loss)
            weights.append(w)
        method = "exact"
    else:
        # Monte Carlo — independent samples using each position's
        # p_win.  Independence is a simplification: in reality
        # correlated markets exist, but the higher-level
        # ``MAX_EXPOSURE_PER_CATEGORY`` cap is what bounds that.
        rng = random.Random(rng_seed)
        losses = []
        weights = [1.0 / monte_carlo_trials] * monte_carlo_trials
        for _ in range(monte_carlo_trials):
            loss = 0.0
            for loss_amt, gain_amt, p_win in triples:
                if rng.random() < p_win:
                    loss -= gain_amt
                else:
                    loss += loss_amt
            losses.append(loss)
        method = "monte_carlo"

    # Sort by loss ascending.  VaR = smallest loss L such that
    # P(loss >= L) <= 1 - confidence.  Equivalent: the (1-confidence)
    # quantile of the loss distribution from the TOP.
    paired = sorted(zip(losses, weights), key=lambda x: x[0])
    # Walk from the worst-loss end, accumulating probability.
    tail_p = 1.0 - confidence
    cum = 0.0
    var = paired[-1][0]
    for loss_val, w in reversed(paired):
        cum += w
        if cum >= tail_p:
            var = loss_val
            break
    # CVaR = expected loss *given* we're in the worst tail_p mass.
    cum = 0.0
    cvar_num = 0.0
    cvar_den = 0.0
    for loss_val, w in reversed(paired):
        take = min(w, tail_p - cum) if cum < tail_p else 0.0
        if take <= 0.0 and cum >= tail_p:
            break
        cvar_num += loss_val * take
        cvar_den += take
        cum += take
    cvar = (cvar_num / cvar_den) if cvar_den > 0 else var

    # VaR/CVaR convention: positive number = dollars lost.  Convert
    # from signed P&L (where negative = loss) before returning.
    return RiskMetrics(
        var_95=max(0.0, var),
        cvar_95=max(0.0, cvar),
        worst_case=worst_case,
        expected_loss=expected_loss,
        n_positions=n,
        method=method,
    )
