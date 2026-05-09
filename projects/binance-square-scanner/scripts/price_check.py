#!/usr/bin/env python3
"""开仓前价格校验 — 对比 Binance testnet 与 Hyperliquid DEX 价格。

用法：
  python3 price_check.py BTC          # 检查 BTC 价格
  python3 price_check.py BTC ETH SOL  # 批量检查
  python3 price_check.py BTC --open   # 校验通过后开仓
"""

import json
import sys
import urllib.request
from typing import Any, Dict, Optional


def get_binance_testnet_price(symbol: str) -> Optional[float]:
    """从 Binance testnet 获取价格。"""
    sym = symbol.upper().replace("/", "").replace(":USDT", "")
    if not sym.endswith("USDT"):
        sym += "USDT"
    url = f"https://testnet.binancefuture.com/fapi/v1/ticker/price?symbol={sym}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return float(data["price"])
    except Exception as e:
        return None


def get_hyperliquid_price(symbol: str) -> Optional[float]:
    """从 Hyperliquid 获取价格。"""
    sym = symbol.upper().replace("USDT", "").replace("/", "")
    url = "https://api.hyperliquid.xyz/info"
    payload = json.dumps({"type": "metaAndAssetCtxs"}).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read())
            meta, ctxs = raw[0], raw[1]
            for i, ctx in enumerate(ctxs):
                name = meta["universe"][i]["name"] if i < len(meta.get("universe", [])) else ""
                if name.upper() == sym:
                    px = ctx.get("markPx")
                    return float(px) if px is not None else None
    except Exception:
        return None
    return None


def check_price(symbol: str) -> Dict[str, Any]:
    """对比两个数据源的价格。"""
    binance_px = get_binance_testnet_price(symbol)
    hl_px = get_hyperliquid_price(symbol)

    result = {
        "symbol": symbol.upper(),
        "binance_testnet": binance_px,
        "hyperliquid": hl_px,
        "status": "unknown",
        "spread_pct": None,
        "verdict": "",
    }

    if binance_px and hl_px:
        spread = abs(binance_px - hl_px) / min(binance_px, hl_px) * 100
        result["spread_pct"] = round(spread, 4)
        if spread < 0.5:
            result["status"] = "ok"
            result["verdict"] = f"✅ 价格一致 (spread {spread:.2f}%)"
        elif spread < 2.0:
            result["status"] = "warning"
            result["verdict"] = f"⚠️ 价格偏差 {spread:.2f}%，建议谨慎"
        else:
            result["status"] = "danger"
            result["verdict"] = f"❌ 价格偏差过大 {spread:.2f}%，暂不开仓"
    elif binance_px and not hl_px:
        result["status"] = "ok"
        result["verdict"] = "✅ 仅 Binance 数据可用，使用 Binance 价格"
    elif hl_px and not binance_px:
        result["status"] = "warning"
        result["verdict"] = "⚠️ 仅 Hyperliquid 数据可用"
    else:
        result["status"] = "error"
        result["verdict"] = "❌ 两个数据源均不可用"

    return result


def main():
    if len(sys.argv) < 2:
        print("用法: python3 price_check.py <symbol> [symbol2 ...]")
        sys.exit(1)

    symbols = [s for s in sys.argv[1:] if not s.startswith("--")]

    print("🔍 价格校验中...\n")
    all_ok = True

    for sym in symbols:
        r = check_price(sym)
        bpx = f"${r['binance_testnet']:,.2f}" if r['binance_testnet'] else "N/A"
        hpx = f"${r['hyperliquid']:,.2f}" if r['hyperliquid'] else "N/A"
        print(f"{r['symbol']:10s} | Binance: {bpx:>14s} | Hyperliquid: {hpx:>14s} | {r['verdict']}")
        if r['status'] not in ('ok',):
            all_ok = False

    print()
    if all_ok:
        print("✅ 所有价格校验通过，可以开仓")
        sys.exit(0)
    else:
        print("⚠️ 存在价格偏差，请人工确认")
        sys.exit(1)


if __name__ == "__main__":
    main()
