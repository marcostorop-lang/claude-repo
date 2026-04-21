"""Calibration tracker — persist Claude oracle estimates + outcomes.

Every probability estimate the oracle returns is appended to a SQLite
DB along with market identifiers and trade context.  When a market
resolves, ``record_outcome`` fills in the realized label and we can
compute Brier score, log loss, and reliability curves.

The entire module is best-effort: failures never crash the bot.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "logs" / "calibration.db"
_LOCK = threading.Lock()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS estimates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    question TEXT,
    p_claude REAL NOT NULL,
    p_market REAL,
    confidence REAL,
    edge REAL,
    edge_direction TEXT,
    reasoning TEXT,
    outcome INTEGER DEFAULT NULL,
    resolved_ts REAL DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_estimates_condition ON estimates(condition_id);
CREATE INDEX IF NOT EXISTS idx_estimates_ts ON estimates(ts);
"""


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH), timeout=5)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _init_db() -> None:
    try:
        with _conn() as con:
            con.executescript(_SCHEMA)
    except Exception:
        logger.debug("Calibration DB init failed.", exc_info=True)


_init_db()


@dataclass(frozen=True)
class CalibrationMetrics:
    """Aggregate calibration scores."""

    n_resolved: int
    n_pending: int
    brier_score: float
    log_loss: float
    mean_p_claude: float
    mean_outcome: float


def record_estimate(
    *,
    strategy: str,
    condition_id: str,
    token_id: str,
    question: str,
    p_claude: float,
    p_market: float | None,
    confidence: float,
    edge: float,
    edge_direction: str,
    reasoning: str,
) -> None:
    """Persist one probability estimate.  Silent on failure."""
    try:
        with _LOCK, _conn() as con:
            con.execute(
                """
                INSERT INTO estimates
                  (ts, strategy, condition_id, token_id, question,
                   p_claude, p_market, confidence, edge, edge_direction, reasoning)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    time.time(), strategy, condition_id, token_id, question[:200],
                    float(p_claude),
                    float(p_market) if p_market is not None else None,
                    float(confidence), float(edge), edge_direction, reasoning[:500],
                ),
            )
    except Exception:
        logger.debug("record_estimate failed.", exc_info=True)


def record_outcome(condition_id: str, outcome: int) -> int:
    """Mark all estimates for this market as resolved.  Returns row count."""
    if outcome not in (0, 1):
        return 0
    try:
        with _LOCK, _conn() as con:
            cur = con.execute(
                """
                UPDATE estimates
                   SET outcome = ?, resolved_ts = ?
                 WHERE condition_id = ? AND outcome IS NULL
                """,
                (outcome, time.time(), condition_id),
            )
            return cur.rowcount
    except Exception:
        logger.debug("record_outcome failed.", exc_info=True)
        return 0


def compute_metrics(strategy: str = "") -> CalibrationMetrics:
    """Compute Brier score + log loss across resolved estimates."""
    try:
        with _conn() as con:
            sql = "SELECT p_claude, outcome FROM estimates WHERE outcome IS NOT NULL"
            args: tuple = ()
            if strategy:
                sql += " AND strategy = ?"
                args = (strategy,)
            rows = con.execute(sql, args).fetchall()

            pending_sql = "SELECT COUNT(*) FROM estimates WHERE outcome IS NULL"
            pending_args: tuple = ()
            if strategy:
                pending_sql += " AND strategy = ?"
                pending_args = (strategy,)
            (pending,) = con.execute(pending_sql, pending_args).fetchone()
    except Exception:
        logger.debug("compute_metrics failed.", exc_info=True)
        return CalibrationMetrics(0, 0, 0.0, 0.0, 0.0, 0.0)

    n = len(rows)
    if n == 0:
        return CalibrationMetrics(0, pending, 0.0, 0.0, 0.0, 0.0)

    import math

    brier_sum = 0.0
    ll_sum = 0.0
    p_sum = 0.0
    y_sum = 0.0
    eps = 1e-6
    for p, y in rows:
        p = max(eps, min(1 - eps, float(p)))
        y = int(y)
        brier_sum += (p - y) ** 2
        ll_sum += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        p_sum += p
        y_sum += y

    return CalibrationMetrics(
        n_resolved=n,
        n_pending=pending,
        brier_score=brier_sum / n,
        log_loss=ll_sum / n,
        mean_p_claude=p_sum / n,
        mean_outcome=y_sum / n,
    )


def recent_estimates(limit: int = 50) -> list[dict]:
    """Return the most recent estimates for inspection."""
    try:
        with _conn() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM estimates ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        logger.debug("recent_estimates failed.", exc_info=True)
        return []
