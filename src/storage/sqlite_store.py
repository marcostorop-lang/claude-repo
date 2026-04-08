"""
SQLite persistence layer for trades, price history, and decision logs.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


class SQLiteStore:
    """SQLite store for trades, price snapshots, and decision audit trail."""

    def __init__(self, db_path: str = "polymarket_bot.db") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        self._migrate()

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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS decision_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                token_id        TEXT NOT NULL,
                condition_id    TEXT NOT NULL,
                action          TEXT NOT NULL,
                reason          TEXT NOT NULL,
                strategy        TEXT,
                confidence      REAL,
                price           REAL,
                spread          REAL,
                signal_detail   TEXT,
                risk_detail     TEXT
            )
        """)
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns to existing tables if missing (backwards-compatible)."""
        cur = self._conn.cursor()
        # Check existing columns on trades table
        existing = {row[1] for row in cur.execute("PRAGMA table_info(trades)").fetchall()}
        migrations = {
            "exit_reason": "ALTER TABLE trades ADD COLUMN exit_reason TEXT DEFAULT ''",
            "spread_at_entry": "ALTER TABLE trades ADD COLUMN spread_at_entry REAL DEFAULT 0.0",
        }
        for col, sql in migrations.items():
            if col not in existing:
                cur.execute(sql)
                logger.info("Migrated trades table: added column '%s'", col)
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
        exit_reason: str = "",
        spread_at_entry: float = 0.0,
    ) -> None:
        self._conn.execute(
            "INSERT INTO trades (order_id, token_id, condition_id, side, size, price, strategy, mode, timestamp, exit_reason, spread_at_entry) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, token_id, condition_id, side, size, price, strategy, mode, timestamp, exit_reason, spread_at_entry),
        )
        self._conn.commit()

    def get_trades(self, limit: int = 50) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]

    def get_all_trades(self) -> list[dict]:
        """Return all trades ordered by timestamp ascending (for portfolio reconstruction)."""
        cur = self._conn.execute("SELECT * FROM trades ORDER BY timestamp ASC")
        return [dict(row) for row in cur.fetchall()]

    # -- Decision log ----------------------------------------------------------

    def insert_decision(
        self,
        timestamp: str,
        token_id: str,
        condition_id: str,
        action: str,
        reason: str,
        strategy: str = "",
        confidence: float = 0.0,
        price: float = 0.0,
        spread: float = 0.0,
        signal_detail: str = "",
        risk_detail: str = "",
    ) -> None:
        """Record a trade decision (entry, exit, skip, rejection) for audit."""
        self._conn.execute(
            "INSERT INTO decision_log (timestamp, token_id, condition_id, action, reason, strategy, confidence, price, spread, signal_detail, risk_detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (timestamp, token_id, condition_id, action, reason, strategy, confidence, price, spread, signal_detail, risk_detail),
        )
        self._conn.commit()

    def get_decisions(self, limit: int = 100) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM decision_log ORDER BY id DESC LIMIT ?", (limit,))
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
