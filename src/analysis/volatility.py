"""
Price-volatility filter — skip tokens whose recent price series is too
choppy to trade cleanly.

Definition
----------
For a prediction-market token the price already lives in [0, 1], so
the raw stddev of the last ``window`` observed prices is a
well-behaved volatility proxy with intuitive units ("cents of
oscillation").  A token whose price has swung by more than, say,
5¢ of ±1σ noise in the recent window is almost certainly being
driven by news, resolution ambiguity, or a thin book — none of which
our edge model is trained to handle.

Fail-safe
---------
If ``get_price_history`` returns fewer than ``window`` samples we
return ``None`` and the caller (``RiskManager``) lets the trade
through.  A young token with no history is *not* presumed volatile.
This matches the "allow, observe, then gate" posture we use for the
temporal filter — the alternative (reject-on-unknown) would silently
freeze the bot on any DB reset.
"""

from __future__ import annotations

import math
from typing import Sequence


def price_volatility(prices: Sequence[float], window: int) -> float | None:
    """Sample standard deviation of the last ``window`` prices.

    Returns ``None`` when fewer than ``window`` samples are available
    (a caller-visible "don't know" signal, distinct from 0.0 which
    means "perfectly stable").  Uses the unbiased (n-1) estimator —
    with tiny samples the difference matters and overestimating
    volatility is the safer error.
    """
    if window < 2:
        return None
    if prices is None:
        return None
    tail = list(prices)[-window:]
    if len(tail) < window:
        return None
    mean = sum(tail) / len(tail)
    var = sum((p - mean) ** 2 for p in tail) / (len(tail) - 1)
    return math.sqrt(var)


def is_too_volatile(
    prices: Sequence[float], window: int, max_vol: float,
) -> tuple[bool, float | None]:
    """Returns ``(too_volatile, measured_vol)``.

    ``too_volatile`` is ``False`` when we don't have enough data —
    cold start is fail-open.  When enough data is present the caller
    rejects iff ``measured_vol > max_vol``.
    """
    vol = price_volatility(prices, window)
    if vol is None:
        return False, None
    return vol > max_vol, vol
