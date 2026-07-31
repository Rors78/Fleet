#!/usr/bin/env python3
"""
AEGIS — Fleet Sensor Fusion & Tradability Engine
==================================================
Bot #11 in the fleet. Port 8079. Color: #e0e0e0 (silver).

Reads the fleet event bus + Command Center APIs. Computes:
  H(t)   — Regime Consensus Entropy (disagreement between bots)
  C(t)   — Signal Temporal Coherence (signal stability)
  W(t)   — Whale-Flow Divergence (whale vs bot alignment)
  S(t)   — Portfolio Stress Velocity (portfolio churn rate)
  Phi(t) — PHITEX Integration (thermodynamic phase state)

Composite: AEGIS = Tradability × Confidence × Safety

Usage: python aegis.py
"""

import json
import math
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

# Fleet integration
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "CommandCenter"))
try:
    from event_publisher import EventPublisher
except ImportError:
    EventPublisher = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 8079
COMMAND_CENTER = "http://127.0.0.1:9000"
SCAN_INTERVAL = 30          # seconds between AEGIS computations
EVENT_WINDOW = 300          # look at last 5 minutes of events
MAX_THROTTLE_PCT = 0.25     # Phase 1: max deployment reduction from whale overlap

# ---------------------------------------------------------------------------
# Regime Dampening — filter out flip-flopping bots
# ---------------------------------------------------------------------------
# Sentinel (291/wk), Trinity (240/wk), PHITEX (195/wk) flip regimes so fast
# they max out consensus entropy and keep the fleet stuck in DEFENSIVE.
# This layer only promotes a regime change to "stable" after it has been held
# for REGIME_HOLD_SECS, filtering flip-flops at the consumer level.

_regime_hold = {}  # bot_id -> {"regime": str, "since": float, "stable": str}
REGIME_HOLD_SECS = 300  # 5 minutes


def dampen_regime(bot_id: str, raw_regime: str) -> str:
    """Only accept regime changes that hold for 5 minutes."""
    now = time.time()
    prev = _regime_hold.get(bot_id)
    if prev is None:
        _regime_hold[bot_id] = {"regime": raw_regime, "since": now, "stable": raw_regime}
        return raw_regime
    if raw_regime != prev["regime"]:
        # Regime changed — start holding timer but return stable (old) regime
        prev["regime"] = raw_regime
        prev["since"] = now
        return prev["stable"]
    # Same regime as before — check if held long enough
    if now - prev["since"] >= REGIME_HOLD_SECS:
        prev["stable"] = raw_regime  # Promote to stable
    return prev["stable"]


# ---------------------------------------------------------------------------
# Component 1: Regime Consensus Entropy
# ---------------------------------------------------------------------------

_REGIME_NORMALIZE = {
    "bull": "BULL", "BULL": "BULL", "TRENDING": "BULL", "TREND_UP": "BULL",
    "bear": "BEAR", "BEAR": "BEAR", "TREND_DOWN": "BEAR",
    "range": "RANGING", "RANGE": "RANGING", "RANGING": "RANGING", "ranging": "RANGING",
    "EQUILIBRIUM": "RANGING", "NEUTRAL": "RANGING", "neutral": "RANGING",
    "MEAN_REVERTING": "RANGING", "mean_reverting": "RANGING",
    "chop": "CHOP", "CHOP": "CHOP",
    "transitioning": "TRANSITIONING", "VOLATILE": "TRANSITIONING",
    "NORMAL": "RANGING", "normal": "RANGING",
    "PRE_CRITICAL": "TRANSITIONING", "CRITICAL": "TRANSITIONING",
    "DEFENSIVE": "RANGING", "defensive": "RANGING",
    "EXTREME_FEAR": "BEAR", "extreme_fear": "BEAR",
    "EXTREME_GREED": "BULL", "extreme_greed": "BULL",
    "MIXED": "RANGING", "mixed": "RANGING",
    "high_activity": "RANGING", "HIGH_ACTIVITY": "RANGING",
    "CAUTIOUS": "RANGING", "cautious": "RANGING",
    "trending": "BULL",
}


def regime_consensus_entropy(regimes: dict) -> float:
    """Normalized Shannon entropy of regime labels. 0=agreement, 1=max disagreement."""
    labels = [_REGIME_NORMALIZE.get(r, r.upper()) for r in regimes.values() if r]
    if not labels:
        return 1.0

    counts = {}
    for l in labels:
        counts[l] = counts.get(l, 0) + 1

    total = len(labels)
    entropy = 0.0
    for count in counts.values():
        p = count / total
        if p > 0:
            entropy -= p * math.log2(p)

    # Normalize by 5 canonical regimes: BULL/BEAR/RANGING/VOLATILE/TRANSITIONING
    max_entropy = math.log2(5)

    return entropy / max_entropy if max_entropy > 0 else 0.0


# ---------------------------------------------------------------------------
# Component 2: Signal Temporal Coherence
# ---------------------------------------------------------------------------

def signal_coherence(signal_events: list, H: float = 0.5) -> float:
    """Measure temporal stability of bot signals. 0=flickering, 1=stable.
    When insufficient data, bootstrap from entropy: clean consensus → higher coherence."""
    if len(signal_events) < 2:
        # Dynamic bootstrap: high entropy → lower coherence, low entropy → higher coherence
        # This breaks the feedback loop where no trades → C=0.5 → low score → no trades
        return 0.6 + 0.2 * (1.0 - H)

    # Group signals by bot
    by_bot = {}
    for e in signal_events:
        src = e.get("source", "")
        data = e.get("data", {})
        if not src or not data.get("pair"):
            continue
        by_bot.setdefault(src, []).append(data)

    coherence_scores = []
    for bot_id, signals in by_bot.items():
        if len(signals) < 2:
            continue
        stable = 0
        total = 0
        for i in range(1, len(signals)):
            if signals[i].get("pair") == signals[i - 1].get("pair"):
                total += 1
                if signals[i].get("direction") == signals[i - 1].get("direction"):
                    stable += 1
        coherence_scores.append(stable / total if total > 0 else 0.5)

    return sum(coherence_scores) / len(coherence_scores) if coherence_scores else (0.6 + 0.2 * (1.0 - H))


# ---------------------------------------------------------------------------
# Component 3: Whale-Flow Divergence
# ---------------------------------------------------------------------------

def whale_flow_divergence(whale_events: list, trade_events: list) -> float:
    """Compare whale direction vs bot direction. +1=aligned, -1=divergent, 0=no data."""
    whale_pairs = {}
    for e in whale_events:
        data = e.get("data", {})
        pair = data.get("pair", "")
        tier = data.get("tier", "")
        if pair and tier in ("EXTREME", "HIGH"):
            whale_pairs[pair] = data.get("score", 50)

    if not whale_pairs:
        return 0.0

    # Bot consensus direction on whale-active pairs
    bot_dir = {}
    for e in trade_events:
        data = e.get("data", {})
        pair = data.get("pair", "")
        if pair in whale_pairs:
            d = 1 if data.get("direction", "").upper() == "LONG" else -1
            bot_dir[pair] = bot_dir.get(pair, 0) + d

    if not bot_dir:
        return 0.0

    alignments = []
    for pair, direction_sum in bot_dir.items():
        bot_sign = 1 if direction_sum > 0 else -1
        whale_conf = whale_pairs[pair] / 100.0
        alignments.append(bot_sign * whale_conf)

    return sum(alignments) / len(alignments)


# ---------------------------------------------------------------------------
# Component 3b: Cross-Pair Correlation from Brainiac
# ---------------------------------------------------------------------------

def get_correlation_regime():
    """Fetch average absolute correlation from Brainiac.
    High correlation = herding, less diversification benefit.
    Returns 0.0 (uncorrelated) to 1.0 (perfectly correlated).
    """
    try:
        resp = requests.get(f"{COMMAND_CENTER}/api/brainiac/correlations", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            if data and data.get("data"):
                return min(1.0, data["data"].get("avg_abs_correlation", 0.5))
    except Exception:
        pass
    return 0.5  # neutral default


# ---------------------------------------------------------------------------
# Component 4: Portfolio Stress Velocity
# ---------------------------------------------------------------------------

def portfolio_stress(curr: dict, prev: dict) -> float:
    """Rate of change of portfolio state. 0=stable, 1=churning."""
    if not curr or not prev:
        return 0.0

    total = max(curr.get("total", 10000), 1)
    prev_total = max(prev.get("total", 10000), 1)

    d_deployed = abs(curr.get("deployed", 0) / total - prev.get("deployed", 0) / prev_total)
    # Direction: long vs short exposure ratio from portfolio state
    curr_dir = curr.get("by_direction", {})
    prev_dir = prev.get("by_direction", {})
    curr_long = curr_dir.get("LONG", 0)
    curr_short = curr_dir.get("SHORT", 0)
    curr_total_exp = curr_long + curr_short
    prev_long = prev_dir.get("LONG", 0)
    prev_short = prev_dir.get("SHORT", 0)
    prev_total_exp = prev_long + prev_short
    curr_ratio = curr_long / curr_total_exp if curr_total_exp > 0 else 0.5
    prev_ratio = prev_long / prev_total_exp if prev_total_exp > 0 else 0.5
    d_direction = abs(curr_ratio - prev_ratio)
    # Position count change
    curr_n = curr.get("active_reservations", 0)
    prev_n = prev.get("active_reservations", 0)
    d_positions = abs(curr_n - prev_n) / 20

    stress = d_deployed * 0.4 + d_direction * 0.35 + d_positions * 0.25
    return min(stress / 0.3, 1.0)


# ---------------------------------------------------------------------------
# Component 5: PHITEX Integration
# ---------------------------------------------------------------------------

def get_phitex_signal(events: list) -> float:
    """Get latest fleet PHITEX score from events."""
    phitex = [e for e in events if e.get("type") == "PHITEX_UPDATE"]
    if not phitex:
        return 0.0
    return phitex[-1].get("data", {}).get("fleet_score", 0.0)


# ---------------------------------------------------------------------------
# AEGIS Composite
# ---------------------------------------------------------------------------

def compute_aegis(H, C, W, S, phi, rho=0.5):
    """AEGIS = Tradability × Confidence × Safety."""
    H = max(0.0, min(1.0, H))  # clamp — no upstream bug can kill the score
    # Soften entropy response: H**0.7 flattens the penalty curve so partial disagreement doesn't crush tradability
    tradability = max(0.25, (1.0 - H**0.7) * C)
    # Agreement boost (capped): when bots agree, lean in — but cap at 1.12 to avoid runaway amplification
    agreement_boost = 1.0 + 0.15 * (1.0 - H)
    tradability *= min(agreement_boost, 1.12)
    # Honest confidence: blind = neutral-positive (0.75), not perfect (1.0).
    # Monotonic in W: W=-1 → 0.25, W=0 → 0.75 (default), W=+1 → 1.0.
    # This removes the "blindness is optimal" inversion where any whale data
    # previously reduced score. Formula: baseline 0.75 + 0.25*W.
    if W == 0:
        confidence = 0.75
    else:
        confidence = max(0.0, min(1.0, 0.75 + 0.25 * W))
    # Safety: high correlation reduces diversification benefit — but crypto is naturally correlated, so weight gently
    corr_penalty = rho * 0.15  # reduced from 0.30 — correlation ≠ danger, just ≠ diversification
    # Cap PHITEX drag: no single component gets more than 0.05 negative influence
    phi_penalty = min(phi * 0.5, 0.05)
    safety = (1.0 - S) * (1.0 - phi_penalty) * (1.0 - corr_penalty)
    return round(max(0.0, min(1.0, tradability * confidence * safety)), 4)


def aegis_regime(score):
    # Bands aligned 2026-07-31 (Jeremy: hysteresis/mismatch not intended).
    # These MUST match the enforcing layer (command_center.py's AEGIS
    # limit mapping, 0.2/0.5/0.8) and the dashboard's published threshold
    # table. The old 0.10/0.35/0.65 bands made this engine report
    # CAUTIOUS/60% at scores the portfolio manager was actually enforcing
    # as DEFENSIVE/30% - three layers, two truths, on-screen contradiction.
    if score >= 0.8:
        return "DEPLOY"
    elif score >= 0.5:
        return "NORMAL"
    elif score >= 0.2:
        return "CAUTIOUS"
    return "DEFENSIVE"


def recommended_max_deployed(score):
    # Same canonical bands as aegis_regime — keep in lockstep.
    if score >= 0.8:
        return 90
    elif score >= 0.5:
        return 80
    elif score >= 0.2:
        return 60
    return 30


# ---------------------------------------------------------------------------
# AEGIS Engine
# ---------------------------------------------------------------------------

class AegisEngine:
    """Fleet Sensor Fusion Engine."""

    def __init__(self):
        self.score = 0.5
        self.regime = "NORMAL"
        self.H = 0.0       # consensus entropy
        self.C = 0.5       # signal coherence
        self.W = 0.0       # whale divergence
        self.S = 0.0       # portfolio stress
        self.phi = 0.0     # phitex signal
        self.rho = 0.5    # cross-pair correlation from Brainiac
        self.cycle = 0
        self.scan_duration = 0.0
        self.status = "initializing"
        self._lock = threading.Lock()
        self._log_buf = deque(maxlen=100)
        self._score_history = deque(maxlen=200)
        self._prev_portfolio = None
        self._regime_sources = {}
        self._normalized_regime_sources = {}
        self._event_pub = EventPublisher(COMMAND_CENTER, "aegis") if EventPublisher else None
        self._last_whale_overlap = {
            "W_position": 0.0,
            "raw_overlap": 0.0,
            "overlapping_pairs": [],
            "whale_pair_count": 0,
            "portfolio_pair_count": 0,
            "activation_threshold_met": False,
            "throttle_pct": 0.0,
        }
        self._adjusted_max_deployed = recommended_max_deployed(self.score)

    def _log(self, msg):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._log_buf.append(f"[{ts}] {msg}")

    def _fetch_json(self, path):
        try:
            resp = requests.get(f"{COMMAND_CENTER}{path}", timeout=5)
            return resp.json()
        except Exception:
            return None

    def compute_exposure_whale_overlap(self, whale_events, portfolio_exposure, portfolio_total):
        """Phase 1: W_position_magnitude — exposure-weighted whale attention overlap.
        Returns (w_position, overlap_data_dict).
        w_position is scalar 0.0-1.0, magnitude only (no direction until Phase 2).
        """
        TIER_WEIGHT = {"EXTREME": 1.0, "HIGH": 0.4}
        WHALE_LOOKBACK = 1800.0  # 30 min
        ACTIVATION_THRESHOLD = 0.15

        now = time.time()

        # Step 1: whale pressure map — max() per pair, not sum
        whale_pairs = {}
        for e in whale_events:
            if e.get("type") != "WHALE_ALERT":
                continue
            data = e.get("data", {}) or {}
            tier = str(data.get("tier", "")).upper()
            if tier not in TIER_WEIGHT:
                continue
            pair = data.get("pair", "")
            if not pair:
                continue
            ts = e.get("ts", 0) or 0
            age = now - ts
            if age > WHALE_LOOKBACK or age < 0:
                continue
            recency = max(0.0, 1.0 - (age / WHALE_LOOKBACK))
            weight = TIER_WEIGHT[tier] * recency
            whale_pairs[pair] = max(whale_pairs.get(pair, 0.0), weight)

        # Step 2: portfolio exposure map (fraction of total)
        by_pair = portfolio_exposure.get("by_pair", {}) if portfolio_exposure else {}
        total = max(portfolio_total, 1.0)  # avoid div by zero
        portfolio_pairs = {}
        for pair, info in by_pair.items():
            amount = info.get("amount", 0) if isinstance(info, dict) else float(info)
            if amount > 0:
                portfolio_pairs[pair] = amount / total

        # Step 3: overlap
        overlapping = []
        raw_overlap = 0.0
        for pair in set(whale_pairs.keys()) & set(portfolio_pairs.keys()):
            contribution = whale_pairs[pair] * portfolio_pairs[pair]
            raw_overlap += contribution
            overlapping.append({
                "pair": pair,
                "whale_weight": round(whale_pairs[pair], 4),
                "exposure_frac": round(portfolio_pairs[pair], 4),
                "contribution": round(contribution, 4),
            })
        raw_overlap = min(1.0, raw_overlap)

        # Step 4: activation threshold rescale
        if raw_overlap < ACTIVATION_THRESHOLD:
            w_position = 0.0
        else:
            w_position = (raw_overlap - ACTIVATION_THRESHOLD) / (1.0 - ACTIVATION_THRESHOLD)

        overlap_data = {
            "W_position": round(w_position, 4),
            "raw_overlap": round(raw_overlap, 4),
            "overlapping_pairs": overlapping,
            "whale_pair_count": len(whale_pairs),
            "portfolio_pair_count": len(portfolio_pairs),
            "activation_threshold_met": w_position > 0.0,
        }
        return w_position, overlap_data

    def compute(self):
        """Run one AEGIS computation cycle."""
        self.cycle += 1
        t0 = time.time()
        self._log(f"Cycle {self.cycle}")

        # Fetch fleet state
        master = self._fetch_json("/api/master")
        events_raw = self._fetch_json("/api/events/recent?n=200")
        portfolio = self._fetch_json("/api/portfolio")

        if not master and not events_raw:
            self._log("Command Center not available — using last known state")
            self.status = "stale"
            self.scan_duration = time.time() - t0
            # Still publish with last known values so dashboard has something
            if self._event_pub:
                try:
                    self._event_pub.emit("AEGIS_UPDATE", {
                        "score": self.score, "regime": self.regime,
                        "components": {"consensus_entropy": self.H, "signal_coherence": self.C,
                                       "whale_divergence": self.W, "portfolio_stress": self.S,
                                       "phitex_signal": self.phi, "correlation": self.rho},
                        "recommended_max_deployed": recommended_max_deployed(self.score),
                        "stale": True,
                    })
                except Exception:
                    pass
            return

        self.status = "computing"
        if not master:
            master = {"bots": []}
        if not events_raw:
            events_raw = []
        events = events_raw if isinstance(events_raw, list) else []

        # Filter recent events (last EVENT_WINDOW seconds)
        cutoff = time.time() - EVENT_WINDOW
        recent = [e for e in events if e.get("ts", 0) > cutoff]

        # 1. Regime Consensus Entropy
        # Only accept sources reporting actual market regimes (not moods/states).
        # EXTREME_FEAR, STRESSED, MIXED, session_overlap, TRANSITIONING_BULL,
        # EQUILIBRIUM etc. are not regimes — they inject noise into consensus.
        _VALID_REGIME_INPUTS = {
            "bull", "bear", "range", "chop",
            "trending", "ranging", "defensive", "cautious",
            "BULL", "BEAR", "RANGE", "CHOP",
            "TRENDING", "RANGING", "DEFENSIVE", "CAUTIOUS",
            "TREND_UP", "TREND_DOWN", "VOLATILE", "MEAN_REVERTING",
            "mean_reverting", "NEUTRAL", "neutral", "NORMAL", "normal",
        }
        _exclude_from_consensus = {"aegis", "phitex"}
        regimes = {}
        for bot in master.get("bots", []):
            if bot.get("alive") and bot["id"] not in _exclude_from_consensus:
                n = bot.get("normalized", {}) or {}
                regime = n.get("regime")
                if regime and regime in _VALID_REGIME_INPUTS:
                    regimes[bot["id"]] = dampen_regime(bot["id"], regime)
        # Also check recent regime change events
        for e in recent:
            src = e.get("source", "")
            if e.get("type") == "REGIME_CHANGE" and src not in _exclude_from_consensus:
                raw = e.get("data", {}).get("to", "")
                if raw and raw in _VALID_REGIME_INPUTS:
                    regimes[src] = dampen_regime(src, raw)

        self.H = regime_consensus_entropy(regimes)
        self._regime_sources = regimes
        # Parallel dict showing normalized labels (what entropy actually sees)
        self._normalized_regime_sources = {
            bot_id: _REGIME_NORMALIZE.get(r, r.upper())
            for bot_id, r in regimes.items()
        }

        # 2. Signal Temporal Coherence
        signal_events = [e for e in events if e.get("type") in ("TRADE_OPEN", "SIGNAL")]
        self.C = signal_coherence(signal_events, self.H)

        # 3. Whale-Flow Divergence
        whale_events = [e for e in recent if e.get("type") == "WHALE_ALERT"]
        trade_events = [e for e in recent if e.get("type") in ("TRADE_OPEN", "SIGNAL")]
        self.W = whale_flow_divergence(whale_events, trade_events)

        # 4. Portfolio Stress Velocity
        if portfolio:
            self.S = portfolio_stress(portfolio, self._prev_portfolio) if self._prev_portfolio else 0.0
            self._prev_portfolio = portfolio

        # 5. PHITEX Integration
        self.phi = get_phitex_signal(events)

        # 6. Cross-pair correlation from Brainiac
        self.rho = get_correlation_regime()

        # Composite — correlation reduces safety (herding = less diversification)
        self.score = compute_aegis(self.H, self.C, self.W, self.S, self.phi, self.rho)
        self.regime = aegis_regime(self.score)
        self._score_history.append(self.score)

        # 7. Phase 1: W_position_magnitude — deployment throttle (snapshot-only, no bus events)
        exposure = self._fetch_json("/api/portfolio/exposure") or {}
        port_full = self._fetch_json("/api/portfolio") or {}
        w_pos, overlap_data = self.compute_exposure_whale_overlap(
            events, exposure, port_full.get("total", 10000)
        )
        throttle_pct = w_pos * MAX_THROTTLE_PCT
        overlap_data["throttle_pct"] = round(throttle_pct, 4)
        self._last_whale_overlap = overlap_data
        base_max = recommended_max_deployed(self.score)
        self._adjusted_max_deployed = int(base_max * (1.0 - throttle_pct))

        self.scan_duration = time.time() - t0
        self.status = "running"

        # Publish
        if self._event_pub:
            try:
                self._event_pub.emit("AEGIS_UPDATE", {
                    "score": self.score,
                    "regime": self.regime,
                    "components": {
                        "consensus_entropy": round(self.H, 4),
                        "signal_coherence": round(self.C, 4),
                        "whale_divergence": round(self.W, 4),
                        "portfolio_stress": round(self.S, 4),
                        "phitex_signal": round(self.phi, 4),
                        "correlation": round(self.rho, 4),
                    },
                    "recommended_max_deployed": recommended_max_deployed(self.score),
                    "regime_sources": self._regime_sources,
                })
            except Exception:
                pass

        self._log(f"AEGIS={self.score:.4f} [{self.regime}] H={self.H:.3f} C={self.C:.3f} "
                  f"W={self.W:+.3f} S={self.S:.3f} Φ={self.phi:.3f} ({self.scan_duration:.1f}s)")

    def snapshot(self):
        """Build JSON snapshot for API."""
        return {
            "timestamp": time.time(),
            "bot_name": "AEGIS",
            "status": self.status,
            "cycle": self.cycle,
            "score": self.score,
            "regime": self.regime,
            "recommended_max_deployed": self._adjusted_max_deployed,
            "components": {
                "consensus_entropy": round(self.H, 4),
                "signal_coherence": round(self.C, 4),
                "whale_divergence": round(self.W, 4),
                "portfolio_stress": round(self.S, 4),
                "phitex_signal": round(self.phi, 4),
                "correlation": round(self.rho, 4),
            },
            "regime_sources": self._regime_sources,
            "normalized_regime_sources": self._normalized_regime_sources,
            "score_history": list(self._score_history)[-50:],
            "scan_duration_s": round(self.scan_duration, 2),
            "logs": list(self._log_buf)[-20:],
            "whale_overlap": self._last_whale_overlap,
        }


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

_engine = AegisEngine()


class AegisHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj):
        data = json.dumps(obj, default=str)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data.encode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/snapshot":
            self._send_json(_engine.snapshot())

        elif path == "/health":
            self._send_json({
                "status": "ok", "bot": "AEGIS", "port": PORT,
                "cycle": _engine.cycle, "score": _engine.score,
                "regime": _engine.regime, "timestamp": time.time(),
            })

        elif path == "/" or path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    self.wfile.write(f.read().encode("utf-8"))
            except FileNotFoundError:
                self.wfile.write(b"<html><body><h1>AEGIS</h1><p>dashboard.html not found</p></body></html>")

        else:
            self.send_error(404)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _scan_loop():
    time.sleep(15)  # let fleet boot
    while True:
        try:
            _engine.compute()
        except Exception as e:
            _engine._log(f"ERROR: {e}")
        time.sleep(SCAN_INTERVAL)


def main():
    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        ensure_port(PORT, "aegis")
        write_pidfile("aegis", PORT)
        atexit.register(cleanup_pidfile, "aegis")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    print()
    print("  +==========================================+")
    print("  |         AEGIS v1.0                       |")
    print("  |  Fleet Sensor Fusion Engine              |")
    print("  +==========================================+")
    print(f"  |  Dashboard:  http://localhost:{PORT}       |")
    print(f"  |  API:        http://localhost:{PORT}/api/snapshot")
    print(f"  |  Scan:       every {SCAN_INTERVAL}s               |")
    print("  +==========================================+")
    print()

    t = threading.Thread(target=_scan_loop, daemon=True, name="AegisScan")
    t.start()

    # ThreadingHTTPServer, not HTTPServer: the plain server handles one request
    # at a time, so while AEGIS is recomputing its score the port stops
    # answering. Command Center's health check times out and reports the bot
    # DOWN even though it is healthy — observed as 18/18 flickering to 15/18.
    # daemon_threads so request threads never block shutdown.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AegisHandler)
    server.daemon_threads = True
    print(f"  Listening on http://localhost:{PORT}")
    print("  Press Ctrl+C to stop")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  AEGIS stopped.")


if __name__ == "__main__":
    main()
