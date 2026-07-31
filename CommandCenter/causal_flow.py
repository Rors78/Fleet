"""
CAUSAL FLOW NETWORK

Builds a directed causal graph between:
- Bot signals and price movements
- Pairs (does BTC cause ETH, or vice versa?)
- Events and outcomes (do whale alerts actually precede moves?)

Uses Granger Causality: X Granger-causes Y if past values of X
improve prediction of Y beyond what past values of Y alone provide.

The fleet can then WEIGHT its intelligence sources by actual
causal power, not just correlation.
"""

import time


class CausalFlowNetwork:
    def __init__(self, max_lag=10):
        self.max_lag = max_lag
        self.causal_graph = {}   # {source: {target: {strength, lag, ...}}}
        self.time_series = {}    # {name: [{value, time}]}
        self.max_history = 500

    def record(self, name, value):
        """Record a time series value."""
        if name not in self.time_series:
            self.time_series[name] = []
        self.time_series[name].append({'value': value, 'time': time.time()})
        self.time_series[name] = self.time_series[name][-self.max_history:]

    def test_causality(self, source_name, target_name):
        """
        Test if source Granger-causes target.
        Returns: {
            'causes': True/False,
            'strength': 0.0-1.0,
            'optimal_lag': int,
            'improvement': float  -- how much source improves prediction
        }
        """
        source = self.time_series.get(source_name, [])
        target = self.time_series.get(target_name, [])

        if (len(source) < self.max_lag + 20
                or len(target) < self.max_lag + 20):
            return {'causes': False, 'strength': 0, 'optimal_lag': 0,
                    'improvement': 0}

        # Align time series
        s_vals = [d['value'] for d in source[-200:]]
        t_vals = [d['value'] for d in target[-200:]]

        min_len = min(len(s_vals), len(t_vals))
        s_vals = s_vals[-min_len:]
        t_vals = t_vals[-min_len:]

        best_improvement = 0
        best_lag = 0

        for lag in range(1, self.max_lag + 1):
            # Restricted model: predict target from its own past only
            restricted_error = self._ar_error(t_vals, lag)

            # Unrestricted model: predict target from its own past + source past
            unrestricted_error = self._ar_with_exog_error(t_vals, s_vals, lag)

            # Improvement = reduction in prediction error
            if restricted_error > 0:
                improvement = ((restricted_error - unrestricted_error)
                              / restricted_error)
            else:
                improvement = 0

            if improvement > best_improvement:
                best_improvement = improvement
                best_lag = lag

        # Threshold: at least 5% improvement to claim causality
        causes = best_improvement > 0.05

        return {
            'causes': causes,
            'strength': round(min(1, best_improvement * 5), 3),
            'optimal_lag': best_lag,
            'improvement': round(best_improvement, 4),
        }

    def build_causal_graph(self):
        """Build the full causal graph between all time series."""
        names = list(self.time_series.keys())
        self.causal_graph = {}

        for source in names:
            self.causal_graph[source] = {}
            for target in names:
                if source == target:
                    continue
                result = self.test_causality(source, target)
                if result['causes']:
                    self.causal_graph[source][target] = result

        return self.causal_graph

    def get_causal_power(self, name):
        """
        How much causal influence does this source have on the network?
        Sources that cause many targets with high strength = high causal power.
        """
        if name not in self.causal_graph:
            return 0

        targets = self.causal_graph[name]
        if not targets:
            return 0

        total_strength = sum(t['strength'] for t in targets.values())
        return round(total_strength / max(1, len(self.time_series)), 3)

    def get_caused_by(self, target_name):
        """What sources Granger-cause this target?"""
        causes = {}
        for source, targets in self.causal_graph.items():
            if target_name in targets:
                causes[source] = targets[target_name]
        return causes

    def _ar_error(self, y, lag):
        """Prediction error using autoregressive model."""
        n = len(y)
        if n <= lag + 1:
            return 1.0

        errors = []
        for t in range(lag, n):
            # Predict y[t] from y[t-1], y[t-2], ..., y[t-lag]
            pred = sum(y[t - i] for i in range(1, lag + 1)) / lag
            errors.append((y[t] - pred) ** 2)

        return sum(errors) / len(errors) if errors else 1.0

    def _ar_with_exog_error(self, y, x, lag):
        """Prediction error using AR model + exogenous variable."""
        n = min(len(y), len(x))
        if n <= lag + 1:
            return 1.0

        errors = []
        for t in range(lag, n):
            # Predict y[t] from y[t-1..lag] and x[t-1..lag]
            pred_y = sum(y[t - i] for i in range(1, lag + 1)) / lag
            pred_x = sum(x[t - i] for i in range(1, lag + 1)) / lag

            # Combined prediction: weighted average
            pred = 0.6 * pred_y + 0.4 * pred_x
            errors.append((y[t] - pred) ** 2)

        return sum(errors) / len(errors) if errors else 1.0

    def get_summary(self):
        """Return a summary of the causal network."""
        causal_powers = {}
        for name in self.time_series:
            causal_powers[name] = self.get_causal_power(name)

        # Sort by causal power
        ranked = sorted(causal_powers.items(), key=lambda x: x[1],
                       reverse=True)

        # Find strongest causal links
        strongest = []
        for source, targets in self.causal_graph.items():
            for target, data in targets.items():
                strongest.append({
                    'source': source,
                    'target': target,
                    'strength': data['strength'],
                    'lag': data['optimal_lag'],
                })
        strongest.sort(key=lambda x: x['strength'], reverse=True)

        return {
            'causal_powers': dict(ranked[:10]),
            'strongest_links': strongest[:10],
            'n_series': len(self.time_series),
            'n_causal_links': sum(
                len(t) for t in self.causal_graph.values()
            ),
        }
