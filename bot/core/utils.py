"""Shared utilities — data classes, helpers, notification stubs."""

from __future__ import annotations

import time
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
    confidence: float  # 0–1
    size_usd: float = 0.0  # filled by risk manager
    reason: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


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


def kelly_size(
    edge: float,
    win_prob: float,
    fraction: float = 0.5,
    bankroll: float = 1000.0,
    max_bet: float = 100.0,
) -> float:
    """Half-Kelly sizing for a binary bet.

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
