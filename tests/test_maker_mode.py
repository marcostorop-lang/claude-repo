"""Tests for the opt-in maker-preferred order mode (paper-only).

Default ``order_mode='taker'`` preserves the existing taker path exactly —
the tests in this module focus on the new maker branch:

* Config exposes ``order_mode`` / ``maker_fill_prob`` with safe defaults.
* ``OrderRequest.order_type`` defaults to ``"taker"`` for back-compat.
* The paper executor honours ``order_type='maker'`` with probabilistic fills
  and no slippage.
* A maker miss records a ``MAKER_MISS`` decision without contaminating
  portfolio or trade state.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from src.analysis.book_depth import BookAnalysis
from src.config import Config
from src.main import _build_strategy, _tick
from src.polymarket.execution import ExecutionEngine, OrderRequest
from src.polymarket.market_data import MarketDataService, MarketSnapshot
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.storage.sqlite_store import SQLiteStore


def _cfg(**overrides) -> Config:
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
        condition_id="cid1", question="Q?", token_id=token_id, outcome="Yes",
        price=price, spread=spread, volume=50000.0, liquidity=20000.0,
        active=True, category="test", end_date="2026-12-31",
    )


def _deep_book(mid=0.55, spread=0.02, depth_shares=1000):
    best_bid = mid - spread / 2
    best_ask = mid + spread / 2
    return BookAnalysis(
        best_bid=best_bid, best_ask=best_ask, spread=spread, midpoint=mid,
        bid_depth_1pct=depth_shares * best_bid, ask_depth_1pct=depth_shares * best_ask,
        bid_depth_5pct=depth_shares * best_bid * 2, ask_depth_5pct=depth_shares * best_ask * 2,
        imbalance_1pct=0.0, imbalance_5pct=0.0,
        vwap_buy=best_ask, vwap_sell=best_bid,
        slippage_buy_pct=0.001, slippage_sell_pct=0.001,
        n_bid_levels=5, n_ask_levels=5, can_fill_at_top=True,
    )


def _make_client(price_series, book):
    client = mock.MagicMock()
    call_count = {"n": 0}
    def _get_price(token_id):
        p = price_series[min(call_count["n"], len(price_series) - 1)]
        call_count["n"] += 1
        return p
    client.get_price.side_effect = _get_price
    client.get_spread.return_value = 0.02
    client.get_book_analysis.return_value = book
    client.get_top_of_book.return_value = {
        "best_bid": book.best_bid, "best_ask": book.best_ask,
        "bid_size": 1000, "ask_size": 1000,
    }
    return client


class TestMakerModeConfig:
    def test_default_taker(self):
        cfg = _cfg()
        assert cfg.order_mode == "taker"

    def test_default_fill_prob(self):
        cfg = _cfg()
        assert cfg.maker_fill_prob == pytest.approx(0.7)

    def test_can_enable_maker_preferred(self):
        cfg = _cfg(ORDER_MODE="maker_preferred", MAKER_FILL_PROB="0.9")
        assert cfg.order_mode == "maker_preferred"
        assert cfg.maker_fill_prob == pytest.approx(0.9)


class TestOrderRequestDefaults:
    def test_order_type_defaults_to_taker(self):
        r = OrderRequest(
            token_id="t", condition_id="c", side="BUY",
            size=10, price=0.5, strategy="s",
        )
        assert r.order_type == "taker"


class TestPaperMakerExecution:
    def _build(self, cfg):
        store = SQLiteStore(":memory:")
        client = mock.MagicMock()
        return ExecutionEngine(client, cfg, store), store

    def test_maker_fills_when_random_below_threshold(self):
        cfg = _cfg(ORDER_MODE="maker_preferred", MAKER_FILL_PROB="0.9")
        engine, store = self._build(cfg)
        order = OrderRequest(
            token_id="tok1", condition_id="c", side="BUY",
            size=100, price=0.54,  # best_bid
            strategy="s", spread=0.02, is_book_price=True,
            order_type="maker",
        )
        with mock.patch("random.random", return_value=0.1):  # < 0.9 → fill
            result = engine.execute(order)
        assert result.success is True
        assert result.filled_size == 100
        assert result.fill_price == pytest.approx(0.54)
        # Trade was recorded
        trades = store.get_trades()
        assert len(trades) == 1
        assert trades[0]["price"] == pytest.approx(0.54)
        store.close()

    def test_maker_misses_when_random_above_threshold(self):
        cfg = _cfg(ORDER_MODE="maker_preferred", MAKER_FILL_PROB="0.3")
        engine, store = self._build(cfg)
        order = OrderRequest(
            token_id="tok1", condition_id="c", side="BUY",
            size=100, price=0.54, strategy="s", spread=0.02,
            is_book_price=True, order_type="maker",
        )
        with mock.patch("random.random", return_value=0.9):  # > 0.3 → miss
            result = engine.execute(order)
        assert result.success is False
        assert result.filled_size == 0
        # No trade recorded on a miss
        assert store.get_trades() == []
        store.close()

    def test_maker_no_slippage_applied(self):
        """Maker fills at posted price exactly — half-spread is NOT added."""
        cfg = _cfg(ORDER_MODE="maker_preferred", MAKER_FILL_PROB="1.0")
        engine, store = self._build(cfg)
        order = OrderRequest(
            token_id="tok1", condition_id="c", side="BUY",
            size=100, price=0.54, strategy="s", spread=0.02,
            is_book_price=True, order_type="maker",
        )
        with mock.patch("random.random", return_value=0.0):
            result = engine.execute(order)
        assert result.fill_price == pytest.approx(0.54)
        store.close()

    def test_taker_path_unchanged(self):
        """Default taker path still behaves as before."""
        cfg = _cfg()
        engine, store = self._build(cfg)
        order = OrderRequest(
            token_id="tok1", condition_id="c", side="BUY",
            size=100, price=0.55, strategy="s", spread=0.02,
        )
        result = engine.execute(order)
        assert result.success is True
        # Half-spread slippage added on taker BUY
        assert result.fill_price == pytest.approx(0.56)
        store.close()


class TestMakerModeTick:
    def test_tick_posts_at_best_bid_for_buy(self):
        """In maker_preferred mode a BUY order posts at best_bid, not ask."""
        cfg = _cfg(ORDER_MODE="maker_preferred", MAKER_FILL_PROB="1.0")
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)
        book = _deep_book(mid=0.55, spread=0.02)
        client = _make_client([0.55] * 10, book)
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        for i, p in enumerate([0.48, 0.50, 0.52, 0.54]):
            store.insert_price("tok1", p, f"2026-04-01T10:{i:02d}:00", spread=0.02)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [_make_snapshot(price=0.55)]

        with mock.patch("random.random", return_value=0.0):
            _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        # Position opened at best_bid (0.54), not best_ask (0.56)
        assert portfolio.open_position_count() == 1
        pos = portfolio.positions["tok1"]
        assert pos.entry_price == pytest.approx(book.best_bid)
        store.close()

    def test_tick_records_maker_miss_decision(self):
        """A maker miss writes a MAKER_MISS decision and no position."""
        cfg = _cfg(ORDER_MODE="maker_preferred", MAKER_FILL_PROB="0.1")
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)
        client = _make_client([0.55] * 10, _deep_book())
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        for i, p in enumerate([0.48, 0.50, 0.52, 0.54]):
            store.insert_price("tok1", p, f"2026-04-01T10:{i:02d}:00", spread=0.02)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [_make_snapshot(price=0.55)]

        with mock.patch("random.random", return_value=0.99):  # almost certain miss
            _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        assert portfolio.open_position_count() == 0
        assert store.get_trades() == []
        decisions = store.get_decisions(limit=20)
        assert any(d["action"] == "MAKER_MISS" for d in decisions)
        store.close()

    def test_taker_tick_unaffected(self):
        """Default taker mode still opens at ask/VWAP, not bid."""
        cfg = _cfg()  # taker
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)
        book = _deep_book(mid=0.55, spread=0.02)
        client = _make_client([0.55] * 10, book)
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        for i, p in enumerate([0.48, 0.50, 0.52, 0.54]):
            store.insert_price("tok1", p, f"2026-04-01T10:{i:02d}:00", spread=0.02)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [_make_snapshot(price=0.55)]

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        assert portfolio.open_position_count() == 1
        pos = portfolio.positions["tok1"]
        # Taker fill is at best_ask (via vwap_buy in this deep book)
        assert pos.entry_price >= book.best_ask - 1e-9
        store.close()
