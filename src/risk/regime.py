"""
Correlated-event / regime-shift detector.

Normal market noise is diffuse: a handful of tokens move a few
percent in a 5-minute window and the aggregate of the book looks
unchanged.  A *regime shift* is different — an exogenous event (a
news flash, a resolution cascade, a chain-wide outage) moves *many*
tokens *at the same time* in the same direction.  The bot's per-trade
risk caps say nothing about this, because they were all computed
under an implicit independence assumption that no longer holds.

This module exposes a small, dependency-free detector:

    verdict = detect_regime_shift(
        token_returns,                # {token_id: recent fractional return}
        move_threshold=0.05,          # tokens moving >5% count as "big move"
        fraction_threshold=0.25,      # ≥25% of universe moving big → shift
        min_universe=20,              # ignore tiny/cold universes
    )

The verdict carries ``shift_detected`` plus the diagnostics the
operator needs to decide whether to pause the bot by hand (kill
switch file) or just monitor.  We deliberately do not auto-halt:
regime detection with tight thresholds has too many false positives
on small universes, and a false halt mid-event is itself a risk.

Input is pre-computed per-token fractional returns (``(p_now - p_then) /
p_then``) so the caller controls the window.  Keeps this module pure
and trivially testable.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class RegimeVerdict:
    """One regime-shift evaluation."""

    shift_detected: bool
    universe_size: int
    big_move_count: int
    big_move_fraction: float
    mean_abs_return: float
    median_abs_return: float
    directional_bias: float  # mean(signed return) — negative = down-move
    reason: str


def detect_regime_shift(
    token_returns: dict[str, float],
    *,
    move_threshold: float = 0.05,
    fraction_threshold: float = 0.25,
    min_universe: int = 20,
) -> RegimeVerdict:
    """Classify a basket of per-token returns as calm vs. regime-shift.

    A "big move" is ``abs(return) >= move_threshold``.  A *shift* is
    declared when at least ``fraction_threshold`` of the universe is
    moving big AND the universe is at least ``min_universe`` tokens
    (otherwise noise dominates).

    The directional_bias is the *signed* mean of returns — if the
    universe is shifting *down* together (classic risk-off) the
    number is negative.  A shift with |bias| small means tokens are
    moving in both directions (still notable — volatility regime
    change — but different interpretation).

    Malformed inputs (non-finite, missing) are dropped silently so a
    single bad tick can't flip the verdict.
    """
    clean: list[tuple[str, float]] = []
    for tok, ret in token_returns.items():
        try:
            r = float(ret)
        except (TypeError, ValueError):
            continue
        # nan / inf poison statistics — skip.
        if r != r or r in (float("inf"), float("-inf")):
            continue
        clean.append((tok, r))

    n = len(clean)
    if n == 0:
        return RegimeVerdict(
            False, 0, 0, 0.0, 0.0, 0.0, 0.0,
            "empty universe — no decision",
        )

    abs_returns = [abs(r) for _, r in clean]
    big = [1 for ar in abs_returns if ar >= move_threshold]
    big_count = sum(big)
    big_fraction = big_count / n
    mean_abs = sum(abs_returns) / n
    median_abs = statistics.median(abs_returns)
    bias = sum(r for _, r in clean) / n

    if n < min_universe:
        return RegimeVerdict(
            False, n, big_count, big_fraction, mean_abs, median_abs, bias,
            (
                f"universe too small: {n} < min_universe={min_universe} — "
                "waiting for more data"
            ),
        )

    shift = big_fraction >= fraction_threshold
    if shift:
        direction = (
            "down" if bias < -move_threshold / 2
            else "up" if bias > move_threshold / 2
            else "mixed"
        )
        reason = (
            f"regime shift ({direction}): "
            f"{big_count}/{n} tokens moved |≥{move_threshold:.0%}| "
            f"(bias={bias:+.2%})"
        )
    else:
        reason = (
            f"calm: {big_count}/{n} big moves "
            f"({big_fraction:.1%} < {fraction_threshold:.0%} threshold)"
        )
    return RegimeVerdict(
        shift, n, big_count, big_fraction, mean_abs, median_abs, bias, reason,
    )


# --- Input helper ---------------------------------------------------------


def returns_from_price_history(
    histories: dict[str, list[float]],
    *,
    lookback_points: int = 5,
) -> dict[str, float]:
    """Turn per-token price lists into per-token returns.

    ``histories[token_id]`` is a time-ordered list (oldest → newest)
    of prices.  We take the newest point and the point
    ``lookback_points`` back and compute ``(now - then) / then``.
    Tokens with insufficient history, zero/negative "then", or
    missing values are skipped.

    Thin helper — the caller is responsible for fetching the history
    (usually a couple of ``get_price_history`` calls with the same
    window for each token).
    """
    out: dict[str, float] = {}
    for tok, hist in histories.items():
        if not hist or len(hist) <= lookback_points:
            continue
        now = hist[-1]
        then = hist[-(lookback_points + 1)]
        try:
            now_f = float(now)
            then_f = float(then)
        except (TypeError, ValueError):
            continue
        if then_f <= 0:
            continue
        out[tok] = (now_f - then_f) / then_f
    return out
