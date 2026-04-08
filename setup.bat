@echo off
echo ============================================
echo   PolyBot - Polymarket Trading Bot Setup
echo ============================================
echo.

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python no encontrado. Instala Python 3.10+ desde https://python.org
    pause
    exit /b 1
)

REM Check Node.js
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Node.js no encontrado. Instala Node.js 18+ desde https://nodejs.org
    pause
    exit /b 1
)

echo [1/4] Instalando dependencias de Python...
pip install -r requirements.txt

echo [2/4] Instalando dependencias del dashboard...
cd dashboard\frontend
call npm install
cd ..\..

echo [3/4] Creando archivo .env...
if not exist .env (
    echo TRADING_MODE=paper> .env
    echo ALLOW_LIVE_TRADING=false>> .env
    echo POLL_INTERVAL_SECONDS=30>> .env
    echo STRATEGY=simple_momentum>> .env
    echo MAX_POSITION_SIZE=50>> .env
    echo MAX_TOTAL_EXPOSURE=500>> .env
    echo STOP_LOSS_PCT=0.10>> .env
    echo TAKE_PROFIT_PCT=0.15>> .env
    echo MAX_OPEN_POSITIONS=10>> .env
    echo LOG_LEVEL=INFO>> .env
    echo SQLITE_DB_PATH=polymarket_bot.db>> .env
    echo.
    echo [OK] Archivo .env creado con configuracion paper trading.
) else (
    echo [OK] Archivo .env ya existe.
)

echo [4/4] Descargando mercados de Polymarket...
python -m src.main backfill-markets

echo.
echo ============================================
echo   Setup completado!
echo.
echo   Para ejecutar el bot:
echo     run_bot.bat
echo.
echo   Para generar el dashboard:
echo     update_dashboard.bat
echo ============================================
pause
