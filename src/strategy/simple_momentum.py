"""
Simple momentum strategy.

Logic:
    * Compute the percentage change over the last ``window`` price observations.
    * If the change exceeds ``+threshold`` → BUY signal.
    * If the change is below ``-threshold`` → SELL signal.
    * Otherwise → HOLD.
"""

from __future__ import annotations

from typing import Sequence

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.base import Action, BaseStrategy, Signal
from src.utils.math_utils import pct_change


class SimpleMomentum(BaseStrategy):
    name = "simple_momentum"

    def __init__(self, cfg: Config) -> None:
        self.window = cfg.momentum_window
        self.threshold = cfg.momentum_threshold

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        price_history: Sequence[float],
    ) -> Signal:
        if len(price_history) < self.window:
            return Signal(
                Action.HOLD, 0.0, "Not enough history.",
                features={"history_len": len(price_history), "window": self.window},
            )

        recent = list(price_history[-self.window :])
        change = pct_change(recent[0], recent[-1])
        features = {
            "momentum": change,
            "window": self.window,
            "threshold": self.threshold,
            "history_len": len(price_history),
            "current_price": snapshot.price,
            "spread": snapshot.spread,
        }

        if change > self.threshold:
            return Signal(
                Action.BUY,
                min(abs(change) / self.threshold, 1.0),
                f"Momentum +{change:.2%} over {self.window} ticks.",
                features=features,
            )
        if change < -self.threshold:
            return Signal(
                Action.SELL,
                min(abs(change) / self.threshold, 1.0),
                f"Momentum {change:.2%} over {self.window} ticks.",
                features=features,
            )
        return Signal(
            Action.HOLD, 0.0, f"No momentum ({change:.2%}).",
            features=features,
        )
