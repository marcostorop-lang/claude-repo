"""
Database abstraction layer.

Reads from the bot's SQLite database. Designed so the DB engine can be
swapped to PostgreSQL later by changing this single module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class Database:
    """Thin wrapper around SQLite with dict-row support."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        if not Path(self.db_path).exists():
            # Create empty DB with schema so the dashboard works without the bot
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._create_tables()
            self._seed_mock_data()
        else:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        assert self._conn is not None, "Database not connected"
        cur = self._conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def _create_tables(self) -> None:
        assert self._conn is not None
        self._conn.executescript("""
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
            );
            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id    TEXT NOT NULL,
                price       REAL NOT NULL,
                timestamp   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS markets_cache (
                condition_id    TEXT PRIMARY KEY,
                question        TEXT,
                data            TEXT,
                updated_at      TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def _seed_mock_data(self) -> None:
        """Insert realistic mock data so the dashboard is usable immediately."""
        import json
        import random
        from datetime import datetime, timedelta, timezone

        assert self._conn is not None

        random.seed(42)
        now = datetime.now(timezone.utc)
        strategies = ["simple_momentum", "mean_reversion"]
        markets_info = [
            ("cond_001", "Will BTC exceed $100k by end of 2026?", "tok_001a", "tok_001b"),
            ("cond_002", "Will the Fed cut rates in Q2 2026?", "tok_002a", "tok_002b"),
            ("cond_003", "Will ETH flip BTC market cap?", "tok_003a", "tok_003b"),
            ("cond_004", "US GDP growth > 3% in 2026?", "tok_004a", "tok_004b"),
            ("cond_005", "Will AI regulation pass in 2026?", "tok_005a", "tok_005b"),
            ("cond_006", "Will SpaceX land on Mars by 2030?", "tok_006a", "tok_006b"),
            ("cond_007", "Will Ethereum merge to POS succeed?", "tok_007a", "tok_007b"),
            ("cond_008", "Democrats win 2026 midterms?", "tok_008a", "tok_008b"),
        ]

        # Insert markets cache
        for cid, question, tok_yes, tok_no in markets_info:
            data = json.dumps({
                "conditionId": cid,
                "question": question,
                "clobTokenIds": [tok_yes, tok_no],
                "outcomes": ["Yes", "No"],
                "volume": random.uniform(5000, 500000),
                "liquidity": random.uniform(1000, 50000),
                "active": True,
            })
            self._conn.execute(
                "INSERT OR IGNORE INTO markets_cache (condition_id, question, data, updated_at) VALUES (?, ?, ?, ?)",
                (cid, question, data, now.isoformat()),
            )

        # Generate 120 mock trades over the past 30 days
        trade_id = 0
        for day_offset in range(30, 0, -1):
            num_trades = random.randint(2, 8)
            for _ in range(num_trades):
                trade_id += 1
                cid, question, tok_yes, tok_no = random.choice(markets_info)
                is_buy = random.random() > 0.35
                side = "BUY" if is_buy else "SELL"
                token_id = tok_yes if random.random() > 0.4 else tok_no
                price = round(random.uniform(0.15, 0.85), 4)
                size = round(random.uniform(5, 50), 2)
                strategy = random.choice(strategies)
                ts = now - timedelta(days=day_offset, hours=random.randint(0, 23), minutes=random.randint(0, 59))

                self._conn.execute(
                    "INSERT INTO trades (order_id, token_id, condition_id, side, size, price, strategy, mode, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"paper-mock{trade_id:04d}", token_id, cid, side, size, price, strategy, "paper", ts.isoformat()),
                )

        # Generate price history
        for _, _, tok_yes, tok_no in markets_info:
            base_price = random.uniform(0.3, 0.7)
            for hour in range(720, 0, -1):  # 30 days of hourly data
                ts = now - timedelta(hours=hour)
                base_price += random.gauss(0, 0.008)
                base_price = max(0.05, min(0.95, base_price))
                self._conn.execute(
                    "INSERT INTO price_history (token_id, price, timestamp) VALUES (?, ?, ?)",
                    (tok_yes, round(base_price, 4), ts.isoformat()),
                )
                self._conn.execute(
                    "INSERT INTO price_history (token_id, price, timestamp) VALUES (?, ?, ?)",
                    (tok_no, round(1 - base_price, 4), ts.isoformat()),
                )

        self._conn.commit()
