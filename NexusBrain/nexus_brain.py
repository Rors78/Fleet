#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NEXUS BRAIN v1.0 -- Multi-TF Cross-Exchange Signal Scanner
============================================================
Multi-timeframe confluence scoring with cross-exchange price validation.
Kraken primary / CoinGecko secondary.
6 signal components across 5 timeframes (5m, 15m, 1h, 4h, 1d).
Bidirectional: LONG and SHORT signals (shorts gated by
fleet_config.direction_allowed; paper/signal-only — spot cannot short).
Regime detection, paper trading, SQLite persistence.

Usage:
    python nexus_brain.py scan                 # one-shot scan all pairs
    python nexus_brain.py backtest             # historical backtest
    python nexus_brain.py run-sim              # continuous paper trading simulation
    python nexus_brain.py report               # show performance report
    python nexus_brain.py health               # health check endpoint
    python nexus_brain.py dashboard            # run backtest + web dashboard
    python nexus_brain.py dashboard --auto     # headless dashboard mode

Flags:
    --auto      Headless mode, no interactive prompts
    --pairs     Comma-separated pairs (default: auto top-20)
    --capital   Initial capital for backtest/sim (default: 10000)
"""

import argparse
import json
import logging
import math
import os
import signal as signal_mod
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

import requests
import sys
from pathlib import Path

# Portfolio client — shared fleet capital pool
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "CommandCenter"))
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

from indicators import (
    Candle, ema, rsi, macd, bollinger_bands, atr, adx,
)

# ═══════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nexus_brain.log")
logger = logging.getLogger("NexusBrain")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)


# ═══════════════════════════════════════════════════════════════════════
# ENUMS & CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

class Regime(Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    VOLATILE = "VOLATILE"


class ExitReason(Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_EXIT = "TIME_EXIT"
    REGIME_CHANGE = "REGIME_CHANGE"
    SIGNAL_FLIP = "SIGNAL_FLIP"


class ConfidenceLabel(Enum):
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    LOW = "LOW"
    REJECT = "REJECT"


VERSION = "1.0"
BOT_NAME = "NexusBrain"

# Exchange endpoints
KRAKEN_REST = "https://api.kraken.com"
COINGECKO_REST = "https://api.coingecko.com/api/v3"

# Timeframe definitions with weights
TIMEFRAMES = {
    "5m":  {"kraken_interval": 5,    "limit": 100, "weight": 0.10},
    "15m": {"kraken_interval": 15,   "limit": 100, "weight": 0.20},
    "1h":  {"kraken_interval": 60,   "limit": 100, "weight": 0.30},
    "4h":  {"kraken_interval": 240,  "limit": 100, "weight": 0.25},
    "1d":  {"kraken_interval": 1440, "limit": 100, "weight": 0.15},
}

# Signal component weights (must sum to 1.0)
# Gain converting |macd_hist|/price into the 0.35-wide MACD bonus band.
# Calibrated from 1120 samples on 1h bars across 8 majors (p90 = 0.352% of
# price -> full bonus). See score_macd_momentum for why this exists.
MACD_HIST_GAIN = 100.0

COMPONENT_WEIGHTS = {
    "ema_alignment":   0.20,
    "rsi_momentum":    0.15,
    "macd_momentum":   0.20,
    "bollinger_pos":   0.15,
    "volume_confirm":  0.15,
    "regime_align":    0.15,
}

# Default trading pairs (Kraken format), filtered by fleet blacklist
_RAW_DEFAULT_PAIRS = [
    "XXBTZUSD", "XETHZUSD", "SOLUSD", "ADAUSD", "DOTUSD",
    "AVAXUSD", "LINKUSD", "POLUSD", "ATOMUSD", "UNIUSD",
    "AAVEUSD", "LTCUSD", "XLMUSD", "ALGOUSD", "NEARUSD",
    "FILUSD", "ICPUSD", "APTUSD", "ARBUSD", "OPUSD",
]
DEFAULT_PAIRS = [p for p in _RAW_DEFAULT_PAIRS if not _is_blacklisted(p)]

# Pair name mapping for display
PAIR_DISPLAY = {
    "XXBTZUSD": "BTC/USD", "XETHZUSD": "ETH/USD", "SOLUSD": "SOL/USD",
    "ADAUSD": "ADA/USD", "DOTUSD": "DOT/USD", "AVAXUSD": "AVAX/USD",
    "LINKUSD": "LINK/USD", "POLUSD": "POL/USD", "ATOMUSD": "ATOM/USD",
    "UNIUSD": "UNI/USD", "AAVEUSD": "AAVE/USD", "LTCUSD": "LTC/USD",
    "XLMUSD": "XLM/USD", "ALGOUSD": "ALGO/USD", "NEARUSD": "NEAR/USD",
    "FILUSD": "FIL/USD", "ICPUSD": "ICP/USD", "APTUSD": "APT/USD",
    "ARBUSD": "ARB/USD", "OPUSD": "OP/USD",
}

# CoinGecko ID mapping for validation
COINGECKO_IDS = {
    "XXBTZUSD": "bitcoin", "XETHZUSD": "ethereum", "SOLUSD": "solana",
    "ADAUSD": "cardano", "DOTUSD": "polkadot", "AVAXUSD": "avalanche-2",
    "LINKUSD": "chainlink", "POLUSD": "polygon-ecosystem-token", "ATOMUSD": "cosmos",
    "UNIUSD": "uniswap", "AAVEUSD": "aave", "LTCUSD": "litecoin",
    "XLMUSD": "stellar", "ALGOUSD": "algorand", "NEARUSD": "near",
    "FILUSD": "filecoin", "ICPUSD": "internet-computer", "APTUSD": "aptos",
    "ARBUSD": "arbitrum", "OPUSD": "optimism",
}


# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class Config:
    """All tunables in one place."""
    # Position management
    max_positions: int = 5
    initial_capital: float = 10000.0
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 0.05   # 5% of capital — raised from 3% on 2026-04-07 to clear 5% pool floor ($500 min)

    # Signal thresholds (raised 2026-04-01 from 0.55; kept post fee-removal
    # as a signal-quality gate — thresholds are computed gross)
    min_confluence: float = 0.70  # lowered 2026-04-08: 0.80 too conservative, zero trades in 8h
    high_confidence_threshold: float = 0.75
    moderate_confidence_threshold: float = 0.72  # aligned with min_confluence

    # Risk parameters
    stop_loss_atr_mult: float = 2.0
    take_profit_atr_mult: float = 3.0
    trailing_stop_atr_mult: float = 1.5
    max_hold_hours: int = 72
    time_exit_hours: int = 48

    # Slippage (execution realism — kept). Fees removed fleet-wide
    # 2026-07-30: this is a signal product; subscribers pay their own
    # exchanges' fees, so P/L is gross price movement. fee_rate stays as
    # a field at 0.0 only because /api results and dashboard.html expose
    # config.fee_rate (shape preserved, honestly zeroed).
    fee_rate: float = 0.0
    slippage_bps: float = 5.0

    # Cross-exchange validation
    max_price_divergence_pct: float = 1.0
    min_exchanges_agree: int = 2

    # Regime detection
    adx_trend_threshold: float = 25.0
    volatility_regime_threshold: float = 0.03

    # Data
    ohlc_limit: int = 100
    scan_interval_sec: float = 60.0

    # Dashboard
    DASHBOARD_PORT: int = 8074

    # Central portfolio
    use_central_portfolio: bool = True
    command_center_url: str = "http://127.0.0.1:9000"

    # Backtest
    backtest_days: int = 90

    # SQLite
    db_path: str = ""

    def __post_init__(self):
        if not self.db_path:
            self.db_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "nexus_brain.db"
            )


# ═══════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class SignalComponents:
    ema_alignment: float = 0.5
    rsi_momentum: float = 0.5
    macd_momentum: float = 0.5
    bollinger_pos: float = 0.5
    volume_confirm: float = 0.5
    regime_align: float = 0.5

    def to_dict(self) -> Dict[str, float]:
        return {
            "ema_alignment": round(self.ema_alignment, 4),
            "rsi_momentum": round(self.rsi_momentum, 4),
            "macd_momentum": round(self.macd_momentum, 4),
            "bollinger_pos": round(self.bollinger_pos, 4),
            "volume_confirm": round(self.volume_confirm, 4),
            "regime_align": round(self.regime_align, 4),
        }


@dataclass
class Signal:
    pair: str
    confluence_score: float
    regime: Regime
    confidence_label: ConfidenceLabel
    entry_price: float
    stop_loss: float
    take_profit: float
    components: SignalComponents
    cross_exchange_valid: bool
    exchange_prices: Dict[str, float]
    timestamp: float = 0.0
    atr_value: float = 0.0
    direction: str = "LONG"

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "pair": PAIR_DISPLAY.get(self.pair, self.pair),
            "direction": self.direction,
            "confluence_score": round(sf(self.confluence_score), 4),
            "regime": self.regime.value,
            "confidence_label": self.confidence_label.value,
            "entry_price": round(sf(self.entry_price), 8),
            "stop_loss": round(sf(self.stop_loss), 8),
            "take_profit": round(sf(self.take_profit), 8),
            "components": self.components.to_dict(),
            "cross_exchange_valid": self.cross_exchange_valid,
            "exchange_prices": {k: round(sf(v), 8) for k, v in self.exchange_prices.items()},
            "timestamp": self.timestamp,
            "atr_value": round(sf(self.atr_value), 8),
        }


@dataclass
class Position:
    pair: str
    entry_price: float
    size_usd: float
    stop_loss: float
    take_profit: float
    signal_score: float
    entry_time: float
    trailing_stop: float = 0.0
    peak_price: float = 0.0
    reservation_id: str = ""
    direction: str = "LONG"

    def __post_init__(self):
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price

    def to_dict(self) -> dict:
        return {
            "pair": PAIR_DISPLAY.get(self.pair, self.pair),
            "direction": self.direction,
            "entry_price": round(sf(self.entry_price), 8),
            "size_usd": round(sf(self.size_usd), 2),
            "stop_loss": round(sf(self.stop_loss), 8),
            "take_profit": round(sf(self.take_profit), 8),
            "signal_score": round(sf(self.signal_score), 4),
            "entry_time": self.entry_time,
        }


@dataclass
class Trade:
    pair: str
    entry_price: float
    exit_price: float
    pnl_usd: float
    pnl_pct: float
    exit_reason: ExitReason
    signal_score: float
    entry_time: float
    exit_time: float
    size_usd: float = 0.0
    direction: str = "LONG"

    def to_dict(self) -> dict:
        return {
            "pair": PAIR_DISPLAY.get(self.pair, self.pair),
            "direction": self.direction,
            "entry_price": round(sf(self.entry_price), 8),
            "exit_price": round(sf(self.exit_price), 8),
            "pnl_usd": round(sf(self.pnl_usd), 2),
            "pnl_pct": round(sf(self.pnl_pct), 4),
            "exit_reason": self.exit_reason.value,
            "signal_score": round(sf(self.signal_score), 4),
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "size_usd": round(sf(self.size_usd), 2),
        }


@dataclass
class PairData:
    """Per-pair candle data across all timeframes."""
    pair: str
    candles: Dict[str, List[Candle]] = field(default_factory=dict)
    last_price: float = 0.0
    last_update: float = 0.0


# ═══════════════════════════════════════════════════════════════════════
# HELPER: SAFE FLOAT (JSON-safe, no inf/nan)
# ═══════════════════════════════════════════════════════════════════════

def _restored_direction(pair, d) -> str:
    """Direction for a position being restored from disk.

    LONG stays the fallback because legacy state files predate shorts, but it
    is no longer SILENT. Current files all record a direction, so a missing
    one now means corruption — and quietly restoring a live SHORT as a LONG
    inverts its P/L, its stop and its take-profit. A restart is exactly when
    nobody is watching, so this announces itself.
    """
    direction = d.get("direction")
    if direction:
        return direction
    logger.warning(
        "Saved position %s has NO direction - assuming LONG (legacy file). "
        "If it is actually short, its P/L, stop and take-profit are inverted.",
        pair)
    return "LONG"


def sf(v) -> float:
    """Safe float -- clamp inf/nan to 0.0 for JSON serialization."""
    if v is None:
        return 0.0
    if isinstance(v, float):
        if math.isinf(v) or math.isnan(v):
            return 0.0
    return float(v)


def volume_momentum(candles: List[Candle], period: int = 20) -> float:
    """Volume momentum -- ratio of the last COMPLETED bar's volume to average.

    Compares candles[-2], not candles[-1]. The final candle from the OHLC feed
    is the bar currently forming, so it holds only the volume accumulated so
    far this period. Measuring it against completed bars scaled the ratio down
    by however much of the bar had elapsed, regardless of actual market
    activity:

        bar 27.6 min into a 60-min period
        forming bar volume      677     -> ratio 0.296 -> score 0.20 (floor)
        last completed bar     1502     -> ratio 0.616 -> score 0.35

    The result was that score_volume_confirmation returned exactly 0.20 -- the
    lowest bucket -- on every pair, every scan. That cost ~4.5pp of confluence
    on every candidate against a 0.70 entry gate and is a large part of why
    total_trades was 0. min_confluence had already been lowered 0.80 -> 0.70
    on 2026-04-08 blaming "too conservative", when the real fault was here.
    (2026-07-29 audit.)
    """
    if len(candles) < period + 2:
        return 1.0
    # Last completed bar, and the `period` completed bars before it.
    recent = candles[-2].volume
    avg_vol = sum(c.volume for c in candles[-period - 2:-2]) / period
    if avg_vol == 0:
        return 1.0
    return recent / avg_vol


def compute_volatility(closes: List[float], period: int = 20) -> float:
    """Compute annualized volatility from close prices."""
    if len(closes) < period + 1:
        return 0.0
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] != 0:
            returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
    if len(returns) < period:
        return 0.0
    recent = returns[-period:]
    mean_r = sum(recent) / len(recent)
    variance = sum((r - mean_r) ** 2 for r in recent) / len(recent)
    return math.sqrt(variance) * math.sqrt(365)


# ═══════════════════════════════════════════════════════════════════════
# REGIME DETECTION
# ═══════════════════════════════════════════════════════════════════════

def detect_regime(candles: List[Candle], cfg: Config) -> Regime:
    """Detect market regime from candle data."""
    if len(candles) < 30:
        return Regime.RANGE

    closes = [c.close for c in candles]
    adx_val, pdi, mdi = adx(candles)
    vol = compute_volatility(closes)

    # Volatile regime takes priority
    if vol > cfg.volatility_regime_threshold:
        # Still check for strong trend within volatility
        if adx_val > cfg.adx_trend_threshold + 10:
            if pdi > mdi:
                return Regime.TREND_UP
            else:
                return Regime.TREND_DOWN
        return Regime.VOLATILE

    # Trending
    if adx_val > cfg.adx_trend_threshold:
        if pdi > mdi:
            return Regime.TREND_UP
        else:
            return Regime.TREND_DOWN

    return Regime.RANGE


# ═══════════════════════════════════════════════════════════════════════
# SIGNAL SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════════

def score_ema_alignment(candles: List[Candle], price: float) -> float:
    """EMA alignment score: how well EMAs are stacked for trend."""
    closes = [c.close for c in candles]
    if len(closes) < 50:
        return 0.5

    ema_8 = ema(closes, 8)
    ema_21 = ema(closes, 21)
    ema_50 = ema(closes, 50)

    if ema_50 == 0:
        return 0.5

    # Perfect bullish: price > ema8 > ema21 > ema50
    bullish_stack = int(price > ema_8) + int(ema_8 > ema_21) + int(ema_21 > ema_50)
    # Perfect bearish: price < ema8 < ema21 < ema50
    bearish_stack = int(price < ema_8) + int(ema_8 < ema_21) + int(ema_21 < ema_50)

    if bullish_stack == 3:
        # Scale by separation magnitude
        spread = (ema_8 - ema_50) / ema_50
        return min(1.0, 0.7 + abs(spread) * 10)
    elif bearish_stack == 3:
        spread = (ema_50 - ema_8) / ema_50
        return max(0.0, 0.3 - abs(spread) * 10)
    else:
        # Mixed -- closer to 0.5
        return 0.5 + (bullish_stack - bearish_stack) * 0.08


def score_rsi_momentum(closes: List[float]) -> float:
    """RSI momentum score."""
    rsi_val = rsi(closes, 14)

    # Oversold bounce = bullish
    if rsi_val < 30:
        return 0.75 + (30 - rsi_val) / 120
    # Overbought = bearish
    elif rsi_val > 70:
        return 0.25 - (rsi_val - 70) / 120
    else:
        # Linear mapping 30-70 -> 0.35-0.65
        return 0.35 + (rsi_val - 30) / 40 * 0.30


def score_macd_momentum(closes: List[float], price: float) -> float:
    """MACD momentum score."""
    macd_line, signal_line, hist = macd(closes)
    if price == 0:
        return 0.5

    # Fraction of price, not percent. This previously computed
    # `hist / price * 100` (a PERCENT) and then applied a gain of 15, which
    # meant the 0.35 bonus band was fully consumed by a histogram worth just
    # 0.0233% of price -- about $15 on BTC at $64k. MACD therefore read 0.950
    # on 5 of 6 live pairs: a binary flag, not a graded score, wasting 20% of
    # the confluence weight (2026-07-29 audit).
    #
    # MACD_HIST_GAIN is calibrated from 1120 samples of |hist|/price on 1h
    # bars across 8 majors: p50 0.122%, p90 0.352%, p99 0.605%. A gain of 100
    # maps the p90 move to the full 0.35 bonus and leaves p50 at ~0.12, so the
    # score spans a useful range instead of pinning.
    hist_frac = hist / price

    # Positive histogram = bullish momentum
    if hist > 0:
        if macd_line > signal_line:
            score = 0.6 + min(hist_frac * MACD_HIST_GAIN, 0.35)
        else:
            score = 0.55
    else:
        if macd_line < signal_line:
            score = 0.4 - min(abs(hist_frac) * MACD_HIST_GAIN, 0.35)
        else:
            score = 0.45

    # Cross detection -- bullish cross gets bonus
    if len(closes) >= 40:
        macd_prev = macd(closes[:-1])
        if macd_prev[2] < 0 and hist > 0:
            score = min(1.0, score + 0.15)
        elif macd_prev[2] > 0 and hist < 0:
            score = max(0.0, score - 0.15)

    return max(0.0, min(1.0, score))


def score_bollinger_position(closes: List[float], price: float) -> float:
    """Bollinger Band position score -- mean reversion bias."""
    bb_upper, bb_mid, bb_lower = bollinger_bands(closes)

    if bb_upper == bb_lower or bb_upper == 0:
        return 0.5

    bb_pct = (price - bb_lower) / (bb_upper - bb_lower)

    # Near lower band = potential long (oversold)
    if bb_pct < 0.15:
        return 0.80
    elif bb_pct < 0.30:
        return 0.65
    # Near upper band = overbought
    elif bb_pct > 0.85:
        return 0.20
    elif bb_pct > 0.70:
        return 0.35
    else:
        # Middle zone
        return 0.50 + (0.50 - bb_pct) * 0.3


def score_volume_confirmation(candles: List[Candle]) -> float:
    """Volume confirmation score -- higher volume validates moves."""
    vol_mom = volume_momentum(candles)

    if vol_mom > 2.5:
        return 0.95
    elif vol_mom > 1.8:
        return 0.80
    elif vol_mom > 1.3:
        return 0.65
    elif vol_mom > 0.8:
        return 0.50
    elif vol_mom > 0.5:
        return 0.35
    else:
        return 0.20


def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """Linear interpolation of x from [x0,x1] onto [y0,y1], clamped."""
    if x1 == x0:
        return y0
    t = (x - x0) / (x1 - x0)
    t = max(0.0, min(1.0, t))
    return y0 + (y1 - y0) * t


def score_regime_alignment(regime: Regime, other_scores: Dict[str, float]) -> float:
    """Score how well the other signals align with the detected regime.

    Continuous, not a step function. The previous version used hard cutoffs
    (avg > 0.6 -> 1.0, avg > 0.5 -> 0.7, else 0.3), which put cliffs right
    where decisions are made: avg_bullish moving 0.500 -> 0.501 jumped final
    confluence by 0.061, and 0.600 -> 0.601 by 0.046. A 0.001 change in raw
    evidence could carry a pair straight across the 0.70 entry gate, so two
    setups with effectively identical signals landed on opposite sides of the
    trade decision (2026-07-29 audit).

    Note this component is deliberately NOT independent evidence -- it is a
    function of the other five, i.e. a regime-conditioned re-weighting of the
    same information. That is defensible as a consistency check, but it means
    its 0.15 weight amplifies the other 0.85 rather than adding anything new.
    Keep that in mind before increasing its weight.
    """
    avg_bullish = sum(other_scores.values()) / max(len(other_scores), 1)

    if regime == Regime.TREND_UP:
        # Bullish evidence in an uptrend is corroborating; bearish contradicts.
        # 0.3 at avg=0.4 rising smoothly to 1.0 at avg=0.7.
        return _lerp(avg_bullish, 0.40, 0.70, 0.30, 1.00)
    elif regime == Regime.TREND_DOWN:
        # Mirror image: bearish evidence (low avg) corroborates a downtrend.
        return _lerp(avg_bullish, 0.30, 0.60, 0.80, 0.20)
    elif regime == Regime.VOLATILE:
        # In volatile conditions, conviction either way is worth something;
        # indecision is not. Already continuous.
        return 0.4 + abs(avg_bullish - 0.5) * 0.4
    else:
        # RANGE -- neutral
        return 0.5


def score_single_timeframe(candles: List[Candle], price: float, regime: Regime) -> SignalComponents:
    """Score all 6 components for a single timeframe."""
    closes = [c.close for c in candles]
    if len(closes) < 30:
        return SignalComponents()

    ema_score = score_ema_alignment(candles, price)
    rsi_score = score_rsi_momentum(closes)
    macd_score = score_macd_momentum(closes, price)
    bb_score = score_bollinger_position(closes, price)
    vol_score = score_volume_confirmation(candles)

    other_scores = {
        "ema_alignment": ema_score,
        "rsi_momentum": rsi_score,
        "macd_momentum": macd_score,
        "bollinger_pos": bb_score,
        "volume_confirm": vol_score,
    }
    regime_score = score_regime_alignment(regime, other_scores)

    return SignalComponents(
        ema_alignment=max(0.0, min(1.0, ema_score)),
        rsi_momentum=max(0.0, min(1.0, rsi_score)),
        macd_momentum=max(0.0, min(1.0, macd_score)),
        bollinger_pos=max(0.0, min(1.0, bb_score)),
        volume_confirm=max(0.0, min(1.0, vol_score)),
        regime_align=max(0.0, min(1.0, regime_score)),
    )


def compute_confluence(components: SignalComponents) -> float:
    """Compute weighted confluence score from components."""
    score = (
        components.ema_alignment * COMPONENT_WEIGHTS["ema_alignment"]
        + components.rsi_momentum * COMPONENT_WEIGHTS["rsi_momentum"]
        + components.macd_momentum * COMPONENT_WEIGHTS["macd_momentum"]
        + components.bollinger_pos * COMPONENT_WEIGHTS["bollinger_pos"]
        + components.volume_confirm * COMPONENT_WEIGHTS["volume_confirm"]
        + components.regime_align * COMPONENT_WEIGHTS["regime_align"]
    )
    return max(0.0, min(1.0, score))


def classify_confidence(score: float, cfg: Config) -> ConfidenceLabel:
    """Classify confluence score into confidence label."""
    if score >= cfg.high_confidence_threshold:
        return ConfidenceLabel.HIGH
    elif score >= cfg.moderate_confidence_threshold:
        return ConfidenceLabel.MODERATE
    elif score >= cfg.min_confluence:
        return ConfidenceLabel.LOW
    else:
        return ConfidenceLabel.REJECT


# ═══════════════════════════════════════════════════════════════════════
# MULTI-TIMEFRAME SIGNAL GENERATION
# ═══════════════════════════════════════════════════════════════════════

def generate_signal(pair: str, pair_data: PairData, cfg: Config) -> Optional[Signal]:
    """Generate a composite multi-TF signal for a pair."""
    price = pair_data.last_price
    if price <= 0:
        return None

    if not pair_data.candles:
        return None

    # Detect regime from the 1h timeframe (or best available)
    regime_candles = pair_data.candles.get("1h", [])
    if not regime_candles:
        regime_candles = pair_data.candles.get("15m", [])
    if not regime_candles:
        # Use whatever we have
        for tf in TIMEFRAMES:
            if tf in pair_data.candles and pair_data.candles[tf]:
                regime_candles = pair_data.candles[tf]
                break
    regime = detect_regime(regime_candles, cfg) if regime_candles else Regime.RANGE

    # Score each timeframe and combine
    combined = SignalComponents(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    total_weight = 0.0

    for tf_name, tf_cfg in TIMEFRAMES.items():
        candles = pair_data.candles.get(tf_name, [])
        if not candles or len(candles) < 30:
            continue

        tf_components = score_single_timeframe(candles, price, regime)
        w = tf_cfg["weight"]
        total_weight += w

        combined.ema_alignment += tf_components.ema_alignment * w
        combined.rsi_momentum += tf_components.rsi_momentum * w
        combined.macd_momentum += tf_components.macd_momentum * w
        combined.bollinger_pos += tf_components.bollinger_pos * w
        combined.volume_confirm += tf_components.volume_confirm * w
        combined.regime_align += tf_components.regime_align * w

    if total_weight == 0:
        return None

    # Normalize by total weight
    combined.ema_alignment /= total_weight
    combined.rsi_momentum /= total_weight
    combined.macd_momentum /= total_weight
    combined.bollinger_pos /= total_weight
    combined.volume_confirm /= total_weight
    combined.regime_align /= total_weight

    # Clamp to [0, 1]
    combined.ema_alignment = max(0.0, min(1.0, combined.ema_alignment))
    combined.rsi_momentum = max(0.0, min(1.0, combined.rsi_momentum))
    combined.macd_momentum = max(0.0, min(1.0, combined.macd_momentum))
    combined.bollinger_pos = max(0.0, min(1.0, combined.bollinger_pos))
    combined.volume_confirm = max(0.0, min(1.0, combined.volume_confirm))
    combined.regime_align = max(0.0, min(1.0, combined.regime_align))

    confluence = compute_confluence(combined)

    # Short confluence — the mirror of the same evidence, component by
    # component. The four directional components score bullish > 0.5 and
    # bearish < 0.5 by construction (bearish EMA stack -> 0.3-band, RSI
    # overbought -> 0.25-band, negative MACD histogram -> 0.4-band, upper
    # Bollinger band -> 0.20), so a short reads them as 1 - score. The other
    # two are NOT directional and are carried over unchanged:
    #   volume_confirm — high volume validates a move in EITHER direction;
    #     inverting it would count strong volume as evidence AGAINST a short.
    #   regime_align — already a regime-consistency measure (in TREND_DOWN it
    #     scores bearish evidence HIGH, see score_regime_alignment); inverting
    #     would punish a short for agreeing with a downtrend.
    short_combined = SignalComponents(
        ema_alignment=1.0 - combined.ema_alignment,
        rsi_momentum=1.0 - combined.rsi_momentum,
        macd_momentum=1.0 - combined.macd_momentum,
        bollinger_pos=1.0 - combined.bollinger_pos,
        volume_confirm=combined.volume_confirm,
        regime_align=combined.regime_align,
    )
    short_confluence = compute_confluence(short_combined)

    # Direction: take whichever side the evidence actually supports. Both
    # sides face the same min_confluence gate (0.70), so a SHORT only fires
    # on strong bearish agreement — shorts stay rare and high-conviction.
    # direction_allowed() is checked dynamically (no restart needed when
    # FLEET_LONG_ONLY flips); without fleet_config we stay LONG-only.
    direction = "LONG"
    _shorts_ok = False
    if _fc and hasattr(_fc, "direction_allowed"):
        try:
            _shorts_ok, _ = _fc.direction_allowed("SHORT")
        except Exception:
            _shorts_ok = False
    if _shorts_ok and short_confluence > confluence:
        direction = "SHORT"
        confluence = short_confluence
        combined = short_combined

    # Direction-regime mismatch filter, symmetric: never go long in a
    # downtrend, never go short in an uptrend.
    if direction == "LONG" and regime == Regime.TREND_DOWN:
        confluence = 0.0
    elif direction == "SHORT" and regime == Regime.TREND_UP:
        confluence = 0.0

    confidence = classify_confidence(confluence, cfg)

    # ATR for stop/target from best available TF
    atr_candles = pair_data.candles.get("1h", regime_candles)
    atr_val = atr(atr_candles) if atr_candles else price * 0.02
    if atr_val <= 0:
        atr_val = price * 0.02

    if direction == "SHORT":
        # Mirrored risk geometry: stop ABOVE entry, target below.
        sl = price + cfg.stop_loss_atr_mult * atr_val
        tp = price - cfg.take_profit_atr_mult * atr_val
    else:
        sl = price - cfg.stop_loss_atr_mult * atr_val
        tp = price + cfg.take_profit_atr_mult * atr_val

    # Cross-exchange price validation
    exchange_prices = _get_exchange_prices(pair)
    cross_valid = _validate_cross_exchange(exchange_prices, cfg)

    return Signal(
        pair=pair,
        confluence_score=confluence,
        regime=regime,
        confidence_label=confidence,
        entry_price=price,
        stop_loss=sl,
        take_profit=tp,
        components=combined,
        cross_exchange_valid=cross_valid,
        exchange_prices=exchange_prices,
        atr_value=atr_val,
        direction=direction,
    )


# ═══════════════════════════════════════════════════════════════════════
# EXCHANGE DATA FETCHERS
# ═══════════════════════════════════════════════════════════════════════

def fetch_kraken_ohlc(pair: str, interval: int, limit: int = 100) -> List[Candle]:
    """Fetch OHLC candles — CC proxy first, Kraken direct fallback (canonical module)."""
    if _fetch_ohlc_canonical:
        raw = _fetch_ohlc_canonical(pair, interval, limit, cc_url="http://127.0.0.1:9000")
        return [
            Candle(
                timestamp=float(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in raw
        ]
    # Fallback: direct Kraken (original behaviour when canonical module absent)
    try:
        url = f"{KRAKEN_REST}/0/public/OHLC"
        params = {"pair": pair, "interval": interval}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error") and data["error"]:
            return []
        for key, val in data.get("result", {}).items():
            if key != "last" and isinstance(val, list):
                return [
                    Candle(
                        timestamp=float(r[0]),
                        open=float(r[1]),
                        high=float(r[2]),
                        low=float(r[3]),
                        close=float(r[4]),
                        volume=float(r[6]),
                    )
                    for r in val[-limit:]
                ]
    except Exception as e:
        logger.warning(f"Kraken OHLC fetch failed for {pair}/{interval}: {e}")
    return []


def fetch_kraken_ticker(pair: str) -> float:
    """Fetch current price from Kraken."""
    try:
        url = f"{KRAKEN_REST}/0/public/Ticker"
        params = {"pair": pair}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error") and data["error"]:
            return 0.0
        result = data.get("result", {})
        for key, val in result.items():
            if "c" in val:
                return float(val["c"][0])
        return 0.0
    except Exception:
        return 0.0


def fetch_coingecko_price(pair: str) -> float:
    """Fetch current price from CoinGecko."""
    cg_id = COINGECKO_IDS.get(pair, "")
    if not cg_id:
        return 0.0
    try:
        url = f"{COINGECKO_REST}/simple/price"
        params = {"ids": cg_id, "vs_currencies": "usd"}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return float(data.get(cg_id, {}).get("usd", 0))
    except Exception:
        return 0.0


def _get_exchange_prices(pair: str) -> Dict[str, float]:
    """Get prices from Kraken + CoinGecko."""
    prices = {}
    kraken_p = fetch_kraken_ticker(pair)
    if kraken_p > 0:
        prices["kraken"] = kraken_p
    cg_p = fetch_coingecko_price(pair)
    if cg_p > 0:
        prices["coingecko"] = cg_p
    return prices


def _validate_cross_exchange(prices: Dict[str, float], cfg: Config) -> bool:
    """Validate that exchange prices agree within tolerance."""
    if len(prices) < cfg.min_exchanges_agree:
        return False

    values = list(prices.values())
    if not values:
        return False

    mid = sum(values) / len(values)
    if mid == 0:
        return False

    for p in values:
        divergence = abs(p - mid) / mid * 100
        if divergence > cfg.max_price_divergence_pct:
            return False

    return True


def fetch_pair_data(pair: str, cfg: Config) -> PairData:
    """Fetch all timeframe data for a pair from Kraken (primary) with CoinGecko price validation."""
    pd = PairData(pair=pair)

    for tf_name, tf_cfg in TIMEFRAMES.items():
        # Kraken OHLC
        candles = fetch_kraken_ohlc(pair, tf_cfg["kraken_interval"], tf_cfg["limit"])
        if candles:
            pd.candles[tf_name] = candles

    # Get latest price
    if pd.candles:
        # Use the latest close from shortest timeframe
        for tf in ["5m", "15m", "1h", "4h", "1d"]:
            if tf in pd.candles and pd.candles[tf]:
                pd.last_price = pd.candles[tf][-1].close
                break

    pd.last_update = time.time()
    return pd


# ═══════════════════════════════════════════════════════════════════════
# PAPER TRADING ENGINE
# ═══════════════════════════════════════════════════════════════════════

class PaperTrader:
    """Paper trading engine with slippage and position management.

    P/L is GROSS price movement — no fee simulation. The fleet is a signal
    product; subscribers pay their own exchanges' fees (2026-07-30).
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.capital = cfg.initial_capital
        self.equity = cfg.initial_capital
        # Central portfolio client
        self._portfolio = None
        if cfg.use_central_portfolio and PortfolioClient:
            self._portfolio = PortfolioClient(cfg.command_center_url, bot_id="nexusbrain")
            logger.info("Central portfolio enabled: %s", cfg.command_center_url)
        self._event_pub = None
        if EventPublisher:
            self._event_pub = EventPublisher(cfg.command_center_url, "nexusbrain")
        self._bus = BusListener() if BusListener else None
        # Live Kraken execution (spot only, long only)
        self._kraken = None
        if _KrakenSpotClient and _fc and _fc.is_live():
            self._kraken = _KrakenSpotClient()
            if self._kraken.has_credentials:
                logger.info("LIVE MODE: Kraken spot client initialized")
            else:
                logger.warning("LIVE MODE: No Kraken API credentials — paper fallback")
                self._kraken = None
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = [cfg.initial_capital]
        self.peak_equity = cfg.initial_capital
        self.lock = threading.Lock()
        self._last_close: Dict[str, float] = {}  # {pair: timestamp}
        self._min_reentry_sec = 3600.0  # raised 2026-04-03: was 10s; churn guard kept post fee-removal (rapid re-entry was noise, not signal)
        self._denial_backoff: Dict[str, Dict] = {}  # {pair: {"until": ts, "delay": float}}
        self._DENIAL_BASE_DELAY = 5.0
        self._DENIAL_MAX_DELAY = 300.0

    # ── Position persistence (live mode only; Backtester never arms it) ──
    _POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nexus_positions.json")

    def _save_positions(self):
        """Persist open positions + reservation ids. No-op until restore_positions() arms it."""
        if not getattr(self, "_persist", False):
            return
        try:
            data = {
                "positions": {
                    pair: {
                        "pair": pos.pair,
                        "entry_price": pos.entry_price,
                        "size_usd": pos.size_usd,
                        "stop_loss": pos.stop_loss,
                        "take_profit": pos.take_profit,
                        "signal_score": pos.signal_score,
                        "entry_time": pos.entry_time,
                        "trailing_stop": pos.trailing_stop,
                        "peak_price": pos.peak_price,
                        "reservation_id": pos.reservation_id,
                        "direction": pos.direction,
                    }
                    for pair, pos in self.positions.items()
                },
                "saved_at": time.time(),
            }
            tmp = self._POSITIONS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._POSITIONS_FILE)
        except Exception as e:
            logger.warning(f"Failed to save positions: {e}")

    def restore_positions(self):
        """Live mode only: reload persisted positions after a restart,
        re-reserve pool capital for them, and release reservations booked to
        this bot that nothing references. Positions were memory-only before
        2026-07-31 — a restart mid-trade orphaned the pool reservation forever
        (same family as the TurtleSue pyramid leak).
        """
        self._persist = True
        # Absent, empty and UNREADABLE must not produce the same empty dict.
        # The orphan sweep below releases every pool reservation no local
        # position references — with an empty positions dict that is ALL of
        # them, while the positions stay open with no capital behind them.
        # A truncated write was indistinguishable from a first run.
        # Unreadable therefore fails toward ARMED: sweep skipped, new entries
        # blocked, the bytes preserved for diagnosis. Mirrors TurtleSue's
        # _load_positions, which already carries this defence.
        self._state_unreadable = False
        try:
            with open(self._POSITIONS_FILE) as f:
                saved = json.load(f).get("positions", {})
        except FileNotFoundError:
            saved = {}          # genuinely absent — a real first run
        except Exception as e:
            self._state_unreadable = True
            saved = {}
            logger.error(
                "UNREADABLE POSITION STATE %s: %s — treating open positions "
                "as UNKNOWN. Orphan sweep disabled and new entries blocked "
                "until this is resolved; the file is not overwritten.",
                self._POSITIONS_FILE, e)
            try:
                _q = "%s.corrupt_%d" % (self._POSITIONS_FILE, int(time.time()))
                os.replace(self._POSITIONS_FILE, _q)
                logger.error("Preserved unreadable state as %s", _q)
            except Exception:
                logger.error("Could not quarantine %s", self._POSITIONS_FILE,
                             exc_info=True)
        for pair, d in saved.items():
            try:
                pos = Position(
                    pair=d["pair"], entry_price=d["entry_price"], size_usd=d["size_usd"],
                    stop_loss=d["stop_loss"], take_profit=d["take_profit"],
                    signal_score=d["signal_score"], entry_time=d["entry_time"],
                    trailing_stop=d.get("trailing_stop", 0.0),
                    peak_price=d.get("peak_price", 0.0),
                    reservation_id=d.get("reservation_id", ""),
                    direction=_restored_direction(pair, d),
                )
                pos._kraken_pair = pair.replace("/", "")
                self.positions[pair] = pos
            except Exception as e:
                logger.warning(f"Skipping malformed saved position {pair}: {e}")
        if self._portfolio:
            reservations = self._portfolio.get_reservations()
            if reservations is not None:
                # Re-reserve restored positions whose reservation is gone.
                # is_reentry=True: direction policy must not refuse capital
                # for a position that is already open.
                for pair, pos in list(self.positions.items()):
                    if pos.reservation_id and pos.reservation_id in reservations:
                        continue
                    _display = PAIR_DISPLAY.get(pair, pair)
                    ok, result = self._portfolio.reserve(
                        _display, pos.direction, pos.size_usd, is_reentry=True)
                    if ok:
                        pos.reservation_id = result
                        logger.info(f"Re-reserved {_display}: {result}")
                    else:
                        logger.warning(f"Re-reserve failed for {_display} ({result}) — dropping restored position")
                        del self.positions[pair]
                # Orphan sweep: pool reservations booked to this bot that no
                # local position references — capital the pool holds forever
                # otherwise. (Sweeps the snapshot taken above, so rids created
                # by the re-reserve loop are never candidates.)
                if self._state_unreadable:
                    logger.error(
                        "Orphan sweep SKIPPED — position state was unreadable, "
                        "so an empty local set is not evidence the pool's "
                        "reservations are orphaned. Held capital is "
                        "recoverable; capital released under a live position "
                        "is not.")
                else:
                    local_rids = {p.reservation_id for p in self.positions.values() if p.reservation_id}
                    for rid, res in reservations.items():
                        if res.get("bot_id") == self._portfolio.bot_id and rid not in local_rids:
                            ok, reason = self._portfolio.release(rid, pnl=0.0)
                            logger.warning(f"Orphaned reservation {rid}: release {'ok' if ok else f'FAILED ({reason})'}")
        if self.positions:
            logger.info(f"Restored {len(self.positions)} open position(s) from disk")
        # Never overwrite state we could not read — the quarantined copy is
        # the only remaining record of what was open.
        if not self._state_unreadable:
            self._save_positions()

    def can_open(self) -> bool:
        """Check if we can open a new position."""
        # Unknown state fails toward ARMED. With positions unreadable, this
        # bot cannot tell "flat" from "holding positions it forgot", and
        # opening more would double-commit capital the pool still books.
        if getattr(self, "_state_unreadable", False):
            return False
        return len(self.positions) < self.cfg.max_positions

    def open_position(self, signal: Signal) -> Optional[Position]:
        """Open a new paper position based on signal."""
        with self.lock:
            if signal.pair in self.positions:
                return None
            if not self.can_open():
                return None
            if signal.confidence_label == ConfidenceLabel.REJECT:
                return None
            # Minimum re-entry cooldown — prevent sub-second open/close cycles
            last = self._last_close.get(signal.pair, 0)
            if time.time() - last < self._min_reentry_sec:
                return None
            # Kraken spot cannot short — shorts are paper/signal-only.
            if signal.direction == "SHORT" and self._kraken:
                logger.info(f"SKIP LIVE SHORT {PAIR_DISPLAY.get(signal.pair, signal.pair)}: spot account cannot short")
                return None

            # Position sizing: risk-based
            risk_amount = self.equity * (self.cfg.risk_per_trade_pct / 100)
            risk_distance = abs(signal.entry_price - signal.stop_loss)
            if risk_distance <= 0:
                return None

            # Risk-based size, kept UNCAPPED here so the bus multipliers
            # below apply to it. _risk_size is what risk_per_trade_pct and
            # the stop distance actually ask for; max_size is a ceiling.
            #
            # Note this ceiling binds in every realistic market: the cap
            # fails to bind only when 0.01/(2*atr_frac) <= 0.05, i.e.
            # atr_frac >= 10%. Live 1h ATR on the traded pairs measured
            # 0.30%-0.78% on 2026-08-13, so raw risk size is ~13x the cap
            # and the 5% cap is the binding constraint for every trade.
            # That is a real policy question — per-trade RISK then varies
            # with volatility instead of being held constant — but it is
            # left as-is deliberately: changing risk_per_trade_pct or the
            # cap changes position sizes on a live fleet and is the
            # operator's call, not a silent fix. What IS fixed here is the
            # multiplier erasure below, which nobody chose.
            _risk_size = risk_amount / risk_distance * signal.entry_price
            max_size = self.equity * self.cfg.max_position_pct
            size_usd = min(_risk_size, max_size)

            # Bus intelligence — fleet context modifies confidence
            _bus_mult = 1.0
            if self._bus:
                try:
                    if self._bus.emergency_active(max_age=300):
                        logger.info(f"BUS SKIP {signal.pair}: emergency active")
                        return None
                    _a = self._bus.aegis_regime()
                    if _a == "DEFENSIVE":
                        _bus_mult *= 0.7
                    elif _a == "DEPLOY":
                        _bus_mult *= 1.2
                    _wt = self._bus.whale_tier(signal.pair, max_age=300)
                    if _wt == "EXTREME":
                        _bus_mult *= 1.3
                    elif _wt == "HIGH":
                        _bus_mult *= 1.1
                    _conv = self._bus.convergent_signals(signal.pair, max_age=60)
                    if _conv and _conv.get("bot_count", 0) >= 3:
                        _bus_mult *= 1.3
                    # NEWTON: Force alignment — BULL corroborates longs,
                    # BEAR corroborates shorts; the opposing force shrinks size.
                    _newton = self._bus.newton_force(signal.pair, max_age=120)
                    if _newton:
                        _with = "BULL" if signal.direction == "LONG" else "BEAR"
                        _against = "BEAR" if signal.direction == "LONG" else "BULL"
                        if _newton["direction"] == _with:
                            _bus_mult *= 1 + min(0.3, _newton["inertia"] * 0.4)
                        elif _newton["direction"] == _against:
                            _bus_mult *= 0.7
                    # NEWTON: Reaction prediction
                    _reactions = self._bus.newton_reactions(signal.pair, max_age=120)
                    if _reactions:
                        for _r in _reactions:
                            if _r["expected_direction"] == "SAME":
                                _bus_mult *= 1.2
                                break
                    # EUCLID: Approaching resistance = reduce long size;
                    # approaching support = reduce short size (mirror).
                    _euclid = self._bus.euclid_levels(signal.pair, max_age=120)
                    _lvl_block = "RESISTANCE_APPROACHING" if signal.direction == "LONG" else "SUPPORT_APPROACHING"
                    for _le in _euclid:
                        _ld = _le.get("data", {})
                        if _ld.get("type") == _lvl_block:
                            _bus_mult *= 0.7
                            break
                    # CHRONOS: temporal bias — soft influence only, never a
                    # hard block. A fresh (<1h) statistically-gated TIME_ANOMALY
                    # opposing this trade's direction shaves 25% off size;
                    # agreement is log-only (stay conservative). SESSION_OVERLAP
                    # fresh (<15m) is a context log line only, no size change.
                    _temporal = self._bus.chronos_temporal(max_age=3600)
                    _anomaly = _temporal.get("time_anomaly") if _temporal else None
                    if _anomaly:
                        _anomaly_dir = "LONG" if _anomaly.get("direction") == "bullish" else "SHORT"
                        if _anomaly_dir != signal.direction:
                            _bus_mult *= 0.75
                            logger.info(
                                f"TEMPORAL OPPOSE {PAIR_DISPLAY.get(signal.pair, signal.pair)}: "
                                f"TIME_ANOMALY {_anomaly.get('direction')} (n={_anomaly.get('n')}, "
                                f"bias={_anomaly.get('bias_pct')}%) opposes {signal.direction} — size x0.75"
                            )
                        else:
                            logger.info(
                                f"TEMPORAL AGREE {PAIR_DISPLAY.get(signal.pair, signal.pair)}: "
                                f"TIME_ANOMALY {_anomaly.get('direction')} (n={_anomaly.get('n')}, "
                                f"bias={_anomaly.get('bias_pct')}%) agrees with {signal.direction} — no size change"
                            )
                    for _se in (_temporal.get("session_events") or []) if _temporal else []:
                        if _se.get("type") == "SESSION_OVERLAP" and (time.time() - _se.get("ts", 0)) < 900:
                            logger.info(
                                f"TEMPORAL CONTEXT {PAIR_DISPLAY.get(signal.pair, signal.pair)}: "
                                f"SESSION_OVERLAP {_se.get('window')} — high volatility window"
                            )
                            break

                    _bus_mult = max(0.3, min(2.0, _bus_mult))
                except Exception:
                    _bus_mult = 1.0

            # Multiply the CAPPED base, then cap again.
            #
            # size_usd is already min(_risk_size, max_size) from above.
            # Because the cap binds in every realistic market (see there),
            # the base IS max_size, and that makes the upward multipliers
            # structurally unreachable: raising size above the base would
            # mean exceeding max_position_pct, which is a deliberate risk
            # limit, not something a conviction signal may override.
            #
            # So: downward multipliers apply in full (x0.3 -> 30% of the
            # cap), upward ones cannot raise size past the cap. That is the
            # honest behaviour of the current config rather than a silent
            # policy change — and unlike before, the log below says so
            # instead of claiming an increase that did not happen.
            #
            # Getting this order wrong breaks one direction or the other,
            # and I did both while writing this: `_risk_size * _bus_mult`
            # erases the DOWNWARD multipliers (risk_size is ~25x the cap at
            # live ATR, so even x0.3 still exceeds it) — strictly worse,
            # since those are the safety-relevant ones.
            _requested = size_usd * _bus_mult
            size_usd = min(_requested, max_size)  # cap is the ceiling, not the value
            # Log what was APPLIED, not what was asked for. The old line
            # printed "size x1.30" from _bus_mult alone, at a point where
            # the cap had already discarded the increase — a log asserting
            # an effect that did not occur.
            if _bus_mult != 1.0:
                _capped = " (capped at %.1f%% of equity)" % (
                    self.cfg.max_position_pct * 100) if _requested > max_size else ""
                logger.info(
                    "BUS CONTEXT %s: size x%.2f requested -> $%.2f%s",
                    PAIR_DISPLAY.get(signal.pair, signal.pair), _bus_mult,
                    size_usd, _capped)

            # Size floor: $100 minimum trade size. Threshold unchanged after
            # fee removal — dust positions produce signals too small to be
            # actionable for subscribers.
            if size_usd < 100:
                return None

            # Central portfolio: reserve capital before opening
            _rid = ""
            _display_pair = PAIR_DISPLAY.get(signal.pair, signal.pair)
            if self._portfolio:
                # Check denial backoff — skip if cooling down
                _db = self._denial_backoff.get(signal.pair)
                if _db and time.time() < _db["until"]:
                    return None
                sl_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price * 100 if signal.entry_price > 0 else 2.0
                ok, result = self._portfolio.reserve(_display_pair, signal.direction, size_usd, stop_loss_pct=sl_pct)
                if not ok:
                    prev = self._denial_backoff.get(signal.pair)
                    delay = min((prev["delay"] * 2) if prev else self._DENIAL_BASE_DELAY, self._DENIAL_MAX_DELAY)
                    self._denial_backoff[signal.pair] = {"until": time.time() + delay, "delay": delay}
                    logger.info(f"Portfolio denied {PAIR_DISPLAY.get(signal.pair, signal.pair)}: {result} (backoff {delay:.0f}s)")
                    return None
                # Clear backoff on success
                self._denial_backoff.pop(signal.pair, None)
                _rid = result

            # Live execution: buy on Kraken spot
            entry_price = signal.entry_price
            _kraken_pair = signal.pair.replace("/", "")  # BTC/USD → BTCUSD
            if self._kraken:
                qty = size_usd / signal.entry_price
                # LIMIT ONLY (fleet policy) — marketable limit, never market.
                ok, txid = self._kraken.buy(
                    _kraken_pair, qty, price=_limit_price(signal.entry_price, "BUY"))
                if not ok:
                    logger.warning(f"LIVE BUY FAILED {signal.pair}: {txid}")
                    if self._portfolio and _rid:
                        self._portfolio.release(_rid, pnl=0.0)
                    return None
                import time as _t; _t.sleep(1.5)
                entry_price = self._kraken.get_fill_price(txid, signal.entry_price)
                logger.info(f"LIVE BUY {signal.pair} qty={qty:.6f} @ {entry_price:.4f} txid={txid}")
            else:
                # Paper fill with adverse slippage: buys fill higher,
                # short-sale entries fill lower.
                slippage = signal.entry_price * (self.cfg.slippage_bps / 10000)
                if signal.direction == "SHORT":
                    entry_price = signal.entry_price - slippage
                else:
                    entry_price = signal.entry_price + slippage

            # No entry fee — P/L is gross (signal product; subscribers pay
            # their own exchanges' fees).

            pos = Position(
                pair=signal.pair,
                entry_price=entry_price,
                size_usd=size_usd,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                signal_score=signal.confluence_score,
                entry_time=time.time(),
                reservation_id=_rid,
                direction=signal.direction,
            )
            pos._kraken_pair = _kraken_pair  # store for exit
            self.positions[signal.pair] = pos
            self._save_positions()
            _mode = "LIVE" if self._kraken else "PAPER"
            logger.info(f"OPEN [{_mode}] {signal.direction} {PAIR_DISPLAY.get(signal.pair, signal.pair)} @ {entry_price:.4f} size=${size_usd:.2f} score={signal.confluence_score:.3f}")
            if self._event_pub:
                try:
                    self._event_pub.emit("TRADE_OPEN", {
                        "pair": PAIR_DISPLAY.get(signal.pair, signal.pair),
                        "direction": signal.direction, "entry": round(entry_price, 4),
                        "size": round(size_usd, 2), "score": round(signal.confluence_score, 4),
                        "regime": signal.regime.value,
                        "sl": round(signal.stop_loss, 4),
                    })
                except Exception:
                    pass
            return pos

    def check_exits(self, pair: str, current_price: float) -> Optional[Trade]:
        """Check if any position should be exited."""
        with self.lock:
            if pair not in self.positions:
                return None
            pos = self.positions[pair]

            exit_reason = None
            exit_price = current_price

            # Minimum hold time: churn guard against 0-5s noise exits.
            # Threshold unchanged after fee removal — sub-minute flips were
            # noise, not signal, even measured gross.
            MIN_HOLD_SECONDS = 60
            hold_secs = time.time() - pos.entry_time
            _is_short = pos.direction == "SHORT"
            # ATR estimate recovered from the stop distance (works both
            # directions: the stop is above entry for shorts, below for longs).
            _atr_est = abs(pos.entry_price - pos.stop_loss) / self.cfg.stop_loss_atr_mult

            # Update peak (best-so-far) price for trailing stop.
            # Long: best = highest seen, trail sits below and ratchets up.
            # Short: best = lowest seen, trail sits above and ratchets down.
            if _is_short:
                if current_price < pos.peak_price:
                    pos.peak_price = current_price
                    if pos.trailing_stop > 0:
                        new_trail = current_price + self.cfg.trailing_stop_atr_mult * _atr_est
                        pos.trailing_stop = min(pos.trailing_stop, new_trail)
            else:
                if current_price > pos.peak_price:
                    pos.peak_price = current_price
                    if pos.trailing_stop > 0:
                        new_trail = current_price - self.cfg.trailing_stop_atr_mult * _atr_est
                        pos.trailing_stop = max(pos.trailing_stop, new_trail)

            # Stop loss (always honored regardless of hold time).
            # Short stop is ABOVE entry and triggers on price rising to it.
            if (current_price >= pos.stop_loss) if _is_short else (current_price <= pos.stop_loss):
                exit_reason = ExitReason.STOP_LOSS
                exit_price = pos.stop_loss

            # Block non-SL exits before minimum hold time
            elif hold_secs < MIN_HOLD_SECONDS:
                return None

            # Take profit (below entry for shorts)
            elif (current_price <= pos.take_profit) if _is_short else (current_price >= pos.take_profit):
                exit_reason = ExitReason.TAKE_PROFIT

            # Trailing stop (activated after 50% of TP distance)
            elif pos.trailing_stop > 0 and (
                (current_price >= pos.trailing_stop) if _is_short else (current_price <= pos.trailing_stop)
            ):
                exit_reason = ExitReason.TRAILING_STOP
                exit_price = pos.trailing_stop

            # Time exit
            elif time.time() - pos.entry_time > self.cfg.max_hold_hours * 3600:
                exit_reason = ExitReason.TIME_EXIT

            # Activate trailing stop if price moved 50%+ toward TP
            if exit_reason is None:
                tp_dist = (pos.entry_price - pos.take_profit) if _is_short else (pos.take_profit - pos.entry_price)
                if tp_dist > 0:
                    progress = ((pos.entry_price - current_price) if _is_short else (current_price - pos.entry_price)) / tp_dist
                    if progress >= 0.5 and pos.trailing_stop == 0:
                        if _is_short:
                            pos.trailing_stop = current_price + self.cfg.trailing_stop_atr_mult * _atr_est
                        else:
                            pos.trailing_stop = current_price - self.cfg.trailing_stop_atr_mult * _atr_est

            if exit_reason is None:
                return None

            # Live execution: sell on Kraken spot (longs only — shorts are
            # never opened live, see open_position guard)
            qty = pos.size_usd / pos.entry_price
            if self._kraken and not _is_short:
                _kp = getattr(pos, '_kraken_pair', pair.replace("/", ""))
                # LIMIT ONLY (fleet policy) — marketable limit, never market.
                ok, txid = self._kraken.sell(
                    _kp, qty, price=_limit_price(current_price, "SELL"))
                if ok:
                    import time as _t; _t.sleep(1.5)
                    exit_price = self._kraken.get_fill_price(txid, exit_price)
                    logger.info(f"LIVE SELL {pair} qty={qty:.6f} @ {exit_price:.4f} txid={txid} reason={exit_reason.value}")
                else:
                    logger.warning(f"LIVE SELL FAILED {pair}: {txid} — using paper price {exit_price}")
            else:
                # Paper: apply adverse slippage to exit. Long exit sells
                # (fills lower); short exit buys back (fills higher).
                slippage = exit_price * (self.cfg.slippage_bps / 10000)
                if _is_short:
                    exit_price += slippage
                else:
                    exit_price -= slippage

            # Calculate P&L — shorts profit when price falls
            if _is_short:
                pnl_usd = qty * (pos.entry_price - exit_price)
                pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100
            else:
                pnl_usd = qty * (exit_price - pos.entry_price)
                pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
            # No exit fee — P/L stays gross (signal product; subscribers
            # pay their own exchanges' fees).

            self.capital += pnl_usd
            self.equity = self.capital + sum(
                p.size_usd * (1 + (
                    (p.entry_price - current_price) if p.direction == "SHORT"
                    else (current_price - p.entry_price)
                ) / p.entry_price)
                for p_name, p in self.positions.items() if p_name != pair
            )

            trade = Trade(
                pair=pair,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                exit_reason=exit_reason,
                signal_score=pos.signal_score,
                entry_time=pos.entry_time,
                exit_time=time.time(),
                size_usd=pos.size_usd,
                direction=pos.direction,
            )
            self.trades.append(trade)

            if _expectancy:
                try:
                    _expectancy.record_trade(
                        bot_id='nexusbrain',
                        pair=PAIR_DISPLAY.get(pair, pair),
                        direction=pos.direction,
                        entry_price=pos.entry_price,
                        exit_price=exit_price,
                        size_usd=pos.size_usd,
                        duration=time.time() - pos.entry_time,
                    )
                except Exception as _e:
                    # Swallowed, this drops the trade from the DURABLE store
                    # while self.trades.append() above already booked it
                    # locally — the two sources diverge with nothing saying
                    # which is short.
                    logger.error(
                        "EXPECTANCY RECORD FAILED for %s: %s: %s — trade is "
                        "in this bot's tally but NOT in the durable store",
                        pair, type(_e).__name__, _e)

            # Central portfolio: release reservation
            if self._portfolio and pos.reservation_id:
                # Report outcome to signal aggregator for learning
                try:
                    import urllib.request as urlreq
                    # was `cfg.command_center_url` — a NameError swallowed by
                    # this except, so no outcome ever reached the aggregator
                    url = f"{self.cfg.command_center_url}/api/signals/outcome"
                    fees = 0.0  # gross P/L reporting — fees removed fleet-wide 2026-07-30; field kept for API shape
                    data = json.dumps({
                        "bot_id": "nexusbrain",
                        "pair": pair,
                        "direction": pos.direction,
                        "won": pnl_usd > 0,
                        "pnl": float(pnl_usd),
                        "fees": float(fees)
                    }).encode("utf-8")
                    req = urlreq.Request(url, data=data, headers={"Content-Type": "application/json"})
                    urlreq.urlopen(req, timeout=3)
                except Exception:
                    pass
                
                self._portfolio.release(
                    pos.reservation_id, pnl=pnl_usd,
                    entry_price=float(pos.entry_price),
                    exit_price=float(exit_price),
                )

            del self.positions[pair]
            self._last_close[pair] = time.time()
            self._save_positions()

            self._update_equity()
            logger.info(f"CLOSE {pos.direction} {PAIR_DISPLAY.get(pair, pair)} @ {exit_price:.4f} P/L=${pnl_usd:+.2f} ({pnl_pct:+.2f}%) reason={exit_reason.value}")
            if self._event_pub:
                try:
                    _risk_usd = abs(pos.entry_price - pos.stop_loss) / pos.entry_price * pos.size_usd if pos.entry_price > 0 else 0
                    _r = round(pnl_usd / _risk_usd, 2) if _risk_usd > 0 else 0
                    self._event_pub.emit("TRADE_CLOSE", {
                        "pair": PAIR_DISPLAY.get(pair, pair),
                        "direction": pos.direction, "exit_price": round(exit_price, 4),
                        "pnl": round(pnl_usd, 2), "exit_reason": exit_reason.value,
                        "r": _r,
                    })
                except Exception:
                    pass
            return trade

    def _update_equity(self):
        """Update equity curve."""
        self.equity_curve.append(round(self.equity, 2))
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        if len(self.equity_curve) > 5000:
            self.equity_curve = self.equity_curve[-5000:]

    def get_stats(self) -> dict:
        """Compute performance statistics."""
        if not self.trades:
            return {
                "total_trades": 0,
                "winners": 0,
                "losers": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "return_pct": 0.0,
                "sharpe_ratio": 0.0,
                "max_drawdown_pct": 0.0,
                "profit_factor": 0.0,
                "avg_winner": 0.0,
                "avg_loser": 0.0,
                "initial_capital": self.cfg.initial_capital,
                "final_equity": self.equity,
            }

        winners = [t for t in self.trades if t.pnl_usd > 0]
        losers = [t for t in self.trades if t.pnl_usd <= 0]

        total_pnl = sum(t.pnl_usd for t in self.trades)
        gross_profit = sum(t.pnl_usd for t in winners) if winners else 0.0
        gross_loss = abs(sum(t.pnl_usd for t in losers)) if losers else 0.0

        win_rate = len(winners) / len(self.trades) if self.trades else 0.0
        avg_winner = gross_profit / len(winners) if winners else 0.0
        avg_loser = -(gross_loss / len(losers)) if losers else 0.0

        # Profit factor -- safe against division by zero, clamp inf to 0.0
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        else:
            profit_factor = 0.0

        # Ensure profit_factor is never inf
        profit_factor = sf(profit_factor)

        # Sharpe ratio from trade returns
        returns = [t.pnl_pct for t in self.trades]
        if len(returns) > 1:
            mean_r = sum(returns) / len(returns)
            var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
            std_r = math.sqrt(var_r) if var_r > 0 else 1.0
            sharpe = mean_r / std_r * math.sqrt(365)
        else:
            sharpe = 0.0
        sharpe = sf(sharpe)

        # Max drawdown from equity curve
        max_dd = 0.0
        peak = self.equity_curve[0] if self.equity_curve else self.cfg.initial_capital
        for eq in self.equity_curve:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (eq - peak) / peak * 100
                if dd < max_dd:
                    max_dd = dd

        return_pct = (self.equity - self.cfg.initial_capital) / self.cfg.initial_capital * 100

        return {
            "total_trades": len(self.trades),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(sf(win_rate), 4),
            "total_pnl": round(sf(total_pnl), 2),
            "return_pct": round(sf(return_pct), 4),
            "sharpe_ratio": round(sf(sharpe), 4),
            "max_drawdown_pct": round(sf(max_dd), 4),
            "profit_factor": round(sf(profit_factor), 4),
            "avg_winner": round(sf(avg_winner), 2),
            "avg_loser": round(sf(avg_loser), 2),
            "initial_capital": self.cfg.initial_capital,
            "final_equity": round(sf(self.equity), 2),
        }


# ═══════════════════════════════════════════════════════════════════════
# SQLITE PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════

class Database:
    """SQLite persistence layer for trades and signals."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT NOT NULL,
                entry_price REAL,
                exit_price REAL,
                pnl_usd REAL,
                pnl_pct REAL,
                exit_reason TEXT,
                signal_score REAL,
                entry_time REAL,
                exit_time REAL,
                size_usd REAL,
                direction TEXT DEFAULT 'LONG'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT NOT NULL,
                confluence_score REAL,
                regime TEXT,
                confidence TEXT,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                cross_valid INTEGER,
                timestamp REAL,
                direction TEXT DEFAULT 'LONG'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                equity REAL,
                timestamp REAL
            )
        """)
        # Migration: pre-bidirectional databases lack the direction column.
        # Every row before the migration was a LONG (the DEFAULT is truthful).
        for _tbl in ("trades", "signals"):
            try:
                c.execute(f"ALTER TABLE {_tbl} ADD COLUMN direction TEXT DEFAULT 'LONG'")
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
        conn.close()

    def save_trade(self, trade: Trade):
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                "INSERT INTO trades (pair, entry_price, exit_price, pnl_usd, pnl_pct, exit_reason, signal_score, entry_time, exit_time, size_usd, direction) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (trade.pair, trade.entry_price, trade.exit_price, trade.pnl_usd, trade.pnl_pct,
                 trade.exit_reason.value, trade.signal_score, trade.entry_time, trade.exit_time, trade.size_usd,
                 trade.direction),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB save_trade error: {e}")

    def save_signal(self, signal: Signal):
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                "INSERT INTO signals (pair, confluence_score, regime, confidence, entry_price, stop_loss, take_profit, cross_valid, timestamp, direction) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (signal.pair, signal.confluence_score, signal.regime.value, signal.confidence_label.value,
                 signal.entry_price, signal.stop_loss, signal.take_profit, int(signal.cross_exchange_valid), signal.timestamp,
                 signal.direction),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB save_signal error: {e}")

    def save_equity(self, equity: float):
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                "INSERT INTO equity_snapshots (equity, timestamp) VALUES (?,?)",
                (equity, time.time()),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB save_equity error: {e}")

    def load_trades(self) -> List[Trade]:
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT pair, entry_price, exit_price, pnl_usd, pnl_pct, exit_reason, signal_score, entry_time, exit_time, size_usd, direction FROM trades ORDER BY exit_time DESC LIMIT 500")
            rows = c.fetchall()
            conn.close()
            trades = []
            for r in rows:
                try:
                    reason = ExitReason(r[5])
                except ValueError:
                    reason = ExitReason.TIME_EXIT
                trades.append(Trade(
                    pair=r[0], entry_price=r[1], exit_price=r[2], pnl_usd=r[3], pnl_pct=r[4],
                    exit_reason=reason, signal_score=r[6], entry_time=r[7], exit_time=r[8], size_usd=r[9] or 0.0,
                    direction=r[10] or "LONG",
                ))
            return trades
        except Exception as e:
            logger.error(f"DB load_trades error: {e}")
            return []

    def load_equity_curve(self) -> List[float]:
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("SELECT equity FROM equity_snapshots ORDER BY timestamp ASC")
            rows = c.fetchall()
            conn.close()
            return [r[0] for r in rows]
        except Exception as e:
            logger.error(f"DB load_equity error: {e}")
            return []


# ═══════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════

class Backtester:
    """Historical backtest using fetched OHLC data."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.trader = PaperTrader(cfg)
        self.signals_generated: List[Signal] = []
        self.all_pair_data: Dict[str, PairData] = {}

    def run(self, pairs: List[str], progress_callback=None) -> dict:
        """Run backtest across all pairs."""
        logger.info(f"Backtest starting: {len(pairs)} pairs, {self.cfg.backtest_days} days, ${self.cfg.initial_capital} capital")

        total_steps = len(pairs)
        for idx, pair in enumerate(pairs):
            if progress_callback:
                progress_callback(idx, total_steps, pair)

            try:
                pd = self._fetch_backtest_data(pair)
                if pd and pd.candles:
                    self.all_pair_data[pair] = pd
                    self._simulate_pair(pair, pd)
            except Exception as e:
                logger.warning(f"Backtest error for {pair}: {e}")

            # Rate limiting (reduced for faster dashboard boot)
            time.sleep(0.2)

        if progress_callback:
            progress_callback(total_steps, total_steps, "complete")

        return self._build_results()

    def _fetch_backtest_data(self, pair: str) -> Optional[PairData]:
        """Fetch historical data for backtesting."""
        pd = PairData(pair=pair)

        for tf_name, tf_cfg in TIMEFRAMES.items():
            # Kraken OHLC
            candles = fetch_kraken_ohlc(pair, tf_cfg["kraken_interval"], tf_cfg["limit"])
            if candles:
                pd.candles[tf_name] = candles

        if pd.candles:
            for tf in ["5m", "15m", "1h", "4h", "1d"]:
                if tf in pd.candles and pd.candles[tf]:
                    pd.last_price = pd.candles[tf][-1].close
                    break
            pd.last_update = time.time()

        return pd if pd.candles else None

    def _simulate_pair(self, pair: str, pd: PairData):
        """Simulate trading on a pair's historical data using walk-forward on 1h candles."""
        candles_1h = pd.candles.get("1h", [])
        if len(candles_1h) < 50:
            return

        # Walk forward through 1h candles, check exits every candle, signals every 4h
        for i in range(50, len(candles_1h)):
            current_candle = candles_1h[i]
            price = current_candle.close

            # Check exits every candle (important for stops)
            self.trader.check_exits(pair, price)

            # Generate signals every 4 candles (4h) to keep backtest fast
            if i % 4 != 0:
                continue

            # Build a temporary PairData with candles up to current point
            tmp_pd = PairData(pair=pair, last_price=price, last_update=current_candle.timestamp)
            for tf_name in pd.candles:
                if tf_name == "1h":
                    tmp_pd.candles[tf_name] = candles_1h[:i + 1]
                else:
                    tmp_pd.candles[tf_name] = pd.candles[tf_name]

            signal = generate_signal(pair, tmp_pd, self.cfg)
            if signal:
                self.signals_generated.append(signal)
                if signal.confidence_label != ConfidenceLabel.REJECT and pair not in self.trader.positions:
                    self.trader.open_position(signal)

            self.trader._update_equity()

    def _build_results(self) -> dict:
        """Build complete backtest results."""
        stats = self.trader.get_stats()
        return {
            "performance": stats,
            "equity_curve": self.trader.equity_curve,
            "recent_signals": [s.to_dict() for s in self.signals_generated[-50:]],
            "recent_trades": [t.to_dict() for t in self.trader.trades[-100:]],
            "open_positions": [p.to_dict() for p in self.trader.positions.values()],
            "config": {
                "max_positions": self.cfg.max_positions,
                "min_confluence": self.cfg.min_confluence,
                "stop_loss_atr_mult": self.cfg.stop_loss_atr_mult,
                "take_profit_atr_mult": self.cfg.take_profit_atr_mult,
                "fee_rate": self.cfg.fee_rate,
                "initial_capital": self.cfg.initial_capital,
                "timeframes": list(TIMEFRAMES.keys()),
                "exchanges": ["kraken", "coingecko"],
            },
        }


# ═══════════════════════════════════════════════════════════════════════
# ANSI TERMINAL DISPLAY
# ═══════════════════════════════════════════════════════════════════════

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_GREEN = "\033[92m"
ANSI_RED = "\033[91m"
ANSI_YELLOW = "\033[93m"
ANSI_CYAN = "\033[96m"
ANSI_MAGENTA = "\033[95m"
ANSI_WHITE = "\033[97m"
ANSI_CLEAR = "\033[2J\033[H"


def format_price(price: float) -> str:
    """Smart price formatting."""
    if price == 0:
        return "-.--"
    if price >= 1000:
        return f"{price:,.2f}"
    elif price >= 1:
        return f"{price:.4f}"
    elif price >= 0.01:
        return f"{price:.6f}"
    else:
        return f"{price:.8f}"


def regime_color(regime: Regime) -> str:
    if regime == Regime.TREND_UP:
        return ANSI_GREEN
    elif regime == Regime.TREND_DOWN:
        return ANSI_RED
    elif regime == Regime.VOLATILE:
        return ANSI_YELLOW
    return ANSI_DIM


def confidence_color(label: ConfidenceLabel) -> str:
    if label == ConfidenceLabel.HIGH:
        return ANSI_GREEN
    elif label == ConfidenceLabel.MODERATE:
        return ANSI_CYAN
    elif label == ConfidenceLabel.LOW:
        return ANSI_YELLOW
    return ANSI_RED


# ═══════════════════════════════════════════════════════════════════════
# CLI COMMANDS
# ═══════════════════════════════════════════════════════════════════════

def cmd_scan(args, cfg: Config):
    """One-shot scan all pairs and display signals."""
    pairs = _resolve_pairs(args)

    print(f"\n{ANSI_CYAN}{ANSI_BOLD}{'='*70}")
    print(f"  NEXUS BRAIN v{VERSION} -- Multi-TF Cross-Exchange Signal Scanner")
    print(f"{'='*70}{ANSI_RESET}\n")
    print(f"  Scanning {len(pairs)} pairs across {len(TIMEFRAMES)} timeframes...\n")

    all_signals = []
    for i, pair in enumerate(pairs):
        display = PAIR_DISPLAY.get(pair, pair)
        print(f"  [{i+1}/{len(pairs)}] {display}...", end="", flush=True)
        try:
            pd = fetch_pair_data(pair, cfg)
            if pd and pd.candles:
                signal = generate_signal(pair, pd, cfg)
                if signal:
                    all_signals.append(signal)
                    rc = regime_color(signal.regime)
                    cc = confidence_color(signal.confidence_label)
                    xv = f"{ANSI_GREEN}YES{ANSI_RESET}" if signal.cross_exchange_valid else f"{ANSI_RED}NO{ANSI_RESET}"
                    dc = ANSI_RED if signal.direction == "SHORT" else ANSI_GREEN
                    print(f" {dc}{signal.direction}{ANSI_RESET} score={signal.confluence_score:.3f} {cc}{signal.confidence_label.value}{ANSI_RESET} {rc}{signal.regime.value}{ANSI_RESET} XVal={xv}")
                else:
                    print(f" {ANSI_DIM}no signal{ANSI_RESET}")
            else:
                print(f" {ANSI_RED}no data{ANSI_RESET}")
        except Exception as e:
            print(f" {ANSI_RED}error: {e}{ANSI_RESET}")
        time.sleep(0.3)

    # Summary
    all_signals.sort(key=lambda s: s.confluence_score, reverse=True)
    print(f"\n{ANSI_BOLD}  Top Signals:{ANSI_RESET}")
    print(f"  {'Pair':<14} {'Dir':<6} {'Score':>6} {'Confidence':<12} {'Regime':<12} {'Entry':>12} {'SL':>12} {'TP':>12} {'XVal':>5}")
    print(f"  {'-'*92}")

    for sig in all_signals[:10]:
        display = PAIR_DISPLAY.get(sig.pair, sig.pair)
        rc = regime_color(sig.regime)
        cc = confidence_color(sig.confidence_label)
        dc = ANSI_RED if sig.direction == "SHORT" else ANSI_GREEN
        xv = "Y" if sig.cross_exchange_valid else "N"
        print(
            f"  {display:<14} {dc}{sig.direction:<6}{ANSI_RESET} {sig.confluence_score:>6.3f} "
            f"{cc}{sig.confidence_label.value:<12}{ANSI_RESET} "
            f"{rc}{sig.regime.value:<12}{ANSI_RESET} "
            f"{format_price(sig.entry_price):>12} "
            f"{format_price(sig.stop_loss):>12} "
            f"{format_price(sig.take_profit):>12} "
            f"{xv:>5}"
        )

    actionable = [s for s in all_signals if s.confidence_label != ConfidenceLabel.REJECT]
    print(f"\n  {len(all_signals)} signals scanned, {len(actionable)} actionable (>= {cfg.min_confluence} confluence)\n")


def cmd_backtest(args, cfg: Config):
    """Run historical backtest."""
    pairs = _resolve_pairs(args)

    print(f"\n{ANSI_CYAN}{ANSI_BOLD}{'='*70}")
    print(f"  NEXUS BRAIN v{VERSION} -- Backtest Engine")
    print(f"{'='*70}{ANSI_RESET}\n")
    print(f"  Capital: ${cfg.initial_capital:,.2f}")
    print(f"  Pairs:   {len(pairs)}")
    print(f"  TFs:     {', '.join(TIMEFRAMES.keys())}")
    print()

    def progress(current, total, pair):
        if current < total:
            display = PAIR_DISPLAY.get(pair, pair)
            print(f"  [{current+1}/{total}] Backtesting {display}...", flush=True)
        else:
            print("\n  Backtest complete.\n")

    bt = Backtester(cfg)
    results = bt.run(pairs, progress_callback=progress)

    # Print results
    stats = results["performance"]
    pnl_color = ANSI_GREEN if stats["total_pnl"] >= 0 else ANSI_RED

    print(f"  {ANSI_BOLD}Performance Summary:{ANSI_RESET}")
    print(f"  {'─'*50}")
    print(f"  Total Trades:    {stats['total_trades']}")
    print(f"  Winners:         {ANSI_GREEN}{stats['winners']}{ANSI_RESET}")
    print(f"  Losers:          {ANSI_RED}{stats['losers']}{ANSI_RESET}")
    print(f"  Win Rate:        {stats['win_rate']*100:.1f}%")
    print(f"  Total P&L:       {pnl_color}${stats['total_pnl']:+,.2f}{ANSI_RESET}")
    print(f"  Return:          {pnl_color}{stats['return_pct']:+.2f}%{ANSI_RESET}")
    print(f"  Sharpe Ratio:    {stats['sharpe_ratio']:.3f}")
    print(f"  Max Drawdown:    {ANSI_RED}{stats['max_drawdown_pct']:.2f}%{ANSI_RESET}")
    print(f"  Profit Factor:   {stats['profit_factor']:.3f}")
    print(f"  Avg Winner:      {ANSI_GREEN}${stats['avg_winner']:+.2f}{ANSI_RESET}")
    print(f"  Avg Loser:       {ANSI_RED}${stats['avg_loser']:.2f}{ANSI_RESET}")
    print(f"  Final Equity:    ${stats['final_equity']:,.2f}")
    print()

    return results


def cmd_run_sim(args, cfg: Config):
    """Run continuous paper trading simulation."""
    pairs = _resolve_pairs(args)
    auto = getattr(args, 'auto', False)

    print(f"\n{ANSI_CYAN}{ANSI_BOLD}{'='*70}")
    print(f"  NEXUS BRAIN v{VERSION} -- Live Paper Trading Simulation")
    print(f"{'='*70}{ANSI_RESET}\n")
    print(f"  Capital: ${cfg.initial_capital:,.2f}")
    print(f"  Pairs:   {len(pairs)}")
    print(f"  Max Pos: {cfg.max_positions}")
    print(f"  Scan:    every {cfg.scan_interval_sec}s")
    print()

    if not auto:
        try:
            input("  Press Enter to start (or run with --auto to skip)...")
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return

    shutdown = threading.Event()
    trader = PaperTrader(cfg)
    trader.restore_positions()
    db = Database(cfg.db_path)

    def handle_sig(signum, frame):
        shutdown.set()

    signal_mod.signal(signal_mod.SIGINT, handle_sig)
    try:
        signal_mod.signal(signal_mod.SIGTERM, handle_sig)
    except (OSError, AttributeError):
        pass

    # ── Live HTTP API (serves /api/snapshot from trader state) ──
    port = cfg.DASHBOARD_PORT
    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit as _atexit
        ensure_port(port, "nexusbrain")
        write_pidfile("nexusbrain", port)
        _atexit.register(cleanup_pidfile, "nexusbrain")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    _live_snapshot = {"status": "starting", "bot_name": BOT_NAME, "timestamp": time.time()}
    _snap_lock = threading.Lock()

    # NB-1: the dashboard's NexusBrain tab renders raw.recent_signals, but only
    # the backtest path (_build_results) ever emitted it — the live run-sim
    # snapshot never did, leaving the panel permanently empty. Same shape as
    # the backtest field (Signal.to_dict(), chronological), capped at the last
    # 20 generated signals (REJECTs included — the tab renders reject reasons).
    _recent_signals: deque = deque(maxlen=20)

    def _update_snapshot():
        stats = trader.get_stats()
        snap = {
            "timestamp": time.time(),
            "bot_name": BOT_NAME,
            "version": VERSION,
            "status": "scanning",
            "scan_cycle": cycle,
            "performance": stats,
            "equity_curve": list(db.load_equity_curve())[-100:] if hasattr(db, 'load_equity_curve') else [],
            "open_positions": [
                {"pair": p, "direction": pos.direction, "entry_price": pos.entry_price,
                 "current_stop": getattr(pos, 'stop_loss', None), "size": getattr(pos, 'size_usd', None),
                 "unrealized_pnl": getattr(pos, 'unrealized_pnl', None)}
                for p, pos in trader.positions.items()
            ],
            "config": {"min_confluence": cfg.min_confluence, "max_positions": cfg.max_positions,
                       "scan_interval": cfg.scan_interval_sec},
            # NB-1: live counterpart of the backtest-only recent_signals field
            "recent_signals": list(_recent_signals),
        }
        if _expectancy:
            snap["expectancy"] = _expectancy.bot_snapshot_fields('nexusbrain')
        with _snap_lock:
            _live_snapshot.update(snap)

    class _SimHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def do_GET(self):
            if self.path == "/api/snapshot":
                with _snap_lock:
                    body = json.dumps(_live_snapshot, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/health":
                data = json.dumps({"status": "ok", "bot": BOT_NAME, "version": VERSION,
                                   "timestamp": time.time(), "scan_cycle": cycle}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()

    # ThreadingHTTPServer: the plain HTTPServer serves one request at a time,
    # so while this bot computes, its port stops answering and Command Center's
    # health check reports it DOWN even though it is healthy.
    _http = ThreadingHTTPServer(("0.0.0.0", port), _SimHandler)
    _http.daemon_threads = True
    _http_t = threading.Thread(target=_http.serve_forever, daemon=True, name="sim-http")
    _http_t.start()
    print(f"  API:     http://localhost:{port}/api/snapshot")

    cycle = 0
    # Signal dedup: only emit when signal meaningfully changes
    # Track last confidence label per pair to detect real transitions
    _last_signal = {}  # pair -> {"label": ConfidenceLabel, "score_bucket": int}

    def _signal_changed(pair, signal):
        """Return True if this signal is meaningfully different from the last one."""
        if pair not in _last_signal:
            return True
        prev = _last_signal[pair]
        # Flipped direction (LONG <-> SHORT) — always meaningful.
        # If the stored signal has NO direction we cannot tell whether this
        # one flipped. Defaulting to "LONG" answered that question with a
        # guess, so a genuine SHORT read as a flip (spurious alert) and a
        # LONG read as unchanged (suppressed alert) — in both cases from a
        # comparison against a value nobody recorded. Unknown means we cannot
        # rule out a change, so treat it as changed and let the other checks
        # below refine it.
        _prev_dir = prev.get("direction")
        if _prev_dir is None or signal.direction != _prev_dir:
            return True
        # Changed confidence tier
        if signal.confidence_label != prev["label"]:
            return True
        # Crossed a 0.1 score boundary (e.g. 0.7→0.8)
        bucket = int(signal.confluence_score * 10)
        if bucket != prev["score_bucket"]:
            return True
        return False

    def _record_signal(pair, signal):
        _last_signal[pair] = {
            "label": signal.confidence_label,
            "score_bucket": int(signal.confluence_score * 10),
            "direction": signal.direction,
        }

    while not shutdown.is_set():
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"\n  {ANSI_DIM}[{now}] Scan cycle {cycle}{ANSI_RESET}")

        # Lease heartbeat: declare held reservation ids so the pool can sweep
        # anything a wiring bug stranded (never raises).
        if trader._portfolio:
            trader._portfolio.confirm_reservations(
                [p.reservation_id for p in trader.positions.values() if p.reservation_id])

        for pair in pairs:
            if shutdown.is_set():
                break
            try:
                pd = fetch_pair_data(pair, cfg)
                if not pd or not pd.candles:
                    continue

                # Check exits for existing positions
                if pair in trader.positions:
                    trade = trader.check_exits(pair, pd.last_price)
                    if trade:
                        db.save_trade(trade)
                        display = PAIR_DISPLAY.get(pair, pair)
                        pnl_c = ANSI_GREEN if trade.pnl_usd >= 0 else ANSI_RED
                        print(f"    EXIT {display} {pnl_c}${trade.pnl_usd:+.2f}{ANSI_RESET} ({trade.exit_reason.value})")
                        # Clear dedup state so next signal for this pair is fresh
                        _last_signal.pop(pair, None)

                # Generate new signals
                signal = generate_signal(pair, pd, cfg)
                if signal:
                    # NB-1: feed the live snapshot's recent_signals ring
                    # (every generated signal, REJECT included, so the tab
                    # reflects the latest scan like the backtest view did)
                    _recent_signals.append(signal.to_dict())
                if signal and signal.confidence_label != ConfidenceLabel.REJECT:
                    # Only save/log when signal meaningfully changes
                    if _signal_changed(pair, signal):
                        db.save_signal(signal)
                        _record_signal(pair, signal)

                    if pair not in trader.positions and trader.can_open():
                        pos = trader.open_position(signal)
                        if pos:
                            display = PAIR_DISPLAY.get(pair, pair)
                            print(f"    OPEN {display} @ {format_price(pos.entry_price)} score={pos.signal_score:.3f}")
                elif signal and signal.confidence_label == ConfidenceLabel.REJECT:
                    # Signal dropped to REJECT — clear dedup so recovery is logged
                    if pair in _last_signal:
                        _last_signal.pop(pair, None)

            except Exception as e:
                logger.error(f"Sim error for {pair}: {e}")
            time.sleep(0.3)

        # Save equity snapshot
        db.save_equity(trader.equity)

        # Update live HTTP snapshot
        _update_snapshot()

        # Summary line
        stats = trader.get_stats()
        pnl_c = ANSI_GREEN if stats["total_pnl"] >= 0 else ANSI_RED
        print(f"  Equity: ${trader.equity:,.2f} | Pos: {len(trader.positions)} | Trades: {stats['total_trades']} | P/L: {pnl_c}${stats['total_pnl']:+.2f}{ANSI_RESET}")

        # Wait for next cycle
        for _ in range(int(cfg.scan_interval_sec / 0.5)):
            if shutdown.is_set():
                break
            time.sleep(0.5)

    print(f"\n  {ANSI_CYAN}Simulation stopped.{ANSI_RESET}")
    stats = trader.get_stats()
    print(f"  Final equity: ${stats['final_equity']:,.2f} | Trades: {stats['total_trades']} | WR: {stats['win_rate']*100:.1f}%\n")


def cmd_report(args, cfg: Config):
    """Show performance report from saved data."""
    db = Database(cfg.db_path)
    trades = db.load_trades()
    equity_curve = db.load_equity_curve()

    print(f"\n{ANSI_CYAN}{ANSI_BOLD}{'='*70}")
    print(f"  NEXUS BRAIN v{VERSION} -- Performance Report")
    print(f"{'='*70}{ANSI_RESET}\n")

    if not trades:
        print("  No trades found in database.\n")
        return

    winners = [t for t in trades if t.pnl_usd > 0]
    losers = [t for t in trades if t.pnl_usd <= 0]
    total_pnl = sum(t.pnl_usd for t in trades)
    gross_profit = sum(t.pnl_usd for t in winners)
    gross_loss = abs(sum(t.pnl_usd for t in losers))

    if gross_loss > 0:
        pf = gross_profit / gross_loss
    else:
        pf = 0.0
    pf = sf(pf)

    wr = len(winners) / len(trades) * 100 if trades else 0

    pnl_c = ANSI_GREEN if total_pnl >= 0 else ANSI_RED

    print(f"  Total Trades:  {len(trades)}")
    print(f"  Winners:       {ANSI_GREEN}{len(winners)}{ANSI_RESET}")
    print(f"  Losers:        {ANSI_RED}{len(losers)}{ANSI_RESET}")
    print(f"  Win Rate:      {wr:.1f}%")
    print(f"  Total P&L:     {pnl_c}${total_pnl:+,.2f}{ANSI_RESET}")
    print(f"  Profit Factor: {pf:.3f}")
    print()

    # Recent trades
    print(f"  {ANSI_BOLD}Recent Trades:{ANSI_RESET}")
    print(f"  {'Pair':<14} {'Dir':<6} {'Entry':>10} {'Exit':>10} {'P&L':>10} {'Reason':<15} {'Score':>6}")
    print(f"  {'-'*77}")
    for t in trades[:20]:
        display = PAIR_DISPLAY.get(t.pair, t.pair)
        pc = ANSI_GREEN if t.pnl_usd >= 0 else ANSI_RED
        print(
            f"  {display:<14} {t.direction:<6} {format_price(t.entry_price):>10} {format_price(t.exit_price):>10} "
            f"{pc}${t.pnl_usd:+8.2f}{ANSI_RESET} {t.exit_reason.value:<15} {t.signal_score:>6.3f}"
        )
    print()


def cmd_health(args, cfg: Config):
    """Run a simple health check HTTP endpoint."""
    port = cfg.DASHBOARD_PORT

    print(f"\n{ANSI_CYAN}  NEXUS BRAIN Health Check -- port {port}{ANSI_RESET}\n")

    class HealthHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def do_GET(self):
            if self.path == "/health":
                data = {
                    "status": "ok",
                    "bot": BOT_NAME,
                    "version": VERSION,
                    "timestamp": time.time(),
                    "uptime": time.time() - _start_time,
                }
                body = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    # ThreadingHTTPServer: the plain HTTPServer serves one request at a time,
    # so while this bot computes, its port stops answering and Command Center's
    # health check reports it DOWN even though it is healthy.
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.daemon_threads = True
    print(f"  Listening on http://localhost:{port}/health")
    print("  Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Health server stopped.")


def cmd_dashboard(args, cfg: Config):
    """Run backtest and serve results via web dashboard."""
    auto = getattr(args, 'auto', False)
    pairs = _resolve_pairs(args)
    port = cfg.DASHBOARD_PORT

    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        ensure_port(port, "nexusbrain")
        write_pidfile("nexusbrain", port)
        atexit.register(cleanup_pidfile, "nexusbrain")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    print(f"\n{ANSI_CYAN}{ANSI_BOLD}{'='*70}")
    print(f"  NEXUS BRAIN v{VERSION} -- Web Dashboard")
    print(f"{'='*70}{ANSI_RESET}\n")
    print(f"  Capital: ${cfg.initial_capital:,.2f}")
    print(f"  Pairs:   {len(pairs)}")
    print(f"  Port:    {port}")
    print()

    if not auto:
        try:
            input("  Press Enter to start backtest (or run with --auto to skip)...")
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return

    # Run backtest
    print("  Running backtest...")

    def progress(current, total, pair):
        if current < total:
            display = PAIR_DISPLAY.get(pair, pair)
            pct = (current + 1) / total * 100
            print(f"    [{current+1}/{total}] {display}... ({pct:.0f}%)", flush=True)
        else:
            print("  Backtest complete.\n")

    bt = Backtester(cfg)
    results = bt.run(pairs, progress_callback=progress)

    # Print summary
    stats = results["performance"]
    pnl_c = ANSI_GREEN if stats["total_pnl"] >= 0 else ANSI_RED
    print(f"  Trades: {stats['total_trades']} | WR: {stats['win_rate']*100:.1f}% | P/L: {pnl_c}${stats['total_pnl']:+,.2f}{ANSI_RESET} | Sharpe: {stats['sharpe_ratio']:.3f}")
    print()

    # Build snapshot
    snapshot = {
        "timestamp": time.time(),
        "bot_name": BOT_NAME,
        "version": VERSION,
        "status": "complete",
        "performance": results["performance"],
        "equity_curve": results["equity_curve"],
        "recent_signals": results["recent_signals"],
        "recent_trades": results["recent_trades"],
        "open_positions": results["open_positions"],
        "config": results["config"],
    }
    if _expectancy:
        snapshot["expectancy"] = _expectancy.bot_snapshot_fields('nexusbrain')

    # JSON-sanitize the entire snapshot
    snapshot_json = json.dumps(snapshot, default=str)

    # Serve dashboard
    here = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(here, "dashboard.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
    except FileNotFoundError:
        html_content = f"<html><body><h1>dashboard.html not found at {html_path}</h1></body></html>"

    class DashHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                body = html_content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/snapshot":
                body = snapshot_json.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/health":
                data = json.dumps({"status": "ok", "bot": BOT_NAME, "version": VERSION, "timestamp": time.time()})
                body = data.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"404 Not Found")

    # ThreadingHTTPServer: the plain HTTPServer serves one request at a time,
    # so while this bot computes, its port stops answering and Command Center's
    # health check reports it DOWN even though it is healthy.
    server = ThreadingHTTPServer(("0.0.0.0", port), DashHandler)
    server.daemon_threads = True
    print(f"  Dashboard: http://localhost:{port}")
    print(f"  API:       http://localhost:{port}/api/snapshot")
    print(f"  Health:    http://localhost:{port}/health")
    print("  Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n  {ANSI_CYAN}Dashboard stopped.{ANSI_RESET}")


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _resolve_pairs(args) -> List[str]:
    """Resolve pair list from CLI args."""
    if hasattr(args, 'pairs') and args.pairs:
        raw = [p.strip().upper() for p in args.pairs.split(",")]
        pairs = []
        # Try to match to known pairs
        name_to_pair = {}
        for k, v in PAIR_DISPLAY.items():
            short = v.split("/")[0]
            name_to_pair[short] = k
            name_to_pair[v] = k
            name_to_pair[k] = k

        for name in raw:
            if name in name_to_pair:
                pairs.append(name_to_pair[name])
            elif name + "USD" in PAIR_DISPLAY:
                pairs.append(name + "USD")
            elif "X" + name + "ZUSD" in PAIR_DISPLAY:
                pairs.append("X" + name + "ZUSD")
            else:
                logger.warning(f"Unknown pair: {name}, skipping")
        return pairs if pairs else DEFAULT_PAIRS[:5]
    else:
        return DEFAULT_PAIRS[:5]  # 5 pairs for faster dashboard boot


_start_time = time.time()


# ═══════════════════════════════════════════════════════════════════════
# MAIN / CLI PARSER
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description=f"NEXUS BRAIN v{VERSION} -- Multi-TF Cross-Exchange Signal Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # Global flags
    parser.add_argument("--auto", action="store_true", help="Headless mode, no prompts")
    parser.add_argument("--pairs", type=str, default="", help="Comma-separated pairs (e.g. BTC,ETH,SOL)")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--port", type=int, default=8074, help="Dashboard/health port")

    # scan
    p_scan = sub.add_parser("scan", help="One-shot scan all pairs")
    p_scan.add_argument("--pairs", type=str, default="")
    p_scan.add_argument("--auto", action="store_true")

    # backtest
    p_bt = sub.add_parser("backtest", help="Run historical backtest")
    p_bt.add_argument("--pairs", type=str, default="")
    p_bt.add_argument("--capital", type=float, default=10000.0)
    p_bt.add_argument("--days", type=int, default=90)
    p_bt.add_argument("--auto", action="store_true")

    # run-sim
    p_sim = sub.add_parser("run-sim", help="Continuous paper trading simulation")
    p_sim.add_argument("--pairs", type=str, default="")
    p_sim.add_argument("--capital", type=float, default=10000.0)
    p_sim.add_argument("--auto", action="store_true")

    # report
    p_rpt = sub.add_parser("report", help="Show performance report")
    p_rpt.add_argument("--auto", action="store_true")

    # health
    p_hlth = sub.add_parser("health", help="Health check endpoint")
    p_hlth.add_argument("--port", type=int, default=8074)
    p_hlth.add_argument("--auto", action="store_true")

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Run backtest + web dashboard")
    p_dash.add_argument("--pairs", type=str, default="")
    p_dash.add_argument("--capital", type=float, default=10000.0)
    p_dash.add_argument("--port", type=int, default=8074)
    p_dash.add_argument("--auto", action="store_true")

    args = parser.parse_args()

    # Build config
    cfg = Config()
    if hasattr(args, 'capital') and args.capital:
        cfg.initial_capital = args.capital
    if hasattr(args, 'port') and args.port:
        cfg.DASHBOARD_PORT = args.port
    if hasattr(args, 'days') and args.days:
        cfg.backtest_days = args.days

    # Route to command
    if args.command == "scan":
        cmd_scan(args, cfg)
    elif args.command == "backtest":
        cmd_backtest(args, cfg)
    elif args.command == "run-sim":
        cmd_run_sim(args, cfg)
    elif args.command == "report":
        cmd_report(args, cfg)
    elif args.command == "health":
        cmd_health(args, cfg)
    elif args.command == "dashboard":
        cmd_dashboard(args, cfg)
    else:
        parser.print_help()
        print("\n  Run with a command: scan, backtest, run-sim, report, health, dashboard")
        print("  Example: python nexus_brain.py scan --pairs BTC,ETH,SOL")
        print("  Example: python nexus_brain.py dashboard --auto\n")


if __name__ == "__main__":
    main()
