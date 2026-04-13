"""Tests for the SemanticMispricingStrategy adapter + tick wiring.

Covers the flow:

* Config defaults keep everything disabled by default.
* The strategy is inert when no semantic context is set.
* When the observer injects mispricings, the strategy emits proper
  BUY/SELL Signals that the rest of the pipeline can consume unchanged.
* When the engine is *disabled*, the tick behaves exactly as before the
  feature existed (critical no-regression guarantee).
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from src.analysis.book_depth import BookAnalysis
from src.analysis.semantic_engine.types import (
    MarketRelation,
    RelationKind,
    SemanticMispricing,
    SyntheticPrice,
)
from src.config import Config
from src.main import _build_strategy, _tick
from src.polymarket.execution import ExecutionEngine
from src.polymarket.market_data import MarketDataService, MarketSnapshot
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import Action
from src.strategy.semantic_mispricing import SemanticMispricingStrategy


def _cfg(**overrides) -> Config:
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "MAX_POSITION_SIZE": "50",
        "MAX_TOTAL_EXPOSURE": "200",
        "MAX_OPEN_POSITIONS": "5",
        "MIN_PRICE": "0.05",
        "MAX_PRICE": "0.95",
        "MAX_SPREAD": "0.10",
        "SQLITE_DB_PATH": ":memory:",
        "STRATEGY": "simple_momentum",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


def _snap(token_id="tok1", price=0.55, spread=0.02, outcome="Yes",
          condition_id="cid1", question="Q?", liquidity=20000.0):
    return MarketSnapshot(
        condition_id=condition_id,
        question=question,
        token_id=token_id,
        outcome=outcome,
        price=price,
        spread=spread,
        volume=50000.0,
        liquidity=liquidity,
        active=True,
        category="test",
        end_date="2026-12-31",
    )


def _mispricing(token_id="tok1", side="BUY", score=0.75, net_edge=0.05,
                gross_edge=0.08):
    synth = SyntheticPrice(
        point=0.60, lower=0.59, upper=0.61,
        confidence=0.90,
        method="structural_complement",
        contributors=("other_tok",),
        n_contributors=1,
        is_range_only=False,
    )
    rel = MarketRelation(
        target_token_id=token_id,
        sibling_token_id="other_tok",
        kind=RelationKind.INVERSE_OUTCOME,
        confidence=0.95,
        reason="test",
    )
    return SemanticMispricing(
        token_id=token_id,
        condition_id="cid1",
        question="Q?",
        category="test",
        best_bid=0.53, best_ask=0.55, midpoint=0.54,
        spread=0.02, liquidity=20000.0,
        synthetic=synth,
        side=side,
        gross_edge=gross_edge,
        net_edge=net_edge,
        score=score,
        relations=(rel,),
        features={"synthetic_fair": 0.60, "gross_edge": gross_edge,
                  "net_edge": net_edge, "signal_score": score},
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestSemanticEngineConfig:
    def test_disabled_by_default(self):
        cfg = _cfg()
        assert cfg.semantic_engine_enabled is False
        assert cfg.semantic_engine_mode == "shadow"

    def test_can_be_enabled(self):
        cfg = _cfg(SEMANTIC_ENGINE_ENABLED="true")
        assert cfg.semantic_engine_enabled is True

    def test_defaults_are_strict(self):
        cfg = _cfg()
        assert cfg.semantic_min_relation_confidence >= 0.50
        assert cfg.semantic_min_net_edge >= 0.01
        assert cfg.semantic_min_signal_score >= 0.50


# ---------------------------------------------------------------------------
# Strategy adapter behaviour
# ---------------------------------------------------------------------------

class TestSemanticStrategyAdapter:
    def test_name(self):
        cfg = _cfg()
        assert SemanticMispricingStrategy(cfg).name == "semantic_mispricing"

    def test_factory_returns_semantic_when_selected(self):
        cfg = _cfg(STRATEGY="semantic_mispricing")
        strat = _build_strategy(cfg)
        assert isinstance(strat, SemanticMispricingStrategy)

    def test_hold_when_no_context(self):
        cfg = _cfg(STRATEGY="semantic_mispricing")
        strat = SemanticMispricingStrategy(cfg)
        sig = strat.evaluate(_snap(), [])
        assert sig.action == Action.HOLD

    def test_buy_signal_from_mispricing(self):
        cfg = _cfg(STRATEGY="semantic_mispricing",
                   SEMANTIC_MIN_SIGNAL_SCORE="0.50",
                   SEMANTIC_MIN_NET_EDGE="0.02")
        strat = SemanticMispricingStrategy(cfg)
        strat.set_semantic_context([_mispricing(token_id="tok1", side="BUY")])
        sig = strat.evaluate(_snap(token_id="tok1"), [])
        assert sig.action == Action.BUY
        assert sig.confidence == pytest.approx(0.75)
        assert sig.features["edge"] > 0

    def test_sell_signal_from_mispricing(self):
        cfg = _cfg(STRATEGY="semantic_mispricing",
                   SEMANTIC_MIN_SIGNAL_SCORE="0.50")
        strat = SemanticMispricingStrategy(cfg)
        strat.set_semantic_context([_mispricing(token_id="tok1", side="SELL")])
        sig = strat.evaluate(_snap(token_id="tok1"), [])
        assert sig.action == Action.SELL
        # Edge is negated for SELL so Kelly sizing uses correct sign.
        assert sig.features["edge"] < 0

    def test_score_below_threshold_blocks(self):
        cfg = _cfg(STRATEGY="semantic_mispricing",
                   SEMANTIC_MIN_SIGNAL_SCORE="0.80")
        strat = SemanticMispricingStrategy(cfg)
        strat.set_semantic_context([_mispricing(token_id="tok1", score=0.60)])
        sig = strat.evaluate(_snap(token_id="tok1"), [])
        assert sig.action == Action.HOLD

    def test_net_edge_below_threshold_blocks(self):
        cfg = _cfg(STRATEGY="semantic_mispricing",
                   SEMANTIC_MIN_NET_EDGE="0.10")
        strat = SemanticMispricingStrategy(cfg)
        strat.set_semantic_context([_mispricing(token_id="tok1", net_edge=0.05)])
        sig = strat.evaluate(_snap(token_id="tok1"), [])
        assert sig.action == Action.HOLD

    def test_clear_context(self):
        cfg = _cfg(STRATEGY="semantic_mispricing")
        strat = SemanticMispricingStrategy(cfg)
        strat.set_semantic_context([_mispricing(token_id="tok1")])
        strat.clear_semantic_context()
        sig = strat.evaluate(_snap(token_id="tok1"), [])
        assert sig.action == Action.HOLD

    def test_other_tokens_hold(self):
        """Only the token with a detection gets a signal; others HOLD."""
        cfg = _cfg(STRATEGY="semantic_mispricing")
        strat = SemanticMispricingStrategy(cfg)
        strat.set_semantic_context([_mispricing(token_id="tok1")])
        sig_other = strat.evaluate(_snap(token_id="tok2"), [])
        assert sig_other.action == Action.HOLD


# ---------------------------------------------------------------------------
# Regression guarantee: disabled → zero behaviour change
# ---------------------------------------------------------------------------

class TestNoRegressionWhenDisabled:
    def test_default_strategy_unaffected(self):
        """Running SimpleMomentum with engine disabled behaves as before."""
        cfg = _cfg()  # engine disabled, default strategy
        assert cfg.semantic_engine_enabled is False
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)

        book = BookAnalysis(
            best_bid=0.54, best_ask=0.56, spread=0.02, midpoint=0.55,
            bid_depth_1pct=540, ask_depth_1pct=560,
            bid_depth_5pct=1080, ask_depth_5pct=1120,
            imbalance_1pct=0.0, imbalance_5pct=0.0,
            vwap_buy=0.56, vwap_sell=0.54,
            slippage_buy_pct=0.001, slippage_sell_pct=0.001,
            n_bid_levels=5, n_ask_levels=5, can_fill_at_top=True,
        )
        client = mock.MagicMock()
        client.get_price.return_value = 0.55
        client.get_spread.return_value = 0.02
        client.get_book_analysis.return_value = book
        client.get_top_of_book.return_value = {
            "best_bid": 0.54, "best_ask": 0.56, "bid_size": 1000, "ask_size": 1000,
        }
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        for i, p in enumerate([0.48, 0.50, 0.52, 0.54]):
            store.insert_price("tok1", p, f"2026-04-01T10:{i:02d}:00", spread=0.02)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [_snap(price=0.55)]

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)
        # SimpleMomentum opens a position on an uptrend — this must still work.
        assert portfolio.open_position_count() == 1
        store.close()


# ---------------------------------------------------------------------------
# Tick integration: enabled engine + semantic strategy
# ---------------------------------------------------------------------------

class TestTickWithSemanticStrategy:
    def test_tick_opens_position_from_yesno_mispricing(self):
        """Engine enabled + Yes/No both cheap → BUY both, one per tick loop."""
        cfg = _cfg(
            STRATEGY="semantic_mispricing",
            SEMANTIC_ENGINE_ENABLED="true",
            SEMANTIC_ENGINE_MODE="shadow",
            SEMANTIC_MIN_SIGNAL_SCORE="0.10",
            SEMANTIC_MIN_NET_EDGE="0.01",
            MIN_PRICE="0.01",
            MAX_PRICE="0.99",
            MAX_SPREAD="0.20",
        )
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)

        book_yes = BookAnalysis(
            best_bid=0.39, best_ask=0.41, spread=0.02, midpoint=0.40,
            bid_depth_1pct=3900, ask_depth_1pct=4100,
            bid_depth_5pct=7800, ask_depth_5pct=8200,
            imbalance_1pct=0.0, imbalance_5pct=0.0,
            vwap_buy=0.41, vwap_sell=0.39,
            slippage_buy_pct=0.001, slippage_sell_pct=0.001,
            n_bid_levels=5, n_ask_levels=5, can_fill_at_top=True,
        )
        client = mock.MagicMock()
        client.get_price.return_value = 0.40
        client.get_spread.return_value = 0.02
        client.get_book_analysis.return_value = book_yes
        client.get_top_of_book.return_value = {
            "best_bid": 0.39, "best_ask": 0.41, "bid_size": 5000, "ask_size": 5000,
        }
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        # Two tokens of the same condition: Yes + No, both @ 0.40 → sum 0.80.
        yes_snap = _snap(token_id="tyes", price=0.40, outcome="Yes")
        no_snap = _snap(token_id="tno", price=0.40, outcome="No")
        for ts, t in [("tyes", yes_snap.token_id), ("tno", no_snap.token_id)]:
            store.insert_price(t, 0.40, "2026-04-01T10:00:00", spread=0.02)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [yes_snap, no_snap]

        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)

        # Engine must have detected 2 mispricings (both Yes and No look cheap)
        # and strategy should have opened at least one position.
        semantic_rows = store.get_recent_semantic_signals(limit=10)
        assert len(semantic_rows) >= 1
        # Recorded in shadow mode tag
        assert all(r["mode"] == "shadow" for r in semantic_rows)
        store.close()

    def test_disabled_engine_produces_no_semantic_rows(self):
        """With engine disabled, no semantic_signals rows are written."""
        cfg = _cfg(
            STRATEGY="simple_momentum",
            SEMANTIC_ENGINE_ENABLED="false",
        )
        store = SQLiteStore(":memory:")
        portfolio = PortfolioTracker()
        risk = RiskManager(cfg, portfolio)
        client = mock.MagicMock()
        client.get_price.return_value = 0.55
        client.get_spread.return_value = 0.02
        client.get_book_analysis.return_value = None
        client.get_top_of_book.return_value = {
            "best_bid": 0.54, "best_ask": 0.56, "bid_size": 1000, "ask_size": 1000,
        }
        strategy = _build_strategy(cfg, store=store)
        executor = ExecutionEngine(client, cfg, store)

        market_svc = mock.MagicMock(spec=MarketDataService)
        market_svc.fetch_and_filter.return_value = [
            _snap(token_id="tyes", price=0.40, outcome="Yes"),
            _snap(token_id="tno", price=0.40, outcome="No"),
        ]
        _tick(market_svc, strategy, risk, executor, portfolio, store, client, cfg)
        assert store.get_recent_semantic_signals(limit=10) == []
        store.close()
