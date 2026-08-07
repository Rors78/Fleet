"""Precision-weighted fusion must actually weight by measured precision.

GaussianBeliefFusion is documented as weighting each regime source "by inverse
variance (historical accuracy x confidence)". But `self.accuracy` was
initialised to {} and WRITTEN NOWHERE — every lookup fell through to the 0.5
default. The per-source confidence was a hardcoded 0.6 carrying the comment
"updated by accuracy tracking", tracking that did not exist.

So BOTH inputs to precision were constants. Every source had identical
precision and this "Gauss-optimal precision-weighted fusion" was
arithmetically a plain average, permanently. Live evidence: the snapshot
published 10 sources at exactly 0.1 weight each with information_gain 0.0.

The 0.0 was honest — a prior fix had already replaced a formula that faked a
gain — but it was honest about doing nothing, and the docstring's promise of
accuracy updates "post 50+ scored cycles" could never arrive.

Calls are now graded against the realized market move. This test asserts the
weighting actually differentiates, AND that the honest degenerate cases still
report nothing rather than guessing.
"""
import math
import re
import sys

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


src = open('D:/Nexus/nexus.py', encoding='utf-8', errors='replace').read()
m = re.search(r'_REGIME_SCALE = \{.*?\nclass GaussianBeliefFusion:.*?\n(?=\n# -{10}|\nclass )',
              src, re.S)
check(m is not None, 'could not extract GaussianBeliefFusion from nexus.py')
if not m:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)

ns = {'math': math}
exec(m.group(0), ns)
G = ns['GaussianBeliefFusion']

# ── The accuracy dict must actually be written ──
check('self.accuracy[source]' in src,
      'nothing writes self.accuracy — the fusion cannot learn and is a plain '
      'average forever')
check('def score_previous' in src,
      'there must be a path that grades previous calls against the market')

# ── 1. A reliable source must out-weigh an unreliable one ──
g = G()
realized = None
res = {}
for _ in range(12):
    g.score_previous(realized)
    res = g.fuse([
        {'source': 'sharp', 'regime': 'BULL', 'confidence': g.accuracy.get('sharp', 0.5)},
        {'source': 'noisy', 'regime': 'BEAR', 'confidence': g.accuracy.get('noisy', 0.5)},
    ])
    realized = 0.010   # market rose: 'sharp' was right, 'noisy' was wrong

_sharp = g.accuracy.get('sharp')
_noisy = g.accuracy.get('noisy')
check(_sharp is not None and _noisy is not None,
      'after 12 graded cycles self.accuracy must contain both sources; got %r '
      '— an empty dict means nothing writes it and the fusion is a plain '
      'average forever (the original defect)' % dict(g.accuracy))
if _sharp is not None and _noisy is not None:
    check(_sharp > _noisy,
          'the source that called the market correctly must earn higher '
          'accuracy: sharp=%.4f noisy=%.4f' % (_sharp, _noisy))
    check(_sharp > 0.8,
          'a consistently correct source must climb well above the 0.5 prior, '
          'got %.4f' % _sharp)
check(res.get('information_gain', 0) > 0,
      'differentiated weights must yield information_gain > 0; got %r — '
      '0 means the fusion is still a plain average'
      % res.get('information_gain'))

# ── 2. Smoothing: a couple of lucky hits must not produce certainty ──
g2 = G()
g2.fuse([{'source': 'lucky', 'regime': 'BULL', 'confidence': 0.5}])
g2.score_previous(0.01)
_lucky = g2.accuracy.get('lucky')
check(_lucky is not None,
      'a graded call must be recorded in self.accuracy; got %r' % dict(g2.accuracy))
if _lucky is not None:
    check(_lucky < 0.9,
          'two graded calls must not push accuracy to near-certainty (Laplace '
          'smoothing); got %.4f' % _lucky)

# ── 3. Unknown realized move must score NOTHING, not guess ──
g3 = G()
g3.fuse([{'source': 'a', 'regime': 'BULL', 'confidence': 0.5}])
g3.score_previous(None)
check(g3.accuracy_report() == {},
      'with no realized move known, nothing may be graded — a fabricated '
      'verdict is worse than no verdict')

# ── 4. Dead zone: noise must not reward a directional call ──
g4 = G()
g4.fuse([{'source': 'bulls', 'regime': 'BULL', 'confidence': 0.5},
         {'source': 'ranger', 'regime': 'RANGE', 'confidence': 0.5}])
g4.score_previous(0.0004)   # +0.04%: noise, not a move
rep = g4.accuracy_report()
check('ranger' in rep and 'bulls' in rep,
      'both calls must be graded once a realized move is known; got %r' % rep)
if 'ranger' in rep and 'bulls' in rep:
    check(rep['ranger']['correct'] == 1,
          'a RANGE call must be credited when the market genuinely went nowhere')
    check(rep['bulls']['correct'] == 0,
          'a directional call must NOT be credited for a noise-level move — '
          'that rewards luck and corrupts the weighting')

# ── 5. An untrained fusion must still report 0 gain honestly ──
g5 = G()
r5 = g5.fuse([{'source': 'x', 'regime': 'BULL', 'confidence': 0.5},
              {'source': 'y', 'regime': 'BEAR', 'confidence': 0.5}])
check(abs(r5.get('information_gain', 0.0)) < 1e-9,
      'with no grading yet, information_gain must be exactly 0 — claiming a '
      'gain from uniform weights is the bug a prior fix removed')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  fusion weights by measured accuracy (sharp=%.2f noisy=%.2f, gain=%.2f bits)'
      % (g.accuracy['sharp'], g.accuracy['noisy'], res.get('information_gain', 0)))
