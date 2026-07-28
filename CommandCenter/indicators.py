#!/usr/bin/env python3
"""
indicators.py — Fleet-wide shared indicator library
====================================================
Single source of truth for all technical indicator implementations
across the 16-bot autonomous crypto trading fleet.

Stdlib only (math). No external dependencies.
Extracted from Trinity/overwatch.py (most complete implementation).

Calling conventions:
  - ema(values, period)                         -> float
  - ema_series(values, period)                  -> List[float]
  - sma(values, period)                         -> float
  - rsi(closes, period=14)                      -> float
  - stochastic_rsi(closes, period, smooth_k, smooth_d) -> (float, float)
  - macd(closes, fast, slow, signal_period)     -> (float, float, float)
  - bollinger_bands(closes, period, std_dev)    -> (upper, middle, lower)
  - atr(candles, period=14)                     -> float
  - adx(candles, period=14)                     -> (ADX, +DI, -DI)
  - donchian(candles, period=20)                -> (upper, middle, lower)
  - vwap(candles, period=20)                    -> float
  - volume_momentum(candles, period=20)         -> float
  - pearson_correlation(x, y)                   -> float

Rubberband-compatible aliases (flat list inputs):
  - calc_sma(closes, period)                    -> Optional[float]
  - calc_ema(closes, period)                    -> Optional[List[float]]
  - calc_bollinger(closes, period, num_std)     -> Optional[(upper, middle, lower)]
  - calc_rsi(closes, period)                    -> Optional[float]
  - calc_atr(highs, lows, closes, period)       -> Optional[float]
  - calc_adx(highs, lows, closes, period)       -> Optional[float]
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Candle dataclass
# ---------------------------------------------------------------------------

@dataclass
class Candle:
    """Standard OHLCV candle. timestamp has a default so it can be omitted."""
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: float = 0.0


# ---------------------------------------------------------------------------
# Core indicator functions
# ---------------------------------------------------------------------------

def ema(values: List[float], period: int) -> float:
    """Exponential Moving Average — returns single latest value."""
    if not values or len(values) < period:
        return 0.0
    k = 2 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = v * k + result * (1 - k)
    return result


def ema_series(values: List[float], period: int) -> List[float]:
    """Full EMA series for all values."""
    if not values or len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: List[float], period: int) -> float:
    """Simple Moving Average."""
    if not values or len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def rsi(closes: List[float], period: int = 14) -> float:
    """Relative Strength Index."""
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def stochastic_rsi(closes: List[float], period: int = 14, smooth_k: int = 3, smooth_d: int = 3) -> Tuple[float, float]:
    """Stochastic RSI — returns (%K, %D)."""
    if len(closes) < period + smooth_k + smooth_d + 1:
        return (50.0, 50.0)
    rsi_vals = []
    for i in range(period + 1, len(closes) + 1):
        rsi_vals.append(rsi(closes[:i], period))
    if len(rsi_vals) < period:
        return (50.0, 50.0)
    stoch_vals = []
    for i in range(period - 1, len(rsi_vals)):
        window = rsi_vals[i - period + 1:i + 1]
        lo = min(window)
        hi = max(window)
        if hi - lo == 0:
            stoch_vals.append(50.0)
        else:
            stoch_vals.append(((rsi_vals[i] - lo) / (hi - lo)) * 100)
    if len(stoch_vals) < smooth_k:
        return (50.0, 50.0)
    k_vals = []
    for i in range(smooth_k - 1, len(stoch_vals)):
        k_vals.append(sum(stoch_vals[i - smooth_k + 1:i + 1]) / smooth_k)
    if len(k_vals) < smooth_d:
        return (k_vals[-1] if k_vals else 50.0, 50.0)
    d_val = sum(k_vals[-smooth_d:]) / smooth_d
    return (k_vals[-1], d_val)


def macd(closes: List[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> Tuple[float, float, float]:
    """MACD — returns (macd_line, signal_line, histogram)."""
    if len(closes) < slow + signal_period:
        return (0.0, 0.0, 0.0)
    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)
    if not fast_ema or not slow_ema:
        return (0.0, 0.0, 0.0)
    min_len = min(len(fast_ema), len(slow_ema))
    macd_line_series = [fast_ema[-(min_len - i)] - slow_ema[-(min_len - i)] for i in range(min_len)]
    if len(macd_line_series) < signal_period:
        return (0.0, 0.0, 0.0)
    signal_ema = ema_series(macd_line_series, signal_period)
    if not signal_ema:
        return (macd_line_series[-1], 0.0, macd_line_series[-1])
    macd_val = macd_line_series[-1]
    signal_val = signal_ema[-1]
    hist = macd_val - signal_val
    return (macd_val, signal_val, hist)


def bollinger_bands(closes: List[float], period: int = 20, std_dev: float = 2.0) -> Tuple[float, float, float]:
    """Bollinger Bands — returns (upper, middle, lower)."""
    if len(closes) < period:
        return (0.0, 0.0, 0.0)
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)
    return (middle + std_dev * std, middle, middle - std_dev * std)


def atr(candles: List[Candle], period: int = 14) -> float:
    """Average True Range."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    atr_val = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
    return atr_val


def adx(candles: List[Candle], period: int = 14) -> Tuple[float, float, float]:
    """Average Directional Index — returns (ADX, +DI, -DI)."""
    if len(candles) < period * 2 + 1:
        return (0.0, 0.0, 0.0)
    plus_dm = []
    minus_dm = []
    trs = []
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm.append(up if (up > down and up > 0) else 0)
        minus_dm.append(down if (down > up and down > 0) else 0)
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
        trs.append(tr)
    if len(trs) < period:
        return (0.0, 0.0, 0.0)
    atr_smooth = sum(trs[:period])
    pdm_smooth = sum(plus_dm[:period])
    mdm_smooth = sum(minus_dm[:period])
    dx_vals = []
    for i in range(period, len(trs)):
        atr_smooth = atr_smooth - atr_smooth / period + trs[i]
        pdm_smooth = pdm_smooth - pdm_smooth / period + plus_dm[i]
        mdm_smooth = mdm_smooth - mdm_smooth / period + minus_dm[i]
        if atr_smooth == 0:
            continue
        plus_di = 100 * pdm_smooth / atr_smooth
        minus_di = 100 * mdm_smooth / atr_smooth
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_vals.append(0)
        else:
            dx_vals.append(100 * abs(plus_di - minus_di) / di_sum)
    if len(dx_vals) < period:
        return (0.0, 0.0, 0.0)
    adx_val = sum(dx_vals[:period]) / period
    for i in range(period, len(dx_vals)):
        adx_val = (adx_val * (period - 1) + dx_vals[i]) / period
    if atr_smooth == 0:
        return (adx_val, 0.0, 0.0)
    final_pdi = 100 * pdm_smooth / atr_smooth
    final_mdi = 100 * mdm_smooth / atr_smooth
    return (adx_val, final_pdi, final_mdi)


def donchian(candles: List[Candle], period: int = 20) -> Tuple[float, float, float]:
    """Donchian Channel — returns (upper, middle, lower)."""
    if len(candles) < period:
        return (0.0, 0.0, 0.0)
    window = candles[-period:]
    hi = max(c.high for c in window)
    lo = min(c.low for c in window)
    mid = (hi + lo) / 2
    return (hi, mid, lo)


def vwap(candles: List[Candle], period: int = 20) -> float:
    """Volume-Weighted Average Price."""
    if len(candles) < period:
        return 0.0
    window = candles[-period:]
    total_vol = sum(c.volume for c in window)
    if total_vol == 0:
        return window[-1].close
    total_vp = sum(((c.high + c.low + c.close) / 3) * c.volume for c in window)
    return total_vp / total_vol


def volume_momentum(candles: List[Candle], period: int = 20) -> float:
    """Volume momentum — ratio of recent volume to average volume."""
    if len(candles) < period + 1:
        return 1.0
    avg_vol = sum(c.volume for c in candles[-period - 1:-1]) / period
    if avg_vol == 0:
        return 1.0
    return candles[-1].volume / avg_vol


def pearson_correlation(x: List[float], y: List[float]) -> float:
    """Pearson correlation coefficient between two series."""
    n = min(len(x), len(y))
    if n < 3:
        return 0.0
    x = x[-n:]
    y = y[-n:]
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
    std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))
    if std_x == 0 or std_y == 0:
        return 0.0
    return cov / (std_x * std_y)


# ---------------------------------------------------------------------------
# Rubberband-compatible aliases (flat list inputs instead of Candle objects)
# ---------------------------------------------------------------------------

def calc_sma(closes: List[float], period: int) -> Optional[float]:
    """SMA alias — returns None when insufficient data (Rubberband convention)."""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_ema(closes: List[float], period: int) -> Optional[List[float]]:
    """Full EMA series — returns list same length as closes, or None."""
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    result = [0.0] * len(closes)
    result[period - 1] = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        result[i] = closes[i] * k + result[i - 1] * (1 - k)
    return result


def calc_bollinger(closes: List[float], period: int = 20,
                   num_std: float = 2.0) -> Optional[Tuple[float, float, float]]:
    """Bollinger Bands alias — returns (upper, middle, lower) or None."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """RSI alias — returns None when insufficient data (Rubberband convention)."""
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0) for c in changes[:period]]
    losses = [abs(min(c, 0)) for c in changes[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for c in changes[period:]:
        gain = max(c, 0)
        loss = abs(min(c, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_atr(highs: List[float], lows: List[float], closes: List[float],
             period: int = 14) -> Optional[float]:
    """ATR alias — flat list inputs, returns None when insufficient data."""
    if len(closes) < period + 1 or len(highs) < period + 1 or len(lows) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        h = highs[i]
        l = lows[i]
        pc = closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def calc_adx(highs: List[float], lows: List[float], closes: List[float],
             period: int = 14) -> Optional[float]:
    """ADX alias — flat list inputs, returns single ADX float or None."""
    n = len(closes)
    if n < period * 2 + 1 or len(highs) < n or len(lows) < n:
        return None
    tr_list = []
    plus_dm_list = []
    minus_dm_list = []
    for i in range(1, n):
        h = highs[i]
        l = lows[i]
        ph = highs[i - 1]
        pl = lows[i - 1]
        pc = closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up_move = h - ph
        down_move = pl - l
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr_list.append(tr)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
    if len(tr_list) < period:
        return None
    atr_val = sum(tr_list[:period]) / period
    plus_dm_smooth = sum(plus_dm_list[:period]) / period
    minus_dm_smooth = sum(minus_dm_list[:period]) / period
    dx_values = []
    for i in range(period, len(tr_list)):
        atr_val = (atr_val * (period - 1) + tr_list[i]) / period
        plus_dm_smooth = (plus_dm_smooth * (period - 1) + plus_dm_list[i]) / period
        minus_dm_smooth = (minus_dm_smooth * (period - 1) + minus_dm_list[i]) / period
        plus_di = (plus_dm_smooth / atr_val * 100) if atr_val > 0 else 0.0
        minus_di = (minus_dm_smooth / atr_val * 100) if atr_val > 0 else 0.0
        di_sum = plus_di + minus_di
        dx = (abs(plus_di - minus_di) / di_sum * 100) if di_sum > 0 else 0.0
        dx_values.append(dx)
    if len(dx_values) < period:
        return None
    adx_val = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    return adx_val
