# AI Changelog

All meaningful changes made by the AI Continuous Improvement process.

---

## 2026-04-08 — Initial Audit & Instrumentation

### Created Planning Documents
- `AUDIT_CURRENT_SYSTEM.md` — Full architecture audit with top risks and assumptions
- `CONTINUOUS_IMPROVEMENT_BACKLOG.md` — Prioritized improvement backlog (20 items)
- `RISK_GUARDRAILS.md` — Paper-mode rules, exposure caps, live-trading gates
- `LEARNING_PHASE_PLAN.md` — Data collection plan, weekly review checklist
- `DASHBOARD_IMPROVEMENT_PLAN.md` — Dashboard gaps and required improvements
- `EXPERIMENT_FRAMEWORK.md` — Experiment naming, acceptance/rejection criteria
- `CHANGELOG_AI.md` — This file

**Why**: Establish a clear audit trail and improvement roadmap before making code changes.

### Code Changes

#### 1. Added spread filter enforcement in market_data.py
- **What**: `_passes_filters()` now checks `snap.spread` against `cfg.max_spread`
- **Why**: `MAX_SPREAD` was defined in config but never enforced — bot could enter illiquid markets
- **Risk**: Low — this is a conservative filter that only prevents bad trades
- **Validation**: Markets with spread > MAX_SPREAD are now excluded

#### 2. Added decision logging table and skip journal (sqlite_store.py)
- **What**: New `decision_log` table storing every trade decision with reason codes
- **Why**: Cannot learn from paper trading without knowing what was considered and rejected
- **Risk**: Low — additive change, no existing behavior modified
- **Validation**: Check decision_log table after a tick cycle

#### 3. Added exit_reason and spread_at_entry to trades table (sqlite_store.py)
- **What**: New columns `exit_reason` and `spread_at_entry` on trades table
- **Why**: Cannot analyze SL/TP effectiveness without knowing exit reasons; cannot assess execution realism without spread data
- **Risk**: Low — ALTER TABLE ADD COLUMN with defaults, backwards compatible
- **Validation**: New trades have exit_reason and spread_at_entry populated

#### 4. Added portfolio reconstruction from DB on startup (portfolio/tracker.py)
- **What**: `reconstruct_from_trades()` rebuilds open positions from trade history
- **Why**: Portfolio was in-memory only — all positions lost on restart
- **Risk**: Low — additive method, existing open_position/close_position unchanged
- **Validation**: Stop and restart bot, verify positions are restored

#### 5. Instrumented _tick() with decision logging (main.py)
- **What**: Each trade decision (entry, exit, skip) is logged to decision_log table
- **Why**: Provides the audit trail needed for learning phase analysis
- **Risk**: Low — logging is additive, does not change execution flow
- **Validation**: Check decision_log table after running a tick

#### 6. Added market fetch limiting to avoid fetching all 51K+ markets (market_data.py)
- **What**: `fetch_and_filter()` now paginates with an early-stop limit (configurable `MAX_MARKETS_FETCH`)
- **Why**: Bot was making 500+ API calls per tick fetching all active markets
- **Risk**: Low — conservative default limit, configurable
- **Validation**: Measure tick duration, check log for market count

#### 7. Added paper slippage simulation (execution.py)
- **What**: Paper fills now apply half-spread as slippage cost to entry/exit prices
- **Why**: Paper PnL was systematically overstated vs reality
- **Risk**: Medium — changes paper PnL calculation; old behavior preserved if spread=0
- **Validation**: Compare paper PnL before/after, verify spread adjustment is applied

#### 8. Hardened risk manager with spread check and duplicate prevention (risk/manager.py)
- **What**: Risk check now rejects trades with excessive spread; prevents duplicate entries for same token
- **Why**: Missing spread gate allowed illiquid trades; no duplicate prevention existed
- **Risk**: Low — additional safety checks, no existing checks removed
- **Validation**: Verify high-spread signals are rejected; verify duplicate signals are blocked

---

## 2026-04-08 — Risk Hardening & Dashboard Upgrade (Phase 2)

### Code Changes

#### 9. Daily max-loss circuit breaker (risk/manager.py)
- **What**: `RiskManager` now tracks daily realized PnL; trips a circuit breaker when loss exceeds `MAX_DAILY_LOSS`; resets at midnight
- **Why**: Without a circuit breaker, a bad day could blow through all paper capital. Essential for live readiness.
- **Risk**: Low — only blocks new BUY orders; still allows SL/TP exits on existing positions
- **Validation**: 5 new tests verify circuit breaker behavior

#### 10. Per-event concentration limits (risk/manager.py, portfolio/tracker.py)
- **What**: Risk check now enforces `MAX_EXPOSURE_PER_EVENT` — total exposure per condition_id. Added `exposure_by_condition()` to PortfolioTracker.
- **Why**: Bot could load up on multiple YES tokens from the same event, creating hidden concentration risk
- **Risk**: Low — additive check, no existing behavior changed
- **Validation**: Tests verify same-event rejection and cross-event allowance

#### 11. Price boundary filter (risk/manager.py, market_data.py)
- **What**: Rejects markets with price < `MIN_PRICE` (0.05) or > `MAX_PRICE` (0.95)
- **Why**: Near-zero and near-certain markets have minimal edge but high risk of resolution surprise
- **Risk**: Low — conservative defaults, configurable
- **Validation**: 5 new tests including exact boundary values

#### 12. Kill-switch file mechanism (main.py)
- **What**: Bot checks for `KILL_SWITCH` file at start of each tick; shuts down immediately if found
- **Why**: Provides an emergency stop without needing to find the process ID; works across SSH sessions
- **Risk**: None — only adds a file existence check
- **Validation**: Manual: `touch KILL_SWITCH` stops the bot

#### 13. Tick timing instrumentation (main.py, sqlite_store.py)
- **What**: Each tick is timed; `tick_stats` table records duration, markets scanned, signals, rejections, trades, portfolio state
- **Why**: Enables performance monitoring and anomaly detection
- **Risk**: Low — additive logging, no execution change
- **Validation**: Check tick_stats table after running

#### 14. Bot state JSON export (main.py)
- **What**: `bot_state.json` written after each tick with live portfolio, config, positions, circuit breaker status
- **Why**: Dashboard can read actual bot state instead of hardcoding values
- **Risk**: None — write-only side effect, ignored by git
- **Validation**: Check bot_state.json after running; dashboard reads it

#### 15. Dashboard upgrade (generate_dashboard.js)
- **What**: Dashboard now reads `bot_state.json` for real config; shows open positions with unrealized PnL; shows spread-adjusted PnL; shows decision log stats (SL/TP exits, risk rejections); shows circuit breaker status
- **Why**: Dashboard was lying about config and missing critical views
- **Risk**: Low — backwards compatible, falls back to defaults if bot_state.json is missing
- **Validation**: Generate dashboard, visually inspect new sections

#### 16. Category and end_date extraction (market_data.py)
- **What**: MarketSnapshot now captures `category` and `end_date` from Gamma API data
- **Why**: Foundation for future category-based analysis and time-to-resolution filtering
- **Risk**: None — additive fields with empty defaults
- **Validation**: Check snapshot objects have category/end_date populated

#### 17. New config parameters
- `MAX_DAILY_LOSS` (default $50) — circuit breaker threshold
- `MAX_EXPOSURE_PER_EVENT` (default $100) — per-event concentration limit
- `MIN_PRICE` (default 0.05) — minimum price to trade
- `MAX_PRICE` (default 0.95) — maximum price to trade
- `STALE_PRICE_SECONDS` (default 300) — stale price threshold
- `KILL_SWITCH_FILE` (default "KILL_SWITCH") — kill switch file path

### New Tests (16 tests)
- test_risk_hardening.py: Circuit breaker (5), concentration limits (2), price boundaries (5), duplicate prevention (2), spread check (2)

### Test Results
- 68 total tests: ALL PASSING (28 original + 24 from phase 1 + 16 new)

---

## 2026-04-14 — Semantic engine dashboard visibility

### Code Changes

#### 18. `SQLiteStore.semantic_signals_summary()` (storage/sqlite_store.py)
- **What**: Read-only aggregate over the `semantic_signals` table: total
  count, BUY/SELL split, counts by `synthetic_method`, counts by `mode`,
  average score / net edge / synthetic confidence, last timestamp. Survives
  a missing table (returns zero-shape instead of raising).
- **Why**: The engine persists every detection but nothing read it back.
  The docs explicitly required inspecting method mix / score / net edge
  distribution before promoting to live mode — there was no way to do
  that without hand-written SQL.
- **Risk**: None — purely additive read method.
- **Validation**: 4 new tests covering empty DB, aggregation, limit
  respect, and missing-table fallback.

#### 19. Semantic summary exported in `bot_state.json` (main.py)
- **What**: `_export_bot_state` now accepts the `SQLiteStore` and, when the
  engine is enabled OR when any signals already exist in the DB, writes a
  `semantic` block with `config` (enabled / mode / thresholds) and
  `summary` (the aggregate from above). Errors inside the summary call are
  swallowed so the dashboard export never breaks the tick.
- **Why**: Dashboard must reflect backend truth — the operator should see
  the live thresholds and the effect of the shadow observer.
- **Risk**: Low — store is passed as an optional arg; callers without the
  store get the pre-existing behaviour.
- **Validation**: Existing no-regression tick test still passes.

#### 20. Dashboard `Semantic Mispricing Engine` section (generate_dashboard.js)
- **What**: New dashboard section — hidden when there are no signals and
  the engine is disabled — showing engine state, detection counters,
  average quality metrics, synthetic-method mix, and a 20-row table of
  recent detections with bid/ask/fair/edge/score/confidence/mode. Nav
  entry also gated on the same condition.
- **Why**: Gives the operator an at-a-glance view of shadow-observer
  output before any decision to promote the engine to live. Closes the
  gap between "detections are persisted" and "detections are reviewable".
- **Risk**: Low — section is gated on `semanticStats || semanticCfg`; old
  DBs render identically to before.
- **Validation**: `node -c generate_dashboard.js` passes; visual
  inspection planned next run.

### New Tests (4 tests)
- test_sqlite_store.py::TestSemanticSignalsSummary:
  empty shape, aggregation across sides/methods/modes, limit respected,
  missing-table fallback.

### Test Results
- 375 total tests: ALL PASSING (371 previous + 4 new)
