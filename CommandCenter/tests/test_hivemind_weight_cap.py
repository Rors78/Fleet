"""HiveMind's max_weight cap was undone by the renormalization below it.

The optimizer capped weights at config.max_weight and then, 24 lines later,
did `consensus /= total`. Capping REMOVES mass, so total < 1.0, and
dividing scales every weight back up — including the ones just capped.

Measured with a concentrated portfolio, which is the case the cap exists
for (max_weight 0.35):

    one dominant asset:  0.35 after cap -> 0.4667 after renormalize
                         (+33.3% over the declared ceiling)
    two dominant assets: 0.35 after cap -> 0.4217 (+20.5%)

Callers that trust max_weight as an invariant for position sizing were
over-concentrated into the top-ranked asset by a third.

Note the fixture matters: a flat/random weight vector never reaches the
cap, so the cap never engages and no breach appears. Testing this with
random weights shows nothing wrong — the first fixture I tried did exactly
that. The bug only surfaces on the portfolios the cap is meant to constrain.

The fix alternates cap-and-renormalize until the cap holds, redistributing
the freed mass across the UNCAPPED names rather than back into the capped
one. Both invariants then hold at once: max <= cap AND sum == 1.0.
"""
import re
import sys

import numpy as np

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


SRC = open('D:/HiveMind/hive_mind.py', encoding='utf-8',
           errors='replace').read()

# ── 1. The bare renormalization must be gone ──
check(re.search(r'# Final renormalization\s*\n\s*total = np\.sum\(consensus\)\s*\n'
                r'\s*if total > 0:\s*\n\s*consensus /= total\s*\n\s*else:', SRC) is None,
      'the bare `consensus /= total` is back — it scales capped weights '
      'straight back over max_weight')
check('cap-preserving' in SRC or 'Cap and renormalize alternately' in SRC,
      'the renormalization must be cap-preserving')
check('_over = consensus > _cap' in SRC,
      'the fix must detect weights that exceed the cap AFTER normalizing')

# ── 2. Behavioural: mirror the shipped sequence ──
MAXW, MINW = 0.35, 0.02


def consensus_weights(raw, max_w=MAXW, min_w=MINW):
    """Mirror of the shipped cap -> normalize -> re-cap loop."""
    w = np.array(raw, dtype=float)
    w /= w.sum()
    w[w < min_w] = 0
    w = np.minimum(w, max_w)
    total = float(np.sum(w))
    if total <= 0:
        return np.ones(len(raw)) / len(raw)
    w /= total
    active = int(np.sum(w > 0))
    if active > 0 and max_w * active >= 1.0 - 1e-9:
        for _ in range(50):
            over = w > max_w + 1e-12
            if not over.any():
                break
            w = np.minimum(w, max_w)
            slack = 1.0 - float(np.sum(w))
            free = (w > 0) & ~over
            fs = float(np.sum(w[free]))
            if slack <= 1e-12 or fs <= 1e-12:
                break
            w[free] += w[free] / fs * slack
    return w


# The case the cap exists for.
for label, raw in (('one dominant', [0.60, 0.15, 0.10, 0.08, 0.04, 0.03]),
                   ('two dominant', [0.45, 0.40, 0.06, 0.04, 0.03, 0.02]),
                   ('three heavy',  [0.34, 0.33, 0.31, 0.01, 0.01, 0.00])):
    w = consensus_weights(raw)
    check(w.max() <= MAXW + 1e-9,
          '[%s] no weight may exceed max_weight %.2f after renormalizing; '
          'got %.4f (+%.1f%% over)'
          % (label, MAXW, w.max(), (w.max() / MAXW - 1) * 100))
    check(abs(float(np.sum(w)) - 1.0) < 1e-9,
          '[%s] weights must still sum to 1.0 — enforcing the cap must not '
          'break the budget; got %.9f' % (label, float(np.sum(w))))

# A portfolio that never reaches the cap must pass through UNCHANGED.
_mild = [0.30, 0.25, 0.20, 0.15, 0.06, 0.04]
_w = consensus_weights(_mild)
_expect = np.array(_mild) / sum(_mild)
check(np.allclose(_w, _expect, atol=1e-9),
      'a portfolio below the cap must be untouched by the cap logic — the '
      'fix must not perturb weights it has no business changing; got %r'
      % (_w,))

# The freed mass must go to the UNCAPPED names, not back to the capped one.
_w2 = consensus_weights([0.60, 0.15, 0.10, 0.08, 0.04, 0.03])
check(abs(_w2[0] - MAXW) < 1e-9,
      'the dominant name must sit exactly AT the cap, got %.4f' % _w2[0])
check(_w2[1] > 0.15,
      'the excess must be redistributed to the uncapped names — that is '
      'what the cap is for; second weight %.4f should exceed its raw share'
      % _w2[1])

# ── 3. An unsatisfiable cap must not loop forever or silently mislead ──
# 3 active assets with a 0.20 cap cannot sum to 1.0.
_w3 = consensus_weights([0.5, 0.3, 0.2], max_w=0.20)
check(abs(float(np.sum(_w3)) - 1.0) < 1e-9,
      'an unsatisfiable cap must still return a normalized vector rather '
      'than looping or emitting garbage; sum=%r' % float(np.sum(_w3)))
check('unsatisfiable' in SRC,
      'an unsatisfiable max_weight must be reported — silently normalizing '
      'without the cap is the same class of lie as breaching it')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  max_weight holds after renormalization, and the budget still sums to 1')
