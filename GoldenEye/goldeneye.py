import os, sys, json, time, argparse, logging, yaml, threading, requests, math
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Lock

# GoldenEye STANDALONE — no fleet, no Command Center, no OPB
# He works alone. Built for Telegram. Scambot destroyer.
_expectancy = None
_is_blacklisted = lambda pair: False
_fc = None
# Paper mode: True unless config.yaml says trading_mode: "live" AND Kraken keys are present.
# Keys: env vars first, then hardcoded fallback for standalone deployment.
_KRAKEN_KEY = os.environ.get('KRAKEN_API_KEY', '')
_KRAKEN_SECRET = os.environ.get('KRAKEN_API_SECRET', '')
_HAS_KEYS = bool(_KRAKEN_KEY and _KRAKEN_SECRET)
_STANDALONE_MODE = "paper"  # overridden from config.yaml in main()
def _is_paper():
    if not _HAS_KEYS:
        return True
    return _STANDALONE_MODE != "live"
def _PAPER():
    return _is_paper()
_PAPER_SLIP = Decimal('0.001')
_PAPER_START_BAL = Decimal('10000')
_PAPER_BALANCE = Decimal('10000')
_PAPER_BAL_LOCK = Lock()
_TRADE_AMT = Decimal('500')         # paper mode default
_LIVE_BALANCE = Decimal('0')        # fetched from Kraken on startup + periodic refresh
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')  # anchored to script location
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))  # root directory for log files
_LIVE_PEAK = Decimal('0')           # high-water mark for drawdown computation (live mode)
_LIVE_BAL_LOCK = Lock()
_LIVE_BAL_LAST_SYNC = 0.0
_MAX_HEAT = Decimal('30')           # portfolio heat cap $
_MAX_OPEN_POS = 20                  # max simultaneous positions
_TELEGRAM_MESSAGES = deque(maxlen=50)  # recent outbound Telegram messages
_TP1_MULT = Decimal('0.75')   # 0.75R — default (used as fallback)
_TP2_MULT = Decimal('1.25')   # 1.25R — default
_TP3_MULT = Decimal('2.0')    # 2.0R — default
# Regime-adaptive TP multipliers: trending regimes let winners run,
# range/chop take profits earlier where targets are less reachable
_TP_BY_REGIME = {
    'bull':  {'tp1': Decimal('0.75'), 'tp2': Decimal('1.50'), 'tp3': Decimal('2.50')},
    'bear':  {'tp1': Decimal('0.50'), 'tp2': Decimal('0.75'), 'tp3': Decimal('1.25')},
    'range': {'tp1': Decimal('0.50'), 'tp2': Decimal('0.75'), 'tp3': Decimal('1.25')},
    'chop':  {'tp1': Decimal('0.50'), 'tp2': Decimal('0.75'), 'tp3': Decimal('1.00')},
}
# Short mode swaps bull/bear TP profiles
_TP_BY_REGIME_SHORT = {
    'bull':  {'tp1': Decimal('0.50'), 'tp2': Decimal('0.75'), 'tp3': Decimal('1.25')},
    'bear':  {'tp1': Decimal('0.75'), 'tp2': Decimal('1.50'), 'tp3': Decimal('2.50')},
    'range': {'tp1': Decimal('0.50'), 'tp2': Decimal('0.75'), 'tp3': Decimal('1.25')},
    'chop':  {'tp1': Decimal('0.50'), 'tp2': Decimal('0.75'), 'tp3': Decimal('1.00')},
}

def _get_tp_mults(regime='chop'):
    """Get TP multipliers for current regime and mode. Returns (tp1, tp2, tp3) Decimals."""
    table = _TP_BY_REGIME_SHORT if _MODE == 'short' else _TP_BY_REGIME
    mults = table.get(regime, table.get('chop'))
    return mults['tp1'], mults['tp2'], mults['tp3']

import pandas as pd
import numpy as np

import ccxt

# Bot mode: 'long' or 'short' — set by --mode CLI arg, used throughout
_MODE = 'long'
_DIR = 1  # +1 for long, -1 for short — multiplier for price math
_BRAND = 'GoldenEye'  # display name, set in main()

# Logging to TUI — single global ring buffer, no per-symbol merging
_tui_logs: deque = deque(maxlen=8)

class TUIHandler(logging.Handler):
    def __init__(self): super().__init__()
    def emit(self, record): _tui_logs.append(f"[{datetime.now():%H:%M:%S}] {self.format(record)}")

# Args
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--output-dir", default=None)
    p.add_argument("--mode", choices=["long", "short"], default="long")
    p.add_argument("--backtest", action="store_true")
    p.add_argument("--days", type=int, default=90)
    return p.parse_args()

# State
class BotState:
    def __init__(self, d):
        self.price = self.last_price = Decimal(0)
        self.df = pd.DataFrame(columns=['time','close','vol'])
        self.pos = None
        self.shutdown = threading.Event()
        self.out_dir = d
        self._lock = Lock()
        self.pos_file = os.path.join(d, "position.json")
        os.makedirs(d, exist_ok=True)
        self._load_position()

    def set_position(self, pos: dict):
        with self._lock:
            self.pos = pos
            self._save_position()

    def clear_position(self):
        with self._lock:
            self.pos = None
            self._save_position()

    def _save_position(self):
        try:
            if self.pos:
                data = {}
                for k, v in self.pos.items():
                    if isinstance(v, Decimal):
                        data[k] = str(v)
                    else:
                        data[k] = v
                tmp = self.pos_file + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(data, f)
                os.replace(tmp, self.pos_file)  # atomic on both POSIX and Windows
            elif os.path.exists(self.pos_file):
                os.remove(self.pos_file)
        except Exception as e:
            logging.warning(f"Failed to save position: {e}")

    def _load_position(self):
        if not os.path.exists(self.pos_file):
            return
        try:
            with open(self.pos_file) as f:
                data = json.load(f)
            for k in ('e', 'sl', 'orig_sl', 'tp1', 'tp2', 'tp3', 'size', 'fee'):
                if k in data and data[k] is not None:
                    data[k] = Decimal(data[k])
            if 'slippage' in data:
                data['slippage'] = Decimal(str(data['slippage']))
            self.pos = data
            logging.info(f"Restored position from {self.pos_file}")
        except Exception as e:
            logging.warning(f"Failed to load position: {e}")

    def update_price(self, price: Decimal):
        with self._lock:
            self.last_price = self.price
            self.price = price

    def get_position(self):
        with self._lock:
            return dict(self.pos) if self.pos else None

    def get_price(self):
        with self._lock:
            return self.price, self.last_price

# Dynamic fee lookup
_exchange = None

def get_fee(symbol: str) -> Decimal:
    return Decimal('0.0040')  # Kraken taker 0.40% — paper must simulate real friction

def get_market_limits(symbol: str) -> tuple:
    global _exchange
    if _exchange is None:
        return None, None, None
    try:
        market = _exchange.markets.get(symbol)
        if market:
            amount_limits = market.get('limits', {}).get('amount', {})
            cost_limits = market.get('limits', {}).get('cost', {})
            return (
                amount_limits.get('min'),
                amount_limits.get('max'),
                cost_limits.get('min')
            )
    except Exception as e:
        logging.warning(f"get_market_limits({symbol}) failed: {e}")
    return None, None, None

def set_exchange(ex):
    global _exchange
    _exchange = ex

_global_config = {}

def set_config(c):
    global _global_config
    _global_config = c

def get_symbol_config(symbol: str) -> dict:
    global _global_config
    symbol_configs = _global_config.get('symbols', {})
    return symbol_configs.get(symbol, {})

def get_sl_pct(symbol: str) -> Decimal:
    sym_config = get_symbol_config(symbol)
    default = _global_config.get('sl_pct', 0.01)
    return Decimal(str(sym_config.get('sl_pct', default)))

def get_tp_pct(symbol: str) -> Decimal:
    sym_config = get_symbol_config(symbol)
    default = _global_config.get('tp_pct', 0.02)
    return Decimal(str(sym_config.get('tp_pct', default)))

def is_auction_theory_enabled() -> bool:
    return _global_config.get('auction_theory', {}).get('enabled', True)

def is_ai_enabled() -> bool:
    return _global_config.get('use_ensemble_ai', False)

# AI ensemble globals (lazy-init when enabled)
_ai_in_q = None
_ai_out_q = None

# UPGRADE D: Dynamic LLM blend — Brier score tracking for Ollama predictions
# Tracks (prediction, outcome) pairs over a rolling window to compute calibration
_llm_brier_history = []  # list of (prediction_score, actual_outcome_0or1)
_LLM_BRIER_WINDOW = 50  # rolling window size
_LLM_BRIER_LOCK = Lock()
_LLM_BLEND_MIN = 0.05   # minimum Ollama blend weight (noisy model)
_LLM_BLEND_MAX = 0.30   # maximum Ollama blend weight (well-calibrated model)
_LLM_BLEND_DEFAULT = 0.15  # default when insufficient data

def _compute_llm_blend_weight():
    """UPGRADE D: Compute dynamic LLM blend weight based on Brier score.
    Brier score = mean((prediction - outcome)^2), lower = better calibrated.
    Perfect = 0.0, climatology = 0.25, useless >= 0.25.
    Maps Brier score to blend weight: 0.0 -> max, 0.25 -> min."""
    with _LLM_BRIER_LOCK:
        if len(_llm_brier_history) < 10:
            return _LLM_BLEND_DEFAULT
        recent = _llm_brier_history[-_LLM_BRIER_WINDOW:]
    brier = sum((p - o) ** 2 for p, o in recent) / len(recent)
    # Map: Brier 0.0 -> blend_max, Brier 0.25 -> blend_min
    # Linear interpolation clamped to [min, max]
    if brier <= 0.0:
        return _LLM_BLEND_MAX
    if brier >= 0.25:
        return _LLM_BLEND_MIN
    t = brier / 0.25  # 0 = perfect, 1 = useless
    weight = _LLM_BLEND_MAX - t * (_LLM_BLEND_MAX - _LLM_BLEND_MIN)
    return round(weight, 3)

def _record_llm_prediction(prediction_score, trade_outcome_r):
    """UPGRADE D: Record an Ollama prediction and its trade outcome for Brier scoring."""
    if prediction_score is None:
        return
    outcome = 1.0 if trade_outcome_r > 0 else 0.0
    with _LLM_BRIER_LOCK:
        _llm_brier_history.append((prediction_score, outcome))
        # Trim to window
        if len(_llm_brier_history) > _LLM_BRIER_WINDOW * 2:
            del _llm_brier_history[:len(_llm_brier_history) - _LLM_BRIER_WINDOW]

def get_auction_confluence_threshold() -> float:
    return _global_config.get('auction_theory', {}).get('min_confluence', 0.4)

def _fp(v):
    """Format price with auto-scaled decimals based on magnitude."""
    v = float(v)
    if v >= 1.0: return f"{v:,.2f}"
    if v >= 0.01: return f"{v:,.4f}"
    if v >= 0.0001: return f"{v:,.6f}"
    return f"{v:,.8f}"

_live_metrics = {'trades': 0, 'wins': 0, 'losses': 0, 'total_r': 0.0, 'start_time': time.time(), 'by_direction': {'long': {'trades': 0, 'wins': 0}, 'short': {'trades': 0, 'wins': 0}}, 'r_history': [], 'pnl_history': [], 'trade_log': []}
_session_baseline = {'trades': 0, 'wins': 0, 'losses': 0, 'total_r': 0.0, 'pnl': 0.0}  # snapshot at startup
_metrics_lock = Lock()
_METRICS_FILE = os.path.join('output', 'metrics.json')

def _save_metrics():
    """Persist live metrics to disk (called after every trade, inside _metrics_lock)."""
    try:
        os.makedirs(os.path.dirname(_METRICS_FILE) or 'output', exist_ok=True)
        with _PAPER_BAL_LOCK:
            bal = str(_PAPER_BALANCE)
        snap = {
            'r_history': _live_metrics['r_history'],
            'pnl_history': _live_metrics['pnl_history'],
            'by_direction': _live_metrics['by_direction'],
            'trade_log': _live_metrics['trade_log'][-2000:],  # FEE_SLAYER: match r_history window so by_direction can be recomputed on load
            'paper_balance': bal,
        }
        tmp = _METRICS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(snap, f)
        os.replace(tmp, _METRICS_FILE)  # atomic on POSIX + Windows
    except Exception as e:
        logging.warning(f"Failed to save metrics: {e}")

def _load_metrics():
    """Restore _live_metrics from disk. Recompute counts from r_history."""
    if not os.path.exists(_METRICS_FILE):
        return
    try:
        with open(_METRICS_FILE) as f:
            snap = json.load(f)
    except Exception as e:
        logging.warning(f"Failed to read metrics file: {e}")
        return
    try:
        r_hist = snap.get('r_history', [])
        pnl_hist = snap.get('pnl_history', [])
        by_dir = snap.get('by_direction', {})
        tlog = snap.get('trade_log', [])
        if not isinstance(r_hist, list):
            r_hist = []
        if not isinstance(pnl_hist, list):
            pnl_hist = []
        if not isinstance(tlog, list):
            tlog = []
        wins = sum(1 for r in r_hist if r > 0)
        losses = len(r_hist) - wins
        _live_metrics['r_history'] = r_hist
        _live_metrics['pnl_history'] = pnl_hist
        _live_metrics['trades'] = len(r_hist)
        _live_metrics['wins'] = wins
        _live_metrics['losses'] = losses
        _live_metrics['total_r'] = sum(r_hist)
        _live_metrics['trade_log'] = tlog
        # Recompute by_direction from trade_log when available
        if tlog and len(tlog) >= len(r_hist):
            long_t = sum(1 for e in tlog if isinstance(e, dict) and e.get('dir', '') == 'L')
            short_t = len(tlog) - long_t
            long_w = sum(1 for e in tlog if isinstance(e, dict) and e.get('dir', '') == 'L' and e.get('r', 0) > 0)
            short_w = sum(1 for e in tlog if isinstance(e, dict) and e.get('dir', '') == 'S' and e.get('r', 0) > 0)
            _live_metrics['by_direction']['long'] = {'trades': long_t, 'wins': long_w}
            _live_metrics['by_direction']['short'] = {'trades': short_t, 'wins': short_w}
        elif isinstance(by_dir, dict):
            for d in ('long', 'short'):
                if d in by_dir and isinstance(by_dir[d], dict):
                    _live_metrics['by_direction'][d] = by_dir[d]
        total_pnl = sum(pnl_hist) if pnl_hist else 0
        global _PAPER_BALANCE
        saved_bal = snap.get('paper_balance')
        if saved_bal is not None:
            with _PAPER_BAL_LOCK:
                _PAPER_BALANCE = Decimal(str(saved_bal))
            logging.info(f"Restored paper balance: ${float(_PAPER_BALANCE):,.2f}")
        elif pnl_hist:
            with _PAPER_BAL_LOCK:
                _PAPER_BALANCE = _PAPER_START_BAL + Decimal(str(round(total_pnl, 8)))
            logging.info(f"Derived paper balance from PnL history: ${float(_PAPER_BALANCE):,.2f}")
        _session_baseline['trades'] = _live_metrics['trades']
        _session_baseline['wins'] = _live_metrics['wins']
        _session_baseline['losses'] = _live_metrics['losses']
        _session_baseline['total_r'] = _live_metrics['total_r']
        _session_baseline['pnl'] = total_pnl
        logging.info(f"Loaded metrics: {len(r_hist)} trades, {len(tlog)} in trade_log, P/L ${total_pnl:+,.2f}")
    except Exception as e:
        logging.warning(f"Failed to load metrics: {e}", exc_info=True)
_benchmark = {'win_rate': 0.45, 'avg_r': 0.8}
_deviation_alerts_sent = {'win_rate': 0, 'drawdown': 0}

# Circuit breaker: daily/weekly loss limits + drawdown-based position scaling
_circuit_breaker = {'until': 0, 'daily_pnl': 0.0, 'weekly_pnl': 0.0,
                    'day_reset': time.time(), 'week_reset': time.time()}
_circuit_breaker_tripped = False
_cb_lock = Lock()

_RGM_MULT = {
    'bull':  {'long': 1.0, 'short': 0.3},
    'bear':  {'long': 0.3, 'short': 1.0},
    'range': {'long': 0.5, 'short': 0.5},
    'chop':  {'long': 0.2, 'short': 0.2},
}

_RGM_CONF_MIN = {
    'bull':  0.40,   # trending — standard bar
    'bear':  0.55,   # counter-trend needs stronger conviction
    'range': 0.55,   # demand clear structure/momentum
    'chop':  0.65,   # only very strong setups survive chop
}
# Short mode: learning ramp from relaxed to strict thresholds
_RGM_CONF_MIN_SHORT_STRICT = {'bull': 0.55, 'bear': 0.40, 'range': 0.55, 'chop': 0.65}
_RGM_CONF_MIN_SHORT_LEARN  = {'bull': 0.35, 'bear': 0.25, 'range': 0.35, 'chop': 0.40}
_CONF_LEARN_TRADES = 30

def _get_conf_min(trade_count=0):
    """Get regime confidence minimums — short mode ramps from learn to strict."""
    if _MODE == 'long':
        return _RGM_CONF_MIN
    if trade_count >= _CONF_LEARN_TRADES:
        return _RGM_CONF_MIN_SHORT_STRICT
    t = trade_count / _CONF_LEARN_TRADES
    return {r: round(_RGM_CONF_MIN_SHORT_LEARN[r] + t * (_RGM_CONF_MIN_SHORT_STRICT[r] - _RGM_CONF_MIN_SHORT_LEARN[r]), 4)
            for r in _RGM_CONF_MIN_SHORT_STRICT}

def update_circuit_breaker(pnl: float):
    global _circuit_breaker, _circuit_breaker_tripped
    with _cb_lock:
        now = time.time()
        if now - _circuit_breaker['day_reset'] > 86400:
            _circuit_breaker['daily_pnl'] = 0.0
            _circuit_breaker['day_reset'] = now
            _circuit_breaker_tripped = False
        if now - _circuit_breaker['week_reset'] > 604800:
            _circuit_breaker['weekly_pnl'] = 0.0
            _circuit_breaker['week_reset'] = now
            _circuit_breaker_tripped = False
        _circuit_breaker['daily_pnl'] += pnl
        _circuit_breaker['weekly_pnl'] += pnl
        if _circuit_breaker['daily_pnl'] < -15:
            _circuit_breaker_tripped = True
            logging.warning(f"Circuit breaker TRIPPED: daily loss ${_circuit_breaker['daily_pnl']:.0f} — entries halted")
        if _circuit_breaker['weekly_pnl'] < -35:
            _circuit_breaker_tripped = True
            logging.warning(f"Circuit breaker TRIPPED: weekly loss ${_circuit_breaker['weekly_pnl']:.0f} — entries halted")
    # Update paper balance
    if _PAPER():
        with _PAPER_BAL_LOCK:
            global _PAPER_BALANCE
            _PAPER_BALANCE += Decimal(str(pnl))

def get_drawdown_mult():
    """Drawdown tier scaling: 0-5%=1x, 5-10%=0.75x, 10-15%=0.5x, 15%+=0x.
    Paper: drawdown from _PAPER_START_BAL. Live: drawdown from running peak _LIVE_PEAK."""
    if _PAPER():
        with _PAPER_BAL_LOCK:
            dd_pct = max(0, float(_PAPER_START_BAL - _PAPER_BALANCE) / float(_PAPER_START_BAL))
    else:
        with _LIVE_BAL_LOCK:
            peak = float(_LIVE_PEAK)
            cur = float(_LIVE_BALANCE)
        if peak <= 0:
            return 1.0  # no peak yet — full size
        dd_pct = max(0, (peak - cur) / peak)
    if dd_pct < 0.05: return 1.0
    if dd_pct < 0.10: return 0.75
    if dd_pct < 0.15: return 0.5
    return 0.0

def _deduct_paper_balance(amt):
    pass  # equity-tracked, not cash-tracked

def _sync_live_balance(ex, force=False):
    """Fetch live balance from Kraken. Caches in _LIVE_BALANCE, persists to disk.
    On failure, returns cached balance instead of 0 to avoid losing tracked balance."""
    global _LIVE_BALANCE, _LIVE_BAL_LAST_SYNC, _LIVE_PEAK
    with _LIVE_BAL_LOCK:
        if not force and (time.time() - _LIVE_BAL_LAST_SYNC) < 60:
            return _LIVE_BALANCE
    try:
        bal = ex.fetch_balance()
        # Kraken uses ZUSD internally for USD
        usd = Decimal(str(bal.get('USD', {}).get('free', 0) or 0))
        if usd <= 0:
            usd = Decimal(str(bal.get('ZUSD', {}).get('free', 0) or 0))
        with _LIVE_BAL_LOCK:
            old_bal = _LIVE_BALANCE
            if usd > 0:
                _LIVE_BALANCE = usd
                if usd > _LIVE_PEAK:
                    _LIVE_PEAK = usd  # high-water mark for drawdown computation
                _LIVE_BAL_LAST_SYNC = time.time()
            else:
                # Balance is 0 - keep old value to avoid losing track
                usd = _LIVE_BALANCE if _LIVE_BALANCE > 0 else Decimal('1')
                _LIVE_BALANCE = usd  # persist fallback in global state
        # Clear RECON funds-fail flags when balance increases (new funds available for SL/TP orders)
        if usd > old_bal and 'sts' in globals() and sts:
            for st in sts.values():
                with st._lock:
                    if st.pos and (st.pos.get('_sl_funds_fail') or st.pos.get('_tp_funds_fail')):
                        st.pos.pop('_sl_funds_fail', None)
                        st.pos.pop('_tp_funds_fail', None)
                        st._save_position()
        logging.info(f"Kraken balance synced: ${float(usd):,.2f} available")
        _save_live_balance()  # Persist balance to disk for crash recovery
        return usd
    except Exception as e:
        logging.warning(f"Kraken balance sync failed: {e}")
        with _LIVE_BAL_LOCK:
            return _LIVE_BALANCE

def _get_trade_amt():
    """Return the base trade amount for the current mode.
    Paper: $500 flat.  Live: 3% of Kraken balance (min $5, to respect Kraken order minimums)."""
    if _PAPER():
        return _TRADE_AMT
    with _LIVE_BAL_LOCK:
        bal = _LIVE_BALANCE
    if bal <= 1:  # If balance is $0 or $1, use minimum
        return Decimal('5')
    # 3% of balance per trade — small account, conservative sizing
    amt = bal * Decimal('0.03')
    return max(amt, Decimal('5'))


def _save_live_balance():
    """Persist live balance to disk for crash recovery."""
    try:
        with open(os.path.join(_OUTPUT_DIR, 'live_balance.json'), 'w') as f:
            json.dump({'balance': float(_LIVE_BALANCE), 'timestamp': time.time()}, f)
    except Exception as e:
        logging.debug(f"Could not save live balance: {e}")


def _load_live_balance():
    """Load persisted live balance from disk."""
    try:
        path = os.path.join(_OUTPUT_DIR, 'live_balance.json')
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
                bal = Decimal(str(data.get('balance', 0)))
                logging.info(f"Loaded persisted balance: ${float(bal):,.2f}")
                return bal
    except Exception as e:
        logging.debug(f"Could not load live balance: {e}")
    return Decimal('0')


# =============================================================================
# Equity time-series logger — powers the dashboard equity curve
# =============================================================================
_EQUITY_FILE = None          # lazy-set in _equity_logger_loop (depends on _OUTPUT_DIR at runtime)
_EQUITY_MAX_ENTRIES = 8640   # 6 days at 1/min
_equity_series = []          # in-memory rolling window
_equity_lock = Lock()
_equity_hwm = 0.0            # running high-water mark (bal + upnl)

def _load_equity_series():
    """Restore equity curve from disk on startup. Also rehydrates the HWM."""
    global _equity_series, _equity_hwm, _EQUITY_FILE
    _EQUITY_FILE = os.path.join(_OUTPUT_DIR, 'equity_curve.json')
    try:
        if os.path.exists(_EQUITY_FILE):
            with open(_EQUITY_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                with _equity_lock:
                    _equity_series = data[-_EQUITY_MAX_ENTRIES:]
                    _equity_hwm = max((e.get('hwm', 0.0) for e in _equity_series), default=0.0)
                logging.info(f"Loaded equity curve: {len(_equity_series)} points, HWM ${_equity_hwm:,.2f}")
    except Exception as e:
        logging.warning(f"Failed to load equity_curve.json: {e}")

def _save_equity_series():
    """Atomic write of the in-memory series to disk. Caller must hold _equity_lock."""
    if not _EQUITY_FILE:
        return
    try:
        tmp = _EQUITY_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(_equity_series, f)
        os.replace(tmp, _EQUITY_FILE)
    except Exception as e:
        logging.warning(f"Failed to save equity_curve.json: {e}")

def _compute_upnl():
    """Sum of unrealized P/L across all open positions. Returns float."""
    if 'sts' not in globals() or not sts:
        return 0.0
    total = 0.0
    for st in sts.values():
        try:
            pos = st.get_position()
            if not pos:
                continue
            px, _ = st.get_price()
            if px <= 0:
                continue
            entry = float(pos.get('e', 0) or 0)
            size = float(pos.get('size', 0) or 0)
            if entry <= 0 or size <= 0:
                continue
            direction = pos.get('direction', 'long')
            sign = 1.0 if direction == 'long' else -1.0
            # Gross unrealized P/L (fees not deducted — those hit on exit)
            total += (float(px) - entry) * size * sign
        except Exception:
            continue
    return total

def _equity_logger_loop(interval=60):
    """Daemon: append a balance snapshot every `interval` seconds.
    Sleeps `interval` seconds before the first write so startup noise settles."""
    global _equity_series, _equity_hwm
    _load_equity_series()
    time.sleep(interval)
    while True:
        try:
            if _PAPER():
                with _PAPER_BAL_LOCK:
                    bal = float(_PAPER_BALANCE)
            else:
                with _LIVE_BAL_LOCK:
                    bal = float(_LIVE_BALANCE)
            upnl = _compute_upnl()
            pos_count = count_open_positions(sts) if 'sts' in globals() and sts else 0
            with _metrics_lock:
                spnl = float(sum(_live_metrics.get('pnl_history', [])[_session_baseline.get('trades', 0):]) or 0.0)
            equity_now = bal + upnl
            if equity_now > _equity_hwm:
                _equity_hwm = equity_now
            snap = {
                'ts': round(time.time(), 3),
                'bal': round(bal, 4),
                'upnl': round(upnl, 4),
                'pos': pos_count,
                'spnl': round(spnl, 4),
                'hwm': round(_equity_hwm, 4),
            }
            with _equity_lock:
                _equity_series.append(snap)
                if len(_equity_series) > _EQUITY_MAX_ENTRIES:
                    _equity_series = _equity_series[-_EQUITY_MAX_ENTRIES:]
                _save_equity_series()
        except Exception as e:
            logging.warning(f"equity logger loop error: {e}")
        time.sleep(interval)

def start_equity_logger():
    _t = threading.Thread(target=_equity_logger_loop, args=(60,), daemon=True)
    _t.start()

# =============================================================================
# Trade marker logger — persistent list of every trade open/close
# =============================================================================
_TRADE_MARKERS_FILE = None
_trade_markers = []
_trade_markers_lock = Lock()

def _load_trade_markers():
    global _trade_markers, _TRADE_MARKERS_FILE
    _TRADE_MARKERS_FILE = os.path.join(_OUTPUT_DIR, 'trade_markers.json')
    try:
        if os.path.exists(_TRADE_MARKERS_FILE):
            with open(_TRADE_MARKERS_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                with _trade_markers_lock:
                    _trade_markers = data
                logging.info(f"Loaded trade markers: {len(_trade_markers)} entries")
    except Exception as e:
        logging.warning(f"Failed to load trade_markers.json: {e}")

def _save_trade_markers_unlocked():
    """Caller must hold _trade_markers_lock."""
    if not _TRADE_MARKERS_FILE:
        return
    try:
        tmp = _TRADE_MARKERS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(_trade_markers, f)
        os.replace(tmp, _TRADE_MARKERS_FILE)
    except Exception as e:
        logging.warning(f"Failed to save trade_markers.json: {e}")

def _log_trade_marker(marker_type, sym, direction, price, r=None, pnl=None):
    """Append a trade open/close marker. Safe to call from any thread.
    marker_type: 'open' or 'close'.  r/pnl are None for opens."""
    try:
        if _PAPER():
            with _PAPER_BAL_LOCK:
                bal_after = float(_PAPER_BALANCE)
        else:
            with _LIVE_BAL_LOCK:
                bal_after = float(_LIVE_BALANCE)
        marker = {
            'ts': round(time.time(), 3),
            'type': marker_type,
            'sym': sym,
            'dir': direction.upper() if direction else '',
            'price': round(float(price), 8) if price is not None else None,
            'r': round(float(r), 4) if r is not None else None,
            'pnl': round(float(pnl), 4) if pnl is not None else None,
            'bal_after': round(bal_after, 4),
        }
        with _trade_markers_lock:
            _trade_markers.append(marker)
            _save_trade_markers_unlocked()
    except Exception as e:
        logging.warning(f"_log_trade_marker failed: {e}")


# =============================================================================
# Gate funnel snapshot — derived from _decision_log, no trading-path touches.
# Dashboard polls /api/funnel; this function computes the response on demand.
# =============================================================================
# Canonical gate ordering (top-to-bottom of the funnel).
# Must match the gate names emitted by the Phase 2 instrumentation in stream().
_FUNNEL_GATE_ORDER = [
    'correlation',
    'factor_floors',
    'confluence',
    'dir_wr',
    'regime_mult',
    'whale',
    'ai_conf',
    'max_positions',
    'drawdown',
    'sentiment',
    'atr_fees',
    'min_size',
    'notional',
    'duplicate',
]

def _compute_funnel_snapshot(window_seconds=60):
    """Tally gate passes/fails from _decision_log entries in the last `window_seconds`.

    Returns a dict with:
      window_seconds: int
      records: total decision records in window
      entered: count where 'entry' is in gates_passed
      gates: ordered list of {name, reached, passed, failed}

    Upstream counters (total_signals, passed_len2) are NOT available via this
    derivation path — decision records are only created for entries that pass
    `len(sg) >= 2` at goldeneye.py:3783. Treat this as the "from correlation
    onward" funnel — the dispatch-named upstream counters are omitted.
    """
    cutoff = time.time() - window_seconds
    with _decision_log_lock:
        records = [r for r in _decision_log if r.get('timestamp', 0) >= cutoff]
    total = len(records)

    # Bucket: name -> {'passed': n, 'failed': n}
    buckets = {g: {'passed': 0, 'failed': 0} for g in _FUNNEL_GATE_ORDER}

    def _norm(tag):
        # tag is e.g. "whale:57.9" or "regime_mult:0.50" or "correlation"
        return tag.split(':', 1)[0] if isinstance(tag, str) else ''

    entered_count = 0
    for r in records:
        gp = r.get('gates_passed') or []
        gf = r.get('gates_failed') or []
        if 'entry' in gp:
            entered_count += 1
        for tag in gp:
            name = _norm(tag)
            if name in buckets:
                buckets[name]['passed'] += 1
        for tag in gf:
            name = _norm(tag)
            if name in buckets:
                buckets[name]['failed'] += 1

    # "reached" = passed + failed for each gate (how many records evaluated it)
    gates = []
    for name in _FUNNEL_GATE_ORDER:
        p = buckets[name]['passed']
        f = buckets[name]['failed']
        reached = p + f
        gates.append({
            'name': name,
            'reached': reached,
            'passed': p,
            'failed': f,
        })

    return {
        'window_seconds': window_seconds,
        'records': total,
        'entered': entered_count,
        'gates': gates,
    }


def get_portfolio_heat(sts):
    """Sum of (position_value * stop_distance_pct) across all open positions."""
    heat = Decimal('0')
    for st in sts.values():
        pos = st.get_position()
        if pos and pos.get('e') and pos.get('sl'):
            e, sl = pos['e'], pos['sl']
            size = Decimal(str(pos.get('filled') or pos.get('size') or 0))
            heat += abs(e - sl) * size
    return heat

def count_open_positions(sts):
    return sum(1 for st in sts.values() if st.get_position() is not None)

def _force_reconcile_exit(sym, st, br, exit_price, exit_type='RECON'):
    """Force-close a local position during reconciliation. Mirrors stream() exit path."""
    pos = st.get_position()
    if not pos:
        return
    direction = pos.get('direction', _MODE)
    filled_size = Decimal(str(pos.get('filled', pos.get('size', 0))))
    fee = pos.get('fee', get_fee(sym))
    risk_dist = abs(float(pos['e']) - float(pos.get('orig_sl', pos['sl'])))
    r = ((float(exit_price) - float(pos['e'])) / risk_dist if direction == 'long'
         else (float(pos['e']) - float(exit_price)) / risk_dist) if risk_dist > 0 else 0.0
    if direction == 'long':
        pnl = (exit_price - pos['e']) * filled_size - (exit_price * fee * filled_size + pos['e'] * fee * filled_size)
    else:
        pnl = (pos['e'] - exit_price) * filled_size - (exit_price * fee * filled_size + pos['e'] * fee * filled_size)
    update_circuit_breaker(float(pnl))
    tp_hit = pos.get('tp_hit', 0)
    elapsed_h = (time.time() - pos.get('opened_at', time.time())) / 3600
    with _sym_cooldown_lock:
        _sym_cooldown[sym] = time.time()
    pos_snap = dict(pos)
    st.clear_position()
    logging.info(f"{sym} RECON EXIT {exit_type} {r:+.1f}R @ {float(exit_price):,.2f}, PnL={float(pnl):.2f}")
    try:
        br.record(pos_snap['sg'], r, pos_snap.get('slippage', Decimal('0')), direction,
                  factors=pos_snap.get('factors'), rgm=pos_snap.get('regime'))
        _td = _build_trade_detail(pos_snap, float(exit_price), tp_hit=tp_hit)
        record_live_trade(direction, float(r), float(pnl), sym=sym, exit_type=exit_type, detail=_td)
        _log_factor_trade(sym, pos_snap, r, float(pnl))
    except Exception as e:
        logging.warning(f"{sym} recon post-trade error: {e}")

def reconcile_positions(ex, sts, brs):
    """Cross-check local positions against exchange state. Run at startup + periodically."""
    if _is_paper():
        return  # paper mode — no exchange state to reconcile
    for sym, st in list(sts.items()):
        pos = st.get_position()
        if not pos:
            continue
        # Skip paper positions left over from a mode switch — they have no exchange state
        entry_id = pos.get('order_id', '')
        if entry_id.startswith('paper_'):
            logging.warning(f"{sym} RECON: clearing stale paper position (mode switched to live)")
            st.clear_position()
            continue
        direction = pos.get('direction', _MODE)
        sl_id = pos.get('sl_order_id')
        tp_id = pos.get('tp_order_id')
        filled_size = Decimal(str(pos.get('filled', pos.get('size', 0))))
        symbol = pos.get('symbol', sym)
        try:
            # Check if SL/TP orders already filled on exchange
            sl_gone = False
            if sl_id:
                try:
                    sl_ord = ex.fetch_order(sl_id, symbol)
                    if sl_ord.get('status') in ('closed', 'filled'):
                        sl_gone = True
                        logging.info(f"{sym} RECON: SL {sl_id} filled on exchange")
                except ccxt.OrderNotFound:
                    sl_gone = True
                    logging.info(f"{sym} RECON: SL {sl_id} not found on exchange")
            tp_gone = False
            if tp_id:
                try:
                    tp_ord = ex.fetch_order(tp_id, symbol)
                    if tp_ord.get('status') in ('closed', 'filled'):
                        tp_gone = True
                        logging.info(f"{sym} RECON: TP {tp_id} filled on exchange")
                except ccxt.OrderNotFound:
                    tp_gone = True
                    logging.info(f"{sym} RECON: TP {tp_id} not found on exchange")
            # If protective order filled/gone → force local exit at current price
            if sl_gone or tp_gone:
                cur_px = Decimal(str(ex.fetch_ticker(symbol)['last']))
                _force_reconcile_exit(sym, st, brs[sym], cur_px, 'SL_FILL' if sl_gone else 'TP_FILL')
                continue
            # Re-create missing protective orders (skip if prior attempt got Insufficient funds)
            exit_side = 'sell' if direction == 'long' else 'buy'
            if not sl_id and pos.get('sl') and not pos.get('_sl_funds_fail'):
                logging.warning(f"{sym} RECON: missing SL order, creating")
                try:
                    sl_order = ex.create_order(symbol, 'stop-loss', exit_side, float(filled_size), params={'price': float(ex.price_to_precision(symbol, float(pos['sl'])))})
                    with st._lock:
                        if st.pos:
                            st.pos['sl_order_id'] = sl_order.get('id')
                            st._save_position()
                except Exception as e:
                    logging.error(f"{sym} RECON: SL creation failed: {e}")
                    if 'Insufficient funds' in str(e):
                        with st._lock:
                            if st.pos:
                                st.pos['_sl_funds_fail'] = True
                                st._save_position()
                        logging.info(f"{sym} RECON: SL retry suppressed until balance changes (software SL active)")
            if not tp_id and pos.get('tp1') and not pos.get('_tp_funds_fail'):
                logging.warning(f"{sym} RECON: missing TP order, creating")
                try:
                    tp_order = ex.create_order(symbol, 'limit', exit_side, float(filled_size), price=float(pos['tp1']))
                    with st._lock:
                        if st.pos:
                            st.pos['tp_order_id'] = tp_order.get('id')
                            st._save_position()
                except Exception as e:
                    logging.error(f"{sym} RECON: TP creation failed: {e}")
                    if 'Insufficient funds' in str(e):
                        with st._lock:
                            if st.pos:
                                st.pos['_tp_funds_fail'] = True
                                st._save_position()
                        logging.info(f"{sym} RECON: TP retry suppressed until balance changes (software TP active)")
        except Exception as e:
            logging.error(f"{sym} RECON failed: {e}")
            # Never clear on transient errors — fail safe

def _build_trade_detail(pos, exit_price, tp_hit=0):
    """Build rich detail dict from position snapshot for closed trade log."""
    factors = pos.get('factors', {})
    opened = pos.get('opened_at', time.time())
    return {
        'entry': round(float(pos.get('e', 0)), 6),
        'exit_price': round(float(exit_price), 6),
        'sl': round(float(pos.get('orig_sl', pos.get('sl', 0))), 6),
        'tp1': round(float(pos.get('tp1', 0)), 6),
        'tp2': round(float(pos.get('tp2', 0)), 6),
        'tp3': round(float(pos.get('tp3', 0)), 6),
        'tp_hit': tp_hit,
        'sigs': pos.get('sg', []),
        'confidence': round(float(pos.get('confidence', 0)), 4),
        'regime': pos.get('regime', 'chop'),
        'factors': {k: round(float(v), 4) for k, v in factors.items()} if factors else {},
        'size': round(float(pos.get('filled', pos.get('size', 0))), 6),
        'duration_h': round((time.time() - opened) / 3600, 2),
        'opened_at': opened,
    }

def record_live_trade(direction: str, r_multiple: float, pnl: float = 0.0, sym: str = '', exit_type: str = '', detail: dict = None):
    with _metrics_lock:
        _live_metrics['trades'] += 1
        _live_metrics['total_r'] += r_multiple
        _live_metrics['r_history'].append(r_multiple)
        _live_metrics['pnl_history'].append(pnl)
        # Cap history lists to prevent unbounded memory growth
        if len(_live_metrics['r_history']) > 2000:
            _live_metrics['r_history'] = _live_metrics['r_history'][-2000:]
        if len(_live_metrics['pnl_history']) > 2000:
            _live_metrics['pnl_history'] = _live_metrics['pnl_history'][-2000:]
        _live_metrics['by_direction'][direction]['trades'] += 1
        if r_multiple > 0:
            _live_metrics['wins'] += 1
            _live_metrics['by_direction'][direction]['wins'] += 1
        else:
            _live_metrics['losses'] += 1
        entry = {
            'sym': sym, 'dir': direction[0].upper(), 'exit': exit_type,
            'r': round(r_multiple, 2), 'pnl': round(pnl, 2), 't': time.time()
        }
        if detail:
            entry['detail'] = detail
        _live_metrics['trade_log'].append(entry)
        if len(_live_metrics['trade_log']) > 2000:
            _live_metrics['trade_log'] = _live_metrics['trade_log'][-2000:]
        _save_metrics()
    # Expectancy tracker — outside _metrics_lock to avoid holding it longer
    if _expectancy and detail:
        try:
            _expectancy.record_trade(
                bot_id='goldeneye',
                pair=sym,
                direction=direction.upper(),
                entry_price=detail.get('entry', 0),
                exit_price=detail.get('exit_price', 0),
                size_usd=detail.get('size', 0) * detail.get('entry', 0),
                duration=detail.get('duration_h', 0) * 3600,
            )
        except Exception as _exp_err:
            logging.warning(f"{sym} expectancy.record_trade failed: {_exp_err}")

def get_live_metrics() -> dict:
    with _metrics_lock:
        t = _live_metrics['trades']
        w = _live_metrics['wins']
        losses = _live_metrics['losses']
        total_r = _live_metrics['total_r']
        start = _live_metrics['start_time']
        lw = _live_metrics['by_direction']['long']['wins']
        lt = _live_metrics['by_direction']['long']['trades']
        sw = _live_metrics['by_direction']['short']['wins']
        st2 = _live_metrics['by_direction']['short']['trades']
        r_hist = list(_live_metrics['r_history'])
        pnl_hist = list(_live_metrics['pnl_history'])
        tlog = list(_live_metrics['trade_log'][-50:])

    win_rate = w / t if t > 0 else 0
    avg_r = total_r / t if t > 0 else 0

    # Cumulative P/L from actual dollar PnL per trade
    total_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnl_hist:
        total_pnl += p
        if total_pnl > peak: peak = total_pnl
        dd = peak - total_pnl
        if dd > max_dd: max_dd = dd
    max_dd_pct = (max_dd / float(_PAPER_START_BAL)) * 100.0 if max_dd > 0 else 0  # DD as % of starting balance

    # Session stats (this run only)
    s_trades = t - _session_baseline['trades']
    s_wins = w - _session_baseline['wins']
    s_losses = losses - _session_baseline['losses']
    s_r = total_r - _session_baseline['total_r']
    s_pnl = total_pnl - _session_baseline['pnl']
    s_wr = s_wins / s_trades if s_trades > 0 else 0
    s_avg_r = s_r / s_trades if s_trades > 0 else 0

    result = {
        # Lifetime (all trades ever)
        'trades': t, 'wins': w, 'losses': losses,
        'win_rate': win_rate, 'avg_r': avg_r, 'max_drawdown': max_dd_pct,
        'total_pnl': total_pnl,
        'uptime_hours': (time.time() - start) / 3600,
        'long_wins': lw, 'long_trades': lt,
        'short_wins': sw, 'short_trades': st2,
        'trade_log': tlog,
        # Session (this run only)
        'session_trades': s_trades, 'session_wins': s_wins, 'session_losses': s_losses,
        'session_wr': s_wr, 'session_avg_r': s_avg_r, 'session_pnl': s_pnl,
    }
    if _expectancy:
        try:
            result['expectancy'] = _expectancy.bot_snapshot_fields('goldeneye')
        except Exception as _exp_snap_err:
            logging.warning(f"expectancy.bot_snapshot_fields failed: {_exp_snap_err}")
    return result

def check_deviation_alerts():
    global _deviation_alerts_sent, _benchmark
    metrics = get_live_metrics()

    if metrics['trades'] < 10:
        return

    wr_deviation = abs(metrics['win_rate'] - _benchmark['win_rate']) / _benchmark['win_rate'] if _benchmark['win_rate'] > 0 else 0
    if wr_deviation > 0.25 and _deviation_alerts_sent['win_rate'] < 3:
        msg = f"ALERT: Live win rate {metrics['win_rate']*100:.1f}% deviates {wr_deviation*100:.0f}% from benchmark {_benchmark['win_rate']*100:.1f}%"
        logging.warning(msg)
        send_alert(msg)
        _deviation_alerts_sent['win_rate'] += 1

    if metrics['avg_r'] < _benchmark['avg_r'] * 0.5 and _deviation_alerts_sent['drawdown'] < 3:
        msg = f"ALERT: Live avg R {metrics['avg_r']:.2f} below 50% of benchmark {_benchmark['avg_r']:.2f}"
        logging.warning(msg)
        send_alert(msg)
        _deviation_alerts_sent['drawdown'] += 1

def get_volatility_SL_TP(df, base_sl_pct, base_tp_pct, multiplier=1.2):
    if df is None or len(df) < 20 or 'atr_pct' not in df.columns:
        return base_sl_pct, base_tp_pct

    atr_pct = df['atr_pct'].iloc[-1]
    if pd.isna(atr_pct) or atr_pct <= 0:
        return base_sl_pct, base_tp_pct

    vol_sl = Decimal(str(atr_pct * multiplier))
    vol_tp = vol_sl  # TP base = SL distance (1R), multiplied by 1x/2x/3x/5x at entry

    min_sl = Decimal('0.005')
    max_sl = Decimal('0.05')   # cap SL at 5% even for volatile assets
    min_tp = Decimal('0.005')

    adjusted_sl = min(max(vol_sl, min_sl), max_sl)
    adjusted_tp = min(max(vol_tp, min_tp), max_sl)  # TP base capped same as SL

    return adjusted_sl, adjusted_tp

_correlation_data = {}
_correlation_lock = Lock()
_correlation_window = 50
_max_correlation = 0.5

# Per-symbol cooldown after exit (prevents rapid re-entry churn)
_sym_cooldown = {}  # {symbol: timestamp_of_last_exit}
_sym_cooldown_lock = Lock()
_COOLDOWN_SECS = 1800  # 30 minutes

# Per-symbol backoff after portfolio denial (prevents hot retry loops)
_denial_backoff = {}  # {symbol: {"until": float, "delay": float}}
_denial_backoff_lock = Lock()
_DENIAL_BASE_DELAY = 5.0     # start at 5 seconds
_DENIAL_MAX_DELAY = 300.0    # cap at 5 minutes

# UPGRADE G: Multiple concurrent shadow positions per symbol
# {symbol: [{"signals": [...], "entry_price": float, "sl": float, "tp1": float,
#             "direction": str, "factors": dict, "regime": str, "timestamp": float,
#             "block_reason": str}, ...]}
_shadow_positions = {}  # list of shadows per symbol (was: one per symbol)
_shadow_lock = Lock()
_SHADOW_EXPIRY = 86400  # 24 hours
_MAX_SHADOWS_PER_SYM = 5  # UPGRADE G: allow up to 5 concurrent shadows per symbol

# Decision traceability log — records all entry decisions with full factor breakdown
_decision_log = []  # list of decision dicts
_decision_log_lock = Lock()
_MAX_DECISION_LOG = 1000  # keep last 1000 decisions

# Per-symbol entry lock (prevents duplicate positions from watchdog-restarted threads)
_sym_entry_locks = {}
_sym_locks_lock = Lock()

# Gate-blocked log throttle — one message per (sym, gate) per 5 minutes
_gate_block_last_log = {}  # {(sym, gate_name): timestamp}

def _get_sym_lock(sym):
    with _sym_locks_lock:
        if sym not in _sym_entry_locks:
            _sym_entry_locks[sym] = Lock()
        return _sym_entry_locks[sym]

def update_price_history(symbol: str, price: float):
    with _correlation_lock:
        if symbol not in _correlation_data:
            _correlation_data[symbol] = []
        _correlation_data[symbol].append(price)
        if len(_correlation_data[symbol]) > _correlation_window:
            _correlation_data[symbol] = _correlation_data[symbol][-_correlation_window:]

def calculate_correlation(sym1: str, sym2: str) -> float:
    with _correlation_lock:
        if sym1 not in _correlation_data or sym2 not in _correlation_data:
            return 0.0
        p1 = list(_correlation_data[sym1])
        p2 = list(_correlation_data[sym2])

    if len(p1) < 10 or len(p2) < 10:
        return 0.0

    min_len = min(len(p1), len(p2))
    p1 = p1[-min_len:]
    p2 = p2[-min_len:]

    # Use returns (pct change) not raw prices — raw prices always correlate in trending markets
    r1 = [(p1[i] - p1[i-1]) / p1[i-1] if p1[i-1] != 0 else 0 for i in range(1, len(p1))]
    r2 = [(p2[i] - p2[i-1]) / p2[i-1] if p2[i-1] != 0 else 0 for i in range(1, len(p2))]
    if len(r1) < 5:
        return 0.0

    mean1 = sum(r1) / len(r1)
    mean2 = sum(r2) / len(r2)

    num = sum((r1[i] - mean1) * (r2[i] - mean2) for i in range(len(r1)))
    den1 = sum((r - mean1) ** 2 for r in r1) ** 0.5
    den2 = sum((r - mean2) ** 2 for r in r2) ** 0.5

    if den1 == 0 or den2 == 0:
        return 0.0

    return num / (den1 * den2)

def check_correlation_risk(symbol: str, open_positions: list) -> tuple:
    """Returns (allowed: bool, max_corr: float)."""
    if not open_positions:
        return True, 0.0  # No positions = no correlation risk
    global _max_correlation
    _CORR_HARD_CEIL = 0.95
    max_corr = 0.0
    for pos_sym in open_positions:
        corr = abs(calculate_correlation(symbol, pos_sym))
        if corr > max_corr:
            max_corr = corr
    if max_corr > _CORR_HARD_CEIL:
        logging.debug(f"Correlation hard block: {symbol} corr={max_corr:.2f} (>{_CORR_HARD_CEIL})")
        return False, max_corr
    if max_corr > _max_correlation:
        logging.debug(f"Correlation risk: {symbol} corr={max_corr:.2f} (max: {_max_correlation})")
        return False, max_corr
    return True, max_corr

def submit_order_with_retry(ex, method: str, symbol: str, amount: float, retries=3, base_delay=1.0):
    import ccxt
    for attempt in range(retries):
        try:
            func = getattr(ex, method)
            order = func(symbol, amount)
            return order
        except ccxt.RateLimitExceeded:
            if attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                logging.warning(f"Rate limited, retrying in {delay}s (attempt {attempt+1}/{retries})")
                time.sleep(delay)
            else:
                raise
        except ccxt.InsufficientFunds as e:
            logging.error(f"Insufficient funds: {e}")
            raise
        except ccxt.NetworkError as e:
            if attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                logging.warning(f"Network error, retrying in {delay}s: {e}")
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            logging.error(f"Order failed: {e}")
            raise
    return None


# Audit logging
_audit_logger = None

def get_audit_logger():
    global _audit_logger
    if _audit_logger is None:
        import logging.handlers
        _audit_logger = logging.getLogger('goldeneye_audit')
        _audit_logger.setLevel(logging.INFO)
        fh = logging.handlers.RotatingFileHandler(os.path.join(_BOT_DIR, 'goldeneye_audit.log'), maxBytes=10*1024*1024, backupCount=5)
        fh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        _audit_logger.addHandler(fh)
    return _audit_logger

def log_fill(sym, side, order_id, filled, price, fee, pnl=None, signals=None):
    audit = get_audit_logger()
    utc_ts = datetime.now(timezone.utc).isoformat() + "Z"
    msg = f"{utc_ts} {sym} {side.upper()} {order_id} filled={filled} price={price} fee={fee}"
    if pnl is not None:
        msg += f" pnl={pnl}"
    if signals:
        msg += f" signals={signals}"
    audit.info(msg)

# Factor audit logger — one JSON line per closed trade for statistical validation
import logging.handlers
_factor_log = logging.getLogger('factor_audit')
_factor_fh = logging.handlers.RotatingFileHandler(os.path.join(_BOT_DIR, 'goldeneye_factors.log'), maxBytes=10*1024*1024, backupCount=3)
_factor_fh.setFormatter(logging.Formatter('%(message)s'))
_factor_log.addHandler(_factor_fh)
_factor_log.setLevel(logging.INFO)
_factor_log.propagate = False

def _log_factor_trade(sym, pos, r, pnl, shaped_r=None):
    """Log entry factors + trade outcome for post-200-trade statistical validation."""
    factors = pos.get('factors', {})
    _factor_log.info(json.dumps({
        't': time.time(), 'sym': sym,
        'trend': factors.get('trend'), 'momentum': factors.get('momentum'),
        'volume': factors.get('volume'), 'volatility': factors.get('volatility'),
        'structure': factors.get('structure'), 'order_flow': factors.get('order_flow'),
        'confidence': pos.get('confidence'),
        'ai_score': pos.get('ai_score'),
        'regime': pos.get('regime', 'chop'),
        'r': round(r, 4), 'r_shaped': round(shaped_r, 4) if shaped_r is not None else None,
        'pnl': round(pnl, 4),
        'tp_hit': pos.get('tp_hit', 0),
        'sigs': pos.get('sg', []),
        'duration_h': round((time.time() - pos.get('opened_at', time.time())) / 3600, 2),
        'corr': pos.get('corr'), 'corr_override': pos.get('corr_override', False)
    }))

def periodic_reconciler(ex, sts, brs, _thread_map, _cfg, interval=300):
    # Deviation alerts + watchdog for dead stream threads
    elapsed = 0
    while True:
        time.sleep(30)
        heartbeat('reconciler')
        elapsed += 30
        # Watchdog: restart dead stream threads
        for sym, t in list(_thread_map.items()):
            if not t.is_alive():
                logging.warning(f"WATCHDOG: {sym} stream thread died, restarting")
                st, br = sts[sym], brs[sym]
                iv = _cfg.get("poll_interval_seconds", 2.0)
                new_t = threading.Thread(target=stream, args=(st, sym, br, ex, iv), daemon=True)
                new_t.start()
                _thread_map[sym] = new_t
        # Purge expired cache entries to prevent memory leaks
        now = time.time()
        with _sym_cooldown_lock:
            for k in [k for k, v in list(_sym_cooldown.items()) if now - v > 2 * _COOLDOWN_SECS]:
                del _sym_cooldown[k]
        with _denial_backoff_lock:
            for k in [k for k, v in list(_denial_backoff.items()) if now > v["until"]]:
                del _denial_backoff[k]
        with _cache_lock:
            for k in [k for k, (t, _) in list(_htf_cache.items()) if now - t > 600]:
                del _htf_cache[k]
            for k in [k for k, (t, _) in list(_sentiment_cache.items()) if now - t > 2 * _sentiment_ttl]:
                del _sentiment_cache[k]
            for k in [k for k, (t, _) in list(_of_cache.items()) if now - t > 2 * _of_ttl]:
                del _of_cache[k]
        if elapsed >= interval:
            elapsed = 0
            try:
                reconcile_positions(ex, sts, brs)
                check_deviation_alerts()
            except Exception as e:
                logging.warning(f"Periodic reconciler error: {e}")

_RESCAN_INTERVAL = 7200  # 2 hours

def _universe_rescan(ex, sts, brs, _thread_map, _cfg, output_dir):
    """Periodic universe rescan — adds/drops symbols based on Kraken volume ranking."""
    while True:
        # Sleep in 30s chunks so health monitor sees heartbeats
        _deadline = time.time() + _RESCAN_INTERVAL
        while time.time() < _deadline:
            heartbeat('rescan')
            time.sleep(30)
        try:
            fresh = top_sym(20)
            if not fresh:
                logging.warning("RESCAN: top_sym returned empty, skipping")
                continue
            current = set(sts.keys())
            fresh_set = set(fresh)
            to_add = fresh_set - current
            to_drop = current - fresh_set

            # Never drop symbols with open positions
            protected = set()
            for sym in to_drop:
                if sts[sym].get_position() is not None:
                    protected.add(sym)
            to_drop -= protected

            if not to_add and not to_drop:
                continue

            iv = _cfg.get("poll_interval_seconds", 2.0)

            # Drop stale symbols
            for sym in to_drop:
                sts[sym].shutdown.set()
                t = _thread_map.pop(sym, None)
                if t:
                    t.join(timeout=5)
                del sts[sym]
                del brs[sym]

            # Add new symbols
            for sym in to_add:
                d = os.path.join(output_dir, sym.replace('/', '_'))
                st = BotState(d)
                br = AdaptiveBrain(st)
                sts[sym] = st
                brs[sym] = br
                t = threading.Thread(target=stream, args=(st, sym, br, ex, iv), daemon=True)
                t.start()
                _thread_map[sym] = t

            parts = []
            if to_add:
                parts.append(f"+{','.join(sorted(to_add))}")
            if to_drop:
                parts.append(f"-{','.join(sorted(to_drop))}")
            if protected:
                parts.append(f"kept(open pos):{','.join(sorted(protected))}")
            logging.info(f"RESCAN: {' | '.join(parts)} → {len(sts)} symbols")
        except Exception as e:
            logging.warning(f"RESCAN failed: {e}")

# Brain
_REGIMES = ('bull', 'bear', 'range', 'chop')

class AdaptiveBrain:
    SIG = 'abcdefghijklmnopqrstuvwxyz2'  # 27 signals: 14 original + 13 new (v2.4)

    def __init__(self, state):
        self.state = state
        self.stats = {s:{'f':0,'w':0} for s in self.SIG}
        self.dir_history = {'long': [], 'short': []}
        self.file = os.path.join(state.out_dir, "brain.json")
        self.trade_count = 0
        self.confidence = 0.5
        self.regime = 'chop'
        self.regime_proba = [0.25, 0.25, 0.25, 0.25]
        self.bayesian_model = BayesianFactorModel()  # UPGRADE A: Bayesian factor model
        self.load()

    def load(self):
        if os.path.exists(self.file):
            try:
                with open(self.file) as f:
                    data = json.load(f)
                saved = data.get('stats', {})
                for k, v in saved.items():
                    if k in self.stats:
                        self.stats[k] = v
                dh = data.get('dir_history', {})
                for d in ('long', 'short'):
                    if d in dh and isinstance(dh[d], list):
                        self.dir_history[d] = dh[d][-10:]
                self.trade_count = data.get('trade_count', 0)
                # UPGRADE A: Load Bayesian factor model
                if 'bayesian_model' in data:
                    self.bayesian_model = BayesianFactorModel.from_dict(data['bayesian_model'])
                # Legacy: load old EMA correlations for backward compat
                if 'aw_corr' in data:
                    self.aw_corr = data['aw_corr']
                    self.aw_n = data.get('aw_n', {r: 0 for r in _FACTOR_W})
                    self.aw = data.get('aw', {})
            except Exception as e:
                logging.warning(f"Failed to load brain.json: {e}")

    def save(self):
        try:
            self._init_adaptive_weights()
            data = {'stats': self.stats, 'dir_history': self.dir_history, 'trade_count': self.trade_count,
                    'aw_corr': self.aw_corr, 'aw_n': self.aw_n, 'aw': self.aw,
                    'bayesian_model': self.bayesian_model.to_dict()}  # UPGRADE A: persist Bayesian model
            tmp = self.file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, self.file)  # atomic on POSIX + Windows
        except Exception as e:
            logging.warning(f"Failed to save brain.json ({self.file}): {e}")

    def record(self, sigs, r, slippage=Decimal('0'), direction=None, factors=None, rgm=None):
        for s in sigs:
            if s not in self.stats:
                continue
            self.stats[s]['f'] += 1
            if r > 0: self.stats[s]['w'] += 1
        if direction in ('long', 'short'):
            self.dir_history[direction].append(1 if r > 0 else 0)
            self.dir_history[direction] = self.dir_history[direction][-10:]
        if factors is not None and rgm is not None:
            self._update_adaptive_weights(factors, r, rgm)
            # UPGRADE A: Update Bayesian logistic regression model
            self.bayesian_model.update(factors, r, rgm)
        self.trade_count += 1
        self.save()

    def _init_adaptive_weights(self):
        if not hasattr(self, 'aw_corr'):
            self.aw_corr = {r: {k: 0.0 for k in _FK} for r in _FACTOR_W}
            self.aw_n = {r: 0 for r in _FACTOR_W}
            self.aw = {}

    def _update_adaptive_weights(self, factors, r_mult, rgm):
        self._init_adaptive_weights()
        if rgm not in _FACTOR_W: return
        self.aw_n[rgm] = self.aw_n.get(rgm, 0) + 1
        r_sign = 1.0 if r_mult > 0 else (-1.0 if r_mult < 0 else 0.0)
        for k in _FK:
            fv = factors.get(k, 0.5)
            agreement = (fv - 0.5) * r_sign
            self.aw_corr[rgm][k] = (1 - _AW_ALPHA) * self.aw_corr[rgm][k] + _AW_ALPHA * agreement
        if self.aw_n[rgm] >= _AW_MIN_TRADES:
            prior = _FACTOR_W[rgm]
            raw = {}
            for k in _FK:
                raw[k] = prior[k] * math.exp(self.aw_corr[rgm][k] * 4.0)
            for k in _FK:
                raw[k] = max(_AW_FLOOR, min(_AW_CEIL, raw[k]))
            total = sum(raw.values())
            self.aw[rgm] = {k: round(raw[k] / total, 4) for k in _FK}

    def get_weights(self, rgm):
        # UPGRADE A: Prefer Bayesian model weights when enough data exists
        bayes_w = self.bayesian_model.get_weights(rgm)
        if bayes_w is not None:
            return bayes_w
        # Fallback to legacy EMA-adapted weights, then prior
        self._init_adaptive_weights()
        return self.aw.get(rgm, _FACTOR_W.get(rgm, _FACTOR_W_DEFAULT))

    def predict_win_probability(self, factors, rgm):
        """UPGRADE A: Get calibrated P(win | factors, regime) from Bayesian model."""
        return self.bayesian_model.predict_proba(factors, rgm)

    def signal_weight(self, sig, global_stats=None, prior_w=20):
        """Bayesian-blended signal win rate → weight in [0,1]. Suppresses signals with WR < 40%."""
        sf, sw = self.stats.get(sig, {}).get('f', 0), self.stats.get(sig, {}).get('w', 0)
        gf = gw = 0
        if global_stats and sig in global_stats:
            gf, gw = global_stats[sig].get('f', 0), global_stats[sig].get('w', 0)
        # Bayesian blend: per-symbol posterior with global prior
        g_wr = gw / gf if gf > 0 else 0.5
        eff_wr = (sw + g_wr * prior_w) / (sf + prior_w) if (sf + prior_w) > 0 else 0.5
        if eff_wr < 0.40: return 0.0  # suppress weak signals
        return round(max(0, min((eff_wr - 0.2) / 0.6, 1.0)), 4)  # 20%→0, 80%→1

    def dir_wr(self, direction):
        """Win rate for last 10 trades in given direction. Returns None if < 5 trades."""
        h = self.dir_history.get(direction, [])
        if len(h) < 5: return None
        return sum(h) / len(h)

    def wr(self):
        t = sum(d['f'] for d in self.stats.values())
        w = sum(d['w'] for d in self.stats.values())
        return w/t*100 if t else 0

# Indicators via TA-Lib (C-based, replaces hand-rolled pandas math)
try:
    import talib
    _HAS_TALIB = True
except ImportError:
    _HAS_TALIB = False

def ind(df):
    if len(df) < 50: return df
    c = df['close'].values.astype(float)

    if _HAS_TALIB:
        df['e9']  = talib.EMA(c, timeperiod=9)
        df['e21'] = talib.EMA(c, timeperiod=21)
        df['e200'] = talib.EMA(c, timeperiod=200)
        df['macd'], df['macd_sig'], df['macd_hist'] = talib.MACD(c, fastperiod=12, slowperiod=26, signalperiod=9)
        df['rsi'] = talib.RSI(c, timeperiod=14)
    else:
        df['e9'] = df['close'].ewm(span=9).mean()
        df['e21'] = df['close'].ewm(span=21).mean()
        df['e200'] = df['close'].ewm(span=200).mean()
        e12 = df['close'].ewm(span=12).mean()
        e26 = df['close'].ewm(span=26).mean()
        df['macd'] = e12 - e26
        df['macd_sig'] = df['macd'].ewm(span=9).mean()
        df['macd_hist'] = df['macd'] - df['macd_sig']
        d = df['close'].diff()
        g = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        l = -d.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
        df['rsi'] = 100 - 100/(1 + g/(l+1e-10))

    has_hlc = 'high' in df.columns and 'low' in df.columns
    if has_hlc:
        h = df['high'].values.astype(float)
        lo = df['low'].values.astype(float)
        if _HAS_TALIB:
            df['atr'] = talib.ATR(h, lo, c, timeperiod=14)
            df['bb_upper'], df['bb_mid'], df['bb_lower'] = talib.BBANDS(c, timeperiod=20, nbdevup=2, nbdevdn=2)
            df['adx'] = talib.ADX(h, lo, c, timeperiod=14)
            df['stoch_k'], df['stoch_d'] = talib.STOCH(h, lo, c, fastk_period=14, slowk_period=3, slowd_period=3)
        else:
            tr1 = df['high'] - df['low']
            tr2 = abs(df['high'] - df['close'].shift())
            tr3 = abs(df['low'] - df['close'].shift())
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            df['atr'] = tr.ewm(alpha=1/14, adjust=False).mean()
            df['bb_mid'] = df['close'].rolling(20).mean()
            bb_std = df['close'].rolling(20).std()
            df['bb_upper'] = df['bb_mid'] + 2 * bb_std
            df['bb_lower'] = df['bb_mid'] - 2 * bb_std
            up_move = df['high'].diff()
            down_move = -df['low'].diff()
            pdm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
            ndm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
            pdm_s = pdm.ewm(alpha=1/14, adjust=False).mean()
            ndm_s = ndm.ewm(alpha=1/14, adjust=False).mean()
            pdi = 100 * pdm_s / (df['atr'] + 1e-10)
            ndi = 100 * ndm_s / (df['atr'] + 1e-10)
            dx = 100 * abs(pdi - ndi) / (pdi + ndi + 1e-10)
            df['adx'] = dx.ewm(alpha=1/14, adjust=False).mean()
            ll14 = df['low'].rolling(14).min()
            hh14 = df['high'].rolling(14).max()
            df['stoch_k'] = 100 * (df['close'] - ll14) / (hh14 - ll14 + 1e-10)
            df['stoch_d'] = df['stoch_k'].rolling(3).mean()

        df['atr_pct'] = df['atr'] / df['close']
        df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / (df['bb_mid'] + 1e-10)
        df['bb_pctb'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'] + 1e-10)

        # Choppiness Index(14) — no TA-Lib equivalent
        if 'atr' in df.columns:
            atr_sum = df['atr'].rolling(14).sum()
            hh = df['high'].rolling(14).max()
            ll = df['low'].rolling(14).min()
            df['chop'] = 100 * np.log10(atr_sum / (hh - ll + 1e-10)) / np.log10(14)

    # MFI(14) — Money Flow Index
    if has_hlc and 'vol' in df.columns:
        if _HAS_TALIB:
            df['mfi'] = talib.MFI(df['high'].values.astype(float), df['low'].values.astype(float), c, df['vol'].values.astype(float), timeperiod=14)
        else:
            tp = (df['high'] + df['low'] + df['close']) / 3
            mf = tp * df['vol']
            pmf = pd.Series(np.where(tp > tp.shift(), mf, 0), index=df.index).rolling(14).sum()
            nmf = pd.Series(np.where(tp < tp.shift(), mf, 0), index=df.index).rolling(14).sum()
            df['mfi'] = 100 - 100 / (1 + pmf / (nmf + 1e-10))

    # OBV — On Balance Volume
    if 'vol' in df.columns:
        if _HAS_TALIB:
            df['obv'] = talib.OBV(c, df['vol'].values.astype(float))
        else:
            obv_dir = np.sign(df['close'].diff()).fillna(0)
            df['obv'] = (obv_dir * df['vol']).cumsum()

    return df

# =============================================================================
# HMM Regime Detection (optional — requires hmmlearn)
# =============================================================================
try:
    from hmmlearn.hmm import GaussianHMM
    _HAS_HMM = True
except ImportError:
    _HAS_HMM = False

_hmm_detectors = {}  # sym -> HMMRegimeDetector

class HMMRegimeDetector:
    """4-state Gaussian HMM for probabilistic regime detection.

    UPGRADE C: Expanded from 3 to 5 features:
    - log_return (winsorized 3σ)
    - vol_ratio (volume / 10-bar MA)
    - realized_vol (10-bar rolling std of log returns)
    - spread_proxy (high-low range normalized by close, proxy for spread/funding)
    - cross_asset_corr (rolling correlation of returns with BTC, proxy for systemic risk)

    Also adds sticky transition priors to reduce regime whipsaws.
    Falls back to uniform [0.25]*4 when not fitted.
    """
    _WINDOW = 750   # rolling training window
    _MIN_BARS = 200  # minimum bars required to fit
    _REFIT_INTERVAL = 500  # refit every N bars
    _PRED_TAIL = 50  # last N bars for forward prediction
    # UPGRADE C: Sticky transition prior — high self-transition probability
    _STICKY_ALPHA = 10.0  # Dirichlet concentration for self-transitions (higher = stickier)

    def __init__(self):
        self._lock = Lock()
        self.fitted = False
        self._model = None
        self._state_map = {}  # hmm_state_idx -> regime label
        self._bar_count = 0
        self._last_fit_bar = 0
        self._btc_returns = None  # UPGRADE C: cached BTC returns for cross-asset correlation

    def set_btc_returns(self, btc_df):
        """UPGRADE C: Cache BTC log returns for cross-asset correlation feature."""
        if btc_df is not None and len(btc_df) > 1 and 'close' in btc_df.columns:
            c = btc_df['close'].astype(float)
            self._btc_returns = np.log(c / c.shift(1))

    # -- feature engineering --------------------------------------------------
    @staticmethod
    def _features(df, btc_returns=None):
        """UPGRADE C: Build (N, 5) feature matrix from OHLCV DataFrame.
        Added spread_proxy and cross_asset_corr features."""
        c = df['close'].astype(float)
        lr = np.log(c / c.shift(1))
        # winsorise at 3σ
        mu, sigma = lr.mean(), lr.std()
        if sigma > 0:
            lr = lr.clip(mu - 3 * sigma, mu + 3 * sigma)
        # volume ratio: vol / 10-bar MA, normalised to ~1
        vr = df['vol'].astype(float) / df['vol'].astype(float).rolling(10).mean()
        vr = vr.clip(0, 5) / 5.0  # scale to [0,1]
        # realised vol: 10-bar rolling std of log returns
        rv = lr.rolling(10).std()
        rv_max = rv.quantile(0.99) if rv.quantile(0.99) > 0 else 1.0
        rv = (rv / rv_max).clip(0, 1)

        # UPGRADE C: Spread/funding proxy — normalized high-low range
        # Wide spreads indicate illiquidity or stress; narrow = calm
        if 'high' in df.columns and 'low' in df.columns:
            hl_range = (df['high'].astype(float) - df['low'].astype(float)) / c
            sp = hl_range.rolling(10).mean()
            sp_max = sp.quantile(0.99) if sp.quantile(0.99) > 0 else 1.0
            sp = (sp / sp_max).clip(0, 1)
        else:
            sp = pd.Series(0.5, index=df.index)

        # UPGRADE C: Cross-asset correlation — rolling 20-bar corr with BTC returns
        if btc_returns is not None and len(btc_returns) > 0:
            # Align indices
            aligned_lr = lr.reindex(btc_returns.index)
            aligned_btc = btc_returns.reindex(lr.index)
            # Rolling correlation (20-bar window)
            ca = aligned_lr.rolling(20).corr(aligned_btc)
            # NaN fill with 0.5 (neutral correlation) and normalize from [-1,1] to [0,1]
            ca = (ca.fillna(0) + 1) / 2.0
        else:
            ca = pd.Series(0.5, index=df.index)

        feat = pd.DataFrame({'lr': lr, 'vr': vr, 'rv': rv, 'sp': sp, 'ca': ca}).dropna()
        return feat.values, feat.index

    # -- state mapping --------------------------------------------------------
    def _map_states(self):
        """Map HMM states to regime labels by inspecting fitted means.

        Highest mean return → bull, lowest → bear,
        lowest vol_ratio of remaining two → range, other → chop.
        UPGRADE C: Works with 5 features [lr, vr, rv, sp, ca].
        """
        means = self._model.means_  # (4, 5) — [lr, vr, rv, sp, ca]
        n = means.shape[0]
        lr_means = means[:, 0]
        order = np.argsort(lr_means)  # ascending by return
        bear_idx = order[0]
        bull_idx = order[-1]
        remaining = [i for i in range(n) if i not in (bear_idx, bull_idx)]
        if len(remaining) == 2:
            # lower realised vol → range, higher → chop
            if means[remaining[0], 2] <= means[remaining[1], 2]:
                range_idx, chop_idx = remaining[0], remaining[1]
            else:
                range_idx, chop_idx = remaining[1], remaining[0]
        elif len(remaining) == 1:
            range_idx = remaining[0]
            chop_idx = remaining[0]
        else:
            range_idx = chop_idx = 0
        self._state_map = {
            bull_idx: 'bull', bear_idx: 'bear',
            range_idx: 'range', chop_idx: 'chop',
        }

    # -- fit / predict --------------------------------------------------------
    def fit(self, df):
        """Train HMM on rolling window. Thread-safe.
        UPGRADE C: Uses 5 features and sticky transition priors."""
        with self._lock:
            if len(df) < self._MIN_BARS:
                return
            tail = df.tail(self._WINDOW)
            X, _ = self._features(tail, self._btc_returns)
            if len(X) < self._MIN_BARS:
                return
            try:
                model = GaussianHMM(
                    n_components=4, covariance_type='full',
                    n_iter=80, tol=1e-4, random_state=42,
                )
                # UPGRADE C: Sticky transition priors — penalize rapid regime switching
                # Set initial transmat with high self-transition probability
                n_states = 4
                sticky_transmat = np.full((n_states, n_states), 1.0 / (n_states + self._STICKY_ALPHA - 1))
                for i in range(n_states):
                    sticky_transmat[i, i] = self._STICKY_ALPHA / (n_states + self._STICKY_ALPHA - 1)
                # Normalize rows
                sticky_transmat /= sticky_transmat.sum(axis=1, keepdims=True)
                model.transmat_ = sticky_transmat
                model.init_params = 'mc'  # only init means and covars, keep our transmat

                model.fit(X)
                self._model = model
                self._map_states()
                self.fitted = True
                self._last_fit_bar = self._bar_count
            except Exception as e:
                logging.debug(f"HMM fit failed: {e}")

    def predict_proba(self, df):
        """Return [p_bull, p_bear, p_range, p_chop] from forward algorithm.

        Uses last _PRED_TAIL bars. Returns uniform if not fitted.
        UPGRADE C: Uses 5 features.
        """
        with self._lock:
            if not self.fitted or self._model is None:
                return [0.25, 0.25, 0.25, 0.25]
            try:
                tail = df.tail(self._PRED_TAIL)
                X, _ = self._features(tail, self._btc_returns)
                if len(X) < 10:
                    return [0.25, 0.25, 0.25, 0.25]
                posteriors = self._model.predict_proba(X)
                last = posteriors[-1]  # (4,) probabilities for last bar
                proba = [0.0] * 4
                for hmm_idx, p in enumerate(last):
                    label = self._state_map.get(hmm_idx, 'chop')
                    reg_idx = _REGIMES.index(label)
                    proba[reg_idx] += p
                return proba
            except Exception as e:
                logging.debug(f"HMM predict_proba failed: {e}")
                return [0.25, 0.25, 0.25, 0.25]

    def dominant_regime(self, df):
        """Return string label of the most likely regime."""
        proba = self.predict_proba(df)
        return _REGIMES[int(np.argmax(proba))]

    def needs_refit(self):
        """True every _REFIT_INTERVAL bars since last fit."""
        return (self._bar_count - self._last_fit_bar) >= self._REFIT_INTERVAL

    def tick(self):
        """Increment bar counter (call once per new bar)."""
        self._bar_count += 1

def regime(df):
    """Classify market regime from OHLC DataFrame."""
    if len(df) < 50: return 'chop'
    l = df.iloc[-1]
    if 'e9' not in l or 'e21' not in l or 'e200' not in l:
        return 'chop'
    if l['e9'] > l['e21'] > l['e200']:     align = 1.0
    elif l['e9'] < l['e21'] < l['e200']:   align = -1.0
    elif l['e9'] > l['e21']:               align = 0.5
    elif l['e9'] < l['e21']:               align = -0.5
    else:                                   align = 0.0
    chop_val = l.get('chop', 50)
    adx_val = l.get('adx', 20)
    if chop_val > 61.8:                     return 'chop'
    if adx_val > 25 and align > 0.5:        return 'bull'
    if adx_val > 25 and align < -0.5:       return 'bear'
    if adx_val < 20:                        return 'range'
    return 'chop'

def is_exhausted(df, direction):
    """Detect trend exhaustion: requires 2 of 3 conditions."""
    if len(df) < 5: return False
    l, p, pp = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    hits = 0
    # Condition 1: RSI extreme
    if 'rsi' in l:
        if direction == 'long' and l['rsi'] > 80: hits += 1
        if direction != 'long' and l['rsi'] < 20: hits += 1
    # Condition 2: MACD histogram declining for 2+ bars
    if 'macd_hist' in l and 'macd_hist' in p and 'macd_hist' in pp:
        if direction == 'long' and l['macd_hist'] < p['macd_hist'] < pp['macd_hist']: hits += 1
        if direction != 'long' and l['macd_hist'] > p['macd_hist'] > pp['macd_hist']: hits += 1
    # Condition 3: Volume below 70% of 5-bar average (excl current bar)
    if 'vol' in df.columns and len(df) >= 6:
        vol_avg5 = df['vol'].iloc[-6:-1].mean()
        if vol_avg5 > 0 and l['vol'] < vol_avg5 * 0.7: hits += 1
    return hits >= 2

def _safe(val, default):
    """Return default if val is None, NaN, or pd.NA, else val."""
    if val is None: return default
    try:
        if pd.isna(val): return default
    except (TypeError, ValueError):
        pass
    return val

# =============================================================================
# 6-Factor Scoring Model
# =============================================================================
# Regime-specific factor weights. Each row sums to 1.0.
# After 200+ trades, run decile regression per regime to refine these.
_FACTOR_W_LONG = {
    'bull':  {'trend': 0.37, 'momentum': 0.27, 'structure': 0.13, 'volume': 0.08, 'volatility': 0.07, 'order_flow': 0.08},
    'bear':  {'trend': 0.17, 'momentum': 0.17, 'structure': 0.22, 'volume': 0.08, 'volatility': 0.21, 'order_flow': 0.15},
    'range': {'trend': 0.12, 'momentum': 0.17, 'structure': 0.30, 'volume': 0.13, 'volatility': 0.13, 'order_flow': 0.15},
    'chop':  {'trend': 0.08, 'momentum': 0.13, 'structure': 0.18, 'volume': 0.13, 'volatility': 0.38, 'order_flow': 0.10},
}
_FACTOR_W_SHORT = {
    'bull':  {'trend': 0.17, 'momentum': 0.17, 'structure': 0.22, 'volume': 0.08, 'volatility': 0.21, 'order_flow': 0.15},
    'bear':  {'trend': 0.37, 'momentum': 0.27, 'structure': 0.13, 'volume': 0.08, 'volatility': 0.07, 'order_flow': 0.08},
    'range': {'trend': 0.12, 'momentum': 0.17, 'structure': 0.30, 'volume': 0.13, 'volatility': 0.13, 'order_flow': 0.15},
    'chop':  {'trend': 0.08, 'momentum': 0.13, 'structure': 0.18, 'volume': 0.13, 'volatility': 0.38, 'order_flow': 0.10},
}
_FACTOR_W = _FACTOR_W_LONG  # reassigned in main() if --mode short
_FACTOR_W_DEFAULT = {'trend': 0.27, 'momentum': 0.22, 'structure': 0.18, 'volume': 0.13, 'volatility': 0.08, 'order_flow': 0.12}
_FK = ('trend', 'momentum', 'structure', 'volume', 'volatility', 'order_flow')
_AW_ALPHA = 0.1       # EMA decay for correlation updates
_AW_MIN_TRADES = 50   # per-regime minimum before adapting
_AW_FLOOR = 0.03      # minimum weight per factor
_AW_CEIL = 0.60       # maximum weight per factor

# UPGRADE: Factor interaction terms (B) — cross-factor products that capture synergy
_INTERACTION_TERMS = [
    ('trend', 'momentum'),      # aligned trend + momentum = high conviction
    ('structure', 'volume'),    # structure at key level + volume confirmation
    ('order_flow', 'momentum'), # order flow + momentum = smart money pressure
]
_INTERACTION_WEIGHT = 0.15  # fraction of total confidence from interaction terms

# UPGRADE: Bayesian logistic regression (A) — replaces EMA correlation tracker
class BayesianFactorModel:
    """Online Bayesian logistic regression for P(win | factors, regime).
    Uses Laplace approximation with online updates (natural gradient).
    Provides calibrated win probability instead of weighted-sum-then-threshold.
    """
    # 6 base factors + 3 interaction terms + 1 bias = 10 features
    _N_BASE = len(_FK)
    _N_INTERACT = len(_INTERACTION_TERMS)
    _N_FEAT = _N_BASE + _N_INTERACT + 1  # +1 for bias

    def __init__(self):
        # Per-regime parameters: mean vector and precision (inverse variance)
        self.mu = {r: np.zeros(self._N_FEAT) for r in _REGIMES}
        self.precision = {r: np.eye(self._N_FEAT) * 0.1 for r in _REGIMES}  # weak prior
        self.n = {r: 0 for r in _REGIMES}

    def _build_features(self, factors):
        """Build feature vector: [base_factors, interaction_terms, bias]."""
        base = np.array([factors.get(k, 0.5) for k in _FK])
        interactions = np.array([
            factors.get(a, 0.5) * factors.get(b, 0.5)
            for a, b in _INTERACTION_TERMS
        ])
        return np.concatenate([base, interactions, [1.0]])

    def _sigmoid(self, x):
        x = np.clip(x, -20, 20)
        return 1.0 / (1.0 + np.exp(-x))

    def predict_proba(self, factors, rgm):
        """P(win | factors, regime) — calibrated probability."""
        if rgm not in self.mu:
            return 0.5
        x = self._build_features(factors)
        logit = x @ self.mu[rgm]
        return float(self._sigmoid(logit))

    def get_weights(self, rgm):
        """Extract factor weights from learned coefficients for compatibility.
        Returns dict of {factor_name: weight} normalized to sum to 1."""
        if rgm not in self.mu or self.n.get(rgm, 0) < _AW_MIN_TRADES:
            return None  # not enough data, use prior
        coefs = self.mu[rgm][:self._N_BASE]
        # Use absolute value of coefficients as importance weights
        abs_coefs = np.abs(coefs) + 1e-8
        # Clip to floor/ceiling before normalizing
        abs_coefs = np.clip(abs_coefs, _AW_FLOOR, _AW_CEIL)
        total = abs_coefs.sum()
        weights = {k: round(float(abs_coefs[i] / total), 4) for i, k in enumerate(_FK)}
        return weights

    def update(self, factors, r_mult, rgm):
        """Online Bayesian update with a single observation.
        Uses Laplace approximation: approximate posterior as Gaussian,
        update via natural gradient on the log-likelihood.
        """
        if rgm not in self.mu:
            return
        x = self._build_features(factors)
        y = 1.0 if r_mult > 0 else 0.0  # binary outcome

        # Current prediction
        logit = x @ self.mu[rgm]
        p = self._sigmoid(logit)

        # Learning rate decays with sample count for stability
        n = self.n.get(rgm, 0)
        lr = max(0.01, 0.1 / (1 + n / 100.0))

        # Gradient of log-likelihood for logistic regression
        grad = (y - p) * x

        # Online precision update (approximate Hessian)
        # H = p * (1-p) * x @ x^T
        w = p * (1 - p) + 1e-8
        self.precision[rgm] += lr * w * np.outer(x, x)

        # Natural gradient update: delta_mu = P^{-1} @ grad
        try:
            delta = np.linalg.solve(self.precision[rgm], grad)
            self.mu[rgm] += lr * delta
        except np.linalg.LinAlgError:
            # Fallback: simple gradient step
            self.mu[rgm] += lr * 0.01 * grad

        self.n[rgm] = n + 1

    def to_dict(self):
        """Serialize for JSON persistence."""
        return {
            'mu': {r: self.mu[r].tolist() for r in self.mu},
            'precision': {r: self.precision[r].tolist() for r in self.precision},
            'n': dict(self.n),
        }

    @classmethod
    def from_dict(cls, d):
        """Deserialize from JSON."""
        obj = cls()
        if 'mu' in d:
            for r in d['mu']:
                if r in obj.mu:
                    arr = np.array(d['mu'][r])
                    if len(arr) == obj._N_FEAT:
                        obj.mu[r] = arr
        if 'precision' in d:
            for r in d['precision']:
                if r in obj.precision:
                    arr = np.array(d['precision'][r])
                    if arr.shape == (obj._N_FEAT, obj._N_FEAT):
                        obj.precision[r] = arr
        if 'n' in d:
            obj.n = {r: d['n'].get(r, 0) for r in _REGIMES}
        return obj

def compute_factors(df, price, vp_data, ob_data, htf_bias=0.5, rgm='chop', of_score=0.5, funding_rate=None, whale_score=None, short_mode=False):
    """Compute 6 continuous factor scores in [0,1] from indicator DataFrame.
    When short_mode=True, directional factors are inverted for bearish entries."""
    l = df.iloc[-1]
    inv = -1 if short_mode else 1  # direction multiplier

    # --- Trend (0.30) — distance-based, no binary jumps ---
    e9, e21, e200 = _safe(l.get('e9'), 0), _safe(l.get('e21'), 0), _safe(l.get('e200'), 0)
    # EMA cross: continuous distance, inverted for short (e21-e9 = bearish separation)
    ema_sep = inv * (e9 - e21) / (e21 + 1e-9)
    ema_cross = max(0, min(ema_sep / 0.02 * 0.5 + 0.5, 1.0))  # ±2% → [0,1]
    # EMA stack: inverted for short (e200>e21 = bearish alignment)
    stack_21_200 = inv * ((e21 - e200) / (e200 + 1e-9)) if e200 > 0 else 0
    ema_stack = max(0, min(stack_21_200 / 0.05 * 0.5 + 0.5, 1.0))  # ±5% → [0,1]
    adx = _safe(l.get('adx'), 0)
    adx_norm = min(adx / 50.0, 1.0)
    htf_norm = max(0, min(1.0 - _safe(htf_bias, 0.5) if short_mode else _safe(htf_bias, 0.5), 1.0))
    trend = ema_cross*0.30 + ema_stack*0.25 + adx_norm*0.25 + htf_norm*0.20

    # --- Momentum (0.25) ---
    rsi = _safe(l.get('rsi'), 50)
    rsi_norm = (100 - rsi) / 100.0 if short_mode else rsi / 100.0
    mh = _safe(l.get('macd_hist'), 0)
    macd_abs = abs(_safe(l.get('macd'), 0.001)) + 1e-9
    # Relative magnitude: inverted for short (negative hist = bearish strength)
    mag_norm = max(0, min((inv * mh / macd_abs + 1) / 2, 1.0))
    # Acceleration: short checks declining (h[-1] < h[-2] < h[-3])
    accel = 0.0
    if len(df) >= 3 and 'macd_hist' in df.columns:
        h3 = df['macd_hist'].iloc[-3:].dropna().values
        if len(h3) >= 3:
            if short_mode:
                if h3[-1] < h3[-2] < h3[-3]: accel = 1.0
                elif h3[-1] < h3[-2]:        accel = 0.5
            else:
                if h3[-1] > h3[-2] > h3[-3]: accel = 1.0
                elif h3[-1] > h3[-2]:        accel = 0.5
    macd_hist_norm = mag_norm * 0.6 + accel * 0.4
    sk = _safe(l.get('stoch_k'), 50)
    stoch_norm = (100 - sk) / 100.0 if short_mode else sk / 100.0
    momentum = rsi_norm*0.35 + macd_hist_norm*0.40 + stoch_norm*0.25

    # --- Volume (0.15) ---
    vol_ma = df['vol'].iloc[-11:-1].mean() if len(df) >= 11 else df['vol'].iloc[:-1].mean() if len(df) > 1 else 1
    cur_vol = _safe(l.get('vol'), 0)
    vol_norm = min(cur_vol / (vol_ma + 1e-9), 3.0) / 3.0
    mfi = _safe(l.get('mfi'), 50)
    mfi_norm = (100 - mfi) / 100.0 if short_mode else mfi / 100.0
    obv_series = df['obv'] if 'obv' in df.columns else pd.Series([0])
    obv_ma = obv_series.iloc[-10:].mean() if len(obv_series) >= 10 else obv_series.mean()
    obv_dev = (obv_series.iloc[-1] - obv_ma) / (abs(obv_ma) + 1e-9)
    obv_norm = 0.5 + min(max(inv * obv_dev, -1), 1) * 0.5
    volume = vol_norm*0.40 + mfi_norm*0.30 + obv_norm*0.30

    # --- Volatility (0.10) — regime-conditioned interpretation ---
    bb_w = _safe(l.get('bb_width'), 0)
    bb_raw = min(bb_w, 0.1) / 0.1  # bb_width already price-normalized in ind(); 0=tight, 1=wide
    atr_pct = _safe(l.get('atr_pct'), 0.02)
    atr_raw = min(atr_pct / 0.05, 1.0)  # 0=calm, 1=volatile
    # Short mode swaps bull/bear interpretation: bear favors squeezes, bull favors expansion
    if short_mode:
        if rgm == 'bull':
            volatility = bb_raw*0.60 + atr_raw*0.40
        elif rgm in ('bear', 'range'):
            volatility = (1.0 - bb_raw)*0.60 + (1.0 - atr_raw)*0.40
        else:
            volatility = 0.5
    else:
        if rgm in ('bull', 'range'):
            volatility = (1.0 - bb_raw)*0.60 + (1.0 - atr_raw)*0.40  # tight = high score
        elif rgm == 'bear':
            volatility = bb_raw*0.60 + atr_raw*0.40  # expansion = high score (trend strength)
        else:  # chop
            volatility = 0.5

    # --- Structure (0.20) ---
    poc = vp_data.get('poc') if vp_data else None
    va = vp_data.get('value_area') if vp_data else None
    poc_dist = 0.5
    va_score = 0.5
    if poc and price > 0:
        poc_dist = 1.0 - min(abs(price - poc) / (price * 0.02 + 1e-9), 1.0)
    if va:
        va_lo, va_hi = va
        if va_lo <= price <= va_hi:
            va_score = 0.8
        else:
            va_score = 0.3
    ob_score = 0.4
    if ob_data:
        _blk_key = 'bearish_blocks' if short_mode else 'bullish_blocks'
        for blk in ob_data.get(_blk_key, []):
            if blk['bottom'] <= price <= blk['top']:
                ob_score = 0.9
                break
    structure = poc_dist*0.30 + va_score*0.30 + ob_score*0.40

    # --- Order Flow ---
    bvc_flow = 0.5
    if len(df) >= 20:
        _ohlcv = df.iloc[-20:]
        _open = _ohlcv['o'] if 'o' in _ohlcv.columns else _ohlcv['close'].shift(1).bfill()
        _z = (_ohlcv['close'] - _open) / (_ohlcv['high'] - _ohlcv['low'] + 1e-9)
        _z = _z.clip(-3, 3)
        _buy_pct = _z.apply(lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2))))
        _net = inv * (_buy_pct * _ohlcv['vol'] - (1 - _buy_pct) * _ohlcv['vol']).mean()
        _avg_v = _ohlcv['vol'].mean() + 1e-9
        bvc_flow = max(0, min((_net / _avg_v + 1) / 2, 1.0))
    # Funding rate: short inverts interpretation (positive FR = shorts get paid = bullish for shorts)
    if funding_rate is not None:
        fr_norm = max(0, min(0.5 + inv * (-funding_rate * 5000), 1.0))
    else:
        fr_norm = None
    # Whale score: 0-100 from Whale Watcher → normalize to [0,1]
    ws_norm = max(0, min(whale_score / 100.0, 1.0)) if whale_score is not None else None
    # Order book imbalance: short inverts (ask-heavy = bullish for shorts)
    of_adj = (1.0 - of_score) if short_mode else of_score

    # Order flow composition — adaptive based on available data sources
    _of_parts = [(of_adj, 0.40)]  # order book imbalance (direction-adjusted)
    _of_parts.append((bvc_flow, 0.15))  # BVC estimated flow
    if fr_norm is not None:  _of_parts.append((fr_norm, 0.15))
    if ws_norm is not None:  _of_parts.append((ws_norm, 0.20))
    # Normalize weights to sum to 1.0
    _of_tw = sum(w for _, w in _of_parts)
    order_flow = sum(v * w / _of_tw for v, w in _of_parts)

    return {
        'trend': round(max(0, min(trend, 1.0)), 4),
        'momentum': round(max(0, min(momentum, 1.0)), 4),
        'volume': round(max(0, min(volume, 1.0)), 4),
        'volatility': round(max(0, min(volatility, 1.0)), 4),
        'structure': round(max(0, min(structure, 1.0)), 4),
        'order_flow': round(max(0, min(order_flow, 1.0)), 4),
    }

def compute_confidence(factors, rgm='chop', rgm_proba=None, brain=None):
    """Regime-weighted sum of 6 factor scores + interaction terms -> confidence in [0,1].

    When rgm_proba is provided (list of 4 floats matching _REGIMES order),
    compute blended weights as probability-weighted average of regime-specific
    factor weights instead of using a single regime's weights.
    When brain is provided, uses brain.get_weights() for adaptive weights.

    UPGRADE B: Adds factor interaction terms (trend*momentum, structure*volume,
    order_flow*momentum) to capture synergistic setups that score poorly as
    independent factors but strongly together.
    """
    if rgm_proba is not None:
        blended = {}
        for key in _FACTOR_W_DEFAULT:
            blended[key] = sum(
                p * (brain.get_weights(r) if brain else _FACTOR_W.get(r, _FACTOR_W_DEFAULT))[key]
                for p, r in zip(rgm_proba, _REGIMES)
            )
        linear = sum(factors[k] * blended[k] for k in blended)
    else:
        w = brain.get_weights(rgm) if brain else _FACTOR_W.get(rgm, _FACTOR_W_DEFAULT)
        linear = sum(factors[k] * w[k] for k in w)

    # UPGRADE B: Factor interaction terms — capture synergy between factor pairs
    # Each interaction is the product of two factors, centered at 0.25 (= 0.5*0.5)
    # so the bonus is positive only when BOTH factors are above average
    interaction_sum = 0.0
    for fa, fb in _INTERACTION_TERMS:
        product = factors.get(fa, 0.5) * factors.get(fb, 0.5)
        # Center: 0.5*0.5=0.25, so (product - 0.25) ranges from -0.25 to +0.75
        interaction_sum += (product - 0.25) / 0.75  # normalize to [-0.33, 1.0]
    # Average interaction contribution
    interaction_avg = interaction_sum / len(_INTERACTION_TERMS) if _INTERACTION_TERMS else 0.0
    # Blend: (1-w)*linear + w*interaction bonus
    confidence = (1 - _INTERACTION_WEIGHT) * linear + _INTERACTION_WEIGHT * max(0, interaction_avg)
    return round(max(0, min(confidence, 1.0)), 4)

# --- Blended regime helpers (HMM probabilistic regime) ---
def _blended_rgm_mult(proba, direction=None):
    """Probability-weighted regime sizing multiplier."""
    if direction is None: direction = _MODE
    mults = [_RGM_MULT.get(r, {}).get(direction, 0.5) for r in _REGIMES]
    return sum(p * m for p, m in zip(proba, mults))

def _blended_rgm_conf_min(proba, trade_count=0):
    """Probability-weighted minimum confidence threshold."""
    conf_min = _get_conf_min(trade_count)
    mins = [conf_min.get(r, 0.55) for r in _REGIMES]
    return sum(p * m for p, m in zip(proba, mins))

# =============================================================================
# Signal weighting — adaptive confluence scoring from brain stats
# =============================================================================
_WTD_SIG_MIN_LONG  = {'bull': 0.50, 'bear': 0.60, 'range': 0.55, 'chop': 0.65}
_WTD_SIG_MIN_SHORT = {'bull': 0.60, 'bear': 0.50, 'range': 0.55, 'chop': 0.65}
_WTD_SIG_MIN = _WTD_SIG_MIN_LONG  # reassigned in main() if --mode short

def get_global_signal_stats(brs_dict):
    """Aggregate per-signal fires/wins across all symbols."""
    stats = {}
    for br in brs_dict.values():
        for sig in br.SIG:
            if sig not in stats:
                stats[sig] = {'f': 0, 'w': 0}
            stats[sig]['f'] += br.stats.get(sig, {}).get('f', 0)
            stats[sig]['w'] += br.stats.get(sig, {}).get('w', 0)
    return stats

def compute_weighted_confluence(firing_sigs, br, rgm='chop', global_stats=None):
    """Weighted signal confluence score. Returns (score, active_sigs, suppressed_sigs)."""
    active, suppressed = [], []
    score = 0.0
    for s in firing_sigs:
        w = br.signal_weight(s, global_stats)
        if w > 0:
            score += w
            active.append(s)
        else:
            suppressed.append(s)
    return score, active, suppressed

# =============================================================================
# Expanded Indicator Suite - Auction Theory
# =============================================================================
def compute_volume_profile(df, bins=20):
    if len(df) < bins * 2:
        return {'value_area': None, 'poc': None, 'profile': {}}
    try:
        low, high = df['low'].min(), df['high'].max()
        if high == low:
            return {'value_area': None, 'poc': None, 'profile': {}}
        price_range = high - low
        bin_size = price_range / bins
        profile = {}
        for _, row in df.iterrows():
            bin_idx = int((row['close'] - low) / bin_size)
            bin_idx = max(0, min(bins - 1, bin_idx))
            profile[bin_idx] = profile.get(bin_idx, 0) + row['vol']
        total_vol = sum(profile.values())
        if total_vol == 0:
            return {'value_area': None, 'poc': None, 'profile': {}}
        poc = max(profile, key=profile.get)
        sorted_bins = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        cumsum = 0
        value_area_bins = []
        for bin_idx, vol in sorted_bins:
            cumsum += vol
            value_area_bins.append(bin_idx)
            if cumsum / total_vol >= 0.7:
                break
        va_low = min(value_area_bins) * bin_size + low
        va_high = max(value_area_bins) * bin_size + low + bin_size
        return {
            'value_area': (va_low, va_high),
            'poc': poc * bin_size + low + bin_size / 2,
            'profile': profile
        }
    except Exception as e:
        logging.debug(f"Volume profile error: {e}")
        return {'value_area': None, 'poc': None, 'profile': {}}

def find_order_blocks(df, lookback=10, threshold=0.003):
    if len(df) < lookback + 5:
        return {'bullish_blocks': [], 'bearish_blocks': []}
    bullish = []
    bearish = []
    for i in range(len(df) - lookback, len(df) - 5):
        segment = df.iloc[i:i + lookback]
        if len(segment) < 3:
            continue
        segment_high = segment['high'].max()
        segment_low = segment['low'].min()
        closes_above = (segment['close'] > segment_high * (1 - threshold)).sum()
        closes_below = (segment['close'] < segment_low * (1 + threshold)).sum()
        if closes_below >= lookback * 0.8:
            bearish.append({'top': segment_high, 'bottom': segment_low, 'index': i})
        if closes_above >= lookback * 0.8:
            bullish.append({'top': segment_high, 'bottom': segment_low, 'index': i})
    return {'bullish_blocks': bullish[-3:], 'bearish_blocks': bearish[-3:]}

def sigs(df):
    """Dispatch to long or short signal detection based on _MODE."""
    return _sigs_short(df) if _MODE == 'short' else _sigs_long(df)

def _sigs_long(df):
    """27 long signals across 13 dimensions (a-z,2). v2.4: +13 signals (b,i,j,m,q,t-z,2)."""
    if len(df) < 4: return []
    l, p = df.iloc[-1], df.iloc[-2]
    pp = df.iloc[-3]
    sg = []

    # a: EMA9/21 cross up (Trend Direction)
    if 'e9' in l and 'e21' in l and 'e9' in p and 'e21' in p:
        if l['e9'] > l['e21'] and p['e9'] <= p['e21']: sg.append('a')

    # c: RSI<30 rising — cross detection (Momentum — oversold bounce)
    if 'rsi' in l and 'rsi' in p and 'rsi' in pp:
        if l['rsi'] < 30 and l['rsi'] > p['rsi'] and p['rsi'] <= pp['rsi']: sg.append('c')

    # d: BB lower bounce + green candle (Mean Reversion)
    if 'bb_lower' in l and 'bb_upper' in l:
        if l['close'] <= l['bb_lower'] and l['close'] > p['close']: sg.append('d')

    # e: EMA pullback buy — price dips to EMA21 in uptrend (Trend Continuation)
    if 'e9' in l and 'e21' in l and 'e200' in l:
        in_uptrend = l['e9'] > l['e21'] > l['e200']
        near_e21 = abs(l['close'] - l['e21']) / (l['e21'] + 1e-10) < 0.015
        bouncing = l['close'] > p['close']
        if in_uptrend and near_e21 and bouncing: sg.append('e')

    # f: MACD hist rising — negative but rising 3 bars (Acceleration Shift)
    if 'macd_hist' in l and 'macd_hist' in p and 'macd_hist' in pp:
        if l['macd_hist'] < 0 and l['macd_hist'] > p['macd_hist'] > pp['macd_hist']:
            sg.append('f')

    # g: RSI bull range — crosses above 50 from below (Momentum Regime)
    if 'rsi' in l and 'rsi' in p:
        if l['rsi'] > 50 and p['rsi'] < 50: sg.append('g')

    # h: BB %B recovery — crosses above 0.2 from below (Mean Reversion Confirm)
    if 'bb_pctb' in l and 'bb_pctb' in p:
        if l['bb_pctb'] > 0.2 and p['bb_pctb'] < 0.2: sg.append('h')

    # j: OBV bull divergence — DISABLED (42% TP1, -0.39R in 180d backtest)
    # if len(df) >= 20 and 'obv' in l:
    #     obv_d = _obv_divergence(df, lookback=20)
    #     if obv_d == 'bull': sg.append('j')

    # k: BB squeeze + breakout — HARD DISABLED (sub-27% WR, negative R)
    # if 'bb_width' in l and len(df) >= 20:
    #     avg_bw = df['bb_width'].iloc[-20:].mean()
    #     if avg_bw > 0:
    #         squeeze_bars = sum(1 for x in df['bb_width'].iloc[-5:] if x < avg_bw * 0.6)
    #         if squeeze_bars >= 3 and l['close'] > l.get('bb_upper', l['close']):
    #             sg.append('k')

    # l: MACD hist bull divergence (Divergence)
    if len(df) >= 20 and 'macd_hist' in l:
        mh_d = _macd_hist_divergence(df, lookback=20)
        if mh_d == 'bull': sg.append('l')

    # m: MFI recovery — DISABLED (51% TP1, -0.15R avg in 365d backtest)
    # if 'mfi' in l and len(df) >= 4:
    #     recent_mfi = df['mfi'].iloc[-4:-1].dropna()
    #     if len(recent_mfi) > 0 and recent_mfi.min() < 30 and l['mfi'] > 35:
    #         sg.append('m')

    # n: Volume surge — vol > 2x 10-bar avg on green candle (Volume Conviction)
    if 'vol' in l and len(df) >= 11:
        vol_avg10 = df['vol'].iloc[-11:-1].mean()
        if vol_avg10 > 0 and l['vol'] > vol_avg10 * 2 and l['close'] > p['close']:
            sg.append('n')

    # o: ADX trend strength — HARD DISABLED (sub-27% WR, negative R)
    # if 'adx' in l and 'adx' in p and 'e9' in l and 'e21' in l:
    #     if l['adx'] > 25 and l['adx'] > p['adx'] and l['e9'] > l['e21']:
    #         sg.append('o')

    # p: Stochastic oversold cross — %K<20 crosses above %D (Alt Momentum)
    if 'stoch_k' in l and 'stoch_d' in l and 'stoch_k' in p and 'stoch_d' in p:
        if l['stoch_k'] < 20 and l['stoch_k'] > l['stoch_d'] and p['stoch_k'] <= p['stoch_d']:
            sg.append('p')

    # q: RSI bullish divergence — DISABLED (20% TP1, -0.71R in 180d backtest)
    # if len(df) >= 20 and 'rsi' in l:
    #     s20 = df.iloc[-20:]
    #     p_lows = _find_swing_lows(s20['close'])
    #     if len(p_lows) >= 2:
    #         i1, i2 = p_lows[-2], p_lows[-1]
    #         if s20['close'].iloc[i2] < s20['close'].iloc[i1]:
    #             r_lows = _find_swing_lows(s20['rsi'])
    #             if len(r_lows) >= 2:
    #                 j1, j2 = r_lows[-2], r_lows[-1]
    #                 if s20['rsi'].iloc[j2] > s20['rsi'].iloc[j1]:
    #                     sg.append('q')

    # r: MACD zero-line cross — HARD DISABLED (sub-27% WR, negative R)
    # if 'macd' in l and 'macd' in p:
    #     if l['macd'] > 0 and p['macd'] <= 0: sg.append('r')

    # s: ATR expansion — HARD DISABLED (sub-27% WR, negative R)
    # if 'atr' in l and len(df) >= 15:
    #     atr_ma14 = df['atr'].iloc[-15:-1].mean()
    #     if atr_ma14 > 0 and l['atr'] > atr_ma14 * 1.3 and l['close'] > p['close']:
    #         sg.append('s')

    # ── v2.4 new signals (b,i,j,m,q,t,u,v,w,x,y,z,2) ──────────────────

    # b: Bullish engulfing candle at pullback (Candle Pattern — Reversal)
    if 'open' in l and 'open' in p:
        prev_red = p['close'] < p['open']
        curr_green = l['close'] > l['open']
        engulfs = l['open'] <= p['close'] and l['close'] >= p['open']
        after_decline = p['close'] < p.get('e9', p['close'])
        if prev_red and curr_green and engulfs and after_decline: sg.append('b')

    # i: MFI oversold recovery — crosses above 20 from below (Money Flow Oversold)
    # Requires MACD histogram rising (signal f) as confluence — data shows 11 trades
    # at 63.6% WR / +0.524R with f, vs 12 trades at 33.3% WR / -0.159R without f.
    # Signal i without f is fee-negative at any practical size.
    _i_mfi_cross = ('mfi' in l and 'mfi' in p
                    and l['mfi'] > 20 and p['mfi'] <= 20)
    _f_macd_rising = ('macd_hist' in l and 'macd_hist' in p and 'macd_hist' in pp
                      and l['macd_hist'] < 0
                      and l['macd_hist'] > p['macd_hist'] > pp['macd_hist'])
    if _i_mfi_cross and _f_macd_rising:
        sg.append('i')

    # j: OBV trend breakout — HARD DISABLED (sub-27% WR, negative R)
    # if 'obv' in df.columns and len(df) >= 21:
    #     obv_ma20 = df['obv'].iloc[-21:-1].mean()
    #     if l['obv'] > obv_ma20 and p['obv'] <= obv_ma20: sg.append('j')

    # m: MFI bull range — HARD DISABLED (sub-27% WR, negative R)
    # if 'mfi' in l and 'mfi' in p:
    #     if l['mfi'] > 50 and p['mfi'] <= 50: sg.append('m')

    # q: Hammer candle near support (Candle Pattern — Reversal)
    if 'high' in l and 'low' in l and 'open' in l:
        body = abs(l['close'] - l['open'])
        lwick = min(l['close'], l['open']) - l['low']
        uwick = l['high'] - max(l['close'], l['open'])
        cr = l['high'] - l['low']
        if cr > 0 and body > 0:
            hammer = lwick >= 2.0 * body and uwick <= body * 0.5 and l['close'] > l['open']
            near_sup = l['close'] < l.get('e21', l['close'] * 1.01)
            if hammer and near_sup: sg.append('q')

    # t: Chop exit — choppiness drops below 38.2 in uptrend (Regime Quality)
    if 'chop' in l and 'chop' in p and 'e9' in l and 'e21' in l:
        if l['chop'] < 38.2 and p['chop'] >= 38.2 and l['e9'] > l['e21']: sg.append('t')

    # u: Stochastic bull momentum — %K crosses above 50, %K > %D (Momentum Continuation)
    if 'stoch_k' in l and 'stoch_d' in l and 'stoch_k' in p:
        if l['stoch_k'] > 50 and p['stoch_k'] <= 50 and l['stoch_k'] > l['stoch_d']:
            sg.append('u')

    # v: EMA200 reclaim — price crosses above EMA200 (Major Trend Structural Shift)
    if 'e200' in l and 'e200' in p:
        if l['close'] > l['e200'] and p['close'] <= p['e200']: sg.append('v')

    # w: OBV accumulation — OBV rising 3 bars while price flat/down (Volume Divergence)
    if 'obv' in df.columns and len(df) >= 4:
        obv_up3 = l['obv'] > p['obv'] > pp['obv']
        px_flat = l['close'] <= pp['close']
        if obv_up3 and px_flat: sg.append('w')

    # x: Triple confluence — HARD DISABLED (sub-27% WR, negative R)
    # if 'rsi' in l and 'macd_hist' in l and 'adx' in l and 'adx' in p:
    #     if l['rsi'] > 50 and l['macd_hist'] > 0 and l['adx'] > 20 and l['adx'] > p['adx']:
    #         sg.append('x')

    # y: BB mid reclaim — price crosses above BB mid on green candle (Mean Reversion Continuation)
    if 'bb_mid' in l and 'bb_mid' in p:
        if l['close'] > l['bb_mid'] and p['close'] <= p['bb_mid'] and l['close'] > l.get('open', 0):
            sg.append('y')

    # z: Volume-confirmed trend — EMA9>21 + vol>1.5x avg + higher high (Trend+Volume Confluence)
    if 'e9' in l and 'e21' in l and 'vol' in l and len(df) >= 11 and 'high' in l and 'high' in p:
        vol_avg10 = df['vol'].iloc[-11:-1].mean()
        if l['e9'] > l['e21'] and vol_avg10 > 0 and l['vol'] > vol_avg10 * 1.5 and l['high'] > p['high']:
            sg.append('z')

    # 2: Chop contraction + MFI — chop falling in transition zone, MFI>50 (Regime+Flow)
    if 'chop' in l and 'chop' in p and 'mfi' in l:
        chop_fall = l['chop'] < p['chop']
        chop_mid = 38.2 <= l['chop'] <= 61.8
        if chop_fall and chop_mid and l['mfi'] > 50: sg.append('2')

    # EMA200 filter moved to soft confidence penalty in entry gate chain
    return sg


def _sigs_short(df):
    """27 SHORT signals across 13 dimensions (a-z,2). Bearish inverses of long signals."""
    if len(df) < 4: return []
    l, p = df.iloc[-1], df.iloc[-2]
    pp = df.iloc[-3]
    sg = []
    # a: EMA9/21 cross DOWN
    if 'e9' in l and 'e21' in l and 'e9' in p and 'e21' in p:
        if l['e9'] < l['e21'] and p['e9'] >= p['e21']: sg.append('a')
    # c: RSI>70 falling (overbought rejection)
    if 'rsi' in l and 'rsi' in p and 'rsi' in pp:
        if l['rsi'] > 70 and l['rsi'] < p['rsi'] and p['rsi'] >= pp['rsi']: sg.append('c')
    # d: BB upper rejection + red candle
    if 'bb_lower' in l and 'bb_upper' in l:
        if l['close'] >= l['bb_upper'] and l['close'] < p['close']: sg.append('d')
    # e: EMA pullback sell in downtrend
    if 'e9' in l and 'e21' in l and 'e200' in l:
        in_downtrend = l['e9'] < l['e21'] < l['e200']
        near_e21 = abs(l['close'] - l['e21']) / (l['e21'] + 1e-10) < 0.015
        rejecting = l['close'] < p['close']
        if in_downtrend and near_e21 and rejecting: sg.append('e')
    # f: MACD hist positive but falling 3 bars
    if 'macd_hist' in l and 'macd_hist' in p and 'macd_hist' in pp:
        if l['macd_hist'] > 0 and l['macd_hist'] < p['macd_hist'] < pp['macd_hist']:
            sg.append('f')
    # g: RSI crosses below 50
    if 'rsi' in l and 'rsi' in p:
        if l['rsi'] < 50 and p['rsi'] > 50: sg.append('g')
    # h: BB %B crosses below 0.8
    if 'bb_pctb' in l and 'bb_pctb' in p:
        if l['bb_pctb'] < 0.8 and p['bb_pctb'] > 0.8: sg.append('h')
    # k: BB squeeze + breakdown — HARD DISABLED
    # if 'bb_width' in l and len(df) >= 20:
    #     avg_bw = df['bb_width'].iloc[-20:].mean()
    #     if avg_bw > 0:
    #         squeeze_bars = sum(1 for x in df['bb_width'].iloc[-5:] if x < avg_bw * 0.6)
    #         if squeeze_bars >= 3 and l['close'] < l.get('bb_lower', l['close']):
    #             sg.append('k')
    # l: MACD hist bear divergence
    if len(df) >= 20 and 'macd_hist' in l:
        mh_d = _macd_hist_divergence(df, lookback=20)
        if mh_d == 'bear': sg.append('l')
    # n: Volume surge on RED candle
    if 'vol' in l and len(df) >= 11:
        vol_avg10 = df['vol'].iloc[-11:-1].mean()
        if vol_avg10 > 0 and l['vol'] > vol_avg10 * 2 and l['close'] < p['close']:
            sg.append('n')
    # o: ADX trend strength — HARD DISABLED
    # if 'adx' in l and 'adx' in p and 'e9' in l and 'e21' in l:
    #     if l['adx'] > 25 and l['adx'] > p['adx'] and l['e9'] < l['e21']:
    #         sg.append('o')
    # p: Stoch overbought — %K>80 crosses below %D
    if 'stoch_k' in l and 'stoch_d' in l and 'stoch_k' in p and 'stoch_d' in p:
        if l['stoch_k'] > 80 and l['stoch_k'] < l['stoch_d'] and p['stoch_k'] >= p['stoch_d']:
            sg.append('p')
    # r: MACD crosses below zero — HARD DISABLED
    # if 'macd' in l and 'macd' in p:
    #     if l['macd'] < 0 and p['macd'] >= 0: sg.append('r')
    # s: ATR expansion — HARD DISABLED
    # if 'atr' in l and len(df) >= 15:
    #     atr_ma14 = df['atr'].iloc[-15:-1].mean()
    #     if atr_ma14 > 0 and l['atr'] > atr_ma14 * 1.3 and l['close'] < p['close']:
    #         sg.append('s')
    # b: Bearish engulfing at rally
    if 'open' in l and 'open' in p:
        prev_green = p['close'] > p['open']
        curr_red = l['close'] < l['open']
        engulfs = l['open'] >= p['close'] and l['close'] <= p['open']
        after_rally = p['close'] > p.get('e9', p['close'])
        if prev_green and curr_red and engulfs and after_rally: sg.append('b')
    # i: MFI crosses below 80 (overbought rejection)
    if 'mfi' in l and 'mfi' in p:
        if l['mfi'] < 80 and p['mfi'] >= 80: sg.append('i')
    # j: OBV crosses below 20-bar SMA — HARD DISABLED
    # if 'obv' in df.columns and len(df) >= 21:
    #     obv_ma20 = df['obv'].iloc[-21:-1].mean()
    #     if l['obv'] < obv_ma20 and p['obv'] >= obv_ma20: sg.append('j')
    # m: MFI crosses below 50 — HARD DISABLED
    # if 'mfi' in l and 'mfi' in p:
    #     if l['mfi'] < 50 and p['mfi'] >= 50: sg.append('m')
    # q: Shooting star near resistance
    if 'high' in l and 'low' in l and 'open' in l:
        body = abs(l['close'] - l['open'])
        lwick = min(l['close'], l['open']) - l['low']
        uwick = l['high'] - max(l['close'], l['open'])
        cr = l['high'] - l['low']
        if cr > 0 and body > 0:
            shooting = uwick >= 2.0 * body and lwick <= body * 0.5 and l['close'] < l['open']
            near_res = l['close'] > l.get('e21', l['close'] * 0.99)
            if shooting and near_res: sg.append('q')
    # t: Chop exit in downtrend
    if 'chop' in l and 'chop' in p and 'e9' in l and 'e21' in l:
        if l['chop'] < 38.2 and p['chop'] >= 38.2 and l['e9'] < l['e21']: sg.append('t')
    # u: Stoch %K crosses below 50, %K < %D
    if 'stoch_k' in l and 'stoch_d' in l and 'stoch_k' in p:
        if l['stoch_k'] < 50 and p['stoch_k'] >= 50 and l['stoch_k'] < l['stoch_d']:
            sg.append('u')
    # v: Price crosses below EMA200
    if 'e200' in l and 'e200' in p:
        if l['close'] < l['e200'] and p['close'] >= p['e200']: sg.append('v')
    # w: OBV distribution — falling 3 bars while price flat/up
    if 'obv' in df.columns and len(df) >= 4:
        obv_down3 = l['obv'] < p['obv'] < pp['obv']
        px_flat = l['close'] >= pp['close']
        if obv_down3 and px_flat: sg.append('w')
    # x: Triple confluence short — HARD DISABLED
    # if 'rsi' in l and 'macd_hist' in l and 'adx' in l and 'adx' in p:
    #     if l['rsi'] < 50 and l['macd_hist'] < 0 and l['adx'] > 20 and l['adx'] > p['adx']:
    #         sg.append('x')
    # y: Price crosses below BB mid on red candle
    if 'bb_mid' in l and 'bb_mid' in p:
        if l['close'] < l['bb_mid'] and p['close'] >= p['bb_mid'] and l['close'] < l.get('open', float('inf')):
            sg.append('y')
    # z: Volume-confirmed downtrend — EMA9<21 + vol>1.5x + lower low
    if 'e9' in l and 'e21' in l and 'vol' in l and len(df) >= 11 and 'low' in l and 'low' in p:
        vol_avg10 = df['vol'].iloc[-11:-1].mean()
        if l['e9'] < l['e21'] and vol_avg10 > 0 and l['vol'] > vol_avg10 * 1.5 and l['low'] < p['low']:
            sg.append('z')
    # 2: Chop contraction + MFI<50
    if 'chop' in l and 'chop' in p and 'mfi' in l:
        chop_fall = l['chop'] < p['chop']
        chop_mid = 38.2 <= l['chop'] <= 61.8
        if chop_fall and chop_mid and l['mfi'] < 50: sg.append('2')
    return sg


def _find_swing_lows(series, order=2):
    """Find indices of local minima (lower than `order` neighbors on each side)."""
    lows = []
    for i in range(order, len(series) - order):
        if all(series.iloc[i] <= series.iloc[i-j] for j in range(1, order+1)) and \
           all(series.iloc[i] <= series.iloc[i+j] for j in range(1, order+1)):
            lows.append(i)
    return lows

def _find_swing_highs(series, order=2):
    """Find indices of local maxima (higher than `order` neighbors on each side)."""
    highs = []
    for i in range(order, len(series) - order):
        if all(series.iloc[i] >= series.iloc[i-j] for j in range(1, order+1)) and \
           all(series.iloc[i] >= series.iloc[i+j] for j in range(1, order+1)):
            highs.append(i)
    return highs

def _obv_divergence(df, lookback=10):
    """OBV divergence: compare two most recent swing lows/highs. Returns 'bull', 'bear', or None."""
    s = df.iloc[-lookback:]
    if len(s) < lookback or 'obv' not in s.columns: return None
    # Bullish: price makes lower low, OBV makes higher low
    p_lows = _find_swing_lows(s['close'])
    if len(p_lows) >= 2:
        i1, i2 = p_lows[-2], p_lows[-1]
        if s['close'].iloc[i2] < s['close'].iloc[i1]:
            o_lows = _find_swing_lows(s['obv'])
            if len(o_lows) >= 2:
                j1, j2 = o_lows[-2], o_lows[-1]
                if s['obv'].iloc[j2] > s['obv'].iloc[j1]:
                    return 'bull'
    # Bearish: price makes higher high, OBV makes lower high
    p_highs = _find_swing_highs(s['close'])
    if len(p_highs) >= 2:
        i1, i2 = p_highs[-2], p_highs[-1]
        if s['close'].iloc[i2] > s['close'].iloc[i1]:
            o_highs = _find_swing_highs(s['obv'])
            if len(o_highs) >= 2:
                j1, j2 = o_highs[-2], o_highs[-1]
                if s['obv'].iloc[j2] < s['obv'].iloc[j1]:
                    return 'bear'
    return None


def _macd_hist_divergence(df, lookback=10):
    """MACD hist divergence: compare two most recent swing lows/highs. Returns 'bull', 'bear', or None."""
    s = df.iloc[-lookback:]
    if len(s) < lookback or 'macd_hist' not in s.columns: return None
    # Bullish: price makes lower low, MACD hist makes higher trough
    p_lows = _find_swing_lows(s['close'])
    if len(p_lows) >= 2:
        i1, i2 = p_lows[-2], p_lows[-1]
        if s['close'].iloc[i2] < s['close'].iloc[i1]:
            h_lows = _find_swing_lows(s['macd_hist'])
            if len(h_lows) >= 2:
                j1, j2 = h_lows[-2], h_lows[-1]
                if s['macd_hist'].iloc[j2] > s['macd_hist'].iloc[j1]:
                    return 'bull'
    # Bearish: price makes higher high, MACD hist makes lower peak
    p_highs = _find_swing_highs(s['close'])
    if len(p_highs) >= 2:
        i1, i2 = p_highs[-2], p_highs[-1]
        if s['close'].iloc[i2] > s['close'].iloc[i1]:
            h_highs = _find_swing_highs(s['macd_hist'])
            if len(h_highs) >= 2:
                j1, j2 = h_highs[-2], h_highs[-1]
                if s['macd_hist'].iloc[j2] < s['macd_hist'].iloc[j1]:
                    return 'bear'
    return None

_htf_cache = {}
_cache_lock = Lock()

_HTF_TTL = {'15m': 60, '1h': 300, '4h': 300, '1d': 3600}

def fetch_higher_timeframe(ex, symbol, timeframe='4h', limit=100):
    cache_key = f"{symbol}_{timeframe}"
    now = time.time()
    ttl = _HTF_TTL.get(timeframe, 300)
    if cache_key in _htf_cache:
        cached_time, cached_data = _htf_cache[cache_key]
        if now - cached_time < ttl:
            return cached_data
    try:
        o = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
        if o:
            df = pd.DataFrame(o, columns=['t','o','h','l','c','v']).rename(columns={'c':'close','h':'high','l':'low','v':'vol'})
            df = ind(df)
            _htf_cache[cache_key] = (now, df)
            return df
    except Exception as e:
        logging.warning(f"HTF fetch failed for {symbol}: {e}")
    return None

def _tf_trend_score(df):
    """Compute trend score in [0,1] for a single timeframe DataFrame."""
    if df is None or len(df) < 20:
        return 0.5
    l = df.iloc[-1]
    e9 = _safe(l.get('e9'), 0)
    e21 = _safe(l.get('e21'), 0)
    rsi = _safe(l.get('rsi'), 50)
    mh = _safe(l.get('macd_hist'), 0)
    # EMA alignment: e9 vs e21
    ema_sep = (e9 - e21) / (e21 + 1e-9) if e21 > 0 else 0
    ema_s = max(0, min(ema_sep / 0.02 * 0.5 + 0.5, 1.0))
    # RSI position relative to 50
    rsi_s = rsi / 100.0
    # MACD histogram direction
    macd_s = 0.5
    if len(df) >= 3 and 'macd_hist' in df.columns:
        h3 = df['macd_hist'].iloc[-3:].dropna().values
        if len(h3) >= 2:
            macd_s = 0.7 if h3[-1] > h3[-2] else 0.3
    return round(ema_s * 0.45 + rsi_s * 0.30 + macd_s * 0.25, 4)

def get_htf_bias(ex, symbol):
    """Backward-compatible wrapper — returns MTF confluence score."""
    return compute_mtf_confluence(ex, symbol)

def compute_mtf_confluence(ex, symbol, df_1h=None):
    """Multi-timeframe confluence score in [0,1]. Combines 15m/1h/4h/1d trend alignment.
    Returns weighted average: 15m(0.15) + 1h(0.35) + 4h(0.30) + 1d(0.20).
    """
    scores = {}
    weights = {'15m': 0.15, '1h': 0.35, '4h': 0.30, '1d': 0.20}

    # 15m — entry precision
    df_15m = fetch_higher_timeframe(ex, symbol, '15m', limit=100)
    scores['15m'] = _tf_trend_score(df_15m)

    # 1h — primary (use provided df or fetch)
    if df_1h is not None and len(df_1h) >= 20:
        scores['1h'] = _tf_trend_score(df_1h)
    else:
        df_1h_fetched = fetch_higher_timeframe(ex, symbol, '1h', limit=100)
        scores['1h'] = _tf_trend_score(df_1h_fetched)

    # 4h — trend confirmation
    df_4h = fetch_higher_timeframe(ex, symbol, '4h', limit=100)
    scores['4h'] = _tf_trend_score(df_4h)

    # 1d — structural context
    df_1d = fetch_higher_timeframe(ex, symbol, '1d', limit=100)
    scores['1d'] = _tf_trend_score(df_1d)

    mtf = sum(scores[tf] * weights[tf] for tf in weights)
    return round(mtf, 4)

_sentiment_cache = {}
_sentiment_ttl = 300

_of_cache = {}   # {symbol: (timestamp, score)}
_of_ttl = 10     # seconds

def compute_order_flow_factor(ex, symbol, depth=10):
    """Order flow factor in [0,1]. 0.5=neutral. Cached 10s."""
    now = time.time()
    cached = _of_cache.get(symbol)
    if cached and now - cached[0] < _of_ttl:
        return cached[1]
    try:
        ob = ex.fetch_order_book(symbol, limit=depth)
        bids, asks = ob.get('bids', []), ob.get('asks', [])
        if len(bids) < 2 or len(asks) < 2:
            return 0.5
        w = [0.9**i for i in range(min(depth, len(bids), len(asks)))]
        bid_w = sum(bids[i][1] * w[i] for i in range(len(w)))
        ask_w = sum(asks[i][1] * w[i] for i in range(len(w)))
        obi = (bid_w - ask_w) / (bid_w + ask_w + 1e-9)
        obi_norm = (obi + 1) / 2
        best_bid, best_ask = bids[0][0], asks[0][0]
        mid = (best_bid + best_ask) / 2
        spread_bps = (best_ask - best_bid) / (mid + 1e-9) * 10000
        spread_norm = 1.0 - min(spread_bps / 20.0, 1.0)
        score = round(max(0.0, min(obi_norm * 0.70 + spread_norm * 0.30, 1.0)), 4)
        _of_cache[symbol] = (now, score)
        return score
    except Exception:
        return 0.5

# Funding rate — derivatives positioning signal (Kraken Futures, no auth needed)
_kf_exchange = None
_fr_cache = {}  # {symbol: (timestamp, rate)}
_fr_ttl = 300   # 5 min cache — funding rates update slowly

def get_funding_rate(symbol):
    """Fetch perpetual funding rate. Cached 5 min. Returns float or None."""
    global _kf_exchange
    now = time.time()
    cached = _fr_cache.get(symbol)
    if cached and now - cached[0] < _fr_ttl:
        return cached[1]
    try:
        if _kf_exchange is None:
            _kf_exchange = ccxt.krakenfutures()
        base = symbol.split('/')[0]
        perp = f"{base}/USD:USD"
        fr = _kf_exchange.fetch_funding_rate(perp)
        rate = fr.get('fundingRate', 0.0)
        _fr_cache[symbol] = (now, rate)
        return rate
    except Exception:
        return None

# Whale score — from Whale Watcher (D:\Whale Watcher\apex_whale_finder.dir)
_whale_engine = None
_whale_cache = {}  # {symbol: (timestamp, score)}
_whale_ttl = 120   # 2 min cache — whale scores change slowly

def get_whale_score(symbol):
    """Get whale activity score (0-100) for symbol. Cached 2 min."""
    global _whale_engine
    now = time.time()
    cached = _whale_cache.get(symbol)
    if cached and now - cached[0] < _whale_ttl:
        return cached[1]
    try:
        if _whale_engine is None:
            # Prefer vendored copy at D:\GoldenEye\whale_indicators.py (standalone).
            # Fall back to sibling fleet bot's copy if vendored file was removed.
            try:
                from whale_indicators import IndicatorsEngine
                logging.info("Whale indicators loaded (vendored)")
            except ImportError:
                import sys
                _ww_path = os.path.join('D:', os.sep, 'Whale Watcher', 'apex_whale_finder.dir')
                if _ww_path not in sys.path:
                    sys.path.insert(0, _ww_path)
                from indicators import IndicatorsEngine
                logging.warning(f"Whale indicators loaded from fleet fallback path ({_ww_path}) — vendor whale_indicators.py into D:\\GoldenEye to make standalone")
            _whale_engine = IndicatorsEngine()
            _whale_engine._use_numba = False  # avoid threading issues
        # Fetch 1h OHLCV and compute whale indicators
        df = fetch_higher_timeframe(_exchange, symbol, '1h', limit=50)
        if df is None or len(df) < 30:
            return None
        # Map columns for Whale Watcher (expects 'high', 'low', 'close', 'volume')
        wdf = df[['high', 'low', 'close', 'vol']].copy().rename(columns={'vol': 'volume'})
        wdf = _whale_engine.add_indicators(wdf)
        if 'whale_score' in wdf.columns:
            ws = float(wdf['whale_score'].iloc[-1])
            if not pd.isna(ws):
                _whale_cache[symbol] = (now, ws)
                return ws
    except Exception as e:
        logging.warning(f"Whale score failed for {symbol}: {e}")
    return None

_twitter_bearer = os.getenv('TWITTER_BEARER_TOKEN')

_twitter_search_terms = {
    'BTC/USD': ['#BTC', 'Bitcoin', 'btc'],
    'ETH/USD': ['#ETH', 'Ethereum', 'eth'],
    'SOL/USD': ['#SOL', 'Solana', 'sol'],
    'XRP/USD': ['#XRP', 'Ripple', 'xrp'],
    'ADA/USD': ['#ADA', 'Cardano', 'ada'],
    'DOGE/USD': ['#DOGE', 'Dogecoin', 'doge'],
    'AVAX/USD': ['#AVAX', 'Avalanche', 'avax'],
    'LINK/USD': ['#LINK', 'Chainlink', 'link'],
    'DOT/USD': ['#DOT', 'Polkadot', 'dot'],
    'LTC/USD': ['#LTC', 'Litecoin', 'ltc'],
}

_positive_words = {'bullish', 'buy', 'pump', 'moon', 'up', 'gain', 'profit', 'breakout', 'rally', 'surge', 'high', 'growth', 'positive', 'optimistic', 'long'}
_negative_words = {'bearish', 'sell', 'dump', 'crash', 'down', 'loss', 'breakdown', 'drop', 'selloff', 'low', 'decline', 'negative', 'pessimistic', 'short', 'hack', 'scam', 'warning'}

def analyze_sentiment(text: str) -> float:
    text_lower = text.lower()
    pos_count = sum(1 for word in _positive_words if word in text_lower)
    neg_count = sum(1 for word in _negative_words if word in text_lower)
    total = pos_count + neg_count
    if total == 0:
        return 0.0
    return (pos_count - neg_count) / total

def get_twitter_sentiment(symbol: str) -> dict:
    global _sentiment_cache
    cache_key = f"sentiment_{symbol}"
    now = time.time()

    if cache_key in _sentiment_cache:
        cached_time, cached_data = _sentiment_cache[cache_key]
        if now - cached_time < _sentiment_ttl:
            return cached_data

    if not _twitter_bearer:
        return {'score': 0.0, 'sentiment': 'neutral', 'tweets': 0}

    search_terms = _twitter_search_terms.get(symbol, [f"#{symbol.replace('/', '')}"])
    query = ' OR '.join(search_terms[:3])

    try:
        url = "https://api.twitter.com/2/tweets/search/recent"
        params = {
            'query': f"{query} -is:retweet lang:en",
            'max_results': 20,
            'tweet.fields': 'public_metrics,created_at'
        }
        headers = {'Authorization': f"Bearer {_twitter_bearer}"}
        r = requests.get(url, params=params, headers=headers, timeout=10)

        if r.status_code == 200:
            data = r.json()
            tweets = data.get('data', [])
            if tweets:
                sentiments = [analyze_sentiment(t.get('text', '')) for t in tweets]
                avg_sentiment = sum(sentiments) / len(sentiments)

                engagement_scores = []
                for t in tweets:
                    metrics = t.get('public_metrics', {})
                    score = metrics.get('like_count', 0) + metrics.get('retweet_count', 0) * 2
                    engagement_scores.append(score)

                total_engagement = sum(engagement_scores)
                weighted_sentiment = sum(s * e for s, e in zip(sentiments, engagement_scores)) / total_engagement if total_engagement > 0 else avg_sentiment

                result = {
                    'score': weighted_sentiment,
                    'sentiment': 'positive' if weighted_sentiment > 0.2 else 'negative' if weighted_sentiment < -0.2 else 'neutral',
                    'tweets': len(tweets),
                    'engagement': sum(engagement_scores)
                }
                _sentiment_cache[cache_key] = (now, result)
                return result
    except Exception as e:
        logging.warning(f"Twitter sentiment fetch failed for {symbol}: {e}")

    return {'score': 0.0, 'sentiment': 'neutral', 'tweets': 0}

def use_sentiment_filter(symbol: str, allow_negative=True) -> bool:
    sentiment = get_twitter_sentiment(symbol)
    score = sentiment.get('score', 0)

    if not allow_negative and score < -0.1:
        logging.info(f"{symbol} filtered: negative sentiment {score:.2f}")
        return False

    # Long blocks very negative, short blocks very positive
    if (_MODE == 'long' and score < -0.3) or (_MODE == 'short' and score > 0.3):
        logging.info(f"{symbol} filtered: sentiment {score:.2f} ({_MODE} mode)")
        return False

    return True

# Top symbols
def top_sym(n=20):
    """Pick top-N tradeable pairs by USD volume directly from Kraken.

    GoldenEye standalone — queries Kraken ticker API for all USD pairs,
    ranks by 24h volume, returns the top N. No CC, no CoinGecko.
    """
    global _exchange
    stables = {'usdt','usdc','busd','dai','tusd','usdp','gusd','fdusd',
               'usd1','pyusd','frax','lusd','susd','crvusd','mkusd',
               'usdd','eurs','eurt','usde','usdj','tribe','fei',
               'xaut','paxg','eur','gbp','aud','cad','chf','jpy',
               'usdg','rlusd'}

    try:
        if not _exchange or not _exchange.markets:
            raise ValueError("exchange not loaded yet")
        # Get all active USD spot pairs from Kraken
        usd_pairs = [s for s, m in _exchange.markets.items()
                     if m.get('active') and m.get('spot')
                     and s.endswith('/USD')
                     and s.split('/')[0].lower() not in stables]
        # Fetch 24h tickers and rank by quote volume
        tickers = _exchange.fetch_tickers(usd_pairs)
        ranked = sorted(usd_pairs,
                        key=lambda p: float(tickers.get(p, {}).get('quoteVolume', 0) or 0),
                        reverse=True)
        if ranked:
            logging.info(f"top_sym: Kraken ticker ranked {len(ranked)} USD pairs, picking top {n}")
            return ranked[:n]
    except Exception as e:
        logging.warning(f"top_sym: Kraken ticker ranking failed ({e}), using fallback")

    # Last-resort fallback
    fallback = ['BTC','ETH','SOL','XRP','ADA','DOGE','AVAX','LINK','DOT','LTC',
                'MATIC','ATOM','UNI','FIL','NEAR','APT','ARB','OP','SUI','PEPE']
    v = []
    for bs in fallback:
        for q in ['USD','USDT']:
            p = f"{bs}/{q}"
            if _exchange and p in _exchange.markets and _exchange.markets[p].get('active'):
                v.append(p)
                break
    return v[:n] or ['BTC/USD','ETH/USD','XRP/USD']

# Backtest
def bt(sym, d, br, ex):
    fee = get_fee(sym)
    s = int((datetime.now(timezone.utc)-timedelta(days=d)).timestamp()*1000)
    o = ex.fetch_ohlcv(sym, '1h', since=s, limit=2000)
    if len(o) < 100: return

    df = pd.DataFrame(o, columns=['t','o','h','l','c','v']).rename(columns={'c':'close','h':'high','l':'low','v':'vol'})
    df = ind(df).dropna()
    _bt_wins = 0; _bt_total = 0; _bt_pnl = 0.0; _bt_cooldown = 0

    for i in range(50, len(df)):
        if i < _bt_cooldown: continue  # respect cooldown after trade
        sg = sigs(df.iloc[:i+1])
        if len(sg) < 2: continue  # gate: require 2+ signals (matches live)
        bt_atr = float(df.iloc[i].get('atr_pct', 0.02)) if 'atr_pct' in df.columns else 0.02
        bt_rgm = regime(df.iloc[:i+1])
        bt_factors = compute_factors(df.iloc[:i+1], float(df.iloc[i]['close']), None, None, 0.5, bt_rgm, of_score=0.5, short_mode=(_MODE == 'short'))
        confidence = compute_confidence(bt_factors, bt_rgm, brain=br)

        # Gate: regime multiplier (matches live)
        rgm_mult = _RGM_MULT.get(bt_rgm, {}).get(_MODE, 0.5)
        if rgm_mult < 0.1: continue

        # Gate: confidence threshold (matches live)
        min_conf = _RGM_CONF_MIN.get(bt_rgm, 0.55)
        if confidence < min_conf: continue

        e = float(df.iloc[i]['close']) * (1 + _DIR * float(_PAPER_SLIP))  # entry slippage
        # ATR-based SL/TP matching live system
        atr_sl = min(max(bt_atr * 1.2, 0.005), 0.05)
        atr_tp = atr_sl
        # Dynamic TP scaling (matches live)
        if len(df.iloc[:i+1]) >= 50 and 'atr' in df.columns:
            _atr_med = float(df['atr'].iloc[:i+1].rolling(50).median().iloc[-1])
            _atr_now = float(df['atr'].iloc[i])
            if _atr_med > 0 and not pd.isna(_atr_med) and not pd.isna(_atr_now):
                _tp_scale = max(0.8, min(1.0 / (_atr_now / _atr_med), 1.5))
            else:
                _tp_scale = 1.0
        else:
            _tp_scale = 1.0
        sl_price = e * (1 + (-_DIR) * atr_sl)  # long: below entry, short: above entry
        _bt_tp1m, _bt_tp2m, _bt_tp3m = _get_tp_mults(bt_rgm)
        tp1 = e + _DIR * e * atr_tp * float(_bt_tp1m) * _tp_scale
        tp2 = e + _DIR * e * atr_tp * float(_bt_tp2m) * _tp_scale
        tp3 = e + _DIR * e * atr_tp * float(_bt_tp3m) * _tp_scale
        risk_dist = abs(e - sl_price)

        # Simulate multi-bar exit with TP1/2/3, SL movement, trailing, time exit
        tp_hit = 0; exit_price = sl_price; exit_bar = min(i+96, len(df)-1)
        cur_sl = sl_price
        trail_active = False
        for j in range(i+1, min(i+96, len(df))):
            h, l, c = float(df.iloc[j]['high']), float(df.iloc[j]['low']), float(df.iloc[j]['close'])
            ema21 = float(df.iloc[j].get('e21', c)) if 'e21' in df.columns else c
            atr_j = float(df.iloc[j].get('atr', e * 0.02)) if 'atr' in df.columns else e * 0.02
            # Check SL (long: low <= sl, short: high >= sl)
            _sl_hit = (l <= cur_sl) if _MODE == 'long' else (h >= cur_sl)
            if _sl_hit:
                exit_price = cur_sl; exit_bar = j; break
            # UPGRADE E: Volatility-adjusted dynamic TPs in backtest
            # Expand TP2/TP3 when current ATR exceeds entry ATR (trending markets)
            if 'atr' in df.columns:
                _bt_cur_atr = float(df.iloc[j].get('atr', e * 0.02))
                _bt_entry_atr = e * bt_atr  # entry ATR in price terms
                if _bt_entry_atr > 0 and _bt_cur_atr > _bt_entry_atr * 1.1:
                    _bt_atr_ratio = min(_bt_cur_atr / _bt_entry_atr, 2.0)
                    _bt_dyn_tp2 = e + _DIR * _bt_cur_atr * 1.2 * float(_bt_tp2m)
                    _bt_dyn_tp3 = e + _DIR * _bt_cur_atr * 1.2 * float(_bt_tp3m)
                    # Only ratchet in favorable direction
                    if _MODE == 'long':
                        tp2 = max(tp2, _bt_dyn_tp2)
                        tp3 = max(tp3, _bt_dyn_tp3)
                    else:
                        tp2 = min(tp2, _bt_dyn_tp2)
                        tp3 = min(tp3, _bt_dyn_tp3)

            # Check TP levels (long: high >= tp, short: low <= tp)
            _tp1_hit = (h >= tp1) if _MODE == 'long' else (l <= tp1)
            _tp2_hit = (h >= tp2) if _MODE == 'long' else (l <= tp2)
            _tp3_hit = (h >= tp3) if _MODE == 'long' else (l <= tp3)
            if tp_hit < 1 and _tp1_hit:
                tp_hit = 1
                cur_sl = e + _DIR * atr_j * 0.5  # move SL to entry+0.5*ATR in trade direction
            if tp_hit < 2 and _tp2_hit:
                tp_hit = 2; trail_active = True
            if tp_hit < 3 and _tp3_hit:
                tp_hit = 3; exit_price = tp3; exit_bar = j; break
            # Keltner trailing after TP2
            if trail_active:
                kc_mult = 1.5 if tp_hit >= 3 else 2.5
                floor = e + _DIR * atr_j * 0.5
                if _MODE == 'long':
                    trail = max(ema21 - kc_mult * atr_j, floor)
                    if trail > cur_sl: cur_sl = trail
                else:
                    trail = min(ema21 + kc_mult * atr_j, floor)
                    if trail < cur_sl: cur_sl = trail
            # Factor decay exit (after 12 bars, pre-TP1)
            if tp_hit == 0 and (j - i) > 12:
                cur_f = compute_factors(df.iloc[:j+1], c, None, None, 0.5, bt_rgm, of_score=0.5, short_mode=(_MODE == 'short'))
                cur_c = compute_confidence(cur_f, bt_rgm, brain=br)
                if confidence > 0 and cur_c < confidence * 0.5:
                    exit_price = c; exit_bar = j; break
            # Exhaustion after TP1
            if tp_hit >= 1 and is_exhausted(df.iloc[:j+1], _MODE):
                exit_price = c; exit_bar = j; break
        else:
            exit_price = float(df.iloc[exit_bar]['close'])  # time exit

        # Apply exit slippage (adverse direction)
        exit_price *= (1 - _DIR * float(_PAPER_SLIP))
        # Continuous R-multiple
        r = _DIR * (exit_price - e) / risk_dist if risk_dist > 0 else (-1 if tp_hit == 0 else tp_hit)
        r -= 2 * float(fee)  # entry + exit fee
        _bt_total += 1
        if r >= 0: _bt_wins += 1
        _bt_pnl += r * float(_TRADE_AMT) * rgm_mult  # dollar PnL = R * base trade * regime mult

        # br.record(sg, r, Decimal('0.001'), direction=_MODE, factors=bt_factors, rgm=bt_rgm)  # NEUTERED: backtest recording poisons brain with non-live trades
        _bt_cooldown = exit_bar + 1  # 1-bar cooldown (matches live _COOLDOWN_SECS=1800s < 1h bar)

    wr = (_bt_wins / _bt_total * 100) if _bt_total > 0 else 0
    print(f"{sym} bt — {_bt_total} trades, WR={wr:.1f}%, P/L=${_bt_pnl:+,.0f}")

def run_backtest_report(syms, brs_map):
    """Generate report from already-run backtest brains. Pass brs dict from main()."""
    total_trades = 0
    total_wins = 0
    for sym in syms:
        br = brs_map.get(sym)
        if not br: continue
        tf = sum(d['f'] for d in br.stats.values())
        tw = sum(d['w'] for d in br.stats.values())
        total_trades += tf
        total_wins += tw

    win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0

    # Per-signal breakdown
    sig_stats = {}
    for sig in AdaptiveBrain.SIG:
        f = sum(brs_map[sym].stats[sig]['f'] for sym in syms if sym in brs_map)
        w = sum(brs_map[sym].stats[sig]['w'] for sym in syms if sym in brs_map)
        if f > 0:
            sig_stats[sig] = {'f': f, 'w': w, 'wr': w/f*100}

    print("\n" + "="*50)
    print("BACKTEST REPORT")
    print("="*50)
    print(f"Total Trades: {total_trades}")
    print(f"Total Wins:   {total_wins}")
    print(f"Win Rate:     {win_rate:.1f}%")
    if sig_stats:
        print(f"\nPer-Signal Breakdown:")
        for sig in AdaptiveBrain.SIG:
            if sig in sig_stats:
                s = sig_stats[sig]
                print(f"  {sig.upper()} {_SIG_DESC.get(sig, ''):20s}  {s['w']}/{s['f']}  WR={s['wr']:.0f}%")
    print("="*50)

def send_alert(message: str):
    try:
        import requests
        webhook_url = os.getenv('DISCORD_WEBHOOK_URL')
        if webhook_url:
            requests.post(webhook_url, json={'content': message}, timeout=5)
        telegram_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        telegram_chat = os.getenv('TELEGRAM_CHAT_ID', '')
        if telegram_token and telegram_chat:
            requests.post(f'https://api.telegram.org/bot{telegram_token}/sendMessage',
                         json={'chat_id': telegram_chat, 'text': message}, timeout=5)
    except Exception as e:
        logging.warning(f"Alert failed: {e}")

# =============================================================================
# Card Renderer — PNG signal cards for Telegram
# =============================================================================
try:
    from card_renderer import OracleCardRenderer, send_card_telegram
    _card_renderer = OracleCardRenderer()
    _CARDS_ENABLED = True
    logging.info("Card renderer loaded")
except Exception as _cr_err:
    _card_renderer = None
    _CARDS_ENABLED = False
    logging.warning(f"Card renderer not available: {_cr_err}")


def _log_telegram_message(channel: str, message: str):
    """Log outbound Telegram message to message store for dashboard."""
    from datetime import datetime, timezone
    _TELEGRAM_MESSAGES.appendleft({
        'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'channel': channel,
        'message': message
    })


def _format_duration(seconds):
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m" if m else f"{h}h"


# Voice lines — identical to dashboard.py, single source of truth
_VOICE_OPEN = [
    "Whatever it takes.",
    "Built honest, runs honest.",
    "27 signals. 7 gates. No shortcuts.",
]
_VOICE_WIN = [
    "The signals aligned. The brain learns. We move forward.",
    "Conviction pays. Oracle sees what the noise hides.",
    "Patience rewarded. The math holds.",
]
_VOICE_LOSS = [
    "Market gave, market took. We learn from this one.",
    "Stopped out. No signal is perfect \u2014 but the system adapts.",
    "Loss absorbed. Brain recalibrates. We go again.",
]
_VOICE_TP1 = [
    "First target secured. Letting the rest ride.",
    "TP1 hit. Risk is off the table now.",
]

def _pick_voice(pool, seed=None):
    """Pick a voice line from a pool, seeded for variety."""
    if seed is None:
        seed = int(time.time())
    return pool[abs(seed) % len(pool)]


def _iter_telegram_targets(signal_type: str):
    """Yield (sub_id, token, chat_id) for each active telegram channel whose
    subscriber has opted in for this signal_type ('long' | 'short' | 'exit').
    Sources auth from subscribers.json — env vars are intentionally ignored.
    """
    _load_subscribers()
    for sub_id, sub in _subscribers.items():
        if not sub.get('active', False):
            continue
        prefs = sub.get('preferences', {})
        if not prefs.get(signal_type, True):
            continue
        for channel in sub.get('channels', []):
            if channel.get('type') != 'telegram':
                continue
            token = channel.get('token', '')
            chat_id = channel.get('chat_id', '')
            if token and chat_id:
                yield sub_id, token, str(chat_id)


def _send_trade_card_open(sym, direction, entry, stop, tp1, tp2, tp3,
                          size, confidence, regime, signals, factors=None):
    """Render and send a POSITION OPENED card to Telegram."""
    if not _CARDS_ENABLED:
        return
    try:
        signal_type = 'short' if direction.lower() == 'short' else 'long'
        targets = list(_iter_telegram_targets(signal_type))
        if not targets:
            logging.info(f"No active telegram subscribers for {signal_type} — card not sent")
            return

        sig_bars = []
        for s in signals[:4]:
            name = _SIG_DESC.get(s, s.upper())
            sig_bars.append({'name': name, 'strength': 0.75})

        now = datetime.now(timezone.utc)

        data = {
            'pair': sym,
            'direction': direction.upper(),
            'entry': float(entry),
            'stop': float(stop),
            'targets': [float(tp1), float(tp2), float(tp3)],
            'size': float(size),
            'conviction': float(confidence) if confidence else 0.5,
            'regime': (regime or 'chop').upper(),
            'signals': sig_bars,
            'gates_passed': 7,
            'voice': _pick_voice(_VOICE_OPEN),
            'timestamp': now.strftime('%H:%M UTC'),
            'date': now.strftime('%d %b %Y'),
        }

        png = _card_renderer.render_position_opened(data)
        copy_vals = _card_renderer.copy_values_opened(data)
        for sub_id, token, chat_id in targets:
            try:
                send_card_telegram(token, chat_id, png, copy_vals)
                logging.info(f"Telegram OPEN card sent: {sub_id} {sym} {direction}")
                _log_telegram_message(f"Signal {sym}", f"POSITION OPENED: {sym} {direction.upper()} @ {float(entry):.4f}")
            except Exception as e:
                logging.warning(f"Telegram OPEN card failed for {sub_id}: {e}")
    except Exception as e:
        logging.warning(f"Card send (open) failed: {e}")


def _send_trade_card_close(sym, direction, pos, exit_price, pnl, r_mult,
                           tp_hit=0, exit_type='SL'):
    """Render and send a POSITION CLOSED card to Telegram."""
    if not _CARDS_ENABLED:
        return
    try:
        targets = list(_iter_telegram_targets('exit'))
        if not targets:
            logging.info(f"No active telegram subscribers for exit — card not sent")
            return

        entry = float(pos.get('e', 0))
        fee = float(pos.get('fee', 0.004))
        size = float(pos.get('filled', pos.get('size', 0)))
        fees = fee * size * (abs(entry) + abs(float(exit_price)))
        gross = float(pnl) + fees
        net = float(pnl)

        duration_s = time.time() - pos.get('opened_at', time.time())
        duration_str = _format_duration(duration_s)

        is_win = net >= 0
        if tp_hit > 0:
            exit_reason = f"TP{tp_hit} tagged"
        elif exit_type == 'SL':
            exit_reason = "Stop loss"
        else:
            exit_reason = exit_type

        with _metrics_lock:
            wins = _live_metrics.get('wins', 0)
            losses = _live_metrics.get('losses', 0)
            total_pnl = sum(_live_metrics.get('pnl_history', []))
        total_trades = wins + losses
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        if is_win:
            voice = _pick_voice(_VOICE_TP1 if tp_hit == 1 else _VOICE_WIN)
        else:
            voice = _pick_voice(_VOICE_LOSS)

        now = datetime.now(timezone.utc)

        data = {
            'pair': sym,
            'direction': direction.upper(),
            'entry': entry,
            'exit': float(exit_price),
            'r_multiple': float(r_mult),
            'gross_pnl': gross,
            'fees': fees,
            'net_pnl': net,
            'duration': duration_str,
            'exit_reason': exit_reason,
            'wins': wins,
            'losses': losses,
            'win_rate': win_rate,
            'total_net': total_pnl,
            'voice': voice,
            'timestamp': now.strftime('%H:%M UTC'),
            'date': now.strftime('%d %b %Y'),
        }

        png = _card_renderer.render_position_closed(data)
        copy_vals = _card_renderer.copy_values_closed(data)
        for sub_id, token, chat_id in targets:
            try:
                send_card_telegram(token, chat_id, png, copy_vals)
                logging.info(f"Telegram CLOSE card sent: {sub_id} {sym} {direction} net=${net:.2f}")
                _log_telegram_message(f"Signal {sym}", f"POSITION CLOSED: {sym} {direction.upper()} {exit_reason} P/L=${net:.2f}")
            except Exception as e:
                logging.warning(f"Telegram CLOSE card failed for {sub_id}: {e}")
    except Exception as e:
        logging.warning(f"Card send (close) failed: {e}")


# =============================================================================
# Subscriber Signal Delivery System
# =============================================================================
_subscribers = {}
_subscriber_file = "subscribers.json"
_api_key = None

def _load_subscribers():
    global _subscribers
    if os.path.exists(_subscriber_file):
        try:
            with open(_subscriber_file) as _f:
                raw = json.load(_f)
            # Filter out non-dict keys (like "_note")
            _subscribers = {k: v for k, v in raw.items() if isinstance(v, dict)}
            # Resolve environment variables in tokens for security
            for sub in _subscribers.values():
                if not isinstance(sub, dict):
                    continue
                for channel in sub.get('channels', []):
                    if not isinstance(channel, dict):
                        continue
                    if channel.get('type') == 'telegram':
                        token = channel.get('token', '')
                        if token.startswith('${') and token.endswith('}'):
                            env_var = token[2:-1]
                            resolved = os.environ.get(env_var, '')
                            if resolved:
                                channel['token'] = resolved
                            else:
                                logging.warning(f"Telegram token env var {env_var} not set — channel disabled")
                                channel['token'] = ''
        except Exception:
            _subscribers = {}
    else:
        _subscribers = {}

def _save_subscribers():
    with open(_subscriber_file, 'w') as f:
        json.dump(_subscribers, f, indent=2)

def _generate_api_key():
    import secrets
    return secrets.token_urlsafe(32)

def register_subscriber(sub_id: str, channels: list, preferences: dict = None) -> str:
    _load_subscribers()
    api_key = _generate_api_key()
    _subscribers[sub_id] = {
        'api_key': api_key,
        'channels': channels,
        'preferences': preferences or {'long': True, 'short': True, 'exit': True},
        'created_at': datetime.now(timezone.utc).isoformat() + "Z",
        'active': True
    }
    _save_subscribers()
    logging.info(f"Subscriber registered: {sub_id}")
    return api_key

def remove_subscriber(sub_id: str):
    _load_subscribers()
    if sub_id in _subscribers:
        del _subscribers[sub_id]
        _save_subscribers()
        logging.info(f"Subscriber removed: {sub_id}")

def verify_api_key(sub_id: str, api_key: str) -> bool:
    _load_subscribers()
    sub = _subscribers.get(sub_id)
    return sub and sub.get('api_key') == api_key and sub.get('active', False)

def format_signal_message(signal_type: str, symbol: str, direction: str, price: float,
                          sl: float = None, tp1: float = None, tp2: float = None,
                          tp3: float = None,
                          confidence: float | None = None, reason: str | None = None,
                          exit_price: float = None, pnl: float = None,
                          tp_hit: int = 0, r_multiple: float = None) -> dict:
    utc_ts = datetime.now(timezone.utc).isoformat() + "Z"
    msg = {
        'signal_id': f"{utc_ts}_{symbol}_{direction}",
        'timestamp': utc_ts,
        'symbol': symbol,
        'type': signal_type,
        'direction': direction.upper(),
        'entry_price': price,
        'confidence': confidence,
        'reason': reason
    }
    if sl:
        msg['stop_loss'] = sl
    for i, tp in enumerate([tp1, tp2, tp3], 1):
        if tp:
            msg[f'tp{i}'] = tp
    if exit_price is not None:
        msg['exit_price'] = exit_price
    if pnl is not None:
        msg['pnl'] = pnl
    if tp_hit:
        msg['tp_hit'] = tp_hit
    if r_multiple is not None:
        msg['r_multiple'] = r_multiple
    return msg

def _deliver_to_channel(channel: dict, signal: dict):
    channel_type = channel.get('type')
    is_exit = signal.get('type') == 'exit'
    is_long = signal.get('direction') == 'LONG'
    try:
        if channel_type == 'webhook':
            requests.post(channel['url'], json=signal, timeout=10)
        elif channel_type == 'discord':
            if is_exit:
                p = signal.get('pnl', 0)
                color = 0x00d4aa if p >= 0 else 0xff4757
                tp_h = signal.get('tp_hit', 0)
                tp_str = "  ".join(f"TP{i} {'✓' if i <= tp_h else '✗'}" for i in range(1, 4))
                r = signal.get('r_multiple', 0)
                fields = [
                    {"name": "Entry", "value": f"```${_fp(signal['entry_price'])}```", "inline": True},
                    {"name": "Exit", "value": f"```${_fp(signal.get('exit_price', 0))}```", "inline": True},
                    {"name": "P/L", "value": f"```{'+' if p >= 0 else ''}{p:,.2f}```", "inline": True},
                    {"name": "Targets", "value": tp_str, "inline": False},
                    {"name": "Result", "value": f"```{r:+.1f}R```", "inline": True},
                ]
                embed = {
                    "title": f"{'📈' if p >= 0 else '📉'} EXIT {signal['direction']} {signal['symbol']}",
                    "color": color, "fields": fields,
                    "footer": {"text": f"{_BRAND} • Honest results, wins AND losses"},
                    "timestamp": signal['timestamp']
                }
            else:
                color = 0x00d4aa if is_long else 0xff4757
                arrow = "▲" if is_long else "▼"
                fields = [
                    {"name": "Entry", "value": f"```${_fp(signal['entry_price'])}```", "inline": True},
                    {"name": "Stop Loss", "value": f"```${_fp(signal.get('stop_loss', 0))}```", "inline": True},
                    {"name": "\u200b", "value": "\u200b", "inline": True},
                ]
                for i in range(1, 4):
                    k = f'tp{i}'
                    if signal.get(k):
                        fields.append({"name": f"TP{i}", "value": f"```${_fp(signal[k])}```", "inline": True})
                conf = signal.get('confidence')
                foot = f"{_BRAND} • {conf:.0%} conf" if conf else _BRAND
                embed = {
                    "title": f"{arrow} {signal['direction']} {signal['symbol']}",
                    "color": color, "fields": fields,
                    "footer": {"text": foot},
                    "timestamp": signal['timestamp']
                }
            requests.post(channel['url'], json={"embeds": [embed]}, timeout=10)
        elif channel_type == 'telegram':
            # Card renderer handles all Telegram output — see _send_trade_card_open/close
            return
        elif channel_type == 'email':
            import smtplib
            from email.mime.text import MIMEText
            subject = f"{_BRAND} Signal: {signal['direction']} {signal['symbol']}"
            body = f"{signal['type'].upper()} Signal\n\n" + "\n".join(f"{k}: {v}" for k, v in signal.items())
            msg = MIMEText(body, 'plain')
            msg['Subject'], msg['From'], msg['To'] = subject, channel['from'], channel['to']
            with smtplib.SMTP(channel['smtp_host'], channel.get('smtp_port', 587)) as s:
                s.starttls()
                s.login(channel['username'], channel['password'])
                s.send_message(msg)
        elif channel_type == 'custom':
            requests.post(channel['url'], json=signal, headers=channel.get('headers', {}), timeout=10)
    except Exception as e:
        logging.warning(f"Signal delivery failed ({channel_type}): {e}")

def deliver_signal(signal_type: str, symbol: str, direction: str, price: float,
                  sl: float = None, tp1: float = None, tp2: float = None,
                  tp3: float = None,
                  confidence: float | None = None, reason: str | None = None,
                  exit_price: float = None, pnl: float = None,
                  tp_hit: int = 0, r_multiple: float = None):
    _load_subscribers()
    signal = format_signal_message(signal_type, symbol, direction, price, sl, tp1, tp2, tp3,
                                   confidence, reason, exit_price, pnl, tp_hit, r_multiple)
    for sub_id, sub in _subscribers.items():
        if not sub.get('active', False):
            continue
        prefs = sub.get('preferences', {})
        if signal_type == 'long' and not prefs.get('long', True):
            continue
        if signal_type == 'short' and not prefs.get('short', True):
            continue
        if signal_type == 'exit' and not prefs.get('exit', True):
            continue
        for channel in sub.get('channels', []):
            _deliver_to_channel(channel, signal)

# =============================================================================
# Subscriber API Server
# =============================================================================
_api_server_thread = None
_api_server_running = True

def _run_subscriber_api():
    import http.server
    import socketserver
    class SubscriberAPIHandler(http.server.BaseHTTPRequestHandler):
        def end_headers(self):
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            super().end_headers()
        def do_OPTIONS(self):
            self.send_response(200)
            self.end_headers()
        def do_GET(self):
            if self.path == '/health':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'ok'}).encode())
            elif self.path == '/subscribers':
                _load_subscribers()
                public_subs = {k: {'active': v.get('active', True),
                                   'preferences': v.get('preferences', {}),
                                   'created_at': v.get('created_at', '')} for k, v in _subscribers.items()}
                body = json.dumps(public_subs).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == '/api/messages':
                msgs = list(_TELEGRAM_MESSAGES)
                body = json.dumps(msgs).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        def do_POST(self):
            if self.path == '/register':
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                api_key = register_subscriber(body['sub_id'], body['channels'], body.get('preferences'))
                self.send_response(201)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'api_key': api_key}).encode())
            elif self.path == '/unsubscribe':
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                remove_subscriber(body['sub_id'])
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'unsubscribed'}).encode())
            else:
                self.send_response(404)
                self.end_headers()
        def do_PUT(self):
            if self.path.startswith('/preferences/'):
                sub_id = self.path.split('/')[-1]
                length = int(self.headers.get('Content-Length', 0))
                prefs = json.loads(self.rfile.read(length))
                _load_subscribers()
                if sub_id in _subscribers:
                    _subscribers[sub_id]['preferences'] = prefs
                    _save_subscribers()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'status': 'updated'}).encode())
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, format, *args):
            pass
    try:
        class _ThreadedAPI(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True
        _sub_port = 18096  # GoldenEye standalone — high port to avoid fleet conflicts
        with _ThreadedAPI(("", _sub_port), SubscriberAPIHandler) as httpd:
            logging.info(f"Subscriber API server listening on port {_sub_port}")
            while _api_server_running:
                httpd.handle_request()
    except Exception as e:
        logging.warning(f"Subscriber API server failed: {e}")

def start_subscriber_api():
    global _api_server_thread
    _api_server_thread = threading.Thread(target=_run_subscriber_api, daemon=True)
    _api_server_thread.start()

def order_status_monitor(ex, st, br, interval=30, sym=''):
    _mon_name = f'monitor_{sym}' if sym else 'monitor'
    while True:
        time.sleep(interval)
        heartbeat(_mon_name)
        try:
            current_pos = st.get_position()
            if not current_pos:
                continue

            direction = current_pos.get('direction', _MODE)
            sl_order_id = current_pos.get('sl_order_id')
            tp_order_id = current_pos.get('tp_order_id')

            if sl_order_id:
                try:
                    sl_order = ex.fetch_order(sl_order_id, current_pos.get('symbol', ''))
                    if sl_order.get('status') in ('closed', 'filled'):
                        filled_price = Decimal(str(sl_order.get('average', current_pos.get('e'))))
                        filled_size = Decimal(str(sl_order.get('filled', current_pos.get('size', 0))))
                        fee = current_pos.get('fee', get_fee(current_pos.get('symbol', '')))
                        risk_dist = abs(float(current_pos['e']) - float(current_pos.get('orig_sl', current_pos['sl'])))
                        if risk_dist > 0:
                            r = (float(filled_price) - float(current_pos['e'])) / risk_dist if direction == 'long' else (float(current_pos['e']) - float(filled_price)) / risk_dist
                        else:
                            r = -1

                        if direction == 'long':
                            pnl = (filled_price - current_pos['e']) * filled_size - (filled_price * fee * filled_size + current_pos['e'] * fee * filled_size)
                        else:
                            pnl = (current_pos['e'] - filled_price) * filled_size - (filled_price * fee * filled_size + current_pos['e'] * fee * filled_size)
                        update_circuit_breaker(float(pnl))

                        if tp_order_id:
                            _cancel_attempts = 0
                            while _cancel_attempts < 3:
                                try:
                                    ex.cancel_order(tp_order_id, current_pos.get('symbol', ''))
                                    logging.info(f"Cancelled TP order {tp_order_id} after SL fill")
                                    break
                                except ccxt.OrderNotFound:
                                    logging.info(f"TP order {tp_order_id} already gone — OK")
                                    break
                                except Exception as _cancel_err:
                                    _cancel_attempts += 1
                                    if _cancel_attempts >= 3:
                                        logging.error(f"ORPHAN RISK: TP {tp_order_id} for {current_pos.get('symbol','')} failed to cancel after SL fill ({_cancel_attempts} attempts): {_cancel_err}")
                                    else:
                                        time.sleep(1)

                        # Clear position under sym_lock to prevent race with stream() exit path
                        _s = current_pos.get('symbol', '')
                        _sl_lock = _get_sym_lock(_s)
                        with _sl_lock:
                            if st.get_position() is None:
                                continue  # stream() already exited this position
                            _sym_cooldown[_s] = time.time()
                            st.clear_position()
                        logging.warning(f"SL HIT ({direction}): {_s} @ {filled_price:,.2f}, PnL={pnl:.2f}")
                        if not _CARDS_ENABLED:
                            send_alert(f"SL HIT — {_s} {direction.upper()} closed @ {filled_price:,.2f} | PnL: ${float(pnl):+.2f} | R: {r:+.2f}R\n— {_BRAND}")

                        try:
                            exit_side = 'sell' if direction == 'long' else 'buy'
                            log_fill(_s, exit_side, sl_order_id, float(filled_size), float(filled_price), float(fee) * float(filled_size) * float(filled_price), float(pnl))
                            br.record(current_pos['sg'], r, current_pos.get('slippage', Decimal('0')), direction, factors=current_pos.get('factors'), rgm=current_pos.get('regime'))
                            _td = _build_trade_detail(current_pos, filled_price, tp_hit=0)
                            record_live_trade(direction, float(r), float(pnl), sym=_s, exit_type='SL', detail=_td)
                            _log_factor_trade(_s, current_pos, r, float(pnl))
                            deliver_signal(
                                signal_type='exit', symbol=_s, direction=direction,
                                price=float(current_pos.get('e', filled_price)),
                                sl=float(current_pos.get('orig_sl', current_pos.get('sl', 0))),
                                tp1=float(current_pos.get('tp1', 0)), tp2=float(current_pos.get('tp2', 0)),
                                tp3=float(current_pos.get('tp3', 0)),
                                confidence=None, exit_price=float(filled_price),
                                pnl=float(pnl), tp_hit=0, r_multiple=float(r)
                            )
                            _send_trade_card_close(_s, direction, current_pos, filled_price,
                                                   pnl, r, tp_hit=0, exit_type='SL')
                            _log_trade_marker('close', _s, direction, filled_price, r=r, pnl=pnl)
                        except Exception as post_err:
                            logging.warning(f"{_s} post-trade processing error (position already cleared): {post_err}")
                except Exception as e:
                    logging.warning(f"SL order check failed: {e}")

            if tp_order_id and st.get_position() is not None:
                try:
                    tp_order = ex.fetch_order(tp_order_id, current_pos.get('symbol', ''))
                    if tp_order.get('status') in ('closed', 'filled'):
                        filled_price = Decimal(str(tp_order.get('average', current_pos.get('e'))))
                        filled_size = Decimal(str(tp_order.get('filled', current_pos.get('size', 0))))
                        fee = current_pos.get('fee', get_fee(current_pos.get('symbol', '')))
                        risk_dist = abs(float(current_pos['e']) - float(current_pos.get('orig_sl', current_pos['sl'])))
                        if risk_dist > 0:
                            r = (float(filled_price) - float(current_pos['e'])) / risk_dist if direction == 'long' else (float(current_pos['e']) - float(filled_price)) / risk_dist
                        else:
                            r = current_pos.get('tp_hit', 1)

                        if direction == 'long':
                            pnl = (filled_price - current_pos['e']) * filled_size - (filled_price * fee * filled_size + current_pos['e'] * fee * filled_size)
                        else:
                            pnl = (current_pos['e'] - filled_price) * filled_size - (filled_price * fee * filled_size + current_pos['e'] * fee * filled_size)
                        update_circuit_breaker(float(pnl))

                        if sl_order_id:
                            _cancel_attempts = 0
                            while _cancel_attempts < 3:
                                try:
                                    ex.cancel_order(sl_order_id, current_pos.get('symbol', ''))
                                    logging.info(f"Cancelled SL order {sl_order_id} after TP fill")
                                    break
                                except ccxt.OrderNotFound:
                                    logging.info(f"SL order {sl_order_id} already gone — OK")
                                    break
                                except Exception as _cancel_err:
                                    _cancel_attempts += 1
                                    if _cancel_attempts >= 3:
                                        logging.error(f"ORPHAN RISK: SL {sl_order_id} for {current_pos.get('symbol','')} failed to cancel after TP fill ({_cancel_attempts} attempts): {_cancel_err}")
                                    else:
                                        time.sleep(1)

                        # Clear position under sym_lock to prevent race with stream() exit path
                        _s = current_pos.get('symbol', '')
                        _tp_h = current_pos.get('tp_hit', 0)
                        _tp_lock = _get_sym_lock(_s)
                        with _tp_lock:
                            if st.get_position() is None:
                                continue  # stream() already exited this position
                            _sym_cooldown[_s] = time.time()
                            st.clear_position()
                        logging.info(f"TP HIT ({direction}): {_s} @ {filled_price:,.2f}, PnL={pnl:.2f}")
                        if not _CARDS_ENABLED:
                            send_alert(f"TP{_tp_h} HIT — {_s} {direction.upper()} closed @ {filled_price:,.2f} | PnL: ${float(pnl):+.2f} | R: {r:+.2f}R\n— {_BRAND}")

                        try:
                            exit_side = 'sell' if direction == 'long' else 'buy'
                            log_fill(_s, exit_side, tp_order_id, float(filled_size), float(filled_price), float(fee) * float(filled_size) * float(filled_price), float(pnl))
                            br.record(current_pos['sg'], r, current_pos.get('slippage', Decimal('0')), direction, factors=current_pos.get('factors'), rgm=current_pos.get('regime'))
                            _td = _build_trade_detail(current_pos, filled_price, tp_hit=_tp_h)
                            record_live_trade(direction, float(r), float(pnl), sym=_s, exit_type='TP', detail=_td)
                            _log_factor_trade(_s, current_pos, r, float(pnl))
                            deliver_signal(
                                signal_type='exit', symbol=_s, direction=direction,
                                price=float(current_pos.get('e', filled_price)),
                                sl=float(current_pos.get('orig_sl', current_pos.get('sl', 0))),
                                tp1=float(current_pos.get('tp1', 0)), tp2=float(current_pos.get('tp2', 0)),
                                tp3=float(current_pos.get('tp3', 0)),
                                confidence=None, exit_price=float(filled_price),
                                pnl=float(pnl), tp_hit=_tp_h, r_multiple=float(r)
                            )
                            _send_trade_card_close(_s, direction, current_pos, filled_price,
                                                   pnl, r, tp_hit=_tp_h, exit_type='TP')
                            _log_trade_marker('close', _s, direction, filled_price, r=r, pnl=pnl)
                        except Exception as post_err:
                            logging.warning(f"{_s} post-trade processing error (position already cleared): {post_err}")
                except Exception as e:
                    logging.warning(f"TP order check failed: {e}")

        except Exception as e:
            logging.warning(f"Order monitor error: {e}")

def paper_fill(direction, current_price, position_size, fee):
    """Simulate a market fill for paper trading."""
    if direction == 'long':
        filled_price = current_price * (1 + _PAPER_SLIP)
    else:
        filled_price = current_price * (1 - _PAPER_SLIP)
    slippage = _PAPER_SLIP
    return filled_price, position_size, slippage

# Stream
def stream(st, sym, br, ex, iv=2):
    global sts
    register_thread(f'stream_{sym}')

    # Bootstrap with historical 1h candles so indicators are ready immediately
    _ohlcv_ts = 0  # last OHLCV refresh timestamp
    _OHLCV_REFRESH = 60  # re-fetch 1h candles every 60s (catches new candle closes)
    def _refresh_ohlcv():
        nonlocal _ohlcv_ts
        now = time.time()
        if now - _ohlcv_ts < _OHLCV_REFRESH:
            return
        try:
            o = ex.fetch_ohlcv(sym, '1h', limit=100)
            if o:
                hdf = pd.DataFrame(o, columns=['t','o','h','l','c','v']).rename(
                    columns={'c':'close','h':'high','l':'low','v':'vol','t':'time'})
                hdf = hdf[['time','o','close','high','low','vol']]
                with st._lock:
                    st.df = ind(hdf)
                _ohlcv_ts = now
        except Exception as e:
            logging.debug(f"{sym} OHLCV refresh failed: {e}")

    _refresh_ohlcv()
    if len(st.df) > 0:
        logging.info(f"{sym} bootstrapped {len(st.df)} candles")

    while not st.shutdown.is_set():
        try:
            heartbeat(f'stream_{sym}')
            tk = ex.fetch_ticker(sym)
            st.update_price(Decimal(str(tk['last'])))
            update_price_history(sym, float(tk['last']))
            v = float(tk.get('baseVolume',0))

            current_price, _ = st.get_price()
            logging.debug(f"{sym} price={current_price:,.2f} vol={v:,.0f}")

            # Decision log instrumentation — populated throughout gate chain, read at line ~4140
            _gates_passed = []
            _gates_failed = []

            # Refresh 1h OHLCV candles periodically — keeps indicators on pure hourly data
            # Ticker provides current price for position management, NOT for indicator computation
            _refresh_ohlcv()
            # Regime on 4h timeframe with 4h cooldown, fallback to tick data
            regime_age = time.time() - getattr(br, '_regime_ts', 0)
            if regime_age > 14400:  # 4 hours
                htf_df = fetch_higher_timeframe(ex, sym, '4h', 100)
                rgm_df = htf_df if htf_df is not None and len(htf_df) >= 50 else st.df
                if _HAS_HMM:
                    if sym not in _hmm_detectors:
                        _hmm_detectors[sym] = HMMRegimeDetector()
                    hd = _hmm_detectors[sym]
                    hd.tick()
                    # UPGRADE C: Feed BTC returns for cross-asset correlation feature
                    if sym != 'BTC/USD':
                        btc_st = sts.get('BTC/USD')
                        if btc_st and len(btc_st.df) > 1:
                            hd.set_btc_returns(btc_st.df)
                    if not hd.fitted or hd.needs_refit():
                        hd.fit(rgm_df)
                    br.regime_proba = hd.predict_proba(rgm_df)
                    br.regime = hd.dominant_regime(rgm_df)
                else:
                    br.regime = regime(rgm_df)
                    br.regime_proba = [1.0 if r == br.regime else 0.0 for r in _REGIMES]
                br._regime_ts = time.time()

            sg = sigs(st.df)
            current_pos = st.get_position()

            base_sl = get_sl_pct(sym)
            base_tp = get_tp_pct(sym)
            vol_sl, vol_tp = get_volatility_SL_TP(st.df, base_sl, base_tp)
            SL_PCT = vol_sl
            TP_PCT = vol_tp

            # UPGRADE G: Check multiple shadow positions per symbol
            # Brain learns from trades the floor blocked
            with _shadow_lock:
                _sh_list = list(_shadow_positions.get(sym, []))
            if _sh_list:
                _sh_price = float(current_price)
                _resolved_indices = []
                for _sh_idx, _sh in enumerate(_sh_list):
                    _sh_resolved = False
                    _sh_r = 0
                    if _sh["direction"] == "long":
                        if _sh_price <= _sh["sl"]:
                            _sh_r = -1.0
                            _sh_resolved = True
                        elif _sh_price >= _sh["tp1"]:
                            _sh_r = 0.75
                            _sh_resolved = True
                    else:
                        if _sh_price >= _sh["sl"]:
                            _sh_r = -1.0
                            _sh_resolved = True
                        elif _sh_price <= _sh["tp1"]:
                            _sh_r = 0.75
                            _sh_resolved = True

                    if not _sh_resolved and time.time() - _sh["timestamp"] > _SHADOW_EXPIRY:
                        _sh_resolved = True
                        _sh_r = 0

                    if _sh_resolved:
                        _resolved_indices.append(_sh_idx)
                        if _sh_r != 0:
                            # br.record(_sh["signals"], _sh_r, Decimal('0'), _sh["direction"],
                            #           factors=_sh["factors"], rgm=_sh["regime"])  # NEUTERED: shadow learning poisons global_stats via 1% SL noise
                            _outcome = "WIN" if _sh_r > 0 else "LOSS"
                            logging.info(f"{sym} SHADOW {_outcome}: {_sh['block_reason']} "
                                         f"entry={_sh['entry_price']:.4f} exit={_sh_price:.4f} R={_sh_r:+.2f}")
                        else:
                            logging.info(f"{sym} SHADOW EXPIRED: {_sh['block_reason']}")

                # Remove resolved shadows (reverse order to preserve indices)
                if _resolved_indices:
                    with _shadow_lock:
                        remaining = _shadow_positions.get(sym, [])
                        _shadow_positions[sym] = [s for i, s in enumerate(remaining)
                                                   if i not in set(_resolved_indices)]
                        if not _shadow_positions[sym]:
                            del _shadow_positions[sym]

            # Entry gate chain — 6-factor confidence model
            cd_ok = time.time() >= _sym_cooldown.get(sym, 0) + _COOLDOWN_SECS
            direction = None
            _llm_cached = None  # cached Ollama score from prior cycle
            factors = None
            confidence = 0.0
            # Debug: log signal count even if below threshold
            if len(sg) > 0:
                logging.info(f"{sym} signals: {sg} (len={len(sg)}, pos={current_pos is not None}, cd_ok={cd_ok})")
            if _circuit_breaker_tripped and current_pos is None:
                logging.debug(f"{sym} entry blocked: circuit breaker tripped")
                time.sleep(iv)
                continue
            if len(sg) >= 2 and current_pos is None and cd_ok:
                logging.info(f"{sym} signals: {sg} (len={len(sg)}, pos={current_pos is not None}, cd_ok={cd_ok})")

                # Bus intelligence layer — fleet context modifies entry decisions
                _bus_boost = Decimal('1')
                # Initialize ALL variables that might be used in decision logging
                _ws = None
                _fr = None
                factors = None
                confidence = 0.0
                _corr_ok = False
                _corr_val = 0.0
                _floor_fail = None
                # Gate 1: Correlation pre-screen
                open_symbols = [pos.get('symbol') for s in sts.values() for pos in [s.get_position()] if pos]
                _corr_ok, _corr_val = check_correlation_risk(sym, open_symbols)
                if not _corr_ok and _corr_val > 0.95:
                    logging.debug(f"{sym} correlation hard ceiling, skipping")
                    _gates_failed.append(f"correlation:{_corr_val:.2f}")
                elif not _corr_ok:
                    logging.info(f"{sym} correlation blocked (corr={_corr_val:.2f})")
                    _gates_failed.append(f"correlation:{_corr_val:.2f}")
                else:
                    _gates_passed.append("correlation")
                    # Compute 6-factor scores + confidence
                    vp_data = ob_data = None
                    if is_auction_theory_enabled():
                        vp_data = compute_volume_profile(st.df.tail(50))
                        ob_data = find_order_blocks(st.df.tail(30))
                    htf_bias = compute_mtf_confluence(ex, sym, st.df)
                    atr_pct = float(st.df['atr_pct'].iloc[-1]) if 'atr_pct' in st.df.columns and len(st.df) > 0 else 0.02
                    of_score = compute_order_flow_factor(ex, sym)
                    _fr = get_funding_rate(sym)
                    _ws = get_whale_score(sym)
                    factors = compute_factors(st.df, float(current_price), vp_data, ob_data, htf_bias, br.regime, of_score=of_score, funding_rate=_fr, whale_score=_ws, short_mode=(_MODE == 'short'))
                    confidence = compute_confidence(factors, br.regime, rgm_proba=br.regime_proba, brain=br)

                    logging.debug(f"{sym} factors: T={factors['trend']:.2f} M={factors['momentum']:.2f} V={factors['volume']:.2f} Vol={factors['volatility']:.2f} S={factors['structure']:.2f} OF={factors['order_flow']:.2f} conf={confidence:.3f}")

                    # Gate 1b: Factor quality floors — reject entries with garbage factor scores
                    # Individual minimums, NOT a combined score. "Momentum below 0.30 = move has no legs."
                    _FACTOR_FLOORS_ENABLED = True
                    _FACTOR_FLOORS = {"trend": 0.25, "momentum": 0.20, "volume": 0.15}
                    _floor_fail = None
                    if _FACTOR_FLOORS_ENABLED and factors:
                        for _ff_name, _ff_min in _FACTOR_FLOORS.items():
                            if factors.get(_ff_name, 0) < _ff_min:
                                _floor_fail = _ff_name
                                break
                    if _floor_fail:
                        logging.info(f"{sym} SKIP: factor_floor_{_floor_fail} ({factors[_floor_fail]:.2f} < {_FACTOR_FLOORS[_floor_fail]})")
                        _gates_failed.append(f"factor_floor:{_floor_fail}")
                        # Shadow track: record what WOULD have happened so brain still learns
                        _shadow_dir = _MODE
                        _shadow_tp_dist = float(current_price) * float(TP_PCT)
                        _shadow_tp1m, _, _ = _get_tp_mults(br.regime)
                        # Match real entry ATR scaling: squeeze -> expand, expansion -> contract
                        _sh_atr_med = float(st.df['atr'].rolling(50).median().iloc[-1]) if 'atr' in st.df.columns and len(st.df) >= 50 else None
                        _sh_atr_now = float(st.df['atr'].iloc[-1]) if 'atr' in st.df.columns and len(st.df) > 0 else None
                        if _sh_atr_med and _sh_atr_now and not pd.isna(_sh_atr_med) and not pd.isna(_sh_atr_now) and _sh_atr_med > 0:
                            _sh_tp_scale = max(0.8, min(1.0 / (_sh_atr_now / _sh_atr_med), 1.5))
                        else:
                            _sh_tp_scale = 1.0
                        if _shadow_dir == 'long':
                            _shadow_sl = float(current_price) * (1 - float(SL_PCT))
                            _shadow_tp1 = float(current_price) + _shadow_tp_dist * float(_shadow_tp1m) * _sh_tp_scale
                        else:
                            _shadow_sl = float(current_price) * (1 + float(SL_PCT))
                            _shadow_tp1 = float(current_price) - _shadow_tp_dist * float(_shadow_tp1m) * _sh_tp_scale
                        # UPGRADE G: Append shadow to list (multiple concurrent shadows)
                        with _shadow_lock:
                            if sym not in _shadow_positions:
                                _shadow_positions[sym] = []
                            # Evict oldest if at capacity
                            while len(_shadow_positions[sym]) >= _MAX_SHADOWS_PER_SYM:
                                _shadow_positions[sym].pop(0)
                            _shadow_positions[sym].append({
                                "signals": list(sg),
                                "entry_price": float(current_price),
                                "sl": _shadow_sl,
                                "tp1": _shadow_tp1,
                                "direction": _shadow_dir,
                                "factors": dict(factors),
                                "regime": br.regime,
                                "timestamp": time.time(),
                                "block_reason": f"factor_floor_{_floor_fail}={factors[_floor_fail]:.3f}",
                            })
                        logging.info(f"{sym} SHADOW: entry={float(current_price):.4f} SL={_shadow_sl:.4f} TP1={_shadow_tp1:.4f} sigs={sg}")
                        direction = None
                    else:
                        # Gate 2: Set direction — confluence and confidence gates follow
                        direction = _MODE
                        _gates_passed.append("factor_floors")

                    # Gate 2a: Weighted signal confluence (adaptive from brain stats)
                    # _DISABLED_SIGS: empty set — all signals cleared for clean-brain trial 2026-04-13
                    # Previously disabled (shadow-poisoned evidence): d,g,j,k,m,o,r,s,u,x
                    _DISABLED_SIGS = set()
                    _DIR_SIGS = set('abcdefghijklmnopqrstuvwxyz2') - _DISABLED_SIGS
                    if direction:
                        dir_sigs = [s for s in sg if s in _DIR_SIGS]
                        if len(dir_sigs) < 1:
                            logging.info(f"{sym} {direction.upper()} blocked: no directional signals")
                            _gates_failed.append("no_dir_sigs")
                            direction = None
                        else:
                            _gs = get_global_signal_stats(brs) if 'brs' in globals() else None
                            _wsc, _active, _suppressed = compute_weighted_confluence(dir_sigs, br, br.regime, _gs)
                            _wmin = _WTD_SIG_MIN.get(br.regime, 0.90)
                            if _wsc < _wmin or len(_active) < 1:
                                logging.info(f"{sym} {direction.upper()} blocked: weighted confluence {_wsc:.2f} < {_wmin} (active={_active} suppressed={_suppressed})")
                                _gates_failed.append(f"confluence:{_wsc:.2f}")
                                direction = None
                            else:
                                sg = _active
                                _gates_passed.append("confluence")

                    # Gate 2b: Directional win-rate floor (last 10 trades)
                    if direction:
                        dwr = br.dir_wr(direction)
                        if dwr is not None and dwr < 0.2:
                            logging.info(f"{sym} {direction.upper()} blocked: dir WR {dwr:.0%} < 20% (last 10)")
                            _gates_failed.append(f"dir_wr:{dwr:.2f}")
                            direction = None
                        else:
                            _gates_passed.append("dir_wr")

                    # Gate 3: Regime weighted multiplier (blended when HMM available)
                    rgm_mult = 1.0
                    if direction:
                        rgm_mult = _blended_rgm_mult(br.regime_proba, direction)
                        if rgm_mult < 0.1:
                            logging.debug(f"{sym} {direction.upper()} blocked by {br.regime} regime (mult={rgm_mult})")
                            _gates_failed.append(f"regime_mult:{rgm_mult:.2f}")
                            direction = None
                        elif rgm_mult < 1.0:
                            logging.debug(f"{sym} regime {br.regime} scaling {direction} by {rgm_mult:.1f}x")
                            _gates_passed.append(f"regime_mult:{rgm_mult:.2f}")
                        else:
                            _gates_passed.append("regime_mult")

                    # Gate 3b: Whale score filter — require whale activity confirmation
                    # Whale score (0-100) from whale_indicators.py confirms volume/momentum
                    # This is the PRIMARY missing gate from the original audit
                    # Lowered from 40 to 30 — 2026-04-13 clean-brain trial, prior blocks clustered 31-37
                    _whale_blocked = False
                    if direction and _ws is not None and _ws < 30:
                        _now_ws = time.time()
                        if _now_ws - _gate_block_last_log.get((sym, 'whale'), 0) > 300:
                            logging.info(f"{sym} {direction.upper()} blocked: whale_score {_ws:.1f} < 30 (no whale confirmation)")
                            _gate_block_last_log[(sym, 'whale')] = _now_ws
                        _gates_failed.append(f"whale:{_ws:.1f}")
                        direction = None
                        _whale_blocked = True
                    elif direction and _ws is not None:
                        _gates_passed.append(f"whale:{_ws:.1f}")

                    # Record shadow position for whale-blocked entries (for learning)
                    if _whale_blocked and factors:
                        with _shadow_lock:
                            if sym not in _shadow_positions:
                                _shadow_positions[sym] = []
                            while len(_shadow_positions[sym]) >= _MAX_SHADOWS_PER_SYM:
                                _shadow_positions[sym].pop(0)
                            _shadow_positions[sym].append({
                                "signals": list(sg),
                                "entry_price": float(current_price),
                                "sl": float(current_price) * 0.99 if _MODE == 'long' else float(current_price) * 1.01,
                                "tp1": float(current_price) * 1.02 if _MODE == 'long' else float(current_price) * 0.98,
                                "direction": _MODE,
                                "factors": dict(factors),
                                "regime": br.regime,
                                "timestamp": time.time(),
                                "block_reason": f"whale_score_{_ws:.1f}<30",
                            })
                        logging.info(f"{sym} SHADOW: whale blocked at {float(current_price):.4f} (whale_score={_ws:.1f})")

                    # Factor confidence: logged for analysis, NOT used as gate
                    # Data shows confidence is anti-predictive (low conf = better trades)
                    auction_score = confidence
                    if direction:
                        br.confidence = confidence

                    # Gate 4a: AI confidence modifier — Ollama LLM only (cached from prior cycle)
                    # FinBERT removed: converting factors→text→sentiment→score was circular
                    # (it could only echo back what the factors already computed)
                    min_confluence = _blended_rgm_conf_min(br.regime_proba, br.trade_count)
                    _ai_raw = None
                    _llm_cached = None
                    if direction and is_ai_enabled():
                        try:
                            from goldeneye_ai import get_cached_ollama_score, get_ollama_queue
                            _llm_cached = get_cached_ollama_score(sym)
                            # Queue a fresh Ollama evaluation for next cycle
                            _oq = get_ollama_queue()
                            if _oq is not None:
                                _ai_prompt = (
                                    f"{sym}: trend={factors['trend']:.2f} momentum={factors['momentum']:.2f} "
                                    f"structure={factors['structure']:.2f} volume={factors['volume']:.2f} "
                                    f"volatility={factors['volatility']:.2f} order_flow={factors['order_flow']:.2f} "
                                    f"regime={br.regime} signals={','.join(sg)}"
                                )
                                try:
                                    _oq.put_nowait({"symbol": sym, "prompt": _ai_prompt})
                                except Exception:
                                    pass  # queue full, skip this cycle
                        except Exception:
                            _llm_cached = None
                        if _llm_cached is not None:
                            old_conf = confidence
                            # UPGRADE D: Dynamic blend weight based on Ollama's Brier score
                            _llm_w = _compute_llm_blend_weight()
                            confidence = (1 - _llm_w) * confidence + _llm_w * _llm_cached
                            _now_ai = time.time()
                            if _now_ai - _gate_block_last_log.get((sym, 'ai_blend'), 0) > 300:
                                logging.info(f"{sym} AI blend: factor={old_conf:.3f} llm={_llm_cached:.3f} w={_llm_w:.2f} → {confidence:.3f}")
                                _gate_block_last_log[(sym, 'ai_blend')] = _now_ai
                            br.confidence = confidence
                            _ai_raw = _llm_cached
                            # Re-check confidence threshold after blend
                            if confidence < min_confluence:
                                if _now_ai - _gate_block_last_log.get((sym, 'ai_conf'), 0) > 300:
                                    logging.info(f"{sym} {direction} blocked: AI-blended confidence ({confidence:.3f} < {min_confluence})")
                                    _gate_block_last_log[(sym, 'ai_conf')] = _now_ai
                                _gates_failed.append(f"ai_conf:{confidence:.3f}")
                                direction = None
                            else:
                                _gates_passed.append(f"ai_conf:{confidence:.3f}")

                    # Gate 5: Position count + drawdown (signal bot — no heat cap)
                    if direction:
                        if count_open_positions(sts) >= _MAX_OPEN_POS:
                            logging.info(f"{sym} blocked: max {_MAX_OPEN_POS} open positions")
                            _gates_failed.append("max_positions")
                            direction = None
                        elif get_drawdown_mult() <= 0:
                            logging.info(f"{sym} blocked: drawdown tier 0x (>15%)")
                            _gates_failed.append("drawdown")
                            direction = None
                        else:
                            _gates_passed.append("max_positions")
                            _gates_passed.append("drawdown")

                    # Gate 6: Sentiment filter (external API — most expensive)
                    if direction:
                        if not use_sentiment_filter(sym):
                            logging.debug(f"{sym} sentiment filter blocked entry")
                            _gates_failed.append("sentiment")
                            direction = None
                        else:
                            _gates_passed.append("sentiment")

            # Gate 7: Minimum move threshold — skip micro-trades that can't overcome fees
            # ATR * 0.33 must exceed round-trip fees. Was previously ATR/2 (0.5x) but too tight —
            # blocked everything in low-vol. 0.33x is the loosened threshold.
            if direction and current_price > 0:
                _atr_pct_val = float(st.df['atr_pct'].iloc[-1]) if 'atr_pct' in st.df.columns and len(st.df) > 0 else 0
                _est_fee_rt = 0.0080  # Kraken taker round-trip 0.80% (tier 0, 2026-04)
                if _atr_pct_val > 0 and (_atr_pct_val * 0.33) < _est_fee_rt:
                    _now = time.time()
                    if _now - _gate_block_last_log.get((sym, 'atr'), 0) > 300:
                        logging.info(f"{sym} {direction} blocked: ATR move {_atr_pct_val*0.33:.4%} < fees {_est_fee_rt:.4%}")
                        _gate_block_last_log[(sym, 'atr')] = _now
                    _gates_failed.append(f"atr_fees:{_atr_pct_val*0.33:.4f}")
                    direction = None
                elif _atr_pct_val > 0:
                    _gates_passed.append(f"atr_fees:{_atr_pct_val*0.33:.4f}")

            if direction and current_price > 0:
                fee = get_fee(sym)
                min_amt, max_amt, cost_min = get_market_limits(sym)
                dd_mult = Decimal(str(get_drawdown_mult()))

                # UPGRADE F: Fractional Kelly position sizing — replaces flat $100
                # Base: half-Kelly fraction of balance, floored at _TRADE_AMT
                # f* = (WR * avg_win - (1-WR) * avg_loss) / avg_win
                with _metrics_lock:
                    _rh = list(_live_metrics['r_history'])
                _base_amt = _get_trade_amt()  # $500 paper, 3% of Kraken balance live
                _kelly_base = _base_amt  # fallback when insufficient data
                if len(_rh) >= 20:
                    _wins = [rv for rv in _rh if rv > 0]
                    _losses = [rv for rv in _rh if rv <= 0]
                    _wr = len(_wins) / len(_rh)
                    _avg_win = sum(_wins) / len(_wins) if _wins else 0
                    _avg_loss = abs(sum(_losses) / len(_losses)) if _losses else 1
                    _payoff = _avg_win / _avg_loss if _avg_loss > 0 else 1
                    _kelly = max(0, (_wr * _payoff - (1 - _wr)) / _payoff) if _payoff > 0 else 0
                    _hk = Decimal(str(round(_kelly / 2, 4)))  # half-Kelly
                    if _hk > 0:
                        if _PAPER():
                            with _PAPER_BAL_LOCK:
                                _kelly_amt = _PAPER_BALANCE * _hk
                        else:
                            with _LIVE_BAL_LOCK:
                                _kelly_amt = _LIVE_BALANCE * _hk
                        _kelly_base = max(_base_amt,
                                          min(_kelly_amt, _base_amt * Decimal('3')))
                        logging.debug(f"{sym} Kelly sizing: f*={_kelly:.3f} hK={_hk:.4f} base=${float(_kelly_base):.0f}")
                elif len(_rh) >= 10:
                    # Ramp up: with 10-19 trades, use quarter-Kelly as transitional
                    _wins = [rv for rv in _rh if rv > 0]
                    _losses = [rv for rv in _rh if rv <= 0]
                    _wr = len(_wins) / len(_rh)
                    _avg_win = sum(_wins) / len(_wins) if _wins else 0
                    _avg_loss = abs(sum(_losses) / len(_losses)) if _losses else 1
                    _payoff = _avg_win / _avg_loss if _avg_loss > 0 else 1
                    _kelly = max(0, (_wr * _payoff - (1 - _wr)) / _payoff) if _payoff > 0 else 0
                    _qk = Decimal(str(round(_kelly / 4, 4)))  # quarter-Kelly
                    if _qk > 0:
                        if _PAPER():
                            with _PAPER_BAL_LOCK:
                                _kelly_amt = _PAPER_BALANCE * _qk
                        else:
                            with _LIVE_BAL_LOCK:
                                _kelly_amt = _LIVE_BALANCE * _qk
                        _kelly_base = max(_base_amt,
                                          min(_kelly_amt, _base_amt * Decimal('2')))

                trade_amt = _kelly_base * Decimal(str(rgm_mult)) * dd_mult * _bus_boost
                # Floor: paper mode needs CC pool floor; live mode uses _base_amt directly
                if _PAPER():
                    trade_amt = max(trade_amt, _TRADE_AMT * Decimal('1.01'))
                else:
                    trade_amt = max(trade_amt, _base_amt)

                # Cap at 20% of balance
                if _PAPER():
                    with _PAPER_BAL_LOCK:
                        max_trade = _PAPER_BALANCE * Decimal('0.2')
                else:
                    with _LIVE_BAL_LOCK:
                        max_trade = _LIVE_BALANCE * Decimal('0.2')
                trade_amt = min(trade_amt, max_trade)
                raw_size = trade_amt / current_price
                if min_amt and raw_size < Decimal(str(min_amt)):
                    raw_size = Decimal(str(min_amt))
                    adjusted_amt = raw_size * current_price
                    if _PAPER():
                        with _PAPER_BAL_LOCK:
                            cap = _PAPER_BALANCE * Decimal('0.1')
                    else:
                        with _LIVE_BAL_LOCK:
                            cap = _LIVE_BALANCE * Decimal('0.1')
                    if adjusted_amt > cap:
                        _now_sz = time.time()
                        if _now_sz - _gate_block_last_log.get((sym, 'min_size'), 0) > 300:
                            logging.warning(f"{sym} min_size scaled to {min_amt} exceeds 10% cap (${float(cap):.2f}), skipping")
                            _gate_block_last_log[(sym, 'min_size')] = _now_sz
                        _gates_failed.append("min_size")
                        direction = None
                    else:
                        trade_amt = adjusted_amt
                        logging.info(f"{sym} scaled to min {min_amt}, trade_amt=${float(trade_amt):.2f}")
                        _gates_passed.append("min_size")
                elif max_amt and raw_size > Decimal(str(max_amt)):
                    raw_size = Decimal(str(max_amt))
            elif direction:
                logging.warning(f"{sym} price is zero, skipping entry")
                direction = None

            # ========== DECISION LOGGING ==========
            # Record this entry decision for traceability and post-trade analysis.
            # _gates_passed and _gates_failed are populated at each gate site throughout
            # the gate chain above (initialized at loop top near line 3683).
            # Ensure _ws has a safe default for decision logging
            if '_ws' not in dir() or _ws is None:
                _ws = None
            if direction:
                _gates_passed.append("entry")

            # Only log decisions that actually ran through at least one gate.
            # Without this guard, every stream iteration where the gate chain is skipped
            # (len(sg) < 2, position already open, etc.) pushes an empty record and the
            # ring buffer fills with meaningless rows, burying the real decisions.
            if _gates_passed or _gates_failed:
                _decision_record = {
                    "timestamp": time.time(),
                    "symbol": sym,
                    "signals": list(sg),
                    "factors": {k: round(v, 3) for k, v in factors.items()} if factors else {},
                    "confidence": round(confidence, 3) if confidence else 0.0,
                    "whale_score": round(_ws, 1) if _ws is not None else None,
                    "regime": br.regime,
                    "gates_passed": _gates_passed,
                    "gates_failed": _gates_failed,
                    "result": "ENTERED" if direction and current_price > 0 else "BLOCKED",
                }
                with _decision_log_lock:
                    _decision_log.append(_decision_record)
                    if len(_decision_log) > _MAX_DECISION_LOG:
                        _decision_log.pop(0)

            if direction:
                # notional stays in Decimal space — raw_size is Decimal, current_price is Decimal
                notional = raw_size * current_price
                if cost_min and notional < Decimal(str(cost_min)):
                    logging.warning(f"{sym} notional {notional} below cost min {cost_min}, skipping")
                    _gates_failed.append("notional")
                else:
                    _gates_passed.append("notional")
                    # Acquire per-symbol lock to prevent duplicate entries from watchdog-restarted threads
                    sym_lock = _get_sym_lock(sym)
                    with sym_lock:
                        # Double-check: another thread may have opened a position between our check and now.
                        # This fires at INFO level so the event shows in logs if a duplicate is ever attempted.
                        if st.get_position() is not None:
                            logging.info(f"{sym} duplicate open blocked: position already open (pair guard)")
                            _gates_failed.append("duplicate")
                        else:
                            position_size = raw_size
                            _reservation_id = ""

                            if _is_paper():
                                filled_price, filled_size, slippage = paper_fill(direction, current_price, position_size, fee)
                                tp_dist = filled_price * TP_PCT
                                # Dynamic TP scaling: squeeze -> expand targets, expansion -> contract
                                _atr_med = float(st.df['atr'].rolling(50).median().iloc[-1]) if 'atr' in st.df.columns and len(st.df) >= 50 else None
                                _atr_now = float(st.df['atr'].iloc[-1]) if 'atr' in st.df.columns and len(st.df) > 0 else None
                                if _atr_med and _atr_now and not pd.isna(_atr_med) and not pd.isna(_atr_now) and _atr_med > 0:
                                    _tp_scale = Decimal(str(max(0.8, min(1.0 / (_atr_now / _atr_med), 1.5))))
                                else:
                                    _tp_scale = Decimal('1')
                                _tp1m, _tp2m, _tp3m = _get_tp_mults(br.regime)
                                if direction == 'long':
                                    stop_loss = filled_price * (1 - SL_PCT)
                                    tp1 = filled_price + tp_dist * _tp1m * _tp_scale
                                    tp2 = filled_price + tp_dist * _tp2m * _tp_scale
                                    tp3 = filled_price + tp_dist * _tp3m * _tp_scale
                                else:
                                    stop_loss = filled_price * (1 + SL_PCT)
                                    tp1 = filled_price - tp_dist * _tp1m * _tp_scale
                                    tp2 = filled_price - tp_dist * _tp2m * _tp_scale
                                    tp3 = filled_price - tp_dist * _tp3m * _tp_scale
                                order_id = f"paper_{int(time.time()*1000)}"
                                logging.info(f"{sym} PAPER {direction.upper()} @ {_fp(filled_price)}, size={filled_size:,.4f} TP1={_fp(tp1)} TP2={_fp(tp2)} TP3={_fp(tp3)}")
                                log_fill(sym, 'buy' if direction == 'long' else 'sell', order_id, float(filled_size), float(filled_price), float(fee) * float(filled_size) * float(filled_price), signals=sg)
                                try:
                                    deliver_signal(
                                        signal_type=direction, symbol=sym, direction=direction,
                                        price=float(filled_price), sl=float(stop_loss),
                                        tp1=float(tp1), tp2=float(tp2), tp3=float(tp3),
                                        confidence=float(br.confidence) if hasattr(br, 'confidence') else None,
                                        reason="confluence entry"
                                    )
                                except Exception as e:
                                    logging.warning(f"Signal delivery failed: {e}")
                                try:
                                    _send_trade_card_open(sym, direction, filled_price, stop_loss,
                                                         tp1, tp2, tp3, filled_size,
                                                         float(br.confidence) if hasattr(br, 'confidence') else None,
                                                         br.regime, sg)
                                except Exception as e:
                                    logging.warning(f"Card send (paper open) failed: {e}")
                                _log_trade_marker('open', sym, direction, filled_price)
                                st.set_position({
                                    'symbol': sym, 'direction': direction,
                                    'e': filled_price, 'sl': stop_loss, 'orig_sl': stop_loss,
                                    'tp1': tp1, 'tp2': tp2, 'tp3': tp3, 'tp_hit': 0,
                                    'opened_at': time.time(), 'last_tp_time': 0,
                                    'sg': sg, 'size': filled_size,
                                    'order_id': order_id, 'fee': fee,
                                    'sl_order_id': None, 'tp_order_id': None,
                                    'filled': float(filled_size), 'slippage': slippage,
                                    'atr_pct': atr_pct, 'auction_score': auction_score,
                                    'factors': factors, 'confidence': confidence,
                                    'ai_score': _ai_raw, 'llm_score': _llm_cached,
                                    'whale_score': _ws if _ws is not None else 0, 'funding_rate': _fr, 'of_score': of_score,
                                    'regime': br.regime, 'regime_proba': list(br.regime_proba),
                                    'corr': round(_corr_val, 4), 'corr_override': _corr_val > _max_correlation,
                                    'reservation_id': _reservation_id,
                                })
                                _deduct_paper_balance(trade_amt)

                            else:
                                # Live mode: refresh balance and gate on available funds
                                _sync_live_balance(ex)
                                with _LIVE_BAL_LOCK:
                                    _avail = _LIVE_BALANCE
                                if trade_amt > _avail:
                                    logging.warning(f"{sym} live order blocked: trade ${float(trade_amt):,.2f} > available ${float(_avail):,.2f}")
                                    continue
                                try:
                                    if direction == 'long':
                                        order = submit_order_with_retry(ex, 'create_market_buy_order', sym, float(position_size))
                                        filled = order.get('filled') if order.get('filled') is not None else order.get('amount', 0) or 0
                                        avg_price = order.get('average') or order.get('price') or current_price or 0
                                        side = 'buy'
                                    else:
                                        order = submit_order_with_retry(ex, 'create_market_sell_order', sym, float(position_size))
                                        filled = order.get('filled') if order.get('filled') is not None else order.get('amount', 0) or 0
                                        avg_price = order.get('average') or order.get('price') or current_price or 0
                                        side = 'sell'
                                    actual_fee = float(fee) * float(filled) * float(avg_price)
                                    if direction == 'long':
                                        slippage = (Decimal(str(avg_price)) - current_price) / current_price
                                    else:
                                        slippage = (current_price - Decimal(str(avg_price))) / current_price
                                    order_id = order.get('id', 'unknown')
                                    logging.info(f"{sym} {direction.upper()} {order_id} @ {avg_price:,.2f}, filled={filled}")
                                    _sync_live_balance(ex, force=True)  # refresh balance after entry
                                    log_fill(sym, side, order_id, filled, avg_price, actual_fee, signals=sg)
                                    filled_price = Decimal(str(avg_price))
                                    filled_size = Decimal(str(filled))
                                    tp_dist = filled_price * TP_PCT
                                    _atr_med = float(st.df['atr'].rolling(50).median().iloc[-1]) if 'atr' in st.df.columns and len(st.df) >= 50 else None
                                    _atr_now = float(st.df['atr'].iloc[-1]) if 'atr' in st.df.columns and len(st.df) > 0 else None
                                    if _atr_med and _atr_now and not pd.isna(_atr_med) and not pd.isna(_atr_now) and _atr_med > 0:
                                        _tp_scale = Decimal(str(max(0.8, min(1.0 / (_atr_now / _atr_med), 1.5))))
                                    else:
                                        _tp_scale = Decimal('1')
                                    _tp1m, _tp2m, _tp3m = _get_tp_mults(br.regime)
                                    if direction == 'long':
                                        stop_loss = filled_price * (1 - SL_PCT)
                                        tp1 = filled_price + tp_dist * _tp1m * _tp_scale
                                        tp2 = filled_price + tp_dist * _tp2m * _tp_scale
                                        tp3 = filled_price + tp_dist * _tp3m * _tp_scale
                                        exit_side = 'sell'
                                    else:
                                        stop_loss = filled_price * (1 + SL_PCT)
                                        tp1 = filled_price - tp_dist * _tp1m * _tp_scale
                                        tp2 = filled_price - tp_dist * _tp2m * _tp_scale
                                        tp3 = filled_price - tp_dist * _tp3m * _tp_scale
                                        exit_side = 'buy'
                                    sl_order = None
                                    tp_order = None
                                    try:
                                        sl_order = ex.create_order(sym, 'stop-loss', exit_side, float(filled_size), params={'price': float(ex.price_to_precision(sym, float(stop_loss)))})
                                        logging.info(f"{sym} SL order placed: {sl_order.get('id', 'unknown')} @ {stop_loss:,.2f}")
                                    except Exception as sl_err:
                                        logging.error(f"{sym} SL order failed: {sl_err}")
                                    try:
                                        tp_order = ex.create_order(sym, 'limit', exit_side, float(filled_size), price=float(tp1))
                                        logging.info(f"{sym} TP1 order placed: {tp_order.get('id', 'unknown')} @ {tp1:,.2f}")
                                    except Exception as tp_err:
                                        logging.error(f"{sym} TP order failed: {tp_err}")
                                    try:
                                        deliver_signal(
                                            signal_type=direction, symbol=sym, direction=direction,
                                            price=float(avg_price), sl=float(stop_loss),
                                            tp1=float(tp1), tp2=float(tp2), tp3=float(tp3),
                                            confidence=float(br.confidence) if hasattr(br, 'confidence') else None,
                                            reason="confluence entry"
                                        )
                                    except Exception as e:
                                        logging.warning(f"Signal delivery failed: {e}")
                                    try:
                                        _send_trade_card_open(sym, direction, avg_price, stop_loss,
                                                             tp1, tp2, tp3, filled_size,
                                                             float(br.confidence) if hasattr(br, 'confidence') else None,
                                                             br.regime, sg)
                                    except Exception as e:
                                        logging.warning(f"Card send (live open) failed: {e}")
                                    _log_trade_marker('open', sym, direction, filled_price)
                                    st.set_position({
                                        'symbol': sym, 'direction': direction,
                                        'e': filled_price, 'sl': stop_loss, 'orig_sl': stop_loss,
                                        'tp1': tp1, 'tp2': tp2, 'tp3': tp3, 'tp_hit': 0,
                                        'opened_at': time.time(), 'last_tp_time': 0,
                                        'sg': sg, 'size': filled_size,
                                        'order_id': order_id, 'fee': fee,
                                        'sl_order_id': sl_order.get('id') if sl_order else None,
                                        'tp_order_id': tp_order.get('id') if tp_order else None,
                                        'filled': filled, 'slippage': slippage,
                                        'atr_pct': atr_pct, 'auction_score': auction_score,
                                        'factors': factors, 'confidence': confidence,
                                    'whale_score': _ws if _ws is not None else 0, 'funding_rate': _fr, 'of_score': of_score,
                                        'regime': br.regime, 'regime_proba': list(br.regime_proba),
                                        'corr': round(_corr_val, 4), 'corr_override': _corr_val > _max_correlation,
                                        'reservation_id': _reservation_id,
                                    })
                                except Exception as order_err:
                                    logging.error(f"{sym} entry order failed: {order_err}")

            if current_pos:
                direction = current_pos.get('direction', _MODE)
                tp_hit = current_pos.get('tp_hit', 0)
                elapsed_h = (time.time() - current_pos.get('opened_at', time.time())) / 3600

                # Check TP levels (highest first) — locked to prevent concurrent mutation
                _sl_moved = False
                _trail_moved = False
                _sl_update_id = None
                _sl_new_price = None
                _sl_filled_size = None
                with st._lock:
                    current_pos = st.pos  # re-read inside lock
                    if current_pos is None:
                        continue

                    # UPGRADE E: Volatility-adjusted dynamic TPs — recalculate TP2/TP3
                    # using CURRENT ATR instead of entry ATR. In trending markets ATR
                    # expands and fixed TP3 at 2R leaves money on the table.
                    # Only adjust upward (never shrink targets below original).
                    if 'atr' in st.df.columns and len(st.df) > 0 and current_pos.get('e'):
                        _cur_atr_raw = st.df['atr'].iloc[-1]
                        _cur_atr = float(_cur_atr_raw) if not pd.isna(_cur_atr_raw) else None
                        _entry_atr_pct = current_pos.get('atr_pct', 0.02)
                        _entry_price = float(current_pos['e'])
                        if _cur_atr is not None and _entry_price > 0:
                            _cur_atr_pct = _cur_atr / _entry_price
                            # Only expand TPs when current ATR > entry ATR (trending market)
                            if _cur_atr_pct > _entry_atr_pct * 1.1:  # 10% threshold to avoid noise
                                _atr_ratio = _cur_atr_pct / (_entry_atr_pct + 1e-9)
                                _atr_ratio = min(_atr_ratio, 2.0)  # cap expansion at 2x
                                _tp_regime = current_pos.get('regime', 'chop')
                                _, _dtp2m, _dtp3m = _get_tp_mults(_tp_regime)
                                _d_tp_dist = _entry_price * _cur_atr_pct * 1.2  # current ATR-based distance
                                if direction == 'long':
                                    _new_tp2 = Decimal(str(_entry_price + _d_tp_dist * float(_dtp2m)))
                                    _new_tp3 = Decimal(str(_entry_price + _d_tp_dist * float(_dtp3m)))
                                    # Only ratchet UP — never shrink targets
                                    if _new_tp2 > current_pos.get('tp2', Decimal('0')):
                                        current_pos['tp2'] = _new_tp2
                                    if _new_tp3 > current_pos.get('tp3', Decimal('0')):
                                        current_pos['tp3'] = _new_tp3
                                else:
                                    _new_tp2 = Decimal(str(_entry_price - _d_tp_dist * float(_dtp2m)))
                                    _new_tp3 = Decimal(str(_entry_price - _d_tp_dist * float(_dtp3m)))
                                    # Only ratchet DOWN (toward more profit for shorts)
                                    if _new_tp2 < current_pos.get('tp2', Decimal('999999')):
                                        current_pos['tp2'] = _new_tp2
                                    if _new_tp3 < current_pos.get('tp3', Decimal('999999')):
                                        current_pos['tp3'] = _new_tp3

                    prev_tp_hit = tp_hit
                    for lvl in [3, 2, 1]:
                        tp_key = f'tp{lvl}'
                        if tp_key not in current_pos:
                            continue
                        tp_price = current_pos[tp_key]
                        if lvl > tp_hit:
                            hit = (direction == 'long' and current_price >= tp_price) or \
                                  (direction != 'long' and current_price <= tp_price)
                            if hit:
                                current_pos['tp_hit'] = lvl
                                current_pos['last_tp_time'] = time.time()
                                tp_hit = lvl
                                logging.info(f"{sym} TP{lvl} HIT @ {current_price:,.2f}")
                    # Apply side effects for all newly crossed TP levels
                    if tp_hit >= 1 and prev_tp_hit < 1:
                        # SL to entry + 0.5*ATR (small cushion above breakeven)
                        _atr_raw = st.df['atr'].iloc[-1] if 'atr' in st.df.columns and len(st.df) > 0 else 0
                        atr_val = float(_atr_raw) if not pd.isna(_atr_raw) else 0
                        if direction == 'long':
                            current_pos['sl'] = current_pos['e'] + Decimal(str(atr_val * 0.5))
                        else:
                            current_pos['sl'] = current_pos['e'] - Decimal(str(atr_val * 0.5))
                        logging.info(f"{sym} SL moved to entry+0.5ATR @ {float(current_pos['sl']):,.2f}")
                        _sl_moved = True
                    if tp_hit >= 2 and prev_tp_hit < 2:
                        current_pos['trail_active'] = True
                        current_pos['trail_activated_at'] = time.time()
                        logging.info(f"{sym} trailing ATR stop activated after TP2")
                    if tp_hit > prev_tp_hit:
                        st.pos = current_pos
                        st._save_position()
                    # Snapshot for exchange SL update (API calls done outside lock)
                    if _sl_moved and not _is_paper() and current_pos.get('sl_order_id'):
                        _sl_update_id = current_pos['sl_order_id']
                        _sl_new_price = float(current_pos['sl'])
                        _sl_filled_size = float(current_pos.get('filled', current_pos.get('size', 0)))

                    # Keltner channel trailing stop after TP2 (skip activation tick)
                    # EMA21-anchored: smoother than HH-based chandelier, no spike-window artifacts
                    # Tightens as profit grows: 2.5*ATR after TP2, 1.5*ATR after TP3
                    # Floored at TP1 lock-in (entry+0.5*ATR) so locked profit is never returned
                    trail_age = time.time() - current_pos.get('trail_activated_at', 0)
                    if current_pos.get('trail_active') and trail_age > iv and 'atr' in st.df.columns and 'e21' in st.df.columns and len(st.df) >= 22:
                        atr_val = float(st.df['atr'].iloc[-1])
                        ema21 = float(st.df['e21'].iloc[-1])
                        kc_mult = 1.5 if tp_hit >= 3 else 2.5
                        if direction == 'long':
                            floor = float(current_pos['e']) + atr_val * 0.5
                            trail = Decimal(str(max(ema21 - kc_mult * atr_val, floor)))
                            if trail > current_pos['sl']:
                                current_pos['sl'] = trail
                                _trail_moved = True
                        else:
                            ceil = float(current_pos['e']) - atr_val * 0.5
                            trail = Decimal(str(min(ema21 + kc_mult * atr_val, ceil)))
                            if trail < current_pos['sl']:
                                current_pos['sl'] = trail
                                _trail_moved = True
                        if _trail_moved:
                            st._save_position()
                        # Snapshot for trailing SL update (API calls done outside lock)
                        if _trail_moved and not _is_paper() and current_pos.get('sl_order_id'):
                            _sl_update_id = current_pos['sl_order_id']
                            _sl_new_price = float(current_pos['sl'])
                            _sl_filled_size = float(current_pos.get('filled', current_pos.get('size', 0)))

                # Exchange SL cancel+create outside lock to avoid holding lock during API calls
                if _sl_update_id and _sl_new_price is not None:
                    exit_side = 'sell' if direction == 'long' else 'buy'
                    _sl_cancel_ok = False
                    try:
                        ex.cancel_order(_sl_update_id, sym)
                        _sl_cancel_ok = True
                    except ccxt.OrderNotFound:
                        _sl_cancel_ok = True  # already gone
                        logging.info(f"{sym} SL update: old order {_sl_update_id} already gone — OK")
                    except Exception as _sl_cancel_err:
                        logging.error(f"{sym} SL update CANCEL FAILED ({_sl_update_id}): {_sl_cancel_err} — will attempt create anyway (duplicate SL risk)")
                    # Immediately null out sl_order_id and persist (crash-safe)
                    with st._lock:
                        current_pos = st.pos
                        if current_pos:
                            current_pos['sl_order_id'] = None
                            st.pos = current_pos; st._save_position()
                    try:
                        new_sl = ex.create_order(sym, 'stop-loss', exit_side, float(_sl_filled_size), params={'price': float(ex.price_to_precision(sym, float(_sl_new_price)))})
                        with st._lock:
                            current_pos = st.pos
                            if current_pos:
                                current_pos['sl_order_id'] = new_sl.get('id')
                                st.pos = current_pos; st._save_position()
                        _label = 'trailing SL' if _trail_moved else 'SL'
                        logging.info(f"{sym} exchange {_label} updated to {_sl_new_price:,.2f}")
                    except Exception as sl_err:
                        logging.critical(f"{sym} EXCHANGE SL CREATE FAILED — position software-managed only: {sl_err}")

                # Compute current factors for exit evaluations (factor decay + AI exit)
                _exit_factors = None
                if ((tp_hit == 0 and elapsed_h > 12) or (tp_hit >= 1 and elapsed_h > 1)) and 'atr' in st.df.columns:
                    try:
                        _fd_htf = get_htf_bias(ex, sym)
                        _fd_of = compute_order_flow_factor(ex, sym)
                        _exit_factors = compute_factors(st.df, float(current_price),
                                    compute_volume_profile(st.df.tail(50)) if len(st.df) >= 40 else None,
                                    find_order_blocks(st.df.tail(30)) if len(st.df) >= 20 else None,
                                    _fd_htf, br.regime, of_score=_fd_of, short_mode=(_MODE == 'short'))
                    except Exception as _ef_err:
                        logging.warning(f"{sym} exit factor computation failed: {_ef_err}")

                # Factor decay exit: momentum collapsed vs entry (pre-TP1 only)
                factor_exit = False
                if tp_hit == 0 and elapsed_h > 12 and _exit_factors:
                    try:
                        _cur_c = compute_confidence(_exit_factors, br.regime, brain=br)
                        _entry_c = float(current_pos.get('confidence', 0.5))
                        if _entry_c > 0 and _cur_c < _entry_c * 0.5:
                            factor_exit = True
                            logging.info(f"{sym} factor decay exit: conf {_cur_c:.3f} < 50% of entry {_entry_c:.3f}")
                    except Exception as _fd_err:
                        logging.warning(f"{sym} factor decay check failed: {_fd_err}")

                # Time-based exit (backstop for dead capital)
                time_exit = False
                if tp_hit == 0 and elapsed_h > 96:
                    time_exit = True
                    logging.info(f"{sym} time exit: 96h with no TP1")
                elif tp_hit >= 1:
                    last_tp_t = current_pos.get('last_tp_time', 0)
                    since_tp_h = (time.time() - last_tp_t) / 3600 if last_tp_t else elapsed_h
                    if since_tp_h > 48:
                        time_exit = True
                        logging.info(f"{sym} time exit: 48h since last TP")

                # AI exit advisor: Ollama cached score vs entry score (after TP1, every 5 min)
                ai_exit = False
                _ai_exit_age = time.time() - current_pos.get('_ai_exit_ts', 0)
                if tp_hit >= 1 and elapsed_h > 1 and _ai_exit_age > 300 and is_ai_enabled():
                    try:
                        from goldeneye_ai import get_cached_ollama_score
                        _exit_ai = get_cached_ollama_score(sym)
                        _entry_ai = current_pos.get('ai_score')
                        if _exit_ai is not None:
                            with st._lock:
                                if st.pos: st.pos['_ai_exit_ts'] = time.time()
                            # Exit if Ollama score dropped sharply vs entry
                            if _exit_ai < 0.3 and (_entry_ai is None or _exit_ai < _entry_ai * 0.5):
                                ai_exit = True
                                logging.info(f"{sym} AI exit: Ollama {_exit_ai:.3f} vs entry {_entry_ai or 'N/A'}")
                    except Exception as _aie_err:
                        logging.warning(f"{sym} AI exit advisor failed: {_aie_err}")

                # Compute exit conditions — must be defined before bus stop-tightening
                exit_triggered = False

                # Exit: SL hit, TP3 reached, exhaustion, factor decay, time exit, or AI exit
                exhausted = tp_hit >= 1 and is_exhausted(st.df, direction)
                if exhausted:
                    logging.info(f"{sym} trend exhaustion detected (TP{tp_hit} banked)")
                if direction == 'long':
                    sl_hit = current_price <= current_pos['sl']
                    exit_triggered = sl_hit or tp_hit >= 3 or exhausted or time_exit or factor_exit or ai_exit
                else:
                    sl_hit = current_price >= current_pos['sl']
                    exit_triggered = sl_hit or tp_hit >= 3 or exhausted or time_exit or factor_exit or ai_exit

                if exit_triggered:
                    sym_lock = _get_sym_lock(sym)
                    with sym_lock:
                        # Re-check position still exists under lock (another thread may have exited it)
                        with st._lock:
                            current_pos = st.pos
                            if current_pos is None:
                                continue
                            # Snapshot position data under lock before doing I/O
                            pos_snap = dict(current_pos)
                        filled_size = Decimal(str(pos_snap.get('filled', pos_snap.get('size', 0))))
                        fee = Decimal(str(pos_snap.get('fee', get_fee(sym))))
                        # Continuous R-multiple based on actual PnL vs risk distance
                        risk_dist = abs(float(pos_snap['e']) - float(pos_snap.get('orig_sl', pos_snap['sl'])))
                        if risk_dist > 0:
                            if direction == 'long':
                                r = (float(current_price) - float(pos_snap['e'])) / risk_dist
                            else:
                                r = (float(pos_snap['e']) - float(current_price)) / risk_dist
                        else:
                            r = tp_hit if tp_hit > 0 else -1

                        if _is_paper():
                            # Apply exit slippage (adverse direction)
                            if direction == 'long':
                                current_price = float(current_price) * (1 - _PAPER_SLIP)
                            else:
                                current_price = float(current_price) * (1 + _PAPER_SLIP)
                            # Recompute R with slippage-adjusted exit price
                            if risk_dist > 0:
                                r = (float(current_price) - float(pos_snap['e'])) / risk_dist if direction == 'long' else (float(pos_snap['e']) - float(current_price)) / risk_dist
                            logging.info(f"{sym} PAPER EXIT {direction.upper()} size={filled_size:,.4f} @ {_fp(current_price)} TP{tp_hit if tp_hit > 0 else 'SL'}")
                        else:
                            # Cancel stale SL/TP orders before placing exit
                            for oid_key in ('sl_order_id', 'tp_order_id'):
                                oid = pos_snap.get(oid_key)
                                if oid:
                                    try: ex.cancel_order(oid, sym)
                                    except Exception: pass
                            try:
                                exit_side = 'sell' if direction == 'long' else 'buy'
                                # Fix 2: reconcile stored size against actual Kraken balance before exit
                                # Partial fills at entry may mean we hold less than filled_size records
                                _base_asset = sym.split('/')[0]
                                try:
                                    _live_bal = ex.fetch_balance()
                                    _actual_held = Decimal(str(_live_bal.get(_base_asset, {}).get('free', 0) or 0))
                                    if _actual_held > 0 and _actual_held < filled_size:
                                        logging.warning(f"{sym} exit size reconciled: stored={filled_size:,.6f} actual_held={_actual_held:,.6f} — using actual")
                                        filled_size = _actual_held
                                except Exception as _bal_err:
                                    logging.warning(f"{sym} balance fetch for exit reconcile failed: {_bal_err} — using stored size")
                                if direction == 'long':
                                    ex.create_market_sell_order(sym, float(filled_size))
                                else:
                                    ex.create_market_buy_order(sym, float(filled_size))
                                logging.info(f"{sym} {exit_side.upper()} FILLED: size={filled_size:,.4f} @ {current_price:,.2f}")
                                _sync_live_balance(ex, force=True)  # refresh balance after exit
                            except Exception as order_err:
                                logging.critical(f"{sym} EXIT ORDER FAILED — position still open on exchange: {order_err}")
                                # Fix 3: on failure, re-query Kraken actual balance and retry with real held quantity
                                try:
                                    _base_asset = sym.split('/')[0]
                                    _retry_bal = ex.fetch_balance()
                                    _retry_held = Decimal(str(_retry_bal.get(_base_asset, {}).get('free', 0) or 0))
                                    if _retry_held > 0:
                                        logging.warning(f"{sym} exit retry with kraken-actual qty={_retry_held:,.6f} (was {filled_size:,.6f})")
                                        if direction == 'long':
                                            ex.create_market_sell_order(sym, float(_retry_held))
                                        else:
                                            ex.create_market_buy_order(sym, float(_retry_held))
                                        logging.info(f"{sym} exit retry SUCCEEDED with actual qty={_retry_held:,.6f}")
                                        _sync_live_balance(ex, force=True)
                                    else:
                                        logging.critical(f"{sym} exit retry aborted: no {_base_asset} balance on exchange")
                                        time.sleep(iv)
                                        continue
                                except Exception as retry_err:
                                    logging.critical(f"{sym} exit retry also failed: {retry_err}")
                                    time.sleep(iv)  # avoid tight retry loop against a down exchange
                                    continue  # do NOT clear position; retry next iteration

                        if direction == 'long':
                            pnl = (float(current_price) - float(pos_snap['e'])) * float(filled_size) - (float(current_price) * float(fee) * float(filled_size) + float(pos_snap['e']) * float(fee) * float(filled_size))
                        else:
                            pnl = (float(pos_snap['e']) - float(current_price)) * float(filled_size) - (float(current_price) * float(fee) * float(filled_size) + float(pos_snap['e']) * float(fee) * float(filled_size))
                        update_circuit_breaker(float(pnl))

                        # Clear position FIRST — all post-trade processing uses pos_snap
                        with _sym_cooldown_lock:
                            _sym_cooldown[sym] = time.time()
                        st.clear_position()
                        _xt = f'TP{tp_hit}' if tp_hit >= 3 else 'EXH' if exhausted else 'FDEC' if factor_exit else 'AI' if ai_exit else 'TIME' if time_exit else 'SL'
                        logging.info(f"{sym} {direction.upper()} EXIT {_xt} {r:+.1f}R @ {current_price:,.2f}, PnL={pnl:.2f}")

                        # Post-trade processing — uses pos_snap (position already cleared)
                        try:
                            exit_side_log = 'sell' if direction == 'long' else 'buy'
                            log_fill(sym, exit_side_log, pos_snap.get('order_id', 'unknown'), float(filled_size), float(current_price), float(fee) * float(filled_size) * float(current_price), float(pnl))
                            br.record(pos_snap['sg'], r, pos_snap.get('slippage', Decimal('0')), direction, factors=pos_snap.get('factors'), rgm=pos_snap.get('regime'))
                            # UPGRADE D: Record LLM prediction vs outcome for Brier scoring
                            _entry_llm = pos_snap.get('llm_score')
                            if _entry_llm is not None:
                                _record_llm_prediction(_entry_llm, r)
                            _td = _build_trade_detail(pos_snap, current_price, tp_hit=tp_hit)
                            record_live_trade(direction, float(r), float(pnl), sym=sym, exit_type=_xt, detail=_td)
                            _log_factor_trade(sym, pos_snap, r, float(pnl))
                            deliver_signal(
                                signal_type='exit', symbol=sym, direction=direction,
                                price=float(pos_snap['e']),
                                sl=float(pos_snap.get('orig_sl', pos_snap['sl'])),
                                tp1=float(pos_snap['tp1']), tp2=float(pos_snap['tp2']),
                                tp3=float(pos_snap['tp3']),
                                confidence=float(br.confidence) if hasattr(br, 'confidence') else None,
                                exit_price=float(current_price), pnl=float(pnl),
                                tp_hit=tp_hit, r_multiple=float(r)
                            )
                            _send_trade_card_close(sym, direction, pos_snap, current_price,
                                                   pnl, r, tp_hit=tp_hit, exit_type=_xt)
                            _log_trade_marker('close', sym, direction, current_price, r=r, pnl=pnl)
                        except Exception as post_err:
                            logging.warning(f"{sym} post-trade processing error (position already cleared): {post_err}")

            time.sleep(iv)
        except Exception as e:
            logging.error(f"{sym}: {e}")
            time.sleep(10)

# =============================================================================
# Rich TUI — Command Center
# =============================================================================
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.console import Console

# TUI color palette (matches dashboard)
_C = {'gold': '#c9a227', 'green': '#22c55e', 'red': '#ef4444', 'amber': '#f59e0b',
      'dim': '#4a4a55', 'muted': '#7a7a8a', 'text': '#e5e5e5'}

_SIG_DESC_LONG = {'a':'EMA Cross Up','b':'Bull Engulfing','c':'RSI<30 Cross','d':'BB Lower Bounce',
             'e':'EMA Pullback Buy','f':'MACD Hist Rising','g':'RSI Bull Range','h':'BB %B Recovery',
             'i':'MFI Oversold','j':'OBV Breakout','k':'BB Squeeze Up','l':'Hist Bull Div',
             'm':'MFI Bull Range','n':'Volume Surge','o':'ADX Trend Str','p':'Stoch Oversold',
             'q':'Hammer Candle','r':'MACD Zero Cross','s':'ATR Expansion','t':'Chop Exit',
             'u':'Stoch Momentum','v':'EMA200 Reclaim','w':'OBV Accumulate','x':'Triple Conflu.',
             'y':'BB Mid Reclaim','z':'Vol+Trend Conf.','2':'Chop+MFI'}
_SIG_DESC_SHORT = {'a':'EMA Cross Down','b':'Bear Engulfing','c':'RSI>70 Fall','d':'BB Upper Reject',
             'e':'EMA Pullback Sell','f':'MACD Hist Fall','g':'RSI Bear Range','h':'BB %B Reject',
             'i':'MFI Overbought','j':'OBV Breakdown','k':'BB Squeeze Down','l':'Hist Bear Div',
             'm':'MFI Bear Range','n':'Vol Surge Red','o':'ADX Trend Str','p':'Stoch Overbought',
             'q':'Shooting Star','r':'MACD Zero Down','s':'ATR Expansion','t':'Chop Exit',
             'u':'Stoch Bear Mom','v':'EMA200 Reject','w':'OBV Distribute','x':'Triple Conflu.',
             'y':'BB Mid Reject','z':'Vol+Down Conf.','2':'Chop+MFI Bear'}
_SIG_DESC = _SIG_DESC_LONG  # reassigned in main() via _MODE

_RGM_CLR = {'bull': _C['green'], 'bear': _C['red'], 'range': _C['amber'],
            'chop': _C['dim']}

def _build_logo():
    """3-line gold eye logo for TUI header."""
    t = Text()
    g = _C['gold']
    a = _C['amber']
    d = _C['dim']
    t.append("    ◜", style=d)
    t.append(" ━━━━━ ", style=g)
    t.append("◝\n", style=d)
    t.append("   ◖", style=f"bold {g}")
    t.append("  ◉  ", style=f"bold {a}")
    t.append("◗\n", style=f"bold {g}")
    t.append("    ◟", style=d)
    t.append(" ━━━━━ ", style=g)
    t.append("◞", style=d)
    return t



def _build_symbols_table(sts, brs):
    t = Table(show_header=True, header_style=f"bold {_C['muted']}", box=None, padding=(0,1), expand=True)
    t.add_column("Symbol", min_width=8)
    t.add_column("Price", min_width=8, justify="right")
    t.add_column("P/L", min_width=6, justify="right")
    t.add_column("Rgm", min_width=5, justify="center")
    t.add_column("WR%", min_width=3, justify="right")
    for sy, s in sorted(sts.items()):
        b = brs[sy]
        price, last_price = s.get_price()
        pos = s.get_position()
        up = price >= last_price
        pc = _C['green'] if up else _C['red']
        arrow = "↑" if up else "↓"
        price_t = Text(f"{arrow}{_fp(price)}", style=pc)
        if pos:
            d = pos.get('direction', _MODE)
            sz = float(pos.get('size', 0))
            fp, ep = float(price), float(pos['e'])
            upl = (fp - ep) * sz if d == 'long' else (ep - fp) * sz
            plc = _C['green'] if upl >= 0 else _C['red']
            pl_t = Text(f"{'+' if upl >= 0 else ''}{upl:,.2f}", style=plc)
        else:
            pl_t = Text("—", style=_C['dim'])
        rgm = b.regime if hasattr(b, 'regime') else '—'
        rgm_t = Text(rgm, style=_RGM_CLR.get(rgm, _C['dim']))
        w = b.wr()
        wr_c = _C['green'] if w >= 50 else _C['amber'] if w > 0 else _C['dim']
        wr_t = Text(f"{w:.0f}" if w > 0 else "—", style=wr_c)
        sym_style = f"bold {_C['gold']}" if pos else _C['text']
        t.add_row(Text(sy, style=sym_style), price_t, pl_t, rgm_t, wr_t)
    return t

def _build_positions_table(sts, brs):
    t = Table(show_header=True, header_style=f"bold {_C['muted']}", box=None, padding=(0,1), expand=True)
    t.add_column("Symbol", style=f"bold {_C['text']}", min_width=8)
    t.add_column("Dir", min_width=3, justify="center")
    t.add_column("Entry", min_width=8, justify="right")
    t.add_column("P/L", min_width=6, justify="right")
    t.add_column("SL", min_width=8, justify="right")
    t.add_column("TP1", min_width=6, justify="right")
    t.add_column("TP2", min_width=6, justify="right")
    t.add_column("TP3", min_width=6, justify="right")
    t.add_column("Age", min_width=5, justify="right")
    has_pos = False
    for sy, s in sorted(sts.items()):
        pos = s.get_position()
        if not pos: continue
        has_pos = True
        b = brs[sy]
        d = pos.get('direction', _MODE)
        dc = _C['green'] if d == 'long' else _C['red']
        tp_hit = pos.get('tp_hit', 0)
        # Current price + unrealized P/L
        price, _ = s.get_price()
        fp, ep = float(price), float(pos['e'])
        sz = float(pos.get('size', 0))
        upl = (fp - ep) * sz if d == 'long' else (ep - fp) * sz
        plc = _C['green'] if upl >= 0 else _C['red']
        def tp_val(key, lvl):
            v = pos.get(key)
            if v is None: return Text("—", style=_C['dim'])
            c = _C['green'] if tp_hit >= lvl else _C['dim']
            return Text(_fp(v), style=c)
        age_s = int(time.time() - pos.get('opened_at', time.time()))
        age_h, age_m = age_s // 3600, (age_s % 3600) // 60
        age_str = f"{age_h}h{age_m:02d}m" if age_h > 0 else f"{age_m}m"
        age_c = _C['amber'] if age_h > 50 else _C['text']
        t.add_row(
            sy, Text(d[0].upper(), style=f"bold {dc}"),
            Text(_fp(ep), style=_C['muted']),
            Text(f"{'+' if upl >= 0 else ''}{upl:,.2f}", style=plc),
            Text(_fp(pos['sl']), style=_C['red']),
            tp_val('tp1', 1), tp_val('tp2', 2), tp_val('tp3', 3),
            Text(age_str, style=age_c)
        )
    if not has_pos:
        t.add_row(Text("No open positions", style=_C['dim']),
                  "", "", "", "", "", "", "", "")
    return t

_kraken_status = {'ok': True, 'checked': 0}


def _build_signals_panel(brs):
    t = Table(show_header=True, header_style=f"bold {_C['muted']}", box=None, padding=(0,1), expand=True)
    t.add_column("Sig", min_width=3, style=f"bold {_C['text']}")
    t.add_column("Desc", min_width=10, style=_C['muted'])
    t.add_column("W/F", min_width=5, justify="right")
    t.add_column("WR%", min_width=4, justify="right")
    for sig in AdaptiveBrain.SIG:
        tf = sum(b.stats[sig]['f'] for b in brs.values())
        tw = sum(b.stats[sig]['w'] for b in brs.values())
        wr = tw / tf * 100 if tf else 0
        wrc = _C['green'] if wr >= 50 else _C['amber'] if tf > 0 else _C['dim']
        t.add_row(sig.upper(), _SIG_DESC.get(sig, '—'), f"{tw}/{tf}",
                  Text(f"{wr:.0f}%" if tf else "—", style=wrc))
    return Panel(t, title=Text("SIGNALS", style=f"bold {_C['gold']}"), border_style=_C['dim'], expand=True)

def _build_analytics_panel(m=None):
    if m is None: m = get_live_metrics()
    lines = []
    # --- Current Session (this run) ---
    up_h = m['uptime_hours']
    h, mn = int(up_h), int((up_h % 1) * 60)
    lines.append(Text(f"── This Session ──", style=f"bold {_C['gold']}"))
    lines.append(Text.assemble(("Uptime   ", _C['muted']), (f"{h}h {mn}m", _C['text'])))
    s_wr = m['session_wr'] * 100
    s_wrc = _C['green'] if s_wr >= 50 else _C['amber'] if m['session_trades'] > 0 else _C['dim']
    lines.append(Text.assemble(("Trades   ", _C['muted']), (f"{m['session_trades']}", _C['text'])))
    lines.append(Text.assemble(("WR       ", _C['muted']), (f"{s_wr:.1f}%", s_wrc)))
    s_pnl_c = _C['green'] if m['session_pnl'] >= 0 else _C['red']
    lines.append(Text.assemble(("P/L      ", _C['muted']), (f"${m['session_pnl']:+,.2f}", s_pnl_c)))
    # --- Lifetime (all trades ever) ---
    lines.append(Text(f"── Lifetime ──", style=f"bold {_C['muted']}"))
    wr = m['win_rate'] * 100
    wrc = _C['green'] if wr >= 50 else _C['amber'] if m['trades'] > 0 else _C['dim']
    lines.append(Text.assemble(("Trades   ", _C['muted']), (f"{m['trades']}", _C['text'])))
    lines.append(Text.assemble(("WR       ", _C['muted']), (f"{wr:.1f}%", wrc)))
    lines.append(Text.assemble(("Avg R    ", _C['muted']), (f"{m['avg_r']:.2f}", _C['text'])))
    t_pnl_c = _C['green'] if m['total_pnl'] >= 0 else _C['red']
    lines.append(Text.assemble(("P/L      ", _C['muted']), (f"${m['total_pnl']:+,.2f}", t_pnl_c)))
    lines.append(Text.assemble(("Max DD   ", _C['muted']), (f"{m['max_drawdown']:.1f}%", _C['red'] if m['max_drawdown'] > 5 else _C['amber'])))
    content = Text("\n").join(lines)
    return Panel(content, title=Text("ANALYTICS", style=f"bold {_C['gold']}"), border_style=_C['dim'], expand=True)

def _build_log_panel():
    lines = list(_tui_logs) if _tui_logs else ["Waiting for log events..."]
    content = Text("\n".join(lines), style=_C['dim'])
    return Panel(content, title=Text("LOG", style=f"bold {_C['gold']}"), border_style=_C['dim'], expand=True)

def _build_trades_panel(m=None):
    if m is None: m = get_live_metrics()
    tlog = m.get('trade_log', [])
    t = Table(show_header=True, header_style=f"bold {_C['muted']}", box=None, padding=(0,1), expand=True)
    t.add_column("Symbol", min_width=8)
    t.add_column("Exit", min_width=4, justify="center")
    t.add_column("R", min_width=5, justify="right")
    t.add_column("P/L", min_width=7, justify="right")
    t.add_column("Age", min_width=5, justify="right")
    if not tlog:
        t.add_row(Text("No closed trades", style=_C['dim']), "", "", "", "")
    else:
        for tr in reversed(tlog[-8:]):
            sym = tr.get('sym', '?')
            xt = tr.get('exit', '?')
            rv = tr.get('r', 0)
            pv = tr.get('pnl', 0)
            rc = _C['green'] if rv > 0 else _C['red']
            pc = _C['green'] if pv >= 0 else _C['red']
            xc = _C['green'] if xt.startswith('TP') else _C['red'] if xt == 'SL' else _C['amber']
            ago_s = int(time.time() - tr.get('t', time.time()))
            if ago_s < 3600:
                ago = f"{ago_s // 60}m"
            elif ago_s < 86400:
                ago = f"{ago_s // 3600}h"
            else:
                ago = f"{ago_s // 86400}d"
            t.add_row(Text(sym, style=_C['text']), Text(xt, style=xc),
                      Text(f"{rv:+.1f}R", style=rc), Text(f"${pv:+.2f}", style=pc),
                      Text(ago, style=_C['dim']))
    return Panel(t, title=Text("TRADES", style=f"bold {_C['gold']}"), border_style=_C['dim'], expand=True)

def build_tui_layout(sts, brs, nm):
    live = get_live_metrics()
    open_count = sum(1 for s in sts.values() if s.get_position() is not None)
    total_pnl = live.get('total_pnl', 0.0)
    wr = live['win_rate'] * 100
    pnl_sign = '+' if total_pnl >= 0 else ''

    # Get balance for display
    if _is_paper():
        bal = _PAPER_BALANCE
        bal_str = f"${float(bal):,.0f} paper"
    else:
        bal = _LIVE_BALANCE
        bal_str = f"${float(bal):,.0f} live"

    # Header: logo + stats in a grid
    logo = _build_logo()
    stats = Text()
    _title = "G O L D E N E Y E  [SHORT]" if _MODE == 'short' else "G O L D E N E Y E"
    stats.append(f"{_title}\n", style=f"bold {_C['gold']}")
    stats.append(f"{bal_str}  •  ", style=f"bold {_C['green']}")
    stats.append(f"{len(sts)} symbols  •  ", style=_C['muted'])
    stats.append(f"{open_count} open", style=f"bold {_C['text']}")
    stats.append(f"  •  P/L ", style=_C['muted'])
    plc = _C['green'] if total_pnl >= 0 else _C['red']
    stats.append(f"${pnl_sign}{total_pnl:,.0f}", style=f"bold {plc}")
    stats.append(f"  •  WR ", style=_C['muted'])
    wrc = _C['green'] if wr >= 50 else _C['amber'] if wr > 0 else _C['dim']
    stats.append(f"{wr:.1f}%", style=f"bold {wrc}")

    hdr = Table.grid(padding=(0, 2))
    hdr.add_column(width=20)
    hdr.add_column(ratio=1)
    hdr.add_row(logo, stats)

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=5),
        Layout(name="top", ratio=3),
        Layout(name="mid", ratio=2),
        Layout(name="bot", size=12),
    )
    layout["header"].update(Panel(hdr, border_style=_C['gold']))
    # Top: symbols (compact) + positions (priority)
    layout["top"].split_row(
        Layout(Panel(_build_symbols_table(sts, brs),
                     title=Text("SYMBOLS", style=f"bold {_C['gold']}"),
                     border_style=_C['dim']), name="symbols", ratio=2),
        Layout(Panel(_build_positions_table(sts, brs),
                     title=Text("POSITIONS", style=f"bold {_C['gold']}"),
                     border_style=_C['dim']), name="positions", ratio=3),
    )
    # Mid: signals + analytics (system panel removed — info redundant with header)
    layout["mid"].split_row(
        Layout(_build_signals_panel(brs), name="signals"),
        Layout(_build_analytics_panel(live), name="analytics"),
    )
    # Bot: trades + log
    layout["bot"].split_row(
        Layout(_build_trades_panel(live), name="trades", ratio=3),
        Layout(_build_log_panel(), name="log", ratio=2),
    )

    return Panel(layout, border_style=_C['gold'], expand=True)

def run_tui(sts, brs, nm):
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    console = Console(force_terminal=True)
    try:
        with Live(build_tui_layout(sts, brs, nm), console=console,
                  refresh_per_second=0.5, screen=True) as live:
            while True:
                live.update(build_tui_layout(sts, brs, nm))
                time.sleep(2)
    except KeyboardInterrupt:
        pass

# Health monitoring
_health_state = {}

_health_lock = Lock()

def register_thread(name: str):
    with _health_lock:
        _health_state[name] = {'last_beat': time.time(), 'alive': True, 'alerted': False}

def heartbeat(name: str):
    with _health_lock:
        if name in _health_state:
            _health_state[name]['last_beat'] = time.time()
            # Clear alert flag on recovery
            if _health_state[name].get('alerted'):
                _health_state[name]['alerted'] = False
                _health_state[name]['alive'] = True

def health_monitor(interval=60):
    while True:
        time.sleep(interval)
        now = time.time()
        issues = []
        with _health_lock:
            for name, state in _health_state.items():
                if now - state['last_beat'] > interval * 3:
                    state['alive'] = False
                    if not state.get('alerted'):
                        issues.append(f"{name} not responding for {now - state['last_beat']:.0f}s")
                        state['alerted'] = True
        if issues:
            msg = f"{_BRAND} HEALTH ALERT\n" + "\n".join(f"  • {i}" for i in issues)
            logging.error(msg)
            send_alert(msg)

_health_thread = None
_health_server_thread = None
_http_server_running = True
_start_time = None

# Health snapshot — updated by background thread, read by HTTP handler.
# NEVER computed inline. Zero locks, zero network, zero imports in the handler path.
_health_snapshot = {"status": "starting", "kraken_api": "unknown", "trading_mode": "paper"}
_health_snap_lock = Lock()

def _update_health_snapshot():
    """Background job: rebuild health snapshot every 5s. Runs in its own thread."""
    import psutil
    while True:
        try:
            s = {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat() + "Z"}
            if _start_time:
                s["uptime_seconds"] = (datetime.now() - _start_time).total_seconds()
            s["active_symbols"] = len(sts) if 'sts' in globals() else 0
            try:
                s["cpu_percent"] = psutil.cpu_percent()
                s["memory_percent"] = psutil.virtual_memory().percent
            except Exception:
                pass
            if 'sts' in globals() and sts:
                s["threads_healthy"] = sum(1 for st in sts.values() if not st.shutdown.is_set())
                s["total_threads"] = len(sts)
            # Kraken: just check if exchange object has markets loaded
            s["kraken_api"] = "connected" if ('exchange' in globals() and exchange and getattr(exchange, 'markets', None)) else "error"
            s["trading_mode"] = "paper" if _is_paper() else "live"
            # Balances — only show what's relevant to current mode
            if _is_paper():
                try:
                    s["paper_balance"] = float(_PAPER_BALANCE)
                except Exception:
                    s["paper_balance"] = 0.0
            else:
                # Refresh live balance on every snapshot build — non-forced, so
                # _sync_live_balance's internal 60s cache still guards the Kraken API.
                # Effect: header balance staleness drops from 5 min to <=60 s.
                try:
                    if 'exchange' in globals() and exchange:
                        _sync_live_balance(exchange)
                except Exception as _bal_err:
                    logging.debug(f"health-snapshot balance sync failed: {_bal_err}")
                try:
                    s["live_balance"] = float(_LIVE_BALANCE)
                    s["live_trade_amt"] = float(_get_trade_amt())
                except Exception:
                    s["live_balance"] = 0.0
                    s["live_trade_amt"] = 0.0
            if 'sts' in globals() and sts:
                s["portfolio_heat"] = float(get_portfolio_heat(sts))
                s["open_positions"] = count_open_positions(sts)
            else:
                s["portfolio_heat"] = 0.0
                s["open_positions"] = 0
            s["drawdown_mult"] = get_drawdown_mult()
            s["max_positions"] = _MAX_OPEN_POS
            s["circuit_breaker_daily_pnl"] = _circuit_breaker.get('daily_pnl', 0.0)
            s["circuit_breaker_weekly_pnl"] = _circuit_breaker.get('weekly_pnl', 0.0)
            s["circuit_breaker_active"] = _circuit_breaker_tripped
            if any(v == "error" for v in s.values() if isinstance(v, str)):
                s["status"] = "degraded"
            with _health_snap_lock:
                global _health_snapshot
                _health_snapshot = s
        except Exception as e:
            logging.warning(f"Health snapshot update failed: {e}")
        time.sleep(5)

def _get_expanded_health():
    """Return pre-built snapshot. Instant. No computation. No locks beyond a dict copy."""
    with _health_snap_lock:
        return dict(_health_snapshot)

def _run_http_health_server():
    import http.server
    import socketserver
    class HealthHandler(http.server.BaseHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            super().end_headers()
        def do_OPTIONS(self):
            self.send_response(200)
            self.end_headers()
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                status = _get_expanded_health()
                response = json.dumps(status)
                self.wfile.write(response.encode())
            elif self.path == "/positions":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                positions = []
                if 'sts' in globals() and 'brs' in globals():
                    for sym, st in sts.items():
                        pos = st.get_position()
                        if pos:
                            br = brs.get(sym)
                            price, _ = st.get_price()
                            d = pos.get('direction', _MODE)
                            sz = float(pos.get('size', 0))
                            fp, ep = float(price), float(pos['e'])
                            upl = (fp - ep) * sz if d == 'long' else (ep - fp) * sz
                            risk_dist = abs(ep - float(pos.get('orig_sl', pos['sl'])))
                            r_mult = round((fp - ep) / risk_dist, 2) if d == 'long' and risk_dist > 0 else round((ep - fp) / risk_dist, 2) if risk_dist > 0 else 0
                            positions.append({
                                'symbol': sym,
                                'direction': d,
                                'signals': pos.get('sg', []),
                                'entry': _fp(pos['e']),
                                'size': sz,
                                'size_usd': round(sz * ep, 2),
                                'pnl': round(upl, 2),
                                'r_mult': r_mult,
                                'sl': _fp(pos['sl']),
                                'tp1': _fp(pos['tp1']) if 'tp1' in pos else None,
                                'tp2': _fp(pos['tp2']) if 'tp2' in pos else None,
                                'tp3': _fp(pos['tp3']) if 'tp3' in pos else None,
                                'tp_hit': pos.get('tp_hit', 0),
                                'opened_at': pos.get('opened_at', 0),
                                'confidence': float(br.confidence) if br and hasattr(br, 'confidence') else 0.5,
                                'regime': pos.get('regime', br.regime if br else 'chop'),
                            })
                self.wfile.write(json.dumps(positions).encode())
            elif self.path == "/analytics":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                m = get_live_metrics()
                # Per-signal breakdown from in-memory brains (same data TUI reads)
                per_signal = {}
                sym_stats = []
                if 'brs' in globals() and brs:
                    for sig in AdaptiveBrain.SIG:
                        tf = sum(b.stats[sig]['f'] for b in brs.values())
                        tw = sum(b.stats[sig]['w'] for b in brs.values())
                        per_signal[sig] = {'f': tf, 'w': tw}
                    for sym, br in sorted(brs.items()):
                        tf = sum(v['f'] for v in br.stats.values())
                        tw = sum(v['w'] for v in br.stats.values())
                        sym_stats.append({'symbol': sym, 'fired': tf, 'wins': tw})
                total_fired = sum(v['f'] for v in per_signal.values())
                total_wins = sum(v['w'] for v in per_signal.values())
                sig_wr = (total_wins / total_fired * 100) if total_fired else 0.0
                data = {
                    'trades': m['trades'], 'wins': m['wins'], 'losses': m['losses'],
                    'win_rate': round(m['win_rate'] * 100, 1),
                    'avg_r': round(m['avg_r'], 2),
                    'max_drawdown': round(m['max_drawdown'], 1),
                    'uptime_hours': round(m['uptime_hours'], 1),
                    'long_wins': m['long_wins'], 'long_trades': m['long_trades'],
                    'short_wins': m['short_wins'], 'short_trades': m['short_trades'],
                    'total_signals': total_fired, 'total_signal_wins': total_wins,
                    'signal_win_rate': round(sig_wr, 1),
                    'total_pnl': round(m['total_pnl'], 2),
                    'symbol_count': len(brs) if 'brs' in globals() else 0,
                    'per_signal': per_signal,
                    'sym_stats': sym_stats,
                    'trade_log': m.get('trade_log', []),
                    # Session stats (this run only)
                    'session_trades': m['session_trades'],
                    'session_wr': round(m['session_wr'] * 100, 1),
                    'session_pnl': round(m['session_pnl'], 2),
                }
                self.wfile.write(json.dumps(data).encode())
            elif self.path == "/metrics":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                status = _get_expanded_health()
                metrics = "\n".join([f"goldeneye_{k} {v}" for k, v in status.items() if isinstance(v, (int, float))])
                self.wfile.write(metrics.encode())
            elif self.path == "/symbols":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                syms = []
                if 'sts' in globals() and 'brs' in globals():
                    # Per-symbol historical P/L from trade log
                    sym_pnl = {}
                    with _metrics_lock:
                        for tr in _live_metrics.get('trade_log', []):
                            s = tr.get('sym', '')
                            sym_pnl[s] = sym_pnl.get(s, 0.0) + tr.get('pnl', 0.0)
                    for sym, st in sorted(sts.items()):
                        br = brs.get(sym)
                        price, last_price = st.get_price()
                        pos = st.get_position()
                        hist_pnl = round(sym_pnl.get(sym, 0.0), 2)
                        rgm = br.regime if br and hasattr(br, 'regime') else '—'
                        w = br.wr() if br else 0
                        syms.append({
                            'symbol': sym, 'price': _fp(price),
                            'up': float(price) >= float(last_price),
                            'pnl': hist_pnl, 'has_pos': pos is not None,
                            'regime': rgm,
                            'wr': round(w, 1),
                        })
                self.wfile.write(json.dumps(syms).encode())
            elif self.path == "/decisions":
                # Decision traceability — last N entry decisions with full factor breakdown
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                with _decision_log_lock:
                    decisions = list(_decision_log)
                self.wfile.write(json.dumps(decisions).encode())
            elif self.path.startswith("/api/trades"):
                # Broadcaster-compatible trade list for EOD card
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                today_start = datetime.strptime(today, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc).timestamp()
                trades = []
                with _metrics_lock:
                    for tr in _live_metrics.get('trade_log', []):
                        ts = tr.get('t', 0)
                        if ts < today_start:
                            continue
                        det = tr.get('detail', {})
                        entry_p = det.get('entry', 0)
                        exit_p = det.get('exit_price', 0)
                        size = det.get('size', 0)
                        fee_rate = 0.004
                        fees = round(fee_rate * size * (entry_p + exit_p), 4) if entry_p else 0
                        pnl = tr.get('pnl', 0)
                        trades.append({
                            "ts": ts,
                            "bot": "goldeneye",
                            "pair": tr.get('sym', ''),
                            "direction": tr.get('dir', 'L'),
                            "pnl": round(pnl + fees, 4),
                            "fees": fees,
                            "exit_reason": tr.get('exit', ''),
                        })
                self.wfile.write(json.dumps({"trades": trades}).encode())
            elif self.path.startswith("/api/expectancy"):
                # Broadcaster-compatible expectancy summary
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                m = get_live_metrics()
                pnl_list = m.get('pnl_history', [])
                avg_pnl = sum(pnl_list) / len(pnl_list) if pnl_list else 0
                self.wfile.write(json.dumps({
                    "fleet_expectancy": round(avg_pnl, 4),
                    "total_trades": m['trades'],
                    "win_rate": round(m['win_rate'] * 100, 1),
                    "total_pnl": round(m['total_pnl'], 2),
                    "bot_rankings": [{"bot": "goldeneye", "trades": m['trades'],
                                      "win_rate": round(m['win_rate'] * 100, 1),
                                      "net_pnl": round(m['total_pnl'], 2)}],
                }).encode())
            elif self.path.startswith("/api/events/recent"):
                # Stub — GoldenEye has no event bus, return empty
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"[]")
            elif self.path == "/logs":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(list(_tui_logs)).encode())
            elif self.path.startswith("/signal/"):
                sig = self.path[len("/signal/"):].lower()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                result = {'error': 'Invalid signal'}
                if sig in AdaptiveBrain.SIG and 'brs' in globals():
                    per_sym = []
                    total_f, total_w = 0, 0
                    for sym, br in sorted(brs.items()):
                        sd = br.stats.get(sig, {'f': 0, 'w': 0})
                        f, w = sd['f'], sd['w']
                        total_f += f
                        total_w += w
                        per_sym.append({
                            'symbol': sym,
                            'fired': f,
                            'wins': w,
                            'wr': round(w / f * 100, 1) if f > 0 else 0,
                        })
                    _SN = {
                        'a':'EMA Cross Up','b':'Bullish Engulfing','c':'RSI<30 Cross',
                        'd':'BB Lower Bounce','e':'EMA Pullback Buy','f':'MACD Hist Rising',
                        'g':'RSI Bull Range','h':'BB %B Recovery','i':'MFI Oversold Cross',
                        'j':'OBV Trend Breakout','k':'BB Squeeze Up','l':'Hist Bull Div',
                        'm':'MFI Bull Range','n':'Volume Surge','o':'ADX Trend Str',
                        'p':'Stoch Oversold','q':'Hammer Candle','r':'MACD Zero Cross',
                        's':'ATR Expansion','t':'Chop Exit','u':'Stoch Bull Momentum',
                        'v':'EMA200 Reclaim','w':'OBV Accumulation','x':'Triple Confluence',
                        'y':'BB Mid Reclaim','z':'Vol-Confirmed Trend','2':'Chop+MFI Confirm',
                    }
                    result = {
                        'signal': sig,
                        'name': _SN.get(sig, sig.upper()),
                        'total_fired': total_f,
                        'total_wins': total_w,
                        'wr': round(total_w / total_f * 100, 1) if total_f > 0 else 0,
                        'per_symbol': per_sym,
                    }
                self.wfile.write(json.dumps(result).encode())
            elif self.path.startswith("/position/"):
                from urllib.parse import unquote
                sym = unquote(self.path[len("/position/"):])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                result = {'error': 'Position not found'}
                if 'sts' in globals() and 'brs' in globals() and sym in sts:
                    st = sts[sym]
                    br = brs.get(sym)
                    pos = st.get_position()
                    if pos:
                        price, last_price = st.get_price()
                        fp, ep = float(price), float(pos['e'])
                        sz = float(pos.get('size', 0))
                        d = pos.get('direction', _MODE)
                        upl = (fp - ep) * sz if d == 'long' else (ep - fp) * sz
                        pnl_pct = ((fp - ep) / ep * 100) if d == 'long' else ((ep - fp) / ep * 100) if ep else 0
                        risk_dist = abs(float(pos['e']) - float(pos.get('orig_sl', pos['sl'])))
                        r_mult = upl / risk_dist if risk_dist > 0 else 0
                        now = time.time()
                        age_s = now - pos.get('opened_at', now)
                        tp_hit = pos.get('tp_hit', 0)
                        last_tp_t = pos.get('last_tp_time', 0)
                        # Time clocks
                        tp1_deadline_h = max(0, 96 - age_s / 3600) if tp_hit == 0 else None
                        since_last_tp_h = (now - last_tp_t) / 3600 if last_tp_t and tp_hit >= 1 else None
                        tp_stall_deadline_h = max(0, 48 - since_last_tp_h) if since_last_tp_h is not None else None
                        # Indicators
                        indicators = {}
                        try:
                            if len(st.df) > 0:
                                row = st.df.iloc[-1]
                                for col in ('rsi','macd','macd_sig','macd_hist','e9','e21','e200',
                                            'atr_pct','adx','bb_pctb','mfi','stoch_k','stoch_d','bb_width'):
                                    if col in row.index and pd.notna(row[col]):
                                        indicators[col] = round(float(row[col]), 4)
                        except Exception:
                            pass
                        result = {
                            'symbol': sym, 'direction': d,
                            'price': _fp(price), 'price_raw': fp,
                            'entry': _fp(pos['e']), 'entry_raw': ep,
                            'sl': _fp(pos['sl']), 'sl_raw': float(pos['sl']),
                            'orig_sl': _fp(pos.get('orig_sl', pos['sl'])), 'orig_sl_raw': float(pos.get('orig_sl', pos['sl'])),
                            'tp1': _fp(pos['tp1']) if 'tp1' in pos else None, 'tp1_raw': float(pos['tp1']) if 'tp1' in pos else None,
                            'tp2': _fp(pos['tp2']) if 'tp2' in pos else None, 'tp2_raw': float(pos['tp2']) if 'tp2' in pos else None,
                            'tp3': _fp(pos['tp3']) if 'tp3' in pos else None, 'tp3_raw': float(pos['tp3']) if 'tp3' in pos else None,
                            'tp_hit': tp_hit,
                            'trail_active': bool(pos.get('trail_active')),
                            'pnl': round(upl, 2), 'pnl_pct': round(pnl_pct, 2), 'r_mult': round(r_mult, 2),
                            'size': sz, 'fee': float(pos.get('fee', 0)),
                            'age_s': int(age_s),
                            'tp1_deadline_h': round(tp1_deadline_h, 1) if tp1_deadline_h is not None else None,
                            'tp_stall_deadline_h': round(tp_stall_deadline_h, 1) if tp_stall_deadline_h is not None else None,
                            'since_last_tp_h': round(since_last_tp_h, 1) if since_last_tp_h is not None else None,
                            'signals': pos.get('sg', []),
                            'confidence': round(float(br.confidence), 3) if br else 0.5,
                            'atr_pct': round(float(pos.get('atr_pct', 0)), 4),
                            'auction_score': round(float(pos.get('auction_score', 0)), 3),
                            'regime': br.regime if br else 'chop',
                            'up': fp >= float(last_price),
                            'indicators': indicators,
                            'factors': {k: round(float(v), 4) for k, v in pos.get('factors', {}).items()} if pos.get('factors') else None,
                            'ai_score': round(float(pos['ai_score']), 3) if pos.get('ai_score') is not None else None,
                            'llm_score': round(float(pos['llm_score']), 3) if pos.get('llm_score') is not None else None,
                            'whale_score': round(float(pos['whale_score']), 1) if pos.get('whale_score') is not None else None,
                            'funding_rate': round(float(pos['funding_rate']), 6) if pos.get('funding_rate') is not None else None,
                            'of_score': round(float(pos['of_score']), 4) if pos.get('of_score') is not None else None,
                        }
                self.wfile.write(json.dumps(result).encode())
            elif self.path.startswith("/trade/"):
                idx_str = self.path[len("/trade/"):]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                result = {'error': 'Trade not found'}
                try:
                    idx = int(idx_str)
                    m = get_live_metrics()
                    tlog = m.get('trade_log', [])
                    if 0 <= idx < len(tlog):
                        result = dict(tlog[idx])
                except (ValueError, IndexError):
                    pass
                self.wfile.write(json.dumps(result).encode())
            elif self.path.startswith("/symbol/"):
                from urllib.parse import unquote
                sym = unquote(self.path[len("/symbol/"):])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                result = {'error': 'Symbol not found'}
                if 'sts' in globals() and 'brs' in globals() and sym in sts:
                    st = sts[sym]
                    br = brs.get(sym)
                    price, last_price = st.get_price()
                    pos = st.get_position()
                    # Indicator snapshot from last candle
                    indicators = {}
                    try:
                        if len(st.df) > 0:
                            row = st.df.iloc[-1]
                            for col in ('rsi','macd','macd_sig','macd_hist','e9','e21','e200',
                                        'atr_pct','adx','bb_pctb','mfi','stoch_k','stoch_d','bb_width'):
                                if col in row.index and pd.notna(row[col]):
                                    indicators[col] = round(float(row[col]), 4)
                    except Exception:
                        pass
                    # Position details
                    pos_data = None
                    if pos:
                        fp, ep = float(price), float(pos['e'])
                        sz = float(pos.get('size', 0))
                        d = pos.get('direction', _MODE)
                        upl = (fp - ep) * sz if d == 'long' else (ep - fp) * sz
                        pos_data = {
                            'direction': d,
                            'entry': _fp(pos['e']),
                            'sl': _fp(pos['sl']),
                            'tp1': _fp(pos['tp1']) if 'tp1' in pos else None,
                            'tp2': _fp(pos['tp2']) if 'tp2' in pos else None,
                            'tp3': _fp(pos['tp3']) if 'tp3' in pos else None,
                            'tp_hit': pos.get('tp_hit', 0),
                            'opened_at': pos.get('opened_at', 0),
                            'signals': pos.get('sg', []),
                            'pnl': round(upl, 2),
                            'confidence': round(float(br.confidence), 3) if br else 0.5,
                            'atr_pct': round(float(pos.get('atr_pct', 0)), 4),
                            'auction_score': round(float(pos.get('auction_score', 0)), 3),
                            'factors': {k: round(float(v), 4) for k, v in pos.get('factors', {}).items()} if pos.get('factors') else None,
                        }
                    result = {
                        'symbol': sym,
                        'price': _fp(price),
                        'last_price': _fp(last_price),
                        'up': float(price) >= float(last_price),
                        'regime': br.regime if br else 'chop',
                        'confidence': round(float(br.confidence), 3) if br else 0.5,
                        'wr': round(br.wr(), 1) if br else 0,
                        'per_signal': br.stats if br else {},
                        'dir_history': br.dir_history if br else {'long': [], 'short': []},
                        'position': pos_data,
                        'indicators': indicators,
                    }
                self.wfile.write(json.dumps(result).encode())
            elif self.path == "/api/equity":
                # Equity curve time-series for the dashboard hero chart
                with _equity_lock:
                    series = list(_equity_series)
                body = json.dumps(series).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/trade_markers":
                # Permanent per-trade open/close markers for chart overlay
                with _trade_markers_lock:
                    markers = list(_trade_markers)
                body = json.dumps(markers).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/funnel":
                # Gate funnel snapshot derived from _decision_log (last 60s)
                snap = _compute_funnel_snapshot(60)
                body = json.dumps(snap).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, format, *args):
            pass
    try:
        class _ThreadedTCP(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True
        _health_port = 18095  # GoldenEye standalone — high port to avoid fleet conflicts
        with _ThreadedTCP(("", _health_port), HealthHandler) as httpd:
            logging.info(f"Health server listening on port {_health_port}")
            while _http_server_running:
                httpd.handle_request()
    except Exception as e:
        logging.warning(f"Health server failed: {e}")

def start_http_health_server():
    global _health_server_thread
    _health_server_thread = threading.Thread(target=_run_http_health_server, daemon=True)
    _health_server_thread.start()

def start_health_monitor():
    global _health_thread
    _health_thread = threading.Thread(target=health_monitor, daemon=True)
    _health_thread.start()

def _balance_refresh_loop(interval=300):
    """Periodic Kraken balance sync for live mode. Fills the gap where
    _sync_live_balance was only called on startup + trade events."""
    # Wait for startup to complete before first sync
    time.sleep(60)  # Give main thread time to fully initialize
    while True:
        time.sleep(interval)
        if _is_paper():
            continue
        try:
            if 'exchange' in globals() and exchange:
                bal = _sync_live_balance(exchange, force=True)
                logging.info(f"Periodic balance sync: ${float(bal):,.2f}")
        except Exception as e:
            logging.warning(f"Periodic balance sync failed: {e}")

def start_balance_refresh():
    _t = threading.Thread(target=_balance_refresh_loop, daemon=True)
    _t.start()

# Main
def graceful_shutdown(signum, frame):
    logging.warning(f"Shutdown signal received (signal={signum})")
    send_alert(f"{_BRAND} going dark for maintenance. Positions are safe on Kraken. Back shortly.")
    _shutdown_all()

def main():
    global exchange, sts, brs, ts, _start_time

    import signal
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    _start_time = datetime.now()

    a = parse_args()
    # Set mode globals before anything else
    global _MODE, _DIR, _BRAND, _FACTOR_W, _WTD_SIG_MIN, _METRICS_FILE, _SIG_DESC
    _MODE = a.mode
    _DIR = -1 if _MODE == 'short' else 1
    _BRAND = 'GoldenEye SHORT' if _MODE == 'short' else 'GoldenEye'
    _FACTOR_W = _FACTOR_W_SHORT if _MODE == 'short' else _FACTOR_W_LONG
    _WTD_SIG_MIN = _WTD_SIG_MIN_SHORT if _MODE == 'short' else _WTD_SIG_MIN_LONG
    _SIG_DESC = _SIG_DESC_SHORT if _MODE == 'short' else _SIG_DESC_LONG
    if a.output_dir is None:
        a.output_dir = 'output_short' if _MODE == 'short' else 'output'
    _METRICS_FILE = os.path.join(a.output_dir, 'metrics.json')
    # Log to file only — stdout is owned by the Rich TUI
    _log_file = 'goldeneye_short_err.log' if _MODE == 'short' else 'goldeneye_err.log'
    _log_file = os.path.join(_BOT_DIR, _log_file)
    _err_fh = logging.handlers.RotatingFileHandler(_log_file, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
    # Force handlers directly to bypass basicConfig no-op behavior
    root = logging.getLogger()
    root.setLevel(a.log_level.upper())
    root.handlers = []
    root.addHandler(_err_fh)
    logging.info("GoldenEye logging initialized")
    _err_fh.flush()
    logging.getLogger('ccxt').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('huggingface_hub').setLevel(logging.WARNING)

    c = yaml.safe_load(open(a.config)) if os.path.exists(a.config) else {}  # noqa: SIM115 — short-lived, GC'd immediately
    set_config(c)
    _load_metrics()

    # Standalone live/paper toggle — reads from config.yaml
    global _STANDALONE_MODE
    _STANDALONE_MODE = c.get('trading_mode', 'paper')

    # Build ccxt config — inject API keys when present
    _ccxt_cfg = {'enableRateLimit': True, 'session': __import__('requests').Session()}
    if _HAS_KEYS:
        _ccxt_cfg['apiKey'] = _KRAKEN_KEY
        _ccxt_cfg['secret'] = _KRAKEN_SECRET

    for _retry in range(5):
        try:
            exchange = ccxt.kraken(_ccxt_cfg)
            adapter = __import__('requests.adapters',fromlist=['HTTPAdapter']).HTTPAdapter(pool_connections=25,pool_maxsize=25)
            exchange.session.mount('https://',adapter)
            exchange.session.mount('http://',adapter)
            exchange.load_markets()
            set_exchange(exchange)
            break
        except Exception as e:
            logging.error(f"Exchange init failed (attempt {_retry+1}/5): {e}")
            if _retry < 4: time.sleep(30)
            else: raise

    if _is_paper():
        if not _HAS_KEYS:
            logging.info("PAPER MODE — no KRAKEN_API_KEY, simulating trades")
        else:
            logging.info("PAPER MODE — keys present but trading_mode='paper' in config.yaml")
    else:
        global _LIVE_BALANCE
        logging.warning("*** LIVE TRADING MODE — real Kraken orders will be placed ***")
        # Load persisted balance before first sync attempt (crash recovery)
        loaded = _load_live_balance()
        _LIVE_BALANCE = loaded
        
        try:
            bal = _sync_live_balance(exchange, force=True)
        except Exception as e:
            logging.warning(f"Kraken balance sync failed: {e}")
            bal = _LIVE_BALANCE
        
        # Force preserve loaded balance if sync returned 0
        if bal == 0:
            bal = _LIVE_BALANCE
        
        logging.warning(f"Kraken available balance: ${float(bal):,.2f} — trade size: ${float(_get_trade_amt()):,.2f}")
        if bal < 1:
            logging.warning("LIVE MODE: Kraken balance sync failed or $0 — will retry in background")

    s = c.get("symbols")
    if not s:
        s = top_sym(20)
    else:
        s = [x for x in s if x in exchange.markets]

    # Filter out fleet-blacklisted pairs (0% WR across fleet)
    _bl_before = len(s)
    s = [x for x in s if not _is_blacklisted(x)]
    if len(s) < _bl_before:
        logging.info(f"Blacklist filtered {_bl_before - len(s)} pairs")

    logging.info(f"Symbols ({len(s)}): {', '.join(s)}")

    sts, brs, ts = {}, {}, []

    if a.backtest:
        for sym in s:
            d = os.path.join(a.output_dir, sym.replace('/','_'))
            st = BotState(d)
            br = AdaptiveBrain(st)
            sts[sym] = st
            brs[sym] = br
            bt(sym, a.days, br, exchange)
        run_backtest_report(s, brs)
        return

    _thread_map = {}
    for sym in s:
        d = os.path.join(a.output_dir, sym.replace('/','_'))
        st = BotState(d)
        br = AdaptiveBrain(st)
        sts[sym] = st
        brs[sym] = br

    # Startup reconciliation — verify local positions vs exchange before streaming
    logging.info("Startup position reconciliation...")
    reconcile_positions(exchange, sts, brs)

    for sym in s:
        st, br = sts[sym], brs[sym]
        t = threading.Thread(target=stream, args=(st,sym,br,exchange,c.get("poll_interval_seconds",2.0)), daemon=True)
        ts.append(t)
        _thread_map[sym] = t
        t.start()

    recon_interval = c.get('reconciliation_interval_seconds', 300)
    recon_thread = threading.Thread(target=periodic_reconciler, args=(exchange, sts, brs, _thread_map, c, recon_interval), daemon=True)
    recon_thread.start()
    register_thread('reconciler')

    # Universe rescan — re-ranks Kraken pairs by volume every 2h, adds/drops symbols
    rescan_thread = threading.Thread(target=_universe_rescan, args=(exchange, sts, brs, _thread_map, c, a.output_dir), daemon=True)
    rescan_thread.start()
    register_thread('rescan')

    # AI ensemble worker (optional — requires Ollama + GPU)
    if is_ai_enabled():
        global _ai_in_q, _ai_out_q
        try:
            import goldeneye_ai
            from queue import Queue as _Q
            goldeneye_ai._heartbeat_fn = heartbeat
            _ai_in_q = _Q()
            _ai_out_q = _Q()
            ai_thread = threading.Thread(target=goldeneye_ai.ai_worker, args=(_ai_in_q, _ai_out_q), daemon=True)
            ai_thread.start()
            register_thread('ai_worker')
            logging.info("AI ensemble worker started")
        except Exception as e:
            logging.warning(f"AI ensemble failed to start: {e}")

    if not _is_paper():
        order_monitor_interval = c.get('order_monitor_interval_seconds', 30)
        for sym in s:
            mon_thread = threading.Thread(target=order_status_monitor, args=(exchange, sts[sym], brs[sym], order_monitor_interval, sym), daemon=True)
            mon_thread.start()
            register_thread(f'monitor_{sym}')

    try:
        import sys as _sys
        _sys.path.insert(0, r"D:\CommandCenter")
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        _goldeneye_port = 18095  # GoldenEye standalone — high port to avoid fleet conflicts
        ensure_port(_goldeneye_port, "goldeneye")
        write_pidfile("goldeneye", _goldeneye_port)
        atexit.register(cleanup_pidfile, "goldeneye")
    except Exception as _e:
        logging.warning(f"[PORT_GUARD] Warning: {_e}")

    start_health_monitor()
    start_balance_refresh()
    # Dashboard data layer: load persisted trade markers, launch equity time-series logger.
    # Equity logger internally calls _load_equity_series() on first iteration.
    _load_trade_markers()
    start_equity_logger()
    # Start background health snapshot updater — /health reads from this, never computes inline
    threading.Thread(target=_update_health_snapshot, daemon=True).start()
    start_http_health_server()
    start_subscriber_api()

    # Launch Flask dashboard in background thread — no second terminal needed
    try:
        from dashboard import app as _dash_app
        def _run_dashboard():
            import logging as _lg
            _lg.getLogger('werkzeug').setLevel(_lg.ERROR)  # suppress Flask request noise
            _dash_env = 'DASHBOARD_PORT_SHORT' if _MODE == 'short' else 'DASHBOARD_PORT'
            _dash_default = 18065  # GoldenEye standalone — high port to avoid fleet conflicts
            _dash_app.run(host='0.0.0.0', port=int(os.getenv(_dash_env, _dash_default)), debug=False, use_reloader=False)
        _dash_port = int(os.getenv('DASHBOARD_PORT_SHORT' if _MODE == 'short' else 'DASHBOARD_PORT', 18065))
        dash_thread = threading.Thread(target=_run_dashboard, daemon=True)
        dash_thread.start()
        logging.info(f"Dashboard started at http://localhost:{_dash_port}/")
    except Exception as e:
        logging.warning(f"Dashboard failed to start: {e}")

    if sts:
        logging.getLogger().addHandler(TUIHandler())
        tui_ok = False
        try:
            run_tui(sts, brs, exchange.id.capitalize())
            tui_ok = True
        except KeyboardInterrupt:
            pass
        except Exception as e:
            import traceback
            logging.warning(f"TUI failed ({e}), running headless\n{traceback.format_exc()}")
            while True:
                try:
                    time.sleep(60)
                except KeyboardInterrupt:
                    break
                except Exception as he:
                    logging.error(f"Headless loop error: {he}")
                    time.sleep(10)
        finally:
            _shutdown_all()

def _shutdown_all():
    global _http_server_running, _api_server_running
    logging.info("Shutting down...")
    _http_server_running = False
    _api_server_running = False
    # Shut down AI worker if running
    if _ai_in_q is not None:
        try:
            import goldeneye_ai
            _ai_in_q.put(goldeneye_ai.SHUTDOWN_SIGNAL)
        except Exception:
            pass
    if 'sts' in globals() and sts:
        for st in sts.values():
            st.shutdown.set()
    if 'ts' in globals() and ts:
        for t in ts:
            t.join(timeout=4)
    _save_metrics()
    logging.shutdown()
    # Hard exit — sys.exit(0) can be caught by Rich/Flask context managers,
    # leaving daemon threads (Flask :8050, HTTP :8080) alive as zombie processes.
    os._exit(0)

if __name__ == "__main__":
    main()
