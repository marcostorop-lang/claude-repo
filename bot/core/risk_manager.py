"""Risk manager — position sizing, exposure limits, drawdown control.

Every TradeSignal passes through ``approve()`` before execution.
The risk manager never places orders; it only validates and sizes.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from bot.config import cfg
from bot.core.utils import Side, TradeSignal, kelly_size

logger = logging.getLogger(__name__)

_REJECTIONS_LOG = Path(__file__).resolve().parent.parent / "logs" / "rejections.jsonl"


@dataclass
class Position:
    """An open position tracked by the risk manager."""

    token_id: str
    condition_id: str
    side: Side
    size: float
    entry_price: float
    strategy: str
    timestamp: float = field(default_factory=time.time)

    @property
    def notional(self) -> float:
        return self.size * self.entry_price


@dataclass
class RiskVerdict:
    """Result of a risk check."""

    approved: bool
    adjusted_size_usd: float = 0.0
    reason: str = ""


class RiskManager:
    """Centralized pre-trade gatekeeper for all strategies."""

    def __init__(self) -> None:
        self.positions: dict[str, Position] = {}  # token_id → Position
        self._daily_pnl: float = 0.0
        self._daily_date: date = date.today()
        self._peak_equity: float = cfg.starting_capital_usd
        self._current_equity: float = cfg.starting_capital_usd
        self._total_realized_pnl: float = 0.0
        self._circuit_breaker: bool = False
        self._rejection_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Rejection logging
    # ------------------------------------------------------------------

    def _record_rejection(self, signal: TradeSignal, reason: str) -> None:
        """Count + persist a rejection so we can analyse missed trades."""
        key = reason.split(".")[0].lower().strip()
        self._rejection_counts[key] = self._rejection_counts.get(key, 0) + 1
        try:
            _REJECTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(_REJECTIONS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "strategy": signal.strategy,
                    "condition_id": signal.condition_id,
                    "token_id": signal.token_id,
                    "side": signal.side.value,
                    "price": signal.price,
                    "edge": signal.edge,
                    "confidence": signal.confidence,
                    "reason": reason,
                }) + "\n")
        except Exception:
            logger.debug("Rejection log write failed.", exc_info=True)

    @property
    def rejection_counts(self) -> dict[str, int]:
        return dict(self._rejection_counts)

    # ------------------------------------------------------------------
    # Equity / PnL tracking
    # ------------------------------------------------------------------

    def record_pnl(self, pnl: float) -> None:
        self._maybe_reset_daily()
        self._daily_pnl += pnl
        self._total_realized_pnl += pnl
        self._current_equity += pnl
        self._peak_equity = max(self._peak_equity, self._current_equity)

        if self._daily_pnl <= -cfg.max_daily_loss_usd:
            self._circuit_breaker = True
            logger.warning(
                "CIRCUIT BREAKER: daily loss $%.2f exceeds limit $%.2f",
                abs(self._daily_pnl), cfg.max_daily_loss_usd,
            )

    def _maybe_reset_daily(self) -> None:
        today = date.today()
        if today != self._daily_date:
            self._daily_pnl = 0.0
            self._daily_date = today
            self._circuit_breaker = False

    @property
    def drawdown_pct(self) -> float:
        if self._peak_equity <= 0:
            return 0.0
        return (self._peak_equity - self._current_equity) / self._peak_equity

    @property
    def is_halted(self) -> bool:
        self._maybe_reset_daily()
        if self._circuit_breaker:
            return True
        if self.drawdown_pct >= cfg.max_drawdown_pct:
            logger.warning(
                "DRAWDOWN STOP: %.1f%% (limit %.1f%%)",
                self.drawdown_pct * 100, cfg.max_drawdown_pct * 100,
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def open_position(self, signal: TradeSignal, fill_price: float, fill_size: float) -> None:
        pos = Position(
            token_id=signal.token_id,
            condition_id=signal.condition_id,
            side=signal.side,
            size=fill_size,
            entry_price=fill_price,
            strategy=signal.strategy,
        )
        self.positions[signal.token_id] = pos
        logger.info(
            "Position opened: %s %s %.2f @ %.4f ($%.2f) [%s]",
            pos.side.value, pos.token_id[:12], pos.size,
            pos.entry_price, pos.notional, pos.strategy,
        )

    def close_position(self, token_id: str, exit_price: float) -> float:
        pos = self.positions.pop(token_id, None)
        if pos is None:
            return 0.0
        if pos.side == Side.BUY:
            pnl = (exit_price - pos.entry_price) * pos.size
        else:
            pnl = (pos.entry_price - exit_price) * pos.size
        self.record_pnl(pnl)
        logger.info(
            "Position closed: %s %s PnL=$%.2f (entry=%.4f exit=%.4f)",
            pos.side.value, token_id[:12], pnl, pos.entry_price, exit_price,
        )
        return pnl

    @property
    def total_exposure(self) -> float:
        return sum(p.notional for p in self.positions.values())

    @property
    def position_count(self) -> int:
        return len(self.positions)

    def exposure_for_condition(self, condition_id: str) -> float:
        return sum(
            p.notional for p in self.positions.values()
            if p.condition_id == condition_id
        )

    # ------------------------------------------------------------------
    # Pre-trade approval
    # ------------------------------------------------------------------

    def approve(self, signal: TradeSignal) -> RiskVerdict:
        """Validate a signal and compute position size.  Returns a verdict."""
        self._maybe_reset_daily()

        def _reject(reason: str) -> RiskVerdict:
            self._record_rejection(signal, reason)
            return RiskVerdict(False, reason=reason)

        # Sanity: price must be in (0, 1)
        if signal.price <= 0.0 or signal.price >= 1.0:
            return _reject(f"Invalid price {signal.price:.4f} (must be 0 < p < 1).")

        # Circuit breaker / drawdown
        if self.is_halted:
            return _reject("Trading halted (circuit breaker or drawdown).")

        # Already have position in this token
        if signal.side == Side.BUY and signal.token_id in self.positions:
            return _reject("Already have open position for this token.")

        # Max positions
        if signal.side == Side.BUY and self.position_count >= cfg.max_positions:
            return _reject(f"Max positions ({cfg.max_positions}) reached.")

        # Compute Kelly size
        raw_size = kelly_size(
            edge=abs(signal.edge),
            win_prob=signal.confidence,
            fraction=cfg.kelly_fraction,
            bankroll=self._current_equity,
            max_bet=cfg.max_position_usd,
        )
        if raw_size <= 0:
            return _reject("Kelly size is zero (edge or confidence too low).")

        # Concentration limit
        cond_exposure = self.exposure_for_condition(signal.condition_id)
        max_cond = self._current_equity * cfg.max_concentration_pct
        if cond_exposure + raw_size > max_cond:
            raw_size = max(0, max_cond - cond_exposure)
            if raw_size <= 0:
                return _reject("Concentration limit reached for this event.")

        # Total exposure cap
        if self.total_exposure + raw_size > cfg.max_total_exposure_usd:
            raw_size = max(0, cfg.max_total_exposure_usd - self.total_exposure)
            if raw_size <= 0:
                return _reject("Total exposure limit reached.")

        # Floor
        if raw_size < 1.0:
            return _reject("Position size too small ($<1).")

        return RiskVerdict(
            approved=True,
            adjusted_size_usd=raw_size,
            reason="Approved.",
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        return {
            "equity": round(self._current_equity, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "total_pnl": round(self._total_realized_pnl, 2),
            "drawdown_pct": round(self.drawdown_pct * 100, 1),
            "positions": self.position_count,
            "exposure": round(self.total_exposure, 2),
            "halted": self.is_halted,
        }
