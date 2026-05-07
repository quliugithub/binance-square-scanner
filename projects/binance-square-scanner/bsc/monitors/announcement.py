#!/usr/bin/env python3
import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


from bsc.data.tool import scrape_square

from bsc.constants import RESULTS_DIR


# ── 官方账号识别 ──────────────────────────────────────────────
OFFICIAL_MARKERS = [
    'binance', 'binanceofficial', 'binance exchange',
    'binance futures', 'binance wallet', 'binance nft',
]

# ── 公告分类规则（按优先级排序） ──────────────────────────────
ANNOUNCEMENT_RULES: List[Tuple[str, List[str], str]] = [
    ('new_listing', [
        'will list', 'adds', 'new listing', 'will open trading',
        'lists', 'will add', 'new tradable', 'will be listed',
    ], 'high'),
    ('futures_listing', [
        'futures will launch', 'perpetual', 'delivery futures',
        'coin-m futures', 'usdⓢ-m futures', 'futures will list',
        'will launch.*futures', 'futures trading',
    ], 'high'),
    ('launchpool', [
        'launchpool', 'megadrop', 'launchpad',
    ], 'high'),
    ('delisting', [
        'delist', 'remove', 'cease trading', 'trading pair removal',
        'will delist', 'will remove',
    ], 'high'),
    ('deposit_withdrawal', [
        'deposit', 'withdrawal', 'suspend', 'resume',
        'network upgrade', 'maintenance', 'mainnet swap',
        'will support', 'integration',
    ], 'medium'),
    ('trading_comp', [
        'trading competition', 'rewards', 'airdrop', 'bonus',
        'challenge', 'giveaway', 'earn', 'pool',
    ], 'low'),
]

SYMBOL_PATTERN = re.compile(r'\b([A-Z0-9]{2,20}USDT?)\b')


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def is_official_author(post: Dict[str, Any]) -> bool:
    username = (post.get('username') or '').lower()
    display = (post.get('author') or '').lower()
    for marker in OFFICIAL_MARKERS:
        if marker in username or marker in display:
            return True
    return False


def classify_announcement(text: str) -> Tuple[str, str]:
    text_lower = text.lower()
    for ann_type, keywords, impact in ANNOUNCEMENT_RULES:
        for kw in keywords:
            if '.*' in kw:
                if re.search(kw, text_lower):
                    return ann_type, impact
            elif kw in text_lower:
                return ann_type, impact
    return 'general', 'low'


def extract_symbols(post: Dict[str, Any], body: str) -> List[str]:
    # 优先用帖子自带的 trading_pairs
    pairs = [str(s).upper() for s in (post.get('trading_pairs') or []) if s]
    if pairs:
        return list(dict.fromkeys(pairs))
    # 回退到正文正则匹配
    found = SYMBOL_PATTERN.findall(body.upper())
    return list(dict.fromkeys(found))


def build_announcement_posts(raw_posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    announcements: List[Dict[str, Any]] = []
    for post in raw_posts:
        if post.get('error'):
            continue
        if not is_official_author(post):
            continue
        body = post.get('body') or ''
        title = body.replace('\n', ' ').strip()[:200]
        ann_type, impact = classify_announcement(body)
        symbols = extract_symbols(post, body)

        announcements.append({
            'type': ann_type,
            'impact': impact,
            'title': title,
            'symbols': symbols,
            'publish_time': post.get('publish_time'),
            'author': post.get('author'),
            'username': post.get('username'),
            'content_id': post.get('content_id'),
            'web_link': post.get('web_link'),
            'body_preview': body.replace('\n', ' ')[:300],
            'engagement_total': post.get('engagement_total') or 0,
        })

    # 按影响强度 + 时间排序
    impact_order = {'high': 0, 'medium': 1, 'low': 2}
    announcements.sort(key=lambda x: (impact_order.get(x['impact'], 9), -(x.get('engagement_total') or 0)))
    return announcements


IMPACT_EMOJI = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}


def render_markdown(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append('# Binance Announcement Monitor V1')
    lines.append('')
    summary = payload.get('summary') or {}
    lines.append(f"- total_announcements: **{summary.get('total_announcements', 0)}**")
    lines.append(f"- high_impact: **{summary.get('high_impact', 0)}**")
    lines.append(f"- medium_impact: **{summary.get('medium_impact', 0)}**")
    lines.append(f"- low_impact: **{summary.get('low_impact', 0)}**")
    lines.append(f"- symbols_mentioned: **{len(summary.get('symbols_mentioned') or [])}**")
    lines.append('')

    for ann in payload.get('announcements') or []:
        emoji = IMPACT_EMOJI.get(ann['impact'], '⚪')
        sym_str = ', '.join(ann.get('symbols') or [])
        header = f"### {emoji} {ann['title']}"
        if sym_str:
            header += f" ({sym_str})"
        lines.append(header)
        lines.append(f"- type: `{ann['type']}` | impact: `{ann['impact']}`")
        if ann.get('symbols'):
            lines.append(f"- symbols: {sym_str}")
        if ann.get('publish_time'):
            lines.append(f"- time: {ann['publish_time']}")
        if ann.get('web_link'):
            lines.append(f"- link: {ann['web_link']}")
        lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


def build_announcement_payload(
    limit: int = 80,
    start_page: int = 1,
    max_pages: int = 6,
) -> Dict[str, Any]:
    raw_posts = scrape_square(limit=limit, start_page=start_page, max_pages=max_pages)
    announcements = build_announcement_posts(raw_posts)

    all_symbols: List[str] = []
    for ann in announcements:
        all_symbols.extend(ann.get('symbols') or [])
    unique_symbols = list(dict.fromkeys(all_symbols))

    high = [a for a in announcements if a['impact'] == 'high']
    medium = [a for a in announcements if a['impact'] == 'medium']
    low = [a for a in announcements if a['impact'] == 'low']

    return {
        'module': 'announcement_monitor_v1',
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'announcement_monitor_v1',
            'limit': limit,
            'start_page': start_page,
            'max_pages': max_pages,
        },
        'summary': {
            'total_announcements': len(announcements),
            'high_impact': len(high),
            'medium_impact': len(medium),
            'low_impact': len(low),
            'symbols_mentioned': unique_symbols,
        },
        'announcements': announcements,
        'high_impact_announcements': high,
    }


def save_payload(payload: Dict[str, Any], output: Optional[str]) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'announcement_monitor_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    path.with_suffix('.md').write_text(render_markdown(payload), encoding='utf-8')
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='币安公告监控 V1')
    parser.add_argument('--limit', type=int, default=80)
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--max-pages', type=int, default=6)
    parser.add_argument('--output')
    args = parser.parse_args()

    payload = build_announcement_payload(
        limit=args.limit,
        start_page=args.start_page,
        max_pages=args.max_pages,
    )
    out = save_payload(payload, args.output)
    print(out)
    print(json.dumps({
        'summary': payload['summary'],
        'announcements': payload['announcements'][:5],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
