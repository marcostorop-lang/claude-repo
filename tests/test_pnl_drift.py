"""Tests for the realised-vs-predicted P&L drift detector.

Covers:
* Cold-start (no trades, fewer than min_samples) → drift_detected=False.
* Healthy strategy (realised ≈ predicted) → no drift flagged.
* Decayed model (realised << predicted, ratio below floor) →
  drift_detected=True with a useful reason string.
* Trades without an ``edge`` feature contribute to win-rate but not
  to the predicted-PnL sum (pure-momentum / mean-rev safety).
* Dashboard endpoint surfaces the verdict over the calibration table.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from src.analysis.pnl_drift import detect_drift
from src.storage.sqlite_store import SQLiteStore


def _trade(*, pnl, confidence=0.7, entry_price=0.50,
           edge=None, size=10.0):
    feats = {}
    if edge is not None:
        feats["edge"] = edge
        feats["filled_size"] = size
    return {
        "pnl": pnl,
        "confidence": confidence,
        "entry_price": entry_price,
        "features": json.dumps(feats),
    }


# -- pure function ---------------------------------------------------------


class TestDetectDrift:
    def test_empty_returns_cold_start(self):
        v = detect_drift([])
        assert v.n_samples == 0
        assert v.drift_detected is False
        assert "No closed" in v.reason

    def test_below_min_samples_is_cold_start(self):
        # 5 trades, min_samples=30 → fail-safe, no flag even if ratio is 0.
        trades = [_trade(pnl=-5.0, edge=0.05) for _ in range(5)]
        v = detect_drift(trades, min_samples=30)
        assert v.drift_detected is False
        assert "Cold-start" in v.reason

    def test_healthy_strategy_no_drift(self):
        # Predicted edge = 0.05, size = 10 → predicted PnL = $0.50 / trade.
        # Realised PnL ≈ $0.50 / trade → ratio ≈ 1.0 (above floor 0.5).
        trades = [_trade(pnl=0.50, edge=0.05) for _ in range(40)]
        v = detect_drift(trades, min_samples=30, ratio_floor=0.5)
        assert v.drift_detected is False
        assert v.realisation_ratio == pytest.approx(1.0, rel=1e-6)
        assert v.realised_win_rate == pytest.approx(1.0)

    def test_decayed_model_flags_drift(self):
        # Predicted: 30 * $0.50 = $15 of edge.  Realised: 30 * -$0.20 = -$6.
        # Ratio = -6/15 = -0.4 → below floor 0.5 → drift!
        trades = [_trade(pnl=-0.20, edge=0.05) for _ in range(30)]
        v = detect_drift(trades, min_samples=30, ratio_floor=0.5)
        assert v.drift_detected is True
        assert v.realisation_ratio < 0.5
        assert "below floor" in v.reason

    def test_trades_without_edge_skipped_from_predicted(self):
        # 20 momentum trades (no edge in features) + 30 edge-tagged
        # losing trades.  Drift verdict only considers the 30 edge ones.
        edge_trades = [_trade(pnl=-0.20, edge=0.05) for _ in range(30)]
        no_edge = [_trade(pnl=0.10) for _ in range(20)]
        v = detect_drift(edge_trades + no_edge, min_samples=30, ratio_floor=0.5)
        # Win rate counts ALL 50 trades (20 winners / 50 = 0.40).
        assert v.realised_win_rate == pytest.approx(20 / 50)
        assert v.n_samples == 50
        # Drift uses only the 30 edge-tagged ones → still flagged.
        assert v.drift_detected is True

    def test_zero_predicted_doesnt_divide_by_zero(self):
        trades = [_trade(pnl=1.0) for _ in range(40)]  # no edge anywhere
        v = detect_drift(trades, min_samples=30)
        assert v.realisation_ratio == 0.0
        assert v.drift_detected is False  # can't drift without a prediction


# -- dashboard endpoint -----------------------------------------------------


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


class TestDriftEndpoint:
    def test_drift_empty_db_safe(self, client):
        tc, _ = client
        body = tc.get("/api/drift").json()
        assert body["n_samples"] == 0
        assert body["drift_detected"] is False
        assert body["realisation_ratio"] == 0.0

    def test_drift_surfaces_verdict_from_calibration(self, client):
        tc, db_path = client
        store = SQLiteStore(str(db_path))
        # Insert 30 closed calibration rows with predicted edge but
        # bad realised PnL (model decay scenario).
        for i in range(30):
            entry_id = store.insert_calibration_entry(
                entry_timestamp=f"2026-04-15T12:{i:02d}:00",
                token_id=f"t{i}", strategy="edge_based",
                confidence=0.70, entry_price=0.50,
                features={"edge": 0.05, "filled_size": 10.0},
            )
            assert entry_id > 0
            store.update_calibration_exit(
                token_id=f"t{i}",
                exit_timestamp=f"2026-04-15T13:{i:02d}:00",
                exit_price=0.48,
                exit_reason="stop_loss",
                pnl=-0.20, return_pct=-0.04,
            )
        store.close()

        body = tc.get("/api/drift?min_samples=30&ratio_floor=0.5").json()
        assert body["n_samples"] == 30
        assert body["drift_detected"] is True
        assert body["realisation_ratio"] < 0.5
        assert "below floor" in body["reason"]
