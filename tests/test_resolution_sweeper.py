"""Tests for the auto-resolution sweeper.

Covers:

* correct settlement-price mapping (token → outcome → payout),
* "closed-but-no-settlement-price" fallback (skip, not mis-book),
* portfolio mutation, trade insert, resolution insert,
* ``should_run`` scheduler semantics,
* multi-leg positions on one condition_id share a single API call,
* deterministic audit when the API returns malformed payloads.

No real network: the :class:`_StubClient` mirrors just the two
methods the sweeper consumes.
"""

from __future__ import annotations

import json

import pytest

from src.config import Config
from src.portfolio.resolution_sweeper import (
    ResolvedPosition,
    should_run,
    sweep_resolved_positions,
    _is_market_resolved,
    _settlement_price_for_token,
)
from src.portfolio.tracker import PortfolioTracker, Position
from src.storage.sqlite_store import SQLiteStore


# -- Helpers ---------------------------------------------------------------


class _StubClient:
    """Minimal stand-in for PolymarketClient."""

    def __init__(self, markets: dict[str, dict | None]) -> None:
        self.markets = markets
        self.calls: list[str] = []
        self.fail_on: set[str] = set()

    def get_market_by_condition_id(self, cid: str) -> dict | None:
        self.calls.append(cid)
        if cid in self.fail_on:
            raise RuntimeError("simulated API outage")
        return self.markets.get(cid)


def _make_cfg(**overrides) -> Config:
    # Config's dataclass + env-driven factories make ad-hoc mutation
    # awkward; we just use defaults here — the sweeper doesn't read
    # any of the opt-in settings.
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


def _resolved_market(yes_price: float, yes_token="tok_yes", no_token="tok_no") -> dict:
    return {
        "question": "Will X happen?",
        "closed": True,
        "umaResolutionStatus": "resolved",
        "clobTokenIds": [yes_token, no_token],
        "outcomes": ["Yes", "No"],
        "outcomePrices": [str(yes_price), str(1.0 - yes_price)],
    }


# -- _is_market_resolved ---------------------------------------------------


class TestIsMarketResolved:
    def test_closed_true(self):
        assert _is_market_resolved({"closed": True}) is True

    def test_resolved_true(self):
        assert _is_market_resolved({"resolved": True}) is True

    def test_uma_resolved(self):
        assert _is_market_resolved({"umaResolutionStatus": "resolved"}) is True

    def test_active_false_alone_not_enough(self):
        # "active=false" can mean "disputed, not yet settled" — don't
        # auto-book a settlement from that signal alone.
        assert _is_market_resolved({"active": False}) is False

    def test_empty(self):
        assert _is_market_resolved({}) is False
        assert _is_market_resolved(None) is False


# -- _settlement_price_for_token ------------------------------------------


class TestSettlementPrice:
    def test_happy_path_yes(self):
        mkt = _resolved_market(1.0)
        assert _settlement_price_for_token(mkt, "tok_yes") == 1.0
        assert _settlement_price_for_token(mkt, "tok_no") == 0.0

    def test_json_string_arrays(self):
        mkt = {
            "clobTokenIds": json.dumps(["a", "b"]),
            "outcomePrices": json.dumps(["0", "1"]),
        }
        assert _settlement_price_for_token(mkt, "a") == 0.0
        assert _settlement_price_for_token(mkt, "b") == 1.0

    def test_unknown_token(self):
        mkt = _resolved_market(1.0)
        assert _settlement_price_for_token(mkt, "not_in_market") is None

    def test_length_mismatch(self):
        mkt = {
            "clobTokenIds": ["a", "b"],
            "outcomePrices": ["1.0"],   # only 1 price for 2 tokens
        }
        assert _settlement_price_for_token(mkt, "a") is None

    def test_clamp_out_of_range(self):
        mkt = {"clobTokenIds": ["a"], "outcomePrices": ["1.2"]}
        assert _settlement_price_for_token(mkt, "a") == 1.0
        mkt = {"clobTokenIds": ["a"], "outcomePrices": ["-0.5"]}
        assert _settlement_price_for_token(mkt, "a") == 0.0

    def test_non_numeric(self):
        mkt = {"clobTokenIds": ["a"], "outcomePrices": ["nope"]}
        assert _settlement_price_for_token(mkt, "a") is None


# -- should_run scheduler ---------------------------------------------------


class TestShouldRun:
    def test_disabled_when_interval_zero(self):
        assert should_run(0.0, 0, now=123.0) is False

    def test_runs_first_time(self):
        assert should_run(0.0, 60, now=100.0) is True

    def test_too_soon(self):
        # 30 minutes after last run, interval=60 → not yet
        assert should_run(1000.0, 60, now=1000.0 + 30 * 60) is False

    def test_due(self):
        assert should_run(1000.0, 60, now=1000.0 + 61 * 60) is True


# -- sweep_resolved_positions ---------------------------------------------


class TestSweep:
    def test_no_positions_is_noop(self):
        portfolio = PortfolioTracker()
        store = SQLiteStore(":memory:")
        client = _StubClient({})
        cfg = _make_cfg()
        report = sweep_resolved_positions(portfolio, client, store, cfg)
        assert report.resolved == []
        assert report.conditions_checked == 0
        assert client.calls == []
        store.close()

    def test_unresolved_market_left_alone(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(Position(
            token_id="tok_yes", condition_id="c1", side="BUY",
            size=10.0, entry_price=0.60, strategy="s", order_id="o1",
        ))
        store = SQLiteStore(":memory:")
        client = _StubClient({"c1": {"closed": False, "active": True}})
        cfg = _make_cfg()

        report = sweep_resolved_positions(portfolio, client, store, cfg)
        assert report.resolved == []
        assert "tok_yes" in portfolio.positions  # still open
        store.close()

    def test_resolved_yes_wins_books_settlement(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(Position(
            token_id="tok_yes", condition_id="c1", side="BUY",
            size=10.0, entry_price=0.60, strategy="s", order_id="o1",
        ))
        store = SQLiteStore(":memory:")
        mkt = _resolved_market(1.0)
        client = _StubClient({"c1": mkt})
        cfg = _make_cfg()

        report = sweep_resolved_positions(portfolio, client, store, cfg)

        # Portfolio: position closed with correct PnL.
        assert "tok_yes" not in portfolio.positions
        assert portfolio.realised_pnl == pytest.approx((1.0 - 0.60) * 10.0)

        # Report reflects the close.
        assert len(report.resolved) == 1
        r: ResolvedPosition = report.resolved[0]
        assert r.settlement_price == 1.0
        assert r.prediction_correct is True
        assert r.side == "BUY"
        assert r.size == 10.0

        # Audit trail — trade row + resolution row both present.
        trades = store.get_all_trades()
        assert len(trades) == 1
        assert trades[0]["side"] == "SELL"
        assert trades[0]["price"] == pytest.approx(1.0)
        assert trades[0]["mode"] == "paper_resolution"
        assert trades[0]["exit_reason"] == "market_resolved"

        resolutions = store.get_resolutions()
        assert len(resolutions) == 1
        assert resolutions[0]["prediction_correct"] == 1
        assert resolutions[0]["resolved_price"] == pytest.approx(1.0)

        store.close()

    def test_resolved_no_wins_counts_as_wrong_for_buy_yes(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(Position(
            token_id="tok_yes", condition_id="c1", side="BUY",
            size=5.0, entry_price=0.60, strategy="s", order_id="o1",
        ))
        store = SQLiteStore(":memory:")
        client = _StubClient({"c1": _resolved_market(0.0)})
        cfg = _make_cfg()

        report = sweep_resolved_positions(portfolio, client, store, cfg)

        assert report.resolved[0].settlement_price == 0.0
        assert report.resolved[0].prediction_correct is False
        assert portfolio.realised_pnl == pytest.approx((0.0 - 0.60) * 5.0)
        store.close()

    def test_multileg_same_condition_one_api_call(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(Position(
            "tok_yes", "c1", "BUY", 10.0, 0.55, "s", "o1",
        ))
        portfolio.open_position(Position(
            "tok_no", "c1", "BUY", 10.0, 0.40, "s", "o2",
        ))
        store = SQLiteStore(":memory:")
        client = _StubClient({"c1": _resolved_market(1.0)})
        cfg = _make_cfg()

        report = sweep_resolved_positions(portfolio, client, store, cfg)

        # One API call for both positions on the same condition.
        assert client.calls == ["c1"]
        # Both positions settled.
        assert len(report.resolved) == 2
        assert portfolio.open_position_count() == 0

        # PnL = YES wins: tok_yes (1-0.55)*10 = 4.5, tok_no (0-0.40)*10 = -4.0
        assert portfolio.realised_pnl == pytest.approx(0.5)
        store.close()

    def test_skip_when_settlement_price_unmappable(self):
        # Market flagged closed but outcomePrices missing — don't book
        # a settlement, surface the skip counter instead.
        portfolio = PortfolioTracker()
        portfolio.open_position(Position(
            "tok_yes", "c1", "BUY", 5.0, 0.50, "s", "o1",
        ))
        store = SQLiteStore(":memory:")
        client = _StubClient({"c1": {"closed": True, "question": "Q?",
                                      "clobTokenIds": ["tok_yes"]}})
        cfg = _make_cfg()

        report = sweep_resolved_positions(portfolio, client, store, cfg)
        assert report.resolved == []
        assert report.skipped_no_settlement == 1
        assert "tok_yes" in portfolio.positions  # still open
        store.close()

    def test_api_error_counted_and_recovered(self):
        portfolio = PortfolioTracker()
        portfolio.open_position(Position(
            "tok_yes", "c1", "BUY", 5.0, 0.50, "s", "o1",
        ))
        portfolio.open_position(Position(
            "tok_b", "c2", "BUY", 5.0, 0.40, "s", "o2",
        ))
        store = SQLiteStore(":memory:")
        client = _StubClient({"c2": _resolved_market(1.0, yes_token="tok_b",
                                                     no_token="tok_b_no")})
        client.fail_on.add("c1")
        cfg = _make_cfg()

        report = sweep_resolved_positions(portfolio, client, store, cfg)
        assert report.api_errors == 1
        # c2 still settled despite c1 failure → sweeper is fault-tolerant.
        assert len(report.resolved) == 1
        assert report.resolved[0].condition_id == "c2"
        store.close()
