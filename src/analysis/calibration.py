"""
Confidence calibration analysis.

Reads closed entries from the ``calibration`` table and produces a calibration
report: for each confidence bucket, the win-rate and average return.

A well-calibrated strategy has a monotonic relationship between confidence
and realised win-rate. A flat or inverted curve is a red flag — the
confidence signal is not predictive.

This module intentionally has no pandas/numpy dependency so it runs in any
minimal Python environment.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.storage.sqlite_store import SQLiteStore


@dataclass
class CalibrationBucket:
    bucket: str
    n: int
    wins: int
    win_rate: float
    avg_return_pct: float
    avg_confidence: float
    total_pnl: float


def calibration_report(store: SQLiteStore, n_bins: int = 5) -> list[dict]:
    """Return a list of dicts describing calibration buckets.

    Buckets are equal-width on [0, 1]. For each bucket we report the number
    of closed trades that had a confidence in that range, the win rate, the
    average return %, the average confidence, and the total PnL.
    """
    rows = store.get_calibration_closed()
    if not rows:
        return []

    buckets: list[list[dict]] = [[] for _ in range(n_bins)]
    for r in rows:
        conf = r.get("confidence") or 0.0
        idx = min(int(conf * n_bins), n_bins - 1)
        buckets[idx].append(r)

    out: list[dict] = []
    for i, bucket in enumerate(buckets):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        if not bucket:
            out.append({
                "bucket": f"[{lo:.2f}, {hi:.2f})",
                "n": 0,
                "wins": 0,
                "win_rate": 0.0,
                "avg_return_pct": 0.0,
                "avg_confidence": 0.0,
                "total_pnl": 0.0,
            })
            continue
        wins = sum(1 for r in bucket if (r.get("return_pct") or 0) > 0)
        avg_ret = sum(r.get("return_pct") or 0 for r in bucket) / len(bucket)
        avg_conf = sum(r.get("confidence") or 0 for r in bucket) / len(bucket)
        total_pnl = sum(r.get("pnl") or 0 for r in bucket)
        out.append({
            "bucket": f"[{lo:.2f}, {hi:.2f})",
            "n": len(bucket),
            "wins": wins,
            "win_rate": wins / len(bucket),
            "avg_return_pct": avg_ret,
            "avg_confidence": avg_conf,
            "total_pnl": total_pnl,
        })
    return out
