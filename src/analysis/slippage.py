"""
Realised-vs-predicted slippage tracker.

At entry time the book-depth analyser computes a *predicted slippage*
for the planned size — how far VWAP diverges from the best quote when
we walk the book.  The executor then fills and reports the *actual
fill price*.  If the paper model or the book probe is right, predicted
≈ realised; if not, every trade is paying a hidden tax we didn't
account for — and the strategy's expected edge is smaller than the
dashboard claims.

This module turns the ``ENTRY_*`` rows of ``decision_log`` into a
drift metric the operator can watch *before* moving to live capital:

    drift_bps = (realised_slippage - predicted_slippage) * 1e4

Positive drift → we're paying *more* than the book predicted (book
walked deeper than expected, microstructure noise, latency).  Large
positive drift is the red flag — it eats edge directly.

Pure function, table-driven — easy to wire from a dashboard route and
to unit-test with fabricated rows.

Definition of slippage:

* predicted: ``features["slippage_pct"]`` — the book-walk cost over
  the best quote at decision time, in *fraction* (0.01 = 1%).
* realised (BUY): ``(fill_price - midpoint_at_entry) / midpoint_at_entry``.
  realised (SELL): ``(midpoint_at_entry - fill_price) / midpoint_at_entry``.

The realised number is "total execution cost over midpoint" — it
bundles half-spread + book walk.  Predicted slippage is only the book
walk.  We report both so operators can reason about each component,
plus the drift over predicted.

Rows without the required features are skipped silently (older trades
from before the tracker existed; paper-mode trades that didn't capture
a real midpoint).  That keeps the metric honest when rolling out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SlippageStats:
    """Aggregate stats for one group (per-strategy or overall)."""

    strategy: str
    n_samples: int
    predicted_mean_bps: float
    realised_mean_bps: float
    drift_mean_bps: float      # realised - predicted, in bps
    drift_p50_bps: float
    drift_p90_bps: float
    underestimate_rate: float  # fraction of trades where realised > predicted


@dataclass
class SlippageReport:
    overall: SlippageStats | None = None
    per_strategy: list[SlippageStats] = field(default_factory=list)
    skipped: int = 0           # rows missing required features


# --- Parsing ---------------------------------------------------------------


def _parse_features(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _row_slippage(row: dict) -> tuple[float, float] | None:
    """Extract ``(predicted, realised)`` slippage fractions for one row.

    Returns ``None`` when the row is missing any required field — the
    caller treats that as "skip" so partial history before tracking
    existed doesn't pollute the aggregate.
    """
    action = str(row.get("action") or "").upper()
    if not action.startswith("ENTRY_"):
        return None
    feats = _parse_features(row.get("features"))
    predicted = feats.get("slippage_pct")
    midpoint = feats.get("midpoint_at_entry")
    fill_price = feats.get("fill_price")
    if predicted is None or midpoint is None or fill_price is None:
        return None
    try:
        predicted = float(predicted)
        midpoint = float(midpoint)
        fill_price = float(fill_price)
    except (TypeError, ValueError):
        return None
    if midpoint <= 0:
        return None
    if action == "ENTRY_BUY":
        realised = (fill_price - midpoint) / midpoint
    elif action == "ENTRY_SELL":
        realised = (midpoint - fill_price) / midpoint
    else:
        return None
    return predicted, realised


# --- Aggregation -----------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    """Inclusive percentile over a finite sample, 0 on empty.

    We avoid numpy here so the module stays dependency-light; the
    sample sizes are small (thousands max) so sorted+interpolation is
    plenty fast.
    """
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    frac = k - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _summarise(strategy: str, rows: list[tuple[float, float]]) -> SlippageStats:
    predicted = [p for p, _ in rows]
    realised = [r for _, r in rows]
    drift = [(r - p) for p, r in rows]
    underestimate = [1.0 for p, r in rows if r > p]
    return SlippageStats(
        strategy=strategy,
        n_samples=len(rows),
        predicted_mean_bps=_mean(predicted) * 1e4,
        realised_mean_bps=_mean(realised) * 1e4,
        drift_mean_bps=_mean(drift) * 1e4,
        drift_p50_bps=_percentile(drift, 50.0) * 1e4,
        drift_p90_bps=_percentile(drift, 90.0) * 1e4,
        underestimate_rate=(
            len(underestimate) / len(rows) if rows else 0.0
        ),
    )


def compute_slippage_report(
    entries: list[dict],
    *,
    min_samples: int = 10,
) -> SlippageReport:
    """Reduce ``ENTRY_*`` rows to per-strategy + overall slippage stats.

    ``entries`` is a list of dicts with at least ``action``, ``strategy``,
    and ``features`` keys — the exact shape returned by
    ``SELECT action, strategy, features FROM decision_log`` with a dict
    row-factory.  Order is irrelevant.

    ``min_samples`` is the floor for *per-strategy* aggregation — a
    strategy with 3 entries is excluded from ``per_strategy`` to avoid
    noisy numbers.  The ``overall`` summary includes all samples
    regardless (it's already a noise average).

    Returns an empty report (``overall=None``) when no row has the
    required features — keeps the dashboard happy on a fresh DB.
    """
    report = SlippageReport()
    buckets: dict[str, list[tuple[float, float]]] = {}
    all_rows: list[tuple[float, float]] = []

    for row in entries:
        extracted = _row_slippage(row)
        if extracted is None:
            report.skipped += 1
            continue
        strategy = str(row.get("strategy") or "unknown")
        buckets.setdefault(strategy, []).append(extracted)
        all_rows.append(extracted)

    if not all_rows:
        return report

    report.overall = _summarise("__all__", all_rows)
    report.per_strategy = [
        _summarise(name, rows)
        for name, rows in sorted(buckets.items())
        if len(rows) >= min_samples
    ]
    return report
