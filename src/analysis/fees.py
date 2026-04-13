"""Opt-in execution-cost model.

Polymarket's CLOB fee schedule has historically been zero for both makers
and takers on outcome tokens, but the protocol has signalled that fees
may be activated in the future, and some integrators (proxy wallets,
bridges) do charge on deposits/withdrawals.

This module keeps fee accounting **behind two config knobs that default
to zero**, so the existing behaviour is unchanged.  When operators want
to stress-test PnL against a hypothetical 0.2% taker fee (or measure
sensitivity to any fee schedule), they flip ``TAKER_FEE_BPS`` on and
the rest of the system starts recording fees separately from gross PnL.

Important: we never mutate the stored fill price.  Gross PnL on trades
remains a clean mark-to-exit signal.  Fees are tracked as a parallel
cumulative counter on the portfolio so the dashboard can show both
``realised_pnl`` (gross) and ``net_pnl`` (gross − fees).
"""

from __future__ import annotations

from src.config import Config


def compute_fee_usd(cfg: Config, notional_usd: float, is_maker: bool = False) -> float:
    """Return the fee (in USD) for a fill of ``notional_usd`` notional.

    Parameters
    ----------
    cfg:
        Loaded :class:`src.config.Config`.  When both ``taker_fee_bps`` and
        ``maker_fee_bps`` are 0 (the default), this function always returns
        0.0 — zero overhead, zero behaviour change.
    notional_usd:
        Fill notional in USD (``filled_size * fill_price``).  Negative or
        zero notionals yield 0.0 fee.
    is_maker:
        If True, use ``maker_fee_bps``.  Default False (all current orders
        are takers since we walk the book).

    Returns
    -------
    float
        The fee in USD — strictly non-negative.
    """
    if notional_usd <= 0:
        return 0.0
    bps = cfg.maker_fee_bps if is_maker else cfg.taker_fee_bps
    if bps <= 0:
        return 0.0
    return notional_usd * (bps / 10000.0)
