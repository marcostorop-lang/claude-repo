"""
Adverse-selection filter.

Why this exists
---------------
A book that is *thinly* on our side and *deeply* on the contra side
is an adverse-selection trap: someone with information is parked at
that level willing to take all our flow.  Buying into a wall of
asks because the bid side is empty is the textbook way to lose
money to better-informed traders.  The filter blocks new BUYs in
that regime and never blocks SELLs (we always want the option to
exit).

Definitions
-----------
For a snapshot of book depth ``(bid_depth_usd, ask_depth_usd)``:

* **same-side depth** = bid for a BUY, ask for a SELL.
* **contra-side depth** = ask for a BUY, bid for a SELL.

Filter trips a BUY when *both*:
  1. ``contra_depth / same_depth >= ratio_threshold``  (a wall of
     asks vs. a thin bid).
  2. ``contra_depth >= absolute_floor``  (the wall is real, not
     just the relative side of two thin sides).

Both conditions guard against false positives — a market with $50
bid and $80 ask would otherwise trip the ratio gate but is
obviously too thin to be adverse selection in any meaningful sense.

What this is *not*
------------------
* A microstructure replacement for OFI — it is a *gate*, not a
  signal.  OFI emits BUYs/SELLs; this filter blocks BUYs that look
  predatory.
* A liquidity check — the existing ``MIN_LIQUIDITY`` filter is
  unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AdverseSelectionVerdict:
    blocked: bool
    reason: str
    ratio: float
    same_depth_usd: float
    contra_depth_usd: float


class AdverseSelectionFilter:
    """Stateful gate evaluated per BUY entry.

    Reads a book provider on demand.  The same callable shape used by
    OrderFlowImbalanceStrategy.
    """

    def __init__(
        self,
        *,
        book_provider,
        ratio_threshold: float = 3.0,
        absolute_floor_usd: float = 500.0,
    ) -> None:
        self.book_provider = book_provider
        self.ratio_threshold = float(ratio_threshold)
        self.absolute_floor_usd = float(absolute_floor_usd)

    def check(
        self, token_id: str, side: str,
    ) -> AdverseSelectionVerdict:
        """Return whether to block this side's entry on this token.

        ``side`` is ``"BUY"`` or ``"SELL"``.  SELLs always pass — we
        never want to block exits.
        """
        if side != "BUY":
            return AdverseSelectionVerdict(
                blocked=False, reason="SELL — never blocked.",
                ratio=0.0, same_depth_usd=0.0, contra_depth_usd=0.0,
            )
        if self.book_provider is None:
            return AdverseSelectionVerdict(
                blocked=False, reason="No book provider attached.",
                ratio=0.0, same_depth_usd=0.0, contra_depth_usd=0.0,
            )
        try:
            book = self.book_provider(token_id)
        except Exception:
            return AdverseSelectionVerdict(
                blocked=False, reason="Book provider raised — fail-safe.",
                ratio=0.0, same_depth_usd=0.0, contra_depth_usd=0.0,
            )
        if not isinstance(book, dict):
            return AdverseSelectionVerdict(
                blocked=False, reason="No book.",
                ratio=0.0, same_depth_usd=0.0, contra_depth_usd=0.0,
            )

        same = float(book.get("bid_depth_usd", 0.0) or 0.0)
        contra = float(book.get("ask_depth_usd", 0.0) or 0.0)
        ratio = contra / same if same > 0 else float("inf")
        if (
            ratio >= self.ratio_threshold
            and contra >= self.absolute_floor_usd
        ):
            return AdverseSelectionVerdict(
                blocked=True,
                reason=(
                    f"Adverse selection: contra ${contra:.0f} / "
                    f"same ${same:.0f} = {ratio:.2f}x "
                    f">= {self.ratio_threshold:.2f}x and contra >= "
                    f"${self.absolute_floor_usd:.0f}."
                ),
                ratio=ratio, same_depth_usd=same, contra_depth_usd=contra,
            )
        return AdverseSelectionVerdict(
            blocked=False,
            reason=f"OK (ratio={ratio:.2f}, contra=${contra:.0f}).",
            ratio=ratio, same_depth_usd=same, contra_depth_usd=contra,
        )
