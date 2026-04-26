"""
Base class for trading strategies.

Every strategy must implement ``evaluate()`` which receives a market snapshot
and recent price history and returns a ``Signal``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from src.polymarket.market_data import MarketSnapshot


class Action(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    """Output of a strategy evaluation.

    The ``features`` dict is a free-form bag of numeric features the strategy
    used to make its decision (e.g. momentum, z-score, recent volatility).
    It is persisted to ``decision_log.features`` as JSON so post-hoc analysis
    can correlate features with realised outcomes.
    """

    action: Action
    confidence: float  # 0.0 – 1.0
    reason: str = ""
    features: dict[str, Any] = field(default_factory=dict)


class BaseStrategy(ABC):
    """Interface that all strategies must implement."""

    name: str = "base"

    @abstractmethod
    def evaluate(
        self,
        snapshot: MarketSnapshot,
        price_history: Sequence[float],
    ) -> Signal:
        """Evaluate a market and return a trading signal."""
        ...


def compute_net_edge(
    gross_edge: float,
    spread: float,
    taker_fee_bps: float,
) -> dict:
    """Decompose a strategy's expected gross move into a net-of-cost edge.

    Returns a dict with the components for inclusion in
    ``Signal.features`` so the audit trail records the exact PnL math
    the strategy assumed when it decided to trade.

    ``half_spread`` is the per-leg cost when crossing on entry; we
    deliberately do *not* double for exit because round-trip costs
    are accrued by the executor on each leg separately.
    """
    half_spread = max(0.0, float(spread)) / 2.0
    fee_pct = max(0.0, float(taker_fee_bps)) / 10000.0
    net = float(gross_edge) - half_spread - fee_pct
    return {
        "gross_edge": float(gross_edge),
        "half_spread": half_spread,
        "fee_pct": fee_pct,
        "net_edge": net,
    }
