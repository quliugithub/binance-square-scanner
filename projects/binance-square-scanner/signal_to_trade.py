#!/usr/bin/env python3
"""signal_to_trade — 将 Monitoring Hub 信号转化为交易建议。

流程：Hub 信号 → 多维评分 → 交易建议 → 用户确认 → 调用 coin-trade-executor

用法：
  python3 signal_to_trade.py              # 生成建议（不执行）
  python3 signal_to_trade.py --execute    # 生成建议并执行（需确认）
  python3 signal_to_trade.py --auto       # 自动执行高置信度信号（危险）
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── 路径配置 ──────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent
TRADE_EXECUTOR = Path(os.path.expanduser(
    "~/.openclaw/workspace/skills/coin-trade-executor/scripts/coin_trade_exec.py"
))

# ── 默认参数 ──────────────────────────────────────────────
DEFAULT_STAKE_USD = 50.0
DEFAULT_LEVERAGE = 2
MAX_LEVERAGE = 5
MIN_SCORE_TO_SUGGEST = 55
MIN_SCORE_TO_AUTO = 85


def _run_monitoring_hub() -> Dict[str, Any]:
    """运行 Monitoring Hub 各模块，汇总信号。"""
    # Add project to path
    if str(PROJECT_DIR) not in sys.path:
        sys.path.insert(0, str(PROJECT_DIR))

    from bsc.data.tool import scrape_square
    from bsc.data.blockbeats import fetch_blockbeats_news, to_narrative_format as bb_fmt
    from bsc.data.defillama import fetch_protocols, detect_tvl_changes, to_narrative_format as dl_fmt
    from bsc.data.twitter_kol import fetch_all_news, to_narrative_format as news_fmt
    from bsc.monitors.narrative import (
        build_narrative_posts, aggregate_narratives, build_symbol_narrative_view,
        enrich_post_symbols,
    )
    from bsc.monitors.funding_rate import build_monitor_payload as build_futures_payload
    from bsc.monitors.onchain import build_monitor_payload as build_onchain_payload

    signals: Dict[str, Any] = {}

    # 1. Narrative
    all_raw = []
    try:
        bs = scrape_square(limit=30)
        all_raw.extend(bs)
    except Exception:
        pass
    try:
        bb = bb_fmt(fetch_blockbeats_news(25))
        enrich_post_symbols(bb)
        all_raw.extend(bb)
    except Exception:
        pass
    try:
        protocols = fetch_protocols()
        tvl = dl_fmt(detect_tvl_changes(protocols, change_threshold_pct=15))
        all_raw.extend(tvl)
    except Exception:
        pass
    try:
        news = news_fmt(fetch_all_news())
        enrich_post_symbols(news)
        all_raw.extend(news)
    except Exception:
        pass
    try:
        from bsc.data.twitter_6551 import fetch_kol_tweets, fetch_crypto_search, to_narrative_format as tw6551_fmt
        tw_kol = fetch_kol_tweets(max_profiles=5, tweets_per_profile=8)
        tw_search = fetch_crypto_search(min_likes=20, max_results=10)
        tw_posts = tw6551_fmt(tw_kol + tw_search)
        enrich_post_symbols(tw_posts)
        all_raw.extend(tw_posts)
    except Exception:
        pass

    narrative_posts = build_narrative_posts(all_raw)
    narrative_rank = aggregate_narratives(narrative_posts)
    symbol_view = build_symbol_narrative_view(narrative_posts)
    signals['narrative'] = {
        'rank': narrative_rank,
        'symbol_view': symbol_view,
        'total_posts': len(all_raw),
    }

    # 2. Futures
    try:
        futures = build_futures_payload(symbols=['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'OPUSDT'])
        signals['futures'] = {r['symbol']: r for r in futures.get('results', []) if not r.get('error')}
    except Exception:
        signals['futures'] = {}

    # 3. On-chain
    try:
        onchain = build_onchain_payload(limit=15, top_n=10)
        signals['onchain'] = onchain
    except Exception:
        signals['onchain'] = {}

    return signals


def _compute_symbol_score(symbol: str, signals: Dict[str, Any]) -> Dict[str, Any]:
    """计算单个币种的综合评分和建议。"""
    score = 50  # baseline
    reasons: List[str] = []
    risks: List[str] = []

    sym_pair = symbol.upper() if symbol.endswith('USDT') else f"{symbol.upper()}USDT"
    sym_base = symbol.upper().replace('USDT', '')

    # ── 叙事热度 ──
    narrative = signals.get('narrative', {})
    symbol_view = narrative.get('symbol_view', [])
    sv = next((s for s in symbol_view if s.get('symbol') == sym_pair), None)
    if sv:
        post_count = sv.get('post_count', 0)
        narratives = sv.get('narratives', [])
        stage = sv.get('dominant_stage', '')
        if post_count >= 10:
            score += 15
            reasons.append(f'叙事热度高 ({post_count}帖, {",".join(narratives[:2])})')
        elif post_count >= 5:
            score += 8
            reasons.append(f'叙事活跃 ({post_count}帖)')
        if stage == '中期扩散':
            score += 5
        elif stage == '末端过热':
            score -= 15
            risks.append('叙事末端过热')

    # ── 合约资金面 ──
    futures = signals.get('futures', {})
    ft = futures.get(sym_pair, {})
    if ft and not ft.get('error'):
        comp = ft.get('composite', {})
        funding = ft.get('funding', {})
        funding_rate = funding.get('funding_rate', 0)
        f_bias = comp.get('capital_flow_bias', 'neutral')
        f_score = comp.get('capital_flow_score', 50)

        if f_bias == 'bullish':
            score += 10
            reasons.append(f'资金面偏多 (score={f_score})')
        elif f_bias == 'bearish':
            score -= 10
            risks.append(f'资金面偏空 (score={f_score})')

        if funding_rate and funding_rate < -0.0003:
            score += 5
            reasons.append(f'负费率={funding.get("funding_rate_pct", "?")}（多头占优）')
        elif funding_rate and funding_rate > 0.001:
            score -= 5
            risks.append(f'正费率偏高={funding.get("funding_rate_pct", "?")}')

    # ── 链上强买盘 ──
    onchain = signals.get('onchain', {})
    all_candidates = (onchain.get('strong_buy_candidates', []) +
                      onchain.get('observation_candidates', []) +
                      onchain.get('all_candidates', []))
    oc = next((c for c in all_candidates if (c.get('symbol') or '').upper() == sym_base), None)
    if oc:
        buy_score = oc.get('onchain_buy_score', 0)
        if buy_score >= 60:
            score += 12
            reasons.append(f'链上强买盘 (score={buy_score})')
        elif buy_score >= 40:
            score += 5
            reasons.append(f'链上买盘活跃 (score={buy_score})')
        if oc.get('tvl_change_1d_pct') and oc['tvl_change_1d_pct'] > 10:
            score += 5
            reasons.append(f'TVL流入 {oc["tvl_change_1d_pct"]:.1f}%')
        if oc.get('whale_net_usd') and oc['whale_net_usd'] > 10_000_000:
            score += 5
            reasons.append(f'巨鲸净提币 ${oc["whale_net_usd"]/1e6:.0f}M')

    # ── 宏观催化 ──
    # (simplified: skip per-symbol macro for now)

    score = max(0, min(100, score))

    # 生成建议
    if score >= 75:
        suggestion = '做多'
        confidence = '高'
    elif score >= 60:
        suggestion = '做多'
        confidence = '中'
    elif score >= 45:
        suggestion = '观望'
        confidence = '低'
    else:
        suggestion = '观望/做空'
        confidence = '低'

    return {
        'symbol': sym_pair,
        'symbol_base': sym_base,
        'score': round(score, 1),
        'suggestion': suggestion,
        'confidence': confidence,
        'reasons': reasons,
        'risks': risks,
    }


def generate_recommendations(
    signals: Optional[Dict[str, Any]] = None,
    min_score: int = MIN_SCORE_TO_SUGGEST,
    top_n: int = 5,
) -> List[Dict[str, Any]]:
    """生成交易建议列表。"""
    if signals is None:
        signals = _run_monitoring_hub()

    # Collect candidate symbols from all sources
    candidates = set()

    # From narrative symbol view
    for sv in signals.get('narrative', {}).get('symbol_view', []):
        sym = sv.get('symbol', '')
        if sym and sym != '-USDT':
            candidates.add(sym)

    # From on-chain
    for c in (signals.get('onchain', {}).get('strong_buy_candidates', []) +
              signals.get('onchain', {}).get('observation_candidates', [])):
        sym = c.get('symbol', '')
        if sym:
            candidates.add(f"{sym}USDT")

    # From futures
    candidates.update(signals.get('futures', {}).keys())

    # Score all candidates
    recommendations = []
    for sym in candidates:
        result = _compute_symbol_score(sym, signals)
        if result['score'] >= min_score:
            recommendations.append(result)

    recommendations.sort(key=lambda x: x['score'], reverse=True)
    return recommendations[:top_n]


def format_recommendation(rec: Dict[str, Any], stake: float, leverage: int) -> str:
    """格式化单个交易建议为可读文本。"""
    emoji = '🟢' if rec['suggestion'] == '做多' else '🔴' if rec['suggestion'] == '做空' else '⚪'
    lines = [
        f"{emoji} **{rec['symbol']}** — {rec['suggestion']} (置信度: {rec['confidence']})",
        f"  综合评分: **{rec['score']}/100**",
    ]
    if rec['reasons']:
        lines.append(f"  ✅ 利好:")
        for r in rec['reasons']:
            lines.append(f"     {r}")
    if rec['risks']:
        lines.append(f"  ⚠️ 风险:")
        for r in rec['risks']:
            lines.append(f"     {r}")
    if rec['suggestion'] == '做多':
        lines.append(f"  📋 建议: stake {stake}U, {leverage}x 杠杆, 做多")
    return '\n'.join(lines)


def build_trade_command(rec: Dict[str, Any], stake: float, leverage: int) -> List[str]:
    """构建 coin-trade-executor 命令。"""
    side = 'long' if rec['suggestion'] == '做多' else 'short'
    return [
        'python3', str(TRADE_EXECUTOR),
        'open',
        '--mode', 'futures',
        '--pair', rec['symbol'].replace('USDT', '/USDT:USDT'),
        '--side', side,
        '--stake', str(stake),
        '--leverage', str(leverage),
        '--pretty',
    ]


def execute_trade(rec: Dict[str, Any], stake: float, leverage: int) -> Dict[str, Any]:
    """执行交易。"""
    cmd = build_trade_command(rec, stake, leverage)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return {
            'symbol': rec['symbol'],
            'command': ' '.join(cmd),
            'returncode': result.returncode,
            'stdout': result.stdout,
            'stderr': result.stderr,
        }
    except Exception as exc:
        return {'symbol': rec['symbol'], 'error': str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description='信号→交易建议生成器')
    parser.add_argument('--execute', action='store_true', help='生成建议后执行（需确认）')
    parser.add_argument('--auto', action='store_true', help='自动执行高置信度信号（危险）')
    parser.add_argument('--stake', type=float, default=DEFAULT_STAKE_USD)
    parser.add_argument('--leverage', type=int, default=DEFAULT_LEVERAGE)
    parser.add_argument('--min-score', type=float, default=MIN_SCORE_TO_SUGGEST)
    parser.add_argument('--top-n', type=int, default=5)
    args = parser.parse_args()

    if args.leverage > MAX_LEVERAGE:
        print(f"⚠️ 杠杆超过最大限制 {MAX_LEVERAGE}x，已调整")
        args.leverage = MAX_LEVERAGE

    print("🔄 正在运行 Monitoring Hub...")
    signals = _run_monitoring_hub()

    print("📊 正在生成交易建议...\n")
    recs = generate_recommendations(signals, min_score=int(args.min_score), top_n=args.top_n)

    if not recs:
        print("❌ 没有达到最低评分的交易建议")
        print(f"   最低评分要求: {args.min_score}")
        return

    print(f"{'='*50}")
    print(f"📊 交易建议 ({len(recs)} 个)")
    print(f"{'='*50}\n")

    for i, rec in enumerate(recs, 1):
        print(f"[{i}] {format_recommendation(rec, args.stake, args.leverage)}")
        print()

    # Output as JSON for programmatic use
    output = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'config': {
            'stake': args.stake,
            'leverage': args.leverage,
            'min_score': args.min_score,
        },
        'recommendations': recs,
    }
    out_path = PROJECT_DIR / 'results' / f'signal_recommendations_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"💾 保存到: {out_path}")

    # Execute if requested
    if args.execute or args.auto:
        for rec in recs:
            if args.auto and rec['score'] < MIN_SCORE_TO_AUTO:
                print(f"⏭️ 跳过 {rec['symbol']} (score={rec['score']} < {MIN_SCORE_TO_AUTO})")
                continue
            print(f"\n🚀 执行: {rec['symbol']} {rec['suggestion']}...")
            result = execute_trade(rec, args.stake, args.leverage)
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
