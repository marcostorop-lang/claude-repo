# Polymarket Trading Bot

Bot modular en Python para operar en [Polymarket](https://polymarket.com) con modo **paper trading** (simulación) por defecto y arquitectura preparada para **live trading**.

> **AVISO DE RIESGO**: Este software es experimental y con fines educativos. Operar en mercados de predicción conlleva riesgo de pérdida total del capital. Úsalo bajo tu propia responsabilidad.

> **Estado del repo**: este README describe el estado actual (mayo 2026) tras varias rondas de mejora. La rama legacy `bot/` se conserva por sus prototipos con el oráculo Claude pero no es la canónica — todo desarrollo nuevo va en `src/`.

---

## Qué hace

- Descarga mercados activos de Polymarket (Gamma API + CLOB API).
- Filtra por volumen, liquidez, spread, ventana de precio y tiempo a resolución.
- Evalúa varias estrategias: `simple_momentum`, `mean_reversion`, `edge_based`, `composite`, `semantic_mispricing`.
- Aplica un risk manager con Kelly opcional (proper o edge-proxy), ajuste por incertidumbre del edge, filtro temporal y de volatilidad, breaker diario y **breaker de drawdown desde peak**.
- En paper: simula órdenes con slippage por VWAP del libro, partial fills, y fricción de ejecución configurable. Opcionalmente añade latencia y rejection para stress.
- En live: triple gate (`TRADING_MODE=live` ∧ `ALLOW_LIVE_TRADING=true` ∧ `I_UNDERSTAND_REAL_MONEY="YES_TRADE_REAL_FUNDS"`).
- Persiste todo en SQLite (trades, decisión, calibración, resoluciones, tick stats, semantic signals).
- Expone un dashboard FastAPI + Next.js y un endpoint Prometheus `/metrics`.

---

## Estructura del proyecto

```
src/                          # canónico — todo el desarrollo nuevo va aquí
├── main.py                   # CLI Click (15 comandos) + run_loop
├── config.py                 # Config dataclass desde env
├── logger.py                 # logging con redaction de credenciales y trace IDs
├── preflight.py              # validaciones previas a arrancar
├── polymarket/               # auth, cliente unificado, market_data, execution
├── strategy/                 # base + 5 estrategias
├── risk/                     # manager (sizing, breakers, filtros)
├── portfolio/                # tracker FIFO + reconciliación on-chain y paper
├── storage/                  # SQLite con migraciones
├── analysis/                 # Brier, Kelly, performance metrics + bootstrap CI,
│                             # multiple-testing, slippage, fees, calibration,
│                             # edge, fundamentals, tail-risk, semantic engine, ...
├── backtest/                 # engine + walk-forward
├── experiments/              # runner con FDR (Benjamini-Hochberg)
├── tools/                    # utilidades operativas
└── utils/                    # alerts, metrics writer, system_monitor, trace, ...

dashboard/                    # canónico
├── backend/                  # FastAPI (15 routers + /metrics Prometheus)
└── frontend/                 # Next.js + TypeScript

bot/                          # legacy — ver bot/__init__.py
tests/                        # 88 archivos, 1097 tests (unit + integración + property)
docs/                         # runbooks
experiments/                  # manifiestos JSON + resultados generados
```

---

## Instalación

### Requisitos

- Python 3.11 o superior
- `pip`
- (opcional) Node + `better-sqlite3` solo si quieres el generador estático legacy

### Setup

```bash
git clone <repo-url>
cd polymarket-bot

python -m venv .venv
source .venv/bin/activate    # Linux/macOS
# .venv\Scripts\activate     # Windows

pip install -r requirements.txt
cp .env.example .env
```

Para paper trading no necesitas credenciales; los defaults son seguros.

---

## Uso

### Ejecutar en paper (default)

```bash
python -m src.main run-bot
```

El bot:
1. Descarga mercados activos (limitado por `MAX_MARKETS_FETCH`).
2. Filtra por volumen, liquidez, spread, ventana de precio.
3. Evalúa la estrategia configurada en cada mercado.
4. Verifica salidas (SL/TP/edge-flip) en posiciones abiertas.
5. Aplica risk checks (sizing, breakers, filtros).
6. Ejecuta vía paper-engine con slippage y fricción.
7. Persiste en SQLite y emite eventos a JSONL/alerts/Prometheus.
8. Repite cada `POLL_INTERVAL_SECONDS` (default 60).

### Otros comandos disponibles

```bash
python -m src.main backfill-markets         # cachea mercados de Gamma
python -m src.main show-portfolio           # posiciones abiertas
python -m src.main show-trades --limit 50   # últimos trades
python -m src.main show-positions           # detalle con P&L marcado a mercado
python -m src.main backtest <strategy>      # backtest contra price_history
python -m src.main calibration              # buckets de calibración por confianza
python -m src.main edge-calibration         # correlación edge → PnL
python -m src.main check-resolutions        # accuracy histórica vs resolución
python -m src.main detect-arbs              # observador de arbitraje
python -m src.main performance-report       # Sharpe / Sortino / DD + CIs
python -m src.main preflight                # health check de configuración
python -m src.main test-alerts              # smoke a los webhooks
python -m src.main daily-summary            # resumen diario por email/webhook
python -m src.main experiments <manifest>   # sweep con FDR-correction
```

`Ctrl+C` o `SIGTERM` → cierre limpio.

---

## Activar Live Trading

> **Lee esto con cuidado.** Live envía órdenes reales y puedes perder dinero.

### Triple gate

Las **tres** condiciones deben cumplirse:

```env
TRADING_MODE=live
ALLOW_LIVE_TRADING=true
I_UNDERSTAND_REAL_MONEY=YES_TRADE_REAL_FUNDS
```

Falta cualquiera y el bot cae en paper. La frase exacta del tercer gate evita activación accidental por checkbox / typo.

### Credenciales (sólo live)

```env
PRIVATE_KEY=0xTU_CLAVE
POLY_API_KEY=...        # opcional — si vacío, el SDK las deriva
POLY_API_SECRET=...
POLY_PASSPHRASE=...
```

### Pre-live checklist (resumen — completo en `RISK_GUARDRAILS.md`)

- ≥ 4 semanas de paper con `PAPER_FRICTION_BPS≥50`
- `net_pnl_after_costs` positivo y win-rate **significativo después de FDR**
- Reconciliación paper vs DB sin drift por 7 días
- Drawdown breaker no tripado en periodo
- Webhook + Prometheus configurados y testeados con `test-alerts`

---

## Salvaguardas

| Capa | Mecanismo |
|---|---|
| Live gate | Triple `is_live` check en `Config`, `place_order`, `execute` |
| Daily breaker | `MAX_DAILY_LOSS` — auto-pausa BUYs, persiste reinicio |
| Drawdown breaker | `MAX_DRAWDOWN_PCT` desde peak equity, auto-reset en peak nuevo |
| Spread | `MAX_SPREAD` enforced en `risk/manager` y `market_data` |
| Stale data | `STALE_PRICE_SECONDS` rechaza precios viejos |
| First-N live autopause | `LIVE_TRADE_MAX_FIRST_N` requiere ack tras los primeros fills |
| Kill switch | `touch KILL_SWITCH` → cierre inmediato |
| Heartbeat | `SystemMonitor` alerta si un tick tarda > `5×poll_interval` |
| Reconciliación | On-chain (live) y paper-self (interna) con alertas en drift |
| Logger redactor | Enmascara `private_key`, `api_secret`, `passphrase` y hex 0x… |

---

## Observabilidad

- **Logs**: stdout + `bot.log` rotativo (10 MB × 5). Credenciales redactadas.
- **Trace IDs**: cada tick y cada orden recibe ID propagado por `contextvars`.
- **Métricas JSONL**: `METRICS_FILE` → consumible por Loki/Vector.
- **Prometheus**: `GET /metrics` en el dashboard backend (11 series clave).
- **SQLite**: `tick_stats`, `decision_log`, `calibration`, `market_resolutions`, `slippage_records`.
- **Alertas**: webhook Slack/Discord + JSONL local. Severidades `info|warning|critical`.
- **Dashboard**: `dashboard/backend` (FastAPI) consumido por `dashboard/frontend` (Next.js).

---

## Estadística

- Sizing Kelly exacto (`SIZING_KELLY_PROPER=true`) con ajuste opcional por σ del edge (`SIZING_KELLY_UNCERTAINTY=true`): `m = max(0, 1 - cv²)`.
- Brier score temporal (7d vs histórico) con bootstrap CI 95% y flag de drift.
- Bootstrap CI para Sharpe y win-rate en `performance-report`.
- Calibración por buckets de confianza y por buckets de edge.
- Bayesian posterior tracker de win-rate por estrategia.
- Walk-forward backtest con detector de overfitting.
- FDR (Benjamini-Hochberg) en sweeps de experimentos.
- Cancelaciones de mercados excluidas del denominador de accuracy (anti-supervivencia).

---

## Tests

```bash
pytest tests/ -v                    # 1097 tests, ~25 s
pytest tests/test_polymarket_contracts.py -v
pytest tests/test_brier.py -v
```

Cobertura por módulos críticos (auth, market_data, client, risk, portfolio, storage, execution, accounting, calibration). Property-based testing con Hypothesis en `test_portfolio_properties.py`.

---

## Limitaciones conocidas

- HTTP polling, no WebSocket (latencia de descubrimiento ≥ `POLL_INTERVAL_SECONDS`).
- Una sola wallet por instancia.
- Backtest replaya price_history que el propio bot grabó: no hay download masivo de histórico.
- Estrategias semánticas requieren `ANTHROPIC_API_KEY` y costean.
- `bot/` (legacy) tiene una implementación alternativa async; no se sincroniza con `src/`.
- `generate_dashboard.js` queda como deprecated; preferir `dashboard/backend` + `/metrics`.

---

## Documentación adicional

- `CLAUDE.md` — convenciones para asistentes IA y layout del repo.
- `AUDIT_CURRENT_SYSTEM.md` — estado conocido.
- `RISK_GUARDRAILS.md` — checklist y reglas de risk.
- `LEARNING_PHASE_PLAN.md`, `EXPERIMENT_FRAMEWORK.md`, `DASHBOARD_IMPROVEMENT_PLAN.md`.
- `CHANGELOG_AI.md` — historial de cambios automatizados.

---

## Licencia

MIT
