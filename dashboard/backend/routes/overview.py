"""Overview / Home endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["overview"])


@router.get("/overview")
def get_overview():
    from ..main import get_db
    db = get_db()

    total = db.query_one("SELECT COUNT(*) as cnt FROM trades") or {"cnt": 0}

    # Pair BUY/SELL trades by token to compute wins/losses
    buys = db.query(
        "SELECT token_id, condition_id, price, size, strategy, timestamp "
        "FROM trades WHERE side='BUY' ORDER BY timestamp"
    )
    sells = db.query(
        "SELECT token_id, price, size, timestamp "
        "FROM trades WHERE side='SELL' ORDER BY timestamp"
    )
    sell_map: dict[str, list[dict]] = {}
    for s in sells:
        sell_map.setdefault(s["token_id"], []).append(s)

    wins = 0
    losses = 0
    total_pnl = 0.0
    closed_trades = 0
    open_positions = set()

    for b in buys:
        tid = b["token_id"]
        if sell_map.get(tid):
            s = sell_map[tid].pop(0)
            pnl = (s["price"] - b["price"]) * min(b["size"], s["size"])
            total_pnl += pnl
            closed_trades += 1
            if pnl >= 0:
                wins += 1
            else:
                losses += 1
        else:
            open_positions.add(tid)

    # Daily PnL (approximate: sum of today's closed trades)
    daily = db.query_one(
        "SELECT COALESCE(SUM(CASE WHEN side='SELL' THEN price*size ELSE -price*size END), 0) as pnl "
        "FROM trades WHERE date(timestamp) = date('now')"
    ) or {"pnl": 0}

    # Exposure: open BUYs not matched
    exposure_row = db.query_one(
        "SELECT COALESCE(SUM(price * size), 0) as exp FROM trades WHERE side='BUY' AND token_id IN "
        f"({','.join('?' for _ in open_positions)})",
        tuple(open_positions),
    ) if open_positions else {"exp": 0}

    last_trade = db.query_one("SELECT timestamp FROM trades ORDER BY id DESC LIMIT 1")
    markets_count = db.query_one("SELECT COUNT(DISTINCT condition_id) as cnt FROM markets_cache") or {"cnt": 0}

    # Paper "balance" is a synthetic figure for paper mode only.  We
    # derive both the starting balance and the realised+fees breakdown
    # from the bot's live state snapshot rather than hardcoding $1000.
    from ..bot_state import get_paper_starting_balance, get_state
    state = get_state()
    starting_balance = get_paper_starting_balance()
    portfolio_state = state.get("portfolio", {})
    fees_paid = float(portfolio_state.get("fees_paid", 0.0))
    paper_friction_paid = float(portfolio_state.get("paper_friction_paid", 0.0))

    return {
        "bot_active": True,
        "last_update": last_trade["timestamp"] if last_trade else None,
        "total_trades": total["cnt"],
        "winning_trades": wins,
        "losing_trades": losses,
        "total_pnl": round(total_pnl, 4),
        "daily_pnl": round(daily["pnl"], 4),
        "current_exposure": round(exposure_row["exp"], 4) if exposure_row else 0,
        "simulated_balance": round(starting_balance + total_pnl - fees_paid - paper_friction_paid, 4),
        "starting_balance": round(starting_balance, 2),
        "fees_paid": round(fees_paid, 4),
        "paper_friction_paid": round(paper_friction_paid, 4),
        "net_pnl_after_costs": round(total_pnl - fees_paid - paper_friction_paid, 4),
        "markets_monitored": markets_count["cnt"],
        "open_positions": len(open_positions),
    }
