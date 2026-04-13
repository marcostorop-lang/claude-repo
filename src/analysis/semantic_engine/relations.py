"""Relation classifier for the semantic engine.

Pipeline per tick:

1. Group ``MarketSnapshot`` objects by ``condition_id`` to harvest the
   cheapest structural relations first (MUTUALLY_EXCLUSIVE / NEG_RISK_LINKED /
   INVERSE_OUTCOME).  These are essentially free — the API already tells us
   these markets belong to the same event.
2. For each remaining cross-condition pair, compute textual similarity and
   check for temporal differences.  This is O(N²) over markets; we cap at
   :attr:`max_related_markets` per target to keep the cost bounded.

The classifier is *conservative*: when in doubt we return :attr:`RelationKind.UNKNOWN`.
A forced INVERSE_OUTCOME via flimsy lexical negation is worse than no
relation at all — we would generate a phantom synthetic price and trade on
noise.  Tighten the thresholds first, loosen later only with evidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

from src.analysis.semantic_engine.text_utils import (
    entity_overlap,
    contains_negation,
    date_overlap_days,
    fuzzy_ratio,
    jaccard,
    normalize,
)
from src.analysis.semantic_engine.types import MarketRelation, RelationKind
from src.polymarket.market_data import MarketSnapshot

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Tuning thresholds.  Callers can override via RelationClassifierConfig.
# --------------------------------------------------------------------------

DEFAULT_EQUIVALENT_FUZZY = 0.92
DEFAULT_NEAR_EQUIVALENT_FUZZY = 0.80
DEFAULT_TEMPORAL_JACCARD = 0.60
DEFAULT_TEMPORAL_MIN_DAYS = 1          # anything same-day isn't 'temporal'
DEFAULT_TEMPORAL_MAX_DAYS = 365        # don't link questions a year apart
DEFAULT_SAME_CATEGORY_REQUIRED = True  # cross-category text match is noisy


@dataclass(frozen=True)
class RelationClassifierConfig:
    """Container for tunable thresholds — defaults are deliberately strict."""

    equivalent_fuzzy: float = DEFAULT_EQUIVALENT_FUZZY
    near_equivalent_fuzzy: float = DEFAULT_NEAR_EQUIVALENT_FUZZY
    temporal_jaccard: float = DEFAULT_TEMPORAL_JACCARD
    temporal_min_days: int = DEFAULT_TEMPORAL_MIN_DAYS
    temporal_max_days: int = DEFAULT_TEMPORAL_MAX_DAYS
    same_category_required: bool = DEFAULT_SAME_CATEGORY_REQUIRED
    # Skip textual classification entirely when set — useful when the
    # operator only trusts structural relations (strong-conservative mode).
    disable_textual: bool = False
    # Skip temporal relations entirely.
    disable_temporal: bool = False
    # Skip inverse-outcome detection (beyond structural Yes/No pairs).
    disable_inverse: bool = False


# --------------------------------------------------------------------------
# Structural relations (condition_id + outcome)
# --------------------------------------------------------------------------

def classify_structural_pair(
    target: MarketSnapshot,
    sibling: MarketSnapshot,
) -> MarketRelation | None:
    """Classify two snapshots that share a ``condition_id``.

    Returns ``None`` when the two are actually the same token (so callers
    can naively iterate pair-wise).  Otherwise emits either INVERSE_OUTCOME
    (binary Yes/No), MUTUALLY_EXCLUSIVE (other binary cases) or
    NEG_RISK_LINKED (multi-outcome events) — always high-confidence
    because the structural grouping is given by the exchange.
    """
    if target.token_id == sibling.token_id:
        return None
    if target.condition_id != sibling.condition_id:
        return None

    # Binary Yes/No is so common on Polymarket it deserves its own kind.
    # We detect it by the outcome labels, not by the number of tokens per
    # condition (some binary markets list only one token publicly).
    t_out = (target.outcome or "").strip().lower()
    s_out = (sibling.outcome or "").strip().lower()
    is_yesno = {t_out, s_out} == {"yes", "no"}

    if is_yesno:
        return MarketRelation(
            target_token_id=target.token_id,
            sibling_token_id=sibling.token_id,
            kind=RelationKind.INVERSE_OUTCOME,
            confidence=0.99,  # Near-perfect: complementary by construction.
            reason=(
                f"Yes/No binary on condition_id={target.condition_id[:10]}: "
                f"target='{target.outcome}' vs sibling='{sibling.outcome}'"
            ),
            evidence={
                "condition_id": target.condition_id,
                "target_outcome": target.outcome,
                "sibling_outcome": sibling.outcome,
                "structural": True,
            },
        )

    # Non-binary same-condition: these are multi-outcome / neg-risk markets.
    # Confidence slightly lower because aggregation across more legs is
    # more sensitive to a single stale leg, but still very high.
    return MarketRelation(
        target_token_id=target.token_id,
        sibling_token_id=sibling.token_id,
        kind=RelationKind.NEG_RISK_LINKED,
        confidence=0.95,
        reason=(
            f"Same condition_id={target.condition_id[:10]} "
            f"(multi-outcome): '{target.outcome}' vs '{sibling.outcome}'"
        ),
        evidence={
            "condition_id": target.condition_id,
            "target_outcome": target.outcome,
            "sibling_outcome": sibling.outcome,
            "structural": True,
        },
    )


# --------------------------------------------------------------------------
# Textual / temporal (cross-condition)
# --------------------------------------------------------------------------

def classify_textual_pair(
    target: MarketSnapshot,
    sibling: MarketSnapshot,
    cfg: RelationClassifierConfig = RelationClassifierConfig(),
) -> MarketRelation | None:
    """Classify two markets from *different* conditions via text.

    Returns ``None`` when nothing interesting fires — saves allocations and
    makes the caller's filter step cheap.
    """
    if target.token_id == sibling.token_id:
        return None
    if target.condition_id == sibling.condition_id:
        # Structural pair — caller should have routed to classify_structural_pair.
        return None
    if cfg.disable_textual:
        return None

    tq = (target.question or "").strip()
    sq = (sibling.question or "").strip()
    if not tq or not sq:
        return None

    if cfg.same_category_required and target.category and sibling.category:
        if target.category.lower() != sibling.category.lower():
            return None

    fuzzy = fuzzy_ratio(tq, sq)
    jac = jaccard(tq, sq)
    ent = entity_overlap(tq, sq)

    # --- Equivalent: very high lexical similarity OR strong entity match
    # plus high Jaccard.  Still require same category (handled above) so
    # we don't collide "Will Trump win the 2028 election?" with a sports
    # question that happens to share boilerplate phrasing.
    if fuzzy >= cfg.equivalent_fuzzy and jac >= 0.6:
        return _textual_relation(
            target, sibling, RelationKind.EQUIVALENT,
            confidence=min(0.90, 0.60 + 0.30 * fuzzy),
            fuzzy=fuzzy, jac=jac, ent=ent,
            reason=f"fuzzy={fuzzy:.2f} jaccard={jac:.2f} → equivalent",
        )

    if fuzzy >= cfg.near_equivalent_fuzzy and jac >= 0.5 and ent >= 0.3:
        return _textual_relation(
            target, sibling, RelationKind.NEAR_EQUIVALENT,
            # Scaled down further than equivalent — this is "likely same
            # question" not "definitely same question".
            confidence=min(0.80, 0.40 + 0.40 * fuzzy),
            fuzzy=fuzzy, jac=jac, ent=ent,
            reason=f"fuzzy={fuzzy:.2f} jaccard={jac:.2f} ent={ent:.2f} → near_equivalent",
        )

    # Secondary near-equivalent path: perfect entity match + strong Jaccard
    # rescues moderate fuzzy ratios on paraphrased questions ("Will Trump
    # win the 2028 election?" vs "Is Trump going to win the 2028 election?").
    # Confidence capped lower than the primary path.
    if fuzzy >= 0.70 and jac >= 0.55 and ent >= 0.90:
        return _textual_relation(
            target, sibling, RelationKind.NEAR_EQUIVALENT,
            confidence=min(0.70, 0.30 + 0.40 * fuzzy),
            fuzzy=fuzzy, jac=jac, ent=ent,
            reason=(
                f"fuzzy={fuzzy:.2f} jaccard={jac:.2f} ent={ent:.2f} "
                "→ near_equivalent (entity-rescue)"
            ),
        )

    # --- Inverse outcome across conditions: two markets about the same
    # entity where one asks X and the other asks NOT X.  Very easy to get
    # wrong with naive negation detection, so we require high entity
    # overlap AND clear negation polarity difference.
    if not cfg.disable_inverse and ent >= 0.5 and jac >= 0.4:
        neg_a = contains_negation(tq)
        neg_b = contains_negation(sq)
        if neg_a != neg_b:
            return _textual_relation(
                target, sibling, RelationKind.INVERSE_OUTCOME,
                confidence=min(0.70, 0.35 + 0.30 * ent),
                fuzzy=fuzzy, jac=jac, ent=ent,
                reason=(
                    f"entity_overlap={ent:.2f} + polarity flip "
                    f"(neg: target={neg_a}, sibling={neg_b})"
                ),
            )

    # --- Temporal checkpoint: high token overlap, same category, but
    # meaningfully different end dates.  Emits a RANGE not a point estimate.
    if not cfg.disable_temporal and jac >= cfg.temporal_jaccard:
        dd = date_overlap_days(target.end_date, sibling.end_date)
        if dd is not None and cfg.temporal_min_days <= dd <= cfg.temporal_max_days:
            return _textual_relation(
                target, sibling, RelationKind.TEMPORAL_CHECKPOINT,
                # Confidence decays with time gap — 30 days apart is fine,
                # 365 days apart is almost unrelated.
                confidence=max(0.30, 0.75 - dd / cfg.temporal_max_days * 0.45),
                fuzzy=fuzzy, jac=jac, ent=ent,
                reason=f"jaccard={jac:.2f}, Δdays={dd} → temporal",
                extra={"delta_days": dd},
            )

    return None


def _textual_relation(
    target: MarketSnapshot,
    sibling: MarketSnapshot,
    kind: RelationKind,
    *,
    confidence: float,
    fuzzy: float,
    jac: float,
    ent: float,
    reason: str,
    extra: dict | None = None,
) -> MarketRelation:
    ev = {
        "fuzzy": round(fuzzy, 4),
        "jaccard": round(jac, 4),
        "entity_overlap": round(ent, 4),
        "target_question": target.question,
        "sibling_question": sibling.question,
        "target_end_date": target.end_date,
        "sibling_end_date": sibling.end_date,
        "category": target.category,
    }
    if extra:
        ev.update(extra)
    return MarketRelation(
        target_token_id=target.token_id,
        sibling_token_id=sibling.token_id,
        kind=kind,
        confidence=round(max(0.0, min(1.0, confidence)), 4),
        reason=reason,
        evidence=ev,
    )


# --------------------------------------------------------------------------
# Top-level discovery
# --------------------------------------------------------------------------

def discover_relations(
    snapshots: Sequence[MarketSnapshot],
    cfg: RelationClassifierConfig = RelationClassifierConfig(),
    max_related_per_target: int = 8,
) -> dict[str, list[MarketRelation]]:
    """Return a mapping ``target_token_id -> list[MarketRelation]``.

    Structural relations come first.  Textual/temporal are appended only
    when there is still headroom under :arg:`max_related_per_target`, so a
    target that already has 10 neg-risk siblings won't accumulate noisy
    textual matches on top.  This keeps the synthetic-price aggregation
    cheap and favours the highest-confidence signals.
    """
    # Structural: O(N + groups) — group by condition_id first.
    by_cond: dict[str, list[MarketSnapshot]] = {}
    for s in snapshots:
        by_cond.setdefault(s.condition_id, []).append(s)

    relations: dict[str, list[MarketRelation]] = {s.token_id: [] for s in snapshots}

    for cond_id, group in by_cond.items():
        if len(group) < 2:
            continue
        for target in group:
            bucket = relations[target.token_id]
            for sibling in group:
                if sibling.token_id == target.token_id:
                    continue
                rel = classify_structural_pair(target, sibling)
                if rel is not None:
                    bucket.append(rel)
                    if len(bucket) >= max_related_per_target:
                        break

    # Textual/temporal: iterate across distinct conditions.  O(N²) in the
    # worst case but bounded by max_related_per_target.  We keep it simple
    # and correct over clever-and-fragile.
    if not (cfg.disable_textual and cfg.disable_temporal):
        for i, target in enumerate(snapshots):
            bucket = relations[target.token_id]
            if len(bucket) >= max_related_per_target:
                continue
            for j, sibling in enumerate(snapshots):
                if i == j:
                    continue
                if target.condition_id == sibling.condition_id:
                    continue
                rel = classify_textual_pair(target, sibling, cfg)
                if rel is not None:
                    bucket.append(rel)
                    if len(bucket) >= max_related_per_target:
                        break

    return relations


def summarise_relations(
    relations_by_token: dict[str, Iterable[MarketRelation]],
) -> dict[str, int]:
    """Histogram of relation kinds across all targets.

    Useful for observability — reported alongside tick_stats for the
    dashboard ("detected 42 structural + 7 equivalent + 2 temporal this tick").
    """
    counts: dict[str, int] = {}
    for rels in relations_by_token.values():
        for r in rels:
            counts[r.kind.value] = counts.get(r.kind.value, 0) + 1
    return counts
