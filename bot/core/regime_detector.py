"""Regime detector — classify market conditions from price volatility and volume.

Uses recent price history to detect whether the market is in a calm,
volatile, or trending regime.  In volatile regimes, strategies should
widen their minimum edge thresholds to avoid getting picked off.

This module is intentionally simple and stateless — it receives data
and returns a classification.  It does not manage any persistent state.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    CALM = "calm"
    VOLATILE = "volatile"
    TRENDING = "trending"


@dataclass(frozen=True)
class RegimeClassification:
    """Result of a regime detection analysis."""

    regime: Regime
    volatility: float  # annualized or raw std dev of returns
    edge_multiplier: float  # multiply min_edge by this (1.0 = no change)
    reason: str


# Default thresholds — these classify hourly returns
_VOL_CALM_THRESHOLD = 0.02  # below 2% → calm
_VOL_VOLATILE_THRESHOLD = 0.05  # above 5% → volatile
_TRENDING_R2_THRESHOLD = 0.6  # R^2 of linear fit above this → trending


def detect_regime(
    prices: list[float],
    volumes: list[float] | None = None,
    *,
    vol_calm: float = _VOL_CALM_THRESHOLD,
    vol_volatile: float = _VOL_VOLATILE_THRESHOLD,
    trending_r2: float = _TRENDING_R2_THRESHOLD,
) -> RegimeClassification:
    """Classify the current market regime from recent price data.

    Parameters
    ----------
    prices : list[float]
        Recent prices (at least 5 points), most recent last.
    volumes : list[float] | None
        Optional parallel volume data (same length as prices).
    vol_calm : float
        Volatility threshold below which regime is CALM.
    vol_volatile : float
        Volatility threshold above which regime is VOLATILE.
    trending_r2 : float
        R-squared threshold for TRENDING classification.
    """
    if len(prices) < 5:
        return RegimeClassification(
            regime=Regime.CALM,
            volatility=0.0,
            edge_multiplier=1.0,
            reason="Insufficient data (< 5 prices), defaulting to calm.",
        )

    # Compute log returns
    returns: list[float] = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0 and prices[i] > 0:
            r = math.log(prices[i] / prices[i - 1])
            if math.isfinite(r):
                returns.append(r)

    if len(returns) < 3:
        return RegimeClassification(
            regime=Regime.CALM,
            volatility=0.0,
            edge_multiplier=1.0,
            reason="Not enough valid returns, defaulting to calm.",
        )

    # Volatility: standard deviation of returns
    mean_ret = sum(returns) / len(returns)
    var = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
    volatility = math.sqrt(var)

    # Check for trending: compute R^2 of a linear fit to prices
    n = len(prices)
    x_mean = (n - 1) / 2.0
    y_mean = sum(prices) / n
    ss_xy = sum((i - x_mean) * (prices[i] - y_mean) for i in range(n))
    ss_xx = sum((i - x_mean) ** 2 for i in range(n))
    ss_yy = sum((prices[i] - y_mean) ** 2 for i in range(n))

    if ss_xx > 0 and ss_yy > 0:
        r_squared = (ss_xy ** 2) / (ss_xx * ss_yy)
    else:
        r_squared = 0.0

    # Classify
    if r_squared >= trending_r2 and volatility < vol_volatile:
        regime = Regime.TRENDING
        # Trending markets: slight increase to min edge (avoid chasing)
        edge_multiplier = 1.2
        reason = f"Trending regime: R^2={r_squared:.2f}, vol={volatility:.4f}"
    elif volatility >= vol_volatile:
        regime = Regime.VOLATILE
        # Volatile markets: widen edge threshold significantly
        edge_multiplier = 1.5 + (volatility - vol_volatile) * 5.0
        edge_multiplier = min(edge_multiplier, 3.0)  # cap at 3x
        reason = f"Volatile regime: vol={volatility:.4f} >= {vol_volatile}"
    else:
        regime = Regime.CALM
        edge_multiplier = 1.0
        reason = f"Calm regime: vol={volatility:.4f} < {vol_calm}"

    # Volume spike detection (optional overlay)
    if volumes and len(volumes) >= 5:
        recent_vol = sum(volumes[-3:]) / 3
        earlier_vol = sum(volumes[:-3]) / max(len(volumes) - 3, 1)
        if earlier_vol > 0 and recent_vol > earlier_vol * 2.0:
            # Volume spike → increase edge multiplier slightly
            edge_multiplier = min(edge_multiplier * 1.2, 3.0)
            reason += f" + volume spike ({recent_vol:.0f} vs {earlier_vol:.0f})"

    return RegimeClassification(
        regime=regime,
        volatility=round(volatility, 6),
        edge_multiplier=round(edge_multiplier, 2),
        reason=reason,
    )
