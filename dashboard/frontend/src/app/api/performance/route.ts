import { NextResponse } from "next/server";
import { query } from "@/lib/db";
export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const trades = query("SELECT * FROM trades ORDER BY timestamp ASC");
    const buysByToken: Record<string, Array<Record<string, unknown>>> = {};
    for (const t of trades) {
      if (t.side === "BUY") {
        const tid = t.token_id as string;
        if (!buysByToken[tid]) buysByToken[tid] = [];
        buysByToken[tid].push(t);
      }
    }

    const closed: Array<{ pnl: number; exitTime: string }> = [];
    for (const t of trades) {
      if (t.side === "SELL") {
        const tid = t.token_id as string;
        if (buysByToken[tid]?.length) {
          const buy = buysByToken[tid].shift()!;
          const size = Math.min(buy.size as number, t.size as number);
          closed.push({ pnl: ((t.price as number) - (buy.price as number)) * size, exitTime: t.timestamp as string });
        }
      }
    }

    if (!closed.length) {
      return NextResponse.json({ equity_curve: [], daily_pnl: [], win_rate: 0, profit_factor: 0, max_drawdown: 0, avg_win: 0, avg_loss: 0, reward_risk_ratio: 0, total_closed: 0, total_pnl: 0, winning: 0, losing: 0 });
    }

    const wins = closed.filter(c => c.pnl >= 0);
    const lossList = closed.filter(c => c.pnl < 0);
    const totalWin = wins.reduce((s, c) => s + c.pnl, 0);
    const totalLoss = Math.abs(lossList.reduce((s, c) => s + c.pnl, 0));
    const avgWin = wins.length ? totalWin / wins.length : 0;
    const avgLoss = lossList.length ? totalLoss / lossList.length : 0;

    let cum = 0;
    const equity = closed.map(c => { cum += c.pnl; return { time: c.exitTime, equity: +cum.toFixed(4) }; });

    const daily: Record<string, number> = {};
    for (const c of closed) { const d = c.exitTime.slice(0, 10); daily[d] = (daily[d] || 0) + c.pnl; }
    const dailyPnl = Object.entries(daily).sort().map(([date, pnl]) => ({ date, pnl: +pnl.toFixed(4) }));

    let peak = 0, maxDd = 0, running = 0;
    for (const c of closed) { running += c.pnl; peak = Math.max(peak, running); maxDd = Math.max(maxDd, peak - running); }

    return NextResponse.json({
      equity_curve: equity, daily_pnl: dailyPnl,
      win_rate: +(wins.length / closed.length * 100).toFixed(2),
      profit_factor: totalLoss > 0 ? +(totalWin / totalLoss).toFixed(4) : 0,
      max_drawdown: +maxDd.toFixed(4), avg_win: +avgWin.toFixed(4), avg_loss: +avgLoss.toFixed(4),
      reward_risk_ratio: avgLoss > 0 ? +(avgWin / avgLoss).toFixed(4) : 0,
      total_closed: closed.length, total_pnl: +cum.toFixed(4), winning: wins.length, losing: lossList.length,
    });
  } catch (err) {
    console.error("Performance error:", err);
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}
