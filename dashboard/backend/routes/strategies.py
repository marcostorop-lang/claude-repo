"""Strategies comparison endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["strategies"])


@router.get("/strategies")
def get_strategies():
    from ..main import get_db
    db = get_db()

    strats = db.query("SELECT DISTINCT strategy FROM trades")

    result = []
    for s_row in strats:
        strat = s_row["strategy"]
        trades = db.query(
            "SELECT * FROM trades WHERE strategy = ? ORDER BY timestamp ASC",
            (strat,),
        )

        buys_by_token: dict[str, list] = {}
        for t in trades:
            if t["side"] == "BUY":
                buys_by_token.setdefault(t["token_id"], []).append(t)

        wins = 0
        losses = 0
        total_pnl = 0.0
        pnls: list[float] = []

        for t in trades:
            if t["side"] == "SELL" and buys_by_token.get(t["token_id"]):
                buy = buys_by_token[t["token_id"]].pop(0)
                size = min(buy["size"], t["size"])
                pnl = (t["price"] - buy["price"]) * size
                total_pnl += pnl
                pnls.append(pnl)
                if pnl >= 0:
                    wins += 1
                else:
                    losses += 1

        closed = wins + losses

        # Drawdown
        peak = 0.0
        max_dd = 0.0
        running = 0.0
        for p in pnls:
            running += p
            peak = max(peak, running)
            max_dd = max(max_dd, peak - running)

        result.append({
            "strategy": strat,
            "total_trades": len(trades),
            "closed_trades": closed,
            "winning": wins,
            "losing": losses,
            "win_rate": round(wins / closed * 100, 2) if closed else 0,
            "total_pnl": round(total_pnl, 4),
            "max_drawdown": round(max_dd, 4),
        })

    # Rank by PnL
    result.sort(key=lambda x: x["total_pnl"], reverse=True)
    for i, r in enumerate(result):
        r["rank"] = i + 1

    return {"strategies": result}
