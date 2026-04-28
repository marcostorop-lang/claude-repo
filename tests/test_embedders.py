"""Tests for the optional embedder backends.

These tests verify the **factory contract** — they do not download
sentence-transformer model weights or call Voyage's API.  The
``sentence_transformers`` and ``voyageai`` modules are stubbed via
``sys.modules`` so the wrapper is exercised end-to-end with
deterministic fakes.
"""

from __future__ import annotations

import sys
import types

import pytest

from src.analysis.embedders import (
    build_embedder,
    make_sentence_transformer_embedder,
    make_voyage_embedder,
)


# ---------------------------------------------------------------------------
# Helpers to install / restore fake modules without touching real installs.
# ---------------------------------------------------------------------------


class _FakeSentenceTransformer:
    """Stand-in for ``sentence_transformers.SentenceTransformer``."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._dim = 8

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim

    def encode(self, text, normalize_embeddings: bool = True):
        # Deterministic embedding: hash to a fixed-length vector,
        # L2-normalised so identical text produces identical vectors
        # and is comparable across calls.
        import math
        vec = [0.0] * self._dim
        for i, ch in enumerate(text or ""):
            vec[i % self._dim] += (ord(ch) % 7) / 7.0
        n = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / n for v in vec]


@pytest.fixture
def fake_sentence_transformers(monkeypatch):
    """Install a fake ``sentence_transformers`` in ``sys.modules``."""
    mod = types.ModuleType("sentence_transformers")
    mod.SentenceTransformer = _FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)
    yield mod


# ---------------------------------------------------------------------------
# sentence-transformers wrapper
# ---------------------------------------------------------------------------


class TestSentenceTransformerWrapper:
    def test_factory_returns_embedder_and_dim(self, fake_sentence_transformers):
        emb, dim = make_sentence_transformer_embedder("any-model")
        assert dim == 8
        v = emb("Will Bitcoin close above 100k?")
        assert len(v) == 8
        assert all(isinstance(x, float) for x in v)

    def test_empty_text_returns_zero_vector(self, fake_sentence_transformers):
        emb, dim = make_sentence_transformer_embedder("any-model")
        assert emb("") == [0.0] * dim

    def test_missing_dependency_raises_focused_error(self, monkeypatch):
        # Delete any cached real or fake module so the import fails.
        monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
        # And block re-import of the real package by injecting a meta
        # path finder that pretends it's not installed.
        import importlib.abc
        import importlib.machinery

        class _Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name == "sentence_transformers" or name.startswith(
                    "sentence_transformers."
                ):
                    raise ModuleNotFoundError(name)
                return None

        blocker = _Blocker()
        monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
        with pytest.raises(ImportError) as exc_info:
            make_sentence_transformer_embedder("any-model")
        assert "pip install sentence-transformers" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Voyage wrapper (mocked)
# ---------------------------------------------------------------------------


class _FakeVoyageResp:
    def __init__(self, vectors):
        self.embeddings = vectors


class _FakeVoyageClient:
    def __init__(self, *args, **kwargs):
        self.calls: list[tuple[list[str], str]] = []

    def embed(self, texts, model: str, input_type: str = "document"):
        self.calls.append((texts, model))
        # 4-dim deterministic per text: char count, length-mod, etc.
        out = []
        for t in texts:
            n = len(t)
            out.append([float(n), float(n % 3), 1.0 if t else 0.0, 0.5])
        return _FakeVoyageResp(out)


@pytest.fixture
def fake_voyageai(monkeypatch):
    mod = types.ModuleType("voyageai")
    mod.Client = _FakeVoyageClient
    monkeypatch.setitem(sys.modules, "voyageai", mod)
    yield mod


class TestVoyageWrapper:
    def test_factory_requires_api_key(self, fake_voyageai, monkeypatch):
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="VOYAGE_API_KEY"):
            make_voyage_embedder()

    def test_factory_returns_embedder_and_dim(self, fake_voyageai, monkeypatch):
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        emb, dim = make_voyage_embedder()
        assert dim == 4  # set by the fake's probe response
        v = emb("hello")
        assert len(v) == 4

    def test_empty_text_skips_api_call(self, fake_voyageai, monkeypatch):
        # The fake doesn't enforce this; we assert the embedder
        # returns a zero vector without proxying the empty-string
        # call to the API (saves cost in production).
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        emb, dim = make_voyage_embedder()
        assert emb("") == [0.0] * dim


# ---------------------------------------------------------------------------
# build_embedder dispatcher
# ---------------------------------------------------------------------------


class TestBuildEmbedder:
    def test_default_returns_none_and_256(self):
        emb, dim = build_embedder()
        assert emb is None
        assert dim == 256

    def test_aliases(self):
        for name in ("hashed_bow", "hashed-bow", "default", ""):
            emb, dim = build_embedder(name)
            assert emb is None
            assert dim == 256

    def test_sentence_transformer_alias(self, fake_sentence_transformers):
        emb, dim = build_embedder("st")
        assert emb is not None
        assert dim == 8

    def test_unknown_name_raises(self):
        with pytest.raises(ValueError, match="Unknown embedder"):
            build_embedder("babelfish")


# ---------------------------------------------------------------------------
# Plug-in into QuestionEmbeddingStore
# ---------------------------------------------------------------------------


class TestPlugIntoStore:
    def test_sentence_transformer_round_trip(self, fake_sentence_transformers, tmp_path):
        from src.analysis.question_embeddings import (
            QuestionEmbeddingStore, ResolvedCase,
        )
        from src.storage.sqlite_store import SQLiteStore
        s = SQLiteStore(str(tmp_path / "t.db"))
        try:
            emb, dim = make_sentence_transformer_embedder("any")
            qs = QuestionEmbeddingStore(s, embedder=emb, dim=dim)
            qs.upsert(ResolvedCase(
                condition_id="cid1",
                question="Will A happen by EOY?",
                outcome="YES", resolved_price=1.0,
                resolved_at="2026-01-15T00:00:00Z",
            ))
            results = qs.find_similar("Will A happen by EOY?")
            # Identical question → similarity 1.0 (within float
            # rounding) regardless of which embedder we use, as long
            # as it is deterministic and L2-normalised.
            assert len(results) == 1
            assert abs(results[0][1] - 1.0) < 1e-5
        finally:
            s.close()
