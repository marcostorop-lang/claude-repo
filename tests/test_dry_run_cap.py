"""Tests for the dry-run live position cap (LIVE_TRADE_MAX_POSITION_USD).

Covers:
* Live mode: cap reduces position size when set > 0.
* Live mode: no effect when cap is 0 (disabled).
* Paper mode: cap has no effect even when set.
* Cap doesn't increase a position that's already smaller.
"""

from __future__ import annotations

import pytest

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.risk.manager import RiskManager


def _make(*, mode="live", allow=True, confirm="YES_TRADE_REAL_FUNDS", cap=5.0, max_pos=100.0):
    cfg = Config()
    object.__setattr__(cfg, "trading_mode", mode)
    object.__setattr__(cfg, "allow_live_trading", allow)
    object.__setattr__(cfg, "i_understand_real_money", confirm)
    object.__setattr__(cfg, "max_position_size", max_pos)
    object.__setattr__(cfg, "live_trade_max_position_usd", cap)
    object.__setattr__(cfg, "sizing_confidence_scale", False)
    object.__setattr__(cfg, "sizing_edge_kelly", False)
    object.__setattr__(cfg, "sizing_kelly_proper", False)
    return RiskManager(cfg, PortfolioTracker())


class TestDryRunCap:
    def test_live_cap_reduces_size(self):
        rm = _make(cap=5.0, max_pos=100.0)
        # price=0.50 → uncapped shares = 100/0.50 = 200
        # capped: $5 / 0.50 = 10 shares
        shares = rm.compute_position_size(price=0.50, confidence=1.0)
        assert shares == pytest.approx(10.0)

    def test_live_cap_zero_disabled(self):
        rm = _make(cap=0.0, max_pos=100.0)
        shares = rm.compute_position_size(price=0.50, confidence=1.0)
        assert shares == pytest.approx(200.0)

    def test_paper_mode_ignores_cap(self):
        rm = _make(mode="paper", allow=False, confirm="", cap=5.0, max_pos=100.0)
        shares = rm.compute_position_size(price=0.50, confidence=1.0)
        # Paper: is_live=False → cap not applied → 200 shares
        assert shares == pytest.approx(200.0)

    def test_cap_does_not_increase(self):
        # max_pos=$2 → base_usd=2, cap=$5 → cap is larger, no change
        rm = _make(cap=5.0, max_pos=2.0)
        shares = rm.compute_position_size(price=0.50, confidence=1.0)
        assert shares == pytest.approx(4.0)  # $2 / 0.50

    def test_cap_with_different_price(self):
        rm = _make(cap=10.0, max_pos=100.0)
        # price=0.25 → uncapped shares = 100/0.25 = 400
        # capped: $10 / 0.25 = 40 shares
        shares = rm.compute_position_size(price=0.25, confidence=1.0)
        assert shares == pytest.approx(40.0)
