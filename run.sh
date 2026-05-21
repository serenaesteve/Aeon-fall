#!/bin/bash
cd "$(dirname "$0")"
pip3 install -r requirements.txt --break-system-packages -q
echo ""
echo "  ╔══════════════════════════════╗"
echo "  ║     AEON FALL v2             ║"
echo "  ║  http://localhost:5001       ║"
echo "  ╚══════════════════════════════╝"
echo ""
python3 app.py
