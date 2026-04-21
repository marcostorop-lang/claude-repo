"""Tests for the healthcheck HTTP server."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from bot.core import healthcheck


_PORT = 8791


@pytest.fixture(scope="module")
def server():
    """Module-scoped server to avoid TIME_WAIT port issues between tests."""
    thread = healthcheck.start_in_thread(host="127.0.0.1", port=_PORT)
    time.sleep(0.1)  # give the server a moment to bind
    yield _PORT


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_health_endpoint_returns_snapshot(server):
    healthcheck.update_state(equity=1234.5, halted=False, cycle_count=5)
    status, body = _get(server, "/health")
    assert status == 200
    data = json.loads(body)
    assert data["equity"] == 1234.5
    assert data["cycle_count"] == 5
    assert "uptime_s" in data


def test_ready_endpoint_200_when_not_halted(server):
    healthcheck.update_state(halted=False)
    status, body = _get(server, "/ready")
    assert status == 200
    assert json.loads(body)["ready"] is True


def test_ready_endpoint_503_when_halted(server):
    healthcheck.update_state(halted=True)
    status, body = _get(server, "/ready")
    assert status == 503
    assert json.loads(body)["ready"] is False


def test_metrics_endpoint_prometheus_format(server):
    healthcheck.update_state(halted=False, equity=500, daily_pnl=-5, positions=2)
    status, body = _get(server, "/metrics")
    assert status == 200
    assert "bot_equity_usd 500" in body
    assert "bot_daily_pnl_usd -5" in body
    assert "bot_positions_open 2" in body
    assert "# HELP" in body
    assert "# TYPE" in body


def test_unknown_path_404(server):
    status, _ = _get(server, "/nope")
    assert status == 404


def test_snapshot_includes_uptime():
    snap = healthcheck.snapshot()
    assert "uptime_s" in snap
    assert snap["uptime_s"] >= 0
