"""
Exact Kelly-criterion sizing for prediction-market binary bets.

For a binary outcome with true probability ``p`` of winning and net
odds ``b`` (payoff per unit stake on a win), the Kelly-optimal stake
fraction is::

    f* = (p · b - q) / b        where q = 1 - p

On Polymarket a YES share costs ``price`` and pays $1 on a win, so
``b = (1 - price) / price`` and the implied Kelly fraction reduces to
``(p - price) / (1 - price)`` — which is the *edge* divided by the
"room above price".

This module exposes :func:`kelly_fraction`, a pure function that never
returns a negative value (if the bet is -EV we just don't take it)
and is clamped to ``[0, 1]`` to keep the caller's arithmetic safe.

The integrating function multiplies this by ``KELLY_FRACTION`` (e.g.
0.25 for quarter-Kelly, the retail-conservative default) and by
``confidence`` so that noisy high-edge signals are sized down.

Why a distinct module from the pre-existing ``sizing_edge_kelly``?
------------------------------------------------------------------
The legacy path uses ``|edge| * confidence * kelly_fraction`` as a
linear proxy.  That's robust but systematically *under*-sizes large
edges at near-fair prices and *over*-sizes small edges at extreme
prices — exactly where the exact formula matters most.  We keep the
old proxy behind its own flag; operators opt into the exact formula
via ``SIZING_KELLY_PROPER=true``.
"""

from __future__ import annotations


def kelly_fraction(price: float, edge: float) -> float:
    """Return the Kelly-optimal *stake fraction* for a binary YES bet.

    ``price`` is the market price of the outcome token (in 0..1) and
    ``edge`` is our estimated probability minus that price (so the
    implied true probability is ``p = price + edge``).  A negative or
    zero edge means the bet is not +EV and we return 0.0.

    The result is clamped to ``[0, 1]``: at ``edge == 1 - price`` the
    mathematical Kelly fraction is 1 (stake the whole bankroll), which
    we cap there; any pathological input above that is treated as the
    cap.  Extreme prices (``price <= 0`` or ``price >= 1``) collapse
    the bet to a degenerate case and also return 0.0.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    if edge <= 0.0:
        return 0.0
    p = price + edge
    # Cap p in (0, 1] — an estimated probability > 1 is nonsense but
    # we don't want a caller's bad input to corrupt downstream arithmetic.
    if p >= 1.0:
        return 1.0
    # b = (1 - price) / price, q = 1 - p
    # f* = (p*b - q) / b  = (p - price) / (1 - price)  after algebra.
    f_star = (p - price) / (1.0 - price)
    if f_star <= 0.0:
        return 0.0
    return min(f_star, 1.0)
