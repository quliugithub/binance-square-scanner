#!/usr/bin/env python3
import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from bsc.constants import RESULTS_DIR
from bsc.data.strategy import (
    DEFAULT_INTERVAL,
    DEFAULT_WINDOWS,
    aggregate_coin_signals,
    backtest_posts,
    parse_publish_time,
    score_post,
)
from bsc.data.tool import scrape_square

DB_PATH = RESULTS_DIR / 'binance_square_research.sqlite3'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS posts (
    content_id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    publish_time_raw TEXT,
    publish_time_iso TEXT,
    author TEXT,
    username TEXT,
    body TEXT,
    trading_pairs_json TEXT,
    tags_json TEXT,
    engagement_total INTEGER,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    view_count INTEGER,
    tone TEXT,
    tone_confidence REAL,
    signal_score REAL,
    signal_label TEXT,
    symbol_candidates_json TEXT,
    raw_post_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL,
    content_id TEXT NOT NULL,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    view_count INTEGER,
    engagement_total INTEGER,
    signal_score REAL,
    signal_label TEXT,
    tone TEXT,
    FOREIGN KEY(content_id) REFERENCES posts(content_id)
);

CREATE TABLE IF NOT EXISTS backtest_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    window_label TEXT NOT NULL,
    tone TEXT,
    signal_score REAL,
    entry_price REAL,
    forward_return_pct REAL,
    max_favorable_pct REAL,
    max_adverse_pct REAL,
    backtested_at TEXT NOT NULL,
    UNIQUE(content_id, symbol, interval, window_label),
    FOREIGN KEY(content_id) REFERENCES posts(content_id)
);
'''


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def connect_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def load_json_field(value: Any) -> Any:
    if value in (None, ''):
        return []
    return json.loads(value)


def floor_time_to_bucket(dt: datetime, bucket_minutes: int) -> datetime:
    dt = dt.astimezone(UTC)
    floored_minute = (dt.minute // bucket_minutes) * bucket_minutes
    return dt.replace(minute=floored_minute, second=0, microsecond=0)


def upsert_post(conn: sqlite3.Connection, post: Dict[str, Any], seen_at: str) -> None:
    publish_dt = parse_publish_time(post.get('publish_time'))
    publish_iso = publish_dt.isoformat().replace('+00:00', 'Z') if publish_dt else None
    conn.execute(
        '''
        INSERT INTO posts (
            content_id, first_seen_at, last_seen_at, publish_time_raw, publish_time_iso,
            author, username, body, trading_pairs_json, tags_json,
            engagement_total, like_count, comment_count, share_count, view_count,
            tone, tone_confidence, signal_score, signal_label, symbol_candidates_json,
            raw_post_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(content_id) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            publish_time_raw=excluded.publish_time_raw,
            publish_time_iso=COALESCE(excluded.publish_time_iso, posts.publish_time_iso),
            author=excluded.author,
            username=excluded.username,
            body=excluded.body,
            trading_pairs_json=excluded.trading_pairs_json,
            tags_json=excluded.tags_json,
            engagement_total=excluded.engagement_total,
            like_count=excluded.like_count,
            comment_count=excluded.comment_count,
            share_count=excluded.share_count,
            view_count=excluded.view_count,
            tone=excluded.tone,
            tone_confidence=excluded.tone_confidence,
            signal_score=excluded.signal_score,
            signal_label=excluded.signal_label,
            symbol_candidates_json=excluded.symbol_candidates_json,
            raw_post_json=excluded.raw_post_json
        ''',
        (
            str(post.get('content_id')),
            seen_at,
            seen_at,
            str(post.get('publish_time') or ''),
            publish_iso,
            post.get('author'),
            post.get('username'),
            post.get('body'),
            json_dumps(post.get('trading_pairs') or []),
            json_dumps(post.get('tags') or []),
            int(post.get('engagement_total') or 0),
            int(post.get('like_count') or 0),
            int(post.get('comment_count') or 0),
            int(post.get('share_count') or 0),
            int(post.get('view_count') or 0),
            post.get('tone'),
            float(post.get('tone_confidence') or 0),
            float(post.get('signal_score') or 0),
            post.get('signal_label'),
            json_dumps(post.get('symbol_candidates') or []),
            json_dumps(post),
        ),
    )


def insert_snapshot(conn: sqlite3.Connection, post: Dict[str, Any], seen_at: str) -> None:
    conn.execute(
        '''
        INSERT INTO snapshots (
            snapshot_at, content_id, like_count, comment_count, share_count,
            view_count, engagement_total, signal_score, signal_label, tone
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            seen_at,
            str(post.get('content_id')),
            int(post.get('like_count') or 0),
            int(post.get('comment_count') or 0),
            int(post.get('share_count') or 0),
            int(post.get('view_count') or 0),
            int(post.get('engagement_total') or 0),
            float(post.get('signal_score') or 0),
            post.get('signal_label'),
            post.get('tone'),
        ),
    )


def collect_posts(limit: int, start_page: int, max_pages: int) -> List[Dict[str, Any]]:
    raw_posts = scrape_square(limit=limit, start_page=start_page, max_pages=max_pages)
    return [score_post(post) for post in raw_posts if not post.get('error')]


def ingest_sample(conn: sqlite3.Connection, posts: List[Dict[str, Any]], snapshot_at: Optional[str] = None) -> Dict[str, Any]:
    seen_at = snapshot_at or utc_now_iso()
    inserted = 0
    for post in posts:
        upsert_post(conn, post, seen_at)
        insert_snapshot(conn, post, seen_at)
        inserted += 1
    conn.commit()
    return {
        'snapshot_at': seen_at,
        'post_count': inserted,
        'unique_content_ids': len({str(p.get('content_id')) for p in posts}),
    }


def load_scored_posts_from_db(conn: sqlite3.Connection, min_signal_score: float = 0.0) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT raw_post_json FROM posts WHERE signal_score >= ? ORDER BY last_seen_at DESC',
        (min_signal_score,),
    ).fetchall()
    return [json.loads(row['raw_post_json']) for row in rows]


def summarize_author_stats(conn: sqlite3.Connection, min_posts: int = 1) -> List[Dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT
            COALESCE(username, author, 'unknown') AS author_key,
            COUNT(*) AS post_count,
            AVG(signal_score) AS avg_signal_score,
            AVG(engagement_total) AS avg_engagement_total,
            SUM(CASE WHEN signal_label = 'high_signal' THEN 1 ELSE 0 END) AS high_signal_posts
        FROM posts
        GROUP BY author_key
        HAVING COUNT(*) >= ?
        ORDER BY avg_signal_score DESC, post_count DESC
        ''',
        (min_posts,),
    ).fetchall()
    return [dict(row) for row in rows]


def summarize_author_edge(conn: sqlite3.Connection, min_posts: int = 2) -> List[Dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT
            COALESCE(p.username, p.author, 'unknown') AS author_key,
            COUNT(DISTINCT b.content_id) AS backtested_posts,
            AVG(b.forward_return_pct) AS avg_return_pct,
            AVG(b.max_favorable_pct) AS avg_mfe_pct,
            AVG(b.max_adverse_pct) AS avg_mae_pct,
            AVG(CASE
                WHEN p.tone = 'bullish' AND b.forward_return_pct > 0 THEN 1.0
                WHEN p.tone = 'bearish' AND b.forward_return_pct < 0 THEN 1.0
                ELSE 0.0
            END) * 100 AS directional_win_rate_pct,
            AVG(p.signal_score) AS avg_signal_score
        FROM backtest_events b
        JOIN posts p ON p.content_id = b.content_id
        GROUP BY author_key
        HAVING COUNT(DISTINCT b.content_id) >= ?
        ORDER BY directional_win_rate_pct DESC, avg_return_pct DESC, backtested_posts DESC
        ''',
        (min_posts,),
    ).fetchall()
    return [
        {
            **dict(row),
            'avg_return_pct': round(row['avg_return_pct'] or 0.0, 4),
            'avg_mfe_pct': round(row['avg_mfe_pct'] or 0.0, 4),
            'avg_mae_pct': round(row['avg_mae_pct'] or 0.0, 4),
            'directional_win_rate_pct': round(row['directional_win_rate_pct'] or 0.0, 2),
            'avg_signal_score': round(row['avg_signal_score'] or 0.0, 2),
        }
        for row in rows
    ]


def summarize_coin_history(conn: sqlite3.Connection, min_posts: int = 1) -> List[Dict[str, Any]]:
    rows = conn.execute('SELECT raw_post_json FROM posts ORDER BY last_seen_at DESC').fetchall()
    posts = [json.loads(row['raw_post_json']) for row in rows]
    coin_signals = aggregate_coin_signals(posts)
    return [row for row in coin_signals if row['post_count'] >= min_posts]


def build_time_bucket_signals(
    conn: sqlite3.Connection,
    bucket_minutes: int = 5,
    lookback_hours: int = 24,
    min_posts: int = 2,
) -> List[Dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
    rows = conn.execute(
        '''
        SELECT publish_time_iso, last_seen_at, username, author, tone, signal_score,
               engagement_total, symbol_candidates_json, content_id
        FROM posts
        ORDER BY COALESCE(publish_time_iso, last_seen_at) DESC
        '''
    ).fetchall()

    buckets: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        time_text = row['publish_time_iso'] or row['last_seen_at']
        if not time_text:
            continue
        dt = datetime.fromisoformat(str(time_text).replace('Z', '+00:00')).astimezone(UTC)
        if dt < cutoff:
            continue
        bucket_time = floor_time_to_bucket(dt, bucket_minutes)
        symbols = load_json_field(row['symbol_candidates_json'])
        for symbol in symbols:
            buckets[(bucket_time.isoformat().replace('+00:00', 'Z'), symbol)].append({
                'author_key': row['username'] or row['author'] or 'unknown',
                'tone': row['tone'],
                'signal_score': row['signal_score'] or 0.0,
                'engagement_total': row['engagement_total'] or 0,
                'content_id': row['content_id'],
            })

    results: List[Dict[str, Any]] = []
    for (bucket_time, symbol), items in buckets.items():
        if len(items) < min_posts:
            continue
        bullish = sum(1 for item in items if item['tone'] == 'bullish')
        bearish = sum(1 for item in items if item['tone'] == 'bearish')
        unique_authors = len({item['author_key'] for item in items})
        avg_signal_score = sum(float(item['signal_score']) for item in items) / len(items)
        total_engagement = sum(int(item['engagement_total']) for item in items)
        signal_velocity = len(items) / max(1, bucket_minutes)
        conviction = min(
            100.0,
            avg_signal_score * 0.5
            + unique_authors * 8
            + math.log1p(total_engagement) * 7
            + signal_velocity * 10,
        )
        if bullish > bearish:
            bias = 'long_watch'
        elif bearish > bullish:
            bias = 'short_watch'
        else:
            bias = 'no_trade'
        if conviction < 50:
            bias = 'no_trade'
        results.append({
            'bucket_time': bucket_time,
            'symbol': symbol,
            'post_count': len(items),
            'unique_authors': unique_authors,
            'bullish_posts': bullish,
            'bearish_posts': bearish,
            'avg_signal_score': round(avg_signal_score, 2),
            'total_engagement': total_engagement,
            'signal_velocity': round(signal_velocity, 4),
            'bucket_conviction': round(conviction, 2),
            'bias': bias,
            'content_ids': [item['content_id'] for item in items[:10]],
        })
    results.sort(key=lambda x: (x['bucket_conviction'], x['post_count'], x['unique_authors']), reverse=True)
    return results


def run_backtest_and_store(
    conn: sqlite3.Connection,
    posts: List[Dict[str, Any]],
    interval: str,
    windows: List[int],
) -> Dict[str, Any]:
    result = backtest_posts(posts, interval=interval, windows=windows)
    backtested_at = utc_now_iso()
    for item in result['studied_posts']:
        if item.get('error'):
            continue
        for window_label, forward_return in (item.get('forward_return_pct') or {}).items():
            conn.execute(
                '''
                INSERT INTO backtest_events (
                    content_id, symbol, interval, window_label, tone, signal_score,
                    entry_price, forward_return_pct, max_favorable_pct, max_adverse_pct, backtested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_id, symbol, interval, window_label) DO UPDATE SET
                    tone=excluded.tone,
                    signal_score=excluded.signal_score,
                    entry_price=excluded.entry_price,
                    forward_return_pct=excluded.forward_return_pct,
                    max_favorable_pct=excluded.max_favorable_pct,
                    max_adverse_pct=excluded.max_adverse_pct,
                    backtested_at=excluded.backtested_at
                ''',
                (
                    str(item.get('content_id')),
                    item.get('symbol'),
                    interval,
                    window_label,
                    item.get('tone'),
                    float(item.get('signal_score') or 0),
                    item.get('entry_price'),
                    forward_return,
                    (item.get('max_favorable_pct') or {}).get(window_label),
                    (item.get('max_adverse_pct') or {}).get(window_label),
                    backtested_at,
                ),
            )
    conn.commit()
    return result


def summarize_backtest_history(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute(
        '''
        SELECT tone, window_label,
               COUNT(*) AS sample_count,
               AVG(forward_return_pct) AS avg_return_pct,
               AVG(max_favorable_pct) AS avg_mfe_pct,
               AVG(max_adverse_pct) AS avg_mae_pct,
               AVG(CASE WHEN forward_return_pct > 0 THEN 1.0 ELSE 0.0 END) * 100 AS win_rate_pct
        FROM backtest_events
        GROUP BY tone, window_label
        ORDER BY tone, window_label
        '''
    ).fetchall()
    return {
        f"{row['tone']}::{row['window_label']}": {
            'sample_count': row['sample_count'],
            'avg_return_pct': round(row['avg_return_pct'] or 0.0, 4),
            'avg_mfe_pct': round(row['avg_mfe_pct'] or 0.0, 4),
            'avg_mae_pct': round(row['avg_mae_pct'] or 0.0, 4),
            'win_rate_pct': round(row['win_rate_pct'] or 0.0, 2),
        }
        for row in rows
    }


def save_report(payload: Dict[str, Any], prefix: str, output: Optional[str] = None) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'{prefix}_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(path)


def cmd_ingest(args: argparse.Namespace) -> None:
    conn = connect_db(Path(args.db))
    posts = collect_posts(limit=args.limit, start_page=args.start_page, max_pages=args.max_pages)
    summary = ingest_sample(conn, posts)
    report = {
        'generated_at': utc_now_iso(),
        'db_path': str(Path(args.db)),
        'ingest': summary,
        'top_coin_signals': aggregate_coin_signals(posts)[:10],
        'sample_posts': posts[:5],
    }
    out = save_report(report, 'binance_square_research_ingest', args.output)
    print(out)
    print(json.dumps(report['ingest'], ensure_ascii=False, indent=2))


def cmd_report(args: argparse.Namespace) -> None:
    conn = connect_db(Path(args.db))
    posts = load_scored_posts_from_db(conn, min_signal_score=args.min_signal_score)
    report = {
        'generated_at': utc_now_iso(),
        'db_path': str(Path(args.db)),
        'stored_post_count': len(posts),
        'coin_history': summarize_coin_history(conn, min_posts=args.min_posts)[:20],
        'author_history': summarize_author_stats(conn, min_posts=args.min_posts)[:20],
        'author_edge': summarize_author_edge(conn, min_posts=args.min_posts)[:20],
        'time_bucket_signals': build_time_bucket_signals(
            conn,
            bucket_minutes=args.bucket_minutes,
            lookback_hours=args.lookback_hours,
            min_posts=args.bucket_min_posts,
        )[:20],
        'backtest_history': summarize_backtest_history(conn),
    }
    out = save_report(report, 'binance_square_research_report', args.output)
    print(out)
    print(json.dumps({
        'stored_post_count': report['stored_post_count'],
        'top_coin_history': report['coin_history'][:5],
        'top_author_edge': report['author_edge'][:5],
        'top_time_bucket_signals': report['time_bucket_signals'][:5],
    }, ensure_ascii=False, indent=2))


def cmd_backfill(args: argparse.Namespace) -> None:
    conn = connect_db(Path(args.db))
    posts = load_scored_posts_from_db(conn, min_signal_score=args.min_signal_score)
    windows = [int(x) for x in args.windows.split(',') if x.strip()]
    result = run_backtest_and_store(conn, posts, interval=args.interval, windows=windows)
    report = {
        'generated_at': utc_now_iso(),
        'db_path': str(Path(args.db)),
        'input_post_count': len(posts),
        'result': result,
        'history_summary': summarize_backtest_history(conn),
    }
    out = save_report(report, 'binance_square_research_backfill', args.output)
    print(out)
    print(json.dumps(report['history_summary'], ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Binance Square 可持续样本研究框架')
    parser.add_argument('--db', default=str(DB_PATH), help='SQLite 数据库路径')
    sub = parser.add_subparsers(dest='command', required=True)

    p_ingest = sub.add_parser('ingest', help='抓取最新帖子，评分后写入研究库，并记录快照')
    p_ingest.add_argument('--limit', type=int, default=50)
    p_ingest.add_argument('--start-page', type=int, default=1)
    p_ingest.add_argument('--max-pages', type=int, default=5)
    p_ingest.add_argument('--output')

    p_report = sub.add_parser('report', help='输出累计样本研究报告')
    p_report.add_argument('--min-posts', type=int, default=1)
    p_report.add_argument('--min-signal-score', type=float, default=0.0)
    p_report.add_argument('--bucket-minutes', type=int, default=5)
    p_report.add_argument('--lookback-hours', type=int, default=24)
    p_report.add_argument('--bucket-min-posts', type=int, default=2)
    p_report.add_argument('--output')

    p_backfill = sub.add_parser('backfill', help='对库中样本批量补回测，并保存历史统计')
    p_backfill.add_argument('--interval', default=DEFAULT_INTERVAL)
    p_backfill.add_argument('--windows', default=','.join(str(x) for x in DEFAULT_WINDOWS))
    p_backfill.add_argument('--min-signal-score', type=float, default=55.0)
    p_backfill.add_argument('--output')

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == 'ingest':
        cmd_ingest(args)
    elif args.command == 'report':
        cmd_report(args)
    elif args.command == 'backfill':
        cmd_backfill(args)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
