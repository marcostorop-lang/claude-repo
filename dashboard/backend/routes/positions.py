"""Open positions endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["positions"])


@router.get("/positions")
def get_positions():
    from ..main import get_db
    db = get_db()

    buys = db.query("SELECT * FROM trades WHERE side='BUY' ORDER BY timestamp ASC")
    sells = db.query("SELECT * FROM trades WHERE side='SELL' ORDER BY timestamp ASC")

    sell_map: dict[str, list[dict]] = {}
    for s in sells:
        sell_map.setdefault(s["token_id"], []).append(s)

    open_positions: list[dict] = []
    for b in buys:
        tid = b["token_id"]
        if sell_map.get(tid):
            sell_map[tid].pop(0)
        else:
            # Get latest price
            latest = db.query_one(
                "SELECT price FROM price_history WHERE token_id = ? ORDER BY id DESC LIMIT 1",
                (tid,),
            )
            current_price = latest["price"] if latest else b["price"]
            unrealised = (current_price - b["price"]) * b["size"]

            # Get market question
            market = db.query_one(
                "SELECT question FROM markets_cache WHERE condition_id = ?",
                (b["condition_id"],),
            )

            # Compute SL/TP distances (using config defaults)
            sl_pct = 0.10
            tp_pct = 0.20
            sl_price = b["price"] * (1 - sl_pct)
            tp_price = b["price"] * (1 + tp_pct)
            dist_to_sl = ((current_price - sl_price) / (b["price"] - sl_price) * 100) if b["price"] != sl_price else 100
            dist_to_tp = ((tp_price - current_price) / (tp_price - b["price"]) * 100) if tp_price != b["price"] else 100

            open_positions.append({
                "token_id": tid,
                "condition_id": b["condition_id"],
                "question": market["question"] if market else "",
                "side": b["side"],
                "entry_price": b["price"],
                "current_price": round(current_price, 4),
                "size": b["size"],
                "unrealised_pnl": round(unrealised, 4),
                "entry_time": b["timestamp"],
                "strategy": b["strategy"],
                "pct_to_stop_loss": round(max(0, min(100, dist_to_sl)), 2),
                "pct_to_take_profit": round(max(0, min(100, dist_to_tp)), 2),
            })

    return {"positions": open_positions}
