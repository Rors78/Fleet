"""CAUSAL_FLOW published market beta and called it Granger causality.

NEXUS publishes causal_flow.strongest_links to the dashboard and, via
signal_broadcaster, to subscribers under the label "Granger Causality". On
2026-08-13 it was publishing 49 links across 20 series with the top five all
at strength 1.000 -- claims like "BTC/USD_volume causes PROS/USD_volume".

The engine could not support any of it. Three compounding defects:

1. NOT A REGRESSION. _ar_with_exog_error predicted y[t] as a fixed blend
   0.6*mean(y's last `lag`) + 0.4*mean(x's last `lag`), with no fitted
   coefficients. Adding any second series hovering around the same level
   reduced squared error by SMOOTHING, not by carrying information. Measured
   on the shipped code: two series sharing nothing but their MEAN produced a
   claimed 9.3% improvement, and the engine reported causation in BOTH
   directions at once (X->Y 0.2244, Y->X 0.2540). Mutual maximal causation is
   impossible; it proved the statistic had no directional content.

2. NO SIGNIFICANCE TEST. "improvement > 5%" judged a model with more
   regressors against one with fewer -- which always improves in-sample fit.
   That is what adding parameters does, not evidence.

3. NO COMMON-FACTOR CONTROL. Crypto pairs co-move because the market moves.
   With a shared driver, X's past genuinely does help predict Y, so the
   regression is right and the QUESTION is wrong.

Measured false-positive rates on data containing NO lagged causation at all,
40 trials x 8 series (2,240 ordered pairs):

    shared beta      shipped code    OLS + F-test    + market control
      0.50              100.0%           87.1%            0.1%
      0.90               40.7%            8.6%            0.3%
      0.99                0.0%            0.1%            0.0%
    independent          0.3%             0.5%            0.6%

The live fleet sits squarely in the damaged band: avg |correlation| 0.319,
BTC/ETH 0.90.

And the discrimination test, which is the one that matters -- a detector that
finds nothing is trivially "safe" and useless:

    world A (no causation)   old: fake link found 30/30   new: 0/30
    world B (one real link)  old: real link found 17/30   new: 30/30

The old engine found the link that did not exist more reliably than the one
that did.
"""
import random
import sys

sys.path.insert(0, 'D:/CommandCenter')

from causal_flow import CausalFlowNetwork

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


_src = open('D:/CommandCenter/causal_flow.py', encoding='utf-8',
            errors='replace').read()

# ── 1. The fixed-weight blend must be gone ──
check("0.6 * pred_y + 0.4 * pred_x" not in _src
      and "0.6*pred_y" not in _src,
      'the fixed 0.6/0.4 blend must be gone -- it is a smoothing comparison, '
      'not a regression, and it fires on any two series sharing a mean')
check("_ols" in _src,
      'the models must be fitted by least squares; without fitted '
      'coefficients the "improvement" measures smoothing')

# ── 2. There must be a significance test with a multiplicity correction ──
check("p_value" in _src and "_f_sf" in _src,
      'an F-test is required -- a larger model always fits better in sample, '
      'so a raw improvement threshold is not evidence')
check("self.max_lag" in _src and "alpha" in _src,
      'the lag sweep is a best-of-N search and the threshold must be '
      'corrected for it, or the reported significance is overstated')

# ── 3. The F distribution must be right ──
# Known critical values: F(1,10) at p=0.05 is 4.96, at p=0.01 is 10.04.
# Called through a guard: against the unfixed engine _f_sf does not exist,
# and an AttributeError here kills the run before a single assertion prints
# -- which makes a red proof read as a clean pass. Tenth time this exact
# flaw has bitten a test in this session.
_fsf = getattr(CausalFlowNetwork, "_f_sf", None)
check(_fsf is not None,
      'the engine must expose an F survival function -- without a '
      'significance test, "improvement > 5%" is not evidence of causality')
if _fsf is not None:
    for _f, _d1, _d2, _want in ((4.96, 1, 10, 0.05), (10.04, 1, 10, 0.01)):
        try:
            _got = _fsf(_f, _d1, _d2)
        except Exception as _exc:
            _got = None
            FAIL.append('F(%.2f; %d,%d) raised %r' % (_f, _d1, _d2, _exc))
        if isinstance(_got, float):
            check(abs(_got - _want) < 0.002,
                  'F(%.2f; %d,%d) must give p=%.2f, got %.4f -- every causal '
                  'claim rests on this' % (_f, _d1, _d2, _want, _got))
    try:
        check(_fsf(0.0, 1, 10) == 1.0,
              'F=0 must give p=1.0 (no evidence), not a small p')
    except Exception as _exc:
        FAIL.append('F(0) raised %r' % (_exc,))

# ── 4. Behavioural: co-moving series must NOT read as causal ──
random.seed(20260813)
N, PTS = 6, 200


def _shared_factor_net(beta):
    net = CausalFlowNetwork()
    factor, v = [0.0] * PTS, 0.0
    for i in range(PTS):
        v += random.gauss(0, 1)
        factor[i] = v
    for s in range(N):
        for i in range(PTS):
            net.record("s%d" % s,
                       100.0 + beta * factor[i] + (1 - beta) * random.gauss(0, 3))
    return net


_g = _shared_factor_net(0.5).build_causal_graph()
_links = sum(len(t) for t in _g.values())
_poss = N * (N - 1)
check(_links <= _poss * 0.10,
      'series driven by one shared factor with NO lagged causation must not '
      'read as a causal network: got %d of %d links (%.0f%%). The shipped '
      'code claimed 100%% of them at strength 1.000'
      % (_links, _poss, 100.0 * _links / _poss))

# ── 5. Behavioural: a REAL link must still be found ──
# Guards against "fixing" false positives by making the test detect nothing.
random.seed(99)
_found = 0
_TRIALS = 8
for _ in range(_TRIALS):
    factor, v = [0.0] * PTS, 0.0
    for i in range(PTS):
        v += random.gauss(0, 1)
        factor[i] = v
    series = [[100.0 + 0.5 * factor[i] + 0.5 * random.gauss(0, 3)
               for i in range(PTS)] for _ in range(N)]
    for i in range(3, PTS):
        series[1][i] += 0.8 * (series[0][i - 3] - 100.0)
    net = CausalFlowNetwork()
    for s, vals in enumerate(series):
        for v2 in vals:
            net.record("s%d" % s, v2)
    if "s1" in net.build_causal_graph().get("s0", {}):
        _found += 1
check(_found >= _TRIALS * 0.7,
      'a genuine lagged link must still be detected -- found in %d/%d trials. '
      'A detector that reports nothing is not a fix, it is a blind spot '
      'with a clean score' % (_found, _TRIALS))

# ── 6. Direction must be asymmetric ──
# Two series sharing only a mean must not "cause" each other both ways.
random.seed(7)
_net = CausalFlowNetwork()
for _ in range(200):
    _net.record("A", 100.0 + random.gauss(0, 5))
    _net.record("B", 100.0 + random.gauss(0, 5))
    _net.record("C", 100.0 + random.gauss(0, 5))
_ab = _net.test_causality("A", "B")
_ba = _net.test_causality("B", "A")
check(not (_ab['causes'] and _ba['causes']),
      'A and B share nothing but their mean, yet both directions were '
      'reported causal (A->B %s, B->A %s) -- mutual causation is not a '
      'finding, it is proof the statistic has no direction'
      % (_ab['causes'], _ba['causes']))

# ── 7. A derived market must not leak between builds ──
_n2 = CausalFlowNetwork()
for _ in range(200):
    _n2.record("X", random.gauss(100, 3))
    _n2.record("Y", random.gauss(100, 3))
    _n2.record("Z", random.gauss(100, 3))
_n2.build_causal_graph()
check(getattr(_n2, "_market", None) is None,
      'a market derived inside build_causal_graph must be cleared afterwards '
      '-- the series change every scan, and a stale control silently tests '
      "against a previous cycle's market")

# An explicitly-set market must SURVIVE, since the caller owns it.
_n3 = CausalFlowNetwork()
if hasattr(_n3, "set_market"):
    _n3.set_market([1.0] * 200)
else:
    FAIL.append('the engine must accept an explicit market control series')
for _ in range(200):
    _n3.record("X", random.gauss(100, 3))
    _n3.record("Y", random.gauss(100, 3))
    _n3.record("Z", random.gauss(100, 3))
_n3.build_causal_graph()
check(getattr(_n3, "_market", None) is not None,
      'a market set explicitly by the caller must not be cleared by a build')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  causal links are F-tested, market-controlled, and directional')
