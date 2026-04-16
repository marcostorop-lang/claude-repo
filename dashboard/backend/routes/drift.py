"""P&L drift endpoint.

Exposes :func:`src.analysis.pnl_drift.detect_drift` over the recent
window of closed trades from the ``calibration`` table.  Lets the
dashboard show a single number — the realisation ratio — that
operators can watch *before* moving to live capital.

Tolerant of empty / freshly-bootstrapped DBs (returns the cold-start
verdict, never 500).
"""

from __future__ import annotations

from fastapi import APIRouter

from src.analysis.pnl_drift import detect_drift

router = APIRouter(tags=["risk"])


@router.get("/drift")
def get_drift(
    window: int = 100,
    min_samples: int = 30,
    ratio_floor: float = 0.5,
) -> dict:
    from ..main import get_db
    db = get_db()

    n = max(1, min(int(window), 1000))
    rows = db.query(
        "SELECT entry_price, confidence, pnl, features FROM calibration "
        "WHERE pnl IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (n,),
    )

    verdict = detect_drift(
        list(reversed(rows)),  # oldest → newest, doesn't affect verdict
        min_samples=int(min_samples),
        ratio_floor=float(ratio_floor),
    )

    return {
        "n_samples": verdict.n_samples,
        "realised_pnl_usd": verdict.realised_pnl_usd,
        "predicted_pnl_usd": verdict.predicted_pnl_usd,
        "realisation_ratio": verdict.realisation_ratio,
        "realised_win_rate": verdict.realised_win_rate,
        "expected_win_rate": verdict.expected_win_rate,
        "drift_detected": verdict.drift_detected,
        "reason": verdict.reason,
        "window_used": n,
    }
