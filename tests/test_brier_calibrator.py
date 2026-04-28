"""Tests for the Brier-calibrated edge gate.

Pins the contract that:
* The multiplier shrinks for poorly-calibrated cells and grows for
  well-calibrated ones, within configured bounds.
* Cold-start cells (n < min_samples) return 1.0 so a strategy with
  no track record is never punished.
* The cache invalidates correctly.
* Wildcard ('') category falls back when the specific cell is cold.
* Integration with RiskManager: ``compute_position_size`` honours
  the multiplier.
"""

from __future__ import annotations

import json

import pytest

from src.analysis.brier_calibrator import BrierCalibrator
from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import Action, Signal


def _seed(store, *, strategy: str, category: str, samples: list[tuple[float, float]]):
    """Insert ``samples`` of (confidence, pnl) closed-trade rows."""
    for i, (conf, pnl) in enumerate(samples):
        features = json.dumps({"category": category}) if category else "{}"
        cur = store._conn.execute(
            "INSERT INTO calibration "
            "(entry_timestamp, token_id, strategy, confidence, entry_price, features) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
                f"{strategy}-{category}-{i}",
                strategy, float(conf), 0.5, features,
            ),
        )
        rid = cur.lastrowid
        store._conn.execute(
            "UPDATE calibration SET exit_timestamp=?, exit_price=?, exit_reason=?, "
            "                       pnl=?, return_pct=? WHERE id=?",
            ("2026-02-01T00:00:00Z", 0.6, "tp", pnl, pnl, rid),
        )
    store._conn.commit()


# ---------------------------------------------------------------------------
# Cell math
# ---------------------------------------------------------------------------


class TestCellMath:
    def test_perfect_calibration_caps_at_max(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            # Confidence ~ outcome → near-zero Brier.
            _seed(store, strategy="s1", category="politics",
                  samples=[(1.0, 1.0)] * 30 + [(0.0, -1.0)] * 30)
            cal = BrierCalibrator(
                store, min_samples=10, cache_seconds=0,
                slope=4.0, min_multiplier=0.25, max_multiplier=1.5,
            )
            mult = cal.calibration_multiplier("s1", "politics")
            assert mult == pytest.approx(1.5)
        finally:
            store.close()

    def test_perfectly_inverted_floors_at_min(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            # Confidence opposite of outcome → Brier ≈ 1 → multiplier
            # clamped to ``min_multiplier``.
            _seed(store, strategy="s1", category="politics",
                  samples=[(1.0, -1.0)] * 30 + [(0.0, 1.0)] * 30)
            cal = BrierCalibrator(
                store, min_samples=10, cache_seconds=0,
                slope=4.0, min_multiplier=0.25, max_multiplier=1.5,
            )
            mult = cal.calibration_multiplier("s1", "politics")
            assert mult == pytest.approx(0.25)
        finally:
            store.close()

    def test_baseline_returns_about_one(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            # Brier = 0.25 (no skill, predict 0.5 always) → mult = 1.0.
            _seed(store, strategy="s1", category="politics",
                  samples=[(0.5, 1.0), (0.5, -1.0)] * 25)
            cal = BrierCalibrator(
                store, min_samples=10, cache_seconds=0,
            )
            mult = cal.calibration_multiplier("s1", "politics")
            assert mult == pytest.approx(1.0, abs=0.05)
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


class TestColdStart:
    def test_below_min_samples_returns_one(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            _seed(store, strategy="s1", category="politics",
                  samples=[(1.0, -1.0)] * 5)  # well below min_samples
            cal = BrierCalibrator(store, min_samples=20, cache_seconds=0)
            assert cal.calibration_multiplier("s1", "politics") == 1.0
        finally:
            store.close()

    def test_unknown_strategy_returns_one(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            cal = BrierCalibrator(store, min_samples=20, cache_seconds=0)
            assert cal.calibration_multiplier("never-traded", "x") == 1.0
        finally:
            store.close()

    def test_empty_strategy_argument_returns_one(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            cal = BrierCalibrator(store)
            assert cal.calibration_multiplier("", "x") == 1.0
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Wildcard fallback
# ---------------------------------------------------------------------------


class TestWildcardFallback:
    def test_specific_cold_uses_strategy_wildcard(self, tmp_path):
        # Specific (s1, sports) is cold (5 samples), but strategy-level
        # cell (s1, '') has 30 well-calibrated samples → wildcard kicks
        # in and returns the calibrated multiplier.
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            _seed(store, strategy="s1", category="politics",
                  samples=[(1.0, 1.0)] * 30)
            _seed(store, strategy="s1", category="sports",
                  samples=[(0.5, -1.0)] * 5)
            cal = BrierCalibrator(store, min_samples=10, cache_seconds=0)
            # Sports cell is cold so it falls through to the wildcard
            # cell which is the union of all categories — well-calibrated.
            mult_sports = cal.calibration_multiplier("s1", "sports")
            mult_wildcard = cal.calibration_multiplier("s1", "")
            assert mult_sports == mult_wildcard
            assert mult_sports > 1.0
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Cache lifecycle
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_hit_does_not_rerun_query(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            _seed(store, strategy="s1", category="politics",
                  samples=[(1.0, 1.0)] * 30)
            cal = BrierCalibrator(store, min_samples=10, cache_seconds=600)
            cal.calibration_multiplier("s1", "politics")
            # Add data after the first call …
            _seed(store, strategy="s1", category="politics",
                  samples=[(0.5, -1.0)] * 30)
            # … cache is fresh → second call returns the same value.
            cells_before = list(cal._cache.values())
            cal.calibration_multiplier("s1", "politics")
            cells_after = list(cal._cache.values())
            assert cells_before == cells_after
        finally:
            store.close()

    def test_invalidate_forces_recompute(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            _seed(store, strategy="s1", category="politics",
                  samples=[(1.0, 1.0)] * 30)
            cal = BrierCalibrator(store, min_samples=10, cache_seconds=600)
            assert cal.calibration_multiplier("s1", "politics") == pytest.approx(1.5)
            # Add equally-many wrong-direction samples (Brier=1.0) so
            # the union's mean Brier ≈ 0.5 → multiplier collapses to
            # the floor.
            _seed(store, strategy="s1", category="politics",
                  samples=[(1.0, -1.0)] * 30)
            cal.invalidate()
            assert cal.calibration_multiplier("s1", "politics") < 1.0
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Integration with RiskManager.compute_position_size
# ---------------------------------------------------------------------------


class TestRiskIntegration:
    def test_multiplier_shrinks_size(self, tmp_path, monkeypatch):
        # Use a poorly-calibrated cell → multiplier ≈ min_multiplier.
        # MAX_POSITION_SIZE = $50, price = 0.50 → base = 100 shares.
        # With a 0.25 multiplier the result must be ≈ 25 shares.
        monkeypatch.setenv("MAX_POSITION_SIZE", "50")
        # Disable the legacy gates that would otherwise also touch
        # ``base_usd`` so the test isolates the Brier multiplier.
        monkeypatch.setenv("SIZING_CONFIDENCE_SCALE", "false")
        monkeypatch.setenv("SIZING_EDGE_KELLY", "false")
        monkeypatch.setenv("SIZING_KELLY_PROPER", "false")
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            _seed(store, strategy="momentum", category="politics",
                  samples=[(1.0, -1.0)] * 30 + [(0.0, 1.0)] * 30)
            rm = RiskManager(cfg, PortfolioTracker())
            rm.brier_calibrator = BrierCalibrator(
                store, min_samples=10, cache_seconds=0,
                slope=4.0, min_multiplier=0.25, max_multiplier=1.5,
            )
            rm._last_check_category = "politics"
            size = rm.compute_position_size(
                price=0.5, confidence=0.7, strategy="momentum",
            )
            # Without Brier multiplier the size would be ~100 shares.
            # With the floor multiplier (0.25) it lands near 25.
            assert size == pytest.approx(25.0, rel=0.05)
        finally:
            store.close()

    def test_no_calibrator_attached_is_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MAX_POSITION_SIZE", "50")
        monkeypatch.setenv("SIZING_CONFIDENCE_SCALE", "false")
        monkeypatch.setenv("SIZING_EDGE_KELLY", "false")
        monkeypatch.setenv("SIZING_KELLY_PROPER", "false")
        cfg = Config()
        rm = RiskManager(cfg, PortfolioTracker())
        # Calibrator left as None → behaviour byte-for-byte unchanged.
        size = rm.compute_position_size(
            price=0.5, confidence=0.7, strategy="momentum",
        )
        assert size == pytest.approx(100.0, rel=0.01)
