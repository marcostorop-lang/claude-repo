"""
Walk-forward validation harness.

Why
---
A naive single backtest produces a single Sharpe number that is
*easy* to overfit to.  Walk-forward analysis (López de Prado, AFML
ch. 7) instead carves the timeline into rolling train/test windows,
evaluates the strategy on each *out-of-sample* test slice, and
reports the **distribution** of OOS performance.  Three failures
that single-shot backtests hide become visible:

1. **In-sample / out-of-sample divergence**: the strategy looks great
   in-sample then collapses on never-seen data.
2. **Regime sensitivity**: PnL is concentrated in 1-2 favourable
   windows and flat or negative in the rest.
3. **Trade-overlap leak**: a position opened in train that closes
   inside test contaminates the OOS PnL with information from train.
   Mitigated here by a **purge gap** (drop trades whose entry-to-exit
   window crosses a split boundary) and an **embargo** after train
   (skip the first N test ticks where train-side serial autocorrelation
   could leak in).

Scope
-----
This harness operates on per-token price histories (the same shape
``Backtester.run`` consumes).  It does *not* re-train any model
parameters across windows — the strategies in this repo are
parameter-light and read straight from ``Config`` — but the slicing
contract is identical to a real walk-forward where you would refit
inside ``train``.  Refit can be added per-strategy without changing
the harness.

This module is *only* called by the CLI ``validate-strategy``
command and the offline experiments runner; it never runs inside the
trading loop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from src.backtest.engine import BacktestReport, Backtester
from src.config import Config
from src.strategy.base import BaseStrategy


@dataclass
class WindowResult:
    """One OOS window of a walk-forward run."""

    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    n_test_ticks: int
    n_trades: int
    total_pnl: float
    avg_return_pct: float
    sharpe_annualized: float
    win_rate: float
    max_drawdown_pct: float


@dataclass
class WalkForwardReport:
    """Aggregate report across every OOS window."""

    strategy: str
    n_windows: int
    n_tokens: int
    train_size: int
    test_size: int
    purge: int
    embargo: int
    seed: int | None
    windows: list[WindowResult] = field(default_factory=list)
    pooled_returns: list[float] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Aggregate stats
    # ------------------------------------------------------------------

    def total_pnl(self) -> float:
        return sum(w.total_pnl for w in self.windows)

    def total_trades(self) -> int:
        return sum(w.n_trades for w in self.windows)

    def winning_windows(self) -> int:
        return sum(1 for w in self.windows if w.total_pnl > 0)

    def windows_above_zero_sharpe(self) -> int:
        return sum(1 for w in self.windows if w.sharpe_annualized > 0)

    def median_oos_sharpe(self) -> float:
        srs = sorted(w.sharpe_annualized for w in self.windows)
        if not srs:
            return 0.0
        m = len(srs) // 2
        return srs[m] if len(srs) % 2 == 1 else 0.5 * (srs[m - 1] + srs[m])

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "n_windows": self.n_windows,
            "n_tokens": self.n_tokens,
            "train_size": self.train_size,
            "test_size": self.test_size,
            "purge": self.purge,
            "embargo": self.embargo,
            "seed": self.seed,
            "total_pnl": self.total_pnl(),
            "total_trades": self.total_trades(),
            "median_oos_sharpe": self.median_oos_sharpe(),
            "winning_windows": self.winning_windows(),
            "windows": [w.__dict__ for w in self.windows],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slice_history(history: list, start: int, end: int) -> list:
    """Return the inclusive-exclusive slice ``[start, end)``."""
    if start >= end:
        return []
    return history[start:end]


def _trade_returns(report: BacktestReport) -> list[float]:
    """Return-per-trade list extracted from a BacktestReport, *only*
    for trades that closed (we exclude EOF-still-open trades from the
    series since their PnL is mark-to-market, not realised).
    """
    out: list[float] = []
    for t in report.trades:
        if t.exit_reason == "" or t.exit_reason == "eof":
            continue
        # ``return_pct`` is a property on BacktestTrade that handles
        # the divide-by-zero edge.
        out.append(t.return_pct)
    return out


# ---------------------------------------------------------------------------
# Splitter
# ---------------------------------------------------------------------------


def make_splits(
    n_ticks: int,
    *,
    train_size: int,
    test_size: int,
    purge: int = 0,
    embargo: int = 0,
    step: int | None = None,
) -> list[tuple[int, int, int, int]]:
    """Generate non-overlapping ``(train_start, train_end, test_start, test_end)``.

    Anchored walk-forward (López de Prado AFML §7.4): each window
    advances by ``step`` ticks (default = ``test_size`` for
    non-overlapping OOS regions).  ``purge`` drops the last ``purge``
    ticks of the train window — these are the candidate trade-overlap
    rows that would cross into test.  ``embargo`` drops the first
    ``embargo`` ticks of the test window — buffering against
    short-range autocorrelation leak.

    Returns an empty list when the inputs are too small to fit even
    one window — callers should treat that as "not enough data yet".
    """
    if n_ticks <= 0 or train_size <= 0 or test_size <= 0:
        return []
    if step is None:
        step = test_size
    if step <= 0:
        return []
    splits: list[tuple[int, int, int, int]] = []
    cursor = 0
    while True:
        tr_start = cursor
        tr_end_full = tr_start + train_size
        purged_end = tr_end_full - purge
        te_start_raw = tr_end_full
        te_start = te_start_raw + embargo
        te_end = te_start + test_size
        if te_end > n_ticks:
            break
        if purged_end <= tr_start:
            # Pathological: purge ate the entire train window.
            break
        splits.append((tr_start, purged_end, te_start, te_end))
        cursor += step
    return splits


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_walk_forward(
    cfg: Config,
    strategy: BaseStrategy,
    price_histories: dict[str, list],
    *,
    train_size: int = 200,
    test_size: int = 100,
    purge: int = 0,
    embargo: int = 0,
    step: int | None = None,
    assumed_spread: float = 0.02,
    position_size_usd: float | None = None,
    seed: int | None = 0,
    rejection_prob: float = 0.0,
    partial_fill_prob: float = 0.0,
    periods_per_year: int = 252,
) -> WalkForwardReport:
    """Run anchored walk-forward across all tokens.

    For each split, build per-token sub-histories restricted to the
    test slice (the strategies in this repo do not refit, but the
    train slice is preserved here so a future refitable strategy can
    consume it).  Run ``Backtester.run`` over the test slice.

    The seed is **forwarded to the inner Backtester** so the
    walk-forward report is fully reproducible.  Each window perturbs
    the seed by its index so different windows draw different RNG
    streams without losing determinism.
    """
    report = WalkForwardReport(
        strategy=strategy.name,
        n_windows=0,
        n_tokens=len(price_histories),
        train_size=train_size,
        test_size=test_size,
        purge=purge,
        embargo=embargo,
        seed=seed,
    )
    if not price_histories:
        return report

    # Use the longest history to size splits.  Tokens with shorter
    # histories simply contribute nothing to early windows but rejoin
    # once their data starts.
    max_len = max(len(h) for h in price_histories.values())
    splits = make_splits(
        max_len,
        train_size=train_size,
        test_size=test_size,
        purge=purge,
        embargo=embargo,
        step=step,
    )
    report.n_windows = len(splits)
    if not splits:
        return report

    for w_idx, (tr_s, tr_e, te_s, te_e) in enumerate(splits):
        # Build per-window test histories.  A token whose data starts
        # after ``te_s`` would yield an empty slice and is skipped by
        # the inner backtester (min-points filter).
        window_histories: dict[str, list] = {}
        for tok, hist in price_histories.items():
            sub = _slice_history(hist, te_s, te_e)
            if len(sub) >= 2:
                window_histories[tok] = sub
        bt = Backtester(
            cfg, strategy,
            assumed_spread=assumed_spread,
            position_size_usd=position_size_usd,
            seed=None if seed is None else seed + w_idx,
            rejection_prob=rejection_prob,
            partial_fill_prob=partial_fill_prob,
        )
        sub_report = bt.run(window_histories)
        # Per-trade returns (excludes EOF carries).
        tr_returns = _trade_returns(sub_report)
        # Sharpe of the test slice.  Per-period = per-trade here, so
        # ``periods_per_year`` is interpreted as "annualizing factor"
        # — caller chooses whether to use 252 (daily) or a rougher
        # trade-frequency estimate.
        from src.analysis.risk_metrics import returns_summary
        rs = returns_summary(tr_returns, periods_per_year=periods_per_year)
        report.windows.append(WindowResult(
            index=w_idx,
            train_start=tr_s, train_end=tr_e,
            test_start=te_s, test_end=te_e,
            n_test_ticks=te_e - te_s,
            n_trades=sub_report.trades_closed,
            total_pnl=sub_report.total_pnl,
            avg_return_pct=sub_report.avg_return_pct,
            sharpe_annualized=rs.sharpe_annualized,
            win_rate=sub_report.win_rate,
            max_drawdown_pct=sub_report.max_drawdown_pct,
        ))
        report.pooled_returns.extend(tr_returns)

    return report
