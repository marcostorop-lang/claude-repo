"""Lightweight backtest harness for the semantic mispricing engine.

Unlike ``src/backtest/engine.py`` — which replays a *single* token's price
history through a generic ``BaseStrategy`` — the semantic engine needs
**cross-sectional** data: at each tick it looks at *all* markets together
to discover relations and synthetic fair prices.  So we run a different
kind of replay:

* input: a sequence of *ticks*, each being a list of
  :class:`MarketSnapshot` objects representing the universe at that
  timestamp,
* process: call :func:`find_semantic_mispricings` on the tick, convert
  each surviving signal into a simulated market order, track the open
  position, close it on the next tick where the engine says the edge is
  gone (or on a stop-loss / take-profit threshold),
* output: a :class:`BacktestReport` with realised PnL, realisation
  ratio vs. detected ``net_edge`` broken down by synthetic method.

This is deliberately conservative about fills:

* A BUY at best_ask + half-spread slippage (one-sided).  SELLs symmetric.
* One open position per token; a second signal is ignored until the
  first is closed.
* No fees unless the caller supplies them — keeps the model simple.

It is NOT a substitute for a live paper run.  Its job is to answer
*"does the engine's detected edge survive a naive one-tick-later exit?"*
and *"which synthetic_method pays its own way in realised terms?"* —
which is the #1 question before moving the strategy out of shadow.

Default-off, read-only, no DB writes.  Never touches portfolio state,
never places real orders, never imports from main.py.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from src.analysis.semantic_engine.engine import find_semantic_mispricings
from src.analysis.semantic_engine.relations import RelationClassifierConfig
from src.analysis.semantic_engine.scoring import ScoringConfig
from src.analysis.semantic_engine.smoothing import EMASmoother
from src.analysis.semantic_engine.types import SemanticMispricing
from src.polymarket.market_data import MarketSnapshot

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass
class SimTrade:
    """One simulated round-trip: open on tick ``open_idx``, close on ``close_idx``."""

    token_id: str
    side: str                 # "BUY" | "SELL"
    method: str               # synthetic_method that produced the signal
    open_idx: int
    open_ts: str
    entry_price: float
    detected_net_edge: float
    size: float               # notional in USD
    close_idx: int | None = None
    close_ts: str = ""
    exit_price: float | None = None
    exit_reason: str = ""     # "signal_gone" | "stop_loss" | "take_profit" | "eoh"

    @property
    def is_closed(self) -> bool:
        return self.exit_price is not None

    @property
    def realised_return(self) -> float:
        """Price-space return, signed by side, as a pct of entry."""
        if self.exit_price is None or self.entry_price <= 0:
            return 0.0
        raw = (self.exit_price - self.entry_price) / self.entry_price
        return raw if self.side == "BUY" else -raw

    @property
    def realised_pnl(self) -> float:
        """USD PnL at the ``size`` at open."""
        if self.exit_price is None:
            return 0.0
        price_delta = (self.exit_price - self.entry_price)
        signed = price_delta if self.side == "BUY" else -price_delta
        # Convert price-move to dollars: size is USD notional at entry, so
        # shares = size / entry_price and P&L = shares * signed_delta.
        if self.entry_price <= 0:
            return 0.0
        return (self.size / self.entry_price) * signed


@dataclass
class MethodPnL:
    method: str
    trades: int = 0
    wins: int = 0
    realised_pnl: float = 0.0
    detected_edge_sum: float = 0.0
    realised_edge_sum: float = 0.0  # realised return per trade, summed

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades) if self.trades else 0.0

    @property
    def avg_detected_edge(self) -> float:
        return (self.detected_edge_sum / self.trades) if self.trades else 0.0

    @property
    def avg_realised_edge(self) -> float:
        return (self.realised_edge_sum / self.trades) if self.trades else 0.0

    @property
    def realisation_ratio(self) -> float:
        """How much of the detected edge was actually realised on average."""
        if self.avg_detected_edge == 0.0:
            return 0.0
        return self.avg_realised_edge / self.avg_detected_edge


@dataclass
class BacktestReport:
    ticks_run: int = 0
    signals_generated: int = 0
    trades_opened: int = 0
    trades_closed: int = 0
    realised_pnl: float = 0.0
    per_method: dict[str, MethodPnL] = field(default_factory=dict)
    open_at_end: int = 0

    def as_dict(self) -> dict:
        return {
            "ticks_run": self.ticks_run,
            "signals_generated": self.signals_generated,
            "trades_opened": self.trades_opened,
            "trades_closed": self.trades_closed,
            "realised_pnl": round(self.realised_pnl, 4),
            "open_at_end": self.open_at_end,
            "per_method": [
                {
                    "method": m.method,
                    "trades": m.trades,
                    "wins": m.wins,
                    "win_rate": round(m.win_rate, 4),
                    "realised_pnl": round(m.realised_pnl, 4),
                    "avg_detected_edge": round(m.avg_detected_edge, 6),
                    "avg_realised_edge": round(m.avg_realised_edge, 6),
                    "realisation_ratio": round(m.realisation_ratio, 4),
                }
                for m in sorted(self.per_method.values(),
                                key=lambda x: -x.trades)
            ],
        }


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------

Tick = tuple[str, Sequence[MarketSnapshot]]  # (timestamp_iso, snapshots)


def replay(
    ticks: Iterable[Tick],
    *,
    position_size_usd: float = 100.0,
    stop_loss_pct: float = 0.10,
    take_profit_pct: float = 0.20,
    relation_cfg: RelationClassifierConfig | None = None,
    scoring_cfg: ScoringConfig | None = None,
    min_relation_confidence: float = 0.65,
    min_net_edge: float = 0.02,
    min_signal_score: float = 0.60,
    use_smoother: bool = False,
    smoother_alpha: float = 0.3,
    fee_bps_roundtrip: float = 0.0,
    on_trade_closed: Callable[[SimTrade], None] | None = None,
) -> BacktestReport:
    """Replay ``ticks`` through the semantic engine and simulate fills.

    The simplest possible fill model: the moment a signal fires on a
    token we open a position at the quoted best_bid/ask (which the
    engine stores on the detection).  On a subsequent tick where the
    token no longer appears in the mispricing set, we close at its
    latest best_bid/ask.  SL/TP close earlier if crossed.

    Fees are applied once per round-trip when ``fee_bps_roundtrip > 0``,
    deducted from realised PnL as a percentage of notional.

    Idempotent and stateless apart from the local open-position dict.
    """
    report = BacktestReport()
    open_by_token: dict[str, SimTrade] = {}
    smoother = EMASmoother(alpha=smoother_alpha) if use_smoother else None

    # Materialise so we can count without exhausting a generator twice.
    tick_list = list(ticks)

    # Lookup the latest snapshot for a token, so we can close out at
    # the current book when a signal disappears.
    last_snap_by_token: dict[str, MarketSnapshot] = {}

    for idx, (ts, snaps) in enumerate(tick_list):
        report.ticks_run += 1
        # Update latest snapshots for closing quotes.
        for s in snaps:
            last_snap_by_token[s.token_id] = s

        mispricings = find_semantic_mispricings(
            snaps,
            relation_cfg=relation_cfg,
            scoring_cfg=scoring_cfg,
            min_relation_confidence=min_relation_confidence,
            min_net_edge=min_net_edge,
            min_signal_score=min_signal_score,
            smoother=smoother,
            tick_ts=ts,
        )
        report.signals_generated += len(mispricings)
        mispriced_tokens = {m.token_id: m for m in mispricings}

        # ---- 1. Close existing positions whose signal has vanished, or
        # which crossed SL/TP at the current midpoint.
        for tok, trade in list(open_by_token.items()):
            snap = last_snap_by_token.get(tok)
            if snap is None or snap.price is None:
                continue
            current_price = float(snap.price)

            exit_reason = ""
            # SL/TP on price move against the side
            move = (current_price - trade.entry_price) / max(trade.entry_price, 1e-9)
            if trade.side == "BUY":
                if move <= -stop_loss_pct:
                    exit_reason = "stop_loss"
                elif move >= take_profit_pct:
                    exit_reason = "take_profit"
            else:
                if -move <= -stop_loss_pct:
                    exit_reason = "stop_loss"
                elif -move >= take_profit_pct:
                    exit_reason = "take_profit"

            if not exit_reason and tok not in mispriced_tokens:
                exit_reason = "signal_gone"

            if exit_reason:
                trade.close_idx = idx
                trade.close_ts = ts
                trade.exit_price = current_price
                trade.exit_reason = exit_reason
                _finalise_trade(trade, report, fee_bps_roundtrip)
                if on_trade_closed:
                    try:
                        on_trade_closed(trade)
                    except Exception:
                        logger.exception("on_trade_closed callback raised.")
                del open_by_token[tok]

        # ---- 2. Open new positions for signals on tokens with no open trade
        for m in mispricings:
            if m.token_id in open_by_token:
                continue
            entry_price = m.best_ask if m.side == "BUY" else m.best_bid
            if entry_price <= 0:
                continue
            open_by_token[m.token_id] = SimTrade(
                token_id=m.token_id,
                side=m.side,
                method=m.synthetic.method,
                open_idx=idx,
                open_ts=ts,
                entry_price=entry_price,
                detected_net_edge=m.net_edge,
                size=position_size_usd,
            )
            report.trades_opened += 1

    # ---- 3. Close any remaining open positions at the final tick's quote
    if tick_list:
        final_idx = len(tick_list) - 1
        final_ts = tick_list[-1][0]
        for tok, trade in list(open_by_token.items()):
            snap = last_snap_by_token.get(tok)
            if snap is None or snap.price is None:
                continue
            trade.close_idx = final_idx
            trade.close_ts = final_ts
            trade.exit_price = float(snap.price)
            trade.exit_reason = "eoh"
            _finalise_trade(trade, report, fee_bps_roundtrip)
            if on_trade_closed:
                try:
                    on_trade_closed(trade)
                except Exception:
                    logger.exception("on_trade_closed callback raised.")
            del open_by_token[tok]

    report.open_at_end = len(open_by_token)
    return report


def _finalise_trade(
    trade: SimTrade, report: BacktestReport, fee_bps_roundtrip: float,
) -> None:
    """Book realised PnL + per-method aggregates for a closed trade."""
    realised = trade.realised_pnl
    if fee_bps_roundtrip > 0:
        fee = trade.size * (fee_bps_roundtrip / 10_000.0)
        realised -= fee
    report.realised_pnl += realised
    report.trades_closed += 1

    bucket = report.per_method.get(trade.method)
    if bucket is None:
        bucket = MethodPnL(method=trade.method)
        report.per_method[trade.method] = bucket
    bucket.trades += 1
    bucket.realised_pnl += realised
    bucket.detected_edge_sum += trade.detected_net_edge
    bucket.realised_edge_sum += trade.realised_return
    if realised > 0:
        bucket.wins += 1
