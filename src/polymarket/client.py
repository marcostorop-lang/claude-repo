"""
Thin wrapper around Polymarket APIs.

Combines the official CLOB client (when available) with direct HTTP calls
to the Gamma API for market metadata.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import Config
from src.polymarket.auth import build_clob_client

logger = logging.getLogger(__name__)

# Gamma API paginates at 100 records
_GAMMA_PAGE_SIZE = 100


class PolymarketClient:
    """Unified access to Polymarket data and (optionally) trading."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._http = httpx.Client(timeout=30, follow_redirects=True)
        self._clob = build_clob_client(cfg)

    # ------------------------------------------------------------------
    # Gamma API helpers (public, no auth)
    # ------------------------------------------------------------------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def _gamma_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET request to the Gamma API with automatic retries."""
        url = f"{self.cfg.gamma_url}{path}"
        resp = self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def get_active_markets(self, limit: int = _GAMMA_PAGE_SIZE, offset: int = 0) -> list[dict]:
        """Fetch active, non-resolved markets from the Gamma API."""
        params = {
            "limit": limit,
            "offset": offset,
            "active": "true",
            "closed": "false",
        }
        return self._gamma_get("/markets", params=params)

    def get_all_active_markets(self) -> list[dict]:
        """Paginate through all active markets."""
        markets: list[dict] = []
        offset = 0
        while True:
            page = self.get_active_markets(limit=_GAMMA_PAGE_SIZE, offset=offset)
            if not page:
                break
            markets.extend(page)
            if len(page) < _GAMMA_PAGE_SIZE:
                break
            offset += _GAMMA_PAGE_SIZE
        return markets

    # ------------------------------------------------------------------
    # CLOB API helpers (may require auth for trading)
    # ------------------------------------------------------------------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def get_order_book(self, token_id: str) -> dict | None:
        """Fetch the order book for a given token via the CLOB client."""
        if self._clob is None:
            logger.debug("CLOB client not available; skipping order book fetch.")
            return None
        try:
            return self._clob.get_order_book(token_id)
        except Exception:
            logger.exception("Error fetching order book for token %s", token_id)
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def get_midpoint(self, token_id: str) -> float | None:
        """Return the midpoint price for a token, or None on failure."""
        if self._clob is None:
            return None
        try:
            mid = self._clob.get_midpoint(token_id)
            return float(mid) if mid is not None else None
        except Exception:
            logger.exception("Error fetching midpoint for token %s", token_id)
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def get_spread(self, token_id: str) -> float | None:
        """Return the spread for a token, or None on failure."""
        if self._clob is None:
            return None
        try:
            spread = self._clob.get_spread(token_id)
            return float(spread) if spread is not None else None
        except Exception:
            logger.exception("Error fetching spread for token %s", token_id)
            return None

    def get_price(self, token_id: str) -> float | None:
        """Return the last/midpoint price for a token."""
        return self.get_midpoint(token_id)

    # ------------------------------------------------------------------
    # Order placement (live only)
    # ------------------------------------------------------------------

    def place_order(self, order_payload: dict) -> dict | None:
        """
        Place an order via the CLOB client.

        Only works when ``cfg.is_live`` is True **and** a valid CLOB client
        with credentials is available.  Returns the API response dict or
        None if in paper mode.
        """
        if not self.cfg.is_live:
            logger.warning("place_order called but not in live mode — ignoring.")
            return None
        if self._clob is None:
            logger.error("Cannot place order: CLOB client not initialised.")
            return None
        try:
            resp = self._clob.post_order(order_payload)
            logger.info("Order placed: %s", resp)
            return resp
        except Exception:
            logger.exception("Failed to place order.")
            return None

    def close(self) -> None:
        self._http.close()
