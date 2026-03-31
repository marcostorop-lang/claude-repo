import { NextRequest, NextResponse } from "next/server";
import { query } from "@/lib/db";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  try {
    const search = req.nextUrl.searchParams.get("search") || "";
    const where = search ? "WHERE m.question LIKE ?" : "";
    const params = search ? [`%${search}%`] : [];
    const rows = query(
      `SELECT m.condition_id, m.question, COUNT(t.id) as total_trades,
       COALESCE(SUM(CASE WHEN t.side='SELL' THEN t.price*t.size ELSE 0 END)-SUM(CASE WHEN t.side='BUY' THEN t.price*t.size ELSE 0 END),0) as pnl
       FROM markets_cache m LEFT JOIN trades t ON m.condition_id=t.condition_id ${where}
       GROUP BY m.condition_id, m.question ORDER BY total_trades DESC`, params
    );
    const markets = rows.map(r => ({ ...r, pnl: +((r.pnl as number) || 0).toFixed(4), win_rate: 0, strategies: "" }));
    return NextResponse.json({ markets });
  } catch (err) {
    console.error("Markets error:", err);
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}
