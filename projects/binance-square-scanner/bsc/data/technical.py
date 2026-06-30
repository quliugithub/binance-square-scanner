#!/usr/bin/env python3
"""技术指标数据源 — 从 Binance API 拉取 K 线并计算 RSI/MA/MACD/ATR。

免费、无需 API Key（公开接口）。
"""

from __future__ import annotations

import json
import math
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple


SPOT_KLINE_URL = "https://api.binance.com/api/v3/klines"
FUTURES_KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"
SPOT_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
FUTURES_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

_EXCHANGE_SYMBOL_CACHE: Optional[Dict[str, Set[str]]] = None
_EXCHANGE_SYMBOL_CACHE_ERROR: Optional[str] = None


def normalize_symbol(symbol: str) -> str:
    sym = symbol.upper().replace("/", "").replace(":USDT", "")
    if not sym.endswith("USDT"):
        sym += "USDT"
    return sym


def _fetch_exchange_symbols() -> Tuple[Dict[str, Set[str]], Optional[str]]:
    global _EXCHANGE_SYMBOL_CACHE, _EXCHANGE_SYMBOL_CACHE_ERROR
    if _EXCHANGE_SYMBOL_CACHE is not None:
        return _EXCHANGE_SYMBOL_CACHE, _EXCHANGE_SYMBOL_CACHE_ERROR

    supported: Dict[str, Set[str]] = {"spot": set(), "usdt_futures": set()}
    errors: Dict[str, str] = {}

    for market, url in (("spot", SPOT_EXCHANGE_INFO_URL), ("usdt_futures", FUTURES_EXCHANGE_INFO_URL)):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = json.loads(resp.read())
            supported[market] = {
                str(item.get("symbol", "")).upper()
                for item in raw.get("symbols", [])
                if item.get("status") == "TRADING" and item.get("symbol")
            }
        except Exception as exc:
            errors[f"{market}_exchange_info_error"] = str(exc)

    _EXCHANGE_SYMBOL_CACHE = supported
    _EXCHANGE_SYMBOL_CACHE_ERROR = "; ".join(f"{key}={value}" for key, value in errors.items()) or None
    return supported, _EXCHANGE_SYMBOL_CACHE_ERROR


def _get_supported_markets(symbol_pair: str) -> Tuple[Optional[List[str]], Optional[str]]:
    supported, precheck_error = _fetch_exchange_symbols()
    markets = [market for market, symbols in supported.items() if symbol_pair in symbols]
    if markets:
        return markets, precheck_error
    if precheck_error:
        return None, precheck_error
    return [], None


def is_symbol_supported(symbol_pair: str) -> Optional[bool]:
    markets, precheck_error = _get_supported_markets(normalize_symbol(symbol_pair))
    if markets is None and precheck_error:
        return None
    return bool(markets)


def fetch_klines(
    symbol: str,
    interval: str = "1h",
    limit: int = 100,
    market: str = "spot",
) -> List[Dict[str, Any]]:
    """从 Binance 拉取 K 线数据。

    返回: [{open_time, open, high, low, close, volume, close_time}, ...]
    """
    sym = normalize_symbol(symbol)
    base_url = FUTURES_KLINE_URL if market == "usdt_futures" else SPOT_KLINE_URL
    url = f"{base_url}?symbol={sym}&interval={interval}&limit={limit}"
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


def fetch_klines_with_fallback(
    symbol: str,
    interval: str = "1h",
    limit: int = 100,
) -> tuple[List[Dict[str, Any]], str, Dict[str, str]]:
    errors: Dict[str, str] = {}
    try:
        return fetch_klines(symbol, interval, limit, market="spot"), "spot", errors
    except Exception as exc:
        errors["spot_error"] = str(exc)

    try:
        return fetch_klines(symbol, interval, limit, market="usdt_futures"), "usdt_futures", errors
    except Exception as exc:
        errors["usdt_futures_error"] = str(exc)
        raise RuntimeError("; ".join(f"{key}={value}" for key, value in errors.items()))


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


def bias_from_points(bullish_pts: int, bearish_pts: int) -> str:
    if bullish_pts > bearish_pts + 2:
        return "bullish"
    if bearish_pts > bullish_pts + 2:
        return "bearish"
    return "neutral"


def analyze_klines(klines: List[Dict[str, Any]], interval: str) -> Dict[str, Any]:
    if not klines:
        raise ValueError(f"empty klines for {interval}")

    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    volumes = [k["volume"] for k in klines]
    current_price = closes[-1]

    indicators: Dict[str, Any] = {}
    signals: List[str] = []
    bullish_pts = 0
    bearish_pts = 0

    # RSI
    rsi = calc_rsi(closes, 14)
    if rsi is not None:
        indicators["rsi"] = round(rsi, 1)
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
        indicators["atr"] = round(atr, 4)
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

    return {
        "interval": interval,
        "indicators": indicators,
        "signals": signals,
        "bias": bias_from_points(bullish_pts, bearish_pts),
        "bullish_pts": bullish_pts,
        "bearish_pts": bearish_pts,
    }


def flatten_primary_indicators(primary: Dict[str, Any]) -> Dict[str, Any]:
    src = primary.get("indicators") or {}
    result = dict(src)
    if "rsi" in src:
        result["rsi_1h"] = src["rsi"]
    if "atr" in src:
        result["atr_1h"] = src["atr"]
    result.pop("rsi", None)
    result.pop("atr", None)
    return result


def build_multi_timeframe_summary(timeframes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    weights = {"15m": 0.7, "1h": 1.0, "4h": 1.3}
    weighted_score = 0.0
    weighted_bullish = 0.0
    weighted_bearish = 0.0
    valid: List[str] = []
    biases: Dict[str, str] = {}

    for interval, weight in weights.items():
        row = timeframes.get(interval) or {}
        if row.get("error"):
            continue
        bullish = float(row.get("bullish_pts") or 0)
        bearish = float(row.get("bearish_pts") or 0)
        weighted_bullish += bullish * weight
        weighted_bearish += bearish * weight
        weighted_score += (bullish - bearish) * weight
        valid.append(interval)
        biases[interval] = str(row.get("bias") or "neutral")

    if weighted_score >= 3:
        composite_bias = "bullish"
    elif weighted_score <= -3:
        composite_bias = "bearish"
    else:
        composite_bias = "neutral"

    direction_votes = {
        "bullish": sum(1 for bias in biases.values() if bias == "bullish"),
        "bearish": sum(1 for bias in biases.values() if bias == "bearish"),
        "neutral": sum(1 for bias in biases.values() if bias == "neutral"),
    }
    if biases.get("1h") == biases.get("4h") and biases.get("1h") in {"bullish", "bearish"}:
        alignment = f"{biases['1h']}_1h_4h_aligned"
    elif direction_votes["bullish"] and direction_votes["bearish"]:
        alignment = "mixed"
    else:
        alignment = "weak_or_neutral"

    if abs(weighted_score) >= 6 and alignment.endswith("_aligned"):
        confidence = "high"
    elif abs(weighted_score) >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "valid_timeframes": valid,
        "timeframe_biases": biases,
        "direction_votes": direction_votes,
        "weighted_score": round(weighted_score, 2),
        "weighted_bullish_pts": round(weighted_bullish, 2),
        "weighted_bearish_pts": round(weighted_bearish, 2),
        "composite_bias": composite_bias,
        "alignment": alignment,
        "confidence": confidence,
    }


def compute_technical_signals(symbol: str) -> Dict[str, Any]:
    """计算币种的完整技术指标。"""
    symbol_pair = normalize_symbol(symbol)
    result = {
        "symbol": symbol.upper(),
        "symbol_pair": symbol_pair,
        "source": "binance_technical",
        "kline_market": "unknown",
        "indicators": {},
        "signals": [],
        "bias": "neutral",
    }

    supported_markets, support_precheck_error = _get_supported_markets(symbol_pair)
    if supported_markets == []:
        result.update({
            "error": f"unsupported_symbol: {symbol_pair} not found in Binance spot or USDT futures exchangeInfo",
            "error_type": "unsupported_symbol",
            "supported_markets": [],
        })
        return result
    if supported_markets:
        result["supported_markets"] = supported_markets
    if support_precheck_error:
        result["support_precheck_error"] = support_precheck_error

    timeframes: Dict[str, Dict[str, Any]] = {}
    for interval in ("15m", "1h", "4h"):
        try:
            klines, kline_market, fallback_errors = fetch_klines_with_fallback(symbol_pair, interval, 120)
            row = analyze_klines(klines, interval)
            row["kline_market"] = kline_market
            if fallback_errors:
                row["fallback_errors"] = fallback_errors
            timeframes[interval] = row
        except Exception as exc:
            timeframes[interval] = {
                "interval": interval,
                "error": str(exc),
                "bias": "unknown",
                "bullish_pts": 0,
                "bearish_pts": 0,
            }

    primary = timeframes.get("1h") or {}
    if primary.get("error"):
        primary = next((row for row in timeframes.values() if not row.get("error")), primary)
    if primary.get("error"):
        result["error"] = primary.get("error")
        result["timeframes"] = timeframes
        return result

    result["kline_market"] = primary.get("kline_market", "unknown")
    result["indicators"] = flatten_primary_indicators(primary)
    result["signals"] = primary.get("signals") or []
    result["bias"] = primary.get("bias", "neutral")
    result["bullish_pts"] = primary.get("bullish_pts", 0)
    result["bearish_pts"] = primary.get("bearish_pts", 0)
    result["timeframes"] = timeframes
    result["multi_timeframe"] = build_multi_timeframe_summary(timeframes)

    if result["multi_timeframe"]["composite_bias"] != "neutral":
        result["bias"] = result["multi_timeframe"]["composite_bias"]

    return result


if __name__ == "__main__":
    import json as _json
    for sym in ["BTC", "ETH", "SOL"]:
        print(f"\n=== {sym} ===")
        sig = compute_technical_signals(sym)
        print(_json.dumps(sig, indent=2, ensure_ascii=False))
