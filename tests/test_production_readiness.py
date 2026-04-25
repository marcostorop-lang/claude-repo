"""Tests for production-readiness features: error monitor, state persistence, graceful shutdown."""

from __future__ import annotations

import json
import time
from pathlib import Path


# ===========================================================================
# ErrorMonitor
# ===========================================================================


class TestErrorMonitor:
    def test_record_and_summary(self):
        from bot.core.error_monitor import ErrorMonitor
        m = ErrorMonitor()
        m.record("api", "CoinGecko timeout")
        m.record("api", "ESPN 503")
        m.record("strategy", "prob_arb crash")
        s = m.summary()
        assert s["total_errors"] == 3
        assert s["categories"]["api"] == 2
        assert s["categories"]["strategy"] == 1
        assert s["errors_last_5m"] == 3

    def test_record_with_exception(self):
        from bot.core.error_monitor import ErrorMonitor
        m = ErrorMonitor()
        try:
            raise ValueError("test error")
        except ValueError as exc:
            m.record("test", "something broke", exc=exc)
        s = m.summary()
        assert s["total_errors"] == 1
        assert len(s["last_errors"]) == 1

    def test_alert_threshold_fires(self):
        from bot.core.error_monitor import ErrorMonitor
        alerts = []
        m = ErrorMonitor(alert_threshold=3, alert_window_s=60)
        m.on_alert(lambda cat, count, msg: alerts.append((cat, count)))
        m.record("api", "err1")
        m.record("api", "err2")
        assert len(alerts) == 0
        m.record("api", "err3")
        assert len(alerts) == 1
        assert alerts[0] == ("api", 3)

    def test_clear_resets(self):
        from bot.core.error_monitor import ErrorMonitor
        m = ErrorMonitor()
        m.record("x", "y")
        m.clear()
        s = m.summary()
        assert s["total_errors"] == 0

    def test_summary_includes_uptime(self):
        from bot.core.error_monitor import ErrorMonitor
        m = ErrorMonitor()
        s = m.summary()
        assert "uptime_s" in s
        assert s["uptime_s"] >= 0

    def test_error_rate_per_hour(self):
        from bot.core.error_monitor import ErrorMonitor
        m = ErrorMonitor()
        m.record("a", "b")
        s = m.summary()
        assert s["error_rate_per_hour"] > 0

    def test_max_recent_capped(self):
        from bot.core.error_monitor import ErrorMonitor
        m = ErrorMonitor()
        m._max_recent = 5
        for i in range(20):
            m.record("cat", f"err{i}")
        assert len(m._recent) == 5

    def test_context_stored(self):
        from bot.core.error_monitor import ErrorMonitor
        m = ErrorMonitor()
        m.record("api", "timeout", context={"url": "https://example.com"})
        assert m._recent[0].context["url"] == "https://example.com"


# ===========================================================================
# State Persistence
# ===========================================================================


class TestStatePersistence:
    def test_save_and_load(self, tmp_path, monkeypatch):
        import bot.core.state_persistence as sp
        state_file = tmp_path / "bot_state.json"
        monkeypatch.setattr(sp, "_STATE_FILE", state_file)

        positions = [
            {"token_id": "tok1", "condition_id": "cond1", "side": "BUY",
             "size": 10.0, "entry_price": 0.5, "strategy": "prob_arb"},
        ]
        ok = sp.save_state(
            positions=positions, equity=950.0, total_pnl=-50.0,
            daily_pnl=-10.0, cycle_count=42, peak_equity=1000.0,
        )
        assert ok is True
        assert state_file.exists()

        loaded = sp.load_state()
        assert loaded is not None
        assert loaded["equity"] == 950.0
        assert loaded["cycle_count"] == 42
        assert len(loaded["positions"]) == 1
        assert loaded["positions"][0]["token_id"] == "tok1"

    def test_load_returns_none_when_no_file(self, tmp_path, monkeypatch):
        import bot.core.state_persistence as sp
        monkeypatch.setattr(sp, "_STATE_FILE", tmp_path / "nonexistent.json")
        assert sp.load_state() is None

    def test_load_returns_none_for_invalid_json(self, tmp_path, monkeypatch):
        import bot.core.state_persistence as sp
        state_file = tmp_path / "bot_state.json"
        state_file.write_text("not valid json", encoding="utf-8")
        monkeypatch.setattr(sp, "_STATE_FILE", state_file)
        assert sp.load_state() is None

    def test_load_rejects_old_state(self, tmp_path, monkeypatch):
        import bot.core.state_persistence as sp
        state_file = tmp_path / "bot_state.json"
        old_state = {
            "saved_at": time.time() - 86400 * 10,  # 10 days ago
            "positions": [], "equity": 1000, "total_pnl": 0,
            "daily_pnl": 0, "cycle_count": 1, "peak_equity": 1000,
        }
        state_file.write_text(json.dumps(old_state), encoding="utf-8")
        monkeypatch.setattr(sp, "_STATE_FILE", state_file)
        assert sp.load_state() is None

    def test_clear_state(self, tmp_path, monkeypatch):
        import bot.core.state_persistence as sp
        state_file = tmp_path / "bot_state.json"
        state_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(sp, "_STATE_FILE", state_file)
        sp.clear_state()
        assert not state_file.exists()

    def test_save_uses_atomic_write(self, tmp_path, monkeypatch):
        import bot.core.state_persistence as sp
        state_file = tmp_path / "bot_state.json"
        monkeypatch.setattr(sp, "_STATE_FILE", state_file)
        sp.save_state(
            positions=[], equity=1000, total_pnl=0,
            daily_pnl=0, cycle_count=1, peak_equity=1000,
        )
        # Verify no .tmp file lingering
        assert not (tmp_path / "bot_state.tmp").exists()
        assert state_file.exists()

    def test_save_with_extra_data(self, tmp_path, monkeypatch):
        import bot.core.state_persistence as sp
        state_file = tmp_path / "bot_state.json"
        monkeypatch.setattr(sp, "_STATE_FILE", state_file)
        sp.save_state(
            positions=[], equity=1000, total_pnl=0,
            daily_pnl=0, cycle_count=1, peak_equity=1000,
            extra={"error_count": 5},
        )
        loaded = sp.load_state()
        assert loaded["extra"]["error_count"] == 5


# ===========================================================================
# Main loop wiring
# ===========================================================================


class TestMainWiring:
    def test_main_imports_error_monitor(self):
        from bot.core.error_monitor import monitor
        assert monitor is not None
        assert hasattr(monitor, "record")
        assert hasattr(monitor, "summary")

    def test_main_imports_state_persistence(self):
        from bot.core.state_persistence import save_state, load_state
        assert callable(save_state)
        assert callable(load_state)

    def test_position_serialization_roundtrip(self):
        from bot.core.risk_manager import Position
        from bot.core.utils import Side
        p = Position(
            token_id="tok1", condition_id="cond1", side=Side.BUY,
            size=10, entry_price=0.5, strategy="prob_arb",
            timestamp=1234567890.0, category="crypto",
        )
        d = {
            "token_id": p.token_id, "condition_id": p.condition_id,
            "side": p.side.value, "size": p.size,
            "entry_price": p.entry_price, "strategy": p.strategy,
            "timestamp": p.timestamp, "category": p.category,
        }
        p2 = Position(
            token_id=d["token_id"], condition_id=d["condition_id"],
            side=Side(d["side"]), size=d["size"],
            entry_price=d["entry_price"], strategy=d["strategy"],
            timestamp=d["timestamp"], category=d["category"],
        )
        assert p2.token_id == p.token_id
        assert p2.side == p.side
        assert p2.entry_price == p.entry_price
