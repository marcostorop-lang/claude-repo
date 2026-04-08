import { NextResponse } from "next/server";
import { getOverview, getTrades, getPerformance, getStrategies, getPositions, getMarkets, getLogs, getConfig } from "@/lib/data";

export const dynamic = "force-dynamic";

export async function GET() {
  const overview = getOverview();
  const trades = getTrades(1, 30);
  const perf = getPerformance();
  const strategies = getStrategies();
  const positions = getPositions();
  const markets = getMarkets();
  const logs = getLogs();
  const config = getConfig();

  const pnlColor = overview.total_pnl >= 0 ? "#22c55e" : "#ef4444";
  const pnlSign = overview.total_pnl >= 0 ? "+" : "";

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
.main{margin-left:220px;flex:1;padding:0}
.header{height:56px;border-bottom:1px solid #2a3040;display:flex;align-items:center;justify-content:between;padding:0 24px;position:sticky;top:0;background:#0f1117;z-index:5}
.header .status{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:12px;color:#22c55e}
.header .dot{width:8px;height:8px;border-radius:50%;background:#22c55e;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.content{padding:24px}
h2{font-size:18px;font-weight:600;margin-bottom:16px}
h3{font-size:14px;font-weight:500;color:#9ca3af;margin-bottom:12px}
.section{margin-bottom:32px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:16px}
@media(max-width:900px){.grid4,.grid3{grid-template-columns:repeat(2,1fr)}.sidebar{display:none}.main{margin-left:0}}
.card{background:#161b22;border:1px solid #2a3040;border-radius:12px;padding:16px}
.stat-val{font-size:24px;font-weight:700;letter-spacing:-0.5px}
.stat-label{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;margin-top:4px}
.green{color:#22c55e}.red{color:#ef4444}.blue{color:#60a5fa}.yellow{color:#f59e0b}.white{color:#fff}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{text-align:left;padding:10px 14px;color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #2a3040;font-weight:500}
tbody td{padding:10px 14px;border-bottom:1px solid rgba(42,48,64,.5)}
tbody tr:hover{background:rgba(28,35,51,.5)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.badge-buy{background:rgba(34,197,94,.15);color:#22c55e}
.badge-sell{background:rgba(239,68,68,.15);color:#ef4444}
.badge-info{background:rgba(96,165,250,.15);color:#60a5fa}
.badge-warn{background:rgba(245,158,11,.15);color:#f59e0b}
.badge-err{background:rgba(239,68,68,.15);color:#ef4444}
.badge-paper{background:rgba(96,165,250,.15);color:#60a5fa}
.tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid #2a3040;padding-bottom:8px}
.tab{padding:6px 16px;border-radius:6px;font-size:13px;color:#9ca3af;cursor:pointer;border:none;background:transparent}
.tab.active{background:rgba(99,102,241,.15);color:#6366f1}
.strat-card{display:flex;flex-direction:column;gap:12px}
.strat-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
.strat-metric .val{font-size:16px;font-weight:600;font-family:monospace}
.strat-metric .lbl{font-size:11px;color:#6b7280}
.config-row{display:flex;justify-content:space-between;padding:6px 0;font-size:13px}
.config-row .key{color:#9ca3af}.config-row .val{font-family:monospace;color:#fff}
.bar-chart{display:flex;align-items:flex-end;gap:3px;height:120px;margin-top:8px}
.bar{min-width:8px;flex:1;border-radius:3px 3px 0 0;transition:height .3s}
.rank{font-size:11px;color:#6b7280}
.trophy{color:#f59e0b}
.overflow-x{overflow-x:auto}
.text-right{text-align:right}
.mono{font-family:'SF Mono',Monaco,Consolas,monospace}
</style>
</head>
<body>
<div class="layout">
<aside class="sidebar">
  <div class="logo">Poly<span>Bot</span></div>
  <nav>
    <a href="/dashboard" class="active">Overview</a>
    <a href="/dashboard#trades">Trades</a>
    <a href="/dashboard#performance">Performance</a>
    <a href="/dashboard#positions">Positions</a>
    <a href="/dashboard#markets">Markets</a>
    <a href="/dashboard#strategies">Strategies</a>
    <a href="/dashboard#logs">Logs</a>
    <a href="/dashboard#config">Config</a>
  </nav>
  <div class="ver">Polymarket Bot v1.0</div>
</aside>

<div class="main">
<div class="header">
  <span style="font-size:14px;color:#9ca3af">Dashboard</span>
  <div class="status"><div class="dot"></div> Connected${overview.last_update ? ` &middot; Last: ${new Date(overview.last_update).toLocaleTimeString()}` : ""}</div>
</div>

<div class="content">

<!-- OVERVIEW -->
<div class="section" id="overview">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
    <div class="dot"></div>
    <h2 style="margin:0">Bot ${overview.bot_active ? "Active" : "Stopped"}</h2>
  </div>
  <div class="grid4">
    <div class="card"><div class="stat-val" style="color:${pnlColor}">${pnlSign}$${overview.total_pnl.toFixed(2)}</div><div class="stat-label">Total PnL</div></div>
    <div class="card"><div class="stat-val white">$${overview.simulated_balance.toFixed(2)}</div><div class="stat-label">Simulated Balance</div></div>
    <div class="card"><div class="stat-val white">${overview.total_trades}</div><div class="stat-label">Total Trades</div></div>
    <div class="card"><div class="stat-val white">${overview.winning_trades + overview.losing_trades > 0 ? ((overview.winning_trades / (overview.winning_trades + overview.losing_trades)) * 100).toFixed(1) : 0}%</div><div class="stat-label">Win Rate</div></div>
  </div>
  <div class="grid4">
    <div class="card"><div class="stat-val green">${overview.winning_trades}</div><div class="stat-label">Winning Trades</div></div>
    <div class="card"><div class="stat-val red">${overview.losing_trades}</div><div class="stat-label">Losing Trades</div></div>
    <div class="card"><div class="stat-val white">${overview.markets_monitored}</div><div class="stat-label">Markets Monitored</div></div>
    <div class="card"><div class="stat-val white">${overview.open_positions}</div><div class="stat-label">Open Positions</div></div>
  </div>
</div>

<!-- PERFORMANCE -->
<div class="section" id="performance">
  <h2>Performance</h2>
  ${perf.total_closed === 0 ? '<div class="card" style="text-align:center;color:#6b7280;padding:32px">No closed trades yet.</div>' : `
  <div class="grid4">
    <div class="card"><div class="stat-val" style="color:${perf.total_pnl >= 0 ? '#22c55e' : '#ef4444'}">$${perf.total_pnl.toFixed(2)}</div><div class="stat-label">Total PnL</div></div>
    <div class="card"><div class="stat-val white">${perf.win_rate}%</div><div class="stat-label">Win Rate</div></div>
    <div class="card"><div class="stat-val white">${perf.profit_factor}</div><div class="stat-label">Profit Factor</div></div>
    <div class="card"><div class="stat-val red">$${perf.max_drawdown.toFixed(2)}</div><div class="stat-label">Max Drawdown</div></div>
  </div>
  <div class="grid4">
    <div class="card"><div class="stat-val green">$${perf.avg_win.toFixed(2)}</div><div class="stat-label">Avg Win</div></div>
    <div class="card"><div class="stat-val red">$${perf.avg_loss.toFixed(2)}</div><div class="stat-label">Avg Loss</div></div>
    <div class="card"><div class="stat-val white">${perf.reward_risk_ratio}</div><div class="stat-label">Reward/Risk</div></div>
    <div class="card"><div class="stat-val white">${perf.total_closed}</div><div class="stat-label">Closed Trades</div></div>
  </div>
  ${perf.daily_pnl.length > 0 ? `
  <div class="card">
    <h3>Daily PnL</h3>
    <div class="bar-chart">
      ${perf.daily_pnl.map(d => {
        const maxAbs = Math.max(...perf.daily_pnl.map(x => Math.abs(x.pnl)), 1);
        const h = Math.max(4, Math.abs(d.pnl) / maxAbs * 100);
        const color = d.pnl >= 0 ? '#22c55e' : '#ef4444';
        return `<div class="bar" style="height:${h}px;background:${color}" title="${d.date}: $${d.pnl.toFixed(2)}"></div>`;
      }).join('')}
    </div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#6b7280;margin-top:4px">
      <span>${perf.daily_pnl[0]?.date || ''}</span>
      <span>${perf.daily_pnl[perf.daily_pnl.length - 1]?.date || ''}</span>
    </div>
  </div>` : ''}
  ${perf.equity_curve.length > 0 ? `
  <div class="card" style="margin-top:12px">
    <h3>Equity Curve</h3>
    <svg viewBox="0 0 ${perf.equity_curve.length * 10} 120" style="width:100%;height:120px" preserveAspectRatio="none">
      ${(() => {
        const pts = perf.equity_curve;
        const minE = Math.min(...pts.map(p => p.equity));
        const maxE = Math.max(...pts.map(p => p.equity));
        const range = maxE - minE || 1;
        const points = pts.map((p, i) => `${i * 10},${110 - ((p.equity - minE) / range) * 100}`).join(' ');
        const lastColor = pts[pts.length - 1].equity >= 0 ? '#22c55e' : '#ef4444';
        return `<polyline points="${points}" fill="none" stroke="${lastColor}" stroke-width="2"/>`;
      })()}
    </svg>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#6b7280;margin-top:4px">
      <span>$${Math.min(...perf.equity_curve.map(p => p.equity)).toFixed(2)}</span>
      <span>$${Math.max(...perf.equity_curve.map(p => p.equity)).toFixed(2)}</span>
    </div>
  </div>` : ''}
  `}
</div>

<!-- TRADES -->
<div class="section" id="trades">
  <h2>Recent Trades (${trades.total} total)</h2>
  <div class="card overflow-x" style="padding:0">
    <table>
      <thead><tr>
        <th>Time</th><th>Market</th><th>Side</th><th>Strategy</th><th class="text-right">Price</th><th class="text-right">Size</th><th>Mode</th>
      </tr></thead>
      <tbody>
        ${trades.trades.map(t => `<tr>
          <td style="color:#6b7280;white-space:nowrap">${new Date(t.timestamp as string).toLocaleString()}</td>
          <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(t.question as string) || (t.condition_id as string).slice(0, 12)}</td>
          <td><span class="badge ${t.side === 'BUY' ? 'badge-buy' : 'badge-sell'}">${t.side}</span></td>
          <td style="color:#d1d5db">${t.strategy}</td>
          <td class="text-right mono">${(t.price as number).toFixed(4)}</td>
          <td class="text-right mono">${(t.size as number).toFixed(2)}</td>
          <td><span class="badge badge-paper">${t.mode}</span></td>
        </tr>`).join('')}
      </tbody>
    </table>
  </div>
</div>

<!-- POSITIONS -->
<div class="section" id="positions">
  <h2>Open Positions (${positions.positions.length})</h2>
  ${positions.positions.length === 0 ? '<div class="card" style="text-align:center;color:#6b7280;padding:32px">No open positions.</div>' : `
  <div class="card overflow-x" style="padding:0">
    <table>
      <thead><tr>
        <th>Market</th><th>Side</th><th class="text-right">Entry</th><th class="text-right">Current</th><th class="text-right">Size</th><th class="text-right">Unrealised PnL</th><th>Strategy</th>
      </tr></thead>
      <tbody>
        ${positions.positions.map(p => `<tr>
          <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.question}</td>
          <td><span class="badge badge-buy">${p.side}</span></td>
          <td class="text-right mono">${(p.entry_price as number).toFixed(4)}</td>
          <td class="text-right mono">${(p.current_price as number).toFixed(4)}</td>
          <td class="text-right mono">${(p.size as number).toFixed(2)}</td>
          <td class="text-right mono" style="color:${(p.unrealised_pnl as number) >= 0 ? '#22c55e' : '#ef4444'}">$${(p.unrealised_pnl as number).toFixed(2)}</td>
          <td style="color:#d1d5db">${p.strategy}</td>
        </tr>`).join('')}
      </tbody>
    </table>
  </div>`}
</div>

<!-- MARKETS -->
<div class="section" id="markets">
  <h2>Markets (${markets.markets.length})</h2>
  <div class="card overflow-x" style="padding:0">
    <table>
      <thead><tr>
        <th>Market</th><th class="text-right">Trades</th><th class="text-right">PnL</th><th class="text-right">Win Rate</th><th>Strategies</th>
      </tr></thead>
      <tbody>
        ${markets.markets.map(m => `<tr>
          <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${m.question}</td>
          <td class="text-right mono">${m.total_trades}</td>
          <td class="text-right mono" style="color:${m.pnl >= 0 ? '#22c55e' : '#ef4444'}">$${m.pnl.toFixed(2)}</td>
          <td class="text-right">${m.win_rate}%</td>
          <td style="color:#d1d5db;font-size:12px">${m.strategies}</td>
        </tr>`).join('')}
      </tbody>
    </table>
  </div>
</div>

<!-- STRATEGIES -->
<div class="section" id="strategies">
  <h2>Strategy Comparison</h2>
  <div class="grid2">
    ${strategies.map(s => `
    <div class="card strat-card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div style="display:flex;align-items:center;gap:8px">
          ${s.rank === 1 ? '<span class="trophy">&#127942;</span>' : ''}
          <span style="font-weight:600;color:#fff">${s.strategy}</span>
        </div>
        <span class="rank">Rank #${s.rank}</span>
      </div>
      <div class="strat-grid">
        <div class="strat-metric"><div class="val white">${s.total_trades}</div><div class="lbl">Total Trades</div></div>
        <div class="strat-metric"><div class="val white">${s.closed_trades}</div><div class="lbl">Closed</div></div>
        <div class="strat-metric"><div class="val ${s.win_rate >= 50 ? 'green' : 'red'}">${s.win_rate}%</div><div class="lbl">Win Rate</div></div>
        <div class="strat-metric"><div class="val ${s.total_pnl >= 0 ? 'green' : 'red'}">$${s.total_pnl.toFixed(2)}</div><div class="lbl">Total PnL</div></div>
        <div class="strat-metric"><div class="val green">${s.winning}</div><div class="lbl">Winning</div></div>
        <div class="strat-metric"><div class="val red">${s.losing}</div><div class="lbl">Losing</div></div>
        <div class="strat-metric"><div class="val red">$${s.max_drawdown.toFixed(2)}</div><div class="lbl">Max DD</div></div>
      </div>
    </div>`).join('')}
  </div>
</div>

<!-- LOGS -->
<div class="section" id="logs">
  <h2>Logs & Events</h2>
  <div class="card overflow-x" style="padding:0;max-height:400px;overflow-y:auto">
    <table style="font-size:12px;font-family:monospace">
      <tbody>
        ${logs.logs.map(l => `<tr>
          <td style="padding:6px 10px;color:#6b7280;white-space:nowrap">${new Date(l.timestamp).toLocaleString()}</td>
          <td style="padding:6px 10px"><span class="badge ${l.level === 'ERROR' ? 'badge-err' : l.level === 'WARNING' ? 'badge-warn' : 'badge-info'}">${l.level}</span></td>
          <td style="padding:6px 10px;color:#6b7280;max-width:150px;overflow:hidden;text-overflow:ellipsis">${l.source}</td>
          <td style="padding:6px 10px;color:#d1d5db">${l.message}</td>
        </tr>`).join('')}
      </tbody>
    </table>
  </div>
</div>

<!-- CONFIG -->
<div class="section" id="config">
  <h2>Configuration <span style="font-size:12px;color:#6b7280;margin-left:8px">Read-only</span></h2>
  <div class="grid2">
    ${Object.entries({
      "Trading": ["TRADING_MODE", "ALLOW_LIVE_TRADING"],
      "Bot Loop": ["POLL_INTERVAL_SECONDS", "STRATEGY", "LOG_LEVEL"],
      "Market Filters": ["MIN_VOLUME", "MIN_LIQUIDITY", "MAX_SPREAD", "MAX_MARKETS"],
      "Risk Management": ["MAX_POSITION_SIZE", "MAX_TOTAL_EXPOSURE", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT", "MAX_OPEN_POSITIONS"],
      "Momentum": ["MOMENTUM_WINDOW", "MOMENTUM_THRESHOLD"],
      "Mean Reversion": ["MEAN_REVERSION_WINDOW", "MEAN_REVERSION_ENTRY_Z", "MEAN_REVERSION_EXIT_Z"],
    }).map(([group, keys]) => `
    <div class="card">
      <h3 style="border-bottom:1px solid #2a3040;padding-bottom:8px">${group}</h3>
      ${keys.map(k => {
        const v = (config.config as Record<string, string>)[k] || '—';
        let color = '#fff';
        if (k === 'TRADING_MODE') color = v === 'paper' ? '#60a5fa' : '#ef4444';
        if (k === 'ALLOW_LIVE_TRADING') color = v === 'true' ? '#ef4444' : '#22c55e';
        const display = k === 'TRADING_MODE' ? v.toUpperCase() : v;
        return `<div class="config-row"><span class="key">${k.replace(/_/g, ' ')}</span><span class="val" style="color:${color}">${display}</span></div>`;
      }).join('')}
    </div>`).join('')}
  </div>
</div>

</div><!-- content -->
</div><!-- main -->
</div><!-- layout -->
</body>
</html>`;

  return new NextResponse(html, {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}
