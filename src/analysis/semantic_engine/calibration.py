"""Calibration loop for the semantic engine.

Takes historical ``semantic_signals`` (detected edge) and the bot's
``trades`` table (realised outcome) and produces a per-method calibration
report. This is the **closed-loop** that answers:

    "When the engine said net_edge = 3%, did we actually make 3% on that
    position, or only 0.5%?"

The answer lets the operator decide:

* Which ``synthetic_method`` is trustworthy (structural_complement
  usually dominates).
* Whether ``SEMANTIC_MIN_NET_EDGE`` should be raised per category because
  the realised edge is consistently < detected.
* Whether the whole engine is ready to promote from ``shadow`` to ``live``.

The module is **read-only**: it never mutates live config. It produces a
report dict that an operator or a cron job can inspect and then choose to
push into ``.env`` manually.  Never auto-apply: changes to live risk
parameters must remain explicit per CLAUDE.md.

Trade matching strategy (deliberately conservative):
  - For each semantic_signal (BUY side), find the next BUY trade on the
    same token within ``match_window_hours`` that used the
    ``semantic_mispricing`` strategy, and the following SELL on the same
    token.  Realised edge ≈ (sell_price − buy_price) / buy_price for BUY
    signals.  Unmatched signals are reported separately (never dropped).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class MethodCalibration:
    """Calibration numbers for one ``synthetic_method``."""

    method: str
    n_signals: int
    n_matched: int
    avg_detected_edge: float
    avg_realised_edge: float
    # Ratio realised/detected. 1.0 means perfectly calibrated; <1 means the
    # engine is over-optimistic, >1 means under-optimistic.  Undefined when
    # detected ~= 0 — we report None in that case.
    realisation_ratio: float | None
    win_rate: float   # fraction of matched signals with realised_edge > 0
    # Suggested next-step threshold (1.2× the historical detected edge that
    # produced break-even realised returns).  None when sample too small.
    suggested_min_net_edge: float | None


@dataclass(frozen=True)
class CalibrationReport:
    """Full calibration report across methods."""

    n_signals: int
    n_matched: int
    window_days: int
    per_method: tuple[MethodCalibration, ...]
    per_side: dict[str, dict[str, float]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_signals": self.n_signals,
            "n_matched": self.n_matched,
            "window_days": self.window_days,
            "per_method": [
                {
                    "method": m.method,
                    "n_signals": m.n_signals,
                    "n_matched": m.n_matched,
                    "avg_detected_edge": round(m.avg_detected_edge, 6),
                    "avg_realised_edge": round(m.avg_realised_edge, 6),
                    "realisation_ratio": (
                        round(m.realisation_ratio, 4)
                        if m.realisation_ratio is not None else None
                    ),
                    "win_rate": round(m.win_rate, 4),
                    "suggested_min_net_edge": (
                        round(m.suggested_min_net_edge, 6)
                        if m.suggested_min_net_edge is not None else None
                    ),
                }
                for m in self.per_method
            ],
            "per_side": self.per_side,
            "warnings": list(self.warnings),
        }


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _pair_buys_with_exits(trades: list[dict]) -> dict[str, list[dict]]:
    """FIFO-pair BUY trades with subsequent SELLs by token_id.

    Returns a mapping ``token_id -> [paired_trade_dict]`` where each entry
    is ``{buy_ts, buy_price, sell_ts, sell_price, realised_edge}``.
    Unclosed BUYs are skipped (they cannot be scored yet).
    """
    by_tok: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: {"buys": [], "sells": []}
    )
    for t in sorted(trades, key=lambda r: r.get("timestamp", "")):
        by_tok[t["token_id"]][f"{t['side'].lower()}s"].append(t)

    out: dict[str, list[dict]] = defaultdict(list)
    for tok, sides in by_tok.items():
        buys = sides["buys"]
        sells = sides["sells"]
        # FIFO pairing
        for i, b in enumerate(buys):
            if i >= len(sells):
                break
            s = sells[i]
            bp = float(b["price"])
            sp = float(s["price"])
            if bp <= 0:
                continue
            realised = (sp - bp) / bp
            out[tok].append({
                "buy_ts": b.get("timestamp", ""),
                "buy_price": bp,
                "sell_ts": s.get("timestamp", ""),
                "sell_price": sp,
                "realised_edge": realised,
            })
    return out


def calibrate(
    signals: list[dict],
    trades: list[dict],
    *,
    window_days: int = 30,
    match_window_hours: float = 24.0,
) -> CalibrationReport:
    """Compute a ``CalibrationReport`` from signal + trade history.

    Parameters
    ----------
    signals
        Rows from ``semantic_signals`` (as returned by
        ``SQLiteStore.get_recent_semantic_signals``).  Only ``BUY`` side
        signals are calibrated — SELL signals on Polymarket are effectively
        a BUY of the complement and would double-count if we included both.
    trades
        Rows from the ``trades`` table, restricted to the
        ``semantic_mispricing`` strategy rows when available.
    window_days
        Lookback window used only for reporting metadata (the filtering of
        signals is expected to happen in the SQL caller).
    match_window_hours
        Maximum time between a signal's ``timestamp`` and the entry trade's
        ``timestamp`` for the two to be considered the same opportunity.
    """
    if not signals:
        return CalibrationReport(
            n_signals=0, n_matched=0, window_days=window_days,
            per_method=(), per_side={},
            warnings=("no_signals_in_window",),
        )

    # Pre-pair trades so we can O(1) look up whether a token had a closed
    # round-trip in the relevant window.
    paired_by_tok = _pair_buys_with_exits(trades)

    per_method_buckets: dict[str, list[dict]] = defaultdict(list)
    per_side_buckets: dict[str, list[dict]] = defaultdict(list)
    matched = 0

    for sig in signals:
        side = sig.get("side")
        if side != "BUY":
            # SELL signals are skipped — they're the mirror of the same
            # event and would double-count.  See module docstring.
            continue
        method = sig.get("synthetic_method") or "unknown"
        tok = sig.get("token_id")
        detected = float(sig.get("net_edge") or 0.0)
        sig_ts = _parse_iso(sig.get("timestamp") or "")

        matched_edge: float | None = None
        if tok and sig_ts and paired_by_tok.get(tok):
            for pair in paired_by_tok[tok]:
                buy_ts = _parse_iso(pair["buy_ts"])
                if buy_ts is None:
                    continue
                delta = abs((buy_ts - sig_ts).total_seconds()) / 3600.0
                if delta <= match_window_hours:
                    matched_edge = pair["realised_edge"]
                    break

        bucket = {
            "detected": detected,
            "realised": matched_edge,
            "matched": matched_edge is not None,
        }
        per_method_buckets[method].append(bucket)
        per_side_buckets[side].append(bucket)
        if matched_edge is not None:
            matched += 1

    warnings: list[str] = []
    if matched == 0:
        warnings.append("no_matched_signals_yet")
    elif matched < 10:
        warnings.append("small_sample_size_below_10")

    per_method: list[MethodCalibration] = []
    for method, rows in per_method_buckets.items():
        n_sig = len(rows)
        matched_rows = [r for r in rows if r["matched"]]
        n_m = len(matched_rows)
        avg_detected = sum(r["detected"] for r in rows) / max(n_sig, 1)
        if n_m:
            avg_realised = sum(r["realised"] for r in matched_rows) / n_m
            ratio = (avg_realised / avg_detected) if abs(avg_detected) > 1e-9 else None
            win_rate = sum(1 for r in matched_rows if r["realised"] > 0) / n_m
            # Suggest min_net_edge that, historically, would have been
            # break-even or better (multiplied by 1.2× safety factor).
            be_detected = [
                r["detected"] for r in matched_rows if r["realised"] >= 0
            ]
            suggested = (
                round(max(be_detected) * 1.2, 6)
                if be_detected and n_m >= 5 else None
            )
        else:
            avg_realised = 0.0
            ratio = None
            win_rate = 0.0
            suggested = None

        per_method.append(MethodCalibration(
            method=method,
            n_signals=n_sig,
            n_matched=n_m,
            avg_detected_edge=avg_detected,
            avg_realised_edge=avg_realised,
            realisation_ratio=ratio,
            win_rate=win_rate,
            suggested_min_net_edge=suggested,
        ))

    # Stable ordering: most-prolific method first
    per_method.sort(key=lambda m: -m.n_signals)

    per_side = {}
    for side, rows in per_side_buckets.items():
        n_m = sum(1 for r in rows if r["matched"])
        n = len(rows)
        avg_detected = sum(r["detected"] for r in rows) / max(n, 1)
        if n_m:
            matched_rows = [r for r in rows if r["matched"]]
            avg_realised = sum(r["realised"] for r in matched_rows) / n_m
            win_rate = sum(1 for r in matched_rows if r["realised"] > 0) / n_m
        else:
            avg_realised = 0.0
            win_rate = 0.0
        per_side[side] = {
            "n_signals": n,
            "n_matched": n_m,
            "avg_detected_edge": round(avg_detected, 6),
            "avg_realised_edge": round(avg_realised, 6),
            "win_rate": round(win_rate, 4),
        }

    return CalibrationReport(
        n_signals=sum(len(v) for v in per_method_buckets.values()),
        n_matched=matched,
        window_days=window_days,
        per_method=tuple(per_method),
        per_side=per_side,
        warnings=tuple(warnings),
    )


def calibrate_from_store(store, *, window_days: int = 30,
                         match_window_hours: float = 24.0,
                         strategy: str = "semantic_mispricing") -> CalibrationReport:
    """Convenience: pull signals + trades from a ``SQLiteStore`` and calibrate.

    Signals are filtered to the last ``window_days`` via a SQL cutoff so
    callers don't have to pre-trim.  Trades are restricted to the given
    strategy — ensuring we don't accidentally calibrate semantic signals
    against momentum-strategy trades.
    """
    cutoff = (datetime.utcnow() - timedelta(days=window_days)).isoformat()
    conn = getattr(store, "_conn", None) or getattr(store, "conn", None)
    if conn is None:
        return CalibrationReport(
            n_signals=0, n_matched=0, window_days=window_days,
            per_method=(), per_side={}, warnings=("store_has_no_connection",),
        )
    try:
        sig_rows = conn.execute(
            "SELECT * FROM semantic_signals WHERE timestamp >= ? "
            "ORDER BY timestamp ASC",
            (cutoff,),
        ).fetchall()
        signals = [dict(r) for r in sig_rows]
    except Exception:
        signals = []
    try:
        tr_rows = conn.execute(
            "SELECT * FROM trades WHERE strategy = ? AND timestamp >= ? "
            "ORDER BY timestamp ASC",
            (strategy, cutoff),
        ).fetchall()
        trades = [dict(r) for r in tr_rows]
    except Exception:
        trades = []
    return calibrate(
        signals, trades,
        window_days=window_days,
        match_window_hours=match_window_hours,
    )
