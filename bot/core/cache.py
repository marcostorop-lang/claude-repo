"""Async-safe in-memory cache with per-key TTL.

Used to avoid redundant HTTP calls within the same scan cycle
(book fetches, market data, news headlines).
"""

from __future__ import annotations

import time
from typing import Any


class TTLCache:
    """Simple key-value cache with per-key expiration."""

    def __init__(self, default_ttl: float = 30.0, max_size: int = 500) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        value, expires_at = entry
        if time.time() > expires_at:
            del self._store[key]
            self._misses += 1
            return None
        self._hits += 1
        return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        if len(self._store) >= self._max_size:
            self._evict_expired()
            if len(self._store) >= self._max_size:
                oldest_key = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest_key]
        expires = time.time() + (ttl if ttl is not None else self._default_ttl)
        self._store[key] = (value, expires)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [k for k, (_, exp) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]

    def stats(self) -> dict:
        self._evict_expired()
        return {
            "size": len(self._store),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(1, self._hits + self._misses), 3),
        }
