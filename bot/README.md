# Polymarket Trading Bot

Three-strategy async trading bot for Polymarket prediction markets.

## Strategies

| # | Strategy | Description | Edge Source |
|---|----------|-------------|-------------|
| 1 | **Probability Arbitrage** | Claude estimates independent probability; trade when market deviates | AI calibration vs market |
| 2 | **Logical Arbitrage** | Detect constraint violations between related markets (implies/excludes/sums-to-one) | Structural / graph-based |
| 3 | **Market Making** | Two-sided liquidity with dynamic spreads, inventory management | Spread capture |

## Quick Start

```bash
# 1. Install dependencies
cd bot
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY

# 3. Run (paper mode — default, safe)
python -m bot.main run

# 4. Check status
python -m bot.main status

# 5. Run a backtest
python -m bot.main backtest --token-id <TOKEN_ID> --question "Will X happen?"
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
├── strategies/
│   ├── probability_arbitrage.py   # Strategy 1
│   ├── logical_arbitrage.py       # Strategy 2
│   └── market_making.py           # Strategy 3
├── core/
│   ├── polymarket_client.py  # CLOB + Gamma API
│   ├── claude_oracle.py      # Claude probability estimation
│   ├── risk_manager.py       # Kelly sizing + drawdown
│   └── utils.py              # Data types + helpers
├── backtest/
│   └── engine.py             # Simple backtester
├── logs/                     # Runtime logs + trade JSONL
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
