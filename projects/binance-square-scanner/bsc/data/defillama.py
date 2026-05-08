#!/usr/bin/env python3
"""DefiLlama TVL 异动数据源 — 免费公开 API，无需 key。"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

BASE = "https://api.llama.fi"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Map DefiLlama categories/protocols to our narrative labels
CATEGORY_TO_NARRATIVE = {
    "Liquid Staking": "DeFi",
    "Lending": "DeFi",
    "DEXes": "DeFi",
    "Yield": "DeFi",
    "Derivatives": "DeFi",
    "CDP": "DeFi",
    "Restaking": "DeFi",
    "Bridge": "DeFi",
    "Liquid Staking": "DeFi",
    "Chain": "L2",
    "Rollup": "L2",
    "Layer 2": "L2",
    "Gaming": "GameFi",
    "GameFi": "GameFi",
    "Metaverse": "GameFi",
    "RWA": "RWA",
    "AI": "AI",
    "Infrastructure": "DePIN",
    "DePIN": "DePIN",
    "Oracle": "DePIN",
    "Meme": "Meme",
}

# Common token symbols to USDT pair mapping
SYMBOL_MAP = {
    "AAVE": "AAVEUSDT", "UNI": "UNIUSDT", "LDO": "LDOUSDT", "MKR": "MKRUSDT",
    "CAKE": "CAKEUSDT", "SOL": "SOLUSDT", "OP": "OPUSDT", "ARB": "ARBUSDT",
    "DOGE": "DOGEUSDT", "PEPE": "PEPEUSDT", "WIF": "WIFUSDT", "BONK": "BONKUSDT",
    "RENDER": "RENDERUSDT", "FET": "FETUSDT", "TAO": "TAOUSDT",
    "ONDO": "ONDOUSDT", "OM": "OMUSDT", "HNT": "HNTUSDT", "AKT": "AKTUSDT",
    "GALA": "GALAUSDT", "IMX": "IMXUSDT", "STRK": "STRKUSDT",
    "ETHFI": "ETHFIUSDT", "JUP": "JUPUSDT", "RAY": "RAYUSDT",
    "BNB": "BNBUSDT", "ORDI": "ORDIUSDT",
}


def fetch_protocols() -> List[Dict[str, Any]]:
    """Fetch all protocols with TVL data."""
    try:
        resp = requests.get(f"{BASE}/protocols", headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return [{"error": str(exc)}]


def fetch_chains_tvl() -> List[Dict[str, Any]]:
    """Fetch TVL by chain."""
    try:
        resp = requests.get(f"{BASE}/v2/chains", headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return [{"error": str(exc)}]


def fetch_protocol_tvl(slug: str) -> Optional[float]:
    """Fetch current TVL for a single protocol."""
    try:
        resp = requests.get(f"{BASE}/tvl/{slug}", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return float(resp.text)
    except Exception:
        return None


def fetch_protocol_history(slug: str) -> Optional[List[Dict[str, Any]]]:
    """Fetch TVL history for a protocol (last 30d)."""
    try:
        resp = requests.get(f"{BASE}/protocol/{slug}", headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("tvl", [])
    except Exception:
        return None


def detect_tvl_changes(
    protocols: List[Dict[str, Any]],
    change_threshold_pct: float = 15.0,
    min_tvl_usd: float = 10_000_000,
    top_n: int = 30,
) -> List[Dict[str, Any]]:
    """Detect protocols with significant TVL changes (24h)."""
    changes: List[Dict[str, Any]] = []
    for p in protocols:
        if isinstance(p, dict) and p.get("error"):
            continue
        name = p.get("name", "")
        slug = p.get("slug", "")
        tvl = p.get("tvl") or 0
        change_1d = p.get("change_1d")
        change_7d = p.get("change_7d")
        category = p.get("category", "")
        symbol = (p.get("symbol") or "").upper()
        chains = p.get("chains", [])

        if tvl < min_tvl_usd:
            continue
        if change_1d is None:
            continue

        try:
            change_1d = float(change_1d)
        except (ValueError, TypeError):
            continue

        if abs(change_1d) < change_threshold_pct:
            continue

        # Map to narrative
        narrative = CATEGORY_TO_NARRATIVE.get(category, "")
        # Also check name for narrative keywords
        name_lower = name.lower()
        if not narrative:
            if any(k in name_lower for k in ("ai", "agent", "bittensor")):
                narrative = "AI"
            elif any(k in name_lower for k in ("meme", "doge", "pepe")):
                narrative = "Meme"
            elif any(k in name_lower for k in ("game", "gaming", "metaverse")):
                narrative = "GameFi"
            elif any(k in name_lower for k in ("depin", "storage", "wireless")):
                narrative = "DePIN"
            elif any(k in name_lower for k in ("rwa", "tokenize")):
                narrative = "RWA"

        symbol_pair = SYMBOL_MAP.get(symbol, f"{symbol}USDT" if symbol else "")

        changes.append({
            "name": name,
            "slug": slug,
            "symbol": symbol,
            "symbol_pair": symbol_pair,
            "tvl": tvl,
            "change_1d_pct": round(change_1d, 2),
            "change_7d_pct": round(float(change_7d), 2) if change_7d else None,
            "category": category,
            "narrative": narrative,
            "chains": chains[:3],
            "source": "defillama",
        })

    changes.sort(key=lambda x: abs(x["change_1d_pct"]), reverse=True)
    return changes[:top_n]


def to_narrative_format(changes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert TVL changes to unified narrative monitor format."""
    rows = []
    for item in changes:
        if item.get("error"):
            continue
        direction = "流入" if item["change_1d_pct"] > 0 else "流出"
        tvl_b = item["tvl"] / 1e9
        body = (
            f"DefiLlama 数据：{item['name']}（{item['symbol']}）"
            f"TVL ${tvl_b:.2f}B，24h {direction} {abs(item['change_1d_pct'])}%"
            f"，类别 {item['category']}"
        )
        if item.get("change_7d_pct") is not None:
            body += f"，7d 变化 {item['change_7d_pct']}%"
        body += f"，部署链: {', '.join(item.get('chains', []))}"

        symbols = []
        if item.get("symbol_pair"):
            symbols.append(item["symbol_pair"])

        rows.append({
            "body": body,
            "tags": [item.get("category", ""), item.get("narrative", "")],
            "trading_pairs": symbols,
            "symbol_candidates": symbols,
            "author": "DefiLlama",
            "username": "defillama",
            "publish_time": "",
            "web_link": f"https://defillama.com/protocol/{item.get('slug', '')}",
            "engagement_total": int(abs(item["change_1d_pct"]) * 10),  # scale as pseudo-engagement
            "signal_score": min(50 + abs(item["change_1d_pct"]) * 2, 95),
            "signal_label": f"TVL异动{'↑' if item['change_1d_pct'] > 0 else '↓'}",
            "content_id": f"defillama_{item.get('slug', '')}",
            "_source": "defillama",
        })
    return rows
