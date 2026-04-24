"""Backtesting engine — single-market, multi-market portfolio, and walk-forward.

Replays historical price data from the Polymarket Data API and
simulates the probability arbitrage strategy with recorded Claude
estimates (or re-queries Claude for each point).

Extended features:
- Multi-market portfolio backtesting (run_portfolio_backtest)
- Resolved-market discovery (fetch_resolved_markets)
- Walk-forward validation (walk_forward_backtest)
- CLI entry point (python -m bot.backtest.engine)
"""

from __future__ import annotations

import asyncio
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


@dataclass
class MarketSpec:
    token_id: str
    condition_id: str
    question: str = ""
    category: str = ""


@dataclass
class PortfolioBacktestResult:
    markets_tested: int = 0
    total_trades: int = 0
    total_pnl: float = 0.0
    portfolio_sharpe: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_trade_pnl: float = 0.0
    per_market: list[BacktestResult] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


@dataclass
class WalkForwardResult:
    windows: list[BacktestResult] = field(default_factory=list)
    aggregate_pnl: float = 0.0
    aggregate_sharpe: float = 0.0
    aggregate_win_rate: float = 0.0
    is_overfit: bool = False


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


# ---------------------------------------------------------------------------
# Resolved market discovery
# ---------------------------------------------------------------------------


async def fetch_resolved_markets(
    limit: int = 50,
    min_volume: float = 10000.0,
) -> list[MarketSpec]:
    """Fetch resolved markets from the Polymarket Gamma API for backtesting."""
    url = f"{cfg.gamma_url}/events"
    params = {
        "active": "false",
        "closed": "true",
        "limit": str(min(limit * 2, 200)),
        "order": "volume",
        "ascending": "false",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.warning("Gamma API returned %d for resolved markets.", resp.status_code)
                return []
            data = resp.json()
    except Exception:
        logger.exception("Failed to fetch resolved markets.")
        return []

    if not isinstance(data, list):
        return []

    specs: list[MarketSpec] = []
    for event in data:
        markets = event.get("markets", [])
        for mkt in markets:
            vol = float(mkt.get("volume", 0) or 0)
            if vol < min_volume:
                continue
            token_ids = mkt.get("clobTokenIds", [])
            if not token_ids:
                continue
            specs.append(MarketSpec(
                token_id=token_ids[0],
                condition_id=mkt.get("conditionId", ""),
                question=mkt.get("question", event.get("title", ""))[:100],
                category=mkt.get("groupItemTitle", ""),
            ))
            if len(specs) >= limit:
                return specs
    return specs


# ---------------------------------------------------------------------------
# Multi-market portfolio backtest
# ---------------------------------------------------------------------------


async def run_portfolio_backtest(
    markets: list[MarketSpec],
    *,
    edge_threshold: float = 0.05,
    kelly_frac: float = 0.5,
    starting_capital: float = 1000.0,
    max_concurrent: int = 5,
) -> PortfolioBacktestResult:
    """Run backtests across multiple markets and aggregate portfolio metrics."""
    sem = asyncio.Semaphore(max_concurrent)

    async def _run_one(spec: MarketSpec) -> BacktestResult:
        async with sem:
            return await run_backtest(
                token_id=spec.token_id,
                condition_id=spec.condition_id,
                question=spec.question,
                edge_threshold=edge_threshold,
                kelly_frac=kelly_frac,
                starting_capital=starting_capital / max(len(markets), 1),
            )

    results = await asyncio.gather(*[_run_one(m) for m in markets], return_exceptions=True)

    per_market: list[BacktestResult] = []
    all_trades: list[BacktestTrade] = []
    all_returns: list[float] = []

    for r in results:
        if isinstance(r, BaseException):
            continue
        per_market.append(r)
        all_trades.extend(r.trades)
        for t in r.trades:
            if t.size_usd > 0:
                all_returns.append(t.pnl / t.size_usd)

    total_pnl = sum(r.total_pnl for r in per_market)
    total_wins = sum(r.win_count for r in per_market)
    total_losses = sum(r.loss_count for r in per_market)
    total_count = total_wins + total_losses

    # Portfolio Sharpe
    portfolio_sharpe = 0.0
    if len(all_returns) >= 2:
        mean_ret = sum(all_returns) / len(all_returns)
        var = sum((r - mean_ret) ** 2 for r in all_returns) / (len(all_returns) - 1)
        std_ret = math.sqrt(var) if var > 0 else 0.0
        portfolio_sharpe = (mean_ret / std_ret * math.sqrt(252)) if std_ret > 0 else 0.0

    # Portfolio max drawdown (sum PnLs chronologically)
    equity = starting_capital
    peak = equity
    max_dd = 0.0
    equity_curve = [equity]
    sorted_trades = sorted(all_trades, key=lambda t: t.timestamp)
    for t in sorted_trades:
        equity += t.pnl
        equity_curve.append(round(equity, 2))
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Profit factor
    gross_profit = sum(t.pnl for t in all_trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in all_trades if t.pnl < 0))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    return PortfolioBacktestResult(
        markets_tested=len(per_market),
        total_trades=total_count,
        total_pnl=round(total_pnl, 2),
        portfolio_sharpe=round(portfolio_sharpe, 3),
        max_drawdown=round(max_dd, 4),
        win_rate=round(total_wins / total_count, 4) if total_count > 0 else 0.0,
        profit_factor=round(pf, 3) if pf != float("inf") else float("inf"),
        avg_trade_pnl=round(total_pnl / total_count, 4) if total_count > 0 else 0.0,
        per_market=per_market,
        equity_curve=equity_curve,
    )


# ---------------------------------------------------------------------------
# Walk-forward validation
# ---------------------------------------------------------------------------


async def walk_forward_backtest(
    token_id: str,
    condition_id: str,
    question: str = "",
    *,
    n_windows: int = 5,
    train_pct: float = 0.7,
    edge_threshold: float = 0.05,
    kelly_frac: float = 0.5,
    starting_capital: float = 1000.0,
) -> WalkForwardResult:
    """Walk-forward validation: train/test split across time windows.

    Prevents overfitting by evaluating out-of-sample performance.
    """
    history = await fetch_price_history(token_id)
    if len(history) < 50:
        return WalkForwardResult()

    prices = [float(p.get("price", p.get("p", 0))) for p in history if "price" in p or "p" in p]
    timestamps = [p.get("t", p.get("timestamp", "")) for p in history]

    if len(prices) < 50:
        return WalkForwardResult()

    window_size = len(prices) // n_windows
    if window_size < 20:
        n_windows = max(2, len(prices) // 20)
        window_size = len(prices) // n_windows

    windows: list[BacktestResult] = []
    in_sample_pnls: list[float] = []
    out_sample_pnls: list[float] = []

    for w in range(n_windows):
        start = w * window_size
        end = min(start + window_size, len(prices))
        if end - start < 20:
            continue

        train_end = start + int((end - start) * train_pct)
        train_prices = prices[start:train_end]
        test_prices = prices[train_end:end]

        if len(train_prices) < 10 or len(test_prices) < 5:
            continue

        # Train: compute optimal edge threshold from training data
        train_mean = sum(train_prices) / len(train_prices)
        train_vol = math.sqrt(sum((p - train_mean) ** 2 for p in train_prices) / len(train_prices)) if len(train_prices) > 1 else 0.01
        adapted_threshold = max(edge_threshold, train_vol * 0.5)

        # Test: run backtest on test window with adapted threshold
        test_history = [{"price": p, "t": timestamps[train_end + i] if (train_end + i) < len(timestamps) else ""} for i, p in enumerate(test_prices)]

        result = BacktestResult(market_question=f"Window {w+1}/{n_windows}: {question[:40]}")
        equity = starting_capital / n_windows
        peak = equity
        position = None
        entry_bar = 0
        test_window = 10

        if len(test_prices) < test_window + 2:
            continue

        for i in range(test_window, len(test_prices)):
            mean = sum(test_prices[i - test_window:i]) / test_window
            current = test_prices[i]

            if position is None:
                edge = abs(current - mean)
                if edge <= adapted_threshold:
                    continue
                side = "BUY" if current < mean else "SELL"
                size = kelly_size(edge=edge, win_prob=0.55 + edge * 0.5, fraction=kelly_frac, bankroll=equity, max_bet=equity * 0.1)
                if size > 0:
                    fill_price = _apply_fee_and_slippage(current, side)
                    position = BacktestTrade(timestamp="", side=side, price=fill_price, size_usd=size, edge=edge)
                    entry_bar = i
            else:
                bars_held = i - entry_bar
                revert = (position.side == "BUY" and current >= mean) or (position.side == "SELL" and current <= mean)
                if bars_held >= 5 or revert:
                    exit_price = _apply_fee_and_slippage(current, "SELL" if position.side == "BUY" else "BUY")
                    pnl = ((exit_price - position.price) if position.side == "BUY" else (position.price - exit_price)) * (position.size_usd / position.price)
                    position.pnl = pnl
                    position.closed = True
                    position.bars_held = bars_held
                    result.trades.append(position)
                    equity += pnl
                    peak = max(peak, equity)
                    dd = (peak - equity) / peak if peak > 0 else 0
                    result.max_drawdown = max(result.max_drawdown, dd)
                    if pnl > 0:
                        result.win_count += 1
                    else:
                        result.loss_count += 1
                    position = None

        result.total_pnl = sum(t.pnl for t in result.trades)
        out_sample_pnls.append(result.total_pnl)
        windows.append(result)

    aggregate_pnl = sum(w.total_pnl for w in windows)
    total_wins = sum(w.win_count for w in windows)
    total_losses = sum(w.loss_count for w in windows)
    total_count = total_wins + total_losses

    all_returns = []
    for w in windows:
        for t in w.trades:
            if t.size_usd > 0:
                all_returns.append(t.pnl / t.size_usd)

    agg_sharpe = 0.0
    if len(all_returns) >= 2:
        mean_ret = sum(all_returns) / len(all_returns)
        var = sum((r - mean_ret) ** 2 for r in all_returns) / (len(all_returns) - 1)
        std_ret = math.sqrt(var) if var > 0 else 0.0
        agg_sharpe = (mean_ret / std_ret * math.sqrt(252)) if std_ret > 0 else 0.0

    # Detect overfitting: if first half of windows performed much better
    is_overfit = False
    if len(out_sample_pnls) >= 4:
        mid = len(out_sample_pnls) // 2
        early_avg = sum(out_sample_pnls[:mid]) / mid
        late_avg = sum(out_sample_pnls[mid:]) / (len(out_sample_pnls) - mid)
        if early_avg > 0 and late_avg < early_avg * 0.3:
            is_overfit = True

    return WalkForwardResult(
        windows=windows,
        aggregate_pnl=round(aggregate_pnl, 2),
        aggregate_sharpe=round(agg_sharpe, 3),
        aggregate_win_rate=round(total_wins / total_count, 4) if total_count > 0 else 0.0,
        is_overfit=is_overfit,
    )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_portfolio_report(result: PortfolioBacktestResult) -> str:
    """Format a portfolio backtest result."""
    lines = [
        "=" * 60,
        "PORTFOLIO BACKTEST REPORT",
        "=" * 60,
        f"  Markets tested: {result.markets_tested}",
        f"  Total trades: {result.total_trades}",
        f"  Total PnL: ${result.total_pnl:.2f}",
        f"  Win rate: {result.win_rate:.1%}",
        f"  Portfolio Sharpe: {result.portfolio_sharpe:.2f}",
        f"  Max drawdown: {result.max_drawdown:.1%}",
        f"  Profit factor: {result.profit_factor:.2f}" if result.profit_factor != float("inf") else "  Profit factor: inf",
        f"  Avg trade PnL: ${result.avg_trade_pnl:.4f}",
        "",
        "Per-market breakdown:",
    ]
    for r in result.per_market:
        total = r.win_count + r.loss_count
        if total == 0:
            continue
        wr = r.win_count / total * 100
        lines.append(f"  {r.market_question[:50]:50s} | {total:3d} trades | ${r.total_pnl:8.2f} | WR={wr:.0f}%")
    return "\n".join(lines)


def format_walk_forward_report(result: WalkForwardResult) -> str:
    """Format a walk-forward validation result."""
    lines = [
        "=" * 60,
        "WALK-FORWARD VALIDATION",
        "=" * 60,
        f"  Windows: {len(result.windows)}",
        f"  Aggregate PnL: ${result.aggregate_pnl:.2f}",
        f"  Aggregate Sharpe: {result.aggregate_sharpe:.2f}",
        f"  Aggregate win rate: {result.aggregate_win_rate:.1%}",
        f"  Overfit detected: {'YES' if result.is_overfit else 'NO'}",
        "",
    ]
    for i, w in enumerate(result.windows, 1):
        total = w.win_count + w.loss_count
        lines.append(f"  Window {i}: {total} trades, PnL=${w.total_pnl:.2f}, DD={w.max_drawdown:.1%}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


async def run_from_cli() -> None:
    """Run a quick backtest on recently resolved markets."""
    print("Fetching resolved markets from Polymarket...")
    markets = await fetch_resolved_markets(limit=10, min_volume=20000.0)
    if not markets:
        print("No resolved markets found with sufficient volume.")
        return

    print(f"Found {len(markets)} markets. Running portfolio backtest...")
    result = await run_portfolio_backtest(markets, starting_capital=1000.0)
    print(format_portfolio_report(result))


if __name__ == "__main__":
    asyncio.run(run_from_cli())
