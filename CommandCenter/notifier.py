#!/usr/bin/env python3
"""
Fleet Notifier — independent external watchdog for the 16-bot trading fleet.

Runs as a standalone process. Does NOT import anything from the fleet codebase.
If Command Center crashes, this process stays alive and reports it via Telegram.

Requirements: Python 3.10+, stdlib only.
Environment: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Config: notifier_config.json (same directory)

Launch:  python D:\\CommandCenter\\notifier.py
Or via:  notifier_launcher.bat  (auto-restart on crash)
"""

import collections
import json
import logging
import os
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "notifier.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("notifier")

# ── Config ──
_DIR = os.path.dirname(os.path.abspath(__file__))
_CFG_PATH = os.path.join(_DIR, "notifier_config.json")

_DEFAULTS = {
    "cc_host": "localhost",
    "cc_port": 9000,
    "socket_check_interval_s": 15,
    "http_check_interval_s": 30,
    "cc_down_threshold": 4,
    "dedup_window_s": 300,
    "seen_ids_max": 500,
    "rate_limit_per_minute": 20,
    "tier3_enabled": False,
    "tier3_types": [],
    "bot_ports": {},
}


def _load_config() -> dict:
    cfg = dict(_DEFAULTS)
    if os.path.exists(_CFG_PATH):
        try:
            with open(_CFG_PATH, "r") as f:
                cfg.update(json.load(f))
            log.info("Config loaded from %s", _CFG_PATH)
        except Exception as e:
            log.warning("Failed to load config (%s), using defaults", e)
    else:
        log.warning("No config file at %s, using defaults", _CFG_PATH)
    return cfg


# ── Telegram ──
_TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


def _escape_md2(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    for ch in r"_[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _send_telegram(message: str) -> bool:
    """Send a pre-formatted MarkdownV2 message. Returns True on success."""
    if not _TG_TOKEN or not _TG_CHAT:
        return False
    url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": _TG_CHAT,
        "text": message,
        "parse_mode": "MarkdownV2",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True
            log.warning("Telegram returned status %s", resp.status)
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
    return False


# ── Rate limiter / dedup ──
class AlertGate:
    """Deduplication + rate limiting for outbound alerts."""

    def __init__(self, dedup_window_s: int, max_seen: int, max_per_min: int):
        self._dedup_window = dedup_window_s
        self._seen_ids: collections.deque = collections.deque(maxlen=max_seen)
        self._last_sent: dict[str, float] = {}
        self._minute_log: collections.deque = collections.deque()
        self._max_per_min = max_per_min
        self._prune_counter = 0

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
            log.warning("Rate limit hit (%d msgs/min), dropping: %s", self._max_per_min, dedup_key)
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
            self._last_sent = {k: v for k, v in self._last_sent.items() if v > cutoff}


# ── Network helpers ──
def _check_socket(host: str, port: int, timeout: float = 5.0) -> bool:
    """Raw TCP connect. Returns True if port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _http_get(host: str, port: int, path: str, timeout: float = 8.0):
    """GET a JSON endpoint. Returns parsed JSON or None on any failure."""
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


# ── Alert formatters ──
def _fmt_tier1(description: str, source: str, port, state: str) -> str:
    e = _escape_md2
    return (
        f"\U0001f6a8 *FLEET ALERT \\- TIER 1*\n\n"
        f"{e(description)}\n\n"
        f"Source: {e(str(source))}\n"
        f"Port: {e(str(port))}\n"
        f"State: {e(state)}\n"
        f"Time: {e(_utc_stamp())}\n\n"
        f"_Action required now\\._"
    )


def _fmt_tier2(description: str, source: str, detail: str) -> str:
    e = _escape_md2
    return (
        f"\u26a0\ufe0f *Fleet Notice \\- Tier 2*\n\n"
        f"{e(description)}\n\n"
        f"Source: {e(str(source))}\n"
        f"Details: {e(detail)}\n"
        f"Time: {e(_utc_stamp())}"
    )


def _fmt_tier3(description: str) -> str:
    return f"\U0001f4cb Fleet Info: {_escape_md2(description)}"


def _fmt_startup(cc_reachable: bool, watchdog) -> str:
    e = _escape_md2
    if watchdog and "bots" in watchdog:
        bots = watchdog["bots"]
        online = sum(1 for b in bots.values() if b.get("alive"))
        dead = sum(1 for b in bots.values() if b.get("state") == "dead")
        total = len(bots)
        wd_line = f"{online}/{total} online, {dead} dead"
    else:
        wd_line = "unavailable"
    return (
        f"\u2705 *Fleet Notifier Online*\n\n"
        f"CC status: {e('reachable' if cc_reachable else 'UNREACHABLE')}\n"
        f"Watchdog: {e(wd_line)}\n"
        f"Time: {e(_utc_stamp())}"
    )


# ── Main loop ──
def main():
    if not _TG_TOKEN or not _TG_CHAT:
        log.critical("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set. Exiting.")
        sys.exit(1)

    cfg = _load_config()
    host = cfg["cc_host"]
    port = cfg["cc_port"]
    socket_interval = cfg["socket_check_interval_s"]
    http_interval = cfg["http_check_interval_s"]
    cc_down_threshold = cfg["cc_down_threshold"]
    tier3_enabled = cfg["tier3_enabled"]

    gate = AlertGate(
        dedup_window_s=cfg["dedup_window_s"],
        max_seen=cfg["seen_ids_max"],
        max_per_min=cfg["rate_limit_per_minute"],
    )

    # Startup probe
    cc_alive = _check_socket(host, port)
    watchdog = _http_get(host, port, "/api/watchdog") if cc_alive else None
    if _send_telegram(_fmt_startup(cc_alive, watchdog)):
        log.info("Startup message sent to Telegram")
    else:
        log.warning("Failed to send startup message — check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

    cc_fail_streak = 0 if cc_alive else 1
    cc_was_down = not cc_alive
    prev_bot_states: dict[str, str] = {}
    tick = 0
    tg_fail_streak = 0
    http_ticks = max(1, http_interval // socket_interval)

    log.info("Running. CC=%s:%s, socket every %ds, HTTP every %ds",
             host, port, socket_interval, http_interval)

    while True:
        try:
            tick += 1
            time.sleep(socket_interval)

            # ── Socket check every tick ──
            alive = _check_socket(host, port)
            if alive:
                if cc_was_down:
                    key = "CC_RECOVERED:notifier"
                    if gate.should_send(key):
                        msg = _fmt_tier2(
                            "Command Center is back online",
                            "notifier",
                            f"Was down for ~{cc_fail_streak * socket_interval}s"
                        )
                        if _send_telegram(msg):
                            gate.record_sent(key)
                            tg_fail_streak = 0
                        else:
                            tg_fail_streak += 1
                cc_fail_streak = 0
                cc_was_down = False
            else:
                cc_fail_streak += 1
                log.warning("CC socket check failed (%d consecutive)", cc_fail_streak)
                if cc_fail_streak == cc_down_threshold:
                    cc_was_down = True
                    key = "CC_DOWN:notifier"
                    if gate.should_send(key):
                        msg = _fmt_tier1(
                            f"Command Center UNREACHABLE for {cc_fail_streak * socket_interval}s",
                            "Command Center", port, "UNREACHABLE"
                        )
                        if _send_telegram(msg):
                            gate.record_sent(key)
                            tg_fail_streak = 0
                        else:
                            tg_fail_streak += 1

            # ── HTTP checks every http_ticks ──
            if tick % http_ticks != 0 or not alive:
                if tg_fail_streak >= 10:
                    log.critical("Telegram unreachable for %d consecutive attempts", tg_fail_streak)
                continue

            # Watchdog sweep
            watchdog = _http_get(host, port, "/api/watchdog")
            if watchdog and "bots" in watchdog:
                for bot_id, bstate in watchdog["bots"].items():
                    state = bstate.get("state", "unknown")
                    prev = prev_bot_states.get(bot_id, "unknown")
                    bot_port = bstate.get("port", "?")
                    cooldown = bstate.get("cooldown_remaining_s", 0)

                    if state == "dead" and cooldown and cooldown > 0:
                        # TIER 1: dead + restart attempted/failed
                        key = f"BOT_DEAD:{bot_id}"
                        if gate.should_send(key):
                            msg = _fmt_tier1(
                                f"{bot_id} is DOWN — auto-restart failed or on cooldown",
                                bot_id, bot_port, "dead (restart failed)"
                            )
                            if _send_telegram(msg):
                                gate.record_sent(key)
                                tg_fail_streak = 0
                            else:
                                tg_fail_streak += 1
                    elif state == "dead" and prev != "dead":
                        # TIER 2: first time entering dead state
                        key = f"BOT_DOWN:{bot_id}"
                        if gate.should_send(key):
                            msg = _fmt_tier2(
                                f"{bot_id} detected DOWN",
                                bot_id, f"port {bot_port}, watchdog attempting restart"
                            )
                            if _send_telegram(msg):
                                gate.record_sent(key)
                                tg_fail_streak = 0
                            else:
                                tg_fail_streak += 1

                    prev_bot_states[bot_id] = state

            # Event scan
            events = _http_get(host, port, "/api/events/recent?n=100")
            if not events or not isinstance(events, list):
                # Handle wrapped response
                if isinstance(events, dict):
                    events = events.get("events", [])
                else:
                    events = []

            regime_change_count = 0

            for evt in events:
                eid = str(evt.get("id", evt.get("timestamp", id(evt))))
                if gate.seen_event(eid):
                    continue

                etype = evt.get("type", "")
                source = evt.get("source", "unknown")
                data = evt.get("data", {}) or {}

                # TIER 1: EMERGENCY_REDUCE
                if etype == "EMERGENCY_REDUCE":
                    key = f"EMERGENCY_REDUCE:{source}"
                    if gate.should_send(key):
                        reason = data.get("reason", "cascade stop warning")
                        if _send_telegram(_fmt_tier1(
                            f"EMERGENCY REDUCE — {reason}", source, "-", "EMERGENCY"
                        )):
                            gate.record_sent(key)

                # TIER 1: CATASTROPHE_WARNING (ews_score > 0.6)
                elif etype == "CATASTROPHE_WARNING":
                    ews = data.get("ews_score", 0)
                    if ews > 0.6:
                        key = f"CATASTROPHE_WARNING:{source}"
                        if gate.should_send(key):
                            cat_type = data.get("catastrophe_type", "unknown")
                            if _send_telegram(_fmt_tier1(
                                f"Catastrophe warning: {cat_type} (EWS={ews:.2f})",
                                source, "-", "CATASTROPHE"
                            )):
                                gate.record_sent(key)

                # TIER 2: BOT_RESTARTED
                elif etype == "BOT_RESTARTED":
                    bot = data.get("bot", source)
                    key = f"BOT_RESTARTED:{bot}"
                    if gate.should_send(key):
                        if _send_telegram(_fmt_tier2(
                            f"{bot} auto-restarted successfully",
                            bot, f"port {data.get('port', '?')}"
                        )):
                            gate.record_sent(key)

                # TIER 2: BOOK_PHASE BOILING or PLASMA
                elif etype == "BOOK_PHASE":
                    phase = data.get("state", data.get("phase", ""))
                    if phase in ("BOILING", "PLASMA"):
                        key = f"BOOK_PHASE:{source}"
                        if gate.should_send(key):
                            pair = data.get("pair", "?")
                            if _send_telegram(_fmt_tier2(
                                f"Order book phase: {phase}",
                                source, f"pair={pair}, liquidity evaporating"
                            )):
                                gate.record_sent(key)

                # TIER 2: CHAOS_STATE attractor departure > 0.8
                elif etype == "CHAOS_STATE":
                    departure = data.get("attractor_departure", 0)
                    if departure > 0.8:
                        key = f"CHAOS_STATE:{source}"
                        if gate.should_send(key):
                            if _send_telegram(_fmt_tier2(
                                f"Attractor departure: {departure:.2f}",
                                source, "regime dissolving"
                            )):
                                gate.record_sent(key)

                # TIER 2: MANIFOLD_WARNING with regime_change_probability >= 1.0
                elif etype == "MANIFOLD_WARNING":
                    prob = data.get("regime_change_probability", 0)
                    if prob >= 1.0:
                        pair = data.get("pair", "?")
                        key = f"MANIFOLD_WARNING:{pair}"
                        if gate.should_send(key):
                            if _send_telegram(_fmt_tier2(
                                "Manifold deformation: regime change certain",
                                source, f"pair={pair}, prob={prob}"
                            )):
                                gate.record_sent(key)

                # TIER 3 events (only if enabled)
                if tier3_enabled:
                    if etype == "REGIME_CHANGE":
                        regime_change_count += 1

                    elif etype == "AEGIS_UPDATE" and data.get("regime") == "DEFENSIVE":
                        key = "AEGIS_DEFENSIVE:aegis"
                        if gate.should_send(key):
                            score = data.get("score", "?")
                            if _send_telegram(_fmt_tier3(
                                f"AEGIS dropped to DEFENSIVE (score={score})"
                            )):
                                gate.record_sent(key)

                    elif etype == "WHALE_ALERT" and data.get("magnitude") == "EXTREME":
                        key = f"WHALE_ALERT:{source}"
                        if gate.should_send(key):
                            if _send_telegram(_fmt_tier3(
                                f"EXTREME whale alert from {source}"
                            )):
                                gate.record_sent(key)

                    elif etype in ("TRADE_OPEN", "TRADE_CLOSE"):
                        pair = data.get("pair", "?")
                        key = f"TRADE:{source}:{pair}"
                        if gate.should_send(key):
                            direction = data.get("direction", "?")
                            pnl = data.get("pnl", "")
                            detail = f"{etype} {pair} {direction}"
                            if pnl:
                                detail += f" PnL=${pnl}"
                            if _send_telegram(_fmt_tier3(detail)):
                                gate.record_sent(key)

            # Regime storm (TIER 3)
            if tier3_enabled and regime_change_count >= 3:
                key = "REGIME_STORM:fleet"
                if gate.should_send(key):
                    if _send_telegram(_fmt_tier3(
                        f"Regime storm: {regime_change_count} changes in scan window"
                    )):
                        gate.record_sent(key)

            if tg_fail_streak >= 10:
                log.critical("Telegram unreachable for %d consecutive attempts", tg_fail_streak)

        except KeyboardInterrupt:
            log.info("Shutting down (KeyboardInterrupt)")
            break
        except Exception:
            log.exception("Main loop error — recovering in 10s")
            time.sleep(10)


if __name__ == "__main__":
    main()
