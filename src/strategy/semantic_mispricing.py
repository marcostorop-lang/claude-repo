"""Semantic mispricing strategy adapter.

This adapter turns :class:`SemanticMispricing` detections into
:class:`Signal` objects so the rest of the tick pipeline (risk checks,
execution, portfolio tracking) can consume them unchanged.

The strategy is *stateful across a single tick* but not across ticks:

* Before the snapshot loop, :func:`_tick` calls
  :meth:`set_semantic_context` with the detections produced by the
  observer scan.  The strategy indexes them by token_id.
* During the loop, :meth:`evaluate` looks up the target token and emits
  a BUY/SELL Signal when there's a mispricing, otherwise HOLD.

No state survives across ticks — the context is reset each time
``set_semantic_context`` is called.  This keeps the adapter compatible
with the existing ``BaseStrategy`` contract (no `prepare()` hook needed
in the base class) and confines semantic engine state to the caller.
"""

from __future__ import annotations

import logging
from typing import Sequence

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.base import Action, BaseStrategy, Signal

logger = logging.getLogger(__name__)


class SemanticMispricingStrategy(BaseStrategy):
    """Opt-in strategy backed by the semantic engine.

    Instantiated by the factory only when ``STRATEGY=semantic_mispricing``.
    The strategy is inert (always returns HOLD) until
    :meth:`set_semantic_context` is called with the current-tick detections.
    """

    name = "semantic_mispricing"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # token_id -> SemanticMispricing for the current tick.
        self._mispricings: dict[str, object] = {}

    # ------------------------------------------------------------------
    # Context injection (called from _tick once per scan)
    # ------------------------------------------------------------------

    def set_semantic_context(self, mispricings: Sequence[object]) -> None:
        """Replace the in-memory detection cache.

        Called by :func:`src.main._tick` after the observer scan runs.
        Passing an empty sequence makes the strategy inert for this tick.
        """
        self._mispricings = {m.token_id: m for m in mispricings}

    def clear_semantic_context(self) -> None:
        self._mispricings = {}

    # ------------------------------------------------------------------
    # BaseStrategy contract
    # ------------------------------------------------------------------

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        price_history: Sequence[float],  # unused — engine derives from cross-market state
    ) -> Signal:
        m = self._mispricings.get(snapshot.token_id)
        if m is None:
            return Signal(
                action=Action.HOLD,
                confidence=0.0,
                reason="no semantic mispricing detected",
                features={"semantic_engine": "no_detection"},
            )

        # Defensive: the engine already gates on net_edge + score, but the
        # Config knobs can tighten without re-scanning, so re-check here.
        if m.net_edge < self.cfg.semantic_min_net_edge:
            return Signal(
                action=Action.HOLD,
                confidence=0.0,
                reason=f"semantic net_edge {m.net_edge:.4f} below min",
                features={"semantic_engine": "net_edge_below_min", **m.features},
            )
        if m.score < self.cfg.semantic_min_signal_score:
            return Signal(
                action=Action.HOLD,
                confidence=0.0,
                reason=f"semantic score {m.score:.3f} below min",
                features={"semantic_engine": "score_below_min", **m.features},
            )

        if m.side == "BUY":
            action = Action.BUY
        elif m.side == "SELL":
            action = Action.SELL
        else:
            return Signal(
                action=Action.HOLD,
                confidence=0.0,
                reason="semantic engine: side=NONE",
                features={"semantic_engine": "side_none", **m.features},
            )

        # Confidence for the rest of the pipeline = engine score.  The
        # features bag includes ``edge`` so the risk manager's
        # min_edge_for_trade gate and Kelly sizing still work.
        features = dict(m.features)
        features["edge"] = float(m.gross_edge) if action == Action.BUY else -float(m.gross_edge)
        features["semantic_engine"] = "signal"
        features["semantic_side"] = m.side
        features["semantic_score"] = m.score
        features["semantic_net_edge"] = m.net_edge

        return Signal(
            action=action,
            confidence=float(m.score),
            reason=(
                f"semantic_{m.synthetic.method}: fair={m.synthetic.point:.4f} "
                f"vs exec={(m.best_ask if action == Action.BUY else m.best_bid):.4f} "
                f"(edge={m.net_edge:+.4f}, score={m.score:.2f}, "
                f"n_contrib={m.synthetic.n_contributors})"
            ),
            features=features,
        )
