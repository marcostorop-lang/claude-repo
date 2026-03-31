import { NextResponse } from "next/server";
import { query, queryOne } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const total = queryOne("SELECT COUNT(*) as cnt FROM trades") || { cnt: 0 };
    const buys = query("SELECT token_id, condition_id, price, size, strategy, timestamp FROM trades WHERE side='BUY' ORDER BY timestamp");
    const sells = query("SELECT token_id, price, size, timestamp FROM trades WHERE side='SELL' ORDER BY timestamp");

    const sellMap: Record<string, Array<Record<string, unknown>>> = {};
    for (const s of sells) {
      const tid = s.token_id as string;
      if (!sellMap[tid]) sellMap[tid] = [];
      sellMap[tid].push(s);
    }

    let wins = 0, losses = 0, totalPnl = 0;
    const openPositions = new Set<string>();

    for (const b of buys) {
      const tid = b.token_id as string;
      if (sellMap[tid]?.length) {
        const s = sellMap[tid].shift()!;
        const pnl = ((s.price as number) - (b.price as number)) * Math.min(b.size as number, s.size as number);
        totalPnl += pnl;
        if (pnl >= 0) wins++; else losses++;
      } else {
        openPositions.add(tid);
      }
    }

    const lastTrade = queryOne("SELECT timestamp FROM trades ORDER BY id DESC LIMIT 1");
    const marketsCount = queryOne("SELECT COUNT(DISTINCT condition_id) as cnt FROM markets_cache") || { cnt: 0 };

    return NextResponse.json({
      bot_active: true,
      last_update: lastTrade?.timestamp || null,
      total_trades: total.cnt,
      winning_trades: wins,
      losing_trades: losses,
      total_pnl: +totalPnl.toFixed(4),
      daily_pnl: 0,
      current_exposure: 0,
      simulated_balance: +(1000 + totalPnl).toFixed(4),
      markets_monitored: marketsCount.cnt,
      open_positions: openPositions.size,
    });
  } catch (err) {
    console.error("Overview error:", err);
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}
