"""
Portfolio tracker.

Maintains a view of open positions, calculates P&L, and provides helpers
consumed by the risk manager and CLI commands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Position:
    token_id: str
    condition_id: str
    side: str
    size: float
    entry_price: float
    strategy: str
    order_id: str

    def unrealised_pnl(self, current_price: float) -> float:
        if self.side == "BUY":
            return (current_price - self.entry_price) * self.size
        return (self.entry_price - current_price) * self.size


@dataclass
class PortfolioTracker:
    """In-memory portfolio state."""

    positions: dict[str, Position] = field(default_factory=dict)
    realised_pnl: float = 0.0

    def open_position(self, pos: Position) -> None:
        self.positions[pos.token_id] = pos
        logger.info("Opened position: %s %s @ %.4f (size=%.4f)", pos.side, pos.token_id[:12], pos.entry_price, pos.size)

    def close_position(self, token_id: str, exit_price: float) -> float:
        """Close a position and return the realised P&L."""
        pos = self.positions.pop(token_id, None)
        if pos is None:
            logger.warning("No open position for token %s", token_id[:12])
            return 0.0
        pnl = pos.unrealised_pnl(exit_price)
        self.realised_pnl += pnl
        logger.info(
            "Closed position: %s %s @ %.4f → %.4f, PnL=%.4f",
            pos.side, token_id[:12], pos.entry_price, exit_price, pnl,
        )
        return pnl

    def open_position_count(self) -> int:
        return len(self.positions)

    def total_exposure(self) -> float:
        return sum(p.size * p.entry_price for p in self.positions.values())

    def total_unrealised_pnl(self, price_fn) -> float:
        """Calculate total unrealised P&L using a callable that returns the current price for a token_id."""
        total = 0.0
        for pos in self.positions.values():
            current = price_fn(pos.token_id)
            if current is not None:
                total += pos.unrealised_pnl(current)
        return total

    def summary(self, price_fn=None) -> dict:
        unrealised = self.total_unrealised_pnl(price_fn) if price_fn else 0.0
        return {
            "open_positions": self.open_position_count(),
            "total_exposure": self.total_exposure(),
            "realised_pnl": self.realised_pnl,
            "unrealised_pnl": unrealised,
            "net_pnl": self.realised_pnl + unrealised,
        }
