import { NextResponse } from "next/server";
import { query } from "@/lib/db";
export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const strats = query("SELECT DISTINCT strategy FROM trades");
    const result = strats.map((s, i) => {
      const name = s.strategy as string;
      const trades = query("SELECT * FROM trades WHERE strategy = ? ORDER BY timestamp ASC", [name]);
      const bbt: Record<string, Array<Record<string, unknown>>> = {};
      for (const t of trades) { if (t.side === "BUY") { const k = t.token_id as string; if (!bbt[k]) bbt[k] = []; bbt[k].push(t); } }
      let wins = 0, losses = 0, totalPnl = 0, peak = 0, maxDd = 0, running = 0;
      for (const t of trades) {
        if (t.side === "SELL" && bbt[t.token_id as string]?.length) {
          const buy = bbt[t.token_id as string].shift()!;
          const pnl = ((t.price as number) - (buy.price as number)) * Math.min(buy.size as number, t.size as number);
          totalPnl += pnl; running += pnl; peak = Math.max(peak, running); maxDd = Math.max(maxDd, peak - running);
          if (pnl >= 0) wins++; else losses++;
        }
      }
      const closed = wins + losses;
      return { strategy: name, total_trades: trades.length, closed_trades: closed, winning: wins, losing: losses,
        win_rate: closed ? +(wins / closed * 100).toFixed(2) : 0, total_pnl: +totalPnl.toFixed(4), max_drawdown: +maxDd.toFixed(4), rank: i + 1 };
    });
    result.sort((a, b) => b.total_pnl - a.total_pnl);
    result.forEach((r, i) => r.rank = i + 1);
    return NextResponse.json({ strategies: result });
  } catch (err) {
    console.error("Strategies error:", err);
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}
