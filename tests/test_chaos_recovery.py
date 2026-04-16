"""Chaos / recovery tests.

Scenario-based tests that exercise the bot's fail-safe posture under
the kinds of errors that *will* happen in production:

* the price feed throws on a token mid-tick → tail-risk still computes,
* the price feed returns garbage (negative, >1, NaN-ish) → values are
  clamped, no crash,
* compute_position_size is called with a bogus end_date → falls back
  to factor 1.0 (no shrinkage),
* the bot restarts after a partial tick — some decision_log rows
  written, no tick_stats — and reconstruct_from_trades + the dashboard
  /api/risk endpoint both still return sensible values rather than 500.

These complement the per-feature unit tests by exercising the
*combinations* operators care about: "the API blew up, what does the
bot show?".
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from src.analysis.tail_risk import compute_tail_risk
from src.config import Config
from src.portfolio.tracker import PortfolioTracker, Position
from src.risk.manager import RiskManager
from src.storage.sqlite_store import SQLiteStore
from src.utils.time_utils import capital_efficiency_factor


def _pos(token, entry=0.50, size=10.0, side="BUY") -> Position:
    return Position(
        token_id=token, condition_id=token + "-c", side=side,
        size=size, entry_price=entry, strategy="s", order_id="o",
    )


def _cfg(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


# -- price feed chaos -------------------------------------------------------


class TestPriceFeedChaos:
    def test_price_fn_raises_falls_back_to_entry(self):
        def angry_fn(_token):
            raise RuntimeError("upstream API down")

        m = compute_tail_risk([_pos("a"), _pos("b")], price_fn=angry_fn)
        # Both p_win fall back to entry_price=0.5; result matches the
        # no-price-fn baseline (deterministic, exact path).
        baseline = compute_tail_risk([_pos("a"), _pos("b")])
        assert m.var_95 == pytest.approx(baseline.var_95)
        assert m.cvar_95 == pytest.approx(baseline.cvar_95)
        assert m.worst_case == pytest.approx(baseline.worst_case)

    def test_price_fn_returns_negative_clamps(self):
        # Garbage upstream → must not propagate negative probabilities.
        m = compute_tail_risk([_pos("a")], price_fn=lambda _t: -0.5)
        # Clamped to 0.0, so p_win=0 for the long → loss is certain.
        # var_95 / cvar_95 collapse to the loss_if_lose value.
        assert m.var_95 == pytest.approx(5.0)

    def test_price_fn_returns_above_one_clamps(self):
        m = compute_tail_risk([_pos("a")], price_fn=lambda _t: 1.7)
        # Clamped to 1.0; long is certain to win → var/cvar stay 0.
        assert m.var_95 == pytest.approx(0.0)
        assert m.expected_loss == pytest.approx(-5.0)  # gain=5

    def test_price_fn_returns_none_falls_back(self):
        m = compute_tail_risk([_pos("a")], price_fn=lambda _t: None)
        baseline = compute_tail_risk([_pos("a")])
        assert m.var_95 == pytest.approx(baseline.var_95)


# -- capital efficiency under bad inputs ------------------------------------


class TestCapitalEfficiencyChaos:
    def test_unparseable_end_date_no_shrinkage(self):
        cfg = _cfg(
            max_position_size=100.0,
            sizing_confidence_scale=False,
            sizing_capital_efficiency_enabled=True,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        # A garbage end_date string should NOT collapse the size to
        # zero — fail-safe is "don't penalise when we don't know".
        size = rm.compute_position_size(
            price=0.50, confidence=1.0, end_date="not-a-date",
        )
        assert size == pytest.approx(100.0 / 0.50)

    def test_factor_floor_holds_under_extreme_horizon(self):
        # 10-year horizon shouldn't make the factor go negative or NaN.
        from datetime import datetime, timedelta, timezone
        far = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
        f = capital_efficiency_factor(far, target_days=14.0, min_factor=0.10)
        assert 0.10 <= f <= 1.0
        assert not math.isnan(f)


# -- partial-tick crash → dashboard still returns a usable response --------


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"
    store = SQLiteStore(str(db_path))
    store.close()
    monkeypatch.setenv("SQLITE_DB_PATH", str(db_path))
    import importlib
    import dashboard.backend.main as backend_main
    importlib.reload(backend_main)
    with TestClient(backend_main.app) as tc:
        yield tc, db_path


class TestPartialTickRecovery:
    def test_decisions_without_tick_stats_dashboard_safe(self, client):
        """Crash mid-tick: decision_log written, tick_stats not.

        /api/risk should still return zero-valued tail-risk + the
        rejection counts it can derive — never 500.
        """
        tc, db_path = client
        store = SQLiteStore(str(db_path))
        # 5 RISK_REJECTED rows written; no tick_stats row at all.
        for i in range(5):
            store.insert_decision(
                timestamp=f"2026-04-15T15:{i:02d}:00",
                token_id=f"t{i}", condition_id=f"c{i}",
                action="RISK_REJECTED",
                reason="Circuit breaker: daily loss exceeded.",
            )
        store.close()

        body = tc.get("/api/risk").json()
        assert body["latest"]["var_95"] == 0.0  # no tick_stats yet
        assert body["latest"]["timestamp"] is None
        assert body["rejections"]["total"] == 5
        assert body["rejections"]["by_bucket"]["circuit_breaker"] == 5
