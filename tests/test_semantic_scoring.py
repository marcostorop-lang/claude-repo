"""Unit tests for the semantic mispricing scorer."""

from __future__ import annotations

import pytest

from src.analysis.semantic_engine.scoring import (
    ScoringConfig,
    score_mispricing,
)
from src.analysis.semantic_engine.types import SyntheticPrice


def _synth(point=0.70, confidence=0.85, n=3, method="structural_complement",
           lower=None, upper=None, is_range_only=False):
    return SyntheticPrice(
        point=point,
        lower=lower if lower is not None else max(0.0, point - 0.01),
        upper=upper if upper is not None else min(1.0, point + 0.01),
        confidence=confidence,
        method=method,
        contributors=tuple(f"t{i}" for i in range(n)),
        n_contributors=n,
        is_range_only=is_range_only,
    )


class TestDirectionDetection:
    def test_buy_when_fair_above_ask(self):
        synth = _synth(point=0.70)
        out = score_mispricing(synth, best_bid=0.59, best_ask=0.61,
                               spread=0.02, liquidity=5000)
        assert out.side == "BUY"
        assert out.gross_edge > 0

    def test_sell_when_fair_below_bid(self):
        synth = _synth(point=0.30)
        out = score_mispricing(synth, best_bid=0.39, best_ask=0.41,
                               spread=0.02, liquidity=5000)
        assert out.side == "SELL"
        assert out.gross_edge > 0

    def test_none_when_fair_within_book(self):
        synth = _synth(point=0.60)
        out = score_mispricing(synth, best_bid=0.58, best_ask=0.62,
                               spread=0.04, liquidity=5000)
        assert out.side == "NONE"


class TestCosts:
    def test_fees_subtracted_from_net(self):
        synth = _synth(point=0.70, confidence=0.85)
        cfg = ScoringConfig(taker_fee_bps=100, safety_margin_bps=0)  # 1% fee
        out = score_mispricing(synth, best_bid=0.59, best_ask=0.61,
                               spread=0.02, liquidity=5000, cfg=cfg)
        assert out.net_edge < out.gross_edge

    def test_safety_margin_subtracted(self):
        synth = _synth(point=0.70)
        cfg = ScoringConfig(taker_fee_bps=0, safety_margin_bps=100)
        out = score_mispricing(synth, best_bid=0.59, best_ask=0.61,
                               spread=0.02, liquidity=5000, cfg=cfg)
        assert out.net_edge < out.gross_edge

    def test_spread_cost_applied(self):
        """Wider spreads cost more."""
        synth = _synth(point=0.70)
        cfg = ScoringConfig(taker_fee_bps=0, safety_margin_bps=0)
        tight = score_mispricing(synth, best_bid=0.60, best_ask=0.62,
                                 spread=0.02, liquidity=5000, cfg=cfg)
        wide = score_mispricing(synth, best_bid=0.55, best_ask=0.65,
                                spread=0.10, liquidity=5000, cfg=cfg)
        assert wide.net_edge < tight.net_edge


class TestScoreFloors:
    def test_range_only_capped(self):
        synth = _synth(point=0.70, confidence=0.95, is_range_only=True,
                       lower=0.40, upper=0.80)
        out = score_mispricing(synth, best_bid=0.59, best_ask=0.61,
                               spread=0.02, liquidity=10000)
        assert out.score <= 0.50

    def test_thin_book_capped(self):
        synth = _synth(point=0.70, confidence=0.95)
        cfg = ScoringConfig(hard_min_liquidity=1000)
        out = score_mispricing(synth, best_bid=0.59, best_ask=0.61,
                               spread=0.02, liquidity=50, cfg=cfg)
        assert out.score <= 0.40

    def test_wide_spread_capped(self):
        synth = _synth(point=0.70, confidence=0.95)
        cfg = ScoringConfig(max_spread=0.05)
        out = score_mispricing(synth, best_bid=0.50, best_ask=0.65,
                               spread=0.15, liquidity=10000, cfg=cfg)
        assert out.score <= 0.40


class TestGates:
    def test_below_min_divergence_returns_zero(self):
        synth = _synth(point=0.605)
        cfg = ScoringConfig(min_abs_divergence=0.05)
        out = score_mispricing(synth, best_bid=0.59, best_ask=0.60,
                               spread=0.01, liquidity=10000, cfg=cfg)
        assert out.side == "BUY"
        # Divergence 0.005 < 0.05 → score 0
        assert out.score == 0.0

    def test_invalid_book_rejects(self):
        synth = _synth(point=0.70)
        out = score_mispricing(synth, best_bid=0.61, best_ask=0.60,
                               spread=0.02, liquidity=10000)
        assert out.side == "NONE"
        assert out.score == 0.0


class TestMakerHint:
    def test_prefer_maker_uses_passive_side(self):
        synth = _synth(point=0.70)
        cfg = ScoringConfig(taker_fee_bps=0, maker_fee_bps=0, safety_margin_bps=0)
        out_taker = score_mispricing(synth, best_bid=0.59, best_ask=0.61,
                                     spread=0.02, liquidity=10000, cfg=cfg)
        out_maker = score_mispricing(synth, best_bid=0.59, best_ask=0.61,
                                     spread=0.02, liquidity=10000, cfg=cfg,
                                     prefer_maker=True)
        # Maker posts at best_bid for BUY → fill @ 0.59; taker @ 0.61.
        # Gross edge is bigger for maker.
        assert out_maker.gross_edge > out_taker.gross_edge


class TestForensicComponents:
    def test_components_reported(self):
        synth = _synth(point=0.70, confidence=0.85, n=3)
        out = score_mispricing(synth, best_bid=0.59, best_ask=0.61,
                               spread=0.02, liquidity=10000)
        for k in ("magnitude", "confidence", "contributors",
                  "spread_penalty", "liquidity", "total_cost",
                  "exec_price", "fair"):
            assert k in out.components
