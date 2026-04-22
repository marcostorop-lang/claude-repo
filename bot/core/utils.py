"""Shared utilities — data classes, helpers, notification stubs."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class TradeSignal:
    """A proposed trade emitted by a strategy."""

    strategy: str
    token_id: str
    condition_id: str
    side: Side
    price: float
    edge: float  # signed: positive means we think price is too low
    confidence: float  # 0–1: Claude's confidence in its own estimate
    probability: float = 0.5  # Claude's estimated TRUE probability
    size_usd: float = 0.0  # filled by risk manager
    reason: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    category: str = ""  # market category for correlation-aware sizing


@dataclass
class MarketInfo:
    """Lightweight view of a Polymarket market from the Gamma API."""

    condition_id: str
    question: str
    description: str
    category: str
    end_date: str
    active: bool
    volume: float
    liquidity: float
    outcomes: list[str]
    outcome_prices: list[float]
    token_ids: list[str]
    tags: list[str] = field(default_factory=list)
    neg_risk: bool = False
    neg_risk_market_id: str = ""


@dataclass
class BookSnapshot:
    """Thin wrapper over an orderbook snapshot."""

    token_id: str
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_depth_usd: float = 0.0
    ask_depth_usd: float = 0.0

    @property
    def midpoint(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2.0
        return self.best_bid or self.best_ask

    @property
    def spread(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return self.best_ask - self.best_bid
        return 0.0

    @property
    def spread_pct(self) -> float:
        mid = self.midpoint
        return self.spread / mid if mid > 0 else 0.0


@dataclass
class OrderResult:
    """Result of an order attempt."""

    success: bool
    order_id: str = ""
    filled_size: float = 0.0
    fill_price: float = 0.0
    message: str = ""
    mode: str = "paper"


# ---------------------------------------------------------------------------
# Notification stubs
# ---------------------------------------------------------------------------


async def notify_telegram(token: str, chat_id: str, text: str) -> None:
    """Fire-and-forget Telegram message.  No-op when credentials are empty."""
    if not token or not chat_id:
        return
    try:
        import httpx
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    except Exception:
        pass


async def notify_discord(webhook_url: str, text: str) -> None:
    """Fire-and-forget Discord webhook.  No-op when URL is empty."""
    if not webhook_url:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(webhook_url, json={"content": text})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Latency tracking (#12)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LatencyStats:
    """Summary statistics for a latency distribution."""

    n_samples: int = 0
    p50_ms: float = 0.0
    p90_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Compute a percentile from a sorted list (linear interpolation)."""
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    idx = pct / 100.0 * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


class LatencyTracker:
    """Track signal-to-fill latencies with rolling percentile stats.

    Records latencies in milliseconds and provides p50/p90/p99 summaries.
    """

    def __init__(self, window: int = 200) -> None:
        self._window = max(10, window)
        self._samples: deque[float] = deque(maxlen=self._window)

    def record(self, latency_s: float) -> None:
        """Record a latency observation in seconds."""
        if latency_s < 0 or not math.isfinite(latency_s):
            return
        self._samples.append(latency_s * 1000.0)  # store in ms

    def stats(self) -> LatencyStats:
        """Compute summary statistics over the rolling window."""
        if not self._samples:
            return LatencyStats()
        sorted_vals = sorted(self._samples)
        return LatencyStats(
            n_samples=len(sorted_vals),
            p50_ms=round(_percentile(sorted_vals, 50), 2),
            p90_ms=round(_percentile(sorted_vals, 90), 2),
            p99_ms=round(_percentile(sorted_vals, 99), 2),
            max_ms=round(sorted_vals[-1], 2),
        )

    @property
    def latest_ms(self) -> float | None:
        """Return the most recent latency in ms, or None if empty."""
        return self._samples[-1] if self._samples else None


def kelly_size(
    edge: float,
    win_prob: float,
    fraction: float = 0.5,
    bankroll: float = 1000.0,
    max_bet: float = 100.0,
) -> float:
    """Half-Kelly sizing for a binary bet.

    Parameters:
      edge     — absolute |P_estimate - P_market|, after fees
      win_prob — our estimated TRUE probability of the event (NOT confidence)
      fraction — Kelly fraction (0.5 = half-Kelly)

    f* = (p * b - q) / b   where b = 1/price - 1, q = 1 - p.
    Returns USD amount, clamped to [0, max_bet].
    """
    if win_prob <= 0 or win_prob >= 1 or edge <= 0:
        return 0.0
    price = win_prob - edge  # market price (our estimate is win_prob)
    if price <= 0 or price >= 1:
        return 0.0
    b = (1.0 - price) / price  # net odds
    q = 1.0 - win_prob
    f_star = (win_prob * b - q) / b
    if f_star <= 0:
        return 0.0
    raw = bankroll * f_star * fraction
    return min(max(raw, 0.0), max_bet)
