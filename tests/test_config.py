"""Tests for src.config."""

import os
from unittest import mock

import pytest

from src.config import Config


def _make_config(**overrides):
    """Create a Config with environment overrides."""
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "POLYMARKET_CLOB_URL": "https://clob.polymarket.com",
        "POLYMARKET_GAMMA_URL": "https://gamma-api.polymarket.com",
        "CHAIN_ID": "137",
        "PRIVATE_KEY": "",
        "POLY_API_KEY": "",
        "POLY_API_SECRET": "",
        "POLY_PASSPHRASE": "",
        "MIN_VOLUME": "1000",
        "MIN_LIQUIDITY": "500",
        "MAX_SPREAD": "0.15",
        "MAX_MARKETS": "20",
        "MAX_POSITION_SIZE": "50",
        "MAX_TOTAL_EXPOSURE": "200",
        "STOP_LOSS_PCT": "0.10",
        "TAKE_PROFIT_PCT": "0.20",
        "MAX_OPEN_POSITIONS": "5",
        "STRATEGY": "simple_momentum",
        "POLL_INTERVAL_SECONDS": "60",
        "LOG_LEVEL": "INFO",
        "LOG_FILE": "",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


class TestConfig:
    def test_paper_mode_by_default(self):
        cfg = _make_config()
        assert cfg.is_paper is True
        assert cfg.is_live is False

    def test_live_mode_requires_flag(self):
        cfg = _make_config(TRADING_MODE="live", ALLOW_LIVE_TRADING="false")
        assert cfg.is_paper is False
        assert cfg.is_live is False

    def test_live_mode_enabled(self):
        cfg = _make_config(TRADING_MODE="live", ALLOW_LIVE_TRADING="true")
        assert cfg.is_live is True

    def test_validate_warns_on_live_without_key(self):
        cfg = _make_config(TRADING_MODE="live", ALLOW_LIVE_TRADING="true", PRIVATE_KEY="")
        problems = cfg.validate()
        assert any("PRIVATE_KEY" in p for p in problems)

    def test_validate_passes_for_paper(self):
        cfg = _make_config()
        assert cfg.validate() == []

    def test_numeric_fields(self):
        cfg = _make_config(MAX_POSITION_SIZE="100", STOP_LOSS_PCT="0.05")
        assert cfg.max_position_size == 100.0
        assert cfg.stop_loss_pct == 0.05
