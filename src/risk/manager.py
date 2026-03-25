"""
Risk manager.

Enforces position sizing, exposure limits, stop-loss and take-profit rules
before any order is sent to the execution engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.strategy.base import Action, Signal

logger = logging.getLogger(__name__)


@dataclass
class RiskVerdict:
    allowed: bool
    adjusted_size: float
    reason: str


class RiskManager:
    """Gate-keeper that validates and adjusts proposed trades."""

    def __init__(self, cfg: Config, portfolio: PortfolioTracker) -> None:
        self.cfg = cfg
        self.portfolio = portfolio

    def check(self, token_id: str, signal: Signal, proposed_size: float, price: float) -> RiskVerdict:
        """Evaluate whether a trade should proceed and at what size."""

        # HOLD signals need no risk check
        if signal.action == Action.HOLD:
            return RiskVerdict(False, 0.0, "HOLD signal — no trade.")

        # --- Max open positions ---
        open_count = self.portfolio.open_position_count()
        if signal.action == Action.BUY and open_count >= self.cfg.max_open_positions:
            return RiskVerdict(False, 0.0, f"Max open positions ({self.cfg.max_open_positions}) reached.")

        # --- Position size cap ---
        size = min(proposed_size, self.cfg.max_position_size)

        # --- Total exposure cap ---
        current_exposure = self.portfolio.total_exposure()
        cost = size * price
        if current_exposure + cost > self.cfg.max_total_exposure:
            available = self.cfg.max_total_exposure - current_exposure
            if available <= 0:
                return RiskVerdict(False, 0.0, "Max total exposure reached.")
            size = available / price
            logger.info("Reduced size to %.4f to stay within exposure limit.", size)

        if size <= 0:
            return RiskVerdict(False, 0.0, "Effective size is zero.")

        return RiskVerdict(True, size, "Risk check passed.")

    def check_stop_loss(self, entry_price: float, current_price: float) -> bool:
        """Return True if current loss exceeds stop-loss threshold."""
        if entry_price == 0:
            return False
        loss_pct = (entry_price - current_price) / entry_price
        return loss_pct >= self.cfg.stop_loss_pct

    def check_take_profit(self, entry_price: float, current_price: float) -> bool:
        """Return True if current gain exceeds take-profit threshold."""
        if entry_price == 0:
            return False
        gain_pct = (current_price - entry_price) / entry_price
        return gain_pct >= self.cfg.take_profit_pct
