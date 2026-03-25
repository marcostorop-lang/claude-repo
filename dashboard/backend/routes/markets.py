"""Markets analysis endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Query

router = APIRouter(tags=["markets"])


@router.get("/markets")
def get_markets(search: str | None = Query(None)):
    from ..main import get_db
    db = get_db()

    where = ""
    params: tuple = ()
    if search:
        where = "WHERE m.question LIKE ?"
        params = (f"%{search}%",)

    rows = db.query(
        f"""
        SELECT
            m.condition_id,
            m.question,
            COUNT(t.id) as total_trades,
            COALESCE(SUM(CASE WHEN t.side='SELL' THEN t.price * t.size ELSE 0 END)
                   - SUM(CASE WHEN t.side='BUY' THEN t.price * t.size ELSE 0 END), 0) as pnl,
            GROUP_CONCAT(DISTINCT t.strategy) as strategies
        FROM markets_cache m
        LEFT JOIN trades t ON m.condition_id = t.condition_id
        {where}
        GROUP BY m.condition_id, m.question
        ORDER BY total_trades DESC
        """,
        params,
    )

    result = []
    for r in rows:
        # Compute win rate per market
        buys = db.query(
            "SELECT price, size, token_id FROM trades WHERE condition_id = ? AND side='BUY' ORDER BY timestamp",
            (r["condition_id"],),
        )
        sells = db.query(
            "SELECT price, size, token_id FROM trades WHERE condition_id = ? AND side='SELL' ORDER BY timestamp",
            (r["condition_id"],),
        )
        sell_map: dict[str, list] = {}
        for s in sells:
            sell_map.setdefault(s["token_id"], []).append(s)

        wins = 0
        total_closed = 0
        best_strat_pnl: dict[str, float] = {}
        for b in buys:
            if sell_map.get(b["token_id"]):
                s = sell_map[b["token_id"]].pop(0)
                p = (s["price"] - b["price"]) * min(b["size"], s["size"])
                total_closed += 1
                if p >= 0:
                    wins += 1

        result.append({
            "condition_id": r["condition_id"],
            "question": r["question"],
            "total_trades": r["total_trades"],
            "pnl": round(r["pnl"], 4),
            "win_rate": round(wins / total_closed * 100, 2) if total_closed else 0,
            "strategies": r["strategies"] or "",
        })

    return {"markets": result}
