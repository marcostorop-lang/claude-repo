"""Tests for the Claude oracle JSON parsing (no API calls)."""

from __future__ import annotations

import pytest

from bot.core.claude_oracle import _extract_json, _parse_estimate, _parse_relations


class TestExtractJson:
    def test_clean_json(self):
        r = _extract_json('{"probability": 0.65}')
        assert r == {"probability": 0.65}

    def test_json_with_preamble(self):
        r = _extract_json('Here is my analysis:\n{"probability": 0.7, "confidence": 0.8}')
        assert r is not None
        assert r["probability"] == 0.7

    def test_json_array(self):
        r = _extract_json('[{"a": 1}, {"a": 2}]')
        assert isinstance(r, list)
        assert len(r) == 2

    def test_invalid_returns_none(self):
        assert _extract_json("no json here") is None

    def test_nested_json(self):
        text = 'blah {"outer": {"inner": 42}} blah'
        r = _extract_json(text)
        assert r is not None


class TestParseEstimate:
    def test_valid_estimate(self):
        raw = '{"probability": 0.72, "confidence": 0.8, "reasoning": "test", "key_factors": ["a"], "edge_direction": "UNDER"}'
        e = _parse_estimate(raw)
        assert e is not None
        assert e.probability == pytest.approx(0.72)
        assert e.confidence == pytest.approx(0.8)
        assert e.edge_direction == "UNDER"

    def test_clamped_probability(self):
        raw = '{"probability": 1.5, "confidence": 0.5, "reasoning": "x"}'
        e = _parse_estimate(raw)
        assert e is not None
        assert e.probability == 0.99

    def test_missing_probability(self):
        raw = '{"confidence": 0.5}'
        e = _parse_estimate(raw)
        assert e is None

    def test_garbage_returns_none(self):
        assert _parse_estimate("not json") is None


class TestParseRelations:
    def test_valid_relation(self):
        raw = '[{"market_a_index": 1, "market_b_index": 2, "relation": "implies", "explanation": "test", "constraint_violation": 0.05, "confidence": 0.8}]'
        markets = [
            {"condition_id": "c1", "question": "Q1"},
            {"condition_id": "c2", "question": "Q2"},
        ]
        rels = _parse_relations(raw, markets)
        assert len(rels) == 1
        assert rels[0].market_a_id == "c1"
        assert rels[0].relation == "implies"

    def test_out_of_bounds_index(self):
        raw = '[{"market_a_index": 5, "market_b_index": 1, "relation": "x", "explanation": "", "constraint_violation": 0, "confidence": 0}]'
        markets = [{"condition_id": "c1", "question": "Q1"}]
        rels = _parse_relations(raw, markets)
        assert len(rels) == 0

    def test_empty_array(self):
        assert _parse_relations("[]", []) == []
