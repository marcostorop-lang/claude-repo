# Operator Runbook

> Read this before switching the bot on. **This is the only place the
> authoritative answers to emergency questions live.**

## TL;DR — default-safe defaults

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `TRADING_MODE` | `paper` | No real orders even if credentials are set |
| `ALLOW_LIVE_TRADING` | `false` | First gate to live |
| `I_UNDERSTAND_REAL_MONEY` | *(empty)* | Second gate — must equal `YES_TRADE_REAL_FUNDS` exactly |
| `SHADOW_MODE` | `false` | Shadow runs record decisions but never fill |
| `SEMANTIC_ENGINE_ENABLED` | `false` | Engine does nothing |
| `STRATEGY` | `simple_momentum` | Conservative default strategy |
| `NET_PAIRED_LEGS` | `false` | Count distinct tokens as slots (old behaviour) |
| `MAX_BOOK_DEPTH_FRACTION` | `0` | Liquidity cap only (old behaviour) |
| `LOG_MAX_BYTES` | `10485760` (10 MB) | Rotate `bot.log` past this size |
| `LOG_BACKUP_COUNT` | `5` | Rotated `bot.log.1..N` retained |
| `DB_BACKUP_DIR` | *(empty)* | Set to enable periodic online SQLite backups |
| `DB_BACKUP_INTERVAL_HOURS` | `24` | How often to snapshot |
| `DB_BACKUP_KEEP` | `7` | How many snapshots to retain |
| `RESOLUTION_SWEEPER_ENABLED` | `false` | Auto-close positions when their market resolves |
| `RESOLUTION_SWEEP_INTERVAL_MINUTES` | `60` | How often the sweeper polls Gamma |
| `POSITION_STALENESS_DAYS` | `0` | 0 = off; >0 flags positions open that long without movement |
| `POSITION_STALENESS_PRICE_EPSILON` | `0.01` | Price range under which a window is "no movement" |
| `POSITION_STALENESS_ACTION` | `alert` | `alert` \| `close` — escalation policy |
| `POSITION_STALENESS_INTERVAL_MINUTES` | `120` | How often staleness scans run |
| `TEMPORAL_FILTER_ENABLED` | `false` | Gate BUYs to UTC hours with proven edge |
| `TEMPORAL_MIN_WINRATE` | `0.65` | Minimum hourly win-rate to allow BUYs |
| `TEMPORAL_MIN_SAMPLES` | `20` | Hourly trade count below which filter stays fail-safe |
| `TEMPORAL_WINDOW_DAYS` | `30` | Lookback window for hourly win-rate |
| `SIZING_KELLY_PROPER` | `false` | Use exact Kelly formula (p,b,q) for sizing |
| `KELLY_FRACTION` | `0.25` | Fractional-Kelly multiplier (1.0 = full, 0.25 = quarter) |
| `VOLATILITY_FILTER_ENABLED` | `false` | Reject BUYs on choppy tokens |
| `MAX_PRICE_VOLATILITY` | `0.05` | Max stddev (in $-of-price) over the window |
| `VOLATILITY_WINDOW` | `10` | Samples used to compute recent-price stddev |
| `BAYESIAN_CALIBRATION_ENABLED` | `false` | Track Beta(α,β) posterior per strategy (silent) |
| `BAYESIAN_SIZING_ENABLED` | `false` | Apply posterior-mean multiplier to sizing (shrinks only) |
| `BAYESIAN_MIN_SAMPLES` | `30` | Trades required before multiplier activates |
| `BAYESIAN_MIN_MULTIPLIER` | `0.3` | Floor on the sizing multiplier |
| `TAIL_RISK_ENABLED` | `true` | Per-tick VaR/CVaR/worst-case computation |
| `VAR_95_ALERT_USD` | `0` | Page when 95% VaR exceeds this (0 = silent) |
| `CVAR_95_ALERT_USD` | `0` | Page when CVaR exceeds this (0 = silent) |
| `SIZING_CAPITAL_EFFICIENCY_ENABLED` | `false` | Shrink size on long-dated markets (capital-cost penalty) |
| `SIZING_CAPITAL_EFFICIENCY_TARGET_DAYS` | `14.0` | Markets ≤ this many days are unscaled |
| `SIZING_CAPITAL_EFFICIENCY_MIN_FACTOR` | `0.25` | Floor on the shrinkage factor for very long-dated markets |
| `WALLET_BALANCE_CHECK_ENABLED` | `true` | Runtime gate: refuse BUYs whose cost exceeds live USDC (live mode only) |
| `WALLET_BALANCE_REFRESH_SECONDS` | `60` | Min seconds between upstream balance fetches (cache TTL) |
| `WALLET_BALANCE_MIN_BUFFER_USD` | `0` | Keep this much USDC unspendable (fee cushion / safety margin) |
| `POSITION_RECONCILIATION_ENABLED` | `false` | Live-mode only: periodically compare tracked positions vs on-chain shares |
| `POSITION_RECONCILIATION_INTERVAL_MINUTES` | `60` | How often the sweep runs |
| `POSITION_RECONCILIATION_TOLERANCE_SHARES` | `0.01` | Divergences ≤ this are ignored (rounding / 1e-6 noise) |
| `LIVE_TRADE_AUTOPAUSE_THRESHOLD` | `0` | >0 = block new live BUYs after N fills until operator acks |
| `LIVE_TRADE_AUTOPAUSE_ACK_FILE` | `live_trades_acknowledged.ack` | Touch this file to resume after the threshold is reached |
| `ALERT_WEBHOOK_MIN_SEVERITY` | `warning` | `info` \| `warning` \| `critical` — min level forwarded to webhook |

When in doubt, change nothing. The defaults have been validated against
the full test suite and the one rule from `CLAUDE.md` is never to
rebuild from scratch.

---

## Emergency: stop the bot NOW

### Option A — graceful (preferred)

Create the kill-switch file in the working directory. The tick loop
checks for it at the top of every iteration and shuts down cleanly,
completing any in-flight execution first:

```bash
touch KILL_SWITCH
# or whatever KILL_SWITCH_FILE is configured to
```

The bot logs `KILL SWITCH FILE detected ... shutting down.` and exits.
Remove the file before restarting.

### Option B — SIGTERM (still graceful)

```bash
# Find the process
ps aux | grep "python -m src.main"
kill <PID>
```

Python's `KeyboardInterrupt` / `SystemExit` handler in `_tick` is
defensive — outstanding trades are logged, portfolio state is flushed to
SQLite, and the DB is closed.

### Option C — SIGKILL (last resort)

```bash
kill -9 <PID>
```

Use only if A and B hang. You'll lose the **current-tick** in-memory
state (any trades that hadn't yet been persisted to SQLite). Restart
with `_reconstruct_portfolio` — the bot rebuilds open positions from the
`trades` table on startup, so an orderly restart recovers to the last
persisted state.

---

## Recovering from a crash

1. Check `bot.log` for the last `tick_stats` line — that's the last
   successful tick.
2. Check `trades` table for any BUY without a matching SELL — those are
   open positions.
3. Check `decision_log` for `RISK_REJECTED` spikes right before the
   crash — often the root cause is a config mismatch or API hiccup.
4. Restart the bot with the same `SQLITE_DB_PATH`. It will:
   - Reconstruct portfolio state from `trades`.
   - Resume risk checks (daily-loss counter **does** reset on date change,
     not on restart — this is intentional, see `RiskManager._maybe_reset_daily`).

---

## Activating live trading

**Never do this in a single session. Staged ramp required.**

### Pre-launch — `preflight`

**Run this every time before changing live flags:**

```bash
python -m src.main preflight
```

Validates the two-gate live config, DB writability, kill-switch
absence, alert wiring, risk-limit coherence, Polymarket API
reachability, and (in live mode) wallet balance ≥ MAX_TOTAL_EXPOSURE.
Exits non-zero if any check FAILs — wire it into your deploy script.

Use `--skip-network` in CI environments without outbound HTTPS.

### Verifying alert transport — `test-alerts`

Before live trading, verify your alert pipeline end-to-end:

```bash
python -m src.main test-alerts
```

Fires one info + one warning + one critical through every configured
sink (file + webhook). A second run inside the dedupe window is a
silent no-op — that's real behaviour, not a bug. The file sink
(``ALERT_LOG_FILE``) captures everything; the webhook
(``ALERT_WEBHOOK_URL``) only forwards alerts at or above
``ALERT_WEBHOOK_MIN_SEVERITY`` (default ``warning``) so operational
noise doesn't drown out paging events.

### Runtime wallet gate

Preflight is the authoritative launch gate, but in live mode the risk
manager also consults a cached USDC-balance probe at trade time. If
wallet balance was drained between preflight and tick-time (another
process withdrew, cross-market fees accumulated), BUYs are refused with
`Insufficient wallet: need $X, available $Y`. Cache TTL is
`WALLET_BALANCE_REFRESH_SECONDS` (default 60s), buffer cushion is
`WALLET_BALANCE_MIN_BUFFER_USD`. Fail-safe: if the SDK isn't installed
or the RPC blips, the runtime gate allows through and logs the cause —
it is a belt-and-braces check, not a replacement for preflight.

### Position reconciliation (live mode)

Enable ``POSITION_RECONCILIATION_ENABLED=true`` once you cross into
live trading. Every
``POSITION_RECONCILIATION_INTERVAL_MINUTES`` (default 60) the
sweeper asks the CLOB for the actual ERC1155 share balance of each
tracked token and reports any divergence larger than
``POSITION_RECONCILIATION_TOLERANCE_SHARES`` (default 0.01). Three
kinds fire:

* **phantom_local** — tracker thinks we hold shares, chain shows
  zero. Alerts at `critical`: most dangerous because it can drive
  false exits. Investigate before the next BUY of that market.
* **untracked_onchain** — chain shows shares we never booked.
  Alerts at `warn`. Usually a manual trade or a restart water-mark
  bug.
* **size_mismatch** — both sides nonzero but differ by more than
  tolerance. Alerts at `warn`.

The sweeper **never mutates portfolio state** — its job is to make
drift visible, not to paper over it.

### First-N live-trades autopause

Set ``LIVE_TRADE_AUTOPAUSE_THRESHOLD=N`` (e.g. 3) the first time you
cross to live so the bot refuses new BUYs after the N-th fill. Manually
verify each of the first N orders on Polymarket's UI — price,
size, wallet debit, alert — then touch
``LIVE_TRADE_AUTOPAUSE_ACK_FILE`` (default:
``live_trades_acknowledged.ack``) to resume. SELLs are never gated:
a paused bot can always close whatever it has open. The counter is
seeded from the ``trades`` table on startup so a restart doesn't
quietly reset the brake.

### Stage 0 — paper only, default strategy

Already where you start. Confirm:

```bash
tail -f bot.log | grep tick_stats
```

Look for non-zero `signals_generated`, non-degenerate `realised_pnl`
over several days. If PnL is implausible, something is mis-configured
before touching anything else.

### Stage 1 — paper + semantic engine in shadow

```bash
SEMANTIC_ENGINE_ENABLED=true
SEMANTIC_ENGINE_MODE=shadow
STRATEGY=simple_momentum
```

Run for at least 3 days. Inspect:

```bash
# Dashboard endpoints
curl http://localhost:8000/api/semantic/summary?days=3
curl http://localhost:8000/api/semantic/calibration?days=3
```

Review the calibration report. The **single most important check**:
`structural_complement` should dominate `per_method`. If
`weighted_avg_equivalent` or `temporal_range` count more hits than
structural, something is off — textual matching is too loose for your
market universe.

### Stage 2 — paper + semantic strategy

```bash
STRATEGY=semantic_mispricing
SEMANTIC_ENGINE_MODE=live   # still paper globally
NET_PAIRED_LEGS=true        # only if the engine frequently hits both legs
MAX_BOOK_DEPTH_FRACTION=0.25
```

Watch paper PnL for a week. Compare realised vs. detected `net_edge` via
`/api/semantic/calibration`. If `realisation_ratio` < 0.5 across methods,
raise `SEMANTIC_MIN_NET_EDGE` until it stabilises above 0.7.

### Stage 3 — live with TINY capital

```bash
TRADING_MODE=live
ALLOW_LIVE_TRADING=true
I_UNDERSTAND_REAL_MONEY=YES_TRADE_REAL_FUNDS   # both gates required
MAX_POSITION_SIZE=5        # five dollars
MAX_TOTAL_EXPOSURE=20      # twenty dollars total
MAX_DAILY_LOSS=5           # five dollars kills trading for the day
```

Both gates must be set. If `I_UNDERSTAND_REAL_MONEY` is wrong or missing,
`Config.is_live` returns `False` and `validate()` emits the warning
`LIVE TRADING BLOCKED`. The bot keeps running in paper.

Watch for:

- first 10 live trades, hand-verify each fill against the Polymarket UI
- daily loss counter in `bot.log` — circuit-breaker fires at
  `MAX_DAILY_LOSS` and halts BUYs automatically
- `RISK_REJECTED` rate vs. `trades_executed` — unusually high rejection
  rates suggest config drift

### Rollback to paper

```bash
ALLOW_LIVE_TRADING=false
# leave I_UNDERSTAND_REAL_MONEY in place — harmless without the first gate
touch KILL_SWITCH  # stop current live process
# Remove KILL_SWITCH and restart with updated env
```

---

## Routine observability checks

### Daily

```bash
curl http://localhost:8000/api/overview | jq
curl http://localhost:8000/api/performance | jq
```

- `daily_pnl` should not trend monotonically negative over 3+ days
- `current_exposure` should be < `MAX_TOTAL_EXPOSURE`
- `open_positions` should not be stuck — positions older than 7 days
  without a SELL are a red flag

### Weekly

```bash
curl http://localhost:8000/api/strategies | jq
curl http://localhost:8000/api/semantic/calibration?days=7 | jq
curl http://localhost:8000/api/risk | jq
curl http://localhost:8000/api/drift | jq
```

- Strategy ranking: active strategy should be near top by `total_pnl`
  *or* have a defensible story for why not
- Semantic calibration `realisation_ratio` by method: structural should
  be > 0.7; textual/temporal ratios below 0.5 mean raise thresholds
- `/api/risk`: `latest.var_95` / `cvar_95` should track open exposure
  monotonically; `rejections.by_bucket` shows *why* trades are being
  blocked (`circuit_breaker`, `temporal_filter`, `volatility`,
  `slippage`, etc.); `sizing_trace` confirms the opt-in shrinkage
  multipliers (capital efficiency, Bayesian, Kelly) are firing as
  configured rather than silently no-op'ing
- `/api/drift`: `realisation_ratio` is realised PnL ÷ predicted PnL
  (in dollars) over the last `window` (default 100) closed trades.
  Healthy strategy converges to ≈1.0; a ratio that drifts down over
  weeks signals model decay — **investigate before adding capital**.
  Cold-start (`< min_samples` edge-tagged trades) is fail-safe so
  early-life noise doesn't trigger false alerts

### Alerts (not yet implemented — known gap)

The bot currently emits no push alerts. Until that's added, a cron job
polling `/api/overview` and paging on `bot_active=false` or a
pre-configured daily-loss watermark is the recommended compensating
control.

### Position hygiene (auto-resolution + staleness)

Two opt-in subsystems in `src/portfolio/` help keep the books honest
on a long-running paper (or live) bot:

- **Resolution sweeper** — `RESOLUTION_SWEEPER_ENABLED=true` polls
  Gamma every `RESOLUTION_SWEEP_INTERVAL_MINUTES` (default 60 min) and
  books a synthetic settlement fill at the outcome price ($1 or $0)
  for any position whose market has resolved.  Writes a trade row
  (`mode=paper_resolution`, `exit_reason=market_resolved`), a
  resolution row (ground-truth audit), and emits an alert + metric.
  Never places a live order — even in live mode, settlement is
  accounting-only.
- **Staleness monitor** — `POSITION_STALENESS_DAYS>0` flags positions
  older than that window whose price range has stayed inside
  `POSITION_STALENESS_PRICE_EPSILON` (default 0.01).
  `POSITION_STALENESS_ACTION=alert` (default) just notifies;
  `=close` additionally exits the position through the normal
  executor (respects paper/live + maker-preferred).  Scans run every
  `POSITION_STALENESS_INTERVAL_MINUTES` (default 120 min).

---

## Known risks (as of 2026-04)

| Risk | Mitigation | Residual |
| ---- | ---------- | -------- |
| Live trading accidentally enabled | Two-gate config + paper default | Operator must still type the exact phrase |
| Semantic textual false positives | `min_relation_confidence=0.65`, `same_category_required=True`, paper-first | Unusual questions will slip through until a human reviews |
| Stale snapshot price | `MAX_PRICE_BOOK_DIVERGENCE=0.03` rejects mismatched ticks | Book itself can be stale if API degrades |
| Over-exposure on neg-risk legs | `NET_PAIRED_LEGS=true` nets opposing sides | Off by default — must enable for multi-leg strategies |
| API rate-limit hit | Poll interval default 60s; `MAX_MARKETS_FETCH=500` | No exponential backoff yet |
| Dashboard vs. backend drift | `/api/health`, typed JSON contracts | Manual inspection still required after schema migrations |
| Phantom exposure in resolved markets | `RESOLUTION_SWEEPER_ENABLED=true` auto-closes at settlement | Off by default — enable on long-running bots |
| Capital trapped in dormant positions | `POSITION_STALENESS_DAYS>0` alerts/closes flat-price holds | Off by default — pick `alert` first to tune thresholds |
| Daily circuit breaker reset by crash | Startup now seeds `RiskManager.daily_pnl` from trades booked since UTC midnight | Relies on accurate `timestamp` column; bad system clock weakens the check |
| Trading at structurally losing hours | `TEMPORAL_FILTER_ENABLED=true` gates BUYs to UTC hours where the realised win-rate clears `TEMPORAL_MIN_WINRATE` | Off by default; cold-start fail-safe so it allows all 24 hours until enough samples — turn on only once calibration has weeks of closed trades |

---

## Running in Docker

A minimal `Dockerfile` + `docker-compose.yml` live at the repo root.
They package the bot process only — the dashboard stays on its own
lifecycle.  Paper mode by default; the two live-trading gates must be
set explicitly at `docker run` or via a `.env` file alongside
`docker-compose.yml`.

```bash
docker compose up -d bot         # build + start
docker compose logs -f bot       # tail logs (mirrors bot.log inside /data)
docker compose exec bot python -m src.main summary
docker compose down              # stop (volume persists)
```

State (SQLite DB, rotated logs, alerts JSONL, metrics JSONL, backups)
lives in the `polybot-data` named volume so container rebuilds never
wipe history.  The image runs as non-root with a read-only root FS;
all writes go through `/data`.

## What to NEVER do

1. **Never** set `ALLOW_LIVE_TRADING=true` on an untested config.
2. **Never** edit `trades`, `portfolio`, or `semantic_signals` by hand
   while the bot is running — use SQLite's `BEGIN EXCLUSIVE` or stop the bot first.
3. **Never** remove `KILL_SWITCH_FILE` logic, the two-gate live check,
   or `MAX_DAILY_LOSS`. Anyone asking you to "simplify" these limits is
   wrong.
4. **Never** commit `.env` files or private keys — `.gitignore` should
   already prevent this, but audit your PRs.
5. **Never** run `git push --force` on shared branches without explicit
   permission.
