#!/usr/bin/env python3
"""巡检脚本 — 定时运行信号分析，发现新机会时输出告警。

用法：
  python3 patrol.py              # 正常巡检
  python3 patrol.py --force      # 强制推送（不管是否有新机会）
  python3 patrol.py --threshold 70  # 提高阈值

输出 JSON 格式：
  {"alert": true/false, "recommendations": [...], "new_symbols": [...]}
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project to path
PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from signal_to_trade import _run_monitoring_hub, generate_recommendations

RESULTS_DIR = PROJECT_DIR / 'results'
STATE_FILE = RESULTS_DIR / 'patrol_state.json'


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {"last_alert_symbols": [], "last_run": None}


def _save_state(state: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def patrol(threshold: int = 60, force: bool = False) -> dict:
    """Run a patrol scan and detect new opportunities."""
    state = _load_state()
    last_symbols = set(state.get('last_alert_symbols', []))

    # Run analysis
    signals = _run_monitoring_hub()
    recs = generate_recommendations(signals, min_score=threshold, top_n=8)

    current_symbols = {r['symbol'] for r in recs}
    new_symbols = list(current_symbols - last_symbols)

    # Determine if we should alert
    should_alert = force or bool(new_symbols) or not last_symbols

    result = {
        'alert': should_alert,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'recommendations': recs,
        'new_symbols': new_symbols,
        'total_opportunities': len(recs),
        'threshold': threshold,
    }

    # Save state
    state['last_alert_symbols'] = list(current_symbols)
    state['last_run'] = result['generated_at']
    state['last_recommendations'] = [
        {'symbol': r['symbol'], 'score': r['score'], 'suggestion': r['suggestion']}
        for r in recs
    ]
    _save_state(state)

    return result


def format_alert(result: dict) -> str:
    """Format patrol result as a user-facing message."""
    recs = result.get('recommendations', [])
    new_syms = result.get('new_symbols', [])

    if not recs:
        return "📊 巡检完成，暂无达到阈值的交易机会。"

    lines = ["🔔 **信号巡检报告**", ""]

    for i, r in enumerate(recs[:5], 1):
        emoji = '🟢' if r['suggestion'] == '做多' else '🔴' if r['suggestion'] == '做空' else '⚪'
        new_tag = ' 🆕' if r['symbol'] in new_syms else ''
        lines.append(f"{i}. {emoji} **{r['symbol']}** — {r['suggestion']} ({r['score']}分){new_tag}")
        for reason in r.get('reasons', [])[:2]:
            lines.append(f"   ✅ {reason}")
        if r.get('risks'):
            for risk in r['risks'][:1]:
                lines.append(f"   ⚠️ {risk}")

    lines.append("")
    if new_syms:
        lines.append(f"🆕 新发现: {', '.join(new_syms)}")
    lines.append("")
    lines.append("回复 **确认开仓 [序号]** 执行，或 **全部开仓** 一键执行。")

    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='信号巡检')
    parser.add_argument('--threshold', type=int, default=60)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    result = patrol(threshold=args.threshold, force=args.force)
    print(format_alert(result))
    print("\n---JSON---")
    print(json.dumps(result, ensure_ascii=False, indent=2))
