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
                risk_detail     TEXT,
                features        TEXT DEFAULT '{}'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS calibration (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_timestamp TEXT NOT NULL,
                exit_timestamp  TEXT,
                token_id        TEXT NOT NULL,
                strategy        TEXT,
                confidence      REAL NOT NULL,
                entry_price     REAL NOT NULL,
                exit_price      REAL,
                exit_reason     TEXT,
                pnl             REAL,
                return_pct      REAL,
                features        TEXT DEFAULT '{}'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tick_stats (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                duration_s      REAL NOT NULL,
                markets_scanned INTEGER NOT NULL DEFAULT 0,
                signals_generated INTEGER NOT NULL DEFAULT 0,
                risk_rejections INTEGER NOT NULL DEFAULT 0,
                trades_executed INTEGER NOT NULL DEFAULT 0,
                open_positions  INTEGER NOT NULL DEFAULT 0,
                total_exposure  REAL NOT NULL DEFAULT 0.0,
                realised_pnl    REAL NOT NULL DEFAULT 0.0,
                unrealised_pnl  REAL NOT NULL DEFAULT 0.0,
                daily_pnl       REAL NOT NULL DEFAULT 0.0,
                skip_warmup     INTEGER NOT NULL DEFAULT 0,
                skip_no_price   INTEGER NOT NULL DEFAULT 0,
                skip_hold       INTEGER NOT NULL DEFAULT 0
            )
        """)
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns to existing tables if missing (backwards-compatible)."""
        cur = self._conn.cursor()
        # trades
        existing = {row[1] for row in cur.execute("PRAGMA table_info(trades)").fetchall()}
        migrations = {
            "exit_reason": "ALTER TABLE trades ADD COLUMN exit_reason TEXT DEFAULT ''",
            "spread_at_entry": "ALTER TABLE trades ADD COLUMN spread_at_entry REAL DEFAULT 0.0",
        }
        for col, sql in migrations.items():
            if col not in existing:
                cur.execute(sql)
                logger.info("Migrated trades table: added column '%s'", col)
        # decision_log: add features column if missing (older DBs)
        existing_dl = {row[1] for row in cur.execute("PRAGMA table_info(decision_log)").fetchall()}
        if existing_dl and "features" not in existing_dl:
            cur.execute("ALTER TABLE decision_log ADD COLUMN features TEXT DEFAULT '{}'")
            logger.info("Migrated decision_log: added column 'features'")
        # price_history: add spread column if missing (older DBs)
        existing_ph = {row[1] for row in cur.execute("PRAGMA table_info(price_history)").fetchall()}
        if existing_ph and "spread" not in existing_ph:
            cur.execute("ALTER TABLE price_history ADD COLUMN spread REAL DEFAULT 0.0")
            logger.info("Migrated price_history: added column 'spread'")
        # tick_stats: add skip counters if missing (older DBs)
        existing_ts = {row[1] for row in cur.execute("PRAGMA table_info(tick_stats)").fetchall()}
        if existing_ts:
            for col in ("skip_warmup", "skip_no_price", "skip_hold"):
                if col not in existing_ts:
                    cur.execute(f"ALTER TABLE tick_stats ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
                    logger.info("Migrated tick_stats: added column '%s'", col)
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
        features: dict | None = None,
    ) -> None:
        """Record a trade decision (entry, exit, skip, rejection) for audit.

        ``features`` is serialised to JSON and stored in the ``features``
        column so downstream analysis can correlate features with outcomes.
        """
        import json
        features_json = json.dumps(features or {}, default=str)
        self._conn.execute(
            "INSERT INTO decision_log (timestamp, token_id, condition_id, action, reason, strategy, confidence, price, spread, signal_detail, risk_detail, features) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (timestamp, token_id, condition_id, action, reason, strategy, confidence, price, spread, signal_detail, risk_detail, features_json),
        )
        self._conn.commit()

    def get_decisions(self, limit: int = 100) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM decision_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]

    # -- Calibration -----------------------------------------------------------

    def insert_calibration_entry(
        self,
        entry_timestamp: str,
        token_id: str,
        strategy: str,
        confidence: float,
        entry_price: float,
        features: dict | None = None,
    ) -> int:
        """Record a new open position for calibration tracking. Returns the row id."""
        import json
        features_json = json.dumps(features or {}, default=str)
        cur = self._conn.execute(
            "INSERT INTO calibration (entry_timestamp, token_id, strategy, confidence, entry_price, features) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry_timestamp, token_id, strategy, confidence, entry_price, features_json),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def update_calibration_exit(
        self,
        token_id: str,
        exit_timestamp: str,
        exit_price: float,
        exit_reason: str,
        pnl: float,
        return_pct: float,
    ) -> None:
        """Close the latest open calibration entry for a token."""
        self._conn.execute(
            "UPDATE calibration SET exit_timestamp=?, exit_price=?, exit_reason=?, pnl=?, return_pct=? "
            "WHERE id = (SELECT id FROM calibration WHERE token_id=? AND exit_timestamp IS NULL ORDER BY id DESC LIMIT 1)",
            (exit_timestamp, exit_price, exit_reason, pnl, return_pct, token_id),
        )
        self._conn.commit()

    def get_calibration_closed(self) -> list[dict]:
        """Return all closed calibration entries (entries with a recorded exit)."""
        cur = self._conn.execute(
            "SELECT * FROM calibration WHERE exit_timestamp IS NOT NULL ORDER BY id ASC"
        )
        return [dict(row) for row in cur.fetchall()]

    # -- Tick stats ------------------------------------------------------------

    def insert_tick_stats(
        self,
        timestamp: str,
        duration_s: float,
        markets_scanned: int = 0,
        signals_generated: int = 0,
        risk_rejections: int = 0,
        trades_executed: int = 0,
        open_positions: int = 0,
        total_exposure: float = 0.0,
        realised_pnl: float = 0.0,
        unrealised_pnl: float = 0.0,
        daily_pnl: float = 0.0,
        skip_warmup: int = 0,
        skip_no_price: int = 0,
        skip_hold: int = 0,
    ) -> None:
        """Record per-tick aggregate statistics for observability."""
        self._conn.execute(
            "INSERT INTO tick_stats (timestamp, duration_s, markets_scanned, signals_generated, risk_rejections, trades_executed, open_positions, total_exposure, realised_pnl, unrealised_pnl, daily_pnl, skip_warmup, skip_no_price, skip_hold) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (timestamp, duration_s, markets_scanned, signals_generated, risk_rejections, trades_executed, open_positions, total_exposure, realised_pnl, unrealised_pnl, daily_pnl, skip_warmup, skip_no_price, skip_hold),
        )
        self._conn.commit()

    # -- Price history ---------------------------------------------------------

    def insert_price(self, token_id: str, price: float, timestamp: str, spread: float = 0.0) -> None:
        self._conn.execute(
            "INSERT INTO price_history (token_id, price, timestamp, spread) VALUES (?, ?, ?, ?)",
            (token_id, price, timestamp, spread),
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
