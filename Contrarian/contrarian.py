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
  FUNDING_EXTREME       Any crypto perp |8h-equivalent funding| > 0.5%
                        (raw Kraken fundingRate is absolute USD/hour;
                        converted via fundingRate/markPrice*8*100)
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

# FUNDING UNITS (verified empirically 2026-07-31 against live
# https://futures.kraken.com/derivatives/api/v3/tickers):
# Kraken's raw `fundingRate` ticker field is an ABSOLUTE per-hour rate
# denominated in USD price terms — it scales with markPrice
# (PF_XBTUSD fr=0.302 @ mark 63,855 vs PF_DOGEUSD fr=2.9e-08 @ mark 0.0698).
# The relative hourly rate is fundingRate / markPrice; with that division all
# 8 majors land in the plausible band (BTC +0.0038%/8h, ETH +0.0057%/8h,
# BCH -0.020%/8h, SOL +0.013%/8h, XRP -0.0096%/8h, ...).
# We report the industry-standard 8h-equivalent percentage:
#   rate_8h_pct = (fundingRate / markPrice) * 8 * 100
# Live distribution across 275 perps: median |8h| = 0.057%, p95 = 0.32%,
# max = 4.03% (illiquid LRC). Threshold 0.5% per 8h sits ~p97 — flags a
# handful of genuinely extreme pairs, not 22 at once.
FUNDING_EXTREME_8H_PCT = 0.5  # |8h-equivalent funding| percent

# NOTE: FUNDING_AVG_GREED compares against the collector's avg_rate, which is
# a mean of RAW absolute-unit fundingRate values (dominated by high-priced
# pairs like BTC). Known-imprecise; kept as-is (out of scope for the
# 2026-07-31 funding-units fix). Do not interpret it as a percentage.
FUNDING_AVG_GREED = 0.0005   # raw-unit avg funding for greed signal
CORRELATION_HIGH = 0.8       # high correlation threshold
CORRELATION_LOW = 0.5        # breakdown target
CASCADE_DROP_PCT = 3.0       # percent drop per pair
CASCADE_MIN_PAIRS = 3        # minimum pairs for cascade signal
MAX_ALERTS = 100             # ring buffer for alert history

# Publish state-change gating
PUBLISH_HEARTBEAT_S = 1800   # 30-min re-publish for persistent conditions
PUBLISH_MATERIAL_DELTA = 0.20  # >20% relative change counts as new information

# Non-crypto symbols on Kraken Futures (tokenized equities / commodity
# trackers, "xStocks" — lowercase-x pairs like SPYx:USD in the raw ticker
# feed). Matched against the NORMALIZED base (PF_ prefix and USD suffix
# stripped, uppercased). Set verified against the live ticker list
# 2026-07-31; do NOT use an endswith("X") heuristic here — genuine crypto
# bases also end in X (AVAX, TRX, STX, DYDX, ZRX, SNX, IMX, GMX, ICX,
# IOTX, CFX, CVX, FLUX, SPX).
_NON_CRYPTO = {"SPYX", "AAPLX", "NVDAX", "GOOGLX", "GLDX", "QQQX", "TSLAX",
               "AMZNX", "MSFTX", "METAX", "COINX", "MSTRX", "XAUX",
               "ANTHROPICX", "OPENAIX", "HOODX", "CRCLX", "SPCXX"}

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


def _norm_futures_symbol(symbol):
    """Normalize a Kraken Futures rates key to a bare base ticker.

    The collector's rates dict keys arrive RAW as PF_<BASE>USD (its
    lowercase .replace("pf_", "") never matches the uppercase symbols),
    so strip the PF_ prefix and USD suffix and uppercase. Idempotent for
    already-bare keys like "BTC".
    """
    s = str(symbol).upper().strip()
    if s.startswith("PF_"):
        s = s[3:]
    if s.endswith("USD"):
        s = s[:-3]
    return s


def detect_funding_extremes(funding_data):
    """Check for FUNDING_EXTREME: |8h-equivalent funding| > FUNDING_EXTREME_8H_PCT.

    Raw Kraken fundingRate is an absolute USD-per-hour rate; converted to
    8h-equivalent percent via rate_8h_pct = rate / mark_price * 8 * 100
    (see derivation at the FUNDING_EXTREME_8H_PCT constant).

    Returns (alerts, filtered_non_crypto):
      alerts              -- list of alert dicts, one per extreme crypto perp
      filtered_non_crypto -- count of tokenized-equity symbols excluded
                             (exposed in the snapshot; truth, not hiding)
    """
    alerts = []
    filtered_non_crypto = 0
    if not funding_data:
        return alerts, filtered_non_crypto

    rates = funding_data.get("rates", {})
    for symbol, info in rates.items():
        base = _norm_futures_symbol(symbol)
        if base in _NON_CRYPTO:
            filtered_non_crypto += 1
            continue  # tokenized equity / commodity tracker, not crypto
        rate_raw = info.get("rate", 0) or 0
        mark = info.get("mark_price") or 0
        if not isinstance(mark, (int, float)) or mark <= 0:
            continue  # cannot convert to a comparable unit without mark price
        rate_8h_pct = (rate_raw / mark) * 8.0 * 100.0
        if abs(rate_8h_pct) > FUNDING_EXTREME_8H_PCT:
            direction = "LONG" if rate_8h_pct > 0 else "SHORT"
            alerts.append({
                "type": "FUNDING_EXTREME",
                "pair": f"{base}/USD",
                "rate_8h_pct": round(rate_8h_pct, 4),
                "funding_unit": "8h_equivalent_pct",
                "rate_raw": rate_raw,  # Kraken absolute USD/hour, for audit
                # compat alias — same honest 8h% value, NOT the old raw*100
                "funding_rate": round(rate_8h_pct, 4),
                "direction": direction,
                "mark_price": info.get("mark_price"),
                "open_interest": info.get("open_interest"),
                "message": (f"Extreme {direction.lower()} funding on {base}: "
                            f"{rate_8h_pct:.4f}%/8h "
                            f"({'longs pay shorts' if rate_8h_pct > 0 else 'shorts pay longs'})"),
            })
    return alerts, filtered_non_crypto


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

    Returns (state_name, score), or (None, None) when there is no reading.

    It used to return ("NEUTRAL", 50) for a missing input. That is byte-
    identical to a genuinely measured neutral market (40-60 maps to NEUTRAL),
    so an unreachable Fear & Greed source published a confident reading of a
    market nobody looked at -- and it HAS fired: contrarian.log carries
    "F&G=None, state=NEUTRAL, score=50" followed by a SENTIMENT_EXTREME
    publish. The raw fear_greed field was already served honestly as None;
    the derived pair was not.
    """
    if fear_greed is None:
        return None, None

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
        # Not "NEUTRAL"/50 — a freshly started bot has measured nothing,
        # and seeding a plausible reading means the first snapshot after a
        # restart publishes an assessment that never happened.
        self.sentiment_state = None
        self.sentiment_score = None
        self.fear_greed = None
        self.regime = "UNKNOWN"
        self.alerts = deque(maxlen=MAX_ALERTS)
        self.total_alerts_ever = 0  # true lifetime counter, not capped by deque
        self.filtered_non_crypto = 0  # tokenized equities excluded last scan
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

        # State-change gating on publishes: _last_published stores, per
        # (signal_type, pair) key, the VALUE last published — not just a
        # timestamp — so a persistent extreme re-publishes only on a
        # material change (>20% relative delta or direction flip) or on a
        # slow 30-minute heartbeat (a persistent extreme is still
        # information, just not every 5 minutes). Keys that stop firing
        # are cleared at scan end, so a fresh under->over crossing always
        # publishes immediately with no cooldown carryover.
        self._last_published = {}  # (type, pair) -> {"ts", "value", "direction"}
        self._heartbeat = PUBLISH_HEARTBEAT_S
        self._material_delta = PUBLISH_MATERIAL_DELTA
        self._active_episode_keys = set()  # keys detected during current scan

    @staticmethod
    def _comparable_value(signal_type, data):
        """Pick the scalar used for material-change comparison per signal type."""
        if signal_type == "FUNDING_EXTREME":
            return data.get("rate_8h_pct")
        if signal_type in ("EXTREME_GREED", "EXTREME_FEAR"):
            return data.get("fear_greed")
        if signal_type == "LIQUIDATION_CASCADE":
            return data.get("pairs_affected")
        if signal_type == "CORRELATION_BREAKDOWN":
            return data.get("current_correlation")
        return None

    def _publish(self, signal_type, data):
        """Publish a SENTIMENT_EXTREME event to the fleet bus.

        State-change gated per (signal_type, pair): while a condition
        persists over threshold, re-publish only when the value changes
        materially (>20% relative delta or direction flip) or on the
        30-minute heartbeat. A new under->over crossing (key absent from
        _last_published — stale keys are cleared at scan end) always
        publishes immediately.
        """
        now = time.time()
        pair = data.get("pair") or data.get("pair_key") or ""
        key = (signal_type, pair)
        self._active_episode_keys.add(key)

        value = self._comparable_value(signal_type, data)
        direction = data.get("direction")
        prev = self._last_published.get(key)

        if prev is not None:
            changed = False
            # Direction flip is always material
            if (direction is not None and prev.get("direction") is not None
                    and direction != prev.get("direction")):
                changed = True
            # >20% relative delta in the comparable value is material
            if not changed:
                pv = prev.get("value")
                if value is not None and pv is not None:
                    denom = max(abs(pv), 1e-9)
                    if abs(value - pv) / denom > self._material_delta:
                        changed = True
            if not changed and (now - prev.get("ts", 0)) < self._heartbeat:
                log.debug(f"State unchanged for {signal_type}/{pair}, "
                          f"skipping publish (heartbeat in "
                          f"{self._heartbeat - (now - prev.get('ts', 0)):.0f}s)")
                return False

        self._last_published[key] = {"ts": now, "value": value,
                                     "direction": direction}

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
        # True = this was a new crossing, a material change, or a heartbeat.
        # Callers gate their WARNING log on this so a persistent condition is
        # not re-logged every scan (see the log sites in the scan loop).
        return True

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
            self.total_alerts_ever += 1  # true lifetime count, deque caps at 100

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
                # Keep the LAST GOOD reading when this fetch failed rather
                # than blanking it: a transient API miss should not erase a
                # sentiment measured 60s ago. But never replace a real
                # reading with a fabricated neutral, which is what the old
                # ("NEUTRAL", 50) return did.
                if state is not None:
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
            # Reset episode tracking: _publish records every key detected
            # this scan; keys absent at scan end have dropped back under
            # threshold and get cleared from _last_published below.
            self._active_episode_keys = set()

            # Extreme greed
            # NOTE on logging: `signals_found` counts every condition ACTIVE
            # this scan (the honest gauge of current market state), but the
            # WARNING line fires only when _publish reports a new crossing,
            # a material change, or the 30-minute heartbeat. Previously the
            # log was unconditional, so a persistent condition re-logged
            # every scan: 12 funding pairs produced 162 identical WARNING
            # lines in one window, which buries genuine warnings and is the
            # reason a log stops being read.
            greed = detect_extreme_greed(fear_greed, funding_data, corr_data)
            if greed:
                self._add_alert(greed)
                _new = self._publish("EXTREME_GREED", greed)
                signals_found += 1
                if _new:
                    log.warning(f"SIGNAL: {greed['message']}")

            # Extreme fear
            fear = detect_extreme_fear(fear_greed, corr_data)
            if fear:
                self._add_alert(fear)
                _new = self._publish("EXTREME_FEAR", fear)
                signals_found += 1
                if _new:
                    log.warning(f"SIGNAL: {fear['message']}")

            # Funding extremes (can produce multiple alerts)
            funding_alerts, filtered_nc = detect_funding_extremes(funding_data)
            with self._lock:
                self.filtered_non_crypto = filtered_nc
            if filtered_nc:
                log.info(f"Funding scan: {filtered_nc} non-crypto symbol(s) excluded")
            _suppressed = 0
            for fa in funding_alerts:
                self._add_alert(fa)
                _new = self._publish("FUNDING_EXTREME", fa)
                signals_found += 1
                if _new:
                    log.warning(f"SIGNAL: {fa['message']}")
                else:
                    _suppressed += 1
            if _suppressed:
                # Say how many were held back, so a quiet log is never
                # mistaken for a quiet market.
                log.info(f"Funding scan: {_suppressed} ongoing extreme(s) "
                         f"unchanged since last publish (not re-logged)")

            # Liquidation cascade
            cascade = detect_liquidation_cascade(pairs)
            if cascade:
                self._add_alert(cascade)
                _new = self._publish("LIQUIDATION_CASCADE", cascade)
                signals_found += 1
                if _new:
                    log.warning(f"SIGNAL: {cascade['message']}")

            # Correlation breakdown
            breakdowns = detect_correlation_breakdown(corr_data, self._prev_corr_data)
            for bd in breakdowns:
                self._add_alert(bd)
                _new = self._publish("CORRELATION_BREAKDOWN", bd)
                signals_found += 1
                if _new:
                    log.warning(f"SIGNAL: {bd['message']}")

            # Episode reconciliation: keys published previously but not
            # detected this scan have dropped back under threshold — clear
            # them so the next under->over crossing publishes immediately
            # (re-fire on new crossing regardless of any cooldown).
            stale_keys = [k for k in self._last_published
                          if k not in self._active_episode_keys]
            for k in stale_keys:
                del self._last_published[k]
            if stale_keys:
                log.debug(f"Cleared {len(stale_keys)} ended alert episode(s)")

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
                "total_alerts": len(self.alerts),  # compat: ring-buffer len, caps at 100
                "total_alerts_ever": self.total_alerts_ever,  # true lifetime count
                "filtered_non_crypto": self.filtered_non_crypto,  # excluded last scan
                "funding_unit": "8h_equivalent_pct",  # unit of rate_8h_pct in alerts
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
    log.info("Scan thread starting, waiting 20s for fleet boot...")
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
    print("  CONTRARIAN v1.0 -- Sentiment Extreme Detection")
    print("  ================================================")
    print(f"  Port:       {PORT}")
    print(f"  Accent:     {ACCENT}")
    print(f"  Interval:   {SCAN_INTERVAL}s")
    print(f"  Log:        {LOG_FILE}")
    print(f"  Fleet bus:  {CC_URL}")
    print(f"  Publisher:  {'active' if EventPublisher else 'UNAVAILABLE'}")
    print(f"  Endpoints:  http://localhost:{PORT}/health")
    print(f"              http://localhost:{PORT}/api/snapshot")
    print("  Press Ctrl+C to stop")
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
        print("\n  Contrarian stopped.")
        log.info("Shutdown requested via Ctrl+C")
        server.server_close()


if __name__ == "__main__":
    main()
