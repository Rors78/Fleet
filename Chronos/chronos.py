#!/usr/bin/env python3
"""
CHRONOS -- Temporal Market Intelligence Bot
=============================================
Port 8086. Color: #ffab00 (amber).

Intelligence-only bot. Does not trade. Publishes CHRONOS_ALERT events
to the fleet event bus so trading bots can act on temporal signals.

Tracks:
  1. Session boundaries (Asia, London, New York) and overlaps
  2. Hourly return profiles from BTC/USD 1h candles (~30 days)
  3. Funding settlement warnings (00, 08, 16 UTC -- 30 min ahead)
  4. Weekend effect (Friday evening warning)

Usage: python chronos.py
"""

import json
import logging
import math
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, urlencode
import urllib.request as urlreq

# Fleet integration
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "CommandCenter"))
try:
    from event_publisher import EventPublisher
except ImportError:
    EventPublisher = None
try:
    from kraken_ohlc import fetch_ohlc as _fetch_ohlc_canonical
except ImportError:
    _fetch_ohlc_canonical = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 8086
BOT_NAME = "Chronos"
ACCENT = "#ffab00"
CC_URL = "http://127.0.0.1:9000"
SCAN_INTERVAL = 120          # seconds between scans
CANDLE_PAIR = "BTC/USD"
CANDLE_INTERVAL = 60         # 1h candles
CANDLE_LIMIT = 720           # ~30 days of hourly data

# Session definitions (UTC hours, inclusive start, exclusive end)
SESSIONS = {
    "Asia":     (0, 8),
    "London":   (7, 16),
    "New York": (13, 22),
}

# Overlap windows (UTC hours)
OVERLAPS = {
    "London-NY":    (13, 16),
    "Asia-London":  (7, 8),
}

# Funding settlement times (UTC hour)
FUNDING_TIMES = [0, 8, 16]
FUNDING_WARN_MINUTES = 30

# Bias threshold -- if an hour has >65% bullish or >65% bearish, it's anomalous
BIAS_THRESHOLD = 0.65

# Weekend warning: Friday after this UTC hour
WEEKEND_WARN_HOUR = 18

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE = Path(__file__).resolve().parent / "chronos.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("chronos")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_scan_count = 0
_status = "initializing"
_recent_alerts = deque(maxlen=50)  # all alerts ever emitted (kept for snapshot)
_hourly_profile = {}               # {hour_int: {"bull": int, "bear": int, "flat": int, "total": int}}
_last_candle_fetch = 0.0
_regime = "unknown"

# Dedup: track which session/overlap/funding events we already fired this cycle
_fired_this_cycle = set()

# Event publisher
_event_pub = EventPublisher(CC_URL, "chronos") if EventPublisher else None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc():
    return datetime.now(timezone.utc)


def _fetch_candles():
    """Fetch 1h BTC/USD candles — canonical shared fetcher with Kraken fallback."""
    if _fetch_ohlc_canonical:
        candles = _fetch_ohlc_canonical(CANDLE_PAIR, CANDLE_INTERVAL, CANDLE_LIMIT, cc_url=CC_URL)
    else:
        # Fallback: direct CC only (original behaviour)
        try:
            params = urlencode({
                "pair": CANDLE_PAIR,
                "interval": CANDLE_INTERVAL,
                "limit": CANDLE_LIMIT,
            })
            url = f"{CC_URL}/api/market/ohlc?{params}"
            req = urlreq.Request(url, headers={"Accept": "application/json"})
            with urlreq.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            candles = data.get("candles", [])
        except Exception as e:
            log.warning(f"Candle fetch failed: {e}")
            candles = []
    if candles:
        log.info(f"Fetched {len(candles)} candles for {CANDLE_PAIR}")
        if len(candles) < 100:
            log.warning(f"Low candle count ({len(candles)}/{CANDLE_LIMIT}) — hourly profile may be unreliable")
    return candles


def _build_hourly_profile(candles):
    """
    Build hourly return profile from candles.
    Each candle: [ts, open, high, low, close, volume, count]
    Returns dict {hour: {"bull": n, "bear": n, "flat": n, "total": n}}
    """
    profile = {}
    for i in range(24):
        profile[i] = {"bull": 0, "bear": 0, "flat": 0, "total": 0}

    for candle in candles:
        ts, o, h, l, c = candle[0], candle[1], candle[2], candle[3], candle[4]
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        hour = dt.hour
        profile[hour]["total"] += 1
        if c > o:
            profile[hour]["bull"] += 1
        elif c < o:
            profile[hour]["bear"] += 1
        else:
            profile[hour]["flat"] += 1

    return profile


def _active_sessions(hour):
    """Return list of session names active at given UTC hour."""
    active = []
    for name, (start, end) in SESSIONS.items():
        if start <= hour < end:
            active.append(name)
    return active


def _active_overlaps(hour):
    """Return list of overlap names active at given UTC hour."""
    active = []
    for name, (start, end) in OVERLAPS.items():
        if start <= hour < end:
            active.append(name)
    return active


def _emit(event_type, data):
    """Emit a CHRONOS_ALERT and store it locally."""
    alert = {
        "type": event_type,
        "data": data,
        "ts": time.time(),
        "utc": _now_utc().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        _recent_alerts.append(alert)
    if _event_pub:
        _event_pub.emit("CHRONOS_ALERT", {"type": event_type, "alert_type": event_type, **data})
    log.info(f"ALERT {event_type}: {json.dumps(data, default=str)}")


def _derive_regime(profile, hour):
    """
    Derive a simple temporal regime from the hourly profile.
    - "high_activity" if current hour has high sample count (top quartile)
    - "session_overlap" if we're in an overlap window
    - "low_activity" if bottom quartile
    - "normal" otherwise
    """
    overlaps = _active_overlaps(hour)
    if overlaps:
        return "session_overlap"

    totals = [v["total"] for v in profile.values() if v["total"] > 0]
    if not totals:
        return "unknown"

    sorted_totals = sorted(totals)
    q75 = sorted_totals[int(len(sorted_totals) * 0.75)] if len(sorted_totals) > 3 else max(totals)
    q25 = sorted_totals[int(len(sorted_totals) * 0.25)] if len(sorted_totals) > 3 else min(totals)

    current_total = profile.get(hour, {}).get("total", 0)
    if current_total >= q75:
        return "high_activity"
    elif current_total <= q25:
        return "low_activity"
    return "normal"


# ---------------------------------------------------------------------------
# Scan Logic
# ---------------------------------------------------------------------------

def _scan():
    """One full temporal scan cycle."""
    global _scan_count, _status, _hourly_profile, _last_candle_fetch, _regime, _fired_this_cycle

    now = _now_utc()
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()  # 0=Monday ... 6=Sunday

    # -- Fetch candles periodically (every 10 min or on first run) --
    if time.time() - _last_candle_fetch > 600 or _last_candle_fetch == 0:
        candles = _fetch_candles()
        if candles:
            profile = _build_hourly_profile(candles)
            with _lock:
                _hourly_profile = profile
            _last_candle_fetch = time.time()

    with _lock:
        profile = dict(_hourly_profile)

    # -- Reset fired set at the top of each new hour --
    cycle_key_hour = f"h{hour}"
    if not hasattr(_scan, "_last_hour"):
        _scan._last_hour = -1
    if hour != _scan._last_hour:
        _fired_this_cycle.clear()
        _scan._last_hour = hour

    # -- 1. Session Open events (within first 5 minutes of session start) --
    for name, (start, end) in SESSIONS.items():
        if hour == start and minute < 5:
            key = f"{cycle_key_hour}:session:{name}"
            if key not in _fired_this_cycle:
                _fired_this_cycle.add(key)
                _emit("SESSION_OPEN", {
                    "session": name,
                    "start_utc": start,
                    "end_utc": end,
                    "note": f"{name} session opens",
                })

    # -- 2. Overlap events (within first 5 minutes of overlap start) --
    for name, (start, _end) in OVERLAPS.items():
        if hour == start and minute < 5:
            key = f"{cycle_key_hour}:overlap:{name}"
            if key not in _fired_this_cycle:
                _fired_this_cycle.add(key)
                _emit("SESSION_OVERLAP", {
                    "sessions": _active_sessions(hour),
                    "window": name,
                    "volatility_note": "elevated liquidity/volatility expected",
                })

    # -- 3. Time anomaly (hourly bias) --
    # Statistically honest test (replaces the old bull_pct/bear_pct > 0.65
    # with n>=5 threshold, which was noise-as-signal — at n=5, 4/5 bullish
    # candles is well within chance). Fire only when n>=30 AND the observed
    # proportion's 95% CI (normal approximation) excludes 0.5.
    if profile:
        hp = profile.get(hour, {})
        total = hp.get("total", 0)
        if total >= 30:
            bull_pct = hp["bull"] / total
            bear_pct = hp["bear"] / total
            direction = "bullish" if bull_pct >= bear_pct else "bearish"
            p = bull_pct if direction == "bullish" else bear_pct
            margin = 1.96 * math.sqrt(p * (1 - p) / total)
            ci_low = p - margin
            ci_high = p + margin
            key = f"{cycle_key_hour}:anomaly"
            if key not in _fired_this_cycle and ci_low > 0.5:
                _fired_this_cycle.add(key)
                _emit("TIME_ANOMALY", {
                    "hour_utc": hour,
                    "bias_pct": round(p * 100, 1),
                    "n": total,
                    "ci_low": round(ci_low, 4),
                    "ci_high": round(ci_high, 4),
                    "direction": direction,
                    "pair": CANDLE_PAIR,
                })

    # -- 4. Funding settlement warnings (30 min before) --
    for ft in FUNDING_TIMES:
        # Warning fires when we are 30 min before the funding hour
        warn_hour = (ft - 1) % 24
        warn_min_start = 30
        if hour == warn_hour and warn_min_start <= minute < warn_min_start + 5:
            key = f"{cycle_key_hour}:funding:{ft}"
            if key not in _fired_this_cycle:
                _fired_this_cycle.add(key)
                settle_time = f"{ft:02d}:00 UTC"
                mins_until = 60 - minute
                _emit("FUNDING_SETTLEMENT", {
                    "settlement_time": settle_time,
                    "minutes_until": mins_until,
                    "warning": f"Funding settles at {settle_time} (~{mins_until} min)",
                })

    # -- 5. Weekend approaching (Friday evening) --
    if weekday == 4 and hour >= WEEKEND_WARN_HOUR:
        key = f"weekend:{now.strftime('%Y-%m-%d')}"
        if key not in _fired_this_cycle:
            _fired_this_cycle.add(key)
            _emit("WEEKEND_APPROACHING", {
                "day": "Friday",
                "hour_utc": hour,
                "warning": "Weekend approaching -- reduced liquidity expected",
            })

    # -- Update regime --
    regime = _derive_regime(profile, hour) if profile else "unknown"
    with _lock:
        _regime = regime

    with _lock:
        _scan_count += 1
        _status = "scanning"


def _scan_loop():
    """Background scan loop."""
    global _status
    # Small delay to let HTTP server start
    time.sleep(2)
    log.info("Scan loop started")
    while True:
        try:
            _scan()
        except Exception as e:
            log.error(f"Scan error: {e}", exc_info=True)
            with _lock:
                _status = "error"
        time.sleep(SCAN_INTERVAL)


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

def _build_snapshot():
    """Build the /api/snapshot response."""
    now = _now_utc()
    hour = now.hour

    with _lock:
        profile = dict(_hourly_profile)
        scan_count = _scan_count
        status = _status
        alerts = list(_recent_alerts)
        regime = _regime

    # Current hour bias
    hp = profile.get(hour, {})
    total = hp.get("total", 0)
    bull_pct = round((hp.get("bull", 0) / total) * 100, 1) if total > 0 else 0.0

    # Build the 24-entry hourly profile for the response
    hourly_out = {}
    for h in range(24):
        entry = profile.get(h, {"bull": 0, "bear": 0, "flat": 0, "total": 0})
        t = entry["total"]
        hourly_out[str(h)] = {
            "bull_pct": round((entry["bull"] / t) * 100, 1) if t > 0 else 0.0,
            "bear_pct": round((entry["bear"] / t) * 100, 1) if t > 0 else 0.0,
            "samples": t,
        }

    # Last 10 alerts for the snapshot
    recent_10 = alerts[-10:] if len(alerts) > 10 else list(alerts)

    return {
        "bot_name": BOT_NAME,
        "status": status,
        "accent": ACCENT,
        "port": PORT,
        "scan_count": scan_count,
        "scan_interval": SCAN_INTERVAL,
        "timestamp": time.time(),
        "utc": now.strftime("%Y-%m-%d %H:%M:%S"),
        "current_hour_utc": hour,
        "active_sessions": _active_sessions(hour),
        "active_overlaps": _active_overlaps(hour),
        "hour_bias": bull_pct,
        "hour_sample_size": total,
        "recent_alerts": recent_10,
        "hourly_profile": hourly_out,
        "regime": regime,
        "candle_pair": CANDLE_PAIR,
        "candle_count": sum(v.get("total", 0) for v in profile.values()),
        # Fleet-standard fields
        "pnl": 0.0,
        "positions": [],
        "win_rate": 0.0,
        "signals": [
            {
                "source": "chronos",
                "type": a["type"],
                "data": a.get("data", {}),
                "ts": a["ts"],
            }
            for a in recent_10
        ],
    }


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class ChronosHandler(BaseHTTPRequestHandler):
    """Handles /health and /api/snapshot."""

    def log_message(self, fmt, *args):
        pass  # suppress default stderr logging

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/health":
            self._respond_json(200, {
                "status": "ok",
                "bot": BOT_NAME,
                "port": PORT,
                "scan_count": _scan_count,
                "timestamp": time.time(),
            })

        elif path == "/api/snapshot":
            snapshot = _build_snapshot()
            self._respond_json(200, snapshot)

        else:
            self._respond_json(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _respond_json(self, code, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        ensure_port(PORT, "chronos")
        write_pidfile("chronos", PORT)
        atexit.register(cleanup_pidfile, "chronos")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    print()
    print(f"  CHRONOS v1.0 -- Temporal Market Intelligence")
    print(f"  Port:     {PORT}")
    print(f"  Accent:   {ACCENT}")
    print(f"  Interval: {SCAN_INTERVAL}s")
    print(f"  Pair:     {CANDLE_PAIR} ({CANDLE_LIMIT} candles)")
    print(f"  Sessions: {', '.join(SESSIONS.keys())}")
    print(f"  Overlaps: {', '.join(OVERLAPS.keys())}")
    print(f"  Funding:  {', '.join(f'{h:02d}:00' for h in FUNDING_TIMES)} UTC")
    print(f"  Log:      {LOG_FILE}")
    print(f"  API:      http://localhost:{PORT}/api/snapshot")
    print(f"  Health:   http://localhost:{PORT}/health")
    print(f"  Press Ctrl+C to stop")
    print()

    # Start scan thread
    scanner = threading.Thread(target=_scan_loop, daemon=True, name="ChronosScan")
    scanner.start()
    log.info("Scanner thread started")

    # Start HTTP server
    server = ThreadedServer(("0.0.0.0", PORT), ChronosHandler)
    log.info(f"HTTP server listening on port {PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Chronos stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
