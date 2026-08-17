"""
Bus Listener — reads events from Command Center's event bus.
Trading bots import this to react to fleet intelligence.

Usage:
    from bus_listener import BusListener
    _bus = BusListener()

    whales = _bus.whale_alerts(pair="BTC/USD", max_age=300)
    phitex = _bus.phitex_status(pair="BTC/USD")
    regime = _bus.aegis_regime()
    convergence = _bus.convergent_signals(pair="BTC/USD", max_age=60)
    emergency = _bus.emergency_active()
"""

import json
import logging
import math
import threading
import time
import urllib.request as urlreq

log = logging.getLogger("bus_listener")

_LN2 = math.log(2)

# Signal half-lives (seconds) — how long each signal type stays relevant
_HALF_LIVES = {
    'WHALE_ALERT': 600, 'SIGNAL': 900, 'HIGH_CONVICTION': 1200,
    'FORECAST_CONVICTION': 1800, 'NEWTON_FORCE': 600, 'EUCLID_LEVEL': 3600,
    'EINSTEIN_ENERGY': 1200, 'SCHWARZSCHILD_HORIZON': 1800,
    'REGIME_CHANGE': 7200, 'AEGIS_UPDATE': 3600, 'PHITEX_UPDATE': 1800,
    'CHRONOS_ALERT': 3600, 'SENTIMENT_EXTREME': 3600, 'ATTENTION': 600,
    'MANIFOLD_WARNING': 7200, 'CYCLE_DETECTED': 14400,
    'QUANTUM_COLLAPSE': 3600, 'CATASTROPHE_WARNING': 3600,
    'CHAOS_STATE': 7200, 'BOOK_PHASE': 600, 'STRUCTURE_FORMING': 3600,
}


def _decay_strength(event, half_life=None):
    """Exponential decay: strength = exp(-ln(2) * age / half_life).

    Returns 0.0 -- fully decayed -- when the event's age cannot be
    determined. This used to return 1.0, MAXIMUM freshness, which is the
    exact inverse of the correct reading: an event of unknown age was
    treated as if it had just arrived.

    That is not academic. whale_tier() below gates on `_decay_strength(best)
    < 0.2`, so a timestamp-less WHALE_ALERT sailed through at full strength
    and was delivered as current to every consumer of this module
    (Arbitrageur, Confluence, Contrarian, Gridzilla, NexusBrain, Rubberband,
    TurtleSue). event_bus.publish uses setdefault, which PRESERVES an
    explicit ts of 0, and disk-replayed events carry no guarantee at all.

    A negative age means a clock disagreement, not freshness, so it decays
    too rather than reading as brand new.
    """
    ts = event.get("ts", event.get("timestamp", 0))
    if not isinstance(ts, (int, float)) or ts <= 0:
        return 0.0
    etype = event.get("type", "")
    hl = half_life or _HALF_LIVES.get(etype, 900)
    age = time.time() - ts
    if age < 0:
        # Future-stamped: a clock skew or a corrupt record. Treat as
        # unusable rather than maximally fresh.
        return 0.0
    return math.exp(-_LN2 * age / hl)


class BusListener:
    CC_URL = "http://localhost:9000"
    POLL_INTERVAL = 10
    _MAX_BACKOFF = 120

    def __init__(self, cc_url=None, poll_interval=None):
        self.cc_url = cc_url or self.CC_URL
        self.poll_interval = poll_interval or self.POLL_INTERVAL
        self._events = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self):
        consecutive_fails = 0
        while True:
            try:
                req = urlreq.Request(f"{self.cc_url}/api/events/recent?n=100")
                resp = urlreq.urlopen(req, timeout=5)
                events = json.loads(resp.read())
                with self._lock:
                    self._events = events if isinstance(events, list) else []
                consecutive_fails = 0
            except Exception:
                consecutive_fails += 1
                if consecutive_fails <= 3:
                    log.warning("Bus poll failed (attempt %d)", consecutive_fails)
                elif consecutive_fails % 10 == 0:
                    log.warning("Bus poll still failing (attempt %d)", consecutive_fails)

            # Exponential backoff: poll_interval * 2^(fails-1), capped
            if consecutive_fails > 0:
                backoff = min(
                    self.poll_interval * (2 ** (consecutive_fails - 1)),
                    self._MAX_BACKOFF,
                )
                time.sleep(backoff)
            else:
                time.sleep(self.poll_interval)

    def _recent(self, event_type=None, pair=None, max_age=300):
        """Filter cached events by type, pair, and age.

        Returns a list of event dicts. Note: these are references to the
        shared cache — callers should treat them as read-only.
        """
        now = time.time()
        with self._lock:
            results = []
            for e in self._events:
                ts = e.get("ts", e.get("timestamp", 0))
                if max_age and now - ts > max_age:
                    continue
                if event_type and e.get("type") != event_type:
                    continue
                if pair:
                    d = e.get("data", {})
                    if d.get("pair") != pair:
                        continue
                results.append(e)
            return results

    def _latest_data(self, event_type, pair=None, max_age=300):
        """Return the data payload from the most recent matching event, or None."""
        events = self._recent(event_type, pair, max_age)
        return events[-1].get("data") if events else None

    def signal_strength(self, event):
        """Get the time-decayed strength (0-1) of any bus event."""
        if not event:
            return 0
        return _decay_strength(event)

    def decayed_value(self, event, field, default=0):
        """Get a field from event data, multiplied by time decay."""
        if not event:
            return default
        val = event.get("data", {}).get(field, default)
        if isinstance(val, (int, float)):
            return val * _decay_strength(event)
        return val

    def whale_alerts(self, pair=None, max_age=300):
        return self._recent("WHALE_ALERT", pair, max_age)

    def whale_tier(self, pair, max_age=300):
        alerts = self.whale_alerts(pair, max_age)
        if not alerts:
            return None
        tiers = {"EXTREME": 3, "HIGH": 2, "MODERATE": 1, "LOW": 0}
        best = max(alerts, key=lambda a: tiers.get(a.get("data", {}).get("tier", "LOW"), 0))
        # Decay: old whale alerts fade to None
        if _decay_strength(best) < 0.2:
            return None
        return best.get("data", {}).get("tier")

    def phitex_status(self, pair=None):
        events = self._recent("PHITEX_UPDATE", max_age=120)
        if not events:
            return None
        latest = events[-1].get("data", {})
        result = {
            "fleet_score": latest.get("fleet_score", 0),
            "fleet_direction": latest.get("fleet_direction", "NEUTRAL"),
        }
        if pair:
            for p in latest.get("critical_pairs", []) + latest.get("pre_critical_pairs", []):
                name = p.get("pair") if isinstance(p, dict) else p
                if name == pair:
                    if isinstance(p, dict):
                        result["pair_score"] = p.get("phi_tex", 0)
                        result["pair_regime"] = p.get("regime", "EQUILIBRIUM")
                    else:
                        result["pair_regime"] = "PRE_CRITICAL"
                    break
        return result

    def phitex_critical(self, pair, max_age=120):
        status = self.phitex_status(pair)
        if not status:
            return False
        return status.get("pair_regime") in ("CRITICAL", "PRE_CRITICAL")

    def aegis_regime(self):
        events = self._recent("AEGIS_UPDATE", max_age=120)
        if not events:
            return None
        return events[-1].get("data", {}).get("regime")

    def aegis_score(self):
        events = self._recent("AEGIS_UPDATE", max_age=120)
        if not events:
            return None
        return events[-1].get("data", {}).get("score")

    def convergent_signals(self, pair, max_age=60):
        return self._latest_data("HIGH_CONVICTION", pair, max_age)

    def emergency_active(self, max_age=300):
        """Check for recent EMERGENCY_REDUCE events.

        Note: this listener polls every ``poll_interval`` seconds (default 10s),
        so detection can lag up to that amount. For lower latency, consider
        subscribing to the SSE stream at /api/events/stream.
        """
        return len(self._recent("EMERGENCY_REDUCE", max_age=max_age)) > 0

    def attention_alerts(self, pair=None, max_age=300):
        return self._recent("ATTENTION", pair, max_age)

    def newton_force(self, pair, max_age=120):
        """Get Newton force reading for a pair."""
        return self._latest_data("NEWTON_FORCE", pair, max_age)

    def newton_reactions(self, pair, max_age=120):
        """Get reaction alerts where this pair is the reactor."""
        events = self._recent("NEWTON_REACTION", max_age=max_age)
        return [e["data"] for e in events
                if e.get("data", {}).get("reactor_pair") == pair]

    def euclid_levels(self, pair, max_age=120):
        """Get Euclidean support/resistance alerts for a pair."""
        return self._recent("EUCLID_LEVEL", pair, max_age)

    def sentiment_extreme(self, max_age=300):
        """Get latest sentiment extreme alert from Contrarian."""
        return self._latest_data("SENTIMENT_EXTREME", max_age=max_age)

    def sentiment_state(self, max_age=300):
        """Get current sentiment state (e.g. EXTREME_FEAR, EXTREME_GREED).

        Reads the ``type`` field from the SENTIMENT_EXTREME event's *data*
        payload — Contrarian includes it there alongside ``signal`` and
        ``sentiment_state``.
        """
        ev = self.sentiment_extreme(max_age)
        return ev.get("type") if ev else None

    def chronos_alerts(self, max_age=300):
        """Get recent Chronos temporal alerts."""
        return self._recent("CHRONOS_ALERT", max_age=max_age)

    def session_active(self, max_age=300):
        """Check if a session open/overlap event fired recently."""
        alerts = self.chronos_alerts(max_age)
        return [a.get("data", {}) for a in alerts
                if a.get("data", {}).get("type") in ("SESSION_OPEN", "SESSION_OVERLAP")]

    def funding_settlement_soon(self, max_age=1800):
        """Check if funding settlement is approaching."""
        alerts = self.chronos_alerts(max_age)
        return any(a.get("data", {}).get("type") == "FUNDING_SETTLEMENT" for a in alerts)

    def chronos_temporal(self, max_age=3600):
        """Get the latest Chronos temporal-bias readout for trade-time consumption.

        Reuses chronos_alerts() (CHRONOS_ALERT, half-life already in
        _HALF_LIVES) and shapes it into the two things a trader cares about:
        the most recent TIME_ANOMALY (statistically-gated hourly bias, see
        chronos.py) and any SESSION_OPEN/SESSION_OVERLAP events that fired
        recently — same type filter as session_active(), but each event
        keeps its bus-level "ts" so callers can gate on freshness (e.g. "did
        an overlap start in the last 15 minutes").

        Returns dict:
            {
                "time_anomaly": dict or None,   # latest TIME_ANOMALY data payload
                "session_events": list[dict],   # recent SESSION_OPEN/SESSION_OVERLAP payloads
            }
        time_anomaly payload (when present): {hour_utc, bias_pct, n, ci_low,
        ci_high, direction, pair, ts}. direction is "bullish" or "bearish".
        """
        alerts = self.chronos_alerts(max_age)
        anomalies = [a for a in alerts if a.get("data", {}).get("type") == "TIME_ANOMALY"]
        latest_anomaly = None
        if anomalies:
            latest_anomaly = dict(anomalies[-1].get("data", {}))
            latest_anomaly["ts"] = anomalies[-1].get("ts", anomalies[-1].get("timestamp", 0))
        session_events = []
        for a in alerts:
            d = a.get("data", {})
            if d.get("type") in ("SESSION_OPEN", "SESSION_OVERLAP"):
                se = dict(d)
                se["ts"] = a.get("ts", a.get("timestamp", 0))
                session_events.append(se)
        return {
            "time_anomaly": latest_anomaly,
            "session_events": session_events,
        }

    def spread_positions(self, max_age=300):
        """Get recent spread open events from Arbitrageur."""
        return self._recent("SPREAD_OPEN", max_age=max_age)

    def spread_closes(self, max_age=300):
        """Get recent spread close events from Arbitrageur."""
        return self._recent("SPREAD_CLOSE", max_age=max_age)

    def active_spreads(self, max_age=600):
        """Get currently open spreads (opens minus closes).

        Uses ``pair_key`` as dedup key — if a spread opens, closes, and
        reopens within ``max_age``, only the last open survives.
        """
        opens = {e.get("data", {}).get("pair_key"): e
                 for e in self._recent("SPREAD_OPEN", max_age=max_age)}
        for e in self._recent("SPREAD_CLOSE", max_age=max_age):
            opens.pop(e.get("data", {}).get("pair_key"), None)
        return list(opens.values())

    def einstein_energy(self, pair=None, max_age=120):
        """Get Einstein energy readings — high breakout potential."""
        return self._latest_data("EINSTEIN_ENERGY", pair, max_age)

    def schwarzschild_topology(self, pair=None, max_age=120):
        """Get Schwarzschild market topology for a pair."""
        return self._latest_data("SCHWARZSCHILD_HORIZON", pair, max_age)

    def is_captured(self, pair, max_age=120):
        """Check if a pair is gravitationally captured (trapped near S/R)."""
        topo = self.schwarzschild_topology(pair, max_age)
        return topo.get("topology") == "CAPTURED" if topo else False

    def breakout_imminent(self, pair, max_age=120):
        """Check if Einstein flags high energy + low gravity = breakout."""
        ei = self.einstein_energy(pair, max_age)
        if not ei:
            return False
        return ei.get("interpretation") == "HIGH_ENERGY_LOW_GRAVITY"

    # === INFORMATION GEOMETRY / TOPOLOGY / QUANTUM / CAUSAL ===

    def manifold_warning(self, pair=None, max_age=300):
        """Get manifold warnings — distribution morphing, models unreliable."""
        return self._latest_data("MANIFOLD_WARNING", pair, max_age)

    def manifold_reliable(self, pair, max_age=300):
        """Is the statistical manifold stable enough to trust models?

        Three distinct cases, and they must not collapse into one:
          no warning at all      -> nothing is objecting; treat as reliable
          warning WITH a figure  -> judge it on the figure
          warning WITHOUT one    -> an objection was raised and we cannot
                                    grade it. That is not evidence of
                                    reliability.

        The old `.get("model_reliability", 1.0)` gave the third case the
        MAXIMUM possible score, so a warning whose reliability was missing
        sailed through a `> 0.5` gate as "reliable" — the one case where
        something is demonstrably wrong was the case scored perfect. nexus now
        publishes a real figure (0.611 / 0.434 / 0.264 / 0.000 across live
        pairs), and 0.0 is a legitimate value there, so a missing key means
        absence, never zero and never one.
        """
        warning = self.manifold_warning(pair, max_age)
        if not warning:
            return True  # no warning = nothing is objecting
        rel = warning.get("model_reliability")
        if not isinstance(rel, (int, float)):
            # A warning we cannot grade is not a clean bill of health.
            return False
        return rel > 0.5

    def cycle_detected(self, pair=None, max_age=300):
        """Get topological cycle detection events."""
        return self._latest_data("CYCLE_DETECTED", pair, max_age)

    def cycle_active(self, pair, max_age=300):
        """Is a topological cycle detected for this pair?"""
        cycle = self.cycle_detected(pair, max_age)
        return cycle is not None and cycle.get("cyclicality", 0) > 0.2

    def quantum_state(self, pair=None, max_age=120):
        """Get the quantum probability distribution for a pair."""
        return self._latest_data("QUANTUM_COLLAPSE", pair, max_age)

    def quantum_collapsed(self, pair, max_age=120):
        """Has the quantum state collapsed to a definite regime?"""
        qs = self.quantum_state(pair, max_age)
        if not qs:
            return False
        return qs.get("confidence", 0) > 0.6

    def causal_graph(self, max_age=600):
        """Get the latest causal graph update."""
        return self._latest_data("CAUSAL_FLOW", max_age=max_age)

    def causal_power(self, source, max_age=600):
        """How much causal influence does this signal source have?"""
        graph = self.causal_graph(max_age)
        if not graph:
            return 0
        return graph.get("causal_powers", {}).get(source, 0)

    # === SHANNON / BOLTZMANN / LORENZ / THOM / PRIGOGINE ===

    def shannon_map(self, max_age=600):
        """Get the latest fleet information map from Shannon."""
        return self._latest_data("SHANNON_MAP", max_age=max_age)

    def signal_noise(self, source, target, max_age=600):
        """Is the connection from source to target noise?"""
        fmap = self.shannon_map(max_age)
        if not fmap:
            return None
        for conn in fmap.get("connections", []):
            if conn.get("source") == source and conn.get("target") == target:
                return conn.get("is_noise", True)
        return None

    def transfer_entropy(self, source, target, max_age=600):
        """Get transfer entropy (directed info flow) from source to target."""
        fmap = self.shannon_map(max_age)
        if not fmap:
            return 0
        for conn in fmap.get("connections", []):
            if conn.get("source") == source and conn.get("target") == target:
                return conn.get("transfer_entropy_fwd", 0)
        return 0

    def book_phase(self, pair=None, max_age=120):
        """Get Boltzmann order book thermodynamic phase."""
        return self._latest_data("BOOK_PHASE", pair, max_age)

    def book_temperature(self, pair, max_age=120):
        """Get order book temperature for a pair."""
        phase = self.book_phase(pair, max_age)
        return phase.get("temperature", 0) if phase else 0

    def book_pressure(self, pair, max_age=120):
        """Get order book pressure (bid/ask imbalance force)."""
        phase = self.book_phase(pair, max_age)
        return phase.get("pressure", 0) if phase else 0

    def book_boiling(self, pair, max_age=120):
        """Is the order book undergoing a phase transition?"""
        phase = self.book_phase(pair, max_age)
        if not phase:
            return False
        return phase.get("phase") in ("BOILING", "PLASMA")

    def chaos_state(self, pair=None, max_age=300):
        """Get Lorenz chaos analysis for a pair."""
        return self._latest_data("CHAOS_STATE", pair, max_age)

    def lyapunov(self, pair, max_age=300):
        """Lyapunov exponent (chaos measure), or None if unmeasured.

        0 is a meaningful Lyapunov value (a non-chaotic, marginally stable
        system), so returning 0 for "no data" claimed a specific measurement
        the fleet never made.
        """
        state = self.chaos_state(pair, max_age)
        if not state:
            return None
        lyap = state.get("lyapunov_exponent")
        return lyap if isinstance(lyap, (int, float)) else None

    def chaos_confidence(self, pair, max_age=300):
        """Chaos-derived confidence (high = predictable), or None if unknown.

        Returns None rather than 0.5 when there is no chaos state for the pair
        or the state carries no confidence. 0.5 is a legitimate measured value
        here, so returning it for "we have no idea" made the two cases
        indistinguishable to every caller. Callers must decide what to do with
        an unknown; they cannot decide if it arrives disguised as a
        middling measurement.
        """
        state = self.chaos_state(pair, max_age)
        if not state:
            return None
        conf = state.get("confidence")
        return conf if isinstance(conf, (int, float)) else None

    def attractor_departing(self, pair, max_age=300):
        """Is the market leaving its attractor (regime change)?"""
        state = self.chaos_state(pair, max_age)
        if not state:
            return False
        return state.get("attractor_departure", 0) > 0.7

    def catastrophe_warning(self, pair=None, max_age=300):
        """Get Thom catastrophe early warning signals."""
        return self._latest_data("CATASTROPHE_WARNING", pair, max_age)

    def catastrophe_imminent(self, pair, max_age=300):
        """Is a catastrophe (sudden regime jump) imminent?"""
        warning = self.catastrophe_warning(pair, max_age)
        if not warning:
            return False
        return warning.get("ews_score", 0) > 0.5

    def catastrophe_direction(self, pair, max_age=300):
        """Predicted direction of the coming catastrophe jump."""
        warning = self.catastrophe_warning(pair, max_age)
        if not warning:
            return None
        return warning.get("predicted_direction")

    def structure_forming(self, pair=None, max_age=300):
        """Get Prigogine dissipative structure analysis."""
        return self._latest_data("STRUCTURE_FORMING", pair, max_age)

    def entropy_production(self, pair, max_age=300):
        """Get entropy production rate for a pair."""
        state = self.structure_forming(pair, max_age)
        return state.get("entropy_production_rate", 0) if state else 0

    def structure_score(self, pair, max_age=300):
        """Get structure formation score (trend/cycle crystallizing)."""
        state = self.structure_forming(pair, max_age)
        return state.get("structure_formation_score", 0) if state else 0

    def bifurcation_near(self, pair, max_age=300):
        """Is the market near a bifurcation point?"""
        state = self.structure_forming(pair, max_age)
        if not state:
            return False
        return state.get("bifurcation_proximity", 0) > 0.5

    # === JIM'S MEASUREMENT INFRASTRUCTURE ===

    def ensemble_decision(self, pair=None, max_age=120):
        """Get latest ensemble aggregator decision for a pair."""
        return self._latest_data("ENSEMBLE_DECISION", pair, max_age)

    def fleet_expectancy(self, max_age=600):
        """Get fleet-wide expectancy stats."""
        return self._latest_data("FLEET_EXPECTANCY", max_age=max_age)

    def bot_expectancy(self, bot_id, max_age=600):
        """Get expectancy stats for a specific bot."""
        stats = self.fleet_expectancy(max_age)
        if not stats:
            return None
        return stats.get("bot_stats", {}).get(bot_id)

    def signal_value_report(self, max_age=3600):
        """Get latest signal decomposition report."""
        return self._latest_data("SIGNAL_VALUE_REPORT", max_age=max_age)

    def source_verdict(self, source, max_age=3600):
        """Get the KEEP/CUT/EVALUATE verdict for a signal source."""
        report = self.signal_value_report(max_age)
        if not report:
            return None
        return report.get("rankings", {}).get(source, {}).get("verdict")
