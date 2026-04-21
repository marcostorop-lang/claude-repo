"""Polymarket client — wraps py-clob-client + Gamma API.

Handles market discovery (Gamma), orderbook queries, and order
placement (CLOB).  Paper mode simulates fills at midpoint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from bot.config import cfg
from bot.core.utils import BookSnapshot, MarketInfo, OrderResult, Side

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CLOB client (synchronous SDK → wrapped in executor for async)
# ---------------------------------------------------------------------------

_clob_client = None


def _get_clob():
    """Lazily build the py-clob-client ClobClient singleton."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    if not cfg.api_key or not cfg.private_key:
        logger.warning("CLOB credentials not set — live orders will fail.")
        return None
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        creds = ApiCreds(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            api_passphrase=cfg.passphrase,
        )
        _clob_client = ClobClient(
            cfg.clob_url,
            key=cfg.private_key,
            chain_id=cfg.chain_id,
            creds=creds,
        )
        logger.info("CLOB client initialised (chain_id=%d).", cfg.chain_id)
        return _clob_client
    except Exception:
        logger.exception("Failed to build CLOB client.")
        return None


# ---------------------------------------------------------------------------
# Gamma API (async via httpx)
# ---------------------------------------------------------------------------


async def fetch_active_markets(
    *,
    min_volume: float = 0.0,
    limit: int = 50,
    category: str = "",
) -> list[MarketInfo]:
    """Fetch active markets from the Gamma API."""
    params: dict[str, Any] = {
        "active": "true",
        "closed": "false",
        "limit": str(min(limit, 100)),
        "order": "volume",
        "ascending": "false",
    }
    if category:
        params["tag"] = category

    url = f"{cfg.gamma_url}/markets"
    results: list[MarketInfo] = []

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        for m in data:
            try:
                vol = float(m.get("volume", 0) or 0)
                if vol < min_volume:
                    continue

                outcomes = m.get("outcomes", "[]")
                if isinstance(outcomes, str):
                    import json
                    outcomes = json.loads(outcomes)

                prices_raw = m.get("outcomePrices", "[]")
                if isinstance(prices_raw, str):
                    import json
                    prices_raw = json.loads(prices_raw)
                prices = [float(p) for p in prices_raw] if prices_raw else []

                tokens = m.get("clobTokenIds", "[]")
                if isinstance(tokens, str):
                    import json
                    tokens = json.loads(tokens)
                if not isinstance(tokens, list):
                    tokens = []

                tags_raw = m.get("tags", [])
                if isinstance(tags_raw, str):
                    import json
                    tags_raw = json.loads(tags_raw)

                results.append(MarketInfo(
                    condition_id=m.get("conditionId", m.get("condition_id", "")),
                    question=m.get("question", ""),
                    description=m.get("description", "")[:500],
                    category=m.get("category", m.get("groupItemTitle", "")),
                    end_date=m.get("endDate", m.get("end_date_iso", "")),
                    active=True,
                    volume=vol,
                    liquidity=float(m.get("liquidity", 0) or 0),
                    outcomes=outcomes,
                    outcome_prices=prices,
                    token_ids=tokens,
                    tags=tags_raw if isinstance(tags_raw, list) else [],
                    neg_risk=bool(m.get("negRisk", False)),
                    neg_risk_market_id=m.get("negRiskMarketId", "") or "",
                ))
            except Exception:
                logger.debug("Skipping malformed market entry.", exc_info=True)

    except httpx.HTTPStatusError as exc:
        logger.error("Gamma API %d: %s", exc.response.status_code, exc.response.text[:200])
    except Exception:
        logger.exception("Gamma API request failed.")

    return results


async def fetch_related_markets(condition_id: str) -> list[MarketInfo]:
    """Fetch markets in the same event group (for logical arb)."""
    url = f"{cfg.gamma_url}/markets"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params={
                "condition_id": condition_id,
                "limit": "50",
            })
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    group_slug = data[0].get("groupSlug", "")
                    if group_slug:
                        resp2 = await client.get(url, params={
                            "slug": group_slug,
                            "limit": "50",
                        })
                        if resp2.status_code == 200:
                            return await _parse_markets(resp2.json())
    except Exception:
        logger.debug("Related markets fetch failed for %s", condition_id[:12], exc_info=True)
    return []


async def _parse_markets(data: list[dict]) -> list[MarketInfo]:
    """Reuse the parsing logic from fetch_active_markets."""
    import json as _json
    results = []
    for m in data:
        try:
            outcomes = m.get("outcomes", "[]")
            if isinstance(outcomes, str):
                outcomes = _json.loads(outcomes)
            prices_raw = m.get("outcomePrices", "[]")
            if isinstance(prices_raw, str):
                prices_raw = _json.loads(prices_raw)
            prices = [float(p) for p in prices_raw] if prices_raw else []
            tokens = m.get("clobTokenIds", "[]")
            if isinstance(tokens, str):
                tokens = _json.loads(tokens)
            results.append(MarketInfo(
                condition_id=m.get("conditionId", ""),
                question=m.get("question", ""),
                description=m.get("description", "")[:500],
                category=m.get("category", ""),
                end_date=m.get("endDate", ""),
                active=bool(m.get("active", True)),
                volume=float(m.get("volume", 0) or 0),
                liquidity=float(m.get("liquidity", 0) or 0),
                outcomes=outcomes if isinstance(outcomes, list) else [],
                outcome_prices=prices,
                token_ids=tokens if isinstance(tokens, list) else [],
                neg_risk=bool(m.get("negRisk", False)),
                neg_risk_market_id=m.get("negRiskMarketId", "") or "",
            ))
        except Exception:
            continue
    return results


# ---------------------------------------------------------------------------
# Orderbook
# ---------------------------------------------------------------------------


async def get_book(token_id: str) -> BookSnapshot:
    """Fetch the top-of-book for a token via the CLOB REST endpoint."""
    snap = BookSnapshot(token_id=token_id)
    try:
        url = f"{cfg.clob_url}/book"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params={"token_id": token_id})
            if resp.status_code != 200:
                return snap
            data = resp.json()

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if bids:
            snap.best_bid = float(bids[0].get("price", 0))
            snap.bid_depth_usd = sum(
                float(b.get("size", 0)) * float(b.get("price", 0))
                for b in bids[:10]
            )
        if asks:
            snap.best_ask = float(asks[0].get("price", 0))
            snap.ask_depth_usd = sum(
                float(a.get("size", 0)) * float(a.get("price", 0))
                for a in asks[:10]
            )
    except Exception:
        logger.debug("Book fetch failed for %s", token_id[:12], exc_info=True)
    return snap


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------


async def place_order(
    token_id: str,
    side: Side,
    price: float,
    size: float,
) -> OrderResult:
    """Place a limit order.  Paper mode simulates an instant fill."""
    if cfg.is_paper:
        return OrderResult(
            success=True,
            order_id=f"paper-{int(time.time()*1000)}",
            filled_size=size,
            fill_price=price,
            message="paper fill",
            mode="paper",
        )

    if not cfg.is_live:
        return OrderResult(success=False, message="Live trading not enabled (two-gate).")

    clob = _get_clob()
    if clob is None:
        return OrderResult(success=False, message="CLOB client not available.")

    try:
        from py_clob_client.order import OrderArgs
        from py_clob_client.clob_types import OrderType

        loop = asyncio.get_running_loop()
        order_args = OrderArgs(
            price=price,
            size=size,
            side=side.value,
            token_id=token_id,
        )
        signed = await loop.run_in_executor(None, clob.create_order, order_args)
        resp = await loop.run_in_executor(None, clob.post_order, signed, OrderType.GTC)

        if resp and resp.get("success"):
            return OrderResult(
                success=True,
                order_id=resp.get("orderID", ""),
                filled_size=size,
                fill_price=price,
                message="live order posted",
                mode="live",
            )
        return OrderResult(
            success=False,
            message=f"CLOB rejected: {resp}",
            mode="live",
        )
    except Exception as exc:
        logger.exception("Order placement failed.")
        return OrderResult(success=False, message=str(exc), mode="live")


async def cancel_order(order_id: str) -> bool:
    """Cancel a resting order.  Paper mode is a no-op success."""
    if cfg.is_paper:
        return True
    clob = _get_clob()
    if clob is None:
        return False
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, clob.cancel, order_id)
        return True
    except Exception:
        logger.exception("Cancel failed for %s", order_id)
        return False


async def cancel_all() -> int:
    """Cancel all resting orders.  Returns count cancelled."""
    if cfg.is_paper:
        return 0
    clob = _get_clob()
    if clob is None:
        return 0
    try:
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, clob.cancel_all)
        cancelled = len(resp) if isinstance(resp, list) else 0
        logger.info("Cancelled %d resting orders.", cancelled)
        return cancelled
    except Exception:
        logger.exception("cancel_all failed.")
        return 0
