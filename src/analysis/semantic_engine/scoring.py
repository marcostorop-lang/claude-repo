"""Composite signal scorer.

Turns a :class:`SyntheticPrice` + observed book microstructure into a single
``score`` in [0, 1] and a ``net_edge`` figure after costs.  Two independent
gates are exposed so the caller can tune them: ``score >= min_signal_score``
controls *quality*, ``net_edge >= min_net_edge`` controls *profitability*.

We deliberately keep the scoring additive and inspectable — every term is
logged to ``features`` so post-hoc you can see which component vetoed or
drove a signal.  No magic weights that you'd need a tuning study to
reverse-engineer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.analysis.semantic_engine.types import SyntheticPrice


# --------------------------------------------------------------------------
# Scoring configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoringConfig:
    """Tunable knobs for :func:`score_mispricing`."""

    # Minimum divergence (in probability units) before we bother scoring.
    # Anything below this is noise relative to tick size.
    min_abs_divergence: float = 0.02
    # Spread cap — beyond this the executable price is too uncertain.
    max_spread: float = 0.05
    # Liquidity penalty kicks in below this USD figure.
    min_liquidity: float = 500.0
    # Fee schedule — mirrored from Config so the scorer stays standalone.
    taker_fee_bps: float = 0.0
    maker_fee_bps: float = 0.0
    # Additional safety margin (bps of notional) — operators can set this
    # to account for unmodelled effects (latency, queue priority, etc.).
    safety_margin_bps: float = 50.0
    # Minimum liquidity required before the score can exceed 0.5 at all —
    # enforces that "high quality" is incompatible with a thin book.
    hard_min_liquidity: float = 100.0


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoreOutcome:
    """Return type of :func:`score_mispricing`."""

    side: str            # "BUY" | "SELL" | "NONE"
    gross_edge: float    # signed synthetic − executable (positive = in our favour)
    net_edge: float      # gross minus all costs (same sign convention)
    score: float         # [0, 1]
    components: dict     # every term that went into the score, for forensics


def score_mispricing(
    synthetic: SyntheticPrice,
    best_bid: float,
    best_ask: float,
    spread: float,
    liquidity: float,
    cfg: ScoringConfig = ScoringConfig(),
    *,
    prefer_maker: bool = False,
) -> ScoreOutcome:
    """Return a :class:`ScoreOutcome` for a candidate mispricing.

    ``prefer_maker`` selects the maker fee schedule and uses the *passive*
    side of the book (best_bid for BUY, best_ask for SELL) as the reference
    execution price — a hint, not an execution decision.  The trading
    strategy is free to override.
    """
    # --- Direction + executable price -------------------------------------
    if best_ask <= 0 or best_bid <= 0 or best_ask <= best_bid:
        return _empty_outcome("book_invalid", synthetic)

    fair = float(synthetic.point)

    if fair > best_ask:
        side = "BUY"
        exec_price = best_bid if prefer_maker else best_ask
        gross_edge = fair - exec_price
    elif fair < best_bid:
        side = "SELL"
        exec_price = best_ask if prefer_maker else best_bid
        gross_edge = exec_price - fair
    else:
        return _empty_outcome("within_book", synthetic)

    abs_div = abs(fair - (best_ask if side == "BUY" else best_bid))
    if abs_div < cfg.min_abs_divergence:
        return _empty_outcome("below_min_divergence", synthetic, gross_edge=gross_edge, side=side)

    # --- Costs ------------------------------------------------------------
    fee_bps = cfg.maker_fee_bps if prefer_maker else cfg.taker_fee_bps
    # Convert bps of notional-per-share.  Since notional = size * exec_price
    # and we're working in per-share edge, fees in per-share edge =
    # (bps/10000) * exec_price.
    fee_cost = (fee_bps / 10000.0) * exec_price
    safety_cost = (cfg.safety_margin_bps / 10000.0) * exec_price
    # Spread already costs us half-spread in expectation; scorers should
    # treat the executable price as the honest cost — no double counting
    # across executable-price + half-spread.  We still add a *microstructure
    # cost* tied to raw spread width because a wide book implies more
    # adverse selection even after we cross.
    spread_cost = 0.25 * max(0.0, float(spread))

    total_cost = fee_cost + safety_cost + spread_cost
    net_edge = gross_edge - total_cost

    # --- Components & score ----------------------------------------------
    magnitude_term = _saturate(gross_edge / max(0.05, cfg.min_abs_divergence * 2))
    confidence_term = float(max(0.0, min(1.0, synthetic.confidence)))
    contributors_term = min(1.0, math.log1p(max(0, synthetic.n_contributors)) / math.log1p(5))

    spread_penalty = 1.0 - _saturate(float(spread) / max(cfg.max_spread, 1e-9))
    liq = float(max(0.0, liquidity))
    liquidity_term = _saturate(math.log1p(liq) / math.log1p(cfg.min_liquidity))

    # Equal-ish weighting but magnitude & confidence dominate.  Weights
    # chosen so a synthetic with mid confidence and marginal edge can't
    # clear the quality gate — operator must see alignment on multiple axes.
    score = (
        0.30 * magnitude_term
        + 0.30 * confidence_term
        + 0.15 * contributors_term
        + 0.15 * spread_penalty
        + 0.10 * liquidity_term
    )

    # Hard floors: if the book is too wide or the venue is too thin we
    # cannot produce a high-quality signal regardless of synthetic confidence.
    if spread > cfg.max_spread:
        score = min(score, 0.40)
    if liq < cfg.hard_min_liquidity:
        score = min(score, 0.40)
    if synthetic.is_range_only:
        # Range-only syntheticas should never clear a high-quality gate.
        score = min(score, 0.50)

    score = round(max(0.0, min(1.0, score)), 6)

    return ScoreOutcome(
        side=side,
        gross_edge=round(gross_edge, 6),
        net_edge=round(net_edge, 6),
        score=score,
        components={
            "magnitude": round(magnitude_term, 4),
            "confidence": round(confidence_term, 4),
            "contributors": round(contributors_term, 4),
            "spread_penalty": round(spread_penalty, 4),
            "liquidity": round(liquidity_term, 4),
            "fee_cost": round(fee_cost, 6),
            "safety_cost": round(safety_cost, 6),
            "spread_cost": round(spread_cost, 6),
            "total_cost": round(total_cost, 6),
            "exec_price": round(exec_price, 6),
            "fair": round(fair, 6),
            "prefer_maker": prefer_maker,
            "is_range_only": synthetic.is_range_only,
        },
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _saturate(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _empty_outcome(
    reason: str,
    synthetic: SyntheticPrice,
    *,
    gross_edge: float = 0.0,
    side: str = "NONE",
) -> ScoreOutcome:
    return ScoreOutcome(
        side=side,
        gross_edge=round(gross_edge, 6),
        net_edge=0.0,
        score=0.0,
        components={"veto": reason, "fair": round(synthetic.point, 6)},
    )
