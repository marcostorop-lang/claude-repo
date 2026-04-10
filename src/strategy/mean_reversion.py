"""
Simple mean-reversion strategy.

Logic:
    * Compute the z-score of the current price relative to recent history.
    * If z < -entry_z  → BUY  (price is "cheap" relative to recent mean).
    * If z > +entry_z  → SELL (price is "expensive").
    * If an existing position is open and |z| < exit_z → close (opposite signal).
    * Otherwise → HOLD.
"""

from __future__ import annotations

from typing import Sequence

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.base import Action, BaseStrategy, Signal
from src.utils.math_utils import z_score


class MeanReversion(BaseStrategy):
    name = "mean_reversion"

    def __init__(self, cfg: Config) -> None:
        self.window = cfg.mean_reversion_window
        self.entry_z = cfg.mean_reversion_entry_z
        self.exit_z = cfg.mean_reversion_exit_z

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        price_history: Sequence[float],
    ) -> Signal:
        if snapshot.price is None:
            return Signal(Action.HOLD, 0.0, "No price available.")

        if len(price_history) < self.window:
            return Signal(
                Action.HOLD, 0.0, "Not enough history.",
                features={"history_len": len(price_history), "window": self.window},
            )

        window = list(price_history[-self.window :])
        z = z_score(snapshot.price, window)
        from src.utils.math_utils import mean, stdev
        features = {
            "z_score": z,
            "window_mean": mean(window),
            "window_stdev": stdev(window),
            "window": self.window,
            "entry_z": self.entry_z,
            "current_price": snapshot.price,
            "spread": snapshot.spread,
            "history_len": len(price_history),
        }

        if z < -self.entry_z:
            return Signal(
                Action.BUY,
                min(abs(z) / self.entry_z, 1.0),
                f"Mean-reversion BUY: z={z:.2f}",
                features=features,
            )
        if z > self.entry_z:
            return Signal(
                Action.SELL,
                min(abs(z) / self.entry_z, 1.0),
                f"Mean-reversion SELL: z={z:.2f}",
                features=features,
            )
        if abs(z) < self.exit_z:
            return Signal(
                Action.HOLD, 0.0,
                f"Mean-reversion near mean: z={z:.2f}, consider closing.",
                features=features,
            )
        return Signal(Action.HOLD, 0.0, f"z={z:.2f}, within bands.", features=features)
