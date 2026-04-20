"""Tests for the latency telemetry module.

Covers:
* Single and multi-sample observation.
* Rolling percentile computation (p50/p90/p95/p99/max).
* Three-interval breakdown (signal→submit, submit→fill, signal→fill).
* Invalid / zero timestamp handling.
* ``should_alert_latency`` threshold logic.
* Window cap (deque maxlen).
"""

from __future__ import annotations

import pytest

from src.utils.latency import (
    LatencyStats,
    LatencyTracker,
    _percentile,
    _summarise,
    should_alert_latency,
)


# -- LatencyTracker.observe ---------------------------------------------------


class TestObserve:
    def test_single_observation(self):
        t = LatencyTracker()
        t.observe(t_signal=1000.0, t_submit=1000.1, t_fill=1000.3)
        assert len(t._signal_to_submit) == 1
        assert len(t._submit_to_fill) == 1
        assert len(t._signal_to_fill) == 1
        assert t._signal_to_submit[0] == pytest.approx(100.0)
        assert t._submit_to_fill[0] == pytest.approx(200.0)
        assert t._signal_to_fill[0] == pytest.approx(300.0)

    def test_multiple_observations(self):
        t = LatencyTracker()
        t.observe(t_signal=1.0, t_submit=1.05, t_fill=1.10)
        t.observe(t_signal=2.0, t_submit=2.10, t_fill=2.30)
        assert len(t._signal_to_fill) == 2
        assert t._signal_to_fill[0] == pytest.approx(100.0)
        assert t._signal_to_fill[1] == pytest.approx(300.0)

    def test_zero_signal_skipped(self):
        t = LatencyTracker()
        t.observe(t_signal=0.0, t_submit=1.0, t_fill=2.0)
        assert len(t._signal_to_submit) == 0
        assert len(t._signal_to_fill) == 0
        # submit→fill still records because t_submit > 0
        assert len(t._submit_to_fill) == 1

    def test_zero_submit_skipped(self):
        t = LatencyTracker()
        t.observe(t_signal=1.0, t_submit=0.0, t_fill=2.0)
        assert len(t._submit_to_fill) == 0
        # signal→submit skipped (t_submit < t_signal)
        assert len(t._signal_to_submit) == 0
        # signal→fill still records
        assert len(t._signal_to_fill) == 1

    def test_backward_timestamps_rejected(self):
        t = LatencyTracker()
        # submit before signal → signal_to_submit skipped
        t.observe(t_signal=5.0, t_submit=4.0, t_fill=6.0)
        assert len(t._signal_to_submit) == 0

        t2 = LatencyTracker()
        # fill before submit → submit_to_fill skipped
        t2.observe(t_signal=5.0, t_submit=5.5, t_fill=5.3)
        assert len(t2._submit_to_fill) == 0

    def test_window_cap(self):
        t = LatencyTracker(window=15)
        for i in range(20):
            t.observe(t_signal=float(i), t_submit=float(i) + 0.01, t_fill=float(i) + 0.02)
        # Window is max(10, 15) = 15 but we pushed 20, first 5 should be gone
        # Actually first observation has t_signal=0.0 which is skipped
        # signal_to_fill only records when t_signal > 0, so i=1..19 = 19 observations, capped at 15
        assert len(t._signal_to_fill) == 15

    def test_minimum_window_10(self):
        t = LatencyTracker(window=3)
        assert t._window == 10


# -- stats() ------------------------------------------------------------------


class TestStats:
    def test_empty_stats(self):
        t = LatencyTracker()
        s = t.stats()
        assert s["signal_to_fill"].n_samples == 0
        assert s["signal_to_fill"].p50_ms == 0.0

    def test_stats_single_sample(self):
        t = LatencyTracker()
        t.observe(t_signal=1.0, t_submit=1.1, t_fill=1.3)
        s = t.stats()
        stf = s["signal_to_fill"]
        assert stf.n_samples == 1
        assert stf.p50_ms == pytest.approx(300.0)
        assert stf.max_ms == pytest.approx(300.0)

    def test_stats_multiple_samples(self):
        t = LatencyTracker()
        for i in range(100):
            delay = (i + 1) * 0.001  # 1ms to 100ms
            t.observe(t_signal=float(i), t_submit=float(i) + delay / 2, t_fill=float(i) + delay)
        s = t.stats()
        stf = s["signal_to_fill"]
        # i=0 skipped (t_signal=0), so 99 samples from i=1..99
        assert stf.n_samples == 99
        assert stf.p50_ms > 0
        assert stf.p99_ms > stf.p50_ms

    def test_all_three_intervals_present(self):
        t = LatencyTracker()
        t.observe(t_signal=1.0, t_submit=1.05, t_fill=1.20)
        s = t.stats()
        assert "signal_to_submit" in s
        assert "submit_to_fill" in s
        assert "signal_to_fill" in s
        assert s["signal_to_submit"].p50_ms == pytest.approx(50.0)
        assert s["submit_to_fill"].p50_ms == pytest.approx(150.0)
        assert s["signal_to_fill"].p50_ms == pytest.approx(200.0)


# -- latest_signal_to_fill_ms ------------------------------------------------


class TestLatest:
    def test_empty_returns_none(self):
        t = LatencyTracker()
        assert t.latest_signal_to_fill_ms() is None

    def test_returns_most_recent(self):
        t = LatencyTracker()
        t.observe(t_signal=1.0, t_submit=1.1, t_fill=1.2)
        t.observe(t_signal=2.0, t_submit=2.1, t_fill=2.5)
        assert t.latest_signal_to_fill_ms() == pytest.approx(500.0)


# -- _percentile helper -------------------------------------------------------


class TestPercentile:
    def test_empty_list(self):
        assert _percentile([], 50.0) == 0.0

    def test_single_element(self):
        assert _percentile([42.0], 50.0) == 42.0
        assert _percentile([42.0], 99.0) == 42.0

    def test_two_elements_median(self):
        result = _percentile([10.0, 20.0], 50.0)
        assert result == pytest.approx(15.0)

    def test_p0_returns_first(self):
        assert _percentile([1.0, 2.0, 3.0], 0.0) == pytest.approx(1.0)

    def test_p100_returns_last(self):
        assert _percentile([1.0, 2.0, 3.0], 100.0) == pytest.approx(3.0)


# -- _summarise helper --------------------------------------------------------


class TestSummarise:
    def test_empty(self):
        s = _summarise([])
        assert s.n_samples == 0
        assert s.max_ms == 0.0

    def test_single_value(self):
        s = _summarise([100.0])
        assert s.n_samples == 1
        assert s.p50_ms == 100.0
        assert s.max_ms == 100.0

    def test_ten_values(self):
        vals = [float(x) for x in range(1, 11)]  # 1..10
        s = _summarise(vals)
        assert s.n_samples == 10
        assert s.max_ms == 10.0
        assert s.p50_ms == pytest.approx(5.5)


# -- should_alert_latency -----------------------------------------------------


class TestAlertThreshold:
    def test_none_latency_no_alert(self):
        assert should_alert_latency(None, threshold_ms=100.0) is False

    def test_zero_threshold_disabled(self):
        assert should_alert_latency(5000.0, threshold_ms=0.0) is False

    def test_negative_threshold_disabled(self):
        assert should_alert_latency(5000.0, threshold_ms=-1.0) is False

    def test_below_threshold_no_alert(self):
        assert should_alert_latency(99.9, threshold_ms=100.0) is False

    def test_at_threshold_alerts(self):
        assert should_alert_latency(100.0, threshold_ms=100.0) is True

    def test_above_threshold_alerts(self):
        assert should_alert_latency(150.0, threshold_ms=100.0) is True
