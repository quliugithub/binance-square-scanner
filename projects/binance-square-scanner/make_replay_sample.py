#!/usr/bin/env python3
import json
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = CURRENT_DIR.parent.parent
src = WORKSPACE_DIR / 'results' / 'binance_square_posts_20260417_100900.json'
out = CURRENT_DIR / 'samples' / 'binance_square_replay_sample.json'
rows = json.loads(src.read_text(encoding='utf-8'))
out.write_text(json.dumps(rows[:12], ensure_ascii=False, indent=2), encoding='utf-8')
print(str(out))
