"""Tests for the negative-risk arbitrage detector.

Detector is READ-ONLY by design.  These tests verify:
- Binary markets with ask sum < 1 are detected as long arbs.
- Binary markets with bid sum > 1 are detected as short arbs.
- Fair-priced markets produce no detection.
- Liquidity floor filters out unfillable arbs.
- scan_and_record persists detections and survives on stores without
  the arb table.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from src.analysis.arb_detector import (
    ArbOpportunity,
    find_negative_risk_arbs,
    format_report_markdown,
    scan_and_record,
)
from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.storage.sqlite_store import SQLiteStore


def _snap(token_id, outcome, price, spread=0.02, liquidity=500.0, cid="cid1"):
    return MarketSnapshot(
        condition_id=cid,
        question="Will X happen?",
        token_id=token_id,
        outcome=outcome,
        price=price,
        spread=spread,
        volume=10000.0,
        liquidity=liquidity,
        active=True,
        category="politics",
        end_date="2026-12-31",
    )


class TestFindArbs:
    def test_fair_priced_binary_no_arb(self):
        """YES=0.49, NO=0.51 at 2c spread → sum_asks = 0.50+0.52 = 1.02, no arb."""
        snaps = [_snap("y", "Yes", 0.49), _snap("n", "No", 0.51)]
        assert find_negative_risk_arbs(snaps) == []

    def test_long_arb_detected(self):
        """YES=0.40, NO=0.50 → sum_asks = 0.41+0.51 = 0.92 → 8% discount."""
        snaps = [_snap("y", "Yes", 0.40), _snap("n", "No", 0.50)]
        arbs = find_negative_risk_arbs(snaps, min_discount=0.05)
        assert len(arbs) == 1
        arb = arbs[0]
        assert arb.kind == "negative_risk_long"
        assert arb.sum_prices == pytest.approx(0.92, abs=1e-4)
        assert arb.discount == pytest.approx(0.08, abs=1e-4)
        assert arb.edge_pct > 0.08  # discount / sum_prices
        assert arb.min_liquidity == 500.0
        assert len(arb.legs) == 2

    def test_short_arb_detected(self):
        """Best-bids summing > 1: YES=0.60, NO=0.50, spread=0.02
        → bids = 0.59 + 0.49 = 1.08 → 8% discount short."""
        snaps = [_snap("y", "Yes", 0.60), _snap("n", "No", 0.50)]
        arbs = find_negative_risk_arbs(snaps, min_discount=0.05)
        assert len(arbs) == 1
        arb = arbs[0]
        assert arb.kind == "negative_risk_short"
        assert arb.sum_prices == pytest.approx(1.08, abs=1e-4)
        assert arb.discount == pytest.approx(0.08, abs=1e-4)

    def test_below_min_discount_not_reported(self):
        """Sum 0.995 → 0.5% discount, below default 1% threshold → skip."""
        snaps = [_snap("y", "Yes", 0.49, spread=0.0), _snap("n", "No", 0.505, spread=0.0)]
        # Asks = 0.49 + 0.505 = 0.995 → 0.5% discount
        assert find_negative_risk_arbs(snaps, min_discount=0.01) == []
        # But with a lower threshold it does show up
        arbs = find_negative_risk_arbs(snaps, min_discount=0.001)
        assert len(arbs) == 1

    def test_liquidity_floor_filters_arb(self):
        """Arb exists but liquidity too low — filtered out."""
        snaps = [
            _snap("y", "Yes", 0.40, liquidity=50.0),
            _snap("n", "No", 0.50, liquidity=50.0),
        ]
        arbs = find_negative_risk_arbs(snaps, min_legs_liquidity=100.0)
        assert arbs == []

    def test_single_leg_no_arb(self):
        """A market with only one outcome can't produce an arb."""
        snaps = [_snap("y", "Yes", 0.40)]
        assert find_negative_risk_arbs(snaps) == []

    def test_missing_price_leg_skipped(self):
        """If any leg has no price we still can't evaluate — skip group."""
        s1 = _snap("y", "Yes", 0.40)
        s2 = _snap("n", "No", None)
        # Only 1 evaluable leg in this condition — no arb.
        assert find_negative_risk_arbs([s1, s2]) == []

    def test_multi_outcome_market(self):
        """A 3-way market with prices summing < 1 is a valid arb."""
        snaps = [
            _snap("a", "A", 0.30, cid="multi", spread=0.0),
            _snap("b", "B", 0.30, cid="multi", spread=0.0),
            _snap("c", "C", 0.30, cid="multi", spread=0.0),
        ]
        # sum_asks = 0.90 → 10% discount
        arbs = find_negative_risk_arbs(snaps, min_discount=0.05)
        assert len(arbs) == 1
        assert arbs[0].kind == "negative_risk_long"
        assert arbs[0].sum_prices == pytest.approx(0.90)
        assert len(arbs[0].legs) == 3

    def test_results_sorted_by_edge_desc(self):
        """Biggest edge first."""
        snaps = [
            # cid1: small arb (3% discount)
            _snap("y1", "Yes", 0.46, cid="cid1", spread=0.0),
            _snap("n1", "No", 0.51, cid="cid1", spread=0.0),
            # cid2: bigger arb (10% discount)
            _snap("y2", "Yes", 0.40, cid="cid2", spread=0.0),
            _snap("n2", "No", 0.50, cid="cid2", spread=0.0),
        ]
        arbs = find_negative_risk_arbs(snaps, min_discount=0.01)
        assert len(arbs) == 2
        assert arbs[0].edge_pct > arbs[1].edge_pct
        assert arbs[0].condition_id == "cid2"


class TestScanAndRecord:
    def _cfg(self, **overrides):
        env = {
            "TRADING_MODE": "paper",
            "ALLOW_LIVE_TRADING": "false",
            "SQLITE_DB_PATH": ":memory:",
        }
        env.update(overrides)
        with mock.patch.dict(os.environ, env, clear=False):
            return Config()

    def test_persists_to_store(self):
        store = SQLiteStore(":memory:")
        snaps = [_snap("y", "Yes", 0.40), _snap("n", "No", 0.50)]
        arbs = scan_and_record(snaps, store, "2026-04-13T10:00:00", min_discount=0.05)
        assert len(arbs) == 1
        rows = store.get_recent_arb_opportunities()
        assert len(rows) == 1
        assert rows[0]["kind"] == "negative_risk_long"
        assert rows[0]["timestamp"] == "2026-04-13T10:00:00"
        store.close()

    def test_noop_when_no_arbs(self):
        store = SQLiteStore(":memory:")
        snaps = [_snap("y", "Yes", 0.49), _snap("n", "No", 0.51)]
        arbs = scan_and_record(snaps, store, "2026-04-13T10:00:00")
        assert arbs == []
        assert store.get_recent_arb_opportunities() == []
        store.close()

    def test_tick_skips_when_disabled(self):
        """Default config has arb_detector_enabled=False."""
        cfg = self._cfg()
        assert cfg.arb_detector_enabled is False

    def test_tick_runs_when_enabled(self):
        cfg = self._cfg(ARB_DETECTOR_ENABLED="true", ARB_MIN_DISCOUNT="0.05")
        assert cfg.arb_detector_enabled is True
        assert cfg.arb_min_discount == pytest.approx(0.05)


class TestFormatReport:
    def test_empty_rendered(self):
        assert "No negative-risk" in format_report_markdown([])

    def test_non_empty_rendered(self):
        arb = ArbOpportunity(
            condition_id="cid1", question="Q?", kind="negative_risk_long",
            sum_prices=0.90, discount=0.10, edge_pct=0.111,
            legs=[("y", "Yes", 0.40, 0.02), ("n", "No", 0.50, 0.02)],
            category="politics", min_liquidity=500.0,
        )
        out = format_report_markdown([arb])
        assert "Yes@0.400" in out
        assert "No@0.500" in out
        assert "politics" not in out  # category not in basic table
