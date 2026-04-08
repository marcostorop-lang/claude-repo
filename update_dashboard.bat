@echo off
echo ============================================
echo   PolyBot - Generando Dashboard
echo ============================================
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
