#!/usr/bin/env python3
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from binance_square_strategy import score_post, aggregate_coin_signals
from binance_square_tool import scrape_square

RESULTS_DIR = Path('/root/.openclaw/workspace/results')
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
TICKER_24H_API = 'https://api.binance.com/api/v3/ticker/24hr'
BOOK_TICKER_API = 'https://api.binance.com/api/v3/ticker/bookTicker'
KLINES_API = 'https://api.binance.com/api/v3/klines'
DEX_SEARCH_API = 'https://api.dexscreener.com/latest/dex/search'


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


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _get_json(url: str, params: Dict[str, Any]) -> Any:
    resp = requests.get(url, params=params, timeout=20)
    if resp.status_code == 400:
        raise ValueError(f'symbol_not_supported:{params.get("symbol")}')
    resp.raise_for_status()
    return resp.json()


def fetch_cex_market_snapshot(symbol: str) -> Dict[str, Any]:
    ticker_data = _get_json(TICKER_24H_API, {'symbol': symbol})
    book_data = _get_json(BOOK_TICKER_API, {'symbol': symbol})
    kline_rows = _get_json(KLINES_API, {'symbol': symbol, 'interval': '15m', 'limit': 5})

    last_price = safe_float(ticker_data.get('lastPrice'))
    weighted_price = safe_float(ticker_data.get('weightedAvgPrice'))
    high_price = safe_float(ticker_data.get('highPrice'))
    low_price = safe_float(ticker_data.get('lowPrice'))
    quote_volume = safe_float(ticker_data.get('quoteVolume'))
    bid_price = safe_float(book_data.get('bidPrice'))
    ask_price = safe_float(book_data.get('askPrice'))
    spread_pct = ((ask_price - bid_price) / ask_price * 100) if ask_price > 0 else 0.0

    momentum_pct = 0.0
    intraday_range_pct = 0.0
    if weighted_price > 0:
        momentum_pct = (last_price / weighted_price - 1) * 100
    if low_price > 0:
        intraday_range_pct = (high_price / low_price - 1) * 100

    recent_closes = [safe_float(row[4]) for row in kline_rows if isinstance(row, list) and len(row) > 4]
    micro_trend_pct = 0.0
    if len(recent_closes) >= 2 and recent_closes[0] > 0:
        micro_trend_pct = (recent_closes[-1] / recent_closes[0] - 1) * 100

    return {
        'market_type': 'cex_spot',
        'last_price': last_price,
        'price_change_pct_24h': safe_float(ticker_data.get('priceChangePercent')),
        'weighted_avg_price': weighted_price,
        'quote_volume_24h': quote_volume,
        'count_24h': int(safe_float(ticker_data.get('count'))),
        'bid_price': bid_price,
        'ask_price': ask_price,
        'spread_pct': round(spread_pct, 4),
        'intraday_range_pct': round(intraday_range_pct, 4),
        'momentum_vs_vwap_pct': round(momentum_pct, 4),
        'micro_trend_pct_15m': round(micro_trend_pct, 4),
    }


def _dex_pair_quality(pair: Dict[str, Any], normalized: str) -> float:
    base_symbol = str(((pair.get('baseToken') or {}).get('symbol') or '')).upper()
    volume_h24 = safe_float(((pair.get('volume') or {}).get('h24')))
    liquidity_usd = safe_float(((pair.get('liquidity') or {}).get('usd')))
    buys_h24 = int(safe_float((((pair.get('txns') or {}).get('h24') or {}).get('buys'))))
    sells_h24 = int(safe_float((((pair.get('txns') or {}).get('h24') or {}).get('sells'))))
    txns_total = buys_h24 + sells_h24
    price_usd = safe_float(pair.get('priceUsd'))
    change_h24 = abs(safe_float(((pair.get('priceChange') or {}).get('h24'))))

    score = 0.0
    if base_symbol == normalized:
        score += 1000
    score += min(volume_h24 / 1000.0, 500)
    score += min(liquidity_usd / 1000.0, 500)
    score += min(txns_total, 500)

    if price_usd <= 0 or price_usd > 1_000_000:
        score -= 1000
    if change_h24 > 500:
        score -= 600
    if txns_total < 20:
        score -= 300
    return score


def fetch_dex_market_snapshot(symbol: str) -> Dict[str, Any]:
    query = symbol.replace('USDT', '').replace('USDC', '').strip()
    data = requests.get(DEX_SEARCH_API, params={'q': query}, timeout=20)
    data.raise_for_status()
    payload = data.json()
    pairs = payload.get('pairs') or []
    if not pairs:
        raise ValueError(f'asset_not_found:{symbol}')

    normalized = query.upper()
    matching_pairs = [
        pair for pair in pairs
        if str(((pair.get('baseToken') or {}).get('symbol') or '')).upper() == normalized
    ]
    candidates = matching_pairs or pairs
    filtered_candidates = [
        pair for pair in candidates
        if safe_float(pair.get('priceUsd')) > 0
    ]
    if not filtered_candidates:
        raise ValueError(f'asset_not_found:{symbol}')

    best = max(filtered_candidates, key=lambda pair: _dex_pair_quality(pair, normalized))

    liquidity_usd = safe_float(((best.get('liquidity') or {}).get('usd')))
    volume_h24 = safe_float(((best.get('volume') or {}).get('h24')))
    buys_h24 = int(safe_float((((best.get('txns') or {}).get('h24') or {}).get('buys'))))
    sells_h24 = int(safe_float((((best.get('txns') or {}).get('h24') or {}).get('sells'))))
    price_usd = safe_float(best.get('priceUsd'))
    price_change_h24 = safe_float(((best.get('priceChange') or {}).get('h24')))
    price_change_h1 = safe_float(((best.get('priceChange') or {}).get('h1')))
    price_change_m5 = safe_float(((best.get('priceChange') or {}).get('m5')))

    return {
        'market_type': 'dex',
        'chain_id': best.get('chainId'),
        'dex_id': best.get('dexId'),
        'pair_address': best.get('pairAddress'),
        'pair_url': best.get('url'),
        'base_symbol': ((best.get('baseToken') or {}).get('symbol')),
        'quote_symbol': ((best.get('quoteToken') or {}).get('symbol')),
        'last_price': price_usd,
        'price_change_pct_24h': price_change_h24,
        'micro_trend_pct_15m': price_change_m5,
        'momentum_vs_vwap_pct': price_change_h1,
        'quote_volume_24h': volume_h24,
        'liquidity_usd': liquidity_usd,
        'txns_buys_24h': buys_h24,
        'txns_sells_24h': sells_h24,
        'spread_pct': 0.0,
        'intraday_range_pct': 0.0,
    }


def fetch_market_snapshot(symbol: str) -> Dict[str, Any]:
    try:
        return fetch_cex_market_snapshot(symbol)
    except Exception as exc:
        if 'symbol_not_supported:' not in str(exc):
            raise
    return fetch_dex_market_snapshot(symbol)


def evaluate_market_filters(symbol: str, bias: str, snapshot: Dict[str, Any], max_spread_pct: float, min_quote_volume: float) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    passed = True
    market_type = snapshot.get('market_type')

    if snapshot.get('quote_volume_24h', 0.0) < min_quote_volume:
        passed = False
        reasons.append(f'24h成交额不足 {int(min_quote_volume)}')

    if market_type == 'cex_spot' and snapshot.get('spread_pct', 0.0) > max_spread_pct:
        passed = False
        reasons.append(f'盘口价差过大 {snapshot.get("spread_pct"):.4f}%')

    if market_type == 'dex':
        if snapshot.get('liquidity_usd', 0.0) < min_quote_volume * 0.1:
            passed = False
            reasons.append(f'DEX流动性不足 {int(min_quote_volume * 0.1)}')
        txns_total = int(snapshot.get('txns_buys_24h', 0)) + int(snapshot.get('txns_sells_24h', 0))
        if txns_total < 200:
            passed = False
            reasons.append(f'DEX成交笔数偏低 {txns_total}')

    change_24h = snapshot.get('price_change_pct_24h', 0.0)
    micro_trend = snapshot.get('micro_trend_pct_15m', 0.0)
    momentum = snapshot.get('momentum_vs_vwap_pct', 0.0)

    if bias == 'long_watch':
        if change_24h <= -8:
            passed = False
            reasons.append(f'24h涨跌幅偏弱 {change_24h:.2f}%')
        if micro_trend <= -2.0:
            passed = False
            reasons.append(f'短线动能转弱 {micro_trend:.2f}%')
        if momentum <= -5.0:
            passed = False
            reasons.append(f'1h动量偏弱 {momentum:.2f}%')
    elif bias == 'short_watch':
        if change_24h >= 12:
            passed = False
            reasons.append(f'24h涨幅过强 {change_24h:.2f}%')
        if micro_trend >= 2.0:
            passed = False
            reasons.append(f'短线动能转强 {micro_trend:.2f}%')
        if momentum >= 5.0:
            passed = False
            reasons.append(f'1h动量过强 {momentum:.2f}%')

    if passed:
        reasons.append(f'通过真实价格过滤 ({market_type})')
    return passed, reasons


def build_trade_candidates(coin_signals: List[Dict[str, Any]], min_conviction: float, min_posts: int, max_spread_pct: float, min_quote_volume: float) -> List[Dict[str, Any]]:
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
        symbol = row.get('symbol')
        market_snapshot: Optional[Dict[str, Any]] = None
        filter_passed = False
        filter_reasons: List[str] = []
        try:
            market_snapshot = fetch_market_snapshot(symbol)
            filter_passed, filter_reasons = evaluate_market_filters(
                symbol=symbol,
                bias=row.get('bias') or 'no_trade',
                snapshot=market_snapshot,
                max_spread_pct=max_spread_pct,
                min_quote_volume=min_quote_volume,
            )
        except Exception as exc:
            message = str(exc)
            if 'asset_not_found:' in message:
                missing_symbol = message.split(':', 1)[1]
                filter_reasons = [f'价格过滤失败: {missing_symbol} 在 CEX 和 DEX 都没找到有效市场']
            elif 'symbol_not_supported:' in message:
                unsupported_symbol = message.split(':', 1)[1]
                filter_reasons = [f'现货不可交易，且链上市场探测失败: {unsupported_symbol}']
            else:
                filter_reasons = [f'价格过滤失败: {exc}']

        rows.append({
            'symbol': symbol,
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
            'price_filter_passed': filter_passed,
            'price_filter_reasons': filter_reasons,
            'market_snapshot': market_snapshot,
            'scanner_note': scanner_note(row),
        })
    rows.sort(key=lambda x: (x['price_filter_passed'], x['conviction_score'], x['post_count'], x['avg_signal_score']), reverse=True)
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


def run_scan(limit: int, start_page: int, max_pages: int, min_conviction: float, min_posts: int, min_post_score: float, post_limit: int, max_spread_pct: float, min_quote_volume: float) -> Dict[str, Any]:
    raw_posts = scrape_square(limit=limit, start_page=start_page, max_pages=max_pages)
    scored_posts = [score_post(post) for post in raw_posts if not post.get('error')]
    coin_signals = aggregate_coin_signals(scored_posts)
    trade_candidates = build_trade_candidates(
        coin_signals,
        min_conviction=min_conviction,
        min_posts=min_posts,
        max_spread_pct=max_spread_pct,
        min_quote_volume=min_quote_volume,
    )
    post_watchlist = build_post_watchlist(scored_posts, min_post_score=min_post_score, limit=post_limit)
    passed_candidates = [row for row in trade_candidates if row.get('price_filter_passed')]

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
            'max_spread_pct': max_spread_pct,
            'min_quote_volume': min_quote_volume,
        },
        'summary': {
            'fetched_posts': len(raw_posts),
            'scored_posts': len(scored_posts),
            'coin_signals': len(coin_signals),
            'trade_candidates': len(trade_candidates),
            'passed_price_filter': len(passed_candidates),
            'watch_posts': len(post_watchlist),
        },
        'trade_candidates': trade_candidates,
        'passed_trade_candidates': passed_candidates,
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
    parser.add_argument('--max-spread-pct', type=float, default=0.35)
    parser.add_argument('--min-quote-volume', type=float, default=5000000)
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
        max_spread_pct=args.max_spread_pct,
        min_quote_volume=args.min_quote_volume,
    )
    out = save_payload(payload, args.output)
    print(out)
    print(json.dumps({
        'summary': payload['summary'],
        'top_trade_candidates': payload['passed_trade_candidates'][:5],
        'top_watch_posts': payload['watch_posts'][:3],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
