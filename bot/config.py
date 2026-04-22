"""
Centralized configuration — all settings from environment variables.

Every tunable knob lives here.  Import ``cfg`` from this module
everywhere; never read os.environ in strategy code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from dotenv import load_dotenv

_ENV = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV)


def _e(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _ef(key: str, default: float = 0.0) -> float:
    return float(_e(key, str(default)))


def _ei(key: str, default: int = 0) -> int:
    return int(_e(key, str(default)))


def _eb(key: str, default: bool = False) -> bool:
    return _e(key, str(default)).lower() in ("true", "1", "yes")


@dataclass(frozen=True)
class BotConfig:
    """Immutable bot configuration loaded once at startup."""

    # -- Mode / safety -------------------------------------------------------
    trading_mode: str = field(default_factory=lambda: _e("TRADING_MODE", "paper"))
    allow_live_trading: bool = field(default_factory=lambda: _eb("ALLOW_LIVE_TRADING"))
    i_understand_real_money: str = field(default_factory=lambda: _e("I_UNDERSTAND_REAL_MONEY", ""))
    LIVE_PHRASE: ClassVar[str] = "YES_TRADE_REAL_FUNDS"

    @property
    def is_paper(self) -> bool:
        return self.trading_mode.lower() == "paper"

    @property
    def is_live(self) -> bool:
        return (
            self.trading_mode.lower() == "live"
            and self.allow_live_trading
            and self.i_understand_real_money == self.LIVE_PHRASE
        )

    # -- Polymarket -----------------------------------------------------------
    clob_url: str = field(default_factory=lambda: _e("POLYMARKET_CLOB_URL", "https://clob.polymarket.com"))
    gamma_url: str = field(default_factory=lambda: _e("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"))
    chain_id: int = field(default_factory=lambda: _ei("CHAIN_ID", 137))
    private_key: str = field(default_factory=lambda: _e("PRIVATE_KEY"))
    api_key: str = field(default_factory=lambda: _e("POLY_API_KEY"))
    api_secret: str = field(default_factory=lambda: _e("POLY_API_SECRET"))
    passphrase: str = field(default_factory=lambda: _e("POLY_PASSPHRASE"))

    # -- Claude / Anthropic ---------------------------------------------------
    anthropic_api_key: str = field(default_factory=lambda: _e("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _e("CLAUDE_MODEL", "claude-sonnet-4-20250514"))
    claude_max_tokens: int = field(default_factory=lambda: _ei("CLAUDE_MAX_TOKENS", 2048))
    claude_temperature: float = field(default_factory=lambda: _ef("CLAUDE_TEMPERATURE", 0.2))

    # -- Strategy toggles ----------------------------------------------------
    strategy_probability_arb: bool = field(default_factory=lambda: _eb("STRATEGY_PROBABILITY_ARB", True))
    strategy_logical_arb: bool = field(default_factory=lambda: _eb("STRATEGY_LOGICAL_ARB", True))
    strategy_market_making: bool = field(default_factory=lambda: _eb("STRATEGY_MARKET_MAKING", False))

    # -- Probability arbitrage ------------------------------------------------
    prob_arb_scan_interval_s: int = field(default_factory=lambda: _ei("PROB_ARB_SCAN_INTERVAL", 120))
    prob_arb_min_volume: float = field(default_factory=lambda: _ef("PROB_ARB_MIN_VOLUME", 5000.0))
    prob_arb_min_edge_pct: float = field(default_factory=lambda: _ef("PROB_ARB_MIN_EDGE_PCT", 0.05))
    prob_arb_fee_pct: float = field(default_factory=lambda: _ef("PROB_ARB_FEE_PCT", 0.02))
    prob_arb_min_confidence: float = field(default_factory=lambda: _ef("PROB_ARB_MIN_CONFIDENCE", 0.6))
    prob_arb_max_markets_per_scan: int = field(default_factory=lambda: _ei("PROB_ARB_MAX_MARKETS_PER_SCAN", 15))

    # -- Logical arbitrage ----------------------------------------------------
    logical_arb_scan_interval_s: int = field(default_factory=lambda: _ei("LOGICAL_ARB_SCAN_INTERVAL", 180))
    logical_arb_min_edge_pct: float = field(default_factory=lambda: _ef("LOGICAL_ARB_MIN_EDGE_PCT", 0.03))
    logical_arb_max_groups: int = field(default_factory=lambda: _ei("LOGICAL_ARB_MAX_GROUPS", 10))

    # -- Market making -------------------------------------------------------
    mm_scan_interval_s: int = field(default_factory=lambda: _ei("MM_SCAN_INTERVAL", 30))
    mm_base_spread_pct: float = field(default_factory=lambda: _ef("MM_BASE_SPREAD_PCT", 0.04))
    mm_max_spread_pct: float = field(default_factory=lambda: _ef("MM_MAX_SPREAD_PCT", 0.12))
    mm_max_inventory_pct: float = field(default_factory=lambda: _ef("MM_MAX_INVENTORY_PCT", 0.30))
    mm_order_size_usd: float = field(default_factory=lambda: _ef("MM_ORDER_SIZE_USD", 10.0))
    mm_max_markets: int = field(default_factory=lambda: _ei("MM_MAX_MARKETS", 3))
    mm_rebalance_threshold: float = field(default_factory=lambda: _ef("MM_REBALANCE_THRESHOLD", 0.20))

    # -- Risk / sizing -------------------------------------------------------
    kelly_fraction: float = field(default_factory=lambda: _ef("KELLY_FRACTION", 0.5))
    max_position_usd: float = field(default_factory=lambda: _ef("MAX_POSITION_USD", 100.0))
    max_total_exposure_usd: float = field(default_factory=lambda: _ef("MAX_TOTAL_EXPOSURE_USD", 500.0))
    max_daily_loss_usd: float = field(default_factory=lambda: _ef("MAX_DAILY_LOSS_USD", 50.0))
    max_drawdown_pct: float = field(default_factory=lambda: _ef("MAX_DRAWDOWN_PCT", 0.20))
    starting_capital_usd: float = field(default_factory=lambda: _ef("STARTING_CAPITAL_USD", 1000.0))
    max_positions: int = field(default_factory=lambda: _ei("MAX_POSITIONS", 10))
    max_concentration_pct: float = field(default_factory=lambda: _ef("MAX_CONCENTRATION_PCT", 0.25))
    strategy_daily_loss_limit_usd: float = field(default_factory=lambda: _ef("STRATEGY_DAILY_LOSS_LIMIT_USD", 25.0))

    # -- Position exit -------------------------------------------------------
    stop_loss_pct: float = field(default_factory=lambda: _ef("STOP_LOSS_PCT", 0.15))
    take_profit_pct: float = field(default_factory=lambda: _ef("TAKE_PROFIT_PCT", 0.25))
    max_hold_hours: float = field(default_factory=lambda: _ef("MAX_HOLD_HOURS", 72.0))

    # -- Calibration gate (for live mode) ------------------------------------
    min_resolved_estimates: int = field(default_factory=lambda: _ei("MIN_RESOLVED_ESTIMATES", 50))
    max_brier_score: float = field(default_factory=lambda: _ef("MAX_BRIER_SCORE", 0.25))

    # -- Claude API budget ---------------------------------------------------
    claude_daily_budget_usd: float = field(default_factory=lambda: _ef("CLAUDE_DAILY_BUDGET_USD", 10.0))

    # -- Notifications (stubs) -----------------------------------------------
    telegram_token: str = field(default_factory=lambda: _e("TELEGRAM_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _e("TELEGRAM_CHAT_ID"))
    discord_webhook_url: str = field(default_factory=lambda: _e("DISCORD_WEBHOOK_URL"))

    # -- Logging --------------------------------------------------------------
    log_level: str = field(default_factory=lambda: _e("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: _e("LOG_FILE", "logs/bot.log"))

    # -- Healthcheck server --------------------------------------------------
    healthcheck_enabled: bool = field(default_factory=lambda: _eb("HEALTHCHECK_ENABLED", True))
    healthcheck_host: str = field(default_factory=lambda: _e("HEALTHCHECK_HOST", "127.0.0.1"))
    healthcheck_port: int = field(default_factory=lambda: _ei("HEALTHCHECK_PORT", 8787))


cfg = BotConfig()
