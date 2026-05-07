"""Lightweight trace-ID propagation for the bot loop.

Two scopes:

* ``tick_id`` — one per ``_tick`` invocation, propagated via
  ``contextvars`` so any logger call inside the tick (including from
  modules we don't own, like the SDK) can be correlated.
* ``order_id`` — one per ``OrderRequest``, generated when the order
  is built and threaded through to the fill record.

We deliberately keep this self-contained (no OpenTelemetry dependency)
because the runbook constraint is "minimal external surface" and we
only need correlation, not distributed tracing.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from typing import Iterator
from contextlib import contextmanager


_tick_id: contextvars.ContextVar[str] = contextvars.ContextVar("tick_id", default="")
_order_id: contextvars.ContextVar[str] = contextvars.ContextVar("order_id", default="")


def new_tick_id() -> str:
    """12-hex-char tick ID — short enough to embed in every log line."""
    return uuid.uuid4().hex[:12]


def new_order_id() -> str:
    """12-hex-char order ID with explicit ``ord-`` prefix."""
    return "ord-" + uuid.uuid4().hex[:12]


def current_tick_id() -> str:
    return _tick_id.get()


def current_order_id() -> str:
    return _order_id.get()


@contextmanager
def tick_scope(tick_id: str | None = None) -> Iterator[str]:
    """Set ``tick_id`` for the duration of a ``with`` block."""
    tid = tick_id or new_tick_id()
    token = _tick_id.set(tid)
    try:
        yield tid
    finally:
        _tick_id.reset(token)


@contextmanager
def order_scope(order_id: str | None = None) -> Iterator[str]:
    oid = order_id or new_order_id()
    token = _order_id.set(oid)
    try:
        yield oid
    finally:
        _order_id.reset(token)


class TraceContextFilter(logging.Filter):
    """Inject ``tick_id`` / ``order_id`` onto every log record.

    Adds them as record attributes so a custom formatter can include
    ``[%(tick_id)s/%(order_id)s]`` if the operator wants verbose
    correlation, while leaving the default formatter unchanged.  The
    filter never blocks records (always returns True).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.tick_id = _tick_id.get()
        record.order_id = _order_id.get()
        return True
