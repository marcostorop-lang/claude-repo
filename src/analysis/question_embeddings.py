"""
Retrieval-augmented Claude prompts: embed past Polymarket questions
together with their resolved outcome, then at inference time retrieve
the top-K most similar resolved questions and inject them into the
Claude prompt as "analogous resolved cases".

Why this matters
----------------
A vanilla "give Claude a question and ask for a probability" call
treats every question as if it were the first one.  Claude has no
memory of how *similar* questions in Polymarket's own history have
resolved — and there is real signal there.  Two resolved markets
about "will X happen by date Y" with similar wording, similar
event-window length, and similar pre-resolution price drift carry
information about how the next one is likely to resolve.

This module is the retrieval half.  It is intentionally
**dependency-free at the default level**: the built-in embedder is a
hashed-bag-of-words producing deterministic vectors that sentence
embeddings would clearly outperform but that *do* let the cosine
similarity ranking work end-to-end without an external service.  An
operator who wants real semantic similarity can plug in an Anthropic
or local embedder via the ``embedder=`` constructor argument; the
contract is just ``Callable[[str], list[float]]``.

What this module does *not* do
------------------------------
* Train or fine-tune anything.
* Call any external service unless the operator explicitly attaches
  an embedder that does so.
* Modify the live trading path.  Retrieval is plumbed into the
  semantic strategy's prompt assembly only — orders still flow
  through the same gates.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import struct
from dataclasses import dataclass
from typing import Callable, Sequence

logger = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# Default embedder: deterministic hashed-bag-of-words.
# ---------------------------------------------------------------------------


def hashed_bow_embedding(text: str, *, dim: int = 256) -> list[float]:
    """Produce a deterministic dense vector from raw text.

    Each lowercase token contributes ``+1`` to the bucket selected by
    a hash of the token.  The vector is L2-normalised so cosine
    similarity reduces to a dot product.  Empty text → zero vector.

    Quality-wise this is strictly worse than a sentence-transformer
    embedding — the point is **default availability**: no external
    service needed, no model weights to download, deterministic in
    tests.  Operators who care about retrieval quality plug in a
    real embedder via ``QuestionEmbeddingStore.embedder=``.
    """
    if not text or dim <= 0:
        return [0.0] * max(dim, 0)
    vec = [0.0] * dim
    for tok in _TOKEN_RE.findall(text.lower()):
        h = hashlib.md5(tok.encode("utf-8")).digest()
        idx = struct.unpack("<I", h[:4])[0] % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity for two equal-length vectors.

    Returns 0.0 on length mismatch or all-zero inputs (rather than
    raising) so the retrieval path never has to special-case them.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class ResolvedCase:
    """One row of the retrieval index."""

    condition_id: str
    question: str
    outcome: str          # e.g. "YES" / "NO" or whichever Polymarket reported
    resolved_price: float  # 0 or 1 typically; in [0, 1] for partial settlements
    resolved_at: str      # ISO-8601 UTC

    def as_prompt_line(self) -> str:
        """Compact one-liner suitable for inclusion in a Claude prompt."""
        truncated_q = self.question.strip()
        if len(truncated_q) > 200:
            truncated_q = truncated_q[:197] + "..."
        return (
            f"- [{self.resolved_at[:10]}] {truncated_q} "
            f"→ resolved {self.outcome} @ {self.resolved_price:.2f}"
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class QuestionEmbeddingStore:
    """Persistent index of resolved Polymarket questions for retrieval.

    Reads/writes ``question_embeddings`` in the bot's SQLite store.
    The embedder is injected so tests can use the deterministic
    default and production can plug in a real model.  Vectors are
    serialised to binary (raw little-endian float32) to keep the
    SQLite blob small and parsable from any language.
    """

    DEFAULT_DIM = 256

    def __init__(
        self,
        store,
        *,
        embedder: Callable[[str], list[float]] | None = None,
        dim: int = DEFAULT_DIM,
    ) -> None:
        self.store = store
        self.dim = dim
        self.embedder = embedder or (lambda text: hashed_bow_embedding(text, dim=dim))

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def upsert(self, case: ResolvedCase) -> None:
        vec = self.embedder(case.question or "")
        if len(vec) != self.dim:
            logger.warning(
                "Embedder returned dim=%d, expected %d — skipping case %s.",
                len(vec), self.dim, case.condition_id[:12],
            )
            return
        self.store.upsert_question_embedding(
            condition_id=case.condition_id,
            question=case.question,
            outcome=case.outcome,
            resolved_price=case.resolved_price,
            resolved_at=case.resolved_at,
            dim=self.dim,
            embedding=_pack_vec(vec),
        )

    def backfill_from_resolutions(self) -> int:
        """One-shot indexing of every resolved market in ``market_resolutions``.

        Returns the number of rows indexed.  Idempotent: re-running it
        upserts existing rows with fresh embeddings (useful when the
        operator swaps the embedder for a better one).
        """
        rows = self.store.get_resolutions()
        n = 0
        for r in rows:
            try:
                cid = r.get("condition_id") or ""
                question = r.get("question") or ""
                outcome = r.get("outcome") or ""
                price = float(r.get("resolved_price") or 0.0)
                ts = r.get("resolution_ts") or r.get("checked_at") or ""
                if not cid or not question or not ts:
                    continue
                self.upsert(ResolvedCase(
                    condition_id=cid, question=question,
                    outcome=outcome, resolved_price=price, resolved_at=ts,
                ))
                n += 1
            except Exception:
                logger.exception("Failed to embed resolution %s", r.get("condition_id"))
        return n

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def find_similar(self, question: str, *, top_k: int = 5) -> list[tuple[ResolvedCase, float]]:
        """Return ``(case, similarity)`` pairs sorted by descending similarity.

        Cold start (no rows in the index) returns an empty list.  The
        default embedder is a deterministic hash, so identical questions
        score 1.0 and unrelated questions score near zero.
        """
        if not question.strip() or top_k <= 0:
            return []
        target = self.embedder(question)
        if len(target) != self.dim:
            return []
        rows = self.store.get_question_embeddings()
        scored: list[tuple[ResolvedCase, float]] = []
        for r in rows:
            try:
                vec = _unpack_vec(r["embedding"], dim=int(r["dim"]))
                if len(vec) != self.dim:
                    continue
                sim = cosine_similarity(target, vec)
                if sim <= 0:
                    continue
                case = ResolvedCase(
                    condition_id=r["condition_id"],
                    question=r["question"],
                    outcome=r["outcome"],
                    resolved_price=float(r["resolved_price"]),
                    resolved_at=r["resolved_at"],
                )
                scored.append((case, sim))
            except Exception:
                continue
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return scored[:top_k]


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def assemble_retrieval_prompt(
    question: str,
    similar: list[tuple[ResolvedCase, float]],
    *,
    min_similarity: float = 0.20,
    max_cases: int = 5,
) -> str:
    """Render the retrieval block to inject into a Claude prompt.

    Returns an empty string when no case clears ``min_similarity`` —
    the caller can then skip the section entirely rather than
    inflating the prompt with noise.
    """
    kept = [(c, s) for c, s in similar[:max_cases] if s >= min_similarity]
    if not kept:
        return ""
    lines = [
        "Analogous resolved Polymarket markets (most similar first):",
    ]
    for case, sim in kept:
        lines.append(f"  (similarity={sim:.3f}) {case.as_prompt_line()}")
    lines.append(
        "Use these only as priors about how *similar* questions have "
        "historically resolved; the live market may price these "
        "differently for good reason."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Vector (de)serialisation
# ---------------------------------------------------------------------------


def _pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack_vec(blob: bytes, *, dim: int) -> list[float]:
    if not blob:
        return []
    expected = dim * 4
    if len(blob) != expected:
        return []
    return list(struct.unpack(f"<{dim}f", blob))
