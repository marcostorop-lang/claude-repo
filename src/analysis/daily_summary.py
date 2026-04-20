"""
Daily summary report.

Operators ask one question every morning: *what happened yesterday?*
Without a structured answer they read the raw tick log line-by-line,
or worse, infer from the dashboard's current snapshot (which shows
the wrong thing on the wrong day).  This module is the structured
answer.

One UTC-day window of activity is reduced to a :class:`DailySummary`:

* headline counts — trades (live vs paper), entries vs exits,
* realised P&L for the window (sign-preserving) and the cumulative
  win-rate inside it,
* top-3 winners / top-3 losers by absolute P&L so the operator
  eyeballs which markets drove the day,
* an ``anomalies`` list flagging things that deserve a second look:
  circuit breaker fired, reconciliation divergence, staleness closure,
  wallet rejection, etc. (caller supplies these — the module just
  carries them through).

Pure function: it takes two lists of dicts (trades + closed
calibration rows) and returns the summary.  Day boundary is UTC.  No
I/O here so the same logic drives the CLI ``daily-summary`` command,
a cron-scheduled file writer, and (eventually) an alert sender.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _utc_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


@dataclass(frozen=True)
class DailyMarketPnL:
    token_id: str
    strategy: str
    pnl: float
    return_pct: float


@dataclass
class DailySummary:
    day: str                                   # "YYYY-MM-DD" (UTC)
    trade_count: int = 0
    live_trade_count: int = 0
    paper_trade_count: int = 0
    entries: int = 0
    exits: int = 0
    realised_pnl_usd: float = 0.0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    top_winners: list[DailyMarketPnL] = field(default_factory=list)
    top_losers: list[DailyMarketPnL] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "trade_count": self.trade_count,
            "live_trade_count": self.live_trade_count,
            "paper_trade_count": self.paper_trade_count,
            "entries": self.entries,
            "exits": self.exits,
            "realised_pnl_usd": round(self.realised_pnl_usd, 4),
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "top_winners": [
                {"token_id": w.token_id, "strategy": w.strategy,
                 "pnl": round(w.pnl, 4),
                 "return_pct": round(w.return_pct, 4)}
                for w in self.top_winners
            ],
            "top_losers": [
                {"token_id": l.token_id, "strategy": l.strategy,
                 "pnl": round(l.pnl, 4),
                 "return_pct": round(l.return_pct, 4)}
                for l in self.top_losers
            ],
            "anomalies": list(self.anomalies),
        }


# --- Core reducer --------------------------------------------------------


def build_daily_summary(
    trades: list[dict],
    closed_calibrations: list[dict],
    *,
    day: date | None = None,
    anomalies: list[str] | None = None,
    top_n: int = 3,
) -> DailySummary:
    """Reduce a day's trades + closed calibrations to a :class:`DailySummary`.

    ``day`` defaults to *yesterday* UTC (not today — the daily report
    is a retrospective, not an in-progress running tally).  The
    window is ``[day 00:00, day+1 00:00)`` UTC.

    ``trades`` rows need at minimum: ``timestamp`` (ISO), ``side``,
    ``mode``.  ``closed_calibrations`` need ``exit_timestamp``,
    ``token_id``, ``strategy``, ``pnl``, ``return_pct``.

    Unknown fields are ignored — the caller can pass the raw DB row
    dicts without reshaping them.
    """
    if day is None:
        day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    start, end = _utc_day_bounds(day)

    summary = DailySummary(day=day.isoformat())

    # Trade-level counts (drive the "how active" headline numbers).
    for t in trades:
        ts = _parse_iso(t.get("timestamp") or "")
        if ts is None or not (start <= ts < end):
            continue
        summary.trade_count += 1
        mode = (t.get("mode") or "").lower()
        if mode.startswith("live"):
            summary.live_trade_count += 1
        else:
            # Everything else (paper, paper_resolution, …) counts as
            # paper for the summary — a synthetic resolution is still
            # not a live order.
            summary.paper_trade_count += 1
        side = (t.get("side") or "").upper()
        if side == "BUY":
            summary.entries += 1
        elif side == "SELL":
            summary.exits += 1

    # P&L and winners/losers come from *closed* calibration rows in
    # the window.  A position opened yesterday and closed today belongs
    # to today — we attribute P&L at the exit, not the entry.
    day_pnls: list[DailyMarketPnL] = []
    for c in closed_calibrations:
        ts = _parse_iso(c.get("exit_timestamp") or "")
        if ts is None or not (start <= ts < end):
            continue
        pnl = c.get("pnl")
        if pnl is None:
            continue
        try:
            pnl_f = float(pnl)
        except (TypeError, ValueError):
            continue
        ret = c.get("return_pct")
        try:
            ret_f = float(ret) if ret is not None else 0.0
        except (TypeError, ValueError):
            ret_f = 0.0
        summary.realised_pnl_usd += pnl_f
        if pnl_f > 0:
            summary.wins += 1
        elif pnl_f < 0:
            summary.losses += 1
        day_pnls.append(DailyMarketPnL(
            token_id=str(c.get("token_id") or ""),
            strategy=str(c.get("strategy") or ""),
            pnl=pnl_f,
            return_pct=ret_f,
        ))

    graded = summary.wins + summary.losses
    summary.win_rate = summary.wins / graded if graded > 0 else 0.0

    # Top winners (highest +pnl) / losers (lowest -pnl).
    winners = sorted(
        (p for p in day_pnls if p.pnl > 0), key=lambda x: x.pnl, reverse=True,
    )[:top_n]
    losers = sorted(
        (p for p in day_pnls if p.pnl < 0), key=lambda x: x.pnl,
    )[:top_n]
    summary.top_winners = winners
    summary.top_losers = losers

    if anomalies:
        summary.anomalies = list(anomalies)

    return summary


# --- Formatter -----------------------------------------------------------


def format_summary(s: DailySummary) -> str:
    """Plain-text rendering suitable for the CLI + webhook payload."""
    lines: list[str] = []
    lines.append(f"Daily summary — {s.day} UTC")
    lines.append("=" * 40)
    lines.append(
        f"Trades: {s.trade_count} "
        f"(live={s.live_trade_count}, paper={s.paper_trade_count}) "
        f"| entries={s.entries} exits={s.exits}"
    )
    lines.append(
        f"Realised PnL: ${s.realised_pnl_usd:+.2f} "
        f"| wins={s.wins} losses={s.losses} "
        f"(win_rate={s.win_rate:.1%})"
    )
    if s.top_winners:
        lines.append("")
        lines.append("Top winners:")
        for w in s.top_winners:
            lines.append(
                f"  + ${w.pnl:+.2f} ({w.return_pct:+.1%}) "
                f"{w.token_id[:12]} [{w.strategy}]"
            )
    if s.top_losers:
        lines.append("")
        lines.append("Top losers:")
        for l in s.top_losers:
            lines.append(
                f"  - ${l.pnl:+.2f} ({l.return_pct:+.1%}) "
                f"{l.token_id[:12]} [{l.strategy}]"
            )
    if s.anomalies:
        lines.append("")
        lines.append("Anomalies:")
        for a in s.anomalies:
            lines.append(f"  ! {a}")
    return "\n".join(lines)
