"""Tests for order book depth analysis."""

import pytest

from src.analysis.book_depth import BookLevel, BookAnalysis, analyze_book


def _bids(*levels):
    return [BookLevel(p, s) for p, s in levels]


def _asks(*levels):
    return [BookLevel(p, s) for p, s in levels]


class TestBookAnalysis:
    def test_empty_book_returns_defaults(self):
        result = analyze_book([], [])
        assert result.best_bid == 0.0
        assert result.best_ask == 0.0

    def test_basic_spread_calculation(self):
        bids = _bids((0.49, 100), (0.48, 200))
        asks = _asks((0.51, 100), (0.52, 200))
        result = analyze_book(bids, asks)
        assert result.best_bid == pytest.approx(0.49)
        assert result.best_ask == pytest.approx(0.51)
        assert result.spread == pytest.approx(0.02)
        assert result.midpoint == pytest.approx(0.50)

    def test_imbalance_heavy_bids(self):
        # Much more depth on bid side → positive imbalance (buy pressure)
        bids = _bids((0.49, 1000), (0.48, 500))
        asks = _asks((0.51, 100))
        result = analyze_book(bids, asks)
        assert result.imbalance_5pct > 0

    def test_imbalance_heavy_asks(self):
        # Much more depth on ask side → negative imbalance (sell pressure)
        bids = _bids((0.49, 100))
        asks = _asks((0.51, 1000), (0.52, 500))
        result = analyze_book(bids, asks)
        assert result.imbalance_5pct < 0

    def test_vwap_buy_single_level(self):
        # $50 buy into 200 shares at 0.50 → fills entirely at 0.50
        bids = _bids((0.49, 200))
        asks = _asks((0.50, 200))  # 200 * 0.50 = $100 available
        result = analyze_book(bids, asks, fill_size_usd=50.0)
        assert result.vwap_buy == pytest.approx(0.50)
        assert result.slippage_buy_pct == pytest.approx(0.0)
        assert result.can_fill_at_top is True

    def test_vwap_buy_walks_multiple_levels(self):
        # $50 buy, but only $10 at best ask → walks to next level
        bids = _bids((0.49, 100))
        asks = _asks((0.50, 20), (0.55, 200))  # 20*0.50=$10 at first, rest at 0.55
        result = analyze_book(bids, asks, fill_size_usd=50.0)
        assert result.vwap_buy > 0.50  # had to pay more
        assert result.slippage_buy_pct > 0  # positive slippage
        assert result.can_fill_at_top is False

    def test_depth_at_thresholds(self):
        # 0.496 is ~0.8% from mid=0.50, clearly within 1%
        bids = _bids((0.496, 100), (0.48, 200))
        asks = _asks((0.504, 100), (0.52, 200))
        result = analyze_book(bids, asks)
        assert result.bid_depth_1pct > 0
        assert result.bid_depth_5pct >= result.bid_depth_1pct

    def test_unsorted_input_handled(self):
        # Bids in wrong order — should still work
        bids = _bids((0.48, 200), (0.49, 100))
        asks = _asks((0.52, 200), (0.51, 100))
        result = analyze_book(bids, asks)
        assert result.best_bid == 0.49
        assert result.best_ask == 0.51

    def test_raw_dict_populated(self):
        bids = _bids((0.49, 100))
        asks = _asks((0.51, 100))
        result = analyze_book(bids, asks)
        assert "spread" in result.raw
        assert "imbalance_1pct" in result.raw
        assert "can_fill_at_top" in result.raw
