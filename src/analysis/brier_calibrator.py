"""
Brier-calibrated edge gate.

A strategy emits a *confidence* (or, in some strategies, an *edge*).
Without calibration, two strategies that emit ``confidence=0.8`` are
treated identically by the risk manager — even if one has been
historically right 75 % of the time at that confidence and the
other 35 %.  That is sloppy.  This module fixes it.

How it works
------------
* For every closed trade we know the strategy, the predicted side
  (``BUY``/``SELL``), the entry confidence, the category, and the
  realised outcome (positive PnL = "right").
* From that history we compute a **Brier score** per
  (strategy, category) cell and per overall strategy:

      Brier = mean( (predicted_prob - realised) ** 2 )

  where ``predicted_prob`` is the entry confidence and
  ``realised`` is 1 (won) or 0 (lost).  Lower is better; 0.25 is
  the no-skill baseline (always predict 0.5); 0.0 is perfect.
* The **calibration multiplier** for a (strategy, category) cell
  is a smooth function of that Brier:

      multiplier(B) = clamp(  1 + slope * (Brier_baseline - B),
                              MIN, MAX  )

  A perfectly calibrated cell (Brier=0) earns multiplier=MAX; the
  no-skill baseline earns 1.0; worse-than-baseline earns < 1.0
  down to MIN.  We never multiply *up* aggressively (MAX caps at
  1.5 by default), but we *do* shrink misbehaving cells fast.
* The risk manager calls ``calibration_multiplier(strategy,
  category)`` and multiplies the proposed edge / size before
  applying the standard caps.  Cold-start cells (n < min_samples)
  return 1.0 — the gate does not punish a strategy with no track
  record.

Why this matters more than another strategy
-------------------------------------------
This is a meta-improvement that lifts the floor of *every* strategy
the bot runs, including future ones.  Add a strategy whose
calibration drifts and the gate shrinks its sizing automatically.
Add one whose Brier improves and the gate lets it size up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cell stats
# ---------------------------------------------------------------------------


@dataclass
class BrierCell:
    strategy: str
    category: str
    n: int
    mean_brier: float

    def is_cold_start(self, min_samples: int) -> bool:
        return self.n < min_samples


# ---------------------------------------------------------------------------
# Calibrator
# ---------------------------------------------------------------------------


class BrierCalibrator:
    """Compute and apply Brier-based calibration multipliers.

    The calibrator is **read-only against the SQLite store** — it
    computes stats on demand from the existing ``calibration`` table
    (entry confidence, exit pnl).  No new persistence is required.

    Stats are cached for ``cache_seconds`` because computing them per
    tick over a fast-growing table would dominate latency.  The
    cache is keyed by store identity so two bots sharing a store
    each have their own cache instance.
    """

    BRIER_BASELINE = 0.25  # no-skill predict-0.5 baseline

    def __init__(
        self,
        store,
        *,
        min_samples: int = 20,
        cache_seconds: float = 60.0,
        slope: float = 4.0,  # 1 unit of Brier improvement → 4× sizing change
        min_multiplier: float = 0.25,
        max_multiplier: float = 1.5,
    ) -> None:
        self.store = store
        self.min_samples = int(min_samples)
        self.cache_seconds = float(cache_seconds)
        self.slope = float(slope)
        self.min_multiplier = float(min_multiplier)
        self.max_multiplier = float(max_multiplier)
        self._cache: dict[tuple[str, str], BrierCell] = {}
        self._cache_ts: float = 0.0

    # ------------------------------------------------------------------
    # Cache lifecycle
    # ------------------------------------------------------------------

    def invalidate(self) -> None:
        """Force the next call to recompute from the store."""
        self._cache_ts = 0.0
        self._cache.clear()

    def _refresh_if_stale(self) -> None:
        import time
        now = time.time()
        if (now - self._cache_ts) < self.cache_seconds and self._cache:
            return
        self._recompute()
        self._cache_ts = now

    def _recompute(self) -> None:
        rows = self._fetch_rows()
        # Aggregate Brier per (strategy, category) and per strategy alone
        # (using empty-string as the "all-categories" wildcard cell).
        sums: dict[tuple[str, str], list[float]] = {}
        for strat, category, conf, pnl in rows:
            if conf is None or pnl is None:
                continue
            won = 1.0 if pnl > 0 else 0.0
            brier = (float(conf) - won) ** 2
            for cat in (category or "", ""):
                key = (strat or "", cat)
                sums.setdefault(key, []).append(brier)
        cache: dict[tuple[str, str], BrierCell] = {}
        for key, briers in sums.items():
            cache[key] = BrierCell(
                strategy=key[0], category=key[1],
                n=len(briers),
                mean_brier=sum(briers) / len(briers),
            )
        self._cache = cache

    def _fetch_rows(self) -> list[tuple[str, str | None, float | None, float | None]]:
        """Pull (strategy, category, confidence, pnl) for closed trades.

        ``calibration`` does not store category directly; we resolve
        it via the JSON ``features`` column when present, and fall
        back to '' otherwise.  This is best-effort — a strategy that
        never recorded category will land in the wildcard cell only.
        """
        import json
        cur = self.store._conn.execute(
            "SELECT strategy, features, confidence, pnl FROM calibration "
            "WHERE exit_timestamp IS NOT NULL AND confidence IS NOT NULL "
            "  AND pnl IS NOT NULL"
        )
        out: list[tuple[str, str | None, float | None, float | None]] = []
        for strat, features_json, conf, pnl in cur.fetchall():
            cat = ""
            if features_json:
                try:
                    f = json.loads(features_json)
                    if isinstance(f, dict):
                        cat = f.get("category") or f.get("market_category") or ""
                except (TypeError, ValueError):
                    pass
            out.append((strat or "", cat, conf, pnl))
        return out

    # ------------------------------------------------------------------
    # Public API consumed by the risk manager
    # ------------------------------------------------------------------

    def calibration_multiplier(
        self, strategy: str, category: str = "",
    ) -> float:
        """Return the sizing multiplier for a (strategy, category) cell.

        Returns 1.0 (no-op) when:
          * ``strategy`` is empty,
          * the cell has fewer than ``min_samples`` closed trades
            (cold start — never punish a strategy with no track
            record),
          * the cell does not exist in the store.

        Otherwise returns ``clamp(1 + slope * (baseline - mean_brier),
        MIN, MAX)``.  Better-than-baseline Brier scores up; worse
        scores down to ``min_multiplier``.
        """
        if not strategy:
            return 1.0
        self._refresh_if_stale()
        cell = self._cache.get((strategy, category))
        if cell is None or cell.is_cold_start(self.min_samples):
            # Try the strategy-level wildcard cell as a fallback.
            cell = self._cache.get((strategy, ""))
            if cell is None or cell.is_cold_start(self.min_samples):
                return 1.0
        raw = 1.0 + self.slope * (self.BRIER_BASELINE - cell.mean_brier)
        return max(self.min_multiplier, min(self.max_multiplier, raw))

    def stats(self) -> dict:
        """Diagnostic snapshot for the dashboard / metrics sink."""
        self._refresh_if_stale()
        return {
            "cells": [
                {
                    "strategy": c.strategy, "category": c.category,
                    "n": c.n, "mean_brier": round(c.mean_brier, 4),
                    "multiplier": round(self.calibration_multiplier(
                        c.strategy, c.category,
                    ), 4),
                }
                for c in sorted(
                    self._cache.values(),
                    key=lambda c: (-c.n, c.strategy, c.category),
                )
            ],
            "min_samples": self.min_samples,
            "baseline": self.BRIER_BASELINE,
        }
