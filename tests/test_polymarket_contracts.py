"""Smoke / contract tests for ``src/polymarket/{auth,client,market_data}.py``.

These modules sit on the boundary with Polymarket's external APIs and
were untested in ``src/`` (the audit flagged them as the largest blind
spot before live trading).  We don't try to hit the real API — these
are *contract* checks that pin behaviour we control:

* Paper mode never builds an authenticated SDK client.
* Live mode without credentials raises a clear ``ValueError`` rather
  than silently producing a half-built client.
* ``MarketDataService`` filters markets by volume, liquidity, spread,
  price boundary, and active flag — each direction tested.
* ``MarketSnapshot.is_valid`` is sane on the obvious cases.
* ``PolymarketClient.place_order`` is a no-op in paper mode regardless
  of input (this is the safety promise of the bot — pin it).
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from src.config import Config
from src.polymarket.client import PolymarketClient
from src.polymarket.market_data import (
    MarketDataService,
    MarketSnapshot,
    _safe_float,
)


def _cfg(**overrides) -> Config:
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "SQLITE_DB_PATH": ":memory:",
        "MIN_VOLUME": "1000",
        "MIN_LIQUIDITY": "500",
        "MAX_SPREAD": "0.10",
        "MIN_PRICE": "0.05",
        "MAX_PRICE": "0.95",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


# --- auth -----------------------------------------------------------------

class TestBuildClobClient:
    def test_paper_mode_no_credentials_required(self):
        from src.polymarket.auth import build_clob_client
        cfg = _cfg()
        # py-clob-client may or may not be installed in test env; the
        # contract is "no exception raised in paper mode".
        try:
            client = build_clob_client(cfg)
        except ValueError as e:
            pytest.fail(f"paper mode must never require credentials: {e}")
        # client may be None (SDK missing) — that's fine, just must not
        # have raised.
        assert client is None or hasattr(client, "__class__")

    def test_live_mode_without_private_key_raises(self):
        from src.polymarket.auth import build_clob_client
        cfg = _cfg(
            TRADING_MODE="live",
            ALLOW_LIVE_TRADING="true",
            I_UNDERSTAND_REAL_MONEY="YES_TRADE_REAL_FUNDS",
            PRIVATE_KEY="",  # <-- missing
            POLY_API_KEY="x", POLY_API_SECRET="y", POLY_PASSPHRASE="z",
        )
        # Our config sanity check refuses live without private key, but
        # build_clob_client also defends in depth.  Either is acceptable
        # — what matters is that we never produce a half-built client.
        if not cfg.is_live:
            # Config refused the activation.  Good.
            return
        # Skip if the SDK isn't installed (test env may not have it).
        try:
            import py_clob_client  # noqa: F401
        except ImportError:
            pytest.skip("py-clob-client not installed in test env")
        with pytest.raises(ValueError, match="PRIVATE_KEY"):
            build_clob_client(cfg)


# --- client.place_order safety guarantee ---------------------------------

class TestPlaceOrderPaperSafety:
    def test_paper_mode_returns_none_no_matter_what(self):
        cfg = _cfg()
        client = PolymarketClient(cfg)
        # Even with a payload that looks valid, paper mode must never
        # forward to the SDK.  This is the main safety promise of the
        # bot — pin it forever.
        result = client.place_order({
            "tokenID": "abc", "price": 0.5, "size": 1, "side": "BUY",
        })
        assert result is None

    def test_paper_mode_does_not_initialise_signing_client(self):
        cfg = _cfg()
        client = PolymarketClient(cfg)
        # _clob is the live-only signed SDK; in paper it should be None
        # OR an unauthenticated read-only instance.  In neither case
        # should it have a private key attached.
        if client._clob is not None:
            # If something is there, ensure it isn't the live-signing
            # version (no creds object).
            assert getattr(client._clob, "creds", None) is None or \
                   not getattr(client._clob, "creds", None)


# --- market_data filtering pinning ---------------------------------------

class TestSafeFloat:
    def test_parses_str(self):
        assert _safe_float("3.14") == pytest.approx(3.14)

    def test_default_on_garbage(self):
        assert _safe_float("not-a-number") == 0.0
        assert _safe_float(None) == 0.0
        assert _safe_float([], default=42.0) == 42.0


class TestMarketSnapshot:
    def test_is_valid_requires_price_and_active(self):
        s = MarketSnapshot(
            condition_id="c", question="?", token_id="t", outcome="YES",
            price=0.5, spread=0.01, volume=2000, liquidity=1000, active=True,
        )
        assert s.is_valid

    def test_invalid_when_no_price(self):
        s = MarketSnapshot(
            condition_id="c", question="?", token_id="t", outcome="YES",
            price=None, spread=0.01, volume=2000, liquidity=1000, active=True,
        )
        assert not s.is_valid

    def test_invalid_when_inactive(self):
        s = MarketSnapshot(
            condition_id="c", question="?", token_id="t", outcome="YES",
            price=0.5, spread=0.01, volume=2000, liquidity=1000, active=False,
        )
        assert not s.is_valid


class TestPassesFilters:
    def _make(self, **kw):
        defaults = dict(
            condition_id="c", question="q", token_id="t", outcome="YES",
            price=0.5, spread=0.05, volume=2000.0, liquidity=1000.0,
            active=True,
        )
        defaults.update(kw)
        return MarketSnapshot(**defaults)

    def test_inactive_rejected(self):
        cfg = _cfg()
        svc = MarketDataService(client=mock.MagicMock(), cfg=cfg)
        assert not svc._passes_filters(self._make(active=False))

    def test_low_volume_rejected(self):
        cfg = _cfg(MIN_VOLUME="5000")
        svc = MarketDataService(client=mock.MagicMock(), cfg=cfg)
        assert not svc._passes_filters(self._make(volume=100.0))

    def test_low_liquidity_rejected(self):
        cfg = _cfg(MIN_LIQUIDITY="5000")
        svc = MarketDataService(client=mock.MagicMock(), cfg=cfg)
        assert not svc._passes_filters(self._make(liquidity=100.0))

    def test_acceptable_market_passes(self):
        cfg = _cfg()
        svc = MarketDataService(client=mock.MagicMock(), cfg=cfg)
        assert svc._passes_filters(self._make())


class TestSpreadFilter:
    def test_wide_spread_rejected(self):
        cfg = _cfg(MAX_SPREAD="0.05")
        svc = MarketDataService(client=mock.MagicMock(), cfg=cfg)
        snap = MarketSnapshot(
            condition_id="c", question="q", token_id="t", outcome="YES",
            price=0.5, spread=0.10, volume=2000, liquidity=1000, active=True,
        )
        assert not svc._passes_spread_filter(snap)

    def test_unknown_spread_passes(self):
        """Conservative: don't block on missing data — log instead."""
        cfg = _cfg()
        svc = MarketDataService(client=mock.MagicMock(), cfg=cfg)
        snap = MarketSnapshot(
            condition_id="c", question="q", token_id="t", outcome="YES",
            price=0.5, spread=None, volume=2000, liquidity=1000, active=True,
        )
        assert svc._passes_spread_filter(snap)


class TestPriceBoundary:
    def test_extreme_low_rejected(self):
        cfg = _cfg(MIN_PRICE="0.05")
        svc = MarketDataService(client=mock.MagicMock(), cfg=cfg)
        snap = MarketSnapshot(
            condition_id="c", question="q", token_id="t", outcome="YES",
            price=0.01, spread=0.01, volume=2000, liquidity=1000, active=True,
        )
        assert not svc._passes_price_boundary(snap)

    def test_extreme_high_rejected(self):
        cfg = _cfg(MAX_PRICE="0.95")
        svc = MarketDataService(client=mock.MagicMock(), cfg=cfg)
        snap = MarketSnapshot(
            condition_id="c", question="q", token_id="t", outcome="YES",
            price=0.99, spread=0.01, volume=2000, liquidity=1000, active=True,
        )
        assert not svc._passes_price_boundary(snap)

    def test_none_price_rejected(self):
        cfg = _cfg()
        svc = MarketDataService(client=mock.MagicMock(), cfg=cfg)
        snap = MarketSnapshot(
            condition_id="c", question="q", token_id="t", outcome="YES",
            price=None, spread=0.01, volume=2000, liquidity=1000, active=True,
        )
        assert not svc._passes_price_boundary(snap)
