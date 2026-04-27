const path = require("path");
const fs = require("fs");

// Try to load better-sqlite3 from dashboard/frontend or globally
let Database;
try {
  Database = require(path.join(__dirname, "dashboard", "frontend", "node_modules", "better-sqlite3"));
} catch {
  Database = require("better-sqlite3");
}

const DB_PATH = process.env.SQLITE_DB_PATH || path.join(__dirname, "polymarket_bot.db");
if (!fs.existsSync(DB_PATH)) {
  console.error("Database not found at:", DB_PATH);
  console.error("Run the bot first: python -m src.main run-bot");
  process.exit(1);
}
const db = new Database(DB_PATH, { readonly: true });
function query(sql, params = []) { return db.prepare(sql).all(...params); }
function queryOne(sql, params = []) { return db.prepare(sql).get(...params); }

// Helper: check if table exists
function tableExists(name) {
  const r = queryOne("SELECT name FROM sqlite_master WHERE type='table' AND name=?", [name]);
  return !!r;
}

// === BOT STATE (from JSON export) ===
const BOT_STATE_PATH = path.join(__dirname, "bot_state.json");
let botState = null;
try {
  if (fs.existsSync(BOT_STATE_PATH)) {
    botState = JSON.parse(fs.readFileSync(BOT_STATE_PATH, "utf-8"));
  }
} catch { botState = null; }

// === DECISION LOG STATS ===
let decisionStats = { entries: 0, exits_sl: 0, exits_tp: 0, risk_rejected: 0 };
if (tableExists("decision_log")) {
  const ds = queryOne("SELECT COUNT(*) as cnt FROM decision_log");
  decisionStats.entries = ds ? ds.cnt : 0;
  const sl = queryOne("SELECT COUNT(*) as cnt FROM decision_log WHERE action LIKE '%STOP_LOSS%'");
  decisionStats.exits_sl = sl ? sl.cnt : 0;
  const tp = queryOne("SELECT COUNT(*) as cnt FROM decision_log WHERE action LIKE '%TAKE_PROFIT%'");
  decisionStats.exits_tp = tp ? tp.cnt : 0;
  const rr = queryOne("SELECT COUNT(*) as cnt FROM decision_log WHERE action='RISK_REJECTED'");
  decisionStats.risk_rejected = rr ? rr.cnt : 0;
}

// === TICK STATS ===
let recentTicks = [];
if (tableExists("tick_stats")) {
  recentTicks = query("SELECT * FROM tick_stats ORDER BY id DESC LIMIT 50").reverse();
}

// === SPREAD-ADJUSTED PNL ===
let spreadCost = 0;
if (tableExists("trades")) {
  try {
    const sc = queryOne("SELECT SUM(spread_at_entry * size / 2.0) as cost FROM trades WHERE spread_at_entry > 0");
    spreadCost = sc && sc.cost ? sc.cost : 0;
  } catch { spreadCost = 0; }
}

// === OVERVIEW ===
const total = queryOne("SELECT COUNT(*) as cnt FROM trades");
const buys = query("SELECT token_id, price, size FROM trades WHERE side='BUY' ORDER BY timestamp");
const sells = query("SELECT token_id, price, size FROM trades WHERE side='SELL' ORDER BY timestamp");
const sellMap = {};
for (const s of sells) { if (!sellMap[s.token_id]) sellMap[s.token_id] = []; sellMap[s.token_id].push(s); }
let wins = 0, losses = 0, totalPnl = 0;
const openPos = new Set();
for (const b of buys) {
  if (sellMap[b.token_id] && sellMap[b.token_id].length) {
    const s = sellMap[b.token_id].shift();
    const pnl = (s.price - b.price) * Math.min(b.size, s.size);
    totalPnl += pnl;
    if (pnl >= 0) wins++; else losses++;
  } else { openPos.add(b.token_id); }
}
const mktsCount = queryOne("SELECT COUNT(DISTINCT condition_id) as cnt FROM markets_cache");

// Prefer the backend's authoritative numbers from bot_state.json (which
// includes fees, unrealised PnL, and an honest closed-trades win rate).
// Fall back to the JS-derived reconstruction only for legacy DBs that
// haven't run a tick under the new exporter yet.
const stateP = botState && botState.portfolio;
const stateW = botState && botState.win_rate;
const o = {
  total_trades: total.cnt,
  winning: stateW ? stateW.wins : wins,
  losing: stateW ? stateW.losses : losses,
  total_pnl: stateP ? +stateP.net_pnl_after_fees.toFixed(2) : +totalPnl.toFixed(2),
  realised_pnl: stateP ? +stateP.realised_pnl.toFixed(2) : +totalPnl.toFixed(2),
  unrealised_pnl: stateP ? +stateP.unrealised_pnl.toFixed(2) : 0,
  fees_paid: stateP ? +stateP.fees_paid.toFixed(2) : 0,
  open_losers: stateP ? stateP.open_losers : 0,
  // No more synthetic 1000-USD starting balance.  Show net PnL only —
  // the dashboard never invents capital that the bot doesn't have.
  markets: mktsCount.cnt,
  open_pos: stateP ? stateP.open_positions : openPos.size,
  win_rate: stateW
    ? (stateW.total_closed > 0 ? (stateW.win_rate * 100).toFixed(1) : "0")
    : ((wins + losses) > 0 ? ((wins / (wins + losses)) * 100).toFixed(1) : "0"),
  win_rate_n: stateW ? stateW.total_closed : (wins + losses),
};

// Heartbeat freshness: a tick that's older than 2x the configured poll
// interval likely means the bot is dead/stuck — never paint the status
// dot green when it isn't.
let botFresh = false;
let staleSeconds = null;
if (botState && typeof botState.heartbeat_unix === "number") {
  const ageS = Math.floor(Date.now() / 1000) - botState.heartbeat_unix;
  staleSeconds = ageS;
  const poll = (botState.config && botState.config.poll_interval) || 60;
  botFresh = ageS <= 2 * poll;
}

// === PERFORMANCE ===
const allTrades = query("SELECT * FROM trades ORDER BY timestamp ASC");
const bbt = {};
for (const t of allTrades) { if (t.side === "BUY") { if (!bbt[t.token_id]) bbt[t.token_id] = []; bbt[t.token_id].push(t); } }
const closed = [];
for (const t of allTrades) {
  if (t.side === "SELL" && bbt[t.token_id] && bbt[t.token_id].length) {
    const buy = bbt[t.token_id].shift();
    closed.push({ pnl: (t.price - buy.price) * Math.min(buy.size, t.size), exitTime: t.timestamp });
  }
}
const cw = closed.filter(c => c.pnl >= 0), cl = closed.filter(c => c.pnl < 0);
const tw = cw.reduce((s, c) => s + c.pnl, 0), tl = Math.abs(cl.reduce((s, c) => s + c.pnl, 0));
let cum = 0, peak = 0, maxDd = 0;
const equity = closed.map(c => { cum += c.pnl; peak = Math.max(peak, cum); maxDd = Math.max(maxDd, peak - cum); return { time: c.exitTime, equity: +cum.toFixed(2) }; });
const daily = {};
for (const c of closed) { const d = c.exitTime.slice(0, 10); daily[d] = (daily[d] || 0) + c.pnl; }
const dailyPnl = Object.entries(daily).sort().map(([date, pnl]) => ({ date, pnl: +pnl.toFixed(2) }));
const p = {
  total_pnl: +cum.toFixed(2), win_rate: closed.length ? +(cw.length / closed.length * 100).toFixed(1) : 0,
  profit_factor: tl > 0 ? +(tw / tl).toFixed(2) : 0, max_drawdown: +maxDd.toFixed(2),
  avg_win: cw.length ? +(tw / cw.length).toFixed(2) : 0, avg_loss: cl.length ? +(tl / cl.length).toFixed(2) : 0,
  total_closed: closed.length, winning: cw.length, losing: cl.length,
};

// === RISK-ADJUSTED METRICS (Sharpe, Sortino, expectancy, Calmar) ===
// Read-only analytics — does not affect trading logic.
const riskAdj = (() => {
  const n = closed.length;
  if (n === 0) return null;
  const pnls = closed.map(c => c.pnl);
  const expectancy = pnls.reduce((a, b) => a + b, 0) / n;
  // Population stdev of trade PnL
  const mu = expectancy;
  const variance = n > 1 ? pnls.reduce((s, x) => s + (x - mu) ** 2, 0) / n : 0;
  const sd = Math.sqrt(variance);
  // Daily-bucketed Sharpe/Sortino (annualised, trading-day convention)
  const daily = Object.values(dailyPnl.reduce((acc, d) => { acc[d.date] = d.pnl; return acc; }, {}));
  let sharpe = 0, sortino = 0;
  if (daily.length >= 2) {
    const muD = daily.reduce((a, b) => a + b, 0) / daily.length;
    const varD = daily.reduce((s, x) => s + (x - muD) ** 2, 0) / daily.length;
    const sdD = Math.sqrt(varD);
    const downside = daily.map(v => Math.min(0, v));
    const dsdVar = downside.reduce((s, x) => s + x * x, 0) / daily.length;
    const dsdD = Math.sqrt(dsdVar);
    const sqrt252 = Math.sqrt(252);
    sharpe = sdD > 0 ? (muD / sdD) * sqrt252 : 0;
    sortino = dsdD > 0 ? (muD / dsdD) * sqrt252 : 0;
  }
  const calmar = maxDd > 0 ? cum / maxDd : 0;
  return {
    expectancy: +expectancy.toFixed(4),
    stdev_trade_pnl: +sd.toFixed(4),
    sharpe_daily: +sharpe.toFixed(2),
    sortino_daily: +sortino.toFixed(2),
    calmar: +calmar.toFixed(2),
    best_trade: +Math.max(...pnls).toFixed(4),
    worst_trade: +Math.min(...pnls).toFixed(4),
  };
})();

// === TRADES ===
const trades = query("SELECT t.*, m.question FROM trades t LEFT JOIN markets_cache m ON t.condition_id = m.condition_id ORDER BY t.timestamp DESC LIMIT 30");

// === STRATEGIES ===
const strats = query("SELECT DISTINCT strategy FROM trades");
const strategies = strats.map(s => {
  const name = s.strategy;
  const st = query("SELECT * FROM trades WHERE strategy = ? ORDER BY timestamp", [name]);
  const bb = {};
  for (const t of st) { if (t.side === "BUY") { if (!bb[t.token_id]) bb[t.token_id] = []; bb[t.token_id].push(t); } }
  let sw = 0, sl2 = 0, sp = 0, spk = 0, smd = 0, sr = 0;
  for (const t of st) {
    if (t.side === "SELL" && bb[t.token_id] && bb[t.token_id].length) {
      const buy = bb[t.token_id].shift();
      const pnl2 = (t.price - buy.price) * Math.min(buy.size, t.size);
      sp += pnl2; sr += pnl2; spk = Math.max(spk, sr); smd = Math.max(smd, spk - sr);
      if (pnl2 >= 0) sw++; else sl2++;
    }
  }
  return { strategy: name, total_trades: st.length, closed_trades: sw + sl2, winning: sw, losing: sl2,
    win_rate: (sw + sl2) ? +(sw / (sw + sl2) * 100).toFixed(1) : 0, total_pnl: +sp.toFixed(2), max_drawdown: +smd.toFixed(2), rank: 0 };
});
strategies.sort((a, b) => b.total_pnl - a.total_pnl);
strategies.forEach((r, i) => r.rank = i + 1);

// === MARKETS ===
const mkts = query("SELECT m.condition_id, m.question, COUNT(t.id) as total_trades, GROUP_CONCAT(DISTINCT t.strategy) as strategies FROM markets_cache m LEFT JOIN trades t ON m.condition_id = t.condition_id GROUP BY m.condition_id ORDER BY COUNT(t.id) DESC LIMIT 50");
const marketsList = mkts.map(r => {
  const mt = query("SELECT * FROM trades WHERE condition_id = ? ORDER BY timestamp", [r.condition_id]);
  const mb = {};
  for (const t of mt) { if (t.side === "BUY") { if (!mb[t.token_id]) mb[t.token_id] = []; mb[t.token_id].push(t); } }
  let mw = 0, ml2 = 0, mp = 0;
  for (const t of mt) {
    if (t.side === "SELL" && mb[t.token_id] && mb[t.token_id].length) {
      const buy = mb[t.token_id].shift();
      const pnl3 = (t.price - buy.price) * Math.min(buy.size, t.size);
      mp += pnl3; if (pnl3 >= 0) mw++; else ml2++;
    }
  }
  const mc = mw + ml2;
  return { question: r.question || r.condition_id, total_trades: r.total_trades, pnl: +mp.toFixed(2), win_rate: mc ? +(mw / mc * 100).toFixed(1) : 0, strategies: r.strategies || "-" };
});

// === RESOLUTION TRACKING ===
let resolutionStats = { total: 0, correct: 0, accuracy: 0, pnl: 0 };
let resolutionRecent = [];
if (tableExists("market_resolutions")) {
  const rs = queryOne("SELECT COUNT(*) as total, SUM(prediction_correct) as correct, SUM(our_pnl) as pnl FROM market_resolutions");
  if (rs && rs.total > 0) {
    resolutionStats = {
      total: rs.total,
      correct: rs.correct || 0,
      accuracy: rs.correct ? (rs.correct / rs.total * 100).toFixed(1) : "0",
      pnl: rs.pnl || 0,
    };
  }
  // Recent resolved markets (most recent 10)
  resolutionRecent = query(
    "SELECT question, outcome, our_side, our_entry_price, our_exit_price, our_pnl, prediction_correct, checked_at " +
    "FROM market_resolutions ORDER BY checked_at DESC LIMIT 10"
  );
}

// === EDGE CALIBRATION ===
// Join resolutions with entry decisions to compute accuracy by confidence bucket.
let edgeCalibration = null;
if (tableExists("market_resolutions") && tableExists("decision_log") && resolutionStats.total > 0) {
  try {
    const rows = query(
      "SELECT mr.prediction_correct as correct, mr.our_pnl as pnl, dl.confidence as confidence, dl.features as features " +
      "FROM market_resolutions mr " +
      "JOIN decision_log dl ON dl.token_id = mr.token_id " +
      "WHERE dl.action IN ('ENTRY_BUY','ENTRY_SELL') " +
      "GROUP BY mr.token_id"
    );
    if (rows.length > 0) {
      const nBins = 5;
      const bins = Array.from({ length: nBins }, () => ({ count: 0, correct: 0, pnl: 0 }));
      for (const r of rows) {
        const c = r.confidence || 0;
        const idx = Math.min(Math.floor(c * nBins), nBins - 1);
        bins[idx].count += 1;
        if (r.correct) bins[idx].correct += 1;
        bins[idx].pnl += (r.pnl || 0);
      }
      edgeCalibration = bins.map((b, i) => ({
        range: `${(i/nBins).toFixed(1)}-${((i+1)/nBins).toFixed(1)}`,
        count: b.count,
        correct: b.correct,
        accuracy: b.count > 0 ? (b.correct / b.count * 100).toFixed(0) : "0",
        avg_pnl: b.count > 0 ? (b.pnl / b.count).toFixed(2) : "0.00",
      })).filter(b => b.count > 0);
    }
  } catch (e) {
    // Non-fatal — dashboard still renders without calibration section
  }
}

// === RISK REJECTION BREAKDOWN (by risk_detail) ===
let rejectionBreakdown = [];
if (tableExists("decision_log")) {
  try {
    const rows = query(
      "SELECT COALESCE(NULLIF(risk_detail,''), 'other') as reason, COUNT(*) as cnt " +
      "FROM decision_log WHERE action='RISK_REJECTED' GROUP BY reason ORDER BY cnt DESC LIMIT 12"
    );
    rejectionBreakdown = rows.map(r => ({ reason: r.reason, count: r.cnt }));
  } catch (_) { /* non-fatal */ }
}

// === BOOK QUALITY METRICS (from recent ENTRY decisions) ===
let bookQualityStats = null;
if (tableExists("decision_log")) {
  try {
    const rows = query(
      "SELECT features FROM decision_log WHERE action IN ('ENTRY_BUY','ENTRY_SELL') " +
      "AND features IS NOT NULL AND features != '{}' ORDER BY id DESC LIMIT 50"
    );
    const slippages = [];
    const imbalances = [];
    for (const r of rows) {
      try {
        const f = JSON.parse(r.features);
        if (typeof f.slippage_pct === "number") slippages.push(f.slippage_pct);
        if (typeof f.book_imbalance_5pct === "number") imbalances.push(f.book_imbalance_5pct);
      } catch (_) { /* skip bad JSON */ }
    }
    if (slippages.length > 0 || imbalances.length > 0) {
      const avg = (arr) => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;
      bookQualityStats = {
        n: Math.max(slippages.length, imbalances.length),
        avg_slippage_bps: (avg(slippages) * 10000).toFixed(1),
        max_slippage_bps: slippages.length ? (Math.max(...slippages) * 10000).toFixed(1) : "0.0",
        avg_imbalance: avg(imbalances).toFixed(3),
      };
    }
  } catch (e) {
    // Non-fatal
  }
}

// === CALIBRATION ===
let calibrationBuckets = [];
if (tableExists("calibration")) {
  const calRows = query("SELECT confidence, return_pct, pnl FROM calibration WHERE exit_timestamp IS NOT NULL AND exit_timestamp != ''");
  if (calRows.length > 0) {
    const nBins = 5;
    const bins = Array.from({ length: nBins }, () => []);
    for (const r of calRows) {
      const conf = r.confidence || 0;
      const idx = Math.min(Math.floor(conf * nBins), nBins - 1);
      bins[idx].push(r);
    }
    for (let i = 0; i < nBins; i++) {
      const lo = (i / nBins).toFixed(2);
      const hi = ((i + 1) / nBins).toFixed(2);
      const b = bins[i];
      const n = b.length;
      const wins = b.filter(r => (r.return_pct || 0) > 0).length;
      const avgRet = n > 0 ? b.reduce((s, r) => s + (r.return_pct || 0), 0) / n : 0;
      const avgConf = n > 0 ? b.reduce((s, r) => s + (r.confidence || 0), 0) / n : 0;
      const totPnl = b.reduce((s, r) => s + (r.pnl || 0), 0);
      calibrationBuckets.push({
        bucket: `[${lo}, ${hi})`, n, wins, winRate: n > 0 ? wins / n : 0,
        avgRet, avgConf, totPnl,
      });
    }
  }
}

// === BUILD HTML ===
const pnlColor = o.total_pnl >= 0 ? "#22c55e" : "#ef4444";
const pnlSign = o.total_pnl >= 0 ? "+" : "";
const maxAbsPnl = Math.max(...dailyPnl.map(x => Math.abs(x.pnl)), 1);

// Daily PnL bars
let barsHtml = "";
for (const dd of dailyPnl) {
  const h = Math.max(4, Math.abs(dd.pnl) / maxAbsPnl * 100);
  const color = dd.pnl >= 0 ? "#22c55e" : "#ef4444";
  barsHtml += `<div class="bar" style="height:${h}px;background:${color}" title="${dd.date}: $${dd.pnl.toFixed(2)}"></div>`;
}

// Equity curve SVG.  Prefer the backend-computed series exported in
// ``bot_state.json`` (which always reflects the same PnL math the
// dashboard's other cards use); fall back to the JS reconstruction
// for legacy DBs or pre-export bot runs.  When shadows are enabled,
// overlay one polyline per shadow series.
const SHADOW_PALETTE = ["#60a5fa", "#a78bfa", "#f472b6", "#facc15", "#22d3ee", "#fb923c"];

let equitySvg = "";
let equitySeries = [];
const ec = botState && botState.equity_curves;
if (ec && Array.isArray(ec.live) && ec.live.length > 0) {
  equitySeries.push({
    label: `Live (${botState.strategy || "live"})`,
    color: ec.live[ec.live.length - 1].pnl >= 0 ? "#22c55e" : "#ef4444",
    points: ec.live.map(pt => pt.pnl),
    width: 2.5,
  });
  if (Array.isArray(ec.shadows)) {
    ec.shadows.forEach((sh, idx) => {
      if (!Array.isArray(sh.points) || sh.points.length === 0) return;
      equitySeries.push({
        label: `Shadow: ${sh.strategy}`,
        color: SHADOW_PALETTE[idx % SHADOW_PALETTE.length],
        points: sh.points.map(pt => pt.pnl),
        width: 1.5,
      });
    });
  }
} else if (equity.length > 0) {
  // Fallback: use the JS-reconstructed series if the backend hasn't
  // exported one yet.
  equitySeries.push({
    label: "Live (reconstructed)",
    color: equity[equity.length - 1].equity >= 0 ? "#22c55e" : "#ef4444",
    points: equity.map(pp => pp.equity),
    width: 2.5,
  });
}

if (equitySeries.length > 0) {
  const allVals = equitySeries.flatMap(s => s.points);
  const minE = Math.min(0, ...allVals);
  const maxE = Math.max(0, ...allVals);
  const range = (maxE - minE) || 1;
  // Width per series = max length × 10 (so the chart accommodates
  // the longest history); shorter series are scaled to the same x
  // axis so legs line up by *trade index*, not wall-clock time.
  const maxLen = Math.max(...equitySeries.map(s => s.points.length));
  const xStep = 10;
  const polylines = equitySeries.map(s => {
    const stride = s.points.length > 1 ? (maxLen - 1) / (s.points.length - 1) : 0;
    const pts = s.points.map((v, i) => {
      const x = (s.points.length === 1 ? maxLen - 1 : stride * i) * xStep;
      const y = 110 - ((v - minE) / range) * 100;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="${s.width}" stroke-linejoin="round"/>`;
  }).join("");
  // Zero-line for visual reference.
  const yZero = 110 - ((0 - minE) / range) * 100;
  const zeroLine = (minE < 0 && maxE > 0)
    ? `<line x1="0" y1="${yZero.toFixed(1)}" x2="${(maxLen - 1) * xStep}" y2="${yZero.toFixed(1)}" stroke="#374151" stroke-width="0.5" stroke-dasharray="3 3"/>`
    : "";
  const legend = equitySeries.map(s => `
    <span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px">
      <span style="width:14px;height:3px;background:${s.color};display:inline-block;border-radius:2px"></span>
      <span style="font-size:11px;color:#d1d5db">${s.label} <span style="color:#6b7280">(n=${s.points.length})</span></span>
    </span>`).join("");
  equitySvg = `<div class="card" style="margin-top:12px">
    <h3>Equity Curve${equitySeries.length > 1 ? " — Live vs Shadows" : ""}</h3>
    <svg viewBox="0 0 ${(maxLen - 1) * xStep} 120" style="width:100%;height:160px" preserveAspectRatio="none">
      ${zeroLine}
      ${polylines}
    </svg>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#6b7280;margin-top:4px">
      <span>$${minE.toFixed(2)}</span><span>$${maxE.toFixed(2)}</span>
    </div>
    <div style="margin-top:8px">${legend}</div>
  </div>`;
}

// Trades rows
let tradesHtml = "";
for (const tr of trades) {
  const badgeClass = tr.side === "BUY" ? "badge-buy" : "badge-sell";
  const market = tr.question || tr.condition_id.slice(0, 12);
  tradesHtml += `<tr>
    <td style="color:#6b7280;white-space:nowrap">${new Date(tr.timestamp).toLocaleString()}</td>
    <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${market}</td>
    <td><span class="badge ${badgeClass}">${tr.side}</span></td>
    <td style="color:#d1d5db">${tr.strategy}</td>
    <td class="text-right mono">${tr.price.toFixed(4)}</td>
    <td class="text-right mono">${tr.size.toFixed(2)}</td>
    <td><span class="badge badge-paper">${tr.mode}</span></td>
  </tr>`;
}

// Markets rows
let marketsHtml = "";
for (const mk of marketsList) {
  const pnlC = mk.pnl >= 0 ? "#22c55e" : "#ef4444";
  marketsHtml += `<tr>
    <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${mk.question}</td>
    <td class="text-right mono">${mk.total_trades}</td>
    <td class="text-right mono" style="color:${pnlC}">$${mk.pnl.toFixed(2)}</td>
    <td class="text-right">${mk.win_rate}%</td>
    <td style="color:#d1d5db;font-size:12px">${mk.strategies}</td>
  </tr>`;
}

// Strategies cards
let stratsHtml = "";
for (const st of strategies) {
  const trophy = st.rank === 1 ? "&#127942; " : "";
  stratsHtml += `<div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <span style="font-weight:600;color:#fff">${trophy}${st.strategy}</span>
      <span style="font-size:11px;color:#6b7280">Rank #${st.rank}</span>
    </div>
    <div class="strat-grid">
      <div class="strat-metric"><div class="val white">${st.total_trades}</div><div class="lbl">Total Trades</div></div>
      <div class="strat-metric"><div class="val white">${st.closed_trades}</div><div class="lbl">Closed</div></div>
      <div class="strat-metric"><div class="val ${st.win_rate >= 50 ? "green" : "red"}">${st.win_rate}%</div><div class="lbl">Win Rate</div></div>
      <div class="strat-metric"><div class="val ${st.total_pnl >= 0 ? "green" : "red"}">$${st.total_pnl.toFixed(2)}</div><div class="lbl">Total PnL</div></div>
      <div class="strat-metric"><div class="val green">${st.winning}</div><div class="lbl">Winning</div></div>
      <div class="strat-metric"><div class="val red">${st.losing}</div><div class="lbl">Losing</div></div>
    </div>
  </div>`;
}

const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>PolyBot Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f1117;color:#e5e7eb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh}
.layout{display:flex;min-height:100vh}
.sidebar{width:220px;background:#161b22;border-right:1px solid #2a3040;display:flex;flex-direction:column;position:fixed;top:0;left:0;bottom:0;z-index:10}
.sidebar .logo{padding:16px 20px;border-bottom:1px solid #2a3040;font-size:18px;font-weight:700;color:#fff}
.sidebar .logo span{color:#6366f1}
.sidebar nav{flex:1;padding:8px}
.sidebar a{display:flex;align-items:center;gap:10px;padding:8px 12px;border-radius:8px;color:#9ca3af;text-decoration:none;font-size:14px;margin:2px 0;transition:all .15s}
.sidebar a:hover,.sidebar a.active{background:rgba(99,102,241,.15);color:#6366f1}
.sidebar .ver{padding:12px 20px;border-top:1px solid #2a3040;font-size:11px;color:#6b7280}
.main{margin-left:220px;flex:1}
.header{height:56px;border-bottom:1px solid #2a3040;display:flex;align-items:center;justify-content:space-between;padding:0 24px;position:sticky;top:0;background:#0f1117;z-index:5}
.header .status{display:flex;align-items:center;gap:8px;font-size:12px;color:#22c55e}
.dot{width:8px;height:8px;border-radius:50%;background:#22c55e;display:inline-block;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.content{padding:24px;max-width:1200px}
h2{font-size:18px;font-weight:600;margin-bottom:16px}
h3{font-size:14px;font-weight:500;color:#9ca3af;margin-bottom:12px}
.section{margin-bottom:36px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:16px}
@media(max-width:900px){.grid4{grid-template-columns:repeat(2,1fr)}.sidebar{display:none}.main{margin-left:0}}
.card{background:#161b22;border:1px solid #2a3040;border-radius:12px;padding:16px}
.stat-val{font-size:24px;font-weight:700;letter-spacing:-0.5px}
.stat-label{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;margin-top:4px}
.green{color:#22c55e}.red{color:#ef4444}.blue{color:#60a5fa}.white{color:#fff}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{text-align:left;padding:10px 14px;color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #2a3040;font-weight:500}
tbody td{padding:10px 14px;border-bottom:1px solid rgba(42,48,64,.5)}
tbody tr:hover{background:rgba(28,35,51,.5)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.badge-buy{background:rgba(34,197,94,.15);color:#22c55e}
.badge-sell{background:rgba(239,68,68,.15);color:#ef4444}
.badge-paper{background:rgba(96,165,250,.15);color:#60a5fa}
.strat-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
.strat-metric .val{font-size:16px;font-weight:600;font-family:monospace}
.strat-metric .lbl{font-size:11px;color:#6b7280}
.bar-chart{display:flex;align-items:flex-end;gap:3px;height:120px;margin-top:8px}
.bar{min-width:6px;flex:1;border-radius:3px 3px 0 0}
.mono{font-family:'SF Mono',Monaco,Consolas,monospace}
.text-right{text-align:right}
.overflow-x{overflow-x:auto}
.config-row{display:flex;justify-content:space-between;padding:6px 0;font-size:13px}
.config-row .key{color:#9ca3af}.config-row .val{font-family:monospace;color:#fff}
.divider{height:1px;background:#2a3040;margin:24px 0}
</style>
</head>
<body>
<div class="layout">
<aside class="sidebar">
  <div class="logo">Poly<span>Bot</span></div>
  <nav>
    <a href="#overview" class="active">Overview</a>
    <a href="#performance">Performance</a>
    <a href="#trades">Trades</a>
    <a href="#markets">Markets</a>
    <a href="#strategies">Strategies</a>
    <a href="#risk">Risk</a>
    <a href="#calibration">Calibration</a>
    <a href="#config">Config</a>
  </nav>
  <div class="ver">Polymarket Bot v1.0</div>
</aside>
<div class="main">
<div class="header">
  <span style="font-size:14px;color:#9ca3af">PolyBot Dashboard</span>
  <div class="status"><div class="dot" style="background:${botFresh ? '#22c55e' : '#ef4444'}"></div> ${botState ? botState.mode.toUpperCase() + ' Mode' : 'Paper Trading Mode'}${botFresh ? '' : (botState ? ` | <span style="color:#ef4444">STALE (${staleSeconds}s)</span>` : ' | <span style="color:#ef4444">NO HEARTBEAT</span>')}${botState && botState.circuit_breaker_active ? ' | <span style="color:#ef4444">CIRCUIT BREAKER</span>' : ''}</div>
</div>
<div class="content">

<div class="section" id="overview">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
    <div class="dot" style="background:${botFresh ? '#22c55e' : '#ef4444'}"></div>
    <h2 style="margin:0">${botFresh ? 'Bot Active' : (botState ? `Bot Stale (${staleSeconds}s since last tick)` : 'Bot Offline (no state)')}</h2>
  </div>
  <div class="grid4">
    <div class="card"><div class="stat-val" style="color:${pnlColor}">${pnlSign}$${o.total_pnl.toFixed(2)}</div><div class="stat-label">Net PnL (after fees)</div></div>
    <div class="card"><div class="stat-val white">$${o.realised_pnl.toFixed(2)}</div><div class="stat-label">Realised PnL</div></div>
    <div class="card"><div class="stat-val ${o.unrealised_pnl >= 0 ? 'green' : 'red'}">$${o.unrealised_pnl.toFixed(2)}</div><div class="stat-label">Unrealised PnL</div></div>
    <div class="card"><div class="stat-val white">${o.win_rate}%${o.win_rate_n ? ` <span style="font-size:11px;color:#9ca3af">(n=${o.win_rate_n})</span>` : ''}</div><div class="stat-label">Win Rate (closed)</div></div>
  </div>
  <div class="grid4">
    <div class="card"><div class="stat-val green">${o.winning}</div><div class="stat-label">Winning Trades</div></div>
    <div class="card"><div class="stat-val red">${o.losing}</div><div class="stat-label">Losing Trades</div></div>
    <div class="card"><div class="stat-val ${o.open_losers > 0 ? 'red' : 'white'}">${o.open_losers}</div><div class="stat-label">Open Losers</div></div>
    <div class="card"><div class="stat-val white">${o.open_pos}</div><div class="stat-label">Open Positions</div></div>
  </div>
</div>

${botState && botState.risk_metrics && botState.risk_metrics.n > 0 ? `
<div class="divider"></div>
<div class="section" id="rolling-risk">
  <h2>Rolling risk-adjusted (last ${botState.risk_metrics.tail_window || botState.risk_metrics.n} closed)</h2>
  <div class="grid4">
    <div class="card"><div class="stat-val ${botState.risk_metrics.sharpe_annualized >= 0 ? 'green' : 'red'}">${botState.risk_metrics.sharpe_annualized.toFixed(2)}</div><div class="stat-label">Sharpe (annualized)</div></div>
    <div class="card"><div class="stat-val ${botState.risk_metrics.sortino_annualized >= 0 ? 'green' : 'red'}">${botState.risk_metrics.sortino_annualized.toFixed(2)}</div><div class="stat-label">Sortino (annualized)</div></div>
    <div class="card"><div class="stat-val red">${(botState.risk_metrics.max_drawdown * 100).toFixed(2)}%</div><div class="stat-label">Max Drawdown (rolling)</div></div>
    <div class="card"><div class="stat-val ${botState.risk_metrics.psr_vs_zero >= 0.95 ? 'green' : 'white'}">${(botState.risk_metrics.psr_vs_zero * 100).toFixed(1)}%</div><div class="stat-label">PSR vs SR=0 (n=${botState.risk_metrics.n})</div></div>
  </div>
  <div style="margin-top:8px;font-size:11px;color:#9ca3af">
    PSR &ge; 95% means the observed Sharpe is statistically distinguishable from 0 after correcting for skew/kurtosis. Use <code>validate-strategy</code> for the full promote-to-live veredict.
  </div>
</div>
` : ''}

${botState && botState.shadow && Array.isArray(botState.shadow.runners) && botState.shadow.runners.length > 0 ? (() => {
  const liveSharpe = (botState.risk_metrics && botState.risk_metrics.sharpe_annualized) || 0;
  const livePnl = (botState.portfolio && botState.portfolio.net_pnl_after_fees) || 0;
  const cards = botState.shadow.runners.map(sh => {
    const shrm = sh.risk_metrics || {n: 0, sharpe_annualized: 0, psr_vs_zero: 0};
    const shadowNet = (sh.realised_pnl || 0) + (sh.unrealised_pnl || 0) - (sh.fees_paid || 0);
    const sharpeDiff = (shrm.sharpe_annualized || 0) - liveSharpe;
    const pnlDiff = shadowNet - livePnl;
    const wr = sh.win_rate || {total_closed: 0, win_rate: 0};
    return `
<div class="card" style="text-align:left;padding:14px">
  <div style="font-weight:600;margin-bottom:8px">${sh.strategy} <span style="font-size:11px;color:#9ca3af">vs ${botState.strategy}</span></div>
  <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:6px 14px;font-size:12px">
    <div><span style="color:#9ca3af">Closed n:</span> <span class="mono">${shrm.n || 0}</span></div>
    <div><span style="color:#9ca3af">Win rate:</span> <span class="mono">${wr.total_closed ? (wr.win_rate * 100).toFixed(1) + '%' : '—'}</span></div>
    <div><span style="color:#9ca3af">Net PnL:</span> <span class="mono ${shadowNet >= 0 ? 'green' : 'red'}">$${shadowNet.toFixed(2)}</span></div>
    <div><span style="color:#9ca3af">Open:</span> <span class="mono">${sh.open_positions}</span></div>
    <div><span style="color:#9ca3af">Sharpe:</span> <span class="mono ${(shrm.sharpe_annualized || 0) >= 0 ? 'green' : 'red'}">${(shrm.sharpe_annualized || 0).toFixed(2)}</span></div>
    <div><span style="color:#9ca3af">PSR:</span> <span class="mono ${(shrm.psr_vs_zero || 0) >= 0.95 ? 'green' : 'white'}">${((shrm.psr_vs_zero || 0) * 100).toFixed(1)}%</span></div>
    <div><span style="color:#9ca3af">&Delta;PnL:</span> <span class="mono ${pnlDiff >= 0 ? 'green' : 'red'}">${pnlDiff >= 0 ? '+' : ''}$${pnlDiff.toFixed(2)}</span></div>
    <div><span style="color:#9ca3af">&Delta;Sharpe:</span> <span class="mono ${sharpeDiff >= 0 ? 'green' : 'red'}">${sharpeDiff >= 0 ? '+' : ''}${sharpeDiff.toFixed(2)}</span></div>
  </div>
</div>`;
  }).join('');
  return `
<div class="divider"></div>
<div class="section" id="ab-shadow">
  <h2>A/B shadow runners (${botState.shadow.n_runners})</h2>
  <div class="${botState.shadow.runners.length === 1 ? 'grid2' : 'grid2'}">${cards}</div>
  <div style="margin-top:8px;font-size:11px;color:#9ca3af">
    Shadow runners observe the same filtered universe as the live strategy and never place orders. <code>&Delta;</code> values are shadow &minus; live. Use <code>shadow-report</code> for a head-to-head table and <code>validate-strategy</code> before promoting any candidate to live.
  </div>
</div>`;
})() : ''}

<div class="divider"></div>

<div class="section" id="performance">
  <h2>Performance</h2>
  <div class="grid4">
    <div class="card"><div class="stat-val" style="color:${p.total_pnl >= 0 ? "#22c55e" : "#ef4444"}">$${p.total_pnl.toFixed(2)}</div><div class="stat-label">Closed PnL</div></div>
    <div class="card"><div class="stat-val white">${p.win_rate}%</div><div class="stat-label">Win Rate</div></div>
    <div class="card"><div class="stat-val white">${p.profit_factor}</div><div class="stat-label">Profit Factor</div></div>
    <div class="card"><div class="stat-val red">$${p.max_drawdown.toFixed(2)}</div><div class="stat-label">Max Drawdown</div></div>
  </div>
  <div class="grid4">
    <div class="card"><div class="stat-val green">$${p.avg_win.toFixed(2)}</div><div class="stat-label">Avg Win</div></div>
    <div class="card"><div class="stat-val red">$${p.avg_loss.toFixed(2)}</div><div class="stat-label">Avg Loss</div></div>
    <div class="card"><div class="stat-val green">${p.winning}</div><div class="stat-label">Winning</div></div>
    <div class="card"><div class="stat-val red">${p.losing}</div><div class="stat-label">Losing</div></div>
  </div>
  ${riskAdj ? `
  <div style="margin-top:16px">
    <h3>Risk-adjusted metrics</h3>
    <div class="grid4" style="margin-top:8px">
      <div class="card"><div class="stat-val white">${riskAdj.sharpe_daily}</div><div class="stat-label">Sharpe (annualised)</div></div>
      <div class="card"><div class="stat-val white">${riskAdj.sortino_daily}</div><div class="stat-label">Sortino (annualised)</div></div>
      <div class="card"><div class="stat-val white">${riskAdj.calmar}</div><div class="stat-label">Calmar (pnl / max_dd)</div></div>
      <div class="card"><div class="stat-val white">$${riskAdj.expectancy.toFixed(4)}</div><div class="stat-label">Expectancy / trade</div></div>
    </div>
    <div style="color:#9ca3af;font-size:12px;margin-top:6px">
      Sharpe/Sortino bucketed by day and annualised (×√252). Expectancy = avg PnL per trade.
      Positive expectancy + positive Sortino = system worth scaling; negative Sortino = bleeding on down days.
    </div>
  </div>
  ` : ''}
  <div class="card">
    <h3>Daily PnL</h3>
    <div class="bar-chart">${barsHtml}</div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#6b7280;margin-top:6px">
      <span>${dailyPnl[0] ? dailyPnl[0].date : ""}</span><span>${dailyPnl.length ? dailyPnl[dailyPnl.length - 1].date : ""}</span>
    </div>
  </div>
  ${equitySvg}
</div>

<div class="divider"></div>

<div class="section" id="trades">
  <h2>Recent Trades (${o.total_trades} total)</h2>
  <div class="card overflow-x" style="padding:0">
    <table>
      <thead><tr><th>Time</th><th>Market</th><th>Side</th><th>Strategy</th><th class="text-right">Price</th><th class="text-right">Size</th><th>Mode</th></tr></thead>
      <tbody>${tradesHtml}</tbody>
    </table>
  </div>
</div>

<div class="divider"></div>

<div class="section" id="markets">
  <h2>Markets (${marketsList.length})</h2>
  <div class="card overflow-x" style="padding:0">
    <table>
      <thead><tr><th>Market</th><th class="text-right">Trades</th><th class="text-right">PnL</th><th class="text-right">Win Rate</th><th>Strategies</th></tr></thead>
      <tbody>${marketsHtml}</tbody>
    </table>
  </div>
</div>

<div class="divider"></div>

<div class="section" id="strategies">
  <h2>Strategy Comparison</h2>
  <div class="grid2">${stratsHtml}</div>
</div>

<div class="divider"></div>

<div class="section" id="risk">
  <h2>Risk & Decisions</h2>
  <div class="grid4">
    <div class="card"><div class="stat-val red">$${spreadCost.toFixed(2)}</div><div class="stat-label">Est. Spread Cost</div></div>
    <div class="card"><div class="stat-val" style="color:${(o.total_pnl - spreadCost) >= 0 ? '#22c55e' : '#ef4444'}">$${(o.total_pnl - spreadCost).toFixed(2)}</div><div class="stat-label">Spread-Adjusted PnL</div></div>
    <div class="card"><div class="stat-val white">${decisionStats.risk_rejected}</div><div class="stat-label">Risk Rejections</div></div>
    <div class="card"><div class="stat-val white">${decisionStats.exits_sl + decisionStats.exits_tp}</div><div class="stat-label">SL/TP Exits</div></div>
  </div>
  <div class="grid4">
    <div class="card"><div class="stat-val red">${decisionStats.exits_sl}</div><div class="stat-label">Stop-Loss Exits</div></div>
    <div class="card"><div class="stat-val green">${decisionStats.exits_tp}</div><div class="stat-label">Take-Profit Exits</div></div>
    <div class="card"><div class="stat-val white">${decisionStats.entries}</div><div class="stat-label">Total Decisions</div></div>
    <div class="card"><div class="stat-val white">${botState ? botState.tick_count : '-'}</div><div class="stat-label">Ticks Completed</div></div>
  </div>
  ${resolutionStats.total > 0 ? `
  <div style="margin-top:12px">
    <h3>Market Resolution Accuracy (ground truth)</h3>
    <div class="grid4" style="margin-top:8px">
      <div class="card"><div class="stat-val white">${resolutionStats.total}</div><div class="stat-label">Markets Resolved</div></div>
      <div class="card"><div class="stat-val" style="color:${resolutionStats.accuracy >= 50 ? '#22c55e' : '#ef4444'}">${resolutionStats.accuracy}%</div><div class="stat-label">Prediction Accuracy</div></div>
      <div class="card"><div class="stat-val green">${resolutionStats.correct}</div><div class="stat-label">Correct Predictions</div></div>
      <div class="card"><div class="stat-val" style="color:${resolutionStats.pnl >= 0 ? '#22c55e' : '#ef4444'}">$${resolutionStats.pnl.toFixed(2)}</div><div class="stat-label">Resolution PnL</div></div>
    </div>
  </div>
  ` : ''}
  ${edgeCalibration && edgeCalibration.length > 0 ? `
  <div style="margin-top:16px">
    <h3>Edge-model calibration (confidence → accuracy)</h3>
    <div class="card overflow-x" style="padding:0;margin-top:8px">
      <table>
        <thead><tr><th>Confidence</th><th class="text-right">N</th><th class="text-right">Correct</th><th class="text-right">Accuracy</th><th class="text-right">Avg PnL</th></tr></thead>
        <tbody>${edgeCalibration.map(b => `
          <tr>
            <td class="mono">${b.range}</td>
            <td class="text-right mono">${b.count}</td>
            <td class="text-right mono">${b.correct}</td>
            <td class="text-right mono" style="color:${parseFloat(b.accuracy) >= 50 ? '#22c55e' : '#ef4444'}">${b.accuracy}%</td>
            <td class="text-right mono" style="color:${parseFloat(b.avg_pnl) >= 0 ? '#22c55e' : '#ef4444'}">$${b.avg_pnl}</td>
          </tr>`).join('')}</tbody>
      </table>
    </div>
    <div style="color:#9ca3af;font-size:12px;margin-top:6px">
      Well-calibrated models show increasing accuracy with higher confidence buckets.
    </div>
  </div>
  ` : ''}
  ${rejectionBreakdown.length > 0 ? `
  <div style="margin-top:16px">
    <h3>Risk rejection reasons</h3>
    <div class="card overflow-x" style="padding:0;margin-top:8px">
      <table>
        <thead><tr><th>Reason</th><th class="text-right">Count</th></tr></thead>
        <tbody>${rejectionBreakdown.map(r => `
          <tr><td class="mono">${r.reason}</td><td class="text-right mono">${r.count}</td></tr>
        `).join('')}</tbody>
      </table>
    </div>
    <div style="color:#9ca3af;font-size:12px;margin-top:6px">
      Gates ordered by frequency. stale_price = snap/book midpoint divergence; book_slippage = depth too thin; book_imbalance_contra = flow against our direction.
    </div>
  </div>
  ` : ''}
  ${bookQualityStats ? `
  <div style="margin-top:16px">
    <h3>Execution quality (last ${bookQualityStats.n} entries)</h3>
    <div class="grid4" style="margin-top:8px">
      <div class="card"><div class="stat-val white">${bookQualityStats.avg_slippage_bps}</div><div class="stat-label">Avg Slippage (bps)</div></div>
      <div class="card"><div class="stat-val white">${bookQualityStats.max_slippage_bps}</div><div class="stat-label">Max Slippage (bps)</div></div>
      <div class="card"><div class="stat-val white">${bookQualityStats.avg_imbalance}</div><div class="stat-label">Avg Book Imbalance</div></div>
      <div class="card"><div class="stat-val white">${bookQualityStats.n}</div><div class="stat-label">Entries Analyzed</div></div>
    </div>
  </div>
  ` : ''}
  ${resolutionRecent.length > 0 ? `
  <div style="margin-top:16px">
    <h3>Recent resolved markets</h3>
    <div class="card overflow-x" style="padding:0;margin-top:8px">
      <table>
        <thead><tr><th>Question</th><th>Outcome</th><th>Side</th><th class="text-right">Entry</th><th class="text-right">Exit</th><th class="text-right">PnL</th><th>Prediction</th></tr></thead>
        <tbody>${resolutionRecent.map(r => `
          <tr>
            <td style="max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.question || '-'}</td>
            <td><span class="badge ${r.outcome === 'YES' ? 'badge-buy' : 'badge-sell'}">${r.outcome || '-'}</span></td>
            <td>${r.our_side || '-'}</td>
            <td class="text-right mono">${r.our_entry_price ? r.our_entry_price.toFixed(3) : '-'}</td>
            <td class="text-right mono">${r.our_exit_price ? r.our_exit_price.toFixed(3) : '-'}</td>
            <td class="text-right mono" style="color:${r.our_pnl >= 0 ? '#22c55e' : '#ef4444'}">$${(r.our_pnl || 0).toFixed(2)}</td>
            <td style="color:${r.prediction_correct ? '#22c55e' : '#ef4444'}">${r.prediction_correct ? '✓ correct' : '✗ wrong'}</td>
          </tr>`).join('')}</tbody>
      </table>
    </div>
  </div>
  ` : ''}
</div>

<div class="divider"></div>

${botState && botState.positions && botState.positions.length > 0 ? `
<div class="section" id="open-positions">
  <h2>Open Positions (${botState.positions.length})</h2>
  <div class="card overflow-x" style="padding:0">
    <table>
      <thead><tr><th>Token</th><th>Side</th><th>Strategy</th><th class="text-right">Size</th><th class="text-right">Entry</th><th class="text-right">Current</th><th class="text-right">Unreal. PnL</th></tr></thead>
      <tbody>${botState.positions.map(pp => {
        const upnlColor = pp.unrealised_pnl >= 0 ? '#22c55e' : '#ef4444';
        return `<tr>
          <td style="font-family:monospace;font-size:12px">${pp.token_id}</td>
          <td><span class="badge badge-buy">${pp.side}</span></td>
          <td style="color:#d1d5db">${pp.strategy}</td>
          <td class="text-right mono">${pp.size.toFixed(2)}</td>
          <td class="text-right mono">${pp.entry_price.toFixed(4)}</td>
          <td class="text-right mono">${pp.current_price ? pp.current_price.toFixed(4) : '-'}</td>
          <td class="text-right mono" style="color:${upnlColor}">${pp.unrealised_pnl ? '$' + pp.unrealised_pnl.toFixed(2) : '-'}</td>
        </tr>`;
      }).join('')}</tbody>
    </table>
  </div>
</div>
<div class="divider"></div>
` : ''}

${calibrationBuckets.length > 0 ? `
<div class="section" id="calibration">
  <h2>Confidence Calibration</h2>
  <p style="font-size:13px;color:#9ca3af;margin-bottom:16px">
    A well-calibrated strategy shows higher win-rates in higher confidence buckets.
    Flat or inverted curves indicate the confidence signal is not predictive.
  </p>
  <div class="grid2">
    <div class="card overflow-x" style="padding:0">
      <table>
        <thead><tr><th>Bucket</th><th class="text-right">N</th><th class="text-right">Wins</th><th class="text-right">Win Rate</th><th class="text-right">Avg Return</th><th class="text-right">PnL</th></tr></thead>
        <tbody>${calibrationBuckets.map(b => {
          const wrColor = b.winRate >= 0.5 ? '#22c55e' : b.n === 0 ? '#6b7280' : '#ef4444';
          const pnlColor2 = b.totPnl >= 0 ? '#22c55e' : '#ef4444';
          return `<tr>
            <td class="mono">${b.bucket}</td>
            <td class="text-right mono">${b.n}</td>
            <td class="text-right mono">${b.wins}</td>
            <td class="text-right mono" style="color:${wrColor}">${(b.winRate * 100).toFixed(1)}%</td>
            <td class="text-right mono" style="color:${b.avgRet >= 0 ? '#22c55e' : '#ef4444'}">${(b.avgRet * 100).toFixed(2)}%</td>
            <td class="text-right mono" style="color:${pnlColor2}">$${b.totPnl.toFixed(2)}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>
    </div>
    <div class="card">
      <h3>Win Rate by Confidence</h3>
      <div style="display:flex;align-items:flex-end;gap:8px;height:120px;margin-top:12px">
        ${calibrationBuckets.map(b => {
          const h = b.n === 0 ? 4 : Math.max(4, b.winRate * 100);
          const color = b.n === 0 ? '#2a3040' : b.winRate >= 0.5 ? '#22c55e' : '#ef4444';
          return `<div style="flex:1;display:flex;flex-direction:column;align-items:center">
            <div style="width:100%;height:${h}px;background:${color};border-radius:3px 3px 0 0" title="${b.bucket}: ${(b.winRate * 100).toFixed(1)}% (n=${b.n})"></div>
            <div style="font-size:9px;color:#6b7280;margin-top:4px">${b.bucket.slice(1, 5)}</div>
          </div>`;
        }).join('')}
      </div>
      <div style="text-align:center;font-size:10px;color:#6b7280;margin-top:8px">Confidence Range</div>
    </div>
  </div>
</div>
<div class="divider"></div>
` : `
<div class="section" id="calibration">
  <h2>Confidence Calibration</h2>
  <div class="card"><p style="color:#6b7280;font-size:13px">No calibration data yet. The bot records confidence vs outcome on each closed trade. Run the bot to accumulate data.</p></div>
</div>
<div class="divider"></div>
`}

<div class="section" id="config">
  <h2>Configuration${botState ? ' (live from bot)' : ' (defaults)'}</h2>
  <div class="grid2">
    <div class="card">
      <h3 style="border-bottom:1px solid #2a3040;padding-bottom:8px">Trading</h3>
      <div class="config-row"><span class="key">Trading Mode</span><span class="val" style="color:#60a5fa">${botState ? botState.mode.toUpperCase() : 'PAPER'}</span></div>
      <div class="config-row"><span class="key">Circuit Breaker</span><span class="val" style="color:${botState && botState.circuit_breaker_active ? '#ef4444' : '#22c55e'}">${botState ? (botState.circuit_breaker_active ? 'ACTIVE' : 'OK') : '-'}</span></div>
      <div class="config-row"><span class="key">Daily PnL</span><span class="val">${botState ? '$' + botState.daily_pnl.toFixed(2) : '-'}</span></div>
      <div class="config-row"><span class="key">Poll Interval</span><span class="val">${botState ? botState.config.poll_interval + 's' : '-'}</span></div>
      <div class="config-row"><span class="key">Strategy</span><span class="val">${botState ? botState.strategy : '-'}</span></div>
    </div>
    <div class="card">
      <h3 style="border-bottom:1px solid #2a3040;padding-bottom:8px">Risk Management</h3>
      <div class="config-row"><span class="key">Max Position Size</span><span class="val">$${botState ? botState.config.max_position_size : '50'}</span></div>
      <div class="config-row"><span class="key">Max Exposure</span><span class="val">$${botState ? botState.config.max_total_exposure : '200'}</span></div>
      <div class="config-row"><span class="key">Stop Loss</span><span class="val">${botState ? (botState.config.stop_loss_pct * 100).toFixed(0) : '10'}%</span></div>
      <div class="config-row"><span class="key">Take Profit</span><span class="val">${botState ? (botState.config.take_profit_pct * 100).toFixed(0) : '20'}%</span></div>
      <div class="config-row"><span class="key">Max Open Positions</span><span class="val">${botState ? botState.config.max_open_positions : '5'}</span></div>
      <div class="config-row"><span class="key">Max Daily Loss</span><span class="val">$${botState ? botState.config.max_daily_loss : '50'}</span></div>
      <div class="config-row"><span class="key">Max Spread</span><span class="val">${botState ? botState.config.max_spread : '0.15'}</span></div>
      <div class="config-row"><span class="key">Price Range</span><span class="val">${botState ? botState.config.min_price + ' - ' + botState.config.max_price : '0.05 - 0.95'}</span></div>
    </div>
  </div>
</div>

<div style="text-align:center;padding:24px;font-size:11px;color:#4b5563">
  Generated ${new Date().toISOString().slice(0, 19).replace("T", " ")} UTC &middot; PolyBot v1.0
</div>

</div></div></div>
</body>
</html>`;

const outDir = path.join(__dirname, "docs");
if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(path.join(outDir, "dashboard.html"), html);
console.log("Dashboard HTML generated:", html.length, "bytes");
db.close();
