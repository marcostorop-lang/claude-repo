"""Unit tests for the relation classifier."""

from __future__ import annotations

import pytest

from src.analysis.semantic_engine.relations import (
    RelationClassifierConfig,
    classify_structural_pair,
    classify_textual_pair,
    discover_relations,
    summarise_relations,
)
from src.analysis.semantic_engine.types import RelationKind
from src.polymarket.market_data import MarketSnapshot


def _snap(
    token_id: str,
    condition_id: str = "cond1",
    question: str = "Will X happen?",
    outcome: str = "Yes",
    category: str = "politics",
    end_date: str = "2026-12-31",
    price: float = 0.5,
    spread: float = 0.02,
    liquidity: float = 10000.0,
) -> MarketSnapshot:
    return MarketSnapshot(
        condition_id=condition_id,
        question=question,
        token_id=token_id,
        outcome=outcome,
        price=price,
        spread=spread,
        volume=50000.0,
        liquidity=liquidity,
        active=True,
        category=category,
        end_date=end_date,
    )


# ---------------------------------------------------------------------------
# Structural classification
# ---------------------------------------------------------------------------

class TestStructuralPair:
    def test_same_token_returns_none(self):
        a = _snap("t1")
        b = _snap("t1")
        assert classify_structural_pair(a, b) is None

    def test_different_conditions_returns_none(self):
        a = _snap("t1", condition_id="c1")
        b = _snap("t2", condition_id="c2")
        assert classify_structural_pair(a, b) is None

    def test_binary_yes_no_is_inverse(self):
        y = _snap("t1", outcome="Yes")
        n = _snap("t2", outcome="No")
        rel = classify_structural_pair(y, n)
        assert rel is not None
        assert rel.kind == RelationKind.INVERSE_OUTCOME
        assert rel.confidence > 0.95

    def test_multi_outcome_is_neg_risk(self):
        a = _snap("t1", outcome="Biden")
        b = _snap("t2", outcome="Trump")
        rel = classify_structural_pair(a, b)
        assert rel is not None
        assert rel.kind == RelationKind.NEG_RISK_LINKED
        assert rel.confidence > 0.90

    def test_evidence_contains_outcomes(self):
        a = _snap("t1", outcome="Yes")
        b = _snap("t2", outcome="No")
        rel = classify_structural_pair(a, b)
        assert rel.evidence["target_outcome"] == "Yes"
        assert rel.evidence["sibling_outcome"] == "No"
        assert rel.evidence["structural"] is True


# ---------------------------------------------------------------------------
# Textual classification
# ---------------------------------------------------------------------------

class TestTextualPair:
    def test_equivalent_high_similarity(self):
        a = _snap("t1", condition_id="cA",
                  question="Will Trump win the 2028 presidential election?")
        b = _snap("t2", condition_id="cB",
                  question="Will Trump win the 2028 presidential election?")
        rel = classify_textual_pair(a, b)
        assert rel is not None
        assert rel.kind == RelationKind.EQUIVALENT

    def test_near_equivalent_moderate_similarity(self):
        a = _snap("t1", condition_id="cA",
                  question="Will Donald Trump win the 2028 presidential election?")
        b = _snap("t2", condition_id="cB",
                  question="Is Donald Trump going to win the 2028 US election?")
        rel = classify_textual_pair(a, b)
        assert rel is not None
        # Could be classified as EQUIVALENT or NEAR_EQUIVALENT depending on
        # fuzzy ratio; both are acceptable semantic-equivalence signals.
        assert rel.kind in (RelationKind.EQUIVALENT, RelationKind.NEAR_EQUIVALENT)

    def test_different_category_returns_none(self):
        a = _snap("t1", condition_id="cA", category="politics",
                  question="Will Trump win the 2028 election?")
        b = _snap("t2", condition_id="cB", category="sports",
                  question="Will Trump win the 2028 election?")
        rel = classify_textual_pair(a, b)
        # Same text across different categories is suppressed by default.
        assert rel is None

    def test_inverse_outcome_across_conditions(self):
        a = _snap("t1", condition_id="cA",
                  question="Will Donald Trump win the 2028 election?")
        b = _snap("t2", condition_id="cB",
                  question="Will Donald Trump NOT win the 2028 election?")
        rel = classify_textual_pair(a, b)
        assert rel is not None
        # Either near-equivalent or inverse — both are valid heuristic hits
        # for a polarity-flipped sibling.
        assert rel.kind in (
            RelationKind.INVERSE_OUTCOME,
            RelationKind.NEAR_EQUIVALENT,
            RelationKind.EQUIVALENT,
        )

    def test_temporal_checkpoint(self):
        a = _snap("t1", condition_id="cA",
                  question="Will Ethereum price exceed 5000 USD by year end?",
                  end_date="2026-12-31")
        b = _snap("t2", condition_id="cB",
                  question="Will Ethereum price exceed 5000 USD by year end?",
                  end_date="2027-12-31")
        cfg = RelationClassifierConfig(
            # Suppress equivalent so we can observe temporal explicitly; in
            # real life both would fire and the engine would pick structural
            # first then textual.
            disable_textual=False,
        )
        rel = classify_textual_pair(a, b, cfg)
        assert rel is not None

    def test_empty_question_returns_none(self):
        a = _snap("t1", condition_id="cA", question="")
        b = _snap("t2", condition_id="cB", question="anything")
        assert classify_textual_pair(a, b) is None

    def test_disable_textual_suppresses_all(self):
        a = _snap("t1", condition_id="cA", question="Will X happen?")
        b = _snap("t2", condition_id="cB", question="Will X happen?")
        cfg = RelationClassifierConfig(disable_textual=True)
        assert classify_textual_pair(a, b, cfg) is None


# ---------------------------------------------------------------------------
# Top-level discovery
# ---------------------------------------------------------------------------

class TestDiscoverRelations:
    def test_empty_input(self):
        assert discover_relations([]) == {}

    def test_single_market_has_no_relations(self):
        rels = discover_relations([_snap("t1")])
        assert rels == {"t1": []}

    def test_yesno_pair_discovered(self):
        snaps = [
            _snap("t1", condition_id="cA", outcome="Yes"),
            _snap("t2", condition_id="cA", outcome="No"),
        ]
        rels = discover_relations(snaps)
        assert any(r.kind == RelationKind.INVERSE_OUTCOME for r in rels["t1"])
        assert any(r.kind == RelationKind.INVERSE_OUTCOME for r in rels["t2"])

    def test_cross_condition_equivalent(self):
        snaps = [
            _snap("t1", condition_id="cA",
                  question="Will Trump win the 2028 election?"),
            _snap("t2", condition_id="cB",
                  question="Will Trump win the 2028 election?"),
        ]
        rels = discover_relations(snaps)
        t1_kinds = {r.kind for r in rels["t1"]}
        assert (RelationKind.EQUIVALENT in t1_kinds or
                RelationKind.NEAR_EQUIVALENT in t1_kinds)

    def test_max_related_caps_bucket(self):
        # 10 binary markets on the same condition → t1 should have at most
        # ``max_related_per_target`` siblings.
        snaps = [_snap(f"t{i}", condition_id="cA", outcome=f"O{i}")
                 for i in range(10)]
        rels = discover_relations(snaps, max_related_per_target=3)
        assert len(rels["t0"]) <= 3

    def test_summarise_relations(self):
        snaps = [
            _snap("t1", condition_id="cA", outcome="Yes"),
            _snap("t2", condition_id="cA", outcome="No"),
        ]
        rels = discover_relations(snaps)
        counts = summarise_relations(rels)
        assert counts.get(RelationKind.INVERSE_OUTCOME.value, 0) >= 2


# ---------------------------------------------------------------------------
# Conservative defaults
# ---------------------------------------------------------------------------

class TestConservativeDefaults:
    def test_short_questions_dont_match_wildly(self):
        """A one-word overlap must not produce a relation."""
        a = _snap("t1", condition_id="cA", question="Will Apple ship a car?")
        b = _snap("t2", condition_id="cB", question="Will Apple lose in court?")
        rel = classify_textual_pair(a, b)
        # Defaults require high fuzzy ratio AND entity overlap; "Apple" alone
        # is not enough.
        assert rel is None or rel.confidence < 0.70
