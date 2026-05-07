@echo off
echo ============================================
echo   PolyBot - Generando Dashboard (DEPRECATED)
echo ============================================
echo.
echo AVISO: este generador estatico esta deprecado.
echo Preferir el backend FastAPI:
echo     cd dashboard\backend ^&^& uvicorn main:app --reload
echo     REST  /api/*       Prometheus  /metrics
echo.
node generate_dashboard.js
if %errorlevel% equ 0 (
    echo.
    echo [OK] Dashboard generado en docs\dashboard.html
    echo Abriendo en el navegador...
    start "" "docs\dashboard.html"
) else (
    echo [ERROR] No se pudo generar el dashboard.
    echo Asegurate de que el bot ha ejecutado al menos un ciclo.
)
pause
