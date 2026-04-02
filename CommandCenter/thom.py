"""
THOM — Catastrophe Theory

Regime changes aren't gradual transitions. They're CATASTROPHES --
sudden, discontinuous jumps between equilibria. Rene Thom classified
ALL possible ways a smooth system can undergo sudden transitions.

Markets use three elementary catastrophes:

1. FOLD: One equilibrium disappears. The range ceases to exist.
   Price MUST move to a new level. Signature: slow approach, sudden jump.

2. CUSP: Two control parameters create a surface where the market can be
   in one of two stable states. At the cusp, the tiniest push causes a
   violent jump. This is a breakout. Signature: bimodal distribution,
   increasing variance, hysteresis.

3. BUTTERFLY: Three control parameters, up to THREE stable states.
   The market oscillates between them. This is a triangle pattern.

UNIVERSAL EARLY WARNING SIGNALS (work for ALL catastrophe types):
- Critical slowing down: recovery from perturbation takes longer
- Variance increases: fluctuations grow as the system approaches the edge
- Autocorrelation approaches 1: the system becomes "sticky"
- Skewness shifts: the distribution tilts toward the coming jump

These three numbers predict regime changes regardless of type.
"""

import math


class ThomEngine:
    def __init__(self, window=50, short_window=15):
        self.window = window
        self.short_window = short_window

    def analyze(self, pair, candles):
        """
        Catastrophe analysis of a price series.

        Returns early warning signals and catastrophe type classification.
        """
        if len(candles) < self.window + 30:
            return self._default()

        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]

        returns = [(closes[i] - closes[i - 1]) / (closes[i - 1] + 1e-10)
                   for i in range(1, len(closes))]

        if len(returns) < self.window + 20:
            return self._default()

        # === EARLY WARNING SIGNAL 1: CRITICAL SLOWING DOWN ===
        # The system takes longer to recover from perturbations
        # Measured by autocorrelation at lag 1 — approaches 1 before catastrophe

        ac_history = self._rolling_autocorrelation(returns, self.window)
        if not ac_history:
            return self._default()

        current_ac = ac_history[-1]
        ac_trend = self._trend(ac_history[-20:]) if len(ac_history) >= 20 else 0

        # === EARLY WARNING SIGNAL 2: VARIANCE INCREASE ===
        # Fluctuations grow as the system approaches the catastrophe boundary

        var_history = self._rolling_variance(returns, self.window)
        current_var = var_history[-1] if var_history else 0

        var_trend = self._trend(var_history[-20:]) if len(var_history) >= 20 else 0

        # Variance ratio: recent vs historical
        if len(var_history) >= 30:
            recent_var = sum(var_history[-10:]) / 10
            hist_var = sum(var_history[-30:-10]) / 20
            var_ratio = recent_var / hist_var if hist_var > 0 else 1
        else:
            var_ratio = 1

        # === EARLY WARNING SIGNAL 3: SKEWNESS SHIFT ===
        # The distribution tilts toward the coming jump direction

        skew_history = self._rolling_skewness(returns, self.window)
        current_skew = skew_history[-1] if skew_history else 0
        skew_trend = self._trend(skew_history[-20:]) if len(skew_history) >= 20 else 0

        # === COMBINED EARLY WARNING SCORE ===
        # All three signals combine — the more aligned, the stronger the warning

        ews_components = {
            'slowing': min(1, max(0, (current_ac - 0.3) / 0.7)),     # 0.3→1.0 maps to 0→1
            'variance': min(1, max(0, (var_ratio - 1) / 2)),          # 1→3 maps to 0→1
            'skewness': min(1, max(0, abs(current_skew) / 2)),        # 0→2 maps to 0→1
            'ac_trend': min(1, max(0, ac_trend * 50)),                # positive trend = warning
            'var_trend': min(1, max(0, var_trend * 100)),
        }

        ews_score = (
            ews_components['slowing'] * 0.35 +
            ews_components['variance'] * 0.30 +
            ews_components['skewness'] * 0.15 +
            ews_components['ac_trend'] * 0.10 +
            ews_components['var_trend'] * 0.10
        )

        # === CATASTROPHE TYPE CLASSIFICATION ===

        # Kurtosis for bimodality detection
        kurt_history = self._rolling_kurtosis(returns, self.window)
        current_kurt = kurt_history[-1] if kurt_history else 0

        # Bimodality coefficient: BC = (skew^2 + 1) / (kurt + 3)
        # BC > 5/9 suggests bimodal distribution → cusp catastrophe
        bimodality_coeff = ((current_skew ** 2 + 1)
                           / (current_kurt + 3 + 1e-10))

        # Price levels — are there two "attracting" prices? (cusp)
        level_analysis = self._detect_price_levels(closes[-self.window:])

        # Classify catastrophe type
        catastrophe = self._classify_catastrophe(
            current_ac, var_ratio, current_skew, bimodality_coeff,
            level_analysis, ews_score
        )

        # === PREDICTED DIRECTION ===
        # Skewness tells us which way the jump will go
        if current_skew > 0.5:
            predicted_direction = "UP"
            direction_confidence = min(1, abs(current_skew) / 2)
        elif current_skew < -0.5:
            predicted_direction = "DOWN"
            direction_confidence = min(1, abs(current_skew) / 2)
        else:
            predicted_direction = "UNKNOWN"
            direction_confidence = 0

        # === TIME TO CATASTROPHE ===
        # Based on rate of approach to critical thresholds
        if current_ac >= 0.9:
            time_to_crit = 0  # already at/past critical threshold
        elif ac_trend > 0:
            time_to_crit = (0.9 - current_ac) / (ac_trend + 1e-10)
            time_to_crit = max(1, min(200, time_to_crit))
        else:
            time_to_crit = -1  # not approaching

        return {
            'ews_score': round(ews_score, 4),
            'ews_components': {k: round(v, 4) for k, v in ews_components.items()},
            'autocorrelation': round(current_ac, 4),
            'ac_trend': round(ac_trend, 6),
            'variance_ratio': round(var_ratio, 4),
            'var_trend': round(var_trend, 6),
            'skewness': round(current_skew, 4),
            'skew_trend': round(skew_trend, 6),
            'kurtosis': round(current_kurt, 4),
            'bimodality_coefficient': round(bimodality_coeff, 4),
            'catastrophe_type': catastrophe,
            'predicted_direction': predicted_direction,
            'direction_confidence': round(direction_confidence, 3),
            'estimated_time_to_event': round(time_to_crit, 1),
            'n_price_levels': level_analysis['n_levels'],
            'interpretation': self._interpret(ews_score, catastrophe, time_to_crit),
        }

    def _rolling_autocorrelation(self, data, window):
        """Rolling lag-1 autocorrelation."""
        results = []
        for i in range(window, len(data)):
            segment = data[i - window:i]
            ac = self._autocorrelation(segment, lag=1)
            results.append(ac)
        return results

    def _autocorrelation(self, data, lag=1):
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

    def _rolling_variance(self, data, window):
        results = []
        for i in range(window, len(data)):
            segment = data[i - window:i]
            mean = sum(segment) / len(segment)
            var = sum((x - mean) ** 2 for x in segment) / len(segment)
            results.append(var)
        return results

    def _rolling_skewness(self, data, window):
        results = []
        for i in range(window, len(data)):
            segment = data[i - window:i]
            n = len(segment)
            mean = sum(segment) / n
            std = math.sqrt(sum((x - mean) ** 2 for x in segment) / n)
            if std > 0:
                skew = sum((x - mean) ** 3 for x in segment) / (n * std ** 3)
            else:
                skew = 0
            results.append(skew)
        return results

    def _rolling_kurtosis(self, data, window):
        results = []
        for i in range(window, len(data)):
            segment = data[i - window:i]
            n = len(segment)
            mean = sum(segment) / n
            std = math.sqrt(sum((x - mean) ** 2 for x in segment) / n)
            if std > 0:
                kurt = sum((x - mean) ** 4 for x in segment) / (n * std ** 4) - 3
            else:
                kurt = 0
            results.append(kurt)
        return results

    def _trend(self, data):
        """Simple linear regression slope."""
        n = len(data)
        if n < 2:
            return 0
        sx = sum(range(n))
        sy = sum(data)
        sxy = sum(i * data[i] for i in range(n))
        sxx = sum(i * i for i in range(n))
        denom = n * sxx - sx ** 2
        if denom == 0:
            return 0
        return (n * sxy - sx * sy) / denom

    def _detect_price_levels(self, prices):
        """Detect clustering of prices around distinct levels."""
        if len(prices) < 10:
            return {'n_levels': 1, 'levels': []}

        # Simple kernel density: find modes
        sorted_p = sorted(prices)
        rng = sorted_p[-1] - sorted_p[0]
        if rng <= 0:
            return {'n_levels': 1, 'levels': [sorted_p[0]]}

        # Bin prices and find peaks
        n_bins = 20
        bin_size = rng / n_bins
        bins = [0] * n_bins
        for p in prices:
            idx = min(int((p - sorted_p[0]) / bin_size), n_bins - 1)
            bins[idx] += 1

        # Find local maxima
        levels = []
        for i in range(1, n_bins - 1):
            if bins[i] > bins[i - 1] and bins[i] > bins[i + 1]:
                level = sorted_p[0] + (i + 0.5) * bin_size
                levels.append(level)

        # If no clear peaks, market is one-mode
        if not levels:
            levels = [sum(prices) / len(prices)]

        return {'n_levels': len(levels), 'levels': levels}

    def _classify_catastrophe(self, ac, var_ratio, skew, bimodality,
                              levels, ews):
        if ews < 0.2:
            return "NONE"

        n_levels = levels['n_levels']

        if n_levels >= 3 and bimodality > 0.5:
            return "BUTTERFLY"      # three equilibria
        elif bimodality > 0.555 or n_levels >= 2:
            return "CUSP"           # two equilibria, one about to vanish
        elif abs(skew) > 1 and var_ratio > 1.5:
            return "FOLD"           # single equilibrium about to disappear
        elif ac > 0.7 and var_ratio > 1.3:
            return "APPROACHING"    # not yet classifiable, but signals active
        else:
            return "SUBCRITICAL"    # early signals but below threshold

    def _interpret(self, ews, catastrophe, time_to_event):
        if ews > 0.7 and catastrophe != "NONE":
            return f"CRITICAL_WARNING_{catastrophe}"
        elif ews > 0.5 and time_to_event >= 0 and time_to_event < 20:
            return "IMMINENT_TRANSITION"
        elif ews > 0.5:
            return f"ELEVATED_WARNING_{catastrophe}"
        elif ews > 0.3:
            return "MILD_WARNING"
        else:
            return "STABLE"

    def _default(self):
        return {
            'ews_score': 0, 'ews_components': {},
            'autocorrelation': 0, 'ac_trend': 0,
            'variance_ratio': 1, 'var_trend': 0,
            'skewness': 0, 'skew_trend': 0,
            'kurtosis': 0, 'bimodality_coefficient': 0,
            'catastrophe_type': 'NONE',
            'predicted_direction': 'UNKNOWN',
            'direction_confidence': 0,
            'estimated_time_to_event': -1,
            'n_price_levels': 0,
            'interpretation': 'INSUFFICIENT_DATA',
        }
