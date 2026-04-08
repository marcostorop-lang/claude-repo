#!/bin/bash
echo "============================================"
echo "  PolyBot - Polymarket Trading Bot Setup"
echo "============================================"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] Python3 not found. Install Python 3.10+"
    exit 1
fi

# Check Node.js
if ! command -v node &> /dev/null; then
    echo "[ERROR] Node.js not found. Install Node.js 18+"
    exit 1
fi

echo "[1/4] Installing Python dependencies..."
pip3 install -r requirements.txt

echo "[2/4] Installing dashboard dependencies..."
cd dashboard/frontend && npm install && cd ../..

echo "[3/4] Creating .env file..."
if [ ! -f .env ]; then
    cat > .env << 'ENVEOF'
TRADING_MODE=paper
ALLOW_LIVE_TRADING=false
POLL_INTERVAL_SECONDS=30
STRATEGY=simple_momentum
MAX_POSITION_SIZE=50
MAX_TOTAL_EXPOSURE=500
STOP_LOSS_PCT=0.10
TAKE_PROFIT_PCT=0.15
MAX_OPEN_POSITIONS=10
LOG_LEVEL=INFO
SQLITE_DB_PATH=polymarket_bot.db
ENVEOF
    echo "[OK] .env created with paper trading config."
else
    echo "[OK] .env already exists."
fi

echo "[4/4] Downloading markets from Polymarket..."
python3 -m src.main backfill-markets

echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
echo "  Run the bot:        ./run_bot.sh"
echo "  Update dashboard:   ./update_dashboard.sh"
echo "============================================"
