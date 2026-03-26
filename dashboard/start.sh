#!/usr/bin/env bash
# start.sh — Arranca backend + frontend del dashboard
# Uso: cd dashboard && bash start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

export SQLITE_DB_PATH="${SQLITE_DB_PATH:-$PROJECT_ROOT/polymarket_bot.db}"
export LOG_FILE="${LOG_FILE:-$PROJECT_ROOT/bot.log}"

echo "=== Polymarket Bot Dashboard ==="
echo "DB: $SQLITE_DB_PATH"
echo ""

# Kill previous instances
pkill -f "uvicorn backend.main:app" 2>/dev/null || true
pkill -f "next dev" 2>/dev/null || true
sleep 1

# Start backend
echo "[1/2] Starting backend on http://localhost:8000 ..."
cd "$SCRIPT_DIR"
nohup uvicorn backend.main:app --host 0.0.0.0 --port 8000 > /tmp/polybot-backend.log 2>&1 &
BACKEND_PID=$!
sleep 2

if curl -s http://localhost:8000/api/health > /dev/null 2>&1; then
    echo "  Backend OK (PID $BACKEND_PID)"
else
    echo "  ERROR: Backend failed to start. Check /tmp/polybot-backend.log"
    exit 1
fi

# Start frontend
echo "[2/2] Starting frontend on http://localhost:3000 ..."
cd "$SCRIPT_DIR/frontend"
nohup npm run dev > /tmp/polybot-frontend.log 2>&1 &
FRONTEND_PID=$!
sleep 5

if curl -s -o /dev/null -w "" http://localhost:3000 2>/dev/null; then
    echo "  Frontend OK (PID $FRONTEND_PID)"
else
    echo "  Frontend starting... (PID $FRONTEND_PID)"
fi

echo ""
echo "==========================================="
echo "  Dashboard: http://localhost:3000"
echo "  API:       http://localhost:8000/api"
echo "==========================================="
echo ""
echo "Logs: /tmp/polybot-backend.log, /tmp/polybot-frontend.log"
echo "Stop: pkill -f uvicorn; pkill -f 'next dev'"
