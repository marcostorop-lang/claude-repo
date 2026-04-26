"""Shared test setup for the Polymarket bot test suite.

Two purposes:

1. Force ``TRADING_MODE=paper`` and disable any other live-money flags so a
   stray test can never even appear to attempt a live order.

2. Default-disable safety gates whose introduction post-dates the bulk of
   the suite (``REQUIRE_KNOWN_SPREAD_FOR_BUY``).  Tests written before
   the gate did not pass a spread because they were not exercising spread
   logic — flipping the production-safe default on globally would mass-fail
   tests that meant to verify other things.  Tests for the gate itself
   override this within their own ``monkeypatch`` scope.
"""

from __future__ import annotations

import os


def _force_safe_test_env() -> None:
    os.environ.setdefault("TRADING_MODE", "paper")
    os.environ.setdefault("ALLOW_LIVE_TRADING", "false")
    os.environ.setdefault("I_UNDERSTAND_REAL_MONEY", "")
    os.environ.setdefault("REQUIRE_KNOWN_SPREAD_FOR_BUY", "false")


_force_safe_test_env()
