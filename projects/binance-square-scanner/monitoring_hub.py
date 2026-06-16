#!/usr/bin/env python3
"""Monitoring Hub — 统一监控总入口（薄包装层，核心逻辑在 bsc 包中）"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from bsc.constants import RESULTS_DIR
from bsc.monitors.hotspot import build_hotspot_payload
from bsc.monitors.narrative import (
    build_narrative_posts, aggregate_narratives, build_symbol_narrative_view, enrich_post_symbols,
)
from bsc.data.tool import scrape_square
from bsc.data.blockbeats import fetch_blockbeats_news, to_narrative_format as bb_to_narrative
from bsc.data.defillama import fetch_protocols, detect_tvl_changes, to_narrative_format as dl_to_narrative
from bsc.data.twitter_kol import (
    fetch_all_news as fetch_crypto_news,
    fetch_rss_feeds as fetch_crypto_rss,
    to_narrative_format as news_to_narrative,
)
from bsc.data.technical import compute_technical_signals
from bsc.data.direction_research import (
    DB_PATH as DIRECTION_DB_PATH,
    build_direction_edge_report,
    build_direction_research_report,
    evaluate_direction_signals,
    record_direction_signals,
)

try:
    from bsc.data.twitter_6551 import fetch_kol_tweets as fetch_tw6551_kol, fetch_crypto_search as fetch_tw6551_search, to_narrative_format as tw6551_to_narrative
    HAS_TWITTER_6551 = True
except ImportError:
    HAS_TWITTER_6551 = False
from bsc.monitors.onchain import build_monitor_payload as build_onchain_payload
from bsc.monitors.meme_early import build_monitor_payload as build_meme_early_payload
from bsc.monitors.meme_early import (
    DEFAULT_TOKEN_COOLDOWN_HOURS as DEFAULT_MEME_TOKEN_COOLDOWN_HOURS,
    DEFAULT_TOKEN_MEMORY_DB as DEFAULT_MEME_TOKEN_MEMORY_DB,
    DEFAULT_MEME_RESEARCH_DB,
    evaluate_meme_signals,
    load_meme_extra_candidate_files,
    parse_hour_windows,
    record_meme_signals,
    summarize_meme_research,
)
from bsc.monitors.funding_rate import build_monitor_payload as build_futures_payload
from bsc.monitors.macro import build_macro_payload
from bsc.monitors.announcement import build_announcement_payload


DEFAULT_TECHNICAL_SYMBOLS = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP']
STABLE_SYMBOLS = {'USD', 'USDT', 'USDC', 'BUSD', 'FDUSD', 'TUSD', 'DAI', 'USDE', 'PYUSD'}
MARKET_REGIME_ANCHORS = {'BTCUSDT': 0.65, 'ETHUSDT': 0.35}
SOURCE_CONFIRMATION_CORE_SOURCES = (
    'technical',
    'narrative_news',
    'futures_capital_flow',
    'announcements',
    'macro_news',
)
SOURCE_RELIABILITY_PROFILES: Dict[str, Dict[str, Any]] = {
    'theblock': {
        'display_name': 'The Block RSS',
        'source_type': 'editorial_news',
        'base_weight': 1.10,
        'accuracy_status': 'source_reputation_only',
    },
    'cointelegraph': {
        'display_name': 'CoinTelegraph RSS',
        'source_type': 'editorial_news',
        'base_weight': 1.05,
        'accuracy_status': 'source_reputation_only',
    },
    'blockbeats': {
        'display_name': 'BlockBeats Newsflash',
        'source_type': 'newsflash',
        'base_weight': 1.00,
        'accuracy_status': 'source_reputation_only',
    },
    'defillama': {
        'display_name': 'DefiLlama TVL',
        'source_type': 'market_data',
        'base_weight': 1.12,
        'accuracy_status': 'data_source_not_direction_backtested',
    },
    'binance_square': {
        'display_name': 'Binance Square',
        'source_type': 'social_posts',
        'base_weight': 0.82,
        'accuracy_status': 'author_dependent',
    },
    'twitter_6551': {
        'display_name': '6551 Twitter/KOL',
        'source_type': 'social_search',
        'base_weight': 0.78,
        'accuracy_status': 'author_dependent',
    },
    'web_search': {
        'display_name': 'Web Search',
        'source_type': 'search_result',
        'base_weight': 0.72,
        'accuracy_status': 'unverified_search_result',
    },
    'unknown': {
        'display_name': 'Unknown',
        'source_type': 'unknown',
        'base_weight': 0.70,
        'accuracy_status': 'unverified',
    },
}
SOURCE_MIN_WEIGHT = 0.55
SOURCE_MAX_WEIGHT = 1.30
SIGNAL_CONFLICT_MIN_ABS_POINTS = 5.0
SIGNAL_CONFLICT_MAX_PENALTY = 18.0
SIGNAL_CONFLICT_PENALTY_RATIO = 0.65
NARRATIVE_UNKNOWN_TIME_WEIGHT = 0.85
NARRATIVE_MIN_FRESHNESS_WEIGHT = 0.65
DEFAULT_EVENT_MEMORY_DB = RESULTS_DIR / 'direction_event_memory.sqlite3'
DEFAULT_EVENT_COOLDOWN_HOURS = 48
EVENT_COOLDOWN_MIN_WEIGHT = 0.45
EVENT_COOLDOWN_REPEAT_STEP = 0.20
DEDUP_STOPWORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'has', 'have',
    'in', 'is', 'it', 'its', 'of', 'on', 'or', 'that', 'the', 'this', 'to', 'with',
    'via', 'into', 'over', 'after', 'before', 'will', 'new', 'says', 'said',
}
EVENT_MEMORY_SCHEMA = '''
CREATE TABLE IF NOT EXISTS narrative_event_memory (
    event_key TEXT PRIMARY KEY,
    event_identity TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_count INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    body_preview TEXT
);

CREATE INDEX IF NOT EXISTS idx_narrative_event_memory_last_seen
ON narrative_event_memory(last_seen_at);
'''


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_content_time(value: Any, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(UTC)
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc_datetime(value)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)

    text = str(value).strip()
    if not text:
        return None

    lowered = text.lower()
    if lowered in {'now', 'just now'} or text == '刚刚':
        return now
    if lowered == 'yesterday' or text == '昨天':
        return now - timedelta(days=1)

    relative = re.search(
        r'\b(?P<num>\d+(?:\.\d+)?|a|an|one)\s*'
        r'(?P<unit>seconds?|secs?|sec|s|minutes?|mins?|min|m|hours?|hrs?|hr|h|days?|day|d|weeks?|week|w)'
        r'\s*ago\b',
        lowered,
    )
    if relative:
        raw_amount = relative.group('num')
        amount = 1.0 if raw_amount in {'a', 'an', 'one'} else float(raw_amount)
        unit = relative.group('unit')
        if unit.startswith('s'):
            return now - timedelta(seconds=amount)
        if unit.startswith('m'):
            return now - timedelta(minutes=amount)
        if unit.startswith('h'):
            return now - timedelta(hours=amount)
        if unit.startswith('d'):
            return now - timedelta(days=amount)
        if unit.startswith('w'):
            return now - timedelta(weeks=amount)

    relative_cn = re.search(r'(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>秒|分钟|分|小时|时|天|日|周|星期)\s*(前|以前)', text)
    if relative_cn:
        amount = float(relative_cn.group('num'))
        unit = relative_cn.group('unit')
        if unit == '秒':
            return now - timedelta(seconds=amount)
        if unit in {'分钟', '分'}:
            return now - timedelta(minutes=amount)
        if unit in {'小时', '时'}:
            return now - timedelta(hours=amount)
        if unit in {'天', '日'}:
            return now - timedelta(days=amount)
        if unit in {'周', '星期'}:
            return now - timedelta(weeks=amount)

    if re.fullmatch(r'\d+(?:\.\d+)?', text):
        ts = float(text)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)

    if re.fullmatch(r'\d{1,2}:\d{2}', text):
        hour_text, minute_text = text.split(':', 1)
        try:
            candidate = now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
        except ValueError:
            return None
        if candidate > now + timedelta(minutes=15):
            candidate -= timedelta(days=1)
        return candidate

    try:
        return as_utc_datetime(datetime.fromisoformat(text.replace('Z', '+00:00')))
    except Exception:
        pass

    for fmt in ('%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M', '%Y/%m/%d'):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except Exception:
            pass

    try:
        return as_utc_datetime(parsedate_to_datetime(text))
    except Exception:
        return None


def narrative_source_key(post: Dict[str, Any]) -> str:
    source = post.get('_source') or post.get('source') or post.get('username')
    if source:
        return str(source)
    if post.get('root_content_id') or post.get('raw'):
        return 'binance_square'
    return 'unknown'


def canonical_url(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        parsed = urlsplit(text)
    except Exception:
        return text.split('?', 1)[0].rstrip('/').lower()
    query = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=False)
        if not key.lower().startswith('utm_') and key.lower() not in {'ref', 'src'}
    ]
    cleaned = parsed._replace(
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip('/'),
        query=urlencode(query),
        fragment='',
    )
    return urlunsplit(cleaned).lower()


def normalized_dedupe_text(value: Any) -> str:
    text = str(value or '').lower()
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'[^0-9a-z\u4e00-\u9fff]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def dedupe_tokens(value: Any) -> set[str]:
    normalized = normalized_dedupe_text(value)
    tokens = {
        token
        for token in normalized.split()
        if len(token) >= 3 and token not in DEDUP_STOPWORDS
    }
    return tokens


def dedupe_text_key(value: Any) -> str:
    tokens = [token for token in normalized_dedupe_text(value).split() if token not in DEDUP_STOPWORDS]
    return ' '.join(tokens[:28])


def token_jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    return intersection / union if union else 0.0


def dedupe_stats_bucket(stats: Dict[str, Any], source: str, key: str) -> None:
    bucket = stats['by_source'].setdefault(source, {
        'raw': 0,
        'kept': 0,
        'duplicates_filtered': 0,
    })
    bucket[key] += 1


def dedupe_narrative_raw_posts(
    posts: list[Dict[str, Any]],
    near_duplicate_threshold: float = 0.88,
) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
    stats: Dict[str, Any] = {
        'enabled': True,
        'raw_posts_before_dedupe': len(posts),
        'deduped_posts_after_dedupe': 0,
        'duplicate_posts_filtered': 0,
        'content_id_duplicates': 0,
        'url_duplicates': 0,
        'text_duplicates': 0,
        'near_text_duplicates': 0,
        'near_duplicate_threshold': near_duplicate_threshold,
        'by_source': {},
    }

    kept: list[Dict[str, Any]] = []
    seen_content_ids: set[str] = set()
    seen_urls: set[str] = set()
    seen_text_keys: set[str] = set()
    kept_token_sets: list[set[str]] = []

    for post in posts:
        source = narrative_source_key(post)
        dedupe_stats_bucket(stats, source, 'raw')

        content_id = str(post.get('content_id') or '').strip()
        body = post.get('body') or post.get('title') or post.get('summary') or ''
        link = canonical_url(post.get('web_link') or post.get('share_link') or post.get('link'))
        text_key = dedupe_text_key(body)
        tokens = dedupe_tokens(body)

        duplicate_reason = None
        if content_id and content_id in seen_content_ids:
            duplicate_reason = 'content_id_duplicates'
        elif link and link in seen_urls:
            duplicate_reason = 'url_duplicates'
        elif text_key and len(text_key) >= 40 and text_key in seen_text_keys:
            duplicate_reason = 'text_duplicates'
        elif len(tokens) >= 8:
            near_match = any(
                token_jaccard(tokens, existing) >= near_duplicate_threshold
                for existing in kept_token_sets
                if len(existing) >= 8
            )
            if near_match:
                duplicate_reason = 'near_text_duplicates'

        if duplicate_reason:
            stats['duplicate_posts_filtered'] += 1
            stats[duplicate_reason] += 1
            dedupe_stats_bucket(stats, source, 'duplicates_filtered')
            continue

        kept.append(post)
        dedupe_stats_bucket(stats, source, 'kept')
        if content_id:
            seen_content_ids.add(content_id)
        if link:
            seen_urls.add(link)
        if text_key:
            seen_text_keys.add(text_key)
        kept_token_sets.append(tokens)

    stats['deduped_posts_after_dedupe'] = len(kept)
    return kept, stats


def clamp_source_weight(value: float) -> float:
    return max(SOURCE_MIN_WEIGHT, min(SOURCE_MAX_WEIGHT, value))


def source_profile_key(source: Any) -> str:
    text = str(source or '').strip().lower()
    if not text:
        return 'unknown'
    if text in SOURCE_RELIABILITY_PROFILES:
        return text
    if 'cointelegraph' in text:
        return 'cointelegraph'
    if 'theblock' in text or text == 'the block':
        return 'theblock'
    if 'blockbeats' in text:
        return 'blockbeats'
    if 'defillama' in text:
        return 'defillama'
    if text in {'binance', 'square', 'binance_square'}:
        return 'binance_square'
    if 'twitter_6551' in text or '6551' in text:
        return 'twitter_6551'
    if 'web_search' in text:
        return 'web_search'
    return 'unknown'


def source_profile(source: Any) -> Dict[str, Any]:
    key = source_profile_key(source)
    profile = dict(SOURCE_RELIABILITY_PROFILES.get(key) or SOURCE_RELIABILITY_PROFILES['unknown'])
    profile['source_key'] = key
    return profile


def event_cooldown_lookup_key(row: Dict[str, Any], symbol: Any) -> tuple[int, str]:
    return (id(row), str(symbol or '').upper())


def narrative_event_identity(row: Dict[str, Any]) -> str:
    source = source_profile_key(row.get('source'))
    content_id = str(row.get('content_id') or '').strip()
    if content_id:
        return f'cid:{source}:{content_id}'

    link = canonical_url(row.get('web_link') or row.get('share_link') or row.get('link'))
    if link:
        return f'url:{link}'

    text_key = dedupe_text_key(row.get('body_preview') or row.get('body') or row.get('title') or '')
    if len(text_key) >= 20:
        return f'txt:{source}:{text_key}'
    return ''


def narrative_event_key(row: Dict[str, Any], symbol: Any) -> tuple[str, str]:
    base = normalize_base_symbol(symbol)
    identity = narrative_event_identity(row)
    if not base or not identity:
        return '', identity
    digest = hashlib.sha1(f'{base}:{identity}'.encode('utf-8', errors='ignore')).hexdigest()[:24]
    return f'{base}:{digest}', identity


def event_cooldown_weight(
    seen_count_before: int,
    hours_since_seen: float | None,
    cooldown_hours: int,
) -> tuple[float, str]:
    if seen_count_before <= 0:
        return 1.0, 'new'
    if hours_since_seen is None or cooldown_hours <= 0:
        return EVENT_COOLDOWN_MIN_WEIGHT, 'repeated'
    if hours_since_seen >= cooldown_hours:
        return 1.0, 'cooldown_expired'

    repeat_floor = max(
        EVENT_COOLDOWN_MIN_WEIGHT,
        1.0 - (EVENT_COOLDOWN_REPEAT_STEP * seen_count_before),
    )
    release_ratio = max(0.0, min(1.0, hours_since_seen / cooldown_hours))
    weight = repeat_floor + ((1.0 - repeat_floor) * release_ratio)
    return round(max(EVENT_COOLDOWN_MIN_WEIGHT, min(1.0, weight)), 3), 'repeated'


def connect_event_memory(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(EVENT_MEMORY_SCHEMA)
    return conn


def bump_event_cooldown_stats(stats: Dict[str, Any], source: str, key: str) -> None:
    bucket = stats['by_source'].setdefault(source, {
        'raw_symbol_events': 0,
        'new_events': 0,
        'repeated_events': 0,
        'cooldown_expired_events': 0,
        'missing_key_events': 0,
    })
    bucket[key] += 1


def apply_narrative_event_cooldown(
    narrative_posts: list[Dict[str, Any]],
    db_path: Path | None = None,
    cooldown_hours: int = DEFAULT_EVENT_COOLDOWN_HOURS,
    now: datetime | None = None,
) -> tuple[Dict[tuple[int, str], Dict[str, Any]], Dict[str, Any]]:
    now = now or datetime.now(UTC)
    path = db_path or DEFAULT_EVENT_MEMORY_DB
    lookup: Dict[tuple[int, str], Dict[str, Any]] = {}
    stats: Dict[str, Any] = {
        'enabled': cooldown_hours > 0,
        'cooldown_hours': cooldown_hours,
        'db_path': str(path),
        'raw_symbol_events': 0,
        'unique_events': 0,
        'new_events': 0,
        'repeated_events': 0,
        'cooldown_expired_events': 0,
        'missing_key_events': 0,
        'avg_cooldown_weight': 1.0,
        'by_source': {},
    }
    if cooldown_hours <= 0:
        return lookup, stats

    events: list[Dict[str, Any]] = []
    unique_events: Dict[str, Dict[str, Any]] = {}
    for row in narrative_posts:
        for symbol in row.get('symbols') or []:
            source = source_profile_key(row.get('source'))
            stats['raw_symbol_events'] += 1
            bump_event_cooldown_stats(stats, source, 'raw_symbol_events')
            event_key, event_identity = narrative_event_key(row, symbol)
            if not event_key:
                stats['missing_key_events'] += 1
                bump_event_cooldown_stats(stats, source, 'missing_key_events')
                continue
            symbol_pair = f"{normalize_base_symbol(symbol)}USDT"
            event = {
                'event_key': event_key,
                'event_identity': event_identity,
                'symbol': symbol_pair,
                'source': source,
                'body_preview': str(row.get('body_preview') or '')[:220],
                'row': row,
            }
            events.append(event)
            unique_events.setdefault(event_key, event)

    stats['unique_events'] = len(unique_events)
    if not events:
        return lookup, stats

    conn = None
    try:
        conn = connect_event_memory(path)
        with conn:
            previous: Dict[str, sqlite3.Row] = {}
            for event_key in unique_events:
                row = conn.execute(
                    'SELECT * FROM narrative_event_memory WHERE event_key = ?',
                    (event_key,),
                ).fetchone()
                if row:
                    previous[event_key] = row

            weights = []
            now_iso = now.isoformat().replace('+00:00', 'Z')
            for event in events:
                source = event['source']
                prev = previous.get(event['event_key'])
                seen_count_before = int(prev['seen_count']) if prev else 0
                last_seen = parse_content_time(prev['last_seen_at'], now=now) if prev else None
                hours_since_seen = None
                if last_seen:
                    hours_since_seen = max(0.0, (now - last_seen).total_seconds() / 3600.0)
                weight, status = event_cooldown_weight(seen_count_before, hours_since_seen, cooldown_hours)
                weights.append(weight)
                if status == 'new':
                    stats['new_events'] += 1
                    bump_event_cooldown_stats(stats, source, 'new_events')
                elif status == 'cooldown_expired':
                    stats['cooldown_expired_events'] += 1
                    bump_event_cooldown_stats(stats, source, 'cooldown_expired_events')
                else:
                    stats['repeated_events'] += 1
                    bump_event_cooldown_stats(stats, source, 'repeated_events')

                lookup[event_cooldown_lookup_key(event['row'], event['symbol'])] = {
                    'event_key': event['event_key'],
                    'status': status,
                    'weight': weight,
                    'seen_count_before': seen_count_before,
                    'hours_since_seen': round(hours_since_seen, 2) if hours_since_seen is not None else None,
                }

            for event_key, event in unique_events.items():
                prev = previous.get(event_key)
                first_seen_at = prev['first_seen_at'] if prev else now_iso
                seen_count = int(prev['seen_count']) + 1 if prev else 1
                prev_sources = []
                if prev:
                    try:
                        prev_sources = json.loads(prev['sources_json'] or '[]')
                    except Exception:
                        prev_sources = []
                sources = sorted({str(item) for item in prev_sources + [event['source']] if item})
                conn.execute(
                    '''
                    INSERT INTO narrative_event_memory (
                        event_key, event_identity, first_seen_at, last_seen_at,
                        seen_count, symbol, sources_json, body_preview
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_key) DO UPDATE SET
                        last_seen_at=excluded.last_seen_at,
                        seen_count=excluded.seen_count,
                        sources_json=excluded.sources_json,
                        body_preview=excluded.body_preview
                    ''',
                    (
                        event_key,
                        event['event_identity'],
                        first_seen_at,
                        now_iso,
                        seen_count,
                        event['symbol'],
                        json.dumps(sources, ensure_ascii=False),
                        event['body_preview'],
                    ),
                )

            if weights:
                stats['avg_cooldown_weight'] = round(sum(weights) / len(weights), 3)
    except Exception as exc:
        stats['error'] = str(exc)
        lookup = {}
    finally:
        if conn is not None:
            conn.close()

    return lookup, stats


def author_key_from_row(row: Dict[str, Any]) -> str:
    return str(row.get('username') or row.get('author') or '').strip()


def backtest_weight_from_win_rate(win_rate_pct: float, sample_count: int) -> float:
    if sample_count < 3:
        return 1.0
    sample_factor = min(1.0, sample_count / 20.0)
    edge = (win_rate_pct - 50.0) / 100.0
    return clamp_source_weight(1.0 + edge * sample_factor)


def load_author_accuracy_map(db_path: Path | None = None, min_posts: int = 3) -> Dict[str, Dict[str, Any]]:
    path = db_path or (RESULTS_DIR / 'binance_square_research.sqlite3')
    if not path.exists():
        return {}
    query = '''
        SELECT
            COALESCE(p.username, p.author, 'unknown') AS author_key,
            COUNT(DISTINCT b.content_id) AS backtested_posts,
            AVG(CASE
                WHEN p.tone = 'bullish' AND b.forward_return_pct > 0 THEN 1.0
                WHEN p.tone = 'bearish' AND b.forward_return_pct < 0 THEN 1.0
                ELSE 0.0
            END) * 100 AS directional_win_rate_pct,
            AVG(b.forward_return_pct) AS avg_return_pct
        FROM backtest_events b
        JOIN posts p ON p.content_id = b.content_id
        GROUP BY author_key
        HAVING COUNT(DISTINCT b.content_id) >= ?
    '''
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, (min_posts,)).fetchall()
    except Exception:
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        author_key = str(row['author_key'] or '').strip()
        if not author_key:
            continue
        sample_count = int(row['backtested_posts'] or 0)
        win_rate = float(row['directional_win_rate_pct'] or 0.0)
        result[author_key] = {
            'backtested_posts': sample_count,
            'directional_win_rate_pct': round(win_rate, 2),
            'avg_return_pct': round(float(row['avg_return_pct'] or 0.0), 4),
            'backtest_weight': round(backtest_weight_from_win_rate(win_rate, sample_count), 3),
        }
    return result


def row_source_weight(row: Dict[str, Any], author_accuracy: Dict[str, Dict[str, Any]]) -> float:
    profile = source_profile(row.get('source'))
    weight = float(profile.get('base_weight') or 1.0)
    author_key = author_key_from_row(row)
    author_edge = author_accuracy.get(author_key)
    if author_edge:
        weight *= float(author_edge.get('backtest_weight') or 1.0)
    return clamp_source_weight(weight)


def narrative_freshness_weight(row: Dict[str, Any], now: datetime | None = None) -> tuple[float, float | None]:
    now = now or datetime.now(UTC)
    published_at = parse_content_time(row.get('publish_time'), now=now)
    if published_at is None:
        return NARRATIVE_UNKNOWN_TIME_WEIGHT, None
    age_hours = max(0.0, (now - published_at).total_seconds() / 3600.0)
    if age_hours <= 6:
        return 1.0, round(age_hours, 2)
    decay_ratio = min(1.0, (age_hours - 6.0) / 66.0)
    weight = 1.0 - ((1.0 - NARRATIVE_MIN_FRESHNESS_WEIGHT) * decay_ratio)
    return round(max(NARRATIVE_MIN_FRESHNESS_WEIGHT, weight), 3), round(age_hours, 2)


def quality_label(weight: float, unique_sources: int, backtested_authors: int = 0) -> str:
    if backtested_authors >= 2 and weight >= 1.05:
        return 'backtested_high'
    if backtested_authors >= 1:
        return 'backtested'
    if unique_sources >= 2 and weight >= 1.0:
        return 'multi_source'
    if weight >= 1.0:
        return 'standard'
    return 'low_verification'


def build_source_quality_report(
    narrative_posts: list[Dict[str, Any]],
    author_accuracy: Dict[str, Dict[str, Any]],
    freshness_stats: Dict[str, Any],
) -> Dict[str, Any]:
    by_source: Dict[str, Dict[str, Any]] = {}
    for row in narrative_posts:
        profile = source_profile(row.get('source'))
        key = profile['source_key']
        bucket = by_source.setdefault(key, {
            'source_key': key,
            'display_name': profile.get('display_name'),
            'source_type': profile.get('source_type'),
            'accuracy_status': profile.get('accuracy_status'),
            'base_weight': profile.get('base_weight'),
            'post_count': 0,
            'symbol_mentions': 0,
            'avg_signal_score': 0.0,
            'backtested_authors': 0,
            '_backtested_author_keys': set(),
            'effective_weight': 0.0,
        })
        bucket['post_count'] += 1
        bucket['symbol_mentions'] += len(row.get('symbols') or [])
        bucket['avg_signal_score'] += float(row.get('signal_score') or 0.0)
        author_key = author_key_from_row(row)
        if author_key and author_key in author_accuracy:
            bucket['_backtested_author_keys'].add(author_key)
        bucket['effective_weight'] += row_source_weight(row, author_accuracy)

    for key, bucket in by_source.items():
        count = max(1, int(bucket['post_count']))
        bucket['avg_signal_score'] = round(float(bucket['avg_signal_score']) / count, 2)
        bucket['effective_weight'] = round(float(bucket['effective_weight']) / count, 3)
        bucket['backtested_authors'] = len(bucket.pop('_backtested_author_keys'))
        freshness = (freshness_stats.get('by_source') or {}).get(key)
        if freshness:
            bucket['freshness'] = freshness
        if bucket['backtested_authors']:
            bucket['accuracy_status'] = 'local_backtested_author_edge'

    return {
        'mode': 'source_quality_v1',
        'backtest_db_present': (RESULTS_DIR / 'binance_square_research.sqlite3').exists(),
        'local_author_accuracy_available': bool(author_accuracy),
        'backtested_author_count': len(author_accuracy),
        'note': (
            'effective_weight uses source type/reputation and optional local backtested author edge; '
            'it is not a guarantee of future direction.'
        ),
        'sources': sorted(by_source.values(), key=lambda row: (row['effective_weight'], row['post_count']), reverse=True),
    }


def attach_symbol_narrative_quality(
    symbol_narrative_view: list[Dict[str, Any]],
    narrative_posts: list[Dict[str, Any]],
    author_accuracy: Dict[str, Dict[str, Any]],
    event_cooldown_lookup: Dict[tuple[int, str], Dict[str, Any]] | None = None,
) -> None:
    event_cooldown_lookup = event_cooldown_lookup or {}
    grouped: Dict[str, list[Dict[str, Any]]] = {}
    for row in narrative_posts:
        for symbol in row.get('symbols') or []:
            grouped.setdefault(symbol, []).append(row)

    for row in symbol_narrative_view:
        items = grouped.get(row.get('symbol')) or []
        if not items:
            continue
        now = datetime.now(UTC)
        weights = [row_source_weight(item, author_accuracy) for item in items]
        freshness = [narrative_freshness_weight(item, now=now) for item in items]
        freshness_weights = [item[0] for item in freshness]
        known_ages = [item[1] for item in freshness if item[1] is not None]
        cooldown_items = [
            event_cooldown_lookup.get(event_cooldown_lookup_key(item, row.get('symbol')), {'weight': 1.0, 'status': 'not_tracked'})
            for item in items
        ]
        cooldown_weights = [float(item.get('weight') or 1.0) for item in cooldown_items]
        sources = sorted({source_profile(item.get('source'))['source_key'] for item in items})
        backtested_authors = len({
            author_key_from_row(item)
            for item in items
            if author_key_from_row(item) in author_accuracy
        })
        avg_weight = sum(weights) / len(weights)
        avg_freshness_weight = sum(freshness_weights) / len(freshness_weights)
        avg_cooldown_weight = sum(cooldown_weights) / len(cooldown_weights)
        direction_weight = clamp_source_weight(avg_weight * avg_freshness_weight * avg_cooldown_weight)
        row['narrative_quality'] = {
            'source_weight': round(avg_weight, 3),
            'freshness_weight': round(avg_freshness_weight, 3),
            'cooldown_weight': round(avg_cooldown_weight, 3),
            'direction_weight': round(direction_weight, 3),
            'avg_age_hours': round(sum(known_ages) / len(known_ages), 2) if known_ages else None,
            'unknown_time_posts': len(freshness) - len(known_ages),
            'cooldown_repeat_events': len([item for item in cooldown_items if item.get('status') == 'repeated']),
            'cooldown_expired_events': len([item for item in cooldown_items if item.get('status') == 'cooldown_expired']),
            'cooldown_new_events': len([item for item in cooldown_items if item.get('status') == 'new']),
            'quality_label': quality_label(avg_weight, len(sources), backtested_authors),
            'unique_sources': len(sources),
            'sources': sources,
            'backtested_authors': backtested_authors,
        }


def filter_recent_posts(
    posts: list[Dict[str, Any]],
    lookback_hours: int,
    now: datetime | None = None,
) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=lookback_hours) if lookback_hours > 0 else None
    stats: Dict[str, Any] = {
        'enabled': lookback_hours > 0,
        'lookback_hours': lookback_hours,
        'cutoff_time': cutoff.isoformat().replace('+00:00', 'Z') if cutoff else None,
        'raw_posts_before_freshness': len(posts),
        'fresh_posts_after_freshness': 0,
        'dated_posts_kept': 0,
        'unknown_time_kept': 0,
        'future_time_kept': 0,
        'stale_posts_filtered': 0,
        'by_source': {},
    }

    def bump(source: str, key: str) -> None:
        bucket = stats['by_source'].setdefault(source, {
            'raw': 0,
            'kept': 0,
            'dated_posts_kept': 0,
            'unknown_time_kept': 0,
            'future_time_kept': 0,
            'stale_posts_filtered': 0,
        })
        bucket[key] += 1

    kept: list[Dict[str, Any]] = []
    for post in posts:
        source = narrative_source_key(post)
        bump(source, 'raw')
        if lookback_hours <= 0:
            kept.append(post)
            bump(source, 'kept')
            continue

        published_at = parse_content_time(post.get('publish_time'), now=now)
        if published_at is None:
            kept.append(post)
            stats['unknown_time_kept'] += 1
            bump(source, 'unknown_time_kept')
            bump(source, 'kept')
            continue

        if published_at < cutoff:
            stats['stale_posts_filtered'] += 1
            bump(source, 'stale_posts_filtered')
            continue

        kept.append(post)
        stats['dated_posts_kept'] += 1
        bump(source, 'dated_posts_kept')
        bump(source, 'kept')
        if published_at > now + timedelta(hours=6):
            stats['future_time_kept'] += 1
            bump(source, 'future_time_kept')

    stats['fresh_posts_after_freshness'] = len(kept)
    return kept, stats


def normalize_base_symbol(symbol: Any) -> str | None:
    raw = str(symbol or '').strip().upper().replace('/', '').replace(':USDT', '')
    if not raw:
        return None
    if raw.endswith('USDT'):
        raw = raw[:-4]
    if not raw or raw in STABLE_SYMBOLS:
        return None
    return raw


def collect_technical_symbols(
    hotspot: Dict[str, Any],
    narrative_payload: Dict[str, Any],
    futures_symbols: list[str] | None,
    limit: int,
) -> list[str]:
    if limit <= 0:
        return []

    symbols: list[str] = []

    def add_symbol(value: Any) -> None:
        base = normalize_base_symbol(value)
        if base and base not in symbols:
            symbols.append(base)

    for row in hotspot.get('buy_candidates') or []:
        add_symbol(row.get('symbol'))
    for row in hotspot.get('top_opportunities') or []:
        add_symbol(row.get('symbol'))
    for row in narrative_payload.get('symbol_narrative_view') or []:
        add_symbol(row.get('symbol'))
    for symbol in futures_symbols or DEFAULT_TECHNICAL_SYMBOLS:
        add_symbol(symbol)
    for symbol in DEFAULT_TECHNICAL_SYMBOLS:
        add_symbol(symbol)

    return symbols[:limit]


def build_technical_payload(symbols: list[str]) -> Dict[str, Any]:
    results = []
    for symbol in symbols:
        base = normalize_base_symbol(symbol)
        if not base:
            continue
        pair = f'{base}USDT'
        try:
            row = compute_technical_signals(base)
            row['symbol_pair'] = pair
            if row.get('bias') == 'bullish':
                row['direction_watch'] = 'rise_watch'
            elif row.get('bias') == 'bearish':
                row['direction_watch'] = 'fall_watch'
            else:
                row['direction_watch'] = 'neutral_watch'
        except Exception as exc:
            row = {
                'symbol': base,
                'symbol_pair': pair,
                'source': 'binance_technical',
                'error': str(exc),
                'bias': 'unknown',
                'direction_watch': 'unknown',
            }
        results.append(row)

    valid_rows = [row for row in results if not row.get('error')]
    bullish_rows = [row for row in valid_rows if row.get('bias') == 'bullish']
    bearish_rows = [row for row in valid_rows if row.get('bias') == 'bearish']

    return {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'technical_monitor_v1',
            'symbols': symbols,
        },
        'summary': {
            'requested_symbols': len(symbols),
            'analyzed_symbols': len(valid_rows),
            'bullish': len(bullish_rows),
            'bearish': len(bearish_rows),
            'neutral': len([row for row in valid_rows if row.get('bias') == 'neutral']),
            'errors': len(results) - len(valid_rows),
        },
        'watchlist': results,
    }


def clamp_direction_score(value: float) -> float:
    return max(-100.0, min(100.0, value))


def build_direction_conflict_report(breakdown: Dict[str, float]) -> Dict[str, Any]:
    directional = []
    for source, value in breakdown.items():
        if source == 'conflict_penalty':
            continue
        try:
            points = float(value)
        except Exception:
            continue
        if abs(points) < SIGNAL_CONFLICT_MIN_ABS_POINTS:
            continue
        directional.append({
            'source': source,
            'points': round(points, 1),
            'side': 'bullish' if points > 0 else 'bearish',
        })

    positive = [item for item in directional if item['points'] > 0]
    negative = [item for item in directional if item['points'] < 0]
    if not positive or not negative:
        return {}

    positive_points = sum(item['points'] for item in positive)
    negative_points_abs = abs(sum(item['points'] for item in negative))
    minority_points = min(positive_points, negative_points_abs)
    majority_points = max(positive_points, negative_points_abs)
    ratio = minority_points / majority_points if majority_points else 0.0

    if ratio >= 0.75 and minority_points >= 12:
        severity = 'strong'
    elif ratio >= 0.35 or minority_points >= 8:
        severity = 'moderate'
    else:
        severity = 'minor'

    penalty = min(SIGNAL_CONFLICT_MAX_PENALTY, minority_points * SIGNAL_CONFLICT_PENALTY_RATIO)
    return {
        'has_conflict': True,
        'severity': severity,
        'positive_points': round(positive_points, 1),
        'negative_points_abs': round(negative_points_abs, 1),
        'conflict_ratio': round(ratio, 3),
        'score_penalty_abs': round(penalty, 1),
        'positive_sources': positive,
        'negative_sources': negative,
    }


def cap_confidence_for_conflict(confidence: str, conflict_report: Dict[str, Any]) -> str:
    severity = conflict_report.get('severity')
    if severity == 'strong':
        return 'low'
    if severity == 'moderate' and confidence == 'high':
        return 'medium'
    return confidence


def confidence_rank(confidence: str) -> int:
    return {'low': 0, 'medium': 1, 'high': 2}.get(str(confidence or '').lower(), 0)


def confidence_from_rank(rank: int) -> str:
    if rank >= 2:
        return 'high'
    if rank == 1:
        return 'medium'
    return 'low'


def cap_confidence(confidence: str, cap: str) -> str:
    return confidence_from_rank(min(confidence_rank(confidence), confidence_rank(cap)))


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ''):
            return default
        return float(value)
    except Exception:
        return default


def volatility_risk_adjustment(score: float, technical_quality: Dict[str, Any]) -> Dict[str, Any]:
    if not technical_quality:
        return {'applied': False, 'level': 'unknown'}

    primary_atr = to_float(technical_quality.get('primary_atr_pct'))
    timeframe_atr = {
        str(key): to_float(value)
        for key, value in (technical_quality.get('timeframe_atr_pct') or {}).items()
        if value not in (None, '')
    }
    atr_values = [value for value in [primary_atr, *timeframe_atr.values()] if value > 0]
    if not atr_values:
        return {'applied': False, 'level': 'unknown'}

    max_atr = max(atr_values)
    if primary_atr >= 6.0 or max_atr >= 9.0:
        level = 'extreme'
        adjustment_abs = 8.0
        confidence_cap = 'low'
    elif primary_atr >= 4.0 or max_atr >= 6.0:
        level = 'high'
        adjustment_abs = 5.0
        confidence_cap = 'medium'
    elif primary_atr >= 2.5 or max_atr >= 4.0:
        level = 'elevated'
        adjustment_abs = 2.0
        confidence_cap = 'medium'
    else:
        return {
            'applied': False,
            'level': 'normal',
            'primary_atr_pct': round(primary_atr, 2) if primary_atr else None,
            'max_atr_pct': round(max_atr, 2),
            'timeframe_atr_pct': timeframe_atr,
        }

    score_sign = 1.0 if score > 0 else (-1.0 if score < 0 else 0.0)
    adjustment = round(-score_sign * min(abs(score), adjustment_abs), 1) if score_sign else 0.0
    return {
        'applied': bool(adjustment),
        'level': level,
        'primary_atr_pct': round(primary_atr, 2) if primary_atr else None,
        'max_atr_pct': round(max_atr, 2),
        'timeframe_atr_pct': timeframe_atr,
        'adjustment': adjustment,
        'confidence_cap': confidence_cap,
    }


def technical_alignment_risk_adjustment(score: float, technical_quality: Dict[str, Any]) -> Dict[str, Any]:
    if not technical_quality:
        return {'applied': False, 'level': 'unknown'}
    if technical_quality.get('mode') != 'multi_timeframe':
        return {'applied': False, 'level': 'single_timeframe'}

    score_sign = 1.0 if score > 0 else (-1.0 if score < 0 else 0.0)
    alignment = str(technical_quality.get('alignment') or 'unknown')
    confidence = str(technical_quality.get('confidence') or 'unknown')
    biases = technical_quality.get('timeframe_biases') or {}
    valid_timeframes = technical_quality.get('valid_timeframes') or []
    direction_votes = technical_quality.get('direction_votes') or {}
    bullish_votes = int(direction_votes.get('bullish') or sum(1 for item in biases.values() if item == 'bullish'))
    bearish_votes = int(direction_votes.get('bearish') or sum(1 for item in biases.values() if item == 'bearish'))

    if len(valid_timeframes) < 2 or score_sign == 0:
        return {
            'applied': False,
            'level': 'insufficient_or_neutral',
            'alignment': alignment,
            'confidence': confidence,
            'timeframe_biases': biases,
            'direction_votes': direction_votes,
        }

    direction_side = 'long' if score_sign > 0 else 'short'
    supporting_votes = bullish_votes if direction_side == 'long' else bearish_votes
    opposing_votes = bearish_votes if direction_side == 'long' else bullish_votes
    aligned_side = None
    if alignment.startswith('bullish_'):
        aligned_side = 'long'
    elif alignment.startswith('bearish_'):
        aligned_side = 'short'

    if aligned_side and aligned_side != direction_side:
        level = 'opposite_alignment'
        adjustment_abs = 8.0
        confidence_cap = 'low'
    elif alignment == 'mixed':
        if opposing_votes >= supporting_votes:
            level = 'strong_mixed_conflict'
            adjustment_abs = 7.0
            confidence_cap = 'low'
        else:
            level = 'mixed_timeframes'
            adjustment_abs = 4.0
            confidence_cap = 'medium'
    else:
        return {
            'applied': False,
            'level': 'aligned_or_neutral',
            'alignment': alignment,
            'confidence': confidence,
            'direction_side': direction_side,
            'supporting_votes': supporting_votes,
            'opposing_votes': opposing_votes,
            'timeframe_biases': biases,
            'direction_votes': direction_votes,
        }

    adjustment = round(-score_sign * min(abs(score), adjustment_abs), 1)
    return {
        'applied': bool(adjustment),
        'level': level,
        'alignment': alignment,
        'confidence': confidence,
        'direction_side': direction_side,
        'supporting_votes': supporting_votes,
        'opposing_votes': opposing_votes,
        'timeframe_biases': biases,
        'direction_votes': direction_votes,
        'adjustment': adjustment,
        'confidence_cap': confidence_cap,
    }


def volume_confirmation_adjustment(score: float, technical_quality: Dict[str, Any]) -> Dict[str, Any]:
    if not technical_quality:
        return {'applied': False, 'level': 'unknown'}

    score_sign = 1.0 if score > 0 else (-1.0 if score < 0 else 0.0)
    if not score_sign:
        return {'applied': False, 'level': 'neutral_direction'}

    primary = technical_quality.get('primary_volume_trend') or {}
    timeframe_trends = technical_quality.get('timeframe_volume_trend') or {}
    trend_rows = [
        row for row in [primary, *timeframe_trends.values()]
        if isinstance(row, dict) and row.get('trend')
    ]
    if not trend_rows:
        return {'applied': False, 'level': 'no_volume_data'}

    expanding = {'异常放量', '温和放量'}
    contracting = {'极度缩量', '温和缩量'}
    expanding_count = sum(1 for row in trend_rows if row.get('trend') in expanding)
    contracting_count = sum(1 for row in trend_rows if row.get('trend') in contracting)
    primary_trend = str(primary.get('trend') or 'unknown')
    primary_ratio = to_float(primary.get('ratio'), 1.0)

    if expanding_count >= 1:
        return {
            'applied': False,
            'level': 'volume_confirmed',
            'primary_trend': primary_trend,
            'primary_ratio': primary_ratio,
            'expanding_count': expanding_count,
            'contracting_count': contracting_count,
            'timeframe_volume_trend': timeframe_trends,
        }

    if primary_trend == '极度缩量' or contracting_count >= 2:
        level = 'contracting_volume'
        adjustment_abs = 5.0
        confidence_cap = 'medium'
    elif abs(score) >= 25:
        level = 'no_volume_confirmation'
        adjustment_abs = 3.0
        confidence_cap = 'medium'
    else:
        return {
            'applied': False,
            'level': 'normal_or_low_signal',
            'primary_trend': primary_trend,
            'primary_ratio': primary_ratio,
            'expanding_count': expanding_count,
            'contracting_count': contracting_count,
            'timeframe_volume_trend': timeframe_trends,
        }

    adjustment = round(-score_sign * min(abs(score), adjustment_abs), 1)
    return {
        'applied': bool(adjustment),
        'level': level,
        'primary_trend': primary_trend,
        'primary_ratio': primary_ratio,
        'expanding_count': expanding_count,
        'contracting_count': contracting_count,
        'timeframe_volume_trend': timeframe_trends,
        'adjustment': adjustment,
        'confidence_cap': confidence_cap,
    }


def price_position_risk_adjustment(score: float, technical_quality: Dict[str, Any]) -> Dict[str, Any]:
    if not technical_quality:
        return {'applied': False, 'level': 'unknown'}

    score_sign = 1.0 if score > 0 else (-1.0 if score < 0 else 0.0)
    if not score_sign or abs(score) < 20.0:
        return {'applied': False, 'level': 'neutral_or_low_signal'}

    def metric_values(primary_key: str, timeframe_key: str) -> list[float]:
        values: list[float] = []
        primary = technical_quality.get(primary_key)
        if primary not in (None, ''):
            values.append(to_float(primary))
        for value in (technical_quality.get(timeframe_key) or {}).values():
            if value not in (None, ''):
                values.append(to_float(value))
        return values

    rsi_values = metric_values('primary_rsi', 'timeframe_rsi')
    bb_positions = metric_values('primary_bb_position', 'timeframe_bb_position')
    ma_distances = metric_values('primary_price_vs_ma25_pct', 'timeframe_price_vs_ma25_pct')
    if not (rsi_values or bb_positions or ma_distances):
        return {'applied': False, 'level': 'no_position_data'}

    if score_sign > 0:
        rsi_votes = sum(1 for value in rsi_values if value >= 72.0)
        bb_votes = sum(1 for value in bb_positions if value >= 0.92)
        ma_votes = sum(1 for value in ma_distances if value >= 8.0)
        extreme = (
            any(value >= 82.0 for value in rsi_values)
            or any(value >= 1.05 for value in bb_positions)
            or any(value >= 18.0 for value in ma_distances)
        )
        side = 'long_chase'
    else:
        rsi_votes = sum(1 for value in rsi_values if value <= 28.0)
        bb_votes = sum(1 for value in bb_positions if value <= 0.08)
        ma_votes = sum(1 for value in ma_distances if value <= -8.0)
        extreme = (
            any(value <= 18.0 for value in rsi_values)
            or any(value <= -0.05 for value in bb_positions)
            or any(value <= -18.0 for value in ma_distances)
        )
        side = 'short_chase'

    overextended_votes = rsi_votes + bb_votes + ma_votes
    if extreme and overextended_votes >= 2:
        level = 'extreme_chase_risk'
        adjustment_abs = 8.0
        confidence_cap = 'low'
    elif overextended_votes >= 3:
        level = 'crowded_position_risk'
        adjustment_abs = 6.0
        confidence_cap = 'medium'
    elif overextended_votes >= 2:
        level = 'chase_risk'
        adjustment_abs = 4.0
        confidence_cap = 'medium'
    else:
        return {
            'applied': False,
            'level': 'not_overextended',
            'side': side,
            'overextended_votes': overextended_votes,
            'rsi_values': rsi_values,
            'bb_positions': bb_positions,
            'price_vs_ma25_pct': ma_distances,
        }

    adjustment = round(-score_sign * min(abs(score), adjustment_abs), 1)
    return {
        'applied': bool(adjustment),
        'level': level,
        'side': side,
        'overextended_votes': overextended_votes,
        'rsi_votes': rsi_votes,
        'bb_votes': bb_votes,
        'ma_distance_votes': ma_votes,
        'rsi_values': rsi_values,
        'bb_positions': bb_positions,
        'price_vs_ma25_pct': ma_distances,
        'adjustment': adjustment,
        'confidence_cap': confidence_cap,
    }


def source_confirmation_adjustment(
    score: float,
    sources: set[str],
    breakdown: Dict[str, Any],
) -> Dict[str, Any]:
    score_sign = 1.0 if score > 0 else (-1.0 if score < 0 else 0.0)
    core_contributions: Dict[str, float] = {}
    for source in SOURCE_CONFIRMATION_CORE_SOURCES:
        if source not in sources and source not in breakdown:
            continue
        points = round(to_float(breakdown.get(source), 0.0), 1)
        if points:
            core_contributions[source] = points

    if not score_sign or abs(score) < 25.0:
        return {
            'applied': False,
            'level': 'low_signal_or_neutral',
            'core_source_count': len(core_contributions),
            'supporting_sources': [],
            'opposing_sources': [],
            'core_contributions': core_contributions,
        }

    supporting_sources = [
        source for source, points in core_contributions.items()
        if points * score_sign > 0
    ]
    opposing_sources = [
        source for source, points in core_contributions.items()
        if points * score_sign < 0
    ]

    if len(supporting_sources) >= 2:
        return {
            'applied': False,
            'level': 'multi_source_confirmed',
            'core_source_count': len(core_contributions),
            'supporting_sources': supporting_sources,
            'opposing_sources': opposing_sources,
            'core_contributions': core_contributions,
        }

    if len(supporting_sources) == 1:
        adjustment_abs = 6.0 if abs(score) >= 40.0 else 4.0
        adjustment = round(-score_sign * min(abs(score), adjustment_abs), 1)
        return {
            'applied': bool(adjustment),
            'level': 'single_source_direction',
            'core_source_count': len(core_contributions),
            'supporting_sources': supporting_sources,
            'opposing_sources': opposing_sources,
            'core_contributions': core_contributions,
            'adjustment': adjustment,
            'confidence_cap': 'medium',
        }

    return {
        'applied': False,
        'level': 'no_core_source_confirmation',
        'core_source_count': len(core_contributions),
        'supporting_sources': supporting_sources,
        'opposing_sources': opposing_sources,
        'core_contributions': core_contributions,
    }


def futures_crowding_risk_adjustment(score: float, futures_row: Dict[str, Any] | None) -> Dict[str, Any]:
    if not futures_row:
        return {'applied': False, 'level': 'unknown'}

    score_sign = 1.0 if score > 0 else (-1.0 if score < 0 else 0.0)
    if not score_sign:
        return {'applied': False, 'level': 'neutral_direction'}

    funding = futures_row.get('funding') or {}
    oi = futures_row.get('oi_5m') or {}
    long_short = futures_row.get('long_short') or {}
    liquidation = futures_row.get('liquidation') or {}
    composite = futures_row.get('composite') or {}

    funding_signal = str(funding.get('signal') or '')
    ls_signal = str(long_short.get('signal') or '')
    oi_signal = str(oi.get('signal') or '')
    liquidation_signal = str(liquidation.get('signal') or '')
    funding_z = to_float(funding.get('z_score'))
    oi_change = to_float(oi.get('oi_change_pct_1h'))

    long_risk_reasons: list[str] = []
    short_risk_reasons: list[str] = []
    if funding_signal == 'crowding_long':
        long_risk_reasons.append(f'funding_crowding_long_z={funding_z}')
    elif funding_signal == 'sustained_positive' and oi_signal in {'surge', 'inflow'}:
        long_risk_reasons.append(f'sustained_positive_funding_with_oi_{oi_signal}')
    if funding_signal == 'crowding_short':
        short_risk_reasons.append(f'funding_crowding_short_z={funding_z}')
    elif funding_signal == 'sustained_negative' and oi_signal in {'surge', 'inflow'}:
        short_risk_reasons.append(f'sustained_negative_funding_with_oi_{oi_signal}')

    if ls_signal == 'extreme_long_crowd':
        long_risk_reasons.append('long_short_extreme_long_crowd')
    elif ls_signal == 'leaning_long' and abs(funding_z) >= 1.5 and funding_z > 0:
        long_risk_reasons.append('long_short_leaning_long_with_positive_funding')
    if ls_signal == 'extreme_short_crowd':
        short_risk_reasons.append('long_short_extreme_short_crowd')
    elif ls_signal == 'leaning_short' and abs(funding_z) >= 1.5 and funding_z < 0:
        short_risk_reasons.append('long_short_leaning_short_with_negative_funding')

    if liquidation_signal == 'long_cascade_risk':
        long_risk_reasons.append('long_liquidation_cascade_risk')
    elif liquidation_signal == 'short_cascade_risk':
        short_risk_reasons.append('short_liquidation_cascade_risk')

    active_side = 'long' if score_sign > 0 else 'short'
    active_reasons = long_risk_reasons if active_side == 'long' else short_risk_reasons
    opposite_reasons = short_risk_reasons if active_side == 'long' else long_risk_reasons
    if not active_reasons:
        return {
            'applied': False,
            'level': 'opposite_or_none',
            'direction_side': active_side,
            'opposite_crowding_reasons': opposite_reasons,
            'funding_signal': funding_signal,
            'funding_z_score': funding_z,
            'oi_signal': oi_signal,
            'oi_change_pct_1h': oi_change,
            'long_short_signal': ls_signal,
            'capital_flow_bias': composite.get('capital_flow_bias'),
        }

    if len(active_reasons) >= 2 or abs(funding_z) >= 3.0 or abs(oi_change) >= 5.0:
        level = 'extreme'
        adjustment_abs = 7.0
        confidence_cap = 'low'
    else:
        level = 'high'
        adjustment_abs = 4.0
        confidence_cap = 'medium'
    adjustment = round(-score_sign * min(abs(score), adjustment_abs), 1)
    return {
        'applied': bool(adjustment),
        'level': level,
        'direction_side': active_side,
        'crowding_reasons': active_reasons,
        'opposite_crowding_reasons': opposite_reasons,
        'funding_signal': funding_signal,
        'funding_z_score': funding_z,
        'oi_signal': oi_signal,
        'oi_change_pct_1h': oi_change,
        'long_short_signal': ls_signal,
        'liquidation_signal': liquidation_signal,
        'capital_flow_bias': composite.get('capital_flow_bias'),
        'adjustment': adjustment,
        'confidence_cap': confidence_cap,
    }


def technical_regime_anchor(row: Dict[str, Any], pair: str, weight: float) -> Dict[str, Any] | None:
    if not row or row.get('error'):
        return None
    multi_tf = row.get('multi_timeframe') or {}
    if multi_tf.get('valid_timeframes'):
        weighted_score = float(multi_tf.get('weighted_score') or 0.0)
        bias = multi_tf.get('composite_bias') or row.get('bias') or 'neutral'
        confidence = multi_tf.get('confidence') or 'low'
        alignment = multi_tf.get('alignment')
    else:
        bullish = float(row.get('bullish_pts') or 0.0)
        bearish = float(row.get('bearish_pts') or 0.0)
        weighted_score = bullish - bearish
        bias = row.get('bias') or 'neutral'
        confidence = 'medium' if abs(weighted_score) >= 3 else 'low'
        alignment = None
    return {
        'symbol': pair,
        'weight': weight,
        'bias': bias,
        'weighted_score': round(weighted_score, 2),
        'confidence': confidence,
        'alignment': alignment,
        'atr_pct': (row.get('indicators') or {}).get('atr_pct'),
        'kline_market': row.get('kline_market'),
    }


def build_market_regime_context(technical_payload: Dict[str, Any]) -> Dict[str, Any]:
    technical_by_pair = {
        row.get('symbol_pair') or f"{normalize_base_symbol(row.get('symbol'))}USDT": row
        for row in technical_payload.get('watchlist') or []
        if normalize_base_symbol(row.get('symbol_pair') or row.get('symbol'))
    }
    anchors = []
    for pair, weight in MARKET_REGIME_ANCHORS.items():
        anchor = technical_regime_anchor(technical_by_pair.get(pair), pair, weight)
        if anchor:
            anchors.append(anchor)

    if not anchors:
        return {
            'available': False,
            'bias': 'unknown',
            'regime': 'unknown',
            'regime_score': 0.0,
            'strength': 'unavailable',
            'anchors': [],
            'anchor_conflict': False,
        }

    total_weight = sum(float(anchor.get('weight') or 0.0) for anchor in anchors) or 1.0
    regime_score = sum(
        float(anchor.get('weighted_score') or 0.0) * float(anchor.get('weight') or 0.0)
        for anchor in anchors
    ) / total_weight

    if regime_score >= 4.0:
        bias = 'bullish'
        regime = 'risk_on'
    elif regime_score <= -4.0:
        bias = 'bearish'
        regime = 'risk_off'
    else:
        bias = 'neutral'
        regime = 'mixed_or_neutral'

    abs_score = abs(regime_score)
    if abs_score >= 7.0:
        strength = 'strong'
    elif abs_score >= 4.0:
        strength = 'medium'
    else:
        strength = 'weak'

    anchor_sides = {anchor.get('bias') for anchor in anchors if anchor.get('bias') in {'bullish', 'bearish'}}
    return {
        'available': True,
        'bias': bias,
        'regime': regime,
        'regime_score': round(regime_score, 2),
        'strength': strength,
        'anchors': anchors,
        'anchor_conflict': len(anchor_sides) > 1,
    }


def market_regime_adjustment(pair: str, score: float, regime_context: Dict[str, Any]) -> Dict[str, Any]:
    if not regime_context.get('available') or pair in MARKET_REGIME_ANCHORS:
        return {
            'applied': False,
            'reason': 'anchor_or_unavailable',
            'adjustment': 0.0,
            'relationship': 'anchor' if pair in MARKET_REGIME_ANCHORS else 'unavailable',
        }

    bias = regime_context.get('bias')
    if bias not in {'bullish', 'bearish'}:
        return {
            'applied': False,
            'reason': 'neutral_regime',
            'adjustment': 0.0,
            'relationship': 'neutral_regime',
        }

    strength = regime_context.get('strength')
    base_adjustment = 8.0 if strength == 'strong' else (5.0 if strength == 'medium' else 3.0)
    regime_sign = 1.0 if bias == 'bullish' else -1.0
    score_sign = 1.0 if score > 0 else (-1.0 if score < 0 else 0.0)
    adjustment = regime_sign * base_adjustment

    if score_sign == 0:
        relationship = 'regime_only'
    elif score_sign == regime_sign:
        relationship = 'tailwind'
    else:
        relationship = 'headwind'

    confidence_cap = None
    if relationship == 'headwind':
        confidence_cap = 'low' if strength == 'strong' else 'medium'
    elif regime_context.get('anchor_conflict'):
        confidence_cap = 'medium'

    return {
        'applied': True,
        'regime': regime_context.get('regime'),
        'bias': bias,
        'strength': strength,
        'regime_score': regime_context.get('regime_score'),
        'anchor_conflict': regime_context.get('anchor_conflict'),
        'relationship': relationship,
        'adjustment': round(adjustment, 1),
        'confidence_cap': confidence_cap,
    }


def evidence_mix_from_sources(sources: set[str]) -> str:
    ordered = sorted(str(item) for item in sources if item)
    return '+'.join(ordered) if ordered else 'none'


def direction_watch_from_score(score: float) -> str:
    if score >= 20:
        return 'rise_watch'
    if score <= -20:
        return 'fall_watch'
    return 'neutral_watch'


def append_catalyst_type(values: list[str], item: str) -> None:
    if item and item not in values:
        values.append(item)


def catalyst_family_from_type(value: str) -> str:
    text = str(value or '')
    if ':' in text:
        return text.split(':', 1)[0]
    if text == 'technical_signal':
        return 'technical'
    if text == 'futures_flow':
        return 'futures'
    return text or 'unknown'


def direction_catalyst_types(
    sources: set[str],
    narrative: Dict[str, Any] | None,
    announcements: list[Dict[str, Any]],
    macro_payload: Dict[str, Any],
    macro_score: float,
) -> list[str]:
    catalyst_types: list[str] = []
    if 'technical' in sources:
        append_catalyst_type(catalyst_types, 'technical_signal')
    if 'futures_capital_flow' in sources:
        append_catalyst_type(catalyst_types, 'futures_flow')
    if narrative:
        append_catalyst_type(catalyst_types, 'news:narrative')
        for source in ((narrative.get('narrative_quality') or {}).get('sources') or []):
            append_catalyst_type(catalyst_types, f"news_source:{source}")
    for ann in announcements[:3]:
        ann_type = str(ann.get('type') or 'general')
        append_catalyst_type(catalyst_types, f'announcement:{ann_type}')
        if ann.get('impact') == 'high':
            append_catalyst_type(catalyst_types, 'announcement:high_impact')
    if macro_score:
        append_catalyst_type(catalyst_types, 'macro:general')
        for item in (macro_payload.get('recent_news') or [])[:10]:
            for category in item.get('categories') or []:
                append_catalyst_type(catalyst_types, f'macro:{category}')
        for event in (macro_payload.get('upcoming_events') or [])[:5]:
            event_type = str(event.get('event_type') or '').strip().lower()
            if event_type:
                append_catalyst_type(catalyst_types, f'macro_event:{event_type}')
    return catalyst_types


def catalyst_families_from_types(catalyst_types: list[str]) -> list[str]:
    families: list[str] = []
    for item in catalyst_types:
        append_catalyst_type(families, catalyst_family_from_type(item))
    return families


def compact_edge_summary(group: str, key: str, summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'group': group,
        'key': key,
        'sample_count': summary.get('sample_count'),
        'hit_rate_pct': summary.get('hit_rate_pct'),
        'avg_directional_return_pct': summary.get('avg_directional_return_pct'),
    }


def local_research_edge_adjustment(
    score: float,
    sources: set[str],
    quality_flags: Dict[str, Any],
    edge_report: Dict[str, Any] | None,
) -> Dict[str, Any]:
    if not edge_report or not edge_report.get('available'):
        return {
            'applied': False,
            'reason': (edge_report or {}).get('reason') or 'edge_unavailable',
        }

    groups = edge_report.get('groups') or {}
    market_regime = quality_flags.get('market_regime') or {}
    direction_conflict = quality_flags.get('direction_conflict') or {}
    technical_alignment_risk = quality_flags.get('technical_alignment_risk') or {}
    volume_confirmation = quality_flags.get('volume_confirmation') or {}
    price_position_risk = quality_flags.get('price_position_risk') or {}
    source_confirmation = quality_flags.get('source_confirmation') or {}
    candidate_direction = direction_watch_from_score(score)
    evidence_mix = evidence_mix_from_sources(sources)
    lookups = [
        ('direction_evidence_mix', f'{candidate_direction}::{evidence_mix}'),
        ('market_regime_relationship', str(market_regime.get('relationship') or 'none')),
        ('direction_conflict', str(direction_conflict.get('severity') or 'none')),
        ('technical_alignment_risk', str(technical_alignment_risk.get('level') or 'unknown')),
        ('volume_confirmation', str(volume_confirmation.get('level') or 'unknown')),
        ('price_position_risk', str(price_position_risk.get('level') or 'unknown')),
        ('source_confirmation', str(source_confirmation.get('level') or 'unknown')),
    ]
    for item in quality_flags.get('catalyst_family') or []:
        lookups.append(('direction_catalyst_family', f'{candidate_direction}::{item}'))
    for item in quality_flags.get('catalyst_types') or []:
        lookups.append(('direction_catalyst_type', f'{candidate_direction}::{item}'))

    matched = []
    for group_name, key in lookups:
        summary = (groups.get(group_name) or {}).get(key)
        if summary:
            matched.append(compact_edge_summary(group_name, key, summary))
    if not matched:
        return {
            'applied': False,
            'reason': 'no_matching_edge_bucket',
            'min_samples': edge_report.get('min_samples'),
            'usable_evaluations': edge_report.get('usable_evaluations'),
        }

    weak_edges = [
        item for item in matched
        if float(item.get('hit_rate_pct') or 0.0) <= 45.0
        and float(item.get('avg_directional_return_pct') or 0.0) <= 0.0
    ]
    strong_edges = [
        item for item in matched
        if float(item.get('hit_rate_pct') or 0.0) >= 62.0
        and float(item.get('avg_directional_return_pct') or 0.0) > 0.0
    ]

    score_sign = 1.0 if score > 0 else (-1.0 if score < 0 else 0.0)
    if weak_edges and score_sign:
        adjustment_abs = min(12.0, 4.0 * len(weak_edges))
        weakest_hit = min(float(item.get('hit_rate_pct') or 0.0) for item in weak_edges)
        return {
            'applied': True,
            'edge': 'weak',
            'adjustment': round(-score_sign * adjustment_abs, 1),
            'confidence_cap': 'low' if weakest_hit <= 35.0 else 'medium',
            'matched_edges': matched,
            'active_edges': weak_edges,
            'min_samples': edge_report.get('min_samples'),
            'usable_evaluations': edge_report.get('usable_evaluations'),
        }

    if strong_edges and score_sign:
        adjustment_abs = min(6.0, 2.0 * len(strong_edges))
        return {
            'applied': True,
            'edge': 'strong',
            'adjustment': round(score_sign * adjustment_abs, 1),
            'confidence_cap': None,
            'matched_edges': matched,
            'active_edges': strong_edges,
            'min_samples': edge_report.get('min_samples'),
            'usable_evaluations': edge_report.get('usable_evaluations'),
        }

    return {
        'applied': False,
        'reason': 'neutral_edge',
        'matched_edges': matched,
        'min_samples': edge_report.get('min_samples'),
        'usable_evaluations': edge_report.get('usable_evaluations'),
    }


def sentiment_points(value: Any) -> int:
    text = str(value or '').lower()
    if '多' in text or 'bull' in text or 'positive' in text or 'rise' in text:
        return 10
    if '空' in text or 'bear' in text or 'negative' in text or 'fall' in text:
        return -10
    return 0


def announcement_points(ann: Dict[str, Any]) -> int:
    ann_type = ann.get('type')
    impact = ann.get('impact')
    if ann_type in {'new_listing', 'futures_listing', 'launchpool'}:
        return 18 if impact == 'high' else 10
    if ann_type == 'delisting':
        return -25 if impact == 'high' else -12
    if impact == 'high':
        return 6
    if impact == 'medium':
        return 3
    return 0


def build_market_direction_board(
    technical_payload: Dict[str, Any],
    narrative_payload: Dict[str, Any],
    futures_payload: Dict[str, Any],
    macro_payload: Dict[str, Any],
    announcement_payload: Dict[str, Any],
    top_n: int,
    direction_edge_report: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    market_regime = build_market_regime_context(technical_payload)
    technical_by_pair = {
        row.get('symbol_pair') or f"{normalize_base_symbol(row.get('symbol'))}USDT": row
        for row in technical_payload.get('watchlist') or []
        if normalize_base_symbol(row.get('symbol_pair') or row.get('symbol'))
    }
    narrative_by_pair = {
        f"{normalize_base_symbol(row.get('symbol'))}USDT": row
        for row in narrative_payload.get('symbol_narrative_view') or []
        if normalize_base_symbol(row.get('symbol'))
    }
    futures_by_pair = {
        row.get('symbol'): row
        for row in futures_payload.get('results') or []
        if row.get('symbol') and not row.get('error')
    }

    announcements_by_pair: dict[str, list[Dict[str, Any]]] = {}
    for ann in announcement_payload.get('announcements') or []:
        for symbol in ann.get('symbols') or []:
            base = normalize_base_symbol(symbol)
            if base:
                announcements_by_pair.setdefault(f'{base}USDT', []).append(ann)

    candidates: list[str] = []
    for source in (technical_by_pair, narrative_by_pair, futures_by_pair, announcements_by_pair):
        for symbol in source.keys():
            base = normalize_base_symbol(symbol)
            pair = f'{base}USDT' if base else None
            if pair and pair not in candidates:
                candidates.append(pair)

    macro_direction = (macro_payload.get('catalyst') or {}).get('news_catalyst_direction')
    macro_score = 3 if macro_direction == 'positive' else (-3 if macro_direction == 'negative' else 0)

    rows = []
    for pair in candidates:
        score = 0.0
        evidence: list[str] = []
        breakdown: dict[str, float] = {}
        quality_flags: dict[str, Any] = {}
        sources: set[str] = set()

        tech = technical_by_pair.get(pair)
        if tech and not tech.get('error'):
            sources.add('technical')
            multi_tf = tech.get('multi_timeframe') or {}
            tech_bias = multi_tf.get('composite_bias') or tech.get('bias')
            if multi_tf.get('valid_timeframes'):
                weighted_score = float(multi_tf.get('weighted_score') or 0)
                tech_points = max(-24.0, min(24.0, weighted_score * 2.5))
                bullish = float(multi_tf.get('weighted_bullish_pts') or 0)
                bearish = float(multi_tf.get('weighted_bearish_pts') or 0)
                quality_flags['technical_quality'] = {
                    'mode': 'multi_timeframe',
                    'valid_timeframes': multi_tf.get('valid_timeframes'),
                    'alignment': multi_tf.get('alignment'),
                    'confidence': multi_tf.get('confidence'),
                    'weighted_score': multi_tf.get('weighted_score'),
                    'direction_votes': multi_tf.get('direction_votes'),
                    'timeframe_biases': multi_tf.get('timeframe_biases'),
                    'primary_atr_pct': (tech.get('indicators') or {}).get('atr_pct'),
                    'primary_rsi': (tech.get('indicators') or {}).get('rsi_1h'),
                    'primary_bb_position': (tech.get('indicators') or {}).get('bb_position'),
                    'primary_price_vs_ma25_pct': (tech.get('indicators') or {}).get('price_vs_ma25_pct'),
                    'primary_volume_trend': (tech.get('indicators') or {}).get('volume_trend'),
                    'timeframe_atr_pct': {
                        interval: ((row or {}).get('indicators') or {}).get('atr_pct')
                        for interval, row in (tech.get('timeframes') or {}).items()
                        if not (row or {}).get('error')
                    },
                    'timeframe_rsi': {
                        interval: ((row or {}).get('indicators') or {}).get('rsi')
                        for interval, row in (tech.get('timeframes') or {}).items()
                        if not (row or {}).get('error') and ((row or {}).get('indicators') or {}).get('rsi') is not None
                    },
                    'timeframe_bb_position': {
                        interval: ((row or {}).get('indicators') or {}).get('bb_position')
                        for interval, row in (tech.get('timeframes') or {}).items()
                        if not (row or {}).get('error') and ((row or {}).get('indicators') or {}).get('bb_position') is not None
                    },
                    'timeframe_price_vs_ma25_pct': {
                        interval: ((row or {}).get('indicators') or {}).get('price_vs_ma25_pct')
                        for interval, row in (tech.get('timeframes') or {}).items()
                        if not (row or {}).get('error') and ((row or {}).get('indicators') or {}).get('price_vs_ma25_pct') is not None
                    },
                    'timeframe_volume_trend': {
                        interval: ((row or {}).get('indicators') or {}).get('volume_trend')
                        for interval, row in (tech.get('timeframes') or {}).items()
                        if not (row or {}).get('error') and ((row or {}).get('indicators') or {}).get('volume_trend')
                    },
                    'kline_market': tech.get('kline_market'),
                }
            else:
                bullish = float(tech.get('bullish_pts') or 0)
                bearish = float(tech.get('bearish_pts') or 0)
                tech_points = max(-24.0, min(24.0, (bullish - bearish) * 2.5))
                quality_flags['technical_quality'] = {
                    'mode': 'single_timeframe',
                    'primary_atr_pct': (tech.get('indicators') or {}).get('atr_pct'),
                    'primary_rsi': (tech.get('indicators') or {}).get('rsi_1h'),
                    'primary_bb_position': (tech.get('indicators') or {}).get('bb_position'),
                    'primary_price_vs_ma25_pct': (tech.get('indicators') or {}).get('price_vs_ma25_pct'),
                    'primary_volume_trend': (tech.get('indicators') or {}).get('volume_trend'),
                    'kline_market': tech.get('kline_market'),
                }
            if tech_bias == 'bullish':
                tech_points += 10
            elif tech_bias == 'bearish':
                tech_points -= 10
            breakdown['technical'] = round(tech_points, 1)
            score += tech_points
            evidence.append(
                f"technical={tech_bias} "
                f"(bullish_pts={round(bullish, 2)}, bearish_pts={round(bearish, 2)}, "
                f"alignment={multi_tf.get('alignment')}, market={tech.get('kline_market')})"
            )

        narrative = narrative_by_pair.get(pair)
        if narrative:
            sources.add('narrative_news')
            narrative_quality = narrative.get('narrative_quality') or {}
            source_weight = float(narrative_quality.get('direction_weight') or narrative_quality.get('source_weight') or 1.0)
            pts = sentiment_points(narrative.get('dominant_sentiment'))
            if pts > 0 and float(narrative.get('avg_signal_score') or 0) >= 70:
                pts += 4
            elif pts < 0 and float(narrative.get('avg_signal_score') or 0) >= 70:
                pts -= 4
            pts = round(pts * source_weight, 1)
            breakdown['narrative_news'] = pts
            score += pts
            quality_flags['narrative_quality'] = narrative_quality
            evidence.append(
                f"narrative={narrative.get('dominant_sentiment')} "
                f"(posts={narrative.get('post_count')}, avg_score={narrative.get('avg_signal_score')}, "
                f"direction_weight={round(source_weight, 3)}, quality={narrative_quality.get('quality_label')})"
            )

        futures = futures_by_pair.get(pair)
        if futures:
            comp = futures.get('composite') or {}
            bias = comp.get('capital_flow_bias')
            pts = 10 if bias == 'bullish' else (-10 if bias == 'bearish' else 0)
            breakdown['futures_capital_flow'] = pts
            score += pts
            sources.add('futures_capital_flow')
            evidence.append(f"futures={bias} (score={comp.get('capital_flow_score')})")

        anns = announcements_by_pair.get(pair) or []
        if anns:
            sources.add('announcements')
            pts = sum(announcement_points(ann) for ann in anns[:3])
            breakdown['announcements'] = pts
            score += pts
            evidence.append(
                'announcements=' + ', '.join(
                    f"{ann.get('type')}:{ann.get('impact')}" for ann in anns[:3]
                )
            )

        if macro_score:
            breakdown['macro_news'] = macro_score
            score += macro_score
            sources.add('macro_news')
            evidence.append(f"macro_news={macro_direction}")

        raw_score_before_conflict = score
        conflict_report = build_direction_conflict_report(breakdown)
        if conflict_report:
            penalty = float(conflict_report.get('score_penalty_abs') or 0.0)
            if raw_score_before_conflict > 0:
                conflict_adjustment = -penalty
            elif raw_score_before_conflict < 0:
                conflict_adjustment = penalty
            else:
                conflict_adjustment = 0.0
            if conflict_adjustment:
                score += conflict_adjustment
                breakdown['conflict_penalty'] = round(conflict_adjustment, 1)
            conflict_report['raw_score_before_penalty'] = round(clamp_direction_score(raw_score_before_conflict), 1)
            conflict_report['score_adjustment'] = round(conflict_adjustment, 1)
            quality_flags['direction_conflict'] = conflict_report
            evidence.append(
                f"direction_conflict={conflict_report.get('severity')} "
                f"(bull={conflict_report.get('positive_points')}, "
                f"bear={conflict_report.get('negative_points_abs')}, "
                f"penalty={round(abs(conflict_adjustment), 1)})"
            )

        regime_adjustment = market_regime_adjustment(pair, score, market_regime)
        if regime_adjustment.get('applied'):
            adjustment = float(regime_adjustment.get('adjustment') or 0.0)
            score += adjustment
            breakdown['market_regime'] = round(adjustment, 1)
            quality_flags['market_regime'] = {
                **regime_adjustment,
                'context': market_regime,
            }
            sources.add('market_regime')
            evidence.append(
                f"market_regime={regime_adjustment.get('regime')} "
                f"({regime_adjustment.get('relationship')}, "
                f"score={regime_adjustment.get('regime_score')}, "
                f"adjustment={round(adjustment, 1)})"
            )
        else:
            quality_flags['market_regime'] = {
                **regime_adjustment,
                'context': market_regime,
            }

        technical_alignment_risk = technical_alignment_risk_adjustment(
            score,
            quality_flags.get('technical_quality') or {},
        )
        quality_flags['technical_alignment_risk'] = technical_alignment_risk
        if technical_alignment_risk.get('applied'):
            adjustment = float(technical_alignment_risk.get('adjustment') or 0.0)
            score += adjustment
            breakdown['technical_alignment_risk'] = round(adjustment, 1)
            evidence.append(
                f"technical_alignment_risk={technical_alignment_risk.get('level')} "
                f"(alignment={technical_alignment_risk.get('alignment')}, "
                f"support={technical_alignment_risk.get('supporting_votes')}, "
                f"oppose={technical_alignment_risk.get('opposing_votes')}, "
                f"adjustment={round(adjustment, 1)}, "
                f"cap={technical_alignment_risk.get('confidence_cap')})"
            )

        volume_confirmation = volume_confirmation_adjustment(
            score,
            quality_flags.get('technical_quality') or {},
        )
        quality_flags['volume_confirmation'] = volume_confirmation
        if volume_confirmation.get('applied'):
            adjustment = float(volume_confirmation.get('adjustment') or 0.0)
            score += adjustment
            breakdown['volume_confirmation'] = round(adjustment, 1)
            evidence.append(
                f"volume_confirmation={volume_confirmation.get('level')} "
                f"(primary={volume_confirmation.get('primary_trend')}, "
                f"ratio={volume_confirmation.get('primary_ratio')}, "
                f"expanding={volume_confirmation.get('expanding_count')}, "
                f"contracting={volume_confirmation.get('contracting_count')}, "
                f"adjustment={round(adjustment, 1)}, "
                f"cap={volume_confirmation.get('confidence_cap')})"
            )

        price_position_risk = price_position_risk_adjustment(
            score,
            quality_flags.get('technical_quality') or {},
        )
        quality_flags['price_position_risk'] = price_position_risk
        if price_position_risk.get('applied'):
            adjustment = float(price_position_risk.get('adjustment') or 0.0)
            score += adjustment
            breakdown['price_position_risk'] = round(adjustment, 1)
            evidence.append(
                f"price_position_risk={price_position_risk.get('level')} "
                f"(side={price_position_risk.get('side')}, "
                f"votes={price_position_risk.get('overextended_votes')}, "
                f"adjustment={round(adjustment, 1)}, "
                f"cap={price_position_risk.get('confidence_cap')})"
            )

        volatility_risk = volatility_risk_adjustment(score, quality_flags.get('technical_quality') or {})
        quality_flags['volatility_risk'] = volatility_risk
        if volatility_risk.get('applied'):
            adjustment = float(volatility_risk.get('adjustment') or 0.0)
            score += adjustment
            breakdown['volatility_risk'] = round(adjustment, 1)
            evidence.append(
                f"volatility_risk={volatility_risk.get('level')} "
                f"(primary_atr={volatility_risk.get('primary_atr_pct')}, "
                f"max_atr={volatility_risk.get('max_atr_pct')}, "
                f"adjustment={round(adjustment, 1)}, "
                f"cap={volatility_risk.get('confidence_cap')})"
            )

        futures_crowding_risk = futures_crowding_risk_adjustment(score, futures)
        quality_flags['futures_crowding_risk'] = futures_crowding_risk
        if futures_crowding_risk.get('applied'):
            adjustment = float(futures_crowding_risk.get('adjustment') or 0.0)
            score += adjustment
            breakdown['futures_crowding_risk'] = round(adjustment, 1)
            evidence.append(
                f"futures_crowding_risk={futures_crowding_risk.get('level')} "
                f"({futures_crowding_risk.get('direction_side')}, "
                f"funding={futures_crowding_risk.get('funding_signal')}, "
                f"oi={futures_crowding_risk.get('oi_signal')}, "
                f"ls={futures_crowding_risk.get('long_short_signal')}, "
                f"adjustment={round(adjustment, 1)}, "
                f"cap={futures_crowding_risk.get('confidence_cap')})"
            )

        source_confirmation = source_confirmation_adjustment(score, sources, breakdown)
        quality_flags['source_confirmation'] = source_confirmation
        if source_confirmation.get('applied'):
            adjustment = float(source_confirmation.get('adjustment') or 0.0)
            score += adjustment
            breakdown['source_confirmation'] = round(adjustment, 1)
            evidence.append(
                f"source_confirmation={source_confirmation.get('level')} "
                f"(supporting={','.join(source_confirmation.get('supporting_sources') or [])}, "
                f"opposing={','.join(source_confirmation.get('opposing_sources') or [])}, "
                f"adjustment={round(adjustment, 1)}, "
                f"cap={source_confirmation.get('confidence_cap')})"
            )

        catalyst_types = direction_catalyst_types(
            sources,
            narrative,
            anns,
            macro_payload,
            macro_score,
        )
        if catalyst_types:
            quality_flags['catalyst_types'] = catalyst_types
            quality_flags['catalyst_family'] = catalyst_families_from_types(catalyst_types)

        local_edge = local_research_edge_adjustment(score, sources, quality_flags, direction_edge_report)
        if local_edge.get('applied'):
            edge_adjustment = float(local_edge.get('adjustment') or 0.0)
            score += edge_adjustment
            breakdown['local_research_edge'] = round(edge_adjustment, 1)
            quality_flags['local_research_edge'] = local_edge
            evidence.append(
                f"local_research_edge={local_edge.get('edge')} "
                f"(adjustment={round(edge_adjustment, 1)}, "
                f"cap={local_edge.get('confidence_cap')})"
            )
        elif direction_edge_report:
            quality_flags['local_research_edge'] = local_edge

        score = round(clamp_direction_score(score), 1)
        direction = direction_watch_from_score(score)

        if abs(score) >= 40 and len(sources) >= 3:
            confidence = 'high'
        elif abs(score) >= 20 and len(sources) >= 2:
            confidence = 'medium'
        else:
            confidence = 'low'
        if conflict_report:
            confidence = cap_confidence_for_conflict(confidence, conflict_report)
        if regime_adjustment.get('confidence_cap'):
            confidence = cap_confidence(confidence, str(regime_adjustment.get('confidence_cap')))
        if technical_alignment_risk.get('confidence_cap'):
            confidence = cap_confidence(confidence, str(technical_alignment_risk.get('confidence_cap')))
        if volume_confirmation.get('confidence_cap'):
            confidence = cap_confidence(confidence, str(volume_confirmation.get('confidence_cap')))
        if price_position_risk.get('confidence_cap'):
            confidence = cap_confidence(confidence, str(price_position_risk.get('confidence_cap')))
        if volatility_risk.get('confidence_cap'):
            confidence = cap_confidence(confidence, str(volatility_risk.get('confidence_cap')))
        if futures_crowding_risk.get('confidence_cap'):
            confidence = cap_confidence(confidence, str(futures_crowding_risk.get('confidence_cap')))
        if source_confirmation.get('confidence_cap'):
            confidence = cap_confidence(confidence, str(source_confirmation.get('confidence_cap')))
        if local_edge.get('confidence_cap'):
            confidence = cap_confidence(confidence, str(local_edge.get('confidence_cap')))

        rows.append({
            'symbol': pair,
            'direction_watch': direction,
            'direction_score': score,
            'confidence': confidence,
            'evidence_sources': sorted(sources),
            'score_breakdown': breakdown,
            'quality_flags': quality_flags,
            'evidence': evidence,
        })

    rows.sort(key=lambda row: (abs(row['direction_score']), len(row['evidence_sources'])), reverse=True)
    rows = rows[:top_n]
    return {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'market_direction_board_v1',
            'top_n': top_n,
        },
        'summary': {
            'tracked_symbols': len(rows),
            'rise_watch': len([row for row in rows if row['direction_watch'] == 'rise_watch']),
            'fall_watch': len([row for row in rows if row['direction_watch'] == 'fall_watch']),
            'neutral_watch': len([row for row in rows if row['direction_watch'] == 'neutral_watch']),
        },
        'market_regime': market_regime,
        'rows': rows,
    }


def build_direction_only_payload(
    limit: int,
    start_page: int,
    max_pages: int,
    top_n: int,
    technical_limit: int,
    lookback_hours: int = 72,
    include_web_search: bool = False,
    event_cooldown_hours: int = DEFAULT_EVENT_COOLDOWN_HOURS,
    event_memory_db: Path | None = None,
    direction_edge_db: Path | None = None,
    direction_edge_min_samples: int = 8,
    enable_direction_edge: bool = True,
) -> Dict[str, Any]:
    narrative_raw = []
    narrative_source_stats: Dict[str, Any] = {}

    try:
        bs_posts = scrape_square(limit=limit, start_page=start_page, max_pages=max_pages)
        narrative_raw.extend(bs_posts)
        narrative_source_stats['binance_square'] = len(bs_posts)
    except Exception as exc:
        narrative_source_stats['binance_square_error'] = str(exc)

    try:
        bb_raw = fetch_blockbeats_news(25)
        bb_posts = bb_to_narrative(bb_raw)
        enrich_post_symbols(bb_posts)
        narrative_raw.extend(bb_posts)
        narrative_source_stats['blockbeats'] = len(bb_posts)
    except Exception as exc:
        narrative_source_stats['blockbeats_error'] = str(exc)

    try:
        news_raw = fetch_crypto_news() if include_web_search else fetch_crypto_rss()
        news_posts = news_to_narrative(news_raw)
        enrich_post_symbols(news_posts)
        narrative_raw.extend(news_posts)
        narrative_source_stats['crypto_news'] = len(news_posts)
        narrative_source_stats['crypto_news_mode'] = 'rss_plus_web_search' if include_web_search else 'rss_only'
    except Exception as exc:
        narrative_source_stats['crypto_news_error'] = str(exc)

    narrative_raw_before_freshness = len(narrative_raw)
    narrative_raw, freshness_stats = filter_recent_posts(narrative_raw, lookback_hours)
    narrative_raw_after_freshness = len(narrative_raw)
    narrative_raw, dedupe_stats = dedupe_narrative_raw_posts(narrative_raw)
    narrative_source_stats['freshness_filter'] = freshness_stats
    narrative_source_stats['dedupe_filter'] = dedupe_stats

    narrative_posts = build_narrative_posts(narrative_raw)
    narrative_rank = aggregate_narratives(narrative_posts)
    symbol_narrative_view = build_symbol_narrative_view(narrative_posts)
    event_cooldown_lookup, event_cooldown_stats = apply_narrative_event_cooldown(
        narrative_posts,
        db_path=event_memory_db,
        cooldown_hours=event_cooldown_hours,
    )
    narrative_source_stats['event_cooldown'] = event_cooldown_stats
    author_accuracy = load_author_accuracy_map()
    attach_symbol_narrative_quality(symbol_narrative_view, narrative_posts, author_accuracy, event_cooldown_lookup)
    source_quality = build_source_quality_report(narrative_posts, author_accuracy, freshness_stats)
    narrative_payload = {
        'source_stats': narrative_source_stats,
        'source_quality': source_quality,
        'summary': {
            'fetched_posts': narrative_raw_before_freshness,
            'fresh_posts': narrative_raw_after_freshness,
            'deduped_posts': len(narrative_raw),
            'stale_posts_filtered': freshness_stats.get('stale_posts_filtered', 0),
            'duplicate_posts_filtered': dedupe_stats.get('duplicate_posts_filtered', 0),
            'cooldown_repeated_events': event_cooldown_stats.get('repeated_events', 0),
            'tagged_posts': len(narrative_posts),
            'narratives': len(narrative_rank),
            'symbols': len(symbol_narrative_view),
        },
        'top_narratives': narrative_rank[:top_n],
        'symbol_narrative_view': symbol_narrative_view[:max(top_n, technical_limit)],
    }

    technical_symbols = collect_technical_symbols(
        hotspot={},
        narrative_payload=narrative_payload,
        futures_symbols=None,
        limit=technical_limit,
    )
    technical_payload = build_technical_payload(technical_symbols)
    direction_edge_report = (
        build_direction_edge_report(
            db_path=direction_edge_db or DIRECTION_DB_PATH,
            min_samples=direction_edge_min_samples,
        )
        if enable_direction_edge
        else {'available': False, 'reason': 'disabled', 'groups': {}}
    )

    empty_payload: Dict[str, Any] = {'results': [], 'announcements': [], 'catalyst': {}}
    market_direction_payload = build_market_direction_board(
        technical_payload=technical_payload,
        narrative_payload=narrative_payload,
        futures_payload=empty_payload,
        macro_payload=empty_payload,
        announcement_payload=empty_payload,
        top_n=top_n,
        direction_edge_report=direction_edge_report,
    )

    system_summary = {
        'trend_candidates': 0,
        'buy_candidates': 0,
        'strong_capital_buy_candidates': 0,
        'top_narratives': len(narrative_payload.get('top_narratives') or []),
        'onchain_strong_buy_candidates': 0,
        'onchain_observation_candidates': 0,
        'futures_symbols': 0,
        'technical_symbols': (technical_payload.get('summary') or {}).get('analyzed_symbols', 0),
        'technical_bullish': (technical_payload.get('summary') or {}).get('bullish', 0),
        'technical_bearish': (technical_payload.get('summary') or {}).get('bearish', 0),
        'upcoming_events': 0,
        'recent_news': 0,
        'announcement_total': 0,
        'announcement_high_impact': 0,
        'direction_rise_watch': (market_direction_payload.get('summary') or {}).get('rise_watch', 0),
        'direction_fall_watch': (market_direction_payload.get('summary') or {}).get('fall_watch', 0),
        'direction_neutral_watch': (market_direction_payload.get('summary') or {}).get('neutral_watch', 0),
    }

    focus_board = {
        'market_direction_board': market_direction_payload.get('rows') or [],
        'strong_capital_buy_candidates': [],
        'buy_candidates': [],
        'top_narratives': narrative_payload.get('top_narratives') or [],
        'onchain_strong_buy_candidates': [],
        'onchain_observation_candidates': [],
        'high_impact_announcements': [],
        'technical_watchlist': technical_payload.get('watchlist') or [],
    }

    return {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'direction_only_v1',
            'limit': limit,
            'start_page': start_page,
            'max_pages': max_pages,
            'top_n': top_n,
            'technical_limit': technical_limit,
            'lookback_hours': lookback_hours,
            'include_web_search': include_web_search,
            'event_cooldown_hours': event_cooldown_hours,
            'event_memory_db': str(event_memory_db or DEFAULT_EVENT_MEMORY_DB),
            'direction_edge_enabled': enable_direction_edge,
            'direction_edge_db': str(direction_edge_db or DIRECTION_DB_PATH),
            'direction_edge_min_samples': direction_edge_min_samples,
        },
        'system_summary': system_summary,
        'focus_board': focus_board,
        'narrative_monitor': narrative_payload,
        'technical_monitor': technical_payload,
        'market_direction_board': market_direction_payload,
    }


def build_hub_payload(
    limit: int,
    start_page: int,
    max_pages: int,
    top_n: int,
    onchain_limit: int,
    futures_symbols: list[str] | None = None,
    technical_limit: int = 12,
    lookback_hours: int = 72,
    event_cooldown_hours: int = DEFAULT_EVENT_COOLDOWN_HOURS,
    event_memory_db: Path | None = None,
    meme_limit: int = 30,
    meme_max_age_hours: int = 72,
    meme_min_liquidity_usd: float = 10_000.0,
    meme_token_cooldown_hours: int = DEFAULT_MEME_TOKEN_COOLDOWN_HOURS,
    meme_token_memory_db: Path | None = None,
    meme_security_check: bool = True,
    goplus_api_token: str | None = None,
    meme_extra_candidates: list[Dict[str, Any]] | None = None,
    meme_research_edge_db: Path | None = None,
    meme_edge_min_samples: int = 8,
    enable_meme_research_edge: bool = True,
    include_meme_early: bool = True,
    direction_edge_db: Path | None = None,
    direction_edge_min_samples: int = 8,
    enable_direction_edge: bool = True,
) -> Dict[str, Any]:
    # ── 热点雷达 ──
    hotspot = build_hotspot_payload(limit=limit, start_page=start_page, max_pages=max_pages, top_n=top_n)

    # ── 叙事监控（多数据源 v2）──
    narrative_raw = []
    narrative_source_stats = {}

    # Binance Square
    try:
        bs_posts = scrape_square(limit=limit, start_page=start_page, max_pages=max_pages)
        narrative_raw.extend(bs_posts)
        narrative_source_stats['binance_square'] = len(bs_posts)
    except Exception as exc:
        narrative_source_stats['binance_square_error'] = str(exc)

    # BlockBeats
    try:
        bb_raw = fetch_blockbeats_news(25)
        bb_posts = bb_to_narrative(bb_raw)
        enrich_post_symbols(bb_posts)
        narrative_raw.extend(bb_posts)
        narrative_source_stats['blockbeats'] = len(bb_posts)
    except Exception as exc:
        narrative_source_stats['blockbeats_error'] = str(exc)

    # DefiLlama TVL 异动
    try:
        protocols = fetch_protocols()
        tvl_changes = detect_tvl_changes(protocols, change_threshold_pct=15.0)
        dl_posts = dl_to_narrative(tvl_changes)
        narrative_raw.extend(dl_posts)
        narrative_source_stats['defillama'] = len(dl_posts)
        narrative_source_stats['defillama_protocols'] = len(protocols)
    except Exception as exc:
        narrative_source_stats['defillama_error'] = str(exc)

    # Crypto News (CoinTelegraph / The Block RSS)
    try:
        news_raw = fetch_crypto_news()
        news_posts = news_to_narrative(news_raw)
        enrich_post_symbols(news_posts)
        narrative_raw.extend(news_posts)
        narrative_source_stats['crypto_news'] = len(news_posts)
    except Exception as exc:
        narrative_source_stats['crypto_news_error'] = str(exc)

    # Twitter 6551 (KOL tweets + search)
    if HAS_TWITTER_6551:
        try:
            tw6551_kol = fetch_tw6551_kol(max_profiles=6, tweets_per_profile=8)
            tw6551_search = fetch_tw6551_search(min_likes=20, max_results=10)
            tw6551_all = tw6551_kol + tw6551_search
            tw6551_posts = tw6551_to_narrative(tw6551_all)
            enrich_post_symbols(tw6551_posts)
            narrative_raw.extend(tw6551_posts)
            narrative_source_stats['twitter_6551'] = len(tw6551_posts)
        except Exception as exc:
            narrative_source_stats['twitter_6551_error'] = str(exc)

    narrative_raw_before_freshness = len(narrative_raw)
    narrative_raw, freshness_stats = filter_recent_posts(narrative_raw, lookback_hours)
    narrative_raw_after_freshness = len(narrative_raw)
    narrative_raw, dedupe_stats = dedupe_narrative_raw_posts(narrative_raw)
    narrative_source_stats['freshness_filter'] = freshness_stats
    narrative_source_stats['dedupe_filter'] = dedupe_stats

    narrative_posts = build_narrative_posts(narrative_raw)
    narrative_rank = aggregate_narratives(narrative_posts)
    symbol_narrative_view = build_symbol_narrative_view(narrative_posts)
    event_cooldown_lookup, event_cooldown_stats = apply_narrative_event_cooldown(
        narrative_posts,
        db_path=event_memory_db,
        cooldown_hours=event_cooldown_hours,
    )
    narrative_source_stats['event_cooldown'] = event_cooldown_stats
    author_accuracy = load_author_accuracy_map()
    attach_symbol_narrative_quality(symbol_narrative_view, narrative_posts, author_accuracy, event_cooldown_lookup)
    source_quality = build_source_quality_report(narrative_posts, author_accuracy, freshness_stats)
    narrative_payload = {
        'source_stats': narrative_source_stats,
        'source_quality': source_quality,
        'summary': {
            'fetched_posts': narrative_raw_before_freshness,
            'fresh_posts': narrative_raw_after_freshness,
            'deduped_posts': len(narrative_raw),
            'stale_posts_filtered': freshness_stats.get('stale_posts_filtered', 0),
            'duplicate_posts_filtered': dedupe_stats.get('duplicate_posts_filtered', 0),
            'cooldown_repeated_events': event_cooldown_stats.get('repeated_events', 0),
            'tagged_posts': len(narrative_posts),
            'narratives': len(narrative_rank),
            'symbols': len(symbol_narrative_view),
        },
        'top_narratives': narrative_rank[:top_n],
        'symbol_narrative_view': symbol_narrative_view[:top_n],
    }

    # ── 链上强买盘 ──
    onchain = build_onchain_payload(limit=onchain_limit, top_n=top_n)

    # ── 一级 MEME 早期雷达 ──
    if include_meme_early:
        meme_early = build_meme_early_payload(
            limit=meme_limit,
            top_n=top_n,
            max_age_hours=meme_max_age_hours,
            min_liquidity_usd=meme_min_liquidity_usd,
            token_cooldown_hours=meme_token_cooldown_hours,
            token_memory_db=meme_token_memory_db,
            enable_security_check=meme_security_check,
            security_api_token=goplus_api_token,
            extra_candidates=meme_extra_candidates,
            enable_research_edge=enable_meme_research_edge,
            research_edge_db=meme_research_edge_db,
            research_edge_min_samples=meme_edge_min_samples,
        )
    else:
        meme_early = {
            'summary': {},
            'early_watch': [],
            'momentum_watch': [],
            'watchlist': [],
            'rug_risk': [],
            'overheated': [],
            'all_candidates': [],
        }

    # ── 主流币合约监控 ──
    futures_payload = build_futures_payload(symbols=futures_symbols)

    # ── 宏观日历 ──
    macro_payload = build_macro_payload()

    technical_symbols = collect_technical_symbols(
        hotspot=hotspot,
        narrative_payload=narrative_payload,
        futures_symbols=futures_symbols,
        limit=technical_limit,
    )
    technical_payload = build_technical_payload(technical_symbols)
    direction_edge_report = (
        build_direction_edge_report(
            db_path=direction_edge_db or DIRECTION_DB_PATH,
            min_samples=direction_edge_min_samples,
        )
        if enable_direction_edge
        else {'available': False, 'reason': 'disabled', 'groups': {}}
    )

    # ── 币安公告监控 ──
    announcement_payload = build_announcement_payload(
        limit=limit, start_page=start_page, max_pages=max_pages,
    )
    market_direction_payload = build_market_direction_board(
        technical_payload=technical_payload,
        narrative_payload=narrative_payload,
        futures_payload=futures_payload,
        macro_payload=macro_payload,
        announcement_payload=announcement_payload,
        top_n=top_n,
        direction_edge_report=direction_edge_report,
    )

    system_summary = {
        'trend_candidates': len(hotspot.get('top_opportunities') or []),
        'buy_candidates': len(hotspot.get('buy_candidates') or []),
        'strong_capital_buy_candidates': len(hotspot.get('strong_capital_buy_candidates') or []),
        'top_narratives': len(narrative_payload.get('top_narratives') or []),
        'onchain_strong_buy_candidates': len(onchain.get('strong_buy_candidates') or []),
        'onchain_observation_candidates': len(onchain.get('observation_candidates') or []),
        'meme_early_watch': (meme_early.get('summary') or {}).get('early_watch', 0),
        'meme_momentum_watch': (meme_early.get('summary') or {}).get('momentum_watch', 0),
        'meme_rug_risk': (meme_early.get('summary') or {}).get('rug_risk', 0),
        'meme_overheated': (meme_early.get('summary') or {}).get('overheated', 0),
        'futures_symbols': len(futures_payload.get('results') or []),
        'technical_symbols': (technical_payload.get('summary') or {}).get('analyzed_symbols', 0),
        'technical_bullish': (technical_payload.get('summary') or {}).get('bullish', 0),
        'technical_bearish': (technical_payload.get('summary') or {}).get('bearish', 0),
        'upcoming_events': len(macro_payload.get('upcoming_events') or []),
        'recent_news': len(macro_payload.get('recent_news') or []),
        'announcement_total': (announcement_payload.get('summary') or {}).get('total_announcements', 0),
        'announcement_high_impact': (announcement_payload.get('summary') or {}).get('high_impact', 0),
        'direction_rise_watch': (market_direction_payload.get('summary') or {}).get('rise_watch', 0),
        'direction_fall_watch': (market_direction_payload.get('summary') or {}).get('fall_watch', 0),
        'direction_neutral_watch': (market_direction_payload.get('summary') or {}).get('neutral_watch', 0),
    }

    focus_board = {
        'market_direction_board': market_direction_payload.get('rows') or [],
        'strong_capital_buy_candidates': hotspot.get('strong_capital_buy_candidates') or [],
        'buy_candidates': hotspot.get('buy_candidates') or [],
        'top_narratives': narrative_payload.get('top_narratives') or [],
        'onchain_strong_buy_candidates': onchain.get('strong_buy_candidates') or [],
        'onchain_observation_candidates': onchain.get('observation_candidates') or [],
        'meme_early_watch': meme_early.get('early_watch') or [],
        'meme_momentum_watch': meme_early.get('momentum_watch') or [],
        'meme_rug_risk': meme_early.get('rug_risk') or [],
        'high_impact_announcements': announcement_payload.get('high_impact_announcements') or [],
        'technical_watchlist': technical_payload.get('watchlist') or [],
    }

    return {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'monitoring_hub_v1',
            'limit': limit,
            'start_page': start_page,
            'max_pages': max_pages,
            'top_n': top_n,
            'onchain_limit': onchain_limit,
            'technical_limit': technical_limit,
            'lookback_hours': lookback_hours,
            'event_cooldown_hours': event_cooldown_hours,
            'event_memory_db': str(event_memory_db or DEFAULT_EVENT_MEMORY_DB),
            'meme_limit': meme_limit,
            'meme_max_age_hours': meme_max_age_hours,
            'meme_min_liquidity_usd': meme_min_liquidity_usd,
            'meme_token_cooldown_hours': meme_token_cooldown_hours,
            'meme_token_memory_db': str(meme_token_memory_db or DEFAULT_MEME_TOKEN_MEMORY_DB),
            'meme_security_check': meme_security_check,
            'meme_research_edge_enabled': enable_meme_research_edge,
            'meme_research_edge_db': str(meme_research_edge_db or DEFAULT_MEME_RESEARCH_DB),
            'meme_edge_min_samples': meme_edge_min_samples,
            'include_meme_early': include_meme_early,
            'direction_edge_enabled': enable_direction_edge,
            'direction_edge_db': str(direction_edge_db or DIRECTION_DB_PATH),
            'direction_edge_min_samples': direction_edge_min_samples,
        },
        'system_summary': system_summary,
        'focus_board': focus_board,
        'trend_monitor': {
            'summary': hotspot.get('summary'),
            'strong_capital_buy_candidates': hotspot.get('strong_capital_buy_candidates'),
            'buy_candidates': hotspot.get('buy_candidates'),
            'top_opportunities': hotspot.get('top_opportunities'),
            'timing_ranking': hotspot.get('timing_ranking'),
        },
        'narrative_monitor': narrative_payload,
        'onchain_strong_buy_monitor': onchain,
        'meme_early_radar': meme_early,
        'major_coin_futures_monitor': futures_payload,
        'technical_monitor': technical_payload,
        'market_direction_board': market_direction_payload,
        'macro_calendar_monitor': macro_payload,
        'announcement_monitor': announcement_payload,
    }


def render_markdown(payload: Dict[str, Any]) -> str:
    lines = []
    lines.append('# Monitoring Hub')
    lines.append('')
    summary = payload.get('system_summary') or {}
    lines.append(f"- trend_candidates: **{summary.get('trend_candidates', 0)}**")
    lines.append(f"- buy_candidates: **{summary.get('buy_candidates', 0)}**")
    lines.append(f"- strong_capital_buy_candidates: **{summary.get('strong_capital_buy_candidates', 0)}**")
    lines.append(f"- top_narratives: **{summary.get('top_narratives', 0)}**")
    lines.append(f"- onchain_strong_buy_candidates: **{summary.get('onchain_strong_buy_candidates', 0)}**")
    lines.append(
        f"- meme_early: early **{summary.get('meme_early_watch', 0)}** / "
        f"momentum **{summary.get('meme_momentum_watch', 0)}** / "
        f"risk **{summary.get('meme_rug_risk', 0)}** / "
        f"overheated **{summary.get('meme_overheated', 0)}**"
    )
    lines.append(f"- announcements: **{summary.get('announcement_total', 0)}** (high: {summary.get('announcement_high_impact', 0)})")
    lines.append(f"- futures_symbols: **{summary.get('futures_symbols', 0)}**")
    lines.append(f"- technical_symbols: **{summary.get('technical_symbols', 0)}** (bullish: {summary.get('technical_bullish', 0)}, bearish: {summary.get('technical_bearish', 0)})")
    lines.append(f"- direction_watch: rise **{summary.get('direction_rise_watch', 0)}** / fall **{summary.get('direction_fall_watch', 0)}** / neutral **{summary.get('direction_neutral_watch', 0)}**")
    narrative_summary = (payload.get('narrative_monitor') or {}).get('summary') or {}
    if narrative_summary:
        lines.append(
            f"- narrative_posts: fetched **{narrative_summary.get('fetched_posts', 0)}** / "
            f"fresh **{narrative_summary.get('fresh_posts', 0)}** / "
            f"deduped **{narrative_summary.get('deduped_posts', 0)}** "
            f"(stale_filtered: {narrative_summary.get('stale_posts_filtered', 0)}, "
            f"duplicates_filtered: {narrative_summary.get('duplicate_posts_filtered', 0)}, "
            f"cooldown_repeated: {narrative_summary.get('cooldown_repeated_events', 0)})"
        )
    lines.append(f"- upcoming_events: **{summary.get('upcoming_events', 0)}**")
    lines.append(f"- recent_news: **{summary.get('recent_news', 0)}**")
    lines.append('')

    research_bits = []
    direction_recording = (payload.get('direction_research') or {}).get('recording') or {}
    if direction_recording:
        research_bits.append(f"direction_signals={direction_recording.get('signals_recorded', 0)}")
    meme_recording = (payload.get('meme_research') or {}).get('recording') or {}
    if meme_recording:
        research_bits.append(f"meme_signals={meme_recording.get('signals_recorded', 0)}")
    if research_bits:
        lines.append('## Research Recording')
        lines.append('')
        lines.append('- ' + ' | '.join(research_bits))
        lines.append('')

    direction_board = payload.get('market_direction_board') or {}
    if direction_board.get('rows'):
        lines.append('## Market Direction Board')
        lines.append('')
        market_regime = direction_board.get('market_regime') or {}
        if market_regime:
            lines.append(
                f"- market_regime: `{market_regime.get('regime')}` "
                f"(bias={market_regime.get('bias')}, strength={market_regime.get('strength')}, "
                f"score={market_regime.get('regime_score')}, "
                f"anchor_conflict={market_regime.get('anchor_conflict')})"
            )
            lines.append('')
        for row in direction_board.get('rows') or []:
            lines.append(
                f"### {row.get('symbol')} - {row.get('direction_watch')} "
                f"({row.get('direction_score')}, {row.get('confidence')})"
            )
            lines.append(f"- sources: `{', '.join(row.get('evidence_sources') or [])}`")
            catalyst_types = (row.get('quality_flags') or {}).get('catalyst_types') or []
            if catalyst_types:
                lines.append(f"- catalysts: `{', '.join(catalyst_types[:6])}`")
            if row.get('score_breakdown'):
                pieces = [f"{name}: {points}" for name, points in row['score_breakdown'].items()]
                lines.append(f"- score_breakdown: {'; '.join(pieces)}")
            technical_quality = (row.get('quality_flags') or {}).get('technical_quality') or {}
            if technical_quality:
                lines.append(
                    f"- technical_quality: `{technical_quality.get('mode')}` "
                    f"(alignment={technical_quality.get('alignment')}, "
                    f"confidence={technical_quality.get('confidence')}, "
                    f"market={technical_quality.get('kline_market')})"
                )
            technical_alignment_risk = (row.get('quality_flags') or {}).get('technical_alignment_risk') or {}
            if technical_alignment_risk.get('applied'):
                lines.append(
                    f"- technical_alignment_risk: `{technical_alignment_risk.get('level')}` "
                    f"(alignment={technical_alignment_risk.get('alignment')}, "
                    f"support={technical_alignment_risk.get('supporting_votes')}, "
                    f"oppose={technical_alignment_risk.get('opposing_votes')}, "
                    f"adjustment={technical_alignment_risk.get('adjustment')}, "
                    f"cap={technical_alignment_risk.get('confidence_cap')})"
                )
            volume_confirmation = (row.get('quality_flags') or {}).get('volume_confirmation') or {}
            if volume_confirmation:
                lines.append(
                    f"- volume_confirmation: `{volume_confirmation.get('level')}` "
                    f"(primary={volume_confirmation.get('primary_trend')}, "
                    f"ratio={volume_confirmation.get('primary_ratio')}, "
                    f"expanding={volume_confirmation.get('expanding_count')}, "
                    f"contracting={volume_confirmation.get('contracting_count')}, "
                    f"cap={volume_confirmation.get('confidence_cap')})"
                )
            price_position_risk = (row.get('quality_flags') or {}).get('price_position_risk') or {}
            if price_position_risk:
                lines.append(
                    f"- price_position_risk: `{price_position_risk.get('level')}` "
                    f"(side={price_position_risk.get('side')}, "
                    f"votes={price_position_risk.get('overextended_votes')}, "
                    f"cap={price_position_risk.get('confidence_cap')})"
                )
            source_confirmation = (row.get('quality_flags') or {}).get('source_confirmation') or {}
            if source_confirmation:
                lines.append(
                    f"- source_confirmation: `{source_confirmation.get('level')}` "
                    f"(supporting={', '.join(source_confirmation.get('supporting_sources') or [])}, "
                    f"opposing={', '.join(source_confirmation.get('opposing_sources') or [])}, "
                    f"cap={source_confirmation.get('confidence_cap')})"
                )
            volatility_risk = (row.get('quality_flags') or {}).get('volatility_risk') or {}
            if volatility_risk.get('applied'):
                lines.append(
                    f"- volatility_risk: `{volatility_risk.get('level')}` "
                    f"(primary_atr={volatility_risk.get('primary_atr_pct')}, "
                    f"max_atr={volatility_risk.get('max_atr_pct')}, "
                    f"adjustment={volatility_risk.get('adjustment')}, "
                    f"cap={volatility_risk.get('confidence_cap')})"
                )
            futures_crowding_risk = (row.get('quality_flags') or {}).get('futures_crowding_risk') or {}
            if futures_crowding_risk.get('applied'):
                lines.append(
                    f"- futures_crowding_risk: `{futures_crowding_risk.get('level')}` "
                    f"({futures_crowding_risk.get('direction_side')}, "
                    f"funding={futures_crowding_risk.get('funding_signal')}, "
                    f"oi={futures_crowding_risk.get('oi_signal')}, "
                    f"ls={futures_crowding_risk.get('long_short_signal')}, "
                    f"adjustment={futures_crowding_risk.get('adjustment')}, "
                    f"cap={futures_crowding_risk.get('confidence_cap')})"
                )
            narrative_quality = (row.get('quality_flags') or {}).get('narrative_quality') or {}
            if narrative_quality:
                lines.append(
                    f"- narrative_quality: `{narrative_quality.get('quality_label')}` "
                    f"(source_weight={narrative_quality.get('source_weight')}, "
                    f"freshness_weight={narrative_quality.get('freshness_weight')}, "
                    f"cooldown_weight={narrative_quality.get('cooldown_weight')}, "
                    f"direction_weight={narrative_quality.get('direction_weight')}, "
                    f"sources={', '.join(narrative_quality.get('sources') or [])})"
                )
            direction_conflict = (row.get('quality_flags') or {}).get('direction_conflict') or {}
            if direction_conflict:
                lines.append(
                    f"- direction_conflict: `{direction_conflict.get('severity')}` "
                    f"(bull={direction_conflict.get('positive_points')}, "
                    f"bear={direction_conflict.get('negative_points_abs')}, "
                    f"penalty={direction_conflict.get('score_penalty_abs')})"
                )
            market_regime_row = (row.get('quality_flags') or {}).get('market_regime') or {}
            if market_regime_row.get('applied'):
                lines.append(
                    f"- market_regime: `{market_regime_row.get('relationship')}` "
                    f"(bias={market_regime_row.get('bias')}, "
                    f"strength={market_regime_row.get('strength')}, "
                    f"adjustment={market_regime_row.get('adjustment')}, "
                    f"cap={market_regime_row.get('confidence_cap')})"
                )
            local_edge = (row.get('quality_flags') or {}).get('local_research_edge') or {}
            if local_edge.get('applied'):
                lines.append(
                    f"- local_research_edge: `{local_edge.get('edge')}` "
                    f"(adjustment={local_edge.get('adjustment')}, "
                    f"cap={local_edge.get('confidence_cap')}, "
                    f"samples={local_edge.get('usable_evaluations')})"
                )
            for item in (row.get('evidence') or [])[:4]:
                lines.append(f"- evidence: {item}")
            lines.append('')

    source_quality = ((payload.get('narrative_monitor') or {}).get('source_quality') or {}).get('sources') or []
    if source_quality:
        lines.append('## Source Quality')
        lines.append('')
        for source in source_quality[:6]:
            lines.append(
                f"- `{source.get('source_key')}` weight `{source.get('effective_weight')}` | "
                f"posts `{source.get('post_count')}` | status `{source.get('accuracy_status')}`"
            )
        lines.append('')

    meme = payload.get('meme_early_radar') or {}
    meme_security = ((meme.get('source_stats') or {}).get('security_check') or {})
    meme_memory = ((meme.get('source_stats') or {}).get('token_memory') or {})
    meme_research_edge = ((meme.get('source_stats') or {}).get('research_edge') or {})
    meme_rows = (meme.get('early_watch') or []) + (meme.get('momentum_watch') or []) + (meme.get('rug_risk') or [])
    if meme_rows:
        lines.append('## Meme Early Radar')
        lines.append('')
        if meme_memory:
            lines.append(
                f"- token_memory: new **{meme_memory.get('new_tokens', 0)}** / "
                f"repeat **{meme_memory.get('repeat_tokens', 0)}** / "
                f"accelerated **{meme_memory.get('accelerated_repeat_tokens', 0)}** / "
                f"fading **{meme_memory.get('fading_repeat_tokens', 0)}**"
            )
        if meme_security:
            lines.append(
                f"- security_check: checked **{meme_security.get('checked', 0)}** / "
                f"risk **{meme_security.get('risk_tokens', 0)}** / "
                f"unavailable **{meme_security.get('unavailable', 0)}** / "
                f"errors **{meme_security.get('errors', 0)}**"
            )
        if meme_research_edge:
            lines.append(
                f"- research_edge: adjusted **{meme_research_edge.get('adjusted_tokens', 0)}** / "
                f"weak **{meme_research_edge.get('weak_edges', 0)}** / "
                f"strong **{meme_research_edge.get('strong_edges', 0)}** / "
                f"samples **{meme_research_edge.get('usable_return_samples', 0)}**"
            )
        if meme_security or meme_memory or meme_research_edge:
            lines.append('')
        for row in meme_rows[:12]:
            lines.append(f"### {row.get('rank')}. {row.get('symbol') or '?'} - {row.get('stage')}")
            lines.append(
                f"- score: `{row.get('early_score')}` | confidence: `{row.get('confidence')}` | "
                f"chain: `{row.get('chain_id')}` | dex: `{row.get('dex_id')}`"
            )
            lines.append(
                f"- discovery: `{row.get('discovery_status')}` | "
                f"memory_weight: `{row.get('memory_weight')}` | "
                f"seen_before: `{row.get('seen_count_before')}`"
            )
            discovery_source_validation = row.get('discovery_source_validation') or {}
            if discovery_source_validation:
                lines.append(
                    f"- discovery_source: `{discovery_source_validation.get('status')}` | "
                    f"groups `{', '.join(discovery_source_validation.get('discovery_groups') or [])}` | "
                    f"sources `{', '.join(discovery_source_validation.get('source_flags') or [])}`"
                )
            local_edge = row.get('local_research_edge') or {}
            if local_edge.get('applied'):
                lines.append(
                    f"- local_research_edge: `{local_edge.get('edge')}` "
                    f"(adjustment={local_edge.get('adjustment')}, "
                    f"cap={local_edge.get('confidence_cap')}, "
                    f"samples={local_edge.get('usable_return_samples')})"
                )
                active_edges = local_edge.get('active_edges') or []
                if active_edges:
                    first_edge = active_edges[0]
                    lines.append(
                        f"- edge_liquidity: retention `{first_edge.get('avg_liquidity_retention_pct')}` | "
                        f"drain `{first_edge.get('liquidity_drain_rate_pct')}` | "
                        f"severe_drain `{first_edge.get('severe_liquidity_drain_rate_pct')}`"
                    )
            memory_delta = row.get('memory_delta') or {}
            if memory_delta:
                lines.append(
                    f"- memory_delta: liq `{memory_delta.get('liquidity_change_since_seen_pct')}%` | "
                    f"vol_1h `{memory_delta.get('volume_h1_change_since_seen_pct')}%` | "
                    f"txns_1h `{memory_delta.get('txns_h1_change_since_seen_pct')}%` | "
                    f"flags `{', '.join(memory_delta.get('flags') or [])}`"
                )
            lines.append(
                f"- age: `{row.get('pair_age_hours')}`h | liq: `${row.get('liquidity_usd')}` | "
                f"txns_1h: `{row.get('txns_h1')}` | buy/sell: `{row.get('buy_sell_ratio_h1')}`"
            )
            social_validation = row.get('social_validation') or {}
            if social_validation:
                lines.append(
                    f"- social_validation: `{social_validation.get('status')}` | "
                    f"links `{social_validation.get('link_count')}` | "
                    f"platforms `{', '.join(social_validation.get('platforms') or [])}`"
                )
            valuation_liquidity = row.get('valuation_liquidity') or {}
            if valuation_liquidity:
                lines.append(
                    f"- valuation_liquidity: `{valuation_liquidity.get('status')}` | "
                    f"fdv/liq `{valuation_liquidity.get('fdv_to_liquidity')}` | "
                    f"vol/liq_1h `{valuation_liquidity.get('volume_liquidity_h1')}`"
                )
            microstructure = row.get('microstructure') or {}
            if microstructure:
                lines.append(
                    f"- microstructure: `{microstructure.get('status')}` | "
                    f"vol/liq_1h `{microstructure.get('volume_liquidity_h1')}` | "
                    f"m5_share `{microstructure.get('m5_volume_share_h1')}`"
                )
            pair_landscape = row.get('pair_landscape') or {}
            if pair_landscape:
                lines.append(
                    f"- pair_landscape: `{pair_landscape.get('status')}` | "
                    f"pairs `{pair_landscape.get('active_pair_count')}` | "
                    f"liq_share `{pair_landscape.get('selected_liquidity_share')}`"
                )
            boost_traction = row.get('boost_traction') or {}
            if boost_traction and boost_traction.get('status') != 'not_boosted':
                lines.append(
                    f"- boost_traction: `{boost_traction.get('status')}` | "
                    f"boost `{boost_traction.get('boost_signal')}` | "
                    f"organic_checks `{boost_traction.get('organic_check_passed')}`"
                )
            if row.get('risk_flags'):
                lines.append(f"- risk_flags: `{', '.join(row.get('risk_flags') or [])}`")
            lines.append(f"- security: `{row.get('security_note')}`")
            if row.get('pair_url'):
                lines.append(f"- link: <{row.get('pair_url')}>")
            lines.append('')

    # ── 公告 & 催化 ──
    ann = payload.get('announcement_monitor') or {}
    if ann.get('announcements'):
        lines.append('## Announcements & Catalysts')
        lines.append('')
        IMPACT_EMOJI = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}
        for a in ann['announcements'][:8]:
            emoji = IMPACT_EMOJI.get(a.get('impact'), '⚪')
            sym_str = ', '.join(a.get('symbols') or [])
            header = f"### {emoji} {a.get('title', '')}"
            if sym_str:
                header += f" ({sym_str})"
            lines.append(header)
            lines.append(f"- type: `{a.get('type')}` | impact: `{a.get('impact')}`")
            if a.get('web_link'):
                lines.append(f"- link: {a['web_link']}")
            lines.append('')

    # ── 主流币合约资金面 ──
    futures = payload.get('major_coin_futures_monitor') or {}
    if futures.get('results'):
        lines.append('## Major Coin Futures')
        lines.append('')
        for r in futures['results']:
            if 'error' in r:
                lines.append(f"### {r['symbol']} ❌ {r['error']}")
                continue
            comp = r.get('composite', {})
            score = comp.get('capital_flow_score', 50)
            bias = comp.get('capital_flow_bias', 'neutral')
            emoji = '🟢' if bias == 'bullish' else ('🔴' if bias == 'bearish' else '⚪')
            f = r.get('funding', {})
            oi = r.get('oi_5m', {})
            lines.append(f"### {emoji} {r['symbol']} — Score {score}/100 ({bias})")
            lines.append(f"- funding: {f.get('funding_rate_pct')} (z={f.get('z_score')}) → `{f.get('signal')}`")
            lines.append(f"- oi: {oi.get('oi')} | 1h change: {oi.get('oi_change_pct_1h')}% → `{oi.get('signal')}`")
            if comp.get('key_drivers'):
                lines.append(f"- drivers: {'; '.join(comp['key_drivers'])}")
            if comp.get('risk_factors'):
                lines.append(f"- risks: {'; '.join(comp['risk_factors'])}")
            lines.append(f"- suggestion: {comp.get('action_suggestion')}")
            lines.append('')

    technical = payload.get('technical_monitor') or {}
    if technical.get('watchlist'):
        lines.append('## Technical Signals')
        lines.append('')
        for row in technical.get('watchlist') or []:
            pair = row.get('symbol_pair') or row.get('symbol')
            if row.get('error'):
                lines.append(f"### {pair} - error")
                lines.append(f"- {row.get('error')}")
                lines.append('')
                continue
            indicators = row.get('indicators') or {}
            signals = row.get('signals') or []
            lines.append(f"### {pair} - {row.get('direction_watch')}")
            lines.append(
                f"- bias: `{row.get('bias')}` | "
                f"bullish_pts: `{row.get('bullish_pts')}` | "
                f"bearish_pts: `{row.get('bearish_pts')}`"
            )
            multi_tf = row.get('multi_timeframe') or {}
            if multi_tf:
                lines.append(
                    f"- multi_timeframe: `{multi_tf.get('composite_bias')}` | "
                    f"alignment: `{multi_tf.get('alignment')}` | "
                    f"score: `{multi_tf.get('weighted_score')}` | "
                    f"timeframes: `{', '.join(multi_tf.get('valid_timeframes') or [])}` | "
                    f"market: `{row.get('kline_market')}`"
                )
            lines.append(
                f"- RSI: `{indicators.get('rsi_1h')}` | "
                f"price_vs_ma25_pct: `{indicators.get('price_vs_ma25_pct')}` | "
                f"MACD_histogram: `{indicators.get('macd_histogram')}` | "
                f"ATR_pct: `{indicators.get('atr_pct')}`"
            )
            if signals:
                lines.append(f"- signals: {'; '.join(signals[:4])}")
            lines.append('')

    # ── 宏观 & 新闻 ──
    macro = payload.get('macro_calendar_monitor') or {}
    catalyst = macro.get('catalyst', {})
    if macro.get('upcoming_events') or macro.get('recent_news'):
        lines.append('## Macro & News')
        lines.append('')
        lines.append(f"🎯 Catalyst score: {catalyst.get('news_catalyst_score')} ({catalyst.get('news_catalyst_direction')})")
        lines.append('')
        for e in (macro.get('upcoming_events') or [])[:5]:
            emoji = '🔴' if e.get('impact_level') == 'high' else '🟡'
            lines.append(f"- {emoji} {e['event_name']} ({e['days_until']}天后)")
        for n in (macro.get('recent_news') or [])[:5]:
            s = '🟢' if n.get('sentiment') == 'positive' else ('🔴' if n.get('sentiment') == 'negative' else '⚪')
            lines.append(f"- {s} [{n.get('source')}] {n.get('title', '')[:60]}")
        lines.append('')

    lines.append('## Strong capital buy candidates')
    lines.append('')
    for row in payload.get('focus_board', {}).get('strong_capital_buy_candidates') or []:
        lines.append(f"### {row.get('priority_rank')}. {row.get('symbol')}")
        lines.append(f"- final_score: `{row.get('final_score')}`")
        lines.append(f"- stage: `{row.get('stage')}`")
        lines.append(f"- reason: {row.get('reason_summary')}")
        lines.append('')

    lines.append('## Top narratives')
    lines.append('')
    for row in payload.get('focus_board', {}).get('top_narratives') or []:
        lines.append(f"### {row.get('narrative_rank')}. {row.get('narrative')}")
        lines.append(f"- stage: `{row.get('dominant_stage')}`")
        lines.append(f"- sentiment: `{row.get('dominant_sentiment')}`")
        lines.append(f"- top_symbols: `{', '.join(row.get('top_symbols') or [])}`")
        lines.append('')

    lines.append('## On-chain strong buy / observation')
    lines.append('')
    for row in payload.get('focus_board', {}).get('onchain_strong_buy_candidates') or []:
        lines.append(f"### {row.get('rank')}. {row.get('symbol')} [强买盘候选]")
        lines.append(f"- buy_score: `{row.get('onchain_buy_score')}`")
        lines.append(f"- reason: {row.get('reason_summary')}")
        lines.append('')
    for row in payload.get('focus_board', {}).get('onchain_observation_candidates') or []:
        lines.append(f"### {row.get('rank')}. {row.get('symbol')} [观察池]")
        lines.append(f"- buy_score: `{row.get('onchain_buy_score')}`")
        lines.append(f"- reason: {row.get('reason_summary')}")
        lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


def save_payload(payload: Dict[str, Any], output: str | None) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'monitoring_hub_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    path.with_suffix('.md').write_text(render_markdown(payload), encoding='utf-8')
    return str(path)


def parse_window_list(value: str) -> list[int]:
    windows = [int(item.strip()) for item in str(value or '').split(',') if item.strip()]
    if not windows:
        raise ValueError('evaluation windows cannot be empty')
    return windows


def main() -> None:
    parser = argparse.ArgumentParser(description='统一监控总入口')
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--max-pages', type=int, default=4)
    parser.add_argument('--top-n', type=int, default=8)
    parser.add_argument('--onchain-limit', type=int, default=15)
    parser.add_argument('--futures-symbols', nargs='+', default=None)
    parser.add_argument('--technical-limit', type=int, default=12)
    parser.add_argument('--lookback-hours', type=int, default=72)
    parser.add_argument('--event-cooldown-hours', type=int, default=DEFAULT_EVENT_COOLDOWN_HOURS)
    parser.add_argument('--event-memory-db', default=str(DEFAULT_EVENT_MEMORY_DB))
    parser.add_argument('--meme-limit', type=int, default=30)
    parser.add_argument('--meme-max-age-hours', type=int, default=72)
    parser.add_argument('--meme-min-liquidity-usd', type=float, default=10_000)
    parser.add_argument('--meme-token-cooldown-hours', type=int, default=DEFAULT_MEME_TOKEN_COOLDOWN_HOURS)
    parser.add_argument('--meme-token-memory-db', default=str(DEFAULT_MEME_TOKEN_MEMORY_DB))
    parser.add_argument('--disable-meme-security-check', action='store_true')
    parser.add_argument('--goplus-api-token', default=None)
    parser.add_argument('--meme-extra-candidates', action='append', default=[])
    parser.add_argument('--record-meme-signals', action='store_true')
    parser.add_argument('--meme-research-db', default=str(DEFAULT_MEME_RESEARCH_DB))
    parser.add_argument('--evaluate-meme-signals', action='store_true')
    parser.add_argument('--meme-evaluation-windows', default='1,4,24')
    parser.add_argument('--meme-evaluation-max-signals', type=int, default=50)
    parser.add_argument('--force-meme-evaluation', action='store_true')
    parser.add_argument('--meme-research-report', action='store_true')
    parser.add_argument('--meme-research-min-samples', type=int, default=1)
    parser.add_argument('--meme-research-top-n', type=int, default=20)
    parser.add_argument('--disable-meme-research-edge', action='store_true')
    parser.add_argument('--meme-edge-min-samples', type=int, default=8)
    parser.add_argument('--disable-meme-early', action='store_true')
    parser.add_argument('--direction-only', action='store_true')
    parser.add_argument('--include-web-search', action='store_true')
    parser.add_argument('--record-direction-signals', action='store_true')
    parser.add_argument('--direction-db', default=str(DIRECTION_DB_PATH))
    parser.add_argument('--disable-direction-edge', action='store_true')
    parser.add_argument('--direction-edge-min-samples', type=int, default=8)
    parser.add_argument('--evaluate-direction-signals', action='store_true')
    parser.add_argument('--evaluation-interval', default='1h')
    parser.add_argument('--evaluation-windows', default='1,4,12,24')
    parser.add_argument('--evaluation-max-signals', type=int, default=50)
    parser.add_argument('--neutral-threshold-pct', type=float, default=0.5)
    parser.add_argument('--force-evaluation', action='store_true')
    parser.add_argument('--direction-research-report', action='store_true')
    parser.add_argument('--research-min-samples', type=int, default=1)
    parser.add_argument('--research-top-n', type=int, default=20)
    parser.add_argument('--output')
    args = parser.parse_args()

    if args.direction_research_report:
        report = build_direction_research_report(
            db_path=Path(args.direction_db),
            min_samples=args.research_min_samples,
            top_n=args.research_top_n,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if args.meme_research_report:
        report = summarize_meme_research(
            db_path=Path(args.meme_research_db),
            min_samples=args.meme_research_min_samples,
            top_n=args.meme_research_top_n,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if args.evaluate_direction_signals:
        result = evaluate_direction_signals(
            db_path=Path(args.direction_db),
            interval=args.evaluation_interval,
            windows=parse_window_list(args.evaluation_windows),
            max_signals=args.evaluation_max_signals,
            neutral_threshold_pct=args.neutral_threshold_pct,
            force=args.force_evaluation,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.evaluate_meme_signals:
        result = evaluate_meme_signals(
            db_path=Path(args.meme_research_db),
            windows=parse_hour_windows(args.meme_evaluation_windows),
            max_signals=args.meme_evaluation_max_signals,
            force=args.force_meme_evaluation,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.direction_only:
        payload = build_direction_only_payload(
            limit=args.limit,
            start_page=args.start_page,
            max_pages=args.max_pages,
            top_n=args.top_n,
            technical_limit=args.technical_limit,
            lookback_hours=args.lookback_hours,
            include_web_search=args.include_web_search,
            event_cooldown_hours=args.event_cooldown_hours,
            event_memory_db=Path(args.event_memory_db),
            direction_edge_db=Path(args.direction_db),
            direction_edge_min_samples=args.direction_edge_min_samples,
            enable_direction_edge=not args.disable_direction_edge,
        )
    else:
        meme_extra_candidates: list[Dict[str, Any]] = []
        meme_extra_candidate_stats: Dict[str, Any] = {}
        if args.meme_extra_candidates:
            meme_extra_candidates, meme_extra_candidate_stats = load_meme_extra_candidate_files(args.meme_extra_candidates)
        payload = build_hub_payload(
            limit=args.limit,
            start_page=args.start_page,
            max_pages=args.max_pages,
            top_n=args.top_n,
            onchain_limit=args.onchain_limit,
            futures_symbols=args.futures_symbols,
            technical_limit=args.technical_limit,
            lookback_hours=args.lookback_hours,
            event_cooldown_hours=args.event_cooldown_hours,
            event_memory_db=Path(args.event_memory_db),
            meme_limit=args.meme_limit,
            meme_max_age_hours=args.meme_max_age_hours,
            meme_min_liquidity_usd=args.meme_min_liquidity_usd,
            meme_token_cooldown_hours=args.meme_token_cooldown_hours,
            meme_token_memory_db=Path(args.meme_token_memory_db),
            meme_security_check=not args.disable_meme_security_check,
            goplus_api_token=args.goplus_api_token,
            meme_extra_candidates=meme_extra_candidates,
            meme_research_edge_db=Path(args.meme_research_db),
            meme_edge_min_samples=args.meme_edge_min_samples,
            enable_meme_research_edge=not args.disable_meme_research_edge,
            include_meme_early=not args.disable_meme_early,
            direction_edge_db=Path(args.direction_db),
            direction_edge_min_samples=args.direction_edge_min_samples,
            enable_direction_edge=not args.disable_direction_edge,
        )
        if meme_extra_candidate_stats and payload.get('meme_early_radar'):
            payload['meme_early_radar'].setdefault('source_stats', {})['extra_candidate_files'] = meme_extra_candidate_stats
    if args.record_direction_signals:
        payload['direction_research'] = {
            'recording': record_direction_signals(payload, db_path=Path(args.direction_db)),
        }
    if args.record_meme_signals and payload.get('meme_early_radar'):
        payload['meme_research'] = {
            'recording': record_meme_signals(payload['meme_early_radar'], db_path=Path(args.meme_research_db)),
        }
    out = save_payload(payload, args.output)
    print(out)
    print(json.dumps({
        'system_summary': payload['system_summary'],
        'direction_research': payload.get('direction_research'),
        'meme_research': payload.get('meme_research'),
        'market_direction_board': payload['focus_board']['market_direction_board'][:5],
        'meme_early_watch': payload['focus_board'].get('meme_early_watch', [])[:5],
        'meme_momentum_watch': payload['focus_board'].get('meme_momentum_watch', [])[:5],
        'strong_capital_buy_candidates': payload['focus_board']['strong_capital_buy_candidates'][:5],
        'top_narratives': payload['focus_board']['top_narratives'][:5],
        'technical_watchlist': payload['focus_board']['technical_watchlist'][:5],
        'onchain_observation_candidates': payload['focus_board']['onchain_observation_candidates'][:5],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
