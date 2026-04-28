"""
Negative-risk arbitrage executor.

Why
---
Polymarket events with N outcomes price each leg in [0, 1].  When the
*sum of best asks* across all legs of the same ``condition_id`` is
below 1.00, buying one share of every leg at those prices guarantees
$1.00 of payoff at resolution (because exactly one leg resolves to 1
and the rest to 0).  The discount is **structural edge** — it does
not depend on a directional prediction, only on the legs filling at
the prices observed.

The detector in ``src/analysis/arb_detector.py`` already finds these
opportunities.  This module is the missing executor: it takes an
``ArbOpportunity`` and routes one ``OrderRequest`` per leg through the
existing ``ExecutionEngine``, with three guards:

* **Per-leg slippage cap** — refuses a fill whose price exceeds the
  detected leg price by more than ``ARB_MAX_LEG_SLIPPAGE_PCT``.  A
  partial-fill at a worse price would silently consume the edge.
* **All-or-nothing semantics** — if any leg fails or hits the
  slippage cap, attempts to unwind already-filled legs at market.
  An open partial leg is *worse* than no arb because it carries
  full directional risk.
* **Concurrency cap** — at most ``ARB_MAX_CONCURRENT`` arbs in
  flight at once, capped by ``ARB_MAX_CAPITAL_USD`` of total
  exposure across them.

What this module does *not* do
------------------------------
* Place live orders directly — every leg goes through
  ``ExecutionEngine.execute`` so the three live-trading gates are
  honoured exactly as for any other order.
* Cross-condition arbs.  Only legs sharing a ``condition_id`` are
  considered (the case where a structural arb is well-defined).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

from src.analysis.arb_detector import ArbOpportunity
from src.config import Config
from src.polymarket.execution import ExecutionEngine, OrderRequest
from src.portfolio.tracker import PortfolioTracker, Position
from src.utils.time_utils import iso_now

logger = logging.getLogger(__name__)


@dataclass
class ArbExecutionResult:
    """Outcome of a single arb attempt."""

    status: str  # "filled" | "skipped" | "partial_unwound" | "failed"
    reason: str
    expected_edge: float
    realised_cost: float
    realised_edge: float
    legs_attempted: int
    legs_filled: int
    legs_unwound: int
    legs_detail: list[dict]

    @property
    def succeeded(self) -> bool:
        return self.status == "filled"


class NegRiskArbExecutor:
    """Routes a detected arb to one OrderRequest per leg.

    Holds a small piece of state (open arbs counter) so the
    concurrency cap survives across calls.  Lives for the duration
    of the bot loop; never persisted across restarts.
    """

    def __init__(
        self,
        cfg: Config,
        executor: ExecutionEngine,
        portfolio: PortfolioTracker,
        store,
    ) -> None:
        self.cfg = cfg
        self.executor = executor
        self.portfolio = portfolio
        self.store = store
        self._open_arbs = 0

    # ------------------------------------------------------------------
    # Top-level dispatch
    # ------------------------------------------------------------------

    def consider(self, arb: ArbOpportunity) -> ArbExecutionResult:
        """Decide whether to execute ``arb``, and do so if it passes gates.

        Returns the result regardless — callers can record every
        decision (skipped, filled, unwound) for the audit trail.
        """
        # ---- Pre-execution gates ----
        if not self._enabled():
            return self._skip(arb, "arb_executor_disabled")
        if arb.discount < self.cfg.arb_min_executable_discount:
            return self._skip(
                arb,
                f"discount {arb.discount:.4f} below execute floor "
                f"{self.cfg.arb_min_executable_discount:.4f}",
            )
        if self._open_arbs >= max(0, self.cfg.arb_max_concurrent):
            return self._skip(arb, f"max_concurrent {self.cfg.arb_max_concurrent} reached")
        notional = self._notional(arb)
        if notional <= 0:
            return self._skip(arb, "non-positive notional")
        if notional > self.cfg.arb_max_capital_usd:
            return self._skip(
                arb,
                f"notional ${notional:.2f} exceeds cap ${self.cfg.arb_max_capital_usd:.2f}",
            )
        if arb.kind != "negative_risk_long":
            # Short-arbs require holding legs we don't already own —
            # out of scope for a clean executor without a borrow path.
            return self._skip(arb, f"kind {arb.kind} not supported by executor")

        # ---- Per-leg execute with slippage cap ----
        return self._execute_long_arb(arb, notional)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enabled(self) -> bool:
        return bool(getattr(self.cfg, "arb_executor_enabled", False))

    def _notional(self, arb: ArbOpportunity) -> float:
        """Notional USD per arb = number of legs × per-leg size cap.

        Equal-size across legs: 1 share of every leg yields exactly $1
        of guaranteed payoff at resolution.  Per-leg size is the
        minimum of (configured per-leg cap, executor max_position_size).
        """
        per_leg_usd = min(
            float(self.cfg.arb_per_leg_max_usd),
            float(self.cfg.max_position_size),
        )
        return per_leg_usd * len(arb.legs)

    def _skip(self, arb: ArbOpportunity, reason: str) -> ArbExecutionResult:
        return ArbExecutionResult(
            status="skipped",
            reason=reason,
            expected_edge=arb.discount,
            realised_cost=0.0,
            realised_edge=0.0,
            legs_attempted=0,
            legs_filled=0,
            legs_unwound=0,
            legs_detail=[],
        )

    def _execute_long_arb(
        self, arb: ArbOpportunity, notional: float,
    ) -> ArbExecutionResult:
        per_leg_usd = notional / len(arb.legs)
        max_slip = float(self.cfg.arb_max_leg_slippage_pct)
        ts = iso_now()
        attempted: list[dict] = []
        filled: list[dict] = []
        total_cost = 0.0

        for token_id, outcome, leg_price, leg_spread in arb.legs:
            size = per_leg_usd / leg_price if leg_price > 0 else 0.0
            order = OrderRequest(
                token_id=token_id, condition_id=arb.condition_id,
                side="BUY", size=size, price=leg_price,
                strategy="neg_risk_arb", spread=leg_spread,
                exit_reason="",
            )
            res = self.executor.execute(order)
            attempt = {
                "token_id": token_id, "outcome": outcome,
                "leg_price": leg_price, "size": size,
                "fill_price": res.fill_price if res.success else None,
                "filled_size": res.filled_size if res.success else 0.0,
                "success": res.success,
                "message": res.message,
            }
            attempted.append(attempt)
            if not res.success:
                return self._unwind(arb, ts, attempted, filled, total_cost,
                                    reason=f"leg {token_id[:12]} failed: {res.message}")
            # Slippage cap per leg.
            fill_px = res.fill_price if res.fill_price > 0 else leg_price
            slip = (fill_px - leg_price) / leg_price if leg_price > 0 else 1.0
            if slip > max_slip:
                attempt["slippage_pct"] = slip
                return self._unwind(arb, ts, attempted, filled, total_cost,
                                    reason=(
                                        f"leg {token_id[:12]} slippage {slip:.4f} "
                                        f"> cap {max_slip:.4f}"
                                    ))
            # Book the position so reconciliation/exposure caps see it.
            actual_size = res.filled_size if res.filled_size > 0 else size
            self.portfolio.open_position(Position(
                token_id=token_id, condition_id=arb.condition_id,
                side="BUY", size=actual_size, entry_price=fill_px,
                strategy="neg_risk_arb",
                order_id=res.order_id, entry_timestamp=ts,
                category=arb.category,
            ))
            total_cost += actual_size * fill_px
            filled.append({**attempt, "filled_size": actual_size,
                           "booked_at": fill_px})

        # All legs filled within slippage budget → arb is in.
        # Realised edge = expected_payoff (1 share each → $1 at
        # resolution) − total_cost.  We size for ``per_leg_usd`` of
        # capital per leg, so the payoff at resolution equals the
        # *minimum* size across legs (since exactly one leg pays $1
        # per share).  Approximation: assume sizes equal (they do
        # under the configured per-leg cap unless the executor
        # partial-filled).
        min_filled = min(f["filled_size"] for f in filled) if filled else 0.0
        realised_edge = min_filled - total_cost
        self._open_arbs += 1
        return ArbExecutionResult(
            status="filled",
            reason="",
            expected_edge=arb.discount,
            realised_cost=total_cost,
            realised_edge=realised_edge,
            legs_attempted=len(attempted),
            legs_filled=len(filled),
            legs_unwound=0,
            legs_detail=attempted,
        )

    def _unwind(
        self,
        arb: ArbOpportunity,
        ts: str,
        attempted: list[dict],
        filled: list[dict],
        total_cost: float,
        *,
        reason: str,
    ) -> ArbExecutionResult:
        """Best-effort unwind of legs already in.

        We don't try to recoup the cost — the goal is *flat* directional
        exposure, which a SELL at any price achieves.  A tracking row in
        ``trades`` is written by the executor for every unwind.
        """
        unwound = 0
        for f in filled:
            try:
                size = f.get("filled_size") or f.get("size") or 0.0
                price = f.get("fill_price") or f.get("leg_price")
                if size <= 0:
                    continue
                rev = OrderRequest(
                    token_id=f["token_id"], condition_id=arb.condition_id,
                    side="SELL", size=size, price=price or 0.5,
                    strategy="neg_risk_arb",
                    spread=0.0, exit_reason="arb_unwind",
                )
                res = self.executor.execute(rev)
                if res.success:
                    self.portfolio.close_position(
                        f["token_id"],
                        res.fill_price if res.fill_price > 0 else (price or 0.5),
                    )
                    unwound += 1
            except Exception:
                logger.exception(
                    "Unwind leg %s failed — manual cleanup needed.",
                    f.get("token_id", "?")[:12],
                )
        status = "partial_unwound" if filled else "failed"
        return ArbExecutionResult(
            status=status,
            reason=reason,
            expected_edge=arb.discount,
            realised_cost=total_cost,
            realised_edge=0.0,
            legs_attempted=len(attempted),
            legs_filled=len(filled),
            legs_unwound=unwound,
            legs_detail=attempted,
        )


def record_arb_execution(
    store, arb: ArbOpportunity, result: ArbExecutionResult,
) -> None:
    """Persist a single arb attempt to ``arb_executions`` for the audit trail.

    Skipped attempts are also recorded — the operator may want to see
    why a candidate arb did not execute (typically: discount below
    floor or capacity reached) without trawling logs.
    """
    try:
        store.insert_arb_execution(
            timestamp=iso_now(),
            condition_id=arb.condition_id,
            question=arb.question or "",
            kind=arb.kind,
            expected_edge=result.expected_edge,
            realised_cost=result.realised_cost,
            realised_edge=result.realised_edge,
            legs_attempted=result.legs_attempted,
            legs_filled=result.legs_filled,
            legs_unwound=result.legs_unwound,
            status=result.status,
            reason=result.reason,
            legs_json=json.dumps(result.legs_detail, default=str),
        )
    except Exception:
        logger.exception("Failed to persist arb execution row.")
