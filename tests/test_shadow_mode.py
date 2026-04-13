"""Tests for shadow mode.

Shadow mode generates signals and records decisions, but never executes
trades or updates portfolio state.  Useful for A/B testing strategy
changes without contaminating real paper PnL.
"""

from __future__ import annotations

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


class TestShadowModeConfig:
    def test_default_off(self):
        cfg = _cfg()
        assert cfg.shadow_mode is False

    def test_can_enable(self):
        cfg = _cfg(SHADOW_MODE="true")
        assert cfg.shadow_mode is True


class TestShadowModeTick:
    def test_shadow_mode_does_not_open_position(self):
        """When shadow_mode=true, a winning signal records a SHADOW_ENTRY_*
        decision but never opens a position or inserts a trade."""
        cfg = _cfg(SHADOW_MODE="true")
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)
        client = _make_client([0.55] * 10, _deep_book())
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        # Seed uptrend history so momentum fires BUY
        for i, p in enumerate([0.48, 0.50, 0.52, 0.54]):
            store.insert_price("tok1", p, f"2026-04-01T10:{i:02d}:00", spread=0.02)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [_make_snapshot(price=0.55)]

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        # No position opened
        assert portfolio.open_position_count() == 0
        # No trades recorded
        assert store.get_trades() == []
        # But a SHADOW_ENTRY_BUY decision was logged
        decisions = store.get_decisions(limit=10)
        shadow_decisions = [d for d in decisions if d["action"] == "SHADOW_ENTRY_BUY"]
        assert len(shadow_decisions) == 1
        store.close()

    def test_non_shadow_mode_still_opens_position(self):
        """Default behaviour (shadow_mode=false) is unchanged."""
        cfg = _cfg()  # default: shadow_mode=false
        assert cfg.shadow_mode is False
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

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        # Position opened as before
        assert portfolio.open_position_count() == 1
        store.close()

    def test_shadow_decision_has_forensic_detail(self):
        """Shadow decisions carry enough features to reconstruct what would
        have been executed."""
        cfg = _cfg(SHADOW_MODE="true")
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

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        decisions = store.get_decisions(limit=10)
        shadow = [d for d in decisions if d["action"] == "SHADOW_ENTRY_BUY"][0]
        import json
        feats = json.loads(shadow["features"]) if shadow["features"] else {}
        assert feats.get("shadow") is True
        assert "would_size" in feats
        assert "would_price" in feats
        store.close()
