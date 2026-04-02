"""
Fleet Intelligence Score — Synthesizes all engine outputs into actionable trading signals.
Polls Nexus snapshot + event bus to build per-pair intelligence scores.
"""

import math
import threading
import time

try:
    import requests as _req
except ImportError:
    _req = None

NEXUS_URL = "http://127.0.0.1:8082"
POLL_INTERVAL = 15  # match Nexus scan interval


class FleetIntelScore:
    """Synthesizes 13 engine outputs into per-pair trading intelligence."""

    def __init__(self, event_bus=None):
        self._event_bus = event_bus
        self._lock = threading.Lock()
        self._pair_scores = {}          # pair -> score dict
        self._nexus_data = {}           # latest Nexus snapshot
        self._last_poll = 0

    # ── Polling ──

    def poll(self):
        """Fetch latest Nexus snapshot and recompute all scores."""
        if not _req:
            return
        try:
            r = _req.get(f"{NEXUS_URL}/api/snapshot", timeout=5)
            if r.status_code == 200:
                self._nexus_data = r.json()
                self._recompute_all()
                self._last_poll = time.time()
        except Exception:
            pass

    def _recompute_all(self):
        """Recompute scores for all pairs from latest engine data."""
        d = self._nexus_data
        if not d:
            return

        # Collect all pairs mentioned across engines
        pairs = set()
        for eng_key in ("info_geometry", "topology", "quantum", "lorenz",
                        "prigogine", "thom", "boltzmann"):
            eng = d.get(eng_key, {})
            for list_key in eng:
                items = eng[list_key]
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and "pair" in item:
                            pairs.add(item["pair"])

        new_scores = {}
        for pair in pairs:
            new_scores[pair] = self._compute_pair(pair, d)

        with self._lock:
            self._pair_scores = new_scores

    def _compute_pair(self, pair, d):
        """Compute intelligence score for a single pair."""
        score = {
            "pair": pair,
            "regime_confidence": 0.5,
            "regime_type": "UNKNOWN",
            "trade_bias": 0.0,
            "risk_multiplier": 1.0,
            "active_warnings": [],
            "engine_agreement": 0.0,
            "contributing_engines": [],
            "updated_at": time.time(),
        }

        # --- INFO GEOMETRY: regime change probability ---
        ig = d.get("info_geometry", {})
        for w in ig.get("manifold_warnings", []):
            if w.get("pair") == pair:
                prob = w.get("regime_change_prob", 0)
                if prob > 0.7:
                    score["risk_multiplier"] *= max(0.3, 1.0 - prob * 0.7)
                    score["active_warnings"].append(
                        f"MANIFOLD: regime change prob {prob:.2f}")
                elif prob > 0.4:
                    score["risk_multiplier"] *= 0.8
                score["contributing_engines"].append("info_geometry")
                break

        # --- TOPOLOGY: cyclicality → ranging signal ---
        topo = d.get("topology", {})
        for c in topo.get("cyclic_pairs", []):
            if c.get("pair") == pair:
                cyc = c.get("cyclicality", 0)
                if cyc > 0.3:
                    score["regime_type"] = "RANGING"
                    score["regime_confidence"] = min(1.0, 0.5 + cyc)
                score["contributing_engines"].append("topology")
                break

        # --- QUANTUM STATE: superposition vs collapsed ---
        q = d.get("quantum", {})
        for item in q.get("collapsed", []):
            if item.get("pair") == pair:
                score["regime_confidence"] = min(1.0,
                                                  item.get("confidence", 0.5))
                score["risk_multiplier"] *= 1.4  # high conviction moment
                score["contributing_engines"].append("quantum_state")
                break
        else:
            for item in q.get("superposed", []):
                if item.get("pair") == pair:
                    entropy = item.get("entropy", 0.5)
                    if entropy > 0.9:
                        score["regime_type"] = "UNCERTAIN"
                        score["risk_multiplier"] *= 0.7
                    score["contributing_engines"].append("quantum_state")
                    break

        # --- LORENZ: chaos analysis ---
        lo = d.get("lorenz", {})
        for item in lo.get("chaotic", []):
            if item.get("pair") == pair:
                lyap = item.get("lyapunov", 0)
                depart = item.get("attractor_departure", 0)
                pred = item.get("predictability", 50)
                if depart > 0.5:
                    score["risk_multiplier"] *= 0.5
                    score["active_warnings"].append(
                        f"CHAOS: departure {depart:.2f}")
                if pred < 15:
                    score["risk_multiplier"] *= 0.6
                    score["active_warnings"].append(
                        f"CHAOS: low predictability {pred:.0f} bars")
                score["contributing_engines"].append("lorenz")
                break

        # --- BOLTZMANN: order book phase ---
        bo = d.get("boltzmann", {})
        for item in bo.get("phases", []):
            if item.get("pair") == pair:
                phase = item.get("phase", "UNKNOWN")
                if phase in ("BOILING", "PLASMA"):
                    score["risk_multiplier"] *= 0.3
                    score["active_warnings"].append(f"BOOK: {phase} phase")
                elif phase == "GAS":
                    score["risk_multiplier"] *= 0.6
                    score["active_warnings"].append("BOOK: volatile GAS phase")
                elif phase == "SOLID":
                    score["regime_type"] = "RANGING"
                    score["regime_confidence"] = max(score["regime_confidence"],
                                                     0.7)
                elif phase == "LIQUID":
                    pass  # normal — no adjustment
                score["contributing_engines"].append("boltzmann")
                break

        # --- PRIGOGINE: dissipative structure ---
        pr = d.get("prigogine", {})
        for item in pr.get("structures_forming", []):
            if item.get("pair") == pair:
                stype = item.get("type", "")
                ssc = item.get("score", 0)
                if "TREND" in stype.upper():
                    score["regime_type"] = "TRENDING"
                    score["risk_multiplier"] *= 1.3  # opportunity
                elif "CYCLE" in stype.upper():
                    score["regime_type"] = "RANGING"
                score["contributing_engines"].append("prigogine")
                break

        # --- THOM: catastrophe early warning ---
        th = d.get("thom", {})
        for item in th.get("warnings", []):
            if item.get("pair") == pair:
                ews = item.get("ews_score", 0)
                if ews > 0.7:
                    score["risk_multiplier"] *= 0.3
                    score["active_warnings"].append(
                        f"CATASTROPHE: EWS {ews:.2f}, type={item.get('catastrophe_type')}")
                elif ews > 0.4:
                    score["risk_multiplier"] *= 0.6
                    score["active_warnings"].append(
                        f"CATASTROPHE: elevated EWS {ews:.2f}")
                score["contributing_engines"].append("thom")
                break
        for item in th.get("imminent", []):
            if item.get("pair") == pair:
                score["risk_multiplier"] *= 0.2
                score["active_warnings"].append(
                    f"CATASTROPHE IMMINENT: {item.get('catastrophe_type')} "
                    f"{item.get('direction')}")
                if "thom" not in score["contributing_engines"]:
                    score["contributing_engines"].append("thom")
                break

        # --- SHANNON: fleet-wide noise ratio ---
        sh = d.get("shannon", {})
        noise_ratio = sh.get("noise_ratio", 0)
        if noise_ratio > 0.7:
            score["risk_multiplier"] *= 0.7
            score["active_warnings"].append(
                f"NOISE: fleet signal {noise_ratio:.0%} noise")
            score["contributing_engines"].append("shannon")
        elif noise_ratio < 0.3 and sh.get("n_series", 0) > 0:
            score["regime_confidence"] = min(1.0,
                                              score["regime_confidence"] + 0.15)
            score["contributing_engines"].append("shannon")

        # --- CAUSAL FLOW: directional bias from causal links ---
        cf = d.get("causal_flow", {})
        for link in cf.get("strongest_links", []):
            src = link.get("source", "")
            tgt = link.get("target", "")
            # Extract pair from source/target names like "BTC/USD_price"
            if pair.replace("/", "") in src.replace("/", "") or \
               pair.replace("/", "") in tgt.replace("/", ""):
                strength = link.get("strength", 0)
                if strength > 0.5:
                    # Net flow direction indicates bias
                    nf = link.get("net_flow", 0)
                    score["trade_bias"] += max(-0.3, min(0.3, nf))
                    score["contributing_engines"].append("causal_flow")
                break

        # --- AGGREGATE ---
        n_engines = len(set(score["contributing_engines"]))
        n_warnings = len(score["active_warnings"])
        if n_engines > 0:
            score["engine_agreement"] = round(
                1.0 - min(1.0, n_warnings / max(n_engines, 1)), 3)

        # Clamp
        score["risk_multiplier"] = round(
            max(0.0, min(2.0, score["risk_multiplier"])), 3)
        score["trade_bias"] = round(
            max(-1.0, min(1.0, score["trade_bias"])), 3)
        score["regime_confidence"] = round(score["regime_confidence"], 3)

        return score

    # ── Public API ──

    def get_score(self, pair):
        """Get intelligence score for a single pair."""
        with self._lock:
            return self._pair_scores.get(pair, {
                "pair": pair,
                "regime_confidence": 0.5,
                "regime_type": "UNKNOWN",
                "trade_bias": 0.0,
                "risk_multiplier": 1.0,
                "active_warnings": [],
                "engine_agreement": 0.0,
                "contributing_engines": [],
            })

    def get_all_scores(self):
        """Get all pair scores."""
        with self._lock:
            return dict(self._pair_scores)

    def summary(self):
        """Summary stats for the fleet."""
        with self._lock:
            scores = list(self._pair_scores.values())
        if not scores:
            return {"pairs_scored": 0, "avg_risk_multiplier": 1.0,
                    "warnings_active": 0, "engines_contributing": 0}
        return {
            "pairs_scored": len(scores),
            "avg_risk_multiplier": round(
                sum(s["risk_multiplier"] for s in scores) / len(scores), 3),
            "warnings_active": sum(len(s["active_warnings"]) for s in scores),
            "engines_contributing": len(set(
                e for s in scores for e in s["contributing_engines"])),
            "last_poll": self._last_poll,
        }
