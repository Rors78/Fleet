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

# This module makes 53 log.* calls and configured no handler, so Python fell
# back to lastResort: INFO was DROPPED ENTIRELY and WARNING+ went to stderr
# bare, with no timestamp or level prefix. The launcher merges stderr into the
# bot log (stderr=subprocess.STDOUT), so warnings did land in the file — but
# indistinguishable from a stray print, and every informational line about
# capital decisions was lost. Verified: zero log records of any level in
# logs/bots/command_center.log across an entire run.
if not log.handlers and not logging.getLogger().handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter(
        "%(asctime)sZ [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

from fleet_logger import FleetLogger
from event_bus import EventBus
from collector import start_collector, register_brainiac_endpoints
from signal_aggregator import SignalAggregator
from signal_decomposition import SignalDecomposition
from probe_pairs import is_probe_pair
from expectancy import ExpectancyTracker
from signal_decay import SignalDecay
from fleet_intel_score import FleetIntelScore

import urllib.request as _urlreq  # UPGRADE: Fleet Intelligence — for LLM briefing

from fleet_config import (
    CC_PORT, INFERENCE_URL as _FC_INFERENCE_URL,
    POLL_INTERVAL as _FC_POLL_INTERVAL, REQUEST_TIMEOUT as _FC_REQUEST_TIMEOUT,
    UNIVERSE_REFRESH_HOURS as _FC_UNIVERSE_REFRESH_HOURS,
    KRAKEN_REST as _FC_KRAKEN_REST, PORTFOLIO_TOTAL as _FC_PORTFOLIO_TOTAL,
    PORTFOLIO_LIMITS as _FC_PORTFOLIO_LIMITS, TRADING_BOTS as _FC_TRADING_BOTS,
    PORTFOLIO_FILE as _FC_PORTFOLIO_FILE, WATCHDOG_COOLDOWN as _FC_WATCHDOG_COOLDOWN,
    WATCHDOG_INTERVAL as _FC_WATCHDOG_INTERVAL, WATCHDOG_FAILURES as _FC_WATCHDOG_FAILURES,
    bot_registry_list,
)
import fleet_config as _fleet_config
from win_rate_scale import to_percent

# ---------------------------------------------------------------------------
# Bot Registry — sourced from fleet_config.py (single source of truth)
# ---------------------------------------------------------------------------

BOT_REGISTRY = bot_registry_list()

POLL_INTERVAL = _FC_POLL_INTERVAL
REQUEST_TIMEOUT = _FC_REQUEST_TIMEOUT
MAX_FEED_SIZE = 50
UNIVERSE_REFRESH_HOURS = _FC_UNIVERSE_REFRESH_HOURS
KRAKEN_REST = _FC_KRAKEN_REST

# Smallest reservation the pool will accept, in dollars. Deliberately absolute
# rather than a fraction of the pool — see the size-floor check in
# PortfolioManager.reserve() for why a percentage silently broke the fleet when
# the pool was resized. This filters dust without tracking pool size.
#
# Lowered 100.00 -> 1.00 on 2026-08-13 when the operator set the pool to
# $210.53. At that size the per-trade cap is $42.11 and TurtleSue's 10%
# pool share is $21.05 — both below the OLD $100 floor, so every bot was
# refused and the fleet was parked.
#
# $5 was the first attempt and was still too high, for a reason worth
# recording: the floor is checked AFTER fleet-intel risk scaling, not
# before. TurtleSue's real $21.05 basis scaled by a live 0.21 risk
# multiplier is $4.42 — refused. Only requests near $40 survived, which is
# above what any bot actually asks for. Verified against the running pool
# rather than reasoned about: the live multipliers today run 0.15-0.39.
#
# $1 sits below the smallest legitimate POST-SCALE size while still
# filtering true dust. The real limiting is done by the per-trade and
# per-pair caps, not by this.
#
# Still absolute, deliberately: the comment above records that making this
# a percentage is what broke the fleet last time the pool was resized. If
# the pool returns to six figures, raise this by hand — it is a dust
# filter, not a risk control, and the per-trade/per-pair caps do the
# actual limiting.
MIN_TRADE_USD = 0.25

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


_cc_state_unreadable = False


def _load_cc_state() -> dict:
    """Load the small-state dict from disk. Returns {} when absent OR broken.

    Sets the module-level _cc_state_unreadable when the file EXISTS but could
    not be read, because the two are not the same thing and the callers below
    treat "no entry" as "cooldown long expired". A file truncated by a restart
    landing mid-write therefore used to disarm every restart cooldown and the
    AEGIS adjustment hold at once — silently, on the boot most likely to have
    caused it.
    """
    global _cc_state_unreadable
    _cc_state_unreadable = False
    if not os.path.exists(_CC_STATE_PATH):
        return {}
    try:
        with open(_CC_STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
        _cc_state_unreadable = True
        log.error("CC state %s is not a dict (%s) — treating cooldowns as "
                  "UNKNOWN, not expired.", _CC_STATE_PATH, type(data).__name__)
    except Exception as e:
        _cc_state_unreadable = True
        log.error("UNREADABLE CC state %s: %s — restart cooldowns and the "
                  "AEGIS hold will be treated as JUST SET, not expired.",
                  _CC_STATE_PATH, e)
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
    COOLDOWN_EXEMPT_BOTS = {"gridzilla"}  # grid bots manage their own entry frequency internally

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
        self._bot_confirms: dict[str, float] = {}  # bot_id -> last lease-confirm ts
        self.aegis_score: float = 0.0  # updated by _apply_aegis_adjustment each cycle
        self._load()

    # ── Persistence ──

    def _load(self) -> None:
        """Load state from portfolio.json if it exists.

        Sets self.state_unreadable when the file EXISTS but cannot be parsed.
        Absent and corrupt used to produce the same result, and that result
        is the LOOSEST possible state: max_deployed_pct falls back to the 80%
        config default (discarding an AEGIS tightening to 30%), reservations
        come back empty so deployed() reports 0.00 and the ENTIRE pool reads
        as available, and the raise-hold clock resets. A truncated write
        would hand the fleet a $1M unreserved pool at the loosest cap in the
        regime AEGIS had just scored defensive.

        Unreadable therefore fails toward ARMED: reserve() refuses new
        capital until a human looks, and the file is preserved.
        """
        self.state_unreadable = False
        try:
            with open(self.filepath, "r") as f:
                data = json.load(f)
            _t = data.get("total", self.total)
            self.total = _t if isinstance(_t, (int, float)) and math.isfinite(_t) \
                else self.total
            # Validate every restored reservation. deployed() sums these, so a
            # single NaN or non-numeric amount makes the sum NaN — and then
            # every risk gate (all upper-bound comparisons) evaluates False
            # and the JSON response cannot be encoded at all. A poisoned file
            # would otherwise survive every restart.
            _res = data.get("reservations", {})
            self.reservations = {}
            if isinstance(_res, dict):
                for _rid, _r in _res.items():
                    _a = _r.get("amount") if isinstance(_r, dict) else None
                    if isinstance(_a, (int, float)) and math.isfinite(_a) and _a > 0:
                        self.reservations[_rid] = _r
                    else:
                        log.error("DROPPED unloadable reservation %s: amount=%r "
                                  "— not a finite positive number", _rid, _a)
            self.history = data.get("history", [])
            # AEGIS tightens max_deployed_pct when the regime turns defensive
            # (60% at the 0.18 score live on 2026-08-06, vs an 80% default).
            # That mutation was in-memory only, so every restart handed back
            # the difference — $200,000 of deployment headroom on a $1M pool,
            # in the regime AEGIS had just scored as defensive. A de-risking
            # brake must not be undone by the most common event in the fleet.
            _saved = data.get("limits")
            if isinstance(_saved, dict):
                _restored = {k: v for k, v in _saved.items()
                             if isinstance(v, (int, float))}
                if _restored:
                    # Only the AEGIS-controlled key is dynamic; the rest come
                    # from config so a stale file cannot loosen them.
                    _md = _restored.get("max_deployed_pct")
                    if isinstance(_md, (int, float)) and 0 < _md <= 100:
                        _default = self.limits.get("max_deployed_pct")
                        self.limits = dict(self.limits)
                        self.limits["max_deployed_pct"] = _md
                        if _default is not None and _md < _default:
                            log.info("Restored AEGIS-tightened deployment cap "
                                     "%s%% (config default %s%%)", _md, _default)
            # Restore the cooldown gates. Only entries with a numeric past
            # timestamp are honoured — a future ts would hold the cooldown
            # open indefinitely, which is the SAFE direction here (it blocks
            # entries) but is still nonsense, so it is dropped and the gate
            # simply re-arms from the next open/close.
            _now_ts = time.time()
            for _attr, _key in (("_pair_cooldowns", "pair_cooldowns"),
                                ("_pair_opens", "pair_opens")):
                _saved_cd = data.get(_key)
                if isinstance(_saved_cd, dict):
                    _clean = {}
                    for _p, _v in _saved_cd.items():
                        if (isinstance(_v, dict)
                                and isinstance(_v.get("ts"), (int, float))
                                and 0 < _v["ts"] <= _now_ts):
                            _clean[_p] = _v
                    if _clean:
                        setattr(self, _attr, _clean)
                        log.info("Restored %d %s entr(ies) — the burst-entry "
                                 "gate survives this restart", len(_clean), _key)
            # Restore the raise-hold clock too — see _save. Only a well-formed
            # entry whose timestamp is in the PAST is honoured; a future
            # `since` (clock change, hand-edited file) would grant the raise
            # immediately, so it is discarded and the hold restarts.
            global _aegis_raise_pending
            _pend = data.get("aegis_raise_pending")
            if isinstance(_pend, dict):
                _pl, _ps = _pend.get("limit"), _pend.get("since")
                if (isinstance(_pl, (int, float)) and 0 < _pl <= 100
                        and isinstance(_ps, (int, float))
                        and 0 < _ps <= time.time()):
                    _aegis_raise_pending = {"limit": _pl, "since": _ps}
                    log.info("Restored AEGIS raise-hold: %s%% pending, %.1f "
                             "min elapsed of %d min",
                             _pl, (time.time() - _ps) / 60,
                             _AEGIS_RAISE_HOLD_SEC // 60)
                elif _pend:
                    log.warning("Discarded unusable AEGIS raise-hold state "
                                "%r — hold restarts", _pend)
        except FileNotFoundError:
            pass          # genuinely absent — a real first run
        except Exception as e:
            # The file exists and could not be read. Restoring "nothing" here
            # is the loosest possible state, not a safe one — see the
            # docstring. Arm, preserve the bytes, and say so loudly.
            self.state_unreadable = True
            self.reservations = {}
            log.error(
                "UNREADABLE PORTFOLIO STATE %s: %s — reservations and the "
                "AEGIS cap are UNKNOWN. New reservations are refused until "
                "this is resolved; the file is not overwritten.",
                self.filepath, e)
            try:
                _q = "%s.corrupt_%d" % (self.filepath, int(time.time()))
                os.replace(self.filepath, _q)
                log.error("Preserved unreadable portfolio state as %s", _q)
            except Exception:
                log.error("Could not quarantine %s", self.filepath,
                          exc_info=True)

    def _save(self) -> None:
        """Atomic write to portfolio.json."""
        data = {
            "total": self.total,
            "updated_at": time.time(),
            "reservations": self.reservations,
            "history": self.history[-200:],
            # Durable so an AEGIS-tightened cap survives a restart — see _load.
            "limits": dict(self.limits) if isinstance(self.limits, dict) else {},
            # ...and so does PROGRESS TOWARD RELEASING it. Persisting only the
            # tightening made restarts a one-way ratchet: the cap survived,
            # the raise-hold clock restarted at zero. With restarts every
            # ~15min against a 20min hold, the raise NEVER applied — the pool
            # sat at the DEFENSIVE 30% for hours while AEGIS scored CAUTIOUS
            # (2026-08-13), which is the cap that denied TurtleSue's
            # re-reserve and forced its position closed.
            "aegis_raise_pending": (dict(_aegis_raise_pending)
                                    if isinstance(_aegis_raise_pending, dict)
                                    else {}),
            # Cooldowns are SAFETY state and were memory-only, so every CC
            # restart silently re-opened the burst-entry window the OPEN gate
            # exists to close (it was written for 4xENJ/USD opens in 60s).
            # With restarts as frequent as they are here, the gate was
            # effectively disarmed most of the time — and absent looked
            # exactly like "no pair has traded recently".
            "pair_cooldowns": dict(self._pair_cooldowns),
            "pair_opens": dict(self._pair_opens),
        }
        tmp = self.filepath + ".tmp"
        with open(tmp, "w") as f:
            # allow_nan=False refuses to WRITE a non-finite value rather than
            # emitting the bare `NaN` literal, which is invalid JSON. One such
            # value in history made portfolio.json unreadable by any strict
            # parser and took /api/portfolio and /api/master offline until it
            # was removed by hand. Better to fail the save loudly here than to
            # persist a file that cannot be served.
            json.dump(data, f, indent=2, allow_nan=False)
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
                stop_loss_pct: Optional[float] = None,
                is_reentry: bool = False) -> dict:
        """Validate risk limits and reserve capital. Returns dict with ok/reason.

        is_reentry marks a re-reservation for a position that is already open
        (bot restart re-claiming capital), which bypasses the direction gate.
        """
        from fleet_config import is_blacklisted
        pair = normalize_pair(pair) or pair  # canonical format for per-pair limits
        with self._lock:
            # Unknown state fails toward ARMED. With portfolio.json unreadable
            # the reservation book is UNKNOWN — deployed() would report 0.00
            # and every downstream limit is computed against that, so the
            # whole pool would read as free. Refuse rather than allocate
            # against a number we cannot vouch for. Releases still work, so
            # a bot can always return capital.
            if getattr(self, "state_unreadable", False):
                return {"ok": False, "reason":
                        "Portfolio state unreadable — reservations UNKNOWN. "
                        "New capital refused until the state file is "
                        "restored or removed."}
            # 0a. Per-pair OPEN-time cooldown — blocks burst re-entry on the same pair before
            #     any trade has closed (the CLOSE cooldown can't fire if nothing closed yet).
            #     Catches e.g. 4×ENJ/USD opens in 60 s — entry 1 allowed, entries 2-4 blocked.
            #     Gridzilla exempt — grid bots legitimately open multiple levels on the same pair.
            # OPEN-gate cooldown — prevents burst same-pair entries before the first close.
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
            #     Gridzilla exempt — it manages its own entry frequency internally.
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

            # Prune cooldown/open stamps older than 1 hour to prevent memory growth
            cutoff = time.time() - 3600
            self._pair_cooldowns = {
                p: v for p, v in self._pair_cooldowns.items() if v["ts"] > cutoff
            }
            self._pair_opens = {
                p: v for p, v in self._pair_opens.items() if v["ts"] > cutoff
            }

            # Blacklist check — fleet-wide protection. Pairs are blacklisted for
            # negative expectancy OR for not being tradeable on Kraken at all,
            # so the reason stays generic rather than asserting "0% WR".
            if is_blacklisted(pair):
                return {"ok": False,
                        "reason": f"Pair {pair} is blacklisted (see fleet_config.BLACKLISTED_PAIRS)"}

            # Direction gate — shorts are retired fleet-wide (paper AND live).
            # Reports the specific rule that fired rather than always blaming
            # live mode, so a paper-mode rejection is not misdiagnosed.
            # is_reentry lets a bot re-claim capital for an ALREADY-OPEN short
            # after a restart; without it the bot reads its own position as
            # unfunded and force-closes it at market.
            _dir_ok, _dir_reason = _fleet_config.direction_allowed(
                direction, is_reentry=is_reentry)
            if not _dir_ok:
                log.info("SHORT DENIED: %s requested %s %s — %s",
                         bot_id, direction, pair, _dir_reason)
                return {"ok": False, "reason": _dir_reason}

            # Validate bot is a trading bot
            if bot_id not in TRADING_BOTS:
                return {"ok": False, "reason": f"Bot '{bot_id}' is not a trading bot"}

            deployed = self.deployed()
            lim = self.limits

            # 1. Total deployment limit
            max_deployed = self.total * lim["max_deployed_pct"] / 100
            if deployed + amount > max_deployed:
                avail = max_deployed - deployed
                return {"ok": False, "reason": f"Fleet deployment limit: {deployed:.2f} deployed + {amount:.2f} requested > {max_deployed:.2f} cap ({lim['max_deployed_pct']}% of pool, AEGIS-adjusted). Fleet headroom: {avail:.2f}"}

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

            # 4b. Pool sanity — every limit below is a percentage OF self.total,
            #     and gates 6 and 7 divide by it. self.total is mutated by
            #     `self.total += pnl` on every release, so a catastrophic
            #     drawdown could drive it to zero or negative. Reject cleanly
            #     here rather than raising ZeroDivisionError inside the reserve
            #     path, which would surface to the bot as a transport error
            #     rather than a refusal.
            if self.total <= 0:
                log.error("Pool is %.2f — refusing all reservations", self.total)
                return {"ok": False,
                        "reason": f"Pool exhausted (total ${self.total:.2f}) — no capital to reserve"}

            # 5. Per-trade limit (epsilon prevents float equality rejection at exact boundary)
            #
            # NOTE (2026-08-13 config audit): this gate cannot bind while
            # max_per_trade_pct >= max_per_pair_pct, and both are currently
            # 20%. Gate 3 above tests `pair_exp + amount` (CUMULATIVE) while
            # this tests `amount` alone (SINGLE), and pair_exp >= 0, so the
            # pair gate is always at least as strict. Any single trade this
            # would refuse was already refused there.
            #
            # Left at 20% deliberately: lowering it would tighten a live risk
            # limit and start refusing trades the fleet currently accepts,
            # which is a position-sizing policy decision for the operator,
            # not a silent side effect of an audit. Recorded here so the
            # redundancy is visible rather than mistaken for active defence.
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
            # Scales with the pool by design, unlike the size floor below.
            dir_totals = self.exposure_by_direction()
            dir_after = dir_totals.get(direction, 0) + amount
            if dir_after > self.total * 0.60:
                return {"ok": False, "reason": f"Directional cap: {direction} would be ${dir_after:.0f} ({dir_after/self.total:.0%} of pool, max 60%)"}

            # 8. Size floor — absolute dollars, NOT a fraction of the pool.
            #    The purpose is to filter dust-sized noise entries, and dust is
            #    an absolute concept: $50 is dust whether the pool is $10k or
            #    $1M. As 5%-of-pool this rule silently redefined what counts as
            #    a real trade every time the pool was resized. Setting the pool
            #    to $1,000,000 on 2026-08-05 moved the floor from ~$500 to
            #    $50,000 and blocked gridzilla from every trade it attempted
            #    (141 denials in one hour, all "size floor"). Live fleet
            #    positions run $550-$1,003, so that floor was ~90x larger than
            #    anything any bot actually reserves — the six surviving
            #    reservations only predate the change.
            min_trade = MIN_TRADE_USD
            if amount < min_trade:
                return {"ok": False, "reason": f"Size floor: ${amount:.2f} < ${min_trade:.2f} minimum"}

            # 9. Fleet intelligence gate — check engine risk assessment
            try:
                intel = _fleet_intel.get_score(pair)
                risk_mult = intel.get("risk_multiplier")
                warnings = intel.get("active_warnings", [])
                # An UNSCORED pair now reports None instead of a confident
                # 1.0. Full size is the correct behaviour — this gate only
                # ever reduces, and refusing every unscored pair would halt
                # the fleet whenever Nexus restarts — but it must be a
                # deliberate pass, not a fabricated multiplier, and it must
                # be visible.
                if risk_mult is None:
                    _age = intel.get("stale_s")
                    # WARNING, not info: the log runs above INFO, so an
                    # info-level line here is invisible — which would make
                    # this deliberate pass indistinguishable from a scored
                    # one, the exact ambiguity the change removes.
                    log.warning("Fleet intel has no score for %s (last poll "
                                "%s) — proceeding at full size, unscaled",
                                pair,
                                f"{_age:.0f}s ago" if isinstance(_age, (int, float))
                                else "never")
                    risk_mult = 1.0
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
                # Deliberate availability trade-off: an intel failure must not
                # block trading. But it was SILENT, and the skip fails toward
                # LARGER size — the whole risk-scaling block (the 0.10 hard
                # block, the proportional scaling, the post-scale floor) is
                # bypassed and the amount proceeds unscaled. Note the
                # asymmetry this line fixes: the deliberate full-size pass for
                # an unscored pair logs a WARNING, while this accidental
                # full-size pass logged nothing at all.
                log.warning("Fleet intel gate SKIPPED for %s %s $%.2f — intel "
                            "raised; proceeding UNSCALED", bot_id, pair, amount,
                            exc_info=True)

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

            amount = res["amount"]

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
                # Signal product (2026-07-30): no fees calculated or deducted —
                # subscribers pay their own exchange. Key kept at 0.0 so
                # history readers don't break on a missing field.
                "fees": 0.0,
                "new_total": self.total,
                "timestamp": time.time(),
            })

            self._save()
            return {"ok": True, "released": res["amount"], "pnl": pnl, "new_total": self.total,
                    "available": self.available(), "reservation": res}

    def force_release_stale(self, max_age_hours: int = 24,
                            active_positions: set | None = None,
                            reporting_bots: set | None = None) -> list:
        """Release reservations older than max_age_hours.

        active_positions: {(bot_id_lower, PAIRNOSLASH)} of positions alive
        bots currently self-report. A reservation whose bot still reports the
        position is a LONG-HELD TRADE, not an orphan — age alone cannot tell
        them apart (the 48h cutoff released Confluence's reservations out
        from under two 66h swing positions, 2026-07-30). Pass None to skip
        the check (legacy behavior).

        reporting_bots: {bot_id_lower} of bots that actually answered this
        poll cycle. A bot that did NOT report contributes nothing to
        active_positions, which the check above reads as "holds nothing" —
        absence of a report is not a report of absence. On 2026-08-13 the
        boot-cycle sweep released $25.8k of TurtleSue's XRP reservations 9s
        after launch, while the bot was still starting; the position was
        live with 3 units. Reservations of non-reporting bots are HELD:
        held capital is recoverable, capital released under a live position
        is not. Pass None to skip (legacy behavior).

        Returns list of released reservation dicts (each has bot_id, pair,
        amount, direction, reserved_at). Callers should record these to the
        expectancy tracker so P&L attribution is not lost.
        """
        with self._lock:
            cutoff = time.time() - max_age_hours * 3600
            stale = []
            held_unreported = []
            for rid, r in self.reservations.items():
                if r["reserved_at"] >= cutoff:
                    continue
                if reporting_bots is not None and \
                        str(r["bot_id"]).lower() not in reporting_bots:
                    held_unreported.append(rid)
                    continue  # bot didn't answer — unknown, not orphaned
                if active_positions is not None:
                    # Same derivation as _active_position_keys — see
                    # _position_key. Deriving the two sides differently is
                    # exactly how a live position loses its reservation.
                    key = _position_key(r["bot_id"], r["pair"])
                    if key in active_positions:
                        continue  # bot still holds this position
                stale.append(rid)
            if held_unreported:
                log.warning(
                    "Stale sweep HELD %d aged reservation(s) whose bot did "
                    "not report this cycle: %s — will re-check next sweep",
                    len(held_unreported), ", ".join(held_unreported))
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

    # ── Reservation leases (2026-07-31) ──
    # Bots that adopt the confirm contract declare, once per scan cycle, the
    # exact reservation ids they still reference. Anything booked to a
    # confirming bot that stays undeclared past both grace windows is an
    # orphan (a bot wiring bug stranded it) and gets released. Bots that never
    # confirm are untouched — legacy behavior, still covered by the hourly
    # position-aware 48h sweep. Rid-level precision catches what the
    # (bot, pair) sweep can't: two reservations on one pair where only one is
    # real (the TurtleSue pyramid leak, PR #10).
    LEASE_OPEN_GRACE_SEC = 300   # never judge a reservation younger than this (reserve→store in flight)
    LEASE_MISS_GRACE_SEC = 600   # must stay undeclared this long before release (rides out transient races)

    def confirm(self, bot_id: str, reservation_ids: list,
                active_positions: set | None = None) -> dict:
        """Refresh leases on declared rids; sweep persistently-undeclared ones.

        active_positions: {(bot_id_lower, PAIRNOSLASH)} of positions the fleet
        currently reports, exactly as force_release_stale takes. A reservation
        whose bot STILL REPORTS the position is not an orphan no matter what
        the bot declared — the declaration can be empty because the bot's own
        position store failed to load, which is indistinguishable from a bot
        that genuinely closed everything.

        Without this, an empty confirm from a healthy bot released all of its
        capital after LEASE_MISS_GRACE_SEC while the positions stayed open on
        the venue — verified in isolation: two live reservations totalling
        $2,664.24 swept by a single confirm([]). That is a double-spend, and
        this 10-minute path was the more aggressive of the two sweeps while
        being the only one without the check.

        Returns {ok, confirmed: <owned rids stamped>, swept: [reservation dicts]}.
        """
        declared = set(reservation_ids or [])
        now = time.time()
        confirmed = 0
        swept = []
        protected = 0
        with self._lock:
            self._bot_confirms[bot_id] = now
            for rid, r in list(self.reservations.items()):
                if r.get("bot_id") != bot_id:
                    continue
                if rid in declared:
                    r["last_confirmed_at"] = now
                    r.pop("unconfirmed_since", None)
                    confirmed += 1
                    continue
                # A reservation with NO reserved_at is not a brand-new one.
                # Defaulting to `now` made its age exactly 0, so it sat inside
                # the open grace window on every single sweep and could never
                # expire — capital stranded permanently, and invisibly,
                # because the sweep reported it as merely young.
                #
                # Missing means unknown, and unknown must be sweepable: treat
                # it as old enough to proceed to the confirmation gates below,
                # which still protect anything the bot actually reports
                # holding (see the active_positions check).
                _resv_at = r.get("reserved_at")
                if isinstance(_resv_at, (int, float)) and \
                        now - _resv_at < self.LEASE_OPEN_GRACE_SEC:
                    continue
                if "unconfirmed_since" not in r:
                    r["unconfirmed_since"] = now
                    continue
                if now - r["unconfirmed_since"] < self.LEASE_MISS_GRACE_SEC:
                    continue
                # Last gate: never sweep a reservation whose bot still reports
                # the position. Held capital is recoverable; capital released
                # out from under a live position is not.
                if active_positions is not None:
                    if _position_key(r["bot_id"], r["pair"]) in active_positions:
                        protected += 1
                        log.warning(
                            "Lease sweep SKIPPED %s (%s %s $%.2f) — bot still "
                            "reports this position; an undeclared rid is not "
                            "evidence the position is gone",
                            rid, r["bot_id"], r["pair"], r.get("amount") or 0)
                        continue
                res = self.reservations.pop(rid)
                self.history.append({
                    "action": "lease_sweep",
                    "reservation_id": rid,
                    "bot_id": res["bot_id"],
                    "pair": res["pair"],
                    "amount": res["amount"],
                    "reason": "owner stopped declaring this reservation",
                    "timestamp": now,
                })
                swept.append({"reservation_id": rid, **res})
            if swept:
                self._save()
        return {"ok": True, "confirmed": confirmed, "swept": swept}

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

            # Same adaptive cooldown the reserve() gate applies — the dashboard
            # must not show a pair as cooling after the gate has released it.
            cooldown_secs = (
                self.PAIR_COOLDOWN_AGGRESSIVE
                if self.aegis_score > 0.7
                else self.PAIR_COOLDOWN_SECS
            )

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
                        "remaining_secs": round(cooldown_secs - (time.time() - v["ts"])),
                        "last_bot": v["bot_id"],
                    }
                    for pair, v in self._pair_cooldowns.items()
                    if time.time() - v["ts"] < cooldown_secs
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
    """Serialize to JSON string, safe for inf/nan/numpy.

    allow_nan=False RAISES on a non-finite native float rather than emitting
    the invalid `NaN` literal — and json.dumps never consults `default` for
    native floats (see _sanitize), so nothing intercepted them. A single NaN
    reaching this function therefore produced an exception, an empty response
    body, and a dead endpoint: /api/portfolio and /api/master both stopped
    serving on 2026-08-06 for exactly this reason, taking the whole fleet's
    polling with them.

    Sanitizing on the retry keeps the endpoint alive and makes the bad value
    visible as a null instead of silently substituting a plausible number.
    """
    try:
        return json.dumps(obj, default=_json_default, allow_nan=False)
    except ValueError:
        log.error("Non-finite value in a JSON response — serving sanitized "
                  "output. This is a data bug upstream, not a display issue.",
                  exc_info=True)
        return json.dumps(_nullify_nonfinite(obj), default=_json_default,
                          allow_nan=False)


def _nullify_nonfinite(obj: Any) -> Any:
    """Replace non-finite floats with None — NOT 0.0.

    _sanitize() substitutes 0.0, which is right at the bot-ingestion boundary
    where a broken sensor reading should not stall the poll. It is wrong here:
    a 0.0 on an outbound API reads as a measured zero. None reads as unknown,
    which is what it is.
    """
    if isinstance(obj, dict):
        return {k: _nullify_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_nullify_nonfinite(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


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

        # Drop blacklisted pairs at the SOURCE.
        #
        # The reserve path already refuses them (gate 1), so no capital was
        # ever at risk — but the universe is what every bot SCANS, so a
        # blacklisted pair still consumed scan cycles, OHLC fetches and
        # Brainiac depth/trade collection for a pair the fleet can never
        # trade. Observed 2026-08-13: SOL/USD was blacklisted and still
        # served in all 50 universe entries, with zero denials on the bus
        # because nothing ever got far enough to be refused.
        #
        # Note this also covers `pin`: a pinned pair that is blacklisted
        # must stay out, or the pin would reintroduce it below.
        try:
            from fleet_config import is_blacklisted as _is_bl
            _before = len(qualified)
            qualified = [p for p in qualified if not _is_bl(p["display"])]
            if len(qualified) != _before:
                log.info("Universe: dropped %d blacklisted pair(s)",
                         _before - len(qualified))
        except Exception:
            # Never let a blacklist import failure empty the universe —
            # the reserve gate is still the binding enforcement.
            log.warning("Universe: blacklist filter unavailable, serving "
                        "unfiltered (reserve gate still enforces)",
                        exc_info=True)

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

    if len(bot.get("endpoints") or []) > 1:
        # Multi-endpoint bots: merge each endpoint under its trailing path
        # segment (e.g. /health -> "health"). TrekBot was the only such bot and
        # has left the fleet, but the branch is kept generic rather than
        # hardcoded to an id so a future multi-endpoint bot works unchanged.
        merged = {}
        total_latency = 0.0
        for ep in bot["endpoints"]:
            key = ep.strip("/").split("/")[-1]
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
        # SN-2: named high_conviction_pairs (a LIST) because Sentinel's own
        # raw snapshot emits "high_conviction" as a COUNT int — same name
        # carrying two types across API surfaces confused consumers. No
        # consumer of normalized.high_conviction existed at rename time
        # (dashboard reads raw.high_conviction from /api/bot/sentinel).
        "high_conviction_pairs": high_conv,
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


def _normalize_confluence(raw: dict) -> dict:
    """Confluence (port 8088) — intel-driven trader that replaced TrekBot.

    Serves a flat snapshot, so most fields map straight across. `regime` is
    INTEL_DRIVEN when its four upstream intel sources are reachable and
    DEGRADED when they are not, so it doubles as an intel-health indicator.
    """
    return {
        "equity": raw.get("equity"),
        "pnl": raw.get("pnl"),
        "pnl_pct": raw.get("pnl_pct"),
        "win_rate": raw.get("win_rate"),
        "drawdown_pct": None,
        "sharpe": None,
        "open_positions": raw.get("open_positions", 0),
        "total_trades": raw.get("total_trades", 0),
        "regime": raw.get("regime"),
        "signals_count": raw.get("signals_count"),
        "uptime": raw.get("uptime"),
        # Confluence-specific extras
        # fees_paid: legacy field — bots are dropping fee reporting (signal
        # product, gross P/L). Default 0 so absence never breaks consumers.
        "fees_paid": raw.get("fees_paid", 0) or 0,
        "intel_status": raw.get("intel_status"),
        "candidates": raw.get("candidates"),
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
    # Trade count and win rate MUST come from the same source. total_trades
    # used to fall back to total_cycles while win_rate stayed with the
    # expectancy block, so a gridzilla with 1 completed cycle and no
    # expectancy history reported "1 trade, 0.0% win rate" — a winning
    # +$34.90 cycle rendered as a total loss. Two fields, two sources, shown
    # as one measurement.
    _exp_trades = exp.get("total_trades")
    _decided = None
    if _exp_trades:
        total_trades = _exp_trades
        wr = exp.get("win_rate")
        # win_rate is computed over DECIDED trades (wins+losses); total_trades
        # counts flats too. Carry the decided count so the fleet aggregate can
        # use the rate's real denominator instead of multiplying the rate by a
        # larger population. Live: 100% over 16 decided, 17 flat, 33 total --
        # 100% x 33 fabricates 17 wins that do not exist.
        _decided = exp.get("decided")
        if _decided is None:
            _w, _l = exp.get("wins"), exp.get("losses")
            if isinstance(_w, int) and isinstance(_l, int):
                _decided = _w + _l
    else:
        # Expectancy has nothing; fall back to the cycle counter for the
        # count and report the rate as unmeasured rather than borrowing a
        # zero from an empty block.
        total_trades = raw.get("total_cycles", 0)
        wr = None
    return {
        "equity": None,
        "pnl": float(pnl) if pnl is not None else None,
        "pnl_pct": None,
        "win_rate": float(wr) if wr is not None else None,
        "win_rate_decided": _decided,
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


def _normalize_inference(raw: dict) -> dict:
    """Inference server (port 9001) — polls /health only.

    /health payload: {"status": "running", "model": "<ollama model>", "port": 9001}.
    Model info already rides on /health, so no extra endpoint is needed.
    """
    return {
        "equity": None, "pnl": None, "pnl_pct": None,
        "win_rate": None, "drawdown_pct": None, "sharpe": None,
        "open_positions": None, "total_trades": None,
        "regime": None, "signals_count": None, "uptime": None,
        # Inference specifics
        "status": raw.get("status"),
        "model": raw.get("model"),
    }


def _normalize_broadcaster(raw: dict) -> dict:
    """Signal broadcaster (port 9002) — polls /health + /stats.

    With two endpoints in fleet_config, _fetch_bot's multi-endpoint branch
    merges them key-namespaced: {"health": {...}, "stats": {...}}. The flat
    single-endpoint shape (health fields at top level) is tolerated too so a
    stale fleet_config can't blank the tile.

    /health: {status, uptime, sse_connected}
    /stats:  {sse:{connected,reconnects,events_received,last_event_ts},
              polling:{...}, channel:{free_sent_today,paid_sent_today,
              failed_today,...}, config:{...}, delayed_queue_size,
              signals_log_size}
    NOTE: these are the LIVE process stats — CC's /api/signals/broadcaster/stats
    route is log-derived and can disagree; this normalizer is the live truth.
    """
    health = raw.get("health") if isinstance(raw.get("health"), dict) else raw
    stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    sse = stats.get("sse") or {}
    channel = stats.get("channel") or {}
    free_sent = channel.get("free_sent_today") or 0
    paid_sent = channel.get("paid_sent_today") or 0
    return {
        "equity": None, "pnl": None, "pnl_pct": None,
        "win_rate": None, "drawdown_pct": None, "sharpe": None,
        "open_positions": None, "total_trades": None,
        "regime": None,
        "signals_count": (free_sent + paid_sent) if channel else None,
        "uptime": health.get("uptime"),
        # Broadcaster specifics (live 9002 truth)
        "status": health.get("status"),
        "sse_connected": health.get("sse_connected", sse.get("connected")),
        "sse_reconnects": sse.get("reconnects"),
        "events_received": sse.get("events_received"),
        "free_sent_today": free_sent if channel else None,
        "paid_sent_today": paid_sent if channel else None,
        "failed_today": channel.get("failed_today"),
        "delayed_queue_size": stats.get("delayed_queue_size"),
        "signals_log_size": stats.get("signals_log_size"),
    }


_NORMALIZERS = {
    "turtlesue":  _normalize_turtlesue,
    "sentinel":   _normalize_sentinel,
    "trinity":    _normalize_trinity,
    "hivemind":   _normalize_hivemind,
    "nexusbrain": _normalize_nexusbrain,
    "confluence": _normalize_confluence,
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
        # None, not 50 — the neighbours above already do this. A default of
        # 50 is a MEASUREMENT ("perfectly neutral market"), and it actively
        # defeated the one defence that existed: Contrarian deliberately
        # serves fear_greed None when its source is unreachable, and this
        # line converted that honesty back into a confident reading.
        "sentiment_score": raw.get("sentiment_score"),
        "fear_greed": raw.get("fear_greed"),
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
        # None, not 50 — same rule. 50 reads as "an even hour", measured.
        "hour_bias": raw.get("hour_bias"),
        "active_sessions": raw.get("active_sessions", []),
        "uptime": None,
    },
    "inference":          _normalize_inference,
    "signal_broadcaster": _normalize_broadcaster,
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


def _trader_ids() -> set:
    """Bot ids whose wins/losses are actual TRADES.

    The fleet win rate must be computed over traders only. Intel and support
    bots publish signal-resolution counters that some normalizers map onto
    win_rate/total_trades -- trinity (role="support", an intel-only scanner)
    was contributing "2 trades at 100%" to the fleet TRADING win rate.

    Falls back to the empty set if the registry is unreadable; callers treat
    that as "cannot attribute", which yields no win rate rather than a wrong
    one computed over everybody.
    """
    try:
        return set(_fleet_config.get_traders().keys())
    except Exception:
        log.warning("fleet_config.get_traders() unavailable; fleet win rate "
                    "will report unmeasured rather than aggregate non-traders")
        return set()


_TRADER_IDS = _trader_ids()


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

# The pool was resized $1,000,000 -> $210.53 on 2026-08-13. Bot P/L ledgers
# are LIFETIME and cross that boundary, so summing them adds two different
# pools' worth of dollars under one label. Live example, 2026-08-26:
# confluence publishes pnl = -362.76, which is dominated by pre-resize trades
# on notionals of $38k-$49k (FLOW -1216.75, STORJ -567.06, XMR -581.30,
# ETHFI +3111.19) against a pool that is now $215. Re-derived from the durable
# bus, the SAME bot's post-resize record is +5.01 over 14 closed trades.
#
# The sum is not wrong arithmetic — it is an honest sum of inputs that mean
# different things. This constant exists so the boundary is a value the code
# can point at rather than a date repeated in ten comments, and so the
# aggregate can DISCLOSE the crossing instead of quietly publishing it.
# 2026-08-13 21:00:00 UTC — MEASURED from the trade record, not assumed from
# the calendar date. Position notionals break cleanly and permanently there:
#
#     08-13 19:22  confluence ETHFI/USD  +3111.19  size $38,557.56   <- old
#     08-13 21:42  confluence CRV/USD       -0.01  size      $6.80   <- new
#
# and nothing after that point is ever sized above ~$14 again. The boundary
# sits inside the 2h20m gap between those two closes.
#
# Two wrong values were written here first, and both failed the same way —
# quietly, by shifting which trades count:
#   1786924800.0  = 2026-08-17 UTC, four days LATE — dropped 7 real trades.
#   1786579200.0  = 2026-08-13 00:00 UTC, ~21h EARLY — admitted 12 old-pool
#                   trades and published +$939 on a $215 pool, with a $300
#                   average win. A calendar midnight is a guess; the notional
#                   break is evidence.
#
# Verify any change here with time.gmtime(), and re-check it against the
# notional column rather than trusting the arithmetic.
POOL_RESIZE_TS = 1786654800.0
POOL_RESIZE_NOTE = ("bot P/L ledgers are lifetime and cross the 2026-08-13 "
                    "pool resize ($1,000,000 -> $210.53); pre-resize trades "
                    "were sized against the old pool")


def _compute_aggregate(bots_data: dict) -> dict:
    """Compute fleet-wide aggregate from all normalized bot data."""
    alive_count = sum(1 for b in bots_data.values() if b.get("alive"))
    norms = {bid: b["normalized"] for bid, b in bots_data.items() if b.get("normalized")}

    def _collect(key):
        return [(bid, n[key]) for bid, n in norms.items() if n.get(key) is not None]

    # TRADERS ONLY for anything that claims to count trading activity.
    #
    # _TRADER_IDS already gated the win rate (see the long note below), and
    # the sibling fields were missed — the fix-one-sibling-miss-the-others
    # shape this project keeps hitting. Observed live 2026-08-26 16:23, minutes
    # after a watchdog restart:
    #
    #     aggregate.total_open_positions   10
    #     portfolio.active_reservations     3
    #
    # The 7-position gap is trinity, which is role="support" — an intel-only
    # scanner holding NO pool capital. Its normalizer maps signal tracks onto
    # open_positions/total_trades, so the fleet appeared to hold 10 positions
    # while the portfolio (the only thing that reserves real capital) held 3.
    #
    # A signal being tracked is not a position being held. The pool is the
    # authority on what is open; a bot that cannot reserve cannot contribute.
    def _collect_traders(key):
        return [(bid, n[key]) for bid, n in norms.items()
                if bid in _TRADER_IDS and n.get(key) is not None]

    equities = _collect("equity")
    # pnl is traders-only for the same reason, though no support bot publishes
    # one today (checked live 2026-08-26: all six contributors are traders).
    # Guarding it now rather than after a normalizer change makes it leak —
    # trinity already leaks through open_positions by exactly that route.
    pnls = _collect_traders("pnl")
    open_pos = _collect_traders("open_positions")
    trades = _collect_traders("total_trades")
    regimes = [n["regime"] for n in norms.values() if n.get("regime")]

    # Global fleet WR = total_wins / total DECIDED trades, over TRADERS only.
    # Reconstruct wins from (win_rate, decided_n) per bot and sum. Bots with
    # zero trades contribute 0 wins AND 0 to the denominator, so they drop out
    # cleanly. This is the only aggregation that means what "fleet WR" says.
    #
    # Two corrections, both of which inflated the published figure:
    #
    # 1. THE DENOMINATOR MUST BE THE RATE'S OWN POPULATION. A bot's win_rate is
    #    computed over DECIDED trades (wins+losses); its total_trades counts
    #    flats as well. Multiplying the rate by the larger count invents wins.
    #    Live 2026-08-25: gridzilla reported win_rate 100.0 with total_trades
    #    33, of which 16 were decided and 17 were FLAT ($0.00 moves). 100% x 33
    #    credited it 33 wins when it had 16 -- 17 fabricated wins, on their own
    #    enough to carry the fleet figure. Prefer win_rate_decided when the bot
    #    publishes it; fall back to total_trades only when it does not, and
    #    record that fallback so the aggregate can disclose it.
    #
    # 2. ONLY BOTS THAT ACTUALLY TRADE MAY COUNT. trinity is role="support" --
    #    an intel-only scanner. Its wins/losses are SIGNAL TRACK outcomes, not
    #    trades, and its normalizer maps them onto win_rate/total_trades, so it
    #    injected "2 trades at 100%" into the fleet TRADING win rate. A signal
    #    that resolved favourably is not a trade that made money.
    _total_wins = 0
    _total_trades_for_wr = 0
    _wr_exact = True   # False once any bot's denominator had to be assumed
    _wr_bots = 0
    for bid, n in norms.items():
        if bid not in _TRADER_IDS:
            continue
        wr = n.get("win_rate")
        tc = n.get("total_trades") or 0
        if wr is None or tc <= 0:
            continue
        # Normalize scale via win_rate_scale, the ONE rule (2026-08-26).
        # Bots publish on two scales and nothing in the payload says which;
        # this guess used to be reimplemented here, in ultron and in the
        # broadcaster, with the broadcaster converting the opposite way.
        wr_pct = to_percent(wr)
        if wr_pct is None:
            continue
        _dec = n.get("win_rate_decided")
        if isinstance(_dec, int) and _dec >= 0:
            denom = _dec
        else:
            # The bot does not publish a decided count. total_trades is the
            # only denominator available; it is exact only if nothing was
            # flat, which cannot be confirmed from here.
            denom = tc
            _wr_exact = False
        if denom <= 0:
            # Decided nothing -- UNMEASURED, not 0%. Contributes to neither
            # numerator nor denominator (turtlesue: 5 trades, all flat).
            continue
        _total_wins += round(wr_pct / 100.0 * denom)
        _total_trades_for_wr += denom
        _wr_bots += 1
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
        # No fleet "equity" figure. There is ONE pool (portfolio.total) and no
        # bot holds capital of its own, so there is nothing to sum. This used
        # to add up five bots' internal balances -- each seeded from a fixed
        # $10,000 -- and published $49,631.58 against a $209.88 pool, which
        # three dashboard sites then rendered as the POOL. The bot 'equity'
        # fields are now zero-based realized-P/L ledgers; summing those gives
        # fleet P/L, which is already reported as total_pnl below.
        "total_equity": None,
        "total_pnl": sum(v for _, v in pnls) if pnls else None,
        # total_pnl sums LIFETIME bot ledgers, which cross the 2026-08-13 pool
        # resize. Publishing a single number that spans two pool sizes without
        # saying so is how -362.76 reads as "the fleet is down $363" when the
        # post-resize record is +5.01. Consumers showing total_pnl MUST show
        # this caveat beside it.
        "total_pnl_spans_pool_resize": True,
        "total_pnl_note": POOL_RESIZE_NOTE,
        "pool_resize_ts": POOL_RESIZE_TS,
        # Global WR = total_wins / total DECIDED trades, traders only.
        # NOTE the denominator is win_rate_n, NOT total_trades: the two count
        # different populations and pairing the rate with total_trades was the
        # defect this replaces. Any consumer showing "(n=...)" beside this rate
        # MUST use win_rate_n.
        "avg_win_rate": global_wr,
        "win_rate_n": _total_trades_for_wr if _total_trades_for_wr > 0 else None,
        "win_rate_wins": _total_wins if _total_trades_for_wr > 0 else None,
        "win_rate_bots": _wr_bots if _total_trades_for_wr > 0 else None,
        # False when at least one bot published no decided count and its
        # total_trades had to stand in, so the n may include flats. The
        # dashboard discloses this rather than presenting an assumed
        # denominator as an exact one.
        "win_rate_n_exact": _wr_exact if _total_trades_for_wr > 0 else None,
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


def _alert_feed_entries(raw: dict, bot_id: str, entries: list) -> None:
    """Append type/suggestion-shaped recent_alerts (Contrarian, Chronos)."""
    name, color = _bot_meta(bot_id)
    for alert in (raw.get("recent_alerts") or [])[-10:]:
        atype = alert.get("type", "")
        suggestion = alert.get("suggestion", "")
        msg = f"{atype}: {suggestion}" if suggestion else atype
        # None, not now(). Stamping render time onto an entry whose real
        # time is unknown makes it sort as the newest thing in the feed --
        # and with a dead bot's payload carried forward each cycle, the same
        # stale signals reappear at the top forever, looking fresh.
        _at = alert.get("timestamp")
        entries.append({"time": _at if isinstance(_at, (int, float)) else None,
                        "bot_id": bot_id, "bot_name": name, "message": msg,
                        "bot_color": color})


def _extract_feed(bots_data: dict) -> list[dict]:
    """Collect and merge signals/alerts from all bots into a unified feed."""
    entries = []

    # TurtleSue signals (cap per-bot to avoid feed flooding)
    ts_raw = (bots_data.get("turtlesue") or {}).get("raw") or {}
    name, color = _bot_meta("turtlesue")
    for sig in (ts_raw.get("signals") or [])[-10:]:
        t = sig.get("time") or sig.get("timestamp") or 0
        msg = f"{sig.get('action', '')} — {sig.get('name', '')}".strip(" —")
        entries.append({"time": t, "bot_id": "turtlesue", "bot_name": name, "message": msg, "bot_color": color})

    # Trinity alerts (timestamp may be a string like "23:34:49" or an epoch)
    tr_raw = (bots_data.get("trinity") or {}).get("raw") or {}
    name, color = _bot_meta("trinity")
    for alert in (tr_raw.get("alerts") or [])[-10:]:
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
        sym = sig.get("symbol", "")
        conf = sig.get("confluence", 0)
        bias = sig.get("bias", "")
        regime = sig.get("regime", "")
        # Unknown stays unknown -- see the note in _alert_feed_entries.
        t = sig.get("timestamp")
        if not isinstance(t, (int, float)):
            t = None
        if bias and bias != "NEUTRAL":
            msg = f"{bias} {sym} conf={conf:.2f} regime={regime}"
            entries.append({"time": t, "bot_id": "trinity", "bot_name": name, "message": msg, "bot_color": color})

    # NexusBrain recent_signals -- format as compact one-liners
    nb_raw = (bots_data.get("nexusbrain") or {}).get("raw") or {}
    name, color = _bot_meta("nexusbrain")
    for sig in (nb_raw.get("recent_signals") or [])[-10:]:
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

    # Confluence candidates — each carries a plain-English thesis naming the
    # intel sources that agreed, which reads better in the feed than a raw
    # signal list. (Replaces the old TrekBot recent_signals block.)
    cf_raw = (bots_data.get("confluence") or {}).get("raw") or {}
    cf_ts = cf_raw.get("timestamp") or 0
    name, color = _bot_meta("confluence")
    for cand in (cf_raw.get("candidates") or [])[:10]:
        srcs = ", ".join(cand.get("sources") or [])
        msg = (f"{cand.get('pair')} conf {cand.get('confluence', 0):.2f} "
               f"[{srcs}] — {cand.get('thesis', '')}")
        entries.append({"time": cf_ts, "bot_id": "confluence", "bot_name": name,
                        "message": msg, "bot_color": color})

    # Rubberband recent trades
    rb_raw = (bots_data.get("rubberband") or {}).get("raw") or {}
    name, color = _bot_meta("rubberband")
    for t in (rb_raw.get("recent_trades") or [])[-10:]:
        pnl = t.get("pnl", 0)
        result = "WIN" if pnl > 0 else "LOSS"
        msg = f"{result} {t.get('direction', '')} {t.get('pair', '')} ${pnl:+.2f} [{t.get('exit_reason', '')}]"
        # None, not now(): a trade with no close time has not just
        # closed. See the note in _alert_feed_entries.
        _ct = t.get("closed_at")
        entries.append({"time": _ct if isinstance(_ct, (int, float)) else None,
                        "bot_id": "rubberband", "bot_name": name,
                        "message": msg, "bot_color": color})

    # Contrarian sentiment alerts
    _alert_feed_entries((bots_data.get("contrarian") or {}).get("raw") or {}, "contrarian", entries)

    # Arbitrageur spread events
    ab_raw = (bots_data.get("arbitrageur") or {}).get("raw") or {}
    name, color = _bot_meta("arbitrageur")
    for t in (ab_raw.get("recent_trades") or [])[-10:]:
        pnl = t.get("pnl", 0)
        result = "WIN" if pnl > 0 else "LOSS"
        msg = f"SPREAD {result} {t.get('pair_key', '')} z={t.get('entry_z', 0):.1f}->{t.get('exit_z', 0):.1f} ${pnl:+.2f} [{t.get('reason', '')}]"
        # None, not now(): a trade with no close time has not just
        # closed. See the note in _alert_feed_entries.
        _ct = t.get("closed_at")
        entries.append({"time": _ct if isinstance(_ct, (int, float)) else None,
                        "bot_id": "arbitrageur", "bot_name": name,
                        "message": msg, "bot_color": color})

    # Chronos session alerts
    _alert_feed_entries((bots_data.get("chronos") or {}).get("raw") or {}, "chronos", entries)

    # Oracle top signals
    or_raw = (bots_data.get("oracle") or {}).get("raw") or {}
    name, color = _bot_meta("oracle")
    for sig in (or_raw.get("top_signals") or [])[:5]:
        pair = sig.get("pair", "")
        direction = sig.get("direction", "NEUTRAL")
        strategy = sig.get("strategy", "")
        score = sig.get("score", 0)
        rr = sig.get("rr")
        rr_str = f" R:R={rr:.1f}" if isinstance(rr, (int, float)) and rr else ""
        if direction != "NEUTRAL" and pair:
            msg = f"{direction} {pair} [{strategy}] score={score:.1f}{rr_str}"
            _ot = or_raw.get("timestamp")
            entries.append({"time": _ot if isinstance(_ot, (int, float)) else None,
                            "bot_id": "oracle", "bot_name": name,
                            "message": msg, "bot_color": color})

    # Sort newest first, cap at MAX_FEED_SIZE
    # Undated entries sort to the BOTTOM (not the top, which is where a
    # fabricated now() put them). -inf keeps the comparison total.
    entries.sort(key=lambda e: (e.get("time")
                                if isinstance(e.get("time"), (int, float))
                                else float("-inf")), reverse=True)
    return entries[:MAX_FEED_SIZE]


# ---------------------------------------------------------------------------
# UPGRADE: Fleet Intelligence — Computation Functions
# ---------------------------------------------------------------------------

# --- A. Fleet-Level Factor Exposure Monitor ---

def _compute_factor_exposure(bots_data: dict) -> dict:
    """Aggregate factor scores across all active positions from bots that report them.

    Reads factor data from NexusBrain (open_positions),
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
        bot_factors: dict[str, list[float]] = {}

        # Confluence: no 6-factor model of its own — its per-source weights are
        # the analogue, exposed as candidate `confluence` scores rather than
        # named factors. Counted for position totals only.
        # (TrekBot's factor block was removed with the bot; it had the only
        # per-position "factors" dict in the fleet.)
        if bid == "confluence":
            total_positions += len(raw.get("positions") or [])

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

        # Compute correlation matrix. Snapshot the counts while still holding
        # the lock — the poll thread mutates _bot_pnl_history between cycles.
        bots_with_data = {
            bid: vals for bid, vals in _bot_pnl_history.items()
            if len(vals) >= MIN_SAMPLES
        }
        bots_tracked = len(_bot_pnl_history)
        samples_collected = {bid: len(vals) for bid, vals in _bot_pnl_history.items()}

    if len(bots_with_data) < 2:
        result = {
            "timestamp": time.time(),
            "status": "insufficient_data",
            "bots_tracked": bots_tracked,
            "min_samples_required": MIN_SAMPLES,
            "samples_collected": samples_collected,
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

            # Pearson correlation on deltas.
            mean_a = sum(da) / len(da)
            mean_b = sum(db) / len(db)
            denom_a = math.sqrt(sum((x - mean_a) ** 2 for x in da))
            denom_b = math.sqrt(sum((x - mean_b) ** 2 for x in db))

            pair_key = f"{bid_a}|{bid_b}"

            # A constant P/L stream has zero variance, and Pearson of a
            # zero-variance series is UNDEFINED (0/0) — not zero. Most bots
            # report an unchanged pnl between closes, so the old
            # `+ 1e-10` epsilon quietly turned every undefined pair into
            # "0.0000": a full matrix of confident "uncorrelated"
            # measurements computed from series that never moved. Live proof:
            # 15/15 pairs read exactly 0.0 while four of six bots had a
            # constant 0.0 pnl. Undefined must render as absent, or the
            # concentration-risk check reads "measured and safe" forever.
            if denom_a < 1e-9 or denom_b < 1e-9:
                matrix[pair_key] = None
                continue

            num = sum((da[k] - mean_a) * (db[k] - mean_b) for k in range(len(da)))
            corr = num / (denom_a * denom_b)
            corr = max(-1.0, min(1.0, corr))  # clamp to [-1, 1]
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

    _measured_pairs = sum(1 for v in matrix.values() if v is not None)
    _undefined_pairs = sum(1 for v in matrix.values() if v is None)
    result = {
        "timestamp": time.time(),
        "status": "active",
        "bots_tracked": bots_tracked,
        "bots_correlated": len(bot_ids),
        "samples_collected": samples_collected,
        "matrix": matrix,
        # Say how much of the matrix is real. concentration_risk=False over
        # a matrix that is entirely undefined is "no risk DETECTED", which
        # is not "no risk" — the note makes that readable.
        "pairs_measured": _measured_pairs,
        "pairs_undefined": _undefined_pairs,
        "coverage_note": (None if _measured_pairs else
                          "no pair had two moving P/L streams — correlation "
                          "is unmeasurable until bots realize P/L changes"),
        "alerts": alerts,
        "concentration_risk": len(alerts) > 0,
    }

    with _bot_correlation_lock:
        _bot_correlation_matrix = result
        _bot_correlation_alerts = alerts

    return result


# --- C. Automated Performance Attribution ---

def _compute_performance_attribution(bots_data: dict) -> dict:
    """Decompose fleet PnL into Alpha (signal quality) and Beta (market exposure).

    Alpha = PnL from position selection (excess return above market)
    Beta  = PnL attributable to market direction (what a passive holder would earn)
    Cost  = always 0.0 — signal product (2026-07-30): the fleet models no
            execution costs; subscribers pay their own exchange's fees.
            The key is kept in the output shape for consumers.

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

    # Cost: always 0 — signal product, no execution costs modeled.
    # P/L is gross price movement; subscribers pay their own venue's fees.
    estimated_cost = 0.0

    # Alpha = total PnL - beta (no cost adjustment in gross semantics)
    alpha_pnl = fleet_total_pnl - beta_pnl

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
            "cost": "Always 0 — gross signal P/L; subscribers pay their own exchange's fees",
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

            with _bot_correlation_lock:
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

# Per-bot freshness budget in seconds: how old a bot's OWN snapshot timestamp
# may be before we call its data stale. CC polls every POLL_INTERVAL (10s), but
# several bots legitimately recompute far more slowly — flagging those against
# the poll cadence would be a permanent false positive.
#
#   hivemind  — REOPTIMIZE_INTERVAL = 1800s (cli.py), so ~30min is by design
#   oracle    — full universe scan, minutes per cycle
#   sentinel  — forecast horizon recompute
#   nexus     — 14-engine council pass
# Anything unlisted is expected to refresh within DEFAULT_FRESHNESS_S.
DEFAULT_FRESHNESS_S = 120
_FRESHNESS_BUDGET_S = {
    "hivemind": 2100,      # 1800s cadence + 300s slack
    "oracle": 900,
    "sentinel": 900,
    "nexus": 900,
    "deepblue": 900,
    "phitex": 600,
    "chronos": 600,
}


def _snapshot_staleness(bid: str, raw: dict) -> tuple:
    """Return (age_seconds, is_stale) for a bot's own snapshot timestamp.

    Returns (None, False) when the bot serves no timestamp — absence of the
    field is not evidence of staleness, and guessing would be worse than
    reporting nothing.
    """
    if not isinstance(raw, dict):
        return None, False
    ts = raw.get("timestamp")
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None, False
    if ts <= 0:
        return None, False
    age = time.time() - ts
    if age < 0:                      # clock skew between processes
        age = 0.0
    budget = _FRESHNESS_BUDGET_S.get(bid, DEFAULT_FRESHNESS_S)
    return round(age, 1), age > budget


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
            # HM-4: regime casing is bot-specific at the source ("bull" vs
            # "RANGE" vs "Ranging"). Uppercase once here — the single shared
            # post-step — so consensus counting, the REGIME SOURCES panel and
            # the dashboard compare like against like. Casing only; the label
            # text and every other normalized value are untouched.
            _rg = normalized.get("regime")
            if isinstance(_rg, str):
                normalized["regime"] = _rg.upper()
            # win_rate has no canonical scale at the source: turtlesue reports
            # 0-100, nexusbrain 0-1 (its normalizer multiplies by 100), and
            # five other normalizers pass through whatever the bot sent. The
            # dashboard then re-derives the scale ad hoc in three places and
            # not at all in five others, so a 60% rate can render as "0.6%".
            #
            # Publish ONE scale — percent, 0-100 — so no consumer has to
            # guess. The <=1.01 test is the same heuristic the existing
            # client-side guards use; it is imperfect for a genuine sub-1%
            # win rate, but a bot posting 0.008 for 0.8% is not a case that
            # occurs here and the alternative is five inconsistent guesses.
            _wr = normalized.get("win_rate")
            if isinstance(_wr, (int, float)) and 0 < _wr <= 1.01:
                normalized["win_rate"] = _wr * 100.0
                normalized["win_rate_scale"] = "pct_from_fraction"
            elif isinstance(_wr, (int, float)):
                normalized["win_rate_scale"] = "pct"
            # Zero trades is not a zero win rate — the third shared post-step,
            # same rationale as the two around it: once here rather than in 13
            # normalizers, four of which were already passing this through.
            #
            # turtlesue, rubberband, arbitrageur and nexusbrain all publish
            # win_rate: 0.0 alongside total_trades: 0. The normalizers were
            # right to pass that on faithfully, but "0% of nothing" renders on
            # the dashboard as a bot that loses every trade. None means "not
            # measured" and the UI already handles it — every other bot with
            # no trade tracking reports None here.
            if not normalized.get("total_trades"):
                if normalized.get("win_rate") == 0:
                    normalized["win_rate"] = None
                # Same for the derived ratios: a profit factor or expectancy
                # of 0.0 from zero samples is a fabricated measurement.
                for _k in ("profit_factor", "expectancy_r", "sharpe"):
                    if normalized.get(_k) == 0:
                        normalized[_k] = None
            # Positions passthrough — the second shared post-step, same
            # rationale as the regime casing above: do it once here rather
            # than in 18 separate normalizers.
            #
            # No normalizer emitted `positions`, so `normalized.positions` was
            # undefined for every bot. The dashboard's ACTIVE POSITIONS panel
            # has a fallback that reads exactly that field to fill its UNRL
            # P/L column, so the column rendered "—" permanently while the
            # data sat in the raw snapshot (TurtleSue publishes
            # unrealized_pnl directly; -67.84 at the time of writing).
            #
            # Bots use three different shapes: dict keyed by pair (turtlesue),
            # list of dicts (confluence), and absent (gridzilla). Normalise to
            # a list of dicts each carrying `pair`, so one consumer shape
            # works for all. Nothing is invented — a bot that reports no
            # unrealized_pnl simply carries None and the panel keeps showing
            # "—" for it, which is honest.
            if "positions" not in normalized:
                _rawpos = raw.get("positions")
                _plist = None
                if isinstance(_rawpos, dict):
                    _plist = []
                    for _pk, _pv in _rawpos.items():
                        if isinstance(_pv, dict):
                            _entry = dict(_pv)
                            _entry.setdefault("pair", _pv.get("name") or _pk)
                            _plist.append(_entry)
                elif isinstance(_rawpos, list):
                    _plist = [p for p in _rawpos if isinstance(p, dict)]
                if _plist is not None:
                    normalized["positions"] = _plist
            stale_age, is_stale = _snapshot_staleness(bid, raw)
            new_bots[bid] = {
                "id": bid, "name": bot["name"], "port": bot["port"],
                "color": bot["color"], "alive": True,
                "last_seen": time.time(), "latency_ms": round(latency, 1),
                # A bot answering HTTP is not the same as a bot serving fresh
                # data. Without this, a bot whose internal refresh has stalled
                # reads as fully healthy forever.
                "data_stale": is_stale, "stale_age_s": stale_age,
                "raw": raw, "normalized": normalized,
            }
        except Exception:
            # The bot did not answer, and we are carrying forward its LAST
            # payload. Omitting data_stale here let the consumer's
            # .get("data_stale", False) publish "fresh" for a dead bot
            # serving a previous cycle's data — the freshness flag read clean
            # in exactly the case it exists to catch. It is set explicitly,
            # and the age is measured from the last successful contact so the
            # staleness grows for as long as the bot stays down.
            _prev_seen = prev.get("last_seen")
            _age = ((time.time() - _prev_seen)
                    if isinstance(_prev_seen, (int, float)) else None)
            new_bots[bid] = {
                "id": bid, "name": bot["name"], "port": bot["port"],
                "color": bot["color"], "alive": False,
                "last_seen": _prev_seen, "latency_ms": None,
                "data_stale": True,
                "stale_age_s": (round(_age, 1) if _age is not None else None),
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
                # An unscored signal is not a 50-scored signal. The old
                # `.get("score", 50)` submitted a fabricated 0.5 CONVICTION
                # into the aggregator and the decomposition log — a number
                # nobody computed, entering the decision path. Skip instead:
                # Oracle always scores what it publishes, so a missing score
                # means the payload is malformed, not neutral.
                _score = sig.get("score")
                if direction in ("LONG", "SHORT") and isinstance(
                        _score, (int, float)):
                    _conf = min(1.0, _score / 100)
                    _signal_aggregator.submit_proposal(
                        source="oracle",
                        pair=sig.get("pair", ""),
                        direction=direction,
                        confidence=_conf,
                        metadata={"strategy": sig.get("strategy", "")},
                    )
                    _signal_decomposition.log_signal(
                        "oracle", sig.get("pair", ""), direction, _conf,
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
_AEGIS_RAISE_HOLD_SEC = 1200  # a cap RAISE must hold this long before applying (drops are instant)
_aegis_raise_pending: dict = {}  # {"limit": target_pct, "since": ts} while a raise is on hold
# Rehydrate from cc_state.json so a reboot within the cooldown window doesn't
# trigger an immediate (unwanted) re-adjustment of deployment limits.
try:
    _last_aegis_adjust = float(_load_cc_state().get("last_aegis_adjust", 0) or 0)
except Exception:
    _last_aegis_adjust = 0
# Unreadable state must not read as "never adjusted". A 0 satisfies the
# cooldown check below immediately, so a truncated file lets AEGIS re-adjust
# the fleet deployment cap on boot — including applying a RAISE without
# serving its hold. Unknown means just-adjusted.
if _cc_state_unreadable:
    _last_aegis_adjust = time.time()
    log.error("CC state unreadable — treating AEGIS as just-adjusted so a "
              "deployment-cap change cannot skip its cooldown.")


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
    # Never adjust the fleet cap from a score AEGIS has not computed.
    # AEGIS now publishes score None until its first scan completes, but
    # this side must not depend on that: on 2026-08-13 04:15:26 an AEGIS
    # restart put "raise to 80% pending 20min hold (score=0.5000)" in this
    # log — the init seed, mapped straight to the NORMAL tier. The raise
    # hold absorbed it by luck; a DROP applies instantly with no hold.
    _st = str(_first_available(raw, norm, "status") or "").lower()
    _cyc = _first_available(raw, norm, "cycle")
    if _st in ("initializing", "starting") or (
            isinstance(_cyc, (int, float)) and _cyc < 1):
        log.info("AEGIS not yet scanned (status=%r cycle=%r) — deployment "
                 "cap unchanged", _st or None, _cyc)
        return

    score = _first_available(raw, norm, "score", "aegis_score")
    if score is None:
        return
    if not isinstance(score, (int, float)) or score != score:
        log.warning("AEGIS score %r is not a usable number — deployment cap "
                    "unchanged", score)
        return

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

    # Signal product (2026-07-30): the fee-ratio deployment throttle was
    # removed — the fleet never pays fees, so there is nothing to throttle
    # on. Deployment limit is driven by the AEGIS score alone.
    # throttle_reason/fee_ratio keys are kept (as None) in the event payload
    # below so PORTFOLIO_LIMIT_CHANGE consumers don't break on missing keys.
    new_limit = base_limit

    # Raise hysteresis (2026-07-31): drops apply immediately (defensive must
    # stay fast), but a RAISE only lands after the computed tier has held
    # continuously for _AEGIS_RAISE_HOLD_SEC. AEGIS lives ~0.17 and spikes
    # over the 0.2 tier boundary for 5-15 min a few times a day; without the
    # hold, every spike opened a brief entry window and whatever entered
    # became instantly over-cap when the tier dropped back (TurtleSue UNI
    # pyramid through the 07:36-07:52 window, 2026-07-31).
    global _aegis_raise_pending
    _pending_before = dict(_aegis_raise_pending)
    _current_limit = None
    for _pm in [_portfolio_paper, _portfolio_live]:
        if _pm:
            _current_limit = _pm.limits.get("max_deployed_pct", 80)
            break
    if _current_limit is not None and new_limit > _current_limit:
        if _aegis_raise_pending.get("limit") != new_limit:
            _aegis_raise_pending = {"limit": new_limit, "since": now}
            log.info(f"AEGIS: raise to {new_limit}% pending {_AEGIS_RAISE_HOLD_SEC // 60}min hold (score={score:.4f})")
            new_limit = _current_limit
        elif now - _aegis_raise_pending.get("since", now) < _AEGIS_RAISE_HOLD_SEC:
            _elapsed = (now - _aegis_raise_pending.get("since", now)) / 60
            log.info("AEGIS: raise to %s%% holding — %.1f of %d min elapsed "
                     "(score=%.4f)", new_limit, _elapsed,
                     _AEGIS_RAISE_HOLD_SEC // 60, score)
            new_limit = _current_limit
        else:
            log.info(f"AEGIS: raise to {new_limit}% held {_AEGIS_RAISE_HOLD_SEC // 60}min — applying")
            _aegis_raise_pending = {}
    else:
        # Not a raise (drop or unchanged): clear any pending hold so a fresh
        # spike must restart its clock.
        _aegis_raise_pending = {}

    # Persist the hold clock whenever it changes, even if the cap itself did
    # not — that is the whole point: the elapsed time must survive restarts.
    # Without this the file only ever records the TIGHTENING, and the fleet
    # ratchets one-way (see PortfolioManager._save).
    if _aegis_raise_pending != _pending_before:
        for _pm in [_portfolio_paper, _portfolio_live]:
            if _pm:
                try:
                    _pm._save()
                except Exception:
                    log.warning("failed to persist AEGIS raise-hold state",
                                exc_info=True)

    # Apply AEGIS adjustment to BOTH portfolios.
    # old_limit is captured OUTSIDE the loop: reading it inside leaves it bound
    # to whichever portfolio happened to be processed last, and leaves it
    # entirely unbound (NameError) when both portfolios are None — so the
    # change-detection below would compare against the wrong value or crash.
    old_limit = new_limit
    for _pm in [_portfolio_paper, _portfolio_live]:
        if not _pm:
            continue
        _changed = False
        with _pm._lock:
            _pm_old = _pm.limits.get("max_deployed_pct", 80)
            if _pm_old != new_limit:
                old_limit = _pm_old
                _pm.limits["max_deployed_pct"] = new_limit
                _changed = True
            _pm.aegis_score = float(score)
        # Persist OUTSIDE the lock (_save takes its own). An in-memory-only cap
        # is undone by the next restart, which is how a defensive tightening
        # kept being handed back as deployment headroom.
        if _changed:
            try:
                _pm._save()
            except Exception:
                log.warning("Failed to persist AEGIS deployment cap %s%%",
                            new_limit, exc_info=True)

    if old_limit != new_limit:
        _event_bus.publish({
            "source": "command_center",
            "type": "PORTFOLIO_LIMIT_CHANGE",
            "data": {
                "aegis_score": round(score, 4),
                "regime": regime,
                "old_limit": old_limit,
                "new_limit": new_limit,
                "throttle_reason": None,             # fee throttle removed 2026-07-30
                "fee_ratio": None,                   # kept for consumer shape only
            },
        })
        log.info(f"AEGIS: {regime} -> {new_limit}% (base={base_limit})")

    _last_aegis_adjust = now
    _save_cc_state({"last_aegis_adjust": now})


def _position_key(bot_id, pair) -> tuple:
    """Canonical (bot, pair) key for matching positions to reservations.

    Must be derived the SAME way on both sides or the stale-reservation sweep
    releases capital out from under a live trade. Normalizing through
    standards.normalize_pair is what makes that true: bots key positions by
    whatever their exchange calls the pair, reservations carry the fleet's
    display form, and "strip the slash and upper-case it" only makes those
    agree by luck.

    TurtleSue keys XLM as the Kraken pair "XXLMZUSD"; its reservation says
    "XLM/USD". The old rule produced XXLMZUSD vs XLMUSD — no match, so a live
    short was one hour of uptime away from having its reservation swept.
    UNIUSD -> UNIUSD matched only because that pair has no Kraken prefix.
    """
    p = normalize_pair(pair) or pair
    return (str(bot_id).lower(), str(p).replace("/", "").upper())


def _active_position_keys(bots: dict) -> set:
    """{(bot_id_lower, PAIRNOSLASH)} for every position an alive bot reports.

    Shape-tolerant, because bots disagree on all three of: the FIELD holding
    open exposure (positions / active_grids), the CONTAINER (dict keyed by
    pair, or list of dicts), and the PAIR FORMAT (Kraken code vs display).
    Getting any of the three wrong silently yields fewer keys, and a missing
    key means the stale sweep releases capital under a live trade — it fails
    open, with no error.

    Used to protect long-held trades from the stale-reservation sweep.
    """
    keys = set()
    for bid, bot in bots.items():
        if not bot.get("alive"):
            continue
        # Fall back to `normalized` when `raw` is absent: /api/master strips
        # raw from its response, so any caller working from the API rather
        # than _state would otherwise silently protect nothing.
        raw = bot.get("raw") or bot.get("normalized") or {}
        # active_grids: Gridzilla holds capital as grids, not positions. Its 4
        # reservations ($170,134 on 2026-08-06 — the largest holdings in the
        # pool) matched no key here and were sweepable. `open_positions` is an
        # int count on several bots, which the isinstance checks below skip.
        for field in ("positions", "open_positions", "active_grids", "grids"):
            val = raw.get(field)
            if isinstance(val, dict):
                for k, v in val.items():
                    # Prefer an explicit pair/name field over the dict key —
                    # TurtleSue keys by Kraken pair but carries "UNI/USD" in
                    # `name`. Both are normalized, so either route agrees.
                    p = k
                    if isinstance(v, dict):
                        p = v.get("pair") or v.get("name") or k
                    keys.add(_position_key(bid, p))
            elif isinstance(val, list):
                for it in val:
                    if isinstance(it, dict):
                        p = it.get("pair") or it.get("symbol")
                        if p:
                            keys.add(_position_key(bid, p))
    return keys


def _poll_loop():
    """Continuously fetch, normalize, aggregate, and store state."""
    # Seeded to NOW, not 0: with 0 the first sweep fires on the first poll
    # cycle (~9s after launch) while most of the fleet is still booting, so
    # every aged reservation is unprotected — that boot race released $25.8k
    # under TurtleSue's live XRP position on 2026-08-13. The fleet has an
    # hour to come up before the first sweep.
    _last_stale_cleanup = time.time()
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
        # Hourly stale reservation cleanup on BOTH portfolios. Reservations
        # whose bot still self-reports the position are long-held trades,
        # not orphans — never sweep those.
        if time.time() - _last_stale_cleanup > 3600:
            with _lock:
                _bots_now = dict(_state["bots"])
            _active = _active_position_keys(_bots_now)
            _reporting = {str(_bid).lower() for _bid, _b in _bots_now.items()
                          if _b.get("alive")}
            for _pm_label, _pm in [("paper", _portfolio_paper), ("live", _portfolio_live)]:
                if _pm:
                    released_list = _pm.force_release_stale(
                        max_age_hours=48, active_positions=_active,
                        reporting_bots=_reporting)
                    if released_list:
                        log.info("Auto-released %d stale %s reservation(s) (>48h old)", len(released_list), _pm_label)
                        for _sr in released_list:
                            log.warning(
                                "Stale reservation force-released: bot=%s pair=%s amount=$%.0f "
                                "(held %.1fh, position no longer reported by bot)",
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


def _sync_kraken_balance() -> dict:
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

        with _portfolio_live._lock:
            old_total = _portfolio_live.total
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
# /api/trades scan cache (TS-2)
# ---------------------------------------------------------------------------
# The durable trade history lives in logs/event_bus/*.jsonl + logs/events/*.jsonl.
# Rescanning every file on every request cost 5-6s per dashboard call (the
# fetchTradeHistory 8s-timeout race). Historical day-files never change, so
# parsed TRADE_CLOSE rows are cached per file keyed by (mtime, size); only
# files that actually changed since the last request (today's) are re-read.
# The dedup pipeline then runs over in-memory rows (hundreds of trades, not
# megabytes of JSONL) so the handler returns in milliseconds while the
# response shape stays identical.

_trades_scan_lock = threading.Lock()
# fpath -> (mtime, size, rows) where rows = [(event_id_or_None, trade_dict), ...]
_trades_file_cache: dict[str, tuple[float, int, list]] = {}


def _parse_trade_log_file(fpath: str) -> list:
    """Extract TRADE_CLOSE rows from one JSONL log file, in file order.

    Row shape must stay in lock-step with what /api/trades serves — these
    dicts are returned to the dashboard verbatim (and are shared across
    requests via the cache: treat them as immutable downstream).
    """
    rows: list = []
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
            bot = ev.get("source") or ev.get("bot") or ""
            d = ev.get("data") if isinstance(ev.get("data"), dict) else ev
            pnl_val = d.get("pnl", ev.get("pnl", 0))
            rid = d.get("reservation_id") or ev.get("reservation_id")
            via = d.get("via") or ev.get("via", "")
            rows.append((ev.get("id"), {
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
            }))
    return rows


def _is_probe_pair(pair) -> bool:
    """Synthetic pairs created by test harnesses exercising the live path.

    Nothing in the payload marks them, so the pair name is the only signal —
    every probe uses a marker prefix no real market carries. They are not a
    rounding error: on 2026-08-07, 50 of 73 rows on /api/trades were probes
    contributing +$617 of fabricated P/L, flipping the raw sum positive while
    real pairs summed -$167. The SIGN of the fleet's trade feed depended on
    test data. (Same predicate as weekly_analysis._is_probe; if probe rows
    ever gain a `synthetic: true` field at write time, both should key on it
    instead.)
    """
    # Delegates to probe_pairs, the ONE definition (2026-08-26). This body
    # used to be one of four copies that had already drifted — see that
    # module's docstring. The wrapper name is kept because call sites and
    # tests reference it.
    return is_probe_pair(pair)


def _collect_closed_trades() -> list[dict]:
    """All durable TRADE_CLOSE trades, fully deduped, newest first.

    Three-stage dedup (unchanged semantics from the original in-handler scan):
      1. By event id (catches the same event written twice to disk)
      2. By reservation_id (the canonical trade key — same trade emitted
         via the release path AND the snapshot-diff bridge)
      3. By heuristic (bot, normalized pair, 10min window) for legacy
         snapshot-diff entries that have no reservation_id

    Bot filtering happens in the handler AFTER dedup — every dedup key
    includes the bot (event ids are globally unique; a reservation_id belongs
    to one bot; stage 3 compares bot equality), so filter placement cannot
    change which rows survive.
    """
    import glob as _glob
    here = os.path.dirname(os.path.abspath(__file__))
    log_dirs = [
        os.path.join(here, "logs", "event_bus"),
        os.path.join(here, "logs", "events"),
    ]

    all_rows: list = []
    with _trades_scan_lock:
        live_paths: set[str] = set()
        for log_dir in log_dirs:
            if not os.path.isdir(log_dir):
                continue
            for fpath in sorted(_glob.glob(os.path.join(log_dir, "*.jsonl"))):
                live_paths.add(fpath)
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue
                cached = _trades_file_cache.get(fpath)
                if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                    rows = cached[2]
                else:
                    try:
                        rows = _parse_trade_log_file(fpath)
                    except Exception:
                        rows = []
                    _trades_file_cache[fpath] = (st.st_mtime, st.st_size, rows)
                all_rows.extend(rows)
        # Evict entries for rotated/deleted files so the cache stays bounded.
        for stale in [p for p in _trades_file_cache if p not in live_paths]:
            _trades_file_cache.pop(stale, None)

    # ── Stage 1 dedup: by event id ──
    seen_ids: set[str] = set()
    raw_trades: list[dict] = []
    for ev_id, t in all_rows:
        if ev_id:
            if ev_id in seen_ids:
                continue
            seen_ids.add(ev_id)
        raw_trades.append(t)

    # ── Stage 2 dedup: by reservation_id ──
    # Same trade can land on disk twice — once from _handle_release
    # (via=portfolio_release, has reservation_id) and once from
    # fleet_logger._detect_events (snapshot diff, no reservation_id).
    # Prefer the release-path entry — it has the realized pnl and uppercase
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
    # and the release record is canonical (realized pnl, real direction
    # casing, real reservation_id). The 90s same-pair OPEN cooldown in
    # PortfolioManager makes faster legitimate turnover impossible.
    # Pair comparison MUST be normalized. The two emitters use different
    # pair formats for the same instrument — the bot's event_publisher
    # sends Kraken-native "AAVEUSD" while the release path sends "AAVE/USD"
    # — so a raw string compare never matched and BOTH copies survived.
    # That double-counted every snapshot-diffed close: TurtleSue's AAVE
    # short showed up twice (-227.24 and -227.37, same second), inflating
    # its realized loss by ~57% (2026-07-29 audit). normalize_pair collapses
    # AAVEUSD/AAVE/USD/XXBTZUSD to one canonical form.
    def _npair(p: str) -> str:
        try:
            return normalize_pair(p or "")
        except Exception:
            return p or ""

    rid_lookup: list[tuple[str, str, float]] = [
        (t.get("bot", ""), _npair(t.get("pair", "")), float(t.get("ts") or 0))
        for t in by_rid.values()
    ]
    deduped_no_rid: list[dict] = []
    for t in no_rid:
        tb = t.get("bot", "")
        tp = _npair(t.get("pair", ""))
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
                if (kept.get("bot") == tb
                        and _npair(kept.get("pair", "")) == tp
                        and abs(float(kept.get("ts") or 0) - tts) < 600):
                    already = True
                    break
            if not already:
                deduped_no_rid.append(t)

    trades = list(by_rid.values()) + deduped_no_rid
    trades.sort(key=lambda t: t["ts"], reverse=True)   # newest first
    return trades


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class CommandCenterHandler(BaseHTTPRequestHandler):

    # -- Exact-match GET route table ------------------------------------------
    _GET_ROUTES: dict[str, str] = {
        "/":                        "_serve_index",
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
        "/api/current_era":         "_serve_current_era",
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
        "/api/portfolio/confirm":  "_handle_portfolio_confirm",
        "/api/portfolio/sync":     "_handle_portfolio_sync",
        "/api/events/publish":     "_handle_event_publish",
        "/api/signals/propose":    "_handle_signals_propose",
        "/api/signals/outcome":    "_handle_signals_outcome",
        "/api/expectancy/record":  "_handle_expectancy_record",
        # Test-support: evict rows whose pair carries a test marker. Scoped
        # to a prefix so it cannot remove real history.
        "/api/expectancy/evict":   "_handle_expectancy_evict",
        "/api/expectancy/repair":  "_handle_expectancy_repair",
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
            # parse_constant fires on the bare NaN / Infinity / -Infinity
            # literals, which Python's json accepts by DEFAULT and standard
            # JSON does not permit. They are not merely unusual values: NaN
            # compares False against every bound, so it defeats every risk
            # gate downstream, and it cannot be re-encoded, which takes the
            # response with it. Reject at the boundary so no handler in the
            # process ever holds one.
            def _no_constants(tok):
                raise ValueError(f"non-finite JSON literal {tok!r} not accepted")
            data = json.loads(body, parse_constant=_no_constants) if body else {}
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json({"error": f"Invalid JSON: {e}"}, 400)
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
                        "data_stale": entry.get("data_stale", False),
                        "stale_age_s": entry.get("stale_age_s"),
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

            # Kraken connection status used to be derived from TrekBot's raw
            # health block. TrekBot left the fleet (renamed GoldenEye, now
            # standalone), so that lookup could only ever yield "offline" —
            # a false negative, not a real reading. No remaining fleet bot
            # reports exchange connectivity, so report it as unknown rather
            # than asserting a state nothing measures.
            _kraken_status = "unknown"

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
            # Revalidate every time, keyed on mtime+size. The dashboard HTML is
            # already served no-store, but its JS (solar_system.js) was getting
            # "max-age=3600" -- so an edit to a renderer silently did nothing for
            # an hour and the browser kept executing the old file. That cost real
            # debugging time on 2026-07-29 (planet changes appeared not to apply).
            # no-cache still allows a 304 via ETag, so we keep the bandwidth win
            # without ever serving stale code.
            try:
                st = os.stat(file_path)
                etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
                self.send_header("ETag", etag)
            except OSError:
                pass
            self.send_header("Cache-Control", "no-cache, must-revalidate")
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

    def _require_portfolio(self):
        """Return the active portfolio, or send a 503 and return None."""
        pm = _active_portfolio()
        if not pm:
            self._send_json({"error": "Portfolio manager not initialized"}, 503)
        return pm

    def _serve_portfolio(self, parsed) -> None:
        if pm := self._require_portfolio():
            self._send_json(pm.state())

    def _serve_portfolio_available(self, parsed) -> None:
        if pm := self._require_portfolio():
            self._send_json(pm.available_snapshot())

    def _serve_portfolio_exposure(self, parsed) -> None:
        if pm := self._require_portfolio():
            self._send_json(pm.exposure_snapshot())

    def _serve_portfolio_reservations(self, parsed) -> None:
        """GET /api/portfolio/reservations — return all active reservations."""
        if not self._require_portfolio():
            return
        with _active_portfolio()._lock:
            reservations = {
                rid: {
                    "bot_id": r["bot_id"],
                    "pair": r["pair"],
                    "amount": r["amount"],
                    "direction": r.get("direction"),
                    "reserved_at": r["reserved_at"],
                    # Lease visibility: without these the heartbeat contract is
                    # unobservable from outside (silent-failure rule).
                    "last_confirmed_at": r.get("last_confirmed_at"),
                    "unconfirmed_since": r.get("unconfirmed_since"),
                }
                for rid, r in _active_portfolio().reservations.items()
            }
            bot_confirms = dict(_active_portfolio()._bot_confirms)
        self._send_json({"reservations": reservations, "bot_confirms": bot_confirms})

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
                    days_left = (datetime.datetime.strptime(dl, "%Y-%m-%d")
                                 - datetime.datetime.strptime(today, "%Y-%m-%d")).days
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
            if live_total is not None and _portfolio_live:
                _lt = float(live_total)
                if not math.isfinite(_lt) or _lt <= 0:
                    self._send_json({"ok": False,
                                     "reason": "live_total must be a finite "
                                               "positive number"}, 400)
                    return
                # Was three unlocked statements. reservations.clear() discarded
                # every reservation WITHOUT releasing it — no history entry, no
                # PORTFOLIO_LEASE_SWEEP event, no P/L — while the positions
                # behind them stayed open on the venue. And a concurrent
                # reserve() holding the lock could interleave, so a bot could
                # be handed a rid that the clear then dropped, leaving it
                # trading against capital the pool no longer tracks.
                with _portfolio_live._lock:
                    _dropped = list(_portfolio_live.reservations.items())
                    for _rid, _r in _dropped:
                        _portfolio_live.history.append({
                            "action": "mode_switch_discard",
                            "reservation_id": _rid,
                            "bot_id": _r.get("bot_id"),
                            "pair": _r.get("pair"),
                            "amount": _r.get("amount"),
                            "reason": "live balance manually set — reservation "
                                      "discarded, position NOT closed",
                            "timestamp": time.time(),
                        })
                    _portfolio_live.reservations.clear()
                    _portfolio_live.total = _lt
                _portfolio_live._save()
                if _dropped:
                    log.warning(
                        "MODE SWITCH discarded %d live reservation(s) totalling "
                        "$%.2f — the positions behind them are NOT closed and "
                        "are now untracked. Reconcile against the venue.",
                        len(_dropped),
                        sum(float(r.get("amount") or 0) for _, r in _dropped))
                log.info("Live portfolio balance manually set to $%.2f", _lt)
            elif _portfolio_live:
                sync_result = _sync_kraken_balance()
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
        # Always build stats from the log file — the live http.client proxy to
        # port 9002 stays disabled (Python 3.14 HTTP/1.0 bug; see CLAUDE.md).
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
                            tier = entry.get("tier", "")
                            if not entry.get("success"):
                                failed += 1
                            elif tier.startswith("paid"):
                                paid_today += 1
                                last_paid = ts
                            elif tier.startswith("free"):
                                free_today += 1
                                last_free = ts
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

    # One-time flag so the rankings filter logs what it suppresses once per
    # process, not once per dashboard poll.
    _rankings_filter_logged = False

    def _serve_signals_rankings(self, parsed) -> None:
        rows = _signal_aggregator.get_source_rankings()
        # Same confetti lineage as the decomposition store: sources like
        # "trekbot:n" are single CHARACTERS of a char-iterated signal string
        # from a bot retired from the fleet, yet they were served here with
        # weights and verdicts as if they were ranked signals. The response
        # shape is a bare LIST (the dashboard's error fallback is []), so
        # disclosure cannot ride this payload — the named exclusions ship on
        # /api/signals/decomposition (excluded_artifacts / excluded_retired),
        # and the suppression is logged once per process here.
        try:
            _roster = set(getattr(_fleet_config, "BOTS", {}) or {})
            kept, dropped = [], []
            for row in rows:
                bot, _, sig = str(row.get("source", "")).partition(":")
                if len(sig if sig else bot) <= 1:
                    dropped.append(row.get("source"))
                elif _roster and bot not in _roster:
                    dropped.append(row.get("source"))
                else:
                    kept.append(row)
            if dropped and not CommandCenterHandler._rankings_filter_logged:
                CommandCenterHandler._rankings_filter_logged = True
                log.info("signals/rankings: suppressing %d artifact/retired "
                         "source(s): %s — see /api/signals/decomposition for "
                         "the disclosed list", len(dropped), sorted(dropped))
            rows = kept
        except Exception:
            log.warning("rankings serve-time filter failed", exc_info=True)
        self._send_json(rows)

    def _serve_signals_decomposition(self, parsed) -> None:
        result = _signal_decomposition.compute_signal_value()
        # The persisted store predates two guards, and every row in it shows
        # it: sources like "trekbot:n" and bare "f"/"2" are single CHARACTERS
        # of an iterated signal string, from a bot retired from the fleet.
        # The CUT/KEEP recommendation was being computed over that confetti.
        # Serve-time filtering (not a store edit — CC holds the store in
        # memory, a disk edit would be overwritten): a ranking key whose
        # signal part is a single character is an artifact; a source bot not
        # in the registry is retired history. Both are disclosed, not
        # silently vanished.
        try:
            _roster = set(getattr(_fleet_config, "BOTS", {}) or {})
            rankings = result.get("rankings") or {}
            kept, artifacts, retired = {}, [], []
            for key, row in rankings.items():
                bot, _, sig = str(key).partition(":")
                if len(sig if sig else bot) <= 1:
                    artifacts.append(key)
                elif _roster and bot not in _roster:
                    retired.append(key)
                else:
                    kept[key] = row
            if artifacts or retired:
                result = dict(result)
                result["rankings"] = kept
                result["excluded_artifacts"] = sorted(artifacts)
                result["excluded_retired"] = sorted(retired)
                if not kept:
                    result["recommendation"] = (
                        "no current-fleet signal has decomposition data yet — "
                        "prior rankings were single-character parsing artifacts "
                        "from a retired bot and are excluded above")
        except Exception:
            log.warning("decomposition serve-time filter failed", exc_info=True)
        self._send_json(result)

    def _serve_expectancy(self, parsed) -> None:
        self._send_json(_expectancy_tracker.get_fleet_stats())

    def _serve_current_era(self, parsed) -> None:
        """The fleet's record on the CURRENT pool, with nothing else in it.

        Every other P/L figure this server publishes is lifetime, and lifetime
        spans the 2026-08-13 pool resize ($1,000,000 -> $210.53). Summed
        across that boundary the fleet reads -$362.76; restricted to the
        current pool it reads +$5.01 over 14 closed trades. Both are
        arithmetically correct and they describe different things, which is
        precisely why this endpoint exists rather than a footnote somewhere.

        Three exclusions, each of which otherwise flatters the number:

          - PRE-RESIZE TRADES. Sized against a pool 4,750x larger. A single
            pre-resize trade (ETHFI +3111.19) exceeds the entire current pool
            by 14x.
          - PROBE PAIRS. ZZPROBE/NF fixtures are test scaffolding. They were
            30 of 58 post-resize closes and every one of them a win, worth
            +370.20 -- all of TurtleSue's apparent profit. Its real-pair P/L
            is $0.00.
          - UNDECIDED (FLAT) TRADES. Counted in n, excluded from the win rate
            denominator, and reported separately. A flat is not a win.

        Returns n alongside every rate. A rate without its n is not a
        measurement, and at n=14 nothing here is significant -- the response
        says so in `caveat` rather than leaving the reader to infer it.
        """
        trades = _collect_closed_trades()

        kept, pre, probe = [], 0, 0
        for t in trades:
            ts = t.get("ts") or t.get("closed_at_ts") or 0
            if not isinstance(ts, (int, float)) or ts < POOL_RESIZE_TS:
                pre += 1
                continue
            if _is_probe_pair(t.get("pair")):
                probe += 1
                continue
            kept.append(t)

        pnls, by_bot = [], {}
        for t in kept:
            p = t.get("pnl")
            if not isinstance(p, (int, float)):
                continue
            pnls.append(p)
            by_bot.setdefault(t.get("bot") or "unknown", []).append(p)

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        flats = [p for p in pnls if p == 0]
        decided = len(wins) + len(losses)

        def _stats(vals):
            w = [v for v in vals if v > 0]
            l = [v for v in vals if v < 0]
            d = len(w) + len(l)
            return {
                "n": len(vals),
                "pnl": round(sum(vals), 4),
                "wins": len(w), "losses": len(l), "flat": len(vals) - d,
                "win_rate": round(100.0 * len(w) / d, 1) if d else None,
                "win_rate_n": d,
            }

        self._send_json({
            "era_start_ts": POOL_RESIZE_TS,
            "era_note": POOL_RESIZE_NOTE,
            "n": len(pnls),
            "total_pnl": round(sum(pnls), 4) if pnls else 0.0,
            "wins": len(wins), "losses": len(losses), "flat": len(flats),
            # Over DECIDED trades only -- flats are neither a win nor a loss.
            "win_rate": round(100.0 * len(wins) / decided, 1) if decided else None,
            "win_rate_n": decided,
            "avg_win": round(sum(wins) / len(wins), 4) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 4) if losses else None,
            "expectancy_per_trade": round(sum(pnls) / len(pnls), 4) if pnls else None,
            "by_bot": {b: _stats(v) for b, v in sorted(by_bot.items())},
            "excluded": {
                "pre_resize": pre,
                "probe_pairs": probe,
                "why": "pre-resize trades were sized against a $1,000,000 "
                       "pool; probe pairs are test fixtures, not trades",
            },
            # Stated, not implied. The fleet's own bar for an expectancy read
            # is ~30 closed trades; below it this is a direction, not a rate.
            "caveat": (None if len(pnls) >= 30 else
                       "PROVISIONAL: n=%d is below the 30-trade bar — quote "
                       "with the n, do not drive parameter changes from it"
                       % len(pnls)),
        })

    def _serve_trades(self, parsed) -> None:
        """Return per-bot trade history from the durable event bus log.
        Survives restarts via logs/event_bus/*.jsonl (written by EventBus.publish).
        Query params: ?bot=confluence&limit=50

        TS-2: the JSONL scan + three-stage dedup live in
        _collect_closed_trades() behind a per-file (mtime, size) cache —
        repeated dashboard calls return in milliseconds instead of rescanning
        every log file. Response shape unchanged: {"trades": [...], "total": N}.
        """
        qs = parse_qs(parsed.query or "")
        bot_filter = qs.get("bot", [""])[0].strip().lower()
        try:
            limit = int(qs.get("limit", ["100"])[0])
        except ValueError:
            limit = 100

        trades = _collect_closed_trades()
        if bot_filter:
            trades = [t for t in trades
                      if (t.get("bot") or "").lower() == bot_filter]

        # Stamp synthetic probe rows and disclose the split. Before this,
        # 50 of 73 rows were unmarked test-harness closes worth +$617 —
        # the raw sum read +$450 while real pairs summed -$167. A feed whose
        # SIGN depends on test data must at minimum say which rows are which.
        # ?synthetic=exclude serves only real trades; =only serves the probes.
        trades = [dict(t, synthetic=_is_probe_pair(t.get("pair"))) for t in trades]
        _mode = qs.get("synthetic", ["include"])[0].strip().lower()
        if _mode == "exclude":
            trades = [t for t in trades if not t["synthetic"]]
        elif _mode == "only":
            trades = [t for t in trades if t["synthetic"]]

        def _sum(rows):
            return round(sum(t.get("pnl") for t in rows
                             if isinstance(t.get("pnl"), (int, float))), 2)

        _real = [t for t in trades if not t["synthetic"]]
        _syn = [t for t in trades if t["synthetic"]]
        self._send_json({
            "trades": trades[:limit],
            "total": len(trades),
            "real_total": len(_real),
            "synthetic_total": len(_syn),
            "real_pnl": _sum(_real),
            "synthetic_pnl": _sum(_syn),
        })

    # POST-only action endpoints under /api/expectancy/. They must NOT be
    # swallowed by the bot_id prefix route below, which would answer a GET
    # with a full, confident, entirely fabricated stats blob for a "bot"
    # named "repair".
    _EXPECTANCY_ACTIONS = ("repair", "evict", "record")

    def _serve_expectancy_prefix(self, parsed, path: str) -> None:
        bot_id = path.split("/api/expectancy/", 1)[1].strip("/")

        # This route used to hand ANY trailing string to get_bot_stats(),
        # which returns a well-formed zero record for a bot that does not
        # exist. So GET /api/expectancy/notabot answered 200 with
        # {"bot_id": "notabot", "total_trades": 0, ...} — a complete record
        # for a nonexistent entity, indistinguishable from a real bot that
        # has genuinely never traded. Same shape as every other defect this
        # fleet has been clearing: absence rendered as a confident zero.
        if bot_id in self._EXPECTANCY_ACTIONS:
            self._send_json({"error": f"/api/expectancy/{bot_id} is POST-only"},
                            405)
            return

        _roster = set(getattr(_fleet_config, "BOTS", {}) or {})
        known = set(_expectancy_tracker.trades.keys()) | _roster
        if bot_id not in known:
            self._send_json({"error": f"unknown bot {bot_id!r}",
                             "known": sorted(_roster)}, 404)
            return

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

    def _serve_locked_snapshot(self, lock, value, no_data_message: str) -> None:
        """Serve a lock-guarded module-level snapshot dict, or a no_data stub."""
        with lock:
            data = dict(value) if value else {}
        if not data:
            self._send_json({"status": "no_data", "message": no_data_message})
            return
        self._send_json(data)

    def _serve_fleet_exposure(self, parsed) -> None:
        """GET /api/fleet/exposure — aggregated factor exposure across all positions."""
        self._serve_locked_snapshot(
            _fleet_exposure_lock, _fleet_exposure,
            "Factor exposure data not yet computed. Wait for next poll cycle.")

    def _serve_fleet_correlations(self, parsed) -> None:
        """GET /api/fleet/correlations — cross-bot PnL correlation matrix and alerts."""
        self._serve_locked_snapshot(
            _bot_correlation_lock, _bot_correlation_matrix,
            "Correlation data not yet computed. Need at least 30 poll cycles (~2 minutes).")

    def _serve_fleet_attribution(self, parsed) -> None:
        """GET /api/fleet/attribution — performance attribution (alpha/beta/cost decomposition)."""
        self._serve_locked_snapshot(
            _perf_attribution_lock, _perf_attribution,
            "Attribution data not yet computed. Wait for next poll cycle.")

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
            # `signals` must be a LIST of names. A bot that sends a plain
            # string used to pass the truthiness check and every consumer's
            # `for sig in signals` iterated its CHARACTERS — the live
            # decomposition store is full of the evidence: sources named
            # "trekbot:n", "trekbot:z", bare "f" and "2", each a single
            # character of some signal string, and the fleet's CUT/KEEP
            # recommendation was computed over that confetti. A lone string
            # is wrapped; anything else is dropped with a log line.
            _sigs = edata["signals"]
            if isinstance(_sigs, str):
                _sigs = [_sigs]
            elif not isinstance(_sigs, (list, tuple)):
                log.warning("TRADE_OPEN from %r: signals is %s, not a list "
                            "— ignored", source, type(_sigs).__name__)
                _sigs = []
            # Single characters are the artifact signature, never a real
            # signal name — filtered wherever they came from.
            _sigs = [s for s in _sigs if isinstance(s, str) and len(s) > 1]
            edata = dict(edata, signals=_sigs)
        if etype == "TRADE_OPEN" and edata.get("signals"):
            with _open_trade_signals_lock:
                _open_trade_signals[f"{source}:{pair}"] = {
                    "signals": edata["signals"],
                    "factors": edata.get("factors", {}),
                    "opened_at": time.time(),
                }
            _save_open_trade_signals()
            # Also submit each signal to the aggregator
            # A TRADE_OPEN that states no direction is not a long, and one
            # that states no confidence has not stated 0.5. Both were
            # fabricated here and then fed straight into a
            # confidence-weighted vote and the signal-decomposition record.
            _edir = edata.get("direction")
            _edir = _edir.upper() if isinstance(_edir, str) and _edir else None
            _econf = edata.get("confidence")
            _econf = _econf if isinstance(_econf, (int, float)) else None
            if _edir is None or _econf is None:
                log.warning(
                    "TRADE_OPEN from %r for %r has no %s -- not submitting to "
                    "the weighted vote (direction=%r confidence=%r)",
                    source, pair,
                    "direction" if _edir is None else "confidence",
                    edata.get("direction"), edata.get("confidence"))
            else:
                for sig in edata["signals"]:
                    _signal_aggregator.submit_proposal(
                        source=f"{source}:{sig}",
                        pair=pair,
                        direction=_edir,
                        confidence=_econf,
                    )
                    _signal_decomposition.log_signal(
                        source=f"{source}:{sig}",
                        pair=pair,
                        direction=_edir,
                        confidence=_econf,
                    )
        elif etype == "TRADE_CLOSE" and pair:
            key = f"{source}:{pair}"
            with _open_trade_signals_lock:
                open_info = _open_trade_signals.pop(key, None)
            if open_info is not None:
                _save_open_trade_signals()
            # An absent pnl is not $0.00. The old default-to-zero made
            # `won = pnl > 0` book every unmeasured close as a LOSS in the
            # per-direction hit rate, and fed a fabricated 0.0 through to
            # the expectancy writer below.
            _praw = edata.get("pnl")
            pnl = _praw if isinstance(_praw, (int, float)) else None
            # An unlabelled close is not a long. record_outcome credits or
            # debits the per-direction hit rate, so defaulting here silently
            # attributed every unlabelled trade to the long side and skewed
            # any later long/short comparison.
            _cdir = edata.get("direction")
            direction = _cdir.upper() if isinstance(_cdir, str) and _cdir else None
            if direction is None:
                log.warning("TRADE_CLOSE from %r for %r has no direction -- "
                            "outcome not attributed to either side",
                            source, pair)
            elif pnl is None:
                log.warning("TRADE_CLOSE from %r for %r has no measured pnl "
                            "-- outcome not attributed to either side",
                            source, pair)
            elif pnl != 0:
                _signal_aggregator.record_outcome(pair, direction, pnl > 0, pnl)
            # direction present, pnl exactly 0: a flat -- neither win nor
            # loss, nothing to attribute.

            # Feed the decay tracker. SignalDecay.record_outcome had ZERO
            # call sites since it was written: /api/signals/decay served 35
            # half-lives, every one `data_points: 0, source: "default"` —
            # hardcoded constants presented on a measurement endpoint. (The
            # payload at least labelled them "default"; this makes the
            # empirical path real.) Only measured, non-zero outcomes count:
            # an unpriced or flat close says nothing about whether the
            # signal's edge had decayed.
            if (open_info and isinstance(pnl, (int, float)) and pnl != 0):
                _opened = open_info.get("opened_at")
                if isinstance(_opened, (int, float)) and _opened > 0:
                    for _sig in (open_info.get("signals") or []):
                        try:
                            _signal_decay.record_outcome(
                                f"{source}:{_sig}", _opened,
                                time.time(), pnl > 0)
                        except Exception:
                            log.warning("signal_decay.record_outcome failed "
                                        "for %s:%s", source, _sig)
            # No else: the warning that lived here said "has no direction",
            # but this condition is about the DECAY feed -- it fired on every
            # close with no recorded open or a flat P/L, direction present or
            # not (turtlesue's XRP close carried direction=SHORT and still
            # tripped it, 2026-08-13), while an actually-missing direction
            # was skipped in silence above. Direction is warned about at the
            # direction check; a close the decay tracker can't learn from is
            # not a defect.

            # Expectancy only ever heard about trades closed through the
            # reservation-release path. A bot that emits TRADE_CLOSE straight
            # to the bus (Gridzilla's completed grid cycles) reached
            # /api/trades and the aggregator but never the expectancy tracker
            # — so gridzilla booked a real $1,414.54 cycle while
            # /api/expectancy reported "no closed trades yet". The fleet's
            # headline signal-quality number silently excluded a whole class
            # of trade.
            #
            # trade_id is the event id, which the release path never uses
            # (it keys on the reservation id), and record_trade dedups on it —
            # so a close that arrives by both routes is counted once.
            if _expectancy_tracker and isinstance(pnl, (int, float)):
                try:
                    # None, not 0 — same contract as the other two writers.
                    # This is the THIRD record_trade call site in this file and
                    # the one that was still fabricating: after the 2026-08-07
                    # relaunch it wrote four fresh gridzilla rows carrying
                    # entry_price 0 beside real P/L (POL/USD +$162.98,
                    # ETH/USD +$30.34 / +$47.74).
                    _bp = edata.get("entry_price")
                    _sp = edata.get("exit_price")
                    if not isinstance(_sp, (int, float)) or _sp <= 0:
                        _sp = edata.get("price")
                    _expectancy_tracker.record_trade(
                        bot_id=source,
                        pair=pair,
                        direction=direction,
                        entry_price=_bp if isinstance(_bp, (int, float)) and _bp > 0 else None,
                        exit_price=_sp if isinstance(_sp, (int, float)) and _sp > 0 else None,
                        size_usd=edata.get("size_usd", 0),
                        duration=edata.get("duration_s", 0),
                        realized_pnl=pnl,
                        # Dedup key. The release path keys on the RESERVATION
                        # id and this path on the EVENT id, so a close arriving
                        # by both routes was stored twice under different keys —
                        # live proof: two POL/USD rows, identical gross_pnl
                        # 162.9797, one with a real exit price and one zeroed.
                        # Key on the reservation id when the emitting bot
                        # supplies it, so both routes collide as intended.
                        trade_id=(edata.get("reservation_id")
                                  or data.get("id")
                                  or f"{source}:{pair}:{pnl}"),
                    )
                except Exception:
                    log.warning("expectancy.record_trade failed for bus "
                                "TRADE_CLOSE %s:%s", source, pair, exc_info=True)
            if open_info and open_info.get("signals") and pnl is not None:
                sigs = open_info["signals"]
                contrib = [f"{source}:{s}" for s in sigs] if isinstance(sigs, list) else [source]
                _signal_decomposition.log_trade(
                    pair, direction, gross_pnl=pnl,
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
        # float() accepts NaN and ±inf, and Python's json.loads accepts a bare
        # NaN literal by default. Every risk gate below is a `>` comparison,
        # and EVERY comparison against NaN is False — so a NaN amount passes
        # the deployment limit, the per-bot cap, the per-pair cap, the
        # directional cap, the concentration cap AND the size floor, all at
        # once. Verified live on 2026-08-06: the reservation was accepted,
        # deployed() then returned NaN, and /api/portfolio and /api/master
        # both stopped serving entirely because the response could not be
        # encoded. One malformed request disabled every capital control in
        # the fleet and took down the two endpoints every bot polls.
        if not math.isfinite(amount):
            log.error("REJECTED non-finite reserve amount %r from bot=%s "
                      "pair=%s — NaN/inf defeats every risk gate",
                      data.get("amount"), data.get("bot_id"), data.get("pair"))
            self._send_json({"ok": False,
                             "reason": "amount must be a finite number"}, 400)
            return
        if amount <= 0:
            # Every gate is an upper bound, so a negative amount passes all of
            # them and is caught only by the size floor at the very end.
            self._send_json({"ok": False,
                             "reason": f"amount must be positive (got {amount})"}, 400)
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
            # Bots re-claiming capital for an already-open position pass this
            # so the fleet-wide short ban does not strand (and force-close)
            # positions that predate the policy.
            is_reentry=bool(data.get("is_reentry")),
        )

        if _fleet_logger:
            if result["ok"]:
                _fleet_logger.log_portfolio_reserve(
                    data["bot_id"], data["pair"], data["direction"],
                    amount, result.get("reservation_id", ""))
            else:
                # pair/direction/amount are right here in scope and the bus
                # publish below already carries them — the disk log did not,
                # so denial_cost.py discarded every row it wrote.
                _fleet_logger.log_portfolio_denial(
                    data["bot_id"], result.get("reason", ""),
                    pair=data.get("pair"), direction=data.get("direction"),
                    amount=amount)

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
            # A release that realized nothing is not a trade. Grid teardowns,
            # cancelled entries and re-reservations all release capital with
            # pnl=0, and recording each as a closed trade poisons the
            # denominator of every statistic computed from this store: on
            # 2026-08-06 gridzilla read "17 trades, 5.9% win rate" from ONE
            # real +$34.90 cycle and sixteen zero-P/L releases, while the bot
            # itself reported 2 trades at 50%. Same class as the grid
            # half-cycles suppressed in gridzilla.py — this was the other
            # path recording them.
            _priced = isinstance(pnl, (int, float)) and pnl != 0
            if _expectancy_tracker and _priced:
                try:
                    # None, not 0 / not "LONG". This writes to the DURABLE
                    # expectancy store, where a fabricated value outlives the
                    # process and cannot be told apart from a measurement.
                    # This is the path that put entry_price 0.0 / exit_price
                    # 0.0 on gridzilla's real +$34.90 ETH/USD record.
                    # record_trade has an explicit PNL-DRIVEN mode for bots
                    # that report P/L without prices, so None is the intended
                    # value here. A missing direction is likewise unknown, not
                    # LONG -- silently calling every unlabelled close a long
                    # corrupts any later long/short attribution.
                    _ep = data.get("entry_price")
                    _xp = data.get("exit_price")
                    _expectancy_tracker.record_trade(
                        bot_id=res_info["bot_id"],
                        pair=res_info["pair"],
                        direction=res_info.get("direction"),
                        entry_price=_ep if isinstance(_ep, (int, float)) and _ep > 0 else None,
                        exit_price=_xp if isinstance(_xp, (int, float)) and _xp > 0 else None,
                        size_usd=res_info["amount"],
                        duration=_duration,
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
            # A release that realized nothing is a capital movement, not a
            # closed trade — the same rule already applied to the expectancy
            # recording above. Publishing it as TRADE_CLOSE put
            # "TRADE_CLOSE ATOM/USD PnL=$0.00" on the live event stream right
            # after a GRID_KILLED for the same pair: one real event rendered
            # twice, the second time as a trade that never happened. The
            # capital movement is already visible as the release itself.
            if not _priced:
                log.debug("Release %s realized nothing — not publishing "
                          "TRADE_CLOSE (capital movement, not a trade)", rid)
            try:
                _size = float(res_info.get("amount") or 0)
                # Signal product (2026-07-30): fees always 0 — pnl is gross
                # price movement. Key kept so TRADE_CLOSE consumers don't break.
                _fees = 0.0
                if _priced:
                    _event_bus.publish({
                        "source": res_info.get("bot_id", "portfolio"),
                        "type": "TRADE_CLOSE",
                        "data": {
                            "pair": res_info.get("pair", ""),
                            # No LONG default: every reservation carries a
                            # real direction (reserve() rejects without one),
                            # and if that ever breaks the consumers now warn
                            # on absence rather than mislabeling the close.
                            "direction": res_info.get("direction"),
                            # NOT 0 when absent. A release whose bot supplied no
                            # prices is a capital movement, not a priced trade —
                            # defaulting to 0 fabricated closes that render as
                            # "LOSS $+0.00, Entry 0.0, Exit 0.0" on subscriber
                            # cards. 20 of 31 TRADE_CLOSE events on the bus carried
                            # zero/missing prices this way (2026-08-06).
                            # None is honest and lets publishers suppress; the
                            # event still reaches /api/trades and expectancy.
                            "entry_price": data.get("entry_price") or None,
                            "exit_price": data.get("exit_price") or None,
                            "priced": bool(data.get("entry_price")
                                           and data.get("exit_price")),
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

    def _handle_portfolio_confirm(self, data: dict) -> None:
        """POST /api/portfolio/confirm — bot declares the reservation ids it
        still references (lease heartbeat). Owned rids missing from the
        declaration long enough are swept as orphans."""
        bot_id = data.get("bot_id")
        if not bot_id:
            self._send_json({"error": "Missing bot_id"}, 400)
            return
        rids = data.get("reservation_ids")
        if not isinstance(rids, list):
            self._send_json({"error": "reservation_ids must be a list"}, 400)
            return
        # Positions the fleet currently reports, same source the 48h sweep
        # uses. A reservation whose bot still reports the position is never an
        # orphan, whatever the declaration said — an empty declaration is
        # exactly what a bot whose position store failed to load will send.
        with _lock:
            _bots_now = dict(_state["bots"])
        _active = _active_position_keys(_bots_now)
        result = {"ok": True, "confirmed": 0, "swept": []}
        for mgr in [_portfolio_paper, _portfolio_live]:
            if not mgr:
                continue
            r = mgr.confirm(bot_id, rids, active_positions=_active)
            result["confirmed"] += r["confirmed"]
            result["swept"].extend(r["swept"])
        for _sw in result["swept"]:
            log.warning(
                "Lease sweep: released orphaned reservation %s (bot=%s pair=%s $%.2f)",
                _sw["reservation_id"], _sw["bot_id"], _sw["pair"], _sw["amount"])
            _event_bus.publish({
                "source": "portfolio", "type": "PORTFOLIO_LEASE_SWEEP",
                "data": {"bot_id": _sw["bot_id"], "pair": _sw["pair"],
                         "amount": _sw["amount"],
                         "reservation_id": _sw["reservation_id"]},
            })
        # Don't leak full reservation internals to the caller
        result["swept"] = [
            {"reservation_id": s["reservation_id"], "pair": s["pair"], "amount": s["amount"]}
            for s in result["swept"]
        ]
        self._send_json(result, 200)

    def _handle_portfolio_sync(self, data: dict) -> None:
        """POST /api/portfolio/sync — force a Kraken balance refresh.

        Pulls live equity from Kraken (TradeBalance), updates _portfolio_live.total,
        and returns the result. Reservations are preserved. Available balance is
        recomputed from total minus active reservations.
        """
        result = _sync_kraken_balance()
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
        # A proposal that states no confidence has not stated 0.5 — that is a
        # fabricated mid-conviction, and it is weighed against proposals whose
        # confidence was actually computed. None lets the aggregator treat it
        # as unweighted rather than averaging in a number nobody sent.
        _conf = data.get("confidence")
        _ok = _signal_aggregator.submit_proposal(
            source=data.get("source", "unknown"),
            pair=data.get("pair", ""),
            direction=data.get("direction", "NEUTRAL"),
            confidence=_conf if isinstance(_conf, (int, float)) else None,
            metadata=data.get("metadata", {}),
        )
        if _ok is False:
            log.warning("Signal proposal from %r for %r rejected: no numeric "
                        "confidence supplied (got %r)",
                        data.get("source"), data.get("pair"), _conf)
            self._send_json({"status": "rejected",
                             "reason": "confidence must be a number 0.0-1.0"},
                            400)
            return
        self._send_json({"status": "accepted"})

    def _handle_signals_outcome(self, data: dict) -> None:
        """POST /api/signals/outcome — bot reports a closed trade's result.

        Absence is not an outcome. The old defaults ("" direction, won
        False, pnl 0) meant a malformed POST fabricated a directionless
        loss — and a FLAT close is neither win nor loss: turtlesue's
        forced XRP exit (2026-08-13, pnl -0.0 by construction) posted
        here and debited the SHORT hit rate with a trade that measured
        nothing. Bots post unconditionally on every close, so this
        choke point does the gating for the whole fleet.
        """
        pair = data.get("pair")
        _dir = data.get("direction")
        _p = data.get("pnl")
        if not pair or not isinstance(_dir, str) or not _dir:
            self._send_json({"status": "rejected",
                             "reason": "pair and direction required"}, 400)
            return
        if not isinstance(_p, (int, float)) or _p == 0:
            self._send_json({"status": "ignored",
                             "reason": "unmeasured or flat pnl -- "
                                       "neither win nor loss"})
            return
        _signal_aggregator.record_outcome(
            pair=pair,
            direction=_dir.upper(),
            won=_p > 0,
            pnl=_p,
        )
        self._send_json({"status": "recorded"})

    def _handle_expectancy_evict(self, data: dict) -> None:
        """POST /api/expectancy/evict {pair_prefix} — drop in-memory test rows.

        A test that exercises the live release path creates a real trade in
        the tracker. Cleaning only the disk file left Command Center serving
        the phantom from memory, so a probe showed up as a real turtlesue
        result on /api/expectancy and the BOT SCOREBOARD.
        """
        prefix = data.get("pair_prefix") or ""
        if not prefix or len(prefix) < 4:
            self._send_json({"error": "pair_prefix required (min 4 chars)"}, 400)
            return
        removed = _expectancy_tracker.evict_by_pair_prefix(prefix)
        log.info("Expectancy evict: removed %d row(s) matching %r", removed, prefix)
        self._send_json({"ok": True, "removed": removed})

    def _handle_expectancy_repair(self, data: dict) -> None:
        """POST /api/expectancy/repair — null fabricated $0.00 prices.

        Three writers used to persist `entry_price=data.get(..., 0)` into the
        durable store, so trades whose bot reported P/L but no prices claimed
        an entry of $0.00 — a price nobody measured. All three are fixed, but
        the rows they wrote remain, and they cannot be cleaned on disk: this
        tracker loads once at construction and _save() writes the in-memory
        copy, so a file edit under a running process is silently overwritten
        by the next trade.

        Also drops rows that double-count ONE close. The release path keyed
        dedup on the reservation id and the bus path on the event id, so a
        close arriving by both routes was stored twice — which inflates
        total_trades and understates true per-trade expectancy.

        Repairs only: no gross_pnl, size_usd, duration, direction or timestamp
        is ever modified.
        """
        result = _expectancy_tracker.repair_fabricated_prices()
        log.info("Expectancy repair: nulled %d fabricated price field(s), "
                 "dropped %d duplicate row(s)%s",
                 result.get("nulled", 0), result.get("dropped", 0),
                 (" — " + "; ".join(result.get("details") or []))
                 if result.get("details") else "")
        self._send_json({"ok": True, **result})

    def _handle_expectancy_record(self, data: dict) -> None:
        # The public write path into the DURABLE store: any bot can POST here,
        # so it is the most exposed way for a fabricated value to become
        # permanent. A caller that omits prices or direction has not stated
        # 0/LONG. record_trade takes the PNL-DRIVEN branch when realized_pnl
        # is supplied and only reads direction on the price-driven branch, so
        # None is both safe and honest.
        _ep = data.get("entry_price")
        _xp = data.get("exit_price")
        _ep = _ep if isinstance(_ep, (int, float)) and _ep > 0 else None
        _xp = _xp if isinstance(_xp, (int, float)) and _xp > 0 else None
        _rp = data.get("realized_pnl")
        _rp = _rp if isinstance(_rp, (int, float)) else None

        # realized_pnl was never forwarded, so a caller that reported P/L
        # without prices had it silently dropped and booked $0.00 into the
        # durable store. With neither prices nor P/L there is nothing to
        # record at all — refuse rather than persist a zero that will be
        # averaged into every statistic computed from this store.
        if _rp is None and (_ep is None or _xp is None):
            self._send_json({"status": "rejected",
                             "reason": "need realized_pnl, or both "
                                       "entry_price and exit_price"}, 400)
            return

        _expectancy_tracker.record_trade(
            bot_id=data.get("bot_id", "unknown"),
            pair=data.get("pair", ""),
            direction=data.get("direction"),
            entry_price=_ep,
            exit_price=_xp,
            size_usd=data.get("size_usd", 0),
            duration=data.get("duration", 0),
            realized_pnl=_rp,
            # fee_rate intentionally not forwarded — expectancy is gross
            # (signal product, 2026-07-30); record_trade ignores fees.
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
# bot_id -> True once we've logged that its directory is missing, so the health
# monitor reports the failure once instead of every cooldown cycle.
_missing_dir_logged: dict[str, bool] = {}
try:
    _cd = _load_cc_state().get("restart_cooldowns", {})
    if isinstance(_cd, dict):
        _restart_cooldowns = {k: float(v) for k, v in _cd.items()
                              if isinstance(v, (int, float, str))}
except Exception:
    _restart_cooldowns = {}
# An unreadable state file is not "no bot was recently restarted". The lookup
# below defaults a missing entry to 0 — a 1970 timestamp, i.e. maximally
# stale — so every bot reads as eligible for restart immediately. That turns a
# truncated file into a fleet-wide restart storm, on exactly the boot most
# likely to have truncated it. Unknown means recently-restarted: arm the
# cooldown for every registered bot rather than disarming it for all of them.
if _cc_state_unreadable:
    _now_boot = time.time()
    _restart_cooldowns = {b["id"]: _now_boot for b in BOT_REGISTRY}
    log.error("CC state unreadable — arming restart cooldowns for all %d bots "
              "(unknown restart history must not read as 'never restarted').",
              len(_restart_cooldowns))
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
                if bcfg and bcfg.get("dir") and not os.path.isdir(bcfg["dir"]):
                    # Bot directory is gone (e.g. TrekBot removed from disk).
                    # Popen would raise NotADirectoryError every cooldown forever,
                    # so mark it permanently down instead of retrying.
                    if not _missing_dir_logged.get(bid):
                        log.error("Cannot restart %s — directory missing: %s (will not retry)",
                                  bid, bcfg["dir"])
                        _missing_dir_logged[bid] = True
                    continue
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
    # No boot-time stale sweep: at boot no bot has been polled yet, so an
    # age-only sweep here is blind to live positions — the exact failure that
    # released Confluence's 66h swing reservations (2026-07-30). The hourly
    # position-aware sweep in the poll loop and the rid-level lease sweep
    # (/api/portfolio/confirm) cover real orphans instead.
    print(f"  Paper Portfolio: ${_portfolio_paper.available():,.2f} available of ${_portfolio_paper.total:,.2f}")

    _live_file = _fleet_config.PORTFOLIO_LIVE_FILE
    if os.path.exists(_live_file):
        _portfolio_live = PortfolioManager(0, PORTFOLIO_LIMITS, _live_file, mode_tag="live")
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
            # Same contract as _handle_event_publish's TRADE_CLOSE path: an
            # absent pnl is not a $0.00 loss, and an unlabelled close is not
            # a long. This bridge had the ORIGINAL defect set after the HTTP
            # path was fixed — the fix-one-sibling shape again (2026-08-13).
            _praw = data.get("pnl")
            pnl = _praw if isinstance(_praw, (int, float)) else None
            _cdir = data.get("direction")
            direction = _cdir.upper() if isinstance(_cdir, str) and _cdir else None
            if direction is None:
                log.warning("TRADE_CLOSE (logger bridge) from %r for %r has "
                            "no direction -- outcome not attributed to "
                            "either side", bot, pair)
            elif pnl is None:
                log.warning("TRADE_CLOSE (logger bridge) from %r for %r has "
                            "no measured pnl -- outcome not attributed to "
                            "either side", bot, pair)
            elif pnl != 0:
                _signal_aggregator.record_outcome(pair, direction, pnl > 0, pnl)
            if open_info and open_info.get("signals") and pnl is not None:
                sigs = open_info["signals"]
                contrib = [f"{bot}:{s}" for s in sigs] if isinstance(sigs, list) else [bot]
                _signal_decomposition.log_trade(
                    pair, direction, gross_pnl=pnl,
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
