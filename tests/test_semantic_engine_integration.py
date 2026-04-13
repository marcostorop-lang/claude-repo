"""End-to-end tests for the semantic engine orchestrator.

These cover :func:`find_semantic_mispricings` and :func:`scan_and_record`
with realistic MarketSnapshot fixtures, plus integration with SQLiteStore.
"""

from __future__ import annotations

import pytest

from src.analysis.semantic_engine import (
    SemanticMispricing,
    find_semantic_mispricings,
    scan_and_record,
)
from src.analysis.semantic_engine.engine import summarise
from src.analysis.semantic_engine.relations import RelationClassifierConfig
from src.analysis.semantic_engine.scoring import ScoringConfig
from src.polymarket.market_data import MarketSnapshot
from src.storage.sqlite_store import SQLiteStore


def _snap(
    token_id,
    price,
    condition_id="cA",
    outcome="Yes",
    question="Will X?",
    category="politics",
    liquidity=10000.0,
    spread=0.02,
):
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
        end_date="2026-12-31",
    )


# ---------------------------------------------------------------------------
# Happy path — binary Yes/No mispricing
# ---------------------------------------------------------------------------

class TestYesNoMispricing:
    def test_yes_cheap_wrt_no(self):
        """Yes at 0.40 while No at 0.40 → sum 0.80 < 1 → arb-ish, both cheap."""
        yes = _snap("t_yes", price=0.40, outcome="Yes", spread=0.02)
        no = _snap("t_no", price=0.40, outcome="No", spread=0.02)
        out = find_semantic_mispricings(
            [yes, no],
            min_net_edge=0.01,
            min_signal_score=0.10,
        )
        # Both should flag as BUY since fair_yes=0.60 >> ask_yes (~0.41)
        # and fair_no=0.60 >> ask_no (~0.41).
        assert len(out) >= 1
        assert all(m.side == "BUY" for m in out)

    def test_no_signal_when_prices_balanced(self):
        """If Yes + No sum exactly to 1, no mispricing exists."""
        yes = _snap("t_yes", price=0.60, outcome="Yes")
        no = _snap("t_no", price=0.40, outcome="No")
        out = find_semantic_mispricings([yes, no], min_net_edge=0.01)
        assert out == []


# ---------------------------------------------------------------------------
# Multi-outcome neg-risk
# ---------------------------------------------------------------------------

class TestMultiOutcomeMispricing:
    def test_missing_mass_across_legs(self):
        """4-way race where prices sum to 0.80 → each leg appears undervalued."""
        a = _snap("a", price=0.20, outcome="A")
        b = _snap("b", price=0.20, outcome="B")
        c = _snap("c", price=0.20, outcome="C")
        d = _snap("d", price=0.20, outcome="D")
        out = find_semantic_mispricings(
            [a, b, c, d], min_net_edge=0.01, min_signal_score=0.10,
        )
        # All 4 legs imply fair = 1 - 0.60 = 0.40 for each, gross edge ~0.19.
        assert len(out) >= 1


# ---------------------------------------------------------------------------
# Filters & gates
# ---------------------------------------------------------------------------

class TestGates:
    def test_below_min_edge_not_emitted(self):
        yes = _snap("t_yes", price=0.55, outcome="Yes")
        no = _snap("t_no", price=0.44, outcome="No")  # sum 0.99, tiny edge
        out = find_semantic_mispricings([yes, no], min_net_edge=0.05)
        assert out == []

    def test_wide_spread_blocked(self):
        yes = _snap("t_yes", price=0.40, outcome="Yes", spread=0.30)
        no = _snap("t_no", price=0.40, outcome="No", spread=0.30)
        out = find_semantic_mispricings(
            [yes, no],
            min_net_edge=0.01,
            min_signal_score=0.60,
            scoring_cfg=ScoringConfig(max_spread=0.05),
        )
        # Score capped at 0.40 by wide-spread floor → below min_signal_score.
        assert out == []

    def test_thin_liquidity_suppresses(self):
        yes = _snap("t_yes", price=0.40, liquidity=50.0, outcome="Yes")
        no = _snap("t_no", price=0.40, liquidity=50.0, outcome="No")
        out = find_semantic_mispricings(
            [yes, no],
            min_net_edge=0.01,
            min_signal_score=0.60,
            scoring_cfg=ScoringConfig(hard_min_liquidity=200.0),
        )
        assert out == []

    def test_min_relation_confidence(self):
        """A very-high confidence floor excludes structural siblings too."""
        yes = _snap("t_yes", price=0.40, outcome="Yes")
        no = _snap("t_no", price=0.40, outcome="No")
        out = find_semantic_mispricings(
            [yes, no],
            min_relation_confidence=1.01,  # impossible
            min_net_edge=0.01,
            min_signal_score=0.10,
        )
        assert out == []


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestScanAndRecord:
    def test_persists_to_sqlite(self):
        yes = _snap("t_yes", price=0.40, outcome="Yes")
        no = _snap("t_no", price=0.40, outcome="No")
        store = SQLiteStore(":memory:")
        res = scan_and_record(
            [yes, no], store, "2026-04-13T12:00:00",
            min_net_edge=0.01, min_signal_score=0.10,
        )
        assert len(res) >= 1
        rows = store.get_recent_semantic_signals(limit=10)
        assert len(rows) == len(res)
        assert rows[0]["side"] in ("BUY", "SELL")
        assert rows[0]["question"]
        store.close()

    def test_empty_scan_inserts_nothing(self):
        yes = _snap("t_yes", price=0.60, outcome="Yes")
        no = _snap("t_no", price=0.40, outcome="No")
        store = SQLiteStore(":memory:")
        res = scan_and_record(
            [yes, no], store, "2026-04-13T12:00:00",
            min_net_edge=0.05,
        )
        assert res == []
        assert store.get_recent_semantic_signals(limit=10) == []
        store.close()


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------

class TestSummarise:
    def test_counts_empty(self):
        assert summarise([]) == {"count": 0, "kinds": {}, "avg_score": 0.0,
                                 "avg_net_edge": 0.0}

    def test_counts_nonempty(self):
        yes = _snap("t_yes", price=0.40, outcome="Yes")
        no = _snap("t_no", price=0.40, outcome="No")
        res = find_semantic_mispricings(
            [yes, no], min_net_edge=0.01, min_signal_score=0.10,
        )
        summary = summarise(res)
        assert summary["count"] == len(res)
        assert summary["by_side"]["BUY"] >= 1


# ---------------------------------------------------------------------------
# Safety: no side effects on unrelated markets
# ---------------------------------------------------------------------------

class TestNoSideEffects:
    def test_markets_without_relations_produce_nothing(self):
        lone = _snap("lone", price=0.50, condition_id="solo")
        out = find_semantic_mispricings([lone], min_net_edge=0.01)
        assert out == []

    def test_invalid_price_snapshot_ignored(self):
        yes = _snap("t_yes", price=None, outcome="Yes")
        no = _snap("t_no", price=0.40, outcome="No")
        # Price=None makes target un-estimatable; engine should just skip it.
        out = find_semantic_mispricings([yes, no], min_net_edge=0.01)
        # At most one signal (for t_no if its synthetic is high enough)
        assert all(m.token_id != "t_yes" for m in out)
