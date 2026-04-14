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

// ---------------------------------------------------------------------------
// Semantic mispricing engine observability
// ---------------------------------------------------------------------------
//
// Mirrors the /api/semantic/{signals,summary,calibration} FastAPI endpoints
// as direct SQLite reads so the Next.js server components can render the
// tile without a second HTTP hop.  The algorithms here MUST stay in sync
// with ``src/analysis/semantic_engine/calibration.py`` — the canonical
// implementation.  If a test catches drift, update both sides in lockstep.

/** Checks whether the semantic_signals table exists — it's created only after
 * the first bot run with SEMANTIC_ENGINE_ENABLED=true, so on a fresh DB
 * this returns false and the UI can render an empty-state tile. */
export function hasSemanticTable(): boolean {
  try {
    const row = queryOne(
      "SELECT name FROM sqlite_master WHERE type='table' AND name='semantic_signals'"
    );
    return row !== undefined && row !== null;
  } catch {
    return false;
  }
}

export type SemanticSummaryMethod = {
  method: string;
  count: number;
  avg_net_edge: number;
  avg_score: number;
  avg_confidence: number;
};

export type SemanticSummary = {
  table_exists: boolean;
  total: number;
  window_days: number;
  avg_net_edge: number;
  avg_score: number;
  by_side: { BUY: number; SELL: number };
  by_method: SemanticSummaryMethod[];
};

export function getSemanticSummary(days = 7): SemanticSummary {
  const empty: SemanticSummary = {
    table_exists: false, total: 0, window_days: days,
    avg_net_edge: 0, avg_score: 0,
    by_side: { BUY: 0, SELL: 0 }, by_method: [],
  };
  if (!hasSemanticTable()) return empty;

  const cutoff = `datetime('now', '-${Math.max(1, Math.floor(days))} days')`;
  const totals = queryOne(
    `SELECT COUNT(*) as n, AVG(net_edge) as avg_e, AVG(score) as avg_s ` +
    `FROM semantic_signals WHERE timestamp >= ${cutoff}`
  );
  const sideRows = query(
    `SELECT side, COUNT(*) as n FROM semantic_signals ` +
    `WHERE timestamp >= ${cutoff} GROUP BY side`
  );
  const methodRows = query(
    `SELECT synthetic_method as method, COUNT(*) as n, ` +
    `AVG(net_edge) as avg_e, AVG(score) as avg_s, ` +
    `AVG(synthetic_confidence) as avg_c ` +
    `FROM semantic_signals WHERE timestamp >= ${cutoff} ` +
    `GROUP BY synthetic_method ORDER BY n DESC`
  );

  const by_side = { BUY: 0, SELL: 0 };
  for (const r of sideRows) {
    const s = r.side as string;
    if (s === "BUY" || s === "SELL") by_side[s] = r.n as number;
  }

  return {
    table_exists: true,
    total: (totals?.n as number) || 0,
    window_days: days,
    avg_net_edge: +((totals?.avg_e as number) || 0).toFixed(6),
    avg_score: +((totals?.avg_s as number) || 0).toFixed(4),
    by_side,
    by_method: methodRows.map((r) => ({
      method: (r.method as string) || "unknown",
      count: r.n as number,
      avg_net_edge: +(((r.avg_e as number) || 0)).toFixed(6),
      avg_score: +(((r.avg_s as number) || 0)).toFixed(4),
      avg_confidence: +(((r.avg_c as number) || 0)).toFixed(4),
    })),
  };
}

export type SemanticSignal = {
  id: number;
  timestamp: string;
  token_id: string;
  question: string;
  category: string;
  side: "BUY" | "SELL";
  net_edge: number;
  score: number;
  synthetic_method: string;
  synthetic_fair: number;
  synthetic_confidence: number;
  midpoint: number;
  spread: number;
  mode: string;
};

export function getSemanticSignals(limit = 25, mode?: "shadow" | "live"): {
  table_exists: boolean;
  signals: SemanticSignal[];
} {
  if (!hasSemanticTable()) return { table_exists: false, signals: [] };
  const where = mode ? "WHERE mode = ?" : "";
  const params: unknown[] = mode ? [mode, limit] : [limit];
  const rows = query(
    `SELECT id, timestamp, token_id, question, category, side, ` +
    `net_edge, score, synthetic_method, synthetic_fair, synthetic_confidence, ` +
    `midpoint, spread, mode FROM semantic_signals ${where} ` +
    `ORDER BY id DESC LIMIT ?`,
    params
  );
  const signals = rows.map((r) => ({
    id: r.id as number,
    timestamp: r.timestamp as string,
    token_id: r.token_id as string,
    question: (r.question as string) || "",
    category: (r.category as string) || "",
    side: r.side as "BUY" | "SELL",
    net_edge: r.net_edge as number,
    score: r.score as number,
    synthetic_method: (r.synthetic_method as string) || "unknown",
    synthetic_fair: r.synthetic_fair as number,
    synthetic_confidence: r.synthetic_confidence as number,
    midpoint: r.midpoint as number,
    spread: r.spread as number,
    mode: (r.mode as string) || "shadow",
  }));
  return { table_exists: true, signals };
}

export type MethodCalibration = {
  method: string;
  n_signals: number;
  n_matched: number;
  avg_detected_edge: number;
  avg_realised_edge: number;
  realisation_ratio: number | null;
  win_rate: number;
};

export type SemanticCalibration = {
  table_exists: boolean;
  n_signals: number;
  n_matched: number;
  window_days: number;
  per_method: MethodCalibration[];
  warnings: string[];
};

/** Match each semantic BUY signal to the next BUY→SELL round-trip on
 * the same token within ``matchWindowHours``.  Mirrors the FIFO logic in
 * src/analysis/semantic_engine/calibration.py:_pair_buys_with_exits. */
export function getSemanticCalibration(
  days = 30,
  matchWindowHours = 24.0,
): SemanticCalibration {
  const empty: SemanticCalibration = {
    table_exists: false, n_signals: 0, n_matched: 0,
    window_days: days, per_method: [], warnings: [],
  };
  if (!hasSemanticTable()) return { ...empty, warnings: ["semantic_signals_table_missing"] };

  const cutoff = `datetime('now', '-${Math.max(1, Math.floor(days))} days')`;
  const signals = query(
    `SELECT timestamp, token_id, synthetic_method, net_edge, side ` +
    `FROM semantic_signals WHERE timestamp >= ${cutoff} ORDER BY timestamp ASC`
  );
  const trades = query(
    `SELECT timestamp, token_id, side, price, size ` +
    `FROM trades WHERE strategy = 'semantic_mispricing' ` +
    `AND timestamp >= ${cutoff} ORDER BY timestamp ASC`
  );
  if (signals.length === 0) {
    return { ...empty, table_exists: true };
  }

  // Build a per-token FIFO of BUY→next-SELL round-trips so we can match
  // each BUY signal to the one it caused (if any).  A BUY signal with no
  // subsequent BUY trade within the window is "unmatched" (reported but
  // not aggregated into the ratio).
  const tradesByToken: Record<string, Array<Record<string, unknown>>> = {};
  for (const t of trades) {
    const k = t.token_id as string;
    (tradesByToken[k] = tradesByToken[k] || []).push(t);
  }

  // Simple pairing: for each BUY trade, find the first SELL on the same
  // token that happened AFTER it.  O(n) with two pointers.
  const pairs: Record<string, Array<{ buyTs: string; buyPx: number; sellPx: number }>> = {};
  for (const k of Object.keys(tradesByToken)) {
    const list = tradesByToken[k];
    const buys = list.filter((t) => t.side === "BUY");
    const sells = list.filter((t) => t.side === "SELL");
    const out: Array<{ buyTs: string; buyPx: number; sellPx: number }> = [];
    let si = 0;
    for (const b of buys) {
      while (si < sells.length && (sells[si].timestamp as string) <= (b.timestamp as string)) {
        si++;
      }
      if (si >= sells.length) break;
      out.push({
        buyTs: b.timestamp as string,
        buyPx: b.price as number,
        sellPx: sells[si].price as number,
      });
      si++;
    }
    pairs[k] = out;
  }

  // For each signal, find a pair whose buyTs is within windowMs after the
  // signal timestamp.  Once matched, the pair is consumed (to avoid two
  // signals claiming the same trade).
  const windowMs = matchWindowHours * 3600 * 1000;
  type Bucket = {
    n_signals: number;
    n_matched: number;
    sum_detected: number;
    sum_realised: number;
    wins: number;
  };
  const buckets: Record<string, Bucket> = {};
  let matchedTotal = 0;
  for (const sig of signals) {
    const method = (sig.synthetic_method as string) || "unknown";
    const b = (buckets[method] = buckets[method] || {
      n_signals: 0, n_matched: 0, sum_detected: 0, sum_realised: 0, wins: 0,
    });
    b.n_signals += 1;
    if (sig.side !== "BUY") continue; // calibration only covers BUY side for now

    const sigTs = Date.parse(sig.timestamp as string);
    const tok = sig.token_id as string;
    const tokenPairs = pairs[tok] || [];
    let matchIdx = -1;
    for (let i = 0; i < tokenPairs.length; i++) {
      const buyTs = Date.parse(tokenPairs[i].buyTs);
      if (Number.isNaN(buyTs)) continue;
      const delta = buyTs - sigTs;
      if (delta < 0) continue;
      if (delta > windowMs) break;
      matchIdx = i;
      break;
    }
    if (matchIdx < 0) continue;
    const p = tokenPairs.splice(matchIdx, 1)[0];
    const detected = sig.net_edge as number;
    const realised = (p.sellPx - p.buyPx) / Math.max(p.buyPx, 1e-9);
    b.n_matched += 1;
    b.sum_detected += detected;
    b.sum_realised += realised;
    if (realised > 0) b.wins += 1;
    matchedTotal += 1;
  }

  const per_method: MethodCalibration[] = Object.entries(buckets).map(([method, b]) => {
    const avgDet = b.n_matched ? b.sum_detected / b.n_matched : 0;
    const avgReal = b.n_matched ? b.sum_realised / b.n_matched : 0;
    const ratio = Math.abs(avgDet) > 1e-6 ? +(avgReal / avgDet).toFixed(4) : null;
    return {
      method,
      n_signals: b.n_signals,
      n_matched: b.n_matched,
      avg_detected_edge: +avgDet.toFixed(6),
      avg_realised_edge: +avgReal.toFixed(6),
      realisation_ratio: ratio,
      win_rate: b.n_matched ? +(b.wins / b.n_matched).toFixed(4) : 0,
    };
  }).sort((a, b) => b.n_signals - a.n_signals);

  const warnings: string[] = [];
  if (signals.length > 0 && matchedTotal === 0) {
    warnings.push("no_signals_matched_trades");
  }
  if (signals.length > 0 && signals.length < 20) {
    warnings.push("small_sample_size");
  }

  return {
    table_exists: true,
    n_signals: signals.length,
    n_matched: matchedTotal,
    window_days: days,
    per_method,
    warnings,
  };
}
