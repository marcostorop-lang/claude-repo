"""
Optional embedder backends for the retrieval-augmented Claude prompt.

The default embedder in ``src/analysis/question_embeddings.py`` is a
deterministic hashed-bag-of-words.  It works without dependencies and
keeps tests reproducible, but it is *strictly worse* than a real
sentence embedding for production retrieval.  This module is the
plug for upgrading.

Two backends are exposed:

* ``make_sentence_transformer_embedder`` — uses a local
  ``sentence-transformers`` model (e.g. ``all-MiniLM-L6-v2``).  Free
  per-inference once the model is downloaded, ~10 ms/text on CPU,
  no external service required.  This is the recommended starting
  point.

* ``make_voyage_embedder`` — calls Voyage AI's hosted embeddings
  endpoint (Anthropic's recommended embeddings provider; Anthropic
  itself does not ship an embeddings API).  Highest quality, costs
  ~$0.0001 per question.  Requires ``VOYAGE_API_KEY`` and a
  ``voyageai`` install.  Only worth turning on once retrieval has
  shown empirical lift with the local model.

Both factories are *lazy* about their imports — calling them when
the optional library is not installed raises a focused
``ImportError`` with the install command, rather than crashing at
module-load time.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local sentence-transformers
# ---------------------------------------------------------------------------


def make_sentence_transformer_embedder(
    model_name: str = "all-MiniLM-L6-v2",
) -> tuple[Callable[[str], list[float]], int]:
    """Build a ``(embedder, dim)`` pair backed by sentence-transformers.

    Parameters
    ----------
    model_name :
        Any model name compatible with ``SentenceTransformer``.
        ``all-MiniLM-L6-v2`` is a good default: ~80MB, dim 384,
        strong on short English text.

    Returns
    -------
    (embedder, dim) :
        ``embedder`` is a callable mapping ``str → list[float]`` of
        length ``dim``.  Plug straight into
        ``QuestionEmbeddingStore(embedder=…, dim=…)``.

    Raises
    ------
    ImportError :
        With a focused message including the install command when
        ``sentence-transformers`` is not available.
    """
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via mocks
        raise ImportError(
            "sentence-transformers is not installed. "
            "Install it with `pip install sentence-transformers` to use "
            f"local embeddings ({model_name}).",
        ) from exc

    model = SentenceTransformer(model_name)
    dim = int(model.get_sentence_embedding_dimension())

    def _embed(text: str) -> list[float]:
        if not text:
            return [0.0] * dim
        # ``encode`` returns a numpy array; normalise to a plain list
        # so the rest of the pipeline (binary serialisation, cosine)
        # has zero numpy dependency.
        vec = model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec]

    return _embed, dim


# ---------------------------------------------------------------------------
# Hosted Voyage AI embeddings (Anthropic's recommended provider)
# ---------------------------------------------------------------------------


def make_voyage_embedder(
    *,
    model: str = "voyage-3",
    api_key_env: str = "VOYAGE_API_KEY",
) -> tuple[Callable[[str], list[float]], int]:
    """Build a ``(embedder, dim)`` pair backed by Voyage AI's hosted API.

    Anthropic does not ship an embeddings endpoint of its own; their
    docs route to Voyage AI for production-quality embeddings.

    Raises
    ------
    ImportError :
        When ``voyageai`` is not installed.
    RuntimeError :
        When the API key env var is missing.
    """
    try:
        import voyageai  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via mocks
        raise ImportError(
            "voyageai is not installed.  Install it with "
            "`pip install voyageai` to use hosted embeddings.",
        ) from exc

    key = os.getenv(api_key_env, "").strip()
    if not key:
        raise RuntimeError(
            f"{api_key_env} is not set; cannot use the Voyage embedder.",
        )

    client = voyageai.Client(api_key=key)
    # voyage-3 is 1024-dim; we record the dim once at construction so
    # callers don't need to know the model's specifics.
    probe = client.embed(["dim probe"], model=model, input_type="document")
    dim = len(probe.embeddings[0])

    def _embed(text: str) -> list[float]:
        if not text:
            return [0.0] * dim
        resp = client.embed([text], model=model, input_type="document")
        return [float(x) for x in resp.embeddings[0]]

    return _embed, dim


# ---------------------------------------------------------------------------
# Convenience: pick a backend by name
# ---------------------------------------------------------------------------


def build_embedder(
    name: str = "hashed_bow",
    *,
    sentence_transformer_model: str = "all-MiniLM-L6-v2",
    voyage_model: str = "voyage-3",
) -> tuple[Callable[[str], list[float]] | None, int]:
    """Build an embedder by name.  Used by CLI commands.

    ``name`` values:
      * ``"hashed_bow"``   → ``(None, 256)`` (signal: use the default).
      * ``"sentence_transformer"`` / ``"st"``
      * ``"voyage"``

    Returns ``(None, dim)`` for the hashed_bow case so the caller
    can keep using ``QuestionEmbeddingStore``'s default embedder.
    """
    name = (name or "").lower().strip()
    if name in ("", "hashed_bow", "hashed-bow", "default"):
        return None, 256
    if name in ("sentence_transformer", "sentence-transformer", "st"):
        emb, dim = make_sentence_transformer_embedder(sentence_transformer_model)
        return emb, dim
    if name == "voyage":
        emb, dim = make_voyage_embedder(model=voyage_model)
        return emb, dim
    raise ValueError(
        f"Unknown embedder '{name}'.  "
        "Valid: 'hashed_bow', 'sentence_transformer', 'voyage'.",
    )
