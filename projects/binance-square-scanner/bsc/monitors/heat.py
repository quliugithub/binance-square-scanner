#!/usr/bin/env python3
import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Set

import requests

from bsc.constants import RESULTS_DIR


COINGECKO_TRENDING_API = 'https://api.coingecko.com/api/v3/search/trending'
BINANCE_FUTURES_TICKER_API = 'https://fapi.binance.com/fapi/v1/ticker/24hr'
STABLECOIN_NOISE = {
    'USD', 'USDT', 'USDC', 'BUSD', 'FDUSD', 'TUSD', 'DAI', 'USDE', 'PYUSD'
}
TICKER_RE = re.compile(r'\$([A-Z][A-Z0-9]{1,15})\b')


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def load_square_posts(path: str) -> List[Dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get('posts'), list):
        return payload['posts']
    raise ValueError('unsupported square posts format')


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace('/', '')


def extract_square_mentions(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Counter[str] = Counter()
    authors: Dict[str, Set[str]] = defaultdict(set)
    engagements: Counter[str] = Counter()
    sample_posts: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for post in posts:
        body = str(post.get('body') or '').upper()
        found = set(normalize_symbol(x) for x in TICKER_RE.findall(body))
        found = {x for x in found if x and x not in STABLECOIN_NOISE}
        author_key = str(post.get('username') or post.get('author') or 'unknown')
        engagement = int(post.get('engagement_total') or 0)

        for symbol in found:
            counts[symbol] += 1
            authors[symbol].add(author_key)
            engagements[symbol] += engagement
            if len(sample_posts[symbol]) < 3:
                sample_posts[symbol].append({
                    'content_id': post.get('content_id'),
                    'author': post.get('author'),
                    'username': post.get('username'),
                    'web_link': post.get('web_link'),
                    'body_preview': str(post.get('body') or '').replace('\n', ' ')[:180],
                })

    rows = []
    for symbol, mention_count in counts.items():
        rows.append({
            'symbol': symbol,
            'square_mentions': mention_count,
            'unique_authors': len(authors[symbol]),
            'square_engagement_score': engagements[symbol],
            'sample_posts': sample_posts[symbol],
        })
    rows.sort(key=lambda x: (x['square_mentions'], x['unique_authors'], x['square_engagement_score']), reverse=True)
    return rows


def fetch_coingecko_trending() -> Dict[str, Dict[str, Any]]:
    resp = requests.get(COINGECKO_TRENDING_API, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    rows = {}
    for rank, coin in enumerate(payload.get('coins') or [], start=1):
        item = coin.get('item') or {}
        symbol = normalize_symbol(str(item.get('symbol') or ''))
        if not symbol or symbol in STABLECOIN_NOISE:
            continue
        rows[symbol] = {
            'coingecko_trending_flag': True,
            'coingecko_rank': rank,
            'coingecko_name': item.get('name'),
            'coingecko_score': max(0, 12 - rank),
        }
    return rows


def fetch_binance_futures_movers(top_n: int = 30) -> Dict[str, Dict[str, Any]]:
    resp = requests.get(BINANCE_FUTURES_TICKER_API, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    rows = []
    for item in payload:
        symbol = str(item.get('symbol') or '')
        if not symbol.endswith('USDT'):
            continue
        base = normalize_symbol(symbol[:-4])
        if not base or base in STABLECOIN_NOISE:
            continue
        rows.append({
            'symbol': base,
            'futures_symbol': symbol,
            'price_change_pct_24h': float(item.get('priceChangePercent') or 0),
            'quote_volume': float(item.get('quoteVolume') or 0),
        })
    rows.sort(key=lambda x: abs(x['price_change_pct_24h']), reverse=True)
    selected = rows[:top_n]

    result = {}
    for rank, item in enumerate(selected, start=1):
        result[item['symbol']] = {
            'futures_movers_flag': True,
            'futures_rank': rank,
            'futures_symbol': item['futures_symbol'],
            'futures_change_pct_24h': item['price_change_pct_24h'],
            'futures_quote_volume': item['quote_volume'],
            'futures_score': max(0, 15 - rank * 0.4),
        }
    return result


def build_heat_ranking(square_rows: List[Dict[str, Any]], trending: Dict[str, Dict[str, Any]], movers: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    for row in square_rows:
        symbol = row['symbol']
        mention_score = row['square_mentions'] * 8
        author_score = row['unique_authors'] * 6
        engagement_score = min(25, row['square_engagement_score'] / 400)
        heat_score = mention_score + author_score + engagement_score
        merged[symbol] = {
            **row,
            'coingecko_trending_flag': False,
            'coingecko_rank': None,
            'coingecko_name': None,
            'coingecko_score': 0,
            'futures_movers_flag': False,
            'futures_rank': None,
            'futures_symbol': None,
            'futures_change_pct_24h': None,
            'futures_quote_volume': None,
            'futures_score': 0,
            'heat_score': round(heat_score, 2),
        }

    for symbol, info in trending.items():
        row = merged.setdefault(symbol, {
            'symbol': symbol,
            'square_mentions': 0,
            'unique_authors': 0,
            'square_engagement_score': 0,
            'sample_posts': [],
            'coingecko_trending_flag': False,
            'coingecko_rank': None,
            'coingecko_name': None,
            'coingecko_score': 0,
            'futures_movers_flag': False,
            'futures_rank': None,
            'futures_symbol': None,
            'futures_change_pct_24h': None,
            'futures_quote_volume': None,
            'futures_score': 0,
            'heat_score': 0,
        })
        row.update(info)
        row['heat_score'] = round(float(row.get('heat_score') or 0) + float(info.get('coingecko_score') or 0), 2)

    for symbol, info in movers.items():
        row = merged.setdefault(symbol, {
            'symbol': symbol,
            'square_mentions': 0,
            'unique_authors': 0,
            'square_engagement_score': 0,
            'sample_posts': [],
            'coingecko_trending_flag': False,
            'coingecko_rank': None,
            'coingecko_name': None,
            'coingecko_score': 0,
            'futures_movers_flag': False,
            'futures_rank': None,
            'futures_symbol': None,
            'futures_change_pct_24h': None,
            'futures_quote_volume': None,
            'futures_score': 0,
            'heat_score': 0,
        })
        row.update(info)
        row['heat_score'] = round(float(row.get('heat_score') or 0) + float(info.get('futures_score') or 0), 2)

    ranked = sorted(merged.values(), key=lambda x: x['heat_score'], reverse=True)
    for idx, row in enumerate(ranked, start=1):
        row['heat_rank'] = idx
    return ranked


def render_markdown(payload: Dict[str, Any]) -> str:
    lines = ['# 🔥 Heat Scanner', '']
    summary = payload.get('summary') or {}
    lines.append(f"- square_posts: **{summary.get('square_posts', 0)}**")
    lines.append(f"- unique_symbols: **{summary.get('unique_symbols', 0)}**")
    lines.append(f"- ranked_symbols: **{summary.get('ranked_symbols', 0)}**")
    lines.append('')
    lines.append('## Top Heat Ranking')
    lines.append('')
    for row in (payload.get('heat_ranking') or [])[:15]:
        badges = []
        if row.get('coingecko_trending_flag'):
            badges.append('🦎 CG')
        if row.get('futures_movers_flag'):
            badges.append('📈 Movers')
        badge_text = ' '.join(badges)
        lines.append(f"### {row.get('heat_rank')}. {row.get('symbol')} {badge_text}".rstrip())
        lines.append(f"- heat_score: `{row.get('heat_score')}`")
        lines.append(f"- square_mentions: `{row.get('square_mentions')}`")
        lines.append(f"- unique_authors: `{row.get('unique_authors')}`")
        lines.append(f"- engagement_score: `{row.get('square_engagement_score')}`")
        if row.get('coingecko_rank'):
            lines.append(f"- CoinGecko rank: `{row.get('coingecko_rank')}`")
        if row.get('futures_symbol'):
            lines.append(f"- Futures mover: `{row.get('futures_symbol')}` / `{row.get('futures_change_pct_24h')}%`")
        for post in row.get('sample_posts') or []:
            lines.append(f"- sample: <{post.get('web_link')}> {post.get('body_preview')}")
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def save_outputs(payload: Dict[str, Any], output: str | None) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'heat_scanner_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    path.with_suffix('.md').write_text(render_markdown(payload), encoding='utf-8')
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='Heat Scanner Phase 1')
    parser.add_argument('--square-input', required=True, help='Binance Square raw posts JSON')
    parser.add_argument('--output')
    args = parser.parse_args()

    posts = load_square_posts(args.square_input)
    square_rows = extract_square_mentions(posts)
    trending = fetch_coingecko_trending()
    movers = fetch_binance_futures_movers()
    heat_ranking = build_heat_ranking(square_rows, trending, movers)

    payload = {
        'generated_at': utc_now_iso(),
        'summary': {
            'square_posts': len(posts),
            'unique_symbols': len(square_rows),
            'ranked_symbols': len(heat_ranking),
        },
        'heat_ranking': heat_ranking,
    }
    out = save_outputs(payload, args.output)
    print(out)
    print(json.dumps({
        'summary': payload['summary'],
        'top_heat_ranking': heat_ranking[:10],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
