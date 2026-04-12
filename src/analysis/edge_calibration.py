"""
Edge calibration analyzer.

Answers the question: *when the edge model predicted an edge of X with
confidence Y, what actually happened at resolution?*

The edge model produces ``(estimated_p, edge, edge_confidence)`` at entry.
Months later, when markets resolve, we can compare:

- **Predicted direction** (BUY = market underprices → price should rise) vs
  **actual outcome** (resolved to YES or NO).
- **Predicted edge magnitude** vs **realised price move**.
- **Confidence bucket calibration**: among predictions with confidence 0.6-0.7,
  what fraction were correct?  A well-calibrated model should hit ~65% in that
  bucket.

This is the ground-truth test of whether the edge model adds value over
random trading.  If high-confidence predictions are wrong as often as
low-confidence ones, the model is noise.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from src.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


@dataclass
class ConfidenceBucket:
    """Calibration stats for a single confidence range."""

    low: float
    high: float
    count: int = 0
    correct: int = 0
    total_realised_pnl: float = 0.0
    predictions: list = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.count if self.count > 0 else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_realised_pnl / self.count if self.count > 0 else 0.0


@dataclass
class EdgeCalibrationReport:
    """Full calibration report across all resolved markets."""

    total_predictions: int = 0
    total_correct: int = 0
    buckets: list[ConfidenceBucket] = field(default_factory=list)
    edge_magnitude_buckets: list[ConfidenceBucket] = field(default_factory=list)
    correlation_edge_pnl: float = 0.0
    correlation_conf_accuracy: float = 0.0

    @property
    def overall_accuracy(self) -> float:
        return self.total_correct / self.total_predictions if self.total_predictions > 0 else 0.0


def _parse_features(features_raw: str | None) -> dict:
    if not features_raw:
        return {}
    try:
        return json.loads(features_raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def analyze_edge_calibration(store: SQLiteStore) -> EdgeCalibrationReport:
    """Build an edge-model calibration report from resolved markets.

    Joins ``market_resolutions`` (ground truth) with ``decision_log``
    (features at entry, including edge estimates) to measure whether
    predicted edge and confidence actually translate into correct outcomes.
    """
    report = EdgeCalibrationReport()

    # Get all resolved markets
    resolutions = store.get_resolutions()
    if not resolutions:
        return report

    # For each resolution, look up the ENTRY decision for that token
    # and pull out the edge features.
    cur = store._conn.execute(
        "SELECT * FROM decision_log "
        "WHERE action IN ('ENTRY_BUY', 'ENTRY_SELL') "
        "ORDER BY timestamp ASC"
    )
    decisions_by_token: dict[str, list[dict]] = {}
    for row in cur.fetchall():
        d = dict(row)
        decisions_by_token.setdefault(d["token_id"], []).append(d)

    # --- Define bucket layout ---
    # Confidence buckets: 0-0.2, 0.2-0.4, 0.4-0.6, 0.6-0.8, 0.8-1.0
    conf_buckets = [
        ConfidenceBucket(low=0.0, high=0.2),
        ConfidenceBucket(low=0.2, high=0.4),
        ConfidenceBucket(low=0.4, high=0.6),
        ConfidenceBucket(low=0.6, high=0.8),
        ConfidenceBucket(low=0.8, high=1.0),
    ]
    # Edge-magnitude buckets: 0-0.02, 0.02-0.05, 0.05-0.10, 0.10+
    edge_buckets = [
        ConfidenceBucket(low=0.0, high=0.02),
        ConfidenceBucket(low=0.02, high=0.05),
        ConfidenceBucket(low=0.05, high=0.10),
        ConfidenceBucket(low=0.10, high=1.0),
    ]

    # Running sums for Pearson correlations (edge→pnl, confidence→correct)
    sum_e = sum_p = sum_ep = sum_e2 = sum_p2 = 0.0
    sum_c = sum_a = sum_ca = sum_c2 = sum_a2 = 0.0

    for res in resolutions:
        token_id = res["token_id"]
        correct = bool(res.get("prediction_correct", 0))
        pnl = float(res.get("our_pnl", 0.0) or 0.0)
        entries = decisions_by_token.get(token_id, [])
        if not entries:
            continue
        # Use the earliest entry for this token (first time we opened)
        entry = entries[0]
        features = _parse_features(entry.get("features"))

        # Try several keys — edge-based strategy writes `edge` directly;
        # older strategies may not have edge info.
        edge = features.get("edge")
        if edge is None:
            edge = features.get("edge_edge")  # nested from edge.signals
        confidence = float(entry.get("confidence") or 0.0)

        report.total_predictions += 1
        if correct:
            report.total_correct += 1

        # Confidence bucket
        for b in conf_buckets:
            if b.low <= confidence < b.high or (b.high == 1.0 and confidence == 1.0):
                b.count += 1
                if correct:
                    b.correct += 1
                b.total_realised_pnl += pnl
                b.predictions.append({
                    "token": token_id[:12],
                    "confidence": confidence,
                    "correct": correct,
                    "pnl": pnl,
                })
                break

        # Edge-magnitude bucket (only if we have edge data)
        if edge is not None:
            edge_mag = abs(float(edge))
            for b in edge_buckets:
                if b.low <= edge_mag < b.high or (b.high == 1.0 and edge_mag >= b.low):
                    b.count += 1
                    if correct:
                        b.correct += 1
                    b.total_realised_pnl += pnl
                    break

            # Running correlation sums
            # edge→pnl: does the sign of predicted edge correlate with pnl?
            e = float(edge)
            sum_e += e
            sum_p += pnl
            sum_ep += e * pnl
            sum_e2 += e * e
            sum_p2 += pnl * pnl

        # confidence→correctness correlation (point-biserial)
        a = 1.0 if correct else 0.0
        sum_c += confidence
        sum_a += a
        sum_ca += confidence * a
        sum_c2 += confidence * confidence
        sum_a2 += a * a

    report.buckets = conf_buckets
    report.edge_magnitude_buckets = edge_buckets

    # Compute Pearson correlation for edge→pnl
    n = report.total_predictions
    if n >= 2:
        # edge→pnl (only over entries where edge existed: approximated as all)
        denom_e = (n * sum_e2 - sum_e * sum_e) * (n * sum_p2 - sum_p * sum_p)
        if denom_e > 0:
            report.correlation_edge_pnl = (
                (n * sum_ep - sum_e * sum_p) / (denom_e ** 0.5)
            )
        # confidence→correct (point-biserial)
        denom_c = (n * sum_c2 - sum_c * sum_c) * (n * sum_a2 - sum_a * sum_a)
        if denom_c > 0:
            report.correlation_conf_accuracy = (
                (n * sum_ca - sum_c * sum_a) / (denom_c ** 0.5)
            )

    return report


def format_report_markdown(report: EdgeCalibrationReport) -> str:
    """Render the calibration report as a markdown summary."""
    if report.total_predictions == 0:
        return (
            "# Edge Calibration Report\n\n"
            "No resolved markets yet.  Run `check-resolutions` after markets close "
            "to populate the dataset."
        )

    lines = [
        "# Edge Calibration Report",
        "",
        f"**Total predictions evaluated**: {report.total_predictions}",
        f"**Overall accuracy**: {report.overall_accuracy:.1%} "
        f"({report.total_correct} / {report.total_predictions})",
        "",
        "## Confidence → Accuracy calibration",
        "",
        "| Confidence | Count | Correct | Accuracy | Avg PnL |",
        "|------------|-------|---------|----------|---------|",
    ]
    for b in report.buckets:
        if b.count == 0:
            continue
        lines.append(
            f"| {b.low:.1f}–{b.high:.1f} | {b.count} | {b.correct} "
            f"| {b.accuracy:.1%} | ${b.avg_pnl:+.2f} |"
        )

    if any(b.count > 0 for b in report.edge_magnitude_buckets):
        lines.extend([
            "",
            "## Edge magnitude → Accuracy",
            "",
            "| |edge| | Count | Correct | Accuracy | Avg PnL |",
            "|--------|-------|---------|----------|---------|",
        ])
        for b in report.edge_magnitude_buckets:
            if b.count == 0:
                continue
            high_label = f"{b.high:.2f}" if b.high < 1.0 else "∞"
            lines.append(
                f"| {b.low:.2f}–{high_label} | {b.count} | {b.correct} "
                f"| {b.accuracy:.1%} | ${b.avg_pnl:+.2f} |"
            )

    lines.extend([
        "",
        "## Correlations",
        f"- **edge → pnl** (does predicted edge predict realised pnl?): "
        f"r = {report.correlation_edge_pnl:+.3f}",
        f"- **confidence → correctness** (is the model well-calibrated?): "
        f"r = {report.correlation_conf_accuracy:+.3f}",
        "",
        "A well-calibrated model has:",
        "- Accuracy increasing with confidence bucket (monotonic)",
        "- Positive correlation r > 0.2 for edge→pnl and confidence→correctness",
        "",
    ])
    return "\n".join(lines)
