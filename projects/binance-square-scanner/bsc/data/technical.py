#!/usr/bin/env python3
"""技术指标数据源 — 从 Binance API 拉取 K 线并计算 RSI/MA/MACD/ATR。

免费、无需 API Key（公开接口）。
"""

from __future__ import annotations

import json
import math
import urllib.request
from typing import Any, Dict, List, Optional


def fetch_klines(symbol: str, interval: str = "1h", limit: int = 100) -> List[Dict[str, Any]]:
    """从 Binance 拉取 K 线数据。

    返回: [{open_time, open, high, low, close, volume, close_time}, ...]
    """
    sym = symbol.upper().replace("/", "").replace(":USDT", "")
    if not sym.endswith("USDT"):
        sym += "USDT"
    url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read())

    klines = []
    for k in raw:
        klines.append({
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": k[6],
        })
    return klines


def calc_sma(closes: List[float], period: int) -> Optional[float]:
    """简单移动平均线。"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_ema(closes: List[float], period: int) -> Optional[float]:
    """指数移动平均线。"""
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """相对强弱指数。"""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[Dict[str, float]]:
    """MACD 指标。"""
    if len(closes) < slow + signal:
        return None

    def _ema(data: List[float], period: int) -> List[float]:
        k = 2 / (period + 1)
        result = [sum(data[:period]) / period]
        for d in data[period:]:
            result.append(d * k + result[-1] * (1 - k))
        return result

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    # 对齐长度
    offset = slow - fast
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    signal_line = _ema(macd_line, signal)
    histogram = [macd_line[i + len(macd_line) - len(signal_line)] - signal_line[i]
                 for i in range(len(signal_line))]

    return {
        "macd": macd_line[-1],
        "signal": signal_line[-1],
        "histogram": histogram[-1],
    }


def calc_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """平均真实波幅。"""
    if len(highs) < period + 1:
        return None
    trs = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def calc_bollinger(closes: List[float], period: int = 20, std_dev: int = 2) -> Optional[Dict[str, float]]:
    """布林带。"""
    if len(closes) < period:
        return None
    recent = closes[-period:]
    sma = sum(recent) / period
    variance = sum((x - sma) ** 2 for x in recent) / period
    std = math.sqrt(variance)
    return {
        "upper": sma + std_dev * std,
        "middle": sma,
        "lower": sma - std_dev * std,
        "width_pct": (2 * std_dev * std / sma * 100) if sma > 0 else 0,
    }


def calc_volume_trend(volumes: List[float], period: int = 20) -> Optional[Dict[str, Any]]:
    """成交量趋势分析。"""
    if len(volumes) < period:
        return None
    recent_vol = volumes[-1]
    avg_vol = sum(volumes[-period:]) / period
    ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

    if ratio > 2.0:
        trend = "异常放量"
    elif ratio > 1.3:
        trend = "温和放量"
    elif ratio < 0.5:
        trend = "极度缩量"
    elif ratio < 0.8:
        trend = "温和缩量"
    else:
        trend = "正常"

    return {
        "current": recent_vol,
        "avg_20": avg_vol,
        "ratio": round(ratio, 2),
        "trend": trend,
    }


def compute_technical_signals(symbol: str) -> Dict[str, Any]:
    """计算币种的完整技术指标。"""
    result = {
        "symbol": symbol.upper(),
        "source": "binance_technical",
        "indicators": {},
        "signals": [],
        "bias": "neutral",
    }

    # 1h K线 (主要分析)
    try:
        klines_1h = fetch_klines(symbol, "1h", 100)
    except Exception as e:
        result["error"] = str(e)
        return result

    closes = [k["close"] for k in klines_1h]
    highs = [k["high"] for k in klines_1h]
    lows = [k["low"] for k in klines_1h]
    volumes = [k["volume"] for k in klines_1h]
    current_price = closes[-1]

    indicators = result["indicators"]
    signals = result["signals"]
    bullish_pts = 0
    bearish_pts = 0

    # RSI
    rsi = calc_rsi(closes, 14)
    if rsi is not None:
        indicators["rsi_1h"] = round(rsi, 1)
        if rsi < 30:
            signals.append(f"RSI={rsi:.0f} 超卖")
            bullish_pts += 3
        elif rsi < 40:
            signals.append(f"RSI={rsi:.0f} 偏弱但未超卖")
            bullish_pts += 1
        elif rsi > 70:
            signals.append(f"RSI={rsi:.0f} 超买⚠️")
            bearish_pts += 3
        elif rsi > 60:
            signals.append(f"RSI={rsi:.0f} 偏强")
            bearish_pts += 1

    # MA 均线
    ma7 = calc_sma(closes, 7)
    ma25 = calc_sma(closes, 25)
    ma99 = calc_sma(closes, 99)
    if ma7:
        indicators["ma7"] = round(ma7, 4)
    if ma25:
        indicators["ma25"] = round(ma25, 4)
    if ma99:
        indicators["ma99"] = round(ma99, 4)

    if ma7 and ma25:
        if ma7 > ma25:
            signals.append("MA7 > MA25（短期多头排列）")
            bullish_pts += 2
        else:
            signals.append("MA7 < MA25（短期空头排列）")
            bearish_pts += 2

    if ma25 and ma99:
        if ma25 > ma99:
            signals.append("MA25 > MA99（中期多头）")
            bullish_pts += 2
        else:
            signals.append("MA25 < MA99（中期空头）")
            bearish_pts += 2

    # 价格相对均线位置
    if ma25 and current_price:
        dist_pct = (current_price - ma25) / ma25 * 100
        indicators["price_vs_ma25_pct"] = round(dist_pct, 2)
        if dist_pct > 5:
            signals.append(f"价格高于MA25 {dist_pct:.1f}%（偏高）")
            bearish_pts += 1
        elif dist_pct < -5:
            signals.append(f"价格低于MA25 {dist_pct:.1f}%（偏低）")
            bullish_pts += 1

    # MACD
    macd = calc_macd(closes)
    if macd:
        indicators["macd"] = round(macd["macd"], 4)
        indicators["macd_signal"] = round(macd["signal"], 4)
        indicators["macd_histogram"] = round(macd["histogram"], 4)
        if macd["histogram"] > 0 and macd["macd"] > macd["signal"]:
            signals.append("MACD 金叉（看多）")
            bullish_pts += 2
        elif macd["histogram"] < 0 and macd["macd"] < macd["signal"]:
            signals.append("MACD 死叉（看空）")
            bearish_pts += 2

    # ATR 波动率
    atr = calc_atr(highs, lows, closes, 14)
    if atr and current_price:
        atr_pct = atr / current_price * 100
        indicators["atr_1h"] = round(atr, 4)
        indicators["atr_pct"] = round(atr_pct, 2)
        if atr_pct > 3:
            signals.append(f"ATR={atr_pct:.1f}% 高波动⚠️")
        elif atr_pct < 0.5:
            signals.append(f"ATR={atr_pct:.1f}% 低波动")

    # 布林带
    bb = calc_bollinger(closes)
    if bb:
        indicators["bb_upper"] = round(bb["upper"], 4)
        indicators["bb_middle"] = round(bb["middle"], 4)
        indicators["bb_lower"] = round(bb["lower"], 4)
        indicators["bb_width_pct"] = round(bb["width_pct"], 2)

        bb_pos = (current_price - bb["lower"]) / (bb["upper"] - bb["lower"]) if bb["upper"] != bb["lower"] else 0.5
        indicators["bb_position"] = round(bb_pos, 2)
        if bb_pos > 0.95:
            signals.append("触及布林上轨（超买）")
            bearish_pts += 1
        elif bb_pos < 0.05:
            signals.append("触及布林下轨（超卖）")
            bullish_pts += 1

    # 成交量趋势
    vol_trend = calc_volume_trend(volumes)
    if vol_trend:
        indicators["volume_trend"] = vol_trend
        if vol_trend["trend"] in ("异常放量", "温和放量"):
            signals.append(f"成交量{vol_trend['trend']}（x{vol_trend['ratio']}）")

    # 综合 bias
    if bullish_pts > bearish_pts + 2:
        result["bias"] = "bullish"
    elif bearish_pts > bullish_pts + 2:
        result["bias"] = "bearish"
    else:
        result["bias"] = "neutral"

    result["bullish_pts"] = bullish_pts
    result["bearish_pts"] = bearish_pts
    return result


if __name__ == "__main__":
    import json as _json
    for sym in ["BTC", "ETH", "SOL"]:
        print(f"\n=== {sym} ===")
        sig = compute_technical_signals(sym)
        print(_json.dumps(sig, indent=2, ensure_ascii=False))
