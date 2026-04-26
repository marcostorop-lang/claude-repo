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
from src.strategy.base import Action, BaseStrategy, Signal, compute_net_edge
from src.utils.math_utils import pct_change
from src.utils.time_utils import time_decay_factor


class SimpleMomentum(BaseStrategy):
    name = "simple_momentum"

    def __init__(self, cfg: Config) -> None:
        self.window = cfg.momentum_window
        self.threshold = cfg.momentum_threshold
        self.cfg = cfg

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
        decay = time_decay_factor(snapshot.end_date)
        # Decompose the gross move into post-cost net edge for the audit
        # trail (and for the optional gate below).  Always recorded in
        # features so calibration analysis can correlate net edge with
        # realised PnL even when the gate is disabled.
        net_breakdown = compute_net_edge(
            gross_edge=abs(change),
            spread=snapshot.spread,
            taker_fee_bps=getattr(self.cfg, "taker_fee_bps", 0.0),
        )
        features = {
            "momentum": change,
            "window": self.window,
            "threshold": self.threshold,
            "history_len": len(price_history),
            "current_price": snapshot.price,
            "spread": snapshot.spread,
            "time_decay": decay,
            **net_breakdown,
        }

        gate_on = getattr(self.cfg, "strategy_net_edge_gate_enabled", False)
        min_net = float(getattr(self.cfg, "strategy_min_net_edge", 0.0))
        if gate_on and net_breakdown["net_edge"] < min_net:
            return Signal(
                Action.HOLD, 0.0,
                f"Net edge {net_breakdown['net_edge']:+.4f} below min {min_net:.4f} after costs.",
                features=features,
            )

        if change > self.threshold:
            raw_conf = min(abs(change) / self.threshold, 1.0)
            return Signal(
                Action.BUY,
                raw_conf * decay,
                f"Momentum +{change:.2%} over {self.window} ticks (decay={decay:.2f}).",
                features=features,
            )
        if change < -self.threshold:
            raw_conf = min(abs(change) / self.threshold, 1.0)
            return Signal(
                Action.SELL,
                raw_conf * decay,
                f"Momentum {change:.2%} over {self.window} ticks (decay={decay:.2f}).",
                features=features,
            )
        return Signal(
            Action.HOLD, 0.0, f"No momentum ({change:.2%}).",
            features=features,
        )
