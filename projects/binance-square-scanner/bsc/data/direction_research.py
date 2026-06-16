#!/usr/bin/env python3
"""Persistence and outcome evaluation for market direction board signals."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from bsc.constants import RESULTS_DIR
from bsc.data.strategy import price_event_study


DB_PATH = RESULTS_DIR / 'direction_signals.sqlite3'
NEUTRAL_ATR_MULTIPLIER = 0.6
NEUTRAL_MIN_THRESHOLD_PCT = 0.3
NEUTRAL_MAX_THRESHOLD_PCT = 5.0
CATALYST_TEXT_RULES = [
    ('macro:etf', ('etf', 'blackrock', 'fidelity', 'grayscale', 'inflow', 'outflow')),
    ('macro:regulation', ('sec', 'cftc', 'regulation', 'regulatory', 'lawsuit', 'approval', 'ban')),
    ('macro:macro', ('fed', 'fomc', 'cpi', 'inflation', 'interest rate', 'powell', 'dxy', 'treasury')),
    ('macro:upgrade', ('upgrade', 'mainnet', 'hard fork', 'testnet', 'eip')),
    ('macro:black_swan', ('hack', 'exploit', 'collapse', 'bankruptcy', 'liquidation', 'rug', 'scam')),
]

SCHEMA = '''
CREATE TABLE IF NOT EXISTS direction_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction_watch TEXT NOT NULL,
    direction_score REAL,
    confidence TEXT,
    evidence_sources_json TEXT,
    score_breakdown_json TEXT,
    quality_flags_json TEXT,
    evidence_json TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_direction_signals_generated_at
ON direction_signals(generated_at);

CREATE INDEX IF NOT EXISTS idx_direction_signals_symbol
ON direction_signals(symbol);

CREATE TABLE IF NOT EXISTS direction_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction_watch TEXT NOT NULL,
    interval TEXT NOT NULL,
    window_label TEXT NOT NULL,
    entry_price REAL,
    forward_return_pct REAL,
    max_favorable_pct REAL,
    max_adverse_pct REAL,
    directional_return_pct REAL,
    direction_hit INTEGER,
    neutral_threshold_pct REAL,
    evaluated_at TEXT NOT NULL,
    error TEXT,
    UNIQUE(signal_id, interval, window_label),
    FOREIGN KEY(signal_id) REFERENCES direction_signals(id)
);
'''


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def json_loads(value: Any, fallback: Any) -> Any:
    if value in (None, ''):
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def parse_iso_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(UTC)
    except Exception:
        return None


def interval_to_timedelta(interval: str) -> timedelta:
    match = re.fullmatch(r'(\d+)([mhd])', interval.strip().lower())
    if not match:
        raise ValueError(f'unsupported interval: {interval}')
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == 'm':
        return timedelta(minutes=amount)
    if unit == 'h':
        return timedelta(hours=amount)
    return timedelta(days=amount)


def parse_window_label(window_label: str) -> int:
    match = re.match(r'(\d+)_', str(window_label or ''))
    if not match:
        return 1
    return max(1, int(match.group(1)))


def normalize_symbol_pair(symbol: Any) -> str:
    text = str(symbol or '').strip().upper().replace('/', '').replace(':USDT', '')
    if not text:
        return ''
    if not text.endswith('USDT'):
        text += 'USDT'
    return text


def connect_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def direction_run_id(payload: Dict[str, Any]) -> str:
    generated_at = str(payload.get('generated_at') or utc_now_iso())
    mode = ((payload.get('scan_config') or {}).get('mode') or 'hub')
    cleaned = re.sub(r'[^0-9A-Za-z_]+', '_', generated_at).strip('_')
    return f'{mode}_{cleaned}'


def direction_rows_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    board = payload.get('market_direction_board') or {}
    rows = board.get('rows') or []
    if rows:
        return rows
    return ((payload.get('focus_board') or {}).get('market_direction_board') or [])


def record_direction_signals(payload: Dict[str, Any], db_path: Path = DB_PATH) -> Dict[str, Any]:
    rows = direction_rows_from_payload(payload)
    run_id = direction_run_id(payload)
    generated_at = str(payload.get('generated_at') or utc_now_iso())
    created_at = utc_now_iso()
    inserted = 0

    conn = connect_db(db_path)
    try:
        for row in rows:
            symbol = normalize_symbol_pair(row.get('symbol'))
            direction = str(row.get('direction_watch') or '')
            if not symbol or not direction:
                continue
            conn.execute(
                '''
                INSERT INTO direction_signals (
                    run_id, generated_at, symbol, direction_watch, direction_score, confidence,
                    evidence_sources_json, score_breakdown_json, quality_flags_json, evidence_json,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, symbol) DO UPDATE SET
                    direction_watch=excluded.direction_watch,
                    direction_score=excluded.direction_score,
                    confidence=excluded.confidence,
                    evidence_sources_json=excluded.evidence_sources_json,
                    score_breakdown_json=excluded.score_breakdown_json,
                    quality_flags_json=excluded.quality_flags_json,
                    evidence_json=excluded.evidence_json,
                    payload_json=excluded.payload_json
                ''',
                (
                    run_id,
                    generated_at,
                    symbol,
                    direction,
                    float(row.get('direction_score') or 0.0),
                    row.get('confidence'),
                    json_dumps(row.get('evidence_sources') or []),
                    json_dumps(row.get('score_breakdown') or {}),
                    json_dumps(row.get('quality_flags') or {}),
                    json_dumps(row.get('evidence') or []),
                    json_dumps(row),
                    created_at,
                ),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()

    return {
        'db_path': str(db_path),
        'run_id': run_id,
        'generated_at': generated_at,
        'signals_recorded': inserted,
    }


def direction_hit(direction_watch: str, forward_return_pct: Optional[float], neutral_threshold_pct: float) -> Optional[int]:
    if forward_return_pct is None:
        return None
    if direction_watch == 'rise_watch':
        return 1 if forward_return_pct > 0 else 0
    if direction_watch == 'fall_watch':
        return 1 if forward_return_pct < 0 else 0
    if direction_watch == 'neutral_watch':
        return 1 if abs(forward_return_pct) <= neutral_threshold_pct else 0
    return None


def directional_return(direction_watch: str, forward_return_pct: Optional[float], neutral_threshold_pct: float) -> Optional[float]:
    if forward_return_pct is None:
        return None
    if direction_watch == 'rise_watch':
        return forward_return_pct
    if direction_watch == 'fall_watch':
        return -forward_return_pct
    if direction_watch == 'neutral_watch':
        return neutral_threshold_pct - abs(forward_return_pct)
    return None


def safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ''):
            return None
        return float(value)
    except Exception:
        return None


def signal_quality_flags(signal_row: sqlite3.Row) -> Dict[str, Any]:
    return json_loads(signal_row['quality_flags_json'], {})


def signal_atr_pct(signal_row: sqlite3.Row) -> Optional[float]:
    technical_quality = signal_quality_flags(signal_row).get('technical_quality') or {}
    primary = safe_float(technical_quality.get('primary_atr_pct'))
    if primary is not None and primary > 0:
        return primary

    timeframe_atr = technical_quality.get('timeframe_atr_pct') or {}
    for key in ('1h', '4h', '15m'):
        value = safe_float(timeframe_atr.get(key))
        if value is not None and value > 0:
            return value
    return None


def dynamic_neutral_threshold_pct(
    signal_row: sqlite3.Row,
    window_label: str,
    fallback_threshold_pct: float,
) -> float:
    atr_pct = signal_atr_pct(signal_row)
    if atr_pct is None:
        return fallback_threshold_pct
    window_count = parse_window_label(window_label)
    threshold = atr_pct * NEUTRAL_ATR_MULTIPLIER * math.sqrt(window_count)
    threshold = max(NEUTRAL_MIN_THRESHOLD_PCT, min(NEUTRAL_MAX_THRESHOLD_PCT, threshold))
    return round(threshold, 4)


def existing_evaluation(conn: sqlite3.Connection, signal_id: int, interval: str, window_label: str) -> bool:
    row = conn.execute(
        '''
        SELECT 1 FROM direction_evaluations
        WHERE signal_id = ? AND interval = ? AND window_label = ?
        ''',
        (signal_id, interval, window_label),
    ).fetchone()
    return row is not None


def due_windows_for_signal(
    signal_row: sqlite3.Row,
    interval: str,
    windows: Iterable[int],
    now: datetime,
    force: bool,
    conn: sqlite3.Connection,
) -> List[int]:
    generated_at = parse_iso_time(signal_row['generated_at'])
    if generated_at is None:
        return []
    interval_delta = interval_to_timedelta(interval)
    due: List[int] = []
    for window in windows:
        label = f'{window}_{interval}'
        if not force and existing_evaluation(conn, int(signal_row['id']), interval, label):
            continue
        if force or now >= generated_at + interval_delta * window:
            due.append(window)
    return due


def insert_evaluation(
    conn: sqlite3.Connection,
    signal_row: sqlite3.Row,
    interval: str,
    window_label: str,
    entry_price: Any,
    forward_return_pct: Any,
    max_favorable_pct: Any,
    max_adverse_pct: Any,
    neutral_threshold_pct: float,
    error: Optional[str] = None,
) -> None:
    direction = signal_row['direction_watch']
    d_return = directional_return(direction, forward_return_pct, neutral_threshold_pct)
    hit = direction_hit(direction, forward_return_pct, neutral_threshold_pct)
    conn.execute(
        '''
        INSERT INTO direction_evaluations (
            signal_id, symbol, direction_watch, interval, window_label, entry_price,
            forward_return_pct, max_favorable_pct, max_adverse_pct, directional_return_pct,
            direction_hit, neutral_threshold_pct, evaluated_at, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_id, interval, window_label) DO UPDATE SET
            entry_price=excluded.entry_price,
            forward_return_pct=excluded.forward_return_pct,
            max_favorable_pct=excluded.max_favorable_pct,
            max_adverse_pct=excluded.max_adverse_pct,
            directional_return_pct=excluded.directional_return_pct,
            direction_hit=excluded.direction_hit,
            neutral_threshold_pct=excluded.neutral_threshold_pct,
            evaluated_at=excluded.evaluated_at,
            error=excluded.error
        ''',
        (
            int(signal_row['id']),
            signal_row['symbol'],
            direction,
            interval,
            window_label,
            entry_price,
            forward_return_pct,
            max_favorable_pct,
            max_adverse_pct,
            d_return,
            hit,
            neutral_threshold_pct,
            utc_now_iso(),
            error,
        ),
    )


def summarize_direction_accuracy(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT
            s.direction_watch,
            e.interval,
            e.window_label,
            COUNT(*) AS sample_count,
            AVG(e.direction_hit) * 100 AS hit_rate_pct,
            AVG(e.forward_return_pct) AS avg_forward_return_pct,
            AVG(e.directional_return_pct) AS avg_directional_return_pct
        FROM direction_evaluations e
        JOIN direction_signals s ON s.id = e.signal_id
        WHERE e.error IS NULL AND e.direction_hit IS NOT NULL
        GROUP BY s.direction_watch, e.interval, e.window_label
        ORDER BY e.interval, e.window_label, s.direction_watch
        '''
    ).fetchall()
    return [
        {
            'direction_watch': row['direction_watch'],
            'interval': row['interval'],
            'window_label': row['window_label'],
            'sample_count': int(row['sample_count'] or 0),
            'hit_rate_pct': round(float(row['hit_rate_pct'] or 0.0), 2),
            'avg_forward_return_pct': round(float(row['avg_forward_return_pct'] or 0.0), 4),
            'avg_directional_return_pct': round(float(row['avg_directional_return_pct'] or 0.0), 4),
        }
        for row in rows
    ]


def fetch_evaluation_records(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT
            e.*,
            s.generated_at,
            s.direction_score,
            s.confidence,
            s.evidence_sources_json,
            s.score_breakdown_json,
            s.quality_flags_json,
            s.evidence_json
        FROM direction_evaluations e
        JOIN direction_signals s ON s.id = e.signal_id
        WHERE e.error IS NULL AND e.direction_hit IS NOT NULL
        '''
    ).fetchall()
    records: List[Dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        record['evidence_sources'] = json_loads(row['evidence_sources_json'], [])
        record['score_breakdown'] = json_loads(row['score_breakdown_json'], {})
        record['quality_flags'] = json_loads(row['quality_flags_json'], {})
        record['evidence'] = json_loads(row['evidence_json'], [])
        records.append(record)
    return records


def summarize_record_group(records: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    count = len(records)
    hit_count = sum(int(row.get('direction_hit') or 0) for row in records)
    avg_forward = sum(float(row.get('forward_return_pct') or 0.0) for row in records) / count
    avg_directional = sum(float(row.get('directional_return_pct') or 0.0) for row in records) / count
    avg_score = sum(float(row.get('direction_score') or 0.0) for row in records) / count
    avg_neutral_threshold = sum(float(row.get('neutral_threshold_pct') or 0.0) for row in records) / count
    return {
        'label': label,
        'sample_count': count,
        'hit_count': hit_count,
        'hit_rate_pct': round(hit_count / count * 100, 2),
        'avg_forward_return_pct': round(avg_forward, 4),
        'avg_directional_return_pct': round(avg_directional, 4),
        'avg_direction_score': round(avg_score, 2),
        'avg_neutral_threshold_pct': round(avg_neutral_threshold, 4),
    }


def summarize_by(
    records: List[Dict[str, Any]],
    key_name: str,
    key_fn: Callable[[Dict[str, Any]], Iterable[str]],
    min_samples: int,
    top_n: int,
) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        for key in key_fn(record):
            if key:
                grouped[str(key)].append(record)

    summaries = []
    for key, items in grouped.items():
        if len(items) < min_samples:
            continue
        summary = summarize_record_group(items, key)
        summary[key_name] = summary.pop('label')
        summaries.append(summary)

    summaries.sort(
        key=lambda row: (
            row['sample_count'],
            row['hit_rate_pct'],
            row['avg_directional_return_pct'],
        ),
        reverse=True,
    )
    return summaries[:top_n]


def single_key(field: str, default: str = 'unknown') -> Callable[[Dict[str, Any]], Iterable[str]]:
    def _inner(record: Dict[str, Any]) -> Iterable[str]:
        return [str(record.get(field) or default)]
    return _inner


def window_key(record: Dict[str, Any]) -> Iterable[str]:
    return [f"{record.get('interval')}::{record.get('window_label')}"]


def evidence_mix_key(record: Dict[str, Any]) -> Iterable[str]:
    sources = sorted(str(item) for item in (record.get('evidence_sources') or []))
    return ['+'.join(sources) if sources else 'none']


def evidence_source_keys(record: Dict[str, Any]) -> Iterable[str]:
    return [str(item) for item in (record.get('evidence_sources') or [])] or ['none']


def narrative_source_keys(record: Dict[str, Any]) -> Iterable[str]:
    quality = (record.get('quality_flags') or {}).get('narrative_quality') or {}
    return [str(item) for item in (quality.get('sources') or [])] or ['none']


def narrative_quality_key(record: Dict[str, Any]) -> Iterable[str]:
    quality = (record.get('quality_flags') or {}).get('narrative_quality') or {}
    return [str(quality.get('quality_label') or 'none')]


def direction_conflict_key(record: Dict[str, Any]) -> Iterable[str]:
    conflict = (record.get('quality_flags') or {}).get('direction_conflict') or {}
    if not conflict:
        return ['none']
    return [str(conflict.get('severity') or 'unknown')]


def volatility_risk_key(record: Dict[str, Any]) -> Iterable[str]:
    risk = (record.get('quality_flags') or {}).get('volatility_risk') or {}
    return [str(risk.get('level') or 'unknown')]


def volatility_risk_applied_key(record: Dict[str, Any]) -> Iterable[str]:
    risk = (record.get('quality_flags') or {}).get('volatility_risk') or {}
    return ['applied' if risk.get('applied') else 'not_applied']


def technical_alignment_risk_key(record: Dict[str, Any]) -> Iterable[str]:
    risk = (record.get('quality_flags') or {}).get('technical_alignment_risk') or {}
    return [str(risk.get('level') or 'unknown')]


def technical_alignment_risk_applied_key(record: Dict[str, Any]) -> Iterable[str]:
    risk = (record.get('quality_flags') or {}).get('technical_alignment_risk') or {}
    return ['applied' if risk.get('applied') else 'not_applied']


def volume_confirmation_key(record: Dict[str, Any]) -> Iterable[str]:
    confirmation = (record.get('quality_flags') or {}).get('volume_confirmation') or {}
    return [str(confirmation.get('level') or 'unknown')]


def volume_confirmation_applied_key(record: Dict[str, Any]) -> Iterable[str]:
    confirmation = (record.get('quality_flags') or {}).get('volume_confirmation') or {}
    return ['applied' if confirmation.get('applied') else 'not_applied']


def price_position_risk_key(record: Dict[str, Any]) -> Iterable[str]:
    risk = (record.get('quality_flags') or {}).get('price_position_risk') or {}
    return [str(risk.get('level') or 'unknown')]


def price_position_risk_applied_key(record: Dict[str, Any]) -> Iterable[str]:
    risk = (record.get('quality_flags') or {}).get('price_position_risk') or {}
    return ['applied' if risk.get('applied') else 'not_applied']


def price_position_side_key(record: Dict[str, Any]) -> Iterable[str]:
    risk = (record.get('quality_flags') or {}).get('price_position_risk') or {}
    return [str(risk.get('side') or 'none')]


def source_confirmation_key(record: Dict[str, Any]) -> Iterable[str]:
    confirmation = (record.get('quality_flags') or {}).get('source_confirmation') or {}
    return [str(confirmation.get('level') or 'unknown')]


def source_confirmation_applied_key(record: Dict[str, Any]) -> Iterable[str]:
    confirmation = (record.get('quality_flags') or {}).get('source_confirmation') or {}
    return ['applied' if confirmation.get('applied') else 'not_applied']


def technical_alignment_key(record: Dict[str, Any]) -> Iterable[str]:
    quality = (record.get('quality_flags') or {}).get('technical_quality') or {}
    return [str(quality.get('alignment') or 'unknown')]


def futures_crowding_risk_key(record: Dict[str, Any]) -> Iterable[str]:
    risk = (record.get('quality_flags') or {}).get('futures_crowding_risk') or {}
    return [str(risk.get('level') or 'unknown')]


def futures_crowding_risk_applied_key(record: Dict[str, Any]) -> Iterable[str]:
    risk = (record.get('quality_flags') or {}).get('futures_crowding_risk') or {}
    return ['applied' if risk.get('applied') else 'not_applied']


def futures_crowding_side_key(record: Dict[str, Any]) -> Iterable[str]:
    risk = (record.get('quality_flags') or {}).get('futures_crowding_risk') or {}
    return [str(risk.get('direction_side') or 'none')]


def market_regime_quality(record: Dict[str, Any]) -> Dict[str, Any]:
    return (record.get('quality_flags') or {}).get('market_regime') or {}


def market_regime_key(record: Dict[str, Any]) -> Iterable[str]:
    regime = market_regime_quality(record)
    context = regime.get('context') or {}
    if not context:
        return ['none']
    return [str(context.get('regime') or 'unknown')]


def market_regime_relationship_key(record: Dict[str, Any]) -> Iterable[str]:
    regime = market_regime_quality(record)
    return [str(regime.get('relationship') or 'none')]


def market_regime_adjustment_key(record: Dict[str, Any]) -> Iterable[str]:
    regime = market_regime_quality(record)
    if not regime.get('applied'):
        return ['not_applied']
    return [str(regime.get('relationship') or 'applied')]


def market_regime_confidence_cap_key(record: Dict[str, Any]) -> Iterable[str]:
    regime = market_regime_quality(record)
    cap = regime.get('confidence_cap')
    return [str(cap or 'none')]


def market_regime_anchor_conflict_key(record: Dict[str, Any]) -> Iterable[str]:
    regime = market_regime_quality(record)
    context = regime.get('context') or {}
    if not context:
        return ['unknown']
    return ['anchor_conflict' if context.get('anchor_conflict') else 'anchor_aligned_or_neutral']


def append_unique(values: List[str], item: str) -> None:
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


def catalyst_types_from_record(record: Dict[str, Any]) -> List[str]:
    quality_flags = record.get('quality_flags') or {}
    explicit = quality_flags.get('catalyst_types') or []
    catalyst_types: List[str] = []
    for item in explicit:
        append_unique(catalyst_types, str(item))
    if catalyst_types:
        return catalyst_types

    evidence_sources = set(str(item) for item in (record.get('evidence_sources') or []))
    score_breakdown = record.get('score_breakdown') or {}
    evidence_text = ' '.join(str(item) for item in (record.get('evidence') or [])).lower()
    if 'technical' in evidence_sources or 'technical' in score_breakdown:
        append_unique(catalyst_types, 'technical_signal')
    if 'futures_capital_flow' in evidence_sources or 'futures_capital_flow' in score_breakdown:
        append_unique(catalyst_types, 'futures_flow')
    if 'narrative_news' in evidence_sources or 'narrative_news' in score_breakdown:
        append_unique(catalyst_types, 'news:narrative')
    if 'announcements' in evidence_sources or 'announcements' in score_breakdown:
        found = False
        for match in re.findall(r'([a-z_]+):(?:high|medium|low)', evidence_text):
            append_unique(catalyst_types, f'announcement:{match}')
            found = True
        if not found:
            append_unique(catalyst_types, 'announcement:general')
        if ':high' in evidence_text:
            append_unique(catalyst_types, 'announcement:high_impact')
    if 'macro_news' in evidence_sources or 'macro_news' in score_breakdown:
        append_unique(catalyst_types, 'macro:general')
        for label, keywords in CATALYST_TEXT_RULES:
            if any(keyword in evidence_text for keyword in keywords):
                append_unique(catalyst_types, label)
    return catalyst_types or ['none']


def catalyst_type_keys(record: Dict[str, Any]) -> Iterable[str]:
    return catalyst_types_from_record(record)


def catalyst_family_keys(record: Dict[str, Any]) -> Iterable[str]:
    families: List[str] = []
    for catalyst_type in catalyst_types_from_record(record):
        append_unique(families, catalyst_family_from_type(catalyst_type))
    return families or ['none']


def direction_prefixed_key(record: Dict[str, Any], key: str) -> str:
    return f"{record.get('direction_watch') or 'unknown'}::{key or 'none'}"


def direction_evidence_mix_key(record: Dict[str, Any]) -> Iterable[str]:
    return [direction_prefixed_key(record, next(iter(evidence_mix_key(record)), 'none'))]


def direction_catalyst_type_keys(record: Dict[str, Any]) -> Iterable[str]:
    return [
        direction_prefixed_key(record, catalyst_type)
        for catalyst_type in catalyst_types_from_record(record)
    ] or [direction_prefixed_key(record, 'none')]


def direction_catalyst_family_keys(record: Dict[str, Any]) -> Iterable[str]:
    return [
        direction_prefixed_key(record, family)
        for family in catalyst_family_keys(record)
    ] or [direction_prefixed_key(record, 'none')]


def top_and_weak_symbols(records: List[Dict[str, Any]], min_samples: int, top_n: int) -> Dict[str, List[Dict[str, Any]]]:
    symbol_rows = summarize_by(records, 'symbol', single_key('symbol'), min_samples, 1000)
    top = sorted(
        symbol_rows,
        key=lambda row: (row['hit_rate_pct'], row['sample_count'], row['avg_directional_return_pct']),
        reverse=True,
    )[:top_n]
    weak = sorted(
        symbol_rows,
        key=lambda row: (row['hit_rate_pct'], -row['sample_count'], row['avg_directional_return_pct']),
    )[:top_n]
    return {'top_symbols': top, 'weak_symbols': weak}


def build_direction_research_report(
    db_path: Path = DB_PATH,
    min_samples: int = 1,
    top_n: int = 20,
) -> Dict[str, Any]:
    if not db_path.exists():
        return {
            'generated_at': utc_now_iso(),
            'db_path': str(db_path),
            'summary': {
                'signals_recorded': 0,
                'evaluations_recorded': 0,
                'evaluated_signals': 0,
                'pending_signals': 0,
            },
            'overall': None,
            'by_window': [],
            'by_direction': [],
            'by_confidence': [],
            'by_evidence_mix': [],
            'by_evidence_source': [],
            'by_catalyst_type': [],
            'by_catalyst_family': [],
            'by_direction_evidence_mix': [],
            'by_direction_catalyst_type': [],
            'by_direction_catalyst_family': [],
            'by_narrative_source': [],
            'by_narrative_quality': [],
            'by_direction_conflict': [],
            'by_technical_alignment': [],
            'by_technical_alignment_risk': [],
            'by_technical_alignment_risk_applied': [],
            'by_volume_confirmation': [],
            'by_volume_confirmation_applied': [],
            'by_price_position_risk': [],
            'by_price_position_risk_applied': [],
            'by_price_position_side': [],
            'by_source_confirmation': [],
            'by_source_confirmation_applied': [],
            'by_volatility_risk': [],
            'by_volatility_risk_applied': [],
            'by_futures_crowding_risk': [],
            'by_futures_crowding_risk_applied': [],
            'by_futures_crowding_side': [],
            'by_market_regime': [],
            'by_market_regime_relationship': [],
            'by_market_regime_adjustment': [],
            'by_market_regime_confidence_cap': [],
            'by_market_regime_anchor_conflict': [],
            'top_symbols': [],
            'weak_symbols': [],
        }

    conn = connect_db(db_path)
    try:
        signals_recorded = int(conn.execute('SELECT COUNT(*) FROM direction_signals').fetchone()[0] or 0)
        evaluations_recorded = int(conn.execute('SELECT COUNT(*) FROM direction_evaluations').fetchone()[0] or 0)
        evaluated_signals = int(conn.execute('SELECT COUNT(DISTINCT signal_id) FROM direction_evaluations WHERE error IS NULL').fetchone()[0] or 0)
        records = fetch_evaluation_records(conn)
    finally:
        conn.close()

    symbol_summary = top_and_weak_symbols(records, min_samples, top_n)
    return {
        'generated_at': utc_now_iso(),
        'db_path': str(db_path),
        'summary': {
            'signals_recorded': signals_recorded,
            'evaluations_recorded': evaluations_recorded,
            'evaluated_signals': evaluated_signals,
            'pending_signals': max(0, signals_recorded - evaluated_signals),
            'usable_evaluations': len(records),
            'min_samples': min_samples,
        },
        'overall': summarize_record_group(records, 'overall') if records else None,
        'by_window': summarize_by(records, 'window', window_key, min_samples, top_n),
        'by_direction': summarize_by(records, 'direction_watch', single_key('direction_watch'), min_samples, top_n),
        'by_confidence': summarize_by(records, 'confidence', single_key('confidence'), min_samples, top_n),
        'by_evidence_mix': summarize_by(records, 'evidence_mix', evidence_mix_key, min_samples, top_n),
        'by_evidence_source': summarize_by(records, 'evidence_source', evidence_source_keys, min_samples, top_n),
        'by_catalyst_type': summarize_by(records, 'catalyst_type', catalyst_type_keys, min_samples, top_n),
        'by_catalyst_family': summarize_by(records, 'catalyst_family', catalyst_family_keys, min_samples, top_n),
        'by_direction_evidence_mix': summarize_by(records, 'direction_evidence_mix', direction_evidence_mix_key, min_samples, top_n),
        'by_direction_catalyst_type': summarize_by(records, 'direction_catalyst_type', direction_catalyst_type_keys, min_samples, top_n),
        'by_direction_catalyst_family': summarize_by(records, 'direction_catalyst_family', direction_catalyst_family_keys, min_samples, top_n),
        'by_narrative_source': summarize_by(records, 'narrative_source', narrative_source_keys, min_samples, top_n),
        'by_narrative_quality': summarize_by(records, 'narrative_quality', narrative_quality_key, min_samples, top_n),
        'by_direction_conflict': summarize_by(records, 'direction_conflict', direction_conflict_key, min_samples, top_n),
        'by_technical_alignment': summarize_by(records, 'technical_alignment', technical_alignment_key, min_samples, top_n),
        'by_technical_alignment_risk': summarize_by(records, 'technical_alignment_risk', technical_alignment_risk_key, min_samples, top_n),
        'by_technical_alignment_risk_applied': summarize_by(records, 'technical_alignment_risk_applied', technical_alignment_risk_applied_key, min_samples, top_n),
        'by_volume_confirmation': summarize_by(records, 'volume_confirmation', volume_confirmation_key, min_samples, top_n),
        'by_volume_confirmation_applied': summarize_by(records, 'volume_confirmation_applied', volume_confirmation_applied_key, min_samples, top_n),
        'by_price_position_risk': summarize_by(records, 'price_position_risk', price_position_risk_key, min_samples, top_n),
        'by_price_position_risk_applied': summarize_by(records, 'price_position_risk_applied', price_position_risk_applied_key, min_samples, top_n),
        'by_price_position_side': summarize_by(records, 'price_position_side', price_position_side_key, min_samples, top_n),
        'by_source_confirmation': summarize_by(records, 'source_confirmation', source_confirmation_key, min_samples, top_n),
        'by_source_confirmation_applied': summarize_by(records, 'source_confirmation_applied', source_confirmation_applied_key, min_samples, top_n),
        'by_volatility_risk': summarize_by(records, 'volatility_risk', volatility_risk_key, min_samples, top_n),
        'by_volatility_risk_applied': summarize_by(records, 'volatility_risk_applied', volatility_risk_applied_key, min_samples, top_n),
        'by_futures_crowding_risk': summarize_by(records, 'futures_crowding_risk', futures_crowding_risk_key, min_samples, top_n),
        'by_futures_crowding_risk_applied': summarize_by(records, 'futures_crowding_risk_applied', futures_crowding_risk_applied_key, min_samples, top_n),
        'by_futures_crowding_side': summarize_by(records, 'futures_crowding_side', futures_crowding_side_key, min_samples, top_n),
        'by_market_regime': summarize_by(records, 'market_regime', market_regime_key, min_samples, top_n),
        'by_market_regime_relationship': summarize_by(records, 'market_regime_relationship', market_regime_relationship_key, min_samples, top_n),
        'by_market_regime_adjustment': summarize_by(records, 'market_regime_adjustment', market_regime_adjustment_key, min_samples, top_n),
        'by_market_regime_confidence_cap': summarize_by(records, 'market_regime_confidence_cap', market_regime_confidence_cap_key, min_samples, top_n),
        'by_market_regime_anchor_conflict': summarize_by(records, 'market_regime_anchor_conflict', market_regime_anchor_conflict_key, min_samples, top_n),
        'top_symbols': symbol_summary['top_symbols'],
        'weak_symbols': symbol_summary['weak_symbols'],
    }


def summaries_by_key(rows: List[Dict[str, Any]], key_name: str) -> Dict[str, Dict[str, Any]]:
    return {str(row.get(key_name)): row for row in rows if row.get(key_name) is not None}


def build_direction_edge_report(
    db_path: Path = DB_PATH,
    min_samples: int = 8,
) -> Dict[str, Any]:
    min_samples = max(1, int(min_samples))
    if not db_path.exists():
        return {
            'available': False,
            'reason': 'missing_db',
            'db_path': str(db_path),
            'min_samples': min_samples,
            'usable_evaluations': 0,
            'groups': {},
        }

    conn = connect_db(db_path)
    try:
        records = fetch_evaluation_records(conn)
    finally:
        conn.close()

    if not records:
        return {
            'available': False,
            'reason': 'no_evaluations',
            'db_path': str(db_path),
            'min_samples': min_samples,
            'usable_evaluations': 0,
            'groups': {},
        }

    evidence_mix = summarize_by(records, 'evidence_mix', evidence_mix_key, min_samples, 1000)
    regime_relationship = summarize_by(
        records,
        'market_regime_relationship',
        market_regime_relationship_key,
        min_samples,
        1000,
    )
    direction_conflict = summarize_by(
        records,
        'direction_conflict',
        direction_conflict_key,
        min_samples,
        1000,
    )
    technical_alignment_risk = summarize_by(
        records,
        'technical_alignment_risk',
        technical_alignment_risk_key,
        min_samples,
        1000,
    )
    volume_confirmation = summarize_by(
        records,
        'volume_confirmation',
        volume_confirmation_key,
        min_samples,
        1000,
    )
    price_position_risk = summarize_by(
        records,
        'price_position_risk',
        price_position_risk_key,
        min_samples,
        1000,
    )
    source_confirmation = summarize_by(
        records,
        'source_confirmation',
        source_confirmation_key,
        min_samples,
        1000,
    )
    catalyst_type = summarize_by(records, 'catalyst_type', catalyst_type_keys, min_samples, 1000)
    catalyst_family = summarize_by(records, 'catalyst_family', catalyst_family_keys, min_samples, 1000)
    direction_evidence_mix = summarize_by(records, 'direction_evidence_mix', direction_evidence_mix_key, min_samples, 1000)
    direction_catalyst_type = summarize_by(records, 'direction_catalyst_type', direction_catalyst_type_keys, min_samples, 1000)
    direction_catalyst_family = summarize_by(records, 'direction_catalyst_family', direction_catalyst_family_keys, min_samples, 1000)
    groups = {
        'evidence_mix': summaries_by_key(evidence_mix, 'evidence_mix'),
        'market_regime_relationship': summaries_by_key(regime_relationship, 'market_regime_relationship'),
        'direction_conflict': summaries_by_key(direction_conflict, 'direction_conflict'),
        'technical_alignment_risk': summaries_by_key(technical_alignment_risk, 'technical_alignment_risk'),
        'volume_confirmation': summaries_by_key(volume_confirmation, 'volume_confirmation'),
        'price_position_risk': summaries_by_key(price_position_risk, 'price_position_risk'),
        'source_confirmation': summaries_by_key(source_confirmation, 'source_confirmation'),
        'catalyst_type': summaries_by_key(catalyst_type, 'catalyst_type'),
        'catalyst_family': summaries_by_key(catalyst_family, 'catalyst_family'),
        'direction_evidence_mix': summaries_by_key(direction_evidence_mix, 'direction_evidence_mix'),
        'direction_catalyst_type': summaries_by_key(direction_catalyst_type, 'direction_catalyst_type'),
        'direction_catalyst_family': summaries_by_key(direction_catalyst_family, 'direction_catalyst_family'),
    }
    return {
        'available': any(groups.values()),
        'reason': 'ok' if any(groups.values()) else 'insufficient_group_samples',
        'db_path': str(db_path),
        'min_samples': min_samples,
        'usable_evaluations': len(records),
        'groups': groups,
    }


def evaluate_direction_signals(
    db_path: Path = DB_PATH,
    interval: str = '1h',
    windows: Optional[List[int]] = None,
    max_signals: int = 50,
    neutral_threshold_pct: float = 0.5,
    force: bool = False,
) -> Dict[str, Any]:
    windows = windows or [1, 4, 12, 24]
    if not db_path.exists():
        return {
            'db_path': str(db_path),
            'evaluated_signals': 0,
            'evaluations_written': 0,
            'skipped_not_due': 0,
            'errors': [],
            'accuracy_summary': [],
        }

    now = datetime.now(UTC)
    evaluated_signals = 0
    evaluations_written = 0
    skipped_not_due = 0
    errors: List[Dict[str, Any]] = []

    conn = connect_db(db_path)
    try:
        signals = conn.execute(
            '''
            SELECT * FROM direction_signals
            ORDER BY generated_at ASC
            LIMIT ?
            ''',
            (max_signals,),
        ).fetchall()

        for signal_row in signals:
            due_windows = due_windows_for_signal(signal_row, interval, windows, now, force, conn)
            if not due_windows:
                skipped_not_due += 1
                continue

            event_time = parse_iso_time(signal_row['generated_at'])
            try:
                result = price_event_study(signal_row['symbol'], event_time, interval=interval, windows=due_windows)
                evaluated_signals += 1
                for window in due_windows:
                    label = f'{window}_{interval}'
                    signal_neutral_threshold_pct = dynamic_neutral_threshold_pct(
                        signal_row,
                        label,
                        neutral_threshold_pct,
                    )
                    insert_evaluation(
                        conn,
                        signal_row,
                        interval,
                        label,
                        result.entry_price,
                        (result.forward_return_pct or {}).get(label),
                        (result.max_favorable_pct or {}).get(label),
                        (result.max_adverse_pct or {}).get(label),
                        signal_neutral_threshold_pct,
                    )
                    evaluations_written += 1
            except Exception as exc:
                errors.append({'symbol': signal_row['symbol'], 'error': str(exc)})

        conn.commit()
        accuracy_summary = summarize_direction_accuracy(conn)
    finally:
        conn.close()

    return {
        'db_path': str(db_path),
        'interval': interval,
        'windows': windows,
        'neutral_threshold_pct': neutral_threshold_pct,
        'neutral_threshold_mode': 'atr_dynamic_with_cli_fallback',
        'neutral_atr_multiplier': NEUTRAL_ATR_MULTIPLIER,
        'neutral_min_threshold_pct': NEUTRAL_MIN_THRESHOLD_PCT,
        'neutral_max_threshold_pct': NEUTRAL_MAX_THRESHOLD_PCT,
        'evaluated_signals': evaluated_signals,
        'evaluations_written': evaluations_written,
        'skipped_not_due': skipped_not_due,
        'errors': errors[:20],
        'accuracy_summary': accuracy_summary,
    }
