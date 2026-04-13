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
    entry_timestamp: str = ""
    category: str = ""

    def unrealised_pnl(self, current_price: float) -> float:
        if self.side == "BUY":
            return (current_price - self.entry_price) * self.size
        return (self.entry_price - current_price) * self.size

    @property
    def notional_exposure(self) -> float:
        return self.size * self.entry_price


@dataclass
class PortfolioTracker:
    """In-memory portfolio state."""

    positions: dict[str, Position] = field(default_factory=dict)
    realised_pnl: float = 0.0
    # Cumulative execution fees paid.  Stays at 0.0 unless the opt-in fee
    # model (TAKER_FEE_BPS / MAKER_FEE_BPS) is enabled.  Tracked separately
    # from realised_pnl so the gross trading signal stays uncontaminated —
    # the dashboard computes net = realised_pnl - fees_paid explicitly.
    fees_paid: float = 0.0

    def open_position(self, pos: Position) -> None:
        """Open a new position or add to an existing one.

        If a position already exists for the same token and side, the two are
        merged with a size-weighted average entry price.  If the existing
        position is on the opposite side, the incoming fill reduces/flips it
        via :meth:`close_position` semantics — opposing BUY against a SELL
        short realises P&L against the short's entry first.

        This preserves accounting correctness when multiple fills accumulate
        on the same token across ticks (e.g. scaling in, partial fills that
        complete on a later tick, or a re-entry after a partial exit).
        """
        existing = self.positions.get(pos.token_id)
        if existing is None:
            self.positions[pos.token_id] = pos
            logger.info(
                "Opened position: %s %s @ %.4f (size=%.4f)",
                pos.side, pos.token_id[:12], pos.entry_price, pos.size,
            )
            return

        # Same-side → size-weighted average entry.
        if existing.side == pos.side:
            total_size = existing.size + pos.size
            if total_size <= 0:
                # Degenerate — replace with new.
                self.positions[pos.token_id] = pos
                return
            new_entry = (
                existing.entry_price * existing.size + pos.entry_price * pos.size
            ) / total_size
            existing.size = total_size
            existing.entry_price = new_entry
            # Preserve original strategy/order_id/entry_timestamp for audit; only
            # the *aggregate* size+entry_price track live state.
            logger.info(
                "Added to position: %s %s +%.4f @ %.4f → size=%.4f, avg_entry=%.4f",
                pos.side, pos.token_id[:12], pos.size, pos.entry_price,
                existing.size, existing.entry_price,
            )
            return

        # Opposite side → treat as (partial) close of the existing position.
        # This handles the "SELL against a BUY" and vice versa case symmetrically.
        close_qty = min(pos.size, existing.size)
        self.close_position(pos.token_id, pos.entry_price, size=close_qty)
        remainder = pos.size - close_qty
        if remainder > 1e-9:
            # More incoming than existing — the residual opens a new position
            # on the *incoming* side (a flip).
            new_pos = Position(
                token_id=pos.token_id,
                condition_id=pos.condition_id,
                side=pos.side,
                size=remainder,
                entry_price=pos.entry_price,
                strategy=pos.strategy,
                order_id=pos.order_id,
                entry_timestamp=pos.entry_timestamp,
                category=pos.category,
            )
            self.positions[pos.token_id] = new_pos
            logger.info(
                "Flipped position: now %s %s size=%.4f @ %.4f",
                new_pos.side, new_pos.token_id[:12], new_pos.size, new_pos.entry_price,
            )

    def close_position(
        self,
        token_id: str,
        exit_price: float,
        size: float | None = None,
    ) -> float:
        """Close a position (full or partial) and return the realised P&L.

        If ``size`` is None or >= the current position size, the position is
        fully closed and removed.  Otherwise a partial close is booked:
        realised P&L for the closed slice is recorded and the remaining size
        stays open at the original entry price (partial exits do not change
        cost basis).
        """
        pos = self.positions.get(token_id)
        if pos is None:
            logger.warning("No open position for token %s", token_id[:12])
            return 0.0

        close_qty = pos.size if size is None else min(size, pos.size)
        if close_qty <= 0:
            return 0.0

        if pos.side == "BUY":
            pnl = (exit_price - pos.entry_price) * close_qty
        else:
            pnl = (pos.entry_price - exit_price) * close_qty
        self.realised_pnl += pnl

        remaining = pos.size - close_qty
        if remaining <= 1e-9:
            self.positions.pop(token_id, None)
            logger.info(
                "Closed position: %s %s @ %.4f → %.4f, size=%.4f, PnL=%.4f",
                pos.side, token_id[:12], pos.entry_price, exit_price, close_qty, pnl,
            )
        else:
            pos.size = remaining
            logger.info(
                "Partial close: %s %s -%.4f @ %.4f, remaining=%.4f, PnL=%.4f",
                pos.side, token_id[:12], close_qty, exit_price, remaining, pnl,
            )
        return pnl

    def open_position_count(self) -> int:
        return len(self.positions)

    def total_exposure(self) -> float:
        return sum(p.notional_exposure for p in self.positions.values())

    def exposure_by_condition(self, condition_id_or_token: str) -> float:
        """Total exposure for all positions sharing the same condition_id.

        Accepts either a condition_id or token_id — looks up the condition_id
        from an existing position with that token.
        """
        # Resolve the condition_id
        cid = condition_id_or_token
        for pos in self.positions.values():
            if pos.token_id == condition_id_or_token:
                cid = pos.condition_id
                break

        return sum(
            p.notional_exposure
            for p in self.positions.values()
            if p.condition_id == cid
        )

    def exposure_by_category(self, category: str) -> float:
        """Total exposure for all positions in the same category.

        This catches cross-event correlation — e.g. multiple political markets
        are in the same category even if their condition_ids differ.
        Returns 0.0 if category is empty (no correlation tracking possible).
        """
        if not category:
            return 0.0
        return sum(
            p.notional_exposure
            for p in self.positions.values()
            if p.category == category
        )

    def position_count_by_category(self, category: str) -> int:
        """Number of open positions in the same category."""
        if not category:
            return 0
        return sum(1 for p in self.positions.values() if p.category == category)

    def total_unrealised_pnl(self, price_fn) -> float:
        """Calculate total unrealised P&L using a callable that returns the current price for a token_id."""
        total = 0.0
        for pos in self.positions.values():
            current = price_fn(pos.token_id)
            if current is not None:
                total += pos.unrealised_pnl(current)
        return total

    def reconstruct_from_trades(self, trades: list[dict]) -> None:
        """Rebuild open positions and realised PnL from trade history.

        Expects trades ordered by timestamp ascending. Each BUY opens or adds
        to a position; each SELL closes or reduces it.  This allows the bot
        to survive restarts without losing portfolio state.
        """
        self.positions.clear()
        self.realised_pnl = 0.0

        for t in trades:
            token_id = t["token_id"]
            side = t["side"]
            size = float(t["size"])
            price = float(t["price"])
            if size <= 0:
                continue

            if side == "BUY":
                # Use the merging-aware open_position so repeated BUYs on the
                # same token produce a weighted-average entry instead of
                # overwriting the prior fill.
                self.open_position(
                    Position(
                        token_id=token_id,
                        condition_id=t["condition_id"],
                        side="BUY",
                        size=size,
                        entry_price=price,
                        strategy=t.get("strategy", ""),
                        order_id=t.get("order_id", ""),
                        entry_timestamp=t.get("timestamp", ""),
                        category=t.get("category", ""),
                    )
                )
            elif side == "SELL":
                # Delegating to open_position handles all cases symmetrically:
                # same-side merge (SELL+SELL), partial/full close against a
                # BUY, and flip when the SELL exceeds the long.
                self.open_position(
                    Position(
                        token_id=token_id,
                        condition_id=t["condition_id"],
                        side="SELL",
                        size=size,
                        entry_price=price,
                        strategy=t.get("strategy", ""),
                        order_id=t.get("order_id", ""),
                        entry_timestamp=t.get("timestamp", ""),
                        category=t.get("category", ""),
                    )
                )

        logger.info(
            "Portfolio reconstructed: %d open positions, realised_pnl=%.4f",
            len(self.positions), self.realised_pnl,
        )

    def record_fee(self, fee_usd: float) -> None:
        """Add to the cumulative fees-paid counter.

        A no-op when ``fee_usd`` is zero or negative — matches the
        default-off fee model where :func:`src.analysis.fees.compute_fee_usd`
        always returns 0.0.
        """
        if fee_usd and fee_usd > 0:
            self.fees_paid += fee_usd

    def summary(self, price_fn=None) -> dict:
        unrealised = self.total_unrealised_pnl(price_fn) if price_fn else 0.0
        gross_net = self.realised_pnl + unrealised
        return {
            "open_positions": self.open_position_count(),
            "total_exposure": self.total_exposure(),
            "realised_pnl": self.realised_pnl,
            "unrealised_pnl": unrealised,
            # "net_pnl" historically meant gross (realised + unrealised).
            # We preserve that meaning for backwards compat with the
            # dashboard and tick_stats schema.  When fees are enabled,
            # callers can compute a fee-adjusted net via
            # ``summary()["net_pnl"] - summary()["fees_paid"]``.
            "net_pnl": gross_net,
            "fees_paid": self.fees_paid,
            "net_pnl_after_fees": gross_net - self.fees_paid,
        }
