"""Tests for the semantic-engine calibration loop."""

from __future__ import annotations

import pytest

from src.analysis.semantic_engine.calibration import (
    CalibrationReport,
    MethodCalibration,
    calibrate,
    calibrate_from_store,
)
from src.analysis.semantic_engine.types import (
    MarketRelation,
    RelationKind,
    SemanticMispricing,
    SyntheticPrice,
)
from src.storage.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signal(method="structural_complement", net_edge=0.05, timestamp="2026-04-01T10:00:00",
            token_id="tok1", side="BUY"):
    return {
        "timestamp": timestamp,
        "token_id": token_id,
        "synthetic_method": method,
        "net_edge": net_edge,
        "side": side,
    }


def _trade(side, price, token_id="tok1", timestamp="2026-04-01T10:01:00",
           strategy="semantic_mispricing"):
    return {
        "side": side,
        "price": price,
        "token_id": token_id,
        "timestamp": timestamp,
        "strategy": strategy,
        "size": 100.0,
    }


# ---------------------------------------------------------------------------
# calibrate() — pure
# ---------------------------------------------------------------------------

class TestCalibrateNoData:
    def test_empty_signals(self):
        r = calibrate([], [])
        assert r.n_signals == 0
        assert r.n_matched == 0
        assert "no_signals_in_window" in r.warnings

    def test_signals_but_no_trades(self):
        r = calibrate([_signal()], [])
        assert r.n_signals == 1
        assert r.n_matched == 0
        assert "no_matched_signals_yet" in r.warnings
        # Method shows up with zero realised
        m = r.per_method[0]
        assert m.n_signals == 1
        assert m.n_matched == 0
        assert m.realisation_ratio is None


class TestCalibrateMatching:
    def test_matches_within_window(self):
        sig = _signal(timestamp="2026-04-01T10:00:00")
        buy = _trade("BUY", 0.50, timestamp="2026-04-01T10:05:00")  # 5 min gap
        sell = _trade("SELL", 0.55, timestamp="2026-04-01T12:00:00")
        r = calibrate([sig], [buy, sell], match_window_hours=1.0)
        assert r.n_matched == 1
        m = r.per_method[0]
        assert m.n_matched == 1
        # realised = (0.55 - 0.50)/0.50 = 0.10 vs detected 0.05 → ratio = 2.0
        assert m.avg_realised_edge == pytest.approx(0.10, abs=1e-6)
        assert m.realisation_ratio == pytest.approx(2.0, abs=1e-3)
        assert m.win_rate == 1.0

    def test_rejects_match_outside_window(self):
        sig = _signal(timestamp="2026-04-01T10:00:00")
        # Buy is 48h after signal — outside default 24h window
        buy = _trade("BUY", 0.50, timestamp="2026-04-03T10:00:00")
        sell = _trade("SELL", 0.55, timestamp="2026-04-03T12:00:00")
        r = calibrate([sig], [buy, sell], match_window_hours=24.0)
        assert r.n_matched == 0

    def test_skips_sell_signals(self):
        """SELL signals are filtered out to avoid double-counting."""
        r = calibrate([_signal(side="SELL")], [])
        # Report acknowledges no matched signals — SELL is skipped for calibration.
        assert r.n_matched == 0
        # per_method entries only come from BUY buckets → empty here
        assert r.per_method == ()


class TestPerMethodBuckets:
    def test_buckets_by_method(self):
        sigs = [
            _signal(method="structural_complement", token_id="a",
                    timestamp="2026-04-01T10:00:00"),
            _signal(method="weighted_avg_equivalent", token_id="b",
                    timestamp="2026-04-01T10:00:00"),
            _signal(method="structural_complement", token_id="c",
                    timestamp="2026-04-01T10:00:00"),
        ]
        r = calibrate(sigs, [])
        methods = {m.method: m for m in r.per_method}
        assert methods["structural_complement"].n_signals == 2
        assert methods["weighted_avg_equivalent"].n_signals == 1

    def test_win_rate_computed(self):
        sigs = [
            _signal(token_id="a", timestamp="2026-04-01T10:00:00"),
            _signal(token_id="b", timestamp="2026-04-01T10:00:00"),
        ]
        trades = [
            # a: profitable
            _trade("BUY", 0.40, token_id="a", timestamp="2026-04-01T10:05:00"),
            _trade("SELL", 0.50, token_id="a", timestamp="2026-04-01T11:00:00"),
            # b: losing
            _trade("BUY", 0.40, token_id="b", timestamp="2026-04-01T10:05:00"),
            _trade("SELL", 0.30, token_id="b", timestamp="2026-04-01T11:00:00"),
        ]
        r = calibrate(sigs, trades)
        assert r.n_matched == 2
        m = r.per_method[0]
        assert m.win_rate == pytest.approx(0.5, abs=1e-6)


class TestAsDictRoundtrip:
    def test_report_serialises(self):
        r = calibrate(
            [_signal(timestamp="2026-04-01T10:00:00")],
            [
                _trade("BUY", 0.40, timestamp="2026-04-01T10:05:00"),
                _trade("SELL", 0.50, timestamp="2026-04-01T11:00:00"),
            ],
        )
        d = r.as_dict()
        assert d["n_signals"] == 1
        assert d["n_matched"] == 1
        assert isinstance(d["per_method"], list)
        assert d["per_method"][0]["method"] == "structural_complement"


# ---------------------------------------------------------------------------
# calibrate_from_store
# ---------------------------------------------------------------------------

class TestCalibrateFromStore:
    def test_empty_store_returns_empty_report(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "bot.db"))
        r = calibrate_from_store(store, window_days=30)
        assert r.n_signals == 0
        store.close()

    def test_reads_from_real_store(self, tmp_path):
        from datetime import datetime
        store = SQLiteStore(str(tmp_path / "bot.db"))
        now = datetime.utcnow().isoformat()

        synth = SyntheticPrice(
            point=0.60, lower=0.59, upper=0.61, confidence=0.9,
            method="structural_complement", contributors=("x",),
            n_contributors=1, is_range_only=False,
        )
        rel = MarketRelation(
            target_token_id="tA", sibling_token_id="x",
            kind=RelationKind.INVERSE_OUTCOME, confidence=0.99, reason="t",
        )
        m = SemanticMispricing(
            token_id="tA", condition_id="cX", question="Q?", category="politics",
            best_bid=0.53, best_ask=0.55, midpoint=0.54, spread=0.02,
            liquidity=5000.0, synthetic=synth, side="BUY",
            gross_edge=0.06, net_edge=0.05, score=0.75,
            relations=(rel,), features={},
        )
        store.insert_semantic_signals(now, [m], mode="shadow")
        # Matching trades
        store.insert_trade(
            order_id="paper-1", token_id="tA", condition_id="cX",
            side="BUY", size=100.0, price=0.50, strategy="semantic_mispricing",
            mode="paper", timestamp=now,
        )
        store.insert_trade(
            order_id="paper-2", token_id="tA", condition_id="cX",
            side="SELL", size=100.0, price=0.55, strategy="semantic_mispricing",
            mode="paper", timestamp=now,
        )
        r = calibrate_from_store(store, window_days=30, match_window_hours=48.0)
        assert r.n_signals == 1
        assert r.n_matched == 1
        store.close()
