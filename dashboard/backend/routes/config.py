"""Config endpoint — read-only view of bot configuration (no secrets)."""

from __future__ import annotations

import os

from fastapi import APIRouter

router = APIRouter(tags=["config"])

# Env vars safe to expose (no secrets)
_SAFE_KEYS = [
    ("TRADING_MODE", "paper"),
    ("ALLOW_LIVE_TRADING", "false"),
    ("POLL_INTERVAL_SECONDS", "60"),
    ("STRATEGY", "simple_momentum"),
    ("MIN_VOLUME", "1000"),
    ("MIN_LIQUIDITY", "500"),
    ("MAX_SPREAD", "0.15"),
    ("MAX_MARKETS", "20"),
    ("MAX_POSITION_SIZE", "50"),
    ("MAX_TOTAL_EXPOSURE", "200"),
    ("STOP_LOSS_PCT", "0.10"),
    ("TAKE_PROFIT_PCT", "0.20"),
    ("MAX_OPEN_POSITIONS", "5"),
    ("MOMENTUM_WINDOW", "5"),
    ("MOMENTUM_THRESHOLD", "0.03"),
    ("MEAN_REVERSION_WINDOW", "10"),
    ("MEAN_REVERSION_ENTRY_Z", "1.5"),
    ("MEAN_REVERSION_EXIT_Z", "0.5"),
    ("LOG_LEVEL", "INFO"),
]


@router.get("/config")
def get_config():
    config = {}
    for key, default in _SAFE_KEYS:
        config[key] = os.getenv(key, default)
    return {"config": config}
