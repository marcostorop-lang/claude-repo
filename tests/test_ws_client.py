"""Tests for the optional WebSocket price-cache client.

Covers the cache contract, staleness gate, the fake-transport receive
loop, and the integration with ``PolymarketClient.get_price`` so a
hung WS never silently feeds stale quotes.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest

from src.polymarket.ws_client import PolymarketWSClient, _WSTransport


# ---------------------------------------------------------------------------
# Fake transport — fully deterministic for tests.
# ---------------------------------------------------------------------------


class _FakeTransport(_WSTransport):
    """Captures sent payloads and serves preloaded receive frames."""

    def __init__(self, frames: list[str] | None = None) -> None:
        self.sent: list[str] = []
        self._frames: queue.Queue = queue.Queue()
        for f in frames or []:
            self._frames.put(f)
        self.closed = False

    def push(self, frame: str) -> None:
        self._frames.put(frame)

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self, timeout: float = 1.0) -> str | None:
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.closed = True


def _factory_returning(transport: _FakeTransport):
    return lambda url: transport


# ---------------------------------------------------------------------------
# Cache contract
# ---------------------------------------------------------------------------


class TestCacheContract:
    def test_unknown_token_returns_none(self):
        cli = PolymarketWSClient(transport=_factory_returning(_FakeTransport()))
        assert cli.get_cached_price("nope") is None

    def test_get_cached_price_after_message(self):
        tx = _FakeTransport()
        cli = PolymarketWSClient(transport=_factory_returning(tx))
        cli._handle_message({"asset_id": "tok1", "price": "0.55"})
        assert cli.get_cached_price("tok1") == 0.55

    def test_max_age_s_filters_stale(self):
        tx = _FakeTransport()
        cli = PolymarketWSClient(transport=_factory_returning(tx))
        cli._handle_message({"asset_id": "tok1", "price": 0.55})
        # Forge a stale ts directly.
        with cli._lock:
            cli._cache["tok1"].ts = time.time() - 100
        assert cli.get_cached_price("tok1", max_age_s=10) is None
        # But fresh enough with a wider window.
        assert cli.get_cached_price("tok1", max_age_s=200) == 0.55

    def test_handle_batch_data(self):
        tx = _FakeTransport()
        cli = PolymarketWSClient(transport=_factory_returning(tx))
        cli._handle_message({
            "data": [
                {"asset_id": "a", "price": 0.40},
                {"asset_id": "b", "price": 0.60},
            ],
        })
        assert cli.get_cached_price("a") == 0.40
        assert cli.get_cached_price("b") == 0.60

    def test_handle_book_midpoint(self):
        tx = _FakeTransport()
        cli = PolymarketWSClient(transport=_factory_returning(tx))
        cli._handle_message({
            "asset_id": "tok1",
            "bids": [{"price": "0.50"}],
            "asks": [{"price": "0.52"}],
        })
        assert cli.get_cached_price("tok1") == pytest.approx(0.51)

    def test_negative_or_zero_price_ignored(self):
        cli = PolymarketWSClient(transport=_factory_returning(_FakeTransport()))
        cli._handle_message({"asset_id": "tok1", "price": -0.1})
        cli._handle_message({"asset_id": "tok1", "price": 0})
        assert cli.get_cached_price("tok1") is None

    def test_malformed_message_does_not_crash(self):
        cli = PolymarketWSClient(transport=_factory_returning(_FakeTransport()))
        cli._handle_message("garbage")  # type: ignore[arg-type]
        cli._handle_message({"no_token": True})
        cli._handle_message({"asset_id": "tok1", "price": "not-a-number"})
        assert cli.stats()["cached"] == 0


# ---------------------------------------------------------------------------
# Subscription tracking
# ---------------------------------------------------------------------------


class TestSubscribe:
    def test_subscribe_dedupes(self):
        cli = PolymarketWSClient(transport=_factory_returning(_FakeTransport()))
        cli.subscribe(["a", "b", "a"])
        cli.subscribe(["b", "c"])
        assert cli._subscribed == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Background thread receive loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    def test_run_loop_writes_to_cache_and_subscribes(self):
        tx = _FakeTransport(frames=[
            json.dumps({"asset_id": "tok1", "price": 0.42}),
            json.dumps({"asset_id": "tok2", "price": 0.77}),
        ])
        cli = PolymarketWSClient(transport=_factory_returning(tx))
        cli.subscribe(["tok1", "tok2"])
        cli.start()
        # Wait briefly for the consumer to drain the frames.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if cli.stats()["messages_received"] >= 2:
                break
            time.sleep(0.01)
        cli.stop(timeout=2.0)
        assert cli.get_cached_price("tok1") == 0.42
        assert cli.get_cached_price("tok2") == 0.77
        # Subscribe payload was sent on connect.
        assert len(tx.sent) >= 1
        first = json.loads(tx.sent[0])
        assert first["type"] == "subscribe"
        assert set(first["token_ids"]) == {"tok1", "tok2"}

    def test_unavailable_when_transport_factory_is_none(self):
        cli = PolymarketWSClient(transport=lambda url: None)
        # Factory returns None — the run loop logs and exits.  A start
        # call is safe; available stays True at construction (we don't
        # know yet) but stats() will report no messages.
        cli.start()
        cli.stop(timeout=2.0)
        assert cli.stats()["messages_received"] == 0


# ---------------------------------------------------------------------------
# Integration with PolymarketClient.get_price
# ---------------------------------------------------------------------------


class TestClientIntegration:
    def test_fresh_cache_hit_skips_http(self):
        from src.polymarket.client import PolymarketClient
        from src.config import Config
        cli = PolymarketClient(Config())
        # Fake a WS cache with a fresh entry.
        ws = MagicMock()
        ws.get_cached_price.return_value = 0.55
        cli.ws_client = ws
        # Spy on the HTTP path.
        cli.get_midpoint = MagicMock(return_value=0.99)  # type: ignore[assignment]
        assert cli.get_price("tok1") == 0.55
        cli.get_midpoint.assert_not_called()

    def test_stale_cache_falls_back_to_http(self):
        from src.polymarket.client import PolymarketClient
        from src.config import Config
        cli = PolymarketClient(Config())
        ws = MagicMock()
        ws.get_cached_price.return_value = None  # stale → cache miss
        cli.ws_client = ws
        cli.get_midpoint = MagicMock(return_value=0.62)  # type: ignore[assignment]
        assert cli.get_price("tok1") == 0.62
        cli.get_midpoint.assert_called_once_with("tok1")

    def test_no_ws_client_uses_http(self):
        from src.polymarket.client import PolymarketClient
        from src.config import Config
        cli = PolymarketClient(Config())
        # ws_client stays None.
        cli.get_midpoint = MagicMock(return_value=0.50)  # type: ignore[assignment]
        assert cli.get_price("tok1") == 0.50
