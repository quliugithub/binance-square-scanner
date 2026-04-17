#!/bin/bash
set -e
cd /root/.openclaw/workspace
python3 binance_square_tool.py scrape "$@"
