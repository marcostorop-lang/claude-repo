"""Risk visibility endpoint.

Surfaces the four risk/sizing signals the bot now writes per tick:

* Latest portfolio tail-risk metrics (VaR/CVaR/worst-case) from
  ``tick_stats`` so the dashboard can show *current* downside.
* A short time series of those metrics for charting.
* Aggregated counts of *why* recent BUY decisions were rejected,
  parsed out of ``decision_log.reason`` — directly answers
  "is the bot trading right now and if not, what's blocking it?".
* Per-strategy Bayesian posterior summary (mean / sample count / size
  multiplier) — read out of the live engine when available, falling
  back to a SQL aggregation over the calibration table when the bot
  process is not the one hosting this dashboard.

All queries are read-only and tolerant of empty / freshly-bootstrapped
databases — they return zero/empty arrays rather than 500.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["risk"])


_REJECTION_BUCKETS: list[tuple[str, str]] = [
    # Substring → normalised bucket label.  Order matters: more
    # specific patterns first so e.g. "Book spread" wins over the
    # generic "Spread" bucket.
    ("Circuit breaker", "circuit_breaker"),
    ("Temporal filter", "temporal_filter"),
    ("Volatility", "volatility"),
    ("Already have", "duplicate_position"),
    ("Edge", "min_edge"),
    ("Stale price", "stale_price"),
    ("Book spread", "book_spread"),
    ("Spread", "spread"),
    ("slippage", "slippage"),
    ("imbalance", "book_imbalance"),
    ("Max open", "max_open_positions"),
    ("Max total exposure", "max_total_exposure"),
    ("Event exposure", "event_concentration"),
    ("Category", "category_concentration"),
    ("Price", "price_band"),
]


def _bucket_reason(reason: str) -> str:
    if not reason:
        return "other"
    for needle, label in _REJECTION_BUCKETS:
        if needle.lower() in reason.lower():
            return label
    return "other"


@router.get("/risk")
def get_risk(window: int = 50) -> dict[str, Any]:
    """Return current risk posture + recent rejection mix."""
    from ..main import get_db
    db = get_db()

    # --- Tail-risk: latest tick + short series -----------------------
    latest = db.query_one(
        "SELECT timestamp, var_95, cvar_95, worst_case, "
        "open_positions, total_exposure "
        "FROM tick_stats ORDER BY id DESC LIMIT 1"
    ) or {}

    # Bound the series for charting — defaults to last 50 ticks
    # (~50 minutes at the 60s default poll interval).
    n = max(1, min(int(window), 500))
    series_rows = db.query(
        "SELECT timestamp, var_95, cvar_95, worst_case "
        "FROM tick_stats ORDER BY id DESC LIMIT ?",
        (n,),
    )
    series = [
        {
            "timestamp": r["timestamp"],
            "var_95": round(r.get("var_95") or 0.0, 4),
            "cvar_95": round(r.get("cvar_95") or 0.0, 4),
            "worst_case": round(r.get("worst_case") or 0.0, 4),
        }
        for r in reversed(series_rows)  # oldest → newest for charting
    ]

    # --- Recent rejection mix ---------------------------------------
    rejections = db.query(
        "SELECT reason FROM decision_log "
        "WHERE action = 'RISK_REJECTED' "
        "ORDER BY id DESC LIMIT 200"
    )
    bucketed: Counter[str] = Counter(_bucket_reason(r["reason"]) for r in rejections)

    # --- Sizing-trace summary --------------------------------------------
    # Pull the last N ENTRY_BUY rows and compute simple summaries of the
    # sizing_trace fields we now log (capital_efficiency_factor,
    # bayes_size_mult, kelly_f_star, hour_allowed).  Surface min/avg so
    # the operator can quickly see whether shrinkage features are
    # actually firing in production.
    entry_rows = db.query(
        "SELECT features FROM decision_log "
        "WHERE action = 'ENTRY_BUY' "
        "ORDER BY id DESC LIMIT 100"
    )
    cap_eff: list[float] = []
    bayes_mult: list[float] = []
    kelly_star: list[float] = []
    hour_allowed_rate = {"true": 0, "false": 0}
    for row in entry_rows:
        try:
            feat = json.loads(row.get("features") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(feat.get("capital_efficiency_factor"), (int, float)):
            cap_eff.append(float(feat["capital_efficiency_factor"]))
        if isinstance(feat.get("bayes_size_mult"), (int, float)):
            bayes_mult.append(float(feat["bayes_size_mult"]))
        if isinstance(feat.get("kelly_f_star"), (int, float)):
            kelly_star.append(float(feat["kelly_f_star"]))
        if "hour_allowed" in feat:
            hour_allowed_rate["true" if feat["hour_allowed"] else "false"] += 1

    def _summary(xs: list[float]) -> dict[str, float | int]:
        if not xs:
            return {"n": 0, "min": 0.0, "avg": 0.0, "max": 0.0}
        return {
            "n": len(xs),
            "min": round(min(xs), 4),
            "avg": round(sum(xs) / len(xs), 4),
            "max": round(max(xs), 4),
        }

    return {
        "latest": {
            "timestamp": latest.get("timestamp"),
            "var_95": round(latest.get("var_95") or 0.0, 4),
            "cvar_95": round(latest.get("cvar_95") or 0.0, 4),
            "worst_case": round(latest.get("worst_case") or 0.0, 4),
            "open_positions": latest.get("open_positions") or 0,
            "total_exposure": round(latest.get("total_exposure") or 0.0, 4),
        },
        "series": series,
        "rejections": {
            "total": sum(bucketed.values()),
            "by_bucket": dict(bucketed.most_common()),
        },
        "sizing_trace": {
            "capital_efficiency_factor": _summary(cap_eff),
            "bayes_size_mult": _summary(bayes_mult),
            "kelly_f_star": _summary(kelly_star),
            "hour_allowed": hour_allowed_rate,
        },
    }
