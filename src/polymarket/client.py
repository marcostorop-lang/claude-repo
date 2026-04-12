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

    def get_active_markets_limited(self, max_total: int = 500) -> list[dict]:
        """Fetch active markets with an upper bound on total records.

        This prevents downloading all 50K+ markets every tick, which causes
        500+ API calls and multi-minute tick times.
        """
        markets: list[dict] = []
        offset = 0
        while len(markets) < max_total:
            page = self.get_active_markets(limit=_GAMMA_PAGE_SIZE, offset=offset)
            if not page:
                break
            markets.extend(page)
            if len(page) < _GAMMA_PAGE_SIZE:
                break
            offset += _GAMMA_PAGE_SIZE
        if len(markets) >= max_total:
            logger.info("Market fetch capped at %d (limit=%d).", len(markets), max_total)
        return markets[:max_total]

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
            return self._parse_numeric(mid)
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
            return self._parse_numeric(spread)
        except Exception:
            logger.exception("Error fetching spread for token %s", token_id)
            return None

    @staticmethod
    def _parse_numeric(value: object) -> float | None:
        """Extract a float from an SDK response (may be float, str, or dict)."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        if isinstance(value, dict):
            # The CLOB SDK returns {"mid": "0.55"} or {"spread": "0.02"}
            for key in ("mid", "midpoint", "spread", "price"):
                if key in value:
                    try:
                        return float(value[key])
                    except (TypeError, ValueError):
                        pass
            # Fallback: try the first numeric-looking value
            for v in value.values():
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    def get_price(self, token_id: str) -> float | None:
        """Return the last/midpoint price for a token."""
        return self.get_midpoint(token_id)

    def get_top_of_book(self, token_id: str) -> dict | None:
        """Return ``{"best_bid", "best_ask", "bid_size", "ask_size"}``.

        Extracted from the CLOB order book. Returns ``None`` on any failure
        so callers can fall back to the midpoint.

        NB: We intentionally only fetch this right before execution — fetching
        it for every scanned market would be prohibitively expensive.
        """
        book = self.get_order_book(token_id)
        if not book:
            return None
        try:
            bids = getattr(book, "bids", None) or (book.get("bids") if isinstance(book, dict) else None) or []
            asks = getattr(book, "asks", None) or (book.get("asks") if isinstance(book, dict) else None) or []
            if not bids or not asks:
                return None

            def _lvl(level):
                # CLOB SDK may return an OrderSummary object or a dict
                price = getattr(level, "price", None)
                size = getattr(level, "size", None)
                if price is None and isinstance(level, dict):
                    price = level.get("price")
                    size = level.get("size")
                try:
                    return float(price), float(size)
                except (TypeError, ValueError):
                    return None, None

            # Bids are sorted descending; asks ascending. Take the first entry.
            best_bid_price, best_bid_size = _lvl(bids[0])
            best_ask_price, best_ask_size = _lvl(asks[0])
            # Some SDKs return bids ascending; guard by picking max/min explicitly.
            bid_prices = [_lvl(b)[0] for b in bids if _lvl(b)[0] is not None]
            ask_prices = [_lvl(a)[0] for a in asks if _lvl(a)[0] is not None]
            if not bid_prices or not ask_prices:
                return None
            best_bid_price = max(bid_prices)
            best_ask_price = min(ask_prices)
            if best_bid_price is None or best_ask_price is None:
                return None
            return {
                "best_bid": best_bid_price,
                "best_ask": best_ask_price,
                "bid_size": best_bid_size or 0.0,
                "ask_size": best_ask_size or 0.0,
            }
        except Exception:
            logger.debug("Failed to parse order book for %s", token_id[:12], exc_info=True)
            return None

    def get_book_analysis(self, token_id: str, fill_size_usd: float = 50.0):
        """Fetch the order book and return a full depth analysis.

        Returns a BookAnalysis object or None on failure.  This is more
        expensive than get_top_of_book() — use only at execution time.
        """
        from src.analysis.book_depth import BookLevel, analyze_book
        book = self.get_order_book(token_id)
        if not book:
            return None
        try:
            raw_bids = getattr(book, "bids", None) or (book.get("bids") if isinstance(book, dict) else None) or []
            raw_asks = getattr(book, "asks", None) or (book.get("asks") if isinstance(book, dict) else None) or []

            def _parse_levels(raw) -> list[BookLevel]:
                levels = []
                for item in raw:
                    price = getattr(item, "price", None)
                    size = getattr(item, "size", None)
                    if price is None and isinstance(item, dict):
                        price = item.get("price")
                        size = item.get("size")
                    try:
                        levels.append(BookLevel(float(price), float(size)))
                    except (TypeError, ValueError):
                        continue
                return levels

            bids = _parse_levels(raw_bids)
            asks = _parse_levels(raw_asks)
            if not bids or not asks:
                return None
            return analyze_book(bids, asks, fill_size_usd)
        except Exception:
            logger.debug("Failed to analyse book for %s", token_id[:12], exc_info=True)
            return None

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
