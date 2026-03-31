import { NextResponse } from "next/server";
import { query } from "@/lib/db";
export const dynamic = "force-dynamic";
export async function GET() {
  const rows = query("SELECT t.*, m.question FROM trades t LEFT JOIN markets_cache m ON t.condition_id = m.condition_id ORDER BY t.timestamp DESC");
  return NextResponse.json({ trades: rows });
}
