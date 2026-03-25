"""
Base class for trading strategies.

Every strategy must implement ``evaluate()`` which receives a market snapshot
and recent price history and returns a ``Signal``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from src.polymarket.market_data import MarketSnapshot


class Action(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    """Output of a strategy evaluation."""

    action: Action
    confidence: float  # 0.0 – 1.0
    reason: str = ""


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
