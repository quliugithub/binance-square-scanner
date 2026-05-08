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


from bsc.data.strategy import score_post
from bsc.data.tool import scrape_square
from bsc.data.blockbeats import fetch_blockbeats_news, to_narrative_format as bb_to_narrative
from bsc.data.defillama import fetch_protocols, detect_tvl_changes, to_narrative_format as dl_to_narrative
from bsc.data.twitter_kol import fetch_all_news as fetch_crypto_news, to_narrative_format as tw_to_narrative

from bsc.constants import RESULTS_DIR


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
        # Pre-formatted posts (from BlockBeats/DefiLlama/Twitter KOL) already have signal_score
        if post.get('_source') and 'signal_score' in post:
            scored = post
        else:
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
                'source': scored.get('_source', 'binance_square'),
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
            src = post.get('source', 'binance_square')
            lines.append(f"- sample [{src}]: <{post.get('web_link')}> {post.get('body_preview')}")
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


def extract_symbols_from_text(text: str) -> List[str]:
    """Extract potential token symbols from Chinese/English news text."""
    # Common crypto token names (Chinese and English)
    TOKEN_NAMES = {
        'bitcoin': 'BTCUSDT', 'btc': 'BTCUSDT', '比特币': 'BTCUSDT',
        'ethereum': 'ETHUSDT', 'eth': 'ETHUSDT', '以太坊': 'ETHUSDT',
        'solana': 'SOLUSDT', 'sol': 'SOLUSDT',
        'bnb': 'BNBUSDT', '币安币': 'BNBUSDT',
        'xrp': 'XRPUSDT', '瑞波': 'XRPUSDT',
        'ada': 'ADAUSDT', 'cardano': 'ADAUSDT',
        'doge': 'DOGEUSDT', 'dogecoin': 'DOGEUSDT', '狗狗币': 'DOGEUSDT',
        'dot': 'DOTUSDT', 'polkadot': 'DOTUSDT',
        'avax': 'AVAXUSDT', 'avalanche': 'AVAXUSDT',
        'matic': 'MATICUSDT', 'polygon': 'MATICUSDT',
        'link': 'LINKUSDT', 'chainlink': 'LINKUSDT',
        'uni': 'UNIUSDT', 'uniswap': 'UNIUSDT',
        'aave': 'AAVEUSDT',
        'ldo': 'LDOUSDT', 'lido': 'LDOUSDT',
        'pepe': 'PEPEUSDT',
        'wif': 'WIFUSDT',
        'bonk': 'BONKUSDT',
        'hype': 'HYPEUSDT',
        'jto': 'JTOUSDT', 'jito': 'JTOUSDT',
        'render': 'RENDERUSDT', 'rndr': 'RENDERUSDT',
        'tao': 'TAOUSDT', 'bittensor': 'TAOUSDT',
        'fet': 'FETUSDT', 'fetch.ai': 'FETUSDT',
        'ondo': 'ONDOUSDT',
        'om': 'OMUSDT', 'mantra': 'OMUSDT',
        'arb': 'ARBUSDT', 'arbitrum': 'ARBUSDT',
        'op': 'OPUSDT', 'optimism': 'OPUSDT',
        'sui': 'SUIUSDT',
        'apt': 'APTUSDT', 'aptos': 'APTUSDT',
        'near': 'NEARUSDT',
        'atom': 'ATOMUSDT', 'cosmos': 'ATOMUSDT',
        'inj': 'INJUSDT', 'injective': 'INJUSDT',
        'rune': 'RUNEUSDT', 'thorchain': 'RUNEUSDT',
        'sei': 'SEIUSDT',
        'tia': 'TIAUSDT', 'celestia': 'TIAUSDT',
        'jup': 'JUPUSDT', 'jupiter': 'JUPUSDT',
        'ray': 'RAYUSDT', 'raydium': 'RAYUSDT',
        'ordi': 'ORDIUSDT',
        'strk': 'STRKUSDT', 'starknet': 'STRKUSDT',
        'imx': 'IMXUSDT', 'immutable': 'IMXUSDT',
        'gala': 'GALAUSDT',
        'cake': 'CAKEUSDT', 'pancakeswap': 'CAKEUSDT',
        'hnt': 'HNTUSDT', 'helium': 'HNTUSDT',
        'akt': 'AKTUSDT', 'akash': 'AKTUSDT',
        'ethfi': 'ETHFIUSDT', 'ether.fi': 'ETHFIUSDT',
        'mkr': 'MKRUSDT', 'maker': 'MKRUSDT',
        'floki': 'FLOKIUSDT',
        'shib': 'SHIBUSDT', 'shiba': 'SHIBUSDT',
        'trump': 'TRUMPUSDT',
    }
    text_lower = text.lower()
    symbols = set()
    for name, pair in TOKEN_NAMES.items():
        # Word boundary check for English tokens
        if re.search(r'\b' + re.escape(name) + r'\b', text_lower):
            symbols.add(pair)
        elif name in text_lower:
            symbols.add(pair)
    return list(symbols)


def enrich_post_symbols(posts: List[Dict[str, Any]]) -> None:
    """Extract symbols from post body text and merge with existing symbol_candidates."""
    for post in posts:
        body = post.get('body', '')
        extracted = extract_symbols_from_text(body)
        existing = set(post.get('symbol_candidates') or [])
        existing.update(extracted)
        existing.update(post.get('trading_pairs') or [])
        post['symbol_candidates'] = list(existing)
        post['trading_pairs'] = list(existing)


def main() -> None:
    parser = argparse.ArgumentParser(description='叙事监控引擎 V2（多数据源）')
    parser.add_argument('--limit', type=int, default=80)
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--max-pages', type=int, default=6)
    parser.add_argument('--top-n', type=int, default=10)
    parser.add_argument('--output')
    # Data source toggles
    parser.add_argument('--no-binance', action='store_true', help='禁用 Binance Square 数据源')
    parser.add_argument('--no-blockbeats', action='store_true', help='禁用 BlockBeats 数据源')
    parser.add_argument('--no-defillama', action='store_true', help='禁用 DefiLlama 数据源')
    parser.add_argument('--no-twitter', action='store_true', help='禁用 Twitter KOL 数据源')
    parser.add_argument('--tvl-threshold', type=float, default=15.0, help='TVL 变化阈值 (%%)')
    parser.add_argument('--kol-count', type=int, default=6, help='Twitter KOL 监控数量')
    args = parser.parse_args()

    all_raw_posts: List[Dict[str, Any]] = []
    source_stats: Dict[str, int] = {}

    # 1. Binance Square (original data source)
    if not args.no_binance:
        try:
            bs_posts = scrape_square(limit=args.limit, start_page=args.start_page, max_pages=args.max_pages)
            source_stats['binance_square'] = len(bs_posts)
            all_raw_posts.extend(bs_posts)
        except Exception as exc:
            source_stats['binance_square_error'] = str(exc)

    # 2. BlockBeats (区块律动快讯)
    if not args.no_blockbeats:
        try:
            bb_raw = fetch_blockbeats_news(limit=25)
            bb_posts = bb_to_narrative(bb_raw)
            enrich_post_symbols(bb_posts)
            source_stats['blockbeats'] = len(bb_posts)
            all_raw_posts.extend(bb_posts)
        except Exception as exc:
            source_stats['blockbeats_error'] = str(exc)

    # 3. DefiLlama TVL 异动
    if not args.no_defillama:
        try:
            protocols = fetch_protocols()
            tvl_changes = detect_tvl_changes(protocols, change_threshold_pct=args.tvl_threshold)
            dl_posts = dl_to_narrative(tvl_changes)
            source_stats['defillama'] = len(dl_posts)
            source_stats['defillama_protocols_scanned'] = len(protocols)
            all_raw_posts.extend(dl_posts)
        except Exception as exc:
            source_stats['defillama_error'] = str(exc)

    # 4. Crypto News Aggregator (replaces Twitter KOL)
    if not args.no_twitter:
        try:
            tw_raw = fetch_crypto_news()
            tw_posts = tw_to_narrative(tw_raw)
            enrich_post_symbols(tw_posts)
            source_stats['crypto_news'] = len(tw_posts)
            all_raw_posts.extend(tw_posts)
        except Exception as exc:
            source_stats['crypto_news_error'] = str(exc)

    narrative_posts = build_narrative_posts(all_raw_posts)
    narrative_rank = aggregate_narratives(narrative_posts)
    symbol_view = build_symbol_narrative_view(narrative_posts)

    payload = {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'narrative_monitor_v2_multi_source',
            'limit': args.limit,
            'start_page': args.start_page,
            'max_pages': args.max_pages,
            'top_n': args.top_n,
            'tvl_threshold': args.tvl_threshold,
            'kol_count': args.kol_count,
        },
        'source_stats': source_stats,
        'summary': {
            'fetched_posts': len(all_raw_posts),
            'tagged_posts': len(narrative_posts),
            'narratives': len(narrative_rank),
            'symbols': len(symbol_view),
            'data_sources': [k for k in source_stats if not k.endswith('_error')],
        },
        'top_narratives': narrative_rank[:args.top_n],
        'narrative_rank': narrative_rank,
        'symbol_narrative_view': symbol_view[:args.top_n],
        'tagged_posts': narrative_posts[: args.top_n * 3],
    }
    out = save_payload(payload, args.output)
    print(out)
    print(json.dumps({
        'source_stats': source_stats,
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
