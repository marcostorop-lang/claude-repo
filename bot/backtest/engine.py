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
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from bot.config import cfg
from bot.core.utils import kelly_size

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fee / slippage constants
# ---------------------------------------------------------------------------

_FEE_PCT = 0.02  # 2 % taker fee
_SLIPPAGE_BPS_MEAN = 30  # 30 bps mean slippage


@dataclass
class BacktestTrade:
    timestamp: str
    side: str
    price: float
    size_usd: float
    edge: float
    pnl: float = 0.0
    closed: bool = False
    close_timestamp: str = ""
    bars_held: int = 0


@dataclass
class BacktestResult:
    market_question: str = ""
    trades: list[BacktestTrade] = field(default_factory=list)
    total_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    avg_trade_duration_bars: float = 0.0
    profit_factor: float = 0.0


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


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


def _load_calibration_estimates(condition_id: str) -> list[dict]:
    """Load recorded Claude estimates from the calibration DB, if available.

    Returns a list of dicts with keys: ts, p_claude, p_market, confidence.
    """
    try:
        from bot.core.calibration import _conn
        with _conn() as con:
            con.row_factory = None
            rows = con.execute(
                "SELECT ts, p_claude, p_market, confidence "
                "FROM estimates WHERE condition_id = ? ORDER BY ts",
                (condition_id,),
            ).fetchall()
        return [
            {"ts": r[0], "p_claude": r[1], "p_market": r[2], "confidence": r[3]}
            for r in rows
        ]
    except Exception:
        logger.debug("Could not load calibration estimates for %s", condition_id[:12])
        return []


def _apply_fee_and_slippage(price: float, side: str) -> float:
    """Apply realistic fee (2%) and slippage (30 bps mean) to a fill price."""
    slip_pct = _SLIPPAGE_BPS_MEAN / 10000.0
    if side == "BUY":
        fill = price * (1.0 + slip_pct)
        fill += fill * _FEE_PCT
        return min(fill, 0.99)
    else:
        fill = price * (1.0 - slip_pct)
        fill -= fill * _FEE_PCT
        return max(fill, 0.01)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


async def run_backtest(
    token_id: str,
    condition_id: str,
    question: str = "",
    *,
    edge_threshold: float = 0.05,
    kelly_frac: float = 0.5,
    starting_capital: float = 1000.0,
    calibration_estimates: list[dict] | None = None,
) -> BacktestResult:
    """Run a price-based backtest with realistic fees, slippage, and metrics.

    Uses recorded Claude estimates from the calibration DB when available.
    Falls back to a mean-reversion heuristic otherwise.

    Closes any open position at the end of the series.
    Computes Sharpe ratio, average trade duration, and profit factor.
    """
    history = await fetch_price_history(token_id)
    if len(history) < 20:
        logger.warning("Not enough history for backtest (%d points).", len(history))
        return BacktestResult(market_question=question)

    # Try to load calibration estimates for this market
    if calibration_estimates is None:
        cal_estimates = _load_calibration_estimates(condition_id)
    else:
        cal_estimates = calibration_estimates

    result = BacktestResult(market_question=question)
    equity = starting_capital
    peak = equity
    position: BacktestTrade | None = None
    entry_bar: int = 0

    # Track per-trade returns for Sharpe
    trade_returns: list[float] = []

    window = 10
    prices = [float(p.get("price", p.get("p", 0))) for p in history if "price" in p or "p" in p]
    timestamps = [p.get("t", p.get("timestamp", "")) for p in history]

    if len(prices) < window + 5:
        return result

    # Build a lookup: timestamp → calibration estimate
    cal_by_ts: dict[float, dict] = {}
    for est in cal_estimates:
        cal_by_ts[est["ts"]] = est

    for i in range(window, len(prices)):
        mean = sum(prices[i - window : i]) / window
        current = prices[i]
        ts = str(timestamps[i]) if i < len(timestamps) else ""

        if position is None:
            # Check if we have a calibration estimate near this timestamp
            edge = abs(current - mean)
            use_cal = False

            # Try to match calibration estimates to this bar
            if cal_estimates:
                # Use the latest available calibration estimate
                for est in cal_estimates:
                    if est.get("p_claude") is not None and est.get("p_market") is not None:
                        cal_edge = abs(est["p_claude"] - current)
                        if cal_edge > edge_threshold:
                            edge = cal_edge
                            side = "BUY" if est["p_claude"] > current else "SELL"
                            use_cal = True
                            break

            if not use_cal:
                if edge <= edge_threshold:
                    continue
                side = "BUY" if current < mean else "SELL"

            size = kelly_size(
                edge=edge,
                win_prob=0.55 + edge * 0.5,
                fraction=kelly_frac,
                bankroll=equity,
                max_bet=equity * 0.1,
            )
            if size > 0:
                fill_price = _apply_fee_and_slippage(current, side)
                position = BacktestTrade(
                    timestamp=ts,
                    side=side,
                    price=fill_price,
                    size_usd=size,
                    edge=edge,
                )
                entry_bar = i
        else:
            # Close after 5 bars or mean-reversion
            bars_held = i - entry_bar
            revert = (
                (position.side == "BUY" and current >= mean)
                or (position.side == "SELL" and current <= mean)
            )
            if bars_held >= 5 or revert:
                exit_price = _apply_fee_and_slippage(
                    current, "SELL" if position.side == "BUY" else "BUY"
                )
                if position.side == "BUY":
                    pnl = (exit_price - position.price) * (position.size_usd / position.price)
                else:
                    pnl = (position.price - exit_price) * (position.size_usd / position.price)
                position.pnl = pnl
                position.closed = True
                position.close_timestamp = ts
                position.bars_held = bars_held
                result.trades.append(position)
                trade_returns.append(pnl / position.size_usd if position.size_usd > 0 else 0.0)
                equity += pnl
                peak = max(peak, equity)
                drawdown = (peak - equity) / peak if peak > 0 else 0
                result.max_drawdown = max(result.max_drawdown, drawdown)
                if pnl > 0:
                    result.win_count += 1
                else:
                    result.loss_count += 1
                position = None

    # Close any remaining open position at the last price
    if position is not None and len(prices) > 0:
        last_price = prices[-1]
        exit_price = _apply_fee_and_slippage(
            last_price, "SELL" if position.side == "BUY" else "BUY"
        )
        if position.side == "BUY":
            pnl = (exit_price - position.price) * (position.size_usd / position.price)
        else:
            pnl = (position.price - exit_price) * (position.size_usd / position.price)
        position.pnl = pnl
        position.closed = True
        position.close_timestamp = str(timestamps[-1]) if timestamps else ""
        position.bars_held = len(prices) - 1 - entry_bar
        result.trades.append(position)
        trade_returns.append(pnl / position.size_usd if position.size_usd > 0 else 0.0)
        equity += pnl
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak if peak > 0 else 0
        result.max_drawdown = max(result.max_drawdown, drawdown)
        if pnl > 0:
            result.win_count += 1
        else:
            result.loss_count += 1

    result.total_pnl = sum(t.pnl for t in result.trades)

    # Compute Sharpe ratio (annualized, assuming hourly bars → ~8760 bars/year)
    if len(trade_returns) >= 2:
        mean_ret = sum(trade_returns) / len(trade_returns)
        var = sum((r - mean_ret) ** 2 for r in trade_returns) / (len(trade_returns) - 1)
        std_ret = math.sqrt(var) if var > 0 else 0.0
        result.sharpe_ratio = (mean_ret / std_ret * math.sqrt(252)) if std_ret > 0 else 0.0
    elif len(trade_returns) == 1:
        result.sharpe_ratio = 0.0  # can't compute with 1 trade

    # Average trade duration in bars
    durations = [t.bars_held for t in result.trades if t.closed]
    result.avg_trade_duration_bars = (
        sum(durations) / len(durations) if durations else 0.0
    )

    # Profit factor = gross_profits / |gross_losses|
    gross_profit = sum(t.pnl for t in result.trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in result.trades if t.pnl < 0))
    result.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

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
        f"  Sharpe ratio: {result.sharpe_ratio:.2f}",
        f"  Avg trade duration: {result.avg_trade_duration_bars:.1f} bars",
        f"  Profit factor: {result.profit_factor:.2f}" if result.profit_factor != float("inf") else "  Profit factor: inf (no losses)",
    ]
    return "\n".join(lines)
