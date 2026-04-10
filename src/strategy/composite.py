"""
Composite multi-factor strategy.

Combines several independent signals into a weighted score:

1. **Momentum** — recent price trend (same as SimpleMomentum).
2. **Mean reversion** — z-score relative to recent mean.
3. **Spread quality** — narrow spread = higher quality signal.
4. **Time decay** — proximity to market resolution.

Each factor produces a score in [-1, 1] where positive = bullish.
The composite score is the weighted average.  When the score exceeds
a configurable threshold, a BUY signal fires; below -threshold, SELL.

This strategy is strictly opt-in — select it via ``STRATEGY=composite``.
"""

from __future__ import annotations

from typing import Sequence

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.base import Action, BaseStrategy, Signal
from src.utils.math_utils import mean, pct_change, stdev, z_score
from src.utils.time_utils import time_decay_factor


class CompositeStrategy(BaseStrategy):
    name = "composite"

    def __init__(self, cfg: Config) -> None:
        # Momentum sub-signal
        self.mom_window = cfg.momentum_window
        self.mom_threshold = cfg.momentum_threshold

        # Mean-reversion sub-signal
        self.mr_window = cfg.mean_reversion_window
        self.mr_entry_z = cfg.mean_reversion_entry_z

        # Spread quality
        self.max_spread = cfg.max_spread

        # Composite threshold for action — lower than single-factor because
        # momentum and MR are inherently opposed (trend vs. reversal) so the
        # composite rarely reaches the extremes.
        self.buy_threshold = 0.20
        self.sell_threshold = -0.20

        # Directional factor weights (sum to 1) — these decide BUY/SELL
        self.weights = {
            "momentum": 0.60,
            "mean_reversion": 0.40,
        }

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        price_history: Sequence[float],
    ) -> Signal:
        if snapshot.price is None:
            return Signal(Action.HOLD, 0.0, "No price available.")

        min_window = max(self.mom_window, self.mr_window)
        if len(price_history) < min_window:
            return Signal(
                Action.HOLD, 0.0,
                f"Not enough history (need {min_window}, have {len(price_history)}).",
                features={"history_len": len(price_history), "min_window": min_window},
            )

        # --- Factor 1: Momentum score [-1, 1] ---
        recent = list(price_history[-self.mom_window:])
        change = pct_change(recent[0], recent[-1])
        # Normalise: change / threshold, clamped to [-1, 1]
        momentum_score = max(-1.0, min(1.0, change / self.mom_threshold)) if self.mom_threshold > 0 else 0.0

        # --- Factor 2: Mean-reversion score [-1, 1] ---
        mr_window = list(price_history[-self.mr_window:])
        z = z_score(snapshot.price, mr_window)
        # Invert: negative z = price below mean = bullish for MR
        mr_score = max(-1.0, min(1.0, -z / self.mr_entry_z)) if self.mr_entry_z > 0 else 0.0

        # --- Factor 3: Spread quality [0, 1] → mapped to [-1, 1] ---
        spread = snapshot.spread or 0.0
        if self.max_spread > 0 and spread > 0:
            # spread_quality = 1 when spread is 0, 0 when spread >= max_spread
            spread_quality = max(0.0, 1.0 - spread / self.max_spread)
        else:
            spread_quality = 0.5  # unknown spread — neutral
        # Map [0, 1] to [-1, 1]: quality 1.0 → +1, quality 0 → -1
        spread_score = 2.0 * spread_quality - 1.0

        # --- Factor 4: Time decay [0.2, 1.0] ---
        decay = time_decay_factor(snapshot.end_date)
        # Map [0, 1] to [-1, 1]: decay 1.0 → +1, decay 0 → -1
        decay_score = 2.0 * decay - 1.0

        # --- Composite directional score (momentum + mean-reversion only) ---
        directional = (
            self.weights["momentum"] * momentum_score
            + self.weights["mean_reversion"] * mr_score
        )

        # Spread quality and time decay are confidence multipliers, not
        # directional factors.  They scale the confidence of any signal
        # but never create a signal on their own.
        quality_mult = spread_quality * decay  # both in [0, 1]

        features = {
            "composite_score": round(directional, 4),
            "momentum_score": round(momentum_score, 4),
            "momentum_raw": round(change, 6),
            "mr_score": round(mr_score, 4),
            "z_score": round(z, 4),
            "spread_score": round(spread_score, 4),
            "spread_quality": round(spread_quality, 4),
            "spread": spread,
            "decay_score": round(decay_score, 4),
            "time_decay": round(decay, 4),
            "quality_mult": round(quality_mult, 4),
            "current_price": snapshot.price,
            "history_len": len(price_history),
        }

        # Confidence = |directional| * quality_mult, clamped to [0, 1]
        confidence = min(abs(directional) * quality_mult, 1.0)

        if directional >= self.buy_threshold:
            return Signal(
                Action.BUY, confidence,
                f"Composite BUY: score={directional:.3f} (mom={momentum_score:.2f}, mr={mr_score:.2f}, qual={quality_mult:.2f})",
                features=features,
            )
        if directional <= self.sell_threshold:
            return Signal(
                Action.SELL, confidence,
                f"Composite SELL: score={directional:.3f} (mom={momentum_score:.2f}, mr={mr_score:.2f}, qual={quality_mult:.2f})",
                features=features,
            )

        return Signal(
            Action.HOLD, 0.0,
            f"Composite HOLD: score={directional:.3f}",
            features=features,
        )
