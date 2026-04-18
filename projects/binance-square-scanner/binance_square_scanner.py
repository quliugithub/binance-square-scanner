#!/usr/bin/env python3
import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

CURRENT_DIR = Path(__file__).resolve().parent
LIB_DIR = CURRENT_DIR / 'lib'
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from binance_square_strategy import score_post, aggregate_coin_signals
from binance_square_tool import scrape_square

RESULTS_DIR = CURRENT_DIR / 'results'
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


def default_cex_filter_config() -> Dict[str, float]:
    return {
        'min_quote_volume_24h': 5_000_000.0,
        'max_spread_pct': 0.35,
        'long_min_change_24h_pct': -8.0,
        'long_min_micro_trend_pct': -2.0,
        'long_min_momentum_pct': -5.0,
        'short_max_change_24h_pct': 12.0,
        'short_max_micro_trend_pct': 2.0,
        'short_max_momentum_pct': 5.0,
    }


def default_dex_filter_config() -> Dict[str, float]:
    return {
        'min_quote_volume_24h': 1_000_000.0,
        'min_liquidity_usd': 250_000.0,
        'min_txns_24h': 120.0,
        'long_min_change_24h_pct': -20.0,
        'long_min_micro_trend_pct': -3.0,
        'long_min_momentum_pct': -8.0,
        'short_max_change_24h_pct': 25.0,
        'short_max_micro_trend_pct': 3.0,
        'short_max_momentum_pct': 8.0,
    }


def evaluate_market_filters(symbol: str, bias: str, snapshot: Dict[str, Any], cex_filter: Dict[str, float], dex_filter: Dict[str, float]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    passed = True
    market_type = snapshot.get('market_type')

    if market_type == 'cex_spot':
        if snapshot.get('quote_volume_24h', 0.0) < cex_filter['min_quote_volume_24h']:
            passed = False
            reasons.append(f"24h成交额不足 {int(cex_filter['min_quote_volume_24h'])}")
        if snapshot.get('spread_pct', 0.0) > cex_filter['max_spread_pct']:
            passed = False
            reasons.append(f'盘口价差过大 {snapshot.get("spread_pct"):.4f}%')
    elif market_type == 'dex':
        if snapshot.get('quote_volume_24h', 0.0) < dex_filter['min_quote_volume_24h']:
            passed = False
            reasons.append(f"24h成交额不足 {int(dex_filter['min_quote_volume_24h'])}")
        if snapshot.get('liquidity_usd', 0.0) < dex_filter['min_liquidity_usd']:
            passed = False
            reasons.append(f"DEX流动性不足 {int(dex_filter['min_liquidity_usd'])}")
        txns_total = int(snapshot.get('txns_buys_24h', 0)) + int(snapshot.get('txns_sells_24h', 0))
        if txns_total < dex_filter['min_txns_24h']:
            passed = False
            reasons.append(f'DEX成交笔数偏低 {txns_total}')
    else:
        passed = False
        reasons.append(f'未知市场类型 {market_type}')

    change_24h = snapshot.get('price_change_pct_24h', 0.0)
    micro_trend = snapshot.get('micro_trend_pct_15m', 0.0)
    momentum = snapshot.get('momentum_vs_vwap_pct', 0.0)
    cfg = cex_filter if market_type == 'cex_spot' else dex_filter

    if bias == 'long_watch':
        if change_24h <= cfg['long_min_change_24h_pct']:
            passed = False
            reasons.append(f'24h涨跌幅偏弱 {change_24h:.2f}%')
        if micro_trend <= cfg['long_min_micro_trend_pct']:
            passed = False
            reasons.append(f'短线动能转弱 {micro_trend:.2f}%')
        if momentum <= cfg['long_min_momentum_pct']:
            passed = False
            reasons.append(f'1h动量偏弱 {momentum:.2f}%')
    elif bias == 'short_watch':
        if change_24h >= cfg['short_max_change_24h_pct']:
            passed = False
            reasons.append(f'24h涨幅过强 {change_24h:.2f}%')
        if micro_trend >= cfg['short_max_micro_trend_pct']:
            passed = False
            reasons.append(f'短线动能转强 {micro_trend:.2f}%')
        if momentum >= cfg['short_max_momentum_pct']:
            passed = False
            reasons.append(f'1h动量过强 {momentum:.2f}%')

    if passed:
        reasons.append(f'通过真实价格过滤 ({market_type})')
    return passed, reasons


def build_trade_candidates(coin_signals: List[Dict[str, Any]], min_conviction: float, min_posts: int, cex_filter: Dict[str, float], dex_filter: Dict[str, float]) -> List[Dict[str, Any]]:
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
                cex_filter=cex_filter,
                dex_filter=dex_filter,
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


def extract_trade_plan(body: str) -> Dict[str, Any]:
    text = (body or '').replace('\n', ' ')
    compact = re.sub(r'\s+', ' ', text).strip()

    def _clean(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        value = re.sub(r'\s+', ' ', value).strip(' -:;,.')
        return value or None

    def _capture(pattern: str) -> Optional[str]:
        m = re.search(pattern, compact, re.I)
        return _clean(m.group(1)) if m else None

    entry = _capture(r'entry(?:\s*zone)?\s*[:\-]?\s*(.+?)(?=\s(?:sl|stop loss|tp\d|take profit\d*|why this setup|market showing|$))')
    stop_loss = _capture(r'(?:\bsl\b|stop loss)\s*[:\-]?\s*(.+?)(?=\s(?:tp\d|take profit\d*|why this setup|market showing|$))')

    take_profits: List[str] = []
    for match in re.finditer(r'(tp\d*|take profit\d*)\s*[:\-]?\s*(.+?)(?=\s(?:tp\d|take profit\d*|sl|stop loss|why this setup|market showing|$))', compact, re.I):
        value = _clean(match.group(2))
        if value:
            take_profits.append(value)

    direction = None
    lower = compact.lower()
    if 'trade setup: long' in lower or re.search(r'\blong\b', lower):
        direction = 'long'
    elif 'trade setup: short' in lower or re.search(r'\bshort\b', lower):
        direction = 'short'

    leverage = _capture(r'(\b\d{1,3}x\b)')

    return {
        'direction': direction,
        'entry': entry,
        'stop_loss': stop_loss,
        'take_profits': take_profits,
        'leverage': leverage,
    }


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


def build_passed_candidate_watchlist(passed_candidates: List[Dict[str, Any]], scored_posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for candidate in passed_candidates:
        symbol = candidate.get('symbol')
        related_posts = []
        for post in scored_posts:
            symbols = post.get('symbol_candidates') or []
            if symbol in symbols:
                related_posts.append(post)

        related_posts.sort(key=lambda p: (float(p.get('signal_score') or 0), int(p.get('engagement_total') or 0)), reverse=True)
        top_posts = []
        for post in related_posts[:3]:
            plan = extract_trade_plan(post.get('body') or '')
            top_posts.append({
                'content_id': post.get('content_id'),
                'author': post.get('author'),
                'username': post.get('username'),
                'publish_time': post.get('publish_time'),
                'signal_score': post.get('signal_score'),
                'tone': post.get('tone'),
                'engagement_total': post.get('engagement_total'),
                'web_link': post.get('web_link'),
                'body_preview': (post.get('body') or '').replace('\n', ' ')[:220],
                'trade_plan': plan,
            })

        rows.append({
            'symbol': symbol,
            'bias': candidate.get('bias'),
            'confidence_band': candidate.get('confidence_band'),
            'conviction_score': candidate.get('conviction_score'),
            'market_snapshot': candidate.get('market_snapshot'),
            'price_filter_reasons': candidate.get('price_filter_reasons'),
            'scanner_note': candidate.get('scanner_note'),
            'top_posts': top_posts,
        })
    return rows


def build_scan_payload(raw_posts: List[Dict[str, Any]], min_conviction: float, min_posts: int, min_post_score: float, post_limit: int, cex_filter: Dict[str, float], dex_filter: Dict[str, float], scan_config: Dict[str, Any]) -> Dict[str, Any]:
    scored_posts = [score_post(post) for post in raw_posts if not post.get('error')]
    coin_signals = aggregate_coin_signals(scored_posts)
    trade_candidates = build_trade_candidates(
        coin_signals,
        min_conviction=min_conviction,
        min_posts=min_posts,
        cex_filter=cex_filter,
        dex_filter=dex_filter,
    )
    post_watchlist = build_post_watchlist(scored_posts, min_post_score=min_post_score, limit=post_limit)
    passed_candidates = [row for row in trade_candidates if row.get('price_filter_passed')]
    passed_candidate_watchlist = build_passed_candidate_watchlist(passed_candidates, scored_posts)

    return {
        'generated_at': utc_now_iso(),
        'scan_config': scan_config,
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
        'passed_candidate_watchlist': passed_candidate_watchlist,
        'watch_posts': post_watchlist,
        'top_coin_signals': coin_signals[:10],
    }


def run_scan(limit: int, start_page: int, max_pages: int, min_conviction: float, min_posts: int, min_post_score: float, post_limit: int, cex_filter: Dict[str, float], dex_filter: Dict[str, float]) -> Dict[str, Any]:
    raw_posts = scrape_square(limit=limit, start_page=start_page, max_pages=max_pages)
    return build_scan_payload(
        raw_posts=raw_posts,
        min_conviction=min_conviction,
        min_posts=min_posts,
        min_post_score=min_post_score,
        post_limit=post_limit,
        cex_filter=cex_filter,
        dex_filter=dex_filter,
        scan_config={
            'mode': 'live',
            'limit': limit,
            'start_page': start_page,
            'max_pages': max_pages,
            'min_conviction': min_conviction,
            'min_posts': min_posts,
            'min_post_score': min_post_score,
            'post_limit': post_limit,
            'cex_filter': cex_filter,
            'dex_filter': dex_filter,
        },
    )


def load_posts_from_file(path: str) -> List[Dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get('posts'), list):
            return payload['posts']
        if isinstance(payload.get('watch_posts'), list):
            raise ValueError('watch_posts 不是原始帖子样本，不能用于回放')
    raise ValueError('不支持的样本格式，需为原始帖子列表或包含 posts 字段的 JSON')


def run_replay(sample_path: str, min_conviction: float, min_posts: int, min_post_score: float, post_limit: int, cex_filter: Dict[str, float], dex_filter: Dict[str, float]) -> Dict[str, Any]:
    raw_posts = load_posts_from_file(sample_path)
    return build_scan_payload(
        raw_posts=raw_posts,
        min_conviction=min_conviction,
        min_posts=min_posts,
        min_post_score=min_post_score,
        post_limit=post_limit,
        cex_filter=cex_filter,
        dex_filter=dex_filter,
        scan_config={
            'mode': 'replay',
            'sample_path': sample_path,
            'min_conviction': min_conviction,
            'min_posts': min_posts,
            'min_post_score': min_post_score,
            'post_limit': post_limit,
            'cex_filter': cex_filter,
            'dex_filter': dex_filter,
        },
    )


def render_watchlist_markdown(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append('# Binance Square Scanner Watchlist')
    lines.append('')
    summary = payload.get('summary') or {}
    lines.append(f"- fetched_posts: {summary.get('fetched_posts', 0)}")
    lines.append(f"- trade_candidates: {summary.get('trade_candidates', 0)}")
    lines.append(f"- passed_price_filter: {summary.get('passed_price_filter', 0)}")
    lines.append('')

    watchlist = payload.get('passed_candidate_watchlist') or []
    if not watchlist:
        lines.append('## No passed candidates')
        return '\n'.join(lines)

    for idx, item in enumerate(watchlist, start=1):
        market = item.get('market_snapshot') or {}
        top_posts = item.get('top_posts') or []
        top_post = top_posts[0] if top_posts else {}
        plan = top_post.get('trade_plan') or {}
        lines.append(f"## {idx}. {item.get('symbol')}")
        lines.append(f"- bias: {item.get('bias')}")
        lines.append(f"- conviction: {item.get('conviction_score')} ({item.get('confidence_band')})")
        lines.append(f"- market: {market.get('market_type')}")
        if market.get('chain_id'):
            lines.append(f"- venue: {market.get('chain_id')} / {market.get('dex_id')}")
        lines.append(f"- last_price: {market.get('last_price')}")
        lines.append(f"- 24h_change: {market.get('price_change_pct_24h')}")
        lines.append(f"- 24h_volume: {market.get('quote_volume_24h')}")
        if market.get('liquidity_usd') is not None:
            lines.append(f"- liquidity: {market.get('liquidity_usd')}")
        if plan.get('direction'):
            lines.append(f"- direction: {plan.get('direction')}")
        if plan.get('entry'):
            lines.append(f"- entry: {plan.get('entry')}")
        if plan.get('stop_loss'):
            lines.append(f"- stop_loss: {plan.get('stop_loss')}")
        if plan.get('take_profits'):
            lines.append(f"- take_profits: {', '.join(plan.get('take_profits'))}")
        if plan.get('leverage'):
            lines.append(f"- leverage: {plan.get('leverage')}")
        lines.append(f"- note: {item.get('scanner_note')}")
        lines.append(f"- source_post: {top_post.get('web_link')}")
        lines.append(f"- preview: {top_post.get('body_preview')}")
        lines.append('')

    lines.append('---')
    lines.append('')
    lines.append('## Detailed Source Posts')
    lines.append('')
    for idx, item in enumerate(watchlist, start=1):
        lines.append(f"### {idx}. {item.get('symbol')}")
        for post_idx, post in enumerate(item.get('top_posts') or [], start=1):
            plan = post.get('trade_plan') or {}
            lines.append(f"- Post {post_idx}: {post.get('author')} (@{post.get('username')})")
            lines.append(f"  - signal_score: {post.get('signal_score')}")
            lines.append(f"  - link: {post.get('web_link')}")
            lines.append(f"  - preview: {post.get('body_preview')}")
            if plan.get('direction'):
                lines.append(f"  - direction: {plan.get('direction')}")
            if plan.get('entry'):
                lines.append(f"  - entry: {plan.get('entry')}")
            if plan.get('stop_loss'):
                lines.append(f"  - stop_loss: {plan.get('stop_loss')}")
            if plan.get('take_profits'):
                lines.append(f"  - take_profits: {', '.join(plan.get('take_profits'))}")
            if plan.get('leverage'):
                lines.append(f"  - leverage: {plan.get('leverage')}")
            lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


def save_payload(payload: Dict[str, Any], output: Optional[str]) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'binance_square_scanner_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    md_path = path.with_suffix('.md')
    md_path.write_text(render_watchlist_markdown(payload), encoding='utf-8')
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='Binance Square 信号扫描器原型')
    sub = parser.add_subparsers(dest='command')

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--min-conviction', type=float, default=45.0)
    common.add_argument('--min-posts', type=int, default=2)
    common.add_argument('--min-post-score', type=float, default=60.0)
    common.add_argument('--post-limit', type=int, default=20)
    common.add_argument('--cex-min-quote-volume', type=float, default=5_000_000)
    common.add_argument('--cex-max-spread-pct', type=float, default=0.35)
    common.add_argument('--dex-min-quote-volume', type=float, default=1_000_000)
    common.add_argument('--dex-min-liquidity-usd', type=float, default=250_000)
    common.add_argument('--dex-min-txns-24h', type=float, default=120)
    common.add_argument('--output')

    live_parser = sub.add_parser('live', parents=[common], help='实时抓取并扫描')
    live_parser.add_argument('--limit', type=int, default=80)
    live_parser.add_argument('--start-page', type=int, default=1)
    live_parser.add_argument('--max-pages', type=int, default=6)

    replay_parser = sub.add_parser('replay', parents=[common], help='对本地样本做回放')
    replay_parser.add_argument('--input', required=True, help='原始帖子样本 JSON 文件')

    args = parser.parse_args()
    if not args.command:
        args = parser.parse_args(['live', *sys.argv[1:]])

    cex_filter = default_cex_filter_config()
    cex_filter['min_quote_volume_24h'] = args.cex_min_quote_volume
    cex_filter['max_spread_pct'] = args.cex_max_spread_pct

    dex_filter = default_dex_filter_config()
    dex_filter['min_quote_volume_24h'] = args.dex_min_quote_volume
    dex_filter['min_liquidity_usd'] = args.dex_min_liquidity_usd
    dex_filter['min_txns_24h'] = args.dex_min_txns_24h

    if args.command == 'replay':
        payload = run_replay(
            sample_path=args.input,
            min_conviction=args.min_conviction,
            min_posts=args.min_posts,
            min_post_score=args.min_post_score,
            post_limit=args.post_limit,
            cex_filter=cex_filter,
            dex_filter=dex_filter,
        )
    else:
        payload = run_scan(
            limit=args.limit,
            start_page=args.start_page,
            max_pages=args.max_pages,
            min_conviction=args.min_conviction,
            min_posts=args.min_posts,
            min_post_score=args.min_post_score,
            post_limit=args.post_limit,
            cex_filter=cex_filter,
            dex_filter=dex_filter,
        )
    out = save_payload(payload, args.output)
    print(out)
    print(json.dumps({
        'summary': payload['summary'],
        'top_trade_candidates': payload['passed_trade_candidates'][:5],
        'passed_candidate_watchlist': payload['passed_candidate_watchlist'][:3],
        'top_watch_posts': payload['watch_posts'][:3],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
