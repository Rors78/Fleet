"""An unreadable pool sized Confluence 476x too large.

confluence.py carried:

    FALLBACK_EQUITY_USD = 10000.0  # basis when CC is unreachable

and _sizing_basis() returned it whenever self._portfolio was absent or
pool_total() failed or returned <= 0. The docstring said "Never sizes off a
guess" while doing precisely that.

MEASURED, with the shipped constants (RISK_POOL_SHARE_PCT=10,
RISK_PER_TRADE_PCT=0.5, MIN_STOP=0.004, bounds 0.005/0.15 of pool) against
the live pool of $209.88 and a 2% stop:

                     CC reachable      CC unreachable
    basis              $20.99            $10,000.00
    risk / trade        $0.10                $50.00
    position            $5.25             $2,500.00
    vs real pool         2.5%              1,191.2%

476x inflation, and a SINGLE position at ~12x the entire fleet pool.

The MAX_POSITION_PCT_OF_POOL cap does not contain it. _position_size derives
its bounds from the basis it was handed --

    _pool = basis / (RISK_POOL_SHARE_PCT / 100.0)

-- so with the fallback basis the ceiling rises from $31.48 to $15,000 in
lockstep. The guard is real, but it measures against the same lie it is
supposed to catch.

This is absent-vs-empty failing in the dangerous direction. An unreadable
pool is not a $10,000 pool. Unreadable must fail ARMED -- refuse to size --
never toward a basis larger than the truth. It is the same shape as the
founding case: a safety anchor that re-baselines itself on a failed read.

Fleet trades no real money, so the dollar damage ceiling is zero. What it
corrupts is the SIGNAL: one 12x-pool position poisons expectancy, win rate,
and every downstream P/L figure with a trade that could never have existed.

Live state when found was healthy -- sizing_basis 20.99, source "pool_share",
risk_per_trade_usd 0.10, three open positions at $3.41/$4.10/$11.70. An armed
trap that had not sprung, not an active fire. Confluence is a phase-2 bot and
can start before CC is serving, which is exactly when it would spring.
"""
import re
import sys

SRC = 'D:/Confluence/confluence.py'
FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


_src = open(SRC, encoding='utf-8', errors='replace').read()


def _const(name, default=None):
    m = re.search(r'^%s\s*=\s*([0-9.]+)' % name, _src, re.M)
    return float(m.group(1)) if m else default


# ── 1. No hardcoded equity basis may survive ──
_fb = re.search(r'^FALLBACK_EQUITY_USD\s*=\s*([0-9.]+)', _src, re.M)
if _fb:
    _v = float(_fb.group(1))
    check(False,
          'FALLBACK_EQUITY_USD = %.1f still exists. An unreadable pool is '
          'not a $%.0f pool -- against the live $209.88 pool this sizes '
          'positions 476x too large, one position at 12x the whole pool'
          % (_v, _v))

# ── 2. The unreadable branch must refuse, not substitute ──
_fn = re.search(r'def _sizing_basis\(self\)[^\n]*\n(.*?)(?=\n    def )', _src, re.S)
check(_fn is not None, '_sizing_basis not found -- test needs updating')
if _fn:
    # Strip the docstring before checking. The fixed function explains the
    # old constant by name in prose; matching that would fail the test on
    # correct code -- a green/red inversion caused by the test, not the bug.
    _body = re.sub(r'"""(?:.|\n)*?"""', '', _fn.group(1))
    check('FALLBACK_EQUITY_USD' not in _body,
          '_sizing_basis still returns a hardcoded basis when the pool is '
          'unreadable. It must return None (refuse to size) so the caller '
          'skips the trade -- unreadable fails ARMED, never toward a larger '
          'basis than reality')
    check('None' in _body,
          '_sizing_basis must be able to report "I could not read the pool" '
          'distinctly from a real basis; a float return cannot express that')

# ── 3. The caller must not trade on an unknown basis ──
_ps = re.search(r'def _position_size\(self[^\n]*\n(.*?)(?=\n    def )', _src, re.S)
check(_ps is not None, '_position_size not found -- test needs updating')
if _ps:
    _pb = _ps.group(1)
    check(re.search(r'basis\s+is\s+None|not\s+isinstance\(\s*basis', _pb),
          '_position_size does not guard against an unknown basis. If '
          '_sizing_basis can return None, sizing arithmetic on it raises or '
          'silently produces a wrong number')

# ── 4. Arithmetic proof: the cap must not scale with the poisoned basis ──
# This is the subtle half. Even with a fallback the MAX bound would look
# protective -- but it is derived from the basis, so it inflates too.
_share = _const('RISK_POOL_SHARE_PCT', 10.0)
_risk = _const('RISK_PER_TRADE_PCT', 0.5)
_maxp = _const('MAX_POSITION_PCT_OF_POOL', 0.15)
_minstop = _const('MIN_STOP_PCT_FOR_SIZING', 0.004)
POOL = 209.88


def _size_for(basis, stop_pct=0.02):
    risk = basis * (_risk / 100.0)
    sz = risk / max(stop_pct, _minstop)
    pool = basis / (_share / 100.0) if _share else basis
    return min(sz, pool * _maxp)


_good = _size_for(POOL * (_share / 100.0))
check(_good <= POOL * 0.20,
      'sanity: a correctly-sized position should be a modest share of the '
      'pool, got $%.2f against a $%.2f pool' % (_good, POOL))

if _fb:
    _bad = _size_for(float(_fb.group(1)))
    check(_bad <= POOL,
          'with the fallback basis a single position is $%.2f against a '
          '$%.2f pool (%.0f%% of it, %.0fx the correct size) -- the '
          'MAX_POSITION_PCT_OF_POOL cap does not contain this because the '
          'cap is derived from the same poisoned basis'
          % (_bad, POOL, 100.0 * _bad / POOL, _bad / _good))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  an unreadable pool refuses to size rather than inventing a basis')
