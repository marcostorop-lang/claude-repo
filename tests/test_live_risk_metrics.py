"""Tests for the live risk-metrics block in bot_state.json.

Pins the contract that the Phase B metrics module is wired into the
operator-facing observability surface, not just the offline CLI.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.storage.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# SQLiteStore.get_closed_trade_returns
# ---------------------------------------------------------------------------


class TestGetClosedTradeReturns:
    def _seed(self, store: SQLiteStore, returns: list[float]) -> None:
        for i, r in enumerate(returns):
            store.insert_calibration_entry(
                entry_timestamp=f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
                token_id=f"tok{i}",
                strategy="momentum",
                confidence=0.5,
                entry_price=0.5,
            )
            store.update_calibration_exit(
                token_id=f"tok{i}",
                exit_timestamp=f"2026-01-{(i % 28) + 1:02d}T01:00:00Z",
                exit_price=0.5 * (1 + r),
                exit_reason="tp",
                pnl=r * 10.0,
                return_pct=r,
            )

    def test_empty_store_returns_empty(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            assert store.get_closed_trade_returns() == []
        finally:
            store.close()

    def test_chronological_order_when_no_limit(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            self._seed(store, [0.01, -0.02, 0.03, -0.01, 0.05])
            got = store.get_closed_trade_returns()
            assert got == [0.01, -0.02, 0.03, -0.01, 0.05]
        finally:
            store.close()

    def test_limit_returns_last_n_chrono(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            self._seed(store, [0.01, -0.02, 0.03, -0.01, 0.05])
            # Last 3 in chronological order.
            got = store.get_closed_trade_returns(limit=3)
            assert got == [0.03, -0.01, 0.05]
        finally:
            store.close()


# ---------------------------------------------------------------------------
# _export_bot_state -> bot_state.json["risk_metrics"]
# ---------------------------------------------------------------------------


class TestExportBotStateRiskMetrics:
    def _build(self, tmp_path, returns: list[float], tail: int | None = None):
        # Build a minimal store with N closed trades.
        if tail is not None:
            os.environ["RISK_METRICS_TAIL_WINDOW"] = str(tail)
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        for i, r in enumerate(returns):
            store.insert_calibration_entry(
                entry_timestamp="2026-01-01T00:00:00Z",
                token_id=f"tok{i}", strategy="m",
                confidence=0.5, entry_price=0.5,
            )
            store.update_calibration_exit(
                token_id=f"tok{i}",
                exit_timestamp="2026-01-01T01:00:00Z",
                exit_price=0.5 * (1 + r),
                exit_reason="tp", pnl=r, return_pct=r,
            )
        portfolio = PortfolioTracker()
        risk_mgr = RiskManager(cfg, portfolio)
        client = MagicMock()
        client.get_price.return_value = None
        # Mock strategy with a name.
        strategy = MagicMock()
        strategy.name = "momentum"
        return cfg, portfolio, risk_mgr, strategy, store, client

    def test_empty_history_writes_zero_block(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg, portfolio, risk_mgr, strategy, store, client = self._build(tmp_path, [])
        try:
            from src.main import _export_bot_state
            _export_bot_state(cfg, portfolio, risk_mgr, strategy, 1, client, store)
            data = json.loads((tmp_path / "bot_state.json").read_text())
            rm = data["risk_metrics"]
            assert rm["n"] == 0
            assert rm["sharpe_annualized"] == 0.0
            assert rm["max_drawdown"] == 0.0
            assert rm["psr_vs_zero"] == 0.0
        finally:
            store.close()

    def test_populated_history_emits_nonzero_sharpe(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Strong positive series → Sharpe > 0.
        rets = [0.01, 0.012, 0.011, 0.013, 0.009, 0.014, 0.008, 0.012] * 8
        cfg, portfolio, risk_mgr, strategy, store, client = self._build(
            tmp_path, rets, tail=200,
        )
        try:
            from src.main import _export_bot_state
            _export_bot_state(cfg, portfolio, risk_mgr, strategy, 1, client, store)
            data = json.loads((tmp_path / "bot_state.json").read_text())
            rm = data["risk_metrics"]
            assert rm["n"] == len(rets)
            assert rm["sharpe_annualized"] > 0
            # For a constant-mean positive series the drawdown is zero.
            assert rm["max_drawdown"] == 0.0
            assert 0.0 <= rm["psr_vs_zero"] <= 1.0
            assert rm["tail_window"] == 200
        finally:
            store.close()

    def test_atomic_write_no_partial_files_left(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg, portfolio, risk_mgr, strategy, store, client = self._build(tmp_path, [0.01])
        try:
            from src.main import _export_bot_state
            _export_bot_state(cfg, portfolio, risk_mgr, strategy, 1, client, store)
            # No leftover .bot_state.*.tmp file in cwd.
            stragglers = list(tmp_path.glob(".bot_state.*.tmp"))
            assert stragglers == []
            assert (tmp_path / "bot_state.json").exists()
        finally:
            store.close()

    def test_tail_window_limits_n(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rets = [0.01] * 50  # constant series; we only check ``n``
        cfg, portfolio, risk_mgr, strategy, store, client = self._build(
            tmp_path, rets, tail=10,
        )
        try:
            from src.main import _export_bot_state
            _export_bot_state(cfg, portfolio, risk_mgr, strategy, 1, client, store)
            data = json.loads((tmp_path / "bot_state.json").read_text())
            assert data["risk_metrics"]["n"] == 10
            assert data["risk_metrics"]["tail_window"] == 10
        finally:
            store.close()
