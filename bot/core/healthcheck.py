"""Minimal HTTP healthcheck server + embedded web dashboard.

Exposes:
  GET /             — Visual HTML dashboard (auto-refreshes every 10s)
  GET /health       — 200 OK with JSON summary (always, even if halted)
  GET /ready        — 200 if not halted, 503 if halted
  GET /metrics      — Prometheus-style plaintext metrics
  GET /api/trades   — Last 50 trades from trades.jsonl
  GET /api/rejections — Last 50 rejections from rejections.jsonl
  GET /api/calibration — Calibration metrics

Runs in a daemon thread so it can live alongside the asyncio bot loop,
or standalone via ``python -m bot.main healthcheck-server``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bot.config import cfg

logger = logging.getLogger(__name__)

_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# Globally-accessible state populated by the main bot loop.
_state_lock = threading.Lock()
_state: dict = {
    "started_at": time.time(),
    "last_cycle_ts": 0.0,
    "cycle_count": 0,
    "equity": cfg.starting_capital_usd,
    "daily_pnl": 0.0,
    "total_pnl": 0.0,
    "drawdown_pct": 0.0,
    "positions": 0,
    "exposure": 0.0,
    "halted": False,
    "mode": "paper",
}


def update_state(**kwargs) -> None:
    """Merge new values into the shared state dict (thread-safe)."""
    with _state_lock:
        _state.update(kwargs)


def snapshot() -> dict:
    with _state_lock:
        s = dict(_state)
    s["uptime_s"] = round(time.time() - s["started_at"], 1)
    s["seconds_since_last_cycle"] = round(
        time.time() - s["last_cycle_ts"] if s["last_cycle_ts"] else -1, 1,
    )
    return s


def _read_jsonl_tail(filename: str, n: int = 50) -> list[dict]:
    path = _LOGS_DIR / filename
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        tail = lines[-n:] if len(lines) > n else lines
        result = []
        for line in reversed(tail):
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result
    except Exception:
        return []


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Bot Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
         background: #0f1117; color: #e4e4e7; min-height: 100vh; }
  .header { background: #16181d; border-bottom: 1px solid #27272a; padding: 16px 24px;
            display: flex; align-items: center; justify-content: space-between; }
  .header h1 { font-size: 20px; font-weight: 600; }
  .header h1 span { color: #22c55e; }
  .badge { padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; text-transform: uppercase; }
  .badge-paper { background: #1e3a5f; color: #60a5fa; }
  .badge-live { background: #5f1e1e; color: #f87171; }
  .badge-halted { background: #5f1e1e; color: #f87171; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
  .meta { font-size: 12px; color: #71717a; }
  .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .card { background: #16181d; border: 1px solid #27272a; border-radius: 12px; padding: 16px; }
  .card-label { font-size: 11px; color: #71717a; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
  .card-value { font-size: 28px; font-weight: 700; }
  .card-value.green { color: #22c55e; }
  .card-value.red { color: #ef4444; }
  .card-value.yellow { color: #eab308; }
  .card-value.blue { color: #3b82f6; }
  .section { background: #16181d; border: 1px solid #27272a; border-radius: 12px; padding: 20px; margin-bottom: 20px; }
  .section h2 { font-size: 16px; font-weight: 600; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
  .section h2 .icon { font-size: 18px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 8px 10px; color: #71717a; border-bottom: 1px solid #27272a;
       font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
  td { padding: 8px 10px; border-bottom: 1px solid #1e1e23; }
  tr:hover td { background: #1a1c23; }
  .buy { color: #22c55e; font-weight: 600; }
  .sell { color: #ef4444; font-weight: 600; }
  .empty { text-align: center; color: #52525b; padding: 32px; font-size: 14px; }
  .refresh-bar { height: 3px; background: #27272a; margin-bottom: 0; position: relative; overflow: hidden; }
  .refresh-bar .fill { height: 100%; background: #3b82f6; width: 0%; transition: width 0.3s linear; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .status-dot.on { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
  .status-dot.off { background: #ef4444; }
  .calibration-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
  .cal-card { background: #1a1c23; border-radius: 8px; padding: 12px; text-align: center; }
  .cal-card .val { font-size: 22px; font-weight: 700; margin: 4px 0; }
  .cal-card .lbl { font-size: 11px; color: #71717a; }
  .footer { text-align: center; color: #3f3f46; font-size: 11px; padding: 20px; }
</style>
</head>
<body>

<div class="header">
  <h1><span>&#9679;</span> Polymarket Trading Bot</h1>
  <div>
    <span class="badge" id="modeBadge">--</span>
    <span class="meta" id="uptimeMeta"></span>
  </div>
</div>
<div class="refresh-bar"><div class="fill" id="refreshFill"></div></div>

<div class="container">

  <!-- KPI Cards -->
  <div class="grid" id="kpiGrid">
    <div class="card"><div class="card-label">Equity</div><div class="card-value" id="kpi-equity">--</div></div>
    <div class="card"><div class="card-label">Daily PnL</div><div class="card-value" id="kpi-daily">--</div></div>
    <div class="card"><div class="card-label">Total PnL</div><div class="card-value" id="kpi-total">--</div></div>
    <div class="card"><div class="card-label">Drawdown</div><div class="card-value" id="kpi-dd">--</div></div>
    <div class="card"><div class="card-label">Positions</div><div class="card-value blue" id="kpi-pos">--</div></div>
    <div class="card"><div class="card-label">Exposure</div><div class="card-value blue" id="kpi-exp">--</div></div>
    <div class="card"><div class="card-label">Cycles</div><div class="card-value" id="kpi-cycles">--</div></div>
    <div class="card"><div class="card-label">Status</div><div class="card-value" id="kpi-status">--</div></div>
  </div>

  <!-- Calibration -->
  <div class="section" id="calSection">
    <h2><span class="icon">&#127919;</span> Calibration</h2>
    <div class="calibration-grid" id="calGrid"></div>
  </div>

  <!-- Recent Trades -->
  <div class="section">
    <h2><span class="icon">&#128200;</span> Recent Trades</h2>
    <div id="tradesBody"></div>
  </div>

  <!-- Recent Rejections -->
  <div class="section">
    <h2><span class="icon">&#128683;</span> Recent Rejections</h2>
    <div id="rejectionsBody"></div>
  </div>

</div>

<div class="footer">Auto-refresh every 10 seconds &bull; Polymarket Trading Bot Dashboard</div>

<script>
const REFRESH_MS = 10000;
let timer = 0;

function $(id) { return document.getElementById(id); }

function fmt$(v) {
  if (v === undefined || v === null) return '--';
  const n = parseFloat(v);
  return (n >= 0 ? '+' : '') + '$' + n.toFixed(2);
}

function colorClass(v) {
  const n = parseFloat(v);
  if (n > 0) return 'green';
  if (n < 0) return 'red';
  return '';
}

function fmtTime(ts) {
  if (!ts) return '--';
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

function truncate(s, n) {
  if (!s) return '';
  return s.length > n ? s.substring(0, n) + '...' : s;
}

async function fetchJSON(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

async function refresh() {
  // Health
  const h = await fetchJSON('/health');
  if (h) {
    $('kpi-equity').textContent = '$' + parseFloat(h.equity).toFixed(2);

    const daily = parseFloat(h.daily_pnl);
    $('kpi-daily').textContent = fmt$(daily);
    $('kpi-daily').className = 'card-value ' + colorClass(daily);

    const total = parseFloat(h.total_pnl);
    $('kpi-total').textContent = fmt$(total);
    $('kpi-total').className = 'card-value ' + colorClass(total);

    const dd = parseFloat(h.drawdown_pct);
    $('kpi-dd').textContent = dd.toFixed(1) + '%';
    $('kpi-dd').className = 'card-value ' + (dd > 10 ? 'red' : dd > 5 ? 'yellow' : 'green');

    $('kpi-pos').textContent = h.positions;
    $('kpi-exp').textContent = '$' + parseFloat(h.exposure).toFixed(2);
    $('kpi-cycles').textContent = h.cycle_count;

    const halted = h.halted;
    $('kpi-status').innerHTML = '<span class="status-dot ' + (halted ? 'off' : 'on') + '"></span>' + (halted ? 'HALTED' : 'Running');
    $('kpi-status').className = 'card-value ' + (halted ? 'red' : 'green');

    const mode = (h.mode || 'paper').toUpperCase();
    const badge = $('modeBadge');
    badge.textContent = mode;
    badge.className = 'badge ' + (mode === 'LIVE' ? 'badge-live' : 'badge-paper');
    if (halted) badge.className = 'badge badge-halted';

    const up = parseFloat(h.uptime_s);
    const hrs = Math.floor(up / 3600);
    const mins = Math.floor((up % 3600) / 60);
    $('uptimeMeta').textContent = 'Uptime: ' + hrs + 'h ' + mins + 'm';
  }

  // Calibration
  const cal = await fetchJSON('/api/calibration');
  if (cal) {
    const g = $('calGrid');
    g.innerHTML = `
      <div class="cal-card"><div class="val">${cal.n_resolved}</div><div class="lbl">Resolved</div></div>
      <div class="cal-card"><div class="val">${cal.n_pending}</div><div class="lbl">Pending</div></div>
      <div class="cal-card"><div class="val">${cal.n_resolved > 0 ? cal.brier_score.toFixed(4) : '--'}</div><div class="lbl">Brier Score</div></div>
      <div class="cal-card"><div class="val">${cal.n_resolved > 0 ? cal.log_loss.toFixed(4) : '--'}</div><div class="lbl">Log Loss</div></div>
    `;
  }

  // Trades
  const trades = await fetchJSON('/api/trades');
  if (trades && trades.length > 0) {
    let rows = trades.map(t => `<tr>
      <td>${fmtTime(t.timestamp)}</td>
      <td>${truncate(t.question, 40)}</td>
      <td class="${(t.side||'').toLowerCase()}">${t.side}</td>
      <td>${parseFloat(t.price||0).toFixed(4)}</td>
      <td>$${parseFloat(t.size_usd||0).toFixed(2)}</td>
      <td>${parseFloat(t.edge||0).toFixed(4)}</td>
      <td>${t.mode || '--'}</td>
    </tr>`).join('');
    $('tradesBody').innerHTML = `<table>
      <thead><tr><th>Time</th><th>Market</th><th>Side</th><th>Price</th><th>Size</th><th>Edge</th><th>Mode</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  } else {
    $('tradesBody').innerHTML = '<div class="empty">No trades yet — the bot is scanning for opportunities</div>';
  }

  // Rejections
  const rej = await fetchJSON('/api/rejections');
  if (rej && rej.length > 0) {
    let rows = rej.slice(0, 20).map(r => `<tr>
      <td>${fmtTime(r.ts)}</td>
      <td>${r.strategy || '--'}</td>
      <td class="${(r.side||'').toLowerCase()}">${r.side || '--'}</td>
      <td>${parseFloat(r.price||0).toFixed(4)}</td>
      <td>${parseFloat(r.edge||0).toFixed(4)}</td>
      <td>${truncate(r.reason, 50)}</td>
    </tr>`).join('');
    $('rejectionsBody').innerHTML = `<table>
      <thead><tr><th>Time</th><th>Strategy</th><th>Side</th><th>Price</th><th>Edge</th><th>Reason</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  } else {
    $('rejectionsBody').innerHTML = '<div class="empty">No rejections recorded yet</div>';
  }
}

// Auto-refresh with progress bar
function tick() {
  timer += 100;
  const pct = (timer / REFRESH_MS) * 100;
  $('refreshFill').style.width = Math.min(pct, 100) + '%';
  if (timer >= REFRESH_MS) {
    timer = 0;
    refresh();
  }
}

refresh();
setInterval(tick, 100);
</script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, code: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 (stdlib API)
        path = self.path.split("?", 1)[0]
        snap = snapshot()

        if path == "/":
            self._html(200, _DASHBOARD_HTML)
            return

        if path == "/health":
            stale = snap["seconds_since_last_cycle"] > 600 and snap["cycle_count"] > 0
            snap["stale"] = stale
            self._json(200, snap)
            return

        if path == "/ready":
            if snap["halted"]:
                self._json(503, {"ready": False, "reason": "halted"})
            else:
                self._json(200, {"ready": True})
            return

        if path == "/metrics":
            lines = [
                "# HELP bot_equity_usd current equity in USD",
                "# TYPE bot_equity_usd gauge",
                f"bot_equity_usd {snap['equity']}",
                "# HELP bot_daily_pnl_usd daily PnL in USD",
                "# TYPE bot_daily_pnl_usd gauge",
                f"bot_daily_pnl_usd {snap['daily_pnl']}",
                "# HELP bot_total_pnl_usd total realized PnL",
                "# TYPE bot_total_pnl_usd gauge",
                f"bot_total_pnl_usd {snap['total_pnl']}",
                "# HELP bot_drawdown_pct drawdown from peak equity",
                "# TYPE bot_drawdown_pct gauge",
                f"bot_drawdown_pct {snap['drawdown_pct']}",
                "# HELP bot_positions_open number of open positions",
                "# TYPE bot_positions_open gauge",
                f"bot_positions_open {snap['positions']}",
                "# HELP bot_exposure_usd current exposure in USD",
                "# TYPE bot_exposure_usd gauge",
                f"bot_exposure_usd {snap['exposure']}",
                "# HELP bot_halted 1 if trading halted",
                "# TYPE bot_halted gauge",
                f"bot_halted {1 if snap['halted'] else 0}",
                "# HELP bot_cycle_count number of scheduler ticks since start",
                "# TYPE bot_cycle_count counter",
                f"bot_cycle_count {snap['cycle_count']}",
            ]
            self._text(200, "\n".join(lines) + "\n")
            return

        if path == "/api/trades":
            self._json(200, _read_jsonl_tail("trades.jsonl", 50))
            return

        if path == "/api/rejections":
            self._json(200, _read_jsonl_tail("rejections.jsonl", 50))
            return

        if path == "/api/calibration":
            try:
                from bot.core.calibration import compute_metrics
                m = compute_metrics()
                self._json(200, {
                    "n_resolved": m.n_resolved,
                    "n_pending": m.n_pending,
                    "brier_score": m.brier_score,
                    "log_loss": m.log_loss,
                    "mean_p_claude": m.mean_p_claude,
                    "mean_outcome": m.mean_outcome,
                })
            except Exception:
                self._json(200, {
                    "n_resolved": 0, "n_pending": 0,
                    "brier_score": 0, "log_loss": 0,
                    "mean_p_claude": 0, "mean_outcome": 0,
                })
            return

        self._json(404, {"error": "unknown path"})

    def log_message(self, fmt, *args):  # silence default per-request stderr
        logger.debug("healthcheck %s", fmt % args)


def run_server(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run the HTTP server forever (blocks)."""
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, port), _Handler)
    logger.info("Healthcheck server listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def start_in_thread(host: str = "127.0.0.1", port: int = 8787) -> threading.Thread:
    """Start the server in a daemon thread.  Returns the thread handle."""
    t = threading.Thread(
        target=run_server, args=(host, port), daemon=True, name="healthcheck",
    )
    t.start()
    return t
