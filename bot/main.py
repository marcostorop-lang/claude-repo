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
from bot.core.claude_oracle import ClaudeOracle
from bot.core.risk_manager import RiskManager
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

    # Build strategies
    strategies = []
    if cfg.strategy_probability_arb:
        from bot.strategies.probability_arbitrage import ProbabilityArbitrage
        strategies.append(ProbabilityArbitrage(oracle, risk))
    if cfg.strategy_logical_arb:
        from bot.strategies.logical_arbitrage import LogicalArbitrage
        strategies.append(LogicalArbitrage(oracle, risk))
    if cfg.strategy_market_making:
        from bot.strategies.market_making import MarketMaking
        strategies.append(MarketMaking(oracle, risk))

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

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
            except Exception:
                logger.exception("Strategy %s crashed — continuing.", strat.name)

        # Log risk state
        s = risk.summary()
        logger.info(
            "Risk | equity=$%.2f | daily_pnl=$%+.2f | dd=%.1f%% | pos=%d ($%.2f)",
            s["equity"], s["daily_pnl"], s["drawdown_pct"],
            s["positions"], s["exposure"],
        )

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

    # Shutdown
    logger.info("Shutting down…")
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


if __name__ == "__main__":
    cli()
