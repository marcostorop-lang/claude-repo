# Polymarket Trading Bot

Bot modular en Python para operar en [Polymarket](https://polymarket.com) con modo **paper trading** (simulación) por defecto y arquitectura preparada para **live trading**.

> **AVISO DE RIESGO**: Este software es experimental y con fines educativos. Operar en mercados de predicción conlleva riesgo de pérdida total del capital. Úsalo bajo tu propia responsabilidad.

---

## Qué hace

- Descarga mercados activos de Polymarket (Gamma API + CLOB API).
- Filtra mercados por volumen, liquidez y spread.
- Evalúa dos estrategias: **momentum simple** y **mean reversion**.
- Aplica un gestor de riesgo (tamaño máximo, exposición total, stop-loss, take-profit).
- En modo paper: simula órdenes y registra todo en SQLite.
- En modo live: envía órdenes reales al CLOB de Polymarket (desactivado por defecto).
- CLI con comandos: `run-bot`, `backfill-markets`, `show-portfolio`, `show-trades`.

---

## Estructura del proyecto

```
src/
├── main.py                 # Entry point y CLI (Click)
├── config.py               # Configuración desde variables de entorno
├── logger.py               # Setup de logging
├── polymarket/
│   ├── auth.py             # Creación del cliente CLOB (autenticado o no)
│   ├── client.py           # Wrapper unificado Gamma + CLOB
│   ├── market_data.py      # Descarga y filtrado de mercados
│   └── execution.py        # Capa de ejecución (paper/live)
├── strategy/
│   ├── base.py             # Clase abstracta BaseStrategy + Signal
│   ├── simple_momentum.py  # Estrategia de momentum
│   └── mean_reversion.py   # Estrategia de reversión a la media
├── risk/
│   └── manager.py          # Gestor de riesgo
├── portfolio/
│   └── tracker.py          # Seguimiento de posiciones y PnL
├── storage/
│   └── sqlite_store.py     # Persistencia en SQLite
└── utils/
    ├── time_utils.py
    └── math_utils.py
tests/
├── test_config.py
├── test_risk_manager.py
├── test_strategies.py
└── test_math_utils.py
```

---

## Instalación

### 1. Requisitos previos

- Python 3.11 o superior
- pip

### 2. Clonar y preparar entorno

```bash
git clone <repo-url>
cd polymarket-bot

# Crear entorno virtual
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# Instalar dependencias
pip install -r requirements.txt
```

### 3. Configurar variables de entorno

```bash
cp .env.example .env
# Editar .env con tus valores
```

Las variables principales están documentadas en `.env.example`. Para **paper trading** no necesitas credenciales.

---

## Uso

### Ejecutar en modo Paper Trading (por defecto)

```bash
python -m src.main run-bot
```

El bot:
1. Descarga mercados activos de Polymarket.
2. Filtra por volumen, liquidez y spread.
3. Evalúa la estrategia configurada en cada mercado.
4. Simula órdenes y las registra en SQLite.
5. Repite cada `POLL_INTERVAL_SECONDS` (default: 60s).

### Otros comandos

```bash
# Descargar y cachear mercados
python -m src.main backfill-markets

# Ver portafolio (posiciones abiertas)
python -m src.main show-portfolio

# Ver historial de trades
python -m src.main show-trades --limit 50
```

### Detener el bot

`Ctrl+C` — el bot terminará el ciclo actual y se detendrá limpiamente.

---

## Activar Live Trading

> **Lee esto con cuidado.** El live trading envía órdenes reales y puedes perder dinero.

### Pasos manuales necesarios

1. **Wallet de Polymarket**: Necesitas una wallet de Polygon con fondos (USDC).
2. **Private key**: Exporta la private key de tu wallet de Polymarket.
3. **API credentials**: Puedes proporcionarlas directamente o dejar que el SDK las derive desde tu private key (el bot lo intentará automáticamente).
4. **Configurar `.env`**:

```env
TRADING_MODE=live
ALLOW_LIVE_TRADING=true
PRIVATE_KEY=0xTU_CLAVE_PRIVADA_AQUI
# Opcionales si tienes API key explícita:
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_PASSPHRASE=...
```

5. **Verificar**: El bot valida la configuración al arrancar y mostrará warnings si faltan credenciales.

### Doble protección

Ambas condiciones deben cumplirse para que se envíen órdenes reales:
- `TRADING_MODE=live`
- `ALLOW_LIVE_TRADING=true`

Si cualquiera de las dos está desactivada, el bot opera en modo paper.

---

## Tests

```bash
pytest tests/ -v
```

---

## Limitaciones actuales

- **Sin websockets**: El bot usa polling HTTP. Para baja latencia se necesitaría una conexión WebSocket al CLOB.
- **Sin backtesting**: No hay un motor de backtesting integrado; solo paper trading en tiempo real.
- **Historial de precios limitado**: Solo se almacenan los precios que el bot observa en cada tick. No se descargan datos históricos.
- **Estrategias básicas**: Las dos estrategias incluidas son ilustrativas. En producción necesitarías señales más sofisticadas.
- **Sin rebalanceo automático**: El portfolio tracker es in-memory y se reconstruye desde trades al usar los comandos CLI.
- **Sin soporte multi-cuenta**: Una sola wallet/cuenta por instancia.

---

## Mejoras sugeridas

- [ ] WebSocket para streaming de precios en tiempo real.
- [ ] Motor de backtesting con datos históricos.
- [ ] Dashboard web (Streamlit/Dash) para monitoreo.
- [ ] Notificaciones (Telegram, Discord, email).
- [ ] Más estrategias: market making, event-driven, sentiment analysis.
- [ ] Persistencia del portfolio tracker (actualmente in-memory).
- [ ] Rate limiter explícito para respetar los límites de la API.
- [ ] Docker Compose para despliegue fácil.
- [ ] CI/CD con GitHub Actions.

---

## Nota sobre el SDK

El SDK oficial de Polymarket (`py-clob-client`) es el recomendado para interactuar con la CLOB API. Aunque el ecosistema de Polymarket tiene herramientas más maduras en TypeScript, `py-clob-client` cubre las operaciones esenciales (order book, midpoint, spread, colocación de órdenes). Si en el futuro necesitas funcionalidades que solo estén disponibles en el SDK de TypeScript, podrías usar un microservicio Node.js como puente.

---

## Licencia

MIT
