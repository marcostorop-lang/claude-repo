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
    return SimpleMomentum(cfg)


def _accrue_fill_fee(
    cfg: Config,
    portfolio: PortfolioTracker,
    filled_size: float,
    fill_price: float,
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
    portfolio.record_fee(compute_fee_usd(cfg, notional, is_maker=False))


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
    strategy = _build_strategy(cfg, store=store)

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
        elif cfg.exit_on_edge_flip and pos.strategy == "edge_based":
            try:
                from src.analysis.edge import estimate_edge
                history = store.get_price_history(token_id, limit=30)
                if len(history) >= 4:
                    spread = client.get_spread(token_id) or 0.0
                    est = estimate_edge(
                        price=current_price, price_history=history, spread=spread,
                    )
                    threshold = cfg.exit_edge_flip_threshold
                    flipped = (
                        (pos.side == "BUY" and est.edge < -threshold) or
                        (pos.side == "SELL" and est.edge > threshold)
                    )
                    if flipped and est.edge_confidence > 0.3:
                        exit_reason = "edge_flip"
            except Exception:
                logger.debug("Edge-flip check failed (circuit-breaker mode) for %s",
                             token_id[:12], exc_info=True)
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
            entry_price = pos.entry_price
            fill_size = result.filled_size if result.filled_size > 0 else pos.size
            fill_px = result.fill_price if result.fill_price > 0 else current_price
            _accrue_fill_fee(cfg, portfolio, fill_size, fill_px)
            pnl = portfolio.close_position(token_id, current_price)
            risk_mgr.record_realized_pnl(pnl)
            return_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
            store.insert_decision(
                timestamp=tick_ts, token_id=token_id, condition_id=pos.condition_id,
                action=f"EXIT_{exit_reason.upper()}", reason=f"PnL={pnl:.4f} (circuit_breaker_mode)",
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

    # 1. Check stop-loss / take-profit / edge-flip on existing positions
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
        elif cfg.exit_on_edge_flip and pos.strategy == "edge_based":
            # Re-estimate edge on the live position.  If it's flipped against
            # us with meaningful magnitude, exit before we hit stop-loss —
            # limits losses when the original thesis is invalidated.
            try:
                from src.analysis.edge import estimate_edge
                history = store.get_price_history(token_id, limit=30)
                if len(history) >= 4:
                    spread = client.get_spread(token_id) or 0.0
                    est = estimate_edge(
                        price=current_price,
                        price_history=history,
                        spread=spread,
                    )
                    threshold = cfg.exit_edge_flip_threshold
                    # BUY position + edge now strongly negative → exit
                    flipped = (
                        (pos.side == "BUY" and est.edge < -threshold) or
                        (pos.side == "SELL" and est.edge > threshold)
                    )
                    if flipped and est.edge_confidence > 0.3:
                        logger.info(
                            "Edge-flip exit for %s (entry=%.4f, current=%.4f, "
                            "new edge=%+.4f, conf=%.2f)",
                            token_id[:12], pos.entry_price, current_price,
                            est.edge, est.edge_confidence,
                        )
                        tokens_to_close.append((token_id, "edge_flip"))
            except Exception:
                logger.debug("Edge-flip check failed for %s", token_id[:12], exc_info=True)

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
            entry_price = pos.entry_price
            fill_size = result.filled_size if result.filled_size > 0 else pos.size
            fill_px = result.fill_price if result.fill_price > 0 else current_price
            _accrue_fill_fee(cfg, portfolio, fill_size, fill_px)
            pnl = portfolio.close_position(token_id, current_price)
            risk_mgr.record_realized_pnl(pnl)
            return_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
            store.insert_decision(
                timestamp=tick_ts, token_id=token_id, condition_id=pos.condition_id,
                action=f"EXIT_{exit_reason.upper()}", reason=f"PnL={pnl:.4f}",
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

    # 2. Fetch market snapshots
    snapshots = market_svc.fetch_and_filter()
    logger.info("Evaluating %d market snapshots.", len(snapshots))

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
        proposed_size = risk_mgr.compute_position_size(
            price=snap.price or 0,
            confidence=sig.confidence,
            liquidity=snap.liquidity,
            edge=sig_edge,
        )
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
                features=sig.features,
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
            # 2% slippage means the fill price is 2% worse than best bid/ask.
            max_slippage = 0.02
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

        # 7. Execute
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
        )
        result = executor.execute(order)

        if result.success and sig.action == Action.BUY:
            trades_executed += 1
            # Use actual filled size (may be partial if book depth < requested)
            actual_size = result.filled_size if result.filled_size > 0 else verdict.adjusted_size
            actual_fill = result.fill_price if result.fill_price > 0 else exec_price
            _accrue_fill_fee(cfg, portfolio, actual_size, actual_fill)
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


if __name__ == "__main__":
    cli()
