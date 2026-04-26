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
from src.strategy.base import Action, BaseStrategy, Signal, compute_net_edge
from src.utils.math_utils import z_score
from src.utils.time_utils import time_decay_factor


class MeanReversion(BaseStrategy):
    name = "mean_reversion"

    def __init__(self, cfg: Config) -> None:
        self.window = cfg.mean_reversion_window
        self.entry_z = cfg.mean_reversion_entry_z
        self.exit_z = cfg.mean_reversion_exit_z
        self.cfg = cfg

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
        decay = time_decay_factor(snapshot.end_date)
        from src.utils.math_utils import mean, stdev
        wmean = mean(window)
        # Expected reversion magnitude: how far we believe the price will
        # snap back toward the mean.  We treat that as the gross edge for
        # cost decomposition.  Floored at zero for safety.
        gross_edge = max(0.0, abs(snapshot.price - wmean))
        net_breakdown = compute_net_edge(
            gross_edge=gross_edge,
            spread=snapshot.spread,
            taker_fee_bps=getattr(self.cfg, "taker_fee_bps", 0.0),
        )
        features = {
            "z_score": z,
            "window_mean": wmean,
            "window_stdev": stdev(window),
            "window": self.window,
            "entry_z": self.entry_z,
            "current_price": snapshot.price,
            "spread": snapshot.spread,
            "history_len": len(price_history),
            "time_decay": decay,
            **net_breakdown,
        }

        gate_on = getattr(self.cfg, "strategy_net_edge_gate_enabled", False)
        min_net = float(getattr(self.cfg, "strategy_min_net_edge", 0.0))
        if gate_on and net_breakdown["net_edge"] < min_net:
            return Signal(
                Action.HOLD, 0.0,
                f"Net reversion {net_breakdown['net_edge']:+.4f} below min {min_net:.4f} after costs.",
                features=features,
            )

        if z < -self.entry_z:
            raw_conf = min(abs(z) / self.entry_z, 1.0)
            return Signal(
                Action.BUY,
                raw_conf * decay,
                f"Mean-reversion BUY: z={z:.2f} (decay={decay:.2f})",
                features=features,
            )
        if z > self.entry_z:
            raw_conf = min(abs(z) / self.entry_z, 1.0)
            return Signal(
                Action.SELL,
                raw_conf * decay,
                f"Mean-reversion SELL: z={z:.2f} (decay={decay:.2f})",
                features=features,
            )
        if abs(z) < self.exit_z:
            return Signal(
                Action.HOLD, 0.0,
                f"Mean-reversion near mean: z={z:.2f}, consider closing.",
                features=features,
            )
        return Signal(Action.HOLD, 0.0, f"z={z:.2f}, within bands.", features=features)
