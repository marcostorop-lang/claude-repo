"""Tests for the position staleness monitor.

Covers:

* age gating (under threshold → not flagged),
* price-range gating (moved > eps → not flagged),
* disabled-by-default (threshold_days=0),
* alert-only vs close action,
* close fallback path without an executor,
* scheduler semantics.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.config import Config
from src.portfolio.staleness import (
    StaleReport,
    detect_stale_positions,
    process_staleness,
    should_run,
)
from src.portfolio.tracker import PortfolioTracker, Position
from src.storage.sqlite_store import SQLiteStore
from src.utils.time_utils import utc_now


# -- Helpers ---------------------------------------------------------------


class _StubClient:
    def __init__(self, price: float | None = None, spread: float = 0.01) -> None:
        self.price = price
        self.spread = spread

    def get_price(self, token_id: str):
        return self.price

    def get_spread(self, token_id: str):
        return self.spread


def _cfg(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


def _aged_position(days_old: float, **kwargs) -> Position:
    entry_ts = (utc_now() - timedelta(days=days_old)).isoformat()
    defaults = dict(
        token_id="tok", condition_id="c", side="BUY",
        size=10.0, entry_price=0.50, strategy="s", order_id="o",
        entry_timestamp=entry_ts,
    )
    defaults.update(kwargs)
    return Position(**defaults)


def _seed_prices(store: SQLiteStore, token_id: str,
                 prices: list[float], days_old_each: list[float]) -> None:
    now = utc_now()
    for p, d in zip(prices, days_old_each):
        ts = (now - timedelta(days=d)).isoformat()
        store.insert_price(token_id, p, ts, spread=0.0)


# -- detect_stale_positions ------------------------------------------------


class TestDetect:
    def test_disabled_by_default(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(_aged_position(30))
        store = SQLiteStore(":memory:")
        # threshold_days = 0 → disabled
        assert detect_stale_positions(portfolio, store, _cfg()) == []
        store.close()

    def test_too_young(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(_aged_position(2))  # 2 days old
        store = SQLiteStore(":memory:")
        cfg = _cfg(position_staleness_days=7)
        assert detect_stale_positions(portfolio, store, cfg) == []
        store.close()

    def test_no_entry_timestamp_not_flagged(self):
        portfolio = PortfolioTracker()
        # Blank timestamp: we don't know how old it is → don't flag.
        p = Position("t", "c", "BUY", 1.0, 0.5, "s", "o", entry_timestamp="")
        portfolio.open_position(p)
        store = SQLiteStore(":memory:")
        cfg = _cfg(position_staleness_days=1)
        assert detect_stale_positions(portfolio, store, cfg) == []
        store.close()

    def test_stale_when_flat(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(_aged_position(10, token_id="t1"))
        store = SQLiteStore(":memory:")
        # Samples all within the 7-day window, all 0.50 ± 0.005 (< eps=0.01)
        _seed_prices(store, "t1",
                     prices=[0.500, 0.502, 0.498, 0.501, 0.500],
                     days_old_each=[6.5, 5, 3, 1, 0])
        cfg = _cfg(position_staleness_days=7,
                   position_staleness_price_epsilon=0.01)
        stale = detect_stale_positions(portfolio, store, cfg)
        assert len(stale) == 1
        assert stale[0].token_id == "t1"
        assert stale[0].samples == 5
        assert stale[0].age_days >= 7
        assert stale[0].price_range <= 0.01
        store.close()

    def test_not_stale_when_moving(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(_aged_position(10, token_id="t1"))
        store = SQLiteStore(":memory:")
        # Moves from 0.40 to 0.60 → range 0.20 >> 0.01.
        _seed_prices(store, "t1",
                     prices=[0.40, 0.50, 0.60],
                     days_old_each=[7, 3, 0])
        cfg = _cfg(position_staleness_days=7)
        assert detect_stale_positions(portfolio, store, cfg) == []
        store.close()

    def test_no_history_counts_as_stale_by_default(self):
        # A position with no recent price rows has range=0.0 by
        # definition.  That IS stale — we haven't seen anything move.
        portfolio = PortfolioTracker()
        portfolio.open_position(_aged_position(10, token_id="t1"))
        store = SQLiteStore(":memory:")
        cfg = _cfg(position_staleness_days=7)
        stale = detect_stale_positions(portfolio, store, cfg)
        assert len(stale) == 1
        assert stale[0].samples == 0
        store.close()


# -- process_staleness -----------------------------------------------------


class TestProcess:
    def test_alert_action_doesnt_close(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(_aged_position(10, token_id="t1"))
        store = SQLiteStore(":memory:")
        _seed_prices(store, "t1", [0.50, 0.50], [5, 0])
        cfg = _cfg(position_staleness_days=7,
                   position_staleness_action="alert")

        report = process_staleness(portfolio, store, _StubClient(0.50), cfg)
        assert len(report.stale) == 1
        assert report.closed == []
        assert "t1" in portfolio.positions  # untouched
        store.close()

    def test_close_action_removes_position_and_books_pnl(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(_aged_position(
            10, token_id="t1", entry_price=0.50, size=10.0,
        ))
        store = SQLiteStore(":memory:")
        _seed_prices(store, "t1", [0.50, 0.50, 0.50], [5, 2, 0])
        cfg = _cfg(position_staleness_days=7,
                   position_staleness_action="close")

        report = process_staleness(
            portfolio, store, _StubClient(price=0.52), cfg,
        )
        assert len(report.stale) == 1
        assert len(report.closed) == 1
        assert "t1" not in portfolio.positions
        # Exit at live midpoint 0.52: (0.52 - 0.50) * 10 = 0.20
        assert portfolio.realised_pnl == pytest.approx(0.20)
        # Trade row booked with staleness mode / reason.
        trades = store.get_all_trades()
        assert trades[-1]["mode"] == "paper_staleness"
        assert trades[-1]["exit_reason"] == "staleness_close"
        store.close()

    def test_close_no_price_leaves_position(self):
        # Without a live price AND no history, we can't close cleanly.
        # The monitor must keep its hands off, not book a fabricated fill.
        portfolio = PortfolioTracker()
        portfolio.open_position(_aged_position(10, token_id="t1"))
        store = SQLiteStore(":memory:")
        cfg = _cfg(position_staleness_days=7,
                   position_staleness_action="close")

        report = process_staleness(
            portfolio, store, _StubClient(price=None), cfg,
        )
        assert len(report.stale) == 1
        assert report.closed == []
        assert "t1" in portfolio.positions
        store.close()

    def test_unknown_action_falls_back_to_alert(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(_aged_position(10, token_id="t1"))
        store = SQLiteStore(":memory:")
        cfg = _cfg(position_staleness_days=7,
                   position_staleness_action="destroy")  # bogus value

        report = process_staleness(portfolio, store, _StubClient(0.5), cfg)
        assert report.closed == []
        assert "t1" in portfolio.positions
        store.close()


# -- Scheduler -------------------------------------------------------------


class TestShouldRun:
    def test_disabled(self):
        assert should_run(0.0, 0.0, now=100.0) is False

    def test_first_run(self):
        assert should_run(0.0, 60.0, now=100.0) is True

    def test_due(self):
        assert should_run(1000.0, 60.0, now=1000.0 + 61 * 60) is True

    def test_not_yet(self):
        assert should_run(1000.0, 60.0, now=1000.0 + 30 * 60) is False
