"""
Bayesian calibration — tracks a Beta(α, β) posterior over the realised
win-rate of each trading strategy and exposes it as a cautious sizing
multiplier.

Why Beta(α, β)?
---------------
The natural prior on a win-rate (a Bernoulli success probability) is
Beta(α, β).  Conjugacy makes online updates trivial:

    new_α = α + 1   (on a win)
    new_β = β + 1   (on a loss)

Posterior mean = α / (α + β) = "our best estimate of this strategy's
true win-rate, given evidence so far".  With a uniform Beta(1, 1)
prior and zero trades, the mean is 0.5 — we know nothing.

How is the posterior used?
--------------------------
Opt-in: ``BAYESIAN_SIZING_ENABLED=true`` causes
:class:`BayesianCalibrator.size_multiplier` to return

    * ``1.0`` until ``n_trades >= BAYESIAN_MIN_SAMPLES`` (cold-start
      fail-safe — we don't downweight before there's evidence),
    * otherwise ``clamp(posterior_mean, min_mult, 1.0)``.

The multiplier is deliberately asymmetric: a strategy can be sized
*down* by a losing track record, but never *up* past baseline.  We
let the Kelly / confidence / edge machinery set the upper bound and
let Bayes only *remove* risk — the opposite asymmetry would be
dangerous (compounding leverage on noisy winning streaks).

Cold-start posture
------------------
No data → multiplier 1.0 (transparent no-op).  Fewer than
``min_samples`` updates → still 1.0.  Once the threshold is crossed
the posterior starts biting — gradually, because the mean moves
slowly once α+β is large.  This matches the broader "observe before
gating" posture from ``CLAUDE.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


@dataclass
class Posterior:
    """A single strategy's Beta posterior + sample count."""

    strategy: str
    alpha: float
    beta: float
    n_trades: int

    @property
    def mean(self) -> float:
        denom = self.alpha + self.beta
        if denom <= 0:
            return 0.5
        return self.alpha / denom


class BayesianCalibrator:
    """Per-strategy Beta posteriors with online updates.

    The calibrator is purely additive: enabling it has no effect on
    sizing unless ``BAYESIAN_SIZING_ENABLED=true`` is *also* set.
    Recording still happens so the posterior accrues, giving the
    operator a week or two of shadow data to review before turning
    the multiplier on.

    Persistence is optional — pass a storage object with
    ``upsert_bayesian_posterior`` / ``get_all_bayesian_posteriors`` /
    ``get_bayesian_posterior`` to survive restarts.  A pure in-memory
    calibrator (store=None) is handy for tests.
    """

    def __init__(
        self,
        store=None,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
        min_samples: int = 30,
        min_multiplier: float = 0.3,
    ) -> None:
        self._store = store
        self._prior_alpha = float(prior_alpha)
        self._prior_beta = float(prior_beta)
        self._min_samples = int(min_samples)
        self._min_mult = float(min_multiplier)
        self._posteriors: dict[str, Posterior] = {}
        if store is not None:
            try:
                for row in store.get_all_bayesian_posteriors() or []:
                    self._posteriors[row["strategy"]] = Posterior(
                        strategy=row["strategy"],
                        alpha=float(row["alpha"]),
                        beta=float(row["beta"]),
                        n_trades=int(row["n_trades"] or 0),
                    )
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to load bayesian posteriors.")

    def _ensure(self, strategy: str) -> Posterior:
        p = self._posteriors.get(strategy)
        if p is None:
            p = Posterior(
                strategy=strategy,
                alpha=self._prior_alpha,
                beta=self._prior_beta,
                n_trades=0,
            )
            self._posteriors[strategy] = p
        return p

    def record_outcome(self, strategy: str, won: bool) -> Posterior:
        """Apply a single trade outcome to the posterior.

        A ``pnl > 0`` is a win; exact zero or negative is a loss.  The
        caller decides ("win" = positive PnL on a closed position).
        """
        if not strategy:
            return self._ensure("_unknown_")
        p = self._ensure(strategy)
        if won:
            p.alpha += 1.0
        else:
            p.beta += 1.0
        p.n_trades += 1
        if self._store is not None:
            try:
                self._store.upsert_bayesian_posterior(
                    strategy=p.strategy,
                    alpha=p.alpha,
                    beta=p.beta,
                    n_trades=p.n_trades,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                logger.exception(
                    "Failed to persist bayesian posterior for %s.", strategy,
                )
        return p

    def posterior(self, strategy: str) -> Posterior:
        """Return the current posterior for ``strategy`` (may be the prior)."""
        return self._ensure(strategy)

    def size_multiplier(self, strategy: str) -> float:
        """Sizing multiplier in ``[min_mult, 1.0]``.

        Cold-start (``n_trades < min_samples``) returns 1.0 — we
        don't penalise a strategy before enough evidence exists to
        distinguish it from the prior.  Once enough trades have
        accrued, the posterior mean is used directly (clamped to the
        floor so a terrible stretch never fully silences the
        strategy — it still gets a chance to recover evidence).
        """
        p = self._ensure(strategy)
        if p.n_trades < self._min_samples:
            return 1.0
        return min(max(p.mean, self._min_mult), 1.0)

    def seed_from_closed_trades(self, closed_rows: Iterable[dict]) -> None:
        """Replay closed calibration rows into the posteriors.

        Idempotent-*ish*: clears existing posteriors first, then
        rebuilds.  Suitable for a one-shot initialisation from
        ``SQLiteStore.get_calibration_closed`` when the
        ``bayesian_posterior`` table is empty (first run on a DB that
        already has trade history).  After seeding, callers should
        persist with :meth:`_persist_all`.
        """
        self._posteriors.clear()
        for row in closed_rows:
            strategy = row.get("strategy") or ""
            if not strategy:
                continue
            pnl = row.get("pnl")
            if pnl is None:
                continue
            try:
                self.record_outcome(strategy, won=float(pnl) > 0.0)
            except (TypeError, ValueError):
                continue
