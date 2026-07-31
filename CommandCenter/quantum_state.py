"""
QUANTUM MARKET STATE ENGINE

Inspired by quantum mechanics, but mathematically rigorous for markets.

Core idea: The market exists in a SUPERPOSITION of states.
It's not "trending" or "ranging" -- it's BOTH with different amplitudes.
The probability of observing a particular behavior depends on
HOW and WHEN you measure (your timeframe, your indicator, your entry).

Key concepts:

1. STATE VECTOR |psi> = a|trending_up> + b|trending_down> + c|ranging> + d|volatile> + e|quiet>
   where |a|^2 + |b|^2 + |c|^2 + |d|^2 + |e|^2 = 1

2. MEASUREMENT COLLAPSE: When you enter a trade (measure the market),
   you collapse the superposition into a definite state. The trade's
   outcome is probabilistic based on the amplitudes.

3. ENTANGLEMENT: Some pairs are entangled -- measuring one (trading it)
   affects the probability distribution of the other. BTC and ETH are
   entangled. Trading BTC "collapses" ETH's state too.

4. INTERFERENCE: Signals can constructively or destructively interfere.
   Two signals that individually have 50% WR can have 70% WR when they
   align (constructive) or 30% when they conflict (destructive).
   This is NOT the same as simple averaging.

5. DECOHERENCE: The superposition degrades over time as more information
   arrives. A fresh signal has high "quantum coherence" (the superposition
   is clean). An old signal has "decohered" -- it's basically classical.
   This mathematically models SIGNAL DECAY.
"""

import math
import time


class QuantumMarketState:
    def __init__(self):
        self.state_names = [
            'TRENDING_UP', 'TRENDING_DOWN', 'RANGING', 'VOLATILE', 'QUIET'
        ]
        self.n_states = len(self.state_names)

        # State vector: complex amplitudes (represented as [real, imag])
        # |a|^2 = probability of that state
        self.amplitudes = {}   # {pair: [[real, imag], ...]}

        # Entanglement matrix -- how strongly pairs are entangled
        self.entanglement = {}  # {pair_a|pair_b: coupling_strength}

        # Signal coherence -- how "fresh" each signal is
        self.signal_coherence = {}  # {pair: {signal_name: {...}}}

    def initialize_pair(self, pair):
        """Start in equal superposition -- maximum uncertainty."""
        amp = 1.0 / math.sqrt(self.n_states)
        self.amplitudes[pair] = [[amp, 0.0] for _ in range(self.n_states)]
        self.signal_coherence[pair] = {}

    def inject_signal(self, pair, signal_name, state_biases, strength=1.0):
        """
        A signal doesn't DETERMINE the state -- it TILTS the amplitude vector
        toward the states it favors.

        state_biases: dict of {state_name: bias_amount}
          Positive bias = more likely, negative = less likely
        strength: how much the signal tilts the vector (decays with age)

        CALIBRATION NOTE (fixed — was a no-op on probability):
        The previous implementation applied an independent U(1) phase
        rotation to each state's (real, imag) pair:
            amp[i] = amp[i] * e^(i * angle_i)
        A phase rotation preserves |amp|^2 EXACTLY for every i (rotation
        matrices are norm-preserving) — so P(state) = |amp|^2 could never
        move no matter how many signals were injected. Only the phase
        (imag component) changed, and decohere() decays phase back toward
        zero every cycle. Net effect: superposition_entropy (H) stayed
        pinned at ~0.96-1.0 for every pair forever (H=1.0 is the uniform
        5-state distribution -log2(1/5)-normalized == 1), and
        QUANTUM_COLLAPSE (H < 0.3 threshold) was structurally unreachable.

        Fix: bias now tilts amplitude MAGNITUDE directly via an
        exponential (log-linear) reweighting of the real component —
        amp_i.real *= exp(bias_i) — analogous to a Boltzmann/exponential-
        family tilt of the Born-rule probabilities. This actually moves
        probability mass toward favored states while leaving the sign of
        the real component (and hence phase continuity) intact, then
        renormalizes so sum(|amp_i|^2) = 1 as required. Repeated same-
        direction injections now compound (probability genuinely
        accumulates in the favored state), so a sustained directional
        regime drives H down toward the collapse threshold, while
        conflicting/noisy injections keep H high — H becomes a real
        discriminator instead of a constant.
        """
        if pair not in self.amplitudes:
            self.initialize_pair(pair)

        amps = self.amplitudes[pair]

        # Exponential tilt: amplify states the signal favors, suppress others
        for i, state in enumerate(self.state_names):
            bias = state_biases.get(state, 0) * strength

            # Tilt factor: exp(bias * TILT_SCALE). TILT_SCALE=0.5 chosen so
            # a strong single-signal injection (bias*strength ~= 0.5, e.g.
            # Newton force near its typical range) moves probability
            # noticeably in one scan cycle without letting one signal
            # instantly dominate — collapse should emerge from several
            # cycles of agreement, not one reading.
            tilt = math.exp(bias * 0.5)

            real = amps[i][0]
            imag = amps[i][1]

            amps[i][0] = real * tilt
            amps[i][1] = imag * tilt

        # Renormalize -- total probability must equal 1
        self._renormalize(pair)

        # Track coherence
        if pair not in self.signal_coherence:
            self.signal_coherence[pair] = {}
        self.signal_coherence[pair][signal_name] = {
            'coherence': 1.0,
            'injected_at': time.time(),
        }

    def get_probabilities(self, pair):
        """Get the probability distribution over states (Born rule: P = |a|^2)."""
        if pair not in self.amplitudes:
            return {s: 1 / self.n_states for s in self.state_names}

        probs = {}
        for i, state in enumerate(self.state_names):
            amp = self.amplitudes[pair][i]
            probs[state] = amp[0] ** 2 + amp[1] ** 2
        return probs

    def get_dominant_state(self, pair):
        """Most probable state and its confidence."""
        probs = self.get_probabilities(pair)
        dominant = max(probs, key=probs.get)
        return dominant, probs[dominant]

    def get_superposition_entropy(self, pair):
        """
        Entropy of the state vector -- measure of "quantumness."
        Low entropy = nearly collapsed (one state dominates)
        High entropy = deep superposition (all states equally likely)
        """
        probs = self.get_probabilities(pair)
        entropy = 0
        for p in probs.values():
            if p > 0:
                entropy -= p * math.log(p + 1e-10)

        max_entropy = math.log(self.n_states)
        return entropy / max_entropy if max_entropy > 0 else 0

    def compute_interference(self, pair, signal_a_state, signal_b_state):
        """
        Compute interference between two signals.

        When two signals point to the SAME state, they constructively interfere:
        combined probability > sum of individual probabilities.

        When they point to DIFFERENT states, they destructively interfere:
        combined probability < sum.

        This is fundamentally different from averaging!
        """
        if pair not in self.amplitudes:
            return {'type': 'NO_DATA', 'factor': 1.0}

        if signal_a_state == signal_b_state:
            # Constructive interference -- amplitudes ADD, then square
            # P_combined = (|a_a| + |a_b|)^2 > |a_a|^2 + |a_b|^2
            return {
                'type': 'CONSTRUCTIVE',
                'factor': 1.4,  # sqrt(2) boost approximation
                'combined_state': signal_a_state,
            }
        else:
            # Destructive interference -- amplitudes subtract
            return {
                'type': 'DESTRUCTIVE',
                'factor': 0.7,
                'state_a': signal_a_state,
                'state_b': signal_b_state,
            }

    def decohere(self, pair, dt_seconds):
        """
        Apply decoherence -- the superposition degrades toward classical.
        Over time, off-diagonal elements decay and the state becomes
        a classical mixture rather than a quantum superposition.

        In market terms: old signals lose their "quantum advantage"
        and the state becomes just a weighted average rather than
        an interference-capable superposition.
        """
        if pair not in self.amplitudes:
            return

        # Decoherence rate -- how fast coherence decays
        decoherence_rate = 0.001  # per second
        decay = math.exp(-decoherence_rate * dt_seconds)

        # Decay imaginary parts toward zero (classical has no phase)
        for amp in self.amplitudes[pair]:
            amp[1] *= decay

        # Renormalize
        self._renormalize(pair)

        # Decay signal coherences
        now = time.time()
        for signal_name in list(self.signal_coherence.get(pair, {}).keys()):
            sc = self.signal_coherence[pair][signal_name]
            age = now - sc['injected_at']
            sc['coherence'] = math.exp(-age / 300)  # 5-minute half-life

    def entangle(self, pair_a, pair_b, correlation):
        """
        Entangle two pairs based on their correlation.
        When one is "measured" (traded), it partially collapses the other.
        """
        key = f"{pair_a}|{pair_b}"
        self.entanglement[key] = abs(correlation)

    def collapse_on_trade(self, pair, observed_state):
        """
        "Measure" the market by entering a trade.
        This collapses the superposition toward the observed state.

        Also partially collapses entangled pairs.
        """
        if pair not in self.amplitudes:
            return

        # Strong collapse toward observed state
        state_idx = (self.state_names.index(observed_state)
                     if observed_state in self.state_names else 0)

        residual = 0.1 / math.sqrt(max(1, self.n_states - 1))
        for i in range(self.n_states):
            if i == state_idx:
                self.amplitudes[pair][i] = [0.9, 0.0]
            else:
                self.amplitudes[pair][i] = [residual, 0.0]

        self._renormalize(pair)

        # Propagate to entangled pairs
        for key, coupling in self.entanglement.items():
            parts = key.split('|')
            if pair in parts:
                other_pair = parts[1] if parts[0] == pair else parts[0]
                if other_pair and other_pair in self.amplitudes:
                    self._partial_collapse(other_pair, observed_state,
                                          coupling * 0.5)

    def _partial_collapse(self, pair, toward_state, strength):
        if pair not in self.amplitudes:
            return

        state_idx = (self.state_names.index(toward_state)
                     if toward_state in self.state_names else 0)

        for i in range(self.n_states):
            current_amp = math.sqrt(
                self.amplitudes[pair][i][0] ** 2 +
                self.amplitudes[pair][i][1] ** 2
            )
            if i == state_idx:
                target = current_amp + strength * (1 - current_amp)
            else:
                target = current_amp * (1 - strength * 0.5)

            # Preserve phase, adjust magnitude
            if current_amp > 0:
                scale = target / current_amp
                self.amplitudes[pair][i][0] *= scale
                self.amplitudes[pair][i][1] *= scale

        self._renormalize(pair)

    def _renormalize(self, pair):
        """Ensure total probability equals 1."""
        amps = self.amplitudes.get(pair)
        if not amps:
            return
        total = sum(a[0] ** 2 + a[1] ** 2 for a in amps)
        if total > 0:
            norm = math.sqrt(total)
            for a in amps:
                a[0] /= norm
                a[1] /= norm

    def get_full_state(self, pair):
        probs = self.get_probabilities(pair)
        dominant, confidence = self.get_dominant_state(pair)
        entropy = self.get_superposition_entropy(pair)

        # Coherence of signals
        coherences = {}
        for sig, data in self.signal_coherence.get(pair, {}).items():
            coherences[sig] = round(data['coherence'], 3)

        return {
            'probabilities': {s: round(p, 4) for s, p in probs.items()},
            'dominant_state': dominant,
            'confidence': round(confidence, 4),
            'superposition_entropy': round(entropy, 4),
            'is_superposed': entropy > 0.5,
            'signal_coherences': coherences,
            'interpretation': self._interpret(probs, entropy, dominant,
                                             confidence),
        }

    def _interpret(self, probs, entropy, dominant, confidence):
        if entropy < 0.2:
            return f"COLLAPSED_{dominant}"
        elif entropy < 0.5:
            sorted_states = sorted(probs.items(), key=lambda x: x[1],
                                  reverse=True)
            return (f"PARTIAL_{sorted_states[0][0]}"
                    f"_vs_{sorted_states[1][0]}")
        elif entropy > 0.8:
            return "DEEP_SUPERPOSITION"
        else:
            return "MODERATE_SUPERPOSITION"
