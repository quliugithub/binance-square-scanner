#!/usr/bin/env python3
"""加密新闻聚合数据源 v2 — 替代 Twitter KOL 抓取。

数据来源：
- CoinTelegraph RSS（英文，30条/次）
- The Block RSS（英文，19条/次）
- Web 搜索近期加密讨论（通过 Jina Reader）

全部免费，无需 API key。
"""

import json
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, List
from xml.etree import ElementTree as ET

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

RSS_FEEDS = {
    "cointelegraph": "https://cointelegraph.com/rss",
    "theblock": "https://www.theblock.co/rss.xml",
}

JINA_READER_URL = "https://r.jina.ai/"

# Web search queries for recent crypto discussions
SEARCH_QUERIES = [
    "crypto altcoin narrative rotation signal",
    "bitcoin funding rate open interest sentiment",
    "solana defi TVL inflows outflows",
    "RWA tokenization institutional adoption",
]


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return unescape(re.sub(r"\s+", " ", text).strip())


def _parse_rss(xml_text: str, source_name: str) -> List[Dict[str, Any]]:
    """Parse RSS XML into list of items."""
    items: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    # Handle both RSS 2.0 and Atom
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    # RSS 2.0
    for item_el in root.iter("item"):
        title = (item_el.findtext("title") or "").strip()
        desc = _strip_html(item_el.findtext("description") or "")
        link = (item_el.findtext("link") or "").strip()
        pub_date = (item_el.findtext("pubDate") or "").strip()

        body = f"{title}. {desc}" if desc else title
        if len(body) < 15:
            continue

        items.append({
            "title": title,
            "body": body[:800],
            "link": link,
            "pub_date": pub_date,
            "source": source_name,
        })

    # Atom fallback
    if not items:
        for entry_el in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = (entry_el.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            summary = _strip_html(
                entry_el.findtext("{http://www.w3.org/2005/Atom}summary") or ""
            )
            link_el = entry_el.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href", "") if link_el is not None else ""
            pub_date = (
                entry_el.findtext("{http://www.w3.org/2005/Atom}published")
                or entry_el.findtext("{http://www.w3.org/2005/Atom}updated")
                or ""
            ).strip()

            body = f"{title}. {summary}" if summary else title
            if len(body) < 15:
                continue

            items.append({
                "title": title,
                "body": body[:800],
                "link": link,
                "pub_date": pub_date,
                "source": source_name,
            })

    return items


def fetch_rss_feeds() -> List[Dict[str, Any]]:
    """Fetch all configured RSS feeds."""
    all_items: List[Dict[str, Any]] = []

    for source_name, url in RSS_FEEDS.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200 and len(resp.text) > 100:
                items = _parse_rss(resp.text, source_name)
                all_items.extend(items)
        except Exception:
            continue

    return all_items


def _search_crypto_discussions(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search for recent crypto discussions via Jina Reader on Google."""
    results: List[Dict[str, Any]] = []
    search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}&tbs=qdr:d"
    jina_url = f"{JINA_READER_URL}{search_url}"

    try:
        resp = requests.get(jina_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if resp.status_code != 200 or len(resp.text) < 100:
            return results

        blocks = re.split(r"\n{2,}", resp.text)
        for block in blocks:
            block = block.strip()
            if len(block) < 40 or len(block) > 600:
                continue
            if any(skip in block.lower() for skip in (
                "sign up", "log in", "google", "related searches",
                "people also", "show more", "privacy", "terms",
            )):
                continue
            alpha_ratio = len(re.findall(r"[a-zA-Z\u4e00-\u9fff]", block)) / max(len(block), 1)
            if alpha_ratio < 0.35:
                continue
            results.append({
                "title": "",
                "body": block[:500],
                "link": "",
                "pub_date": "",
                "source": "web_search",
            })
    except Exception:
        pass

    return results[:max_results]


def fetch_web_searches() -> List[Dict[str, Any]]:
    """Fetch recent crypto discussions from web search."""
    all_results: List[Dict[str, Any]] = []
    for query in SEARCH_QUERIES[:3]:
        results = _search_crypto_discussions(query, max_results=4)
        all_results.extend(results)
    return all_results


def fetch_all_news() -> List[Dict[str, Any]]:
    """Fetch all news from RSS feeds + web search."""
    all_items: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    # RSS feeds
    rss_items = fetch_rss_feeds()
    for item in rss_items:
        item["fetched_at"] = now.isoformat()
    all_items.extend(rss_items)

    # Web search
    web_items = fetch_web_searches()
    for item in web_items:
        item["fetched_at"] = now.isoformat()
    all_items.extend(web_items)

    return all_items


def to_narrative_format(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert news items to unified narrative monitor format."""
    rows = []
    for item in items:
        if item.get("error"):
            continue
        body = item.get("body", "")
        source = item.get("source", "news")
        title = item.get("title", "")

        # Higher signal score for RSS news (editorial curation)
        base_score = 58 if source in ("cointelegraph", "theblock") else 52

        rows.append({
            "body": body,
            "tags": [],
            "trading_pairs": [],
            "symbol_candidates": [],
            "author": source.replace("_", " ").title(),
            "username": source,
            "publish_time": item.get("pub_date", ""),
            "web_link": item.get("link", ""),
            "engagement_total": 0,
            "signal_score": base_score,
            "signal_label": "加密快讯",
            "content_id": f"news_{source}_{abs(hash(body[:80]))}",
            "_source": source,
        })
    return rows


if __name__ == "__main__":
    items = fetch_all_news()
    print(f"Total items: {len(items)}")
    for item in items[:5]:
        src = item["source"]
        body = item["body"][:120]
        print(f"  [{src}] {body}")
        print()
