"""Tests for the opt-in execution-fee model.

The fee model is strictly opt-in (default 0 bps) so these tests confirm:
- Default config → fees always 0.0 (zero behaviour change).
- Enabling TAKER_FEE_BPS → fees accrue to portfolio.fees_paid.
- Fees never mutate realised_pnl; they live in a parallel counter.
- Summary exposes both gross and net-after-fees.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from src.analysis.fees import compute_fee_usd
from src.config import Config
from src.portfolio.tracker import PortfolioTracker, Position


def _cfg(**overrides) -> Config:
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "SQLITE_DB_PATH": ":memory:",
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return Config()


class TestComputeFeeUsd:
    def test_default_config_zero_fee(self):
        cfg = _cfg()
        # Default taker_fee_bps / maker_fee_bps are both 0 → always 0
        assert compute_fee_usd(cfg, 100.0) == 0.0
        assert compute_fee_usd(cfg, 100.0, is_maker=True) == 0.0

    def test_taker_fee_applied(self):
        cfg = _cfg(TAKER_FEE_BPS="20")  # 0.20%
        # 0.20% of $100 = $0.20
        assert compute_fee_usd(cfg, 100.0) == pytest.approx(0.20)

    def test_maker_fee_applied_when_is_maker(self):
        cfg = _cfg(TAKER_FEE_BPS="20", MAKER_FEE_BPS="5")
        assert compute_fee_usd(cfg, 100.0, is_maker=False) == pytest.approx(0.20)
        assert compute_fee_usd(cfg, 100.0, is_maker=True) == pytest.approx(0.05)

    def test_zero_notional_returns_zero(self):
        cfg = _cfg(TAKER_FEE_BPS="20")
        assert compute_fee_usd(cfg, 0.0) == 0.0
        assert compute_fee_usd(cfg, -5.0) == 0.0  # guard against negative

    def test_config_defaults_are_zero(self):
        cfg = _cfg()
        assert cfg.taker_fee_bps == 0.0
        assert cfg.maker_fee_bps == 0.0


class TestPortfolioFees:
    def test_default_fees_paid_is_zero(self):
        pt = PortfolioTracker()
        assert pt.fees_paid == 0.0

    def test_record_fee_accumulates(self):
        pt = PortfolioTracker()
        pt.record_fee(0.20)
        pt.record_fee(0.30)
        assert pt.fees_paid == pytest.approx(0.50)

    def test_record_fee_zero_is_noop(self):
        pt = PortfolioTracker()
        pt.record_fee(0.0)
        pt.record_fee(-1.0)  # negative also ignored
        assert pt.fees_paid == 0.0

    def test_fees_do_not_touch_realised_pnl(self):
        """Critical: gross realised PnL stays clean even with fees enabled."""
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "s", "o1"))
        pt.close_position("t1", 0.55)  # realises 0.50 PnL
        pt.record_fee(0.10)
        # realised_pnl is the gross trading signal — untouched by fees
        assert pt.realised_pnl == pytest.approx(0.50)
        assert pt.fees_paid == pytest.approx(0.10)

    def test_summary_exposes_net_after_fees(self):
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "s", "o1"))
        pt.close_position("t1", 0.60)  # realises 1.0
        pt.record_fee(0.20)
        summary = pt.summary()
        assert summary["realised_pnl"] == pytest.approx(1.0)
        assert summary["fees_paid"] == pytest.approx(0.20)
        assert summary["net_pnl"] == pytest.approx(1.0)  # gross (unchanged meaning)
        assert summary["net_pnl_after_fees"] == pytest.approx(0.80)

    def test_summary_without_fees_still_backward_compatible(self):
        """No fees → old fields keep their historical values."""
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "s", "o1"))
        pt.close_position("t1", 0.55)
        summary = pt.summary()
        # New fields default to zero — nothing downstream breaks
        assert summary["fees_paid"] == 0.0
        assert summary["net_pnl_after_fees"] == pytest.approx(summary["net_pnl"])


class TestMainLoopAccrual:
    """Verify the _accrue_fill_fee helper wired into main.py."""

    def test_accrual_noop_when_fees_disabled(self):
        from src.main import _accrue_fill_fee
        cfg = _cfg()  # default: 0 bps
        pt = PortfolioTracker()
        _accrue_fill_fee(cfg, pt, filled_size=100, fill_price=0.50)
        assert pt.fees_paid == 0.0

    def test_accrual_when_fees_enabled(self):
        from src.main import _accrue_fill_fee
        cfg = _cfg(TAKER_FEE_BPS="20")
        pt = PortfolioTracker()
        _accrue_fill_fee(cfg, pt, filled_size=100, fill_price=0.50)
        # notional = 100 * 0.50 = $50; fee = 0.002 * 50 = $0.10
        assert pt.fees_paid == pytest.approx(0.10)

    def test_accrual_guards_degenerate_inputs(self):
        from src.main import _accrue_fill_fee
        cfg = _cfg(TAKER_FEE_BPS="20")
        pt = PortfolioTracker()
        _accrue_fill_fee(cfg, pt, filled_size=0, fill_price=0.50)
        _accrue_fill_fee(cfg, pt, filled_size=100, fill_price=0)
        assert pt.fees_paid == 0.0


class TestPaperFriction:
    """PAPER_FRICTION_BPS — paper-only execution-cost stress (default 50 bps)."""

    def test_default_paper_friction_is_active(self):
        cfg = _cfg(PAPER_FRICTION_BPS="50")  # default-equivalent
        assert cfg.paper_friction_bps == pytest.approx(50.0)

    def test_friction_accrues_in_paper_mode(self):
        from src.main import _accrue_fill_fee
        cfg = _cfg(PAPER_FRICTION_BPS="50", TAKER_FEE_BPS="0")
        pt = PortfolioTracker()
        # notional = 100 * 0.50 = $50; friction = 50bps * $50 = $0.25
        _accrue_fill_fee(cfg, pt, filled_size=100, fill_price=0.50)
        assert pt.paper_friction_paid == pytest.approx(0.25)
        assert pt.fees_paid == 0.0  # real fees still zero

    def test_friction_disabled_when_zero(self):
        from src.main import _accrue_fill_fee
        cfg = _cfg(PAPER_FRICTION_BPS="0")
        pt = PortfolioTracker()
        _accrue_fill_fee(cfg, pt, filled_size=100, fill_price=0.50)
        assert pt.paper_friction_paid == 0.0

    def test_friction_does_not_apply_in_live_mode(self):
        """Paper friction must never burden live PnL."""
        from src.main import _accrue_fill_fee
        cfg = _cfg(
            TRADING_MODE="live",
            ALLOW_LIVE_TRADING="true",
            I_UNDERSTAND_REAL_MONEY="YES_TRADE_REAL_FUNDS",
            PRIVATE_KEY="0x" + "a" * 64,
            POLY_API_KEY="x", POLY_API_SECRET="y", POLY_PASSPHRASE="z",
            PAPER_FRICTION_BPS="50",
        )
        # Sanity: this config is actually live.
        assert cfg.is_live, "test setup error: cfg should be live"
        pt = PortfolioTracker()
        _accrue_fill_fee(cfg, pt, filled_size=100, fill_price=0.50)
        assert pt.paper_friction_paid == 0.0

    def test_summary_exposes_net_after_costs(self):
        pt = PortfolioTracker()
        pt.open_position(Position("t1", "c1", "BUY", 10, 0.50, "s", "o1"))
        pt.close_position("t1", 0.60)  # realises 1.0
        pt.record_fee(0.20)
        pt.record_paper_friction(0.05)
        summary = pt.summary()
        assert summary["realised_pnl"] == pytest.approx(1.0)
        assert summary["fees_paid"] == pytest.approx(0.20)
        assert summary["paper_friction_paid"] == pytest.approx(0.05)
        assert summary["net_pnl_after_fees"] == pytest.approx(0.80)
        assert summary["net_pnl_after_costs"] == pytest.approx(0.75)
