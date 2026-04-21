"""Minimal HTTP healthcheck server for monitoring / Kubernetes probes.

Exposes:
  GET /health      — 200 OK with JSON summary (always, even if halted)
  GET /ready       — 200 if not halted, 503 if halted
  GET /metrics     — Prometheus-style plaintext metrics

Runs in a daemon thread so it can live alongside the asyncio bot loop,
or standalone via ``python -m bot.main healthcheck-server``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot.config import cfg

logger = logging.getLogger(__name__)


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


class _Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
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

    def do_GET(self):  # noqa: N802 (stdlib API)
        path = self.path.split("?", 1)[0]
        snap = snapshot()

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
