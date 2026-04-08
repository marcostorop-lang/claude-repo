# Audit: Current System State

**Date**: 2026-04-08
**Auditor**: AI Continuous Improvement Lead
**Scope**: Full codebase, database, logs, dashboard, tests

---

## 1. Architecture Summary

### Bot Core (`src/`)
```
src/
  main.py           — CLI entry point + main polling loop (_tick)
  config.py          — Frozen dataclass config from env vars
  logger.py          — Basic logging setup (console + file)
  polymarket/
    auth.py          — CLOB client builder (paper=unauthenticated, live=authenticated)
    client.py        — Unified Polymarket API wrapper (Gamma + CLOB)
    market_data.py   — Market fetching, filtering, enrichment
    execution.py     — Order routing (paper records to SQLite, live forwards to CLOB)
  portfolio/
    tracker.py       — In-memory position tracking + PnL calculation
  risk/
    manager.py       — Pre-trade risk checks (position count, size, exposure, SL/TP)
  strategy/
    base.py          — ABC + Signal/Action types
    simple_momentum.py — Momentum over N-tick window
    mean_reversion.py  — Z-score based mean reversion
  storage/
    sqlite_store.py  — SQLite persistence (trades, price_history, markets_cache)
  utils/
    math_utils.py    — mean, stdev, z_score, pct_change
    time_utils.py    — UTC/ISO helpers
```

### Dashboard
- `generate_dashboard.js` — Node.js script reads SQLite, generates static HTML
- `docs/dashboard.html` — Generated output
- `dashboard/` — Older Next.js dashboard (appears unused / legacy)

### Data Flow
```
Gamma API → get_all_active_markets() → filter by volume/liquidity
  → enrich top N with CLOB midpoint/spread
    → strategy.evaluate(snapshot, price_history)
      → risk_mgr.check(signal, proposed_size, price)
        → executor.execute(order) → SQLite trade record
          → portfolio.open_position() (in-memory)
```

### Execution Flow
```
_tick() runs every poll_interval seconds:
  1. Check SL/TP on existing positions → close if triggered
  2. Fetch & filter markets from Gamma
  3. Evaluate strategy on each snapshot
  4. Risk check proposed trades
  5. Execute (paper: record to DB; live: forward to CLOB)
  6. Update in-memory portfolio
  7. Log portfolio summary
```

---

## 2. Top 10 Risks

| # | Risk | Severity | Impact |
|---|------|----------|--------|
| 1 | **Portfolio is in-memory only** — positions lost on restart, no reconstruction from DB on startup | CRITICAL | All open positions invisible after restart; SL/TP won't fire |
| 2 | **Fetches ALL active markets every tick** — 51,578 markets (500+ API calls per tick) | HIGH | Extremely slow ticks (~5 min), rate limiting risk, wasted bandwidth |
| 3 | **No spread filter applied before entry** — `max_spread` in config but never checked | HIGH | Bot can enter illiquid markets with wide spreads, creating false PnL |
| 4 | **Paper fills assume 100% fill at midpoint** — no slippage, no partial fills, no spread cost | HIGH | Paper PnL is systematically overstated vs reality |
| 5 | **No exit reason stored in trades table** — can't distinguish SL, TP, strategy, or manual exits | MEDIUM | Impossible to audit exit behavior or optimize SL/TP thresholds |
| 6 | **No skip/rejection journal** — risk denials and strategy HOLDs are not persisted | MEDIUM | Can't analyze why the bot isn't trading or what it's filtering out |
| 7 | **SL/TP checks use `get_price()` which calls live API** — if API returns stale/None, position stays open indefinitely | MEDIUM | Zombie positions that never close |
| 8 | **Dashboard hardcodes config values** — shows "$500 exposure", "10% SL" regardless of actual config | MEDIUM | Dashboard lies about current settings |
| 9 | **Dashboard PnL calculation is separate from backend** — re-derives PnL from trades with simple BUY/SELL matching | MEDIUM | Dashboard PnL can diverge from actual portfolio PnL |
| 10 | **No correlated exposure tracking** — bot can load up on multiple YES tokens from the same event | MEDIUM | Hidden concentration risk |

---

## 3. Top 10 Most Dangerous Assumptions

1. **Every paper fill happens at the midpoint price** — real fills happen at the ask (for buys) or bid (for sells), not the mid
2. **Every order fills completely** — real markets have depth; large orders may partial fill
3. **Price doesn't move between decision and execution** — in reality there's latency
4. **Portfolio state survives between ticks** — but not between process restarts
5. **All 51K+ markets need to be fetched every tick** — massive over-fetching
6. **Volume and liquidity from Gamma are current** — they may be stale/cumulative
7. **The spread at enrichment time equals the spread at execution time** — not guaranteed
8. **A single strategy runs globally** — no per-market or per-category strategy selection
9. **Position size = `max_position_size / price`** — this means dollar-cost is constant but share count varies inversely with price, which may not be intended
10. **Dashboard balance starts at $1000** — hardcoded, not derived from actual paper balance

---

## 4. Learning Blockers

- No journal of skipped trades → can't learn from what was filtered out
- No spread/depth snapshot stored at decision time → can't retroactively assess execution realism
- No exit reason codes → can't tune SL/TP thresholds
- No per-market or per-category PnL breakdown in backend → dashboard does its own (inconsistent) calculation
- No confidence calibration data → can't assess if strategy confidence correlates with outcomes
- No market metadata stored with trades (question text, category, time-to-resolution)

---

## 5. Live-Trading Blockers

- Portfolio reconstruction from DB not implemented
- No fill simulation realism (slippage, spread cost, partial fills)
- No order lifecycle tracking (submitted → accepted → filled → settled)
- No balance tracking (real or simulated)
- No API rate limiting
- No duplicate order prevention
- No idempotency on trade recording
- No alerting/monitoring for anomalies
- Live execution scaffold is incomplete (order_args format is approximate)
- No kill switch beyond Ctrl+C

---

## 6. Test Coverage Assessment

| Module | Tests | Coverage | Gap |
|--------|-------|----------|-----|
| config.py | test_config.py | Good | Missing: invalid env var handling |
| risk/manager.py | test_risk_manager.py | Good | Missing: edge cases (zero price, negative values) |
| strategy/*.py | test_strategies.py | Adequate | Missing: boundary conditions, confidence values |
| utils/math_utils.py | test_math_utils.py | Good | Adequate |
| portfolio/tracker.py | None | **NONE** | Critical gap |
| storage/sqlite_store.py | None | **NONE** | Critical gap |
| execution.py | test_lifecycle.py (integration) | Partial | Not a proper unit test |
| market_data.py | None | **NONE** | Critical gap |
| main.py (_tick) | None | **NONE** | Integration gap |

---

## 7. What Works Well

- Clean modular architecture with clear separation of concerns
- Proper paper/live mode separation with double-safety checks
- Frozen config dataclass prevents accidental mutation
- Graceful shutdown handling (SIGINT/SIGTERM)
- Retry logic on API calls (tenacity)
- Basic risk controls (position count, size, exposure caps, SL/TP)
- Two strategy implementations with shared interface
- SQLite persistence for trades and price history
- Dashboard generates useful overview from trade data
