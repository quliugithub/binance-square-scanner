#!/usr/bin/env python3
"""Hyperliquid DEX 永续合约数据源。

免费、无需 API Key，覆盖 230+ 交易对。
提供：资金费率、持仓量、24h 交易量、溢价、标记价。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import urllib.request

API_URL = "https://api.hyperliquid.xyz/info"


def _safe_float(val: Any, default: float = 0.0) -> float:
    """安全转换浮点数，处理 None 和无效值。"""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _post_info(payload: dict, timeout: int = 15) -> Any:
    """调用 Hyperliquid info API。"""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_all_assets() -> List[Dict[str, Any]]:
    """获取所有永续合约的实时数据。"""
    raw = _post_info({"type": "metaAndAssetCtxs"})
    meta, ctxs = raw[0], raw[1]
    results = []
    for i, ctx in enumerate(ctxs):
        name = meta["universe"][i]["name"] if i < len(meta.get("universe", [])) else f"UNKNOWN_{i}"
        results.append({
            "name": name,
            "mark_px": _safe_float(ctx.get("markPx")),
            "oracle_px": _safe_float(ctx.get("oraclePx")),
            "mid_px": _safe_float(ctx.get("midPx")),
            "funding": _safe_float(ctx.get("funding")),
            "open_interest": _safe_float(ctx.get("openInterest")),
            "prev_day_px": _safe_float(ctx.get("prevDayPx")),
            "day_ntl_vlm": _safe_float(ctx.get("dayNtlVlm")),
            "day_base_vlm": _safe_float(ctx.get("dayBaseVlm")),
            "premium": _safe_float(ctx.get("premium")),
            "impact_pxs": ctx.get("impactPxs", []),
        })
    return results


def fetch_asset(symbol: str) -> Optional[Dict[str, Any]]:
    """获取单个交易对数据。symbol 如 'BTC', 'ETH'。"""
    all_assets = fetch_all_assets()
    sym_upper = symbol.upper().replace("USDT", "")
    for a in all_assets:
        if a["name"].upper() == sym_upper:
            return a
    return None


def to_futures_signal(asset: Dict[str, Any]) -> Dict[str, Any]:
    """将 Hyperliquid 原始数据转换为评分系统可用的信号格式。"""
    name = asset["name"]
    funding = asset["funding"]
    premium = asset["premium"]
    oi = asset["open_interest"]
    vol_24h = asset["day_ntl_vlm"]
    mark = asset["mark_px"]
    prev = asset["prev_day_px"]

    # 价格变化
    price_change_pct = ((mark - prev) / prev * 100) if prev > 0 else 0

    # 资金费率信号
    if funding > 0.001:
        funding_signal = "high_positive"
    elif funding > 0.0003:
        funding_signal = "positive"
    elif funding < -0.0003:
        funding_signal = "negative"
    elif funding < -0.001:
        funding_signal = "high_negative"
    else:
        funding_signal = "neutral"

    # 溢价信号
    if premium > 0.005:
        premium_signal = "high_premium"
    elif premium < -0.005:
        premium_signal = "high_discount"
    else:
        premium_signal = "neutral"

    # 综合 bias
    bullish_score = 0
    bearish_score = 0

    if funding < -0.0003:
        bullish_score += 2
    elif funding > 0.001:
        bearish_score += 2

    if premium < -0.003:
        bullish_score += 1
    elif premium > 0.003:
        bearish_score += 1

    if price_change_pct > 3:
        bullish_score += 1
    elif price_change_pct < -3:
        bearish_score += 1

    if bullish_score > bearish_score + 1:
        bias = "bullish"
    elif bearish_score > bullish_score + 1:
        bias = "bearish"
    else:
        bias = "neutral"

    return {
        "symbol": f"{name}USDT",
        "source": "hyperliquid",
        "mark_px": mark,
        "oracle_px": asset["oracle_px"],
        "mid_px": asset["mid_px"],
        "funding_rate": funding,
        "funding_rate_pct": f"{funding * 100:.4f}%",
        "funding_signal": funding_signal,
        "premium": premium,
        "premium_signal": premium_signal,
        "open_interest": oi,
        "day_volume_usd": vol_24h,
        "day_volume_base": asset["day_base_vlm"],
        "price_change_24h_pct": round(price_change_pct, 2),
        "bias": bias,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_futures_signals(symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """批量获取合约信号。

    Args:
        symbols: 要获取的交易对列表，如 ['BTC', 'ETH']。None 表示全部。

    Returns:
        {symbol: signal_dict}
    """
    all_assets = fetch_all_assets()
    signals = {}

    for asset in all_assets:
        name = asset["name"]
        if symbols:
            matched = any(
                s.upper().replace("USDT", "") == name.upper()
                for s in symbols
            )
            if not matched:
                continue
        sig = to_futures_signal(asset)
        signals[sig["symbol"]] = sig

    return signals


def get_top_by_volume(n: int = 20) -> List[Dict[str, Any]]:
    """按 24h 交易量排序取 top N。"""
    all_assets = fetch_all_assets()
    sorted_assets = sorted(all_assets, key=lambda x: x["day_ntl_vlm"], reverse=True)
    return [to_futures_signal(a) for a in sorted_assets[:n]]


if __name__ == "__main__":
    import json as _json
    print("=== BTC ===")
    btc = fetch_asset("BTC")
    if btc:
        print(_json.dumps(to_futures_signal(btc), indent=2, ensure_ascii=False))

    print("\n=== Top 10 by Volume ===")
    top = get_top_by_volume(10)
    for sig in top:
        vol_m = sig["day_volume_usd"] / 1e6
        print(f"{sig['symbol']:12s} | ${vol_m:>8,.0f}M | funding={sig['funding_rate_pct']:>8s} | bias={sig['bias']}")
