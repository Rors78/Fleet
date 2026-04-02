"""
SIGNAL DECAY -- Every signal has a half-life.

An RSI reading is useful for 3 bars. An Oracle regime call lasts 50 bars.
A whale detection lasts 10 bars. After that, the information is PRICED IN.

This module:
1. Assigns default half-lives to each signal type
2. Measures EMPIRICAL half-lives from historical signal→outcome data
3. Provides a decay function that MUST be applied before consuming any signal
4. Rejects signals that have decayed below noise threshold

"Never use a signal without decaying it first." -- Jim Simons

Usage:
    from signal_decay import SignalDecay
    decay = SignalDecay()

    # When consuming a signal:
    strength = decay.get_strength('WHALE_ALERT', signal_timestamp)
    if strength < 0.1:
        pass  # signal is stale, ignore it

    # When publishing, attach half-life:
    half_life = decay.get_half_life('WHALE_ALERT')
    publisher.publish('WHALE_ALERT', {
        ...,
        'estimated_half_life': half_life,
    })

    # Record outcome to improve half-life estimates:
    decay.record_outcome('WHALE_ALERT', signal_time, outcome_time, profitable)
"""

import json
import math
import os
import time

_LN2 = math.log(2)  # 0.6931... — exact ln(2) for exponential decay


# Default half-lives in seconds (initial estimates, refined by data)
DEFAULT_HALF_LIVES = {
    # Fast-decaying: microstructure signals
    'WHALE_ALERT':          600,    # 10 min -- whales move fast
    'TRADE_OPEN':           120,    # 2 min -- immediate event
    'TRADE_CLOSE':          60,     # 1 min -- already happened
    'SPREAD_OPEN':          300,    # 5 min
    'SPREAD_CLOSE':         60,     # 1 min

    # Medium-decaying: analytical signals
    'SIGNAL':               900,    # 15 min -- generic signal
    'HIGH_CONVICTION':      1200,   # 20 min -- fleet convergence
    'FORECAST_CONVICTION':  1800,   # 30 min -- forecast
    'NEWTON_FORCE':         600,    # 10 min -- force reading
    'NEWTON_REACTION':      300,    # 5 min -- reaction pair
    'EUCLID_LEVEL':         3600,   # 1 hr -- S/R levels persist
    'EINSTEIN_ENERGY':      1200,   # 20 min -- breakout potential
    'SCHWARZSCHILD_HORIZON': 1800,  # 30 min -- topology
    'ATTENTION':            600,    # 10 min

    # Slow-decaying: regime/structural signals
    'REGIME_CHANGE':        7200,   # 2 hr -- regimes last
    'AEGIS_UPDATE':         3600,   # 1 hr -- meta-assessment
    'PHITEX_UPDATE':        1800,   # 30 min -- thermodynamic state
    'CHRONOS_ALERT':        3600,   # 1 hr -- session events
    'SENTIMENT_EXTREME':    3600,   # 1 hr -- sentiment

    # Very slow: structural
    'MANIFOLD_WARNING':     7200,   # 2 hr -- distribution morphing
    'CYCLE_DETECTED':       14400,  # 4 hr -- topological cycle
    'QUANTUM_COLLAPSE':     3600,   # 1 hr -- regime definite
    'CAUSAL_GRAPH_UPDATE':  21600,  # 6 hr -- causal structure
    'CATASTROPHE_WARNING':  3600,   # 1 hr -- catastrophe approaching
    'CHAOS_STATE':          7200,   # 2 hr -- attractor properties
    'BOOK_PHASE':           600,    # 10 min -- book state changes fast
    'STRUCTURE_FORMING':    3600,   # 1 hr -- dissipative structure
    'SHANNON_MAP':          21600,  # 6 hr -- info map

    # Events that don't decay (pure state updates)
    'BOT_STATUS':           86400,  # 24 hr
    'BOT_DOWN':             86400,
    'BOT_RECOVERED':        86400,
    'PORTFOLIO_RESERVE':    300,
    'PORTFOLIO_DENIAL':     300,
    'EMERGENCY_REDUCE':     1800,   # 30 min
    'FLEET_ALERT':          1800,
}

NOISE_THRESHOLD = 0.05  # below 5% strength = noise, ignore


class SignalDecay:
    PERSIST_PATH = os.path.join(os.path.dirname(__file__),
                                'logs', 'signal_decay.json')

    def __init__(self):
        self.half_lives = dict(DEFAULT_HALF_LIVES)
        self.outcome_log = []       # for empirical half-life estimation
        self.max_outcomes = 2000
        self._load()

    # ── Core Decay Function ─────────────────────────────────────────

    def get_strength(self, signal_type, signal_timestamp, current_time=None):
        """
        Get the current strength of a signal after decay.

        Returns: 0.0 (completely stale) to 1.0 (perfectly fresh)

        Decay model: exponential
        strength = exp(-_LN2 * age / half_life)
        At t = half_life, strength = 0.5
        At t = 3 * half_life, strength = 0.125
        At t = 5 * half_life, strength = 0.031 (effectively dead)
        """
        if current_time is None:
            current_time = time.time()

        age = current_time - signal_timestamp
        if age < 0:
            return 1.0  # future signal? use full strength

        half_life = self.half_lives.get(signal_type, 900)  # default 15 min
        if half_life <= 0:
            return 0.0

        strength = math.exp(-_LN2 * age / half_life)
        return max(0, min(1, strength))

    def is_stale(self, signal_type, signal_timestamp, current_time=None):
        """Is this signal below the noise threshold?"""
        return self.get_strength(signal_type, signal_timestamp,
                                current_time) < NOISE_THRESHOLD

    def get_half_life(self, signal_type):
        """Get the current half-life estimate for a signal type."""
        return self.half_lives.get(signal_type, 900)

    def decay_confidence(self, signal_type, original_confidence,
                         signal_timestamp, current_time=None):
        """
        Apply decay to a confidence score.
        This is what bots should call before using any bus signal.
        """
        strength = self.get_strength(signal_type, signal_timestamp,
                                    current_time)
        return original_confidence * strength

    # ── Empirical Half-Life Estimation ──────────────────────────────

    def record_outcome(self, signal_type, signal_timestamp,
                       outcome_timestamp, profitable):
        """
        Record a signal → outcome pair for empirical half-life measurement.

        After enough data, the empirical half-life overrides the default.
        """
        self.outcome_log.append({
            'type': signal_type,
            'signal_ts': signal_timestamp,
            'outcome_ts': outcome_timestamp,
            'delay': outcome_timestamp - signal_timestamp,
            'profitable': profitable,
        })
        self.outcome_log = self.outcome_log[-self.max_outcomes:]

        # Re-estimate half-life if we have enough data
        self._update_half_life(signal_type)
        self._save()

    def _update_half_life(self, signal_type):
        """
        Estimate empirical half-life from outcome data.

        Method: Find the delay at which the signal's accuracy drops to 50%
        of its peak accuracy. That's the half-life.

        Signals are useful when they predict outcomes better than chance.
        As the delay grows, accuracy approaches 50% (random).
        The half-life is when accuracy = (peak_accuracy + 50%) / 2.
        """
        outcomes = [o for o in self.outcome_log if o['type'] == signal_type]
        if len(outcomes) < 20:
            return  # not enough data

        # Sort by delay
        outcomes.sort(key=lambda o: o['delay'])

        # Bin by delay and compute accuracy in each bin
        n_bins = 8
        bin_size = max(1, len(outcomes) // n_bins)
        bins = []

        for i in range(0, len(outcomes), bin_size):
            chunk = outcomes[i:i + bin_size]
            if not chunk:
                continue
            avg_delay = sum(o['delay'] for o in chunk) / len(chunk)
            accuracy = sum(1 for o in chunk if o['profitable']) / len(chunk)
            bins.append((avg_delay, accuracy))

        if len(bins) < 3:
            return

        # Find peak accuracy (usually the first bins)
        peak_acc = max(acc for _, acc in bins[:3])
        baseline = 0.5  # random chance

        if peak_acc <= baseline:
            return  # signal has no edge at any delay

        # Half-life = delay where accuracy = midpoint between peak and baseline
        half_level = (peak_acc + baseline) / 2

        for delay, acc in bins:
            if acc <= half_level:
                # Found the half-life delay
                self.half_lives[signal_type] = max(30, round(delay))
                return

        # Never dropped to half-level — signal has very long half-life
        self.half_lives[signal_type] = max(
            self.half_lives.get(signal_type, 900),
            round(bins[-1][0] * 1.5),
        )

    # ── Reporting ───────────────────────────────────────────────────

    def get_all_half_lives(self):
        """Return all half-lives, sorted by duration."""
        result = []
        for sig_type, hl in sorted(self.half_lives.items(),
                                   key=lambda x: x[1]):
            outcomes = [o for o in self.outcome_log if o['type'] == sig_type]
            result.append({
                'signal_type': sig_type,
                'half_life_seconds': hl,
                'half_life_human': self._human_duration(hl),
                'data_points': len(outcomes),
                'source': 'empirical' if len(outcomes) >= 20 else 'default',
            })
        return result

    def get_stale_signals(self, events, current_time=None):
        """
        Given a list of bus events, return which ones are stale.
        Useful for fleet audit.
        """
        if current_time is None:
            current_time = time.time()

        stale = []
        for e in events:
            sig_type = e.get('type', '')
            ts = e.get('ts', e.get('timestamp', 0))
            strength = self.get_strength(sig_type, ts, current_time)
            if strength < NOISE_THRESHOLD:
                stale.append({
                    'type': sig_type,
                    'age': round(current_time - ts),
                    'strength': round(strength, 4),
                    'half_life': self.half_lives.get(sig_type, 900),
                })
        return stale

    def _human_duration(self, seconds):
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            return f"{seconds // 60}m"
        elif seconds < 86400:
            return f"{seconds // 3600}h"
        else:
            return f"{seconds // 86400}d"

    # ── Persistence ─────────────────────────────────────────────────

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.PERSIST_PATH), exist_ok=True)
            state = {
                'half_lives': self.half_lives,
                'outcome_log': self.outcome_log[-500:],
            }
            tmp = self.PERSIST_PATH + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(state, f)
            os.replace(tmp, self.PERSIST_PATH)
        except Exception:
            pass

    def _load(self):
        try:
            if os.path.exists(self.PERSIST_PATH):
                with open(self.PERSIST_PATH) as f:
                    state = json.load(f)
                saved_hl = state.get('half_lives', {})
                # Merge saved over defaults (saved takes priority)
                self.half_lives.update(saved_hl)
                self.outcome_log = state.get('outcome_log', [])
        except Exception:
            pass
