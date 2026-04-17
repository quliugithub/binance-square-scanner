#!/bin/bash
set -e
cd /root/.openclaw/workspace

# Full unattended mode depends on a fresh browser-side seed refresh when Binance WAF blocks plain HTTP sitemap fetches.
# Refresh seed file first when needed:
#   node binance_square_browser_seed_refresh.js 50 1 5
# Then run scrape normally:
#   ./binance_square_scraper.sh --limit 50 --format csv

python3 binance_square_tool.py scrape "$@"
