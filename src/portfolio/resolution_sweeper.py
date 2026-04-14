"""
Auto-settlement of positions in resolved markets.

When a Polymarket market closes, each outcome token converges to
either $1 (winner) or $0 (loser).  A portfolio that still shows an
open position after that point carries *phantom exposure*: the
dashboard shows P&L against a stale last-seen price, the risk manager
thinks it has notional it doesn't, and paper PnL drifts silently from
ground truth.

This module walks ``PortfolioTracker.positions`` once per sweep, asks
the Gamma API for the current state of each distinct ``condition_id``,
and — for any market that has resolved — synthesises a closing fill at
the settlement price.  The fill is:

* recorded in the ``trades`` table with ``mode="paper_resolution"`` and
  ``exit_reason="market_resolved"`` so it is distinguishable from a
  real/paper exit in every audit query,
* applied to the in-memory ``PortfolioTracker`` so realised PnL,
  ``fees_paid`` (no fee on settlement), and open-position counts
  converge to reality,
* written to the ``market_resolutions`` ground-truth table so the
  "did we predict correctly?" metric stays accurate,
* alerted + metric-emitted per closure so operators see what happened.

**No live orders.** Even in live mode we *never* call the CLOB.  The
settlement price comes from Polymarket's own reported outcome — we
just book the accounting entry the exchange has already effected.

Default-off: :meth:`sweep_resolved_positions` is a pure function
driven by the caller; it only runs from the main loop when
``RESOLUTION_SWEEPER_ENABLED=true``.  This keeps the paper-trading
contract intact ("no unexpected writes") until an operator opts in.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from src.config import Config
from src.polymarket.client import PolymarketClient
from src.portfolio.tracker import PortfolioTracker, Position
from src.storage.sqlite_store import SQLiteStore
from src.utils.time_utils import iso_now

logger = logging.getLogger(__name__)


# --- Return value ---------------------------------------------------------


@dataclass
class ResolvedPosition:
    """One position closed during a sweep."""

    condition_id: str
    token_id: str
    question: str
    side: str
    size: float
    entry_price: float
    settlement_price: float
    pnl: float
    prediction_correct: bool


@dataclass
class SweepReport:
    resolved: list[ResolvedPosition] = field(default_factory=list)
    conditions_checked: int = 0
    api_errors: int = 0
    skipped_no_settlement: int = 0

    @property
    def any_resolved(self) -> bool:
        return bool(self.resolved)


# --- Market-state helpers -------------------------------------------------


def _parse_list(value) -> list:
    """Gamma returns arrays as either native lists *or* JSON strings.

    Tolerate both forms (plus ``None`` / malformed) without crashing —
    the sweeper's job is to *not* mis-settle when the payload is weird.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _is_market_resolved(mkt: dict) -> bool:
    """True iff the Gamma payload says this market has settled.

    Polymarket exposes several overlapping flags; we require *at least
    one* strong resolution indicator rather than inferring from
    ``active=false`` alone (a market can be briefly inactive mid-dispute
    without being resolved).
    """
    if not mkt:
        return False
    if mkt.get("closed") is True:
        return True
    if mkt.get("resolved") is True:
        return True
    # UMA finalisation is the ground-truth settlement signal on
    # Polymarket — "resolved" status on the oracle means payout is
    # fixed even if Gamma hasn't flipped ``closed`` yet.
    status = (mkt.get("umaResolutionStatus") or "").lower()
    if status in {"resolved", "settled"}:
        return True
    return False


def _settlement_price_for_token(mkt: dict, token_id: str) -> float | None:
    """Return the settlement price ($1 or $0, typically) for one token.

    Returns ``None`` when we cannot confidently map token → outcome:
    * missing ``clobTokenIds`` / ``outcomePrices``
    * mismatched array lengths
    * non-numeric outcome-price string

    A ``None`` result means "don't settle this position yet" — we'd
    rather leave phantom exposure on the books for one more sweep than
    book an accounting entry at the wrong price.
    """
    tokens = [str(t) for t in _parse_list(mkt.get("clobTokenIds") or mkt.get("clob_token_ids"))]
    prices_raw = _parse_list(mkt.get("outcomePrices") or mkt.get("outcome_prices"))
    if not tokens or not prices_raw:
        return None
    if len(tokens) != len(prices_raw):
        return None
    try:
        idx = tokens.index(str(token_id))
    except ValueError:
        return None
    try:
        val = float(prices_raw[idx])
    except (TypeError, ValueError):
        return None
    # Settlement prices are in [0, 1]; clamp defensively so we never
    # book an impossible payout even if the API hiccups.
    if val < 0.0:
        return 0.0
    if val > 1.0:
        return 1.0
    return val


def _winning_outcome(mkt: dict, settlement_price: float) -> str:
    """Best-effort human label for the resolved side (dashboard aid)."""
    outcomes = [str(o) for o in _parse_list(mkt.get("outcomes"))]
    if not outcomes:
        return "YES" if settlement_price >= 0.5 else "NO"
    prices = _parse_list(mkt.get("outcomePrices") or mkt.get("outcome_prices"))
    if len(outcomes) == len(prices):
        try:
            idx = max(range(len(prices)), key=lambda i: float(prices[i]))
            return outcomes[idx]
        except (TypeError, ValueError):
            pass
    return outcomes[0]


# --- Main entry point -----------------------------------------------------


def sweep_resolved_positions(
    portfolio: PortfolioTracker,
    client: PolymarketClient,
    store: SQLiteStore,
    cfg: Config,
    *,
    alerts=None,
    metrics=None,
    risk_mgr=None,
    now_fn: Callable[[], str] = iso_now,
) -> SweepReport:
    """Scan open positions; auto-close any whose market has resolved.

    Parameters mirror the usual tick-loop wiring so the sweeper plugs
    into ``run_loop`` without new plumbing.  Pass ``risk_mgr`` when
    available so daily-PnL attribution includes the settlement entry;
    omitted in tests and the CLI ``check-resolutions`` path.

    Returns a :class:`SweepReport` — the caller logs / alerts / emits
    metrics as it sees fit so this function stays testable.  No
    side-effects beyond: trade insert, portfolio mutation, resolution
    insert, cache refresh.
    """
    report = SweepReport()
    if not portfolio.positions:
        return report

    # Group open positions by condition_id so we only call the API once
    # per market even when we hold multiple legs (Yes + No).
    by_condition: dict[str, list[Position]] = {}
    for pos in portfolio.positions.values():
        if not pos.condition_id:
            continue
        by_condition.setdefault(pos.condition_id, []).append(pos)

    if not by_condition:
        return report

    now = now_fn()

    for cid, positions in by_condition.items():
        report.conditions_checked += 1
        try:
            mkt = client.get_market_by_condition_id(cid)
        except Exception:
            logger.exception("Sweeper: failed to fetch market %s", cid[:12])
            report.api_errors += 1
            continue

        if not mkt or not _is_market_resolved(mkt):
            continue

        question = mkt.get("question", "") or ""

        # Refresh the market cache so downstream tooling (dashboard,
        # resolution_tracker) sees the final payload immediately.
        try:
            store.upsert_market(cid, question, json.dumps(mkt), now)
        except Exception:
            logger.debug("Sweeper: cache refresh failed for %s", cid[:12], exc_info=True)

        for pos in positions:
            settle = _settlement_price_for_token(mkt, pos.token_id)
            if settle is None:
                # Market closed but we can't confidently map this token
                # to an outcome price — leave it for the next sweep so
                # we don't book a wrong settlement.
                report.skipped_no_settlement += 1
                logger.warning(
                    "Sweeper: market %s resolved but no settlement price for %s — skipping.",
                    cid[:12], pos.token_id[:12],
                )
                if alerts is not None:
                    alerts.warn(
                        "resolution sweep: missing settlement price",
                        condition_id=cid, token_id=pos.token_id,
                        question=question,
                    )
                continue

            # Snapshot values *before* mutating portfolio so the
            # ResolvedPosition record reflects the closed slice.
            entry_price = pos.entry_price
            size = pos.size
            side = pos.side
            pnl = portfolio.close_position(pos.token_id, settle)

            # Book the settlement as a trade for full audit trail.
            # ``exit_reason`` and ``mode`` both flag the synthetic origin.
            # ``order_id`` uses a stable deterministic key so idempotent
            # re-runs on the same DB would fail UNIQUE insertion rather
            # than double-count (SQLite surfaces via IntegrityError,
            # swallowed — see note below).
            close_side = "SELL" if side == "BUY" else "BUY"
            order_id = f"resolution:{cid[:16]}:{pos.token_id[-12:]}:{int(settle * 100):03d}"
            try:
                store.insert_trade(
                    order_id=order_id,
                    token_id=pos.token_id,
                    condition_id=cid,
                    side=close_side,
                    size=size,
                    price=settle,
                    strategy=pos.strategy or "",
                    mode="paper_resolution",
                    timestamp=now,
                    exit_reason="market_resolved",
                    spread_at_entry=0.0,
                )
            except Exception:
                logger.exception("Sweeper: failed to persist settlement trade for %s", pos.token_id[:12])

            # Decision log for operator visibility in /logs.
            try:
                store.insert_decision(
                    timestamp=now, token_id=pos.token_id, condition_id=cid,
                    action="EXIT_MARKET_RESOLVED",
                    reason=f"market settled at {settle:.2f} (pnl={pnl:+.4f})",
                    strategy=pos.strategy or "", price=settle, spread=0.0,
                    features={"settlement_price": settle, "side": side,
                              "entry_price": entry_price, "size": size},
                )
            except Exception:
                logger.debug("Sweeper: decision_log insert failed", exc_info=True)

            # Ground-truth resolution record (the audit table the
            # legacy check-resolutions CLI already populates).
            prediction_correct = (
                (side == "BUY" and settle >= 0.5) or
                (side == "SELL" and settle < 0.5)
            )
            try:
                store.insert_resolution(
                    condition_id=cid, token_id=pos.token_id, question=question,
                    outcome=_winning_outcome(mkt, settle),
                    resolved_price=settle, resolution_ts=now,
                    our_side=side, our_entry_price=entry_price,
                    our_exit_price=settle, our_pnl=pnl,
                    prediction_correct=prediction_correct, checked_at=now,
                )
            except Exception:
                # ``market_resolutions`` has no UNIQUE constraint by
                # design — duplicates are acceptable noise in the audit
                # log.  Swallow anything else so the sweeper never
                # aborts mid-iteration.
                logger.debug("Sweeper: resolution insert failed", exc_info=True)

            # Risk-manager daily PnL tracking (optional for tests).
            if risk_mgr is not None:
                try:
                    risk_mgr.record_realized_pnl(pnl)
                except Exception:
                    logger.debug("Sweeper: risk_mgr.record_realized_pnl failed", exc_info=True)

            # Dashboard's calibration table joins on the ``calibration``
            # row's exit — fill it so settlement-exits don't show as
            # orphan entries forever.
            try:
                store.update_calibration_exit(
                    token_id=pos.token_id, exit_timestamp=now,
                    exit_price=settle, exit_reason="market_resolved",
                    pnl=pnl,
                    return_pct=(settle - entry_price) / entry_price if entry_price > 0 else 0.0,
                )
            except Exception:
                logger.debug("Sweeper: calibration exit update failed", exc_info=True)

            resolved = ResolvedPosition(
                condition_id=cid, token_id=pos.token_id, question=question,
                side=side, size=size, entry_price=entry_price,
                settlement_price=settle, pnl=pnl,
                prediction_correct=prediction_correct,
            )
            report.resolved.append(resolved)

            logger.info(
                "Sweeper: settled %s %s @ %.2f → pnl=%+.4f (correct=%s)",
                side, pos.token_id[:12], settle, pnl, prediction_correct,
            )

            if alerts is not None:
                alerts.info(
                    "position settled at market resolution",
                    condition_id=cid, token_id=pos.token_id,
                    question=question[:80], side=side,
                    size=round(size, 4),
                    entry_price=round(entry_price, 4),
                    settlement_price=round(settle, 4),
                    pnl=round(pnl, 4), prediction_correct=prediction_correct,
                )
            if metrics is not None and getattr(metrics, "enabled", False):
                metrics.emit(
                    "position_resolved",
                    condition_id=cid, token_id=pos.token_id, side=side,
                    size=size, entry_price=entry_price,
                    settlement_price=settle, pnl=pnl,
                    prediction_correct=prediction_correct,
                )

    return report


# --- Scheduler helper -----------------------------------------------------


def should_run(last_run_epoch: float, interval_minutes: float, now: float) -> bool:
    """Scheduler helper analogous to ``storage.backup.should_run``.

    Returns True when the sweeper is due: ``interval_minutes > 0`` and
    either it has never run (``last_run_epoch`` is 0) or enough time
    has passed since the last run.  Keeps sweep frequency sane
    (default every 60 min) so we don't hammer the Gamma API.
    """
    if interval_minutes <= 0:
        return False
    if last_run_epoch <= 0:
        return True
    return (now - last_run_epoch) >= (interval_minutes * 60.0)
