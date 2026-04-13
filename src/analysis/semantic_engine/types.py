"""Immutable dataclasses shared across the semantic engine.

Every dataclass here is plain data: pure Python, no I/O, no circular imports
with the rest of the engine.  They are the contract the engine emits and the
strategy consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RelationKind(str, Enum):
    """Classification of how two markets are related.

    ``str`` subclass keeps the values JSON-serialisable without a custom
    encoder — useful since these end up in the ``semantic_signals`` table.
    """

    # --- Structural (condition_id based) ---
    MUTUALLY_EXCLUSIVE = "mutually_exclusive"   # Same event, different outcomes
    NEG_RISK_LINKED = "neg_risk_linked"         # Same multi-outcome event
    INVERSE_OUTCOME = "inverse_outcome"         # Binary Yes/No pair

    # --- Textual / semantic (across conditions) ---
    EQUIVALENT = "equivalent"                   # Near-identical question
    NEAR_EQUIVALENT = "near_equivalent"         # High similarity but not identical
    TEMPORAL_CHECKPOINT = "temporal_checkpoint"  # Same topic, different horizon

    # --- Reserved for future, not emitted by v1 ---
    SUBSET = "subset"
    SUPERSET = "superset"
    RELATED_BUT_WEAK = "related_but_weak"

    UNKNOWN = "unknown"

    @property
    def is_structural(self) -> bool:
        return self in (
            RelationKind.MUTUALLY_EXCLUSIVE,
            RelationKind.NEG_RISK_LINKED,
            RelationKind.INVERSE_OUTCOME,
        )

    @property
    def is_textual(self) -> bool:
        return self in (
            RelationKind.EQUIVALENT,
            RelationKind.NEAR_EQUIVALENT,
            RelationKind.TEMPORAL_CHECKPOINT,
        )


@dataclass(frozen=True)
class MarketRelation:
    """A classified relation between a target and a sibling market.

    ``target_token_id`` is the market whose synthetic price we want to estimate.
    ``sibling_token_id`` is the auxiliary market used as evidence.  All
    relations are directional — an EQUIVALENT A→B also implies B→A but
    the engine classifies them pair-wise to keep the evidence trail clean.
    """

    target_token_id: str
    sibling_token_id: str
    kind: RelationKind
    confidence: float  # 0.0 – 1.0
    # Human-readable short reason (e.g. "same condition_id, outcome='No'").
    reason: str = ""
    # Structured evidence usable for offline analysis/debugging.
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SyntheticPrice:
    """A synthetic fair-price estimate derived from related markets.

    ``point`` is the central estimate; ``lower``/``upper`` describe a
    tolerance band.  When a relation class only yields bounds (e.g.
    TEMPORAL_CHECKPOINT) the point is the midpoint and the band is wide.
    """

    point: float
    lower: float
    upper: float
    confidence: float  # 0.0 – 1.0
    method: str        # e.g. "neg_risk_complement", "weighted_avg_equivalent"
    # Token ids that contributed, in descending weight order.
    contributors: tuple[str, ...] = field(default_factory=tuple)
    # How many siblings actually fed the estimate (may differ from contributors
    # if some were dropped for low liquidity / stale price).
    n_contributors: int = 0
    # True when the best we can offer is a range, not a point (e.g. temporal).
    is_range_only: bool = False


@dataclass(frozen=True)
class SemanticMispricing:
    """A detected mispricing between executable price and synthetic fair.

    This is what the engine emits and what the observer persists.  Written
    to the ``semantic_signals`` SQLite table and consumed by the
    :class:`SemanticMispricingStrategy` when ``STRATEGY=semantic_mispricing``.

    All prices are in Polymarket decimal probability convention (0.0 – 1.0).
    """

    token_id: str
    condition_id: str
    question: str
    category: str

    # Observed executable prices at the time of detection.
    best_bid: float
    best_ask: float
    midpoint: float
    spread: float
    liquidity: float

    # What the engine thinks is fair.
    synthetic: SyntheticPrice

    # Side we would take.  "BUY" when synthetic > best_ask (market too cheap),
    # "SELL" when synthetic < best_bid (market too rich).
    side: str   # "BUY" | "SELL" | "NONE"
    # Edge before subtracting costs (signed: positive = in our favour).
    gross_edge: float
    # Edge after fees, estimated slippage and safety margin.  Only this matters
    # for trading decisions — gross is kept purely for diagnostics.
    net_edge: float
    # Composite quality score in [0, 1].  The strategy uses
    # ``net_edge >= min_net_edge AND score >= min_signal_score`` as its gate.
    score: float

    # Relations that fed the synthetic price, for forensic trails.
    relations: tuple[MarketRelation, ...] = field(default_factory=tuple)
    # Free-form numeric bag (mirrors Signal.features) — ends up as JSON.
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def has_actionable_edge(self) -> bool:
        return self.side in ("BUY", "SELL") and self.net_edge > 0.0
