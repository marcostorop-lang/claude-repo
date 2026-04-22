"""Auto-resolution poller — detect closed markets and record outcomes.

Periodically queries the Gamma API for markets that we have pending
estimates for, and checks if they have resolved.  When a market
resolves, records the outcome in the calibration DB so Brier score
and log loss can be computed automatically.
"""

from __future__ import annotations

import logging

import httpx

from bot.config import cfg
from bot.core import calibration

logger = logging.getLogger(__name__)


async def poll_resolutions() -> int:
    """Check pending estimates and resolve any closed markets.

    Returns the number of newly resolved estimates.
    """
    pending = calibration.recent_estimates(limit=200)
    if not pending:
        return 0

    # Deduplicate condition_ids that are still pending
    pending_cids = {
        e["condition_id"]
        for e in pending
        if e.get("outcome") is None and e.get("condition_id")
    }
    if not pending_cids:
        return 0

    resolved_count = 0

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for cid in pending_cids:
                try:
                    resp = await client.get(
                        f"{cfg.gamma_url}/markets",
                        params={"condition_id": cid, "limit": "1"},
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    if not isinstance(data, list) or not data:
                        continue

                    market = data[0]
                    is_closed = market.get("closed", False)
                    if not is_closed:
                        continue

                    # Determine outcome from resolution data
                    outcome = _extract_outcome(market)
                    if outcome is None:
                        continue

                    n = calibration.record_outcome(cid, outcome)
                    if n > 0:
                        resolved_count += n
                        logger.info(
                            "Auto-resolved %s → outcome=%d (%d estimates updated)",
                            cid[:16], outcome, n,
                        )
                except Exception:
                    logger.debug("Resolution check failed for %s", cid[:12], exc_info=True)
    except Exception:
        logger.debug("Resolution polling failed.", exc_info=True)

    return resolved_count


def _extract_outcome(market: dict) -> int | None:
    """Extract binary outcome (0 or 1) from a resolved Gamma market."""
    # Gamma API uses several possible fields for resolution
    resolution = market.get("resolution", market.get("resolutionSource", ""))
    if isinstance(resolution, str):
        resolution = resolution.lower()
        if resolution in ("yes", "true", "1"):
            return 1
        if resolution in ("no", "false", "0"):
            return 0

    # Check outcome prices — resolved markets have [1.0, 0.0] or [0.0, 1.0]
    try:
        prices_raw = market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            import json
            prices_raw = json.loads(prices_raw)
        if isinstance(prices_raw, list) and len(prices_raw) >= 2:
            yes_price = float(prices_raw[0])
            if yes_price >= 0.99:
                return 1
            if yes_price <= 0.01:
                return 0
    except (ValueError, TypeError, IndexError):
        pass

    return None
