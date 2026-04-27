"""
Polymarket Trading Bot — entry point.

CLI commands:
    python -m src.main run-bot          Start the trading loop
    python -m src.main backfill-markets Download and cache markets
    python -m src.main show-portfolio   Display current portfolio
    python -m src.main show-trades      Display recent trades
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time

import click
from tabulate import tabulate

from src.backtest.engine import Backtester, load_price_histories_from_store, save_report
from src.config import Config
from src.logger import setup_logging
from src.polymarket.client import PolymarketClient
from src.polymarket.execution import ExecutionEngine, OrderRequest
from src.polymarket.market_data import MarketDataService, MarketSnapshot
from src.portfolio.tracker import PortfolioTracker, Position
from src.risk.manager import RiskManager
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import Action, BaseStrategy
from src.strategy.composite import CompositeStrategy
from src.strategy.edge_based import EdgeBasedStrategy
from src.strategy.mean_reversion import MeanReversion
from src.strategy.semantic_mispricing import SemanticMispricingStrategy
from src.strategy.simple_momentum import SimpleMomentum
from src.utils.time_utils import iso_now, utc_timestamp

logger = logging.getLogger(__name__)

# Graceful shutdown flag
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s — shutting down after current cycle.", signum)
    _shutdown = True


def _build_strategy(cfg: Config, store: SQLiteStore | None = None) -> BaseStrategy:
    if cfg.strategy == "mean_reversion":
        return MeanReversion(cfg)
    if cfg.strategy == "composite":
        return CompositeStrategy(cfg)
    if cfg.strategy == "edge_based":
        return EdgeBasedStrategy(cfg, store=store)
    if cfg.strategy == "semantic_mispricing":
        return SemanticMispricingStrategy(cfg)
    return SimpleMomentum(cfg)


def _accrue_fill_fee(
    cfg: Config,
    portfolio: PortfolioTracker,
    filled_size: float,
    fill_price: float,
    is_maker: bool = False,
) -> None:
    """Record the execution fee for a fill, if the fee model is enabled.

    No-op when ``taker_fee_bps == 0`` (default).  Keeping the call site-
    unconditional keeps the trading flow identical whether fees are on or
    off — only the fee counter changes.
    """
    if filled_size <= 0 or fill_price <= 0:
        return
    # Always compute; helper returns 0 when bps==0.
    from src.analysis.fees import compute_fee_usd
    notional = filled_size * fill_price
    portfolio.record_fee(compute_fee_usd(cfg, notional, is_maker=is_maker))


def _observe_position_price(
    portfolio: PortfolioTracker,
    cfg: Config,
    token_id: str,
    current_price: float | None,
) -> tuple[bool, float | None]:
    """Update price-observation tracking and detect zombies.

    Returns ``(is_zombie, fallback_price)``:
      * ``is_zombie`` is True the first tick the missing-price counter
        reaches the configured threshold (and on every tick after).
      * ``fallback_price`` is the position's ``last_known_price`` when
        the operator chose ``ZOMBIE_POSITION_ACTION=close`` and a
        last-known price is available; ``None`` otherwise (alert-only).

    Without this hook, a position whose price feed has gone silent
    silently bypasses every SL/TP check on every tick — capital
    trapped indefinitely.
    """
    if current_price is not None and current_price > 0:
        portfolio.record_price_observation(token_id, current_price)
        return (False, None)
    threshold = int(getattr(cfg, "zombie_position_max_missing_ticks", 0) or 0)
    misses = portfolio.record_missing_price(token_id)
    if threshold <= 0 or misses < threshold:
        return (False, None)
    pos = portfolio.positions.get(token_id)
    if pos is None:
        return (False, None)
    action = (getattr(cfg, "zombie_position_action", "alert") or "alert").lower()
    if action == "close" and pos.last_known_price > 0:
        logger.warning(
            "ZOMBIE POSITION: %s missing price for %d ticks → force-closing at last known %.4f.",
            token_id[:12], misses, pos.last_known_price,
        )
        return (True, pos.last_known_price)
    logger.warning(
        "ZOMBIE POSITION: %s missing price for %d consecutive ticks (action=%s).",
        token_id[:12], misses, action,
    )
    return (True, None)


# ---------------------------------------------------------------------------
# Bot loop
# ---------------------------------------------------------------------------

def run_loop(cfg: Config) -> None:
    """Main polling loop: fetch → evaluate → execute."""
    client = PolymarketClient(cfg)
    store = SQLiteStore(cfg.sqlite_db_path)
    portfolio = PortfolioTracker()

    # Reconstruct portfolio from trade history so positions survive restarts.
    # Capture today's realised PnL so we can seed the risk manager below —
    # otherwise a restart after a crashed day would silently reset the
    # daily-loss circuit breaker to zero.
    all_trades = store.get_all_trades()
    reconstruct_stats: dict = {}
    if all_trades:
        reconstruct_stats = portfolio.reconstruct_from_trades(all_trades)
        logger.info("Restored %d open positions from trade history.", portfolio.open_position_count())

    risk_mgr = RiskManager(cfg, portfolio)
    if reconstruct_stats:
        pnl_today = float(reconstruct_stats.get("realised_pnl_today", 0.0) or 0.0)
        if pnl_today != 0.0:
            risk_mgr.seed_daily_pnl(pnl_today)

    # Optional temporal-edge filter: allow BUYs only in UTC hours whose
    # historical win-rate clears the configured threshold.  Constructed
    # here (not inside RiskManager) so it can read from the SQLiteStore
    # closed-calibration table lazily — no new coupling on RiskManager.
    if getattr(cfg, "temporal_filter_enabled", False):
        try:
            from src.analysis.temporal_edge import TemporalFilter
            risk_mgr.temporal_filter = TemporalFilter(
                load_calibration=store.get_calibration_closed,
                min_winrate=cfg.temporal_min_winrate,
                min_samples=cfg.temporal_min_samples,
                window_days=cfg.temporal_window_days,
                ttl_seconds=cfg.temporal_refresh_seconds,
            )
            logger.info(
                "Temporal edge filter enabled (min_winrate=%.2f, min_samples=%d, window=%dd).",
                cfg.temporal_min_winrate,
                cfg.temporal_min_samples,
                cfg.temporal_window_days,
            )
        except Exception:
            logger.exception("Failed to build TemporalFilter — running without.")
            risk_mgr.temporal_filter = None

    # Optional volatility filter: reject BUYs on choppy tokens.  We
    # attach the store's price-history loader (bound method, so it
    # captures the current DB connection) — RiskManager treats a None
    # loader as "feature off".  Cold-start fail-safe (see
    # ``src/analysis/volatility.py``).
    if getattr(cfg, "volatility_filter_enabled", False):
        risk_mgr.get_price_history = store.get_price_history
        logger.info(
            "Volatility filter enabled (max_stddev=%.4f over last %d samples).",
            cfg.max_price_volatility, cfg.volatility_window,
        )

    # Optional Bayesian calibrator.  Always loads (cheap) when the
    # tracker flag is on; only influences sizing when the sizing flag
    # is *also* on — operators can run the tracker silently for a
    # week or two to accrue evidence before flipping sizing on.
    if getattr(cfg, "bayesian_calibration_enabled", False):
        try:
            from src.analysis.bayesian_calibrator import BayesianCalibrator
            risk_mgr.bayesian_calibrator = BayesianCalibrator(
                store=store,
                prior_alpha=cfg.bayesian_prior_alpha,
                prior_beta=cfg.bayesian_prior_beta,
                min_samples=cfg.bayesian_min_samples,
                min_multiplier=cfg.bayesian_min_multiplier,
            )
            # If the bayesian table is empty but we have closed
            # calibration history, seed from it once — gives the
            # posterior a head start instead of a cold uniform prior.
            if not store.get_all_bayesian_posteriors():
                closed = store.get_calibration_closed()
                if closed:
                    risk_mgr.bayesian_calibrator.seed_from_closed_trades(closed)
                    logger.info(
                        "Seeded bayesian posteriors from %d closed trades.",
                        len(closed),
                    )
            logger.info(
                "Bayesian calibration enabled (sizing=%s, min_samples=%d, min_mult=%.2f).",
                "on" if cfg.bayesian_sizing_enabled else "shadow",
                cfg.bayesian_min_samples, cfg.bayesian_min_multiplier,
            )
        except Exception:
            logger.exception("Failed to build BayesianCalibrator — running without.")
            risk_mgr.bayesian_calibrator = None
    # Optional live wallet balance gate.  Built only in live mode
    # because in paper mode the simulated balance is the source of
    # truth.  When the SDK isn't installed the fetcher returns None
    # and the provider stays None — the runtime check is fully
    # opt-out via WALLET_BALANCE_CHECK_ENABLED=false.
    if cfg.is_live and getattr(cfg, "wallet_balance_check_enabled", True):
        try:
            from src.risk.wallet_balance import (
                WalletBalanceProvider, build_clob_balance_fetcher,
            )
            fetcher = build_clob_balance_fetcher(cfg)
            if fetcher is not None:
                risk_mgr.wallet_balance_provider = WalletBalanceProvider(
                    fetcher,
                    refresh_seconds=cfg.wallet_balance_refresh_seconds,
                    min_buffer_usd=cfg.wallet_balance_min_buffer_usd,
                )
                logger.info(
                    "Wallet balance gate enabled (refresh=%.0fs, buffer=$%.2f).",
                    cfg.wallet_balance_refresh_seconds,
                    cfg.wallet_balance_min_buffer_usd,
                )
            else:
                logger.warning(
                    "Live mode but wallet balance fetcher unavailable — "
                    "runtime affordability check disabled. Verify wallet "
                    "funding manually.",
                )
        except Exception:
            logger.exception("Failed to build wallet balance provider — running without.")

    # Optional first-N live-trades autopause gate.  Only armed in live
    # mode when the threshold is positive.  Seeds the count from the
    # existing trades table so restarts don't reset the counter and
    # give the operator another "free" batch of unverified fills.
    if cfg.is_live and cfg.live_trade_autopause_threshold > 0:
        try:
            from src.risk.live_autopause import LiveTradeAutopauseGate
            initial_count = 0
            try:
                cur = store._conn.execute(
                    "SELECT COUNT(*) FROM trades "
                    "WHERE mode = 'live' AND side = 'BUY'"
                )
                row = cur.fetchone()
                initial_count = int(row[0]) if row is not None else 0
            except Exception:
                logger.debug("autopause: seed from DB failed", exc_info=True)
            risk_mgr.live_autopause_gate = LiveTradeAutopauseGate(
                threshold=cfg.live_trade_autopause_threshold,
                ack_file=cfg.live_trade_autopause_ack_file,
                initial_count=initial_count,
            )
            logger.info(
                "Live-trade autopause armed: %d/%d BUYs, ack file=%r.",
                initial_count, cfg.live_trade_autopause_threshold,
                cfg.live_trade_autopause_ack_file,
            )
        except Exception:
            logger.exception("Failed to build live-trade autopause gate — running without.")

    executor = ExecutionEngine(client, cfg, store)
    market_svc = MarketDataService(client, cfg)
    strategy = _build_strategy(cfg, store=store)

    # Optional cross-tick EMA smoother for the semantic engine's synthetic
    # fair-price.  Persistent across ticks so the moving average actually
    # accumulates state.  Default-off (alpha=0.0) so existing paper runs
    # are unaffected byte-for-byte.
    semantic_smoother = None
    if (
        getattr(cfg, "semantic_engine_enabled", False)
        and getattr(cfg, "semantic_smoothing_alpha", 0.0) > 0.0
    ):
        try:
            from src.analysis.semantic_engine.smoothing import EMASmoother
            semantic_smoother = EMASmoother(
                alpha=cfg.semantic_smoothing_alpha,
                max_keys=cfg.semantic_smoothing_max_keys,
            )
            logger.info(
                "Semantic EMA smoother enabled (alpha=%.2f, max_keys=%d).",
                cfg.semantic_smoothing_alpha, cfg.semantic_smoothing_max_keys,
            )
        except Exception:
            logger.exception("Failed to build EMA smoother — running without.")
            semantic_smoother = None

    # Alert manager: reads cfg.alert_* and wires up file + webhook sinks.
    # If nothing is configured, it's a silent no-op so we can call it
    # unconditionally below without adding latency in the default path.
    try:
        from src.utils.alerts import build_from_config
        alerts = build_from_config(cfg)
    except Exception:
        logger.exception("Alert manager setup failed — running without alerts.")
        alerts = None

    # Structured JSON metrics writer — independent of the DB so external
    # pipelines can aggregate without schema coupling.  No-op when
    # ``METRICS_FILE`` is unset.
    try:
        from src.utils.metrics import build_from_config as build_metrics
        metrics = build_metrics(cfg)
    except Exception:
        logger.exception("Metrics writer setup failed — running without metrics.")
        from src.utils.metrics import MetricsWriter
        metrics = MetricsWriter("")

    # Latency telemetry — rolling tracker for signal→submit→fill intervals.
    latency_tracker = None
    if cfg.latency_tracking_enabled:
        try:
            from src.utils.latency import LatencyTracker
            latency_tracker = LatencyTracker(window=cfg.latency_window_samples)
            logger.info(
                "Latency tracker enabled (window=%d, alert_threshold=%.0f ms).",
                cfg.latency_window_samples, cfg.latency_alert_threshold_ms,
            )
        except Exception:
            logger.exception("Latency tracker setup failed — running without.")

    # Shadow A/B(/C/…) strategies.  ``Config.shadow_strategy_list``
    # merges the legacy SHADOW_STRATEGY (singular) and the new
    # SHADOW_STRATEGIES (CSV), drops the live name, and de-duplicates.
    # Each name builds an independent ShadowRunner with its own
    # PortfolioTracker; all of them write to the same ``shadow_*``
    # tables but tagged with their own ``strategy`` column for
    # downstream filtering.
    shadow_runners: list = []
    try:
        names = cfg.shadow_strategy_list()
    except Exception:
        logger.exception("Could not parse shadow strategy list — running without.")
        names = []
    if names:
        try:
            from src.strategy.shadow_runner import ShadowRunner
            saved_strategy_env = os.environ.get("STRATEGY", strategy.name)
            for shadow_name in names:
                try:
                    os.environ["STRATEGY"] = shadow_name
                    shadow_cfg = Config()
                    shadow_strat = _build_strategy(shadow_cfg, store=store)
                    shadow_runners.append(ShadowRunner(cfg, shadow_strat))
                    logger.info(
                        "A/B shadow runner enabled: strategy=%s "
                        "(writing to shadow_* tables).",
                        shadow_strat.name,
                    )
                except Exception:
                    logger.exception(
                        "Shadow runner '%s' setup failed — skipping it.", shadow_name,
                    )
            os.environ["STRATEGY"] = saved_strategy_env
        except Exception:
            logger.exception("Shadow runners setup failed — running without.")
            shadow_runners = []

    mode_label = "PAPER" if cfg.is_paper else ("LIVE" if cfg.is_live else "PAPER (live not enabled)")
    logger.info("=== Bot started | mode=%s | strategy=%s | poll=%ds ===", mode_label, strategy.name, cfg.poll_interval)
    if alerts is not None and alerts.sinks:
        alerts.info(
            "bot started", mode=mode_label, strategy=strategy.name,
            poll_seconds=cfg.poll_interval,
        )

    problems = cfg.validate()
    for p in problems:
        logger.warning("Config warning: %s", p)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Scheduler state for DB online-backups — last epoch we ran one.
    # 0.0 → first check runs immediately.  Enforced via
    # ``src.storage.backup.should_run`` so disabled (interval<=0 or
    # empty dir) is a true no-op.
    last_db_backup = 0.0
    # Scheduler state for the resolution sweeper + staleness monitor.
    # Both run on their own cadence (minutes, not ticks) so a 60 s poll
    # interval doesn't trigger an API call + settlement pass every
    # single loop.  ``0.0`` → first check runs immediately when the
    # feature is enabled.
    last_resolution_sweep = 0.0
    last_staleness_check = 0.0
    last_reconciliation = 0.0
    last_regime_check = 0.0
    last_daily_summary_day = ""  # "YYYY-MM-DD" of the most recent dispatch
    last_decision_log_prune = 0.0

    # Build the on-chain shares fetcher once per loop: it's a no-op in
    # paper mode and when the SDK isn't importable.  Stored here so the
    # periodic sweeper reuses the same closure (cheap closure over cfg).
    reconciliation_fetcher = None
    if cfg.position_reconciliation_enabled and cfg.is_live:
        try:
            from src.portfolio.reconciliation import build_clob_shares_fetcher
            reconciliation_fetcher = build_clob_shares_fetcher(cfg)
            if reconciliation_fetcher is None:
                logger.warning(
                    "Reconciliation enabled but no fetcher available — "
                    "check py-clob-client install and credentials.",
                )
        except Exception:
            logger.exception("Failed to build reconciliation fetcher.")

    tick_count = 0
    while not _shutdown:
        # Kill switch file check
        if os.path.exists(cfg.kill_switch_file):
            logger.warning("KILL SWITCH FILE detected (%s) — shutting down.", cfg.kill_switch_file)
            if alert_mgr is not None:
                # Critical because operator action: someone (or some
                # script) explicitly halted the bot.  We want a paging
                # signal, not a silent file-only entry.
                alert_mgr.notify(
                    "critical", "kill switch detected",
                    {"file": cfg.kill_switch_file,
                     "open_positions": portfolio.open_position_count(),
                     "total_exposure": round(portfolio.total_exposure(), 2)},
                )
            break

        # Periodic SQLite online backup.  Cheap: a few-MB DB copies in
        # <100 ms under a read lock that doesn't block writes.  Runs
        # only when ``DB_BACKUP_DIR`` is set.
        if cfg.db_backup_dir:
            try:
                from src.storage.backup import should_run as _bk_should_run
                from src.storage.backup import snapshot as _bk_snapshot
                if _bk_should_run(last_db_backup, cfg.db_backup_interval_hours):
                    out = _bk_snapshot(
                        cfg.sqlite_db_path, cfg.db_backup_dir,
                        keep=cfg.db_backup_keep,
                    )
                    if out is not None:
                        last_db_backup = time.time()
                        if metrics.enabled:
                            metrics.emit("db_backup", path=str(out))
                    else:
                        if alerts is not None:
                            alerts.warn(
                                "db backup failed",
                                dir=cfg.db_backup_dir,
                                source=cfg.sqlite_db_path,
                            )
            except Exception:
                logger.exception("DB backup path crashed — continuing.")

        # Periodic decision_log retention.  Cheap when there's nothing
        # to delete; bounded once the retention window kicks in.  Skips
        # entirely when ``decision_log_retention_days <= 0``.
        if cfg.decision_log_retention_days > 0:
            interval_s = max(0.0, cfg.decision_log_prune_interval_hours) * 3600.0
            now = time.time()
            if interval_s > 0 and (now - last_decision_log_prune) >= interval_s:
                try:
                    deleted = store.prune_decision_log(cfg.decision_log_retention_days)
                    last_decision_log_prune = now
                    if metrics.enabled and deleted > 0:
                        metrics.emit("decision_log_prune", deleted=deleted)
                except Exception:
                    logger.exception("decision_log prune crashed — continuing.")

        # Periodic resolution sweeper — auto-closes positions whose
        # markets have settled so the portfolio stops carrying phantom
        # exposure.  Strictly opt-in; never places live orders (pure
        # accounting against Polymarket's reported outcome).
        if cfg.resolution_sweeper_enabled:
            try:
                from src.portfolio.resolution_sweeper import (
                    should_run as _rs_should_run,
                    sweep_resolved_positions,
                )
                if _rs_should_run(
                    last_resolution_sweep,
                    cfg.resolution_sweep_interval_minutes,
                    time.time(),
                ):
                    sweep_report = sweep_resolved_positions(
                        portfolio, client, store, cfg,
                        alerts=alerts, metrics=metrics, risk_mgr=risk_mgr,
                    )
                    last_resolution_sweep = time.time()
                    if sweep_report.any_resolved:
                        logger.info(
                            "Resolution sweep: closed %d position(s) across %d market(s).",
                            len(sweep_report.resolved), sweep_report.conditions_checked,
                        )
            except Exception:
                logger.exception("Resolution sweeper crashed — continuing.")

        # Periodic staleness monitor — flags (and optionally closes)
        # positions that have gone nowhere for N days.  Independent of
        # the resolution sweeper schedule so each subsystem has its
        # own cadence.
        if cfg.position_staleness_days > 0:
            try:
                from src.portfolio.staleness import (
                    process_staleness,
                    should_run as _sl_should_run,
                )
                if _sl_should_run(
                    last_staleness_check,
                    cfg.position_staleness_interval_minutes,
                    time.time(),
                ):
                    st_report = process_staleness(
                        portfolio, store, client, cfg,
                        executor=executor, risk_mgr=risk_mgr,
                        alerts=alerts, metrics=metrics,
                    )
                    last_staleness_check = time.time()
                    if st_report.any_stale:
                        logger.info(
                            "Staleness: %d flagged, %d closed.",
                            len(st_report.stale), len(st_report.closed),
                        )
            except Exception:
                logger.exception("Staleness monitor crashed — continuing.")

        # Periodic reconciliation sweep — compares tracked positions
        # with on-chain shares and alerts on divergences above a
        # tolerance.  Report-only; never mutates portfolio state.
        if cfg.position_reconciliation_enabled and reconciliation_fetcher is not None:
            try:
                from src.portfolio.reconciliation import (
                    reconcile_positions,
                    should_run as _rc_should_run,
                )
                if _rc_should_run(
                    last_reconciliation,
                    cfg.position_reconciliation_interval_minutes,
                    time.time(),
                ):
                    rc_report = reconcile_positions(
                        portfolio, reconciliation_fetcher,
                        tolerance_shares=cfg.position_reconciliation_tolerance_shares,
                        alerts=alerts, metrics=metrics,
                    )
                    last_reconciliation = time.time()
                    if rc_report.any_divergence:
                        logger.warning(
                            "Reconciliation: %d divergence(s) across %d position(s) checked.",
                            len(rc_report.divergences), rc_report.positions_checked,
                        )
            except Exception:
                logger.exception("Reconciliation sweeper crashed — continuing.")

        # Periodic regime-shift detector — reads recent price history
        # for each tracked token, asks the detector whether the
        # universe is moving together, and alerts (+ optionally
        # auto-pauses BUYs) when a shift is observed.
        if cfg.regime_detector_enabled:
            try:
                from src.risk.regime import (
                    detect_regime_shift, returns_from_price_history,
                )
                # Reuse the staleness sweeper's "should_run" shape.
                now = time.time()
                if (
                    cfg.regime_detector_interval_minutes > 0
                    and (
                        last_regime_check <= 0
                        or (now - last_regime_check)
                        >= cfg.regime_detector_interval_minutes * 60.0
                    )
                ):
                    # Only look at tokens the bot actually tracked this
                    # session (open positions + recent price pushes).
                    active = {p.token_id for p in portfolio.positions.values()}
                    # Also include tokens that have price history — they
                    # were seen at least once and form the "universe".
                    cur = store._conn.execute(
                        "SELECT DISTINCT token_id FROM price_history "
                        "ORDER BY id DESC LIMIT 200"
                    )
                    for row in cur.fetchall():
                        active.add(row["token_id"])

                    histories = {
                        tok: store.get_price_history(
                            tok, limit=cfg.regime_lookback_points + 1,
                        )
                        for tok in active
                    }
                    rets = returns_from_price_history(
                        histories, lookback_points=cfg.regime_lookback_points,
                    )
                    verdict = detect_regime_shift(
                        rets,
                        move_threshold=cfg.regime_move_threshold,
                        fraction_threshold=cfg.regime_fraction_threshold,
                        min_universe=cfg.regime_min_universe,
                    )
                    last_regime_check = now

                    if verdict.shift_detected:
                        if alerts is not None:
                            alerts.warn(
                                "regime shift detected",
                                universe=verdict.universe_size,
                                big_move_fraction=round(verdict.big_move_fraction, 4),
                                directional_bias=round(verdict.directional_bias, 4),
                                reason=verdict.reason,
                            )
                        if cfg.regime_auto_pause_on_shift and not risk_mgr.regime_paused:
                            risk_mgr.regime_paused = True
                            risk_mgr.regime_pause_reason = verdict.reason
                            logger.warning(
                                "Regime auto-pause engaged: %s", verdict.reason,
                            )
                    else:
                        if risk_mgr.regime_paused:
                            logger.info(
                                "Regime auto-pause released: %s", verdict.reason,
                            )
                            if alerts is not None:
                                alerts.info(
                                    "regime auto-pause released",
                                    reason=verdict.reason,
                                )
                        risk_mgr.regime_paused = False
                        risk_mgr.regime_pause_reason = ""
                    if metrics.enabled:
                        metrics.emit(
                            "regime_check",
                            shift=verdict.shift_detected,
                            universe=verdict.universe_size,
                            big_move_fraction=verdict.big_move_fraction,
                            directional_bias=verdict.directional_bias,
                        )
            except Exception:
                logger.exception("Regime detector crashed — continuing.")

        # Daily summary scheduler — fires once per UTC day, shortly
        # after midnight, covering the *previous* day.  Idempotent
        # (tracked in-memory), no backfill on restart.
        if cfg.daily_summary_enabled:
            try:
                from datetime import datetime as _dt
                from datetime import timedelta as _td
                from datetime import timezone as _tz

                from src.analysis.daily_summary import (
                    build_daily_summary, format_summary,
                )

                now_utc = _dt.now(_tz.utc)
                today_iso = now_utc.date().isoformat()
                # Fire only after 00:05 UTC to avoid race with in-flight writes
                # from the boundary second, and only once per day.
                if (
                    now_utc.hour > 0 or now_utc.minute >= 5
                ) and last_daily_summary_day != today_iso:
                    yday = (now_utc - _td(days=1)).date()
                    trades_all = store.get_all_trades()
                    cal_all = store.get_calibration_closed()
                    summary = build_daily_summary(trades_all, cal_all, day=yday)
                    logger.info("Daily summary:\n%s", format_summary(summary))
                    if alerts is not None:
                        alerts.info(
                            f"daily summary {summary.day}",
                            **summary.to_dict(),
                        )
                    if metrics.enabled:
                        metrics.emit("daily_summary", **summary.to_dict())
                    last_daily_summary_day = today_iso
            except Exception:
                logger.exception("Daily summary scheduler crashed — continuing.")

        # Circuit breaker check
        if risk_mgr.is_circuit_breaker_active:
            logger.warning("Circuit breaker active (daily loss $%.2f). Skipping tick, monitoring only.", abs(risk_mgr.daily_pnl))
            if alerts is not None:
                alerts.critical(
                    "circuit breaker active",
                    daily_loss_usd=round(abs(risk_mgr.daily_pnl), 2),
                    max_daily_loss_usd=cfg.max_daily_loss,
                )
            # Still check SL/TP on existing positions even when circuit breaker is active
            _check_exits_only(risk_mgr, executor, portfolio, store, client, cfg)
        else:
            try:
                _tick(
                    market_svc, strategy, risk_mgr, executor, portfolio,
                    store, client, cfg, semantic_smoother=semantic_smoother,
                    metrics=metrics, latency_tracker=latency_tracker,
                    shadow_runners=shadow_runners,
                )
            except Exception as exc:
                logger.exception("Error in bot tick — will retry next cycle.")
                if alerts is not None:
                    alerts.warn(
                        "tick error", error=type(exc).__name__, message=str(exc),
                    )
                if metrics.enabled:
                    metrics.emit("tick_error", error=type(exc).__name__,
                                 message=str(exc))

        tick_count += 1
        # Export bot state for dashboard every tick
        _export_bot_state(cfg, portfolio, risk_mgr, strategy, tick_count, client, store, shadow_runners=shadow_runners)

        logger.debug("Sleeping %d s …", cfg.poll_interval)
        for _ in range(cfg.poll_interval):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("Bot stopped.")
    client.close()
    store.close()


def _decide_exit_reason(
    pos: Position,
    current_price: float,
    cfg: Config,
    risk_mgr: RiskManager,
    store: SQLiteStore,
    client: PolymarketClient,
) -> str:
    """Pure decision: return the exit reason string for this position, or ''.

    Single source of truth shared by ``_check_exits_only`` and the
    ``_tick`` exit-scan loop.  Order matters: SL > TP > edge_flip.
    """
    if risk_mgr.check_stop_loss(pos.entry_price, current_price):
        return "stop_loss"
    if risk_mgr.check_take_profit(pos.entry_price, current_price):
        return "take_profit"
    if cfg.exit_on_edge_flip and pos.strategy == "edge_based":
        try:
            from src.analysis.edge import estimate_edge
            history = store.get_price_history(pos.token_id, limit=30)
            if len(history) >= 4:
                spread = client.get_spread(pos.token_id) or 0.0
                est = estimate_edge(
                    price=current_price, price_history=history, spread=spread,
                )
                threshold = cfg.exit_edge_flip_threshold
                flipped = (
                    (pos.side == "BUY" and est.edge < -threshold) or
                    (pos.side == "SELL" and est.edge > threshold)
                )
                if flipped and est.edge_confidence > cfg.exit_edge_flip_min_confidence:
                    return "edge_flip"
        except Exception:
            logger.debug(
                "Edge-flip check failed for %s", pos.token_id[:12], exc_info=True,
            )
    return ""


def _close_position_and_record(
    *,
    token_id: str,
    exit_reason: str,
    current_price: float,
    portfolio: PortfolioTracker,
    executor: ExecutionEngine,
    risk_mgr: RiskManager,
    store: SQLiteStore,
    client: PolymarketClient,
    cfg: Config,
    tick_ts: str,
    reason_suffix: str = "",
) -> None:
    """Execute the close, accrue fees, record the trade + decision.

    Shared between the circuit-breaker-only path and the regular tick.
    Silently no-ops when the position has been closed already (a partial
    fill earlier in the tick can race the second loop in _tick).
    """
    pos = portfolio.positions.get(token_id)
    if pos is None:
        return
    close_side = "SELL" if pos.side == "BUY" else "BUY"
    current_spread = client.get_spread(token_id) or 0.0
    order = OrderRequest(
        token_id=token_id, condition_id=pos.condition_id,
        side=close_side, size=pos.size, price=current_price,
        strategy=pos.strategy, spread=current_spread, exit_reason=exit_reason,
    )
    result = executor.execute(order)
    if not result.success:
        return
    entry_price = pos.entry_price
    fill_size = result.filled_size if result.filled_size > 0 else pos.size
    fill_px = result.fill_price if result.fill_price > 0 else current_price
    _accrue_fill_fee(cfg, portfolio, fill_size, fill_px)
    pnl = portfolio.close_position(token_id, current_price)
    risk_mgr.record_realized_pnl(pnl)
    return_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
    reason_text = f"PnL={pnl:.4f}"
    if reason_suffix:
        reason_text = f"{reason_text} ({reason_suffix})"
    store.insert_decision(
        timestamp=tick_ts, token_id=token_id, condition_id=pos.condition_id,
        action=f"EXIT_{exit_reason.upper()}", reason=reason_text,
        strategy=pos.strategy, price=current_price, spread=current_spread,
    )
    store.update_calibration_exit(
        token_id=token_id,
        exit_timestamp=tick_ts,
        exit_price=current_price,
        exit_reason=exit_reason,
        pnl=pnl,
        return_pct=return_pct,
    )
    if risk_mgr.bayesian_calibrator is not None:
        try:
            risk_mgr.bayesian_calibrator.record_outcome(
                strategy=pos.strategy, won=pnl > 0,
            )
        except Exception:
            logger.exception("Bayesian record_outcome failed — skipped.")


def _check_exits_only(
    risk_mgr: RiskManager,
    executor: ExecutionEngine,
    portfolio: PortfolioTracker,
    store: SQLiteStore,
    client: PolymarketClient,
    cfg: Config,
) -> None:
    """Check SL/TP/edge-flip on existing positions without scanning for new entries.

    Used when the circuit breaker is active — we still want to close
    losing positions, but we don't want to open new ones.
    """
    tick_ts = iso_now()
    for token_id, pos in list(portfolio.positions.items()):
        current_price = client.get_price(token_id)
        is_zombie, fallback_price = _observe_position_price(
            portfolio, cfg, token_id, current_price,
        )
        if current_price is None:
            if is_zombie and fallback_price is not None:
                current_price = fallback_price
                exit_reason = "zombie_close"
            else:
                continue
        else:
            exit_reason = _decide_exit_reason(
                pos, current_price, cfg, risk_mgr, store, client,
            )
        if not exit_reason:
            continue
        _close_position_and_record(
            token_id=token_id,
            exit_reason=exit_reason,
            current_price=current_price,
            portfolio=portfolio,
            executor=executor,
            risk_mgr=risk_mgr,
            store=store,
            client=client,
            cfg=cfg,
            tick_ts=tick_ts,
            reason_suffix="circuit_breaker_mode",
        )


_SHADOW_RUNNER_ZERO_BLOCK = {
    "enabled": False,
    "strategy": "",
    "open_positions": 0,
    "realised_pnl": 0.0,
    "unrealised_pnl": 0.0,
    "fees_paid": 0.0,
    "win_rate": {"wins": 0, "losses": 0, "breakeven": 0,
                 "total_closed": 0, "win_rate": 0.0},
    "risk_metrics": {"n": 0, "sharpe_annualized": 0.0,
                     "sortino_annualized": 0.0, "max_drawdown": 0.0,
                     "calmar": 0.0, "psr_vs_zero": 0.0,
                     "tail_window": 0},
}


def _build_shadow_runner_block(runner, store, client: PolymarketClient, cfg: Config) -> dict:
    """Per-runner observability block (one entry of the ``runners`` list)."""
    block = dict(_SHADOW_RUNNER_ZERO_BLOCK)
    block["win_rate"] = dict(_SHADOW_RUNNER_ZERO_BLOCK["win_rate"])
    block["risk_metrics"] = dict(_SHADOW_RUNNER_ZERO_BLOCK["risk_metrics"])
    if runner is None or store is None:
        return block
    try:
        st = runner.state(client.get_price)
        block.update({
            "enabled": st.enabled,
            "strategy": st.strategy,
            "open_positions": st.open_positions,
            "realised_pnl": round(st.realised_pnl, 4),
            "unrealised_pnl": round(st.unrealised_pnl, 4),
            "fees_paid": round(st.fees_paid, 4),
        })
        block["win_rate"] = store.compute_shadow_win_rate(strategy=st.strategy)
        from src.analysis.risk_metrics import (
            probabilistic_sharpe_ratio, returns_summary,
        )
        tail = int(getattr(cfg, "risk_metrics_tail_window", 200) or 0)
        rets = store.get_shadow_closed_returns(
            limit=tail if tail > 0 else None,
            strategy=st.strategy,
        )
        if rets:
            rs = returns_summary(rets, periods_per_year=252)
            psr = probabilistic_sharpe_ratio(rets, periods_per_year=252)
            block["risk_metrics"] = {
                "n": rs.n,
                "sharpe_annualized": round(rs.sharpe_annualized, 4),
                "sortino_annualized": round(rs.sortino_annualized, 4),
                "max_drawdown": round(rs.max_drawdown, 4),
                "calmar": round(rs.calmar, 4),
                "psr_vs_zero": round(psr, 4),
                "tail_window": tail,
            }
    except Exception:
        logger.debug("Shadow runner block failed", exc_info=True)
    return block


def _build_shadow_state_block(
    shadow_runners,
    store,
    client: PolymarketClient,
    cfg: Config,
) -> dict:
    """Compose the ``shadow`` block for ``bot_state.json``.

    Two-level shape:
      * Top-level keys mirror the *first* runner so existing dashboard
        code that reads ``shadow.enabled`` / ``shadow.strategy`` /
        ``shadow.realised_pnl`` keeps working unchanged.
      * ``runners`` is a list of per-runner blocks for the multi-A/B
        view (zero or more entries).

    Returns a well-formed dict (never None) so the dashboard can
    render unconditionally.
    """
    runners = list(shadow_runners or [])
    runners_blocks = [
        _build_shadow_runner_block(r, store, client, cfg) for r in runners
    ]
    if runners_blocks:
        head = dict(runners_blocks[0])
    else:
        head = dict(_SHADOW_RUNNER_ZERO_BLOCK)
        head["win_rate"] = dict(_SHADOW_RUNNER_ZERO_BLOCK["win_rate"])
        head["risk_metrics"] = dict(_SHADOW_RUNNER_ZERO_BLOCK["risk_metrics"])
    head["n_runners"] = len(runners_blocks)
    head["runners"] = runners_blocks
    return head


def _export_bot_state(
    cfg: Config,
    portfolio: PortfolioTracker,
    risk_mgr: RiskManager,
    strategy: BaseStrategy,
    tick_count: int,
    client: PolymarketClient,
    store: SQLiteStore | None = None,
    shadow_runners=None,
) -> None:
    """Write a JSON file with current bot state for the dashboard to consume.

    The dashboard treats this file as the single source of truth — it
    never recomputes PnL or win-rate from raw trades.  All derived
    metrics (realised, unrealised, fees, win rate, open losers count)
    are computed here and exported as final numbers, so the dashboard
    cannot diverge from the backend.

    Written atomically via ``os.replace`` so a concurrent reader never
    sees a half-written JSON document.
    """
    try:
        import os, tempfile
        positions_data = []
        open_losers = 0
        unrealised_total = 0.0
        for pos in portfolio.positions.values():
            current_price = client.get_price(pos.token_id)
            unrealised = pos.unrealised_pnl(current_price) if current_price else 0.0
            unrealised_total += unrealised
            if current_price is not None and unrealised < 0:
                open_losers += 1
            positions_data.append({
                "token_id": pos.token_id[:16],
                "condition_id": pos.condition_id[:16],
                "side": pos.side,
                "size": round(pos.size, 4),
                "entry_price": round(pos.entry_price, 4),
                "current_price": round(current_price, 4) if current_price else None,
                "unrealised_pnl": round(unrealised, 4) if current_price else None,
                "strategy": pos.strategy,
            })

        # Honest win-rate from closed trades only.  An empty store gives
        # an all-zero block — no inflated 100% from a handful of paper
        # trades and definitely no synthetic balance.
        win_stats = store.compute_win_rate() if store is not None else {
            "wins": 0, "losses": 0, "breakeven": 0,
            "total_closed": 0, "win_rate": 0.0,
        }

        # Risk-adjusted performance over closed trades.  Computed on a
        # rolling tail (default 200) so the figure tracks the recent
        # regime instead of being dragged forever by the first month.
        # Empty / tiny histories degrade to all-zero — never NaN.
        risk_block: dict = {
            "n": 0, "sharpe_annualized": 0.0, "sortino_annualized": 0.0,
            "max_drawdown": 0.0, "calmar": 0.0, "psr_vs_zero": 0.0,
            "tail_window": 0,
        }
        if store is not None:
            try:
                from src.analysis.risk_metrics import (
                    probabilistic_sharpe_ratio, returns_summary,
                )
                tail = int(getattr(cfg, "risk_metrics_tail_window", 200) or 0)
                returns = store.get_closed_trade_returns(
                    limit=tail if tail > 0 else None,
                )
                if returns:
                    rs = returns_summary(returns, periods_per_year=252)
                    psr0 = probabilistic_sharpe_ratio(
                        returns, benchmark_sharpe=0.0, periods_per_year=252,
                    )
                    risk_block = {
                        "n": rs.n,
                        "sharpe_annualized": round(rs.sharpe_annualized, 4),
                        "sortino_annualized": round(rs.sortino_annualized, 4),
                        "max_drawdown": round(rs.max_drawdown, 4),
                        "calmar": round(rs.calmar, 4),
                        "psr_vs_zero": round(psr0, 4),
                        "tail_window": tail,
                    }
            except Exception:
                # Risk-metrics path is purely informational; never let
                # a bug here mask a healthy bot tick.
                logger.debug("risk_metrics block failed", exc_info=True)

        realised = portfolio.realised_pnl
        fees = portfolio.fees_paid
        net_pnl_after_fees = realised + unrealised_total - fees

        state = {
            "timestamp": iso_now(),
            "heartbeat_unix": int(time.time()),
            "tick_count": tick_count,
            "mode": "paper" if cfg.is_paper else "live",
            "strategy": strategy.name,
            "circuit_breaker_active": risk_mgr.is_circuit_breaker_active,
            "daily_pnl": round(risk_mgr.daily_pnl, 4),
            "portfolio": {
                "open_positions": portfolio.open_position_count(),
                "open_losers": open_losers,
                "total_exposure": round(portfolio.total_exposure(), 2),
                "realised_pnl": round(realised, 4),
                "unrealised_pnl": round(unrealised_total, 4),
                "fees_paid": round(fees, 4),
                "net_pnl": round(realised + unrealised_total, 4),
                "net_pnl_after_fees": round(net_pnl_after_fees, 4),
            },
            "win_rate": win_stats,
            "risk_metrics": risk_block,
            "shadow": _build_shadow_state_block(shadow_runners, store, client, cfg),
            "config": {
                "max_position_size": cfg.max_position_size,
                "max_total_exposure": cfg.max_total_exposure,
                "max_open_positions": cfg.max_open_positions,
                "stop_loss_pct": cfg.stop_loss_pct,
                "take_profit_pct": cfg.take_profit_pct,
                "max_spread": cfg.max_spread,
                "max_daily_loss": cfg.max_daily_loss,
                "min_price": cfg.min_price,
                "max_price": cfg.max_price,
                "poll_interval": cfg.poll_interval,
            },
            "positions": positions_data,
        }
        # Atomic write: write to a sibling temp file in the same dir,
        # then ``os.replace`` (atomic on POSIX) so a concurrent reader
        # either sees the previous full file or the new full file —
        # never a half-written one.
        target = "bot_state.json"
        tmp_dir = os.path.dirname(os.path.abspath(target)) or "."
        with tempfile.NamedTemporaryFile(
            "w", dir=tmp_dir, prefix=".bot_state.", suffix=".json.tmp",
            delete=False,
        ) as f:
            json.dump(state, f, indent=2)
            tmp_path = f.name
        os.replace(tmp_path, target)
    except Exception:
        logger.debug("Failed to export bot state JSON.", exc_info=True)


def _tick(
    market_svc: MarketDataService,
    strategy: BaseStrategy,
    risk_mgr: RiskManager,
    executor: ExecutionEngine,
    portfolio: PortfolioTracker,
    store: SQLiteStore,
    client: PolymarketClient,
    cfg: Config,
    *,
    semantic_smoother=None,
    metrics=None,
    latency_tracker=None,
    shadow_runners=None,
) -> None:
    """One iteration of the bot loop."""

    tick_start = utc_timestamp()
    tick_ts = iso_now()

    # Shadow runners — opt-in 2nd/3rd/Nth strategies that observe the
    # same market data on every tick and persist their decisions to
    # the ``shadow_*`` tables.  Exits run before live so a slow-shadow
    # bug can never delay a live SL/TP.  Each runner is independent;
    # one crashing never affects another or the live path.
    runners_list = list(shadow_runners or [])
    for runner in runners_list:
        try:
            runner.evaluate_exits(store, client.get_price)
        except Exception:
            logger.debug(
                "Shadow exits crashed for %s — continuing.",
                getattr(runner.strategy, "name", "?"), exc_info=True,
            )

    # 1. Check stop-loss / take-profit / edge-flip on existing positions.
    # Build the list first, close in a second pass — keeps the close
    # loop from mutating ``portfolio.positions`` while we iterate it.
    tokens_to_close: list[tuple[str, str, float]] = []  # (token_id, reason, mark_price)
    for token_id, pos in list(portfolio.positions.items()):
        current_price = client.get_price(token_id)
        is_zombie, fallback_price = _observe_position_price(
            portfolio, cfg, token_id, current_price,
        )
        if current_price is None:
            if is_zombie and fallback_price is not None:
                logger.warning(
                    "Force-closing zombie position %s at last known %.4f.",
                    token_id[:12], fallback_price,
                )
                tokens_to_close.append((token_id, "zombie_close", fallback_price))
            else:
                logger.debug("No price for position %s — skipping SL/TP check.", token_id[:12])
            continue
        reason = _decide_exit_reason(pos, current_price, cfg, risk_mgr, store, client)
        if reason:
            logger.info(
                "%s triggered for %s (entry=%.4f, current=%.4f)",
                reason.replace("_", " ").title(),
                token_id[:12], pos.entry_price, current_price,
            )
            tokens_to_close.append((token_id, reason, current_price))

    for token_id, exit_reason, mark_price in tokens_to_close:
        pos = portfolio.positions.get(token_id)
        if pos is None:
            continue
        # Prefer the mark we already validated → live → last-known →
        # entry price.  Falling back to entry would silently zero out
        # realised PnL on a zombie close.
        current_price = (
            mark_price
            or client.get_price(token_id)
            or (pos.last_known_price if pos.last_known_price > 0 else None)
            or pos.entry_price
        )
        _close_position_and_record(
            token_id=token_id,
            exit_reason=exit_reason,
            current_price=current_price,
            portfolio=portfolio,
            executor=executor,
            risk_mgr=risk_mgr,
            store=store,
            client=client,
            cfg=cfg,
            tick_ts=tick_ts,
        )

    # 2. Fetch market snapshots
    snapshots = market_svc.fetch_and_filter()
    logger.info("Evaluating %d market snapshots.", len(snapshots))

    # 2a. Shadow runner entries.  Runs *after* fetch so each runner
    # sees the exact same filtered universe the live strategy will
    # see, but before any live execution so a long shadow pass can
    # never delay an order placement (it can't — shadow never places
    # one).  Each runner is independent.
    if runners_list:
        loader = lambda tid: store.get_price_history(tid, limit=50)
        for runner in runners_list:
            try:
                runner.evaluate_entries(snapshots, store, price_history_loader=loader)
            except Exception:
                logger.debug(
                    "Shadow entries crashed for %s — continuing.",
                    getattr(runner.strategy, "name", "?"), exc_info=True,
                )

    # 2b. Opt-in negative-risk arb scan.  READ-ONLY observer — never
    # trades.  Runs after filtering so it sees the same markets the bot
    # would trade, but we don't gate anything on its findings.
    if getattr(cfg, "arb_detector_enabled", False):
        try:
            from src.analysis.arb_detector import scan_and_record
            scan_and_record(
                snapshots, store, tick_ts,
                min_discount=cfg.arb_min_discount,
                min_legs_liquidity=cfg.arb_min_legs_liquidity,
            )
        except Exception:
            # Non-fatal: a broken detector must never break the tick loop.
            logger.exception("Arb detector crashed — skipping this tick.")

    # 2c. Opt-in Semantic Mispricing Engine scan.  READ-ONLY observer when
    # STRATEGY is something else; feeds the strategy adapter when
    # STRATEGY=semantic_mispricing.  Gated entirely behind
    # ``semantic_engine_enabled`` — default off, zero cost when disabled.
    semantic_mispricings: list = []
    if getattr(cfg, "semantic_engine_enabled", False):
        try:
            from src.analysis.semantic_engine import scan_and_record as semantic_scan
            from src.analysis.semantic_engine.relations import RelationClassifierConfig
            from src.analysis.semantic_engine.scoring import ScoringConfig

            rel_cfg = RelationClassifierConfig(
                disable_textual=not cfg.semantic_use_textual_links,
                disable_temporal=not cfg.semantic_use_temporal_links,
                disable_inverse=not cfg.semantic_use_inverse_links,
            )
            sc_cfg = ScoringConfig(
                max_spread=cfg.semantic_max_spread,
                min_liquidity=cfg.semantic_min_liquidity,
                taker_fee_bps=cfg.taker_fee_bps,
                maker_fee_bps=cfg.maker_fee_bps,
                safety_margin_bps=cfg.semantic_safety_margin_bps,
            )
            prefer_maker = cfg.semantic_execution_preference == "maker"
            semantic_mispricings = semantic_scan(
                snapshots, store, tick_ts,
                relation_cfg=rel_cfg,
                scoring_cfg=sc_cfg,
                min_relation_confidence=cfg.semantic_min_relation_confidence,
                min_net_edge=cfg.semantic_min_net_edge,
                min_signal_score=cfg.semantic_min_signal_score,
                max_related_per_target=cfg.semantic_max_related_markets,
                min_sibling_liquidity=cfg.semantic_min_sibling_liquidity,
                prefer_maker=prefer_maker,
                smoother=semantic_smoother,
            )
            if semantic_mispricings:
                logger.info(
                    "Semantic engine: %d mispricings (mode=%s).",
                    len(semantic_mispricings), cfg.semantic_engine_mode,
                )
        except Exception:
            # Non-fatal: a broken engine must never break the tick loop.
            logger.exception("Semantic engine crashed — skipping this tick.")
            semantic_mispricings = []

    # Feed the strategy adapter if it subscribes to this context.  Strategies
    # that don't care (most) simply don't implement the method.
    if hasattr(strategy, "set_semantic_context"):
        try:
            strategy.set_semantic_context(semantic_mispricings)
        except Exception:
            logger.exception("Failed to inject semantic context — continuing.")

    # Semantic engine 'disabled' short-circuit: if the strategy is
    # semantic_mispricing and mode=='disabled', force HOLD for this tick
    # by clearing the context.  This gives operators a runtime kill switch
    # separate from the binary enabled flag (useful for A/B live tests).
    if (
        getattr(cfg, "semantic_engine_mode", "shadow") == "disabled"
        and hasattr(strategy, "clear_semantic_context")
    ):
        strategy.clear_semantic_context()

    # 3. Evaluate strategy on each snapshot
    signals_generated = 0
    risk_rejections = 0
    trades_executed = 0
    skip_already_open = 0
    skip_no_price = 0
    skip_insufficient_history = 0
    skip_hold = 0

    for snap in snapshots:
        if snap.token_id in portfolio.positions:
            skip_already_open += 1
            continue  # already have a position

        # Record price (with spread for realistic backtest slippage)
        if snap.price is not None:
            store.insert_price(snap.token_id, snap.price, tick_ts, spread=snap.spread or 0.0)
        else:
            skip_no_price += 1
            continue

        history = store.get_price_history(snap.token_id)
        sig = strategy.evaluate(snap, history)
        t_signal_ts = time.time()

        if sig.action == Action.HOLD:
            # Split HOLD reasons for diagnostics — warmup vs no-signal
            reason_lc = sig.reason.lower()
            if "not enough history" in reason_lc or "history" in reason_lc and "len" in str(sig.features):
                skip_insufficient_history += 1
            else:
                skip_hold += 1
            continue

        signals_generated += 1

        # 3b. Confidence gate — skip if below minimum
        if sig.confidence < cfg.min_confidence_for_trade:
            logger.debug("Skipping %s: confidence %.3f below min %.3f",
                         snap.token_id[:12], sig.confidence, cfg.min_confidence_for_trade)
            skip_hold += 1
            continue

        # 4. Risk check (dynamic sizing based on confidence + liquidity + edge)
        # Extract signed edge from signal features if the strategy publishes it
        # (edge-based strategy does; momentum/mean-rev do not).
        sig_edge = None
        try:
            raw_edge = sig.features.get("edge") if sig.features else None
            if raw_edge is not None:
                sig_edge = float(raw_edge)
        except (TypeError, ValueError):
            sig_edge = None

        # Inject condition_id into the signal features so the risk manager's
        # paired-leg check can net opposing BUYs of the same event.  Strategies
        # don't need to know the event structure themselves.
        if sig.features is None:
            sig.features = {}
        sig.features.setdefault("condition_id", snap.condition_id)

        # Opt-in: pre-fetch a cheap book analysis to get fillable depth so
        # ``compute_position_size`` can cap the base notional by real depth
        # rather than by the flat ``liquidity`` field.  Skipped when the
        # feature is off so we don't pay the extra API call.
        book_depth_usd = 0.0
        if getattr(cfg, "max_book_depth_fraction", 0.0) > 0:
            try:
                depth_ba = client.get_book_analysis(snap.token_id, fill_size_usd=50.0)
                if depth_ba is not None:
                    book_depth_usd = (
                        depth_ba.ask_depth_5pct if sig.action == Action.BUY
                        else depth_ba.bid_depth_5pct
                    )
            except Exception:  # pragma: no cover — defensive: API glitches
                book_depth_usd = 0.0

        proposed_size = risk_mgr.compute_position_size(
            price=snap.price or 0,
            confidence=sig.confidence,
            liquidity=snap.liquidity,
            edge=sig_edge,
            book_depth_usd=book_depth_usd,
            strategy=cfg.strategy,
            end_date=snap.end_date or "",
        )

        # Sizing-trace meta — captures *why* the proposed_size came out the
        # way it did, for post-hoc reproducibility ("given this logged
        # decision, can I rebuild the multipliers that produced it?").
        # Each field is only emitted when its corresponding feature is
        # actually enabled, so disabled paths stay quiet in the log.
        sizing_trace: dict[str, float | bool] = {
            "proposed_size": round(proposed_size, 6),
        }
        if getattr(cfg, "sizing_capital_efficiency_enabled", False) and snap.end_date:
            from src.utils.time_utils import capital_efficiency_factor
            sizing_trace["capital_efficiency_factor"] = round(
                capital_efficiency_factor(
                    snap.end_date,
                    target_days=cfg.sizing_capital_efficiency_target_days,
                    min_factor=cfg.sizing_capital_efficiency_min_factor,
                ),
                4,
            )
        if getattr(cfg, "sizing_kelly_proper", False) and sig_edge is not None:
            from src.analysis.kelly import kelly_fraction as _kelly_f
            try:
                sizing_trace["kelly_f_star"] = round(
                    _kelly_f(price=snap.price or 0.0, edge=sig_edge), 4,
                )
            except Exception:
                pass
        if (
            getattr(cfg, "bayesian_sizing_enabled", False)
            and risk_mgr.bayesian_calibrator is not None
        ):
            try:
                sizing_trace["bayes_size_mult"] = round(
                    risk_mgr.bayesian_calibrator.size_multiplier(strategy.name), 4,
                )
            except Exception:
                pass
        if (
            getattr(cfg, "temporal_filter_enabled", False)
            and risk_mgr.temporal_filter is not None
        ):
            try:
                sizing_trace["hour_allowed"] = bool(
                    risk_mgr.temporal_filter.is_hour_allowed()
                )
            except Exception:
                pass

        verdict = risk_mgr.check(snap.token_id, sig, proposed_size, snap.price or 0, spread=snap.spread or 0.0, category=snap.category)
        if not verdict.allowed:
            risk_rejections += 1
            logger.debug("Risk denied for %s: %s", snap.token_id[:12], verdict.reason)
            store.insert_decision(
                timestamp=tick_ts, token_id=snap.token_id, condition_id=snap.condition_id,
                action="RISK_REJECTED", reason=verdict.reason,
                strategy=strategy.name, confidence=sig.confidence,
                price=snap.price or 0, spread=snap.spread or 0,
                signal_detail=sig.reason, risk_detail=verdict.reason,
                features={**(sig.features or {}), **sizing_trace},
            )
            continue

        # 5. Fetch full book analysis for the winning signal so execution uses
        #    the **realistic VWAP** (not just top-of-book), which accounts for
        #    slippage when our size exceeds depth at the best level.
        #
        #    Fall back to midpoint + half-spread if the book fetch fails.
        exec_price = snap.price or 0.0
        exec_spread = snap.spread or 0.0
        is_book_price = False
        slippage_pct = 0.0
        book_imbalance = 0.0

        # Size in USD for book VWAP calculation
        size_usd = max(verdict.adjusted_size * (snap.price or 0.0), 1.0)
        ba = client.get_book_analysis(snap.token_id, fill_size_usd=size_usd)
        if ba is not None and ba.best_bid > 0 and ba.best_ask > 0:
            real_spread = ba.spread
            if sig.action == Action.BUY:
                exec_price = ba.vwap_buy if ba.vwap_buy > 0 else ba.best_ask
                slippage_pct = ba.slippage_buy_pct
            else:
                exec_price = ba.vwap_sell if ba.vwap_sell > 0 else ba.best_bid
                slippage_pct = ba.slippage_sell_pct
            exec_spread = real_spread
            is_book_price = True
            book_imbalance = ba.imbalance_5pct

            # Stale-price / data-integrity gate — if the snapshot price
            # (from /price) and the live book midpoint disagree materially,
            # one of them is stale or the market just moved sharply. Either
            # way, trading on inconsistent data is unsafe, so reject.
            snap_price = snap.price or 0.0
            divergence = 0.0
            if snap_price > 0 and ba.midpoint > 0:
                divergence = abs(snap_price - ba.midpoint) / ba.midpoint
            if divergence > cfg.max_price_book_divergence:
                risk_rejections += 1
                store.insert_decision(
                    timestamp=tick_ts, token_id=snap.token_id, condition_id=snap.condition_id,
                    action="RISK_REJECTED",
                    reason=f"Stale price: snap={snap_price:.4f} vs book_mid={ba.midpoint:.4f} ({divergence:.2%})",
                    strategy=strategy.name, confidence=sig.confidence,
                    price=exec_price, spread=real_spread,
                    signal_detail=sig.reason, risk_detail="stale_price",
                    features={
                        **sig.features,
                        "snap_price": round(snap_price, 4),
                        "book_mid": round(ba.midpoint, 4),
                        "price_divergence": round(divergence, 6),
                    },
                )
                continue

            # Second-chance spread gate using the real book
            if real_spread > cfg.max_spread:
                risk_rejections += 1
                store.insert_decision(
                    timestamp=tick_ts, token_id=snap.token_id, condition_id=snap.condition_id,
                    action="RISK_REJECTED", reason=f"Book spread {real_spread:.4f} exceeds max",
                    strategy=strategy.name, confidence=sig.confidence,
                    price=exec_price, spread=real_spread,
                    signal_detail=sig.reason, risk_detail="book_spread",
                    features={**sig.features, "book_imbalance_5pct": round(book_imbalance, 4)},
                )
                continue

            # Reject if slippage is excessive — book too thin for our size.
            # Threshold lives in config (``MAX_BOOK_SLIPPAGE_PCT``).
            max_slippage = float(getattr(cfg, "max_book_slippage_pct", 0.02))
            if slippage_pct > max_slippage:
                risk_rejections += 1
                store.insert_decision(
                    timestamp=tick_ts, token_id=snap.token_id, condition_id=snap.condition_id,
                    action="RISK_REJECTED",
                    reason=f"Book too thin: slippage {slippage_pct:.3%} > {max_slippage:.1%}",
                    strategy=strategy.name, confidence=sig.confidence,
                    price=exec_price, spread=real_spread,
                    signal_detail=sig.reason, risk_detail="book_slippage",
                    features={
                        **sig.features,
                        "slippage_pct": round(slippage_pct, 6),
                        "size_usd": round(size_usd, 2),
                        "book_imbalance_5pct": round(book_imbalance, 4),
                    },
                )
                continue

            # Reject if book imbalance strongly contradicts our direction.
            # imbalance > 0 = buy pressure; imbalance < 0 = sell pressure.
            # A strong -0.5 imbalance while we want to BUY is a red flag.
            contra_threshold = 0.5
            is_contra = (
                (sig.action == Action.BUY and book_imbalance <= -contra_threshold) or
                (sig.action == Action.SELL and book_imbalance >= contra_threshold)
            )
            if is_contra:
                risk_rejections += 1
                store.insert_decision(
                    timestamp=tick_ts, token_id=snap.token_id, condition_id=snap.condition_id,
                    action="RISK_REJECTED",
                    reason=f"Book imbalance {book_imbalance:+.3f} contradicts {sig.action.value}",
                    strategy=strategy.name, confidence=sig.confidence,
                    price=exec_price, spread=real_spread,
                    signal_detail=sig.reason, risk_detail="book_imbalance_contra",
                    features={
                        **sig.features,
                        "book_imbalance_5pct": round(book_imbalance, 4),
                    },
                )
                continue

        # 6. Compute max_fillable_size from book depth (partial fill handling).
        #    We already walked the book to get VWAP; the fill-side depth tells
        #    us how many shares the book can actually absorb at the target
        #    slippage.  If the book has less than we want, the paper engine
        #    will do a partial fill — reflecting reality.
        max_fillable_size: float | None = None
        if ba is not None:
            # Use depth within 5% of midpoint on the relevant side as the
            # fillable cap.  depth is in USD → convert to shares at the
            # best price on that side.
            if sig.action == Action.BUY and ba.best_ask > 0:
                max_fillable_size = ba.ask_depth_5pct / ba.best_ask
            elif sig.action == Action.SELL and ba.best_bid > 0:
                max_fillable_size = ba.bid_depth_5pct / ba.best_bid

        # 7. Execute (or observe, under shadow mode)
        #
        # Shadow mode short-circuits here: we record a SHADOW_ENTRY_* decision
        # so the operator can compare signal quality across configs, but we
        # skip execution, portfolio updates, and calibration so shadow runs
        # never contaminate real paper PnL.  Exits are also suppressed
        # (we don't open anything to exit).
        if getattr(cfg, "shadow_mode", False):
            store.insert_decision(
                timestamp=tick_ts, token_id=snap.token_id, condition_id=snap.condition_id,
                action=f"SHADOW_ENTRY_{sig.action.value}",
                reason=sig.reason,
                strategy=strategy.name, confidence=sig.confidence,
                price=exec_price, spread=exec_spread,
                signal_detail=sig.reason,
                features={
                    **sig.features,
                    "shadow": True,
                    "would_size": round(verdict.adjusted_size, 4),
                    "would_price": round(exec_price, 4),
                    "book_imbalance_5pct": round(book_imbalance, 4),
                    "slippage_pct": round(slippage_pct, 6),
                },
            )
            continue

        # Maker-preferred mode (paper-only experiment): override exec_price
        # to the passive side of the book and tag the order as a maker.  We
        # only engage when a live book analysis is available, so the price
        # we post is the *current* best bid/ask — otherwise we fall back to
        # the existing taker behaviour.  Entries only; exits keep crossing
        # so circuit-breaker / stop-loss exits are never stranded on a
        # passive queue.
        order_type = "taker"
        if (
            getattr(cfg, "order_mode", "taker") == "maker_preferred"
            and ba is not None
            and ba.best_bid > 0
            and ba.best_ask > 0
        ):
            order_type = "maker"
            if sig.action == Action.BUY:
                exec_price = ba.best_bid
            else:
                exec_price = ba.best_ask
            is_book_price = True
            # Maker quote sits in queue — no book-depth partial-fill cap.
            max_fillable_size = None

        order = OrderRequest(
            token_id=snap.token_id,
            condition_id=snap.condition_id,
            side=sig.action.value,
            size=verdict.adjusted_size,
            price=exec_price,
            strategy=strategy.name,
            spread=exec_spread,
            is_book_price=is_book_price,
            max_fillable_size=max_fillable_size,
            order_type=order_type,
        )
        t_submit_ts = time.time()
        result = executor.execute(order)
        t_fill_ts = time.time()

        if latency_tracker is not None and result.success:
            latency_tracker.observe(
                t_signal=t_signal_ts, t_submit=t_submit_ts, t_fill=t_fill_ts,
            )
            from src.utils.latency import should_alert_latency
            latest_ms = latency_tracker.latest_signal_to_fill_ms()
            if should_alert_latency(latest_ms, threshold_ms=cfg.latency_alert_threshold_ms):
                logger.warning(
                    "Latency alert: signal→fill %.0f ms (threshold %.0f ms)",
                    latest_ms, cfg.latency_alert_threshold_ms,
                )

        # Record maker-miss as a decision so operators can see the signal
        # fired and a passive quote was posted but nobody took it.  No
        # position or trade is ever recorded — this branch is pure telemetry.
        if (
            not result.success
            and order_type == "maker"
            and sig.action == Action.BUY
        ):
            store.insert_decision(
                timestamp=tick_ts, token_id=snap.token_id,
                condition_id=snap.condition_id,
                action="MAKER_MISS",
                reason=result.message or "Maker quote not filled",
                strategy=strategy.name, confidence=sig.confidence,
                price=exec_price, spread=exec_spread,
                signal_detail=sig.reason,
                features={
                    **sig.features,
                    "maker_fill_prob": round(float(cfg.maker_fill_prob), 4),
                    "would_size": round(verdict.adjusted_size, 4),
                    "posted_price": round(exec_price, 4),
                },
            )

        if result.success and sig.action == Action.BUY:
            trades_executed += 1
            # Tick the first-N live-trades autopause counter as soon as
            # a live BUY fills.  No-op in paper or when the gate is off.
            if result.mode == "live" and risk_mgr.live_autopause_gate is not None:
                risk_mgr.live_autopause_gate.record_live_fill("BUY")
            # Use actual filled size (may be partial if book depth < requested)
            actual_size = result.filled_size if result.filled_size > 0 else verdict.adjusted_size
            actual_fill = result.fill_price if result.fill_price > 0 else exec_price
            _accrue_fill_fee(
                cfg, portfolio, actual_size, actual_fill,
                is_maker=(order_type == "maker"),
            )
            portfolio.open_position(
                Position(
                    token_id=snap.token_id,
                    condition_id=snap.condition_id,
                    side="BUY",
                    size=actual_size,
                    entry_price=actual_fill,
                    strategy=strategy.name,
                    order_id=result.order_id,
                    entry_timestamp=tick_ts,
                    category=snap.category,
                )
            )
            was_partial = actual_size < verdict.adjusted_size - 1e-9
            entry_features = {
                **sig.features,
                **sizing_trace,
                "slippage_pct": round(slippage_pct, 6),
                "book_imbalance_5pct": round(book_imbalance, 4),
                "fill_price": round(actual_fill, 4),
                "midpoint_at_entry": round(snap.price or 0.0, 4),
                "requested_size": round(verdict.adjusted_size, 4),
                "filled_size": round(actual_size, 4),
                "fill_ratio": round(actual_size / verdict.adjusted_size, 4) if verdict.adjusted_size > 0 else 0.0,
                "partial_fill": was_partial,
            }
            store.insert_decision(
                timestamp=tick_ts, token_id=snap.token_id, condition_id=snap.condition_id,
                action="ENTRY_BUY", reason=sig.reason,
                strategy=strategy.name, confidence=sig.confidence,
                price=snap.price or 0, spread=snap.spread or 0,
                signal_detail=sig.reason,
                features=entry_features,
            )
            # Record a calibration entry so we can later measure predicted
            # confidence vs realised outcome.
            store.insert_calibration_entry(
                entry_timestamp=tick_ts,
                token_id=snap.token_id,
                strategy=strategy.name,
                confidence=sig.confidence,
                entry_price=actual_fill,
                features=entry_features,
            )

    # Log tick summary with timing and per-reason counters
    tick_duration = utc_timestamp() - tick_start
    summary = portfolio.summary(price_fn=client.get_price)
    logger.info(
        "Tick complete (%.1fs): markets=%d, signals=%d, risk_rejected=%d, trades=%d | "
        "skip: warmup=%d, already_open=%d, no_price=%d, hold=%d | daily_pnl=$%.2f | Portfolio: %s",
        tick_duration, len(snapshots), signals_generated, risk_rejections, trades_executed,
        skip_insufficient_history, skip_already_open, skip_no_price, skip_hold,
        risk_mgr.daily_pnl, summary,
    )

    # Compute portfolio tail risk (VaR/CVaR/worst-case) over open
    # positions.  Cheap when N <= 12 (exact 2^N enumeration), Monte
    # Carlo above.  Default-on for observability; alert thresholds
    # default to 0 (= silent) so an operator must opt into paging.
    tail_var = tail_cvar = tail_worst = 0.0
    if getattr(cfg, "tail_risk_enabled", True):
        try:
            from src.analysis.tail_risk import compute_tail_risk
            metrics = compute_tail_risk(
                portfolio.positions.values(),
                price_fn=lambda tid: client.get_price(tid),
                rng_seed=42,  # deterministic across re-ticks
            )
            tail_var = metrics.var_95
            tail_cvar = metrics.cvar_95
            tail_worst = metrics.worst_case
            if alert_mgr is not None:
                if cfg.var_95_alert_usd > 0 and tail_var >= cfg.var_95_alert_usd:
                    alert_mgr.notify(
                        "critical", "var 95 breach",
                        {"var_95_usd": round(tail_var, 2),
                         "threshold_usd": cfg.var_95_alert_usd,
                         "n_positions": metrics.n_positions,
                         "method": metrics.method},
                    )
                if cfg.cvar_95_alert_usd > 0 and tail_cvar >= cfg.cvar_95_alert_usd:
                    alert_mgr.notify(
                        "critical", "cvar 95 breach",
                        {"cvar_95_usd": round(tail_cvar, 2),
                         "threshold_usd": cfg.cvar_95_alert_usd,
                         "n_positions": metrics.n_positions,
                         "method": metrics.method},
                    )
        except Exception:
            logger.debug("Tail-risk computation failed.", exc_info=True)

    # Persist tick stats for dashboard and analysis
    try:
        store.insert_tick_stats(
            timestamp=tick_ts,
            duration_s=tick_duration,
            markets_scanned=len(snapshots),
            signals_generated=signals_generated,
            risk_rejections=risk_rejections,
            trades_executed=trades_executed,
            open_positions=summary["open_positions"],
            total_exposure=summary["total_exposure"],
            realised_pnl=summary["realised_pnl"],
            unrealised_pnl=summary["unrealised_pnl"],
            daily_pnl=risk_mgr.daily_pnl,
            skip_warmup=skip_insufficient_history,
            skip_no_price=skip_no_price,
            skip_hold=skip_hold,
            var_95=tail_var,
            cvar_95=tail_cvar,
            worst_case=tail_worst,
        )
    except Exception:
        logger.debug("Failed to insert tick stats.", exc_info=True)

    # Mirror tick stats as a JSON line so external pipelines (Loki,
    # Vector, etc.) can consume metrics without reaching into SQLite.
    # No-op when ``metrics_file`` is unset.
    if metrics is not None and getattr(metrics, "enabled", False):
        metrics.emit(
            "tick",
            ts=tick_ts,
            duration_s=round(tick_duration, 3),
            markets_scanned=len(snapshots),
            signals_generated=signals_generated,
            risk_rejections=risk_rejections,
            trades_executed=trades_executed,
            open_positions=summary["open_positions"],
            total_exposure=round(summary["total_exposure"], 2),
            realised_pnl=round(summary["realised_pnl"], 4),
            unrealised_pnl=round(summary["unrealised_pnl"], 4),
            daily_pnl=round(risk_mgr.daily_pnl, 4),
            skip_warmup=skip_insufficient_history,
            skip_no_price=skip_no_price,
            skip_hold=skip_hold,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """Polymarket Trading Bot CLI."""
    pass


@cli.command("run-bot")
def cmd_run_bot():
    """Start the main trading loop."""
    cfg = Config()
    setup_logging(
        cfg.log_level, cfg.log_file,
        max_bytes=cfg.log_max_bytes, backup_count=cfg.log_backup_count,
    )
    run_loop(cfg)


@cli.command("backfill-markets")
def cmd_backfill():
    """Download active markets and cache them locally.

    Uses the configured MAX_MARKETS_FETCH cap to avoid fetching tens of
    thousands of markets (Polymarket has 51K+ active markets; fetching all
    of them blocks for a long time and is unnecessary for paper trading).
    """
    cfg = Config()
    setup_logging(
        cfg.log_level, cfg.log_file,
        max_bytes=cfg.log_max_bytes, backup_count=cfg.log_backup_count,
    )
    client = PolymarketClient(cfg)
    store = SQLiteStore(cfg.sqlite_db_path)

    markets = client.get_active_markets_limited(max_total=cfg.max_markets_fetch)
    for mkt in markets:
        cid = mkt.get("conditionId") or mkt.get("condition_id", "")
        question = mkt.get("question", "")
        store.upsert_market(cid, question, json.dumps(mkt), iso_now())

    click.echo(f"Cached {len(markets)} markets (capped at {cfg.max_markets_fetch}).")
    client.close()
    store.close()


@cli.command("show-portfolio")
def cmd_portfolio():
    """Show the current (in-memory) portfolio state — reads from trade history."""
    cfg = Config()
    setup_logging(cfg.log_level)
    store = SQLiteStore(cfg.sqlite_db_path)
    trades = store.get_trades(limit=200)
    if not trades:
        click.echo("No trades recorded yet.")
        return

    # Reconstruct positions from trade log (simple approach)
    positions: dict[str, dict] = {}
    for t in reversed(trades):
        tid = t["token_id"]
        if t["side"] == "BUY":
            positions[tid] = t
        elif t["side"] == "SELL" and tid in positions:
            del positions[tid]

    if not positions:
        click.echo("No open positions.")
    else:
        rows = [[p["token_id"][:16], p["side"], p["size"], p["price"], p["strategy"], p["timestamp"]] for p in positions.values()]
        click.echo(tabulate(rows, headers=["Token", "Side", "Size", "Price", "Strategy", "Time"]))
    store.close()


@cli.command("show-trades")
@click.option("--limit", default=20, help="Number of trades to show.")
def cmd_trades(limit: int):
    """Show recent trades from the SQLite database."""
    cfg = Config()
    setup_logging(cfg.log_level)
    store = SQLiteStore(cfg.sqlite_db_path)
    trades = store.get_trades(limit=limit)
    if not trades:
        click.echo("No trades recorded yet.")
        return
    rows = [
        [t["order_id"][:16], t["side"], t["size"], t["price"], t["mode"], t["strategy"], t["timestamp"]]
        for t in trades
    ]
    click.echo(tabulate(rows, headers=["Order ID", "Side", "Size", "Price", "Mode", "Strategy", "Time"]))
    store.close()


@cli.command("backtest")
@click.option("--strategy", default=None, help="Strategy to test (defaults to config).")
@click.option("--min-points", default=5, help="Minimum price points per token.")
@click.option("--spread", default=0.02, help="Assumed spread for slippage.")
@click.option("--output", default="backtest_report.json", help="Output JSON path.")
def cmd_backtest(strategy: str, min_points: int, spread: float, output: str):
    """Replay historical price_history through a strategy and report PnL.

    Reads price_history from SQLite, replays each token independently, and
    writes a JSON report with per-trade detail. No API calls are made.
    """
    cfg = Config()
    setup_logging(cfg.log_level)
    if strategy:
        # Override strategy in-place by building a new Config via env
        os.environ["STRATEGY"] = strategy
        cfg = Config()

    store = SQLiteStore(cfg.sqlite_db_path)
    strat = _build_strategy(cfg, store=store)

    histories = load_price_histories_from_store(store, min_points=min_points)
    if not histories:
        click.echo(f"No price histories with >= {min_points} points. Let the bot run first to collect prices.")
        store.close()
        return

    click.echo(f"Replaying {len(histories)} tokens with strategy={strat.name}, spread={spread}...")
    bt = Backtester(cfg, strat, assumed_spread=spread)
    report = bt.run(histories)
    click.echo("\n" + report.summary())
    save_report(report, output)
    click.echo(f"\nFull report written to {output}")
    store.close()


@cli.command("validate-strategy")
@click.option("--strategy", default=None, help="Strategy to validate (defaults to config).")
@click.option("--min-points", default=20, help="Minimum price points per token.")
@click.option("--spread", default=0.02, help="Assumed spread for slippage.")
@click.option("--train-size", default=None, type=int, help="Train window size (ticks). Default: cfg.validation_train_size.")
@click.option("--test-size", default=None, type=int, help="Test (OOS) window size. Default: cfg.validation_test_size.")
@click.option("--purge", default=None, type=int, help="Ticks to drop at the train/test boundary.")
@click.option("--embargo", default=None, type=int, help="Ticks to skip at the start of each test slice.")
@click.option("--n-trials", default=None, type=int, help="Number of strategy configurations evaluated to arrive here. Drives DSR.")
@click.option("--seed", default=None, type=int, help="RNG seed for the inner backtester. Same seed → byte-identical report.")
@click.option("--stress-slippage/--no-stress-slippage", default=False, help="Multiply assumed spread by VALIDATION_SLIPPAGE_STRESS_MULTIPLIER as a sanity check.")
@click.option("--output", default="validation_report.json", help="Output JSON path.")
def cmd_validate_strategy(
    strategy: str,
    min_points: int,
    spread: float,
    train_size: int | None,
    test_size: int | None,
    purge: int | None,
    embargo: int | None,
    n_trials: int | None,
    seed: int | None,
    stress_slippage: bool,
    output: str,
):
    """Walk-forward validation against the promote-to-live gates.

    Runs an anchored walk-forward over per-token price histories from
    SQLite, evaluates the configured ``VALIDATION_*`` thresholds, and
    prints a binary verdict (``PROMOTE`` / ``REJECT``) plus the gates
    that passed or failed.  Writes the full machine-readable report
    to ``--output``.

    This command is read-only: it never places orders, never mutates
    portfolio state, and never trains anything destructively.  Run it
    on the same DB the bot writes to.
    """
    cfg = Config()
    setup_logging(cfg.log_level)
    if strategy:
        os.environ["STRATEGY"] = strategy
        cfg = Config()

    store = SQLiteStore(cfg.sqlite_db_path)
    strat = _build_strategy(cfg, store=store)

    histories = load_price_histories_from_store(store, min_points=min_points)
    if not histories:
        click.echo(
            f"No price histories with >= {min_points} points. "
            f"Let the bot run first to collect prices.",
        )
        store.close()
        return

    used_spread = spread * cfg.validation_slippage_stress_multiplier if stress_slippage else spread
    train = train_size if train_size is not None else cfg.validation_train_size
    test = test_size if test_size is not None else cfg.validation_test_size
    purge_v = purge if purge is not None else cfg.validation_purge
    embargo_v = embargo if embargo is not None else cfg.validation_embargo
    seed_v = seed if seed is not None else cfg.validation_seed
    if n_trials is not None:
        os.environ["VALIDATION_N_TRIALS"] = str(n_trials)
        cfg = Config()

    from src.backtest.walk_forward import run_walk_forward
    from src.backtest.validate import evaluate_validation, report_to_json

    click.echo(
        f"Running walk-forward: tokens={len(histories)} "
        f"train={train} test={test} purge={purge_v} embargo={embargo_v} "
        f"seed={seed_v} spread={used_spread:.4f}{' (stress)' if stress_slippage else ''}",
    )
    wf = run_walk_forward(
        cfg, strat, histories,
        train_size=train, test_size=test,
        purge=purge_v, embargo=embargo_v,
        assumed_spread=used_spread,
        seed=seed_v,
    )
    report = evaluate_validation(
        strategy_name=strat.name, walk_forward=wf, cfg=cfg,
    )
    click.echo("")
    click.echo(report.pretty())
    with open(output, "w") as f:
        f.write(report_to_json(report))
    click.echo(f"\nFull report written to {output}")
    if not report.promote:
        # Non-zero exit so CI / shell pipelines can react to a REJECT.
        store.close()
        raise SystemExit(1)
    store.close()


@cli.command("shadow-report")
@click.option("--tail", default=200, help="Most-recent N closed trades per series.")
@click.option("--strategy", default=None, help="Filter shadow rows to one strategy (default: list all configured).")
def cmd_shadow_report(tail: int, strategy: str | None):
    """Compare live vs shadow A/B(/C/…) PnL and risk-adjusted metrics.

    Reads ``calibration`` for live and ``shadow_calibration`` for
    each configured shadow strategy and prints a head-to-head table:
    trade count, win rate, realised PnL, annualized Sharpe / Sortino,
    max drawdown, PSR vs zero.  Read-only.

    Without ``--strategy`` it iterates the strategies listed in
    ``SHADOW_STRATEGY``/``SHADOW_STRATEGIES`` (one column per
    candidate).  With ``--strategy <name>`` it reports just that one.
    """
    cfg = Config()
    setup_logging(cfg.log_level)
    store = SQLiteStore(cfg.sqlite_db_path)
    try:
        from src.analysis.risk_metrics import (
            probabilistic_sharpe_ratio, returns_summary,
        )

        def _summarise(returns: list[float], wins_block: dict) -> dict:
            rs = returns_summary(returns, periods_per_year=252)
            psr = probabilistic_sharpe_ratio(returns, periods_per_year=252) if returns else 0.0
            return {
                "n": rs.n,
                "sharpe": rs.sharpe_annualized,
                "sortino": rs.sortino_annualized,
                "max_dd": rs.max_drawdown,
                "psr": psr,
                "total_return": sum(returns),
                "mean_return": rs.mean,
                "win_rate": wins_block,
            }

        # Live column.
        live = _summarise(
            store.get_closed_trade_returns(limit=tail),
            store.compute_win_rate(),
        )

        # Shadow columns.
        if strategy:
            shadow_names = [strategy]
        else:
            shadow_names = list(cfg.shadow_strategy_list())
            if not shadow_names:
                # Backwards-compatible fallback: show whatever rows are
                # already in the table even if no SHADOW_STRATEGIES is
                # set right now (operator may have collected data
                # earlier with a different config).
                cur = store._conn.execute(
                    "SELECT DISTINCT strategy FROM shadow_calibration "
                    "WHERE exit_timestamp IS NOT NULL ORDER BY strategy",
                )
                shadow_names = [r[0] for r in cur.fetchall()]

        shadow_cols: list[tuple[str, dict]] = []
        for name in shadow_names:
            shadow_cols.append((name, _summarise(
                store.get_shadow_closed_returns(limit=tail, strategy=name),
                store.compute_shadow_win_rate(strategy=name),
            )))

        def _fmt_wr(s: dict) -> str:
            wr = s["win_rate"]
            decisive = wr["wins"] + wr["losses"]
            return f"{wr['win_rate'] * 100:.1f}% ({wr['wins']}/{decisive})"

        headers = ["Metric", "Live"] + [name for name, _ in shadow_cols]
        rows = [
            ("Closed trades (n)", f"{live['n']}", *[f"{s['n']}" for _, s in shadow_cols]),
            ("Win rate", _fmt_wr(live), *[_fmt_wr(s) for _, s in shadow_cols]),
            ("Sum of returns",
             f"{live['total_return']:+.4f}",
             *[f"{s['total_return']:+.4f}" for _, s in shadow_cols]),
            ("Mean return",
             f"{live['mean_return']:+.5f}",
             *[f"{s['mean_return']:+.5f}" for _, s in shadow_cols]),
            ("Sharpe (annual.)",
             f"{live['sharpe']:+.3f}",
             *[f"{s['sharpe']:+.3f}" for _, s in shadow_cols]),
            ("Sortino (annual.)",
             f"{live['sortino']:+.3f}",
             *[f"{s['sortino']:+.3f}" for _, s in shadow_cols]),
            ("Max drawdown",
             f"{live['max_dd']:.4f}",
             *[f"{s['max_dd']:.4f}" for _, s in shadow_cols]),
            ("PSR vs SR=0",
             f"{live['psr']:.3f}",
             *[f"{s['psr']:.3f}" for _, s in shadow_cols]),
        ]
        click.echo("")
        click.echo(tabulate(rows, headers=headers, tablefmt="github"))
        click.echo("")
        if not shadow_cols:
            click.echo(
                "No shadow strategies configured. "
                "Set SHADOW_STRATEGIES=foo,bar (or SHADOW_STRATEGY=foo) and let the bot run.",
            )
            return
        # Pick the best shadow by Sharpe and write a one-line verdict.
        best_name, best = max(shadow_cols, key=lambda kv: kv[1]["sharpe"])
        if best["n"] == 0:
            click.echo("No shadow trades yet — wait for the bot to accumulate closed positions.")
        elif best["n"] < 30:
            click.echo(
                f"Best shadow is '{best_name}' (n={best['n']}) — too few trades for inference. "
                "Need >= 30; promote-to-live via validate-strategy needs >= 200.",
            )
        elif best["sharpe"] > live["sharpe"] and best["psr"] >= 0.95:
            click.echo(
                f"Best shadow '{best_name}' beats live on Sharpe AND PSR>=0.95 vs zero — "
                "consider running validate-strategy on it.",
            )
        elif best["sharpe"] <= live["sharpe"]:
            click.echo(
                f"No shadow outperforms live (best is '{best_name}'). "
                "Keep observing or rotate candidates.",
            )
        else:
            click.echo(
                f"Best shadow '{best_name}' beats live on Sharpe but PSR<0.95 — "
                "sample size or noise; keep observing.",
            )
    finally:
        store.close()


@cli.command("calibration-report")
@click.option("--bins", default=5, help="Number of confidence bins.")
def cmd_calibration(bins: int):
    """Report calibration of strategy confidence vs realised PnL.

    Reads decision_log entries with action=ENTRY_BUY, joins them with
    subsequent exits (EXIT_STOP_LOSS / EXIT_TAKE_PROFIT) on the same
    token, and bins by predicted confidence.
    """
    from src.analysis.calibration import calibration_report
    cfg = Config()
    setup_logging(cfg.log_level)
    store = SQLiteStore(cfg.sqlite_db_path)
    table = calibration_report(store, n_bins=bins)
    if not table:
        click.echo("No matched entry/exit pairs in decision_log yet.")
        store.close()
        return
    click.echo(tabulate(table, headers="keys", floatfmt=".4f"))
    store.close()


@cli.command("show-positions-detail")
def cmd_positions_detail():
    """Analyze currently open positions with time-in-position and live PnL."""
    from src.tools.analyze_positions import analyze_open_positions
    cfg = Config()
    setup_logging(cfg.log_level)
    client = PolymarketClient(cfg)
    store = SQLiteStore(cfg.sqlite_db_path)
    rows = analyze_open_positions(store, client)
    if not rows:
        click.echo("No open positions found in the trade history.")
    else:
        click.echo(tabulate(rows, headers="keys", floatfmt=".4f"))
    client.close()
    store.close()


@cli.command("run-experiments")
@click.argument("manifest", default="experiments/manifest.json")
def cmd_experiments(manifest: str):
    """Run a batch of backtests defined in a JSON manifest."""
    from src.experiments.runner import run_manifest
    cfg = Config()
    setup_logging(cfg.log_level)
    store = SQLiteStore(cfg.sqlite_db_path)
    result = run_manifest(manifest, cfg, store)
    click.echo(result)
    store.close()


@cli.command("check-resolutions")
def cmd_check_resolutions():
    """Check if any traded markets have resolved and record outcomes."""
    from src.analysis.resolution_tracker import check_resolutions
    cfg = Config()
    setup_logging(cfg.log_level)
    client = PolymarketClient(cfg)
    store = SQLiteStore(cfg.sqlite_db_path)
    result = check_resolutions(cfg, client, store)
    click.echo(result)
    store.close()
    client.close()


@cli.command("edge-calibration")
def cmd_edge_calibration():
    """Analyze whether edge-model predictions were actually correct."""
    from src.analysis.edge_calibration import analyze_edge_calibration, format_report_markdown
    cfg = Config()
    setup_logging(cfg.log_level)
    store = SQLiteStore(cfg.sqlite_db_path)
    report = analyze_edge_calibration(store)
    click.echo(format_report_markdown(report))
    store.close()


@cli.command("detect-arbs")
@click.option("--min-discount", type=float, default=None,
              help="Override ARB_MIN_DISCOUNT (e.g. 0.02 = 2%).")
@click.option("--limit", type=int, default=20, help="Max rows in output table.")
def cmd_detect_arbs(min_discount, limit):
    """Scan current Polymarket markets for negative-risk arbs (read-only).

    Never places trades.  Records detections to ``arb_opportunities`` so
    operators can review and decide whether to act manually.
    """
    from src.analysis.arb_detector import (
        find_negative_risk_arbs, scan_and_record, format_report_markdown,
    )
    cfg = Config()
    setup_logging(cfg.log_level)
    store = SQLiteStore(cfg.sqlite_db_path)
    client = PolymarketClient(cfg)
    market_svc = MarketDataService(client, cfg)
    click.echo("Fetching markets...")
    snapshots = market_svc.fetch_and_filter()
    click.echo(f"Scanning {len(snapshots)} snapshots for negative-risk arbs...")
    disc = min_discount if min_discount is not None else cfg.arb_min_discount
    arbs = scan_and_record(
        snapshots, store, iso_now(),
        min_discount=disc,
        min_legs_liquidity=cfg.arb_min_legs_liquidity,
    )
    click.echo(format_report_markdown(arbs, limit=limit))
    store.close()


@cli.command("performance-report")
def cmd_performance_report():
    """Print risk-adjusted performance metrics (Sharpe, Sortino, expectancy, Calmar).

    Read-only: joins closed BUY/SELL pairs from the trade log and computes
    quant-standard metrics.  Does not touch portfolio or trading state.
    """
    from src.analysis.performance_metrics import (
        build_trade_pnls_from_store, compute_performance, format_report_markdown,
    )
    cfg = Config()
    setup_logging(cfg.log_level)
    store = SQLiteStore(cfg.sqlite_db_path)
    closed = build_trade_pnls_from_store(store)
    report = compute_performance(closed)
    click.echo(format_report_markdown(report))
    store.close()


@cli.command("preflight")
@click.option("--skip-network", is_flag=True, default=False,
              help="Skip Polymarket API + wallet balance probes (CI use).")
def cmd_preflight(skip_network: bool):
    """Run pre-launch sanity checks; exit 1 if any FAIL.

    Run this *every time* before changing live-trading flags.  See
    docs/runbook.md for what each check guards against.
    """
    from src.preflight import format_results, run_preflight
    cfg = Config()
    setup_logging(cfg.log_level)
    results = run_preflight(cfg, skip_network=skip_network)
    click.echo(format_results(results))
    if any(r.is_blocking() for r in results):
        sys.exit(1)


@cli.command("test-alerts")
@click.option("--severity", type=click.Choice(["info", "warning", "critical", "all"]),
              default="all", help="Fire alerts at this severity (or all three).")
def cmd_test_alerts(severity: str):
    """Fire test alerts through every configured sink.

    Use this *before* flipping live-trading flags to verify that your
    Slack / Discord webhook is reachable and that the file sink is
    writing where you expect.  It dispatches real alerts through the
    manager (respecting dedupe + rate limits), so a second run inside
    the dedupe window will silently no-op — that's the real behaviour.
    """
    from src.utils.alerts import build_from_config
    cfg = Config()
    setup_logging(cfg.log_level)
    mgr = build_from_config(cfg)
    if not mgr.sinks:
        click.echo("No alert sinks configured. Set ALERT_LOG_FILE or "
                   "ALERT_WEBHOOK_URL, then retry.")
        sys.exit(1)
    click.echo(f"Alert manager has {len(mgr.sinks)} sink(s) configured.")
    levels = ["info", "warning", "critical"] if severity == "all" else [severity]
    sent = 0
    for lvl in levels:
        ok = mgr.notify(
            lvl,
            f"test-alerts {lvl} probe",
            {
                "source": "src.main.test-alerts",
                "trading_mode": cfg.trading_mode,
                "note": "This is a synthetic alert. No trading action was taken.",
            },
        )
        click.echo(f"  {lvl:<8} -> {'delivered' if ok else 'dropped (dedupe/rate-limit)'}")
        if ok:
            sent += 1
    click.echo(f"Fired {sent}/{len(levels)} alert(s). "
               "Check your Slack/Discord channel and the file sink.")


@cli.command("daily-summary")
@click.option("--date", "day_str", default="",
              help="UTC date (YYYY-MM-DD); defaults to yesterday.")
@click.option("--alert", is_flag=True, default=False,
              help="Dispatch the summary through configured alert sinks.")
def cmd_daily_summary(day_str: str, alert: bool):
    """Print yesterday's activity: trades, PnL, winners/losers.

    Safe to cron: pure read of the local SQLite DB, no network, no
    order placement.  Pass ``--alert`` to route the summary through
    the same alert sinks used for operational events (info severity
    so webhook filters can route it to a separate channel).
    """
    from datetime import date as _date

    from src.analysis.daily_summary import build_daily_summary, format_summary
    from src.storage.sqlite_store import SQLiteStore
    from src.utils.alerts import build_from_config

    cfg = Config()
    setup_logging(cfg.log_level)

    if day_str:
        try:
            target_day = _date.fromisoformat(day_str)
        except ValueError:
            click.echo(f"Invalid --date {day_str!r}; expected YYYY-MM-DD.")
            sys.exit(1)
    else:
        target_day = None  # build_daily_summary defaults to UTC yesterday

    store = SQLiteStore(cfg.sqlite_db_path)
    try:
        trades = store.get_all_trades()
        closed = store.get_calibration_closed()
    finally:
        store.close()

    summary = build_daily_summary(trades, closed, day=target_day)
    text = format_summary(summary)
    click.echo(text)

    if alert:
        mgr = build_from_config(cfg)
        if not mgr.sinks:
            click.echo("\n(--alert requested but no sinks configured.)")
        else:
            mgr.notify("info", f"daily summary {summary.day}", summary.to_dict())
            click.echo("\nDispatched through alert sinks.")


if __name__ == "__main__":
    cli()
