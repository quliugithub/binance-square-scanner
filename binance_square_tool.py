#!/usr/bin/env python3
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

RESULTS_DIR = Path('/root/.openclaw/workspace/results')
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

SQUARE_URL = 'https://www.binance.com/en/square'
DETAIL_API = 'https://www.binance.com/bapi/composite/v2/friendly/pgc/special/content/detail/{content_id}'
COMMON_HEADERS = {
    'user-agent': 'Mozilla/5.0',
    'referer': SQUARE_URL,
    'origin': 'https://www.binance.com',
}


def _run_browser_js(limit: int) -> List[str]:
    raise RuntimeError('Browser-backed discovery must be run through the OpenClaw browser tool, not plain local Python')


def discover_post_ids(limit: int = 20) -> List[str]:
    cache = RESULTS_DIR / 'binance_square_seed_ids.json'
    if cache.exists():
        data = json.loads(cache.read_text(encoding='utf-8'))
        return list(dict.fromkeys((data.get('ids') or [])[:limit]))
    raise RuntimeError('No cached seed ids found. Use the OpenClaw browser tool flow to refresh seed ids first.')


def fetch_post_detail(content_id: str, session: Optional[requests.Session] = None) -> Dict[str, Any]:
    session = session or requests.Session()
    url = DETAIL_API.format(content_id=content_id)
    resp = session.get(url, headers=COMMON_HEADERS, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get('data')
    if not data:
        raise ValueError(f'empty data for content_id={content_id}')
    return data


def normalize_post(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'content_id': data.get('id'),
        'root_content_id': data.get('rootContentId'),
        'author': data.get('displayName'),
        'username': data.get('username'),
        'author_avatar': data.get('avatar'),
        'publish_time': data.get('latestReleaseTimeString') or data.get('firstReleaseTime'),
        'body': data.get('bodyTextOnly') or data.get('summary') or '',
        'view_count': data.get('viewCount'),
        'comment_count': data.get('commentCount'),
        'like_count': data.get('likeCount'),
        'share_count': data.get('shareCount'),
        'bookmark_count': data.get('bookmarkCount'),
        'quote_count': data.get('quoteCount'),
        'tendency': data.get('tendency'),
        'bullish_ratio': data.get('bullishRatio'),
        'bearish_ratio': data.get('bearishRatio'),
        'symbols': data.get('coinPairList') or [],
        'trading_pairs': [p.get('symbol') for p in (data.get('tradingPairsV2') or []) if isinstance(p, dict)],
        'tags': [h.get('tagName') or h.get('name') for h in (data.get('hashtagList') or []) if isinstance(h, dict)],
        'content_type': data.get('contentType'),
        'source_type': data.get('sourceType'),
        'web_link': data.get('webLink'),
        'share_link': data.get('shareLink'),
        'raw': data,
    }


def scrape_square(limit: int = 20, include_raw: bool = False) -> List[Dict[str, Any]]:
    ids = discover_post_ids(limit)
    session = requests.Session()
    rows: List[Dict[str, Any]] = []
    for content_id in ids:
        try:
            detail = fetch_post_detail(content_id, session=session)
            row = normalize_post(detail)
            if not include_raw:
                row.pop('raw', None)
            rows.append(row)
        except Exception as e:
            rows.append({'content_id': content_id, 'error': str(e)})
    return rows


def save_output(rows: List[Dict[str, Any]], fmt: str, output: Optional[str]) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'binance_square_posts_{ts}.{fmt}'
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == 'json':
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    elif fmt == 'jsonl':
        path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows), encoding='utf-8')
    else:
        raise ValueError(f'unsupported format: {fmt}')
    return str(path)


def main():
    parser = argparse.ArgumentParser(description='Binance Square 通用抓取工具')
    sub = parser.add_subparsers(dest='command', required=True)

    p_discover = sub.add_parser('discover', help='仅发现广场帖子 ID')
    p_discover.add_argument('--limit', type=int, default=20)

    p_detail = sub.add_parser('detail', help='抓取单条帖子详情')
    p_detail.add_argument('content_id')
    p_detail.add_argument('--raw', action='store_true')

    p_scrape = sub.add_parser('scrape', help='批量抓取广场帖子')
    p_scrape.add_argument('--limit', type=int, default=20)
    p_scrape.add_argument('--format', choices=['json', 'jsonl'], default='json')
    p_scrape.add_argument('--output')
    p_scrape.add_argument('--raw', action='store_true')

    args = parser.parse_args()

    if args.command == 'discover':
        ids = discover_post_ids(args.limit)
        print(json.dumps(ids, ensure_ascii=False, indent=2))
        return

    if args.command == 'detail':
        row = normalize_post(fetch_post_detail(args.content_id))
        if not args.raw:
            row.pop('raw', None)
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return

    if args.command == 'scrape':
        rows = scrape_square(limit=args.limit, include_raw=args.raw)
        out = save_output(rows, args.format, args.output)
        print(out)
        print(json.dumps({
            'count': len(rows),
            'sample': rows[:3],
        }, ensure_ascii=False, indent=2))
        return


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
