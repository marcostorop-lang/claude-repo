"""Tests for the daily summary report.

Covers:
* Default day = UTC yesterday.
* Day-boundary filtering is inclusive on start, exclusive on end.
* Live vs paper trade bucket counts.
* Win rate ignores zero-PnL trades.
* Top-3 winners / losers sorting.
* Anomalies are carried through verbatim.
* Formatter is stable (no tracebacks on empty / degenerate input).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.analysis.daily_summary import (
    build_daily_summary,
    format_summary,
)


def _t(ts: str, side: str = "BUY", mode: str = "paper") -> dict:
    return {"timestamp": ts, "side": side, "mode": mode}


def _c(ts: str, pnl: float, token: str = "tok", strategy: str = "s",
       return_pct: float = 0.0) -> dict:
    return {
        "exit_timestamp": ts, "token_id": token, "strategy": strategy,
        "pnl": pnl, "return_pct": return_pct,
    }


TODAY = date(2026, 4, 20)
YESTERDAY_ISO = "2026-04-19T12:00:00Z"


# -- Day window ----------------------------------------------------------


class TestDayBounds:
    def test_trade_inside_window_counted(self):
        s = build_daily_summary([_t(YESTERDAY_ISO)], [], day=date(2026, 4, 19))
        assert s.trade_count == 1

    def test_trade_before_window_excluded(self):
        s = build_daily_summary(
            [_t("2026-04-18T23:59:59Z")], [], day=date(2026, 4, 19),
        )
        assert s.trade_count == 0

    def test_trade_at_end_boundary_excluded(self):
        # 00:00:00 of the *next* day is outside the window (exclusive end).
        s = build_daily_summary(
            [_t("2026-04-20T00:00:00Z")], [], day=date(2026, 4, 19),
        )
        assert s.trade_count == 0

    def test_trade_at_start_boundary_included(self):
        s = build_daily_summary(
            [_t("2026-04-19T00:00:00Z")], [], day=date(2026, 4, 19),
        )
        assert s.trade_count == 1

    def test_default_day_is_yesterday_utc(self):
        today_utc = datetime.now(timezone.utc).date()
        s = build_daily_summary([], [], day=None)
        assert s.day == (today_utc - timedelta(days=1)).isoformat()

    def test_malformed_timestamp_skipped(self):
        s = build_daily_summary(
            [_t("not-a-date"), _t(YESTERDAY_ISO)],
            [], day=date(2026, 4, 19),
        )
        assert s.trade_count == 1


# -- Counters ------------------------------------------------------------


class TestCounters:
    def test_live_vs_paper_separated(self):
        trades = [
            _t(YESTERDAY_ISO, mode="live"),
            _t(YESTERDAY_ISO, mode="live"),
            _t(YESTERDAY_ISO, mode="paper"),
            _t(YESTERDAY_ISO, mode="paper_resolution"),  # paper-bucket
        ]
        s = build_daily_summary(trades, [], day=date(2026, 4, 19))
        assert s.live_trade_count == 2
        assert s.paper_trade_count == 2
        assert s.trade_count == 4

    def test_entries_and_exits_counted(self):
        trades = [
            _t(YESTERDAY_ISO, side="BUY"),
            _t(YESTERDAY_ISO, side="BUY"),
            _t(YESTERDAY_ISO, side="SELL"),
        ]
        s = build_daily_summary(trades, [], day=date(2026, 4, 19))
        assert s.entries == 2
        assert s.exits == 1


# -- P&L and win rate -----------------------------------------------------


class TestPnL:
    def test_realised_pnl_sums_calibrations(self):
        cals = [_c(YESTERDAY_ISO, 1.5), _c(YESTERDAY_ISO, -0.5)]
        s = build_daily_summary([], cals, day=date(2026, 4, 19))
        assert s.realised_pnl_usd == pytest.approx(1.0)

    def test_win_rate_ignores_zero_pnl(self):
        cals = [
            _c(YESTERDAY_ISO, 1.0),
            _c(YESTERDAY_ISO, -1.0),
            _c(YESTERDAY_ISO, 0.0),  # scratch — doesn't count toward winrate
        ]
        s = build_daily_summary([], cals, day=date(2026, 4, 19))
        assert s.wins == 1
        assert s.losses == 1
        assert s.win_rate == pytest.approx(0.5)

    def test_empty_win_rate_is_zero_not_nan(self):
        s = build_daily_summary([], [], day=date(2026, 4, 19))
        assert s.win_rate == 0.0

    def test_non_numeric_pnl_skipped(self):
        cals = [
            {"exit_timestamp": YESTERDAY_ISO,
             "token_id": "t", "strategy": "s",
             "pnl": None, "return_pct": 0.0},
            _c(YESTERDAY_ISO, 2.0),
        ]
        s = build_daily_summary([], cals, day=date(2026, 4, 19))
        assert s.realised_pnl_usd == pytest.approx(2.0)
        assert s.wins == 1


# -- Winners / losers ----------------------------------------------------


class TestTopMovers:
    def test_top_winners_sorted_desc(self):
        cals = [
            _c(YESTERDAY_ISO, 0.5, token="t1"),
            _c(YESTERDAY_ISO, 5.0, token="t2"),
            _c(YESTERDAY_ISO, 2.0, token="t3"),
        ]
        s = build_daily_summary([], cals, day=date(2026, 4, 19), top_n=2)
        assert [w.token_id for w in s.top_winners] == ["t2", "t3"]

    def test_top_losers_sorted_asc(self):
        cals = [
            _c(YESTERDAY_ISO, -0.5, token="t1"),
            _c(YESTERDAY_ISO, -5.0, token="t2"),
            _c(YESTERDAY_ISO, -2.0, token="t3"),
        ]
        s = build_daily_summary([], cals, day=date(2026, 4, 19), top_n=2)
        assert [l.token_id for l in s.top_losers] == ["t2", "t3"]

    def test_winners_exclude_losers_and_vice_versa(self):
        cals = [
            _c(YESTERDAY_ISO, 1.0, token="w"),
            _c(YESTERDAY_ISO, -1.0, token="l"),
        ]
        s = build_daily_summary([], cals, day=date(2026, 4, 19))
        assert [w.token_id for w in s.top_winners] == ["w"]
        assert [l.token_id for l in s.top_losers] == ["l"]


# -- Anomalies + formatter -----------------------------------------------


class TestAnomaliesAndFormat:
    def test_anomalies_passed_through(self):
        s = build_daily_summary(
            [], [], day=date(2026, 4, 19),
            anomalies=["circuit_breaker", "reconciliation phantom_local"],
        )
        assert s.anomalies == ["circuit_breaker", "reconciliation phantom_local"]

    def test_format_summary_empty_does_not_crash(self):
        s = build_daily_summary([], [], day=date(2026, 4, 19))
        text = format_summary(s)
        assert "2026-04-19" in text
        assert "Trades: 0" in text

    def test_format_summary_shows_sections(self):
        cals = [
            _c(YESTERDAY_ISO, 2.0, token="winner", strategy="s1"),
            _c(YESTERDAY_ISO, -3.0, token="loser", strategy="s2"),
        ]
        s = build_daily_summary(
            [_t(YESTERDAY_ISO)], cals, day=date(2026, 4, 19),
            anomalies=["wallet rejection"],
        )
        text = format_summary(s)
        assert "Top winners:" in text
        assert "Top losers:" in text
        assert "Anomalies:" in text
        assert "wallet rejection" in text


# -- to_dict --------------------------------------------------------------


class TestToDict:
    def test_to_dict_round_trip(self):
        cals = [_c(YESTERDAY_ISO, 1.25, token="tok1", strategy="s")]
        s = build_daily_summary([_t(YESTERDAY_ISO, mode="live")], cals,
                                day=date(2026, 4, 19))
        d = s.to_dict()
        assert d["day"] == "2026-04-19"
        assert d["realised_pnl_usd"] == 1.25
        assert d["live_trade_count"] == 1
        assert d["top_winners"][0]["token_id"] == "tok1"
