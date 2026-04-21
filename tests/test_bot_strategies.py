"""Integration tests for bot strategies with oracle + client mocked.

These tests verify the end-to-end flow through a strategy cycle without
making real API calls, using dummy ClaudeOracle and patched polymarket
client functions.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from bot.core.claude_oracle import LogicalRelation, OracleEstimate
from bot.core.risk_manager import RiskManager
from bot.core.utils import BookSnapshot, MarketInfo, OrderResult


def _make_market(
    cid="0xcond1",
    token_id="tok_yes",
    price_yes=0.40,
    volume=15000,
    neg_risk=False,
    neg_risk_id="",
):
    return MarketInfo(
        condition_id=cid,
        question=f"Market question for {cid}",
        description="Detailed description",
        category="Crypto",
        end_date="2026-12-31",
        active=True,
        volume=volume,
        liquidity=5000,
        outcomes=["Yes", "No"],
        outcome_prices=[price_yes, 1 - price_yes],
        token_ids=[token_id, token_id + "_no"],
        tags=["test"],
        neg_risk=neg_risk,
        neg_risk_market_id=neg_risk_id,
    )


def _make_book(bid=0.40, ask=0.42, bid_d=100, ask_d=120):
    return BookSnapshot(
        token_id="tok_yes",
        best_bid=bid,
        best_ask=ask,
        bid_depth_usd=bid_d,
        ask_depth_usd=ask_d,
    )


class _StubOracle:
    def __init__(self, estimate=None, relations=None, vol=1.0):
        self._estimate = estimate
        self._relations = relations or []
        self._vol = vol

    async def estimate_probability(self, *args, **kwargs):
        return self._estimate

    async def detect_logical_relations(self, markets):
        return self._relations

    async def assess_volatility(self, *args, **kwargs):
        return self._vol


# ---------------------------------------------------------------------------
# Probability arbitrage
# ---------------------------------------------------------------------------


def test_prob_arb_trades_on_big_edge():
    """Claude estimate >> market price → BUY signal, paper fill."""
    from bot.strategies.probability_arbitrage import ProbabilityArbitrage

    market = _make_market(price_yes=0.40)
    est = OracleEstimate(
        probability=0.65, confidence=0.80,
        reasoning="strong fundamentals", key_factors=["a", "b"],
        edge_direction="UNDER",
    )
    oracle = _StubOracle(estimate=est)
    risk = RiskManager()
    strat = ProbabilityArbitrage(oracle, risk)

    with patch("bot.strategies.probability_arbitrage.fetch_active_markets",
               AsyncMock(return_value=[market])), \
         patch("bot.strategies.probability_arbitrage.get_book",
               AsyncMock(return_value=_make_book())), \
         patch("bot.strategies.probability_arbitrage.place_order",
               AsyncMock(return_value=OrderResult(
                   success=True, order_id="paper-1",
                   filled_size=10, fill_price=0.42, mode="paper"))):
        trades = asyncio.run(strat.scan_and_trade())

    assert len(trades) == 1
    t = trades[0]
    assert t["success"] is True
    assert t["side"] == "BUY"
    assert t["p_claude"] == 0.65
    assert t["strategy"] == "prob_arb"
    assert t["mode"] == "paper"


def test_prob_arb_skips_when_edge_too_small():
    from bot.strategies.probability_arbitrage import ProbabilityArbitrage

    market = _make_market(price_yes=0.50)
    est = OracleEstimate(
        probability=0.52, confidence=0.80,
        reasoning="near-fair", key_factors=[],
        edge_direction="FAIR",
    )
    oracle = _StubOracle(estimate=est)
    risk = RiskManager()
    strat = ProbabilityArbitrage(oracle, risk)

    with patch("bot.strategies.probability_arbitrage.fetch_active_markets",
               AsyncMock(return_value=[market])), \
         patch("bot.strategies.probability_arbitrage.get_book",
               AsyncMock(return_value=_make_book(bid=0.495, ask=0.505))), \
         patch("bot.strategies.probability_arbitrage.place_order",
               AsyncMock(return_value=OrderResult(success=True))):
        trades = asyncio.run(strat.scan_and_trade())

    assert trades == []


def test_prob_arb_skips_low_confidence():
    from bot.strategies.probability_arbitrage import ProbabilityArbitrage

    market = _make_market(price_yes=0.40)
    est = OracleEstimate(
        probability=0.70, confidence=0.30,  # below 0.6 default
        reasoning="uncertain", key_factors=[],
        edge_direction="UNDER",
    )
    oracle = _StubOracle(estimate=est)
    risk = RiskManager()
    strat = ProbabilityArbitrage(oracle, risk)

    with patch("bot.strategies.probability_arbitrage.fetch_active_markets",
               AsyncMock(return_value=[market])), \
         patch("bot.strategies.probability_arbitrage.get_book",
               AsyncMock(return_value=_make_book())):
        trades = asyncio.run(strat.scan_and_trade())

    assert trades == []


def test_prob_arb_stops_when_risk_halted():
    from bot.strategies.probability_arbitrage import ProbabilityArbitrage

    oracle = _StubOracle()
    risk = RiskManager()
    risk.record_pnl(-200)  # trigger circuit breaker
    strat = ProbabilityArbitrage(oracle, risk)

    trades = asyncio.run(strat.scan_and_trade())
    assert trades == []


def test_prob_arb_skips_extreme_prices():
    """Markets near 0 or 1 yield no opportunities."""
    from bot.strategies.probability_arbitrage import ProbabilityArbitrage

    market = _make_market(price_yes=0.99)
    oracle = _StubOracle()
    risk = RiskManager()
    strat = ProbabilityArbitrage(oracle, risk)

    with patch("bot.strategies.probability_arbitrage.fetch_active_markets",
               AsyncMock(return_value=[market])), \
         patch("bot.strategies.probability_arbitrage.get_book",
               AsyncMock(return_value=_make_book(bid=0.98, ask=0.99))):
        trades = asyncio.run(strat.scan_and_trade())

    assert trades == []


def test_prob_arb_handles_oracle_none():
    """Oracle returning None (parse fail, rate limit) is handled gracefully."""
    from bot.strategies.probability_arbitrage import ProbabilityArbitrage

    market = _make_market()
    oracle = _StubOracle(estimate=None)
    risk = RiskManager()
    strat = ProbabilityArbitrage(oracle, risk)

    with patch("bot.strategies.probability_arbitrage.fetch_active_markets",
               AsyncMock(return_value=[market])), \
         patch("bot.strategies.probability_arbitrage.get_book",
               AsyncMock(return_value=_make_book())):
        trades = asyncio.run(strat.scan_and_trade())

    assert trades == []


# ---------------------------------------------------------------------------
# Logical arbitrage
# ---------------------------------------------------------------------------


def test_logical_arb_detects_sum_to_one_violation():
    """Two neg-risk markets whose YES prices sum to 1.15 should trigger."""
    from bot.strategies.logical_arbitrage import LogicalArbitrage

    m1 = _make_market(cid="0xA", token_id="tok_a", price_yes=0.60,
                      neg_risk=True, neg_risk_id="neg1")
    m2 = _make_market(cid="0xB", token_id="tok_b", price_yes=0.55,
                      neg_risk=True, neg_risk_id="neg1")
    oracle = _StubOracle()
    risk = RiskManager()
    strat = LogicalArbitrage(oracle, risk)

    with patch("bot.strategies.logical_arbitrage.fetch_active_markets",
               AsyncMock(return_value=[m1, m2])), \
         patch("bot.strategies.logical_arbitrage.get_book",
               AsyncMock(return_value=_make_book(bid=0.59, ask=0.61))), \
         patch("bot.strategies.logical_arbitrage.place_order",
               AsyncMock(return_value=OrderResult(
                   success=True, order_id="paper-1",
                   filled_size=10, fill_price=0.60, mode="paper"))):
        trades = asyncio.run(strat.scan_and_trade())

    assert len(trades) >= 1
    # Should sell the most expensive
    assert any(t.get("violation") == "sum_to_one" for t in trades)


def test_logical_arb_skips_consistent_prices():
    """Neg-risk markets summing to 1.00 should not trigger."""
    from bot.strategies.logical_arbitrage import LogicalArbitrage

    m1 = _make_market(cid="0xA", price_yes=0.40, neg_risk=True, neg_risk_id="neg1")
    m2 = _make_market(cid="0xB", price_yes=0.60, neg_risk=True, neg_risk_id="neg1")
    oracle = _StubOracle()
    risk = RiskManager()
    strat = LogicalArbitrage(oracle, risk)

    with patch("bot.strategies.logical_arbitrage.fetch_active_markets",
               AsyncMock(return_value=[m1, m2])), \
         patch("bot.strategies.logical_arbitrage.get_book",
               AsyncMock(return_value=_make_book())), \
         patch("bot.strategies.logical_arbitrage.place_order",
               AsyncMock(return_value=OrderResult(success=True))):
        trades = asyncio.run(strat.scan_and_trade())

    assert trades == []


def test_logical_arb_respects_risk_halt():
    from bot.strategies.logical_arbitrage import LogicalArbitrage

    oracle = _StubOracle()
    risk = RiskManager()
    risk.record_pnl(-1000)
    strat = LogicalArbitrage(oracle, risk)
    trades = asyncio.run(strat.scan_and_trade())
    assert trades == []


# ---------------------------------------------------------------------------
# End-to-end pipeline smoke test
# ---------------------------------------------------------------------------


def test_e2e_paper_cycle_records_position():
    """One full paper cycle should open a position and record it."""
    from bot.strategies.probability_arbitrage import ProbabilityArbitrage

    market = _make_market(price_yes=0.35)
    est = OracleEstimate(
        probability=0.70, confidence=0.85,
        reasoning="strong edge", key_factors=["factor1"],
        edge_direction="UNDER",
    )
    oracle = _StubOracle(estimate=est)
    risk = RiskManager()
    strat = ProbabilityArbitrage(oracle, risk)

    assert risk.position_count == 0

    with patch("bot.strategies.probability_arbitrage.fetch_active_markets",
               AsyncMock(return_value=[market])), \
         patch("bot.strategies.probability_arbitrage.get_book",
               AsyncMock(return_value=_make_book(bid=0.35, ask=0.37))), \
         patch("bot.strategies.probability_arbitrage.place_order",
               AsyncMock(return_value=OrderResult(
                   success=True, order_id="paper-1",
                   filled_size=50, fill_price=0.37, mode="paper"))):
        trades = asyncio.run(strat.scan_and_trade())

    assert len(trades) == 1
    assert risk.position_count == 1
    assert "tok_yes" in risk.positions
