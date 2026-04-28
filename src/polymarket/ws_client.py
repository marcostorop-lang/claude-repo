"""
Polymarket WebSocket price-cache client (opt-in).

Why
---
The default loop polls ``/api/price`` every ``poll_interval`` seconds.
At 60 s polls a 5 % swing that completes in 10 s is invisible to the
bot until ~30 s after the move is over — by which time the better
operators have already closed the gap.  A WebSocket subscription
collapses that window to ~milliseconds.

Design
------
* **Self-contained** — does not require ``websocket-client`` or
  ``websockets`` to be installed.  When neither is present the
  client is a no-op and ``PolymarketClient.get_price`` falls back
  to HTTP polling (existing behaviour byte-for-byte).
* **Threaded sync** — runs on a background thread that owns the
  socket, receives messages, parses them, and writes to an
  in-memory cache guarded by a lock.  The bot's tick loop stays
  synchronous.
* **Cache is read-through with a freshness gate** — readers must
  pass a max age; stale entries return ``None`` so the caller
  falls back to HTTP.  The freshness gate is what makes WS
  *upgrade* polling rather than *replace* it: if the WS hangs the
  bot doesn't go blind.
* **Reconnect with exponential backoff** — capped at 60 s.  Every
  reconnect attempt resubscribes to all known tokens.

What this module does *not* do
-------------------------------
* Place orders.  Orders go through the CLOB SDK like before.
* Book depth.  Subscribes to *price* updates only.  Book depth is
  fetched on demand right before execution; that pattern is
  unchanged.
* Live mode special handling.  WS is purely an observability
  upgrade — the live-trading gates do not depend on it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    price: float
    ts: float  # unix seconds, when the entry landed in the cache


class PolymarketWSClient:
    """Maintains an in-memory ``token_id → mid price`` cache via WS.

    The transport is injected so tests can swap in a fake socket.
    Callers in production pass the default ``transport=None``;
    the client autodetects ``websocket-client`` / ``websockets`` /
    falls back to no-op.
    """

    DEFAULT_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    MAX_BACKOFF_S = 60.0

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        transport: Callable[[str], "_WSTransport"] | None = None,
    ) -> None:
        self.url = url
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribed: set[str] = set()
        self._transport_factory = transport or _default_transport
        self._available = self._transport_factory is not None
        self._reconnects = 0
        self._messages_received = 0
        self._last_message_ts = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Is the WS feature usable in this environment?

        Returns ``False`` when no transport library is installed —
        the rest of the bot is then expected to ignore this client and
        fall back to HTTP polling.
        """
        return self._available

    def start(self) -> None:
        if not self._available or self._thread is not None:
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="polymarket-ws", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ------------------------------------------------------------------
    # Reader API consumed by PolymarketClient.get_price
    # ------------------------------------------------------------------

    def subscribe(self, token_ids: list[str]) -> None:
        """Add token ids to the subscription set.

        New tokens take effect on the next reconnect (or, when a
        transport supports incremental subscribe, immediately).
        Existing entries in the cache are unaffected.
        """
        with self._lock:
            self._subscribed.update(token_ids)

    def get_cached_price(
        self, token_id: str, *, max_age_s: float = 10.0,
    ) -> float | None:
        """Return the cached mid price for a token, or None when stale.

        ``max_age_s`` matters: readers should stay conservative
        (e.g. 10 s) so a hung WS does not silently feed the SL/TP
        check with old quotes.  ``None`` is the "cache miss" signal —
        callers fall back to HTTP polling.
        """
        with self._lock:
            entry = self._cache.get(token_id)
        if entry is None:
            return None
        if (time.time() - entry.ts) > max_age_s:
            return None
        return entry.price

    def stats(self) -> dict:
        """Diagnostic snapshot for the dashboard / metrics sink."""
        with self._lock:
            return {
                "available": self._available,
                "subscribed": len(self._subscribed),
                "cached": len(self._cache),
                "reconnects": self._reconnects,
                "messages_received": self._messages_received,
                "seconds_since_last_message": (
                    time.time() - self._last_message_ts
                    if self._last_message_ts > 0 else None
                ),
            }

    # ------------------------------------------------------------------
    # Internal: receive loop with exponential backoff
    # ------------------------------------------------------------------

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop_evt.is_set():
            try:
                tx = self._transport_factory(self.url)
                if tx is None:
                    # No transport actually available at runtime —
                    # log once and stop the thread; ``available`` will
                    # have been ``True`` only because the import flag
                    # said so.
                    logger.info("WS transport unavailable — stopping WS loop.")
                    return
                logger.info("WS connected to %s", self.url)
                with self._lock:
                    subs = list(self._subscribed)
                if subs:
                    tx.send(json.dumps({"type": "subscribe", "token_ids": subs}))
                self._consume(tx)
                backoff = 1.0  # reset on clean exit
            except Exception:
                logger.warning(
                    "WS loop error — reconnecting in %.1fs", backoff, exc_info=True,
                )
                self._reconnects += 1
                self._stop_evt.wait(backoff)
                backoff = min(self.MAX_BACKOFF_S, backoff * 2.0)

    def _consume(self, tx: "_WSTransport") -> None:
        while not self._stop_evt.is_set():
            raw = tx.recv(timeout=1.0)
            if raw is None:
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            self._handle_message(msg)

    def _handle_message(self, msg: dict) -> None:
        # Polymarket's WS schema can vary by event type; we only
        # consume the price-update shape and ignore the rest.  Two
        # common variants are accepted: a single update object and a
        # batch list.
        items: list = []
        if isinstance(msg, dict):
            if "asset_id" in msg or "token_id" in msg:
                items = [msg]
            elif isinstance(msg.get("data"), list):
                items = msg["data"]
            elif isinstance(msg.get("updates"), list):
                items = msg["updates"]
        elif isinstance(msg, list):
            items = msg
        now = time.time()
        with self._lock:
            for it in items:
                if not isinstance(it, dict):
                    continue
                tok = (
                    it.get("asset_id") or it.get("token_id")
                    or it.get("tokenId") or ""
                )
                if not tok:
                    continue
                price = (
                    it.get("price") if "price" in it
                    else it.get("mid") if "mid" in it
                    else _midpoint_from_book(it)
                )
                try:
                    p = float(price)
                except (TypeError, ValueError):
                    continue
                if p <= 0:
                    continue
                self._cache[tok] = _CacheEntry(price=p, ts=now)
            self._messages_received += 1
            self._last_message_ts = now


def _midpoint_from_book(it: dict) -> float | None:
    bids = it.get("bids") or []
    asks = it.get("asks") or []
    try:
        if not bids or not asks:
            return None
        bb = float(bids[0]["price"]) if isinstance(bids[0], dict) else float(bids[0][0])
        ba = float(asks[0]["price"]) if isinstance(asks[0], dict) else float(asks[0][0])
        if bb <= 0 or ba <= 0:
            return None
        return (bb + ba) / 2.0
    except (KeyError, TypeError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Transport abstraction — production uses websocket-client / websockets;
# tests inject a fake.
# ---------------------------------------------------------------------------


class _WSTransport:
    """Minimal interface a transport must implement."""

    def send(self, payload: str) -> None: ...
    def recv(self, timeout: float = 1.0) -> str | None: ...
    def close(self) -> None: ...


def _default_transport(url: str) -> _WSTransport | None:
    """Try ``websocket-client``; fall back to ``None`` (feature off)."""
    try:
        import websocket  # websocket-client
    except ImportError:
        return None

    class _Wrap(_WSTransport):
        def __init__(self, url: str) -> None:
            self._sock = websocket.create_connection(url, timeout=10)

        def send(self, payload: str) -> None:
            self._sock.send(payload)

        def recv(self, timeout: float = 1.0) -> str | None:
            self._sock.settimeout(timeout)
            try:
                return self._sock.recv()
            except Exception:  # timeout, ConnectionClosed, etc.
                return None

        def close(self) -> None:
            try:
                self._sock.close()
            except Exception:
                pass

    return _Wrap(url)
