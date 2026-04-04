#!/usr/bin/env python3
"""
GRIDZILLA v2 — Adaptive Intelligent Grid Trading Engine
========================================================

Not a dumb grid. A grid that:
- Detects the OPTIMAL range from market structure (not fixed levels)
- Adjusts grid spacing dynamically based on realized volatility
- Expands/contracts the number of grid lines based on regime
- Uses asymmetric grids (more lines on the side with momentum)
- Kills itself when the range breaks (trend detected)
- Reactivates when a new range forms
- Tracks every fill, every level, every P/L with fee accounting
- Listens to fleet intelligence (whales, PHITEX, AEGIS) to adjust

Grid Parameters (all dynamic, all adaptive):
- range_high / range_low — detected from support/resistance
- grid_lines — 5 to 25 depending on range width and volatility
- grid_spacing — ATR-based, not fixed percentage
- position_size_per_level — edge-weighted
- max_exposure — AEGIS-throttled
- trailing_profits — locks in gains as grid fills

Usage:
    python gridzilla.py              # interactive
    python gridzilla.py --auto       # headless mode (fleet launcher)
"""

import argparse
import os
import sys
import time
import json
import math
import hmac
import hashlib
import base64
import logging
import threading
import traceback
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs
from pathlib import Path

# Fleet integration
sys.path.insert(0, os.environ.get("CC_DIR", str(Path(__file__).resolve().parent.parent / "CommandCenter")))
try:
    from bus_listener import BusListener
    from event_publisher import EventPublisher
    from portfolio_client import PortfolioClient
    from expectancy import ExpectancyTracker
except ImportError as e:
    logging.warning(f"Fleet module import failed: {e}")

try:
    from fleet_config import is_blacklisted as _is_blacklisted
except ImportError:
    _is_blacklisted = lambda pair: False

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

CONFIG = {
    "name": "Gridzilla",
    "version": "2.0.0",
    "port": 8077,

    # Kraken
    "kraken_key": os.environ.get("KRAKEN_API_KEY", ""),
    "kraken_secret": os.environ.get("KRAKEN_API_SECRET", ""),

    # Universe — pairs to scan for grid opportunities
    "universe": [
        "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
        "DOT/USD", "AVAX/USD", "LINK/USD", "MATIC/USD", "ATOM/USD",
    ],

    # Grid defaults (all overridden dynamically)
    "min_grid_lines": 5,
    "max_grid_lines": 25,
    "min_grid_spacing_atr": 0.3,   # minimum spacing = 0.3 ATR
    "max_grid_spacing_atr": 1.5,   # maximum spacing = 1.5 ATR

    # Risk
    "max_total_exposure_pct": 0.30,  # max 30% of portfolio in grids
    "max_per_pair_pct": 0.05,        # max 5% per pair — must stay within portfolio max_per_trade_pct=5%
    "max_drawdown_pct": 0.05,        # kill grid if DD > 5% of allocation
    "fee_rate": 0.0026,              # Kraken taker fee (0.26%) — applied per fill, both legs

    # Regime thresholds
    "adx_trend_threshold": 25,       # ADX > 25 = trending, no grid
    "adx_range_threshold": 20,       # ADX < 20 = ranging, grid ON
    "bb_squeeze_threshold": 0.015,   # BB width < 1.5% = tight range

    # Timing
    "scan_interval": 60,             # scan every 60 seconds
    "ohlc_interval": 60,             # 1-minute candles
    "ohlc_count": 200,               # 200 candles of history

    # Fleet
    "cc_url": "http://127.0.0.1:9000",

    # State persistence
    "state_file": str(Path(__file__).resolve().parent / "gridzilla_state.json"),
}

# ═══════════════════════════════════════════════════════════════
# KRAKEN API CLIENT
# ═══════════════════════════════════════════════════════════════

import urllib.request


class KrakenClient:
    """Minimal Kraken REST client — stdlib only."""

    BASE = "https://api.kraken.com"

    def __init__(self, key="", secret=""):
        self.key = key
        self.secret = secret

    def public(self, method, params=None):
        url = f"{self.BASE}/0/public/{method}"
        if params:
            url += "?" + urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Gridzilla/2.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            if data.get("error"):
                logging.warning(f"Kraken public error: {data['error']}")
            return data.get("result", {})
        except Exception as e:
            logging.error(f"Kraken request failed: {e}")
            return {}

    def private(self, method, params=None):
        if not self.key or not self.secret:
            logging.warning("Kraken API keys not configured")
            return {}

        params = params or {}
        params["nonce"] = str(int(time.time() * 1000))

        urlpath = f"/0/private/{method}"
        postdata = urlencode(params)

        encoded = (params["nonce"] + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        signature = hmac.new(
            base64.b64decode(self.secret), message, hashlib.sha512
        )
        sigdigest = base64.b64encode(signature.digest()).decode()

        headers = {
            "API-Key": self.key,
            "API-Sign": sigdigest,
            "User-Agent": "Gridzilla/2.0",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            req = urllib.request.Request(
                self.BASE + urlpath,
                data=postdata.encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            if data.get("error"):
                logging.warning(f"Kraken private error: {data['error']}")
            return data.get("result", {})
        except Exception as e:
            logging.error(f"Kraken private request failed: {e}")
            return {}

    def fetch_ohlc(self, pair, interval=60, count=200):
        """Fetch OHLC candles. Returns list of [time, open, high, low, close, vwap, volume, count]."""
        kraken_pair = pair.replace("/", "")
        result = self.public("OHLC", {"pair": kraken_pair, "interval": interval})
        for key in result:
            if key != "last":
                candles = result[key]
                return candles[-count:] if len(candles) > count else candles
        return []

    def fetch_ticker(self, pair):
        kraken_pair = pair.replace("/", "")
        result = self.public("Ticker", {"pair": kraken_pair})
        for key in result:
            return result[key]
        return {}

    def fetch_orderbook(self, pair, depth=10):
        kraken_pair = pair.replace("/", "")
        result = self.public("Depth", {"pair": kraken_pair, "count": depth})
        for key in result:
            return result[key]
        return {}


# ═══════════════════════════════════════════════════════════════
# INDICATORS — All computed internally
# ═══════════════════════════════════════════════════════════════

class Indicators:
    """Pure-Python technical indicators. No external dependencies."""

    @staticmethod
    def atr(highs, lows, closes, period=14):
        if len(closes) < period + 1:
            return 0
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        if len(trs) < period:
            return sum(trs) / len(trs) if trs else 0
        # Wilder's smoothing
        atr_val = sum(trs[:period]) / period
        for i in range(period, len(trs)):
            atr_val = (atr_val * (period - 1) + trs[i]) / period
        return atr_val

    @staticmethod
    def adx(highs, lows, closes, period=14):
        if len(closes) < period * 2:
            return 20  # neutral default

        plus_dms = []
        minus_dms = []
        trs = []

        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)

            plus_dm = highs[i] - highs[i - 1]
            minus_dm = lows[i - 1] - lows[i]

            if plus_dm > minus_dm and plus_dm > 0:
                plus_dms.append(plus_dm)
                minus_dms.append(0)
            elif minus_dm > plus_dm and minus_dm > 0:
                plus_dms.append(0)
                minus_dms.append(minus_dm)
            else:
                plus_dms.append(0)
                minus_dms.append(0)

        if len(trs) < period:
            return 20

        # Smoothed averages
        atr_s = sum(trs[:period]) / period
        plus_di_s = sum(plus_dms[:period]) / period
        minus_di_s = sum(minus_dms[:period]) / period

        dx_values = []
        for i in range(period, len(trs)):
            atr_s = (atr_s * (period - 1) + trs[i]) / period
            plus_di_s = (plus_di_s * (period - 1) + plus_dms[i]) / period
            minus_di_s = (minus_di_s * (period - 1) + minus_dms[i]) / period

            if atr_s > 0:
                plus_di = (plus_di_s / atr_s) * 100
                minus_di = (minus_di_s / atr_s) * 100
            else:
                plus_di = minus_di = 0

            di_sum = plus_di + minus_di
            if di_sum > 0:
                dx = abs(plus_di - minus_di) / di_sum * 100
            else:
                dx = 0
            dx_values.append(dx)

        if not dx_values:
            return 20

        adx = sum(dx_values[-period:]) / min(period, len(dx_values))
        return adx

    @staticmethod
    def bollinger(closes, period=20, std_mult=2.0):
        if len(closes) < period:
            return {"upper": 0, "lower": 0, "mid": 0, "width": 0, "pct_b": 0.5}

        recent = closes[-period:]
        mid = sum(recent) / period
        variance = sum((c - mid) ** 2 for c in recent) / period
        std = math.sqrt(variance) if variance > 0 else 0

        upper = mid + std_mult * std
        lower = mid - std_mult * std
        width = (upper - lower) / (mid + 1e-10)
        pct_b = (closes[-1] - lower) / (upper - lower + 1e-10)

        return {"upper": upper, "lower": lower, "mid": mid, "width": width, "pct_b": pct_b}

    @staticmethod
    def rsi(closes, period=14):
        if len(closes) < period + 1:
            return 50

        gains = []
        losses = []
        for i in range(1, len(closes)):
            change = closes[i] - closes[i - 1]
            gains.append(max(0, change))
            losses.append(max(0, -change))

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def support_resistance(highs, lows, closes, lookback=50, sensitivity=0.02):
        """Find support and resistance levels using price clustering."""
        if len(closes) < lookback:
            return [], []

        recent_highs = highs[-lookback:]
        recent_lows = lows[-lookback:]
        recent_closes = closes[-lookback:]

        # Combine all price points
        all_prices = recent_highs + recent_lows + recent_closes
        current = closes[-1]
        threshold = current * sensitivity

        # Cluster prices
        clusters = []
        sorted_prices = sorted(all_prices)

        i = 0
        while i < len(sorted_prices):
            cluster = [sorted_prices[i]]
            j = i + 1
            while j < len(sorted_prices) and sorted_prices[j] - cluster[0] < threshold:
                cluster.append(sorted_prices[j])
                j += 1

            if len(cluster) >= 3:  # at least 3 touches
                level = sum(cluster) / len(cluster)
                strength = len(cluster)
                clusters.append((level, strength))
            i = j

        supports = [(level, strength) for level, strength in clusters if level < current]
        resistances = [(level, strength) for level, strength in clusters if level > current]

        # Sort by strength (most touches first)
        supports.sort(key=lambda x: x[1], reverse=True)
        resistances.sort(key=lambda x: x[1], reverse=True)

        return supports[:5], resistances[:5]

    @staticmethod
    def hurst_exponent(closes, max_lag=20):
        """Hurst exponent: < 0.5 = mean-reverting (good for grids), > 0.5 = trending."""
        if len(closes) < max_lag + 10:
            return 0.5

        returns = [
            (closes[i] - closes[i - 1]) / (closes[i - 1] + 1e-10)
            for i in range(1, len(closes))
        ]

        lags = range(2, max_lag)
        rs_values = []

        for lag in lags:
            chunks = [returns[i : i + lag] for i in range(0, len(returns) - lag, lag)]
            if not chunks:
                continue

            rs_list = []
            for chunk in chunks:
                if len(chunk) < 2:
                    continue
                mean = sum(chunk) / len(chunk)
                deviations = [r - mean for r in chunk]
                cumulative = []
                s = 0
                for d in deviations:
                    s += d
                    cumulative.append(s)

                R = max(cumulative) - min(cumulative) if cumulative else 0
                S = (
                    math.sqrt(sum(d**2 for d in deviations) / len(deviations))
                    if deviations
                    else 0
                )

                if S > 0:
                    rs_list.append(R / S)

            if rs_list:
                rs_values.append(
                    (math.log(lag), math.log(sum(rs_list) / len(rs_list) + 1e-10))
                )

        if len(rs_values) < 3:
            return 0.5

        # Linear regression for Hurst exponent
        n = len(rs_values)
        sum_x = sum(x for x, y in rs_values)
        sum_y = sum(y for x, y in rs_values)
        sum_xy = sum(x * y for x, y in rs_values)
        sum_xx = sum(x * x for x, y in rs_values)

        denom = n * sum_xx - sum_x * sum_x
        if denom == 0:
            return 0.5

        slope = (n * sum_xy - sum_x * sum_y) / denom
        return max(0, min(1, slope))


# ═══════════════════════════════════════════════════════════════
# REGIME DETECTOR
# ═══════════════════════════════════════════════════════════════

class RegimeDetector:
    """Determines if a pair is suitable for grid trading."""

    def __init__(self, config):
        self.config = config

    def analyze(self, candles):
        """Returns regime analysis for grid suitability."""
        if len(candles) < 50:
            return {"suitable": False, "reason": "insufficient data", "regime": "UNKNOWN"}

        closes = [float(c[4]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]

        # ADX — trend strength
        adx = Indicators.adx(highs, lows, closes)

        # Bollinger Bands — range width
        bb = Indicators.bollinger(closes)

        # RSI — overbought/oversold
        rsi = Indicators.rsi(closes)

        # Hurst exponent — mean-reversion tendency
        hurst = Indicators.hurst_exponent(closes)

        # ATR for volatility
        atr = Indicators.atr(highs, lows, closes)
        atr_pct = atr / (closes[-1] + 1e-10)

        # Support/Resistance levels
        supports, resistances = Indicators.support_resistance(highs, lows, closes)

        # ═══ REGIME CLASSIFICATION ═══

        # Strong trend — DO NOT GRID
        if adx > self.config["adx_trend_threshold"]:
            direction = "UP" if closes[-1] > closes[-20] else "DOWN"
            return {
                "suitable": False,
                "reason": f"trending {direction} (ADX={adx:.1f})",
                "regime": f"TRENDING_{direction}",
                "adx": round(adx, 2),
                "bb_width": round(bb["width"], 4),
                "hurst": round(hurst, 3),
                "atr_pct": round(atr_pct, 4),
                "rsi": round(rsi, 1),
            }

        # Hurst > 0.55 — trending behavior, risky for grids
        if hurst > 0.55 and adx > 18:
            return {
                "suitable": False,
                "reason": f"trending Hurst ({hurst:.2f})",
                "regime": "TRENDING_HURST",
                "adx": round(adx, 2),
                "bb_width": round(bb["width"], 4),
                "hurst": round(hurst, 3),
                "atr_pct": round(atr_pct, 4),
                "rsi": round(rsi, 1),
            }

        # Ideal grid conditions:
        # ADX < 20 (ranging) + Hurst < 0.45 (mean-reverting) + defined S/R levels
        grid_score = 0
        reasons = []

        if adx < self.config["adx_range_threshold"]:
            grid_score += 30
            reasons.append(f"low ADX ({adx:.1f})")

        if hurst < 0.45:
            grid_score += 25
            reasons.append(f"mean-reverting (H={hurst:.2f})")

        if bb["width"] < 0.04:
            grid_score += 20
            reasons.append(f"tight range (BB={bb['width']:.3f})")

        if 35 < rsi < 65:
            grid_score += 10
            reasons.append("neutral RSI")

        if supports and resistances:
            grid_score += 15
            reasons.append(f"{len(supports)}S/{len(resistances)}R levels")

        suitable = grid_score >= 50

        return {
            "suitable": suitable,
            "grid_score": grid_score,
            "reason": ", ".join(reasons) if reasons else "no clear range",
            "regime": "RANGING" if suitable else "MIXED",
            "adx": round(adx, 2),
            "bb_width": round(bb["width"], 4),
            "hurst": round(hurst, 3),
            "atr_pct": round(atr_pct, 4),
            "rsi": round(rsi, 1),
            "supports": [(round(l, 2), s) for l, s in supports],
            "resistances": [(round(l, 2), s) for l, s in resistances],
        }


# ═══════════════════════════════════════════════════════════════
# GRID ARCHITECT — Designs the optimal grid
# ═══════════════════════════════════════════════════════════════

class GridArchitect:
    """Designs adaptive grid configurations based on market structure."""

    def __init__(self, config):
        self.config = config

    def design(self, pair, candles, regime, allocation_usd):
        """Design an optimal grid for this pair and market condition."""
        closes = [float(c[4]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]

        current_price = closes[-1]
        atr = Indicators.atr(highs, lows, closes)
        bb = Indicators.bollinger(closes)

        supports = regime.get("supports", [])
        resistances = regime.get("resistances", [])

        # ═══ RANGE DETECTION ═══
        # Use Bollinger Bands + S/R to define the grid range

        if supports and resistances:
            # Use strongest S/R as boundaries
            range_low = supports[0][0]
            range_high = resistances[0][0]
        else:
            # Fall back to Bollinger Bands
            range_low = bb["lower"]
            range_high = bb["upper"]

        # Sanity: range must be at least 2 ATR wide
        range_width = range_high - range_low
        min_width = atr * 2
        if range_width < min_width:
            center = (range_high + range_low) / 2
            range_low = center - min_width / 2
            range_high = center + min_width / 2
            range_width = range_high - range_low

        # ═══ GRID SPACING ═══
        # Based on ATR — adaptive to current volatility

        bb_width = regime.get("bb_width", 0.03)

        # Tighter range → tighter spacing, wider range → wider spacing
        if bb_width < 0.02:
            spacing_mult = self.config["min_grid_spacing_atr"]
        elif bb_width > 0.06:
            spacing_mult = self.config["max_grid_spacing_atr"]
        else:
            t = (bb_width - 0.02) / 0.04
            spacing_mult = self.config["min_grid_spacing_atr"] + t * (
                self.config["max_grid_spacing_atr"] - self.config["min_grid_spacing_atr"]
            )

        grid_spacing = atr * spacing_mult

        # ═══ NUMBER OF GRID LINES ═══
        n_lines = int(range_width / (grid_spacing + 1e-10))
        n_lines = max(self.config["min_grid_lines"], min(self.config["max_grid_lines"], n_lines))

        # Recalculate spacing to evenly distribute
        grid_spacing = range_width / (n_lines + 1)

        # ═══ GRID LEVELS ═══
        levels = []
        for i in range(1, n_lines + 1):
            price = range_low + i * grid_spacing

            # Distance from current price determines initial side
            side = "BUY" if price < current_price else "SELL"

            # Position size per level — more at edges (mean-reversion)
            distance_from_center = abs(price - (range_high + range_low) / 2)
            max_distance = range_width / 2
            edge_factor = distance_from_center / (max_distance + 1e-10)

            # Edge levels get 50% more size (catch reversals)
            size_mult = 1 + edge_factor * 0.5
            base_size = allocation_usd / n_lines
            level_size = base_size * size_mult

            level_profit = grid_spacing * (level_size / current_price)

            levels.append({
                "price": round(price, 6),
                "side": side,
                "size_usd": round(level_size, 2),
                "size_base": round(level_size / current_price, 8),
                "distance_pct": round((price - current_price) / current_price * 100, 3),
                "edge_factor": round(edge_factor, 3),
                "expected_profit": round(level_profit, 4),
                "fees": 0,
                "filled": False,
                "fill_price": 0,
                "fill_time": 0,
                "paired_with": None,  # index of the opposite level
            })

        # ═══ PAIR LEVELS — each buy has a sell target and vice versa ═══
        buy_levels = [l for l in levels if l["side"] == "BUY"]
        sell_levels = [l for l in levels if l["side"] == "SELL"]

        # Pair each buy with the nearest sell above it
        for bl in buy_levels:
            nearest_sell = None
            min_dist = float("inf")
            for sl in sell_levels:
                dist = sl["price"] - bl["price"]
                if dist > 0 and dist < min_dist:
                    min_dist = dist
                    nearest_sell = sl
            if nearest_sell:
                bl["take_profit"] = nearest_sell["price"]
                bl["expected_r"] = round(min_dist / (grid_spacing + 1e-10), 2)

        for sl in sell_levels:
            nearest_buy = None
            min_dist = float("inf")
            for bl in buy_levels:
                dist = sl["price"] - bl["price"]
                if dist > 0 and dist < min_dist:
                    min_dist = dist
                    nearest_buy = bl
            if nearest_buy:
                sl["take_profit"] = nearest_buy["price"]
                sl["expected_r"] = round(min_dist / (grid_spacing + 1e-10), 2)

        return {
            "pair": pair,
            "range_high": round(range_high, 6),
            "range_low": round(range_low, 6),
            "range_width_pct": round(range_width / current_price * 100, 3),
            "current_price": round(current_price, 6),
            "atr": round(atr, 6),
            "grid_spacing": round(grid_spacing, 6),
            "grid_spacing_pct": round(grid_spacing / current_price * 100, 3),
            "n_levels": len(levels),
            "levels": levels,
            "allocation_usd": round(allocation_usd, 2),
            "estimated_profit_per_cycle": round(
                sum(l.get("expected_profit", 0) for l in levels), 4
            ),
            "total_fees_per_cycle": round(
                sum(l.get("fees", 0) for l in levels), 4
            ),
            "fee_ratio": round(
                sum(l.get("fees", 0) for l in levels)
                / (sum(l.get("expected_profit", 0) for l in levels) + 1e-10)
                * 100,
                1,
            ),
            "designed_at": time.time(),
        }


# ═══════════════════════════════════════════════════════════════
# GRID EXECUTOR — Manages active grids
# ═══════════════════════════════════════════════════════════════

class GridExecutor:
    """Manages live grid execution, fills, and P/L tracking."""

    def __init__(self, config, kraken, publisher):
        self.config = config
        self.kraken = kraken
        self.publisher = publisher
        self.active_grids = {}  # {pair: grid_state}
        self.trade_history = []
        self.total_pnl = 0
        self.total_fees = 0
        self.total_cycles = 0
        self.lock = threading.Lock()

    def deploy_grid(self, design):
        """Deploy a designed grid to the market."""
        pair = design["pair"]

        with self.lock:
            if pair in self.active_grids:
                logging.info(f"Grid already active for {pair}, skipping deploy")
                return False

            grid_state = {
                "design": design,
                "status": "ACTIVE",
                "deployed_at": time.time(),
                "levels": [lvl.copy() for lvl in design["levels"]],
                "fills": [],
                "cycles_completed": 0,
                "grid_pnl": 0,
                "grid_fees": 0,
                "peak_pnl": 0,
                "max_drawdown": 0,
                "range_breaks": 0,
                "last_check": time.time(),
            }

            self.active_grids[pair] = grid_state

            # Publish deployment
            if self.publisher:
                try:
                    self.publisher.emit("GRID_DEPLOYED", {
                        "pair": pair,
                        "levels": design["n_levels"],
                        "range": f"{design['range_low']:.2f}-{design['range_high']:.2f}",
                        "spacing_pct": design["grid_spacing_pct"],
                        "allocation": design["allocation_usd"],
                    })
                except Exception:
                    pass

            logging.info(
                f"Grid deployed: {pair} — {design['n_levels']} levels, "
                f"range {design['range_low']:.2f}-{design['range_high']:.2f}"
            )
            return True

    def check_fills(self, pair, current_price):
        """Check if any grid levels have been hit by current price."""
        with self.lock:
            grid = self.active_grids.get(pair)
            if not grid or grid["status"] != "ACTIVE":
                return []

            fills = []

            for level in grid["levels"]:
                if level["filled"]:
                    continue

                # Check if price has crossed this level
                if level["side"] == "BUY" and current_price <= level["price"]:
                    # Buy level hit — fill it
                    level["filled"] = True
                    level["fill_price"] = current_price
                    level["fill_time"] = time.time()

                    fee = level["size_usd"] * self.config["fee_rate"]

                    fill = {
                        "pair": pair,
                        "side": "BUY",
                        "price": current_price,
                        "level_price": level["price"],
                        "size_usd": level["size_usd"],
                        "fee": round(fee, 4),
                        "time": time.time(),
                        "take_profit": level.get("take_profit"),
                    }

                    grid["fills"].append(fill)
                    grid["grid_fees"] += fee
                    self.total_fees += fee
                    fills.append(fill)

                    logging.info(
                        f"Grid BUY filled: {pair} @ {current_price:.6f} "
                        f"(level {level['price']:.6f})"
                    )

                elif level["side"] == "SELL" and current_price >= level["price"]:
                    # Sell level hit — check if this closes a buy
                    level["filled"] = True
                    level["fill_price"] = current_price
                    level["fill_time"] = time.time()

                    fee = level["size_usd"] * self.config["fee_rate"]

                    # Find the matching buy fill to calculate P/L
                    matching_buy = None
                    for f in grid["fills"]:
                        if (
                            f["side"] == "BUY"
                            and f["pair"] == pair
                            and f.get("take_profit")
                            and not f.get("closed")
                        ):
                            if (
                                abs(f["take_profit"] - level["price"]) / level["price"]
                                < 0.001
                            ):
                                matching_buy = f
                                break

                    pnl = 0
                    if matching_buy:
                        # Calculate grid cycle P/L
                        buy_cost = matching_buy["size_usd"]
                        sell_revenue = level["size_usd"] * (
                            current_price / level["price"]
                        )
                        pnl = sell_revenue - buy_cost - fee - matching_buy["fee"]
                        matching_buy["closed"] = True
                        grid["cycles_completed"] += 1
                        self.total_cycles += 1

                    fill = {
                        "pair": pair,
                        "side": "SELL",
                        "price": current_price,
                        "level_price": level["price"],
                        "size_usd": level["size_usd"],
                        "fee": round(fee, 4),
                        "pnl": round(pnl, 4),
                        "time": time.time(),
                    }

                    grid["fills"].append(fill)
                    grid["grid_pnl"] += pnl
                    grid["grid_fees"] += fee
                    self.total_pnl += pnl
                    self.total_fees += fee
                    fills.append(fill)

                    if pnl != 0:
                        logging.info(
                            f"Grid SELL filled: {pair} @ {current_price:.6f} "
                            f"(level {level['price']:.6f}) PnL: ${pnl:.4f}"
                        )

            # Track drawdown
            if grid["grid_pnl"] > grid["peak_pnl"]:
                grid["peak_pnl"] = grid["grid_pnl"]
            dd = grid["peak_pnl"] - grid["grid_pnl"]
            if dd > grid["max_drawdown"]:
                grid["max_drawdown"] = dd

            # Publish fills
            if fills and self.publisher:
                for fill in fills:
                    event_type = "TRADE_OPEN" if fill["side"] == "BUY" else "TRADE_CLOSE"
                    try:
                        self.publisher.emit(event_type, {
                            "bot": "gridzilla",
                            "pair": pair,
                            "direction": "LONG",  # grids are always long-side cycles
                            "price": fill["price"],
                            "size_usd": fill["size_usd"],
                            "pnl": fill.get("pnl", 0),
                            "fee": fill["fee"],
                            "grid_cycle": grid["cycles_completed"],
                        })
                    except Exception:
                        pass

            return fills

    def check_range_break(self, pair, current_price):
        """Check if price has broken out of the grid range."""
        with self.lock:
            grid = self.active_grids.get(pair)
            if not grid or grid["status"] != "ACTIVE":
                return False

            design = grid["design"]
            range_high = design["range_high"]
            range_low = design["range_low"]
            atr = design["atr"]
            buffer = atr * 0.5

            if current_price > range_high + buffer or current_price < range_low - buffer:
                grid["range_breaks"] += 1

                if grid["range_breaks"] >= 3:
                    grid["status"] = "RANGE_BROKEN"
                    logging.warning(
                        f"Grid {pair} RANGE BROKEN — price {current_price:.6f} "
                        f"outside {range_low:.6f}-{range_high:.6f}"
                    )

                    if self.publisher:
                        try:
                            self.publisher.emit("GRID_KILLED", {
                                "pair": pair,
                                "reason": "range_break",
                                "price": current_price,
                                "range": f"{range_low:.2f}-{range_high:.2f}",
                                "pnl": round(grid["grid_pnl"], 4),
                                "cycles": grid["cycles_completed"],
                            })
                        except Exception:
                            pass

                    return True
            else:
                grid["range_breaks"] = max(0, grid["range_breaks"] - 1)

            return False

    def check_drawdown_kill(self, pair, allocation):
        """Kill grid if drawdown exceeds threshold."""
        with self.lock:
            grid = self.active_grids.get(pair)
            if not grid or grid["status"] != "ACTIVE":
                return False

            max_dd = allocation * self.config["max_drawdown_pct"]
            if grid["max_drawdown"] > max_dd:
                grid["status"] = "DD_KILLED"
                logging.warning(
                    f"Grid {pair} killed by drawdown: "
                    f"${grid['max_drawdown']:.2f} > ${max_dd:.2f}"
                )

                if self.publisher:
                    try:
                        self.publisher.emit("GRID_KILLED", {
                            "pair": pair,
                            "reason": "drawdown",
                            "drawdown": round(grid["max_drawdown"], 4),
                            "pnl": round(grid["grid_pnl"], 4),
                        })
                    except Exception:
                        pass
                return True
            return False

    def reset_filled_levels(self, pair):
        """Reset filled levels for a new cycle (when grid completes a round)."""
        with self.lock:
            grid = self.active_grids.get(pair)
            if not grid:
                return

            filled = sum(1 for l in grid["levels"] if l["filled"])
            total = len(grid["levels"])

            if filled > total * 0.8:
                for level in grid["levels"]:
                    level["filled"] = False
                    level["fill_price"] = 0
                    level["fill_time"] = 0

                logging.info(
                    f"Grid {pair} reset for new cycle "
                    f"(cycle #{grid['cycles_completed']})"
                )

    def remove_grid(self, pair):
        """Remove an inactive grid. Returns trade summary dict or None."""
        with self.lock:
            if pair in self.active_grids:
                grid = self.active_grids.pop(pair)
                summary = {
                    "pair": pair,
                    "started": grid["deployed_at"],
                    "ended": time.time(),
                    "status": grid["status"],
                    "pnl": grid["grid_pnl"],
                    "fees": grid["grid_fees"],
                    "cycles": grid["cycles_completed"],
                    "fills": len(grid["fills"]),
                    "reservation_id": grid.get("reservation_id", ""),
                }
                self.trade_history.append(summary)
                return summary
        return None

    def get_active_grids(self):
        with self.lock:
            return {
                pair: {
                    "status": g["status"],
                    "pair": pair,
                    "range": f"{g['design']['range_low']:.2f}-{g['design']['range_high']:.2f}",
                    "levels": g["design"]["n_levels"],
                    "filled": sum(1 for l in g["levels"] if l["filled"]),
                    "cycles": g["cycles_completed"],
                    "pnl": round(g["grid_pnl"], 4),
                    "fees": round(g["grid_fees"], 4),
                    "net_pnl": round(g["grid_pnl"] - g["grid_fees"], 4),
                    "max_dd": round(g["max_drawdown"], 4),
                    "uptime_min": round((time.time() - g["deployed_at"]) / 60, 1),
                }
                for pair, g in self.active_grids.items()
            }


# ═══════════════════════════════════════════════════════════════
# BUS INTELLIGENCE — React to fleet events
# ═══════════════════════════════════════════════════════════════

class BusIntelligence:
    """Integrate fleet intelligence into grid decisions."""

    def __init__(self, bus_listener):
        self.bus = bus_listener
        self.whale_alert_active = False
        self.phitex_critical = False
        self.aegis_regime = "DEFENSIVE"
        self.aegis_score = 0.01
        self.last_update = 0

    def refresh(self):
        """Pull latest intel from the bus."""
        if not self.bus:
            return

        try:
            self.aegis_regime = self.bus.aegis_regime() or "DEFENSIVE"
            self.aegis_score = self.bus.aegis_score() or 0.01

            # Whale alert status — check fleet-wide (pair=None returns all)
            whale_alerts = self.bus.whale_alerts(pair=None, max_age=300)
            if whale_alerts:
                tiers = {"EXTREME": 3, "HIGH": 2, "MODERATE": 1, "LOW": 0}
                best_tier = max(
                    (a.get("data", {}).get("tier", "LOW") for a in whale_alerts),
                    key=lambda t: tiers.get(t, 0),
                )
                # Only pause on EXTREME whales — HIGH fires constantly from Deep Blue
                # and would permanently block grids during normal market activity
                self.whale_alert_active = best_tier == "EXTREME"
            else:
                self.whale_alert_active = False

            # PHITEX critical — check fleet-wide status
            phitex = self.bus.phitex_status(pair=None)
            self.phitex_critical = (
                phitex is not None and phitex.get("fleet_score", 0) > 0.5
            )

            self.last_update = time.time()
        except Exception as e:
            logging.debug(f"Bus refresh error: {e}")

    def should_pause_grids(self):
        """Should all grids be paused based on fleet intelligence?"""
        if self.whale_alert_active:
            return True, "whale alert active"
        if self.phitex_critical:
            return True, "PHITEX critical — phase transition imminent"
        if self.aegis_regime == "EMERGENCY":
            return True, "AEGIS emergency"
        return False, ""

    def max_exposure_multiplier(self):
        """Adjust max exposure based on AEGIS regime."""
        multipliers = {
            "DEFENSIVE": 0.5,
            "CAUTIOUS": 0.7,
            "NORMAL": 1.0,
            "DEPLOY": 1.2,
        }
        return multipliers.get(self.aegis_regime, 0.5)


# ═══════════════════════════════════════════════════════════════
# MAIN ENGINE — The Brain
# ═══════════════════════════════════════════════════════════════

class GridzillaEngine:
    """Main engine coordinating all components."""

    def __init__(self, config):
        self.config = config
        self.kraken = KrakenClient(
            config.get("kraken_key", ""), config.get("kraken_secret", "")
        )

        # Fleet integration
        self.publisher = None
        self.bus_listener = None
        self.portfolio = None
        self.expectancy = None
        try:
            self.publisher = EventPublisher(config["cc_url"], "gridzilla")
        except Exception as e:
            logging.warning(f"EventPublisher init failed: {e}")
        try:
            self.bus_listener = BusListener(config["cc_url"])
        except Exception as e:
            logging.warning(f"BusListener init failed: {e}")
        try:
            self.portfolio = PortfolioClient(config["cc_url"], "gridzilla")
        except Exception as e:
            logging.warning(f"PortfolioClient init failed: {e}")
        try:
            self.expectancy = ExpectancyTracker()
        except Exception as e:
            logging.warning(f"ExpectancyTracker init failed: {e}")

        self.regime_detector = RegimeDetector(config)
        self.grid_architect = GridArchitect(config)
        self.executor = GridExecutor(config, self.kraken, self.publisher)
        self.intel = BusIntelligence(self.bus_listener)

        # State
        self.running = False
        self.scan_count = 0
        self.pair_analysis = {}  # {pair: last_analysis}
        self.start_time = time.time()

        logging.info(f"Gridzilla v{config['version']} initialized")

    def start(self):
        """Start the main scan loop."""
        self.running = True

        # Start HTTP server in background
        server_thread = threading.Thread(target=self._run_server, daemon=True)
        server_thread.start()

        # Start bus listener (BusListener starts its poll thread in __init__, no .start() needed)
        if self.bus_listener:
            pass

        logging.info(f"Gridzilla running on port {self.config['port']}")

        # Main loop
        while self.running:
            try:
                self._scan_cycle()
            except Exception as e:
                logging.error(f"Scan cycle error: {e}")
                traceback.print_exc()

            time.sleep(self.config["scan_interval"])

    def _scan_cycle(self):
        """One complete scan cycle."""
        self.scan_count += 1

        # Refresh fleet intelligence
        self.intel.refresh()

        # Check if we should pause
        pause, reason = self.intel.should_pause_grids()
        if pause:
            logging.info(f"Grids paused: {reason}")
            # Don't deploy new grids, but still check existing ones

        # Get portfolio allocation
        portfolio_balance = 10000  # default
        if self.portfolio:
            try:
                avail = self.portfolio.available()
                if avail is not None:
                    portfolio_balance = avail
            except Exception:
                pass

        max_exposure = portfolio_balance * self.config["max_total_exposure_pct"]
        max_exposure *= self.intel.max_exposure_multiplier()

        # Current exposure
        active = self.executor.get_active_grids()
        current_exposure = sum(g.get("levels", 0) * 10 for g in active.values())  # rough

        # Scan universe for grid opportunities (skip blacklisted pairs)
        for pair in self.config["universe"]:
            if _is_blacklisted(pair):
                continue
            try:
                # Fetch candles
                candles = self.kraken.fetch_ohlc(
                    pair, self.config["ohlc_interval"], self.config["ohlc_count"]
                )
                if not candles or len(candles) < 50:
                    continue

                current_price = float(candles[-1][4])

                # Check existing grid
                if pair in self.executor.active_grids:
                    # Check fills
                    self.executor.check_fills(pair, current_price)

                    # Check range break
                    if self.executor.check_range_break(pair, current_price):
                        summary = self.executor.remove_grid(pair)
                        if summary and summary.get("reservation_id") and self.portfolio:
                            try:
                                self.portfolio.release(summary["reservation_id"], pnl=summary["pnl"] - summary["fees"])
                            except Exception:
                                pass
                        continue

                    # Check drawdown kill
                    per_pair_alloc = portfolio_balance * self.config["max_per_pair_pct"]
                    if self.executor.check_drawdown_kill(pair, per_pair_alloc):
                        summary = self.executor.remove_grid(pair)
                        if summary and summary.get("reservation_id") and self.portfolio:
                            try:
                                self.portfolio.release(summary["reservation_id"], pnl=summary["pnl"] - summary["fees"])
                            except Exception:
                                pass
                        continue

                    # Reset filled levels if cycle complete
                    self.executor.reset_filled_levels(pair)

                    continue  # Grid already active, skip analysis

                # Skip if paused
                if pause:
                    continue

                # Skip if at max exposure
                if current_exposure >= max_exposure:
                    continue

                # Analyze regime
                regime = self.regime_detector.analyze(candles)
                self.pair_analysis[pair] = regime

                if not regime["suitable"]:
                    continue

                # Design grid
                per_pair_alloc = min(
                    portfolio_balance * self.config["max_per_pair_pct"],
                    max_exposure - current_exposure,
                )

                if per_pair_alloc < 20:  # minimum allocation
                    continue

                design = self.grid_architect.design(pair, candles, regime, per_pair_alloc)

                # Deploy if design is profitable after fees
                if design["fee_ratio"] < 80:  # fees must be < 80% of profit
                    # Reserve capital from central portfolio
                    _rid = ""
                    if self.portfolio:
                        try:
                            ok, result = self.portfolio.reserve(pair, "LONG", per_pair_alloc)
                            if not ok:
                                logging.info(f"Portfolio denied grid {pair}: {result}")
                                continue
                            _rid = result
                        except Exception:
                            pass  # CC unreachable — deploy with local limits only
                    self.executor.deploy_grid(design)
                    # Store reservation ID on the grid state
                    if _rid and pair in self.executor.active_grids:
                        self.executor.active_grids[pair]["reservation_id"] = _rid
                    current_exposure += per_pair_alloc
                else:
                    logging.debug(
                        f"{pair} grid rejected: fee ratio {design['fee_ratio']:.1f}%"
                    )

            except Exception as e:
                logging.debug(f"Error scanning {pair}: {e}")

        # Publish scan complete
        if self.publisher and self.scan_count % 5 == 0:
            try:
                self.publisher.emit("SCAN_COMPLETE", {
                    "bot": "gridzilla",
                    "pairs_scanned": len(self.config["universe"]),
                    "active_grids": len(self.executor.active_grids),
                    "total_pnl": round(self.executor.total_pnl, 4),
                    "total_fees": round(self.executor.total_fees, 4),
                    "total_cycles": self.executor.total_cycles,
                })
            except Exception:
                pass

    def get_snapshot(self):
        """Return current state for HTTP endpoint."""
        active = self.executor.get_active_grids()

        exp_report = {}
        if self.expectancy:
            try:
                exp_report = self.expectancy.get_bot_stats("gridzilla") or {}
            except Exception:
                pass

        return {
            "name": self.config["name"],
            "version": self.config["version"],
            "status": "running" if self.running else "stopped",
            "uptime_hours": round((time.time() - self.start_time) / 3600, 2),
            "scan_count": self.scan_count,
            "regime": self.intel.aegis_regime,
            "aegis_score": self.intel.aegis_score,
            "active_grids": active,
            "n_active_grids": len(active),
            "total_pnl": round(self.executor.total_pnl, 4),
            "total_fees": round(self.executor.total_fees, 4),
            "net_pnl": round(self.executor.total_pnl - self.executor.total_fees, 4),
            "total_cycles": self.executor.total_cycles,
            "trade_history": self.executor.trade_history[-20:],
            "pair_analysis": {
                pair: {
                    "suitable": a.get("suitable", False),
                    "regime": a.get("regime", "UNKNOWN"),
                    "adx": a.get("adx", 0),
                    "hurst": a.get("hurst", 0.5),
                    "grid_score": a.get("grid_score", 0),
                }
                for pair, a in self.pair_analysis.items()
            },
            "intel": {
                "aegis_regime": self.intel.aegis_regime,
                "whale_alert": self.intel.whale_alert_active,
                "phitex_critical": self.intel.phitex_critical,
                "paused": self.intel.should_pause_grids()[0],
            },
            "expectancy": exp_report,
        }

    # ═══ HTTP SERVER ═══

    def _run_server(self):
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlparse(self.path).path.rstrip("/")

                if path in ("", "/health", "/api/snapshot"):
                    data = engine.get_snapshot()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps(data, default=str).encode())

                elif path == "/api/grids":
                    data = engine.executor.get_active_grids()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(data, default=str).encode())

                elif path == "/api/history":
                    data = engine.executor.trade_history
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(data, default=str).encode())

                elif path == "/api/analysis":
                    data = engine.pair_analysis
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(data, default=str).encode())

                else:
                    self.send_error(404)

            def log_message(self, fmt, *args):
                pass  # silent

        server = HTTPServer(("0.0.0.0", self.config["port"]), Handler)
        server.serve_forever()


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Gridzilla v2 — Adaptive Intelligent Grid Engine")
    parser.add_argument("--auto", action="store_true", help="Headless mode (fleet launcher)")
    args = parser.parse_args()

    import sys as _sys
    _sys.path.insert(0, r"D:\CommandCenter")
    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        _port = CONFIG["port"]
        ensure_port(_port, "gridzilla")
        write_pidfile("gridzilla", _port)
        atexit.register(cleanup_pidfile, "gridzilla")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gridzilla_v2.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(_log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    engine = GridzillaEngine(CONFIG)

    try:
        engine.start()
    except KeyboardInterrupt:
        logging.info("Gridzilla shutting down...")
        engine.running = False


if __name__ == "__main__":
    main()
