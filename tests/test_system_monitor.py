"""Tests for systemic-failure alerts (API, DB, heartbeat, auth)."""

from __future__ import annotations

import time
from unittest import mock

import pytest

from src.utils.alerts import AlertManager, AlertSink, Alert
from src.utils.system_monitor import SystemMonitor


class _RecordingSink:
    def __init__(self):
        self.alerts: list[Alert] = []

    def emit(self, alert: Alert) -> None:
        self.alerts.append(alert)


@pytest.fixture
def sink_and_monitor():
    sink = _RecordingSink()
    mgr = AlertManager(sinks=[sink], dedupe_window_s=0)  # dedupe disabled for testing
    mon = SystemMonitor(manager=mgr, api_min_samples=5, api_window_seconds=60)
    return sink, mon


class TestApiErrorRate:
    def test_no_alert_below_threshold(self, sink_and_monitor):
        sink, mon = sink_and_monitor
        for _ in range(5):
            mon.record_api_call(was_error=False)
        assert sink.alerts == []

    def test_alert_when_rate_exceeded(self, sink_and_monitor):
        sink, mon = sink_and_monitor
        # 4 successes, 6 errors → 60% > 20% threshold
        for _ in range(4):
            mon.record_api_call(was_error=False)
        for _ in range(6):
            mon.record_api_call(was_error=True)
        # At least one warning was emitted.
        warns = [a for a in sink.alerts if a.severity == "warning"]
        assert any("error-rate" in a.subject.lower() for a in warns)

    def test_min_samples_gate(self, sink_and_monitor):
        sink, mon = sink_and_monitor
        # All errors but only 4 samples (< 5 min) → no alert
        for _ in range(4):
            mon.record_api_call(was_error=True)
        assert sink.alerts == []

    def test_window_eviction(self, sink_and_monitor):
        sink, mon = sink_and_monitor
        # Fake an old error outside the window then 5 successes
        old_ts = time.time() - 1000
        mon._api_events.append((old_ts, True))
        for _ in range(5):
            mon.record_api_call(was_error=False)
        # Stale event was evicted on the next sliding step.
        assert all(not e for _, e in mon._api_events)
        assert sink.alerts == []


class TestDbAndAuth:
    def test_db_error_is_critical(self, sink_and_monitor):
        sink, mon = sink_and_monitor
        mon.record_db_error("insert_trade", "disk full")
        assert any(a.severity == "critical" for a in sink.alerts)
        assert any("SQLite" in a.subject for a in sink.alerts)

    def test_db_error_truncates_long_messages(self, sink_and_monitor):
        sink, mon = sink_and_monitor
        big = "x" * 5000
        mon.record_db_error("insert_trade", big)
        assert all(len(a.detail.get("error", "")) <= 300 for a in sink.alerts)

    def test_auth_failure_is_critical(self, sink_and_monitor):
        sink, mon = sink_and_monitor
        mon.record_auth_failure("clob", "401 unauthorised")
        crits = [a for a in sink.alerts if a.severity == "critical"]
        assert crits and "Auth" in crits[0].subject


class TestHeartbeat:
    def test_no_alert_before_first_heartbeat(self, sink_and_monitor):
        sink, mon = sink_and_monitor
        # Never called heartbeat() → check_heartbeat is a no-op.
        mon.check_heartbeat()
        assert sink.alerts == []

    def test_no_alert_when_recent(self, sink_and_monitor):
        sink, mon = sink_and_monitor
        mon.heartbeat()
        mon.check_heartbeat()
        assert sink.alerts == []

    def test_alert_when_silent_too_long(self, sink_and_monitor):
        sink, mon = sink_and_monitor
        mon.heartbeat()
        mon.heartbeat_max_silence_s = 0.001
        time.sleep(0.01)
        mon.check_heartbeat()
        crits = [a for a in sink.alerts if a.severity == "critical"]
        assert crits and "heartbeat" in crits[0].subject.lower()


class TestNoOpWithoutSinks:
    def test_no_sinks_no_crash(self):
        """Bot loop must be free to call notify even if no alert config."""
        mgr = AlertManager()  # zero sinks
        mon = SystemMonitor(manager=mgr, api_min_samples=2)
        mon.record_api_call(was_error=True)
        mon.record_api_call(was_error=True)
        mon.record_db_error("op", "boom")
        mon.heartbeat()
        mon.check_heartbeat()
        # No exceptions raised → success.
