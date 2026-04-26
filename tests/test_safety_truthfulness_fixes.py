"""Regression tests for the safety/truthfulness fixes shipped in this branch.

Each test corresponds to a bug from the audit so a future refactor that
silently re-introduces the bug fails CI immediately.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker, Position
from src.risk.manager import RiskManager
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import Action, Signal
from src.strategy.simple_momentum import SimpleMomentum
from src.strategy.mean_reversion import MeanReversion
from src.polymarket.market_data import MarketSnapshot
from src.backtest.engine import Backtester


# ---------------------------------------------------------------------------
# 1. Daily circuit breaker resets at UTC midnight, not local midnight.
# ---------------------------------------------------------------------------

class TestCircuitBreakerUTC:
    def test_uses_utc_today_for_reset(self):
        cfg = Config()
        rm = RiskManager(cfg, PortfolioTracker())
        # Force "today" to a known UTC date by patching datetime.now.
        fake_today = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        with patch("src.risk.manager.datetime") as mock_dt:
            mock_dt.now.return_value = fake_today
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            rm._maybe_reset_daily()
        assert rm._current_date == date(2026, 1, 15)

    def test_does_not_call_date_today_anywhere(self):
        # Use the AST so docstrings/comments mentioning ``date.today``
        # don't cause false positives — only real call expressions count.
        import ast
        import inspect
        from src.risk import manager as rm_mod
        tree = ast.parse(inspect.getsource(rm_mod))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "today":
                if isinstance(func.value, ast.Name) and func.value.id == "date":
                    raise AssertionError(
                        "RiskManager.manager still calls date.today() — the "
                        "circuit-breaker reset must be anchored to UTC."
                    )


# ---------------------------------------------------------------------------
# 2. MAX_SPREAD now blocks BUYs whose spread is unknown when the gate is on.
# ---------------------------------------------------------------------------

class TestSpreadFallback:
    def _rm(self, **env_overrides):
        for k, v in env_overrides.items():
            os.environ[k] = v
        cfg = Config()
        return RiskManager(cfg, PortfolioTracker())

    def test_buy_with_unknown_spread_rejected_when_gate_on(self, monkeypatch):
        monkeypatch.setenv("REQUIRE_KNOWN_SPREAD_FOR_BUY", "true")
        rm = self._rm()
        v = rm.check("tok1", Signal(Action.BUY, 0.8), proposed_size=10, price=0.5, spread=0.0)
        assert not v.allowed
        assert "spread unknown" in v.reason.lower()

    def test_buy_with_known_spread_passes_when_gate_on(self, monkeypatch):
        monkeypatch.setenv("REQUIRE_KNOWN_SPREAD_FOR_BUY", "true")
        rm = self._rm()
        v = rm.check("tok1", Signal(Action.BUY, 0.8), proposed_size=10, price=0.5, spread=0.02)
        assert v.allowed

    def test_sell_with_unknown_spread_still_allowed(self, monkeypatch):
        # SELLs (closes) must never be blocked by this gate — we always
        # want the option to exit.
        monkeypatch.setenv("REQUIRE_KNOWN_SPREAD_FOR_BUY", "true")
        rm = self._rm()
        v = rm.check("tok1", Signal(Action.SELL, 0.8), proposed_size=10, price=0.5, spread=0.0)
        assert v.allowed

    def test_buy_with_known_spread_above_max_rejected(self, monkeypatch):
        # Pre-existing path still enforced.
        monkeypatch.setenv("MAX_SPREAD", "0.05")
        rm = self._rm()
        v = rm.check("tok1", Signal(Action.BUY, 0.8), proposed_size=10, price=0.5, spread=0.10)
        assert not v.allowed
        assert "spread" in v.reason.lower()


# ---------------------------------------------------------------------------
# 3. Zombie positions: missing-price counter triggers force-close.
# ---------------------------------------------------------------------------

class TestZombiePositions:
    def _pos(self, **kw) -> Position:
        defaults = dict(
            token_id="tokA", condition_id="cidA", side="BUY",
            size=10.0, entry_price=0.50, strategy="momentum",
            order_id="o1",
        )
        defaults.update(kw)
        return Position(**defaults)

    def test_record_observation_resets_counter(self):
        pt = PortfolioTracker()
        pt.open_position(self._pos())
        pt.record_missing_price("tokA")
        pt.record_missing_price("tokA")
        assert pt.positions["tokA"].consecutive_missing_price_ticks == 2
        pt.record_price_observation("tokA", 0.55)
        assert pt.positions["tokA"].consecutive_missing_price_ticks == 0
        assert pt.positions["tokA"].last_known_price == 0.55

    def test_zombie_positions_returns_above_threshold(self):
        pt = PortfolioTracker()
        pt.open_position(self._pos())
        for _ in range(4):
            pt.record_missing_price("tokA")
        assert pt.zombie_positions(max_missing_ticks=5) == []
        pt.record_missing_price("tokA")
        zombies = pt.zombie_positions(max_missing_ticks=5)
        assert len(zombies) == 1
        assert zombies[0].token_id == "tokA"

    def test_zero_threshold_disables_check(self):
        pt = PortfolioTracker()
        pt.open_position(self._pos())
        for _ in range(20):
            pt.record_missing_price("tokA")
        assert pt.zombie_positions(max_missing_ticks=0) == []


# ---------------------------------------------------------------------------
# 4. compute_win_rate aggregates honestly from calibration.
# ---------------------------------------------------------------------------

class TestComputeWinRate:
    def test_empty_store(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            stats = store.compute_win_rate()
            assert stats == {
                "wins": 0, "losses": 0, "breakeven": 0,
                "total_closed": 0, "win_rate": 0.0,
            }
        finally:
            store.close()

    def test_breakeven_excluded_from_winrate_denominator(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            for pnl in (1.0, 1.0, -1.0, 0.0, 0.0):
                row_id = store.insert_calibration_entry(
                    entry_timestamp="2026-01-01T00:00:00Z",
                    token_id="x", strategy="s",
                    confidence=0.5, entry_price=0.5,
                )
                store.update_calibration_exit(
                    token_id="x", exit_timestamp="2026-01-01T01:00:00Z",
                    exit_price=0.5, exit_reason="tp",
                    pnl=pnl, return_pct=pnl / 0.5,
                )
            stats = store.compute_win_rate()
            assert stats["wins"] == 2
            assert stats["losses"] == 1
            assert stats["breakeven"] == 2
            assert stats["total_closed"] == 5
            # 2 / (2 + 1) = 0.6667
            assert abs(stats["win_rate"] - 0.6667) < 1e-3
        finally:
            store.close()


# ---------------------------------------------------------------------------
# 5. Strategy net-edge gate rejects below-cost signals when enabled.
# ---------------------------------------------------------------------------

class TestStrategyNetEdgeGate:
    def _snap(self, price: float, spread: float) -> MarketSnapshot:
        return MarketSnapshot(
            condition_id="c", question="q?", token_id="t",
            outcome="YES", price=price, spread=spread,
            volume=10000, liquidity=5000, active=True,
        )

    def test_momentum_gate_rejects_when_spread_eats_edge(self, monkeypatch):
        monkeypatch.setenv("STRATEGY_NET_EDGE_GATE_ENABLED", "true")
        monkeypatch.setenv("STRATEGY_MIN_NET_EDGE", "0.005")
        monkeypatch.setenv("MOMENTUM_THRESHOLD", "0.01")
        cfg = Config()
        s = SimpleMomentum(cfg)
        # Gross 2% rise across the window, but spread is 5% (half = 2.5%)
        # so net edge is negative — the gate must reject.
        history = [0.50, 0.50, 0.51]
        sig = s.evaluate(self._snap(price=0.51, spread=0.05), history)
        assert sig.action == Action.HOLD
        assert "net edge" in sig.reason.lower() or "below min" in sig.reason.lower()

    def test_momentum_gate_passes_when_edge_clears_costs(self, monkeypatch):
        monkeypatch.setenv("STRATEGY_NET_EDGE_GATE_ENABLED", "true")
        monkeypatch.setenv("STRATEGY_MIN_NET_EDGE", "0.005")
        monkeypatch.setenv("MOMENTUM_THRESHOLD", "0.01")
        cfg = Config()
        s = SimpleMomentum(cfg)
        history = [0.50, 0.51, 0.55]
        sig = s.evaluate(self._snap(price=0.55, spread=0.005), history)
        assert sig.action == Action.BUY

    def test_features_record_net_edge_breakdown(self, monkeypatch):
        # Even when the gate is off, the breakdown must be in features
        # so calibration analysis can use it.
        monkeypatch.setenv("STRATEGY_NET_EDGE_GATE_ENABLED", "false")
        cfg = Config()
        s = SimpleMomentum(cfg)
        history = [0.50, 0.51, 0.55]
        sig = s.evaluate(self._snap(price=0.55, spread=0.02), history)
        for key in ("gross_edge", "half_spread", "fee_pct", "net_edge"):
            assert key in sig.features


# ---------------------------------------------------------------------------
# 6. Backtester is deterministic when given a seed.
# ---------------------------------------------------------------------------

class TestBacktesterDeterminism:
    def test_same_seed_produces_same_report(self, monkeypatch):
        # Disable the strategy gate so the behaviour under test is the
        # backtester's RNG, not the strategy's filter.
        monkeypatch.setenv("STRATEGY_NET_EDGE_GATE_ENABLED", "false")
        cfg = Config()
        strat = SimpleMomentum(cfg)
        prices = {"t1": [(0.5 + 0.01 * (i % 5), 0.02) for i in range(40)]}

        def run(seed):
            return Backtester(
                cfg, strat,
                seed=seed, rejection_prob=0.4,
                partial_fill_prob=0.4, partial_fill_min_ratio=0.3,
            ).run(prices)

        a, b = run(seed=42), run(seed=42)
        assert a.trades_opened == b.trades_opened
        assert a.rejections_simulated == b.rejections_simulated
        assert a.partial_fills == b.partial_fills
        assert a.total_pnl == b.total_pnl

    def test_different_seeds_can_diverge(self, monkeypatch):
        monkeypatch.setenv("STRATEGY_NET_EDGE_GATE_ENABLED", "false")
        cfg = Config()
        strat = SimpleMomentum(cfg)
        prices = {"t1": [(0.5 + 0.01 * (i % 5), 0.02) for i in range(60)]}
        a = Backtester(cfg, strat, seed=1, rejection_prob=0.5).run(prices)
        b = Backtester(cfg, strat, seed=2, rejection_prob=0.5).run(prices)
        # Not strictly guaranteed but with 60 ticks @ p=0.5 the chance
        # of identical outputs is astronomically small.  This protects
        # against a regression that hardwires the RNG.
        assert (a.rejections_simulated, a.trades_opened) != (
            b.rejections_simulated, b.trades_opened,
        )


# ---------------------------------------------------------------------------
# 7. SQLite store creates indexes + can prune decision log.
# ---------------------------------------------------------------------------

class TestSQLiteIndexesAndRetention:
    def test_indexes_created(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            cur = store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
            names = {r[0] for r in cur.fetchall()}
            for expected in (
                "idx_trades_token", "idx_trades_condition", "idx_decision_ts",
                "idx_decision_action", "idx_price_token_ts", "idx_tick_ts",
                "idx_calibration_token",
            ):
                assert expected in names, f"missing index {expected}"
        finally:
            store.close()

    def test_prune_decision_log_removes_old_rows(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            old_ts = (
                datetime.now(timezone.utc) - timedelta(days=60)
            ).isoformat()
            new_ts = datetime.now(timezone.utc).isoformat()
            store.insert_decision(
                timestamp=old_ts, token_id="t", condition_id="c",
                action="ENTRY", reason="r",
            )
            store.insert_decision(
                timestamp=new_ts, token_id="t", condition_id="c",
                action="ENTRY", reason="r",
            )
            deleted = store.prune_decision_log(max_age_days=30)
            assert deleted == 1
            cur = store._conn.execute("SELECT COUNT(*) FROM decision_log")
            assert cur.fetchone()[0] == 1
        finally:
            store.close()

    def test_prune_zero_disabled(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            assert store.prune_decision_log(max_age_days=0) == 0
        finally:
            store.close()


# ---------------------------------------------------------------------------
# 8. _decide_exit_reason returns expected reasons.
# ---------------------------------------------------------------------------

class TestExitDecisionHelper:
    def test_stop_loss_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("STOP_LOSS_PCT", "0.10")
        monkeypatch.setenv("TAKE_PROFIT_PCT", "0.20")
        cfg = Config()
        rm = RiskManager(cfg, PortfolioTracker())
        from src.main import _decide_exit_reason

        class _FakeClient:
            def get_price(self, *_): return None
            def get_spread(self, *_): return 0.0

        class _FakeStore:
            def get_price_history(self, *a, **k): return []

        pos = Position(
            token_id="t", condition_id="c", side="BUY",
            size=10.0, entry_price=1.0, strategy="momentum", order_id="o",
        )
        # Price 0.85 → 15% loss → SL should fire (10% threshold).
        reason = _decide_exit_reason(pos, 0.85, cfg, rm, _FakeStore(), _FakeClient())
        assert reason == "stop_loss"

    def test_no_exit_when_inside_bands(self, monkeypatch):
        cfg = Config()
        rm = RiskManager(cfg, PortfolioTracker())
        from src.main import _decide_exit_reason

        class _FakeClient:
            def get_price(self, *_): return None
            def get_spread(self, *_): return 0.0

        class _FakeStore:
            def get_price_history(self, *a, **k): return []

        pos = Position(
            token_id="t", condition_id="c", side="BUY",
            size=10.0, entry_price=1.0, strategy="momentum", order_id="o",
        )
        assert _decide_exit_reason(pos, 1.02, cfg, rm, _FakeStore(), _FakeClient()) == ""


# ---------------------------------------------------------------------------
# 9. _observe_position_price end-to-end.
# ---------------------------------------------------------------------------

class TestObservePositionPrice:
    def test_zombie_with_close_action_returns_fallback(self, monkeypatch):
        monkeypatch.setenv("ZOMBIE_POSITION_MAX_MISSING_TICKS", "3")
        monkeypatch.setenv("ZOMBIE_POSITION_ACTION", "close")
        cfg = Config()
        from src.main import _observe_position_price
        pt = PortfolioTracker()
        pt.open_position(Position(
            token_id="t", condition_id="c", side="BUY",
            size=10.0, entry_price=0.50, strategy="momentum", order_id="o",
            last_known_price=0.55,
        ))
        # First two misses below threshold.
        for _ in range(2):
            is_zombie, fb = _observe_position_price(pt, cfg, "t", None)
            assert not is_zombie
            assert fb is None
        # Third miss reaches the threshold → close action returns fallback.
        is_zombie, fb = _observe_position_price(pt, cfg, "t", None)
        assert is_zombie
        assert fb == 0.55

    def test_alert_action_does_not_return_fallback(self, monkeypatch):
        monkeypatch.setenv("ZOMBIE_POSITION_MAX_MISSING_TICKS", "1")
        monkeypatch.setenv("ZOMBIE_POSITION_ACTION", "alert")
        cfg = Config()
        from src.main import _observe_position_price
        pt = PortfolioTracker()
        pt.open_position(Position(
            token_id="t", condition_id="c", side="BUY",
            size=10.0, entry_price=0.50, strategy="momentum", order_id="o",
            last_known_price=0.55,
        ))
        is_zombie, fb = _observe_position_price(pt, cfg, "t", None)
        assert is_zombie
        assert fb is None  # alert-only path
