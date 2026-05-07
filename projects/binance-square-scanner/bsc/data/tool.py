#!/usr/bin/env python3
import argparse
import csv
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

from bsc.constants import RESULTS_DIR

SQUARE_URL = 'https://www.binance.com/en/square'
SITEMAP_URL = 'https://www.binance.com/en/square/sitemap/post/{page}'
DETAIL_API = 'https://www.binance.com/bapi/composite/v2/friendly/pgc/special/content/detail/{content_id}'
COMMON_HEADERS = {
    'user-agent': 'Mozilla/5.0',
    'referer': SQUARE_URL,
    'origin': 'https://www.binance.com',
}
POST_ID_RE = re.compile(r'/square/post/(\d+)')


def discover_post_ids_from_sitemap_html(html: str) -> List[str]:
    return list(dict.fromkeys(POST_ID_RE.findall(html)))


def fetch_sitemap_page(page: int, session: Optional[requests.Session] = None) -> str:
    session = session or requests.Session()
    url = SITEMAP_URL.format(page=page)
    resp = session.get(url, headers=COMMON_HEADERS, timeout=30)
    resp.raise_for_status()
    if not resp.text:
        raise ValueError(
            f'sitemap page {page} returned empty body, likely blocked by WAF challenge in plain HTTP mode'
        )
    return resp.text


def discover_post_ids(limit: int = 20, start_page: int = 1, max_pages: int = 10) -> List[str]:
    session = requests.Session()
    ids: List[str] = []
    try:
        for page in range(start_page, start_page + max_pages):
            html = fetch_sitemap_page(page, session=session)
            page_ids = discover_post_ids_from_sitemap_html(html)
            if not page_ids:
                break
            for content_id in page_ids:
                if content_id not in ids:
                    ids.append(content_id)
                    if len(ids) >= limit:
                        return ids
        if ids:
            return ids
    except Exception:
        pass

    cache = RESULTS_DIR / 'binance_square_seed_ids.json'
    if cache.exists():
        data = json.loads(cache.read_text(encoding='utf-8'))
        cached_ids = list(dict.fromkeys(data.get('ids') or []))
        if cached_ids:
            return cached_ids[:limit]

    raise RuntimeError(
        'automatic discovery failed in plain HTTP mode and no browser-refreshed seed cache is available. '
        'Run the browser seed refresh helper first.'
    )


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


def _match_keywords(text: str, keywords: Optional[List[str]]) -> bool:
    if not keywords:
        return True
    lowered = (text or '').lower()
    return any(k.lower() in lowered for k in keywords)


def _engagement_value(row: Dict[str, Any]) -> int:
    total = 0
    for key in ['view_count', 'comment_count', 'like_count', 'share_count', 'bookmark_count', 'quote_count']:
        value = row.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total


def scrape_square(
    limit: int = 20,
    include_raw: bool = False,
    start_page: int = 1,
    max_pages: int = 10,
    keywords: Optional[List[str]] = None,
    min_likes: Optional[int] = None,
    min_comments: Optional[int] = None,
) -> List[Dict[str, Any]]:
    ids = discover_post_ids(limit=limit, start_page=start_page, max_pages=max_pages)
    session = requests.Session()
    rows: List[Dict[str, Any]] = []
    for content_id in ids:
        try:
            detail = fetch_post_detail(content_id, session=session)
            row = normalize_post(detail)
            if keywords and not _match_keywords(
                ' '.join([
                    row.get('body') or '',
                    ' '.join(row.get('tags') or []),
                    ' '.join(row.get('trading_pairs') or []),
                ]),
                keywords,
            ):
                continue
            if min_likes is not None and (row.get('like_count') or 0) < min_likes:
                continue
            if min_comments is not None and (row.get('comment_count') or 0) < min_comments:
                continue
            row['engagement_total'] = _engagement_value(row)
            if not include_raw:
                row.pop('raw', None)
            rows.append(row)
        except Exception as e:
            rows.append({'content_id': content_id, 'error': str(e)})
    return rows


def _flatten_row(row: Dict[str, Any]) -> Dict[str, Any]:
    flat = dict(row)
    for key in ['symbols', 'trading_pairs', 'tags']:
        value = flat.get(key)
        if isinstance(value, list):
            flat[key] = '|'.join(str(v) for v in value)
    if isinstance(flat.get('raw'), dict):
        flat['raw'] = json.dumps(flat['raw'], ensure_ascii=False)
    return flat


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
    elif fmt == 'csv':
        flattened = [_flatten_row(r) for r in rows]
        fieldnames = sorted({k for row in flattened for k in row.keys()}) if flattened else ['content_id']
        with path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(flattened)
    else:
        raise ValueError(f'unsupported format: {fmt}')
    return str(path)


def main():
    parser = argparse.ArgumentParser(description='Binance Square 通用抓取工具')
    sub = parser.add_subparsers(dest='command', required=True)

    p_discover = sub.add_parser('discover', help='自动发现广场帖子 ID')
    p_discover.add_argument('--limit', type=int, default=20)
    p_discover.add_argument('--start-page', type=int, default=1)
    p_discover.add_argument('--max-pages', type=int, default=10)

    p_detail = sub.add_parser('detail', help='抓取单条帖子详情')
    p_detail.add_argument('content_id')
    p_detail.add_argument('--raw', action='store_true')

    p_scrape = sub.add_parser('scrape', help='批量抓取广场帖子')
    p_scrape.add_argument('--limit', type=int, default=20)
    p_scrape.add_argument('--start-page', type=int, default=1)
    p_scrape.add_argument('--max-pages', type=int, default=10)
    p_scrape.add_argument('--format', choices=['json', 'jsonl', 'csv'], default='json')
    p_scrape.add_argument('--output')
    p_scrape.add_argument('--raw', action='store_true')
    p_scrape.add_argument('--keyword', action='append', dest='keywords')
    p_scrape.add_argument('--min-likes', type=int)
    p_scrape.add_argument('--min-comments', type=int)

    args = parser.parse_args()

    if args.command == 'discover':
        ids = discover_post_ids(limit=args.limit, start_page=args.start_page, max_pages=args.max_pages)
        print(json.dumps(ids, ensure_ascii=False, indent=2))
        return

    if args.command == 'detail':
        row = normalize_post(fetch_post_detail(args.content_id))
        if not args.raw:
            row.pop('raw', None)
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return

    if args.command == 'scrape':
        rows = scrape_square(
            limit=args.limit,
            include_raw=args.raw,
            start_page=args.start_page,
            max_pages=args.max_pages,
            keywords=args.keywords,
            min_likes=args.min_likes,
            min_comments=args.min_comments,
        )
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
