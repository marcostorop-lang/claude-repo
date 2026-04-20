"""Feature attribution — which entry-time features predict profitable trades?

Reads closed calibration rows, parses their ``features`` JSON, and
computes per-feature statistics: how does the distribution of a
numeric feature differ between winning and losing trades?

This is a lightweight alternative to running a full regression: for
each numeric feature key, we compute win-group vs loss-group means,
Cohen's d (standardised effect size), and win-rate when the feature
is above/below the overall median.  This lets the operator
immediately see "when ``edge > 0.04`` my win-rate is 72% vs 54%
overall", without installing scipy.

Pure-data, stateless, no external deps beyond the standard library.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureAttribution:
    feature: str
    n_total: int
    n_win: int
    n_loss: int
    win_mean: float
    loss_mean: float
    overall_mean: float
    cohens_d: float
    winrate_above_median: float
    winrate_below_median: float
    overall_winrate: float


@dataclass(frozen=True)
class AttributionReport:
    total_trades: int
    overall_winrate: float
    features: list[FeatureAttribution]


def _parse_features(row: dict) -> dict:
    raw = row.get("features", "{}")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            if isinstance(val, dict):
                return val
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _is_numeric(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return math.isfinite(v)
    return False


def compute_attribution(
    closed_calibrations: list[dict],
    *,
    min_samples: int = 20,
    min_feature_count: int = 10,
) -> AttributionReport:
    """Build a feature attribution report from closed calibration rows.

    ``min_samples`` gates the entire report; ``min_feature_count``
    gates individual features (skip rare features).
    """
    if len(closed_calibrations) < min_samples:
        return AttributionReport(
            total_trades=len(closed_calibrations),
            overall_winrate=0.0,
            features=[],
        )

    wins: list[dict] = []
    losses: list[dict] = []

    for row in closed_calibrations:
        pnl = row.get("pnl")
        if pnl is None:
            continue
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            continue
        feats = _parse_features(row)
        if pnl > 0:
            wins.append(feats)
        else:
            losses.append(feats)

    total = len(wins) + len(losses)
    if total < min_samples:
        return AttributionReport(
            total_trades=total,
            overall_winrate=len(wins) / total if total > 0 else 0.0,
            features=[],
        )

    overall_wr = len(wins) / total

    all_keys: set[str] = set()
    for f in wins + losses:
        for k, v in f.items():
            if _is_numeric(v):
                all_keys.add(k)

    results: list[FeatureAttribution] = []
    for key in sorted(all_keys):
        win_vals = [f[key] for f in wins if key in f and _is_numeric(f[key])]
        loss_vals = [f[key] for f in losses if key in f and _is_numeric(f[key])]
        n_total_feat = len(win_vals) + len(loss_vals)
        if n_total_feat < min_feature_count:
            continue

        win_mean = sum(win_vals) / len(win_vals) if win_vals else 0.0
        loss_mean = sum(loss_vals) / len(loss_vals) if loss_vals else 0.0
        all_vals = win_vals + loss_vals
        overall_mean = sum(all_vals) / len(all_vals)

        # Cohen's d: (win_mean - loss_mean) / pooled_stddev
        combined = all_vals
        variance = sum((x - overall_mean) ** 2 for x in combined) / max(len(combined) - 1, 1)
        stddev = math.sqrt(variance) if variance > 0 else 0.0
        cohens_d = (win_mean - loss_mean) / stddev if stddev > 0 else 0.0

        # Win-rate above vs below median
        sorted_all = sorted(all_vals)
        median_val = sorted_all[len(sorted_all) // 2]

        above_win = 0
        above_total = 0
        below_win = 0
        below_total = 0

        for f in wins:
            v = f.get(key)
            if v is not None and _is_numeric(v):
                if v >= median_val:
                    above_win += 1
                    above_total += 1
                else:
                    below_win += 1
                    below_total += 1

        for f in losses:
            v = f.get(key)
            if v is not None and _is_numeric(v):
                if v >= median_val:
                    above_total += 1
                else:
                    below_total += 1

        wr_above = above_win / above_total if above_total > 0 else 0.0
        wr_below = below_win / below_total if below_total > 0 else 0.0

        results.append(FeatureAttribution(
            feature=key,
            n_total=n_total_feat,
            n_win=len(win_vals),
            n_loss=len(loss_vals),
            win_mean=win_mean,
            loss_mean=loss_mean,
            overall_mean=overall_mean,
            cohens_d=cohens_d,
            winrate_above_median=wr_above,
            winrate_below_median=wr_below,
            overall_winrate=overall_wr,
        ))

    results.sort(key=lambda a: abs(a.cohens_d), reverse=True)

    return AttributionReport(
        total_trades=total,
        overall_winrate=overall_wr,
        features=results,
    )
