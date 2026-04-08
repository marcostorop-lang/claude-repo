"""
Centralized configuration loaded from environment variables.

All settings are read once at import time. Use ``Config`` as a singleton
throughout the application to avoid re-reading the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (two levels up from this file)
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_float(key: str, default: float = 0.0) -> float:
    return float(_env(key, str(default)))


def _env_int(key: str, default: int = 0) -> int:
    return int(_env(key, str(default)))


def _env_bool(key: str, default: bool = False) -> bool:
    return _env(key, str(default)).lower() in ("true", "1", "yes")


@dataclass(frozen=True)
class Config:
    """Immutable application configuration."""

    # -- Trading mode ----------------------------------------------------------
    trading_mode: str = field(default_factory=lambda: _env("TRADING_MODE", "paper"))
    allow_live_trading: bool = field(default_factory=lambda: _env_bool("ALLOW_LIVE_TRADING"))

    # -- Polymarket endpoints --------------------------------------------------
    clob_url: str = field(default_factory=lambda: _env("POLYMARKET_CLOB_URL", "https://clob.polymarket.com"))
    gamma_url: str = field(default_factory=lambda: _env("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"))
    chain_id: int = field(default_factory=lambda: _env_int("CHAIN_ID", 137))

    # -- Credentials (only used in live mode) ----------------------------------
    private_key: str = field(default_factory=lambda: _env("PRIVATE_KEY"))
    api_key: str = field(default_factory=lambda: _env("POLY_API_KEY"))
    api_secret: str = field(default_factory=lambda: _env("POLY_API_SECRET"))
    passphrase: str = field(default_factory=lambda: _env("POLY_PASSPHRASE"))

    # -- Market filters --------------------------------------------------------
    min_volume: float = field(default_factory=lambda: _env_float("MIN_VOLUME", 1000))
    min_liquidity: float = field(default_factory=lambda: _env_float("MIN_LIQUIDITY", 500))
    max_spread: float = field(default_factory=lambda: _env_float("MAX_SPREAD", 0.15))
    max_markets: int = field(default_factory=lambda: _env_int("MAX_MARKETS", 20))

    # -- Risk management -------------------------------------------------------
    max_position_size: float = field(default_factory=lambda: _env_float("MAX_POSITION_SIZE", 50.0))
    max_total_exposure: float = field(default_factory=lambda: _env_float("MAX_TOTAL_EXPOSURE", 200.0))
    stop_loss_pct: float = field(default_factory=lambda: _env_float("STOP_LOSS_PCT", 0.10))
    take_profit_pct: float = field(default_factory=lambda: _env_float("TAKE_PROFIT_PCT", 0.20))
    max_open_positions: int = field(default_factory=lambda: _env_int("MAX_OPEN_POSITIONS", 5))

    # -- Strategy --------------------------------------------------------------
    strategy: str = field(default_factory=lambda: _env("STRATEGY", "simple_momentum"))
    momentum_window: int = field(default_factory=lambda: _env_int("MOMENTUM_WINDOW", 5))
    momentum_threshold: float = field(default_factory=lambda: _env_float("MOMENTUM_THRESHOLD", 0.03))
    mean_reversion_window: int = field(default_factory=lambda: _env_int("MEAN_REVERSION_WINDOW", 10))
    mean_reversion_entry_z: float = field(default_factory=lambda: _env_float("MEAN_REVERSION_ENTRY_Z", 1.5))
    mean_reversion_exit_z: float = field(default_factory=lambda: _env_float("MEAN_REVERSION_EXIT_Z", 0.5))

    # -- Bot loop --------------------------------------------------------------
    poll_interval: int = field(default_factory=lambda: _env_int("POLL_INTERVAL_SECONDS", 60))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: _env("LOG_FILE", "bot.log"))

    # -- Storage ---------------------------------------------------------------
    sqlite_db_path: str = field(default_factory=lambda: _env("SQLITE_DB_PATH", "polymarket_bot.db"))

    # -- Market fetch limit (to avoid downloading all 50K+ markets per tick) ---
    max_markets_fetch: int = field(default_factory=lambda: _env_int("MAX_MARKETS_FETCH", 500))

    # -- Derived helpers -------------------------------------------------------
    @property
    def is_paper(self) -> bool:
        return self.trading_mode.lower() == "paper"

    @property
    def is_live(self) -> bool:
        return (
            self.trading_mode.lower() == "live"
            and self.allow_live_trading
        )

    def validate(self) -> list[str]:
        """Return a list of configuration problems (empty == OK)."""
        problems: list[str] = []
        if self.trading_mode.lower() == "live" and not self.allow_live_trading:
            problems.append(
                "TRADING_MODE=live but ALLOW_LIVE_TRADING is not true. "
                "Orders will NOT be sent."
            )
        if self.is_live and not self.private_key:
            problems.append("Live trading requires PRIVATE_KEY to be set.")
        if self.is_live and not self.api_key:
            problems.append("Live trading requires POLY_API_KEY to be set.")
        if self.max_position_size <= 0:
            problems.append("MAX_POSITION_SIZE must be > 0.")
        if self.max_total_exposure <= 0:
            problems.append("MAX_TOTAL_EXPOSURE must be > 0.")
        return problems
