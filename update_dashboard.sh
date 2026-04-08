#!/bin/bash
echo "============================================"
echo "  PolyBot - Generating Dashboard"
echo "============================================"
echo ""
node generate_dashboard.js
if [ $? -eq 0 ]; then
    echo ""
    echo "[OK] Dashboard generated at docs/dashboard.html"
    echo "Open docs/dashboard.html in your browser to view it."
    # Try to open in default browser
    if command -v xdg-open &> /dev/null; then xdg-open docs/dashboard.html
    elif command -v open &> /dev/null; then open docs/dashboard.html
    fi
fi
