#!/usr/bin/env python3
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from binance_square_strategy import score_post, aggregate_coin_signals
from binance_square_tool import scrape_square

RESULTS_DIR = Path('/root/.openclaw/workspace/results')
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def confidence_band(score: float) -> str:
    if score >= 80:
        return 'A'
    if score >= 65:
        return 'B'
    if score >= 50:
        return 'C'
    return 'D'


def build_trade_candidates(coin_signals: List[Dict[str, Any]], min_conviction: float, min_posts: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in coin_signals:
        if row.get('bias') == 'no_trade':
            continue
        if float(row.get('conviction_score') or 0) < min_conviction:
            continue
        if int(row.get('post_count') or 0) < min_posts:
            continue
        conviction = float(row.get('conviction_score') or 0)
        avg_signal = float(row.get('avg_signal_score') or 0)
        rows.append({
            'symbol': row.get('symbol'),
            'bias': row.get('bias'),
            'confidence_band': confidence_band(conviction),
            'conviction_score': round(conviction, 2),
            'avg_signal_score': round(avg_signal, 2),
            'post_count': int(row.get('post_count') or 0),
            'unique_authors': int(row.get('unique_authors') or 0),
            'bullish_posts': int(row.get('bullish_posts') or 0),
            'bearish_posts': int(row.get('bearish_posts') or 0),
            'total_engagement': int(row.get('total_engagement') or 0),
            'sample_post_ids': row.get('sample_post_ids') or [],
            'scanner_note': scanner_note(row),
        })
    rows.sort(key=lambda x: (x['conviction_score'], x['post_count'], x['avg_signal_score']), reverse=True)
    return rows


def scanner_note(row: Dict[str, Any]) -> str:
    symbol = row.get('symbol')
    bias = row.get('bias')
    posts = int(row.get('post_count') or 0)
    authors = int(row.get('unique_authors') or 0)
    bulls = int(row.get('bullish_posts') or 0)
    bears = int(row.get('bearish_posts') or 0)
    if bias == 'long_watch':
        return f'{symbol} 偏多，{posts} 条帖子，{authors} 位作者，情绪多头 {bulls}:{bears}'
    return f'{symbol} 偏空，{posts} 条帖子，{authors} 位作者，情绪空头 {bears}:{bulls}'


def build_post_watchlist(posts: List[Dict[str, Any]], min_post_score: float, limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for post in posts:
        score = float(post.get('signal_score') or 0)
        if score < min_post_score:
            continue
        rows.append({
            'content_id': post.get('content_id'),
            'author': post.get('author'),
            'username': post.get('username'),
            'publish_time': post.get('publish_time'),
            'tone': post.get('tone'),
            'signal_label': post.get('signal_label'),
            'signal_score': round(score, 2),
            'symbols': post.get('symbol_candidates') or [],
            'engagement_total': int(post.get('engagement_total') or 0),
            'body_preview': (post.get('body') or '').replace('\n', ' ')[:180],
            'web_link': post.get('web_link'),
        })
    rows.sort(key=lambda x: (x['signal_score'], x['engagement_total']), reverse=True)
    return rows[:limit]


def run_scan(limit: int, start_page: int, max_pages: int, min_conviction: float, min_posts: int, min_post_score: float, post_limit: int) -> Dict[str, Any]:
    raw_posts = scrape_square(limit=limit, start_page=start_page, max_pages=max_pages)
    scored_posts = [score_post(post) for post in raw_posts if not post.get('error')]
    coin_signals = aggregate_coin_signals(scored_posts)
    trade_candidates = build_trade_candidates(coin_signals, min_conviction=min_conviction, min_posts=min_posts)
    post_watchlist = build_post_watchlist(scored_posts, min_post_score=min_post_score, limit=post_limit)

    return {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'limit': limit,
            'start_page': start_page,
            'max_pages': max_pages,
            'min_conviction': min_conviction,
            'min_posts': min_posts,
            'min_post_score': min_post_score,
            'post_limit': post_limit,
        },
        'summary': {
            'fetched_posts': len(raw_posts),
            'scored_posts': len(scored_posts),
            'coin_signals': len(coin_signals),
            'trade_candidates': len(trade_candidates),
            'watch_posts': len(post_watchlist),
        },
        'trade_candidates': trade_candidates,
        'watch_posts': post_watchlist,
        'top_coin_signals': coin_signals[:10],
    }


def save_payload(payload: Dict[str, Any], output: Optional[str]) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'binance_square_scanner_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='Binance Square 信号扫描器原型')
    parser.add_argument('--limit', type=int, default=80)
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--max-pages', type=int, default=6)
    parser.add_argument('--min-conviction', type=float, default=45.0)
    parser.add_argument('--min-posts', type=int, default=2)
    parser.add_argument('--min-post-score', type=float, default=60.0)
    parser.add_argument('--post-limit', type=int, default=20)
    parser.add_argument('--output')
    args = parser.parse_args()

    payload = run_scan(
        limit=args.limit,
        start_page=args.start_page,
        max_pages=args.max_pages,
        min_conviction=args.min_conviction,
        min_posts=args.min_posts,
        min_post_score=args.min_post_score,
        post_limit=args.post_limit,
    )
    out = save_payload(payload, args.output)
    print(out)
    print(json.dumps({
        'summary': payload['summary'],
        'top_trade_candidates': payload['trade_candidates'][:5],
        'top_watch_posts': payload['watch_posts'][:3],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
