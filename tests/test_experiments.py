"""Tests for the experiment runner."""

import json
import os
from unittest import mock

from src.config import Config
from src.experiments.runner import run_manifest
from src.storage.sqlite_store import SQLiteStore


def _seed_prices(store: SQLiteStore, token: str, prices: list[float]) -> None:
    for i, p in enumerate(prices):
        store.insert_price(token, p, f"2026-01-01T00:0{i}:00")


def test_missing_manifest_returns_message(tmp_path):
    cfg = Config()
    store = SQLiteStore(":memory:")
    result = run_manifest(str(tmp_path / "nope.json"), cfg, store)
    assert "not found" in result.lower()
    store.close()


def test_empty_experiments_returns_message(tmp_path):
    cfg = Config()
    store = SQLiteStore(":memory:")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"name": "empty", "experiments": []}))
    result = run_manifest(str(manifest), cfg, store)
    assert "no experiments" in result.lower()
    store.close()


def test_no_price_history_returns_warning(tmp_path):
    cfg = Config()
    store = SQLiteStore(":memory:")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "name": "x",
        "experiments": [{"id": "a", "strategy": "simple_momentum", "params": {}}],
    }))
    result = run_manifest(str(manifest), cfg, store)
    assert "no price history" in result.lower()
    store.close()


def test_runs_experiments_and_produces_markdown(tmp_path):
    env = {"SQLITE_DB_PATH": ":memory:"}
    with mock.patch.dict(os.environ, env, clear=False):
        cfg = Config()
    store = SQLiteStore(":memory:")
    _seed_prices(store, "token_a", [0.40, 0.42, 0.44, 0.46, 0.50, 0.55, 0.60])
    _seed_prices(store, "token_b", [0.50, 0.50, 0.50, 0.50, 0.50, 0.50, 0.50])

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "name": "unit_test",
        "experiments": [
            {
                "id": "fast",
                "strategy": "simple_momentum",
                "params": {
                    "MOMENTUM_WINDOW": "3",
                    "MOMENTUM_THRESHOLD": "0.02",
                    "STOP_LOSS_PCT": "0.10",
                    "TAKE_PROFIT_PCT": "0.05",
                },
            },
            {
                "id": "slow",
                "strategy": "simple_momentum",
                "params": {
                    "MOMENTUM_WINDOW": "5",
                    "MOMENTUM_THRESHOLD": "0.05",
                    "STOP_LOSS_PCT": "0.10",
                    "TAKE_PROFIT_PCT": "0.05",
                },
            },
        ],
    }))

    # Run in a temp cwd so experiments/results/ is inside tmp_path
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = run_manifest(str(manifest), cfg, store)
    finally:
        os.chdir(cwd)

    assert "# Experiment: unit_test" in result
    assert "fast" in result
    assert "slow" in result
    # Results JSON should exist
    assert (tmp_path / "experiments" / "results" / "unit_test.json").exists()
    store.close()
