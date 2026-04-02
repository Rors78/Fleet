"""
INFORMATION GEOMETRY ENGINE

The Fisher Information Metric measures the curvature of statistical
manifolds. In plain English: it measures how FAST the market's
probability distribution is changing shape.

When the market's return distribution is stable (same mean, same variance,
same skew for days), the Fisher metric is LOW -- you're traveling through
flat statistical space. Your models are reliable.

When the distribution is MORPHING (variance expanding, skew shifting,
kurtosis spiking), the Fisher metric is HIGH -- you're traveling through
curved statistical space. Your models are becoming unreliable. This is
the moment BEFORE a regime change, not after. Traditional regime detection
catches the change AFTER it happens. Fisher catches the distribution
WARPING before the change manifests in price.

This is the mathematical equivalent of feeling the earthquake's P-wave
before the S-wave hits.

Applications:
1. Pre-regime-change detection (Fisher spike = distribution morphing)
2. Model confidence decay (high Fisher = your model's assumptions are breaking)
3. Optimal rebalancing timing (rebalance when Fisher crosses threshold)
4. Signal validity scoring (signals are less trustworthy in high-curvature space)
"""

import math
import time


class InformationGeometryEngine:
    def __init__(self, window=50, step=10):
        self.window = window  # rolling window for distribution estimation
        self.step = step      # step size for Fisher computation

    def compute(self, pair, candles):
        if len(candles) < self.window + self.step + 10:
            return self._default()

        closes = [float(c[4]) for c in candles]

        # Compute returns
        returns = [(closes[i] - closes[i - 1]) / (closes[i - 1] + 1e-10)
                   for i in range(1, len(closes))]

        if len(returns) < self.window + self.step:
            return self._default()

        # === DISTRIBUTION PARAMETERS at two time points ===
        # Point A: window ending at t - step
        # Point B: window ending at t (now)

        returns_a = returns[-(self.window + self.step):-(self.step)]
        returns_b = returns[-self.window:]

        # Estimate distribution parameters: mean, variance, skewness, kurtosis
        params_a = self._estimate_distribution(returns_a)
        params_b = self._estimate_distribution(returns_b)

        # === FISHER INFORMATION METRIC ===
        # The distance between two nearby distributions on the statistical manifold
        # For Gaussian: ds^2 = (d_mu/sigma)^2 + 2(d_sigma/sigma)^2
        # Extended for skew and kurtosis

        dmu = params_b['mean'] - params_a['mean']
        dsigma = params_b['std'] - params_a['std']
        dskew = params_b['skew'] - params_a['skew']
        dkurt = params_b['kurtosis'] - params_a['kurtosis']

        sigma_avg = (params_a['std'] + params_b['std']) / 2 + 1e-10

        # Fisher metric components
        fisher_location = (dmu / sigma_avg) ** 2
        fisher_scale = 2 * (dsigma / sigma_avg) ** 2
        fisher_shape = 0.5 * (dskew ** 2 + 0.25 * dkurt ** 2)

        fisher_total = math.sqrt(fisher_location + fisher_scale + fisher_shape)

        # === GEODESIC VELOCITY ===
        # How fast are we traveling through distribution space?
        geodesic_velocity = fisher_total / (self.step + 1e-10)

        # === CURVATURE ACCELERATION ===
        # Is the Fisher metric itself changing? (second derivative)
        if len(returns) >= self.window + self.step * 2:
            returns_a2 = returns[-(self.window + self.step * 2):-(self.step * 2)]
            returns_b2 = returns[-(self.window + self.step):-(self.step)]

            params_a2 = self._estimate_distribution(returns_a2)
            params_b2 = self._estimate_distribution(returns_b2)

            dmu2 = params_b2['mean'] - params_a2['mean']
            dsigma2 = params_b2['std'] - params_a2['std']
            sigma_avg2 = (params_a2['std'] + params_b2['std']) / 2 + 1e-10

            fisher_prev = math.sqrt(
                (dmu2 / sigma_avg2) ** 2 +
                2 * (dsigma2 / sigma_avg2) ** 2
            )

            curvature_accel = (fisher_total - fisher_prev) / (self.step + 1e-10)
        else:
            curvature_accel = 0

        # === DISTRIBUTION DIVERGENCE (KL Divergence approximation) ===
        kl_div = self._kl_divergence_gaussian(params_a, params_b)

        # === ENTROPY RATE ===
        entropy = self._entropy(returns_b)
        entropy_prev = self._entropy(returns_a)
        entropy_change = entropy - entropy_prev

        # === MANIFOLD CLASSIFICATION ===
        interpretation = self._classify_manifold(
            fisher_total, geodesic_velocity, curvature_accel,
            kl_div, entropy_change, params_b
        )

        return {
            'fisher_metric': round(fisher_total, 6),
            'geodesic_velocity': round(geodesic_velocity, 6),
            'curvature_acceleration': round(curvature_accel, 6),
            'kl_divergence': round(kl_div, 6),
            'entropy': round(entropy, 4),
            'entropy_change': round(entropy_change, 4),
            'distribution': {
                'mean': round(params_b['mean'] * 10000, 4),   # basis points
                'std': round(params_b['std'] * 10000, 4),
                'skew': round(params_b['skew'], 4),
                'kurtosis': round(params_b['kurtosis'], 4),
            },
            'interpretation': interpretation,
            'model_reliability': round(max(0, 1 - fisher_total * 10), 3),
            'regime_change_probability': round(min(1, fisher_total * 5), 3),
        }

    def _estimate_distribution(self, data):
        n = len(data)
        if n == 0:
            return {'mean': 0, 'std': 0.01, 'skew': 0, 'kurtosis': 3}

        mean = sum(data) / n
        variance = sum((x - mean) ** 2 for x in data) / n
        std = math.sqrt(variance) if variance > 0 else 1e-10

        # Skewness
        if std > 0:
            skew = sum((x - mean) ** 3 for x in data) / (n * std ** 3)
        else:
            skew = 0

        # Excess kurtosis
        if std > 0:
            kurtosis = sum((x - mean) ** 4 for x in data) / (n * std ** 4) - 3
        else:
            kurtosis = 0

        return {'mean': mean, 'std': std, 'skew': skew, 'kurtosis': kurtosis}

    def _kl_divergence_gaussian(self, p, q):
        """KL divergence between two Gaussian distributions."""
        sp, sq = max(p['std'], 1e-10), max(q['std'], 1e-10)
        return (
            math.log(sq / sp) +
            (sp ** 2 + (p['mean'] - q['mean']) ** 2) / (2 * sq ** 2) -
            0.5
        )

    def _entropy(self, data):
        """Approximate entropy of a return series."""
        if len(data) < 10:
            return 0
        std = (sum((x - sum(data) / len(data)) ** 2 for x in data) / len(data)) ** 0.5
        if std <= 0:
            return 0
        # Gaussian entropy approximation
        return 0.5 * math.log(2 * math.pi * math.e * std ** 2 + 1e-10)

    def _classify_manifold(self, fisher, velocity, accel, kl, entropy_change, params):
        if accel > 0.001 and fisher > 0.05:
            return "ACCELERATING_DEFORMATION"
        elif fisher > 0.1:
            return "HIGH_CURVATURE"
        elif fisher > 0.05 and entropy_change > 0.1:
            return "ENTROPY_INJECTION"
        elif fisher > 0.05 and entropy_change < -0.1:
            return "ENTROPY_EXTRACTION"
        elif fisher < 0.01 and abs(entropy_change) < 0.02:
            return "FLAT_MANIFOLD"
        elif params['kurtosis'] > 3:
            return "FAT_TAILS_ACTIVE"
        elif abs(params['skew']) > 1:
            return "SKEWED_LEFT" if params['skew'] < 0 else "SKEWED_RIGHT"
        else:
            return "NORMAL_CURVATURE"

    def _default(self):
        return {
            'fisher_metric': 0, 'geodesic_velocity': 0,
            'curvature_acceleration': 0, 'kl_divergence': 0,
            'entropy': 0, 'entropy_change': 0,
            'distribution': {'mean': 0, 'std': 0, 'skew': 0, 'kurtosis': 0},
            'interpretation': 'INSUFFICIENT_DATA',
            'model_reliability': 1.0, 'regime_change_probability': 0,
        }
