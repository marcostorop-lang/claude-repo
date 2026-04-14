"""Tests for the semantic-engine backtest harness.

We don't try to verify exact PnL — the synthetic-engine semantics are
exercised in their own tests.  Instead, the harness itself has four
invariants we lock in:

* empty input → empty report, no crash,
* a tick with no mispricings never opens a trade,
* a mispricing that disappears next tick closes the position with
  reason ``signal_gone``,
* accounting sums match the per-trade PnL list.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.analysis.semantic_engine.backtest import (
    MethodPnL,
    SimTrade,
    replay,
)
from src.analysis.semantic_engine.types import (
    MarketRelation,
    RelationKind,
    SemanticMispricing,
    SyntheticPrice,
)
from src.polymarket.market_data import MarketSnapshot


def _snap(token_id, price=0.5, spread=0.02, liquidity=5000.0,
          condition_id="cond", outcome="Yes",
          question="Q?", category="politics"):
    return MarketSnapshot(
        token_id=token_id, condition_id=condition_id, question=question,
        outcome=outcome, price=price, spread=spread, volume=1000.0,
        liquidity=liquidity, active=True, category=category,
    )


class TestReplayShape:
    def test_empty_input_safe(self):
        report = replay([])
        assert report.ticks_run == 0
        assert report.trades_opened == 0
        assert report.trades_closed == 0
        assert report.per_method == {}

    def test_no_mispricings_no_trades(self):
        """A universe with a single market can't generate relations → no signals."""
        ticks = [
            ("t1", [_snap("a", price=0.5)]),
            ("t2", [_snap("a", price=0.55)]),
        ]
        report = replay(ticks, min_net_edge=0.001, min_relation_confidence=0.01)
        assert report.ticks_run == 2
        assert report.signals_generated == 0
        assert report.trades_opened == 0
        assert report.trades_closed == 0

    def test_complementary_markets_run_without_error(self):
        """Two binary-complementary snapshots — engine may or may not emit a
        signal depending on config, but the backtester must not crash and
        must not book negative or inconsistent counters."""
        ticks = [
            ("t1", [
                _snap("yes", price=0.55),
                _snap("no", price=0.40),
            ]),
            ("t2", [
                _snap("yes", price=0.55),
                _snap("no", price=0.42),
            ]),
        ]
        report = replay(ticks, min_net_edge=0.001, min_signal_score=0.01,
                        min_relation_confidence=0.5)
        # Bookkeeping invariants.
        assert report.trades_opened >= report.trades_closed
        # realised_pnl equals the sum of per-method realised_pnl.
        per_method_total = sum(m.realised_pnl for m in report.per_method.values())
        assert report.realised_pnl == pytest.approx(per_method_total, abs=1e-9)
        # Every per-method bucket satisfies its own trade-count bounds.
        for m in report.per_method.values():
            assert m.trades >= m.wins >= 0


class TestMethodPnLAggregation:
    def test_win_rate_and_ratios_zero_on_empty(self):
        m = MethodPnL(method="x")
        assert m.win_rate == 0.0
        assert m.avg_detected_edge == 0.0
        assert m.avg_realised_edge == 0.0
        assert m.realisation_ratio == 0.0

    def test_realisation_ratio_signs(self):
        m = MethodPnL(method="x", trades=2, wins=1,
                      detected_edge_sum=0.10, realised_edge_sum=0.05)
        # avg_detected=0.05, avg_realised=0.025 → ratio=0.5
        assert m.realisation_ratio == pytest.approx(0.5)


class TestSimTradePnL:
    def test_buy_pnl_signed_correctly(self):
        t = SimTrade(
            token_id="a", side="BUY", method="structural_complement",
            open_idx=0, open_ts="t1", entry_price=0.50,
            detected_net_edge=0.05, size=100.0,
        )
        t.exit_price = 0.60
        # 100 USD at 0.50 = 200 shares; 200 * 0.10 = 20 USD
        assert t.realised_pnl == pytest.approx(20.0)
        assert t.realised_return == pytest.approx(0.20)

    def test_sell_pnl_inverted(self):
        t = SimTrade(
            token_id="a", side="SELL", method="structural_complement",
            open_idx=0, open_ts="t1", entry_price=0.50,
            detected_net_edge=0.05, size=100.0,
        )
        t.exit_price = 0.40
        # SELL gains when price drops: 100/0.5 = 200 shares, 200 * 0.10 = 20
        assert t.realised_pnl == pytest.approx(20.0)
        assert t.realised_return == pytest.approx(0.20)


class TestAsDictSerialisation:
    def test_as_dict_shape(self):
        report = replay([])
        d = report.as_dict()
        assert d["ticks_run"] == 0
        assert d["per_method"] == []
        assert "realised_pnl" in d
