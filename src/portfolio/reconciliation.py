"""
Position-vs-Polymarket reconciliation sweeper.

The in-memory ``PortfolioTracker`` is the bot's authoritative view of
what we own.  In live mode it can silently drift from the on-chain
truth for all the boring-but-lethal reasons:

* a manual trade through the Polymarket UI that the bot never saw,
* an SDK-level partial fill that the executor booked but the wallet
  did not receive (gas spike, slippage reject),
* a resolution sweep against a market the oracle later un-resolved,
* an order that failed locally *after* funds were debited on-chain,
* a restart that replayed the trade log at the wrong water-mark.

None of these are rare enough to ignore forever.  This module runs
periodically, asks the CLOB how many shares of each tracked token are
actually in the wallet, and reports divergences above a tolerance.

**Report only — never mutate.**  Automatically "correcting" portfolio
state would hide real bugs: if we silently add shares the bot thinks
it doesn't own, we risk trading against them as a second position; if
we silently delete tracked shares that the chain confirms exist, we
double-count on the next reconstruction.  The sweeper surfaces the
mismatch via alert + metric so an operator can diagnose the root
cause and reconcile manually.

Default-off; paper mode is always a no-op (the tracker *is* the
ground truth in paper).  Fail-safe: if the SDK is missing or the RPC
blips, the sweep reports ``skipped_no_fetcher`` / ``fetch_errors`` and
does not panic the bot.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.portfolio.tracker import PortfolioTracker

logger = logging.getLogger(__name__)


# --- Data types -----------------------------------------------------------


@dataclass
class ReconciliationDivergence:
    """One token whose tracked vs on-chain share count disagrees."""

    token_id: str
    condition_id: str
    side: str
    tracked_size: float
    onchain_size: float
    diff: float  # onchain - tracked; negative → phantom local holdings
    kind: str  # "phantom_local" | "untracked_onchain" | "size_mismatch"


@dataclass
class ReconciliationReport:
    """Aggregate output of one reconciliation sweep."""

    divergences: list[ReconciliationDivergence] = field(default_factory=list)
    positions_checked: int = 0
    fetch_errors: int = 0
    skipped_no_fetcher: bool = False

    @property
    def any_divergence(self) -> bool:
        return bool(self.divergences)


# --- Classification -------------------------------------------------------


def _classify(tracked: float, onchain: float, tolerance: float) -> Optional[str]:
    """Return a divergence kind or None if within tolerance."""
    diff = onchain - tracked
    if abs(diff) <= tolerance:
        return None
    if tracked > 0 and onchain <= tolerance:
        return "phantom_local"
    if tracked <= tolerance and onchain > 0:
        return "untracked_onchain"
    return "size_mismatch"


# --- Core sweep -----------------------------------------------------------


def reconcile_positions(
    portfolio: PortfolioTracker,
    balance_fetcher: Optional[Callable[[str], float | None]],
    *,
    tolerance_shares: float = 0.01,
    alerts=None,
    metrics=None,
    extra_token_ids: Optional[list[str]] = None,
) -> ReconciliationReport:
    """Compare tracked positions against on-chain share balances.

    ``balance_fetcher(token_id) -> shares or None``.  ``None`` means
    "couldn't read" and counts as a fetch error for that token (report
    it, don't flag it as a divergence — fail-safe).

    ``extra_token_ids`` lets the caller probe specific tokens the
    tracker doesn't know about (e.g. passing the full set of tokens
    traded in the last N days to catch "untracked on-chain" after a
    restart).
    """
    report = ReconciliationReport()

    if balance_fetcher is None:
        report.skipped_no_fetcher = True
        logger.debug("Reconciliation: no balance fetcher wired — skipping.")
        return report

    # Union of tracked tokens + caller-supplied probes so we also catch
    # the "untracked_onchain" case (chain has it, tracker doesn't).
    tracked_by_token = dict(portfolio.positions)
    all_tokens = set(tracked_by_token.keys())
    if extra_token_ids:
        all_tokens.update(extra_token_ids)

    for token_id in sorted(all_tokens):
        report.positions_checked += 1
        pos = tracked_by_token.get(token_id)
        tracked_size = float(pos.size) if pos is not None else 0.0

        try:
            onchain = balance_fetcher(token_id)
        except Exception:
            logger.exception(
                "Reconciliation: fetcher raised for %s", token_id[:12],
            )
            report.fetch_errors += 1
            continue

        if onchain is None:
            # Couldn't read — neither a pass nor a fail.  Surface as an
            # error so operators know the check was incomplete, but
            # don't synthesise a divergence record.
            report.fetch_errors += 1
            continue

        onchain_size = float(onchain)
        kind = _classify(tracked_size, onchain_size, tolerance_shares)
        if kind is None:
            continue

        divergence = ReconciliationDivergence(
            token_id=token_id,
            condition_id=pos.condition_id if pos is not None else "",
            side=pos.side if pos is not None else "",
            tracked_size=tracked_size,
            onchain_size=onchain_size,
            diff=onchain_size - tracked_size,
            kind=kind,
        )
        report.divergences.append(divergence)

        logger.warning(
            "Reconciliation: %s on %s — tracked=%.4f onchain=%.4f diff=%+.4f",
            kind, token_id[:12], tracked_size, onchain_size,
            onchain_size - tracked_size,
        )

        if alerts is not None:
            # Critical only when we *think* we have shares that aren't
            # actually there — that's the scenario most likely to
            # produce bad trading decisions.
            severity = "critical" if kind == "phantom_local" else "warn"
            notify = getattr(alerts, severity, None)
            if callable(notify):
                notify(
                    "portfolio reconciliation divergence",
                    token_id=token_id, condition_id=divergence.condition_id,
                    side=divergence.side, kind=kind,
                    tracked_size=round(tracked_size, 6),
                    onchain_size=round(onchain_size, 6),
                    diff=round(onchain_size - tracked_size, 6),
                )

        if metrics is not None and getattr(metrics, "enabled", False):
            metrics.emit(
                "portfolio_reconciliation_divergence",
                token_id=token_id, condition_id=divergence.condition_id,
                kind=kind, tracked_size=tracked_size,
                onchain_size=onchain_size,
                diff=onchain_size - tracked_size,
            )

    return report


# --- Live fetcher builder -------------------------------------------------


def build_clob_shares_fetcher(cfg) -> Optional[Callable[[str], float | None]]:
    """Return a callable ``token_id -> shares`` backed by py-clob-client.

    ``None`` when the SDK isn't importable, we're in paper mode, or
    credentials are missing — the caller treats that as "skip
    reconciliation this run".  Shares are ERC1155 units (integer
    on-chain); the fetcher divides by the conventional 1e6 scale so
    downstream code talks in the same float-USDC units as the rest of
    the bot.
    """
    if not cfg.is_live:
        return None
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    except ImportError:
        logger.debug(
            "py-clob-client not importable — no reconciliation fetcher."
        )
        return None
    if not cfg.private_key:
        return None

    def _fetch(token_id: str) -> float | None:
        try:
            client = ClobClient(
                cfg.clob_url, key=cfg.private_key, chain_id=cfg.chain_id,
            )
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
            )
            result = client.get_balance_allowance(params)
            raw = result.get("balance") if isinstance(result, dict) else None
            if raw is None:
                return None
            return float(raw) / 1_000_000.0
        except Exception:
            logger.exception(
                "CLOB get_balance_allowance failed for %s", token_id[:12],
            )
            return None

    return _fetch


# --- Scheduler helper -----------------------------------------------------


def reconcile_paper_self(
    portfolio: PortfolioTracker,
    store,
    *,
    tolerance_shares: float = 1e-6,
    alerts=None,
    metrics=None,
) -> ReconciliationReport:
    """Compare the live tracker against what the trade log would replay.

    On-chain reconciliation is meaningless in paper mode (no chain), but
    *internal* drift between the in-memory tracker and the SQLite trade
    log is still possible — for instance:

    * a position was opened but the matching ``insert_trade`` failed
      silently (older bare-except),
    * the trade log was tampered with (pruning, manual edits),
    * a bug in :meth:`PortfolioTracker.reconstruct_from_trades` that
      diverges from the live ``open_position``/``close_position`` flow.

    Catching these in paper is cheap insurance — we have the DB right
    here.  We rebuild a side tracker, compare position-by-position,
    and report exactly like the on-chain sweep does (same divergence
    kinds, same alerts) so the dashboard / runbooks don't have to fork.

    Read-only: never mutates ``portfolio`` or ``store``.
    """
    if store is None or not hasattr(store, "get_all_trades"):
        return ReconciliationReport(skipped_no_fetcher=True)
    try:
        trades = store.get_all_trades()
    except sqlite3.Error:
        logger.exception("Paper reconcile: failed to read trade history.")
        return ReconciliationReport(fetch_errors=1)

    # Build a side tracker by replaying.  Doing this every time is cheap
    # at typical trade volumes (~10k trades / day) and removes the need
    # to maintain a parallel running shadow.
    shadow = PortfolioTracker()
    shadow.reconstruct_from_trades(trades)

    # Compare token-by-token across the union of both tracker keys.
    tracked_tokens = set(portfolio.positions.keys())
    shadow_tokens = set(shadow.positions.keys())
    divergences: list[ReconciliationDivergence] = []
    for tok in tracked_tokens | shadow_tokens:
        live = portfolio.positions.get(tok)
        rep = shadow.positions.get(tok)
        live_size = live.size if live else 0.0
        rep_size = rep.size if rep else 0.0
        kind = _classify(live_size, rep_size, tolerance_shares)
        if kind is None:
            continue
        side = (live or rep).side  # at least one is set
        cid = (live or rep).condition_id
        divergences.append(ReconciliationDivergence(
            token_id=tok, condition_id=cid, side=side,
            tracked_size=live_size, onchain_size=rep_size,
            diff=rep_size - live_size, kind=kind,
        ))

    report = ReconciliationReport(
        divergences=divergences,
        positions_checked=len(tracked_tokens | shadow_tokens),
        fetch_errors=0,
    )
    if report.any_divergence:
        if alerts is not None:
            alerts.warn(
                "paper-mode tracker drift",
                divergences=len(divergences),
                kinds=sorted({d.kind for d in divergences}),
            )
        if metrics is not None and getattr(metrics, "enabled", False):
            metrics.emit(
                "paper_reconcile_drift",
                divergences=len(divergences),
                positions=len(tracked_tokens | shadow_tokens),
            )
        logger.warning(
            "Paper reconcile: %d divergence(s) across %d positions.",
            len(divergences), len(tracked_tokens | shadow_tokens),
        )
    else:
        logger.debug(
            "Paper reconcile: clean (%d positions checked).",
            len(tracked_tokens | shadow_tokens),
        )
    return report


def should_run(last_run_epoch: float, interval_minutes: float, now: float) -> bool:
    """Return True when the sweeper is due (or has never run)."""
    if interval_minutes <= 0:
        return False
    if last_run_epoch <= 0:
        return True
    return (now - last_run_epoch) >= (interval_minutes * 60.0)
