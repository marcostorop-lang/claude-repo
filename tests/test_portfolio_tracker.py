"""Tests for src.portfolio.tracker — including reconstruction from trades."""

import pytest

from src.portfolio.tracker import PortfolioTracker, Position


class TestPortfolioTracker:
    def test_open_and_close_position(self):
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "test", "o1"))
        assert pt.open_position_count() == 1
        pnl = pt.close_position("t1", 0.55)
        assert pnl == pytest.approx(0.5)  # (0.55 - 0.50) * 10
        assert pt.open_position_count() == 0
        assert pt.realised_pnl == pytest.approx(0.5)

    def test_close_nonexistent_position(self):
        pt = PortfolioTracker()
        pnl = pt.close_position("nonexistent", 0.50)
        assert pnl == 0.0

    def test_total_exposure(self):
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "test", "o1"))
        pt.open_position(Position("t2", "c2", "BUY", 20, 0.60, "test", "o2"))
        assert pt.total_exposure() == pytest.approx(10 * 0.50 + 20 * 0.60)

    def test_unrealised_pnl_buy(self):
        pos = Position("t1", "c1", "BUY", 10, 0.50, "test", "o1")
        assert pos.unrealised_pnl(0.55) == pytest.approx(0.5)
        assert pos.unrealised_pnl(0.45) == pytest.approx(-0.5)

    def test_unrealised_pnl_sell(self):
        pos = Position("t1", "c1", "SELL", 10, 0.50, "test", "o1")
        assert pos.unrealised_pnl(0.45) == pytest.approx(0.5)
        assert pos.unrealised_pnl(0.55) == pytest.approx(-0.5)

    def test_summary_without_price_fn(self):
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "test", "o1"))
        s = pt.summary()
        assert s["open_positions"] == 1
        assert s["unrealised_pnl"] == 0.0  # no price_fn

    def test_summary_with_price_fn(self):
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "test", "o1"))
        s = pt.summary(price_fn=lambda tid: 0.55)
        assert s["unrealised_pnl"] == pytest.approx(0.5)


class TestPortfolioReconstruction:
    def test_reconstruct_no_trades(self):
        pt = PortfolioTracker()
        pt.reconstruct_from_trades([])
        assert pt.open_position_count() == 0
        assert pt.realised_pnl == 0.0

    def test_reconstruct_open_position(self):
        trades = [
            {"token_id": "t1", "condition_id": "c1", "side": "BUY",
             "size": 10.0, "price": 0.50, "strategy": "test", "order_id": "o1"},
        ]
        pt = PortfolioTracker()
        pt.reconstruct_from_trades(trades)
        assert pt.open_position_count() == 1
        assert "t1" in pt.positions
        assert pt.positions["t1"].entry_price == 0.50

    def test_reconstruct_closed_position(self):
        trades = [
            {"token_id": "t1", "condition_id": "c1", "side": "BUY",
             "size": 10.0, "price": 0.50, "strategy": "test", "order_id": "o1"},
            {"token_id": "t1", "condition_id": "c1", "side": "SELL",
             "size": 10.0, "price": 0.55, "strategy": "test", "order_id": "o2"},
        ]
        pt = PortfolioTracker()
        pt.reconstruct_from_trades(trades)
        assert pt.open_position_count() == 0
        assert pt.realised_pnl == pytest.approx(0.5)

    def test_reconstruct_mixed(self):
        trades = [
            {"token_id": "t1", "condition_id": "c1", "side": "BUY",
             "size": 10.0, "price": 0.50, "strategy": "test", "order_id": "o1"},
            {"token_id": "t2", "condition_id": "c2", "side": "BUY",
             "size": 20.0, "price": 0.60, "strategy": "test", "order_id": "o2"},
            {"token_id": "t1", "condition_id": "c1", "side": "SELL",
             "size": 10.0, "price": 0.55, "strategy": "test", "order_id": "o3"},
        ]
        pt = PortfolioTracker()
        pt.reconstruct_from_trades(trades)
        assert pt.open_position_count() == 1
        assert "t2" in pt.positions
        assert pt.realised_pnl == pytest.approx(0.5)


class TestPositionMerging:
    def test_same_side_buy_uses_weighted_average_entry(self):
        """Two BUYs on the same token merge with weighted-average entry price."""
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "s", "o1"))
        pt.open_position(Position("t1", "c1", "BUY", 30, 0.60, "s", "o2"))
        assert pt.open_position_count() == 1
        pos = pt.positions["t1"]
        # (10 * 0.50 + 30 * 0.60) / 40 = 23/40 = 0.575
        assert pos.size == pytest.approx(40.0)
        assert pos.entry_price == pytest.approx(0.575)

    def test_same_side_sell_uses_weighted_average_entry(self):
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "SELL", 10, 0.60, "s", "o1"))
        pt.open_position(Position("t1", "c1", "SELL", 10, 0.50, "s", "o2"))
        pos = pt.positions["t1"]
        assert pos.size == pytest.approx(20.0)
        assert pos.entry_price == pytest.approx(0.55)

    def test_opposite_side_reduces_existing(self):
        """SELL fill against existing BUY closes (partially) with correct PnL."""
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "s", "o1"))
        # Incoming SELL 4 @ 0.60 → closes 4 of the 10-share long
        pt.open_position(Position("t1", "c1", "SELL", 4, 0.60, "s", "o2"))
        assert pt.open_position_count() == 1
        pos = pt.positions["t1"]
        assert pos.side == "BUY"
        assert pos.size == pytest.approx(6.0)
        assert pos.entry_price == pytest.approx(0.50)  # unchanged
        # Realised PnL: (0.60 - 0.50) * 4 = 0.40
        assert pt.realised_pnl == pytest.approx(0.40)

    def test_opposite_side_flip(self):
        """Incoming SELL larger than existing BUY → long fully closed and a
        new SELL is opened for the residual."""
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "s", "o1"))
        pt.open_position(Position("t1", "c1", "SELL", 15, 0.60, "s", "o2"))
        # Long of 10 closed at 0.60 → PnL = (0.60-0.50)*10 = 1.0
        # Residual 5 opens a new SELL position at 0.60
        assert pt.realised_pnl == pytest.approx(1.0)
        assert pt.open_position_count() == 1
        pos = pt.positions["t1"]
        assert pos.side == "SELL"
        assert pos.size == pytest.approx(5.0)
        assert pos.entry_price == pytest.approx(0.60)


class TestPartialClose:
    def test_partial_close_reduces_size(self):
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "s", "o1"))
        pnl = pt.close_position("t1", 0.55, size=4)
        # (0.55 - 0.50) * 4 = 0.20
        assert pnl == pytest.approx(0.20)
        assert pt.positions["t1"].size == pytest.approx(6.0)
        # Entry price unchanged on partial close
        assert pt.positions["t1"].entry_price == pytest.approx(0.50)

    def test_partial_close_then_full_close(self):
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "s", "o1"))
        pt.close_position("t1", 0.55, size=4)  # realises 0.20
        pt.close_position("t1", 0.60)  # closes remaining 6 @ 0.60
        assert pt.open_position_count() == 0
        # 0.20 + (0.60 - 0.50) * 6 = 0.20 + 0.60 = 0.80
        assert pt.realised_pnl == pytest.approx(0.80)

    def test_close_size_larger_than_position_clamps(self):
        """Requesting to close more than held closes the full position."""
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "s", "o1"))
        pnl = pt.close_position("t1", 0.55, size=100)
        # Clamps to held size (10)
        assert pnl == pytest.approx(0.50)
        assert pt.open_position_count() == 0

    def test_partial_close_on_short(self):
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "SELL", 10, 0.60, "s", "o1"))
        pnl = pt.close_position("t1", 0.50, size=3)
        # (0.60 - 0.50) * 3 = 0.30
        assert pnl == pytest.approx(0.30)
        assert pt.positions["t1"].size == pytest.approx(7.0)


class TestReconstructionWithMerging:
    def test_multiple_buys_same_token_merge(self):
        """Reconstruction merges repeated BUYs into a weighted-average entry."""
        trades = [
            {"token_id": "t1", "condition_id": "c1", "side": "BUY",
             "size": 10.0, "price": 0.50, "strategy": "s", "order_id": "o1"},
            {"token_id": "t1", "condition_id": "c1", "side": "BUY",
             "size": 30.0, "price": 0.60, "strategy": "s", "order_id": "o2"},
        ]
        pt = PortfolioTracker()
        pt.reconstruct_from_trades(trades)
        assert pt.open_position_count() == 1
        pos = pt.positions["t1"]
        assert pos.size == pytest.approx(40.0)
        assert pos.entry_price == pytest.approx(0.575)

    def test_partial_sell_leaves_remainder(self):
        """A SELL smaller than the accumulated long only closes part of it."""
        trades = [
            {"token_id": "t1", "condition_id": "c1", "side": "BUY",
             "size": 10.0, "price": 0.50, "strategy": "s", "order_id": "o1"},
            {"token_id": "t1", "condition_id": "c1", "side": "SELL",
             "size": 4.0, "price": 0.55, "strategy": "s", "order_id": "o2"},
        ]
        pt = PortfolioTracker()
        pt.reconstruct_from_trades(trades)
        assert pt.open_position_count() == 1
        pos = pt.positions["t1"]
        assert pos.size == pytest.approx(6.0)
        assert pt.realised_pnl == pytest.approx(0.20)

    def test_sell_larger_than_long_flips(self):
        trades = [
            {"token_id": "t1", "condition_id": "c1", "side": "BUY",
             "size": 10.0, "price": 0.50, "strategy": "s", "order_id": "o1"},
            {"token_id": "t1", "condition_id": "c1", "side": "SELL",
             "size": 15.0, "price": 0.60, "strategy": "s", "order_id": "o2"},
        ]
        pt = PortfolioTracker()
        pt.reconstruct_from_trades(trades)
        assert pt.realised_pnl == pytest.approx(1.0)
        assert pt.open_position_count() == 1
        pos = pt.positions["t1"]
        assert pos.side == "SELL"
        assert pos.size == pytest.approx(5.0)

    def test_reconstruction_skips_zero_size_trades(self):
        """Zero or negative size trades (bad data) are skipped, not crashing."""
        trades = [
            {"token_id": "t1", "condition_id": "c1", "side": "BUY",
             "size": 0.0, "price": 0.50, "strategy": "s", "order_id": "o1"},
            {"token_id": "t1", "condition_id": "c1", "side": "BUY",
             "size": 5.0, "price": 0.50, "strategy": "s", "order_id": "o2"},
        ]
        pt = PortfolioTracker()
        pt.reconstruct_from_trades(trades)
        assert pt.positions["t1"].size == pytest.approx(5.0)
