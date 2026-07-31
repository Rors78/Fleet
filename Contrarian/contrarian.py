#!/usr/bin/env python3
"""
CONTRARIAN -- Sentiment Extreme Detection Intelligence Bot
===========================================================
Bot #14. Port 8084. Color: #ff1744 (red).

NOT a trader. Scans fleet data every 120 seconds and publishes
SENTIMENT_EXTREME events to the fleet event bus when conditions
indicate crowd positioning has reached dangerous extremes.

Data sources (all via Command Center on port 9000):
  1. Fear & Greed       -- Oracle bot's fear_greed score
  2. Funding rates      -- Brainiac collector's Kraken Futures data
  3. Correlations       -- Brainiac collector's pair correlation matrix
  4. Liquidation cascade -- OHLC 5-min candles for multi-pair crash detection
  5. Universe pairs     -- Command Center's active pair list

Signal types published:
  EXTREME_GREED         F&G > 80 AND avg funding > 0.05% AND correlation > 0.8
  EXTREME_FEAR          F&G < 15 AND correlation > 0.8
  FUNDING_EXTREME       Any pair funding > 0.1% or < -0.1%
  LIQUIDATION_CASCADE   3+ pairs drop > 3% in last 15 minutes
  CORRELATION_BREAKDOWN Pair correlation drops from > 0.8 to < 0.5

Usage: python contrarian.py
"""

import json
import logging
import os
import sys
from pathlib import Path
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

import requests

# Fleet integration -- EventPublisher lives in CommandCenter
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "CommandCenter"))
try:
    from event_publisher import EventPublisher
except ImportError:
    EventPublisher = None
try:
    from bus_listener import BusListener
except ImportError:
    BusListener = None

try:
    from fleet_config import is_blacklisted as _is_blacklisted
except ImportError:
    _is_blacklisted = lambda pair: False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 8084
CC_URL = "http://127.0.0.1:9000"
SCAN_INTERVAL = 120  # seconds between scans
ACCENT = "#ff1744"
BOT_NAME = "Contrarian"

# Thresholds
EXTREME_GREED_FG = 80        # Fear & Greed above this
EXTREME_FEAR_FG = 15         # Fear & Greed below this
FUNDING_EXTREME_PCT = 0.005  # 0.5% per-period — truly extreme leveraged positioning
FUNDING_AVG_GREED = 0.0005   # 0.05% avg funding for greed signal
CORRELATION_HIGH = 0.8       # high correlation threshold
CORRELATION_LOW = 0.5        # breakdown target
CASCADE_DROP_PCT = 3.0       # percent drop per pair
CASCADE_MIN_PAIRS = 3        # minimum pairs for cascade signal
MAX_ALERTS = 100             # ring buffer for alert history

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "contrarian.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("contrarian")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch_json(url, timeout=5):
    """Fetch JSON from a URL. Returns parsed dict or None on any error."""
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Data Fetchers
# ---------------------------------------------------------------------------

def fetch_fear_greed():
    """Get Oracle's Fear & Greed index via Command Center passthrough.

    Command Center serves /api/bot/oracle which returns Oracle's raw snapshot.
    The fear_greed field is a 0-100 integer.
    """
    data = fetch_json(f"{CC_URL}/api/bot/oracle")
    if data and isinstance(data, dict):
        # CC /api/bot/<id> returns the raw snapshot from the bot
        raw = data.get("raw", data)
        fg = raw.get("fear_greed")
        if fg is not None:
            return fg
    return None


def fetch_funding_rates():
    """Get Brainiac's funding rate data.

    Returns dict: {
        "avg_rate": float,
        "rates": {"BTC": {"rate": float, ...}, ...},
        "pairs": int,
        ...
    } or None.
    """
    data = fetch_json(f"{CC_URL}/api/brainiac/funding")
    if data and isinstance(data, dict):
        return data.get("data", data)
    return None


def fetch_correlations():
    """Get Brainiac's correlation matrix data.

    Returns dict: {
        "avg_abs_correlation": float,
        "top_correlated": [...],
        "least_correlated": [...],
        "pairs_count": int,
        ...
    } or None.
    """
    data = fetch_json(f"{CC_URL}/api/brainiac/correlations")
    if data and isinstance(data, dict):
        return data.get("data", data)
    return None


def fetch_universe():
    """Get the current tradeable pair list from Command Center.

    Returns list of pair strings like ["BTC/USD", "ETH/USD", ...].
    """
    data = fetch_json(f"{CC_URL}/api/universe")
    if data and isinstance(data, dict):
        pairs = data.get("pairs", [])
        return [p["display"] if isinstance(p, dict) else p for p in pairs]
    return ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD"]


def fetch_ohlc(pair, interval=5, limit=4):
    """Fetch recent OHLC candles for a pair from Command Center.

    Returns list of candle arrays: [[time, open, high, low, close, vwap, count], ...]
    or None.
    """
    data = fetch_json(f"{CC_URL}/api/market/ohlc?pair={pair}&interval={interval}&limit={limit}")
    if data and isinstance(data, dict):
        return data.get("candles")
    return None


# ---------------------------------------------------------------------------
# Detection Functions
# ---------------------------------------------------------------------------

def detect_extreme_greed(fear_greed, funding_data, corr_data):
    """Check for EXTREME_GREED: F&G > 80 AND avg funding > 0.05% AND correlation > 0.8."""
    if fear_greed is None or fear_greed <= EXTREME_GREED_FG:
        return None

    avg_funding = 0.0
    if funding_data:
        avg_funding = abs(funding_data.get("avg_rate", 0))

    avg_corr = 0.0
    if corr_data:
        avg_corr = corr_data.get("avg_abs_correlation", 0)

    if avg_funding > FUNDING_AVG_GREED and avg_corr > CORRELATION_HIGH:
        return {
            "type": "EXTREME_GREED",
            "fear_greed": fear_greed,
            "avg_funding": round(avg_funding * 100, 4),  # as percentage
            "avg_correlation": round(avg_corr, 4),
            "message": (f"Extreme greed detected: F&G={fear_greed}, "
                        f"avg funding={avg_funding * 100:.4f}%, "
                        f"correlation={avg_corr:.4f}"),
        }
    return None


def detect_extreme_fear(fear_greed, corr_data):
    """Check for EXTREME_FEAR: F&G < 15 AND correlation > 0.8."""
    if fear_greed is None or fear_greed >= EXTREME_FEAR_FG:
        return None

    avg_corr = 0.0
    if corr_data:
        avg_corr = corr_data.get("avg_abs_correlation", 0)

    if avg_corr > CORRELATION_HIGH:
        return {
            "type": "EXTREME_FEAR",
            "fear_greed": fear_greed,
            "avg_correlation": round(avg_corr, 4),
            "message": (f"Extreme fear detected: F&G={fear_greed}, "
                        f"correlation={avg_corr:.4f} (panic selling in lockstep)"),
        }
    return None


def detect_funding_extremes(funding_data):
    """Check for FUNDING_EXTREME: any pair funding > 0.1% or < -0.1%.

    Returns list of alerts (one per extreme pair).
    """
    alerts = []
    if not funding_data:
        return alerts

    # Non-crypto symbols from Kraken Futures (equity/commodity index futures)
    _NON_CRYPTO = {"SPYX", "AAPLX", "NVDAX", "GOOGLX", "GLDX", "QQQX", "TSLAX",
                   "AMZNX", "MSFTX", "METAX", "COINX", "MSTRX", "XAUX"}
    rates = funding_data.get("rates", {})
    for symbol, info in rates.items():
        if symbol in _NON_CRYPTO or symbol.endswith("X"):
            continue  # skip equity/commodity futures
        rate = info.get("rate", 0)
        if abs(rate) > FUNDING_EXTREME_PCT:
            direction = "LONG" if rate > 0 else "SHORT"
            alerts.append({
                "type": "FUNDING_EXTREME",
                "pair": f"{symbol}/USD",
                "funding_rate": round(rate * 100, 4),  # as percentage
                "direction": direction,
                "mark_price": info.get("mark_price"),
                "open_interest": info.get("open_interest"),
                "message": (f"Extreme {direction.lower()} funding on {symbol}: "
                            f"{rate * 100:.4f}% "
                            f"({'longs pay shorts' if rate > 0 else 'shorts pay longs'})"),
            })
    return alerts


def detect_liquidation_cascade(pairs):
    """Check for LIQUIDATION_CASCADE: 3+ pairs drop > 3% in 15 minutes.

    Fetches 5-min candles (last 4 bars = 20 minutes window) for each pair.
    A drop is measured from the highest high to the last close within the window.
    """
    dropping_pairs = []

    for pair in pairs[:15]:  # check top 15 pairs to limit API calls
        candles = fetch_ohlc(pair, interval=5, limit=4)
        if not candles or len(candles) < 3:
            continue

        try:
            # Find highest high and latest close in the window
            highest_high = max(float(c[2]) if isinstance(c[2], (int, float, str)) else c[2]
                               for c in candles)
            latest_close = float(candles[-1][4]) if not isinstance(candles[-1][4], float) else candles[-1][4]

            if highest_high <= 0:
                continue

            drop_pct = ((highest_high - latest_close) / highest_high) * 100

            if drop_pct > CASCADE_DROP_PCT:
                dropping_pairs.append({
                    "pair": pair,
                    "drop_pct": round(drop_pct, 2),
                    "high": round(highest_high, 4),
                    "close": round(latest_close, 4),
                })
        except (ValueError, TypeError, IndexError):
            continue

        time.sleep(0.3)  # rate limit between OHLC calls

    if len(dropping_pairs) >= CASCADE_MIN_PAIRS:
        return {
            "type": "LIQUIDATION_CASCADE",
            "pairs_affected": len(dropping_pairs),
            "details": dropping_pairs,
            "message": (f"Liquidation cascade: {len(dropping_pairs)} pairs "
                        f"dropping >3% in 15min — "
                        f"{', '.join(d['pair'] + ' -' + str(d['drop_pct']) + '%' for d in dropping_pairs[:5])}"),
        }
    return None


def detect_correlation_breakdown(corr_data, prev_corr_data):
    """Check for CORRELATION_BREAKDOWN: any pair drops from > 0.8 to < 0.5 correlation.

    Compares the current correlation snapshot against the previous one.
    Returns list of breakdown alerts.
    """
    alerts = []
    if not corr_data or not prev_corr_data:
        return alerts

    # Build lookup of previous correlations
    prev_top = {}
    for item in prev_corr_data.get("top_correlated", []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            prev_top[item[0]] = item[1]

    # Also check current correlations for breakdown candidates
    # We need the full set: both top_correlated and least_correlated contain pair|pair keys
    curr_all = {}
    for item in corr_data.get("top_correlated", []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            curr_all[item[0]] = item[1]
    for item in corr_data.get("least_correlated", []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            curr_all[item[0]] = item[1]

    # Check for breakdowns: was > 0.8, now < 0.5
    for pair_key, prev_val in prev_top.items():
        if prev_val > CORRELATION_HIGH:
            curr_val = curr_all.get(pair_key)
            if curr_val is not None and curr_val < CORRELATION_LOW:
                alerts.append({
                    "type": "CORRELATION_BREAKDOWN",
                    "pair_key": pair_key,
                    "previous_correlation": round(prev_val, 4),
                    "current_correlation": round(curr_val, 4),
                    "message": (f"Correlation breakdown: {pair_key} dropped "
                                f"from {prev_val:.4f} to {curr_val:.4f} "
                                f"(regime divergence)"),
                })

    return alerts


# ---------------------------------------------------------------------------
# Composite Sentiment State
# ---------------------------------------------------------------------------

def compute_sentiment_state(fear_greed):
    """Map fear_greed score (0-100) to a named sentiment state.

    Returns (state_name, score).
    """
    if fear_greed is None:
        return "NEUTRAL", 50

    score = int(fear_greed)
    score = max(0, min(100, score))

    if score > 80:
        state = "EXTREME_GREED"
    elif score > 60:
        state = "GREED"
    elif score >= 40:
        state = "NEUTRAL"
    elif score >= 15:
        state = "FEAR"
    else:
        state = "EXTREME_FEAR"

    return state, score


# ---------------------------------------------------------------------------
# Contrarian Engine
# ---------------------------------------------------------------------------

class ContrarianEngine:
    """Core scan engine. Runs in a daemon thread, publishes events."""

    def __init__(self):
        self._lock = threading.Lock()
        self.scan_count = 0
        self.status = "initializing"
        self.sentiment_state = "NEUTRAL"
        self.sentiment_score = 50
        self.fear_greed = None
        self.regime = "UNKNOWN"
        self.alerts = deque(maxlen=MAX_ALERTS)
        self.last_scan_time = 0
        self.scan_duration = 0.0
        self.last_error = None

        # Previous correlation snapshot for breakdown detection
        self._prev_corr_data = None

        # Temporal context (Chronos) — intel-only, never a trade decision.
        self.temporal_context = None

        # Event publisher
        self._event_pub = (EventPublisher(CC_URL, "contrarian")
                           if EventPublisher else None)

        # Bus listener — reads Chronos temporal bias for sentiment context
        self._bus = BusListener(CC_URL) if BusListener else None

        # Track last published alerts to avoid spamming identical signals
        self._last_published = {}  # (type, pair) -> timestamp of last publish
        self._cooldown = 300  # 5-minute cooldown per (signal type, pair)

    def _publish(self, signal_type, data):
        """Publish a SENTIMENT_EXTREME event to the fleet bus.

        Applies a cooldown per (signal_type, pair) to avoid spam while
        allowing different pairs to publish independently.
        """
        now = time.time()
        pair = data.get("pair", "")
        cooldown_key = (signal_type, pair)
        last = self._last_published.get(cooldown_key, 0)
        if now - last < self._cooldown:
            log.debug(f"Cooldown active for {signal_type}/{pair}, skipping publish")
            return

        self._last_published[cooldown_key] = now

        event_data = {
            "signal": signal_type,
            "sentiment_state": self.sentiment_state,
            "sentiment_score": self.sentiment_score,
            **data,
        }

        if self._event_pub:
            self._event_pub.emit("SENTIMENT_EXTREME", event_data)
            log.info(f"Published SENTIMENT_EXTREME/{signal_type}: {data.get('message', '')}")
        else:
            log.warning(f"EventPublisher unavailable, cannot publish {signal_type}")

    def _add_alert(self, alert):
        """Add an alert to the ring buffer with timestamp and blacklist flag."""
        alert["ts"] = time.time()
        alert["iso"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Blacklist flagging — check pair fields against fleet blacklist
        pair = alert.get("pair")
        if pair:
            alert["blacklisted"] = _is_blacklisted(pair)
        elif alert.get("pair_key"):
            # Correlation breakdown: "BTC/USD|ETH/USD" — flag if either half is blacklisted
            parts = alert["pair_key"].split("|")
            alert["blacklisted"] = any(_is_blacklisted(p) for p in parts)
        elif alert.get("details"):
            # Liquidation cascade: flag each detail entry, plus overall if any hit
            for detail in alert["details"]:
                dp = detail.get("pair", "")
                detail["blacklisted"] = _is_blacklisted(dp)
            alert["blacklisted"] = any(d.get("blacklisted") for d in alert["details"])
        else:
            alert["blacklisted"] = False

        with self._lock:
            self.alerts.appendleft(alert)

    def scan(self):
        """Run one full scan cycle. Called from the daemon thread."""
        self.scan_count += 1
        t0 = time.time()
        log.info(f"--- Scan #{self.scan_count} ---")

        with self._lock:
            self.status = "scanning"

        try:
            # ----------------------------------------------------------
            # 1. Fetch all data sources
            # ----------------------------------------------------------
            fear_greed = fetch_fear_greed()
            funding_data = fetch_funding_rates()
            corr_data = fetch_correlations()
            pairs = fetch_universe()

            # Update composite sentiment
            state, score = compute_sentiment_state(fear_greed)
            with self._lock:
                self.fear_greed = fear_greed
                self.sentiment_state = state
                self.sentiment_score = score

            log.info(f"F&G={fear_greed}, state={state}, score={score}, "
                     f"pairs={len(pairs)}")

            # CHRONOS: temporal bias — added context signal only. Contrarian
            # is intel-only and never trades, so this is never a
            # position-sizing decision — it is folded into the sentiment
            # summary/state this bot already produces and logged so wiring
            # is provable from the log post-restart.
            if self._bus:
                try:
                    temporal = self._bus.chronos_temporal(max_age=3600)
                    anomaly = temporal.get("time_anomaly") if temporal else None
                    with self._lock:
                        self.temporal_context = temporal
                    if anomaly:
                        log.info(
                            f"TEMPORAL CONTEXT: TIME_ANOMALY {anomaly.get('direction')} "
                            f"(n={anomaly.get('n')}, bias={anomaly.get('bias_pct')}%, "
                            f"pair={anomaly.get('pair')}) — incorporated into sentiment context, "
                            f"not a trade decision"
                        )
                    for se in (temporal.get("session_events") or []) if temporal else []:
                        if se.get("type") == "SESSION_OVERLAP" and (time.time() - se.get("ts", 0)) < 900:
                            log.info(f"TEMPORAL CONTEXT: SESSION_OVERLAP {se.get('window')} — high volatility window")
                            break
                except Exception as e:
                    log.debug(f"Temporal bias check failed: {e}")

            # Log data availability
            sources = []
            if fear_greed is not None:
                sources.append("oracle")
            if funding_data:
                sources.append("funding")
            if corr_data:
                sources.append("correlations")
            log.info(f"Data sources active: {', '.join(sources) or 'none'}")

            # ----------------------------------------------------------
            # 2. Run all detectors
            # ----------------------------------------------------------
            signals_found = 0

            # Extreme greed
            greed = detect_extreme_greed(fear_greed, funding_data, corr_data)
            if greed:
                self._add_alert(greed)
                self._publish("EXTREME_GREED", greed)
                signals_found += 1
                log.warning(f"SIGNAL: {greed['message']}")

            # Extreme fear
            fear = detect_extreme_fear(fear_greed, corr_data)
            if fear:
                self._add_alert(fear)
                self._publish("EXTREME_FEAR", fear)
                signals_found += 1
                log.warning(f"SIGNAL: {fear['message']}")

            # Funding extremes (can produce multiple alerts)
            funding_alerts = detect_funding_extremes(funding_data)
            for fa in funding_alerts:
                self._add_alert(fa)
                self._publish("FUNDING_EXTREME", fa)
                signals_found += 1
                log.warning(f"SIGNAL: {fa['message']}")

            # Liquidation cascade
            cascade = detect_liquidation_cascade(pairs)
            if cascade:
                self._add_alert(cascade)
                self._publish("LIQUIDATION_CASCADE", cascade)
                signals_found += 1
                log.warning(f"SIGNAL: {cascade['message']}")

            # Correlation breakdown
            breakdowns = detect_correlation_breakdown(corr_data, self._prev_corr_data)
            for bd in breakdowns:
                self._add_alert(bd)
                self._publish("CORRELATION_BREAKDOWN", bd)
                signals_found += 1
                log.warning(f"SIGNAL: {bd['message']}")

            # Store current correlation data for next cycle's breakdown check
            self._prev_corr_data = corr_data

            # ----------------------------------------------------------
            # 3. Determine regime from fleet state
            # ----------------------------------------------------------
            master = fetch_json(f"{CC_URL}/api/master")
            if master and isinstance(master, dict):
                agg = master.get("aggregate", {})
                self.regime = agg.get("regime", "UNKNOWN")

            # ----------------------------------------------------------
            # Done
            # ----------------------------------------------------------
            elapsed = time.time() - t0
            with self._lock:
                self.status = "idle"
                self.last_scan_time = time.time()
                self.scan_duration = elapsed
                self.last_error = None

            log.info(f"Scan #{self.scan_count} complete: {signals_found} signal(s) "
                     f"in {elapsed:.1f}s")

        except Exception as e:
            elapsed = time.time() - t0
            with self._lock:
                self.status = "error"
                self.last_error = str(e)
                self.scan_duration = elapsed
            log.error(f"Scan #{self.scan_count} failed after {elapsed:.1f}s: {e}",
                      exc_info=True)

    def snapshot(self):
        """Build the /api/snapshot response."""
        with self._lock:
            recent = list(self.alerts)[:10]
            return {
                "timestamp": time.time(),
                "bot_name": BOT_NAME,
                "status": self.status,
                "port": PORT,
                "accent": ACCENT,
                "scan_count": self.scan_count,
                "sentiment_state": self.sentiment_state,
                "sentiment_score": self.sentiment_score,
                "fear_greed": self.fear_greed,
                "temporal_context": self.temporal_context,
                "regime": self.regime,
                "recent_alerts": recent,
                "total_alerts": len(self.alerts),
                "last_scan_time": self.last_scan_time,
                "scan_duration_s": round(self.scan_duration, 2),
                "last_error": self.last_error,
            }


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

_engine = ContrarianEngine()


class ContrarianHandler(BaseHTTPRequestHandler):
    """Stdlib HTTP handler for /health and /api/snapshot."""

    def log_message(self, fmt, *args):
        pass  # suppress default access log; we use our own logger

    def _send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/api/snapshot":
            self._send_json(_engine.snapshot())

        elif path == "/health":
            self._send_json({
                "status": "ok",
                "bot": BOT_NAME,
                "port": PORT,
                "scan_count": _engine.scan_count,
                "sentiment_state": _engine.sentiment_state,
                "timestamp": time.time(),
            })

        else:
            self.send_error(404, "Not Found")


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# Scan Loop (daemon thread)
# ---------------------------------------------------------------------------

def _scan_loop():
    """Background thread: wait for fleet boot, then scan on interval."""
    log.info(f"Scan thread starting, waiting 20s for fleet boot...")
    time.sleep(20)  # let fleet services come up

    while True:
        try:
            _engine.scan()
        except Exception as e:
            log.error(f"Scan loop exception: {e}", exc_info=True)
        time.sleep(SCAN_INTERVAL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        ensure_port(PORT, "contrarian")
        write_pidfile("contrarian", PORT)
        atexit.register(cleanup_pidfile, "contrarian")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    print()
    print(f"  CONTRARIAN v1.0 -- Sentiment Extreme Detection")
    print(f"  ================================================")
    print(f"  Port:       {PORT}")
    print(f"  Accent:     {ACCENT}")
    print(f"  Interval:   {SCAN_INTERVAL}s")
    print(f"  Log:        {LOG_FILE}")
    print(f"  Fleet bus:  {CC_URL}")
    print(f"  Publisher:  {'active' if EventPublisher else 'UNAVAILABLE'}")
    print(f"  Endpoints:  http://localhost:{PORT}/health")
    print(f"              http://localhost:{PORT}/api/snapshot")
    print(f"  Press Ctrl+C to stop")
    print()

    # Start scan daemon thread
    scan_thread = threading.Thread(target=_scan_loop, daemon=True,
                                   name="ContrarianScan")
    scan_thread.start()
    log.info("Scan daemon thread started")

    # Run HTTP server on main thread
    server = ThreadedServer(("0.0.0.0", PORT), ContrarianHandler)
    log.info(f"HTTP server listening on 0.0.0.0:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n  Contrarian stopped.")
        log.info("Shutdown requested via Ctrl+C")
        server.server_close()


if __name__ == "__main__":
    main()
