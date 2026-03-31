import { NextResponse } from "next/server";
import { query, queryOne } from "@/lib/db";
export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const buys = query("SELECT * FROM trades WHERE side='BUY' ORDER BY timestamp ASC");
    const sells = query("SELECT * FROM trades WHERE side='SELL' ORDER BY timestamp ASC");
    const sellMap: Record<string, Array<Record<string, unknown>>> = {};
    for (const s of sells) { const t = s.token_id as string; if (!sellMap[t]) sellMap[t] = []; sellMap[t].push(s); }

    const positions = [];
    for (const b of buys) {
      const tid = b.token_id as string;
      if (sellMap[tid]?.length) { sellMap[tid].shift(); continue; }
      const latest = queryOne("SELECT price FROM price_history WHERE token_id = ? ORDER BY id DESC LIMIT 1", [tid]);
      const cp = (latest?.price as number) || (b.price as number);
      const market = queryOne("SELECT question FROM markets_cache WHERE condition_id = ?", [b.condition_id as string]);
      positions.push({
        token_id: tid, condition_id: b.condition_id, question: market?.question || "",
        side: "BUY", entry_price: b.price, current_price: +cp.toFixed(4), size: b.size,
        unrealised_pnl: +((cp - (b.price as number)) * (b.size as number)).toFixed(4),
        entry_time: b.timestamp, strategy: b.strategy,
        pct_to_stop_loss: 50, pct_to_take_profit: 50,
      });
    }
    return NextResponse.json({ positions });
  } catch (err) {
    console.error("Positions error:", err);
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}
