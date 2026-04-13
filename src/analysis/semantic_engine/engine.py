"""Public façade for the semantic mispricing engine.

Two public functions:

* :func:`find_semantic_mispricings` — stateless pure scan.  Tests, scripts
  and the CLI can call this directly.
* :func:`scan_and_record` — convenience wrapper that also persists the
  detections to the ``semantic_signals`` table via :class:`SQLiteStore`.

Neither function places trades or mutates portfolio state.  Both are safe
to call from any context.  The strategy layer
(:class:`SemanticMispricingStrategy`) consumes the result via
``set_semantic_context`` during :func:`_tick`.
"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

from src.analysis.semantic_engine.relations import (
    RelationClassifierConfig,
    discover_relations,
    summarise_relations,
)
from src.analysis.semantic_engine.scoring import (
    ScoringConfig,
    score_mispricing,
)
from src.analysis.semantic_engine.synthetic_price import estimate_fair_price
from src.analysis.semantic_engine.types import SemanticMispricing
from src.polymarket.market_data import MarketSnapshot

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def find_semantic_mispricings(
    snapshots: Sequence[MarketSnapshot],
    *,
    relation_cfg: RelationClassifierConfig | None = None,
    scoring_cfg: ScoringConfig | None = None,
    min_relation_confidence: float = 0.50,
    min_net_edge: float = 0.01,
    min_signal_score: float = 0.50,
    max_related_per_target: int = 8,
    min_sibling_liquidity: float = 0.0,
    prefer_maker: bool = False,
) -> list[SemanticMispricing]:
    """Scan ``snapshots`` and return actionable mispricings.

    Only targets that pass *all* of these survive:

    * at least one relation with ``confidence >= min_relation_confidence``,
    * a synthetic fair price could be built,
    * ``net_edge >= min_net_edge`` AND ``score >= min_signal_score``.

    Targets with no relations silently produce nothing.  The scan is
    idempotent: given the same inputs and cfg it always returns the same
    list.
    """
    if not snapshots:
        return []

    rel_cfg = relation_cfg or RelationClassifierConfig()
    sc_cfg = scoring_cfg or ScoringConfig()

    # ---- 1. Discover relations across every target ----
    relations_by_token = discover_relations(
        snapshots, cfg=rel_cfg, max_related_per_target=max_related_per_target,
    )

    # Early exit if nothing structural nor textual fired — common on cold,
    # small universes.
    total_rels = sum(len(v) for v in relations_by_token.values())
    if total_rels == 0:
        logger.debug("Semantic engine: no relations discovered.")
        return []
    logger.debug("Semantic engine: %d relations, %s",
                 total_rels, summarise_relations(relations_by_token))

    # ---- 2. Index snapshots by token_id for O(1) sibling lookups ----
    by_tok = {s.token_id: s for s in snapshots}

    # ---- 3. Score each target ----
    out: list[SemanticMispricing] = []
    for target in snapshots:
        rels = relations_by_token.get(target.token_id, [])
        # Apply the confidence floor *before* building the synthetic price
        # so a noisy sibling cannot drag the fair-value.
        rels = [r for r in rels if r.confidence >= min_relation_confidence]
        if not rels:
            continue

        synth = estimate_fair_price(
            target, rels, by_tok, min_sibling_liquidity=min_sibling_liquidity,
        )
        if synth is None:
            continue

        # Need an executable book to quote edge against — if the snapshot
        # lacks bid/ask (uses midpoint only), approximate from price±spread/2.
        best_bid, best_ask = _bid_ask_from_snapshot(target)
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            continue

        outcome = score_mispricing(
            synth,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=float(target.spread or 0.0),
            liquidity=float(target.liquidity or 0.0),
            cfg=sc_cfg,
            prefer_maker=prefer_maker,
        )

        if outcome.side == "NONE":
            continue
        if outcome.net_edge < min_net_edge:
            continue
        if outcome.score < min_signal_score:
            continue

        features = {
            "synthetic_fair": synth.point,
            "synthetic_lower": synth.lower,
            "synthetic_upper": synth.upper,
            "synthetic_method": synth.method,
            "synthetic_confidence": synth.confidence,
            "synthetic_n_contributors": synth.n_contributors,
            "gross_edge": outcome.gross_edge,
            "net_edge": outcome.net_edge,
            "signal_score": outcome.score,
            "relation_kinds": sorted({r.kind.value for r in rels}),
            "relation_count": len(rels),
            "relation_top_confidence": max(r.confidence for r in rels),
            "score_components": outcome.components,
        }

        out.append(SemanticMispricing(
            token_id=target.token_id,
            condition_id=target.condition_id,
            question=target.question,
            category=target.category,
            best_bid=round(best_bid, 6),
            best_ask=round(best_ask, 6),
            midpoint=round((best_bid + best_ask) / 2.0, 6),
            spread=round(float(target.spread or 0.0), 6),
            liquidity=round(float(target.liquidity or 0.0), 4),
            synthetic=synth,
            side=outcome.side,
            gross_edge=outcome.gross_edge,
            net_edge=outcome.net_edge,
            score=outcome.score,
            relations=tuple(rels),
            features=features,
        ))

    logger.info(
        "Semantic engine: scanned %d markets, emitted %d mispricing signals.",
        len(snapshots), len(out),
    )
    return out


# --------------------------------------------------------------------------
# Observer: scan + persist
# --------------------------------------------------------------------------

def scan_and_record(
    snapshots: Sequence[MarketSnapshot],
    store,  # SQLiteStore — avoid circular import at module load
    timestamp: str,
    *,
    relation_cfg: RelationClassifierConfig | None = None,
    scoring_cfg: ScoringConfig | None = None,
    min_relation_confidence: float = 0.50,
    min_net_edge: float = 0.01,
    min_signal_score: float = 0.50,
    max_related_per_target: int = 8,
    min_sibling_liquidity: float = 0.0,
    prefer_maker: bool = False,
) -> list[SemanticMispricing]:
    """Call :func:`find_semantic_mispricings` and persist to ``semantic_signals``.

    Returns the detections so the caller can feed them back to the strategy
    layer without a DB round-trip.
    """
    mispricings = find_semantic_mispricings(
        snapshots,
        relation_cfg=relation_cfg,
        scoring_cfg=scoring_cfg,
        min_relation_confidence=min_relation_confidence,
        min_net_edge=min_net_edge,
        min_signal_score=min_signal_score,
        max_related_per_target=max_related_per_target,
        min_sibling_liquidity=min_sibling_liquidity,
        prefer_maker=prefer_maker,
    )
    if not mispricings:
        return mispricings
    try:
        store.insert_semantic_signals(timestamp, mispricings)
    except AttributeError:
        logger.warning(
            "SQLiteStore lacks insert_semantic_signals — skipping persistence. "
            "(Is this an old DB/store instance?)"
        )
    except Exception:
        # Persistence must never break the tick loop.
        logger.exception("Failed to persist semantic signals — continuing.")
    return mispricings


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _bid_ask_from_snapshot(snap: MarketSnapshot) -> tuple[float, float]:
    """Derive (best_bid, best_ask) from a ``MarketSnapshot``.

    The snapshot only carries a midpoint + spread.  When the spread is
    unknown or zero we cannot estimate an executable price honestly, so we
    return (0, 0) and the caller drops the target.  This avoids trading
    on a synthesised book.
    """
    price = snap.price
    spread = snap.spread
    if price is None or spread is None or spread <= 0:
        return 0.0, 0.0
    half = float(spread) / 2.0
    best_bid = max(0.0, float(price) - half)
    best_ask = min(1.0, float(price) + half)
    return best_bid, best_ask


def summarise(mispricings: Iterable[SemanticMispricing]) -> dict:
    """Aggregate KPI dict suitable for tick logs / dashboard."""
    items = list(mispricings)
    if not items:
        return {"count": 0, "kinds": {}, "avg_score": 0.0, "avg_net_edge": 0.0}
    kinds: dict[str, int] = {}
    for m in items:
        for r in m.relations:
            kinds[r.kind.value] = kinds.get(r.kind.value, 0) + 1
    return {
        "count": len(items),
        "kinds": kinds,
        "avg_score": round(sum(m.score for m in items) / len(items), 4),
        "avg_net_edge": round(sum(m.net_edge for m in items) / len(items), 6),
        "by_side": {
            "BUY": sum(1 for m in items if m.side == "BUY"),
            "SELL": sum(1 for m in items if m.side == "SELL"),
        },
    }
