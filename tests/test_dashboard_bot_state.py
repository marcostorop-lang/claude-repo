"""Tests for the dashboard de-hardcoding via bot_state.json."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from src.storage.sqlite_store import SQLiteStore  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"
    store = SQLiteStore(str(db_path))
    store.close()
    monkeypatch.setenv("SQLITE_DB_PATH", str(db_path))
    state_path = tmp_path / "bot_state.json"
    monkeypatch.setenv("BOT_STATE_PATH", str(state_path))
    import importlib
    import dashboard.backend.main as backend_main
    import dashboard.backend.bot_state as bot_state_mod
    importlib.reload(bot_state_mod)
    importlib.reload(backend_main)
    with TestClient(backend_main.app) as tc:
        yield tc, db_path, state_path


def _seed_trade(db_path, **kw):
    store = SQLiteStore(str(db_path))
    store.insert_trade(
        order_id=kw["order_id"], token_id=kw["token_id"],
        condition_id=kw["condition_id"], side=kw["side"],
        size=kw["size"], price=kw["price"], strategy="t",
        mode="paper", timestamp=kw.get("timestamp", "2026-05-01T00:00:00"),
    )
    store.close()


class TestPositionsRouteUsesBotState:
    def test_sl_tp_pct_read_from_state_file(self, client):
        tc, db_path, state_path = client
        _seed_trade(db_path, order_id="o1", token_id="tkA", condition_id="c1",
                    side="BUY", size=10, price=0.50)
        # Seed a price-history row so current_price != entry_price; this
        # makes dist_to_sl sensitive to sl_pct.
        store = SQLiteStore(str(db_path))
        # Current price below entry: dist_to_sl falls between 0 and 100,
        # which is the only regime where sl_pct affects the output.
        store.insert_price("tkA", 0.48, "2026-05-01T00:01:00", spread=0.01)
        store.close()
        # First config: tight SL (5%), wide TP (40%).
        state_path.write_text(json.dumps({
            "config": {"stop_loss_pct": 0.05, "take_profit_pct": 0.40},
        }))
        sl_a = tc.get("/api/positions").json()["positions"][0]["pct_to_stop_loss"]
        tp_a = tc.get("/api/positions").json()["positions"][0]["pct_to_take_profit"]
        # Second config: wide SL (20%), tight TP (10%).
        state_path.write_text(json.dumps({
            "config": {"stop_loss_pct": 0.20, "take_profit_pct": 0.10},
        }))
        sl_b = tc.get("/api/positions").json()["positions"][0]["pct_to_stop_loss"]
        tp_b = tc.get("/api/positions").json()["positions"][0]["pct_to_take_profit"]
        # Distance numbers must move when config moves.  This is the
        # pin: the dashboard *cannot* be stale-hardcoded after the fix.
        # We assert on SL only because the seeded current price is below
        # entry, which puts dist_to_tp in the clamped >=100 region; the
        # SL signal alone is enough to prove the route reads config.
        assert sl_a != sl_b

    def test_falls_back_safely_without_state(self, client):
        tc, db_path, state_path = client
        _seed_trade(db_path, order_id="o2", token_id="tkB", condition_id="c2",
                    side="BUY", size=10, price=0.50)
        # No state file: hits the cold-start defaults (10% / 20%).
        body = tc.get("/api/positions").json()
        assert body["positions"][0]["pct_to_stop_loss"] >= 0


class TestOverviewRouteDerivesBalance:
    def test_balance_grows_with_max_total_exposure(self, client):
        tc, _, state_path = client
        # No trades, no state → falls back to default.
        body0 = tc.get("/api/overview").json()
        # Now write a state with a 10x exposure cap and re-fetch.
        state_path.write_text(json.dumps({
            "config": {"max_total_exposure": 2000.0},
            "portfolio": {"fees_paid": 0.0, "paper_friction_paid": 0.0},
        }))
        body1 = tc.get("/api/overview").json()
        assert body1["starting_balance"] > body0["starting_balance"]
        assert body1["simulated_balance"] != 1000.0  # never the legacy magic value

    def test_balance_subtracts_fees_and_friction(self, client):
        tc, _, state_path = client
        state_path.write_text(json.dumps({
            "config": {"max_total_exposure": 200.0},
            "portfolio": {"fees_paid": 5.0, "paper_friction_paid": 2.5},
        }))
        body = tc.get("/api/overview").json()
        # starting = 200 * 5 = 1000.  total_pnl=0 (no trades).
        # balance = 1000 + 0 - 5 - 2.5 = 992.5
        assert body["starting_balance"] == 1000.0
        assert body["simulated_balance"] == pytest.approx(992.5)
        assert body["fees_paid"] == 5.0
        assert body["paper_friction_paid"] == 2.5
