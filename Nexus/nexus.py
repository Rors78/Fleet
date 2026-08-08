#!/usr/bin/env python3
"""
NEXUS — Market Interferometer
==============================
Bot #12. Port 8082. Color: #00bfa5 (teal).

Second-order intelligence: observes the FLEET observing the market.
Measures information propagation velocity, lens dispersion, and
temporal echoes to detect things no single bot can see.

Ten engines:
  1. Propagation Tracker     — order bots detect events reveals move type
  2. Lens Dispersion         — cross-bot disagreement predicts volatility
  3. Temporal Echo            — slow bots leading fast bots = structural shift
  4. Pythagorean Resonance   — multi-TF harmonic analysis
  5. Gaussian Belief Fusion  — optimal regime estimate fusion
  6. Riemann Spectral        — hidden periodicities in event timing
  7. Newtonian Force         — F=ma applied to capital flow, inertia, reactions
  8. Euclidean Structure     — geometric S/R surfaces in price-volume-time
  9. Einsteinian Relativity  — mass-energy equivalence, time dilation, reference frames
  10. Schwarzschild Horizons — event horizon detection, escape velocity, market topology

Usage: python nexus.py
"""

import json
import math
import sys
import threading
import time
from collections import Counter, deque
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
    from info_geometry import InformationGeometryEngine
    from topology import TopologicalAnalyzer
    from quantum_state import QuantumMarketState
    from causal_flow import CausalFlowNetwork
    from shannon import ShannonEngine
    from boltzmann import BoltzmannEngine
    from lorenz import LorenzEngine
    from prigogine import PrigogineEngine
    from thom import ThomEngine
    _info_geo = InformationGeometryEngine()
    _topology = TopologicalAnalyzer()
    _quantum = QuantumMarketState()
    _causal = CausalFlowNetwork()
    _shannon = ShannonEngine()
    _boltzmann = BoltzmannEngine()
    _lorenz = LorenzEngine()
    _prigogine = PrigogineEngine()
    _thom = ThomEngine()
    _council_loaded = True
except Exception:
    _council_loaded = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 8082
COMMAND_CENTER = "http://127.0.0.1:9000"
PHITEX_URL = "http://127.0.0.1:8078"  # PHITEX thermodynamic engine
SCAN_INTERVAL = 15  # seconds — faster than other bots, it's lightweight


# ---------------------------------------------------------------------------
# Engine 1: Propagation Tracker
# ---------------------------------------------------------------------------

class PropagationTracker:
    """Track the order bots detect market events."""

    def __init__(self):
        self.event_chains = {}  # {pair: [{bot, timestamp, type}]}
        self.fingerprints = deque(maxlen=100)

    def on_event(self, event):
        data = event.get("data", {})
        pair = data.get("pair")
        if not pair:
            return

        chain = self.event_chains.setdefault(pair, [])
        chain.append({
            "bot": event.get("source", ""),
            "timestamp": event.get("ts", time.time()),
            "type": event.get("type", ""),
        })

        # Trim to last 30 minutes
        cutoff = time.time() - 1800
        self.event_chains[pair] = [e for e in chain if e["timestamp"] > cutoff]

    def get_fingerprint(self, pair):
        chain = self.event_chains.get(pair, [])
        if len(chain) < 2:
            return None

        ordered = sorted(chain, key=lambda x: x["timestamp"])
        first = ordered[0]
        last = ordered[-1]
        spread = last["timestamp"] - first["timestamp"]

        fp = {
            "pair": pair,
            "first_detector": first["bot"],
            "last_detector": last["bot"],
            "propagation_time_s": round(spread, 1),
            "detection_order": [e["bot"] for e in ordered],
            "event_count": len(ordered),
            "move_type": self._classify(ordered, spread),
        }
        return fp

    def _classify(self, ordered, spread):
        first = ordered[0]["bot"]

        # Everything within 30s = systemic
        if spread < 30 and len(ordered) >= 3:
            return "SYSTEMIC"
        # Whale first
        if first == "deepblue":
            return "INSTITUTIONAL"
        # Fast indicators first, no whale in first 3
        if first in ("trinity", "trekbot", "gridzilla"):
            whale_early = any(e["bot"] == "deepblue" for e in ordered[:3])
            if not whale_early:
                return "RETAIL_NOISE"
        # Slow models first
        if first in ("hivemind", "oracle", "turtlesue"):
            return "STRUCTURAL_SHIFT"
        return "MIXED"

    def get_active_fingerprints(self):
        fps = []
        for pair in list(self.event_chains.keys()):
            fp = self.get_fingerprint(pair)
            if fp and fp["event_count"] >= 2:
                fps.append(fp)
        return fps


# ---------------------------------------------------------------------------
# Engine 2: Lens Dispersion
# ---------------------------------------------------------------------------

class LensDispersion:
    """Measure cross-bot disagreement — predicts volatility."""

    def __init__(self, window=30):
        self.history = deque(maxlen=window)

    def compute(self, bot_states):
        scores = {}

        for bot_id, state in bot_states.items():
            norm = state.get("normalized", {}) or {}
            raw = state.get("raw", {}) or {}

            if bot_id == "trinity":
                # Normalize signals count by expected max
                sc = norm.get("signals_count", 0)
                scores["trinity"] = min(sc / 20, 1.0) if sc else 0.5
            elif bot_id == "trekbot":
                wr = norm.get("win_rate")
                scores["trekbot"] = wr / 100 if wr else 0.5
            elif bot_id == "nexusbrain":
                wr = norm.get("win_rate")
                scores["nexusbrain"] = wr / 100 if wr and wr <= 100 else 0.5
            elif bot_id == "gridzilla":
                wr = norm.get("win_rate")
                scores["gridzilla"] = wr / 100 if wr else 0.5
            elif bot_id == "hivemind":
                sh = norm.get("sharpe")
                scores["hivemind"] = min(max(sh / 5, 0), 1.0) if sh else 0.5
            elif bot_id == "phitex":
                fs = norm.get("fleet_score") or raw.get("fleet_score", 0)
                scores["phitex"] = min(fs, 1.0) if fs else 0
            elif bot_id == "deepblue":
                wc = norm.get("whale_count") or raw.get("whale_count", 0)
                scores["deepblue"] = min(wc / 10, 1.0) if wc else 0

        if len(scores) < 3:
            return {"dispersion": 0, "trend": "STABLE", "prediction": "NEUTRAL", "scores": scores}

        values = list(scores.values())
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)

        self.history.append(variance)

        # Trend
        if len(self.history) >= 5:
            recent = list(self.history)[-5:]
            older = list(self.history)[-10:-5] if len(self.history) >= 10 else recent
            delta = sum(recent) / len(recent) - sum(older) / len(older)
            trend = "RISING" if delta > 0.005 else "FALLING" if delta < -0.005 else "STABLE"
        else:
            trend = "STABLE"

        # Prediction
        if variance > 0.10 and trend == "RISING":
            prediction = "VOL_EXPANSION"
        elif variance < 0.03 and trend == "FALLING":
            prediction = "VOL_COMPRESSION"
        else:
            prediction = "NEUTRAL"

        return {
            "dispersion": round(variance, 6),
            "mean_conviction": round(mean, 4),
            "trend": trend,
            "prediction": prediction,
            "scores": {k: round(v, 4) for k, v in scores.items()},
        }


# ---------------------------------------------------------------------------
# Engine 3: Temporal Echo Detector
# ---------------------------------------------------------------------------

class TemporalEchoDetector:
    """Detect reverse cascades — slow bots signaling before fast bots."""

    SLOW_BOTS = {"hivemind", "oracle", "turtlesue"}
    FAST_BOTS = {"trekbot", "trinity", "gridzilla", "nexusbrain"}
    SIGNAL_TYPES = {"REGIME_CHANGE", "SIGNAL", "TRADE_OPEN", "TRADE_CLOSE",
                    "SCAN_COMPLETE", "WHALE_ALERT", "PHITEX_UPDATE"}

    def __init__(self):
        self.slow_events = deque(maxlen=200)
        self.fast_events = deque(maxlen=200)
        self.detected_echoes = deque(maxlen=50)

    def on_event(self, event):
        source = event.get("source", "")
        if event.get("type") not in self.SIGNAL_TYPES:
            return

        entry = {
            "source": source,
            "type": event.get("type"),
            "timestamp": event.get("ts", time.time()),
            "data": event.get("data", {}),
        }

        if source in self.SLOW_BOTS:
            self.slow_events.append(entry)
        elif source in self.FAST_BOTS:
            self.fast_events.append(entry)
            echo = self._check(entry)
            if echo:
                self.detected_echoes.append(echo)

    def _check(self, fast_event):
        now = fast_event["timestamp"]
        fast_pair = fast_event["data"].get("pair")

        for slow in reversed(list(self.slow_events)):
            lag = now - slow["timestamp"]
            if lag > 7200:
                break
            if lag < 60:
                continue

            slow_pair = slow["data"].get("pair")
            if fast_pair and slow_pair and fast_pair != slow_pair:
                continue

            # Check direction alignment if available
            slow_dir = slow["data"].get("direction", slow["data"].get("to", ""))
            fast_dir = fast_event["data"].get("direction", "")
            if slow_dir and fast_dir and slow_dir.upper() != fast_dir.upper():
                continue

            sig = "HIGH" if lag > 600 else "MEDIUM" if lag > 120 else "LOW"

            return {
                "timestamp": now,
                "slow_source": slow["source"],
                "fast_source": fast_event["source"],
                "slow_type": slow["type"],
                "fast_type": fast_event["type"],
                "pair": fast_pair or slow_pair,
                "lag_seconds": round(lag),
                "lag_human": f"{lag / 60:.0f}m",
                "significance": sig,
            }
        return None

    def recent_echoes(self, max_age=3600):
        now = time.time()
        return [e for e in self.detected_echoes if now - e["timestamp"] < max_age]


# ---------------------------------------------------------------------------
# NEXUS Engine
# ---------------------------------------------------------------------------

def _interpret(character, vol_pred, echo):
    parts = []
    if character == "INSTITUTIONAL":
        parts.append("Whale-driven move — likely to continue")
    elif character == "RETAIL_NOISE":
        parts.append("Retail noise — likely to revert")
    elif character == "STRUCTURAL_SHIFT":
        parts.append("Structural shift — slow bots leading")
    elif character == "SYSTEMIC":
        parts.append("Systemic event — all sensors triggered")

    if vol_pred == "VOL_EXPANSION":
        parts.append("Vol expansion imminent")
    elif vol_pred == "VOL_COMPRESSION":
        parts.append("Vol compressing")

    if echo:
        parts.append(f"Echo: {echo.get('message', '')}")

    return " | ".join(parts) if parts else "Quiet — fleet in agreement"


# ---------------------------------------------------------------------------
# Engine 7: Newtonian Force Dynamics
# ---------------------------------------------------------------------------

class NewtonianForceEngine:
    """
    F = ma applied to capital flow.

    First Law (Inertia): A trend in motion stays in motion unless acted
    upon by an external force. Measure the "mass" of a trend by its
    duration x volume. Heavy trends resist reversal.

    Second Law (Force): F = volume_acceleration x price_acceleration.
    When both volume and price are accelerating in the same direction,
    the force is compounding. When they diverge, the force is dissipating.

    Third Law (Reaction): For every large move in a correlated pair,
    there's a delayed reaction in its partners. Use Brainiac's correlation
    matrix to predict which pairs will react and estimate the lag.
    """

    def compute(self, pair, candles, correlation_matrix=None):
        """
        candles: list of [timestamp, open, high, low, close, volume, count]
        correlation_matrix: from Brainiac, {pair_key: correlation}

        Returns:
            momentum, acceleration, force, mass, inertia_score, reaction_pairs
        """
        if len(candles) < 30:
            return self._default()

        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]

        # === FIRST LAW: INERTIA ===
        returns_10 = [(closes[i] - closes[i - 1]) / closes[i - 1]
                      for i in range(len(closes) - 10, len(closes))]
        returns_20 = [(closes[i] - closes[i - 1]) / closes[i - 1]
                      for i in range(len(closes) - 20, len(closes))]

        # Trend direction: +1 (up), -1 (down), 0 (flat)
        trend_20 = sum(1 if r > 0 else -1 for r in returns_20) / 20

        # Mass = how "heavy" the current trend is
        # Duration: consecutive bars in same direction
        duration = 0
        current_dir = 1 if returns_10[-1] > 0 else -1
        for i in range(len(closes) - 2, max(0, len(closes) - 50), -1):
            bar_dir = 1 if closes[i] > closes[i - 1] else -1
            if bar_dir == current_dir:
                duration += 1
            else:
                break

        # Mass = duration x average volume during the trend
        trend_volumes = volumes[-duration:] if duration > 0 else volumes[-5:]
        avg_trend_vol = sum(trend_volumes) / len(trend_volumes) if trend_volumes else 1
        avg_total_vol = sum(volumes[-30:]) / 30
        mass = (duration / 30) * (avg_trend_vol / (avg_total_vol + 1e-10))
        mass = min(mass, 3.0)  # cap at 3.0

        # Inertia score: heavier trends are harder to reverse
        direction_consistency = abs(trend_20)
        inertia = min(1.0, mass * direction_consistency)

        # === SECOND LAW: F = ma ===
        if len(closes) >= 12:
            momentum_5 = (closes[-1] - closes[-6]) / (closes[-6] + 1e-10)
            momentum_10 = (closes[-1] - closes[-11]) / (closes[-11] + 1e-10)
        else:
            momentum_5 = momentum_10 = 0

        # Acceleration: change in momentum
        acceleration = momentum_5 - momentum_10 / 2

        # Volume acceleration
        vol_recent = sum(volumes[-5:]) / 5
        vol_prior = sum(volumes[-10:-5]) / 5 if len(volumes) >= 10 else vol_recent
        vol_acceleration = (vol_recent - vol_prior) / (vol_prior + 1e-10)

        # Force = price_acceleration x volume_acceleration
        force = acceleration * vol_acceleration * 1000
        force_direction = "BULL" if force > 0.1 else "BEAR" if force < -0.1 else "NEUTRAL"

        # === THIRD LAW: REACTION PAIRS ===
        reaction_pairs = []
        if correlation_matrix:
            for pair_key, corr in correlation_matrix.items():
                if pair in pair_key and abs(corr) > 0.6:
                    parts = pair_key.split("|")
                    other = parts[1] if parts[0] == pair else parts[0]

                    # Expected lag: higher correlation = faster reaction
                    lag = max(1, int((1 - abs(corr)) * 10))
                    expected_dir = "SAME" if corr > 0 else "OPPOSITE"

                    reaction_pairs.append({
                        "pair": other,
                        "correlation": round(corr, 3),
                        "expected_lag_bars": lag,
                        "expected_direction": expected_dir,
                        "force_transfer": round(abs(force * corr), 4),
                    })

            reaction_pairs.sort(key=lambda x: x["force_transfer"], reverse=True)
            reaction_pairs = reaction_pairs[:5]

        return {
            "momentum": round(momentum_5 * 100, 4),
            "acceleration": round(acceleration * 10000, 4),
            "force": round(force, 4),
            "force_direction": force_direction,
            "mass": round(mass, 3),
            "trend_duration_bars": duration,
            "inertia_score": round(inertia, 3),
            "volume_acceleration": round(vol_acceleration, 3),
            "direction_consistency": round(direction_consistency, 3),
            "reaction_pairs": reaction_pairs,
        }

    def _default(self):
        return {
            "momentum": 0, "acceleration": 0, "force": 0,
            "force_direction": "NEUTRAL", "mass": 0,
            "trend_duration_bars": 0, "inertia_score": 0,
            "volume_acceleration": 0, "direction_consistency": 0,
            "reaction_pairs": [],
        }


# ---------------------------------------------------------------------------
# Engine 8: Euclidean Geometric Structure
# ---------------------------------------------------------------------------

class EuclideanStructureEngine:
    """
    Support/resistance as geometric surfaces in price-time-volume space.

    Traditional S/R: horizontal lines at round numbers or previous highs/lows.
    Euclidean S/R: SURFACES with measurable strength based on three dimensions:
      1. Volume weight -- how much was traded at this level
      2. Time weight -- how long price spent at this level
      3. Touch count -- how many times price returned to this level

    A level with high volume, high time, and high touch count is a
    "thick" surface that price will struggle to break through.
    """

    def compute(self, pair, candles, lookback=100):
        """
        Returns support/resistance levels, distances, bias, breakout probability.
        """
        if len(candles) < lookback:
            lookback = len(candles)
        if lookback < 20:
            return self._default()

        recent = candles[-lookback:]
        closes = [float(c[4]) for c in recent]
        highs = [float(c[2]) for c in recent]
        lows = [float(c[3]) for c in recent]
        volumes = [float(c[5]) for c in recent]
        current_price = closes[-1]

        # Step 1: Build price histogram weighted by volume
        price_min = min(lows)
        price_max = max(highs)
        price_range = price_max - price_min
        if price_range == 0:
            return self._default()

        n_bins = min(50, max(10, int(lookback / 2)))
        bin_width = price_range / n_bins

        volume_profile = [0.0] * n_bins
        time_profile = [0] * n_bins
        touch_profile = [0] * n_bins

        for i, candle in enumerate(recent):
            bar_high = float(candle[2])
            bar_low = float(candle[3])
            bar_vol = float(candle[5])

            # Distribute volume across all bins this bar spans
            for b in range(n_bins):
                bin_low = price_min + b * bin_width
                bin_high = bin_low + bin_width

                overlap_low = max(bar_low, bin_low)
                overlap_high = min(bar_high, bin_high)

                if overlap_high > overlap_low:
                    bar_range = bar_high - bar_low if bar_high > bar_low else 1e-10
                    overlap_fraction = (overlap_high - overlap_low) / bar_range
                    volume_profile[b] += bar_vol * overlap_fraction
                    time_profile[b] += 1

            # Touch detection: price bouncing off a level
            bar_close = float(candle[4])
            close_bin = min(n_bins - 1, max(0, int((bar_close - price_min) / bin_width)))

            if i < len(recent) - 1:
                next_close = float(recent[i + 1][4])
                if bar_close < next_close and bar_low <= price_min + close_bin * bin_width + bin_width:
                    touch_profile[close_bin] += 1
                elif bar_close > next_close and bar_high >= price_min + close_bin * bin_width:
                    touch_profile[close_bin] += 1

        # Step 2: Normalize profiles
        max_vol = max(volume_profile) if max(volume_profile) > 0 else 1
        max_time = max(time_profile) if max(time_profile) > 0 else 1
        max_touch = max(touch_profile) if max(touch_profile) > 0 else 1

        # Step 3: Compute strength for each bin
        levels = []
        for b in range(n_bins):
            bin_center = price_min + (b + 0.5) * bin_width

            vol_weight = volume_profile[b] / max_vol
            time_weight = time_profile[b] / max_time
            touch_weight = touch_profile[b] / max_touch

            # Euclidean strength = geometric distance in 3D space
            strength = (vol_weight ** 2 + time_weight ** 2 + touch_weight ** 2) ** 0.5
            strength = strength / 1.732  # normalize: max sqrt(3)

            if strength > 0.15:
                levels.append({
                    "price": round(bin_center, 6),
                    "strength": round(strength, 3),
                    "volume_weight": round(vol_weight, 3),
                    "time_weight": round(time_weight, 3),
                    "touches": touch_profile[b],
                })

        # Step 4: Classify as support or resistance
        support_levels = sorted(
            [l for l in levels if l["price"] < current_price],
            key=lambda x: x["strength"], reverse=True,
        )[:5]

        resistance_levels = sorted(
            [l for l in levels if l["price"] > current_price],
            key=lambda x: x["strength"], reverse=True,
        )[:5]

        # Step 5: Find nearest levels
        nearest_support = max(support_levels, key=lambda x: x["price"]) if support_levels else None
        nearest_resistance = min(resistance_levels, key=lambda x: x["price"]) if resistance_levels else None

        # Step 6: Distance and bias
        support_dist = ((current_price - nearest_support["price"]) / current_price * 100) if nearest_support else 99
        resistance_dist = ((nearest_resistance["price"] - current_price) / current_price * 100) if nearest_resistance else 99

        if nearest_support and nearest_resistance:
            support_pull = nearest_support["strength"] / (support_dist + 0.01)
            resistance_pull = nearest_resistance["strength"] / (resistance_dist + 0.01)

            if support_pull > resistance_pull * 1.5:
                bias = "SUPPORT_CLOSE"
            elif resistance_pull > support_pull * 1.5:
                bias = "RESISTANCE_CLOSE"
            else:
                bias = "NEUTRAL"
        else:
            bias = "NEUTRAL"

        # Step 7: Breakout probability
        if nearest_resistance:
            breakout_prob = max(0, min(1, 1 - nearest_resistance["strength"]))
        else:
            breakout_prob = 0.5

        return {
            "support_levels": support_levels,
            "resistance_levels": resistance_levels,
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
            "support_distance_pct": round(support_dist, 3),
            "resistance_distance_pct": round(resistance_dist, 3),
            "geometric_bias": bias,
            "breakout_probability": round(breakout_prob, 3),
            "total_levels_found": len(levels),
        }

    def _default(self):
        return {
            "support_levels": [], "resistance_levels": [],
            "nearest_support": None, "nearest_resistance": None,
            "support_distance_pct": 99, "resistance_distance_pct": 99,
            "geometric_bias": "NEUTRAL", "breakout_probability": 0.5,
            "total_levels_found": 0,
        }


def _is_severe(msg):
    """Should this log line reach stdout (and so the on-disk log)?

    Deliberately broader than ERROR/WARN/FAIL. The first version of this gate
    matched only those three, which silently excluded the MOST severe
    messages in the fleet: PhiTex emits "CRITICAL:" and "PRE_CRITICAL:", and
    Sentinel emits "Degraded cycle ...". A gate that catches warnings but
    drops criticals is worse than no gate, because the quiet log then reads
    as calm at exactly the moment it should not.
    """
    up = str(msg).upper()
    return any(k in up for k in (
        "ERROR", "WARN", "FAIL", "CRITICAL", "DEGRADED",
        "EXCEPTION", "TIMEOUT", "UNREACHABLE", "STALE",
    ))


def _mean_or_none(vals):
    """Mean of measured values, or None when nothing was measured.

    Deliberately not `sum(...)/max(len(...), 1)`: that yields 0.0 for an
    empty set, which downstream reads as "measured, and the answer is zero"
    rather than "never measured".
    """
    return round(sum(vals) / len(vals), 3) if vals else None


# ---------------------------------------------------------------------------
# Engine 4: Pythagorean Harmonic Resonance
# ---------------------------------------------------------------------------

class PythagoreanResonance:
    """Multi-timeframe harmonic analysis. Treats TF scores as a vibrating system."""

    TF_ORDER = ["5m", "15m", "1h", "4h", "1d"]

    def compute(self, pair, tf_scores):
        """Compute harmonic resonance from multi-TF trend scores.
        tf_scores = {"5m": 0.72, "15m": 0.68, "1h": 0.71, "4h": 0.35, "1d": 0.65}
        Each score: >0.5=bullish, <0.5=bearish, 0.5=neutral.
        """
        # Only timeframes PHITEX actually measured take part. The old
        # `.get(tf, 0.5)` was doubly wrong: 0.5 is not neutral under the
        # `s > 0.5` test below, it is BEARISH, so an absent timeframe cast a
        # silent bear vote AND was counted as an agreeing voice in
        # `consonance`. With no measurements at all that yielded
        # BEAR @ consonance 1.0 — the strongest possible reading, from
        # five timeframes that were never measured.
        present = [(tf, tf_scores.get(tf)) for tf in self.TF_ORDER]
        present = [(tf, s) for tf, s in present if isinstance(s, (int, float))]
        if len(present) < 2:
            # One timeframe cannot resonate with anything. Say so.
            return {
                "pair": pair,
                "consonance": None,
                "dissonant_tf": [],
                "harmonic_power": None,
                "resolution_bias": "UNMEASURED",
                "octave_ratio": None,
                "intervals": [],
                "tf_measured": [tf for tf, _ in present],
                "measured": False,
            }

        tfs = [tf for tf, _ in present]
        scores = [s for _, s in present]
        directions = [1 if s > 0.5 else -1 for s in scores]
        majority = 1 if sum(directions) > 0 else -1

        consonance = sum(1 for d in directions if d == majority) / len(directions)
        dissonant = [tfs[i] for i, d in enumerate(directions) if d != majority]

        # Adjacent intervals (Pythagorean ratios)
        intervals = []
        for i in range(len(scores) - 1):
            lo, hi = min(scores[i], scores[i + 1]), max(scores[i], scores[i + 1])
            intervals.append(round(hi / (lo + 1e-10), 3))

        # Harmonic power: geometric product of conviction strengths for aligned TFs
        aligned = [abs(scores[i] - 0.5) * 2 for i in range(len(scores)) if directions[i] == majority]
        power = 1.0
        for a in aligned:
            power *= max(a, 0.01)

        # Octave ratio: fastest vs slowest aligned
        aligned_raw = [scores[i] for i in range(len(scores)) if directions[i] == majority]
        octave = aligned_raw[0] / (aligned_raw[-1] + 1e-10) if len(aligned_raw) >= 2 else 1.0

        return {
            "pair": pair,
            "consonance": round(consonance, 3),
            "dissonant_tf": dissonant,
            "harmonic_power": round(power, 6),
            "resolution_bias": "BULL" if majority > 0 else "BEAR",
            "octave_ratio": round(octave, 3),
            "intervals": intervals,
            "tf_measured": tfs,
            "measured": True,
        }


# ---------------------------------------------------------------------------
# Engine 5: Gaussian Belief Fusion
# ---------------------------------------------------------------------------

_REGIME_SCALE = {
    "BULL": 1.0, "TRENDING": 1.0, "TREND_UP": 1.0, "bull": 1.0,
    "BEAR": -1.0, "TREND_DOWN": -1.0, "bear": -1.0,
    "RANGE": 0.0, "RANGING": 0.0, "range": 0.0, "ranging": 0.0,
    "EQUILIBRIUM": 0.0, "NEUTRAL": 0.0,
    "TRANSITIONING": 0.3, "transitioning": 0.3,
    "CHOP": -0.3, "chop": -0.3,
    "VOLATILE": 0.2,
}


class GaussianBeliefFusion:
    """Gauss's optimal fusion of uncertain regime estimates.
    Weights each source by inverse variance (historical accuracy × confidence).
    """

    def __init__(self):
        # Historical accuracy — starts at 0.5, updated as regime predictions
        # are verified against actual market moves
        self.accuracy = {}  # {source: float}
        # Pending calls awaiting a verdict: {source: regime_value} from the
        # previous cycle, scored once the realized move is known.
        self._pending = {}
        self._scored = {}   # {source: [n_scored, n_correct]}

    def score_previous(self, realized_move):
        """Grade last cycle's regime calls against the realized market move.

        Before 2026-08-06 `self.accuracy` was initialised to {} and WRITTEN
        NOWHERE — every lookup fell through to the 0.5 default, so every
        source carried identical precision and this "Gauss-optimal
        precision-weighted fusion" was arithmetically a plain average,
        permanently. The docstring's promise that accuracy is "updated as
        regime predictions are verified" was never implemented, and the
        weights block published 10 sources at exactly 0.1 each with
        information_gain 0.0 — honest, but honest about doing nothing.

        realized_move: signed fractional change of the market proxy over the
        cycle (e.g. +0.004 = +0.4%). None when unknown, which scores nothing
        rather than guessing a direction.
        """
        if realized_move is None or not self._pending:
            self._pending = {}
            return
        # Below this the move is noise and BOTH a directional and a range
        # call are defensible, so scoring it would reward luck.
        DEAD_ZONE = 0.0015
        actual = 0.0
        if realized_move > DEAD_ZONE:
            actual = 1.0
        elif realized_move < -DEAD_ZONE:
            actual = -1.0

        for source, predicted in self._pending.items():
            # Correct if the call had the right sign, or if it called RANGE
            # (0.0) and the market genuinely went nowhere.
            if actual == 0.0:
                correct = abs(predicted) < 0.5
            else:
                correct = (predicted * actual) > 0
            rec = self._scored.setdefault(source, [0, 0])
            rec[0] += 1
            rec[1] += 1 if correct else 0
            # Laplace-smoothed hit rate: starts at 0.5 with no evidence and
            # moves only as calls are actually graded, so a source with 2
            # lucky hits does not jump to 1.0 and dominate the fusion.
            n, k = rec
            self.accuracy[source] = (k + 1.0) / (n + 2.0)
        self._pending = {}

    def accuracy_report(self):
        """Per-source grading record, for the snapshot."""
        return {s: {"scored": n, "correct": k, "accuracy": round(self.accuracy.get(s, 0.5), 4)}
                for s, (n, k) in sorted(self._scored.items())}

    def fuse(self, estimates):
        """Fuse multiple regime estimates optimally.
        estimates = [{"source": "trinity", "regime": "BULL", "confidence": 0.72}, ...]
        """
        if not estimates:
            return {"fused_regime": "UNKNOWN", "fused_confidence": 0.0}

        values = []
        precisions = []

        for est in estimates:
            val = _REGIME_SCALE.get(est["regime"], 0.0)
            acc = self.accuracy.get(est["source"], 0.5)
            conf = est.get("confidence", 0.5)
            precision = max(acc * conf, 0.01)
            values.append(val)
            precisions.append(precision)
            # Remember this call so the next cycle can grade it against the
            # realized move (see score_previous).
            self._pending[est["source"]] = val

        total_prec = sum(precisions)
        fused_val = sum(v * p for v, p in zip(values, precisions)) / total_prec
        fused_var = 1.0 / total_prec
        fused_conf = max(0.0, min(1.0, 1.0 - fused_var))

        # Information gain: bits gained by precision-weighted (Gauss-optimal)
        # fusion over a NAIVE equal-weighted average of the same N sources.
        #
        # var_naive = variance of a plain average of N estimates, each with
        #             its own variance 1/precision_i:
        #               var_naive = (1/N^2) * sum(1/precision_i)
        # var_opt   = variance of the precision-weighted fusion (the BLUE
        #             estimator): var_opt = 1 / sum(precision_i)
        # info_gain_bits = 0.5 * log2(var_naive / var_opt)
        #
        # At UNIFORM weights (self.accuracy all equal — the untrained
        # default state) precision-weighted fusion IS the naive average,
        # so var_naive == var_opt and info_gain == 0 bits exactly: there is
        # no informational advantage to claim yet because the model hasn't
        # learned which sources are more trustworthy. Gain only grows once
        # self.accuracy differentiates sources (post 50+ scored cycles),
        # which is when precision-weighting actually starts outperforming
        # a plain average. This replaces the old total_prec/best_single
        # formula, which degenerated to exactly n_sources at uniform
        # weights (a source count mislabeled as "information gain").
        n_sources = len(precisions)
        if n_sources > 0 and total_prec > 0:
            var_naive = sum(1.0 / p for p in precisions) / (n_sources ** 2)
            var_opt = 1.0 / total_prec
            info_gain = 0.5 * math.log2(var_naive / var_opt) if var_opt > 0 else 0.0
            info_gain = max(0.0, info_gain)  # guard float noise at exact uniformity
        else:
            info_gain = 0.0

        if fused_val > 0.6:
            regime = "BULL"
        elif fused_val > 0.2:
            regime = "TRANSITIONING_BULL"
        elif fused_val > -0.2:
            regime = "RANGE"
        elif fused_val > -0.6:
            regime = "TRANSITIONING_BEAR"
        else:
            regime = "BEAR"

        weights = {est["source"]: round(p / total_prec, 3)
                   for est, p in zip(estimates, precisions)}

        return {
            "fused_regime": regime,
            "fused_value": round(fused_val, 4),
            "fused_confidence": round(fused_conf, 4),
            "information_gain": round(info_gain, 2),
            "weights": weights,
        }


# ---------------------------------------------------------------------------
# Engine 6: Riemann Spectral Analysis
# ---------------------------------------------------------------------------

def riemann_spectral(events, event_type, max_freqs=5):
    """Spectral analysis of event timing — find hidden periodicities."""
    from math import pi, sin, cos, sqrt, log2

    timestamps = sorted(e.get("ts", e.get("timestamp", 0))
                        for e in events if e.get("type") == event_type)
    if len(timestamps) < 10:
        return {"dominant_period_min": None, "spectral_entropy": 1.0, "clustering": 0.0}

    intervals = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    N = len(intervals)
    mean_iv = sum(intervals) / N
    centered = [x - mean_iv for x in intervals]

    # DFT
    max_k = min(N // 2, 50)
    spectrum = []
    for k in range(1, max_k + 1):
        re = sum(centered[n] * cos(2 * pi * k * n / N) for n in range(N))
        im = sum(centered[n] * sin(2 * pi * k * n / N) for n in range(N))
        power = (re ** 2 + im ** 2) / N
        period = (mean_iv * N) / k / 60
        spectrum.append({"freq": k, "power": round(power, 4), "period_min": round(period, 1)})

    dominant = max(spectrum, key=lambda x: x["power"]) if spectrum else None

    # Spectral entropy
    total_p = sum(s["power"] for s in spectrum) + 1e-10
    probs = [s["power"] / total_p for s in spectrum]
    entropy = -sum(p * log2(p) if p > 0 else 0 for p in probs)
    max_ent = log2(len(probs)) if len(probs) > 1 else 1
    norm_entropy = entropy / max_ent if max_ent > 0 else 1.0

    # Clustering (CV)
    var = sum((x - mean_iv) ** 2 for x in intervals) / N
    cv = sqrt(var) / mean_iv if mean_iv > 0 else 0

    return {
        "dominant_period_min": dominant["period_min"] if dominant else None,
        "spectral_entropy": round(norm_entropy, 4),
        "clustering": round(cv, 3),
        "mean_interval_s": round(mean_iv, 1),
        "top_freqs": sorted(spectrum, key=lambda x: x["power"], reverse=True)[:max_freqs],
    }


# ---------------------------------------------------------------------------
# Engine 9: Einsteinian Relativity
# ---------------------------------------------------------------------------

class EinsteinianRelativity:
    """E=mc² applied to market energy. Time dilation near volume zones.
    Reference-frame-invariant z-scores. Spacetime interval classification."""

    def compute(self, pair, candles):
        if len(candles) < 50:
            return self._default()

        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]

        # === 1. MASS-ENERGY: E = mc² ===
        # Mass = volume concentration at current price zone (±1 ATR)
        atr = self._compute_atr(highs, lows, closes, 14)
        current = closes[-1]
        zone_low, zone_high = current - atr, current + atr

        mass = sum(volumes[i] for i in range(-50, 0) if zone_low <= closes[i] <= zone_high)
        total_vol = sum(volumes[-50:]) + 1e-10
        mass_concentration = mass / total_vol

        # Velocity = rate of price change (percentage per bar)
        returns = [(closes[i] - closes[i - 1]) / (closes[i - 1] + 1e-10)
                   for i in range(-20, 0)]
        velocity = sum(returns) / len(returns) * 100

        # Energy = mass × velocity² (high mass + high velocity = explosive release)
        energy = mass_concentration * velocity ** 2 * 10000

        # === 2. TIME DILATION ===
        # Volume profile: price moves slower in high-volume zones, faster in deserts
        zones = 20
        price_range = max(highs[-50:]) - min(lows[-50:])
        if price_range < 1e-10:
            time_dilation = 1.0
        else:
            zone_width = price_range / zones
            price_min = min(lows[-50:])
            zone_volumes = [0.0] * zones
            for i in range(-50, 0):
                z = int((closes[i] - price_min) / zone_width)
                z = max(0, min(zones - 1, z))
                zone_volumes[z] += volumes[i]

            avg_zone_vol = sum(zone_volumes) / zones + 1e-10
            current_zone = int((current - price_min) / zone_width)
            current_zone = max(0, min(zones - 1, current_zone))
            time_dilation = zone_volumes[current_zone] / avg_zone_vol

        # === 3. REFERENCE FRAME INVARIANT — z-score ===
        mean_ret = sum(returns) / len(returns)
        std_ret = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5
        z_score = (returns[-1] - mean_ret) / (std_ret + 1e-10)

        # === 4. SPACETIME INTERVAL ===
        abs_returns = sorted([(abs(returns[i]), i) for i in range(len(returns))],
                             reverse=True)
        if len(abs_returns) >= 2:
            m1, m2 = abs_returns[0][1], abs_returns[1][1]
            price_sep = abs(returns[m1] - returns[m2])
            time_sep = abs(m1 - m2)
            interval_sq = price_sep ** 2 - (time_sep * (std_ret + 1e-10)) ** 2
            if interval_sq > 0:
                spacetime_type = "CAUSAL"
                interval = math.sqrt(interval_sq)
            else:
                spacetime_type = "COINCIDENTAL"
                interval = math.sqrt(abs(interval_sq))
        else:
            spacetime_type, interval = "INSUFFICIENT_DATA", 0

        breakout_potential = energy / (time_dilation + 0.1)

        return {
            "pair": pair,
            "mass_concentration": round(mass_concentration, 4),
            "velocity": round(velocity, 4),
            "energy": round(energy, 4),
            "time_dilation": round(time_dilation, 3),
            "z_score_invariant": round(z_score, 3),
            "spacetime_interval": round(interval, 6),
            "spacetime_type": spacetime_type,
            "breakout_potential": round(breakout_potential, 4),
            "interpretation": self._interpret(energy, time_dilation, z_score),
        }

    def _interpret(self, energy, dilation, z):
        if energy > 5 and dilation < 0.8:
            return "HIGH_ENERGY_LOW_GRAVITY"
        elif energy > 5 and dilation > 1.5:
            return "HIGH_ENERGY_TRAPPED"
        elif energy < 1 and dilation > 2:
            return "LOW_ENERGY_HEAVY"
        elif abs(z) > 2:
            return "EXTREME_MOVE"
        return "NORMAL_SPACETIME"

    def _compute_atr(self, highs, lows, closes, period):
        trs = []
        for i in range(-period, 0):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        return sum(trs) / len(trs) if trs else 0

    def _default(self):
        return {"pair": None, "mass_concentration": 0, "velocity": 0, "energy": 0,
                "time_dilation": 1, "z_score_invariant": 0, "spacetime_interval": 0,
                "spacetime_type": "INSUFFICIENT_DATA", "breakout_potential": 0,
                "interpretation": "INSUFFICIENT_DATA"}


# ---------------------------------------------------------------------------
# Engine 10: Schwarzschild Horizons
# ---------------------------------------------------------------------------

class SchwarzschildHorizons:
    """Event horizon physics applied to market structure.
    Computes Schwarzschild radius for each S/R level, escape velocity,
    Hawking radiation (level weakening), and market topology classification."""

    def compute(self, pair, candles, euclid_data=None):
        if len(candles) < 100:
            return self._default(pair)

        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        current = closes[-1]

        # Get S/R levels from Euclid or compute basic ones
        if euclid_data and euclid_data.get("support_levels"):
            levels = (euclid_data.get("support_levels", []) +
                      euclid_data.get("resistance_levels", []))
        else:
            levels = self._find_levels(closes, volumes, highs, lows)

        if not levels:
            return self._default(pair)

        # Market velocity
        returns = [(closes[i] - closes[i - 1]) / (closes[i - 1] + 1e-10)
                   for i in range(-20, 0)]
        volatility = (sum(r ** 2 for r in returns) / len(returns)) ** 0.5
        momentum = abs(sum(returns[-5:]) / 5) * 100

        horizons = []
        for level in levels:
            price = level.get("price", 0)
            if price == 0:
                continue

            vol_weight = level.get("volume_weight", 0.3)
            touches = level.get("touches", 1)
            strength = level.get("strength", 0.5)

            # R_s = 2GM/c²  =>  R_market = 2 × volume × strength / volatility²
            M = vol_weight * strength * 100
            c_sq = max(volatility ** 2, 1e-10) * 10000
            R_s_pct = 2 * M / c_sq

            distance = abs(current - price) / (current + 1e-10) * 100
            within_horizon = distance < R_s_pct

            # Escape velocity: sqrt(2GM/r)
            r = max(distance, 0.01)
            escape_velocity = math.sqrt(2 * M / r)
            can_escape = momentum > escape_velocity

            # Hawking radiation: strength decays with tests
            hawking = max(0.1, 1 - touches * 0.05)
            eff_strength = strength * hawking

            horizons.append({
                "price": round(price, 6),
                "type": "SUPPORT" if price < current else "RESISTANCE",
                "schwarzschild_radius_pct": round(R_s_pct, 4),
                "distance_pct": round(distance, 4),
                "within_horizon": within_horizon,
                "escape_velocity": round(escape_velocity, 4),
                "current_momentum": round(momentum, 4),
                "can_escape": can_escape,
                "hawking_factor": round(hawking, 3),
                "effective_strength": round(eff_strength, 3),
                "is_singularity": R_s_pct > 2.0,
                "touches": touches,
            })

        horizons.sort(key=lambda h: h["distance_pct"])
        nearest = horizons[0] if horizons else None
        trapped = [h for h in horizons if h["within_horizon"]]
        singularities = [h for h in horizons if h["is_singularity"]]

        return {
            "pair": pair,
            "horizons": horizons[:10],
            "nearest_horizon": nearest,
            "singularity_count": len(singularities),
            "trapped_in_horizon": len(trapped) > 0,
            "current_momentum": round(momentum, 4),
            "market_topology": self._topology(horizons, momentum),
        }

    def _topology(self, horizons, momentum):
        trapped = [h for h in horizons if h["within_horizon"]]
        nearest = horizons[0] if horizons else None
        if not nearest:
            return "FLAT_SPACE"
        if len(trapped) > 1:
            return "BINARY_SYSTEM"
        if len(trapped) == 1:
            return "ESCAPE_TRAJECTORY" if trapped[0]["can_escape"] else "CAPTURED"
        if nearest["distance_pct"] < nearest["schwarzschild_radius_pct"] * 2:
            return "APPROACHING_HORIZON"
        return "FREE_SPACE"

    def _find_levels(self, closes, volumes, highs, lows):
        levels = []
        max_vol = max(volumes) if volumes else 1
        for i in range(5, len(closes) - 5):
            if highs[i] == max(highs[max(0, i - 5):i + 6]):
                vol = sum(volumes[max(0, i - 2):i + 3]) / 5
                levels.append({"price": highs[i], "volume_weight": vol / max_vol,
                                "touches": 1, "strength": 0.5})
            if lows[i] == min(lows[max(0, i - 5):i + 6]):
                vol = sum(volumes[max(0, i - 2):i + 3]) / 5
                levels.append({"price": lows[i], "volume_weight": vol / max_vol,
                                "touches": 1, "strength": 0.5})
        if not levels:
            return []
        levels.sort(key=lambda l: l["price"])
        deduped = [levels[0]]
        for l in levels[1:]:
            if abs(l["price"] - deduped[-1]["price"]) / (deduped[-1]["price"] + 1e-10) > 0.005:
                deduped.append(l)
            else:
                deduped[-1]["touches"] += 1
                deduped[-1]["strength"] = min(1, deduped[-1]["strength"] + 0.1)
        return deduped

    def _default(self, pair=None):
        return {"pair": pair, "horizons": [], "nearest_horizon": None,
                "singularity_count": 0, "trapped_in_horizon": False,
                "current_momentum": 0, "market_topology": "INSUFFICIENT_DATA"}


class NexusEngine:
    """Market Interferometer — intelligence about intelligence."""

    def __init__(self):
        # Original engines
        self.propagation = PropagationTracker()
        self.dispersion = LensDispersion()
        self.echoes = TemporalEchoDetector()
        # New mathematical engines
        self.pythagorean = PythagoreanResonance()
        self.gaussian = GaussianBeliefFusion()
        self.newton = NewtonianForceEngine()
        self.euclid = EuclideanStructureEngine()
        self.einstein = EinsteinianRelativity()
        self.schwarzschild = SchwarzschildHorizons()
        self.cycle = 0
        self.scan_duration = 0.0
        self.status = "initializing"
        self.state = {}
        self._lock = threading.Lock()
        self._log_buf = deque(maxlen=100)
        self._prev_vol_forecast = "NEUTRAL"
        self._prev_character = "QUIET"
        self._event_pub = EventPublisher(COMMAND_CENTER, "nexus") if EventPublisher else None
        # Emit-on-change state for MANIFOLD_WARNING: {pair: (above_threshold, interpretation)}
        # Prevents re-firing every 15s scan cycle while a pair stays elevated --
        # only emits on entry into the warning band or a change in manifold
        # classification, mirroring how a state machine should gate alerts.
        self._prev_manifold_state = {}

    def _log(self, msg):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_buf.append(line)
        # nexus is the fleet's busiest engine and its _log_buf is a bounded
        # deque that nothing persists, so before 2026-08-06 an error here
        # existed only in RAM and died with the process: the scan loop
        # formats "ERROR: {e}" and handed it to a dead end. logs/bots/nexus.log
        # sat at 672 bytes while the cycle counter advanced 54 -> 77.
        if _is_severe(msg):
            print(line, flush=True)

    def _fetch_candles(self, pair, interval=5, limit=200):
        """Fetch cached candles from Command Center's shared market layer."""
        try:
            resp = requests.get(f"{COMMAND_CENTER}/api/market/ohlc",
                                params={"pair": pair, "interval": interval, "limit": limit},
                                timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("candles", [])
        except Exception:
            pass
        return []

    def _fetch_correlations(self):
        """Fetch Brainiac correlation matrix. Returns {pair_key: corr} dict."""
        try:
            resp = requests.get(f"{COMMAND_CENTER}/api/brainiac/correlations", timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                if data and data.get("data"):
                    d = data["data"]
                    # Rebuild matrix from top + least correlated lists
                    matrix = {}
                    for item in d.get("top_correlated", []):
                        if isinstance(item, list) and len(item) == 2:
                            matrix[item[0]] = item[1]
                    for item in d.get("least_correlated", []):
                        if isinstance(item, list) and len(item) == 2:
                            matrix[item[0]] = item[1]
                    return matrix
        except Exception:
            pass
        return {}

    def _fetch_universe_pairs(self):
        """Get active trading pairs from Command Center universe."""
        try:
            resp = requests.get(f"{COMMAND_CENTER}/api/universe", timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                return [p.get("display", p.get("pair", ""))
                        for p in data.get("pairs", [])][:20]
        except Exception:
            pass
        return ["BTC/USD", "ETH/USD", "SOL/USD"]

    def _fetch_events(self):
        try:
            resp = requests.get(f"{COMMAND_CENTER}/api/events/recent?n=200", timeout=5)
            return resp.json() if resp.status_code == 200 else []
        except Exception:
            return []

    def _fetch_bot_states(self):
        try:
            resp = requests.get(f"{COMMAND_CENTER}/api/master", timeout=5)
            if resp.status_code != 200:
                return {}
            data = resp.json()
            return {b["id"]: b for b in data.get("bots", []) if b.get("alive")}
        except Exception:
            return {}

    def compute(self):
        self.cycle += 1
        t0 = time.time()

        events = self._fetch_events()
        bot_states = self._fetch_bot_states()

        if not events and not bot_states:
            self._log("DEGRADED: no data from Command Center — cycle running on STALE inputs")
            self.status = "waiting"
            return

        self.status = "computing"

        # Feed events to engines
        for ev in events:
            self.propagation.on_event(ev)
            self.echoes.on_event(ev)

        # Compute lens dispersion from bot states
        disp = self.dispersion.compute(bot_states)

        # Get propagation fingerprints
        fingerprints = self.propagation.get_active_fingerprints()
        if fingerprints:
            # Dominant move type
            types = [f["move_type"] for f in fingerprints]
            dominant = Counter(types).most_common(1)[0][0]
        else:
            dominant = "QUIET"

        # Get echoes
        recent_echoes = self.echoes.recent_echoes(max_age=1800)
        echo_signal = None
        high_echoes = [e for e in recent_echoes if e["significance"] == "HIGH"]
        if high_echoes:
            latest = high_echoes[-1]
            echo_signal = {
                "type": "STRUCTURAL_LEAD",
                "slow_source": latest["slow_source"],
                "fast_source": latest["fast_source"],
                "pair": latest.get("pair"),
                "lag": latest["lag_human"],
                "message": f"{latest['slow_source']} detected change {latest['lag_human']} before {latest['fast_source']}",
            }

        interpretation = _interpret(dominant, disp["prediction"], echo_signal)

        # Engine 4: Pythagorean Harmonic Resonance
        # Fetch PHITEX snapshot for per-pair multi-TF data
        pythagorean_results = {}
        try:
            phitex_resp = requests.get(f"{PHITEX_URL}/api/snapshot", timeout=3)
            if phitex_resp.status_code == 200:
                phitex_data = phitex_resp.json()
                _phx_pairs = phitex_data.get("pairs")
                if not isinstance(_phx_pairs, dict):
                    _phx_pairs = {}
                for pair, pdata in _phx_pairs.items():
                    # PHITEX state variables are normalized MAGNITUDES, not
                    # directional trend scores. Feeding them straight into
                    # PythagoreanResonance was a category error: that engine
                    # reads >0.5 as bullish and <=0.5 as bearish, but
                    # `temperature` is >0.5 on 56/56 live pairs (min 0.837)
                    # while `chi_norm` and `phi_tex` are >0.5 on 3 and 2 of 56.
                    # The direction pattern was therefore a fixed artefact of
                    # each variable's distribution, not of the market — every
                    # top pair reported an identical confident BEAR, and the
                    # derived bias agreed with PHITEX's own `direction` on
                    # 1 of 8 pairs, i.e. worse than chance.
                    #
                    # Direction comes from PHITEX's `direction` field, which is
                    # what actually carries it. Magnitude supplies only the
                    # CONVICTION away from neutral. A NEUTRAL pair — 48 of 56
                    # live, PHITEX reporting an honest EQUILIBRIUM — yields no
                    # timeframe votes at all rather than a manufactured bias.
                    _dir = str(pdata.get("direction") or "").upper()
                    if _dir in ("LONG", "BULL", "UP"):
                        _sign = 1.0
                    elif _dir in ("SHORT", "BEAR", "DOWN"):
                        _sign = -1.0
                    else:
                        _sign = None  # NEUTRAL / absent — no directional claim

                    _proxy_src = {
                        "5m": "chi_norm",       # fastest-responding
                        "15m": "C_norm",        # mid-fast
                        "1h": "temperature",    # base TF
                        "4h": "FCI_norm",       # structural
                        "1d": "phi_tex",        # slowest composite
                    }
                    tf_proxy = {}
                    if _sign is not None:
                        for _tf, _k in _proxy_src.items():
                            _v = pdata.get(_k)
                            # A magnitude of ~0 is ZERO CONVICTION on that
                            # timeframe, not a bearish opinion. It maps to
                            # exactly 0.5, and `0.5 > 0.5` is False, so
                            # including it cast a silent bear vote: live
                            # LTC/USD and PUMP/USD each had three 0.0000
                            # readings outvote two genuine bullish ones and
                            # invert a PHITEX LONG into a BEAR. Timeframes
                            # with no conviction abstain.
                            if isinstance(_v, (int, float)) and abs(_v) > 1e-9:
                                # magnitude -> signed score around the 0.5
                                # neutral point, clamped to [0, 1]
                                tf_proxy[_tf] = max(0.0, min(
                                    1.0, 0.5 + _sign * abs(_v) * 0.5))
                    # With no direction, tf_proxy stays empty and compute()
                    # returns resolution_bias UNMEASURED — which is the truth.
                    #
                    # KNOWN LIMIT (2026-08-06): every timeframe now inherits
                    # its sign from the single PHITEX `direction` field, so
                    # they cannot disagree and consonance is 1.0 whenever a
                    # direction exists. This is an honest conviction-weighted
                    # reading, NOT independent cross-timeframe confirmation —
                    # do not read consonance here as agreement between
                    # separately-derived timeframe opinions. Genuine
                    # multi-TF resonance needs per-timeframe directional
                    # inputs, which PHITEX does not currently publish.
                    pythagorean_results[pair] = self.pythagorean.compute(pair, tf_proxy)
        except Exception:
            pass

        # Engine 4b: Archimedean Displacement from Brainiac depth data
        archimedean = {}
        try:
            # Fetch depth data for top pairs
            for pair_name in ["BTC/USD", "ETH/USD", "SOL/USD"]:
                try:
                    resp = requests.get(f"{COMMAND_CENTER}/api/brainiac/depth",
                                        params={"pair": pair_name}, timeout=2)
                    if resp.status_code == 200:
                        d = resp.json()
                        if d and d.get("data"):
                            dd = d["data"]
                            archimedean[pair_name] = {
                                "bid_depth": dd.get("bid_depth_usd", 0),
                                "ask_depth": dd.get("ask_depth_usd", 0),
                                "imbalance": dd.get("imbalance", 0),
                                "spread_pct": dd.get("spread_pct", 0),
                                "direction": "BUY" if dd.get("imbalance", 0) > 0.1 else
                                             "SELL" if dd.get("imbalance", 0) < -0.1 else "NEUTRAL",
                                "blacklisted": _is_blacklisted(pair_name),
                            }
                except Exception:
                    pass
        except Exception:
            pass

        # Engine 5: Gaussian Belief Fusion
        #
        # Grade the PREVIOUS cycle's calls before making new ones. BTC is the
        # market proxy: a regime call is a claim about market direction, and
        # BTC's realized move over the cycle is the cheapest honest ground
        # truth available here. If the price is unavailable the cycle scores
        # nothing rather than inventing a verdict.
        _realized = None
        try:
            _btc = self._fetch_candles("BTC/USD", interval=5, limit=3)
            if _btc and len(_btc) >= 2:
                _prev_c = float(_btc[-2][4])
                _last_c = float(_btc[-1][4])
                if _prev_c > 0:
                    _realized = (_last_c - _prev_c) / _prev_c
        except Exception:
            _realized = None
        try:
            self.gaussian.score_previous(_realized)
        except Exception:
            pass

        regime_estimates = []
        for bot_id, bstate in bot_states.items():
            norm = bstate.get("normalized", {}) or {}
            regime = norm.get("regime")
            if regime and bot_id not in ("aegis", "phitex", "nexus"):
                # Per-source confidence is the graded hit rate when the source
                # has been scored, and 0.5 (no evidence) when it has not. The
                # old hardcoded 0.6 made every precision identical, so both
                # inputs to the "precision weighting" were constants and the
                # fusion could never be anything but a plain average.
                _acc = self.gaussian.accuracy.get(bot_id)
                regime_estimates.append({
                    "source": bot_id,
                    "regime": regime,
                    "confidence": _acc if isinstance(_acc, float) else 0.5,
                })
        gaussian_result = self.gaussian.fuse(regime_estimates) if regime_estimates else {}
        if gaussian_result:
            gaussian_result["accuracy"] = self.gaussian.accuracy_report()
            gaussian_result["realized_move"] = (
                round(_realized, 6) if _realized is not None else None)

        # Engine 6: Riemann Spectral Analysis
        whale_spectral = riemann_spectral(events, "WHALE_ALERT")
        trade_spectral = riemann_spectral(events, "TRADE_CLOSE")

        # Engine 7 & 8: Newton + Euclid (per-pair, use 5m candles)
        newton_results = {}
        euclid_results = {}
        correlations = self._fetch_correlations()
        universe_pairs = self._fetch_universe_pairs()

        einstein_results = {}
        schwarzschild_results = {}
        info_geo_results = {}
        topology_results = {}
        quantum_results = {}
        lorenz_results = {}
        prigogine_results = {}
        thom_results = {}

        for pair_name in universe_pairs[:10]:  # top 10 pairs to keep cycle fast
            candles = self._fetch_candles(pair_name, interval=5, limit=300)
            if len(candles) >= 30:
                newton_results[pair_name] = self.newton.compute(
                    pair_name, candles, correlations)
                euclid_results[pair_name] = self.euclid.compute(
                    pair_name, candles, lookback=200)
                # Engine 9: Einstein — needs 50+ candles
                if len(candles) >= 50:
                    einstein_results[pair_name] = self.einstein.compute(
                        pair_name, candles)
                # Engine 10: Schwarzschild — needs 100+ candles, uses Euclid data
                if len(candles) >= 100:
                    schwarzschild_results[pair_name] = self.schwarzschild.compute(
                        pair_name, candles, euclid_results.get(pair_name))

                # Council engines (info geometry, topology, quantum, causal)
                if _council_loaded:
                    try:
                        ig = _info_geo.compute(pair_name, candles)
                        info_geo_results[pair_name] = ig

                        topo = _topology.analyze(pair_name, candles)
                        topology_results[pair_name] = topo

                        # Feed causal flow with latest price and volume
                        last_close = float(candles[-1][4])
                        last_vol = float(candles[-1][5])
                        _causal.record(f"{pair_name}_price", last_close)
                        _causal.record(f"{pair_name}_volume", last_vol)

                        # Feed Shannon with price data
                        _shannon.record(f"{pair_name}_close", last_close)
                        _shannon.record(f"{pair_name}_volume", last_vol)

                        # Lorenz: chaos analysis (needs 95+ candles for embedding)
                        lorenz_results[pair_name] = _lorenz.analyze(pair_name, candles)

                        # Prigogine: dissipative structure detection (needs 210+ candles)
                        prigogine_results[pair_name] = _prigogine.analyze(pair_name, candles)

                        # Thom: catastrophe early warning (needs 80+ candles)
                        thom_results[pair_name] = _thom.analyze(pair_name, candles)

                        # Quantum: decohere, then inject signals from other engines
                        _quantum.decohere(pair_name, SCAN_INTERVAL)

                        # Build state biases from newton force direction
                        nr = newton_results.get(pair_name)
                        if nr:
                            force_dir = nr.get("force_direction", "NEUTRAL")
                            biases = {}
                            if force_dir == "BULL":
                                biases = {"TRENDING_UP": 0.5, "TRENDING_DOWN": -0.3}
                            elif force_dir == "BEAR":
                                biases = {"TRENDING_DOWN": 0.5, "TRENDING_UP": -0.3}
                            if biases:
                                _quantum.inject_signal(pair_name, "newton_force",
                                                       biases, strength=min(abs(nr.get("force", 0)), 3.0))

                        # Inject from info geometry regime change probability
                        if ig.get("regime_change_probability", 0) > 0.3:
                            _quantum.inject_signal(pair_name, "info_geo_instability",
                                                   {"VOLATILE": 0.4, "QUIET": -0.3},
                                                   strength=ig["regime_change_probability"])

                        # Inject from topology cyclicality
                        if topo.get("cyclicality", 0) > 0.2:
                            _quantum.inject_signal(pair_name, "topo_cyclical",
                                                   {"RANGING": 0.3, "TRENDING_UP": -0.1,
                                                    "TRENDING_DOWN": -0.1},
                                                   strength=topo["cyclicality"])

                        # Inject from Lorenz chaos into quantum state
                        lr = lorenz_results.get(pair_name, {})
                        if lr.get("attractor_departure", 0) > 0.5:
                            _quantum.inject_signal(pair_name, "lorenz_chaos",
                                                   {"VOLATILE": 0.5, "RANGING": -0.3},
                                                   strength=lr["attractor_departure"])

                        # Inject from Thom catastrophe warning
                        tr = thom_results.get(pair_name, {})
                        if tr.get("ews_score", 0) > 0.6:
                            _quantum.inject_signal(pair_name, "thom_catastrophe",
                                                   {"VOLATILE": 0.4, "TRENDING_UP": -0.2,
                                                    "TRENDING_DOWN": -0.2},
                                                   strength=tr["ews_score"])

                        quantum_results[pair_name] = _quantum.get_full_state(pair_name)
                    except Exception:
                        pass

        # Causal flow: build graph and extract summary after all pairs recorded
        causal_summary = {}
        shannon_map = {}
        boltzmann_results = {}
        if _council_loaded:
            try:
                _causal.build_causal_graph()
                causal_summary = _causal.get_summary()
            except Exception:
                pass

            # Shannon: build fleet information map from all recorded price series
            try:
                shannon_map = _shannon.build_fleet_map()
            except Exception:
                pass

            # Boltzmann: analyze order book depth for top pairs
            for pair_name in universe_pairs[:5]:
                try:
                    resp = requests.get(f"{COMMAND_CENTER}/api/brainiac/depth",
                                        params={"pair": pair_name}, timeout=2)
                    if resp.status_code == 200:
                        d = resp.json()
                        if d and d.get("data"):
                            dd = d["data"]
                            # Use aggregated method — Brainiac stores summaries, not raw arrays
                            agg_snap = {
                                "pair": pair_name,
                                "bid_depth_usd": dd.get("bid_depth_usd", 0),
                                "ask_depth_usd": dd.get("ask_depth_usd", 0),
                                "imbalance": dd.get("imbalance", 0),
                                "spread_pct": dd.get("spread_pct", 0),
                                "levels": dd.get("levels", 0),
                            }
                            boltzmann_results[pair_name] = _boltzmann.analyze_aggregated(agg_snap)
                except Exception:
                    pass

        # Newton snapshot summaries
        newton_top_forces = sorted(
            [{"pair": p, "force": r["force"], "direction": r["force_direction"],
              "inertia": r["inertia_score"], "blacklisted": _is_blacklisted(p)}
             for p, r in newton_results.items() if abs(r["force"]) > 0.01],
            key=lambda x: abs(x["force"]), reverse=True,
        )[:5]

        newton_reaction_alerts = []
        for p, r in newton_results.items():
            for rp in r.get("reaction_pairs", [])[:3]:
                if rp["force_transfer"] > 0.5:
                    newton_reaction_alerts.append({
                        "trigger": p,
                        "reactor": rp["pair"],
                        "lag": rp["expected_lag_bars"],
                        "force_transfer": rp["force_transfer"],
                        "blacklisted": _is_blacklisted(p) or _is_blacklisted(rp["pair"]),
                    })
        newton_reaction_alerts.sort(key=lambda x: x["force_transfer"], reverse=True)
        newton_reaction_alerts = newton_reaction_alerts[:5]

        # Euclid snapshot summaries
        euclid_closest = [
            {"pair": p, "nearest_support": r["nearest_support"]["price"] if r["nearest_support"] else None,
             "nearest_resistance": r["nearest_resistance"]["price"] if r["nearest_resistance"] else None,
             "bias": r["geometric_bias"], "blacklisted": _is_blacklisted(p)}
            for p, r in euclid_results.items()
        ]

        euclid_breakout = sorted(
            [{"pair": p, "resistance": r["nearest_resistance"]["price"],
              "strength": r["nearest_resistance"]["strength"],
              "breakout_prob": r["breakout_probability"],
              "blacklisted": _is_blacklisted(p)}
             for p, r in euclid_results.items()
             if r["nearest_resistance"] and r["breakout_probability"] > 0.6],
            key=lambda x: x["breakout_prob"], reverse=True,
        )[:5]

        # Einstein summaries
        einstein_high_energy = sorted(
            [{"pair": p, "energy": r["energy"],
              "breakout_potential": r["breakout_potential"],
              "interpretation": r["interpretation"],
              "blacklisted": _is_blacklisted(p)}
             for p, r in einstein_results.items() if r["energy"] > 1],
            key=lambda x: x["breakout_potential"], reverse=True,
        )[:5]

        einstein_dilated = [
            {"pair": p, "time_dilation": r["time_dilation"],
             "mass_concentration": r["mass_concentration"],
             "blacklisted": _is_blacklisted(p)}
            for p, r in einstein_results.items() if r["time_dilation"] > 1.5
        ]

        # Schwarzschild summaries
        schwarzschild_trapped = [
            {"pair": p, "topology": r["market_topology"],
             "nearest": r["nearest_horizon"],
             "blacklisted": _is_blacklisted(p)}
            for p, r in schwarzschild_results.items()
            if r["market_topology"] in ("CAPTURED", "ESCAPE_TRAJECTORY", "BINARY_SYSTEM")
        ]

        schwarzschild_singularities = [
            {"pair": p, "count": r["singularity_count"],
             "topology": r["market_topology"],
             "blacklisted": _is_blacklisted(p)}
            for p, r in schwarzschild_results.items() if r["singularity_count"] > 0
        ]

        with self._lock:
            self.state = {
                "market_character": dominant,
                "volatility_forecast": disp["prediction"],
                "lens_dispersion": disp["dispersion"],
                "dispersion_trend": disp["trend"],
                "mean_conviction": disp["mean_conviction"],
                "bot_scores": disp["scores"],
                "echo_signal": echo_signal,
                "active_echoes": len(recent_echoes),
                "propagation_fingerprints": fingerprints[:5],
                "interpretation": interpretation,
                # New engines
                "pythagorean": {
                    # Unmeasured pairs carry harmonic_power/consonance None.
                    # They sort last (not as 0, which would rank them against
                    # measured pairs) and are excluded from the fleet mean
                    # rather than dragging it toward zero.
                    "top_pairs": sorted(
                        [{**v, "blacklisted": _is_blacklisted(v.get("pair", ""))}
                         for v in pythagorean_results.values()],
                        key=lambda x: (
                            1 if isinstance(x.get("harmonic_power"), (int, float)) else 0,
                            x.get("harmonic_power") if isinstance(
                                x.get("harmonic_power"), (int, float)) else 0.0),
                        reverse=True)[:5] if pythagorean_results else [],
                    "fleet_consonance": _mean_or_none([
                        r["consonance"] for r in pythagorean_results.values()
                        if isinstance(r.get("consonance"), (int, float))]),
                    "pairs_measured": sum(
                        1 for r in pythagorean_results.values()
                        if isinstance(r.get("consonance"), (int, float))),
                },
                "gaussian_fusion": gaussian_result,
                "archimedean": archimedean,
                "riemann": {
                    "whale": whale_spectral,
                    "trade": trade_spectral,
                },
                "newton": {
                    "top_forces": newton_top_forces,
                    "reaction_alerts": newton_reaction_alerts,
                    "pairs_computed": len(newton_results),
                },
                "euclid": {
                    "closest_levels": euclid_closest,
                    "breakout_candidates": euclid_breakout,
                    "pairs_computed": len(euclid_results),
                },
                "einstein": {
                    "high_energy": einstein_high_energy,
                    "time_dilated": einstein_dilated,
                    "pairs_computed": len(einstein_results),
                },
                "schwarzschild": {
                    "trapped": schwarzschild_trapped,
                    "singularities": schwarzschild_singularities,
                    "pairs_computed": len(schwarzschild_results),
                },
                # Council engines
                "info_geometry": {
                    "manifold_warnings": [
                        {"pair": p, "regime_change_prob": r["regime_change_probability"],
                         "fisher_metric": r["fisher_metric"],
                         "geodesic_velocity": r["geodesic_velocity"],
                         # Surface the model's own reliability alongside the
                         # warning. A regime-change probability published
                         # without it invites the reader to treat every
                         # warning as equally trustworthy.
                         "model_reliability": r.get("model_reliability"),
                         "interpretation": r.get("interpretation"),
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in info_geo_results.items()
                        if r.get("regime_change_probability", 0) > 0.4
                    ],
                    "pairs_computed": len(info_geo_results),
                },
                "topology": {
                    "cyclic_pairs": [
                        {"pair": p, "cyclicality": r["cyclicality"],
                         "complexity": r["complexity_score"],
                         "fragmentation": r["fragmentation"],
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in topology_results.items()
                        if r.get("cyclicality", 0) > 0.2
                    ],
                    "pairs_computed": len(topology_results),
                },
                "quantum": {
                    "collapsed": [
                        {"pair": p, "dominant": r["dominant_state"],
                         "confidence": r["confidence"],
                         "entropy": r["superposition_entropy"],
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in quantum_results.items()
                        if r.get("superposition_entropy", 1) < 0.4
                    ],
                    "superposed": [
                        {"pair": p, "entropy": r["superposition_entropy"],
                         "interpretation": r["interpretation"],
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in quantum_results.items()
                        if r.get("superposition_entropy", 0) > 0.7
                    ],
                    "pairs_computed": len(quantum_results),
                },
                "causal_flow": {
                    "strongest_links": causal_summary.get("strongest_links", [])[:5],
                    "causal_powers": causal_summary.get("causal_powers", {}),
                    "n_causal_links": causal_summary.get("n_causal_links", 0),
                    "n_series": causal_summary.get("n_series", 0),
                },
                "shannon": {
                    "signal_channels": shannon_map.get("signal_channels", 0),
                    "noise_channels": shannon_map.get("noise_channels", 0),
                    "noise_ratio": shannon_map.get("noise_ratio", 0),
                    "total_capacity_bits": shannon_map.get("total_capacity_bits", 0),
                    "top_connections": shannon_map.get("connections", [])[:5],
                    "n_series": shannon_map.get("n_series", 0),
                },
                "boltzmann": {
                    "phases": [
                        {"pair": p, "phase": r["phase"],
                         "temperature": r["temperature"],
                         "entropy": r["entropy"],
                         "deviation": r["boltzmann_deviation"],
                         "interpretation": r["interpretation"],
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in boltzmann_results.items()
                        if r.get("phase", "UNKNOWN") != "UNKNOWN"
                    ],
                    "boiling": [
                        {"pair": p, "phase": r["phase"],
                         "temperature": r["temperature"],
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in boltzmann_results.items()
                        if r.get("phase") in ("BOILING", "PLASMA")
                    ],
                    "pairs_computed": len(boltzmann_results),
                },
                "lorenz": {
                    "chaotic": [
                        {"pair": p, "lyapunov": r["lyapunov_exponent"],
                         "attractor_departure": r["attractor_departure"],
                         "predictability": r["predictability_horizon"],
                         "interpretation": r["interpretation"],
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in lorenz_results.items()
                        if r.get("lyapunov_exponent", 0) > 0
                    ],
                    "departures": [
                        {"pair": p, "departure": r["attractor_departure"],
                         "direction": r.get("interpretation", ""),
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in lorenz_results.items()
                        if r.get("attractor_departure", 0) > 0.5
                    ],
                    "pairs_computed": len(lorenz_results),
                },
                "prigogine": {
                    "structures_forming": [
                        {"pair": p, "type": r["structure_type"],
                         "score": r["structure_formation_score"],
                         "bifurcation": r["bifurcation_type"],
                         "interpretation": r["interpretation"],
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in prigogine_results.items()
                        if r.get("structure_formation_score", 0) > 0.3
                    ],
                    "far_from_equilibrium": [
                        {"pair": p, "distance": r["distance_from_equilibrium"],
                         "entropy_production": r["entropy_production_rate"],
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in prigogine_results.items()
                        if r.get("distance_from_equilibrium", 0) > 2
                    ],
                    "pairs_computed": len(prigogine_results),
                },
                "thom": {
                    "warnings": [
                        {"pair": p, "ews_score": r["ews_score"],
                         "catastrophe_type": r["catastrophe_type"],
                         "predicted_direction": r["predicted_direction"],
                         "confidence": r["direction_confidence"],
                         "time_to_event": r["estimated_time_to_event"],
                         "interpretation": r["interpretation"],
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in thom_results.items()
                        if r.get("ews_score", 0) > 0.4
                    ],
                    "imminent": [
                        {"pair": p, "ews_score": r["ews_score"],
                         "catastrophe_type": r["catastrophe_type"],
                         "direction": r["predicted_direction"],
                         "blacklisted": _is_blacklisted(p)}
                        for p, r in thom_results.items()
                        if r.get("ews_score", 0) > 0.7
                    ],
                    "pairs_computed": len(thom_results),
                },
            }

        self.scan_duration = time.time() - t0
        self.status = "running"

        # Publish
        if self._event_pub:
            try:
                self._event_pub.emit("NEXUS_UPDATE", {
                    "market_character": dominant,
                    "volatility_forecast": disp["prediction"],
                    "dispersion": disp["dispersion"],
                    "dispersion_trend": disp["trend"],
                    "echo_signal": echo_signal,
                    "interpretation": interpretation,
                    "gaussian_regime": gaussian_result.get("fused_regime"),
                    "gaussian_confidence": gaussian_result.get("fused_confidence"),
                    "gaussian_info_gain": gaussian_result.get("information_gain"),
                    "fleet_consonance": self.state.get("pythagorean", {}).get("fleet_consonance", 0),
                })
            except Exception:
                pass

            # Alert on state changes
            if dominant != self._prev_character and dominant != "QUIET":
                try:
                    self._event_pub.emit("NEXUS_ALERT", {
                        "alert": f"{dominant}_DETECTED",
                        "message": interpretation,
                    })
                except Exception:
                    pass

            if disp["prediction"] == "VOL_EXPANSION" and self._prev_vol_forecast != "VOL_EXPANSION":
                try:
                    self._event_pub.emit("NEXUS_ALERT", {
                        "alert": "VOL_EXPANSION_IMMINENT",
                        "dispersion": disp["dispersion"],
                        "message": "Fleet disagreement rising — volatility expansion predicted",
                    })
                except Exception:
                    pass

            if echo_signal and echo_signal.get("type") == "STRUCTURAL_LEAD":
                try:
                    self._event_pub.emit("NEXUS_ALERT", {
                        "alert": "TEMPORAL_ECHO",
                        "slow_source": echo_signal["slow_source"],
                        "lag": echo_signal["lag"],
                        "message": echo_signal["message"],
                    })
                except Exception:
                    pass

            # Newton: publish when force exceeds threshold
            for pair_name, nr in newton_results.items():
                try:
                    if abs(nr["force"]) > 1.0:
                        self._event_pub.emit("NEWTON_FORCE", {
                            "pair": pair_name,
                            "force": nr["force"],
                            "direction": nr["force_direction"],
                            "inertia": nr["inertia_score"],
                            "mass": nr["mass"],
                        })
                except Exception:
                    pass

                # Newton: reaction alerts
                for reaction in nr.get("reaction_pairs", [])[:3]:
                    try:
                        if reaction["force_transfer"] > 1.0:
                            self._event_pub.emit("NEWTON_REACTION", {
                                "pair": pair_name,
                                "trigger_pair": pair_name,
                                "reactor_pair": reaction["pair"],
                                "expected_lag": reaction["expected_lag_bars"],
                                "expected_direction": reaction["expected_direction"],
                                "force_transfer": reaction["force_transfer"],
                            })
                    except Exception:
                        pass

            # Euclid: publish when price is within 0.5% of strong level
            for pair_name, er in euclid_results.items():
                try:
                    if er["support_distance_pct"] < 0.5 and er["nearest_support"]:
                        self._event_pub.emit("EUCLID_LEVEL", {
                            "pair": pair_name,
                            "type": "SUPPORT_APPROACHING",
                            "level": er["nearest_support"]["price"],
                            "strength": er["nearest_support"]["strength"],
                            "distance_pct": er["support_distance_pct"],
                        })
                except Exception:
                    pass
                try:
                    if er["resistance_distance_pct"] < 0.5 and er["nearest_resistance"]:
                        self._event_pub.emit("EUCLID_LEVEL", {
                            "pair": pair_name,
                            "type": "RESISTANCE_APPROACHING",
                            "level": er["nearest_resistance"]["price"],
                            "strength": er["nearest_resistance"]["strength"],
                            "distance_pct": er["resistance_distance_pct"],
                        })
                except Exception:
                    pass

            # Einstein: publish when breakout potential is high
            for pair_name, ei in einstein_results.items():
                try:
                    if ei["breakout_potential"] > 10:
                        self._event_pub.emit("EINSTEIN_ENERGY", {
                            "pair": pair_name,
                            "energy": ei["energy"],
                            "breakout_potential": ei["breakout_potential"],
                            "time_dilation": ei["time_dilation"],
                            "interpretation": ei["interpretation"],
                        })
                except Exception:
                    pass

            # Schwarzschild: publish topology changes
            for pair_name, sh in schwarzschild_results.items():
                try:
                    if sh["market_topology"] in ("ESCAPE_TRAJECTORY", "CAPTURED",
                                                   "BINARY_SYSTEM"):
                        self._event_pub.emit("SCHWARZSCHILD_HORIZON", {
                            "pair": pair_name,
                            "topology": sh["market_topology"],
                            "nearest_horizon": sh["nearest_horizon"],
                            "momentum": sh["current_momentum"],
                            "singularities": sh["singularity_count"],
                        })
                except Exception:
                    pass

            # Council: MANIFOLD_WARNING when regime change probability > 0.6
            # Emit-on-change: only fire when a pair newly crosses into the
            # warning band, or its manifold classification changes while
            # still elevated. Otherwise a pair sitting above threshold for
            # minutes would re-emit identical warnings every 15s scan.
            if _council_loaded:
                for pair_name, ig in info_geo_results.items():
                    try:
                        prob = ig.get("regime_change_probability", 0)
                        interp = ig.get("interpretation", "")
                        above = prob > 0.6
                        prev_above, prev_interp = self._prev_manifold_state.get(
                            pair_name, (False, None))

                        if above and (not prev_above or interp != prev_interp):
                            self._event_pub.emit("MANIFOLD_WARNING", {
                                "pair": pair_name,
                                "regime_change_probability": prob,
                                "fisher_metric": ig["fisher_metric"],
                                "geodesic_velocity": ig["geodesic_velocity"],
                                # None, not 1.0. info_geometry does not compute
                                # this, so the old default published "perfectly
                                # reliable" on every MANIFOLD_WARNING the fleet
                                # has ever emitted.
                                "model_reliability": ig.get("model_reliability"),
                                "interpretation": interp,
                            })

                        self._prev_manifold_state[pair_name] = (above, interp)
                    except Exception:
                        pass

                # Council: CYCLE_DETECTED when cyclicality > 0.3
                for pair_name, topo in topology_results.items():
                    try:
                        if topo.get("cyclicality", 0) > 0.3:
                            self._event_pub.emit("CYCLE_DETECTED", {
                                "pair": pair_name,
                                "cyclicality": topo["cyclicality"],
                                "complexity": topo["complexity_score"],
                                "fragmentation": topo["fragmentation"],
                                "interpretation": topo.get("interpretation", ""),
                            })
                    except Exception:
                        pass

                # Council: QUANTUM_COLLAPSE — emit on STATE CHANGE, not on a
                # level check.
                #
                # This was `if superposition_entropy < 0.3`, which is a level,
                # not an event. Entropy sits near zero essentially always
                # (median ~0, 88% below 0.05), so it fired every scan for every
                # pair forever: measured 2026-08-06 at 2,383/hour across 10
                # pairs, one per pair every 16.6s like clockwork, and 100% of
                # emissions repeated the previous dominant_state — 0 actual
                # changes in 88 consecutive comparisons. A "collapse" event
                # that never reported a collapse.
                #
                # A collapse is a TRANSITION into a definite state. Emit only
                # when dominant_state actually differs from the last one seen
                # for that pair; the entropy gate still applies so we only
                # report transitions into a definite state.
                if not hasattr(self, "_last_quantum_state"):
                    self._last_quantum_state = {}
                for pair_name, qr in quantum_results.items():
                    try:
                        if qr.get("superposition_entropy", 1) >= 0.3:
                            continue
                        _state = qr["dominant_state"]
                        _prev = self._last_quantum_state.get(pair_name)
                        self._last_quantum_state[pair_name] = _state
                        if _prev == _state:
                            continue  # same state as last scan — not a collapse
                        self._event_pub.emit("QUANTUM_COLLAPSE", {
                            "pair": pair_name,
                            "dominant_state": _state,
                            "previous_state": _prev,
                            "confidence": qr["confidence"],
                            "superposition_entropy": qr["superposition_entropy"],
                            "interpretation": qr.get("interpretation", ""),
                        })
                    except Exception:
                        pass

                # Council: CAUSAL_FLOW when strong causal links exist
                for link in causal_summary.get("strongest_links", [])[:3]:
                    try:
                        if link.get("strength", 0) > 0.5:
                            self._event_pub.emit("CAUSAL_FLOW", {
                                "source": link["source"],
                                "target": link["target"],
                                "strength": link["strength"],
                                "lag": link["lag"],
                            })
                    except Exception:
                        pass

                # Boltzmann: BOOK_PHASE when order book enters BOILING or PLASMA
                for pair_name, br in boltzmann_results.items():
                    try:
                        if br.get("phase") in ("BOILING", "PLASMA"):
                            self._event_pub.emit("BOOK_PHASE", {
                                "pair": pair_name,
                                "phase": br["phase"],
                                "temperature": br["temperature"],
                                "entropy": br["entropy"],
                                "interpretation": br.get("interpretation", ""),
                            })
                    except Exception:
                        pass

                # Lorenz: CHAOS_STATE when attractor departure is significant
                for pair_name, lr in lorenz_results.items():
                    try:
                        if lr.get("attractor_departure", 0) > 0.5:
                            self._event_pub.emit("CHAOS_STATE", {
                                "pair": pair_name,
                                "lyapunov_exponent": lr["lyapunov_exponent"],
                                "attractor_departure": lr["attractor_departure"],
                                "predictability_horizon": lr["predictability_horizon"],
                                "interpretation": lr.get("interpretation", ""),
                            })
                    except Exception:
                        pass

                # Prigogine: STRUCTURE_FORMING when dissipative structure detected
                for pair_name, pr in prigogine_results.items():
                    try:
                        if pr.get("structure_formation_score", 0) > 0.4:
                            self._event_pub.emit("STRUCTURE_FORMING", {
                                "pair": pair_name,
                                "structure_type": pr["structure_type"],
                                "formation_score": pr["structure_formation_score"],
                                "bifurcation_type": pr["bifurcation_type"],
                                "entropy_production": pr["entropy_production_rate"],
                                "interpretation": pr.get("interpretation", ""),
                            })
                    except Exception:
                        pass

                # Thom: CATASTROPHE_WARNING when EWS score is high
                for pair_name, tr in thom_results.items():
                    try:
                        if tr.get("ews_score", 0) > 0.6:
                            self._event_pub.emit("CATASTROPHE_WARNING", {
                                "pair": pair_name,
                                "ews_score": tr["ews_score"],
                                "catastrophe_type": tr["catastrophe_type"],
                                "predicted_direction": tr["predicted_direction"],
                                "direction_confidence": tr["direction_confidence"],
                                "time_to_event": tr["estimated_time_to_event"],
                                "interpretation": tr.get("interpretation", ""),
                            })
                    except Exception:
                        pass

                # Shannon: SHANNON_ENTROPY when noise ratio is extreme
                if shannon_map.get("noise_ratio", 0) > 0.7:
                    try:
                        self._event_pub.emit("SHANNON_ENTROPY", {
                            "noise_ratio": shannon_map["noise_ratio"],
                            "signal_channels": shannon_map.get("signal_channels", 0),
                            "noise_channels": shannon_map.get("noise_channels", 0),
                            "total_capacity": shannon_map.get("total_capacity_bits", 0),
                            "interpretation": "Fleet signal dominated by noise — reduce model confidence",
                        })
                    except Exception:
                        pass

        self._prev_character = dominant
        self._prev_vol_forecast = disp["prediction"]

        self._log(f"Cycle {self.cycle}: {dominant} | {disp['prediction']} | "
                  f"disp={disp['dispersion']:.4f} {disp['trend']} | "
                  f"echoes={len(recent_echoes)} | "
                  f"N={len(newton_results)} E={len(euclid_results)} "
                  f"Ei={len(einstein_results)} Sch={len(schwarzschild_results)} "
                  f"IG={len(info_geo_results)} T={len(topology_results)} "
                  f"Q={len(quantum_results)} C={causal_summary.get('n_causal_links', 0)} "
                  f"Lo={len(lorenz_results)} Pr={len(prigogine_results)} "
                  f"Th={len(thom_results)} Bo={len(boltzmann_results)} "
                  f"Sh={shannon_map.get('n_series', 0)}s | "
                  f"{self.scan_duration:.1f}s")

    def snapshot(self):
        with self._lock:
            state = dict(self.state)
        return {
            "timestamp": time.time(),
            "bot_name": "NEXUS",
            "status": self.status,
            "cycle": self.cycle,
            **state,
            "scan_duration_s": round(self.scan_duration, 2),
            "logs": list(self._log_buf)[-15:],
        }


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

_engine = NexusEngine()


class NexusHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj):
        data = json.dumps(obj, default=str)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data.encode())

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/api/snapshot":
            self._send_json(_engine.snapshot())

        elif path == "/health":
            self._send_json({
                "status": "ok", "bot": "NEXUS", "port": PORT,
                "cycle": _engine.cycle,
                "market_character": _engine.state.get("market_character", "?"),
                "timestamp": time.time(),
            })

        else:
            self.send_error(404)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _scan_loop():
    time.sleep(10)
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
        ensure_port(PORT, "nexus")
        write_pidfile("nexus", PORT)
        atexit.register(cleanup_pidfile, "nexus")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    print("\n  NEXUS v1.0 — Market Interferometer")
    print(f"  Port: {PORT}")
    print(f"  Scan: every {SCAN_INTERVAL}s")
    print(f"  http://localhost:{PORT}/api/snapshot")
    print("  Press Ctrl+C to stop\n")

    threading.Thread(target=_scan_loop, daemon=True, name="NexusScan").start()

    server = ThreadedServer(("0.0.0.0", PORT), NexusHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  NEXUS stopped.")


if __name__ == "__main__":
    main()
