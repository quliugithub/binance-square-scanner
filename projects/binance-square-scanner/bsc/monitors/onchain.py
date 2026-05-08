#!/usr/bin/env python3
import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from bsc.constants import RESULTS_DIR

try:
    from bsc.data.defillama import fetch_protocols, detect_tvl_changes
    HAS_DEFILLAMA = True
except ImportError:
    HAS_DEFILLAMA = False

DEX_SEARCH_API = 'https://api.dexscreener.com/latest/dex/search'
DEX_TRENDING_SEARCH_QUERIES = ['SIREN', 'ORDI', 'BANANAS31', 'INJ', 'SOL', 'DOGE', 'PEPE', 'TAO', 'FET', 'RAY', 'ONDO', 'AAVE', 'ETHFI', 'CAKE', 'WIF']


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def fetch_pairs_for_query(query: str) -> List[Dict[str, Any]]:
    resp = requests.get(DEX_SEARCH_API, params={'q': query}, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    pairs = payload.get('pairs') or []
    return pairs if isinstance(pairs, list) else []


def pair_quality_score(pair: Dict[str, Any]) -> float:
    liquidity = safe_float(((pair.get('liquidity') or {}).get('usd')))
    volume_h24 = safe_float(((pair.get('volume') or {}).get('h24')))
    txns_h24 = safe_float((((pair.get('txns') or {}).get('h24') or {}).get('buys'))) + safe_float((((pair.get('txns') or {}).get('h24') or {}).get('sells')))
    price_usd = safe_float(pair.get('priceUsd'))
    score = 0.0
    score += min(liquidity / 5000.0, 30)
    score += min(volume_h24 / 10000.0, 35)
    score += min(txns_h24 / 40.0, 20)
    if price_usd <= 0:
        score -= 50
    return score


def compute_pair_signal(pair: Dict[str, Any]) -> Dict[str, Any]:
    txns = pair.get('txns') or {}
    volume = pair.get('volume') or {}
    liquidity = pair.get('liquidity') or {}
    buys_m5 = int(safe_float((txns.get('m5') or {}).get('buys')))
    sells_m5 = int(safe_float((txns.get('m5') or {}).get('sells')))
    buys_h1 = int(safe_float((txns.get('h1') or {}).get('buys')))
    sells_h1 = int(safe_float((txns.get('h1') or {}).get('sells')))

    volume_m5 = safe_float(volume.get('m5'))
    volume_h1 = safe_float(volume.get('h1'))
    volume_h24 = safe_float(volume.get('h24'))
    liquidity_usd = safe_float(liquidity.get('usd'))

    price_change_m5 = safe_float((pair.get('priceChange') or {}).get('m5'))
    price_change_h1 = safe_float((pair.get('priceChange') or {}).get('h1'))
    price_change_h24 = safe_float((pair.get('priceChange') or {}).get('h24'))

    imbalance_m5 = buys_m5 - sells_m5
    imbalance_ratio_m5 = buys_m5 / max(sells_m5, 1)
    imbalance_h1 = buys_h1 - sells_h1
    imbalance_ratio_h1 = buys_h1 / max(sells_h1, 1)
    volume_spike_score = 0.0
    baseline_h1_per_5m = volume_h1 / 12.0 if volume_h1 > 0 else 0.0
    if baseline_h1_per_5m > 0:
        volume_spike_score = (volume_m5 / baseline_h1_per_5m)

    risk_flags: List[str] = []
    if liquidity_usd < 80_000:
        risk_flags.append('low_liquidity')
    if volume_h24 < 150_000:
        risk_flags.append('low_24h_volume')
    if buys_m5 + sells_m5 < 12:
        risk_flags.append('thin_m5_txns')
    if price_change_h24 > 80:
        risk_flags.append('overheated_24h')
    if price_change_m5 > 18:
        risk_flags.append('overheated_m5')

    buy_score = 0.0
    buy_score += min(max(imbalance_m5, 0) * 1.2, 18)
    buy_score += min(max(imbalance_h1, 0) * 0.45, 16)
    buy_score += min(max(imbalance_ratio_m5 - 1.0, 0.0) * 12, 18)
    buy_score += min(max(volume_spike_score - 1.0, 0.0) * 10, 18)
    buy_score += min(liquidity_usd / 20000.0, 12)
    buy_score += min(volume_h24 / 100000.0, 10)
    if 0 <= price_change_h1 <= 20:
        buy_score += 8
    elif price_change_h1 > 30:
        buy_score -= 10
    if 'low_liquidity' in risk_flags:
        buy_score -= 18
    if 'thin_m5_txns' in risk_flags:
        buy_score -= 10
    if 'overheated_24h' in risk_flags or 'overheated_m5' in risk_flags:
        buy_score -= 14
    buy_score = clamp(buy_score)

    if buy_score >= 52 and not any(flag in risk_flags for flag in ['low_liquidity', 'thin_m5_txns']):
        stage = '强买盘候选'
    elif buy_score >= 32:
        stage = '观察池'
    elif 'overheated_24h' in risk_flags or 'overheated_m5' in risk_flags:
        stage = '末端过热'
    else:
        stage = '低质量噪音'

    reason_parts = []
    if imbalance_m5 > 0:
        reason_parts.append(f'm5 买卖差 {imbalance_m5}')
    if volume_spike_score > 1.3:
        reason_parts.append(f'm5 放量 {volume_spike_score:.2f}x')
    if liquidity_usd >= 100_000:
        reason_parts.append(f'流动性 {liquidity_usd:.0f} USD')
    if price_change_h1 > 0:
        reason_parts.append(f'1h 涨幅 {price_change_h1:.2f}%')
    if not reason_parts:
        reason_parts.append('买盘信号一般')

    return {
        'symbol': ((pair.get('baseToken') or {}).get('symbol')),
        'chain_id': pair.get('chainId'),
        'dex_id': pair.get('dexId'),
        'pair_address': pair.get('pairAddress'),
        'pair_url': pair.get('url'),
        'price_usd': safe_float(pair.get('priceUsd')),
        'price_change_pct_m5': round(price_change_m5, 2),
        'price_change_pct_h1': round(price_change_h1, 2),
        'price_change_pct_h24': round(price_change_h24, 2),
        'volume_m5': round(volume_m5, 2),
        'volume_h1': round(volume_h1, 2),
        'volume_h24': round(volume_h24, 2),
        'liquidity_usd': round(liquidity_usd, 2),
        'txns_buys_m5': buys_m5,
        'txns_sells_m5': sells_m5,
        'txns_buys_h1': buys_h1,
        'txns_sells_h1': sells_h1,
        'buy_sell_imbalance': imbalance_m5,
        'buy_sell_ratio_m5': round(imbalance_ratio_m5, 2),
        'buy_sell_ratio_h1': round(imbalance_ratio_h1, 2),
        'volume_spike_score': round(volume_spike_score, 2),
        'onchain_buy_score': round(buy_score, 2),
        'risk_flags': risk_flags,
        'stage': stage,
        'reason_summary': '，'.join(reason_parts),
    }


# Symbol → DefiLlama protocol name mapping
_SYMBOL_TO_PROTOCOL = {
    'AAVE': ['aave'], 'UNI': ['uniswap'], 'LDO': ['lido'], 'MKR': ['maker', 'spark'],
    'CAKE': ['pancakeswap'], 'SOL': ['solana', 'marinade', 'jito', 'jupiter', 'raydium', 'orca', 'meteora'],
    'OP': ['optimism', 'velodrome'], 'ARB': ['arbitrum', 'gmx', 'camelot'],
    'DOGE': ['dogecoin'], 'PEPE': [], 'WIF': [], 'BONK': [],
    'RENDER': [], 'FET': ['fetch.ai'], 'TAO': [],
    'ONDO': ['ondo'], 'OM': ['mantra'], 'HNT': ['helium'], 'AKT': ['akash'],
    'GALA': [], 'IMX': ['immutable'], 'STRK': ['starknet'],
    'ETHFI': ['ether.fi'], 'JUP': ['jupiter'], 'RAY': ['raydium'],
    'BNB': ['venus', 'pancakeswap'], 'ORDI': [],
    'SUI': ['sui'], 'APT': ['aptos'], 'INJ': ['injective'],
    'LINK': ['chainlink'], 'DOT': ['polkadot'],
    'SIREN': ['siren'], 'BMX': ['bmx'], 'TIA': [],
    'RUNE': ['thorchain'], 'SEI': ['sei'],
}


def _build_tvl_index() -> Dict[str, Dict[str, Any]]:
    """Fetch DefiLlama TVL changes and build a protocol_name→tvl_info lookup."""
    if not HAS_DEFILLAMA:
        return {}
    try:
        protocols = fetch_protocols()
        changes = detect_tvl_changes(protocols, change_threshold_pct=5.0, min_tvl_usd=3_000_000, top_n=100)
        index: Dict[str, Dict[str, Any]] = {}
        for item in changes:
            name = (item.get('name') or '').lower()
            symbol = (item.get('symbol') or '').upper()
            if name:
                index[name] = item
            if symbol and symbol != '-':
                index[symbol.lower()] = item
        return index
    except Exception:
        return {}


def _match_tvl_signal(symbol: str, tvl_index: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find TVL data for a given token symbol via protocol mapping."""
    sym_upper = symbol.upper() if symbol else ''
    # Get protocol names for this symbol
    protocol_names = _SYMBOL_TO_PROTOCOL.get(sym_upper, [])
    if not protocol_names:
        # Fallback: try symbol.lower() directly
        sym_lower = sym_upper.lower()
        if sym_lower in tvl_index:
            return tvl_index[sym_lower]
        return None
    # Try each protocol name
    for pname in protocol_names:
        if pname in tvl_index:
            return tvl_index[pname]
    # Fuzzy match: check if any index key contains the protocol name
    for key, item in tvl_index.items():
        for pname in protocol_names:
            if pname in key:
                return item
    return None


def build_monitor_payload(limit: int, top_n: int) -> Dict[str, Any]:
    unique_queries = DEX_TRENDING_SEARCH_QUERIES[:limit]

    # Fetch DefiLlama TVL data
    tvl_index = _build_tvl_index()
    tvl_inflow_symbols = {sym for sym, info in tvl_index.items() if info.get('change_1d_pct', 0) > 0}

    rows: List[Dict[str, Any]] = []
    for query in unique_queries:
        try:
            pairs = fetch_pairs_for_query(query)
        except Exception:
            continue
        if not pairs:
            continue
        filtered_pairs = [pair for pair in pairs if safe_float(((pair.get('liquidity') or {}).get('usd'))) >= 50_000 and safe_float(((pair.get('volume') or {}).get('h24'))) >= 150_000]
        candidate_pairs = filtered_pairs or pairs
        best_pair = max(candidate_pairs, key=pair_quality_score)
        row = compute_pair_signal(best_pair)

        # ── DefiLlama TVL 联动 ──
        symbol = row.get('symbol') or ''
        tvl_match = _match_tvl_signal(symbol, tvl_index)
        if tvl_match:
            tvl_change_1d = tvl_match.get('change_1d_pct', 0) or 0
            tvl_change_7d = tvl_match.get('change_7d_pct', 0) or 0
            tvl_usd = tvl_match.get('tvl', 0) or 0
            row['tvl_usd'] = round(tvl_usd, 2)
            row['tvl_change_1d_pct'] = round(tvl_change_1d, 2)
            row['tvl_change_7d_pct'] = round(tvl_change_7d, 2) if tvl_change_7d else None
            row['tvl_category'] = tvl_match.get('category', '')
            row['tvl_chains'] = tvl_match.get('chains', [])[:3]

            # Boost buy score based on TVL inflow
            tvl_boost = 0
            if tvl_change_1d > 20:
                tvl_boost = 15
            elif tvl_change_1d > 10:
                tvl_boost = 10
            elif tvl_change_1d > 5:
                tvl_boost = 5
            elif tvl_change_1d < -20:
                tvl_boost = -12  # Large outflow = risk
            elif tvl_change_1d < -10:
                tvl_boost = -6

            if tvl_boost != 0:
                row['onchain_buy_score'] = clamp(row['onchain_buy_score'] + tvl_boost)
                row['tvl_boost'] = tvl_boost
                if tvl_boost > 0:
                    row['reason_summary'] += f'，TVL 流入 {tvl_change_1d:.1f}%'
                else:
                    row['reason_summary'] += f'，⚠️ TVL 流出 {tvl_change_1d:.1f}%'

            # Update stage based on TVL
            if tvl_change_1d > 15 and row['onchain_buy_score'] >= 45:
                row['stage'] = '强买盘候选'
        else:
            row['tvl_usd'] = None
            row['tvl_change_1d_pct'] = None

        rows.append(row)

    rows.sort(key=lambda x: (x['stage'] == '强买盘候选', x['onchain_buy_score'], x['volume_spike_score'], x['liquidity_usd']), reverse=True)
    for idx, row in enumerate(rows, start=1):
        row['rank'] = idx

    strong = [row for row in rows if row.get('stage') == '强买盘候选']
    observed = [row for row in rows if row.get('stage') == '观察池']

    return {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'onchain_strong_buy_monitor_v2_with_tvl',
            'profile_limit': limit,
            'top_n': top_n,
        },
        'summary': {
            'profile_queries': len(unique_queries),
            'pairs_scanned': len(rows),
            'tvl_protocols_matched': sum(1 for r in rows if r.get('tvl_usd') is not None),
            'strong_buy_candidates': len(strong),
            'observation_candidates': len(observed),
        },
        'strong_buy_candidates': strong[:top_n],
        'observation_candidates': observed[:top_n],
        'all_candidates': rows[: max(top_n, 30)],
    }


def render_markdown(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append('# On-chain Strong Buy Monitor')
    lines.append('')
    summary = payload.get('summary') or {}
    lines.append(f"- pairs_scanned: **{summary.get('pairs_scanned', 0)}**")
    lines.append(f"- strong_buy_candidates: **{summary.get('strong_buy_candidates', 0)}**")
    lines.append(f"- observation_candidates: **{summary.get('observation_candidates', 0)}**")
    lines.append('')
    lines.append('## Strong buy candidates')
    lines.append('')
    for row in payload.get('strong_buy_candidates') or []:
        lines.append(f"### {row.get('rank')}. {row.get('symbol')} [{row.get('chain_id')} / {row.get('dex_id')}]")
        lines.append(f"- buy_score: `{row.get('onchain_buy_score')}`")
        lines.append(f"- price: `{row.get('price_usd')}`")
        lines.append(f"- m5 imbalance: `{row.get('buy_sell_imbalance')}` | ratio `{row.get('buy_sell_ratio_m5')}`")
        lines.append(f"- volume spike: `{row.get('volume_spike_score')}`")
        lines.append(f"- liquidity: `{row.get('liquidity_usd')}`")
        lines.append(f"- reason: {row.get('reason_summary')}")
        if row.get('tvl_usd') is not None:
            tvl_b = row['tvl_usd'] / 1e9
            lines.append(f"- TVL: ${tvl_b:.3f}B | 24h: `{row.get('tvl_change_1d_pct', '?')}%` | 7d: `{row.get('tvl_change_7d_pct', '?')}%`")
        if row.get('risk_flags'):
            lines.append(f"- risk_flags: `{', '.join(row.get('risk_flags') or [])}`")
        if row.get('pair_url'):
            lines.append(f"- link: <{row.get('pair_url')}>")
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def save_payload(payload: Dict[str, Any], output: Optional[str]) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'onchain_strong_buy_monitor_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    path.with_suffix('.md').write_text(render_markdown(payload), encoding='utf-8')
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='链上强买盘监控模块 V1')
    parser.add_argument('--limit', type=int, default=40)
    parser.add_argument('--top-n', type=int, default=10)
    parser.add_argument('--output')
    args = parser.parse_args()

    payload = build_monitor_payload(limit=args.limit, top_n=args.top_n)
    out = save_payload(payload, args.output)
    print(out)
    print(json.dumps({
        'summary': payload['summary'],
        'strong_buy_candidates': payload['strong_buy_candidates'][:5],
        'observation_candidates': payload['observation_candidates'][:5],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
