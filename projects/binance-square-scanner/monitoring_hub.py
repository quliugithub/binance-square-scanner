#!/usr/bin/env python3
import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import hotspot_radar
import narrative_monitor
import onchain_strong_buy_monitor

RESULTS_DIR = CURRENT_DIR / 'results'
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def build_hub_payload(limit: int, start_page: int, max_pages: int, top_n: int, onchain_limit: int) -> Dict[str, Any]:
    hotspot = hotspot_radar.build_hotspot_payload(limit=limit, start_page=start_page, max_pages=max_pages, top_n=top_n)

    raw_posts = narrative_monitor.scrape_square(limit=limit, start_page=start_page, max_pages=max_pages)
    narrative_posts = narrative_monitor.build_narrative_posts(raw_posts)
    narrative_rank = narrative_monitor.aggregate_narratives(narrative_posts)
    symbol_narrative_view = narrative_monitor.build_symbol_narrative_view(narrative_posts)
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

    onchain = onchain_strong_buy_monitor.build_monitor_payload(limit=onchain_limit, top_n=top_n)

    system_summary = {
        'trend_candidates': len(hotspot.get('top_opportunities') or []),
        'buy_candidates': len(hotspot.get('buy_candidates') or []),
        'strong_capital_buy_candidates': len(hotspot.get('strong_capital_buy_candidates') or []),
        'top_narratives': len(narrative_payload.get('top_narratives') or []),
        'onchain_strong_buy_candidates': len(onchain.get('strong_buy_candidates') or []),
        'onchain_observation_candidates': len(onchain.get('observation_candidates') or []),
    }

    focus_board = {
        'strong_capital_buy_candidates': hotspot.get('strong_capital_buy_candidates') or [],
        'buy_candidates': hotspot.get('buy_candidates') or [],
        'top_narratives': narrative_payload.get('top_narratives') or [],
        'onchain_strong_buy_candidates': onchain.get('strong_buy_candidates') or [],
        'onchain_observation_candidates': onchain.get('observation_candidates') or [],
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
    parser.add_argument('--output')
    args = parser.parse_args()

    payload = build_hub_payload(
        limit=args.limit,
        start_page=args.start_page,
        max_pages=args.max_pages,
        top_n=args.top_n,
        onchain_limit=args.onchain_limit,
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
