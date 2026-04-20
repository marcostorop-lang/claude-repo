"""Tests for the position-vs-Polymarket reconciliation sweeper.

Covers:
* Classification: phantom_local, untracked_onchain, size_mismatch.
* Tolerance absorbs small drift (floating-point noise, rounding).
* Fail-safe: fetcher=None and fetch errors never fabricate divergences.
* Alerts: phantom_local → critical, others → warn.
* Scheduler ``should_run`` semantics.
* Report-only contract: portfolio state is never mutated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from src.portfolio.reconciliation import (
    ReconciliationReport,
    _classify,
    reconcile_positions,
    should_run,
)
from src.portfolio.tracker import PortfolioTracker, Position


# -- Stub alert sink -------------------------------------------------------


class _FakeAlerts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def _record(self, level: str):
        def _inner(msg: str, **fields):
            self.calls.append((level, msg, fields))
        return _inner

    def __getattr__(self, name: str):
        # Lazily expose info/warn/critical as callables.
        if name in {"info", "warn", "critical"}:
            return self._record(name)
        raise AttributeError(name)


class _FakeMetrics:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields) -> None:
        self.events.append((event, fields))


# -- Helpers --------------------------------------------------------------


def _pt_with(pos: Optional[Position] = None) -> PortfolioTracker:
    pt = PortfolioTracker()
    if pos is not None:
        pt.open_position(pos)
    return pt


def _pos(token_id: str, size: float, side: str = "BUY") -> Position:
    return Position(
        token_id=token_id, condition_id="c1", side=side,
        size=size, entry_price=0.5, strategy="s", order_id="o",
    )


# -- _classify ------------------------------------------------------------


class TestClassify:
    def test_within_tolerance_is_none(self):
        assert _classify(10.0, 10.001, tolerance=0.01) is None

    def test_phantom_local_when_onchain_zero(self):
        assert _classify(10.0, 0.0, tolerance=0.01) == "phantom_local"

    def test_untracked_onchain_when_tracked_zero(self):
        assert _classify(0.0, 10.0, tolerance=0.01) == "untracked_onchain"

    def test_size_mismatch_when_both_present_but_diff(self):
        assert _classify(10.0, 7.0, tolerance=0.01) == "size_mismatch"
        assert _classify(10.0, 12.0, tolerance=0.01) == "size_mismatch"


# -- reconcile_positions --------------------------------------------------


class TestReconcile:
    def test_no_fetcher_skips_safely(self):
        pt = _pt_with(_pos("t1", 5.0))
        report = reconcile_positions(pt, None)
        assert report.skipped_no_fetcher is True
        assert report.divergences == []

    def test_no_positions_is_empty_success(self):
        pt = PortfolioTracker()
        report = reconcile_positions(pt, lambda t: 0.0)
        assert report.any_divergence is False
        assert report.positions_checked == 0

    def test_match_within_tolerance(self):
        pt = _pt_with(_pos("t1", 10.0))
        report = reconcile_positions(
            pt, lambda t: 10.0 + 1e-6, tolerance_shares=1e-3,
        )
        assert report.any_divergence is False
        assert report.positions_checked == 1

    def test_phantom_local_reported(self):
        pt = _pt_with(_pos("t1", 10.0))
        report = reconcile_positions(pt, lambda t: 0.0)
        assert len(report.divergences) == 1
        d = report.divergences[0]
        assert d.kind == "phantom_local"
        assert d.tracked_size == 10.0
        assert d.onchain_size == 0.0
        assert d.diff == -10.0

    def test_untracked_onchain_reported(self):
        pt = PortfolioTracker()
        report = reconcile_positions(
            pt, lambda t: 7.0,
            extra_token_ids=["t_rogue"],
        )
        assert len(report.divergences) == 1
        d = report.divergences[0]
        assert d.kind == "untracked_onchain"
        assert d.token_id == "t_rogue"
        assert d.tracked_size == 0.0
        assert d.onchain_size == 7.0

    def test_size_mismatch_reported(self):
        pt = _pt_with(_pos("t1", 10.0))
        report = reconcile_positions(pt, lambda t: 7.0)
        d = report.divergences[0]
        assert d.kind == "size_mismatch"
        assert d.diff == pytest.approx(-3.0)

    def test_fetcher_exception_counts_error_not_divergence(self):
        pt = _pt_with(_pos("t1", 10.0))
        def _boom(t):
            raise RuntimeError("rpc down")
        report = reconcile_positions(pt, _boom)
        assert report.fetch_errors == 1
        assert report.divergences == []

    def test_fetcher_returns_none_counts_error(self):
        pt = _pt_with(_pos("t1", 10.0))
        report = reconcile_positions(pt, lambda t: None)
        assert report.fetch_errors == 1
        assert report.divergences == []

    def test_portfolio_state_is_never_mutated(self):
        pt = _pt_with(_pos("t1", 10.0))
        before = dict(pt.positions)
        before_size = before["t1"].size
        reconcile_positions(pt, lambda t: 0.0)
        # Tracker still shows the phantom — report-only contract.
        assert pt.positions["t1"].size == before_size
        assert "t1" in pt.positions

    def test_phantom_local_alerts_as_critical(self):
        pt = _pt_with(_pos("t1", 10.0))
        alerts = _FakeAlerts()
        reconcile_positions(pt, lambda t: 0.0, alerts=alerts)
        levels = [lvl for lvl, _m, _f in alerts.calls]
        assert "critical" in levels
        assert "warn" not in levels

    def test_size_mismatch_alerts_as_warn(self):
        pt = _pt_with(_pos("t1", 10.0))
        alerts = _FakeAlerts()
        reconcile_positions(pt, lambda t: 3.0, alerts=alerts)
        levels = [lvl for lvl, _m, _f in alerts.calls]
        assert levels == ["warn"]

    def test_untracked_onchain_alerts_as_warn(self):
        pt = PortfolioTracker()
        alerts = _FakeAlerts()
        reconcile_positions(
            pt, lambda t: 5.0,
            extra_token_ids=["t1"], alerts=alerts,
        )
        levels = [lvl for lvl, _m, _f in alerts.calls]
        assert levels == ["warn"]

    def test_metrics_emitted_when_enabled(self):
        pt = _pt_with(_pos("t1", 10.0))
        metrics = _FakeMetrics(enabled=True)
        reconcile_positions(pt, lambda t: 0.0, metrics=metrics)
        assert any(
            ev == "portfolio_reconciliation_divergence"
            for ev, _f in metrics.events
        )

    def test_metrics_silent_when_disabled(self):
        pt = _pt_with(_pos("t1", 10.0))
        metrics = _FakeMetrics(enabled=False)
        reconcile_positions(pt, lambda t: 0.0, metrics=metrics)
        assert metrics.events == []


# -- should_run ------------------------------------------------------------


class TestShouldRun:
    def test_disabled_when_interval_zero_or_negative(self):
        assert should_run(0.0, 0.0, 1000.0) is False
        assert should_run(0.0, -5.0, 1000.0) is False

    def test_runs_immediately_when_never_run(self):
        assert should_run(0.0, 60.0, 1000.0) is True

    def test_waits_for_interval(self):
        # interval_minutes=1 → 60 s between runs.
        assert should_run(1000.0, 1.0, 1030.0) is False
        assert should_run(1000.0, 1.0, 1059.9) is False
        assert should_run(1000.0, 1.0, 1060.0) is True
        assert should_run(1000.0, 1.0, 5000.0) is True
