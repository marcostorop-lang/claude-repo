"""Tests for the helpers extracted from ``_tick``.

Targets:

* ``_collect_exit_candidates`` — pure-ish, returns the list of
  ``(token_id, reason)`` tuples to close based on SL/TP/edge-flip.
* ``_persist_tick_stats`` — wraps the SQLite write with proper error
  routing into SystemMonitor.

Both used to be ~50 lines of inline code in the 600-LOC ``_tick``
body; pulling them out lets us unit-test the trigger logic without
spinning the full polling loop.
"""

from __future__ import annotations

import os
import sqlite3
from unittest import mock

import pytest

from src.config import Config
from src.main import _collect_exit_candidates, _persist_tick_stats
from src.portfolio.tracker import PortfolioTracker, Position
from src.risk.manager import RiskManager
from src.storage.sqlite_store import SQLiteStore
from src.utils.alerts import AlertManager
from src.utils.system_monitor import SystemMonitor


def _cfg(**overrides):
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "STOP_LOSS_PCT": "0.10",
        "TAKE_PROFIT_PCT": "0.20",
        "EXIT_ON_EDGE_FLIP": "false",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


class _StubClient:
    def __init__(self, prices: dict[str, float] | None = None):
        self.prices = prices or {}

    def get_price(self, token_id: str):
        return self.prices.get(token_id)

    def get_spread(self, token_id: str):
        return 0.01


class TestCollectExitCandidates:
    def test_no_positions_yields_empty(self):
        cfg = _cfg()
        rm = RiskManager(cfg, PortfolioTracker())
        store = SQLiteStore(":memory:")
        out = _collect_exit_candidates(
            PortfolioTracker(), rm, _StubClient(), store, cfg,
        )
        assert out == []
        store.close()

    def test_stop_loss_triggers(self):
        cfg = _cfg(STOP_LOSS_PCT="0.10")
        portfolio = PortfolioTracker()
        portfolio.open_position(Position(
            token_id="tk1", condition_id="c", side="BUY",
            size=10, entry_price=1.0, strategy="t", order_id="o",
        ))
        rm = RiskManager(cfg, portfolio)
        store = SQLiteStore(":memory:")
        out = _collect_exit_candidates(
            portfolio, rm, _StubClient({"tk1": 0.85}),
            store, cfg,
        )
        assert out == [("tk1", "stop_loss")]
        store.close()

    def test_take_profit_triggers(self):
        cfg = _cfg(TAKE_PROFIT_PCT="0.20")
        portfolio = PortfolioTracker()
        portfolio.open_position(Position(
            token_id="tk2", condition_id="c", side="BUY",
            size=10, entry_price=1.0, strategy="t", order_id="o",
        ))
        rm = RiskManager(cfg, portfolio)
        store = SQLiteStore(":memory:")
        out = _collect_exit_candidates(
            portfolio, rm, _StubClient({"tk2": 1.30}),
            store, cfg,
        )
        assert out == [("tk2", "take_profit")]
        store.close()

    def test_no_price_skips_position(self):
        cfg = _cfg()
        portfolio = PortfolioTracker()
        portfolio.open_position(Position(
            token_id="tk3", condition_id="c", side="BUY",
            size=10, entry_price=1.0, strategy="t", order_id="o",
        ))
        rm = RiskManager(cfg, portfolio)
        store = SQLiteStore(":memory:")
        # No price for tk3 → helper must not raise and must skip it.
        out = _collect_exit_candidates(
            portfolio, rm, _StubClient(prices={}),
            store, cfg,
        )
        assert out == []
        store.close()

    def test_only_one_reason_per_position(self):
        """SL takes precedence over TP / edge-flip when both could fire."""
        cfg = _cfg(STOP_LOSS_PCT="0.05", TAKE_PROFIT_PCT="0.05")
        portfolio = PortfolioTracker()
        portfolio.open_position(Position(
            token_id="tk4", condition_id="c", side="BUY",
            size=10, entry_price=1.0, strategy="t", order_id="o",
        ))
        rm = RiskManager(cfg, portfolio)
        store = SQLiteStore(":memory:")
        # 0.92 → 8% loss → SL (>5%) trips.  TP would also need >=1.05 so
        # it doesn't apply here; we just pin "exactly one tuple".
        out = _collect_exit_candidates(
            portfolio, rm, _StubClient({"tk4": 0.92}),
            store, cfg,
        )
        assert len(out) == 1
        store.close()


class TestPersistTickStats:
    def _summary(self):
        return {
            "open_positions": 1,
            "total_exposure": 50.0,
            "realised_pnl": 0.0,
            "unrealised_pnl": 0.0,
            "fees_paid": 0.0,
            "paper_friction_paid": 0.0,
            "net_pnl": 0.0,
            "net_pnl_after_fees": 0.0,
            "net_pnl_after_costs": 0.0,
        }

    def test_writes_row(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        _persist_tick_stats(
            store, timestamp="2026-05-07T12:00:00", duration_s=0.5,
            markets_scanned=10, signals_generated=2, risk_rejections=0,
            trades_executed=0, summary=self._summary(),
            daily_pnl=0.0, skip_warmup=0, skip_no_price=0, skip_hold=0,
            var_95=0.0, cvar_95=0.0, worst_case=0.0,
        )
        rows = store._conn.execute("SELECT * FROM tick_stats").fetchall()
        assert len(rows) == 1
        store.close()

    def test_db_error_routed_to_monitor(self):
        # Create a closed store so insert raises.
        store = SQLiteStore(":memory:")
        store._conn.close()  # next op will raise sqlite3.ProgrammingError

        sink_alerts = []

        class _Sink:
            def emit(self, a):
                sink_alerts.append(a)

        mon = SystemMonitor(manager=AlertManager(sinks=[_Sink()], dedupe_window_s=0))
        # Helper must not raise — it routes the error to the monitor.
        _persist_tick_stats(
            store, timestamp="2026-05-07T12:00:00", duration_s=0.5,
            markets_scanned=0, signals_generated=0, risk_rejections=0,
            trades_executed=0, summary=self._summary(),
            daily_pnl=0.0, skip_warmup=0, skip_no_price=0, skip_hold=0,
            var_95=0.0, cvar_95=0.0, worst_case=0.0,
            system_monitor=mon,
        )
        # SystemMonitor recorded a critical alert about insert_tick_stats.
        crits = [a for a in sink_alerts if a.severity == "critical"]
        assert any("SQLite" in a.subject for a in crits)
