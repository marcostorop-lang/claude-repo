# Risk Guardrails

**Last updated**: 2026-04-08

---

## 1. Paper-Mode Rules

- `TRADING_MODE` defaults to `paper`. This is enforced in `Config`.
- `ALLOW_LIVE_TRADING` defaults to `false`. Must be explicitly set to `true` for live orders.
- `ExecutionEngine.execute()` routes to `_paper_execute()` when `is_paper=True` OR `is_live=False`.
- `PolymarketClient.place_order()` has an independent guard: returns `None` if `cfg.is_live` is `False`.
- `_live_execute()` has a double-safety check: returns failure if `cfg.is_live` is `False`.
- **Three independent gates** must all pass for a real order to be sent.

### Rules
- NEVER set `ALLOW_LIVE_TRADING=true` without explicit written authorization.
- NEVER modify the safety checks in `execution.py` or `client.py`.
- NEVER remove the `is_paper` / `is_live` property logic in `config.py`.
- All new execution paths must respect paper/live separation.

---

## 2. Exposure Caps

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_POSITION_SIZE` | $50 | Maximum dollar value per position |
| `MAX_TOTAL_EXPOSURE` | $200 | Maximum total portfolio exposure |
| `MAX_OPEN_POSITIONS` | 5 | Maximum concurrent open positions |
| `STOP_LOSS_PCT` | 10% | Close position if loss exceeds this |
| `TAKE_PROFIT_PCT` | 20% | Close position if gain exceeds this |

### Rules
- Position size is capped at `MAX_POSITION_SIZE` in the risk manager.
- If adding a position would exceed `MAX_TOTAL_EXPOSURE`, the size is reduced.
- If `MAX_OPEN_POSITIONS` is reached, new BUY signals are rejected.
- SL/TP are checked at the start of every tick.

### Known Gaps (to be addressed)
- No per-market or per-condition_id concentration limit.
- No per-category or per-event-cluster exposure limit.
- No maximum loss per day/week.
- No drawdown-based circuit breaker.
- No volatility-adjusted position sizing.

---

## 3. Spread / Depth / Liquidity Filters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_VOLUME` | $1,000 | Minimum market volume to consider |
| `MIN_LIQUIDITY` | $500 | Minimum market liquidity to consider |
| `MAX_SPREAD` | 0.15 | Maximum spread (config exists but NOT enforced in code) |

### Known Gaps
- **CRITICAL**: `MAX_SPREAD` is defined in config but `_passes_filters()` never checks it.
- No depth check — bot may trade in thin order books.
- No check for stale quotes.
- Volume/liquidity values from Gamma may be cumulative, not current.

---

## 4. No-Trade Conditions

The bot should NOT trade when:
- [ ] Market spread exceeds `MAX_SPREAD` (**not implemented**)
- [ ] Price data is stale (older than N minutes) (**not implemented**)
- [ ] Market resolves within N hours (**not implemented**)
- [ ] Market price is near 0 or 1 (extreme probability, low edge) (**not implemented**)
- [ ] Market has insufficient order book depth (**not implemented**)
- [ ] Maximum daily loss reached (**not implemented**)
- [ ] Bot has been running for fewer than N ticks (warmup) (**not implemented**)

---

## 5. Kill-Switch Conditions

Current: Ctrl+C (SIGINT) or SIGTERM triggers graceful shutdown.

Recommended additions (not yet implemented):
- [ ] Maximum daily loss triggers automatic shutdown
- [ ] Maximum drawdown from peak triggers automatic shutdown
- [ ] API error rate exceeds threshold triggers automatic shutdown
- [ ] Manual kill file (`touch KILL_SWITCH`) triggers automatic shutdown
- [ ] Heartbeat monitoring — alert if bot stops producing log entries

---

## 6. Future Live-Trading Gates

Before enabling live trading, ALL of these must be satisfied:

### Mandatory Pre-Live Checklist
- [ ] Minimum 4 weeks of paper trading data collected
- [ ] Paper PnL is positive after accounting for realistic spread costs
- [ ] Win rate is above 50% on closed trades with N > 50
- [ ] Maximum drawdown is within acceptable range
- [ ] Portfolio reconstruction from DB is working and tested
- [ ] Slippage simulation shows edge survives realistic execution
- [ ] All P0 and P1 backlog items are resolved
- [ ] Unit test coverage for portfolio, storage, execution, risk
- [ ] Kill switch and circuit breaker are implemented and tested
- [ ] API rate limiting is implemented
- [ ] Duplicate order prevention is implemented
- [ ] Order lifecycle tracking is implemented
- [ ] Balance tracking is implemented
- [ ] Live execution path has been tested on testnet (if available)
- [ ] Manual code review of all execution paths
- [ ] Alerting/monitoring is configured
- [ ] Rollback procedure is documented and tested
