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
from .routes import config, logs, markets, overview, performance, positions, strategies, trades

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


@app.get("/api/health")
def health():
    return {"status": "ok", "db_path": _DB_PATH}
