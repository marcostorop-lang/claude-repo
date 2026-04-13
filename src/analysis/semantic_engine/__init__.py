"""Semantic Mispricing Engine — opt-in observer + strategy layer.

This package detects desalignments between a target Polymarket market and a
synthetic fair price derived from *related* markets (mutually exclusive
outcomes, neg-risk siblings, near-equivalent questions across conditions,
temporal checkpoints).  It is strictly opt-in via :data:`Config.semantic_engine_enabled`
and default-off.  When disabled nothing here is imported or run.

Public entry points:

* :func:`find_semantic_mispricings` — stateless scan (pure function).
* :func:`scan_and_record` — scan + persist to ``semantic_signals`` table.
* :class:`SemanticMispricing` / :class:`MarketRelation` / :class:`SyntheticPrice`
  — immutable dataclasses suitable for logging and testing.

Heuristics are deterministic and stdlib-only — no LLM calls, no heavy NLP
dependencies — precisely so a spurious textual similarity cannot
contaminate real trades.  See ``docs/semantic_mispricing.md``.
"""

from __future__ import annotations

from src.analysis.semantic_engine.engine import (
    find_semantic_mispricings,
    scan_and_record,
)
from src.analysis.semantic_engine.types import (
    MarketRelation,
    RelationKind,
    SemanticMispricing,
    SyntheticPrice,
)

__all__ = [
    "MarketRelation",
    "RelationKind",
    "SemanticMispricing",
    "SyntheticPrice",
    "find_semantic_mispricings",
    "scan_and_record",
]
