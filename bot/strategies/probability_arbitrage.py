"""Strategy 1 — AI-Powered Probability Arbitrage.

Scan high-volume active markets, ask Claude for an independent
probability estimate, and trade when |P_claude - P_market| > min_edge
(after fees).

Flow per tick:
  1. Fetch top-N active markets from Gamma API.
  2. For each, get orderbook midpoint.
  3. Ask ClaudeOracle for a probability estimate.
  4. If edge > threshold: emit a TradeSignal.
  5. RiskManager sizes and approves.
  6. Place order via PolymarketClient.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bot.config import cfg
from bot.core.claude_oracle import ClaudeOracle
from bot.core.polymarket_client import (
    fetch_active_markets,
    get_book,
    place_order,
)
from bot.core.risk_manager import RiskManager
from bot.core.utils import MarketInfo, Side, TradeSignal

logger = logging.getLogger(__name__)


class ProbabilityArbitrage:
    """Scan markets and trade when Claude's estimate diverges from price."""

    name = "prob_arb"

    def __init__(self, oracle: ClaudeOracle, risk: RiskManager) -> None:
        self._oracle = oracle
        self._risk = risk
        self._last_scan = 0.0
        self._trades_session: list[dict] = []

    async def scan_and_trade(self) -> list[dict]:
        """Run one full scan cycle.  Returns list of trade records."""
        now = time.time()
        if now - self._last_scan < cfg.prob_arb_scan_interval_s:
            return []
        self._last_scan = now

        if self._risk.is_halted:
            logger.info("[prob_arb] Risk halted — skipping scan.")
            return []

        logger.info("[prob_arb] Scanning active markets…")
        markets = await fetch_active_markets(
            min_volume=cfg.prob_arb_min_volume,
            limit=cfg.prob_arb_max_markets_per_scan,
        )
        if not markets:
            logger.info("[prob_arb] No markets found.")
            return []

        results: list[dict] = []
        for mkt in markets:
            if self._risk.is_halted:
                break
            try:
                record = await self._evaluate_market(mkt)
                if record:
                    results.append(record)
            except Exception:
                logger.exception("[prob_arb] Error evaluating %s", mkt.condition_id[:12])
            await asyncio.sleep(1)  # rate-limit courtesy

        if results:
            logger.info("[prob_arb] Cycle produced %d trade(s).", len(results))
        return results

    async def _evaluate_market(self, mkt: MarketInfo) -> dict | None:
        if not mkt.token_ids or not mkt.outcome_prices:
            return None

        # Use the YES token (index 0)
        yes_token = mkt.token_ids[0]
        market_price = mkt.outcome_prices[0]

        if market_price <= 0.02 or market_price >= 0.98:
            return None  # near-certain / near-zero — no edge

        book = await get_book(yes_token)
        midpoint = book.midpoint or market_price

        book_summary = (
            f"bid={book.best_bid:.3f} ask={book.best_ask:.3f} "
            f"spread={book.spread_pct:.2%} "
            f"bid_depth=${book.bid_depth_usd:.0f} ask_depth=${book.ask_depth_usd:.0f}"
        )

        estimate = await self._oracle.estimate_probability(
            question=mkt.question,
            description=mkt.description,
            current_price=midpoint,
            volume=mkt.volume,
            liquidity=mkt.liquidity,
            category=mkt.category,
            end_date=mkt.end_date,
            book_summary=book_summary,
        )
        if estimate is None:
            return None

        raw_edge = estimate.probability - midpoint
        abs_edge = abs(raw_edge)
        net_edge = abs_edge - cfg.prob_arb_fee_pct

        logger.info(
            "[prob_arb] %s | P_claude=%.3f P_market=%.3f edge=%.3f net=%.3f conf=%.2f",
            mkt.question[:60], estimate.probability, midpoint,
            raw_edge, net_edge, estimate.confidence,
        )

        if net_edge < cfg.prob_arb_min_edge_pct:
            return None
        if estimate.confidence < cfg.prob_arb_min_confidence:
            logger.debug("[prob_arb] Confidence %.2f below threshold.", estimate.confidence)
            return None

        # Direction: if Claude thinks higher → BUY YES; lower → SELL YES (= BUY NO)
        if raw_edge > 0:
            side = Side.BUY
            token_id = yes_token
            price = book.best_ask if book.best_ask > 0 else midpoint
        else:
            side = Side.SELL
            token_id = yes_token
            price = book.best_bid if book.best_bid > 0 else midpoint

        signal = TradeSignal(
            strategy=self.name,
            token_id=token_id,
            condition_id=mkt.condition_id,
            side=side,
            price=price,
            edge=net_edge,
            confidence=estimate.confidence,
            reason=estimate.reasoning,
            features={
                "p_claude": estimate.probability,
                "p_market": midpoint,
                "raw_edge": raw_edge,
                "net_edge": net_edge,
                "edge_direction": estimate.edge_direction,
                "key_factors": estimate.key_factors,
            },
        )

        verdict = self._risk.approve(signal)
        if not verdict.approved:
            logger.debug("[prob_arb] Risk rejected: %s", verdict.reason)
            return None

        size_shares = verdict.adjusted_size_usd / price if price > 0 else 0
        if size_shares <= 0:
            return None

        result = await place_order(token_id, side, price, size_shares)

        record = {
            "strategy": self.name,
            "timestamp": time.time(),
            "question": mkt.question[:80],
            "token_id": token_id[:16],
            "side": side.value,
            "price": price,
            "size_shares": round(size_shares, 2),
            "size_usd": round(verdict.adjusted_size_usd, 2),
            "edge": round(net_edge, 4),
            "confidence": round(estimate.confidence, 3),
            "p_claude": round(estimate.probability, 4),
            "p_market": round(midpoint, 4),
            "order_id": result.order_id,
            "success": result.success,
            "mode": result.mode,
        }

        if result.success:
            self._risk.open_position(signal, result.fill_price, result.filled_size)
            self._trades_session.append(record)

        return record
