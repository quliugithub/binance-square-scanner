#!/usr/bin/env python3
"""Macro Calendar Monitor — 宏观日历事件 + 重大新闻监控"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from bsc.constants import RESULTS_DIR


TIMEOUT = 15

# ── 宏观事件日历 ─────────────────────────────────────────────────
# 经济日历来源: Forex Factory / Investing.com 公开数据
# 这里用静态配置 + 可选远程拉取的方式

CALENDAR_API = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'

# 事件权重配置
EVENT_IMPACT_MAP: Dict[str, str] = {
    'CPI': 'high',
    'PPI': 'medium',
    'FOMC': 'high',
    'FOMC Statement': 'high',
    'Federal Funds Rate': 'high',
    'NFP': 'high',
    'Non-Farm Employment Change': 'high',
    'Unemployment Rate': 'medium',
    'GDP': 'medium',
    'Retail Sales': 'medium',
    'PCE': 'high',
    'Core PCE': 'high',
    'Jackson Hole': 'high',
    'ECB': 'medium',
    'BOJ': 'medium',
}

EVENT_TYPE_MAP: Dict[str, str] = {
    'CPI': 'cpi',
    'PPI': 'cpi',
    'FOMC': 'fomc',
    'FOMC Statement': 'fomc',
    'Federal Funds Rate': 'fomc',
    'NFP': 'nfp',
    'Non-Farm Employment Change': 'nfp',
    'Unemployment Rate': 'nfp',
    'GDP': 'gdp',
    'Retail Sales': 'macro',
    'PCE': 'cpi',
    'Core PCE': 'cpi',
    'Jackson Hole': 'fomc',
    'ECB': 'fomc',
    'BOJ': 'fomc',
}

# 新闻 RSS 来源
NEWS_SOURCES = [
    {
        'name': 'CoinDesk',
        'url': 'https://www.coindesk.com/arc/outboundfeeds/rss/',
        'type': 'rss',
    },
    {
        'name': 'The Block',
        'url': 'https://www.theblock.co/rss.xml',
        'type': 'rss',
    },
]

# 关键词分类
NEWS_KEYWORDS: Dict[str, List[str]] = {
    'etf': ['etf', 'spot etf', 'bitcoin etf', 'ethereum etf', 'inflow', 'outflow', 'sosovalue', 'blackrock', 'fidelity', 'grayscale'],
    'regulation': ['sec', 'cftc', 'regulation', 'ban', 'lawsuit', 'enforcement', 'compliance', 'regulatory', 'approval'],
    'upgrade': ['upgrade', 'hard fork', 'eip', 'merge', 'update', 'mainnet', 'testnet', 'launch'],
    'macro': ['fed', 'federal reserve', 'interest rate', 'inflation', 'cpi', 'fomc', 'powell', 'treasury', 'dollar', 'dxy'],
    'black_swan': ['hack', 'exploit', 'crash', 'collapse', 'bankruptcy', 'liquidation', 'rug', 'scam'],
}

SENTIMENT_KEYWORDS: Dict[str, List[str]] = {
    'positive': ['surge', 'rally', 'bullish', 'approval', 'breakthrough', 'milestone', 'record', 'adoption', 'partnership', 'inflow'],
    'negative': ['crash', 'plunge', 'bearish', 'hack', 'ban', 'lawsuit', 'outflow', 'collapse', 'warning', 'risk'],
}


# ── helpers ──────────────────────────────────────────────────────

def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def parse_event_date(date_str: str) -> Optional[datetime]:
    """尝试解析事件日期"""
    for fmt in ['%Y-%m-%d', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%fZ', '%b %d, %Y']:
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


# ── 宏观日历 ─────────────────────────────────────────────────────

def fetch_economic_calendar() -> List[Dict]:
    """从 Forex Factory 拉取本周经济日历"""
    try:
        resp = requests.get(CALENDAR_API, timeout=TIMEOUT)
        resp.raise_for_status()
        events = resp.json()
        if isinstance(events, list):
            return events
        return []
    except Exception:
        return []


def process_calendar_events(raw_events: List[Dict]) -> List[Dict]:
    """处理经济日历事件"""
    now = datetime.now(UTC)
    processed = []

    for evt in raw_events:
        title = evt.get('title', '') or evt.get('event', '')
        country = evt.get('country', '') or evt.get('currency', '')
        date_str = evt.get('date', '') or evt.get('actual_date', '')
        impact_raw = evt.get('impact', '') or evt.get('forecast', '')

        # 解析日期
        event_time = parse_event_date(date_str)
        if not event_time:
            continue

        # 只看未来 7 天内的事件
        days_until = (event_time - now).days
        if days_until < 0 or days_until > 7:
            continue

        # 只关注美国相关 + 主要央行
        if country.upper() not in ['USD', 'US', 'ALL', '']:
            continue

        # 匹配事件类型和影响
        event_type = 'macro'
        impact_level = 'low'
        for key, typ in EVENT_TYPE_MAP.items():
            if key.lower() in title.lower():
                event_type = typ
                impact_level = EVENT_IMPACT_MAP.get(key, 'medium')
                break

        # 如果事件有影响字段
        if isinstance(impact_raw, str) and impact_raw:
            if 'high' in impact_raw.lower() or '3' in impact_raw:
                impact_level = 'high'
            elif 'medium' in impact_raw.lower() or '2' in impact_raw:
                impact_level = 'medium'

        processed.append({
            'event_name': title,
            'event_type': event_type,
            'event_time': event_time.isoformat().replace('+00:00', 'Z'),
            'days_until': days_until,
            'impact_level': impact_level,
            'country': country,
        })

    # 按时间排序
    processed.sort(key=lambda x: x.get('days_until', 99))
    return processed


# ── 新闻抓取 ─────────────────────────────────────────────────────

def fetch_rss(url: str, limit: int = 20) -> List[Dict]:
    """简易 RSS 解析"""
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        text = resp.text
    except Exception:
        return []

    items = []
    # 简易 XML 解析，不用外部依赖
    import re
    item_matches = re.findall(r'<item>(.*?)</item>', text, re.DOTALL)
    for item_xml in item_matches[:limit]:
        title = re.search(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', item_xml)
        link = re.search(r'<link>(.*?)</link>', item_xml)
        desc = re.search(r'<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>', item_xml, re.DOTALL)
        pub_date = re.search(r'<pubDate>(.*?)</pubDate>', item_xml)

        items.append({
            'title': title.group(1).strip() if title else '',
            'url': link.group(1).strip() if link else '',
            'description': desc.group(1).strip()[:300] if desc else '',
            'published_at': pub_date.group(1).strip() if pub_date else '',
        })

    return items


def classify_news(item: Dict) -> Dict:
    """分类新闻"""
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()

    # 分类
    categories = []
    for cat, keywords in NEWS_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            categories.append(cat)

    if not categories:
        categories.append('other')

    # 相关性
    relevance = []
    if any(w in text for w in ['bitcoin', 'btc']):
        relevance.append('btc')
    if any(w in text for w in ['ethereum', 'eth']):
        relevance.append('eth')
    if any(w in text for w in ['fed', 'inflation', 'cpi', 'fomc', 'dollar', 'macro']):
        relevance.append('macro')
    if not relevance:
        relevance.append('industry')

    # 情绪
    pos = sum(1 for kw in SENTIMENT_KEYWORDS['positive'] if kw in text)
    neg = sum(1 for kw in SENTIMENT_KEYWORDS['negative'] if kw in text)
    if pos > neg:
        sentiment = 'positive'
    elif neg > pos:
        sentiment = 'negative'
    else:
        sentiment = 'neutral'

    return {
        **item,
        'categories': categories,
        'relevance': relevance,
        'sentiment': sentiment,
    }


def fetch_and_classify_news(limit_per_source: int = 15) -> List[Dict]:
    """抓取并分类新闻"""
    all_items = []
    for src in NEWS_SOURCES:
        items = fetch_rss(src['url'], limit=limit_per_source)
        for item in items:
            item['source'] = src['name']
            classified = classify_news(item)
            all_items.append(classified)

    # 去重（按标题）
    seen_titles = set()
    unique = []
    for item in all_items:
        t = item.get('title', '').lower()
        if t and t not in seen_titles:
            seen_titles.add(t)
            unique.append(item)

    # 按相关性排序（有 btc/eth 的优先）
    def sort_key(item):
        rel = item.get('relevance', [])
        if 'btc' in rel or 'eth' in rel:
            return 0
        if 'macro' in rel:
            return 1
        return 2

    unique.sort(key=sort_key)
    return unique[:30]


# ── 新闻催化评分 ─────────────────────────────────────────────────

def calc_news_catalyst_score(events: List[Dict], news: List[Dict]) -> Dict:
    """计算消息面催化评分"""
    score = 0.0
    drivers = []

    # 宏观事件贡献
    high_events = [e for e in events if e.get('impact_level') == 'high']
    near_events = [e for e in events if e.get('days_until', 99) <= 2]

    if near_events:
        score += len(near_events) * 10
        for e in near_events[:3]:
            drivers.append(f'{e["event_name"]} ({e["days_until"]}天后)')

    if high_events:
        score += len(high_events) * 5

    # 新闻催化
    relevant_news = [n for n in news if 'btc' in n.get('relevance', []) or 'eth' in n.get('relevance', [])]
    negative_news = [n for n in relevant_news if n.get('sentiment') == 'negative']
    positive_news = [n for n in relevant_news if n.get('sentiment') == 'positive']

    if negative_news:
        score -= len(negative_news) * 5
        for n in negative_news[:2]:
            drivers.append(f'负面: {n["title"][:50]}')

    if positive_news:
        score += len(positive_news) * 5
        for n in positive_news[:2]:
            drivers.append(f'正面: {n["title"][:50]}')

    # ETF 特别关注
    etf_news = [n for n in news if 'etf' in n.get('categories', [])]
    if etf_news:
        score += 5
        drivers.append(f'ETF相关新闻 {len(etf_news)} 条')

    from funding_rate_monitor import clamp
    score = clamp(score, -50, 50)

    if score > 15:
        catalyst = 'positive'
    elif score < -15:
        catalyst = 'negative'
    else:
        catalyst = 'neutral'

    return {
        'news_catalyst_score': round(score, 1),
        'news_catalyst_direction': catalyst,
        'relevant_news_count': len(relevant_news),
        'drivers': drivers,
    }


# ── main pipeline ────────────────────────────────────────────────

def build_macro_payload() -> Dict:
    """构建完整宏观监控 payload"""
    # 经济日历
    raw_events = fetch_economic_calendar()
    events = process_calendar_events(raw_events)

    # 新闻
    news = fetch_and_classify_news()

    # 催化评分
    catalyst = calc_news_catalyst_score(events, news)

    # 筛选重要新闻
    important_news = [
        {k: v for k, v in n.items() if k not in ('description',)}
        for n in news[:15]
    ]

    return {
        'generated_at': utc_now_iso(),
        'scan_config': {
            'mode': 'macro_calendar_monitor_v1',
        },
        'upcoming_events': events[:10],
        'recent_news': important_news,
        'catalyst': catalyst,
    }


# ── rendering ────────────────────────────────────────────────────

def render_markdown(payload: Dict) -> str:
    lines = ['# Macro Calendar Monitor', '']

    # 事件
    events = payload.get('upcoming_events', [])
    lines.append(f'## 📅 近期宏观事件 ({len(events)})')
    lines.append('')
    if events:
        for e in events:
            emoji = '🔴' if e.get('impact_level') == 'high' else ('🟡' if e.get('impact_level') == 'medium' else '⚪')
            lines.append(f'- {emoji} **{e.get("event_name")}** — {e.get("days_until")}天后 ({e.get("event_type")})')
    else:
        lines.append('- 无近期重大事件')
    lines.append('')

    # 新闻
    news = payload.get('recent_news', [])
    lines.append(f'## 📰 近期新闻 ({len(news)})')
    lines.append('')
    for n in news[:10]:
        s_emoji = '🟢' if n.get('sentiment') == 'positive' else ('🔴' if n.get('sentiment') == 'negative' else '⚪')
        cats = ', '.join(n.get('categories', []))
        rel = ', '.join(n.get('relevance', []))
        lines.append(f'- {s_emoji} **[{n.get("source")}]** {n.get("title", "")[:80]}')
        lines.append(f'  分类: {cats} | 关联: {rel}')
    lines.append('')

    # 催化评分
    cat = payload.get('catalyst', {})
    lines.append(f'## 🎯 消息面催化评分: {cat.get("news_catalyst_score")} ({cat.get("news_catalyst_direction")})')
    if cat.get('drivers'):
        for d in cat['drivers']:
            lines.append(f'- {d}')

    return '\n'.join(lines).rstrip() + '\n'


def save_payload(payload: Dict, output: Optional[str] = None) -> str:
    if output:
        path = Path(output)
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = RESULTS_DIR / f'macro_monitor_{ts}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    md_path = path.with_suffix('.md')
    md_path.write_text(render_markdown(payload), encoding='utf-8')
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='Macro Calendar Monitor')
    parser.add_argument('--output')
    args = parser.parse_args()

    payload = build_macro_payload()
    out = save_payload(payload, args.output)
    print(out)
    print(render_markdown(payload))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
