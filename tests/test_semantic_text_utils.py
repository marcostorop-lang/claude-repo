"""Unit tests for stdlib-only text utilities used by the semantic engine."""

from __future__ import annotations

import pytest

from src.analysis.semantic_engine.text_utils import (
    contains_negation,
    date_overlap_days,
    entity_overlap,
    extract_entities,
    fuzzy_ratio,
    jaccard,
    normalize,
    tokens,
)


class TestNormalize:
    def test_lowercases(self):
        assert normalize("Hello World") == "hello world"

    def test_strips_punctuation_but_keeps_digits(self):
        assert normalize("Will BTC hit $100,000 in 2026?") == "will btc hit 100 000 in 2026"

    def test_handles_em_dash(self):
        assert normalize("Trump — the candidate") == "trump the candidate"

    def test_collapses_whitespace(self):
        assert normalize("a\t\nb   c") == "a b c"

    def test_empty(self):
        assert normalize("") == ""


class TestTokens:
    def test_drops_stopwords(self):
        assert "the" not in tokens("Will the market close up today?")
        assert "will" not in tokens("Will the market close up today?")

    def test_preserves_content_words(self):
        toks = tokens("Will Trump win the 2028 election?")
        assert "trump" in toks
        assert "win" in toks
        assert "2028" in toks
        assert "election" in toks


class TestJaccard:
    def test_identical_is_one(self):
        q = "Will the Fed cut rates in December?"
        assert jaccard(q, q) == 1.0

    def test_disjoint_is_zero(self):
        assert jaccard("Will Apple ship a car?", "Does Taylor Swift tour Europe?") == 0.0

    def test_partial_overlap(self):
        a = "Will Trump win the 2028 election?"
        b = "Will Trump lose the 2028 election?"
        v = jaccard(a, b)
        assert 0.3 < v < 1.0


class TestFuzzyRatio:
    def test_identical_is_one(self):
        assert fuzzy_ratio("hello world", "hello world") == 1.0

    def test_similar_high(self):
        assert fuzzy_ratio(
            "Will BTC close above 100k on Dec 31 2026?",
            "BTC above 100k on Dec 31 2026",
        ) > 0.60  # permutations hurt SequenceMatcher a bit

    def test_unrelated_low(self):
        # SequenceMatcher picks up incidental shared characters; we only
        # require that unrelated strings score clearly below the
        # near_equivalent threshold (0.80).
        assert fuzzy_ratio("BTC to the moon", "Messi wins the Ballon d'Or") < 0.50


class TestContainsNegation:
    @pytest.mark.parametrize("t", [
        "Will Trump NOT win?",
        "Will X fail to deliver?",
        "Will X not happen by EOY?",
        "Will X never occur?",
    ])
    def test_positive(self, t):
        assert contains_negation(t) is True

    @pytest.mark.parametrize("t", [
        "Will Trump win?",
        "Will Apple ship the car?",
        "Will the Fed cut rates?",
    ])
    def test_negative(self, t):
        assert contains_negation(t) is False


class TestExtractEntities:
    def test_two_word_proper_noun(self):
        ents = extract_entities("Will Donald Trump win?")
        assert "Donald Trump" in ents

    def test_multi_entity(self):
        ents = extract_entities("Will Federal Reserve raise rates before Taylor Swift tour?")
        assert any("Federal Reserve" in e for e in ents)
        assert any("Taylor Swift" in e for e in ents)

    def test_filters_short_tickers(self):
        ents = extract_entities("Will BTC hit ATH before Bitcoin Corporation IPOs?")
        # All-caps short tokens are filtered.
        assert "BTC" not in ents
        # Proper multi-word company name survives.
        assert any("Bitcoin" in e for e in ents)


class TestEntityOverlap:
    def test_full_overlap(self):
        assert entity_overlap("Trump wins", "Trump loses") == 1.0

    def test_no_overlap(self):
        assert entity_overlap("Trump wins", "Messi wins") == 0.0

    def test_partial(self):
        v = entity_overlap(
            "Will Donald Trump or Joe Biden win?",
            "Will Donald Trump beat Kamala Harris?",
        )
        assert 0.0 < v < 1.0


class TestDateOverlapDays:
    def test_same_date(self):
        assert date_overlap_days("2026-01-01", "2026-01-01") == 0

    def test_one_month(self):
        assert date_overlap_days("2026-01-01", "2026-02-01") == 31

    def test_with_time(self):
        assert date_overlap_days("2026-01-01T12:00:00Z", "2026-01-02T00:00:00Z") == 0 \
            or date_overlap_days("2026-01-01T12:00:00Z", "2026-01-02T00:00:00Z") == 1

    def test_unparseable_returns_none(self):
        assert date_overlap_days("", "2026-01-01") is None
        assert date_overlap_days("garbage", "2026-01-01") is None
