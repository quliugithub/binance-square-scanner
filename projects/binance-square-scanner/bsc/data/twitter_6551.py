#!/usr/bin/env python3
"""OpenTwitter 数据源 — 通过 6551.io API 获取 Twitter 数据。

数据来源：https://ai.6551.io (需 API Token)
申请地址：https://6551.io/mcp

环境变量：
  TWITTER_TOKEN — API Bearer Token

免费额度请在 6551.io/mcp 查看。
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

API_BASE = os.environ.get("API_6551_BASE_URL", "https://ai.6551.io").rstrip("/")
# Fix: api.6551.com SSL issues, default to ai.6551.io
if "api.6551.com" in API_BASE:
    API_BASE = "https://ai.6551.io"
TIMEOUT = 25

# ── 默认 KOL 列表 ──────────────────────────────────────────
DEFAULT_KOLS = [
    "Pentoshi1337", "CryptoMichNL", "TheDeFiEdge", "CryptoHayes",
    "GaborGurbacs", "sassal0x", "aeyakovenko", "VitalikButerin",
    "anatoly_solana", "0xfoobar", "0xminion", "wojak_anon",
]


def _get_token() -> Optional[str]:
    return os.environ.get("TWITTER_TOKEN") or os.environ.get("API_6551_KEY")


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post(endpoint: str, body: dict, token: str) -> Optional[dict]:
    """Make a POST request to the 6551 API."""
    try:
        resp = requests.post(
            f"{API_BASE}{endpoint}",
            headers=_headers(token),
            json=body,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"error": str(exc)}


# ── API 调用 ──────────────────────────────────────────────

def get_user_info(username: str, token: str) -> Optional[dict]:
    """获取 Twitter 用户资料。"""
    return _post("/open/twitter_user_info", {"username": username}, token)


def get_user_tweets(
    username: str,
    token: str,
    max_results: int = 15,
    include_replies: bool = False,
    include_retweets: bool = False,
) -> List[Dict[str, Any]]:
    """获取用户最新推文。"""
    result = _post("/open/twitter_user_tweets", {
        "username": username,
        "maxResults": max_results,
        "product": "Latest",
        "includeReplies": include_replies,
        "includeRetweets": include_retweets,
    }, token)
    if not result or result.get("error"):
        return []
    # API returns {"tweets": [...]} or a list
    tweets = result.get("tweets") or result.get("data") or result
    if isinstance(tweets, dict):
        tweets = tweets.get("tweets") or tweets.get("data") or []
    return tweets if isinstance(tweets, list) else []


def search_tweets(
    token: str,
    keywords: Optional[str] = None,
    from_user: Optional[str] = None,
    hashtag: Optional[str] = None,
    min_likes: int = 0,
    min_retweets: int = 0,
    max_results: int = 20,
    since_date: Optional[str] = None,
    lang: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """搜索推文，支持互动量过滤。"""
    body: Dict[str, Any] = {
        "maxResults": max_results,
        "product": "Latest",
    }
    if keywords:
        body["keywords"] = keywords
    if from_user:
        body["fromUser"] = from_user
    if hashtag:
        body["hashtag"] = hashtag
    if min_likes > 0:
        body["minLikes"] = min_likes
    if min_retweets > 0:
        body["minRetweets"] = min_retweets
    if since_date:
        body["sinceDate"] = since_date
    if lang:
        body["lang"] = lang

    result = _post("/open/twitter_search", body, token)
    if not result or result.get("error"):
        return []
    tweets = result.get("tweets") or result.get("data") or result
    if isinstance(tweets, dict):
        tweets = tweets.get("tweets") or tweets.get("data") or []
    return tweets if isinstance(tweets, list) else []


# ── 高层封装 ──────────────────────────────────────────────

def fetch_kol_tweets(
    token: Optional[str] = None,
    handles: Optional[List[str]] = None,
    max_profiles: int = 8,
    tweets_per_profile: int = 8,
) -> List[Dict[str, Any]]:
    """批量获取 KOL 推文。"""
    token = token or _get_token()
    if not token:
        return [{"error": "TWITTER_TOKEN not set"}]

    handles = (handles or DEFAULT_KOLS)[:max_profiles]
    all_tweets: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for handle in handles:
        tweets = get_user_tweets(handle, token, max_results=tweets_per_profile)
        for tw in tweets:
            tw["_kol_handle"] = handle
            tw["_fetched_at"] = now.isoformat()
        all_tweets.extend(tweets)

    return all_tweets


def fetch_crypto_search(
    token: Optional[str] = None,
    queries: Optional[List[str]] = None,
    min_likes: int = 20,
    max_results: int = 15,
) -> List[Dict[str, Any]]:
    """搜索近期加密货币相关推文。"""
    token = token or _get_token()
    if not token:
        return [{"error": "TWITTER_TOKEN not set"}]

    queries = queries or [
        "crypto bullish altcoin",
        "bitcoin funding rate",
        "solana defi TVL",
        "RWA tokenization",
        "AI crypto narrative",
    ]

    all_tweets: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for q in queries[:4]:
        tweets = search_tweets(
            token,
            keywords=q,
            min_likes=min_likes,
            max_results=max_results,
        )
        for tw in tweets:
            tw["_search_query"] = q
            tw["_fetched_at"] = now.isoformat()
        all_tweets.extend(tweets)

    return all_tweets


def to_narrative_format(tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将推文转换为叙事监控统一格式。"""
    rows = []
    for tw in tweets:
        if tw.get("error"):
            continue
        body = (
            tw.get("text")
            or tw.get("full_text")
            or tw.get("content")
            or tw.get("body")
            or ""
        )
        if len(body) < 10:
            continue

        author = (
            tw.get("_kol_handle")
            or tw.get("userScreenName")
            or tw.get("author", {}).get("username")
            or tw.get("user", {}).get("screen_name")
            or "unknown"
        )
        # 6551 API uses camelCase fields
        likes = tw.get("favoriteCount") or tw.get("like_count") or tw.get("favorite_count") or 0
        retweets = tw.get("retweetCount") or tw.get("retweet_count") or 0
        replies = tw.get("replyCount") or tw.get("reply_count") or 0
        views = tw.get("viewCount") or tw.get("view_count") or 0
        engagement = int(likes) + int(retweets) * 2 + int(replies)

        # Signal score: base 50 + engagement weight + virality bonus
        import math
        signal_score = min(50 + math.log1p(engagement) * 5 + math.log1p(int(views)) * 2, 95)

        tweet_id = tw.get("id") or tw.get("id_str") or ""
        tw_url = f"https://x.com/{author}/status/{tweet_id}" if tweet_id else f"https://x.com/{author}"

        rows.append({
            "body": body[:800],
            "tags": [],
            "trading_pairs": [],
            "symbol_candidates": [],
            "author": f"@{author}",
            "username": author,
            "publish_time": tw.get("created_at") or tw.get("timestamp") or "",
            "web_link": tw_url,
            "engagement_total": engagement,
            "signal_score": round(signal_score, 1),
            "signal_label": "Twitter KOL",
            "content_id": f"twitter_{tweet_id or hash(body[:80])}",
            "_source": "twitter_6551",
            # Extra fields for hotspot radar
            "like_count": int(likes),
            "retweet_count": int(retweets),
            "reply_count": int(replies),
            "view_count": int(views),
        })
    return rows


if __name__ == "__main__":
    token = _get_token()
    if not token:
        print("Error: Set TWITTER_TOKEN environment variable")
        print("Get token at: https://6551.io/mcp")
    else:
        tweets = fetch_kol_tweets(token, max_profiles=3, tweets_per_profile=5)
        print(f"Fetched {len(tweets)} tweets")
        for tw in tweets[:5]:
            body = (tw.get("text") or tw.get("full_text") or "")[:120]
            handle = tw.get("_kol_handle", "?")
            print(f"  @{handle}: {body}")
