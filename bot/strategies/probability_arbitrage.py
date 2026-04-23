"""Strategy 1 — AI-Powered Probability Arbitrage.

Scan high-volume active markets, ask Claude for an independent
probability estimate, and trade when |P_claude - P_market| > min_edge
(after fees).

Competitive edge: enriches Claude with real-time news + category-specific
data (crypto prices, polling data) so it has information the market
hasn't priced in yet.  Evaluates markets in parallel for speed.

Flow per tick:
  1. Fetch top-N active markets from Gamma API.
  2. Pre-fetch all orderbooks in parallel.
  3. For each (parallel, bounded): fetch news + data, ask Claude.
  4. If edge > threshold: emit a TradeSignal.
  5. RiskManager sizes and approves.
  6. Place order via PolymarketClient.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bot.config import cfg
from bot.core import calibration
from bot.core.claude_oracle import ClaudeOracle
from bot.core.polymarket_client import (
    fetch_active_markets,
    get_book,
    place_order,
)
from bot.core.risk_manager import RiskManager
from bot.core.utils import BookSnapshot, LatencyTracker, MarketInfo, Side, TradeSignal

logger = logging.getLogger(__name__)


class ProbabilityArbitrage:
    """Scan markets and trade when Claude's estimate diverges from price."""

    name = "prob_arb"

    def __init__(
        self,
        oracle: ClaudeOracle,
        risk: RiskManager,
        news_fetcher=None,
        data_router=None,
    ) -> None:
        self._oracle = oracle
        self._risk = risk
        self._last_scan = 0.0
        self._trades_session: list[dict] = []
        self._latency_tracker = LatencyTracker(window=200)
        self._min_edge_override: float | None = None
        self._news_fetcher = news_fetcher
        self._data_router = data_router

    @property
    def latency_tracker(self) -> LatencyTracker:
        return self._latency_tracker

    async def scan_and_trade(self) -> list[dict]:
        """Run one full scan cycle.  Returns list of trade records."""
        now = time.time()
        if now - self._last_scan < cfg.prob_arb_scan_interval_s:
            return []
        self._last_scan = now

        if self._risk.is_halted:
            logger.info("[prob_arb] Risk halted — skipping scan.")
            return []

        # Per-strategy circuit breaker
        if self._risk.is_strategy_paused(self.name):
            logger.info("[prob_arb] Strategy paused (daily loss limit) — skipping.")
            return []

        logger.info("[prob_arb] Scanning active markets…")
        markets = await fetch_active_markets(
            min_volume=cfg.prob_arb_min_volume,
            limit=cfg.prob_arb_max_markets_per_scan,
        )
        if not markets:
            logger.info("[prob_arb] No markets found.")
            return []

        # Regime detection: adjust edge threshold based on market conditions
        try:
            from bot.core.regime_detector import detect_regime
            sample_prices = [
                m.outcome_prices[0] for m in markets
                if m.outcome_prices
            ][:20]
            if len(sample_prices) >= 5:
                regime = detect_regime(sample_prices)
                adjusted_edge = cfg.prob_arb_min_edge_pct * regime.edge_multiplier
                self._min_edge_override = adjusted_edge
                logger.info(
                    "[prob_arb] Regime=%s edge_mult=%.2f min_edge=%.4f",
                    regime.regime.value, regime.edge_multiplier, adjusted_edge,
                )
            else:
                self._min_edge_override = None
        except Exception:
            self._min_edge_override = None

        # Pre-fetch all orderbooks in parallel for speed
        eligible = [m for m in markets if m.token_ids and m.outcome_prices]
        book_tasks = [get_book(m.token_ids[0]) for m in eligible]
        book_results = await asyncio.gather(*book_tasks, return_exceptions=True)

        books: dict[str, BookSnapshot] = {}
        for mkt, book_or_exc in zip(eligible, book_results):
            if isinstance(book_or_exc, BaseException):
                continue
            books[mkt.token_ids[0]] = book_or_exc

        # Evaluate markets in parallel (bounded by semaphore)
        sem = asyncio.Semaphore(cfg.speed_parallel_evaluations)

        async def _bounded_eval(mkt: MarketInfo) -> dict | None:
            if self._risk.is_halted:
                return None
            async with sem:
                try:
                    token_id = mkt.token_ids[0] if mkt.token_ids else ""
                    book = books.get(token_id)
                    return await self._evaluate_market(mkt, book)
                except Exception:
                    logger.exception("[prob_arb] Error evaluating %s", mkt.condition_id[:12])
                    return None

        eval_results = await asyncio.gather(
            *[_bounded_eval(m) for m in eligible],
            return_exceptions=True,
        )

        results = [r for r in eval_results if isinstance(r, dict)]

        if results:
            logger.info("[prob_arb] Cycle produced %d trade(s).", len(results))
        return results

    async def _evaluate_market(
        self,
        mkt: MarketInfo,
        pre_fetched_book: BookSnapshot | None = None,
    ) -> dict | None:
        if not mkt.token_ids or not mkt.outcome_prices:
            return None

        yes_token = mkt.token_ids[0]
        market_price = mkt.outcome_prices[0]

        if market_price <= 0.02 or market_price >= 0.98:
            return None

        book = pre_fetched_book or await get_book(yes_token)
        midpoint = book.midpoint or market_price

        book_summary = (
            f"bid={book.best_bid:.3f} ask={book.best_ask:.3f} "
            f"spread={book.spread_pct:.2%} "
            f"bid_depth=${book.bid_depth_usd:.0f} ask_depth=${book.ask_depth_usd:.0f}"
        )

        # Fetch real-time news and data for competitive edge
        news_context = ""
        data_context = ""

        if self._news_fetcher is not None:
            try:
                items = await self._news_fetcher.fetch_relevant_news(
                    mkt.question, mkt.category, max_items=cfg.news_max_items,
                )
                news_context = self._news_fetcher.format_for_oracle(items)
            except Exception:
                logger.debug("[prob_arb] News fetch failed for %s", mkt.question[:40])

        if self._data_router is not None:
            try:
                data_context = await self._data_router.enrich(
                    mkt.question, mkt.description, mkt.category, mkt.tags,
                )
            except Exception:
                logger.debug("[prob_arb] Data feed failed for %s", mkt.question[:40])

        t_signal = time.time()

        estimate = await self._oracle.estimate_probability(
            question=mkt.question,
            description=mkt.description,
            current_price=midpoint,
            volume=mkt.volume,
            liquidity=mkt.liquidity,
            category=mkt.category,
            end_date=mkt.end_date,
            book_summary=book_summary,
            news_context=news_context,
            data_context=data_context,
        )
        if estimate is None:
            return None

        raw_edge = estimate.probability - midpoint
        abs_edge = abs(raw_edge)
        net_edge = abs_edge - cfg.prob_arb_fee_pct

        logger.info(
            "[prob_arb] %s | P_claude=%.3f P_market=%.3f edge=%.3f net=%.3f conf=%.2f%s",
            mkt.question[:60], estimate.probability, midpoint,
            raw_edge, net_edge, estimate.confidence,
            " [+news]" if news_context else "",
        )

        calibration.record_estimate(
            strategy=self.name,
            condition_id=mkt.condition_id,
            token_id=yes_token,
            question=mkt.question,
            p_claude=estimate.probability,
            p_market=midpoint,
            confidence=estimate.confidence,
            edge=raw_edge,
            edge_direction=estimate.edge_direction,
            reasoning=estimate.reasoning,
        )

        min_edge = self._min_edge_override if self._min_edge_override is not None else cfg.prob_arb_min_edge_pct

        if net_edge < min_edge:
            return None
        if estimate.confidence < cfg.prob_arb_min_confidence:
            logger.debug("[prob_arb] Confidence %.2f below threshold.", estimate.confidence)
            return None

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
            probability=estimate.probability,
            reason=estimate.reasoning,
            category=mkt.category,
            features={
                "p_claude": estimate.probability,
                "p_market": midpoint,
                "raw_edge": raw_edge,
                "net_edge": net_edge,
                "edge_direction": estimate.edge_direction,
                "key_factors": estimate.key_factors,
                "has_news": bool(news_context),
                "has_data": bool(data_context),
            },
        )

        verdict = self._risk.approve(signal)
        if not verdict.approved:
            logger.debug("[prob_arb] Risk rejected: %s", verdict.reason)
            return None

        size_shares = verdict.adjusted_size_usd / price if price > 0 else 0
        if size_shares <= 0:
            return None

        # Pass book for depth-aware slippage
        result = await place_order(token_id, side, price, size_shares, book=book)

        t_fill = time.time()
        latency_s = t_fill - t_signal
        self._latency_tracker.record(latency_s)

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
            "signal_to_fill_ms": round(latency_s * 1000, 1),
            "has_news": bool(news_context),
            "has_data": bool(data_context),
        }

        if result.success:
            self._risk.open_position(signal, result.fill_price, result.filled_size)
            self._trades_session.append(record)

        return record
