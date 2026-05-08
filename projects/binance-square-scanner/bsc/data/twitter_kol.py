#!/usr/bin/env python3
"""Twitter KOL 监控数据源 — 通过 Jina Reader 抓取，免费无需 API key。

可选增强：配置 READX_API_KEY 环境变量后使用 readx API 获得更精准的数据。
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

# ── 默认 KOL 列表（加密货币领域关键意见领袖）────────────────────────
DEFAULT_KOLS = [
    # 英文核心 KOL
    "GaborGurbacs",       # VanEck 数字资产策略师
    "zabor_gibbens",      # 宏观分析
    "Pentoshi1337",       # 交易员
    "CryptoMichNL",       # 分析师
    "TheDeFiEdge",        # DeFi 教育
    "0xfoobar",           # 安全研究
    "sassal0x",           # ETH 布道者
    "VitalikButerin",     # 以太坊创始人
    "CryptoHayes",        # BitMEX 创始人
    "aeyakovenko",        # Solana 联合创始人
    "anatoly_solana",     # Solana
    # 中文圈 KOL（推特上活跃的）
    "BTW0203",            # 分析师
    "wojak_anon",         # 链上分析
    "0xminion",           # DeFi 研究
]

JINA_READER_URL = "https://r.jina.ai/"
JINA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/plain",
}

# Jina reader script path
JINA_SCRIPT = os.path.expanduser("~/.openclaw/workspace/skills/haibo-jina-reader/scripts/jina-reader.py")


def _get_readx_api_key() -> Optional[str]:
    """Try to load readx API key from config."""
    cred_path = os.path.expanduser("~/.config/readx/credentials.json")
    try:
        with open(cred_path) as f:
            data = json.load(f)
            return data.get("api_key")
    except Exception:
        return os.environ.get("READX_API_KEY")


def fetch_twitter_profile_jina(handle: str, timeout: int = 30) -> Optional[str]:
    """Fetch a Twitter profile page using Jina Reader API."""
    handle = handle.lstrip("@").strip()
    url = f"{JINA_READER_URL}https://x.com/{handle}"
    try:
        resp = requests.get(url, headers=JINA_HEADERS, timeout=timeout)
        if resp.status_code == 200 and len(resp.text) > 50:
            return resp.text
    except Exception:
        pass

    # Fallback: try jina-reader.py script
    if os.path.exists(JINA_SCRIPT):
        try:
            result = subprocess.run(
                ["python3", JINA_SCRIPT, f"https://x.com/{handle}", "--format", "markdown"],
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0 and len(result.stdout) > 50:
                return result.stdout
        except Exception:
            pass

    return None


def extract_tweets_from_page(content: str, handle: str) -> List[Dict[str, Any]]:
    """Extract individual tweets from a Jina-reader scraped Twitter page."""
    tweets: List[Dict[str, Any]] = []

    # Split into tweet-like chunks
    # Jina reader returns markdown with tweet text blocks
    # Look for text blocks separated by newlines that look like tweets
    chunks = re.split(r"\n{2,}", content)

    for chunk in chunks:
        chunk = chunk.strip()
        if len(chunk) < 20 or len(chunk) > 1000:
            continue
        # Skip navigation/header/footer text
        if any(skip in chunk.lower() for skip in (
            "sign up", "log in", "terms of service", "privacy policy",
            "cookie", "download", "follow", "followers", "following",
            "posts", "replies", "highlights", "media", "likes",
            "x corp", "help center", "get app",
        )):
            continue
        # Skip if mostly links/mentions
        alpha_ratio = len(re.findall(r"[a-zA-Z\u4e00-\u9fff]", chunk)) / max(len(chunk), 1)
        if alpha_ratio < 0.4:
            continue

        tweets.append({
            "body": chunk[:500],
            "author_handle": handle,
            "source": "twitter_kol",
        })

    return tweets[:10]  # Max 10 tweets per profile


def fetch_kol_tweets(
    handles: Optional[List[str]] = None,
    max_profiles: int = 8,
    timeout: int = 25,
) -> List[Dict[str, Any]]:
    """Fetch tweets from multiple KOL Twitter profiles."""
    handles = (handles or DEFAULT_KOLS)[:max_profiles]
    all_tweets: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for handle in handles:
        page_content = fetch_twitter_profile_jina(handle, timeout=timeout)
        if not page_content:
            continue
        tweets = extract_tweets_from_page(page_content, handle)
        for tweet in tweets:
            tweet["fetched_at"] = now.isoformat()
        all_tweets.extend(tweets)

    return all_tweets


def to_narrative_format(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert KOL tweets to unified narrative monitor format."""
    rows = []
    for item in items:
        if item.get("error"):
            continue
        body = item.get("body", "")
        handle = item.get("author_handle", "unknown")
        rows.append({
            "body": body,
            "tags": [],
            "trading_pairs": [],
            "symbol_candidates": [],
            "author": f"@{handle}",
            "username": handle,
            "publish_time": "",
            "web_link": f"https://x.com/{handle}",
            "engagement_total": 0,
            "signal_score": 55,  # slightly higher baseline for KOL signals
            "signal_label": "KOL观点",
            "content_id": f"twitter_{handle}_{abs(hash(body[:80]))}",
            "_source": "twitter_kol",
        })
    return rows


if __name__ == "__main__":
    tweets = fetch_kol_tweets(max_profiles=3)
    print(json.dumps(tweets, ensure_ascii=False, indent=2))
