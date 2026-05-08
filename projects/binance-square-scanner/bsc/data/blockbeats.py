#!/usr/bin/env python3
"""BlockBeats (区块律动) 快讯数据源 — 免费，无需 API key。"""

import re
import json
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, List

import requests

BLOCKBEATS_URL = "https://www.theblockbeats.info/newsflash"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return unescape(re.sub(r"\s+", " ", text).strip())


def _extract_items_from_nuxt(html: str) -> List[Dict[str, Any]]:
    """Extract news items from BlockBeats Nuxt SSR page."""
    match = re.search(r"window\.__NUXT__=(.*?)</script>", html, re.DOTALL)
    if not match:
        return []

    nuxt = match.group(1)
    parts = nuxt.split("isShare:")
    items: List[Dict[str, Any]] = []

    for part in parts[1:]:
        cm = re.search(r'content:"((?:[^"\\]|\\.)*)"', part)
        tm = re.search(r'time:"(\d{2}:\d{2})"', part)
        if not cm:
            continue
        raw_content = cm.group(1)
        decoded = raw_content.replace('\\"', '"')
        decoded = decoded.encode("raw_unicode_escape").decode("unicode_escape")
        text = _strip_html(decoded)
        if len(text) < 15:
            continue
        items.append({
            "body": text,
            "time": tm.group(1) if tm else "",
            "source": "blockbeats",
        })

    return items


def fetch_blockbeats_news(limit: int = 30) -> List[Dict[str, Any]]:
    """Fetch latest BlockBeats newsflash items."""
    try:
        resp = requests.get(BLOCKBEATS_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        return [{"error": f"BlockBeats fetch failed: {exc}", "source": "blockbeats"}]

    items = _extract_items_from_nuxt(resp.text)
    now = datetime.now(timezone.utc)
    for item in items[:limit]:
        item["fetched_at"] = now.isoformat()
    return items[:limit]


def to_narrative_format(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert BlockBeats items to unified narrative monitor format."""
    rows = []
    for item in items:
        if item.get("error"):
            continue
        body = item.get("body", "")
        rows.append({
            "body": body,
            "tags": [],
            "trading_pairs": [],
            "symbol_candidates": [],
            "author": "BlockBeats",
            "username": "theblockbeats",
            "publish_time": item.get("time", ""),
            "web_link": BLOCKBEATS_URL,
            "engagement_total": 0,
            "signal_score": 50,
            "signal_label": "快讯",
            "content_id": f"blockbeats_{abs(hash(body[:80]))}",
            "_source": "blockbeats",
        })
    return rows
