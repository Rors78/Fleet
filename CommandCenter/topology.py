"""
TOPOLOGICAL DATA ANALYSIS -- Persistent Features in Market Structure

Traditional analysis sees price as a 1D time series.
Topology sees it as a point cloud in N-dimensional space,
where each point is a market state (price, volume, volatility, momentum, ...).

Key concept: PERSISTENT HOMOLOGY
- Imagine inflating a sphere around each point in the cloud
- As spheres grow, they connect to form structures:
  - Connected components (clusters of similar states)
  - Loops (cyclical patterns that don't fill in)
  - Voids (empty regions the market avoids)

- Features that PERSIST across many scales of sphere radius are REAL
- Features that appear and vanish quickly are NOISE

Applications:
1. Detect market cycles that aren't visible in price alone
2. Find structural support/resistance as topological features
3. Identify regime boundaries as topological phase transitions
4. Measure market complexity (number of persistent features)

This is a SIMPLIFIED version -- full persistent homology requires
specialized libraries. We implement the key insights using
distance matrices and connected component analysis.
"""

import math


class TopologicalAnalyzer:
    def __init__(self, embedding_dim=5, delay=3):
        self.embedding_dim = embedding_dim  # Takens embedding dimension
        self.delay = delay                  # Takens delay

    def analyze(self, pair, candles):
        if len(candles) < self.embedding_dim * self.delay + 50:
            return self._default()

        closes = [float(c[4]) for c in candles]

        # === STEP 1: TAKENS EMBEDDING ===
        # Convert 1D price series into high-dimensional phase space
        # Each point is [p(t), p(t-d), p(t-2d), ..., p(t-(m-1)d)]
        # This reconstructs the hidden dynamics of the system

        returns = [(closes[i] - closes[i - 1]) / (closes[i - 1] + 1e-10)
                   for i in range(1, len(closes))]

        points = self._takens_embed(returns, self.embedding_dim, self.delay)

        if len(points) < 20:
            return self._default()

        # Normalize to unit cube
        points = self._normalize_points(points)

        # === STEP 2: DISTANCE MATRIX ===
        n = len(points)
        n = min(n, 100)  # cap for performance
        points = points[-n:]

        distances = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                d = self._euclidean_distance(points[i], points[j])
                distances[i][j] = d
                distances[j][i] = d

        # === STEP 3: PERSISTENCE ANALYSIS ===
        # Track connected components at different distance thresholds
        # Features that persist across many thresholds are structural

        # Get all unique distances (sorted)
        all_dists = sorted(set(
            distances[i][j]
            for i in range(n) for j in range(i + 1, n)
        ))

        # Sample thresholds
        n_thresholds = min(50, len(all_dists))
        threshold_indices = [int(i * len(all_dists) / n_thresholds)
                            for i in range(n_thresholds)]
        thresholds = [all_dists[idx] for idx in threshold_indices
                      if idx < len(all_dists)]

        # Track component count at each threshold
        component_counts = []
        for threshold in thresholds:
            components = self._count_components(n, distances, threshold)
            component_counts.append(components)

        # === STEP 4: EXTRACT FEATURES ===

        # Feature 1: Fragmentation -- how many disconnected clusters at median distance
        median_idx = len(thresholds) // 2
        fragmentation = (component_counts[median_idx]
                        if median_idx < len(component_counts) else 1)

        # Feature 2: Persistence -- how quickly do components merge?
        if len(component_counts) >= 2:
            merge_rate = ((component_counts[0] - component_counts[-1])
                         / (len(thresholds) + 1e-10))
        else:
            merge_rate = 0

        # Feature 3: Compactness -- average nearest-neighbor distance
        nn_distances = []
        for i in range(n):
            nearest = min(distances[i][j] for j in range(n) if j != i)
            nn_distances.append(nearest)
        compactness = sum(nn_distances) / len(nn_distances)

        # Feature 4: Dimension estimate (correlation dimension)
        corr_dim = self._correlation_dimension(distances, n)

        # Feature 5: Recurrence -- how often does the system return near previous states?
        recurrence_rate = self._recurrence_rate(distances, n,
                                                threshold=compactness * 2)

        # Feature 6: Lacunarity -- gaps in the phase space
        lacunarity = self._estimate_lacunarity(points)

        # === STEP 5: TOPOLOGICAL MARKET STATE ===
        interpretation = self._interpret(
            fragmentation, merge_rate, compactness,
            corr_dim, recurrence_rate, lacunarity
        )

        return {
            'fragmentation': fragmentation,
            'merge_rate': round(merge_rate, 4),
            'compactness': round(compactness, 4),
            'correlation_dimension': round(corr_dim, 3),
            'recurrence_rate': round(recurrence_rate, 3),
            'lacunarity': round(lacunarity, 4),
            'embedding_dim': self.embedding_dim,
            'n_points': n,
            'interpretation': interpretation,
            'complexity_score': round(min(1, corr_dim / self.embedding_dim), 3),
            'cyclicality': round(recurrence_rate, 3),
        }

    def _takens_embed(self, data, dim, delay):
        points = []
        for i in range(len(data) - (dim - 1) * delay):
            point = [data[i + j * delay] for j in range(dim)]
            points.append(point)
        return points

    def _normalize_points(self, points):
        dim = len(points[0])
        mins = [min(p[d] for p in points) for d in range(dim)]
        maxs = [max(p[d] for p in points) for d in range(dim)]
        ranges = [maxs[d] - mins[d] if maxs[d] != mins[d] else 1
                  for d in range(dim)]
        return [[(p[d] - mins[d]) / ranges[d] for d in range(dim)]
                for p in points]

    def _euclidean_distance(self, a, b):
        return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(len(a))))

    def _count_components(self, n, distances, threshold):
        """Union-Find to count connected components at given threshold."""
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(n):
            for j in range(i + 1, n):
                if distances[i][j] <= threshold:
                    union(i, j)

        return len(set(find(i) for i in range(n)))

    def _correlation_dimension(self, distances, n):
        """Estimate correlation dimension from distance matrix."""
        all_dists = sorted(
            distances[i][j]
            for i in range(n) for j in range(i + 1, n)
            if distances[i][j] > 0
        )

        if len(all_dists) < 10:
            return 1.0

        # Count pairs within increasing radii
        radii = [all_dists[int(len(all_dists) * f)]
                 for f in [0.1, 0.2, 0.3, 0.5]]
        total_pairs = n * (n - 1) / 2
        counts = []

        for r in radii:
            count = sum(1 for d in all_dists if d <= r) / total_pairs
            counts.append(max(count, 1e-10))

        # Linear regression of log(count) vs log(radius)
        if (len(radii) >= 2 and all(r > 0 for r in radii)
                and all(c > 0 for c in counts)):
            log_r = [math.log(r) for r in radii]
            log_c = [math.log(c) for c in counts]

            n_pts = len(log_r)
            sum_x = sum(log_r)
            sum_y = sum(log_c)
            sum_xy = sum(log_r[i] * log_c[i] for i in range(n_pts))
            sum_xx = sum(x ** 2 for x in log_r)

            denom = n_pts * sum_xx - sum_x ** 2
            if denom != 0:
                slope = (n_pts * sum_xy - sum_x * sum_y) / denom
                return max(0.1, min(10, slope))

        return 1.5

    def _recurrence_rate(self, distances, n, threshold):
        """Fraction of point pairs that are within threshold distance."""
        count = sum(
            1 for i in range(n) for j in range(i + 1, n)
            if distances[i][j] <= threshold
        )
        total = n * (n - 1) / 2
        return count / (total + 1e-10)

    def _estimate_lacunarity(self, points):
        """Estimate lacunarity -- measure of gaps in the phase space."""
        grid_sizes = [5, 10, 20]
        lacunarities = []
        dim = len(points[0])

        for gs in grid_sizes:
            occupied = set()
            for p in points:
                cell = tuple(min(int(p[d] * gs), gs - 1) for d in range(dim))
                occupied.add(cell)

            total_cells = gs ** dim
            density = len(occupied) / (total_cells + 1e-10)

            if density > 0:
                lacunarities.append(1 / density)

        return sum(lacunarities) / len(lacunarities) if lacunarities else 1.0

    def _interpret(self, frag, merge, compact, corr_dim, recurrence, lacunarity):
        if recurrence > 0.3 and corr_dim < 2:
            return "LOW_DIMENSIONAL_CYCLE"
        elif corr_dim > 3 and lacunarity > 5:
            return "HIGH_DIMENSIONAL_CHAOS"
        elif frag > 5 and merge < 0.1:
            return "FRAGMENTED_STRUCTURE"
        elif compact < 0.1 and recurrence > 0.2:
            return "TIGHT_ATTRACTOR"
        elif lacunarity > 10:
            return "SPARSE_MANIFOLD"
        elif corr_dim < 1.5 and recurrence < 0.1:
            return "LINEAR_DRIFT"
        else:
            return "MODERATE_COMPLEXITY"

    def _default(self):
        return {
            'fragmentation': 0, 'merge_rate': 0, 'compactness': 0,
            'correlation_dimension': 0, 'recurrence_rate': 0, 'lacunarity': 0,
            'embedding_dim': self.embedding_dim, 'n_points': 0,
            'interpretation': 'INSUFFICIENT_DATA',
            'complexity_score': 0, 'cyclicality': 0,
        }
