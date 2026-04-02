"""
LORENZ — Strange Attractors and Deterministic Chaos

The market isn't random. It isn't ordered either. It's CHAOTIC --
deterministic but unpredictable. Lorenz finds the ATTRACTOR.

Every chaotic system orbits a strange attractor -- a shape in phase space
that the system never exactly repeats but never leaves. The market has an
attractor. Its shape changes with regime.

Key outputs:

- Lyapunov Exponent: How fast nearby trajectories diverge.
  Positive = chaotic. Large positive = VERY chaotic.
  Small positive = mildly chaotic (edge exists).
  Negative = stable/periodic (strong edge).
  THIS IS THE CONFIDENCE SCORE -- measured from data, not assumptions.

- Attractor Dimension: Low = simple dynamics, predictable short-term.
  High = complex dynamics, harder to predict.

- Predictability Horizon: How many steps ahead can you see before
  prediction error exceeds the attractor size? This is the maximum
  useful forecast window.

- Attractor Departure: When the system trajectory moves AWAY from the
  attractor -- this is what happens in regime changes. The old attractor
  dissolves and a new one forms.
"""

import math


class LorenzEngine:
    def __init__(self, embedding_dim=5, delay=3, n_neighbors=5):
        self.embedding_dim = embedding_dim
        self.delay = delay
        self.n_neighbors = n_neighbors  # for Lyapunov estimation

    def analyze(self, pair, candles):
        """
        Full chaos analysis of a price series.

        candles: list of [ts, open, high, low, close, volume]
        """
        min_needed = self.embedding_dim * self.delay + 80
        if len(candles) < min_needed:
            return self._default()

        closes = [float(c[4]) for c in candles]
        returns = [(closes[i] - closes[i - 1]) / (closes[i - 1] + 1e-10)
                   for i in range(1, len(closes))]

        if len(returns) < min_needed:
            return self._default()

        # === STEP 1: TAKENS EMBEDDING ===
        points = self._embed(returns)
        if len(points) < 50:
            return self._default()

        # Normalize
        points = self._normalize(points)
        n = len(points)

        # === STEP 2: MAXIMAL LYAPUNOV EXPONENT ===
        lyapunov = self._lyapunov_exponent(points)

        # === STEP 3: CORRELATION DIMENSION (attractor dimension) ===
        # Reuse approach from topology but on the embedded series
        attractor_dim = self._correlation_dimension(points)

        # === STEP 4: PREDICTABILITY HORIZON ===
        # Time steps before prediction error exceeds attractor diameter
        if lyapunov > 0:
            # Error grows as e^(lambda * t)
            # Attractor diameter ~ 1 (normalized)
            # Predict until error ~ 0.5 (half the attractor)
            horizon = math.log(0.5 / 0.01) / lyapunov  # starting from 1% error
            horizon = max(1, min(200, horizon))
        else:
            horizon = 200  # stable system — long horizon

        # === STEP 5: ATTRACTOR DEPARTURE ===
        # Is the current trajectory moving away from the attractor center?
        departure = self._attractor_departure(points)

        # === STEP 6: LOCAL DIVERGENCE RATE ===
        # Lyapunov exponent for just the RECENT portion
        recent_n = min(80, n // 2)
        recent_points = points[-recent_n:]
        local_lyapunov = self._lyapunov_exponent(recent_points)

        # Divergence trend: is chaos increasing or decreasing?
        chaos_trend = local_lyapunov - lyapunov

        # === STEP 7: RECURRENCE QUANTIFICATION ===
        # How often does the trajectory return near previous states?
        determinism = self._determinism(points)

        # === CLASSIFICATION ===
        interpretation = self._classify(
            lyapunov, local_lyapunov, attractor_dim,
            departure, determinism, chaos_trend
        )

        # Confidence score: inverse of Lyapunov
        # High Lyapunov = low confidence, low Lyapunov = high confidence
        if lyapunov > 0:
            confidence = max(0, 1 - lyapunov * 2)
        else:
            confidence = min(1, 1 + abs(lyapunov) * 2)

        return {
            'lyapunov_exponent': round(lyapunov, 6),
            'local_lyapunov': round(local_lyapunov, 6),
            'chaos_trend': round(chaos_trend, 6),
            'attractor_dimension': round(attractor_dim, 3),
            'predictability_horizon': round(horizon, 1),
            'attractor_departure': round(departure, 4),
            'determinism': round(determinism, 4),
            'confidence': round(confidence, 3),
            'interpretation': interpretation,
        }

    def _embed(self, data):
        """Takens delay embedding."""
        points = []
        max_idx = len(data) - (self.embedding_dim - 1) * self.delay
        for i in range(max_idx):
            point = [data[i + j * self.delay] for j in range(self.embedding_dim)]
            points.append(point)
        return points

    def _normalize(self, points):
        dim = len(points[0])
        mins = [min(p[d] for p in points) for d in range(dim)]
        maxs = [max(p[d] for p in points) for d in range(dim)]
        rngs = [maxs[d] - mins[d] if maxs[d] != mins[d] else 1
                for d in range(dim)]
        return [[(p[d] - mins[d]) / rngs[d] for d in range(dim)]
                for p in points]

    def _dist(self, a, b):
        return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(len(a))))

    def _lyapunov_exponent(self, points):
        """
        Estimate maximal Lyapunov exponent using Rosenstein's method.

        For each point, find its nearest neighbor (excluding temporal neighbors).
        Track how fast the distance between them grows over time.
        Average growth rate = Lyapunov exponent.
        """
        n = len(points)
        if n < 30:
            return 0

        min_temporal_sep = self.delay * 2  # avoid temporally close neighbors
        divergences = []

        # Sample points (not all, for performance)
        step = max(1, n // 60)
        for i in range(0, n - 20, step):
            # Find nearest neighbor (not temporally adjacent)
            best_dist = float('inf')
            best_j = -1
            for j in range(n - 20):
                if abs(i - j) < min_temporal_sep:
                    continue
                d = self._dist(points[i], points[j])
                if 0 < d < best_dist:
                    best_dist = d
                    best_j = j

            if best_j < 0 or best_dist <= 0:
                continue

            # Track divergence over time steps
            max_steps = min(15, n - max(i, best_j) - 1)
            for k in range(1, max_steps):
                if i + k >= n or best_j + k >= n:
                    break
                new_dist = self._dist(points[i + k], points[best_j + k])
                if new_dist > 0 and best_dist > 0:
                    divergences.append(math.log(new_dist / best_dist) / k)

        if not divergences:
            return 0

        # Median is more robust than mean for Lyapunov estimation
        divergences.sort()
        return divergences[len(divergences) // 2]

    def _correlation_dimension(self, points):
        """Estimate attractor dimension from correlation integral."""
        n = min(len(points), 80)
        pts = points[-n:]

        # Compute all pairwise distances
        dists = []
        for i in range(n):
            for j in range(i + 1, n):
                dists.append(self._dist(pts[i], pts[j]))

        if not dists:
            return 1.0

        dists.sort()

        # Correlation integral at different radii
        radii = [dists[int(len(dists) * f)]
                 for f in [0.1, 0.2, 0.35, 0.5]
                 if int(len(dists) * f) < len(dists)]

        if len(radii) < 2:
            return 1.0

        total_pairs = n * (n - 1) / 2
        counts = []
        for r in radii:
            c = sum(1 for d in dists if d <= r) / total_pairs
            counts.append(max(c, 1e-10))

        # Log-log regression
        valid = [(r, c) for r, c in zip(radii, counts) if r > 0 and c > 0]
        if len(valid) < 2:
            return 1.5

        log_r = [math.log(r) for r, _ in valid]
        log_c = [math.log(c) for _, c in valid]

        n_pts = len(log_r)
        sx = sum(log_r)
        sy = sum(log_c)
        sxy = sum(log_r[i] * log_c[i] for i in range(n_pts))
        sxx = sum(x ** 2 for x in log_r)

        denom = n_pts * sxx - sx ** 2
        if denom == 0:
            return 1.5

        slope = (n_pts * sxy - sx * sy) / denom
        return max(0.1, min(self.embedding_dim, slope))

    def _attractor_departure(self, points):
        """
        Measure how far the current trajectory is from the attractor center.
        High departure = the system may be leaving the attractor (regime change).
        """
        n = len(points)
        if n < 20:
            return 0

        # Attractor center = centroid of historical points
        dim = len(points[0])
        centroid = [sum(p[d] for p in points) / n for d in range(dim)]

        # Historical average distance to centroid
        all_dists = [self._dist(p, centroid) for p in points]
        avg_dist = sum(all_dists) / n
        std_dist = math.sqrt(
            sum((d - avg_dist) ** 2 for d in all_dists) / n
        ) if n > 1 else 1

        # Current distance (average of last 5 points)
        recent_dist = sum(all_dists[-5:]) / min(5, n)

        # Z-score of departure
        if std_dist > 0:
            return (recent_dist - avg_dist) / std_dist
        return 0

    def _determinism(self, points):
        """
        Determinism from recurrence plot.
        High determinism = trajectories follow predictable paths.
        Low determinism = trajectories are erratic.
        """
        n = min(len(points), 60)
        pts = points[-n:]

        # Build recurrence matrix with threshold = 15th percentile distance
        dists = []
        for i in range(n):
            for j in range(i + 1, n):
                dists.append(self._dist(pts[i], pts[j]))

        if not dists:
            return 0

        dists.sort()
        threshold = dists[int(len(dists) * 0.15)]

        # Count diagonal lines in recurrence plot
        # Diagonal lines = deterministic behavior
        recurrence = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i != j and self._dist(pts[i], pts[j]) <= threshold:
                    recurrence[i][j] = 1

        # Count points on diagonals of length >= 2
        diag_points = 0
        total_recurrence = 0
        for i in range(n):
            for j in range(n):
                total_recurrence += recurrence[i][j]
                # Check if part of a diagonal line
                if (recurrence[i][j] == 1
                        and i + 1 < n and j + 1 < n
                        and recurrence[i + 1][j + 1] == 1):
                    diag_points += 1

        if total_recurrence == 0:
            return 0
        return diag_points / total_recurrence

    def _classify(self, lyap, local_lyap, dim, departure, determ, trend):
        if departure > 2:
            return "ATTRACTOR_DEPARTURE"
        elif lyap > 0.3:
            return "HIGH_CHAOS"
        elif lyap > 0.1 and trend > 0.05:
            return "CHAOS_INCREASING"
        elif lyap > 0.1 and trend < -0.05:
            return "CHAOS_DECREASING"
        elif lyap > 0.05:
            return "MILD_CHAOS"
        elif lyap < 0 and determ > 0.5:
            return "PERIODIC"
        elif lyap < 0:
            return "STABLE"
        elif determ > 0.6:
            return "DETERMINISTIC"
        else:
            return "EDGE_OF_CHAOS"

    def _default(self):
        return {
            'lyapunov_exponent': 0, 'local_lyapunov': 0, 'chaos_trend': 0,
            'attractor_dimension': 0, 'predictability_horizon': 0,
            'attractor_departure': 0, 'determinism': 0,
            'confidence': 0.5, 'interpretation': 'INSUFFICIENT_DATA',
        }
