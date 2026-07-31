#!/usr/bin/env python3
"""
PHITEX — Phase-Thermodynamic Extractor
========================================
Bot #10 in the fleet. Port 8078. Color: #e040fb (magenta).

Computes thermodynamic state variables from OHLCV data:
  T(t)   — Market Temperature (permutation entropy)
  M(t)   — Magnetization (BVC directional consensus)
  chi(t) — Magnetic Susceptibility (dM/dT)
  C(t)   — Specific Heat (dVar/dT)
  FCI(t) — Fisher Criticality Index

Composite: PHITEX = chi_norm * C_norm * (1 - FCI_norm)

Usage: python phitex.py
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

try:
    from fleet_config import is_blacklisted as _is_blacklisted
except ImportError:
    _is_blacklisted = lambda pair: False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 8078
COMMAND_CENTER = "http://127.0.0.1:9000"
SCAN_INTERVAL = 60          # seconds between full scans
OHLC_INTERVAL = 60          # 1h candles
OHLC_LIMIT = 300            # 300 bars of history

# State variable parameters
PE_WINDOW = 60              # permutation entropy lookback
PE_M = 4                    # embedding dimension (4! = 24 ordinal patterns)
PE_TAU = 1                  # delay
MAG_WINDOW = 20             # magnetization EMA window
CHI_WINDOW = 10             # susceptibility dM/dT window
C_VAR_WINDOW = 20           # variance window for specific heat
C_CHI_WINDOW = 10           # dVar/dT window
FCI_WINDOW = 30             # Fisher Information lookback
FCI_BINS = 10               # discretization bins
NORM_LOOKBACK = 200         # percentile normalization lookback
EWMA_ALPHA = 0.1            # smoothing for chi and C

# Thresholds
CRITICAL = 0.8
PRE_CRITICAL = 0.5
TRANSITIONING = 0.2


# ---------------------------------------------------------------------------
# Thermodynamic State Variables
# ---------------------------------------------------------------------------

def permutation_entropy(returns, m=PE_M, tau=PE_TAU):
    """Permutation entropy of a return series. Returns [0, 1]."""
    n = len(returns)
    needed = m + (m - 1) * (tau - 1)
    if n < needed:
        return 1.0  # insufficient data, assume max entropy

    # Count ordinal patterns
    patterns = {}
    for i in range(n - (m - 1) * tau):
        window = tuple(returns[i + j * tau] for j in range(m))
        pattern = tuple(sorted(range(m), key=lambda k: window[k]))
        patterns[pattern] = patterns.get(pattern, 0) + 1

    # Shannon entropy normalized by max
    total = sum(patterns.values())
    entropy = 0.0
    for count in patterns.values():
        p = count / total
        if p > 0:
            entropy -= p * math.log2(p)

    max_entropy = math.log2(math.factorial(m))
    return entropy / max_entropy if max_entropy > 0 else 1.0


def magnetization(closes, volumes, highs, lows, window=MAG_WINDOW):
    """Signed BVC magnetization. Returns [-1, 1]."""
    n = len(closes)
    if n < 2 or n < window:
        return 0.0

    m_values = []
    for i in range(1, n):
        # BVC: estimate buy fraction from intra-bar price position
        bar_range = highs[i] - lows[i]
        if bar_range > 0:
            z = (closes[i] - (highs[i] + lows[i]) / 2) / bar_range
        else:
            z = 0.0

        # Normal CDF approximation for buy volume fraction
        buy_frac = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
        net = 2.0 * buy_frac - 1.0  # [-1, 1]
        m_values.append(net * volumes[i])

    if not m_values:
        return 0.0

    # EMA of volume-weighted net flow
    alpha = 2.0 / (window + 1)
    ema = m_values[0]
    for v in m_values[1:]:
        ema = alpha * v + (1.0 - alpha) * ema

    # Normalize by average volume
    avg_vol = sum(volumes[-window:]) / window if window > 0 else 1.0
    return max(-1.0, min(1.0, ema / (avg_vol + 1e-10)))


def susceptibility(M_series, T_series, window=CHI_WINDOW):
    """chi = |dM/dT| — sensitivity of magnetization to entropy changes."""
    if len(M_series) < window + 1 or len(T_series) < window + 1:
        return 0.0

    chi_values = []
    for i in range(-window, 0):
        dM = abs(M_series[i] - M_series[i - 1])
        dT = abs(T_series[i] - T_series[i - 1])
        if dT > 1e-6:
            chi_values.append(dM / dT)

    return sum(chi_values) / len(chi_values) if chi_values else 0.0


def specific_heat(returns, T_series, var_window=C_VAR_WINDOW, chi_window=C_CHI_WINDOW):
    """C = |dVar/dT| — volatility-entropy coupling."""
    if len(returns) < var_window + chi_window:
        return 0.0

    # Rolling realized variance
    var_series = []
    for i in range(var_window, len(returns) + 1):
        w = returns[i - var_window:i]
        var_series.append(sum(r ** 2 for r in w) / var_window)

    if len(var_series) < chi_window + 1 or len(T_series) < chi_window + 1:
        return 0.0

    c_values = []
    for i in range(-chi_window, 0):
        dVar = abs(var_series[i] - var_series[i - 1])
        dT = abs(T_series[i] - T_series[i - 1])
        if dT > 1e-6:
            c_values.append(dVar / dT)

    return sum(c_values) / len(c_values) if c_values else 0.0


def fisher_information(T_series, window=FCI_WINDOW, bins=FCI_BINS):
    """Fisher Information of the temperature series. High = stable regime."""
    if len(T_series) < window:
        return 1.0  # assume stable

    recent = T_series[-window:]
    min_t, max_t = min(recent), max(recent)
    if max_t - min_t < 1e-10:
        return 1.0  # constant = perfectly stable

    bin_width = (max_t - min_t) / bins
    histogram = [0] * bins
    for t in recent:
        idx = min(int((t - min_t) / bin_width), bins - 1)
        histogram[idx] += 1

    total = sum(histogram)
    probs = [h / total for h in histogram]

    fi = 0.0
    for i in range(len(probs) - 1):
        if probs[i] > 1e-10:
            fi += (probs[i + 1] - probs[i]) ** 2 / probs[i]

    return fi


# ---------------------------------------------------------------------------
# Percentile Normalization + EWMA Smoothing
# ---------------------------------------------------------------------------

def percentile_rank(value, history):
    """Rank value within history, return [0, 1]."""
    if not history:
        return 0.5
    below = sum(1 for h in history if h < value)
    return below / len(history)


class EWMASmoother:
    """Exponential weighted moving average with alpha."""
    def __init__(self, alpha=EWMA_ALPHA):
        self.alpha = alpha
        self.value = None

    def update(self, x):
        if self.value is None:
            self.value = x
        else:
            self.value = self.alpha * x + (1.0 - self.alpha) * self.value
        return self.value


# ---------------------------------------------------------------------------
# Per-Pair ΦTEX Computer
# ---------------------------------------------------------------------------

class PairPhiTex:
    """Computes and tracks all 5 state variables for one pair."""

    def __init__(self, pair):
        self.pair = pair
        # Raw history for normalization
        self.T_history = deque(maxlen=NORM_LOOKBACK)
        self.M_history = deque(maxlen=NORM_LOOKBACK)
        self.chi_history = deque(maxlen=NORM_LOOKBACK)
        self.C_history = deque(maxlen=NORM_LOOKBACK)
        self.FCI_history = deque(maxlen=NORM_LOOKBACK)
        # Rolling series for cross-derivatives
        self.T_series = deque(maxlen=NORM_LOOKBACK)
        self.M_series = deque(maxlen=NORM_LOOKBACK)
        # Smoothers
        self.chi_smoother = EWMASmoother()
        self.C_smoother = EWMASmoother()
        # Latest values
        self.T = 0.0
        self.M = 0.0
        self.chi = 0.0
        self.C = 0.0
        self.FCI = 1.0
        self.phi_tex = 0.0
        self.chi_norm = 0.0
        self.C_norm = 0.0
        self.FCI_norm = 0.5

    def compute(self, closes, highs, lows, volumes):
        """Walk-forward computation: compute state variables at each bar
        position to build normalization history from the candle history itself.
        Only recomputes from scratch when history buffers are empty (first call).
        """
        n = len(closes)
        if n < PE_WINDOW + 20:
            return 0.0

        # Returns for full series
        returns = [(closes[i] - closes[i - 1]) / closes[i - 1]
                    if closes[i - 1] != 0 else 0.0
                    for i in range(1, n)]

        need_bootstrap = len(self.T_history) < 20

        if need_bootstrap:
            # Walk forward through the candle history, computing T and M
            # at each bar to build normalization history immediately
            self.T_history.clear()
            self.M_history.clear()
            self.chi_history.clear()
            self.C_history.clear()
            self.FCI_history.clear()
            self.T_series.clear()
            self.M_series.clear()
            self.chi_smoother = EWMASmoother()
            self.C_smoother = EWMASmoother()

            start = PE_WINDOW + MAG_WINDOW  # need enough bars for both T and M
            step = max(1, (n - start) // NORM_LOOKBACK)  # sample evenly

            for end in range(start, n, step):
                ret_slice = returns[:end]
                T_val = permutation_entropy(ret_slice[-PE_WINDOW:]) if len(ret_slice) >= PE_WINDOW else 1.0
                M_val = magnetization(closes[:end + 1], volumes[:end + 1], highs[:end + 1], lows[:end + 1])

                self.T_series.append(T_val)
                self.M_series.append(M_val)
                self.T_history.append(T_val)
                self.M_history.append(abs(M_val))

                # Cross-derivatives need series history
                if len(self.T_series) > CHI_WINDOW:
                    raw_chi = susceptibility(list(self.M_series), list(self.T_series))
                    chi_val = self.chi_smoother.update(raw_chi)
                    self.chi_history.append(chi_val)

                    raw_C = specific_heat(ret_slice, list(self.T_series))
                    C_val = self.C_smoother.update(raw_C)
                    self.C_history.append(C_val)

                    fci_val = fisher_information(list(self.T_series))
                    self.FCI_history.append(fci_val)

        # Final computation at the latest bar (always run)
        self.T = permutation_entropy(returns[-PE_WINDOW:]) if len(returns) >= PE_WINDOW else 1.0
        self.M = magnetization(closes, volumes, highs, lows)
        self.T_series.append(self.T)
        self.M_series.append(self.M)
        self.T_history.append(self.T)
        self.M_history.append(abs(self.M))

        raw_chi = susceptibility(list(self.M_series), list(self.T_series))
        self.chi = self.chi_smoother.update(raw_chi)
        self.chi_history.append(self.chi)

        raw_C = specific_heat(returns, list(self.T_series))
        self.C = self.C_smoother.update(raw_C)
        self.C_history.append(self.C)

        self.FCI = fisher_information(list(self.T_series))
        self.FCI_history.append(self.FCI)

        # Percentile normalization (now has 20+ data points from bootstrap)
        self.chi_norm = percentile_rank(self.chi, list(self.chi_history))
        self.C_norm = percentile_rank(self.C, list(self.C_history))
        self.FCI_norm = percentile_rank(self.FCI, list(self.FCI_history))

        # Composite ΦTEX score
        self.phi_tex = self.chi_norm * self.C_norm * (1.0 - self.FCI_norm)

        return self.phi_tex

    def regime_label(self):
        if self.phi_tex >= CRITICAL:
            return "CRITICAL"
        elif self.phi_tex >= PRE_CRITICAL:
            return "PRE_CRITICAL"
        elif self.phi_tex >= TRANSITIONING:
            return "TRANSITIONING"
        return "EQUILIBRIUM"

    def direction(self):
        if self.M > 0.1:
            return "LONG"
        elif self.M < -0.1:
            return "SHORT"
        return "NEUTRAL"

    def to_dict(self):
        return {
            "temperature": round(self.T, 4),
            "magnetization": round(self.M, 4),
            "susceptibility": round(self.chi, 6),
            "specific_heat": round(self.C, 6),
            "fisher_info": round(self.FCI, 4),
            "chi_norm": round(self.chi_norm, 4),
            "C_norm": round(self.C_norm, 4),
            "FCI_norm": round(self.FCI_norm, 4),
            "phi_tex": round(self.phi_tex, 4),
            "direction": self.direction(),
            "regime": self.regime_label(),
            "blacklisted": _is_blacklisted(self.pair),
        }


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class PhiTexScanner:
    """Scans all universe pairs and computes ΦTEX."""

    def __init__(self):
        self.pairs = {}            # pair_name -> PairPhiTex
        self.fleet_score = 0.0
        self.fleet_direction = "NEUTRAL"
        self.alerts = []
        self.cycle = 0
        self.scan_duration = 0.0
        self.status = "initializing"
        self._lock = threading.Lock()
        self._prev_scores = {}     # pair -> previous phi_tex for threshold crossing
        self._event_pub = EventPublisher(COMMAND_CENTER, "phitex") if EventPublisher else None
        self._log_buf = deque(maxlen=100)

    def _log(self, msg):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._log_buf.append(f"[{ts}] {msg}")

    def _fetch_ohlc(self, pair):
        """Fetch OHLC from Command Center, fallback to Kraken directly."""
        # Try Command Center market data layer first
        try:
            resp = requests.get(f"{COMMAND_CENTER}/api/market/ohlc",
                                params={"pair": pair, "interval": OHLC_INTERVAL, "limit": OHLC_LIMIT},
                                timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                candles = data.get("candles", [])
                if candles:
                    return candles
        except Exception:
            pass

        # Fallback: fetch directly from Kraken
        try:
            resp = requests.get("https://api.kraken.com/0/public/OHLC",
                                params={"pair": pair, "interval": OHLC_INTERVAL},
                                timeout=10)
            data = resp.json()
            if data.get("error"):
                return None
            for k, v in data.get("result", {}).items():
                if k != "last" and isinstance(v, list):
                    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                             float(r[4]), float(r[6]), int(r[7])] for r in v]
            return None
        except Exception:
            return None

    def _fetch_universe(self):
        """Get universe pairs from Command Center, fallback to hardcoded."""
        try:
            resp = requests.get(f"{COMMAND_CENTER}/api/universe", timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                pairs = [p["display"] for p in data.get("pairs", [])]
                if pairs:
                    return pairs
        except Exception:
            pass

        # Fallback: hardcoded top 20
        return ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
                "DOGE/USD", "DOT/USD", "AVAX/USD", "LINK/USD", "UNI/USD",
                "ATOM/USD", "LTC/USD", "BCH/USD", "XLM/USD", "ALGO/USD",
                "FIL/USD", "APT/USD", "ARB/USD", "OP/USD", "NEAR/USD"]

    def scan(self):
        """Run one full scan cycle."""
        self.cycle += 1
        t0 = time.time()
        self._log(f"Cycle {self.cycle}: scanning...")

        universe = self._fetch_universe()
        if not universe:
            self._log("No universe pairs available")
            self.status = "waiting"
            return

        self.status = "scanning"
        alerts = []

        for pair in universe:
            candles = self._fetch_ohlc(pair)
            if not candles or len(candles) < PE_WINDOW + 20:
                continue

            # Extract arrays
            closes = [c[4] for c in candles]
            highs = [c[2] for c in candles]
            lows = [c[3] for c in candles]
            volumes = [c[5] for c in candles]

            # Get or create pair computer
            if pair not in self.pairs:
                self.pairs[pair] = PairPhiTex(pair)
            pp = self.pairs[pair]

            prev_score = self._prev_scores.get(pair, 0.0)
            score = pp.compute(closes, highs, lows, volumes)
            self._prev_scores[pair] = score

            # Check for threshold crossings
            if score >= CRITICAL and prev_score < CRITICAL:
                alert = {
                    "pair": pair,
                    "phi_tex": round(score, 4),
                    "direction": pp.direction(),
                    "chi": round(pp.chi, 6),
                    "C": round(pp.C, 6),
                    "FCI": round(pp.FCI, 4),
                    "crossed": "CRITICAL",
                }
                alerts.append(alert)
                self._log(f"CRITICAL: {pair} PHITEX={score:.4f} dir={pp.direction()}")
                if self._event_pub:
                    try:
                        self._event_pub.emit("PHASE_TRANSITION", {
                            "pair": pair,
                            "phi_tex": round(score, 4),
                            "direction": pp.direction(),
                            "susceptibility": round(pp.chi, 6),
                            "specific_heat": round(pp.C, 6),
                            "fisher_info": round(pp.FCI, 4),
                        })
                    except Exception:
                        pass

            elif score >= PRE_CRITICAL and prev_score < PRE_CRITICAL:
                self._log(f"PRE_CRITICAL: {pair} PHITEX={score:.4f}")

        # Fleet-wide composite
        active = [pp for pp in self.pairs.values() if pp.phi_tex > 0]
        if active:
            self.fleet_score = sum(pp.phi_tex for pp in active) / len(active)
            avg_M = sum(pp.M for pp in active) / len(active)
            if avg_M > 0.1:
                self.fleet_direction = "LONG"
            elif avg_M < -0.1:
                self.fleet_direction = "SHORT"
            else:
                self.fleet_direction = "NEUTRAL"

        with self._lock:
            self.alerts = alerts

        self.scan_duration = time.time() - t0
        self.status = "running"

        # Publish fleet update
        critical = [p for p, pp in self.pairs.items() if pp.phi_tex >= CRITICAL]
        pre_crit = [p for p, pp in self.pairs.items() if PRE_CRITICAL <= pp.phi_tex < CRITICAL]

        if self._event_pub:
            try:
                self._event_pub.emit("PHITEX_UPDATE", {
                    "fleet_score": round(self.fleet_score, 4),
                    "fleet_direction": self.fleet_direction,
                    "critical_pairs": critical,
                    "pre_critical_pairs": pre_crit,
                    "pairs_scanned": len(self.pairs),
                })
            except Exception:
                pass

        self._log(f"Cycle {self.cycle} complete: {len(self.pairs)} pairs, "
                  f"fleet={self.fleet_score:.4f}, {len(critical)} critical, "
                  f"{len(pre_crit)} pre-critical, {self.scan_duration:.1f}s")

    def snapshot(self):
        """Build JSON snapshot for API."""
        pairs_data = {}
        for pair, pp in sorted(self.pairs.items()):
            pairs_data[pair] = pp.to_dict()

        with self._lock:
            alerts = list(self.alerts)

        return {
            "timestamp": time.time(),
            "bot_name": "PHITEX",
            "status": self.status,
            "cycle": self.cycle,
            "fleet_score": round(self.fleet_score, 4),
            "fleet_direction": self.fleet_direction,
            "fleet_regime": (
                "CRITICAL" if self.fleet_score >= CRITICAL
                else "PRE_CRITICAL" if self.fleet_score >= PRE_CRITICAL
                else "TRANSITIONING" if self.fleet_score >= TRANSITIONING
                else "EQUILIBRIUM"
            ),
            "pairs": pairs_data,
            "alerts": alerts,
            "scan_duration_s": round(self.scan_duration, 2),
            "logs": list(self._log_buf)[-20:],
        }


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

_scanner = PhiTexScanner()


class PhiTexHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/snapshot":
            data = json.dumps(_scanner.snapshot(), default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))

        elif path == "/health":
            data = json.dumps({
                "status": "ok", "bot": "PHITEX", "port": PORT,
                "cycle": _scanner.cycle, "timestamp": time.time(),
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))

        elif path == "/" or path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    self.wfile.write(f.read().encode("utf-8"))
            except FileNotFoundError:
                self.wfile.write(b"<html><body><h1>PHITEX</h1><p>dashboard.html not found</p></body></html>")

        else:
            self.send_error(404)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _scan_loop():
    """Background scanning thread."""
    # Wait for Command Center to be ready
    time.sleep(10)
    while True:
        try:
            _scanner.scan()
        except Exception as e:
            _scanner._log(f"ERROR: {e}")
        time.sleep(SCAN_INTERVAL)


def main():
    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        ensure_port(PORT, "phitex")
        write_pidfile("phitex", PORT)
        atexit.register(cleanup_pidfile, "phitex")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    print()
    print("  +==========================================+")
    print("  |         PHITEX v1.0                      |")
    print("  |  Phase-Thermodynamic Extractor           |")
    print("  +==========================================+")
    print(f"  |  Dashboard:  http://localhost:{PORT}       |")
    print(f"  |  API:        http://localhost:{PORT}/api/snapshot")
    print(f"  |  Scan:       every {SCAN_INTERVAL}s               |")
    print("  +==========================================+")
    print()

    # Start scanner thread
    t = threading.Thread(target=_scan_loop, daemon=True, name="PhiTexScanner")
    t.start()

    # Start HTTP server
    # ThreadingHTTPServer: the plain HTTPServer serves one request at a time,
    # so while this bot computes, its port stops answering and Command Center's
    # health check reports it DOWN even though it is healthy.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), PhiTexHandler)
    server.daemon_threads = True
    print(f"  Listening on http://localhost:{PORT}")
    print("  Press Ctrl+C to stop")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  PHITEX stopped.")


if __name__ == "__main__":
    main()
