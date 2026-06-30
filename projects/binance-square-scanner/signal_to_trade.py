#!/usr/bin/env python3
"""signal_to_trade — 将 Monitoring Hub 信号转化为交易建议。

流程：Hub 信号 → 多维评分 → 交易建议 → 用户确认 → 调用 coin-trade-executor

用法：
  python3 signal_to_trade.py              # 生成建议（不执行）
  python3 signal_to_trade.py --execute    # 生成建议并执行（需确认）
  python3 signal_to_trade.py --auto       # 自动执行高置信度信号（危险）
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bsc.constants import PROJECT_DIR, RESULTS_DIR

# ── 路径配置 ──────────────────────────────────────────────
DEFAULT_TRADE_EXECUTOR = "~/.openclaw/workspace/skills/coin-trade-executor/scripts/coin_trade_exec.py"
TRADE_EXECUTOR = Path(os.path.expanduser(
    os.environ.get('TRADE_EXECUTOR', DEFAULT_TRADE_EXECUTOR)
))

# ── 默认参数 ──────────────────────────────────────────────
DEFAULT_STAKE_USD = 50.0
DEFAULT_LEVERAGE = 2
MAX_LEVERAGE = 5
MIN_SCORE_TO_SUGGEST = 55
MIN_SCORE_TO_AUTO = 85

_NOISE_BASE_SYMBOLS = {
    'USD', 'USDT', 'USDC', 'FDUSD', 'TUSD', 'BUSD', 'DAI', 'USDP', 'PYUSD',
}


def _normalize_candidate_base_symbol_with_reason(value: Any) -> Tuple[Optional[str], Optional[str]]:
    if value is None:
        return None, 'empty'

    raw = str(value).strip().upper()
    if not raw:
        return None, 'empty'

    raw = ''.join(raw.split())
    while raw.startswith('$'):
        raw = raw[1:]

    if not raw:
        return None, 'empty'

    for suffix in (':USDT', '/USDT', '-USDT'):
        if raw.endswith(suffix):
            raw = raw[:-len(suffix)]
            break

    if raw.endswith('USDT'):
        raw = raw[:-4]

    if not raw:
        return None, 'empty'
    if raw in _NOISE_BASE_SYMBOLS:
        return None, 'noise_symbol'
    if not raw[0].isalpha():
        return None, 'non_alpha_start'
    if raw.isdigit():
        return None, 'numeric_symbol'
    if not raw.isalnum():
        return None, 'invalid_characters'

    return raw, None


def _normalize_candidate_base_symbol(value: Any) -> Optional[str]:
    symbol, _reason = _normalize_candidate_base_symbol_with_reason(value)
    return symbol


def _normalize_candidate_pair(value: Any) -> Optional[str]:
    symbol = _normalize_candidate_base_symbol(value)
    return f'{symbol}USDT' if symbol else None


def _run_monitoring_hub() -> Dict[str, Any]:
    """运行 Monitoring Hub 各模块，汇总信号。"""
    if str(PROJECT_DIR) not in sys.path:
        sys.path.insert(0, str(PROJECT_DIR))

    from bsc.data.tool import scrape_square
    from bsc.data.blockbeats import fetch_blockbeats_news, to_narrative_format as bb_fmt
    from bsc.data.defillama import fetch_protocols, detect_tvl_changes, to_narrative_format as dl_fmt
    from bsc.data.twitter_kol import fetch_all_news, to_narrative_format as news_fmt
    from bsc.monitors.narrative import (
        build_narrative_posts, aggregate_narratives, build_symbol_narrative_view,
        enrich_post_symbols,
    )
    from bsc.monitors.funding_rate import build_monitor_payload as build_futures_payload
    from bsc.monitors.onchain import build_monitor_payload as build_onchain_payload
    from bsc.data.hyperliquid import fetch_futures_signals as fetch_hyperliquid
    from bsc.data.technical import compute_technical_signals

    signals: Dict[str, Any] = {}
    source_status: Dict[str, Any] = {}

    # 1. Narrative
    all_raw = []
    narrative_source_stats: Dict[str, Any] = {}

    try:
        bs = scrape_square(limit=30)
        all_raw.extend(bs)
        narrative_source_stats['binance_square'] = len(bs)
    except Exception as exc:
        narrative_source_stats['binance_square_error'] = str(exc)

    try:
        bb = bb_fmt(fetch_blockbeats_news(25))
        enrich_post_symbols(bb)
        all_raw.extend(bb)
        narrative_source_stats['blockbeats'] = len(bb)
    except Exception as exc:
        narrative_source_stats['blockbeats_error'] = str(exc)

    try:
        protocols = fetch_protocols()
        tvl = dl_fmt(detect_tvl_changes(protocols, change_threshold_pct=15))
        all_raw.extend(tvl)
        narrative_source_stats['defillama'] = len(tvl)
        narrative_source_stats['defillama_protocols'] = len(protocols)
    except Exception as exc:
        narrative_source_stats['defillama_error'] = str(exc)

    try:
        news = news_fmt(fetch_all_news())
        enrich_post_symbols(news)
        all_raw.extend(news)
        narrative_source_stats['crypto_news'] = len(news)
    except Exception as exc:
        narrative_source_stats['crypto_news_error'] = str(exc)

    try:
        from bsc.data.twitter_6551 import fetch_kol_tweets, fetch_crypto_search, to_narrative_format as tw6551_fmt
        tw_kol = fetch_kol_tweets(max_profiles=5, tweets_per_profile=8)
        tw_search = fetch_crypto_search(min_likes=20, max_results=10)
        tw_posts = tw6551_fmt(tw_kol + tw_search)
        enrich_post_symbols(tw_posts)
        all_raw.extend(tw_posts)
        narrative_source_stats['twitter_6551'] = len(tw_posts)
    except Exception as exc:
        narrative_source_stats['twitter_6551_error'] = str(exc)

    narrative_posts = build_narrative_posts(all_raw)
    narrative_rank = aggregate_narratives(narrative_posts)
    symbol_view = build_symbol_narrative_view(narrative_posts)
    signals['narrative'] = {
        'rank': narrative_rank,
        'symbol_view': symbol_view,
        'total_posts': len(all_raw),
    }
    source_status['narrative'] = {
        'source_stats': narrative_source_stats,
        'successful_sources': len([k for k in narrative_source_stats if not k.endswith('_error')]),
        'failed_sources': len([k for k in narrative_source_stats if k.endswith('_error')]),
        'total_posts': len(all_raw),
        'tagged_posts': len(narrative_posts),
        'symbols': len(symbol_view),
    }

    # 2. Futures (Binance)
    futures_symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'OPUSDT']
    try:
        futures = build_futures_payload(symbols=futures_symbols)
        future_results = futures.get('results', [])
        success_results = [r for r in future_results if not r.get('error')]
        error_results = [r for r in future_results if r.get('error')]
        signals['futures'] = {r['symbol']: r for r in success_results}
        source_status['futures'] = {
            'requested_symbols': len(futures_symbols),
            'successful_symbols': len(success_results),
            'failed_symbols': len(error_results),
            'failed_symbol_errors': {
                r.get('symbol', f'unknown_{idx}'): str(r.get('error'))
                for idx, r in enumerate(error_results, 1)
            },
        }
    except Exception as exc:
        signals['futures'] = {}
        source_status['futures'] = {
            'requested_symbols': len(futures_symbols),
            'successful_symbols': 0,
            'failed_symbols': len(futures_symbols),
            'error': str(exc),
        }

    # 2b. Hyperliquid DEX futures (free, 230+ pairs, fallback for missing Binance data)
    try:
        hyperliquid = fetch_hyperliquid()  # dict of {symbolUSDT: signal}
        signals['hyperliquid'] = hyperliquid
        source_status['hyperliquid'] = {
            'successful_symbols': len(hyperliquid),
        }
    except Exception as exc:
        signals['hyperliquid'] = {}
        source_status['hyperliquid'] = {
            'successful_symbols': 0,
            'error': str(exc),
        }

    # 3. On-chain
    try:
        onchain = build_onchain_payload(limit=15, top_n=10)
        signals['onchain'] = onchain
        source_status['onchain'] = {
            'strong_buy_candidates': len(onchain.get('strong_buy_candidates', [])),
            'observation_candidates': len(onchain.get('observation_candidates', [])),
            'all_candidates': len(onchain.get('all_candidates', [])),
        }
    except Exception as exc:
        signals['onchain'] = {}
        source_status['onchain'] = {
            'strong_buy_candidates': 0,
            'observation_candidates': 0,
            'all_candidates': 0,
            'error': str(exc),
        }

    # 4. Technical indicators (RSI/MA/MACD/ATR from Binance)
    tech_symbols = set()
    skipped_symbols: List[Dict[str, str]] = []
    skipped_symbol_reasons: Dict[str, int] = {}
    raw_symbol_count = 0

    def add_technical_candidate(raw_value: Any) -> None:
        nonlocal raw_symbol_count
        raw_symbol_count += 1
        sym, reason = _normalize_candidate_base_symbol_with_reason(raw_value)
        if sym:
            tech_symbols.add(sym)
            return
        reason = reason or 'unknown'
        skipped_symbol_reasons[reason] = skipped_symbol_reasons.get(reason, 0) + 1
        if len(skipped_symbols) < 20:
            skipped_symbols.append({'raw': str(raw_value), 'reason': reason})

    for sv in signals.get('narrative', {}).get('symbol_view', []):
        add_technical_candidate(sv.get('symbol'))

    default_tech_symbols = [
        'BTC', 'ETH', 'SOL', 'BNB', 'XRP',
        'DOGE', 'ADA', 'AVAX', 'LINK', 'DOT',
        'MATIC', 'ARB', 'OP', 'SUI', 'APT', 'TAO', 'AAVE', 'ONDO', 'FET',
        'ORDI', 'RENDER', 'INJ', 'WIF', 'PEPE', 'FLOKI', 'BONK', 'SEI',
        'STRK', 'PENDLE', 'JUP', 'PYTH', 'JTO',
    ]
    for sym in default_tech_symbols:
        add_technical_candidate(sym)

    signals['technical'] = {}
    technical_errors: Dict[str, str] = {}
    for sym in sorted(tech_symbols):
        try:
            sig = compute_technical_signals(sym)
            symbol_pair = _normalize_candidate_pair(sym) or f'{sym}USDT'
            if 'error' not in sig:
                signals['technical'][symbol_pair] = sig
            else:
                technical_errors[sym] = str(sig.get('error'))
        except Exception as exc:
            technical_errors[sym] = str(exc)
    source_status['technical'] = {
        'raw_symbol_count': raw_symbol_count,
        'requested_symbols': len(tech_symbols),
        'successful_symbols': len(signals['technical']),
        'failed_symbols': len(technical_errors),
        'failed_symbol_errors': dict(list(technical_errors.items())[:10]),
        'skipped_symbols': skipped_symbols,
        'skipped_symbol_reasons': skipped_symbol_reasons,
    }

    signals['_source_status'] = source_status
    return signals


def _compute_symbol_score(symbol: str, signals: Dict[str, Any]) -> Dict[str, Any]:
    """计算单个币种的综合评分和建议，含详细评分拆解。

    评分体系 (v3):
    - 基础分 50
    - 💰 合约资金面: 最高 ±20 (核心维度)
    - 📡 叙事热度: 最高 +12 (辅助维度，权重降低)
    - ⛓️ 链上信号: 最高 ±15
    - 📈 技术指标: 最高 ±12
    - ⚠️ 数据覆盖惩罚: -5 / -10
    - 每个维度含 sub_items 子项明细
    """
    score = 50
    reasons: List[str] = []
    risks: List[str] = []
    breakdown: Dict[str, Dict[str, Any]] = {}
    data_dimensions_found = 0

    sym_pair = symbol.upper() if symbol.endswith('USDT') else f"{symbol.upper()}USDT"
    sym_base = symbol.upper().replace('USDT', '')

    # ── 1. 💰 合约资金面 ──
    futures = signals.get('futures', {})
    ft = futures.get(sym_pair, {})
    hl = signals.get('hyperliquid', {})
    hl_ft = hl.get(sym_pair, {})
    has_futures_data = False
    fut_items = []

    if ft and not ft.get('error'):
        has_futures_data = True
        comp = ft.get('composite', {})
        funding = ft.get('funding', {})
        funding_rate = funding.get('funding_rate', 0)
        f_bias = comp.get('capital_flow_bias', 'neutral')
        f_score = comp.get('capital_flow_score', 50)
        oi = ft.get('oi_5m', {})
        oi_change = oi.get('oi_change_pct_1h', 0) or 0
        fr_pct = funding.get('funding_rate_pct', '?')
        fut_pts = 0

        fut_items.append(f"Binance 资金面: {f_bias} (评分{f_score}/100)")
        if f_bias == 'bullish':
            fut_pts += 10
            fut_items.append(f"  ↳ 资金面偏多: +10")
            reasons.append(f'资金面偏多 (score={f_score})')
        elif f_bias == 'bearish':
            fut_pts -= 10
            fut_items.append(f"  ↳ 资金面偏空: -10")
            risks.append(f'资金面偏空 (score={f_score})')

        if funding_rate is not None:
            if funding_rate < -0.0003:
                fut_pts += 5
                fut_items.append(f"  ↳ 负费率 {fr_pct}（空头付费给多头）: +5")
                reasons.append(f'负费率={fr_pct}')
            elif funding_rate > 0.001:
                fut_pts -= 5
                fut_items.append(f"  ↳ 正费率偏高 {fr_pct}: -5")
                risks.append(f'正费率偏高={fr_pct}')

        if oi_change > 2:
            fut_items.append(f"  ↳ OI 1h +{oi_change}%（资金流入）")
        elif oi_change < -2:
            fut_items.append(f"  ↳ OI 1h {oi_change}%（资金流出）")

        if fut_pts == 0:
            fut_items.append("  ↳ 资金面中性: 0")

        breakdown['💰 合约资金面'] = {'points': fut_pts, 'detail': ' | '.join(fut_items), 'sub_items': fut_items}
        score += fut_pts

    elif hl_ft:
        has_futures_data = True
        hl_funding = hl_ft.get('funding_rate', 0)
        hl_bias = hl_ft.get('bias', 'neutral')
        hl_premium = hl_ft.get('premium', 0)
        hl_vol = hl_ft.get('day_volume_usd', 0)
        hl_fund_sig = hl_ft.get('funding_signal', 'neutral')
        fut_pts = 0
        vol_m = hl_vol / 1e6

        fut_items.append(f"Hyperliquid funding={hl_ft.get('funding_rate_pct','?')}, 24h_vol=${vol_m:.0f}M")

        if hl_fund_sig == 'high_negative':
            fut_pts += 10
            fut_items.append(f"  ↳ 负费率（空头付费，多头占优）: +10")
            reasons.append(f'负费率 {hl_ft.get("funding_rate_pct")}')
        elif hl_fund_sig == 'negative':
            fut_pts += 5
            fut_items.append(f"  ↳ 负费率: +5")
        elif hl_fund_sig == 'high_positive':
            fut_pts -= 8
            fut_items.append(f"  ↳ 正费率过高（多头拥挤）: -8")
            risks.append(f'正费率过高={hl_ft.get("funding_rate_pct")}')

        if hl_premium < -0.005:
            fut_pts += 5
            fut_items.append(f"  ↳ 永续折价 {hl_premium*100:.2f}%（可能超卖）: +5")
        elif hl_premium > 0.005:
            fut_pts -= 3
            fut_items.append(f"  ↳ 永续溢价 {hl_premium*100:.2f}%: -3")

        if hl_vol > 500_000_000:
            fut_pts += 3
            fut_items.append(f"  ↳ 高流动性 ($500M+): +3")
        elif hl_vol < 5_000_000:
            fut_pts -= 2
            fut_items.append(f"  ↳ 低流动性 (<$5M): -2")
            risks.append('流动性不足')

        if hl_bias == 'bullish':
            fut_pts += 2
            fut_items.append(f"  ↳ DEX合约偏多: +2")
        elif hl_bias == 'bearish':
            fut_pts -= 2
            fut_items.append(f"  ↳ DEX合约偏空: -2")

        if fut_pts == 0:
            fut_items.append("  ↳ 合约面中性: 0")

        fut_pts = max(-15, min(20, fut_pts))
        breakdown['💰 合约资金面'] = {'points': fut_pts, 'detail': ' | '.join(fut_items), 'sub_items': fut_items}
        score += fut_pts
    else:
        breakdown['💰 合约资金面'] = {'points': -5, 'detail': '❌ 无合约数据（Binance+Hyperliquid均缺失）', 'sub_items': ['Binance: 无数据', 'Hyperliquid: 无数据', '  ↳ 数据缺失惩罚: -5']}
        score -= 5
        risks.append('合约数据缺失')

    if has_futures_data:
        data_dimensions_found += 1

    # ── 2. 📡 叙事热度 ──
    narrative = signals.get('narrative', {})
    symbol_view = narrative.get('symbol_view', [])
    sv = next((s for s in symbol_view if s.get('symbol') == sym_pair), None)
    narr_items = []

    if sv:
        data_dimensions_found += 1
        post_count = sv.get('post_count', 0)
        narratives = sv.get('narratives', [])
        stage = sv.get('dominant_stage', '')
        narr_pts = 0

        narr_items.append(f"帖子数量: {post_count}")
        if post_count >= 10:
            narr_pts += 8
            narr_items.append(f"  ↳ ≥10帖热度高: +10")
            reasons.append(f'📡 叙事热度高 ({post_count}帖, {",".join(narratives[:2])})')
        elif post_count >= 5:
            narr_pts += 4
            narr_items.append(f"  ↳ 5-9帖活跃: +5")
            reasons.append(f'叙事活跃 ({post_count}帖)')
        else:
            narr_items.append(f"  ↳ <5帖热度不足: 0")

        if narratives:
            narr_items.append(f"赛道: {', '.join(narratives[:2])}")

        narr_items.append(f"扩散阶段: {stage or '未知'}")
        if stage == '中期扩散':
            narr_pts += 2
            narr_items.append(f"  ↳ 中期扩散（有机会）: +2")
        elif stage == '末端过热':
            narr_pts -= 8
            narr_items.append(f"  ↳ 末端过热（危险）: -10")
            risks.append('叙事末端过热')

        breakdown['📡 叙事热度'] = {'points': narr_pts, 'detail': ' | '.join(narr_items), 'sub_items': narr_items}
        score += narr_pts
    else:
        breakdown['📡 叙事热度'] = {'points': 0, 'detail': '未出现在叙事监控中', 'sub_items': ['帖子数量: 0', '  ↳ <5帖热度不足: 0', '赛道: 无', '扩散阶段: 无']}

    # ── 3. 链上强买盘 ──
    onchain = signals.get('onchain', {})
    all_candidates = (onchain.get('strong_buy_candidates', []) +
                      onchain.get('observation_candidates', []) +
                      onchain.get('all_candidates', []))
    oc = next((c for c in all_candidates if (c.get('symbol') or '').upper() == sym_base), None)
    oc_items = []

    if oc:
        data_dimensions_found += 1
        buy_score = oc.get('onchain_buy_score', 0)
        oc_pts = 0

        oc_items.append(f"DEX买卖评分: {buy_score}/100")
        if buy_score >= 60:
            oc_pts += 12
            oc_items.append(f"  ↳ ≥60 买盘强劲: +12")
            reasons.append(f'链上强买盘 (score={buy_score})')
        elif buy_score >= 40:
            oc_pts += 5
            oc_items.append(f"  ↳ 40-59 买盘活跃: +5")
            reasons.append(f'链上买盘活跃 (score={buy_score})')
        else:
            oc_items.append(f"  ↳ <40 买盘不足: 0")

        tvl_change = oc.get('tvl_change_1d_pct')
        if tvl_change is not None:
            oc_items.append(f"TVL 24h变化: {tvl_change:+.1f}%")
            if tvl_change > 10:
                oc_pts += 5
                oc_items.append(f"  ↳ TVL大幅流入: +5")
                reasons.append(f'TVL流入 {tvl_change:.1f}%')
            elif tvl_change < -10:
                oc_pts -= 5
                oc_items.append(f"  ↳ TVL大幅流出: -5")

        whale_net = oc.get('whale_net_usd', 0)
        if whale_net and abs(whale_net) > 1_000_000:
            whale_m = whale_net / 1e6
            oc_items.append(f"巨鲸净流向: ${whale_m:+.0f}M")
            if whale_net > 10_000_000:
                oc_pts += 5
                oc_items.append(f"  ↳ 巨鲸净提币 >$10M: +5")
                reasons.append(f'巨鲸净提币 ${whale_m:.0f}M')
            elif whale_net < -10_000_000:
                oc_pts -= 5
                oc_items.append(f"  ↳ 巨鲸净充币 >$10M: -5")

        breakdown['⛓️ 链上信号'] = {'points': oc_pts, 'detail': ' | '.join(oc_items), 'sub_items': oc_items}
        score += oc_pts
    else:
        breakdown['⛓️ 链上信号'] = {'points': 0, 'detail': f'{sym_base}为主网币，DEX上无原生交易对', 'sub_items': [f'{sym_base}: DEX上无原生交易对（非DeFi代币）', '  ↳ 链上买盘评分: 无数据', '  ↳ TVL变化: 无数据', '  ↳ 巨鲸流向: 无数据']}

    # ── 4. 📈 技术指标 ──
    tech = signals.get('technical', {})
    tech_data = tech.get(sym_pair, {})
    tech_items = []

    if tech_data and not tech_data.get('error'):
        data_dimensions_found += 1
        indicators = tech_data.get('indicators', {})
        bullish = tech_data.get('bullish_pts', 0)
        bearish = tech_data.get('bearish_pts', 0)
        tech_bias = tech_data.get('bias', 'neutral')
        tech_pts = 0

        # RSI
        rsi = indicators.get('rsi_1h')
        if rsi is not None:
            if rsi < 30:
                tech_pts += 3
                tech_items.append(f"RSI={rsi:.1f} 超卖: +3")
                reasons.append(f'RSI超卖={rsi:.0f}')
            elif rsi < 40:
                tech_pts += 1
                tech_items.append(f"RSI={rsi:.1f} 偏弱: +1")
            elif rsi > 70:
                tech_pts -= 3
                tech_items.append(f"RSI={rsi:.1f} 超买: -3")
                risks.append(f'RSI超买={rsi:.0f}')
            elif rsi > 60:
                tech_pts -= 1
                tech_items.append(f"RSI={rsi:.1f} 偏强: -1")
            else:
                tech_items.append(f"RSI={rsi:.1f} 中性: 0")

        # MA
        ma7 = indicators.get('ma7')
        ma25 = indicators.get('ma25')
        ma99 = indicators.get('ma99')
        if ma7 and ma25:
            if ma7 > ma25:
                tech_pts += 2
                tech_items.append(f"MA7({ma7:.0f})>MA25({ma25:.0f}) 短期多头排列: +2")
            else:
                tech_pts -= 2
                tech_items.append(f"MA7({ma7:.0f})<MA25({ma25:.0f}) 短期空头排列: -2")
        if ma25 and ma99:
            if ma25 > ma99:
                tech_pts += 2
                tech_items.append(f"MA25>MA99 中期多头: +2")
            else:
                tech_pts -= 2
                tech_items.append(f"MA25<MA99 中期空头: -2")

        # MACD
        macd_h = indicators.get('macd_histogram')
        macd_val = indicators.get('macd')
        macd_sig = indicators.get('macd_signal')
        if macd_h is not None and macd_val is not None and macd_sig is not None:
            if macd_h > 0 and macd_val > macd_sig:
                tech_pts += 2
                tech_items.append(f"MACD金叉 (hist={macd_h:.1f}): +2")
            elif macd_h < 0 and macd_val < macd_sig:
                tech_pts -= 2
                tech_items.append(f"MACD死叉 (hist={macd_h:.1f}): -2")
            else:
                tech_items.append(f"MACD中性 (hist={macd_h:.1f}): 0")

        # ATR
        atr_pct = indicators.get('atr_pct')
        if atr_pct is not None:
            if atr_pct > 3:
                tech_items.append(f"ATR={atr_pct:.1f}% 高波动⚠️")
            elif atr_pct < 0.5:
                tech_items.append(f"ATR={atr_pct:.1f}% 低波动")
            else:
                tech_items.append(f"ATR={atr_pct:.1f}% 正常波动")

        tech_pts = max(-15, min(15, tech_pts))

        if tech_bias == 'bullish' and tech_pts > 0:
            reasons.append(f'技术面偏多 ({tech_pts}分)')
        elif tech_bias == 'bearish':
            risks.append('技术面偏空')

        breakdown['📈 技术指标'] = {'points': tech_pts, 'detail': ' | '.join(tech_items), 'sub_items': tech_items}
        score += tech_pts
    else:
        breakdown['📈 技术指标'] = {'points': 0, 'detail': '未计算📈 技术指标', 'sub_items': ['无技术数据']}

    # ── 5. ⚠️ 数据覆盖度惩罚 ──
    if data_dimensions_found == 0:
        penalty = -10
        breakdown['⚠️ 数据覆盖'] = {'points': penalty, 'detail': '四个维度全部缺失', 'sub_items': ['0/4维度有数据: -10']}
        score += penalty
        risks.append('⚠️ 数据覆盖不足')
    elif data_dimensions_found == 1:
        penalty = -5
        breakdown['⚠️ 数据覆盖'] = {'points': penalty, 'detail': f'仅{data_dimensions_found}/4维度有数据', 'sub_items': [f'{data_dimensions_found}/4维度有数据: -5']}
        score += penalty
        risks.append('数据维度单一')

    score = max(0, min(100, score))

    if score >= 75:
        suggestion = '做多'
        confidence = '高'
    elif score >= 60:
        suggestion = '做多'
        confidence = '中'
    elif score >= 45:
        suggestion = '观望'
        confidence = '低'
    else:
        suggestion = '观望/做空'
        confidence = '低'

    if data_dimensions_found <= 1 and confidence == '高':
        confidence = '中'
    if data_dimensions_found == 0 and confidence == '中':
        confidence = '低'

    return {
        'symbol': sym_pair,
        'symbol_base': sym_base,
        'score': round(score, 1),
        'suggestion': suggestion,
        'confidence': confidence,
        'reasons': reasons,
        'risks': risks,
        'score_breakdown': breakdown,
        'data_coverage': data_dimensions_found,
    }


def generate_recommendations(
    signals: Optional[Dict[str, Any]] = None,
    min_score: int = MIN_SCORE_TO_SUGGEST,
    top_n: Optional[int] = 10,
) -> List[Dict[str, Any]]:
    """生成交易建议列表。"""
    if signals is None:
        signals = _run_monitoring_hub()

    # Collect candidate symbols from all sources
    candidates = set()

    # From narrative symbol view
    for sv in signals.get('narrative', {}).get('symbol_view', []):
        sym = _normalize_candidate_pair(sv.get('symbol'))
        if sym:
            candidates.add(sym)

    # From on-chain
    for c in (signals.get('onchain', {}).get('strong_buy_candidates', []) +
              signals.get('onchain', {}).get('observation_candidates', [])):
        sym = _normalize_candidate_pair(c.get('symbol'))
        if sym:
            candidates.add(sym)

    # From futures
    for sym in signals.get('futures', {}).keys():
        normalized = _normalize_candidate_pair(sym)
        if normalized:
            candidates.add(normalized)

    # Score all candidates
    recommendations = []
    for sym in candidates:
        result = _compute_symbol_score(sym, signals)
        if result['score'] >= min_score:
            recommendations.append(result)

    recommendations.sort(key=lambda x: (-x['score'], x['symbol']))
    return recommendations if top_n is None else recommendations[:top_n]


def format_recommendation(rec: Dict[str, Any], stake: float, leverage: int) -> str:
    """格式化单个交易建议为可读文本。"""
    emoji = '🟢' if rec['suggestion'] == '做多' else '🔴' if rec['suggestion'] == '做空' else '⚪'
    lines = [
        f"{emoji} **{rec['symbol']}** — {rec['suggestion']} (置信度: {rec['confidence']})",
        f"  综合评分: **{rec['score']}/100**",
    ]
    if rec['reasons']:
        lines.append(f"  ✅ 利好:")
        for r in rec['reasons']:
            lines.append(f"     {r}")
    if rec['risks']:
        lines.append(f"  ⚠️ 风险:")
        for r in rec['risks']:
            lines.append(f"     {r}")
    if rec['suggestion'] == '做多':
        lines.append(f"  📋 建议: stake {stake}U, {leverage}x 杠杆, 做多")
    return '\n'.join(lines)


def build_trade_command(rec: Dict[str, Any], stake: float, leverage: int) -> List[str]:
    """构建 coin-trade-executor 命令。"""
    side = 'long' if rec['suggestion'] == '做多' else 'short'
    return [
        'python3', str(TRADE_EXECUTOR),
        'open',
        '--mode', 'futures',
        '--pair', rec['symbol'].replace('USDT', '/USDT:USDT'),
        '--side', side,
        '--stake', str(stake),
        '--leverage', str(leverage),
        '--pretty',
    ]


def execute_trade(rec: Dict[str, Any], stake: float, leverage: int) -> Dict[str, Any]:
    """执行交易。"""
    cmd = build_trade_command(rec, stake, leverage)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return {
            'symbol': rec['symbol'],
            'command': ' '.join(cmd),
            'returncode': result.returncode,
            'stdout': result.stdout,
            'stderr': result.stderr,
        }
    except Exception as exc:
        return {'symbol': rec['symbol'], 'error': str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description='信号→交易建议生成器')
    parser.add_argument('--execute', action='store_true', help='生成建议后执行（需确认）')
    parser.add_argument('--auto', action='store_true', help='自动执行高置信度信号（危险）')
    parser.add_argument('--stake', type=float, default=DEFAULT_STAKE_USD)
    parser.add_argument('--leverage', type=int, default=DEFAULT_LEVERAGE)
    parser.add_argument('--min-score', type=float, default=MIN_SCORE_TO_SUGGEST)
    parser.add_argument('--top-n', type=int, default=5)
    args = parser.parse_args()

    if args.leverage > MAX_LEVERAGE:
        print(f"⚠️ 杠杆超过最大限制 {MAX_LEVERAGE}x，已调整")
        args.leverage = MAX_LEVERAGE

    print("🔄 正在运行 Monitoring Hub...")
    signals = _run_monitoring_hub()

    print("📊 正在生成交易建议...\n")
    recs = generate_recommendations(signals, min_score=int(args.min_score), top_n=args.top_n)

    if recs:
        print(f"{'='*50}")
        print(f"📊 交易建议 ({len(recs)} 个)")
        print(f"{'='*50}\n")

        for i, rec in enumerate(recs, 1):
            print(f"[{i}] {format_recommendation(rec, args.stake, args.leverage)}")
            print()
    else:
        print("❌ 没有达到最低评分的交易建议")
        print(f"   最低评分要求: {args.min_score}")

    # Output as JSON for programmatic use
    output = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'config': {
            'stake': args.stake,
            'leverage': args.leverage,
            'min_score': args.min_score,
        },
        'source_status': signals.get('_source_status', {}),
        'recommendations': recs,
    }
    out_path = RESULTS_DIR / f'signal_recommendations_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"💾 保存到: {out_path}")

    # Execute if requested
    if args.execute or args.auto:
        for rec in recs:
            if args.auto and rec['score'] < MIN_SCORE_TO_AUTO:
                print(f"⏭️ 跳过 {rec['symbol']} (score={rec['score']} < {MIN_SCORE_TO_AUTO})")
                continue
            print(f"\n🚀 执行: {rec['symbol']} {rec['suggestion']}...")
            result = execute_trade(rec, args.stake, args.leverage)
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
