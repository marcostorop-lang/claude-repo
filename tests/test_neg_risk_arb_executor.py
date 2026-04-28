"""Tests for the negative-risk arbitrage executor.

Covers:
* Pre-execution gates: disabled flag, discount floor, concurrency
  cap, notional cap, unsupported kind.
* Happy path: every leg fills within slippage budget; positions are
  booked and ``arb_executions`` row is recorded as ``filled``.
* Failure paths: a leg fails → unwind ; slippage cap breached →
  unwind ; partial-unwound state when not all reverse legs fill.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from src.analysis.arb_detector import ArbOpportunity
from src.config import Config
from src.polymarket.execution import OrderRequest
from src.portfolio.tracker import PortfolioTracker
from src.storage.sqlite_store import SQLiteStore
from src.strategy.neg_risk_arb_executor import (
    NegRiskArbExecutor, record_arb_execution,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _arb(
    discount: float = 0.05,
    legs: int = 3,
    leg_price: float = 0.30,
) -> ArbOpportunity:
    return ArbOpportunity(
        condition_id="cond1",
        question="Q?",
        kind="negative_risk_long",
        sum_prices=leg_price * legs,
        discount=discount,
        edge_pct=discount / (leg_price * legs),
        legs=[
            (f"tok{i}", f"YES_{i}", leg_price, 0.005)
            for i in range(legs)
        ],
        category="politics",
        min_liquidity=500.0,
    )


_RELEVANT_ENV_KEYS = (
    "ARB_EXECUTOR_ENABLED",
    "ARB_MIN_EXECUTABLE_DISCOUNT",
    "ARB_MAX_LEG_SLIPPAGE_PCT",
    "ARB_PER_LEG_MAX_USD",
    "ARB_MAX_CONCURRENT",
    "ARB_MAX_CAPITAL_USD",
)


def _cfg(**env) -> Config:
    """Build a Config with a clean env: clear every ARB_* key first."""
    import os
    for k in _RELEVANT_ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = str(v)
    return Config()


def _ok_executor(fill_at_price: bool = True):
    """Mock ExecutionEngine.execute returning successful fills.

    By default fills at the requested price (no slippage).  Set
    ``fill_at_price=False`` to get a slip ratio applied.
    """
    ex = MagicMock()
    def _execute(order: OrderRequest):
        result = MagicMock()
        result.success = True
        result.order_id = f"oid-{order.token_id}"
        result.filled_size = order.size
        result.fill_price = order.price
        result.message = "ok"
        result.mode = "paper"
        return result
    ex.execute.side_effect = _execute
    return ex


# ---------------------------------------------------------------------------
# Pre-exec gates
# ---------------------------------------------------------------------------


class TestPreExecutionGates:
    def test_disabled_flag_skips(self, tmp_path):
        cfg = _cfg(ARB_EXECUTOR_ENABLED="false")
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            x = NegRiskArbExecutor(cfg, _ok_executor(), PortfolioTracker(), store)
            res = x.consider(_arb())
            assert res.status == "skipped"
            assert "disabled" in res.reason
        finally:
            store.close()

    def test_below_executable_floor_skips(self, tmp_path):
        cfg = _cfg(
            ARB_EXECUTOR_ENABLED="true",
            ARB_MIN_EXECUTABLE_DISCOUNT="0.10",
        )
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            x = NegRiskArbExecutor(cfg, _ok_executor(), PortfolioTracker(), store)
            res = x.consider(_arb(discount=0.05))
            assert res.status == "skipped"
            assert "below execute floor" in res.reason
        finally:
            store.close()

    def test_concurrency_cap_skips(self, tmp_path):
        cfg = _cfg(
            ARB_EXECUTOR_ENABLED="true",
            ARB_MIN_EXECUTABLE_DISCOUNT="0.0",
            ARB_MAX_CONCURRENT="0",
        )
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            x = NegRiskArbExecutor(cfg, _ok_executor(), PortfolioTracker(), store)
            res = x.consider(_arb())
            assert res.status == "skipped"
            assert "max_concurrent" in res.reason
        finally:
            store.close()

    def test_capital_cap_skips(self, tmp_path):
        cfg = _cfg(
            ARB_EXECUTOR_ENABLED="true",
            ARB_MIN_EXECUTABLE_DISCOUNT="0.0",
            ARB_MAX_CONCURRENT="10",
            ARB_PER_LEG_MAX_USD="100",
            ARB_MAX_CAPITAL_USD="50",  # 3 legs × $100 = $300 > $50
        )
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            x = NegRiskArbExecutor(cfg, _ok_executor(), PortfolioTracker(), store)
            res = x.consider(_arb())
            assert res.status == "skipped"
            assert "exceeds cap" in res.reason
        finally:
            store.close()

    def test_unsupported_kind_skips(self, tmp_path):
        cfg = _cfg(
            ARB_EXECUTOR_ENABLED="true",
            ARB_MIN_EXECUTABLE_DISCOUNT="0.0",
        )
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            x = NegRiskArbExecutor(cfg, _ok_executor(), PortfolioTracker(), store)
            res = x.consider(replace(_arb(), kind="negative_risk_short"))
            assert res.status == "skipped"
            assert "kind" in res.reason
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_all_legs_fill_books_positions_and_records(self, tmp_path):
        cfg = _cfg(
            ARB_EXECUTOR_ENABLED="true",
            ARB_MIN_EXECUTABLE_DISCOUNT="0.0",
            ARB_PER_LEG_MAX_USD="10",
            ARB_MAX_CAPITAL_USD="100",
        )
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            pt = PortfolioTracker()
            x = NegRiskArbExecutor(cfg, _ok_executor(), pt, store)
            arb = _arb(legs=3, leg_price=0.30)
            res = x.consider(arb)
            assert res.status == "filled"
            assert res.legs_filled == 3
            assert pt.open_position_count() == 3
            # Per-leg notional = $10; per-leg size = 10 / 0.30
            for pos in pt.positions.values():
                assert pos.strategy == "neg_risk_arb"
            # Concurrency counter advanced.
            assert x._open_arbs == 1
            # Persistence: an arb_executions row landed.
            record_arb_execution(store, arb, res)
            rows = store.get_recent_arb_executions(limit=10)
            assert len(rows) == 1
            assert rows[0]["status"] == "filled"
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_leg_fails_triggers_unwind(self, tmp_path):
        cfg = _cfg(
            ARB_EXECUTOR_ENABLED="true",
            ARB_MIN_EXECUTABLE_DISCOUNT="0.0",
            ARB_PER_LEG_MAX_USD="10",
            ARB_MAX_CAPITAL_USD="100",
        )
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            ex = MagicMock()
            calls = {"i": 0}

            def _execute(order: OrderRequest):
                r = MagicMock()
                r.message = "ok"
                if order.exit_reason == "arb_unwind":
                    r.success = True
                    r.order_id = "u"
                    r.filled_size = order.size
                    r.fill_price = order.price
                    return r
                # Fail the 2nd entry leg.
                calls["i"] += 1
                if calls["i"] == 2:
                    r.success = False
                    r.message = "book empty"
                    r.fill_price = 0
                    r.filled_size = 0
                    return r
                r.success = True
                r.order_id = f"o{calls['i']}"
                r.filled_size = order.size
                r.fill_price = order.price
                return r

            ex.execute.side_effect = _execute
            pt = PortfolioTracker()
            x = NegRiskArbExecutor(cfg, ex, pt, store)
            res = x.consider(_arb(legs=3))
            assert res.status == "partial_unwound"
            assert res.legs_filled == 1  # only the first one stuck
            assert res.legs_unwound == 1
            # Position was reversed.
            assert pt.open_position_count() == 0
            # Concurrency counter NOT advanced — arb did not fill.
            assert x._open_arbs == 0
        finally:
            store.close()

    def test_slippage_cap_triggers_unwind(self, tmp_path):
        cfg = _cfg(
            ARB_EXECUTOR_ENABLED="true",
            ARB_MIN_EXECUTABLE_DISCOUNT="0.0",
            ARB_PER_LEG_MAX_USD="10",
            ARB_MAX_LEG_SLIPPAGE_PCT="0.005",  # 0.5%
        )
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            ex = MagicMock()
            calls = {"i": 0}

            def _execute(order: OrderRequest):
                r = MagicMock()
                r.success = True
                r.order_id = f"o-{order.token_id}"
                r.filled_size = order.size
                r.message = "ok"
                if order.exit_reason == "arb_unwind":
                    r.fill_price = order.price
                    return r
                calls["i"] += 1
                if calls["i"] == 1:
                    # First leg fills at the asked price (no slip).
                    r.fill_price = order.price
                else:
                    # Second leg fills 2% above → blows the 0.5% cap.
                    r.fill_price = order.price * 1.02
                return r

            ex.execute.side_effect = _execute
            pt = PortfolioTracker()
            x = NegRiskArbExecutor(cfg, ex, pt, store)
            res = x.consider(_arb(legs=2))
            assert res.status == "partial_unwound"
            assert "slippage" in res.reason
            assert res.legs_filled == 1
            assert res.legs_unwound == 1
            assert pt.open_position_count() == 0
        finally:
            store.close()


# ---------------------------------------------------------------------------
# record_arb_execution serialisation
# ---------------------------------------------------------------------------


class TestRecordArbExecution:
    def test_legs_detail_round_trips(self, tmp_path):
        cfg = _cfg(
            ARB_EXECUTOR_ENABLED="true",
            ARB_MIN_EXECUTABLE_DISCOUNT="0.0",
            ARB_PER_LEG_MAX_USD="10",
        )
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            x = NegRiskArbExecutor(cfg, _ok_executor(), PortfolioTracker(), store)
            arb = _arb()
            res = x.consider(arb)
            record_arb_execution(store, arb, res)
            row = store.get_recent_arb_executions(limit=1)[0]
            legs = json.loads(row["legs_json"])
            assert len(legs) == 3
            assert all("token_id" in leg for leg in legs)
        finally:
            store.close()
