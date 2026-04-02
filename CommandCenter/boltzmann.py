"""
BOLTZMANN — Statistical Mechanics of Order Books

Every order in the book is a particle with energy (distance from mid-price)
and state (bid or ask). The order book IS a thermodynamic system.

- Temperature: Average kinetic energy of orders (spread x volume).
  Hot book = wide spread, high volume, volatile. Cold book = tight, quiet.

- Pressure: Imbalance force on mid-price (bid depth vs ask depth).
  Positive pressure = bids dominate = price pushed up.
  Negative pressure = asks dominate = price pushed down.

- Entropy: Disorder in the book. Uniformly distributed orders = high entropy
  = stable. Concentrated at one level = low entropy = fragile.

- Phase transitions: When the book's statistical properties cross critical
  thresholds, the MICROSTRUCTURE changes — liquidity evaporates, spreads
  blow out, cascades begin. Detect the boiling point BEFORE it completes.

Uses Brainiac's depth data from D:/CommandCenter/brainiac/depth/
"""

import math
import time


class BoltzmannEngine:
    def __init__(self):
        self.history = []       # [{ts, temp, pressure, entropy, ...}]
        self.max_history = 500
        # Phase transition thresholds (calibrated over time)
        self._temp_ma = []      # moving average for anomaly detection
        self._entropy_ma = []

    def analyze(self, depth_snapshot):
        """
        Analyze an order book depth snapshot.

        depth_snapshot: {
            'pair': 'BTC/USD',
            'bids': [[price, volume], ...],  # sorted desc by price
            'asks': [[price, volume], ...],  # sorted asc by price
            'mid_price': float,
            'spread': float,
        }

        Returns full thermodynamic state of the order book.
        """
        bids = depth_snapshot.get('bids', [])
        asks = depth_snapshot.get('asks', [])
        mid = depth_snapshot.get('mid_price', 0)
        spread = depth_snapshot.get('spread', 0)

        if not bids or not asks or mid <= 0:
            return self._default()

        # === TEMPERATURE ===
        # Average "kinetic energy" = distance_from_mid * volume
        # Weighted by volume — large orders far from mid = very hot

        bid_energies = []
        for price, vol in bids:
            distance = abs(mid - price) / mid  # normalized distance
            bid_energies.append(distance * vol)

        ask_energies = []
        for price, vol in asks:
            distance = abs(price - mid) / mid
            ask_energies.append(distance * vol)

        all_energies = bid_energies + ask_energies
        temperature = (sum(all_energies) / len(all_energies)
                      if all_energies else 0)

        # Spread contributes to temperature
        spread_normalized = spread / mid if mid > 0 else 0
        temperature += spread_normalized * 100

        # === PRESSURE ===
        # Net force on mid-price from bid/ask imbalance
        # Weight orders by inverse distance (closer orders exert more force)

        bid_force = 0
        for price, vol in bids:
            dist = max(abs(mid - price) / mid, 1e-8)
            bid_force += vol / dist  # force = volume / distance

        ask_force = 0
        for price, vol in asks:
            dist = max(abs(price - mid) / mid, 1e-8)
            ask_force += vol / dist

        total_force = bid_force + ask_force
        if total_force > 0:
            pressure = (bid_force - ask_force) / total_force  # -1 to +1
        else:
            pressure = 0

        # === ENTROPY ===
        # How uniformly distributed are the orders?
        # High entropy = well-distributed = resilient
        # Low entropy = concentrated = fragile

        bid_volumes = [v for _, v in bids if v > 0]
        ask_volumes = [v for _, v in asks if v > 0]
        all_volumes = bid_volumes + ask_volumes

        entropy = self._volume_entropy(all_volumes)
        bid_entropy = self._volume_entropy(bid_volumes)
        ask_entropy = self._volume_entropy(ask_volumes)

        # Entropy asymmetry — if one side is more concentrated than the other
        entropy_asymmetry = bid_entropy - ask_entropy

        # === DEPTH PROFILE ===
        bid_depth_total = sum(v for _, v in bids)
        ask_depth_total = sum(v for _, v in asks)
        depth_ratio = (bid_depth_total / ask_depth_total
                      if ask_depth_total > 0 else 1)

        # Near-touch liquidity (within 0.1% of mid)
        near_threshold = mid * 0.001
        near_bid = sum(v for p, v in bids if abs(mid - p) <= near_threshold)
        near_ask = sum(v for p, v in asks if abs(p - mid) <= near_threshold)
        near_imbalance = ((near_bid - near_ask) / max(near_bid + near_ask, 1e-10))

        # === PHASE TRANSITION DETECTION ===
        # Track temperature and entropy over time
        # Sudden temperature spike + entropy drop = phase transition
        self._temp_ma.append(temperature)
        self._entropy_ma.append(entropy)
        self._temp_ma = self._temp_ma[-100:]
        self._entropy_ma = self._entropy_ma[-100:]

        phase = self._detect_phase_transition(temperature, entropy)

        # === BOLTZMANN DISTRIBUTION FIT ===
        # In thermal equilibrium, order density follows Boltzmann: n(E) ~ exp(-E/kT)
        # Deviation from Boltzmann = out-of-equilibrium = interesting
        boltzmann_deviation = self._boltzmann_fit(all_energies, temperature)

        # === CLASSIFICATION ===
        interpretation = self._classify(
            temperature, pressure, entropy, phase,
            near_imbalance, boltzmann_deviation
        )

        result = {
            'temperature': round(temperature, 6),
            'pressure': round(pressure, 4),
            'entropy': round(entropy, 4),
            'bid_entropy': round(bid_entropy, 4),
            'ask_entropy': round(ask_entropy, 4),
            'entropy_asymmetry': round(entropy_asymmetry, 4),
            'depth_ratio': round(depth_ratio, 4),
            'near_imbalance': round(near_imbalance, 4),
            'spread_bps': round(spread_normalized * 10000, 2),
            'phase': phase,
            'boltzmann_deviation': round(boltzmann_deviation, 4),
            'interpretation': interpretation,
        }

        self.history.append({**result, 'ts': time.time()})
        self.history = self.history[-self.max_history:]

        return result

    def _volume_entropy(self, volumes):
        """Shannon entropy of volume distribution (normalized)."""
        if not volumes or len(volumes) < 2:
            return 0
        total = sum(volumes)
        if total <= 0:
            return 0
        h = 0
        for v in volumes:
            p = v / total
            if p > 0:
                h -= p * math.log(p)
        # Normalize by max possible entropy
        max_h = math.log(len(volumes))
        return h / max_h if max_h > 0 else 0

    def _detect_phase_transition(self, temperature, entropy):
        """
        Detect phase transitions in the order book.

        Phases:
        - SOLID: Low temp, high entropy — tight, stable, well-distributed
        - LIQUID: Medium temp, medium entropy — normal trading
        - GAS: High temp, low entropy — volatile, concentrated, unstable
        - PLASMA: Extreme temp — liquidity crisis, book disintegrating

        Transitions between phases are where the action is.
        """
        if len(self._temp_ma) < 10:
            return "UNKNOWN"

        avg_temp = sum(self._temp_ma[-20:]) / min(len(self._temp_ma), 20)
        avg_entropy = sum(self._entropy_ma[-20:]) / min(len(self._entropy_ma), 20)

        # Temperature anomaly
        temp_std = self._std(self._temp_ma[-50:])
        temp_z = ((temperature - avg_temp) / temp_std
                  if temp_std > 0 else 0)

        # Entropy anomaly
        ent_std = self._std(self._entropy_ma[-50:])
        ent_z = ((entropy - avg_entropy) / ent_std
                 if ent_std > 0 else 0)

        # Phase classification
        if temp_z > 3:
            return "PLASMA"         # extreme — liquidity crisis
        elif temp_z > 2 and ent_z < -1:
            return "BOILING"        # phase transition in progress
        elif temperature > avg_temp * 1.5 and entropy < avg_entropy * 0.7:
            return "GAS"            # volatile, fragile
        elif temp_z < -1 and ent_z > 1:
            return "FREEZING"       # becoming very stable
        elif temperature < avg_temp * 0.5 and entropy > avg_entropy * 1.3:
            return "SOLID"          # tight, stable
        elif abs(temp_z) < 1 and abs(ent_z) < 1:
            return "LIQUID"         # normal trading
        else:
            return "TRANSITIONING"  # between states

    def _boltzmann_fit(self, energies, temperature):
        """
        Measure deviation from Boltzmann distribution.
        In equilibrium: P(E) ~ exp(-E/kT)
        Deviation > 0 means the book is out of equilibrium.
        """
        if not energies or temperature <= 0:
            return 0

        # Sort energies into bins
        n_bins = 10
        max_e = max(energies) if energies else 1
        if max_e <= 0:
            return 0

        observed = [0] * n_bins
        for e in energies:
            idx = min(int(e / max_e * n_bins), n_bins - 1)
            observed[idx] += 1

        total = sum(observed)
        if total == 0:
            return 0

        # Expected Boltzmann distribution
        kt = temperature if temperature > 0 else 1e-10
        expected = []
        for i in range(n_bins):
            e_center = (i + 0.5) / n_bins * max_e
            expected.append(math.exp(-e_center / kt))

        exp_total = sum(expected)
        if exp_total <= 0:
            return 0

        # Chi-squared-like deviation
        deviation = 0
        for i in range(n_bins):
            obs = observed[i] / total
            exp_val = expected[i] / exp_total
            if exp_val > 0:
                deviation += (obs - exp_val) ** 2 / exp_val

        return deviation

    def _std(self, values):
        if len(values) < 2:
            return 0
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return math.sqrt(variance)

    def _classify(self, temp, pressure, entropy, phase,
                  near_imbalance, boltzmann_dev):
        if phase == "PLASMA":
            return "LIQUIDITY_CRISIS"
        elif phase == "BOILING":
            return "PHASE_TRANSITION_ACTIVE"
        elif phase == "GAS" and abs(pressure) > 0.5:
            direction = "BULLISH" if pressure > 0 else "BEARISH"
            return f"VOLATILE_{direction}_PRESSURE"
        elif phase == "SOLID" and abs(pressure) < 0.2:
            return "STABLE_EQUILIBRIUM"
        elif abs(near_imbalance) > 0.6:
            side = "BID" if near_imbalance > 0 else "ASK"
            return f"NEAR_TOUCH_{side}_HEAVY"
        elif boltzmann_dev > 0.5:
            return "OUT_OF_EQUILIBRIUM"
        elif phase == "LIQUID":
            return "NORMAL_TRADING"
        else:
            return "TRANSITIONING"

    def analyze_aggregated(self, agg_snapshot):
        """
        Analyze from Brainiac's aggregated depth data (no raw bid/ask arrays).

        agg_snapshot: {
            'pair': 'BTC/USD',
            'bid_depth_usd': float,
            'ask_depth_usd': float,
            'imbalance': float,      # -1 to +1
            'spread_pct': float,     # e.g. 0.0001
            'levels': int,
        }
        """
        bid_depth = agg_snapshot.get('bid_depth_usd', 0)
        ask_depth = agg_snapshot.get('ask_depth_usd', 0)
        imbalance = agg_snapshot.get('imbalance', 0)
        spread_pct = agg_snapshot.get('spread_pct', 0)
        levels = agg_snapshot.get('levels', 0)

        if bid_depth <= 0 and ask_depth <= 0:
            return self._default()

        total_depth = bid_depth + ask_depth

        # Temperature: spread is the primary heat signal
        # Wide spread = hot (market makers stepping back)
        # Thin depth relative to typical = hot
        temperature = spread_pct * 10000  # convert to bps as base
        # Depth asymmetry adds heat
        if total_depth > 0:
            depth_skew = abs(bid_depth - ask_depth) / total_depth
            temperature += depth_skew * 50

        # Pressure: imbalance directly maps to directional pressure
        pressure = imbalance  # already -1 to +1

        # Entropy proxy: levels count + depth balance
        # More levels = more distributed = higher entropy
        # Balanced depth = higher entropy
        if levels > 0:
            level_entropy = min(1.0, math.log(levels + 1) / math.log(50))
        else:
            level_entropy = 0
        balance = 1.0 - abs(imbalance)  # 1 = perfectly balanced
        entropy = (level_entropy * 0.6 + balance * 0.4)

        # Depth ratio
        depth_ratio = bid_depth / ask_depth if ask_depth > 0 else 1.0

        # Near imbalance (use raw imbalance as proxy)
        near_imbalance = imbalance

        # Spread in bps
        spread_bps = spread_pct * 10000

        # Track for phase detection
        self._temp_ma.append(temperature)
        self._entropy_ma.append(entropy)
        self._temp_ma = self._temp_ma[-100:]
        self._entropy_ma = self._entropy_ma[-100:]

        phase = self._detect_phase_transition(temperature, entropy)
        boltzmann_deviation = 0  # can't compute without raw data

        interpretation = self._classify(
            temperature, pressure, entropy, phase,
            near_imbalance, boltzmann_deviation
        )

        result = {
            'temperature': round(temperature, 6),
            'pressure': round(pressure, 4),
            'entropy': round(entropy, 4),
            'bid_entropy': round(entropy * (1 + imbalance * 0.1), 4),
            'ask_entropy': round(entropy * (1 - imbalance * 0.1), 4),
            'entropy_asymmetry': round(imbalance * 0.1, 4),
            'depth_ratio': round(depth_ratio, 4),
            'near_imbalance': round(near_imbalance, 4),
            'spread_bps': round(spread_bps, 2),
            'phase': phase,
            'boltzmann_deviation': 0,
            'interpretation': interpretation,
        }

        self.history.append({**result, 'ts': time.time()})
        self.history = self.history[-self.max_history:]

        return result

    def _default(self):
        return {
            'temperature': 0, 'pressure': 0, 'entropy': 0,
            'bid_entropy': 0, 'ask_entropy': 0, 'entropy_asymmetry': 0,
            'depth_ratio': 1, 'near_imbalance': 0, 'spread_bps': 0,
            'phase': 'UNKNOWN', 'boltzmann_deviation': 0,
            'interpretation': 'INSUFFICIENT_DATA',
        }
