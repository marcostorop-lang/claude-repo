"""Rigorous performance metrics for trading evaluation.

Pure analytics — **read-only**, no side effects, no impact on trading logic.
Designed to be called from CLI / dashboard / tests without touching the
main loop.

Why: win-rate alone can't compare strategies fairly.  A strategy with
60% win rate can still blow up if the losers are much larger than the
winners, or if the equity curve is wildly volatile.  These metrics
(Sharpe, Sortino, expectancy, Calmar) are the standard quant toolkit
for comparing strategies on a risk-adjusted basis.

All functions are defensive: they return sentinel values (0.0 / None)
on empty or degenerate input rather than raising, so dashboards don't
crash on a fresh DB.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Sequence


# Annualisation factor for per-trade metrics when holding period is unknown.
# With no explicit holding period assumption, we annualise daily-bucketed
# returns by sqrt(252) (trading-day convention).  This gives dashboard-
# readable Sharpe numbers that are comparable to industry norms.
_TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class PerformanceReport:
    """Summary of risk-adjusted performance metrics."""

    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float
    avg_win: float
    avg_loss: float  # positive magnitude (absolute value)
    profit_factor: float
    expectancy: float  # average PnL per trade
    total_pnl: float
    max_drawdown: float
    sharpe_daily: float  # annualised from daily buckets
    sortino_daily: float  # annualised from daily buckets
    calmar: float  # total_return / max_drawdown
    stdev_trade_pnl: float
    best_trade: float
    worst_trade: float

    def as_dict(self) -> dict:
        """Return a JSON-serialisable dict (useful for dashboards)."""
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _std(values: Sequence[float]) -> float:
    """Population stdev; 0.0 on degenerate input (< 2 samples)."""
    n = len(values)
    if n < 2:
        return 0.0
    mu = sum(values) / n
    var = sum((v - mu) ** 2 for v in values) / n
    return math.sqrt(var)


def _downside_std(values: Sequence[float], target: float = 0.0) -> float:
    """Stdev of returns below ``target`` (downside deviation).

    Sortino's key insight: penalise only losses, not symmetric volatility.
    We compute the root-mean-square of negative deviations from target.
    """
    downside = [min(0.0, v - target) for v in values]
    n = len(downside)
    if n < 2:
        return 0.0
    sq = sum(d * d for d in downside) / n
    return math.sqrt(sq)


def _compute_daily_returns(trades: Sequence[dict]) -> list[float]:
    """Bucket closed-trade PnLs by UTC day and return the daily PnL series.

    Expects trades with ``exit_timestamp`` or ``timestamp`` (ISO format) and
    ``pnl`` keys.  A trade with no exit timestamp is skipped.  Used only
    as input to Sharpe/Sortino — not a substitute for mark-to-market.
    """
    daily: dict[str, float] = {}
    for t in trades:
        ts = t.get("exit_timestamp") or t.get("timestamp") or ""
        if not ts:
            continue
        day = ts[:10]  # YYYY-MM-DD
        daily[day] = daily.get(day, 0.0) + float(t.get("pnl", 0.0))
    return [daily[d] for d in sorted(daily.keys())]


def _drawdown(equity_curve: Sequence[float]) -> float:
    """Max peak-to-trough drawdown (positive magnitude)."""
    peak = float("-inf")
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return max_dd


def compute_performance(trades: Sequence[dict]) -> PerformanceReport:
    """Compute a full performance report from a sequence of closed trades.

    Each trade must be a dict with at least a ``pnl`` key.  Optional keys:
    ``exit_timestamp`` / ``timestamp`` for time-bucketed Sharpe/Sortino.

    Returns a ``PerformanceReport`` with all-zero metrics when there are
    no trades — safe to render on an empty dashboard.
    """
    pnls = [float(t.get("pnl", 0.0)) for t in trades]
    n = len(pnls)
    if n == 0:
        return PerformanceReport(
            n_trades=0, n_wins=0, n_losses=0, win_rate=0.0,
            avg_win=0.0, avg_loss=0.0, profit_factor=0.0, expectancy=0.0,
            total_pnl=0.0, max_drawdown=0.0, sharpe_daily=0.0,
            sortino_daily=0.0, calmar=0.0, stdev_trade_pnl=0.0,
            best_trade=0.0, worst_trade=0.0,
        )

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n_wins = len(wins)
    n_losses = len(losses)
    total_pnl = sum(pnls)
    avg_win = (sum(wins) / n_wins) if n_wins else 0.0
    avg_loss_abs = (abs(sum(losses)) / n_losses) if n_losses else 0.0
    gross_profit = sum(wins)
    gross_loss_abs = abs(sum(losses))
    profit_factor = (
        gross_profit / gross_loss_abs if gross_loss_abs > 0
        else (float("inf") if gross_profit > 0 else 0.0)
    )
    win_rate = n_wins / n if n else 0.0
    expectancy = total_pnl / n

    # Equity curve → max drawdown
    equity: list[float] = []
    running = 0.0
    for p in pnls:
        running += p
        equity.append(running)
    max_dd = _drawdown(equity)

    # Daily-bucketed Sharpe / Sortino (annualised)
    daily = _compute_daily_returns(trades)
    if len(daily) >= 2:
        mu_d = sum(daily) / len(daily)
        sd_d = _std(daily)
        dsd_d = _downside_std(daily, target=0.0)
        sharpe = (mu_d / sd_d) * math.sqrt(_TRADING_DAYS_PER_YEAR) if sd_d > 0 else 0.0
        sortino = (mu_d / dsd_d) * math.sqrt(_TRADING_DAYS_PER_YEAR) if dsd_d > 0 else 0.0
    else:
        sharpe = 0.0
        sortino = 0.0

    # Calmar = annualised-ish total_pnl / max_drawdown.  We don't annualise
    # the numerator here (we don't know the time span from a pure trade list);
    # it's a simple risk-adjusted ratio.  Callers who want strict Calmar can
    # divide annualised return by max_dd themselves.
    calmar = (total_pnl / max_dd) if max_dd > 0 else 0.0

    return PerformanceReport(
        n_trades=n, n_wins=n_wins, n_losses=n_losses, win_rate=win_rate,
        avg_win=avg_win, avg_loss=avg_loss_abs, profit_factor=profit_factor,
        expectancy=expectancy, total_pnl=total_pnl, max_drawdown=max_dd,
        sharpe_daily=sharpe, sortino_daily=sortino, calmar=calmar,
        stdev_trade_pnl=_std(pnls), best_trade=max(pnls), worst_trade=min(pnls),
    )


def build_trade_pnls_from_store(store) -> list[dict]:
    """Convert raw BUY/SELL trades from the store into closed-trade PnL rows.

    Matches each BUY with its FIFO-paired SELL on the same token_id, as the
    dashboard already does.  Returns a list of ``{pnl, exit_timestamp, ...}``
    dicts suitable for :func:`compute_performance`.

    Read-only, never writes to the store.
    """
    trades = store.get_all_trades() if hasattr(store, "get_all_trades") else []
    buys_by_token: dict[str, list[dict]] = {}
    closed: list[dict] = []
    for t in trades:
        token = t["token_id"]
        if t["side"] == "BUY":
            buys_by_token.setdefault(token, []).append(t)
        elif t["side"] == "SELL" and buys_by_token.get(token):
            buy = buys_by_token[token].pop(0)
            size = min(float(buy["size"]), float(t["size"]))
            pnl = (float(t["price"]) - float(buy["price"])) * size
            closed.append({
                "pnl": pnl,
                "entry_timestamp": buy["timestamp"],
                "exit_timestamp": t["timestamp"],
                "token_id": token,
                "strategy": t.get("strategy", ""),
                "size": size,
                "entry_price": float(buy["price"]),
                "exit_price": float(t["price"]),
            })
    return closed


# --------------------------------------------------------------------------
# Bootstrap confidence intervals
# --------------------------------------------------------------------------
#
# Point estimates of Sharpe and win-rate are easy to misread at small N:
# a Sharpe of 1.0 over 20 trades is statistically indistinguishable from
# zero.  These helpers expose non-parametric CIs (percentile bootstrap)
# so the dashboard can render bands instead of bare numbers.
#
# Pure Python — same constraint as the rest of the analysis package.


def _bootstrap_ci(
    values: Sequence[float],
    metric_fn: Callable[[Sequence[float]], float],
    n_resamples: int = 1000,
    alpha: float = 0.05,
    seed: int | None = None,
) -> tuple[float, float]:
    """Generic percentile-bootstrap CI.

    Returns ``(nan, nan)`` for fewer than two observations or when
    ``n_resamples`` is zero.  Caller chooses ``metric_fn`` so this
    function is reusable for Sharpe, win-rate, profit-factor, etc.
    """
    n = len(values)
    if n < 2 or n_resamples <= 0:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(n_resamples):
        resampled = [values[rng.randrange(n)] for _ in range(n)]
        try:
            samples.append(metric_fn(resampled))
        except Exception:
            # Metric undefined on this resample (e.g. all losses for
            # profit-factor) — drop, don't fail the whole CI.
            continue
    if len(samples) < 2:
        return float("nan"), float("nan")
    samples.sort()
    lo = samples[max(0, int(math.floor((alpha / 2) * len(samples))))]
    hi = samples[min(len(samples) - 1, int(math.ceil((1 - alpha / 2) * len(samples))) - 1)]
    return lo, hi


def _sharpe_from_daily(daily: Sequence[float]) -> float:
    """Annualised daily Sharpe (matches the in-line computation in
    :func:`compute_performance`).  Returns 0 on degenerate input so the
    bootstrap doesn't drop too many resamples."""
    if len(daily) < 2:
        return 0.0
    mu = sum(daily) / len(daily)
    sd = _std(daily)
    if sd <= 0:
        return 0.0
    return (mu / sd) * math.sqrt(_TRADING_DAYS_PER_YEAR)


def sharpe_ci(
    daily_returns: Sequence[float],
    n_resamples: int = 1000,
    alpha: float = 0.05,
    seed: int | None = None,
) -> tuple[float, float]:
    """95% bootstrap CI for the annualised daily Sharpe ratio.

    Bootstraps the *daily* PnL series (not individual trades) so the
    annualisation is consistent with ``compute_performance.sharpe_daily``.
    """
    return _bootstrap_ci(
        list(daily_returns), _sharpe_from_daily,
        n_resamples=n_resamples, alpha=alpha, seed=seed,
    )


def _win_rate(pnls: Sequence[float]) -> float:
    if not pnls:
        return 0.0
    return sum(1 for p in pnls if p > 0) / len(pnls)


def win_rate_ci(
    trade_pnls: Sequence[float],
    n_resamples: int = 1000,
    alpha: float = 0.05,
    seed: int | None = None,
) -> tuple[float, float]:
    """95% bootstrap CI for win-rate.

    Equivalent to a Wilson interval for very large N but doesn't assume
    normality — preferred at the small samples we typically see.
    """
    return _bootstrap_ci(
        list(trade_pnls), _win_rate,
        n_resamples=n_resamples, alpha=alpha, seed=seed,
    )


@dataclass(frozen=True)
class PerformanceCI:
    """Confidence intervals attached to a :class:`PerformanceReport`."""

    n_resamples: int
    alpha: float
    sharpe_low: float
    sharpe_high: float
    win_rate_low: float
    win_rate_high: float

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def compute_ci(
    trades: Sequence[dict],
    n_resamples: int = 1000,
    alpha: float = 0.05,
    seed: int | None = None,
) -> PerformanceCI:
    """Compute bootstrap CIs alongside the standard performance report.

    Returns ``nan`` for both ends of any CI that is undefined (too few
    samples).  Cheap when ``n_resamples`` is reasonable: a few thousand
    1-D resamples take milliseconds even at N=10k trades.
    """
    pnls = [float(t.get("pnl", 0.0)) for t in trades]
    daily = _compute_daily_returns(trades)
    s_lo, s_hi = sharpe_ci(daily, n_resamples=n_resamples, alpha=alpha, seed=seed)
    w_lo, w_hi = win_rate_ci(pnls, n_resamples=n_resamples, alpha=alpha, seed=seed)
    return PerformanceCI(
        n_resamples=n_resamples, alpha=alpha,
        sharpe_low=s_lo, sharpe_high=s_hi,
        win_rate_low=w_lo, win_rate_high=w_hi,
    )


def format_report_markdown(report: PerformanceReport) -> str:
    """Render a human-readable markdown summary — for CLI output."""
    if report.n_trades == 0:
        return "_No closed trades yet._"
    pf = (
        f"{report.profit_factor:.2f}" if math.isfinite(report.profit_factor) else "∞"
    )
    lines = [
        "| Metric | Value |",
        "|---|---:|",
        f"| Trades | {report.n_trades} (W {report.n_wins} / L {report.n_losses}) |",
        f"| Win rate | {report.win_rate * 100:.1f}% |",
        f"| Total PnL | {report.total_pnl:+.4f} |",
        f"| Expectancy / trade | {report.expectancy:+.4f} |",
        f"| Profit factor | {pf} |",
        f"| Avg win / avg loss | {report.avg_win:.4f} / {report.avg_loss:.4f} |",
        f"| Best / worst trade | {report.best_trade:+.4f} / {report.worst_trade:+.4f} |",
        f"| Trade-PnL stdev | {report.stdev_trade_pnl:.4f} |",
        f"| Max drawdown | {report.max_drawdown:.4f} |",
        f"| Sharpe (daily, annualised) | {report.sharpe_daily:.2f} |",
        f"| Sortino (daily, annualised) | {report.sortino_daily:.2f} |",
        f"| Calmar (total / max_dd) | {report.calmar:.2f} |",
    ]
    return "\n".join(lines)
