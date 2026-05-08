#!/usr/bin/env python3
"""Whale Alert 大额转账数据源 — 通过 @whale_alert Twitter 获取。

不需要 Whale Alert API key，复用 6551 Twitter API。
解析 Whale Alert 推文中的转账数据：金额、代币、来源、去向。
"""

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bsc.data.twitter_6551 import search_tweets, _get_token

# 交易所名称映射
EXCHANGE_MAP = {
    'binance': 'Binance', 'coinbase': 'Coinbase', 'kraken': 'Kraken',
    'okex': 'OKX', 'huobi': 'HTX', 'htx': 'HTX', 'bybit': 'Bybit',
    'kucoin': 'KuCoin', 'gate.io': 'Gate.io', 'gate': 'Gate.io',
    'bitfinex': 'Bitfinex', 'gemini': 'Gemini', 'bitstamp': 'Bitstamp',
    'crypto.com': 'Crypto.com', 'upbit': 'Upbit', 'bithumb': 'Bithumb',
    'unknown wallet': '未知钱包', 'unknown': '未知钱包',
}

# 转账方向判断规则
EXCHANGE_TO_UNKNOWN = '交易所转出'      # 提币 = 看涨信号
UNKNOWN_TO_EXCHANGE = '转入交易所'      # 充币 = 看跌信号
EXCHANGE_TO_EXCHANGE = '交易所间转移'
SPARK_TO_EXCHANGE = 'Spark清算'         # 特殊：Spark (MakerDAO) 清算


def _parse_whale_tweet(text: str) -> Optional[Dict[str, Any]]:
    """Parse a Whale Alert tweet into structured transaction data."""
    # Pattern: 🚨 AMOUNT $TOKEN (USD_VALUE) transferred from #SOURCE to #DEST
    pattern = re.compile(
        r'(\d[\d,]*(?:\.\d+)?)\s*\$(\w+)\s*\((\d[\d,]*(?:\.\d+)?)\s*USD\)\s*'
        r'transferred from\s*#?([^\n]+?)\s+to\s*#?([^\n]+?)(?:\s*$|\s*https?)',
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None

    amount_str = match.group(1).replace(',', '')
    token = match.group(2).upper()
    usd_str = match.group(3).replace(',', '')
    source_raw = match.group(4).strip()
    dest_raw = match.group(5).strip()

    try:
        amount = float(amount_str)
        usd_value = float(usd_str)
    except ValueError:
        return None

    # Normalize source/dest names
    source = source_raw.lower().strip()
    dest = dest_raw.lower().strip()
    for key, name in EXCHANGE_MAP.items():
        if key in source:
            source = name
        if key in dest:
            dest = name

    # Determine direction
    source_is_exchange = source not in ('未知钱包', 'unknown wallet', 'unknown')
    dest_is_exchange = dest not in ('未知钱包', 'unknown wallet', 'unknown')

    if source_is_exchange and not dest_is_exchange:
        direction = EXCHANGE_TO_UNKNOWN
        signal = 'bullish'  # 提币 = 囤币
    elif not source_is_exchange and dest_is_exchange:
        direction = UNKNOWN_TO_EXCHANGE
        signal = 'bearish'  # 充币 = 准备卖
    elif source_is_exchange and dest_is_exchange:
        direction = EXCHANGE_TO_EXCHANGE
        signal = 'neutral'
    else:
        direction = '钱包间转移'
        signal = 'neutral'

    # Spark special case (MakerDAO DAI vault liquidations)
    if 'spark' in source_raw.lower() or 'spark' in dest_raw.lower():
        direction = 'Spark清算'
        signal = 'bearish'

    return {
        'amount': amount,
        'token': token,
        'usd_value': usd_value,
        'source': source,
        'destination': dest,
        'direction': direction,
        'signal': signal,
        'raw_text': text[:300],
    }


def fetch_whale_alerts(
    token: Optional[str] = None,
    max_results: int = 20,
    min_usd: float = 5_000_000,
) -> List[Dict[str, Any]]:
    """Fetch recent Whale Alert tweets and parse them."""
    token = token or _get_token()
    if not token:
        return [{"error": "No API token"}]

    tweets = search_tweets(token, from_user='whale_alert', min_likes=5, max_results=max_results)
    alerts: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for tw in tweets:
        text = tw.get('text') or tw.get('full_text') or ''
        parsed = _parse_whale_tweet(text)
        if not parsed:
            continue
        if parsed['usd_value'] < min_usd:
            continue
        parsed['tweet_id'] = tw.get('id', '')
        parsed['likes'] = tw.get('favoriteCount', 0)
        parsed['fetched_at'] = now.isoformat()
        alerts.append(parsed)

    return alerts


def to_narrative_format(alerts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert whale alerts to unified narrative monitor format."""
    rows = []
    for alert in alerts:
        if alert.get('error'):
            continue
        usd_m = alert['usd_value'] / 1e6
        body = (
            f"巨鲸转账：{alert['amount']:,.0f} ${alert['token']} "
            f"(${usd_m:.1f}M) {alert['direction']} "
            f"从 {alert['source']} → {alert['destination']}"
        )

        signal_score = 55
        if alert['signal'] == 'bullish':
            signal_score = 65 + min(usd_m / 50, 20)  # 大额提币加分
        elif alert['signal'] == 'bearish':
            signal_score = 60 + min(usd_m / 50, 15)

        rows.append({
            'body': body,
            'tags': ['whale', alert['direction']],
            'trading_pairs': [f"{alert['token']}USDT"] if alert['token'] in (
                'BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE', 'ADA', 'AVAX',
                'DOT', 'MATIC', 'LINK', 'UNI', 'AAVE', 'OP', 'ARB',
            ) else [],
            'symbol_candidates': [f"{alert['token']}USDT"],
            'author': 'Whale Alert',
            'username': 'whale_alert',
            'publish_time': '',
            'web_link': f"https://x.com/whale_alert/status/{alert.get('tweet_id', '')}",
            'engagement_total': alert.get('likes', 0),
            'signal_score': round(signal_score, 1),
            'signal_label': f"巨鲸{'↑' if alert['signal'] == 'bullish' else '↓' if alert['signal'] == 'bearish' else '↔'}",
            'content_id': f"whale_{alert.get('tweet_id', hash(alert['raw_text']))}",
            '_source': 'whale_alert',
            # Extra fields for on-chain module
            'whale_direction': alert['direction'],
            'whale_signal': alert['signal'],
            'whale_usd': alert['usd_value'],
            'whale_token': alert['token'],
        })
    return rows


if __name__ == "__main__":
    token = _get_token()
    if not token:
        print("Set API_6551_KEY or TWITTER_TOKEN")
    else:
        alerts = fetch_whale_alerts(token, max_results=15)
        print(f"Found {len(alerts)} whale alerts")
        for a in alerts[:5]:
            usd_m = a['usd_value'] / 1e6
            print(f"  ${usd_m:.1f}M {a['token']} | {a['direction']} | {a['source']} → {a['destination']} | {a['signal']}")
