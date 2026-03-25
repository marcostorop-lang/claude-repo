"""Trades listing endpoint with filtering, sorting and pagination."""

from __future__ import annotations

from fastapi import APIRouter, Query

router = APIRouter(tags=["trades"])


@router.get("/trades")
def get_trades(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    strategy: str | None = None,
    side: str | None = None,
    condition_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort_by: str = "timestamp",
    sort_dir: str = "desc",
):
    from ..main import get_db
    db = get_db()

    where_clauses: list[str] = []
    params: list = []

    if strategy:
        where_clauses.append("t.strategy = ?")
        params.append(strategy)
    if side:
        where_clauses.append("t.side = ?")
        params.append(side.upper())
    if condition_id:
        where_clauses.append("t.condition_id = ?")
        params.append(condition_id)
    if date_from:
        where_clauses.append("t.timestamp >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("t.timestamp <= ?")
        params.append(date_to)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    allowed_sorts = {"timestamp", "price", "size", "strategy", "side"}
    sort_col = sort_by if sort_by in allowed_sorts else "timestamp"
    direction = "ASC" if sort_dir.lower() == "asc" else "DESC"

    # Total count
    count = db.query_one(f"SELECT COUNT(*) as cnt FROM trades t WHERE {where_sql}", tuple(params))
    total = count["cnt"] if count else 0

    offset = (page - 1) * per_page
    rows = db.query(
        f"SELECT t.*, m.question FROM trades t "
        f"LEFT JOIN markets_cache m ON t.condition_id = m.condition_id "
        f"WHERE {where_sql} ORDER BY t.{sort_col} {direction} LIMIT ? OFFSET ?",
        tuple(params) + (per_page, offset),
    )

    return {
        "trades": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
    }


@router.get("/trades/export")
def export_trades():
    """Return all trades as a flat list for CSV export."""
    from ..main import get_db
    db = get_db()
    rows = db.query(
        "SELECT t.*, m.question FROM trades t "
        "LEFT JOIN markets_cache m ON t.condition_id = m.condition_id "
        "ORDER BY t.timestamp DESC"
    )
    return {"trades": rows}
