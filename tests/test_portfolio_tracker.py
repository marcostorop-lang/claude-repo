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
