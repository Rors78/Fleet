"""
Fleet Intelligence Score — Synthesizes all engine outputs into actionable trading signals.
Polls Nexus snapshot + event bus to build per-pair intelligence scores.
"""

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
            # None, not 0.5 — a pair that reaches the aggregate with no
            # engine setting a confidence has not been assessed at 50%, it
            # has not been assessed. get_score() below was already hardened
            # to return None for the unscored case; this seed was missed, so
            # the two paths disagreed about the same field.
            "regime_confidence": None,
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
                # A MANIFOLD_WARNING exists because the model is objecting.
                # Defaulting its probability to 0 made "warning raised, value
                # unreadable" score identically to "no regime change".
                prob = w.get("regime_change_prob")
                if not isinstance(prob, (int, float)):
                    score["active_warnings"].append(
                        "MANIFOLD: warning raised but probability absent "
                        "— not scored")
                    break
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
                # This branch INCREASES position size (*1.4), so it must fire
                # only on a measured conviction. Defaulting a missing
                # confidence to 0.5 and then upsizing anyway would enlarge
                # real capital exposure on the strength of a number nobody
                # produced. No confidence -> no conviction bonus.
                _conf = item.get("confidence")
                if isinstance(_conf, (int, float)):
                    score["regime_confidence"] = min(1.0, _conf)
                    score["risk_multiplier"] *= 1.4  # high conviction moment
                    score["contributing_engines"].append("quantum_state")
                break
        else:
            for item in q.get("superposed", []):
                if item.get("pair") == pair:
                    # entropy > 0.9 is a RISK-REDUCING gate. Defaulting a
                    # missing entropy to 0.5 silently passes that gate, so an
                    # unmeasured pair skips the de-risking a genuinely
                    # uncertain one would get. Absent measurement must not
                    # read as "measured, and fine".
                    entropy = item.get("entropy")
                    if isinstance(entropy, (int, float)):
                        if entropy > 0.9:
                            score["regime_type"] = "UNCERTAIN"
                            score["risk_multiplier"] *= 0.7
                        score["contributing_engines"].append("quantum_state")
                    break

        # --- LORENZ: chaos analysis ---
        lo = d.get("lorenz", {})
        for item in lo.get("chaotic", []):
            if item.get("pair") == pair:
                # Both branches below REDUCE risk, so a missing value must not
                # be allowed to satisfy them. `predictability` defaulting to
                # 50 against a `< 15` test was the clearest case: an unmeasured
                # pair sailed through the low-predictability check exactly as
                # a genuinely predictable one would. Same for a departure
                # defaulting to 0 against `> 0.5`. Unmeasured means the gate
                # does not fire AND the engine does not claim to have
                # contributed.
                depart = item.get("attractor_departure")
                pred = item.get("predictability")
                _used = False
                if isinstance(depart, (int, float)) and depart > 0.5:
                    score["risk_multiplier"] *= 0.5
                    score["active_warnings"].append(
                        f"CHAOS: departure {depart:.2f}")
                if isinstance(pred, (int, float)) and pred < 15:
                    score["risk_multiplier"] *= 0.6
                    score["active_warnings"].append(
                        f"CHAOS: low predictability {pred:.0f} bars")
                if isinstance(depart, (int, float)) or isinstance(pred, (int, float)):
                    _used = True
                if _used:
                    score["contributing_engines"].append("lorenz")
                break

        # --- BOLTZMANN: order book phase ---
        bo = d.get("boltzmann", {})
        for item in bo.get("phases", []):
            if item.get("pair") == pair:
                # "UNKNOWN" fell through every elif INCLUDING the explicit
                # `LIQUID: pass` branch, so "we could not read the order
                # book" was handled by the identical code path as "the order
                # book is normal". Absent must not reach the same outcome as
                # measured-and-fine.
                phase = item.get("phase")
                if not isinstance(phase, str) or not phase or phase == "UNKNOWN":
                    break
                if phase in ("BOILING", "PLASMA"):
                    score["risk_multiplier"] *= 0.3
                    score["active_warnings"].append(f"BOOK: {phase} phase")
                elif phase == "GAS":
                    score["risk_multiplier"] *= 0.6
                    score["active_warnings"].append("BOOK: volatile GAS phase")
                elif phase == "SOLID":
                    score["regime_type"] = "RANGING"
                    _prev = score["regime_confidence"]
                    score["regime_confidence"] = (0.7 if _prev is None
                                                  else max(_prev, 0.7))
                elif phase == "LIQUID":
                    pass  # normal — no adjustment
                score["contributing_engines"].append("boltzmann")
                break

        # --- PRIGOGINE: dissipative structure ---
        pr = d.get("prigogine", {})
        for item in pr.get("structures_forming", []):
            if item.get("pair") == pair:
                # This branch UPSIZES 1.3x, so it must fire only on a real
                # structure type — the same discipline the quantum 1.4x
                # upsize gate already got. An absent type currently points
                # the safe way (no upsize), but an unvalidated string
                # deciding position size is the shape that goes wrong later.
                # (`score` was read into a variable that nothing used, same
                # dead-read as the lyapunov one removed on 2026-08-06.)
                stype = item.get("type")
                if not isinstance(stype, str) or not stype:
                    break
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
                # THE most aggressive de-risker in this file (0.3x), and a
                # Thom warning exists precisely BECAUSE a catastrophe signal
                # fired. Defaulting a missing ews_score to 0 produced
                # risk_multiplier 1.0 — indistinguishable from a pair with no
                # catastrophe signal at all. Against the largest live
                # position that was the difference between $66,710 and
                # $20,013 of exposure into a warning the engine was raising.
                ews = item.get("ews_score")
                if not isinstance(ews, (int, float)):
                    score["active_warnings"].append(
                        "CATASTROPHE: warning raised but ews_score absent "
                        "— not scored")
                    break
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
        # Fleet-wide, not per-pair: this gate is applied to EVERY pair in the
        # same recompute, so a dead Shannon engine silently un-scaled the
        # entire fleet at once. Smaller multiplier (0.7x) than the others but
        # by far the broadest blast radius. None means unmeasured, and 0 is a
        # legitimate reading (a perfectly clean signal), so the two must not
        # collapse into the same value.
        noise_ratio = sh.get("noise_ratio")
        if not isinstance(noise_ratio, (int, float)):
            noise_ratio = None
        if noise_ratio is None:
            pass  # unmeasured: no scaling, and shannon claims no credit
        elif noise_ratio > 0.7:
            score["risk_multiplier"] *= 0.7
            score["active_warnings"].append(
                f"NOISE: fleet signal {noise_ratio:.0%} noise")
            score["contributing_engines"].append("shannon")
        elif noise_ratio < 0.3 and sh.get("n_series", 0) > 0:
            # A boost applies to an EXISTING confidence. With none set, this
            # engine is the first to speak, so its own floor stands rather
            # than 0 + 0.15 (which would read as a near-zero assessment).
            _prev = score["regime_confidence"]
            score["regime_confidence"] = (0.15 if _prev is None
                                          else min(1.0, _prev + 0.15))
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
        score["regime_confidence"] = (
            round(score["regime_confidence"], 3)
            if score["regime_confidence"] is not None else None)

        return score

    # ── Public API ──

    def get_score(self, pair):
        """Get intelligence score for a single pair.

        An UNSCORED pair is marked `scored: False` rather than being handed a
        complete, confident-looking record. The old default returned
        risk_multiplier 1.0 and regime_confidence 0.5 for a pair no engine had
        ever evaluated — indistinguishable from a pair assessed as safe, and
        risk_multiplier gates real position size in PortfolioManager.reserve().

        `stale_s` is the age of the last successful poll. poll() swallows a
        dead Nexus, a non-200 and a malformed body into the same silent no-op
        and leaves the previous scores in place, so a consumer that does not
        check this is applying multipliers of unknown age to live capital.
        """
        with self._lock:
            _age = (time.time() - self._last_poll) if self._last_poll else None
            hit = self._pair_scores.get(pair)
            if hit is not None:
                out = dict(hit)
                out["scored"] = True
                out["stale_s"] = _age
                return out
            return {
                "pair": pair,
                "regime_confidence": None,
                "regime_type": "UNKNOWN",
                "trade_bias": 0.0,
                # None, not 1.0 — "no opinion" is not "full size approved".
                # The caller decides what to do with an unscored pair.
                "risk_multiplier": None,
                "active_warnings": [],
                "engine_agreement": 0.0,
                "contributing_engines": [],
                "scored": False,
                "stale_s": _age,
            }

    def get_all_scores(self):
        """Get all pair scores."""
        with self._lock:
            return dict(self._pair_scores)

    def summary(self):
        """Summary stats for the fleet."""
        with self._lock:
            scores = list(self._pair_scores.values())
        if not scores:
            # None, not 1.0. An average over ZERO scored pairs is not
            # "full size approved across the fleet" — it is no measurement at
            # all, and the dashboard paints this figure green at >0.7.
            return {"pairs_scored": 0, "avg_risk_multiplier": None,
                    "warnings_active": 0, "engines_contributing": 0}
        # Only real multipliers average. An unscored pair carries None (see
        # get_score), and summing that raises TypeError — the same shape that
        # took /api/expectancy down when _empty_stats started returning None.
        _mults = [s["risk_multiplier"] for s in scores
                  if isinstance(s.get("risk_multiplier"), (int, float))]
        return {
            "pairs_scored": len(scores),
            "avg_risk_multiplier": (round(sum(_mults) / len(_mults), 3)
                                    if _mults else None),
            "pairs_with_multiplier": len(_mults),
            "warnings_active": sum(len(s["active_warnings"]) for s in scores),
            "engines_contributing": len(set(
                e for s in scores for e in s["contributing_engines"])),
            "last_poll": self._last_poll,
        }
