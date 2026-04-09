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

from src.config import Config
from src.logger import setup_logging
from src.polymarket.client import PolymarketClient
from src.polymarket.execution import ExecutionEngine, OrderRequest
from src.polymarket.market_data import MarketDataService, MarketSnapshot
from src.portfolio.tracker import PortfolioTracker, Position
from src.risk.manager import RiskManager
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import Action, BaseStrategy
from src.strategy.mean_reversion import MeanReversion
from src.strategy.simple_momentum import SimpleMomentum
from src.utils.time_utils import iso_now, utc_timestamp

logger = logging.getLogger(__name__)

# Graceful shutdown flag
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s — shutting down after current cycle.", signum)
    _shutdown = True


def _build_strategy(cfg: Config) -> BaseStrategy:
    if cfg.strategy == "mean_reversion":
        return MeanReversion(cfg)
    return SimpleMomentum(cfg)


# ---------------------------------------------------------------------------
# Bot loop
# ---------------------------------------------------------------------------

def run_loop(cfg: Config) -> None:
    """Main polling loop: fetch → evaluate → execute."""
    client = PolymarketClient(cfg)
    store = SQLiteStore(cfg.sqlite_db_path)
    portfolio = PortfolioTracker()

    # Reconstruct portfolio from trade history so positions survive restarts
    all_trades = store.get_all_trades()
    if all_trades:
        portfolio.reconstruct_from_trades(all_trades)
        logger.info("Restored %d open positions from trade history.", portfolio.open_position_count())

    risk_mgr = RiskManager(cfg, portfolio)
    executor = ExecutionEngine(client, cfg, store)
    market_svc = MarketDataService(client, cfg)
    strategy = _build_strategy(cfg)

    mode_label = "PAPER" if cfg.is_paper else ("LIVE" if cfg.is_live else "PAPER (live not enabled)")
    logger.info("=== Bot started | mode=%s | strategy=%s | poll=%ds ===", mode_label, strategy.name, cfg.poll_interval)

    problems = cfg.validate()
    for p in problems:
        logger.warning("Config warning: %s", p)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    tick_count = 0
    while not _shutdown:
        # Kill switch file check
        if os.path.exists(cfg.kill_switch_file):
            logger.warning("KILL SWITCH FILE detected (%s) — shutting down.", cfg.kill_switch_file)
            break

        # Circuit breaker check
        if risk_mgr.is_circuit_breaker_active:
            logger.warning("Circuit breaker active (daily loss $%.2f). Skipping tick, monitoring only.", abs(risk_mgr.daily_pnl))
            # Still check SL/TP on existing positions even when circuit breaker is active
            _check_exits_only(risk_mgr, executor, portfolio, store, client, cfg)
        else:
            try:
                _tick(market_svc, strategy, risk_mgr, executor, portfolio, store, client, cfg)
            except Exception:
                logger.exception("Error in bot tick — will retry next cycle.")

        tick_count += 1
        # Export bot state for dashboard every tick
        _export_bot_state(cfg, portfolio, risk_mgr, strategy, tick_count, client)

        logger.debug("Sleeping %d s …", cfg.poll_interval)
        for _ in range(cfg.poll_interval):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("Bot stopped.")
    client.close()
    store.close()


def _check_exits_only(
    risk_mgr: RiskManager,
    executor: ExecutionEngine,
    portfolio: PortfolioTracker,
    store: SQLiteStore,
    client: PolymarketClient,
    cfg: Config,
) -> None:
    """Check SL/TP on existing positions without scanning for new entries.

    Used when circuit breaker is active — we still want to close losing
    positions, but we don't want to open new ones.
    """
    tick_ts = iso_now()
    for token_id, pos in list(portfolio.positions.items()):
        current_price = client.get_price(token_id)
        if current_price is None:
            continue
        exit_reason = ""
        if risk_mgr.check_stop_loss(pos.entry_price, current_price):
            exit_reason = "stop_loss"
        elif risk_mgr.check_take_profit(pos.entry_price, current_price):
            exit_reason = "take_profit"
        if not exit_reason:
            continue

        close_side = "SELL" if pos.side == "BUY" else "BUY"
        current_spread = client.get_spread(token_id) or 0.0
        order = OrderRequest(
            token_id=token_id, condition_id=pos.condition_id,
            side=close_side, size=pos.size, price=current_price,
            strategy=pos.strategy, spread=current_spread, exit_reason=exit_reason,
        )
        result = executor.execute(order)
        if result.success:
            pnl = portfolio.close_position(token_id, current_price)
            risk_mgr.record_realized_pnl(pnl)
            store.insert_decision(
                timestamp=tick_ts, token_id=token_id, condition_id=pos.condition_id,
                action=f"EXIT_{exit_reason.upper()}", reason=f"PnL={pnl:.4f} (circuit_breaker_mode)",
                strategy=pos.strategy, price=current_price, spread=current_spread,
            )


def _export_bot_state(
    cfg: Config,
    portfolio: PortfolioTracker,
    risk_mgr: RiskManager,
    strategy: BaseStrategy,
    tick_count: int,
    client: PolymarketClient,
) -> None:
    """Write a JSON file with current bot state for the dashboard to consume."""
    try:
        positions_data = []
        for pos in portfolio.positions.values():
            current_price = client.get_price(pos.token_id)
            positions_data.append({
                "token_id": pos.token_id[:16],
                "condition_id": pos.condition_id[:16],
                "side": pos.side,
                "size": round(pos.size, 4),
                "entry_price": round(pos.entry_price, 4),
                "current_price": round(current_price, 4) if current_price else None,
                "unrealised_pnl": round(pos.unrealised_pnl(current_price), 4) if current_price else None,
                "strategy": pos.strategy,
            })

        state = {
            "timestamp": iso_now(),
            "tick_count": tick_count,
            "mode": "paper" if cfg.is_paper else "live",
            "strategy": strategy.name,
            "circuit_breaker_active": risk_mgr.is_circuit_breaker_active,
            "daily_pnl": round(risk_mgr.daily_pnl, 4),
            "portfolio": {
                "open_positions": portfolio.open_position_count(),
                "total_exposure": round(portfolio.total_exposure(), 2),
                "realised_pnl": round(portfolio.realised_pnl, 4),
            },
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
        with open("bot_state.json", "w") as f:
            json.dump(state, f, indent=2)
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
) -> None:
    """One iteration of the bot loop."""

    tick_start = utc_timestamp()
    tick_ts = iso_now()

    # 1. Check stop-loss / take-profit on existing positions
    tokens_to_close: list[tuple[str, str]] = []  # (token_id, exit_reason)
    for token_id, pos in list(portfolio.positions.items()):
        current_price = client.get_price(token_id)
        if current_price is None:
            logger.debug("No price for position %s — skipping SL/TP check.", token_id[:12])
            continue
        if risk_mgr.check_stop_loss(pos.entry_price, current_price):
            logger.info("Stop-loss triggered for %s (entry=%.4f, current=%.4f)", token_id[:12], pos.entry_price, current_price)
            tokens_to_close.append((token_id, "stop_loss"))
        elif risk_mgr.check_take_profit(pos.entry_price, current_price):
            logger.info("Take-profit triggered for %s (entry=%.4f, current=%.4f)", token_id[:12], pos.entry_price, current_price)
            tokens_to_close.append((token_id, "take_profit"))

    for token_id, exit_reason in tokens_to_close:
        pos = portfolio.positions.get(token_id)
        if pos is None:
            continue
        close_side = "SELL" if pos.side == "BUY" else "BUY"
        current_price = client.get_price(token_id) or pos.entry_price
        current_spread = client.get_spread(token_id) or 0.0
        order = OrderRequest(
            token_id=token_id,
            condition_id=pos.condition_id,
            side=close_side,
            size=pos.size,
            price=current_price,
            strategy=pos.strategy,
            spread=current_spread,
            exit_reason=exit_reason,
        )
        result = executor.execute(order)
        if result.success:
            pnl = portfolio.close_position(token_id, current_price)
            risk_mgr.record_realized_pnl(pnl)
            store.insert_decision(
                timestamp=tick_ts, token_id=token_id, condition_id=pos.condition_id,
                action=f"EXIT_{exit_reason.upper()}", reason=f"PnL={pnl:.4f}",
                strategy=pos.strategy, price=current_price, spread=current_spread,
            )

    # 2. Fetch market snapshots
    snapshots = market_svc.fetch_and_filter()
    logger.info("Evaluating %d market snapshots.", len(snapshots))

    # 3. Evaluate strategy on each snapshot
    signals_generated = 0
    risk_rejections = 0
    trades_executed = 0

    for snap in snapshots:
        if snap.token_id in portfolio.positions:
            continue  # already have a position

        # Record price
        if snap.price is not None:
            store.insert_price(snap.token_id, snap.price, tick_ts)

        history = store.get_price_history(snap.token_id)
        sig = strategy.evaluate(snap, history)

        if sig.action == Action.HOLD:
            continue

        signals_generated += 1

        # 4. Risk check
        proposed_size = cfg.max_position_size / snap.price if snap.price else 0
        verdict = risk_mgr.check(snap.token_id, sig, proposed_size, snap.price or 0, spread=snap.spread or 0.0)
        if not verdict.allowed:
            risk_rejections += 1
            logger.debug("Risk denied for %s: %s", snap.token_id[:12], verdict.reason)
            store.insert_decision(
                timestamp=tick_ts, token_id=snap.token_id, condition_id=snap.condition_id,
                action="RISK_REJECTED", reason=verdict.reason,
                strategy=strategy.name, confidence=sig.confidence,
                price=snap.price or 0, spread=snap.spread or 0,
                signal_detail=sig.reason, risk_detail=verdict.reason,
            )
            continue

        # 5. Execute
        order = OrderRequest(
            token_id=snap.token_id,
            condition_id=snap.condition_id,
            side=sig.action.value,
            size=verdict.adjusted_size,
            price=snap.price or 0,
            strategy=strategy.name,
            spread=snap.spread or 0.0,
        )
        result = executor.execute(order)

        if result.success and sig.action == Action.BUY:
            trades_executed += 1
            portfolio.open_position(
                Position(
                    token_id=snap.token_id,
                    condition_id=snap.condition_id,
                    side="BUY",
                    size=verdict.adjusted_size,
                    entry_price=snap.price or 0,
                    strategy=strategy.name,
                    order_id=result.order_id,
                )
            )
            store.insert_decision(
                timestamp=tick_ts, token_id=snap.token_id, condition_id=snap.condition_id,
                action="ENTRY_BUY", reason=sig.reason,
                strategy=strategy.name, confidence=sig.confidence,
                price=snap.price or 0, spread=snap.spread or 0,
                signal_detail=sig.reason,
            )

    # Log tick summary with timing
    tick_duration = utc_timestamp() - tick_start
    summary = portfolio.summary(price_fn=client.get_price)
    logger.info(
        "Tick complete (%.1fs): markets=%d, signals=%d, risk_rejected=%d, trades=%d, daily_pnl=$%.2f | Portfolio: %s",
        tick_duration, len(snapshots), signals_generated, risk_rejections, trades_executed,
        risk_mgr.daily_pnl, summary,
    )

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
        )
    except Exception:
        logger.debug("Failed to insert tick stats.", exc_info=True)


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
    setup_logging(cfg.log_level, cfg.log_file)
    run_loop(cfg)


@cli.command("backfill-markets")
def cmd_backfill():
    """Download active markets and cache them locally.

    Uses the configured MAX_MARKETS_FETCH cap to avoid fetching tens of
    thousands of markets (Polymarket has 51K+ active markets; fetching all
    of them blocks for a long time and is unnecessary for paper trading).
    """
    cfg = Config()
    setup_logging(cfg.log_level, cfg.log_file)
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


if __name__ == "__main__":
    cli()
