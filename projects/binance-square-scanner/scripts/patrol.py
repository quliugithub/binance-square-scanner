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


MAJOR_COINS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT']


def patrol(threshold: int = 60, force: bool = False) -> dict:
    """Run a patrol scan — 5 major + 5 minor coins with detailed scoring."""
    state = _load_state()
    last_symbols = set(state.get('last_alert_symbols', []))

    # Run analysis
    signals = _run_monitoring_hub()

    # Score ALL candidates (low threshold to get pool)
    all_recs = generate_recommendations(signals, min_score=0, top_n=100)

    # Split into major and minor
    major_recs = [r for r in all_recs if r['symbol'] in MAJOR_COINS]
    minor_recs = [r for r in all_recs if r['symbol'] not in MAJOR_COINS]

    # Take top 5 from each, then combine
    recs = major_recs[:5]

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
    """Format patrol result as a detailed user-facing message."""
    recs = result.get('recommendations', [])
    new_syms = result.get('new_symbols', [])

    if not recs:
        return "📊 巡检完成，暂无达到阈值的交易机会。"

    lines = ["🔔 **信号巡检报告**", ""]

    # ── 汇总表格 ──
    lines.append("📋 **汇总一览**:")
    for i, r in enumerate(recs[:10], 1):
        emoji = '🟢' if r['suggestion'] == '做多' else '🔴' if r['suggestion'] == '做空' else '⚪'
        new_tag = ' 🆕' if r['symbol'] in new_syms else ''
        s = r.get('score', 0)
        bd = r.get('score_breakdown', {})
        fut = bd.get('💰 合约资金面', {}).get('points', 0)
        nar = bd.get('📡 叙事热度', {}).get('points', 0)
        chn = bd.get('⛓️ 链上信号', {}).get('points', 0)
        tch = bd.get('📈 技术指标', {}).get('points', 0)
        cvg = r.get('data_coverage', '?')
        def _fmt(v):
            return f'+{v}' if v > 0 else str(v)
        lines.append(f"  {i}. {emoji} {r['symbol']:10s} | 合约{_fmt(fut):>4s} | 叙事{_fmt(nar):>4s} | 链上{_fmt(chn):>4s} | 技术{_fmt(tch):>4s} | **{s}分** | {cvg}/4{new_tag}")
    lines.append("")
    lines.append("—" * 40)
    lines.append("")

    # ── 详细拆解（只展开做多/做空） ──
    action_recs = [r for r in recs[:5] if r['suggestion'] in ('做多', '做空')]
    if action_recs:
        lines.append("""━━━ 详细拆解 ━━━""")
        lines.append("")
    for i, r in enumerate(action_recs, 1):
        emoji = '🟢' if r['suggestion'] == '做多' else '🔴' if r['suggestion'] == '做空' else '⚪'
        # 找到在原列表中的序号
        orig_idx = recs.index(r) + 1
        new_tag = ' 🆕' if r['symbol'] in new_syms else ''
        score = r.get('score', 0)
        confidence = r.get('confidence', '?')
        coverage = r.get('data_coverage', '?')
        breakdown = r.get('score_breakdown', {})

        # Header
        lines.append(f"━━━ {orig_idx}. **{r['symbol']}** — {r['suggestion']} ({score}分) 置信度:{confidence} 覆盖:{coverage}/4{new_tag} ━━━")
        lines.append("")

        # Score breakdown - all dimensions with sub_items
        lines.append(f"📊 评分拆解（基准50分 + 各维度）:")
        dim_order = ['💰 合约资金面', '📡 叙事热度', '⛓️ 链上信号', '📈 技术指标', '数据覆盖']
        for dim in dim_order:
            if dim in breakdown:
                info = breakdown[dim]
                pts = info.get('points', 0)
                sign = '+' if pts > 0 else ('' if pts <= 0 else '')
                lines.append(f"  · {dim}: {sign}{pts}分")
                sub_items = info.get('sub_items', [])
                if sub_items:
                    for item in sub_items:
                        lines.append(f"    - {item}")
                else:
                    detail = info.get('detail', '')
                    if detail:
                        lines.append(f"    - {detail}")
        # 其他维度
        for dim, info in breakdown.items():
            if dim not in dim_order:
                pts = info.get('points', 0)
                sign = '+' if pts > 0 else ''
                lines.append(f"  · {dim}: {sign}{pts}分")
                sub_items = info.get('sub_items', [])
                for item in sub_items:
                    lines.append(f"    - {item}")
        lines.append(f"  ➤ 总分: {score}分")
        lines.append("")

        # Reasons
        if r.get('reasons'):
            lines.append("✅ 看多理由:")
            for reason in r['reasons']:
                lines.append(f"  · {reason}")

        # Risks
        if r.get('risks'):
            lines.append("⚠️ 风险提示:")
            for risk in r['risks']:
                lines.append(f"  · {risk}")

        lines.append("")

    if new_syms:
        lines.append(f"🆕 新出现: {', '.join(new_syms)}")
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
