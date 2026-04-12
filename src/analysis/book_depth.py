"""
Order book depth analysis.

Analyses the full order book (not just top-of-book) to answer:

1. **Can we fill without moving the price?**
   If our order size exceeds the depth at the best level, we'll eat into
   worse price levels — the effective price is worse than the quoted spread.

2. **Is the book balanced?**
   Heavy bids + light asks = buying pressure.  Heavy asks + light bids = selling.
   Imbalance signals imminent price movement.

3. **What's the real cost of execution (market impact)?**
   Walk the book with our desired size and compute the volume-weighted
   average price (VWAP) of the fill.

These signals are used at execution time (not for every scanned market)
since fetching the full book is expensive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class BookLevel:
    """One price level in the order book."""
    price: float
    size: float


@dataclass
class BookAnalysis:
    """Result of analysing an order book."""

    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    midpoint: float = 0.0

    # Depth: total size available within X% of midpoint
    bid_depth_1pct: float = 0.0  # total bid size within 1% of mid
    ask_depth_1pct: float = 0.0  # total ask size within 1% of mid
    bid_depth_5pct: float = 0.0
    ask_depth_5pct: float = 0.0

    # Book imbalance: positive = buy pressure, negative = sell pressure
    # Calculated as (bid_depth - ask_depth) / (bid_depth + ask_depth)
    imbalance_1pct: float = 0.0
    imbalance_5pct: float = 0.0

    # VWAP for a hypothetical fill
    vwap_buy: float = 0.0  # what we'd actually pay to buy N shares
    vwap_sell: float = 0.0  # what we'd actually receive selling N shares
    slippage_buy_pct: float = 0.0  # (vwap_buy - best_ask) / best_ask
    slippage_sell_pct: float = 0.0  # (best_bid - vwap_sell) / best_bid

    # Number of levels
    n_bid_levels: int = 0
    n_ask_levels: int = 0

    # Can we fill at the top level?
    can_fill_at_top: bool = True

    raw: dict = field(default_factory=dict)


def parse_book_levels(raw_levels: list) -> list[BookLevel]:
    """Parse order book levels from the CLOB SDK response."""
    levels = []
    for item in raw_levels:
        try:
            price = float(getattr(item, "price", None) or (item.get("price") if isinstance(item, dict) else None))
            size = float(getattr(item, "size", None) or (item.get("size") if isinstance(item, dict) else None))
            levels.append(BookLevel(price=price, size=size))
        except (TypeError, ValueError, AttributeError):
            continue
    return levels


def analyze_book(
    bids: list[BookLevel],
    asks: list[BookLevel],
    fill_size_usd: float = 50.0,
) -> BookAnalysis:
    """Analyse an order book and return depth metrics.

    Parameters
    ----------
    bids : sorted descending by price (best bid first)
    asks : sorted ascending by price (best ask first)
    fill_size_usd : hypothetical order size for VWAP calculation
    """
    result = BookAnalysis()

    if not bids or not asks:
        return result

    # Sort defensively
    bids = sorted(bids, key=lambda l: l.price, reverse=True)
    asks = sorted(asks, key=lambda l: l.price)

    result.best_bid = bids[0].price
    result.best_ask = asks[0].price
    result.spread = result.best_ask - result.best_bid
    result.midpoint = (result.best_bid + result.best_ask) / 2.0
    result.n_bid_levels = len(bids)
    result.n_ask_levels = len(asks)

    mid = result.midpoint
    if mid <= 0:
        return result

    # --- Depth at various thresholds ---
    for lvl in bids:
        dist = (mid - lvl.price) / mid
        if dist <= 0.01:
            result.bid_depth_1pct += lvl.size * lvl.price
        if dist <= 0.05:
            result.bid_depth_5pct += lvl.size * lvl.price

    for lvl in asks:
        dist = (lvl.price - mid) / mid
        if dist <= 0.01:
            result.ask_depth_1pct += lvl.size * lvl.price
        if dist <= 0.05:
            result.ask_depth_5pct += lvl.size * lvl.price

    # --- Imbalance ---
    total_1 = result.bid_depth_1pct + result.ask_depth_1pct
    total_5 = result.bid_depth_5pct + result.ask_depth_5pct
    if total_1 > 0:
        result.imbalance_1pct = (result.bid_depth_1pct - result.ask_depth_1pct) / total_1
    if total_5 > 0:
        result.imbalance_5pct = (result.bid_depth_5pct - result.ask_depth_5pct) / total_5

    # --- VWAP for hypothetical BUY (walk asks) ---
    remaining = fill_size_usd
    filled_value = 0.0
    filled_shares = 0.0
    for lvl in asks:
        level_usd = lvl.size * lvl.price
        take = min(remaining, level_usd)
        shares = take / lvl.price if lvl.price > 0 else 0
        filled_value += take
        filled_shares += shares
        remaining -= take
        if remaining <= 0:
            break
    if filled_shares > 0:
        result.vwap_buy = filled_value / filled_shares
        if result.best_ask > 0:
            result.slippage_buy_pct = (result.vwap_buy - result.best_ask) / result.best_ask
    result.can_fill_at_top = (remaining <= 0 and filled_shares > 0 and result.slippage_buy_pct < 0.005)

    # --- VWAP for hypothetical SELL (walk bids) ---
    remaining = fill_size_usd
    filled_value = 0.0
    filled_shares = 0.0
    for lvl in bids:
        level_usd = lvl.size * lvl.price
        take = min(remaining, level_usd)
        shares = take / lvl.price if lvl.price > 0 else 0
        filled_value += take
        filled_shares += shares
        remaining -= take
        if remaining <= 0:
            break
    if filled_shares > 0:
        result.vwap_sell = filled_value / filled_shares
        if result.best_bid > 0:
            result.slippage_sell_pct = (result.best_bid - result.vwap_sell) / result.best_bid

    result.raw = {
        "spread": round(result.spread, 4),
        "bid_depth_1pct": round(result.bid_depth_1pct, 2),
        "ask_depth_1pct": round(result.ask_depth_1pct, 2),
        "imbalance_1pct": round(result.imbalance_1pct, 4),
        "imbalance_5pct": round(result.imbalance_5pct, 4),
        "vwap_buy": round(result.vwap_buy, 4),
        "slippage_buy_pct": round(result.slippage_buy_pct, 4),
        "can_fill_at_top": result.can_fill_at_top,
        "n_bid_levels": result.n_bid_levels,
        "n_ask_levels": result.n_ask_levels,
    }

    return result
