"""Negative-risk / structural arbitrage detector.

READ-ONLY by design.  This module **detects** arbitrage opportunities
on Polymarket binary (and multi-outcome) markets — it never trades on
them.  The rationale is conservative: an arb detector that is wrong
about token-pairing or staleness would send capital into losing trades
under the illusion of "free money".  So we surface detections in the
store and on the dashboard, and let the operator decide whether the
arb is real before ever acting on it.

The core insight
----------------
Polymarket markets have N outcome tokens whose prices *should* sum to
1.0 at any instant (ignoring spread and resolution cost).  When the
best-ask prices across all outcomes sum to *less than* 1.0 by a
meaningful margin, buying one of each at ask locks a guaranteed profit
at resolution — this is a **negative-risk** arbitrage.

Symmetrically, when best-bid prices across all outcomes sum to *more
than* 1.0, selling one of each at bid locks a guaranteed profit.  This
symmetric case is rarer and only viable when you already hold all
outcomes (or can short them) — we still detect and log it.

Caveats (why this is detection-only, not trading):
- Token pairing must be correct.  We rely on ``condition_id`` being
  the canonical grouping key.
- Prices must be fresh and consistent.  Using stale ``/price`` data
  against a moved book produces phantom arbs.
- Multi-outcome markets with poor liquidity can show negative-risk on
  the API but not fillable in the book at those prices — so we also
  require all sides to have positive depth.

Usage
-----
- ``find_negative_risk_arbs(snapshots)`` — pure function over a list
  of MarketSnapshots.  Returns a list of opportunities.
- ``scan_and_record(snapshots, store)`` — calls the above, persists
  detections to the ``arb_opportunities`` table for observability.
- Integration hook in ``main._tick``: when ``ARB_DETECTOR_ENABLED=true``,
  we invoke the scan each tick.  Default: **off**.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from src.polymarket.market_data import MarketSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArbOpportunity:
    """A detected structural arbitrage opportunity.

    Attributes
    ----------
    condition_id:
        Canonical market identifier (one arb per condition_id).
    question:
        Human-readable market question.
    kind:
        ``"negative_risk_long"`` = buy all outcomes for < 1.0 (most common).
        ``"negative_risk_short"`` = sell all outcomes at > 1.0 (rarer,
        requires holding or shorting all legs).
    sum_prices:
        Total of the prices used (asks for long, bids for short).
    discount:
        For ``long``: 1.0 - sum_prices (positive = profitable).
        For ``short``: sum_prices - 1.0.
    edge_pct:
        ``discount`` expressed as % of the sum — useful for ranking.
    legs:
        List of ``(token_id, outcome, price, spread)`` tuples — full
        forensic detail so operators can re-verify before acting.
    category:
        Market category (politics, sports, etc.) for dashboard grouping.
    min_liquidity:
        Minimum liquidity across the legs — a proxy for fillability.
    """

    condition_id: str
    question: str
    kind: str
    sum_prices: float
    discount: float
    edge_pct: float
    legs: list[tuple[str, str, float, float]] = field(default_factory=list)
    category: str = ""
    min_liquidity: float = 0.0

    @property
    def is_profitable(self) -> bool:
        return self.discount > 0


def find_negative_risk_arbs(
    snapshots: Sequence[MarketSnapshot],
    min_discount: float = 0.01,
    min_legs_liquidity: float = 100.0,
) -> list[ArbOpportunity]:
    """Scan snapshots for negative-risk arbitrage opportunities.

    Parameters
    ----------
    snapshots:
        Market snapshots (already enriched with price/spread).
    min_discount:
        Minimum absolute discount to report — below this the "arb" is
        swamped by spread + fees + timing noise.  Default 1% (100 bps),
        which is comfortably wider than current Polymarket spreads on
        liquid markets.
    min_legs_liquidity:
        Each leg must have reported ``liquidity`` above this USD floor,
        otherwise the arb is unfillable in practice.  Default $100.

    Returns
    -------
    list[ArbOpportunity]
        Sorted by ``edge_pct`` descending — biggest edge first.  Empty
        when there are no qualifying detections.
    """
    # Group snapshots by condition_id.  Skip any snapshot without price
    # (we can't evaluate an arb without a price).
    groups: dict[str, list[MarketSnapshot]] = {}
    for s in snapshots:
        if s.price is None or s.condition_id == "":
            continue
        groups.setdefault(s.condition_id, []).append(s)

    results: list[ArbOpportunity] = []
    for cid, legs in groups.items():
        # Need at least 2 outcomes to form an arb (binary or multi-way).
        if len(legs) < 2:
            continue
        # Skip if any leg has zero liquidity (structural — the book is
        # empty).
        min_liq = min(leg.liquidity for leg in legs)
        if min_liq < min_legs_liquidity:
            continue

        # Approximate best-ask as price + spread/2 (fallback when we don't
        # have the live book).  Callers who have BookAnalysis cached
        # should prefer the book-walked version, but for a cheap scan this
        # is close enough — and we require a meaningful discount so the
        # noise washes out.
        def _ask(leg: MarketSnapshot) -> float:
            sp = leg.spread or 0.0
            return (leg.price or 0.0) + sp / 2.0

        def _bid(leg: MarketSnapshot) -> float:
            sp = leg.spread or 0.0
            return max((leg.price or 0.0) - sp / 2.0, 0.0)

        sum_asks = sum(_ask(leg) for leg in legs)
        sum_bids = sum(_bid(leg) for leg in legs)

        # Long arb: all asks sum to less than 1 — buying one of each
        # guarantees a payout of 1 at resolution for less than 1 paid.
        if sum_asks < 1.0 - min_discount:
            disc = 1.0 - sum_asks
            edge_pct = disc / sum_asks if sum_asks > 0 else 0.0
            results.append(ArbOpportunity(
                condition_id=cid,
                question=legs[0].question,
                kind="negative_risk_long",
                sum_prices=sum_asks,
                discount=disc,
                edge_pct=edge_pct,
                legs=[(leg.token_id, leg.outcome, _ask(leg), leg.spread or 0.0) for leg in legs],
                category=legs[0].category,
                min_liquidity=min_liq,
            ))
            continue  # A market can be long OR short but not both sensibly.

        # Short arb: all bids sum to more than 1 — selling one of each
        # generates more than 1, and we owe exactly 1 at resolution.
        # Requires holding or shorting every outcome, so it's niche.
        if sum_bids > 1.0 + min_discount:
            disc = sum_bids - 1.0
            edge_pct = disc / sum_bids if sum_bids > 0 else 0.0
            results.append(ArbOpportunity(
                condition_id=cid,
                question=legs[0].question,
                kind="negative_risk_short",
                sum_prices=sum_bids,
                discount=disc,
                edge_pct=edge_pct,
                legs=[(leg.token_id, leg.outcome, _bid(leg), leg.spread or 0.0) for leg in legs],
                category=legs[0].category,
                min_liquidity=min_liq,
            ))

    results.sort(key=lambda a: a.edge_pct, reverse=True)
    return results


def scan_and_record(
    snapshots: Sequence[MarketSnapshot],
    store,
    timestamp: str,
    min_discount: float = 0.01,
    min_legs_liquidity: float = 100.0,
) -> list[ArbOpportunity]:
    """Run :func:`find_negative_risk_arbs` and persist the detections.

    Returns the full list of detections (same as the underlying scan)
    so the caller can log / dashboard them if desired.
    """
    arbs = find_negative_risk_arbs(snapshots, min_discount=min_discount,
                                   min_legs_liquidity=min_legs_liquidity)
    if not arbs:
        return arbs
    try:
        store.insert_arb_opportunities(timestamp, arbs)
    except AttributeError:
        # Older stores without the arb table — skip persistence silently.
        logger.debug("Store has no insert_arb_opportunities; skipping persistence.")
    if arbs:
        logger.info(
            "Arb detector: %d opportunities found; top edge=%.2f%% on %s",
            len(arbs), arbs[0].edge_pct * 100, arbs[0].question[:60],
        )
    return arbs


def format_report_markdown(arbs: Sequence[ArbOpportunity], limit: int = 20) -> str:
    """Render a markdown table of arb opportunities — for CLI output."""
    if not arbs:
        return "_No negative-risk arbitrage opportunities detected._"
    lines = [
        "| Edge % | Sum prices | Discount | Outcomes | Question | Liquidity |",
        "|---:|---:|---:|---|---|---:|",
    ]
    for a in arbs[:limit]:
        outcomes = " + ".join(f"{leg[1]}@{leg[2]:.3f}" for leg in a.legs)
        lines.append(
            f"| {a.edge_pct * 100:.2f}% | {a.sum_prices:.4f} | "
            f"{a.discount:+.4f} | {outcomes} | "
            f"{(a.question or '-')[:50]} | ${a.min_liquidity:,.0f} |"
        )
    return "\n".join(lines)
