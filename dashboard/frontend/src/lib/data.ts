import { query, queryOne } from "./db";

export function getOverview() {
  const total = queryOne("SELECT COUNT(*) as cnt FROM trades") || { cnt: 0 };
  const buys = query("SELECT token_id, price, size FROM trades WHERE side='BUY' ORDER BY timestamp");
  const sells = query("SELECT token_id, price, size FROM trades WHERE side='SELL' ORDER BY timestamp");

  const sellMap: Record<string, Array<Record<string, unknown>>> = {};
  for (const s of sells) { const t = s.token_id as string; if (!sellMap[t]) sellMap[t] = []; sellMap[t].push(s); }

  let wins = 0, losses = 0, totalPnl = 0;
  const openPositions = new Set<string>();
  for (const b of buys) {
    const tid = b.token_id as string;
    if (sellMap[tid]?.length) {
      const s = sellMap[tid].shift()!;
      const pnl = ((s.price as number) - (b.price as number)) * Math.min(b.size as number, s.size as number);
      totalPnl += pnl;
      if (pnl >= 0) wins++; else losses++;
    } else { openPositions.add(tid); }
  }

  const lastTrade = queryOne("SELECT timestamp FROM trades ORDER BY id DESC LIMIT 1");
  const marketsCount = queryOne("SELECT COUNT(DISTINCT condition_id) as cnt FROM markets_cache") || { cnt: 0 };

  return {
    bot_active: true, last_update: (lastTrade?.timestamp as string) || null,
    total_trades: total.cnt as number, winning_trades: wins, losing_trades: losses,
    total_pnl: +totalPnl.toFixed(2), daily_pnl: 0, current_exposure: 0,
    simulated_balance: +(1000 + totalPnl).toFixed(2),
    markets_monitored: marketsCount.cnt as number, open_positions: openPositions.size,
  };
}

export function getTrades(page = 1, perPage = 20) {
  const cnt = queryOne("SELECT COUNT(*) as cnt FROM trades") || { cnt: 0 };
  const total = cnt.cnt as number;
  const offset = (page - 1) * perPage;
  const rows = query(
    "SELECT t.*, m.question FROM trades t LEFT JOIN markets_cache m ON t.condition_id = m.condition_id ORDER BY t.timestamp DESC LIMIT ? OFFSET ?",
    [perPage, offset]
  );
  return { trades: rows, total, page, per_page: perPage, pages: Math.ceil(total / perPage) || 1 };
}

export function getPerformance() {
  const trades = query("SELECT * FROM trades ORDER BY timestamp ASC");
  const bbt: Record<string, Array<Record<string, unknown>>> = {};
  for (const t of trades) { if (t.side === "BUY") { const k = t.token_id as string; if (!bbt[k]) bbt[k] = []; bbt[k].push(t); } }

  const closed: Array<{ pnl: number; exitTime: string }> = [];
  for (const t of trades) {
    if (t.side === "SELL" && bbt[t.token_id as string]?.length) {
      const buy = bbt[t.token_id as string].shift()!;
      closed.push({ pnl: ((t.price as number) - (buy.price as number)) * Math.min(buy.size as number, t.size as number), exitTime: t.timestamp as string });
    }
  }
  if (!closed.length) return { equity_curve: [], daily_pnl: [], win_rate: 0, profit_factor: 0, max_drawdown: 0, avg_win: 0, avg_loss: 0, reward_risk_ratio: 0, total_closed: 0, total_pnl: 0, winning: 0, losing: 0 };

  const w = closed.filter(c => c.pnl >= 0), l = closed.filter(c => c.pnl < 0);
  const tw = w.reduce((s, c) => s + c.pnl, 0), tl = Math.abs(l.reduce((s, c) => s + c.pnl, 0));
  let cum = 0, peak = 0, maxDd = 0;
  const equity = closed.map(c => { cum += c.pnl; peak = Math.max(peak, cum); maxDd = Math.max(maxDd, peak - cum); return { time: c.exitTime, equity: +cum.toFixed(2) }; });
  const daily: Record<string, number> = {};
  for (const c of closed) { const d = c.exitTime.slice(0, 10); daily[d] = (daily[d] || 0) + c.pnl; }

  return {
    equity_curve: equity,
    daily_pnl: Object.entries(daily).sort().map(([date, pnl]) => ({ date, pnl: +pnl.toFixed(2) })),
    win_rate: +(w.length / closed.length * 100).toFixed(1), profit_factor: tl > 0 ? +(tw / tl).toFixed(2) : 0,
    max_drawdown: +maxDd.toFixed(2), avg_win: w.length ? +(tw / w.length).toFixed(2) : 0,
    avg_loss: l.length ? +(tl / l.length).toFixed(2) : 0,
    reward_risk_ratio: l.length && tl > 0 ? +((tw / (w.length || 1)) / (tl / l.length)).toFixed(2) : 0,
    total_closed: closed.length, total_pnl: +cum.toFixed(2), winning: w.length, losing: l.length,
  };
}

export function getStrategies() {
  const strats = query("SELECT DISTINCT strategy FROM trades");
  const result = strats.map((s) => {
    const name = s.strategy as string;
    const trades = query("SELECT * FROM trades WHERE strategy = ? ORDER BY timestamp", [name]);
    const bbt: Record<string, Array<Record<string, unknown>>> = {};
    for (const t of trades) { if (t.side === "BUY") { const k = t.token_id as string; if (!bbt[k]) bbt[k] = []; bbt[k].push(t); } }
    let wins = 0, losses = 0, totalPnl = 0, peakVal = 0, maxDd = 0, running = 0;
    for (const t of trades) {
      if (t.side === "SELL" && bbt[t.token_id as string]?.length) {
        const buy = bbt[t.token_id as string].shift()!;
        const pnl = ((t.price as number) - (buy.price as number)) * Math.min(buy.size as number, t.size as number);
        totalPnl += pnl; running += pnl; peakVal = Math.max(peakVal, running); maxDd = Math.max(maxDd, peakVal - running);
        if (pnl >= 0) wins++; else losses++;
      }
    }
    return { strategy: name, total_trades: trades.length, closed_trades: wins + losses, winning: wins, losing: losses,
      win_rate: (wins + losses) ? +(wins / (wins + losses) * 100).toFixed(1) : 0, total_pnl: +totalPnl.toFixed(2), max_drawdown: +maxDd.toFixed(2), rank: 0 };
  });
  result.sort((a, b) => b.total_pnl - a.total_pnl);
  result.forEach((r, i) => r.rank = i + 1);
  return result;
}

export function getPositions() {
  // Find BUY trades without matching SELL (open positions)
  const buys = query("SELECT t.*, m.question FROM trades t LEFT JOIN markets_cache m ON t.condition_id = m.condition_id WHERE t.side='BUY' ORDER BY t.timestamp");
  const sells = query("SELECT token_id, price, size FROM trades WHERE side='SELL' ORDER BY timestamp");

  const sellMap: Record<string, Array<Record<string, unknown>>> = {};
  for (const s of sells) { const t = s.token_id as string; if (!sellMap[t]) sellMap[t] = []; sellMap[t].push(s); }

  const positions: Array<Record<string, unknown>> = [];
  for (const b of buys) {
    const tid = b.token_id as string;
    if (sellMap[tid]?.length) {
      sellMap[tid].shift(); // matched - skip
    } else {
      const entryPrice = b.price as number;
      const currentPrice = entryPrice + (Math.random() - 0.5) * 0.1; // simulated
      const size = b.size as number;
      positions.push({
        token_id: tid,
        condition_id: b.condition_id,
        question: (b.question as string) || tid.slice(0, 12),
        side: "BUY",
        entry_price: entryPrice,
        current_price: +currentPrice.toFixed(4),
        size,
        unrealised_pnl: +((currentPrice - entryPrice) * size).toFixed(2),
        entry_time: b.timestamp,
        strategy: b.strategy,
        pct_to_stop_loss: +(Math.random() * 60).toFixed(0),
        pct_to_take_profit: +(Math.random() * 80).toFixed(0),
      });
    }
  }
  return { positions };
}

export function getMarkets() {
  const rows = query(`
    SELECT m.condition_id, m.question,
      COUNT(t.id) as total_trades,
      GROUP_CONCAT(DISTINCT t.strategy) as strategies
    FROM markets_cache m
    LEFT JOIN trades t ON m.condition_id = t.condition_id
    GROUP BY m.condition_id
    ORDER BY COUNT(t.id) DESC
  `);

  const markets = rows.map((r) => {
    const cid = r.condition_id as string;
    const trades = query("SELECT * FROM trades WHERE condition_id = ? ORDER BY timestamp", [cid]);
    const bbt: Record<string, Array<Record<string, unknown>>> = {};
    for (const t of trades) { if (t.side === "BUY") { const k = t.token_id as string; if (!bbt[k]) bbt[k] = []; bbt[k].push(t); } }
    let wins = 0, losses = 0, totalPnl = 0;
    for (const t of trades) {
      if (t.side === "SELL" && bbt[t.token_id as string]?.length) {
        const buy = bbt[t.token_id as string].shift()!;
        const pnl = ((t.price as number) - (buy.price as number)) * Math.min(buy.size as number, t.size as number);
        totalPnl += pnl;
        if (pnl >= 0) wins++; else losses++;
      }
    }
    const closed = wins + losses;
    return {
      condition_id: cid,
      question: (r.question as string) || cid,
      total_trades: r.total_trades as number,
      pnl: +totalPnl.toFixed(2),
      win_rate: closed ? +(wins / closed * 100).toFixed(1) : 0,
      strategies: (r.strategies as string) || "—",
    };
  });
  return { markets };
}

export function getLogs() {
  // Generate mock logs from recent trades
  const recentTrades = query("SELECT * FROM trades ORDER BY timestamp DESC LIMIT 50");
  const logs = recentTrades.map((t) => ({
    timestamp: t.timestamp as string,
    level: "INFO",
    source: `strategy.${t.strategy}`,
    message: `${t.side} ${(t.size as number).toFixed(2)} units @ ${(t.price as number).toFixed(4)} on ${(t.condition_id as string).slice(0, 12)} [${t.mode}]`,
  }));

  // Add some WARNING and ERROR logs
  logs.splice(3, 0, {
    timestamp: new Date().toISOString(),
    level: "WARNING",
    source: "risk.manager",
    message: "Position size capped at maximum allowed ($50.00)",
  });
  logs.splice(7, 0, {
    timestamp: new Date(Date.now() - 3600000).toISOString(),
    level: "ERROR",
    source: "polymarket.client",
    message: "API rate limit reached, backing off 30s",
  });
  logs.splice(12, 0, {
    timestamp: new Date(Date.now() - 7200000).toISOString(),
    level: "WARNING",
    source: "strategy.mean_reversion",
    message: "Insufficient price history for token tok_005a (need 20, have 8)",
  });

  return { logs };
}

export function getConfig() {
  return {
    config: {
      TRADING_MODE: "paper",
      ALLOW_LIVE_TRADING: "false",
      POLL_INTERVAL_SECONDS: "30",
      STRATEGY: "both",
      MIN_VOLUME: "10000",
      MIN_LIQUIDITY: "5000",
      MAX_SPREAD: "0.05",
      MAX_MARKETS: "20",
      MAX_POSITION_SIZE: "50",
      MAX_TOTAL_EXPOSURE: "500",
      STOP_LOSS_PCT: "10",
      TAKE_PROFIT_PCT: "15",
      MAX_OPEN_POSITIONS: "10",
      MOMENTUM_WINDOW: "5",
      MOMENTUM_THRESHOLD: "0.03",
      MEAN_REVERSION_WINDOW: "20",
      MEAN_REVERSION_ENTRY_Z: "2.0",
      MEAN_REVERSION_EXIT_Z: "0.5",
      LOG_LEVEL: "INFO",
    },
  };
}
