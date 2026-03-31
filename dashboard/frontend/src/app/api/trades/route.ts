import { NextRequest, NextResponse } from "next/server";
import { query, queryOne } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  try {
    const p = req.nextUrl.searchParams;
    const page = Math.max(1, parseInt(p.get("page") || "1"));
    const perPage = Math.min(100, Math.max(1, parseInt(p.get("per_page") || "20")));
    const strategy = p.get("strategy") || "";
    const side = p.get("side") || "";
    const sortBy = ["timestamp","price","size","strategy","side"].includes(p.get("sort_by") || "") ? p.get("sort_by")! : "timestamp";
    const sortDir = p.get("sort_dir") === "asc" ? "ASC" : "DESC";

    const where: string[] = [];
    const params: unknown[] = [];
    if (strategy) { where.push("t.strategy = ?"); params.push(strategy); }
    if (side) { where.push("t.side = ?"); params.push(side.toUpperCase()); }

    const whereSQL = where.length ? where.join(" AND ") : "1=1";
    const cnt = queryOne(`SELECT COUNT(*) as cnt FROM trades t WHERE ${whereSQL}`, params);
    const total = (cnt?.cnt as number) || 0;
    const offset = (page - 1) * perPage;

    const rows = query(
      `SELECT t.*, m.question FROM trades t LEFT JOIN markets_cache m ON t.condition_id = m.condition_id WHERE ${whereSQL} ORDER BY t.${sortBy} ${sortDir} LIMIT ? OFFSET ?`,
      [...params, perPage, offset]
    );

    return NextResponse.json({ trades: rows, total, page, per_page: perPage, pages: Math.ceil(total / perPage) || 1 });
  } catch (err) {
    console.error("Trades error:", err);
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}
