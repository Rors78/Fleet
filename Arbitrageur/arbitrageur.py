#!/usr/bin/env python3
"""
ARBITRAGEUR v2.0 -- Correlation-Alpha Engine (Leader-Follower Catch-Up)
=========================================================================
Bot #14. Port 8085. Color: #7c4dff (deep purple).

LONG-only. Uses Brainiac correlation data to detect when a leading pair moves
and the correlated laggard hasn't caught up yet. Buys the laggard expecting
a catch-up move. Single-leg. P/L is GROSS price movement — the fleet is a
signal product; subscribers pay their own exchanges' fees.

v1 was spread trading (SHORT one leg, LONG the other) — incompatible with
LONG-only mode, twice the legs per round trip, and 0% win rate on 4 closed
trades.

Signal: leader gained > 2% in 4h while laggard gained < 0.5% → BUY laggard
Exit:   TP at 50% gap closure, SL at 1.5x ATR, time stop 12h

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
    import fleet_config as _fc
except ImportError:
    _fc = None
# LIMIT ONLY (fleet policy). Fallback keeps the 15bps cross rather than
# vanishing — an unpriced order is refused by the client, which is the correct
# failure, but losing the helper entirely would break every entry and exit.
try:
    from limit_order import limit_price as _limit_price
except ImportError:
    def _limit_price(ref, direction, cross_pct=None):
        try:
            ref = float(ref)
        except (TypeError, ValueError):
            return 0.0
        if ref <= 0:
            return 0.0
        pct = 0.0015 if cross_pct is None else float(cross_pct)
        return ref * (1.0 + pct) if (direction or "").upper() in ("LONG", "BUY") else ref * (1.0 - pct)
try:
    from kraken_client import KrakenSpotClient as _KrakenSpotClient
except ImportError:
    _KrakenSpotClient = None
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

# v2 Correlation-Alpha config
SCAN_INTERVAL = 120           # seconds between scans
CORR_THRESHOLD = 0.85         # raised from 0.80 — need strong correlation for catch-up thesis
CATCH_UP_THRESHOLD = 0.020    # 2% return gap minimum to trigger entry
CATCH_UP_LOOKBACK_H = 4       # hours to measure the gap
MIN_LEADER_RETURN = 0.015     # leader must have gained at least 1.5%
MAX_LAGGARD_RETURN = 0.005    # laggard must not have already caught up > 0.5%
FEE_RATE = 0.0                # signal product — subscribers pay their own exchange's fees; P/L is gross
MIN_GAP_PCT = 0.016           # gross gap floor for entries (same 1.6% threshold the old fee-derived gate enforced)
LOOKBACK = 60                 # candle window for correlation and z-score computation
TRADE_SIZE_PCT = 0.05         # 5% of equity per trade (single leg)
MAX_POSITIONS = 3             # concurrent positions
SL_ATR_MULT = 1.5             # stop loss = 1.5x ATR
TP_CATCHUP_PCT = 0.50         # target 50% gap closure as TP
TIME_STOP_HOURS = 12          # 12h (was 48h — too generous)
INITIAL_EQUITY = 10_000.0     # paper balance

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
# Position — v2 single-leg LONG
# ---------------------------------------------------------------------------

class Position:
    """Single-leg LONG position on the laggard pair."""

    def __init__(self, pair, entry_price, size_usd, stop_loss, take_profit,
                 catch_up_score, leader_pair, gap_pct, reservation_id=None):
        self.id = f"ca-{pair.replace('/', '')}-{int(time.time())}"
        self.pair = pair
        self.direction = "LONG"
        self.leader_pair = leader_pair
        self.entry_price = entry_price
        self.size_usd = size_usd
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.catch_up_score = catch_up_score
        self.gap_pct = gap_pct
        self.entry_time = time.time()
        self.reservation_id = reservation_id
        self.tp_hit = False

    def age_hours(self):
        return (time.time() - self.entry_time) / 3600

    def unrealized_pnl(self, current_price):
        if self.entry_price <= 0:
            return 0.0
        return self.size_usd * (current_price - self.entry_price) / self.entry_price

    def to_dict(self):
        return {
            "id": self.id,
            "pair": self.pair,
            "direction": self.direction,
            "leader_pair": self.leader_pair,
            "entry_price": round(self.entry_price, 6),
            "size_usd": round(self.size_usd, 2),
            "stop_loss": round(self.stop_loss, 6),
            "take_profit": round(self.take_profit, 6),
            "catch_up_score": round(self.catch_up_score, 4),
            "gap_pct": round(self.gap_pct, 4),
            "age_hours": round(self.age_hours(), 2),
            "reservation_id": self.reservation_id,
        }


# ---------------------------------------------------------------------------
# Arbitrageur Engine
# ---------------------------------------------------------------------------

class ArbitrageurEngine:
    """v2 Correlation-Alpha: leader-follower catch-up, LONG only."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.time()
        self.scan_count = 0
        self.scan_duration = 0.0

        # Paper balance
        self.equity = INITIAL_EQUITY
        self.realized_pnl = 0.0

        # v2: single-leg positions
        self.open_positions: list[Position] = []
        self.closed_trades: list[dict] = []   # last 50 closed

        # Market state
        self.all_correlations: dict[str, float] = {}  # "A|B" -> corr
        self.price_cache: dict[str, list[float]] = {}  # pair -> [close prices]
        self.return_cache: dict[str, float] = {}       # pair -> 4h return
        self.opportunities: list[dict] = []            # current catch-up candidates
        self.tracked_pairs: dict = {}                    # pair -> z-score tracking data

        # Log buffer for snapshot (must be before _load_positions which calls _log)
        self._log_buf = deque(maxlen=30)

        self._positions_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arbitrageur_state.json")

        # Fleet integration
        self._bus = BusListener() if BusListener else None
        self._portfolio = (PortfolioClient(CC_URL, "arbitrageur")
                           if PortfolioClient else None)
        self._publisher = (EventPublisher(CC_URL, "arbitrageur")
                           if EventPublisher else None)

        # Position persistence — MUST come after _portfolio exists: the v1
        # migration path releases old spread reservations through it, and
        # loading before it was created crashed the load on every boot (the
        # migration never completed, so the state file re-migrated forever).
        self._load_positions()

        # Live Kraken spot execution
        self._kraken = None
        if _KrakenSpotClient and _fc and _fc.is_live():
            self._kraken = _KrakenSpotClient()
            if self._kraken.has_credentials:
                self._log("LIVE MODE: Kraken spot client initialized")
            else:
                self._log("LIVE MODE: No Kraken API credentials — paper fallback")
                self._kraken = None

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
        """On startup, release stale old-format spread reservations and migrate."""
        if not self._portfolio:
            return
        # Release any old spread reservations that might be lingering
        reservations = self._portfolio.get_reservations()
        if not reservations:
            return
        for pos in list(self.open_positions):
            if pos.reservation_id and pos.reservation_id not in reservations:
                self._log(f"Stale reservation for {pos.id} — closing")
                self._close_position(pos, "stale_reservation", pos.entry_price)

    # -----------------------------------------------------------------------
    # Position Persistence — v2 single-leg format
    # -----------------------------------------------------------------------

    def _load_positions(self):
        """Load positions from disk. Handles v1→v2 migration.

        Sets self._state_unreadable when the file EXISTS but cannot be
        parsed. Absent, empty and corrupt all used to produce the same empty
        open_positions, and the scan loop's lease heartbeat then declares an
        EMPTY reservation list to the pool — which Command Center's confirm()
        treats as "this bot holds nothing" and sweeps every reservation
        booked to it, while the positions remain open with no capital behind
        them. Mirrors TurtleSue's _load_positions.

        Unreadable therefore fails toward ARMED: the heartbeat declines to
        declare, new entries are blocked, and the file is preserved.
        """
        self._state_unreadable = False
        if not os.path.exists(self._positions_file):
            return
        try:
            with open(self._positions_file, "r") as f:
                data = json.load(f)
            # v1 migration: old format has "open_spreads" with pair_a/pair_b
            if "open_spreads" in data and data["open_spreads"]:
                self._log(f"Migrating {len(data['open_spreads'])} v1 spread positions → releasing")
                if self._portfolio:
                    for pdata in data["open_spreads"]:
                        for rid_key in ["reservation_a", "reservation_b"]:
                            rid = pdata.get(rid_key)
                            if rid:
                                try:
                                    self._portfolio.release(rid, pnl=0.0)
                                except Exception:
                                    pass
                # Clear v1 state
                data = {"positions": [], "saved_at": time.time()}
                tmp = self._positions_file + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, self._positions_file)
                self._log("v1 migration complete — state cleared")
                return

            # v2 format
            loaded = 0
            for pdata in data.get("positions", []):
                pos = Position(
                    pair=pdata["pair"],
                    entry_price=pdata["entry_price"],
                    size_usd=pdata["size_usd"],
                    stop_loss=pdata["stop_loss"],
                    take_profit=pdata["take_profit"],
                    catch_up_score=pdata.get("catch_up_score", 0),
                    leader_pair=pdata.get("leader_pair", ""),
                    gap_pct=pdata.get("gap_pct", 0),
                    reservation_id=pdata.get("reservation_id"),
                )
                pos.id = pdata.get("id", pos.id)
                pos.entry_time = pdata.get("entry_time", time.time())
                self.open_positions.append(pos)
                loaded += 1
            if loaded > 0:
                self._log(f"Restored {loaded} position(s) from disk")
        except Exception as e:
            # The file exists and could not be read. That is NOT "no
            # positions" — treat it as unknown state, loudly.
            self._state_unreadable = True
            self.open_positions = []
            log.error(
                "UNREADABLE POSITION STATE %s: %s — treating open positions "
                "as UNKNOWN. Lease heartbeat will not declare, new entries "
                "blocked, file preserved for diagnosis.",
                self._positions_file, e)
            try:
                _q = "%s.corrupt_%d" % (self._positions_file, int(time.time()))
                os.replace(self._positions_file, _q)
                log.error("Preserved unreadable state as %s", _q)
            except Exception:
                log.error("Could not quarantine %s", self._positions_file,
                          exc_info=True)

    def _save_positions(self):
        """Atomic save of positions to disk."""
        try:
            data = {
                "positions": [
                    {
                        "id": p.id,
                        "pair": p.pair,
                        "direction": p.direction,
                        "leader_pair": p.leader_pair,
                        "entry_price": p.entry_price,
                        "size_usd": p.size_usd,
                        "stop_loss": p.stop_loss,
                        "take_profit": p.take_profit,
                        "catch_up_score": p.catch_up_score,
                        "gap_pct": p.gap_pct,
                        "entry_time": p.entry_time,
                        "reservation_id": p.reservation_id,
                    }
                    for p in self.open_positions
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
    # Position Management — v2 Leader-Follower
    # -----------------------------------------------------------------------

    def _compute_relative_returns(self, pairs):
        """Compute short-term returns for all pairs from cached OHLC."""
        self.return_cache.clear()
        for pair in pairs:
            closes = self.price_cache.get(pair)
            if not closes or len(closes) < CATCH_UP_LOOKBACK_H + 1:
                continue
            lookback = min(CATCH_UP_LOOKBACK_H, len(closes) - 1)
            old_price = closes[-(lookback + 1)]
            new_price = closes[-1]
            if old_price > 0:
                self.return_cache[pair] = (new_price - old_price) / old_price

    def _find_catch_up_opportunities(self):
        """Find leader-laggard pairs where laggard hasn't caught up."""
        self.opportunities = []
        for key, corr in self.all_correlations.items():
            if abs(corr) < CORR_THRESHOLD:
                continue
            parts = key.split("|")
            if len(parts) != 2:
                continue
            pair_a, pair_b = parts

            ret_a = self.return_cache.get(pair_a)
            ret_b = self.return_cache.get(pair_b)
            if ret_a is None or ret_b is None:
                continue

            # Check both directions: A leads B, or B leads A
            for leader, laggard, l_ret, lag_ret in [
                (pair_a, pair_b, ret_a, ret_b),
                (pair_b, pair_a, ret_b, ret_a),
            ]:
                gap = l_ret - lag_ret
                if gap < CATCH_UP_THRESHOLD:
                    continue
                if l_ret < MIN_LEADER_RETURN:
                    continue  # leader didn't move enough — could be noise
                if lag_ret > MAX_LAGGARD_RETURN:
                    continue  # laggard already catching up

                # Blacklist check (was a NameError no-op for months —
                # `_is_blacklisted` was never imported; the bare except ate it)
                try:
                    if _fc and _fc.is_blacklisted(laggard):
                        continue
                except Exception:
                    pass

                # PHITEX check
                if self._phitex_blocked(laggard):
                    continue

                # Already in this pair?
                if any(p.pair == laggard for p in self.open_positions):
                    continue

                self.opportunities.append({
                    "leader": leader,
                    "laggard": laggard,
                    "leader_return": round(l_ret, 4),
                    "laggard_return": round(lag_ret, 4),
                    "gap_pct": round(gap, 4),
                    "correlation": round(corr, 3),
                })

        # Sort by gap size (biggest opportunity first)
        self.opportunities.sort(key=lambda x: -x["gap_pct"])

    def _open_position(self, opp):
        """Open a LONG position on the laggard. Single leg."""
        # Unknown state fails toward ARMED: with positions unreadable this
        # bot cannot tell "flat" from "holding positions it forgot", and
        # opening more would double-commit capital the pool still books.
        if getattr(self, "_state_unreadable", False):
            return
        if len(self.open_positions) >= MAX_POSITIONS:
            return

        pair = opp["laggard"]
        leader = opp["leader"]
        gap = opp["gap_pct"]
        price = self.price_cache.get(pair, [0])[-1] if pair in self.price_cache else 0
        if price <= 0:
            return

        # Size: 5% of equity
        size_usd = self.equity * TRADE_SIZE_PCT
        if size_usd < 100:
            self._log(f"SKIP {pair}: insufficient equity (${self.equity:.2f})")
            return

        # CHRONOS: temporal bias — soft influence only, never a hard block.
        # Arbitrageur is LONG-only, so "opposing" means a fresh bearish
        # TIME_ANOMALY. A fresh (<1h) statistically-gated anomaly opposing
        # this entry shaves 25% off size; agreement is log-only (stay
        # conservative). SESSION_OVERLAP fresh (<15m) is a context log line
        # only, no size change.
        if self._bus:
            try:
                temporal = self._bus.chronos_temporal(max_age=3600)
                anomaly = temporal.get("time_anomaly") if temporal else None
                if anomaly:
                    if anomaly.get("direction") == "bearish":
                        size_usd *= 0.75
                        self._log(
                            f"TEMPORAL OPPOSE {pair}: TIME_ANOMALY bearish "
                            f"(n={anomaly.get('n')}, bias={anomaly.get('bias_pct')}%) opposes "
                            f"LONG — size x0.75"
                        )
                    else:
                        self._log(
                            f"TEMPORAL AGREE {pair}: TIME_ANOMALY bullish "
                            f"(n={anomaly.get('n')}, bias={anomaly.get('bias_pct')}%) agrees with "
                            f"LONG — no size change"
                        )
                for se in (temporal.get("session_events") or []) if temporal else []:
                    if se.get("type") == "SESSION_OVERLAP" and (time.time() - se.get("ts", 0)) < 900:
                        self._log(f"TEMPORAL CONTEXT {pair}: SESSION_OVERLAP {se.get('window')} — high volatility window")
                        break
            except Exception as e:
                self._log(f"Temporal bias check failed: {e}")

        # Gross gap gate: gap must clear the minimum gross floor.
        # Threshold unchanged from the old fee-derived gate (4x 0.40% = 1.6%),
        # now a fixed gross constant — no fee term.
        if gap < MIN_GAP_PCT:
            self._log(f"SKIP {pair}: gap {gap:.2%} < gross floor {MIN_GAP_PCT:.2%}")
            return

        # ATR for stop loss
        closes = self.price_cache.get(pair, [])
        if len(closes) < 15:
            return
        # Simple ATR estimate from closes
        diffs = [abs(closes[i] - closes[i-1]) for i in range(max(1, len(closes)-14), len(closes))]
        atr = sum(diffs) / len(diffs) if diffs else price * 0.02

        stop_loss = price - SL_ATR_MULT * atr
        take_profit = price * (1 + gap * TP_CATCHUP_PCT)  # target 50% gap closure

        # Portfolio reservation (LONG only)
        rid = None
        if self._portfolio:
            ok, result = self._portfolio.reserve(pair, "LONG", size_usd)
            if ok:
                rid = result
            else:
                self._log(f"Portfolio denied {pair}: {result}")
                return

        # Live execution: buy on Kraken spot
        entry_price = price
        if self._kraken:
            _kp = pair.replace("/", "")
            qty = size_usd / price
            # LIMIT ONLY (fleet policy) — marketable limit, never market.
            ok, txid = self._kraken.buy(_kp, qty, price=_limit_price(price, "BUY"))
            if ok:
                time.sleep(1.5)
                entry_price = self._kraken.get_fill_price(txid, price)
                self._log(f"LIVE BUY {pair} qty={qty:.6f} @ {entry_price:.4f} txid={txid}")
            else:
                self._log(f"LIVE BUY FAILED {pair}: {txid}")
                if self._portfolio and rid:
                    self._portfolio.release(rid, pnl=0.0)
                return

        pos = Position(
            pair=pair, entry_price=entry_price, size_usd=size_usd,
            stop_loss=stop_loss, take_profit=take_profit,
            catch_up_score=gap, leader_pair=leader, gap_pct=gap,
            reservation_id=rid,
        )
        self.open_positions.append(pos)
        self._save_positions()
        self._log(
            f"OPEN {pos.id} | {pair} (leader: {leader}) | gap={gap:.2%} | "
            f"@ {entry_price:.4f} | SL={stop_loss:.4f} TP={take_profit:.4f} | ${size_usd:.0f}"
        )

        if self._publisher:
            self._publisher.emit("TRADE_OPEN", {
                "pair": pair, "direction": "LONG",
                "entry": round(entry_price, 4), "size": round(size_usd, 2),
                "leader": leader, "gap_pct": round(gap, 4),
            })
            self._publisher.emit("CATCH_UP_SIGNAL", {
                "leader": leader, "laggard": pair,
                "gap_pct": round(gap, 4), "correlation": opp.get("correlation", 0),
            })

    def _check_exits(self):
        """Check open positions for exit conditions."""
        to_close = []
        for pos in self.open_positions:
            closes = self.price_cache.get(pos.pair, [])
            current_price = closes[-1] if closes else pos.entry_price
            if current_price <= 0:
                continue

            reason = None
            if current_price <= pos.stop_loss:
                reason = "STOP_LOSS"
            elif current_price >= pos.take_profit:
                reason = "TAKE_PROFIT"
            elif pos.age_hours() >= TIME_STOP_HOURS:
                reason = "TIME_STOP"

            # Trailing stop after 50% of TP reached
            if reason is None and not pos.tp_hit:
                tp_dist = pos.take_profit - pos.entry_price
                if tp_dist > 0:
                    progress = (current_price - pos.entry_price) / tp_dist
                    if progress >= 0.5:
                        pos.tp_hit = True
                        pos.stop_loss = max(pos.stop_loss, pos.entry_price)  # breakeven

            if reason:
                to_close.append((pos, reason, current_price))

        for pos, reason, price in to_close:
            self._close_position(pos, reason, price)

    def _close_position(self, pos, reason, current_price):
        """Close a LONG position and update state."""
        # Live execution: sell on Kraken
        exit_price = current_price
        if self._kraken:
            _kp = pos.pair.replace("/", "")
            qty = pos.size_usd / pos.entry_price
            # LIMIT ONLY (fleet policy) — marketable limit, never market.
            ok, txid = self._kraken.sell(
                _kp, qty, price=_limit_price(current_price, "SELL"))
            if ok:
                time.sleep(1.5)
                exit_price = self._kraken.get_fill_price(txid, current_price)
                self._log(f"LIVE SELL {pos.pair} @ {exit_price:.4f} reason={reason} txid={txid}")
            else:
                self._log(f"LIVE SELL FAILED {pos.pair}: {txid}")

        # P&L is gross price movement x size — signal product, no fee deduction
        # (subscribers pay their own exchange's fees)
        pnl = pos.unrealized_pnl(exit_price)

        self.open_positions = [p for p in self.open_positions if p.id != pos.id]
        self._save_positions()
        self.realized_pnl += pnl
        self.equity += pnl

        if _expectancy:
            try:
                _expectancy.record_trade(
                    bot_id='arbitrageur', pair=pos.pair, direction='LONG',
                    entry_price=pos.entry_price, exit_price=exit_price,
                    size_usd=pos.size_usd, duration=pos.age_hours() * 3600,
                    fee_rate=0.0,  # gross expectancy — signal product, no fee accounting
                )
            except Exception:
                pass

        # "fees" key kept at 0.0 for record-shape compatibility (P/L is gross)
        record = {**pos.to_dict(), "exit_reason": reason, "exit_price": round(exit_price, 6),
                  "pnl": round(pnl, 2), "fees": 0.0, "exit_time": time.time()}
        self.closed_trades.append(record)
        if len(self.closed_trades) > 50:
            self.closed_trades = self.closed_trades[-50:]

        self._log(f"CLOSE {pos.id} | {pos.pair} | {reason} | PnL: ${pnl:+.2f} (gross) | held {pos.age_hours():.1f}h")

        if self._portfolio and pos.reservation_id:
            self._portfolio.release(
                pos.reservation_id, pnl=pnl,
                entry_price=float(pos.entry_price),
                exit_price=float(exit_price),
            )

        if self._publisher:
            self._publisher.emit("TRADE_CLOSE", {
                "pair": pos.pair, "direction": "LONG", "pnl": round(pnl, 2),
                "exit_reason": reason, "duration_s": round(pos.age_hours() * 3600, 0),
                "leader": pos.leader_pair, "gap_pct": round(pos.gap_pct, 4),
            })

    # -----------------------------------------------------------------------
    # Main Scan Cycle — v2 Leader-Follower
    # -----------------------------------------------------------------------

    def scan(self):
        """One full scan cycle: correlations → returns → catch-up opportunities → trade."""
        t0 = time.time()
        self.scan_count += 1
        self._log(f"--- Scan #{self.scan_count} ---")

        # Lease heartbeat: declare held reservation ids so the pool can sweep
        # anything a wiring bug stranded (never raises).
        # An empty declaration from a bot whose state is UNKNOWN is not a
        # declaration that it holds nothing — CC's confirm() would sweep
        # every reservation booked here. Stay silent instead; the pool's
        # own 48h position-aware sweep still covers genuine orphans.
        if self._portfolio and not getattr(self, "_state_unreadable", False):
            self._portfolio.confirm_reservations(
                [p.reservation_id for p in self.open_positions if p.reservation_id])

        # 1. Get universe
        pairs = self._fetch_universe()
        self._log(f"Universe: {len(pairs)} pairs")

        # 2. Build correlation map (fetches OHLC as needed)
        self._build_correlation_map(pairs)
        high_corr = sum(1 for v in self.all_correlations.values()
                        if abs(v) >= CORR_THRESHOLD)
        self._log(f"Correlations: {len(self.all_correlations)} total, "
                  f"{high_corr} above {CORR_THRESHOLD}")

        # 3. Compute relative returns
        self._compute_relative_returns(pairs)

        # 4. Find catch-up opportunities
        self._find_catch_up_opportunities()
        self._log(f"Catch-up opportunities: {len(self.opportunities)}")

        # 5. Check exits on open positions
        self._check_exits()

        # 6. Open positions on best opportunities
        for opp in self.opportunities[:3]:  # top 3 only
            self._open_position(opp)

        self.scan_duration = time.time() - t0
        self._log(
            f"Scan complete in {self.scan_duration:.1f}s | "
            f"Open: {len(self.open_positions)} | "
            f"Equity: ${self.equity:,.2f} | "
            f"PnL: ${self.realized_pnl:+,.2f}"
        )

    # -----------------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------------

    def _win_rate(self):
        """Win rate from closed trades."""
        if not self.closed_trades:
            return 0.0
        wins = sum(1 for t in self.closed_trades if t.get("pnl", 0) > 0)
        return round(wins / len(self.closed_trades) * 100, 1)

    # -----------------------------------------------------------------------
    # Snapshot (for HTTP API)
    # -----------------------------------------------------------------------

    def snapshot(self):
        """Full bot state for /api/snapshot."""
        with self._lock:
            positions = {}
            for pos in self.open_positions:
                closes = self.price_cache.get(pos.pair, [])
                cur = closes[-1] if closes else pos.entry_price
                positions[pos.id] = {
                    **pos.to_dict(),
                    "current_price": round(cur, 6),
                    "unrealized_pnl": round(pos.unrealized_pnl(cur), 2),
                }

            result = {
                "bot_name": BOT_NAME,
                "status": "SCANNING" if self.scan_count > 0 else "STARTING",
                "accent": ACCENT,
                "port": PORT,
                "scan_count": self.scan_count,
                "scan_interval": SCAN_INTERVAL,
                "equity": round(self.equity, 2),
                "initial_equity": INITIAL_EQUITY,
                "open_positions": len(self.open_positions),
                "total_trades": len(self.closed_trades),
                "win_rate": self._win_rate(),
                "pnl": round(self.realized_pnl, 2),
                "pnl_pct": round(self.realized_pnl / INITIAL_EQUITY * 100, 2),
                "correlation_pairs": len(self.all_correlations),
                "high_corr_pairs": sum(1 for v in self.all_correlations.values() if v >= CORR_THRESHOLD),
                "opportunities": self.opportunities[:5],
                "positions": positions,
                "closed_recent": self.closed_trades[-10:],
                "strategy": {
                    "name": "Correlation-Alpha (Leader-Follower Catch-Up)",
                    "type": "LONG_only",
                    "corr_threshold": CORR_THRESHOLD,
                    "catch_up_threshold": CATCH_UP_THRESHOLD,
                    "lookback_hours": CATCH_UP_LOOKBACK_H,
                    "time_stop_hours": TIME_STOP_HOURS,
                    "max_positions": MAX_POSITIONS,
                    # Keys kept for snapshot-shape compatibility; zeroed —
                    # signal product, P/L is gross of subscriber exchange fees.
                    "fee_rate": FEE_RATE,
                    "round_trip_fee_pct": FEE_RATE * 2 * 100,
                    "min_gap_gross_pct": MIN_GAP_PCT * 100,
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
            try:
                data = json.dumps(_engine.snapshot(), default=str)
            except Exception as e:
                import traceback
                log.error(f"snapshot() crashed: {e}\n{traceback.format_exc()}")
                data = json.dumps({"error": str(e), "traceback": traceback.format_exc()})
            self._respond(200, data)
        elif path == "/health":
            data = json.dumps({
                "status": "ok",
                "bot": BOT_NAME,
                "port": PORT,
                "scan_count": _engine.scan_count,
                "equity": round(_engine.equity, 2),
                "open_positions": len(_engine.open_positions),
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
    print("  ARBITRAGEUR v2.0 -- Correlation-Alpha (Leader-Follower Catch-Up)")
    print(f"  Port: {PORT}")
    print(f"  Accent: {ACCENT}")
    print(f"  Equity: ${INITIAL_EQUITY:,.0f} (paper)")
    print(f"  Scan interval: {SCAN_INTERVAL}s")
    print(f"  Correlation threshold: {CORR_THRESHOLD}")
    print(f"  Catch-up gap: {CATCH_UP_THRESHOLD:.1%} min, {TP_CATCHUP_PCT:.0%} TP target")
    print(f"  Max positions: {MAX_POSITIONS}, {TRADE_SIZE_PCT*100:.0f}% per trade, LONG only")
    print(f"  http://localhost:{PORT}/api/snapshot")
    print(f"  http://localhost:{PORT}/health")
    print("  Press Ctrl+C to stop")
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
            for pos in list(_engine.open_positions):
                try:
                    # v2 positions carry a single reservation_id (the old
                    # reservation_a/b attrs were v1 spread legs — releasing
                    # them here was a silent AttributeError, leaking capital
                    # on every manual shutdown)
                    if pos.reservation_id:
                        _engine._portfolio.release(pos.reservation_id, pnl=0.0)
                except Exception:
                    pass
        print(f"\n  {BOT_NAME} stopped.")


if __name__ == "__main__":
    main()
