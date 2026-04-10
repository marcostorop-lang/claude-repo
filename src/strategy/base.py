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
