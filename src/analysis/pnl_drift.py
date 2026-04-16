"""
Realised-vs-predicted P&L drift detector.

The bot already stores per-trade *predictions* (signed ``edge`` and
``confidence``) at entry, and the realised PnL at exit, in the
``calibration`` table.  This module turns that history into a single
*drift verdict* the operator can read at a glance:

    realisation_ratio = (sum realised pnl) / (sum predicted pnl in $)

A healthy strategy converges to ``realisation_ratio ≈ 1.0`` after
enough samples.  A ratio that *drops over time* signals model decay
(the world has shifted under us) — exactly the symptom an operator
needs to catch *before* adding real capital.

We deliberately do NOT auto-pause trading on drift — the false-positive
risk on small samples is too high.  Instead we surface the metric on
the dashboard and (optionally) emit an alert when the ratio falls
below a configured floor with enough samples to back it up.

The function is pure: it takes a list of closed-trade dicts (the ones
:meth:`SQLiteStore.get_calibration_summary`-style queries already
return) and returns a :class:`DriftVerdict` — easy to unit-test and
trivial to wire from a dashboard route.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class DriftVerdict:
    """Outcome of a drift evaluation."""

    n_samples: int
    realised_pnl_usd: float
    predicted_pnl_usd: float
    realisation_ratio: float          # realised / predicted; 1.0 = match
    realised_win_rate: float          # fraction of closed trades with pnl > 0
    expected_win_rate: float          # mean(predicted_p) — the model's prior
    drift_detected: bool              # ratio below floor with enough samples
    reason: str                       # one-liner explanation


def _parse_features(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _predicted_dollars(entry_price: float, edge: float, size_shares: float) -> float:
    """Per-trade predicted dollar P&L from the edge model.

    ``edge`` is signed in price-units (e.g. +0.04 means we predict the
    fair value is 4¢ higher than the market).  Holding ``size_shares``
    of a binary YES, the *predicted* dollar P&L at resolution is::

        size * edge

    This is the obvious closed form: if the market moves ``edge``
    cents in our favour, we earn ``size * edge`` dollars.  We use
    absolute value because BUYs and SELLs have different sign
    conventions and we just want gross magnitude here.
    """
    return abs(float(edge)) * float(size_shares)


def detect_drift(
    closed_trades: list[dict],
    *,
    min_samples: int = 30,
    ratio_floor: float = 0.5,
) -> DriftVerdict:
    """Compute realisation ratio across the supplied closed trades.

    ``closed_trades`` must contain dicts with at least:

    * ``pnl``         — realised dollar P&L (signed)
    * ``confidence``  — model confidence at entry, 0..1
    * ``entry_price`` — fill price at entry
    * ``features``    — JSON-serialised feature dict (``{'edge': ...}``
                       when the strategy publishes a signed edge);
                       trades from non-edge strategies are skipped
                       from the predicted-PnL sum but still counted
                       toward the win-rate computation.

    Trades without a parseable edge contribute to win-rate but not
    to ``realisation_ratio`` — there's no model prediction to compare
    them against.  This keeps momentum / mean-reversion strategies
    from polluting the metric.

    Cold-start (``n_samples < min_samples``) is fail-safe: returns
    ``drift_detected=False`` regardless of the ratio.  This mirrors
    the same posture as the temporal and Bayesian features.
    """
    if not closed_trades:
        return DriftVerdict(
            n_samples=0, realised_pnl_usd=0.0, predicted_pnl_usd=0.0,
            realisation_ratio=0.0, realised_win_rate=0.0,
            expected_win_rate=0.0, drift_detected=False,
            reason="No closed trades yet.",
        )

    realised_total = 0.0
    predicted_total = 0.0
    confidence_sum = 0.0
    n_with_edge = 0
    n_total = 0
    n_winners = 0

    for t in closed_trades:
        pnl = t.get("pnl")
        if pnl is None:
            continue
        n_total += 1
        pnl = float(pnl)
        realised_total += pnl
        if pnl > 0:
            n_winners += 1
        confidence_sum += float(t.get("confidence") or 0.0)

        feats = _parse_features(t.get("features"))
        edge = feats.get("edge")
        # ``filled_size`` was added to features at ENTRY_BUY time.
        size = feats.get("filled_size") or feats.get("requested_size")
        if edge is not None and size is not None:
            try:
                predicted_total += _predicted_dollars(
                    float(t.get("entry_price") or 0.0),
                    float(edge), float(size),
                )
                n_with_edge += 1
            except (TypeError, ValueError):
                pass

    if n_total == 0:
        return DriftVerdict(
            n_samples=0, realised_pnl_usd=0.0, predicted_pnl_usd=0.0,
            realisation_ratio=0.0, realised_win_rate=0.0,
            expected_win_rate=0.0, drift_detected=False,
            reason="No closed trades had a recorded P&L.",
        )

    realised_win_rate = n_winners / n_total
    expected_win_rate = confidence_sum / n_total
    if predicted_total > 0:
        ratio = realised_total / predicted_total
    else:
        ratio = 0.0

    drift = (
        n_with_edge >= min_samples
        and predicted_total > 0
        and ratio < ratio_floor
    )
    if drift:
        reason = (
            f"Realisation ratio {ratio:+.2f} below floor {ratio_floor:+.2f} "
            f"over {n_with_edge} edge-tagged closed trades "
            f"(realised ${realised_total:+.2f}, predicted ${predicted_total:+.2f})."
        )
    elif n_with_edge < min_samples:
        reason = (
            f"Cold-start: only {n_with_edge} edge-tagged trades closed "
            f"(need {min_samples})."
        )
    else:
        reason = (
            f"Realisation ratio {ratio:+.2f} ≥ floor {ratio_floor:+.2f} "
            f"over {n_with_edge} edge-tagged closed trades."
        )

    return DriftVerdict(
        n_samples=n_total,
        realised_pnl_usd=round(realised_total, 4),
        predicted_pnl_usd=round(predicted_total, 4),
        realisation_ratio=round(ratio, 4),
        realised_win_rate=round(realised_win_rate, 4),
        expected_win_rate=round(expected_win_rate, 4),
        drift_detected=drift,
        reason=reason,
    )
