"""
Pairs / cointegration strategy on legs of the same Polymarket condition.

Edge thesis
-----------
Polymarket events with N outcomes have legs whose prices must sum to
1.0 at resolution.  In life, the sum oscillates around 1.0 — sometimes
above (when both legs are momentarily bid up by uninformed flow),
sometimes below (the negative-risk arb regime that the dedicated
``NegRiskArbExecutor`` already harvests).

The neg-risk executor only operates the *one-sided* arb when the sum
is far below 1.0.  Most of the time the sum sits inside a band that
no static threshold catches but **does** revert.  The pairs/coint
strategy harvests that **statistical** reversion:

* For every condition_id seen with N≥2 legs, maintain a rolling
  window of ``residuals``: ``residual_t = sum(prices_t) - 1.0``.
* Compute the rolling z-score of ``residual_t``.  When ``|z|`` clears
  ``PAIRS_ENTRY_Z`` we have evidence the sum is unusually far from
  fair, and we trade the **leg whose price is most extreme** in
  the direction that pulls the sum back toward 1.

This is *direction-only*: a ``BUY`` on the leg the strategy thinks
will rise, a ``SELL`` on the leg it thinks will fall.  The
combination of legs across multiple ticks naturally hedges itself.

What this is *not*
------------------
* Not the structural arb operated by ``neg_risk_arb_executor.py``.
  Coint targets *temporal* mispricings of the sum, not sustained
  arb opportunities.
* Not market-making.  We always cross the spread to enter.
* Not a substitute for risk caps — the same exposure / liquidity /
  spread gates apply.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Sequence

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.base import Action, BaseStrategy, Signal, compute_net_edge


@dataclass
class _CondState:
    """Per-condition running state.

    ``residuals`` is the rolling window of ``sum(prices_t) - 1.0``;
    ``token_prices`` is the latest tick's snapshot per token, so we
    can tell which leg is most extreme when a signal fires.
    """

    residuals: deque
    token_prices: dict[str, float]


def _z(values: Sequence[float], x: float) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mu = sum(values) / n
    var = sum((v - mu) ** 2 for v in values) / (n - 1)
    if var <= 0:
        return 0.0
    return (x - mu) / (var ** 0.5)


class PairsCointegrationStrategy(BaseStrategy):
    """Trades temporal mispricings of the sum-of-leg-prices.

    Notes
    -----
    Holds an internal cache keyed by ``condition_id`` so it sees the
    *same* condition's legs across ticks.  The bot's market-data
    service feeds it one snapshot per token per tick, so the state
    grows linearly with active conditions; a soft cap drops the
    oldest residual once the deque is full.
    """

    name = "pairs_cointegration"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.window = max(8, int(getattr(cfg, "pairs_window", 30)))
        self.entry_z = float(getattr(cfg, "pairs_entry_z", 1.5))
        self.min_legs = max(2, int(getattr(cfg, "pairs_min_legs", 2)))
        self.max_residual = float(getattr(cfg, "pairs_max_abs_residual", 0.5))
        self._states: dict[str, _CondState] = {}

    # ------------------------------------------------------------------
    # State maintenance
    # ------------------------------------------------------------------

    def _state_for(self, condition_id: str) -> _CondState:
        s = self._states.get(condition_id)
        if s is None:
            s = _CondState(
                residuals=deque(maxlen=self.window),
                token_prices={},
            )
            self._states[condition_id] = s
        return s

    def _record(self, snap: MarketSnapshot) -> _CondState:
        st = self._state_for(snap.condition_id)
        # Update *this* leg's price; the residual is computed from the
        # union of all leg prices we've seen this tick.  We make no
        # attempt to detect "tick boundaries" — the window is
        # dimensionless, and cross-tick noise just becomes part of
        # the rolling variance estimator.
        if snap.price is not None and snap.price > 0:
            st.token_prices[snap.token_id] = float(snap.price)
            if len(st.token_prices) >= self.min_legs:
                residual = sum(st.token_prices.values()) - 1.0
                # Reject pathological residuals — when one leg is
                # mid-resolution or the API is mid-failure the sum
                # can be wildly off.
                if abs(residual) <= self.max_residual:
                    st.residuals.append(residual)
        return st

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        price_history: Sequence[float],
    ) -> Signal:
        if snapshot.price is None or not snapshot.condition_id:
            return Signal(Action.HOLD, 0.0, "No price or condition_id.")

        st = self._record(snapshot)
        n = len(st.residuals)
        if n < max(8, self.window // 2):
            return Signal(
                Action.HOLD, 0.0,
                f"Warming up ({n}/{self.window}).",
                features={"n_residuals": n, "n_legs": len(st.token_prices)},
            )

        latest = st.residuals[-1]
        z = _z(list(st.residuals), latest)
        net = compute_net_edge(
            gross_edge=abs(z * (sum((r - sum(st.residuals) / n) ** 2 for r in st.residuals) / max(n - 1, 1)) ** 0.5),
            spread=snapshot.spread or 0.0,
            taker_fee_bps=getattr(self.cfg, "taker_fee_bps", 0.0),
        )
        features = {
            "residual": latest,
            "z_score": z,
            "n_residuals": n,
            "n_legs": len(st.token_prices),
            "entry_z": self.entry_z,
            **net,
        }

        if abs(z) < self.entry_z:
            return Signal(
                Action.HOLD, 0.0,
                f"Residual z={z:+.2f} inside band ±{self.entry_z}.",
                features=features,
            )

        # Direction logic.
        # - Sum currently *above* 1 (residual > 0) → market is over-pricing
        #   the legs.  Most-extreme leg (highest above its in-sample
        #   mean) should fall back → SELL it.
        # - Sum *below* 1 (residual < 0) → market is under-pricing.
        #   Most-extreme leg below mean should recover → BUY it.
        # We can only act on the leg we're currently looking at, which
        # may not be the *most* extreme — but if its sign matches the
        # residual sign, it is at least *contributing* to the
        # mispricing, and the trade is direction-correct in expectation.
        # When the snapshot's leg is on the "wrong" side, HOLD.
        leg_sign = 1.0 if snapshot.price >= 0.5 else -1.0
        if z > 0:
            # Sum too high → SELL on the bull leg (price > 0.5).
            if leg_sign < 0:
                return Signal(
                    Action.HOLD, 0.0,
                    "Residual too high but this leg is the cheap side.",
                    features=features,
                )
            confidence = min((abs(z) - self.entry_z) / max(1.0, self.entry_z) + 0.5, 1.0)
            return Signal(
                Action.SELL, confidence,
                f"Pairs SELL: residual {latest:+.4f} (z={z:+.2f}); "
                f"sum-of-legs above 1.",
                features=features,
            )
        # z < -entry_z
        if leg_sign > 0:
            return Signal(
                Action.HOLD, 0.0,
                "Residual too low but this leg is the rich side.",
                features=features,
            )
        confidence = min((abs(z) - self.entry_z) / max(1.0, self.entry_z) + 0.5, 1.0)
        return Signal(
            Action.BUY, confidence,
            f"Pairs BUY: residual {latest:+.4f} (z={z:+.2f}); "
            f"sum-of-legs below 1.",
            features=features,
        )
