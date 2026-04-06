#!/usr/bin/env python3
"""
ARBITRAGEUR -- Statistical Arbitrage / Pair Trading Bot
========================================================
Bot #14. Port 8085. Color: #7c4dff (deep purple).

Trades the spread between highly correlated crypto pairs.
Fetches correlation matrix from Brainiac, identifies pairs with
correlation > 0.8, computes log price ratio (spread), and trades
z-score mean reversion.

Entry:  z > +2.0  -> SHORT pair A, LONG pair B
        z < -2.0  -> LONG pair A, SHORT pair B
Exit:   |z| < 0.5 (MEAN_REVERSION), |z| > 3.5 (STOP_LOSS), 48h (TIME_STOP)

Max 5 spread positions, 4% per leg, paper balance $10,000.
Scans every 120 seconds.

Usage: python arbitrageur.py
"""

import json
import logging
import math
import os
import sys
from pathlib import Path
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Fleet integration
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "CommandCenter"))

try:
    from bus_listener import BusListener
except ImportError:
    BusListener = None

try:
    from portfolio_client import PortfolioClient
except ImportError:
    PortfolioClient = None

try:
    from event_publisher import EventPublisher
except ImportError:
    EventPublisher = None
try:
    from expectancy import ExpectancyTracker
    _expectancy = ExpectancyTracker()
except Exception:
    _expectancy = None
try:
    from kraken_ohlc import fetch_ohlc as _fetch_ohlc_canonical
except ImportError:
    _fetch_ohlc_canonical = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 8085
BOT_NAME = "Arbitrageur"
ACCENT = "#7c4dff"
CC_URL = "http://127.0.0.1:9000"

SCAN_INTERVAL = 120          # seconds between scans
CORR_THRESHOLD = 0.80        # minimum correlation to consider a pair
ZSCORE_ENTRY = 2.0           # z-score to open a spread (lowered from 2.5 — $100 min trade reduces fee drag)
FEE_RATE = 0.0026            # Kraken taker fee per leg (4 legs total per round-trip)
ZSCORE_EXIT = 0.5            # z-score to close (mean reversion)
ZSCORE_STOP = 3.5            # z-score stop loss (divergence blowout)
TIME_STOP_HOURS = 48         # max hours to hold a spread
LOOKBACK = 20                # rolling window for z-score
MAX_SPREADS = 5              # max simultaneous spread positions
LEG_PCT = 0.04               # 4% of equity per leg
INITIAL_EQUITY = 10_000.0    # paper balance

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arbitrageur.log")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("Arbitrageur")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_json(url, timeout=5):
    """GET JSON from a URL. Returns dict/list or None on failure."""
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def pearson(x, y):
    """Compute Pearson correlation coefficient between two lists."""
    n = min(len(x), len(y))
    if n < 5:
        return 0.0
    x, y = x[-n:], y[-n:]
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    dx = math.sqrt(sum((v - mx) ** 2 for v in x) + 1e-15)
    dy = math.sqrt(sum((v - my) ** 2 for v in y) + 1e-15)
    return num / (dx * dy)


# ---------------------------------------------------------------------------
# Spread Position
# ---------------------------------------------------------------------------

class SpreadPosition:
    """Tracks a single spread (pair) trade."""

    def __init__(self, pair_a, pair_b, direction, z_at_entry,
                 price_a, price_b, size_a, size_b,
                 reservation_a=None, reservation_b=None):
        self.id = str(uuid.uuid4())[:8]
        self.pair_a = pair_a               # leg A
        self.pair_b = pair_b               # leg B
        self.direction = direction         # "SHORT_A_LONG_B" or "LONG_A_SHORT_B"
        self.z_at_entry = z_at_entry
        self.entry_price_a = price_a
        self.entry_price_b = price_b
        self.size_a = size_a               # USD notional
        self.size_b = size_b
        self.entry_time = time.time()
        self.reservation_a = reservation_a
        self.reservation_b = reservation_b
        self.exit_reason = None
        self.pnl = 0.0

    def label(self):
        return f"{self.pair_a}|{self.pair_b}"

    def age_hours(self):
        return (time.time() - self.entry_time) / 3600

    def to_dict(self):
        return {
            "id": self.id,
            "pair_key": f"{self.pair_a}|{self.pair_b}",
            "pair_a": self.pair_a,
            "pair_b": self.pair_b,
            "direction": self.direction,
            "z_at_entry": round(self.z_at_entry, 3),
            "entry_price_a": round(self.entry_price_a, 4),
            "entry_price_b": round(self.entry_price_b, 4),
            "size_a": round(self.size_a, 2),
            "size_b": round(self.size_b, 2),
            "age_hours": round(self.age_hours(), 2),
            "reservation_a": self.reservation_a,
            "reservation_b": self.reservation_b,
        }


# ---------------------------------------------------------------------------
# Arbitrageur Engine
# ---------------------------------------------------------------------------

class ArbitrageurEngine:
    """Core stat-arb engine: correlation scanning, spread tracking, z-score trading."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.time()
        self.scan_count = 0
        self.scan_duration = 0.0

        # Paper balance
        self.equity = INITIAL_EQUITY
        self.realized_pnl = 0.0

        # Positions
        self.open_spreads: list[SpreadPosition] = []
        self.closed_spreads: list[dict] = []   # last 50 closed

        # Market state
        self.tracked_pairs: dict[str, dict] = {}   # "A|B" -> {corr, z, spread_history}
        self.all_correlations: dict[str, float] = {}  # "A|B" -> corr
        self.price_cache: dict[str, list[float]] = {}  # pair -> [close prices]

        # Position persistence
        self._positions_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arbitrageur_state.json")
        self._load_positions()

        # Fleet integration
        self._bus = BusListener() if BusListener else None
        self._portfolio = (PortfolioClient(CC_URL, "arbitrageur")
                           if PortfolioClient else None)
        self._publisher = (EventPublisher(CC_URL, "arbitrageur")
                           if EventPublisher else None)

        # Log buffer for snapshot
        self._log_buf = deque(maxlen=30)

    # -----------------------------------------------------------------------
    # Internal log
    # -----------------------------------------------------------------------

    def _log(self, msg):
        log.info(msg)
        self._log_buf.append(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}")

    # -----------------------------------------------------------------------
    # Portfolio Reconciliation
    # -----------------------------------------------------------------------

    def _reconcile_positions(self):
        """On startup, verify reservations are still valid. Try to re-reserve if stale."""
        if not self._portfolio:
            return

        reservations = self._portfolio.get_reservations()
        if not reservations:
            return

        for pos in list(self.open_spreads):
            stale = []
            if pos.reservation_a and pos.reservation_a not in reservations:
                stale.append("A")
            if pos.reservation_b and pos.reservation_b not in reservations:
                stale.append("B")
            if not stale:
                continue

            # Try re-reserve both legs
            ok_a = ok_b = True
            new_a = new_b = None
            if "A" in stale:
                ok_a, new_a = self._portfolio.reserve(pos.pair_a, "buy", pos.size_a)
            if "B" in stale:
                ok_b, new_b = self._portfolio.reserve(pos.pair_b, "sell", pos.size_b)

            if ok_a and ok_b:
                pos.reservation_a = new_a
                pos.reservation_b = new_b
                self._save_positions()
                self._log(f"Re-reserved spread {pos.id}")
            else:
                self._log(f"Stale reservation for spread {pos.id} - closing", "WARNING")
                self._close_spread(pos, "stale_reservation", 0, 0)

    # -----------------------------------------------------------------------
    # Position Persistence
    # -----------------------------------------------------------------------

    def _load_positions(self):
        """Load positions from disk on startup."""
        if not os.path.exists(self._positions_file):
            return
        try:
            with open(self._positions_file, "r") as f:
                data = json.load(f)
            loaded = 0
            for pdata in data.get("open_spreads", []):
                pos = SpreadPosition(
                    pdata.get("pair_a"),
                    pdata.get("pair_b"),
                    pdata.get("direction"),
                    pdata.get("z_at_entry"),
                    pdata.get("entry_price_a"),
                    pdata.get("entry_price_b"),
                    pdata.get("size_a"),
                    pdata.get("size_b"),
                    pdata.get("reservation_a"),
                    pdata.get("reservation_b"),
                )
                pos.id = pdata.get("id")
                pos.entry_time = pdata.get("entry_time", time.time())
                self.open_spreads.append(pos)
                loaded += 1
            if loaded > 0:
                self._log(f"Restored {loaded} spread position(s) from disk")
        except Exception as e:
            log.warning(f"Failed to load positions: {e}")

    def _save_positions(self):
        """Atomic save of positions to disk."""
        try:
            data = {
                "open_spreads": [
                    {
                        "id": p.id,
                        "pair_a": p.pair_a,
                        "pair_b": p.pair_b,
                        "direction": p.direction,
                        "z_at_entry": p.z_at_entry,
                        "entry_price_a": p.entry_price_a,
                        "entry_price_b": p.entry_price_b,
                        "size_a": p.size_a,
                        "size_b": p.size_b,
                        "entry_time": p.entry_time,
                        "reservation_a": p.reservation_a,
                        "reservation_b": p.reservation_b,
                    }
                    for p in self.open_spreads
                ],
                "saved_at": time.time(),
            }
            tmp = self._positions_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._positions_file)
        except Exception as e:
            log.warning(f"Failed to save positions: {e}")

    # -----------------------------------------------------------------------
    # Data Fetching
    # -----------------------------------------------------------------------

    def _fetch_correlations(self):
        """Fetch correlation matrix from Brainiac via Command Center."""
        data = fetch_json(f"{CC_URL}/api/brainiac/correlations")
        if not data or not data.get("data"):
            self._log("WARN: No correlation data from Brainiac")
            return {}

        d = data["data"]
        matrix = {}
        for item in d.get("top_correlated", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                matrix[item[0]] = float(item[1])
        for item in d.get("least_correlated", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                matrix[item[0]] = float(item[1])

        # Also pull the full correlation data if the endpoint returns all pairs
        # The top/least lists are truncated; we may need to compute our own
        # from OHLC if we want all pairs with corr > 0.8
        return matrix

    def _fetch_universe(self):
        """Get active trading pairs from Command Center."""
        data = fetch_json(f"{CC_URL}/api/universe")
        if data and data.get("pairs"):
            return [p.get("display", p.get("pair", "")) for p in data["pairs"][:20]]
        return ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
                "DOT/USD", "AVAX/USD", "LINK/USD", "MATIC/USD", "ATOM/USD"]

    def _fetch_ohlc(self, pair, interval=60, limit=100):
        """Fetch OHLC candles — CC proxy first, Kraken fallback via canonical module."""
        if _fetch_ohlc_canonical:
            return _fetch_ohlc_canonical(pair, interval, limit, cc_url=CC_URL)
        # Fallback if kraken_ohlc unavailable
        data = fetch_json(
            f"{CC_URL}/api/market/ohlc?pair={pair}&interval={interval}&limit={limit}"
        )
        if data and data.get("candles"):
            return data["candles"]
        return []

    # -----------------------------------------------------------------------
    # Spread / Z-Score Computation
    # -----------------------------------------------------------------------

    def _compute_log_spread(self, closes_a, closes_b):
        """Compute log price ratio spread = ln(A/B)."""
        n = min(len(closes_a), len(closes_b))
        if n < LOOKBACK:
            return [], None, None
        a = closes_a[-n:]
        b = closes_b[-n:]
        spread = [math.log(a[i] / b[i]) for i in range(n) if b[i] > 0 and a[i] > 0]
        if len(spread) < LOOKBACK:
            return spread, None, None

        # Rolling z-score over last LOOKBACK bars
        window = spread[-LOOKBACK:]
        mean = sum(window) / len(window)
        std = math.sqrt(sum((x - mean) ** 2 for x in window) / len(window))
        if std < 1e-10:
            return spread, 0.0, std

        z = (spread[-1] - mean) / std
        return spread, z, std

    # -----------------------------------------------------------------------
    # Correlation Scanning & Pair Discovery
    # -----------------------------------------------------------------------

    def _build_correlation_map(self, pairs):
        """
        Build full correlation map from OHLC closes.
        Uses Brainiac data first, fills gaps with our own calculation.
        """
        # Fetch OHLC closes for all pairs
        for pair in pairs:
            if pair not in self.price_cache or len(self.price_cache[pair]) < LOOKBACK:
                candles = self._fetch_ohlc(pair, interval=60, limit=100)
                if candles and len(candles) >= LOOKBACK:
                    closes = [float(c[4]) for c in candles if len(c) > 4]
                    if len(closes) >= LOOKBACK:
                        self.price_cache[pair] = closes
                time.sleep(0.3)  # rate limit Kraken

        # Get Brainiac correlations as starting point
        brainiac = self._fetch_correlations()
        self.all_correlations = {}

        # Use Brainiac data where available
        for key, corr in brainiac.items():
            self.all_correlations[key] = corr

        # Compute remaining from cached OHLC data
        cached_pairs = [p for p in pairs if p in self.price_cache
                        and len(self.price_cache[p]) >= LOOKBACK]

        for i in range(len(cached_pairs)):
            for j in range(i + 1, len(cached_pairs)):
                pa, pb = cached_pairs[i], cached_pairs[j]
                key = f"{pa}|{pb}"
                rkey = f"{pb}|{pa}"
                if key not in self.all_correlations and rkey not in self.all_correlations:
                    # Compute log returns
                    ca = self.price_cache[pa]
                    cb = self.price_cache[pb]
                    n = min(len(ca), len(cb))
                    if n < LOOKBACK:
                        continue
                    ra = [math.log(ca[k] / ca[k - 1]) for k in range(1, n) if ca[k - 1] > 0]
                    rb = [math.log(cb[k] / cb[k - 1]) for k in range(1, n) if cb[k - 1] > 0]
                    corr = pearson(ra, rb)
                    self.all_correlations[key] = round(corr, 4)

    def _find_tradeable_pairs(self):
        """
        From the correlation map, find pairs with |corr| > CORR_THRESHOLD.
        Compute z-scores for each. Update tracked_pairs.
        """
        candidates = {}
        for key, corr in self.all_correlations.items():
            if abs(corr) >= CORR_THRESHOLD:
                parts = key.split("|")
                if len(parts) == 2:
                    candidates[key] = corr

        new_tracked = {}
        for key, corr in candidates.items():
            pa, pb = key.split("|")
            if pa not in self.price_cache or pb not in self.price_cache:
                continue
            closes_a = self.price_cache[pa]
            closes_b = self.price_cache[pb]
            spread, z, std = self._compute_log_spread(closes_a, closes_b)

            if z is not None:
                new_tracked[key] = {
                    "pair_a": pa,
                    "pair_b": pb,
                    "correlation": corr,
                    "z_score": round(z, 3),
                    "spread_std": round(std, 6) if std else 0,
                    "spread_len": len(spread),
                    "current_price_a": closes_a[-1] if closes_a else 0,
                    "current_price_b": closes_b[-1] if closes_b else 0,
                }

        with self._lock:
            self.tracked_pairs = new_tracked

    # -----------------------------------------------------------------------
    # PHITEX Check (avoid phase transitions)
    # -----------------------------------------------------------------------

    def _phitex_blocked(self, pair):
        """Return True if PHITEX says this pair is in a critical phase transition."""
        if not self._bus:
            return False
        try:
            return self._bus.phitex_critical(pair, max_age=120)
        except Exception:
            return False

    # -----------------------------------------------------------------------
    # Position Management
    # -----------------------------------------------------------------------

    def _open_spread(self, key, info):
        """Open a spread position if limits allow."""
        z = info["z_score"]
        pa = info["pair_a"]
        pb = info["pair_b"]
        price_a = info["current_price_a"]
        price_b = info["current_price_b"]

        if len(self.open_spreads) >= MAX_SPREADS:
            return

        # Check if we already have this spread open
        for pos in self.open_spreads:
            if pos.label() == key or pos.label() == f"{pb}|{pa}":
                return

        # PHITEX check: skip if either leg is in phase transition
        if self._phitex_blocked(pa) or self._phitex_blocked(pb):
            self._log(f"BLOCKED by PHITEX: {key}")
            return

        # Determine direction
        if z > ZSCORE_ENTRY:
            # Spread too high -> expect reversion down
            # SHORT A, LONG B
            direction = "SHORT_A_LONG_B"
        elif z < -ZSCORE_ENTRY:
            # Spread too low -> expect reversion up
            # LONG A, SHORT B
            direction = "LONG_A_SHORT_B"
        else:
            return

        # Size: 4% of equity per leg
        leg_size = self.equity * LEG_PCT
        if leg_size < 10.0:
            self._log(f"SKIP {key}: insufficient equity ({self.equity:.2f})")
            return

        # Fee awareness: log round-trip cost so it's visible in audit
        round_trip_fees = leg_size * FEE_RATE * 4  # 4 legs: open A, open B, close A, close B
        self._log(
            f"FEE CHECK {key}: round-trip fees ${round_trip_fees:.2f} on ${leg_size:.0f}/leg "
            f"(z={z:.2f}, need >{round_trip_fees/leg_size*100:.1f}% move to profit)"
        )

        # Portfolio reservation
        res_a, res_b = None, None
        if self._portfolio:
            dir_a = "SHORT" if direction == "SHORT_A_LONG_B" else "LONG"
            dir_b = "LONG" if direction == "SHORT_A_LONG_B" else "SHORT"
            ok_a, rid_a = self._portfolio.reserve(pa, dir_a, leg_size)
            if ok_a:
                res_a = rid_a
            else:
                self._log(f"Portfolio reserve FAIL leg A ({pa}): {rid_a}")
                return
            ok_b, rid_b = self._portfolio.reserve(pb, dir_b, leg_size)
            if ok_b:
                res_b = rid_b
            else:
                # Release first leg
                self._portfolio.release(res_a, pnl=0.0)
                self._log(f"Portfolio reserve FAIL leg B ({pb}): {rid_b}")
                return

        pos = SpreadPosition(
            pair_a=pa, pair_b=pb, direction=direction,
            z_at_entry=z, price_a=price_a, price_b=price_b,
            size_a=leg_size, size_b=leg_size,
            reservation_a=res_a, reservation_b=res_b,
        )
        self.open_spreads.append(pos)
        self._save_positions()
        self._log(
            f"OPEN {pos.id} | {key} | {direction} | z={z:.2f} | "
            f"A={price_a:.4f} B={price_b:.4f} | ${leg_size:.0f}/leg"
        )

        # Publish events
        if self._publisher:
            self._publisher.emit("SPREAD_OPEN", {
                "spread_id": pos.id,
                "pair_a": pa,
                "pair_b": pb,
                "direction": direction,
                "z_score": round(z, 3),
                "size_per_leg": round(leg_size, 2),
            })
            # Also publish as TRADE_OPEN for fleet analytics
            self._publisher.emit("TRADE_OPEN", {
                "pair": f"{pa}|{pb}",
                "direction": "SHORT" if direction == "SHORT_A_LONG_B" else "LONG",
                "entry": round(z, 3),
                "size": round(leg_size * 2, 2),
            })

    def _check_exits(self):
        """Check open spreads for exit conditions."""
        to_close = []
        for pos in self.open_spreads:
            key = pos.label()
            rkey = f"{pos.pair_b}|{pos.pair_a}"

            # Get current z-score
            info = self.tracked_pairs.get(key) or self.tracked_pairs.get(rkey)
            if info:
                current_z = info["z_score"]
                # Flip z-sign if the tracked pair is reversed
                if rkey in self.tracked_pairs and key not in self.tracked_pairs:
                    current_z = -current_z
            else:
                # Pair fell out of tracked (corr dropped) -- use cached prices
                if (pos.pair_a in self.price_cache and pos.pair_b in self.price_cache):
                    _, z, _ = self._compute_log_spread(
                        self.price_cache[pos.pair_a],
                        self.price_cache[pos.pair_b],
                    )
                    current_z = z if z is not None else pos.z_at_entry
                else:
                    current_z = pos.z_at_entry

            # Determine exit reason
            reason = None
            if abs(current_z) < ZSCORE_EXIT:
                reason = "MEAN_REVERSION"
            elif abs(current_z) > ZSCORE_STOP:
                reason = "STOP_LOSS"
            elif pos.age_hours() >= TIME_STOP_HOURS:
                reason = "TIME_STOP"

            if reason:
                # Calculate PnL
                pnl = self._compute_spread_pnl(pos, current_z)
                pos.exit_reason = reason
                pos.pnl = pnl
                to_close.append((pos, reason, current_z, pnl))

        for pos, reason, current_z, pnl in to_close:
            self._close_spread(pos, reason, current_z, pnl)

    def _compute_spread_pnl(self, pos, current_z):
        """
        Estimate PnL from z-score movement.
        If z moved toward zero from entry, the spread trade is profitable.
        PnL is proportional to the z-score change times the spread volatility.
        """
        pa = pos.pair_a
        pb = pos.pair_b

        # Get current prices
        price_a = (self.price_cache.get(pa, [0])[-1]
                   if pa in self.price_cache else pos.entry_price_a)
        price_b = (self.price_cache.get(pb, [0])[-1]
                   if pb in self.price_cache else pos.entry_price_b)

        if pos.entry_price_a == 0 or pos.entry_price_b == 0:
            return 0.0

        # Return on each leg
        ret_a = (price_a - pos.entry_price_a) / pos.entry_price_a
        ret_b = (price_b - pos.entry_price_b) / pos.entry_price_b

        if pos.direction == "SHORT_A_LONG_B":
            # Short A (profit if A falls), Long B (profit if B rises)
            pnl = pos.size_a * (-ret_a) + pos.size_b * ret_b
        else:
            # Long A (profit if A rises), Short B (profit if B falls)
            pnl = pos.size_a * ret_a + pos.size_b * (-ret_b)

        return round(pnl, 2)

    def _close_spread(self, pos, reason, current_z, pnl):
        """Close a spread position and update state."""
        self.open_spreads = [p for p in self.open_spreads if p.id != pos.id]
        self._save_positions()
        self.realized_pnl += pnl
        self.equity += pnl

        if _expectancy:
            try:
                _expectancy.record_trade(
                    bot_id='arbitrageur',
                    pair=pos.label(),
                    direction=pos.direction,
                    entry_price=pos.entry_price_a,
                    exit_price=pos.entry_price_a * (1 + pnl / (pos.size_a + pos.size_b)) if (pos.size_a + pos.size_b) > 0 else pos.entry_price_a,
                    size_usd=pos.size_a + pos.size_b,
                    duration=pos.age_hours() * 3600,
                )
            except Exception:
                pass

        record = {
            **pos.to_dict(),
            "exit_reason": reason,
            "exit_z": round(current_z, 3),
            "pnl": round(pnl, 2),
            "exit_time": time.time(),
            "hold_hours": round(pos.age_hours(), 2),
        }
        self.closed_spreads.append(record)
        if len(self.closed_spreads) > 50:
            self.closed_spreads = self.closed_spreads[-50:]

        self._log(
            f"CLOSE {pos.id} | {pos.label()} | {reason} | "
            f"z: {pos.z_at_entry:.2f} -> {current_z:.2f} | "
            f"PnL: ${pnl:+.2f} | held {pos.age_hours():.1f}h"
        )

        # Release portfolio reservations
        if self._portfolio:
            # Report outcome to signal aggregator for learning
            try:
                import urllib.request as urlreq
                url = f"{CC_URL}/api/signals/outcome"
                fees = abs(pos.size_a + pos.size_b) * 0.0026 * 2  # Approx round-trip fees
                data = json.dumps({
                    "bot_id": "arbitrageur",
                    "pair": f"{pos.pair_a}/{pos.pair_b}",
                    "direction": pos.direction,
                    "won": pnl > 0,
                    "pnl": float(pnl),
                    "fees": float(fees)
                }).encode("utf-8")
                req = urlreq.Request(url, data=data, headers={"Content-Type": "application/json"})
                urlreq.urlopen(req, timeout=3)
            except Exception:
                pass
            
            if pos.reservation_a:
                self._portfolio.release(pos.reservation_a, pnl=pnl / 2)
            if pos.reservation_b:
                self._portfolio.release(pos.reservation_b, pnl=pnl / 2)

        # Publish events
        if self._publisher:
            self._publisher.emit("SPREAD_CLOSE", {
                "spread_id": pos.id,
                "pair_a": pos.pair_a,
                "pair_b": pos.pair_b,
                "direction": pos.direction,
                "exit_reason": reason,
                "z_entry": round(pos.z_at_entry, 3),
                "z_exit": round(current_z, 3),
                "pnl": round(pnl, 2),
                "hold_hours": round(pos.age_hours(), 2),
            })
            # Also publish as TRADE_CLOSE for fleet analytics
            self._publisher.emit("TRADE_CLOSE", {
                "pair": f"{pos.pair_a}|{pos.pair_b}",
                "direction": "SHORT" if pos.direction == "SHORT_A_LONG_B" else "LONG",
                "pnl": round(pnl, 2),
                "exit_reason": reason,
                "duration_s": round(pos.age_hours() * 3600, 0),
            })

    # -----------------------------------------------------------------------
    # Main Scan Cycle
    # -----------------------------------------------------------------------

    def scan(self):
        """One full scan cycle: fetch data, update spreads, trade."""
        t0 = time.time()
        self.scan_count += 1
        self._log(f"--- Scan #{self.scan_count} ---")

        # 1. Get universe
        pairs = self._fetch_universe()
        self._log(f"Universe: {len(pairs)} pairs")

        # 2. Build correlation map (fetches OHLC as needed)
        self._build_correlation_map(pairs)
        high_corr = sum(1 for v in self.all_correlations.values()
                        if abs(v) >= CORR_THRESHOLD)
        self._log(f"Correlations: {len(self.all_correlations)} pairs, "
                  f"{high_corr} above {CORR_THRESHOLD}")

        # 3. Compute z-scores for correlated pairs
        self._find_tradeable_pairs()
        self._log(f"Tracked spreads: {len(self.tracked_pairs)}")

        # 4. Check exit conditions on open positions
        self._check_exits()

        # 5. Scan for new entries
        for key, info in self.tracked_pairs.items():
            z = info["z_score"]
            if abs(z) >= ZSCORE_ENTRY:
                self._open_spread(key, info)

        self.scan_duration = time.time() - t0
        self._log(
            f"Scan complete in {self.scan_duration:.1f}s | "
            f"Open: {len(self.open_spreads)} | "
            f"Equity: ${self.equity:,.2f} | "
            f"PnL: ${self.realized_pnl:+,.2f}"
        )

    # -----------------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------------

    def _win_rate(self):
        """Win rate from closed trades."""
        if not self.closed_spreads:
            return 0.0
        wins = sum(1 for t in self.closed_spreads if t.get("pnl", 0) > 0)
        return round(wins / len(self.closed_spreads) * 100, 1)

    def _top_spreads(self, n=5):
        """Top N tracked spreads by absolute z-score."""
        items = sorted(
            self.tracked_pairs.values(),
            key=lambda x: abs(x.get("z_score", 0)),
            reverse=True,
        )
        return [
            {
                "pair": f"{it['pair_a']}|{it['pair_b']}",
                "correlation": it["correlation"],
                "z_score": it["z_score"],
                "price_a": round(it["current_price_a"], 4),
                "price_b": round(it["current_price_b"], 4),
            }
            for it in items[:n]
        ]

    # -----------------------------------------------------------------------
    # Snapshot (for HTTP API)
    # -----------------------------------------------------------------------

    def snapshot(self):
        """Full bot state for /api/snapshot."""
        with self._lock:
            positions = {}
            for pos in self.open_spreads:
                positions[pos.id] = {
                    **pos.to_dict(),
                    "current_z": None,
                }
                # Inject current z
                info = (self.tracked_pairs.get(pos.label()) or
                        self.tracked_pairs.get(f"{pos.pair_b}|{pos.pair_a}"))
                if info:
                    positions[pos.id]["current_z"] = info["z_score"]

            result = {
                "bot_name": BOT_NAME,
                "status": "SCANNING" if self.scan_count > 0 else "STARTING",
                "accent": ACCENT,
                "port": PORT,
                "scan_count": self.scan_count,
                "scan_interval": SCAN_INTERVAL,
                "equity": round(self.equity, 2),
                "initial_equity": INITIAL_EQUITY,
                "open_spreads": len(self.open_spreads),
                "total_trades": len(self.closed_spreads),
                "win_rate": self._win_rate(),
                "pnl": round(self.realized_pnl, 2),
                "pnl_pct": round(self.realized_pnl / INITIAL_EQUITY * 100, 2),
                "tracked_pairs": len(self.tracked_pairs),
                "correlation_pairs": len(self.all_correlations),
                "positions": positions,
                "open_spreads_detail": list(positions.values()),
                "top_spreads": self._top_spreads(5),
                "closed_recent": self.closed_spreads[-10:],
                "strategy": {
                    "name": "Statistical Arbitrage / Pair Trading",
                    "entry_z": ZSCORE_ENTRY,
                    "exit_z": ZSCORE_EXIT,
                    "stop_z": ZSCORE_STOP,
                    "time_stop_hours": TIME_STOP_HOURS,
                    "lookback": LOOKBACK,
                    "corr_threshold": CORR_THRESHOLD,
                    "max_spreads": MAX_SPREADS,
                    "leg_pct": LEG_PCT,
                    "fee_rate": FEE_RATE,
                    "round_trip_fee_usd": round(self.equity * LEG_PCT * FEE_RATE * 4, 2),
                },
                "regime": self._infer_regime(),
                "uptime_s": round(time.time() - self._started, 1),
                "scan_duration_s": round(self.scan_duration, 2),
                "timestamp": time.time(),
                "logs": list(self._log_buf)[-15:],
            }
            if _expectancy:
                result["expectancy"] = _expectancy.bot_snapshot_fields('arbitrageur')
            return result

    def _infer_regime(self):
        """Infer market regime from spread behavior."""
        if not self.tracked_pairs:
            return "UNKNOWN"
        zs = [abs(v["z_score"]) for v in self.tracked_pairs.values()]
        avg_z = sum(zs) / len(zs) if zs else 0

        if avg_z > 2.0:
            return "DISLOCATION"   # many spreads blown out
        elif avg_z > 1.0:
            return "STRESSED"      # some divergence
        else:
            return "MEAN_REVERTING"  # spreads near equilibrium


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

_engine = ArbitrageurEngine()
_engine._reconcile_positions()


class ArbitrageurHandler(BaseHTTPRequestHandler):
    """Serves /health and /api/snapshot."""

    def log_message(self, fmt, *args):
        pass  # suppress default stderr logging

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/api/snapshot":
            data = json.dumps(_engine.snapshot(), default=str)
            self._respond(200, data)
        elif path == "/health":
            data = json.dumps({
                "status": "ok",
                "bot": BOT_NAME,
                "port": PORT,
                "scan_count": _engine.scan_count,
                "equity": round(_engine.equity, 2),
                "open_spreads": len(_engine.open_spreads),
                "timestamp": time.time(),
            })
            self._respond(200, data)
        else:
            self.send_error(404)

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# Scan Loop
# ---------------------------------------------------------------------------

def _scan_loop():
    """Background thread: scan every SCAN_INTERVAL seconds."""
    time.sleep(20)  # wait for fleet to boot
    while True:
        try:
            _engine.scan()
        except Exception as e:
            _engine._log(f"ERROR in scan: {e}")
            log.exception("Scan error")
        time.sleep(SCAN_INTERVAL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        ensure_port(PORT, "arbitrageur")
        write_pidfile("arbitrageur", PORT)
        atexit.register(cleanup_pidfile, "arbitrageur")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    print()
    print(f"  ARBITRAGEUR v1.0 -- Statistical Arbitrage / Pair Trading")
    print(f"  Port: {PORT}")
    print(f"  Accent: {ACCENT}")
    print(f"  Equity: ${INITIAL_EQUITY:,.0f} (paper)")
    print(f"  Scan interval: {SCAN_INTERVAL}s")
    print(f"  Correlation threshold: {CORR_THRESHOLD}")
    print(f"  Z-score entry/exit/stop: {ZSCORE_ENTRY}/{ZSCORE_EXIT}/{ZSCORE_STOP}")
    print(f"  Max spreads: {MAX_SPREADS}, {LEG_PCT*100:.0f}% per leg")
    print(f"  http://localhost:{PORT}/api/snapshot")
    print(f"  http://localhost:{PORT}/health")
    print(f"  Press Ctrl+C to stop")
    print()

    # Start scan loop in background
    threading.Thread(target=_scan_loop, daemon=True, name="ArbitrageurScan").start()

    # Start HTTP server in foreground
    server = ThreadedServer(("0.0.0.0", PORT), ArbitrageurHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Release all portfolio reservations on shutdown
        if _engine._portfolio:
            for pos in list(_engine.open_spreads):
                try:
                    if pos.reservation_a:
                        _engine._portfolio.release(pos.reservation_a, pnl=0.0)
                    if pos.reservation_b:
                        _engine._portfolio.release(pos.reservation_b, pnl=0.0)
                except Exception:
                    pass
        print(f"\n  {BOT_NAME} stopped.")


if __name__ == "__main__":
    main()
