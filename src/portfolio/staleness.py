"""
Staleness monitor for open positions.

Some positions go sideways for days: the market remains nominally
active, we're still holding the leg, but nothing is happening — the
price band is unchanged, there's no catalyst, and the capital is just
sitting there unable to earn.  Left unchecked these accumulate and
quietly starve the sizer of headroom for fresher signals.

This module flags such positions so the operator (or the bot, if
opted in to auto-close) can free the capital.  Staleness is a
combination of *age* and *inactivity*:

* the position must have been open for at least
  ``cfg.position_staleness_days`` full days, AND
* the price history over that window must show a max-min range
  below ``cfg.position_staleness_price_epsilon`` (default 0.01 — one
  price-tick, approximately).

Both thresholds are opt-in (``POSITION_STALENESS_DAYS=0`` disables
the whole subsystem — the default).  Action is decoupled from
detection: ``alert`` (the safer default) just surfaces the finding;
``close`` escalates to a market-close at the current bid so the
capital is recovered.  Even in ``close`` mode the live-trading gate
still applies — a paper bot closes on paper, a live bot closes live.

No auto-close ever lies about the fill: we use the real current
price (or, lacking one, ``alert`` only, never a fabricated close).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from src.config import Config
from src.polymarket.client import PolymarketClient
from src.portfolio.tracker import PortfolioTracker, Position
from src.storage.sqlite_store import SQLiteStore
from src.utils.time_utils import parse_end_date, utc_now

logger = logging.getLogger(__name__)


# --- Data types -----------------------------------------------------------


@dataclass
class StaleReport:
    """A single stale position + the diagnostic numbers behind the flag."""

    token_id: str
    condition_id: str
    side: str
    size: float
    entry_price: float
    entry_timestamp: str
    age_days: float
    price_range: float
    samples: int
    latest_price: float | None


@dataclass
class StalenessReport:
    """Aggregate output of one staleness scan."""

    stale: list[StaleReport] = field(default_factory=list)
    closed: list[StaleReport] = field(default_factory=list)
    positions_checked: int = 0

    @property
    def any_stale(self) -> bool:
        return bool(self.stale)


# --- Core detection -------------------------------------------------------


def _position_age_days(pos: Position, now=None) -> float | None:
    """Return the age of ``pos`` in days, or ``None`` if unknown.

    We rely on ``entry_timestamp`` which is populated for both paper
    and live fills (see ``PortfolioTracker.reconstruct_from_trades``).
    Empty timestamp → ``None`` so the monitor treats the position as
    "not yet stale" rather than fabricating an age.
    """
    ts = getattr(pos, "entry_timestamp", "") or ""
    if not ts:
        return None
    dt = parse_end_date(ts)  # same parser as end-dates — ISO-8601
    if dt is None:
        return None
    now = now or utc_now()
    return max((now - dt).total_seconds() / 86400.0, 0.0)


def _price_range(prices: list[float]) -> float:
    """Range (max - min) across a price series; 0.0 when <2 samples."""
    if len(prices) < 2:
        return 0.0
    return max(prices) - min(prices)


def detect_stale_positions(
    portfolio: PortfolioTracker,
    store: SQLiteStore,
    cfg: Config,
    *,
    now=None,
) -> list[StaleReport]:
    """Identify positions that look stale under the configured thresholds.

    Pure function: reads from ``portfolio`` and ``store`` only.
    Returns an empty list when staleness monitoring is disabled
    (``position_staleness_days <= 0``) so callers can invoke it
    unconditionally.
    """
    threshold_days = float(getattr(cfg, "position_staleness_days", 0.0))
    if threshold_days <= 0:
        return []
    eps = float(getattr(cfg, "position_staleness_price_epsilon", 0.01))
    now = now or utc_now()
    cutoff = (now - timedelta(days=threshold_days)).isoformat()

    stale: list[StaleReport] = []
    for pos in portfolio.positions.values():
        age = _position_age_days(pos, now=now)
        if age is None or age < threshold_days:
            continue

        rows = store.get_price_history_since(pos.token_id, cutoff)
        prices = [float(r["price"]) for r in rows]
        rng = _price_range(prices)
        latest = prices[-1] if prices else None

        # "Stale" means *both* aged and boring.  A wildly-moving
        # position (rng > eps) is still worth holding even if old —
        # we only flag the ones where the market has effectively
        # fallen asleep.
        if rng <= eps:
            stale.append(StaleReport(
                token_id=pos.token_id, condition_id=pos.condition_id,
                side=pos.side, size=pos.size, entry_price=pos.entry_price,
                entry_timestamp=pos.entry_timestamp,
                age_days=round(age, 2), price_range=round(rng, 4),
                samples=len(prices), latest_price=latest,
            ))

    return stale


# --- Action pipeline ------------------------------------------------------


def _close_stale_position(
    report: StaleReport,
    portfolio: PortfolioTracker,
    store: SQLiteStore,
    client: PolymarketClient,
    cfg: Config,
    *,
    executor=None,
    risk_mgr=None,
    alerts=None,
    metrics=None,
    now_iso: str,
) -> bool:
    """Close a single stale position via the normal exit path.

    When ``executor`` is provided the close goes through the regular
    execution engine (so paper / live / maker-preferred all behave
    identically to a stop-loss exit).  Without an executor — e.g. in
    tests or when the caller wants a best-effort book entry only — we
    fall back to synthesising a paper fill at the latest observed
    price, logged as a separate ``exit_reason="staleness_close"`` so
    audits can tell it apart from real exits.

    Returns True if the position is gone from the portfolio after the
    call, False otherwise (so the caller can distinguish close-failed
    from not-attempted).
    """
    pos = portfolio.positions.get(report.token_id)
    if pos is None:
        return False

    # Need a price to close at — prefer the live midpoint, fall back
    # to the latest observed price we had in history.  If we have
    # neither, we *don't* fabricate: leave the alert and move on.
    try:
        live_price = client.get_price(report.token_id)
    except Exception:
        live_price = None
        logger.debug("Staleness: live price fetch failed for %s",
                     report.token_id[:12], exc_info=True)
    exit_price = live_price if live_price is not None else report.latest_price
    if exit_price is None:
        logger.warning(
            "Staleness: cannot close %s — no price available (age=%.1fd).",
            report.token_id[:12], report.age_days,
        )
        return False

    close_side = "SELL" if pos.side == "BUY" else "BUY"

    # Preferred path — real executor (respects paper/live + maker mode).
    if executor is not None:
        try:
            from src.execution.engine import OrderRequest
            try:
                spread = client.get_spread(report.token_id) or 0.0
            except Exception:
                spread = 0.0
            order = OrderRequest(
                token_id=report.token_id, condition_id=report.condition_id,
                side=close_side, size=pos.size, price=exit_price,
                strategy=pos.strategy or "", spread=spread,
                exit_reason="staleness_close",
            )
            result = executor.execute(order)
            if not result.success:
                logger.info(
                    "Staleness: executor declined close for %s (reason=%s)",
                    report.token_id[:12], getattr(result, "reason", ""),
                )
                return False
            fill_px = (
                result.fill_price if getattr(result, "fill_price", 0) > 0
                else exit_price
            )
            pnl = portfolio.close_position(report.token_id, fill_px)
            if risk_mgr is not None:
                try:
                    risk_mgr.record_realized_pnl(pnl)
                except Exception:
                    logger.debug("Staleness: risk_mgr.record_realized_pnl failed",
                                 exc_info=True)
        except Exception:
            logger.exception("Staleness: executor-path close failed for %s",
                             report.token_id[:12])
            return False
    else:
        # Fallback path — book a synthetic paper fill.  Only happens
        # when the sweeper is called outside the main loop (tests, CLI).
        pnl = portfolio.close_position(report.token_id, exit_price)
        try:
            store.insert_trade(
                order_id=f"stale:{report.token_id[-12:]}:{int(exit_price * 1000):04d}",
                token_id=report.token_id, condition_id=report.condition_id,
                side=close_side, size=pos.size, price=exit_price,
                strategy=pos.strategy or "", mode="paper_staleness",
                timestamp=now_iso, exit_reason="staleness_close",
                spread_at_entry=0.0,
            )
        except (sqlite3.Error, OSError):
            logger.debug("Staleness: trade insert failed (fallback path)", exc_info=True)
        if risk_mgr is not None:
            try:
                risk_mgr.record_realized_pnl(pnl)
            except Exception:
                logger.debug("Staleness: risk_mgr.record_realized_pnl failed",
                             exc_info=True)

    try:
        store.insert_decision(
            timestamp=now_iso, token_id=report.token_id,
            condition_id=report.condition_id,
            action="EXIT_STALENESS",
            reason=f"age={report.age_days:.1f}d range={report.price_range:.4f} (<eps)",
            strategy=pos.strategy or "", price=float(exit_price), spread=0.0,
            features={"age_days": report.age_days, "price_range": report.price_range,
                      "samples": report.samples},
        )
    except (sqlite3.Error, OSError):
        logger.debug("Staleness: decision_log insert failed", exc_info=True)

    if alerts is not None:
        alerts.warn(
            "stale position closed",
            token_id=report.token_id, condition_id=report.condition_id,
            age_days=report.age_days, price_range=report.price_range,
            exit_price=round(float(exit_price), 4),
        )
    if metrics is not None and getattr(metrics, "enabled", False):
        metrics.emit(
            "position_staleness_closed",
            token_id=report.token_id, condition_id=report.condition_id,
            age_days=report.age_days, price_range=report.price_range,
            exit_price=float(exit_price),
        )
    return report.token_id not in portfolio.positions


def process_staleness(
    portfolio: PortfolioTracker,
    store: SQLiteStore,
    client: PolymarketClient,
    cfg: Config,
    *,
    executor=None,
    risk_mgr=None,
    alerts=None,
    metrics=None,
    now=None,
    now_iso: str = "",
) -> StalenessReport:
    """Run a full staleness pass — detect, alert, optionally close.

    Respects ``cfg.position_staleness_action``:

    * ``"alert"`` (default): emit one warn-level alert per stale
      position, take no action — operator can decide.
    * ``"close"``: additionally route each stale position through
      :func:`_close_stale_position`.

    Unknown values fall back to ``alert`` (fail-safe).
    """
    report = StalenessReport()
    threshold = float(getattr(cfg, "position_staleness_days", 0.0))
    if threshold <= 0:
        return report
    report.positions_checked = len(portfolio.positions)

    stale = detect_stale_positions(portfolio, store, cfg, now=now)
    report.stale = stale
    if not stale:
        return report

    action = (getattr(cfg, "position_staleness_action", "alert") or "alert").lower()
    if action not in {"alert", "close"}:
        action = "alert"

    from src.utils.time_utils import iso_now as _iso_now
    now_iso = now_iso or _iso_now()

    for sr in stale:
        if alerts is not None:
            alerts.warn(
                "stale position detected",
                token_id=sr.token_id, condition_id=sr.condition_id,
                side=sr.side, age_days=sr.age_days,
                price_range=sr.price_range, samples=sr.samples,
                action=action,
            )
        if metrics is not None and getattr(metrics, "enabled", False):
            metrics.emit(
                "position_stale",
                token_id=sr.token_id, condition_id=sr.condition_id,
                side=sr.side, age_days=sr.age_days,
                price_range=sr.price_range, samples=sr.samples,
                action=action,
            )
        logger.info(
            "Staleness: %s %s age=%.1fd range=%.4f samples=%d action=%s",
            sr.side, sr.token_id[:12], sr.age_days, sr.price_range,
            sr.samples, action,
        )
        if action == "close":
            closed = _close_stale_position(
                sr, portfolio, store, client, cfg,
                executor=executor, risk_mgr=risk_mgr,
                alerts=alerts, metrics=metrics, now_iso=now_iso,
            )
            if closed:
                report.closed.append(sr)

    return report


# --- Scheduler helper -----------------------------------------------------


def should_run(last_run_epoch: float, interval_minutes: float, now: float) -> bool:
    """True when the staleness pass is due.

    Matches the pattern used by the DB backup and resolution sweeper
    schedulers — same semantics, so the ops mental model is one
    pattern, not three.
    """
    if interval_minutes <= 0:
        return False
    if last_run_epoch <= 0:
        return True
    return (now - last_run_epoch) >= (interval_minutes * 60.0)
