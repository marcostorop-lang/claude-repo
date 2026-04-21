# Polymarket Trading Bot — Install & Run

Complete walkthrough: zero → running paper bot → verified output → (optionally) systemd / Docker deployment.

Default mode is **paper** (simulated fills). Real orders require three explicit env gates — see [Going Live](#going-live).

---

## 1. Requirements

- Python **3.10+** (3.11 recommended; tested here on 3.11.14)
- `pip`, `git`, `curl`
- An **Anthropic API key** — https://console.anthropic.com/
- (Optional, only for live trading) Polymarket CLOB API credentials:
  - Wallet private key (Polygon chain)
  - CLOB API key / secret / passphrase — generate from https://docs.polymarket.com

---

## 2. Install

```bash
# Clone or pull the repo
git clone <repo-url> polymarket-bot
cd polymarket-bot

# Create & activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows PowerShell

# Install dependencies
pip install --upgrade pip
pip install -r bot/requirements.txt
```

Expected: `anthropic`, `py-clob-client`, `httpx`, `click`, `python-dotenv` all resolve.

---

## 3. Configure

```bash
cp bot/.env.example bot/.env
${EDITOR:-nano} bot/.env
```

**Minimum for paper mode:**

```env
TRADING_MODE=paper
ALLOW_LIVE_TRADING=false
ANTHROPIC_API_KEY=sk-ant-...        # required

STRATEGY_PROBABILITY_ARB=true
STRATEGY_LOGICAL_ARB=true
STRATEGY_MARKET_MAKING=false        # leave disabled initially
```

Everything else has sensible defaults. Override via env vars or `.env` as needed (see `bot/.env.example`).

---

## 4. Pre-flight check

Before the first run, validate the install:

```bash
python -m bot.main preflight
```

This runs nine checks and exits non-zero on failure:

```
=== Pre-flight checks ===
  [PASS] Trading mode  — paper
  [PASS] At least one strategy enabled  — probability_arb, logical_arb
  [PASS] ANTHROPIC_API_KEY set
  [PASS] Claude API reachable  — model=claude-sonnet-4-20250514
  [PASS] Gamma API reachable  — HTTP 200
  [skip] CLOB client  — paper mode, not required
  [PASS] Log directory writable  — .../bot/logs
  [PASS] Calibration DB  — resolved=0 pending=0
  [PASS] Paper order round-trip  — order_id=paper-... mode=paper

All checks passed — safe to run.
```

If any check fails, fix it before continuing — running anyway will usually just crash after a few cycles.

---

## 5. Run the bot

```bash
python -m bot.main run
```

You should see:

```
============================================================
Polymarket Trading Bot starting | mode=PAPER
============================================================
Paper mode — no real orders.
Active strategies: probability_arb, logical_arb
Healthcheck server listening on http://127.0.0.1:8787
─── Cycle 1 ───
[prob_arb] Scanning active markets…
[prob_arb] Will Bitcoin… | P_claude=0.450 P_market=0.520 edge=-0.070 net=-0.090 conf=0.80
...
Risk | equity=$1000.00 | daily_pnl=$+0.00 | dd=0.0% | pos=0 ($0.00)
```

Stop with `Ctrl+C`. A graceful shutdown cancels any resting quotes and prints a daily summary.

### What to verify in the first hour

1. **Cycles keep advancing** — log shows `─── Cycle N ───` roughly every 30–120s depending on the active strategy.
2. **Markets are being evaluated** — `[prob_arb] <question> | P_claude=... P_market=... edge=...` lines appear.
3. **Risk summary updates** — `Risk | equity=... positions=...` logs every cycle.
4. **No stack traces** — unhandled exceptions indicate a real bug; open an issue with the traceback.

---

## 6. Inspect outputs

Everything lives in `bot/logs/`:

| File | Description |
|---|---|
| `bot.log` | Rolling stdout + all INFO/WARNING/ERROR |
| `trades.jsonl` | One JSON per attempted trade (one line = one trade) |
| `rejections.jsonl` | One JSON per risk-rejected signal — why you didn't trade |
| `calibration.db` | SQLite: every Claude estimate + outcome (once recorded) |

Quick peeks:

```bash
# Trades the bot wanted to do (paper or live)
tail -f bot/logs/trades.jsonl

# Why some signals were rejected (Kelly=0, halted, concentration...)
tail -f bot/logs/rejections.jsonl

# Current bot state as JSON
python -m bot.main status

# Calibration scores (Brier / log loss once you've recorded outcomes)
python -m bot.main calibration-report
```

### Check the healthcheck endpoint

```bash
curl -s http://127.0.0.1:8787/health | python -m json.tool
curl -s http://127.0.0.1:8787/ready
curl -s http://127.0.0.1:8787/metrics     # Prometheus format
```

---

## 7. Record market resolutions (for calibration)

The bot automatically stores every probability estimate. When a market resolves, tell the bot the outcome so it can compute Brier score / log loss:

```bash
python -m bot.main record-resolution --condition-id 0xabc123... --outcome 1
# outcome: 1 = YES resolved, 0 = NO resolved
```

Then:

```bash
python -m bot.main calibration-report
```

This is how you measure whether Claude is actually well-calibrated on your market basket — the single most important metric for strategy quality.

---

## 8. Run a quick backtest

```bash
python -m bot.main backtest --token-id <TOKEN_ID> --question "Will X happen?"
```

Note: the backtester is a **stub** using a mean-reversion proxy on recent price history. It validates the sizing / drawdown plumbing, but does NOT replay Claude decisions (that would be cost-prohibitive on every historical tick). Don't treat its numbers as expected strategy performance.

---

## 9. Run the test suite

```bash
# All bot tests (fast — no network, no API calls)
python -m pytest tests/test_bot_*.py -q
```

You should see `59 passed` or similar. Any failures block running — investigate before proceeding.

---

## 10. Deployment

### Option A — systemd (recommended for a single VPS)

```bash
# On the server:
sudo useradd --system --create-home --home-dir /opt/polymarket-bot polybot
sudo mkdir -p /opt/polymarket-bot
sudo chown polybot:polybot /opt/polymarket-bot
sudo -u polybot -H bash <<'EOF'
    cd /opt/polymarket-bot
    git clone <repo-url> .
    python3 -m venv .venv
    .venv/bin/pip install -r bot/requirements.txt
    cp bot/.env.example bot/.env
    # edit bot/.env: set ANTHROPIC_API_KEY
EOF

# Install the unit
sudo cp bot/deploy/systemd/polymarket-bot.service /etc/systemd/system/
sudo cp bot/deploy/logrotate.conf /etc/logrotate.d/polymarket-bot
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-bot

# Live logs
sudo journalctl -u polymarket-bot -f
```

### Option B — Docker Compose

```bash
cd bot/deploy
docker compose up -d
docker compose logs -f bot
curl http://127.0.0.1:8787/health
```

Logs and the calibration DB are persisted under `bot/deploy/data/logs/`.

---

## 11. Going live

**DO NOT enable live trading until you've:**

1. Run the bot in paper mode for at least a week
2. Reviewed `trades.jsonl` — are the trades sensible?
3. Recorded resolutions for enough markets to compute a Brier score <0.20
4. Verified the preflight with CLOB credentials set
5. Set a hard `LIVE_TRADE_MAX_POSITION_USD` to cap dry-run exposure

When ready, flip **all three** gates in `.env`:

```env
TRADING_MODE=live
ALLOW_LIVE_TRADING=true
I_UNDERSTAND_REAL_MONEY=YES_TRADE_REAL_FUNDS

# CLOB credentials (required)
PRIVATE_KEY=0x...
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_PASSPHRASE=...
```

Then:

```bash
python -m bot.main preflight        # must PASS including CLOB client
python -m bot.main run
```

Logs will show `⚠️  LIVE MODE — real orders will be placed.` Start with small capital, monitor the healthcheck endpoint, and watch Telegram/Discord notifications if configured.

---

## 12. Troubleshooting

**`ModuleNotFoundError: anthropic`** — reinstall deps inside the venv: `pip install -r bot/requirements.txt`.

**`Gamma API reachable FAIL`** — check outbound network / firewall. The Gamma API is `gamma-api.polymarket.com`, port 443.

**Claude rate-limited** — the bot backs off automatically. Reduce `PROB_ARB_MAX_MARKETS_PER_SCAN` or raise `PROB_ARB_SCAN_INTERVAL`.

**Healthcheck port already in use** — change `HEALTHCHECK_PORT` in `.env` or disable with `HEALTHCHECK_ENABLED=false`.

**Bot stopped advancing cycles** — check `/health` endpoint for `seconds_since_last_cycle`. If >600s, restart. Long-running strategies can hang on network calls.

**High Brier score (>0.25)** — Claude is poorly calibrated on your market mix. Raise `PROB_ARB_MIN_CONFIDENCE` or `PROB_ARB_MIN_EDGE_PCT`, or switch strategies (logical arb is structural and doesn't depend on calibration).

---

## 13. File map

```
bot/
├── main.py                   # CLI + scheduler
├── config.py                 # All settings from env
├── INSTALL.md                # ← this document
├── README.md                 # Architecture overview
├── .env.example              # Configuration template
├── core/
│   ├── claude_oracle.py      # Claude probability estimation
│   ├── polymarket_client.py  # CLOB + Gamma API
│   ├── risk_manager.py       # Kelly + drawdown + rejection log
│   ├── calibration.py        # SQLite Brier/log-loss tracking
│   ├── healthcheck.py        # HTTP health/metrics server
│   └── utils.py              # Data types + helpers
├── strategies/
│   ├── probability_arbitrage.py
│   ├── logical_arbitrage.py
│   └── market_making.py
├── backtest/
│   └── engine.py             # Mean-reversion proxy backtester
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── logrotate.conf
│   └── systemd/polymarket-bot.service
└── logs/                     # bot.log, trades.jsonl, calibration.db
```
