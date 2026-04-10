"""
Analyze currently open positions.

Reconstructs the portfolio from the trade history, fetches current prices
(best-effort — uses the midpoint), and produces a per-position report with:

- entry time and time-in-position
- entry price, current price, unrealised PnL (absolute and %)
- distance from stop-loss and take-profit thresholds
- a rough "status" label (healthy / warning / near_stop)

This is intended to run on an existing DB that was carried over from an
older version of the bot, but it works on a fresh DB too.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config import Config
from src.polymarket.client import PolymarketClient
from src.portfolio.tracker import PortfolioTracker
from src.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def analyze_open_positions(store: SQLiteStore, client: PolymarketClient) -> list[dict]:
    """Return a list of dicts describing each currently open position.

    Uses the trade history to reconstruct positions (so it survives restarts)
    and then fetches the current midpoint from the CLOB for PnL calculation.
    """
    cfg = Config()
    portfolio = PortfolioTracker()
    trades = store.get_all_trades()
    if not trades:
        return []
    portfolio.reconstruct_from_trades(trades)

    # Build a quick lookup of the BUY trade for each open position so we can
    # grab the original timestamp (reconstruct_from_trades does not preserve it).
    entry_ts_by_token: dict[str, str] = {}
    for t in trades:
        if t["side"] == "BUY":
            entry_ts_by_token[t["token_id"]] = t["timestamp"]
        elif t["side"] == "SELL" and t["token_id"] in entry_ts_by_token:
            entry_ts_by_token.pop(t["token_id"], None)

    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    for tok, pos in portfolio.positions.items():
        current_price = None
        try:
            current_price = client.get_price(tok)
        except Exception:
            logger.debug("Price fetch failed for %s", tok[:12], exc_info=True)

        entry_ts = entry_ts_by_token.get(tok, "")
        entry_dt = _parse_iso(entry_ts)
        age_hours = None
        if entry_dt is not None:
            age = now - entry_dt
            age_hours = round(age.total_seconds() / 3600.0, 2)

        if current_price is not None and pos.entry_price > 0:
            upnl_abs = (current_price - pos.entry_price) * pos.size
            upnl_pct = (current_price - pos.entry_price) / pos.entry_price
            sl_distance = cfg.stop_loss_pct + upnl_pct  # positive = room left; negative = past SL
            tp_distance = cfg.take_profit_pct - upnl_pct
            if upnl_pct <= -cfg.stop_loss_pct * 0.8:
                status = "near_stop"
            elif upnl_pct <= -cfg.stop_loss_pct * 0.5:
                status = "warning"
            elif upnl_pct >= cfg.take_profit_pct * 0.8:
                status = "near_target"
            else:
                status = "healthy"
        else:
            upnl_abs = None
            upnl_pct = None
            sl_distance = None
            tp_distance = None
            status = "no_price"

        rows.append({
            "token": tok[:12],
            "condition": pos.condition_id[:12],
            "strategy": pos.strategy,
            "size": round(pos.size, 4),
            "entry_price": round(pos.entry_price, 4),
            "current": round(current_price, 4) if current_price is not None else None,
            "upnl_$": round(upnl_abs, 4) if upnl_abs is not None else None,
            "upnl_%": round(upnl_pct * 100, 2) if upnl_pct is not None else None,
            "sl_buffer_%": round(sl_distance * 100, 2) if sl_distance is not None else None,
            "tp_buffer_%": round(tp_distance * 100, 2) if tp_distance is not None else None,
            "age_h": age_hours,
            "status": status,
        })
    return rows
