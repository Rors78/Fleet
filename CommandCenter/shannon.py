"""
SHANNON — Information Theory Applied to Signal Chains

Your fleet generates 900+ events per day. How much of that is INFORMATION
and how much is NOISE? Shannon measures it exactly.

- Mutual Information: Between any two signals, how many bits of information
  does one contain about the other?
- Channel Capacity: Each bot-to-bot connection has a maximum useful bandwidth.
  Events beyond that capacity are noise.
- Transfer Entropy: Like Granger causality but information-theoretic.
  Measures directed information flow WITHOUT assuming linearity.
  Catches nonlinear causal relationships that Granger misses.
- Information Bottleneck: Find the MINIMUM information each bot needs
  from the fleet to make optimal decisions. Strip everything else.

Builds an information-theoretic map of the fleet. Every connection gets
a bits-per-second rating. Connections below threshold get pruned.
The fleet becomes an optimally efficient information network.
"""

import math
import time


class ShannonEngine:
    def __init__(self, n_bins=20, max_history=500):
        self.n_bins = n_bins        # histogram resolution for MI estimation
        self.max_history = max_history
        self.series = {}            # {name: [values]}
        self.event_streams = {}     # {name: [timestamps]}  for rate measurement
        self.channel_scores = {}    # {src|tgt: {mi, te, capacity}}

    def record(self, name, value):
        """Record a scalar observation from a signal source."""
        if name not in self.series:
            self.series[name] = []
        self.series[name].append(value)
        self.series[name] = self.series[name][-self.max_history:]

    def record_event(self, source, event_type):
        """Record an event occurrence for rate / capacity analysis."""
        key = f"{source}:{event_type}"
        if key not in self.event_streams:
            self.event_streams[key] = []
        self.event_streams[key].append(time.time())
        # Keep last hour
        cutoff = time.time() - 3600
        self.event_streams[key] = [t for t in self.event_streams[key] if t > cutoff]

    # ── Mutual Information ──────────────────────────────────────────

    def mutual_information(self, name_a, name_b):
        """
        Mutual information I(X;Y) in bits.
        Measures how many bits of information X contains about Y.
        0 = independent. Higher = more shared information.
        """
        a = self.series.get(name_a, [])
        b = self.series.get(name_b, [])
        n = min(len(a), len(b))
        if n < 30:
            return 0.0

        a = a[-n:]
        b = b[-n:]

        # Build 2D histogram
        joint, edges_a, edges_b = self._histogram_2d(a, b)

        # Marginals
        marg_a = [sum(joint[i][j] for j in range(self.n_bins))
                  for i in range(self.n_bins)]
        marg_b = [sum(joint[i][j] for i in range(self.n_bins))
                  for j in range(self.n_bins)]

        mi = 0.0
        for i in range(self.n_bins):
            for j in range(self.n_bins):
                pxy = joint[i][j]
                px = marg_a[i]
                py = marg_b[j]
                if pxy > 0 and px > 0 and py > 0:
                    mi += pxy * math.log2(pxy / (px * py))

        return max(0.0, round(mi, 6))

    def _histogram_2d(self, a, b):
        """Build normalized 2D histogram."""
        n = len(a)
        min_a, max_a = min(a), max(a)
        min_b, max_b = min(b), max(b)
        range_a = max_a - min_a if max_a != min_a else 1.0
        range_b = max_b - min_b if max_b != min_b else 1.0

        bins = self.n_bins
        hist = [[0] * bins for _ in range(bins)]

        for k in range(n):
            i = min(int((a[k] - min_a) / range_a * bins), bins - 1)
            j = min(int((b[k] - min_b) / range_b * bins), bins - 1)
            hist[i][j] += 1

        # Normalize with Laplace smoothing
        total = n + bins * bins * 1e-10
        for i in range(bins):
            for j in range(bins):
                hist[i][j] = (hist[i][j] + 1e-10) / total

        return hist, (min_a, max_a), (min_b, max_b)

    # ── Shannon Entropy ─────────────────────────────────────────────

    def entropy(self, name):
        """Shannon entropy H(X) in bits. Measures unpredictability."""
        data = self.series.get(name, [])
        if len(data) < 10:
            return 0.0

        # Build histogram
        hist = self._histogram_1d(data)
        h = 0.0
        for p in hist:
            if p > 0:
                h -= p * math.log2(p)
        return round(h, 4)

    def _histogram_1d(self, data):
        """Build normalized 1D histogram."""
        n = len(data)
        lo, hi = min(data), max(data)
        rng = hi - lo if hi != lo else 1.0
        bins = self.n_bins
        hist = [0] * bins
        for v in data:
            idx = min(int((v - lo) / rng * bins), bins - 1)
            hist[idx] += 1
        total = n + bins * 1e-10
        return [(h + 1e-10) / total for h in hist]

    # ── Transfer Entropy ────────────────────────────────────────────

    def transfer_entropy(self, source_name, target_name, lag=1):
        """
        Transfer entropy TE(X→Y) in bits.
        Measures directed information flow from X to Y.
        Unlike Granger causality, this catches NONLINEAR relationships.

        TE(X→Y) = H(Y_future | Y_past) - H(Y_future | Y_past, X_past)
        = how much does knowing X's past reduce uncertainty about Y's future
          beyond what Y's own past already tells you?
        """
        x = self.series.get(source_name, [])
        y = self.series.get(target_name, [])
        n = min(len(x), len(y))
        if n < 50:
            return 0.0

        x = x[-n:]
        y = y[-n:]

        # Build conditional distributions
        # We need: p(y_t, y_{t-lag}, x_{t-lag})
        # and marginals: p(y_t, y_{t-lag}), p(y_{t-lag}, x_{t-lag}), p(y_{t-lag})

        bins = min(self.n_bins, 10)  # fewer bins for 3D histogram

        # Discretize all series
        x_d = self._discretize(x, bins)
        y_d = self._discretize(y, bins)

        # Count joint occurrences
        count_yty_px = {}   # (y_t, y_past, x_past)
        count_yty_p = {}    # (y_t, y_past)
        count_y_px = {}     # (y_past, x_past)
        count_y_p = {}      # (y_past,)

        for t in range(lag, n):
            yt = y_d[t]
            yp = y_d[t - lag]
            xp = x_d[t - lag]

            key3 = (yt, yp, xp)
            key2a = (yt, yp)
            key2b = (yp, xp)
            key1 = (yp,)

            count_yty_px[key3] = count_yty_px.get(key3, 0) + 1
            count_yty_p[key2a] = count_yty_p.get(key2a, 0) + 1
            count_y_px[key2b] = count_y_px.get(key2b, 0) + 1
            count_y_p[key1] = count_y_p.get(key1, 0) + 1

        total = n - lag
        if total <= 0:
            return 0.0

        te = 0.0
        for (yt, yp, xp), c3 in count_yty_px.items():
            p_yty_px = c3 / total
            p_yty_p = count_yty_p.get((yt, yp), 1) / total
            p_y_px = count_y_px.get((yp, xp), 1) / total
            p_y_p = count_y_p.get((yp,), 1) / total

            # TE = sum p(yt, yp, xp) * log2[ p(yt|yp,xp) / p(yt|yp) ]
            # = sum p(yt, yp, xp) * log2[ p(yt,yp,xp)*p(yp) / (p(yp,xp)*p(yt,yp)) ]
            numer = p_yty_px * p_y_p
            denom = p_y_px * p_yty_p
            if numer > 0 and denom > 0:
                te += p_yty_px * math.log2(numer / denom)

        return max(0.0, round(te, 6))

    def _discretize(self, data, bins):
        """Discretize continuous data into bin indices."""
        lo, hi = min(data), max(data)
        rng = hi - lo if hi != lo else 1.0
        return [min(int((v - lo) / rng * bins), bins - 1) for v in data]

    # ── Channel Capacity ────────────────────────────────────────────

    def channel_capacity(self, source, target):
        """
        Estimate effective channel capacity between source and target.
        Capacity = mutual information / time = bits per observation.
        A channel carrying less than 0.01 bits/obs is noise.
        """
        mi = self.mutual_information(source, target)
        te_fwd = self.transfer_entropy(source, target)
        te_rev = self.transfer_entropy(target, source)

        # Net directed information flow
        net_flow = te_fwd - te_rev

        return {
            'mutual_information': mi,
            'transfer_entropy_fwd': te_fwd,
            'transfer_entropy_rev': te_rev,
            'net_flow': round(net_flow, 6),
            'direction': source if net_flow > 0 else target,
            'capacity': round(max(mi, te_fwd), 6),
            'is_noise': mi < 0.01 and te_fwd < 0.01,
        }

    # ── Event Rate Analysis ─────────────────────────────────────────

    def event_rate(self, source, event_type, window=300):
        """Events per second from a source in the given window."""
        key = f"{source}:{event_type}"
        stream = self.event_streams.get(key, [])
        cutoff = time.time() - window
        recent = [t for t in stream if t > cutoff]
        return len(recent) / window if window > 0 else 0

    def event_entropy(self, source, window=300):
        """
        Entropy of event timing from a source.
        Regular (predictable) events = low entropy.
        Bursty (unpredictable) events = high entropy.
        """
        keys = [k for k in self.event_streams if k.startswith(f"{source}:")]
        if not keys:
            return 0.0

        cutoff = time.time() - window
        all_times = []
        for k in keys:
            all_times.extend(t for t in self.event_streams[k] if t > cutoff)

        if len(all_times) < 5:
            return 0.0

        all_times.sort()

        # Compute inter-event intervals
        intervals = [all_times[i] - all_times[i - 1]
                     for i in range(1, len(all_times))]

        if not intervals:
            return 0.0

        # Histogram of intervals → entropy
        hist = self._histogram_1d(intervals)
        h = 0.0
        for p in hist:
            if p > 0:
                h -= p * math.log2(p)
        return round(h, 4)

    # ── Information Bottleneck Score ────────────────────────────────

    def bottleneck_score(self, target_name, source_names):
        """
        For a target signal (e.g., price), rank source signals by
        how much unique information they contribute.

        Returns sources ranked by marginal information gain.
        Sources at the bottom can be pruned — they add noise, not signal.
        """
        scores = []
        for source in source_names:
            mi = self.mutual_information(source, target_name)
            te = self.transfer_entropy(source, target_name)

            # Redundancy: how much of this source's info is already
            # in other sources?
            redundancy = 0
            for other in source_names:
                if other == source:
                    continue
                mi_other = self.mutual_information(source, other)
                redundancy = max(redundancy, mi_other)

            unique_info = max(0, mi - redundancy * 0.5)

            scores.append({
                'source': source,
                'mutual_information': mi,
                'transfer_entropy': te,
                'redundancy': round(redundancy, 6),
                'unique_info': round(unique_info, 6),
                'verdict': ('KEEP' if unique_info > 0.01 or te > 0.01
                           else 'PRUNE'),
            })

        scores.sort(key=lambda s: s['unique_info'], reverse=True)
        return scores

    # ── Fleet Information Map ───────────────────────────────────────

    def build_fleet_map(self):
        """
        Build the complete information-theoretic map of all recorded series.
        Every pair gets MI, TE in both directions, net flow, and noise flag.
        """
        names = list(self.series.keys())
        connections = []

        for i, src in enumerate(names):
            for j, tgt in enumerate(names):
                if i >= j:
                    continue  # skip self and duplicates
                ch = self.channel_capacity(src, tgt)
                connections.append({
                    'source': src,
                    'target': tgt,
                    **ch,
                })

        # Sort by capacity descending
        connections.sort(key=lambda c: c['capacity'], reverse=True)

        # Summary stats
        total_channels = len(connections)
        noise_channels = sum(1 for c in connections if c['is_noise'])
        total_bits = sum(c['capacity'] for c in connections)

        return {
            'connections': connections[:20],  # top 20
            'total_channels': total_channels,
            'noise_channels': noise_channels,
            'signal_channels': total_channels - noise_channels,
            'total_capacity_bits': round(total_bits, 4),
            'noise_ratio': round(noise_channels / max(1, total_channels), 3),
            'n_series': len(names),
        }

    # ── Analyze specific pair ───────────────────────────────────────

    def analyze_pair(self, source_name, target_name):
        """Full information analysis of a source→target relationship."""
        mi = self.mutual_information(source_name, target_name)
        te_fwd = self.transfer_entropy(source_name, target_name)
        te_rev = self.transfer_entropy(target_name, source_name)
        h_src = self.entropy(source_name)
        h_tgt = self.entropy(target_name)

        # Normalized MI (0-1 scale)
        max_h = max(h_src, h_tgt, 1e-10)
        nmi = mi / max_h

        return {
            'mutual_information': mi,
            'normalized_mi': round(nmi, 4),
            'transfer_entropy_fwd': te_fwd,
            'transfer_entropy_rev': te_rev,
            'net_flow': round(te_fwd - te_rev, 6),
            'entropy_source': h_src,
            'entropy_target': h_tgt,
            'interpretation': self._interpret(mi, te_fwd, te_rev, nmi),
        }

    def _interpret(self, mi, te_fwd, te_rev, nmi):
        if nmi > 0.3 and te_fwd > 0.05:
            return "STRONG_CAUSAL_LINK"
        elif nmi > 0.3 and te_fwd < 0.01:
            return "CORRELATED_NOT_CAUSAL"
        elif nmi < 0.05 and te_fwd > 0.02:
            return "HIDDEN_NONLINEAR_LINK"
        elif nmi < 0.05:
            return "INDEPENDENT"
        elif te_fwd > te_rev * 2:
            return "DIRECTED_INFLUENCE"
        elif te_rev > te_fwd * 2:
            return "REVERSE_INFLUENCE"
        else:
            return "BIDIRECTIONAL_COUPLING"
