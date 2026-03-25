"""Tests for strategy modules."""

import os
from unittest import mock

import pytest

from src.config import Config
from src.polymarket.market_data import MarketSnapshot
from src.strategy.base import Action
from src.strategy.mean_reversion import MeanReversion
from src.strategy.simple_momentum import SimpleMomentum


def _cfg(**overrides):
    env = {
        "MOMENTUM_WINDOW": "3",
        "MOMENTUM_THRESHOLD": "0.05",
        "MEAN_REVERSION_WINDOW": "5",
        "MEAN_REVERSION_ENTRY_Z": "1.5",
        "MEAN_REVERSION_EXIT_Z": "0.5",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


def _snap(price: float = 0.5) -> MarketSnapshot:
    return MarketSnapshot(
        condition_id="c1",
        question="Will X happen?",
        token_id="t1",
        outcome="Yes",
        price=price,
        spread=0.02,
        volume=5000,
        liquidity=2000,
        active=True,
    )


class TestSimpleMomentum:
    def test_hold_when_not_enough_history(self):
        s = SimpleMomentum(_cfg())
        sig = s.evaluate(_snap(), [0.5])
        assert sig.action == Action.HOLD

    def test_buy_on_upward_momentum(self):
        s = SimpleMomentum(_cfg(MOMENTUM_WINDOW="3", MOMENTUM_THRESHOLD="0.05"))
        history = [0.40, 0.42, 0.50]  # +25% over window
        sig = s.evaluate(_snap(0.50), history)
        assert sig.action == Action.BUY

    def test_sell_on_downward_momentum(self):
        s = SimpleMomentum(_cfg(MOMENTUM_WINDOW="3", MOMENTUM_THRESHOLD="0.05"))
        history = [0.60, 0.55, 0.50]  # -16.7%
        sig = s.evaluate(_snap(0.50), history)
        assert sig.action == Action.SELL

    def test_hold_when_flat(self):
        s = SimpleMomentum(_cfg(MOMENTUM_WINDOW="3", MOMENTUM_THRESHOLD="0.05"))
        history = [0.50, 0.50, 0.50]
        sig = s.evaluate(_snap(0.50), history)
        assert sig.action == Action.HOLD


class TestMeanReversion:
    def test_hold_when_not_enough_history(self):
        s = MeanReversion(_cfg())
        sig = s.evaluate(_snap(), [0.5, 0.5])
        assert sig.action == Action.HOLD

    def test_buy_when_price_below_mean(self):
        s = MeanReversion(_cfg(MEAN_REVERSION_WINDOW="5", MEAN_REVERSION_ENTRY_Z="1.0"))
        # Mean ≈ 0.50, stdev ≈ small, but current price 0.30 is well below
        history = [0.50, 0.51, 0.49, 0.50, 0.50]
        sig = s.evaluate(_snap(0.30), history)
        assert sig.action == Action.BUY

    def test_sell_when_price_above_mean(self):
        s = MeanReversion(_cfg(MEAN_REVERSION_WINDOW="5", MEAN_REVERSION_ENTRY_Z="1.0"))
        history = [0.50, 0.51, 0.49, 0.50, 0.50]
        sig = s.evaluate(_snap(0.70), history)
        assert sig.action == Action.SELL

    def test_hold_when_within_bands(self):
        s = MeanReversion(_cfg(MEAN_REVERSION_WINDOW="5", MEAN_REVERSION_ENTRY_Z="2.0"))
        history = [0.50, 0.51, 0.49, 0.50, 0.50]
        sig = s.evaluate(_snap(0.50), history)
        assert sig.action == Action.HOLD
