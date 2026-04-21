# Polymarket Trading Bot

Three-strategy async trading bot for Polymarket prediction markets.

## Strategies

| # | Strategy | Description | Edge Source |
|---|----------|-------------|-------------|
| 1 | **Probability Arbitrage** | Claude estimates independent probability; trade when market deviates | AI calibration vs market |
| 2 | **Logical Arbitrage** | Detect constraint violations between related markets (implies/excludes/sums-to-one) | Structural / graph-based |
| 3 | **Market Making** | Two-sided liquidity with dynamic spreads, inventory management | Spread capture |

## Quick Start

See **[INSTALL.md](INSTALL.md)** for the full walkthrough (env setup, preflight, verification).

```bash
# 1. Install dependencies
pip install -r bot/requirements.txt

# 2. Configure
cp bot/.env.example bot/.env
# Edit bot/.env — at minimum set ANTHROPIC_API_KEY

# 3. Validate the install
python -m bot.main preflight

# 4. Run (paper mode — default, safe)
python -m bot.main run

# 5. Useful commands
python -m bot.main status                # Current risk state
python -m bot.main calibration-report    # Brier score / log loss
python -m bot.main record-resolution --condition-id 0x... --outcome 1
python -m bot.main backtest --token-id <TOKEN_ID> --question "Will X happen?"
```

Healthcheck endpoint runs alongside the bot:

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/metrics       # Prometheus-style
```

## Safety

The bot uses a **three-gate** safety system for live trading:

1. `TRADING_MODE=live`
2. `ALLOW_LIVE_TRADING=true`
3. `I_UNDERSTAND_REAL_MONEY=YES_TRADE_REAL_FUNDS`

All three must be set. Paper mode is the default — no real orders are ever placed unless all gates pass.

### Risk Controls

- **Half-Kelly sizing** — positions sized at 50% of Kelly optimal
- **Daily loss circuit breaker** — stops trading after $50 daily loss (configurable)
- **Drawdown stop** — halts at 20% drawdown from peak equity
- **Per-position cap** — $100 max per trade
- **Concentration limit** — max 25% of capital in one event
- **Max positions** — 10 concurrent (configurable)

## Architecture

```
bot/
├── main.py                  # CLI + async scheduler
├── config.py                # All settings from env
├── INSTALL.md               # Step-by-step install & verification
├── strategies/
│   ├── probability_arbitrage.py   # Strategy 1
│   ├── logical_arbitrage.py       # Strategy 2
│   └── market_making.py           # Strategy 3
├── core/
│   ├── polymarket_client.py  # CLOB + Gamma API
│   ├── claude_oracle.py      # Claude probability estimation
│   ├── risk_manager.py       # Kelly + drawdown + rejection log
│   ├── calibration.py        # SQLite Brier / log-loss tracking
│   ├── healthcheck.py        # HTTP /health /ready /metrics server
│   └── utils.py              # Data types + helpers
├── backtest/
│   └── engine.py             # Mean-reversion proxy backtester
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── logrotate.conf
│   └── systemd/polymarket-bot.service
├── logs/                     # bot.log, trades.jsonl, rejections.jsonl, calibration.db
└── requirements.txt
```

## Configuration

All settings are environment variables (see `.env.example`). Key knobs:

| Variable | Default | Description |
|----------|---------|-------------|
| `PROB_ARB_MIN_EDGE_PCT` | 0.05 | Minimum edge after fees to trade |
| `PROB_ARB_MIN_CONFIDENCE` | 0.6 | Claude confidence threshold |
| `KELLY_FRACTION` | 0.5 | Half-Kelly (lower = more conservative) |
| `MAX_DRAWDOWN_PCT` | 0.20 | Kill switch at 20% drawdown |
| `MM_BASE_SPREAD_PCT` | 0.04 | Market making base spread |
