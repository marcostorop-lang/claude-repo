"""Strategy 3 — Automated Market Making.

Provide continuous two-sided liquidity on selected markets.
Dynamic spread based on volatility (Claude-assessed) + inventory skew.
Inventory rebalancing when net exposure exceeds threshold.

Flow per tick:
  1. Select top-N liquid markets.
  2. For each, assess volatility via Claude (cached / infrequent).
  3. Compute dynamic spread = base_spread × volatility_mult × inventory_skew.
  4. Cancel stale quotes, post new bid+ask.
  5. If inventory > threshold, widen spread on the heavy side.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bot.config import cfg
from bot.core.claude_oracle import ClaudeOracle
from bot.core.polymarket_client import (
    cancel_order,
    fetch_active_markets,
    get_book,
    place_order,
)
from bot.core.risk_manager import RiskManager
from bot.core.utils import MarketInfo, Side, TradeSignal

logger = logging.getLogger(__name__)


class MarketMaking:
    """Two-sided liquidity provision with inventory management."""

    name = "market_making"

    def __init__(self, oracle: ClaudeOracle, risk: RiskManager) -> None:
        self._oracle = oracle
        self._risk = risk
        self._last_scan = 0.0
        self._trades_session: list[dict] = []

        # Cached volatility assessments: token_id → (mult, timestamp)
        self._vol_cache: dict[str, tuple[float, float]] = {}
        _VOL_CACHE_TTL = 600  # 10 minutes

        # Active quotes: token_id → {"bid_id": ..., "ask_id": ...}
        self._active_quotes: dict[str, dict[str, str]] = {}

        # Inventory tracking: token_id → net shares (positive = long)
        self._inventory: dict[str, float] = {}

    async def scan_and_trade(self) -> list[dict]:
        now = time.time()
        if now - self._last_scan < cfg.mm_scan_interval_s:
            return []
        self._last_scan = now

        if self._risk.is_halted:
            logger.info("[mm] Risk halted — cancelling all quotes.")
            await self._cancel_all_quotes()
            return []

        logger.info("[mm] Market-making cycle…")

        markets = await fetch_active_markets(
            min_volume=cfg.prob_arb_min_volume * 2,  # need higher liquidity
            limit=cfg.mm_max_markets * 3,
        )

        # Select the most liquid markets
        markets.sort(key=lambda m: m.liquidity, reverse=True)
        selected = markets[: cfg.mm_max_markets]

        if not selected:
            logger.info("[mm] No suitable markets found.")
            return []

        results: list[dict] = []
        active_tokens = set()

        for mkt in selected:
            if self._risk.is_halted:
                break
            if not mkt.token_ids:
                continue

            token_id = mkt.token_ids[0]  # YES token
            active_tokens.add(token_id)

            try:
                trades = await self._quote_market(mkt, token_id)
                results.extend(trades)
            except Exception:
                logger.exception("[mm] Error quoting %s", mkt.question[:40])
            await asyncio.sleep(0.5)

        # Cancel quotes on markets we're no longer tracking
        for tok in list(self._active_quotes.keys()):
            if tok not in active_tokens:
                await self._cancel_quotes_for(tok)

        return results

    async def _quote_market(
        self,
        mkt: MarketInfo,
        token_id: str,
    ) -> list[dict]:
        """Post or refresh two-sided quotes for one market."""
        results: list[dict] = []

        book = await get_book(token_id)
        mid = book.midpoint
        if mid <= 0.02 or mid >= 0.98:
            await self._cancel_quotes_for(token_id)
            return results

        # Get volatility multiplier (cached)
        vol_mult = await self._get_volatility(mkt)

        # Inventory skew
        net_inv = self._inventory.get(token_id, 0.0)
        inv_ratio = 0.0
        if cfg.mm_order_size_usd > 0 and mid > 0:
            total_capacity = cfg.mm_order_size_usd * 5 / mid
            inv_ratio = net_inv / total_capacity if total_capacity > 0 else 0.0

        # Dynamic spread
        base_spread = cfg.mm_base_spread_pct
        spread = base_spread * vol_mult

        # Widen spread on the heavy side, narrow on the light side
        bid_spread = spread * (1.0 + max(inv_ratio, 0) * 0.5)
        ask_spread = spread * (1.0 + max(-inv_ratio, 0) * 0.5)

        # Clamp spread
        bid_spread = min(bid_spread, cfg.mm_max_spread_pct)
        ask_spread = min(ask_spread, cfg.mm_max_spread_pct)

        bid_price = max(0.01, mid - bid_spread / 2)
        ask_price = min(0.99, mid + ask_spread / 2)

        # Don't cross the book
        if book.best_bid > 0 and bid_price > book.best_bid:
            bid_price = book.best_bid
        if book.best_ask > 0 and ask_price < book.best_ask:
            ask_price = book.best_ask

        if bid_price >= ask_price:
            return results

        size_shares = cfg.mm_order_size_usd / mid if mid > 0 else 0
        if size_shares < 1:
            return results

        # Check inventory limit — pause the heavy side
        skip_bid = inv_ratio > cfg.mm_max_inventory_pct
        skip_ask = inv_ratio < -cfg.mm_max_inventory_pct

        # Cancel existing quotes before posting new ones
        await self._cancel_quotes_for(token_id)

        new_quotes: dict[str, str] = {}

        if not skip_bid:
            bid_result = await place_order(token_id, Side.BUY, bid_price, size_shares)
            if bid_result.success:
                new_quotes["bid_id"] = bid_result.order_id
                if bid_result.filled_size > 0:
                    self._inventory[token_id] = net_inv + bid_result.filled_size
                    results.append(self._make_record(
                        mkt, token_id, Side.BUY, bid_price, bid_result, vol_mult,
                    ))

        if not skip_ask:
            ask_result = await place_order(token_id, Side.SELL, ask_price, size_shares)
            if ask_result.success:
                new_quotes["ask_id"] = ask_result.order_id
                if ask_result.filled_size > 0:
                    self._inventory[token_id] = self._inventory.get(token_id, 0) - ask_result.filled_size
                    results.append(self._make_record(
                        mkt, token_id, Side.SELL, ask_price, ask_result, vol_mult,
                    ))

        if new_quotes:
            self._active_quotes[token_id] = new_quotes

        return results

    async def _get_volatility(self, mkt: MarketInfo) -> float:
        """Get volatility multiplier, with caching."""
        token_id = mkt.token_ids[0] if mkt.token_ids else mkt.condition_id
        cached = self._vol_cache.get(token_id)
        if cached and (time.time() - cached[1]) < 600:
            return cached[0]

        mult = await self._oracle.assess_volatility(mkt.question, mkt.description)
        self._vol_cache[token_id] = (mult, time.time())
        return mult

    async def _cancel_quotes_for(self, token_id: str) -> None:
        quotes = self._active_quotes.pop(token_id, {})
        for key, order_id in quotes.items():
            if order_id:
                await cancel_order(order_id)

    async def _cancel_all_quotes(self) -> None:
        for token_id in list(self._active_quotes.keys()):
            await self._cancel_quotes_for(token_id)

    def _make_record(
        self,
        mkt: MarketInfo,
        token_id: str,
        side: Side,
        price: float,
        result,
        vol_mult: float,
    ) -> dict:
        return {
            "strategy": self.name,
            "timestamp": time.time(),
            "question": mkt.question[:60],
            "token_id": token_id[:16],
            "side": side.value,
            "price": round(price, 4),
            "size_usd": round(cfg.mm_order_size_usd, 2),
            "vol_mult": round(vol_mult, 2),
            "inventory": round(self._inventory.get(token_id, 0), 2),
            "order_id": result.order_id,
            "success": result.success,
            "mode": result.mode,
        }

    def get_inventory_summary(self) -> dict[str, float]:
        return dict(self._inventory)
