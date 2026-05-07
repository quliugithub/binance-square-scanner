#!/usr/bin/env python3
"""Major Coin Futures Monitor — 资金费率 + OI + 基差 + 多空比综合监控"""

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from bsc.constants import RESULTS_DIR


BASE_URL = 'https://fapi.binance.com'
TIMEOUT = 15
DEFAULT_SYMBOLS = ['BTCUSDT', 'ETHUSDT']
PERIODS = ['5m', '15m', '1h', '4h', '1d']


# ── helpers ──────────────────────────────────────────────────────

def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


# ── API fetchers ─────────────────────────────────────────────────

def fetch_funding_rate(symbol: str, limit: int = 30) -> List[Dict]:
    """最近 N 次资金费率记录"""
    url = f'{BASE_URL}/fapi/v1/fundingRate'
    resp = requests.get(url, params={'symbol': symbol, 'limit': limit}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_premium_index(symbol: str) -> Dict:
    """当前基差 / 标记价格 / 下次资金费率时间"""
    url = f'{BASE_URL}/fapi/v1/premiumIndex'
    resp = requests.get(url, params={'symbol': symbol}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_oi_history(symbol: str, period: str = '5m', limit: int = 48) -> List[Dict]:
    """OI 历史"""
    url = f'{BASE_URL}/futures/data/openInterestHist'
    resp = requests.get(url, params={'symbol': symbol, 'period': period, 'limit': limit}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_top_long_short_ratio(symbol: str, period: str = '5m', limit: int = 48) -> List[Dict]:
    """大户多空比"""
    url = f'{BASE_URL}/futures/data/topLongShortPositionRatio'
    resp = requests.get(url, params={'symbol': symbol, 'period': period, 'limit': limit}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_global_long_short_ratio(symbol: str, period: str = '5m', limit: int = 48) -> List[Dict]:
    """全局多空比"""
    url = f'{BASE_URL}/futures/data/globalLongShortAccountRatio'
    resp = requests.get(url, params={'symbol': symbol, 'period': period, 'limit': limit}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_force_orders(symbol: str, limit: int = 100) -> List[Dict]:
    """清算数据（可能在维护中）"""
    url = f'{BASE_URL}/fapi/v1/allForceOrders'
    try:
        resp = requests.get(url, params={'symbol': symbol, 'limit': limit}, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


# ── factor calculators ───────────────────────────────────────────

def calc_funding_signal(history: List[Dict]) -> Dict:
    """资金费率因子"""
    if not history:
        return {'funding_rate': 0, 'avg_30d': 0, 'z_score': 0, 'signal': 'unknown'}

    rates = [safe_float(h.get('fundingRate')) for h in history]
    current = rates[-1] if rates else 0
    avg = statistics.mean(rates) if rates else 0
    stdev = statistics.stdev(rates) if len(rates) > 1 else 1e-8
    z = (current - avg) / stdev if stdev > 0 else 0

    # 判断连续方向
    consecutive_positive = 0
    consecutive_negative = 0
    for r in reversed(rates):
        if r > 0:
            consecutive_positive += 1
        elif r < 0:
            consecutive_negative += 1
        else:
            break

    # 信号判断
    if z > 2.0:
        signal = 'crowding_long'  # 多头拥挤
    elif z < -2.0:
        signal = 'crowding_short'  # 空头拥挤
    elif current > 0 and consecutive_positive >= 6:
        signal = 'sustained_positive'
    elif current < 0 and consecutive_negative >= 6:
        signal = 'sustained_negative'
    elif abs(z) < 0.5:
        signal = 'normal'
    else:
        signal = 'mild_deviation'

    return {
        'funding_rate': current,
        'funding_rate_pct': f'{current * 100:.4f}%',
        'avg_30d': avg,
        'avg_30d_pct': f'{avg * 100:.4f}%',
        'z_score': round(z, 2),
        'consecutive_positive': consecutive_positive,
        'consecutive_negative': consecutive_negative,
        'signal': signal,
    }


def calc_oi_signal(history: List[Dict]) -> Dict:
    """OI 因子"""
    if not history or len(history) < 2:
        return {'oi': 0, 'oi_value': 0, 'signal': 'unknown'}

    latest = history[-1]
    oi = safe_float(latest.get('sumOpenInterest'))
    oi_value = safe_float(latest.get('sumOpenInterestValue'))

    def oi_change(n_periods: int) -> float:
        if len(history) <= n_periods:
            return 0
        old = safe_float(history[-n_periods - 1].get('sumOpenInterest'))
        return pct_change(old, oi)

    def oi_value_change(n_periods: int) -> float:
        if len(history) <= n_periods:
            return 0
        old = safe_float(history[-n_periods - 1].get('sumOpenInterestValue'))
        return pct_change(old, oi_value)

    chg_1h = oi_change(12) if len(history) >= 13 else 0    # 12 * 5m = 1h
    chg_4h = oi_change(48) if len(history) >= 49 else 0    # 48 * 5m = 4h

    # OI 绝对变化趋势（近 12 个 5m）
    recent_oi = [safe_float(h.get('sumOpenInterest')) for h in history[-12:]]
    if len(recent_oi) >= 2:
        trend = 'rising' if recent_oi[-1] > recent_oi[0] * 1.005 else ('falling' if recent_oi[-1] < recent_oi[0] * 0.995 else 'stable')
    else:
        trend = 'stable'

    if chg_1h > 3:
        signal = 'surge'
    elif chg_1h < -3:
        signal = 'crash'
    elif chg_1h > 1:
        signal = 'inflow'
    elif chg_1h < -1:
        signal = 'outflow'
    else:
        signal = 'stable'

    return {
        'oi': round(oi, 2),
        'oi_value_usd': round(oi_value, 2),
        'oi_change_pct_1h': round(chg_1h, 2),
        'oi_value_change_pct_1h': round(oi_value_change(12) if len(history) >= 13 else 0, 2),
        'oi_trend': trend,
        'signal': signal,
    }


def calc_basis_signal(premium: Dict) -> Dict:
    """基差因子"""
    mark = safe_float(premium.get('markPrice'))
    index = safe_float(premium.get('indexPrice'))
    funding = safe_float(premium.get('lastFundingRate'))

    if index == 0:
        basis_pct = 0
    else:
        basis_pct = (mark - index) / index * 100

    if basis_pct > 0.1:
        signal = 'contango'
    elif basis_pct < -0.1:
        signal = 'backwardation'
    else:
        signal = 'neutral'

    return {
        'mark_price': mark,
        'index_price': index,
        'basis_pct': round(basis_pct, 4),
        'last_funding_rate': funding,
        'signal': signal,
    }


def calc_long_short_signal(top_data: List[Dict], global_data: List[Dict]) -> Dict:
    """多空比因子"""
    top_ratio = safe_float(top_data[-1].get('longShortRatio')) if top_data else 0
    global_ratio = safe_float(global_data[-1].get('longShortRatio')) if global_data else 0
    top_long_pct = safe_float(top_data[-1].get('longAccount')) * 100 if top_data else 0
    global_long_pct = safe_float(global_data[-1].get('longAccount')) * 100 if global_data else 0

    if top_ratio > 2.0:
        top_signal = 'long_crowded'
    elif top_ratio < 0.5:
        top_signal = 'short_crowded'
    else:
        top_signal = 'balanced'

    if global_ratio > 2.0:
        global_signal = 'long_crowded'
    elif global_ratio < 0.5:
        global_signal = 'short_crowded'
    else:
        global_signal = 'balanced'

    # 综合信号
    if top_signal == 'short_crowded' and global_signal == 'short_crowded':
        combined = 'extreme_short_crowd'  # 极端空头拥挤 → 反弹机会
    elif top_signal == 'long_crowded' and global_signal == 'long_crowded':
        combined = 'extreme_long_crowd'   # 极端多头拥挤 → 反转风险
    elif top_ratio < 0.7 and global_ratio < 0.7:
        combined = 'leaning_short'
    elif top_ratio > 1.3 and global_ratio > 1.3:
        combined = 'leaning_long'
    else:
        combined = 'balanced'

    return {
        'top_trader_long_ratio': round(top_ratio, 4),
        'top_trader_long_pct': round(top_long_pct, 1),
        'global_long_ratio': round(global_ratio, 4),
        'global_long_pct': round(global_long_pct, 1),
        'top_signal': top_signal,
        'global_signal': global_signal,
        'signal': combined,
    }


def calc_liquidation_signal(force_orders: List[Dict]) -> Dict:
    """清算因子"""
    if not force_orders:
        return {
            'available': False,
            'total_24h_usd': 0,
            'long_pct': 0,
            'signal': 'unavailable',
        }

    total_qty = sum(safe_float(o.get('executedQty')) for o in force_orders)
    long_qty = sum(safe_float(o.get('executedQty')) for o in force_orders if o.get('side') == 'SELL')
    long_pct = (long_qty / total_qty * 100) if total_qty > 0 else 0

    if long_pct > 80:
        signal = 'long_cascade_risk'
    elif long_pct < 20:
        signal = 'short_cascade_risk'
    else:
        signal = 'normal'

    return {
        'available': True,
        'total_orders': len(force_orders),
        'long_pct': round(long_pct, 1),
        'signal': signal,
    }


# ── composite scoring ────────────────────────────────────────────

def build_capital_flow_score(funding: Dict, oi: Dict, basis: Dict, ls: Dict, liq: Dict) -> Dict:
    """综合资金面评分 0-100，50=中性"""
    score = 50.0
    drivers = []
    risks = []

    # 资金费率
    z = funding.get('z_score', 0)
    fs = funding.get('signal', 'normal')
    if fs == 'crowding_long':
        score -= 15
        drivers.append(f'资金费率极端偏正 (z={z})')
        risks.append('多头拥挤，反转风险')
    elif fs == 'crowding_short':
        score += 15
        drivers.append(f'资金费率极端偏负 (z={z})')
    elif fs == 'sustained_positive':
        score -= 5
        drivers.append(f'资金费率持续为正 ({funding.get("consecutive_positive")}期)')
    elif fs == 'sustained_negative':
        score += 5
        drivers.append(f'资金费率持续为负 ({funding.get("consecutive_negative")}期)')

    # OI
    oi_chg = oi.get('oi_change_pct_1h', 0)
    os = oi.get('signal', 'stable')
    if os == 'surge':
        score += 10
        drivers.append(f'OI 1h +{oi_chg:.2f}%')
    elif os == 'crash':
        score -= 10
        drivers.append(f'OI 1h {oi_chg:.2f}%')
        risks.append('持仓量大幅减少')
    elif os == 'inflow':
        score += 5
        drivers.append(f'OI 温和流入 +{oi_chg:.2f}%')
    elif os == 'outflow':
        score -= 5

    # 基差
    bs = basis.get('signal', 'neutral')
    bp = basis.get('basis_pct', 0)
    if bs == 'contango':
        score -= 3
        drivers.append(f'正基差 {bp:.4f}%')
    elif bs == 'backwardation':
        score += 8
        drivers.append(f'负基差 {bp:.4f}%（反常）')

    # 多空比
    lss = ls.get('signal', 'balanced')
    if lss == 'extreme_short_crowd':
        score += 15
        drivers.append('大户+散户极端空头拥挤')
    elif lss == 'extreme_long_crowd':
        score -= 15
        drivers.append('大户+散户极端多头拥挤')
        risks.append('多头拥挤度极高')
    elif lss == 'leaning_short':
        score += 8
        drivers.append('多空比偏空')
    elif lss == 'leaning_long':
        score -= 8

    # 清算
    if liq.get('available'):
        lss2 = liq.get('signal', 'normal')
        if lss2 == 'long_cascade_risk':
            score -= 10
            drivers.append(f'多头清算占比 {liq.get("long_pct", 0)}%')
            risks.append('级联清算风险')
        elif lss2 == 'short_cascade_risk':
            score += 10
            drivers.append(f'空头清算占比 {100 - liq.get("long_pct", 100):.0f}%')

    score = clamp(score)

    if score >= 70:
        bias = 'bullish'
        confidence = 'high' if score >= 80 else 'medium'
    elif score <= 30:
        bias = 'bearish'
        confidence = 'high' if score <= 20 else 'medium'
    elif score >= 60:
        bias = 'bullish'
        confidence = 'low'
    elif score <= 40:
        bias = 'bearish'
        confidence = 'low'
    else:
        bias = 'neutral'
        confidence = 'low'

    # 操作建议
    if bias == 'bullish' and not risks:
        suggestion = '资金面偏多，可关注做多机会'
    elif bias == 'bullish' and risks:
        suggestion = '资金面偏多但有风险因素，谨慎看多'
    elif bias == 'bearish' and not risks:
        suggestion = '资金面偏空，可关注做空机会'
    elif bias == 'bearish' and risks:
        suggestion = '资金面偏空且有下行风险，注意风控'
    else:
        suggestion = '资金面中性，等待更明确信号'

    return {
        'capital_flow_score': round(score, 1),
        'capital_flow_bias': bias,
        'confidence': confidence,
        'key_drivers': drivers,
        'risk_factors': risks,
        'action_suggestion': suggestion,
    }


# ── main pipeline ────────────────────────────────────────────────

def analyze_symbol(symbol: str) -> Dict:
    """对单个币种做完整资金面分析"""
    # 并行获取数据（串行但够快，Binance 公开 API 限速宽松）
    funding_hist = fetch_funding_rate(symbol, limit=30)
    premium = fetch_premium_index(symbol)
    oi_hist_5m = fetch_oi_history(symbol, period='5m', limit=48)
    oi_hist_1h = fetch_oi_history(symbol, period='1h', limit=48)
    top_ls = fetch_top_long_short_ratio(symbol, period='5m', limit=48)
    global_ls = fetch_global_long_short_ratio(symbol, period='5m', limit=48)
    force_orders = fetch_force_orders(symbol, limit=100)

    funding = calc_funding_signal(funding_hist)
    oi = calc_oi_signal(oi_hist_5m)
    oi_1h = calc_oi_signal(oi_hist_1h)
    basis = calc_basis_signal(premium)
    ls = calc_long_short_signal(top_ls, global_ls)
    liq = calc_liquidation_signal(force_orders)
    composite = build_capital_flow_score(funding, oi, basis, ls, liq)

    return {
        'symbol': symbol,
        'generated_at': utc_now_iso(),
        'funding': funding,
        'oi_5m': oi,
        'oi_1h': oi_1h,
        'basis': basis,
        'long_short': ls,
        'liquidation': liq,
        'composite': composite,
    }


def build_monitor_payload(symbols: Optional[List[str]] = None) -> Dict:
    """构建完整 payload"""
    symbols = symbols or DEFAULT_SYMBOLS
    results = []
    for sym in symbols:
        try:
            results.append(analyze_symbol(sym))
        except Exception as exc:
            results.append({'symbol': sym, 'error': str(exc)})

    return {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'major_coin_futures_monitor_v1',
            'symbols': symbols,
        },
        'results': results,
    }


# ── rendering ────────────────────────────────────────────────────

def render_markdown(payload: Dict) -> str:
    lines = ['# Major Coin Futures Monitor', '']

    for r in payload.get('results', []):
        sym = r.get('symbol', '?')
        if 'error' in r:
            lines.append(f'## {sym} ❌ Error')
            lines.append(f'- {r["error"]}')
            continue

        comp = r.get('composite', {})
        score = comp.get('capital_flow_score', 50)
        bias = comp.get('capital_flow_bias', 'neutral')
        conf = comp.get('confidence', 'low')

        emoji = '🟢' if bias == 'bullish' else ('🔴' if bias == 'bearish' else '⚪')
        lines.append(f'## {emoji} {sym}  —  Score: {score}/100  ({bias} / {conf})')
        lines.append('')

        # Funding
        f = r.get('funding', {})
        lines.append(f'**资金费率**: {f.get("funding_rate_pct")} (avg 30d: {f.get("avg_30d_pct")}, z={f.get("z_score")}) → `{f.get("signal")}`')

        # OI
        o5 = r.get('oi_5m', {})
        o1 = r.get('oi_1h', {})
        lines.append(f'**OI**: {o5.get("oi")} (${o5.get("oi_value_usd", 0)/1e9:.2f}B) | 1h 变化: {o5.get("oi_change_pct_1h")}% → `{o5.get("signal")}`')

        # Basis
        b = r.get('basis', {})
        lines.append(f'**基差**: {b.get("basis_pct")}% | mark={b.get("mark_price")} index={b.get("index_price")} → `{b.get("signal")}`')

        # Long/Short
        ls = r.get('long_short', {})
        lines.append(f'**多空比**: 大户={ls.get("top_trader_long_ratio")} 散户={ls.get("global_long_ratio")} → `{ls.get("signal")}`')

        # Liquidation
        liq = r.get('liquidation', {})
        if liq.get('available'):
            lines.append(f'**清算**: 多头占比 {liq.get("long_pct")}% → `{liq.get("signal")}`')
        else:
            lines.append('**清算**: 接口维护中')

        # Drivers
        if comp.get('key_drivers'):
            lines.append(f'**驱动**: {"; ".join(comp["key_drivers"])}')
        if comp.get('risk_factors'):
            lines.append(f'**风险**: {"; ".join(comp["risk_factors"])}')

        lines.append(f'**建议**: {comp.get("action_suggestion")}')
        lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


def save_payload(payload: Dict, output: Optional[str] = None) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'futures_monitor_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    md_path = path.with_suffix('.md')
    md_path.write_text(render_markdown(payload), encoding='utf-8')
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='Major Coin Futures Monitor')
    parser.add_argument('--symbols', nargs='+', default=DEFAULT_SYMBOLS)
    parser.add_argument('--output')
    args = parser.parse_args()

    payload = build_monitor_payload(symbols=args.symbols)
    out = save_payload(payload, args.output)
    print(out)
    print(render_markdown(payload))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
