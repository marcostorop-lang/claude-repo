"""
Brier-weighted ensemble strategy.

Why an ensemble
---------------
Each strategy in this repo captures a different edge: SimpleMomentum
chases trends, MeanReversion fades them, EdgeBased trusts a calibrated
fundamentals score, OrderFlowImbalance reads microstructure, and
PairsCointegration exploits Polymarket's leg-sum structure.  None of
them is right all the time; pretending one of them *is* and running
it solo is exactly the mistake that punishes most retail bots.

The ensemble runs every member on the same snapshot, then **votes
weighted by the Brier-calibrated track record** of each.  A vote is
the member's signed confidence; the final action is the sign of the
weighted sum, with HOLD when the magnitudes cancel.

Why Brier weighting matters here
--------------------------------
A simple equal-weight vote treats a strategy that has been right 70%
of the time at conf=0.6 the same as one that has been right 25% of
the time at conf=0.6.  The Brier-calibrated weight is *exactly* the
multiplier that corrects for this — calibrated members vote louder
than poorly-calibrated ones, automatically.  Cold-start members vote
at weight 1.0 so a brand-new strategy is heard while it accumulates
a track record.

Composition rules
-----------------
* Members are resolved by name from the existing strategy registry
  in ``src/strategy``.  No member sees the others' state — votes
  combine downstream.
* Members that return HOLD are skipped (zero weight); the ensemble
  also returns HOLD when no member voted with non-zero magnitude.
* The combined confidence is the *normalised* magnitude of the
  weighted sum, capped at 1.0, so the existing risk-manager sizing
  is sane regardless of how many members voted.
"""

from __future__ import annotations

import logging
from typing import Sequence

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.base import Action, BaseStrategy, Signal

logger = logging.getLogger(__name__)


# Member names supported out of the box.  Map to the same identifiers
# the rest of the bot uses in ``STRATEGY=...`` so configuration is
# uniform.  Membership is parsed from ``ENSEMBLE_MEMBERS`` (CSV).
def _build_member(name: str, cfg: Config) -> BaseStrategy | None:
    """Build a single member by name.  Returns None on unknown names."""
    name = (name or "").strip().lower()
    if not name:
        return None
    if name == "simple_momentum":
        from src.strategy.simple_momentum import SimpleMomentum
        return SimpleMomentum(cfg)
    if name == "mean_reversion":
        from src.strategy.mean_reversion import MeanReversion
        return MeanReversion(cfg)
    if name == "edge_based":
        from src.strategy.edge_based import EdgeBasedStrategy
        return EdgeBasedStrategy(cfg)
    if name == "order_flow_imbalance":
        from src.strategy.order_flow_imbalance import OrderFlowImbalanceStrategy
        return OrderFlowImbalanceStrategy(cfg)
    if name == "pairs_cointegration":
        from src.strategy.pairs_cointegration import PairsCointegrationStrategy
        return PairsCointegrationStrategy(cfg)
    if name == "semantic_mispricing":
        from src.strategy.semantic_mispricing import SemanticMispricingStrategy
        return SemanticMispricingStrategy(cfg)
    return None


class BrierEnsembleStrategy(BaseStrategy):
    """Ensemble member voter weighted by per-member Brier calibration.

    The Brier calibrator is plug-in: ``self.calibrator`` defaults to
    ``None``, which collapses the ensemble to plain equal-weight
    voting.  ``run_loop`` attaches the same ``BrierCalibrator``
    instance the risk manager uses, so members are weighted by their
    *real* historical track record without duplicate state.
    """

    name = "ensemble"

    def __init__(
        self,
        cfg: Config,
        members: list[BaseStrategy] | None = None,
    ) -> None:
        self.cfg = cfg
        if members is not None:
            self.members = list(members)
        else:
            names_raw = (getattr(cfg, "ensemble_members", "") or "").strip()
            names = [n.strip() for n in names_raw.split(",") if n.strip()]
            self.members = [m for m in (_build_member(n, cfg) for n in names) if m is not None]
        self.calibrator = None
        self.min_total_weight = float(getattr(cfg, "ensemble_min_total_weight", 0.5))
        self.min_net_score = float(getattr(cfg, "ensemble_min_net_score", 0.0))

    # ------------------------------------------------------------------
    # Vote aggregation
    # ------------------------------------------------------------------

    def _weight_for(self, member: BaseStrategy, category: str) -> float:
        if self.calibrator is None:
            return 1.0
        try:
            return float(self.calibrator.calibration_multiplier(member.name, category))
        except Exception:
            logger.debug("Brier weight lookup failed for %s", member.name, exc_info=True)
            return 1.0

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        price_history: Sequence[float],
    ) -> Signal:
        if not self.members:
            return Signal(Action.HOLD, 0.0, "No ensemble members configured.")

        category = getattr(snapshot, "category", "") or ""
        weighted_sum = 0.0
        total_abs_weight = 0.0
        votes: list[dict] = []

        for member in self.members:
            try:
                sub = member.evaluate(snapshot, price_history)
            except Exception:
                logger.debug("Member %s raised", member.name, exc_info=True)
                continue
            if sub.action == Action.HOLD:
                votes.append({
                    "member": member.name, "action": "HOLD",
                    "confidence": 0.0, "weight": 0.0,
                })
                continue
            sign = +1.0 if sub.action == Action.BUY else -1.0
            weight = self._weight_for(member, category)
            if weight <= 0:
                continue
            contribution = sign * sub.confidence * weight
            weighted_sum += contribution
            total_abs_weight += weight
            votes.append({
                "member": member.name,
                "action": sub.action.value,
                "confidence": round(sub.confidence, 4),
                "weight": round(weight, 4),
                "contribution": round(contribution, 4),
            })

        features = {
            "votes": votes,
            "weighted_sum": round(weighted_sum, 4),
            "total_abs_weight": round(total_abs_weight, 4),
            "n_members": len(self.members),
            "n_voted": sum(1 for v in votes if v["action"] != "HOLD"),
        }

        if total_abs_weight < self.min_total_weight:
            return Signal(
                Action.HOLD, 0.0,
                f"Ensemble weight {total_abs_weight:.2f} below floor "
                f"{self.min_total_weight:.2f}.",
                features=features,
            )

        # Net score in [-1, +1].
        net_score = weighted_sum / total_abs_weight
        if abs(net_score) < self.min_net_score:
            return Signal(
                Action.HOLD, 0.0,
                f"Ensemble net score {net_score:+.3f} inside ±{self.min_net_score:.3f} band.",
                features=features,
            )

        confidence = min(abs(net_score), 1.0)
        if net_score > 0:
            return Signal(
                Action.BUY, confidence,
                f"Ensemble BUY: net={net_score:+.3f} from {features['n_voted']} voters.",
                features=features,
            )
        return Signal(
            Action.SELL, confidence,
            f"Ensemble SELL: net={net_score:+.3f} from {features['n_voted']} voters.",
            features=features,
        )
