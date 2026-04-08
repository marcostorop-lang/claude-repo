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
const o = {
  total_trades: total.cnt, winning: wins, losing: losses,
  total_pnl: +totalPnl.toFixed(2), balance: +(1000 + totalPnl).toFixed(2),
  markets: mktsCount.cnt, open_pos: openPos.size,
  win_rate: (wins + losses) > 0 ? ((wins / (wins + losses)) * 100).toFixed(1) : "0",
};

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

// Equity curve SVG
let equitySvg = "";
if (equity.length > 0) {
  const minE = Math.min(...equity.map(pp => pp.equity));
  const maxE = Math.max(...equity.map(pp => pp.equity));
  const range = maxE - minE || 1;
  const points = equity.map((pp, i) => `${i * 10},${110 - ((pp.equity - minE) / range) * 100}`).join(" ");
  const lastColor = equity[equity.length - 1].equity >= 0 ? "#22c55e" : "#ef4444";
  equitySvg = `<div class="card" style="margin-top:12px">
    <h3>Equity Curve</h3>
    <svg viewBox="0 0 ${equity.length * 10} 120" style="width:100%;height:120px" preserveAspectRatio="none">
      <polyline points="${points}" fill="none" stroke="${lastColor}" stroke-width="2"/>
    </svg>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#6b7280;margin-top:4px">
      <span>$${minE.toFixed(2)}</span><span>$${maxE.toFixed(2)}</span>
    </div>
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
    <a href="#config">Config</a>
  </nav>
  <div class="ver">Polymarket Bot v1.0</div>
</aside>
<div class="main">
<div class="header">
  <span style="font-size:14px;color:#9ca3af">PolyBot Dashboard</span>
  <div class="status"><div class="dot"></div> Paper Trading Mode</div>
</div>
<div class="content">

<div class="section" id="overview">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
    <div class="dot"></div>
    <h2 style="margin:0">Bot Active</h2>
  </div>
  <div class="grid4">
    <div class="card"><div class="stat-val" style="color:${pnlColor}">${pnlSign}$${o.total_pnl.toFixed(2)}</div><div class="stat-label">Total PnL</div></div>
    <div class="card"><div class="stat-val white">$${o.balance.toFixed(2)}</div><div class="stat-label">Simulated Balance</div></div>
    <div class="card"><div class="stat-val white">${o.total_trades}</div><div class="stat-label">Total Trades</div></div>
    <div class="card"><div class="stat-val white">${o.win_rate}%</div><div class="stat-label">Win Rate</div></div>
  </div>
  <div class="grid4">
    <div class="card"><div class="stat-val green">${o.winning}</div><div class="stat-label">Winning Trades</div></div>
    <div class="card"><div class="stat-val red">${o.losing}</div><div class="stat-label">Losing Trades</div></div>
    <div class="card"><div class="stat-val white">${o.markets}</div><div class="stat-label">Markets</div></div>
    <div class="card"><div class="stat-val white">${o.open_pos}</div><div class="stat-label">Open Positions</div></div>
  </div>
</div>

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

<div class="section" id="config">
  <h2>Configuration</h2>
  <div class="grid2">
    <div class="card">
      <h3 style="border-bottom:1px solid #2a3040;padding-bottom:8px">Trading</h3>
      <div class="config-row"><span class="key">Trading Mode</span><span class="val" style="color:#60a5fa">PAPER</span></div>
      <div class="config-row"><span class="key">Live Trading</span><span class="val" style="color:#22c55e">false</span></div>
      <div class="config-row"><span class="key">Poll Interval</span><span class="val">30s</span></div>
      <div class="config-row"><span class="key">Strategy</span><span class="val">both</span></div>
    </div>
    <div class="card">
      <h3 style="border-bottom:1px solid #2a3040;padding-bottom:8px">Risk Management</h3>
      <div class="config-row"><span class="key">Max Position Size</span><span class="val">$50</span></div>
      <div class="config-row"><span class="key">Max Exposure</span><span class="val">$500</span></div>
      <div class="config-row"><span class="key">Stop Loss</span><span class="val">10%</span></div>
      <div class="config-row"><span class="key">Take Profit</span><span class="val">15%</span></div>
      <div class="config-row"><span class="key">Max Open Positions</span><span class="val">10</span></div>
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
