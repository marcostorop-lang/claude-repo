"""
Risk manager.

Enforces position sizing, exposure limits, stop-loss, take-profit,
daily loss circuit breaker, concentration limits, and price boundary rules
before any order is sent to the execution engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from src.config import Config
from src.portfolio.tracker import PortfolioTracker
from src.strategy.base import Action, Signal

logger = logging.getLogger(__name__)


@dataclass
class RiskVerdict:
    allowed: bool
    adjusted_size: float
    reason: str


class RiskManager:
    """Gate-keeper that validates and adjusts proposed trades."""

    def __init__(self, cfg: Config, portfolio: PortfolioTracker) -> None:
        self.cfg = cfg
        self.portfolio = portfolio
        # Daily loss tracking — anchored to UTC so the reset boundary is
        # the same regardless of where the host runs.  ``date.today()``
        # would silently drift the cutoff by the local timezone offset.
        self._daily_realized_pnl: float = 0.0
        self._current_date: date = datetime.now(timezone.utc).date()
        self._circuit_breaker_tripped: bool = False
        # Optional temporal-edge filter (see ``src/analysis/temporal_edge.py``).
        # Left ``None`` by default; the main loop constructs one and attaches
        # it when ``TEMPORAL_FILTER_ENABLED=true``.  Risk checks treat
        # ``None`` as "feature off" — zero runtime cost when disabled.
        self.temporal_filter = None
        # Optional price-history loader for the volatility filter.  Signature:
        # ``(token_id, limit) -> list[float]`` (oldest→newest).  None disables.
        self.get_price_history = None
        # Optional Bayesian calibrator (see
        # ``src/analysis/bayesian_calibrator.py``).  When attached *and*
        # ``BAYESIAN_SIZING_ENABLED=true`` is set, ``compute_position_size``
        # applies a posterior-mean multiplier for the strategy that
        # produced the signal.  ``None`` means the feature is off; zero
        # runtime cost in that case.
        self.bayesian_calibrator = None
        # Optional live-wallet balance provider (see
        # ``src/risk/wallet_balance.py``).  When attached, ``check`` will
        # refuse a BUY whose cost exceeds wallet USDC.  ``None`` =>
        # feature off (paper mode never builds one).
        self.wallet_balance_provider = None
        # Optional first-N live-trades autopause gate (see
        # ``src/risk/live_autopause.py``).  ``None`` => feature off.
        self.live_autopause_gate = None
        # Regime-shift autopause.  Flipped True by the periodic
        # detector when a coordinated market-wide move is observed;
        # flipped back when the detector next reports calm.  Not
        # persisted across restarts on purpose — a transient shift
        # shouldn't halt the bot forever on the next launch.
        self.regime_paused: bool = False
        self.regime_pause_reason: str = ""

    # ------------------------------------------------------------------
    # Daily loss tracking
    # ------------------------------------------------------------------

    def record_realized_pnl(self, pnl: float) -> None:
        """Track realized PnL for daily circuit breaker."""
        self._maybe_reset_daily()
        self._daily_realized_pnl += pnl
        if self._daily_realized_pnl <= -self.cfg.max_daily_loss:
            self._circuit_breaker_tripped = True
            logger.warning(
                "CIRCUIT BREAKER TRIPPED: daily loss $%.2f exceeds limit $%.2f",
                abs(self._daily_realized_pnl), self.cfg.max_daily_loss,
            )

    def seed_daily_pnl(self, pnl_today: float) -> None:
        """Seed the daily PnL accumulator from reconstructed history.

        The bot rebuilds ``PortfolioTracker`` from the ``trades`` table
        at startup (see :meth:`PortfolioTracker.reconstruct_from_trades`)
        — but without this hook ``_daily_realized_pnl`` would start at
        zero on every restart.  That's a real safety bug: a bot that
        crashed after losing $40 against a $50 daily-loss limit would
        come back with the full $50 of headroom again, defeating the
        circuit breaker.

        Idempotent under repeated calls on the same UTC day (later
        calls overwrite, they don't stack), because the bot loop calls
        this exactly once at startup with the authoritative figure
        computed from trade history.  If that figure already breaches
        the limit, the breaker trips immediately — the restarting bot
        refuses to open new positions until a new UTC day begins,
        matching the behaviour it would have had if the crash had not
        happened.
        """
        self._maybe_reset_daily()
        self._daily_realized_pnl = float(pnl_today)
        if self._daily_realized_pnl <= -self.cfg.max_daily_loss:
            self._circuit_breaker_tripped = True
            logger.warning(
                "CIRCUIT BREAKER TRIPPED on startup: reconstructed daily loss "
                "$%.2f exceeds limit $%.2f",
                abs(self._daily_realized_pnl), self.cfg.max_daily_loss,
            )
        else:
            logger.info(
                "Daily PnL seeded from trade history: $%+.2f (limit: -$%.2f).",
                self._daily_realized_pnl, self.cfg.max_daily_loss,
            )

    def _maybe_reset_daily(self) -> None:
        """Reset daily counters at UTC midnight."""
        today = datetime.now(timezone.utc).date()
        if today != self._current_date:
            if self._circuit_breaker_tripped:
                logger.info("Circuit breaker reset for new day.")
            self._daily_realized_pnl = 0.0
            self._current_date = today
            self._circuit_breaker_tripped = False

    @property
    def is_circuit_breaker_active(self) -> bool:
        self._maybe_reset_daily()
        return self._circuit_breaker_tripped

    @property
    def daily_pnl(self) -> float:
        self._maybe_reset_daily()
        return self._daily_realized_pnl

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Dynamic position sizing
    # ------------------------------------------------------------------

    def compute_position_size(
        self,
        price: float,
        confidence: float,
        liquidity: float = 0.0,
        edge: float | None = None,
        book_depth_usd: float = 0.0,
        strategy: str = "",
        end_date: str = "",
    ) -> float:
        """Compute the proposed position size in shares.

        Base size = max_position_size / price, then optionally scaled by:
        - Edge-aware fractional-Kelly (when ``edge`` is supplied and
          ``SIZING_EDGE_KELLY=true``): size *= |edge| * confidence * kelly_fraction
          capped at 1.0 to avoid oversizing.
        - Signal confidence (linear: size *= confidence)
          — skipped when edge-Kelly is active (edge-Kelly already uses confidence).
        - Available liquidity (cap at max_liquidity_fraction of reported liquidity).
        - Real order-book depth when provided (cap at
          ``MAX_BOOK_DEPTH_FRACTION * book_depth_usd``) — this is *more*
          accurate than the ``liquidity`` cap because it uses the actual
          fillable USD within 5% of midpoint on the relevant side.
        """
        if price <= 0:
            return 0.0
        base_usd = self.cfg.max_position_size

        # Exact Kelly sizing — opt-in, takes precedence when enabled and
        # a signed edge is available.  Uses the closed-form
        # f* = (p - price) / (1 - price) for a binary YES bet, then
        # scaled by KELLY_FRACTION (for retail safety margin) and by
        # confidence (for model-uncertainty discounting).  A negative
        # or zero edge collapses to size 0 — we simply won't trade.
        if getattr(self.cfg, "sizing_kelly_proper", False) and edge is not None:
            from src.analysis.kelly import kelly_fraction as _kelly
            f_star = _kelly(price=price, edge=edge)
            kelly_mult = (
                f_star * max(confidence, 0.0) * self.cfg.kelly_fraction
            )
            kelly_mult = min(max(kelly_mult, 0.0), 1.0)
            base_usd *= kelly_mult
            logger.debug(
                "Proper-Kelly sizing: price=%.4f edge=%+.4f f*=%.4f "
                "conf=%.3f frac=%.2f → mult=%.4f (base=$%.2f)",
                price, edge, f_star, confidence,
                self.cfg.kelly_fraction, kelly_mult, base_usd,
            )
        # Edge-aware Kelly sizing takes precedence when enabled and edge is known.
        # For prediction markets, edge ≈ P_estimated - P_market.  The Kelly
        # fraction for a binary bet with known edge and true probability p is
        # f* = (p*b - q) / b where b is net odds = 1/price - 1.
        # As a well-behaved proxy we use |edge| * confidence * kelly_fraction,
        # which is linear in edge and trivially bounded in [0, 1].
        elif self.cfg.sizing_edge_kelly and edge is not None and abs(edge) > 0:
            # Pure edge-Kelly: size = base * min(|edge| * confidence * kelly_fraction, 1)
            # Tiny edges → tiny positions; filter via MIN_EDGE_FOR_TRADE if
            # execution costs would swamp them.
            kelly_mult = min(abs(edge) * max(confidence, 0.0) * self.cfg.kelly_fraction, 1.0)
            base_usd *= kelly_mult
            logger.debug(
                "Edge-Kelly sizing: edge=%+.4f conf=%.3f frac=%.2f → mult=%.4f (base=$%.2f)",
                edge, confidence, self.cfg.kelly_fraction, kelly_mult, base_usd,
            )
        elif self.cfg.sizing_confidence_scale and confidence > 0:
            # Legacy confidence-only scaling
            base_usd *= min(confidence, 1.0)

        # Liquidity cap: never take more than X% of reported market liquidity
        if liquidity > 0 and self.cfg.max_liquidity_fraction > 0:
            max_usd_from_liq = liquidity * self.cfg.max_liquidity_fraction
            if base_usd > max_usd_from_liq:
                logger.debug(
                    "Sizing capped by liquidity: $%.2f -> $%.2f (%.1f%% of $%.0f)",
                    base_usd, max_usd_from_liq,
                    self.cfg.max_liquidity_fraction * 100, liquidity,
                )
                base_usd = max_usd_from_liq

        # Book-depth cap: stricter than the liquidity cap because it uses
        # the actual fillable USD within 5% of midpoint on the relevant
        # side, as measured from the current order book.  Only applied
        # when the operator has opted in and a depth value was passed.
        depth_cap = getattr(self.cfg, "max_book_depth_fraction", 0.0) or 0.0
        if book_depth_usd > 0 and depth_cap > 0:
            max_usd_from_depth = book_depth_usd * depth_cap
            if base_usd > max_usd_from_depth:
                logger.debug(
                    "Sizing capped by book depth: $%.2f -> $%.2f "
                    "(%.0f%% of $%.0f depth_5pct)",
                    base_usd, max_usd_from_depth,
                    depth_cap * 100, book_depth_usd,
                )
                base_usd = max_usd_from_depth

        # Bayesian posterior multiplier: applied *last*, strictly in
        # ``[min_mult, 1.0]`` so it can only shrink a strategy's size
        # (never amplify it).  Cold-start returns 1.0, so this branch
        # is a transparent no-op until enough evidence has accrued.
        if (
            getattr(self.cfg, "bayesian_sizing_enabled", False)
            and self.bayesian_calibrator is not None
            and strategy
        ):
            try:
                mult = self.bayesian_calibrator.size_multiplier(strategy)
            except Exception:
                logger.exception(
                    "Bayesian size_multiplier failed — skipping shrinkage.",
                )
                mult = 1.0
            if mult < 1.0:
                logger.debug(
                    "Bayesian sizing: strategy='%s' mult=%.3f base=$%.2f → $%.2f",
                    strategy, mult, base_usd, base_usd * mult,
                )
            base_usd *= mult

        # Capital-efficiency factor — opt-in shrinkage for *long-dated*
        # markets.  A 60-day market with the same per-share edge as a
        # 6-day market earns the same dollars but ties capital up 10x
        # longer; this factor caps long-dated bets at
        # ``target_days / days_to_resolution`` (floored at min_factor).
        # Short-dated markets and missing/unparseable end_date → no
        # change (factor = 1.0), keeping the path fail-safe.
        if (
            getattr(self.cfg, "sizing_capital_efficiency_enabled", False)
            and end_date
        ):
            from src.utils.time_utils import capital_efficiency_factor
            cap_factor = capital_efficiency_factor(
                end_date,
                target_days=self.cfg.sizing_capital_efficiency_target_days,
                min_factor=self.cfg.sizing_capital_efficiency_min_factor,
            )
            if cap_factor < 1.0:
                logger.debug(
                    "Capital-efficiency: end_date=%s factor=%.3f base=$%.2f → $%.2f",
                    end_date, cap_factor, base_usd, base_usd * cap_factor,
                )
            base_usd *= cap_factor

        # Dry-run live cap: hard ceiling on USD notional for live orders.
        live_cap = getattr(self.cfg, "live_trade_max_position_usd", 0.0) or 0.0
        if live_cap > 0 and self.cfg.is_live and base_usd > live_cap:
            logger.info(
                "Dry-run cap: $%.2f → $%.2f (LIVE_TRADE_MAX_POSITION_USD)",
                base_usd, live_cap,
            )
            base_usd = live_cap

        return base_usd / price

    # Pre-trade risk check
    # ------------------------------------------------------------------

    def check(
        self,
        token_id: str,
        signal: Signal,
        proposed_size: float,
        price: float,
        spread: float = 0.0,
        category: str = "",
    ) -> RiskVerdict:
        """Evaluate whether a trade should proceed and at what size."""

        # HOLD signals need no risk check
        if signal.action == Action.HOLD:
            return RiskVerdict(False, 0.0, "HOLD signal — no trade.")

        # --- Circuit breaker ---
        if self.is_circuit_breaker_active and signal.action == Action.BUY:
            return RiskVerdict(False, 0.0, f"Circuit breaker: daily loss ${abs(self._daily_realized_pnl):.2f} exceeds limit.")

        # --- Temporal edge filter ---
        # Only gates new BUYs — SELL exits always fire (don't strand
        # positions because the hour is "wrong").  Cold-start is
        # fail-safe: the filter returns True when insufficient data.
        if signal.action == Action.BUY and self.temporal_filter is not None:
            if not self.temporal_filter.is_hour_allowed():
                from datetime import datetime, timezone
                h = datetime.now(timezone.utc).hour
                return RiskVerdict(
                    False, 0.0,
                    f"Temporal filter: hour {h:02d} UTC below "
                    f"win-rate threshold {self.cfg.temporal_min_winrate:.2f}.",
                )

        # --- Volatility filter ---
        # Reject BUYs on tokens whose recent price stddev exceeds the
        # threshold.  Cold-start fail-safe: insufficient history → allow.
        # Never gates SELLs — always lets us exit a stale/chaotic market.
        if (
            signal.action == Action.BUY
            and getattr(self.cfg, "volatility_filter_enabled", False)
            and self.get_price_history is not None
        ):
            try:
                from src.analysis.volatility import is_too_volatile
                prices = self.get_price_history(
                    token_id, limit=self.cfg.volatility_window,
                ) or []
                too_vol, measured = is_too_volatile(
                    prices,
                    window=self.cfg.volatility_window,
                    max_vol=self.cfg.max_price_volatility,
                )
                if too_vol:
                    return RiskVerdict(
                        False, 0.0,
                        f"Volatility {measured:.4f} exceeds max "
                        f"{self.cfg.max_price_volatility:.4f} "
                        f"(window={self.cfg.volatility_window}).",
                    )
            except Exception:
                # Fail open — never let a loader bug freeze trading.
                logger.exception("Volatility filter errored — allowing trade.")

        # --- Anti-pump filter ---
        # Reject BUYs into tokens that moved sharply in recent ticks.
        # Cold-start safe: insufficient history → allow.
        if (
            signal.action == Action.BUY
            and getattr(self.cfg, "anti_pump_enabled", False)
            and self.get_price_history is not None
        ):
            try:
                from src.risk.anti_pump import check_pump
                prices = self.get_price_history(
                    token_id,
                    limit=self.cfg.anti_pump_window_points + 1,
                ) or []
                pump_v = check_pump(
                    prices,
                    window_points=self.cfg.anti_pump_window_points,
                    threshold=self.cfg.anti_pump_threshold,
                )
                if pump_v.blocked:
                    return RiskVerdict(False, 0.0, f"Anti-pump: {pump_v.reason}")
            except Exception:
                logger.exception("Anti-pump filter errored — allowing trade.")

        # --- Duplicate position prevention ---
        if signal.action == Action.BUY and token_id in self.portfolio.positions:
            return RiskVerdict(False, 0.0, "Already have an open position for this token.")

        # --- Minimum edge gate (only when strategy reports an edge) ---
        # The edge-based strategy publishes the signed edge in features.
        # If configured, reject trades below the threshold even if confidence
        # is high (e.g., high confidence of a 0.005 edge is not worth trading).
        # When MIN_EDGE_BY_CATEGORY is populated, the per-category value
        # overrides the global — categories with noisier resolution or
        # wider spreads can require a bigger edge without dragging the
        # global threshold up for everyone.
        effective_min_edge = (
            self.cfg.effective_min_edge(category)
            if hasattr(self.cfg, "effective_min_edge")
            else self.cfg.min_edge_for_trade
        )
        if effective_min_edge > 0:
            sig_edge = signal.features.get("edge") if signal.features else None
            if sig_edge is not None:
                try:
                    if abs(float(sig_edge)) < effective_min_edge:
                        return RiskVerdict(
                            False, 0.0,
                            f"Edge {float(sig_edge):+.4f} below min {effective_min_edge:.4f} "
                            f"(category='{category}').",
                        )
                except (TypeError, ValueError):
                    pass

        # --- Spread check ---
        # Two-part gate:
        #   1. If a positive spread is supplied, enforce ``MAX_SPREAD``.
        #   2. If the spread is unknown (<= 0) on a BUY and the operator
        #      hasn't explicitly opted out, refuse the trade.  Without
        #      this fallback, any caller that forgets to compute spread
        #      silently bypasses the cap — the "looks safe, isn't" hole.
        if spread > 0 and spread > self.cfg.max_spread:
            return RiskVerdict(False, 0.0, f"Spread {spread:.4f} exceeds max {self.cfg.max_spread:.4f}.")
        if (
            signal.action == Action.BUY
            and spread <= 0
            and getattr(self.cfg, "require_known_spread_for_buy", True)
        ):
            return RiskVerdict(
                False, 0.0,
                "Spread unknown — refusing BUY (set REQUIRE_KNOWN_SPREAD_FOR_BUY=false to override).",
            )

        # --- Price boundary filter ---
        if signal.action == Action.BUY and price > 0:
            if price < self.cfg.min_price:
                return RiskVerdict(False, 0.0, f"Price {price:.4f} below min {self.cfg.min_price:.4f} (near-zero, low edge).")
            if price > self.cfg.max_price:
                return RiskVerdict(False, 0.0, f"Price {price:.4f} above max {self.cfg.max_price:.4f} (near-certain, low edge).")

        # --- Max open positions ---
        # When NET_PAIRED_LEGS is on, count distinct *events* (condition_ids)
        # not distinct tokens — a second BUY on the other side of a binary
        # Yes/No is a cap-lock, not a new independent position.  Also, if
        # the pending BUY would *add* to a paired leg, it doesn't consume
        # a new slot.
        if signal.action == Action.BUY:
            paired_leg = False
            condition_id = signal.features.get("condition_id") if signal.features else None
            if getattr(self.cfg, "net_paired_legs", False):
                open_count = self.portfolio.event_slot_count()
                if condition_id:
                    paired_leg = self.portfolio.is_paired_leg_buy(condition_id, token_id)
                # A paired leg does not consume a new slot
                if not paired_leg and open_count >= self.cfg.max_open_positions:
                    return RiskVerdict(
                        False, 0.0,
                        f"Max open events ({self.cfg.max_open_positions}) reached "
                        f"[net_paired_legs].",
                    )
            else:
                open_count = self.portfolio.open_position_count()
                if open_count >= self.cfg.max_open_positions:
                    return RiskVerdict(False, 0.0, f"Max open positions ({self.cfg.max_open_positions}) reached.")

        # --- Per-event concentration limit ---
        if signal.action == Action.BUY:
            if getattr(self.cfg, "net_paired_legs", False):
                # Net notionals: a cap-locked pair has near-zero net
                # directional exposure, so letting both legs through is
                # correct.  Fallback to gross when no condition_id is known.
                cid = (signal.features.get("condition_id") if signal.features else None) or token_id
                event_exposure = self.portfolio.net_exposure_by_condition(cid)
            else:
                event_exposure = self.portfolio.exposure_by_condition(token_id)
            if event_exposure >= self.cfg.max_exposure_per_event:
                return RiskVerdict(False, 0.0, f"Event exposure ${event_exposure:.2f} exceeds limit ${self.cfg.max_exposure_per_event:.2f}.")

        # --- Cross-event category correlation limit ---
        if signal.action == Action.BUY and category:
            cat_exposure = self.portfolio.exposure_by_category(category)
            if cat_exposure >= self.cfg.max_exposure_per_category:
                return RiskVerdict(
                    False, 0.0,
                    f"Category '{category}' exposure ${cat_exposure:.2f} exceeds limit ${self.cfg.max_exposure_per_category:.2f}.",
                )
            cat_count = self.portfolio.position_count_by_category(category)
            if cat_count >= self.cfg.max_positions_per_category:
                return RiskVerdict(
                    False, 0.0,
                    f"Category '{category}' already has {cat_count} positions (max {self.cfg.max_positions_per_category}).",
                )

        # --- Position size cap ---
        size = min(proposed_size, self.cfg.max_position_size)

        # --- Total exposure cap ---
        current_exposure = self.portfolio.total_exposure()
        cost = size * price
        if current_exposure + cost > self.cfg.max_total_exposure:
            available = self.cfg.max_total_exposure - current_exposure
            if available <= 0:
                return RiskVerdict(False, 0.0, "Max total exposure reached.")
            size = available / price
            logger.info("Reduced size to %.4f to stay within exposure limit.", size)

        if size <= 0:
            return RiskVerdict(False, 0.0, "Effective size is zero.")

        # --- Live wallet balance gate ---
        # Belt-and-braces check: even if the exposure cap fits, refuse
        # the order when the wallet itself can't cover the cost.  Only
        # active in live mode (paper builds no provider).  Fail-safe:
        # an unreadable balance allows the trade (preflight is the
        # primary gate; this is the runtime backup).
        if (
            signal.action == Action.BUY
            and self.wallet_balance_provider is not None
            and getattr(self.cfg, "wallet_balance_check_enabled", True)
        ):
            cost_usd = size * price
            verdict_w = self.wallet_balance_provider.check_can_afford(cost_usd)
            if not verdict_w.allowed:
                return RiskVerdict(False, 0.0, verdict_w.reason)

        # --- Regime-shift autopause ---
        # When the detector flips ``regime_paused`` on, new BUYs are
        # refused until the next detector run reports calm.  SELLs are
        # allowed so positions can be closed during a shift.
        if signal.action == Action.BUY and self.regime_paused:
            return RiskVerdict(
                False, 0.0,
                f"Regime shift active — {self.regime_pause_reason}",
            )

        # --- First-N live-trades autopause gate ---
        # Refuse BUYs once the operator-defined threshold of live fills
        # has been executed, until an ack file is dropped in the working
        # directory.  SELLs are never gated so a paused bot can still
        # close whatever it opened.
        if (
            signal.action == Action.BUY
            and self.live_autopause_gate is not None
        ):
            verdict_ap = self.live_autopause_gate.check_buy_allowed()
            if not verdict_ap.allowed:
                return RiskVerdict(False, 0.0, verdict_ap.reason)

        return RiskVerdict(True, size, "Risk check passed.")

    # ------------------------------------------------------------------
    # Stop-loss / Take-profit
    # ------------------------------------------------------------------

    def check_stop_loss(self, entry_price: float, current_price: float) -> bool:
        """Return True if current loss exceeds stop-loss threshold."""
        if entry_price == 0:
            return False
        loss_pct = (entry_price - current_price) / entry_price
        return loss_pct >= self.cfg.stop_loss_pct

    def check_take_profit(self, entry_price: float, current_price: float) -> bool:
        """Return True if current gain exceeds take-profit threshold."""
        if entry_price == 0:
            return False
        gain_pct = (current_price - entry_price) / entry_price
        return gain_pct >= self.cfg.take_profit_pct
