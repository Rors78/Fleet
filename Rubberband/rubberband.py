#!/usr/bin/env python3
"""
Rubberband v2.0 -- Trend-Aligned Mean Reversion (long + short)
==============================================================
Bollinger Band + RSI mean reversion bot.
Paper trading on Kraken pairs via Command Center market data.

Strategy (v2 — trend-aligned mean reversion, both directions):
    LONG:  60m uptrend (price > EMA50)  AND  price <= lower BB  AND  RSI < 40
    SHORT: 60m downtrend (price < EMA50) AND  price >= upper BB  AND  RSI > 60
    TP1 = middle BB (mean), TP2 = opposite band, SL = 2.0x ATR(60m)
    On TP1 hit: move SL to breakeven

Port 8083 | Accent #00e5ff

Usage:
    python rubberband.py              # interactive
    python rubberband.py --auto       # headless (fleet launcher)
"""

import argparse
import json
import logging
import os
import signal as signal_mod
import socketserver
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Fleet integration (graceful degradation)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.environ.get("CC_DIR", str(Path(__file__).resolve().parent.parent / "CommandCenter")))

try:
    from portfolio_client import PortfolioClient
except ImportError:
    PortfolioClient = None

try:
    from event_publisher import EventPublisher
except ImportError:
    EventPublisher = None

try:
    from bus_listener import BusListener
except ImportError:
    BusListener = None
try:
    from expectancy import ExpectancyTracker
    _expectancy = ExpectancyTracker()
except Exception:
    _expectancy = None

try:
    from fleet_config import is_blacklisted as _is_blacklisted
except ImportError:
    _is_blacklisted = lambda pair: False
try:
    import fleet_config as _fc
except ImportError:
    _fc = None
# LIMIT ONLY (fleet policy). Fallback keeps the 15bps cross rather than
# returning None — an unpriced order is refused by the client, which is the
# correct failure, but losing the helper entirely would break every exit.
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

from indicators import calc_ema, calc_bollinger, calc_rsi, calc_atr, calc_adx

# ---------------------------------------------------------------------------
# External deps
# ---------------------------------------------------------------------------
try:
    import requests
except ImportError:
    print("FATAL: 'requests' package required.  pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants — v2.0 Trend-Aligned Dip Buyer
# ---------------------------------------------------------------------------
VERSION = "2.0"
BOT_NAME = "Rubberband"
ACCENT = "#00e5ff"
PORT = 8083
COMMAND_CENTER_URL = "http://127.0.0.1:9000"

# Strategy v2: fade extremes in the direction of the 60m trend.
# v1 fought the trend (ADX<18 = no trend). v2 requires the trend (ADX>20 + DI+>DI-).
# The "rubberband snap" = price stretches to a band against the trend, then reverts to mean.
# LONG: buy the dip to lower BB in an uptrend. SHORT: fade the rip to upper BB in a
# downtrend (enabled 2026-07 after the fleet-wide short ban lifted — FLEET_LONG_ONLY=False).
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
ADX_PERIOD = 14
ATR_PERIOD = 14

# Timeframe: 15m primary (was 5m — too noisy), 60m for trend confirmation
OHLC_INTERVAL = 15           # 15-minute candles
OHLC_LIMIT = 100             # ~25 hours of 15m data
HTF_INTERVAL = 60            # Higher timeframe for trend
HTF_LIMIT = 100              # ~4 days of 1h data

# Entry: INVERTED from v1 — require trend, fade the counter-trend extreme
ADX_TREND_MIN = 20           # ADX > 20 = trend exists (v1 required < 18)
RSI_DIP_THRESHOLD = 40       # RSI < 40 = dip (v1 required < 30 — too extreme)
RSI_DEEP_DIP = 30            # RSI < 30 = deep dip (size boost)
RSI_RIP_THRESHOLD = 60       # RSI > 60 = rip (SHORT mirror of RSI_DIP_THRESHOLD: 100-40)
RSI_DEEP_PUMP = 70           # RSI > 70 = deep pump (SHORT mirror of RSI_DEEP_DIP: 100-30)

# Exit
SL_ATR_MULT = 2.0            # 2.0 ATR below entry on 60m (v1: 1.5 on 5m — too tight)
TRAILING_ATR_MULT = 1.5      # After TP1: trail by 1.5 ATR
MIN_RR_RATIO = 2.0           # Minimum GROSS reward:risk (no fee term — see below)

# Fees: retired 2026-07-30. The fleet is a signal product — it never trades
# real money, and subscribers pay their own exchange's fees. All P/L and
# gating in this file is GROSS price movement. (KRAKEN_FEE_RATE removed.)

# Risk
MAX_POSITIONS = 1             # Start conservative — scale to 3 after 20 profitable trades
TRADE_RISK_PCT = 0.05         # 5% per trade
PAPER_BALANCE = 10_000.0
SCAN_INTERVAL = 60
UNIVERSE_LIMIT = 15

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE = Path(__file__).parent / "rubberband.log"
logger = logging.getLogger("rubberband")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)
_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_ch)


# ============================================================================
# Position data class
# ============================================================================

class Position:
    """Represents an open paper position."""

    def __init__(self, pair: str, direction: str, entry_price: float,
                 size_usd: float, stop_loss: float, tp1: float, tp2: float,
                 reservation_id: Optional[str] = None):
        self.id = f"{pair}-{direction}-{int(time.time()*1000)}"
        self.pair = pair
        self.direction = direction          # "LONG" or "SHORT"
        self.entry_price = entry_price
        self.size_usd = size_usd
        self.stop_loss = stop_loss
        self.original_sl = stop_loss
        self.tp1 = tp1
        self.tp2 = tp2
        self.tp1_hit = False
        self._tp1_logged = False
        self.opened_at = time.time()
        self.reservation_id = reservation_id

    def unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized P&L in USD."""
        if self.entry_price == 0:
            return 0.0
        qty = self.size_usd / self.entry_price
        if self.direction == "LONG":
            return qty * (current_price - self.entry_price)
        else:
            return qty * (self.entry_price - current_price)

    def check_exit(self, current_price: float) -> Optional[str]:
        """
        Check if position should be closed.
        Returns exit reason or None.
        Also handles TP1 -> breakeven SL move.
        """
        if self.direction == "LONG":
            # Stop loss
            if current_price <= self.stop_loss:
                return "STOP_LOSS"
            # TP1 check (move SL to breakeven)
            if not self.tp1_hit and current_price >= self.tp1:
                self.tp1_hit = True
                self.stop_loss = self.entry_price  # breakeven
            # TP2 (full take profit)
            if current_price >= self.tp2:
                return "TAKE_PROFIT"
        else:
            # SHORT direction
            if current_price >= self.stop_loss:
                return "STOP_LOSS"
            if not self.tp1_hit and current_price <= self.tp1:
                self.tp1_hit = True
                self.stop_loss = self.entry_price  # breakeven
            if current_price <= self.tp2:
                return "TAKE_PROFIT"
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pair": self.pair,
            "direction": self.direction,
            "entry_price": round(self.entry_price, 6),
            "size_usd": round(self.size_usd, 2),
            "stop_loss": round(self.stop_loss, 6),
            "original_sl": round(self.original_sl, 6),
            "tp1": round(self.tp1, 6),
            "tp2": round(self.tp2, 6),
            "tp1_hit": self.tp1_hit,
            "opened_at": self.opened_at,
            "age_s": round(time.time() - self.opened_at),
            "reservation_id": self.reservation_id,
        }


# ============================================================================
# Trade record
# ============================================================================

class TradeRecord:
    """Completed trade for history."""

    def __init__(self, pair: str, direction: str, entry_price: float,
                 exit_price: float, size_usd: float, pnl: float,
                 exit_reason: str, duration_s: float):
        self.pair = pair
        self.direction = direction
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.size_usd = size_usd
        self.pnl = pnl
        self.exit_reason = exit_reason
        self.duration_s = duration_s
        self.closed_at = time.time()

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "direction": self.direction,
            "entry_price": round(self.entry_price, 6),
            "exit_price": round(self.exit_price, 6),
            "size_usd": round(self.size_usd, 2),
            "pnl": round(self.pnl, 2),
            "exit_reason": self.exit_reason,
            "duration_s": round(self.duration_s),
            "closed_at": self.closed_at,
        }


# ============================================================================
# Rubberband Engine
# ============================================================================

class RubberbandEngine:
    """Core mean reversion trading engine."""

    def __init__(self, auto: bool = False):
        self.auto = auto
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.status = "starting"

        # Paper portfolio
        self.starting_equity = PAPER_BALANCE
        self.equity = PAPER_BALANCE
        self.peak_equity = PAPER_BALANCE
        self.equity_curve: List[dict] = []

        # Positions and trades
        self.positions: List[Position] = []
        self.trades: List[TradeRecord] = []
        self.wins = 0
        self.losses = 0

        # Market data
        self.pairs: List[str] = []
        self.prices: Dict[str, float] = {}
        self.candles: Dict[str, list] = {}       # pair -> list of [ts, o, h, l, c, vol, count]
        self.indicators: Dict[str, dict] = {}    # pair -> {bb, rsi, adx, atr}

        # Scan tracking
        self.scan_count = 0
        self.last_scan = 0.0
        self.log_buffer: deque = deque(maxlen=100)

        # Position persistence
        self._positions_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rubberband_state.json")
        self._load_positions()

        # Fleet integration
        self._portfolio_client: Optional[object] = None
        self._event_pub: Optional[object] = None
        self._bus: Optional[object] = None
        self._bus_regime: Optional[str] = None  # aegis regime cache

        # FEE_SLAYER: stop-loss cooldown — prevents re-entering a pair for 2h after a SL exit
        self._sl_cooldowns: dict = {}  # pair -> timestamp of last stop-loss exit
        self.SL_COOLDOWN_SECS = 7200  # 2 hours

        # Shutdown flag
        self.shutdown_event = threading.Event()

    # -- Portfolio Reconciliation ---------------------------------------------

    def _reconcile_positions(self):
        """On startup, verify reservations are still valid. Try to re-reserve if stale."""
        if not self._portfolio_client:
            return

        reservations = self._portfolio_client.get_reservations()
        if not reservations:
            return

        for pos in list(self.positions):
            if pos.reservation_id and pos.reservation_id not in reservations:
                # Try re-reserve. is_reentry=True: the position is already open,
                # so direction policy (e.g. the fleet-wide short ban) must not
                # apply — a refusal here closes a live position at market purely
                # because of a config change.
                ok, new_rid = self._portfolio_client.reserve(
                    pos.pair, pos.direction, pos.size_usd, is_reentry=True)
                if ok:
                    pos.reservation_id = new_rid
                    self._save_positions()
                    self._log(f"Re-reserved {pos.pair}: {new_rid}")
                else:
                    self._log(f"Stale reservation for {pos.pair} - closing", "WARNING")
                    # Get current price for closing
                    current_price = self.prices.get(pos.pair, pos.entry_price)
                    self.close_position(pos, current_price, "stale_reservation")

    # -- Logging helper -------------------------------------------------------

    def _log(self, msg: str, level: str = "INFO"):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        with self._lock:
            self.log_buffer.append(entry)
        if level == "ERROR":
            logger.error(msg)
        elif level == "WARNING":
            logger.warning(msg)
        elif level == "DEBUG":
            logger.debug(msg)
        else:
            logger.info(msg)

    # -- Position Persistence ----------------------------------------------------

    def _load_positions(self):
        """Load positions from disk on startup."""
        if not os.path.exists(self._positions_file):
            return
        try:
            with open(self._positions_file, "r") as f:
                data = json.load(f)
            loaded = 0
            for pdata in data.get("positions", []):
                # Skip records missing the fields needed to manage the position
                required = ("pair", "direction", "entry_price", "size_usd",
                            "stop_loss", "tp1", "tp2")
                if any(pdata.get(k) is None for k in required):
                    self._log(f"Skipping malformed saved position: {pdata.get('pair')}", "WARNING")
                    continue
                pos = Position(
                    pair=pdata["pair"],
                    direction=pdata["direction"],   # "LONG" or "SHORT"
                    entry_price=pdata["entry_price"],
                    size_usd=pdata["size_usd"],
                    stop_loss=pdata["stop_loss"],
                    tp1=pdata["tp1"],
                    tp2=pdata["tp2"],
                    reservation_id=pdata.get("reservation_id"),
                )
                pos.id = pdata.get("id", pos.id)
                pos.original_sl = pdata.get("original_sl", pos.stop_loss)
                pos.tp1_hit = bool(pdata.get("tp1_hit", False))
                pos.opened_at = pdata.get("opened_at", time.time())
                self.positions.append(pos)
                loaded += 1
            # Restore the safety state written above. All optional, so an
            # older state file still loads.
            _cd = data.get("sl_cooldowns")
            if isinstance(_cd, dict):
                self._sl_cooldowns = {k: float(v) for k, v in _cd.items()
                                      if isinstance(v, (int, float))}
            for _k in ("wins", "losses"):
                _v = data.get(_k)
                if isinstance(_v, int):
                    setattr(self, _k, _v)
            for _k in ("equity", "peak_equity"):
                _v = data.get(_k)
                if isinstance(_v, (int, float)):
                    setattr(self, _k, float(_v))
            if loaded > 0 or _cd:
                self._log(
                    f"Restored {loaded} position(s), "
                    f"{len(self._sl_cooldowns)} SL cooldown(s), "
                    f"{self.wins}W/{self.losses}L from disk")
        except Exception as e:
            self._log(f"Failed to load positions: {e}", "WARNING")

    def _save_positions(self):
        """Atomic save of positions to disk."""
        try:
            data = {
                "positions": [
                    {
                        "id": p.id,
                        "pair": p.pair,
                        "direction": p.direction,
                        "size_usd": p.size_usd,
                        "entry_price": p.entry_price,
                        "stop_loss": p.stop_loss,
                        "original_sl": p.original_sl,
                        "tp1": p.tp1,
                        "tp2": p.tp2,
                        "tp1_hit": p.tp1_hit,
                        "opened_at": p.opened_at,
                        "reservation_id": p.reservation_id,
                    }
                    for p in self.positions
                ],
                "saved_at": time.time(),
                # Safety state that must survive a restart. The 2-hour
                # stop-loss cooldown lived only in memory, and the gate reads
                # self._sl_cooldowns.get(pair, 0) — a missing entry is a 1970
                # timestamp, so sl_elapsed becomes ~1.7 billion seconds and the
                # cooldown always passes. A restart therefore let the bot
                # immediately re-enter the pair that had just stopped it out.
                "sl_cooldowns": dict(self._sl_cooldowns),
                # wins/losses and peak_equity reset to their initial values on
                # boot, so the win rate and drawdown restarted from zero while
                # the positions they describe were restored from this file.
                "wins": self.wins,
                "losses": self.losses,
                "equity": self.equity,
                "peak_equity": self.peak_equity,
            }
            tmp = self._positions_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._positions_file)
        except Exception as e:
            self._log(f"Failed to save positions: {e}", "WARNING")

    # -- Fleet integration init -----------------------------------------------

    def init_fleet(self):
        """Initialize portfolio client, event publisher, and bus listener."""
        if PortfolioClient:
            try:
                self._portfolio_client = PortfolioClient(COMMAND_CENTER_URL, "rubberband")
                self._log("Portfolio client initialized")
            except Exception as e:
                self._log(f"Portfolio client init failed: {e}", "WARNING")
        else:
            self._log("PortfolioClient not available (import failed)", "WARNING")

        if EventPublisher:
            try:
                self._event_pub = EventPublisher(COMMAND_CENTER_URL, "rubberband")
                self._log("Event publisher initialized")
            except Exception as e:
                self._log(f"Event publisher init failed: {e}", "WARNING")
        else:
            self._log("EventPublisher not available (import failed)", "WARNING")

        if BusListener:
            try:
                self._bus = BusListener()
                self._log("Bus listener initialized")
            except Exception as e:
                self._log(f"Bus listener init failed: {e}", "WARNING")
        else:
            self._log("BusListener not available (import failed)", "WARNING")

        # Live Kraken spot execution
        self._kraken = None
        if _KrakenSpotClient and _fc and _fc.is_live():
            self._kraken = _KrakenSpotClient()
            if self._kraken.has_credentials:
                self._log("LIVE MODE: Kraken spot client initialized")
            else:
                self._log("LIVE MODE: No Kraken API credentials — paper fallback", "WARNING")
                self._kraken = None

    # -- Universe fetch -------------------------------------------------------

    def fetch_universe(self) -> List[str]:
        """Get top N pairs from Command Center universe."""
        try:
            resp = requests.get(f"{COMMAND_CENTER_URL}/api/universe", timeout=5)
            data = resp.json()
            symbols = data.get("symbols", [])
            pairs = [p for p in symbols if not _is_blacklisted(p)][:UNIVERSE_LIMIT]
            if pairs:
                self._log(f"Universe: {len(pairs)} pairs from Command Center")
                return pairs
        except Exception as e:
            self._log(f"Universe fetch failed: {e}", "WARNING")

        # Fallback: hardcoded pairs
        fallback = [
            "BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD", "DOT/USD",
            "AVAX/USD", "LINK/USD", "POL/USD", "ATOM/USD", "UNI/USD",
            "AAVE/USD", "LTC/USD", "XLM/USD", "ALGO/USD", "NEAR/USD",
        ]
        fallback = [p for p in fallback if not _is_blacklisted(p)]
        self._log("Using fallback pair list", "WARNING")
        return fallback

    # -- OHLC fetch -----------------------------------------------------------

    def fetch_candles(self, pair: str, interval: int = OHLC_INTERVAL,
                      limit: int = OHLC_LIMIT) -> Optional[list]:
        """Fetch OHLC candles — canonical shared fetcher (CC proxy + Kraken fallback).
        Returns list of [ts, open, high, low, close, volume, count] or None.
        interval and limit default to module constants; pass overrides for
        higher-timeframe fetches (e.g. 60-min candles for the EMA50 filter).
        """
        if _fetch_ohlc_canonical:
            candles = _fetch_ohlc_canonical(pair, interval, limit, cc_url=COMMAND_CENTER_URL)
        else:
            try:
                url = (f"{COMMAND_CENTER_URL}/api/market/ohlc"
                       f"?pair={pair}&interval={interval}&limit={limit}")
                resp = requests.get(url, timeout=10)
                data = resp.json()
                candles = data.get("candles") or []
            except Exception as e:
                self._log(f"Candle fetch failed for {pair} (interval={interval}): {e}", "DEBUG")
                candles = []
        min_required = BB_PERIOD + 5 if interval == OHLC_INTERVAL else 50
        if candles and len(candles) >= min_required:
            return candles
        return None

    # -- Indicator calc on candle data ----------------------------------------

    def compute_indicators(self, pair: str, candles: list) -> Optional[dict]:
        """Compute BB, RSI, ADX, ATR from candle data.
        Candle format: [ts, open, high, low, close, volume, count]
        Returns dict or None if insufficient data.
        """
        if not candles or len(candles) < max(BB_PERIOD, RSI_PERIOD + 1, ADX_PERIOD * 2 + 1, ATR_PERIOD + 1):
            return None

        closes = [c[4] for c in candles]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]

        bb = calc_bollinger(closes, BB_PERIOD, BB_STD)
        rsi = calc_rsi(closes, RSI_PERIOD)
        adx = calc_adx(highs, lows, closes, ADX_PERIOD)
        atr = calc_atr(highs, lows, closes, ATR_PERIOD)

        if bb is None or rsi is None or atr is None:
            return None

        return {
            "bb_upper": bb[0],
            "bb_middle": bb[1],
            "bb_lower": bb[2],
            "rsi": rsi,
            "adx": adx,       # may be None if insufficient data
            "atr": atr,
            "price": closes[-1],
        }

    # -- Bus intelligence -----------------------------------------------------

    def _check_bus_vetoes(self, pair: str) -> Optional[str]:
        """Check bus for conditions that should prevent new entries.
        Returns veto reason string, or None if clear.
        """
        if not self._bus:
            return None

        # Emergency active -- no new trades
        try:
            if self._bus.emergency_active(max_age=300):
                return "EMERGENCY_ACTIVE"
        except Exception:
            pass

        # Aegis regime -- avoid opening in extreme regimes
        try:
            regime = self._bus.aegis_regime()
            if regime:
                self._bus_regime = regime
                if regime in ("CRISIS", "EXTREME_FEAR"):
                    return f"AEGIS_REGIME_{regime}"
        except Exception:
            pass

        # PhiTex critical -- pair in phase transition
        try:
            if self._bus.phitex_critical(pair, max_age=120):
                return "PHITEX_CRITICAL"
        except Exception:
            pass

        # Whale tier -- extreme whale activity
        try:
            tier = self._bus.whale_tier(pair, max_age=300)
            if tier in ("EXTREME", "HIGH"):
                return f"WHALE_TIER_{tier}"
        except Exception:
            pass

        return None

    # -- Signal evaluation v2: Trend-Aligned Mean Reversion (long + short) -----

    def evaluate_signal(self, pair: str, ind: dict) -> Optional[dict]:
        """v2: Fade counter-trend extremes in the direction of the 60m trend.
        LONG: buy oversold dips in confirmed uptrends.
        SHORT: fade overbought rips in confirmed downtrends.
        Requires: 60m trend + 15m band/RSI extreme + gross R:R gate.
        """
        # SL cooldown — skip if this pair stop-loss'd recently
        sl_elapsed = time.time() - self._sl_cooldowns.get(pair, 0)
        if sl_elapsed < self.SL_COOLDOWN_SECS:
            return None

        price = ind["price"]
        rsi = ind["rsi"]
        adx = ind["adx"]
        atr = ind["atr"]
        bb_upper = ind["bb_upper"]
        bb_middle = ind["bb_middle"]
        bb_lower = ind["bb_lower"]

        # 1. Trend filter: ADX > 20 = trend exists (v1 required < 18 — inverted)
        if adx is None:
            return None
        if adx < ADX_TREND_MIN:
            return None  # no trend — not our market

        # 2. Higher-timeframe trend confirmation (60m EMA alignment)
        candles_60m = self.fetch_candles(pair, interval=HTF_INTERVAL, limit=HTF_LIMIT)
        if candles_60m is None or len(candles_60m) < 55:
            return None
        closes_60m = [c[4] for c in candles_60m]
        highs_60m = [c[2] for c in candles_60m]
        lows_60m = [c[3] for c in candles_60m]

        ema8 = calc_ema(closes_60m, 8)
        ema21 = calc_ema(closes_60m, 21)
        ema50 = calc_ema(closes_60m, 50)
        if ema8 is None or ema21 is None or ema50 is None:
            return None

        # 60m ATR for wider, more stable stops
        atr_60m = calc_atr(highs_60m, lows_60m, closes_60m, ATR_PERIOD)
        if atr_60m is None or atr_60m <= 0:
            return None

        # 3. Trend side: price vs 60m EMA50 picks the direction we trade.
        if price >= ema50[-1]:
            # Uptrend — buy the dip (v1 required price below EMA50, which fought the trend)
            # EMA stack: 8 > 21 > 50 = strong uptrend (optional boost, not required)
            ema_stacked = ema8[-1] > ema21[-1] > ema50[-1]

            # Dip detection: price at/below lower BB AND RSI < 40
            if price > bb_lower:
                return None  # not at the band — no dip
            if rsi >= RSI_DIP_THRESHOLD:
                return None  # not oversold enough

            direction = "LONG"
            stop_loss = price - SL_ATR_MULT * atr_60m  # 2.0 ATR on 60m (wider, stable)
            tp1 = bb_middle   # mean reversion target (the snap-back)
            tp2 = bb_upper    # full extension

            # Sanity: TP must be above entry
            if tp1 <= price or tp2 <= price:
                return None

            # Deep dip boost: RSI < 30 = extra confidence
            deep_extreme = rsi < RSI_DEEP_DIP
        else:
            # Downtrend — fade the rip (mirror of the long path; shorts enabled
            # after the fleet-wide ban lifted — FLEET_LONG_ONLY = False)
            # EMA stack: 8 < 21 < 50 = strong downtrend (optional boost, not required)
            ema_stacked = ema8[-1] < ema21[-1] < ema50[-1]

            # Rip detection: price at/above upper BB AND RSI > 60
            if price < bb_upper:
                return None  # not at the band — no rip
            if rsi <= RSI_RIP_THRESHOLD:
                return None  # not overbought enough

            direction = "SHORT"
            stop_loss = price + SL_ATR_MULT * atr_60m  # 2.0 ATR on 60m (wider, stable)
            tp1 = bb_middle   # mean reversion target (the snap-back)
            tp2 = bb_lower    # full extension

            # Sanity: TP must be below entry
            if tp1 >= price or tp2 >= price:
                return None

            # Deep pump boost: RSI > 70 = extra confidence
            deep_extreme = rsi > RSI_DEEP_PUMP

        # 4. Gross R:R gate (shared by LONG and SHORT — both branches converge
        # here). What ended the 21-consecutive-loss streak was demanding 2:1
        # reward:risk before entry — that discipline stays. The Kraken fee term
        # is gone: this is a signal product, subscribers pay their own
        # exchange's fees, so the gate is measured on gross price movement.
        # abs() keeps it direction-agnostic: for SHORT, gross_reward = entry - tp1.
        risk = abs(price - stop_loss)
        gross_reward = abs(tp1 - price)
        if risk <= 0 or gross_reward <= 0 or (gross_reward / risk) < MIN_RR_RATIO:
            return None

        return {
            "pair": pair,
            "direction": direction,
            "price": price,
            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "rsi": rsi,
            "adx": adx,
            "atr": atr,
            "atr_60m": atr_60m,
            "bb_upper": bb_upper,
            "bb_middle": bb_middle,
            "bb_lower": bb_lower,
            "ema_stacked": ema_stacked,
            "deep_extreme": deep_extreme,
        }

    # -- Position management --------------------------------------------------

    def _already_in_pair(self, pair: str) -> bool:
        """Check if we already have a position in this pair."""
        with self._lock:
            return any(p.pair == pair for p in self.positions)

    def open_position(self, signal: dict) -> bool:
        """Open a new paper position from a signal."""
        pair = signal["pair"]
        direction = signal["direction"]
        price = signal["price"]

        # Max positions check
        with self._lock:
            if len(self.positions) >= MAX_POSITIONS:
                self._log(f"Max positions ({MAX_POSITIONS}) reached, skip {pair}", "DEBUG")
                return False

        # Already in this pair?
        if self._already_in_pair(pair):
            self._log(f"Already in {pair}, skip", "DEBUG")
            return False

        # Bus veto check
        veto = self._check_bus_vetoes(pair)
        if veto:
            self._log(f"Bus veto for {pair}: {veto}", "DEBUG")
            return False

        # Position sizing: 3% of equity
        with self._lock:
            size_usd = self.equity * TRADE_RISK_PCT

        # CHRONOS: temporal bias — soft influence only, never a hard block.
        # A fresh (<1h) statistically-gated TIME_ANOMALY opposing this trade's
        # direction shaves 25% off size; agreement is log-only (stay
        # conservative). SESSION_OVERLAP fresh (<15m) is a context log line
        # only, no size change.
        if self._bus:
            try:
                temporal = self._bus.chronos_temporal(max_age=3600)
                anomaly = temporal.get("time_anomaly") if temporal else None
                if anomaly:
                    anomaly_dir = "LONG" if anomaly.get("direction") == "bullish" else "SHORT"
                    if anomaly_dir != direction:
                        size_usd *= 0.75
                        self._log(
                            f"TEMPORAL OPPOSE {pair}: TIME_ANOMALY {anomaly.get('direction')} "
                            f"(n={anomaly.get('n')}, bias={anomaly.get('bias_pct')}%) opposes "
                            f"{direction} — size x0.75"
                        )
                    else:
                        self._log(
                            f"TEMPORAL AGREE {pair}: TIME_ANOMALY {anomaly.get('direction')} "
                            f"(n={anomaly.get('n')}, bias={anomaly.get('bias_pct')}%) agrees with "
                            f"{direction} — no size change"
                        )
                for se in (temporal.get("session_events") or []) if temporal else []:
                    if se.get("type") == "SESSION_OVERLAP" and (time.time() - se.get("ts", 0)) < 900:
                        self._log(f"TEMPORAL CONTEXT {pair}: SESSION_OVERLAP {se.get('window')} — high volatility window")
                        break
            except Exception as e:
                self._log(f"Temporal bias check failed: {e}", "DEBUG")

        # Portfolio reservation
        reservation_id = None
        if self._portfolio_client:
            try:
                ok, rid = self._portfolio_client.reserve(pair, direction, size_usd)
                if ok:
                    reservation_id = rid
                    self._log(f"Portfolio reserved ${size_usd:.2f} for {pair} ({rid})")
                else:
                    self._log(f"Portfolio reservation denied for {pair}: {rid}", "WARNING")
                    # Fall back to local balance (continue without reservation)
            except Exception as e:
                self._log(f"Portfolio reserve error: {e}", "WARNING")

        # Live execution: buy on Kraken spot (LONG only — no spot short execution
        # exists; SHORT positions stay paper even in live mode, close path mirrors this)
        entry_price = price
        if self._kraken and direction.upper() == "LONG":
            _kp = pair.replace("/", "")
            qty = size_usd / price
            # LIMIT ONLY (fleet policy) — marketable limit, never market.
            ok, txid = self._kraken.buy(_kp, qty, price=_limit_price(price, "BUY"))
            if ok:
                import time as _t; _t.sleep(1.5)
                entry_price = self._kraken.get_fill_price(txid, price)
                self._log(f"LIVE BUY {pair} qty={qty:.6f} @ {entry_price:.4f} txid={txid}")
            else:
                self._log(f"LIVE BUY FAILED {pair}: {txid} — aborting", "WARNING")
                if self._portfolio_client and reservation_id:
                    self._portfolio_client.release(reservation_id, pnl=0.0)
                return False

        pos = Position(
            pair=pair,
            direction=direction,
            entry_price=entry_price,
            size_usd=size_usd,
            stop_loss=signal["stop_loss"],
            tp1=signal["tp1"],
            tp2=signal["tp2"],
            reservation_id=reservation_id,
        )

        with self._lock:
            self.positions.append(pos)
            self._save_positions()

        adx_str = f"{signal['adx']:.1f}" if signal['adx'] is not None else "N/A"
        self._log(f"OPEN {direction} {pair} @ {price:.6f} | "
                  f"SL={signal['stop_loss']:.6f} TP1={signal['tp1']:.6f} TP2={signal['tp2']:.6f} | "
                  f"Size=${size_usd:.2f} RSI={signal['rsi']:.1f} ADX={adx_str}")

        # Publish event
        if self._event_pub:
            self._event_pub.emit("TRADE_OPEN", {
                "pair": pair,
                "direction": direction,
                "entry_price": round(price, 6),
                "size_usd": round(size_usd, 2),
                "stop_loss": round(signal["stop_loss"], 6),
                "tp1": round(signal["tp1"], 6),
                "tp2": round(signal["tp2"], 6),
                "rsi": round(signal["rsi"], 2),
                "adx": round(signal["adx"], 2) if signal["adx"] else None,
                "strategy": "bollinger_rsi_mean_reversion",
            })

        return True

    def close_position(self, pos: Position, current_price: float, reason: str):
        """Close a position, record trade, update equity."""
        # Live execution: sell on Kraken spot to close LONG
        exit_price = current_price
        if self._kraken and pos.direction.upper() == "LONG":
            _kp = pos.pair.replace("/", "")
            qty = pos.size_usd / pos.entry_price
            # LIMIT ONLY (fleet policy) — marketable limit, never market.
            ok, txid = self._kraken.sell(
                _kp, qty, price=_limit_price(current_price, "SELL"))
            if ok:
                import time as _t; _t.sleep(1.5)
                exit_price = self._kraken.get_fill_price(txid, current_price)
                self._log(f"LIVE SELL {pos.pair} qty={qty:.6f} @ {exit_price:.4f} reason={reason} txid={txid}")
            else:
                self._log(f"LIVE SELL FAILED {pos.pair}: {txid} — using paper price", "WARNING")
            current_price = exit_price

        # GROSS P/L: direction-aware price movement × size (unrealized_pnl
        # mirrors LONG and SHORT). No fee deduction — signal product;
        # subscribers pay their own exchange's fees.
        pnl = pos.unrealized_pnl(current_price)
        duration = time.time() - pos.opened_at

        trade = TradeRecord(
            pair=pos.pair,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=current_price,
            size_usd=pos.size_usd,
            pnl=pnl,
            exit_reason=reason,
            duration_s=duration,
        )

        if _expectancy:
            try:
                _expectancy.record_trade(
                    bot_id='rubberband',
                    pair=pos.pair,
                    direction=pos.direction,
                    entry_price=pos.entry_price,
                    exit_price=current_price,
                    size_usd=pos.size_usd,
                    duration=duration,
                    fee_rate=0.0,  # gross — signal product, no fee accounting
                )
            except Exception:
                pass

        with self._lock:
            self.equity += pnl
            if self.equity > self.peak_equity:
                self.peak_equity = self.equity
            self.trades.append(trade)
            if pnl >= 0:
                self.wins += 1
            else:
                self.losses += 1
            # Remove from open positions
            self.positions = [p for p in self.positions if p.id != pos.id]
            self._save_positions()

        self._log(f"CLOSE {pos.direction} {pos.pair} @ {current_price:.6f} | "
                  f"PnL=${pnl:+.2f} | Reason={reason} | "
                  f"Duration={duration:.0f}s | Equity=${self.equity:.2f}")

        # FEE_SLAYER: set SL cooldown on stop-loss exits — no re-entry for 2h on this pair
        if reason == "STOP_LOSS":
            self._sl_cooldowns[pos.pair] = time.time()
            self._log(f"SL cooldown set: {pos.pair} locked for 2h", "WARNING")

        # Release portfolio reservation
        if pos.reservation_id and self._portfolio_client:
            try:
                # Report outcome to signal aggregator for learning
                try:
                    import urllib.request as urlreq
                    url = f"{COMMAND_CENTER_URL}/api/signals/outcome"
                    data = json.dumps({
                        "bot_id": "rubberband",
                        "pair": pos.pair,
                        "direction": pos.direction,
                        "won": pnl > 0,
                        "pnl": float(pnl),
                        "fees": 0.0  # retired — signal product; subscribers pay their own exchange fees
                    }).encode("utf-8")
                    req = urlreq.Request(url, data=data, headers={"Content-Type": "application/json"})
                    urlreq.urlopen(req, timeout=3)
                except Exception:
                    pass
                
                self._portfolio_client.release(
                    pos.reservation_id, pnl=pnl,
                    entry_price=float(pos.entry_price),
                    exit_price=float(current_price),
                )
                self._log(f"Portfolio released {pos.reservation_id} with PnL=${pnl:+.2f}")
            except Exception as e:
                self._log(f"Portfolio release error: {e}", "WARNING")

        # Publish event
        if self._event_pub:
            self._event_pub.emit("TRADE_CLOSE", {
                "pair": pos.pair,
                "direction": pos.direction,
                "entry_price": round(pos.entry_price, 6),
                "exit_price": round(current_price, 6),
                "size_usd": round(pos.size_usd, 2),
                "pnl": round(pnl, 2),
                "exit_reason": reason,
                "duration_s": round(duration),
                "strategy": "bollinger_rsi_mean_reversion",
            })

    # -- Position monitoring --------------------------------------------------

    def monitor_positions(self):
        """Check all open positions for exit conditions."""
        exits = []
        tp1_hits = []

        with self._lock:
            for pos in self.positions:
                price = self.prices.get(pos.pair)
                if price is None:
                    continue

                exit_reason = pos.check_exit(price)
                if exit_reason:
                    exits.append((pos, price, exit_reason))
                elif pos.tp1_hit and not pos._tp1_logged:
                    pos._tp1_logged = True
                    tp1_hits.append(pos)

        for pos in tp1_hits:
            self._log(f"TP1 hit {pos.pair} -- SL moved to breakeven @ {pos.entry_price:.6f}")

        for pos, price, reason in exits:
            self.close_position(pos, price, reason)

    # -- Main scan loop -------------------------------------------------------

    def scan_once(self):
        """Run one full scan cycle: fetch data, compute indicators, check signals, manage positions."""
        self.scan_count += 1
        self.last_scan = time.time()
        self._log(f"--- Scan #{self.scan_count} ---")

        # Lease heartbeat: declare held reservation ids so the pool can sweep
        # anything a wiring bug stranded (never raises).
        if self._portfolio_client:
            self._portfolio_client.confirm_reservations(
                [p.reservation_id for p in self.positions if p.reservation_id])

        # Refresh universe periodically (every 10 scans)
        if self.scan_count % 10 == 1 or not self.pairs:
            self.pairs = self.fetch_universe()

        # Fetch candles and compute indicators for each pair
        signals = []
        for pair in self.pairs:
            candles = self.fetch_candles(pair)
            if candles is None:
                continue

            with self._lock:
                self.candles[pair] = candles
                # Update latest price
                self.prices[pair] = candles[-1][4]  # close of last candle

            ind = self.compute_indicators(pair, candles)
            if ind is None:
                continue

            with self._lock:
                self.indicators[pair] = ind

            signal = self.evaluate_signal(pair, ind)
            if signal:
                signals.append(signal)

        self._log(f"Scanned {len(self.pairs)} pairs, {len(signals)} signals found")

        # Try to open positions from signals
        opened = 0
        for sig in signals:
            if self.open_position(sig):
                opened += 1

        if opened:
            self._log(f"Opened {opened} new position(s)")

        # Monitor existing positions
        self.monitor_positions()

        # Record equity curve point
        with self._lock:
            self.equity_curve.append({
                "ts": time.time(),
                "equity": round(self.equity, 2),
                "positions": len(self.positions),
            })
            # Trim equity curve to last 2000 points
            if len(self.equity_curve) > 2000:
                self.equity_curve = self.equity_curve[-2000:]

    def run_scan_loop(self):
        """Continuous scan loop (runs in daemon thread)."""
        self.status = "running"
        self._log(f"Scan loop started (interval={SCAN_INTERVAL}s)")

        while not self.shutdown_event.is_set():
            try:
                self.scan_once()
                # Checkpoint every scan, not only on trade events. All three
                # _save_positions call sites are entry/exit, so a bot holding
                # steady positions never rewrote the file and the safety state
                # added above never reached disk between trades.
                self._save_positions()
            except Exception as e:
                self._log(f"Scan error: {e}\n{traceback.format_exc()}", "ERROR")

            # Wait for next scan (interruptible)
            self.shutdown_event.wait(timeout=SCAN_INTERVAL)

        self.status = "stopped"
        self._log("Scan loop stopped")

    # -- Snapshot for API -----------------------------------------------------

    def snapshot(self) -> dict:
        """Build full state snapshot for /api/snapshot."""
        with self._lock:
            open_pos = [p.to_dict() for p in self.positions]
            recent = [t.to_dict() for t in self.trades[-10:]]
            total_trades = self.wins + self.losses
            win_rate = (self.wins / total_trades * 100) if total_trades else 0.0
            pnl = self.equity - self.starting_equity
            pnl_pct = (pnl / self.starting_equity * 100) if self.starting_equity else 0.0
            dd = self.peak_equity - self.equity
            dd_pct = (dd / self.peak_equity * 100) if self.peak_equity else 0.0
            eq = self.equity
            positions_dict = {}
            for p in self.positions:
                pr = self.prices.get(p.pair)
                d = p.to_dict()
                if pr is not None:
                    d["current_price"] = round(pr, 6)
                    d["unrealized_pnl"] = round(p.unrealized_pnl(pr), 2)
                positions_dict[p.pair] = d

            # Determine overall regime from indicators
            ranging = 0
            trending = 0
            for pair, ind in self.indicators.items():
                adx = ind.get("adx")
                if adx is not None:
                    if adx < ADX_TREND_MIN:
                        ranging += 1
                    else:
                        trending += 1
            regime = "RANGING" if ranging >= trending else "TRENDING"

            indicator_summary = {}
            for pair, ind in self.indicators.items():
                indicator_summary[pair] = {
                    "bb_upper": round(ind["bb_upper"], 6),
                    "bb_middle": round(ind["bb_middle"], 6),
                    "bb_lower": round(ind["bb_lower"], 6),
                    "rsi": round(ind["rsi"], 2),
                    "adx": round(ind["adx"], 2) if ind["adx"] is not None else None,
                    "atr": round(ind["atr"], 6),
                    "price": round(ind["price"], 6),
                }

        result = {
            "timestamp": time.time(),
            "bot_name": BOT_NAME,
            "version": VERSION,
            "accent": ACCENT,
            "status": self.status,
            "mode": "paper",
            "scan_count": self.scan_count,
            "last_scan": self.last_scan,
            "equity": round(eq, 2),
            "starting_equity": self.starting_equity,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "drawdown_pct": round(dd_pct, 2),
            "open_positions": len(open_pos),
            "total_trades": total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(win_rate, 1),
            "positions": positions_dict,
            "recent_trades": recent,
            "strategy": {
                "name": "Trend-Aligned Mean Reversion (BB + RSI + Trend, long + short)",
                "bb_period": BB_PERIOD,
                "bb_std": BB_STD,
                "rsi_period": RSI_PERIOD,
                "rsi_dip_threshold": RSI_DIP_THRESHOLD,
                "rsi_deep_dip": RSI_DEEP_DIP,
                "rsi_rip_threshold": RSI_RIP_THRESHOLD,
                "rsi_deep_pump": RSI_DEEP_PUMP,
                "adx_threshold": ADX_TREND_MIN,
                "sl_atr_mult": SL_ATR_MULT,
                "max_positions": MAX_POSITIONS,
                "trade_risk_pct": TRADE_RISK_PCT,
            },
            "regime": regime,
            "bus_regime": self._bus_regime,
            "pairs_count": len(self.pairs),
            "pairs": list(self.pairs),
            "indicators": indicator_summary,
            "prices": {p: round(v, 6) for p, v in self.prices.items()},
            "equity_curve": self.equity_curve[-500:],
            "uptime_seconds": round(time.time() - self.start_time),
            "logs": list(self.log_buffer)[-50:],
        }
        if _expectancy:
            result["expectancy"] = _expectancy.bot_snapshot_fields('rubberband')
        return result


# ============================================================================
# HTTP Server
# ============================================================================

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads."""
    daemon_threads = True
    allow_reuse_address = True


class RubberbandHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Rubberband API."""

    engine: RubberbandEngine  # set via class factory

    def log_message(self, fmt, *args):
        pass  # suppress default stderr logging

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/snapshot":
            self._serve_snapshot()
        elif path == "/health":
            self._serve_health()
        else:
            self.send_error(404, "Not Found")

    def _serve_snapshot(self):
        try:
            snap = self.engine.snapshot()
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"snapshot() crashed: {e}\n{tb}")
            snap = {"error": str(e), "traceback": tb}
        self._serve_json(snap)

    def _serve_health(self):
        data = {
            "status": "ok",
            "bot": BOT_NAME,
            "version": VERSION,
            "timestamp": time.time(),
            "uptime": round(time.time() - self.engine.start_time),
        }
        self._serve_json(data)

    def _serve_json(self, obj):
        try:
            body = json.dumps(obj, default=str).encode("utf-8")
        except Exception:
            body = b'{"error": "serialization error"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description=f"{BOT_NAME} v{VERSION} -- Trend-Aligned Mean Reversion (long + short)")
    parser.add_argument("--auto", action="store_true", help="Headless mode (fleet launcher)")
    parser.add_argument("--port", type=int, default=PORT, help=f"HTTP port (default {PORT})")
    args = parser.parse_args()

    port = args.port

    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        ensure_port(port, "rubberband")
        write_pidfile("rubberband", port)
        atexit.register(cleanup_pidfile, "rubberband")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    print(f"\n{'='*60}")
    print(f"  {BOT_NAME} v{VERSION} -- Trend-Aligned Mean Reversion (long + short)")
    print(f"  Strategy: Bollinger Band ({BB_PERIOD}, {BB_STD}) + RSI ({RSI_PERIOD}) + ADX ({ADX_PERIOD})")
    print(f"  Port: {port} | Paper Balance: ${PAPER_BALANCE:,.0f}")
    print(f"  Max Positions: {MAX_POSITIONS} | Risk/Trade: {TRADE_RISK_PCT*100:.0f}%")
    print(f"{'='*60}\n")

    # Build engine
    engine = RubberbandEngine(auto=args.auto)
    engine.init_fleet()
    engine._reconcile_positions()

    # Start HTTP server (main thread owns this)
    handler_class = type("Handler", (RubberbandHandler,), {"engine": engine})
    server = ThreadedHTTPServer(("0.0.0.0", port), handler_class)
    logger.info(f"HTTP server listening on 0.0.0.0:{port}")
    print(f"  API:    http://127.0.0.1:{port}/api/snapshot")
    print(f"  Health: http://127.0.0.1:{port}/health")
    print()

    # Start scan loop in daemon thread
    scan_thread = threading.Thread(target=engine.run_scan_loop, name="rubberband-scan", daemon=True)
    scan_thread.start()

    # Graceful shutdown
    def _shutdown(signum, frame):
        print(f"\n  Shutting down {BOT_NAME}...")
        engine.shutdown_event.set()
        engine.status = "stopping"
        server.shutdown()

    signal_mod.signal(signal_mod.SIGINT, _shutdown)
    signal_mod.signal(signal_mod.SIGTERM, _shutdown)

    # Serve forever (main thread)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.shutdown_event.set()
        engine.status = "stopped"
        # Release portfolio reservations for any open positions
        if engine._portfolio_client:
            with engine._lock:
                for pos in engine.positions:
                    if pos.reservation_id:
                        try:
                            engine._portfolio_client.release(pos.reservation_id, pnl=0.0)
                            print(f"  Released reservation {pos.reservation_id}")
                        except Exception:
                            pass
        server.server_close()

    # Final stats
    with engine._lock:
        total = engine.wins + engine.losses
        wr = (engine.wins / total * 100) if total else 0
        pnl = engine.equity - engine.starting_equity

    print(f"\n{'='*60}")
    print(f"  {BOT_NAME} Final Stats")
    print(f"  Equity: ${engine.equity:,.2f} | PnL: ${pnl:+,.2f}")
    print(f"  Trades: {total} | Win Rate: {wr:.1f}%")
    print(f"  Scans: {engine.scan_count}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
