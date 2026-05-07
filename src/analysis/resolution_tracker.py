"""
Market resolution tracker.

Periodically checks whether markets where we traded have resolved, and
records the outcome.  This is the ground truth measurement — not trading
PnL, but "did we predict correctly?"

A prediction is "correct" if:
- We bought YES and the market resolved YES (price → 1.0)
- We sold YES (or bought NO) and the market resolved NO (price → 0.0)

Usage:
    python -m src.main check-resolutions

The check works by:
1. Finding all condition_ids where we have trades
2. Fetching market data from Gamma API
3. If a market is now resolved/closed, recording the resolution
4. Comparing our entry price & side with the resolved outcome
"""

from __future__ import annotations

import json
import logging

from src.config import Config
from src.polymarket.client import PolymarketClient
from src.storage.sqlite_store import SQLiteStore
from src.utils.time_utils import iso_now

logger = logging.getLogger(__name__)


def check_resolutions(
    cfg: Config,
    client: PolymarketClient,
    store: SQLiteStore,
) -> str:
    """Check all traded markets for resolutions.

    Returns a markdown summary of what was found.
    """
    traded_cids = store.get_traded_condition_ids()
    if not traded_cids:
        return "No trades found — nothing to check."

    # Get existing resolutions to avoid duplicates
    existing = store.get_resolutions()
    already_resolved = {r["condition_id"] for r in existing}

    # Get our trades grouped by condition_id
    all_trades = store.get_all_trades()
    trades_by_cid: dict[str, list[dict]] = {}
    for t in all_trades:
        cid = t["condition_id"]
        if cid not in trades_by_cid:
            trades_by_cid[cid] = []
        trades_by_cid[cid].append(t)

    new_resolutions = 0
    checked = 0
    errors = 0
    now = iso_now()

    for cid in traded_cids:
        if cid in already_resolved:
            continue

        # Try to get market data from cache first
        cached = store._conn.execute(
            "SELECT data, question FROM markets_cache WHERE condition_id = ?",
            (cid,),
        ).fetchone()

        question = ""
        is_terminal = False  # market done trading (resolved OR cancelled)
        is_resolved = False  # has a ground-truth outcome
        mkt_data: dict = {}

        def _classify(d: dict) -> tuple[bool, bool]:
            """Return (is_terminal, is_resolved).

            * resolved → has a verifiable outcome (UMA settled, resolutionPrice
              set, ``resolved=True``).  Goes into accuracy stats.
            * terminal-but-not-resolved → trading stopped without a verifiable
              outcome (cancelled, withdrawn, dispute).  Logged as
              ``is_cancelled=1`` and excluded from accuracy.
            """
            res = bool(
                d.get("resolved", False)
                or d.get("umaResolutionStatus", "").lower() == "resolved"
                or d.get("resolutionTimestamp")
                or d.get("resolutionPrice") is not None
            )
            term = bool(
                res
                or d.get("closed", False)
                or d.get("archived", False)
                or not d.get("active", True)
            )
            return term, res

        if cached and cached["data"]:
            try:
                mkt_data = json.loads(cached["data"])
                question = cached["question"] or mkt_data.get("question", "")
                is_terminal, is_resolved = _classify(mkt_data)
            except (json.JSONDecodeError, TypeError):
                pass

        # Always re-fetch when cache says non-terminal (cheap freshness
        # check) to avoid stale-cache misses on recently resolved markets.
        if not is_terminal:
            try:
                fresh = client._gamma_get(f"/markets/{cid}")
                if fresh:
                    mkt_data = fresh
                    is_terminal, is_resolved = _classify(fresh)
                    question = fresh.get("question", question)
                    store.upsert_market(
                        cid, question, json.dumps(fresh), now,
                    )
            except Exception:
                errors += 1
                logger.debug("Failed to fetch market %s from Gamma", cid[:12], exc_info=True)

        checked += 1

        if not is_terminal:
            continue
        # We treat cancellations symmetrically: book an entry with
        # is_cancelled=1, pnl=0 (capital returned), and continue to the
        # next market.  Accuracy denominator excludes these (see
        # ``get_resolution_stats``).
        is_cancelled = is_terminal and not is_resolved

        # Market resolved — record each of our trades' outcomes
        trades = trades_by_cid.get(cid, [])
        buy_trades = [t for t in trades if t["side"] == "BUY"]
        sell_trades = [t for t in trades if t["side"] == "SELL"]

        for bt in buy_trades:
            token_id = bt["token_id"]
            entry_price = bt["price"]

            matching_sell = next(
                (s for s in sell_trades if s["token_id"] == token_id),
                None,
            )

            if is_cancelled:
                # No ground truth — record symmetric cancellation entry.
                # PnL: if we already exited via a SELL, lock in that PnL;
                # otherwise treat capital as returned (0 PnL).  Never
                # punish the strategy for events outside its control.
                exit_price = matching_sell["price"] if matching_sell else entry_price
                pnl = (
                    (exit_price - entry_price) * bt["size"]
                    if matching_sell
                    else 0.0
                )
                store.insert_resolution(
                    condition_id=cid,
                    token_id=token_id,
                    question=question,
                    outcome="CANCELLED",
                    resolved_price=0.0,
                    resolution_ts=now,
                    our_side="BUY",
                    our_entry_price=entry_price,
                    our_exit_price=exit_price,
                    our_pnl=pnl,
                    prediction_correct=False,  # n/a; flagged via is_cancelled
                    checked_at=now,
                    is_cancelled=True,
                )
                new_resolutions += 1
                continue

            # Try to get the current/final price for this token
            final_price = None
            try:
                final_price = client.get_price(token_id)
            except Exception:
                pass

            # Prefer the structured resolution price when the API exposes
            # it — that's the ground truth, not the last-traded price.
            if final_price is None:
                rp = mkt_data.get("resolutionPrice")
                if rp is not None:
                    try:
                        final_price = float(rp)
                    except (TypeError, ValueError):
                        final_price = None

            if final_price is None:
                # Resolved but no recoverable price → demote to cancelled
                # rather than silently assume 0 (which would invent a loss).
                exit_price = matching_sell["price"] if matching_sell else entry_price
                pnl = (
                    (exit_price - entry_price) * bt["size"]
                    if matching_sell
                    else 0.0
                )
                store.insert_resolution(
                    condition_id=cid,
                    token_id=token_id,
                    question=question,
                    outcome="CANCELLED",
                    resolved_price=0.0,
                    resolution_ts=now,
                    our_side="BUY",
                    our_entry_price=entry_price,
                    our_exit_price=exit_price,
                    our_pnl=pnl,
                    prediction_correct=False,
                    checked_at=now,
                    is_cancelled=True,
                )
                new_resolutions += 1
                continue

            # For a BUY on a YES token:
            # resolved_price near 1.0 → prediction correct
            # resolved_price near 0.0 → prediction wrong
            prediction_correct = final_price > 0.5

            exit_price = matching_sell["price"] if matching_sell else final_price
            pnl = (exit_price - entry_price) * bt["size"]

            store.insert_resolution(
                condition_id=cid,
                token_id=token_id,
                question=question,
                outcome="YES" if final_price > 0.5 else "NO",
                resolved_price=final_price,
                resolution_ts=now,
                our_side="BUY",
                our_entry_price=entry_price,
                our_exit_price=exit_price,
                our_pnl=pnl,
                prediction_correct=prediction_correct,
                checked_at=now,
                is_cancelled=False,
            )
            new_resolutions += 1

    # Build summary
    stats = store.get_resolution_stats()
    lines = [
        "# Resolution Check",
        "",
        f"Markets checked: {checked}",
        f"New resolutions recorded: {new_resolutions}",
        f"API errors: {errors}",
        "",
        "## Cumulative Resolution Stats",
        f"- Total terminal: {stats['total_resolved']} "
        f"(decided: {stats.get('decided', 0)}, cancelled: {stats.get('cancelled', 0)})",
        f"- Correct predictions: {stats['correct_predictions']}",
        f"- Accuracy (decided only): {stats['accuracy']:.1%}",
        f"- Total PnL from resolutions: ${stats['total_pnl']:+.2f}",
    ]
    return "\n".join(lines)
