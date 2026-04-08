# Continuous Improvement Backlog

**Last updated**: 2026-04-08

## Priority Legend
- **P0**: Safety / data integrity — must fix immediately
- **P1**: Learning quality — blocks evidence collection
- **P2**: Realism / hardening — improves paper-to-live readiness
- **P3**: Observability / dashboard — improves decision quality
- **P4**: Strategy / optimization — only after P0-P3 are solid

---

| # | Priority | Issue | Severity | Expected Value | Complexity | Risk | Validation | Status |
|---|----------|-------|----------|---------------|------------|------|------------|--------|
| 1 | P0 | Portfolio not reconstructed from DB on restart | CRITICAL | Prevents lost positions | Low | Low | Restart bot, verify positions persist | **TODO** |
| 2 | P0 | Bot fetches ALL 51K+ markets every tick (500+ API calls) | HIGH | 100x faster ticks, no rate limiting | Medium | Low | Measure tick duration before/after | **TODO** |
| 3 | P1 | No spread filter enforced at entry | HIGH | Prevents trades in illiquid markets | Low | Low | Check trades exclude high-spread markets | **TODO** |
| 4 | P1 | No exit reason stored in trades | HIGH | Enables SL/TP analysis | Low | Low | Query trades for exit_reason column | **TODO** |
| 5 | P1 | No skip/rejection journal | HIGH | Enables filter analysis | Medium | Low | Check decision_log table exists | **TODO** |
| 6 | P1 | No spread/depth snapshot at decision time | MEDIUM | Enables execution realism analysis | Medium | Low | Check trades have spread_at_entry column | **TODO** |
| 7 | P2 | Paper fills assume midpoint with no slippage | HIGH | Realistic PnL estimation | Medium | Medium | Compare paper PnL with slippage-adjusted | **TODO** |
| 8 | P2 | No correlated exposure tracking | MEDIUM | Prevents hidden concentration | Medium | Low | Verify condition_id grouping in risk check | **TODO** |
| 9 | P3 | Dashboard hardcodes config values | MEDIUM | Dashboard reflects truth | Low | Low | Visual inspection | **TODO** |
| 10 | P3 | Dashboard PnL diverges from backend | MEDIUM | Single source of truth | Medium | Low | Compare dashboard vs backend PnL | **TODO** |
| 11 | P2 | No balance tracking (paper or real) | MEDIUM | Realistic paper trading | Medium | Low | Check balance column in DB | **TODO** |
| 12 | P2 | No duplicate order prevention | MEDIUM | Prevents double entries | Low | Low | Test rapid duplicate signals | **TODO** |
| 13 | P2 | No stale price handling | MEDIUM | Prevents stale-data trades | Low | Low | Inject stale price, verify rejection | **TODO** |
| 14 | P3 | No per-category PnL breakdown | LOW | Better market selection | Medium | Low | Dashboard shows category breakdown | **TODO** |
| 15 | P1 | Logging lacks structured decision context | MEDIUM | Better audit trail | Medium | Low | Review log output for decision fields | **TODO** |
| 16 | P2 | No API rate limiting | MEDIUM | Prevents bans/throttling | Medium | Low | Monitor API call count per tick | **TODO** |
| 17 | P2 | Missing unit tests for portfolio, storage, market_data | MEDIUM | Prevents regressions | Medium | Low | Run pytest, check coverage | **TODO** |
| 18 | P4 | No per-market strategy selection | LOW | Better signal quality | High | Medium | Backtest per-market vs global | **FUTURE** |
| 19 | P4 | No time-to-resolution awareness | LOW | Better timing | Medium | Medium | Add resolution date to filtering | **FUTURE** |
| 20 | P4 | No confidence calibration | LOW | Evidence quality | High | Low | Track confidence vs outcome | **FUTURE** |
