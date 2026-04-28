"""
Order-Flow Imbalance (OFI) strategy.

Microstructure intuition
------------------------
In an order-driven market, the *visible depth* on the bid and ask
sides of the book carries information.  A book that is consistently
heavier on the bid (more dollars willing to buy at or near the
midpoint than to sell) tends to drift up; the opposite drifts down.
This is a well-documented HFT signal — Cont, Kukanov, Stoikov
(2014) "The Price Impact of Order Book Events" — but it does not
appear in any vanilla "Claude bot" because:

* It needs the order book, not just the midpoint.
* It needs *sustained* imbalance over multiple snapshots, not a
  single point-in-time reading (which is noise).
* It needs a per-token rolling window of recent imbalance values.

This strategy supplies all three.  It plugs into the existing
``BaseStrategy`` contract and consumes a book via a callable
injected at construction:

    OrderFlowImbalanceStrategy(cfg, book_provider=client.get_top_of_book_with_depth)

When the book provider is missing or returns ``None``, the strategy
returns ``HOLD`` rather than guessing.

Signal logic
------------
Define **imbalance** at tick *t* on token *T* as:

    imbalance_t = (bid_depth_usd - ask_depth_usd)
                  / max(bid_depth_usd + ask_depth_usd, eps)

Bounded in ``[-1, +1]``.  Positive = more bid pressure → expect
upward drift → BUY.  Negative = more ask pressure → SELL.

A signal fires only when:
  * The rolling-window mean of imbalance over the last
    ``OFI_WINDOW`` ticks exceeds ``OFI_THRESHOLD`` in magnitude.
  * The most recent ``OFI_WINDOW`` readings agree on sign.
  * Total visible depth on each side ≥ ``OFI_MIN_DEPTH_USD`` so we
    never act on whisper-thin books.

Confidence equals the absolute mean imbalance, capped at 1.0.
``net_edge`` features (gross_edge, half_spread, fee_pct, net_edge)
are recorded so the calibration pipeline can correlate OFI
signals with realised PnL.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Sequence

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.base import Action, BaseStrategy, Signal, compute_net_edge


# Type alias for clarity in the docstring above.
BookProvider = Callable[[str], dict | None]


@dataclass
class _OFIWindow:
    """Rolling window of recent imbalance readings for one token."""

    history: deque
    last_seen_ts: float = 0.0


class OrderFlowImbalanceStrategy(BaseStrategy):
    """Microstructure strategy: signals on sustained book imbalance.

    Attributes
    ----------
    book_provider :
        ``Callable[[token_id: str], dict | None]`` that returns at
        least ``{"bid_depth_usd": float, "ask_depth_usd": float}``
        (other keys ignored).  ``None`` returns are tolerated and
        produce HOLD.
    """

    name = "order_flow_imbalance"

    def __init__(
        self,
        cfg: Config,
        *,
        book_provider: BookProvider | None = None,
    ) -> None:
        self.cfg = cfg
        self.book_provider = book_provider
        self.window = max(2, int(getattr(cfg, "ofi_window", 5)))
        self.threshold = float(getattr(cfg, "ofi_threshold", 0.30))
        self.min_depth_usd = float(getattr(cfg, "ofi_min_depth_usd", 200.0))
        self._windows: dict[str, _OFIWindow] = {}

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------

    def _record(self, token_id: str, imbalance: float) -> _OFIWindow:
        w = self._windows.get(token_id)
        if w is None:
            w = _OFIWindow(history=deque(maxlen=self.window))
            self._windows[token_id] = w
        w.history.append(imbalance)
        return w

    @staticmethod
    def _imbalance(bid_usd: float, ask_usd: float) -> float:
        total = bid_usd + ask_usd
        if total <= 0:
            return 0.0
        return (bid_usd - ask_usd) / total

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        price_history: Sequence[float],
    ) -> Signal:
        if snapshot.price is None:
            return Signal(Action.HOLD, 0.0, "No price.")
        if self.book_provider is None:
            return Signal(Action.HOLD, 0.0, "No book provider attached.")

        try:
            book = self.book_provider(snapshot.token_id)
        except Exception:
            return Signal(Action.HOLD, 0.0, "Book provider raised.")

        if not isinstance(book, dict):
            return Signal(Action.HOLD, 0.0, "No book.")

        bid_usd = float(book.get("bid_depth_usd", 0.0) or 0.0)
        ask_usd = float(book.get("ask_depth_usd", 0.0) or 0.0)

        if bid_usd < self.min_depth_usd or ask_usd < self.min_depth_usd:
            # Too thin to trust the imbalance signal — return HOLD
            # *without* recording into the window so the next tick on
            # a fatter book starts cleanly.
            return Signal(
                Action.HOLD, 0.0,
                f"Book too thin (bid={bid_usd:.0f}, ask={ask_usd:.0f}; "
                f"floor={self.min_depth_usd:.0f}).",
                features={"bid_depth_usd": bid_usd, "ask_depth_usd": ask_usd},
            )

        imb = self._imbalance(bid_usd, ask_usd)
        w = self._record(snapshot.token_id, imb)

        if len(w.history) < self.window:
            return Signal(
                Action.HOLD, 0.0,
                f"Warming up ({len(w.history)}/{self.window}).",
                features={"imbalance": imb},
            )

        # Sustained imbalance: rolling mean magnitude clears the
        # threshold *and* every reading in the window agrees on sign
        # (positive or negative).  Two same-sign requirements catch
        # whip-saws that would alternate +/- around zero.
        mean_imb = sum(w.history) / len(w.history)
        signs = {1 if x > 0 else (-1 if x < 0 else 0) for x in w.history}
        all_same_sign = signs == {1} or signs == {-1}
        net = compute_net_edge(
            gross_edge=abs(mean_imb),
            spread=snapshot.spread or 0.0,
            taker_fee_bps=getattr(self.cfg, "taker_fee_bps", 0.0),
        )
        features = {
            "imbalance": imb,
            "mean_imbalance": mean_imb,
            "window": self.window,
            "threshold": self.threshold,
            "bid_depth_usd": bid_usd,
            "ask_depth_usd": ask_usd,
            "all_same_sign": all_same_sign,
            **net,
        }

        if abs(mean_imb) < self.threshold or not all_same_sign:
            return Signal(
                Action.HOLD, 0.0,
                f"OFI {mean_imb:+.3f} below threshold {self.threshold:.3f} "
                f"or sign mixed.",
                features=features,
            )

        # Optional: gate on net edge (mean imbalance after costs).
        gate_on = bool(getattr(self.cfg, "strategy_net_edge_gate_enabled", False))
        min_net = float(getattr(self.cfg, "strategy_min_net_edge", 0.0))
        if gate_on and net["net_edge"] < min_net:
            return Signal(
                Action.HOLD, 0.0,
                f"OFI net edge {net['net_edge']:+.4f} below min {min_net:.4f}.",
                features=features,
            )

        confidence = min(abs(mean_imb), 1.0)
        if mean_imb > 0:
            return Signal(
                Action.BUY, confidence,
                f"OFI BUY: mean imbalance {mean_imb:+.3f} over {self.window} ticks.",
                features=features,
            )
        return Signal(
            Action.SELL, confidence,
            f"OFI SELL: mean imbalance {mean_imb:+.3f} over {self.window} ticks.",
            features=features,
        )
