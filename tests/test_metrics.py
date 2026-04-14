"""Tests for the structured JSON metrics writer."""

from __future__ import annotations

import json

import pytest

from src.utils.metrics import MetricsWriter, build_from_config


class TestDisabled:
    def test_empty_path_is_noop(self, tmp_path):
        w = MetricsWriter("")
        assert w.enabled is False
        # Does not raise and does not create any file.
        w.emit("tick", markets=10)
        assert list(tmp_path.iterdir()) == []

    def test_build_from_config_disabled(self):
        class C: pass
        w = build_from_config(C())
        assert w.enabled is False


class TestAppendingJsonl:
    def test_one_line_per_call(self, tmp_path):
        p = tmp_path / "m.jsonl"
        w = MetricsWriter(str(p), clock=lambda: 1700000000.0)
        w.emit("tick", markets=10, signals=3)
        w.emit("trade", token="a", side="BUY", size=50.0)

        lines = p.read_text().strip().splitlines()
        assert len(lines) == 2
        r1 = json.loads(lines[0])
        assert r1 == {"ts": 1700000000.0, "event": "tick",
                      "markets": 10, "signals": 3}
        r2 = json.loads(lines[1])
        assert r2["event"] == "trade"
        assert r2["token"] == "a"

    def test_explicit_ts_overrides_clock(self, tmp_path):
        p = tmp_path / "m.jsonl"
        w = MetricsWriter(str(p), clock=lambda: 999.0)
        w.emit("tick", ts="2026-04-14T10:00:00", markets=1)
        rec = json.loads(p.read_text().strip())
        assert rec["ts"] == "2026-04-14T10:00:00"


class TestRobustness:
    def test_unserialisable_field_falls_back_to_repr(self, tmp_path):
        p = tmp_path / "m.jsonl"
        w = MetricsWriter(str(p))

        class Unjsonable:
            def __repr__(self): return "<Unjsonable>"

        w.emit("weird", blob=Unjsonable())
        line = p.read_text().strip()
        # The repr fallback stringifies every field; the line is still JSON.
        rec = json.loads(line)
        assert "blob" in rec
        assert "Unjsonable" in rec["blob"]

    def test_bad_path_logged_not_raised(self, tmp_path):
        w = MetricsWriter(str(tmp_path / "no_such_dir" / "m.jsonl"))
        # Must not raise.
        w.emit("tick", x=1)

    def test_as_dict_protocol_serialised(self, tmp_path):
        p = tmp_path / "m.jsonl"
        w = MetricsWriter(str(p))

        class HasAsDict:
            def as_dict(self): return {"k": "v"}

        w.emit("x", obj=HasAsDict())
        rec = json.loads(p.read_text().strip())
        assert rec["obj"] == {"k": "v"}
