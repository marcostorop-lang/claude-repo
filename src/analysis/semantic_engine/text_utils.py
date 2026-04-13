"""Deterministic text utilities — stdlib only.

Why no NLP library?  A trading bot that *acts* on textual similarity must
never accept latent, non-reproducible inference.  Every function here is
deterministic, inspectable and testable.  Upgrades to heavier matchers
(sentence-transformers, rapidfuzz, etc.) can slot in behind these same
interfaces without touching the relation classifier.

Scope:
* ``normalize(text)`` — lowercase, strip punctuation, collapse whitespace,
  remove a tiny stoplist.  Preserves digits because date tokens and
  thresholds (e.g. "300bps", "Q3 2026") carry information.
* ``tokens(text)`` — ``normalize`` + whitespace split.
* ``jaccard(a, b)`` — token-set Jaccard similarity in [0, 1].
* ``fuzzy_ratio(a, b)`` — ``difflib.SequenceMatcher`` ratio on normalized text.
* ``contains_negation(text)`` — does the text use 'not', 'no', 'won\\'t' etc.
* ``extract_entities(text)`` — greedy capitalized n-gram extraction
  (pre-tokenisation, pre-lowering).  Meant as a cheap proxy for "who or
  what is this market about".
* ``date_overlap_days(a, b)`` — signed day delta between two ISO dates;
  returns ``None`` if either is unparseable.
"""

from __future__ import annotations

import re
from datetime import datetime
from difflib import SequenceMatcher

# Conservatively short stoplist — we want to keep enough signal that two
# questions about different subjects don't look similar by virtue of shared
# grammar words, without over-pruning domain tokens.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "in", "is", "it", "its", "of", "on", "or", "that",
    "the", "to", "will", "with", "than", "into", "during", "over", "per",
    "this", "these", "those", "be", "been", "being", "do", "does", "did",
})

# Tokens that flip polarity.  Order matters for tests: shortest substrings last.
_NEGATION_TOKENS = frozenset({
    "not", "no", "never", "without", "cannot", "can't", "won't", "shan't",
    "doesn't", "didn't", "isn't", "aren't", "wasn't", "weren't", "fail",
    "fails", "failed",
})

_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")
_CAPITALIZED_RUN_RE = re.compile(r"(?:\b[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*)")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Digits are preserved — dates and thresholds carry information.  Stopword
    removal happens in :func:`tokens` because ``normalize`` is also used by
    :func:`fuzzy_ratio` where stopwords do carry positional information.
    """
    if not text:
        return ""
    # Replace em/en dashes explicitly so words split cleanly.
    text = text.replace("—", " ").replace("–", " ").replace("/", " ")
    text = _PUNCT_RE.sub(" ", text.lower())
    text = _WS_RE.sub(" ", text).strip()
    return text


def tokens(text: str) -> list[str]:
    """Return a normalised token list with stopwords removed."""
    norm = normalize(text)
    if not norm:
        return []
    return [t for t in norm.split(" ") if t and t not in _STOPWORDS]


def jaccard(a: str, b: str) -> float:
    """Token-set Jaccard similarity of two strings in [0, 1]."""
    sa, sb = set(tokens(a)), set(tokens(b))
    if not sa and not sb:
        return 0.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def fuzzy_ratio(a: str, b: str) -> float:
    """``difflib.SequenceMatcher`` ratio on normalised text.

    Sensitive to word order — good for detecting near-identical questions
    phrased slightly differently, e.g. "Will X happen by Y?" vs
    "By Y, will X happen?".  Use in combination with Jaccard for a
    robust equivalence score: Jaccard catches paraphrases that reorder
    words aggressively, SequenceMatcher catches surface-level restatings.
    """
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def contains_negation(text: str) -> bool:
    """Return True if ``text`` contains a polarity-flipping token.

    Used to detect a possible INVERSE_OUTCOME relation even across different
    condition_ids — e.g. "Will X happen?" and "Will X NOT happen?".
    """
    toks = set(tokens(text)) | set(normalize(text).split(" "))
    return bool(toks & _NEGATION_TOKENS)


_SENTENCE_LEADERS = frozenset({
    "will", "does", "do", "did", "is", "are", "was", "were", "can", "could",
    "should", "shall", "has", "have", "had", "may", "might", "must", "would",
    "who", "what", "when", "where", "why", "how", "which",
})


def extract_entities(text: str) -> list[str]:
    """Greedy extraction of capitalized runs (pre-normalisation).

    Intentionally naive — catches proper nouns like "Donald Trump",
    "Federal Reserve", "Taylor Swift" as single entities without needing
    an NER model.  Filters out:

    * single-letter words,
    * all-caps ticker-like tokens (BTC, USD),
    * common sentence leaders (Will, Does, Is, …) at the start of a run —
      these are only capitalized because they begin a question.
    """
    if not text:
        return []
    matches = _CAPITALIZED_RUN_RE.findall(text)
    out: list[str] = []
    for m in matches:
        m = m.strip()
        if len(m) < 2:
            continue
        # Split off a sentence-leader word if present.  "Will Donald Trump"
        # → "Donald Trump".
        parts = m.split()
        while parts and parts[0].lower() in _SENTENCE_LEADERS:
            parts = parts[1:]
        if not parts:
            continue
        m = " ".join(parts)
        # Skip things that look like all-caps tickers (BTC, USD) — these
        # come back as noise because the whole question isn't capitalized.
        if m.isupper() and len(m) <= 5:
            continue
        if len(m) < 2:
            continue
        out.append(m)
    return out


def entity_overlap(a: str, b: str) -> float:
    """Fraction of shared entities (case-insensitive), in [0, 1]."""
    ea = {e.lower() for e in extract_entities(a)}
    eb = {e.lower() for e in extract_entities(b)}
    if not ea and not eb:
        return 0.0
    if not (ea & eb):
        return 0.0
    return len(ea & eb) / max(len(ea | eb), 1)


def date_overlap_days(a: str, b: str) -> int | None:
    """Absolute day delta between two ISO date strings.

    ``None`` on parse failure — callers must treat unknown dates as
    'no temporal signal' rather than 'same date'.  Accepts both
    ``YYYY-MM-DD`` and ``YYYY-MM-DDTHH:MM:SS`` forms.
    """
    da = _parse_iso(a)
    db = _parse_iso(b)
    if da is None or db is None:
        return None
    return abs((da - db).days)


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # fromisoformat supports both date and datetime in Python 3.11+
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        # Fallback: try just the date part
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
