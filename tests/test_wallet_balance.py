"""Tests for the live wallet-balance provider + RiskManager integration.

Covers:
* Cached fetch (within ``refresh_seconds`` only one upstream call).
* Force-refresh + invalidate.
* check_can_afford with sufficient / insufficient balance / buffer.
* Fail-safe on unreadable balance (returns ``allowed=True``).
* RiskManager integration: paper mode never builds a provider; live
  mode rejects an order whose cost exceeds the wallet.
"""

from __future__ import annotations

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager
from src.risk.wallet_balance import AffordVerdict, WalletBalanceProvider
from src.strategy.base import Action, Signal


def _cfg(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


# -- pure provider ---------------------------------------------------------


class TestProviderCache:
    def test_caches_within_refresh_window(self):
        calls = {"n": 0}
        def fetch():
            calls["n"] += 1
            return 100.0

        clock = {"t": 0.0}
        p = WalletBalanceProvider(
            fetch, refresh_seconds=60.0, clock=lambda: clock["t"],
        )
        assert p.get_balance() == 100.0
        assert p.get_balance() == 100.0
        clock["t"] = 30.0
        assert p.get_balance() == 100.0
        assert calls["n"] == 1
        # After the window elapses the next call refreshes.
        clock["t"] = 70.0
        assert p.get_balance() == 100.0
        assert calls["n"] == 2

    def test_force_refresh(self):
        calls = {"n": 0}
        def fetch():
            calls["n"] += 1
            return 50.0

        p = WalletBalanceProvider(fetch, refresh_seconds=60.0)
        p.get_balance()
        p.get_balance(force_refresh=True)
        assert calls["n"] == 2

    def test_invalidate(self):
        calls = {"n": 0}
        def fetch():
            calls["n"] += 1
            return 25.0
        p = WalletBalanceProvider(fetch, refresh_seconds=60.0)
        p.get_balance()
        p.invalidate()
        p.get_balance()
        assert calls["n"] == 2

    def test_fetch_exception_keeps_previous_cache(self):
        state = {"raise": False}
        def fetch():
            if state["raise"]:
                raise RuntimeError("rpc down")
            return 200.0

        clock = {"t": 0.0}
        p = WalletBalanceProvider(
            fetch, refresh_seconds=10.0, clock=lambda: clock["t"],
        )
        assert p.get_balance() == 200.0
        state["raise"] = True
        clock["t"] = 100.0
        # Even though refresh raises, we keep the cached 200.0 rather
        # than collapsing to None and gating trades unnecessarily.
        assert p.get_balance() == 200.0


# -- check_can_afford ------------------------------------------------------


class TestCheckCanAfford:
    def test_sufficient_balance_allowed(self):
        p = WalletBalanceProvider(lambda: 500.0, refresh_seconds=60.0)
        v = p.check_can_afford(100.0)
        assert v.allowed is True
        assert v.available_usd == 500.0

    def test_insufficient_balance_rejected(self):
        p = WalletBalanceProvider(lambda: 50.0, refresh_seconds=60.0)
        v = p.check_can_afford(100.0)
        assert v.allowed is False
        assert "Insufficient" in v.reason

    def test_buffer_reduces_usable(self):
        # $100 balance, $30 buffer → only $70 usable.
        p = WalletBalanceProvider(
            lambda: 100.0, refresh_seconds=60.0, min_buffer_usd=30.0,
        )
        assert p.check_can_afford(60.0).allowed is True
        v = p.check_can_afford(80.0)
        assert v.allowed is False
        assert "buffer" in v.reason

    def test_unreadable_balance_failsafe_allows(self):
        p = WalletBalanceProvider(lambda: None, refresh_seconds=60.0)
        v = p.check_can_afford(50.0)
        assert v.allowed is True
        assert v.available_usd is None
        assert "unreadable" in v.reason


# -- RiskManager integration ----------------------------------------------


def _buy_signal() -> Signal:
    return Signal(
        action=Action.BUY, confidence=1.0,
        reason="t", features={},
    )


class TestRiskManagerIntegration:
    def test_paper_mode_no_provider_no_check(self):
        cfg = _cfg(max_position_size=100.0, max_total_exposure=200.0)
        rm = RiskManager(cfg, PortfolioTracker())
        # Even with no provider attached, BUY at $50 cost is allowed.
        v = rm.check("tok1", _buy_signal(), proposed_size=100.0, price=0.5)
        assert v.allowed is True

    def test_live_mode_rejects_when_wallet_too_small(self):
        cfg = _cfg(max_position_size=100.0, max_total_exposure=500.0)
        rm = RiskManager(cfg, PortfolioTracker())
        # Wallet has $20 — $50 trade rejected.
        rm.wallet_balance_provider = WalletBalanceProvider(
            lambda: 20.0, refresh_seconds=60.0,
        )
        v = rm.check("tok1", _buy_signal(), proposed_size=100.0, price=0.5)
        assert v.allowed is False
        assert "Insufficient wallet" in v.reason

    def test_live_mode_allows_when_wallet_sufficient(self):
        cfg = _cfg(max_position_size=100.0, max_total_exposure=500.0)
        rm = RiskManager(cfg, PortfolioTracker())
        rm.wallet_balance_provider = WalletBalanceProvider(
            lambda: 1000.0, refresh_seconds=60.0,
        )
        v = rm.check("tok1", _buy_signal(), proposed_size=100.0, price=0.5)
        assert v.allowed is True

    def test_disabled_flag_skips_check(self):
        cfg = _cfg(
            max_position_size=100.0, max_total_exposure=500.0,
            wallet_balance_check_enabled=False,
        )
        rm = RiskManager(cfg, PortfolioTracker())
        rm.wallet_balance_provider = WalletBalanceProvider(
            lambda: 0.0, refresh_seconds=60.0,
        )
        # Provider says $0 but the flag is off → trade allowed.
        v = rm.check("tok1", _buy_signal(), proposed_size=10.0, price=0.5)
        assert v.allowed is True

    def test_sell_not_blocked_by_wallet(self):
        cfg = _cfg(max_position_size=100.0, max_total_exposure=500.0)
        # Open a position so SELL has something to close.
        from src.portfolio.tracker import Position
        pt = PortfolioTracker()
        pt.open_position(Position(
            token_id="tok1", condition_id="c1", side="BUY",
            size=10.0, entry_price=0.5, strategy="s", order_id="o",
        ))
        rm = RiskManager(cfg, pt)
        rm.wallet_balance_provider = WalletBalanceProvider(
            lambda: 0.0, refresh_seconds=60.0,
        )
        sell = Signal(action=Action.SELL, confidence=1.0,
                      reason="exit", features={})
        v = rm.check("tok1", sell, proposed_size=10.0, price=0.5)
        # SELL exits should never be gated by wallet (we already own
        # the shares; closing them returns funds, not consumes them).
        assert v.allowed is True
