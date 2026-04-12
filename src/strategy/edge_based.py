"""
Edge-based strategy.

Replaces noisy weighted averaging of opposed signals (momentum + mean-reversion)
with a principled edge-detection approach:

    edge = P_estimated - P_market

The strategy uses :func:`src.analysis.edge.estimate_edge` to produce a
probability estimate from multiple independent estimators and compares it with
the market midpoint.  When a positive edge is detected with sufficient
confidence, a BUY signal fires (market underprices the event).  A negative
edge triggers SELL.

A second layer — :func:`src.analysis.fundamentals.compute_fundamentals` — is
used as a **confidence gate**, not as a direction source.  Poor fundamentals
(widening spreads, volume/price divergence, thin data) down-weight the edge;
strong fundamentals amplify it.

Design invariants:
- Fundamentals NEVER flip direction — they only scale confidence.
- Edge magnitude and estimator agreement drive confidence.
- If the store is unavailable the strategy degrades gracefully to pure edge.

Select via ``STRATEGY=edge_based``.
"""

from __future__ import annotations

from typing import Sequence

from src.analysis.edge import estimate_edge
from src.analysis.fundamentals import compute_fundamentals, fundamentals_score
from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import Action, BaseStrategy, Signal


class EdgeBasedStrategy(BaseStrategy):
    name = "edge_based"

    def __init__(self, cfg: Config, store: SQLiteStore | None = None) -> None:
        self.cfg = cfg
        self.store = store

        # Edge thresholds — how much mispricing do we need to act?
        # An edge of 0.03 = we think the market is 3 cents off fair value.
        self.min_edge = 0.03
        self.min_edge_confidence = 0.30

        # Fundamentals modifier range: score in [-1, 1] → multiplier in [0.5, 1.25]
        # This keeps fundamentals from killing a signal outright but can tilt it.
        self.fund_floor = 0.5
        self.fund_ceiling = 1.25

        # Momentum window passed to edge estimator
        self.momentum_window = max(cfg.momentum_window, 3)

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        price_history: Sequence[float],
    ) -> Signal:
        if snapshot.price is None:
            return Signal(Action.HOLD, 0.0, "No price available.")

        # Need enough history for the edge estimator
        if len(price_history) < self.momentum_window + 1:
            return Signal(
                Action.HOLD, 0.0,
                f"Not enough history (need {self.momentum_window + 1}, "
                f"have {len(price_history)}).",
                features={"history_len": len(price_history)},
            )

        # --- Primary signal: edge detection ---
        edge_est = estimate_edge(
            price=snapshot.price,
            price_history=list(price_history),
            spread=snapshot.spread or 0.0,
            end_date=snapshot.end_date,
            momentum_window=self.momentum_window,
        )

        # --- Confidence modifier: fundamentals ---
        fund_score = 0.0
        fund_features: dict = {}
        if self.store is not None:
            try:
                fund = compute_fundamentals(
                    token_id=snapshot.token_id,
                    store=self.store,
                    current_price=snapshot.price,
                    current_spread=snapshot.spread or 0.0,
                    current_volume=snapshot.volume,
                    current_liquidity=snapshot.liquidity,
                )
                fund_score = fundamentals_score(fund)
                fund_features = fund.raw
            except Exception:
                # Fundamentals are best-effort — never fail the strategy
                fund_score = 0.0

        # Map fundamentals score [-1, 1] → multiplier [floor, ceiling]
        # score = -1 → floor (0.5), score = 0 → 1.0, score = +1 → ceiling (1.25)
        if fund_score >= 0:
            fund_mult = 1.0 + fund_score * (self.fund_ceiling - 1.0)
        else:
            fund_mult = 1.0 + fund_score * (1.0 - self.fund_floor)
        fund_mult = max(self.fund_floor, min(self.fund_ceiling, fund_mult))

        # Scale edge confidence by fundamentals quality
        effective_confidence = min(edge_est.edge_confidence * fund_mult, 1.0)

        features = {
            "market_price": round(snapshot.price, 4),
            "estimated_p": round(edge_est.estimated_p, 4),
            "edge": round(edge_est.edge, 4),
            "edge_confidence_raw": round(edge_est.edge_confidence, 4),
            "fund_score": round(fund_score, 4),
            "fund_mult": round(fund_mult, 4),
            "effective_confidence": round(effective_confidence, 4),
            "history_len": len(price_history),
            "spread": snapshot.spread or 0.0,
            **{f"fund_{k}": v for k, v in fund_features.items()},
            **{f"edge_{k}": v for k, v in edge_est.signals.items()},
        }

        # --- Decision ---
        # No edge or insufficient confidence → HOLD
        if abs(edge_est.edge) < self.min_edge:
            return Signal(
                Action.HOLD, 0.0,
                f"No edge: |{edge_est.edge:+.4f}| < {self.min_edge}",
                features=features,
            )

        if effective_confidence < self.min_edge_confidence:
            return Signal(
                Action.HOLD, 0.0,
                f"Low confidence: {effective_confidence:.3f} < "
                f"{self.min_edge_confidence} (edge={edge_est.edge:+.3f}, "
                f"fund={fund_score:+.2f})",
                features=features,
            )

        # Positive edge → market underprices YES → BUY
        if edge_est.edge > 0:
            return Signal(
                Action.BUY, effective_confidence,
                f"Edge BUY: est={edge_est.estimated_p:.3f} "
                f"vs mkt={snapshot.price:.3f} "
                f"(edge={edge_est.edge:+.3f}, conf={effective_confidence:.2f}, "
                f"fund={fund_score:+.2f})",
                features=features,
            )

        # Negative edge → market overprices YES → SELL
        return Signal(
            Action.SELL, effective_confidence,
            f"Edge SELL: est={edge_est.estimated_p:.3f} "
            f"vs mkt={snapshot.price:.3f} "
            f"(edge={edge_est.edge:+.3f}, conf={effective_confidence:.2f}, "
            f"fund={fund_score:+.2f})",
            features=features,
        )
