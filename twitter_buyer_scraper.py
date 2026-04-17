#!/usr/bin/env python3
import csv
import json
import os
import re
import subprocess
from collections import OrderedDict
from datetime import datetime
from urllib.parse import quote

OUTPUT_DIR = "/root/.openclaw/workspace/results"
LOG_DIR = "/root/.openclaw/workspace/logs"
DATE = datetime.now().strftime("%Y-%m-%d")
CSV_FILE = os.path.join(OUTPUT_DIR, f"gift_card_buyers_{DATE}.csv")
LOG_FILE = os.path.join(LOG_DIR, f"buyer_scraper_{datetime.now().strftime('%Y%m%d')}.log")

SEARCH_QUERIES = [
    "need gift card razer gold",
    "want to buy gift card apple",
    "looking for gift card supplier",
    "gift card wholesale buyer",
    "bulk gift card purchase",
    "gift card reseller Vietnam",
    "gift card buyer Malaysia",
    "razer gold supplier Thailand",
    "steam wallet buyer Indonesia",
    "amazon gift card Philippines",
]

BUY_INTENT_PATTERNS = [
    r"\bneed\b",
    r"\bwant(?:\s+to)?\s+buy\b",
    r"\blooking\s+for\b",
    r"\bbuy(?:ing)?\b",
    r"\bsupplier\b",
    r"\bwholesale\b",
    r"\breseller\b",
    r"\bbulk\b",
    r"\bmarket\s*rate\b",
]

NEGATIVE_PATTERNS = [
    r"realtor",
    r"seller referral",
    r"prize draw",
    r"restaurant",
    r"tesla",
    r"jesus christ",
]


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(query: str, api_key: str) -> str:
    url = f"https://readx.cc/consumer/Search?q={quote(query)}&api_key={api_key}&count=20"
    cmd = ["curl", "-ks", url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"curl failed: {result.returncode}")
    return result.stdout


def extract_entries(payload: dict):
    timeline = (((payload.get("data") or {}).get("search_by_raw_query") or {}).get("search_timeline") or {}).get("timeline") or {}
    for instruction in timeline.get("instructions", []):
        for entry in instruction.get("entries", []):
            content = entry.get("content") or {}
            item = content.get("content") or content.get("itemContent") or {}
            tweet_wrap = item.get("tweet_results") or {}
            tweet = tweet_wrap.get("result")
            if tweet:
                yield tweet


def get_user_info(tweet: dict):
    user = ((((tweet.get("core") or {}).get("user_results") or {}).get("result")) or {})
    legacy = user.get("legacy") or {}
    core = user.get("core") or {}
    location = (user.get("location") or {}).get("location") or legacy.get("location", "")
    return {
        "username": core.get("screen_name") or legacy.get("screen_name", ""),
        "display_name": core.get("name") or legacy.get("name", ""),
        "location": location,
        "followers": (user.get("relationship_counts") or {}).get("followers") or legacy.get("followers_count", 0) or 0,
    }


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()


def is_buyer_tweet(text: str) -> bool:
    t = text.lower()
    if not any(re.search(p, t) for p in BUY_INTENT_PATTERNS):
        return False
    if any(re.search(p, t) for p in NEGATIVE_PATTERNS):
        return False
    return any(k in t for k in ["gift card", "razer", "steam wallet", "apple gift", "amazon gift"])


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    api_key = os.getenv("READX_API_KEY")
    if not api_key:
        env_path = "/root/.openclaw/workspace/.env"
        if os.path.exists(env_path):
            for line in open(env_path, encoding="utf-8"):
                if line.startswith("READX_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
    if not api_key:
        raise SystemExit("READX_API_KEY not found")

    rows = OrderedDict()
    with open(CSV_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "query", "username", "display_name", "tweet_text", "created_at", "favorites", "location", "followers", "tweet_id"])

        for query in SEARCH_QUERIES:
            log(f"搜索: {query}")
            try:
                raw = fetch(query, api_key)
                payload = json.loads(raw)
            except Exception as e:
                log(f"请求失败: {query} | {e}")
                continue

            count = 0
            for tweet in extract_entries(payload):
                legacy = tweet.get("legacy") or {}
                text = normalize_text(legacy.get("full_text", ""))
                if not text or not is_buyer_tweet(text):
                    continue
                user = get_user_info(tweet)
                tweet_id = tweet.get("rest_id") or legacy.get("id_str", "")
                key = tweet_id or f"{user['username']}::{text[:80]}"
                if key in rows:
                    continue
                row = [
                    datetime.now().isoformat(timespec="seconds"),
                    query,
                    user["username"],
                    user["display_name"],
                    text,
                    legacy.get("created_at", ""),
                    legacy.get("favorite_count", 0),
                    user["location"],
                    user["followers"],
                    tweet_id,
                ]
                rows[key] = row
                writer.writerow(row)
                count += 1
            log(f"找到 {count} 条相关推文")

    log(f"抓取完成，总计 {len(rows)} 条真实广场候选数据，输出: {CSV_FILE}")

if __name__ == "__main__":
    main()
