"""Tests for the adverse-selection filter."""

from __future__ import annotations

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.adverse_selection import AdverseSelectionFilter
from src.risk.manager import RiskManager
from src.strategy.base import Action, Signal


def _book(bid: float, ask: float):
    return lambda tok: {"bid_depth_usd": bid, "ask_depth_usd": ask}


# ---------------------------------------------------------------------------
# Filter logic in isolation
# ---------------------------------------------------------------------------


class TestFilterLogic:
    def test_balanced_book_passes(self):
        f = AdverseSelectionFilter(
            book_provider=_book(1000, 1000),
            ratio_threshold=3.0, absolute_floor_usd=500,
        )
        v = f.check("tok1", "BUY")
        assert not v.blocked
        assert v.ratio == pytest.approx(1.0)

    def test_wall_of_asks_thin_bid_blocks_buy(self):
        f = AdverseSelectionFilter(
            book_provider=_book(100, 1000),
            ratio_threshold=3.0, absolute_floor_usd=500,
        )
        v = f.check("tok1", "BUY")
        assert v.blocked
        assert v.ratio == pytest.approx(10.0)
        assert "Adverse selection" in v.reason

    def test_thin_both_sides_passes(self):
        # Ratio is huge but the absolute wall is below the floor →
        # not adverse selection in any meaningful sense.
        f = AdverseSelectionFilter(
            book_provider=_book(10, 100),
            ratio_threshold=3.0, absolute_floor_usd=500,
        )
        v = f.check("tok1", "BUY")
        assert not v.blocked
        assert v.ratio == pytest.approx(10.0)

    def test_sell_never_blocked(self):
        f = AdverseSelectionFilter(
            book_provider=_book(100, 5000),  # would block a BUY
            ratio_threshold=3.0, absolute_floor_usd=500,
        )
        v = f.check("tok1", "SELL")
        assert not v.blocked

    def test_no_book_provider_passes(self):
        f = AdverseSelectionFilter(
            book_provider=None,
            ratio_threshold=3.0, absolute_floor_usd=500,
        )
        v = f.check("tok1", "BUY")
        assert not v.blocked

    def test_provider_exception_fails_safe(self):
        def explode(tok: str):
            raise RuntimeError("kaboom")
        f = AdverseSelectionFilter(
            book_provider=explode,
            ratio_threshold=3.0, absolute_floor_usd=500,
        )
        # Fail-safe = pass.  We never *block* a trade because the
        # gate itself is broken; that would be a worse failure mode.
        v = f.check("tok1", "BUY")
        assert not v.blocked


# ---------------------------------------------------------------------------
# Integration with RiskManager
# ---------------------------------------------------------------------------


class TestRiskIntegration:
    def test_buy_blocked_when_filter_trips(self, monkeypatch):
        # Disable other gates that would block the BUY first.
        monkeypatch.setenv("REQUIRE_KNOWN_SPREAD_FOR_BUY", "false")
        cfg = Config()
        rm = RiskManager(cfg, PortfolioTracker())
        rm.adverse_selection_filter = AdverseSelectionFilter(
            book_provider=_book(100, 5000),
            ratio_threshold=3.0, absolute_floor_usd=500,
        )
        v = rm.check("tok1", Signal(Action.BUY, 0.8), 10, 0.5, spread=0.02)
        assert not v.allowed
        assert "Adverse selection" in v.reason

    def test_sell_allowed_when_filter_would_block(self, monkeypatch):
        monkeypatch.setenv("REQUIRE_KNOWN_SPREAD_FOR_BUY", "false")
        cfg = Config()
        rm = RiskManager(cfg, PortfolioTracker())
        rm.adverse_selection_filter = AdverseSelectionFilter(
            book_provider=_book(100, 5000),
            ratio_threshold=3.0, absolute_floor_usd=500,
        )
        # Open a position so the SELL can potentially close it.
        from src.portfolio.tracker import Position
        rm.portfolio.open_position(Position(
            token_id="tok1", condition_id="c", side="BUY",
            size=10, entry_price=0.5, strategy="x", order_id="o",
        ))
        v = rm.check("tok1", Signal(Action.SELL, 0.8), 10, 0.5, spread=0.02)
        # Adverse-selection filter never gates SELLs; the SELL may
        # still trip *other* gates but not this one.
        assert "Adverse selection" not in (v.reason or "")

    def test_no_filter_attached_is_no_op(self, monkeypatch):
        monkeypatch.setenv("REQUIRE_KNOWN_SPREAD_FOR_BUY", "false")
        cfg = Config()
        rm = RiskManager(cfg, PortfolioTracker())
        # ``adverse_selection_filter`` left as None.
        v = rm.check("tok1", Signal(Action.BUY, 0.8), 10, 0.5, spread=0.02)
        # No mention of the filter in the reason regardless of outcome.
        assert "Adverse selection" not in (v.reason or "")
