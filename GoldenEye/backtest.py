#!/usr/bin/env python3
"""GoldenEye Backtesting Engine — signal-bot-focused quality evaluation.

Usage:
    python backtest.py                          # 90 days, top 20 symbols
    python backtest.py --days 365 --symbols BTC/USD ETH/USD
    python backtest.py --days 180 --walk-forward --folds 5
    python backtest.py --days 180 --monte-carlo --mc-sims 5000
    python backtest.py --days 365 --full        # all validations
    python backtest.py --no-hmm                  # ablation testing
"""
import os, json, time, argparse, logging, hashlib, tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections import defaultdict

import numpy as np
import pandas as pd
import ccxt

# ---------------------------------------------------------------------------
# Suppress TUI / live globals from goldeneye import — harmless but noisy
# ---------------------------------------------------------------------------
os.environ['_BACKTEST_MODE'] = '1'
logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')

from goldeneye import (
    BotState, AdaptiveBrain, HMMRegimeDetector,
    ind, regime, sigs, compute_factors, compute_confidence,
    is_exhausted,
    compute_volume_profile, find_order_blocks,
    set_exchange, top_sym,
    _REGIMES, _FK,
    _TP1_MULT, _TP2_MULT, _TP3_MULT,
    _PAPER_SLIP, _SIG_DESC,
    _blended_rgm_mult, _blended_rgm_conf_min,
)

try:
    import hmmlearn.hmm  # noqa: F401 — only need _HAS_HMM flag
    _HAS_HMM = True
except ImportError:
    _HAS_HMM = False

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    _HAS_PLOTLY = True
except ImportError:
    _HAS_PLOTLY = False

from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, MofNCompleteColumn
from rich.table import Table
from rich.panel import Panel

console = Console()

# ═══════════════════════════════════════════════════════════════════════════
# DataLoader — paginated OHLCV from Kraken via ccxt, parquet cache
# ═══════════════════════════════════════════════════════════════════════════

_CACHE_DIR = os.path.join('output', 'backtest_cache')

def _cache_key(sym, tf, since_ms, until_ms):
    """Deterministic cache key for a symbol + timeframe + date range."""
    raw = f"{sym}_{tf}_{since_ms}_{until_ms}"
    return hashlib.md5(raw.encode()).hexdigest()

def fetch_ohlcv(ex, sym, tf='1h', days=90, progress=None, task_id=None):
    """Paginated OHLCV fetch from Kraken. Returns DataFrame with OHLCV columns.

    Fetches 720 bars per call, loops until entire date range is covered.
    Caches to parquet in output/backtest_cache/.
    """
    now_ms = int(time.time() * 1000)
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    # Round to hour boundary for cache stability
    since_ms = since_ms - (since_ms % 3600000)
    until_ms = now_ms - (now_ms % 3600000)

    os.makedirs(_CACHE_DIR, exist_ok=True)
    ck = _cache_key(sym, tf, since_ms, until_ms)
    cache_path = os.path.join(_CACHE_DIR, f"{ck}.parquet")

    if os.path.exists(cache_path):
        try:
            df = pd.read_parquet(cache_path)
            if len(df) > 50:
                if progress and task_id is not None:
                    progress.update(task_id, advance=1)
                return df
        except Exception:
            pass

    all_bars = []
    cursor = since_ms
    per_call = 720
    tf_ms = {'1h': 3600000, '4h': 14400000, '1d': 86400000}.get(tf, 3600000)

    while cursor < until_ms:
        try:
            bars = ex.fetch_ohlcv(sym, tf, since=cursor, limit=per_call)
            if not bars:
                break
            all_bars.extend(bars)
            last_ts = bars[-1][0]
            if last_ts <= cursor:
                break
            cursor = last_ts + tf_ms
            # Rate limit: Kraken allows ~1 req/s for public endpoints
            time.sleep(0.35)
        except ccxt.RateLimitExceeded:
            time.sleep(2)
        except Exception as e:
            logging.warning(f"OHLCV fetch failed {sym}: {e}")
            break

    if not all_bars:
        if progress and task_id is not None:
            progress.update(task_id, advance=1)
        return pd.DataFrame()

    df = pd.DataFrame(all_bars, columns=['time', 'o', 'high', 'low', 'close', 'vol'])
    df = df.drop_duplicates(subset='time').sort_values('time').reset_index(drop=True)
    # Filter to requested date range
    df = df[(df['time'] >= since_ms) & (df['time'] <= until_ms)].reset_index(drop=True)

    try:
        df.to_parquet(cache_path, index=False)
    except Exception:
        pass

    if progress and task_id is not None:
        progress.update(task_id, advance=1)
    return df


def load_regime_pickle(path, window_key, symbols=None):
    """Load cached regime candles from pickle file.
    Returns dict {sym: DataFrame(time, o, high, low, close, vol)} matching fetch_multi output.
    """
    import pickle
    with open(path, 'rb') as f:
        all_data = pickle.load(f)
    if window_key not in all_data:
        console.print(f"[red]Window '{window_key}' not in pickle. Available: {list(all_data.keys())}[/red]")
        return {}
    window_data = all_data[window_key]
    out = {}
    for sym, df in window_data.items():
        if symbols and sym not in symbols:
            continue
        if df is None or len(df) < 100:
            continue
        # Normalize columns: pickle uses 't', backtest expects 'time'
        dfn = df.rename(columns={'t': 'time'}).copy()
        if 'time' not in dfn.columns:
            continue
        dfn = dfn[['time', 'o', 'high', 'low', 'close', 'vol']].copy()
        dfn = dfn.sort_values('time').drop_duplicates(subset='time').reset_index(drop=True)
        out[sym] = dfn
    return out


def fetch_multi(ex, syms, tf='1h', days=90):
    """Fetch OHLCV for all symbols with progress bar."""
    data = {}
    with Progress(
        TextColumn("[bold gold1]Fetching OHLCV"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("", total=len(syms))
        for sym in syms:
            df = fetch_ohlcv(ex, sym, tf, days, progress, task)
            if df is not None and len(df) > 100:
                data[sym] = df
            else:
                progress.update(task, advance=1)
                logging.warning(f"Skipping {sym}: insufficient data ({len(df) if df is not None else 0} bars)")
    return data


def resample_htf(df_1h, tf='4h'):
    """Resample 1h OHLCV to higher timeframe. Returns DataFrame in same format."""
    if df_1h is None or len(df_1h) < 10:
        return None
    _df = df_1h.copy()
    _df['dt'] = pd.to_datetime(_df['time'], unit='ms', utc=True)
    _df = _df.set_index('dt')
    rule = {'4h': '4h', '1d': '1D'}.get(tf, '4h')
    agg = _df.resample(rule).agg({
        'time': 'first', 'o': 'first', 'high': 'max',
        'low': 'min', 'close': 'last', 'vol': 'sum'
    }).dropna().reset_index(drop=True)
    return agg


def _htf_bias_from_df(df_4h):
    """Compute HTF bias score in [0,1] from 4h DataFrame (no API call)."""
    if df_4h is None or len(df_4h) < 20:
        return 0.5
    ma5 = df_4h['close'].iloc[-5:].mean()
    ma20 = df_4h['close'].iloc[-20:].mean()
    if ma20 > 0:
        bias = max(-1, min((ma5 - ma20) / ma20, 1))
        return round(0.5 + bias * 0.5, 4)
    return 0.5


# ═══════════════════════════════════════════════════════════════════════════
# SignalRecord — one published signal
# ═══════════════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field

@dataclass
class SignalRecord:
    """One trading signal — all signals that pass factor/confidence gates are published."""
    symbol: str
    bar_idx: int
    entry_price: float
    entry_time: float          # unix ms
    sl_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    confidence: float
    regime: str
    signals: list              # signal codes active at entry
    factors: dict              # 6 factor scores
    dqn_action: int = 0        # always 0 (published) — DQN gate removed
    rgm_proba: list = field(default_factory=lambda: [0.25]*4)

    # --- Outcome (filled during simulation) ---
    exit_bar: int = 0
    exit_price: float = 0.0
    exit_time: float = 0.0
    exit_type: str = ''        # SL / TP3 / EXH / TIME
    r_multiple: float = 0.0
    tp_hit: int = 0            # highest TP level reached (0-3)
    peak_r: float = 0.0        # best R achieved during signal lifetime
    time_to_tp1_h: float = 0.0 # hours to TP1 (0 if never hit)
    duration_h: float = 0.0    # total signal lifetime in hours

    @property
    def published(self):
        return self.dqn_action == 0

    @property
    def risk_dist(self):
        return abs(self.entry_price - self.sl_price)

    def to_dict(self):
        return {
            'sym': self.symbol, 'bar_idx': self.bar_idx,
            'entry': self.entry_price, 'exit': self.exit_price,
            'entry_time': self.entry_time, 'exit_time': self.exit_time,
            'sl': self.sl_price, 'tp1': self.tp1_price,
            'tp2': self.tp2_price, 'tp3': self.tp3_price,
            'confidence': self.confidence, 'regime': self.regime,
            'signals': self.signals, 'factors': self.factors,
            'dqn_action': self.dqn_action,
            'exit_type': self.exit_type, 'r': round(self.r_multiple, 4),
            'tp_hit': self.tp_hit, 'peak_r': round(self.peak_r, 4),
            'time_to_tp1_h': round(self.time_to_tp1_h, 1),
            'duration_h': round(self.duration_h, 1),
            'published': self.published,
        }


# ═══════════════════════════════════════════════════════════════════════════
# SignalSimulator — bar-by-bar signal evaluation (no portfolio constraints)
# ═══════════════════════════════════════════════════════════════════════════

class SignalSimulator:
    """Signal-bot backtester. Evaluates signal quality, not portfolio returns.

    Every signal that passes the factor/confidence/regime gates is published.
    No position limits, no portfolio-level drawdown gates, no balance tracking.
    """

    def __init__(self, data, use_hmm=True, brains=None, fresh_brains=True,
                 conservative_tp=False, move_sl_at_tp1=False,
                 conf_floor_override=None):
        self.data = data
        self.syms = sorted(data.keys())
        self.use_hmm = use_hmm and _HAS_HMM
        self.base_slip = float(_PAPER_SLIP)
        self.slip = self.base_slip  # updated per-bar via _dynamic_slip()
        self.conservative_tp = conservative_tp
        self.move_sl_at_tp1 = move_sl_at_tp1  # live bot does NOT move — default aligned with live
        self.conf_floor_override = conf_floor_override  # dict {regime: min_conf} to override _RGM_CONF_MIN

        # Per-symbol indicator / regime caches
        self.ind_dfs = {}
        self.htf_dfs = {}
        self.hmm = {}
        self.rgm = {}
        self.rgm_proba = {}
        self.cooldowns = {}   # sym -> bar_idx of last exit (per-symbol cooldown)

        # Brains
        if brains and not fresh_brains:
            self.brains = brains
        else:
            self._tmpdir = tempfile.mkdtemp(prefix='bt_sig_')
            self.brains = {}
            for sym in self.syms:
                d = os.path.join(self._tmpdir, sym.replace('/', '_'))
                os.makedirs(d, exist_ok=True)
                st = BotState(d)
                br = AdaptiveBrain(st)
                br.regime = 'chop'
                br.regime_proba = [0.25, 0.25, 0.25, 0.25]
                self.brains[sym] = br

        # Results
        self.records = []         # all SignalRecords (published)
        self._open = {}           # sym -> list of open SignalRecords (multiple allowed)

        self._align_data()

    def _dynamic_slip(self, df):
        """ATR-scaled slippage: high volatility = worse fills."""
        if 'atr' not in df.columns or len(df) < 50:
            return self.base_slip
        atr_now = float(df['atr'].iloc[-1])
        atr_med = float(df['atr'].rolling(50).median().iloc[-1])
        if pd.isna(atr_now) or pd.isna(atr_med) or atr_med <= 0:
            return self.base_slip
        ratio = atr_now / atr_med
        # Scale: low vol = 0.5x base, high vol = up to 2.5x base
        return self.base_slip * max(0.5, min(ratio, 2.5))

    def _align_data(self):
        """Build per-symbol time->row index maps. No common-time intersection required."""
        self.time_idx = {}
        self.sym_times = {}
        all_t = set()
        for sym in self.syms:
            t2i = {}
            for i, row in self.data[sym].iterrows():
                t2i[row['time']] = i
            self.time_idx[sym] = t2i
            self.sym_times[sym] = sorted(t2i.keys())
            all_t.update(t2i.keys())
        self.all_times = sorted(all_t)

    def _get_slice(self, sym, end_time, lookback=250):
        df = self.data[sym]
        idx = self.time_idx[sym].get(end_time)
        if idx is None:
            return None
        start = max(0, idx - lookback + 1)
        return df.iloc[start:idx + 1].copy()

    # -------------------------------------------------------------------
    # Main run loop
    # -------------------------------------------------------------------
    def run(self):
        """Execute full simulation. Returns list of SignalRecords."""
        if not self.all_times:
            console.print("[red]No data for simulation[/red]")
            return self.records

        n_bars = len(self.all_times)
        warmup = 250

        if n_bars <= warmup:
            console.print(f"[red]Only {n_bars} bars — need >{warmup} for warmup[/red]")
            return self.records

        with Progress(
            TextColumn("[bold gold1]Simulating signals"),
            BarColumn(bar_width=40),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("", total=n_bars - warmup)

            for bar_i in range(warmup, n_bars):
                t = self.all_times[bar_i]
                for sym in self.syms:
                    if t in self.time_idx[sym]:
                        self._process_bar(sym, t, bar_i)
                progress.update(task, advance=1)

        # Close any remaining open signals at last close price (not entry)
        for sym in list(self._open.keys()):
            last_px = float(self.data[sym].iloc[-1]['close']) * (1 - self.base_slip)
            for rec in list(self._open.get(sym, [])):
                self._close_signal(rec, last_px, n_bars - 1, 'TIME', self.all_times[-1])

        return self.records

    # -------------------------------------------------------------------
    # Per-bar processing
    # -------------------------------------------------------------------
    def _process_bar(self, sym, t, bar_i):
        sl = self._get_slice(sym, t)
        if sl is None or len(sl) < 50:
            return

        df = ind(sl)
        self.ind_dfs[sym] = df
        price = float(df.iloc[-1]['close'])
        high = float(df.iloc[-1].get('high', price))
        low = float(df.iloc[-1].get('low', price))
        br = self.brains[sym]

        # Update 4h HTF every 4 bars
        if bar_i % 4 == 0 or sym not in self.htf_dfs:
            htf_raw = self._get_slice(sym, t, lookback=1200)
            if htf_raw is not None and len(htf_raw) > 20:
                htf = resample_htf(htf_raw, '4h')
                if htf is not None and len(htf) > 20:
                    htf = ind(htf)
                    self.htf_dfs[sym] = htf

        # Regime detection
        if bar_i % 4 == 0 or sym not in self.rgm:
            rgm_df = self.htf_dfs.get(sym, df)
            if self.use_hmm and len(rgm_df) >= 200:
                if sym not in self.hmm:
                    self.hmm[sym] = HMMRegimeDetector()
                hd = self.hmm[sym]
                hd.tick()
                if not hd.fitted or hd.needs_refit():
                    hd.fit(rgm_df)
                self.rgm_proba[sym] = hd.predict_proba(rgm_df)
                self.rgm[sym] = _REGIMES[int(np.argmax(self.rgm_proba[sym]))]
            else:
                self.rgm[sym] = regime(rgm_df)
                self.rgm_proba[sym] = [1.0 if r == self.rgm[sym] else 0.0 for r in _REGIMES]
            br.regime = self.rgm[sym]
            br.regime_proba = list(self.rgm_proba[sym])

        # --- EXIT CHECK for open signals ---
        for rec in list(self._open.get(sym, [])):
            self._check_exit(rec, df, price, high, low, bar_i, t)

        # --- ENTRY EVALUATION ---
        self._check_entry(sym, df, price, bar_i, t)

    # -------------------------------------------------------------------
    # Exit logic — mirrors live (SL, TP1/2/3, trailing, exhaustion, time)
    # -------------------------------------------------------------------
    def _check_exit(self, rec, df, price, high, low, bar_i, t):
        bars_held = bar_i - rec.bar_idx
        elapsed_h = bars_held  # 1 bar = 1h
        self.slip = self._dynamic_slip(df)

        # Track peak R
        if rec.risk_dist > 0:
            cur_r = (high - rec.entry_price) / rec.risk_dist
            if cur_r > rec.peak_r:
                rec.peak_r = cur_r

        # Internal mutable state stored on rec via private attrs
        cur_sl = getattr(rec, '_sl', rec.sl_price)
        tp_hit = getattr(rec, '_tp_hit', 0)
        last_tp_bar = getattr(rec, '_last_tp_bar', rec.bar_idx)
        trail_active = getattr(rec, '_trail', False)

        # --- SL check (use low for intra-bar hits) ---
        if low <= cur_sl:
            exit_px = max(cur_sl, low) * (1 - self.slip)
            self._close_signal(rec, exit_px, bar_i, 'SL', t, tp_hit=tp_hit)
            return

        # --- TP levels (use high for intra-bar hits) ---
        prev_tp = tp_hit
        if tp_hit < 1 and high >= rec.tp1_price:
            tp_hit = 1
            last_tp_bar = bar_i
            rec.time_to_tp1_h = float(elapsed_h)
            if self.move_sl_at_tp1:
                atr_val = float(df.iloc[-1].get('atr', rec.entry_price * 0.02)) if 'atr' in df.columns else rec.entry_price * 0.02
                cur_sl = rec.entry_price + atr_val * 0.5
        if tp_hit < 2 and high >= rec.tp2_price:
            tp_hit = 2
            last_tp_bar = bar_i
            trail_active = True
        if tp_hit < 3 and high >= rec.tp3_price:
            # Conservative mode: require close > TP3 (wicks don't reliably fill limits)
            if self.conservative_tp and price < rec.tp3_price:
                tp_hit = 3  # register TP3 hit for trailing logic, but don't exit yet
                last_tp_bar = bar_i
            else:
                tp_hit = 3
                exit_px = rec.tp3_price * (1 - self.slip)
                self._close_signal(rec, exit_px, bar_i, 'TP3', t, tp_hit=3)
                return

        # Re-check SL after move
        if tp_hit > prev_tp and low <= cur_sl:
            exit_px = max(cur_sl, low) * (1 - self.slip)
            self._close_signal(rec, exit_px, bar_i, 'SL', t, tp_hit=tp_hit)
            return

        # Keltner trailing after TP2
        if trail_active and 'atr' in df.columns and 'e21' in df.columns and len(df) >= 22:
            atr_val = float(df.iloc[-1]['atr'])
            ema21 = float(df.iloc[-1]['e21'])
            kc_mult = 1.5 if tp_hit >= 3 else 2.5
            floor = rec.entry_price + atr_val * 0.5
            trail = max(ema21 - kc_mult * atr_val, floor)
            if trail > cur_sl:
                cur_sl = trail
            if low <= cur_sl:
                exit_px = max(cur_sl, low) * (1 - self.slip)
                self._close_signal(rec, exit_px, bar_i, 'EXH', t, tp_hit=tp_hit)
                return

        # Trend exhaustion after TP1
        if tp_hit >= 1 and is_exhausted(df, 'long'):
            exit_px = price * (1 - self.slip)
            self._close_signal(rec, exit_px, bar_i, 'EXH', t, tp_hit=tp_hit)
            return

        # Time exit: 96h no TP1, 48h since last TP
        if tp_hit == 0 and elapsed_h > 96:
            exit_px = price * (1 - self.slip)
            self._close_signal(rec, exit_px, bar_i, 'TIME', t, tp_hit=tp_hit)
            return
        if tp_hit >= 1:
            since_tp = bar_i - last_tp_bar
            if since_tp > 48:
                exit_px = price * (1 - self.slip)
                self._close_signal(rec, exit_px, bar_i, 'TIME', t, tp_hit=tp_hit)
                return

        # Store mutable state back
        rec._sl = cur_sl
        rec._tp_hit = tp_hit
        rec._last_tp_bar = last_tp_bar
        rec._trail = trail_active

    def _close_signal(self, rec, exit_px, bar_i, exit_type, t, tp_hit=None):
        """Finalise a signal record and move from open to closed."""
        rec.exit_bar = bar_i
        rec.exit_price = exit_px
        rec.exit_time = float(t) if isinstance(t, (int, float)) else 0.0
        rec.exit_type = exit_type
        rec.tp_hit = tp_hit if tp_hit is not None else getattr(rec, '_tp_hit', 0)
        rec.duration_h = float(bar_i - rec.bar_idx)

        # R-multiple
        rd = rec.risk_dist
        if rd > 0:
            rec.r_multiple = (exit_px - rec.entry_price) / rd
        else:
            rec.r_multiple = 0.0  # invalid risk distance — don't fabricate R
        # Deduct fees (entry + exit)
        rec.r_multiple -= 2 * 0.004

        self.records.append(rec)

        # Remove from open list and record to brain
        sym = rec.symbol
        lst = self._open.get(sym, [])
        if rec in lst:
            lst.remove(rec)
        br = self.brains.get(sym)
        if br:
            br.record(rec.signals, rec.r_multiple, Decimal('0.001'), 'long',
                      factors=rec.factors, rgm=rec.regime)
        # Cooldown
        self.cooldowns[sym] = bar_i

    # -------------------------------------------------------------------
    # Entry evaluation — signal + factor gates
    # PARITY NOTE: of_score=0.5 (live uses real order book), no funding_rate,
    # no whale_score, simplified HTF bias (_htf_bias_from_df vs compute_mtf_confluence).
    # Confidence thresholds from backtest should not be directly transferred to live.
    # Relative comparisons (higher conf > lower conf) transfer; absolute values don't.
    # -------------------------------------------------------------------
    def _check_entry(self, sym, df, price, bar_i, t):
        br = self.brains[sym]

        # Cooldown (1 bar = 1h, live is 30 min)
        last_exit = self.cooldowns.get(sym, -999)
        if (bar_i - last_exit) < 1:
            return

        # Signals — need 2+
        sg = sigs(df)
        if len(sg) < 2:
            return

        # 6-factor scores
        htf_bias = _htf_bias_from_df(self.htf_dfs.get(sym))
        cur_rgm = self.rgm.get(sym, 'chop')
        rgm_proba = self.rgm_proba.get(sym, [0.25] * 4)

        vp_data = compute_volume_profile(df.tail(50)) if len(df) >= 40 else None
        ob_data = find_order_blocks(df.tail(30)) if len(df) >= 20 else None
        factors = compute_factors(df, price, vp_data, ob_data, htf_bias, cur_rgm, of_score=0.5)
        confidence = compute_confidence(factors, cur_rgm, rgm_proba=rgm_proba, brain=br)

        # Win-rate floor
        wr_raw = br.dir_wr('long')
        wr = wr_raw if wr_raw is not None else 0.5
        if wr_raw is not None and wr_raw < 0.2:
            return

        # Regime multiplier gate
        rgm_mult = _blended_rgm_mult(rgm_proba, 'long')
        if rgm_mult < 0.1:
            return

        # Confidence threshold
        if self.conf_floor_override is not None:
            cur_rgm_name = self.rgm.get(sym, 'chop')
            min_conf = self.conf_floor_override.get(cur_rgm_name, _blended_rgm_conf_min(rgm_proba))
        else:
            min_conf = _blended_rgm_conf_min(rgm_proba)
        if confidence < min_conf:
            return

        # --- Compute SL / TP levels ---
        self.slip = self._dynamic_slip(df)
        entry_price = price * (1 + self.slip)
        atr_pct = float(df.iloc[-1].get('atr_pct', 0.02)) if 'atr_pct' in df.columns else 0.02
        atr_sl = min(max(atr_pct * 1.2, 0.005), 0.05)
        atr_tp = atr_sl

        # Dynamic TP scaling (ATR expansion ratio)
        if len(df) >= 50 and 'atr' in df.columns:
            atr_med = float(df['atr'].rolling(50).median().iloc[-1])
            atr_now = float(df['atr'].iloc[-1])
            if atr_med > 0 and not pd.isna(atr_med) and not pd.isna(atr_now):
                tp_scale = max(0.8, min(1.0 / (atr_now / atr_med), 1.5))
            else:
                tp_scale = 1.0
        else:
            tp_scale = 1.0

        sl_price = entry_price * (1 - atr_sl)
        tp1 = entry_price + entry_price * atr_tp * float(_TP1_MULT) * tp_scale
        tp2 = entry_price + entry_price * atr_tp * float(_TP2_MULT) * tp_scale
        tp3 = entry_price + entry_price * atr_tp * float(_TP3_MULT) * tp_scale

        rec = SignalRecord(
            symbol=sym, bar_idx=bar_i, entry_price=entry_price,
            entry_time=float(t), sl_price=sl_price,
            tp1_price=tp1, tp2_price=tp2, tp3_price=tp3,
            confidence=confidence, regime=cur_rgm,
            signals=list(sg), factors=dict(factors),
            rgm_proba=list(rgm_proba),
        )
        # Init mutable exit state
        rec._sl = sl_price
        rec._tp_hit = 0
        rec._last_tp_bar = bar_i
        rec._trail = False

        self._open.setdefault(sym, []).append(rec)

    def get_records(self):
        return self.records

    def published_records(self):
        return [r for r in self.records if r.published]


# ═══════════════════════════════════════════════════════════════════════════
# SignalMetrics — signal-quality analytics
# ═══════════════════════════════════════════════════════════════════════════

class SignalMetrics:
    """Compute signal-bot quality metrics from list of SignalRecords."""

    def __init__(self, records):
        self.all = records
        self.pub = [r for r in records if r.published]

    # --- Core hit rates ---
    def _rate(self, recs, fn):
        if not recs: return 0.0
        return sum(1 for r in recs if fn(r)) / len(recs)

    def tp1_hit_rate(self, recs=None):
        recs = recs if recs is not None else self.pub
        return self._rate(recs, lambda r: r.tp_hit >= 1)

    def tp2_hit_rate(self, recs=None):
        recs = recs if recs is not None else self.pub
        return self._rate(recs, lambda r: r.tp_hit >= 2)

    def tp3_hit_rate(self, recs=None):
        recs = recs if recs is not None else self.pub
        return self._rate(recs, lambda r: r.tp_hit >= 3)

    def sl_hit_rate(self, recs=None):
        recs = recs if recs is not None else self.pub
        return self._rate(recs, lambda r: r.tp_hit == 0 and r.exit_type == 'SL')

    def win_rate(self, recs=None):
        recs = recs if recs is not None else self.pub
        return self._rate(recs, lambda r: r.r_multiple > 0)

    # --- R-multiple stats ---
    def avg_r(self, recs=None):
        recs = recs if recs is not None else self.pub
        if not recs: return 0.0
        return float(np.mean([r.r_multiple for r in recs]))

    def median_r(self, recs=None):
        recs = recs if recs is not None else self.pub
        if not recs: return 0.0
        return float(np.median([r.r_multiple for r in recs]))

    def best_r(self, recs=None):
        recs = recs if recs is not None else self.pub
        if not recs: return 0.0
        return float(max(r.r_multiple for r in recs))

    def worst_r(self, recs=None):
        recs = recs if recs is not None else self.pub
        if not recs: return 0.0
        return float(min(r.r_multiple for r in recs))

    def profit_factor(self, recs=None):
        recs = recs if recs is not None else self.pub
        gp = sum(r.r_multiple for r in recs if r.r_multiple > 0)
        gl = abs(sum(r.r_multiple for r in recs if r.r_multiple < 0))
        if gl == 0: return float('inf') if gp > 0 else 0.0
        return gp / gl

    def expectancy(self, recs=None):
        """(WR * avg_win_R) - ((1-WR) * avg_loss_R)"""
        recs = recs if recs is not None else self.pub
        if not recs: return 0.0
        wins = [r.r_multiple for r in recs if r.r_multiple > 0]
        losses = [abs(r.r_multiple) for r in recs if r.r_multiple <= 0]
        wr = len(wins) / len(recs) if recs else 0
        avg_w = np.mean(wins) if wins else 0
        avg_l = np.mean(losses) if losses else 0
        return float(wr * avg_w - (1 - wr) * avg_l)

    # --- Timing ---
    def avg_time_to_tp1(self, recs=None):
        recs = recs if recs is not None else self.pub
        tp1_recs = [r for r in recs if r.tp_hit >= 1]
        if not tp1_recs: return 0.0
        return float(np.mean([r.time_to_tp1_h for r in tp1_recs]))

    def avg_duration(self, recs=None):
        recs = recs if recs is not None else self.pub
        if not recs: return 0.0
        return float(np.mean([r.duration_h for r in recs]))

    # --- Exit distribution ---
    def exit_distribution(self, recs=None):
        recs = recs if recs is not None else self.pub
        dist = defaultdict(int)
        for r in recs:
            dist[r.exit_type] += 1
        return dict(dist)

    # --- Per-signal-code breakdown ---
    def per_signal_stats(self, recs=None):
        """Returns {sig_code: {count, tp1_rate, avg_r, wr}}."""
        recs = recs if recs is not None else self.pub
        stats = defaultdict(lambda: {'count': 0, 'tp1': 0, 'rs': [], 'wins': 0})
        for r in recs:
            for s in r.signals:
                stats[s]['count'] += 1
                stats[s]['rs'].append(r.r_multiple)
                if r.tp_hit >= 1: stats[s]['tp1'] += 1
                if r.r_multiple > 0: stats[s]['wins'] += 1
        out = {}
        for sig, d in stats.items():
            n = d['count']
            out[sig] = {
                'count': n,
                'tp1_rate': d['tp1'] / n if n else 0,
                'avg_r': float(np.mean(d['rs'])) if d['rs'] else 0,
                'wr': d['wins'] / n if n else 0,
            }
        return out

    # --- Per-regime breakdown ---
    def per_regime_stats(self, recs=None):
        recs = recs if recs is not None else self.pub
        stats = defaultdict(lambda: {'count': 0, 'tp1': 0, 'rs': [], 'wins': 0})
        for r in recs:
            stats[r.regime]['count'] += 1
            stats[r.regime]['rs'].append(r.r_multiple)
            if r.tp_hit >= 1: stats[r.regime]['tp1'] += 1
            if r.r_multiple > 0: stats[r.regime]['wins'] += 1
        out = {}
        for rgm, d in stats.items():
            n = d['count']
            out[rgm] = {
                'count': n,
                'tp1_rate': d['tp1'] / n if n else 0,
                'avg_r': float(np.mean(d['rs'])) if d['rs'] else 0,
                'wr': d['wins'] / n if n else 0,
            }
        return out

    # --- Per-confidence-band breakdown ---
    def per_confidence_band(self, recs=None):
        recs = recs if recs is not None else self.pub
        bands = {'<0.50': [], '0.50-0.60': [], '0.60-0.70': [], '0.70+': []}
        for r in recs:
            c = r.confidence
            if c < 0.50: bands['<0.50'].append(r)
            elif c < 0.60: bands['0.50-0.60'].append(r)
            elif c < 0.70: bands['0.60-0.70'].append(r)
            else: bands['0.70+'].append(r)
        out = {}
        for band, lst in bands.items():
            n = len(lst)
            if n == 0:
                out[band] = {'count': 0, 'tp1_rate': 0, 'avg_r': 0, 'wr': 0}
            else:
                out[band] = {
                    'count': n,
                    'tp1_rate': sum(1 for r in lst if r.tp_hit >= 1) / n,
                    'avg_r': float(np.mean([r.r_multiple for r in lst])),
                    'wr': sum(1 for r in lst if r.r_multiple > 0) / n,
                }
        return out

    # --- Per-signal-count breakdown ---
    def per_signal_count(self, recs=None):
        recs = recs if recs is not None else self.pub
        groups = defaultdict(list)
        for r in recs:
            k = f"{len(r.signals)}sig" if len(r.signals) <= 4 else "5+sig"
            groups[k].append(r)
        out = {}
        for k, lst in sorted(groups.items()):
            n = len(lst)
            out[k] = {
                'count': n,
                'tp1_rate': sum(1 for r in lst if r.tp_hit >= 1) / n,
                'avg_r': float(np.mean([r.r_multiple for r in lst])),
                'wr': sum(1 for r in lst if r.r_multiple > 0) / n,
            }
        return out

    # --- Factor correlations ---
    def factor_correlations(self, recs=None):
        recs = recs if recs is not None else self.pub
        if len(recs) < 10: return {}
        try:
            from scipy.stats import spearmanr
        except ImportError:
            return {}
        r_vals = [r.r_multiple for r in recs]
        corrs = {}
        for k in _FK:
            f_vals = [r.factors.get(k, 0.5) for r in recs]
            rho, _ = spearmanr(f_vals, r_vals)
            corrs[k] = round(float(rho) if not np.isnan(rho) else 0.0, 4)
        return corrs

    # --- Summary dict ---
    def summary_dict(self):
        return {
            'signal_count': len(self.pub),
            'suppressed_count': 0,
            'tp1_hit_rate': round(self.tp1_hit_rate() * 100, 1),
            'tp2_hit_rate': round(self.tp2_hit_rate() * 100, 1),
            'tp3_hit_rate': round(self.tp3_hit_rate() * 100, 1),
            'sl_hit_rate': round(self.sl_hit_rate() * 100, 1),
            'win_rate': round(self.win_rate() * 100, 1),
            'avg_r': round(self.avg_r(), 3),
            'median_r': round(self.median_r(), 3),
            'best_r': round(self.best_r(), 3),
            'worst_r': round(self.worst_r(), 3),
            'profit_factor': round(self.profit_factor(), 2),
            'expectancy': round(self.expectancy(), 4),
            'avg_time_to_tp1_h': round(self.avg_time_to_tp1(), 1),
            'avg_duration_h': round(self.avg_duration(), 1),
        }


# ═══════════════════════════════════════════════════════════════════════════
# GateAnalyzer — gate effectiveness analysis (confidence/regime gates)
# ═══════════════════════════════════════════════════════════════════════════

class GateAnalyzer:
    """Evaluate gate effectiveness. DQN gate removed — all signals are published."""

    def __init__(self, metrics):
        self.m = metrics

    def analyze(self):
        pub = self.m.pub
        total = len(pub)

        result = {
            'total_candidates': total,
            'published': total,
            'suppressed': 0,
            'filter_rate': 0.0,
        }

        # Published quality
        result['pub_tp1_rate'] = round(self.m.tp1_hit_rate(pub) * 100, 1)
        result['pub_avg_r'] = round(self.m.avg_r(pub), 3)
        result['pub_wr'] = round(self.m.win_rate(pub) * 100, 1)

        # No suppressed signals (DQN gate removed)
        result['sup_tp1_rate'] = 0.0
        result['sup_avg_r'] = 0.0
        result['sup_wr'] = 0.0
        result['dqn_accuracy'] = 0.0

        # Confidence threshold effectiveness
        conf_stats = self.m.per_confidence_band(pub)
        result['confidence_bands'] = conf_stats

        # Regime gate
        regime_stats = self.m.per_regime_stats(pub)
        result['regime_stats'] = regime_stats

        return result


# ═══════════════════════════════════════════════════════════════════════════
# WalkForwardValidator — signal-quality walk-forward
# ═══════════════════════════════════════════════════════════════════════════

class WalkForwardValidator:
    """N-fold anchored walk-forward. Measures signal quality OOS."""

    def __init__(self, data, folds=5, embargo_bars=48, use_hmm=True,
                 conservative_tp=False):
        self.data = data
        self.folds = folds
        self.embargo = embargo_bars
        self.use_hmm = use_hmm
        self.conservative_tp = conservative_tp
        self.fold_results = []

    def run(self):
        all_times = None
        for sym in self.data:
            times = set(self.data[sym]['time'].values)
            all_times = times if all_times is None else all_times & times
        if not all_times:
            console.print("[red]No common times for walk-forward[/red]")
            return None
        times = sorted(all_times)
        n = len(times)
        fold_size = n // self.folds

        console.print(f"\n[bold gold1]Walk-Forward Validation[/bold gold1]: {self.folds} folds, "
                      f"{n} total bars, ~{fold_size} bars/fold, {self.embargo}h embargo")

        all_oos_records = []

        for fold in range(self.folds):
            is_end = (fold + 1) * fold_size
            oos_start = is_end + self.embargo
            oos_end = min(is_end + fold_size, n)
            if oos_start >= n or oos_end <= oos_start:
                continue

            is_times = set(times[:is_end])
            oos_times = set(times[oos_start:oos_end])

            console.print(f"  Fold {fold+1}/{self.folds}: IS={is_end} bars, "
                          f"OOS={len(oos_times)} bars, embargo={self.embargo}h")

            # Build IS data
            is_data = {}
            for sym in self.data:
                df = self.data[sym]
                mask = df['time'].isin(is_times)
                is_df = df[mask].reset_index(drop=True)
                if len(is_df) > 100:
                    is_data[sym] = is_df
            if not is_data:
                continue

            # Train on IS
            is_sim = SignalSimulator(is_data, self.use_hmm, fresh_brains=True,
                                    conservative_tp=self.conservative_tp)
            is_sim.run()

            # Build OOS data (with lookback context)
            oos_data = {}
            for sym in self.data:
                df = self.data[sym]
                oos_with_ctx = df[df['time'].isin(
                    set(times[max(0, oos_start - 300):oos_end])
                )].reset_index(drop=True)
                if len(oos_with_ctx) > 100:
                    oos_data[sym] = oos_with_ctx
            if not oos_data:
                continue

            # Evaluate OOS with trained brains
            oos_brains = {}
            for sym in is_sim.brains:
                if sym in oos_data:
                    oos_brains[sym] = is_sim.brains[sym]
            if not oos_brains:
                continue

            oos_sim = SignalSimulator(oos_data, self.use_hmm,
                                     brains=oos_brains, fresh_brains=False,
                                     conservative_tp=self.conservative_tp)
            oos_sim.run()
            oos_pub = oos_sim.published_records()
            oos_m = SignalMetrics(oos_sim.get_records())

            self.fold_results.append({
                'fold': fold + 1,
                'is_bars': is_end,
                'oos_bars': len(oos_times),
                'oos_signals': len(oos_pub),
                'oos_tp1_rate': round(oos_m.tp1_hit_rate() * 100, 1),
                'oos_avg_r': round(oos_m.avg_r(), 3),
                'oos_wr': round(oos_m.win_rate() * 100, 1),
                'oos_expectancy': round(oos_m.expectancy(), 4),
            })
            all_oos_records.extend(oos_sim.get_records())

        if not all_oos_records:
            console.print("[yellow]No OOS signals produced[/yellow]")
            return None

        agg_m = SignalMetrics(all_oos_records)
        consistency = sum(1 for f in self.fold_results if f['oos_avg_r'] > 0)
        consistency = consistency / len(self.fold_results) if self.fold_results else 0

        return {
            'folds': self.fold_results,
            'aggregate': agg_m.summary_dict(),
            'consistency': round(consistency, 2),
        }


# ═══════════════════════════════════════════════════════════════════════════
# MonteCarloValidator — permutation test for signal-bot edge
# ═══════════════════════════════════════════════════════════════════════════

class MonteCarloValidator:
    """Shuffle signal outcomes to test whether signal selection has genuine edge."""

    def __init__(self, records, n_sims=5000):
        self.pub = [r for r in records if r.published]
        self.all = [r for r in records if r.exit_type]  # all closed records
        self.n_sims = n_sims

    @staticmethod
    def _block_resample(arr, block_size):
        """Block bootstrap: resample blocks of consecutive trades to preserve temporal clustering."""
        n = len(arr)
        if block_size >= n:
            return arr.copy()
        n_blocks = int(np.ceil(n / block_size))
        starts = np.random.randint(0, n - block_size + 1, size=n_blocks)
        blocks = [arr[s:s + block_size] for s in starts]
        return np.concatenate(blocks)[:n]

    def run(self):
        if len(self.pub) < 10:
            console.print("[yellow]Too few signals for Monte Carlo (<10)[/yellow]")
            return None

        console.print(f"\n[bold gold1]Monte Carlo Analysis[/bold gold1]: {self.n_sims} simulations, "
                      f"{len(self.pub)} published signals")

        rs = np.array([r.r_multiple for r in self.pub])
        tp1_flags = np.array([1 if r.tp_hit >= 1 else 0 for r in self.pub])
        actual_tp1 = float(np.mean(tp1_flags))
        actual_avg_r = float(np.mean(rs))
        n_pub = len(self.pub)

        # Full candidate pool for permutation test
        all_rs = np.array([r.r_multiple for r in self.all])
        all_tp1 = np.array([1 if r.tp_hit >= 1 else 0 for r in self.all])
        n_all = len(all_rs)

        console.print(f"[dim]Candidate pool: {n_all} total signals ({n_pub} published)[/dim]")

        perm_tp1_rates = []
        perm_avg_rs = []

        with Progress(
            TextColumn("[bold]Permutation test"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("", total=self.n_sims)
            for _ in range(self.n_sims):
                # Randomly select n_pub signals from the full pool
                idx = np.random.choice(n_all, size=min(n_pub, n_all), replace=False)
                perm_avg_rs.append(float(np.mean(all_rs[idx])))
                perm_tp1_rates.append(float(np.mean(all_tp1[idx])))
                progress.update(task, advance=1)

        perm_avg_rs = np.array(perm_avg_rs)
        perm_tp1_rates = np.array(perm_tp1_rates)

        # Block bootstrap CIs — preserves temporal clustering (win/loss streaks)
        n_boot = min(self.n_sims, 2000)
        block_size = max(5, int(np.sqrt(len(rs))))  # sqrt(n) blocks
        boot_tp1 = []
        boot_avg_r = []
        boot_wr = []
        boot_pf = []
        for _ in range(n_boot):
            b_rs = self._block_resample(rs, block_size)
            b_tp1 = self._block_resample(tp1_flags, block_size)
            boot_tp1.append(float(np.mean(b_tp1)))
            boot_avg_r.append(float(np.mean(b_rs)))
            boot_wr.append(float(np.mean(b_rs > 0)))
            gp = float(np.sum(b_rs[b_rs > 0]))
            gl = abs(float(np.sum(b_rs[b_rs < 0])))
            boot_pf.append(gp / gl if gl > 0 else 10.0)

        boot_tp1 = np.array(boot_tp1)
        boot_avg_r = np.array(boot_avg_r)
        boot_wr = np.array(boot_wr)
        boot_pf = np.array(boot_pf)

        # p-values: fraction of random selections >= actual (one-sided)
        p_value = float(np.mean(perm_avg_rs >= actual_avg_r))
        p_value_tp1 = float(np.mean(perm_tp1_rates >= actual_tp1))

        return {
            'actual_tp1_rate': round(actual_tp1, 4),
            'actual_avg_r': round(actual_avg_r, 4),
            'p_value': round(p_value, 4),
            'p_value_tp1': round(p_value_tp1, 4),
            'n_candidates': n_all,
            'n_published': n_pub,
            'perm_avg_rs': perm_avg_rs.tolist(),
            'block_size': block_size,
            'bootstrap': {
                'tp1_ci': [round(float(np.percentile(boot_tp1, 2.5)), 3),
                           round(float(np.percentile(boot_tp1, 97.5)), 3)],
                'avg_r_ci': [round(float(np.percentile(boot_avg_r, 2.5)), 3),
                             round(float(np.percentile(boot_avg_r, 97.5)), 3)],
                'wr_ci': [round(float(np.percentile(boot_wr, 2.5)), 3),
                          round(float(np.percentile(boot_wr, 97.5)), 3)],
                'pf_ci': [round(float(np.percentile(boot_pf, 2.5)), 2),
                          round(float(np.percentile(boot_pf, 97.5)), 2)],
            },
        }


# ═══════════════════════════════════════════════════════════════════════════
# ReportGenerator — standalone HTML with embedded Plotly charts
# ═══════════════════════════════════════════════════════════════════════════

_BG = '#0b0b0f'
_BG2 = '#141419'
_BG3 = '#1c1c24'
_BORDER = '#2a2a35'
_TEXT = '#e8e8f0'
_MUTED = '#7a7a8a'
_GREEN = '#22c55e'
_RED = '#ef4444'
_AMBER = '#f59e0b'
_GOLD = '#c9a227'
_ACCENT = '#7b8fa3'

_PLOT_LAYOUT = dict(
    paper_bgcolor=_BG,
    plot_bgcolor=_BG2,
    font=dict(color=_TEXT, family='Inter, sans-serif', size=12),
    xaxis=dict(gridcolor=_BORDER, zeroline=False),
    yaxis=dict(gridcolor=_BORDER, zeroline=False),
    margin=dict(l=50, r=30, t=40, b=40),
)


class ReportGenerator:
    """Generate standalone HTML report with signal-bot-specific charts."""

    def __init__(self, metrics, gate_analysis, records, days=90, symbols=None,
                 wf_results=None, mc_results=None):
        self.m = metrics
        self.ga = gate_analysis
        self.records = records
        self.pub = [r for r in records if r.published]
        self.days = days
        self.symbols = symbols or []
        self.wf = wf_results
        self.mc = mc_results

    def generate(self, output_path='output/backtest_report.html'):
        if not _HAS_PLOTLY:
            console.print("[yellow]Plotly not installed — skipping HTML report[/yellow]")
            return None

        charts = []
        summary = self.m.summary_dict()

        # 1. Signal outcome pie chart
        charts.append(self._outcome_pie())
        # 2. R-multiple distribution histogram
        charts.append(self._r_distribution())
        # 3. Time-to-TP1 distribution
        charts.append(self._time_to_tp1_hist())
        # 4. Per-signal-code TP1 hit rate
        charts.append(self._signal_bars())
        # 5. Per-regime performance
        charts.append(self._regime_bars())
        # 6. Confidence vs R scatter
        charts.append(self._confidence_scatter())
        # 7. Gate effectiveness
        charts.append(self._gate_chart())
        # 8. Signal timeline
        charts.append(self._signal_timeline())
        # 9. Monthly signal quality
        charts.append(self._monthly_quality())
        # 10. Factor correlations
        try:
            charts.append(self._factor_corr_bars())
        except Exception:
            pass
        # 11. Walk-forward results
        if self.wf:
            charts.append(self._wf_chart())
        # 12. Monte Carlo
        if self.mc:
            charts.append(self._mc_chart())

        # Filter out None charts
        charts = [c for c in charts if c is not None]

        # Build HTML
        cards_html = self._summary_cards_html(summary)
        chart_divs = []
        for i, (title, fig) in enumerate(charts):
            if fig is None:
                continue
            div = fig.to_html(full_html=False, include_plotlyjs=False, div_id=f'chart-{i}')
            chart_divs.append(f"""
            <div class="section">
                <h2 class="sec-title">{title}</h2>
                <div class="chart-wrap">{div}</div>
            </div>""")

        gate_html = self._gate_summary_html()
        wf_html = self._wf_summary_html() if self.wf else ''
        mc_html = self._mc_summary_html() if self.mc else ''
        trade_table = self._signal_table_html()

        html = self._assemble_html(summary, cards_html, chart_divs, gate_html,
                                    wf_html, mc_html, trade_table)

        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)

        console.print(f"[bold green]Report saved:[/bold green] {output_path}")
        return output_path

    # --- Chart builders ---

    def _outcome_pie(self):
        """Signal outcome distribution: TP3/TP2-only/TP1-only/SL."""
        if not self.pub:
            return ('Signal Outcomes', go.Figure())
        cats = {'TP3 Hit': 0, 'TP2 Only': 0, 'TP1 Only': 0, 'Stopped Out': 0}
        for r in self.pub:
            if r.tp_hit >= 3: cats['TP3 Hit'] += 1
            elif r.tp_hit == 2: cats['TP2 Only'] += 1
            elif r.tp_hit == 1: cats['TP1 Only'] += 1
            else: cats['Stopped Out'] += 1
        colors = [_GREEN, _GOLD, _AMBER, _RED]
        fig = go.Figure(go.Pie(
            labels=list(cats.keys()), values=list(cats.values()),
            hole=0.5, marker_colors=colors,
            textinfo='label+percent',
            textfont=dict(size=11, color=_TEXT),
        ))
        fig.update_layout(**_PLOT_LAYOUT, height=350, showlegend=True,
                          legend=dict(font=dict(color=_TEXT, size=11)))
        return ('Signal Outcomes', fig)

    def _r_distribution(self):
        rs = [r.r_multiple for r in self.pub]
        if not rs:
            return ('R-Multiple Distribution', go.Figure())
        fig = go.Figure(go.Histogram(
            x=rs, nbinsx=40, marker_color=_GOLD, opacity=0.8,
        ))
        fig.add_vline(x=0, line_dash='dash', line_color=_MUTED, line_width=1)
        avg = np.mean(rs)
        fig.add_vline(x=avg, line_dash='dot',
                      line_color=_GREEN if avg > 0 else _RED,
                      line_width=2, annotation_text=f"Avg: {avg:.2f}R",
                      annotation_font_color=_TEXT)
        fig.update_layout(**_PLOT_LAYOUT, height=300, showlegend=False,
                          xaxis_title='R-Multiple', yaxis_title='Count')
        return ('R-Multiple Distribution', fig)

    def _time_to_tp1_hist(self):
        tp1_recs = [r for r in self.pub if r.tp_hit >= 1]
        if not tp1_recs:
            return ('Time to TP1', go.Figure())
        times = [r.time_to_tp1_h for r in tp1_recs]
        fig = go.Figure(go.Histogram(
            x=times, nbinsx=30, marker_color=_ACCENT, opacity=0.8,
        ))
        avg_t = np.mean(times)
        fig.add_vline(x=avg_t, line_dash='dot', line_color=_GOLD, line_width=2,
                      annotation_text=f"Avg: {avg_t:.1f}h",
                      annotation_font_color=_TEXT)
        fig.update_layout(**_PLOT_LAYOUT, height=300, showlegend=False,
                          xaxis_title='Hours to TP1', yaxis_title='Count')
        return ('Time to TP1 Distribution', fig)

    def _signal_bars(self):
        ss = self.m.per_signal_stats()
        if not ss:
            return ('Per-Signal TP1 Hit Rate', go.Figure())
        sorted_s = sorted(ss.items(), key=lambda x: x[1]['tp1_rate'], reverse=True)
        labels = [f"{s.upper()} {_SIG_DESC.get(s, '')}" for s, _ in sorted_s]
        tp1_rates = [d['tp1_rate'] * 100 for _, d in sorted_s]
        avg_rs = [d['avg_r'] for _, d in sorted_s]
        counts = [d['count'] for _, d in sorted_s]

        fig = make_subplots(rows=1, cols=2, subplot_titles=['TP1 Hit Rate %', 'Avg R-Multiple'],
                            column_widths=[0.55, 0.45])
        fig.add_trace(go.Bar(x=tp1_rates, y=labels, orientation='h',
                             marker_color=_GOLD, showlegend=False,
                             text=[f"{t:.0f}% ({c})" for t, c in zip(tp1_rates, counts)],
                             textposition='outside', textfont=dict(size=9)),
                      row=1, col=1)
        colors = [_GREEN if r > 0 else _RED for r in avg_rs]
        fig.add_trace(go.Bar(x=avg_rs, y=labels, orientation='h',
                             marker_color=colors, showlegend=False,
                             text=[f"{r:+.2f}" for r in avg_rs],
                             textposition='outside', textfont=dict(size=9)),
                      row=1, col=2)
        h = max(300, len(labels) * 28)
        fig.update_layout(**_PLOT_LAYOUT, height=h)
        for ann in fig['layout']['annotations']:
            ann['font'] = dict(color=_MUTED, size=11)
        return ('Per-Signal Performance', fig)

    def _regime_bars(self):
        rb = self.m.per_regime_stats()
        if not rb:
            return ('Per-Regime Performance', go.Figure())
        rgms = list(rb.keys())
        colors = {'bull': _GREEN, 'bear': _RED, 'range': _AMBER, 'chop': _MUTED}

        fig = make_subplots(rows=1, cols=3,
                            subplot_titles=['TP1 Hit Rate %', 'Avg R', 'Signal Count'])
        for rgm in rgms:
            c = colors.get(rgm, _ACCENT)
            fig.add_trace(go.Bar(x=[rgm], y=[rb[rgm]['tp1_rate'] * 100],
                                 marker_color=c, showlegend=False), row=1, col=1)
            fig.add_trace(go.Bar(x=[rgm], y=[rb[rgm]['avg_r']],
                                 marker_color=c, showlegend=False), row=1, col=2)
            fig.add_trace(go.Bar(x=[rgm], y=[rb[rgm]['count']],
                                 marker_color=c, showlegend=False), row=1, col=3)
        fig.update_layout(**_PLOT_LAYOUT, height=300)
        for ann in fig['layout']['annotations']:
            ann['font'] = dict(color=_MUTED, size=11)
        return ('Per-Regime Performance', fig)

    def _confidence_scatter(self):
        if not self.pub:
            return ('Confidence vs R-Multiple', go.Figure())
        confs = [r.confidence for r in self.pub]
        rs = [r.r_multiple for r in self.pub]
        colors = [_GREEN if r > 0 else _RED for r in rs]
        fig = go.Figure(go.Scatter(
            x=confs, y=rs, mode='markers',
            marker=dict(color=colors, size=6, opacity=0.6),
        ))
        fig.add_hline(y=0, line_dash='dash', line_color=_MUTED, line_width=1)
        fig.update_layout(**_PLOT_LAYOUT, height=350, showlegend=False,
                          xaxis_title='Confidence', yaxis_title='R-Multiple')
        return ('Confidence vs R-Multiple', fig)

    def _gate_chart(self):
        if not self.ga:
            return None
        pub_data = [self.ga.get('pub_tp1_rate', 0), self.ga.get('pub_avg_r', 0) * 100,
                    self.ga.get('pub_wr', 0)]
        sup_data = [self.ga.get('sup_tp1_rate', 0), self.ga.get('sup_avg_r', 0) * 100,
                    self.ga.get('sup_wr', 0)]
        labels = ['TP1 Hit Rate %', 'Avg R x100', 'Win Rate %']

        fig = go.Figure()
        fig.add_trace(go.Bar(x=labels, y=pub_data, name='Published',
                             marker_color=_GREEN, opacity=0.8))
        fig.add_trace(go.Bar(x=labels, y=sup_data, name='Suppressed',
                             marker_color=_RED, opacity=0.6))
        fig.update_layout(**_PLOT_LAYOUT, height=300, barmode='group',
                          legend=dict(font=dict(color=_TEXT, size=11)))
        return ('Gate Effectiveness', fig)

    def _signal_timeline(self):
        if not self.pub:
            return ('Signal Timeline', go.Figure())
        xs = [r.entry_time for r in self.pub]
        ys = [r.r_multiple for r in self.pub]
        exit_colors = {'SL': _RED, 'TP3': _GREEN, 'EXH': _AMBER, 'TIME': _MUTED}
        cs = [exit_colors.get(r.exit_type, _ACCENT) for r in self.pub]
        labels = [f"{r.symbol} {r.exit_type} TP{r.tp_hit}" for r in self.pub]

        # Convert unix ms to readable
        try:
            x_dt = [datetime.fromtimestamp(x / 1000, tz=timezone.utc) for x in xs]
        except Exception:
            x_dt = xs

        fig = go.Figure(go.Scatter(
            x=x_dt, y=ys, mode='markers',
            marker=dict(color=cs, size=7, opacity=0.7),
            text=labels, hoverinfo='text+y',
        ))
        fig.add_hline(y=0, line_dash='dash', line_color=_MUTED, line_width=1)
        fig.update_layout(**_PLOT_LAYOUT, height=350, showlegend=False,
                          xaxis_title='Date', yaxis_title='R-Multiple')
        return ('Signal Timeline', fig)

    def _monthly_quality(self):
        if not self.pub:
            return ('Monthly Signal Quality', go.Figure())
        # Group by month
        monthly = defaultdict(list)
        for r in self.pub:
            try:
                dt = datetime.fromtimestamp(r.entry_time / 1000, tz=timezone.utc)
                key = dt.strftime('%Y-%m')
            except Exception:
                key = 'unknown'
            monthly[key].append(r)

        months = sorted(monthly.keys())
        tp1_rates = []
        avg_rs = []
        counts = []
        for m in months:
            recs = monthly[m]
            n = len(recs)
            counts.append(n)
            tp1_rates.append(sum(1 for r in recs if r.tp_hit >= 1) / n * 100 if n else 0)
            avg_rs.append(float(np.mean([r.r_multiple for r in recs])) if recs else 0)

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.1,
                            subplot_titles=['TP1 Hit Rate %', 'Avg R-Multiple'])
        fig.add_trace(go.Bar(x=months, y=tp1_rates, marker_color=_GOLD,
                             text=[f"{t:.0f}% ({c})" for t, c in zip(tp1_rates, counts)],
                             textposition='outside', textfont=dict(size=9),
                             showlegend=False), row=1, col=1)
        colors = [_GREEN if r > 0 else _RED for r in avg_rs]
        fig.add_trace(go.Bar(x=months, y=avg_rs, marker_color=colors,
                             text=[f"{r:+.2f}" for r in avg_rs],
                             textposition='outside', textfont=dict(size=9),
                             showlegend=False), row=2, col=1)
        fig.update_layout(**_PLOT_LAYOUT, height=400)
        for ann in fig['layout']['annotations']:
            ann['font'] = dict(color=_MUTED, size=11)
        return ('Monthly Signal Quality', fig)

    def _factor_corr_bars(self):
        fc = self.m.factor_correlations()
        if not fc:
            return ('Factor-R Correlation', go.Figure())
        labels = [k.title() for k in fc]
        vals = list(fc.values())
        colors = [_GREEN if v > 0 else _RED for v in vals]
        fig = go.Figure(go.Bar(
            x=labels, y=vals, marker_color=colors,
            text=[f"{v:+.3f}" for v in vals], textposition='outside',
        ))
        fig.add_hline(y=0, line_color=_MUTED, line_width=1)
        fig.update_layout(**_PLOT_LAYOUT, height=300, showlegend=False,
                          yaxis_title='Spearman rho', xaxis_title='Factor')
        return ('Factor-R Correlation (Spearman)', fig)

    def _wf_chart(self):
        folds = self.wf.get('folds', [])
        if not folds:
            return ('Walk-Forward', go.Figure())
        fold_labels = [f"Fold {f['fold']}" for f in folds]
        oos_tp1s = [f['oos_tp1_rate'] for f in folds]
        oos_avg_rs = [f['oos_avg_r'] for f in folds]
        oos_wrs = [f['oos_wr'] for f in folds]

        fig = make_subplots(rows=1, cols=3,
                            subplot_titles=['OOS TP1 Rate %', 'OOS Avg R', 'OOS Win Rate %'])
        fig.add_trace(go.Bar(x=fold_labels, y=oos_tp1s, marker_color=_GOLD,
                             showlegend=False), row=1, col=1)
        cs = [_GREEN if r > 0 else _RED for r in oos_avg_rs]
        fig.add_trace(go.Bar(x=fold_labels, y=oos_avg_rs, marker_color=cs,
                             showlegend=False), row=1, col=2)
        fig.add_trace(go.Bar(x=fold_labels, y=oos_wrs, marker_color=_ACCENT,
                             showlegend=False), row=1, col=3)
        fig.update_layout(**_PLOT_LAYOUT, height=300)
        for ann in fig['layout']['annotations']:
            ann['font'] = dict(color=_MUTED, size=11)
        return ('Walk-Forward Results', fig)

    def _mc_chart(self):
        perm = self.mc.get('perm_avg_rs', [])
        actual = self.mc.get('actual_avg_r', 0)
        if not perm:
            return ('Monte Carlo', go.Figure())
        fig = go.Figure(go.Histogram(
            x=perm, nbinsx=60, marker_color=_ACCENT, opacity=0.7,
            name='Permuted Avg R',
        ))
        fig.add_vline(x=actual, line_dash='dash', line_color=_GOLD, line_width=2,
                      annotation_text=f"Actual: {actual:+.3f}R",
                      annotation_font_color=_GOLD)
        fig.add_vline(x=0, line_dash='dot', line_color=_MUTED, line_width=1)
        p = self.mc.get('p_value', 0)
        fig.update_layout(**_PLOT_LAYOUT, height=350, showlegend=False,
                          xaxis_title=f'Avg R-Multiple (p={p:.3f})',
                          yaxis_title='Count')
        return ('Monte Carlo — Signal Permutation', fig)

    # --- HTML helpers ---

    def _summary_cards_html(self, s):
        def _c(val, suffix=''):
            if isinstance(val, (int, float)):
                cls = 'up' if val > 0 else ('dn' if val < 0 else '')
            else:
                cls = ''
            return f'<span class="{cls}">{val}{suffix}</span>'

        cards = [
            ('Signals Published', str(s['signal_count'])),
            ('TP1 Hit Rate', f"<span class='{'up' if s['tp1_hit_rate']>50 else 'dn'}'>{s['tp1_hit_rate']}%</span>"),
            ('TP2 Hit Rate', f"{s['tp2_hit_rate']}%"),
            ('TP3 Hit Rate', f"{s['tp3_hit_rate']}%"),
            ('SL Hit Rate', f"<span class='dn'>{s['sl_hit_rate']}%</span>"),
            ('Win Rate', f"{s['win_rate']}%"),
            ('Avg R', _c(s['avg_r'])),
            ('Median R', _c(s['median_r'])),
            ('Best R', _c(s['best_r'])),
            ('Worst R', _c(s['worst_r'])),
            ('Profit Factor', str(s['profit_factor'])),
            ('Expectancy (R)', _c(s['expectancy'])),
            ('Avg Time to TP1', f"{s['avg_time_to_tp1_h']}h"),
            ('Avg Duration', f"{s['avg_duration_h']}h"),
        ]
        inner = ''.join(f'<div class="card"><div class="card-label">{lbl}</div>'
                        f'<div class="card-val">{val}</div></div>' for lbl, val in cards)
        return f'<div class="cards">{inner}</div>'

    def _gate_summary_html(self):
        if not self.ga:
            return ''
        ga = self.ga
        return f"""<div class="section">
<h2 class="sec-title">Gate Analysis</h2>
<div class="wf-grid">
<div class="info-card"><div class="info-label">Total Candidates</div>
<div class="info-val">{ga.get('total_candidates',0)}</div></div>
<div class="info-card"><div class="info-label">Published</div>
<div class="info-val">{ga.get('published',0)}</div></div>
<div class="info-card"><div class="info-label">Published TP1 / Avg R / WR</div>
<div class="info-val">{ga.get('pub_tp1_rate',0)}% / {ga.get('pub_avg_r',0):+.3f}R / {ga.get('pub_wr',0)}%</div></div>
</div></div>"""

    def _wf_summary_html(self):
        if not self.wf:
            return ''
        agg = self.wf.get('aggregate', {})
        consistency = self.wf.get('consistency', 0)
        return f"""<div class="section">
<h2 class="sec-title">Walk-Forward Summary (OOS Aggregate)</h2>
<div class="wf-grid">
<div class="info-card"><div class="info-label">OOS Signals</div>
<div class="info-val">{agg.get('signal_count',0)}</div></div>
<div class="info-card"><div class="info-label">OOS TP1 Hit Rate</div>
<div class="info-val">{agg.get('tp1_hit_rate',0)}%</div></div>
<div class="info-card"><div class="info-label">OOS Avg R</div>
<div class="info-val {'up' if agg.get('avg_r',0)>0 else 'dn'}">{agg.get('avg_r',0):+.3f}</div></div>
<div class="info-card"><div class="info-label">OOS Win Rate</div>
<div class="info-val">{agg.get('win_rate',0)}%</div></div>
<div class="info-card"><div class="info-label">OOS Expectancy</div>
<div class="info-val">{agg.get('expectancy',0):.4f}</div></div>
<div class="info-card"><div class="info-label">Consistency (folds w/ +R)</div>
<div class="info-val">{consistency:.0%}</div></div>
</div></div>"""

    def _mc_summary_html(self):
        if not self.mc:
            return ''
        bs = self.mc.get('bootstrap', {})
        return f"""<div class="section">
<h2 class="sec-title">Monte Carlo Summary</h2>
<div class="mc-grid">
<div class="info-card"><div class="info-label">p-value (signal edge test)</div>
<div class="info-val">{self.mc.get('p_value','-')}</div></div>
<div class="info-card"><div class="info-label">Actual TP1 Rate</div>
<div class="info-val">{self.mc.get('actual_tp1_rate',0):.1%}</div></div>
<div class="info-card"><div class="info-label">Actual Avg R</div>
<div class="info-val">{self.mc.get('actual_avg_r',0):+.4f}</div></div>
<div class="info-card"><div class="info-label">TP1 Rate 95% CI</div>
<div class="info-val">{bs.get('tp1_ci',['?','?'])[0]:.1%} -- {bs.get('tp1_ci',['?','?'])[1]:.1%}</div></div>
<div class="info-card"><div class="info-label">Avg R 95% CI</div>
<div class="info-val">{bs.get('avg_r_ci',['?','?'])[0]:+.3f} -- {bs.get('avg_r_ci',['?','?'])[1]:+.3f}</div></div>
<div class="info-card"><div class="info-label">Win Rate 95% CI</div>
<div class="info-val">{bs.get('wr_ci',['?','?'])[0]:.1%} -- {bs.get('wr_ci',['?','?'])[1]:.1%}</div></div>
<div class="info-card"><div class="info-label">Profit Factor 95% CI</div>
<div class="info-val">{bs.get('pf_ci',['?','?'])[0]} -- {bs.get('pf_ci',['?','?'])[1]}</div></div>
</div></div>"""

    def _signal_table_html(self):
        if not self.pub:
            return '<div class="info-card">No signals published</div>'
        rows = []
        for r in self.pub:
            r_cls = 'up' if r.r_multiple > 0 else 'dn'
            sigs_str = ', '.join(s.upper() for s in r.signals)
            try:
                dt = datetime.fromtimestamp(r.entry_time / 1000, tz=timezone.utc).strftime('%m-%d %H:%M')
            except Exception:
                dt = str(r.bar_idx)
            rows.append(
                f"<tr><td>{r.symbol}</td>"
                f"<td>{dt}</td>"
                f"<td class='mono'>{r.entry_price:.6g}</td>"
                f"<td class='mono'>{r.exit_price:.6g}</td>"
                f"<td class='{r_cls} mono'>{r.r_multiple:+.2f}</td>"
                f"<td>{r.exit_type}</td>"
                f"<td>TP{r.tp_hit}</td>"
                f"<td>{r.regime}</td>"
                f"<td>{r.confidence:.2f}</td>"
                f"<td>{r.duration_h:.0f}h</td>"
                f"<td>{sigs_str}</td></tr>"
            )
        return f"""<div class="trade-wrap"><table>
<thead><tr><th>Symbol</th><th>Date</th><th>Entry</th><th>Exit</th><th>R</th>
<th>Type</th><th>TP</th><th>Regime</th><th>Conf</th><th>Dur</th><th>Signals</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""

    def _assemble_html(self, summary, cards_html, chart_divs, gate_html,
                       wf_html, mc_html, trade_table):
        n_pub = summary['signal_count']
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GoldenEye Signal Bot Backtest</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
:root {{
  --bg: {_BG}; --bg2: {_BG2}; --bg3: {_BG3};
  --border: {_BORDER}; --text: {_TEXT}; --muted: {_MUTED};
  --green: {_GREEN}; --red: {_RED}; --amber: {_AMBER};
  --gold: {_GOLD}; --accent: {_ACCENT};
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text);
       min-height: 100vh; font-size: 15px; line-height: 1.5; padding: 20px; }}
.container {{ max-width: 1100px; margin: 0 auto; }}
h1 {{ font-size: 1.6rem; font-weight: 700; color: var(--gold); margin-bottom: 4px;
     letter-spacing: .15em; }}
.subtitle {{ color: var(--muted); font-size: .85rem; margin-bottom: 24px; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
         gap: 12px; margin-bottom: 28px; }}
.card {{ background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
        padding: 14px; text-align: center; }}
.card-label {{ color: var(--muted); font-size: .7rem; text-transform: uppercase;
              letter-spacing: .06em; }}
.card-val {{ font-family: 'JetBrains Mono', monospace; font-size: 1.15rem;
            font-weight: 700; margin-top: 4px; }}
.section {{ margin-bottom: 32px; }}
.sec-title {{ font-size: .85rem; color: var(--muted); text-transform: uppercase;
             letter-spacing: .08em; margin-bottom: 10px;
             border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
.chart-wrap {{ background: var(--bg2); border: 1px solid var(--border);
              border-radius: 8px; padding: 8px; overflow: hidden; }}
.up {{ color: var(--green); }} .dn {{ color: var(--red); }}
table {{ width: 100%; border-collapse: collapse; font-size: .82rem; }}
th {{ text-align: left; padding: 8px 10px; color: var(--muted); font-size: .72rem;
     text-transform: uppercase; letter-spacing: .05em;
     border-bottom: 1px solid var(--border); background: var(--bg2); position: sticky; top: 0; }}
td {{ padding: 6px 10px; border-bottom: 1px solid rgba(42,42,53,.4); }}
tbody tr:hover td {{ background: var(--bg3); }}
.trade-wrap {{ max-height: 500px; overflow-y: auto; background: var(--bg2);
              border: 1px solid var(--border); border-radius: 8px; }}
.mono {{ font-family: 'JetBrains Mono', monospace; }}
.wf-grid, .mc-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 10px; }}
.info-card {{ background: var(--bg2); border: 1px solid var(--border); border-radius: 6px;
             padding: 12px; }}
.info-label {{ color: var(--muted); font-size: .72rem; text-transform: uppercase; }}
.info-val {{ font-family: 'JetBrains Mono', monospace; font-weight: 600; margin-top: 2px; }}
@media (max-width: 700px) {{
  .cards {{ grid-template-columns: repeat(2, 1fr); }}
  .wf-grid, .mc-grid {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<div class="container">
<h1>GOLDENEYE SIGNAL BACKTEST</h1>
<div class="subtitle">{self.days} days &middot; {len(self.symbols)} symbols &middot;
{n_pub} signals published &middot; Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>

{cards_html}

{gate_html}

{''.join(chart_divs)}

{wf_html}
{mc_html}

<div class="section">
<h2 class="sec-title">Signal Log</h2>
{trade_table}
</div>

</div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════
# Console summary — always printed
# ═══════════════════════════════════════════════════════════════════════════

def print_summary(metrics, gate, wf=None, mc=None, symbols=None, days=0):
    s = metrics.summary_dict()

    console.print()
    console.print(Panel(
        f"[bold gold1]GOLDENEYE SIGNAL BACKTEST[/bold gold1]\n"
        f"[dim]{days} days | {len(symbols or [])} symbols | "
        f"{s['signal_count']} signals published | "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}[/dim]",
        border_style="gold1", width=72,
    ))

    # Primary metrics table
    t = Table(title="Signal Quality", show_header=False, border_style="dim",
              padding=(0, 2), width=70)
    t.add_column("Metric", style="dim", width=22)
    t.add_column("Value", justify="right", width=14)
    t.add_column("Metric", style="dim", width=22)
    t.add_column("Value", justify="right", width=14)

    def _cv(val, fmt='.2f'):
        v = float(val)
        c = 'green' if v > 0 else 'red' if v < 0 else ''
        return f"[{c}]{v:{fmt}}[/{c}]" if c else f"{v:{fmt}}"

    rows = [
        ('TP1 Hit Rate', f"{s['tp1_hit_rate']}%", 'Avg R', _cv(s['avg_r'], '.3f')),
        ('TP2 Hit Rate', f"{s['tp2_hit_rate']}%", 'Median R', _cv(s['median_r'], '.3f')),
        ('TP3 Hit Rate', f"{s['tp3_hit_rate']}%", 'Best R', _cv(s['best_r'], '.3f')),
        ('SL Hit Rate', f"[red]{s['sl_hit_rate']}%[/red]", 'Worst R', _cv(s['worst_r'], '.3f')),
        ('Win Rate', f"{s['win_rate']}%", 'Profit Factor', f"{s['profit_factor']:.2f}"),
        ('Expectancy (R)', _cv(s['expectancy'], '.4f'), 'Avg Time to TP1', f"{s['avg_time_to_tp1_h']}h"),
        ('Avg Duration', f"{s['avg_duration_h']}h", '', ''),
    ]
    for r in rows:
        t.add_row(*r)
    console.print(t)

    # Gate analysis
    if gate:
        gt = Table(title="Gate Analysis", border_style="dim", padding=(0, 1))
        gt.add_column("Metric", width=24)
        gt.add_column("Published", justify="right", width=14)
        gt.add_column("Suppressed", justify="right", width=14)
        gt.add_row("Count", str(gate.get('published', 0)), str(gate.get('suppressed', 0)))
        gt.add_row("TP1 Hit Rate", f"{gate.get('pub_tp1_rate', 0)}%", f"{gate.get('sup_tp1_rate', 0)}%")
        gt.add_row("Avg R", f"{gate.get('pub_avg_r', 0):+.3f}", f"{gate.get('sup_avg_r', 0):+.3f}")
        gt.add_row("Win Rate", f"{gate.get('pub_wr', 0)}%", f"{gate.get('sup_wr', 0)}%")
        console.print(gt)

    # Regime breakdown
    rb = metrics.per_regime_stats()
    if rb:
        rt = Table(title="Per-Regime Signal Quality", border_style="dim", padding=(0, 1))
        rt.add_column("Regime", width=8)
        rt.add_column("Signals", justify="right", width=8)
        rt.add_column("TP1%", justify="right", width=8)
        rt.add_column("Avg R", justify="right", width=8)
        rt.add_column("WR%", justify="right", width=8)
        rgm_colors = {'bull': 'green', 'bear': 'red', 'range': 'yellow', 'chop': 'dim'}
        for rgm in ['bull', 'bear', 'range', 'chop']:
            if rgm in rb:
                d = rb[rgm]
                c = rgm_colors.get(rgm, '')
                rt.add_row(f"[{c}]{rgm}[/{c}]", str(d['count']),
                           f"{d['tp1_rate']*100:.1f}", f"{d['avg_r']:+.3f}",
                           f"{d['wr']*100:.1f}")
        console.print(rt)

    # Signal attribution (top 10)
    sa = metrics.per_signal_stats()
    if sa:
        st_tbl = Table(title="Signal Attribution (top 10 by TP1 rate)", border_style="dim", padding=(0, 1))
        st_tbl.add_column("Sig", width=4)
        st_tbl.add_column("Name", width=20)
        st_tbl.add_column("Count", justify="right", width=6)
        st_tbl.add_column("TP1%", justify="right", width=6)
        st_tbl.add_column("Avg R", justify="right", width=8)
        st_tbl.add_column("WR%", justify="right", width=6)
        sorted_sigs = sorted(sa.items(), key=lambda x: x[1]['tp1_rate'], reverse=True)[:10]
        for sig, d in sorted_sigs:
            st_tbl.add_row(sig.upper(), _SIG_DESC.get(sig, ''),
                           str(d['count']), f"{d['tp1_rate']*100:.0f}",
                           f"{d['avg_r']:+.3f}", f"{d['wr']*100:.0f}")
        console.print(st_tbl)

    # Exit distribution
    ed = metrics.exit_distribution()
    if ed:
        et = Table(title="Exit Distribution", border_style="dim", padding=(0, 1))
        et.add_column("Type", width=10)
        et.add_column("Count", justify="right", width=8)
        et.add_column("%", justify="right", width=8)
        total = sum(ed.values())
        for xt, cnt in sorted(ed.items(), key=lambda x: -x[1]):
            et.add_row(xt, str(cnt), f"{cnt/total*100:.1f}")
        console.print(et)

    # Confidence bands
    cb = metrics.per_confidence_band()
    if cb:
        ct = Table(title="Confidence Band Analysis", border_style="dim", padding=(0, 1))
        ct.add_column("Band", width=12)
        ct.add_column("Signals", justify="right", width=8)
        ct.add_column("TP1%", justify="right", width=8)
        ct.add_column("Avg R", justify="right", width=8)
        ct.add_column("WR%", justify="right", width=8)
        for band in ['<0.50', '0.50-0.60', '0.60-0.70', '0.70+']:
            if band in cb:
                d = cb[band]
                ct.add_row(band, str(d['count']),
                           f"{d['tp1_rate']*100:.1f}", f"{d['avg_r']:+.3f}",
                           f"{d['wr']*100:.1f}")
        console.print(ct)

    # Walk-forward summary
    if wf:
        agg = wf.get('aggregate', {})
        consistency = wf.get('consistency', 0)
        console.print(Panel(
            f"[bold]Walk-Forward (OOS Aggregate)[/bold]\n"
            f"Signals: {agg.get('signal_count',0)} | "
            f"TP1 Rate: {agg.get('tp1_hit_rate',0)}% | "
            f"Avg R: {agg.get('avg_r',0):+.3f} | "
            f"WR: {agg.get('win_rate',0)}% | "
            f"Consistency: {consistency:.0%}",
            border_style="dim", width=72,
        ))

    # MC summary
    if mc:
        bs = mc.get('bootstrap', {})
        console.print(Panel(
            f"[bold]Monte Carlo ({len(mc.get('perm_avg_rs',[]))} sims)[/bold]\n"
            f"p-value: {mc.get('p_value','-')} | "
            f"Actual TP1: {mc.get('actual_tp1_rate',0):.1%} | "
            f"Actual Avg R: {mc.get('actual_avg_r',0):+.3f}\n"
            f"Avg R 95% CI: {bs.get('avg_r_ci',['?','?'])} | "
            f"WR 95% CI: {[round(x*100,1) for x in bs.get('wr_ci',[0,0])]}%",
            border_style="dim", width=72,
        ))

    console.print()


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description='GoldenEye Signal Bot Backtester')
    p.add_argument('--days', type=int, default=90, help='Lookback period in days')
    p.add_argument('--symbols', nargs='+', default=None,
                   help='Symbols to backtest (e.g. BTC/USD ETH/USD)')
    p.add_argument('--walk-forward', action='store_true', help='Run walk-forward validation')
    p.add_argument('--folds', type=int, default=5, help='Walk-forward folds')
    p.add_argument('--monte-carlo', action='store_true', help='Run Monte Carlo analysis')
    p.add_argument('--mc-sims', type=int, default=5000, help='Monte Carlo simulations')
    p.add_argument('--full', action='store_true', help='Run all validations')
    p.add_argument('--no-hmm', action='store_true', help='Disable HMM regime detection')
    p.add_argument('--conservative-tp', action='store_true', help='Require close>TP3 (not just wick)')
    p.add_argument('--no-report', action='store_true', help='Skip HTML report generation')
    p.add_argument('--output', default='output/backtest_report.html', help='Report output path')
    p.add_argument('--pickle', default=None, help='Load candles from pickle file (bypasses Kraken)')
    p.add_argument('--window', default=None, help='Pickle window key (e.g. RECENT_2025Q4_2026Q1)')
    p.add_argument('--metrics-out', default=None, help='Save metrics JSON to this path')
    p.add_argument('--move-sl-tp1', action='store_true', help='Move SL to entry+0.5ATR at TP1 (LEGACY — live bot does NOT do this)')
    p.add_argument('--conf-floor-bull', type=float, default=None, help='Override bull regime conf floor')
    p.add_argument('--conf-floor-bear', type=float, default=None, help='Override bear regime conf floor')
    p.add_argument('--conf-floor-range', type=float, default=None, help='Override range regime conf floor')
    p.add_argument('--conf-floor-chop', type=float, default=None, help='Override chop regime conf floor')
    return p.parse_args()


def main():
    args = parse_args()
    if args.full:
        args.walk_forward = True
        args.monte_carlo = True

    console.print(Panel(
        "[bold gold1]G O L D E N E Y E[/bold gold1]  [dim]Signal Bot Backtester[/dim]",
        border_style="gold1", width=50,
    ))

    # Init exchange (public endpoints only) — skipped when loading from pickle
    if args.pickle:
        console.print(f"[dim]Loading from pickle: {args.pickle} [{args.window}][/dim]")
        if not args.window:
            console.print("[red]--window required when using --pickle[/red]")
            return
        data = load_regime_pickle(args.pickle, args.window, symbols=args.symbols)
        if not data:
            console.print("[red]Pickle load returned no data[/red]")
            return
        syms = sorted(data.keys())
        console.print(f"[bold]{len(syms)} symbols loaded from pickle:[/bold] {', '.join(syms[:10])}"
                      f"{'...' if len(syms) > 10 else ''}")
    else:
        console.print("[dim]Initializing Kraken exchange...[/dim]")
        ex = ccxt.kraken({'enableRateLimit': True})
        try:
            ex.load_markets()
        except Exception as e:
            console.print(f"[red]Failed to load markets: {e}[/red]")
            return
        set_exchange(ex)

        # Discover symbols
        if args.symbols:
            syms = args.symbols
        else:
            console.print("[dim]Discovering top symbols...[/dim]")
            syms = top_sym(20)
        console.print(f"[bold]{len(syms)} symbols:[/bold] {', '.join(syms[:10])}"
                      f"{'...' if len(syms) > 10 else ''}")

        # Fetch OHLCV data
        data = fetch_multi(ex, syms, '1h', args.days)
    if not data:
        console.print("[red]No data fetched — exiting[/red]")
        return
    console.print(f"[green]Loaded {len(data)} symbols[/green] "
                  f"({sum(len(df) for df in data.values()):,} total bars)")

    # === Primary Signal Simulation ===
    console.print("\n[bold gold1]Running signal simulation...[/bold gold1]")
    conf_override = None
    if any(getattr(args, f'conf_floor_{r}') is not None for r in ('bull','bear','range','chop')):
        from goldeneye import _RGM_CONF_MIN as _default_floors
        conf_override = dict(_default_floors)
        for r in ('bull','bear','range','chop'):
            v = getattr(args, f'conf_floor_{r}')
            if v is not None:
                conf_override[r] = v
        console.print(f"[yellow]Confidence floor override: {conf_override}[/yellow]")

    sim = SignalSimulator(
        data,
        use_hmm=not args.no_hmm,
        conservative_tp=args.conservative_tp,
        move_sl_at_tp1=args.move_sl_tp1,
        conf_floor_override=conf_override,
    )
    sim.run()

    records = sim.get_records()
    pub = sim.published_records()

    if not pub:
        console.print("[yellow]No signals published[/yellow]")
        return

    # Compute metrics
    try:
        from scipy.stats import spearmanr
    except ImportError:
        console.print("[yellow]scipy not installed — factor correlations will be skipped[/yellow]")

    metrics = SignalMetrics(records)
    gate_analyzer = GateAnalyzer(metrics)
    gate = gate_analyzer.analyze()

    # === Walk-Forward ===
    wf_results = None
    if args.walk_forward:
        wfv = WalkForwardValidator(
            data, folds=args.folds,
            use_hmm=not args.no_hmm,
            conservative_tp=args.conservative_tp,
        )
        wf_results = wfv.run()

    # === Monte Carlo ===
    mc_results = None
    if args.monte_carlo:
        mcv = MonteCarloValidator(records, args.mc_sims)
        mc_results = mcv.run()

    # === Console Summary ===
    print_summary(metrics, gate, wf_results, mc_results, list(data.keys()), args.days)

    # === HTML Report ===
    if not args.no_report:
        rg = ReportGenerator(
            metrics, gate, records, args.days, list(data.keys()),
            wf_results, mc_results,
        )
        rg.generate(args.output)

    # Save signal log JSON
    signals_path = os.path.join('output', 'backtest_signals.json')
    try:
        with open(signals_path, 'w') as f:
            json.dump([r.to_dict() for r in pub], f, indent=2, default=str)
        console.print(f"[dim]Signal log saved: {signals_path}[/dim]")
    except Exception:
        pass

    # Save metrics JSON
    metrics_path = args.metrics_out or os.path.join('output', 'backtest_metrics.json')
    try:
        out = metrics.summary_dict()
        out['gate_analysis'] = gate
        with open(metrics_path, 'w') as f:
            json.dump(out, f, indent=2, default=str)
        console.print(f"[dim]Metrics saved: {metrics_path}[/dim]")
    except Exception:
        pass


if __name__ == '__main__':
    main()
