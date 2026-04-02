#!/usr/bin/env python3
"""
COMMAND CENTER — Fleet Aggregator & Dashboard Server
=====================================================
Polls all registered trading bots, normalizes their metrics, and serves
a unified dashboard on port 9000.

Usage: python command_center.py
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import string
import subprocess
import sys
import threading
import time
from collections import Counter
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

import requests

from standards import normalize_pair

log = logging.getLogger("command_center")

from fleet_logger import FleetLogger
from event_bus import EventBus
from collector import start_collector, register_brainiac_endpoints
from signal_aggregator import SignalAggregator
from signal_decomposition import SignalDecomposition
from expectancy import ExpectancyTracker
from signal_decay import SignalDecay
from fleet_intel_score import FleetIntelScore

import urllib.request as _urlreq  # UPGRADE: Fleet Intelligence — for LLM briefing

# ---------------------------------------------------------------------------
# Bot Registry
# ---------------------------------------------------------------------------

BOT_REGISTRY = [
    {"id": "turtlesue",  "name": "TurtleSue",  "port": 8070, "color": "#00e676", "endpoints": ["/api/snapshot"]},
    {"id": "sentinel",   "name": "Sentinel",    "port": 8071, "color": "#00bfa5", "endpoints": ["/api/snapshot"]},
    {"id": "trinity",    "name": "Trinity",     "port": 8072, "color": "#00b0ff", "endpoints": ["/api/snapshot"]},
    {"id": "hivemind",   "name": "HiveMind",    "port": 8073, "color": "#ffab00", "endpoints": ["/api/snapshot"]},
    {"id": "nexusbrain", "name": "NexusBrain",  "port": 8074, "color": "#d500f9", "endpoints": ["/api/snapshot"]},
    {"id": "trekbot",    "name": "TrekBot",     "port": 8080, "color": "#ff6d00", "endpoints": ["/health", "/positions", "/analytics"]},
    {"id": "oracle",     "name": "Oracle",      "port": 8075, "color": "#76ff03", "endpoints": ["/api/snapshot"]},
    {"id": "deepblue",  "name": "Deep Blue",   "port": 8076, "color": "#18ffff", "endpoints": ["/api/snapshot"]},
    {"id": "gridzilla", "name": "Gridzilla",   "port": 8077, "color": "#ffd600", "endpoints": ["/api/snapshot"]},
    {"id": "phitex",   "name": "PHITEX",      "port": 8078, "color": "#e040fb", "endpoints": ["/api/snapshot"]},
    {"id": "aegis",    "name": "AEGIS",       "port": 8079, "color": "#e0e0e0", "endpoints": ["/api/snapshot"]},
    {"id": "nexus",      "name": "NEXUS",       "port": 8082, "color": "#26c6da", "endpoints": ["/api/snapshot"]},
    {"id": "rubberband", "name": "Rubberband",  "port": 8083, "color": "#00e5ff", "endpoints": ["/api/snapshot"]},
    {"id": "contrarian", "name": "Contrarian",  "port": 8084, "color": "#ff1744", "endpoints": ["/api/snapshot"]},
    {"id": "arbitrageur","name": "Arbitrageur", "port": 8085, "color": "#7c4dff", "endpoints": ["/api/snapshot"]},
    {"id": "chronos",    "name": "Chronos",     "port": 8086, "color": "#ff9100", "endpoints": ["/api/snapshot"]},
]

POLL_INTERVAL = 4       # seconds
REQUEST_TIMEOUT = 3     # seconds per HTTP call
MAX_FEED_SIZE = 50
UNIVERSE_REFRESH_HOURS = 6  # refresh universe every 6 hours
KRAKEN_REST = "https://api.kraken.com/0/public"

# ---------------------------------------------------------------------------
# Thread-safe state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state = {
    "bots": {},
    "aggregate": {},
    "feed": [],
}

# Universe state
_universe = {
    "pairs": [],            # [{"display": "BTC/USD", "kraken_pair": "XXBTZUSD", "base": "BTC", "volume_usd": 123456}, ...]
    "last_refresh": 0,
    "config": {},
}
_universe_lock = threading.Lock()

# Event bus — real-time pub/sub for fleet communication
_event_bus = EventBus(max_history=1000)

# Jim's measurement infrastructure
_signal_aggregator = SignalAggregator()
_signal_decomposition = SignalDecomposition()
_expectancy_tracker = ExpectancyTracker()
_signal_decay = SignalDecay()
_fleet_intel = FleetIntelScore()
_start_time = time.time()

# ---------------------------------------------------------------------------
# UPGRADE: Fleet Intelligence Layer — state
# ---------------------------------------------------------------------------

# A. Factor Exposure Monitor — aggregated factor scores across fleet positions
_fleet_exposure: dict = {}           # updated each poll cycle
_fleet_exposure_lock = threading.Lock()

# B. Cross-Bot Correlation Tracker — rolling PnL correlations between bots
_bot_pnl_history: dict[str, list[float]] = {}   # bot_id -> list of recent PnL values
_bot_correlation_matrix: dict = {}               # updated each poll cycle
_bot_correlation_alerts: list[dict] = []
_bot_correlation_lock = threading.Lock()

# C. Performance Attribution — alpha/beta/cost decomposition
_perf_attribution: dict = {}         # latest decomposition
_perf_attribution_lock = threading.Lock()

# D. LLM Fleet Briefing — cached natural-language briefing
_fleet_briefing: dict = {"text": "Briefing not yet generated.", "generated_at": 0, "status": "pending"}
_fleet_briefing_lock = threading.Lock()
BRIEFING_INTERVAL = 300  # 5 minutes
INFERENCE_URL = "http://localhost:9001"


# ---------------------------------------------------------------------------
# Portfolio Manager — Central Capital Pool
# ---------------------------------------------------------------------------

PORTFOLIO_TOTAL = 10000.00  # Single shared pool

PORTFOLIO_LIMITS = {
    "max_deployed_pct": 80,      # Max 80% of pool deployed at once
    "max_per_bot_pct": 30,       # Max 30% to any single bot
    "max_per_pair_pct": 20,      # Max 20% on any single pair
    "max_directional_pct": 60,   # Max 60% long or 60% short
    "max_per_trade_pct": 5,      # Max 5% risked on a single trade
}

TRADING_BOTS = {"turtlesue", "nexusbrain", "gridzilla", "trekbot", "rubberband"}

PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")


class PortfolioManager:
    """Central capital pool shared by all trading bots."""

    def __init__(self, total: float, limits: dict[str, int], filepath: str):
        self._lock = threading.Lock()
        self.total = total
        self.limits = limits
        self.filepath = filepath
        self.reservations: dict[str, dict] = {}
        self.history: list[dict] = []
        self._load()

    # ── Persistence ──

    def _load(self) -> None:
        """Load state from portfolio.json if it exists."""
        try:
            with open(self.filepath, "r") as f:
                data = json.load(f)
            self.total = data.get("total", self.total)
            self.reservations = data.get("reservations", {})
            self.history = data.get("history", [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        """Atomic write to portfolio.json."""
        data = {
            "total": self.total,
            "updated_at": time.time(),
            "reservations": self.reservations,
            "history": self.history[-200:],
        }
        tmp = self.filepath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.filepath)

    # ── Computed Properties ──

    def deployed(self) -> float:
        return sum(r["amount"] for r in self.reservations.values())

    def available(self) -> float:
        return self.total - self.deployed()

    def _exposure_by(self, key: str) -> dict[str, float]:
        totals: dict[str, float] = {}
        for r in self.reservations.values():
            k = r.get(key, "unknown")
            totals[k] = totals.get(k, 0) + r["amount"]
        return totals

    def exposure_by_bot(self) -> dict[str, float]:
        return self._exposure_by("bot_id")

    def exposure_by_pair(self) -> dict[str, float]:
        return self._exposure_by("pair")

    def exposure_by_direction(self) -> dict[str, float]:
        return self._exposure_by("direction")

    def available_snapshot(self) -> dict:
        """Return available-capital summary under lock."""
        with self._lock:
            deployed = self.deployed()
            return {
                "total": self.total,
                "deployed": round(deployed, 2),
                "available": round(self.total - deployed, 2),
                "deployed_pct": round(deployed / self.total * 100, 1) if self.total > 0 else 0,
            }

    def exposure_snapshot(self) -> dict:
        """Return full exposure breakdown under lock."""
        with self._lock:
            return {
                "by_bot": self.exposure_by_bot(),
                "by_pair": self.exposure_by_pair(),
                "by_direction": self.exposure_by_direction(),
            }

    # ── Core Operations ──

    def reserve(self, bot_id: str, pair: str, direction: str, amount: float,
                stop_loss_pct: Optional[float] = None) -> dict:
        """Validate risk limits and reserve capital. Returns dict with ok/reason."""
        pair = normalize_pair(pair) or pair  # canonical format for per-pair limits
        with self._lock:
            # Validate bot is a trading bot
            if bot_id not in TRADING_BOTS:
                return {"ok": False, "reason": f"Bot '{bot_id}' is not a trading bot"}

            deployed = self.deployed()
            lim = self.limits

            # 1. Total deployment limit
            max_deployed = self.total * lim["max_deployed_pct"] / 100
            if deployed + amount > max_deployed:
                avail = max_deployed - deployed
                return {"ok": False, "reason": f"Deployment limit: {deployed+amount:.2f} > {max_deployed:.2f} max ({lim['max_deployed_pct']}%). Available: {avail:.2f}"}

            # 2. Per-bot limit
            bot_exp = self.exposure_by_bot().get(bot_id, 0)
            max_bot = self.total * lim["max_per_bot_pct"] / 100
            if bot_exp + amount > max_bot:
                return {"ok": False, "reason": f"Bot limit: {bot_id} at {bot_exp:.2f}+{amount:.2f} > {max_bot:.2f} max ({lim['max_per_bot_pct']}%)"}

            # 3. Per-pair limit
            pair_exp = self.exposure_by_pair().get(pair, 0)
            max_pair = self.total * lim["max_per_pair_pct"] / 100
            if pair_exp + amount > max_pair:
                return {"ok": False, "reason": f"Pair limit: {pair} at {pair_exp:.2f}+{amount:.2f} > {max_pair:.2f} max ({lim['max_per_pair_pct']}%)"}

            # 4. Directional limit
            dir_exp = self.exposure_by_direction().get(direction, 0)
            max_dir = self.total * lim["max_directional_pct"] / 100
            if dir_exp + amount > max_dir:
                return {"ok": False, "reason": f"Direction limit: {direction} at {dir_exp:.2f}+{amount:.2f} > {max_dir:.2f} max ({lim['max_directional_pct']}%)"}

            # 5. Per-trade limit
            max_trade = self.total * lim["max_per_trade_pct"] / 100
            if amount > max_trade:
                return {"ok": False, "reason": f"Trade limit: {amount:.2f} > {max_trade:.2f} max ({lim['max_per_trade_pct']}%)"}

            # 6. Concentration check — no single pair > 40% of deployed capital
            projected_deployed = deployed + amount
            if projected_deployed > 0:
                projected_pair = pair_exp + amount
                pair_concentration = projected_pair / projected_deployed
                if pair_concentration > 0.40:
                    return {"ok": False, "reason": f"Concentration limit: {pair} would be {pair_concentration:.0%} of deployed (max 40%)"}

            # 7. Direction balance — no more than 70% in one direction
            dir_totals = self.exposure_by_direction()
            long_total = dir_totals.get("LONG", 0)
            short_total = dir_totals.get("SHORT", 0)
            if direction == "LONG":
                long_total += amount
            else:
                short_total += amount
            total_directional = long_total + short_total
            if total_directional > 0:
                dominant_pct = max(long_total, short_total) / total_directional
                if dominant_pct > 0.70:
                    dominant_dir = "LONG" if long_total > short_total else "SHORT"
                    return {"ok": False, "reason": f"Direction balance: {dominant_dir} would be {dominant_pct:.0%} (max 70%)"}

            # 8. Fee floor — reject tiny trades where fees dominate
            # Kraken taker fee ~0.26%, round trip = ~0.52%
            estimated_round_trip_fee = amount * 0.0052
            min_profitable_amount = 5.0  # $5 minimum to cover ~$0.026 fee
            if amount < min_profitable_amount:
                return {"ok": False, "reason": f"Fee floor: ${amount:.2f} trade too small (min ${min_profitable_amount:.0f} to cover fees)"}

            # 9. Fleet intelligence gate — check engine risk assessment
            try:
                intel = _fleet_intel.get_score(pair)
                risk_mult = intel.get("risk_multiplier", 1.0)
                warnings = intel.get("active_warnings", [])
                if risk_mult < 0.3 and warnings:
                    return {"ok": False, "reason": f"Fleet intelligence block: risk={risk_mult:.2f}, warnings={warnings[:2]}"}
            except Exception:
                pass  # don't let intel failure block trading

            # All checks passed — create reservation
            suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
            rid = f"{bot_id}_{pair}_{int(time.time())}_{suffix}"

            self.reservations[rid] = {
                "bot_id": bot_id,
                "pair": pair,
                "direction": direction.upper(),
                "amount": amount,
                "stop_loss_pct": stop_loss_pct,
                "reserved_at": time.time(),
            }

            self.history.append({
                "action": "reserve",
                "reservation_id": rid,
                "bot_id": bot_id,
                "pair": pair,
                "direction": direction.upper(),
                "amount": amount,
                "timestamp": time.time(),
            })

            self._save()
            return {"ok": True, "reservation_id": rid, "amount": amount, "available": self.available()}

    def release(self, reservation_id: str, pnl: float = 0.0) -> dict:
        """Release a reservation and apply PnL to the pool."""
        with self._lock:
            res = self.reservations.pop(reservation_id, None)
            if not res:
                return {"ok": False, "reason": f"Reservation '{reservation_id}' not found"}

            self.total += pnl

            self.history.append({
                "action": "release",
                "reservation_id": reservation_id,
                "bot_id": res["bot_id"],
                "pair": res["pair"],
                "amount": res["amount"],
                "pnl": pnl,
                "new_total": self.total,
                "timestamp": time.time(),
            })

            self._save()
            return {"ok": True, "released": res["amount"], "pnl": pnl, "new_total": self.total,
                    "available": self.available(), "reservation": res}

    def force_release_stale(self, max_age_hours: int = 24) -> int:
        """Release reservations older than max_age_hours. Returns count released."""
        with self._lock:
            cutoff = time.time() - max_age_hours * 3600
            stale = [rid for rid, r in self.reservations.items() if r["reserved_at"] < cutoff]
            for rid in stale:
                res = self.reservations.pop(rid)
                self.history.append({
                    "action": "force_release",
                    "reservation_id": rid,
                    "bot_id": res["bot_id"],
                    "pair": res["pair"],
                    "amount": res["amount"],
                    "reason": "stale",
                    "timestamp": time.time(),
                })
            if stale:
                self._save()
            return len(stale)

    def state(self) -> dict:
        """Return full portfolio state for API response."""
        with self._lock:
            deployed = self.deployed()
            avail = self.available()
            deployed_pct = (deployed / self.total * 100) if self.total > 0 else 0

            # Per-bot breakdown
            by_bot = {}
            for rid, r in self.reservations.items():
                bid = r["bot_id"]
                if bid not in by_bot:
                    by_bot[bid] = {"amount": 0, "positions": 0, "pairs": []}
                by_bot[bid]["amount"] += r["amount"]
                by_bot[bid]["positions"] += 1
                if r["pair"] not in by_bot[bid]["pairs"]:
                    by_bot[bid]["pairs"].append(r["pair"])

            # Per-pair breakdown
            by_pair = {}
            for rid, r in self.reservations.items():
                p = r["pair"]
                if p not in by_pair:
                    by_pair[p] = {"amount": 0, "bots": []}
                by_pair[p]["amount"] += r["amount"]
                if r["bot_id"] not in by_pair[p]["bots"]:
                    by_pair[p]["bots"].append(r["bot_id"])

            # Direction breakdown
            by_dir = self.exposure_by_direction()

            # Risk status — relative to current AEGIS-adjusted limit
            lim = self.limits
            max_pct = lim["max_deployed_pct"]
            usage_ratio = deployed_pct / max_pct if max_pct > 0 else 0
            if usage_ratio > 1.0:
                risk_status = "RED"        # over limit (violation)
            elif usage_ratio > 0.90:
                risk_status = "AMBER"      # near limit (>90% of max)
            else:
                risk_status = "GREEN"      # within limits

            return {
                "total": self.total,
                "deployed": round(deployed, 2),
                "available": round(avail, 2),
                "deployed_pct": round(deployed_pct, 1),
                "cash_floor_pct": 100 - lim["max_deployed_pct"],
                "risk_status": risk_status,
                "limits": lim,
                "by_bot": by_bot,
                "by_pair": by_pair,
                "by_direction": by_dir,
                "active_reservations": len(self.reservations),
                "history_recent": self.history[-20:],
            }


# Module-level instances (initialized in main())
_portfolio_mgr = None
_fleet_logger = None


# ---------------------------------------------------------------------------
# JSON safety — handles inf, nan, numpy types
# ---------------------------------------------------------------------------

def _json_default(obj):
    """Custom default handler for json.dumps."""
    # Handle float inf / nan
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return 0.0
        return obj
    # numpy scalar types (if numpy happens to be importable)
    try:
        import numpy as np
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            v = float(obj)
            if math.isinf(v) or math.isnan(v):
                return 0.0
            return v
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.bool_):
            return bool(obj)
    except ImportError:
        pass
    # Fallback
    return str(obj)


def _safe_json(obj: Any) -> str:
    """Serialize to JSON string, safe for inf/nan/numpy."""
    return json.dumps(obj, default=_json_default, allow_nan=False)


def _sanitize(obj: Any) -> Any:
    """Walk a structure and replace inf/nan floats with 0.0.

    json.dumps never calls `default` for native Python floats (they're a
    natively serializable type), so _json_default cannot intercept them.
    This function must be called at the ingestion boundary (after fetching
    bot data) to prevent allow_nan=False from raising ValueError.
    """
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return 0.0
    return obj


# ---------------------------------------------------------------------------
# Universe Discovery -- shared pair list for the entire fleet
# ---------------------------------------------------------------------------

KRAKEN_CLEAN = {
    "XXBT": "BTC", "XBT": "BTC", "XETH": "ETH", "XLTC": "LTC",
    "XXRP": "XRP", "XDOGE": "DOGE", "XXLM": "XLM", "XXMR": "XMR",
}

def _clean_base(raw: str) -> str:
    """Normalize a Kraken base-asset code to a common ticker symbol.

    Kraken prefixes ISO 4217 currency codes with 'X' (crypto) or 'Z' (fiat),
    e.g. 'XXBT' -> 'BTC', 'ZUSD' -> 'USD'.  Strip the prefix when the raw
    code is longer than a standard 3-char ticker.
    """
    if raw in KRAKEN_CLEAN:
        return KRAKEN_CLEAN[raw]
    if len(raw) > 3 and raw[0] in ("X", "Z"):
        return raw[1:]
    return raw


def _load_universe_config():
    """Load universe.json from same directory."""
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universe.json")
    try:
        with open(cfg_path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "count": 20, "quote": "USD", "minimum_volume_usd": 50000,
            "exclude": [], "pin": ["BTC/USD", "ETH/USD", "SOL/USD"],
        }


def _discover_universe():
    """Fetch top Kraken USD pairs by volume. Returns list of pair dicts."""
    config = _load_universe_config()
    count = config.get("count", 20)
    min_vol = config.get("minimum_volume_usd", 50000)
    exclude = set(config.get("exclude", []))
    pin = config.get("pin", [])

    try:
        # Get all asset pairs
        resp = requests.get(f"{KRAKEN_REST}/AssetPairs", timeout=15)
        data = resp.json()
        if data.get("error"):
            return []

        usd_pairs = []
        for name, info in data.get("result", {}).items():
            if info.get("status") == "delisted":
                continue
            quote = info.get("quote", "")
            if quote not in ("ZUSD", "USD") and not name.endswith("USD"):
                continue

            base_raw = info.get("base", "")
            wsname = info.get("wsname", "")
            if "/" in wsname:
                base_raw = wsname.split("/")[0]
            clean = _clean_base(base_raw)
            display = f"{clean}/USD"

            if display in exclude:
                continue

            usd_pairs.append({
                "display": display,
                "base": clean,
                "kraken_pair": name,
            })

        # Batch ticker fetch for volume ranking
        pair_names = [p["kraken_pair"] for p in usd_pairs]
        volumes = {}

        for batch_start in range(0, len(pair_names), 20):
            batch = pair_names[batch_start:batch_start + 20]
            pair_str = ",".join(batch)
            try:
                tresp = requests.get(f"{KRAKEN_REST}/Ticker?pair={pair_str}", timeout=15)
                tdata = tresp.json()
                for key, tinfo in tdata.get("result", {}).items():
                    last = float(tinfo.get("c", [0])[0])
                    vol24 = float(tinfo.get("v", [0, 0])[1])
                    vol_usd = vol24 * last
                    volumes[key] = vol_usd
            except Exception:
                log.debug("Universe ticker batch failed (batch starting %d)", batch_start, exc_info=True)
            time.sleep(0.3)

        # Attach volumes and filter
        qualified = []
        for p in usd_pairs:
            vol = volumes.get(p["kraken_pair"], 0)
            # Try fuzzy match on key
            if vol == 0:
                for k, v in volumes.items():
                    if p["kraken_pair"] in k or k in p["kraken_pair"]:
                        vol = v
                        break
            p["volume_usd"] = round(vol, 2)
            if vol >= min_vol:
                qualified.append(p)

        # Deduplicate by display name
        seen = set()
        deduped = []
        for p in qualified:
            if p["display"] not in seen:
                seen.add(p["display"])
                deduped.append(p)

        # Sort by volume
        deduped.sort(key=lambda x: x["volume_usd"], reverse=True)

        # Ensure pinned pairs are always included (reserve slots for them)
        pinned = [p for p in deduped if p["display"] in pin]
        non_pinned = [p for p in deduped if p["display"] not in pin]
        top = pinned + non_pinned[:count - len(pinned)]
        return top[:count]

    except Exception as e:
        print(f"  Universe discovery error: {e}")
        return []


def _refresh_universe():
    """Refresh universe if stale."""
    with _universe_lock:
        age = time.time() - _universe["last_refresh"]
        if age < UNIVERSE_REFRESH_HOURS * 3600 and _universe["pairs"]:
            return  # still fresh

    pairs = _discover_universe()
    if pairs:
        with _universe_lock:
            _universe["pairs"] = pairs
            _universe["last_refresh"] = time.time()
            _universe["config"] = _load_universe_config()


def _universe_worker():
    """Background thread that refreshes universe periodically."""
    while True:
        _refresh_universe()
        time.sleep(UNIVERSE_REFRESH_HOURS * 3600)


# ---------------------------------------------------------------------------
# Shared Market Data Layer — Phase 1
# ---------------------------------------------------------------------------
# One Kraken connection serves the whole fleet instead of 7 independent ones.

_market_cache = {}   # {(pair, interval): {"candles": [...], "fetched_at": float}}
_market_lock = threading.Lock()

# interval_minutes -> refresh_seconds
MARKET_INTERVALS = {
    5:    60,     # 5m candles, refresh every 60s  (was 30s — scaled for 50 pairs)
    15:   120,    # 15m candles, refresh every 120s (was 60s)
    60:   180,    # 1h candles, refresh every 3min  (was 2min)
    240:  600,    # 4h candles, refresh every 10min (was 5min)
    1440: 1800,   # 1d candles, refresh every 30min (was 15min)
}


def _fetch_kraken_ohlc(pair, interval):
    """Fetch OHLC candles from Kraken. Returns list of [ts, o, h, l, c, vol, count] or None."""
    try:
        url = f"{KRAKEN_REST}/OHLC"
        resp = requests.get(url, params={"pair": pair, "interval": interval}, timeout=10)
        data = resp.json()
        if data.get("error"):
            return None
        result = data.get("result", {})
        # Find the candle array (key varies by pair)
        for k, v in result.items():
            if k != "last" and isinstance(v, list):
                # Kraken returns: [ts, open, high, low, close, vwap, volume, count]
                # Normalize to: [ts, open, high, low, close, volume, count]
                candles = []
                for row in v:
                    candles.append([
                        int(row[0]),       # timestamp
                        float(row[1]),     # open
                        float(row[2]),     # high
                        float(row[3]),     # low
                        float(row[4]),     # close
                        float(row[6]),     # volume (skip vwap at index 5)
                        int(row[7]),       # count
                    ])
                return candles
        return None
    except Exception:
        log.debug("OHLC fetch failed for %s/%sm", pair, interval, exc_info=True)
        return None


def _market_data_worker():
    """Background thread: fetches OHLC for all universe pairs at all intervals."""
    while True:
        with _universe_lock:
            pairs = [p["display"] for p in _universe.get("pairs", [])]
        if not pairs:
            time.sleep(30)
            continue

        for interval, refresh_sec in MARKET_INTERVALS.items():
            for pair in pairs:
                key = (pair, interval)
                with _market_lock:
                    cached = _market_cache.get(key)
                if cached and (time.time() - cached["fetched_at"]) < refresh_sec:
                    continue  # still fresh

                candles = _fetch_kraken_ohlc(pair, interval)
                if candles:
                    with _market_lock:
                        _market_cache[key] = {
                            "candles": candles,
                            "fetched_at": time.time(),
                        }
                time.sleep(0.3)  # rate limit: ~3 req/sec (50 pairs need more spacing)

        # Sleep before next full cycle
        time.sleep(10)


def _get_market_ohlc(pair, interval, limit=100):
    """Get cached OHLC or fetch on demand. Returns dict or None."""
    key = (pair, interval)
    with _market_lock:
        cached = _market_cache.get(key)

    if cached:
        candles = cached["candles"][-limit:] if limit else cached["candles"]
        return {
            "pair": pair,
            "interval": interval,
            "candles": candles,
            "cached_at": cached["fetched_at"],
            "age_s": round(time.time() - cached["fetched_at"], 1),
        }

    # Not in cache — fetch on demand
    candles = _fetch_kraken_ohlc(pair, interval)
    if candles:
        with _market_lock:
            _market_cache[key] = {"candles": candles, "fetched_at": time.time()}
        return {
            "pair": pair,
            "interval": interval,
            "candles": candles[-limit:] if limit else candles,
            "cached_at": time.time(),
            "age_s": 0,
        }
    return None


def _get_market_ticker():
    """Build ticker from latest candle data for all pairs."""
    ticker = {}
    with _universe_lock:
        pairs = [p["display"] for p in _universe.get("pairs", [])]
    for pair in pairs:
        # Use 5m candles (most frequent) for latest price
        key = (pair, 5)
        with _market_lock:
            cached = _market_cache.get(key)
        if cached and cached["candles"]:
            last = cached["candles"][-1]
            ticker[pair] = {
                "price": last[4],       # close
                "high": last[2],
                "low": last[3],
                "volume": last[5],
                "timestamp": last[0],
                "age_s": round(time.time() - cached["fetched_at"], 1),
            }
    return ticker


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch_json(url: str) -> tuple[dict, float]:
    """GET a URL, return (json_dict, latency_ms) or raise."""
    t0 = time.time()
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    latency = (time.time() - t0) * 1000
    return resp.json(), latency


def _fetch_bot(bot: dict) -> tuple[dict, float]:
    """Fetch data for a single bot. Returns (raw_dict, latency_ms) or raises."""
    base = f"http://127.0.0.1:{bot['port']}"

    if bot["id"] == "trekbot":
        # TrekBot uses three separate endpoints
        merged = {}
        total_latency = 0.0
        for ep in bot["endpoints"]:
            key = ep.strip("/").split("/")[-1]  # "health", "positions", "analytics"
            data, lat = _fetch_json(base + ep)
            merged[key] = data
            total_latency += lat
        return merged, total_latency
    else:
        # Standard bots — single /api/snapshot
        ep = bot["endpoints"][0]
        return _fetch_json(base + ep)


# ---------------------------------------------------------------------------
# Normalization — per-bot extractors
# ---------------------------------------------------------------------------

def _normalize_turtlesue(raw: dict) -> dict:
    trade_stats = raw.get("trade_stats") or {}
    wr_raw = trade_stats.get("win_rate", 0)
    return {
        "equity": raw.get("equity"),
        "pnl": raw.get("pnl"),
        "pnl_pct": raw.get("pnl_pct"),
        "win_rate": wr_raw if wr_raw is not None else None,
        "drawdown_pct": raw.get("drawdown_pct"),
        "sharpe": None,
        "open_positions": len(raw.get("positions") or {}),
        "total_trades": trade_stats.get("total", 0),
        "regime": None,  # system_mode is "S1"/"S2"/"BOTH", not a market regime
        "signals_count": len(raw.get("signals") or []),
        "uptime": None,
    }


def _normalize_sentinel(raw: dict) -> dict:
    fc = raw.get("forecasts", {})
    high_conv = []
    for pair, f in fc.items():
        h4 = f.get("4h", {})
        dp = h4.get("direction_probability", {})
        if dp.get("up", 0) > 0.65 or dp.get("down", 0) > 0.65:
            high_conv.append(pair)
    return {
        "equity": None,
        "pnl": None,
        "pnl_pct": None,
        "win_rate": None,
        "drawdown_pct": None,
        "sharpe": None,
        "open_positions": None,
        "total_trades": None,
        "regime": raw.get("fleet_inputs", {}).get("regime"),
        "signals_count": len(high_conv),
        "uptime": None,
        "pairs_forecast": raw.get("pairs_forecast", 0),
        "high_conviction": high_conv,
    }


def _normalize_trinity(raw: dict) -> dict:
    top_syms = raw.get("top_symbols") or []
    regime = None
    if top_syms and isinstance(top_syms[0], dict):
        regime = top_syms[0].get("regime")
    return {
        "equity": None,
        "pnl": None,
        "pnl_pct": None,
        "win_rate": raw.get("win_rate", 0),
        "drawdown_pct": None,
        "sharpe": None,
        "open_positions": raw.get("opens", 0),
        "total_trades": (raw.get("wins", 0) or 0) + (raw.get("losses", 0) or 0),
        "regime": regime,
        "signals_count": len(top_syms),
        "uptime": raw.get("uptime", 0),
        # extras
        "symbols_streaming": raw.get("symbols_streaming"),
        "symbols_total": raw.get("symbols_total"),
    }


def _normalize_hivemind(raw: dict) -> dict:
    consensus = raw.get("consensus") or {}
    regime_block = raw.get("regime") or {}
    md = consensus.get("max_drawdown", 0) or 0
    return {
        "equity": None,
        "pnl": None,
        "pnl_pct": None,
        "win_rate": None,
        "drawdown_pct": abs(md) * 100,
        "sharpe": consensus.get("sharpe_ratio"),
        "open_positions": None,
        "total_trades": None,
        "regime": regime_block.get("regime"),
        "signals_count": len(raw.get("members") or []),
        "uptime": None,
        # extras
        "consensus_weights": consensus.get("weights") or {},
    }


def _normalize_nexusbrain(raw: dict) -> dict:
    perf = raw.get("performance") or {}
    wr_raw = perf.get("win_rate", 0)
    md = perf.get("max_drawdown_pct", 0) or 0
    return {
        "equity": perf.get("final_equity"),
        "pnl": perf.get("total_pnl"),
        "pnl_pct": perf.get("return_pct"),
        "win_rate": (wr_raw * 100) if wr_raw is not None else None,
        "drawdown_pct": abs(md),
        "sharpe": perf.get("sharpe_ratio"),
        "open_positions": len(raw.get("open_positions") or []),
        "total_trades": perf.get("total_trades", 0),
        "regime": None,
        "signals_count": len(raw.get("recent_signals") or []),
        "uptime": None,
    }


def _normalize_trekbot(raw: dict) -> dict:
    health = raw.get("health") or {}
    positions = raw.get("positions") or []
    analytics = raw.get("analytics") or {}
    return {
        "equity": health.get("paper_balance"),
        "pnl": health.get("circuit_breaker_daily_pnl"),
        "pnl_pct": None,
        "win_rate": analytics.get("win_rate"),
        "drawdown_pct": None,
        "sharpe": None,
        "open_positions": len(positions) if isinstance(positions, list) else 0,
        "total_trades": analytics.get("trades"),
        "regime": None,
        "signals_count": None,
        "uptime": health.get("uptime_seconds"),
    }


def _normalize_oracle(raw: dict) -> dict:
    regime = raw.get("regime")
    top_sigs = raw.get("top_signals") or []
    return {
        "equity": None,           # Oracle doesn't trade
        "pnl": None,
        "pnl_pct": None,
        "win_rate": None,
        "drawdown_pct": None,
        "sharpe": None,
        "open_positions": None,
        "total_trades": None,
        "regime": regime,
        "signals_count": len(top_sigs),
        "uptime": None,
        # Oracle-specific extras
        "pairs_scanned": raw.get("pairs_scanned"),
        "scan_duration_s": raw.get("scan_duration_s"),
        "fear_greed": raw.get("fear_greed"),
        "top_signal": top_sigs[0] if top_sigs else None,
        "next_scan_time": raw.get("next_scan_time"),
    }


def _normalize_deepblue(raw: dict) -> dict:
    whales = raw.get("whales") or []
    pairs = raw.get("pairs") or {}
    # Tier breakdown (all counted from pairs dict for consistency)
    pair_scores = [(p.get("whaleScore") or 0) for p in pairs.values()]
    extreme = sum(1 for s in pair_scores if s >= 80)
    high = sum(1 for s in pair_scores if 65 <= s < 80)
    moderate = sum(1 for s in pair_scores if 50 <= s < 65)
    low = len(pair_scores) - extreme - high - moderate
    whale_count = extreme + high  # actionable whales
    top_whale = None
    if whales:
        top = whales[0]
        top_whale = {"pair": top.get("pair", ""), "score": top.get("whaleScore", 0),
                     "trend": top.get("trend", "")}
    return {
        "equity": None,
        "pnl": None,
        "pnl_pct": None,
        "win_rate": None,
        "drawdown_pct": None,
        "sharpe": None,
        "open_positions": None,
        "total_trades": None,
        "regime": None,
        "signals_count": whale_count,
        "uptime": None,
        # Deep Blue specifics
        "whale_count": whale_count,
        "tier_breakdown": {"extreme": extreme, "high": high, "moderate": moderate, "low": max(low, 0)},
        "top_whale": top_whale,
        "cycle_count": raw.get("cycle", 0),
        "pairs_scanned": raw.get("pairs_scanned", len(pairs)),
    }


def _normalize_gridzilla(raw: dict) -> dict:
    perf = raw.get("performance") or raw
    eq = perf.get("equity") or raw.get("equity")
    pnl = perf.get("pnl") or raw.get("pnl")
    open_pos = raw.get("open_positions") or []
    return {
        "equity": float(eq) if eq else None,
        "pnl": float(pnl) if pnl else None,
        "pnl_pct": float(raw.get("pnl_pct", 0)) if raw.get("pnl_pct") else None,
        "win_rate": float(raw.get("win_rate", 0)) if raw.get("win_rate") else None,
        "drawdown_pct": float(raw.get("drawdown_pct", 0)) if raw.get("drawdown_pct") else None,
        "sharpe": None,
        "open_positions": len(open_pos) if isinstance(open_pos, list) else 0,
        "total_trades": raw.get("total_trades", 0),
        "regime": raw.get("regime"),
        "signals_count": None,
        "uptime": None,
        # Gridzilla specifics
        "grid_levels_active": len(raw.get("grid_levels", {})),
        "pairs_count": raw.get("pairs_count"),
    }


def _normalize_phitex(raw: dict) -> dict:
    return {
        "equity": None,
        "pnl": None,
        "pnl_pct": None,
        "win_rate": None,
        "drawdown_pct": None,
        "sharpe": None,
        "open_positions": None,
        "total_trades": None,
        "regime": raw.get("fleet_regime"),
        "signals_count": len(raw.get("alerts", [])),
        "uptime": None,
        "fleet_score": raw.get("fleet_score"),
        "fleet_direction": raw.get("fleet_direction"),
        "pairs_scanned": len(raw.get("pairs", {})),
    }


def _normalize_aegis(raw: dict) -> dict:
    return {
        "equity": None,
        "pnl": None,
        "pnl_pct": None,
        "win_rate": None,
        "drawdown_pct": None,
        "sharpe": None,
        "open_positions": None,
        "total_trades": None,
        "regime": raw.get("regime"),
        "signals_count": None,
        "uptime": None,
        "aegis_score": raw.get("score"),
        "recommended_deploy": raw.get("recommended_max_deployed"),
        "components": raw.get("components"),
    }


_NORMALIZERS = {
    "turtlesue":  _normalize_turtlesue,
    "sentinel":   _normalize_sentinel,
    "trinity":    _normalize_trinity,
    "hivemind":   _normalize_hivemind,
    "nexusbrain": _normalize_nexusbrain,
    "trekbot":    _normalize_trekbot,
    "oracle":     _normalize_oracle,
    "deepblue":   _normalize_deepblue,
    "gridzilla":  _normalize_gridzilla,
    "phitex":     _normalize_phitex,
    "aegis":      _normalize_aegis,
    "rubberband": lambda raw: {
        "equity": raw.get("equity"), "pnl": raw.get("pnl"), "pnl_pct": None,
        "win_rate": raw.get("win_rate"), "drawdown_pct": None, "sharpe": None,
        "open_positions": raw.get("open_positions", 0),
        "total_trades": raw.get("total_trades", 0),
        "regime": raw.get("regime", "RANGING"),
        "signals_count": None, "uptime": None,
    },
    "contrarian": lambda raw: {
        "equity": None, "pnl": None, "pnl_pct": None,
        "win_rate": None, "drawdown_pct": None, "sharpe": None,
        "open_positions": None, "total_trades": None,
        "regime": raw.get("sentiment_state", raw.get("regime")),
        "signals_count": len(raw.get("recent_alerts", [])),
        "sentiment_score": raw.get("sentiment_score", 50),
        "fear_greed": raw.get("fear_greed", 50),
        "uptime": None,
    },
    "arbitrageur": lambda raw: {
        "equity": raw.get("equity"), "pnl": raw.get("pnl"), "pnl_pct": None,
        "win_rate": raw.get("win_rate"), "drawdown_pct": None, "sharpe": None,
        "open_positions": raw.get("open_spreads", 0),
        "total_trades": raw.get("total_trades", 0),
        "regime": raw.get("regime", "SCANNING"),
        "signals_count": raw.get("tracked_pairs", 0),
        "uptime": None,
    },
    "chronos": lambda raw: {
        "equity": None, "pnl": None, "pnl_pct": None,
        "win_rate": None, "drawdown_pct": None, "sharpe": None,
        "open_positions": None, "total_trades": None,
        "regime": raw.get("regime"),
        "signals_count": len(raw.get("recent_alerts", [])),
        "hour_bias": raw.get("hour_bias", 50),
        "active_sessions": raw.get("active_sessions", []),
        "uptime": None,
    },
    "nexus":      lambda raw: {
        "equity": None, "pnl": None, "pnl_pct": None, "win_rate": None,
        "drawdown_pct": None, "sharpe": None, "open_positions": None,
        "total_trades": None, "signals_count": None, "uptime": None,
        "regime": raw.get("market_character"),
        "market_character": raw.get("market_character"),
        "volatility_forecast": raw.get("volatility_forecast"),
        "lens_dispersion": raw.get("lens_dispersion"),
        "interpretation": raw.get("interpretation"),
    },
}


def _first_available(d1: dict, d2: dict, *keys: str) -> Any:
    """Return the first non-None value found across dicts for any key."""
    for k in keys:
        for d in (d1, d2):
            v = d.get(k)
            if v is not None:
                return v
    return None


# ---------------------------------------------------------------------------
# Aggregate computation
# ---------------------------------------------------------------------------

def _compute_aggregate(bots_data: dict) -> dict:
    """Compute fleet-wide aggregate from all normalized bot data."""
    alive_count = sum(1 for b in bots_data.values() if b.get("alive"))
    norms = {bid: b["normalized"] for bid, b in bots_data.items() if b.get("normalized")}

    def _collect(key):
        return [(bid, n[key]) for bid, n in norms.items() if n.get(key) is not None]

    equities = _collect("equity")
    pnls = _collect("pnl")
    win_rates = _collect("win_rate")
    open_pos = _collect("open_positions")
    trades = _collect("total_trades")
    regimes = [n["regime"] for n in norms.values() if n.get("regime")]

    # Best / worst performer by pnl_pct first, then pnl
    def _perf_score(bid):
        n = norms.get(bid, {})
        pct = n.get("pnl_pct")
        if pct is not None:
            return pct
        p = n.get("pnl")
        if p is not None:
            return p
        return None

    scored = []
    for bid in norms:
        s = _perf_score(bid)
        if s is not None:
            scored.append((bid, s))
    best = max(scored, key=lambda x: x[1])[0] if scored else None
    worst = min(scored, key=lambda x: x[1])[0] if scored else None

    # Regime consensus
    regime_consensus = None
    if regimes:
        counts = Counter(regimes)
        regime_consensus = counts.most_common(1)[0][0]

    return {
        "bots_alive": alive_count,
        "bots_total": len(BOT_REGISTRY),
        "total_equity": sum(v for _, v in equities) if equities else None,
        "total_pnl": sum(v for _, v in pnls) if pnls else None,
        "avg_win_rate": (sum(v for _, v in win_rates) / len(win_rates)) if win_rates else None,
        "total_open_positions": sum(v for _, v in open_pos) if open_pos else None,
        "total_trades": sum(v for _, v in trades) if trades else None,
        "best_performer": best,
        "worst_performer": worst,
        "regime_consensus": regime_consensus,
    }


# ---------------------------------------------------------------------------
# Signal feed aggregation
# ---------------------------------------------------------------------------

_BOT_META: dict[str, tuple[str, str]] = {b["id"]: (b["name"], b["color"]) for b in BOT_REGISTRY}


def _bot_meta(bot_id: str) -> tuple[str, str]:
    return _BOT_META.get(bot_id, (bot_id, "#ffffff"))


def _extract_feed(bots_data: dict) -> list[dict]:
    """Collect and merge signals/alerts from all bots into a unified feed."""
    entries = []

    # TurtleSue signals (cap per-bot to avoid feed flooding)
    ts_raw = (bots_data.get("turtlesue") or {}).get("raw") or {}
    for sig in (ts_raw.get("signals") or [])[-10:]:
        name, color = _bot_meta("turtlesue")
        t = sig.get("time") or sig.get("timestamp") or 0
        msg = f"{sig.get('action', '')} — {sig.get('name', '')}".strip(" —")
        entries.append({"time": t, "bot_id": "turtlesue", "bot_name": name, "message": msg, "bot_color": color})

    # Trinity alerts (timestamp may be a string like "23:34:49" or an epoch)
    tr_raw = (bots_data.get("trinity") or {}).get("raw") or {}
    for alert in (tr_raw.get("alerts") or [])[-10:]:
        name, color = _bot_meta("trinity")
        t = alert.get("timestamp") or 0
        # Trinity sends timestamp as "HH:MM:SS" string -- use current time as approx
        if isinstance(t, str):
            t = time.time()
        sym = alert.get("symbol", "")
        msg_text = alert.get("message", "")
        msg = f"{sym}: {msg_text}".strip(": ")
        if msg:
            entries.append({"time": t, "bot_id": "trinity", "bot_name": name, "message": msg, "bot_color": color})

    # Trinity top_symbols as signals (these are the actual scan results)
    for sig in (tr_raw.get("top_symbols") or [])[:5]:
        name, color = _bot_meta("trinity")
        sym = sig.get("symbol", "")
        conf = sig.get("confluence", 0)
        bias = sig.get("bias", "")
        regime = sig.get("regime", "")
        t = sig.get("timestamp") or time.time()
        if isinstance(t, str):
            t = time.time()
        if bias and bias != "NEUTRAL":
            msg = f"{bias} {sym} conf={conf:.2f} regime={regime}"
            entries.append({"time": t, "bot_id": "trinity", "bot_name": name, "message": msg, "bot_color": color})

    # NexusBrain recent_signals -- format as compact one-liners
    nb_raw = (bots_data.get("nexusbrain") or {}).get("raw") or {}
    for sig in (nb_raw.get("recent_signals") or [])[-10:]:
        name, color = _bot_meta("nexusbrain")
        t = sig.get("time") or sig.get("timestamp") or 0
        if isinstance(sig, dict) and sig.get("pair"):
            pair = sig.get("pair", "")
            conf = sig.get("confluence_score", sig.get("confluence", 0))
            regime = sig.get("regime", "")
            label = sig.get("confidence_label", "")
            conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else str(conf)
            direction = sig.get("direction", "LONG")
            msg = f"{direction} {pair} conf={conf_str} regime={regime}"
            if label:
                msg += f" [{label}]"
        else:
            msg = sig.get("message") or sig.get("signal") or str(sig)[:80]
        entries.append({"time": t, "bot_id": "nexusbrain", "bot_name": name, "message": msg, "bot_color": color})

    # TrekBot recent_signals from analytics
    tb_raw = (bots_data.get("trekbot") or {}).get("raw") or {}
    analytics = tb_raw.get("analytics") or {}
    for sig in (analytics.get("recent_signals") or [])[-10:]:
        name, color = _bot_meta("trekbot")
        t = sig.get("time") or sig.get("timestamp") or 0
        msg = sig.get("message") or sig.get("signal") or sig.get("name") or str(sig)
        entries.append({"time": t, "bot_id": "trekbot", "bot_name": name, "message": msg, "bot_color": color})

    # Rubberband recent trades
    rb_raw = (bots_data.get("rubberband") or {}).get("raw") or {}
    for t in (rb_raw.get("recent_trades") or [])[-10:]:
        name, color = _bot_meta("rubberband")
        pnl = t.get("pnl", 0)
        result = "WIN" if pnl > 0 else "LOSS"
        msg = f"{result} {t.get('direction', '')} {t.get('pair', '')} ${pnl:+.2f} [{t.get('exit_reason', '')}]"
        entries.append({"time": t.get("closed_at", time.time()), "bot_id": "rubberband", "bot_name": name, "message": msg, "bot_color": color})

    # Contrarian sentiment alerts
    ct_raw = (bots_data.get("contrarian") or {}).get("raw") or {}
    for alert in (ct_raw.get("recent_alerts") or [])[-10:]:
        name, color = _bot_meta("contrarian")
        atype = alert.get("type", "")
        suggestion = alert.get("suggestion", "")
        msg = f"{atype}: {suggestion}" if suggestion else atype
        entries.append({"time": alert.get("timestamp", time.time()), "bot_id": "contrarian", "bot_name": name, "message": msg, "bot_color": color})

    # Arbitrageur spread events
    ab_raw = (bots_data.get("arbitrageur") or {}).get("raw") or {}
    for t in (ab_raw.get("recent_trades") or [])[-10:]:
        name, color = _bot_meta("arbitrageur")
        pnl = t.get("pnl", 0)
        result = "WIN" if pnl > 0 else "LOSS"
        msg = f"SPREAD {result} {t.get('pair_key', '')} z={t.get('entry_z', 0):.1f}->{t.get('exit_z', 0):.1f} ${pnl:+.2f} [{t.get('reason', '')}]"
        entries.append({"time": t.get("closed_at", time.time()), "bot_id": "arbitrageur", "bot_name": name, "message": msg, "bot_color": color})

    # Chronos session alerts
    ch_raw = (bots_data.get("chronos") or {}).get("raw") or {}
    for alert in (ch_raw.get("recent_alerts") or [])[-10:]:
        name, color = _bot_meta("chronos")
        atype = alert.get("type", "")
        suggestion = alert.get("suggestion", "")
        msg = f"{atype}: {suggestion}" if suggestion else atype
        entries.append({"time": alert.get("timestamp", time.time()), "bot_id": "chronos", "bot_name": name, "message": msg, "bot_color": color})

    # Oracle top signals
    or_raw = (bots_data.get("oracle") or {}).get("raw") or {}
    for sig in (or_raw.get("top_signals") or [])[:5]:
        name, color = _bot_meta("oracle")
        pair = sig.get("pair", "")
        direction = sig.get("direction", "NEUTRAL")
        strategy = sig.get("strategy", "")
        score = sig.get("score", 0)
        rr = sig.get("rr")
        rr_str = f" R:R={rr:.1f}" if isinstance(rr, (int, float)) and rr else ""
        if direction != "NEUTRAL" and pair:
            msg = f"{direction} {pair} [{strategy}] score={score:.1f}{rr_str}"
            entries.append({"time": or_raw.get("timestamp", time.time()), "bot_id": "oracle", "bot_name": name, "message": msg, "bot_color": color})

    # Sort newest first, cap at MAX_FEED_SIZE
    entries.sort(key=lambda e: e.get("time", 0), reverse=True)
    return entries[:MAX_FEED_SIZE]


# ---------------------------------------------------------------------------
# UPGRADE: Fleet Intelligence — Computation Functions
# ---------------------------------------------------------------------------

# --- A. Fleet-Level Factor Exposure Monitor ---

def _compute_factor_exposure(bots_data: dict) -> dict:
    """Aggregate factor scores across all active positions from bots that report them.

    Reads factor data from TrekBot (analytics.positions), NexusBrain (open_positions),
    and any bot that exposes factor scores in its raw data.

    Returns dict with per-factor aggregation and concentration alerts.
    """
    factor_totals: dict[str, list[float]] = {}  # factor_name -> [scores...]
    positions_with_factors = 0
    total_positions = 0
    per_bot_factors: dict[str, dict] = {}

    for bid, bot in bots_data.items():
        if not bot.get("alive"):
            continue
        raw = bot.get("raw") or {}
        norm = bot.get("normalized") or {}
        bot_factors: dict[str, list[float]] = {}

        # TrekBot: positions list, each may have "factors" dict
        if bid == "trekbot":
            positions = raw.get("positions") or []
            analytics = raw.get("analytics") or {}
            for pos in positions:
                total_positions += 1
                factors = pos.get("factors") or pos.get("detail", {}).get("factors") or {}
                if factors:
                    positions_with_factors += 1
                    for fname, fval in factors.items():
                        if isinstance(fval, (int, float)):
                            factor_totals.setdefault(fname, []).append(fval)
                            bot_factors.setdefault(fname, []).append(fval)
            # Also check analytics-level factor summary
            last_trade_factors = analytics.get("last_trade_factors") or {}
            for fname, fval in last_trade_factors.items():
                if isinstance(fval, (int, float)):
                    factor_totals.setdefault(fname, []).append(fval)
                    bot_factors.setdefault(fname, []).append(fval)

        # NexusBrain: open_positions list, each may have factor scores
        elif bid == "nexusbrain":
            for pos in (raw.get("open_positions") or []):
                total_positions += 1
                factors = pos.get("factors") or pos.get("scores") or {}
                if factors:
                    positions_with_factors += 1
                    for fname, fval in factors.items():
                        if isinstance(fval, (int, float)):
                            factor_totals.setdefault(fname, []).append(fval)
                            bot_factors.setdefault(fname, []).append(fval)

        # Generic: any bot with "factors" at top level (e.g., from signals)
        else:
            top_factors = raw.get("factors") or {}
            if isinstance(top_factors, dict):
                for fname, fval in top_factors.items():
                    if isinstance(fval, (int, float)):
                        factor_totals.setdefault(fname, []).append(fval)
                        bot_factors.setdefault(fname, []).append(fval)

        if bot_factors:
            per_bot_factors[bid] = {
                fname: round(sum(vals) / len(vals), 4)
                for fname, vals in bot_factors.items()
            }

    # Compute per-factor stats
    factor_summary = {}
    concentration_alerts = []
    for fname, scores in factor_totals.items():
        if not scores:
            continue
        avg = sum(scores) / len(scores)
        mn = min(scores)
        mx = max(scores)
        factor_summary[fname] = {
            "avg": round(avg, 4),
            "min": round(mn, 4),
            "max": round(mx, 4),
            "count": len(scores),
        }
        # Concentration check: if 80%+ of factor scores are > 0.7 (strong loading)
        high_loading = sum(1 for s in scores if abs(s) > 0.7)
        if len(scores) >= 2 and high_loading / len(scores) >= 0.8:
            concentration_alerts.append({
                "factor": fname,
                "avg_score": round(avg, 4),
                "high_loading_pct": round(high_loading / len(scores) * 100, 1),
                "severity": "HIGH" if abs(avg) > 0.85 else "MEDIUM",
                "message": f"Factor '{fname}' concentration: {high_loading}/{len(scores)} positions loaded >0.7 (avg={avg:.2f})",
            })

    result = {
        "timestamp": time.time(),
        "total_positions": total_positions,
        "positions_with_factors": positions_with_factors,
        "factors": factor_summary,
        "per_bot": per_bot_factors,
        "concentration_alerts": concentration_alerts,
        "concentration_risk": len(concentration_alerts) > 0,
    }

    with _fleet_exposure_lock:
        global _fleet_exposure
        _fleet_exposure = result

    return result


# --- B. Cross-Bot Correlation Tracker ---

def _compute_bot_correlations(bots_data: dict) -> dict:
    """Compute rolling correlations between bot PnL streams.

    Maintains a rolling window of per-bot PnL values (sampled each poll cycle).
    Once at least 30 samples exist for a pair of bots, computes Pearson correlation.
    Flags pairs with correlation > 0.85 as concentration risk.

    Returns correlation matrix dict and list of alerts.
    """
    MAX_HISTORY = 200  # keep last 200 poll samples (~13 minutes at 4s polling)
    MIN_SAMPLES = 30   # minimum samples for meaningful correlation

    # Update PnL history from current bot data
    with _bot_correlation_lock:
        for bid, bot in bots_data.items():
            if not bot.get("alive"):
                continue
            norm = bot.get("normalized") or {}
            pnl = norm.get("pnl")
            if pnl is not None:
                if bid not in _bot_pnl_history:
                    _bot_pnl_history[bid] = []
                _bot_pnl_history[bid].append(float(pnl))
                # Cap history size
                if len(_bot_pnl_history[bid]) > MAX_HISTORY:
                    _bot_pnl_history[bid] = _bot_pnl_history[bid][-MAX_HISTORY:]

        # Compute correlation matrix
        bots_with_data = {
            bid: vals for bid, vals in _bot_pnl_history.items()
            if len(vals) >= MIN_SAMPLES
        }

    if len(bots_with_data) < 2:
        result = {
            "timestamp": time.time(),
            "status": "insufficient_data",
            "bots_tracked": len(_bot_pnl_history),
            "min_samples_required": MIN_SAMPLES,
            "samples_collected": {bid: len(vals) for bid, vals in _bot_pnl_history.items()},
            "matrix": {},
            "alerts": [],
        }
        with _bot_correlation_lock:
            global _bot_correlation_matrix, _bot_correlation_alerts
            _bot_correlation_matrix = result
            _bot_correlation_alerts = []
        return result

    # Compute pairwise Pearson correlation using PnL deltas (changes, not levels)
    matrix = {}
    alerts = []
    bot_ids = sorted(bots_with_data.keys())

    for i in range(len(bot_ids)):
        for j in range(i + 1, len(bot_ids)):
            bid_a, bid_b = bot_ids[i], bot_ids[j]
            vals_a = bots_with_data[bid_a]
            vals_b = bots_with_data[bid_b]

            # Use only overlapping tail
            min_len = min(len(vals_a), len(vals_b))
            a = vals_a[-min_len:]
            b = vals_b[-min_len:]

            # Compute deltas (PnL changes between consecutive samples)
            if min_len < MIN_SAMPLES + 1:
                continue
            da = [a[k] - a[k - 1] for k in range(1, len(a))]
            db = [b[k] - b[k - 1] for k in range(1, len(b))]

            if not da or not db:
                continue

            # Pearson correlation on deltas
            mean_a = sum(da) / len(da)
            mean_b = sum(db) / len(db)
            num = sum((da[k] - mean_a) * (db[k] - mean_b) for k in range(len(da)))
            denom_a = math.sqrt(sum((x - mean_a) ** 2 for x in da)) + 1e-10
            denom_b = math.sqrt(sum((x - mean_b) ** 2 for x in db)) + 1e-10
            corr = num / (denom_a * denom_b)
            corr = max(-1.0, min(1.0, corr))  # clamp to [-1, 1]

            pair_key = f"{bid_a}|{bid_b}"
            matrix[pair_key] = round(corr, 4)

            # Alert if correlation > 0.85
            if abs(corr) > 0.85:
                alerts.append({
                    "bots": [bid_a, bid_b],
                    "correlation": round(corr, 4),
                    "samples": min_len,
                    "severity": "HIGH" if abs(corr) > 0.95 else "MEDIUM",
                    "message": f"{bid_a} and {bid_b} are {corr:.2f} correlated — concentration risk",
                })

    result = {
        "timestamp": time.time(),
        "status": "active",
        "bots_tracked": len(_bot_pnl_history),
        "bots_correlated": len(bot_ids),
        "samples_collected": {bid: len(vals) for bid, vals in _bot_pnl_history.items()},
        "matrix": matrix,
        "alerts": alerts,
        "concentration_risk": len(alerts) > 0,
    }

    with _bot_correlation_lock:
        _bot_correlation_matrix = result
        _bot_correlation_alerts = alerts

    return result


# --- C. Automated Performance Attribution ---

def _compute_performance_attribution(bots_data: dict) -> dict:
    """Decompose fleet PnL into Alpha (signal quality), Beta (market exposure),
    and Cost (estimated fees/slippage).

    Alpha = PnL from position selection (excess return above market)
    Beta  = PnL attributable to market direction (what a passive holder would earn)
    Cost  = Estimated trading costs (fees + slippage estimate)

    Uses the latest market data and bot positions to estimate these components.
    """
    # Collect all trading bot PnL data
    bot_pnls = {}
    fleet_total_pnl = 0.0
    fleet_total_trades = 0
    fleet_open_positions = 0

    for bid, bot in bots_data.items():
        if not bot.get("alive"):
            continue
        norm = bot.get("normalized") or {}
        pnl = norm.get("pnl")
        if pnl is not None:
            bot_pnls[bid] = float(pnl)
            fleet_total_pnl += float(pnl)

        total_trades = norm.get("total_trades")
        if total_trades is not None:
            fleet_total_trades += int(total_trades)

        open_pos = norm.get("open_positions")
        if open_pos is not None:
            fleet_open_positions += int(open_pos)

    # Estimate Beta (market direction component)
    # Use BTC/USD price change as market proxy from cached OHLC data
    market_return_pct = 0.0
    with _market_lock:
        btc_cache = _market_cache.get(("BTC/USD", 1440))  # daily candles
        if btc_cache and btc_cache["candles"]:
            candles = btc_cache["candles"]
            if len(candles) >= 2:
                prev_close = candles[-2][4]
                curr_close = candles[-1][4]
                if prev_close > 0:
                    market_return_pct = (curr_close - prev_close) / prev_close * 100

    # Estimate deployed capital from portfolio manager
    deployed_capital = 0.0
    if _portfolio_mgr:
        with _portfolio_mgr._lock:
            deployed_capital = _portfolio_mgr.deployed()

    # Beta = market_return_pct * deployed_capital / 100
    # This approximates what passive market exposure would have returned
    beta_pnl = market_return_pct * deployed_capital / 100.0 if deployed_capital > 0 else 0.0

    # Cost estimate: assume 0.26% round-trip fee (Kraken taker) per trade
    # plus 0.05% slippage estimate
    FEE_RATE = 0.0026
    SLIPPAGE_RATE = 0.0005
    # Estimate average trade size from portfolio
    avg_trade_size = deployed_capital / max(fleet_open_positions, 1) if deployed_capital > 0 else 0
    # Only count new trades (approximation: use total_trades as proxy)
    # For a running system, this gives cumulative cost
    estimated_cost = fleet_total_trades * avg_trade_size * (FEE_RATE + SLIPPAGE_RATE)
    # Cap at reasonable fraction of total PnL magnitude to avoid absurd estimates
    if abs(fleet_total_pnl) > 0:
        estimated_cost = min(estimated_cost, abs(fleet_total_pnl) * 0.5)

    # Alpha = total PnL - beta - (-cost)  =>  Alpha = total PnL - beta + cost
    alpha_pnl = fleet_total_pnl - beta_pnl + estimated_cost

    # Per-bot attribution (simplified: proportional to their share of total PnL)
    per_bot = {}
    for bid, pnl in bot_pnls.items():
        share = pnl / fleet_total_pnl if fleet_total_pnl != 0 else 0
        per_bot[bid] = {
            "total_pnl": round(pnl, 2),
            "alpha_share": round(alpha_pnl * share, 2) if alpha_pnl else 0,
            "beta_share": round(beta_pnl * share, 2) if beta_pnl else 0,
            "cost_share": round(estimated_cost * share, 2) if estimated_cost else 0,
        }

    # Edge decay indicator: if alpha is trending toward zero or negative,
    # edge may be decaying even if total PnL is still positive from beta
    edge_status = "HEALTHY"
    if alpha_pnl < 0:
        edge_status = "DECAYING"
    elif alpha_pnl < abs(beta_pnl) * 0.1 and beta_pnl > 0:
        edge_status = "MARGINAL"

    result = {
        "timestamp": time.time(),
        "fleet_pnl": round(fleet_total_pnl, 2),
        "alpha": round(alpha_pnl, 2),
        "beta": round(beta_pnl, 2),
        "cost": round(estimated_cost, 2),
        "market_return_pct": round(market_return_pct, 4),
        "deployed_capital": round(deployed_capital, 2),
        "total_trades": fleet_total_trades,
        "open_positions": fleet_open_positions,
        "edge_status": edge_status,
        "per_bot": per_bot,
        "explanation": {
            "alpha": "PnL from position selection skill (signal quality)",
            "beta": "PnL from market direction exposure (passive component)",
            "cost": "Estimated trading costs (fees + slippage)",
            "edge_status": f"Alpha {'exceeds' if alpha_pnl > 0 else 'trails'} passive exposure — edge is {edge_status.lower()}",
        },
    }

    with _perf_attribution_lock:
        global _perf_attribution
        _perf_attribution = result

    return result


# --- D. LLM Fleet Briefing Worker ---

def _generate_briefing() -> dict:
    """Generate a natural-language fleet briefing via the inference server.

    Feeds aggregate stats, AEGIS score, positions, recent events, correlation
    alerts, and exposure concentration to the LLM for a 3-sentence sitrep.
    """
    try:
        # Gather all intelligence
        with _lock:
            bots_data = dict(_state["bots"])
            aggregate = dict(_state["aggregate"])
            feed = list(_state["feed"][:10])

        portfolio_state = _portfolio_mgr.state() if _portfolio_mgr else {}

        with _fleet_exposure_lock:
            exposure = dict(_fleet_exposure) if _fleet_exposure else {}

        with _bot_correlation_lock:
            corr_alerts = list(_bot_correlation_alerts)

        with _perf_attribution_lock:
            attribution = dict(_perf_attribution) if _perf_attribution else {}

        # Build context for the LLM
        alive_bots = []
        for bid, bot in bots_data.items():
            if bot.get("alive"):
                n = bot.get("normalized") or {}
                alive_bots.append({
                    "name": bot.get("name", bid),
                    "alive": True,
                    "normalized": {
                        "equity": n.get("equity"),
                        "pnl": n.get("pnl"),
                        "win_rate": n.get("win_rate"),
                        "open_positions": n.get("open_positions"),
                        "regime": n.get("regime"),
                    },
                })

        # AEGIS data
        aegis_bot = bots_data.get("aegis", {})
        aegis_norm = (aegis_bot.get("normalized") or {}) if aegis_bot.get("alive") else {}
        aegis_score = aegis_norm.get("aegis_score", "N/A")
        aegis_regime = aegis_norm.get("regime", "N/A")

        fleet_state = {
            "bots": alive_bots,
            "aggregate": aggregate,
            "portfolio": {
                "total": portfolio_state.get("total", 0),
                "deployed": portfolio_state.get("deployed", 0),
                "available": portfolio_state.get("available", 0),
                "deployed_pct": portfolio_state.get("deployed_pct", 0),
                "active_reservations": portfolio_state.get("active_reservations", 0),
            },
            "aegis_score": aegis_score,
            "aegis_regime": aegis_regime,
            "phitex_score": (bots_data.get("phitex", {}).get("normalized") or {}).get("fleet_score", "N/A"),
            "whale_count": (bots_data.get("deepblue", {}).get("normalized") or {}).get("whale_count", 0),
            "recent_events": [{"bot": e.get("bot_name", ""), "message": e.get("message", "")} for e in feed[:5]],
            "correlation_alerts": [a.get("message", "") for a in corr_alerts[:3]],
            "exposure_concentration": [a.get("message", "") for a in exposure.get("concentration_alerts", [])[:3]],
            "attribution": {
                "alpha": attribution.get("alpha", 0),
                "beta": attribution.get("beta", 0),
                "edge_status": attribution.get("edge_status", "UNKNOWN"),
            },
        }

        # POST to inference server
        payload = json.dumps({"fleet_state": fleet_state}).encode("utf-8")
        req = _urlreq.Request(
            f"{INFERENCE_URL}/api/ai/fleet-assessment",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = _urlreq.urlopen(req, timeout=60)
        ai_response = json.loads(resp.read().decode("utf-8"))

        # Extract the assessment text (may be in "assessment" field from LLM JSON)
        briefing_text = ai_response.get("assessment", "")
        if not briefing_text:
            briefing_text = ai_response.get("text", json.dumps(ai_response))

        return {
            "text": briefing_text,
            "risk_factors": ai_response.get("risk_factors", []),
            "opportunities": ai_response.get("opportunities", []),
            "recommended_action": ai_response.get("recommended_action", ""),
            "generated_at": time.time(),
            "status": "ok",
            "source": "inference_server",
        }

    except Exception as e:
        # Degrade gracefully — generate a simple rule-based briefing instead
        try:
            with _lock:
                agg = dict(_state.get("aggregate", {}))
            alive = agg.get("bots_alive", 0)
            total = agg.get("bots_total", 16)
            consensus = agg.get("regime_consensus", "UNKNOWN")
            total_pnl = agg.get("total_pnl")
            pnl_str = f"${total_pnl:+.2f}" if total_pnl is not None else "N/A"

            with _perf_attribution_lock:
                edge = _perf_attribution.get("edge_status", "UNKNOWN") if _perf_attribution else "UNKNOWN"

            alert_count = len(_bot_correlation_alerts)
            fallback_text = (
                f"Fleet: {alive}/{total} bots online, regime consensus {consensus}, fleet PnL {pnl_str}. "
                f"Edge status: {edge}. "
                f"{'No correlation alerts.' if alert_count == 0 else f'{alert_count} correlation alert(s) active — review diversification.'}"
            )
            return {
                "text": fallback_text,
                "risk_factors": [],
                "opportunities": [],
                "recommended_action": "",
                "generated_at": time.time(),
                "status": "fallback",
                "source": "rule_based",
                "error": str(e),
            }
        except Exception:
            return {
                "text": "Briefing generation failed.",
                "generated_at": time.time(),
                "status": "error",
                "source": "none",
                "error": str(e),
            }


def _briefing_worker():
    """Background thread: generates a new LLM fleet briefing every 5 minutes."""
    global _fleet_briefing
    # Wait for initial data to accumulate
    time.sleep(30)
    while True:
        briefing = _generate_briefing()
        with _fleet_briefing_lock:
            _fleet_briefing = briefing
        time.sleep(BRIEFING_INTERVAL)


# ---------------------------------------------------------------------------
# Polling loop (background thread)
# ---------------------------------------------------------------------------

def _poll_all_bots() -> None:
    """Single poll cycle -- fetch all bots, normalize, aggregate."""
    new_bots = {}

    for bot in BOT_REGISTRY:
        bid = bot["id"]
        with _lock:
            prev = _state["bots"].get(bid, {})
        try:
            raw, latency = _fetch_bot(bot)
            raw = _sanitize(raw)
            normalizer = _NORMALIZERS.get(bid)
            normalized = normalizer(raw) if normalizer else {}
            new_bots[bid] = {
                "id": bid, "name": bot["name"], "port": bot["port"],
                "color": bot["color"], "alive": True,
                "last_seen": time.time(), "latency_ms": round(latency, 1),
                "raw": raw, "normalized": normalized,
            }
        except Exception:
            new_bots[bid] = {
                "id": bid, "name": bot["name"], "port": bot["port"],
                "color": bot["color"], "alive": False,
                "last_seen": prev.get("last_seen"), "latency_ms": None,
                "raw": prev.get("raw"), "normalized": prev.get("normalized"),
            }

    aggregate = _compute_aggregate(new_bots)
    feed = _extract_feed(new_bots)

    with _lock:
        _state["bots"] = new_bots
        _state["aggregate"] = aggregate
        _state["feed"] = feed

    # Submit Oracle signals as proposals to the signal aggregator
    try:
        or_data = new_bots.get("oracle") or {}
        if or_data.get("alive"):
            for sig in ((or_data.get("raw") or {}).get("top_signals") or [])[:5]:
                direction = sig.get("direction", "NEUTRAL")
                if direction in ("LONG", "SHORT"):
                    _signal_aggregator.submit_proposal(
                        source="oracle",
                        pair=sig.get("pair", ""),
                        direction=direction,
                        confidence=min(1.0, (sig.get("score", 50) / 100)),
                        metadata={"strategy": sig.get("strategy", "")},
                    )
                    _signal_decomposition.log_signal(
                        "oracle", sig.get("pair", ""), direction,
                        min(1.0, (sig.get("score", 50) / 100)),
                        metadata={"strategy": sig.get("strategy", "")},
                    )
    except Exception:
        pass

    # UPGRADE: Fleet Intelligence — update computed metrics each poll cycle
    try:
        _compute_factor_exposure(new_bots)
    except Exception:
        log.debug("Factor exposure computation failed", exc_info=True)
    try:
        _compute_bot_correlations(new_bots)
    except Exception:
        log.debug("Bot correlation computation failed", exc_info=True)
    try:
        _compute_performance_attribution(new_bots)
    except Exception:
        log.debug("Performance attribution computation failed", exc_info=True)


_last_aegis_adjust = 0
_AEGIS_COOLDOWN = 300  # 5 minutes between adjustments


def _apply_aegis_adjustment():
    """Read AEGIS score and dynamically adjust portfolio deployment limits."""
    global _last_aegis_adjust
    now = time.time()
    if now - _last_aegis_adjust < _AEGIS_COOLDOWN:
        return
    if not _portfolio_mgr:
        return

    with _lock:
        aegis_data = _state.get("bots", {}).get("aegis")
    if not aegis_data or not aegis_data.get("alive"):
        return

    raw = aegis_data.get("raw") or {}
    norm = aegis_data.get("normalized") or {}
    # AEGIS raw uses "score"; normalizer copies it to "aegis_score".
    # Check both dicts × both keys so older/newer AEGIS versions both work.
    score = _first_available(raw, norm, "score", "aegis_score")
    if score is None:
        return

    if score >= 0.8:
        new_limit = 90
        regime = "DEPLOY"
    elif score >= 0.5:
        new_limit = 80
        regime = "NORMAL"
    elif score >= 0.2:
        new_limit = 60
        regime = "CAUTIOUS"
    else:
        new_limit = 30
        regime = "DEFENSIVE"

    with _portfolio_mgr._lock:
        old_limit = _portfolio_mgr.limits.get("max_deployed_pct", 80)
        if old_limit != new_limit:
            _portfolio_mgr.limits["max_deployed_pct"] = new_limit

    if old_limit != new_limit:
        _event_bus.publish({
            "source": "command_center",
            "type": "PORTFOLIO_LIMIT_CHANGE",
            "data": {
                "aegis_score": round(score, 4),
                "regime": regime,
                "old_limit": old_limit,
                "new_limit": new_limit,
            },
        })

    _last_aegis_adjust = now


def _poll_loop():
    """Continuously fetch, normalize, aggregate, and store state."""
    while True:
        _poll_all_bots()
        try:
            _apply_aegis_adjustment()
        except Exception:
            log.warning("AEGIS adjustment failed", exc_info=True)
        # Poll fleet intel score from Nexus every other cycle
        try:
            _fleet_intel.poll()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Fleet Logger state helpers (called by FleetLogger from its thread)
# ---------------------------------------------------------------------------

def _get_logger_state():
    """Return current bot state for the fleet logger."""
    with _lock:
        return {
            "bots": dict(_state["bots"]),
            "aggregate": dict(_state["aggregate"]),
            "feed": list(_state["feed"]),
        }


def _get_logger_portfolio():
    """Return current portfolio state for the fleet logger."""
    if _portfolio_mgr:
        return _portfolio_mgr.state()
    return None


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class CommandCenterHandler(BaseHTTPRequestHandler):

    # -- Exact-match GET route table ------------------------------------------
    _GET_ROUTES: dict[str, str] = {
        "/":                        "_serve_index",
        "/v2":                      "_serve_v2",
        "/v3":                      "_serve_index",
        "/api/master":              "_serve_master",
        "/api/universe":            "_serve_universe",
        "/api/portfolio":           "_serve_portfolio",
        "/api/portfolio/available": "_serve_portfolio_available",
        "/api/portfolio/exposure":  "_serve_portfolio_exposure",
        "/api/fleet/daily":         "_serve_fleet_daily",
        "/api/market/ohlc":         "_serve_market_ohlc",
        "/api/market/ohlc/bulk":    "_serve_market_ohlc_bulk",
        "/api/market/ticker":       "_serve_market_ticker",
        "/api/events/stream":       "_serve_event_stream",
        "/api/events/recent":       "_serve_events_recent",
        "/api/events/stats":        "_serve_events_stats",
        "/api/signals/decide":      "_serve_signals_decide",
        "/api/signals/rankings":    "_serve_signals_rankings",
        "/api/signals/decomposition": "_serve_signals_decomposition",
        "/api/expectancy":          "_serve_expectancy",
        "/api/signals/decay":       "_serve_signals_decay",
        "/api/manifest":            "_serve_manifest",
        # UPGRADE: Fleet Intelligence endpoints
        "/api/fleet/exposure":      "_serve_fleet_exposure",
        "/api/fleet/correlations":  "_serve_fleet_correlations",
        "/api/fleet/attribution":   "_serve_fleet_attribution",
        "/api/fleet/briefing":      "_serve_fleet_briefing",
        "/api/fleet/intel_score":   "_serve_fleet_intel_score",
    }

    # -- Prefix-match GET routes (checked after exact miss) -------------------
    _GET_PREFIX_ROUTES: list[tuple[str, str]] = [
        ("/api/bot/",         "_serve_bot_prefix"),
        ("/api/expectancy/",  "_serve_expectancy_prefix"),
    ]

    # -- Exact-match POST route table -----------------------------------------
    _POST_ROUTES: dict[str, str] = {
        "/api/portfolio/reserve":  "_handle_reserve",
        "/api/portfolio/release":  "_handle_release",
        "/api/events/publish":     "_handle_event_publish",
        "/api/signals/propose":    "_handle_signals_propose",
        "/api/signals/outcome":    "_handle_signals_outcome",
        "/api/expectancy/record":  "_handle_expectancy_record",
    }

    def log_message(self, fmt, *args):
        """Suppress default stderr logging."""
        pass

    # -- Response helpers -----------------------------------------------------

    def _set_json_headers(self, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _set_html_headers(self, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()

    def _send_json(self, obj: Any, code: int = 200) -> None:
        self._set_json_headers(code)
        self.wfile.write(_safe_json(obj).encode("utf-8"))

    # -- Routing dispatch -----------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # Exact match
        method_name = self._GET_ROUTES.get(path)
        if method_name:
            getattr(self, method_name)(parsed)
            return

        # Prefix match
        for prefix, method_name in self._GET_PREFIX_ROUTES:
            if path.startswith(prefix):
                getattr(self, method_name)(parsed, path)
                return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        method_name = self._POST_ROUTES.get(path)
        if method_name:
            getattr(self, method_name)(data)
        else:
            self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        """CORS preflight for POST requests."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # -- GET route handler methods --------------------------------------------
    # All GET handlers accept (self, parsed) for uniformity with the route table.
    # Prefix-match handlers accept (self, parsed, path).

    def _serve_index(self, parsed) -> None:
        self._serve_html("command_center_v4.html")

    def _serve_v2(self, parsed) -> None:
        self._serve_html("command_center.html")

    def _serve_html(self, filename: str = "command_center_v3.html") -> None:
        """Serve dashboard HTML from same directory as this script."""
        here = os.path.dirname(os.path.abspath(__file__))
        html_path = os.path.join(here, filename)
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                content = f.read()
            self._set_html_headers(200)
            self.wfile.write(content.encode("utf-8"))
        except FileNotFoundError:
            self._set_html_headers(200)
            placeholder = (
                "<!DOCTYPE html><html><head><title>Command Center</title></head>"
                "<body style='background:#111;color:#ccc;font-family:monospace;padding:40px'>"
                "<h1>COMMAND CENTER</h1>"
                "<p>command_center.html not found. Place it next to command_center.py.</p>"
                "<p>API available at <a href='/api/master' style='color:#00e676'>/api/master</a></p>"
                "</body></html>"
            )
            self.wfile.write(placeholder.encode("utf-8"))

    def _serve_master(self, parsed) -> None:
        """Return full aggregated state."""
        with _lock:
            bots_list = []
            for bot in BOT_REGISTRY:
                entry = _state["bots"].get(bot["id"])
                if entry:
                    bots_list.append({
                        "id": entry["id"],
                        "name": entry["name"],
                        "port": entry["port"],
                        "color": entry["color"],
                        "alive": entry["alive"],
                        "last_seen": entry["last_seen"],
                        "latency_ms": entry["latency_ms"],
                        "normalized": entry.get("normalized"),
                    })
                else:
                    bots_list.append({
                        "id": bot["id"],
                        "name": bot["name"],
                        "port": bot["port"],
                        "color": bot["color"],
                        "alive": False,
                        "last_seen": None,
                        "latency_ms": None,
                        "normalized": None,
                    })

            payload = {
                "timestamp": time.time(),
                "bots": bots_list,
                "aggregate": _state["aggregate"],
                "feed": _state["feed"],
                "portfolio": _portfolio_mgr.state() if _portfolio_mgr else None,
                "daily_24h": _fleet_logger.get_daily_accumulator() if _fleet_logger else None,
            }

        self._send_json(payload)

    def _serve_bot_prefix(self, parsed, path: str) -> None:
        """Return raw snapshot for a single bot (prefix route)."""
        bot_id = path.split("/api/bot/", 1)[1].strip("/")
        with _lock:
            entry = _state["bots"].get(bot_id)
        if not entry:
            self._send_json({"error": f"Bot '{bot_id}' not found"}, 404)
            return
        self._send_json({
            "id": entry["id"],
            "name": entry["name"],
            "alive": entry["alive"],
            "last_seen": entry["last_seen"],
            "latency_ms": entry["latency_ms"],
            "raw": entry.get("raw"),
            "normalized": entry.get("normalized"),
        })

    def _serve_universe(self, parsed) -> None:
        """Return the shared fleet universe."""
        with _universe_lock:
            pairs = _universe["pairs"]
            last_refresh = _universe["last_refresh"]
            config = _universe["config"]
        self._send_json({
            "timestamp": time.time(),
            "last_refresh": last_refresh,
            "count": len(pairs),
            "config": config,
            "pairs": pairs,
            "symbols": [p["display"] for p in pairs],
            "kraken_pairs": [p["kraken_pair"] for p in pairs],
        })

    def _serve_portfolio(self, parsed) -> None:
        if not _portfolio_mgr:
            self._send_json({"error": "Portfolio manager not initialized"}, 503)
            return
        self._send_json(_portfolio_mgr.state())

    def _serve_portfolio_available(self, parsed) -> None:
        if not _portfolio_mgr:
            self._send_json({"error": "Portfolio manager not initialized"}, 503)
            return
        self._send_json(_portfolio_mgr.available_snapshot())

    def _serve_portfolio_exposure(self, parsed) -> None:
        if not _portfolio_mgr:
            self._send_json({"error": "Portfolio manager not initialized"}, 503)
            return
        self._send_json(_portfolio_mgr.exposure_snapshot())

    def _serve_fleet_daily(self, parsed) -> None:
        if not _fleet_logger:
            self._send_json({"error": "Fleet logger not initialized"}, 503)
            return
        self._send_json(_fleet_logger.get_daily_accumulator())

    def _serve_market_ohlc(self, parsed) -> None:
        qs = parse_qs(parsed.query)
        pair = qs.get("pair", [None])[0]
        interval = int(qs.get("interval", [60])[0])
        limit = int(qs.get("limit", [100])[0])
        if not pair:
            self._send_json({"error": "pair parameter required"}, 400)
            return
        result = _get_market_ohlc(pair, interval, limit)
        if result:
            self._send_json(result)
        else:
            self._send_json({"error": f"No data for {pair} at {interval}m"}, 404)

    def _serve_market_ohlc_bulk(self, parsed) -> None:
        qs = parse_qs(parsed.query)
        pairs_str = qs.get("pairs", [""])[0]
        interval = int(qs.get("interval", [60])[0])
        limit = int(qs.get("limit", [100])[0])
        if not pairs_str:
            self._send_json({"error": "pairs parameter required"}, 400)
            return
        pairs = [p.strip() for p in pairs_str.split(",") if p.strip()]
        results = {}
        for pair in pairs:
            data = _get_market_ohlc(pair, interval, limit)
            if data:
                results[pair] = data
        self._send_json(results)

    def _serve_market_ticker(self, parsed) -> None:
        self._send_json(_get_market_ticker())

    def _serve_event_stream(self, parsed) -> None:
        """GET /api/events/stream — SSE stream."""
        qs = parse_qs(parsed.query)
        types = qs.get("types", [None])[0]
        filter_types = types.split(",") if types else None

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        sub = _event_bus.subscribe(filter_types)
        try:
            for chunk in _event_bus.sse_generator(sub):
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            _event_bus.unsubscribe(sub)

    def _serve_events_recent(self, parsed) -> None:
        qs = parse_qs(parsed.query)
        n = int(qs.get("n", [50])[0])
        event_type = qs.get("type", [None])[0]
        self._send_json(_event_bus.get_recent(n, event_type))

    def _serve_events_stats(self, parsed) -> None:
        self._send_json(_event_bus.stats())

    def _serve_signals_decide(self, parsed) -> None:
        qs = parse_qs(parsed.query)
        pair = qs.get("pair", ["BTC/USD"])[0]
        self._send_json(_signal_aggregator.decide(pair))

    def _serve_signals_rankings(self, parsed) -> None:
        self._send_json(_signal_aggregator.get_source_rankings())

    def _serve_signals_decomposition(self, parsed) -> None:
        self._send_json(_signal_decomposition.compute_signal_value())

    def _serve_expectancy(self, parsed) -> None:
        self._send_json(_expectancy_tracker.get_fleet_stats())

    def _serve_expectancy_prefix(self, parsed, path: str) -> None:
        bot_id = path.split("/api/expectancy/", 1)[1].strip("/")
        self._send_json(_expectancy_tracker.get_bot_stats(bot_id))

    def _serve_signals_decay(self, parsed) -> None:
        self._send_json(_signal_decay.get_all_half_lives())

    def _serve_manifest(self, parsed) -> None:
        with _lock:
            bot_list = list(_state.get("bots", {}).values())

        # Detect Nexus-hosted engines via event bus activity (last 5 min)
        _ENGINE_EVENTS = {
            "info_geometry": "MANIFOLD_WARNING",
            "topology": "CYCLE_DETECTED",
            "quantum_state": "QUANTUM_COLLAPSE",
            "causal_flow": "CAUSAL_FLOW",
            "shannon": "SHANNON_ENTROPY",
            "boltzmann": "BOOK_PHASE",
            "lorenz": "CHAOS_STATE",
            "thom": "CATASTROPHE_WARNING",
            "prigogine": "STRUCTURE_FORMING",
        }
        bus_recent = _event_bus.get_recent(500)
        now = time.time()
        recent_types = set()
        for ev in bus_recent:
            if now - ev.get("ts", 0) < 300:  # last 5 minutes
                recent_types.add(ev.get("type"))

        # Also check FleetIntelScore — engines that compute but don't hit event
        # thresholds still appear as contributing_engines in the intel scores
        intel_engines = set()
        try:
            for pair_score in _fleet_intel.get_all_scores().values():
                for eng in pair_score.get("contributing_engines", []):
                    intel_engines.add(eng)
        except Exception:
            pass

        nexus_modules = {}
        for engine, evt_type in _ENGINE_EVENTS.items():
            nexus_modules[engine] = (evt_type in recent_types) or (engine in intel_engines)

        self._send_json({
            "version": "2.0",
            "uptime_seconds": round(time.time() - _start_time),
            "bots_alive": sum(1 for b in bot_list if b.get("alive")),
            "bots_total": len(BOT_REGISTRY),
            "modules": {
                "signal_aggregator": True,
                "signal_decomposition": True,
                "expectancy_tracker": True,
                "signal_decay": True,
                "fleet_exposure": True,
                "fleet_correlations": True,
                "fleet_attribution": True,
                "fleet_briefing": True,
                **nexus_modules,
            },
            "event_bus": _event_bus.stats(),
            "expectancy": _expectancy_tracker.get_fleet_stats(),
        })

    # -- UPGRADE: Fleet Intelligence GET handlers ------------------------------

    def _serve_fleet_exposure(self, parsed) -> None:
        """GET /api/fleet/exposure — aggregated factor exposure across all positions."""
        with _fleet_exposure_lock:
            data = dict(_fleet_exposure) if _fleet_exposure else {}
        if not data:
            self._send_json({
                "status": "no_data",
                "message": "Factor exposure data not yet computed. Wait for next poll cycle.",
            })
            return
        self._send_json(data)

    def _serve_fleet_correlations(self, parsed) -> None:
        """GET /api/fleet/correlations — cross-bot PnL correlation matrix and alerts."""
        with _bot_correlation_lock:
            data = dict(_bot_correlation_matrix) if _bot_correlation_matrix else {}
        if not data:
            self._send_json({
                "status": "no_data",
                "message": "Correlation data not yet computed. Need at least 30 poll cycles (~2 minutes).",
            })
            return
        self._send_json(data)

    def _serve_fleet_attribution(self, parsed) -> None:
        """GET /api/fleet/attribution — performance attribution (alpha/beta/cost decomposition)."""
        with _perf_attribution_lock:
            data = dict(_perf_attribution) if _perf_attribution else {}
        if not data:
            self._send_json({
                "status": "no_data",
                "message": "Attribution data not yet computed. Wait for next poll cycle.",
            })
            return
        self._send_json(data)

    def _serve_fleet_briefing(self, parsed) -> None:
        """GET /api/fleet/briefing — latest LLM-generated fleet situation report."""
        with _fleet_briefing_lock:
            data = dict(_fleet_briefing)
        self._send_json(data)

    def _serve_fleet_intel_score(self, parsed) -> None:
        """GET /api/fleet/intel_score — synthesized engine intelligence per pair."""
        qs = parse_qs(parsed.query)
        pair = qs.get("pair", [None])[0]
        if pair:
            self._send_json(_fleet_intel.get_score(pair))
        else:
            self._send_json({
                "scores": _fleet_intel.get_all_scores(),
                "summary": _fleet_intel.summary(),
            })

    # -- POST route handler methods -------------------------------------------

    def _handle_event_publish(self, data: dict) -> None:
        if not data.get("type"):
            self._send_json({"error": "type field required"}, 400)
            return
        _event_bus.publish(data)
        # Feed signal attribution from bot-published TRADE events
        etype = data.get("type")
        edata = data.get("data", {})
        source = data.get("source", "")
        pair = edata.get("pair", "")
        if etype == "TRADE_OPEN" and edata.get("signals"):
            _open_trade_signals[f"{source}:{pair}"] = {
                "signals": edata["signals"],
                "factors": edata.get("factors", {}),
            }
            # Also submit each signal to the aggregator
            for sig in edata["signals"]:
                _signal_aggregator.submit_proposal(
                    source=f"{source}:{sig}",
                    pair=pair,
                    direction=edata.get("direction", "LONG").upper(),
                    confidence=edata.get("confidence", 0.5),
                )
                _signal_decomposition.log_signal(
                    source=f"{source}:{sig}",
                    pair=pair,
                    direction=edata.get("direction", "LONG").upper(),
                    confidence=edata.get("confidence", 0.5),
                )
        elif etype == "TRADE_CLOSE" and pair:
            key = f"{source}:{pair}"
            open_info = _open_trade_signals.pop(key, None)
            pnl = edata.get("pnl", 0)
            direction = edata.get("direction", "LONG").upper()
            won = pnl > 0
            _signal_aggregator.record_outcome(pair, direction, won, pnl)
            if open_info and open_info.get("signals"):
                sigs = open_info["signals"]
                contrib = [f"{source}:{s}" for s in sigs] if isinstance(sigs, list) else [source]
                _signal_decomposition.log_trade(
                    pair, direction, gross_pnl=pnl, fees=0,
                    duration=edata.get("duration_h", 0) * 3600 if edata.get("duration_h") else edata.get("duration_s", 0),
                    contributing_signals=contrib,
                )
        self._send_json({"ok": True})

    def _handle_reserve(self, data: dict) -> None:
        """POST /api/portfolio/reserve — bot requests capital."""
        if not _portfolio_mgr:
            self._send_json({"error": "Portfolio manager not initialized"}, 503)
            return

        missing = [f for f in ("bot_id", "pair", "direction", "amount") if f not in data]
        if missing:
            self._send_json({"error": f"Missing fields: {', '.join(missing)}"}, 400)
            return

        try:
            amount = float(data["amount"])
        except (ValueError, TypeError):
            self._send_json({"error": "Invalid amount"}, 400)
            return

        result = _portfolio_mgr.reserve(
            bot_id=data["bot_id"],
            pair=data["pair"],
            direction=data["direction"],
            amount=amount,
            stop_loss_pct=data.get("stop_loss_pct"),
        )

        if _fleet_logger:
            if result["ok"]:
                _fleet_logger.log_portfolio_reserve(
                    data["bot_id"], data["pair"], data["direction"],
                    amount, result.get("reservation_id", ""))
            else:
                _fleet_logger.log_portfolio_denial(data["bot_id"], result.get("reason", ""))

        if result["ok"]:
            _event_bus.publish({
                "source": "portfolio", "type": "PORTFOLIO_RESERVE",
                "data": {"bot_id": data["bot_id"], "pair": data["pair"],
                         "direction": data["direction"], "amount": amount,
                         "reservation_id": result.get("reservation_id", "")},
            })
        else:
            _event_bus.publish({
                "source": "portfolio", "type": "PORTFOLIO_DENIAL",
                "data": {"bot_id": data["bot_id"], "pair": data.get("pair", ""),
                         "amount": amount, "reason": result.get("reason", "")},
            })

        self._send_json(result, 200 if result["ok"] else 403)

    def _handle_release(self, data: dict) -> None:
        """POST /api/portfolio/release — bot returns capital."""
        if not _portfolio_mgr:
            self._send_json({"error": "Portfolio manager not initialized"}, 503)
            return

        rid = data.get("reservation_id")
        if not rid:
            self._send_json({"error": "Missing reservation_id"}, 400)
            return

        pnl = float(data.get("pnl", 0))
        result = _portfolio_mgr.release(rid, pnl=pnl)

        if _fleet_logger and result["ok"]:
            res_info = result["reservation"]
            _fleet_logger.log_portfolio_release(
                res_info["bot_id"], res_info["pair"],
                res_info["amount"], pnl, rid)

        # Don't leak internal reservation data to the API caller
        result.pop("reservation", None)
        self._send_json(result, 200 if result["ok"] else 404)

    def _handle_signals_propose(self, data: dict) -> None:
        _signal_aggregator.submit_proposal(
            source=data.get("source", "unknown"),
            pair=data.get("pair", ""),
            direction=data.get("direction", "NEUTRAL"),
            confidence=data.get("confidence", 0.5),
            metadata=data.get("metadata", {}),
        )
        self._send_json({"status": "accepted"})

    def _handle_signals_outcome(self, data: dict) -> None:
        _signal_aggregator.record_outcome(
            pair=data.get("pair", ""),
            direction=data.get("direction", ""),
            won=data.get("won", False),
            pnl=data.get("pnl", 0),
        )
        self._send_json({"status": "recorded"})

    def _handle_expectancy_record(self, data: dict) -> None:
        _expectancy_tracker.record_trade(
            bot_id=data.get("bot_id", "unknown"),
            pair=data.get("pair", ""),
            direction=data.get("direction", "LONG"),
            entry_price=data.get("entry_price", 0),
            exit_price=data.get("exit_price", 0),
            size_usd=data.get("size_usd", 0),
            duration=data.get("duration", 0),
            fee_rate=data.get("fee_rate", 0.0026),
        )
        self._send_json({"status": "recorded"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

BANNER = """
  +==========================================+
  |        COMMAND CENTER v2.0               |
  |        Fleet Status: Scanning...         |
  +==========================================+
  |  Dashboard:  http://localhost:9000       |
  |  Portfolio:  http://localhost:9000/api/portfolio
  |  Polling:    4s interval                 |
  +==========================================+"""


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _health_monitor() -> None:
    """Background thread: detect crashed bots, auto-restart, publish events."""
    fails: dict[str, int] = {b["id"]: 0 for b in BOT_REGISTRY}
    fleet_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_config.json")
    bot_cmds: dict[str, dict] = {}
    try:
        with open(fleet_cfg_path) as f:
            fc = json.load(f)
        for bid, bcfg in fc.get("bots", {}).items():
            bot_cmds[bid] = bcfg
    except Exception:
        log.warning("Could not load fleet_config.json for health monitor", exc_info=True)

    while True:
        for bot in BOT_REGISTRY:
            bid = bot["id"]
            try:
                resp = requests.get(f"http://127.0.0.1:{bot['port']}/health", timeout=3)
                if resp.status_code == 200:
                    if fails[bid] >= 3:
                        _event_bus.publish({"source": "health_monitor", "type": "BOT_RECOVERED",
                                            "data": {"bot": bid, "port": bot["port"]}})
                    fails[bid] = 0
                else:
                    fails[bid] += 1
            except Exception:
                fails[bid] += 1

            if fails[bid] == 3:
                _event_bus.publish({"source": "health_monitor", "type": "BOT_DOWN",
                                    "data": {"bot": bid, "port": bot["port"],
                                             "consecutive_fails": 3}})
                # Auto-restart if we have the command
                bcfg = bot_cmds.get(bid)
                if bcfg and bcfg.get("dir"):
                    try:
                        cmd_val = bcfg.get("cmd", ["sentinel.py"])
                        if isinstance(cmd_val, str):
                            cmd_val = [cmd_val]
                        script = os.path.basename(cmd_val[-1])
                        subprocess.Popen(
                            ["python", script],
                            cwd=bcfg["dir"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
                        )
                    except Exception:
                        log.error("Auto-restart failed for %s", bid, exc_info=True)
        time.sleep(30)


def main():
    global _portfolio_mgr, _fleet_logger

    print(BANNER)
    print(f"  Fleet: {len(BOT_REGISTRY)} bots registered")
    for bot in BOT_REGISTRY:
        print(f"    {bot['name']:12s}  :{bot['port']}  {bot['endpoints']}")
    print()

    # Initialize portfolio manager
    _portfolio_mgr = PortfolioManager(PORTFOLIO_TOTAL, PORTFOLIO_LIMITS, PORTFOLIO_FILE)
    stale = _portfolio_mgr.force_release_stale(max_age_hours=24)
    if stale:
        print(f"  Portfolio: released {stale} stale reservation(s)")
    print(f"  Portfolio: ${_portfolio_mgr.available():,.2f} available of ${_portfolio_mgr.total:,.2f}")
    print(f"  Portfolio API: http://localhost:9000/api/portfolio")
    print()

    print(f"  Polling every {POLL_INTERVAL}s, timeout {REQUEST_TIMEOUT}s per request")
    print(f"  Dashboard:  http://localhost:9000/")
    print(f"  Master API: http://localhost:9000/api/master")
    print()

    # Universe + initial scan — all non-blocking so HTTP server starts FAST
    # The coordinator should never depend on subordinates or external APIs to boot
    print("  Universe + initial scan: starting in background...")
    uni_thread = threading.Thread(target=_universe_worker, daemon=True)
    uni_thread.start()
    threading.Thread(target=_poll_all_bots, daemon=True, name="InitialScan").start()

    # Start polling thread
    poller = threading.Thread(target=_poll_loop, daemon=True)
    poller.start()

    # Start market data worker (shared OHLC cache for fleet)
    market_thread = threading.Thread(target=_market_data_worker, daemon=True, name="MarketData")
    market_thread.start()

    # Load event bus reaction rules
    _reactions_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reactions.json")
    try:
        with open(_reactions_path, "r") as f:
            _reactions_cfg = json.load(f)
        _event_bus.load_reactions(_reactions_cfg.get("rules", []))
        print(f"  Event Bus:  {len(_reactions_cfg.get('rules', []))} reaction rules loaded")
    except FileNotFoundError:
        print(f"  Event Bus:  no reactions.json found (running without reactions)")
    except Exception as e:
        print(f"  Event Bus:  failed to load reactions: {e}")
    print(f"  Market Data: caching {len(MARKET_INTERVALS)} intervals (universe loading in background)")

    # Health monitor
    threading.Thread(target=_health_monitor, daemon=True, name="HealthMonitor").start()
    print(f"  Health:     monitoring {len(BOT_REGISTRY)} bots every 30s (auto-restart on 3 fails)")

    # Bridge fleet logger events to event bus + measurement infrastructure
    _open_trade_signals = {}  # track signals at open for attribution at close

    def _on_logger_event(event):
        _event_bus.publish(event)
        data = event.get("data", {})
        etype = event.get("type")
        bot = data.get("bot", event.get("source", ""))
        pair = data.get("pair", "")
        if etype == "TRADE_OPEN" and data.get("signals"):
            _open_trade_signals[f"{bot}:{pair}"] = {
                "signals": data["signals"],
                "factors": data.get("factors", {}),
            }
        elif etype == "TRADE_CLOSE" and pair:
            key = f"{bot}:{pair}"
            open_info = _open_trade_signals.pop(key, None)
            pnl = data.get("pnl", 0)
            direction = data.get("direction", "LONG")
            won = pnl > 0
            _signal_aggregator.record_outcome(pair, direction, won, pnl)
            if open_info and open_info.get("signals"):
                sigs = open_info["signals"]
                contrib = [f"{bot}:{s}" for s in sigs] if isinstance(sigs, list) else [bot]
                _signal_decomposition.log_trade(
                    pair, direction, gross_pnl=pnl, fees=0,
                    duration=data.get("duration_s", 0),
                    contributing_signals=contrib,
                    metadata=open_info.get("factors", {}),
                )

    # Start fleet logger (snapshots every 60s, event detection, daily summaries)
    _fleet_logger = FleetLogger(_get_logger_state, _get_logger_portfolio,
                                on_event=_on_logger_event)
    _fleet_logger.start()
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    print(f"  Fleet Logger: writing to {_log_dir}")
    print(f"    Snapshots: every 60s  |  Events: real-time  |  Daily: midnight UTC")
    print(f"    Analyze:   python analyze.py --help")
    print()

    # Start Brainiac data collector
    start_collector()
    register_brainiac_endpoints(CommandCenterHandler)
    print(f"  Brainiac:   collecting depth, trades, correlations, funding, metrics")
    print(f"    API:      http://localhost:9000/api/brainiac/{{depth|trades|correlations|funding|metrics}}")

    # UPGRADE: Fleet Intelligence — start LLM briefing worker
    threading.Thread(target=_briefing_worker, daemon=True, name="FleetBriefing").start()
    print(f"  Intelligence: factor exposure, bot correlations, performance attribution")
    print(f"    Briefing: LLM sitrep every {BRIEFING_INTERVAL}s (inference @ {INFERENCE_URL})")
    print(f"    API:      /api/fleet/{{exposure|correlations|attribution|briefing}}")

    # Start HTTP server — threaded so SSE streams don't block other requests
    server = ThreadedHTTPServer(("0.0.0.0", 9000), CommandCenterHandler)
    print(f"  Dashboard:  http://localhost:9000")
    print(f"  Universe:   http://localhost:9000/api/universe")
    print(f"  Portfolio:  http://localhost:9000/api/portfolio")
    print(f"  Market:     http://localhost:9000/api/market/ohlc")
    print(f"  Events:     http://localhost:9000/api/events/stream (SSE)")
    print(f"  Daily API:  http://localhost:9000/api/fleet/daily")
    print(f"  Press Ctrl+C to stop")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Command Center stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
