"""Tests for src.utils.math_utils."""

import pytest

from src.utils.math_utils import mean, pct_change, stdev, z_score


class TestMathUtils:
    def test_mean_empty(self):
        assert mean([]) == 0.0

    def test_mean_basic(self):
        assert mean([1, 2, 3]) == pytest.approx(2.0)

    def test_stdev_single(self):
        assert stdev([5.0]) == 0.0

    def test_stdev_basic(self):
        assert stdev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.0, abs=0.01)

    def test_z_score_zero_stdev(self):
        assert z_score(5.0, [5.0, 5.0, 5.0]) == 0.0

    def test_z_score_basic(self):
        vals = [10, 20, 30]
        z = z_score(30, vals)
        assert z > 0

    def test_pct_change_zero(self):
        assert pct_change(0, 5) == 0.0

    def test_pct_change_basic(self):
        assert pct_change(100, 110) == pytest.approx(0.10)
