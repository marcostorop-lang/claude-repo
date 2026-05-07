"""Tests for the Prometheus /metrics endpoint."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from src.storage.sqlite_store import SQLiteStore  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"
    state_path = tmp_path / "bot_state.json"
    SQLiteStore(str(db_path)).close()
    monkeypatch.setenv("SQLITE_DB_PATH", str(db_path))
    monkeypatch.setenv("BOT_STATE_PATH", str(state_path))
    import importlib
    import dashboard.backend.bot_state as bot_state_mod
    import dashboard.backend.main as backend_main
    importlib.reload(bot_state_mod)
    importlib.reload(backend_main)
    with TestClient(backend_main.app) as tc:
        yield tc, db_path, state_path


def test_metrics_endpoint_exists(client):
    tc, _, _ = client
    r = tc.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_metrics_contains_required_series(client):
    tc, _, _ = client
    body = tc.get("/metrics").text
    expected = [
        "bot_total_trades",
        "bot_open_positions",
        "bot_total_exposure_usd",
        "bot_daily_pnl_usd",
        "bot_realised_pnl_usd",
        "bot_fees_paid_usd",
        "bot_paper_friction_paid_usd",
        "bot_drawdown_pct",
        "bot_circuit_breaker_active",
        "bot_drawdown_breaker_active",
        "bot_last_tick_age_seconds",
    ]
    for name in expected:
        assert f"# HELP {name}" in body, f"missing HELP for {name}"
        assert f"# TYPE {name}" in body, f"missing TYPE for {name}"
        # Each metric also has a value line.
        assert any(
            line.startswith(name + " ") for line in body.splitlines()
        ), f"missing value line for {name}"


def test_metrics_reflects_state_file(client):
    tc, _, state_path = client
    state_path.write_text(json.dumps({
        "timestamp": "2026-05-07T12:00:00",
        "circuit_breaker_active": True,
        "drawdown_breaker_active": False,
        "drawdown_pct": 0.15,
        "daily_pnl": -3.5,
        "portfolio": {
            "open_positions": 2,
            "total_exposure": 80.0,
            "realised_pnl": 12.34,
            "fees_paid": 1.0,
            "paper_friction_paid": 0.5,
        },
    }))
    body = tc.get("/metrics").text
    assert "bot_circuit_breaker_active 1.0" in body
    assert "bot_drawdown_breaker_active 0.0" in body
    assert "bot_open_positions 2.0" in body
    assert "bot_total_exposure_usd 80.0" in body
    assert "bot_daily_pnl_usd -3.5" in body
    assert "bot_realised_pnl_usd 12.34" in body
    assert "bot_fees_paid_usd 1.0" in body
    assert "bot_paper_friction_paid_usd 0.5" in body
    assert "bot_drawdown_pct 0.15" in body
