"""Tests for the A/B shadow runner.

Pins the contract that:
* Shadow evaluation never touches live PortfolioTracker / order flow.
* Decisions and calibration rows land in the ``shadow_*`` tables
  (and only there).
* Win-rate + return helpers honour the shadow/live separation.
* The bot_state.json shadow block is well-formed when the runner is
  on, and a degenerate-empty block when it's off.
"""

from __future__ import annotations

import json
import os

import pytest

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.portfolio.tracker import PortfolioTracker, Position
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import Action, BaseStrategy, Signal
from src.strategy.shadow_runner import ShadowRunner


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FixedStrategy(BaseStrategy):
    """Strategy whose evaluate() returns a hard-coded Signal.

    Used so test cases pin behaviour to *what the runner does with a
    signal*, not to the live strategies' internal logic.
    """

    name = "fixed"

    def __init__(self, signal: Signal) -> None:
        self._signal = signal

    def evaluate(self, snapshot, price_history):
        return self._signal


def _snap(token_id: str = "tokA", price: float = 0.5, spread: float = 0.02,
          condition_id: str = "cidA") -> MarketSnapshot:
    return MarketSnapshot(
        condition_id=condition_id, question="Q?",
        token_id=token_id, outcome="YES",
        price=price, spread=spread,
        volume=10000, liquidity=5000, active=True,
    )


# ---------------------------------------------------------------------------
# Entry path
# ---------------------------------------------------------------------------


class TestShadowEntries:
    def test_buy_signal_opens_shadow_position_only(self, tmp_path):
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            sig = Signal(Action.BUY, 0.8, "go", features={"x": 1})
            runner = ShadowRunner(cfg, _FixedStrategy(sig))
            runner.evaluate_entries(
                [_snap()],
                store,
                price_history_loader=lambda tid: [0.5, 0.51, 0.52],
            )
            # Shadow portfolio has the position …
            assert runner.portfolio.open_position_count() == 1
            pos = next(iter(runner.portfolio.positions.values()))
            assert pos.side == "BUY"
            assert pos.entry_price > 0.5  # spread baked in
            # … and a calibration row landed in shadow_calibration.
            cur = store._conn.execute("SELECT COUNT(*) FROM shadow_calibration")
            assert cur.fetchone()[0] == 1
            # The live calibration table is untouched.
            cur = store._conn.execute("SELECT COUNT(*) FROM calibration")
            assert cur.fetchone()[0] == 0
            # And a SHADOW_ENTRY decision was logged in the shadow log only.
            cur = store._conn.execute(
                "SELECT action FROM shadow_decision_log",
            )
            actions = [r[0] for r in cur.fetchall()]
            assert any("SHADOW_ENTRY" in a for a in actions)
            cur = store._conn.execute("SELECT COUNT(*) FROM decision_log")
            assert cur.fetchone()[0] == 0
        finally:
            store.close()

    def test_hold_signal_is_no_op(self, tmp_path):
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            runner = ShadowRunner(cfg, _FixedStrategy(Signal(Action.HOLD, 0.0, "")))
            runner.evaluate_entries([_snap()], store, lambda tid: [])
            assert runner.portfolio.open_position_count() == 0
            cur = store._conn.execute("SELECT COUNT(*) FROM shadow_calibration")
            assert cur.fetchone()[0] == 0
        finally:
            store.close()

    def test_price_outside_bounds_is_skipped(self, tmp_path):
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            runner = ShadowRunner(cfg, _FixedStrategy(Signal(Action.BUY, 0.7, "")))
            runner.evaluate_entries(
                [_snap(price=0.99)],  # > MAX_PRICE 0.95
                store, lambda tid: [],
            )
            assert runner.portfolio.open_position_count() == 0
        finally:
            store.close()

    def test_spread_above_max_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAX_SPREAD", "0.05")
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            runner = ShadowRunner(cfg, _FixedStrategy(Signal(Action.BUY, 0.7, "")))
            runner.evaluate_entries(
                [_snap(spread=0.20)],
                store, lambda tid: [],
            )
            assert runner.portfolio.open_position_count() == 0
        finally:
            store.close()

    def test_duplicate_position_skipped(self, tmp_path):
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            runner = ShadowRunner(cfg, _FixedStrategy(Signal(Action.BUY, 0.8, "")))
            runner.evaluate_entries([_snap()], store, lambda tid: [])
            assert runner.portfolio.open_position_count() == 1
            # Second tick: same token, same signal — must NOT double-up.
            runner.evaluate_entries([_snap()], store, lambda tid: [])
            assert runner.portfolio.open_position_count() == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Exit path
# ---------------------------------------------------------------------------


class TestShadowExits:
    def test_take_profit_closes_at_marked_price(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TAKE_PROFIT_PCT", "0.10")
        monkeypatch.setenv("STOP_LOSS_PCT", "0.50")
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            runner = ShadowRunner(cfg, _FixedStrategy(Signal(Action.BUY, 0.8, "")))
            runner.evaluate_entries([_snap(price=0.50, spread=0.0)], store, lambda tid: [])
            assert runner.portfolio.open_position_count() == 1
            # Force-mark up 20% → triggers TP
            runner.evaluate_exits(store, get_price=lambda tid: 0.60)
            assert runner.portfolio.open_position_count() == 0
            # Realised PnL in shadow tracker is positive.
            assert runner.portfolio.realised_pnl > 0
            # An EXIT_TAKE_PROFIT row landed in shadow_decision_log.
            cur = store._conn.execute(
                "SELECT action FROM shadow_decision_log WHERE action LIKE 'EXIT_%'",
            )
            assert any("TAKE_PROFIT" in r[0] for r in cur.fetchall())
            # shadow_calibration row is now closed.
            cur = store._conn.execute(
                "SELECT pnl, return_pct FROM shadow_calibration WHERE exit_timestamp IS NOT NULL",
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] > 0
            assert abs(row[1] - 0.20) < 1e-6
        finally:
            store.close()

    def test_stop_loss_closes_with_loss(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STOP_LOSS_PCT", "0.10")
        monkeypatch.setenv("TAKE_PROFIT_PCT", "0.50")
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            runner = ShadowRunner(cfg, _FixedStrategy(Signal(Action.BUY, 0.8, "")))
            runner.evaluate_entries([_snap(price=0.50, spread=0.0)], store, lambda tid: [])
            runner.evaluate_exits(store, get_price=lambda tid: 0.40)  # -20%
            assert runner.portfolio.open_position_count() == 0
            assert runner.portfolio.realised_pnl < 0
        finally:
            store.close()

    def test_missing_price_does_not_close(self, tmp_path):
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            runner = ShadowRunner(cfg, _FixedStrategy(Signal(Action.BUY, 0.8, "")))
            runner.evaluate_entries([_snap(price=0.50, spread=0.0)], store, lambda tid: [])
            runner.evaluate_exits(store, get_price=lambda tid: None)
            # Position still open; shadow keeps it (no zombie close).
            assert runner.portfolio.open_position_count() == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


class TestShadowStoreHelpers:
    def test_compute_shadow_win_rate(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            for r, pnl in ((0.05, 1.0), (-0.03, -1.0), (0.01, 1.0), (0.0, 0.0)):
                store.insert_shadow_calibration_entry(
                    entry_timestamp="2026-01-01T00:00:00Z",
                    token_id="x", strategy="s",
                    confidence=0.5, entry_price=0.5,
                )
                store.update_shadow_calibration_exit(
                    token_id="x",
                    exit_timestamp="2026-01-01T01:00:00Z",
                    exit_price=0.5,
                    exit_reason="tp",
                    pnl=pnl, return_pct=r,
                )
            stats = store.compute_shadow_win_rate()
            assert stats["wins"] == 2
            assert stats["losses"] == 1
            assert stats["breakeven"] == 1
            # ``compute_shadow_win_rate`` rounds to 4 decimals.
            assert abs(stats["win_rate"] - (2 / 3)) < 1e-3

        finally:
            store.close()

    def test_get_shadow_closed_returns_chrono(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            for i, r in enumerate((0.01, -0.02, 0.03)):
                store.insert_shadow_calibration_entry(
                    entry_timestamp=f"2026-01-{i+1:02d}T00:00:00Z",
                    token_id=f"x{i}", strategy="s",
                    confidence=0.5, entry_price=0.5,
                )
                store.update_shadow_calibration_exit(
                    token_id=f"x{i}",
                    exit_timestamp=f"2026-01-{i+1:02d}T01:00:00Z",
                    exit_price=0.5, exit_reason="tp",
                    pnl=r, return_pct=r,
                )
            assert store.get_shadow_closed_returns() == [0.01, -0.02, 0.03]
            # Limit returns last-N still chronologically.
            assert store.get_shadow_closed_returns(limit=2) == [-0.02, 0.03]
        finally:
            store.close()


# ---------------------------------------------------------------------------
# bot_state.json shadow block
# ---------------------------------------------------------------------------


class TestExportShadowBlock:
    def test_block_is_zero_when_runner_is_none(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        from src.main import _export_bot_state
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            monkeypatch.chdir(tmp_path)
            client = MagicMock(); client.get_price.return_value = None
            strategy = MagicMock(); strategy.name = "live"
            risk_mgr = MagicMock()
            risk_mgr.is_circuit_breaker_active = False
            risk_mgr.daily_pnl = 0.0
            _export_bot_state(
                cfg, PortfolioTracker(), risk_mgr, strategy,
                1, client, store, shadow_runners=[],
            )
            data = json.loads((tmp_path / "bot_state.json").read_text())
            assert data["shadow"]["enabled"] is False
            assert data["shadow"]["open_positions"] == 0
            assert data["shadow"]["n_runners"] == 0
            assert data["shadow"]["runners"] == []
        finally:
            store.close()

    def test_block_carries_shadow_state_when_runner_present(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        from src.main import _export_bot_state
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            monkeypatch.chdir(tmp_path)
            client = MagicMock()
            client.get_price.return_value = 0.55
            strategy = MagicMock(); strategy.name = "live"
            risk_mgr = MagicMock()
            risk_mgr.is_circuit_breaker_active = False
            risk_mgr.daily_pnl = 0.0
            runner = ShadowRunner(cfg, _FixedStrategy(Signal(Action.BUY, 0.7, "")))
            runner.evaluate_entries([_snap(price=0.50, spread=0.0)], store, lambda tid: [])
            _export_bot_state(
                cfg, PortfolioTracker(), risk_mgr, strategy,
                1, client, store, shadow_runners=[runner],
            )
            data = json.loads((tmp_path / "bot_state.json").read_text())
            sh = data["shadow"]
            # Top-level keys mirror the first runner (back-compat).
            assert sh["enabled"] is True
            assert sh["strategy"] == "fixed"
            assert sh["open_positions"] == 1
            # New ``runners`` shape.
            assert sh["n_runners"] == 1
            assert len(sh["runners"]) == 1
            assert sh["runners"][0]["strategy"] == "fixed"
            assert sh["runners"][0]["open_positions"] == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Multi-runner: list integrity + per-runner row separation
# ---------------------------------------------------------------------------


class _NamedFixedStrategy(_FixedStrategy):
    """``_FixedStrategy`` with a configurable strategy name so the
    multi-shadow tests can verify rows tag the right writer."""

    def __init__(self, name: str, signal: Signal) -> None:
        super().__init__(signal)
        self.name = name


class TestMultiShadow:
    def test_rows_tag_strategy_per_runner(self, tmp_path):
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            r1 = ShadowRunner(cfg, _NamedFixedStrategy("alpha", Signal(Action.BUY, 0.7, "")))
            r2 = ShadowRunner(cfg, _NamedFixedStrategy("beta", Signal(Action.BUY, 0.7, "")))
            # Different tokens so neither runner blocks the other on
            # the duplicate-position rule.
            for r, snap in ((r1, _snap("a", 0.5)), (r2, _snap("b", 0.5))):
                r.evaluate_entries([snap], store, lambda tid: [])
            cur = store._conn.execute(
                "SELECT strategy, COUNT(*) FROM shadow_calibration GROUP BY strategy",
            )
            counts = dict(cur.fetchall())
            assert counts == {"alpha": 1, "beta": 1}
        finally:
            store.close()

    def test_compute_shadow_win_rate_filters_by_strategy(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            # alpha: 1 win, 1 loss; beta: 2 wins.
            for strat, pnl in (("alpha", 1.0), ("alpha", -1.0), ("beta", 1.0), ("beta", 1.0)):
                store.insert_shadow_calibration_entry(
                    entry_timestamp="2026-01-01T00:00:00Z",
                    token_id=f"{strat}-x",
                    strategy=strat, confidence=0.5, entry_price=0.5,
                )
                store.update_shadow_calibration_exit(
                    token_id=f"{strat}-x",
                    exit_timestamp="2026-01-01T01:00:00Z",
                    exit_price=0.5, exit_reason="tp",
                    pnl=pnl, return_pct=pnl,
                )
            assert store.compute_shadow_win_rate(strategy="alpha")["wins"] == 1
            assert store.compute_shadow_win_rate(strategy="alpha")["losses"] == 1
            assert store.compute_shadow_win_rate(strategy="beta")["wins"] == 2
            # Unfiltered = union.
            assert store.compute_shadow_win_rate()["wins"] == 3
        finally:
            store.close()

    def test_get_shadow_closed_returns_filters_by_strategy(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            for strat, r in (("alpha", 0.05), ("beta", -0.02), ("alpha", 0.01)):
                store.insert_shadow_calibration_entry(
                    entry_timestamp="2026-01-01T00:00:00Z",
                    token_id=f"{strat}-y",
                    strategy=strat, confidence=0.5, entry_price=0.5,
                )
                store.update_shadow_calibration_exit(
                    token_id=f"{strat}-y",
                    exit_timestamp="2026-01-01T01:00:00Z",
                    exit_price=0.5, exit_reason="tp",
                    pnl=r, return_pct=r,
                )
            assert store.get_shadow_closed_returns(strategy="alpha") == [0.05, 0.01]
            assert store.get_shadow_closed_returns(strategy="beta") == [-0.02]
            assert store.get_shadow_closed_returns() == [0.05, -0.02, 0.01]
        finally:
            store.close()

    def test_export_state_has_one_runner_block_per_runner(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        from src.main import _export_bot_state
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            monkeypatch.chdir(tmp_path)
            client = MagicMock(); client.get_price.return_value = None
            strategy = MagicMock(); strategy.name = "live"
            risk_mgr = MagicMock()
            risk_mgr.is_circuit_breaker_active = False
            risk_mgr.daily_pnl = 0.0
            r1 = ShadowRunner(cfg, _NamedFixedStrategy("alpha", Signal(Action.HOLD, 0.0, "")))
            r2 = ShadowRunner(cfg, _NamedFixedStrategy("beta", Signal(Action.HOLD, 0.0, "")))
            _export_bot_state(
                cfg, PortfolioTracker(), risk_mgr, strategy,
                1, client, store, shadow_runners=[r1, r2],
            )
            data = json.loads((tmp_path / "bot_state.json").read_text())
            sh = data["shadow"]
            assert sh["n_runners"] == 2
            names = [r["strategy"] for r in sh["runners"]]
            assert names == ["alpha", "beta"]
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Config.shadow_strategy_list
# ---------------------------------------------------------------------------


class TestShadowStrategyList:
    def test_legacy_singular(self, monkeypatch):
        monkeypatch.setenv("STRATEGY", "live")
        monkeypatch.setenv("SHADOW_STRATEGY", "alpha")
        monkeypatch.delenv("SHADOW_STRATEGIES", raising=False)
        cfg = Config()
        assert cfg.shadow_strategy_list() == ["alpha"]

    def test_csv_plural(self, monkeypatch):
        monkeypatch.setenv("STRATEGY", "live")
        monkeypatch.delenv("SHADOW_STRATEGY", raising=False)
        monkeypatch.setenv("SHADOW_STRATEGIES", "alpha, beta ,gamma")
        cfg = Config()
        assert cfg.shadow_strategy_list() == ["alpha", "beta", "gamma"]

    def test_drops_live_and_dedupes(self, monkeypatch):
        monkeypatch.setenv("STRATEGY", "live")
        monkeypatch.setenv("SHADOW_STRATEGY", "alpha")
        monkeypatch.setenv("SHADOW_STRATEGIES", "alpha,live,beta,beta,alpha")
        cfg = Config()
        assert cfg.shadow_strategy_list() == ["alpha", "beta"]

    def test_empty_returns_empty(self, monkeypatch):
        monkeypatch.setenv("STRATEGY", "live")
        monkeypatch.delenv("SHADOW_STRATEGY", raising=False)
        monkeypatch.delenv("SHADOW_STRATEGIES", raising=False)
        cfg = Config()
        assert cfg.shadow_strategy_list() == []
