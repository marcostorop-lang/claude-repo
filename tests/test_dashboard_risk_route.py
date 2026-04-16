"""Tests for the dashboard /api/risk endpoint.

Covers:
* Empty / freshly-bootstrapped DB returns zero-valued posture (no 500).
* Latest tick_stats row surfaces VaR/CVaR/worst_case correctly.
* Recent RISK_REJECTED rows are bucketed by reason (circuit breaker,
  temporal, volatility, slippage, …).
* sizing_trace block aggregates the per-feature multipliers from
  ENTRY_BUY decision rows (capital_efficiency_factor, bayes_size_mult,
  kelly_f_star, hour_allowed).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from src.storage.sqlite_store import SQLiteStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"
    store = SQLiteStore(str(db_path))
    store.close()
    monkeypatch.setenv("SQLITE_DB_PATH", str(db_path))
    import importlib
    import dashboard.backend.main as backend_main
    importlib.reload(backend_main)
    with TestClient(backend_main.app) as tc:
        yield tc, db_path


def test_risk_empty_db_safe(client):
    tc, _ = client
    r = tc.get("/api/risk")
    assert r.status_code == 200
    body = r.json()
    assert body["latest"]["var_95"] == 0.0
    assert body["latest"]["cvar_95"] == 0.0
    assert body["series"] == []
    assert body["rejections"]["total"] == 0
    assert body["sizing_trace"]["capital_efficiency_factor"]["n"] == 0


def test_risk_surfaces_latest_tick(client):
    tc, db_path = client
    store = SQLiteStore(str(db_path))
    store.insert_tick_stats(
        timestamp="2026-04-15T12:00:00", duration_s=1.0,
        markets_scanned=10, signals_generated=2, risk_rejections=1,
        trades_executed=1, open_positions=3, total_exposure=42.5,
        realised_pnl=0.0, unrealised_pnl=0.0, daily_pnl=0.0,
        var_95=12.34, cvar_95=18.50, worst_case=25.0,
    )
    store.insert_tick_stats(
        timestamp="2026-04-15T12:01:00", duration_s=1.0,
        markets_scanned=10, signals_generated=2, risk_rejections=0,
        trades_executed=0, open_positions=3, total_exposure=42.5,
        realised_pnl=0.0, unrealised_pnl=0.0, daily_pnl=0.0,
        var_95=15.0, cvar_95=20.0, worst_case=30.0,
    )
    store.close()

    body = tc.get("/api/risk").json()
    assert body["latest"]["var_95"] == 15.0  # newest row
    assert body["latest"]["cvar_95"] == 20.0
    assert body["latest"]["worst_case"] == 30.0
    # series ordered oldest → newest for charting
    assert [s["var_95"] for s in body["series"]] == [12.34, 15.0]


def test_risk_buckets_rejections(client):
    tc, db_path = client
    store = SQLiteStore(str(db_path))
    rejections = [
        ("Circuit breaker: daily loss $50.00 exceeds limit.", "circuit_breaker"),
        ("Temporal filter: hour 03 UTC below win-rate threshold 0.65.", "temporal_filter"),
        ("Volatility 0.0800 exceeds max 0.0500", "volatility"),
        ("Already have an open position for this token.", "duplicate_position"),
        ("Edge +0.0010 below min 0.0050 (category='politics').", "min_edge"),
        ("Stale price: snap=0.5500 vs book_mid=0.5200 (5.45%)", "stale_price"),
        ("Book spread 0.0800 exceeds max", "book_spread"),
        ("Book too thin: slippage 0.080 > 5.0%", "slippage"),
    ]
    for i, (reason, _bucket) in enumerate(rejections):
        store.insert_decision(
            timestamp=f"2026-04-15T12:{i:02d}:00",
            token_id=f"t{i}", condition_id=f"c{i}",
            action="RISK_REJECTED", reason=reason,
        )
    store.close()

    body = tc.get("/api/risk").json()
    assert body["rejections"]["total"] == len(rejections)
    by_bucket = body["rejections"]["by_bucket"]
    expected_buckets = {b for _, b in rejections}
    for bucket in expected_buckets:
        assert by_bucket.get(bucket, 0) >= 1, f"Missing bucket: {bucket}"


def test_sizing_trace_aggregates_entry_buy_features(client):
    tc, db_path = client
    store = SQLiteStore(str(db_path))
    # Three ENTRY_BUY rows with sizing_trace fields; one with hour_allowed=False.
    rows = [
        {"capital_efficiency_factor": 0.50, "bayes_size_mult": 0.80,
         "kelly_f_star": 0.10, "hour_allowed": True},
        {"capital_efficiency_factor": 0.25, "bayes_size_mult": 0.90,
         "kelly_f_star": 0.20, "hour_allowed": True},
        {"capital_efficiency_factor": 1.00, "bayes_size_mult": 1.00,
         "kelly_f_star": 0.05, "hour_allowed": False},
    ]
    for i, feats in enumerate(rows):
        store.insert_decision(
            timestamp=f"2026-04-15T13:{i:02d}:00",
            token_id=f"t{i}", condition_id=f"c{i}",
            action="ENTRY_BUY", reason="ok",
            features=feats,
        )
    store.close()

    body = tc.get("/api/risk").json()
    trace = body["sizing_trace"]
    assert trace["capital_efficiency_factor"]["n"] == 3
    assert trace["capital_efficiency_factor"]["min"] == 0.25
    assert trace["capital_efficiency_factor"]["max"] == 1.0
    assert trace["bayes_size_mult"]["n"] == 3
    assert trace["kelly_f_star"]["n"] == 3
    assert trace["hour_allowed"] == {"true": 2, "false": 1}
