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
