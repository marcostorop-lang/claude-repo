"""Unit tests for the synthetic fair-price engine."""

from __future__ import annotations

import pytest

from src.analysis.semantic_engine.synthetic_price import estimate_fair_price
from src.analysis.semantic_engine.types import MarketRelation, RelationKind
from src.polymarket.market_data import MarketSnapshot


def _snap(token_id, price=0.5, liquidity=10000.0, condition_id="c1"):
    return MarketSnapshot(
        condition_id=condition_id,
        question="Q?",
        token_id=token_id,
        outcome="Yes",
        price=price,
        spread=0.02,
        volume=50000.0,
        liquidity=liquidity,
        active=True,
        category="test",
        end_date="2026-12-31",
    )


def _rel(target, sibling, kind=RelationKind.INVERSE_OUTCOME, conf=0.95):
    return MarketRelation(
        target_token_id=target,
        sibling_token_id=sibling,
        kind=kind,
        confidence=conf,
        reason="test",
    )


class TestStructuralComplement:
    def test_binary_yes_no_complement(self):
        """If sibling 'No' is at 0.40, 'Yes' fair = 1 - 0.40 = 0.60."""
        target = _snap("yes", price=0.55)
        sibling = _snap("no", price=0.40)
        rel = _rel("yes", "no", RelationKind.INVERSE_OUTCOME)
        by_tok = {"yes": target, "no": sibling}
        out = estimate_fair_price(target, [rel], by_tok)
        assert out is not None
        assert out.point == pytest.approx(0.60, abs=1e-6)
        assert out.method == "structural_complement"
        assert out.n_contributors == 1
        assert not out.is_range_only

    def test_multi_outcome_sum(self):
        """Target fair = 1 - sum(other 3 outcomes)."""
        t = _snap("t", price=0.30, condition_id="cA")
        o1 = _snap("o1", price=0.30, condition_id="cA")
        o2 = _snap("o2", price=0.25, condition_id="cA")
        o3 = _snap("o3", price=0.15, condition_id="cA")
        rels = [
            _rel("t", "o1", RelationKind.NEG_RISK_LINKED, conf=0.95),
            _rel("t", "o2", RelationKind.NEG_RISK_LINKED, conf=0.95),
            _rel("t", "o3", RelationKind.NEG_RISK_LINKED, conf=0.95),
        ]
        by_tok = {"t": t, "o1": o1, "o2": o2, "o3": o3}
        out = estimate_fair_price(t, rels, by_tok)
        assert out is not None
        assert out.point == pytest.approx(1.0 - (0.30 + 0.25 + 0.15), abs=1e-6)
        assert out.n_contributors == 3
        # Count bonus pushes confidence above base.
        assert out.confidence > 0.95

    def test_clamped_to_unit_interval(self):
        """If siblings sum > 1 (possible pre-arb), fair clamps at 0."""
        t = _snap("t", price=0.1, condition_id="cA")
        o1 = _snap("o1", price=0.80, condition_id="cA")
        o2 = _snap("o2", price=0.30, condition_id="cA")
        rels = [
            _rel("t", "o1", RelationKind.NEG_RISK_LINKED),
            _rel("t", "o2", RelationKind.NEG_RISK_LINKED),
        ]
        by_tok = {"t": t, "o1": o1, "o2": o2}
        out = estimate_fair_price(t, rels, by_tok)
        assert out is not None
        assert 0.0 <= out.point <= 1.0


class TestTextualEquivalent:
    def test_weighted_average(self):
        t = _snap("t", price=0.40, liquidity=5000, condition_id="c0")
        s1 = _snap("s1", price=0.60, liquidity=10000, condition_id="c1")
        s2 = _snap("s2", price=0.65, liquidity=20000, condition_id="c2")
        rels = [
            _rel("t", "s1", RelationKind.EQUIVALENT, conf=0.85),
            _rel("t", "s2", RelationKind.EQUIVALENT, conf=0.80),
        ]
        by_tok = {"t": t, "s1": s1, "s2": s2}
        out = estimate_fair_price(t, rels, by_tok)
        assert out is not None
        # Weighted mean must fall between sibling prices.
        assert 0.60 <= out.point <= 0.65
        assert out.method == "weighted_avg_equivalent"
        assert out.n_contributors == 2

    def test_high_dispersion_lowers_confidence(self):
        t = _snap("t", price=0.40)
        s1 = _snap("s1", price=0.30, condition_id="c1", liquidity=10000)
        s2 = _snap("s2", price=0.80, condition_id="c2", liquidity=10000)
        rels = [
            _rel("t", "s1", RelationKind.EQUIVALENT, conf=0.85),
            _rel("t", "s2", RelationKind.EQUIVALENT, conf=0.85),
        ]
        by_tok = {"t": t, "s1": s1, "s2": s2}
        out = estimate_fair_price(t, rels, by_tok)
        assert out is not None
        # Dispersion is high (0.30 vs 0.80), so confidence must be punished.
        assert out.confidence < 0.50
        # Band should also be wide.
        assert out.upper - out.lower > 0.10

    def test_single_sibling_penalty(self):
        t = _snap("t", price=0.40)
        s1 = _snap("s1", price=0.60, condition_id="c1", liquidity=10000)
        rels = [_rel("t", "s1", RelationKind.EQUIVALENT, conf=0.90)]
        by_tok = {"t": t, "s1": s1}
        out = estimate_fair_price(t, rels, by_tok)
        assert out is not None
        # Single sibling is penalised.
        assert out.confidence < 0.90


class TestTemporalRange:
    def test_emits_range_only(self):
        t = _snap("t", price=0.40)
        s1 = _snap("s1", price=0.35, condition_id="c1")
        s2 = _snap("s2", price=0.55, condition_id="c2")
        rels = [
            _rel("t", "s1", RelationKind.TEMPORAL_CHECKPOINT, conf=0.55),
            _rel("t", "s2", RelationKind.TEMPORAL_CHECKPOINT, conf=0.50),
        ]
        by_tok = {"t": t, "s1": s1, "s2": s2}
        out = estimate_fair_price(t, rels, by_tok)
        assert out is not None
        assert out.is_range_only
        assert out.lower == pytest.approx(0.35)
        assert out.upper == pytest.approx(0.55)
        assert out.method == "temporal_range"

    def test_temporal_confidence_capped(self):
        t = _snap("t", price=0.40)
        s1 = _snap("s1", price=0.50, condition_id="c1")
        rels = [_rel("t", "s1", RelationKind.TEMPORAL_CHECKPOINT, conf=0.80)]
        by_tok = {"t": t, "s1": s1}
        out = estimate_fair_price(t, rels, by_tok)
        assert out is not None
        # Temporal confidence is capped below 0.60 regardless of relation conf.
        assert out.confidence <= 0.60


class TestPrecedence:
    def test_structural_beats_textual_for_same_sibling(self):
        """When a sibling is both structural and textual, structural wins."""
        t = _snap("t", price=0.40, condition_id="c1")
        s = _snap("s", price=0.60, condition_id="c1")
        rels = [
            _rel("t", "s", RelationKind.EQUIVALENT, conf=0.80),
            _rel("t", "s", RelationKind.INVERSE_OUTCOME, conf=0.99),
        ]
        by_tok = {"t": t, "s": s}
        out = estimate_fair_price(t, rels, by_tok)
        assert out is not None
        assert out.method == "structural_complement"

    def test_textual_used_when_no_structural(self):
        t = _snap("t", price=0.40, condition_id="c0")
        s = _snap("s", price=0.60, condition_id="c1", liquidity=10000)
        rel = _rel("t", "s", RelationKind.EQUIVALENT, conf=0.85)
        by_tok = {"t": t, "s": s}
        out = estimate_fair_price(t, [rel], by_tok)
        assert out is not None
        assert out.method == "weighted_avg_equivalent"


class TestRobustness:
    def test_missing_sibling_drops_out(self):
        t = _snap("t", price=0.40)
        rel = _rel("t", "ghost", RelationKind.INVERSE_OUTCOME)
        out = estimate_fair_price(t, [rel], {"t": t})
        # Sibling not in by_tok → nothing to aggregate.
        assert out is None

    def test_sibling_without_price_drops(self):
        t = _snap("t", price=0.40)
        s = _snap("s", price=None)  # ← invalid price
        rel = _rel("t", "s", RelationKind.INVERSE_OUTCOME)
        out = estimate_fair_price(t, [rel], {"t": t, "s": s})
        assert out is None

    def test_min_liquidity_filter(self):
        t = _snap("t", price=0.40)
        s = _snap("s", price=0.60, liquidity=50.0)  # below floor
        rel = _rel("t", "s", RelationKind.INVERSE_OUTCOME)
        out = estimate_fair_price(t, [rel], {"t": t, "s": s}, min_sibling_liquidity=500.0)
        assert out is None

    def test_no_relations_returns_none(self):
        t = _snap("t", price=0.40)
        assert estimate_fair_price(t, [], {"t": t}) is None
