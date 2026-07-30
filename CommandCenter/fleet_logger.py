#!/usr/bin/env python3
"""
FLEET LOGGER — Persistent Performance Data for Command Center
==============================================================
Background service that writes structured logs to disk.

Three layers:
  1. Snapshots  — full /api/master state every 60s (JSONL)
  2. Events     — real-time trade/signal/regime changes (JSONL)
  3. Daily      — midnight summary report (JSON)

Usage: imported and started by command_center.py
"""

import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import urllib.request as _urlreq

INFERENCE_URL = "http://localhost:9001"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
SNAPSHOT_DIR = os.path.join(LOG_DIR, "snapshots")
EVENT_DIR = os.path.join(LOG_DIR, "events")
DAILY_DIR = os.path.join(LOG_DIR, "daily")
# In-progress daily accumulator checkpoint — persisted on every mutation so a
# mid-day reboot does not lose the day's running counters. Distinct from
# DAILY_DIR which holds finalized midnight-flush summaries.
DAILY_STATE_FILE = os.path.join(LOG_DIR, "daily_state.json")

SNAPSHOT_INTERVAL = 60      # seconds between snapshots
RETENTION_SNAPSHOTS = 90    # days
RETENTION_EVENTS = 365      # days
RETENTION_DAILY = 365       # days


# ---------------------------------------------------------------------------
# JSON safety (mirrors command_center.py)
# ---------------------------------------------------------------------------

def _json_default(obj):
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return 0.0
    try:
        import numpy as np
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            v = float(obj)
            return 0.0 if math.isinf(v) or math.isnan(v) else v
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.bool_):
            return bool(obj)
    except ImportError:
        pass
    return str(obj)


def _sanitize_floats(obj):
    """Replace inf/nan floats with 0.0. json.dumps default= doesn't handle native floats."""
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_floats(v) for v in obj]
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return 0.0
    return obj


def _safe_json(obj):
    return json.dumps(obj, default=_json_default, allow_nan=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ensure_dirs():
    for d in (LOG_DIR, SNAPSHOT_DIR, EVENT_DIR, DAILY_DIR):
        os.makedirs(d, exist_ok=True)


def _append_jsonl(directory, line_dict):
    """Append a single JSON line to today's JSONL file. Atomic per-line."""
    path = os.path.join(directory, f"{_today_str()}.jsonl")
    line = _safe_json(_sanitize_floats(line_dict)) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()


def _rotate_old_files(directory, max_days):
    """Delete files older than max_days based on filename date."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    try:
        for fname in os.listdir(directory):
            fpath = os.path.join(directory, fname)
            if not os.path.isfile(fpath):
                continue
            date_part = fname.split(".")[0]  # "2025-03-15" from "2025-03-15.jsonl"
            try:
                file_date = datetime.strptime(date_part, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if file_date < cutoff:
                    os.remove(fpath)
            except ValueError:
                continue  # filename doesn't match date pattern
    except OSError:
        pass


JOURNAL_DIR = os.path.join(LOG_DIR, "journals")


def _auto_journal(trade_event):
    """Background: POST trade to AI inference server, save journal to disk."""
    try:
        payload = json.dumps({"trade": trade_event}).encode("utf-8")
        req = _urlreq.Request(f"{INFERENCE_URL}/api/ai/trade-journal",
                              data=payload,
                              headers={"Content-Type": "application/json"})
        resp = _urlreq.urlopen(req, timeout=30)
        journal = json.loads(resp.read().decode("utf-8"))

        os.makedirs(JOURNAL_DIR, exist_ok=True)
        path = os.path.join(JOURNAL_DIR, f"{_today_str()}.jsonl")
        entry = {"timestamp": time.time(), "trade": trade_event, "ai": journal}
        with open(path, "a", encoding="utf-8") as f:
            f.write(_safe_json(entry) + "\n")
    except Exception:
        pass  # inference server down — silent fail


# ---------------------------------------------------------------------------
# FleetLogger
# ---------------------------------------------------------------------------

class FleetLogger:
    """
    Reads from Command Center's in-memory state (passed via callback),
    writes structured logs to disk. Runs in its own daemon thread.
    """

    def __init__(self, get_state_fn, get_portfolio_fn, on_event=None):
        """
        Args:
            get_state_fn:  callable returning dict with keys: bots, aggregate, feed
            get_portfolio_fn: callable returning portfolio state dict (or None)
            on_event: optional callback(event_dict) for publishing events to the bus
        """
        self._get_state = get_state_fn
        self._get_portfolio = get_portfolio_fn
        self._on_event = on_event

        # Thread pool for AI journal calls (bounded to avoid overloading Ollama)
        self._journal_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="Journal")

        # Previous state for diff-based event detection
        self._prev_bots = {}        # bot_id -> normalized dict
        self._prev_positions = {}   # bot_id -> set of position keys
        self._prev_regimes = {}     # bot_id -> confirmed regime string
        self._pending_regimes = {}  # bot_id -> {"regime": str, "count": int}
        self._prev_whale_tiers = {} # pair -> tier string
        self._prev_alive = {}       # bot_id -> bool

        # Daily accumulator (resets at midnight UTC)
        self._daily = self._empty_daily()
        self._daily_date = _today_str()
        self._daily_lock = threading.Lock()

        # Track starting equity for daily summary
        self._starting_equity = None

        _ensure_dirs()

        # Rehydrate in-progress daily state from disk. Done last so any
        # persisted values override the blank defaults above.
        self._load_daily_state()

    # ── Daily accumulator ──

    def _empty_daily(self):
        return {
            "trades": [],           # list of trade event dicts
            "regime_changes": 0,
            "whale_alerts": 0,
            "portfolio_denials": 0,
            "status_changes": [],   # bot online/offline transitions
            "pnl_by_bot": {},       # bot_id -> running pnl
        }

    def _save_daily_state(self):
        """Atomic write of the in-progress daily accumulator.

        Called after every mutation so a reboot mid-day picks up exactly where
        we left off. Uses tmp+os.replace for atomicity. Disk errors are
        silently swallowed — daily-stats persistence is best-effort and must
        not break live event routing.
        """
        try:
            os.makedirs(os.path.dirname(DAILY_STATE_FILE), exist_ok=True)
            with self._daily_lock:
                snap = {
                    "date": self._daily_date,
                    "daily": self._daily,
                    "starting_equity": self._starting_equity,
                }
            tmp = DAILY_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, default=str)
            os.replace(tmp, DAILY_STATE_FILE)
        except Exception:
            pass

    def _load_daily_state(self):
        """Rehydrate the daily accumulator on startup if the state file is from today.

        If the persisted date is older than today, ignore it — the day has
        rolled over during the downtime and a fresh accumulator is correct.
        """
        try:
            if not os.path.exists(DAILY_STATE_FILE):
                return
            with open(DAILY_STATE_FILE, "r", encoding="utf-8") as fh:
                snap = json.load(fh)
            if not isinstance(snap, dict):
                return
            persisted_date = snap.get("date")
            today = _today_str()
            if persisted_date != today:
                # Day rolled over while we were down; leave fresh state in place.
                return
            daily = snap.get("daily")
            if isinstance(daily, dict):
                # Merge keys from persisted state, keeping schema stable if the
                # persisted file is from an older schema version.
                merged = self._empty_daily()
                for k in merged:
                    if k in daily:
                        merged[k] = daily[k]
                self._daily = merged
                self._daily_date = persisted_date
                se = snap.get("starting_equity")
                if se is not None:
                    self._starting_equity = se
        except Exception:
            pass

    def _check_day_rollover(self):
        """If the UTC date changed, finalize yesterday and reset."""
        today = _today_str()
        with self._daily_lock:
            if today == self._daily_date:
                return
            old_date = self._daily_date
            old_daily = self._daily.copy()
            old_trades = list(self._daily["trades"])
            self._daily = self._empty_daily()
            self._daily_date = today
            self._starting_equity = None
        # Write summary outside the lock using the snapshot
        self._write_daily_summary(old_date, old_daily, old_trades)
        # Checkpoint the fresh accumulator so a reboot right after rollover
        # picks up today's blank state, not yesterday's stale file.
        self._save_daily_state()

    def get_daily_accumulator(self):
        """Return current daily stats for the dashboard sidebar."""
        with self._daily_lock:
            d = self._daily
            trades = d["trades"]
            wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
            losses = sum(1 for t in trades if t.get("pnl", 0) <= 0)
            total_pnl = sum(t.get("pnl", 0) for t in trades)

            # Per-bot PnL for best/worst
            bot_pnl = {}
            for t in trades:
                bid = t.get("bot", "")
                bot_pnl[bid] = bot_pnl.get(bid, 0) + t.get("pnl", 0)

            best_bot = max(bot_pnl.items(), key=lambda x: x[1]) if bot_pnl else None
            worst_bot = min(bot_pnl.items(), key=lambda x: x[1]) if bot_pnl else None

            return {
                "pnl": round(total_pnl, 2),
                "trades": len(trades),
                "wins": wins,
                "losses": losses,
                "best_bot": {"id": best_bot[0], "pnl": round(best_bot[1], 2)} if best_bot else None,
                "worst_bot": {"id": worst_bot[0], "pnl": round(worst_bot[1], 2)} if worst_bot else None,
                "regime_changes": d["regime_changes"],
                "whale_alerts": d["whale_alerts"],
                "portfolio_denials": d["portfolio_denials"],
            }

    # ── Event logging (called externally for portfolio events) ──

    def log_portfolio_denial(self, bot_id, reason):
        """Called by command_center when a reserve is denied."""
        event = {
            "ts": time.time(),
            "type": "PORTFOLIO",
            "event": "RESERVE_DENIED",
            "bot": bot_id,
            "reason": reason,
        }
        _append_jsonl(EVENT_DIR, event)
        with self._daily_lock:
            self._daily["portfolio_denials"] += 1
        self._save_daily_state()

    def log_portfolio_reserve(self, bot_id, pair, direction, amount, reservation_id):
        """Called by command_center when a reserve succeeds."""
        event = {
            "ts": time.time(),
            "type": "PORTFOLIO",
            "event": "RESERVE",
            "bot": bot_id,
            "pair": pair,
            "direction": direction,
            "amount": amount,
            "reservation_id": reservation_id,
        }
        _append_jsonl(EVENT_DIR, event)

    def log_portfolio_release(self, bot_id, pair, amount, pnl, reservation_id):
        """Called by command_center when a release happens.

        Gross semantics (2026-07-30): pnl IS gross price movement — the
        fleet is a signal product and pays no fees. gross_pnl == pnl and
        fees is 0.0; both keys kept so event-log readers don't break.
        (Pre-2026-07-30 events on disk carry fabricated fees and inflated
        gross_pnl — history is not rewritten.)
        """
        event = {
            "ts": time.time(),
            "type": "PORTFOLIO",
            "event": "RELEASE",
            "bot": bot_id,
            "pair": pair,
            "amount": amount,
            "pnl": pnl,
            "gross_pnl": round(pnl, 2),
            "fees": 0.0,
            "reservation_id": reservation_id,
        }
        _append_jsonl(EVENT_DIR, event)

    # ── Snapshot diffing — detect events ──

    def _detect_events(self, bots_data):
        """Compare current bot state against previous, emit events for changes."""
        events = []

        for bid, bot in bots_data.items():
            norm = bot.get("normalized") or {}
            raw = bot.get("raw") or {}
            alive = bot.get("alive", False)

            # --- Online/Offline transitions ---
            prev_alive = self._prev_alive.get(bid)
            if prev_alive is not None and prev_alive != alive:
                ev = {
                    "ts": time.time(),
                    "bot": bid,
                    "type": "STATUS_CHANGE",
                    "from": "ONLINE" if prev_alive else "OFFLINE",
                    "to": "ONLINE" if alive else "OFFLINE",
                }
                events.append(ev)
                with self._daily_lock:
                    self._daily["status_changes"].append(ev)
            self._prev_alive[bid] = alive

            if not alive:
                continue

            # --- Position changes (trade open/close) ---
            current_positions = self._extract_positions(bid, raw, norm)
            prev_positions = self._prev_positions.get(bid, {})

            # New positions = TRADE_OPEN
            for key, pos in current_positions.items():
                if key not in prev_positions:
                    ev = {
                        "ts": time.time(),
                        "bot": bid,
                        "type": "TRADE_OPEN",
                        "pair": pos.get("pair", key),
                        "direction": pos.get("direction", "LONG"),
                        "entry": pos.get("entry", 0),
                        "size": pos.get("size", 0),
                    }
                    events.append(ev)

            # Removed positions = TRADE_CLOSE
            for key, pos in prev_positions.items():
                if key not in current_positions:
                    pair_name = pos.get("pair", key)
                    pnl = pos.get("unrealized_pnl", 0)
                    open_time = pos.get("open_time", 0)
                    exit_reason = "unknown"

                    # Try to get real exit reason and PnL from bot's trade log
                    exit_info = self._find_trade_close(bid, pair_name, raw)
                    if exit_info:
                        exit_reason = exit_info.get("exit_reason", exit_reason)
                        if exit_info.get("pnl") is not None:
                            pnl = exit_info["pnl"]

                    duration = time.time() - open_time if open_time > 0 else 0
                    # Gross semantics (2026-07-30): pnl IS gross price
                    # movement — no fee fabrication. fees kept at 0.0 for
                    # event-shape compatibility.
                    _size = pos.get("total_size", 0) or pos.get("size_usd", 0) or pos.get("amount", 500)
                    _entry = pos.get("entry_price", 0) or pos.get("avg_entry", 0)
                    if _entry > 0 and _size > 0:
                        _size_usd = _size * _entry if _size < 100 else _size  # handle coin qty vs usd
                    else:
                        _size_usd = 500  # fallback
                    ev = {
                        "ts": time.time(),
                        "bot": bid,
                        "type": "TRADE_CLOSE",
                        "pair": pair_name,
                        "pnl": pnl,
                        "gross_pnl": round(pnl, 2),
                        "fees": 0.0,
                        "size_usd": round(_size_usd, 2),
                        "duration_s": duration,
                        "exit_reason": exit_reason,
                        "direction": pos.get("direction", exit_info.get("direction", "") if exit_info else ""),
                    }
                    events.append(ev)
                    with self._daily_lock:
                        self._daily["trades"].append(ev)

                    # Auto-journal via AI inference (non-blocking, bounded pool)
                    self._journal_pool.submit(_auto_journal, ev)

            self._prev_positions[bid] = current_positions

            # --- Regime changes (with stability filter: 3 consecutive scans) ---
            regime = norm.get("regime")
            confirmed = self._prev_regimes.get(bid)
            if regime and confirmed and regime != confirmed:
                pending = self._pending_regimes.get(bid)
                if pending and pending["regime"] == regime:
                    pending["count"] += 1
                else:
                    self._pending_regimes[bid] = {"regime": regime, "count": 1}
                    pending = self._pending_regimes[bid]
                if pending["count"] >= 3:
                    ev = {
                        "ts": time.time(),
                        "bot": bid,
                        "type": "REGIME_CHANGE",
                        "from": confirmed,
                        "to": regime,
                    }
                    events.append(ev)
                    with self._daily_lock:
                        self._daily["regime_changes"] += 1
                    self._prev_regimes[bid] = regime
                    self._pending_regimes.pop(bid, None)
            else:
                # Regime matches confirmed or is None — clear any pending
                self._pending_regimes.pop(bid, None)
                if regime:
                    self._prev_regimes[bid] = regime

            # --- Deep Blue whale tier changes ---
            if bid == "deepblue":
                whales = raw.get("whales") if isinstance(raw.get("whales"), list) else []
                for whale in whales:
                    pair = whale.get("pair", "")
                    score = whale.get("whaleScore", 0)
                    if score >= 80:
                        tier = "EXTREME"
                    elif score >= 65:
                        tier = "HIGH"
                    else:
                        continue  # only log actionable whales

                    prev_tier = self._prev_whale_tiers.get(pair)
                    if prev_tier != tier:
                        ev = {
                            "ts": time.time(),
                            "bot": "deepblue",
                            "type": "WHALE_ALERT",
                            "pair": pair,
                            "tier": tier,
                            "score": score,
                        }
                        events.append(ev)
                        with self._daily_lock:
                            self._daily["whale_alerts"] += 1
                        self._prev_whale_tiers[pair] = tier

            self._prev_bots[bid] = norm

        # Write all events and publish to event bus
        for ev in events:
            _append_jsonl(EVENT_DIR, ev)
            if self._on_event and ev.get("type") in (
                "TRADE_OPEN", "TRADE_CLOSE", "REGIME_CHANGE", "WHALE_ALERT",
            ):
                try:
                    self._on_event({
                        "source": ev.get("bot", "fleet_logger"),
                        "type": ev["type"],
                        "data": {k: v for k, v in ev.items()
                                 if k not in ("type",)},
                    })
                except Exception:
                    pass  # never let bus errors break logging

        # Checkpoint the daily accumulator once per detection pass. Any of the
        # blocks above (status_changes, trades, regime_changes, whale_alerts)
        # may have mutated self._daily — persist the result so a mid-day reboot
        # preserves today's running counters instead of losing them.
        if events:
            self._save_daily_state()

        return events

    def _extract_positions(self, bid, raw, norm):
        """Extract a dict of position_key -> position_info from raw bot data."""
        positions = {}

        if bid == "turtlesue":
            for pair, pos in (raw.get("positions") or {}).items():
                positions[pair] = {
                    "pair": pair,
                    "direction": pos.get("direction", pos.get("side", "LONG")).upper(),
                    "entry": pos.get("avg_entry", pos.get("entry_price", 0)),
                    "size": pos.get("total_size", pos.get("size", pos.get("size_usd", 0))),
                    "unrealized_pnl": pos.get("unrealized_pnl", 0),
                    "open_time": pos.get("opened_at", pos.get("open_time", time.time())),
                }

        elif bid == "nexusbrain":
            for pos in (raw.get("open_positions") or []):
                pair = pos.get("pair", pos.get("symbol", ""))
                positions[pair] = {
                    "pair": pair,
                    "direction": pos.get("direction", pos.get("side", "LONG")).upper(),
                    "entry": pos.get("entry_price", pos.get("entry", 0)),
                    "size": pos.get("size", pos.get("size_usd", pos.get("amount", 0))),
                    "unrealized_pnl": pos.get("unrealized_pnl", pos.get("pnl", 0)),
                    "open_time": pos.get("open_time", time.time()),
                }

        elif bid == "trekbot":
            for pos in (raw.get("positions") or []):
                pair = pos.get("symbol", pos.get("pair", ""))
                opened = pos.get("opened_at", pos.get("open_time", 0))
                key = f"{pair}_{int(opened)}"
                positions[key] = {
                    "pair": pair,
                    "direction": pos.get("direction", "LONG").upper(),
                    "entry": pos.get("entry_price", pos.get("entry", 0)),
                    "size": pos.get("size", pos.get("size_usd", 0)),
                    "unrealized_pnl": pos.get("unrealized_pnl", pos.get("pnl", 0)),
                    "open_time": opened,
                }

        elif bid == "gridzilla":
            for pos in (raw.get("open_positions") or []):
                pair = pos.get("pair", pos.get("symbol", ""))
                key = f"{pair}_{pos.get('grid_level', '')}"
                positions[key] = {
                    "pair": pair,
                    "direction": pos.get("direction", pos.get("side", "LONG")).upper(),
                    "entry": pos.get("entry_price", pos.get("entry", 0)),
                    "size": pos.get("size", pos.get("size_usd", 0)),
                    "unrealized_pnl": pos.get("unrealized_pnl", pos.get("pnl", 0)),
                    "open_time": pos.get("open_time", time.time()),
                }

        elif bid == "rubberband":
            # Rubberband exposes positions as a dict keyed by pair under "positions"
            rb_pos = raw.get("positions") or {}
            if isinstance(rb_pos, dict):
                for pair, pos in rb_pos.items():
                    if isinstance(pos, dict):
                        positions[pair] = {
                            "pair": pos.get("pair", pair),
                            "direction": pos.get("direction", "LONG").upper(),
                            "entry": pos.get("entry_price", pos.get("entry", 0)),
                            "size": pos.get("size_usd", pos.get("size", 0)),
                            "unrealized_pnl": pos.get("unrealized_pnl", pos.get("pnl", 0)),
                            "open_time": pos.get("opened_at", pos.get("open_time", time.time())),
                        }

        elif bid == "arbitrageur":
            # open_spreads_detail (list) preferred, fallback to open_positions (list)
            open_pos = raw.get("open_spreads_detail") or raw.get("open_positions") or []
            if isinstance(open_pos, list):
                for pos in open_pos:
                    if isinstance(pos, dict):
                        pair_key = pos.get("pair_key", pos.get("pair", ""))
                        positions[pair_key] = {
                            "pair": pair_key,
                            "direction": pos.get("direction", "LONG").upper(),
                            "entry": pos.get("entry_z", pos.get("entry", 0)),
                            "size": pos.get("size", pos.get("size_usd", 0)),
                            "unrealized_pnl": pos.get("unrealized_pnl", pos.get("pnl", 0)),
                            "open_time": pos.get("open_time", time.time()),
                        }
            # If open_spreads is an int count, position diffing can't work.

        return positions

    def _find_trade_close(self, bid, pair, raw):
        """Look up real exit reason and PnL from a bot's trade log.

        Returns dict with 'exit_reason' and 'pnl', or None if not found.
        Searches the most recent trade_log entries for a matching pair.
        """
        trade_log = None

        if bid == "trekbot":
            # TrekBot: raw = {"health": {}, "positions": [], "analytics": {}}
            analytics = raw.get("analytics") or {}
            trade_log = analytics.get("trade_log") or []
            for tr in reversed(trade_log):
                sym = tr.get("sym", tr.get("pair", ""))
                if sym == pair:
                    return {
                        "exit_reason": tr.get("exit", "unknown"),
                        "pnl": tr.get("pnl"),
                    }

        elif bid == "turtlesue":
            # TurtleSue: raw.trades = list of dicts with pair, exit_reason, pnl
            trade_log = raw.get("trades") or []
            for tr in reversed(trade_log):
                if tr.get("pair") == pair:
                    return {
                        "exit_reason": tr.get("exit_reason", "unknown"),
                        "pnl": tr.get("pnl"),
                    }

        elif bid == "nexusbrain":
            # NexusBrain: raw.recent_trades = list with pair, exit_reason, pnl_usd
            trade_log = raw.get("recent_trades") or []
            for tr in reversed(trade_log):
                if tr.get("pair") == pair:
                    return {
                        "exit_reason": tr.get("exit_reason", "unknown"),
                        "pnl": tr.get("pnl_usd"),
                    }

        elif bid == "gridzilla":
            # Gridzilla: raw.trade_history = list with pair, status, pnl
            trade_log = raw.get("trade_history") or []
            for tr in reversed(trade_log):
                if tr.get("pair") == pair:
                    return {
                        "exit_reason": tr.get("status", "unknown"),
                        "pnl": tr.get("pnl"),
                    }

        elif bid == "rubberband":
            # Rubberband: raw.recent_trades = list with pair, exit_reason, pnl
            trade_log = raw.get("recent_trades") or []
            for tr in reversed(trade_log):
                if tr.get("pair") == pair:
                    return {
                        "exit_reason": tr.get("exit_reason", "unknown"),
                        "pnl": tr.get("pnl"),
                    }

        elif bid == "arbitrageur":
            # Arbitrageur: raw.closed_recent = list with pair_key/pair_a/pair_b, exit_reason, pnl
            trade_log = raw.get("closed_recent") or []
            for tr in reversed(trade_log):
                if tr.get("pair_key") == pair or tr.get("pair_a") == pair or tr.get("pair_b") == pair:
                    return {
                        "exit_reason": tr.get("exit_reason", "unknown"),
                        "pnl": tr.get("pnl"),
                    }

        return None

    # ── Snapshot writing ──

    def _write_snapshot(self, state, portfolio):
        """Write full fleet state as one JSONL line."""
        snapshot = {
            "ts": time.time(),
            "aggregate": state.get("aggregate", {}),
            "portfolio": portfolio,
            "bots": {},
        }
        for bid, bot in state.get("bots", {}).items():
            snapshot["bots"][bid] = {
                "alive": bot.get("alive", False),
                "latency_ms": bot.get("latency_ms"),
                "normalized": bot.get("normalized"),
            }
        _append_jsonl(SNAPSHOT_DIR, snapshot)

    # ── Daily summary ──

    def _write_daily_summary(self, date_str, daily_data=None, trades_list=None):
        """Generate and write the daily summary JSON for a given date.

        Args:
            date_str: The date being summarized.
            daily_data: Pre-snapshotted daily accumulator (avoids re-acquiring lock).
            trades_list: Pre-snapshotted trades list.
        """
        if daily_data is not None:
            d = daily_data
            trades = trades_list or []
        else:
            with self._daily_lock:
                d = self._daily.copy()
                trades = list(d["trades"])

        # Get current state for ending equity
        try:
            state = self._get_state()
            portfolio = self._get_portfolio()
        except Exception:
            state = {}
            portfolio = None

        ending_equity = 0
        if portfolio:
            ending_equity = portfolio.get("total", 0)

        starting_equity = self._starting_equity or ending_equity

        # Per-bot stats
        per_bot = {}
        for t in trades:
            bid = t.get("bot", "unknown")
            if bid not in per_bot:
                per_bot[bid] = {
                    "trades": 0, "wins": 0, "losses": 0, "pnl": 0,
                    "hold_times": [], "worst_trade": None,
                }
            b = per_bot[bid]
            b["trades"] += 1
            pnl = t.get("pnl", 0)
            b["pnl"] += pnl
            if pnl > 0:
                b["wins"] += 1
            else:
                b["losses"] += 1
            dur = t.get("duration_s", 0)
            b["hold_times"].append(dur)
            if b["worst_trade"] is None or pnl < b["worst_trade"].get("pnl", 0):
                b["worst_trade"] = {"pair": t.get("pair", ""), "pnl": round(pnl, 2)}

        # Finalize per-bot
        per_bot_final = {}
        for bid, b in per_bot.items():
            avg_hold = (sum(b["hold_times"]) / len(b["hold_times"])) if b["hold_times"] else 0
            per_bot_final[bid] = {
                "trades": b["trades"],
                "wins": b["wins"],
                "losses": b["losses"],
                "pnl": round(b["pnl"], 2),
                "avg_hold_time_s": round(avg_hold),
                "worst_trade": b["worst_trade"],
            }

        # Uptime: count status changes
        uptime = {}
        for sc in d.get("status_changes", []):
            bid = sc.get("bot", "")
            if bid not in uptime:
                uptime[bid] = {"offline_transitions": 0}
            if sc.get("to") == "OFFLINE":
                uptime[bid]["offline_transitions"] += 1

        # Fleet totals
        total_trades = len(trades)
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = total_trades - wins
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        bot_pnls = {bid: b["pnl"] for bid, b in per_bot_final.items()}
        best = max(bot_pnls.items(), key=lambda x: x[1]) if bot_pnls else ("none", 0)
        worst = min(bot_pnls.items(), key=lambda x: x[1]) if bot_pnls else ("none", 0)

        summary = {
            "date": date_str,
            "fleet": {
                "starting_equity": round(starting_equity, 2),
                "ending_equity": round(ending_equity, 2),
                "daily_pnl": round(total_pnl, 2),
                "total_trades": total_trades,
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 1),
                "best_bot": {"id": best[0], "pnl": round(best[1], 2)},
                "worst_bot": {"id": worst[0], "pnl": round(worst[1], 2)},
                "regime_changes": d["regime_changes"],
                "whale_alerts": d["whale_alerts"],
                "portfolio_denials": d["portfolio_denials"],
            },
            "per_bot": per_bot_final,
            "uptime": uptime,
        }

        path = os.path.join(DAILY_DIR, f"{date_str}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, default=_json_default)
        os.replace(tmp, path)

    # ── Main loop ──

    def _tick(self):
        """One logger cycle: snapshot + event detection."""
        self._check_day_rollover()

        try:
            state = self._get_state()
            portfolio_state = self._get_portfolio()
        except Exception:
            return

        bots = state.get("bots", {})

        # Capture starting equity on first tick of the day
        if self._starting_equity is None and portfolio_state:
            self._starting_equity = portfolio_state.get("total", 0)

        # Detect events from state diff
        self._detect_events(bots)

        # Write snapshot
        self._write_snapshot(state, portfolio_state)

    def _run(self):
        """Background loop — runs forever."""
        while True:
            try:
                self._tick()
            except Exception as e:
                # Log errors but never crash
                try:
                    _append_jsonl(EVENT_DIR, {
                        "ts": time.time(),
                        "type": "LOGGER_ERROR",
                        "error": str(e),
                    })
                except Exception:
                    pass
            time.sleep(SNAPSHOT_INTERVAL)

    def start(self):
        """Start the logger in a daemon thread."""
        # Rotate old files on startup
        _rotate_old_files(SNAPSHOT_DIR, RETENTION_SNAPSHOTS)
        _rotate_old_files(EVENT_DIR, RETENTION_EVENTS)
        _rotate_old_files(DAILY_DIR, RETENTION_DAILY)

        t = threading.Thread(target=self._run, daemon=True, name="FleetLogger")
        t.start()
        return t
