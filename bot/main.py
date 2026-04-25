"""
Polymarket Trading Bot — main entry point.

Runs three independent strategy loops on an async scheduler:
  1. Probability Arbitrage (Claude-powered)
  2. Logical Arbitrage (graph-based constraint violations)
  3. Market Making (two-sided liquidity)

Usage:
    python -m bot.main run          # Start the bot (paper mode default)
    python -m bot.main status       # Show risk manager state
    python -m bot.main backtest     # Run a quick backtest
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from bot.config import cfg
from bot.core import calibration
from bot.core.claude_oracle import ClaudeOracle
from bot.core.error_monitor import monitor as error_monitor
from bot.core.risk_manager import Position, RiskManager
from bot.core.state_persistence import load_state, save_state
from bot.core.utils import notify_discord, notify_telegram

# ---------------------------------------------------------------------------
# Logging setup (loguru-style via stdlib — loguru optional)
# ---------------------------------------------------------------------------

_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)


def _setup_logging() -> None:
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_path = _LOG_DIR / "bot.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt))
    handlers.append(file_handler)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format=fmt,
        handlers=handlers,
        force=True,
    )
    # Silence noisy third-party loggers
    for name in ("httpx", "httpcore", "anthropic", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


logger = logging.getLogger("bot")

# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    logger.info("Signal %s received — shutting down after current cycle.", signum)
    _shutdown = True


# ---------------------------------------------------------------------------
# Trade log persistence
# ---------------------------------------------------------------------------

_TRADE_LOG = _LOG_DIR / "trades.jsonl"


def _persist_trade(record: dict) -> None:
    try:
        with open(_TRADE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        logger.debug("Failed to persist trade record.", exc_info=True)


# ---------------------------------------------------------------------------
# Daily report
# ---------------------------------------------------------------------------


def _daily_report(risk: RiskManager, cycle_count: int) -> str:
    s = risk.summary()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"📊 *Daily Report — {now}*\n"
        f"Equity: ${s['equity']:.2f}\n"
        f"Daily PnL: ${s['daily_pnl']:+.2f}\n"
        f"Total PnL: ${s['total_pnl']:+.2f}\n"
        f"Drawdown: {s['drawdown_pct']:.1f}%\n"
        f"Positions: {s['positions']} (${s['exposure']:.2f})\n"
        f"Cycles: {cycle_count}\n"
        f"Halted: {'YES' if s['halted'] else 'No'}"
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def _run_bot() -> None:
    _setup_logging()

    mode = "PAPER" if cfg.is_paper else ("LIVE" if cfg.is_live else "PAPER (live not enabled)")
    logger.info("=" * 60)
    logger.info("Polymarket Trading Bot starting | mode=%s", mode)
    logger.info("=" * 60)

    if cfg.is_live:
        logger.warning("⚠️  LIVE MODE — real orders will be placed.")
    else:
        logger.info("Paper mode — no real orders.")

    # Brier calibration gate (blocks live trading until proven accuracy)
    if cfg.is_live:
        from bot.core.calibration import compute_metrics as _cm
        _temp_risk = RiskManager()
        gate_ok, gate_msg = _temp_risk.check_calibration_gate()
        if not gate_ok:
            logger.error("🚫 %s", gate_msg)
            logger.error("Bot will NOT start in live mode until calibration gate passes.")
            return
        logger.info("✅ %s", gate_msg)
        del _temp_risk

    # Show active strategies
    active = []
    if cfg.strategy_probability_arb:
        active.append("probability_arb")
    if cfg.strategy_logical_arb:
        active.append("logical_arb")
    if cfg.strategy_market_making:
        active.append("market_making")
    logger.info("Active strategies: %s", ", ".join(active) or "(none)")

    if not active:
        logger.error("No strategies enabled. Set STRATEGY_*=true in .env.")
        return

    # Build shared components
    oracle = ClaudeOracle()
    risk = RiskManager()

    # Restore state from previous run (graceful shutdown recovery)
    saved = load_state()
    if saved:
        try:
            from bot.core.utils import Side
            for pos_dict in saved.get("positions", []):
                pos = Position(
                    token_id=pos_dict["token_id"],
                    condition_id=pos_dict.get("condition_id", ""),
                    side=Side(pos_dict["side"]),
                    size=pos_dict["size"],
                    entry_price=pos_dict["entry_price"],
                    strategy=pos_dict.get("strategy", "unknown"),
                    timestamp=pos_dict.get("timestamp", time.time()),
                    category=pos_dict.get("category", ""),
                )
                risk.positions[pos.token_id] = pos
            risk._current_equity = saved.get("equity", cfg.starting_capital_usd)
            risk._peak_equity = saved.get("peak_equity", risk._current_equity)
            risk._total_realized_pnl = saved.get("total_pnl", 0.0)
            logger.info(
                "Restored %d positions, equity=$%.2f from saved state.",
                len(risk.positions), risk._current_equity,
            )
        except Exception:
            logger.warning("Failed to restore positions from saved state.", exc_info=True)

    # Build competitive edge: news + data feeds
    news_fetcher = None
    data_router = None
    try:
        if cfg.news_feed_enabled:
            from bot.core.news_feed import NewsFetcher
            news_fetcher = NewsFetcher()
            logger.info("News feed enabled — Claude will receive real-time headlines.")
    except Exception:
        logger.warning("News feed initialization failed.", exc_info=True)
    try:
        if cfg.data_feeds_enabled:
            from bot.core.data_feeds import DataFeedRouter
            data_router = DataFeedRouter()
            logger.info("Data feeds enabled — Claude will receive category-specific data.")
    except Exception:
        logger.warning("Data feed initialization failed.", exc_info=True)

    # Build strategies
    strategies = []
    if cfg.strategy_probability_arb:
        from bot.strategies.probability_arbitrage import ProbabilityArbitrage
        strategies.append(ProbabilityArbitrage(
            oracle, risk,
            news_fetcher=news_fetcher,
            data_router=data_router,
        ))
    if cfg.strategy_logical_arb:
        from bot.strategies.logical_arbitrage import LogicalArbitrage
        strategies.append(LogicalArbitrage(oracle, risk))
    if cfg.strategy_market_making:
        from bot.strategies.market_making import MarketMaking
        strategies.append(MarketMaking(oracle, risk))

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Healthcheck HTTP server (daemon thread)
    if cfg.healthcheck_enabled:
        try:
            from bot.core import healthcheck
            healthcheck.update_state(mode=mode.lower())
            healthcheck.start_in_thread(
                host=cfg.healthcheck_host, port=cfg.healthcheck_port,
            )
        except Exception:
            logger.warning("Healthcheck server failed to start.", exc_info=True)

    cycle = 0
    last_report = ""
    startup_msg = f"Bot started | mode={mode} | strategies={','.join(active)}"
    await notify_telegram(cfg.telegram_token, cfg.telegram_chat_id, startup_msg)
    await notify_discord(cfg.discord_webhook_url, startup_msg)

    while not _shutdown:
        cycle += 1
        cycle_start = time.time()

        logger.info("─── Cycle %d ───", cycle)

        # Run all strategies (sequentially to avoid rate-limit issues)
        for strat in strategies:
            if _shutdown:
                break
            try:
                trades = await strat.scan_and_trade()
                for t in trades:
                    _persist_trade(t)
                    if t.get("success"):
                        logger.info(
                            "TRADE | %s | %s %s @ %.4f | $%.2f | edge=%.3f | %s",
                            t.get("strategy"), t.get("side"), t.get("token_id", "")[:12],
                            t.get("price", 0), t.get("size_usd", 0),
                            t.get("edge", 0), t.get("mode", "?"),
                        )
            except Exception as exc:
                logger.exception("Strategy %s crashed — continuing.", strat.name)
                error_monitor.record("strategy_crash", f"{strat.name}: {exc}", exc=exc)

        # Check position exits (stop-loss, take-profit, max hold)
        try:
            from bot.core.polymarket_client import get_book
            exits = await risk.check_exits(get_book)
            for ex in exits:
                _persist_trade(ex)
        except Exception as exc:
            logger.exception("Position exit check failed.")
            error_monitor.record("exit_check", str(exc), exc=exc)

        # Log risk state
        s = risk.summary()
        logger.info(
            "Risk | equity=$%.2f | daily_pnl=$%+.2f | dd=%.1f%% | pos=%d ($%.2f)",
            s["equity"], s["daily_pnl"], s["drawdown_pct"],
            s["positions"], s["exposure"],
        )

        # Publish state to healthcheck
        if cfg.healthcheck_enabled:
            try:
                from bot.core import healthcheck
                budget = oracle.budget.summary()
                healthcheck.update_state(
                    last_cycle_ts=time.time(),
                    cycle_count=cycle,
                    equity=s["equity"],
                    daily_pnl=s["daily_pnl"],
                    total_pnl=s["total_pnl"],
                    drawdown_pct=s["drawdown_pct"],
                    positions=s["positions"],
                    exposure=s["exposure"],
                    halted=s["halted"],
                    claude_daily_spend=budget["daily_spend_usd"],
                    claude_budget_limit=budget["budget_limit_usd"],
                    claude_calls=budget["total_calls"],
                    errors=error_monitor.summary(),
                )
            except Exception:
                logger.debug("healthcheck update failed", exc_info=True)

        # Auto-resolve markets (check every 10 cycles to avoid spamming)
        if cycle % 10 == 0:
            try:
                from bot.core.resolution_poller import poll_resolutions
                resolved = await poll_resolutions()
                if resolved > 0:
                    logger.info("Auto-resolved %d estimate(s) this cycle.", resolved)
            except Exception:
                logger.debug("Resolution polling failed.", exc_info=True)

        # Daily report (once per UTC day)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != last_report:
            report = _daily_report(risk, cycle)
            logger.info(report)
            await notify_telegram(cfg.telegram_token, cfg.telegram_chat_id, report)
            await notify_discord(cfg.discord_webhook_url, report)
            last_report = today

        # Sleep until next cycle (shortest strategy interval)
        intervals = []
        if cfg.strategy_probability_arb:
            intervals.append(cfg.prob_arb_scan_interval_s)
        if cfg.strategy_logical_arb:
            intervals.append(cfg.logical_arb_scan_interval_s)
        if cfg.strategy_market_making:
            intervals.append(cfg.mm_scan_interval_s)
        sleep_s = min(intervals) if intervals else 60

        elapsed = time.time() - cycle_start
        wait = max(1, sleep_s - elapsed)
        logger.debug("Sleeping %.0fs…", wait)

        for _ in range(int(wait)):
            if _shutdown:
                break
            await asyncio.sleep(1)

    # Graceful shutdown: save state, cancel orders, notify
    logger.info("Shutting down — saving state…")

    # Save positions and equity for next startup
    positions_data = [
        {
            "token_id": p.token_id,
            "condition_id": p.condition_id,
            "side": p.side.value,
            "size": p.size,
            "entry_price": p.entry_price,
            "strategy": p.strategy,
            "timestamp": p.timestamp,
            "category": p.category,
        }
        for p in risk.positions.values()
    ]
    save_state(
        positions=positions_data,
        equity=risk._current_equity,
        total_pnl=risk._total_realized_pnl,
        daily_pnl=risk._daily_pnl,
        cycle_count=cycle,
        peak_equity=risk._peak_equity,
        strategy_pnl=dict(risk._strategy_daily_pnl),
        extra={"error_summary": error_monitor.summary()},
    )

    if cfg.strategy_market_making:
        from bot.core.polymarket_client import cancel_all
        await cancel_all()

    shutdown_msg = f"Bot stopped | {_daily_report(risk, cycle)}"
    await notify_telegram(cfg.telegram_token, cfg.telegram_chat_id, shutdown_msg)
    await notify_discord(cfg.discord_webhook_url, shutdown_msg)
    logger.info("Goodbye.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli():
    """Polymarket Trading Bot."""
    pass


@cli.command()
def run():
    """Start the trading bot."""
    asyncio.run(_run_bot())


@cli.command()
def status():
    """Show current risk manager state."""
    _setup_logging()
    risk = RiskManager()
    click.echo(json.dumps(risk.summary(), indent=2))


@cli.command()
@click.option("--token-id", required=True, help="Token ID to backtest.")
@click.option("--question", default="", help="Market question (for display).")
def backtest(token_id: str, question: str):
    """Run a simple backtest on a token."""
    _setup_logging()

    async def _bt():
        from bot.backtest.engine import format_backtest_report, run_backtest
        result = await run_backtest(
            token_id=token_id,
            condition_id="",
            question=question or token_id[:16],
        )
        click.echo(format_backtest_report(result))

    asyncio.run(_bt())


@cli.command()
def preflight():
    """Pre-launch sanity checks.  Run this before ``run``.

    Verifies:
      - Environment variables / safety gates
      - Anthropic key + Claude reachability (1 cheap call)
      - Gamma API reachability
      - CLOB client buildable (only if live)
      - Log directory writable
    """
    _setup_logging()
    click.echo(click.style("\n=== Pre-flight checks ===\n", bold=True))

    ok = True

    def check(label: str, success: bool, detail: str = "") -> None:
        nonlocal ok
        tag = click.style("PASS", fg="green") if success else click.style("FAIL", fg="red")
        click.echo(f"  [{tag}] {label}" + (f"  — {detail}" if detail else ""))
        if not success:
            ok = False

    # 1. Mode / safety
    if cfg.is_live:
        check("Trading mode", True, click.style("LIVE (real money)", fg="yellow", bold=True))
    elif cfg.is_paper:
        check("Trading mode", True, "paper")
    else:
        check("Trading mode", False, f"Unrecognized mode={cfg.trading_mode!r}")

    # 2. Strategy toggles
    active = [
        s for s, enabled in [
            ("probability_arb", cfg.strategy_probability_arb),
            ("logical_arb", cfg.strategy_logical_arb),
            ("market_making", cfg.strategy_market_making),
        ] if enabled
    ]
    check("At least one strategy enabled", bool(active), ", ".join(active) or "none")

    # 3. Anthropic key
    has_key = bool(cfg.anthropic_api_key)
    check("ANTHROPIC_API_KEY set", has_key)

    # 4. Claude reachability
    if has_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
            resp = client.messages.create(
                model=cfg.claude_model,
                max_tokens=10,
                messages=[{"role": "user", "content": "say ok"}],
            )
            _ = resp.content[0].text
            check("Claude API reachable", True, f"model={cfg.claude_model}")
        except Exception as exc:
            check("Claude API reachable", False, str(exc)[:80])
    else:
        check("Claude API reachable", False, "skipped (no key)")

    # 5. Gamma API
    async def _gamma_check() -> tuple[bool, str]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{cfg.gamma_url}/markets", params={"limit": "1"})
                return r.status_code == 200, f"HTTP {r.status_code}"
        except Exception as exc:
            return False, str(exc)[:80]

    gamma_ok, gamma_detail = asyncio.run(_gamma_check())
    check("Gamma API reachable", gamma_ok, gamma_detail)

    # 6. CLOB client (only check if live mode)
    if cfg.is_live:
        if not cfg.private_key:
            check("CLOB private key", False, "PRIVATE_KEY not set")
        else:
            try:
                from bot.core.polymarket_client import _get_clob
                clob = _get_clob()
                check("CLOB client", clob is not None, cfg.clob_url)
            except Exception as exc:
                check("CLOB client", False, str(exc)[:80])
    else:
        click.echo("  [skip] CLOB client  — paper mode, not required")

    # 7. Log dir writable
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        test_path = _LOG_DIR / ".preflight_test"
        test_path.write_text("ok", encoding="utf-8")
        test_path.unlink()
        check("Log directory writable", True, str(_LOG_DIR))
    except Exception as exc:
        check("Log directory writable", False, str(exc)[:80])

    # 8. Calibration DB
    try:
        metrics = calibration.compute_metrics()
        check(
            "Calibration DB",
            True,
            f"resolved={metrics.n_resolved} pending={metrics.n_pending}",
        )
    except Exception as exc:
        check("Calibration DB", False, str(exc)[:80])

    # 8b. Calibration gate (informational — only enforced in live mode)
    if cfg.is_live:
        temp_rm = RiskManager()
        gate_ok, gate_msg = temp_rm.check_calibration_gate()
        check("Calibration gate (live required)", gate_ok, gate_msg)
    else:
        try:
            m = calibration.compute_metrics()
            if m.n_resolved >= cfg.min_resolved_estimates and m.brier_score <= cfg.max_brier_score:
                click.echo(f"  [info] Calibration gate would PASS if live "
                           f"(n={m.n_resolved}, Brier={m.brier_score:.4f})")
            else:
                click.echo(f"  [info] Calibration gate would FAIL if live "
                           f"(n={m.n_resolved}/{cfg.min_resolved_estimates}, "
                           f"Brier={m.brier_score:.4f}/{cfg.max_brier_score:.4f})")
        except Exception:
            pass

    # 9. Paper order round-trip
    async def _paper_roundtrip() -> tuple[bool, str]:
        try:
            from bot.core.polymarket_client import place_order
            from bot.core.utils import Side
            r = await place_order("test_token_preflight", Side.BUY, 0.5, 1)
            return r.success, f"order_id={r.order_id} mode={r.mode}"
        except Exception as exc:
            return False, str(exc)[:80]

    if cfg.is_paper:
        po_ok, po_detail = asyncio.run(_paper_roundtrip())
        check("Paper order round-trip", po_ok, po_detail)

    click.echo()
    if ok:
        click.echo(click.style("All checks passed — safe to run.\n", fg="green", bold=True))
        sys.exit(0)
    else:
        click.echo(click.style("Some checks failed — fix before running.\n", fg="red", bold=True))
        sys.exit(1)


@cli.command()
@click.option("--strategy", default="", help="Filter by strategy name.")
def calibration_report(strategy: str):
    """Show calibration metrics from recorded estimates."""
    _setup_logging()
    m = calibration.compute_metrics(strategy)
    click.echo(click.style("\n=== Calibration report ===\n", bold=True))
    filter_note = f" [strategy={strategy}]" if strategy else ""
    click.echo(f"Scope{filter_note}")
    click.echo(f"  Resolved estimates: {m.n_resolved}")
    click.echo(f"  Pending estimates:  {m.n_pending}")
    if m.n_resolved > 0:
        click.echo(f"  Brier score:  {m.brier_score:.4f}   (lower is better, 0.25 = random)")
        click.echo(f"  Log loss:     {m.log_loss:.4f}")
        click.echo(f"  Mean P(claude): {m.mean_p_claude:.3f}")
        click.echo(f"  Mean outcome:   {m.mean_outcome:.3f}")
    else:
        click.echo("  (no resolved estimates yet — let the bot run and record outcomes)")
    click.echo()


@cli.command()
@click.option("--condition-id", required=True, help="Market condition_id.")
@click.option("--outcome", required=True, type=click.Choice(["0", "1"]),
              help="Resolution: 1 = YES, 0 = NO.")
def record_resolution(condition_id: str, outcome: str):
    """Record a market resolution so calibration can be computed."""
    _setup_logging()
    n = calibration.record_outcome(condition_id, int(outcome))
    click.echo(f"Updated {n} estimate(s) for {condition_id}.")


@cli.command()
@click.option("--port", default=8787, type=int, help="HTTP port.")
@click.option("--host", default="127.0.0.1", help="HTTP bind address.")
def healthcheck_server(port: int, host: str):
    """Run a minimal healthcheck HTTP server (for monitoring / k8s)."""
    from bot.core.healthcheck import run_server
    run_server(host=host, port=port)


if __name__ == "__main__":
    cli()
