#!/bin/bash
# Install deps and run the scraper app
cd "$(dirname "$0")"
pip install -r requirements.txt --break-system-packages -q 2>/dev/null || pip install -r requirements.txt -q
echo ""
echo "========================================="
echo "  Site Scraper running at:"
echo "  http://localhost:5000"
echo "========================================="
echo ""
python app.py
