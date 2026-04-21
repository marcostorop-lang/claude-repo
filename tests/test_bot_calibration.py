"""Tests for the calibration tracker (uses a temp DB path)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.core import calibration


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the calibration DB at an isolated tmp file."""
    test_db = tmp_path / "calib.db"
    monkeypatch.setattr(calibration, "_DB_PATH", test_db)
    calibration._init_db()
    yield test_db


def test_record_and_retrieve(temp_db):
    calibration.record_estimate(
        strategy="prob_arb",
        condition_id="0xA",
        token_id="tokA",
        question="Q1",
        p_claude=0.7,
        p_market=0.6,
        confidence=0.8,
        edge=0.10,
        edge_direction="UNDER",
        reasoning="test",
    )
    rows = calibration.recent_estimates()
    assert len(rows) == 1
    assert rows[0]["p_claude"] == 0.7
    assert rows[0]["outcome"] is None


def test_record_outcome_updates_pending(temp_db):
    calibration.record_estimate(
        strategy="s", condition_id="0xB", token_id="tok",
        question="Q", p_claude=0.3, p_market=0.5,
        confidence=0.7, edge=0.15, edge_direction="OVER",
        reasoning="r",
    )
    n = calibration.record_outcome("0xB", 0)
    assert n == 1

    # Second call finds nothing (already resolved)
    n2 = calibration.record_outcome("0xB", 0)
    assert n2 == 0


def test_compute_metrics_empty(temp_db):
    m = calibration.compute_metrics()
    assert m.n_resolved == 0
    assert m.n_pending == 0
    assert m.brier_score == 0.0


def test_compute_metrics_with_resolved(temp_db):
    # Three well-calibrated, two wrong
    for p, y in [(0.8, 1), (0.7, 1), (0.3, 0), (0.9, 0), (0.2, 1)]:
        calibration.record_estimate(
            strategy="s", condition_id=f"c{p}_{y}", token_id="t",
            question="q", p_claude=p, p_market=p,
            confidence=0.5, edge=0.0, edge_direction="FAIR",
            reasoning="",
        )
        calibration.record_outcome(f"c{p}_{y}", y)

    m = calibration.compute_metrics()
    assert m.n_resolved == 5
    assert 0 < m.brier_score < 1
    assert m.log_loss > 0


def test_invalid_outcome_ignored(temp_db):
    calibration.record_estimate(
        strategy="s", condition_id="0xC", token_id="t",
        question="q", p_claude=0.5, p_market=0.5,
        confidence=0.5, edge=0.0, edge_direction="FAIR",
        reasoning="",
    )
    n = calibration.record_outcome("0xC", 42)  # invalid
    assert n == 0
    m = calibration.compute_metrics()
    assert m.n_resolved == 0


def test_strategy_filter(temp_db):
    calibration.record_estimate(
        strategy="prob_arb", condition_id="cA", token_id="t",
        question="q", p_claude=0.6, p_market=0.5,
        confidence=0.7, edge=0.1, edge_direction="UNDER",
        reasoning="",
    )
    calibration.record_estimate(
        strategy="logical_arb", condition_id="cB", token_id="t",
        question="q", p_claude=0.4, p_market=0.5,
        confidence=0.6, edge=0.05, edge_direction="OVER",
        reasoning="",
    )
    calibration.record_outcome("cA", 1)
    calibration.record_outcome("cB", 0)

    m_all = calibration.compute_metrics()
    m_prob = calibration.compute_metrics(strategy="prob_arb")
    assert m_all.n_resolved == 2
    assert m_prob.n_resolved == 1
