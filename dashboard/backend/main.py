"""
Dashboard backend — FastAPI application.

Reads from the bot's SQLite database and exposes REST endpoints
for the Next.js frontend.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import Database
from .routes import attribution, config, drift, latency, logs, markets, metrics, overview, performance, positions, risk, semantic, slippage, strategies, trades

_DB_PATH = os.getenv("SQLITE_DB_PATH", "polymarket_bot.db")
_LOG_FILE = os.getenv("LOG_FILE", "bot.log")

db = Database(_DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    yield
    db.close()


app = FastAPI(title="Polymarket Bot Dashboard", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db() -> Database:
    return db


def get_log_file() -> str:
    return _LOG_FILE


# Register routers
app.include_router(overview.router, prefix="/api")
app.include_router(trades.router, prefix="/api")
app.include_router(performance.router, prefix="/api")
app.include_router(positions.router, prefix="/api")
app.include_router(markets.router, prefix="/api")
app.include_router(strategies.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
app.include_router(config.router, prefix="/api")
app.include_router(semantic.router, prefix="/api")
app.include_router(risk.router, prefix="/api")
app.include_router(drift.router, prefix="/api")
app.include_router(slippage.router, prefix="/api")
app.include_router(latency.router, prefix="/api")
app.include_router(attribution.router, prefix="/api")
# Prometheus exposition under /metrics (unprefixed — convention).
app.include_router(metrics.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "db_path": _DB_PATH}
