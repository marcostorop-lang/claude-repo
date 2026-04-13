"""Synthetic fair-price engine.

Given a target market and a set of :class:`MarketRelation` edges to siblings,
build an estimate of where the target *should* trade.  Each relation kind
drives its own formula — we never mix kinds arbitrarily.  If the best we
can do is a band (no point estimate possible), we say so via
:attr:`SyntheticPrice.is_range_only`.

Design principles:

* **Traceable**: every estimate records ``method``, ``contributors``, and
  ``n_contributors``.  A downstream operator can always reconstruct how
  the number was built.
* **Conservative**: a missing sibling price or a stale spread makes the
  sibling *drop out* of the aggregation, never silently substituted with
  a guess.  If every sibling drops out we emit ``None`` and the engine
  emits no signal for this target.
* **Confidence-aware**: weights blend relation confidence and sibling
  liquidity, so a barely-related sibling in a thin book cannot dominate
  a strong relation on a deep one.
"""

from __future__ import annotations

import logging
from typing import Mapping, Sequence

from src.analysis.semantic_engine.types import (
    MarketRelation,
    RelationKind,
    SyntheticPrice,
)
from src.polymarket.market_data import MarketSnapshot

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------

def estimate_fair_price(
    target: MarketSnapshot,
    relations: Sequence[MarketRelation],
    snapshots_by_token: Mapping[str, MarketSnapshot],
    min_sibling_liquidity: float = 0.0,
) -> SyntheticPrice | None:
    """Return the best synthetic fair price for ``target`` or ``None``.

    Strategy:

    1. If we have *any* structural relations (MUTUALLY_EXCLUSIVE / NEG_RISK /
       INVERSE) use ONLY those — structural always wins.  Sum the sibling
       prices and complement.
    2. Else if we have textual EQUIVALENT/NEAR_EQUIVALENT — weighted average
       of sibling prices.
    3. Else if we have only TEMPORAL_CHECKPOINT — emit a range, not a point.
    4. Otherwise → ``None``.
    """
    if not relations:
        return None

    siblings_with_prices: dict[str, tuple[MarketRelation, MarketSnapshot]] = {}
    for rel in relations:
        snap = snapshots_by_token.get(rel.sibling_token_id)
        if snap is None:
            continue
        if snap.price is None:
            continue
        if min_sibling_liquidity > 0 and (snap.liquidity or 0) < min_sibling_liquidity:
            continue
        # If the same sibling appears via two relation kinds keep the
        # strongest structural; otherwise keep the first-seen textual.
        prior = siblings_with_prices.get(rel.sibling_token_id)
        if prior is None or _is_stronger(rel.kind, prior[0].kind):
            siblings_with_prices[rel.sibling_token_id] = (rel, snap)

    if not siblings_with_prices:
        return None

    structural = [x for x in siblings_with_prices.values() if x[0].kind.is_structural]
    if structural:
        return _estimate_structural(target, structural)

    textual_eq = [
        x for x in siblings_with_prices.values()
        if x[0].kind in (RelationKind.EQUIVALENT, RelationKind.NEAR_EQUIVALENT)
    ]
    if textual_eq:
        return _estimate_textual_equivalent(target, textual_eq)

    temporal = [
        x for x in siblings_with_prices.values()
        if x[0].kind == RelationKind.TEMPORAL_CHECKPOINT
    ]
    if temporal:
        return _estimate_temporal_range(target, temporal)

    return None


# --------------------------------------------------------------------------
# Structural estimator
# --------------------------------------------------------------------------

def _estimate_structural(
    target: MarketSnapshot,
    pairs: list[tuple[MarketRelation, MarketSnapshot]],
) -> SyntheticPrice | None:
    """Exclusive-outcome complement: ``fair = 1 − Σ(sibling_prices)``.

    INVERSE_OUTCOME is just the two-token case, but we handle it in the
    same formula.  Confidence is the *lowest* sibling confidence (weakest
    link) * a count bonus because redundancy helps.
    """
    if not pairs:
        return None

    total = 0.0
    contributors: list[tuple[str, float]] = []  # (token_id, weight for sort)
    min_conf = 1.0
    for rel, snap in pairs:
        if snap.price is None:
            continue
        # Clamp into [0, 1] — Polymarket quotes can drift to the rails.
        total += max(0.0, min(1.0, float(snap.price)))
        contributors.append((snap.token_id, float(rel.confidence)))
        min_conf = min(min_conf, float(rel.confidence))

    if not contributors:
        return None

    point = 1.0 - total
    # Small count bonus.  With n=1 we rely on a single sibling; with n>=3
    # the estimate is much more robust.  Cap at +0.10 so we don't overstate.
    count_bonus = min(0.10, 0.03 * (len(contributors) - 1))
    confidence = min(0.99, max(0.0, min_conf + count_bonus))

    # Structural estimates get a narrow tolerance band — the underlying
    # identity (Σ = 1) is hard.  Band widens with number of legs because
    # each leg's bid/ask noise compounds.
    band = max(0.01, 0.005 * len(contributors))
    lower = max(0.0, point - band)
    upper = min(1.0, point + band)

    contributors.sort(key=lambda x: -x[1])
    return SyntheticPrice(
        point=round(max(0.0, min(1.0, point)), 6),
        lower=round(lower, 6),
        upper=round(upper, 6),
        confidence=round(confidence, 4),
        method="structural_complement",
        contributors=tuple(tid for tid, _ in contributors),
        n_contributors=len(contributors),
        is_range_only=False,
    )


# --------------------------------------------------------------------------
# Textual equivalent estimator
# --------------------------------------------------------------------------

def _estimate_textual_equivalent(
    target: MarketSnapshot,
    pairs: list[tuple[MarketRelation, MarketSnapshot]],
) -> SyntheticPrice | None:
    """Weighted average of sibling prices (weight = rel.confidence × log1p(liquidity)).

    Liquidity is log-scaled so a $1M market doesn't dwarf a $10k one beyond
    reason.  Band is wider than structural because textual equivalence can
    be wrong — we embed that uncertainty directly in ``upper - lower``.
    """
    import math

    total_weight = 0.0
    weighted_sum = 0.0
    weights: list[tuple[str, float, float]] = []  # (token_id, price, weight)
    min_conf = 1.0

    for rel, snap in pairs:
        if snap.price is None:
            continue
        price = max(0.0, min(1.0, float(snap.price)))
        # log1p(liquidity): $100 ~ 4.6, $10k ~ 9.2, $1M ~ 13.8 — manageable spread.
        liq_w = math.log1p(max(0.0, float(snap.liquidity)))
        w = float(rel.confidence) * max(liq_w, 1.0)
        if w <= 0:
            continue
        weighted_sum += price * w
        total_weight += w
        weights.append((snap.token_id, price, w))
        min_conf = min(min_conf, float(rel.confidence))

    if not weights or total_weight <= 0:
        return None

    point = weighted_sum / total_weight

    # Dispersion penalty: if sibling prices disagree sharply, widen the band
    # and lower the confidence.  Operators shouldn't chase a mean that
    # doesn't reflect a converging opinion.
    mean = point
    var = sum(((p - mean) ** 2) * w for _, p, w in weights) / total_weight
    stdev = math.sqrt(max(0.0, var))

    band = max(0.02, 2.0 * stdev)
    lower = max(0.0, point - band)
    upper = min(1.0, point + band)

    # Confidence penalised by dispersion and number of contributors.
    conf = min_conf * max(0.4, 1.0 - 2.0 * stdev)
    if len(weights) == 1:
        conf *= 0.80  # Single-sibling equivalent is fragile.
    conf = max(0.0, min(0.95, conf))

    weights.sort(key=lambda x: -x[2])
    return SyntheticPrice(
        point=round(max(0.0, min(1.0, point)), 6),
        lower=round(lower, 6),
        upper=round(upper, 6),
        confidence=round(conf, 4),
        method="weighted_avg_equivalent",
        contributors=tuple(tid for tid, _, _ in weights),
        n_contributors=len(weights),
        is_range_only=False,
    )


# --------------------------------------------------------------------------
# Temporal range estimator
# --------------------------------------------------------------------------

def _estimate_temporal_range(
    target: MarketSnapshot,
    pairs: list[tuple[MarketRelation, MarketSnapshot]],
) -> SyntheticPrice | None:
    """Range from min to max sibling price.

    Temporal checkpoints are used as *sanity bounds*, not point estimates.
    A "by end of 2027" market price is a weak anchor for a "by end of Q2 2027"
    market — different horizons carry different information.  We emit
    ``is_range_only=True`` so the scoring layer treats this as a bounds check
    and refuses to trade on a point mispricing.
    """
    prices = [max(0.0, min(1.0, float(s.price))) for _, s in pairs if s.price is not None]
    if not prices:
        return None
    lo = min(prices)
    hi = max(prices)
    point = (lo + hi) / 2.0
    # Confidence is the max relation confidence times a range-only penalty.
    max_conf = max(float(r.confidence) for r, _ in pairs)
    conf = max(0.0, min(0.60, max_conf * 0.70))
    return SyntheticPrice(
        point=round(point, 6),
        lower=round(lo, 6),
        upper=round(hi, 6),
        confidence=round(conf, 4),
        method="temporal_range",
        contributors=tuple(snap.token_id for _, snap in pairs),
        n_contributors=len(pairs),
        is_range_only=True,
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_STRENGTH_ORDER = {
    RelationKind.INVERSE_OUTCOME: 4,
    RelationKind.MUTUALLY_EXCLUSIVE: 4,
    RelationKind.NEG_RISK_LINKED: 4,
    RelationKind.EQUIVALENT: 3,
    RelationKind.NEAR_EQUIVALENT: 2,
    RelationKind.TEMPORAL_CHECKPOINT: 1,
}


def _is_stronger(a: RelationKind, b: RelationKind) -> bool:
    return _STRENGTH_ORDER.get(a, 0) > _STRENGTH_ORDER.get(b, 0)
