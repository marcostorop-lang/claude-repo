"""Tests for trace-ID propagation."""

from __future__ import annotations

import logging

from src.utils.trace import (
    TraceContextFilter,
    current_order_id,
    current_tick_id,
    order_scope,
    tick_scope,
)


def test_default_ids_empty():
    assert current_tick_id() == ""
    assert current_order_id() == ""


def test_tick_scope_sets_and_resets():
    assert current_tick_id() == ""
    with tick_scope("test-tick-1") as tid:
        assert tid == "test-tick-1"
        assert current_tick_id() == "test-tick-1"
    assert current_tick_id() == ""


def test_tick_scope_auto_generates():
    with tick_scope() as tid:
        assert len(tid) == 12
        assert current_tick_id() == tid


def test_order_scope_independent_of_tick():
    with tick_scope("tick-A"):
        with order_scope("ord-1"):
            assert current_tick_id() == "tick-A"
            assert current_order_id() == "ord-1"
        # Order scope released, tick scope alive.
        assert current_tick_id() == "tick-A"
        assert current_order_id() == ""


def test_filter_attaches_ids_to_record():
    f = TraceContextFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="hi", args=(), exc_info=None,
    )
    with tick_scope("tick-X"), order_scope("ord-Y"):
        assert f.filter(record) is True
        assert record.tick_id == "tick-X"
        assert record.order_id == "ord-Y"


def test_nested_tick_scope_restores_outer():
    with tick_scope("outer"):
        assert current_tick_id() == "outer"
        with tick_scope("inner"):
            assert current_tick_id() == "inner"
        assert current_tick_id() == "outer"
