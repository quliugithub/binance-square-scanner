#!/usr/bin/env python3
"""Monitoring Hub — 统一监控总入口（薄包装层，核心逻辑在 bsc 包中）"""
import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict

from bsc.constants import RESULTS_DIR
from bsc.monitors.hotspot import build_hotspot_payload
from bsc.monitors.narrative import (
    scrape_square, build_narrative_posts, aggregate_narratives, build_symbol_narrative_view,
)
from bsc.monitors.onchain import build_monitor_payload as build_onchain_payload
from bsc.monitors.funding_rate import build_monitor_payload as build_futures_payload
from bsc.monitors.macro import build_macro_payload
from bsc.monitors.announcement import build_announcement_payload


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def build_hub_payload(
    limit: int,
    start_page: int,
    max_pages: int,
    top_n: int,
    onchain_limit: int,
    futures_symbols: list[str] | None = None,
) -> Dict[str, Any]:
    # ── 热点雷达 ──
    hotspot = build_hotspot_payload(limit=limit, start_page=start_page, max_pages=max_pages, top_n=top_n)

    # ── 叙事监控 ──
    raw_posts = scrape_square(limit=limit, start_page=start_page, max_pages=max_pages)
    narrative_posts = build_narrative_posts(raw_posts)
    narrative_rank = aggregate_narratives(narrative_posts)
    symbol_narrative_view = build_symbol_narrative_view(narrative_posts)
    narrative_payload = {
        'summary': {
            'fetched_posts': len(raw_posts),
            'tagged_posts': len(narrative_posts),
            'narratives': len(narrative_rank),
            'symbols': len(symbol_narrative_view),
        },
        'top_narratives': narrative_rank[:top_n],
        'symbol_narrative_view': symbol_narrative_view[:top_n],
    }

    # ── 链上强买盘 ──
    onchain = build_onchain_payload(limit=onchain_limit, top_n=top_n)

    # ── 主流币合约监控 ──
    futures_payload = build_futures_payload(symbols=futures_symbols)

    # ── 宏观日历 ──
    macro_payload = build_macro_payload()

    # ── 币安公告监控 ──
    announcement_payload = build_announcement_payload(
        limit=limit, start_page=start_page, max_pages=max_pages,
    )

    system_summary = {
        'trend_candidates': len(hotspot.get('top_opportunities') or []),
        'buy_candidates': len(hotspot.get('buy_candidates') or []),
        'strong_capital_buy_candidates': len(hotspot.get('strong_capital_buy_candidates') or []),
        'top_narratives': len(narrative_payload.get('top_narratives') or []),
        'onchain_strong_buy_candidates': len(onchain.get('strong_buy_candidates') or []),
        'onchain_observation_candidates': len(onchain.get('observation_candidates') or []),
        'futures_symbols': len(futures_payload.get('results') or []),
        'upcoming_events': len(macro_payload.get('upcoming_events') or []),
        'recent_news': len(macro_payload.get('recent_news') or []),
        'announcement_total': (announcement_payload.get('summary') or {}).get('total_announcements', 0),
        'announcement_high_impact': (announcement_payload.get('summary') or {}).get('high_impact', 0),
    }

    focus_board = {
        'strong_capital_buy_candidates': hotspot.get('strong_capital_buy_candidates') or [],
        'buy_candidates': hotspot.get('buy_candidates') or [],
        'top_narratives': narrative_payload.get('top_narratives') or [],
        'onchain_strong_buy_candidates': onchain.get('strong_buy_candidates') or [],
        'onchain_observation_candidates': onchain.get('observation_candidates') or [],
        'high_impact_announcements': announcement_payload.get('high_impact_announcements') or [],
    }

    return {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'monitoring_hub_v1',
            'limit': limit,
            'start_page': start_page,
            'max_pages': max_pages,
            'top_n': top_n,
            'onchain_limit': onchain_limit,
        },
        'system_summary': system_summary,
        'focus_board': focus_board,
        'trend_monitor': {
            'summary': hotspot.get('summary'),
            'strong_capital_buy_candidates': hotspot.get('strong_capital_buy_candidates'),
            'buy_candidates': hotspot.get('buy_candidates'),
            'top_opportunities': hotspot.get('top_opportunities'),
            'timing_ranking': hotspot.get('timing_ranking'),
        },
        'narrative_monitor': narrative_payload,
        'onchain_strong_buy_monitor': onchain,
        'major_coin_futures_monitor': futures_payload,
        'macro_calendar_monitor': macro_payload,
        'announcement_monitor': announcement_payload,
    }


def render_markdown(payload: Dict[str, Any]) -> str:
    lines = []
    lines.append('# Monitoring Hub')
    lines.append('')
    summary = payload.get('system_summary') or {}
    lines.append(f"- trend_candidates: **{summary.get('trend_candidates', 0)}**")
    lines.append(f"- buy_candidates: **{summary.get('buy_candidates', 0)}**")
    lines.append(f"- strong_capital_buy_candidates: **{summary.get('strong_capital_buy_candidates', 0)}**")
    lines.append(f"- top_narratives: **{summary.get('top_narratives', 0)}**")
    lines.append(f"- onchain_strong_buy_candidates: **{summary.get('onchain_strong_buy_candidates', 0)}**")
    lines.append(f"- announcements: **{summary.get('announcement_total', 0)}** (high: {summary.get('announcement_high_impact', 0)})")
    lines.append(f"- futures_symbols: **{summary.get('futures_symbols', 0)}**")
    lines.append(f"- upcoming_events: **{summary.get('upcoming_events', 0)}**")
    lines.append(f"- recent_news: **{summary.get('recent_news', 0)}**")
    lines.append('')

    # ── 公告 & 催化 ──
    ann = payload.get('announcement_monitor') or {}
    if ann.get('announcements'):
        lines.append('## Announcements & Catalysts')
        lines.append('')
        IMPACT_EMOJI = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}
        for a in ann['announcements'][:8]:
            emoji = IMPACT_EMOJI.get(a.get('impact'), '⚪')
            sym_str = ', '.join(a.get('symbols') or [])
            header = f"### {emoji} {a.get('title', '')}"
            if sym_str:
                header += f" ({sym_str})"
            lines.append(header)
            lines.append(f"- type: `{a.get('type')}` | impact: `{a.get('impact')}`")
            if a.get('web_link'):
                lines.append(f"- link: {a['web_link']}")
            lines.append('')

    # ── 主流币合约资金面 ──
    futures = payload.get('major_coin_futures_monitor') or {}
    if futures.get('results'):
        lines.append('## Major Coin Futures')
        lines.append('')
        for r in futures['results']:
            if 'error' in r:
                lines.append(f"### {r['symbol']} ❌ {r['error']}")
                continue
            comp = r.get('composite', {})
            score = comp.get('capital_flow_score', 50)
            bias = comp.get('capital_flow_bias', 'neutral')
            emoji = '🟢' if bias == 'bullish' else ('🔴' if bias == 'bearish' else '⚪')
            f = r.get('funding', {})
            oi = r.get('oi_5m', {})
            lines.append(f"### {emoji} {r['symbol']} — Score {score}/100 ({bias})")
            lines.append(f"- funding: {f.get('funding_rate_pct')} (z={f.get('z_score')}) → `{f.get('signal')}`")
            lines.append(f"- oi: {oi.get('oi')} | 1h change: {oi.get('oi_change_pct_1h')}% → `{oi.get('signal')}`")
            if comp.get('key_drivers'):
                lines.append(f"- drivers: {'; '.join(comp['key_drivers'])}")
            if comp.get('risk_factors'):
                lines.append(f"- risks: {'; '.join(comp['risk_factors'])}")
            lines.append(f"- suggestion: {comp.get('action_suggestion')}")
            lines.append('')

    # ── 宏观 & 新闻 ──
    macro = payload.get('macro_calendar_monitor') or {}
    catalyst = macro.get('catalyst', {})
    if macro.get('upcoming_events') or macro.get('recent_news'):
        lines.append('## Macro & News')
        lines.append('')
        lines.append(f"🎯 Catalyst score: {catalyst.get('news_catalyst_score')} ({catalyst.get('news_catalyst_direction')})")
        lines.append('')
        for e in (macro.get('upcoming_events') or [])[:5]:
            emoji = '🔴' if e.get('impact_level') == 'high' else '🟡'
            lines.append(f"- {emoji} {e['event_name']} ({e['days_until']}天后)")
        for n in (macro.get('recent_news') or [])[:5]:
            s = '🟢' if n.get('sentiment') == 'positive' else ('🔴' if n.get('sentiment') == 'negative' else '⚪')
            lines.append(f"- {s} [{n.get('source')}] {n.get('title', '')[:60]}")
        lines.append('')

    lines.append('## Strong capital buy candidates')
    lines.append('')
    for row in payload.get('focus_board', {}).get('strong_capital_buy_candidates') or []:
        lines.append(f"### {row.get('priority_rank')}. {row.get('symbol')}")
        lines.append(f"- final_score: `{row.get('final_score')}`")
        lines.append(f"- stage: `{row.get('stage')}`")
        lines.append(f"- reason: {row.get('reason_summary')}")
        lines.append('')

    lines.append('## Top narratives')
    lines.append('')
    for row in payload.get('focus_board', {}).get('top_narratives') or []:
        lines.append(f"### {row.get('narrative_rank')}. {row.get('narrative')}")
        lines.append(f"- stage: `{row.get('dominant_stage')}`")
        lines.append(f"- sentiment: `{row.get('dominant_sentiment')}`")
        lines.append(f"- top_symbols: `{', '.join(row.get('top_symbols') or [])}`")
        lines.append('')

    lines.append('## On-chain strong buy / observation')
    lines.append('')
    for row in payload.get('focus_board', {}).get('onchain_strong_buy_candidates') or []:
        lines.append(f"### {row.get('rank')}. {row.get('symbol')} [强买盘候选]")
        lines.append(f"- buy_score: `{row.get('onchain_buy_score')}`")
        lines.append(f"- reason: {row.get('reason_summary')}")
        lines.append('')
    for row in payload.get('focus_board', {}).get('onchain_observation_candidates') or []:
        lines.append(f"### {row.get('rank')}. {row.get('symbol')} [观察池]")
        lines.append(f"- buy_score: `{row.get('onchain_buy_score')}`")
        lines.append(f"- reason: {row.get('reason_summary')}")
        lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


def save_payload(payload: Dict[str, Any], output: str | None) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'monitoring_hub_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    path.with_suffix('.md').write_text(render_markdown(payload), encoding='utf-8')
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='统一监控总入口')
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--max-pages', type=int, default=4)
    parser.add_argument('--top-n', type=int, default=8)
    parser.add_argument('--onchain-limit', type=int, default=15)
    parser.add_argument('--futures-symbols', nargs='+', default=None)
    parser.add_argument('--output')
    args = parser.parse_args()

    payload = build_hub_payload(
        limit=args.limit,
        start_page=args.start_page,
        max_pages=args.max_pages,
        top_n=args.top_n,
        onchain_limit=args.onchain_limit,
        futures_symbols=args.futures_symbols,
    )
    out = save_payload(payload, args.output)
    print(out)
    print(json.dumps({
        'system_summary': payload['system_summary'],
        'strong_capital_buy_candidates': payload['focus_board']['strong_capital_buy_candidates'][:5],
        'top_narratives': payload['focus_board']['top_narratives'][:5],
        'onchain_observation_candidates': payload['focus_board']['onchain_observation_candidates'][:5],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
