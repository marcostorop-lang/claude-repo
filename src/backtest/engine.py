"""
Backtesting engine.

Replays price history (from the SQLite price_history table or an in-memory
dict) through a strategy and reports what would have happened. The goal is
not to produce a perfect simulation of Polymarket — that requires full order
book history we do not store — but to answer three concrete questions before
changing the strategy:

1. Does the strategy generate signals on real historical data?
2. What does the PnL distribution look like across many replays?
3. Is the predicted ``confidence`` correlated with realised outcome?

Assumptions / known limitations (documented inline so nobody treats the
output as ground truth):

- Only midpoint prices are replayed; historical bid/ask/depth is not stored.
- Spread is approximated with a constant ``assumed_spread`` (default 0.02).
- Fills apply half-spread slippage like the live paper engine.
- Stop-loss / take-profit checks run on the next tick (no intrabar logic).
- No market-level volume / liquidity filters (we assume the historical tick
  already passed them when it was collected live).
- Only one open position per token is allowed (same as the live bot).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Iterable

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import Action, BaseStrategy

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """One simulated round-trip trade."""

    token_id: str
    entry_idx: int
    entry_price: float
    confidence: float
    signal_reason: str
    exit_idx: int | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    size: float = 0.0

    @property
    def return_pct(self) -> float:
        if self.exit_price is None or self.entry_price <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.size


@dataclass
class BacktestReport:
    """Aggregate statistics for one backtest run."""

    strategy: str
    total_ticks: int
    total_tokens: int
    signals_generated: int
    trades_opened: int
    trades_closed: int
    wins: int
    losses: int
    total_return_pct: float
    avg_return_pct: float
    max_drawdown_pct: float
    win_rate: float
    total_pnl: float
    trades: list[BacktestTrade] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Strategy:        {self.strategy}",
            f"Ticks replayed:  {self.total_ticks}",
            f"Tokens:          {self.total_tokens}",
            f"Signals:         {self.signals_generated}",
            f"Trades opened:   {self.trades_opened}",
            f"Trades closed:   {self.trades_closed}",
            f"Wins / losses:   {self.wins} / {self.losses}",
            f"Win rate:        {self.win_rate:.1%}",
            f"Avg return:      {self.avg_return_pct:+.2%}",
            f"Total return:    {self.total_return_pct:+.2%}",
            f"Max drawdown:    -{self.max_drawdown_pct:.2%}",
            f"Total PnL ($):   {self.total_pnl:+.2f}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "trades"}
        d["trades"] = [asdict(t) for t in self.trades]
        return d


class Backtester:
    """Replay price histories through a strategy."""

    def __init__(
        self,
        cfg: Config,
        strategy: BaseStrategy,
        assumed_spread: float = 0.02,
        position_size_usd: float | None = None,
    ) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.assumed_spread = assumed_spread
        self.position_size_usd = position_size_usd or cfg.max_position_size

    def run(self, price_histories: dict[str, list[float]]) -> BacktestReport:
        """Replay every token's history independently.

        Parameters
        ----------
        price_histories:
            ``{token_id: [p0, p1, ..., pN]}`` with chronological prices.
        """
        all_trades: list[BacktestTrade] = []
        signals_generated = 0
        total_ticks = 0
        half_spread = self.assumed_spread / 2.0

        for token_id, history in price_histories.items():
            if len(history) < 2:
                continue
            open_trade: BacktestTrade | None = None

            for i in range(1, len(history)):
                total_ticks += 1
                current_price = history[i]
                history_so_far = history[: i + 1]

                # --- Check exits on open position ---
                if open_trade is not None:
                    loss_pct = (open_trade.entry_price - current_price) / open_trade.entry_price
                    gain_pct = (current_price - open_trade.entry_price) / open_trade.entry_price
                    if loss_pct >= self.cfg.stop_loss_pct:
                        open_trade.exit_idx = i
                        open_trade.exit_price = max(current_price - half_spread, 0.0001)
                        open_trade.exit_reason = "stop_loss"
                        all_trades.append(open_trade)
                        open_trade = None
                        continue
                    if gain_pct >= self.cfg.take_profit_pct:
                        open_trade.exit_idx = i
                        open_trade.exit_price = max(current_price - half_spread, 0.0001)
                        open_trade.exit_reason = "take_profit"
                        all_trades.append(open_trade)
                        open_trade = None
                        continue
                    # Still open — skip new entry logic while a position exists
                    continue

                # --- Price boundary filter (mirror the live bot) ---
                if current_price < self.cfg.min_price or current_price > self.cfg.max_price:
                    continue

                # --- Evaluate strategy ---
                snap = MarketSnapshot(
                    condition_id="",
                    question="",
                    token_id=token_id,
                    outcome="",
                    price=current_price,
                    spread=self.assumed_spread,
                    volume=1e6,
                    liquidity=1e5,
                    active=True,
                )
                sig = self.strategy.evaluate(snap, history_so_far)
                if sig.action != Action.BUY:
                    continue

                signals_generated += 1

                # --- Simulate fill (with slippage) ---
                fill_price = current_price + half_spread
                size = self.position_size_usd / fill_price if fill_price > 0 else 0.0
                open_trade = BacktestTrade(
                    token_id=token_id,
                    entry_idx=i,
                    entry_price=fill_price,
                    confidence=sig.confidence,
                    signal_reason=sig.reason,
                    size=size,
                )

            # Close any still-open trade with the last observed price (end-of-file)
            if open_trade is not None:
                open_trade.exit_idx = len(history) - 1
                open_trade.exit_price = max(history[-1] - half_spread, 0.0001)
                open_trade.exit_reason = "eof"
                all_trades.append(open_trade)

        return self._build_report(
            all_trades=all_trades,
            signals_generated=signals_generated,
            total_ticks=total_ticks,
            total_tokens=len(price_histories),
        )

    def _build_report(
        self,
        all_trades: list[BacktestTrade],
        signals_generated: int,
        total_ticks: int,
        total_tokens: int,
    ) -> BacktestReport:
        closed = [t for t in all_trades if t.exit_price is not None]
        wins = [t for t in closed if t.return_pct > 0]
        losses = [t for t in closed if t.return_pct <= 0]
        returns = [t.return_pct for t in closed]
        pnls = [t.pnl for t in closed]
        total_return = sum(returns)
        avg_return = total_return / len(returns) if returns else 0.0
        win_rate = len(wins) / len(closed) if closed else 0.0

        # Max drawdown on the cumulative return curve
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in returns:
            cum += r
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

        return BacktestReport(
            strategy=self.strategy.name,
            total_ticks=total_ticks,
            total_tokens=total_tokens,
            signals_generated=signals_generated,
            trades_opened=len(all_trades),
            trades_closed=len(closed),
            wins=len(wins),
            losses=len(losses),
            total_return_pct=total_return,
            avg_return_pct=avg_return,
            max_drawdown_pct=max_dd,
            win_rate=win_rate,
            total_pnl=sum(pnls),
            trades=all_trades,
        )


# ---------------------------------------------------------------------------
# Convenience loaders
# ---------------------------------------------------------------------------

def load_price_histories_from_store(
    store: SQLiteStore,
    min_points: int = 5,
) -> dict[str, list[float]]:
    """Load all price histories from the SQLite store, grouped by token.

    Only returns tokens with at least *min_points* observations since most
    strategies require a warmup window.
    """
    cur = store._conn.execute(
        "SELECT token_id, price FROM price_history ORDER BY id ASC"
    )
    histories: dict[str, list[float]] = defaultdict(list)
    for row in cur.fetchall():
        histories[row["token_id"]].append(row["price"])
    return {tok: h for tok, h in histories.items() if len(h) >= min_points}


def save_report(report: BacktestReport, path: str) -> None:
    """Persist a backtest report as JSON for later comparison."""
    with open(path, "w") as f:
        json.dump(report.to_dict(), f, indent=2)
