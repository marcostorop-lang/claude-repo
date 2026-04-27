"""
A/B shadow runner.

Evaluates a *second* strategy in parallel to the live one on every
tick.  All the shadow's PnL accounting happens in-memory + persists
to ``shadow_decision_log`` and ``shadow_calibration``.  Crucially:

* The shadow never touches ``PortfolioTracker`` (the live one) or the
  ``ExecutionEngine``.  No order is ever placed on its behalf.
* Slippage is simulated identically to the live paper executor
  (mid + half-spread) so the comparison is apples-to-apples.
* SL/TP thresholds come from the same ``Config`` so the comparison
  isolates the *signal*, not the risk parameters.

Why a separate module
---------------------
A second strategy in the live ``_tick`` would tangle two PnL streams,
confuse the dashboard, and tempt operators to start trading on shadow
signals before the protocol of ``validate-strategy`` has cleared
them.  This isolation makes it impossible to mix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.portfolio.tracker import PortfolioTracker, Position
from src.strategy.base import Action, BaseStrategy, Signal
from src.utils.time_utils import iso_now

logger = logging.getLogger(__name__)


@dataclass
class ShadowState:
    """Light read-only view exposed in ``bot_state.json``."""

    enabled: bool
    strategy: str
    open_positions: int
    realised_pnl: float
    unrealised_pnl: float
    fees_paid: float


class ShadowRunner:
    """Runs ``shadow_strategy`` against the same market data.

    Signal acceptance is gated by the *same* SL/TP / spread / price-
    boundary rules the live bot would apply, but exposure caps come
    from a separate counter so the shadow can hold a different number
    of positions if it wants.  We keep the rules identical to live in
    every other respect to make the A/B comparison meaningful.
    """

    def __init__(self, cfg: Config, strategy: BaseStrategy) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.portfolio = PortfolioTracker()

    # ------------------------------------------------------------------
    # Public API consumed by the main tick.
    # ------------------------------------------------------------------

    def evaluate_exits(self, store, get_price) -> None:
        """Walk open shadow positions and close any that meet SL/TP.

        Mirrors the live exit pass but writes to ``shadow_calibration``
        / ``shadow_decision_log``.  Stale-price (None) positions are
        skipped — the shadow is paper-only, so a zombie close would be
        gratuitous noise rather than capital protection.
        """
        tick_ts = iso_now()
        to_close: list[tuple[str, str, float]] = []
        for token_id, pos in list(self.portfolio.positions.items()):
            current_price = get_price(token_id)
            if current_price is None:
                continue
            entry = pos.entry_price
            if entry <= 0:
                continue
            loss_pct = (entry - current_price) / entry
            gain_pct = (current_price - entry) / entry
            reason = ""
            if loss_pct >= self.cfg.stop_loss_pct:
                reason = "stop_loss"
            elif gain_pct >= self.cfg.take_profit_pct:
                reason = "take_profit"
            if reason:
                to_close.append((token_id, reason, current_price))
        for token_id, reason, price in to_close:
            pos = self.portfolio.positions.get(token_id)
            if pos is None:
                continue
            pnl = self.portfolio.close_position(token_id, price)
            entry = pos.entry_price
            return_pct = (price - entry) / entry if entry > 0 else 0.0
            try:
                store.update_shadow_calibration_exit(
                    token_id=token_id,
                    exit_timestamp=tick_ts,
                    exit_price=price,
                    exit_reason=reason,
                    pnl=pnl,
                    return_pct=return_pct,
                )
                store.insert_shadow_decision(
                    timestamp=tick_ts, token_id=token_id,
                    condition_id=pos.condition_id,
                    action=f"EXIT_{reason.upper()}",
                    reason=f"shadow PnL={pnl:.4f}",
                    strategy=self.strategy.name,
                    price=price,
                )
            except Exception:
                logger.debug("Shadow exit persist failed", exc_info=True)

    def evaluate_entries(
        self,
        snapshots: list[MarketSnapshot],
        store,
        price_history_loader,
    ) -> None:
        """Run the shadow strategy on each filtered snapshot, simulate
        the BUY/SELL fill, and persist the decision."""
        tick_ts = iso_now()
        for snap in snapshots:
            try:
                history = price_history_loader(snap.token_id) or []
                sig = self.strategy.evaluate(snap, history)
            except Exception:
                logger.debug("Shadow strategy raised on %s", snap.token_id[:12], exc_info=True)
                continue
            if sig.action == Action.HOLD:
                continue
            # Mirror the live entry filters that have nothing to do
            # with order routing: price boundaries, spread cap.
            if snap.price < self.cfg.min_price or snap.price > self.cfg.max_price:
                continue
            if snap.spread > 0 and snap.spread > self.cfg.max_spread:
                continue
            # Skip a duplicate entry on the same token — keeps shadow
            # accounting clean (no averaging-in vs. averaging-down
            # ambiguity vs. the live bot's behaviour).
            if snap.token_id in self.portfolio.positions:
                continue
            half_spread = max(0.0, snap.spread) / 2.0
            if sig.action == Action.BUY:
                fill = snap.price + half_spread
            else:
                fill = max(snap.price - half_spread, 1e-4)
            if fill <= 0:
                continue
            size = self.cfg.max_position_size / fill
            try:
                self.portfolio.open_position(Position(
                    token_id=snap.token_id,
                    condition_id=snap.condition_id,
                    side=sig.action.value,
                    size=size,
                    entry_price=fill,
                    strategy=self.strategy.name,
                    order_id=f"shadow:{tick_ts}:{snap.token_id[:8]}",
                    entry_timestamp=tick_ts,
                    category=snap.category if hasattr(snap, "category") else "",
                ))
                store.insert_shadow_calibration_entry(
                    entry_timestamp=tick_ts,
                    token_id=snap.token_id,
                    strategy=self.strategy.name,
                    confidence=sig.confidence,
                    entry_price=fill,
                    features=sig.features,
                )
                store.insert_shadow_decision(
                    timestamp=tick_ts, token_id=snap.token_id,
                    condition_id=snap.condition_id,
                    action=f"SHADOW_ENTRY_{sig.action.value}",
                    reason=sig.reason or "",
                    strategy=self.strategy.name,
                    confidence=sig.confidence,
                    price=fill, spread=snap.spread,
                    signal_detail=sig.reason,
                    features=sig.features,
                )
            except Exception:
                logger.debug("Shadow entry persist failed", exc_info=True)

    # ------------------------------------------------------------------
    # State export for ``bot_state.json``
    # ------------------------------------------------------------------

    def state(self, get_price) -> ShadowState:
        unrealised = self.portfolio.total_unrealised_pnl(get_price)
        return ShadowState(
            enabled=True,
            strategy=self.strategy.name,
            open_positions=self.portfolio.open_position_count(),
            realised_pnl=self.portfolio.realised_pnl,
            unrealised_pnl=unrealised,
            fees_paid=self.portfolio.fees_paid,
        )
