"""Tests for the opt-in alerting module.

Focus on the guarantees that matter for ops:

* alerts never raise,
* a disabled (empty-sinks) manager is a silent no-op,
* de-dupe and rate-limit actually drop the right messages,
* file sink is append-only JSONL and survives re-open,
* webhook sink never crashes the process on HTTP error / timeout.
"""

from __future__ import annotations

import json
import time

import pytest

from src.utils.alerts import (
    Alert,
    AlertManager,
    FileAlertSink,
    WebhookAlertSink,
    build_from_config,
)


# --------------------------------------------------------------------------
# Manager behaviour
# --------------------------------------------------------------------------

class TestManagerNoOpWhenEmpty:
    def test_no_sinks_returns_false(self):
        mgr = AlertManager()
        assert mgr.notify("warning", "test") is False
        # And doesn't record state for de-dupe of non-emitted alerts.
        assert mgr.notify("warning", "test") is False


class TestManagerFanOut:
    def test_all_sinks_receive(self):
        calls = []

        class Spy:
            def emit(self, a):
                calls.append(a)

        mgr = AlertManager(sinks=[Spy(), Spy()])
        assert mgr.notify("info", "hi") is True
        assert len(calls) == 2

    def test_sink_exception_doesnt_propagate(self):
        class Bad:
            def emit(self, a):
                raise RuntimeError("boom")

        calls = []

        class Good:
            def emit(self, a):
                calls.append(a)

        mgr = AlertManager(sinks=[Bad(), Good()])
        # Still returns True — Good sink received it.
        assert mgr.notify("warning", "oops") is True
        assert len(calls) == 1


class TestDedupe:
    def test_duplicate_within_window_suppressed(self):
        calls = []

        class Spy:
            def emit(self, a):
                calls.append(a)

        mgr = AlertManager(sinks=[Spy()], dedupe_window_s=3600.0)
        mgr.notify("warning", "stale price")
        mgr.notify("warning", "stale price")
        mgr.notify("warning", "stale price")
        assert len(calls) == 1

    def test_different_severities_not_deduped(self):
        calls = []

        class Spy:
            def emit(self, a):
                calls.append(a)

        mgr = AlertManager(sinks=[Spy()], dedupe_window_s=3600.0)
        mgr.notify("warning", "s")
        mgr.notify("critical", "s")
        assert len(calls) == 2


class TestRateLimit:
    def test_burst_over_cap_is_dropped(self):
        calls = []

        class Spy:
            def emit(self, a):
                calls.append(a)

        mgr = AlertManager(sinks=[Spy()], dedupe_window_s=0.0, max_per_minute=3)
        for i in range(10):
            mgr.notify("info", f"msg-{i}")
        assert len(calls) == 3


# --------------------------------------------------------------------------
# FileAlertSink
# --------------------------------------------------------------------------

class TestFileSink:
    def test_appends_jsonl(self, tmp_path):
        p = tmp_path / "alerts.jsonl"
        sink = FileAlertSink(p)
        sink.emit(Alert(severity="info", subject="a", detail={"x": 1}))
        sink.emit(Alert(severity="warning", subject="b"))

        lines = p.read_text().strip().splitlines()
        assert len(lines) == 2
        parsed = [json.loads(ln) for ln in lines]
        assert parsed[0]["subject"] == "a"
        assert parsed[0]["detail"] == {"x": 1}
        assert parsed[1]["severity"] == "warning"

    def test_bad_path_logged_not_raised(self, tmp_path):
        # Directory doesn't exist → fails to open.  Must NOT raise.
        sink = FileAlertSink(tmp_path / "missing_dir" / "alerts.jsonl")
        sink.emit(Alert(severity="info", subject="x"))


# --------------------------------------------------------------------------
# WebhookAlertSink
# --------------------------------------------------------------------------

class TestWebhookSink:
    def test_http_error_does_not_raise(self, monkeypatch):
        """A 500 response is logged but not propagated."""
        pytest.importorskip("requests")
        import requests

        class FakeResp:
            status_code = 500

        def fake_post(url, json, timeout):
            return FakeResp()

        monkeypatch.setattr(requests, "post", fake_post)
        sink = WebhookAlertSink("http://example.invalid/hook")
        sink.emit(Alert(severity="critical", subject="t", detail={}))

    def test_network_exception_does_not_raise(self, monkeypatch):
        pytest.importorskip("requests")
        import requests

        def boom(url, json, timeout):
            raise requests.ConnectionError("dns fail")

        monkeypatch.setattr(requests, "post", boom)
        sink = WebhookAlertSink("http://example.invalid/hook")
        sink.emit(Alert(severity="critical", subject="t", detail={}))


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------

class TestBuildFromConfig:
    def test_disabled_config_yields_empty_manager(self):
        class Cfg:
            pass
        mgr = build_from_config(Cfg())
        assert mgr.sinks == []
        assert mgr.notify("info", "nothing") is False

    def test_file_sink_wired(self, tmp_path):
        class Cfg:
            alert_log_file = str(tmp_path / "a.jsonl")
        mgr = build_from_config(Cfg())
        assert len(mgr.sinks) == 1
        mgr.notify("info", "hi")
        assert (tmp_path / "a.jsonl").exists()


class TestWebhookSeverityFilter:
    def test_drops_below_threshold(self, monkeypatch):
        pytest.importorskip("requests")
        import requests

        calls = []

        def fake_post(url, json, timeout):
            calls.append(1)
            class R:
                status_code = 200
            return R()

        monkeypatch.setattr(requests, "post", fake_post)
        sink = WebhookAlertSink("http://example.invalid/hook", min_severity="warning")
        sink.emit(Alert(severity="info", subject="noise", detail={}))
        assert calls == []  # info < warning → dropped
        sink.emit(Alert(severity="warning", subject="alert", detail={}))
        sink.emit(Alert(severity="critical", subject="page", detail={}))
        assert len(calls) == 2

    def test_default_is_info_passes_all(self, monkeypatch):
        pytest.importorskip("requests")
        import requests
        calls = []
        def fake_post(url, json, timeout):
            calls.append(1)
            class R:
                status_code = 200
            return R()
        monkeypatch.setattr(requests, "post", fake_post)
        sink = WebhookAlertSink("http://example.invalid/hook")
        for lvl in ("info", "warning", "critical"):
            sink.emit(Alert(severity=lvl, subject="s", detail={}))
        assert len(calls) == 3

    def test_factory_honours_config_min_severity(self, monkeypatch):
        pytest.importorskip("requests")
        import requests
        calls = []
        def fake_post(url, json, timeout):
            calls.append(json["text"])
            class R:
                status_code = 200
            return R()
        monkeypatch.setattr(requests, "post", fake_post)

        class Cfg:
            alert_webhook_url = "http://example.invalid/hook"
            alert_webhook_min_severity = "critical"

        mgr = build_from_config(Cfg())
        mgr.notify("info", "t1")
        mgr.notify("warning", "t2")
        mgr.notify("critical", "t3")
        # Only critical should have reached the webhook.
        assert len(calls) == 1
        assert "t3" in calls[0]
