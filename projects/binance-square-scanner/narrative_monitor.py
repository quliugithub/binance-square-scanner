#!/usr/bin/env python3
import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

CURRENT_DIR = Path(__file__).resolve().parent
LIB_DIR = CURRENT_DIR / 'lib'
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from binance_square_strategy import score_post
from binance_square_tool import scrape_square

RESULTS_DIR = CURRENT_DIR / 'results'
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

NARRATIVE_RULES: Dict[str, Dict[str, List[str]]] = {
    'AI': {
        'keywords': [' ai ', ' agent ', ' agents ', 'agi', 'llm', 'gpu', 'inference', 'bittensor', 'render', 'deai'],
        'symbols': ['TAOUSDT', 'FETUSDT', 'RENDERUSDT', 'VIRTUALUSDT', 'AI16ZUSDT'],
    },
    'Meme': {
        'keywords': ['meme', 'memecoin', 'degen', 'frog', 'doge', 'pepe', ' moon ', 'moonshot'],
        'symbols': ['DOGEUSDT', 'PEPEUSDT', 'WIFUSDT', 'BONKUSDT', 'BANANAS31USDT'],
    },
    'RWA': {
        'keywords': ['rwa', 'real world asset', 'treasury', 'yield-bearing', 'tokenization', 'ondo'],
        'symbols': ['ONDOUSDT', 'MKRUSDT', 'OMUSDT'],
    },
    'DePIN': {
        'keywords': ['depin', 'bandwidth', 'storage', 'wireless', 'helium', 'akash', 'io.net'],
        'symbols': ['AKTUSDT', 'HNTUSDT', 'IOUSDT'],
    },
    'L2': {
        'keywords': ['layer 2', ' rollup ', 'arbitrum', 'optimism', 'base ecosystem', 'blast'],
        'symbols': ['ARBUSDT', 'OPUSDT', 'STRKUSDT'],
    },
    'BTCFi': {
        'keywords': ['btcfi', 'bitcoin defi', 'ordinals', 'runes', 'brc20', 'bitlayer'],
        'symbols': ['ORDIUSDT', 'SATSUSDT'],
    },
    'Solana': {
        'keywords': ['solana', 'pumpfun', 'jupiter', 'raydium', 'spl'],
        'symbols': ['SOLUSDT', 'JUPUSDT', 'RAYUSDT'],
    },
    'BSC': {
        'keywords': ['bsc', 'bnb chain', 'pancakeswap', 'wbnb', 'binance smart chain'],
        'symbols': ['CAKEUSDT', 'BNBUSDT', 'SIRENUSDT'],
    },
    'GameFi': {
        'keywords': ['gamefi', 'gaming', 'metaverse', 'play to earn', 'p2e'],
        'symbols': ['IMXUSDT', 'GALAUSDT', 'BEAMXUSDT'],
    },
    'DeFi': {
        'keywords': ['defi', ' amm ', ' dex ', 'lending', 'yield', 'staking', 'restaking', 'lsd'],
        'symbols': ['AAVEUSDT', 'UNIUSDT', 'LDOUSDT', 'ETHFIUSDT'],
    },
}

BULLISH_WORDS = ['breakout', 'bullish', 'continuation', 'accumulation', 'momentum', 'strong', 'send it', 'higher high', 'buy']
BEARISH_WORDS = ['dump', 'crash', 'bearish', 'rejection', 'short', 'lower high', 'sell', 'distribution']
EARLY_WORDS = ['starting', 'quietly', 'building', 'forming', 'early', 'rotation', 'first move', 'under the radar']
MID_WORDS = ['continuation', 'trend', 'momentum', 'expansion', 'follow through', 'confirm']
LATE_WORDS = ['parabolic', 'everyone', 'already pumped', 'euphoria', 'fomo', 'overextended', 'blow off']
FUNDS_WORDS = ['volume', 'oi', 'open interest', 'liquidity', 'buyers', 'inflows', 'whale', 'big buy']


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def normalize_text(value: str) -> str:
    return ' ' + re.sub(r'\s+', ' ', (value or '').lower()).strip() + ' '


def score_narratives(post: Dict[str, Any]) -> List[Dict[str, Any]]:
    body = normalize_text(post.get('body') or '')
    symbols = {str(s).upper() for s in (post.get('trading_pairs') or []) if s}
    text_blob = ' '.join([body, ' '.join((post.get('tags') or []))]).lower()
    matches: List[Dict[str, Any]] = []

    for narrative, rule in NARRATIVE_RULES.items():
        keyword_hits = sum(1 for kw in rule['keywords'] if kw.lower() in text_blob)
        symbol_hits = sum(1 for sym in rule['symbols'] if sym in symbols)
        score = keyword_hits * 10 + symbol_hits * 18
        if score <= 0:
            continue
        matches.append({
            'narrative': narrative,
            'keyword_hits': keyword_hits,
            'symbol_hits': symbol_hits,
            'narrative_score': score,
        })

    matches.sort(key=lambda x: x['narrative_score'], reverse=True)
    return matches


def detect_stage(body: str, signal_score: float, engagement_total: int) -> str:
    text = normalize_text(body)
    early_hits = sum(1 for word in EARLY_WORDS if word in text)
    mid_hits = sum(1 for word in MID_WORDS if word in text)
    late_hits = sum(1 for word in LATE_WORDS if word in text)

    if late_hits >= max(2, early_hits + 1) or (signal_score >= 90 and engagement_total >= 800):
        return '末端过热'
    if early_hits >= max(1, mid_hits) and engagement_total < 600:
        return '早期发酵'
    if mid_hits > 0 or signal_score >= 75:
        return '中期扩散'
    return '观察期'


def detect_sentiment(body: str) -> str:
    text = normalize_text(body)
    bull_hits = sum(1 for word in BULLISH_WORDS if word in text)
    bear_hits = sum(1 for word in BEARISH_WORDS if word in text)
    if bull_hits > bear_hits:
        return '偏多'
    if bear_hits > bull_hits:
        return '偏空'
    return '中性'


def detect_funds_confirmation(body: str, post: Dict[str, Any], signal_score: float) -> str:
    text = normalize_text(body)
    funds_hits = sum(1 for word in FUNDS_WORDS if word in text)
    engagement_total = int(post.get('engagement_total') or 0)
    if funds_hits >= 2 and signal_score >= 70:
        return '资金确认'
    if engagement_total >= 500 and signal_score >= 80:
        return '热度确认'
    return '待确认'


def build_narrative_posts(raw_posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for post in raw_posts:
        if post.get('error'):
            continue
        scored = score_post(post)
        matches = score_narratives(scored)
        if not matches:
            continue
        body = scored.get('body') or ''
        for match in matches[:2]:
            rows.append({
                'content_id': scored.get('content_id'),
                'author': scored.get('author'),
                'username': scored.get('username'),
                'publish_time': scored.get('publish_time'),
                'web_link': scored.get('web_link'),
                'narrative': match['narrative'],
                'narrative_score': match['narrative_score'],
                'keyword_hits': match['keyword_hits'],
                'symbol_hits': match['symbol_hits'],
                'signal_score': scored.get('signal_score'),
                'signal_label': scored.get('signal_label'),
                'engagement_total': int(scored.get('engagement_total') or 0),
                'sentiment': detect_sentiment(body),
                'stage': detect_stage(body, float(scored.get('signal_score') or 0), int(scored.get('engagement_total') or 0)),
                'funds_confirmation': detect_funds_confirmation(body, scored, float(scored.get('signal_score') or 0)),
                'symbols': scored.get('symbol_candidates') or [],
                'body_preview': body.replace('\n', ' ')[:220],
            })
    rows.sort(key=lambda x: (x['narrative_score'], x['signal_score'], x['engagement_total']), reverse=True)
    return rows


def aggregate_narratives(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row['narrative']].append(row)

    result: List[Dict[str, Any]] = []
    for narrative, items in grouped.items():
        sentiments = Counter(item['sentiment'] for item in items)
        stages = Counter(item['stage'] for item in items)
        confirms = Counter(item['funds_confirmation'] for item in items)
        symbols = Counter(sym for item in items for sym in item.get('symbols') or [])
        total_engagement = sum(item['engagement_total'] for item in items)
        avg_signal_score = sum(float(item['signal_score'] or 0) for item in items) / len(items)
        narrative_heat = len(items) * 14 + len({item.get('author') for item in items}) * 8 + min(total_engagement / 120.0, 30)
        if confirms.get('资金确认', 0) >= 2:
            narrative_heat += 10

        dominant_sentiment = sentiments.most_common(1)[0][0] if sentiments else '中性'
        dominant_stage = stages.most_common(1)[0][0] if stages else '观察期'
        top_symbols = [sym for sym, _ in symbols.most_common(5)]

        result.append({
            'narrative': narrative,
            'post_count': len(items),
            'unique_authors': len({item.get('author') for item in items}),
            'avg_signal_score': round(avg_signal_score, 2),
            'total_engagement': total_engagement,
            'narrative_heat_score': round(narrative_heat, 2),
            'dominant_sentiment': dominant_sentiment,
            'dominant_stage': dominant_stage,
            'funds_confirmation_mix': dict(confirms),
            'top_symbols': top_symbols,
            'sample_posts': items[:3],
        })

    result.sort(key=lambda x: (x['narrative_heat_score'], x['post_count'], x['avg_signal_score']), reverse=True)
    for idx, row in enumerate(result, start=1):
        row['narrative_rank'] = idx
    return result


def build_symbol_narrative_view(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for symbol in row.get('symbols') or []:
            grouped[symbol].append(row)

    result: List[Dict[str, Any]] = []
    for symbol, items in grouped.items():
        narratives = Counter(item['narrative'] for item in items)
        stages = Counter(item['stage'] for item in items)
        sentiments = Counter(item['sentiment'] for item in items)
        confirms = Counter(item['funds_confirmation'] for item in items)
        result.append({
            'symbol': symbol,
            'narratives': [name for name, _ in narratives.most_common(3)],
            'post_count': len(items),
            'dominant_stage': stages.most_common(1)[0][0] if stages else '观察期',
            'dominant_sentiment': sentiments.most_common(1)[0][0] if sentiments else '中性',
            'funds_confirmation_mix': dict(confirms),
            'avg_signal_score': round(sum(float(item['signal_score'] or 0) for item in items) / len(items), 2),
            'total_engagement': sum(item['engagement_total'] for item in items),
            'sample_posts': items[:2],
        })
    result.sort(key=lambda x: (x['post_count'], x['avg_signal_score'], x['total_engagement']), reverse=True)
    return result


def render_markdown(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append('# Narrative Monitor V1')
    lines.append('')
    summary = payload.get('summary') or {}
    lines.append(f"- fetched_posts: **{summary.get('fetched_posts', 0)}**")
    lines.append(f"- tagged_posts: **{summary.get('tagged_posts', 0)}**")
    lines.append(f"- narratives: **{summary.get('narratives', 0)}**")
    lines.append('')
    lines.append('## Top narratives')
    lines.append('')
    for row in payload.get('top_narratives') or []:
        lines.append(f"### {row.get('narrative_rank')}. {row.get('narrative')} [{row.get('dominant_stage')}]")
        lines.append(f"- heat_score: `{row.get('narrative_heat_score')}`")
        lines.append(f"- sentiment: `{row.get('dominant_sentiment')}`")
        lines.append(f"- posts/authors: `{row.get('post_count')}` / `{row.get('unique_authors')}`")
        lines.append(f"- top_symbols: `{', '.join(row.get('top_symbols') or [])}`")
        for post in row.get('sample_posts') or []:
            lines.append(f"- sample: <{post.get('web_link')}> {post.get('body_preview')}")
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def save_payload(payload: Dict[str, Any], output: str | None) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'narrative_monitor_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    path.with_suffix('.md').write_text(render_markdown(payload), encoding='utf-8')
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='叙事监控引擎 V1')
    parser.add_argument('--limit', type=int, default=80)
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--max-pages', type=int, default=6)
    parser.add_argument('--top-n', type=int, default=10)
    parser.add_argument('--output')
    args = parser.parse_args()

    raw_posts = scrape_square(limit=args.limit, start_page=args.start_page, max_pages=args.max_pages)
    narrative_posts = build_narrative_posts(raw_posts)
    narrative_rank = aggregate_narratives(narrative_posts)
    symbol_view = build_symbol_narrative_view(narrative_posts)

    payload = {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'narrative_monitor_v1',
            'limit': args.limit,
            'start_page': args.start_page,
            'max_pages': args.max_pages,
            'top_n': args.top_n,
        },
        'summary': {
            'fetched_posts': len(raw_posts),
            'tagged_posts': len(narrative_posts),
            'narratives': len(narrative_rank),
            'symbols': len(symbol_view),
        },
        'top_narratives': narrative_rank[:args.top_n],
        'narrative_rank': narrative_rank,
        'symbol_narrative_view': symbol_view[:args.top_n],
        'tagged_posts': narrative_posts[: args.top_n * 3],
    }
    out = save_payload(payload, args.output)
    print(out)
    print(json.dumps({
        'summary': payload['summary'],
        'top_narratives': payload['top_narratives'][:5],
        'symbol_narrative_view': payload['symbol_narrative_view'][:5],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
