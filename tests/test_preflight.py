"""Tests for the preflight checks.

Goal: every check is exercised against both its happy path and a
failure path, so the operator can trust the verdict.  Network and
wallet checks are exercised via injected fakes — no real HTTP.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Config
from src.preflight import (
    CheckResult,
    _check_alerts_wired,
    _check_backup_dir,
    _check_credentials_present,
    _check_db_writable,
    _check_kill_switch_absent,
    _check_polymarket_reachable,
    _check_risk_limits_coherent,
    _check_two_gate_live,
    _check_wallet_balance,
    format_results,
    run_preflight,
)


def _cfg(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


# -- two-gate live -------------------------------------------------------


class TestTwoGateLive:
    def test_paper_default_ok(self):
        r = _check_two_gate_live(_cfg(trading_mode="paper"))
        assert r.status == "OK"
        assert "PAPER" in r.detail

    def test_live_with_no_allow_fails(self):
        r = _check_two_gate_live(_cfg(
            trading_mode="live", allow_live_trading=False,
        ))
        assert r.status == "FAIL"

    def test_live_with_allow_but_wrong_phrase_fails(self):
        r = _check_two_gate_live(_cfg(
            trading_mode="live", allow_live_trading=True,
            i_understand_real_money="WRONG",
        ))
        assert r.status == "FAIL"
        assert "I_UNDERSTAND_REAL_MONEY" in r.detail

    def test_both_gates_set_ok(self):
        r = _check_two_gate_live(_cfg(
            trading_mode="live", allow_live_trading=True,
            i_understand_real_money="YES_TRADE_REAL_FUNDS",
        ))
        assert r.status == "OK"
        assert "WILL send real orders" in r.detail


# -- credentials ---------------------------------------------------------


class TestCredentials:
    def test_paper_skips(self):
        assert _check_credentials_present(_cfg()).status == "OK"

    def test_live_missing_pk_fails(self):
        cfg = _cfg(
            trading_mode="live", allow_live_trading=True,
            i_understand_real_money="YES_TRADE_REAL_FUNDS",
            private_key="", api_key="abc",
        )
        r = _check_credentials_present(cfg)
        assert r.status == "FAIL"
        assert "PRIVATE_KEY" in r.detail

    def test_live_full_creds_ok(self):
        cfg = _cfg(
            trading_mode="live", allow_live_trading=True,
            i_understand_real_money="YES_TRADE_REAL_FUNDS",
            private_key="0xdead", api_key="abc",
        )
        assert _check_credentials_present(cfg).status == "OK"


# -- kill switch ---------------------------------------------------------


class TestKillSwitch:
    def test_absent_ok(self, tmp_path):
        cfg = _cfg(kill_switch_file=str(tmp_path / "missing.flag"))
        assert _check_kill_switch_absent(cfg).status == "OK"

    def test_present_fails(self, tmp_path):
        flag = tmp_path / "KILL_SWITCH"
        flag.write_text("stop")
        cfg = _cfg(kill_switch_file=str(flag))
        r = _check_kill_switch_absent(cfg)
        assert r.status == "FAIL"
        assert "Remove it first" in r.detail


# -- DB writable --------------------------------------------------------


class TestDbWritable:
    def test_writable_path_ok(self, tmp_path):
        cfg = _cfg(sqlite_db_path=str(tmp_path / "bot.db"))
        assert _check_db_writable(cfg).status == "OK"

    def test_missing_parent_fails(self):
        cfg = _cfg(sqlite_db_path="/nonexistent/path/bot.db")
        r = _check_db_writable(cfg)
        assert r.status == "FAIL"
        assert "parent directory" in r.detail


# -- risk limits --------------------------------------------------------


class TestRiskLimits:
    def test_default_coherent(self):
        assert _check_risk_limits_coherent(_cfg()).status == "OK"

    def test_zero_position_fails(self):
        r = _check_risk_limits_coherent(_cfg(max_position_size=0))
        assert r.status == "FAIL"

    def test_position_above_total_warns(self):
        # Both >0, but max_pos > max_total → soft warning, not blocking.
        r = _check_risk_limits_coherent(_cfg(
            max_position_size=200.0, max_total_exposure=100.0,
        ))
        assert r.status == "WARN"
        assert "blow the global cap" in r.detail

    def test_inverted_price_band_warns(self):
        r = _check_risk_limits_coherent(_cfg(min_price=0.9, max_price=0.1))
        assert r.status == "WARN"


# -- alerts -------------------------------------------------------------


class TestAlerts:
    def test_paper_no_sinks_ok(self):
        # Default paper config with no alert env vars set → silent OK.
        assert _check_alerts_wired(_cfg()).status == "OK"

    def test_live_no_sinks_warns(self):
        cfg = _cfg(
            trading_mode="live", allow_live_trading=True,
            i_understand_real_money="YES_TRADE_REAL_FUNDS",
            private_key="0xdead", api_key="abc",
        )
        r = _check_alerts_wired(cfg)
        assert r.status == "WARN"
        assert "silent" in r.detail


# -- backup dir ---------------------------------------------------------


class TestBackupDir:
    def test_unset_ok(self):
        assert _check_backup_dir(_cfg(db_backup_dir="")).status == "OK"

    def test_creates_missing_dir(self, tmp_path):
        path = tmp_path / "newly-created"
        cfg = _cfg(db_backup_dir=str(path))
        r = _check_backup_dir(cfg)
        assert r.status == "OK"
        assert path.exists()


# -- network checks (with injected fakes) ------------------------------


class TestPolymarketReachable:
    def test_returns_ok_with_market(self):
        class _FakeClient:
            def __init__(self, _cfg): pass
            def get_active_markets(self, limit=1):
                return [{"question": "Will X happen?"}]

        r = _check_polymarket_reachable(_cfg(), client_factory=_FakeClient)
        assert r.status == "OK"
        assert "Will X happen" in r.detail

    def test_empty_response_warns(self):
        class _FakeClient:
            def __init__(self, _cfg): pass
            def get_active_markets(self, limit=1):
                return []
        r = _check_polymarket_reachable(_cfg(), client_factory=_FakeClient)
        assert r.status == "WARN"

    def test_exception_fails(self):
        class _FakeClient:
            def __init__(self, _cfg): pass
            def get_active_markets(self, limit=1):
                raise ConnectionError("DNS failure")
        r = _check_polymarket_reachable(_cfg(), client_factory=_FakeClient)
        assert r.status == "FAIL"
        assert "DNS failure" in r.detail


class TestWalletBalance:
    def test_paper_skipped(self):
        r = _check_wallet_balance(_cfg())
        assert r.status == "OK"
        assert "skipped" in r.detail

    def test_live_balance_above_cap_ok(self):
        cfg = _cfg(
            trading_mode="live", allow_live_trading=True,
            i_understand_real_money="YES_TRADE_REAL_FUNDS",
            private_key="0xdead", api_key="abc",
            max_total_exposure=100.0,
        )
        r = _check_wallet_balance(cfg, balance_fetcher=lambda c: 500.0)
        assert r.status == "OK"

    def test_live_balance_below_cap_fails(self):
        cfg = _cfg(
            trading_mode="live", allow_live_trading=True,
            i_understand_real_money="YES_TRADE_REAL_FUNDS",
            private_key="0xdead", api_key="abc",
            max_total_exposure=100.0,
        )
        r = _check_wallet_balance(cfg, balance_fetcher=lambda c: 25.0)
        assert r.status == "FAIL"
        assert "Fund wallet" in r.detail

    def test_live_unreadable_balance_warns(self):
        cfg = _cfg(
            trading_mode="live", allow_live_trading=True,
            i_understand_real_money="YES_TRADE_REAL_FUNDS",
            private_key="0xdead", api_key="abc",
        )
        r = _check_wallet_balance(cfg, balance_fetcher=lambda c: None)
        assert r.status == "WARN"

    def test_live_balance_fetcher_raises_warns(self):
        cfg = _cfg(
            trading_mode="live", allow_live_trading=True,
            i_understand_real_money="YES_TRADE_REAL_FUNDS",
            private_key="0xdead", api_key="abc",
        )

        def _angry(_):
            raise RuntimeError("rpc down")

        r = _check_wallet_balance(cfg, balance_fetcher=_angry)
        assert r.status == "WARN"


# -- orchestrator + formatter -----------------------------------------


class TestOrchestrator:
    def test_run_preflight_skip_network_returns_no_network_checks(self, tmp_path):
        cfg = _cfg(sqlite_db_path=str(tmp_path / "bot.db"))
        results = run_preflight(cfg, skip_network=True)
        names = {r.name for r in results}
        assert "polymarket_api" not in names
        assert "wallet_balance" not in names
        # Default config: every check should be OK or WARN, never FAIL.
        assert all(r.status in ("OK", "WARN") for r in results)

    def test_format_results_blocked_when_fail(self):
        results = [
            CheckResult("foo", "FAIL", "broken"),
            CheckResult("bar", "OK", "fine"),
        ]
        text = format_results(results)
        assert "BLOCKED" in text
        assert "1 OK, 0 WARN, 1 FAIL" in text

    def test_format_results_green_all_ok(self):
        results = [CheckResult("foo", "OK", "fine")]
        assert "GREEN" in format_results(results)

    def test_format_results_caution_warn_only(self):
        results = [CheckResult("foo", "WARN", "soft")]
        assert "CAUTION" in format_results(results)
