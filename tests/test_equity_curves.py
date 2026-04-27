"""Tests for the live + multi-shadow equity-curve export.

Pins the contract that:
* ``SQLiteStore.get_equity_curve`` cumulates PnL chronologically and
  honours ``shadow``/``strategy``/``limit`` filters.
* ``_build_equity_curves_block`` returns a well-formed dict with one
  ``shadows[]`` entry per registered runner.
* ``bot_state.json`` carries the block end-to-end.
* The cap honours ``EQUITY_CURVE_TAIL_POINTS``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import Action, BaseStrategy, Signal
from src.strategy.shadow_runner import ShadowRunner


class _NamedFixed(BaseStrategy):
    def __init__(self, name: str, signal: Signal) -> None:
        self.name = name
        self._signal = signal

    def evaluate(self, snapshot, price_history):
        return self._signal


def _seed_calibration(store, *, table: str, strategy: str, pnls: list[float]) -> None:
    insert_entry = (
        store.insert_calibration_entry if table == "calibration"
        else store.insert_shadow_calibration_entry
    )
    update_exit = (
        store.update_calibration_exit if table == "calibration"
        else store.update_shadow_calibration_exit
    )
    for i, pnl in enumerate(pnls):
        insert_entry(
            entry_timestamp=f"2026-01-{i + 1:02d}T00:00:00Z",
            token_id=f"{strategy}-{i}",
            strategy=strategy, confidence=0.5, entry_price=0.5,
        )
        update_exit(
            token_id=f"{strategy}-{i}",
            exit_timestamp=f"2026-01-{i + 1:02d}T01:00:00Z",
            exit_price=0.5, exit_reason="tp",
            pnl=pnl, return_pct=pnl,
        )


# ---------------------------------------------------------------------------
# Store helper
# ---------------------------------------------------------------------------


class TestGetEquityCurve:
    def test_empty_returns_empty(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            assert store.get_equity_curve() == []
            assert store.get_equity_curve(shadow=True) == []
        finally:
            store.close()

    def test_live_cumulates_pnl_chrono(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            _seed_calibration(store, table="calibration", strategy="live",
                              pnls=[1.0, -0.5, 2.0])
            curve = store.get_equity_curve()
            assert [pt["pnl"] for pt in curve] == [1.0, 0.5, 2.5]
            # Chronological order on exit_timestamp.
            ts = [pt["t"] for pt in curve]
            assert ts == sorted(ts)
        finally:
            store.close()

    def test_shadow_filtered_by_strategy(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            _seed_calibration(store, table="shadow", strategy="alpha",
                              pnls=[0.1, 0.2, -0.3])
            _seed_calibration(store, table="shadow", strategy="beta",
                              pnls=[1.0, -1.0])
            alpha = store.get_equity_curve(shadow=True, strategy="alpha")
            beta = store.get_equity_curve(shadow=True, strategy="beta")
            union = store.get_equity_curve(shadow=True)
            assert [pt["pnl"] for pt in alpha] == pytest.approx([0.1, 0.3, 0.0])
            assert [pt["pnl"] for pt in beta] == [1.0, 0.0]
            # Union cumulates across both strategies in exit_timestamp order.
            assert len(union) == 5
        finally:
            store.close()

    def test_limit_keeps_chronological_tail(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            _seed_calibration(store, table="calibration", strategy="live",
                              pnls=[1.0, 1.0, 1.0, 1.0, 1.0])
            tail = store.get_equity_curve(limit=2)
            # Tail is the last 2 rows, but PnL still cumulates only over
            # those 2 rows (cumulative within the returned slice).  This
            # is an intentional simplification — the dashboard cares
            # about *trend over the last N* trades, not the all-time
            # cumulative anchor.
            assert len(tail) == 2
            assert [pt["pnl"] for pt in tail] == [1.0, 2.0]
        finally:
            store.close()


# ---------------------------------------------------------------------------
# bot_state.json equity_curves block
# ---------------------------------------------------------------------------


class TestEquityCurvesBlock:
    def _setup_export_args(self, tmp_path):
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        client = MagicMock(); client.get_price.return_value = None
        strategy = MagicMock(); strategy.name = "live"
        risk_mgr = MagicMock()
        risk_mgr.is_circuit_breaker_active = False
        risk_mgr.daily_pnl = 0.0
        return cfg, store, client, strategy, risk_mgr

    def test_no_runners_block_has_only_live(self, tmp_path, monkeypatch):
        from src.main import _export_bot_state
        monkeypatch.chdir(tmp_path)
        cfg, store, client, strategy, risk_mgr = self._setup_export_args(tmp_path)
        try:
            _seed_calibration(store, table="calibration", strategy="live",
                              pnls=[1.0, -0.5])
            _export_bot_state(
                cfg, PortfolioTracker(), risk_mgr, strategy,
                1, client, store, shadow_runners=[],
            )
            data = json.loads((tmp_path / "bot_state.json").read_text())
            ec = data["equity_curves"]
            assert len(ec["live"]) == 2
            assert ec["live"][-1]["pnl"] == 0.5
            assert ec["shadows"] == []
        finally:
            store.close()

    def test_block_carries_one_entry_per_runner(self, tmp_path, monkeypatch):
        from src.main import _export_bot_state
        monkeypatch.chdir(tmp_path)
        cfg, store, client, strategy, risk_mgr = self._setup_export_args(tmp_path)
        try:
            _seed_calibration(store, table="shadow", strategy="alpha",
                              pnls=[0.1, 0.2])
            _seed_calibration(store, table="shadow", strategy="beta",
                              pnls=[1.0])
            r1 = ShadowRunner(cfg, _NamedFixed("alpha", Signal(Action.HOLD, 0.0, "")))
            r2 = ShadowRunner(cfg, _NamedFixed("beta", Signal(Action.HOLD, 0.0, "")))
            _export_bot_state(
                cfg, PortfolioTracker(), risk_mgr, strategy,
                1, client, store, shadow_runners=[r1, r2],
            )
            data = json.loads((tmp_path / "bot_state.json").read_text())
            ec = data["equity_curves"]
            names = [s["strategy"] for s in ec["shadows"]]
            assert names == ["alpha", "beta"]
            alpha = next(s for s in ec["shadows"] if s["strategy"] == "alpha")
            beta = next(s for s in ec["shadows"] if s["strategy"] == "beta")
            assert [pt["pnl"] for pt in alpha["points"]] == [0.1, 0.3]
            assert [pt["pnl"] for pt in beta["points"]] == [1.0]
        finally:
            store.close()

    def test_tail_caps_points_per_series(self, tmp_path, monkeypatch):
        from src.main import _export_bot_state
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("EQUITY_CURVE_TAIL_POINTS", "3")
        cfg, store, client, strategy, risk_mgr = self._setup_export_args(tmp_path)
        try:
            _seed_calibration(store, table="calibration", strategy="live",
                              pnls=[1.0] * 10)
            _export_bot_state(
                cfg, PortfolioTracker(), risk_mgr, strategy,
                1, client, store, shadow_runners=[],
            )
            data = json.loads((tmp_path / "bot_state.json").read_text())
            assert len(data["equity_curves"]["live"]) == 3
        finally:
            store.close()
