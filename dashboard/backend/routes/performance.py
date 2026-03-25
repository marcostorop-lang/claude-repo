"""Performance / analytics endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["performance"])


def _compute_performance(trades: list[dict]) -> dict:
    """Compute performance metrics from paired BUY/SELL trades."""
    buys_by_token: dict[str, list[dict]] = {}
    for t in trades:
        if t["side"] == "BUY":
            buys_by_token.setdefault(t["token_id"], []).append(t)

    closed: list[dict] = []
    for t in trades:
        if t["side"] == "SELL" and buys_by_token.get(t["token_id"]):
            buy = buys_by_token[t["token_id"]].pop(0)
            size = min(buy["size"], t["size"])
            pnl = (t["price"] - buy["price"]) * size
            closed.append({
                "pnl": pnl,
                "entry_price": buy["price"],
                "exit_price": t["price"],
                "size": size,
                "entry_time": buy["timestamp"],
                "exit_time": t["timestamp"],
                "strategy": buy["strategy"],
                "token_id": t["token_id"],
                "condition_id": buy.get("condition_id", ""),
            })

    if not closed:
        return {
            "equity_curve": [],
            "daily_pnl": [],
            "win_rate": 0,
            "profit_factor": 0,
            "max_drawdown": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "reward_risk_ratio": 0,
            "total_closed": 0,
            "total_pnl": 0,
            "winning": 0,
            "losing": 0,
        }

    wins = [c for c in closed if c["pnl"] >= 0]
    losses_list = [c for c in closed if c["pnl"] < 0]

    total_win = sum(c["pnl"] for c in wins)
    total_loss = abs(sum(c["pnl"] for c in losses_list))

    avg_win = total_win / len(wins) if wins else 0
    avg_loss = total_loss / len(losses_list) if losses_list else 0

    # Equity curve
    equity = []
    cum = 0.0
    for c in sorted(closed, key=lambda x: x["exit_time"]):
        cum += c["pnl"]
        equity.append({"time": c["exit_time"], "equity": round(cum, 4)})

    # Daily PnL
    daily: dict[str, float] = {}
    for c in closed:
        day = c["exit_time"][:10]
        daily[day] = daily.get(day, 0) + c["pnl"]
    daily_pnl = [{"date": k, "pnl": round(v, 4)} for k, v in sorted(daily.items())]

    # Max drawdown
    peak = 0.0
    max_dd = 0.0
    running = 0.0
    for c in sorted(closed, key=lambda x: x["exit_time"]):
        running += c["pnl"]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    return {
        "equity_curve": equity,
        "daily_pnl": daily_pnl,
        "win_rate": round(len(wins) / len(closed) * 100, 2) if closed else 0,
        "profit_factor": round(total_win / total_loss, 4) if total_loss > 0 else float("inf"),
        "max_drawdown": round(max_dd, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "reward_risk_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else 0,
        "total_closed": len(closed),
        "total_pnl": round(sum(c["pnl"] for c in closed), 4),
        "winning": len(wins),
        "losing": len(losses_list),
    }


@router.get("/performance")
def get_performance():
    from ..main import get_db
    db = get_db()
    trades = db.query("SELECT * FROM trades ORDER BY timestamp ASC")
    return _compute_performance(trades)
