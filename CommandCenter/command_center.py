#!/usr/bin/env python3
"""
COMMAND CENTER — Fleet Aggregator & Dashboard Server
=====================================================
Polls all registered trading bots, normalizes their metrics, and serves
a unified dashboard on port 9000.

Usage: python command_center.py
"""

from __future__ import annotations

import datetime
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

from fleet_config import (
    BOTS, CC_PORT, CC_URL, INFERENCE_URL as _FC_INFERENCE_URL,
    POLL_INTERVAL as _FC_POLL_INTERVAL, REQUEST_TIMEOUT as _FC_REQUEST_TIMEOUT,
    UNIVERSE_REFRESH_HOURS as _FC_UNIVERSE_REFRESH_HOURS,
    KRAKEN_REST as _FC_KRAKEN_REST, PORTFOLIO_TOTAL as _FC_PORTFOLIO_TOTAL,
    PORTFOLIO_LIMITS as _FC_PORTFOLIO_LIMITS, TRADING_BOTS as _FC_TRADING_BOTS,
    PORTFOLIO_FILE as _FC_PORTFOLIO_FILE, WATCHDOG_COOLDOWN as _FC_WATCHDOG_COOLDOWN,
    WATCHDOG_INTERVAL as _FC_WATCHDOG_INTERVAL, WATCHDOG_FAILURES as _FC_WATCHDOG_FAILURES,
    bot_registry_list,
)
import fleet_config as _fleet_config

# ---------------------------------------------------------------------------
# Bot Registry — sourced from fleet_config.py (single source of truth)
# ---------------------------------------------------------------------------

BOT_REGISTRY = bot_registry_list()

POLL_INTERVAL = _FC_POLL_INTERVAL
REQUEST_TIMEOUT = _FC_REQUEST_TIMEOUT
MAX_FEED_SIZE = 50
UNIVERSE_REFRESH_HOURS = _FC_UNIVERSE_REFRESH_HOURS
KRAKEN_REST = _FC_KRAKEN_REST

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

# Event bus — real-time pub/sub for fleet communication.
# Ring buffer sized for ~3h of headroom at current fleet emission rate
# (~600 physics-engine events/h from Nexus + trade events). The EOD card
# falls back to disk-backed /api/trades if TRADE_CLOSE rotates out, but a
# larger buffer keeps dashboard + SSE consumers from seeing empty recent
# history during quiet trading periods.
_event_bus = EventBus(max_history=5000)

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
INFERENCE_URL = _FC_INFERENCE_URL

# E. Open-trade signal attribution linkage.
# When a bot publishes TRADE_OPEN with a list of contributing signals, we stash
# {signals, factors} keyed by f"{source}:{pair}". On TRADE_CLOSE we pop it and
# feed the signal attribution pipeline. This dict MUST survive restarts — if CC
# restarts while a bot has an open position, the close event would otherwise
# arrive with no signal context, and that trade becomes invisible to decomposition.
_open_trade_signals: dict = {}
_open_trade_signals_lock = threading.Lock()
_OPEN_TRADE_SIGNALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "logs", "open_trade_signals.json")

# F. Small-state persistence (AEGIS cooldown, health-monitor restart cooldowns,
# and anything else that needs to survive reboot but isn't worth its own file).
# Shared cc_state.json with atomic write.
_CC_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "logs", "cc_state.json")
_cc_state_lock = threading.Lock()


def _load_cc_state() -> dict:
    """Load the small-state dict from disk. Returns {} on any failure."""
    try:
        if os.path.exists(_CC_STATE_PATH):
            with open(_CC_STATE_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_cc_state(updates: dict) -> None:
    """Merge updates into cc_state.json atomically.

    Callers pass only the keys they want to update; everything else is preserved.
    This keeps multiple independent small-state consumers (AEGIS, health monitor,
    future additions) from stomping each other's keys.
    """
    try:
        os.makedirs(os.path.dirname(_CC_STATE_PATH), exist_ok=True)
        with _cc_state_lock:
            current = _load_cc_state()
            current.update(updates)
            tmp = _CC_STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(current, fh, default=str)
            os.replace(tmp, _CC_STATE_PATH)
    except Exception:
        pass


def _save_open_trade_signals() -> None:
    """Atomic write of the open-trade signal linkage map."""
    try:
        os.makedirs(os.path.dirname(_OPEN_TRADE_SIGNALS_PATH), exist_ok=True)
        tmp = _OPEN_TRADE_SIGNALS_PATH + ".tmp"
        with _open_trade_signals_lock:
            snap = dict(_open_trade_signals)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snap, fh)
        os.replace(tmp, _OPEN_TRADE_SIGNALS_PATH)
    except Exception:
        pass  # Disk errors must not break live event routing.


def _load_open_trade_signals() -> None:
    """Rehydrate the open-trade signal linkage map on startup."""
    global _open_trade_signals
    try:
        if os.path.exists(_OPEN_TRADE_SIGNALS_PATH):
            with open(_OPEN_TRADE_SIGNALS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                with _open_trade_signals_lock:
                    _open_trade_signals = data
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Portfolio Manager — Central Capital Pool
# ---------------------------------------------------------------------------

PORTFOLIO_TOTAL = _FC_PORTFOLIO_TOTAL
PORTFOLIO_LIMITS = _FC_PORTFOLIO_LIMITS
TRADING_BOTS = _FC_TRADING_BOTS
PORTFOLIO_FILE = _FC_PORTFOLIO_FILE


class PortfolioManager:
    """Central capital pool shared by all trading bots."""

    PAIR_COOLDOWN_SECS = 600        # 10 min base cooldown after any trade closes
    PAIR_COOLDOWN_AGGRESSIVE = 300  # 5 min when AEGIS score > 0.7 (fast market)
    PAIR_OPEN_COOLDOWN_SECS = 90    # 90 s cooldown after any OPEN on a pair (prevents burst re-entry before first close)
    COOLDOWN_EXEMPT_BOTS = {"gridzilla"}  # grid bots manage their own frequency via fee gate

    def __init__(self, total: float, limits: dict[str, int], filepath: str, mode_tag: str = "paper"):
        self._lock = threading.Lock()
        self.total = total
        self.limits = limits
        self.filepath = filepath
        self.mode_tag = mode_tag  # "paper" or "live"
        self.reservations: dict[str, dict] = {}
        self.history: list[dict] = []
        self._pair_cooldowns: dict[str, dict] = {}  # pair -> {ts, bot_id} — stamped on CLOSE
        self._pair_opens: dict[str, dict] = {}     # pair -> {ts, bot_id} — stamped on OPEN
        self.aegis_score: float = 0.0  # updated by _apply_aegis_adjustment each cycle
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
        from fleet_config import is_blacklisted
        pair = normalize_pair(pair) or pair  # canonical format for per-pair limits
        with self._lock:
            # 0a. Per-pair OPEN-time cooldown — blocks burst re-entry on the same pair before
            #     any trade has closed (the CLOSE cooldown can't fire if nothing closed yet).
            #     Catches e.g. 4×ENJ/USD opens in 60 s — entry 1 allowed, entries 2-4 blocked.
            #     Gridzilla exempt — grid bots legitimately open multiple levels on the same pair.
            # FEE_SLAYER: added OPEN-gate cooldown — prevents burst same-pair entries before first close
            if bot_id.lower() not in self.COOLDOWN_EXEMPT_BOTS:
                op = self._pair_opens.get(pair)
                if op:
                    elapsed = time.time() - op["ts"]
                    remaining = self.PAIR_OPEN_COOLDOWN_SECS - elapsed
                    if remaining > 0:
                        log.info(
                            "OPEN_COOLDOWN DENIED: %s requested %s, %.0fs remaining after %s's open",
                            bot_id, pair, remaining, op["bot_id"],
                        )
                        return {
                            "ok": False,
                            "reason": f"pair_open_cooldown:{remaining:.0f}s (last open: {op['bot_id']} on {pair})",
                        }

            # 0b. Per-pair CLOSE-time cooldown — first check, before all capital arithmetic and
            #     blacklist lookups (no point running any of that for a pair still cooling down).
            #     Gridzilla exempt — fee gate handles its frequency.
            #     Duration adapts to AEGIS: 5 min when aggressive, 10 min otherwise.
            if bot_id.lower() not in self.COOLDOWN_EXEMPT_BOTS:
                cooldown_secs = (
                    self.PAIR_COOLDOWN_AGGRESSIVE
                    if self.aegis_score > 0.7
                    else self.PAIR_COOLDOWN_SECS
                )
                cd = self._pair_cooldowns.get(pair)
                if cd:
                    elapsed = time.time() - cd["ts"]
                    remaining = cooldown_secs - elapsed
                    if remaining > 0:
                        log.info(
                            "COOLDOWN DENIED: %s requested %s, %.0fs remaining after %s's close",
                            bot_id, pair, remaining, cd["bot_id"],
                        )
                        return {
                            "ok": False,
                            "reason": f"COOLDOWN: {pair} has {remaining:.0f}s remaining (last close: {cd['bot_id']})",
                        }

            # Prune cooldown entries older than 1 hour to prevent memory growth
            cutoff = time.time() - 3600
            self._pair_cooldowns = {
                p: v for p, v in self._pair_cooldowns.items() if v["ts"] > cutoff
            }

            # Blacklist check — fleet-wide protection
            if is_blacklisted(pair):
                return {"ok": False, "reason": f"Pair {pair} is blacklisted (0% WR across fleet)"}

            # Live direction gate — LONG only in live mode
            if not _fleet_config.live_direction_allowed(direction):
                return {"ok": False, "reason": f"LIVE_LONG_ONLY: SHORT positions blocked in live mode"}

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

            # 5. Per-trade limit (epsilon prevents float equality rejection at exact boundary)
            max_trade = self.total * lim["max_per_trade_pct"] / 100
            if amount > max_trade + 0.01:
                return {"ok": False, "reason": f"Trade limit: {amount:.2f} > {max_trade:.2f} max ({lim['max_per_trade_pct']}%)"}

            # 6. Concentration check — no single pair > 40% of total pool
            # (comparing against pool, not deployed, so small deployed totals don't
            # block new positions — e.g. $500 reserve when $12 deployed is fine)
            pair_concentration_of_pool = (pair_exp + amount) / self.total
            if pair_concentration_of_pool > 0.40:
                return {"ok": False, "reason": f"Concentration limit: {pair} would be {pair_concentration_of_pool:.0%} of pool (max 40%)"}

            # 7. Hard directional cap — no single direction may exceed 60% of pool
            # Absolute rule regardless of what's on the other side. Prevents fleet herding.
            # 60% of $9,948 = ~$5,969 max LONG or SHORT across all bots simultaneously.
            dir_totals = self.exposure_by_direction()
            dir_after = dir_totals.get(direction, 0) + amount
            if dir_after > self.total * 0.60:
                return {"ok": False, "reason": f"Directional cap: {direction} would be ${dir_after:.0f} ({dir_after/self.total:.0%} of pool, max 60%)"}

            # 8. Size floor — minimum 5% of pool to keep fees proportional
            min_trade = self.total * 0.05
            if amount < min_trade:
                return {"ok": False, "reason": f"Size floor: ${amount:.2f} < 5% of pool (${min_trade:.2f})"}

            # 8b. Time-of-day gate — deny reservations during historically unprofitable hours
            # Data shows hours 1, 9, 11 UTC have sub-15% win rates
            LOSING_HOURS_UTC = {1, 9, 11}
            current_hour_utc = datetime.datetime.utcnow().hour
            if current_hour_utc in LOSING_HOURS_UTC:
                return {"ok": False, "reason": f"Time gate: hour {current_hour_utc} UTC historically unprofitable (sub-15% WR)"}

            # 9. Fleet intelligence gate — check engine risk assessment
            try:
                intel = _fleet_intel.get_score(pair)
                risk_mult = intel.get("risk_multiplier", 1.0)
                warnings = intel.get("active_warnings", [])
                if risk_mult < 0.10:
                    return {"ok": False, "reason": f"Fleet intel BLOCK: risk={risk_mult:.2f} < 0.10, warnings={warnings[:2]}"}
                if risk_mult < 1.0:
                    # Scale position down proportionally to risk
                    original = amount
                    amount = round(amount * risk_mult, 2)
                    if amount < min_trade:
                        return {"ok": False, "reason": f"Fleet intel scaled ${original:.2f} -> ${amount:.2f} (risk={risk_mult:.2f}), below size floor"}
                    log.info("Intel scaled %s %s: $%.2f -> $%.2f (risk=%.2f)", bot_id, pair, original, amount, risk_mult)
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

            # Stamp open-time cooldown so subsequent same-pair requests within 90 s are blocked.
            # Not tied to close — expires naturally via timestamp comparison in the gate above.
            # Gridzilla is exempt from the check, so skip the stamp too — a Gridzilla open
            # should not inadvertently block other bots from entering the same pair.
            if bot_id.lower() not in self.COOLDOWN_EXEMPT_BOTS:
                self._pair_opens[pair] = {"ts": time.time(), "bot_id": bot_id}

            self._save()
            return {"ok": True, "reservation_id": rid, "amount": amount, "available": self.available()}

    def release(self, reservation_id: str, pnl: float = 0.0) -> dict:
        """Release a reservation and apply PnL to the pool."""
        with self._lock:
            res = self.reservations.pop(reservation_id, None)
            if not res:
                return {"ok": False, "reason": f"Reservation '{reservation_id}' not found"}

            # Calculate and log fee impact (Kraken taker 0.40% × 2 = 0.80% round-trip, tier 0)
            amount = res["amount"]
            KRAKEN_TAKER = 0.0040
            round_trip_fees = amount * KRAKEN_TAKER * 2
            fee_pct = (round_trip_fees / amount * 100) if amount > 0 else 0
            
            # Log significant fee events (>2% of position is significant)
            if fee_pct > 2:
                log.warning(f"Fee alert: {res['bot_id']} {res['pair']} "
                           f"fees ${round_trip_fees:.2f} ({fee_pct:.1f}%) on ${amount:.0f}")

            self.total += pnl

            # Stamp pair cooldown — blocks re-entry until cooldown expires
            self._pair_cooldowns[res["pair"]] = {"ts": time.time(), "bot_id": res["bot_id"]}

            self.history.append({
                "action": "release",
                "reservation_id": reservation_id,
                "bot_id": res["bot_id"],
                "pair": res["pair"],
                "amount": amount,
                "pnl": pnl,
                "fees": round_trip_fees,
                "new_total": self.total,
                "timestamp": time.time(),
            })

            self._save()
            return {"ok": True, "released": res["amount"], "pnl": pnl, "new_total": self.total,
                    "available": self.available(), "reservation": res}

    def force_release_stale(self, max_age_hours: int = 24) -> list:
        """Release reservations older than max_age_hours.
        Returns list of released reservation dicts (each has bot_id, pair, amount, direction, reserved_at).
        Callers should record these to the expectancy tracker so P&L attribution is not lost.
        """
        with self._lock:
            cutoff = time.time() - max_age_hours * 3600
            stale = [rid for rid, r in self.reservations.items() if r["reserved_at"] < cutoff]
            released = []
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
                released.append({"reservation_id": rid, **res})
            if stale:
                self._save()
            return released

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

            reservations_list = [
                dict(reservation_id=rid, **r)
                for rid, r in self.reservations.items()
            ]

            return {
                "mode": self.mode_tag,
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
                "reservations": reservations_list,
                "history_recent": self.history[-20:],
                "pair_cooldowns": {
                    pair: {
                        "remaining_secs": round(self.PAIR_COOLDOWN_SECS - (time.time() - v["ts"])),
                        "last_bot": v["bot_id"],
                    }
                    for pair, v in self._pair_cooldowns.items()
                    if time.time() - v["ts"] < self.PAIR_COOLDOWN_SECS
                },
            }


# Module-level instances (initialized in main())
_portfolio_paper = None
_portfolio_live = None
_fleet_logger = None


def _active_portfolio():
    """Return the portfolio that the dashboard should display (based on engage state)."""
    if _fleet_config.FLEET_ENGAGE_STATE == "paper":
        return _portfolio_paper
    return _portfolio_live  # live_armed or live_engaged → show live


def _alt_portfolio():
    """Return the OTHER portfolio (for the compact status display)."""
    if _fleet_config.FLEET_ENGAGE_STATE == "paper":
        return _portfolio_live
    return _portfolio_paper


def _get_portfolio_for_bot(bot_id: str):
    """Route a bot to the correct portfolio based on fleet state."""
    state = _fleet_config.FLEET_ENGAGE_STATE
    if state == "paper":
        return _portfolio_paper
    elif state == "live_armed":
        # All bots blocked from trading — live not engaged yet
        return None
    elif state == "live_engaged":
        return _portfolio_live
    return _portfolio_paper




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
    # Gridzilla v2 schema: total_pnl, net_pnl, total_fees, active_grids, n_active_grids,
    # pair_analysis, expectancy, trade_history, scan_count, uptime_hours
    exp = raw.get("expectancy") or {}
    active_grids = raw.get("active_grids") or {}
    n_grids = raw.get("n_active_grids", len(active_grids))
    net_pnl = raw.get("net_pnl")
    pnl = net_pnl if net_pnl is not None else raw.get("total_pnl")
    wr = exp.get("win_rate")
    total_trades = exp.get("total_trades") or raw.get("total_cycles", 0)
    return {
        "equity": None,
        "pnl": float(pnl) if pnl is not None else None,
        "pnl_pct": None,
        "win_rate": float(wr) if wr is not None else None,
        "drawdown_pct": None,
        "sharpe": None,
        "open_positions": n_grids,
        "total_trades": total_trades,
        "regime": raw.get("regime"),
        "signals_count": None,
        "uptime": raw.get("uptime_hours"),
        # Gridzilla v2 specifics
        "grid_levels_active": n_grids,
        "pairs_count": len(raw.get("pair_analysis") or {}),
        "total_fees": raw.get("total_fees", 0),
        "scan_count": raw.get("scan_count", 0),
        "aegis_score": raw.get("aegis_score"),
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
        "equity": raw.get("equity"), "pnl": raw.get("pnl"),
        "pnl_pct": raw.get("pnl_pct"),
        "win_rate": raw.get("win_rate"), "drawdown_pct": None, "sharpe": None,
        "open_positions": raw.get("open_spreads", 0),
        "total_trades": raw.get("total_trades", 0),
        "regime": raw.get("regime", "SCANNING"),
        "signals_count": raw.get("tracked_pairs", 0),
        "uptime": raw.get("uptime_s"),
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

    # Global fleet WR = total_wins / total_trades across bots that have traded.
    # Reconstruct wins from (win_rate, total_trades) per bot and sum. Bots with
    # zero trades contribute 0 wins AND 0 to the denominator, so they drop out
    # cleanly. This is the only aggregation that means what "fleet WR" says.
    _total_wins = 0
    _total_trades_for_wr = 0
    for n in norms.values():
        wr = n.get("win_rate")
        tc = n.get("total_trades") or 0
        if wr is None or tc <= 0:
            continue
        # Normalize scale: TurtleSue/Gridzilla raw passthrough may be 0-1,
        # NexusBrain is already scaled to 0-100. Anything > 1 is assumed %.
        wr_pct = wr if wr > 1 else wr * 100
        _total_wins += round(wr_pct / 100.0 * tc)
        _total_trades_for_wr += tc
    global_wr = (_total_wins / _total_trades_for_wr * 100.0) if _total_trades_for_wr > 0 else None

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
        "bots_total": len(bots_data),  # polled bots only, not all registered
        "total_equity": sum(v for _, v in equities) if equities else None,
        "total_pnl": sum(v for _, v in pnls) if pnls else None,
        "avg_win_rate": global_wr,  # Global WR = total_wins / total_trades (not an unweighted mean)
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
    if _active_portfolio():
        with _active_portfolio()._lock:
            deployed_capital = _active_portfolio().deployed()

    # Beta = market_return_pct * deployed_capital / 100
    # This approximates what passive market exposure would have returned
    beta_pnl = market_return_pct * deployed_capital / 100.0 if deployed_capital > 0 else 0.0

    # Cost estimate: 0.40% per side Kraken taker fee (tier 0) per trade
    # plus 0.05% slippage estimate
    FEE_RATE = 0.0040
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

        portfolio_state = _active_portfolio().state() if _active_portfolio() else {}

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


_AEGIS_COOLDOWN = 300  # 5 minutes between adjustments
# Rehydrate from cc_state.json so a reboot within the cooldown window doesn't
# trigger an immediate (unwanted) re-adjustment of deployment limits.
try:
    _last_aegis_adjust = float(_load_cc_state().get("last_aegis_adjust", 0) or 0)
except Exception:
    _last_aegis_adjust = 0


def _get_fleet_fee_ratio():
    """Calculate fleet fee ratio from portfolio history.
    Returns: fee_ratio (total_fees / abs(total_gross_pnl))
    Returns None if insufficient data.
    """
    if not _active_portfolio():
        return None
    try:
        with _active_portfolio()._lock:
            history = _active_portfolio().reservations.get("history", [])
        if not history:
            return None
        # Get last 100 trades for recent fee ratio
        recent = history[-100:] if len(history) > 100 else history
        total_fees = sum(h.get("fees", 0) for h in recent)
        total_gross = sum(h.get("gross_pnl", 0) for h in recent)
        if abs(total_gross) < 0.01:  # Avoid division by near-zero
            return None
        return total_fees / abs(total_gross)
    except Exception:
        return None


def _apply_aegis_adjustment():
    """Read AEGIS score and dynamically adjust portfolio deployment limits."""
    global _last_aegis_adjust
    now = time.time()
    if now - _last_aegis_adjust < _AEGIS_COOLDOWN:
        return
    if not _active_portfolio():
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

    # Get fleet fee ratio for throttle calculation
    fee_ratio = _get_fleet_fee_ratio()

    # Base deployment limit from AEGIS score
    if score >= 0.8:
        base_limit = 90
        regime = "DEPLOY"
    elif score >= 0.5:
        base_limit = 80
        regime = "NORMAL"
    elif score >= 0.2:
        base_limit = 60
        regime = "CAUTIOUS"
    else:
        base_limit = 30
        regime = "DEFENSIVE"

    # Apply fee ratio throttle - reduce deployment when fees are unhealthy
    # fee_ratio > 1.0 means fees > gross profit (bad)
    # fee_ratio > 2.0 means fees are 2x gross profit (catastrophic)
    if fee_ratio is not None and fee_ratio > 1.0:
        if fee_ratio > 2.0:
            fee_multiplier = 0.3  # Severe throttle - 30% of base
            throttle_reason = "FEE_CATASTROPHIC"
        elif fee_ratio > 1.5:
            fee_multiplier = 0.5  # Moderate throttle - 50% of base
            throttle_reason = "FEE_CRITICAL"
        elif fee_ratio > 1.0:
            fee_multiplier = 0.7  # Light throttle - 70% of base
            throttle_reason = "FEE_HIGH"
        else:
            fee_multiplier = 1.0
            throttle_reason = None
        new_limit = int(base_limit * fee_multiplier)
    else:
        new_limit = base_limit
        throttle_reason = None

    # Apply AEGIS adjustment to BOTH portfolios
    for _pm in [_portfolio_paper, _portfolio_live]:
        if not _pm:
            continue
        with _pm._lock:
            old_limit = _pm.limits.get("max_deployed_pct", 80)
            if old_limit != new_limit:
                _pm.limits["max_deployed_pct"] = new_limit
            _pm.aegis_score = float(score)

    if old_limit != new_limit:
        throttle_info = f" (fee_ratio={fee_ratio:.1%})" if fee_ratio else ""
        _event_bus.publish({
            "source": "command_center",
            "type": "PORTFOLIO_LIMIT_CHANGE",
            "data": {
                "aegis_score": round(score, 4),
                "regime": regime,
                "old_limit": old_limit,
                "new_limit": new_limit,
                "throttle_reason": throttle_reason,
                "fee_ratio": round(fee_ratio, 3) if fee_ratio else None,
            },
        })
        log.info(f"AEGIS: {regime} -> {new_limit}% (base={base_limit}){throttle_info}")

    _last_aegis_adjust = now
    _save_cc_state({"last_aegis_adjust": now})


def _poll_loop():
    """Continuously fetch, normalize, aggregate, and store state."""
    _last_stale_cleanup = 0
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
        # Hourly stale reservation cleanup on BOTH portfolios
        if time.time() - _last_stale_cleanup > 3600:
            for _pm_label, _pm in [("paper", _portfolio_paper), ("live", _portfolio_live)]:
                if _pm:
                    released_list = _pm.force_release_stale(max_age_hours=2)
                    if released_list:
                        log.info("Auto-released %d stale %s reservation(s) (>2h old)", len(released_list), _pm_label)
                        for _sr in released_list:
                            log.warning(
                                "Stale reservation force-released: bot=%s pair=%s amount=$%.0f "
                                "(held %.1fh — bot likely crashed before calling release)",
                                _sr["bot_id"], _sr["pair"], _sr["amount"],
                                (time.time() - _sr.get("reserved_at", time.time())) / 3600,
                            )
            _last_stale_cleanup = time.time()
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Kraken live portfolio sync
# ---------------------------------------------------------------------------

KRAKEN_SYNC_INTERVAL = 30  # seconds — match the bot poll cadence
_kraken_sync_state = {
    "last_ok_ts": 0,
    "last_attempt_ts": 0,
    "last_error": None,
    "last_equity": None,
    "consecutive_failures": 0,
}
_kraken_sync_lock = threading.Lock()


def _sync_kraken_balance(force: bool = False) -> dict:
    """Pull current equity from Kraken and update _portfolio_live.total.

    Returns a dict describing the outcome (always — never raises). Loud on
    failure: logs a warning AND emits a KRAKEN_SYNC_FAIL event so the dashboard
    can show sync health. On success, emits KRAKEN_SYNC_OK.

    Reservations are preserved across syncs — only `total` is updated. The
    `available()` calculation re-derives from total minus active reservations.
    """
    if not _portfolio_live:
        return {"ok": False, "reason": "live_portfolio_not_initialized"}

    if not os.environ.get("KRAKEN_API_KEY") or not os.environ.get("KRAKEN_API_SECRET"):
        return {"ok": False, "reason": "no_credentials"}

    with _kraken_sync_lock:
        _kraken_sync_state["last_attempt_ts"] = time.time()
        try:
            from kraken_client import KrakenSpotClient
            kc = KrakenSpotClient()
            tb = kc.get_trade_balance()
        except Exception as e:
            _kraken_sync_state["last_error"] = f"exception:{e}"
            _kraken_sync_state["consecutive_failures"] += 1
            log.warning("Kraken balance sync raised: %s", e, exc_info=True)
            if _event_bus:
                _event_bus.publish({
                    "source": "command_center",
                    "type": "KRAKEN_SYNC_FAIL",
                    "data": {"reason": "exception", "error": str(e),
                             "consecutive_failures": _kraken_sync_state["consecutive_failures"]},
                })
            return {"ok": False, "reason": "exception", "error": str(e)}

        # Empty dict = Kraken-side error (auth, rate limit, server). Loud.
        if not tb:
            _kraken_sync_state["last_error"] = "kraken_returned_empty"
            _kraken_sync_state["consecutive_failures"] += 1
            log.warning("Kraken balance sync returned empty dict (auth/rate-limit/server error?)")
            if _event_bus:
                _event_bus.publish({
                    "source": "command_center",
                    "type": "KRAKEN_SYNC_FAIL",
                    "data": {"reason": "empty_response",
                             "consecutive_failures": _kraken_sync_state["consecutive_failures"]},
                })
            return {"ok": False, "reason": "empty_response"}

        # 'e' = equity (string). Parse defensively.
        try:
            equity = float(tb.get("e", 0))
        except (ValueError, TypeError) as e:
            _kraken_sync_state["last_error"] = f"parse:{e}"
            _kraken_sync_state["consecutive_failures"] += 1
            log.warning("Kraken balance sync: failed to parse equity from %s: %s", tb, e)
            if _event_bus:
                _event_bus.publish({
                    "source": "command_center",
                    "type": "KRAKEN_SYNC_FAIL",
                    "data": {"reason": "parse_error", "raw": tb, "error": str(e)},
                })
            return {"ok": False, "reason": "parse_error", "raw": tb}

        if equity < 0:
            _kraken_sync_state["last_error"] = f"negative_equity:{equity}"
            log.warning("Kraken balance sync: refusing negative equity %.4f", equity)
            return {"ok": False, "reason": "negative_equity", "equity": equity}

        old_total = _portfolio_live.total
        with _portfolio_live._lock:
            _portfolio_live.total = equity
            _portfolio_live._save()

        _kraken_sync_state["last_ok_ts"] = time.time()
        _kraken_sync_state["last_equity"] = equity
        _kraken_sync_state["last_error"] = None
        _kraken_sync_state["consecutive_failures"] = 0

        delta = equity - old_total
        log.info("Kraken sync: equity=$%.4f (was $%.4f, delta %+.4f)", equity, old_total, delta)
        if _event_bus:
            _event_bus.publish({
                "source": "command_center",
                "type": "KRAKEN_SYNC_OK",
                "data": {
                    "equity": round(equity, 4),
                    "old_total": round(old_total, 4),
                    "delta": round(delta, 4),
                    "raw": tb,
                },
            })
        return {"ok": True, "equity": equity, "old_total": old_total, "delta": delta, "raw": tb}


def _kraken_sync_loop():
    """Background daemon: periodically sync _portfolio_live.total from Kraken.

    Only runs when the fleet is in live_armed or live_engaged state. In paper
    mode the loop sleeps without hitting Kraken (no rate-limit cost).
    """
    log.info("Kraken sync loop started (interval=%ds)", KRAKEN_SYNC_INTERVAL)
    while True:
        try:
            state = getattr(_fleet_config, "FLEET_ENGAGE_STATE", "paper")
            if state in ("live_armed", "live_engaged"):
                _sync_kraken_balance()
        except Exception:
            log.warning("Kraken sync loop iteration failed", exc_info=True)
        time.sleep(KRAKEN_SYNC_INTERVAL)


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
    if _active_portfolio():
        return _active_portfolio().state()
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
        "/api/portfolio/reservations": "_serve_portfolio_reservations",
        "/api/portfolio/sync":      "_serve_portfolio_sync_status",
        "/api/fleet/daily":         "_serve_fleet_daily",
        "/api/fleet/promotions":    "_serve_promotions",
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
        "/api/trades":              "_serve_trades",
        "/api/signals/decay":       "_serve_signals_decay",
        "/api/manifest":            "_serve_manifest",
        # UPGRADE: Fleet Intelligence endpoints
        "/api/fleet/exposure":      "_serve_fleet_exposure",
        "/api/fleet/correlations":  "_serve_fleet_correlations",
        "/api/fleet/attribution":   "_serve_fleet_attribution",
        "/api/fleet/briefing":      "_serve_fleet_briefing",
        "/api/fleet/intel_score":   "_serve_fleet_intel_score",
        "/api/watchdog":            "_serve_watchdog",
        "/api/fleet/mode":          "_serve_fleet_mode",
        # Signal Broadcaster dashboard + APIs
        "/signals":                 "_serve_signal_dashboard",
        "/api/signals/feed":        "_serve_signals_feed",
        "/api/signals/broadcaster/config": "_serve_broadcaster_config",
        "/api/signals/broadcaster/stats":  "_serve_broadcaster_stats",
    }

    # -- Prefix-match GET routes (checked after exact miss) -------------------
    _GET_PREFIX_ROUTES: list[tuple[str, str]] = [
        ("/api/bot/",         "_serve_bot_prefix"),
        ("/api/expectancy/",  "_serve_expectancy_prefix"),
        ("/audio/",           "_serve_audio_file"),
        ("/static/",          "_serve_static_file"),
    ]

    # -- Exact-match POST route table -----------------------------------------
    _POST_ROUTES: dict[str, str] = {
        "/api/portfolio/reserve":  "_handle_reserve",
        "/api/portfolio/release":  "_handle_release",
        "/api/portfolio/sync":     "_handle_portfolio_sync",
        "/api/events/publish":     "_handle_event_publish",
        "/api/signals/propose":    "_handle_signals_propose",
        "/api/signals/outcome":    "_handle_signals_outcome",
        "/api/expectancy/record":  "_handle_expectancy_record",
        "/api/fleet/mode":         "_handle_fleet_mode",
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
        try:
            self.wfile.write(_safe_json(obj).encode("utf-8"))
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass

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

        # Serve root-level static assets (.js, .css, .png, etc.)
        if path.endswith(('.js', '.css', '.png', '.ico', '.jpg', '.svg')):
            self._serve_root_asset(parsed, path)
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
            try:
                self.wfile.write(content.encode("utf-8"))
            except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
                pass
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
            try:
                self.wfile.write(placeholder.encode("utf-8"))
            except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
                pass

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

            # Derive Kraken connection status from TrekBot's raw health
            _tb = _state["bots"].get("trekbot")
            _tb_health = ((_tb or {}).get("raw") or {}).get("health") or {} if _tb else {}
            _kraken_status = _tb_health.get("kraken_api", "unknown") if _tb and _tb.get("alive") else "offline"

            payload = {
                "timestamp": time.time(),
                "fleet_mode": _fleet_config.FLEET_MODE,
                "kraken": {
                    "api_keys": bool(os.environ.get("KRAKEN_API_KEY")),
                    "connection": _kraken_status,
                },
                "bots": bots_list,
                "aggregate": _state["aggregate"],
                "feed": _state["feed"],
                "fleet_engage_state": _fleet_config.FLEET_ENGAGE_STATE,
                "portfolio": _active_portfolio().state() if _active_portfolio() else None,
                "portfolio_alt": _alt_portfolio().state() if _alt_portfolio() else None,
                "daily_24h": _fleet_logger.get_daily_accumulator() if _fleet_logger else None,
            }

            # Attach active promotions (lightweight — only active with deadlines)
            try:
                _promo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kraken_promotions.json")
                with open(_promo_file, "r") as _pf:
                    _promos = json.load(_pf).get("promotions", [])
                _today = time.strftime("%Y-%m-%d")
                _active = [p for p in _promos if p.get("status") == "active" and (not p.get("deadline") or p["deadline"] >= _today)]
                if _active:
                    payload["promotions"] = _active
            except Exception:
                pass

        self._send_json(payload)

    def _serve_audio_file(self, parsed, path: str) -> None:
        """Serve static audio files from the audio/ directory."""
        filename = path.split("/audio/", 1)[1].strip("/")
        # Safety: no path traversal
        if "/" in filename or "\\" in filename or ".." in filename:
            self.send_error(400, "Bad Request")
            return
        here = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(here, "audio", filename)
        if not os.path.isfile(file_path):
            self.send_error(404, "Not Found")
            return
        ext = filename.rsplit(".", 1)[-1].lower()
        mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg"}.get(ext, "application/octet-stream")
        try:
            with open(file_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, str(e))

    def _serve_static_file(self, parsed, path: str) -> None:
        """Serve files from the static/ directory."""
        filename = path.split("/static/", 1)[1].strip("/")
        if "/" in filename or "\\" in filename or ".." in filename:
            self.send_error(400, "Bad Request")
            return
        here = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(here, "static", filename)
        if not os.path.isfile(file_path):
            self.send_error(404, "Not Found")
            return
        self._send_file(file_path)

    def _serve_root_asset(self, parsed, path: str) -> None:
        """Serve root-level static assets (.js, .css, .png)."""
        filename = path.lstrip("/")
        if "/" in filename or "\\" in filename or ".." in filename:
            self.send_error(400, "Bad Request")
            return
        here = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(here, filename)
        if not os.path.isfile(file_path):
            self.send_error(404, "Not Found")
            return
        self._send_file(file_path)

    def _send_file(self, file_path: str) -> None:
        """Send a file with proper MIME type."""
        ext = file_path.rsplit(".", 1)[-1].lower()
        mime = {
            "js": "application/javascript", "css": "text/css",
            "png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml",
            "ico": "image/x-icon", "json": "application/json",
        }.get(ext, "application/octet-stream")
        try:
            with open(file_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, str(e))

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
        if not _active_portfolio():
            self._send_json({"error": "Portfolio manager not initialized"}, 503)
            return
        self._send_json(_active_portfolio().state())

    def _serve_portfolio_available(self, parsed) -> None:
        if not _active_portfolio():
            self._send_json({"error": "Portfolio manager not initialized"}, 503)
            return
        self._send_json(_active_portfolio().available_snapshot())

    def _serve_portfolio_exposure(self, parsed) -> None:
        if not _active_portfolio():
            self._send_json({"error": "Portfolio manager not initialized"}, 503)
            return
        self._send_json(_active_portfolio().exposure_snapshot())

    def _serve_portfolio_reservations(self, parsed) -> None:
        """GET /api/portfolio/reservations — return all active reservations."""
        if not _active_portfolio():
            self._send_json({"error": "Portfolio manager not initialized"}, 503)
            return
        with _active_portfolio()._lock:
            reservations = {
                rid: {
                    "bot_id": r["bot_id"],
                    "pair": r["pair"],
                    "amount": r["amount"],
                    "direction": r.get("direction"),
                    "reserved_at": r["reserved_at"],
                }
                for rid, r in _active_portfolio().reservations.items()
            }
        self._send_json({"reservations": reservations})

    def _serve_fleet_daily(self, parsed) -> None:
        if not _fleet_logger:
            self._send_json({"error": "Fleet logger not initialized"}, 503)
            return
        self._send_json(_fleet_logger.get_daily_accumulator())

    def _serve_promotions(self, parsed) -> None:
        """GET /api/fleet/promotions — active Kraken promotions with deadlines."""
        promo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kraken_promotions.json")
        try:
            with open(promo_file, "r") as f:
                data = json.load(f)
            today = time.strftime("%Y-%m-%d")
            for p in data.get("promotions", []):
                dl = p.get("deadline")
                if dl and dl < today:
                    p["status"] = "expired"
                elif dl:
                    from datetime import datetime
                    days_left = (datetime.strptime(dl, "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
                    p["days_left"] = days_left
            self._send_json(data)
        except FileNotFoundError:
            self._send_json({"promotions": []})

    def _serve_fleet_mode(self, parsed) -> None:
        """GET /api/fleet/mode — current trading mode + engage state."""
        has_keys = bool(os.environ.get("KRAKEN_API_KEY"))
        self._send_json({
            "mode": _fleet_config.FLEET_MODE,
            "engage_state": _fleet_config.FLEET_ENGAGE_STATE,
            "has_api_keys": has_keys,
            "effective": "live" if (_fleet_config.FLEET_MODE == "live" and has_keys) else "paper",
            "paper_total": _portfolio_paper.total if _portfolio_paper else 0,
            "live_total": _portfolio_live.total if _portfolio_live else 0,
        })

    def _handle_fleet_mode(self, data: dict) -> None:
        """POST /api/fleet/mode — paper/live/engage/disengage."""
        new_mode = data.get("mode", "").lower().strip()
        if new_mode not in ("paper", "live", "engage", "disengage"):
            self._send_json({"error": "mode must be 'paper', 'live', 'engage', or 'disengage'"}, 400)
            return
        has_keys = bool(os.environ.get("KRAKEN_API_KEY"))
        if new_mode == "live" and not has_keys:
            self._send_json({"error": "Cannot go live: KRAKEN_API_KEY not set"}, 400)
            return

        # When switching to live, set the live portfolio balance.
        # Manual override (data.live_total) wins; otherwise always re-sync from
        # Kraken — no gate on total==0, so toggling live always pulls fresh.
        if new_mode == "live":
            live_total = data.get("live_total")
            if live_total is not None:
                _portfolio_live.total = float(live_total)
                _portfolio_live.reservations.clear()
                _portfolio_live._save()
                log.info("Live portfolio balance manually set to $%.2f", float(live_total))
            elif _portfolio_live:
                sync_result = _sync_kraken_balance(force=True)
                if sync_result.get("ok"):
                    log.info("Live portfolio synced from Kraken: $%.4f", sync_result["equity"])
                else:
                    log.warning("Kraken sync on live transition failed: %s", sync_result.get("reason"))

        old_state = _fleet_config.FLEET_ENGAGE_STATE
        try:
            new_state = _fleet_config.set_fleet_mode(new_mode)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
            return

        log.warning("Fleet state changed: %s -> %s", old_state, new_state)
        if _event_bus:
            _event_bus.publish({
                "source": "command_center",
                "type": "FLEET_MODE_CHANGE",
                "data": {"old": old_state, "new": new_state, "action": new_mode},
            })
        self._send_json({
            "engage_state": new_state,
            "mode": _fleet_config.FLEET_MODE,
            "paper_total": _portfolio_paper.total if _portfolio_paper else 0,
            "live_total": _portfolio_live.total if _portfolio_live else 0,
        })

    # -- Signal Broadcaster handlers ------------------------------------------

    def _serve_signal_dashboard(self, parsed) -> None:
        self._serve_html("signal_dashboard.html")

    def _serve_signals_feed(self, parsed) -> None:
        """GET /api/signals/feed — recent signals sent by broadcaster."""
        qs = parse_qs(parsed.query)
        n = int(qs.get("n", [50])[0])
        try:
            import http.client
            conn = http.client.HTTPConnection("localhost", 9002, timeout=3)
            conn.request("GET", "/stats")
            resp = conn.getresponse()
            stats = json.loads(resp.read().decode("utf-8"))
            conn.close()
            # Also get feed
            conn2 = http.client.HTTPConnection("localhost", 9002, timeout=3)
            conn2.request("GET", "/feed")
            resp2 = conn2.getresponse()
            feed = json.loads(resp2.read().decode("utf-8"))
            conn2.close()
            self._send_json({"feed": feed, "stats": stats})
        except Exception:
            # Broadcaster not running — fall back to log file
            log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals_sent.log")
            entries = []
            if os.path.exists(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    for line in lines[-n:]:
                        try:
                            entries.append(json.loads(line.strip()))
                        except Exception:
                            pass
                except Exception:
                    pass
            self._send_json({"feed": entries, "stats": {}, "error": "broadcaster not reachable"})

    def _serve_broadcaster_config(self, parsed) -> None:
        """GET /api/signals/broadcaster/config — current broadcaster config."""
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_config.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            # Redact token
            if "telegram_bot_token" in config:
                token = config["telegram_bot_token"]
                config["telegram_bot_token"] = f"{token[:8]}..." if len(token) > 8 else "***"
            self._send_json(config)
        except FileNotFoundError:
            self._send_json({"error": "signal_config.json not found"}, 404)

    def _serve_broadcaster_stats(self, parsed) -> None:
        """GET /api/signals/broadcaster/stats — broadcaster stats from log file + health check."""
        stats = {"sse": {"connected": False}, "channel": {}, "config": {}}
        # Check if broadcaster is alive (simple socket test)
        broadcaster_alive = False
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect(("localhost", 9002))
            s.close()
            broadcaster_alive = True
        except Exception:
            pass
        if False:  # http.client proxy disabled — Python 3.14 HTTP/1.0 bug
            pass
        else:
            # Broadcaster not reachable via network — build stats from log file
            log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals_sent.log")
            free_today = 0
            paid_today = 0
            failed = 0
            last_free = ""
            last_paid = ""
            today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            if os.path.exists(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                entry = json.loads(line.strip())
                                ts = entry.get("ts", "")
                                if not ts.startswith(today_str):
                                    continue
                                if entry.get("tier") == "paid":
                                    paid_today += 1
                                    last_paid = ts
                                elif entry.get("tier") == "free":
                                    free_today += 1
                                    last_free = ts
                                if not entry.get("success"):
                                    failed += 1
                            except Exception:
                                pass
                except Exception:
                    pass
            stats = {
                "sse": {"connected": broadcaster_alive, "events_received": 0},
                "channel": {
                    "paid_sent_today": paid_today,
                    "free_sent_today": free_today,
                    "failed_today": failed,
                    "last_paid_ts": last_paid,
                    "last_free_ts": last_free,
                },
                "config": {"enabled": True, "rate_limit": 30},
                "source": "log_fallback",
            }
        self._send_json(stats)

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
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError, OSError):
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

    def _serve_trades(self, parsed) -> None:
        """Return per-bot trade history from the durable event bus log.
        Survives restarts via logs/event_bus/*.jsonl (written by EventBus.publish).
        Query params: ?bot=trekbot&limit=50

        Two-stage dedup:
          1. By event id (catches the same event written twice to disk)
          2. By reservation_id (the canonical trade key — same trade emitted
             via the release path AND the snapshot-diff bridge)
          3. By heuristic (bot, pair, round(pnl), 5min window) for legacy
             snapshot-diff entries that have no reservation_id
        """
        import glob as _glob
        qs = parsed.query or ""
        params: dict[str, str] = {}
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = v
        bot_filter = params.get("bot", "").strip().lower()
        try:
            limit = int(params.get("limit", "100"))
        except ValueError:
            limit = 100

        raw_trades: list[dict] = []
        log_dirs = [
            os.path.join(os.path.dirname(__file__), "logs", "event_bus"),
            os.path.join(os.path.dirname(__file__), "logs", "events"),
        ]
        seen_ids: set[str] = set()
        for log_dir in log_dirs:
            if not os.path.isdir(log_dir):
                continue
            for fpath in sorted(_glob.glob(os.path.join(log_dir, "*.jsonl"))):
                try:
                    with open(fpath, encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                ev = json.loads(line)
                            except Exception:
                                continue
                            if ev.get("type") != "TRADE_CLOSE":
                                continue
                            ev_id = ev.get("id")
                            if ev_id and ev_id in seen_ids:
                                continue
                            if ev_id:
                                seen_ids.add(ev_id)
                            bot = ev.get("source") or ev.get("bot") or ""
                            if bot_filter and bot.lower() != bot_filter:
                                continue
                            d = ev.get("data") if isinstance(ev.get("data"), dict) else ev
                            pnl_val = d.get("pnl", ev.get("pnl", 0))
                            rid = d.get("reservation_id") or ev.get("reservation_id")
                            via = d.get("via") or ev.get("via", "")
                            raw_trades.append({
                                "ts":             ev.get("ts", 0),
                                "bot":            bot,
                                "pair":           d.get("pair", ev.get("pair", "")),
                                "pnl":            pnl_val,
                                "gross_pnl":      d.get("gross_pnl", ev.get("gross_pnl", pnl_val)),
                                "fees":           d.get("fees", ev.get("fees", 0)),
                                "duration_s":     d.get("duration_s", ev.get("duration_s", 0)),
                                "exit_reason":    d.get("exit_reason", ev.get("exit_reason", "")),
                                "direction":      d.get("direction", ev.get("direction", ev.get("side", ""))),
                                "won":            (pnl_val or 0) > 0,
                                "reservation_id": rid,
                                "via":            via,
                            })
                except Exception:
                    pass

        # ── Stage 2 dedup: by reservation_id ──
        # Same trade can land on disk twice — once from _handle_release
        # (via=portfolio_release, has reservation_id) and once from
        # fleet_logger._detect_events (snapshot diff, no reservation_id).
        # Prefer the release-path entry — it has accurate fees and uppercase
        # direction. Fall back to the snapshot-diff entry only when it's the
        # only one we have for that trade.
        by_rid: dict[str, dict] = {}
        no_rid: list[dict] = []
        for t in raw_trades:
            rid = t.get("reservation_id")
            if rid:
                # First one wins; release-path entries always carry rid so
                # they will populate by_rid before any duplicate could.
                if rid not in by_rid:
                    by_rid[rid] = t
            else:
                no_rid.append(t)

        # ── Stage 3 dedup: heuristic for snapshot-diff entries ──
        # The snapshot-diff path emits TRADE_CLOSE from fleet_logger by
        # observing positions disappearing between polls. It can capture a
        # stale unrealized_pnl number that doesn't match the realized pnl
        # the release path records. So we cannot dedup by PnL match.
        #
        # Instead: drop a no-rid entry if there's a by_rid entry for the
        # same (bot, pair) within 600s. Same pair, same bot, within 10
        # minutes, with a release record in hand → it's the same trade,
        # and the release record is canonical (real fees, real direction
        # casing, real reservation_id). The 90s same-pair OPEN cooldown in
        # PortfolioManager makes faster legitimate turnover impossible.
        rid_lookup: list[tuple[str, str, float]] = [
            (t.get("bot", ""), t.get("pair", ""), float(t.get("ts") or 0))
            for t in by_rid.values()
        ]
        deduped_no_rid: list[dict] = []
        for t in no_rid:
            tb = t.get("bot", "")
            tp = t.get("pair", "")
            try:
                tts = float(t.get("ts") or 0)
            except (ValueError, TypeError):
                tts = 0.0
            is_dup = False
            for (rb, rp, rts) in rid_lookup:
                if rb == tb and rp == tp and abs(rts - tts) < 600:
                    is_dup = True
                    break
            if not is_dup:
                # Dedup no-rid entries against each other too. Two snapshot
                # diffs for the same trade (60s apart from successive polls)
                # would otherwise both survive.
                already = False
                for kept in deduped_no_rid:
                    if (kept.get("bot") == tb and kept.get("pair") == tp
                            and abs(float(kept.get("ts") or 0) - tts) < 600):
                        already = True
                        break
                if not already:
                    deduped_no_rid.append(t)

        trades = list(by_rid.values()) + deduped_no_rid

        # newest first, capped
        trades.sort(key=lambda t: t["ts"], reverse=True)
        self._send_json({"trades": trades[:limit], "total": len(trades)})

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
            "bots_total": len(bot_list),  # polled bots only
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

    def _serve_watchdog(self, parsed) -> None:
        """GET /api/watchdog — fleet health from watchdog's perspective."""
        now = time.time()
        snapshot = dict(_watchdog_state)
        online = sum(1 for s in snapshot.values() if s.get("alive"))
        dead = sum(1 for s in snapshot.values() if s.get("state") == "dead")
        self._send_json({
            "timestamp": now,
            "summary": {
                "total": len(snapshot),
                "online": online,
                "dead": dead,
                "failing": sum(1 for s in snapshot.values() if s.get("state") == "failing"),
            },
            "bots": snapshot,
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
            with _open_trade_signals_lock:
                _open_trade_signals[f"{source}:{pair}"] = {
                    "signals": edata["signals"],
                    "factors": edata.get("factors", {}),
                    "opened_at": time.time(),
                }
            _save_open_trade_signals()
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
            with _open_trade_signals_lock:
                open_info = _open_trade_signals.pop(key, None)
            if open_info is not None:
                _save_open_trade_signals()
            pnl = edata.get("pnl", 0)
            direction = edata.get("direction", "LONG").upper()
            won = pnl > 0
            _signal_aggregator.record_outcome(pair, direction, won, pnl)
            if open_info and open_info.get("signals"):
                sigs = open_info["signals"]
                contrib = [f"{source}:{s}" for s in sigs] if isinstance(sigs, list) else [source]
                _signal_decomposition.log_trade(
                    pair, direction, gross_pnl=pnl, fees=edata.get("fees", 0),
                    duration=edata.get("duration_h", 0) * 3600 if edata.get("duration_h") else edata.get("duration_s", 0),
                    contributing_signals=contrib,
                )
        self._send_json({"ok": True})

    def _handle_reserve(self, data: dict) -> None:
        """POST /api/portfolio/reserve — bot requests capital."""
        missing = [f for f in ("bot_id", "pair", "direction", "amount") if f not in data]
        if missing:
            self._send_json({"error": f"Missing fields: {', '.join(missing)}"}, 400)
            return

        try:
            amount = float(data["amount"])
        except (ValueError, TypeError):
            self._send_json({"error": "Invalid amount"}, 400)
            return

        mgr = _get_portfolio_for_bot(data["bot_id"])
        if mgr is None:
            self._send_json({
                "ok": False,
                "reason": f"LIVE_ARMED: trading paused until ENGAGE ({_fleet_config.FLEET_ENGAGE_STATE})",
            }, 403)
            return

        result = mgr.reserve(
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
        rid = data.get("reservation_id")
        if not rid:
            self._send_json({"error": "Missing reservation_id"}, 400)
            return

        pnl = float(data.get("pnl", 0))
        # Search both portfolios for this reservation
        result = None
        for mgr in [_portfolio_paper, _portfolio_live]:
            if mgr and rid in mgr.reservations:
                result = mgr.release(rid, pnl=pnl)
                break
        if result is None:
            result = {"ok": False, "reason": f"Reservation '{rid}' not found in either portfolio"}

        if result["ok"]:
            res_info = result["reservation"]
            if _fleet_logger:
                _fleet_logger.log_portfolio_release(
                    res_info["bot_id"], res_info["pair"],
                    res_info["amount"], pnl, rid)

            # Record to expectancy tracker for fleet analytics.
            # PNL-driven mode: pass realized pnl directly so trades from bots
            # that don't report entry/exit prices (Gridzilla, Rubberband, etc.)
            # don't divide-by-zero in the legacy formula path. trade_id=rid
            # dedups against the snapshot-diff bridge so the same close
            # can't be recorded twice.
            _duration = time.time() - res_info.get("reserved_at", time.time())
            if _expectancy_tracker:
                try:
                    _expectancy_tracker.record_trade(
                        bot_id=res_info["bot_id"],
                        pair=res_info["pair"],
                        direction=res_info.get("direction", "LONG"),
                        entry_price=data.get("entry_price", 0),
                        exit_price=data.get("exit_price", 0),
                        size_usd=res_info["amount"],
                        duration=_duration,
                        fee_rate=_fleet_config.KRAKEN_FEE_TAKER,
                        realized_pnl=pnl,
                        trade_id=rid,
                    )
                except Exception:
                    log.warning("expectancy.record_trade failed for %s", rid,
                                exc_info=True)

            # Publish TRADE_CLOSE to the event bus so /api/trades and downstream
            # consumers (signal aggregator, decomposition, dashboards) see this
            # trade the same way they see bot-emitted closes. Without this,
            # portfolio-flow trades are invisible to /api/trades which filters
            # for type=TRADE_CLOSE. The event also gets durably logged by the
            # bus itself (logs/event_bus/*.jsonl) so it survives reboots.
            try:
                _size = float(res_info.get("amount") or 0)
                _fees = round(_size * _fleet_config.KRAKEN_FEE_TAKER * 2, 4)  # round-trip
                _event_bus.publish({
                    "source": res_info.get("bot_id", "portfolio"),
                    "type": "TRADE_CLOSE",
                    "data": {
                        "pair": res_info.get("pair", ""),
                        "direction": res_info.get("direction", "LONG"),
                        "entry_price": data.get("entry_price", 0),
                        "exit_price": data.get("exit_price", 0),
                        "size_usd": _size,
                        "pnl": round(float(pnl), 4),
                        "fees": _fees,
                        "duration_s": round(_duration),
                        "reservation_id": rid,
                        "via": "portfolio_release",
                    },
                })
            except Exception:
                pass

        # Don't leak internal reservation data to the API caller
        result.pop("reservation", None)
        self._send_json(result, 200 if result["ok"] else 404)

    def _handle_portfolio_sync(self, data: dict) -> None:
        """POST /api/portfolio/sync — force a Kraken balance refresh.

        Pulls live equity from Kraken (TradeBalance), updates _portfolio_live.total,
        and returns the result. Reservations are preserved. Available balance is
        recomputed from total minus active reservations.
        """
        result = _sync_kraken_balance(force=True)
        if result.get("ok"):
            result["available"] = _portfolio_live.available() if _portfolio_live else 0
            self._send_json(result, 200)
        else:
            self._send_json(result, 502)

    def _serve_portfolio_sync_status(self, parsed) -> None:
        """GET /api/portfolio/sync — last sync status (no Kraken call)."""
        with _kraken_sync_lock:
            state = dict(_kraken_sync_state)
        state["interval_secs"] = KRAKEN_SYNC_INTERVAL
        state["fleet_state"] = getattr(_fleet_config, "FLEET_ENGAGE_STATE", "paper")
        state["live_total"] = _portfolio_live.total if _portfolio_live else 0
        state["live_available"] = _portfolio_live.available() if _portfolio_live else 0
        if state["last_ok_ts"]:
            state["seconds_since_ok"] = round(time.time() - state["last_ok_ts"])
        else:
            state["seconds_since_ok"] = None
        self._send_json(state)

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
            fee_rate=data.get("fee_rate", 0.0040),
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


_watchdog_state: dict = {}  # shared state for /api/watchdog endpoint
# bot_id -> last restart timestamp. Rehydrated from cc_state.json so the health
# monitor doesn't re-restart a bot that was just restarted before CC rebooted.
_restart_cooldowns: dict[str, float] = {}
try:
    _cd = _load_cc_state().get("restart_cooldowns", {})
    if isinstance(_cd, dict):
        _restart_cooldowns = {k: float(v) for k, v in _cd.items()
                              if isinstance(v, (int, float, str))}
except Exception:
    _restart_cooldowns = {}
_RESTART_COOLDOWN_S = _FC_WATCHDOG_COOLDOWN


def _kill_zombies_on_port(port: int) -> None:
    """Kill any processes listening on a port before restarting a bot."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                try:
                    pid = int(parts[-1])
                    if pid > 0:
                        subprocess.run(
                            ["taskkill", "/F", "/PID", str(pid)],
                            capture_output=True, timeout=5,
                        )
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass


def _health_monitor() -> None:
    """Background thread: detect crashed bots, auto-restart, publish events."""
    global _watchdog_state
    fails: dict[str, int] = {b["id"]: 0 for b in BOT_REGISTRY}
    last_seen: dict[str, float] = {b["id"]: 0.0 for b in BOT_REGISTRY}
    # Circuit breaker state: open after 10 failures, closed after cooldown
    circuit_state: dict[str, dict] = {b["id"]: {"open": False, "opened_at": 0.0} for b in BOT_REGISTRY}
    CIRCUIT_FAILURE_THRESHOLD = 10
    CIRCUIT_COOLDOWN_SECS = 300  # 5 minutes
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
        watchdog_snapshot: dict = {}
        for bot in BOT_REGISTRY:
            bid = bot["id"]
            port = bot["port"]
            alive = False
            # Try /api/snapshot first (most bots), fall back to /health
            for endpoint in ["/api/snapshot", "/health"]:
                try:
                    resp = requests.get(f"http://127.0.0.1:{port}{endpoint}", timeout=3)
                    if resp.status_code == 200:
                        alive = True
                        break
                except Exception:
                    pass

            if alive:
                if fails[bid] >= _FC_WATCHDOG_FAILURES:
                    _event_bus.publish({"source": "health_monitor", "type": "BOT_RECOVERED",
                                        "data": {"bot": bid, "port": port}})
                    log.info("Bot recovered: %s on :%s", bid, port)
                fails[bid] = 0
                last_seen[bid] = time.time()
                # Close circuit breaker on recovery
                if circuit_state[bid]["open"]:
                    circuit_state[bid]["open"] = False
                    log.info(f"Circuit breaker closed for {bid} (recovered)")
            else:
                fails[bid] += 1

            state = "online" if alive else ("dead" if fails[bid] >= _FC_WATCHDOG_FAILURES else "failing")
            cooldown_remaining = max(0, int(_RESTART_COOLDOWN_S - (time.time() - _restart_cooldowns.get(bid, 0))))

            # Circuit breaker: skip polling dead bots during cooldown
            circuit = circuit_state[bid]
            if circuit["open"] and time.time() - circuit["opened_at"] < CIRCUIT_COOLDOWN_SECS:
                # Skip polling - circuit is open
                state = "circuit_open"
                cooldown_remaining = max(0, int(CIRCUIT_COOLDOWN_SECS - (time.time() - circuit["opened_at"])))
                watchdog_snapshot[bid] = {
                    "alive": False,
                    "port": port,
                    "state": state,
                    "consecutive_failures": fails[bid],
                    "last_seen": last_seen[bid],
                    "cooldown_remaining_s": cooldown_remaining,
                }
                continue

            watchdog_snapshot[bid] = {
                "alive": alive,
                "port": port,
                "state": state,
                "consecutive_failures": fails[bid],
                "last_seen": last_seen[bid],
                "cooldown_remaining_s": cooldown_remaining,
            }

            if fails[bid] == _FC_WATCHDOG_FAILURES:
                _event_bus.publish({"source": "health_monitor", "type": "BOT_DOWN",
                                    "data": {"bot": bid, "port": port, "consecutive_fails": _FC_WATCHDOG_FAILURES}})
                log.warning("Bot down: %s on :%s — attempting auto-restart", bid, port)

                # Check cooldown
                last_restart = _restart_cooldowns.get(bid, 0)
                if time.time() - last_restart < _RESTART_COOLDOWN_S:
                    log.warning("Skipping restart of %s — cooldown (%ds remaining)",
                                bid, int(_RESTART_COOLDOWN_S - (time.time() - last_restart)))
                    continue

                bcfg = bot_cmds.get(bid)
                if bcfg and bcfg.get("dir"):
                    try:
                        # Kill any zombie processes on this port first
                        _kill_zombies_on_port(port)
                        time.sleep(2)

                        cmd_val = bcfg.get("cmd", [])
                        if isinstance(cmd_val, str):
                            cmd_val = [cmd_val]
                        if not cmd_val:
                            continue
                        # cmd_val is e.g. ["turtlebot.py", "--auto"]
                        _restart_log_path = os.path.join(
                            os.path.dirname(os.path.abspath(__file__)), "logs", f"{bid}_restart.log"
                        )
                        restart_log = open(_restart_log_path, "a")
                        subprocess.Popen(
                            ["python"] + cmd_val,
                            cwd=bcfg["dir"],
                            stdout=subprocess.DEVNULL,
                            stderr=restart_log,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
                        )
                        _restart_cooldowns[bid] = time.time()
                        _save_cc_state({"restart_cooldowns": _restart_cooldowns})
                        log.info("Auto-restarted %s (port %s)", bid, port)
                        _event_bus.publish({"source": "health_monitor", "type": "BOT_RESTARTED",
                                            "data": {"bot": bid, "port": port}})
                    except Exception:
                        log.error("Auto-restart failed for %s", bid, exc_info=True)

            # Open circuit breaker after repeated failures
            if fails[bid] >= CIRCUIT_FAILURE_THRESHOLD and not circuit_state[bid]["open"]:
                circuit_state[bid]["open"] = True
                circuit_state[bid]["opened_at"] = time.time()
                log.warning(f"Circuit breaker OPEN for {bid} — skipping polls for {CIRCUIT_COOLDOWN_SECS}s")

        _watchdog_state = watchdog_snapshot
        time.sleep(_FC_WATCHDOG_INTERVAL)


def main():
    global _portfolio_paper, _portfolio_live, _fleet_logger

    print(BANNER)
    print(f"  Fleet: {len(BOT_REGISTRY)} bots registered")
    for bot in BOT_REGISTRY:
        print(f"    {bot['name']:12s}  :{bot['port']}  {bot['endpoints']}")
    print()

    # Initialize dual portfolio managers
    _portfolio_paper = PortfolioManager(PORTFOLIO_TOTAL, PORTFOLIO_LIMITS, PORTFOLIO_FILE, mode_tag="paper")
    stale = _portfolio_paper.force_release_stale(max_age_hours=2)
    if stale:
        print(f"  Paper Portfolio: released {len(stale)} stale reservation(s) (>2h old)")
    print(f"  Paper Portfolio: ${_portfolio_paper.available():,.2f} available of ${_portfolio_paper.total:,.2f}")

    _live_file = _fleet_config.PORTFOLIO_LIVE_FILE
    if os.path.exists(_live_file):
        _portfolio_live = PortfolioManager(0, PORTFOLIO_LIMITS, _live_file, mode_tag="live")
        _portfolio_live.force_release_stale(max_age_hours=2)
        print(f"  Live Portfolio:  ${_portfolio_live.available():,.2f} available of ${_portfolio_live.total:,.2f}")
    else:
        _portfolio_live = PortfolioManager(0, PORTFOLIO_LIMITS, _live_file, mode_tag="live")
        print(f"  Live Portfolio:  $0.00 (set balance when switching to live)")

    print(f"  Portfolio API: http://localhost:9000/api/portfolio")
    print()

    _has_keys = bool(os.environ.get("KRAKEN_API_KEY"))
    _mode_label = _fleet_config.FLEET_MODE.upper()
    if _fleet_config.FLEET_MODE == "live" and _has_keys:
        print(f"  Fleet Mode: \033[91m*** {_mode_label} — REAL KRAKEN ORDERS ***\033[0m")
    elif _has_keys:
        print(f"  Fleet Mode: {_mode_label} (API keys present, toggle via POST /api/fleet/mode)")
    else:
        print(f"  Fleet Mode: {_mode_label} (no API keys set)")
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

    # Kraken live portfolio sync (only hits Kraken when fleet is in live state)
    threading.Thread(target=_kraken_sync_loop, daemon=True, name="KrakenSync").start()
    print(f"  Kraken Sync: live portfolio refresh every {KRAKEN_SYNC_INTERVAL}s (live mode only)")

    # Bridge fleet logger events to event bus + measurement infrastructure.
    # Uses the module-level _open_trade_signals dict (not a local) so that both
    # this bridge AND _handle_event_publish share the same attribution map.
    # The map is persisted to disk on every mutation so it survives reboots.
    _load_open_trade_signals()

    def _on_logger_event(event):
        _event_bus.publish(event)
        data = event.get("data", {})
        etype = event.get("type")
        bot = data.get("bot", event.get("source", ""))
        pair = data.get("pair", "")
        if etype == "TRADE_OPEN" and data.get("signals"):
            with _open_trade_signals_lock:
                _open_trade_signals[f"{bot}:{pair}"] = {
                    "signals": data["signals"],
                    "factors": data.get("factors", {}),
                    "opened_at": time.time(),
                }
            _save_open_trade_signals()
        elif etype == "TRADE_CLOSE" and pair:
            key = f"{bot}:{pair}"
            with _open_trade_signals_lock:
                open_info = _open_trade_signals.pop(key, None)
            if open_info is not None:
                _save_open_trade_signals()
            pnl = data.get("pnl", 0)
            direction = data.get("direction", "LONG")
            won = pnl > 0
            _signal_aggregator.record_outcome(pair, direction, won, pnl)
            if open_info and open_info.get("signals"):
                sigs = open_info["signals"]
                contrib = [f"{bot}:{s}" for s in sigs] if isinstance(sigs, list) else [bot]
                _signal_decomposition.log_trade(
                    pair, direction, gross_pnl=pnl, fees=data.get("fees", 0),
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
    server = ThreadedHTTPServer(("0.0.0.0", CC_PORT), CommandCenterHandler)
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
