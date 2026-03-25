"""Logs endpoint — reads the bot's log file."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(tags=["logs"])


@router.get("/logs")
def get_logs(
    level: str | None = Query(None, description="Filter: INFO, WARNING, ERROR"),
    limit: int = Query(200, ge=1, le=2000),
):
    from ..main import get_log_file
    log_file = get_log_file()

    if not Path(log_file).exists():
        # Return mock logs when no log file exists
        return {"logs": _mock_logs(level, limit)}

    lines = []
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            # Take last N lines
            tail = all_lines[-limit:]
            for raw in tail:
                entry = _parse_log_line(raw.strip())
                if entry and (level is None or entry["level"] == level.upper()):
                    lines.append(entry)
    except Exception:
        pass

    return {"logs": lines}


def _parse_log_line(line: str) -> dict | None:
    """Parse a log line: '2025-01-01 12:00:00 | INFO     | module | message'"""
    parts = line.split(" | ", 3)
    if len(parts) < 4:
        return {"timestamp": "", "level": "INFO", "source": "", "message": line}
    return {
        "timestamp": parts[0].strip(),
        "level": parts[1].strip(),
        "source": parts[2].strip(),
        "message": parts[3].strip(),
    }


def _mock_logs(level: str | None, limit: int) -> list[dict]:
    """Generate mock log entries for demo purposes."""
    from datetime import datetime, timedelta, timezone
    import random

    random.seed(99)
    now = datetime.now(timezone.utc)
    entries = []
    templates = [
        ("INFO", "src.main", "Bot started | mode=PAPER | strategy=simple_momentum | poll=60s"),
        ("INFO", "src.polymarket.market_data", "Fetched 142 raw markets from Gamma API."),
        ("INFO", "src.polymarket.market_data", "After filtering: 23 snapshots from 284 candidates."),
        ("INFO", "src.polymarket.market_data", "Enriched 18 valid market snapshots."),
        ("INFO", "src.main", "Evaluating 18 market snapshots."),
        ("INFO", "src.polymarket.execution", "[PAPER] BUY 25.0000 of tok_001a @ 0.4500 (strategy=simple_momentum)"),
        ("INFO", "src.portfolio.tracker", "Opened position: BUY tok_001a @ 0.4500 (size=25.0000)"),
        ("WARNING", "src.polymarket.client", "Rate limit approaching, slowing down."),
        ("INFO", "src.main", "Stop-loss triggered for tok_003a"),
        ("INFO", "src.polymarket.execution", "[PAPER] SELL 15.0000 of tok_003a @ 0.3200 (strategy=mean_reversion)"),
        ("INFO", "src.main", "Take-profit triggered for tok_002a"),
        ("ERROR", "src.polymarket.client", "Connection timeout fetching order book — retrying (attempt 2/3)."),
        ("INFO", "src.polymarket.client", "Retry successful."),
        ("INFO", "src.main", "Portfolio: {'open_positions': 3, 'total_exposure': 87.50, 'realised_pnl': 12.34}"),
        ("WARNING", "src.risk.manager", "Reduced size to 12.5000 to stay within exposure limit."),
        ("INFO", "src.main", "Sleeping 60 s …"),
    ]

    for i in range(min(limit, 300)):
        lvl, src, msg = random.choice(templates)
        if level and lvl != level.upper():
            continue
        ts = now - timedelta(minutes=i * 2)
        entries.append({
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "level": lvl,
            "source": src,
            "message": msg,
        })

    return entries[:limit]
