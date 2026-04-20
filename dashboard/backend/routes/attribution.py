"""Feature attribution endpoint.

Returns per-feature win/loss statistics from closed calibration rows,
sorted by |Cohen's d| so the most discriminating features appear first.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.analysis.feature_attribution import compute_attribution

router = APIRouter(tags=["analysis"])


def _attr_to_dict(a) -> dict:
    return {
        "feature": a.feature,
        "n_total": a.n_total,
        "n_win": a.n_win,
        "n_loss": a.n_loss,
        "win_mean": round(a.win_mean, 6),
        "loss_mean": round(a.loss_mean, 6),
        "overall_mean": round(a.overall_mean, 6),
        "cohens_d": round(a.cohens_d, 4),
        "winrate_above_median": round(a.winrate_above_median, 4),
        "winrate_below_median": round(a.winrate_below_median, 4),
        "overall_winrate": round(a.overall_winrate, 4),
    }


@router.get("/attribution")
def get_attribution(min_samples: int = 20, min_feature_count: int = 10) -> dict:
    from ..main import get_db
    db = get_db()

    rows = db.query(
        "SELECT pnl, features FROM calibration "
        "WHERE exit_timestamp IS NOT NULL "
        "ORDER BY id ASC",
        (),
    )

    report = compute_attribution(
        list(rows),
        min_samples=int(min_samples),
        min_feature_count=int(min_feature_count),
    )

    return {
        "total_trades": report.total_trades,
        "overall_winrate": round(report.overall_winrate, 4),
        "features": [_attr_to_dict(a) for a in report.features],
    }
