#!/usr/bin/env python3
import json
from pathlib import Path

src = Path('/root/.openclaw/workspace/results/binance_square_posts_20260418_074453.json')
out = Path('/root/.openclaw/workspace/results/binance_square_replay_sample.json')
rows = json.loads(src.read_text(encoding='utf-8'))
out.write_text(json.dumps(rows[:12], ensure_ascii=False, indent=2), encoding='utf-8')
print(str(out))
