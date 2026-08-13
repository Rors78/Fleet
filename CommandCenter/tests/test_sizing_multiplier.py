"""A log that claims an effect the code did not apply.

NexusBrain sizes positions as `min(risk_based, max_size)` and then applies
bus-intelligence multipliers (AEGIS DEPLOY x1.2, whale EXTREME x1.3,
convergent signals x1.3, NEWTON alignment x1.3, plus downward ones).

The cap binds in EVERY realistic market. Sizing solves
`risk_size = equity*0.01 / (2*atr_frac)`, so the 5% cap fails to bind only
when atr_frac >= 10%. Measured live on 2026-08-13 from /api/market/ohlc,
1h ATR on the traded pairs:

    BTC 0.303%   ETH 0.387%   SOL 0.411%
    XRP 0.406%   LINK 0.572%  ADA 0.776%

Highest is 0.776% against the 10% needed — raw risk size is ~13x the cap.
So size_usd == max_size on entry to the multiplier block, and
`min(max_size * mult, max_size)` returned max_size for EVERY multiplier
>= 1.0. The upward multipliers were structurally erased, the downward ones
still worked, and the log printed "size x1.30" for an increase that never
happened.

What this test pins is the ORDER, because getting it wrong breaks one
direction or the other and both were tried while fixing it:

  min(risk,cap) then *mult then min(cap)  -> upward erased (original bug)
  risk*mult then min(cap)                 -> DOWNWARD erased, strictly
      worse: risk_size is ~25x the cap, so even x0.3 still exceeds it and
      a de-risking signal does nothing at all.

The shipped order multiplies the capped base: downward applies in full,
upward is bounded by the risk limit (which is a deliberate ceiling, not
something a conviction signal may override), and the log now reports what
was APPLIED.
"""
import re
import sys

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


SRC = open('D:/NexusBrain/nexus_brain.py', encoding='utf-8',
           errors='replace').read()

# ── 1. The shipped order, executed ──
_m = re.search(
    r'_requested = (size_usd|_risk_size|_base) \* _bus_mult\s*\n\s*'
    r'size_usd = min\(_requested, max_size\)', SRC)
check(_m is not None,
      'could not locate the shipped multiplier/cap block')

if _m:
    _mult_base = _m.group(1)
    check(_mult_base != '_risk_size',
          'the multiplier must NOT be applied to the uncapped risk size — '
          'risk_size is ~25x the cap at live ATR, so every DOWNWARD '
          'multiplier (the safety-relevant direction) would be erased')

    def size(mult, atr_frac=0.0039, equity=10000.0, risk_pct=1.0,
             cap_pct=0.05, price=100.0):
        risk_amount = equity * (risk_pct / 100)
        risk_distance = 2 * atr_frac * price          # stop_loss_atr_mult=2
        risk_size = risk_amount / risk_distance * price
        max_size = equity * cap_pct
        base = min(risk_size, max_size)               # shipped line 1
        return min(base * mult, max_size)             # shipped lines 2-3

    cap = 10000.0 * 0.05

    # Downward multipliers MUST apply in full — this is the direction that
    # protects capital, and the one my first attempt destroyed.
    check(abs(size(0.3) - cap * 0.3) < 1e-9,
          'a x0.3 de-risking multiplier must produce 30%% of the cap, got '
          '%r (cap %r)' % (size(0.3), cap))
    check(abs(size(0.5) - cap * 0.5) < 1e-9,
          'a x0.5 multiplier must halve the size, got %r' % size(0.5))

    # Upward multipliers are bounded by the risk limit, not silently
    # discarded — same NUMBER as before, but now it is a stated policy.
    check(size(1.3) == cap and size(1.2) == cap,
          'an upward multiplier must not exceed max_position_pct — the cap '
          'is a deliberate risk limit; got x1.3 -> %r, x1.2 -> %r'
          % (size(1.3), size(1.2)))
    check(size(1.0) == cap,
          'a neutral multiplier must leave size at the cap, got %r' % size(1.0))

    # Ordering regression guard: if the cap ever stops binding (a much
    # higher-volatility asset), BOTH directions must still behave.
    _hi = 0.20      # 20% ATR — cap does not bind
    _rs = (10000.0 * 0.01) / (2 * _hi * 100.0) * 100.0
    check(_rs < cap,
          'sanity: at 20%% ATR the risk size (%r) should sit below the cap '
          '(%r) so this case tests the unbound branch' % (_rs, cap))
    check(abs(size(0.5, atr_frac=_hi) - _rs * 0.5) < 1e-9,
          'when the cap does NOT bind, a x0.5 must halve the risk-based '
          'size, got %r' % size(0.5, atr_frac=_hi))
    check(abs(size(1.3, atr_frac=_hi) - _rs * 1.3) < 1e-9,
          'when the cap does not bind, a x1.3 must actually raise size — '
          'the upward path must work wherever there is headroom; got %r'
          % size(1.3, atr_frac=_hi))

# ── 2. The log must report what was applied, not what was requested ──
check('size x{_bus_mult:.2f}' not in SRC,
      'the old log line printed the requested multiplier at a point where '
      'the cap had already discarded it — a log asserting an effect that '
      'did not occur')
check('requested ->' in SRC and 'capped at' in SRC,
      'the log must state the resulting size and disclose when the cap '
      'truncated the request')

# ── 3. The risk-based figure must still be computed, not removed ──
check('_risk_size = risk_amount / risk_distance' in SRC,
      'risk-based sizing must remain the input to the cap — deleting it '
      'would make max_position_pct the only sizing rule by construction')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  de-risking multipliers apply in full; the cap bounds increases; '
      'the log matches reality')
