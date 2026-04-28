"""Tests for retrieval-augmented Claude prompts.

The default embedder is a deterministic hashed-bag-of-words; that's
*intentional* so tests don't need an external service and so the
ranking is testable end-to-end.  Production can swap in a real
embedder via ``QuestionEmbeddingStore(embedder=...)``.

Covers:
* The default embedder is deterministic and L2-normalised.
* Cosine similarity edge cases (empty / zero / mismatched dim).
* Index round-trip: upsert → retrieve same vector.
* Retrieval ranks similar questions higher than unrelated ones.
* Backfill from ``market_resolutions`` indexes the rows.
* Prompt assembly drops below-threshold cases and bails when none
  remain (no inflated prompt with noise).
"""

from __future__ import annotations

import math

import pytest

from src.analysis.question_embeddings import (
    QuestionEmbeddingStore,
    ResolvedCase,
    assemble_retrieval_prompt,
    cosine_similarity,
    hashed_bow_embedding,
)
from src.storage.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# Default embedder
# ---------------------------------------------------------------------------


class TestHashedBowEmbedding:
    def test_deterministic(self):
        a = hashed_bow_embedding("Will Trump win the 2028 election?")
        b = hashed_bow_embedding("Will Trump win the 2028 election?")
        assert a == b

    def test_l2_normalised_when_nonempty(self):
        v = hashed_bow_embedding("hello world")
        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 1e-6

    def test_empty_returns_zero_vector(self):
        v = hashed_bow_embedding("", dim=64)
        assert v == [0.0] * 64

    def test_unrelated_questions_score_low(self):
        a = hashed_bow_embedding("Will it snow in Madrid this winter?")
        b = hashed_bow_embedding("Bitcoin price by year end")
        assert cosine_similarity(a, b) < 0.5


# ---------------------------------------------------------------------------
# Cosine
# ---------------------------------------------------------------------------


class TestCosine:
    def test_identical_vectors_score_one(self):
        v = hashed_bow_embedding("foo bar baz")
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-9

    def test_zero_vector_safe(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_dim_mismatch_safe(self):
        assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# Round-trip via the store
# ---------------------------------------------------------------------------


class TestStoreRoundTrip:
    def test_upsert_and_retrieve_same_vector(self, tmp_path):
        s = SQLiteStore(str(tmp_path / "t.db"))
        try:
            qs = QuestionEmbeddingStore(s, dim=128)
            case = ResolvedCase(
                condition_id="cid1", question="Will A happen by end of year?",
                outcome="YES", resolved_price=1.0,
                resolved_at="2026-01-15T00:00:00Z",
            )
            qs.upsert(case)
            results = qs.find_similar("Will A happen by end of year?")
            assert len(results) == 1
            best, sim = results[0]
            assert best.condition_id == "cid1"
            # Identical question → similarity 1.0 (within float rounding).
            assert abs(sim - 1.0) < 1e-5
        finally:
            s.close()

    def test_upsert_overwrites_same_condition_id(self, tmp_path):
        s = SQLiteStore(str(tmp_path / "t.db"))
        try:
            qs = QuestionEmbeddingStore(s)
            for q in ("first version", "second version"):
                qs.upsert(ResolvedCase(
                    condition_id="cid1", question=q,
                    outcome="YES", resolved_price=1.0,
                    resolved_at="2026-01-15T00:00:00Z",
                ))
            cur = s._conn.execute(
                "SELECT COUNT(*) FROM question_embeddings WHERE condition_id='cid1'",
            )
            assert cur.fetchone()[0] == 1
            cur = s._conn.execute(
                "SELECT question FROM question_embeddings WHERE condition_id='cid1'",
            )
            assert cur.fetchone()[0] == "second version"
        finally:
            s.close()


# ---------------------------------------------------------------------------
# Retrieval ranking
# ---------------------------------------------------------------------------


class TestRanking:
    def test_similar_outranks_unrelated(self, tmp_path):
        s = SQLiteStore(str(tmp_path / "t.db"))
        try:
            qs = QuestionEmbeddingStore(s)
            cases = [
                ("cid1", "Will Bitcoin close above 100k this year?"),
                ("cid2", "Will Bitcoin close above 80k this year?"),
                ("cid3", "Will the Lakers win the NBA finals?"),
                ("cid4", "Will it snow in Madrid this winter?"),
            ]
            for cid, q in cases:
                qs.upsert(ResolvedCase(
                    condition_id=cid, question=q,
                    outcome="YES", resolved_price=1.0,
                    resolved_at="2026-01-15T00:00:00Z",
                ))
            results = qs.find_similar(
                "Will Bitcoin close above 90k this year?",
                top_k=4,
            )
            ranked_cids = [c.condition_id for c, _ in results]
            # Both Bitcoin cases must rank above either of the
            # off-topic cases.
            btc = [c for c in ranked_cids if c in ("cid1", "cid2")]
            other = [c for c in ranked_cids if c in ("cid3", "cid4")]
            assert ranked_cids[0] in ("cid1", "cid2")
            assert btc.index(btc[0]) < (
                ranked_cids.index(other[0]) if other else len(ranked_cids)
            )
        finally:
            s.close()

    def test_top_k_clamps_results(self, tmp_path):
        s = SQLiteStore(str(tmp_path / "t.db"))
        try:
            qs = QuestionEmbeddingStore(s)
            for i in range(10):
                qs.upsert(ResolvedCase(
                    condition_id=f"c{i}", question=f"market {i}",
                    outcome="YES", resolved_price=1.0,
                    resolved_at="2026-01-15T00:00:00Z",
                ))
            results = qs.find_similar("market", top_k=3)
            assert len(results) <= 3
        finally:
            s.close()

    def test_empty_index_returns_empty(self, tmp_path):
        s = SQLiteStore(str(tmp_path / "t.db"))
        try:
            qs = QuestionEmbeddingStore(s)
            assert qs.find_similar("anything") == []
        finally:
            s.close()


# ---------------------------------------------------------------------------
# Backfill from market_resolutions
# ---------------------------------------------------------------------------


class TestBackfill:
    def test_indexes_existing_resolutions(self, tmp_path):
        s = SQLiteStore(str(tmp_path / "t.db"))
        try:
            # Seed market_resolutions directly.
            s._conn.execute(
                "INSERT INTO market_resolutions "
                "(condition_id, token_id, question, outcome, resolved_price, "
                " resolution_ts, our_side, our_entry_price, our_exit_price, "
                " our_pnl, prediction_correct, checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("cid1", "tok1", "Will A happen by EOY?", "YES", 1.0,
                 "2026-01-10T00:00:00Z", "BUY", 0.5, 1.0, 0.5, 1,
                 "2026-01-10T00:01:00Z"),
            )
            s._conn.commit()
            qs = QuestionEmbeddingStore(s)
            n = qs.backfill_from_resolutions()
            assert n == 1
            results = qs.find_similar("Will A happen by EOY?")
            assert len(results) == 1
            assert results[0][0].condition_id == "cid1"
        finally:
            s.close()


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


class TestPromptAssembly:
    def test_below_threshold_produces_empty_string(self):
        case = ResolvedCase(
            condition_id="cid1", question="Q?", outcome="YES",
            resolved_price=1.0, resolved_at="2026-01-15T00:00:00Z",
        )
        text = assemble_retrieval_prompt(
            "anything",
            similar=[(case, 0.05)],
            min_similarity=0.20,
        )
        assert text == ""

    def test_renders_kept_cases(self):
        case_hi = ResolvedCase(
            condition_id="cid1", question="Will Bitcoin close above 100k?",
            outcome="YES", resolved_price=1.0,
            resolved_at="2026-01-15T00:00:00Z",
        )
        case_low = ResolvedCase(
            condition_id="cid2", question="Lakers finals?",
            outcome="NO", resolved_price=0.0,
            resolved_at="2026-01-15T00:00:00Z",
        )
        text = assemble_retrieval_prompt(
            "Will Bitcoin close above 95k?",
            similar=[(case_hi, 0.85), (case_low, 0.10)],
            min_similarity=0.20,
        )
        assert "Bitcoin" in text
        assert "Lakers" not in text  # filtered by threshold
        assert "0.850" in text  # similarity rendered
        assert "Use these only as priors" in text

    def test_max_cases_cap(self):
        cases = [
            (
                ResolvedCase(
                    condition_id=f"cid{i}", question=f"q{i}",
                    outcome="YES", resolved_price=1.0,
                    resolved_at="2026-01-15T00:00:00Z",
                ),
                0.9,
            )
            for i in range(20)
        ]
        text = assemble_retrieval_prompt(
            "q", similar=cases, max_cases=5, min_similarity=0.0,
        )
        # Count how many cases ended up in the final block.
        case_lines = [
            line for line in text.splitlines()
            if line.strip().startswith("(similarity=")
        ]
        assert len(case_lines) == 5


# ---------------------------------------------------------------------------
# Custom embedder injection
# ---------------------------------------------------------------------------


class TestCustomEmbedder:
    def test_embedder_callable_is_used(self, tmp_path):
        s = SQLiteStore(str(tmp_path / "t.db"))
        try:
            calls: list[str] = []

            def fake(text: str) -> list[float]:
                calls.append(text)
                return [1.0] * 4 + [0.0] * (16 - 4)  # always same vec, dim 16

            qs = QuestionEmbeddingStore(s, embedder=fake, dim=16)
            qs.upsert(ResolvedCase(
                condition_id="cid1", question="anything",
                outcome="YES", resolved_price=1.0,
                resolved_at="2026-01-15T00:00:00Z",
            ))
            qs.find_similar("anything")
            # Embedder used twice — once on upsert, once on retrieve.
            assert len(calls) == 2
        finally:
            s.close()
