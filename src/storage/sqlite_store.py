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
        # Performance + concurrency PRAGMAs.  WAL lets the dashboard's
        # read-only queries proceed while the bot writes a tick batch
        # without blocking either side.  ``synchronous=NORMAL`` is the
        # WAL-recommended pairing — durable across crashes, only at
        # risk of losing the very last commit on hardware power loss
        # (acceptable for paper, and we take periodic backups anyway).
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA temp_store=MEMORY")
        except sqlite3.DatabaseError:
            # Older SQLite or unusual filesystem (e.g. some networked
            # mounts) may refuse WAL.  Fall back silently rather than
            # refusing to start the bot.
            logger.exception("Could not apply performance PRAGMAs — continuing on defaults.")
        self._create_tables()
        self._migrate()
        self._create_indexes()

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
            CREATE TABLE IF NOT EXISTS market_resolutions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id    TEXT NOT NULL,
                token_id        TEXT NOT NULL,
                question        TEXT,
                outcome         TEXT,
                resolved_price  REAL,
                resolution_ts   TEXT,
                our_side        TEXT,
                our_entry_price REAL,
                our_exit_price  REAL,
                our_pnl         REAL,
                prediction_correct INTEGER,
                checked_at      TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS arb_opportunities (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                condition_id    TEXT NOT NULL,
                question        TEXT,
                kind            TEXT NOT NULL,
                sum_prices      REAL NOT NULL,
                discount        REAL NOT NULL,
                edge_pct        REAL NOT NULL,
                legs_json       TEXT,
                category        TEXT,
                min_liquidity   REAL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS semantic_signals (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp             TEXT NOT NULL,
                token_id              TEXT NOT NULL,
                condition_id          TEXT NOT NULL,
                question              TEXT,
                category              TEXT,
                side                  TEXT NOT NULL,
                best_bid              REAL NOT NULL,
                best_ask              REAL NOT NULL,
                midpoint              REAL NOT NULL,
                spread                REAL NOT NULL,
                liquidity             REAL NOT NULL,
                synthetic_fair        REAL NOT NULL,
                synthetic_lower       REAL NOT NULL,
                synthetic_upper       REAL NOT NULL,
                synthetic_method      TEXT NOT NULL,
                synthetic_confidence  REAL NOT NULL,
                synthetic_contributors_n INTEGER NOT NULL,
                gross_edge            REAL NOT NULL,
                net_edge              REAL NOT NULL,
                score                 REAL NOT NULL,
                relations_json        TEXT,
                features_json         TEXT,
                mode                  TEXT NOT NULL DEFAULT 'shadow'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bayesian_posterior (
                strategy    TEXT PRIMARY KEY,
                alpha       REAL NOT NULL,
                beta        REAL NOT NULL,
                n_trades    INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL
            )
        """)
        # ``shadow_decision_log`` and ``shadow_calibration`` are exact
        # mirrors of ``decision_log`` and ``calibration`` used by the
        # opt-in A/B shadow runner (see ``src/strategy/shadow_runner.py``).
        # Kept in separate tables on purpose so a join query is the
        # only way to mix shadow and live decisions — accidentally
        # treating shadow rows as live PnL is not possible.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shadow_decision_log (
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
                features        TEXT DEFAULT '{}'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shadow_calibration (
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
                skip_hold       INTEGER NOT NULL DEFAULT 0,
                var_95          REAL NOT NULL DEFAULT 0.0,
                cvar_95         REAL NOT NULL DEFAULT 0.0,
                worst_case      REAL NOT NULL DEFAULT 0.0
            )
        """)
        self._conn.commit()

    def _create_indexes(self) -> None:
        """Create the indexes the dashboard and reconstruction paths need.

        ``CREATE INDEX IF NOT EXISTS`` is idempotent so this runs cheaply
        on every startup.  Without these, the dashboard's GROUP BYs over
        ``trades`` and the per-token price-history pulls became O(n)
        scans the moment the DB grew past a few thousand rows.
        """
        cur = self._conn.cursor()
        statements = [
            "CREATE INDEX IF NOT EXISTS idx_trades_token        ON trades(token_id)",
            "CREATE INDEX IF NOT EXISTS idx_trades_condition    ON trades(condition_id)",
            "CREATE INDEX IF NOT EXISTS idx_trades_ts           ON trades(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_decision_ts         ON decision_log(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_decision_action     ON decision_log(action)",
            "CREATE INDEX IF NOT EXISTS idx_decision_token      ON decision_log(token_id)",
            "CREATE INDEX IF NOT EXISTS idx_price_token_ts      ON price_history(token_id, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_tick_ts             ON tick_stats(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_calibration_token   ON calibration(token_id)",
            "CREATE INDEX IF NOT EXISTS idx_calibration_exit_ts ON calibration(exit_timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_resolutions_cond    ON market_resolutions(condition_id)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_token_ts   ON semantic_signals(token_id, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_arb_ts              ON arb_opportunities(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_shadow_dec_ts       ON shadow_decision_log(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_shadow_dec_action   ON shadow_decision_log(action)",
            "CREATE INDEX IF NOT EXISTS idx_shadow_cal_token    ON shadow_calibration(token_id)",
            "CREATE INDEX IF NOT EXISTS idx_shadow_cal_exit_ts  ON shadow_calibration(exit_timestamp)",
        ]
        for stmt in statements:
            try:
                cur.execute(stmt)
            except sqlite3.DatabaseError:
                logger.exception("Failed to create index: %s", stmt)
        self._conn.commit()

    def prune_decision_log(self, max_age_days: int) -> int:
        """Delete decision_log rows older than ``max_age_days``.

        Returns the number of rows removed.  The decision log is the
        biggest source of bloat in a long-running paper deployment —
        the bot writes one row per signal and per risk rejection on
        every market on every tick.  At 60s polls and 50 markets, that
        easily clears 70k rows/day; without rotation the table can
        cross 10M rows in a few months and queries against it grind to
        a halt.

        ``max_age_days <= 0`` disables pruning (returns 0).
        """
        if max_age_days <= 0:
            return 0
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM decision_log WHERE timestamp < ?",
            (cutoff,),
        )
        deleted = cur.rowcount or 0
        self._conn.commit()
        if deleted > 0:
            logger.info(
                "Pruned %d decision_log rows older than %d day(s).",
                deleted, max_age_days,
            )
        return deleted

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
            for col in ("var_95", "cvar_95", "worst_case"):
                if col not in existing_ts:
                    cur.execute(f"ALTER TABLE tick_stats ADD COLUMN {col} REAL NOT NULL DEFAULT 0.0")
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

    def get_closed_trade_returns(self, *, limit: int | None = None) -> list[float]:
        """Return per-trade return_pct values for closed calibration entries.

        Newest-first when ``limit`` is supplied (the typical use case
        is "last N trades for a rolling Sharpe"); chronological-asc
        when not, so callers that want a true equity curve get the
        natural ordering.

        Skips rows whose ``return_pct`` is NULL (an exit row that
        never finished writing) so the caller never sees ``None`` in
        the output list.
        """
        if limit is not None and limit > 0:
            cur = self._conn.execute(
                "SELECT return_pct FROM calibration "
                "WHERE exit_timestamp IS NOT NULL AND return_pct IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
            rows = [float(r[0]) for r in cur.fetchall()]
            rows.reverse()  # caller usually wants chrono order even when capped
            return rows
        cur = self._conn.execute(
            "SELECT return_pct FROM calibration "
            "WHERE exit_timestamp IS NOT NULL AND return_pct IS NOT NULL "
            "ORDER BY id ASC"
        )
        return [float(r[0]) for r in cur.fetchall()]

    # -- Shadow A/B mirrors ---------------------------------------------------
    # Same shape as the live decision/calibration writers, but writing
    # to the ``shadow_*`` tables.  Kept as separate methods (no shared
    # helper with a ``table`` parameter) so an audit grep for "writes
    # to ``calibration``" still finds the live path only.

    def insert_shadow_decision(
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
        features: dict | None = None,
    ) -> None:
        import json
        features_json = json.dumps(features or {}, default=str)
        self._conn.execute(
            "INSERT INTO shadow_decision_log (timestamp, token_id, condition_id, action, reason, strategy, confidence, price, spread, signal_detail, features) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (timestamp, token_id, condition_id, action, reason, strategy, confidence, price, spread, signal_detail, features_json),
        )
        self._conn.commit()

    def insert_shadow_calibration_entry(
        self,
        entry_timestamp: str,
        token_id: str,
        strategy: str,
        confidence: float,
        entry_price: float,
        features: dict | None = None,
    ) -> int:
        import json
        features_json = json.dumps(features or {}, default=str)
        cur = self._conn.execute(
            "INSERT INTO shadow_calibration (entry_timestamp, token_id, strategy, confidence, entry_price, features) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry_timestamp, token_id, strategy, confidence, entry_price, features_json),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def update_shadow_calibration_exit(
        self,
        token_id: str,
        exit_timestamp: str,
        exit_price: float,
        exit_reason: str,
        pnl: float,
        return_pct: float,
    ) -> None:
        self._conn.execute(
            "UPDATE shadow_calibration SET exit_timestamp=?, exit_price=?, exit_reason=?, pnl=?, return_pct=? "
            "WHERE id = (SELECT id FROM shadow_calibration WHERE token_id=? AND exit_timestamp IS NULL ORDER BY id DESC LIMIT 1)",
            (exit_timestamp, exit_price, exit_reason, pnl, return_pct, token_id),
        )
        self._conn.commit()

    def get_shadow_closed_returns(
        self, *, limit: int | None = None, strategy: str | None = None,
    ) -> list[float]:
        """Per-trade returns from ``shadow_calibration``.

        ``strategy`` filters by the writer's strategy name so multi-
        shadow setups don't collapse different runners into one
        series.  ``None`` (default) returns the union — useful for
        the legacy single-shadow case and for aggregate dashboards.
        """
        where = ["exit_timestamp IS NOT NULL", "return_pct IS NOT NULL"]
        params: list = []
        if strategy:
            where.append("strategy = ?")
            params.append(strategy)
        where_sql = " AND ".join(where)
        if limit is not None and limit > 0:
            cur = self._conn.execute(
                f"SELECT return_pct FROM shadow_calibration WHERE {where_sql} "
                f"ORDER BY id DESC LIMIT ?",
                (*params, int(limit)),
            )
            rows = [float(r[0]) for r in cur.fetchall()]
            rows.reverse()
            return rows
        cur = self._conn.execute(
            f"SELECT return_pct FROM shadow_calibration WHERE {where_sql} "
            f"ORDER BY id ASC",
            tuple(params),
        )
        return [float(r[0]) for r in cur.fetchall()]

    def compute_shadow_win_rate(self, *, strategy: str | None = None) -> dict:
        """Same contract as :meth:`compute_win_rate` but over shadow exits.

        ``strategy`` filters by writer; ``None`` aggregates across all
        shadow runners.
        """
        if strategy:
            cur = self._conn.execute(
                "SELECT pnl FROM shadow_calibration "
                "WHERE exit_timestamp IS NOT NULL AND strategy = ?",
                (strategy,),
            )
        else:
            cur = self._conn.execute(
                "SELECT pnl FROM shadow_calibration WHERE exit_timestamp IS NOT NULL"
            )
        wins = losses = breakeven = 0
        for (pnl,) in cur.fetchall():
            if pnl is None:
                continue
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            else:
                breakeven += 1
        decisive = wins + losses
        win_rate = (wins / decisive) if decisive > 0 else 0.0
        return {
            "wins": wins, "losses": losses, "breakeven": breakeven,
            "total_closed": wins + losses + breakeven,
            "win_rate": round(win_rate, 4),
        }

    def compute_win_rate(self) -> dict:
        """Aggregate realised win/loss stats from the calibration table.

        Counts only *closed* trades, so the figure is honest about the
        fact that an open losing position is not yet a "loss".  The
        complementary unrealised picture lives in PortfolioTracker.

        ``breakeven`` (PnL == 0) is reported separately so a noisy run
        full of zero-PnL paper exits can't artificially inflate the
        win rate.  ``win_rate`` is wins / (wins + losses) — breakevens
        are excluded from the denominator on purpose.
        """
        cur = self._conn.execute(
            "SELECT pnl FROM calibration WHERE exit_timestamp IS NOT NULL"
        )
        wins = losses = breakeven = 0
        for (pnl,) in cur.fetchall():
            if pnl is None:
                continue
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            else:
                breakeven += 1
        decisive = wins + losses
        win_rate = (wins / decisive) if decisive > 0 else 0.0
        return {
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "total_closed": wins + losses + breakeven,
            "win_rate": round(win_rate, 4),
        }

    # -- Tick stats ------------------------------------------------------------

    # -- Arbitrage opportunities (read-only observer) --------------------------

    def insert_arb_opportunities(self, timestamp: str, arbs) -> None:
        """Persist a batch of detected arb opportunities.

        ``arbs`` is a sequence of :class:`src.analysis.arb_detector.ArbOpportunity`.
        We serialise the ``legs`` list as JSON for forensic reconstruction.
        Fails silently if the table does not yet exist (allows old DBs to
        run without migration).
        """
        import json
        rows = []
        for a in arbs:
            rows.append((
                timestamp,
                a.condition_id,
                a.question or "",
                a.kind,
                float(a.sum_prices),
                float(a.discount),
                float(a.edge_pct),
                json.dumps(list(a.legs)),
                a.category or "",
                float(a.min_liquidity),
            ))
        if not rows:
            return
        self._conn.executemany(
            "INSERT INTO arb_opportunities "
            "(timestamp, condition_id, question, kind, sum_prices, discount, "
            "edge_pct, legs_json, category, min_liquidity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def get_recent_arb_opportunities(self, limit: int = 50) -> list[dict]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM arb_opportunities ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
        except sqlite3.OperationalError:
            return []

    # -- Semantic mispricing signals (read-only observer) ---------------------

    def insert_semantic_signals(self, timestamp: str, mispricings, mode: str = "shadow") -> None:
        """Persist detected semantic mispricings.

        ``mispricings`` is a sequence of
        :class:`src.analysis.semantic_engine.types.SemanticMispricing`.
        The ``relations`` and ``features`` are serialised as JSON for
        forensic reconstruction.  Silent no-op on missing table so old DBs
        still run without migration (observer pattern — must never break
        the tick).
        """
        import json
        rows = []
        for m in mispricings:
            rels_payload = [
                {
                    "target": r.target_token_id,
                    "sibling": r.sibling_token_id,
                    "kind": r.kind.value,
                    "confidence": r.confidence,
                    "reason": r.reason,
                    "evidence": r.evidence,
                }
                for r in m.relations
            ]
            rows.append((
                timestamp,
                m.token_id,
                m.condition_id,
                m.question or "",
                m.category or "",
                m.side,
                float(m.best_bid),
                float(m.best_ask),
                float(m.midpoint),
                float(m.spread),
                float(m.liquidity),
                float(m.synthetic.point),
                float(m.synthetic.lower),
                float(m.synthetic.upper),
                m.synthetic.method,
                float(m.synthetic.confidence),
                int(m.synthetic.n_contributors),
                float(m.gross_edge),
                float(m.net_edge),
                float(m.score),
                json.dumps(rels_payload),
                json.dumps(m.features),
                mode,
            ))
        if not rows:
            return
        try:
            self._conn.executemany(
                "INSERT INTO semantic_signals "
                "(timestamp, token_id, condition_id, question, category, side, "
                "best_bid, best_ask, midpoint, spread, liquidity, "
                "synthetic_fair, synthetic_lower, synthetic_upper, synthetic_method, "
                "synthetic_confidence, synthetic_contributors_n, "
                "gross_edge, net_edge, score, relations_json, features_json, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            logger.warning("insert_semantic_signals skipped: %s", exc)

    def get_recent_semantic_signals(self, limit: int = 50) -> list[dict]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM semantic_signals ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
        except sqlite3.OperationalError:
            return []

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
        var_95: float = 0.0,
        cvar_95: float = 0.0,
        worst_case: float = 0.0,
    ) -> None:
        """Record per-tick aggregate statistics for observability."""
        self._conn.execute(
            "INSERT INTO tick_stats (timestamp, duration_s, markets_scanned, signals_generated, risk_rejections, trades_executed, open_positions, total_exposure, realised_pnl, unrealised_pnl, daily_pnl, skip_warmup, skip_no_price, skip_hold, var_95, cvar_95, worst_case) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (timestamp, duration_s, markets_scanned, signals_generated, risk_rejections, trades_executed, open_positions, total_exposure, realised_pnl, unrealised_pnl, daily_pnl, skip_warmup, skip_no_price, skip_hold, var_95, cvar_95, worst_case),
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

    def get_price_history_since(self, token_id: str, since_iso: str) -> list[dict]:
        """Return ``(timestamp, price)`` rows for a token since ``since_iso``.

        Ordered ascending by insertion id.  Used by the staleness
        monitor to detect "no meaningful price movement in N days".
        Returns an empty list when the token is unknown or no rows
        match the cut-off.
        """
        cur = self._conn.execute(
            "SELECT timestamp, price FROM price_history "
            "WHERE token_id = ? AND timestamp >= ? ORDER BY id ASC",
            (token_id, since_iso),
        )
        return [dict(row) for row in cur.fetchall()]

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

    # -- Market resolutions ----------------------------------------------------

    def insert_resolution(
        self,
        condition_id: str,
        token_id: str,
        question: str,
        outcome: str,
        resolved_price: float,
        resolution_ts: str,
        our_side: str,
        our_entry_price: float,
        our_exit_price: float,
        our_pnl: float,
        prediction_correct: bool,
        checked_at: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO market_resolutions "
            "(condition_id, token_id, question, outcome, resolved_price, resolution_ts, "
            "our_side, our_entry_price, our_exit_price, our_pnl, prediction_correct, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (condition_id, token_id, question, outcome, resolved_price, resolution_ts,
             our_side, our_entry_price, our_exit_price, our_pnl, int(prediction_correct), checked_at),
        )
        self._conn.commit()

    def get_resolutions(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM market_resolutions ORDER BY checked_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]

    def get_resolution_stats(self) -> dict:
        """Return aggregate resolution statistics."""
        cur = self._conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(prediction_correct) as correct, "
            "SUM(our_pnl) as total_pnl "
            "FROM market_resolutions"
        )
        row = cur.fetchone()
        total = row["total"] or 0
        correct = row["correct"] or 0
        total_pnl = row["total_pnl"] or 0.0
        return {
            "total_resolved": total,
            "correct_predictions": correct,
            "accuracy": correct / total if total > 0 else 0.0,
            "total_pnl": total_pnl,
        }

    # -- Bayesian posterior ---------------------------------------------------

    def upsert_bayesian_posterior(
        self, strategy: str, alpha: float, beta: float,
        n_trades: int, updated_at: str,
    ) -> None:
        """Persist a per-strategy Beta(α, β) posterior.

        Idempotent overwrite — callers compute the new α/β in memory and
        call this once per update.  Keeps the write path simple and
        testable without giving the DB layer any opinion on priors.
        """
        self._conn.execute(
            "INSERT INTO bayesian_posterior (strategy, alpha, beta, n_trades, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(strategy) DO UPDATE SET "
            "alpha=excluded.alpha, beta=excluded.beta, "
            "n_trades=excluded.n_trades, updated_at=excluded.updated_at",
            (strategy, alpha, beta, n_trades, updated_at),
        )
        self._conn.commit()

    def get_bayesian_posterior(self, strategy: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM bayesian_posterior WHERE strategy = ?", (strategy,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_all_bayesian_posteriors(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM bayesian_posterior ORDER BY strategy ASC",
        )
        return [dict(row) for row in cur.fetchall()]

    def get_traded_condition_ids(self) -> set[str]:
        """Return all condition_ids where we have executed trades."""
        cur = self._conn.execute("SELECT DISTINCT condition_id FROM trades")
        return {row["condition_id"] for row in cur.fetchall()}

    def close(self) -> None:
        self._conn.close()
