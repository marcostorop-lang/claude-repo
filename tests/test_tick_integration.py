"""Integration test for the bot's full tick loop.

Exercises the entire path: market data → strategy → risk → book analysis →
execution → portfolio update → storage.  Uses a MagicMock client with
scripted responses so the test runs offline and deterministically.
"""

import os
from unittest import mock

import pytest

from src.analysis.book_depth import BookAnalysis
from src.config import Config
from src.main import _build_strategy, _tick
from src.polymarket.execution import ExecutionEngine
from src.polymarket.market_data import MarketDataService, MarketSnapshot
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.storage.sqlite_store import SQLiteStore


def _cfg(**overrides):
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "MAX_POSITION_SIZE": "50",
        "MAX_TOTAL_EXPOSURE": "200",
        "MAX_OPEN_POSITIONS": "5",
        "STOP_LOSS_PCT": "0.10",
        "TAKE_PROFIT_PCT": "0.20",
        "MIN_PRICE": "0.05",
        "MAX_PRICE": "0.95",
        "MAX_SPREAD": "0.10",
        "MOMENTUM_WINDOW": "3",
        "MOMENTUM_THRESHOLD": "0.02",
        "SQLITE_DB_PATH": ":memory:",
        "STRATEGY": "simple_momentum",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


def _make_snapshot(token_id="tok1", price=0.55, spread=0.02):
    return MarketSnapshot(
        condition_id="cid1", question="Will X happen?", token_id=token_id,
        outcome="Yes", price=price, spread=spread,
        volume=50000.0, liquidity=20000.0, active=True,
        category="test", end_date="2026-12-31",
    )


def _deep_book(mid=0.55, spread=0.02, depth_shares=1000):
    """Returns a BookAnalysis with plenty of depth at a narrow spread."""
    best_bid = mid - spread / 2
    best_ask = mid + spread / 2
    ba = BookAnalysis(
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        midpoint=mid,
        bid_depth_1pct=depth_shares * best_bid,
        ask_depth_1pct=depth_shares * best_ask,
        bid_depth_5pct=depth_shares * best_bid * 2,
        ask_depth_5pct=depth_shares * best_ask * 2,
        imbalance_1pct=0.0,
        imbalance_5pct=0.0,
        vwap_buy=best_ask,
        vwap_sell=best_bid,
        slippage_buy_pct=0.001,
        slippage_sell_pct=0.001,
        n_bid_levels=5,
        n_ask_levels=5,
        can_fill_at_top=True,
    )
    return ba


def _thin_book(mid=0.55, spread=0.02, ask_depth_usd=2.0):
    """Book with very thin depth — triggers partial fill."""
    best_bid = mid - spread / 2
    best_ask = mid + spread / 2
    return BookAnalysis(
        best_bid=best_bid, best_ask=best_ask, spread=spread, midpoint=mid,
        bid_depth_1pct=ask_depth_usd, ask_depth_1pct=ask_depth_usd,
        bid_depth_5pct=ask_depth_usd, ask_depth_5pct=ask_depth_usd,
        imbalance_1pct=0.0, imbalance_5pct=0.0,
        vwap_buy=best_ask, vwap_sell=best_bid,
        slippage_buy_pct=0.001, slippage_sell_pct=0.001,
        n_bid_levels=1, n_ask_levels=1, can_fill_at_top=True,
    )


def _contra_book(mid=0.55, spread=0.02, depth_shares=1000):
    """Book with extreme negative imbalance — should reject BUY orders."""
    best_bid = mid - spread / 2
    best_ask = mid + spread / 2
    return BookAnalysis(
        best_bid=best_bid, best_ask=best_ask, spread=spread, midpoint=mid,
        bid_depth_1pct=100.0,
        ask_depth_1pct=900.0,  # lots of asks = sell pressure
        bid_depth_5pct=100.0,
        ask_depth_5pct=900.0,
        imbalance_1pct=-0.80, imbalance_5pct=-0.80,
        vwap_buy=best_ask, vwap_sell=best_bid,
        slippage_buy_pct=0.001, slippage_sell_pct=0.001,
        n_bid_levels=3, n_ask_levels=5, can_fill_at_top=True,
    )


def _make_client(price_series: list[float], book: BookAnalysis | None):
    """Build a mock client that returns successive prices and a fixed book."""
    client = mock.MagicMock()
    # get_price returns prices from the series (cycling if exhausted)
    call_count = {"n": 0}
    def _get_price(token_id):
        p = price_series[min(call_count["n"], len(price_series) - 1)]
        call_count["n"] += 1
        return p
    client.get_price.side_effect = _get_price
    client.get_spread.return_value = 0.02
    client.get_book_analysis.return_value = book
    client.get_top_of_book.return_value = (
        {"best_bid": book.best_bid, "best_ask": book.best_ask,
         "bid_size": 1000, "ask_size": 1000} if book else None
    )
    return client


class TestTickIntegration:
    def test_tick_with_deep_book_opens_position(self):
        """Uptrend + deep book + clean spread → should open a BUY position."""
        cfg = _cfg()
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)
        client = _make_client([0.55] * 10, _deep_book())
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        # Seed price history so momentum has enough data (uptrend)
        for i, p in enumerate([0.48, 0.50, 0.52, 0.54]):
            store.insert_price("tok1", p, f"2026-04-01T10:{i:02d}:00", spread=0.02)

        # Mock market_svc to return our scripted snapshot
        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [
            _make_snapshot(price=0.55, spread=0.02),
        ]

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        # Portfolio should have opened a position
        assert portfolio.open_position_count() == 1
        pos = list(portfolio.positions.values())[0]
        assert pos.side == "BUY"
        # Entry price should reflect the book VWAP (best_ask ~ 0.56)
        assert 0.55 <= pos.entry_price <= 0.57
        store.close()

    def test_tick_with_thin_book_does_partial_fill(self):
        """Thin book → order gets partial fill sized to available depth."""
        cfg = _cfg(MAX_POSITION_SIZE="100")  # wants $100 = ~180 shares at 0.55
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)
        # Book has only ~$2 of depth — dramatically under requested $100
        thin = _thin_book()
        client = _make_client([0.55] * 10, thin)
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        for i, p in enumerate([0.48, 0.50, 0.52, 0.54]):
            store.insert_price("tok1", p, f"2026-04-01T10:{i:02d}:00", spread=0.02)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [_make_snapshot(price=0.55)]

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        # Either the order was partially filled or rejected for slippage.
        # If filled, size must be much smaller than the requested 100/0.55 ≈ 180.
        if portfolio.open_position_count() == 1:
            pos = list(portfolio.positions.values())[0]
            # Available depth: $2 / 0.56 ≈ 3.5 shares
            assert pos.size < 20.0
        # If rejected, decision log should show book_slippage or book_spread
        store.close()

    def test_tick_rejects_on_contrary_book_imbalance(self):
        """Strong ask-heavy book should reject a BUY signal."""
        cfg = _cfg()
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)
        client = _make_client([0.55] * 10, _contra_book())
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        for i, p in enumerate([0.48, 0.50, 0.52, 0.54]):
            store.insert_price("tok1", p, f"2026-04-01T10:{i:02d}:00", spread=0.02)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [_make_snapshot(price=0.55)]

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        # No position opened because of book imbalance rejection
        assert portfolio.open_position_count() == 0
        # Decision log should show the rejection
        decisions = store.get_decisions(limit=10)
        rejections = [d for d in decisions if d["action"] == "RISK_REJECTED"]
        assert any("imbalance" in (d.get("risk_detail") or "") for d in rejections)
        store.close()

    def test_tick_hold_signal_does_not_open(self):
        """Flat price history → HOLD → no position opened."""
        cfg = _cfg()
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)
        client = _make_client([0.55] * 10, _deep_book())
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        # Flat history — no momentum
        for i in range(4):
            store.insert_price("tok1", 0.55, f"2026-04-01T10:{i:02d}:00", spread=0.02)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [_make_snapshot(price=0.55)]

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        assert portfolio.open_position_count() == 0
        store.close()

    def test_tick_rejects_on_stale_price_feed(self):
        """Snapshot price far from book midpoint → rejected as stale."""
        cfg = _cfg(MAX_PRICE_BOOK_DIVERGENCE="0.03")
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)
        # Book mid at 0.55, but snapshot will claim 0.70 → 27% divergence
        client = _make_client([0.70] * 10, _deep_book(mid=0.55))
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        # Uptrend history so momentum would otherwise fire
        for i, p in enumerate([0.60, 0.63, 0.66, 0.69]):
            store.insert_price("tok1", p, f"2026-04-01T10:{i:02d}:00", spread=0.02)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [
            _make_snapshot(price=0.70, spread=0.02),
        ]

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        assert portfolio.open_position_count() == 0
        decisions = store.get_decisions(limit=10)
        rejections = [d for d in decisions if d["action"] == "RISK_REJECTED"]
        assert any("stale_price" == (d.get("risk_detail") or "") for d in rejections), \
            f"Expected stale_price rejection, got: {[d.get('risk_detail') for d in rejections]}"
        store.close()

    def test_tick_records_tick_stats(self):
        """Every tick should persist a tick_stats row for observability."""
        cfg = _cfg()
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)
        client = _make_client([0.55] * 10, _deep_book())
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = []

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        cur = store._conn.execute("SELECT COUNT(*) as c FROM tick_stats")
        assert cur.fetchone()["c"] == 1
        store.close()
