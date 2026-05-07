#!/usr/bin/env python3
import argparse
import json
import math
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from bsc.constants import RESULTS_DIR


from bsc.data.strategy import score_post, aggregate_coin_signals
from bsc.data.tool import scrape_square
from bsc.monitors.heat import extract_square_mentions, fetch_coingecko_trending, fetch_binance_futures_movers, build_heat_ranking

OI_HIST_API = 'https://fapi.binance.com/futures/data/openInterestHist'
FUTURES_TICKER_API = 'https://fapi.binance.com/fapi/v1/ticker/24hr'
SPOT_TICKER_API = 'https://api.binance.com/api/v3/ticker/24hr'
STABLE_NOISE = {'USD', 'USDT', 'USDC', 'BUSD', 'FDUSD', 'TUSD', 'DAI', 'USDE', 'PYUSD'}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def normalize_symbol(symbol: str) -> str:
    return (symbol or '').strip().upper().replace('/', '')


def to_futures_symbol(symbol: str) -> Optional[str]:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return None
    if symbol.endswith('USDT'):
        base = symbol[:-4]
    else:
        base = symbol
    if not base or base in STABLE_NOISE:
        return None
    return f'{base}USDT'


def score_square_heat(coin_signals: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for row in coin_signals:
        symbol = row.get('symbol')
        if not symbol:
            continue
        rows[symbol] = {
            'symbol': symbol,
            'square_conviction_score': round(float(row.get('conviction_score') or 0), 2),
            'square_avg_signal_score': round(float(row.get('avg_signal_score') or 0), 2),
            'square_post_count': int(row.get('post_count') or 0),
            'square_unique_authors': int(row.get('unique_authors') or 0),
            'square_total_engagement': int(row.get('total_engagement') or 0),
            'square_bias': row.get('bias'),
            'sample_post_ids': row.get('sample_post_ids') or [],
        }
    return rows


def fetch_oi_hist(symbol: str, period: str = '5m', limit: int = 12) -> List[Dict[str, Any]]:
    resp = requests.get(OI_HIST_API, params={'symbol': symbol, 'period': period, 'limit': limit}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or len(data) < 2:
        raise ValueError(f'not_enough_oi_history:{symbol}')
    return data


def fetch_futures_snapshot(symbol: str) -> Dict[str, Any]:
    resp = requests.get(FUTURES_TICKER_API, params={'symbol': symbol}, timeout=20)
    if resp.status_code == 400:
        raise ValueError(f'futures_symbol_not_supported:{symbol}')
    resp.raise_for_status()
    return resp.json()


def fetch_spot_snapshot(symbol: str) -> Optional[Dict[str, Any]]:
    resp = requests.get(SPOT_TICKER_API, params={'symbol': symbol}, timeout=20)
    if resp.status_code == 400:
        return None
    resp.raise_for_status()
    return resp.json()


def compute_timing_signal(symbol: str) -> Dict[str, Any]:
    hist = fetch_oi_hist(symbol)
    latest = hist[-1]
    previous = hist[0]
    oi_latest = safe_float(latest.get('sumOpenInterest'))
    oi_prev = safe_float(previous.get('sumOpenInterest'))
    oi_latest_value = safe_float(latest.get('sumOpenInterestValue'))
    oi_prev_value = safe_float(previous.get('sumOpenInterestValue'))
    oi_change_pct = ((oi_latest - oi_prev) / oi_prev * 100) if oi_prev > 0 else 0.0
    oi_value_change_pct = ((oi_latest_value - oi_prev_value) / oi_prev_value * 100) if oi_prev_value > 0 else 0.0

    fut = fetch_futures_snapshot(symbol)
    price_change_pct_24h = safe_float(fut.get('priceChangePercent'))
    last_price = safe_float(fut.get('lastPrice'))
    quote_volume = safe_float(fut.get('quoteVolume'))
    count_24h = safe_float(fut.get('count'))
    weighted = safe_float(fut.get('weightedAvgPrice'))
    momentum_pct = ((last_price / weighted - 1) * 100) if weighted > 0 else 0.0
    divergence_ratio = oi_change_pct / max(abs(price_change_pct_24h), 0.75)

    timing_score = 0.0
    timing_score += min(max(oi_change_pct, 0.0) * 2.0, 32)
    timing_score += min(max(oi_value_change_pct, 0.0) * 1.5, 16)
    timing_score += min(max(divergence_ratio, 0.0) * 8.0, 20)
    timing_score += min(math.log1p(max(quote_volume, 0.0)) * 2.0, 10)
    if -4.0 <= price_change_pct_24h <= 18.0:
        timing_score += 8
    elif -10.0 <= price_change_pct_24h < -4.0:
        timing_score += 2
    if -3.0 <= momentum_pct <= 12.0:
        timing_score += 6
    elif momentum_pct > 20.0:
        timing_score -= 8
    if price_change_pct_24h < -20.0:
        timing_score -= 25
    elif price_change_pct_24h > 45.0:
        timing_score -= 18
    if oi_change_pct < 0:
        timing_score -= 10
    timing_score = clamp(timing_score)

    direction_bias = 'long' if oi_change_pct > 0 and price_change_pct_24h > -6.0 else 'mixed'
    if price_change_pct_24h < -12.0:
        direction_bias = 'short_or_noise'
    if price_change_pct_24h > 35.0 and momentum_pct > 20.0:
        direction_bias = 'overheated'

    return {
        'symbol': symbol,
        'oi_change_pct': round(oi_change_pct, 2),
        'oi_value_change_pct': round(oi_value_change_pct, 2),
        'price_change_pct_24h': round(price_change_pct_24h, 2),
        'divergence_ratio': round(divergence_ratio, 2),
        'timing_score': round(timing_score, 2),
        'direction_bias': direction_bias,
        'last_price': last_price,
        'quote_volume_24h': quote_volume,
        'count_24h': int(count_24h),
        'momentum_vs_vwap_pct': round(momentum_pct, 2),
        'open_interest': oi_latest,
        'open_interest_value': oi_latest_value,
        'market_type': 'binance_futures',
        'venue': 'binance_futures',
    }


def build_reason_summary(square: Optional[Dict[str, Any]], heat: Optional[Dict[str, Any]], timing: Optional[Dict[str, Any]]) -> str:
    reasons: List[str] = []
    if square and square.get('square_post_count', 0) > 0:
        reasons.append(f"广场提及 {square.get('square_post_count')} 帖, {square.get('square_unique_authors')} 位作者")
    if square and square.get('square_bias') == 'long_watch':
        reasons.append('广场情绪偏多')
    if heat and heat.get('coingecko_trending_flag'):
        reasons.append(f"CoinGecko 热榜 #{heat.get('coingecko_rank')}")
    if heat and heat.get('futures_movers_flag'):
        reasons.append(f"Binance 合约异动榜 #{heat.get('futures_rank')}")
    if timing and timing.get('oi_change_pct', 0) > 0:
        reasons.append(f"OI {timing.get('oi_change_pct')}% / 价格 {timing.get('price_change_pct_24h')}%")
    if timing and timing.get('direction_bias') == 'overheated':
        reasons.append('短线过热, 谨慎追高')
    if timing and timing.get('direction_bias') == 'short_or_noise':
        reasons.append('更像下跌或噪音热度')
    return '，'.join(reasons) if reasons else '热度或资金信号不足'


def build_hotspot_payload(limit: int, start_page: int, max_pages: int, top_n: int) -> Dict[str, Any]:
    raw_posts = scrape_square(limit=limit, start_page=start_page, max_pages=max_pages)
    scored_posts = [score_post(post) for post in raw_posts if not post.get('error')]
    coin_signals = aggregate_coin_signals(scored_posts)
    square_scores = score_square_heat(coin_signals)

    square_mentions = extract_square_mentions(raw_posts)
    trending = fetch_coingecko_trending()
    movers = fetch_binance_futures_movers()
    heat_ranking = build_heat_ranking(square_mentions, trending, movers)
    heat_by_symbol = {f"{normalize_symbol(row.get('symbol'))}USDT": row for row in heat_ranking if row.get('symbol')}

    candidate_symbols = set(square_scores.keys()) | set(heat_by_symbol.keys()) | {f"{normalize_symbol(k)}USDT" for k in trending.keys()} | {f"{normalize_symbol(k)}USDT" for k in movers.keys()}

    timing_rows = []
    timing_by_symbol: Dict[str, Dict[str, Any]] = {}
    for symbol in sorted(candidate_symbols):
        fut_symbol = to_futures_symbol(symbol)
        if not fut_symbol:
            continue
        try:
            row = compute_timing_signal(fut_symbol)
            timing_rows.append(row)
            timing_by_symbol[fut_symbol] = row
        except Exception:
            continue

    timing_rows.sort(key=lambda x: x.get('timing_score', 0), reverse=True)
    for idx, row in enumerate(timing_rows, start=1):
        row['timing_rank'] = idx

    radar_rows = []
    union_symbols = sorted(set(candidate_symbols) | set(timing_by_symbol.keys()))
    for symbol in union_symbols:
        square = square_scores.get(symbol)
        heat = heat_by_symbol.get(symbol)
        timing = timing_by_symbol.get(symbol)
        if not square and not heat and not timing:
            continue

        heat_score = 0.0
        market_type = 'unknown'
        venue = None
        if heat:
            heat_score = max(heat_score, safe_float(heat.get('heat_score')))
            if heat.get('futures_movers_flag'):
                market_type = 'binance_futures'
                venue = 'binance_futures'
        if square:
            heat_score = max(heat_score, safe_float(square.get('square_conviction_score')))
        if timing:
            market_type = timing.get('market_type') or market_type
            venue = timing.get('venue') or venue

        square_bias = (square or {}).get('square_bias')
        timing_score = safe_float((timing or {}).get('timing_score'))
        direction_bias = (timing or {}).get('direction_bias')

        buy_opportunity_score = heat_score * 0.45 + timing_score * 0.55
        if square_bias == 'long_watch':
            buy_opportunity_score += 10
        elif square_bias == 'short_watch':
            buy_opportunity_score -= 20
        if direction_bias == 'overheated':
            buy_opportunity_score -= 18
        elif direction_bias == 'short_or_noise':
            buy_opportunity_score -= 35
        elif direction_bias == 'long':
            buy_opportunity_score += 8
        final_score = round(clamp(buy_opportunity_score), 2)

        if square_bias == 'long_watch' and timing_score >= 40 and direction_bias == 'long':
            stage = '可关注买入'
        elif square_bias == 'long_watch' and heat_score >= 65:
            stage = '热度偏多'
        elif direction_bias == 'overheated':
            stage = '过热观察'
        elif direction_bias == 'short_or_noise' or square_bias == 'short_watch':
            stage = '回避'
        elif timing_score >= 45:
            stage = '资金异动观察'
        else:
            stage = '观察'

        spot_snapshot = fetch_spot_snapshot(symbol) if symbol.endswith('USDT') else None
        quote_volume_24h = safe_float((timing or {}).get('quote_volume_24h'))
        oi_change_pct = safe_float((timing or {}).get('oi_change_pct'))
        oi_value_change_pct = safe_float((timing or {}).get('oi_value_change_pct'))
        spot_quote_volume_24h = safe_float((spot_snapshot or {}).get('quoteVolume')) if spot_snapshot else 0.0
        market_quality_ok = quote_volume_24h >= 8_000_000 or spot_quote_volume_24h >= 3_000_000
        strong_capital_confirmed = bool(
            square_bias == 'long_watch'
            and direction_bias == 'long'
            and oi_change_pct >= 0.15
            and oi_value_change_pct >= 0.3
            and market_quality_ok
            and final_score >= 55
        )

        radar_rows.append({
            'symbol': symbol,
            'square_heat': square,
            'external_heat': heat,
            'timing_signal': timing,
            'spot_market_snapshot': {
                'last_price': safe_float((spot_snapshot or {}).get('lastPrice')),
                'price_change_pct_24h': safe_float((spot_snapshot or {}).get('priceChangePercent')),
                'quote_volume_24h': spot_quote_volume_24h,
            } if spot_snapshot else None,
            'heat_score': round(heat_score, 2),
            'timing_score': round(timing_score, 2),
            'final_score': final_score,
            'market_type': market_type,
            'venue': venue,
            'stage': stage,
            'is_buy_candidate': stage in {'可关注买入', '热度偏多', '资金异动观察'},
            'strong_capital_confirmed': strong_capital_confirmed,
            'reason_summary': build_reason_summary(square, heat, timing),
        })

    radar_rows.sort(key=lambda x: (x['is_buy_candidate'], x['final_score'], x['timing_score'], x['heat_score']), reverse=True)
    for idx, row in enumerate(radar_rows, start=1):
        row['priority_rank'] = idx

    return {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'dual_engine_hotspot_radar',
            'limit': limit,
            'start_page': start_page,
            'max_pages': max_pages,
            'top_n': top_n,
        },
        'summary': {
            'fetched_posts': len(raw_posts),
            'scored_posts': len(scored_posts),
            'square_coin_signals': len(coin_signals),
            'heat_symbols': len(heat_ranking),
            'timing_symbols': len(timing_rows),
            'radar_candidates': len(radar_rows),
        },
        'top_opportunities': radar_rows[:top_n],
        'buy_candidates': [row for row in radar_rows if row.get('is_buy_candidate')][:top_n],
        'strong_capital_buy_candidates': [row for row in radar_rows if row.get('strong_capital_confirmed')][:top_n],
        'radar_candidates': radar_rows,
        'heat_ranking': heat_ranking[:top_n],
        'timing_ranking': timing_rows[:top_n],
    }


def render_markdown(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append('# Hotspot Radar')
    lines.append('')
    summary = payload.get('summary') or {}
    lines.append(f"- fetched_posts: **{summary.get('fetched_posts', 0)}**")
    lines.append(f"- square_coin_signals: **{summary.get('square_coin_signals', 0)}**")
    lines.append(f"- timing_symbols: **{summary.get('timing_symbols', 0)}**")
    lines.append(f"- radar_candidates: **{summary.get('radar_candidates', 0)}**")
    lines.append('')
    lines.append('## Strong capital confirmed buy candidates')
    lines.append('')
    for row in payload.get('strong_capital_buy_candidates') or []:
        timing = row.get('timing_signal') or {}
        lines.append(f"### {row.get('priority_rank')}. {row.get('symbol')} [{row.get('stage')}]")
        lines.append(f"- final_score: `{row.get('final_score')}`")
        lines.append(f"- heat_score: `{row.get('heat_score')}`")
        lines.append(f"- timing_score: `{row.get('timing_score')}`")
        lines.append(f"- market_type: `{row.get('market_type')}`")
        lines.append(f"- reason: {row.get('reason_summary')}")
        if timing:
            lines.append(f"- OI change: `{timing.get('oi_change_pct')}%` | price 24h: `{timing.get('price_change_pct_24h')}%` | divergence: `{timing.get('divergence_ratio')}`")
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def save_payload(payload: Dict[str, Any], output: Optional[str]) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'hotspot_radar_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    path.with_suffix('.md').write_text(render_markdown(payload), encoding='utf-8')
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='热点币机会雷达, 方案 B: 广场热度 + 资金异动')
    parser.add_argument('--limit', type=int, default=80)
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--max-pages', type=int, default=6)
    parser.add_argument('--top-n', type=int, default=10)
    parser.add_argument('--output')
    args = parser.parse_args()

    payload = build_hotspot_payload(
        limit=args.limit,
        start_page=args.start_page,
        max_pages=args.max_pages,
        top_n=args.top_n,
    )
    out = save_payload(payload, args.output)
    print(out)
    print(json.dumps({
        'summary': payload['summary'],
        'strong_capital_buy_candidates': payload['strong_capital_buy_candidates'][:5],
        'buy_candidates': payload['buy_candidates'][:5],
        'top_opportunities': payload['top_opportunities'][:5],
        'timing_ranking': payload['timing_ranking'][:5],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
