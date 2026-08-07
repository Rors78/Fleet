#!/usr/bin/env python3
"""
TRINITY SIGNAL BOT v2.0 — "OVERWATCH"
State-of-the-art crypto signal engine with full indicator suite
ANSI-only . Pydroid3 compatible . Pure stdlib where possible

Usage:
    python overwatch.py          # interactive mode (press Enter to start)
    python overwatch.py --auto   # headless, no prompts
"""

import threading
import time
import json
import math
import signal as sig
import sys
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# Fleet event bus integration
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "CommandCenter"))
try:
    from event_publisher import EventPublisher
except ImportError:
    EventPublisher = None

try:
    from fleet_config import is_blacklisted as _is_blacklisted
except ImportError:
    _is_blacklisted = lambda pair: False

from indicators import (
    Candle, ema, rsi, stochastic_rsi, macd, bollinger_bands,
    atr, adx, donchian, vwap, volume_momentum, pearson_correlation,
)

try:
    from kraken_ohlc import fetch_ohlc as _fetch_ohlc_canonical
except ImportError:
    _fetch_ohlc_canonical = None

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

KRAKEN_REST = "https://api.kraken.com/0/public"
CC_URL = "http://127.0.0.1:9000"

UNIVERSE_SIZE = 20
TOP_DISPLAY = 5

# TR-1: fiat/forex/stablecoin pairs must never enter the top_symbols ranking —
# they were getting full directional treatment (USDT/USD once carried SL/TP
# 8 bps apart). Trinity builds its OWN candidate list (CC /api/universe first,
# direct-Kraken fallback second), so it needs a local exclude set; the
# canonical exclude list lives in D:\CommandCenter\universe.json — keep this
# in sync when that changes.
NON_CRYPTO_PAIRS = {
    "EUR/USD", "GBP/USD", "AUD/USD", "USDT/USD", "USDC/USD", "USDG/USD",
    "DAI/USD", "PYUSD/USD", "EURT/USD", "TUSD/USD",
}
ROLLING_TICKS = 60
OHLC_REFRESH = 45
DASH_REFRESH = 1.5
SIGNAL_HISTORY_MAX = 50
ALERT_LOG_MAX = 20
CORRELATION_WINDOW = 20
DASHBOARD_PORT = 8072

TIMEFRAMES = {
    "5m":  {"interval": "5m",  "limit": 50, "weight": 0.25},
    "15m": {"interval": "15m", "limit": 50, "weight": 0.50},
    "1h":  {"interval": "1h",  "limit": 50, "weight": 0.25},
}

# ── Score gains for the four continuous indicators (2026-07-29 audit) ────────
# These four convert a price-relative magnitude into a 0..1 score via
# `0.5 + x * gain`. They previously computed x as a PERCENT (`... * 100`) and
# then applied a gain sized for a FRACTION, so every one of them saturated on
# moves far below market noise:
#     ema_cross  x10  -> pinned at 1.00 by a 0.050% EMA gap
#     macd       x20  -> pinned at 1.00 by a 0.025% histogram
#     vwap        x5  -> pinned at 1.00 by a 0.100% deviation
#     momentum    x5  -> pinned at 1.00 by a 0.100% 10-bar move
# Live proof: ema_cross read exactly 1.00 on all five top symbols, vwap 0.95,
# momentum 0.90. Four of ten indicators — 0.44 of total weight — had degraded
# into binary on/off flags, so the "10-indicator confluence" was really six
# graded scores plus four stuck switches. It also biased everything long,
# because all four are momentum-style and pin high the moment price ticks up.
#
# Inputs are now fractions, and each gain is calibrated from the measured
# distribution of that quantity on 15m bars across BTC/ETH/UNI/XLM/SUI/LINK,
# so the 90th-percentile move maps to ~0.90 and the score spans a useful range:
#     ema_gap   p50 0.158%  p90 0.642%   -> gain  62
#     macd_hist p50 0.162%  p90 0.654%   -> gain  61
#     vwap_dev  p50 0.315%  p90 1.390%   -> gain  29
#     roc_10    p50 0.415%  p90 1.552%   -> gain  26
SCORE_GAIN = {
    "ema_cross": 62.0,
    "macd":      61.0,
    "vwap":      29.0,
    "momentum":  26.0,
}

WEIGHTS = {
    "ema_cross":    0.12,
    "rsi":          0.10,
    "macd":         0.12,
    "bollinger":    0.08,
    "stoch_rsi":    0.08,
    "adx_trend":    0.10,
    "donchian":     0.12,
    "vwap":         0.08,
    "vol_spike":    0.10,
    "momentum":     0.10,
}

RISK_PER_TRADE_PCT = 1.0
DEFAULT_EQUITY = 10000

# ─────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class SignalResult:
    symbol: str
    price: float
    confluence: float
    bias: str              # "LONG", "SHORT", "NEUTRAL"
    regime: str            # "TRENDING", "RANGING", "VOLATILE"
    mtf_alignment: float
    stop_loss: float
    take_profit_tiers: List[float] = field(default_factory=list)
    position_size: float = 0.0
    indicators: Dict[str, float] = field(default_factory=dict)
    # True when timeframes actively disagree (some bullish, some bearish),
    # as opposed to merely being undecided. The old abs(sum) alignment metric
    # could not express this — see generate_signal.
    mtf_conflict: bool = False
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")


@dataclass
class AlertEntry:
    timestamp: str
    symbol: str
    message: str
    level: str  # "INFO", "WARN", "SIGNAL", "EXIT"


@dataclass
class SignalTrack:
    symbol: str
    bias: str
    entry_price: float
    entry_time: str
    stop: float
    tiers: List[float] = field(default_factory=list)
    peak_pnl: float = 0.0
    result: str = "OPEN"   # "WIN", "LOSS", "OPEN"
    # Set once price tags TP1. The stop moves to breakeven at that point, so a
    # later retrace cannot re-score a banked win as a loss. See track_signals.
    tp1_hit: bool = False


# ─────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────

state_lock = threading.Lock()
live_prices: Dict[str, float] = {}
rolling_prices: Dict[str, deque] = {}
ohlc_data: Dict[str, Dict[str, List[Candle]]] = {}   # symbol -> tf -> candles
signals: Dict[str, SignalResult] = {}
top_symbols: List[Tuple[str, float]] = []
signal_history: Dict[str, List[SignalTrack]] = {}
alert_log: deque = deque(maxlen=ALERT_LOG_MAX)
equity_curve: List[float] = [DEFAULT_EQUITY]

# Fleet event publisher
_event_pub = EventPublisher(CC_URL, "trinity") if EventPublisher else None
_last_regime: Optional[str] = None

ws_state = {
    "status": "DISCONNECTED",
    "messages_total": 0,
    "reconnects": 0,
    "uptime_start": time.time(),
    "last_msg_time": 0,
}

shutdown_event = threading.Event()



# ─────────────────────────────────────────────
# SIGNAL ENGINE
# ─────────────────────────────────────────────

def score_single_tf(candles: List[Candle], price: float) -> Dict[str, float]:
    """Score all 10 indicators for a single timeframe. Returns dict of scores [0..1]."""
    scores = {}
    closes = [c.close for c in candles]
    if len(closes) < 30:
        return {k: 0.5 for k in WEIGHTS}

    # 1. EMA Cross
    ema_fast = ema(closes, 9)
    ema_slow = ema(closes, 21)
    if ema_slow != 0:
        # Fraction, not percent. See SCORE_GAIN for why this mattered.
        cross_frac = (ema_fast - ema_slow) / ema_slow
        scores["ema_cross"] = max(0, min(1, 0.5 + cross_frac * SCORE_GAIN["ema_cross"]))
    else:
        scores["ema_cross"] = 0.5

    # 2. RSI
    rsi_val = rsi(closes, 14)
    if rsi_val < 30:
        scores["rsi"] = 0.8 + (30 - rsi_val) / 150
    elif rsi_val > 70:
        scores["rsi"] = 0.2 - (rsi_val - 70) / 150
    else:
        scores["rsi"] = 0.5 + (rsi_val - 50) / 100
    scores["rsi"] = max(0, min(1, scores["rsi"]))

    # 3. MACD
    macd_line, signal_line, hist = macd(closes)
    if price != 0:
        hist_frac = hist / price
        scores["macd"] = max(0, min(1, 0.5 + hist_frac * SCORE_GAIN["macd"]))
    else:
        scores["macd"] = 0.5

    # 4. Bollinger Bands
    bb_upper, bb_mid, bb_lower = bollinger_bands(closes)
    if bb_upper != bb_lower:
        bb_pct = (price - bb_lower) / (bb_upper - bb_lower)
        if bb_pct < 0.2:
            scores["bollinger"] = 0.8
        elif bb_pct > 0.8:
            scores["bollinger"] = 0.2
        else:
            scores["bollinger"] = 0.5 + (0.5 - bb_pct)
    else:
        scores["bollinger"] = 0.5
    scores["bollinger"] = max(0, min(1, scores["bollinger"]))

    # 5. Stochastic RSI
    stoch_k, stoch_d = stochastic_rsi(closes)
    if stoch_k < 20:
        scores["stoch_rsi"] = 0.8
    elif stoch_k > 80:
        scores["stoch_rsi"] = 0.2
    else:
        scores["stoch_rsi"] = 0.5 + (stoch_k - 50) / 100
    scores["stoch_rsi"] = max(0, min(1, scores["stoch_rsi"]))

    # 6. ADX + Trend
    adx_val, pdi, mdi = adx(candles)
    if adx_val > 25:
        trend_score = 0.6 + min(adx_val - 25, 50) / 125
        if pdi > mdi:
            scores["adx_trend"] = trend_score
        else:
            scores["adx_trend"] = 1 - trend_score
    else:
        scores["adx_trend"] = 0.5
    scores["adx_trend"] = max(0, min(1, scores["adx_trend"]))

    # 7. Donchian Breakout
    dc_upper, dc_mid, dc_lower = donchian(candles, 20)
    if dc_upper != dc_lower:
        dc_pos = (price - dc_lower) / (dc_upper - dc_lower)
        if dc_pos > 0.95:
            scores["donchian"] = 0.9
        elif dc_pos < 0.05:
            scores["donchian"] = 0.1
        else:
            scores["donchian"] = dc_pos
    else:
        scores["donchian"] = 0.5

    # 8. VWAP
    vwap_val = vwap(candles, 20)
    if vwap_val != 0:
        vwap_dev = (price - vwap_val) / vwap_val
        scores["vwap"] = max(0, min(1, 0.5 + vwap_dev * SCORE_GAIN["vwap"]))
    else:
        scores["vwap"] = 0.5

    # 9. Volume Spike
    vol_mom = volume_momentum(candles)
    if vol_mom > 2.0:
        scores["vol_spike"] = 0.9
    elif vol_mom > 1.5:
        scores["vol_spike"] = 0.7
    elif vol_mom < 0.5:
        scores["vol_spike"] = 0.3
    else:
        scores["vol_spike"] = 0.5 + (vol_mom - 1) * 0.4
    scores["vol_spike"] = max(0, min(1, scores["vol_spike"]))

    # 10. Momentum (Rate of Change)
    if len(closes) >= 10 and closes[-10] != 0:
        roc = (closes[-1] - closes[-10]) / closes[-10]
        scores["momentum"] = max(0, min(1, 0.5 + roc * SCORE_GAIN["momentum"]))
    else:
        scores["momentum"] = 0.5

    return scores


def generate_signal(symbol: str, candle_data: Dict[str, List[Candle]], price: float) -> Optional[SignalResult]:
    """Generate a composite signal from multi-timeframe analysis."""
    if price <= 0:
        return None

    tf_scores = {}
    for tf_name, tf_cfg in TIMEFRAMES.items():
        candles = candle_data.get(tf_name, [])
        if not candles:
            continue
        tf_scores[tf_name] = score_single_tf(candles, price)

    if not tf_scores:
        return None

    # Composite weighted score per indicator
    composite = {}
    for ind_name in WEIGHTS:
        val = 0.0
        w_sum = 0.0
        for tf_name, tf_cfg in TIMEFRAMES.items():
            if tf_name in tf_scores and ind_name in tf_scores[tf_name]:
                val += tf_scores[tf_name][ind_name] * tf_cfg["weight"]
                w_sum += tf_cfg["weight"]
        if w_sum > 0:
            composite[ind_name] = val / w_sum
        else:
            composite[ind_name] = 0.5

    # Overall confluence
    confluence = sum(composite[k] * WEIGHTS[k] for k in WEIGHTS) / sum(WEIGHTS.values())

    # MTF alignment — the largest bloc of timeframes agreeing on one direction.
    #
    # This used to be abs(sum(directions))/len(directions), which cannot tell
    # agreement from cancellation because opposing votes annihilate:
    #     [1, 1, 1] -> 1.00  genuine agreement
    #     [1, 1,-1] -> 0.33  a real CONFLICT, reads as weak agreement
    #     [1,-1, 0] -> 0.00  direct conflict, scores the same as all-neutral
    #     [0, 0, 0] -> 0.00  no opinion at all
    # So a signal whose timeframes actively disagreed was indistinguishable
    # from one that simply had no view (2026-07-29 audit).
    #
    # max(bull, bear)/n answers the question the name implies: what fraction of
    # timeframes actually point the same way? Conflict now scores strictly
    # lower than indecision on the winning side, and [1,1,-1] -> 0.67 with a
    # dissent flag rather than masquerading as 0.33 of agreement.
    tf_directions = []
    for tf_name in tf_scores:
        tf_total = sum(tf_scores[tf_name].get(k, 0.5) * WEIGHTS[k] for k in WEIGHTS) / sum(WEIGHTS.values())
        tf_directions.append(1 if tf_total > 0.55 else (-1 if tf_total < 0.45 else 0))
    if tf_directions:
        n_bull = sum(1 for d in tf_directions if d > 0)
        n_bear = sum(1 for d in tf_directions if d < 0)
        mtf_alignment = max(n_bull, n_bear) / len(tf_directions)
        # True when at least one timeframe points the opposite way to the
        # majority — the case the old metric silently folded away.
        mtf_conflict = (n_bull > 0 and n_bear > 0)
    else:
        mtf_alignment = 0.0
        mtf_conflict = False

    # Bias
    if confluence > 0.58:
        bias = "LONG"
    elif confluence < 0.42:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    # Regime detection using ADX
    primary_tf = "15m"
    primary_candles = candle_data.get(primary_tf, [])
    if primary_candles:
        adx_val, _, _ = adx(primary_candles)
        atr_val = atr(primary_candles)
        if adx_val > 25:
            regime = "TRENDING"
        elif atr_val > 0 and primary_candles:
            avg_price = sum(c.close for c in primary_candles[-20:]) / min(20, len(primary_candles))
            if avg_price > 0 and atr_val / avg_price > 0.02:
                regime = "VOLATILE"
            else:
                regime = "RANGING"
        else:
            regime = "RANGING"
    else:
        regime = "RANGING"

    # Stop loss & take profit
    atr_val = 0.0
    if primary_candles:
        atr_val = atr(primary_candles)
    if atr_val <= 0:
        atr_val = price * 0.02  # fallback 2%

    if bias == "LONG":
        stop_loss = price - 2 * atr_val
        tp1 = price + 1.5 * atr_val
        tp2 = price + 3.0 * atr_val
        tp3 = price + 5.0 * atr_val
    elif bias == "SHORT":
        stop_loss = price + 2 * atr_val
        tp1 = price - 1.5 * atr_val
        tp2 = price - 3.0 * atr_val
        tp3 = price - 5.0 * atr_val
    else:
        stop_loss = price - 1.5 * atr_val
        tp1 = price + 1.0 * atr_val
        tp2 = price + 2.0 * atr_val
        tp3 = price + 3.0 * atr_val

    # Position sizing (risk-based)
    risk_amount = DEFAULT_EQUITY * (RISK_PER_TRADE_PCT / 100)
    risk_dist = abs(price - stop_loss)
    if risk_dist > 0:
        position_size = risk_amount / risk_dist
    else:
        position_size = 0.0

    return SignalResult(
        symbol=symbol,
        price=price,
        confluence=confluence,
        bias=bias,
        regime=regime,
        mtf_alignment=mtf_alignment,
        stop_loss=stop_loss,
        take_profit_tiers=[tp1, tp2, tp3],
        position_size=position_size,
        indicators=composite,
        mtf_conflict=mtf_conflict,
    )


def compute_rankings() -> List[Tuple[str, float]]:
    """Rank all symbols by composite signal score with correlation penalty."""
    with state_lock:
        current_signals = dict(signals)
        current_rolling = dict(rolling_prices)

    scored = []
    for sym, sig_r in current_signals.items():
        # TR-1 belt-and-braces: even if a fiat/stable pair is already streaming
        # (state predating the universe filter), it must not be ranked.
        if sym in NON_CRYPTO_PAIRS:
            continue
        if sig_r.bias == "NEUTRAL":
            bias_bonus = 0
        elif sig_r.bias == "LONG":
            bias_bonus = 0.05
        else:
            bias_bonus = 0.03
        score = sig_r.confluence + bias_bonus + sig_r.mtf_alignment * 0.1
        scored.append((sym, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Correlation filter — penalize highly correlated pairs in top list
    if len(scored) > 1:
        filtered = [scored[0]]
        for sym, score in scored[1:]:
            correlated = False
            dq = current_rolling.get(sym, deque())
            if len(dq) < CORRELATION_WINDOW:
                filtered.append((sym, score))
                continue
            sym_prices = list(dq)[-CORRELATION_WINDOW:]
            for fsym, _ in filtered:
                fdq = current_rolling.get(fsym, deque())
                if len(fdq) < CORRELATION_WINDOW:
                    continue
                f_prices = list(fdq)[-CORRELATION_WINDOW:]
                corr = pearson_correlation(sym_prices, f_prices)
                if abs(corr) > 0.85:
                    correlated = True
                    break
            if correlated:
                score -= 0.1  # penalty
            filtered.append((sym, score))
        filtered.sort(key=lambda x: x[1], reverse=True)
        return filtered[:TOP_DISPLAY]

    return scored[:TOP_DISPLAY]


# ─────────────────────────────────────────────
# SIGNAL TRACKING (WIN/LOSS)
# ─────────────────────────────────────────────

def record_signal(sig_r: SignalResult):
    """Record a new signal for tracking."""
    with state_lock:
        if sig_r.symbol not in signal_history:
            signal_history[sig_r.symbol] = []
        # Don't duplicate if same bias recently
        recent = signal_history[sig_r.symbol]
        if recent and recent[-1].result == "OPEN" and recent[-1].bias == sig_r.bias:
            return
        track = SignalTrack(
            symbol=sig_r.symbol,
            bias=sig_r.bias,
            entry_price=sig_r.price,
            entry_time=sig_r.timestamp,
            stop=sig_r.stop_loss,
            tiers=list(sig_r.take_profit_tiers),
        )
        signal_history[sig_r.symbol].append(track)
        # Trim
        if len(signal_history[sig_r.symbol]) > SIGNAL_HISTORY_MAX:
            signal_history[sig_r.symbol] = signal_history[sig_r.symbol][-SIGNAL_HISTORY_MAX:]


def track_signals():
    """Update open signal tracks with current prices."""
    with state_lock:
        prices_snap = dict(live_prices)
        history_snap = {k: list(v) for k, v in signal_history.items()}

    updates = []
    for sym, tracks in history_snap.items():
        price = prices_snap.get(sym, 0)
        if price <= 0:
            continue
        for track in tracks:
            if track.result != "OPEN":
                continue
            if track.bias == "LONG":
                pnl = (price - track.entry_price) / track.entry_price * 100
            else:
                pnl = (track.entry_price - price) / track.entry_price * 100
            track.peak_pnl = max(track.peak_pnl, pnl)

            # ── Resolution ──────────────────────────────────────────────────
            # A WIN used to require reaching tiers[-1] (TP3) while a LOSS only
            # required touching the stop. With a 1:2.50 risk:reward that made
            # the win condition ~2.5x harder to satisfy than the loss
            # condition, so the recorded win rate was structurally pessimistic:
            # a signal could run to TP1, reverse, and be booked as a pure loss
            # despite having been in profit (2026-07-29 audit).
            #
            # Now TP1 counts as the win — that is the first target a real
            # trade would bank — and once TP1 is tagged the stop moves to
            # breakeven, so the same track can no longer be re-scored a loss
            # on a later retrace. Verified live: TP1 sits ~0.3-1.0% away vs a
            # stop ~0.4-1.3% away, which is a fair symmetric test.
            tp1 = track.tiers[0] if track.tiers else None

            if tp1 is not None and not track.tp1_hit:
                if ((track.bias == "LONG" and price >= tp1) or
                        (track.bias == "SHORT" and price <= tp1)):
                    track.tp1_hit = True
                    track.stop = track.entry_price      # stop to breakeven
                    track.result = "WIN"
                    updates.append((sym, track, "WIN"))
                    continue

            # Stop (or breakeven stop after TP1) — only a loss if TP1 never hit
            if not track.tp1_hit:
                if ((track.bias == "LONG" and price <= track.stop) or
                        (track.bias == "SHORT" and price >= track.stop)):
                    track.result = "LOSS"
                    updates.append((sym, track, "LOSS"))

    # Mutate history under the lock, then emit alerts AFTER releasing it.
    #
    # add_alert() acquires state_lock itself, and state_lock is a plain
    # threading.Lock (not an RLock), so calling it from inside this block
    # self-deadlocks the ranking worker. The worker dies holding the lock,
    # every other consumer blocks on it, and the ThreadingHTTPServer stops
    # answering — the process keeps the port bound but /api/snapshot hangs
    # forever, which is exactly what took Trinity to 17/18 on 2026-07-29
    # (curl exit 28, PID alive, port bound, no response).
    #
    # This was latent for as long as no signal ever resolved. The TP1
    # resolution fix in the same audit made WIN/LOSS reachable for the first
    # time, so `updates` became non-empty and the deadlock finally fired.
    pending_alerts = []
    with state_lock:
        for sym, track, result in updates:
            if sym in signal_history:
                for t in signal_history[sym]:
                    if t.entry_time == track.entry_time and t.result == "OPEN":
                        t.result = result
                        t.peak_pnl = track.peak_pnl
            pending_alerts.append((
                sym,
                f"Signal {result}: {track.bias} @ {track.entry_price:.4f} -> P/L {track.peak_pnl:.2f}%",
                "EXIT" if result == "LOSS" else "SIGNAL",
            ))

    for sym, msg, level in pending_alerts:
        add_alert(sym, msg, level)


def get_win_rate() -> Tuple[int, int, int]:
    """Returns (wins, losses, opens) across all tracked signals."""
    wins = losses = opens = 0
    for sym, tracks in signal_history.items():
        for t in tracks:
            if t.result == "WIN":
                wins += 1
            elif t.result == "LOSS":
                losses += 1
            else:
                opens += 1
    return wins, losses, opens


# ─────────────────────────────────────────────
# ALERT LOG
# ─────────────────────────────────────────────

def add_alert(symbol: str, message: str, level: str = "INFO"):
    """Thread-safe alert log append."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    entry = AlertEntry(timestamp=ts, symbol=symbol, message=message, level=level)
    with state_lock:
        alert_log.append(entry)
    # Trinity's ONLY stdout emit was render_dashboard(), whose thread starts
    # `if not auto` — and the fleet launches it with --auto. It was therefore
    # 100% silent on disk by construction: alerts existed solely in the
    # in-memory alert_log, so logs/bots/trinity.log could never show a
    # problem no matter how bad things got. Non-INFO alerts now reach stdout,
    # which the launcher captures, without resurrecting the TUI.
    if str(level).upper() not in ("INFO", "DEBUG"):
        print(f"[{ts}] {level}: {symbol} {message}", flush=True)


# ─────────────────────────────────────────────
# NETWORK / DATA FETCHING
# ─────────────────────────────────────────────

# Kraken interval mapping: our tf names -> Kraken interval minutes
KRAKEN_INTERVALS = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}

# Kraken symbol quirks -> clean display names
KRAKEN_CLEAN = {
    "XXBT": "BTC", "XBT": "BTC", "XETH": "ETH", "XLTC": "LTC",
    "XXRP": "XRP", "XDOGE": "DOGE", "XXLM": "XLM", "XXMR": "XMR",
}

# Maps display name (e.g. "BTC/USD") -> kraken pair name (e.g. "XXBTZUSD")
_pair_map: Dict[str, str] = {}


def _clean_base(raw: str) -> str:
    if raw in KRAKEN_CLEAN:
        return KRAKEN_CLEAN[raw]
    if len(raw) > 3 and raw[0] in ("X", "Z"):
        return raw[1:]
    return raw


def _cc_fetch(path, timeout=5):
    """Fetch JSON from Command Center, return None on failure."""
    try:
        r = requests.get(f"{CC_URL}{path}", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def fetch_universe() -> List[str]:
    """Get top UNIVERSE_SIZE USD pairs by volume — CC first, Kraken fallback.
    Retries up to 3 times with 5s sleep so a single SSL/network hiccup at startup
    doesn't cause an empty return and silent exit."""
    global _pair_map
    # Try Command Center first
    try:
        cc = _cc_fetch("/api/universe")
        if cc and cc.get("pairs"):
            _pair_map.clear()
            symbols = []
            for p in cc["pairs"][:UNIVERSE_SIZE]:
                disp = p.get("display", "")
                kraken = p.get("kraken", p.get("pair", ""))
                if disp in NON_CRYPTO_PAIRS:      # TR-1: no fiat/stables
                    continue
                if disp and kraken:
                    _pair_map[disp] = kraken
                    symbols.append(disp)
            if symbols:
                add_alert("SYSTEM", f"Universe: {len(symbols)} pairs via CC", "INFO")
                return symbols
    except Exception:
        pass
    # Fallback: Kraken direct
    try:
        # Get all asset pairs
        resp = requests.get(f"{KRAKEN_REST}/AssetPairs", timeout=15)
        data = resp.json()
        if data.get("error"):
            add_alert("SYSTEM", f"Kraken AssetPairs error: {data['error']}", "WARN")
            return []

        usd_pairs = []
        for name, info in data.get("result", {}).items():
            if info.get("status") == "delisted":
                continue
            wsname = info.get("wsname", "")
            quote = info.get("quote", "")
            # Accept USD and USDT pairs
            if quote in ("ZUSD", "USD") or name.endswith("USD"):
                base_raw = info.get("base", "")
                if "/" in wsname:
                    base_raw = wsname.split("/")[0]
                clean = _clean_base(base_raw)
                display = f"{clean}/USD"
                if display in NON_CRYPTO_PAIRS:   # TR-1: no fiat/stables
                    continue
                usd_pairs.append({"kraken_pair": name, "display": display, "base": clean})

        # Fetch tickers for volume ranking
        # Kraken allows comma-separated pairs in one call (up to ~30)
        pair_names = [p["kraken_pair"] for p in usd_pairs]
        qualified = []

        # Batch ticker requests (Kraken supports multi-pair)
        for batch_start in range(0, len(pair_names), 20):
            batch = pair_names[batch_start:batch_start + 20]
            pair_str = ",".join(batch)
            try:
                tresp = requests.get(f"{KRAKEN_REST}/Ticker?pair={pair_str}", timeout=15)
                tdata = tresp.json()
                if tdata.get("error"):
                    continue
                for key, tinfo in tdata.get("result", {}).items():
                    last = float(tinfo.get("c", [0])[0])
                    vol24 = float(tinfo.get("v", [0, 0])[1])
                    vol_usd = vol24 * last
                    if last > 0 and vol_usd > 10000:
                        # Find matching pair info
                        match = next((p for p in usd_pairs if p["kraken_pair"] == key
                                      or key.startswith(p["kraken_pair"][:6])), None)
                        if not match:
                            # Try matching by checking if key contains the kraken_pair
                            for p in usd_pairs:
                                if p["kraken_pair"] in key or key in p["kraken_pair"]:
                                    match = p
                                    break
                        if match:
                            match["last"] = last
                            match["vol_usd"] = vol_usd
                            qualified.append(match)
            except Exception:
                pass
            time.sleep(0.5)

        # Deduplicate by display name (Kraken sometimes has both XBTUSD and XXBTZUSD)
        seen = set()
        deduped = []
        for p in qualified:
            if p["display"] not in seen:
                seen.add(p["display"])
                deduped.append(p)

        deduped.sort(key=lambda x: x.get("vol_usd", 0), reverse=True)
        top = deduped[:UNIVERSE_SIZE]

        # Build pair map and return display names
        _pair_map.clear()
        symbols = []
        for p in top:
            _pair_map[p["display"]] = p["kraken_pair"]
            symbols.append(p["display"])

        add_alert("SYSTEM", f"Universe: {len(symbols)} Kraken USD pairs", "INFO")
        return symbols

    except Exception as e:
        add_alert("SYSTEM", f"Universe fetch failed: {e}", "WARN")
        return []


def fetch_ohlc(symbol: str, interval: str, limit: int) -> List[Candle]:
    """Fetch OHLC candles — CC proxy first, Kraken fallback (canonical module).
    symbol: display name like 'BTC/USD'
    interval: our tf name like '5m', '15m', '1h'
    """
    kraken_interval = KRAKEN_INTERVALS.get(interval, 60)
    if _fetch_ohlc_canonical:
        raw = _fetch_ohlc_canonical(symbol, kraken_interval, limit, cc_url=CC_URL)
        if raw:
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
        return []
    # Fallback: original implementation when canonical module absent
    try:
        cc = _cc_fetch(f"/api/market/ohlc?pair={symbol}&interval={kraken_interval}&limit={limit}")
        if cc and cc.get("candles"):
            candles = []
            for r in cc["candles"]:
                candles.append(Candle(
                    timestamp=float(r[0]) if isinstance(r, list) else float(r.get("time", 0)),
                    open=float(r[1]) if isinstance(r, list) else float(r.get("open", 0)),
                    high=float(r[2]) if isinstance(r, list) else float(r.get("high", 0)),
                    low=float(r[3]) if isinstance(r, list) else float(r.get("low", 0)),
                    close=float(r[4]) if isinstance(r, list) else float(r.get("close", 0)),
                    volume=float(r[6]) if isinstance(r, list) else float(r.get("volume", 0)),
                ))
            if candles:
                return candles[-limit:] if len(candles) > limit else candles
    except Exception:
        pass
    kraken_pair_symbol = _pair_map.get(symbol)
    if not kraken_pair_symbol:
        return []
    try:
        params = {"pair": kraken_pair_symbol, "interval": kraken_interval}
        resp = requests.get(f"{KRAKEN_REST}/OHLC", params=params, timeout=15)
        data = resp.json()
        if data.get("error") and data["error"]:
            return []
        candles = []
        for key, rows in data.get("result", {}).items():
            if key == "last":
                continue
            for r in rows:
                candles.append(Candle(
                    timestamp=float(r[0]),
                    open=float(r[1]),
                    high=float(r[2]),
                    low=float(r[3]),
                    close=float(r[4]),
                    volume=float(r[6]),
                ))
            break
        return candles[-limit:] if len(candles) > limit else candles
    except Exception as e:
        add_alert(symbol, f"OHLC fetch fail ({interval}): {e}", "WARN")
        return []


# ─────────────────────────────────────────────
# WORKERS
# ─────────────────────────────────────────────

def ohlc_worker():
    """Background worker: periodically refresh OHLC data and recompute signals."""
    while not shutdown_event.is_set():
        with state_lock:
            syms = list(live_prices.keys())
        for sym in syms:
            if shutdown_event.is_set():
                return
            sym_data = {}
            for tf_name, tf_cfg in TIMEFRAMES.items():
                candles = fetch_ohlc(sym, tf_cfg["interval"], tf_cfg["limit"])
                if candles:
                    sym_data[tf_name] = candles
            if sym_data:
                with state_lock:
                    ohlc_data[sym] = sym_data
                    price = live_prices.get(sym, 0)
                if price > 0:
                    sig_r = generate_signal(sym, sym_data, price)
                    if sig_r:
                        with state_lock:
                            old = signals.get(sym)
                            signals[sym] = sig_r
                        # Record actionable signals (skip blacklisted pairs)
                        if sig_r.bias != "NEUTRAL" and sig_r.confluence > 0.6:
                            if not old or old.bias != sig_r.bias:
                                if _is_blacklisted(sym):
                                    add_alert(sym, f"{sig_r.bias} signal BLACKLISTED | conf={sig_r.confluence:.2f} regime={sig_r.regime}", "WARN")
                                else:
                                    record_signal(sig_r)
                                    add_alert(sym, f"{sig_r.bias} signal | conf={sig_r.confluence:.2f} regime={sig_r.regime}", "SIGNAL")
            time.sleep(0.3)  # rate limit
        # Track open signals
        track_signals()
        # Update equity curve
        with state_lock:
            wins, losses, opens = get_win_rate()
            total = wins + losses
            if total > 0:
                wr = wins / total
                pnl_estimate = DEFAULT_EQUITY * (1 + (wr - 0.5) * 0.1 * total)
                equity_curve.append(round(pnl_estimate, 2))
                if len(equity_curve) > 200:
                    equity_curve.pop(0)

        # Publish regime change to fleet event bus
        global _last_regime
        if _event_pub:
            with state_lock:
                current_top = top_symbols[:1]
            if current_top:
                top_sym = current_top[0][0]
                top_sig = signals.get(top_sym)
                if top_sig and top_sig.regime:
                    current_regime = top_sig.regime
                    if _last_regime and current_regime != _last_regime:
                        try:
                            _event_pub.emit("REGIME_CHANGE", {
                                "from": _last_regime,
                                "to": current_regime,
                                "source": "trinity",
                            })
                        except Exception:
                            pass
                    _last_regime = current_regime

        for _ in range(int(OHLC_REFRESH / 0.5)):
            if shutdown_event.is_set():
                return
            time.sleep(0.5)


def ranking_worker():
    """Background worker: recompute rankings every few seconds."""
    while not shutdown_event.is_set():
        rankings = compute_rankings()
        with state_lock:
            global top_symbols
            top_symbols = rankings
        for _ in range(10):
            if shutdown_event.is_set():
                return
            time.sleep(0.5)


# ─────────────────────────────────────────────
# LIVE PRICE POLLER
# ─────────────────────────────────────────────

def start_price_poller(symbols: List[str]):
    """Poll prices — CC ticker first, Kraken fallback. Runs forever until shutdown."""
    with state_lock:
        ws_state["status"] = "CONNECTED"
    add_alert("PRICE", f"Price poller started -- {len(symbols)} symbols", "INFO")

    while not shutdown_event.is_set():
        got_prices = False
        # Try Command Center ticker first
        try:
            cc = _cc_fetch("/api/market/ticker")
            if cc and isinstance(cc, dict):
                with state_lock:
                    ws_state["status"] = "CONNECTED"
                    ws_state["last_msg_time"] = time.time()
                for sym in symbols:
                    price = cc.get(sym)
                    if price and float(price) > 0:
                        p = float(price)
                        with state_lock:
                            live_prices[sym] = p
                            if sym not in rolling_prices:
                                rolling_prices[sym] = deque(maxlen=ROLLING_TICKS)
                            rolling_prices[sym].append(p)
                            ws_state["messages_total"] += 1
                        got_prices = True
        except Exception:
            pass

        # Fallback: Kraken direct
        if not got_prices:
            try:
                kraken_pairs = [_pair_map.get(s) for s in symbols if _pair_map.get(s)]
                if kraken_pairs:
                    pair_str = ",".join(kraken_pairs)
                    resp = requests.get(f"{KRAKEN_REST}/Ticker?pair={pair_str}", timeout=15)
                    data = resp.json()

                    if data.get("error") and data["error"]:
                        with state_lock:
                            ws_state["status"] = "ERROR"
                        time.sleep(5)
                        continue

                    with state_lock:
                        ws_state["status"] = "CONNECTED"
                        ws_state["last_msg_time"] = time.time()

                    for key, tinfo in data.get("result", {}).items():
                        last = float(tinfo.get("c", [0])[0])
                        if last <= 0:
                            continue
                        display = None
                        for d, kp in _pair_map.items():
                            if kp == key or key.startswith(kp[:6]) or kp in key or key in kp:
                                display = d
                                break
                        if display:
                            with state_lock:
                                live_prices[display] = last
                                if display not in rolling_prices:
                                    rolling_prices[display] = deque(maxlen=ROLLING_TICKS)
                                rolling_prices[display].append(last)
                                ws_state["messages_total"] += 1

            except Exception as e:
                with state_lock:
                    ws_state["status"] = "ERROR"
                    ws_state["reconnects"] += 1
                add_alert("PRICE", f"Ticker poll error: {e}", "WARN")
                time.sleep(5)
                continue

        # Poll every 3 seconds
        for _ in range(6):
            if shutdown_event.is_set():
                return
            time.sleep(0.5)


# ─────────────────────────────────────────────
# ANSI TERMINAL DISPLAY
# ─────────────────────────────────────────────

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_GREEN = "\033[92m"
ANSI_RED = "\033[91m"
ANSI_YELLOW = "\033[93m"
ANSI_CYAN = "\033[96m"
ANSI_MAGENTA = "\033[95m"
ANSI_WHITE = "\033[97m"
ANSI_BG_DARK = "\033[40m"
ANSI_CLEAR = "\033[2J\033[H"


def sparkline(values, width: int = 20) -> str:
    """Generate a sparkline string from a list of values."""
    if not values:
        return ""
    blocks = " ▁▂▃▄▅▆▇█"
    vals = list(values)[-width:]
    lo = min(vals)
    hi = max(vals)
    rng = hi - lo
    if rng == 0:
        return blocks[4] * len(vals)
    return "".join(blocks[min(8, int((v - lo) / rng * 8))] for v in vals)


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


def bias_color(bias: str) -> str:
    if bias == "LONG":
        return ANSI_GREEN
    elif bias == "SHORT":
        return ANSI_RED
    return ANSI_YELLOW


def regime_color(regime: str) -> str:
    if regime == "TRENDING":
        return ANSI_CYAN
    elif regime == "VOLATILE":
        return ANSI_RED
    return ANSI_DIM


def level_color(level: str) -> str:
    colors = {
        "INFO": ANSI_DIM,
        "WARN": ANSI_YELLOW,
        "SIGNAL": ANSI_GREEN,
        "EXIT": ANSI_RED,
    }
    return colors.get(level, ANSI_DIM)


def render_dashboard():
    """Render the full ANSI dashboard to terminal."""
    with state_lock:
        top = list(top_symbols)
        alerts = list(alert_log)
        ws_snap = dict(ws_state)
        price_snap = dict(live_prices)
        sig_snap = dict(signals)
        roll_snap = {k: list(v) for k, v in rolling_prices.items()}

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    uptime = time.time() - ws_snap["uptime_start"]
    up_min = int(uptime // 60)
    up_sec = int(uptime % 60)
    streaming = sum(1 for p in price_snap.values() if p > 0)

    wins, losses, opens = get_win_rate()
    total_tracks = wins + losses
    wr = f"{wins/total_tracks*100:.1f}%" if total_tracks > 0 else "N/A"

    lines = []
    lines.append(ANSI_CLEAR)
    lines.append(f"{ANSI_BG_DARK}{ANSI_BOLD}{ANSI_CYAN}")
    lines.append("=" * 78)
    lines.append("  TRINITY OVERWATCH v2.0  —  Crypto Signal Intelligence Engine")
    lines.append("=" * 78)
    lines.append(ANSI_RESET)
    lines.append("")

    # Status bar
    ws_color = ANSI_GREEN if ws_snap["status"] == "CONNECTED" else ANSI_RED
    lines.append(
        f"  {ANSI_DIM}WS:{ANSI_RESET} {ws_color}{ws_snap['status']}{ANSI_RESET}"
        f"  {ANSI_DIM}Msgs:{ANSI_RESET} {ws_snap['messages_total']:,}"
        f"  {ANSI_DIM}Recon:{ANSI_RESET} {ws_snap['reconnects']}"
        f"  {ANSI_DIM}Stream:{ANSI_RESET} {streaming}/{len(price_snap)}"
        f"  {ANSI_DIM}Up:{ANSI_RESET} {up_min}m{up_sec:02d}s"
        f"  {ANSI_DIM}Time:{ANSI_RESET} {now}"
    )
    lines.append(
        f"  {ANSI_DIM}Signals:{ANSI_RESET} W={ANSI_GREEN}{wins}{ANSI_RESET}"
        f" L={ANSI_RED}{losses}{ANSI_RESET}"
        f" O={ANSI_YELLOW}{opens}{ANSI_RESET}"
        f"  {ANSI_DIM}WR:{ANSI_RESET} {wr}"
        f"  {ANSI_DIM}Dashboard:{ANSI_RESET} http://localhost:{DASHBOARD_PORT}"
    )
    lines.append("")

    # Top symbols
    lines.append(f"  {ANSI_BOLD}{ANSI_WHITE}{'Symbol':<12} {'Price':>12} {'Conf':>6} {'Bias':>7} {'Regime':>10} {'MTF':>5} {'Score':>6} {'Sparkline':>22}{ANSI_RESET}")
    lines.append(f"  {ANSI_DIM}{'─'*82}{ANSI_RESET}")

    if top:
        for sym, score in top:
            sig_r = sig_snap.get(sym)
            if not sig_r:
                continue
            spark = sparkline(roll_snap.get(sym, []))
            bc = bias_color(sig_r.bias)
            rc = regime_color(sig_r.regime)
            bl_tag = f" {ANSI_RED}[BL]{ANSI_RESET}" if _is_blacklisted(sym) else ""
            lines.append(
                f"  {ANSI_WHITE}{sym:<12}{ANSI_RESET}"
                f" {format_price(sig_r.price):>12}"
                f" {sig_r.confluence:>6.2f}"
                f" {bc}{sig_r.bias:>7}{ANSI_RESET}"
                f" {rc}{sig_r.regime:>10}{ANSI_RESET}"
                f" {sig_r.mtf_alignment:>5.2f}"
                f" {score:>6.2f}"
                f" {ANSI_CYAN}{spark:>22}{ANSI_RESET}"
                f"{bl_tag}"
            )
            # Indicator breakdown
            ind_str = "  "
            for k, v in sig_r.indicators.items():
                clr = ANSI_GREEN if v > 0.6 else (ANSI_RED if v < 0.4 else ANSI_DIM)
                ind_str += f" {clr}{k}={v:.2f}{ANSI_RESET}"
            lines.append(f"    {ANSI_DIM}{ind_str}{ANSI_RESET}")
            # Risk levels
            lines.append(
                f"    {ANSI_DIM}SL={format_price(sig_r.stop_loss)}"
                f"  TP1={format_price(sig_r.take_profit_tiers[0]) if sig_r.take_profit_tiers else '-.--'}"
                f"  TP2={format_price(sig_r.take_profit_tiers[1]) if len(sig_r.take_profit_tiers) > 1 else '-.--'}"
                f"  TP3={format_price(sig_r.take_profit_tiers[2]) if len(sig_r.take_profit_tiers) > 2 else '-.--'}"
                f"  Size={sig_r.position_size:.4f}{ANSI_RESET}"
            )
            lines.append("")
    else:
        lines.append(f"    {ANSI_DIM}Loading signals...{ANSI_RESET}")
        lines.append("")

    # Alert log
    lines.append(f"  {ANSI_BOLD}{ANSI_WHITE}Alert Log{ANSI_RESET}")
    lines.append(f"  {ANSI_DIM}{'─'*78}{ANSI_RESET}")
    if alerts:
        for a in list(alerts)[-10:]:
            lc = level_color(a.level)
            lines.append(f"  {ANSI_DIM}{a.timestamp}{ANSI_RESET} {lc}[{a.level:>6}]{ANSI_RESET} {a.symbol:<10} {a.message}")
    else:
        lines.append(f"    {ANSI_DIM}No alerts yet{ANSI_RESET}")

    lines.append("")
    lines.append(f"  {ANSI_DIM}Ctrl+C to exit{ANSI_RESET}")
    lines.append("")

    print("\n".join(lines), flush=True)


def dashboard_worker():
    """Background worker: refresh terminal display."""
    while not shutdown_event.is_set():
        try:
            render_dashboard()
        except Exception:
            pass
        for _ in range(int(DASH_REFRESH / 0.25)):
            if shutdown_event.is_set():
                return
            time.sleep(0.25)


# ─────────────────────────────────────────────
# SNAPSHOT (JSON-SERIALIZABLE ENGINE STATE)
# ─────────────────────────────────────────────

def _safe_float(v):
    """Ensure a float is JSON-serializable (no inf/nan)."""
    if v is None:
        return 0.0
    if isinstance(v, float):
        if math.isinf(v) or math.isnan(v):
            return 0.0
    return v


def build_snapshot() -> dict:
    """Build JSON-serializable snapshot of entire engine state."""
    with state_lock:
        # Top symbols with full signal data
        top = []
        for s, score in top_symbols:
            sig_r = signals.get(s)
            if not sig_r:
                continue
            dq = rolling_prices.get(s, deque())
            top.append({
                "symbol": s,
                "composite_score": round(_safe_float(score), 2),
                "price": round(_safe_float(sig_r.price), 8),
                "confluence": round(_safe_float(sig_r.confluence), 2),
                "bias": sig_r.bias,
                "regime": sig_r.regime,
                "mtf_alignment": round(_safe_float(sig_r.mtf_alignment), 2),
                "mtf_conflict": bool(getattr(sig_r, "mtf_conflict", False)),
                "stop_loss": round(_safe_float(sig_r.stop_loss), 8),
                "take_profit_tiers": [round(_safe_float(t), 8) for t in sig_r.take_profit_tiers],
                "position_size": round(_safe_float(sig_r.position_size), 4),
                "indicators": {k: round(_safe_float(v), 2) for k, v in sig_r.indicators.items()},
                "sparkline": [_safe_float(x) for x in dq],
                "timestamp": sig_r.timestamp,
                "blacklisted": _is_blacklisted(s),
            })

        # All tracked signals for win/loss
        all_tracks = []
        for sym, hist in signal_history.items():
            for t in hist:
                all_tracks.append({
                    "symbol": sym,
                    "bias": t.bias,
                    "entry_price": round(_safe_float(t.entry_price), 8),
                    "entry_time": t.entry_time,
                    "stop": round(_safe_float(t.stop), 8),
                    "tiers": [round(_safe_float(x), 8) for x in t.tiers],
                    "peak_pnl": round(_safe_float(t.peak_pnl), 4),
                    "result": t.result,
                    "blacklisted": _is_blacklisted(sym),
                })

        # Alert log
        alerts = [{"timestamp": a.timestamp, "symbol": a.symbol,
                    "message": a.message, "level": a.level}
                   for a in alert_log]

        # Win rate
        wins, losses, opens = get_win_rate()
        total = wins + losses

        # All live prices
        all_prices = {k: _safe_float(v) for k, v in live_prices.items()}

        # Equity curve copy
        eq_curve = list(equity_curve)

    return {
        "timestamp": time.time(),
        "uptime": _safe_float(time.time() - ws_state.get("uptime_start", time.time())),
        "ws_status": ws_state.get("status", "UNKNOWN"),
        "ws_messages": ws_state.get("messages_total", 0),
        "ws_reconnects": ws_state.get("reconnects", 0),
        "symbols_streaming": sum(1 for p in all_prices.values() if p > 0),
        "symbols_total": len(all_prices),
        "live_prices": all_prices,
        "top_symbols": top,
        "signal_tracks": all_tracks[-50:],
        "alerts": alerts,
        "wins": wins,
        "losses": losses,
        "opens": opens,
        # null, not 0, when nothing has resolved yet. Sending 0 made the
        # dashboard render a confident red "WIN RATE 0.0%" next to "W/L 0/0"
        # for a bot that has simply never closed a signal — an unmeasured
        # quantity displayed as a measured zero. The CLI already got this
        # right (prints "N/A"); only the API lied (2026-07-29 audit).
        "win_rate": round(_safe_float(wins / total * 100), 1) if total > 0 else None,
        "resolved_trades": total,
        "equity_curve": eq_curve,
        "config": {
            "universe_size": UNIVERSE_SIZE,
            "top_display": TOP_DISPLAY,
            "timeframes": list(TIMEFRAMES.keys()),
            "weights": WEIGHTS,
            "risk_per_trade_pct": RISK_PER_TRADE_PCT,
            "default_equity": DEFAULT_EQUITY,
        },
    }


# ─────────────────────────────────────────────
# DASHBOARD HTTP SERVER
# ─────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    """Serves dashboard.html on / and JSON snapshot on /api/snapshot."""

    def log_message(self, format, *args):
        """Suppress default HTTP logging so it doesn't clobber the TUI."""
        pass

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/snapshot":
            self._serve_snapshot()
        elif self.path == "/" or self.path == "/index.html":
            self._serve_dashboard()
        elif self.path == "/health":
            self._serve_health()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def _serve_snapshot(self):
        try:
            data = build_snapshot()
            body = json.dumps(data, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def _serve_health(self):
        body = json.dumps({"status": "ok", "timestamp": time.time()}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_dashboard(self):
        # Try to serve dashboard.html from same directory
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8") as f:
                body = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            # Serve a minimal fallback page
            fallback = FALLBACK_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(fallback)))
            self.end_headers()
            self.wfile.write(fallback)


FALLBACK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Trinity Overwatch v2.0</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0a0a0f; color: #c0c0c0; font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 14px; }
  .header { text-align: center; padding: 24px; border-bottom: 1px solid #1a1a2e; }
  .header h1 { color: #00d4ff; font-size: 22px; letter-spacing: 2px; }
  .header .sub { color: #555; font-size: 12px; margin-top: 4px; }
  .status-bar { display: flex; justify-content: center; gap: 24px; padding: 12px; background: #0d0d18; flex-wrap: wrap; }
  .status-bar .item { font-size: 12px; }
  .status-bar .label { color: #555; }
  .status-bar .val { color: #aaa; }
  .status-bar .val.green { color: #00e676; }
  .status-bar .val.red { color: #ff5252; }
  .status-bar .val.yellow { color: #ffd740; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 16px; max-width: 1400px; margin: auto; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  .panel { background: #0d0d18; border: 1px solid #1a1a2e; border-radius: 6px; padding: 16px; }
  .panel h2 { color: #00d4ff; font-size: 14px; margin-bottom: 12px; border-bottom: 1px solid #1a1a2e; padding-bottom: 6px; }
  .panel.full { grid-column: 1 / -1; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { color: #555; text-align: left; padding: 4px 8px; border-bottom: 1px solid #1a1a2e; }
  td { padding: 4px 8px; border-bottom: 1px solid #0f0f1a; }
  .long { color: #00e676; }
  .short { color: #ff5252; }
  .neutral { color: #ffd740; }
  .trending { color: #00d4ff; }
  .ranging { color: #888; }
  .volatile { color: #ff5252; }
  .alert-row { font-size: 11px; padding: 3px 0; border-bottom: 1px solid #0f0f1a; }
  .alert-time { color: #555; }
  .alert-sym { color: #aaa; min-width: 80px; display: inline-block; }
  .ind-bar { display: inline-block; width: 60px; height: 8px; background: #1a1a2e; border-radius: 4px; vertical-align: middle; overflow: hidden; }
  .ind-fill { height: 100%; border-radius: 4px; }
  .sparkline { color: #00d4ff; letter-spacing: 1px; }
  .wr-box { text-align: center; padding: 12px; }
  .wr-big { font-size: 32px; font-weight: bold; }
  .eq-curve { width: 100%; height: 60px; }
  #loading { text-align: center; padding: 48px; color: #555; font-size: 16px; }
</style>
</head>
<body>
<div class="header">
  <h1>TRINITY OVERWATCH v2.0</h1>
  <div class="sub">Crypto Signal Intelligence Engine</div>
</div>
<div class="status-bar" id="statusBar">
  <div class="item"><span class="label">WS:</span> <span class="val" id="wsStatus">--</span></div>
  <div class="item"><span class="label">Msgs:</span> <span class="val" id="wsMsgs">0</span></div>
  <div class="item"><span class="label">Symbols:</span> <span class="val" id="symCount">0/0</span></div>
  <div class="item"><span class="label">Uptime:</span> <span class="val" id="uptime">0m00s</span></div>
  <div class="item"><span class="label">W/L/O:</span> <span class="val green" id="wins">0</span>/<span class="val red" id="losses">0</span>/<span class="val yellow" id="opens">0</span></div>
  <div class="item"><span class="label">WR:</span> <span class="val" id="winRate">N/A</span></div>
</div>
<div class="grid" id="mainGrid">
  <div id="loading">Connecting to engine...</div>
</div>
<script>
const API = '/api/snapshot';
let lastData = null;

function fmt(p) {
  if (!p || p === 0) return '-.--';
  if (p >= 1000) return p.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
  if (p >= 1) return p.toFixed(4);
  if (p >= 0.01) return p.toFixed(6);
  return p.toFixed(8);
}

function spark(vals) {
  if (!vals || !vals.length) return '';
  const blocks = ' \\u2581\\u2582\\u2583\\u2584\\u2585\\u2586\\u2587\\u2588';
  const lo = Math.min(...vals), hi = Math.max(...vals), rng = hi - lo;
  if (rng === 0) return blocks[4].repeat(vals.length);
  return vals.slice(-20).map(v => blocks[Math.min(8, Math.floor((v-lo)/rng*8))]).join('');
}

function indColor(v) {
  if (v > 0.6) return '#00e676';
  if (v < 0.4) return '#ff5252';
  return '#888';
}

function biasClass(b) { return (b||'').toLowerCase(); }
function regimeClass(r) { return (r||'').toLowerCase(); }

function renderSymbols(data) {
  if (!data.top_symbols || !data.top_symbols.length) return '<div class="panel full"><h2>Top Signals</h2><p style="color:#555">Loading signals...</p></div>';
  let html = '<div class="panel full"><h2>Top Signals</h2><table><tr><th>Symbol</th><th>Price</th><th>Conf</th><th>Bias</th><th>Regime</th><th>MTF</th><th>Score</th><th>Sparkline</th></tr>';
  for (const s of data.top_symbols) {
    html += '<tr>';
    const blTag = s.blacklisted ? ' <span style="color:#ff5252;font-size:10px;font-weight:bold">[BL]</span>' : '';
    html += '<td style="color:#fff">' + s.symbol + blTag + '</td>';
    html += '<td>' + fmt(s.price) + '</td>';
    html += '<td>' + s.confluence.toFixed(2) + '</td>';
    html += '<td class="' + biasClass(s.bias) + '">' + s.bias + '</td>';
    html += '<td class="' + regimeClass(s.regime) + '">' + s.regime + '</td>';
    html += '<td>' + s.mtf_alignment.toFixed(2) + '</td>';
    html += '<td>' + s.composite_score.toFixed(2) + '</td>';
    html += '<td class="sparkline">' + spark(s.sparkline) + '</td>';
    html += '</tr>';
    // indicators row
    html += '<tr><td colspan="8" style="padding:2px 8px 8px 20px;font-size:11px">';
    if (s.indicators) {
      for (const [k,v] of Object.entries(s.indicators)) {
        const c = indColor(v);
        html += '<span style="color:' + c + ';margin-right:10px">' + k + '=' + v.toFixed(2) + '</span>';
      }
    }
    html += '<br><span style="color:#555">SL=' + fmt(s.stop_loss) + '  ';
    if (s.take_profit_tiers) {
      s.take_profit_tiers.forEach((t,i) => { html += 'TP'+(i+1)+'='+fmt(t)+'  '; });
    }
    html += 'Size=' + s.position_size.toFixed(4) + '</span>';
    html += '</td></tr>';
  }
  html += '</table></div>';
  return html;
}

function renderAlerts(data) {
  let html = '<div class="panel"><h2>Alert Log</h2>';
  if (!data.alerts || !data.alerts.length) {
    html += '<p style="color:#555">No alerts yet</p>';
  } else {
    const recent = data.alerts.slice(-15);
    for (const a of recent) {
      const lc = a.level === 'SIGNAL' ? '#00e676' : a.level === 'WARN' ? '#ffd740' : a.level === 'EXIT' ? '#ff5252' : '#555';
      html += '<div class="alert-row"><span class="alert-time">' + a.timestamp + '</span> ';
      html += '<span style="color:' + lc + '">[' + a.level + ']</span> ';
      html += '<span class="alert-sym">' + a.symbol + '</span> ' + a.message + '</div>';
    }
  }
  html += '</div>';
  return html;
}

function renderTracks(data) {
  let html = '<div class="panel"><h2>Signal Tracks</h2>';
  if (!data.signal_tracks || !data.signal_tracks.length) {
    html += '<p style="color:#555">No signals tracked yet</p>';
  } else {
    html += '<table><tr><th>Symbol</th><th>Bias</th><th>Entry</th><th>P/L%</th><th>Result</th></tr>';
    const recent = data.signal_tracks.slice(-15);
    for (const t of recent) {
      const rc = t.result === 'WIN' ? '#00e676' : t.result === 'LOSS' ? '#ff5252' : '#ffd740';
      html += '<tr><td>' + t.symbol + '</td>';
      html += '<td class="' + biasClass(t.bias) + '">' + t.bias + '</td>';
      html += '<td>' + fmt(t.entry_price) + '</td>';
      html += '<td>' + t.peak_pnl.toFixed(2) + '%</td>';
      html += '<td style="color:' + rc + '">' + t.result + '</td></tr>';
    }
    html += '</table>';
  }
  html += '</div>';
  return html;
}

function renderWinRate(data) {
  const total = data.wins + data.losses;
  const wr = total > 0 ? (data.wins / total * 100).toFixed(1) + '%' : 'N/A';
  const wrColor = total === 0 ? '#555' : (data.wins/total >= 0.5 ? '#00e676' : '#ff5252');
  let html = '<div class="panel"><h2>Performance</h2><div class="wr-box">';
  html += '<div class="wr-big" style="color:' + wrColor + '">' + wr + '</div>';
  html += '<div style="color:#555;margin-top:4px">Win Rate (' + total + ' closed)</div>';
  html += '</div>';
  // equity curve as sparkline
  if (data.equity_curve && data.equity_curve.length > 1) {
    html += '<div style="text-align:center;margin-top:12px"><span style="color:#555">Equity: </span><span class="sparkline" style="font-size:16px">' + spark(data.equity_curve) + '</span></div>';
  }
  html += '</div>';
  return html;
}

async function refresh() {
  try {
    const res = await fetch(API);
    if (!res.ok) return;
    const data = await res.json();
    lastData = data;

    // Status bar
    const wsEl = document.getElementById('wsStatus');
    wsEl.textContent = data.ws_status;
    wsEl.className = 'val ' + (data.ws_status === 'CONNECTED' ? 'green' : 'red');
    document.getElementById('wsMsgs').textContent = (data.ws_messages||0).toLocaleString();
    document.getElementById('symCount').textContent = data.symbols_streaming + '/' + data.symbols_total;
    const up = data.uptime || 0;
    document.getElementById('uptime').textContent = Math.floor(up/60) + 'm' + ('0'+Math.floor(up%60)).slice(-2) + 's';
    document.getElementById('wins').textContent = data.wins;
    document.getElementById('losses').textContent = data.losses;
    document.getElementById('opens').textContent = data.opens;
    document.getElementById('winRate').textContent = data.win_rate > 0 ? data.win_rate + '%' : 'N/A';

    // Main grid
    const grid = document.getElementById('mainGrid');
    grid.innerHTML = renderSymbols(data) + renderAlerts(data) + renderTracks(data) + renderWinRate(data);
  } catch (e) {
    // silent retry
  }
}

setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>"""


class DashboardServer:
    """Threaded HTTP server for the web dashboard."""

    def __init__(self, port: int = DASHBOARD_PORT):
        self.port = port
        self.server = None
        self.thread = None

    def start(self):
        """Start the dashboard server in a daemon thread."""
        try:
            # ThreadingHTTPServer: the plain HTTPServer serves one request at a time,
            # so while this bot computes, its port stops answering and Command Center's
            # health check reports it DOWN even though it is healthy.
            self.server = ThreadingHTTPServer(("0.0.0.0", self.port), DashboardHandler)
            self.server.daemon_threads = True
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="DashboardServer")
            self.thread.start()
            add_alert("HTTP", f"Dashboard server started on port {self.port}", "INFO")
        except Exception as e:
            add_alert("HTTP", f"Dashboard server failed to start: {e}", "WARN")

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def start_bot(auto: bool = False):
    """Main entry point — start all workers and the WebSocket."""
    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        ensure_port(DASHBOARD_PORT, "trinity")
        write_pidfile("trinity", DASHBOARD_PORT)
        atexit.register(cleanup_pidfile, "trinity")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    ws_state["uptime_start"] = time.time()

    print("\033[96m")
    print("=" * 60)
    print("  TRINITY OVERWATCH v2.0")
    print("  Crypto Signal Intelligence Engine")
    print("=" * 60)
    print("\033[0m")
    print(f"  Dashboard: http://localhost:{DASHBOARD_PORT}")
    print(f"  API:       http://localhost:{DASHBOARD_PORT}/api/snapshot")
    print()

    if not auto:
        try:
            input("  Press Enter to start (or run with --auto to skip)...")
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return

    # Start dashboard HTTP server FIRST so Command Center sees us alive
    dash_server = DashboardServer(DASHBOARD_PORT)
    dash_server.start()

    print("  Fetching universe...")
    symbols = []
    for _attempt in range(3):
        symbols = fetch_universe()
        if symbols:
            break
        print(f"  Universe fetch attempt {_attempt + 1}/3 failed, retrying in 5s...")
        time.sleep(5)
    if not symbols:
        print("  ERROR: Could not fetch symbol universe after 3 attempts. Check network.")
        return

    print(f"  Tracking {len(symbols)} symbols: {', '.join(symbols[:5])}...")
    with state_lock:
        for s in symbols:
            live_prices[s] = 0.0

    # Start background workers
    ohlc_t = threading.Thread(target=ohlc_worker, daemon=True, name="OHLCWorker")
    rank_t = threading.Thread(target=ranking_worker, daemon=True, name="RankingWorker")

    ohlc_t.start()
    rank_t.start()

    # Only start the TUI display if running interactively (not --auto)
    if not auto:
        display_t = threading.Thread(target=dashboard_worker, daemon=True, name="DisplayWorker")
        display_t.start()

    # Start Kraken price poller in a thread
    price_t = threading.Thread(target=start_price_poller, args=(symbols,), daemon=True, name="PricePoller")
    price_t.start()

    # Block main thread until shutdown
    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        dash_server.stop()
        print(f"\n{ANSI_CYAN}  Trinity Overwatch shutting down...{ANSI_RESET}")


def main():
    auto = "--auto" in sys.argv

    # Graceful shutdown on Ctrl+C
    def handle_sig(signum, frame):
        shutdown_event.set()
        sys.exit(0)

    sig.signal(sig.SIGINT, handle_sig)
    try:
        sig.signal(sig.SIGTERM, handle_sig)
    except (OSError, AttributeError):
        pass  # SIGTERM not available on all platforms

    start_bot(auto=auto)


if __name__ == "__main__":
    main()
