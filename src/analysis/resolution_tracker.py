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
        is_resolved = False
        resolved_outcome = ""

        if cached and cached["data"]:
            try:
                mkt_data = json.loads(cached["data"])
                question = cached["question"] or mkt_data.get("question", "")
                # Check if market is resolved
                is_resolved = (
                    mkt_data.get("closed", False)
                    or mkt_data.get("resolved", False)
                    or not mkt_data.get("active", True)
                )
                resolved_outcome = mkt_data.get("resolutionSource", "") or ""
            except (json.JSONDecodeError, TypeError):
                pass

        # Also try fetching fresh data from Gamma API
        if not is_resolved:
            try:
                fresh = client._gamma_get(f"/markets/{cid}")
                if fresh:
                    is_resolved = (
                        fresh.get("closed", False)
                        or fresh.get("resolved", False)
                        or not fresh.get("active", True)
                    )
                    question = fresh.get("question", question)

                    # Update cache with fresh data
                    store.upsert_market(
                        cid, question, json.dumps(fresh), now,
                    )
            except Exception:
                errors += 1
                logger.debug("Failed to fetch market %s from Gamma", cid[:12], exc_info=True)

        checked += 1

        if not is_resolved:
            continue

        # Market resolved — record each of our trades' outcomes
        trades = trades_by_cid.get(cid, [])
        buy_trades = [t for t in trades if t["side"] == "BUY"]
        sell_trades = [t for t in trades if t["side"] == "SELL"]

        for bt in buy_trades:
            token_id = bt["token_id"]
            entry_price = bt["price"]

            # Try to get the current/final price for this token
            final_price = None
            try:
                final_price = client.get_price(token_id)
            except Exception:
                pass

            if final_price is None:
                # Assume resolved to 0 or 1 based on whether it's near those
                final_price = 0.0  # conservative: assume loss

            # For a BUY on a YES token:
            # resolved_price near 1.0 → prediction correct
            # resolved_price near 0.0 → prediction wrong
            prediction_correct = final_price > 0.5

            # Find matching sell (if any) for PnL
            matching_sell = next(
                (s for s in sell_trades if s["token_id"] == token_id),
                None,
            )
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
        f"- Total resolved: {stats['total_resolved']}",
        f"- Correct predictions: {stats['correct_predictions']}",
        f"- Accuracy: {stats['accuracy']:.1%}",
        f"- Total PnL from resolutions: ${stats['total_pnl']:+.2f}",
    ]
    return "\n".join(lines)
