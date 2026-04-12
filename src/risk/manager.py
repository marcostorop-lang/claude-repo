"""
Risk manager.

Enforces position sizing, exposure limits, stop-loss, take-profit,
daily loss circuit breaker, concentration limits, and price boundary rules
before any order is sent to the execution engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

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
        # Daily loss tracking
        self._daily_realized_pnl: float = 0.0
        self._current_date: date = date.today()
        self._circuit_breaker_tripped: bool = False

    # ------------------------------------------------------------------
    # Daily loss tracking
    # ------------------------------------------------------------------

    def record_realized_pnl(self, pnl: float) -> None:
        """Track realized PnL for daily circuit breaker."""
        self._maybe_reset_daily()
        self._daily_realized_pnl += pnl
        if self._daily_realized_pnl <= -self.cfg.max_daily_loss:
            self._circuit_breaker_tripped = True
            logger.warning(
                "CIRCUIT BREAKER TRIPPED: daily loss $%.2f exceeds limit $%.2f",
                abs(self._daily_realized_pnl), self.cfg.max_daily_loss,
            )

    def _maybe_reset_daily(self) -> None:
        """Reset daily counters if the date has changed."""
        today = date.today()
        if today != self._current_date:
            if self._circuit_breaker_tripped:
                logger.info("Circuit breaker reset for new day.")
            self._daily_realized_pnl = 0.0
            self._current_date = today
            self._circuit_breaker_tripped = False

    @property
    def is_circuit_breaker_active(self) -> bool:
        self._maybe_reset_daily()
        return self._circuit_breaker_tripped

    @property
    def daily_pnl(self) -> float:
        self._maybe_reset_daily()
        return self._daily_realized_pnl

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Dynamic position sizing
    # ------------------------------------------------------------------

    def compute_position_size(
        self,
        price: float,
        confidence: float,
        liquidity: float = 0.0,
        edge: float | None = None,
    ) -> float:
        """Compute the proposed position size in shares.

        Base size = max_position_size / price, then optionally scaled by:
        - Edge-aware fractional-Kelly (when ``edge`` is supplied and
          ``SIZING_EDGE_KELLY=true``): size *= |edge| * confidence * kelly_fraction
          capped at 1.0 to avoid oversizing.
        - Signal confidence (linear: size *= confidence)
          — skipped when edge-Kelly is active (edge-Kelly already uses confidence).
        - Available liquidity (cap at max_liquidity_fraction of reported liquidity)
        """
        if price <= 0:
            return 0.0
        base_usd = self.cfg.max_position_size

        # Edge-aware Kelly sizing takes precedence when enabled and edge is known.
        # For prediction markets, edge ≈ P_estimated - P_market.  The Kelly
        # fraction for a binary bet with known edge and true probability p is
        # f* = (p*b - q) / b where b is net odds = 1/price - 1.
        # As a well-behaved proxy we use |edge| * confidence * kelly_fraction,
        # which is linear in edge and trivially bounded in [0, 1].
        if self.cfg.sizing_edge_kelly and edge is not None and abs(edge) > 0:
            # Pure edge-Kelly: size = base * min(|edge| * confidence * kelly_fraction, 1)
            # Tiny edges → tiny positions; filter via MIN_EDGE_FOR_TRADE if
            # execution costs would swamp them.
            kelly_mult = min(abs(edge) * max(confidence, 0.0) * self.cfg.kelly_fraction, 1.0)
            base_usd *= kelly_mult
            logger.debug(
                "Edge-Kelly sizing: edge=%+.4f conf=%.3f frac=%.2f → mult=%.4f (base=$%.2f)",
                edge, confidence, self.cfg.kelly_fraction, kelly_mult, base_usd,
            )
        elif self.cfg.sizing_confidence_scale and confidence > 0:
            # Legacy confidence-only scaling
            base_usd *= min(confidence, 1.0)

        # Liquidity cap: never take more than X% of reported market liquidity
        if liquidity > 0 and self.cfg.max_liquidity_fraction > 0:
            max_usd_from_liq = liquidity * self.cfg.max_liquidity_fraction
            if base_usd > max_usd_from_liq:
                logger.debug(
                    "Sizing capped by liquidity: $%.2f -> $%.2f (%.1f%% of $%.0f)",
                    base_usd, max_usd_from_liq,
                    self.cfg.max_liquidity_fraction * 100, liquidity,
                )
                base_usd = max_usd_from_liq

        return base_usd / price

    # Pre-trade risk check
    # ------------------------------------------------------------------

    def check(
        self,
        token_id: str,
        signal: Signal,
        proposed_size: float,
        price: float,
        spread: float = 0.0,
        category: str = "",
    ) -> RiskVerdict:
        """Evaluate whether a trade should proceed and at what size."""

        # HOLD signals need no risk check
        if signal.action == Action.HOLD:
            return RiskVerdict(False, 0.0, "HOLD signal — no trade.")

        # --- Circuit breaker ---
        if self.is_circuit_breaker_active and signal.action == Action.BUY:
            return RiskVerdict(False, 0.0, f"Circuit breaker: daily loss ${abs(self._daily_realized_pnl):.2f} exceeds limit.")

        # --- Duplicate position prevention ---
        if signal.action == Action.BUY and token_id in self.portfolio.positions:
            return RiskVerdict(False, 0.0, "Already have an open position for this token.")

        # --- Minimum edge gate (only when strategy reports an edge) ---
        # The edge-based strategy publishes the signed edge in features.
        # If configured, reject trades below the threshold even if confidence
        # is high (e.g., high confidence of a 0.005 edge is not worth trading).
        if self.cfg.min_edge_for_trade > 0:
            sig_edge = signal.features.get("edge") if signal.features else None
            if sig_edge is not None:
                try:
                    if abs(float(sig_edge)) < self.cfg.min_edge_for_trade:
                        return RiskVerdict(
                            False, 0.0,
                            f"Edge {float(sig_edge):+.4f} below min {self.cfg.min_edge_for_trade:.4f}.",
                        )
                except (TypeError, ValueError):
                    pass

        # --- Spread check ---
        if spread > 0 and spread > self.cfg.max_spread:
            return RiskVerdict(False, 0.0, f"Spread {spread:.4f} exceeds max {self.cfg.max_spread:.4f}.")

        # --- Price boundary filter ---
        if signal.action == Action.BUY and price > 0:
            if price < self.cfg.min_price:
                return RiskVerdict(False, 0.0, f"Price {price:.4f} below min {self.cfg.min_price:.4f} (near-zero, low edge).")
            if price > self.cfg.max_price:
                return RiskVerdict(False, 0.0, f"Price {price:.4f} above max {self.cfg.max_price:.4f} (near-certain, low edge).")

        # --- Max open positions ---
        open_count = self.portfolio.open_position_count()
        if signal.action == Action.BUY and open_count >= self.cfg.max_open_positions:
            return RiskVerdict(False, 0.0, f"Max open positions ({self.cfg.max_open_positions}) reached.")

        # --- Per-event concentration limit ---
        if signal.action == Action.BUY:
            event_exposure = self.portfolio.exposure_by_condition(token_id)
            if event_exposure >= self.cfg.max_exposure_per_event:
                return RiskVerdict(False, 0.0, f"Event exposure ${event_exposure:.2f} exceeds limit ${self.cfg.max_exposure_per_event:.2f}.")

        # --- Cross-event category correlation limit ---
        if signal.action == Action.BUY and category:
            cat_exposure = self.portfolio.exposure_by_category(category)
            if cat_exposure >= self.cfg.max_exposure_per_category:
                return RiskVerdict(
                    False, 0.0,
                    f"Category '{category}' exposure ${cat_exposure:.2f} exceeds limit ${self.cfg.max_exposure_per_category:.2f}.",
                )
            cat_count = self.portfolio.position_count_by_category(category)
            if cat_count >= self.cfg.max_positions_per_category:
                return RiskVerdict(
                    False, 0.0,
                    f"Category '{category}' already has {cat_count} positions (max {self.cfg.max_positions_per_category}).",
                )

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

    # ------------------------------------------------------------------
    # Stop-loss / Take-profit
    # ------------------------------------------------------------------

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
