#!/usr/bin/env python3
"""
SENTINEL — Probabilistic Forecast Engine
==========================================
Bot #13 (replaces Viper). Port 8071. Color: #00bfa5 (teal).

For each pair, computes forward probability distributions by combining:
  1. Monte Carlo simulation (log-normal random walk)
  2. Regime-conditioned drift (from Gaussian fusion)
  3. PHITEX phase adjustment (widen distribution if pre-critical)
  4. Order book gravity (Brainiac depth biases direction)
  5. Whale momentum (Deep Blue activity biases direction)
  6. Mean reversion anchor (distance from multi-TF EMAs)

Outputs: expected price, confidence intervals, skew, tail risk
         at 1h, 4h, 24h horizons.

Usage: python sentinel.py
"""

import json
import math
import os
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

import requests

# Fleet integration
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "CommandCenter"))
try:
    from event_publisher import EventPublisher
except ImportError:
    EventPublisher = None
try:
    from fleet_config import is_blacklisted as _is_blacklisted
except ImportError:
    _is_blacklisted = lambda pair: False
try:
    from kraken_ohlc import fetch_ohlc as _fetch_ohlc_canonical
except ImportError:
    _fetch_ohlc_canonical = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 8071
CC_URL = "http://127.0.0.1:9000"
SCAN_INTERVAL = 300         # 5 minutes between forecast cycles
# Cold-start tolerance: Sentinel can boot before Command Center has populated
# /api/universe. Without a retry the first cycle degrades to three majors and
# holds it for a full SCAN_INTERVAL (5 min).
#
# Sizing: Sentinel is a phase-2 bot, launched as soon as CC BINDS its port —
# but CC needs appreciably longer to finish its first poll sweep and populate
# the universe. Measured on a cold boot, 5x3s (12s) still landed in the empty
# window. 20x3s covers a full minute, which comfortably exceeds CC's observed
# warm-up while still costing nothing on a warm start (the first attempt
# succeeds and returns immediately).
UNIVERSE_RETRIES = 20
UNIVERSE_RETRY_WAIT_S = 3
# Pairs used only when the universe is genuinely unreachable. Kept as a named
# constant so the scan loop can detect that it ran a degraded cycle.
_FALLBACK_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD"]
DEGRADED_RETRY_S = 30       # re-run soon after a fallback cycle
ORACLE_CATCHUP_MAX = 15     # cap the end-of-cycle catch-up pass
HORIZONS = [60, 240, 1440]  # minutes: 1h, 4h, 24h
# Monte Carlo paths per pair. Raised 500 -> 2000 (2026-07-29 audit): at 500,
# re-running an IDENTICAL input with different seeds moved the up-probability
# by up to 9.6pp (sigma 2.2pp), so the whole-percent figures on the matrix
# ("49%" vs "51%") were entirely inside the sampling noise floor.
#   N=500  -> +/-2.24pp (1 s.e.)   N=2000 -> +/-1.12pp   N=5000 -> +/-0.71pp
# 5000 was measured at ~164s of a 300s cycle — too close to the budget. 2000
# costs ~65s (22%) and halves the error. prob_se is published per forecast so
# consumers can check the noise floor themselves rather than trusting digits.
N_SIMULATIONS = 2000
# Width of the flat/dead band, in standard deviations of THIS pair over THIS
# horizon. Replaces a hardcoded +/-0.1%, which meant 2.5 sigma for a stablecoin
# but 0.01 sigma for a memecoin — see compute_distribution for the full note.
# 0.25 sigma keeps the band meaningful without swallowing real moves.
DEAD_BAND_SIGMA = 0.25
# Reference per-minute volatility used to convert the absolute drift terms into
# a per-sigma tilt. Roughly a mid-cap crypto pair (~1.2%/hr), so a pair at this
# vol keeps the drift the terms were originally tuned for; quieter pairs stop
# saturating and noisier ones stop having their signal washed out.
DRIFT_REF_VOL = 0.012 / math.sqrt(60)
# Hard cap on drift expressed in sigma-per-minute. 0.05 keeps a 4h forecast
# well short of certainty even with every term maxed, so no combination of
# fleet inputs can produce a 100% call.
#
# Measured 2026-08-13: this cap NEVER BINDS. The four drift terms sum to at
# most base 1e-5 + book 5e-6 + whale 5e-6 + reversion ~1e-6 = 2.1e-5, which
# is drift_snr 0.0136 — 27% of this cap. The cap is a real backstop for a
# future term, not a live constraint, and is left alone deliberately.
DRIFT_SNR_CAP = 0.05
# High-conviction gate, in direction probability at the 4h horizon.
#
# Was 0.65, which NO market condition could reach. Measured by running the
# shipped monte_carlo_paths/compute_distribution at the true drift ceiling
# (snr 0.0136): max achievable P(up) is 0.482 — an 0.168 gap the inputs
# cannot close, so self.high_conviction was permanently empty and every
# downstream consumer (bus_listener, card_renderer, signal_broadcaster,
# signal_decay) received nothing from this path. Confirmed on the durable
# bus log: 2,602 HIGH_CONVICTION events in 24h, ALL from event_bus reaction
# rules, ZERO from sentinel.
#
# 0.65 was correct against the pre-2026-07-29 drift scale and was left
# behind when drift was rescaled into sigma units — the same shape as the
# other findings in this audit.
#
# 0.45 sits inside the achievable range while still requiring a genuine
# directional tilt: P(up) 0.45 against P(down) ~0.33 at the same reading,
# with the rest in the flat band. Raising the drift terms instead would
# manufacture conviction the inputs do not support, which is the opposite
# of the fix.
HIGH_CONVICTION_PROB = 0.45
# Forecast the full CC universe, not just the top slice by volume.
#
# At 20 this covered only 14 of Oracle's scanned pairs, leaving 13 that CC
# already tracks with no Sentinel forecast — including pairs Confluence was
# actively holding (CRV, MANA). Confluence needs 2+ agreeing sources, so an
# unforecast pair can only reach quorum via NEXUS (a fleet-wide posture, not
# per-pair confirmation), which weakens the whole premise of the bot.
#
# Cost is not a constraint: measured 0.52s per pair, so 50 pairs is ~26s of a
# 300s cycle (9%). Raising this trades idle time for real signal overlap.
TOP_PAIRS = 50              # forecast the full CC universe


# ---------------------------------------------------------------------------
# Monte Carlo Engine
# ---------------------------------------------------------------------------

def monte_carlo_paths(price, vol_per_min, drift_per_min, horizon_min, n_paths):
    """Generate Monte Carlo price paths.

    Uses geometric Brownian motion:
    dS = mu*S*dt + sigma*S*dW

    Returns list of final prices.
    """
    dt = 1.0  # 1 minute steps
    steps = horizon_min
    finals = []

    for _ in range(n_paths):
        s = price
        for _ in range(steps):
            dW = random.gauss(0, 1)
            s *= math.exp((drift_per_min - 0.5 * vol_per_min ** 2) * dt
                          + vol_per_min * math.sqrt(dt) * dW)
        finals.append(s)

    return sorted(finals)


def _sigfig(x, sig=5):
    """Round to significant figures, not fixed decimals.

    round(x, 4) quantises every price to a 0.0001 tick, which is fine for BTC
    but catastrophic for sub-cent pairs: PUMP at $0.0019 has a tick worth 5.3%
    of its own price, so its whole 68% CI collapsed to [0.0018, 0.0020] — the
    rounding error exceeded a typical 1h move (2026-07-29 audit).
    """
    if x is None:
        return None
    try:
        if x == 0 or not math.isfinite(x):
            return x
        return round(x, -int(math.floor(math.log10(abs(x)))) + (sig - 1))
    except (ValueError, OverflowError):
        return x


def compute_distribution(finals, current_price, sigma_horizon=None):
    """Compute distribution statistics from Monte Carlo endpoints.

    `sigma_horizon` is the log-return standard deviation over this horizon. It
    sizes the flat/dead band; see the note below for why a fixed band is wrong.
    """
    n = len(finals)
    if n < 10:
        return None

    mean = sum(finals) / n
    p2_5 = finals[int(n * 0.025)]
    p5 = finals[int(n * 0.05)]
    p50 = finals[int(n * 0.50)]
    p95 = finals[int(n * 0.95)]
    p97_5 = finals[int(n * 0.975)]

    # 1-sigma (68%) interval
    p16 = finals[int(n * 0.16)]
    p84 = finals[int(n * 0.84)]

    # ── Direction probability ────────────────────────────────────────────
    # The dead band separating "up" from "flat" is scaled by this horizon's
    # sigma, NOT fixed at ±0.1%.
    #
    # With a fixed band, the threshold means something completely different
    # per instrument, because sigma varies ~250x across the universe:
    #     USDG  band = 2.50 sigma -> P(up) 100.0%
    #     EUR   band = 0.33 sigma -> P(up)  67.3%
    #     BTC   band = 0.04 sigma -> P(up)  51.8%
    #     AKE   band = 0.01 sigma -> P(up)  48.5%
    # ...all at the SAME drift. So "high conviction" (p > 0.65) was selecting
    # purely for low volatility, and Sentinel's only two HC signals were EUR
    # and a dollar-pegged stablecoin at 100% confidence. It was a volatility
    # filter wearing a forecasting label (2026-07-29 audit).
    #
    # A sigma-proportional band asks the same question of every pair: "is the
    # move meaningfully larger than this pair's own noise?"
    if sigma_horizon and sigma_horizon > 0:
        band = DEAD_BAND_SIGMA * sigma_horizon      # in log-return units
    else:
        band = 0.001                                # legacy fallback
    up_thresh = current_price * math.exp(band)
    dn_thresh = current_price * math.exp(-band)

    up = sum(1 for f in finals if f > up_thresh) / n
    down = sum(1 for f in finals if f < dn_thresh) / n
    flat = 1.0 - up - down

    # Skew: positive = bullish tilt
    if p50 > 0:
        skew = (mean - p50) / (p95 - p5 + 1e-10)
    else:
        skew = 0.0

    # Tail risk: probability of >5% move
    tail_up = sum(1 for f in finals if f > current_price * 1.05) / n
    tail_down = sum(1 for f in finals if f < current_price * 0.95) / n

    expected_move_pct = (mean - current_price) / current_price * 100

    # Monte Carlo standard error on a probability estimate: sqrt(p(1-p)/n).
    # Published so consumers can tell signal from sampling noise instead of
    # reading whole-percent differences that are inside the error bars.
    p_hat = max(up, down)
    prob_se = math.sqrt(max(p_hat * (1.0 - p_hat), 0.0) / n)

    return {
        "expected": _sigfig(mean),
        "median": _sigfig(p50),
        "ci_68": [_sigfig(p16), _sigfig(p84)],
        # ci_95 used to be [p5, p95] — that is a 90% interval, not 95%.
        # Verified against 20k ground-truth sims: actual coverage was 90.0%
        # while the key said 95. Now genuinely 95% (p2.5..p97.5), with the
        # 90% band published alongside rather than silently relabelled.
        "ci_90": [_sigfig(p5), _sigfig(p95)],
        "ci_95": [_sigfig(p2_5), _sigfig(p97_5)],
        "skew": round(skew, 4),
        "tail_risk_up": round(tail_up, 4),
        "tail_risk_down": round(tail_down, 4),
        "expected_move_pct": round(expected_move_pct, 3),
        # Band actually used, as a percentage — makes the flat bucket auditable
        # instead of an unexplained residual.
        "flat_band_pct": round((math.exp(band) - 1.0) * 100, 4),
        "prob_se": round(prob_se, 4),
        "n_sims": n,
        "direction_probability": {
            "up": round(up, 3),
            "down": round(down, 3),
            "flat": round(flat, 3),
        },
    }


# ---------------------------------------------------------------------------
# Fleet Data Fetchers
# ---------------------------------------------------------------------------

def fetch_json(url, timeout=5):
    try:
        resp = requests.get(url, timeout=timeout)
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None


def _normalise_pair(p):
    """Oracle emits bare symbols ('CRV'); the fleet speaks 'CRV/USD'."""
    p = str(p or "").strip().upper()
    if not p:
        return None
    return p if "/" in p else f"{p}/USD"


# Oracle pairs seen recently, pair -> last-seen unix ts. Oracle rotates its
# top-N faster than Sentinel's 300s cycle, so unioning only the CURRENT set
# chases a moving target: by the time forecasts are served Oracle has already
# moved on, and measured overlap oscillates between ~2/10 and ~9/10 purely on
# sampling phase. Remembering recent pairs keeps coverage stable across the
# rotation instead.
_ORACLE_SEEN = {}
ORACLE_MEMORY_S = 1800      # keep a pair in the forecast set for 30 min
# Persisted so the memory survives a restart. Held only in RAM, every cold boot
# started blind: cycle 1 saw a single Oracle poll, and since Oracle rotates
# faster than the 300s cycle, the forecast set was built against a top-10 that
# had already changed by the time forecasts were served (measured 1/10 overlap
# after a restart vs 7/10 on a warm process).
_ORACLE_SEEN_FILE = Path(__file__).parent / "oracle_seen.json"


def _load_oracle_seen():
    try:
        with open(_ORACLE_SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        return {k: float(v) for k, v in data.items()
                if now - float(v) <= ORACLE_MEMORY_S}
    except Exception:
        return {}


def _save_oracle_seen():
    try:
        tmp = str(_ORACLE_SEEN_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_ORACLE_SEEN, f)
        os.replace(tmp, _ORACLE_SEEN_FILE)
    except Exception:
        pass


def get_oracle_pairs():
    """Pairs Oracle is currently signalling on, plus recently-seen ones.

    Sentinel ranks by VOLUME while Oracle signals on mid-cap alts, so the two
    select along different axes and their sets barely intersect by chance.
    Confluence needs 2+ agreeing sources per pair, so an Oracle candidate with
    no Sentinel forecast can only reach quorum via NEXUS — a fleet-wide posture,
    not per-pair confirmation. Forecasting Oracle's actual candidates is what
    makes cross-source agreement mean anything.

    Includes pairs seen within ORACLE_MEMORY_S so a pair that rotates out of
    Oracle's top-N mid-cycle still has a fresh forecast when it rotates back.
    """
    now = time.time()
    data = fetch_json("http://127.0.0.1:8075/api/snapshot")
    if data:
        sigs = data.get("all_signals") or data.get("top_signals") or []
        for s in sigs:
            p = _normalise_pair(s.get("pair"))
            if p:
                _ORACLE_SEEN[p] = now

    # Drop pairs Oracle has not signalled on for a while, so the forecast set
    # does not grow without bound.
    for p in [p for p, ts in _ORACLE_SEEN.items() if now - ts > ORACLE_MEMORY_S]:
        _ORACLE_SEEN.pop(p, None)

    _save_oracle_seen()

    # Most-recently-seen first, so the freshest Oracle candidates are forecast
    # earliest in the cycle.
    return sorted(_ORACLE_SEEN, key=lambda p: _ORACLE_SEEN[p], reverse=True)


def get_universe():
    """Volume-ranked universe UNION Oracle's live signal pairs.

    Oracle's candidates are appended even when they fall outside the top-N by
    volume — they are precisely the pairs a forecast needs to cover for
    Confluence to reach genuine multi-source agreement.
    """
    # On a cold fleet start Sentinel can reach this before Command Center has
    # populated /api/universe. Accepting that empty answer silently collapses
    # the forecast set to three majors and HOLDS it for a full SCAN_INTERVAL
    # (5 min), which is indistinguishable from a healthy cycle downstream.
    # Retry briefly instead — CC typically fills within a few seconds.
    base = []
    for attempt in range(UNIVERSE_RETRIES):
        data = fetch_json(f"{CC_URL}/api/universe")
        if data and data.get("pairs"):
            base = [p["display"] for p in data["pairs"][:TOP_PAIRS]]
            break
        if attempt < UNIVERSE_RETRIES - 1:
            time.sleep(UNIVERSE_RETRY_WAIT_S)

    merged = list(base)
    seen = set(base)
    for p in get_oracle_pairs():
        if p not in seen:
            merged.append(p)
            seen.add(p)

    # Drop blacklisted pairs before forecasting rather than after. Some entries
    # are blacklisted precisely because Kraken has no OHLC for them (e.g.
    # XNO/USD -> EQuery:Unknown asset pair), so every cycle would otherwise
    # spend a fetch attempt on a pair that can never produce a forecast and can
    # never be traded.
    merged = [p for p in merged if not _is_blacklisted(p)]

    if merged:
        return merged
    # Genuine last resort — surface it, because a 3-pair forecast set is a
    # degraded state, not a normal one, and it must not pass unnoticed.
    # (Module-level function: no engine instance here, so print rather than
    # self._log — stdout is captured to logs/bots/sentinel.log by the launcher.)
    print(f"[WARN] Universe unavailable after {UNIVERSE_RETRIES} attempts "
          f"(CC and Oracle both empty) — falling back to 3 majors this cycle",
          flush=True)
    return list(_FALLBACK_PAIRS)


def get_ohlc(pair, interval=60, limit=100):
    if _fetch_ohlc_canonical:
        result = _fetch_ohlc_canonical(pair, interval, limit, cc_url=CC_URL)
        return result if result else None
    # Fallback if kraken_ohlc unavailable
    data = fetch_json(f"{CC_URL}/api/market/ohlc?pair={pair}&interval={interval}&limit={limit}")
    if data and data.get("candles"):
        return data["candles"]
    return None


def get_fleet_context():
    """Gather fleet intelligence for forecast conditioning."""
    ctx = {
        "regime": "RANGING",
        # None, not 0.5. A dead upstream produced a confident-looking
        # RANGING/0.5 context indistinguishable from a measured one, and
        # regime_confidence feeds base_drift -> total_drift -> the Monte
        # Carlo -> direction_probability -> high_conviction, which
        # Confluence consumes as a confirming source.
        "regime_confidence": None,
        "phitex_fleet": 0.0,
        "phitex_pairs": {},
        "whale_pairs": {},
        "book_imbalance": {},
        # None, not 0.5. Nothing currently READS these two -- they are
        # populated below and go unused -- but 0.5 is a real mid-range
        # reading for both (AEGIS scores 0-1, avg_abs_correlation 0-1), so
        # whoever wires them up next would inherit a fabrication that is
        # indistinguishable from a measurement. Left in place rather than
        # deleted because the fetches are wired and working; made honest so
        # they are safe to consume.
        "aegis_score": None,
        "correlation": None,
    }

    # Gaussian fusion regime from Nexus (via CC proxy)
    nexus_bot = fetch_json(f"{CC_URL}/api/bot/nexus")
    nexus = (nexus_bot or {}).get("raw") or {}
    if nexus:
        gf = nexus.get("gaussian_fusion", {})
        if gf.get("fused_regime"):
            ctx["regime"] = gf["fused_regime"]
            # Double fabrication: a None response already leaves the seed
            # above, and a present-but-incomplete response re-invented it
            # here. Neither layer alone could close the hole.
            ctx["regime_confidence"] = gf.get("fused_confidence")

    # PHITEX (via CC proxy)
    phitex_bot = fetch_json(f"{CC_URL}/api/bot/phitex")
    phitex = (phitex_bot or {}).get("raw") or {}
    if phitex:
        ctx["phitex_fleet"] = phitex.get("fleet_score", 0)
        for pair, pdata in phitex.get("pairs", {}).items():
            ctx["phitex_pairs"][pair] = pdata.get("phi_tex", 0)

    # Deep Blue whales
    events = fetch_json(f"{CC_URL}/api/events/recent?n=50")
    if events:
        for e in events:
            if e.get("type") == "WHALE_ALERT":
                d = e.get("data", {})
                pair = d.get("pair", "")
                if pair:
                    ctx["whale_pairs"][pair] = {
                        "score": d.get("score", 0),
                        "tier": d.get("tier", ""),
                    }

    # Brainiac order book
    for pair in ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD"]:
        depth = fetch_json(f"{CC_URL}/api/brainiac/depth?pair={pair}")
        if depth and depth.get("data"):
            ctx["book_imbalance"][pair] = depth["data"].get("imbalance", 0)

    # AEGIS (via CC proxy)
    aegis_bot = fetch_json(f"{CC_URL}/api/bot/aegis")
    aegis = (aegis_bot or {}).get("raw") or {}
    if aegis:
        _as = aegis.get("score")
        ctx["aegis_score"] = _as if isinstance(_as, (int, float)) else None

    # Brainiac correlation
    corr = fetch_json(f"{CC_URL}/api/brainiac/correlations")
    if corr and corr.get("data"):
        _c = corr["data"].get("avg_abs_correlation")
        ctx["correlation"] = _c if isinstance(_c, (int, float)) else None

    return ctx


# ---------------------------------------------------------------------------
# Forecast Engine
# ---------------------------------------------------------------------------

def compute_volatility(candles, window=20):
    """Compute annualized volatility from candle closes."""
    if not candles or len(candles) < window + 1:
        return 0.02  # 2% default
    closes = [c[4] for c in candles]
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1]
               for i in range(1, len(closes)) if closes[i - 1] != 0]
    if len(returns) < window:
        return 0.02
    recent = returns[-window:]
    var = sum(r ** 2 for r in recent) / len(recent)
    return math.sqrt(var)  # per-bar volatility


def forecast_pair(pair, candles, ctx, horizons=HORIZONS):
    """Generate probabilistic forecasts for one pair at multiple horizons."""
    if not candles or len(candles) < 30:
        return None

    price = candles[-1][4]
    if price <= 0:
        return None

    # Base volatility from historical data (per-bar, 1h candles)
    vol_per_bar = compute_volatility(candles)
    vol_per_min = vol_per_bar / math.sqrt(60)  # convert to per-minute

    # Regime-conditioned drift
    regime = ctx.get("regime", "RANGING")
    # No confidence reading means no regime-conditioned drift. Assuming a
    # mid 0.5 injected a directional bias the fleet never measured.
    regime_conf = ctx.get("regime_confidence")
    base_drift = 0.0
    if isinstance(regime_conf, (int, float)):
        if "BULL" in regime:
            base_drift = 0.00001 * regime_conf  # slight upward drift
        elif "BEAR" in regime:
            base_drift = -0.00001 * regime_conf

    # PHITEX phase adjustment: if pre-critical/critical, widen distribution
    phitex_score = ctx.get("phitex_pairs", {}).get(pair, ctx.get("phitex_fleet", 0))
    vol_multiplier = 1.0
    if phitex_score > 0.5:
        vol_multiplier = 1.0 + phitex_score  # up to 2x vol for critical

    # Order book gravity
    book_bias = ctx.get("book_imbalance", {}).get(pair, 0)
    # Positive imbalance (bid-heavy) = upward pressure
    drift_book = book_bias * 0.000005

    # Whale momentum
    whale = ctx.get("whale_pairs", {}).get(pair)
    drift_whale = 0.0
    if whale and whale.get("tier") in ("EXTREME", "HIGH"):
        # High whale score = accumulation = bullish bias.
        # A MISSING score contributes no drift. Defaulting it to 50 injected
        # a half-strength bullish push into the simulated path on the basis
        # of a number nobody measured — indistinguishable in the output from
        # a genuine mid-strength whale reading.
        _ws = whale.get("score")
        if isinstance(_ws, (int, float)):
            drift_whale = 0.000005 * (_ws / 100)

    # Mean reversion anchor: how far is price from recent mean?
    closes = [c[4] for c in candles[-50:]]
    ema50 = sum(closes) / len(closes)
    deviation = (price - ema50) / ema50
    drift_reversion = -deviation * 0.000002  # mean revert gently

    # ── Total drift, expressed in units of THIS pair's volatility ─────────
    # The four terms above are absolute per-minute constants (1e-5 and
    # friends), which silently made the forecast far more confident about
    # low-volatility instruments than about anything that actually moves.
    # Over 4h the same drift is worth 5.9 sigma to a stablecoin but 0.02
    # sigma to a memecoin:
    #     USDG  sigma_4h 0.0004  drift*H 0.0024 -> SNR 5.88 -> P(up) 100.0%
    #     EUR   sigma_4h 0.0030  drift*H 0.0024 -> SNR 0.78 -> P(up)  70.3%
    #     BTC   sigma_4h 0.0240  drift*H 0.0024 -> SNR 0.10 -> P(up)  43.5%
    # That is why Sentinel's only two "high conviction" calls were EUR/USD
    # and a dollar-pegged stablecoin at 100% (2026-07-29 audit). Sizing the
    # dead band by sigma fixed the measurement; this fixes the signal.
    #
    # Treating the raw sum as a per-sigma tilt makes every pair answer the
    # same question: "how large is this edge relative to my own noise?"
    # DRIFT_SNR_CAP bounds the tilt so no single term can imply certainty.
    drift_raw = base_drift + drift_book + drift_whale + drift_reversion
    drift_snr = drift_raw / DRIFT_REF_VOL          # in reference-sigma units
    drift_snr = max(-DRIFT_SNR_CAP, min(DRIFT_SNR_CAP, drift_snr))
    adjusted_vol = vol_per_min * vol_multiplier
    total_drift = drift_snr * adjusted_vol         # now scales with the pair

    # Run Monte Carlo for each horizon
    result = {"current": _sigfig(price)}
    for horizon in horizons:
        finals = monte_carlo_paths(price, adjusted_vol, total_drift, horizon, N_SIMULATIONS)
        # Log-return sigma over this horizon — sizes the flat band so the
        # direction question is asked in units of each pair's own noise.
        sigma_h = adjusted_vol * math.sqrt(horizon)
        dist = compute_distribution(finals, price, sigma_horizon=sigma_h)
        if dist:
            label = f"{horizon // 60}h" if horizon >= 60 else f"{horizon}m"
            result[label] = dist

    return result


# ---------------------------------------------------------------------------
# Sentinel Engine
# ---------------------------------------------------------------------------

class SentinelEngine:
    def __init__(self):
        self.forecasts = {}
        self.high_conviction = []
        self.fleet_context = {}
        self.cycle = 0
        self.scan_duration = 0.0
        self.status = "initializing"
        self._lock = threading.Lock()
        self._log_buf = deque(maxlen=100)
        self._event_pub = EventPublisher(CC_URL, "sentinel") if EventPublisher else None

    def _log(self, msg):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_buf.append(line)
        # The buffer is bounded and never persisted, so an error logged here
        # was visible only via the live API and lost on restart — nothing
        # reached logs/bots/sentinel.log. Mirror errors to stdout, which the
        # launcher captures to the per-bot log file.
        # Broader than ERROR/WARN/FAIL: Sentinel logs "Degraded cycle ...",
        # which the original three-word gate did not match, so a
        # permanently-degraded Sentinel looked silent on disk.
        up = str(msg).upper()
        if any(k in up for k in ("ERROR", "WARN", "FAIL", "CRITICAL",
                                 "DEGRADED", "EXCEPTION", "TIMEOUT",
                                 "UNREACHABLE", "STALE")):
            print(line, flush=True)

    def compute(self):
        self.cycle += 1
        t0 = time.time()
        self._log(f"Cycle {self.cycle}: forecasting...")

        # Get fleet context
        ctx = get_fleet_context()
        self.fleet_context = ctx

        # Get universe
        pairs = get_universe()
        self.status = "forecasting"

        forecasts = {}
        high_conviction = []

        for pair in pairs:
            candles = get_ohlc(pair, interval=60, limit=200)
            if not candles:
                continue

            forecast = forecast_pair(pair, candles, ctx)
            if forecast:
                forecasts[pair] = forecast

                # Check for high conviction at 4h horizon
                h4 = forecast.get("4h", {})
                dp = h4.get("direction_probability", {})
                prob_up = dp.get("up", 0.5)
                prob_down = dp.get("down", 0.5)

                if prob_up > HIGH_CONVICTION_PROB or prob_down > HIGH_CONVICTION_PROB:
                    # Skip blacklisted pairs — still forecast for intel,
                    # but don't emit high-conviction signals that traders act on
                    if _is_blacklisted(pair):
                        continue
                    direction = "UP" if prob_up > prob_down else "DOWN"
                    probability = max(prob_up, prob_down)
                    high_conviction.append({
                        "pair": pair,
                        "direction": direction,
                        "probability": round(probability, 3),
                        "expected_move_pct": h4.get("expected_move_pct", 0),
                        "horizon": "4h",
                    })

            time.sleep(0.1)  # don't hammer CC market data

        # Catch-up pass: the main loop takes ~50s, and Oracle rotates its top-N
        # faster than that, so pairs it started signalling DURING this cycle
        # would otherwise wait a full SCAN_INTERVAL for a forecast — leaving
        # Confluence without a confirming source on exactly the freshest
        # candidates. Re-poll Oracle and forecast anything new before publishing.
        try:
            # Blacklisted pairs must be excluded here too, not just from the
            # main universe. Without this the catch-up retries them every cycle
            # — XNO/USD is blacklisted precisely because Kraken has no OHLC for
            # it, so each attempt is a guaranteed-failed fetch. Observed cost:
            # per-pair time rose 1.04s -> 1.30s on a cycle that added +0 pairs.
            late = [p for p in get_oracle_pairs()
                    if p not in forecasts and not _is_blacklisted(p)]
            added = 0
            for pair in late[:ORACLE_CATCHUP_MAX]:
                candles = get_ohlc(pair, interval=60, limit=200)
                if not candles:
                    continue
                forecast = forecast_pair(pair, candles, ctx)
                if forecast:
                    forecasts[pair] = forecast
                    added += 1
                time.sleep(0.1)
            if late:
                # Report attempted vs added — "+0 late" alone reads like a no-op
                # when it actually means every attempt failed.
                self._log(f"Catch-up: +{added} of {len(late[:ORACLE_CATCHUP_MAX])} "
                          f"attempted late Oracle pair(s)")
        except Exception as e:
            self._log(f"Catch-up pass failed: {e}")

        with self._lock:
            self.forecasts = forecasts

        self.scan_duration = time.time() - t0
        self.status = "running"

        # Persist for the snapshot. This used to live only in a local and in
        # the emitted event, so the dashboard resorted to regex-scraping the
        # human-readable log line (/(\d+) high conviction/) to render HC COUNT
        # — a KPI that would silently read 0 if the log wording ever changed.
        with self._lock:
            self.high_conviction = list(high_conviction)

        # Publish
        if self._event_pub:
            try:
                self._event_pub.emit("FORECAST_UPDATE", {
                    "pairs_forecast": len(forecasts),
                    "high_conviction": len(high_conviction),
                    "regime": ctx.get("regime"),
                    "phitex_fleet": ctx.get("phitex_fleet"),
                })
            except Exception:
                pass

            for hc in high_conviction:
                try:
                    self._event_pub.emit("FORECAST_CONVICTION", hc)
                except Exception:
                    pass

        self._log(f"Cycle {self.cycle}: {len(forecasts)} pairs, "
                  f"{len(high_conviction)} high conviction, {self.scan_duration:.1f}s")

    def snapshot(self):
        with self._lock:
            fc = dict(self.forecasts)
            hc = list(getattr(self, "high_conviction", []))

        # Sort by absolute expected move at 4h
        top = sorted(fc.items(),
                     key=lambda x: abs(x[1].get("4h", {}).get("expected_move_pct", 0)),
                     reverse=True)

        return {
            "timestamp": time.time(),
            "bot_name": "Sentinel",
            "status": self.status,
            "cycle": self.cycle,
            "pairs_forecast": len(fc),
            # Structured fields — read these, don't parse the log lines.
            "high_conviction": len(hc),
            "high_conviction_list": hc,
            "n_simulations": N_SIMULATIONS,
            "dead_band_sigma": DEAD_BAND_SIGMA,
            # Serve every forecast computed, not just the top 20 by expected
            # move. Consumers (Confluence) look up specific pairs by name — a
            # forecast that was computed but withheld here is invisible to
            # them, which silently breaks cross-source confirmation for exactly
            # the mid-cap pairs Oracle tends to signal on. Ordering is
            # preserved so any consumer reading positionally still sees the
            # highest-conviction entries first.
            "forecasts": dict(top),
            "fleet_inputs": self.fleet_context,
            "scan_duration_s": round(self.scan_duration, 2),
            "logs": list(self._log_buf)[-15:],
        }


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

_engine = SentinelEngine()


class SentinelHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/snapshot":
            data = json.dumps(_engine.snapshot(), default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data.encode())
        elif path == "/health":
            data = json.dumps({"status": "ok", "bot": "Sentinel", "port": PORT,
                               "cycle": _engine.cycle, "timestamp": time.time()})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data.encode())
        else:
            self.send_error(404)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _scan_loop():
    # Restore Oracle memory before the first cycle so a cold boot does not
    # build its universe from a single Oracle poll.
    _ORACLE_SEEN.update(_load_oracle_seen())
    if _ORACLE_SEEN:
        print(f"[INFO] Restored {len(_ORACLE_SEEN)} remembered Oracle pair(s)",
              flush=True)
    time.sleep(15)  # wait for fleet to boot
    while True:
        degraded = False
        try:
            _engine.compute()
            # A cycle that produced only the 3-major fallback means the
            # universe was still unavailable. Retry shortly rather than
            # holding a degraded forecast set for a full SCAN_INTERVAL —
            # downstream consumers (Confluence) cannot tell the difference
            # between "3 pairs is all there is" and "CC wasn't ready yet".
            with _engine._lock:
                degraded = len(_engine.forecasts) <= len(_FALLBACK_PAIRS)
        except Exception as e:
            _engine._log(f"ERROR: {e}")
        if degraded:
            _engine._log(f"Degraded cycle ({len(_engine.forecasts)} pairs) — "
                         f"retrying in {DEGRADED_RETRY_S}s")
            time.sleep(DEGRADED_RETRY_S)
        else:
            time.sleep(SCAN_INTERVAL)


def main():
    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        ensure_port(PORT, "sentinel")
        write_pidfile("sentinel", PORT)
        atexit.register(cleanup_pidfile, "sentinel")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    print("\n  SENTINEL v1.0 — Probabilistic Forecast Engine")
    print(f"  Port: {PORT}")
    print(f"  Horizons: {[f'{h//60}h' for h in HORIZONS]}")
    print(f"  Simulations: {N_SIMULATIONS} paths/pair")
    print(f"  http://localhost:{PORT}/api/snapshot")
    print("  Press Ctrl+C to stop\n")

    threading.Thread(target=_scan_loop, daemon=True, name="SentinelScan").start()
    server = ThreadedServer(("0.0.0.0", PORT), SentinelHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Sentinel stopped.")


if __name__ == "__main__":
    main()
