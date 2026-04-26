"""
Centralized configuration loaded from environment variables.

All settings are read once at import time. Use ``Config`` as a singleton
throughout the application to avoid re-reading the environment.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

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


def _env_json_dict(key: str, default: dict | None = None) -> dict:
    """Parse a JSON-dict env var, returning ``default`` on missing or invalid.

    Keeps config loading robust: a malformed override never crashes the
    bot — it's silently ignored and the caller falls back to globals.
    """
    raw = _env(key, "").strip()
    if not raw:
        return dict(default or {})
    try:
        val = json.loads(raw)
        if isinstance(val, dict):
            return val
    except (json.JSONDecodeError, TypeError):
        pass
    return dict(default or {})


@dataclass(frozen=True)
class Config:
    """Immutable application configuration."""

    # -- Trading mode ----------------------------------------------------------
    trading_mode: str = field(default_factory=lambda: _env("TRADING_MODE", "paper"))
    allow_live_trading: bool = field(default_factory=lambda: _env_bool("ALLOW_LIVE_TRADING"))
    # Second gate: live trading requires BOTH ``ALLOW_LIVE_TRADING=true`` AND
    # this variable set to the exact phrase below.  A typo in either one
    # keeps the bot in paper mode.  This is defence-in-depth against
    # accidental production pushes — never remove without replacing with a
    # stronger mechanism.
    i_understand_real_money: str = field(
        default_factory=lambda: _env("I_UNDERSTAND_REAL_MONEY", "")
    )
    # Shadow mode: generates signals, runs risk checks, persists decisions
    # to decision_log, but never calls the executor and never updates
    # portfolio state.  Useful for A/B testing a new strategy config
    # alongside the regular paper run (with a distinct SQLITE_DB_PATH)
    # without contaminating the real paper PnL.  Default: off.
    shadow_mode: bool = field(default_factory=lambda: _env_bool("SHADOW_MODE", False))

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
    # When True (default), a BUY whose snapshot does not carry a positive
    # spread is rejected.  Without this gate, ``MAX_SPREAD`` silently
    # collapses to a no-op for any caller that forgets (or fails) to
    # compute the spread — the exact "looks safe, isn't" failure mode.
    # SELLs are never blocked: we always want to be able to close.
    require_known_spread_for_buy: bool = field(
        default_factory=lambda: _env_bool("REQUIRE_KNOWN_SPREAD_FOR_BUY", True)
    )
    max_markets: int = field(default_factory=lambda: _env_int("MAX_MARKETS", 20))

    # -- Risk management -------------------------------------------------------
    max_position_size: float = field(default_factory=lambda: _env_float("MAX_POSITION_SIZE", 50.0))
    max_total_exposure: float = field(default_factory=lambda: _env_float("MAX_TOTAL_EXPOSURE", 200.0))
    stop_loss_pct: float = field(default_factory=lambda: _env_float("STOP_LOSS_PCT", 0.10))
    take_profit_pct: float = field(default_factory=lambda: _env_float("TAKE_PROFIT_PCT", 0.20))
    max_open_positions: int = field(default_factory=lambda: _env_int("MAX_OPEN_POSITIONS", 5))
    max_daily_loss: float = field(default_factory=lambda: _env_float("MAX_DAILY_LOSS", 50.0))
    max_exposure_per_event: float = field(default_factory=lambda: _env_float("MAX_EXPOSURE_PER_EVENT", 100.0))
    # When True, positions that are opposing sides of the same binary event
    # (BUY Yes + BUY No on the same condition_id, or symmetric shorts) are
    # counted as a *single* slot for ``max_open_positions`` and their
    # notionals are netted for exposure purposes.  Rationale: buying both
    # legs of a Yes/No is a cap-locked position (capital is locked but
    # market-neutral), not two independent risks.  Default off to preserve
    # existing sizing behaviour byte-for-byte; flip on when running
    # neg-risk / semantic strategies that frequently hold multiple legs.
    net_paired_legs: bool = field(default_factory=lambda: _env_bool("NET_PAIRED_LEGS", False))
    # Cross-event concentration: max total exposure in one category (e.g. "politics")
    max_exposure_per_category: float = field(default_factory=lambda: _env_float("MAX_EXPOSURE_PER_CATEGORY", 150.0))
    max_positions_per_category: int = field(default_factory=lambda: _env_int("MAX_POSITIONS_PER_CATEGORY", 3))

    # -- Price filters ---------------------------------------------------------
    min_price: float = field(default_factory=lambda: _env_float("MIN_PRICE", 0.05))
    max_price: float = field(default_factory=lambda: _env_float("MAX_PRICE", 0.95))
    stale_price_seconds: int = field(default_factory=lambda: _env_int("STALE_PRICE_SECONDS", 300))

    # -- Strategy --------------------------------------------------------------
    strategy: str = field(default_factory=lambda: _env("STRATEGY", "simple_momentum"))
    # Defaults tuned for faster warmup on a cold DB (was 5/0.03 which required
    # 5+ ticks of history per token before any signal could fire).
    momentum_window: int = field(default_factory=lambda: _env_int("MOMENTUM_WINDOW", 3))
    momentum_threshold: float = field(default_factory=lambda: _env_float("MOMENTUM_THRESHOLD", 0.02))
    mean_reversion_window: int = field(default_factory=lambda: _env_int("MEAN_REVERSION_WINDOW", 10))
    mean_reversion_entry_z: float = field(default_factory=lambda: _env_float("MEAN_REVERSION_ENTRY_Z", 1.5))
    mean_reversion_exit_z: float = field(default_factory=lambda: _env_float("MEAN_REVERSION_EXIT_Z", 0.5))

    # -- Strategy net-edge gate (opt-in) ---------------------------------------
    # Requires the *expected gross move* of a momentum/MR signal to clear
    # half-spread + taker fee + ``strategy_min_net_edge`` before the
    # strategy emits a BUY/SELL.  Without this, a 2% momentum gross-signal
    # in a market with a 4% spread is a guaranteed loser.  The risk
    # manager has its own ``MIN_EDGE_FOR_TRADE`` (used only by edge_based);
    # this gate fixes the same hole for the simpler strategies.  Default
    # off to preserve historical paper PnL byte-for-byte until enabled.
    strategy_net_edge_gate_enabled: bool = field(
        default_factory=lambda: _env_bool("STRATEGY_NET_EDGE_GATE_ENABLED", False)
    )
    strategy_min_net_edge: float = field(
        default_factory=lambda: _env_float("STRATEGY_MIN_NET_EDGE", 0.005)
    )

    # -- Dynamic sizing --------------------------------------------------------
    # Minimum confidence required to open a position (below this → skip)
    min_confidence_for_trade: float = field(default_factory=lambda: _env_float("MIN_CONFIDENCE_FOR_TRADE", 0.0))
    # When True, position size scales linearly with signal confidence:
    #   size = base_size * confidence
    # This means a 0.80 confidence signal takes 80% of max_position_size.
    sizing_confidence_scale: bool = field(default_factory=lambda: _env_bool("SIZING_CONFIDENCE_SCALE", False))
    # Maximum fraction of reported liquidity to consume in a single trade
    max_liquidity_fraction: float = field(default_factory=lambda: _env_float("MAX_LIQUIDITY_FRACTION", 0.02))
    # Maximum fraction of real order-book depth (within 5% of midpoint, USD)
    # to consume in a single trade.  Opt-in: when a BookAnalysis is
    # available at sizing time, the proposed notional is capped at
    # ``max_book_depth_fraction * depth_5pct_usd`` on the fill side.
    # Defaults to 0.0 (disabled) so behaviour is unchanged until an
    # operator opts in.  A conservative production value is 0.25 — take at
    # most a quarter of visible depth to leave headroom for slippage and
    # for the book to fill back in before a second tick fires.
    max_book_depth_fraction: float = field(default_factory=lambda: _env_float("MAX_BOOK_DEPTH_FRACTION", 0.0))
    # Maximum tolerated slippage when walking the book at the requested
    # size: ``slippage_pct`` is (fill_vwap / best_bid_or_ask - 1).  Above
    # this the bot rejects the order rather than buy the wick.  Default
    # 2% mirrors the long-standing hardcoded value that used to live in
    # ``main._tick``; lifting it to config makes it tunable without
    # editing source.
    max_book_slippage_pct: float = field(
        default_factory=lambda: _env_float("MAX_BOOK_SLIPPAGE_PCT", 0.02)
    )
    # Edge-aware (fractional-Kelly) sizing: when True and a signed edge is
    # supplied by the strategy, scale the position by |edge| * confidence *
    # kelly_fraction.  This makes high-edge + high-confidence trades larger
    # and marginal trades smaller — properly risk-adjusted.
    sizing_edge_kelly: bool = field(default_factory=lambda: _env_bool("SIZING_EDGE_KELLY", False))
    # Kelly fraction — 1.0 is full Kelly (aggressive), 0.25 is quarter-Kelly
    # (conservative and typical for retail).  Applied on top of |edge| * confidence.
    kelly_fraction: float = field(default_factory=lambda: _env_float("KELLY_FRACTION", 0.25))
    # Exact-formula Kelly: f* = (p*b - q)/b with p = price+edge and
    # b = (1-price)/price (for YES shares priced in [0,1]).  Takes
    # precedence over ``sizing_edge_kelly`` when both are True.  Still
    # multiplied by ``kelly_fraction`` and ``confidence`` so retail
    # operators can stay at quarter-Kelly while upgrading the math.
    # Default off — opt in once the edge estimator has been calibrated
    # against realised outcomes (see ``src/analysis/calibration.py``).
    sizing_kelly_proper: bool = field(
        default_factory=lambda: _env_bool("SIZING_KELLY_PROPER", False)
    )
    # Minimum edge magnitude to trade (below this → HOLD regardless of confidence)
    min_edge_for_trade: float = field(default_factory=lambda: _env_float("MIN_EDGE_FOR_TRADE", 0.0))
    # Optional per-category overrides for MIN_EDGE_FOR_TRADE.  Expected as
    # a JSON dict in the env, e.g.
    #   MIN_EDGE_BY_CATEGORY='{"politics": 0.03, "sports": 0.05}'
    # When a snapshot's category matches a key here, that value overrides
    # the global threshold *for that trade only*.  Unmatched categories
    # fall back to the global — so enabling this is strictly additive and
    # reversible.  An empty dict (the default) disables the feature.
    min_edge_by_category: dict = field(default_factory=lambda: _env_json_dict("MIN_EDGE_BY_CATEGORY"))
    # Exit early when the edge model re-evaluates and flips direction against
    # our open position.  Only applies to positions opened by strategies that
    # expose an edge estimate (e.g. edge_based).  Requires |edge| > this value
    # in the opposite direction to trigger exit.
    exit_on_edge_flip: bool = field(default_factory=lambda: _env_bool("EXIT_ON_EDGE_FLIP", False))
    exit_edge_flip_threshold: float = field(default_factory=lambda: _env_float("EXIT_EDGE_FLIP_THRESHOLD", 0.04))
    # Confidence floor on the *re-estimated* edge before an edge-flip
    # exit fires.  Without this gate, a low-confidence flicker against
    # us would close the position prematurely.  Lifted out of two
    # hardcoded ``> 0.3`` checks in main._tick / _check_exits_only.
    exit_edge_flip_min_confidence: float = field(
        default_factory=lambda: _env_float("EXIT_EDGE_FLIP_MIN_CONFIDENCE", 0.3)
    )

    # Max relative divergence between the snapshot price (from the /price
    # endpoint, which can lag after low-activity periods) and the live book
    # midpoint before we treat the snapshot as stale and reject the order.
    # Default 3% — conservative enough to tolerate tick granularity without
    # masking genuinely stale feeds.
    max_price_book_divergence: float = field(default_factory=lambda: _env_float("MAX_PRICE_BOOK_DIVERGENCE", 0.03))

    # -- Negative-risk arbitrage detector (opt-in, read-only) -----------------
    # When enabled, each tick scans the filtered market snapshots for
    # structural arbs (sum of outcome-prices < 1) and records detections
    # to ``arb_opportunities``.  Never places trades automatically — this
    # is purely observational.  Default: off.
    arb_detector_enabled: bool = field(default_factory=lambda: _env_bool("ARB_DETECTOR_ENABLED", False))
    arb_min_discount: float = field(default_factory=lambda: _env_float("ARB_MIN_DISCOUNT", 0.01))
    arb_min_legs_liquidity: float = field(default_factory=lambda: _env_float("ARB_MIN_LEGS_LIQUIDITY", 100.0))

    # -- Execution-cost model (opt-in) -----------------------------------------
    # Hypothetical fee schedule in basis points of notional.  Both default
    # to 0 (Polymarket's current CLOB charges no fee), so enabling this is
    # strictly additive and reversible — no existing trade accounting
    # changes until an operator sets a non-zero value.  Fees never modify
    # recorded fill prices; they accumulate in PortfolioTracker.fees_paid
    # and are reported alongside gross PnL.
    taker_fee_bps: float = field(default_factory=lambda: _env_float("TAKER_FEE_BPS", 0.0))
    maker_fee_bps: float = field(default_factory=lambda: _env_float("MAKER_FEE_BPS", 0.0))

    # -- Order posting mode (paper-only experiment) ---------------------------
    # ``taker``           — cross the spread; fill is immediate at VWAP/ask
    #                       (current behaviour, default).
    # ``maker_preferred`` — post passively at best_bid (BUY) / best_ask (SELL).
    #                       Paper engine simulates a fill probability per
    #                       tick via ``MAKER_FILL_PROB``.  On a miss the
    #                       signal is simply dropped for that tick (we never
    #                       fake a fill).  No live-mode behaviour change.
    # Strictly opt-in — default preserves paper PnL exactly.
    order_mode: str = field(default_factory=lambda: _env("ORDER_MODE", "taker"))
    maker_fill_prob: float = field(default_factory=lambda: _env_float("MAKER_FILL_PROB", 0.7))

    # -- Semantic Mispricing Engine (opt-in, default off) ---------------------
    # A read-only observer that detects mispricings between a target market
    # and a synthetic fair price derived from *related* markets (structural
    # neg-risk siblings, near-equivalent questions, temporal checkpoints).
    # When ``STRATEGY=semantic_mispricing`` the engine's output is also used
    # as a trading strategy — otherwise it only populates the
    # ``semantic_signals`` table for offline analysis.
    #
    # All thresholds default to values that are strict enough to prevent
    # textual false positives from generating trades.  Loosen with care.
    semantic_engine_enabled: bool = field(
        default_factory=lambda: _env_bool("SEMANTIC_ENGINE_ENABLED", False)
    )
    # ``disabled`` → no-op.  ``shadow`` → scan + persist + record SHADOW_*
    # decisions if STRATEGY=semantic_mispricing.  ``live`` → the strategy
    # routes signals through the normal risk + execution path (still paper
    # unless ALLOW_LIVE_TRADING=true globally).
    semantic_engine_mode: str = field(
        default_factory=lambda: _env("SEMANTIC_ENGINE_MODE", "shadow")
    )
    semantic_min_relation_confidence: float = field(
        default_factory=lambda: _env_float("SEMANTIC_MIN_RELATION_CONFIDENCE", 0.65)
    )
    semantic_min_net_edge: float = field(
        default_factory=lambda: _env_float("SEMANTIC_MIN_NET_EDGE", 0.02)
    )
    semantic_min_signal_score: float = field(
        default_factory=lambda: _env_float("SEMANTIC_MIN_SIGNAL_SCORE", 0.60)
    )
    semantic_max_spread: float = field(
        default_factory=lambda: _env_float("SEMANTIC_MAX_SPREAD", 0.05)
    )
    semantic_min_liquidity: float = field(
        default_factory=lambda: _env_float("SEMANTIC_MIN_LIQUIDITY", 500.0)
    )
    semantic_max_related_markets: int = field(
        default_factory=lambda: _env_int("SEMANTIC_MAX_RELATED_MARKETS", 8)
    )
    semantic_min_sibling_liquidity: float = field(
        default_factory=lambda: _env_float("SEMANTIC_MIN_SIBLING_LIQUIDITY", 100.0)
    )
    semantic_safety_margin_bps: float = field(
        default_factory=lambda: _env_float("SEMANTIC_SAFETY_MARGIN_BPS", 50.0)
    )
    # Execution preference: "taker" | "maker" | "auto".  "auto" lets the
    # scorer pick maker when a passive post is economically better given
    # the fee schedule, but still flags the intent in the features dict.
    semantic_execution_preference: str = field(
        default_factory=lambda: _env("SEMANTIC_EXECUTION_PREFERENCE", "auto")
    )
    semantic_use_neg_risk_links: bool = field(
        default_factory=lambda: _env_bool("SEMANTIC_USE_NEG_RISK_LINKS", True)
    )
    semantic_use_temporal_links: bool = field(
        default_factory=lambda: _env_bool("SEMANTIC_USE_TEMPORAL_LINKS", True)
    )
    semantic_use_inverse_links: bool = field(
        default_factory=lambda: _env_bool("SEMANTIC_USE_INVERSE_LINKS", True)
    )
    semantic_use_textual_links: bool = field(
        default_factory=lambda: _env_bool("SEMANTIC_USE_TEXTUAL_LINKS", True)
    )
    semantic_calibration_overlay_enabled: bool = field(
        default_factory=lambda: _env_bool("SEMANTIC_CALIBRATION_OVERLAY", False)
    )
    # Cross-tick EMA smoothing for the synthetic fair-price.  Alpha in
    # (0, 1] activates smoothing; 0.0 (default) disables it and the engine
    # uses the raw per-tick synthetic.  A typical value is 0.3 — moderate
    # trailing average that dampens single-tick stale-sibling spikes
    # without masking genuine moves.  See
    # src/analysis/semantic_engine/smoothing.py for the rationale.
    semantic_smoothing_alpha: float = field(
        default_factory=lambda: _env_float("SEMANTIC_SMOOTHING_ALPHA", 0.0)
    )
    semantic_smoothing_max_keys: int = field(
        default_factory=lambda: _env_int("SEMANTIC_SMOOTHING_MAX_KEYS", 2048)
    )

    # -- Bot loop --------------------------------------------------------------
    poll_interval: int = field(default_factory=lambda: _env_int("POLL_INTERVAL_SECONDS", 60))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: _env("LOG_FILE", "bot.log"))
    # Rotate bot.log once it grows past this.  Default 10 MB keeps the
    # disk footprint bounded (at most (backup+1)*max_bytes ≈ 60 MB with
    # defaults).  Set to 0 to disable rotation (legacy behaviour).
    log_max_bytes: int = field(
        default_factory=lambda: _env_int("LOG_MAX_BYTES", 10 * 1024 * 1024)
    )
    log_backup_count: int = field(
        default_factory=lambda: _env_int("LOG_BACKUP_COUNT", 5)
    )

    # -- DB backups (opt-in, but strongly recommended) ------------------------
    # Empty string disables.  When set, the bot takes an online SQLite
    # backup every ``db_backup_interval_hours`` and keeps the N most
    # recent files.  Uses ``sqlite3.Connection.backup`` (page-level,
    # concurrent-write-safe) — *not* a plain file copy.
    db_backup_dir: str = field(default_factory=lambda: _env("DB_BACKUP_DIR", ""))
    db_backup_interval_hours: float = field(
        default_factory=lambda: _env_float("DB_BACKUP_INTERVAL_HOURS", 24.0)
    )
    db_backup_keep: int = field(
        default_factory=lambda: _env_int("DB_BACKUP_KEEP", 7)
    )

    # -- Decision log retention ------------------------------------------------
    # The bot writes a row per evaluated market per tick into
    # ``decision_log``.  At realistic poll/market counts that crosses
    # millions of rows in months, and the dashboard's ``GROUP BY action``
    # queries slow to a crawl.  Periodic pruning bounds the working
    # set without sacrificing the recent forensic window.
    # ``0`` disables pruning entirely.
    decision_log_retention_days: int = field(
        default_factory=lambda: _env_int("DECISION_LOG_RETENTION_DAYS", 30)
    )
    decision_log_prune_interval_hours: float = field(
        default_factory=lambda: _env_float("DECISION_LOG_PRUNE_INTERVAL_HOURS", 24.0)
    )

    # -- Storage ---------------------------------------------------------------
    sqlite_db_path: str = field(default_factory=lambda: _env("SQLITE_DB_PATH", "polymarket_bot.db"))

    # -- Market fetch limit (to avoid downloading all 50K+ markets per tick) ---
    max_markets_fetch: int = field(default_factory=lambda: _env_int("MAX_MARKETS_FETCH", 500))

    # -- Kill switch -----------------------------------------------------------
    kill_switch_file: str = field(default_factory=lambda: _env("KILL_SWITCH_FILE", "KILL_SWITCH"))

    # -- Alerting (opt-in) -----------------------------------------------------
    # Empty strings disable each sink.  See ``src/utils/alerts.py``.  The
    # file sink is local JSONL (audit log); the webhook sink is Slack /
    # Discord compatible.  Both default to off so paper runs behave
    # exactly as before.
    alert_log_file: str = field(default_factory=lambda: _env("ALERT_LOG_FILE", ""))
    alert_webhook_url: str = field(default_factory=lambda: _env("ALERT_WEBHOOK_URL", ""))
    alert_dedupe_seconds: float = field(
        default_factory=lambda: _env_float("ALERT_DEDUPE_SECONDS", 300.0)
    )
    alert_max_per_minute: int = field(
        default_factory=lambda: _env_int("ALERT_MAX_PER_MINUTE", 20)
    )
    # Webhook-specific severity threshold.  File sink always captures
    # everything (local JSONL is free); the webhook drops anything
    # below this level so Slack isn't spammed with tick noise.
    # Valid: "info", "warning", "critical".
    alert_webhook_min_severity: str = field(
        default_factory=lambda: _env("ALERT_WEBHOOK_MIN_SEVERITY", "warning")
    )

    # -- Auto-settlement of resolved markets (opt-in) --------------------------
    # When a Polymarket market resolves, each outcome token converges to
    # $1 or $0.  The sweeper walks open positions every N minutes,
    # detects resolved markets via the Gamma API, and books a
    # synthetic settlement fill + audit row so the portfolio reflects
    # reality.  NO live orders are ever placed — settlement is
    # accounting-only (the exchange has already effected the payout).
    # Default off: flip ``RESOLUTION_SWEEPER_ENABLED=true`` to activate.
    resolution_sweeper_enabled: bool = field(
        default_factory=lambda: _env_bool("RESOLUTION_SWEEPER_ENABLED", False)
    )
    resolution_sweep_interval_minutes: float = field(
        default_factory=lambda: _env_float("RESOLUTION_SWEEP_INTERVAL_MINUTES", 60.0)
    )

    # -- Staleness monitor (opt-in) --------------------------------------------
    # Flag (and optionally close) positions that have been open for
    # more than N days with no meaningful price movement — they
    # silently tie up capital that a fresher signal could use.
    # ``0`` (default) disables the feature entirely.  ``action`` is
    # ``alert`` (safe) or ``close`` (escalate to the normal exit path).
    position_staleness_days: float = field(
        default_factory=lambda: _env_float("POSITION_STALENESS_DAYS", 0.0)
    )
    position_staleness_price_epsilon: float = field(
        default_factory=lambda: _env_float("POSITION_STALENESS_PRICE_EPSILON", 0.01)
    )
    position_staleness_action: str = field(
        default_factory=lambda: _env("POSITION_STALENESS_ACTION", "alert")
    )
    position_staleness_interval_minutes: float = field(
        default_factory=lambda: _env_float("POSITION_STALENESS_INTERVAL_MINUTES", 120.0)
    )

    # -- Zombie position detection (always on) ---------------------------------
    # Distinct from POSITION_STALENESS_*, which looks for *days* of price
    # stagnation in the historical feed.  This counter tracks *ticks*
    # for which ``client.get_price`` returned ``None`` for a position,
    # which indicates the API is silent for that token even though the
    # bot itself is healthy.  Without this gate, SL/TP checks silently
    # skip the position and capital stays trapped.  ``0`` disables.
    zombie_position_max_missing_ticks: int = field(
        default_factory=lambda: _env_int("ZOMBIE_POSITION_MAX_MISSING_TICKS", 5)
    )
    # ``alert`` (log + optional webhook) or ``close`` (force-close at
    # last known price).  ``close`` is safer for capital preservation
    # but requires a non-zero ``last_known_price`` to mark-to.
    zombie_position_action: str = field(
        default_factory=lambda: _env("ZOMBIE_POSITION_ACTION", "alert")
    )

    # -- Temporal edge filter (opt-in) -----------------------------------------
    # Restrict new BUY entries to UTC hours where our own closed-trade
    # win-rate clears ``temporal_min_winrate`` with at least
    # ``temporal_min_samples`` trades in the lookback window.  Default
    # off; cold-start safe (allows all 24 hours until enough evidence
    # accumulates).  See ``src/analysis/temporal_edge.py``.
    temporal_filter_enabled: bool = field(
        default_factory=lambda: _env_bool("TEMPORAL_FILTER_ENABLED", False)
    )
    temporal_min_winrate: float = field(
        default_factory=lambda: _env_float("TEMPORAL_MIN_WINRATE", 0.65)
    )
    temporal_min_samples: int = field(
        default_factory=lambda: _env_int("TEMPORAL_MIN_SAMPLES", 20)
    )
    temporal_window_days: int = field(
        default_factory=lambda: _env_int("TEMPORAL_WINDOW_DAYS", 30)
    )
    temporal_refresh_seconds: float = field(
        default_factory=lambda: _env_float("TEMPORAL_REFRESH_SECONDS", 3600.0)
    )

    # -- Volatility filter (opt-in) --------------------------------------------
    # Reject BUYs on tokens whose recent raw-price stddev exceeds
    # ``max_price_volatility`` (in $-of-price units, since prices live
    # in [0, 1]).  Cold-start fail-safe: a token with fewer than
    # ``volatility_window`` samples is *not* presumed volatile.
    # Default off.  See ``src/analysis/volatility.py``.
    volatility_filter_enabled: bool = field(
        default_factory=lambda: _env_bool("VOLATILITY_FILTER_ENABLED", False)
    )
    max_price_volatility: float = field(
        default_factory=lambda: _env_float("MAX_PRICE_VOLATILITY", 0.05)
    )
    volatility_window: int = field(
        default_factory=lambda: _env_int("VOLATILITY_WINDOW", 10)
    )

    # -- Bayesian calibration (opt-in, default off) ----------------------------
    # Tracks a Beta(α, β) posterior per strategy, updated after each
    # closed trade (win → α+=1, loss → β+=1).  When
    # ``BAYESIAN_SIZING_ENABLED=true`` the posterior mean is used as a
    # sizing multiplier clamped to ``[BAYESIAN_MIN_MULT, 1.0]`` —
    # asymmetric: can only size *down* a losing strategy, never up.
    # Cold-start fail-safe: until ``BAYESIAN_MIN_SAMPLES`` trades
    # accrue, the multiplier is 1.0 (no effect).  Enabling only
    # ``BAYESIAN_CALIBRATION_ENABLED`` runs the tracker silently so
    # the operator can accumulate evidence before toggling sizing on.
    bayesian_calibration_enabled: bool = field(
        default_factory=lambda: _env_bool("BAYESIAN_CALIBRATION_ENABLED", False)
    )
    bayesian_sizing_enabled: bool = field(
        default_factory=lambda: _env_bool("BAYESIAN_SIZING_ENABLED", False)
    )
    bayesian_min_samples: int = field(
        default_factory=lambda: _env_int("BAYESIAN_MIN_SAMPLES", 30)
    )
    bayesian_min_multiplier: float = field(
        default_factory=lambda: _env_float("BAYESIAN_MIN_MULTIPLIER", 0.3)
    )
    bayesian_prior_alpha: float = field(
        default_factory=lambda: _env_float("BAYESIAN_PRIOR_ALPHA", 1.0)
    )
    bayesian_prior_beta: float = field(
        default_factory=lambda: _env_float("BAYESIAN_PRIOR_BETA", 1.0)
    )

    # -- Tail-risk monitoring (opt-in, observational by default) ----------------
    # Per-tick VaR/CVaR computation over open positions.  Default on
    # once any position exists (cheap: enumerates 2^N up to N=12, MC
    # beyond).  Alerts fire only when thresholds are set > 0.
    tail_risk_enabled: bool = field(
        default_factory=lambda: _env_bool("TAIL_RISK_ENABLED", True)
    )
    # Alert when 95% VaR crosses this level (USD).  0 = no alert.
    var_95_alert_usd: float = field(
        default_factory=lambda: _env_float("VAR_95_ALERT_USD", 0.0)
    )
    # Alert when CVaR (expected shortfall) crosses this level (USD).
    # 0 = no alert.  Typically set tighter than ``var_95_alert_usd``.
    cvar_95_alert_usd: float = field(
        default_factory=lambda: _env_float("CVAR_95_ALERT_USD", 0.0)
    )

    # -- Capital-efficiency sizing (opt-in) ------------------------------------
    # Shrinks the position when the market resolves more than
    # ``sizing_capital_efficiency_target_days`` away — capital tied
    # up longer should earn more per dollar.  Multiplier is
    # ``target_days / days_to_resolution`` floored at
    # ``sizing_capital_efficiency_min_factor``.  Default off so
    # existing paper PnL stays byte-identical until enabled.
    sizing_capital_efficiency_enabled: bool = field(
        default_factory=lambda: _env_bool("SIZING_CAPITAL_EFFICIENCY_ENABLED", False)
    )
    sizing_capital_efficiency_target_days: float = field(
        default_factory=lambda: _env_float("SIZING_CAPITAL_EFFICIENCY_TARGET_DAYS", 14.0)
    )
    sizing_capital_efficiency_min_factor: float = field(
        default_factory=lambda: _env_float("SIZING_CAPITAL_EFFICIENCY_MIN_FACTOR", 0.25)
    )

    # -- Live wallet balance gate ----------------------------------------------
    # Belt-and-braces runtime check: refuse a live BUY if the cost
    # exceeds available USDC (minus a configurable buffer).  Has no
    # effect in paper mode.  Cold/unreadable balance is fail-safe
    # (allows the trade) — the preflight command is the authoritative
    # pre-launch gate; this is the runtime backup that catches a
    # mid-flight balance drop (manual withdrawal, bridge out, etc.).
    wallet_balance_check_enabled: bool = field(
        default_factory=lambda: _env_bool("WALLET_BALANCE_CHECK_ENABLED", True)
    )
    wallet_balance_refresh_seconds: float = field(
        default_factory=lambda: _env_float("WALLET_BALANCE_REFRESH_SECONDS", 60.0)
    )
    wallet_balance_min_buffer_usd: float = field(
        default_factory=lambda: _env_float("WALLET_BALANCE_MIN_BUFFER_USD", 0.0)
    )

    # -- Position reconciliation ------------------------------------------------
    # Periodically compares ``PortfolioTracker.positions`` against the
    # CLOB-reported wallet balance for each tracked token, flagging
    # divergences above a tolerance.  Report-only (never mutates state).
    # Paper mode is always a no-op.  See ``src/portfolio/reconciliation.py``.
    position_reconciliation_enabled: bool = field(
        default_factory=lambda: _env_bool("POSITION_RECONCILIATION_ENABLED", False)
    )
    position_reconciliation_interval_minutes: float = field(
        default_factory=lambda: _env_float("POSITION_RECONCILIATION_INTERVAL_MINUTES", 60.0)
    )
    position_reconciliation_tolerance_shares: float = field(
        default_factory=lambda: _env_float("POSITION_RECONCILIATION_TOLERANCE_SHARES", 0.01)
    )

    # -- First-N live-trades autopause ------------------------------------------
    # Block BUYs after this many live fills until the operator touches
    # the ack file.  0 = disabled (back-compat).  Paper mode is a no-op
    # regardless.  See ``src/risk/live_autopause.py``.
    live_trade_autopause_threshold: int = field(
        default_factory=lambda: _env_int("LIVE_TRADE_AUTOPAUSE_THRESHOLD", 0)
    )
    live_trade_autopause_ack_file: str = field(
        default_factory=lambda: _env("LIVE_TRADE_AUTOPAUSE_ACK_FILE", "live_trades_acknowledged.ack")
    )

    # -- Regime-shift detector (correlated market moves) -----------------------
    # When enabled, the loop periodically asks the detector whether
    # the recent price history of *all tracked tokens* shows a
    # coordinated move (classic election-night / news-flash scenario).
    # Report-only by default — alerts fire, operators decide.  Set
    # ``regime_auto_pause_on_shift=true`` to auto-block new BUYs while
    # a shift is active (an in-memory flag cleared on the next calm
    # verdict; never persists across restarts so the bot self-heals).
    regime_detector_enabled: bool = field(
        default_factory=lambda: _env_bool("REGIME_DETECTOR_ENABLED", False)
    )
    regime_detector_interval_minutes: float = field(
        default_factory=lambda: _env_float("REGIME_DETECTOR_INTERVAL_MINUTES", 5.0)
    )
    regime_move_threshold: float = field(
        default_factory=lambda: _env_float("REGIME_MOVE_THRESHOLD", 0.05)
    )
    regime_fraction_threshold: float = field(
        default_factory=lambda: _env_float("REGIME_FRACTION_THRESHOLD", 0.25)
    )
    regime_min_universe: int = field(
        default_factory=lambda: _env_int("REGIME_MIN_UNIVERSE", 20)
    )
    regime_lookback_points: int = field(
        default_factory=lambda: _env_int("REGIME_LOOKBACK_POINTS", 10)
    )
    regime_auto_pause_on_shift: bool = field(
        default_factory=lambda: _env_bool("REGIME_AUTO_PAUSE_ON_SHIFT", False)
    )

    # -- Daily summary scheduler -----------------------------------------------
    # When enabled, the tick loop fires the same daily summary that
    # the CLI prints, dispatched through the alert manager at info
    # severity, shortly after UTC midnight.  Idempotent: fires at
    # most once per UTC day, tracked in-memory.
    daily_summary_enabled: bool = field(
        default_factory=lambda: _env_bool("DAILY_SUMMARY_ENABLED", False)
    )

    # -- Dry-run live cap (opt-in) -----------------------------------------------
    # When > 0 and mode is live, caps every position's USD notional at
    # this value regardless of what sizing logic computes.  Lets the
    # operator run live with real orders but tiny stakes until confident
    # that execution, fees, and accounting are working correctly.
    # 0 (default) = no cap (normal sizing applies).
    live_trade_max_position_usd: float = field(
        default_factory=lambda: _env_float("LIVE_TRADE_MAX_POSITION_USD", 0.0)
    )

    # -- Anti-pump filter (opt-in) ----------------------------------------------
    # Reject BUYs when the token's recent absolute return exceeds a
    # threshold — catching news pumps and thin-book spikes before the
    # bot buys the top.  SELLs never gated.  Cold-start safe.
    anti_pump_enabled: bool = field(
        default_factory=lambda: _env_bool("ANTI_PUMP_ENABLED", False)
    )
    anti_pump_threshold: float = field(
        default_factory=lambda: _env_float("ANTI_PUMP_THRESHOLD", 0.10)
    )
    anti_pump_window_points: int = field(
        default_factory=lambda: _env_int("ANTI_PUMP_WINDOW_POINTS", 10)
    )

    # -- Latency telemetry (opt-in) --------------------------------------------
    # Rolling recorder for signal→submit→fill intervals.  Fires a
    # warning alert when the latest end-to-end latency exceeds the
    # threshold (0 = disabled).  Dashboard: GET /api/latency.
    latency_tracking_enabled: bool = field(
        default_factory=lambda: _env_bool("LATENCY_TRACKING_ENABLED", False)
    )
    latency_alert_threshold_ms: float = field(
        default_factory=lambda: _env_float("LATENCY_ALERT_THRESHOLD_MS", 0.0)
    )
    latency_window_samples: int = field(
        default_factory=lambda: _env_int("LATENCY_WINDOW_SAMPLES", 500)
    )

    # -- Live risk-metrics tail window ----------------------------------------
    # Number of most-recent closed trades fed into the rolling Sharpe /
    # Sortino / max-drawdown summary surfaced in ``bot_state.json`` and
    # the dashboard.  ``0`` uses the entire history; smaller values
    # track recent regime changes faster but give noisier estimates.
    risk_metrics_tail_window: int = field(
        default_factory=lambda: _env_int("RISK_METRICS_TAIL_WINDOW", 200)
    )

    # -- Strategy validation thresholds (CLI ``validate-strategy``) ----------
    # The promote-to-live veredict requires *all* of the conditions
    # below to hold simultaneously.  Defaults are conservative (López
    # de Prado-style): the rule of thumb is that a strategy that can't
    # clear these on paper data is never safe with live capital.
    # Override via env to tighten or loosen for a given research phase.
    validation_min_trades: int = field(
        default_factory=lambda: _env_int("VALIDATION_MIN_TRADES", 200)
    )
    validation_min_oos_winning_windows_frac: float = field(
        default_factory=lambda: _env_float("VALIDATION_MIN_OOS_WINNING_WINDOWS_FRAC", 0.6)
    )
    validation_min_median_oos_sharpe: float = field(
        default_factory=lambda: _env_float("VALIDATION_MIN_MEDIAN_OOS_SHARPE", 0.5)
    )
    validation_min_psr: float = field(
        default_factory=lambda: _env_float("VALIDATION_MIN_PSR", 0.95)
    )
    validation_min_dsr: float = field(
        default_factory=lambda: _env_float("VALIDATION_MIN_DSR", 0.95)
    )
    validation_max_drawdown_pct: float = field(
        default_factory=lambda: _env_float("VALIDATION_MAX_DRAWDOWN_PCT", 0.15)
    )
    validation_n_trials: int = field(
        default_factory=lambda: _env_int("VALIDATION_N_TRIALS", 1)
    )
    validation_train_size: int = field(
        default_factory=lambda: _env_int("VALIDATION_TRAIN_SIZE", 200)
    )
    validation_test_size: int = field(
        default_factory=lambda: _env_int("VALIDATION_TEST_SIZE", 100)
    )
    validation_purge: int = field(
        default_factory=lambda: _env_int("VALIDATION_PURGE", 5)
    )
    validation_embargo: int = field(
        default_factory=lambda: _env_int("VALIDATION_EMBARGO", 2)
    )
    validation_seed: int = field(
        default_factory=lambda: _env_int("VALIDATION_SEED", 0)
    )
    validation_periods_per_year: int = field(
        default_factory=lambda: _env_int("VALIDATION_PERIODS_PER_YEAR", 252)
    )
    validation_slippage_stress_multiplier: float = field(
        default_factory=lambda: _env_float("VALIDATION_SLIPPAGE_STRESS_MULTIPLIER", 2.0)
    )

    # -- Metrics (opt-in JSONL sink) -------------------------------------------
    # Empty string disables.  When set, each tick writes one JSON record
    # to the file; external log shippers can tail it without opening the
    # SQLite DB.  See ``src/utils/metrics.py``.
    metrics_file: str = field(default_factory=lambda: _env("METRICS_FILE", ""))

    # -- Derived helpers -------------------------------------------------------
    @property
    def is_paper(self) -> bool:
        return self.trading_mode.lower() == "paper"

    # The exact second-gate phrase required alongside ALLOW_LIVE_TRADING=true.
    # ``ClassVar`` keeps dataclasses from treating this as a field, so the
    # phrase is a true constant — not something an operator can override
    # via env without changing source.
    LIVE_CONFIRMATION_PHRASE: ClassVar[str] = "YES_TRADE_REAL_FUNDS"

    @property
    def is_live(self) -> bool:
        return (
            self.trading_mode.lower() == "live"
            and self.allow_live_trading
            and self.i_understand_real_money == self.LIVE_CONFIRMATION_PHRASE
        )

    def effective_min_edge(self, category: str = "") -> float:
        """Return the min-edge threshold for a given market category.

        Resolution order: exact per-category override → global fallback.
        Absent categories or an empty override dict both yield the
        global :attr:`min_edge_for_trade`, so the default behaviour is
        unchanged.
        """
        if category and self.min_edge_by_category:
            try:
                val = self.min_edge_by_category.get(category)
                if val is not None:
                    return float(val)
            except (TypeError, ValueError):
                pass
        return self.min_edge_for_trade

    def validate(self) -> list[str]:
        """Return a list of configuration problems (empty == OK)."""
        problems: list[str] = []
        if self.trading_mode.lower() == "live" and not self.allow_live_trading:
            problems.append(
                "TRADING_MODE=live but ALLOW_LIVE_TRADING is not true. "
                "Orders will NOT be sent."
            )
        # Second-gate check: if the operator set ALLOW_LIVE_TRADING=true
        # but forgot I_UNDERSTAND_REAL_MONEY (or typo'd), surface it
        # prominently — we stay in paper regardless.
        if (
            self.trading_mode.lower() == "live"
            and self.allow_live_trading
            and self.i_understand_real_money != self.LIVE_CONFIRMATION_PHRASE
        ):
            problems.append(
                "LIVE TRADING BLOCKED: I_UNDERSTAND_REAL_MONEY must equal "
                f"'{self.LIVE_CONFIRMATION_PHRASE}' exactly. "
                "Bot will operate in paper mode."
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
