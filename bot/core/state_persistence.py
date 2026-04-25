"""State persistence — save and restore bot state across restarts.

On graceful shutdown, the bot saves positions, equity, PnL, and cycle
count to a JSON file.  On startup, it restores from this file so the
bot picks up where it left off without losing track of open positions.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).resolve().parent.parent / "logs" / "bot_state.json"


def save_state(
    *,
    positions: list[dict],
    equity: float,
    total_pnl: float,
    daily_pnl: float,
    cycle_count: int,
    peak_equity: float,
    strategy_pnl: dict[str, float] | None = None,
    extra: dict | None = None,
) -> bool:
    """Persist bot state to disk. Returns True on success."""
    state = {
        "saved_at": time.time(),
        "positions": positions,
        "equity": equity,
        "total_pnl": total_pnl,
        "daily_pnl": daily_pnl,
        "cycle_count": cycle_count,
        "peak_equity": peak_equity,
        "strategy_pnl": strategy_pnl or {},
    }
    if extra:
        state["extra"] = extra

    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        tmp.replace(_STATE_FILE)
        logger.info("State saved: equity=$%.2f, %d positions, cycle=%d",
                     equity, len(positions), cycle_count)
        return True
    except Exception:
        logger.exception("Failed to save state.")
        return False


def load_state() -> dict | None:
    """Load previously saved state. Returns None if no state file or invalid."""
    if not _STATE_FILE.exists():
        logger.info("No saved state found — starting fresh.")
        return None

    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        required_keys = {"positions", "equity", "total_pnl", "cycle_count"}
        if not required_keys.issubset(state.keys()):
            logger.warning("State file missing required keys: %s",
                           required_keys - state.keys())
            return None

        age_s = time.time() - state.get("saved_at", 0)
        if age_s > 86400 * 7:
            logger.warning("State file is %.1f days old — ignoring.", age_s / 86400)
            return None

        logger.info(
            "Restored state: equity=$%.2f, %d positions, cycle=%d (saved %.0fs ago)",
            state["equity"], len(state["positions"]), state["cycle_count"], age_s,
        )
        return state

    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Failed to parse state file: %s", exc)
        return None
    except Exception:
        logger.exception("Failed to load state file.")
        return None


def clear_state() -> None:
    """Remove the state file (e.g., after successful clean shutdown)."""
    try:
        if _STATE_FILE.exists():
            _STATE_FILE.unlink()
    except Exception:
        pass
