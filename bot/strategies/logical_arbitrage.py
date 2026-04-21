"""Strategy 2 — Correlation & Logical Arbitrage.

Build a dynamic graph of related markets (same event group / neg-risk
set), use Claude to detect logical violations ("If A is 78%, B cannot
be 35%"), and trade both legs simultaneously when the discrepancy
exceeds a threshold.

Near risk-free when both legs fill, because the constraint is
structural — it doesn't depend on a probability estimate.

Flow per tick:
  1. Fetch active markets, group by event / neg-risk cluster.
  2. For each group with >= 2 markets, ask ClaudeOracle for relations.
  3. Score violations.
  4. Execute simultaneous legs (buy underpriced, sell overpriced).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

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


class LogicalArbitrage:
    """Detect and exploit logical price violations across related markets."""

    name = "logical_arb"

    def __init__(self, oracle: ClaudeOracle, risk: RiskManager) -> None:
        self._oracle = oracle
        self._risk = risk
        self._last_scan = 0.0
        self._trades_session: list[dict] = []

    async def scan_and_trade(self) -> list[dict]:
        now = time.time()
        if now - self._last_scan < cfg.logical_arb_scan_interval_s:
            return []
        self._last_scan = now

        if self._risk.is_halted:
            logger.info("[logical_arb] Risk halted — skipping.")
            return []

        logger.info("[logical_arb] Scanning for logical violations…")

        markets = await fetch_active_markets(
            min_volume=cfg.prob_arb_min_volume,
            limit=60,
        )
        if len(markets) < 2:
            return []

        groups = self._cluster_markets(markets)
        logger.info("[logical_arb] Found %d market groups.", len(groups))

        results: list[dict] = []
        for group_key, group_markets in list(groups.items())[: cfg.logical_arb_max_groups]:
            if self._risk.is_halted:
                break
            if len(group_markets) < 2:
                continue
            try:
                trades = await self._analyze_group(group_key, group_markets)
                results.extend(trades)
            except Exception:
                logger.exception("[logical_arb] Error in group %s", group_key[:20])
            await asyncio.sleep(1)

        if results:
            logger.info("[logical_arb] Cycle produced %d leg(s).", len(results))
        return results

    def _cluster_markets(self, markets: list[MarketInfo]) -> dict[str, list[MarketInfo]]:
        """Group markets by neg-risk ID or category similarity."""
        groups: dict[str, list[MarketInfo]] = defaultdict(list)

        for mkt in markets:
            if mkt.neg_risk and mkt.neg_risk_market_id:
                groups[f"neg_{mkt.neg_risk_market_id}"].append(mkt)
            elif mkt.category:
                groups[f"cat_{mkt.category.lower()}"].append(mkt)

        # Also look for "sums to one" among neg-risk groups
        return dict(groups)

    async def _analyze_group(
        self,
        group_key: str,
        markets: list[MarketInfo],
    ) -> list[dict]:
        """Analyze a group of related markets for violations."""
        results: list[dict] = []

        # 1. Structural check: neg-risk outcomes should sum ≈ 1
        if group_key.startswith("neg_"):
            violations = self._check_sum_to_one(markets)
            for v in violations:
                trades = await self._execute_sum_violation(v, markets)
                results.extend(trades)
            return results

        # 2. Claude-assisted logical relation detection
        market_dicts = [
            {
                "condition_id": m.condition_id,
                "question": m.question,
                "outcomes": m.outcomes,
                "outcome_prices": m.outcome_prices,
            }
            for m in markets
            if m.outcome_prices
        ]

        if len(market_dicts) < 2:
            return results

        # Limit to 8 markets per Claude call to control cost
        market_dicts = market_dicts[:8]
        relations = await self._oracle.detect_logical_relations(market_dicts)

        for rel in relations:
            if rel.constraint_violation < cfg.logical_arb_min_edge_pct:
                continue
            if rel.confidence < 0.6:
                continue

            logger.info(
                "[logical_arb] Violation: %s ↔ %s | type=%s | violation=%.3f | conf=%.2f | %s",
                rel.market_a_id[:12], rel.market_b_id[:12],
                rel.relation, rel.constraint_violation, rel.confidence,
                rel.explanation,
            )

            trades = await self._execute_relation_violation(rel, markets)
            results.extend(trades)

        return results

    def _check_sum_to_one(self, markets: list[MarketInfo]) -> list[dict]:
        """Check if YES prices in a neg-risk group sum to ~1.0."""
        total = 0.0
        valid = []
        for m in markets:
            if m.outcome_prices:
                yes_price = m.outcome_prices[0]
                total += yes_price
                valid.append({"market": m, "yes_price": yes_price})

        if len(valid) < 2:
            return []

        deviation = total - 1.0

        if abs(deviation) < cfg.logical_arb_min_edge_pct:
            return []

        logger.info(
            "[logical_arb] Sum-to-one violation: %d markets sum to %.4f (deviation %.4f)",
            len(valid), total, deviation,
        )

        return [{"type": "sum_to_one", "deviation": deviation, "markets": valid}]

    async def _execute_sum_violation(
        self,
        violation: dict,
        all_markets: list[MarketInfo],
    ) -> list[dict]:
        """Trade a sum-to-one violation: buy the cheapest, sell the most expensive."""
        markets_data = violation["markets"]
        deviation = violation["deviation"]
        results: list[dict] = []

        if deviation > 0:
            # Overpriced total → sell the most expensive outcome
            markets_data.sort(key=lambda x: x["yes_price"], reverse=True)
            target = markets_data[0]
            mkt = target["market"]
            if not mkt.token_ids:
                return results
            token_id = mkt.token_ids[0]
            side = Side.SELL
            price = target["yes_price"]
        else:
            # Underpriced total → buy the cheapest outcome
            markets_data.sort(key=lambda x: x["yes_price"])
            target = markets_data[0]
            mkt = target["market"]
            if not mkt.token_ids:
                return results
            token_id = mkt.token_ids[0]
            side = Side.BUY
            price = target["yes_price"]

        book = await get_book(token_id)
        exec_price = (
            book.best_ask if side == Side.BUY and book.best_ask > 0
            else book.best_bid if side == Side.SELL and book.best_bid > 0
            else price
        )

        signal = TradeSignal(
            strategy=self.name,
            token_id=token_id,
            condition_id=mkt.condition_id,
            side=side,
            price=exec_price,
            edge=abs(deviation),
            confidence=0.85,
            reason=f"Sum-to-one violation: {deviation:+.4f}",
            features={"violation_type": "sum_to_one", "deviation": deviation},
        )

        verdict = self._risk.approve(signal)
        if not verdict.approved:
            return results

        size_shares = verdict.adjusted_size_usd / exec_price if exec_price > 0 else 0
        if size_shares <= 0:
            return results

        result = await place_order(token_id, side, exec_price, size_shares)

        record = {
            "strategy": self.name,
            "timestamp": time.time(),
            "question": mkt.question[:60],
            "token_id": token_id[:16],
            "side": side.value,
            "price": round(exec_price, 4),
            "size_usd": round(verdict.adjusted_size_usd, 2),
            "edge": round(abs(deviation), 4),
            "violation": "sum_to_one",
            "order_id": result.order_id,
            "success": result.success,
            "mode": result.mode,
        }

        if result.success:
            self._risk.open_position(signal, result.fill_price, result.filled_size)
            self._trades_session.append(record)

        results.append(record)
        return results

    async def _execute_relation_violation(
        self,
        relation,
        all_markets: list[MarketInfo],
    ) -> list[dict]:
        """Trade a logical relation violation detected by Claude."""
        results: list[dict] = []

        mkt_a = next((m for m in all_markets if m.condition_id == relation.market_a_id), None)
        mkt_b = next((m for m in all_markets if m.condition_id == relation.market_b_id), None)
        if not mkt_a or not mkt_b:
            return results
        if not mkt_a.token_ids or not mkt_b.token_ids:
            return results

        # Determine which leg is mispriced based on relation type
        price_a = mkt_a.outcome_prices[0] if mkt_a.outcome_prices else 0
        price_b = mkt_b.outcome_prices[0] if mkt_b.outcome_prices else 0

        legs: list[tuple[MarketInfo, Side, float]] = []

        if relation.relation == "implies":
            # A → B: P(A) <= P(B).  If P(A) > P(B), sell A and buy B.
            if price_a > price_b:
                legs.append((mkt_a, Side.SELL, price_a))
                legs.append((mkt_b, Side.BUY, price_b))
        elif relation.relation == "excludes":
            # A ⊥ B: P(A) + P(B) <= 1.  If sum > 1, sell both.
            if price_a + price_b > 1.0 + cfg.logical_arb_min_edge_pct:
                legs.append((mkt_a, Side.SELL, price_a))
                legs.append((mkt_b, Side.SELL, price_b))
        elif relation.relation == "complements":
            # A + B ≈ 1.  If sum != 1, buy the cheap one.
            if price_a + price_b < 1.0 - cfg.logical_arb_min_edge_pct:
                cheaper = mkt_a if price_a < price_b else mkt_b
                legs.append((cheaper, Side.BUY, min(price_a, price_b)))

        for mkt, side, price in legs:
            token_id = mkt.token_ids[0]
            book = await get_book(token_id)
            exec_price = (
                book.best_ask if side == Side.BUY and book.best_ask > 0
                else book.best_bid if side == Side.SELL and book.best_bid > 0
                else price
            )

            signal = TradeSignal(
                strategy=self.name,
                token_id=token_id,
                condition_id=mkt.condition_id,
                side=side,
                price=exec_price,
                edge=relation.constraint_violation,
                confidence=relation.confidence,
                reason=f"{relation.relation}: {relation.explanation}",
                features={
                    "violation_type": relation.relation,
                    "violation_amount": relation.constraint_violation,
                },
            )

            verdict = self._risk.approve(signal)
            if not verdict.approved:
                continue

            size_shares = verdict.adjusted_size_usd / exec_price if exec_price > 0 else 0
            if size_shares <= 0:
                continue

            result = await place_order(token_id, side, exec_price, size_shares)

            record = {
                "strategy": self.name,
                "timestamp": time.time(),
                "question": mkt.question[:60],
                "token_id": token_id[:16],
                "side": side.value,
                "price": round(exec_price, 4),
                "size_usd": round(verdict.adjusted_size_usd, 2),
                "edge": round(relation.constraint_violation, 4),
                "violation": relation.relation,
                "order_id": result.order_id,
                "success": result.success,
                "mode": result.mode,
            }

            if result.success:
                self._risk.open_position(signal, result.fill_price, result.filled_size)
                self._trades_session.append(record)

            results.append(record)

        return results
