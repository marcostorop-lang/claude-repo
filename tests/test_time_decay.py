"""Tests for time decay factor and temporal utilities."""

from datetime import datetime, timedelta, timezone

import pytest

from src.utils.time_utils import hours_until, time_decay_factor


def _future(hours: float) -> str:
    """Return an ISO datetime string *hours* from now."""
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.isoformat()


class TestTimeDecay:
    def test_empty_end_date_returns_1(self):
        assert time_decay_factor("") == 1.0

    def test_unparseable_returns_1(self):
        assert time_decay_factor("not-a-date") == 1.0

    def test_far_future_returns_1(self):
        assert time_decay_factor(_future(60 * 24)) == 1.0  # 60 days out

    def test_one_week_out_returns_near_1(self):
        f = time_decay_factor(_future(8 * 24))  # 8 days
        assert f == 1.0

    def test_three_days_returns_moderate(self):
        f = time_decay_factor(_future(3 * 24))  # 3 days
        assert 0.7 < f < 1.0

    def test_twelve_hours_returns_half(self):
        f = time_decay_factor(_future(12))
        assert f == pytest.approx(0.5)

    def test_two_hours_returns_low(self):
        f = time_decay_factor(_future(2))
        assert f == pytest.approx(0.2)

    def test_past_date_returns_low(self):
        f = time_decay_factor(_future(-5))  # already resolved
        assert f == pytest.approx(0.2)

    def test_hours_until_returns_positive(self):
        h = hours_until(_future(10))
        assert h is not None
        assert 9.9 < h < 10.1

    def test_hours_until_empty_returns_none(self):
        assert hours_until("") is None

    def test_decay_affects_momentum_confidence(self):
        """Verify that time decay integrates into the strategy."""
        import os
        from unittest import mock
        from src.config import Config
        from src.polymarket.market_data import MarketSnapshot
        from src.strategy.simple_momentum import SimpleMomentum

        env = {"MOMENTUM_WINDOW": "3", "MOMENTUM_THRESHOLD": "0.02", "SQLITE_DB_PATH": ":memory:"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = Config()

        strat = SimpleMomentum(cfg)
        history = [0.40, 0.42, 0.44, 0.50]

        # Far future → high confidence
        snap_far = MarketSnapshot("c", "q", "t", "Y", 0.50, 0.02, 1e6, 1e5, True, end_date=_future(30 * 24))
        sig_far = strat.evaluate(snap_far, history)

        # Near expiry → lower confidence
        snap_near = MarketSnapshot("c", "q", "t", "Y", 0.50, 0.02, 1e6, 1e5, True, end_date=_future(3))
        sig_near = strat.evaluate(snap_near, history)

        assert sig_far.action == sig_near.action  # same signal direction
        assert sig_far.confidence > sig_near.confidence  # but less confident near expiry
        assert sig_near.features.get("time_decay") < 1.0
