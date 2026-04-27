"""Tests for the drawdown circuit breaker.

The daily-loss breaker resets at UTC midnight and only catches
single-day spirals.  An 8% intraday slide that stops just shy of the
daily cap leaves the bot trading inside an obviously broken regime
the next day.  The drawdown breaker is the catch.
"""

from __future__ import annotations

import os

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import Action, Signal


def _rm(store=None, **env_overrides) -> RiskManager:
    for k, v in env_overrides.items():
        os.environ[k] = str(v)
    cfg = Config()
    pt = PortfolioTracker()
    rm = RiskManager(cfg, pt)
    rm.store = store
    rm.seed_drawdown_state()
    return rm


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


class TestDisabledByDefault:
    def test_zero_threshold_is_no_op(self, monkeypatch):
        monkeypatch.setenv("MAX_DRAWDOWN_PCT", "0.0")
        rm = _rm()
        rm.update_equity(100.0)
        rm.update_equity(50.0)  # would be a 50% drawdown if enabled
        assert rm.is_drawdown_breaker_active is False
        assert rm.is_circuit_breaker_active is False


# ---------------------------------------------------------------------------
# Trip mechanics
# ---------------------------------------------------------------------------


class TestTrip:
    def test_breaker_trips_below_threshold(self, monkeypatch):
        monkeypatch.setenv("MAX_DRAWDOWN_PCT", "0.10")
        rm = _rm()
        rm.update_equity(100.0)  # peak
        rm.update_equity(95.0)   # 5% drawdown — under threshold
        assert rm.is_drawdown_breaker_active is False
        rm.update_equity(89.0)   # 11% drawdown — over threshold
        assert rm.is_drawdown_breaker_active is True
        assert rm.is_circuit_breaker_active is True

    def test_peak_only_grows(self, monkeypatch):
        monkeypatch.setenv("MAX_DRAWDOWN_PCT", "0.10")
        rm = _rm()
        rm.update_equity(50.0)
        rm.update_equity(100.0)
        rm.update_equity(70.0)   # below new peak by 30% → trip
        assert rm.is_drawdown_breaker_active is True
        # And the recorded peak is the highest seen, not the latest.
        assert rm._equity_peak == 100.0

    def test_negative_equity_does_not_set_peak_to_negative(self, monkeypatch):
        monkeypatch.setenv("MAX_DRAWDOWN_PCT", "0.10")
        rm = _rm()
        rm.update_equity(-5.0)
        # Cold start — no peak yet, can't compute drawdown.
        assert rm.is_drawdown_breaker_active is False

    def test_buy_blocked_when_tripped(self, monkeypatch):
        monkeypatch.setenv("MAX_DRAWDOWN_PCT", "0.10")
        # Spread fallback off so we don't trip *that* gate first.
        monkeypatch.setenv("REQUIRE_KNOWN_SPREAD_FOR_BUY", "false")
        rm = _rm()
        rm.update_equity(100.0)
        rm.update_equity(80.0)
        v = rm.check("tok1", Signal(Action.BUY, 0.8), 10, 0.5)
        assert not v.allowed
        assert "drawdown" in v.reason.lower()

    def test_sell_still_allowed_when_tripped(self, monkeypatch):
        monkeypatch.setenv("MAX_DRAWDOWN_PCT", "0.10")
        monkeypatch.setenv("REQUIRE_KNOWN_SPREAD_FOR_BUY", "false")
        rm = _rm()
        rm.update_equity(100.0)
        rm.update_equity(80.0)
        v = rm.check("tok1", Signal(Action.SELL, 0.8), 10, 0.5)
        # Drawdown breaker is BUY-gating; the SELL passes the
        # circuit-breaker gate.  (It may still hit other gates.)
        assert "drawdown" not in (v.reason or "").lower()


# ---------------------------------------------------------------------------
# Persistence across restart
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_peak_and_trip_survive_restart(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAX_DRAWDOWN_PCT", "0.10")
        db = str(tmp_path / "t.db")

        # Phase 1: trip the breaker.
        s1 = SQLiteStore(db)
        try:
            rm1 = _rm(store=s1)
            rm1.update_equity(100.0)
            rm1.update_equity(80.0)
            assert rm1.is_drawdown_breaker_active is True
        finally:
            s1.close()

        # Phase 2: brand-new RiskManager + same DB.
        s2 = SQLiteStore(db)
        try:
            rm2 = _rm(store=s2)
            assert rm2._equity_peak == 100.0
            assert rm2.is_drawdown_breaker_active is True
            # And it persists across yet another update.
            rm2.update_equity(110.0)
            assert rm2._equity_peak == 110.0
            # Trip is still set — only the ack file clears it.
            assert rm2.is_drawdown_breaker_active is True
        finally:
            s2.close()


# ---------------------------------------------------------------------------
# Reset via ack file
# ---------------------------------------------------------------------------


class TestAckReset:
    def test_ack_file_clears_trip_and_persists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAX_DRAWDOWN_PCT", "0.10")
        ack = tmp_path / "drawdown.ack"
        monkeypatch.setenv("DRAWDOWN_BREAKER_ACK_FILE", str(ack))
        monkeypatch.chdir(tmp_path)  # so a relative ack path also works
        db = str(tmp_path / "t.db")
        store = SQLiteStore(db)
        try:
            rm = _rm(store=store)
            rm.update_equity(100.0)
            rm.update_equity(80.0)
            assert rm.is_drawdown_breaker_active is True
            # Operator drops the ack file.
            ack.touch()
            # Reading the property triggers reset_drawdown_breaker.
            assert rm.is_drawdown_breaker_active is False
            # And a brand-new RiskManager picks up the cleared state.
            rm2 = _rm(store=store)
            assert rm2._drawdown_breaker_tripped is False
        finally:
            store.close()

    def test_no_ack_file_keeps_trip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAX_DRAWDOWN_PCT", "0.10")
        monkeypatch.setenv("DRAWDOWN_BREAKER_ACK_FILE", str(tmp_path / "nonexistent.ack"))
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            rm = _rm(store=store)
            rm.update_equity(100.0)
            rm.update_equity(80.0)
            # Several reads — never auto-clears without the file.
            for _ in range(3):
                assert rm.is_drawdown_breaker_active is True
        finally:
            store.close()
