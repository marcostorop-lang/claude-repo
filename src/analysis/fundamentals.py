"""
Market fundamentals analysis.

Extracts information-based signals from market metadata that go beyond
pure price action.  In prediction markets, the 90% of alpha comes from
information — volume surges, liquidity changes, price/volume divergences,
and temporal patterns all carry predictive content.

This module works with data already available from the Gamma API and SQLite
(no external APIs or keys needed).
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field

from src.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


@dataclass
class MarketFundamentals:
    """Fundamental signals for a single market/token.

    Each signal is normalised to [-1, 1] where positive = bullish.
    """

    # Volume momentum: is trading activity increasing?
    volume_trend: float = 0.0
    # Liquidity change: is the market getting deeper or thinner?
    liquidity_trend: float = 0.0
    # Price-volume agreement: does volume confirm the price move?
    price_volume_agreement: float = 0.0
    # Spread trend: is the spread narrowing (bullish) or widening (bearish)?
    spread_trend: float = 0.0
    # Market maturity: how established is this market (more history = more reliable)
    maturity: float = 0.0
    # Raw data for logging
    raw: dict = field(default_factory=dict)


def compute_fundamentals(
    token_id: str,
    store: SQLiteStore,
    current_price: float = 0.0,
    current_spread: float = 0.0,
    current_volume: float = 0.0,
    current_liquidity: float = 0.0,
) -> MarketFundamentals:
    """Compute fundamental signals for a token from stored history.

    Uses price_history (with spread) and decision_log to build a picture
    of whether this market is moving on real interest or just noise.
    """
    f = MarketFundamentals()

    # Load recent price history with spreads
    try:
        cols = {row[1] for row in store._conn.execute("PRAGMA table_info(price_history)").fetchall()}
        has_spread = "spread" in cols

        if has_spread:
            cur = store._conn.execute(
                "SELECT price, spread, timestamp FROM price_history "
                "WHERE token_id = ? ORDER BY id DESC LIMIT 50",
                (token_id,),
            )
        else:
            cur = store._conn.execute(
                "SELECT price, 0.0 as spread, timestamp FROM price_history "
                "WHERE token_id = ? ORDER BY id DESC LIMIT 50",
                (token_id,),
            )
        rows = cur.fetchall()
    except sqlite3.Error:
        logger.debug("Failed to load price history for fundamentals", exc_info=True)
        return f

    if len(rows) < 3:
        f.maturity = len(rows) / 10.0  # 0-1 scale, 10+ ticks = mature
        return f

    prices = [r["price"] for r in reversed(rows)]
    spreads = [r["spread"] for r in reversed(rows)]
    n = len(prices)

    # --- Maturity: more data points = more reliable ---
    f.maturity = min(n / 20.0, 1.0)

    # --- Spread trend: is it narrowing or widening? ---
    if len(spreads) >= 5 and any(s > 0 for s in spreads):
        recent_spreads = [s for s in spreads[-5:] if s > 0]
        older_spreads = [s for s in spreads[:5] if s > 0]
        if recent_spreads and older_spreads:
            avg_recent = sum(recent_spreads) / len(recent_spreads)
            avg_older = sum(older_spreads) / len(older_spreads)
            if avg_older > 0:
                spread_change = (avg_older - avg_recent) / avg_older
                # Narrowing = positive (more confident market), widening = negative
                f.spread_trend = max(-1.0, min(1.0, spread_change * 5.0))

    # --- Price momentum (already in strategy, but needed for agreement) ---
    if n >= 3:
        price_change = (prices[-1] - prices[-3]) / prices[-3] if prices[-3] > 0 else 0.0
    else:
        price_change = 0.0

    # --- Volume signal ---
    # We use decision_log count as a proxy for market activity since we don't
    # store volume per tick. More decisions in recent ticks = more interest.
    try:
        recent_decisions = store._conn.execute(
            "SELECT COUNT(*) as cnt FROM decision_log "
            "WHERE token_id = ? AND timestamp > datetime('now', '-1 hour')",
            (token_id,),
        ).fetchone()
        older_decisions = store._conn.execute(
            "SELECT COUNT(*) as cnt FROM decision_log "
            "WHERE token_id = ? AND timestamp > datetime('now', '-24 hours') "
            "AND timestamp <= datetime('now', '-1 hour')",
            (token_id,),
        ).fetchone()

        recent_count = recent_decisions["cnt"] if recent_decisions else 0
        older_count = older_decisions["cnt"] if older_decisions else 0

        if older_count > 0:
            vol_change = (recent_count - older_count / 23.0) / max(older_count / 23.0, 1)
            f.volume_trend = max(-1.0, min(1.0, vol_change))
    except (sqlite3.Error, ValueError, TypeError, ZeroDivisionError):
        pass

    # --- Price-volume agreement ---
    # If price is moving AND volume is increasing → agreement (+1)
    # If price is moving BUT volume is flat/down → divergence (-1)
    if abs(price_change) > 0.01:
        if f.volume_trend > 0:
            f.price_volume_agreement = 1.0  # confirmed move
        elif f.volume_trend < -0.3:
            f.price_volume_agreement = -1.0  # divergence — suspicious move
        else:
            f.price_volume_agreement = 0.0  # inconclusive
    else:
        f.price_volume_agreement = 0.0  # no significant price move

    # --- Liquidity trend (from reported liquidity if available) ---
    if current_liquidity > 0:
        # Compare to what we'd expect: higher liquidity = healthier market
        # This is a rough signal — if the market has >$10K liquidity, it's solid
        liq_score = min(math.log10(max(current_liquidity, 1)) / 5.0, 1.0)  # $100K = 1.0
        f.liquidity_trend = liq_score * 2 - 1  # map [0,1] → [-1,1]

    f.raw = {
        "n_ticks": n,
        "price_change_3": round(price_change, 6),
        "spread_trend": round(f.spread_trend, 4),
        "volume_trend": round(f.volume_trend, 4),
        "pv_agreement": round(f.price_volume_agreement, 4),
        "liquidity_trend": round(f.liquidity_trend, 4),
        "maturity": round(f.maturity, 4),
    }

    return f


def fundamentals_score(fund: MarketFundamentals) -> float:
    """Collapse fundamentals into a single [-1, 1] quality score.

    This is used as a confidence multiplier — it doesn't create signals,
    it validates (or invalidates) existing directional signals.

    High score = market conditions support trading (narrow spreads, volume
    confirms, liquid market, mature data).
    Low score = market conditions are noisy or deteriorating.
    """
    weights = {
        "spread_trend": 0.25,
        "price_volume_agreement": 0.30,
        "liquidity_trend": 0.20,
        "maturity": 0.25,
    }
    score = (
        weights["spread_trend"] * fund.spread_trend
        + weights["price_volume_agreement"] * fund.price_volume_agreement
        + weights["liquidity_trend"] * fund.liquidity_trend
        + weights["maturity"] * fund.maturity
    )
    return max(-1.0, min(1.0, score))
