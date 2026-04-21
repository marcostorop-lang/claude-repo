"""Simple backtesting engine.

Replays historical price data from the Polymarket Data API and
simulates the probability arbitrage strategy with recorded Claude
estimates (or re-queries Claude for each point).

This is a *stub* — production backtesting needs tick-level book data,
realistic fill simulation, and latency modelling.  This module exists
so the operator can do a quick sanity check before going live.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from bot.config import cfg
from bot.core.utils import kelly_size

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    timestamp: str
    side: str
    price: float
    size_usd: float
    edge: float
    pnl: float = 0.0
    closed: bool = False


@dataclass
class BacktestResult:
    market_question: str = ""
    trades: list[BacktestTrade] = field(default_factory=list)
    total_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    max_drawdown: float = 0.0


async def fetch_price_history(
    token_id: str,
    *,
    interval: str = "1h",
    fidelity: int = 60,
) -> list[dict]:
    """Fetch historical price data from the Polymarket Data API."""
    url = f"https://data-api.polymarket.com/prices"
    params = {
        "market": token_id,
        "interval": interval,
        "fidelity": str(fidelity),
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "history" in data:
                    return data["history"]
    except Exception:
        logger.exception("Failed to fetch price history for %s", token_id[:12])
    return []


async def run_backtest(
    token_id: str,
    condition_id: str,
    question: str = "",
    *,
    edge_threshold: float = 0.05,
    kelly_frac: float = 0.5,
    starting_capital: float = 1000.0,
) -> BacktestResult:
    """Run a simple price-based backtest.

    This uses a mean-reversion heuristic as a stand-in for Claude
    estimates (calling Claude for every historical point would be
    prohibitively expensive).  The real value is structural: it
    validates the sizing, PnL, and drawdown logic.
    """
    history = await fetch_price_history(token_id)
    if len(history) < 20:
        logger.warning("Not enough history for backtest (%d points).", len(history))
        return BacktestResult(market_question=question)

    result = BacktestResult(market_question=question)
    equity = starting_capital
    peak = equity
    position: BacktestTrade | None = None

    # Simple mean-reversion backtest:
    # compute rolling mean, trade when price deviates by > edge_threshold
    window = 10
    prices = [float(p.get("price", p.get("p", 0))) for p in history if "price" in p or "p" in p]
    timestamps = [p.get("t", p.get("timestamp", "")) for p in history]

    if len(prices) < window + 5:
        return result

    for i in range(window, len(prices)):
        mean = sum(prices[i - window : i]) / window
        current = prices[i]
        ts = str(timestamps[i]) if i < len(timestamps) else ""

        if position is None:
            edge = abs(current - mean)
            if edge > edge_threshold:
                side = "BUY" if current < mean else "SELL"
                size = kelly_size(
                    edge=edge,
                    win_prob=0.55 + edge * 0.5,
                    fraction=kelly_frac,
                    bankroll=equity,
                    max_bet=equity * 0.1,
                )
                if size > 0:
                    position = BacktestTrade(
                        timestamp=ts,
                        side=side,
                        price=current,
                        size_usd=size,
                        edge=edge,
                    )
        else:
            # Close after 5 bars or mean-reversion
            bars_held = i - next(
                (j for j in range(window, i) if str(timestamps[j]) == position.timestamp),
                i - 5,
            )
            revert = (
                (position.side == "BUY" and current >= mean)
                or (position.side == "SELL" and current <= mean)
            )
            if bars_held >= 5 or revert:
                if position.side == "BUY":
                    pnl = (current - position.price) * (position.size_usd / position.price)
                else:
                    pnl = (position.price - current) * (position.size_usd / position.price)
                position.pnl = pnl
                position.closed = True
                result.trades.append(position)
                equity += pnl
                peak = max(peak, equity)
                drawdown = (peak - equity) / peak if peak > 0 else 0
                result.max_drawdown = max(result.max_drawdown, drawdown)
                if pnl > 0:
                    result.win_count += 1
                else:
                    result.loss_count += 1
                position = None

    result.total_pnl = sum(t.pnl for t in result.trades)
    return result


def format_backtest_report(result: BacktestResult) -> str:
    """Format a backtest result as a human-readable string."""
    total = result.win_count + result.loss_count
    wr = result.win_count / total * 100 if total > 0 else 0
    lines = [
        f"Backtest: {result.market_question[:60]}",
        f"  Trades: {total} (W={result.win_count} L={result.loss_count})",
        f"  Win rate: {wr:.1f}%",
        f"  Total PnL: ${result.total_pnl:.2f}",
        f"  Max drawdown: {result.max_drawdown:.1%}",
    ]
    return "\n".join(lines)
