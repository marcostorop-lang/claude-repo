# Polymarket Bot Dashboard

Dashboard web para monitorizar el bot de trading de Polymarket en tiempo real e histórico.

**Stack**: FastAPI (Python) + Next.js 14 (TypeScript) + Tailwind CSS + Recharts + SWR

---

## Vista rápida

| Sección | Descripción |
|---------|-------------|
| **Overview** | Estado del bot, PnL total/diario, exposición, balance simulado |
| **Trades** | Tabla completa con filtros, paginación, ordenación y export CSV |
| **Performance** | Equity curve, PnL diario, win rate, profit factor, drawdown, donut |
| **Positions** | Posiciones abiertas con PnL no realizado, SL/TP progress bars |
| **Markets** | Resultados por mercado, búsqueda, PnL y win rate por mercado |
| **Strategies** | Comparativa de estrategias, ranking por PnL |
| **Logs** | Logs del bot con filtro por nivel (INFO/WARNING/ERROR) |
| **Config** | Configuración actual del bot (read-only, sin secretos) |

---

## Instalación

### 1. Backend (FastAPI)

```bash
cd dashboard/backend
python -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn python-dotenv
```

### 2. Frontend (Next.js)

```bash
cd dashboard/frontend
npm install
```

---

## Ejecución

### Arrancar backend

```bash
cd dashboard
# Apuntar a la BD del bot (por defecto crea una con datos mock)
export SQLITE_DB_PATH=../polymarket_bot.db
export LOG_FILE=../bot.log

uvicorn backend.main:app --reload --port 8000
```

El backend escucha en `http://localhost:8000`. Si la BD no existe, crea una con **datos mock realistas** (~120 trades, 8 mercados, 30 días de precio).

### Arrancar frontend

```bash
cd dashboard/frontend
npm run dev
```

Abre `http://localhost:3000`. El frontend hace proxy de `/api/*` al backend en el puerto 8000.

### Script todo-en-uno

```bash
# Terminal 1: Backend
cd dashboard && SQLITE_DB_PATH=../polymarket_bot.db uvicorn backend.main:app --reload --port 8000

# Terminal 2: Frontend
cd dashboard/frontend && npm run dev
```

---

## Conectar la base de datos del bot

El backend lee directamente del archivo SQLite que genera el bot (`polymarket_bot.db`). Para conectarlo:

```bash
# Opción 1: Variable de entorno
export SQLITE_DB_PATH=/ruta/a/polymarket_bot.db

# Opción 2: Symlink
ln -s /ruta/a/polymarket_bot.db dashboard/polymarket_bot.db
```

Si ejecutas el bot y el dashboard simultáneamente, ambos acceden al mismo archivo SQLite. SQLite soporta lecturas concurrentes sin problema.

---

## Intervalo de refresco

El frontend refresca datos automáticamente cada **10 segundos** (configurable en `src/lib/hooks.ts`):

```typescript
const REFRESH = 10_000; // milisegundos
```

Cada hook SWR usa este intervalo. Un indicador visual en el header muestra el estado de sincronización.

---

## Exportación

- **Trades**: Botón "Export CSV" en la pestaña Trades → descarga todos los trades
- **Performance**: Botón "Export Summary" → descarga métricas de rendimiento

---

## Arquitectura

```
dashboard/
├── backend/
│   ├── main.py          # FastAPI app, CORS, routers
│   ├── database.py      # Abstracción DB (SQLite, preparada para PostgreSQL)
│   └── routes/
│       ├── overview.py   # GET /api/overview
│       ├── trades.py     # GET /api/trades, /api/trades/export
│       ├── performance.py# GET /api/performance
│       ├── positions.py  # GET /api/positions
│       ├── markets.py    # GET /api/markets
│       ├── strategies.py # GET /api/strategies
│       ├── logs.py       # GET /api/logs
│       └── config.py     # GET /api/config
├── frontend/
│   └── src/
│       ├── app/          # Next.js App Router pages
│       ├── components/   # UI components, charts, layout
│       ├── lib/          # API client, SWR hooks
│       └── types/        # TypeScript interfaces
└── README.md
```

### Migración a PostgreSQL

El módulo `database.py` centraliza todo el acceso a datos. Para migrar:
1. Reemplazar `sqlite3` por `asyncpg` o `psycopg2`
2. Ajustar la creación de tablas (el SQL es compatible)
3. Cambiar la cadena de conexión

---

## Datos mock vs reales

| Componente | Estado |
|-----------|--------|
| Overview stats | **Datos reales** del SQLite del bot |
| Trades table | **Datos reales** del SQLite del bot |
| Performance metrics | **Calculados** en tiempo real desde los trades |
| Open positions | **Datos reales** (reconstruidos desde trades + price_history) |
| Markets | **Datos reales** desde markets_cache del bot |
| Strategies | **Calculados** en tiempo real desde los trades |
| Logs | **Datos reales** si existe bot.log, **mock** si no |
| Config | **Datos reales** (lee variables de entorno del proceso) |

Si no existe la BD al arrancar el backend, se genera automáticamente con datos mock realistas para que el dashboard sea funcional de inmediato.
