"""Read-side helper that loads ``bot_state.json`` for dashboard routes.

The bot writes a snapshot on every tick (see ``src/main.py::_export_bot_state``)
containing the live config and portfolio state.  Dashboard routes consume
it through :func:`get_state` so they never have to hardcode SL/TP, daily
loss limits, or the simulated balance.

Robust to missing / stale files: returns an empty dict so call sites can
do ``state.get("config", {}).get("stop_loss_pct", 0.10)`` and degrade
gracefully when the bot hasn't ticked yet.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# When the dashboard runs in a different CWD than the bot (Docker, k8s,
# systemd unit paths), the operator can override the path explicitly.
_BOT_STATE_PATH = os.getenv(
    "BOT_STATE_PATH",
    str(Path(__file__).resolve().parents[2] / "bot_state.json"),
)


def get_state() -> dict:
    """Return the most recent ``bot_state.json`` snapshot, or {}.

    Always tolerant: file missing, JSON corrupt, or unreadable → returns
    an empty dict.  Caller decides defaults via ``.get(...)``.
    """
    try:
        with open(_BOT_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def get_config(key: str, default):
    """Shorthand: ``get_config("stop_loss_pct", 0.10)`` reads from state."""
    return get_state().get("config", {}).get(key, default)


def get_paper_starting_balance() -> float:
    """Effective starting balance for the *simulated* paper account.

    Until the bot ships a configurable paper bankroll, we derive a sane
    default from the configured exposure cap — a number that's actually
    grounded in the user's risk parameters rather than the historical
    hardcoded $1000.  ``MAX_TOTAL_EXPOSURE * 5`` gives the cap room to
    be a comfortable fraction of the bankroll without forcing operators
    to hand-tune yet another knob.
    """
    cap = get_config("max_total_exposure", 200.0)
    try:
        cap_f = float(cap)
    except (TypeError, ValueError):
        cap_f = 200.0
    return cap_f * 5.0
