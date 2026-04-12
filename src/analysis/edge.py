"""
Edge detection model.

Estimates whether a prediction market token is mispriced by combining
multiple independent probability estimates and comparing them to the
market price.

    edge = P_estimated - P_market

A positive edge on a YES token means the market underprices the event.
We don't claim to know the "true" probability — we estimate it from
observable signals and look for situations where multiple estimators
agree the market is off.

This is explicitly not a "predict the news" model.  It's a "the market
structure suggests mispricing" model.  The signals are:

1. **Momentum-implied probability**: if price is trending to 0.70 from 0.55,
   momentum says the "real" price is ahead of market.  P_momentum > P_market.

2. **Mean-reversion-implied probability**: if the price overshot and is due
   to snap back, the "real" price is behind market.  P_mr < P_market.

3. **Spread-adjusted fair value**: wide spreads mean the midpoint is
   unreliable; the fair value is somewhere within the bid-ask range.

4. **Convergence pressure**: as resolution approaches, prices that aren't
   near 0 or 1 are inherently uncertain.  Discount edge near expiry.

The model produces:
- estimated_p: our best-guess P(YES)
- edge: estimated_p - market_price (positive = underpriced)
- edge_confidence: how much we trust this edge estimate [0, 1]
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from src.utils.math_utils import mean, pct_change, stdev
from src.utils.time_utils import time_decay_factor


@dataclass
class EdgeEstimate:
    """Result of the edge detection model for a single token."""

    market_price: float
    estimated_p: float
    edge: float  # estimated_p - market_price
    edge_confidence: float  # 0-1, how reliable is this edge estimate
    signals: dict = field(default_factory=dict)

    @property
    def has_edge(self) -> bool:
        """True if we believe there's a meaningful mispricing."""
        return abs(self.edge) > 0.03 and self.edge_confidence > 0.3

    @property
    def direction(self) -> str:
        if self.edge > 0.03:
            return "BUY"  # market underprices → buy YES
        if self.edge < -0.03:
            return "SELL"  # market overprices → sell YES
        return "HOLD"


def estimate_edge(
    price: float,
    price_history: Sequence[float],
    spread: float = 0.0,
    end_date: str = "",
    momentum_window: int = 3,
) -> EdgeEstimate:
    """Estimate the edge (mispricing) for a token.

    Parameters
    ----------
    price : current midpoint price (= market's P(YES))
    price_history : chronological prices
    spread : current bid-ask spread
    end_date : ISO date of market resolution
    momentum_window : ticks for momentum calculation
    """
    if price <= 0 or price >= 1 or len(price_history) < momentum_window + 1:
        return EdgeEstimate(
            market_price=price, estimated_p=price, edge=0.0, edge_confidence=0.0,
            signals={"reason": "insufficient_data"},
        )

    estimators: list[tuple[float, float]] = []  # (estimated_p, weight)

    # --- Estimator 1: Momentum extrapolation ---
    # If price has been trending from 0.55 → 0.60, momentum suggests the
    # "true" price may be slightly ahead: ~0.62
    recent = list(price_history[-momentum_window:])
    mom_change = pct_change(recent[0], recent[-1])
    # Project one-step ahead: current_price * (1 + change_per_step)
    if len(recent) > 1:
        per_step = mom_change / max(len(recent) - 1, 1)
        momentum_p = price * (1 + per_step)
        momentum_p = max(0.01, min(0.99, momentum_p))
        # Weight by strength of trend (weak trends get low weight)
        mom_weight = min(abs(mom_change) * 10, 1.0)
        estimators.append((momentum_p, mom_weight * 0.3))

    # --- Estimator 2: Mean-reversion anchor ---
    # The historical mean is where "average information" places the price.
    # If price deviates far, some reversion is expected.
    if len(price_history) >= 5:
        hist_mean = mean(list(price_history[-min(len(price_history), 20):]))
        hist_std = stdev(list(price_history[-min(len(price_history), 20):]))
        if hist_std > 0:
            z = (price - hist_mean) / hist_std
            # Light reversion: estimate pulls ~20% toward mean
            mr_p = price - 0.2 * (price - hist_mean)
            mr_p = max(0.01, min(0.99, mr_p))
            # Weight: stronger z-score = more weight for MR
            mr_weight = min(abs(z) / 3.0, 1.0) * 0.3
            estimators.append((mr_p, mr_weight))

    # --- Estimator 3: Spread-adjusted fair value ---
    # The midpoint is only fair if bid and ask are equally informed.
    # Wide spreads suggest the midpoint is unreliable.
    if spread > 0:
        # Uncertainty band: [price - spread/2, price + spread/2]
        # Fair value is the midpoint (which IS the price), but our confidence
        # in it is inversely proportional to spread width
        spread_confidence = max(0.0, 1.0 - spread / 0.15)  # 15%+ spread = 0 confidence
        # The fair value estimate is just the price, but with low confidence
        estimators.append((price, spread_confidence * 0.2))

    # --- Estimator 4: Binary convergence (near resolution) ---
    decay = time_decay_factor(end_date)
    if decay < 0.7:
        # Near resolution, the price should be near 0 or 1.
        # If it's in the middle (0.3-0.7), something is uncertain.
        # Estimate: price will continue toward whichever pole it's closest to.
        if price > 0.5:
            convergence_p = price + (1.0 - price) * 0.1 * (1.0 - decay)
        else:
            convergence_p = price - price * 0.1 * (1.0 - decay)
        convergence_p = max(0.01, min(0.99, convergence_p))
        estimators.append((convergence_p, (1.0 - decay) * 0.2))

    # --- Weighted average of estimators ---
    if not estimators:
        return EdgeEstimate(
            market_price=price, estimated_p=price, edge=0.0, edge_confidence=0.0,
            signals={"reason": "no_estimators"},
        )

    total_weight = sum(w for _, w in estimators)
    if total_weight <= 0:
        return EdgeEstimate(
            market_price=price, estimated_p=price, edge=0.0, edge_confidence=0.0,
            signals={"reason": "zero_weight"},
        )

    estimated_p = sum(p * w for p, w in estimators) / total_weight
    estimated_p = max(0.01, min(0.99, estimated_p))
    edge = estimated_p - price

    # --- Edge confidence ---
    # High when: (1) estimators agree with each other, (2) enough data, (3) low spread
    # Low when: estimators disagree, thin data, wide spread
    estimator_ps = [p for p, w in estimators if w > 0.05]
    if len(estimator_ps) >= 2:
        agreement = 1.0 - min(stdev(estimator_ps) * 10, 1.0)
    else:
        agreement = 0.3

    data_quality = min(len(price_history) / 15.0, 1.0)
    spread_quality = max(0.0, 1.0 - spread / 0.10) if spread > 0 else 0.5

    edge_confidence = (agreement * 0.4 + data_quality * 0.3 + spread_quality * 0.3)
    edge_confidence = max(0.0, min(1.0, edge_confidence)) * decay

    signals = {
        "estimated_p": round(estimated_p, 4),
        "edge": round(edge, 4),
        "edge_confidence": round(edge_confidence, 4),
        "n_estimators": len(estimators),
        "total_weight": round(total_weight, 4),
        "agreement": round(agreement, 4),
        "data_quality": round(data_quality, 4),
        "spread_quality": round(spread_quality, 4),
        "time_decay": round(decay, 4),
        "momentum_change": round(mom_change, 6) if 'mom_change' in dir() else 0.0,
    }

    return EdgeEstimate(
        market_price=price,
        estimated_p=estimated_p,
        edge=edge,
        edge_confidence=edge_confidence,
        signals=signals,
    )
