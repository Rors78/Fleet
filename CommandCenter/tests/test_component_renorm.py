"""An unmeasurable component must not score as an average one.

NexusBrain's volume_momentum returned 1.0 for both of its unmeasurable
cases -- fewer than period+2 candles, and avg_vol == 0 (routine for thin
pairs). A ratio of exactly 1.0 means "this bar's volume equals its trailing
average": a real, unremarkable reading. It fell into the `> 0.8` bucket of
score_volume_confirmation and scored 0.50 -- a mid-range CONFIRMATION
derived from no data, folded at 0.15 weight into the confluence score that
gates entries at 0.70.

Measured cost of the old path, with every other component at 0.8:

    all six measured @0.8           -> 0.8000
    volume fabricated (scores 0.50) -> 0.7550   <- 4.5pp penalty for nothing
    volume dropped + renormalised   -> 0.8000

That 4.5pp is the same magnitude volume_momentum's own docstring blames for
an earlier live incident ("cost ~4.5pp of confluence on every candidate
against a 0.70 entry gate and is a large part of why total_trades was 0").
The bar-offset half of that bug was fixed in the 2026-07-29 audit; the
sentinel half was not.

The fix is NOT to return 0.0 either -- that would fail the gate for a reason
nobody measured. Missing components are dropped and the remaining weights
renormalised, so an absence neither inflates nor deflates the score. When
nothing at all can be measured, the score is None, not 0.0.

Also covers:
  - SignalComponents defaulting every field to 0.5 (a real neutral reading
    several scorers legitimately produce) before anything was computed.
  - The multi-timeframe accumulator, which divided every component by ONE
    shared total_weight. A component measured on only some timeframes would
    have been scaled down in proportion to how often it was missing -- a
    real reading corrupted by an absence elsewhere. Weight is now tracked
    per component.
"""
import sys

sys.path.insert(0, 'D:/NexusBrain')
sys.path.insert(0, 'D:/CommandCenter')

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


from nexus_brain import (COMPONENT_WEIGHTS, SignalComponents,
                         compute_confluence, volume_momentum)

# ── 1. Weights are a partition ──
_tw = sum(COMPONENT_WEIGHTS.values())
check(abs(_tw - 1.0) < 1e-9,
      'COMPONENT_WEIGHTS must sum to 1.0, got %.6f -- renormalisation '
      'assumes it' % _tw)

# ── 2. All components equal => that value, whatever the weights ──
_all = SignalComponents(0.8, 0.8, 0.8, 0.8, 0.8, 0.8)
_s = compute_confluence(_all)
check(_s is not None and abs(_s - 0.8) < 1e-9,
      'six components all at 0.8 must score 0.8, got %r' % (_s,))

# ── 3. A dropped component must not change an otherwise-uniform score ──
_missing = SignalComponents(0.8, 0.8, 0.8, 0.8, None, 0.8)
_sm = compute_confluence(_missing)
check(_sm is not None and abs(_sm - 0.8) < 1e-9,
      'with volume unmeasurable and every other component at 0.8, the score '
      'must stay 0.8 (drop and renormalise), got %r. The old code folded in '
      'a fabricated 0.50 and produced 0.7550 -- a 4.5pp penalty for data '
      'that did not exist' % (_sm,))

# ── 4. Renormalisation must equal the hand-computed weighted mean ──
_vals = {'ema_alignment': 0.9, 'rsi_momentum': 0.6, 'macd_momentum': 0.7,
         'volume_confirm': 0.5, 'regime_align': 0.8}      # bollinger absent
_c = SignalComponents(0.9, 0.6, 0.7, None, 0.5, 0.8)
_num = sum(v * COMPONENT_WEIGHTS[k] for k, v in _vals.items())
_den = sum(COMPONENT_WEIGHTS[k] for k in _vals)
_want = _num / _den
_got = compute_confluence(_c)
check(_got is not None and abs(_got - _want) < 1e-9,
      'renormalised score must equal the weighted mean over PRESENT '
      'components: got %r, expected %.9f' % (_got, _want))

# ── 5. Nothing measurable => None, never 0.0 ──
_none = compute_confluence(SignalComponents())
check(_none is None,
      'with no component measurable the confluence score must be None, got '
      '%r -- 0.0 is a claim that every signal disagreed' % (_none,))

# ── 6. The dataclass must not seed a real-looking neutral ──
_d = SignalComponents()
for _f in ('ema_alignment', 'rsi_momentum', 'macd_momentum',
           'bollinger_pos', 'volume_confirm', 'regime_align'):
    _v = getattr(_d, _f)
    check(_v is None,
          'SignalComponents.%s defaults to %r. 0.5 is a real neutral score '
          'that several scorers produce, so an unset component was '
          'indistinguishable from a measured one' % (_f, _v))

# ── 7. volume_momentum must report absence, not a 1.0 ratio ──
check(volume_momentum([]) is None,
      'volume_momentum([]) must be None -- 1.0 means "volume equals its '
      'average", which is a reading')


class _C:
    """Minimal candle stand-in: enough candles, but all volumes zero."""

    def __init__(self):
        self.volume = 0.0
        self.close = 100.0
        self.high = 101.0
        self.low = 99.0
        self.open = 100.0


check(volume_momentum([_C() for _ in range(40)]) is None,
      'volume_momentum with a zero trailing average must be None -- that is '
      'routine for thin pairs, not a 1.0 ratio')

# ── 8. to_dict must survive None ──
try:
    _dd = SignalComponents(0.5, None, 0.5, None, 0.5, 0.5).to_dict()
    check(_dd.get('rsi_momentum') is None,
          'to_dict must preserve None, got %r' % (_dd.get('rsi_momentum'),))
except Exception as _e:
    FAIL.append('to_dict raised on a None component: %r' % (_e,))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  unmeasured components are dropped and the rest renormalised')
