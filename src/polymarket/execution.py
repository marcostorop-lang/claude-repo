"""
Order execution layer.

Paper mode: records orders in the local store.
Live mode:  forwards orders to the CLOB API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config import Config
from src.polymarket.client import PolymarketClient
from src.storage.sqlite_store import SQLiteStore
from src.utils.time_utils import iso_now

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    token_id: str
    condition_id: str
    side: str  # "BUY" or "SELL"
    size: float
    price: float
    strategy: str
    spread: float = 0.0
    exit_reason: str = ""
    # When True, ``price`` is already a side-of-book price (best_ask for BUY,
    # best_bid for SELL) so the paper engine should NOT add further slippage.
    is_book_price: bool = False


@dataclass
class OrderResult:
    success: bool
    order_id: str
    mode: str  # "paper" or "live"
    message: str = ""


class ExecutionEngine:
    """Routes orders to paper or live execution."""

    def __init__(self, client: PolymarketClient, cfg: Config, store: SQLiteStore) -> None:
        self.client = client
        self.cfg = cfg
        self.store = store

    def execute(self, order: OrderRequest) -> OrderResult:
        """Execute an order request."""
        if self.cfg.is_paper or not self.cfg.is_live:
            return self._paper_execute(order)
        return self._live_execute(order)

    # ------------------------------------------------------------------
    # Paper execution
    # ------------------------------------------------------------------

    def _paper_execute(self, order: OrderRequest) -> OrderResult:
        import uuid

        # Slippage simulation.
        #
        # If the caller already resolved the side-of-book price (best_ask for
        # BUY, best_bid for SELL), ``is_book_price`` is True and we use it as
        # the fill directly — adding further half-spread would double-count.
        #
        # If we only have a midpoint (book unavailable), we fall back to the
        # classical half-spread slippage approximation.
        if order.is_book_price:
            half_spread = 0.0
            fill_price = max(order.price, 0.0001)
        else:
            half_spread = order.spread / 2.0 if order.spread > 0 else 0.0
            if order.side == "BUY":
                fill_price = order.price + half_spread
            else:
                fill_price = max(order.price - half_spread, 0.0001)

        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        self.store.insert_trade(
            order_id=order_id,
            token_id=order.token_id,
            condition_id=order.condition_id,
            side=order.side,
            size=order.size,
            price=fill_price,
            strategy=order.strategy,
            mode="paper",
            timestamp=iso_now(),
            exit_reason=order.exit_reason,
            spread_at_entry=order.spread,
        )
        logger.info(
            "[PAPER] %s %.4f of %s @ %.4f (mid=%.4f, spread=%.4f, slippage=%.4f, strategy=%s%s)",
            order.side,
            order.size,
            order.token_id[:12],
            fill_price,
            order.price,
            order.spread,
            half_spread,
            order.strategy,
            f", exit_reason={order.exit_reason}" if order.exit_reason else "",
        )
        return OrderResult(
            success=True, order_id=order_id, mode="paper",
            message=f"Paper order recorded (fill={fill_price:.4f}, slippage={half_spread:.4f}).",
        )

    # ------------------------------------------------------------------
    # Live execution
    # ------------------------------------------------------------------

    def _live_execute(self, order: OrderRequest) -> OrderResult:
        """
        Send a real order via the CLOB API.

        This path is only reachable when:
        1. TRADING_MODE=live
        2. ALLOW_LIVE_TRADING=true
        3. Valid credentials are configured

        The py-clob-client expects an ``OrderArgs`` dict; refer to the SDK
        docs for the exact format.  The implementation below is a scaffold
        that should be adapted to your specific signing flow.
        """
        # Double-safety check
        if not self.cfg.is_live:
            return OrderResult(
                success=False, order_id="", mode="live",
                message="Live trading not enabled.",
            )

        try:
            from py_clob_client.order_builder.constants import BUY, SELL

            side = BUY if order.side == "BUY" else SELL

            # Build order args — see py-clob-client docs
            order_args = {
                "token_id": order.token_id,
                "price": order.price,
                "size": order.size,
                "side": side,
            }

            resp = self.client.place_order(order_args)
            if resp is None:
                return OrderResult(
                    success=False, order_id="", mode="live",
                    message="Order placement returned None.",
                )

            order_id = resp.get("orderID", resp.get("id", "unknown"))
            self.store.insert_trade(
                order_id=str(order_id),
                token_id=order.token_id,
                condition_id=order.condition_id,
                side=order.side,
                size=order.size,
                price=order.price,
                strategy=order.strategy,
                mode="live",
                timestamp=iso_now(),
            )
            logger.info("[LIVE] Order placed: %s", order_id)
            return OrderResult(success=True, order_id=str(order_id), mode="live")

        except Exception as exc:
            logger.exception("Live order failed.")
            return OrderResult(
                success=False, order_id="", mode="live",
                message=str(exc),
            )
