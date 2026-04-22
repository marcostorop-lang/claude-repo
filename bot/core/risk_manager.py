"""Risk manager — position sizing, exposure limits, drawdown control.

Every TradeSignal passes through ``approve()`` before execution.
The risk manager never places orders; it only validates and sizes.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from bot.config import cfg
from bot.core.utils import Side, TradeSignal, kelly_size

logger = logging.getLogger(__name__)

_REJECTIONS_LOG = Path(__file__).resolve().parent.parent / "logs" / "rejections.jsonl"

# Correlation-aware sizing: reduce by this factor per existing position
# in the same category.
_CATEGORY_CORRELATION_DISCOUNT = 0.30


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
    category: str = ""  # market category for correlation-aware sizing

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
        self._lock = threading.Lock()
        self.positions: dict[str, Position] = {}  # token_id → Position
        self._daily_pnl: float = 0.0
        self._daily_date: date = date.today()
        self._peak_equity: float = cfg.starting_capital_usd
        self._current_equity: float = cfg.starting_capital_usd
        self._total_realized_pnl: float = 0.0
        self._circuit_breaker: bool = False
        self._rejection_counts: dict[str, int] = {}

        # Per-strategy daily loss tracking
        self._strategy_daily_pnl: dict[str, float] = {}
        self._strategy_paused: dict[str, bool] = {}

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

    def record_pnl(self, pnl: float, strategy: str = "") -> None:
        with self._lock:
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

            # Per-strategy daily loss tracking
            if strategy:
                self._strategy_daily_pnl.setdefault(strategy, 0.0)
                self._strategy_daily_pnl[strategy] += pnl
                if self._strategy_daily_pnl[strategy] <= -cfg.strategy_daily_loss_limit_usd:
                    self._strategy_paused[strategy] = True
                    logger.warning(
                        "STRATEGY PAUSED: %s daily loss $%.2f exceeds limit $%.2f",
                        strategy,
                        abs(self._strategy_daily_pnl[strategy]),
                        cfg.strategy_daily_loss_limit_usd,
                    )

    def _maybe_reset_daily(self) -> None:
        today = date.today()
        if today != self._daily_date:
            self._daily_pnl = 0.0
            self._daily_date = today
            self._circuit_breaker = False
            self._strategy_daily_pnl.clear()
            self._strategy_paused.clear()

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

    def is_strategy_paused(self, strategy: str) -> bool:
        """Check if a specific strategy is paused due to its daily loss limit."""
        self._maybe_reset_daily()
        return self._strategy_paused.get(strategy, False)

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def open_position(self, signal: TradeSignal, fill_price: float, fill_size: float) -> None:
        with self._lock:
            pos = Position(
                token_id=signal.token_id,
                condition_id=signal.condition_id,
                side=signal.side,
                size=fill_size,
                entry_price=fill_price,
                strategy=signal.strategy,
                category=getattr(signal, "category", ""),
            )
            self.positions[signal.token_id] = pos
            logger.info(
                "Position opened: %s %s %.2f @ %.4f ($%.2f) [%s]",
                pos.side.value, pos.token_id[:12], pos.size,
                pos.entry_price, pos.notional, pos.strategy,
            )

    def close_position(self, token_id: str, exit_price: float, strategy: str = "") -> float:
        with self._lock:
            pos = self.positions.pop(token_id, None)
            if pos is None:
                return 0.0
            strat = strategy or pos.strategy
        # Release lock before calling record_pnl (which acquires lock)
        if pos.side == Side.BUY:
            pnl = (exit_price - pos.entry_price) * pos.size
        else:
            pnl = (pos.entry_price - exit_price) * pos.size
        self.record_pnl(pnl, strategy=strat)
        logger.info(
            "Position closed: %s %s PnL=$%.2f (entry=%.4f exit=%.4f)",
            pos.side.value, token_id[:12], pnl, pos.entry_price, exit_price,
        )
        return pnl

    @property
    def total_exposure(self) -> float:
        with self._lock:
            return sum(p.notional for p in self.positions.values())

    @property
    def position_count(self) -> int:
        with self._lock:
            return len(self.positions)

    def exposure_for_condition(self, condition_id: str) -> float:
        with self._lock:
            return sum(
                p.notional for p in self.positions.values()
                if p.condition_id == condition_id
            )

    # ------------------------------------------------------------------
    # Chain reconciliation (#4)
    # ------------------------------------------------------------------

    def reconcile_positions(self, on_chain_balances: dict[str, float]) -> list[dict]:
        """Compare tracked positions against on-chain balances.

        ``on_chain_balances`` maps token_id → actual share count.
        Logs warnings for mismatches and adjusts internal tracking.
        Returns a list of mismatch records for observability.
        """
        mismatches: list[dict] = []
        with self._lock:
            all_tokens = set(self.positions.keys()) | set(on_chain_balances.keys())

            for token_id in all_tokens:
                tracked = self.positions.get(token_id)
                on_chain_size = on_chain_balances.get(token_id, 0.0)
                tracked_size = tracked.size if tracked else 0.0

                if abs(tracked_size - on_chain_size) < 0.01:
                    continue  # within tolerance

                record = {
                    "token_id": token_id[:16],
                    "tracked_size": round(tracked_size, 4),
                    "on_chain_size": round(on_chain_size, 4),
                    "delta": round(on_chain_size - tracked_size, 4),
                    "action": "",
                }

                if tracked and on_chain_size > 0:
                    # Size mismatch — adjust tracked position
                    logger.warning(
                        "RECONCILE mismatch: %s tracked=%.4f on_chain=%.4f, adjusting",
                        token_id[:12], tracked_size, on_chain_size,
                    )
                    tracked.size = on_chain_size
                    record["action"] = "adjusted"
                elif tracked and on_chain_size == 0.0:
                    # We think we have a position but chain says no
                    logger.warning(
                        "RECONCILE phantom: %s tracked=%.4f but not on-chain, removing",
                        token_id[:12], tracked_size,
                    )
                    del self.positions[token_id]
                    record["action"] = "removed_phantom"
                elif not tracked and on_chain_size > 0:
                    # On-chain position we don't know about
                    logger.warning(
                        "RECONCILE unknown: %s on_chain=%.4f but not tracked",
                        token_id[:12], on_chain_size,
                    )
                    record["action"] = "unknown_on_chain"

                mismatches.append(record)

        return mismatches

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

        # Per-strategy circuit breaker
        if self.is_strategy_paused(signal.strategy):
            return _reject(f"Strategy {signal.strategy} paused (daily loss limit).")

        # Already have position in this token
        if signal.side == Side.BUY and signal.token_id in self.positions:
            return _reject("Already have open position for this token.")

        # Max positions
        if signal.side == Side.BUY and self.position_count >= cfg.max_positions:
            return _reject(f"Max positions ({cfg.max_positions}) reached.")

        # Compute Kelly size — win_prob is the TRUE probability estimate,
        # NOT the confidence score.
        raw_size = kelly_size(
            edge=abs(signal.edge),
            win_prob=signal.probability,
            fraction=cfg.kelly_fraction * min(signal.confidence, 1.0),
            bankroll=self._current_equity,
            max_bet=cfg.max_position_usd,
        )
        if raw_size <= 0:
            return _reject("Kelly size is zero (edge or confidence too low).")

        # Correlation-aware sizing (#11)
        # If there are already open positions in the same category, reduce size.
        category = getattr(signal, "category", "") or ""
        if category:
            with self._lock:
                same_cat_count = sum(
                    1 for p in self.positions.values()
                    if p.category == category
                )
            if same_cat_count > 0:
                discount = max(0.1, 1.0 - same_cat_count * _CATEGORY_CORRELATION_DISCOUNT)
                raw_size *= discount
                logger.debug(
                    "Correlation discount: %d positions in category '%s', "
                    "size reduced to %.1f%%",
                    same_cat_count, category, discount * 100,
                )

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
    # Position exit scanner
    # ------------------------------------------------------------------

    async def check_exits(self, get_book_fn) -> list[dict]:
        """Scan open positions for stop-loss, take-profit, or expiration.

        ``get_book_fn`` must be an async callable(token_id) → BookSnapshot.
        Returns a list of exit records for logging.
        """
        exits: list[dict] = []
        now = time.time()

        with self._lock:
            token_ids = list(self.positions.keys())

        for token_id in token_ids:
            with self._lock:
                pos = self.positions.get(token_id)
            if pos is None:
                continue

            reason = ""
            exit_price = 0.0

            # Check max hold time
            hold_hours = (now - pos.timestamp) / 3600.0
            if hold_hours >= cfg.max_hold_hours:
                reason = f"max_hold ({hold_hours:.1f}h >= {cfg.max_hold_hours}h)"

            # Fetch current price
            if not reason:
                try:
                    book = await get_book_fn(token_id)
                    mid = book.midpoint
                    if mid <= 0:
                        continue
                except Exception:
                    logger.debug("Book fetch failed for exit check on %s", token_id[:12])
                    continue
            else:
                try:
                    book = await get_book_fn(token_id)
                    mid = book.midpoint if book.midpoint > 0 else pos.entry_price
                except Exception:
                    mid = pos.entry_price

            exit_price = mid

            if not reason:
                # Compute unrealized PnL percentage
                if pos.side == Side.BUY:
                    pnl_pct = (mid - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0
                else:
                    pnl_pct = (pos.entry_price - mid) / pos.entry_price if pos.entry_price > 0 else 0

                if pnl_pct <= -cfg.stop_loss_pct:
                    reason = f"stop_loss ({pnl_pct:.1%} <= -{cfg.stop_loss_pct:.0%})"
                elif pnl_pct >= cfg.take_profit_pct:
                    reason = f"take_profit ({pnl_pct:.1%} >= {cfg.take_profit_pct:.0%})"

            if reason:
                pnl = self.close_position(token_id, exit_price)
                record = {
                    "event": "position_exit",
                    "token_id": token_id[:16],
                    "side": pos.side.value,
                    "entry_price": round(pos.entry_price, 4),
                    "exit_price": round(exit_price, 4),
                    "size": round(pos.size, 4),
                    "pnl": round(pnl, 4),
                    "reason": reason,
                    "strategy": pos.strategy,
                    "hold_hours": round((now - pos.timestamp) / 3600.0, 1),
                    "timestamp": now,
                }
                logger.info(
                    "EXIT | %s | %s @ %.4f → %.4f | PnL=$%.2f | %s",
                    pos.side.value, token_id[:12], pos.entry_price,
                    exit_price, pnl, reason,
                )
                exits.append(record)

        return exits

    # ------------------------------------------------------------------
    # Brier gate for live trading
    # ------------------------------------------------------------------

    def check_calibration_gate(self) -> tuple[bool, str]:
        """Check if calibration is sufficient for live trading.

        Returns (passed, message).  Only relevant when cfg.is_live.
        """
        if not cfg.is_live:
            return True, "Paper mode — no calibration gate."

        try:
            from bot.core.calibration import compute_metrics
            m = compute_metrics()

            if m.n_resolved < cfg.min_resolved_estimates:
                return False, (
                    f"Calibration gate FAILED: only {m.n_resolved} resolved estimates "
                    f"(need {cfg.min_resolved_estimates}). Keep running in paper mode."
                )

            if m.brier_score > cfg.max_brier_score:
                return False, (
                    f"Calibration gate FAILED: Brier score {m.brier_score:.4f} "
                    f"> max {cfg.max_brier_score:.4f}. Oracle needs better calibration."
                )

            return True, (
                f"Calibration gate PASSED: {m.n_resolved} resolved, "
                f"Brier={m.brier_score:.4f} (limit {cfg.max_brier_score:.4f})."
            )
        except Exception:
            logger.exception("Calibration gate check failed.")
            return False, "Calibration gate check error — refusing live trading."

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
