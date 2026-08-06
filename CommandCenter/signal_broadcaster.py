"""
Signal Broadcaster — Two-tier Telegram signal service for the trading fleet.
=============================================================================

Consumes events from Command Center's SSE stream (with polling fallback),
enriches them with fleet intelligence, routes to free/paid tiers, formats
as Telegram HTML cards, and delivers via the Telegram Bot API.

Architecture:
    SSEListener -> IntelligenceBuilder -> TierRouter -> CardFormatter -> ChannelOps

Stdlib + urllib only. No pip installs required.
"""

import base64
import collections
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Callable, Optional

# ── Bot display names — internal names never shown to subscribers ────────────

BOT_DISPLAY_NAMES = {
    'confluence':    'Concord',
    'turtlesue':     'Stalker',
    'turtlebot':     'Stalker',
    'nexusbrain':    'Prism',
    'nexus_brain':   'Prism',
    'oracle':        'Atlas',
    'deepblue':      'Leviathan',
    'deep_blue':     'Leviathan',
    'gridzilla':     'Ironweb',
    'nexus':         'The Council',
    'aegis':         'Sovereign',
    'sentinel':      'Watcher',
    'trinity':       'Trident',
    'hivemind':      'Chorus',
    'hive_mind':     'Chorus',
    'phitex':        'Pulse',
    'rubberband':    'Slingshot',
    'contrarian':    'Heretic',
    'arbitrageur':   'Ghost',
    'chronos':       'Meridian',
    'inference':     'Inference',
}


def display_name(internal: str) -> str:
    """Map internal bot name to public display name."""
    return BOT_DISPLAY_NAMES.get(internal.lower().replace('-', '_'), internal.title())


# ── Logging ──────────────────────────────────────────────────────────────────
# Log in UTC with an explicit Z. Python's %(asctime)s defaults to LOCAL time
# and says so nowhere, which on 2026-08-05 made a scheduler verification read
# as wrong: "DailySummaryJob started, next in 46650s" logged at 13:02:30 does
# not reach 08:00 — but 13:02:30 was Mountain, and 19:02:30 UTC + 46650s is
# exactly 08:00:00. The job was right; the frame was unlabeled.
#
# Every timestamp this fleet emits is compared against a UTC schedule, so the
# log must be in the same frame as the thing it describes. The Z is not
# decoration — it is what stops the next reader inferring a frame.
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("signal_broadcaster")


# ═══════════════════════════════════════════════════════════════════════════════
# TRANSPORT LAYER
# ═══════════════════════════════════════════════════════════════════════════════

class SSEListener:
    """Connects to Command Center's SSE event stream and delivers events via callback.

    Reads ``data: {...}`` lines from /api/events/stream. Heartbeat lines
    (starting with ``:``) are silently consumed. On disconnect: exponential
    backoff (2^failures seconds, capped at 60s).
    """

    def __init__(self, cc_url: str, on_event: Callable[[dict], None],
                 on_disconnect: Optional[Callable] = None,
                 on_reconnect: Optional[Callable] = None):
        self._cc_url = cc_url.rstrip("/")
        self._on_event = on_event
        self._on_disconnect = on_disconnect or (lambda: None)
        self._on_reconnect = on_reconnect or (lambda: None)
        self._shutdown = threading.Event()
        self._connected = False
        self._reconnects = 0
        self._events_received = 0
        self._last_event_ts: Optional[float] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        t = threading.Thread(target=self._run, daemon=True, name="sse-listener")
        t.start()

    def stop(self) -> None:
        self._shutdown.set()

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def stats(self) -> dict:
        with self._lock:
            return {
                "connected": self._connected,
                "reconnects": self._reconnects,
                "events_received": self._events_received,
                "last_event_ts": self._last_event_ts,
            }

    def _run(self) -> None:
        failures = 0
        url = f"{self._cc_url}/api/events/stream"

        while not self._shutdown.is_set():
            resp = None
            try:
                req = urllib.request.Request(url)
                resp = urllib.request.urlopen(req, timeout=300)

                with self._lock:
                    self._connected = True
                if failures > 0:
                    with self._lock:
                        self._reconnects += 1
                    failures = 0
                    try:
                        self._on_reconnect()
                    except Exception:
                        log.warning("on_reconnect callback error", exc_info=True)
                log.info("SSE connected to %s", url)

                for raw_line in resp:
                    if self._shutdown.is_set():
                        break
                    try:
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    except Exception:
                        continue

                    if line.startswith(":") or not line:
                        continue

                    if line.startswith("data: "):
                        payload = line[6:]
                        try:
                            event = json.loads(payload)
                            with self._lock:
                                self._events_received += 1
                                self._last_event_ts = time.time()
                            self._on_event(event)
                        except json.JSONDecodeError:
                            log.debug("SSE non-JSON data line: %.100s", payload)
                        except Exception:
                            log.warning("on_event callback error", exc_info=True)

            except Exception as e:
                log.warning("SSE connection lost: %s", e)
            finally:
                if resp:
                    try:
                        resp.close()
                    except Exception:
                        pass

                with self._lock:
                    was_connected = self._connected
                    self._connected = False

                if was_connected:
                    try:
                        self._on_disconnect()
                    except Exception:
                        log.warning("on_disconnect callback error", exc_info=True)

            # Backoff before retry (outside finally)
            failures += 1
            backoff = min(2 ** failures, 60)
            log.info("SSE reconnecting in %ds (failure #%d)", backoff, failures)
            if self._shutdown.wait(backoff):
                break


class PollingFallback:
    """Polls /api/events/recent when the SSE stream is down.

    Only polls while ``_active`` is True. Tracks seen event IDs in a bounded
    deque to avoid re-delivering events.
    """

    def __init__(self, cc_url: str, on_event: Callable[[dict], None],
                 poll_interval: int = 30):
        self._cc_url = cc_url.rstrip("/")
        self._on_event = on_event
        self._poll_interval = poll_interval
        self._active = False
        self._shutdown = threading.Event()
        self._seen_ids: collections.deque = collections.deque(maxlen=1000)
        self._polls = 0
        self._events_delivered = 0
        self._last_poll_ts: Optional[float] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        t = threading.Thread(target=self._run, daemon=True, name="poll-fallback")
        t.start()

    def stop(self) -> None:
        self._shutdown.set()

    def set_active(self, active: bool) -> None:
        with self._lock:
            self._active = active
        if active:
            log.info("Polling fallback ACTIVATED")
        else:
            log.info("Polling fallback deactivated (SSE restored)")

    def stats(self) -> dict:
        with self._lock:
            return {
                "active": self._active,
                "polls": self._polls,
                "events_delivered": self._events_delivered,
                "last_poll_ts": self._last_poll_ts,
            }

    def _run(self) -> None:
        failures = 0
        while not self._shutdown.is_set():
            wait_time = self._poll_interval if failures == 0 else min(
                self._poll_interval * (2 ** failures), 120
            )
            if self._shutdown.wait(wait_time):
                break

            with self._lock:
                active = self._active
            if not active:
                failures = 0
                continue

            url = f"{self._cc_url}/api/events/recent?n=200"
            try:
                req = urllib.request.Request(url)
                resp = urllib.request.urlopen(req, timeout=10)
                raw = resp.read()
                events = json.loads(raw)

                if isinstance(events, dict):
                    events = events.get("events", [])
                if not isinstance(events, list):
                    events = []

                with self._lock:
                    self._polls += 1
                    self._last_poll_ts = time.time()

                delivered = 0
                for event in events:
                    eid = event.get("id")
                    if not eid:
                        eid = f"{event.get('source', '')}_{event.get('type', '')}_{event.get('ts', '')}"
                    if eid in self._seen_ids:
                        continue
                    self._seen_ids.append(eid)
                    try:
                        self._on_event(event)
                        delivered += 1
                    except Exception:
                        log.warning("on_event callback error (poll)", exc_info=True)

                with self._lock:
                    self._events_delivered += delivered
                failures = 0

            except Exception as e:
                failures += 1
                if failures <= 3 or failures % 10 == 0:
                    log.warning("Poll failed (attempt %d): %s", failures, e)


class BroadcasterHealthServer:
    """Minimal HTTP health/stats server on its own port.

    GET /health -> {"status": "ok", "uptime": ..., "sse_connected": ...}
    GET /stats  -> combined stats from all components
    POST /reload -> triggers config reload
    """

    def __init__(self, port: int, get_stats: Callable[[], dict],
                 on_reload: Callable[[], None],
                 get_feed: Callable[[int], list] = None):
        self._port = port
        self._get_stats = get_stats
        self._on_reload = on_reload
        self._get_feed = get_feed or (lambda n: [])
        self._start_time = time.time()
        self._server: Optional[HTTPServer] = None

    def start(self) -> None:
        try:
            from port_guard import ensure_port
            ensure_port(self._port, "signal_broadcaster")
        except ImportError:
            log.warning("port_guard not available, skipping port cleanup")
        except Exception as e:
            log.warning("port_guard failed: %s", e)

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                try:
                    if self.path == "/health":
                        stats = parent._get_stats()
                        body = json.dumps({
                            "status": "ok",
                            "uptime": round(time.time() - parent._start_time, 1),
                            "sse_connected": stats.get("sse", {}).get("connected", False),
                        })
                        self._respond(200, body)
                    elif self.path == "/stats":
                        body = json.dumps(parent._get_stats(), default=str)
                        self._respond(200, body)
                    elif self.path.startswith("/feed"):
                        body = json.dumps(parent._get_feed(50), default=str)
                        self._respond(200, body)
                    else:
                        self._respond(404, json.dumps({"error": "not found"}))
                except Exception:
                    self._respond(500, json.dumps({"error": "internal"}))

            def do_POST(self):
                try:
                    if self.path == "/reload":
                        parent._on_reload()
                        self._respond(200, json.dumps({"status": "reloaded"}))
                    else:
                        self._respond(404, json.dumps({"error": "not found"}))
                except Exception:
                    self._respond(500, json.dumps({"error": "internal"}))

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def _respond(self, code: int, body: str):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

        # ThreadingHTTPServer: a browser keep-alive connection (dashboard
        # panel polling /stats + /feed) must not block the CC watchdog's
        # /health probe — a blocked probe gets this process taskkilled.
        class _ExclusiveServer(ThreadingHTTPServer):
            # HTTPServer sets SO_REUSEADDR, which on Windows lets a second
            # process bind the same port. Two watchdogs (CC health monitor
            # + launch_fleet) can each spawn a broadcaster within seconds
            # of each other; with a shared bind both consume SSE and every
            # Telegram signal goes out twice. Exclusive bind makes the
            # second instance fail fast instead.
            allow_reuse_address = False

        try:
            self._server = _ExclusiveServer(("0.0.0.0", self._port), Handler)
        except OSError as e:
            log.critical("Port %d already bound — another broadcaster "
                         "instance is running, exiting: %s", self._port, e)
            sys.exit(0)
        t = threading.Thread(target=self._server.serve_forever, daemon=True,
                             name="health-server")
        t.start()
        log.info("Health server listening on port %d", self._port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()


# ═══════════════════════════════════════════════════════════════════════════════
# INTELLIGENCE + ROUTING LAYER
# ═══════════════════════════════════════════════════════════════════════════════

class IntelligenceBuilder:
    """Enriches raw event-bus events with Command Center API data.

    Each event type triggers specific API lookups. Results are cached with a
    configurable TTL. Never raises — on failure, returns event copy with
    enrichment_failed=True.
    """

    def __init__(self, cc_url: str = "http://localhost:9000",
                 cache_ttl: int = 15, fetch_timeout: int = 5):
        self._cc_url = cc_url.rstrip("/")
        self._cache_ttl = cache_ttl
        self._fetch_timeout = fetch_timeout
        self._cache: dict[str, tuple[float, object]] = {}

    def _fetch(self, path: str):
        url = f"{self._cc_url}{path}"
        now = time.time()
        cached = self._cache.get(url)
        if cached and cached[0] > now:
            return cached[1]
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self._fetch_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._cache[url] = (now + self._cache_ttl, data)
            return data
        except Exception as exc:
            log.debug("Fetch failed for %s: %s", path, exc)
            return None

    def _extract_pair(self, event: dict) -> str:
        data = event.get("data", {})
        if isinstance(data, dict):
            return data.get("pair", "unknown")
        return "unknown"

    def _enrich_high_conviction(self, enriched: dict) -> None:
        pair = self._extract_pair(enriched)
        if pair != "unknown":
            decide = self._fetch(f"/api/signals/decide?pair={pair}")
            if decide:
                enriched["ensemble_score"] = decide.get("score", decide.get("confidence"))
                enriched["ensemble_direction"] = decide.get("direction", decide.get("decision"))
        decomp = self._fetch("/api/signals/decomposition")
        if decomp:
            enriched["signal_attribution"] = decomp if isinstance(decomp, list) else decomp.get("signals", decomp.get("sources", []))
        decay = self._fetch("/api/signals/decay")
        if decay:
            enriched["signal_freshness"] = decay

    def _enrich_trade(self, enriched: dict) -> None:
        data = enriched.get("data", {})
        if not isinstance(data, dict):
            data = {}

        expectancy = self._fetch("/api/expectancy")
        if expectancy:
            enriched["fleet_expectancy"] = expectancy.get("fleet_expectancy", expectancy.get("expectancy_per_trade"))
            bot = data.get("source", data.get("bot", ""))
            bot_stats = expectancy.get("bot_stats", {})
            if bot and bot in bot_stats:
                enriched["bot_expectancy"] = bot_stats[bot].get("expectancy_per_trade")

        # Pull intelligence: conviction score + top signals from /api/signals/intel
        pair = data.get("pair", "")
        if pair:
            intel = self._fetch(f"/api/signals/intel?pair={pair}")
            if intel and isinstance(intel, dict):
                enriched["intel_score"] = intel.get("score", intel.get("intel_score"))
                enriched["conviction"] = intel.get("conviction", intel.get("score"))
                # Extract top contributing signals if present
                signals = intel.get("signals", intel.get("top_signals", intel.get("contributors", [])))
                if isinstance(signals, list) and signals:
                    enriched["top_signals"] = signals[:3]
                elif isinstance(signals, dict):
                    enriched["top_signals"] = list(signals.keys())[:3]

            # Ensemble decision — direction confirmation + score
            decide = self._fetch(f"/api/signals/decide?pair={pair}")
            if decide and isinstance(decide, dict):
                enriched["ensemble_score"] = decide.get("score", decide.get("confidence"))
                enriched["ensemble_direction"] = decide.get("direction", decide.get("decision"))

        # AEGIS regime context — pull from /api/master
        master = self._fetch("/api/master")
        if master and isinstance(master, dict):
            agg = master.get("aggregate", {})
            enriched["fleet_regime"] = agg.get("regime_consensus", enriched.get("_fleet_regime"))
            enriched["bots_alive"] = agg.get("bots_alive", enriched.get("_bots_alive"))
            portfolio = master.get("portfolio", {})
            enriched["deployed_pct"] = portfolio.get("deployed_pct", enriched.get("_deployed_pct"))
            # AEGIS score — bots is a list in /api/master, search for aegis entry
            bots_list = master.get("bots", [])
            if isinstance(bots_list, list):
                for _b in bots_list:
                    if isinstance(_b, dict) and _b.get("id", "").lower() == "aegis":
                        enriched["aegis_score"] = _b.get("score", _b.get("aegis_score"))
                        break
            elif isinstance(bots_list, dict):
                aegis_data = bots_list.get("aegis", {})
                if isinstance(aegis_data, dict):
                    enriched["aegis_score"] = aegis_data.get("score", aegis_data.get("aegis_score"))

        # Pull live position details from the bot's snapshot (entry, stop, size)
        bot = data.get("source", data.get("bot", ""))
        if bot and pair:
            snapshot = self._fetch(f"/api/bot/{bot}")
            if snapshot and isinstance(snapshot, dict):
                # Find position in snapshot — different bots use different formats
                positions = snapshot.get("positions", snapshot.get("open_positions", {}))
                pos = None
                if isinstance(positions, dict):
                    # TurtleSue: positions keyed by pair (AAVEUSD)
                    pair_key = pair.replace("/", "")
                    pos = positions.get(pair_key, positions.get(pair))
                elif isinstance(positions, list):
                    # TrekBot/others: list of position dicts
                    for p in positions:
                        if p.get("pair", p.get("symbol", "")) in (pair, pair.replace("/", "")):
                            pos = p
                            break
                if pos and isinstance(pos, dict):
                    if "current_stop" in pos:
                        data["stop_loss"] = pos["current_stop"]
                    elif "stop" in pos:
                        data["stop_loss"] = pos["stop"]
                    if "avg_entry" in pos and "entry_price" not in data:
                        data["entry_price"] = pos["avg_entry"]
                    elif "entry_price" in pos and "entry_price" not in data:
                        data["entry_price"] = pos["entry_price"]
                    if "total_size" in pos and "size" not in data:
                        data["size"] = pos["total_size"]
                    if "unrealized_pnl" in pos:
                        data["unrealized_pnl"] = pos["unrealized_pnl"]
                    # Conviction from position if bot exposes it
                    if "conviction" in pos and "conviction" not in enriched:
                        enriched["conviction"] = pos["conviction"]
                    # Top signals from position
                    if "signals" in pos and "top_signals" not in enriched:
                        pos_signals = pos["signals"]
                        if isinstance(pos_signals, list):
                            enriched["top_signals"] = pos_signals[:3]
                        elif isinstance(pos_signals, dict):
                            enriched["top_signals"] = list(pos_signals.keys())[:3]

    def _enrich_trade_close(self, enriched: dict) -> None:
        self._enrich_trade(enriched)
        decomp = self._fetch("/api/signals/decomposition")
        if decomp:
            enriched["signal_attribution"] = decomp if isinstance(decomp, list) else decomp.get("signals", decomp.get("sources", []))

    def _enrich_emergency_reduce(self, enriched: dict) -> None:
        portfolio = self._fetch("/api/portfolio")
        if portfolio:
            enriched["portfolio_deployed_pct"] = portfolio.get("deployed_pct", 0)
            enriched["portfolio_cash"] = portfolio.get("available", portfolio.get("cash", 0))

    def _enrich_catastrophe_warning(self, enriched: dict) -> None:
        pair = self._extract_pair(enriched)
        if pair != "unknown":
            intel = self._fetch(f"/api/signals/intel?pair={pair}")
            if intel:
                enriched["intel_score"] = intel.get("score", intel.get("intel_score"))

    def _enrich_whale_alert(self, enriched: dict) -> None:
        data = enriched.get("data", {})
        if isinstance(data, dict):
            # Normalize Deep Blue's "tier" field → "magnitude" expected by TierRouter/CardFormatter
            if "magnitude" not in data and "tier" in data:
                tier = str(data["tier"]).upper()
                data["magnitude"] = tier  # HIGH → HIGH, EXTREME → EXTREME
            # Also normalize volume_usd from score if absent
            if "volume_usd" not in data and "score" in data:
                data["volume_usd"] = int(data["score"] * 10000)  # rough estimate

        pair = self._extract_pair(enriched)
        if pair != "unknown":
            ticker = self._fetch("/api/market/ticker")
            if ticker and isinstance(ticker, dict):
                price_entry = ticker.get(pair, {})
                if isinstance(price_entry, dict):
                    enriched["current_price"] = price_entry.get("last", price_entry.get("price"))
                elif isinstance(price_entry, (int, float)):
                    enriched["current_price"] = price_entry

    def _enrich_aegis_update(self, enriched: dict) -> None:
        data = enriched.get("data", {})
        if isinstance(data, dict):
            regime = data.get("regime", data.get("fleet_regime", ""))
            enriched["aegis_defensive"] = "DEFENSIVE" in str(regime).upper()
        else:
            enriched["aegis_defensive"] = False

    _ENRICHERS = {
        "HIGH_CONVICTION": _enrich_high_conviction,
        "TRADE_OPEN": _enrich_trade,
        "TRADE_CLOSE": _enrich_trade_close,
        "EMERGENCY_REDUCE": _enrich_emergency_reduce,
        "CATASTROPHE_WARNING": _enrich_catastrophe_warning,
        "WHALE_ALERT": _enrich_whale_alert,
        "AEGIS_UPDATE": _enrich_aegis_update,
    }

    def _enrich_universal(self, enriched: dict) -> None:
        """Add live market + fleet context to every event."""
        data = enriched.get("data", {})
        pair = data.get("pair") if isinstance(data, dict) else None

        # Current price for any event with a pair
        if pair:
            ticker = self._fetch("/api/market/ticker")
            if ticker and isinstance(ticker, dict):
                entry = ticker.get(pair, {})
                if isinstance(entry, dict):
                    price = entry.get("price", entry.get("last"))
                    enriched["_price"] = price
                    enriched["_high"] = entry.get("high")
                    enriched["_low"] = entry.get("low")
                    enriched["_vol"] = entry.get("volume")
                elif isinstance(entry, (int, float)):
                    enriched["_price"] = entry

        # Fleet posture
        master = self._fetch("/api/master")
        if master:
            agg = master.get("aggregate", {})
            enriched["_fleet_pnl"] = agg.get("total_pnl")
            enriched["_fleet_regime"] = agg.get("regime_consensus")
            enriched["_bots_alive"] = agg.get("bots_alive")
            enriched["_open_positions"] = agg.get("total_open_positions")
            portfolio = master.get("portfolio", {})
            enriched["_deployed_pct"] = portfolio.get("deployed_pct")
            enriched["_available"] = portfolio.get("available")

            # AEGIS defensive flag — set on every event so the caution banner
            # fires on HIGH_CONVICTION, SIGNAL, TRADE_OPEN when fleet is defensive
            fleet_regime = str(agg.get("regime_consensus", "")).upper()
            if "DEFENSIVE" in fleet_regime:
                enriched["aegis_defensive"] = True
            enriched["_risk_status"] = portfolio.get("risk_status")

    def enrich(self, event: dict) -> dict:
        try:
            enriched = copy.deepcopy(event)
            event_type = event.get("type", "")
            # Universal enrichment first
            self._enrich_universal(enriched)
            # Type-specific enrichment
            enricher = self._ENRICHERS.get(event_type)
            if enricher:
                enricher(self, enriched)
            enriched["enriched_at"] = time.time()
            return enriched
        except Exception as exc:
            log.warning("Enrichment failed for %s: %s", event.get("type", "?"), exc)
            try:
                fallback = copy.deepcopy(event)
            except Exception:
                fallback = dict(event)
            fallback["enrichment_failed"] = True
            fallback["enriched_at"] = time.time()
            return fallback


class TierRouter:
    """Pure routing logic. No state, no network calls, no side effects.

    Returns a routing decision dict or None if the event should be suppressed.
    """

    @staticmethod
    def route(event: dict, config: dict) -> Optional[dict]:
        event_type = event.get("type", "")
        data = event.get("data", {}) if isinstance(event.get("data"), dict) else {}
        source = event.get("source", data.get("source", "unknown"))
        pair = data.get("pair", "fleet")

        min_conviction = config.get("min_conviction_threshold", 0.8)
        free_delay_s = int(config.get("free_delay_hours", 4) * 3600)
        overrides = config.get("routing_overrides", {})

        # ═══════════════════════════════════════════════════════════════
        # STRICT TELEGRAM WHITELIST — "is this worth a pocket pull?"
        # ═══════════════════════════════════════════════════════════════
        # A signal reaches Telegram only if a human would *act* on it:
        # open a trade, close a trade, rebalance, or stop the fleet.
        # Everything else — physics council chatter, AEGIS heartbeats,
        # "the system detected X" updates — stays on the internal event
        # bus for the dashboard and is silent on Telegram.
        #
        # Whitelist:
        #   TRADE_OPEN          — a bot just opened a real position
        #   TRADE_CLOSE         — a bot just closed a real position
        #   HIGH_CONVICTION     — confidence >= 0.85 AND not a duplicate
        #   EMERGENCY_REDUCE    — fleet-wide risk-off
        #   REGIME_CHANGE       — AEGIS regime actually flipped (deduped)
        #   WHALE_ALERT         — only EXTREME magnitude on a held pair
        #   CATASTROPHE_WARNING — ews_score >= 0.75 (was 0.6)
        #
        # Blacklist (returns None, never fires):
        #   CHAOS_STATE, MANIFOLD_WARNING, STRUCTURE_FORMING,
        #   CYCLE_DETECTED, CAUSAL_FLOW, EUCLID_LEVEL, BOOK_PHASE,
        #   FLEET_ALERT, AEGIS_UPDATE, SHANNON_ENTROPY, QUANTUM_COLLAPSE,
        #   BOLTZMANN, THOM, NEWTON_FORCE, NEWTON_REACTION, NEXUS_UPDATE,
        #   PHITEX_UPDATE, SIGNAL (raw bot proposals), and everything
        #   else not explicitly listed above.
        #
        # Override the whitelist per-event via reactions.json → routing_overrides.

        # REGIME_CHANGE used to collapse to the bare type, so all ten
        # regime-emitting bots shared ONE dedup bucket and the 3600s window
        # let exactly one card through per hour — a clock, not a signal
        # (2026-08-05: 93% of a week's sends were REGIME_CHANGE at ~61min
        # intervals). Now it keys by source like every other type, and the
        # AEGIS-only gate below is what actually controls volume.
        _FLEET_DEDUP_TYPES: set = set()
        if event_type in _FLEET_DEDUP_TYPES:
            dedup_key = event_type
        else:
            dedup_key = f"{event_type}_{pair}_{source}"
        result = None

        # TRADE_* are free=True TEMPORARILY (2026-08-05). These are the
        # highest-value cards and were going nowhere: routed paid-only while
        # telegram_paid_chat_id is unset, so ChannelOps.send_paid dropped them
        # (156 on 2026-07-28 alone, incl. all 58 TRADE_OPEN). Until a real paid
        # channel exists, they go to free so subscribers see them at all.
        # delay_free_s=0 deliberately: a 4h-delayed trade signal is worthless.
        # REVERT to free=False once telegram_paid_chat_id is set and verified
        # with getChat — this makes paid-tier content free.
        if event_type == "TRADE_OPEN":
            result = {"free": True, "paid": True, "priority": 2,
                      "category": "trade", "delay_free_s": 0}

        elif event_type == "TRADE_CLOSE":
            # Suppress unpriced closes. A capital release with no entry/exit
            # renders as "Result ✖ LOSS $+0.00 / Entry 0.0 / Exit 0.0" — a
            # loss of zero with no prices, which is internally contradictory
            # and cannot be defended to a subscriber. CC no longer fabricates
            # 0 for these (it sends None + priced:false), but bots can still
            # emit their own zero-price closes, so check the values rather
            # than trusting the flag.
            _ep = data.get("entry_price")
            _xp = data.get("exit_price")
            _priced = data.get("priced")
            if _priced is False or not _ep or not _xp:
                log.info("suppressing unpriced TRADE_CLOSE %s (entry=%r "
                         "exit=%r) — capital movement, not a priced trade",
                         pair, _ep, _xp)
                return None
            result = {"free": True, "paid": True, "priority": 2,
                      "category": "trade", "delay_free_s": 0}

        elif event_type == "HIGH_CONVICTION":
            # Raise the bar: 0.80 was too loose and tripped often.
            #
            # Coerce before comparing. Reaction-generated events arrive via
            # reactions.json template substitution, which stringifies every
            # value — so confidence can be "1.0", and `"1.0" >= 0.85` raises
            # TypeError inside the router rather than suppressing the event.
            # A crash here is worse than the silent drop it replaces: it takes
            # out the whole routing call for that event.
            conf = data.get("confidence", data.get("conviction", 0)) or 0
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                log.warning("HIGH_CONVICTION: non-numeric confidence %r from "
                            "%s — treating as 0", conf, source)
                conf = 0.0
            if conf >= 0.85:
                result = {"free": True, "paid": True, "priority": 1,
                          "category": "trade", "delay_free_s": 0}
            else:
                return None

        elif event_type == "EMERGENCY_REDUCE":
            result = {"free": True, "paid": True, "priority": 1,
                      "category": "risk", "delay_free_s": 0}

        elif event_type == "REGIME_CHANGE":
            # AEGIS ONLY. This comment used to claim "only fires when AEGIS
            # actually flips" while the code accepted any bot's regime event
            # — ten bots with ten unrelated vocabularies (trinity
            # TRENDING/RANGING per pair, nexus SYSTEMIC/MIXED, contrarian
            # NEUTRAL/FEAR, chronos session_overlap/high_activity) all landed
            # in one bucket. AEGIS is the fleet risk authority; its
            # NORMAL/CAUTIOUS/DEFENSIVE posture is the only regime a
            # subscriber can act on. Everything else stays on the bus for
            # the dashboard.
            if str(source).lower() != "aegis":
                return None
            frm = data.get("from") or data.get("old_regime")
            to = data.get("to") or data.get("new_regime") or data.get("regime")
            # A "change" that doesn't change is not news.
            if frm is not None and to is not None and str(frm) == str(to):
                return None
            result = {"free": True, "paid": True, "priority": 2,
                      "category": "regime", "delay_free_s": 0,
                      "hysteresis_key": f"regime_{source}",
                      "hysteresis_state": f"{frm}>{to}"}

        elif event_type == "WHALE_ALERT":
            # Strict: EXTREME magnitude AND the fleet must actually hold
            # the pair. A whale dumping something we don't own is news-
            # paper reading; a whale hitting our position is a decision.
            #
            # If the event carries no held_pairs list (which will be the
            # common case because the whale emitter doesn't know fleet
            # state), we read the live deployment from CC and only fire
            # when deployed_pct > 0 AND the pair is in the by_pair map.
            # If we can't verify either way, we SUPPRESS — "unknown" =
            # "don't page the phone."
            magnitude = str(data.get("magnitude", "")).upper()
            if magnitude != "EXTREME":
                return None
            held_pairs = {str(p).upper() for p in
                          (data.get("held_pairs") or
                           data.get("fleet_held_pairs") or [])}
            pair_u = str(pair).upper()
            if held_pairs and pair_u in held_pairs:
                result = {"free": False, "paid": True, "priority": 1,
                          "category": "whale", "delay_free_s": 0}
            else:
                # No held_pairs in the event → suppress by default.
                # (Runtime enrichment can override this in the main
                # handler by adding held_pairs to the event data before
                # routing, using a live /api/portfolio read.)
                return None

        elif event_type == "CATASTROPHE_WARNING":
            # Raised threshold from 0.6 to 0.75 — fewer false alarms.
            ews = data.get("ews_score", 0) or 0
            if ews >= 0.75:
                result = {"free": True, "paid": True, "priority": 1,
                          "category": "risk", "delay_free_s": 0}
            else:
                return None

        else:
            # Everything not explicitly whitelisted above is silent.
            # Physics council, AEGIS heartbeats, fleet alerts, raw SIGNAL
            # proposals, Newton/Nexus/Phitex updates, book phase shifts,
            # manifold warnings, chaos state, cycle detections, causal
            # flow, Shannon entropy, structure forming — all stay on the
            # internal event bus for the dashboard and do not page the
            # user's phone.
            return None

        # Apply per-type overrides from config
        type_override = overrides.get(event_type)
        if type_override and isinstance(type_override, dict):
            for key in ("free", "paid", "priority", "category", "delay_free_s"):
                if key in type_override:
                    result[key] = type_override[key]

        if not result["free"] and not result["paid"]:
            return None

        result["dedup_key"] = dedup_key
        result["event"] = event
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# FORMATTING + DELIVERY LAYER
# ═══════════════════════════════════════════════════════════════════════════════

# Engine display names for paid cards
_ENGINE_NAMES = {
    "CATASTROPHE_WARNING": "Thom Catastrophe Theory",
    "CYCLE_DETECTED": "Persistent Homology",
    "CAUSAL_FLOW": "Granger Causality",
    "MANIFOLD_WARNING": "Fisher Information Geometry",
    "BOOK_PHASE": "Boltzmann Statistical Mechanics",
    "STRUCTURE_FORMING": "Prigogine Dissipative Structures",
    "CHAOS_STATE": "Lorenz Strange Attractor",
    "EUCLID_LEVEL": "Euclid Support/Resistance",
    "SHANNON_ENTROPY": "Shannon Information Theory",
    "QUANTUM_COLLAPSE": "Quantum State Collapse",
}

_FOOTER_PAID = "GoldenEye Intelligence"
_FOOTER_FREE = "Fleet Pulse"
_LINE  = "\u2501" * 32   # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_LINE2 = "\u2500" * 32   # ────────────────────────────────  (thin divider)


def _human_regime(regime) -> str:
    """Translate internal regime labels to human-readable prose.
    Source of truth is card_renderer._human_regime — keep in sync.
    """
    return {
        "TRENDING":       "Trending (bullish bias)",
        "TRENDING_UP":    "Strong uptrend",
        "TRENDING_DOWN":  "Downtrend",
        "BULL":           "Bullish",
        "BEAR":           "Bearish",
        "RANGING":        "Range-bound (sideways)",
        "NORMAL":         "Neutral",
        "DEFENSIVE":      "Defensive (risk-off)",
        "CAUTIOUS":       "Cautious",
        "EQUILIBRIUM":    "Balanced",
        "MIXED":          "Mixed signals",
        "EXTREME_FEAR":   "Extreme fear",
        "EXTREME_GREED":  "Extreme greed",
        "HIGH_ACTIVITY":  "High activity",
    }.get((regime or "").upper().replace(" ", "_"), (regime or "Unknown").replace("_", " ").title())


def format_whale_narrator(data: dict):
    """GoldenEye narrator voice for WHALE_ALERT events.
    Returns (pulse_text, intel_text) tuple.
    pulse_text  — plain narrator for Fleet Pulse (simple channel).
    intel_text  — narrator + structured detail block for Fleet Intelligence.
    """
    pair = data.get("pair", "Unknown")
    tier = str(data.get("tier", "")).upper()
    volume = data.get("volume", 0) or 0
    price = data.get("price", 0) or 0
    regime = data.get("regime", "")
    risk_level = data.get("risk_level", "GREEN")
    deployed_pct = data.get("deployed_pct", 0) or 0
    bot_count = data.get("bot_count", 0) or 0

    # Volume formatter
    if volume >= 1_000_000:
        vol_str = f"${volume / 1_000_000:.1f}M"
    elif volume > 0:
        vol_str = f"${volume:,.0f}"
    else:
        vol_str = "significant volume"

    # Risk word
    risk_word = {"GREEN": "low", "YELLOW": "elevated", "RED": "high"}.get(risk_level, "unknown")

    # Magnitude-specific narrator
    if tier == "EXTREME":
        header = f"\U0001f40b Whale Activity \u2014 {pair}"
        narrator = f"Very large capital movement detected \u2014 {vol_str} flowing through {pair}."
        reg_up = (regime or "").upper()
        if reg_up in ("TRENDING", "TRENDING_UP", "BULL"):
            narrator += " In the current uptrend, this suggests institutional players building momentum positions."
        elif reg_up in ("BEAR", "TRENDING_DOWN"):
            narrator += " With the market trending down, this could be capitulation or smart money accumulating at lower prices."
        elif reg_up in ("RANGING", "NORMAL", "EQUILIBRIUM"):
            narrator += " In a ranging market, moves this size can trigger breakouts. Worth watching closely."
        else:
            narrator += " The fleet is tracking this for potential impact."
        narrator += f"\n\nFleet status: risk is {risk_word}, {deployed_pct:.0f}% of capital deployed."
    else:  # HIGH or fallback
        header = f"\U0001f40b Whale Activity \u2014 {pair}"
        narrator = f"Large players are moving {vol_str} in {pair}"
        if price > 0:
            narrator += f" at ${price:g}"
        narrator += "."
        reg_up = (regime or "").upper()
        if reg_up in ("TRENDING", "TRENDING_UP", "BULL"):
            narrator += " Trending market \u2014 likely momentum positioning rather than a reversal signal."
        elif reg_up in ("BEAR", "TRENDING_DOWN"):
            narrator += " Bearish conditions \u2014 could be profit-taking or a new short building."
        elif reg_up in ("RANGING", "NORMAL", "EQUILIBRIUM"):
            narrator += " In this range-bound market, whale activity often precedes direction."
        else:
            narrator += " The fleet is monitoring for follow-through."
        narrator += f"\n\n{bot_count} bots online, risk {risk_word}. No action needed."

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC \u00b7 %d %b %Y")

    pulse_text = f"{header}\n\n{narrator}\n\nFleet Pulse \u00b7 {ts}"

    # Intelligence detail block
    risk_emoji = {"GREEN": "\u2705", "YELLOW": "\u26a0\ufe0f", "RED": "\U0001f534"}.get(risk_level, "")
    available = data.get("available_capital", 0) or 0
    intel_details = [
        f"\u25b8 Regime: {_human_regime(regime)}",
        f"\u25b8 Risk: {risk_level} {risk_emoji}",
        f"\u25b8 Fleet: {bot_count} bots \u00b7 {deployed_pct:.0f}% deployed \u00b7 ${available:,.0f} available",
        f"\u25b8 Magnitude: {tier} \u00b7 Volume: {vol_str}",
    ]
    intel_text = (f"{header}\n\n{narrator}\n\n"
                  + "\n".join(intel_details)
                  + f"\n\nFleet Intelligence \u00b7 {ts}")

    return pulse_text, intel_text


def format_regime_or_aegis_narrator(data: dict, event_type: str):
    """Single method handling both REGIME_CHANGE and AEGIS_UPDATE narrator cards.
    Branches narrator content at the paragraph level.
    Returns (pulse_text, intel_text).
    AEGIS_UPDATE: pulse_text is None (Intelligence-only event).
    REGIME_CHANGE: both channels receive a message.
    """
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC \u00b7 %d %b %Y")
    risk_level = data.get("risk_level", "GREEN")
    deployed_pct = data.get("deployed_pct", 0) or 0
    bot_count = data.get("bot_count", 0) or 0
    risk_emoji = {"GREEN": "\u2705", "YELLOW": "\u26a0\ufe0f", "RED": "\U0001f534"}.get(risk_level, "")
    risk_word = {"GREEN": "low", "YELLOW": "elevated", "RED": "high"}.get(risk_level, "unknown")

    if event_type == "AEGIS_UPDATE":
        # AEGIS health update — Intelligence only
        score = data.get("score", 0) or 0
        regime_label = data.get("regime", "UNKNOWN")
        max_deploy = data.get("max_deploy") or data.get("recommended_max_deployed", 0) or 0
        h_entropy = data.get("h_entropy") or data.get("consensus_entropy", 0) or 0
        what_would_help = data.get("what_would_help") or []

        header = "\U0001f6e1\ufe0f Fleet Health Update"

        if score >= 0.5:
            health_word = "strong"
        elif score >= 0.35:
            health_word = "healthy"
        elif score >= 0.25:
            health_word = "cautious"
        else:
            health_word = "defensive"

        narrator = f"Fleet health is {health_word} at {score:.3f} ({regime_label})."
        narrator += f" Currently {deployed_pct:.0f}% deployed with a {max_deploy:.0f}% cap."

        if what_would_help:
            first_help = (str(what_would_help[0]).lower()
                          if isinstance(what_would_help, list)
                          else str(what_would_help).lower())
            narrator += f"\n\nTo improve: {first_help}"

        intel_details = [
            f"\u25b8 Score: {score:.3f} \u00b7 Regime: {regime_label}",
            f"\u25b8 Consensus: {h_entropy:.3f} \u00b7 Deploy cap: {max_deploy:.0f}%",
            f"\u25b8 Deployed: {deployed_pct:.0f}%",
        ]
        intel_text = (f"{header}\n\n{narrator}\n\n"
                      + "\n".join(intel_details)
                      + f"\n\nFleet Intelligence \u00b7 {ts}")
        return None, intel_text  # None = do not send to Fleet Pulse

    else:  # REGIME_CHANGE
        pair = data.get("pair") or "FLEET"
        source_bot = data.get("source_bot") or data.get("source") or "The fleet"
        old_regime = data.get("old_regime") or data.get("from") or ""
        new_regime = data.get("new_regime") or data.get("to") or ""
        confidence = data.get("confidence", 0) or 0

        old_h = _human_regime(old_regime).lower()
        new_h = _human_regime(new_regime).lower()

        pair_label = pair if pair != "FLEET" else "Fleet-Wide"
        header = f"\U0001f504 Market Shift \u2014 {pair_label}"
        narrator = f"Market conditions are shifting from {old_h} to {new_h}."

        bullish_regimes = {"TRENDING", "TRENDING_UP", "BULL", "AGGRESSIVE"}
        bearish_regimes = {"BEAR", "TRENDING_DOWN", "EXTREME_FEAR", "DEFENSIVE"}
        neutral_regimes = {"RANGING", "NORMAL", "EQUILIBRIUM", "MIXED", "CAUTIOUS"}

        new_up = (new_regime or "").upper()
        old_up = (old_regime or "").upper()

        if new_up in bullish_regimes and old_up not in bullish_regimes:
            narrator += " The trend is turning positive \u2014 the fleet may increase exposure."
        elif new_up in bearish_regimes and old_up not in bearish_regimes:
            narrator += " Conditions are deteriorating \u2014 the fleet is tightening risk."
        elif new_up in neutral_regimes:
            narrator += " Things are settling into a range. Grid and mean-reversion strategies tend to perform well here."
        else:
            narrator += f" {source_bot} detected the shift \u2014 the fleet is adjusting."

        narrator += f"\n\nRisk {risk_word}, {deployed_pct:.0f}% deployed."

        pulse_text = f"{header}\n\n{narrator}\n\nFleet Pulse \u00b7 {ts}"

        intel_details = [
            f"\u25b8 Detected by: {source_bot}",
            f"\u25b8 Shift: {old_regime} \u2192 {new_regime}",
            f"\u25b8 Confidence: {confidence:.0%}",
            f"\u25b8 Fleet: {bot_count} bots \u00b7 {deployed_pct:.0f}% deployed \u00b7 Risk {risk_level} {risk_emoji}",
        ]
        intel_text = (f"{header}\n\n{narrator}\n\n"
                      + "\n".join(intel_details)
                      + f"\n\nFleet Intelligence \u00b7 {ts}")

        return pulse_text, intel_text

# Color-coded emoji per event type
_TYPE_EMOJI = {
    "HIGH_CONVICTION":     "\U0001f7e2",  # 🟢
    "TRADE_OPEN":          "\U0001f535",  # 🔵
    "TRADE_CLOSE":         "\u26aa",      # ⚪
    "WHALE_ALERT":         "\U0001f7e0",  # 🟠
    "REGIME_CHANGE":       "\U0001f7e3",  # 🟣
    "AEGIS_UPDATE":        "\U0001f7e3",  # 🟣
    "CATASTROPHE_WARNING": "\U0001f534",  # 🔴
    "EMERGENCY_REDUCE":    "\U0001f534",  # 🔴
    "SIGNAL":              "\U0001f7e2",  # 🟢
    "BOOK_PHASE":          "\U0001f7e1",  # 🟡
    "FLEET_ALERT":         "\U0001f534",  # 🔴
    "STRUCTURE_FORMING":   "\U0001f535",  # 🔵
    "CHAOS_STATE":         "\U0001f534",  # 🔴
    "MANIFOLD_WARNING":    "\U0001f7e1",  # 🟡
    "CYCLE_DETECTED":      "\U0001f7e3",  # 🟣
    "CAUSAL_FLOW":         "\U0001f7e2",  # 🟢
    "EUCLID_LEVEL":        "\U0001f7e1",  # 🟡
}

def _bar(value: float, total: float = 1.0, width: int = 10) -> str:
    """Render a mini progress bar: ▓▓▓▓▓░░░░░"""
    if not isinstance(value, (int, float)) or total <= 0:
        return "─" * width
    filled = max(0, min(width, round(value / total * width)))
    return "\u2593" * filled + "\u2591" * (width - filled)

def _regime_badge(regime: str) -> str:
    """Short regime label with indicator char."""
    r = str(regime).upper()
    icons = {
        "BULL": "\u25b2 BULL", "BEAR": "\u25bc BEAR",
        "RANGING": "\u25a0 RANGING", "RANGE": "\u25a0 RANGING",
        "VOLATILE": "\u26a1 VOLATILE", "TRANSITIONING": "\u21c4 TRANSITIONING",
        "CAUTIOUS": "\u26a0 CAUTIOUS", "DEFENSIVE": "\U0001f6e1 DEFENSIVE",
        "NORMAL": "\u25cf NORMAL",
    }
    for key, label in icons.items():
        if key in r:
            return label
    return regime


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC · %d %b %Y")


def _v(data: dict, key: str, default: str = "\u2014") -> str:
    val = data.get(key)
    if val is None or val == "":
        return default
    return str(val)


def _header(label: str, event_type: str = "") -> str:
    emoji = _TYPE_EMOJI.get(event_type, "\u26aa")
    return f"<code>{_LINE}</code>\n<b>{emoji}  {label}</b>"


def _divider() -> str:
    return f"\n<code>{_LINE2}</code>"


def _footer(label: str) -> str:
    return f"\n<code>{_LINE}\n{label}  {_utc_now()}</code>"


class CardFormatter:
    """Converts routing decisions into Telegram HTML messages.

    Two renderings per event: paid (full detail, engine names, prose)
    and free (redacted, no pair/direction/bot names).
    """

    def __init__(self):
        self._engine_names = dict(_ENGINE_NAMES)

    # ── Public ───────────────────────────────────────────────────────

    @staticmethod
    def _context_line(event: dict) -> str:
        """Build a live fleet context footer from enriched event data."""
        parts = []
        price = event.get("_price")
        pair = (event.get("data", {}) or {}).get("pair", "") if isinstance(event.get("data"), dict) else ""
        if price and pair:
            parts.append(f"{pair} ${price:,.2f}")
        regime = event.get("_fleet_regime")
        if regime:
            parts.append(_regime_badge(str(regime)))
        risk = event.get("_risk_status")
        if risk:
            risk_icons = {"GREEN": "\u2705", "YELLOW": "\u26a0", "RED": "\U0001f534"}
            parts.append(f"Risk {risk_icons.get(str(risk).upper(), '')} {risk}")
        deployed = event.get("_deployed_pct")
        if isinstance(deployed, (int, float)):
            parts.append(f"Deployed {deployed:.0f}%")
        bots = event.get("_bots_alive")
        if bots:
            parts.append(f"{bots} bots")
        if not parts:
            return ""
        return "\n<code>" + " · ".join(parts) + "</code>"

    # ── Markup modes (Stage 2, 2026-08-05) ──────────────────────────────
    # Card bodies are authored with Telegram HTML (<code>/<b>/<i>) and
    # box-drawing rules. Platforms with a hard character budget (X: 280)
    # or no HTML need the same content in plain text. Rather than fork 34
    # tag sites, normalize once on the way out.
    #
    # Measured on real trade cards: 312-315 chars as authored -> ~153 in
    # plain mode, which is what makes them portable to X at all.

    _TAG_RE = re.compile(r"</?(?:code|b|i|pre)>")
    _RULE_RE = re.compile(r"^[\s─-╿—―-]*$")

    @classmethod
    def to_plain(cls, body: str) -> str:
        """Strip Telegram HTML and box-drawing rules. Content unchanged."""
        if not body:
            return ""
        text = cls._TAG_RE.sub("", body)
        text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        kept = [ln.rstrip() for ln in text.splitlines()
                if ln.strip() and not cls._RULE_RE.match(ln)]
        out, blank = [], False
        for ln in kept:
            if not ln.strip():
                if blank:
                    continue
                blank = True
            else:
                blank = False
            out.append(ln)
        return "\n".join(out).strip()

    @classmethod
    def fit(cls, body: str, limit: int) -> str:
        """Trim plain text to a hard character budget on a line boundary."""
        if limit <= 0 or len(body) <= limit:
            return body
        lines, out = body.splitlines(), []
        for ln in lines:
            candidate = "\n".join(out + [ln])
            if len(candidate) > limit - 1:
                break
            out.append(ln)
        trimmed = "\n".join(out).rstrip()
        if not trimmed:
            trimmed = body[:max(0, limit - 1)].rstrip()
        return trimmed + "…"

    @staticmethod
    def _drop_empty_rows(body: str) -> str:
        """Remove label rows whose only value is a placeholder.

        Cards are built with fixed layouts, so a field the event lacks renders
        as "Regime    —" or "Stop    None". A row that says nothing is worse
        than no row: it asserts the fleet measured something and got nothing.
        Applied once here rather than guarded at every call site — the same
        approach the markup conversion uses.
        """
        if not body:
            return body
        out = []
        for ln in body.splitlines():
            s = ln.strip()
            if s and not s.startswith("<"):
                toks = s.split()
                # "Label    —"  and also "Avg P/L   — per trade": the
                # placeholder can sit mid-row followed by a unit, so match on
                # the VALUE token rather than on row length. A row whose value
                # is a placeholder says nothing regardless of its suffix.
                if len(toks) >= 2 and any(
                        t in ("—", "None", "none", "null") for t in toks[1:]):
                    # Guard: never drop a row that also carries a real number,
                    # e.g. "Range     — to 1.23".
                    if not any(any(c.isdigit() for c in t) for t in toks[1:]):
                        continue
            out.append(ln)
        return "\n".join(out)

    def format_paid(self, decision: dict) -> str:
        event = decision.get("event", decision)
        etype = event.get("type", "")
        data = event.get("data", {}) if isinstance(event.get("data"), dict) else {}

        # Same as format_free: the emitting bot is on the event, not in data.
        if "source" not in data and event.get("source"):
            data = {**data, "source": event["source"]}

        prefix = ""
        if event.get("aegis_defensive") and etype in ("HIGH_CONVICTION", "SIGNAL", "TRADE_OPEN"):
            prefix = ("<b>\u26a0 FLEET CAUTION \u2014 AEGIS DEFENSIVE</b>\n"
                       "Signal confidence reduced. Exposure throttled.\n\n")

        handler = self._PAID_HANDLERS.get(etype)
        if handler:
            body = handler(self, etype, data)
        else:
            body = self._paid_generic(etype, data)

        context = self._context_line(event)
        return prefix + self._drop_empty_rows(body) + context

    def format_free(self, decision: dict) -> str:
        event = decision.get("event", decision)
        etype = event.get("type", "")
        data = event.get("data", {}) if isinstance(event.get("data"), dict) else {}
        # The emitting bot lives on the event, not in data. Formatters only
        # receive data, so carry it through — without this every card that
        # names a bot renders an em-dash (the paid trade card did, silently).
        if "source" not in data and event.get("source"):
            data = {**data, "source": event["source"]}

        handler = self._FREE_HANDLERS.get(etype)
        if handler:
            body = handler(self, etype, data)
        else:
            body = self._free_generic(etype, data)

        return self._drop_empty_rows(body)

    def format_daily_summary(self, stats: dict) -> str:
        pnl = stats.get("fleet_pnl")
        pnl_s = f"${pnl:+.2f}" if isinstance(pnl, (int, float)) else "\u2014"
        trades = stats.get("total_trades", 0)
        wr = stats.get("win_rate")
        wr_s = f"{wr:.0%}" if isinstance(wr, float) and wr <= 1 else ("\u2014" if wr is None else str(wr))
        ev = stats.get("expectancy")
        ev_s = f"${ev:+.2f}" if isinstance(ev, (int, float)) else "\u2014"
        # Denominator on the CARD. "Avg P/L per trade" computed over 2 of 18
        # members is not a fleet statistic; name the population it covers.
        _pb, _fm = stats.get("participating_bots"), stats.get("fleet_members")
        if (isinstance(_pb, int) and isinstance(_fm, int)
                and _fm and _pb < _fm):
            ev_s = f"{ev_s} ({_pb}/{_fm} bots)"
        # With no closed trades there is no expectancy to report. Omit the row
        # rather than print "—" or, worse, "$+0.00" — a zero is a measured
        # result and this is the absence of one.
        _ev_row = "" if not isinstance(ev, (int, float)) else f"Avg P/L   {ev_s} per trade\n"
        dep = stats.get("deployed_pct")
        dep_s = f"{dep:.0f}%" if isinstance(dep, (int, float)) else "\u2014"
        regime = _regime_badge(str(stats.get("regime", "\u2014")))
        aegis = stats.get("aegis_score")
        aegis_s = f"{aegis:.2f}" if isinstance(aegis, (int, float)) else "\u2014"
        aegis_bar = _bar(aegis or 0, 1.0, 10) if isinstance(aegis, (int, float)) else ""
        top_bot = display_name(stats.get("top_bot", "\u2014"))
        top_pair = stats.get("top_pair", "\u2014")

        # Coach voice summary
        if isinstance(pnl, (int, float)):
            if pnl > 0:
                why = f"Good day. Fleet made {pnl_s} across {trades} trades."
            elif pnl == 0 and trades == 0:
                why = f"Quiet day \u2014 no trades triggered. The fleet is waiting for conditions to improve before putting capital at risk."
            elif pnl == 0:
                why = f"Flat day. {trades} trades but nothing moved the needle."
            else:
                why = f"Rough day. Fleet gave back {pnl_s} across {trades} trades. Risk was managed \u2014 no blowups."
        else:
            why = "Here's where the fleet stands at the end of the day."

        if isinstance(aegis, (int, float)) and aegis < 0.4:
            why += " Health score is low so we're being cautious with capital."
        if top_bot and top_bot != "\u2014":
            why += f" Best performer: {top_bot}."

        return (
            f"{_header('END OF DAY REPORT', 'AEGIS_UPDATE')}\n"
            f"<i>{why}</i>"
            f"{_divider()}\n"
            f"<code>"
            f"P/L Today {pnl_s}\n"
            f"Trades    {trades}\n"
            f"Win Rate  {wr_s}\n"
            f"{_ev_row}"
            f"Deployed  {dep_s}\n"
            f"Regime    {regime}\n"
            f"Health    {aegis_bar} {aegis_s}\n"
            f"Best Bot  {top_bot}\n"
            f"Best Pair {top_pair}"
            f"</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    def format_weekly_report(self, report: dict) -> str:
        pnl = report.get("total_pnl")
        pnl_s = f"${pnl:+.2f}" if isinstance(pnl, (int, float)) else "\u2014"
        trades = report.get("total_trades", 0)
        wr = report.get("avg_win_rate")
        wr_s = f"{wr:.0%}" if isinstance(wr, float) and wr <= 1 else ("\u2014" if wr is None else str(wr))
        ev = report.get("avg_expectancy")
        ev_s = f"${ev:+.2f}" if isinstance(ev, (int, float)) else "\u2014"
        best_day = report.get("best_day", "\u2014")
        worst_day = report.get("worst_day", "\u2014")
        days_pos = report.get("days_positive", "\u2014")
        days_total = report.get("days_total", 7)

        # Coach voice
        if isinstance(pnl, (int, float)):
            if pnl > 0:
                why = f"Positive week. Fleet banked {pnl_s} total across {trades} trades."
            elif pnl == 0 and trades == 0:
                why = f"The fleet was mostly on the sidelines this week \u2014 conditions didn't meet our standards for putting capital to work."
            else:
                why = f"Down {pnl_s} this week across {trades} trades. Not the result we wanted, but drawdowns are part of the game. No panic."
        else:
            why = "Here's how the fleet performed this week."

        if isinstance(days_pos, (int, float)) and isinstance(days_total, (int, float)):
            why += f" {int(days_pos)} out of {int(days_total)} days were profitable."

        return (
            f"{_header('WEEKLY SCORECARD', 'HIGH_CONVICTION')}\n"
            f"<i>{why}</i>"
            f"{_divider()}\n"
            f"<code>"
            f"Week P/L  {pnl_s}\n"
            f"Trades    {trades}\n"
            f"Win Rate  {wr_s}\n"
            f"Avg P/L   {ev_s} per trade\n"
            f"Best Day  {best_day}\n"
            f"Worst Day {worst_day}\n"
            f"Green Days {days_pos}/{days_total}"
            f"</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    # ── Paid card handlers ───────────────────────────────────────────

    def format_daily_summary_free(self, stats: dict) -> str:
        pnl = stats.get("fleet_pnl")
        pnl_s = f"${pnl:+.2f}" if isinstance(pnl, (int, float)) else "\u2014"
        trades = stats.get("total_trades", 0)
        regime = _regime_badge(str(stats.get("regime", "\u2014")))
        # Replaced a "\ud83d\udd12 Full stats for subscribers" line. There is no paid
        # tier behind it, so it was a paywall teaser for a product that does
        # not exist. Win rate is already in stats \u2014 show the real number, and
        # omit the line entirely rather than printing a placeholder.
        _wr = stats.get("win_rate")
        _wr_line = ""
        if isinstance(_wr, (int, float)):
            _wr_pct = _wr * 100 if _wr <= 1.01 else _wr
            _wr_line = f"Win Rate  {_wr_pct:.0f}%\n"

        if isinstance(pnl, (int, float)):
            if pnl > 0:
                why = f"Good day for the fleet \u2014 {pnl_s} across {trades} trades. We'll take it."
            elif pnl == 0 and trades == 0:
                why = f"Quiet day \u2014 no trades triggered. We only trade when conditions are right."
            else:
                why = f"Down {pnl_s} today. Part of the process. Risk was managed, no surprises."
        else:
            why = "End of day update from the fleet."

        return (
            f"{_header('END OF DAY', 'AEGIS_UPDATE')}\n"
            f"<i>{why}</i>"
            f"{_divider()}\n"
            f"<code>"
            f"P/L       {pnl_s}\n"
            f"Trades    {trades}\n"
            f"{_wr_line}"
            f"Regime    {regime}"
            f"</code>"
            f"{_footer(_FOOTER_FREE)}"
        )

    def format_weekly_report_free(self, report: dict) -> str:
        pnl = report.get("total_pnl")
        pnl_s = f"${pnl:+.2f}" if isinstance(pnl, (int, float)) else "\u2014"
        trades = report.get("total_trades", 0)
        days_pos = report.get("days_positive", "\u2014")
        days_total = report.get("days_total", 7)

        if isinstance(pnl, (int, float)):
            if pnl > 0:
                why = f"Winning week \u2014 fleet made {pnl_s} across {trades} trades."
            elif trades == 0:
                why = f"Mostly sidelined this week. We don't force trades."
            else:
                why = f"Red week at {pnl_s}. Drawdowns happen \u2014 what matters is how we come back."
        else:
            why = "Weekly recap from the fleet."

        if isinstance(days_pos, (int, float)):
            why += f" {int(days_pos)}/{int(days_total)} days in the green."

        return (
            f"{_header('WEEKLY SCORECARD', 'HIGH_CONVICTION')}\n"
            f"<i>{why}</i>"
            f"{_divider()}\n"
            f"<code>"
            f"Week P/L  {pnl_s}\n"
            f"Trades    {trades}\n"
            f"Green Days {days_pos}/{days_total}"
            f"</code>"
            f"{_footer(_FOOTER_FREE)}"
        )

    # ── Paid card handlers ───────────────────────────────────────────

    def _paid_high_conviction(self, etype: str, d: dict) -> str:
        pair = _v(d, 'pair')
        direction = str(d.get('direction', '')).upper()
        dir_glyph = "\u25b2 LONG" if direction in ("LONG", "BUY") else ("\u25bc SHORT" if direction in ("SHORT", "SELL") else direction)
        regime = _regime_badge(_v(d, 'regime'))
        conf = d.get("confidence", d.get("score"))
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "\u2014"
        conf_bar = _bar(conf or 0, 1.0, 12)
        bots = d.get("bots", d.get("sources", []))
        bots_list = bots if isinstance(bots, list) else []
        bots_s = " \u00b7 ".join(display_name(b) for b in bots_list) if bots_list else "Fleet consensus"
        count = len(bots_list) if bots_list else "?"
        ev = d.get("expectancy", d.get("ev"))
        ev_s = f"${ev:+.2f}/trade" if isinstance(ev, (int, float)) else "\u2014"
        aegis = d.get("aegis_score", d.get("aegis"))
        aegis_s = f"{aegis:.2f}" if isinstance(aegis, (int, float)) else "\u2014"

        return (
            f"{_header('HIGH CONVICTION SIGNAL', 'HIGH_CONVICTION')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"Pair      {pair}\n"
            f"Signal    {dir_glyph}\n"
            f"Regime    {regime}\n"
            f"Conf      {conf_bar} {conf_s}\n"
            f"E[V]      {ev_s}\n"
            f"Sources   {bots_s}"
            f"</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    def _paid_trade_open(self, etype: str, d: dict) -> str:
        pair = _v(d, 'pair')
        direction = str(d.get('direction', '')).upper()
        dir_glyph = "\u25b2 LONG" if direction in ("LONG", "BUY") else ("\u25bc SHORT" if direction in ("SHORT", "SELL") else _v(d, 'direction'))
        bot = display_name(_v(d, 'source', _v(d, 'bot')))
        regime = _regime_badge(_v(d, 'regime'))
        size = d.get("size", d.get("amount", d.get("total_size")))
        entry = d.get("entry_price", d.get("entry", d.get("price", d.get("avg_entry"))))
        stop = d.get("stop_loss", d.get("stop", d.get("current_stop")))

        # Smart formatting — detect decimals needed from price magnitude
        if isinstance(entry, (int, float)):
            if entry > 100:
                entry_s = f"{entry:,.2f}"
            elif entry > 1:
                entry_s = f"{entry:,.4f}"
            else:
                entry_s = f"{entry:,.6f}"
        else:
            entry_s = "\u2014"
        if isinstance(stop, (int, float)):
            if stop > 100:
                stop_s = f"{stop:,.2f}"
            elif stop > 1:
                stop_s = f"{stop:,.4f}"
            else:
                stop_s = f"{stop:,.6f}"
        else:
            stop_s = "\u2014"
        if isinstance(size, (int, float)):
            size_s = f"{size:,.2f} units" if size > 1 else f"{size:.6f} units"
        else:
            size_s = "\u2014"

        # Risk calc
        if isinstance(entry, (int, float)) and isinstance(stop, (int, float)) and entry > 0:
            risk_pct = abs(stop - entry) / entry * 100
            risk_s = f"{risk_pct:.1f}%"
        else:
            risk_s = "\u2014"

        return (
            f"{_header('TRADE ALERT \u2014 ' + dir_glyph, 'TRADE_OPEN')}\n"
            f"{_divider()}\n"
            f"<code>"
            f" {pair}\n"
            f" Entry   {entry_s}\n"
            f" Stop    {stop_s}\n"
            f" Risk    {risk_s}\n"
            f"Direction {dir_glyph}\n"
            f"Size      {size_s}\n"
            f"Regime    {regime}\n"
            f"Bot       {bot}"
            f"</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    def _paid_trade_close(self, etype: str, d: dict) -> str:
        pair = _v(d, 'pair')
        pnl = d.get("pnl")
        # No fee line on cards (2026-07-30): P/L is gross price movement \u2014
        # subscribers pay their own exchange's fees, not ours to preach.
        pnl_s = f"${pnl:+.2f}" if isinstance(pnl, (int, float)) else "\u2014"
        pnl_won = isinstance(pnl, (int, float)) and pnl > 0
        result_glyph = "\u2714 WIN" if pnl_won else "\u2716 LOSS"
        bot = display_name(_v(d, 'source', _v(d, 'bot')))
        direction = str(d.get('direction', '')).upper()
        dir_glyph = "\u25b2 LONG" if direction in ("LONG", "BUY") else ("\u25bc SHORT" if direction in ("SHORT", "SELL") else _v(d, 'direction'))
        regime = _regime_badge(_v(d, 'regime'))
        duration_s = d.get("duration_s", d.get("duration"))
        exit_reason = d.get("exit_reason", d.get("reason", ""))
        entry = d.get("entry_price", d.get("entry"))
        exit_p = d.get("exit_price", d.get("close_price"))
        size = d.get("size_usd", d.get("size", d.get("amount")))

        # Smart price formatting
        def _price(v):
            if not isinstance(v, (int, float)):
                return "\u2014"
            if v > 100:
                return f"{v:,.2f}"
            elif v > 1:
                return f"{v:,.4f}"
            else:
                return f"{v:,.6f}"

        entry_s = _price(entry)
        exit_s = _price(exit_p)
        size_s = f"${size:,.2f}" if isinstance(size, (int, float)) else "\u2014"

        # Duration formatting
        if isinstance(duration_s, (int, float)):
            hours = duration_s / 3600
            if hours >= 24:
                dur_s = f"{hours/24:.1f} days"
            elif hours >= 1:
                dur_s = f"{hours:.1f}h"
            else:
                dur_s = f"{duration_s/60:.0f}min"
        elif duration_s:
            dur_s = str(duration_s)
        else:
            dur_s = "\u2014"

        # Return %
        if isinstance(entry, (int, float)) and isinstance(exit_p, (int, float)) and entry > 0:
            if direction in ("LONG", "BUY"):
                ret_pct = (exit_p - entry) / entry * 100
            else:
                ret_pct = (entry - exit_p) / entry * 100
            ret_s = f"{ret_pct:+.2f}%"
        elif isinstance(pnl, (int, float)) and isinstance(size, (int, float)) and size > 0:
            ret_pct = pnl / size * 100
            ret_s = f"{ret_pct:+.2f}%"
        else:
            ret_s = ""

        return (
            f"{_header('TRADE RESULT \u2014 ' + result_glyph, 'TRADE_CLOSE')}\n"
            f"{_divider()}\n"
            f"<code>"
            f" {pair}\n"
            f" P/L     {pnl_s}\n"
            f" Entry   {entry_s}\n"
            f" Exit    {exit_s}\n"
            f"Direction {dir_glyph}\n"
            f"Size      {size_s}\n"
            f"Duration  {dur_s}\n"
            f"Regime    {regime}\n"
            f"Bot       {bot}"
            f"</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    def _paid_signal(self, etype: str, d: dict) -> str:
        pair = _v(d, 'pair')
        direction = str(d.get('direction', '')).upper()
        dir_glyph = "\u25b2 LONG" if direction in ("LONG", "BUY") else ("\u25bc SHORT" if direction in ("SHORT", "SELL") else _v(d, 'direction'))
        source = display_name(_v(d, 'source'))
        regime = _regime_badge(_v(d, 'regime'))
        conf = d.get("confidence")
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "\u2014"
        conf_bar = _bar(conf or 0, 1.0, 10)
        reason = d.get("reason", d.get("signal_reason", d.get("suggested_action", d.get("description", ""))))
        reason_s = str(reason).replace("_", " ") if reason else "\u2014"

        return (
            f"{_header('SIGNAL', 'SIGNAL')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"Pair      {pair}\n"
            f"Signal    {dir_glyph}\n"
            f"Conf      {conf_bar} {conf_s}\n"
            f"Regime    {regime}\n"
            f"Source    {source}"
            f"</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    def _paid_regime_change(self, etype: str, d: dict) -> str:
        from_r = _v(d, 'from', _v(d, 'old_regime'))
        to_r   = _v(d, 'to',   _v(d, 'new_regime'))
        raw_src = d.get('source') or ""
        source = display_name(raw_src) if raw_src and raw_src != "\u2014" else "Fleet consensus"
        confidence = d.get("confidence", d.get("probability"))
        conf_s = f"{confidence:.0%}" if isinstance(confidence, float) and confidence <= 1 else ""
        from_badge = _regime_badge(from_r)
        to_badge   = _regime_badge(to_r)
        return (
            f"{_header('REGIME CHANGE', 'REGIME_CHANGE')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"From      {from_badge}\n"
            f"To        {to_badge}\n"
            f"Source    {source}"
            f"</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    def _paid_aegis_update(self, etype: str, d: dict) -> str:
        score = d.get("score")
        score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "\u2014"
        score_bar = _bar(score or 0, 1.0, 12)
        regime = _regime_badge(_v(d, 'regime'))
        deploy_cap = d.get("recommended_max_deployed", d.get("deploy_limit"))
        cap_s = f"{int(deploy_cap)}%" if isinstance(deploy_cap, (int, float)) else "\u2014"
        components = d.get("components", {})
        regime_sources = d.get("regime_sources", {})

        # Translate components into plain English
        comp_lines = []
        comp_readable = {
            "consensus_entropy": ("Bot agreement",     "Are our bots agreeing or fighting each other?"),
            "signal_coherence":  ("Signal clarity",    "Are signals consistent or noisy?"),
            "whale_divergence":  ("Whale risk",        "Are big players moving against us?"),
            "portfolio_stress":  ("Portfolio health",  "How much stress is our book under?"),
            "phitex_signal":     ("Market temperature","Is the market running hot or cold?"),
            "correlation":       ("Correlation risk",  "Are all our bets moving together?"),
        }
        if isinstance(components, dict):
            for k, (label, _) in comp_readable.items():
                v = components.get(k)
                if v is not None and isinstance(v, (int, float)):
                    comp_lines.append(f"{label:16s} {_bar(v, 1.0, 8)} {v:.2f}")

        # Regime consensus in plain english
        reg_votes = ""
        if isinstance(regime_sources, dict) and regime_sources:
            votes = " \u00b7 ".join(f"{display_name(k)}:{v}" for k, v in list(regime_sources.items())[:3])
            reg_votes = f"\nVotes     {votes}"

        comp_block = "\n".join(comp_lines)

        return (
            f"{_header('FLEET HEALTH CHECK', 'AEGIS_UPDATE')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"Health    {score_bar} {score_s}\n"
            f"Posture   {regime}\n"
            f"Max Deploy {cap_s}"
            f"{reg_votes}"
            f"</code>"
            f"{_divider()}\n"
            f"<code>{comp_block}</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    def _paid_emergency(self, etype: str, d: dict) -> str:
        reason = _v(d, 'reason', _v(d, 'trigger'))
        stops = d.get("stop_count", "")
        window = _v(d, 'window')
        source = display_name(_v(d, 'source'))
        stops_s = str(stops) if stops and str(stops) != "\u2014" else "multiple"

        return (
            f"{_header('EMERGENCY \u2014 REDUCING ALL EXPOSURE', 'EMERGENCY_REDUCE')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"Trigger   {reason}\n"
            f"Stops     {stops_s}\n"
            f"Window    {window}\n"
            f"Action    \u25bc Going to cash"
            f"</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    def _paid_fleet_alert(self, etype: str, d: dict) -> str:
        alert = _v(d, 'description', _v(d, 'message'))
        source = display_name(_v(d, 'source'))
        severity = _v(d, 'severity')
        reason = d.get("reason", d.get("suggested_action", ""))
        reason_clean = str(reason).replace("_", " ") if reason else ""
        return (
            f"{_header('FLEET ALERT', 'FLEET_ALERT')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"Source    {source}\n"
            f"Severity  {severity}"
            f"</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    def _paid_whale(self, etype: str, d: dict) -> str:
        pair = _v(d, 'pair')
        side = _v(d, 'side', _v(d, 'direction'))
        magnitude = _v(d, 'magnitude')
        volume = d.get("volume_usd", d.get("volume", d.get("amount")))
        vol_s = f"${volume:,.0f}" if isinstance(volume, (int, float)) else "\u2014"
        score = d.get("score")
        score_s = f"{score:.1f}" if isinstance(score, (int, float)) else "\u2014"
        score_bar = _bar(score or 0, 100.0, 10) if isinstance(score, (int, float)) else ""
        reliability = d.get("reliability")
        rel_s = f"{reliability:.0f}%" if isinstance(reliability, (int, float)) else "\u2014"
        side_str = str(side).upper()
        side_glyph = ("\u25b2 BUY pressure" if side_str in ("BUY", "LONG") else
                      "\u25bc SELL pressure" if side_str in ("SELL", "SHORT") else side)
        return (
            f"{_header('WHALE ALERT', 'WHALE_ALERT')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"Pair      {pair}\n"
            f"Side      {side_glyph}\n"
            f"Magnitude {magnitude}\n"
            f"Volume    {vol_s}"
            f"</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    def _paid_structure(self, etype: str, d: dict) -> str:
        pair = _v(d, 'pair')

        if etype == "CATASTROPHE_WARNING":
            ews = d.get("ews_score")
            ews_s = f"{ews:.2f}" if isinstance(ews, (int, float)) else "\u2014"
            ews_bar = _bar(ews or 0, 1.0, 10)
            severity = _v(d, 'severity')
            label = "SUDDEN MOVE WARNING"
            data = (f"Pair      {pair}\n"
                    f"Risk      {ews_bar} {ews_s}\n"
                    f"Severity  {severity}")

        elif etype == "CYCLE_DETECTED":
            cyclicality = d.get("cyclicality")
            cyc_s = f"{cyclicality:.2f}" if isinstance(cyclicality, (int, float)) else "\u2014"
            cyc_bar = _bar(cyclicality or 0, 1.0, 10)
            label = "REPEATING PATTERN"
            data = (f"Pair      {pair}\n"
                    f"Pattern   {cyc_bar} {cyc_s}")

        elif etype == "BOOK_PHASE":
            phase = _v(d, 'phase', _v(d, 'state'))
            temp = d.get("temperature")
            temp_s = f"{temp:,.0f}" if isinstance(temp, (int, float)) else "\u2014"
            entropy = d.get("entropy")
            ent_s = f"{entropy:.2f}" if isinstance(entropy, (int, float)) else "\u2014"
            ent_bar = _bar(entropy or 0, 1.0, 10)
            phase_str = str(phase).upper()
            if phase_str == "PLASMA":
                label = "LIQUIDITY CRISIS"
            elif phase_str == "BOILING":
                label = "LIQUIDITY WARNING"
            else:
                label = "LIQUIDITY UPDATE"
            data = (f"Pair      {pair}\n"
                    f"Phase     {phase_str}\n"
                    f"Disorder  {ent_bar} {ent_s}\n"
                    f"Activity  {temp_s}")

        elif etype == "STRUCTURE_FORMING":
            score = d.get("formation_score", d.get("structure_formation_score", d.get("score")))
            score_s = f"{score:.2f}" if isinstance(score, (int, float)) else "\u2014"
            score_bar = _bar(score or 0, 1.0, 10)
            label = "NEW TREND FORMING"
            data = (f"Pair      {pair}\n"
                    f"Strength  {score_bar} {score_s}")

        elif etype == "CHAOS_STATE":
            departure = d.get("attractor_departure")
            dep_s = f"{departure:.2f}" if isinstance(departure, (int, float)) else "\u2014"
            dep_bar = _bar(departure or 0, 1.0, 10)
            horizon = d.get("predictability_horizon")
            hor_s = f"{horizon:.0f}h" if isinstance(horizon, (int, float)) else _v(d, 'predictability_horizon')
            label = "UNPREDICTABLE MARKET"
            data = (f"Pair      {pair}\n"
                    f"Chaos     {dep_bar} {dep_s}\n"
                    f"Lookahead {hor_s}")

        elif etype == "MANIFOLD_WARNING":
            prob = d.get("regime_change_probability")
            prob_s = f"{prob:.0%}" if isinstance(prob, float) and prob <= 1 else str(prob) if prob else "\u2014"
            prob_bar = _bar((prob or 0), 1.0, 10)
            label = "REGIME SHIFT WARNING"
            data = (f"Pair        {pair}\n"
                    f"Probability {prob_bar} {prob_s}")

        elif etype == "CAUSAL_FLOW":
            source_node = d.get("source", d.get("cause", d.get("from", "\u2014")))
            target_node = d.get("target", d.get("effect", d.get("to", "\u2014")))
            strength = d.get("strength")
            str_s = f"{strength:.2f}" if isinstance(strength, (int, float)) else "\u2014"
            str_bar = _bar(strength or 0, 1.0, 10) if isinstance(strength, (int, float)) else ""
            lag = d.get("lag")
            lag_s = f"{lag}" if isinstance(lag, (int, float)) else "\u2014"
            label = "LEAD-LAG DETECTED"
            src_clean = str(source_node).replace("_price", "").replace("_volume", " volume")
            tgt_clean = str(target_node).replace("_price", "").replace("_volume", " volume")
            data = (f"Leader    {src_clean}\n"
                    f"Follower  {tgt_clean}\n"
                    f"Link      {str_bar} {str_s}\n"
                    f"Delay     {lag_s} bars")

        elif etype == "EUCLID_LEVEL":
            level = d.get("level", d.get("price"))
            lev_s = f"${level:,.5f}" if isinstance(level, (int, float)) else "\u2014"
            ltype = _v(d, 'type', _v(d, 'level_type'))
            strength = d.get("strength")
            str_s = f"{strength:.2f}" if isinstance(strength, (int, float)) else "\u2014"
            str_bar = _bar(strength or 0, 1.0, 10) if isinstance(strength, (int, float)) else ""
            dist = d.get("distance_pct")
            dist_s = f"{dist*100:.1f}%" if isinstance(dist, (int, float)) else "\u2014"
            ltype_clean = str(ltype).replace("_", " ").lower()
            label = "KEY PRICE LEVEL"
            data = (f"Pair      {pair}\n"
                    f"Level     {lev_s}\n"
                    f"Type      {ltype_clean}\n"
                    f"Strength  {str_bar} {str_s}\n"
                    f"Distance  {dist_s}")

        else:
            label = etype.replace("_", " ")
            data = f"Pair  {pair}"

        return (
            f"{_header(label, etype)}\n"
            f"{_divider()}\n"
            f"<code>{data}</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    def _paid_generic(self, etype: str, d: dict) -> str:
        label = etype.replace("_", " ") if etype else "EVENT"
        fields = []
        for k, v in d.items():
            if k not in ("id", "timestamp", "ts", "category", "type"):
                fields.append(f"{k:12s}{v}")
        block = "\n".join(fields[:8]) if fields else "No additional data"
        return (
            f"{_header(label, etype)}\n"
            f"<code>{block}</code>"
            f"{_footer(_FOOTER_PAID)}"
        )

    # ── Free card handlers ───────────────────────────────────────────

    def _free_high_conviction(self, etype: str, d: dict) -> str:
        pair = _v(d, 'pair')
        direction = str(d.get('direction', '')).upper()
        dir_glyph = "\u25b2 LONG" if direction in ("LONG", "BUY") else ("\u25bc SHORT" if direction in ("SHORT", "SELL") else _v(d, 'direction'))
        regime = _regime_badge(_v(d, 'regime'))
        bots = d.get("bots", d.get("sources", []))
        count = len(bots) if isinstance(bots, list) else "Multiple"
        # Render what the event ACTUALLY carries. Reaction-generated
        # HIGH_CONVICTION (quantum collapse, convergent signal) has no
        # direction and no regime — it has a `reason` describing the state
        # transition and a confidence. The card rendered direction/regime
        # anyway, so both showed "—" and the only real content was dropped:
        # subscribers got "HIGH CONVICTION SIGNAL / Signal — / Regime —"
        # plus a paywall teaser, which says nothing at all.
        # Lock line removed for the same reason it was removed from trade
        # cards — there is no paid tier behind it.
        _lines = [f"Pair      {pair}"]
        _reason = d.get("reason")
        if _reason:
            # Reason text is bot-authored and lands inside a <code> block with
            # parse_mode=HTML — escape it rather than trusting the source.
            _safe = (str(_reason).replace("&", "&amp;")
                     .replace("<", "&lt;").replace(">", "&gt;"))
            _lines.append(f"Detail    {_safe}")
        if direction:
            _lines.append(f"Signal    {dir_glyph}")
        _rg = d.get("regime")
        if _rg:
            _lines.append(f"Regime    {regime}")
        _conf = d.get("confidence", d.get("conviction"))
        try:
            if _conf is not None:
                _lines.append(f"Conviction {float(_conf):.0%}")
        except (TypeError, ValueError):
            pass
        return (
            f"{_header('HIGH CONVICTION SIGNAL', 'HIGH_CONVICTION')}\n"
            f"{_divider()}\n"
            f"<code>"
            + "\n".join(_lines) +
            f"</code>"
            f"{_footer(_FOOTER_FREE)}"
        )

    def _free_trade(self, etype: str, d: dict) -> str:
        is_close = "CLOSE" in etype
        pair = _v(d, 'pair')
        direction = str(d.get('direction', '')).upper()
        dir_glyph = "\u25b2 LONG" if direction in ("LONG", "BUY") else ("\u25bc SHORT" if direction in ("SHORT", "SELL") else _v(d, 'direction'))
        regime = _regime_badge(_v(d, 'regime'))

        # Build rows from what the event HAS. Bots do not agree on field
        # names — TurtleSue emits entry/size/sl, Confluence emits
        # entry_price/size_usd/stop — so a single-key lookup renders "—" for a
        # value that is present under another name. And `_v(d,'a') or
        # _v(d,'b')` never falls through, because _v returns "—" (truthy) when
        # the key is missing: the `or` is dead code. Check the dict directly.
        def _row(label, *keys, fmt=None):
            for k in keys:
                v = d.get(k)
                if v not in (None, "", 0):
                    try:
                        return f"{label:<10}{fmt(v) if fmt else v}\n"
                    except Exception:
                        return f"{label:<10}{v}\n"
            return ""   # omit rather than print a placeholder

        _rg = f"Regime    {regime}\n" if d.get("regime") else ""
        _close_rows = (_rg
                       + _row("Entry", "entry_price", "entry", "avg_entry")
                       + _row("Exit", "exit_price", "exit"))
        _open_rows = (_rg
                      + _row("Entry", "entry_price", "entry", "price")
                      + _row("Size", "size_usd", "size", "amount",
                             fmt=lambda v: f"${float(v):,.2f}")
                      + _row("Stop", "stop_loss", "stop", "sl", "current_stop"))

        if is_close:
            pnl = d.get("pnl")
            pnl_s = f"${pnl:+.2f}" if isinstance(pnl, (int, float)) else "\u2014"
            pnl_won = isinstance(pnl, (int, float)) and pnl > 0
            result_glyph = "\u2714 WIN" if pnl_won else "\u2716 LOSS"
            return (
                f"{_header('POSITION CLOSED', 'TRADE_CLOSE')}\n"
                f"{_divider()}\n"
                f"<code>"
                f"Pair      {pair}\n"
                f"Result    {result_glyph}  {pnl_s}\n"
                f"{_close_rows}"
                f"Bot       {display_name(_v(d, 'source', _v(d, 'bot')))}"
                f"</code>"
                f"{_footer(_FOOTER_FREE)}"
            )
        return (
            f"{_header('POSITION OPENED', 'TRADE_OPEN')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"Pair      {pair}\n"
            f"Signal    {dir_glyph}\n"
            f"{_open_rows}"
            f"Bot       {display_name(_v(d, 'source', _v(d, 'bot')))}"
            f"</code>"
            f"{_footer(_FOOTER_FREE)}"
        )

    def _free_emergency(self, etype: str, d: dict) -> str:
        return (
            f"{_header('EMERGENCY \u2014 GOING TO CASH', 'EMERGENCY_REDUCE')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"Action    \u25bc All positions closing\n"
            f"Severity  HIGH"
            f"</code>"
            f"{_footer(_FOOTER_FREE)}"
        )

    def _free_whale(self, etype: str, d: dict) -> str:
        pair = _v(d, 'pair')
        magnitude = _v(d, 'magnitude')
        side = _v(d, 'side', _v(d, 'direction'))
        volume = d.get("volume_usd", d.get("volume", d.get("amount")))
        vol_s = f"${volume:,.0f}" if isinstance(volume, (int, float)) else ""
        side_str = str(side).upper()
        side_word = "buying" if side_str in ("BUY", "LONG") else ("selling" if side_str in ("SELL", "SHORT") else "moving")
        side_glyph = ("\u25b2 BUY" if side_str in ("BUY", "LONG") else
                      "\u25bc SELL" if side_str in ("SELL", "SHORT") else side)
        return (
            f"{_header('WHALE ALERT', 'WHALE_ALERT')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"Pair      {pair}\n"
            f"Side      {side_glyph}\n"
            f"Magnitude {magnitude}"
            f"</code>"
            f"{_footer(_FOOTER_FREE)}"
        )

    def _free_regime(self, etype: str, d: dict) -> str:
        from_r = _v(d, 'from', _v(d, 'old_regime'))
        to_r   = _v(d, 'to',   _v(d, 'new_regime'))
        raw_src = d.get('source') or ""
        source = display_name(raw_src) if raw_src and raw_src != "\u2014" else "Fleet consensus"
        from_badge = _regime_badge(from_r)
        to_badge   = _regime_badge(to_r)
        implications = {
            "BULL":          "Market looks strong. We're leaning into buys.",
            "BEAR":          "Turning defensive. We're pulling back.",
            "RANGING":       "No clear trend. We're playing the range.",
            "RANGE":         "No clear trend. We're playing the range.",
            "VOLATILE":      "Getting choppy. We're cutting risk.",
            "TRANSITIONING": "Trend is breaking down. We're waiting it out.",
            "CAUTIOUS":      "Something feels off. Less capital at risk.",
            "DEFENSIVE":     "High alert. Mostly cash right now.",
        }
        to_key = str(to_r).upper().split("_")[0]
        meaning = implications.get(str(to_r).upper(), implications.get(to_key, "Adjusting our approach."))
        return (
            f"{_header('MARKET SHIFT', 'REGIME_CHANGE')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"Before    {from_badge}\n"
            f"Now       {to_badge}\n"
            f"Source    {source}"
            f"</code>"
            f"{_footer(_FOOTER_FREE)}"
        )

    def _free_aegis(self, etype: str, d: dict) -> str:
        score = d.get("score")
        score_s = f"{score:.2f}" if isinstance(score, (int, float)) else "\u2014"
        score_bar = _bar(score or 0, 1.0, 12)
        regime = _regime_badge(_v(d, 'regime'))
        deploy_cap = d.get("recommended_max_deployed")
        cap_s = f"{int(deploy_cap)}%" if isinstance(deploy_cap, (int, float)) else "\u2014"
        return (
            f"{_header('FLEET HEALTH CHECK', 'AEGIS_UPDATE')}\n"
            f"{_divider()}\n"
            f"<code>"
            f"Health    {score_bar} {score_s}\n"
            f"Posture   {regime}\n"
            f"Max Deploy {cap_s}"
            f"</code>"
            f"{_footer(_FOOTER_FREE)}"
        )

    def _free_structure(self, etype: str, d: dict) -> str:
        pair = _v(d, 'pair')
        if etype == "CATASTROPHE_WARNING":
            ews = d.get("ews_score")
            ews_bar = _bar(ews or 0, 1.0, 10)
            ews_s = f"{ews:.2f}" if isinstance(ews, (int, float)) else "\u2014"
            label = "SUDDEN MOVE WARNING"
            data_block = f"Pair      {pair}\nRisk      {ews_bar} {ews_s}"
        elif etype == "BOOK_PHASE":
            phase = _v(d, 'phase', _v(d, 'state'))
            phase_str = str(phase).upper()
            label = "LIQUIDITY CRISIS" if phase_str == "PLASMA" else "LIQUIDITY WARNING"
            data_block = f"Pair      {pair}\nPhase     {phase_str}"
        else:
            label = "MARKET SIGNAL"
            data_block = f"Pair      {pair}"
        return (
            f"{_header(label, etype)}\n"
            f"{_divider()}\n"
            f"<code>{data_block}</code>"
            f"{_footer(_FOOTER_FREE)}"
        )

    def _free_generic(self, etype: str, d: dict) -> str:
        return (
            f"{_header('FLEET UPDATE', etype)}\n"
            f"{_footer(_FOOTER_FREE)}"
        )

    # ── Dispatch tables ──────────────────────────────────────────────

    _PAID_HANDLERS = {
        "HIGH_CONVICTION": _paid_high_conviction,
        "SIGNAL": _paid_signal,
        "TRADE_OPEN": _paid_trade_open,
        "TRADE_CLOSE": _paid_trade_close,
        "REGIME_CHANGE": _paid_regime_change,
        "AEGIS_UPDATE": _paid_aegis_update,
        "EMERGENCY_REDUCE": _paid_emergency,
        "FLEET_ALERT": _paid_fleet_alert,
        "WHALE_ALERT": _paid_whale,
        "CATASTROPHE_WARNING": _paid_structure,
        "CYCLE_DETECTED": _paid_structure,
        "BOOK_PHASE": _paid_structure,
        "STRUCTURE_FORMING": _paid_structure,
        "CHAOS_STATE": _paid_structure,
        "MANIFOLD_WARNING": _paid_structure,
        "CAUSAL_FLOW": _paid_structure,
        "EUCLID_LEVEL": _paid_structure,
    }

    _FREE_HANDLERS = {
        "HIGH_CONVICTION": _free_high_conviction,
        "SIGNAL": _free_high_conviction,
        "TRADE_OPEN": _free_trade,
        "TRADE_CLOSE": _free_trade,
        "EMERGENCY_REDUCE": _free_emergency,
        "FLEET_ALERT": _free_emergency,
        "WHALE_ALERT": _free_whale,
        "AEGIS_UPDATE": _free_aegis,
        "REGIME_CHANGE": _free_regime,
        "CATASTROPHE_WARNING": _free_structure,
        "CYCLE_DETECTED": _free_structure,
        "BOOK_PHASE": _free_structure,
        "STRUCTURE_FORMING": _free_structure,
        "CHAOS_STATE": _free_structure,
        "MANIFOLD_WARNING": _free_structure,
        "CAUSAL_FLOW": _free_structure,
        "EUCLID_LEVEL": _free_structure,
    }


# ── AlertGate (from notifier.py) ────────────────────────────────────────────

class AlertGate:
    """Deduplication + rate limiting for outbound alerts.
    Copied from notifier.py lines 109-146."""

    def __init__(self, dedup_window_s: int = 300, max_seen: int = 1000,
                 max_per_min: int = 30, hysteresis_window_s: int = 1800):
        self._dedup_window = dedup_window_s
        self._seen_ids: collections.deque = collections.deque(maxlen=max_seen)
        self._last_sent: dict[str, float] = {}
        self._minute_log: collections.deque = collections.deque()
        self._max_per_min = max_per_min
        self._prune_counter = 0
        # Hysteresis: suppress an A->B->A reversal inside this window.
        # 60.9% of observed regime transitions were pure flip-flops
        # (trinity alone: 635 TRENDING<->RANGING of 765 events).
        # Maps hysteresis_key -> (last_state_str, ts).
        self._hysteresis_window = hysteresis_window_s
        self._last_state: dict[str, tuple] = {}

    def check_hysteresis(self, key: str, state: str) -> bool:
        """False if this is a reversal of the last transition within the window.

        `state` is "FROM>TO". A following "TO>FROM" inside the window is the
        oscillation we want to swallow. Records the state when it passes, so
        callers should only invoke this once per candidate send.
        """
        if not key or not state:
            return True
        now = time.time()
        prev = self._last_state.get(key)
        if prev:
            prev_state, prev_ts = prev
            if now - prev_ts < self._hysteresis_window:
                try:
                    pf, pt = prev_state.split(">", 1)
                    cf, ct = state.split(">", 1)
                except ValueError:
                    pf = pt = cf = ct = None
                if pf is not None and cf == pt and ct == pf:
                    log.info("Hysteresis: suppressing reversal %s on %s",
                             state, key)
                    return False
        self._last_state[key] = (state, now)
        return True

    def seen_event(self, event_id: str) -> bool:
        if event_id in self._seen_ids:
            return True
        self._seen_ids.append(event_id)
        return False

    def should_send(self, dedup_key: str) -> bool:
        now = time.time()
        if dedup_key in self._last_sent:
            if now - self._last_sent[dedup_key] < self._dedup_window:
                return False
        while self._minute_log and now - self._minute_log[0] > 60:
            self._minute_log.popleft()
        if len(self._minute_log) >= self._max_per_min:
            log.warning("Rate limit hit (%d msgs/min), dropping: %s",
                        self._max_per_min, dedup_key)
            return False
        return True

    def record_sent(self, dedup_key: str):
        now = time.time()
        self._last_sent[dedup_key] = now
        self._minute_log.append(now)
        self._prune_counter += 1
        if self._prune_counter >= 100:
            self._prune_counter = 0
            cutoff = now - 3600
            self._last_sent = {k: v for k, v in self._last_sent.items()
                               if v > cutoff}


# ── Transport protocol ───────────────────────────────────────────────────────
#
# Stage 1 of platform-agnostic broadcasting (2026-08-05). Everything upstream
# of here — TierRouter, CardFormatter, CardRenderer — is already platform
# neutral; only the send path knew about Telegram. This protocol is the seam.
#
# A Transport takes an already-formatted payload and delivers it to one
# platform. It does NOT decide what to send, format text, or render images.
# `tier` stays in the interface because routing produces tier decisions;
# each transport maps tiers onto its own reality (Telegram: two channels;
# a single-account platform: one destination, or drop the paid tier).
#
# Adding a platform = implement this protocol + register it in the fan-out.
# Do not add platform-specific branches upstream of this line.

class Transport:
    """One delivery destination. Formatting happens upstream."""

    name = "transport"

    def send_text(self, tier: str, message: str, event_id: str = "",
                  event_type: str = "") -> bool:
        raise NotImplementedError

    def send_image(self, tier: str, png_bytes: bytes, caption: str = "",
                   event_id: str = "", event_type: str = "",
                   fallback_text: str = "") -> bool:
        raise NotImplementedError

    def stats(self) -> dict:
        return {}


# ── ChannelOps (Telegram transport) ──────────────────────────────────────────

class ChannelOps(Transport):
    """Sends formatted messages to Telegram channels via urllib.request.

    Implements Transport. The tier-specific methods (send_free/send_paid/...)
    are retained as the Telegram-native API and remain the call path used by
    the existing 12 call sites; send_text/send_image are the platform-neutral
    entry points that dispatch to them.
    """

    name = "telegram"

    def __init__(self, bot_token: str, free_chat_id: str, paid_chat_id: str,
                 personal_chat_id: str = "",
                 log_path: str = "signals_sent.log"):
        self._token = bot_token
        self._free_chat = free_chat_id
        self._paid_chat = paid_chat_id
        self._personal_chat = personal_chat_id
        self._log_path = log_path
        self._file_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._daily_stats = {
            "free_sent": 0, "paid_sent": 0, "failed": 0,
            "last_free_ts": "", "last_paid_ts": "",
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }

    def send_free(self, message: str, event_id: str = "",
                  event_type: str = "") -> bool:
        if not self._free_chat:
            return False
        ok = self._send(self._free_chat, message)
        self._log_attempt("free", event_type, event_id, ok)
        with self._stats_lock:
            self._reset_if_new_day()
            if ok:
                self._daily_stats["free_sent"] += 1
                self._daily_stats["last_free_ts"] = datetime.now(timezone.utc).isoformat()
            else:
                self._daily_stats["failed"] += 1
        return ok

    def send_paid(self, message: str, event_id: str = "",
                  event_type: str = "") -> bool:
        if not self._paid_chat:
            # Was a silent return — 156 paid cards (all 58 TRADE_OPEN + 6
            # TRADE_CLOSE) vanished on 2026-07-28 with no trace. Never drop
            # a send quietly.
            # ...and count it. The log line alone left failed_today at 0
            # while cards vanished — a counter that reads healthy because
            # the failure path returned before reaching it. A drop must move
            # a number, not just emit a line someone has to be reading.
            with self._stats_lock:
                self._reset_if_new_day()
                self._daily_stats["failed"] += 1
                _warn = not getattr(self, "_warned_unset_paid", False)
                self._warned_unset_paid = True
            if _warn:
                log.warning("PAID CHANNEL UNSET — dropping %s (%s) and any "
                            "further paid cards today. Counted in "
                            "failed_today. Set telegram_paid_chat_id in "
                            "signal_config.json.",
                            event_type or "card", event_id or "-")
            self._log_attempt("paid", event_type, event_id, False)
            return False
        ok = self._send(self._paid_chat, message)
        self._log_attempt("paid", event_type, event_id, ok)
        with self._stats_lock:
            self._reset_if_new_day()
            if ok:
                self._daily_stats["paid_sent"] += 1
                self._daily_stats["last_paid_ts"] = datetime.now(timezone.utc).isoformat()
            else:
                self._daily_stats["failed"] += 1
        return ok

    def send_paid_image(self, png_bytes: bytes, caption: str = "",
                        event_id: str = "", event_type: str = "",
                        copyable_block: str = "",
                        text_fallback: str = "") -> bool:
        """Send a PNG image card to the paid channel via sendPhoto.

        If *copyable_block* is provided, it is placed in the photo caption
        using Markdown parse mode so backtick code spans render as native
        tap-to-copy buttons on mobile — including when the user opens the
        image fullscreen (separate follow-up messages are hidden in that
        view, captions are not).

        Falls back to text sendMessage if the photo upload fails.
        Uses *text_fallback* for the fallback message.
        """
        if not self._paid_chat:
            # An unconfigured channel is NOT a routing no-op — that framing is
            # what let 156 paid cards (all 58 TRADE_OPEN) vanish on 2026-07-28
            # with nothing recording it. A card that does not arrive is a
            # failure regardless of why, so it moves the counter and lands in
            # signals_sent.log.
            #
            # The original concern was noise, and it was fair: warning on
            # every signal is how a warning gets ignored. So count always,
            # log once per process.
            with self._stats_lock:
                self._reset_if_new_day()
                self._daily_stats["failed"] += 1
                _warn = not getattr(self, "_warned_unset_paid", False)
                self._warned_unset_paid = True
            if _warn:
                log.warning("PAID CHANNEL UNSET — dropping %s image (%s) and "
                            "any further paid cards today. Counted in "
                            "failed_today. Set telegram_paid_chat_id in "
                            "signal_config.json.",
                            event_type or "card", event_id or "-")
            self._log_attempt("paid", event_type, event_id, False)
            return False
        if copyable_block:
            # Caption carries the copy values in Markdown so backticks
            # render as tap-to-copy in the fullscreen image view.
            ok = self._send_photo(self._paid_chat, png_bytes,
                                  caption=copyable_block,
                                  parse_mode="Markdown")
        else:
            ok = self._send_photo(self._paid_chat, png_bytes, caption)
        if not ok:
            # Fallback to text — never drop a signal
            fallback = text_fallback or copyable_block or caption
            log.warning("sendPhoto failed, falling back to text for %s", event_type)
            ok = self._send(self._paid_chat, fallback)
        self._log_attempt("paid_image", event_type, event_id, ok)
        with self._stats_lock:
            self._reset_if_new_day()
            if ok:
                self._daily_stats["paid_sent"] += 1
                self._daily_stats["last_paid_ts"] = datetime.now(timezone.utc).isoformat()
            else:
                self._daily_stats["failed"] += 1
        return ok

    def send_free_image(self, png_bytes: bytes, caption: str = "",
                        event_id: str = "", event_type: str = "") -> bool:
        """Send a PNG image card to the free channel via sendPhoto."""
        if not self._free_chat:
            return False
        ok = self._send_photo(self._free_chat, png_bytes, caption)
        if not ok:
            log.warning("sendPhoto (free) failed, falling back to text for %s", event_type)
            ok = self._send(self._free_chat, caption)
        self._log_attempt("free_image", event_type, event_id, ok)
        with self._stats_lock:
            self._reset_if_new_day()
            if ok:
                self._daily_stats["free_sent"] += 1
                self._daily_stats["last_free_ts"] = datetime.now(timezone.utc).isoformat()
            else:
                self._daily_stats["failed"] += 1
        return ok

    def send_personal(self, message: str) -> bool:
        if not self._personal_chat:
            return False
        ok = self._send(self._personal_chat, message)
        self._log_attempt("personal", "DIAGNOSTIC", "", ok)
        return ok

    # ── Transport protocol ──────────────────────────────────────────────
    # Neutral entry points that dispatch to the tier-specific Telegram
    # methods above. They add no behavior of their own, so an existing
    # call site and its send_text/send_image equivalent are identical.

    def send_text(self, tier: str, message: str, event_id: str = "",
                  event_type: str = "") -> bool:
        if tier == "paid":
            return self.send_paid(message, event_id, event_type)
        if tier == "free":
            return self.send_free(message, event_id, event_type)
        if tier == "personal":
            return self.send_personal(message)
        log.warning("%s: unknown tier %r for %s", self.name, tier,
                    event_type or "card")
        return False

    def send_image(self, tier: str, png_bytes: bytes, caption: str = "",
                   event_id: str = "", event_type: str = "",
                   copyable_block: str = "", text_fallback: str = "") -> bool:
        if tier == "paid":
            return self.send_paid_image(png_bytes, caption, event_id,
                                        event_type, copyable_block,
                                        text_fallback)
        if tier == "free":
            return self.send_free_image(png_bytes, caption, event_id,
                                        event_type)
        log.warning("%s: unknown tier %r for %s image", self.name, tier,
                    event_type or "card")
        return False

    def stats(self) -> dict:
        with self._stats_lock:
            self._reset_if_new_day()
            return {
                "free_sent_today": self._daily_stats["free_sent"],
                "paid_sent_today": self._daily_stats["paid_sent"],
                "failed_today": self._daily_stats["failed"],
                "last_free_ts": self._daily_stats["last_free_ts"],
                "last_paid_ts": self._daily_stats["last_paid_ts"],
            }

    def _reset_if_new_day(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_stats["date"] != today:
            self._daily_stats = {
                "free_sent": 0, "paid_sent": 0, "failed": 0,
                "last_free_ts": "", "last_paid_ts": "", "date": today,
            }
            # Re-arm the once-per-run warnings. Process-scoped flags plus a
            # daily counter reset means a broadcaster running a week warns on
            # day one and then increments in silence for six more. One warning
            # per DAY honors the noise concern without a day passing that says
            # nothing.
            self._warned_unset_paid = False

    def _send(self, chat_id: str, message: str) -> bool:
        if not self._token:
            log.error("Cannot send: telegram_bot_token is not configured")
            return False
        if not chat_id:
            # An unconfigured channel (e.g. no paid/personal chat set) is a
            # routing no-op, not a failure. Logging it as an error every cycle
            # buries real problems.
            log.debug("Skipping send — chat_id not configured")
            return False

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")

        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        log.info("Sent to chat …%s OK", str(chat_id)[-4:])
                        return True
                    log.warning("Telegram returned status %s", resp.status)
                    return False
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    try:
                        body = json.loads(e.read().decode("utf-8"))
                        wait = body.get("parameters", {}).get("retry_after", 5)
                    except Exception:
                        wait = 5
                    log.warning("Telegram 429, retrying after %ds", wait)
                    time.sleep(wait)
                    continue
                elif e.code in (400, 403):
                    log.error("Telegram permanent error %d: %s", e.code, e.reason)
                    return False
                else:
                    log.warning("Telegram HTTP error %d", e.code)
                    return False
            except Exception as e:
                log.warning("Telegram send error: %s", e)
                return False

        return False

    def _send_photo(self, chat_id: str, png_bytes: bytes,
                    caption: str = "", parse_mode: str = "HTML") -> bool:
        """Upload a PNG via Telegram sendPhoto (multipart/form-data).

        *parse_mode* controls how the caption is rendered. Use "Markdown"
        when the caption contains backtick code blocks that need to render
        as tap-to-copy on mobile.
        """
        if not self._token:
            log.error("Cannot sendPhoto: telegram_bot_token is not configured")
            return False
        if not chat_id:
            log.debug("Skipping sendPhoto — chat_id not configured")
            return False

        url = f"https://api.telegram.org/bot{self._token}/sendPhoto"
        boundary = "----GoldenEyeBoundary"

        # Build multipart body
        parts = []
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                     f"{chat_id}\r\n")
        if caption:
            # Truncate caption to Telegram's 1024-char limit for photos
            cap = caption[:1024]
            parts.append(f"--{boundary}\r\n"
                         f'Content-Disposition: form-data; name="caption"\r\n\r\n'
                         f"{cap}\r\n")
            parts.append(f"--{boundary}\r\n"
                         f'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'
                         f"{parse_mode}\r\n")
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="photo"; '
                     f'filename="signal.png"\r\n'
                     f"Content-Type: image/png\r\n\r\n")

        body = b""
        for p in parts:
            body += p.encode("utf-8")
        body += png_bytes
        body += f"\r\n--{boundary}--\r\n".encode("utf-8")

        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    url, data=body,
                    headers={
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status == 200:
                        log.info("sendPhoto to chat …%s OK", str(chat_id)[-4:])
                        return True
                    log.warning("sendPhoto returned status %s", resp.status)
                    return False
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    try:
                        body_json = json.loads(e.read().decode("utf-8"))
                        wait = body_json.get("parameters", {}).get("retry_after", 5)
                    except Exception:
                        wait = 5
                    log.warning("sendPhoto 429, retrying after %ds", wait)
                    time.sleep(wait)
                    continue
                elif e.code in (400, 403):
                    log.error("sendPhoto permanent error %d: %s", e.code, e.reason)
                    return False
                else:
                    log.warning("sendPhoto HTTP error %d", e.code)
                    return False
            except Exception as e:
                log.warning("sendPhoto error: %s", e)
                return False

        return False

    def _send_code_block(self, chat_id: str, markdown_text: str) -> bool:
        """Send a Markdown code block message (for one-tap copy on mobile)."""
        if not self._token:
            log.error("Cannot send code block: telegram_bot_token is not configured")
            return False
        if not chat_id:
            log.debug("Skipping code block — chat_id not configured")
            return False

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": markdown_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "disable_notification": True,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    log.info("Code block sent to chat …%s OK", str(chat_id)[-4:])
                    return True
                log.warning("Code block send returned status %s", resp.status)
                return False
        except Exception as e:
            log.warning("Code block send error: %s", e)
            return False

    def _log_attempt(self, tier: str, event_type: str, event_id: str,
                     success: bool):
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tier": tier, "event_type": event_type,
            "event_id": event_id, "success": success,
        }
        line = json.dumps(record) + "\n"
        with self._file_lock:
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as e:
                log.error("Failed to write to %s: %s", self._log_path, e)


# ── XTransport (X / Twitter) ─────────────────────────────────────────────────

class XTransport(Transport):
    """Posts cards to X via API v2. Implements Transport (Stage 2).

    Auth is OAuth 1.0a user context (HMAC-SHA1), signed with the standard
    library — no extra dependency. Verified against the RFC 5849 reference
    vector.

    Tier mapping: X has no free/paid split. `free` posts; `paid` is dropped
    by default (posting paid-tier content publicly would give it away). Set
    post_paid=True to deliberately mirror paid content.

    Character budget: TEXT_LIMIT (280). Bodies are converted to plain text
    and trimmed on a line boundary. Image posts carry the PNG plus the
    caption, so the budget applies to the caption alone.

    UNCONFIGURED IS NOT SILENT. Missing credentials log a warning and count
    as a failure, never a quiet False — that is the exact bug class this
    codebase already paid for once (156 cards dropped 2026-07-28).
    """

    name = "x"
    TEXT_LIMIT = 280
    API_TWEETS = "https://api.twitter.com/2/tweets"
    API_UPLOAD = "https://upload.twitter.com/1.1/media/upload.json"

    def __init__(self, consumer_key: str = "", consumer_secret: str = "",
                 access_token: str = "", access_secret: str = "",
                 post_paid: bool = False, enabled: bool = True,
                 log_path: str = "signals_sent.log"):
        self._ck = consumer_key
        self._cs = consumer_secret
        self._at = access_token
        self._as = access_secret
        self._post_paid = post_paid
        self._enabled = enabled
        self._log_path = log_path
        self._lock = threading.Lock()
        self._stats = {"posted": 0, "failed": 0, "skipped": 0}

    def configured(self) -> bool:
        return bool(self._ck and self._cs and self._at and self._as)

    # ── OAuth 1.0a ──────────────────────────────────────────────────────
    @staticmethod
    def _q(s: str) -> str:
        return urllib.parse.quote(str(s), safe="~")

    def _auth_header(self, method: str, url: str, params: dict) -> str:
        oauth = {
            "oauth_consumer_key": self._ck,
            "oauth_nonce": secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self._at,
            "oauth_version": "1.0",
        }
        allp = {**(params or {}), **oauth}
        norm = "&".join(f"{self._q(k)}={self._q(allp[k])}"
                        for k in sorted(allp))
        base = "&".join([method.upper(), self._q(url), self._q(norm)])
        key = f"{self._q(self._cs)}&{self._q(self._as)}"
        oauth["oauth_signature"] = base64.b64encode(
            hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
        ).decode()
        return "OAuth " + ", ".join(
            f'{self._q(k)}="{self._q(v)}"' for k, v in sorted(oauth.items()))

    def _post_json(self, payload: dict) -> tuple:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.API_TWEETS, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": self._auth_header(
                         "POST", self.API_TWEETS, {})})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return True, r.read().decode("utf-8", "replace")[:200]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            return False, f"HTTP {e.code}: {detail}"
        except Exception as e:
            return False, str(e)

    # ── Transport protocol ──────────────────────────────────────────────
    def _guard(self, tier: str, event_type: str) -> Optional[bool]:
        if not self._enabled:
            self._bump("skipped")
            return False
        if tier == "paid" and not self._post_paid:
            log.info("x: skipping paid-tier %s (post_paid=False)",
                     event_type or "card")
            self._bump("skipped")
            return False
        if not self.configured():
            log.warning("X NOT CONFIGURED — dropping %s (%s). Set x_consumer_key/"
                        "x_consumer_secret/x_access_token/x_access_secret in "
                        "signal_config.json.", event_type or "card", tier)
            self._bump("failed")
            return False
        return None

    def send_text(self, tier: str, message: str, event_id: str = "",
                  event_type: str = "") -> bool:
        guard = self._guard(tier, event_type)
        if guard is not None:
            return guard
        text = CardFormatter.fit(CardFormatter.to_plain(message),
                                 self.TEXT_LIMIT)
        ok, detail = self._post_json({"text": text})
        if not ok:
            log.warning("x: post failed for %s — %s", event_type or "card",
                        detail)
        self._bump("posted" if ok else "failed")
        self._log_attempt(tier, event_type, event_id, ok)
        return ok

    def send_image(self, tier: str, png_bytes: bytes, caption: str = "",
                   event_id: str = "", event_type: str = "",
                   copyable_block: str = "", text_fallback: str = "") -> bool:
        # Media upload is v1.1 multipart and needs its own signing pass;
        # until that is implemented and tested against the live API, fall
        # back to the text card rather than pretending an image went out.
        body = text_fallback or caption
        if not body:
            log.warning("x: image send for %s has no text fallback — dropping",
                        event_type or "card")
            self._bump("failed")
            return False
        log.info("x: image not yet supported, posting text fallback for %s",
                 event_type or "card")
        return self.send_text(tier, body, event_id, event_type)

    def _bump(self, key: str) -> None:
        with self._lock:
            self._stats[key] = self._stats.get(key, 0) + 1

    def _log_attempt(self, tier: str, event_type: str, event_id: str,
                     ok: bool) -> None:
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"),
                    "tier": f"x_{tier}", "event_type": event_type,
                    "event_id": event_id, "success": ok}) + "\n")
        except Exception:
            pass

    def stats(self) -> dict:
        with self._lock:
            return {"platform": "x", "configured": self.configured(),
                    **self._stats}


# ── TransportFan ─────────────────────────────────────────────────────────────

class TransportFan(Transport):
    """Fans one send out to N transports and counts attempted vs delivered.

    Drop-in for a single Transport: same interface, so call sites are
    unchanged. With one transport registered, behavior is identical to
    calling that transport directly (the return value is its return value).

    The counting is the point. Both 2026-08-05 signal bugs had the same
    signature — content went nowhere and nothing said so. Every added
    platform multiplies that surface, so the fan-out records attempted and
    delivered per platform and logs any divergence. Absence of output must
    produce evidence of itself.
    """

    name = "fan"

    def __init__(self, transports: list):
        self._transports = [t for t in transports if t is not None]
        self._lock = threading.Lock()
        self._counts: dict = {}

    def _tally(self, tname: str, ok: bool) -> None:
        with self._lock:
            c = self._counts.setdefault(
                tname, {"attempted": 0, "delivered": 0, "failed": 0})
            c["attempted"] += 1
            c["delivered" if ok else "failed"] += 1

    def _fan(self, method: str, tier: str, *args, **kwargs) -> bool:
        any_ok = False
        for t in self._transports:
            try:
                ok = bool(getattr(t, method)(tier, *args, **kwargs))
            except Exception as e:
                ok = False
                log.error("transport %s %s failed: %s",
                          getattr(t, "name", "?"), method, e)
            self._tally(getattr(t, "name", "?"), ok)
            if not ok:
                log.warning("transport %s did not deliver %s (%s)",
                            getattr(t, "name", "?"),
                            kwargs.get("event_type") or "card", tier)
            any_ok = any_ok or ok
        return any_ok

    def send_text(self, tier: str, message: str, event_id: str = "",
                  event_type: str = "") -> bool:
        return self._fan("send_text", tier, message,
                         event_id=event_id, event_type=event_type)

    def send_image(self, tier: str, png_bytes: bytes, caption: str = "",
                   event_id: str = "", event_type: str = "",
                   copyable_block: str = "", text_fallback: str = "") -> bool:
        return self._fan("send_image", tier, png_bytes, caption,
                         event_id=event_id, event_type=event_type,
                         copyable_block=copyable_block,
                         text_fallback=text_fallback)

    # ── Telegram-named compatibility surface ────────────────────────────
    # The 12 existing call sites speak the tier-named API. The fan must be a
    # true drop-in for ChannelOps or enabling a second transport would break
    # every one of them, so mirror that surface onto the neutral protocol.

    def send_free(self, message: str, event_id: str = "",
                  event_type: str = "") -> bool:
        return self.send_text("free", message, event_id, event_type)

    def send_paid(self, message: str, event_id: str = "",
                  event_type: str = "") -> bool:
        return self.send_text("paid", message, event_id, event_type)

    def send_personal(self, message: str) -> bool:
        return self.send_text("personal", message)

    def send_free_image(self, png_bytes: bytes, caption: str = "",
                        event_id: str = "", event_type: str = "") -> bool:
        return self.send_image("free", png_bytes, caption, event_id,
                               event_type)

    def send_paid_image(self, png_bytes: bytes, caption: str = "",
                        event_id: str = "", event_type: str = "",
                        copyable_block: str = "",
                        text_fallback: str = "") -> bool:
        return self.send_image("paid", png_bytes, caption, event_id,
                               event_type, copyable_block, text_fallback)

    def stats(self) -> dict:
        with self._lock:
            out = {"transports": {k: dict(v) for k, v in self._counts.items()}}
        for t in self._transports:
            try:
                out.setdefault("per_platform", {})[
                    getattr(t, "name", "?")] = t.stats()
            except Exception:
                pass
        return out


# ── Scheduled Jobs ───────────────────────────────────────────────────────────

def _http_get_json(url: str, timeout: float = 10.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("HTTP GET %s failed: %s", url, e)
        return None


def _seconds_until(hour: int, minute: int = 0) -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _seconds_until_weekday(weekday: int, hour: int, minute: int = 0) -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = weekday - now.weekday()
    if days_ahead < 0 or (days_ahead == 0 and target <= now):
        days_ahead += 7
    target += timedelta(days=days_ahead)
    return (target - now).total_seconds()


class DailySummaryJob:
    """Sends a daily summary at 08:00 UTC to both channels."""

    def __init__(self, cc_url: str, channel_ops: ChannelOps,
                 formatter: CardFormatter):
        self._cc_url = cc_url.rstrip("/")
        self._channel = channel_ops
        self._formatter = formatter
        self._stop = threading.Event()

    def start(self) -> None:
        self._stop.clear()
        t = threading.Thread(target=self._loop, daemon=True, name="daily-summary")
        t.start()
        log.info("DailySummaryJob started, next in %.0fs", _seconds_until(8, 0))

    def stop(self) -> None:
        self._stop.set()

    def run_now(self) -> None:
        self._execute()

    def _loop(self):
        while not self._stop.is_set():
            if self._stop.wait(timeout=_seconds_until(8, 0)):
                break
            self._execute()

    def _execute(self):
        log.info("DailySummaryJob executing")
        try:
            daily = _http_get_json(f"{self._cc_url}/api/fleet/daily") or {}
            expectancy = _http_get_json(f"{self._cc_url}/api/expectancy") or {}
            portfolio = _http_get_json(f"{self._cc_url}/api/portfolio") or {}

            stats = {
                "fleet_pnl": daily.get("fleet_pnl", daily.get("pnl", "\u2014")),
                "total_trades": daily.get("total_trades", "\u2014"),
                "win_rate": daily.get("win_rate", "\u2014"),
                "expectancy": expectancy.get("fleet_expectancy", "\u2014"),
                # Carry the denominator onto the CARD, not just the payload.
                # A bare "-$35.42/trade" labelled *fleet* reads as "the fleet
                # is losing money" when it means "two bots took twelve trades
                # between them". A coverage note sitting in an API nobody
                # reads protects nobody.
                "participating_bots": expectancy.get("participating_bots"),
                "fleet_members": expectancy.get("fleet_members"),
                "deployed_pct": portfolio.get("deployed_pct", "\u2014"),
                "regime": daily.get("regime", "\u2014"),
                "aegis_score": daily.get("aegis_score", "\u2014"),
                "top_bot": daily.get("top_bot", "\u2014"),
                "top_pair": daily.get("top_pair", "\u2014"),
            }
            msg_paid = self._formatter.format_daily_summary(stats)
            msg_free = self._formatter.format_daily_summary_free(stats)
            self._channel.send_paid(msg_paid, event_type="DAILY_SUMMARY")
            self._channel.send_free(msg_free, event_type="DAILY_SUMMARY")
            log.info("Daily summary sent")
        except Exception:
            log.exception("DailySummaryJob failed")


class WeeklyReportJob:
    """Sends a weekly report every Sunday at 08:00 UTC."""

    def __init__(self, cc_url: str, channel_ops: ChannelOps,
                 formatter: CardFormatter):
        self._cc_url = cc_url.rstrip("/")
        self._channel = channel_ops
        self._formatter = formatter
        self._stop = threading.Event()
        self._logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "logs", "daily")

    def start(self) -> None:
        self._stop.clear()
        t = threading.Thread(target=self._loop, daemon=True, name="weekly-report")
        t.start()
        log.info("WeeklyReportJob started")

    def stop(self) -> None:
        self._stop.set()

    def run_now(self) -> None:
        self._execute()

    def _loop(self):
        while not self._stop.is_set():
            if self._stop.wait(timeout=_seconds_until_weekday(6, 8, 0)):
                break
            self._execute()

    def _execute(self):
        log.info("WeeklyReportJob executing")
        try:
            today = datetime.now(timezone.utc).date()
            total_pnl = 0.0
            total_trades = 0
            win_rates = []
            expectancies = []
            best_day = None
            best_pnl = float("-inf")
            worst_day = None
            worst_pnl = float("inf")
            days_positive = 0
            days_loaded = 0

            for i in range(7):
                day = today - timedelta(days=i)
                day_str = day.strftime("%Y-%m-%d")
                path = os.path.join(self._logs_dir, f"{day_str}.json")
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    continue

                days_loaded += 1
                day_pnl = data.get("fleet_pnl", data.get("pnl", 0))
                if isinstance(day_pnl, (int, float)):
                    total_pnl += day_pnl
                    if day_pnl > best_pnl:
                        best_pnl = day_pnl
                        best_day = f"{day_str} (${day_pnl:+.2f})"
                    if day_pnl < worst_pnl:
                        worst_pnl = day_pnl
                        worst_day = f"{day_str} (${day_pnl:+.2f})"
                    if day_pnl > 0:
                        days_positive += 1

                dt = data.get("total_trades", 0)
                if isinstance(dt, int):
                    total_trades += dt
                wr = data.get("win_rate")
                if isinstance(wr, (int, float)):
                    win_rates.append(wr)
                ev = data.get("expectancy")
                if isinstance(ev, (int, float)):
                    expectancies.append(ev)

            report = {
                "total_pnl": total_pnl if days_loaded else "\u2014",
                "total_trades": total_trades if days_loaded else "\u2014",
                "avg_win_rate": (f"{sum(win_rates) / len(win_rates):.0%}"
                                 if win_rates else "\u2014"),
                "avg_expectancy": (sum(expectancies) / len(expectancies)
                                   if expectancies else "\u2014"),
                "best_day": best_day or "\u2014",
                "worst_day": worst_day or "\u2014",
                "days_positive": days_positive,
                "days_total": days_loaded or 7,
            }
            msg_paid = self._formatter.format_weekly_report(report)
            msg_free = self._formatter.format_weekly_report_free(report)
            self._channel.send_paid(msg_paid, event_type="WEEKLY_REPORT")
            self._channel.send_free(msg_free, event_type="WEEKLY_REPORT")
            log.info("Weekly report sent")
        except Exception:
            log.exception("WeeklyReportJob failed")


class EndOfDayJob:
    """Sends visual End of Day card at configurable UTC time (default 23:59)."""

    def __init__(self, cc_url: str, channel_ops: ChannelOps,
                 card_renderer, hour: int = 23, minute: int = 59):
        self._cc_url = cc_url.rstrip("/")
        self._channel = channel_ops
        self._renderer = card_renderer  # CardRenderer instance or None
        self._hour = hour
        self._minute = minute
        self._stop = threading.Event()

    def start(self) -> None:
        self._stop.clear()
        t = threading.Thread(target=self._loop, daemon=True, name="end-of-day")
        t.start()
        log.info("EndOfDayJob started, fires at %02d:%02d UTC (next in %.0fs)",
                 self._hour, self._minute,
                 _seconds_until(self._hour, self._minute))

    def stop(self) -> None:
        self._stop.set()

    def run_now(self) -> None:
        """Manual trigger for testing."""
        self._execute()

    def _loop(self):
        while not self._stop.is_set():
            if self._stop.wait(timeout=_seconds_until(self._hour, self._minute)):
                break
            self._execute()

    def _execute(self):
        log.info("EndOfDayJob executing")
        try:
            data = self._gather_data()
            if not self._renderer:
                log.warning("EndOfDayJob: no CardRenderer, skipping image cards")
                return

            # Paid card
            try:
                png_paid = self._renderer.render_end_of_day(data, tier="paid")
                self._channel.send_paid_image(
                    png_paid, caption="End of Day — GoldenEye Intelligence",
                    event_type="END_OF_DAY")
            except Exception:
                log.exception("EOD paid image failed")

            # Free card
            try:
                png_free = self._renderer.render_end_of_day(data, tier="free")
                self._channel.send_free_image(
                    png_free, caption="End of Day — GoldenEye Intelligence",
                    event_type="END_OF_DAY")
            except Exception:
                log.exception("EOD free image failed")

            log.info("End of Day cards sent")
        except Exception:
            log.exception("EndOfDayJob failed")

    def _gather_data(self) -> dict:
        """Pull live fleet data and build the EOD data dict.

        Data sourcing strategy (multi-tier fallback):
          1. Primary:  /api/events/recent?type=TRADE_CLOSE (volatile RAM ring buffer)
          2. Fallback: /api/trades?limit=200 (persistent disk-backed event log)
          3. Sanity:   /api/expectancy for cross-check stats

        The bus ring buffer (max_history=1000) can rotate today's trades out
        before 23:59 UTC when physics engines flood the bus. Disk-backed
        /api/trades reads from logs/events/YYYY-MM-DD.jsonl and never loses data.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_display = datetime.now(timezone.utc).strftime("%d %b %Y")
        today_start_unix = datetime.strptime(today, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()
        today_end_unix = today_start_unix + 86400

        today_trades: list[dict] = []
        source_used = "none"

        # ── Primary source: bus ring buffer ──
        try:
            events = _http_get_json(
                f"{self._cc_url}/api/events/recent?n=500&type=TRADE_CLOSE") or []
            for ev in events:
                # Bus events store ts as a float Unix timestamp, not an ISO string
                ts_raw = ev.get("ts", ev.get("timestamp", 0))
                try:
                    ts_float = float(ts_raw)
                except (ValueError, TypeError):
                    continue
                if not (today_start_unix <= ts_float < today_end_unix):
                    continue
                d = ev.get("data", {}) if isinstance(ev.get("data"), dict) else {}
                pnl = d.get("pnl")
                if not isinstance(pnl, (int, float)):
                    continue
                hour = int(time.strftime("%H", time.gmtime(ts_float)))
                # Gross semantics (2026-07-30): pnl IS the number — no fee
                # deduction. "net" and "fees" keys kept for card-shape compat.
                today_trades.append({
                    "bot": ev.get("source") or d.get("bot", ""),
                    "pair": d.get("pair", ""),
                    "side": str(d.get("direction", "")).upper(),
                    "net": pnl,
                    "hour": hour,
                    "exit_reason": d.get("exit_reason", d.get("reason", "")),
                    "gross_pnl": pnl,
                    "fees": 0,
                })
            if today_trades:
                source_used = "events_recent"
                log.info("EOD data: %d trades from /api/events/recent",
                         len(today_trades))
        except Exception:
            log.exception("EOD primary source (events/recent) failed")

        # ── Fallback: /api/trades reads from disk-backed event log ──
        if not today_trades:
            try:
                trades_resp = _http_get_json(
                    f"{self._cc_url}/api/trades?limit=500") or {}
                trade_list = trades_resp.get("trades", []) if isinstance(
                    trades_resp, dict) else []
                seen_keys = set()  # dedup the snapshot-diff vs release double-emit
                for t in trade_list:
                    ts_float = t.get("ts", 0)
                    try:
                        ts_float = float(ts_float)
                    except (ValueError, TypeError):
                        continue
                    if not (today_start_unix <= ts_float < today_end_unix):
                        continue
                    pair = t.get("pair", "")
                    bot = t.get("bot", "")
                    pnl = t.get("pnl")
                    if not isinstance(pnl, (int, float)):
                        continue
                    # Dedup key: same bot + pair + rounded pnl + 60s window
                    # collapses the snapshot-diff TRADE_CLOSE and the
                    # _handle_release TRADE_CLOSE for the same underlying trade
                    dkey = (bot, pair, round(float(pnl), 2),
                            int(ts_float / 60))
                    if dkey in seen_keys:
                        continue
                    seen_keys.add(dkey)
                    # Gross semantics (2026-07-30): pnl is gross price
                    # movement — no fee reconstruction.
                    gross = t.get("gross_pnl", pnl)
                    if not isinstance(gross, (int, float)):
                        gross = pnl
                    hour = int(time.strftime("%H", time.gmtime(ts_float)))
                    today_trades.append({
                        "bot": bot,
                        "pair": pair,
                        "side": str(t.get("direction", "")).upper(),
                        "net": pnl,
                        "hour": hour,
                        "exit_reason": t.get("exit_reason", ""),
                        "gross_pnl": gross,
                        "fees": 0,
                    })
                if today_trades:
                    source_used = "api_trades_disk"
                    log.warning(
                        "EOD data: bus ring empty for today, fell back to "
                        "/api/trades disk log — recovered %d trades",
                        len(today_trades))
            except Exception:
                log.exception("EOD fallback source (api/trades) failed")

        # ── Compute aggregates (gross — signal product, 2026-07-30) ──
        gross = sum(t.get("gross_pnl", 0) or 0 for t in today_trades
                    if isinstance(t.get("gross_pnl"), (int, float)))
        fees = 0.0   # never computed; key kept for card-shape compatibility
        net = gross  # net == gross under gross semantics

        # Cross-check: pull expectancy stats but don't use them as primary
        # numbers (they're lifetime, not today-only)
        try:
            expectancy = _http_get_json(f"{self._cc_url}/api/expectancy") or {}
        except Exception:
            expectancy = {}

        if not today_trades:
            log.warning("EOD: no trades found in any source for %s "
                        "(bus, disk both empty) — card will show zeros", today)

        return {
            "date": date_display,
            "trades": today_trades,
            "gross": gross if today_trades else None,
            "fees": fees if today_trades else None,
            "net": net if today_trades else None,
            "timestamp": f"{self._hour:02d}:{self._minute:02d} UTC",
            "source": source_used,
            "fleet_expectancy_lifetime": expectancy.get("fleet_expectancy"),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# BROADCASTER — MAIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

_DEFAULT_CONFIG = {
    "cc_url": "http://localhost:9000",
    "broadcaster_port": 9002,
    "telegram_bot_token": "",
    "telegram_free_chat_id": "",
    "telegram_paid_chat_id": "",
    "telegram_personal_chat_id": "",
    "dedup_window_s": 300,
    "seen_ids_max": 1000,
    "rate_limit_per_minute": 30,
    "free_delay_hours": 4,
    "min_conviction_threshold": 0.8,
    "daily_summary_utc_hour": 8,
    "weekly_report_utc_day": 6,
    "weekly_report_utc_hour": 8,
    "poll_fallback_interval_s": 30,
    "enrichment_cache_ttl_s": 15,
    "enrichment_timeout_s": 5,
    "routing_overrides": {},
    "enabled": True,
}


class Broadcaster:
    """Main orchestrator. Wires all components, runs the event pipeline."""

    def __init__(self, config_path: str = "signal_config.json"):
        self._config_path = config_path
        self._config = self._load_config()
        self._shutdown = threading.Event()
        self._gate_lock = threading.Lock()
        self._signals_log: list[dict] = []  # last 200 signals for /api/signals/feed
        self._signals_log_lock = threading.Lock()
        self._delayed_queue: list[dict] = []
        self._delayed_lock = threading.Lock()

        cc_url = self._config["cc_url"]

        # Components
        self._gate = AlertGate(
            dedup_window_s=self._config["dedup_window_s"],
            max_seen=self._config["seen_ids_max"],
            max_per_min=self._config["rate_limit_per_minute"],
            hysteresis_window_s=int(self._config.get(
                "regime_hysteresis_window_s", 1800)),
        )
        _telegram = ChannelOps(
            bot_token=self._config["telegram_bot_token"],
            free_chat_id=self._config["telegram_free_chat_id"],
            paid_chat_id=self._config["telegram_paid_chat_id"],
            personal_chat_id=self._config.get("telegram_personal_chat_id", ""),
        )
        # Stage 2: X is opt-in. Off unless x_enabled is true AND credentials
        # are present, so the default path is byte-identical to Telegram-only.
        # When only one transport is active the fan is a pass-through.
        self._telegram = _telegram
        _transports = [_telegram]
        if self._config.get("x_enabled"):
            _x = XTransport(
                consumer_key=self._config.get("x_consumer_key", ""),
                consumer_secret=self._config.get("x_consumer_secret", ""),
                access_token=self._config.get("x_access_token", ""),
                access_secret=self._config.get("x_access_secret", ""),
                post_paid=bool(self._config.get("x_post_paid", False)),
            )
            if not _x.configured():
                log.warning("x_enabled is true but credentials are incomplete "
                            "— X posts will be counted as failures, not "
                            "silently skipped.")
            _transports.append(_x)
            log.info("Transports: telegram + x (post_paid=%s)",
                     bool(self._config.get("x_post_paid", False)))
        self._channel = (_telegram if len(_transports) == 1
                         else TransportFan(_transports))
        self._seed_gate_from_log()

        # Report channel wiring once at startup so an unconfigured channel is
        # visible here rather than as a repeated error on every send attempt.
        if not self._config.get("telegram_bot_token"):
            log.error("telegram_bot_token is not configured — no messages will send")
        _configured = [n for n, k in (
            ("free", "telegram_free_chat_id"),
            ("paid", "telegram_paid_chat_id"),
            ("personal", "telegram_personal_chat_id"),
        ) if self._config.get(k)]
        _unconfigured = [n for n in ("free", "paid", "personal") if n not in _configured]
        log.info("Telegram channels active: %s", ", ".join(_configured) or "none")
        if _unconfigured:
            log.info("Telegram channels not configured (sends will skip): %s",
                     ", ".join(_unconfigured))
        self._formatter = CardFormatter()
        self._intel = IntelligenceBuilder(
            cc_url=cc_url,
            cache_ttl=self._config["enrichment_cache_ttl_s"],
            fetch_timeout=self._config["enrichment_timeout_s"],
        )
        self._sse = SSEListener(
            cc_url=cc_url,
            on_event=self._on_event,
            on_disconnect=lambda: self._poller.set_active(True),
            on_reconnect=lambda: self._poller.set_active(False),
        )
        self._poller = PollingFallback(
            cc_url=cc_url,
            on_event=self._on_event,
            poll_interval=self._config["poll_fallback_interval_s"],
        )
        self._daily = DailySummaryJob(cc_url, self._channel, self._formatter)
        self._weekly = WeeklyReportJob(cc_url, self._channel, self._formatter)

        # Visual card renderer (Pillow) — lazy import, degrades to text-only
        self._card_renderer = None
        try:
            from card_renderer import CardRenderer
            self._card_renderer = CardRenderer()
            log.info("CardRenderer loaded — paid tier gets image cards")
        except Exception as e:
            log.warning("CardRenderer not available (%s) — text-only mode", e)

        self._eod = EndOfDayJob(
            cc_url, self._channel, self._card_renderer,
            hour=self._config.get("end_of_day_hour_utc", 23),
            minute=self._config.get("end_of_day_minute_utc", 59),
        )
        self._health = BroadcasterHealthServer(
            port=self._config["broadcaster_port"],
            get_stats=self.stats,
            on_reload=self._reload_config,
            get_feed=self.get_signals_log,
        )

    def _load_config(self) -> dict:
        config = dict(_DEFAULT_CONFIG)
        if os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    user = json.load(f)
                config.update(user)
                log.info("Config loaded from %s", self._config_path)
            except Exception as e:
                log.warning("Failed to load config: %s, using defaults", e)
        else:
            log.info("No config file at %s, using defaults", self._config_path)
        return config

    def _reload_config(self):
        self._config = self._load_config()
        self._gate = AlertGate(
            dedup_window_s=self._config["dedup_window_s"],
            max_seen=self._config["seen_ids_max"],
            max_per_min=self._config["rate_limit_per_minute"],
            hysteresis_window_s=int(self._config.get(
                "regime_hysteresis_window_s", 1800)),
        )
        # A fresh gate has an empty seen-set — re-seed so a reload doesn't
        # open a window where recently-sent events could be re-sent.
        self._seed_gate_from_log()
        log.info("Config reloaded")

    def _seed_gate_from_log(self) -> None:
        """Pre-populate the dedup gate with recently-sent event ids.

        The gate is in-memory and the watchdogs restart this process
        freely. After a restart the polling fallback re-fetches recent bus
        events, so without seeding, an event sent seconds before the
        restart would be sent to Telegram again — the subscriber sees a
        duplicate. Seed the seen-set from the tail of signals_sent.log so
        already-delivered event ids are dropped on arrival.
        """
        try:
            path = self._channel._log_path
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                tail = f.readlines()[-1000:]
            cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
            seeded = 0
            for line in tail:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                # Only successful sends — a failed attempt may deserve a
                # retry if the event is redelivered.
                if not entry.get("success"):
                    continue
                try:
                    when = datetime.strptime(
                        entry.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if when < cutoff:
                    continue
                eid = entry.get("event_id")
                if eid:
                    with self._gate_lock:
                        self._gate.seen_event(eid)
                    seeded += 1
            if seeded:
                log.info("Dedup gate seeded with %d recently-sent event ids",
                         seeded)
        except Exception:
            log.warning("Gate seeding failed (non-fatal)", exc_info=True)

    def start(self) -> None:
        # Bind the status port before anything else — it doubles as a
        # single-instance mutex. The CC probe below can take 30s; binding
        # first closes the window where two freshly spawned instances both
        # pass port_guard because neither has bound yet.
        self._health.start()

        cc_url = self._config["cc_url"]
        log.info("Probing Command Center at %s...", cc_url)
        for i in range(15):
            try:
                urllib.request.urlopen(f"{cc_url}/api/master", timeout=3)
                log.info("Command Center is up")
                break
            except Exception:
                if i < 14:
                    time.sleep(2)
        else:
            log.critical("Command Center unreachable after 30s, exiting")
            sys.exit(1)

        self._sse.start()
        self._poller.start()
        self._daily.start()
        self._weekly.start()
        self._eod.start()

        # Delayed sender thread
        t = threading.Thread(target=self._delayed_sender, daemon=True,
                             name="delayed-sender")
        t.start()

        token_status = "configured" if self._config["telegram_bot_token"] else "NOT SET"
        log.info("Signal Broadcaster online (Telegram token: %s)", token_status)

        # Startup notification disabled — too noisy during development
        log.info("Signal Broadcaster online (Telegram token: configured)")

    def stop(self) -> None:
        log.info("Shutting down...")
        self._shutdown.set()
        self._sse.stop()
        self._poller.stop()
        self._daily.stop()
        self._weekly.stop()
        self._eod.stop()
        self._health.stop()
        log.info("Shutdown complete")

    def stats(self) -> dict:
        return {
            "sse": self._sse.stats(),
            "polling": self._poller.stats(),
            "channel": self._channel.stats(),
            "config": {
                "enabled": self._config.get("enabled", True),
                "free_delay_hours": self._config.get("free_delay_hours", 4),
                "min_conviction": self._config.get("min_conviction_threshold", 0.8),
                "rate_limit": self._config.get("rate_limit_per_minute", 30),
            },
            "delayed_queue_size": len(self._delayed_queue),
            "signals_log_size": len(self._signals_log),
        }

    def config(self) -> dict:
        return dict(self._config)

    def get_signals_log(self, n: int = 50) -> list[dict]:
        with self._signals_log_lock:
            return list(self._signals_log[-n:])

    def preview(self, event: dict) -> dict:
        enriched = self._intel.enrich(event)
        decision = TierRouter.route(enriched, self._config)
        if decision is None:
            return {"suppressed": True, "reason": "no routing rule matched"}
        return {
            "suppressed": False,
            "free": decision["free"],
            "paid": decision["paid"],
            "priority": decision["priority"],
            "category": decision["category"],
            "free_message": self._formatter.format_free(decision) if decision["free"] else "",
            "paid_message": self._formatter.format_paid(decision) if decision["paid"] else "",
        }

    def _on_event(self, event: dict) -> None:
        if not self._config.get("enabled", True):
            return

        try:
            event_id = event.get("id", "")
            if not event_id:
                event_id = f"{event.get('source', '')}_{event.get('type', '')}_{event.get('ts', '')}"

            with self._gate_lock:
                if self._gate.seen_event(event_id):
                    return

            enriched = self._intel.enrich(event)
            decision = TierRouter.route(enriched, self._config)
            if decision is None:
                return

            etype = event.get("type", "")
            dedup_key = decision["dedup_key"]

            # Hysteresis: swallow A->B->A reversals (regime oscillation).
            # Checked once per candidate, before any tier gate, so a
            # suppressed reversal costs nothing downstream.
            _hk = decision.get("hysteresis_key")
            if _hk and not self._gate.check_hysteresis(
                    _hk, decision.get("hysteresis_state", "")):
                return

            # Suppress empty HIGH_CONVICTION / SIGNAL cards — no pair + no signal = no send
            if etype in ("HIGH_CONVICTION", "SIGNAL"):
                edata = event.get("data", {}) if isinstance(event.get("data"), dict) else {}
                pair = edata.get("pair") or ""
                direction = edata.get("direction") or ""
                if (not pair or pair == "\u2014") and not direction:
                    log.info("Suppressed empty %s — no pair or signal data", etype)
                    return

            # Record in signals log
            with self._signals_log_lock:
                self._signals_log.append({
                    "ts": _utc_now(),
                    "type": etype,
                    "pair": event.get("data", {}).get("pair", "") if isinstance(event.get("data"), dict) else "",
                    "free": decision["free"],
                    "paid": decision["paid"],
                    "priority": decision["priority"],
                    "category": decision["category"],
                })
                if len(self._signals_log) > 200:
                    self._signals_log = self._signals_log[-200:]

            # ── Narrator dual-channel routing (WHALE_ALERT, REGIME_CHANGE, AEGIS_UPDATE) ──
            # These events use the GoldenEye narrator voice and route to both channels
            # via module-level format_whale_narrator / format_regime_or_aegis_narrator.
            # AEGIS_UPDATE is Intelligence-only (pulse_text=None). Trade cards bypass this.
            _NARRATOR_TYPES = ("WHALE_ALERT", "REGIME_CHANGE", "AEGIS_UPDATE")
            if etype in _NARRATOR_TYPES:
                edata = event.get("data", {}) if isinstance(event.get("data"), dict) else {}
                # Merge enriched fleet context into edata for narrator
                for _fld in ("deployed_pct", "bot_count", "risk_level", "available_capital",
                             "_deployed_pct", "_bots_alive"):
                    if _fld in enriched and _fld not in edata:
                        edata[_fld] = enriched[_fld]
                if "deployed_pct" not in edata and "_deployed_pct" in edata:
                    edata["deployed_pct"] = edata["_deployed_pct"]
                if "bot_count" not in edata and "_bots_alive" in edata:
                    edata["bot_count"] = edata["_bots_alive"]

                try:
                    if etype == "WHALE_ALERT":
                        pulse_text, intel_text = format_whale_narrator(edata)
                    else:
                        pulse_text, intel_text = format_regime_or_aegis_narrator(edata, etype)
                except Exception:
                    log.warning("Narrator format failed for %s, falling through to legacy", etype,
                                exc_info=True)
                    pulse_text, intel_text = None, None

                if intel_text:
                    with self._gate_lock:
                        if self._gate.should_send(f"paid_{dedup_key}"):
                            if self._channel.send_paid(intel_text, event_id, etype):
                                self._gate.record_sent(f"paid_{dedup_key}")

                if pulse_text and self._channel._free_chat:
                    with self._gate_lock:
                        if self._gate.should_send(f"free_{dedup_key}"):
                            if self._channel.send_free(pulse_text, event_id, etype):
                                self._gate.record_sent(f"free_{dedup_key}")
                # Narrator path handled — skip legacy routing below
                return

            # Send to paid tier (immediate)
            if decision["paid"]:
                with self._gate_lock:
                    if self._gate.should_send(f"paid_{dedup_key}"):
                        msg = self._formatter.format_paid(decision)
                        sent = False
                        # Image cards for trade events
                        if self._card_renderer and etype in ("TRADE_OPEN", "TRADE_CLOSE"):
                            try:
                                edata = dict(event.get("data", {}) if isinstance(event.get("data"), dict) else {})
                                # Merge enriched top-level intelligence fields into edata
                                # so the CardRenderer has conviction, signals, regime context
                                _INTEL_FIELDS = (
                                    "conviction", "intel_score", "ensemble_score",
                                    "ensemble_direction", "top_signals", "fleet_regime",
                                    "deployed_pct", "bots_alive", "aegis_score",
                                    "fleet_expectancy", "bot_expectancy",
                                    # universal enrichment fields
                                    "_fleet_regime", "_deployed_pct", "_bots_alive",
                                )
                                for _fld in _INTEL_FIELDS:
                                    if _fld in event and _fld not in edata:
                                        edata[_fld] = event[_fld]
                                # Normalise fleet_regime from universal enrichment fallback
                                if "fleet_regime" not in edata and "_fleet_regime" in edata:
                                    edata["fleet_regime"] = edata["_fleet_regime"]
                                if "deployed_pct" not in edata and "_deployed_pct" in edata:
                                    edata["deployed_pct"] = edata["_deployed_pct"]
                                if "bots_alive" not in edata and "_bots_alive" in edata:
                                    edata["bots_alive"] = edata["_bots_alive"]
                                # Footer bot count: feed the renderer the live
                                # figure from /api/master. None when this event
                                # carried no count — the footer then omits the
                                # line instead of asserting a stale/invented
                                # number (was hardcoded "19 bots · live").
                                _ba = edata.get("bots_alive")
                                self._card_renderer.set_bots_alive(
                                    _ba if isinstance(_ba, int) else None)
                                if etype == "TRADE_OPEN":
                                    png = self._card_renderer.render_trade_open(edata)
                                    copyable = self._card_renderer.copyable_trade_open(edata)
                                else:
                                    png = self._card_renderer.render_trade_close(edata)
                                    copyable = self._card_renderer.copyable_trade_close(edata)
                                sent = self._channel.send_paid_image(
                                    png, caption="", event_id=event_id,
                                    event_type=etype,
                                    copyable_block=copyable or "",
                                    text_fallback=msg)
                            except Exception:
                                log.warning("Image card failed for %s, using text", etype,
                                            exc_info=True)
                        if not sent:
                            sent = self._channel.send_paid(msg, event_id, etype)
                        if sent:
                            self._gate.record_sent(f"paid_{dedup_key}")

            # Send to free tier (immediate or delayed)
            if decision["free"]:
                delay = decision.get("delay_free_s", 0)
                if delay > 0:
                    with self._delayed_lock:
                        self._delayed_queue.append({
                            "send_after": time.time() + delay,
                            "decision": decision,
                            "event_id": event_id,
                            "event_type": etype,
                            "dedup_key": dedup_key,
                        })
                else:
                    with self._gate_lock:
                        if self._gate.should_send(f"free_{dedup_key}"):
                            msg = self._formatter.format_free(decision)
                            if self._channel.send_free(msg, event_id, etype):
                                self._gate.record_sent(f"free_{dedup_key}")

        except Exception:
            log.exception("Pipeline error for event %s", event.get("type", "?"))

    def _delayed_sender(self) -> None:
        while not self._shutdown.is_set():
            self._shutdown.wait(60)
            now = time.time()
            to_send = []
            with self._delayed_lock:
                remaining = []
                for item in self._delayed_queue:
                    if item["send_after"] <= now:
                        to_send.append(item)
                    else:
                        remaining.append(item)
                self._delayed_queue = remaining

            for item in to_send:
                try:
                    with self._gate_lock:
                        dk = f"free_{item['dedup_key']}"
                        if self._gate.should_send(dk):
                            msg = self._formatter.format_free(item["decision"])
                            if self._channel.send_free(msg, item["event_id"],
                                                       item["event_type"]):
                                self._gate.record_sent(dk)
                except Exception:
                    log.warning("Delayed send failed", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "signal_config.json")

    # Create default config if missing
    if not os.path.exists(config_path):
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(_DEFAULT_CONFIG, f, indent=2)
        log.info("Created default config at %s", config_path)
        log.info("Edit signal_config.json with your Telegram bot token and channel IDs, then restart.")
        sys.exit(0)

    broadcaster = Broadcaster(config_path)

    def shutdown_handler(signum, frame):
        broadcaster.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    broadcaster.start()

    # Block main thread
    try:
        broadcaster._shutdown.wait()
    except KeyboardInterrupt:
        broadcaster.stop()


if __name__ == "__main__":
    main()
