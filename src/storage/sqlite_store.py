"""
SQLite persistence layer for trades and price history.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


class SQLiteStore:
    """Simple SQLite store for trades and price snapshots."""

    def __init__(self, db_path: str = "polymarket_bot.db") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        cur = self._conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id        TEXT NOT NULL,
                token_id        TEXT NOT NULL,
                condition_id    TEXT NOT NULL,
                side            TEXT NOT NULL,
                size            REAL NOT NULL,
                price           REAL NOT NULL,
                strategy        TEXT NOT NULL,
                mode            TEXT NOT NULL,
                timestamp       TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id    TEXT NOT NULL,
                price       REAL NOT NULL,
                timestamp   TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS markets_cache (
                condition_id    TEXT PRIMARY KEY,
                question        TEXT,
                data            TEXT,
                updated_at      TEXT NOT NULL
            )
        """)
        self._conn.commit()

    # -- Trades ----------------------------------------------------------------

    def insert_trade(
        self,
        order_id: str,
        token_id: str,
        condition_id: str,
        side: str,
        size: float,
        price: float,
        strategy: str,
        mode: str,
        timestamp: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO trades (order_id, token_id, condition_id, side, size, price, strategy, mode, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, token_id, condition_id, side, size, price, strategy, mode, timestamp),
        )
        self._conn.commit()

    def get_trades(self, limit: int = 50) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]

    # -- Price history ---------------------------------------------------------

    def insert_price(self, token_id: str, price: float, timestamp: str) -> None:
        self._conn.execute(
            "INSERT INTO price_history (token_id, price, timestamp) VALUES (?, ?, ?)",
            (token_id, price, timestamp),
        )
        self._conn.commit()

    def get_price_history(self, token_id: str, limit: int = 100) -> list[float]:
        cur = self._conn.execute(
            "SELECT price FROM price_history WHERE token_id = ? ORDER BY id DESC LIMIT ?",
            (token_id, limit),
        )
        rows = cur.fetchall()
        return [row["price"] for row in reversed(rows)]

    # -- Markets cache ---------------------------------------------------------

    def upsert_market(self, condition_id: str, question: str, data: str, updated_at: str) -> None:
        self._conn.execute(
            "INSERT INTO markets_cache (condition_id, question, data, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(condition_id) DO UPDATE SET question=excluded.question, data=excluded.data, updated_at=excluded.updated_at",
            (condition_id, question, data, updated_at),
        )
        self._conn.commit()

    def get_cached_markets(self) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM markets_cache ORDER BY updated_at DESC")
        return [dict(row) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
