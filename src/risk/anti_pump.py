"""Anti-pump filter — reject BUYs into tokens that spiked recently.

A token whose price jumped 15% in the last 10 ticks is likely riding
a news pump or thin-book spike.  Buying the top of that move is the
single most common paper→live mistake: backtest gets the post-spike
price at the instant of the signal; live execution fills *worse*
because the spike moves faster than we can post.

This filter computes the absolute return over the most recent
``window_points`` price observations.  If the magnitude exceeds a
configurable threshold, BUYs are rejected.  SELLs are never gated
(always allow exit from a pumped position).

Pure-data, stateless, depends only on a price-history loader already
available via ``RiskManager.get_price_history``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AntiPumpVerdict:
    blocked: bool
    abs_return: float
    reason: str


def check_pump(
    prices: list[float],
    *,
    window_points: int = 10,
    threshold: float = 0.10,
) -> AntiPumpVerdict:
    """Return a verdict on whether the recent price move is a pump.

    ``prices`` is oldest→newest.  We use the last ``window_points + 1``
    entries to compute a single return = (latest - base) / base.

    Cold-start safe: if there aren't enough prices, no block.
    """
    needed = window_points + 1
    if len(prices) < needed:
        return AntiPumpVerdict(
            blocked=False, abs_return=0.0,
            reason=f"insufficient history ({len(prices)}/{needed})",
        )

    base = prices[-(window_points + 1)]
    latest = prices[-1]

    if not isinstance(base, (int, float)) or not isinstance(latest, (int, float)):
        return AntiPumpVerdict(blocked=False, abs_return=0.0, reason="non-numeric price")

    if base <= 0:
        return AntiPumpVerdict(blocked=False, abs_return=0.0, reason="zero/negative base price")

    ret = (latest - base) / base
    abs_ret = abs(ret)

    if abs_ret >= threshold:
        direction = "up" if ret > 0 else "down"
        return AntiPumpVerdict(
            blocked=True,
            abs_return=abs_ret,
            reason=f"pump detected ({direction} {abs_ret:.2%} over {window_points} points, threshold {threshold:.2%})",
        )

    return AntiPumpVerdict(
        blocked=False,
        abs_return=abs_ret,
        reason="within threshold",
    )
