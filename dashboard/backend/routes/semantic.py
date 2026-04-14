"""Semantic engine observability endpoints.

Exposes the ``semantic_signals`` table (populated by the mispricing engine
in shadow / live mode) plus a small summary by synthetic method so an
operator can quickly audit signal quality before trusting live activation.

This is purely read-only: the dashboard never modifies the bot's state,
and if the table is missing (older DB, engine never enabled) the endpoint
returns an empty payload instead of erroring — keeping the UI resilient.
"""

from __future__ import annotations

import json
from collections import defaultdict

from fastapi import APIRouter, Query

from src.analysis.semantic_engine.calibration import calibrate

router = APIRouter(tags=["semantic"])


def _has_table(db) -> bool:
    """Return True iff ``semantic_signals`` exists in this DB."""
    row = db.query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='semantic_signals'"
    )
    return row is not None


def _parse(row: dict) -> dict:
    """Decode JSON blobs and normalise types for the front-end."""
    out = dict(row)
    for key in ("relations_json", "features_json"):
        raw = out.get(key)
        if raw:
            try:
                out[key.replace("_json", "")] = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                out[key.replace("_json", "")] = None
        out.pop(key, None)
    return out


@router.get("/semantic/signals")
def get_signals(
    limit: int = Query(50, ge=1, le=500),
    mode: str | None = Query(None, pattern="^(shadow|live)$"),
):
    """Most recent semantic detections, optionally filtered by mode.

    Returns an empty list when the table doesn't exist so the dashboard
    tile renders gracefully on a fresh DB.
    """
    from ..main import get_db
    db = get_db()
    if not _has_table(db):
        return {"signals": [], "table_exists": False}

    params: list = []
    where = ""
    if mode:
        where = "WHERE mode = ?"
        params.append(mode)
    sql = (
        "SELECT id, timestamp, token_id, condition_id, question, category, "
        "side, best_bid, best_ask, midpoint, spread, liquidity, "
        "synthetic_fair, synthetic_lower, synthetic_upper, synthetic_method, "
        "synthetic_confidence, synthetic_contributors_n, gross_edge, net_edge, score, "
        "relations_json, features_json, mode "
        f"FROM semantic_signals {where} ORDER BY id DESC LIMIT ?"
    )
    params.append(limit)
    rows = db.query(sql, tuple(params))
    return {"signals": [_parse(r) for r in rows], "table_exists": True}


@router.get("/semantic/summary")
def get_summary(
    days: int = Query(7, ge=1, le=90),
):
    """Aggregate KPIs over the lookback window.

    Groups by ``synthetic_method`` so the operator can tell at a glance
    whether signals are dominated by solid structural sources or the
    flakier textual / temporal ones — the single most important quality
    check before considering live activation.
    """
    from ..main import get_db
    db = get_db()
    if not _has_table(db):
        return {
            "total": 0, "by_method": [], "by_side": {"BUY": 0, "SELL": 0},
            "avg_net_edge": 0.0, "avg_score": 0.0, "table_exists": False,
        }

    window = f"timestamp >= datetime('now', '-{int(days)} days')"
    totals = db.query_one(
        f"SELECT COUNT(*) as n, AVG(net_edge) as avg_net_edge, "
        f"AVG(score) as avg_score FROM semantic_signals WHERE {window}"
    ) or {}
    by_side_rows = db.query(
        f"SELECT side, COUNT(*) as n FROM semantic_signals WHERE {window} "
        "GROUP BY side"
    )
    by_method_rows = db.query(
        f"SELECT synthetic_method as method, COUNT(*) as n, "
        f"AVG(net_edge) as avg_net_edge, AVG(score) as avg_score, "
        f"AVG(synthetic_confidence) as avg_conf "
        f"FROM semantic_signals WHERE {window} GROUP BY synthetic_method "
        "ORDER BY n DESC"
    )

    side_counts = defaultdict(int, {r["side"]: r["n"] for r in by_side_rows})
    by_method = [
        {
            "method": r["method"] or "unknown",
            "count": r["n"],
            "avg_net_edge": round(r["avg_net_edge"] or 0.0, 6),
            "avg_score": round(r["avg_score"] or 0.0, 4),
            "avg_confidence": round(r["avg_conf"] or 0.0, 4),
        }
        for r in by_method_rows
    ]
    return {
        "total": totals.get("n", 0) or 0,
        "avg_net_edge": round(totals.get("avg_net_edge") or 0.0, 6),
        "avg_score": round(totals.get("avg_score") or 0.0, 4),
        "by_side": {
            "BUY": int(side_counts.get("BUY", 0)),
            "SELL": int(side_counts.get("SELL", 0)),
        },
        "by_method": by_method,
        "table_exists": True,
        "window_days": days,
    }


@router.get("/semantic/calibration")
def get_calibration(
    days: int = Query(30, ge=1, le=365),
    match_window_hours: float = Query(24.0, ge=0.5, le=168.0),
):
    """Detected-vs-realised edge calibration by synthetic_method.

    This is the **closed-loop observability** that tells you whether the
    engine's edge estimates translate into actual PnL.  Matches BUY
    signals to subsequent BUY+SELL trades on the same token via FIFO
    within ``match_window_hours`` of the signal.

    Returns an ``as_dict()`` of :class:`CalibrationReport` — see
    ``src/analysis/semantic_engine/calibration.py`` for the shape.
    """
    from ..main import get_db
    db = get_db()
    if not _has_table(db):
        return {
            "n_signals": 0, "n_matched": 0, "window_days": days,
            "per_method": [], "per_side": {},
            "warnings": ["semantic_signals_table_missing"],
        }

    cutoff_sql = f"datetime('now', '-{int(days)} days')"
    sigs = db.query(
        f"SELECT timestamp, token_id, synthetic_method, net_edge, side "
        f"FROM semantic_signals WHERE timestamp >= {cutoff_sql} "
        "ORDER BY timestamp ASC"
    )
    trades = db.query(
        f"SELECT timestamp, token_id, side, price, size, strategy "
        f"FROM trades WHERE strategy = 'semantic_mispricing' "
        f"AND timestamp >= {cutoff_sql} ORDER BY timestamp ASC"
    )
    report = calibrate(
        sigs, trades,
        window_days=days,
        match_window_hours=match_window_hours,
    )
    return report.as_dict()
