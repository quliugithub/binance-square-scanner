#!/usr/bin/env python3
"""Early-stage meme token radar using DexScreener public endpoints."""

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional

import requests

from bsc.constants import RESULTS_DIR


TOKEN_PROFILES_URL = 'https://api.dexscreener.com/token-profiles/latest/v1'
TOKEN_BOOSTS_LATEST_URL = 'https://api.dexscreener.com/token-boosts/latest/v1'
TOKEN_BOOSTS_TOP_URL = 'https://api.dexscreener.com/token-boosts/top/v1'
TOKEN_PAIRS_URL = 'https://api.dexscreener.com/token-pairs/v1/{chainId}/{tokenAddress}'
GOPLUS_EVM_SECURITY_URL = 'https://api.gopluslabs.io/api/v1/token_security/{chain_id}'
GOPLUS_SOLANA_SECURITY_URL = 'https://api.gopluslabs.io/api/v1/solana/token_security'

DEFAULT_CHAINS = {'solana', 'base', 'bsc', 'ethereum'}
GOPLUS_EVM_CHAIN_IDS = {
    'ethereum': '1',
    'eth': '1',
    'bsc': '56',
    'bnb': '56',
    'base': '8453',
    'polygon': '137',
    'arbitrum': '42161',
    'avalanche': '43114',
    'optimism': '10',
    'fantom': '250',
}
STABLE_SYMBOLS = {'USDT', 'USDC', 'USD', 'DAI', 'FDUSD', 'TUSD', 'WETH', 'WBNB', 'SOL', 'WSOL'}
DEFAULT_TOKEN_MEMORY_DB = RESULTS_DIR / 'meme_token_memory.sqlite3'
DEFAULT_MEME_RESEARCH_DB = RESULTS_DIR / 'meme_research.sqlite3'
DEFAULT_TOKEN_COOLDOWN_HOURS = 24
TOKEN_REPEAT_MIN_WEIGHT = 0.55
TOKEN_REPEAT_STEP = 0.15
NULL_SECURITY_ADDRESSES = {
    '',
    '0',
    '0x',
    'none',
    'null',
    '0x0000000000000000000000000000000000000000',
    '0x000000000000000000000000000000000000dead',
}
CREATOR_MEMORY_PENALTIES = {
    'creator_rug_history': 30,
    'owner_rug_history': 24,
    'creator_risky_token_history': 16,
    'owner_risky_token_history': 12,
}
MEME_KEYWORDS = {
    'meme', 'memecoin', 'dog', 'cat', 'frog', 'pepe', 'shib', 'inu', 'bonk',
    'wif', 'pump', 'moon', 'degen', 'cto', 'community', 'fair launch',
}
TOKEN_MEMORY_SCHEMA = '''
CREATE TABLE IF NOT EXISTS meme_token_memory (
    token_key TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_count INTEGER NOT NULL,
    last_stage TEXT,
    last_score REAL,
    last_price_usd REAL,
    last_liquidity_usd REAL,
    last_volume_h1 REAL,
    last_txns_h1 INTEGER,
    last_pair_address TEXT,
    last_pair_age_hours REAL
);

CREATE INDEX IF NOT EXISTS idx_meme_token_memory_last_seen
ON meme_token_memory(last_seen_at);

CREATE TABLE IF NOT EXISTS meme_creator_memory (
    creator_key TEXT NOT NULL,
    token_key TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    party_type TEXT NOT NULL,
    address TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_count INTEGER NOT NULL,
    symbol TEXT,
    last_stage TEXT,
    last_score REAL,
    last_liquidity_usd REAL,
    last_pair_address TEXT,
    risk_flags_json TEXT NOT NULL,
    PRIMARY KEY (creator_key, token_key)
);

CREATE INDEX IF NOT EXISTS idx_meme_creator_memory_last_seen
ON meme_creator_memory(last_seen_at);

CREATE INDEX IF NOT EXISTS idx_meme_creator_memory_address
ON meme_creator_memory(chain_id, address);
'''
MEME_RESEARCH_SCHEMA = '''
CREATE TABLE IF NOT EXISTS meme_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    symbol TEXT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pair_address TEXT,
    pair_url TEXT,
    stage TEXT NOT NULL,
    early_score REAL,
    confidence TEXT,
    price_usd REAL,
    liquidity_usd REAL,
    pair_age_hours REAL,
    source_flags_json TEXT NOT NULL,
    risk_flags_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, chain_id, token_address, pair_address)
);

CREATE INDEX IF NOT EXISTS idx_meme_signals_generated_at
ON meme_signals(generated_at);

CREATE INDEX IF NOT EXISTS idx_meme_signals_token
ON meme_signals(chain_id, token_address);

CREATE TABLE IF NOT EXISTS meme_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    window_label TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    current_price_usd REAL,
    forward_return_pct REAL,
    current_liquidity_usd REAL,
    current_pair_address TEXT,
    error TEXT,
    UNIQUE(signal_id, window_label),
    FOREIGN KEY(signal_id) REFERENCES meme_signals(id)
);
'''


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def ensure_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def fetch_json(url: str) -> Any:
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_latest_profiles() -> List[Dict[str, Any]]:
    return ensure_list(fetch_json(TOKEN_PROFILES_URL))


def fetch_latest_boosts() -> List[Dict[str, Any]]:
    return ensure_list(fetch_json(TOKEN_BOOSTS_LATEST_URL))


def fetch_top_boosts() -> List[Dict[str, Any]]:
    return ensure_list(fetch_json(TOKEN_BOOSTS_TOP_URL))


def fetch_token_pairs(chain_id: str, token_address: str) -> List[Dict[str, Any]]:
    url = TOKEN_PAIRS_URL.format(chainId=chain_id, tokenAddress=token_address)
    return ensure_list(fetch_json(url))


def goplus_auth_headers(api_token: str | None = None) -> Dict[str, str]:
    token = str(api_token or os.getenv('GOPLUS_API_TOKEN') or '').strip()
    if not token:
        return {}
    return {'Authorization': f'Bearer {token}'}


def fetch_goplus_security(
    chain_id: str,
    token_address: str,
    api_token: str | None = None,
) -> Dict[str, Any] | None:
    chain = str(chain_id or '').strip().lower()
    token = str(token_address or '').strip()
    if not chain or not token:
        return None

    params = {'contract_addresses': token}
    headers = goplus_auth_headers(api_token)
    if chain == 'solana':
        resp = requests.get(GOPLUS_SOLANA_SECURITY_URL, params=params, headers=headers, timeout=20)
    else:
        goplus_chain_id = GOPLUS_EVM_CHAIN_IDS.get(chain)
        if not goplus_chain_id:
            return None
        resp = requests.get(
            GOPLUS_EVM_SECURITY_URL.format(chain_id=goplus_chain_id),
            params=params,
            headers=headers,
            timeout=20,
        )
    resp.raise_for_status()
    payload = resp.json()
    result = payload.get('result') if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None
    token_lower = token.lower()
    for key, value in result.items():
        if str(key).lower() == token_lower and isinstance(value, dict):
            return value
    if len(result) == 1:
        only_value = next(iter(result.values()))
        if isinstance(only_value, dict):
            return only_value
    return result if result else None


def ensure_columns(conn: sqlite3.Connection, table_name: str, columns: Dict[str, str]) -> None:
    existing = {
        str(row['name'])
        for row in conn.execute(f'PRAGMA table_info({table_name})').fetchall()
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE {table_name} ADD COLUMN {name} {definition}')


def ensure_token_memory_columns(conn: sqlite3.Connection) -> None:
    ensure_columns(conn, 'meme_token_memory', {
        'last_price_usd': 'REAL',
        'last_liquidity_usd': 'REAL',
        'last_volume_h1': 'REAL',
        'last_txns_h1': 'INTEGER',
        'last_pair_address': 'TEXT',
        'last_pair_age_hours': 'REAL',
    })


def security_scalar(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ('status', 'value', 'result'):
            if key in value:
                return value.get(key)
    return value


def truthy_security_value(value: Any) -> bool:
    return str(security_scalar(value)).strip().lower() in {'1', 'true', 'yes', 'y'}


def false_security_value(value: Any) -> bool:
    return str(security_scalar(value)).strip().lower() in {'0', 'false', 'no', 'n'}


def parse_security_float(value: Any) -> float:
    value = security_scalar(value)
    if isinstance(value, str) and value.endswith('%'):
        value = value[:-1]
    return safe_float(value)


def parse_security_ratio(value: Any) -> float:
    value = security_scalar(value)
    if value is None:
        return 0.0
    text = str(value).strip()
    has_percent = text.endswith('%')
    if has_percent:
        text = text[:-1]
    ratio = safe_float(text.replace(',', ''))
    if has_percent or ratio > 1:
        ratio /= 100.0
    return max(0.0, ratio)


def security_field(data: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data.get(name)
    return None


def normalize_security_address(value: Any) -> str:
    text = str(security_scalar(value) or '').strip()
    if not text:
        return ''
    normalized = text.lower()
    if normalized in NULL_SECURITY_ADDRESSES:
        return ''
    if len(normalized) < 8:
        return ''
    return normalized


def security_address_field(data: Dict[str, Any], *names: str) -> str:
    for name in names:
        if name not in data:
            continue
        address = normalize_security_address(data.get(name))
        if address:
            return address
    return ''


def security_ratio_field(data: Dict[str, Any], *names: str) -> float:
    ratios = [parse_security_ratio(data.get(name)) for name in names if name in data]
    return max(ratios) if ratios else 0.0


def security_holder_entries(security: Dict[str, Any]) -> List[Dict[str, Any]]:
    for name in ('holders', 'top_holders', 'holder_list', 'token_holders'):
        value = security_field(security, name)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for nested_name in ('holders', 'list', 'items', 'data'):
                nested = value.get(nested_name)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    return []


def holder_is_burn_or_locked(holder: Dict[str, Any]) -> bool:
    tag = str(
        holder.get('tag')
        or holder.get('label')
        or holder.get('address')
        or holder.get('holder_address')
        or holder.get('account')
        or ''
    ).lower()
    if truthy_security_value(holder.get('is_locked')) or truthy_security_value(holder.get('is_burned')):
        return True
    return any(marker in tag for marker in ('dead', 'burn', 'lock'))


def holder_percent(holder: Dict[str, Any]) -> float:
    return security_ratio_field(
        holder,
        'percent',
        'percentage',
        'holder_percent',
        'balance_percent',
        'balance_percentage',
        'rate',
        'holder_rate',
    )


def security_holder_count(security: Dict[str, Any]) -> int | None:
    count = security_field(security, 'holder_count', 'holders_count', 'holder_total', 'total_holders')
    if count is not None:
        parsed = int(safe_float(security_scalar(count)))
        return parsed if parsed > 0 else None
    holders = security_holder_entries(security)
    return len(holders) if holders else None


def security_top_holder_percent(security: Dict[str, Any]) -> float:
    percents = [
        holder_percent(holder)
        for holder in security_holder_entries(security)
        if not holder_is_burn_or_locked(holder)
    ]
    return max(percents) if percents else 0.0


def security_top10_holder_percent(security: Dict[str, Any]) -> float:
    explicit = security_ratio_field(
        security,
        'top_10_holder_rate',
        'top_10_holder_ratio',
        'top_10_holder_percent',
        'top10_holder_rate',
        'top10_holder_ratio',
        'top10_holder_percent',
        'top_ten_holder_rate',
        'top_ten_holder_percent',
    )
    if explicit:
        return explicit
    percents = sorted(
        (
            holder_percent(holder)
            for holder in security_holder_entries(security)
            if not holder_is_burn_or_locked(holder)
        ),
        reverse=True,
    )
    return min(sum(percents[:10]), 1.0) if percents else 0.0


def security_risk_flags(security: Dict[str, Any] | None) -> List[str]:
    if not security:
        return []

    flags: List[str] = []
    bool_flag_map = {
        'security_honeypot': ('is_honeypot', 'honeypot_with_same_creator'),
        'security_cannot_sell_all': ('cannot_sell_all',),
        'security_blacklist': ('is_blacklisted', 'blacklist', 'blacklist_function'),
        'security_trading_cooldown': ('trading_cooldown',),
        'security_personal_slippage': ('personal_slippage_modifiable',),
        'security_slippage_modifiable': ('slippage_modifiable',),
        'security_can_take_back_ownership': ('can_take_back_ownership',),
        'security_owner_change_balance': ('owner_change_balance',),
        'security_hidden_owner': ('hidden_owner',),
        'security_selfdestruct': ('selfdestruct', 'self_destruct'),
        'security_external_call': ('external_call',),
        'security_mintable': ('is_mintable', 'mintable'),
        'security_proxy': ('is_proxy',),
        'security_transfer_pausable': ('transfer_pausable',),
        'security_freezable': ('freezable',),
        'security_metadata_mutable': ('metadata_mutable',),
        'security_balance_mutable_authority': ('balance_mutable_authority',),
        'security_non_transferable': ('non_transferable',),
        'security_airdrop_scam': ('is_airdrop_scam',),
    }
    for flag, names in bool_flag_map.items():
        if any(truthy_security_value(security.get(name)) for name in names):
            flags.append(flag)

    if false_security_value(security_field(security, 'is_open_source')):
        flags.append('security_not_open_source')
    if false_security_value(security_field(security, 'is_in_dex')):
        flags.append('security_not_in_dex')
    if false_security_value(security_field(security, 'is_true_token')):
        flags.append('security_not_true_token')

    buy_tax = parse_security_float(security_field(security, 'buy_tax'))
    sell_tax = parse_security_float(security_field(security, 'sell_tax'))
    if buy_tax >= 0.15:
        flags.append('security_high_buy_tax')
    if sell_tax >= 0.15:
        flags.append('security_high_sell_tax')

    creator_percent = security_ratio_field(
        security,
        'creator_percent',
        'creator_percentage',
        'creator_balance_percent',
        'creator_balance_percentage',
    )
    owner_percent = security_ratio_field(
        security,
        'owner_percent',
        'owner_percentage',
        'owner_balance_percent',
        'owner_balance_percentage',
    )
    top_holder_percent = security_top_holder_percent(security)
    top10_holder_percent = security_top10_holder_percent(security)
    if creator_percent >= 0.2:
        flags.append('security_creator_high_balance')
    if owner_percent >= 0.2:
        flags.append('security_owner_high_balance')
    if top_holder_percent >= 0.2:
        flags.append('security_top_holder_concentrated')
    if top10_holder_percent >= 0.65:
        flags.append('security_top10_holder_concentrated')

    owner_address = security_address_field(security, 'owner_address', 'owner', 'ownerAddress')
    if owner_address:
        flags.append('security_owner_not_renounced')

    lp_holders = security.get('lp_holders')
    if isinstance(lp_holders, list) and lp_holders:
        locked_pct = 0.0
        burned_pct = 0.0
        for holder in lp_holders:
            if not isinstance(holder, dict):
                continue
            percent = parse_security_float(holder.get('percent'))
            tag = str(holder.get('tag') or holder.get('address') or '').lower()
            if truthy_security_value(holder.get('is_locked')) or 'lock' in tag:
                locked_pct += percent
            if truthy_security_value(holder.get('is_burned')) or 'dead' in tag or 'burn' in tag:
                burned_pct += percent
        if locked_pct + burned_pct < 0.5:
            flags.append('security_lp_not_locked')

    return flags


def security_snapshot(security: Dict[str, Any] | None) -> Dict[str, Any]:
    if not security:
        return {'provider': 'goplus', 'status': 'not_checked'}
    return {
        'provider': 'goplus',
        'status': 'checked',
        'buy_tax': security_scalar(security_field(security, 'buy_tax')),
        'sell_tax': security_scalar(security_field(security, 'sell_tax')),
        'is_honeypot': security_scalar(security_field(security, 'is_honeypot')),
        'cannot_sell_all': security_scalar(security_field(security, 'cannot_sell_all')),
        'is_blacklisted': security_scalar(security_field(security, 'is_blacklisted', 'blacklist')),
        'is_mintable': security_scalar(security_field(security, 'is_mintable', 'mintable')),
        'creator_address': security_address_field(
            security,
            'creator_address',
            'creatorAddress',
            'deployer_address',
            'deployerAddress',
            'contract_creator',
            'contractCreator',
            'creator_addr',
        ),
        'owner_address': security_address_field(security, 'owner_address', 'owner', 'ownerAddress'),
        'is_open_source': security_scalar(security_field(security, 'is_open_source')),
        'is_proxy': security_scalar(security_field(security, 'is_proxy')),
        'creator_balance_pct': round(security_ratio_field(
            security,
            'creator_percent',
            'creator_percentage',
            'creator_balance_percent',
            'creator_balance_percentage',
        ) * 100.0, 4),
        'owner_balance_pct': round(security_ratio_field(
            security,
            'owner_percent',
            'owner_percentage',
            'owner_balance_percent',
            'owner_balance_percentage',
        ) * 100.0, 4),
        'top_holder_pct': round(security_top_holder_percent(security) * 100.0, 4),
        'top10_holder_pct': round(security_top10_holder_percent(security) * 100.0, 4),
        'holder_count': security_holder_count(security),
        'lp_holder_count': len(security.get('lp_holders') or []) if isinstance(security.get('lp_holders'), list) else None,
    }


def apply_security_to_row(row: Dict[str, Any], security: Dict[str, Any] | None) -> None:
    flags = security_risk_flags(security)
    risk_flags = row.setdefault('risk_flags', [])
    for flag in flags:
        if flag not in risk_flags:
            risk_flags.append(flag)
    row['security'] = security_snapshot(security)
    if flags:
        row['security_note'] = 'contract_security_risk:' + ','.join(flags[:6])
    elif security:
        row['security_note'] = 'contract_security_checked'


def reclassify_scored_row(row: Dict[str, Any], max_age_hours: int, min_liquidity_usd: float) -> None:
    penalties = {
        'security_honeypot': 45,
        'security_cannot_sell_all': 45,
        'security_blacklist': 35,
        'security_not_true_token': 35,
        'security_high_sell_tax': 28,
        'security_high_buy_tax': 18,
        'security_mintable': 18,
        'security_freezable': 18,
        'security_transfer_pausable': 18,
        'security_creator_high_balance': 28,
        'security_owner_high_balance': 24,
        'security_top_holder_concentrated': 22,
        'security_top10_holder_concentrated': 14,
        'security_owner_not_renounced': 12,
        'security_lp_not_locked': 12,
        'security_not_open_source': 10,
        'security_proxy': 8,
        'security_metadata_mutable': 8,
        'security_balance_mutable_authority': 14,
        'security_selfdestruct': 30,
        'security_external_call': 12,
        'security_owner_change_balance': 20,
        'security_can_take_back_ownership': 18,
        'security_airdrop_scam': 35,
        'security_non_transferable': 35,
    }
    original_score = safe_float(row.get('early_score'))
    penalty = sum(penalties.get(flag, 0) for flag in row.get('risk_flags') or [])
    if penalty:
        row['security_score_penalty'] = penalty
        row['early_score'] = round(clamp(original_score - penalty), 2)
    row['stage'], row['confidence'] = classify_token_stage(row, max_age_hours, min_liquidity_usd)


def security_risk_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    checked = 0
    unavailable = 0
    by_flag: Dict[str, int] = {}
    for row in rows:
        security = row.get('security') or {}
        if security.get('status') == 'checked':
            checked += 1
        else:
            unavailable += 1
        for flag in row.get('risk_flags') or []:
            if str(flag).startswith('security_'):
                by_flag[flag] = by_flag.get(flag, 0) + 1
    return {
        'provider': 'goplus',
        'checked': checked,
        'unavailable': unavailable,
        'risk_tokens': sum(1 for row in rows if any(str(flag).startswith('security_') for flag in row.get('risk_flags') or [])),
        'by_flag': dict(sorted(by_flag.items(), key=lambda item: item[1], reverse=True)),
    }


def token_key(item: Dict[str, Any]) -> str:
    chain_id = str(item.get('chainId') or '').strip().lower()
    token_address = str(item.get('tokenAddress') or '').strip()
    if not chain_id or not token_address:
        return ''
    return f'{chain_id}:{token_address.lower()}'


def row_token_key(row: Dict[str, Any]) -> str:
    chain_id = str(row.get('chain_id') or row.get('chainId') or '').strip().lower()
    token_address = str(row.get('token_address') or row.get('tokenAddress') or '').strip().lower()
    if not chain_id or not token_address:
        return ''
    return f'{chain_id}:{token_address}'


def parse_utc_time(value: Any) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def connect_token_memory(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(TOKEN_MEMORY_SCHEMA)
    ensure_token_memory_columns(conn)
    return conn


def connect_meme_research(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(MEME_RESEARCH_SCHEMA)
    return conn


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def json_loads(value: Any, fallback: Any) -> Any:
    if value in (None, ''):
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def first_present_value(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, '', []):
            return row.get(key)
    return None


def normalize_candidate_chain(value: Any) -> str:
    text = str(value or '').strip().lower()
    aliases = {
        'sol': 'solana',
        'eth': 'ethereum',
        'ether': 'ethereum',
        'bnb': 'bsc',
        'binance-smart-chain': 'bsc',
        'binance_smart_chain': 'bsc',
    }
    return aliases.get(text, text)


def source_flag_slug(value: Any) -> str:
    text = str(value or '').strip().lower()
    chars = [char if char.isalnum() else '_' for char in text]
    slug = '_'.join(part for part in ''.join(chars).split('_') if part)
    return slug[:48]


def normalize_extra_candidate(item: Dict[str, Any], default_source: str = 'external_candidate') -> Dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    base_token = item.get('baseToken') if isinstance(item.get('baseToken'), dict) else {}
    token_address = first_present_value(
        item,
        'tokenAddress',
        'token_address',
        'address',
        'mint',
        'contractAddress',
        'contract_address',
        'ca',
    ) or base_token.get('address')
    chain_id = first_present_value(
        item,
        'chainId',
        'chain_id',
        'chain',
        'network',
        'blockchain',
    )
    chain_id = normalize_candidate_chain(chain_id)
    token_address = str(token_address or '').strip()
    if not chain_id or not token_address:
        return None

    source_name = first_present_value(item, 'source', 'source_name', 'provider', 'external_source') or default_source
    flags: List[str] = ['external_candidate']
    if item.get('source_flags'):
        raw_flags = item.get('source_flags')
        if isinstance(raw_flags, list):
            flags.extend(str(flag) for flag in raw_flags if flag)
        else:
            flags.extend(str(flag).strip() for flag in str(raw_flags).split(',') if flag.strip())
    source_slug = source_flag_slug(source_name)
    if source_slug and source_slug != 'external_candidate':
        flags.append(f'external_{source_slug}')
    deduped_flags: List[str] = []
    for flag in flags:
        append_unique(deduped_flags, str(flag))

    normalized: Dict[str, Any] = {
        'chainId': chain_id,
        'tokenAddress': token_address,
        'source_flags': deduped_flags,
        'external_source': str(source_name),
    }
    for field in (
        'symbol',
        'name',
        'description',
        'url',
        'icon',
        'header',
        'links',
        'amount',
        'totalAmount',
        'detected_at',
        'created_at',
    ):
        value = item.get(field)
        if value not in (None, '', []):
            normalized[field] = value
    if base_token.get('symbol') and not normalized.get('symbol'):
        normalized['symbol'] = base_token.get('symbol')
    if base_token.get('name') and not normalized.get('name'):
        normalized['name'] = base_token.get('name')
    return normalized


def candidate_rows_from_json(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ('candidates', 'tokens', 'items', 'data', 'results'):
            items = value.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return [value]
    return []


def load_extra_candidate_file(path: Path) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    stats = {
        'path': str(path),
        'rows_read': 0,
        'candidates_loaded': 0,
        'skipped_rows': 0,
    }
    if not path.exists():
        stats['error'] = 'missing_file'
        return [], stats
    source_name = path.stem
    rows: List[Dict[str, Any]] = []
    try:
        if path.suffix.lower() == '.csv':
            with path.open('r', encoding='utf-8-sig', newline='') as handle:
                rows = [dict(row) for row in csv.DictReader(handle)]
        elif path.suffix.lower() == '.jsonl':
            with path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    rows.append(json.loads(text))
        else:
            rows = candidate_rows_from_json(json.loads(path.read_text(encoding='utf-8')))
    except Exception as exc:
        stats['error'] = str(exc)
        return [], stats

    normalized: List[Dict[str, Any]] = []
    stats['rows_read'] = len(rows)
    for row in rows:
        candidate = normalize_extra_candidate(row, default_source=source_name)
        if candidate:
            normalized.append(candidate)
        else:
            stats['skipped_rows'] += 1
    stats['candidates_loaded'] = len(normalized)
    return normalized, stats


def load_meme_extra_candidate_files(paths: Iterable[str]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    files: List[Dict[str, Any]] = []
    seen = set()
    for raw_path in paths:
        path = Path(raw_path)
        loaded, stats = load_extra_candidate_file(path)
        files.append(stats)
        for candidate in loaded:
            key = token_key(candidate)
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    return candidates, {
        'files': files,
        'candidates_loaded': len(candidates),
    }


def parse_hour_windows(value: str) -> List[int]:
    windows: List[int] = []
    seen = set()
    for item in str(value or '').split(','):
        item = item.strip()
        if not item:
            continue
        hours = int(item)
        if hours <= 0:
            raise ValueError('windows must be positive hours')
        if hours not in seen:
            windows.append(hours)
            seen.add(hours)
    if not windows:
        raise ValueError('windows cannot be empty')
    return windows


def sqlite_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def pct_change(current: float, previous: float) -> float | None:
    if previous <= 0 or current <= 0:
        return None
    return ((current / previous) - 1.0) * 100.0


def append_unique(values: List[str], item: str) -> None:
    if item not in values:
        values.append(item)


def memory_delta_snapshot(row: Dict[str, Any], previous: sqlite3.Row | None) -> Dict[str, Any]:
    if previous is None:
        return {}

    current_price = safe_float(row.get('price_usd'))
    current_liquidity = safe_float(row.get('liquidity_usd'))
    current_volume_h1 = safe_float(row.get('volume_h1'))
    current_txns_h1 = int(safe_float(row.get('txns_h1')))
    previous_price = safe_float(sqlite_value(previous, 'last_price_usd'))
    previous_liquidity = safe_float(sqlite_value(previous, 'last_liquidity_usd'))
    previous_volume_h1 = safe_float(sqlite_value(previous, 'last_volume_h1'))
    previous_txns_h1 = int(safe_float(sqlite_value(previous, 'last_txns_h1')))

    price_delta = pct_change(current_price, previous_price)
    liquidity_delta = pct_change(current_liquidity, previous_liquidity)
    volume_delta = pct_change(current_volume_h1, previous_volume_h1)
    txns_delta = pct_change(float(current_txns_h1), float(previous_txns_h1))
    flags: List[str] = []

    if liquidity_delta is not None and liquidity_delta >= 50 and current_liquidity >= 10_000:
        flags.append('liquidity_accelerating')
    if volume_delta is not None and volume_delta >= 100 and current_volume_h1 >= 5_000:
        flags.append('volume_accelerating_h1')
    if txns_delta is not None and txns_delta >= 75 and current_txns_h1 >= 40:
        flags.append('txns_accelerating_h1')
    if price_delta is not None and price_delta >= 25:
        flags.append('price_breakout_since_seen')
    if liquidity_delta is not None and liquidity_delta <= -40 and previous_liquidity >= 10_000:
        flags.append('liquidity_fading')
    if volume_delta is not None and volume_delta <= -60 and previous_volume_h1 >= 5_000:
        flags.append('volume_fading_h1')
    if price_delta is not None and price_delta <= -35:
        flags.append('price_down_since_seen')
    previous_pair = str(sqlite_value(previous, 'last_pair_address') or '').strip().lower()
    current_pair = str(row.get('pair_address') or '').strip().lower()
    if previous_pair and current_pair and previous_pair != current_pair:
        flags.append('pair_changed_since_seen')

    def rounded(value: float | None) -> float | None:
        return round(value, 2) if value is not None else None

    return {
        'previous_price_usd': previous_price or None,
        'previous_liquidity_usd': previous_liquidity or None,
        'previous_volume_h1': previous_volume_h1 or None,
        'previous_txns_h1': previous_txns_h1 or None,
        'price_change_since_seen_pct': rounded(price_delta),
        'liquidity_change_since_seen_pct': rounded(liquidity_delta),
        'volume_h1_change_since_seen_pct': rounded(volume_delta),
        'txns_h1_change_since_seen_pct': rounded(txns_delta),
        'flags': flags,
    }


def apply_memory_delta_to_row(row: Dict[str, Any], delta: Dict[str, Any]) -> tuple[bool, bool]:
    if not delta:
        return False, False
    row['memory_delta'] = delta
    positive_flags = {
        'liquidity_accelerating',
        'volume_accelerating_h1',
        'txns_accelerating_h1',
        'price_breakout_since_seen',
    }
    negative_flags = {
        'liquidity_fading',
        'volume_fading_h1',
        'price_down_since_seen',
    }
    flags = set(delta.get('flags') or [])
    accelerated = bool(flags & positive_flags)
    fading = bool(flags & negative_flags)
    if accelerated:
        source_flags = row.setdefault('source_flags', [])
        append_unique(source_flags, 'memory_acceleration')
    if fading:
        risk_flags = row.setdefault('risk_flags', [])
        for flag in sorted(flags & negative_flags):
            append_unique(risk_flags, flag)
    if 'pair_changed_since_seen' in flags:
        append_unique(row.setdefault('risk_flags', []), 'pair_changed_since_seen')
    return accelerated, fading


def token_memory_weight(
    seen_count_before: int,
    hours_since_seen: float | None,
    cooldown_hours: int,
) -> tuple[float, str]:
    if seen_count_before <= 0:
        return 1.0, 'new_token'
    if cooldown_hours <= 0:
        return 1.0, 'memory_disabled'
    if hours_since_seen is not None and hours_since_seen >= cooldown_hours:
        return 1.0, 'cooldown_expired'

    repeat_floor = max(
        TOKEN_REPEAT_MIN_WEIGHT,
        1.0 - (TOKEN_REPEAT_STEP * seen_count_before),
    )
    release_ratio = 0.0
    if hours_since_seen is not None and cooldown_hours > 0:
        release_ratio = max(0.0, min(1.0, hours_since_seen / cooldown_hours))
    weight = repeat_floor + ((1.0 - repeat_floor) * release_ratio)
    return round(max(TOKEN_REPEAT_MIN_WEIGHT, min(1.0, weight)), 3), 'repeat_token'


def classify_token_stage(row: Dict[str, Any], max_age_hours: int, min_liquidity_usd: float) -> tuple[str, str]:
    score = safe_float(row.get('early_score'))
    risk_flags = set(row.get('risk_flags') or [])
    liquidity = safe_float(row.get('liquidity_usd'))
    txns_h1 = int(safe_float(row.get('txns_h1')))
    social_count = int(safe_float(row.get('social_link_count')))
    age_hours = row.get('pair_age_hours')
    critical_flags = {
        'token_is_quote',
        'stable_or_base_asset',
        'very_low_liquidity',
        'valuation_no_liquidity',
        'no_pairs',
        'security_honeypot',
        'security_cannot_sell_all',
        'security_blacklist',
        'security_not_true_token',
        'security_airdrop_scam',
        'security_non_transferable',
        'security_creator_high_balance',
        'security_owner_high_balance',
        'security_top_holder_concentrated',
        'creator_rug_history',
        'owner_rug_history',
    }

    if (
        critical_flags & risk_flags
        or {'security_high_sell_tax', 'security_selfdestruct'} & risk_flags
        or ('thin_txns_h1' in risk_flags and 'low_liquidity' in risk_flags)
        or ('dumping_h1' in risk_flags and liquidity < min_liquidity_usd * 3)
    ):
        stage = 'rug_risk'
    elif {'overheated_m5', 'overheated_h1', 'overheated_h24'} & risk_flags:
        stage = 'overheated'
    elif score >= 65 and (age_hours is None or safe_float(age_hours) <= max_age_hours):
        stage = 'early_watch'
    elif score >= 50:
        stage = 'momentum_watch'
    elif score >= 32:
        stage = 'watchlist'
    else:
        stage = 'noise'

    confidence = 'low'
    if score >= 70 and social_count >= 1 and liquidity >= min_liquidity_usd and txns_h1 >= 50:
        confidence = 'medium'
    if score >= 82 and social_count >= 2 and liquidity >= min_liquidity_usd * 2 and txns_h1 >= 100:
        confidence = 'high'
    if stage in {'rug_risk', 'overheated'}:
        confidence = 'low'
    if {'no_social_links', 'social_website_only'} & risk_flags:
        confidence = cap_meme_confidence(confidence, 'low')
    elif {'social_single_link', 'social_missing_core_social'} & risk_flags:
        confidence = cap_meme_confidence(confidence, 'medium')
    if 'discovery_unknown_source' in risk_flags:
        confidence = cap_meme_confidence(confidence, 'low')
    elif {'discovery_external_only', 'discovery_boost_only'} & risk_flags:
        confidence = cap_meme_confidence(confidence, 'medium')
    if 'paid_boost_low_traction' in risk_flags:
        confidence = 'low'
    elif 'paid_boost_weak_traction' in risk_flags and confidence == 'high':
        confidence = 'medium'
    if 'valuation_extreme_fdv_gap' in risk_flags:
        confidence = cap_meme_confidence(confidence, 'low')
    elif {'high_fdv_low_liquidity', 'valuation_stretched'} & risk_flags:
        confidence = cap_meme_confidence(confidence, 'medium')
    if {'micro_liquidity_churn_risk', 'micro_m5_spike_risk', 'micro_one_sided_pump'} & risk_flags:
        confidence = cap_meme_confidence(confidence, 'medium')
    if 'pair_migration_risk' in risk_flags:
        confidence = cap_meme_confidence(confidence, 'low')
    elif {'pair_fragmented_liquidity', 'pair_low_primary_liquidity_share', 'pair_many_thin_pools'} & risk_flags:
        confidence = cap_meme_confidence(confidence, 'medium')
    return stage, confidence


def apply_token_memory(
    rows: List[Dict[str, Any]],
    db_path: Path | None = None,
    cooldown_hours: int = DEFAULT_TOKEN_COOLDOWN_HOURS,
    max_age_hours: int = 72,
    min_liquidity_usd: float = 10_000.0,
    now: datetime | None = None,
) -> Dict[str, Any]:
    now = now or datetime.now(UTC)
    path = db_path or DEFAULT_TOKEN_MEMORY_DB
    stats: Dict[str, Any] = {
        'enabled': cooldown_hours > 0,
        'cooldown_hours': cooldown_hours,
        'db_path': str(path),
        'tokens_seen': len(rows),
        'new_tokens': 0,
        'repeat_tokens': 0,
        'cooldown_expired_tokens': 0,
        'memory_disabled_tokens': 0,
        'missing_key_tokens': 0,
        'accelerated_repeat_tokens': 0,
        'fading_repeat_tokens': 0,
        'avg_memory_weight': 1.0,
    }
    if cooldown_hours <= 0:
        for row in rows:
            row['discovery_status'] = 'memory_disabled'
            row['memory_weight'] = 1.0
        stats['memory_disabled_tokens'] = len(rows)
        return stats

    conn = None
    weights: List[float] = []
    try:
        conn = connect_token_memory(path)
        with conn:
            now_iso = now.isoformat().replace('+00:00', 'Z')
            for row in rows:
                key = row_token_key(row)
                if not key:
                    stats['missing_key_tokens'] += 1
                    row['discovery_status'] = 'missing_key'
                    row['memory_weight'] = 1.0
                    continue

                previous = conn.execute(
                    'SELECT * FROM meme_token_memory WHERE token_key = ?',
                    (key,),
                ).fetchone()
                seen_count_before = int(previous['seen_count']) if previous else 0
                last_seen = parse_utc_time(previous['last_seen_at']) if previous else None
                hours_since_seen = None
                if last_seen:
                    hours_since_seen = max(0.0, (now - last_seen).total_seconds() / 3600.0)
                weight, status = token_memory_weight(seen_count_before, hours_since_seen, cooldown_hours)
                delta = memory_delta_snapshot(row, previous)
                accelerated, fading = apply_memory_delta_to_row(row, delta)
                if accelerated:
                    stats['accelerated_repeat_tokens'] += 1
                    row['memory_weight_before_acceleration'] = weight
                    weight = round(min(1.0, weight + 0.22), 3)
                    row['memory_acceleration_weight_bonus'] = round(
                        weight - safe_float(row.get('memory_weight_before_acceleration')),
                        3,
                    )
                if fading:
                    stats['fading_repeat_tokens'] += 1
                    row['memory_fading_score_penalty'] = 8
                weights.append(weight)

                if status == 'new_token':
                    stats['new_tokens'] += 1
                elif status == 'cooldown_expired':
                    stats['cooldown_expired_tokens'] += 1
                elif status == 'memory_disabled':
                    stats['memory_disabled_tokens'] += 1
                else:
                    stats['repeat_tokens'] += 1

                raw_score = safe_float(row.get('early_score'))
                if fading:
                    raw_score = max(0.0, raw_score - safe_float(row.get('memory_fading_score_penalty')))
                row['raw_early_score_before_memory'] = round(raw_score, 2)
                row['early_score'] = round(clamp(raw_score * weight), 2)
                row['memory_weight'] = weight
                row['discovery_status'] = status
                row['seen_count_before'] = seen_count_before
                row['hours_since_seen'] = round(hours_since_seen, 2) if hours_since_seen is not None else None
                row['stage'], row['confidence'] = classify_token_stage(row, max_age_hours, min_liquidity_usd)

                first_seen = previous['first_seen_at'] if previous else now_iso
                conn.execute(
                    '''
                    INSERT INTO meme_token_memory (
                        token_key, chain_id, token_address, first_seen_at,
                        last_seen_at, seen_count, last_stage, last_score,
                        last_price_usd, last_liquidity_usd, last_volume_h1,
                        last_txns_h1, last_pair_address, last_pair_age_hours
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(token_key) DO UPDATE SET
                        last_seen_at=excluded.last_seen_at,
                        seen_count=excluded.seen_count,
                        last_stage=excluded.last_stage,
                        last_score=excluded.last_score,
                        last_price_usd=excluded.last_price_usd,
                        last_liquidity_usd=excluded.last_liquidity_usd,
                        last_volume_h1=excluded.last_volume_h1,
                        last_txns_h1=excluded.last_txns_h1,
                        last_pair_address=excluded.last_pair_address,
                        last_pair_age_hours=excluded.last_pair_age_hours
                    ''',
                    (
                        key,
                        str(row.get('chain_id') or ''),
                        str(row.get('token_address') or ''),
                        first_seen,
                        now_iso,
                        seen_count_before + 1,
                        row.get('stage'),
                        safe_float(row.get('early_score')),
                        safe_float(row.get('price_usd')),
                        safe_float(row.get('liquidity_usd')),
                        safe_float(row.get('volume_h1')),
                        int(safe_float(row.get('txns_h1'))),
                        row.get('pair_address'),
                        row.get('pair_age_hours'),
                    ),
                )
    except Exception as exc:
        stats['error'] = str(exc)
        for row in rows:
            row.setdefault('discovery_status', 'memory_error')
            row.setdefault('memory_weight', 1.0)
    finally:
        if conn is not None:
            conn.close()

    if weights:
        stats['avg_memory_weight'] = round(sum(weights) / len(weights), 3)
    return stats


def row_security_parties(row: Dict[str, Any]) -> List[Dict[str, str]]:
    security = row.get('security') or {}
    if not isinstance(security, dict) or security.get('status') != 'checked':
        return []
    chain_id = str(row.get('chain_id') or '').strip().lower()
    if not chain_id:
        return []

    parties: List[Dict[str, str]] = []
    seen_keys = set()
    for party_type, address_value in (
        ('creator', security.get('creator_address')),
        ('owner', security.get('owner_address')),
    ):
        address = normalize_security_address(address_value)
        if not address:
            continue
        creator_key = f'{chain_id}:{party_type}:{address}'
        if creator_key in seen_keys:
            continue
        seen_keys.add(creator_key)
        parties.append({
            'creator_key': creator_key,
            'chain_id': chain_id,
            'party_type': party_type,
            'address': address,
        })
    return parties


def creator_memory_token_is_risky(stage: str, risk_flags: List[str]) -> bool:
    if stage == 'rug_risk':
        return True
    return bool({
        'no_pairs',
        'very_low_liquidity',
        'security_honeypot',
        'security_cannot_sell_all',
        'security_blacklist',
        'security_not_true_token',
        'security_airdrop_scam',
        'security_non_transferable',
        'security_high_sell_tax',
        'security_selfdestruct',
        'security_creator_high_balance',
        'security_owner_high_balance',
        'security_top_holder_concentrated',
        'creator_rug_history',
        'owner_rug_history',
    } & set(risk_flags or []))


def creator_memory_summary(
    conn: sqlite3.Connection,
    creator_key: str,
    current_token_key: str,
) -> Dict[str, Any]:
    records = conn.execute(
        '''
        SELECT token_key, symbol, last_stage, last_score, last_liquidity_usd,
               last_pair_address, risk_flags_json, last_seen_at
        FROM meme_creator_memory
        WHERE creator_key = ? AND token_key <> ?
        ORDER BY last_seen_at DESC
        ''',
        (creator_key, current_token_key),
    ).fetchall()
    token_count = len(records)
    rug_count = 0
    risk_count = 0
    last_bad: Dict[str, Any] | None = None
    for record in records:
        flags = json_loads(record['risk_flags_json'], [])
        stage = str(record['last_stage'] or '')
        is_rug = stage == 'rug_risk'
        is_risky = creator_memory_token_is_risky(stage, flags)
        if is_rug:
            rug_count += 1
        if is_risky:
            risk_count += 1
            if last_bad is None:
                last_bad = {
                    'symbol': record['symbol'],
                    'stage': record['last_stage'],
                    'score': record['last_score'],
                    'last_seen_at': record['last_seen_at'],
                }
    return {
        'token_count': token_count,
        'rug_stage_count': rug_count,
        'risk_token_count': risk_count,
        'rug_rate_pct': round((rug_count / token_count) * 100.0, 2) if token_count else 0.0,
        'risk_rate_pct': round((risk_count / token_count) * 100.0, 2) if token_count else 0.0,
        'last_seen_at': records[0]['last_seen_at'] if records else None,
        'last_bad_token': last_bad,
    }


def creator_memory_flags(party_type: str, summary: Dict[str, Any]) -> List[str]:
    token_count = int(summary.get('token_count') or 0)
    if token_count <= 0:
        return []
    rug_count = int(summary.get('rug_stage_count') or 0)
    risk_count = int(summary.get('risk_token_count') or 0)
    rug_rate = rug_count / token_count
    risk_rate = risk_count / token_count
    if party_type == 'creator':
        if rug_count >= 1 and rug_rate >= 0.5:
            return ['creator_rug_history']
        if token_count >= 2 and risk_rate >= 0.5:
            return ['creator_risky_token_history']
    if party_type == 'owner':
        if token_count >= 2 and rug_rate >= 0.5:
            return ['owner_rug_history']
        if token_count >= 3 and risk_rate >= 0.5:
            return ['owner_risky_token_history']
    return []


def compact_creator_memory_summary(party: Dict[str, str], summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'party_type': party.get('party_type'),
        'address': party.get('address'),
        'token_count': summary.get('token_count'),
        'rug_stage_count': summary.get('rug_stage_count'),
        'risk_token_count': summary.get('risk_token_count'),
        'rug_rate_pct': summary.get('rug_rate_pct'),
        'risk_rate_pct': summary.get('risk_rate_pct'),
        'last_seen_at': summary.get('last_seen_at'),
        'last_bad_token': summary.get('last_bad_token'),
    }


def upsert_creator_memory(
    conn: sqlite3.Connection,
    row: Dict[str, Any],
    party: Dict[str, str],
    token_key_value: str,
    now_iso: str,
) -> None:
    previous = conn.execute(
        '''
        SELECT first_seen_at, seen_count
        FROM meme_creator_memory
        WHERE creator_key = ? AND token_key = ?
        ''',
        (party['creator_key'], token_key_value),
    ).fetchone()
    first_seen = previous['first_seen_at'] if previous else now_iso
    seen_count = int(previous['seen_count']) + 1 if previous else 1
    conn.execute(
        '''
        INSERT INTO meme_creator_memory (
            creator_key, token_key, chain_id, party_type, address,
            first_seen_at, last_seen_at, seen_count, symbol, last_stage,
            last_score, last_liquidity_usd, last_pair_address, risk_flags_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(creator_key, token_key) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            seen_count=excluded.seen_count,
            symbol=excluded.symbol,
            last_stage=excluded.last_stage,
            last_score=excluded.last_score,
            last_liquidity_usd=excluded.last_liquidity_usd,
            last_pair_address=excluded.last_pair_address,
            risk_flags_json=excluded.risk_flags_json
        ''',
        (
            party['creator_key'],
            token_key_value,
            party['chain_id'],
            party['party_type'],
            party['address'],
            first_seen,
            now_iso,
            seen_count,
            row.get('symbol'),
            row.get('stage'),
            safe_float(row.get('early_score')),
            safe_float(row.get('liquidity_usd')),
            row.get('pair_address'),
            json_dumps(row.get('risk_flags') or []),
        ),
    )


def apply_creator_memory(
    rows: List[Dict[str, Any]],
    db_path: Path | None = None,
    enabled: bool = True,
    max_age_hours: int = 72,
    min_liquidity_usd: float = 10_000.0,
    now: datetime | None = None,
) -> Dict[str, Any]:
    path = db_path or DEFAULT_TOKEN_MEMORY_DB
    stats: Dict[str, Any] = {
        'enabled': enabled,
        'db_path': str(path),
        'tokens_seen': len(rows),
        'tokens_with_party': 0,
        'matched_histories': 0,
        'risky_histories': 0,
        'adjusted_tokens': 0,
        'missing_party_tokens': 0,
    }
    if not enabled:
        return stats

    now = now or datetime.now(UTC)
    conn = None
    try:
        conn = connect_token_memory(path)
        with conn:
            now_iso = now.isoformat().replace('+00:00', 'Z')
            for row in rows:
                token_key_value = row_token_key(row)
                parties = row_security_parties(row)
                if not token_key_value or not parties:
                    stats['missing_party_tokens'] += 1
                    continue
                stats['tokens_with_party'] += 1

                histories: List[Dict[str, Any]] = []
                new_flags: List[str] = []
                for party in parties:
                    summary = creator_memory_summary(conn, party['creator_key'], token_key_value)
                    if int(summary.get('token_count') or 0) > 0:
                        stats['matched_histories'] += 1
                        histories.append(compact_creator_memory_summary(party, summary))
                    for flag in creator_memory_flags(party['party_type'], summary):
                        risk_flags = row.setdefault('risk_flags', [])
                        if flag not in risk_flags:
                            append_unique(risk_flags, flag)
                            new_flags.append(flag)

                if histories:
                    row['creator_memory'] = histories
                if new_flags:
                    penalty = min(40.0, sum(CREATOR_MEMORY_PENALTIES.get(flag, 0) for flag in new_flags))
                    if penalty:
                        row['creator_memory_score_penalty'] = penalty
                        row['early_score'] = round(clamp(safe_float(row.get('early_score')) - penalty), 2)
                    row['stage'], row['confidence'] = classify_token_stage(row, max_age_hours, min_liquidity_usd)
                    stats['risky_histories'] += 1
                    stats['adjusted_tokens'] += 1

                for party in parties:
                    upsert_creator_memory(conn, row, party, token_key_value, now_iso)
    except Exception as exc:
        stats['error'] = str(exc)
    finally:
        if conn is not None:
            conn.close()
    return stats


def all_signal_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = payload.get('all_candidates') or []
    if rows:
        return [row for row in rows if isinstance(row, dict)]
    result: List[Dict[str, Any]] = []
    for key in ('early_watch', 'momentum_watch', 'watchlist', 'rug_risk', 'overheated'):
        result.extend([row for row in (payload.get(key) or []) if isinstance(row, dict)])
    return result


def record_meme_signals(
    payload: Dict[str, Any],
    db_path: Path = DEFAULT_MEME_RESEARCH_DB,
    stages: Optional[set[str]] = None,
) -> Dict[str, Any]:
    stages = stages or {'early_watch', 'momentum_watch', 'watchlist', 'rug_risk', 'overheated'}
    generated_at = str(payload.get('generated_at') or utc_now_iso())
    run_id = generated_at
    rows = [row for row in all_signal_rows(payload) if row.get('stage') in stages]
    upserted = 0
    recorded = 0
    conn = connect_meme_research(db_path)
    try:
        with conn:
            for row in rows:
                chain_id = str(row.get('chain_id') or '').strip().lower()
                token_address = str(row.get('token_address') or '').strip()
                pair_address = str(row.get('pair_address') or '').strip()
                if not chain_id or not token_address:
                    continue
                conn.execute(
                    '''
                    INSERT INTO meme_signals (
                        run_id, generated_at, symbol, chain_id, token_address,
                        pair_address, pair_url, stage, early_score, confidence,
                        price_usd, liquidity_usd, pair_age_hours,
                        source_flags_json, risk_flags_json, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, chain_id, token_address, pair_address) DO UPDATE SET
                        stage=excluded.stage,
                        early_score=excluded.early_score,
                        confidence=excluded.confidence,
                        price_usd=excluded.price_usd,
                        liquidity_usd=excluded.liquidity_usd,
                        pair_age_hours=excluded.pair_age_hours,
                        source_flags_json=excluded.source_flags_json,
                        risk_flags_json=excluded.risk_flags_json,
                        payload_json=excluded.payload_json
                    ''',
                    (
                        run_id,
                        generated_at,
                        row.get('symbol'),
                        chain_id,
                        token_address,
                        pair_address,
                        row.get('pair_url'),
                        row.get('stage'),
                        safe_float(row.get('early_score')),
                        row.get('confidence'),
                        safe_float(row.get('price_usd')),
                        safe_float(row.get('liquidity_usd')),
                        row.get('pair_age_hours'),
                        json_dumps(row.get('source_flags') or []),
                        json_dumps(row.get('risk_flags') or []),
                        json_dumps(row),
                        utc_now_iso(),
                    ),
                )
                upserted += 1
            recorded = int(
                conn.execute(
                    'SELECT COUNT(*) FROM meme_signals WHERE run_id = ?',
                    (run_id,),
                ).fetchone()[0] or 0
            )
    finally:
        conn.close()
    return {
        'db_path': str(db_path),
        'signals_considered': len(rows),
        'signals_upserted': upserted,
        'signals_recorded': recorded,
    }


def find_pair_for_evaluation(signal: sqlite3.Row) -> Dict[str, Any] | None:
    pairs = fetch_token_pairs(str(signal['chain_id']), str(signal['token_address']))
    pair_address = str(signal['pair_address'] or '').lower()
    if pair_address:
        for pair in pairs:
            if str(pair.get('pairAddress') or '').lower() == pair_address:
                return pair
    return choose_best_pair(pairs, str(signal['token_address']))


def evaluate_single_meme_signal(conn: sqlite3.Connection, signal: sqlite3.Row, window_label: str) -> Dict[str, Any]:
    try:
        pair = find_pair_for_evaluation(signal)
        if not pair:
            raise RuntimeError('no_pair')
        entry_price = safe_float(signal['price_usd'])
        current_price = safe_float(pair.get('priceUsd'))
        if entry_price <= 0 or current_price <= 0:
            raise RuntimeError('missing_price')
        forward_return_pct = ((current_price / entry_price) - 1.0) * 100.0
        current_liquidity = safe_float(((pair.get('liquidity') or {}).get('usd')))
        current_pair = pair.get('pairAddress')
        error = None
    except Exception as exc:
        current_price = None
        forward_return_pct = None
        current_liquidity = None
        current_pair = None
        error = str(exc)

    conn.execute(
        '''
        INSERT INTO meme_evaluations (
            signal_id, window_label, evaluated_at, current_price_usd,
            forward_return_pct, current_liquidity_usd, current_pair_address, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_id, window_label) DO UPDATE SET
            evaluated_at=excluded.evaluated_at,
            current_price_usd=excluded.current_price_usd,
            forward_return_pct=excluded.forward_return_pct,
            current_liquidity_usd=excluded.current_liquidity_usd,
            current_pair_address=excluded.current_pair_address,
            error=excluded.error
        ''',
        (
            int(signal['id']),
            window_label,
            utc_now_iso(),
            current_price,
            forward_return_pct,
            current_liquidity,
            current_pair,
            error,
        ),
    )
    return {
        'signal_id': int(signal['id']),
        'symbol': signal['symbol'],
        'window_label': window_label,
        'forward_return_pct': round(forward_return_pct, 2) if forward_return_pct is not None else None,
        'error': error,
    }


def evaluate_meme_signals(
    db_path: Path = DEFAULT_MEME_RESEARCH_DB,
    windows: Optional[List[int]] = None,
    max_signals: int = 50,
    force: bool = False,
) -> Dict[str, Any]:
    windows = windows or [1, 4, 24]
    now = datetime.now(UTC)
    if not db_path.exists():
        return {'db_path': str(db_path), 'evaluated_signals': 0, 'evaluations': [], 'missing_db': True}

    conn = connect_meme_research(db_path)
    evaluations: List[Dict[str, Any]] = []
    try:
        with conn:
            for hours in windows:
                window_label = f'{hours}h'
                candidates = conn.execute(
                    '''
                    SELECT s.*
                    FROM meme_signals s
                    WHERE (
                        ? = 1 OR NOT EXISTS (
                            SELECT 1 FROM meme_evaluations e
                            WHERE e.signal_id = s.id AND e.window_label = ?
                        )
                      )
                    ORDER BY s.generated_at ASC
                    LIMIT ?
                    ''',
                    (1 if force else 0, window_label, max_signals * 5),
                ).fetchall()
                due = []
                for signal in candidates:
                    generated_at = parse_utc_time(signal['generated_at'])
                    if not generated_at:
                        continue
                    if force or generated_at <= now - timedelta(hours=hours):
                        due.append(signal)
                    if len(due) >= max_signals:
                        break
                for signal in due:
                    evaluations.append(evaluate_single_meme_signal(conn, signal, window_label))
    finally:
        conn.close()
    return {
        'db_path': str(db_path),
        'windows': [f'{item}h' for item in windows],
        'evaluated_signals': len(evaluations),
        'evaluations': evaluations,
    }


def liquidity_retention_pct(record: Dict[str, Any]) -> float | None:
    if record.get('current_liquidity_usd') in (None, ''):
        return None
    entry_liquidity = safe_float(record.get('entry_liquidity_usd'))
    current_liquidity = safe_float(record.get('current_liquidity_usd'))
    if entry_liquidity <= 0 or current_liquidity < 0:
        return None
    return (current_liquidity / entry_liquidity) * 100.0


def summarize_return_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    returns = [safe_float(row.get('forward_return_pct')) for row in records]
    liquidity_retention = [
        value for value in (liquidity_retention_pct(row) for row in records)
        if value is not None
    ]
    return {
        'sample_count': len(returns),
        'avg_return_pct': round(sum(returns) / len(returns), 2),
        'median_return_pct': round(median(returns), 2),
        'positive_rate_pct': round((sum(1 for value in returns if value > 0) / len(returns)) * 100, 2),
        'min_return_pct': round(min(returns), 2),
        'max_return_pct': round(max(returns), 2),
        'avg_liquidity_retention_pct': (
            round(sum(liquidity_retention) / len(liquidity_retention), 2)
            if liquidity_retention else None
        ),
        'liquidity_retention_sample_count': len(liquidity_retention),
        'liquidity_drain_rate_pct': (
            round((sum(1 for value in liquidity_retention if value <= 50.0) / len(liquidity_retention)) * 100, 2)
            if liquidity_retention else None
        ),
        'severe_liquidity_drain_rate_pct': (
            round((sum(1 for value in liquidity_retention if value <= 20.0) / len(liquidity_retention)) * 100, 2)
            if liquidity_retention else None
        ),
    }


def grouped_return_summary(
    records: List[Dict[str, Any]],
    label_key: str,
    key_func: Any,
    min_samples: int,
    top_n: int,
    reverse: bool = True,
) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = key_func(record)
        if key in (None, ''):
            continue
        groups[str(key)].append(record)

    rows = []
    for key, items in groups.items():
        if len(items) < min_samples:
            continue
        rows.append({label_key: key, **summarize_return_records(items)})
    rows.sort(key=lambda item: (item['avg_return_pct'], item['sample_count']), reverse=reverse)
    return rows[:top_n]


def grouped_return_summary_multi(
    records: List[Dict[str, Any]],
    label_key: str,
    key_func: Any,
    min_samples: int,
    top_n: int,
    reverse: bool = True,
) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        for key in key_func(record):
            if key in (None, ''):
                continue
            groups[str(key)].append(record)

    rows = []
    for key, items in groups.items():
        if len(items) < min_samples:
            continue
        rows.append({label_key: key, **summarize_return_records(items)})
    rows.sort(key=lambda item: (item['avg_return_pct'], item['sample_count']), reverse=reverse)
    return rows[:top_n]


def research_party_key(chain_id: Any, address: Any) -> str:
    normalized = normalize_security_address(address)
    if not normalized:
        return ''
    chain = str(chain_id or '').strip().lower()
    return f'{chain}:{normalized}' if chain else normalized


def creator_history_flags_from_record(record: Dict[str, Any]) -> List[str]:
    return [
        str(flag)
        for flag in record.get('risk_flags') or []
        if str(flag) in CREATOR_MEMORY_PENALTIES
    ]


def creator_memory_status(record: Dict[str, Any]) -> str:
    flags = set(creator_history_flags_from_record(record))
    if {'creator_rug_history', 'owner_rug_history'} & flags:
        return 'rug_history'
    if {'creator_risky_token_history', 'owner_risky_token_history'} & flags:
        return 'risky_history'
    if record.get('creator_memory'):
        return 'prior_history'
    security = record.get('security') if isinstance(record.get('security'), dict) else {}
    if (
        record.get('creator_address')
        or record.get('owner_address')
        or normalize_security_address(security.get('creator_address'))
        or normalize_security_address(security.get('owner_address'))
    ):
        return 'no_prior_history'
    return 'no_creator_owner_data'


def boost_traction_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    boost_traction = record.get('boost_traction')
    return boost_traction if isinstance(boost_traction, dict) else {}


def boost_traction_status(record: Dict[str, Any]) -> str:
    boost_traction = boost_traction_payload(record)
    status = boost_traction.get('status')
    return str(status or 'unknown')


def boost_traction_passed_bucket(record: Dict[str, Any]) -> str:
    boost_traction = boost_traction_payload(record)
    status = str(boost_traction.get('status') or 'unknown')
    if status in {'not_boosted', 'unknown'}:
        return status
    passed = boost_traction.get('organic_check_passed')
    if passed in (None, ''):
        return 'unknown'
    passed_count = int(safe_float(passed))
    if passed_count <= 1:
        return 'organic_checks_0_1'
    if passed_count == 2:
        return 'organic_checks_2'
    return 'organic_checks_3_plus'


def microstructure_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    microstructure = record.get('microstructure')
    return microstructure if isinstance(microstructure, dict) else {}


def microstructure_status(record: Dict[str, Any]) -> str:
    microstructure = microstructure_payload(record)
    status = microstructure.get('status')
    return str(status or 'unknown')


def pair_landscape_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    pair_landscape = record.get('pair_landscape')
    return pair_landscape if isinstance(pair_landscape, dict) else {}


def pair_landscape_status(record: Dict[str, Any]) -> str:
    pair_landscape = pair_landscape_payload(record)
    status = pair_landscape.get('status')
    return str(status or 'unknown')


def valuation_liquidity_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    valuation = record.get('valuation_liquidity')
    return valuation if isinstance(valuation, dict) else {}


def valuation_liquidity_status(record: Dict[str, Any]) -> str:
    valuation = valuation_liquidity_payload(record)
    status = valuation.get('status')
    return str(status or 'unknown')


def social_validation_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    social_validation = record.get('social_validation')
    return social_validation if isinstance(social_validation, dict) else {}


def social_validation_status(record: Dict[str, Any]) -> str:
    social_validation = social_validation_payload(record)
    status = social_validation.get('status')
    return str(status or 'unknown')


def discovery_source_validation_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    validation = record.get('discovery_source_validation')
    return validation if isinstance(validation, dict) else {}


def discovery_source_validation_status(record: Dict[str, Any]) -> str:
    validation = discovery_source_validation_payload(record)
    status = validation.get('status')
    return str(status or 'unknown')


def load_meme_research_records(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT
            s.id AS signal_id,
            s.symbol,
            s.chain_id,
            s.token_address,
            s.pair_address,
            s.pair_url,
            s.stage,
            s.confidence,
            s.early_score,
            s.liquidity_usd AS entry_liquidity_usd,
            s.source_flags_json,
            s.risk_flags_json,
            s.payload_json,
            e.window_label,
            e.forward_return_pct,
            e.current_liquidity_usd,
            e.current_price_usd
        FROM meme_signals s
        JOIN meme_evaluations e ON e.signal_id = s.id
        WHERE e.error IS NULL AND e.forward_return_pct IS NOT NULL
        '''
    ).fetchall()
    records: List[Dict[str, Any]] = []
    for row in rows:
        payload = json_loads(row['payload_json'], {})
        payload = payload if isinstance(payload, dict) else {}
        security = payload.get('security')
        security = security if isinstance(security, dict) else {}
        creator_memory = payload.get('creator_memory')
        creator_memory = creator_memory if isinstance(creator_memory, list) else []
        chain_id = row['chain_id']
        risk_flags = json_loads(row['risk_flags_json'], [])
        record = {
            'signal_id': row['signal_id'],
            'symbol': row['symbol'],
            'chain_id': chain_id,
            'token_address': row['token_address'],
            'pair_address': row['pair_address'],
            'pair_url': row['pair_url'],
            'stage': row['stage'],
            'confidence': row['confidence'] or 'unknown',
            'early_score': row['early_score'],
            'entry_liquidity_usd': row['entry_liquidity_usd'],
            'source_flags': json_loads(row['source_flags_json'], []),
            'risk_flags': risk_flags,
            'discovery_status': payload.get('discovery_status') or 'unknown',
            'creator_address': research_party_key(chain_id, security.get('creator_address')),
            'owner_address': research_party_key(chain_id, security.get('owner_address')),
            'creator_memory': creator_memory,
            'window_label': row['window_label'],
            'forward_return_pct': row['forward_return_pct'],
            'current_liquidity_usd': row['current_liquidity_usd'],
            'current_price_usd': row['current_price_usd'],
        }
        record['creator_history_flags'] = creator_history_flags_from_record(record)
        record['creator_memory_status'] = creator_memory_status(record)
        record['boost_traction_status'] = boost_traction_status(payload)
        record['boost_traction_passed_bucket'] = boost_traction_passed_bucket(payload)
        record['microstructure_status'] = microstructure_status(payload)
        record['pair_landscape_status'] = pair_landscape_status(payload)
        record['valuation_liquidity_status'] = valuation_liquidity_status(payload)
        record['social_validation_status'] = social_validation_status(payload)
        record['discovery_source_validation_status'] = discovery_source_validation_status(payload)
        records.append({
            **record,
        })
    return records


def summarize_cases(records: List[Dict[str, Any]], top_n: int, reverse: bool) -> List[Dict[str, Any]]:
    case_limit = min(max(top_n, 1), 20)
    rows = sorted(records, key=lambda item: safe_float(item.get('forward_return_pct')), reverse=reverse)
    return [
        {
            'symbol': row.get('symbol'),
            'chain_id': row.get('chain_id'),
            'token_address': row.get('token_address'),
            'stage': row.get('stage'),
            'confidence': row.get('confidence'),
            'window_label': row.get('window_label'),
            'forward_return_pct': round(safe_float(row.get('forward_return_pct')), 2),
            'pair_url': row.get('pair_url'),
        }
        for row in rows[:case_limit]
    ]


def summarize_meme_research(
    db_path: Path = DEFAULT_MEME_RESEARCH_DB,
    min_samples: int = 1,
    top_n: int = 20,
) -> Dict[str, Any]:
    min_samples = max(1, int(min_samples))
    top_n = max(1, int(top_n))
    if not db_path.exists():
        return {
            'generated_at': utc_now_iso(),
            'db_path': str(db_path),
            'min_samples': min_samples,
            'top_n': top_n,
            'summary': {
                'signals_recorded': 0,
                'evaluations_recorded': 0,
                'usable_return_samples': 0,
            },
            'by_stage': [],
            'by_stage_window': [],
            'by_window': [],
            'by_confidence': [],
            'by_discovery_status': [],
            'by_boost_traction_status': [],
            'by_boost_traction_passed_bucket': [],
            'by_microstructure_status': [],
            'by_pair_landscape_status': [],
            'by_valuation_liquidity_status': [],
            'by_social_validation_status': [],
            'by_discovery_source_validation_status': [],
            'by_risk_flag': [],
            'by_source_flag': [],
            'by_creator_memory_status': [],
            'by_creator_history_flag': [],
            'by_creator_address': [],
            'by_owner_address': [],
            'evaluation_errors': [],
            'best_cases': [],
            'worst_cases': [],
        }
    conn = connect_meme_research(db_path)
    try:
        signals_recorded = int(conn.execute('SELECT COUNT(*) FROM meme_signals').fetchone()[0] or 0)
        evaluations_recorded = int(conn.execute('SELECT COUNT(*) FROM meme_evaluations WHERE error IS NULL').fetchone()[0] or 0)
        error_rows = [
            {
                'error': row['error'],
                'sample_count': int(row['sample_count']),
            }
            for row in conn.execute(
                '''
                SELECT error, COUNT(*) AS sample_count
                FROM meme_evaluations e
                WHERE e.error IS NOT NULL
                GROUP BY error
                ORDER BY sample_count DESC
                '''
            )
        ]
        records = load_meme_research_records(conn)
    finally:
        conn.close()

    by_stage = grouped_return_summary(
        records, 'stage', lambda row: row.get('stage'), min_samples, top_n
    )
    by_stage_window = grouped_return_summary(
        records,
        'stage_window',
        lambda row: f"{row.get('stage')}:{row.get('window_label')}",
        min_samples,
        top_n,
    )
    by_window = grouped_return_summary(
        records, 'window_label', lambda row: row.get('window_label'), min_samples, top_n
    )
    by_confidence = grouped_return_summary(
        records, 'confidence', lambda row: row.get('confidence'), min_samples, top_n
    )
    by_discovery_status = grouped_return_summary(
        records,
        'discovery_status',
        lambda row: row.get('discovery_status'),
        min_samples,
        top_n,
    )
    by_boost_traction_status = grouped_return_summary(
        records,
        'boost_traction_status',
        lambda row: row.get('boost_traction_status'),
        min_samples,
        top_n,
    )
    by_boost_traction_passed_bucket = grouped_return_summary(
        records,
        'boost_traction_passed_bucket',
        lambda row: row.get('boost_traction_passed_bucket'),
        min_samples,
        top_n,
    )
    by_microstructure_status = grouped_return_summary(
        records,
        'microstructure_status',
        lambda row: row.get('microstructure_status'),
        min_samples,
        top_n,
    )
    by_pair_landscape_status = grouped_return_summary(
        records,
        'pair_landscape_status',
        lambda row: row.get('pair_landscape_status'),
        min_samples,
        top_n,
    )
    by_valuation_liquidity_status = grouped_return_summary(
        records,
        'valuation_liquidity_status',
        lambda row: row.get('valuation_liquidity_status'),
        min_samples,
        top_n,
        reverse=False,
    )
    by_social_validation_status = grouped_return_summary(
        records,
        'social_validation_status',
        lambda row: row.get('social_validation_status'),
        min_samples,
        top_n,
        reverse=False,
    )
    by_discovery_source_validation_status = grouped_return_summary(
        records,
        'discovery_source_validation_status',
        lambda row: row.get('discovery_source_validation_status'),
        min_samples,
        top_n,
        reverse=False,
    )
    by_risk_flag = grouped_return_summary_multi(
        records,
        'risk_flag',
        lambda row: row.get('risk_flags') or ['no_risk_flags'],
        min_samples,
        top_n,
        reverse=False,
    )
    by_source_flag = grouped_return_summary_multi(
        records,
        'source_flag',
        lambda row: row.get('source_flags') or ['no_source_flags'],
        min_samples,
        top_n,
    )
    by_creator_memory_status = grouped_return_summary(
        records,
        'creator_memory_status',
        lambda row: row.get('creator_memory_status'),
        min_samples,
        top_n,
        reverse=False,
    )
    by_creator_history_flag = grouped_return_summary_multi(
        records,
        'creator_history_flag',
        lambda row: row.get('creator_history_flags') or ['no_creator_history_flag'],
        min_samples,
        top_n,
        reverse=False,
    )
    by_creator_address = grouped_return_summary(
        records,
        'creator_address',
        lambda row: row.get('creator_address'),
        min_samples,
        top_n,
        reverse=False,
    )
    by_owner_address = grouped_return_summary(
        records,
        'owner_address',
        lambda row: row.get('owner_address'),
        min_samples,
        top_n,
        reverse=False,
    )
    return {
        'generated_at': utc_now_iso(),
        'db_path': str(db_path),
        'min_samples': min_samples,
        'top_n': top_n,
        'summary': {
            'signals_recorded': signals_recorded,
            'evaluations_recorded': evaluations_recorded,
            'usable_return_samples': len(records),
        },
        'by_stage': by_stage,
        'by_stage_window': by_stage_window,
        'by_window': by_window,
        'by_confidence': by_confidence,
        'by_discovery_status': by_discovery_status,
        'by_boost_traction_status': by_boost_traction_status,
        'by_boost_traction_passed_bucket': by_boost_traction_passed_bucket,
        'by_microstructure_status': by_microstructure_status,
        'by_pair_landscape_status': by_pair_landscape_status,
        'by_valuation_liquidity_status': by_valuation_liquidity_status,
        'by_social_validation_status': by_social_validation_status,
        'by_discovery_source_validation_status': by_discovery_source_validation_status,
        'by_risk_flag': by_risk_flag,
        'by_source_flag': by_source_flag,
        'by_creator_memory_status': by_creator_memory_status,
        'by_creator_history_flag': by_creator_history_flag,
        'by_creator_address': by_creator_address,
        'by_owner_address': by_owner_address,
        'evaluation_errors': error_rows[:top_n],
        'best_cases': summarize_cases(records, top_n, reverse=True),
        'worst_cases': summarize_cases(records, top_n, reverse=False),
    }


def summarize_rows_by_key(rows: List[Dict[str, Any]], key_name: str) -> Dict[str, Dict[str, Any]]:
    return {str(row.get(key_name)): row for row in rows if row.get(key_name) is not None}


def build_meme_edge_report(
    db_path: Path = DEFAULT_MEME_RESEARCH_DB,
    min_samples: int = 8,
) -> Dict[str, Any]:
    min_samples = max(1, int(min_samples))
    if not db_path.exists():
        return {
            'enabled': True,
            'available': False,
            'reason': 'missing_db',
            'db_path': str(db_path),
            'min_samples': min_samples,
            'usable_return_samples': 0,
            'groups': {},
        }

    conn = connect_meme_research(db_path)
    try:
        records = load_meme_research_records(conn)
    finally:
        conn.close()

    if not records:
        return {
            'enabled': True,
            'available': False,
            'reason': 'no_evaluations',
            'db_path': str(db_path),
            'min_samples': min_samples,
            'usable_return_samples': 0,
            'groups': {},
        }

    by_stage = grouped_return_summary(records, 'stage', lambda row: row.get('stage'), min_samples, 1000)
    by_confidence = grouped_return_summary(records, 'confidence', lambda row: row.get('confidence'), min_samples, 1000)
    by_discovery = grouped_return_summary(records, 'discovery_status', lambda row: row.get('discovery_status'), min_samples, 1000)
    by_boost_traction_status = grouped_return_summary(
        records,
        'boost_traction_status',
        lambda row: row.get('boost_traction_status'),
        min_samples,
        1000,
    )
    by_boost_traction_passed_bucket = grouped_return_summary(
        records,
        'boost_traction_passed_bucket',
        lambda row: row.get('boost_traction_passed_bucket'),
        min_samples,
        1000,
    )
    by_microstructure_status = grouped_return_summary(
        records,
        'microstructure_status',
        lambda row: row.get('microstructure_status'),
        min_samples,
        1000,
    )
    by_pair_landscape_status = grouped_return_summary(
        records,
        'pair_landscape_status',
        lambda row: row.get('pair_landscape_status'),
        min_samples,
        1000,
    )
    by_valuation_liquidity_status = grouped_return_summary(
        records,
        'valuation_liquidity_status',
        lambda row: row.get('valuation_liquidity_status'),
        min_samples,
        1000,
    )
    by_social_validation_status = grouped_return_summary(
        records,
        'social_validation_status',
        lambda row: row.get('social_validation_status'),
        min_samples,
        1000,
    )
    by_discovery_source_validation_status = grouped_return_summary(
        records,
        'discovery_source_validation_status',
        lambda row: row.get('discovery_source_validation_status'),
        min_samples,
        1000,
    )
    by_creator_memory_status = grouped_return_summary(
        records,
        'creator_memory_status',
        lambda row: row.get('creator_memory_status'),
        min_samples,
        1000,
    )
    by_risk_flag = grouped_return_summary_multi(
        records,
        'risk_flag',
        lambda row: row.get('risk_flags') or ['no_risk_flags'],
        min_samples,
        1000,
    )
    by_source_flag = grouped_return_summary_multi(
        records,
        'source_flag',
        lambda row: row.get('source_flags') or ['no_source_flags'],
        min_samples,
        1000,
    )
    groups = {
        'stage': summarize_rows_by_key(by_stage, 'stage'),
        'confidence': summarize_rows_by_key(by_confidence, 'confidence'),
        'discovery_status': summarize_rows_by_key(by_discovery, 'discovery_status'),
        'boost_traction_status': summarize_rows_by_key(by_boost_traction_status, 'boost_traction_status'),
        'boost_traction_passed_bucket': summarize_rows_by_key(
            by_boost_traction_passed_bucket,
            'boost_traction_passed_bucket',
        ),
        'microstructure_status': summarize_rows_by_key(by_microstructure_status, 'microstructure_status'),
        'pair_landscape_status': summarize_rows_by_key(by_pair_landscape_status, 'pair_landscape_status'),
        'valuation_liquidity_status': summarize_rows_by_key(
            by_valuation_liquidity_status,
            'valuation_liquidity_status',
        ),
        'social_validation_status': summarize_rows_by_key(
            by_social_validation_status,
            'social_validation_status',
        ),
        'discovery_source_validation_status': summarize_rows_by_key(
            by_discovery_source_validation_status,
            'discovery_source_validation_status',
        ),
        'creator_memory_status': summarize_rows_by_key(by_creator_memory_status, 'creator_memory_status'),
        'risk_flag': summarize_rows_by_key(by_risk_flag, 'risk_flag'),
        'source_flag': summarize_rows_by_key(by_source_flag, 'source_flag'),
    }
    return {
        'enabled': True,
        'available': any(groups.values()),
        'reason': 'ok' if any(groups.values()) else 'insufficient_group_samples',
        'db_path': str(db_path),
        'min_samples': min_samples,
        'usable_return_samples': len(records),
        'groups': groups,
    }


def compact_meme_edge_summary(group: str, key: str, summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'group': group,
        'key': key,
        'sample_count': summary.get('sample_count'),
        'avg_return_pct': summary.get('avg_return_pct'),
        'median_return_pct': summary.get('median_return_pct'),
        'positive_rate_pct': summary.get('positive_rate_pct'),
        'avg_liquidity_retention_pct': summary.get('avg_liquidity_retention_pct'),
        'liquidity_drain_rate_pct': summary.get('liquidity_drain_rate_pct'),
        'severe_liquidity_drain_rate_pct': summary.get('severe_liquidity_drain_rate_pct'),
    }


def has_critical_meme_risk(row: Dict[str, Any]) -> bool:
    risk_flags = set(row.get('risk_flags') or [])
    critical_flags = {
        'token_is_quote',
        'stable_or_base_asset',
        'very_low_liquidity',
        'valuation_no_liquidity',
        'no_pairs',
        'security_honeypot',
        'security_cannot_sell_all',
        'security_blacklist',
        'security_not_true_token',
        'security_airdrop_scam',
        'security_non_transferable',
        'security_high_sell_tax',
        'security_selfdestruct',
        'security_creator_high_balance',
        'security_owner_high_balance',
        'security_top_holder_concentrated',
        'security_top10_holder_concentrated',
        'creator_rug_history',
        'owner_rug_history',
    }
    return bool(risk_flags & critical_flags)


def cap_meme_confidence(confidence: str, cap: str) -> str:
    rank = {'low': 0, 'medium': 1, 'high': 2}
    reverse = {0: 'low', 1: 'medium', 2: 'high'}
    return reverse[min(rank.get(str(confidence or '').lower(), 0), rank.get(str(cap or '').lower(), 0))]


def local_meme_edge_adjustment(row: Dict[str, Any], edge_report: Dict[str, Any] | None) -> Dict[str, Any]:
    if not edge_report or not edge_report.get('available'):
        return {
            'applied': False,
            'reason': (edge_report or {}).get('reason') or 'edge_unavailable',
        }

    groups = edge_report.get('groups') or {}
    lookups: List[tuple[str, str]] = [
        ('stage', str(row.get('stage') or 'unknown')),
        ('confidence', str(row.get('confidence') or 'unknown')),
        ('discovery_status', str(row.get('discovery_status') or 'unknown')),
        ('creator_memory_status', creator_memory_status(row)),
    ]
    boost_status = boost_traction_status(row)
    if boost_status != 'unknown':
        lookups.append(('boost_traction_status', boost_status))
    boost_bucket = boost_traction_passed_bucket(row)
    if boost_bucket != 'unknown':
        lookups.append(('boost_traction_passed_bucket', boost_bucket))
    micro_status = microstructure_status(row)
    if micro_status != 'unknown':
        lookups.append(('microstructure_status', micro_status))
    pair_status = pair_landscape_status(row)
    if pair_status != 'unknown':
        lookups.append(('pair_landscape_status', pair_status))
    valuation_status = valuation_liquidity_status(row)
    if valuation_status != 'unknown':
        lookups.append(('valuation_liquidity_status', valuation_status))
    social_status = social_validation_status(row)
    if social_status != 'unknown':
        lookups.append(('social_validation_status', social_status))
    discovery_source_status = discovery_source_validation_status(row)
    if discovery_source_status != 'unknown':
        lookups.append(('discovery_source_validation_status', discovery_source_status))
    for flag in row.get('risk_flags') or []:
        lookups.append(('risk_flag', str(flag)))
    if not (row.get('risk_flags') or []):
        lookups.append(('risk_flag', 'no_risk_flags'))
    for flag in row.get('source_flags') or []:
        lookups.append(('source_flag', str(flag)))
    if not (row.get('source_flags') or []):
        lookups.append(('source_flag', 'no_source_flags'))

    matched = []
    for group_name, key in lookups:
        summary = (groups.get(group_name) or {}).get(key)
        if summary:
            matched.append(compact_meme_edge_summary(group_name, key, summary))
    if not matched:
        return {
            'applied': False,
            'reason': 'no_matching_edge_bucket',
            'min_samples': edge_report.get('min_samples'),
            'usable_return_samples': edge_report.get('usable_return_samples'),
        }

    weak_edges = [
        item for item in matched
        if (
            float(item.get('avg_return_pct') or 0.0) <= -5.0
            and float(item.get('positive_rate_pct') or 0.0) <= 45.0
        )
        or (
            item.get('avg_liquidity_retention_pct') is not None
            and float(item.get('avg_liquidity_retention_pct') or 0.0) <= 50.0
        )
        or (
            item.get('liquidity_drain_rate_pct') is not None
            and float(item.get('liquidity_drain_rate_pct') or 0.0) >= 60.0
        )
    ]
    strong_edges = [
        item for item in matched
        if float(item.get('avg_return_pct') or 0.0) >= 12.0
        and float(item.get('positive_rate_pct') or 0.0) >= 58.0
        and (
            item.get('avg_liquidity_retention_pct') is None
            or float(item.get('avg_liquidity_retention_pct') or 0.0) >= 80.0
        )
        and (
            item.get('severe_liquidity_drain_rate_pct') is None
            or float(item.get('severe_liquidity_drain_rate_pct') or 0.0) <= 20.0
        )
    ]

    if weak_edges:
        weakest_positive_rate = min(float(item.get('positive_rate_pct') or 0.0) for item in weak_edges)
        worst_severe_drain_rate = max(
            float(item.get('severe_liquidity_drain_rate_pct') or 0.0)
            for item in weak_edges
        )
        adjustment = -min(18.0, 5.0 * len(weak_edges))
        return {
            'applied': True,
            'edge': 'weak',
            'adjustment': round(adjustment, 1),
            'confidence_cap': 'low' if weakest_positive_rate <= 35.0 or worst_severe_drain_rate >= 40.0 else 'medium',
            'matched_edges': matched,
            'active_edges': weak_edges,
            'min_samples': edge_report.get('min_samples'),
            'usable_return_samples': edge_report.get('usable_return_samples'),
        }

    if strong_edges and not has_critical_meme_risk(row) and row.get('stage') not in {'rug_risk', 'overheated'}:
        adjustment = min(8.0, 2.5 * len(strong_edges))
        return {
            'applied': True,
            'edge': 'strong',
            'adjustment': round(adjustment, 1),
            'confidence_cap': None,
            'matched_edges': matched,
            'active_edges': strong_edges,
            'min_samples': edge_report.get('min_samples'),
            'usable_return_samples': edge_report.get('usable_return_samples'),
        }

    return {
        'applied': False,
        'reason': 'neutral_edge',
        'matched_edges': matched,
        'min_samples': edge_report.get('min_samples'),
        'usable_return_samples': edge_report.get('usable_return_samples'),
    }


def apply_local_meme_edge(
    rows: List[Dict[str, Any]],
    edge_report: Dict[str, Any] | None,
    max_age_hours: int,
    min_liquidity_usd: float,
) -> Dict[str, Any]:
    stats = {
        'enabled': bool((edge_report or {}).get('enabled', bool(edge_report))),
        'available': bool(edge_report and edge_report.get('available')),
        'reason': (edge_report or {}).get('reason'),
        'min_samples': (edge_report or {}).get('min_samples'),
        'usable_return_samples': (edge_report or {}).get('usable_return_samples', 0),
        'weak_edges': 0,
        'strong_edges': 0,
        'adjusted_tokens': 0,
    }
    if not edge_report or not edge_report.get('available'):
        return stats

    for row in rows:
        edge = local_meme_edge_adjustment(row, edge_report)
        row['local_research_edge'] = edge
        if not edge.get('applied'):
            continue
        adjustment = safe_float(edge.get('adjustment'))
        row['raw_early_score_before_research_edge'] = row.get('early_score')
        row['early_score'] = round(clamp(safe_float(row.get('early_score')) + adjustment), 2)
        row['research_score_adjustment'] = adjustment
        if edge.get('confidence_cap'):
            row['confidence'] = cap_meme_confidence(str(row.get('confidence') or 'low'), str(edge.get('confidence_cap')))
        row['stage'], row['confidence'] = classify_token_stage(row, max_age_hours, min_liquidity_usd)
        if edge.get('confidence_cap'):
            row['confidence'] = cap_meme_confidence(str(row.get('confidence') or 'low'), str(edge.get('confidence_cap')))
        if edge.get('edge') == 'weak':
            stats['weak_edges'] += 1
        elif edge.get('edge') == 'strong':
            stats['strong_edges'] += 1
        stats['adjusted_tokens'] += 1
    return stats


def social_links_from_profile(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    links = profile.get('links') or []
    return [item for item in links if isinstance(item, dict)]


def social_links_from_pair(pair: Dict[str, Any]) -> List[Dict[str, Any]]:
    info = pair.get('info') or {}
    links: List[Dict[str, Any]] = []
    for item in info.get('websites') or []:
        if isinstance(item, dict) and item.get('url'):
            links.append({'type': 'website', 'url': item.get('url')})
    for item in info.get('socials') or []:
        if isinstance(item, dict):
            links.append({
                'type': item.get('type') or item.get('platform') or 'social',
                'url': item.get('url') or item.get('handle'),
            })
    return links


def count_social_links(*groups: Iterable[Dict[str, Any]]) -> int:
    urls = set()
    for group in groups:
        for item in group:
            url = str(item.get('url') or '').strip().lower()
            if url:
                urls.add(url)
    return len(urls)


def social_link_platform(item: Dict[str, Any]) -> str:
    raw = ' '.join([
        str(item.get('type') or ''),
        str(item.get('platform') or ''),
        str(item.get('url') or ''),
    ]).strip().lower()
    if 'twitter' in raw or 'x.com' in raw:
        return 'twitter'
    if 'telegram' in raw or 't.me' in raw:
        return 'telegram'
    if 'discord' in raw or 'discord.gg' in raw:
        return 'discord'
    if 'website' in raw or raw.startswith('http'):
        return 'website'
    return 'other'


def social_validation_snapshot(
    profile_socials: List[Dict[str, Any]],
    pair_socials: List[Dict[str, Any]],
) -> Dict[str, Any]:
    links = [item for item in [*profile_socials, *pair_socials] if isinstance(item, dict)]
    urls = {
        str(item.get('url') or '').strip().lower()
        for item in links
        if str(item.get('url') or '').strip()
    }
    platforms = sorted({
        social_link_platform(item)
        for item in links
        if str(item.get('url') or '').strip()
    })
    link_count = len(urls)
    has_twitter = 'twitter' in platforms
    has_telegram = 'telegram' in platforms
    has_website = 'website' in platforms

    risk_flags: List[str] = []
    support_flags: List[str] = []
    if link_count <= 0:
        status = 'no_social_links'
        risk_flags.append('no_social_links')
    elif platforms == ['website']:
        status = 'website_only'
        risk_flags.append('social_website_only')
    elif link_count == 1:
        status = 'single_social_link'
        risk_flags.append('social_single_link')
    elif has_twitter and has_telegram:
        status = 'social_stack_confirmed'
        support_flags.append('social_stack_confirmed')
    elif has_twitter or has_telegram:
        status = 'core_social_present'
        support_flags.append('core_social_present')
    else:
        status = 'missing_core_social'
        risk_flags.append('social_missing_core_social')

    return {
        'status': status,
        'risk_flags': risk_flags,
        'support_flags': support_flags,
        'link_count': link_count,
        'platforms': platforms,
        'has_twitter': has_twitter,
        'has_telegram': has_telegram,
        'has_website': has_website,
    }


def discovery_source_validation_snapshot(source_flags: List[str]) -> Dict[str, Any]:
    flags = sorted({str(flag) for flag in source_flags if flag})
    has_profile = 'latest_profile' in flags
    has_boost = 'latest_boost' in flags or 'top_boost' in flags
    has_external = 'external_candidate' in flags or any(flag.startswith('external_') for flag in flags)
    discovery_groups = [
        group for group, present in (
            ('profile', has_profile),
            ('boost', has_boost),
            ('external', has_external),
        )
        if present
    ]
    risk_flags: List[str] = []
    support_flags: List[str] = []

    if len(discovery_groups) >= 2:
        if has_external and (has_profile or has_boost):
            status = 'external_native_confirmed'
        elif has_profile and has_boost:
            status = 'profile_boost_confirmed'
        else:
            status = 'cross_source_confirmed'
        support_flags.append('discovery_cross_source_confirmed')
    elif has_profile:
        status = 'profile_only'
    elif has_boost:
        status = 'boost_only'
        risk_flags.append('discovery_boost_only')
    elif has_external:
        status = 'external_only'
        risk_flags.append('discovery_external_only')
    else:
        status = 'unknown_source'
        risk_flags.append('discovery_unknown_source')

    return {
        'status': status,
        'risk_flags': risk_flags,
        'support_flags': support_flags,
        'source_flags': flags,
        'discovery_groups': discovery_groups,
        'source_group_count': len(discovery_groups),
        'has_profile': has_profile,
        'has_boost': has_boost,
        'has_external': has_external,
    }


def pair_created_age_hours(pair: Dict[str, Any], now: datetime | None = None) -> float | None:
    created = pair.get('pairCreatedAt')
    if created in (None, ''):
        return None
    try:
        ts = float(created)
        if ts > 1e12:
            ts /= 1000.0
        created_at = datetime.fromtimestamp(ts, tz=UTC)
    except Exception:
        return None
    now = now or datetime.now(UTC)
    return max(0.0, (now - created_at).total_seconds() / 3600.0)


def pair_txn_count(pair: Dict[str, Any], window: str) -> int:
    bucket = (pair.get('txns') or {}).get(window) or {}
    return int(safe_float(bucket.get('buys')) + safe_float(bucket.get('sells')))


def pair_buys(pair: Dict[str, Any], window: str) -> int:
    return int(safe_float(((pair.get('txns') or {}).get(window) or {}).get('buys')))


def pair_sells(pair: Dict[str, Any], window: str) -> int:
    return int(safe_float(((pair.get('txns') or {}).get(window) or {}).get('sells')))


def pair_quality_score(pair: Dict[str, Any], token_address: str) -> float:
    base = pair.get('baseToken') or {}
    quote = pair.get('quoteToken') or {}
    base_addr = str(base.get('address') or '').lower()
    quote_addr = str(quote.get('address') or '').lower()
    token_addr = str(token_address or '').lower()
    liquidity = safe_float(((pair.get('liquidity') or {}).get('usd')))
    volume_h1 = safe_float(((pair.get('volume') or {}).get('h1')))
    volume_h24 = safe_float(((pair.get('volume') or {}).get('h24')))
    txns_h1 = pair_txn_count(pair, 'h1')
    score = min(liquidity / 2500.0, 35) + min(volume_h1 / 1000.0, 25) + min(volume_h24 / 10000.0, 20) + min(txns_h1 / 3.0, 20)
    if base_addr == token_addr:
        score += 10
    elif quote_addr == token_addr:
        score -= 20
    if safe_float(pair.get('priceUsd')) <= 0:
        score -= 50
    return score


def choose_best_pair(pairs: List[Dict[str, Any]], token_address: str) -> Dict[str, Any] | None:
    candidates = [pair for pair in pairs if isinstance(pair, dict)]
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair_quality_score(pair, token_address))


def pair_landscape_snapshot(
    pairs: List[Dict[str, Any]],
    selected_pair: Dict[str, Any],
    min_liquidity_usd: float,
    now: datetime | None = None,
) -> Dict[str, Any]:
    now = now or datetime.now(UTC)
    candidates = [pair for pair in pairs if isinstance(pair, dict)]
    selected_address = str(selected_pair.get('pairAddress') or '').strip().lower()
    selected_liquidity = safe_float(((selected_pair.get('liquidity') or {}).get('usd')))
    selected_volume_h1 = safe_float(((selected_pair.get('volume') or {}).get('h1')))
    selected_age = pair_created_age_hours(selected_pair, now=now)

    pair_rows: List[Dict[str, Any]] = []
    for pair in candidates:
        liquidity = safe_float(((pair.get('liquidity') or {}).get('usd')))
        volume_h1 = safe_float(((pair.get('volume') or {}).get('h1')))
        age = pair_created_age_hours(pair, now=now)
        active = liquidity > 0 or volume_h1 > 0
        significant = liquidity >= min_liquidity_usd * 0.25 or volume_h1 >= 5_000
        pair_rows.append({
            'pair_address': str(pair.get('pairAddress') or '').strip().lower(),
            'dex_id': pair.get('dexId'),
            'liquidity_usd': liquidity,
            'volume_h1': volume_h1,
            'age_hours': age,
            'active': active,
            'significant': significant,
        })

    active_rows = [row for row in pair_rows if row['active']]
    significant_rows = [row for row in pair_rows if row['significant']]
    total_liquidity = sum(row['liquidity_usd'] for row in active_rows)
    total_volume_h1 = sum(row['volume_h1'] for row in active_rows)
    selected_liquidity_share = selected_liquidity / total_liquidity if total_liquidity > 0 else 0.0
    selected_volume_share_h1 = selected_volume_h1 / total_volume_h1 if total_volume_h1 > 0 else 0.0
    younger_significant_pairs = [
        row for row in significant_rows
        if row['pair_address'] != selected_address
        and selected_age is not None
        and row['age_hours'] is not None
        and row['age_hours'] + 1.0 < selected_age
    ]

    risk_flags: List[str] = []
    support_flags: List[str] = []
    if len(active_rows) >= 4 and selected_liquidity_share < 0.55:
        risk_flags.append('pair_fragmented_liquidity')
    if total_liquidity >= min_liquidity_usd and len(significant_rows) >= 2 and selected_liquidity_share < 0.45:
        risk_flags.append('pair_low_primary_liquidity_share')
    if len(active_rows) >= 5 and total_liquidity < min_liquidity_usd * 2:
        risk_flags.append('pair_many_thin_pools')
    if younger_significant_pairs and selected_volume_share_h1 < 0.6:
        risk_flags.append('pair_migration_risk')

    if selected_liquidity >= min_liquidity_usd and selected_liquidity_share >= 0.7:
        support_flags.append('primary_pair_dominant')
    if len(active_rows) >= 2 and total_liquidity >= min_liquidity_usd * 2 and selected_liquidity_share >= 0.55:
        support_flags.append('multi_pair_liquidity_confirmed')

    if 'pair_migration_risk' in risk_flags:
        status = 'migration_risk'
    elif 'pair_fragmented_liquidity' in risk_flags or 'pair_low_primary_liquidity_share' in risk_flags:
        status = 'fragmented_liquidity'
    elif 'pair_many_thin_pools' in risk_flags:
        status = 'many_thin_pools'
    elif 'multi_pair_liquidity_confirmed' in support_flags:
        status = 'multi_pair_confirmed'
    elif 'primary_pair_dominant' in support_flags:
        status = 'primary_pair_dominant'
    elif len(active_rows) <= 1:
        status = 'single_pair'
    else:
        status = 'mixed_pairs'

    top_pairs = sorted(
        pair_rows,
        key=lambda row: (row['liquidity_usd'], row['volume_h1']),
        reverse=True,
    )[:5]
    return {
        'status': status,
        'risk_flags': risk_flags,
        'support_flags': support_flags,
        'pair_count': len(candidates),
        'active_pair_count': len(active_rows),
        'significant_pair_count': len(significant_rows),
        'total_liquidity_usd': round(total_liquidity, 2),
        'total_volume_h1': round(total_volume_h1, 2),
        'selected_liquidity_share': round(selected_liquidity_share, 3),
        'selected_volume_share_h1': round(selected_volume_share_h1, 3),
        'younger_significant_pair_count': len(younger_significant_pairs),
        'top_pairs': [
            {
                'pair_address': row['pair_address'],
                'dex_id': row.get('dex_id'),
                'liquidity_usd': round(row['liquidity_usd'], 2),
                'volume_h1': round(row['volume_h1'], 2),
                'age_hours': round(row['age_hours'], 2) if row['age_hours'] is not None else None,
            }
            for row in top_pairs
        ],
    }


def looks_meme_like(profile: Dict[str, Any], pair: Dict[str, Any]) -> bool:
    base = pair.get('baseToken') or {}
    blob = ' '.join([
        str(base.get('symbol') or ''),
        str(base.get('name') or ''),
        str(profile.get('description') or ''),
        str(profile.get('url') or ''),
    ]).lower()
    return any(keyword in blob for keyword in MEME_KEYWORDS)


def boost_traction_snapshot(
    boost_signal: float,
    liquidity: float,
    volume_h1: float,
    txns_h1: int,
    social_count: int,
    buy_sell_ratio_h1: float,
    min_liquidity_usd: float,
) -> Dict[str, Any]:
    organic_checks = {
        'liquidity_ok': liquidity >= min_liquidity_usd,
        'volume_h1_ok': volume_h1 >= 5_000,
        'txns_h1_ok': txns_h1 >= 50,
        'socials_ok': social_count >= 1,
        'flow_not_one_sided': 0.4 <= buy_sell_ratio_h1 <= 8.0,
    }
    passed = sum(1 for value in organic_checks.values() if value)
    if boost_signal <= 0:
        status = 'not_boosted'
    elif passed <= 1:
        status = 'low_traction'
    elif passed <= 2:
        status = 'weak_traction'
    else:
        status = 'supported'
    return {
        'status': status,
        'boost_signal': round(boost_signal, 2),
        'organic_checks': organic_checks,
        'organic_check_passed': passed,
    }


def valuation_liquidity_snapshot(
    fdv: float,
    liquidity: float,
    volume_h1: float,
    txns_h1: int,
    min_liquidity_usd: float,
) -> Dict[str, Any]:
    fdv_to_liquidity = fdv / liquidity if fdv > 0 and liquidity > 0 else None
    volume_liquidity_h1 = volume_h1 / liquidity if liquidity > 0 else 0.0
    risk_flags: List[str] = []
    support_flags: List[str] = []

    if liquidity <= 0:
        status = 'no_liquidity'
        risk_flags.append('valuation_no_liquidity')
    elif fdv_to_liquidity is None:
        status = 'unknown_valuation'
    elif fdv_to_liquidity >= 300 and liquidity < min_liquidity_usd * 5:
        status = 'extreme_fdv_gap'
        risk_flags.append('valuation_extreme_fdv_gap')
    elif fdv_to_liquidity >= 120:
        status = 'high_fdv_low_liquidity'
        risk_flags.append('high_fdv_low_liquidity')
    elif fdv_to_liquidity >= 80 and liquidity < min_liquidity_usd * 3:
        status = 'stretched_valuation'
        risk_flags.append('valuation_stretched')
    elif liquidity < min_liquidity_usd:
        status = 'thin_liquidity'
    elif fdv_to_liquidity <= 60 and txns_h1 >= 80 and 0.15 <= volume_liquidity_h1 <= 4.0:
        status = 'healthy_launch_liquidity'
        support_flags.append('valuation_liquidity_aligned')
    else:
        status = 'balanced_valuation'

    return {
        'status': status,
        'risk_flags': risk_flags,
        'support_flags': support_flags,
        'fdv': round(fdv, 2) if fdv else None,
        'liquidity_usd': round(liquidity, 2),
        'fdv_to_liquidity': round(fdv_to_liquidity, 2) if fdv_to_liquidity is not None else None,
        'volume_liquidity_h1': round(volume_liquidity_h1, 3),
        'txns_h1': txns_h1,
    }


def microstructure_snapshot(
    liquidity: float,
    volume_m5: float,
    volume_h1: float,
    volume_h24: float,
    txns_m5: int,
    txns_h1: int,
    buy_sell_ratio_h1: float,
    price_change_m5: float,
    price_change_h1: float,
    min_liquidity_usd: float,
) -> Dict[str, Any]:
    volume_liquidity_h1 = volume_h1 / liquidity if liquidity > 0 else 0.0
    volume_liquidity_h24 = volume_h24 / liquidity if liquidity > 0 else 0.0
    m5_volume_share_h1 = volume_m5 / volume_h1 if volume_h1 > 0 else 0.0
    m5_txn_share_h1 = txns_m5 / txns_h1 if txns_h1 > 0 else 0.0
    avg_trade_h1 = volume_h1 / txns_h1 if txns_h1 > 0 else 0.0

    risk_flags: List[str] = []
    support_flags: List[str] = []
    if txns_h1 < 20 or volume_h1 < 1_000:
        risk_flags.append('micro_thin_flow')
    if volume_liquidity_h1 >= 8.0 or (volume_liquidity_h1 >= 4.0 and txns_h1 < 80):
        risk_flags.append('micro_liquidity_churn_risk')
    if volume_m5 >= 5_000 and m5_volume_share_h1 >= 0.75 and price_change_m5 >= 20:
        risk_flags.append('micro_m5_spike_risk')
    if buy_sell_ratio_h1 > 10 and price_change_h1 >= 50:
        risk_flags.append('micro_one_sided_pump')

    healthy_early = (
        liquidity >= min_liquidity_usd
        and txns_h1 >= 100
        and 0.7 <= buy_sell_ratio_h1 <= 4.0
        and 0.2 <= volume_liquidity_h1 <= 3.0
        and 5 <= price_change_h1 <= 80
        and m5_volume_share_h1 <= 0.7
    )
    balanced_momentum = (
        txns_h1 >= 80
        and volume_h1 >= 5_000
        and 0.6 <= buy_sell_ratio_h1 <= 6.0
        and -10 <= price_change_h1 <= 90
        and volume_liquidity_h1 <= 4.0
        and m5_txn_share_h1 <= 0.75
    )
    if healthy_early:
        support_flags.append('micro_healthy_early_flow')
    elif balanced_momentum:
        support_flags.append('micro_balanced_momentum')

    if 'micro_liquidity_churn_risk' in risk_flags:
        status = 'liquidity_churn_risk'
    elif 'micro_m5_spike_risk' in risk_flags:
        status = 'm5_spike_risk'
    elif 'micro_one_sided_pump' in risk_flags:
        status = 'one_sided_pump'
    elif 'micro_thin_flow' in risk_flags:
        status = 'thin_flow'
    elif healthy_early:
        status = 'healthy_early_flow'
    elif balanced_momentum:
        status = 'balanced_momentum'
    else:
        status = 'mixed_flow'

    return {
        'status': status,
        'risk_flags': risk_flags,
        'support_flags': support_flags,
        'volume_liquidity_h1': round(volume_liquidity_h1, 3),
        'volume_liquidity_h24': round(volume_liquidity_h24, 3),
        'm5_volume_share_h1': round(m5_volume_share_h1, 3),
        'm5_txn_share_h1': round(m5_txn_share_h1, 3),
        'avg_trade_h1': round(avg_trade_h1, 2),
    }


def score_pair(
    profile: Dict[str, Any],
    pair: Dict[str, Any],
    source_flags: List[str],
    max_age_hours: int,
    min_liquidity_usd: float,
    pair_landscape: Dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Dict[str, Any]:
    now = now or datetime.now(UTC)
    base = pair.get('baseToken') or {}
    quote = pair.get('quoteToken') or {}
    token_address = str(profile.get('tokenAddress') or '').strip()
    token_addr_lower = token_address.lower()
    base_addr = str(base.get('address') or '').lower()
    quote_addr = str(quote.get('address') or '').lower()
    symbol = str(base.get('symbol') or profile.get('symbol') or '').upper()
    original_source_flags = list(source_flags or [])

    liquidity = safe_float(((pair.get('liquidity') or {}).get('usd')))
    fdv = safe_float(pair.get('fdv') or pair.get('marketCap'))
    volume_m5 = safe_float(((pair.get('volume') or {}).get('m5')))
    volume_h1 = safe_float(((pair.get('volume') or {}).get('h1')))
    volume_h24 = safe_float(((pair.get('volume') or {}).get('h24')))
    buys_m5 = pair_buys(pair, 'm5')
    sells_m5 = pair_sells(pair, 'm5')
    txns_m5 = buys_m5 + sells_m5
    buys_h1 = pair_buys(pair, 'h1')
    sells_h1 = pair_sells(pair, 'h1')
    txns_h1 = buys_h1 + sells_h1
    buy_sell_ratio_h1 = buys_h1 / max(sells_h1, 1)
    age_hours = pair_created_age_hours(pair, now=now)
    price_change_m5 = safe_float((pair.get('priceChange') or {}).get('m5'))
    price_change_h1 = safe_float((pair.get('priceChange') or {}).get('h1'))
    price_change_h24 = safe_float((pair.get('priceChange') or {}).get('h24'))
    profile_socials = social_links_from_profile(profile)
    pair_socials = social_links_from_pair(pair)
    social_count = count_social_links(profile_socials, pair_socials)
    social_validation = social_validation_snapshot(profile_socials, pair_socials)
    discovery_source_validation = discovery_source_validation_snapshot(original_source_flags)
    boosted_amount = safe_float(profile.get('amount'))
    boosted_total = safe_float(profile.get('totalAmount'))
    pair_boosts = safe_float((pair.get('boosts') or {}).get('active'))
    boost_signal = boosted_amount + boosted_total + pair_boosts * 10.0
    boost_traction = boost_traction_snapshot(
        boost_signal,
        liquidity,
        volume_h1,
        txns_h1,
        social_count,
        buy_sell_ratio_h1,
        min_liquidity_usd,
    )
    microstructure = microstructure_snapshot(
        liquidity,
        volume_m5,
        volume_h1,
        volume_h24,
        txns_m5,
        txns_h1,
        buy_sell_ratio_h1,
        price_change_m5,
        price_change_h1,
        min_liquidity_usd,
    )
    valuation_liquidity = valuation_liquidity_snapshot(
        fdv,
        liquidity,
        volume_h1,
        txns_h1,
        min_liquidity_usd,
    )
    pair_landscape = pair_landscape if isinstance(pair_landscape, dict) else {'status': 'unknown'}

    risk_flags: List[str] = []
    if quote_addr == token_addr_lower and base_addr != token_addr_lower:
        risk_flags.append('token_is_quote')
    if symbol in STABLE_SYMBOLS:
        risk_flags.append('stable_or_base_asset')
    if age_hours is None:
        risk_flags.append('unknown_pair_age')
    elif age_hours > max_age_hours:
        risk_flags.append('old_pair')
    if liquidity < 5_000:
        risk_flags.append('very_low_liquidity')
    elif liquidity < min_liquidity_usd:
        risk_flags.append('low_liquidity')
    if txns_h1 < 20:
        risk_flags.append('thin_txns_h1')
    for flag in social_validation.get('risk_flags') or []:
        append_unique(risk_flags, flag)
    for flag in social_validation.get('support_flags') or []:
        append_unique(source_flags, flag)
    for flag in discovery_source_validation.get('risk_flags') or []:
        append_unique(risk_flags, flag)
    for flag in discovery_source_validation.get('support_flags') or []:
        append_unique(source_flags, flag)
    for flag in valuation_liquidity.get('risk_flags') or []:
        append_unique(risk_flags, flag)
    for flag in valuation_liquidity.get('support_flags') or []:
        append_unique(source_flags, flag)
    if price_change_m5 > 35:
        risk_flags.append('overheated_m5')
    if price_change_h1 > 120:
        risk_flags.append('overheated_h1')
    if price_change_h24 > 300:
        risk_flags.append('overheated_h24')
    if price_change_h1 < -45:
        risk_flags.append('dumping_h1')
    if price_change_h24 < -70:
        risk_flags.append('crashed_h24')
    if buy_sell_ratio_h1 > 12 and txns_h1 < 80:
        risk_flags.append('one_sided_thin_flow')
    if boost_traction.get('status') == 'low_traction':
        risk_flags.append('paid_boost_low_traction')
    elif boost_traction.get('status') == 'weak_traction':
        risk_flags.append('paid_boost_weak_traction')
    for flag in microstructure.get('risk_flags') or []:
        append_unique(risk_flags, flag)
    for flag in microstructure.get('support_flags') or []:
        append_unique(source_flags, flag)
    for flag in pair_landscape.get('risk_flags') or []:
        append_unique(risk_flags, flag)
    for flag in pair_landscape.get('support_flags') or []:
        append_unique(source_flags, flag)

    score = 0.0
    if age_hours is None:
        score += 4
    elif age_hours <= 1:
        score += 20
    elif age_hours <= 6:
        score += 16
    elif age_hours <= 24:
        score += 10
    elif age_hours <= max_age_hours:
        score += 5
    else:
        score -= 10

    if 5_000 <= liquidity < 25_000:
        score += 8
    elif 25_000 <= liquidity <= 250_000:
        score += 16
    elif liquidity > 250_000:
        score += 10
    score += min(volume_m5 / 1_500.0, 10)
    score += min(volume_h1 / 5_000.0, 18)
    score += min(txns_h1 / 6.0, 16)
    score += min(max(buys_h1 - sells_h1, 0) / 4.0, 8)
    if 1.2 <= buy_sell_ratio_h1 <= 6:
        score += min((buy_sell_ratio_h1 - 1.0) * 4.0, 12)
    if 5 <= price_change_h1 <= 80:
        score += 8
    elif price_change_h1 > 120:
        score -= 10
    if social_count >= 2:
        score += 8
    elif social_count == 1:
        score += 4
    if social_validation.get('status') == 'social_stack_confirmed':
        score += 4
    elif social_validation.get('status') == 'core_social_present':
        score += 2
    if discovery_source_validation.get('status') in {'external_native_confirmed', 'profile_boost_confirmed'}:
        score += 5
    elif discovery_source_validation.get('status') == 'cross_source_confirmed':
        score += 3
    if looks_meme_like(profile, pair):
        score += 6
    if boost_signal:
        score += min(boost_signal / 20.0, 14)
    if microstructure.get('status') == 'healthy_early_flow':
        score += 8
    elif microstructure.get('status') == 'balanced_momentum':
        score += 4
    if valuation_liquidity.get('status') == 'healthy_launch_liquidity':
        score += 3
    if pair_landscape.get('status') == 'primary_pair_dominant':
        score += 4
    elif pair_landscape.get('status') == 'multi_pair_confirmed':
        score += 3

    penalties = {
        'token_is_quote': 20,
        'stable_or_base_asset': 40,
        'very_low_liquidity': 22,
        'low_liquidity': 8,
        'thin_txns_h1': 12,
        'no_social_links': 6,
        'social_website_only': 6,
        'social_single_link': 4,
        'social_missing_core_social': 4,
        'discovery_external_only': 5,
        'discovery_boost_only': 6,
        'discovery_unknown_source': 8,
        'high_fdv_low_liquidity': 10,
        'valuation_no_liquidity': 18,
        'valuation_extreme_fdv_gap': 8,
        'valuation_stretched': 6,
        'overheated_m5': 10,
        'overheated_h1': 14,
        'overheated_h24': 10,
        'dumping_h1': 28,
        'crashed_h24': 18,
        'one_sided_thin_flow': 8,
        'old_pair': 10,
        'paid_boost_low_traction': 14,
        'paid_boost_weak_traction': 6,
        'micro_thin_flow': 6,
        'micro_liquidity_churn_risk': 14,
        'micro_m5_spike_risk': 10,
        'micro_one_sided_pump': 10,
        'pair_fragmented_liquidity': 12,
        'pair_low_primary_liquidity_share': 10,
        'pair_many_thin_pools': 8,
        'pair_migration_risk': 14,
    }
    score -= sum(penalties.get(flag, 0) for flag in risk_flags)
    score = round(clamp(score), 2)

    critical_flags = {
        'token_is_quote',
        'stable_or_base_asset',
        'very_low_liquidity',
        'valuation_no_liquidity',
        'no_pairs',
        'security_honeypot',
        'security_cannot_sell_all',
        'security_blacklist',
        'security_not_true_token',
        'security_airdrop_scam',
        'security_non_transferable',
        'security_creator_high_balance',
        'security_owner_high_balance',
        'security_top_holder_concentrated',
        'creator_rug_history',
        'owner_rug_history',
    }
    if (
        critical_flags & set(risk_flags)
        or {'security_high_sell_tax', 'security_selfdestruct'} & set(risk_flags)
        or ('thin_txns_h1' in risk_flags and 'low_liquidity' in risk_flags)
        or ('dumping_h1' in risk_flags and liquidity < min_liquidity_usd * 3)
    ):
        stage = 'rug_risk'
    elif 'overheated_m5' in risk_flags or 'overheated_h1' in risk_flags or 'overheated_h24' in risk_flags:
        stage = 'overheated'
    elif score >= 65 and (age_hours is None or age_hours <= max_age_hours):
        stage = 'early_watch'
    elif score >= 50:
        stage = 'momentum_watch'
    elif score >= 32:
        stage = 'watchlist'
    else:
        stage = 'noise'

    confidence = 'low'
    if score >= 70 and social_count >= 1 and liquidity >= min_liquidity_usd and txns_h1 >= 50:
        confidence = 'medium'
    if score >= 82 and social_count >= 2 and liquidity >= min_liquidity_usd * 2 and txns_h1 >= 100:
        confidence = 'high'
    if stage in {'rug_risk', 'overheated'}:
        confidence = 'low'
    if {'no_social_links', 'social_website_only'} & set(risk_flags):
        confidence = cap_meme_confidence(confidence, 'low')
    elif {'social_single_link', 'social_missing_core_social'} & set(risk_flags):
        confidence = cap_meme_confidence(confidence, 'medium')
    if 'discovery_unknown_source' in risk_flags:
        confidence = cap_meme_confidence(confidence, 'low')
    elif {'discovery_external_only', 'discovery_boost_only'} & set(risk_flags):
        confidence = cap_meme_confidence(confidence, 'medium')
    if 'paid_boost_low_traction' in risk_flags:
        confidence = 'low'
    elif 'paid_boost_weak_traction' in risk_flags and confidence == 'high':
        confidence = 'medium'
    if 'valuation_extreme_fdv_gap' in risk_flags:
        confidence = cap_meme_confidence(confidence, 'low')
    elif {'high_fdv_low_liquidity', 'valuation_stretched'} & set(risk_flags):
        confidence = cap_meme_confidence(confidence, 'medium')
    if {'micro_liquidity_churn_risk', 'micro_m5_spike_risk', 'micro_one_sided_pump'} & set(risk_flags):
        confidence = cap_meme_confidence(confidence, 'medium')
    if 'pair_migration_risk' in risk_flags:
        confidence = cap_meme_confidence(confidence, 'low')
    elif {'pair_fragmented_liquidity', 'pair_low_primary_liquidity_share', 'pair_many_thin_pools'} & set(risk_flags):
        confidence = cap_meme_confidence(confidence, 'medium')

    reason_parts = []
    if age_hours is not None:
        reason_parts.append(f'pair_age={age_hours:.1f}h')
    if liquidity:
        reason_parts.append(f'liq=${liquidity:.0f}')
    if valuation_liquidity.get('fdv_to_liquidity') is not None:
        reason_parts.append(f"fdv/liq={valuation_liquidity.get('fdv_to_liquidity')}")
    if volume_h1:
        reason_parts.append(f'vol_1h=${volume_h1:.0f}')
    if txns_h1:
        reason_parts.append(f'txns_1h={txns_h1}')
    if buy_sell_ratio_h1 > 1:
        reason_parts.append(f'buy/sell_1h={buy_sell_ratio_h1:.2f}')
    if source_flags:
        reason_parts.append('source=' + '+'.join(source_flags))
    if discovery_source_validation.get('status'):
        reason_parts.append(f"discovery={discovery_source_validation.get('status')}")

    return {
        'symbol': symbol,
        'name': base.get('name'),
        'chain_id': pair.get('chainId') or profile.get('chainId'),
        'dex_id': pair.get('dexId'),
        'token_address': token_address,
        'pair_address': pair.get('pairAddress'),
        'pair_url': pair.get('url') or profile.get('url'),
        'stage': stage,
        'early_score': score,
        'confidence': confidence,
        'source_flags': source_flags,
        'discovery_source_validation': discovery_source_validation,
        'price_usd': safe_float(pair.get('priceUsd')),
        'pair_age_hours': round(age_hours, 2) if age_hours is not None else None,
        'liquidity_usd': round(liquidity, 2),
        'fdv': round(fdv, 2) if fdv else None,
        'fdv_to_liquidity': round(fdv / liquidity, 2) if fdv and liquidity else None,
        'volume_m5': round(volume_m5, 2),
        'volume_h1': round(volume_h1, 2),
        'volume_h24': round(volume_h24, 2),
        'txns_m5': txns_m5,
        'txns_h1': txns_h1,
        'buys_h1': buys_h1,
        'sells_h1': sells_h1,
        'buy_sell_ratio_h1': round(buy_sell_ratio_h1, 2),
        'price_change_pct_m5': round(price_change_m5, 2),
        'price_change_pct_h1': round(price_change_h1, 2),
        'price_change_pct_h24': round(price_change_h24, 2),
        'social_link_count': social_count,
        'social_validation': social_validation,
        'boost_amount': boosted_amount or None,
        'boost_total_amount': boosted_total or None,
        'active_pair_boosts': pair_boosts or None,
        'boost_traction': boost_traction,
        'valuation_liquidity': valuation_liquidity,
        'microstructure': microstructure,
        'pair_landscape': pair_landscape,
        'risk_flags': risk_flags,
        'reason_summary': ', '.join(reason_parts) if reason_parts else 'early meme signal weak',
        'security_note': 'contract_security_not_checked',
    }


def merge_discovery_items(
    profiles: List[Dict[str, Any]],
    latest_boosts: List[Dict[str, Any]],
    top_boosts: List[Dict[str, Any]],
    extra_candidates: List[Dict[str, Any]],
    chains: set[str],
    limit: int,
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    def add_items(items: List[Dict[str, Any]], source_flag: str) -> None:
        for item in items:
            key = token_key(item)
            if not key:
                continue
            chain_id, _ = key.split(':', 1)
            if chains and chain_id not in chains:
                continue
            row = merged.setdefault(key, dict(item))
            row.setdefault('source_flags', [])
            if source_flag not in row['source_flags']:
                row['source_flags'].append(source_flag)
            for field in (
                'amount',
                'totalAmount',
                'description',
                'url',
                'icon',
                'header',
                'links',
                'symbol',
                'name',
                'external_source',
                'detected_at',
                'created_at',
            ):
                if item.get(field) not in (None, '', []):
                    row[field] = item.get(field)

    add_items(extra_candidates, 'external_candidate')
    add_items(profiles, 'latest_profile')
    add_items(latest_boosts, 'latest_boost')
    add_items(top_boosts, 'top_boost')

    rows = list(merged.values())
    rows.sort(key=lambda row: (
        'external_candidate' in row.get('source_flags', []),
        'latest_boost' in row.get('source_flags', []),
        'latest_profile' in row.get('source_flags', []),
        safe_float(row.get('totalAmount')),
    ), reverse=True)
    return rows[:limit]


def build_monitor_payload(
    limit: int = 40,
    top_n: int = 10,
    chains: Optional[List[str]] = None,
    max_age_hours: int = 72,
    min_liquidity_usd: float = 10_000.0,
    include_top_boosts: bool = True,
    extra_candidates: Optional[List[Dict[str, Any]]] = None,
    token_cooldown_hours: int = DEFAULT_TOKEN_COOLDOWN_HOURS,
    token_memory_db: Path | None = None,
    enable_security_check: bool = True,
    security_api_token: str | None = None,
    enable_research_edge: bool = True,
    research_edge_db: Path | None = None,
    research_edge_min_samples: int = 8,
) -> Dict[str, Any]:
    chain_set = {str(item).lower() for item in (chains or DEFAULT_CHAINS) if item}
    source_stats: Dict[str, Any] = {}
    normalized_extra_candidates = [
        candidate
        for candidate in (normalize_extra_candidate(item) for item in (extra_candidates or []))
        if candidate
    ]
    source_stats['extra_candidates'] = len(normalized_extra_candidates)

    try:
        profiles = fetch_latest_profiles()
    except Exception as exc:
        profiles = []
        source_stats['latest_profiles_error'] = str(exc)
    source_stats['latest_profiles'] = len(profiles)

    try:
        latest_boosts = fetch_latest_boosts()
    except Exception as exc:
        latest_boosts = []
        source_stats['latest_boosts_error'] = str(exc)
    source_stats['latest_boosts'] = len(latest_boosts)

    top_boosts: List[Dict[str, Any]] = []
    if include_top_boosts:
        try:
            top_boosts = fetch_top_boosts()
        except Exception as exc:
            source_stats['top_boosts_error'] = str(exc)
    source_stats['top_boosts'] = len(top_boosts)

    discovery = merge_discovery_items(
        profiles,
        latest_boosts,
        top_boosts,
        normalized_extra_candidates,
        chain_set,
        limit,
    )
    rows: List[Dict[str, Any]] = []
    pair_errors = 0
    security_errors = 0
    for item in discovery:
        chain_id = str(item.get('chainId') or '').strip().lower()
        token_address = str(item.get('tokenAddress') or '').strip()
        if not chain_id or not token_address:
            continue
        security: Dict[str, Any] | None = None
        if enable_security_check:
            try:
                security = fetch_goplus_security(chain_id, token_address, api_token=security_api_token)
            except Exception:
                security_errors += 1
        try:
            pairs = fetch_token_pairs(chain_id, token_address)
        except Exception:
            pair_errors += 1
            continue
        pair = choose_best_pair(pairs, token_address)
        if not pair:
            row = {
                'symbol': None,
                'chain_id': chain_id,
                'token_address': token_address,
                'stage': 'rug_risk',
                'early_score': 0,
                'confidence': 'low',
                'source_flags': item.get('source_flags') or [],
                'risk_flags': ['no_pairs'],
                'reason_summary': 'no DexScreener pairs returned',
                'security_note': 'contract_security_not_checked',
            }
            if enable_security_check:
                apply_security_to_row(row, security)
                if not security:
                    row['security'] = {'provider': 'goplus', 'status': 'unavailable'}
                    row['security_note'] = 'contract_security_unavailable'
            rows.append(row)
            continue
        pair_landscape = pair_landscape_snapshot(
            pairs,
            pair,
            min_liquidity_usd=min_liquidity_usd,
        )
        row = score_pair(
            item,
            pair,
            source_flags=item.get('source_flags') or [],
            max_age_hours=max_age_hours,
            min_liquidity_usd=min_liquidity_usd,
            pair_landscape=pair_landscape,
        )
        if enable_security_check:
            apply_security_to_row(row, security)
            if not security:
                row['security'] = {'provider': 'goplus', 'status': 'unavailable'}
                row['security_note'] = 'contract_security_unavailable'
            reclassify_scored_row(row, max_age_hours, min_liquidity_usd)
        rows.append(row)

    memory_stats = apply_token_memory(
        rows,
        db_path=token_memory_db,
        cooldown_hours=token_cooldown_hours,
        max_age_hours=max_age_hours,
        min_liquidity_usd=min_liquidity_usd,
    )
    creator_memory_stats = apply_creator_memory(
        rows,
        db_path=token_memory_db,
        enabled=token_cooldown_hours > 0,
        max_age_hours=max_age_hours,
        min_liquidity_usd=min_liquidity_usd,
    )
    meme_edge_report = (
        build_meme_edge_report(
            db_path=research_edge_db or DEFAULT_MEME_RESEARCH_DB,
            min_samples=research_edge_min_samples,
        )
        if enable_research_edge
        else {'enabled': False, 'available': False, 'reason': 'disabled'}
    )
    research_edge_stats = apply_local_meme_edge(
        rows,
        meme_edge_report,
        max_age_hours=max_age_hours,
        min_liquidity_usd=min_liquidity_usd,
    )

    rows.sort(key=lambda row: (
        row.get('stage') == 'early_watch',
        row.get('stage') == 'momentum_watch',
        row.get('early_score') or 0,
        row.get('liquidity_usd') or 0,
    ), reverse=True)
    for idx, row in enumerate(rows, start=1):
        row['rank'] = idx

    early = [row for row in rows if row.get('stage') == 'early_watch']
    momentum = [row for row in rows if row.get('stage') == 'momentum_watch']
    watchlist = [row for row in rows if row.get('stage') == 'watchlist']
    risk = [row for row in rows if row.get('stage') == 'rug_risk']
    overheated = [row for row in rows if row.get('stage') == 'overheated']

    return {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'meme_early_radar_v1',
            'limit': limit,
            'top_n': top_n,
            'chains': sorted(chain_set),
            'max_age_hours': max_age_hours,
            'min_liquidity_usd': min_liquidity_usd,
            'include_top_boosts': include_top_boosts,
            'extra_candidates': len(normalized_extra_candidates),
            'token_cooldown_hours': token_cooldown_hours,
            'token_memory_db': str(token_memory_db or DEFAULT_TOKEN_MEMORY_DB),
            'security_check_enabled': enable_security_check,
            'research_edge_enabled': enable_research_edge,
            'research_edge_db': str(research_edge_db or DEFAULT_MEME_RESEARCH_DB),
            'research_edge_min_samples': research_edge_min_samples,
        },
        'source_stats': {
            **source_stats,
            'discovered_tokens': len(discovery),
            'pair_errors': pair_errors,
            'pairs_scored': len(rows),
            'token_memory': memory_stats,
            'creator_memory': creator_memory_stats,
            'security_check': {
                **security_risk_summary(rows),
                'errors': security_errors,
                'enabled': enable_security_check,
            },
            'research_edge': research_edge_stats,
        },
        'summary': {
            'pairs_scored': len(rows),
            'early_watch': len(early),
            'momentum_watch': len(momentum),
            'watchlist': len(watchlist),
            'rug_risk': len(risk),
            'overheated': len(overheated),
        },
        'early_watch': early[:top_n],
        'momentum_watch': momentum[:top_n],
        'watchlist': watchlist[:top_n],
        'rug_risk': risk[:top_n],
        'overheated': overheated[:top_n],
        'all_candidates': rows[:max(top_n, 30)],
    }


def render_markdown(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append('# Meme Early Radar')
    lines.append('')
    summary = payload.get('summary') or {}
    lines.append(f"- pairs_scored: **{summary.get('pairs_scored', 0)}**")
    lines.append(f"- early_watch: **{summary.get('early_watch', 0)}**")
    lines.append(f"- momentum_watch: **{summary.get('momentum_watch', 0)}**")
    lines.append(f"- rug_risk: **{summary.get('rug_risk', 0)}**")
    lines.append(f"- overheated: **{summary.get('overheated', 0)}**")
    token_memory = (payload.get('source_stats') or {}).get('token_memory') or {}
    if token_memory:
        lines.append(
            f"- token_memory: new **{token_memory.get('new_tokens', 0)}** / "
            f"repeat **{token_memory.get('repeat_tokens', 0)}** / "
            f"expired **{token_memory.get('cooldown_expired_tokens', 0)}** / "
            f"accelerated **{token_memory.get('accelerated_repeat_tokens', 0)}** / "
            f"fading **{token_memory.get('fading_repeat_tokens', 0)}**"
        )
    creator_memory = (payload.get('source_stats') or {}).get('creator_memory') or {}
    if creator_memory:
        lines.append(
            f"- creator_memory: matched **{creator_memory.get('matched_histories', 0)}** / "
            f"risky **{creator_memory.get('risky_histories', 0)}** / "
            f"adjusted **{creator_memory.get('adjusted_tokens', 0)}** / "
            f"with_party **{creator_memory.get('tokens_with_party', 0)}**"
        )
    security_stats = (payload.get('source_stats') or {}).get('security_check') or {}
    if security_stats:
        lines.append(
            f"- security_check: checked **{security_stats.get('checked', 0)}** / "
            f"risk **{security_stats.get('risk_tokens', 0)}** / "
            f"unavailable **{security_stats.get('unavailable', 0)}** / "
            f"errors **{security_stats.get('errors', 0)}**"
        )
    research_edge = (payload.get('source_stats') or {}).get('research_edge') or {}
    if research_edge:
        lines.append(
            f"- research_edge: adjusted **{research_edge.get('adjusted_tokens', 0)}** / "
            f"weak **{research_edge.get('weak_edges', 0)}** / "
            f"strong **{research_edge.get('strong_edges', 0)}** / "
            f"samples **{research_edge.get('usable_return_samples', 0)}**"
        )
    lines.append('')

    for section, title in (
        ('early_watch', 'Early Watch'),
        ('momentum_watch', 'Momentum Watch'),
        ('watchlist', 'Watchlist'),
        ('rug_risk', 'Risk Watch'),
        ('overheated', 'Overheated'),
    ):
        rows = payload.get(section) or []
        if not rows:
            continue
        lines.append(f'## {title}')
        lines.append('')
        for row in rows:
            lines.append(f"### {row.get('rank')}. {row.get('symbol') or '?'} [{row.get('chain_id')} / {row.get('dex_id')}]")
            lines.append(f"- score: `{row.get('early_score')}` | confidence: `{row.get('confidence')}` | stage: `{row.get('stage')}`")
            local_edge = row.get('local_research_edge') or {}
            if local_edge.get('applied'):
                lines.append(
                f"- local_research_edge: `{local_edge.get('edge')}` | "
                f"adjustment `{local_edge.get('adjustment')}` | "
                f"cap `{local_edge.get('confidence_cap')}` | "
                f"samples `{local_edge.get('usable_return_samples')}`"
            )
            active_edges = local_edge.get('active_edges') or []
            if active_edges:
                first_edge = active_edges[0]
                lines.append(
                    f"- edge_liquidity: retention `{first_edge.get('avg_liquidity_retention_pct')}` | "
                    f"drain `{first_edge.get('liquidity_drain_rate_pct')}` | "
                    f"severe_drain `{first_edge.get('severe_liquidity_drain_rate_pct')}`"
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
            memory_delta = row.get('memory_delta') or {}
            if memory_delta:
                lines.append(
                    f"- memory_delta: liq `{memory_delta.get('liquidity_change_since_seen_pct')}%` | "
                    f"vol_1h `{memory_delta.get('volume_h1_change_since_seen_pct')}%` | "
                    f"txns_1h `{memory_delta.get('txns_h1_change_since_seen_pct')}%` | "
                    f"flags `{', '.join(memory_delta.get('flags') or [])}`"
                )
            creator_history = row.get('creator_memory') or []
            if creator_history:
                first_history = creator_history[0]
                lines.append(
                    f"- creator_memory: `{first_history.get('party_type')}` | "
                    f"tokens `{first_history.get('token_count')}` | "
                    f"rug `{first_history.get('rug_stage_count')}` | "
                    f"risk `{first_history.get('risk_token_count')}`"
                )
            lines.append(f"- age: `{row.get('pair_age_hours')}`h | liq: `${row.get('liquidity_usd')}` | txns_1h: `{row.get('txns_h1')}` | buy/sell: `{row.get('buy_sell_ratio_h1')}`")
            lines.append(f"- volume: m5 `${row.get('volume_m5')}` / h1 `${row.get('volume_h1')}` | h1 change `{row.get('price_change_pct_h1')}%`")
            lines.append(f"- source: `{', '.join(row.get('source_flags') or [])}` | socials: `{row.get('social_link_count')}`")
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
                    f"m5_share `{microstructure.get('m5_volume_share_h1')}` | "
                    f"avg_trade_1h `${microstructure.get('avg_trade_h1')}`"
                )
            pair_landscape = row.get('pair_landscape') or {}
            if pair_landscape:
                lines.append(
                    f"- pair_landscape: `{pair_landscape.get('status')}` | "
                    f"pairs `{pair_landscape.get('active_pair_count')}` | "
                    f"liq_share `{pair_landscape.get('selected_liquidity_share')}` | "
                    f"vol_share `{pair_landscape.get('selected_volume_share_h1')}`"
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
            lines.append(f"- reason: {row.get('reason_summary')}")
            lines.append(f"- security: `{row.get('security_note')}`")
            if row.get('pair_url'):
                lines.append(f"- link: <{row.get('pair_url')}>")
            lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


def save_payload(payload: Dict[str, Any], output: Optional[str]) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'meme_early_radar_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    path.with_suffix('.md').write_text(render_markdown(payload), encoding='utf-8')
    return str(path)


def parse_chain_list(value: str) -> List[str]:
    return [item.strip().lower() for item in str(value or '').split(',') if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description='一级 MEME 早期雷达')
    parser.add_argument('--limit', type=int, default=40)
    parser.add_argument('--top-n', type=int, default=10)
    parser.add_argument('--chains', default='solana,base,bsc,ethereum')
    parser.add_argument('--max-age-hours', type=int, default=72)
    parser.add_argument('--min-liquidity-usd', type=float, default=10_000)
    parser.add_argument('--token-cooldown-hours', type=int, default=DEFAULT_TOKEN_COOLDOWN_HOURS)
    parser.add_argument('--token-memory-db', default=str(DEFAULT_TOKEN_MEMORY_DB))
    parser.add_argument('--disable-security-check', action='store_true')
    parser.add_argument('--goplus-api-token', default=None)
    parser.add_argument('--record-meme-signals', action='store_true')
    parser.add_argument('--meme-research-db', default=str(DEFAULT_MEME_RESEARCH_DB))
    parser.add_argument('--disable-research-edge', action='store_true')
    parser.add_argument('--meme-edge-min-samples', type=int, default=8)
    parser.add_argument('--evaluate-meme-signals', action='store_true')
    parser.add_argument('--meme-evaluation-windows', default='1,4,24')
    parser.add_argument('--meme-evaluation-max-signals', type=int, default=50)
    parser.add_argument('--force-meme-evaluation', action='store_true')
    parser.add_argument('--meme-research-report', action='store_true')
    parser.add_argument('--meme-research-min-samples', type=int, default=1)
    parser.add_argument('--meme-research-top-n', type=int, default=20)
    parser.add_argument('--no-top-boosts', action='store_true')
    parser.add_argument('--extra-candidates', action='append', default=[])
    parser.add_argument('--output')
    args = parser.parse_args()

    if args.meme_research_report:
        print(json.dumps(
            summarize_meme_research(
                Path(args.meme_research_db),
                min_samples=args.meme_research_min_samples,
                top_n=args.meme_research_top_n,
            ),
            ensure_ascii=False,
            indent=2,
        ))
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

    extra_candidates: List[Dict[str, Any]] = []
    extra_candidate_stats: Dict[str, Any] = {}
    if args.extra_candidates:
        extra_candidates, extra_candidate_stats = load_meme_extra_candidate_files(args.extra_candidates)

    payload = build_monitor_payload(
        limit=args.limit,
        top_n=args.top_n,
        chains=parse_chain_list(args.chains),
        max_age_hours=args.max_age_hours,
        min_liquidity_usd=args.min_liquidity_usd,
        include_top_boosts=not args.no_top_boosts,
        extra_candidates=extra_candidates,
        token_cooldown_hours=args.token_cooldown_hours,
        token_memory_db=Path(args.token_memory_db),
        enable_security_check=not args.disable_security_check,
        security_api_token=args.goplus_api_token,
        enable_research_edge=not args.disable_research_edge,
        research_edge_db=Path(args.meme_research_db),
        research_edge_min_samples=args.meme_edge_min_samples,
    )
    if extra_candidate_stats:
        payload.setdefault('source_stats', {})['extra_candidate_files'] = extra_candidate_stats
    if args.record_meme_signals:
        payload['meme_research'] = {
            'recording': record_meme_signals(payload, db_path=Path(args.meme_research_db)),
        }
    out = save_payload(payload, args.output)
    print(out)
    print(json.dumps({
        'summary': payload['summary'],
        'meme_research': payload.get('meme_research'),
        'early_watch': payload['early_watch'][:5],
        'momentum_watch': payload['momentum_watch'][:5],
        'rug_risk': payload['rug_risk'][:5],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
