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
- Tracks every fill, every level, every P/L as GROSS price movement
  (signal product — subscribers pay their own exchanges' fees)
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
import logging
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlparse
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
try:
    import fleet_config as _fc
except ImportError:
    _fc = None
# LIMIT ONLY (fleet policy). Fallback keeps the 15bps cross rather than
# vanishing — an unpriced order is refused by the client, which is the correct
# failure, but losing the helper entirely would break every entry and exit.
try:
    from limit_order import limit_price as _limit_price
except ImportError:
    def _limit_price(ref, direction, cross_pct=None):
        try:
            ref = float(ref)
        except (TypeError, ValueError):
            return 0.0
        if ref <= 0:
            return 0.0
        pct = 0.0015 if cross_pct is None else float(cross_pct)
        return ref * (1.0 + pct) if (direction or "").upper() in ("LONG", "BUY") else ref * (1.0 - pct)
try:
    from kraken_client import KrakenSpotClient as _KrakenSpotClient
except ImportError:
    _KrakenSpotClient = None

from indicators import (
    rsi as _ind_rsi,
    bollinger_bands as _ind_bollinger_bands,
    calc_atr as _ind_calc_atr,
    calc_adx as _ind_calc_adx,
)

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
        "DOT/USD", "AVAX/USD", "LINK/USD", "POL/USD", "ATOM/USD",
    ],

    # Grid defaults (all overridden dynamically)
    # Lines capped at 5 as a signal-density limit: every level is an alert a
    # subscriber receives, so each one must represent a meaningful move.
    "min_grid_lines": 5,
    "max_grid_lines": 5,
    "min_grid_spacing_atr": 0.3,   # minimum spacing = 0.3 ATR
    "max_grid_spacing_atr": 1.5,   # maximum spacing = 1.5 ATR

    # Risk
    "max_total_exposure_pct": 0.30,  # max 30% of portfolio in grids
    "max_per_pair_pct": 0.05,        # 5% per pair — aligned with pool floor
    "max_drawdown_pct": 0.05,        # kill grid if DD > 5% of allocation
    # fee_rate removed — signal product: P/L is gross price movement,
    # subscribers pay their own exchanges' fees.

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

    def fetch_ohlc(self, pair, interval=60, count=200):
        """Fetch OHLC candles. Returns list of [time, open, high, low, close, vwap, volume, count]."""
        kraken_pair = pair.replace("/", "")
        result = self.public("OHLC", {"pair": kraken_pair, "interval": interval})
        for key in result:
            if key != "last":
                candles = result[key]
                return candles[-count:] if len(candles) > count else candles
        return []


# ═══════════════════════════════════════════════════════════════
# INDICATORS — All computed internally
# ═══════════════════════════════════════════════════════════════

class Indicators:
    """Pure-Python technical indicators. No external dependencies."""

    @staticmethod
    def atr(highs, lows, closes, period=14):
        return _ind_calc_atr(highs, lows, closes, period) or 0

    @staticmethod
    def adx(highs, lows, closes, period=14):
        return _ind_calc_adx(highs, lows, closes, period) or 20

    @staticmethod
    def bollinger(closes, period=20, std_mult=2.0):
        if len(closes) < period:
            return {"upper": 0, "lower": 0, "mid": 0, "width": 0, "pct_b": 0.5}
        upper, mid, lower = _ind_bollinger_bands(closes, period, std_mult)
        width = (upper - lower) / (mid + 1e-10)
        pct_b = (closes[-1] - lower) / (upper - lower + 1e-10)
        return {"upper": upper, "lower": lower, "mid": mid, "width": width, "pct_b": pct_b}

    @staticmethod
    def rsi(closes, period=14):
        return _ind_rsi(closes, period)

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

        # Minimum absolute spacing floor: at least 1.2% of price.
        # Signal-quality floor, not fee math: a grid level must represent a
        # meaningful price move worth alerting a subscriber about. Tighter
        # spacing degrades into micro-churn noise (the old 0.5% floor made
        # POL/USD unusable). Same 1.2% value — the rationale changed, not
        # the number.
        min_spacing = current_price * 0.012
        grid_spacing = max(grid_spacing, min_spacing)

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
                "fees": 0.0,  # legacy field kept for shape — no fee accounting (signal product)
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

        # ═══ GROSS PROFIT FLOOR — $0.50 per level minimum ═══
        # $0.50 gross per level — floor for signal-worthiness, not fee survival.
        # A grid cycle whose gross price movement is worth less than $0.50 is
        # not worth alerting a subscriber about — hard reject. Same dollar
        # value as the old net-of-fees floor.
        MIN_GROSS_PROFIT_PER_CYCLE = 0.50
        avg_level_size = allocation_usd / max(len(levels), 1)
        spacing_pct = grid_spacing / (current_price + 1e-10)
        avg_gross_per_level = spacing_pct * avg_level_size
        if avg_gross_per_level < MIN_GROSS_PROFIT_PER_CYCLE:
            logging.warning(
                f"[{pair}] Grid rejected: avg gross/level ${avg_gross_per_level:.3f} < "
                f"${MIN_GROSS_PROFIT_PER_CYCLE} signal-worthiness floor "
                f"(spacing {spacing_pct*100:.2f}%)"
            )
            return None

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
            # Legacy fee fields — zeroed, kept so dashboard/consumers keep their
            # shape. Signal product: P/L is gross, subscribers pay their own fees.
            "total_fees_per_cycle": 0.0,
            "fee_ratio": 0.0,
            "designed_at": time.time(),
        }


# ═══════════════════════════════════════════════════════════════
# GRID EXECUTOR — Manages active grids
# ═══════════════════════════════════════════════════════════════

class GridExecutor:
    """Manages live grid execution, fills, and P/L tracking."""

    def __init__(self, config, kraken, publisher, expectancy=None):
        self.config = config
        self.kraken = kraken
        self.publisher = publisher
        # Completed cycles are recorded here as they happen. Without it the
        # snapshot's expectancy block is queried but never fed, so every
        # field reads 0 while total_pnl shows real money.
        self.expectancy = expectancy
        self.active_grids = {}  # {pair: grid_state}
        self.trade_history = []
        self.total_pnl = 0
        self.total_fees = 0  # legacy — stays 0; P/L is gross (signal product)
        self.total_cycles = 0
        self.lock = threading.Lock()

    # ═══ STATE PERSISTENCE ═══

    def _save_state(self):
        """Atomic write of active_grids to gridzilla_state.json."""
        state_file = self.config.get("state_file")
        if not state_file:
            return
        try:
            with self.lock:
                snapshot = {
                    "saved_at": time.time(),
                    "total_pnl": self.total_pnl,
                    "total_fees": self.total_fees,
                    "total_cycles": self.total_cycles,
                    "active_grids": {
                        pair: {
                            "design": g["design"],
                            "status": g["status"],
                            "deployed_at": g["deployed_at"],
                            "levels": g["levels"],
                            "fills": g["fills"],
                            "cycles_completed": g["cycles_completed"],
                            "grid_pnl": g["grid_pnl"],
                            "grid_fees": g["grid_fees"],
                            "peak_pnl": g["peak_pnl"],
                            "max_drawdown": g["max_drawdown"],
                            "range_breaks": g["range_breaks"],
                            "last_check": g["last_check"],
                            "reservation_id": g.get("reservation_id", ""),
                        }
                        for pair, g in self.active_grids.items()
                    },
                }
            tmp = state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, default=str)
            os.replace(tmp, state_file)
        except Exception as e:
            logging.warning(f"[state] Save failed: {e}")

    def _load_state(self, portfolio_client=None):
        """Load active_grids from gridzilla_state.json on startup.

        After loading, reconcile: for each loaded grid, verify the portfolio
        reservation ID still exists (call portfolio manager). If a reservation
        is gone (orphaned from old run), log a warning and drop that grid entry
        rather than double-spending.
        """
        state_file = self.config.get("state_file")
        if not state_file or not os.path.exists(state_file):
            return

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                snapshot = json.load(f)
        except Exception as e:
            logging.warning(f"[state] Could not read state file: {e}")
            return

        # Only accept files that were written by v2 (they have an "active_grids" key
        # at top level with grid dicts that contain "design" + "levels").
        raw_grids = snapshot.get("active_grids")
        if not isinstance(raw_grids, dict):
            logging.info("[state] State file is v1 format — skipping load.")
            return

        # Fetch current live reservations from portfolio manager once.
        # /api/portfolio returns {"reservations": [{"reservation_id": ..., ...}, ...]}
        live_reservation_ids = set()
        if portfolio_client:
            try:
                resp = portfolio_client._get("/api/portfolio")
                if resp and isinstance(resp, dict):
                    reservations = resp.get("reservations", [])
                    live_reservation_ids = {r["reservation_id"] for r in reservations if "reservation_id" in r}
                    logging.info(
                        f"[state] Portfolio has {len(live_reservation_ids)} live reservations"
                    )
            except Exception as e:
                logging.warning(f"[state] Could not fetch portfolio for reconciliation: {e}")

        loaded = 0
        dropped = 0
        for pair, g in raw_grids.items():
            rid = g.get("reservation_id", "")

            # Reconcile: if a reservation_id was recorded but is no longer live,
            # that capital was already cleaned up — do not restore the grid.
            if rid and live_reservation_ids and rid not in live_reservation_ids:
                logging.warning(
                    f"[state] Dropping {pair} grid — reservation {rid} not found "
                    f"in live portfolio (orphaned from previous run)"
                )
                dropped += 1
                continue

            # Restore grid state
            grid_state = {
                "design": g.get("design", {}),
                "status": g.get("status", "ACTIVE"),
                "deployed_at": g.get("deployed_at", time.time()),
                "levels": g.get("levels", []),
                "fills": g.get("fills", []),
                "cycles_completed": g.get("cycles_completed", 0),
                "grid_pnl": g.get("grid_pnl", 0),
                "grid_fees": 0.0,  # legacy field — fee accounting retired (signal product)
                "peak_pnl": g.get("peak_pnl", 0),
                "max_drawdown": g.get("max_drawdown", 0),
                "range_breaks": g.get("range_breaks", 0),
                "last_check": g.get("last_check", time.time()),
                "reservation_id": rid,
            }
            self.active_grids[pair] = grid_state

            # Restore totals (fee totals from old state are NOT restored —
            # fee accounting is retired, P/L is gross)
            self.total_pnl += g.get("grid_pnl", 0)
            self.total_cycles += g.get("cycles_completed", 0)

            loaded += 1

        logging.info(
            f"[state] Loaded {loaded} grid(s), dropped {dropped} orphaned grid(s) "
            f"from {state_file}"
        )

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
                # Mark-to-market: realized cycles PLUS open inventory. The
                # drawdown kill measures this, not grid_pnl alone — see the
                # drawdown block in _check_grid. Present from creation so
                # every consumer sees a consistent shape.
                "unrealized_pnl": 0,
                "total_pnl_mtm": 0,
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
        self._save_state()  # persist after deploy (outside lock — _save_state takes its own lock)
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
                    fill_price = current_price
                    _kp = pair.replace("/", "")
                    if self._kraken_spot:
                        qty = level["size_usd"] / current_price
                        # LIMIT ONLY (fleet policy) — marketable limit, never market.
                        ok, txid = self._kraken_spot.buy(
                            _kp, qty, price=_limit_price(current_price, "BUY"))
                        if ok:
                            import time as _t; _t.sleep(1.5)
                            fill_price = self._kraken_spot.get_fill_price(txid, current_price)
                            logging.info(f"LIVE GRID BUY {pair} qty={qty:.6f} @ {fill_price:.4f} txid={txid}")
                        else:
                            logging.warning(f"LIVE GRID BUY FAILED {pair}: {txid} — using paper fill")

                    level["filled"] = True
                    level["fill_price"] = fill_price
                    level["fill_time"] = time.time()

                    fill = {
                        "pair": pair,
                        "side": "BUY",
                        "price": fill_price,
                        "level_price": level["price"],
                        "size_usd": level["size_usd"],
                        "fee": 0.0,  # legacy field — gross accounting (signal product)
                        "time": time.time(),
                        "take_profit": level.get("take_profit"),
                    }

                    grid["fills"].append(fill)
                    fills.append(fill)

                    logging.info(
                        f"Grid BUY filled: {pair} @ {fill_price:.6f} "
                        f"(level {level['price']:.6f})"
                    )

                elif level["side"] == "SELL" and current_price >= level["price"]:
                    # Sell level hit — check if this closes a buy
                    fill_price_sell = current_price
                    _kp = pair.replace("/", "")
                    if self._kraken_spot:
                        qty = level["size_usd"] / current_price
                        # LIMIT ONLY (fleet policy) — marketable limit, never market.
                        ok, txid = self._kraken_spot.sell(
                            _kp, qty, price=_limit_price(current_price, "SELL"))
                        if ok:
                            import time as _t; _t.sleep(1.5)
                            fill_price_sell = self._kraken_spot.get_fill_price(txid, current_price)
                            logging.info(f"LIVE GRID SELL {pair} qty={qty:.6f} @ {fill_price_sell:.4f} txid={txid}")
                        else:
                            logging.warning(f"LIVE GRID SELL FAILED {pair}: {txid} — using paper fill")

                    level["filled"] = True
                    level["fill_price"] = fill_price_sell
                    level["fill_time"] = time.time()

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
                        # Grid cycle P/L — GROSS price movement only.
                        # Signal product: subscribers pay their own exchanges' fees.
                        #
                        # Price the SAME quantity through both legs. The old
                        # formula was
                        #     sell_revenue = level["size_usd"] * (current/level_price)
                        #     pnl = sell_revenue - matching_buy["size_usd"]
                        # which subtracts the BUY's dollar size from the SELL
                        # LEVEL's dollar size. Those are different amounts, so
                        # the size mismatch was booked as profit: a live ETH
                        # cycle (buy $9,895.80 @ 1894.01, sell @ 1900.69)
                        # recorded $1,414.54 against a true gain of $34.90 —
                        # a 40x overstatement — because the sell level was
                        # sized $11,309.48. It also meant a sell landing
                        # exactly on its level scored $0.00 no matter how far
                        # the price had climbed from the buy.
                        buy_cost = matching_buy["size_usd"]
                        buy_price = matching_buy.get("price") or 0
                        if buy_price > 0:
                            # Coins bought, revalued at the sell price.
                            qty = buy_cost / buy_price
                            sell_revenue = qty * current_price
                        else:
                            # No buy price recorded — cannot price the cycle.
                            # Record nothing rather than a fabricated figure.
                            sell_revenue = buy_cost
                        pnl = sell_revenue - buy_cost
                        matching_buy["closed"] = True
                        grid["cycles_completed"] += 1
                        self.total_cycles += 1

                        # Record the COMPLETED CYCLE here, where the P/L is
                        # actually realized. trade_history was only appended
                        # in remove_grid(), i.e. on teardown — but a completed
                        # cycle RESETS the grid and keeps it running, so a
                        # grid could cycle profitably for days and never
                        # appear in trade history. That is why the dashboard
                        # showed Net P&L +$1414.54 next to Wins/Losses 0/0 and
                        # a 0.0% win rate on a winning trade: total_cycles
                        # counts cycles, trade_history counted teardowns, and
                        # the win-rate panel reads the latter.
                        self.trade_history.append({
                            "pair": pair,
                            "started": matching_buy.get("time", 0),
                            "ended": time.time(),
                            "status": "CYCLE",
                            "pnl": round(pnl, 4),
                            "fees": 0.0,  # gross accounting (signal product)
                            "cycles": 1,
                            "fills": 2,   # the buy and this sell
                            "reservation_id": grid.get("reservation_id", ""),
                            "won": pnl > 0,
                        })

                        # ...and into the expectancy tracker, which the
                        # snapshot reports via get_bot_stats(). Nothing ever
                        # called record_trade, so that block was queried and
                        # never fed — every field read 0 (win_rate 0.0%,
                        # wins/losses 0/0) beside a real +$1414.54.
                        if self.expectancy is not None:
                            try:
                                self.expectancy.record_trade(
                                    bot_id="gridzilla",
                                    pair=pair,
                                    direction="LONG",
                                    # None, not 0. A recorded entry_price of
                                    # 0.0 is not a price anybody measured, and
                                    # it lands in the DURABLE expectancy store
                                    # where it outlives the process and cannot
                                    # be told apart from a real reading. The
                                    # live store already holds a gridzilla
                                    # ETH/USD trade with entry_price 0.0 /
                                    # exit_price 0.0 beside a genuine
                                    # +$34.90 on $20,781 — the P/L is real,
                                    # the prices are fiction. r_multiple is
                                    # null for exactly this reason.
                                    entry_price=(buy_price if buy_price > 0
                                                 else None),
                                    exit_price=current_price,
                                    size_usd=matching_buy.get("size_usd", 0),
                                    duration=time.time() - matching_buy.get("time", time.time()),
                                    realized_pnl=pnl,
                                    trade_id=f"gridzilla_{pair}_{int(time.time() * 1000)}",
                                )
                            except Exception as e:
                                logging.warning(
                                    f"expectancy.record_trade failed for "
                                    f"{pair} cycle: {e}")

                    fill = {
                        "pair": pair,
                        "side": "SELL",
                        "price": current_price,
                        "level_price": level["price"],
                        "size_usd": level["size_usd"],
                        "fee": 0.0,  # legacy field — gross accounting (signal product)
                        "pnl": round(pnl, 4),
                        "time": time.time(),
                        # The cycle's buy price, so the TRADE_CLOSE event can
                        # report a real entry instead of leaving Command
                        # Center to default it to 0. None when the matching
                        # buy carried no price — absent, not zero.
                        "entry_price": buy_price if buy_price > 0 else None,
                        # Lets Command Center dedup this close against the
                        # reservation-release path, which keys on the same id.
                        "reservation_id": grid.get("reservation_id") or None,
                    }

                    grid["fills"].append(fill)
                    grid["grid_pnl"] += pnl
                    self.total_pnl += pnl
                    fills.append(fill)

                    if pnl != 0:
                        logging.info(
                            f"Grid SELL filled: {pair} @ {current_price:.6f} "
                            f"(level {level['price']:.6f}) PnL: ${pnl:.4f}"
                        )

            # Track drawdown — on TOTAL equity, realized plus open inventory.
            #
            # This measured grid_pnl alone, which is the sum of COMPLETED
            # cycles. A cycle only completes when a sell fires at
            # `current_price >= level["price"]` against a buy filled at a
            # strictly lower level, so every realized pnl is >= 0 and
            # grid_pnl is monotonically NON-DECREASING. peak_pnl therefore
            # always equalled grid_pnl and dd was structurally 0.0 forever:
            # the DD_KILLED gate below could never fire.
            #
            # The actual risk in a grid is the opposite quantity — open
            # inventory bought on the way down, which the old measure
            # ignored entirely. A grid bleeding badly on held bags reported
            # max_drawdown 0.0 and was killable only by the range-break
            # path. Everything needed was already tracked per level
            # (filled / fill_price / size_usd), so unrealized is computed
            # here rather than inferred.
            _unrealized = 0.0
            for _lv in grid["levels"]:
                if _lv.get("filled") and _lv.get("side") == "BUY":
                    _fp = _lv.get("fill_price") or 0
                    if _fp > 0 and current_price > 0:
                        _qty = (_lv.get("size_usd") or 0) / _fp
                        _unrealized += _qty * (current_price - _fp)
            grid["unrealized_pnl"] = round(_unrealized, 4)
            _equity = (grid.get("grid_pnl") or 0) + _unrealized
            grid["total_pnl_mtm"] = round(_equity, 4)
            # .get with a default: grids restored from a state file written
            # before these keys existed would otherwise KeyError here, on
            # the first scan after an upgrade restart.
            if _equity > (grid.get("peak_pnl") or 0):
                grid["peak_pnl"] = _equity
            dd = (grid.get("peak_pnl") or 0) - _equity
            if dd > (grid.get("max_drawdown") or 0):
                grid["max_drawdown"] = dd

            # Publish fills
            if fills and self.publisher:
                for fill in fills:
                    # A grid SELL is only a closed trade when it completed a
                    # round-trip. Every sell used to emit TRADE_CLOSE, so
                    # half-cycles landed in /api/trades carrying pnl=0 — rows
                    # that look like flat trades but measure nothing. They
                    # inflate the win-rate denominator (3 unrealized fills +
                    # 1 real win reads 25%, not 100%) and are exactly the
                    # unpriced-close class the broadcaster already suppresses.
                    #
                    # GRID_FILL keeps the fill visible on the bus without
                    # claiming a trade closed.
                    _realized = fill.get("pnl")
                    _closed = (fill["side"] != "BUY"
                               and isinstance(_realized, (int, float))
                               and _realized != 0)
                    if fill["side"] == "BUY":
                        event_type = "TRADE_OPEN"
                    elif _closed:
                        event_type = "TRADE_CLOSE"
                    else:
                        event_type = "GRID_FILL"
                    try:
                        self.publisher.emit(event_type, {
                            "bot": "gridzilla",
                            "pair": pair,
                            "direction": "LONG",  # grids are always long-side cycles
                            "price": fill["price"],
                            # Real prices for the round-trip. Without these
                            # Command Center defaulted entry_price to 0 and
                            # wrote it into the DURABLE expectancy store: four
                            # gridzilla rows after the 2026-08-07 relaunch
                            # carried entry 0 beside genuine P/L, and
                            # r_multiple is null for exactly that reason.
                            "entry_price": fill.get("entry_price"),
                            "exit_price": fill["price"] if _closed else None,
                            "size_usd": fill["size_usd"],
                            "pnl": fill.get("pnl", 0),
                            "fee": fill["fee"],
                            # Dedup key shared with the reservation-release
                            # path, which recorded the SAME close under a
                            # different id — live proof was two POL/USD rows
                            # with identical gross_pnl 162.9797.
                            "reservation_id": fill.get("reservation_id"),
                            "grid_cycle": grid["cycles_completed"],
                            # Explicit: downstream can tell a measured result
                            # from a leg that simply hasn't resolved yet.
                            "realized": bool(_closed),
                        })
                    except Exception:
                        pass

        if fills:
            self._save_state()  # persist level fills (outside lock — _save_state takes its own lock)
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
        summary = None
        with self.lock:
            if pair in self.active_grids:
                grid = self.active_grids.pop(pair)
                summary = {
                    "pair": pair,
                    "started": grid["deployed_at"],
                    "ended": time.time(),
                    "status": grid["status"],
                    "pnl": grid["grid_pnl"],  # gross price movement
                    "fees": 0.0,  # legacy field — gross accounting (signal product)
                    "cycles": grid["cycles_completed"],
                    "fills": len(grid["fills"]),
                    "reservation_id": grid.get("reservation_id", ""),
                }
                # Completed cycles are recorded as they happen (see
                # check_fills). Appending this teardown summary too would
                # double-count their P/L — grid_pnl is the SUM of the cycles
                # already in trade_history. Only record a teardown that
                # carries P/L no cycle claimed, i.e. a grid torn down with
                # zero completed cycles.
                if not grid.get("cycles_completed"):
                    summary["status"] = grid["status"]
                    summary["won"] = (grid["grid_pnl"] or 0) > 0
                    self.trade_history.append(summary)
        if summary is not None:
            self._save_state()  # persist after removal (outside lock — _save_state takes its own lock)
        return summary

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
                    "fees": 0.0,  # legacy field — gross accounting (signal product)
                    "net_pnl": round(g["grid_pnl"], 4),  # net == gross now; key kept for consumers
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

            # Whale alert status — only pause on EXTREME alerts for pairs we actually trade.
            # Fleet-wide whale alerts on unrelated pairs (e.g. OP/USD) must not block
            # grids on ETH/USD, XRP/USD, ATOM/USD etc.
            our_pairs = set(CONFIG.get("universe", []))
            whale_alerts = self.bus.whale_alerts(pair=None, max_age=300)
            self.whale_alert_active = any(
                a.get("data", {}).get("tier") == "EXTREME"
                and a.get("data", {}).get("pair") in our_pairs
                for a in (whale_alerts or [])
            )

            # PHITEX critical — check fleet-wide status
            phitex = self.bus.phitex_status(pair=None)
            self.phitex_critical = (
                phitex is not None and phitex.get("fleet_score", 0) > 0.5
            )

            # CHRONOS: temporal bias — LOG-ONLY here, not a size modifier.
            # Grids deploy a whole allocation split across N pre-computed
            # levels (GridArchitect.design()), not a single per-trade size —
            # there is no clean entry-size hook to shave 25% off the way the
            # single-leg traders do. Consumption is logged so the wiring is
            # provable and visible to the operator; it never changes grid
            # sizing or level count.
            temporal = self.bus.chronos_temporal(max_age=3600)
            anomaly = temporal.get("time_anomaly") if temporal else None
            if anomaly:
                logging.info(
                    f"TEMPORAL {anomaly.get('direction', '?').upper()} bias: "
                    f"TIME_ANOMALY {anomaly.get('direction')} (n={anomaly.get('n')}, "
                    f"bias={anomaly.get('bias_pct')}%) — log-only, grid sizing unaffected"
                )
            for se in (temporal.get("session_events") or []) if temporal else []:
                if se.get("type") == "SESSION_OVERLAP" and (time.time() - se.get("ts", 0)) < 900:
                    logging.info(f"TEMPORAL CONTEXT: SESSION_OVERLAP {se.get('window')} — high volatility window")
                    break

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

        # Live Kraken spot execution
        self._kraken_spot = None
        if _KrakenSpotClient and _fc and _fc.is_live():
            self._kraken_spot = _KrakenSpotClient()
            if self._kraken_spot.has_credentials:
                logging.info("LIVE MODE: Kraken spot client initialized for Gridzilla")
            else:
                logging.warning("LIVE MODE: No Kraken API credentials — paper fallback")
                self._kraken_spot = None

        self.regime_detector = RegimeDetector(config)
        self.grid_architect = GridArchitect(config)
        self.executor = GridExecutor(config, self.kraken, self.publisher,
                                     expectancy=self.expectancy)
        self.executor._kraken_spot = self._kraken_spot  # live spot execution
        self.intel = BusIntelligence(self.bus_listener)

        # State
        self.running = False
        self.scan_count = 0
        self.pair_analysis = {}  # {pair: last_analysis}
        self.start_time = time.time()

        # Restore persisted grid state (must come after portfolio is initialized)
        self.executor._load_state(portfolio_client=self.portfolio)

        logging.info(f"Gridzilla v{config['version']} initialized")

    def start(self):
        """Start the main scan loop."""
        self.running = True

        # Start HTTP server in background
        server_thread = threading.Thread(target=self._run_server, daemon=True)
        server_thread.start()

        # BusListener starts its poll thread in __init__ — no explicit .start() needed

        logging.info(f"Gridzilla running on port {self.config['port']}")

        # Main loop
        _last_checkpoint = 0.0
        while self.running:
            try:
                self._scan_cycle()
            except Exception as e:
                logging.error(f"Scan cycle error: {e}")
                traceback.print_exc()

            # Periodic checkpoint — makes the state file's AGE a real
            # liveness signal.
            #
            # All four other _save_state() calls are event-driven (deploy,
            # fill, removal, reservation-id stamp), so a quiet market and a
            # hung writer produce IDENTICAL staleness. Observed 2026-08-13:
            # gridzilla_state.json was 55 minutes old while the bot was
            # perfectly healthy — the file matched live state exactly, it
            # just had nothing new to record. That ambiguity is the same
            # absent-vs-broken confusion this fleet keeps hitting, and it
            # defeats any age-based staleness check over this file.
            #
            # A write every 5 minutes is negligible (one small atomic JSON
            # write) and means an age beyond that is genuinely a stalled
            # loop rather than a calm one.
            _now = time.time()
            if _now - _last_checkpoint >= 300:
                try:
                    self.executor._save_state()
                    _last_checkpoint = _now
                except Exception:
                    logging.warning("periodic checkpoint failed", exc_info=True)

            time.sleep(self.config["scan_interval"])

    def _scan_cycle(self):
        """One complete scan cycle."""
        self.scan_count += 1

        # Lease heartbeat: declare held reservation ids so the pool can sweep
        # anything a wiring bug stranded (never raises).
        if self.portfolio:
            self.portfolio.confirm_reservations(
                [g.get("reservation_id") for g in self.executor.active_grids.values() if g.get("reservation_id")])

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

        # Current exposure, in DOLLARS.
        #
        # This was `sum(g.get("levels", 0) * 10 ...)`, and get_active_grids
        # returns "levels" as design.n_levels — a COUNT OF GRID LINES, not
        # money. With 5 lines per grid and a 10-pair universe that caps out
        # at 10*5*10 = $500, against a max_exposure of balance * 0.30 *
        # multiplier (>= balance * 0.15). Tripping it needed a balance
        # under ~$3.3k, but the deploy path below already refuses to act
        # below $10k — the two ranges are disjoint, so the 30% portfolio
        # ceiling could never fire and only the per-pair 5% cap constrained
        # anything. Ten pairs at 5% each is 50% of the portfolio.
        #
        # allocation is the real capital committed to each grid (set at
        # deploy from per_pair_alloc), so summing it is the actual exposure.
        active_full = self.executor.active_grids
        current_exposure = 0.0
        for _g in list(active_full.values()):
            if not isinstance(_g, dict):
                continue
            _alloc = _g.get("allocation")
            if isinstance(_alloc, (int, float)) and _alloc > 0:
                current_exposure += float(_alloc)

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
                                # Report outcome to signal aggregator
                                try:
                                    import urllib.request as urlreq
                                    url = f"{self.config['cc_url']}/api/signals/outcome"
                                    data = json.dumps({
                                        "bot_id": "gridzilla",
                                        "pair": pair,
                                        "direction": "LONG",
                                        "won": summary["pnl"] > 0,
                                        "pnl": float(summary["pnl"]),  # gross price movement
                                        "fees": 0.0  # legacy key kept for endpoint shape (signal product)
                                    }).encode("utf-8")
                                    req = urlreq.Request(url, data=data, headers={"Content-Type": "application/json"})
                                    urlreq.urlopen(req, timeout=3)
                                except Exception:
                                    pass
                                
                                self.portfolio.release(summary["reservation_id"], pnl=summary["pnl"])  # gross
                            except Exception:
                                pass
                        continue

                    # Check drawdown kill
                    per_pair_alloc = portfolio_balance * self.config["max_per_pair_pct"]
                    if self.executor.check_drawdown_kill(pair, per_pair_alloc):
                        summary = self.executor.remove_grid(pair)
                        if summary and summary.get("reservation_id") and self.portfolio:
                            try:
                                # Report outcome to signal aggregator
                                try:
                                    import urllib.request as urlreq
                                    url = f"{self.config['cc_url']}/api/signals/outcome"
                                    data = json.dumps({
                                        "bot_id": "gridzilla",
                                        "pair": pair,
                                        "direction": "LONG",
                                        "won": summary["pnl"] > 0,
                                        "pnl": float(summary["pnl"]),  # gross price movement
                                        "fees": 0.0  # legacy key kept for endpoint shape (signal product)
                                    }).encode("utf-8")
                                    req = urlreq.Request(url, data=data, headers={"Content-Type": "application/json"})
                                    urlreq.urlopen(req, timeout=3)
                                except Exception:
                                    pass
                                
                                self.portfolio.release(summary["reservation_id"], pnl=summary["pnl"])  # gross
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

                if per_pair_alloc < 500:  # minimum allocation — must clear 5% pool floor (raised from $300 on 2026-04-07)
                    continue

                design = self.grid_architect.design(pair, candles, regime, per_pair_alloc)

                # design() returns None if net profit floor not met
                if design is None:
                    continue

                # Fee-ratio deploy gate removed — signal product, no fee-survival
                # math. Signal quality is enforced upstream by the 1.2% spacing
                # floor, the 5-line cap, and the $0.50 gross-per-level floor.
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
                # Store reservation ID on the grid state, then persist so the
                # reservation_id survives a restart (deploy_grid saved without it)
                if _rid and pair in self.executor.active_grids:
                    self.executor.active_grids[pair]["reservation_id"] = _rid
                    self.executor._save_state()
                current_exposure += per_pair_alloc

            except Exception as e:
                logging.debug(f"Error scanning {pair}: {e}")

        # Publish scan complete
        if self.publisher and self.scan_count % 5 == 0:
            try:
                self.publisher.emit("SCAN_COMPLETE", {
                    "bot": "gridzilla",
                    "pairs_scanned": len(self.config["universe"]),
                    "active_grids": len(self.executor.active_grids),
                    "total_pnl": round(self.executor.total_pnl, 4),  # gross
                    "total_fees": 0.0,  # legacy key kept for consumers (signal product)
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
            # P/L is GROSS price movement (signal product — subscribers pay
            # their own exchanges' fees). total_fees and net_pnl keys are kept
            # because the CC normalizer (_normalize_gridzilla) reads both;
            # net_pnl == total_pnl now.
            "total_pnl": round(self.executor.total_pnl, 4),
            "total_fees": 0.0,
            "net_pnl": round(self.executor.total_pnl, 4),
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
              try:
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
              except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                  pass  # Client disconnected — normal during health checks

            def log_message(self, fmt, *args):
                pass  # silent

        # ThreadingHTTPServer, not HTTPServer: the plain server handles one
        # request at a time, so while the grid recalculates its levels the port
        # stops answering. Command Center's health check times out and reports
        # the bot DOWN even though it is healthy. Measured worst case here was
        # 7.8s — the longest stall of any bot in the fleet.
        # daemon_threads so request threads never block shutdown.
        server = ThreadingHTTPServer(("0.0.0.0", self.config["port"]), Handler)
        server.daemon_threads = True
        server.serve_forever()


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Gridzilla v2 — Adaptive Intelligent Grid Engine")
    parser.add_argument("--auto", action="store_true", help="Headless mode (fleet launcher)")
    args = parser.parse_args()

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
        logging.info("Gridzilla shutting down — releasing all portfolio reservations...")
        engine.running = False
        # Release all active grid reservations back to the pool on clean shutdown
        if engine.portfolio:
            for pair, grid_state in list(engine.executor.active_grids.items()):
                rid = grid_state.get("reservation_id", "")
                if rid:
                    try:
                        pnl = grid_state.get("grid_pnl", 0)  # gross — no fee netting (signal product)
                        engine.portfolio.release(rid, pnl=pnl)
                        logging.info(f"Released reservation {rid} for {pair} (pnl={pnl:.4f})")
                    except Exception as e:
                        logging.warning(f"Failed to release reservation {rid}: {e}")


if __name__ == "__main__":
    main()
