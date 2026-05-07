"""Prometheus exposition format endpoint.

The bot historically exposed Prometheus only from ``bot/core/healthcheck.py``;
the canonical ``src/`` path was missing it.  This route surfaces the
metrics any operator would want to scrape from the live SQLite +
``bot_state.json`` snapshot:

* ``bot_total_trades``           (counter-like)
* ``bot_open_positions``         (gauge)
* ``bot_total_exposure_usd``     (gauge)
* ``bot_daily_pnl_usd``          (gauge)
* ``bot_realised_pnl_usd``       (gauge — cumulative)
* ``bot_fees_paid_usd``          (gauge)
* ``bot_paper_friction_paid_usd``(gauge)
* ``bot_drawdown_pct``           (gauge, 0..1)
* ``bot_circuit_breaker_active`` (gauge, 0/1)
* ``bot_drawdown_breaker_active``(gauge, 0/1)
* ``bot_last_tick_age_seconds``  (gauge)

Format: text/plain following Prometheus exposition spec.  No
external dependency — we synthesise the lines ourselves to keep the
dashboard's footprint identical.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["metrics"])


def _fmt(name: str, value: float, help_text: str, type_: str = "gauge") -> list[str]:
    return [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} {type_}",
        f"{name} {value}",
    ]


def _last_tick_age_seconds(state: dict) -> float:
    ts = state.get("timestamp")
    if not ts:
        return -1.0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, time.time() - dt.timestamp())
    except (ValueError, AttributeError):
        return -1.0


@router.get("/metrics", response_class=PlainTextResponse)
def metrics():
    from ..main import get_db
    from ..bot_state import get_state
    db = get_db()

    total_row = db.query_one("SELECT COUNT(*) as cnt FROM trades") or {"cnt": 0}
    state = get_state()
    portfolio_state = state.get("portfolio", {})

    lines: list[str] = []
    lines += _fmt(
        "bot_total_trades", float(total_row["cnt"]),
        "Total trades recorded in SQLite.", "counter",
    )
    lines += _fmt(
        "bot_open_positions", float(portfolio_state.get("open_positions", 0)),
        "Open positions in the bot's tracker.",
    )
    lines += _fmt(
        "bot_total_exposure_usd", float(portfolio_state.get("total_exposure", 0.0)),
        "Total dollar exposure of open positions.",
    )
    lines += _fmt(
        "bot_daily_pnl_usd", float(state.get("daily_pnl", 0.0)),
        "Realised PnL accrued today (UTC day).",
    )
    lines += _fmt(
        "bot_realised_pnl_usd", float(portfolio_state.get("realised_pnl", 0.0)),
        "Cumulative realised PnL since the bot started.",
    )
    lines += _fmt(
        "bot_fees_paid_usd", float(portfolio_state.get("fees_paid", 0.0)),
        "Cumulative protocol fees paid (paper or live).",
    )
    lines += _fmt(
        "bot_paper_friction_paid_usd",
        float(portfolio_state.get("paper_friction_paid", 0.0)),
        "Cumulative simulated paper-trade execution friction.",
    )
    lines += _fmt(
        "bot_drawdown_pct", float(state.get("drawdown_pct", 0.0)),
        "Current drawdown from peak equity (0..1).",
    )
    lines += _fmt(
        "bot_circuit_breaker_active",
        1.0 if state.get("circuit_breaker_active") else 0.0,
        "Daily-loss circuit breaker active (1=tripped).",
    )
    lines += _fmt(
        "bot_drawdown_breaker_active",
        1.0 if state.get("drawdown_breaker_active") else 0.0,
        "Drawdown-from-peak breaker active (1=tripped).",
    )
    lines += _fmt(
        "bot_last_tick_age_seconds", _last_tick_age_seconds(state),
        "Seconds since the last tick wrote bot_state.json (-1 if unknown).",
    )

    return "\n".join(lines) + "\n"
