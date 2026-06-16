#!/usr/bin/env python3
import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from bsc.constants import RESULTS_DIR
from bsc.data.tool import scrape_square

KLINE_API = 'https://api.binance.com/api/v3/klines'
FUTURES_KLINE_API = 'https://fapi.binance.com/fapi/v1/klines'
DEFAULT_INTERVAL = '5m'
DEFAULT_WINDOWS = [3, 12, 48]  # 15m, 1h, 4h on 5m bars
TEXT_BULLISH = {
    'long', 'buy', 'bull', 'bullish', 'breakout', 'moon', 'pump', 'strong', 'continue', 'continuation'
}
TEXT_BEARISH = {
    'short', 'sell', 'bear', 'bearish', 'dump', 'crash', 'weak', 'rejection', 'stall', 'down'
}


@dataclass
class PriceEventResult:
    entry_price: Optional[float]
    forward_return_pct: Dict[str, Optional[float]]
    max_favorable_pct: Dict[str, Optional[float]]
    max_adverse_pct: Dict[str, Optional[float]]


def parse_publish_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        ts = float(text)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if 'ago' in text.lower():
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except Exception:
        return None


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def log1p_score(value: int, scale: float) -> float:
    if value <= 0:
        return 0.0
    return clamp(math.log1p(value) * scale)


def infer_tone(body: str, tendency: Any) -> Tuple[str, float]:
    if tendency == 1:
        return 'bullish', 1.0
    if tendency == 2:
        return 'bearish', 1.0

    lowered = (body or '').lower()
    bull_hits = sum(1 for token in TEXT_BULLISH if token in lowered)
    bear_hits = sum(1 for token in TEXT_BEARISH if token in lowered)
    if bull_hits > bear_hits:
        return 'bullish', min(1.0, 0.5 + 0.1 * (bull_hits - bear_hits))
    if bear_hits > bull_hits:
        return 'bearish', min(1.0, 0.5 + 0.1 * (bear_hits - bull_hits))
    return 'neutral', 0.25


def extract_symbol_candidates(post: Dict[str, Any]) -> List[str]:
    found: List[str] = []
    for symbol in post.get('trading_pairs') or []:
        if isinstance(symbol, str) and symbol:
            found.append(symbol.upper())
    for symbol in post.get('symbols') or []:
        if isinstance(symbol, str) and symbol:
            found.append(symbol.upper())
    body = post.get('body') or ''
    for token in re.findall(r'\b([A-Z]{2,15}USDT)\b', body.upper()):
        found.append(token)
    for token in re.findall(r'\$\s*([A-Z0-9]{2,15})\b', body.upper()):
        found.append(f'{token}USDT')
    return list(dict.fromkeys(found))


def score_post(post: Dict[str, Any]) -> Dict[str, Any]:
    body = post.get('body') or ''
    tone, tone_conf = infer_tone(body, post.get('tendency'))
    like_count = safe_int(post.get('like_count'))
    comment_count = safe_int(post.get('comment_count'))
    share_count = safe_int(post.get('share_count'))
    view_count = safe_int(post.get('view_count'))
    bookmark_count = safe_int(post.get('bookmark_count'))

    engagement_score = clamp(
        log1p_score(like_count, 8.0)
        + log1p_score(comment_count, 10.0)
        + log1p_score(share_count, 12.0)
        + log1p_score(bookmark_count, 10.0)
        + log1p_score(view_count, 4.0),
        0,
        35,
    )

    specificity_score = 0.0
    if post.get('trading_pairs'):
        specificity_score += 10
    if re.search(r'\b(tp|sl|entry|stop loss|take profit)\b', body, re.I):
        specificity_score += 10
    if re.search(r'\d', body):
        specificity_score += 5
    specificity_score = clamp(specificity_score, 0, 25)

    quality_score = 0.0
    if len(body) >= 80:
        quality_score += 8
    if len(body) >= 200:
        quality_score += 6
    if len(set((post.get('tags') or []))) > 0:
        quality_score += 4
    if post.get('author'):
        quality_score += 4
    quality_score = clamp(quality_score, 0, 20)

    confidence_score = clamp(tone_conf * 20, 0, 20)
    total_score = round(engagement_score + specificity_score + quality_score + confidence_score, 2)

    if total_score >= 75:
        label = 'high_signal'
    elif total_score >= 55:
        label = 'watch'
    else:
        label = 'noise'

    return {
        **post,
        'symbol_candidates': extract_symbol_candidates(post),
        'tone': tone,
        'tone_confidence': round(tone_conf, 3),
        'score_engagement': round(engagement_score, 2),
        'score_specificity': round(specificity_score, 2),
        'score_quality': round(quality_score, 2),
        'score_confidence': round(confidence_score, 2),
        'signal_score': total_score,
        'signal_label': label,
    }


def aggregate_coin_signals(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bucket: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for post in posts:
        for symbol in post.get('symbol_candidates') or []:
            bucket[symbol].append(post)

    rows: List[Dict[str, Any]] = []
    for symbol, items in bucket.items():
        tones = Counter(item.get('tone') for item in items)
        mean_score = statistics.mean(item.get('signal_score', 0) for item in items)
        unique_authors = len({item.get('username') or item.get('author') for item in items})
        total_engagement = sum(safe_int(item.get('engagement_total')) for item in items)
        bullish = tones.get('bullish', 0)
        bearish = tones.get('bearish', 0)
        if bullish > bearish:
            bias = 'long_watch'
        elif bearish > bullish:
            bias = 'short_watch'
        else:
            bias = 'no_trade'

        conviction = clamp(mean_score * 0.55 + unique_authors * 6 + math.log1p(total_engagement) * 6, 0, 100)
        if conviction < 45:
            bias = 'no_trade'

        rows.append({
            'symbol': symbol,
            'post_count': len(items),
            'unique_authors': unique_authors,
            'bullish_posts': bullish,
            'bearish_posts': bearish,
            'neutral_posts': tones.get('neutral', 0),
            'avg_signal_score': round(mean_score, 2),
            'total_engagement': total_engagement,
            'conviction_score': round(conviction, 2),
            'bias': bias,
            'sample_post_ids': [item.get('content_id') for item in items[:5]],
        })
    rows.sort(key=lambda x: (x['conviction_score'], x['post_count']), reverse=True)
    return rows


def fetch_klines(symbol: str, interval: str, limit: int = 500) -> List[List[Any]]:
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    try:
        resp = requests.get(KLINE_API, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as spot_exc:
        futures_resp = requests.get(FUTURES_KLINE_API, params=params, timeout=30)
        try:
            futures_resp.raise_for_status()
        except Exception as futures_exc:
            raise RuntimeError(f'spot_error={spot_exc}; usdt_futures_error={futures_exc}') from futures_exc
        return futures_resp.json()


def price_event_study(symbol: str, event_time: Optional[datetime], interval: str, windows: List[int]) -> PriceEventResult:
    klines = fetch_klines(symbol, interval=interval, limit=max(500, max(windows) + 50))
    candles = []
    for row in klines:
        open_ts = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)
        candles.append({
            'time': open_ts,
            'open': float(row[1]),
            'high': float(row[2]),
            'low': float(row[3]),
            'close': float(row[4]),
        })
    if not candles:
        return PriceEventResult(None, {}, {}, {})

    if event_time is None:
        idx = max(0, len(candles) - max(windows) - 2)
    else:
        idx = next((i for i, c in enumerate(candles) if c['time'] >= event_time), len(candles) - max(windows) - 2)
        idx = min(max(0, idx), len(candles) - 2)

    entry = candles[idx]['close']
    forward_return_pct: Dict[str, Optional[float]] = {}
    max_favorable_pct: Dict[str, Optional[float]] = {}
    max_adverse_pct: Dict[str, Optional[float]] = {}

    for window in windows:
        end_idx = min(len(candles) - 1, idx + window)
        segment = candles[idx + 1:end_idx + 1]
        label = f'{window}_{interval}'
        if not segment:
            forward_return_pct[label] = None
            max_favorable_pct[label] = None
            max_adverse_pct[label] = None
            continue
        final_close = segment[-1]['close']
        forward_return_pct[label] = round((final_close / entry - 1) * 100, 4)
        max_favorable_pct[label] = round((max(c['high'] for c in segment) / entry - 1) * 100, 4)
        max_adverse_pct[label] = round((min(c['low'] for c in segment) / entry - 1) * 100, 4)

    return PriceEventResult(entry, forward_return_pct, max_favorable_pct, max_adverse_pct)


def backtest_posts(posts: List[Dict[str, Any]], interval: str, windows: List[int]) -> Dict[str, Any]:
    studied = []
    grouped_returns: Dict[str, List[float]] = defaultdict(list)
    grouped_mfe: Dict[str, List[float]] = defaultdict(list)
    grouped_mae: Dict[str, List[float]] = defaultdict(list)

    for post in posts:
        if post.get('signal_label') == 'noise':
            continue
        symbols = post.get('symbol_candidates') or []
        if not symbols:
            continue
        symbol = symbols[0]
        try:
            event_time = parse_publish_time(post.get('publish_time'))
            result = price_event_study(symbol, event_time, interval=interval, windows=windows)
            row = {
                'content_id': post.get('content_id'),
                'symbol': symbol,
                'tone': post.get('tone'),
                'signal_score': post.get('signal_score'),
                'signal_label': post.get('signal_label'),
                'entry_price': result.entry_price,
                'forward_return_pct': result.forward_return_pct,
                'max_favorable_pct': result.max_favorable_pct,
                'max_adverse_pct': result.max_adverse_pct,
            }
            studied.append(row)
            for label, value in result.forward_return_pct.items():
                if value is not None:
                    grouped_returns[f"{post.get('tone')}::{label}"].append(value)
            for label, value in result.max_favorable_pct.items():
                if value is not None:
                    grouped_mfe[f"{post.get('tone')}::{label}"].append(value)
            for label, value in result.max_adverse_pct.items():
                if value is not None:
                    grouped_mae[f"{post.get('tone')}::{label}"].append(value)
        except Exception as exc:
            studied.append({
                'content_id': post.get('content_id'),
                'symbol': symbol,
                'error': str(exc),
            })

    summary = {}
    for key, values in grouped_returns.items():
        summary[key] = {
            'count': len(values),
            'avg_return_pct': round(statistics.mean(values), 4),
            'median_return_pct': round(statistics.median(values), 4),
            'win_rate_pct': round(sum(1 for v in values if v > 0) / len(values) * 100, 2),
            'avg_mfe_pct': round(statistics.mean(grouped_mfe.get(key, [0.0])), 4),
            'avg_mae_pct': round(statistics.mean(grouped_mae.get(key, [0.0])), 4),
        }

    return {
        'interval': interval,
        'windows': windows,
        'studied_posts': studied,
        'summary': summary,
    }


def save_json(payload: Dict[str, Any], prefix: str, output: Optional[str] = None) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'{prefix}_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(path)


def load_posts(path: str) -> List[Dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get('posts'), list):
        return payload['posts']
    raise ValueError('unsupported input format')


def cmd_score(args: argparse.Namespace) -> None:
    if args.input:
        posts = load_posts(args.input)
    else:
        posts = scrape_square(limit=args.limit, start_page=args.start_page, max_pages=args.max_pages)
    scored = [score_post(post) for post in posts if not post.get('error')]
    coins = aggregate_coin_signals(scored)
    payload = {
        'generated_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'post_count': len(scored),
        'posts': scored,
        'coin_signals': coins,
    }
    out = save_json(payload, 'binance_square_scored', args.output)
    print(out)
    print(json.dumps({'top_coin_signals': coins[:5], 'sample_posts': scored[:3]}, ensure_ascii=False, indent=2))


def cmd_backtest(args: argparse.Namespace) -> None:
    posts = [score_post(post) for post in load_posts(args.input) if not post.get('error')]
    result = backtest_posts(posts, interval=args.interval, windows=[int(x) for x in args.windows.split(',') if x.strip()])
    payload = {
        'generated_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'input': args.input,
        'result': result,
    }
    out = save_json(payload, 'binance_square_backtest', args.output)
    print(out)
    print(json.dumps(result['summary'], ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Binance Square 策略评分与回测脚手架')
    sub = parser.add_subparsers(dest='command', required=True)

    p_score = sub.add_parser('score', help='抓取或读取帖子，并生成评分结果')
    p_score.add_argument('--input', help='已有 JSON 文件路径，默认直接抓取')
    p_score.add_argument('--limit', type=int, default=50)
    p_score.add_argument('--start-page', type=int, default=1)
    p_score.add_argument('--max-pages', type=int, default=5)
    p_score.add_argument('--output')

    p_backtest = sub.add_parser('backtest', help='对评分后的帖子做价格事件回测')
    p_backtest.add_argument('--input', required=True, help='score 结果文件或帖子 JSON 文件')
    p_backtest.add_argument('--interval', default=DEFAULT_INTERVAL)
    p_backtest.add_argument('--windows', default=','.join(str(x) for x in DEFAULT_WINDOWS))
    p_backtest.add_argument('--output')

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == 'score':
        cmd_score(args)
    elif args.command == 'backtest':
        cmd_backtest(args)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
