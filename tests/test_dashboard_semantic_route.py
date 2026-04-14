"""Tests for the dashboard /api/semantic/* endpoints.

Goal: verify that
* the endpoints don't crash on a fresh DB (no ``semantic_signals`` table),
* they expose rows the bot inserted via ``SQLiteStore.insert_semantic_signals``,
* and the summary aggregates by method/side correctly.
"""

from __future__ import annotations

import os
import tempfile

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from src.analysis.semantic_engine.types import (
    MarketRelation,
    RelationKind,
    SemanticMispricing,
    SyntheticPrice,
)
from src.storage.sqlite_store import SQLiteStore


def _mispricing(token_id="tA", side="BUY", method="structural_complement",
                net_edge=0.05, score=0.75):
    synth = SyntheticPrice(
        point=0.60, lower=0.59, upper=0.61, confidence=0.9,
        method=method, contributors=("other",), n_contributors=1,
        is_range_only=False,
    )
    rel = MarketRelation(
        target_token_id=token_id, sibling_token_id="other",
        kind=RelationKind.INVERSE_OUTCOME, confidence=0.99, reason="t",
    )
    return SemanticMispricing(
        token_id=token_id, condition_id="cX", question="Q?", category="politics",
        best_bid=0.53, best_ask=0.55, midpoint=0.54, spread=0.02,
        liquidity=5000.0, synthetic=synth, side=side,
        gross_edge=net_edge + 0.01, net_edge=net_edge, score=score,
        relations=(rel,), features={"signal_score": score, "net_edge": net_edge},
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Spin a FastAPI test client bound to an isolated temp DB."""
    db_path = tmp_path / "bot.db"
    # Init with schema (creates semantic_signals table).
    store = SQLiteStore(str(db_path))
    store.close()

    # Point the backend at our temp DB *before* importing it.
    monkeypatch.setenv("SQLITE_DB_PATH", str(db_path))
    import importlib
    import dashboard.backend.main as backend_main
    importlib.reload(backend_main)
    with TestClient(backend_main.app) as tc:
        yield tc, db_path


def test_signals_empty_on_fresh_db(client):
    tc, _ = client
    r = tc.get("/api/semantic/signals")
    assert r.status_code == 200
    body = r.json()
    assert body["signals"] == []
    assert body["table_exists"] is True  # store created table on init


def test_summary_empty_safe(client):
    tc, _ = client
    r = tc.get("/api/semantic/summary?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["by_method"] == []


def test_signals_exposes_bot_inserted_rows(client):
    tc, db_path = client
    store = SQLiteStore(str(db_path))
    store.insert_semantic_signals("2026-04-14T10:00:00", [
        _mispricing(method="structural_complement", score=0.80),
        _mispricing(method="weighted_avg_equivalent", score=0.55),
    ], mode="shadow")
    store.close()

    r = tc.get("/api/semantic/signals?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert len(body["signals"]) == 2
    # JSON blobs are decoded into real lists/dicts
    first = body["signals"][0]
    assert isinstance(first["relations"], list)
    assert isinstance(first["features"], dict)
    assert all(s["mode"] == "shadow" for s in body["signals"])


def test_summary_groups_by_method(client):
    tc, db_path = client
    store = SQLiteStore(str(db_path))
    # 2 structural, 1 textual → summary should show both methods.
    store.insert_semantic_signals("2026-04-14T10:00:00", [
        _mispricing(method="structural_complement", net_edge=0.05, score=0.80),
        _mispricing(method="structural_complement", net_edge=0.04, score=0.70),
        _mispricing(method="weighted_avg_equivalent", net_edge=0.03, score=0.55),
    ], mode="shadow")
    store.close()

    r = tc.get("/api/semantic/summary?days=90")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    methods = {m["method"]: m for m in body["by_method"]}
    assert methods["structural_complement"]["count"] == 2
    assert methods["weighted_avg_equivalent"]["count"] == 1
    # BUY default in _mispricing helper
    assert body["by_side"]["BUY"] == 3


def test_calibration_endpoint_empty_safe(client):
    tc, _ = client
    r = tc.get("/api/semantic/calibration?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["n_signals"] == 0
    assert body["n_matched"] == 0


def test_filter_by_mode(client):
    tc, db_path = client
    store = SQLiteStore(str(db_path))
    store.insert_semantic_signals("2026-04-14T10:00:00",
                                  [_mispricing()], mode="shadow")
    store.insert_semantic_signals("2026-04-14T10:01:00",
                                  [_mispricing()], mode="live")
    store.close()

    shadow = tc.get("/api/semantic/signals?mode=shadow").json()
    live = tc.get("/api/semantic/signals?mode=live").json()
    assert len(shadow["signals"]) == 1
    assert len(live["signals"]) == 1
    assert shadow["signals"][0]["mode"] == "shadow"
    assert live["signals"][0]["mode"] == "live"
