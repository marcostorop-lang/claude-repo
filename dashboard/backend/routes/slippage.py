"""Slippage (predicted vs realised) endpoint.

Reads the last N ``ENTRY_*`` decision rows and returns
per-strategy + overall slippage drift stats.  Complements ``/api/drift``
(which tracks PnL realisation) by isolating the execution-cost
component — a drop in PnL can be a worse model *or* worse fills, and
this endpoint tells the two apart.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.analysis.slippage import compute_slippage_report

router = APIRouter(tags=["risk"])


def _stats_to_dict(s) -> dict:
    return {
        "strategy": s.strategy,
        "n_samples": s.n_samples,
        "predicted_mean_bps": round(s.predicted_mean_bps, 2),
        "realised_mean_bps": round(s.realised_mean_bps, 2),
        "drift_mean_bps": round(s.drift_mean_bps, 2),
        "drift_p50_bps": round(s.drift_p50_bps, 2),
        "drift_p90_bps": round(s.drift_p90_bps, 2),
        "underestimate_rate": round(s.underestimate_rate, 4),
    }


@router.get("/slippage")
def get_slippage(window: int = 500, min_samples: int = 10) -> dict:
    from ..main import get_db
    db = get_db()

    n = max(1, min(int(window), 5000))
    rows = db.query(
        "SELECT action, strategy, features FROM decision_log "
        "WHERE action LIKE 'ENTRY_%' "
        "ORDER BY id DESC LIMIT ?",
        (n,),
    )

    report = compute_slippage_report(
        list(rows), min_samples=int(min_samples),
    )

    return {
        "window_used": n,
        "skipped_rows": report.skipped,
        "overall": _stats_to_dict(report.overall) if report.overall else None,
        "per_strategy": [_stats_to_dict(s) for s in report.per_strategy],
    }
