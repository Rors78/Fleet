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
        # Cross-sectional mean of all series — "the market". Without it, this
        # test cannot separate CAUSATION from shared BETA, because with a
        # common driver X's past genuinely does help predict Y: the fitted
        # regression is right and the QUESTION is wrong.
        #
        # Measured across 40 trials of 8 series, false-positive rate on data
        # containing NO lagged causation whatsoever:
        #
        #   shared factor beta=0.50   fixed-blend engine 100.0%
        #                             + real OLS + F-test  87.1%
        #                             + market control      (see below)
        #
        # The live fleet sits in that band: avg |correlation| 0.319, BTC/ETH
        # 0.90. NEXUS was publishing 49 links over 20 series, top five all at
        # strength 1.000, labelled "Granger Causality" to subscribers.
        self._market = None

    def set_market(self, values):
        """Set the common-factor control series explicitly (optional).

        When unset, build_causal_graph derives it as the cross-sectional mean
        of every recorded series.
        """
        self._market = list(values) if values else None

    def _derive_market(self, length):
        """Cross-sectional mean of all series over the last `length` points.

        Returns None with fewer than three series: with two, the "market" is
        half the pair under test, and controlling for it removes the very
        signal being measured.
        """
        cols = []
        for vals in self.time_series.values():
            v = [d['value'] for d in vals[-length:]]
            if len(v) == length:
                cols.append(v)
        if len(cols) < 3:
            return None
        return [sum(c[i] for c in cols) / len(cols) for i in range(length)]

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
        best_restricted = 0.0
        best_unrestricted = 0.0

        # The market control, aligned to the pair's window. When present it
        # goes in BOTH models, so a surviving link is one whose source adds
        # information beyond the market's own past — not one that merely
        # moves with the market.
        m_vals = None
        if self._market and len(self._market) >= min_len:
            m_vals = list(self._market)[-min_len:]

        for lag in range(1, self.max_lag + 1):
            # Restricted: the target's own past (+ the market's, when known).
            if m_vals is not None:
                restricted_error = self._ar_with_exog_error(
                    t_vals, m_vals, lag)
            else:
                restricted_error = self._ar_error(t_vals, lag)

            # Unrestricted: the same, plus the source's past.
            if m_vals is not None:
                rows, ys = [], []
                for t in range(lag, min_len):
                    rows.append(
                        [t_vals[t - i] for i in range(1, lag + 1)]
                        + [m_vals[t - i] for i in range(1, lag + 1)]
                        + [s_vals[t - i] for i in range(1, lag + 1)])
                    ys.append(t_vals[t])
                _mse = self._ols(rows, ys)
                unrestricted_error = (_mse if _mse is not None
                                      else restricted_error)
            else:
                unrestricted_error = self._ar_with_exog_error(
                    t_vals, s_vals, lag)

            # Improvement = reduction in prediction error
            if restricted_error > 0:
                improvement = ((restricted_error - unrestricted_error)
                              / restricted_error)
            else:
                improvement = 0

            if improvement > best_improvement:
                best_improvement = improvement
                best_lag = lag
                best_restricted = restricted_error
                best_unrestricted = unrestricted_error

        # A fitted model with MORE regressors always reduces in-sample error,
        # so "improvement > 5%" is not evidence of anything — it is what
        # adding parameters does. Granger's test is an F-test on whether the
        # reduction exceeds what the extra parameters buy by construction.
        #
        # And the lag sweep above is a best-of-10 search: taking the maximum
        # over ten tries and judging it at a single-test bar inflates the
        # false-positive rate. Bonferroni over the lags actually searched.
        n_obs = max(1, min_len - best_lag)
        df_num = max(1, best_lag)                      # lags of the source
        # Params in the unrestricted fit: intercept + target lags + source
        # lags, plus market lags when the control is in play.
        _n_params = (3 if m_vals is not None else 2) * best_lag + 1
        df_den = max(1, n_obs - _n_params)
        f_stat = 0.0
        if best_unrestricted > 0 and df_den > 0:
            f_stat = (((best_restricted - best_unrestricted) / df_num)
                      / (best_unrestricted / df_den))
        p_value = self._f_sf(f_stat, df_num, df_den)
        alpha = 0.01 / self.max_lag                    # Bonferroni over lags
        causes = bool(f_stat > 0 and p_value < alpha
                      and best_improvement > 0.05)

        return {
            'causes': causes,
            # Strength is now the significance, not the raw error reduction:
            # the old `improvement * 5` hit 1.000 whenever improvement passed
            # 20%, which co-moving series reached trivially. Every one of the
            # five links NEXUS published on 2026-08-13 sat at exactly 1.000.
            'strength': round(max(0.0, min(1.0, 1.0 - p_value / alpha)), 3)
                        if causes else 0.0,
            'optimal_lag': best_lag,
            'improvement': round(best_improvement, 4),
            'p_value': round(p_value, 6),
            'alpha': round(alpha, 6),
            'n_obs': n_obs,
        }

    @staticmethod
    def _f_sf(f, d1, d2):
        """P(F > f) for the F(d1, d2) distribution, via the regularized
        incomplete beta function. Pure stdlib — no scipy in the fleet.
        """
        if f <= 0 or d1 <= 0 or d2 <= 0:
            return 1.0
        x = d2 / (d2 + d1 * f)
        return CausalFlowNetwork._betainc(x, d2 / 2.0, d1 / 2.0)

    @staticmethod
    def _betainc(x, a, b):
        """Regularized incomplete beta I_x(a, b) by continued fraction."""
        import math
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        lbeta = (math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b))
        front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta)
        # Continued fraction converges fast for x < (a+1)/(a+b+2); otherwise
        # use the symmetry I_x(a,b) = 1 - I_{1-x}(b,a).
        if x > (a + 1) / (a + b + 2):
            return 1.0 - CausalFlowNetwork._betainc(1 - x, b, a)
        f, c, d = 1.0, 1.0, 0.0
        for i in range(200):
            m = i // 2
            if i == 0:
                num = 1.0
            elif i % 2 == 0:
                num = (m * (b - m) * x) / ((a + 2.0 * m - 1) * (a + 2.0 * m))
            else:
                num = (-((a + m) * (a + b + m) * x)
                       / ((a + 2.0 * m) * (a + 2.0 * m + 1)))
            d = 1.0 + num * d
            if abs(d) < 1e-30:
                d = 1e-30
            d = 1.0 / d
            c = 1.0 + num / c
            if abs(c) < 1e-30:
                c = 1e-30
            f *= c * d
            if abs(1.0 - c * d) < 1e-10:
                break
        return front * (f - 1.0) / a

    def build_causal_graph(self):
        """Build the full causal graph between all time series."""
        names = list(self.time_series.keys())
        self.causal_graph = {}

        # Derive the common-factor control once per build, unless a caller
        # set one explicitly. Every existing caller gets the control for
        # free — the alternative is publishing market beta as causation.
        if self._market is None and names:
            _len = min(len(self.time_series[n]) for n in names)
            _len = min(_len, 200)
            if _len > self.max_lag + 20:
                self._market = self._derive_market(_len)
                self._market_derived = True

        for source in names:
            self.causal_graph[source] = {}
            for target in names:
                if source == target:
                    continue
                result = self.test_causality(source, target)
                if result['causes']:
                    self.causal_graph[source][target] = result

        # A market DERIVED for this build must not survive into the next one
        # as if a caller had set it: the series change every scan, and a
        # stale control silently tests against last cycle's market.
        if getattr(self, "_market_derived", False):
            self._market = None
            self._market_derived = False

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

    @staticmethod
    def _ols(rows, ys):
        """Least-squares fit with an intercept. Returns residual MSE.

        Normal equations solved by Gauss-Jordan with partial pivoting. Falls
        back to the variance of ys when the system is singular (perfectly
        collinear regressors), because an unsolvable fit must not be reported
        as a perfect one.
        """
        if not rows or len(rows) != len(ys):
            return None
        k = len(rows[0]) + 1                     # +1 for the intercept
        if len(rows) <= k + 1:                   # not enough data to fit
            return None
        xtx = [[0.0] * k for _ in range(k)]
        xty = [0.0] * k
        for r, yv in zip(rows, ys):
            v = [1.0] + list(r)
            for i in range(k):
                xty[i] += v[i] * yv
                for j in range(k):
                    xtx[i][j] += v[i] * v[j]
        # Ridge nudge on the diagonal: keeps near-collinear systems solvable
        # without changing a well-conditioned fit meaningfully.
        for i in range(k):
            xtx[i][i] += 1e-8
        aug = [xtx[i] + [xty[i]] for i in range(k)]
        for c in range(k):
            p = max(range(c, k), key=lambda r: abs(aug[r][c]))
            if abs(aug[p][c]) < 1e-12:
                return None
            aug[c], aug[p] = aug[p], aug[c]
            pv = aug[c][c]
            aug[c] = [v / pv for v in aug[c]]
            for r in range(k):
                if r != c and aug[r][c]:
                    f = aug[r][c]
                    aug[r] = [a - f * b for a, b in zip(aug[r], aug[c])]
        beta = [aug[i][k] for i in range(k)]
        sse = 0.0
        for r, yv in zip(rows, ys):
            v = [1.0] + list(r)
            pred = sum(b * vv for b, vv in zip(beta, v))
            sse += (yv - pred) ** 2
        return sse / len(ys)

    def _ar_error(self, y, lag):
        """Residual MSE of an AR(lag) model fitted by least squares.

        WAS: a fixed unweighted mean of the last `lag` values, with no fitted
        coefficients. Paired with _ar_with_exog_error's fixed 0.6/0.4 blend,
        that made the whole test a smoothing comparison rather than a
        causality test — adding ANY second series that hovered around the
        same level reduced squared error simply by averaging away noise.

        Measured on the old code, 2026-08-13: two series sharing nothing but
        their MEAN produced a claimed 9.3% improvement, and the engine
        reported causation in BOTH directions at once (X->Y improvement
        0.2244, Y->X 0.2540). Mutual maximal causation is not a finding, it
        is proof the statistic was not measuring direction at all.
        """
        n = len(y)
        if n <= lag + 1:
            return 1.0
        rows, ys = [], []
        for t in range(lag, n):
            rows.append([y[t - i] for i in range(1, lag + 1)])
            ys.append(y[t])
        mse = self._ols(rows, ys)
        if mse is None:
            # Unfittable: report the target's own variance, the error of the
            # best constant predictor. Never 0 — a failed fit must not look
            # like a perfect one, which would make every improvement 100%.
            m = sum(ys) / len(ys) if ys else 0.0
            return (sum((v - m) ** 2 for v in ys) / len(ys)) if ys else 1.0
        return mse

    def _ar_with_exog_error(self, y, x, lag):
        """Residual MSE of AR(lag) plus `lag` lags of an exogenous series."""
        n = min(len(y), len(x))
        if n <= lag + 1:
            return 1.0
        rows, ys = [], []
        for t in range(lag, n):
            rows.append([y[t - i] for i in range(1, lag + 1)]
                        + [x[t - i] for i in range(1, lag + 1)])
            ys.append(y[t])
        mse = self._ols(rows, ys)
        if mse is None:
            # Cannot fit the larger model: fall back to the restricted one, so
            # the improvement is 0 rather than an artifact of a failed solve.
            return self._ar_error(y, lag)
        return mse

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
