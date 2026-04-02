"""
PRIGOGINE — Dissipative Structures / Far-From-Equilibrium Thermodynamics

Markets are not equilibrium systems. They are DISSIPATIVE STRUCTURES --
like hurricanes, like living organisms. They maintain order by continuously
consuming energy (capital flow) and dissipating entropy (distributing
information).

Equilibrium thermodynamics says systems tend toward maximum entropy (chaos).
But Prigogine showed that far-from-equilibrium systems spontaneously CREATE
order -- complex, beautiful, self-organizing patterns.

Trends, cycles, support/resistance -- these are dissipative structures.
They emerge at critical entropy production rates.

Key outputs:

- Entropy Production Rate: How fast is entropy being generated?
  LOW = near equilibrium, boring, ranging.
  HIGH = far from equilibrium, structures forming (trends/cycles).

- Distance From Equilibrium: How far is the market from its "rest" state?
  This is the driving force for structure formation.

- Order Parameter: Measures spontaneous order in the market.
  When it crosses a threshold, a dissipative structure is forming.

- Bifurcation Detection: At critical points, the system splits into
  two possible states. This is where trends are BORN.
"""

import math


class PrigogineEngine:
    def __init__(self, window=50, eq_window=200):
        self.window = window        # analysis window
        self.eq_window = eq_window  # equilibrium estimation window

    def analyze(self, pair, candles):
        """
        Dissipative structure analysis of a price series.
        """
        if len(candles) < self.eq_window + 10:
            return self._default()

        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]

        returns = [(closes[i] - closes[i - 1]) / (closes[i - 1] + 1e-10)
                   for i in range(1, len(closes))]

        if len(returns) < self.eq_window:
            return self._default()

        # === ENTROPY PRODUCTION RATE ===
        # Rate at which the market generates entropy
        # Measured by the change in Shannon entropy of returns over time

        epr = self._entropy_production_rate(returns)

        # === DISTANCE FROM EQUILIBRIUM ===
        # Equilibrium = historical average behavior
        # Distance = how different current dynamics are from the average

        dfe = self._distance_from_equilibrium(returns, closes, volumes)

        # === ORDER PARAMETER ===
        # Measures spontaneous order emerging from chaos
        # Uses autocorrelation structure as a proxy — ordered systems
        # have strong, persistent autocorrelation

        order_param = self._order_parameter(returns)

        # === ENERGY FLUX ===
        # Capital flow through the market — the "fuel" for dissipative structures
        # Volume * absolute return = energy being pumped into the system

        energy_flux = self._energy_flux(returns, volumes)

        # === BIFURCATION DETECTION ===
        # At bifurcation points, the system's behavior splits
        # Detected by: variance explosion + bimodal return distribution

        bifurcation = self._detect_bifurcation(returns)

        # === STRUCTURE FORMATION SCORE ===
        # Combines all signals — is a dissipative structure forming RIGHT NOW?

        # Prigogine's insight: structures form when entropy production
        # is HIGH enough to sustain them AND the system is far from equilibrium
        formation_score = self._structure_formation_score(
            epr, dfe, order_param, energy_flux, bifurcation
        )

        # === STRUCTURE TYPE ===
        structure_type = self._classify_structure(
            epr, dfe, order_param, energy_flux, bifurcation, formation_score
        )

        return {
            'entropy_production_rate': round(epr, 6),
            'distance_from_equilibrium': round(dfe, 4),
            'order_parameter': round(order_param, 4),
            'energy_flux': round(energy_flux, 6),
            'bifurcation_proximity': round(bifurcation['proximity'], 4),
            'bifurcation_type': bifurcation['type'],
            'structure_formation_score': round(formation_score, 4),
            'structure_type': structure_type,
            'interpretation': self._interpret(formation_score, structure_type,
                                             epr, dfe),
        }

    def _entropy_production_rate(self, returns):
        """
        Rate of entropy production.
        Compute Shannon entropy in sliding windows and measure the rate of change.
        """
        w = self.window
        if len(returns) < w + 20:
            return 0

        entropies = []
        for i in range(w, len(returns), max(1, w // 5)):
            segment = returns[i - w:i]
            h = self._shannon_entropy(segment)
            entropies.append(h)

        if len(entropies) < 3:
            return 0

        # Rate = derivative of entropy over time
        # Use finite differences
        rates = [entropies[i] - entropies[i - 1]
                 for i in range(1, len(entropies))]

        return sum(rates) / len(rates)

    def _shannon_entropy(self, data):
        """Shannon entropy of a data series using histogram method."""
        if len(data) < 5:
            return 0
        n_bins = 15
        lo, hi = min(data), max(data)
        rng = hi - lo if hi != lo else 1
        bins = [0] * n_bins
        for v in data:
            idx = min(int((v - lo) / rng * n_bins), n_bins - 1)
            bins[idx] += 1

        total = len(data)
        h = 0
        for b in bins:
            if b > 0:
                p = b / total
                h -= p * math.log(p)
        return h

    def _distance_from_equilibrium(self, returns, closes, volumes):
        """
        How far is the current market state from equilibrium?

        Equilibrium = long-term average of:
        - Mean return ≈ 0
        - Variance ≈ historical average
        - Volume ≈ historical average
        - Autocorrelation ≈ 0

        Distance = multivariate deviation from these.
        """
        w = self.window
        eq_w = self.eq_window

        # Current statistics
        recent_ret = returns[-w:]
        current_mean = sum(recent_ret) / len(recent_ret)
        current_var = sum((r - current_mean) ** 2 for r in recent_ret) / len(recent_ret)
        current_ac = self._lag1_autocorrelation(recent_ret)
        current_vol = sum(volumes[-w:]) / w if len(volumes) >= w else 0

        # Equilibrium statistics
        eq_ret = returns[-eq_w:]
        eq_mean = sum(eq_ret) / len(eq_ret)
        eq_var = sum((r - eq_mean) ** 2 for r in eq_ret) / len(eq_ret)
        eq_vol = sum(volumes[-eq_w:]) / eq_w if len(volumes) >= eq_w else 1

        # Distance components
        mean_dev = abs(current_mean - eq_mean) / max(math.sqrt(eq_var), 1e-10)
        var_dev = abs(current_var - eq_var) / max(eq_var, 1e-10)
        ac_dev = abs(current_ac)  # equilibrium AC ≈ 0
        vol_dev = abs(current_vol - eq_vol) / max(eq_vol, 1e-10)

        # Euclidean distance in deviation space
        distance = math.sqrt(
            mean_dev ** 2 + var_dev ** 2 + ac_dev ** 2 + vol_dev ** 2
        )

        return distance

    def _order_parameter(self, returns):
        """
        Order parameter — measures spontaneous symmetry breaking.

        In a disordered (equilibrium) market, returns are symmetric around 0.
        When order emerges (trend/structure), the symmetry breaks:
        - Persistent direction (trend)
        - Persistent oscillation (cycle)
        - Persistent clustering (regime)

        We measure: autocorrelation decay profile.
        Fast decay = disordered. Slow decay = ordered.
        """
        w = self.window
        recent = returns[-w:]

        # Compute autocorrelation at lags 1 through 10
        acs = []
        for lag in range(1, min(11, w // 3)):
            ac = self._lag1_autocorrelation(recent, lag)
            acs.append(abs(ac))

        if not acs:
            return 0

        # Order parameter = area under AC curve (slow decay = high order)
        return sum(acs) / len(acs)

    def _energy_flux(self, returns, volumes):
        """
        Energy flux = capital flow through the system.
        |return| * volume = energy transferred per candle.
        """
        w = self.window
        if len(returns) < w or len(volumes) < w + 1:
            return 0

        flux = []
        for i in range(len(returns) - w, len(returns)):
            if i >= 0 and i + 1 < len(volumes):
                flux.append(abs(returns[i]) * volumes[i + 1])

        return sum(flux) / len(flux) if flux else 0

    def _detect_bifurcation(self, returns):
        """
        Detect proximity to bifurcation point.

        At bifurcation:
        1. Variance explodes (fluctuations grow without bound)
        2. Return distribution becomes bimodal (two attracting states)
        3. System "flickers" between the two emerging states

        Returns proximity (0-1) and type.
        """
        w = self.window
        if len(returns) < w + 30:
            return {'proximity': 0, 'type': 'NONE'}

        recent = returns[-w:]
        earlier = returns[-(w + 30):-(30)]

        # Variance growth
        var_recent = sum(r ** 2 for r in recent) / len(recent)
        var_earlier = sum(r ** 2 for r in earlier) / len(earlier)
        var_ratio = var_recent / max(var_earlier, 1e-10)

        # Bimodality: split returns into positive and negative
        pos = [r for r in recent if r > 0]
        neg = [r for r in recent if r < 0]

        if pos and neg:
            pos_mean = sum(pos) / len(pos)
            neg_mean = sum(neg) / len(neg)
            separation = abs(pos_mean - neg_mean)
            overall_std = math.sqrt(var_recent) if var_recent > 0 else 1e-10
            bimodal_score = separation / overall_std
        else:
            bimodal_score = 0

        # Flickering: count sign changes
        sign_changes = sum(1 for i in range(1, len(recent))
                          if recent[i] * recent[i - 1] < 0)
        expected_changes = len(recent) / 2  # random walk expectation
        flicker_ratio = sign_changes / expected_changes if expected_changes > 0 else 1

        # Combine into proximity score
        proximity = (
            min(1, max(0, (var_ratio - 1) / 3)) * 0.4 +
            min(1, max(0, bimodal_score / 2)) * 0.3 +
            min(1, max(0, abs(flicker_ratio - 1) * 2)) * 0.3
        )

        # Type
        if bimodal_score > 1.5:
            bif_type = "PITCHFORK"   # symmetric splitting
        elif var_ratio > 2 and bimodal_score > 0.5:
            bif_type = "TRANSCRITICAL"  # asymmetric
        elif var_ratio > 1.5:
            bif_type = "SADDLE_NODE"    # one state about to vanish
        else:
            bif_type = "NONE"

        return {'proximity': proximity, 'type': bif_type}

    def _structure_formation_score(self, epr, dfe, order, flux, bifurcation):
        """
        Prigogine's key insight: dissipative structures form when:
        1. System is far from equilibrium (high dfe)
        2. Entropy production is sufficient (moderate epr)
        3. Energy flux sustains the structure (high flux)
        4. Order is beginning to emerge (rising order parameter)
        """
        # Normalize components
        dfe_score = min(1, dfe / 3)
        epr_score = min(1, abs(epr) * 20)
        order_score = min(1, order * 3)
        bif_score = bifurcation['proximity']

        # Structures need both distance from equilibrium AND order emerging
        formation = (
            dfe_score * 0.35 +
            order_score * 0.30 +
            epr_score * 0.15 +
            bif_score * 0.20
        )

        return formation

    def _classify_structure(self, epr, dfe, order, flux, bif, score):
        if score < 0.2:
            return "EQUILIBRIUM"
        elif score < 0.4 and dfe < 1:
            return "NEAR_EQUILIBRIUM"
        elif bif['proximity'] > 0.6:
            return f"BIFURCATION_{bif['type']}"
        elif order > 0.3 and dfe > 1.5:
            return "DISSIPATIVE_TREND"
        elif order > 0.2 and epr > 0:
            return "DISSIPATIVE_CYCLE"
        elif dfe > 2 and order < 0.1:
            return "FAR_FROM_EQ_DISORDERED"
        else:
            return "STRUCTURE_FORMING"

    def _lag1_autocorrelation(self, data, lag=1):
        n = len(data)
        if n <= lag:
            return 0
        mean = sum(data) / n
        var = sum((x - mean) ** 2 for x in data) / n
        if var <= 0:
            return 0
        cov = sum((data[i] - mean) * (data[i - lag] - mean)
                  for i in range(lag, n)) / n
        return cov / var

    def _interpret(self, score, structure, epr, dfe):
        if score > 0.7 and "BIFURCATION" in structure:
            return "CRITICAL_BIFURCATION"
        elif score > 0.6 and "TREND" in structure:
            return "TREND_CRYSTALLIZING"
        elif score > 0.5 and "CYCLE" in structure:
            return "CYCLE_EMERGING"
        elif score > 0.4:
            return "STRUCTURE_NUCLEATING"
        elif dfe > 2 and score < 0.3:
            return "FAR_FROM_EQ_NO_ORDER"
        elif dfe < 0.5:
            return "THERMAL_EQUILIBRIUM"
        else:
            return "FLUCTUATING"

    def _default(self):
        return {
            'entropy_production_rate': 0,
            'distance_from_equilibrium': 0,
            'order_parameter': 0, 'energy_flux': 0,
            'bifurcation_proximity': 0, 'bifurcation_type': 'NONE',
            'structure_formation_score': 0, 'structure_type': 'EQUILIBRIUM',
            'interpretation': 'INSUFFICIENT_DATA',
        }
