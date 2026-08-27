"""Two Gridzilla safety gates that could not fire.

1. THE PORTFOLIO EXPOSURE CEILING measured grid LINES, not dollars.

   `current_exposure = sum(g.get("levels", 0) * 10 for g in active.values())`
   and get_active_grids returns "levels" as design.n_levels — a count of
   grid lines. With 5 lines per grid over a 10-pair universe that caps at
   10*5*10 = $500, compared against `balance * 0.30 * multiplier` (>=
   balance * 0.15). Tripping it needs a balance under ~$3.3k, but the
   deploy path refuses to act below $10k — disjoint ranges, so the ceiling
   never fired and only the per-pair 5% cap constrained anything (10 pairs
   x 5% = 50% of portfolio).

   Measured live 2026-08-13: real Gridzilla exposure $199,018 against a
   $300,149 ceiling — 66% of it — while the old formula reported $250.

2. THE DRAWDOWN KILL measured only realized cycles, which cannot fall.

   grid_pnl is written in exactly one place, and only inside the
   `if matching_buy:` branch of a SELL fill. A sell fires only at
   `current_price >= level["price"]` against a buy filled at a strictly
   LOWER level, so every realized pnl is >= 0 and grid_pnl is monotonically
   non-decreasing. peak_pnl therefore always equalled grid_pnl, dd was
   structurally 0.0, and DD_KILLED could never trigger.

   The real risk in a grid is the opposite quantity: open inventory bought
   on the way down. A grid bleeding on held bags reported max_drawdown 0.0.
   Everything needed was already tracked per level (filled / fill_price /
   size_usd), so drawdown is now measured on mark-to-market equity.
"""
import re
import sys

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


SRC = open('D:/Gridzilla/gridzilla.py', encoding='utf-8',
           errors='replace').read()

# ── 1. Exposure must be summed in dollars ──
check('sum(g.get("levels", 0) * 10 for g in active.values())' not in SRC,
      'exposure still counts grid LINES x10 and compares them against a '
      'dollar ceiling — the gate cannot fire')
# CORRECTED 2026-08-27. This used to require `_alloc = _g.get("allocation")`
# -- and deploy_grid NEVER writes that key into grid_state. The only
# "allocation" in gridzilla.py is inside the GRID_DEPLOYED event payload,
# so the sum was always 0.0 and the 30% portfolio ceiling could never fire.
#
# The test was therefore PINNING THE BUG: it asserted a read against a
# phantom field, and went red against the correct fix. A guard that
# enforces the defect it was written to prevent is the worst false green
# in this suite, because it actively resists repair.
#
# The real field is design.allocation_usd, written at GridArchitect.design.
check('allocation_usd' in SRC,
      'exposure must sum each grid real allocation from '
      'design.allocation_usd -- the field deploy_grid actually stores')
check(re.search(r'_alloc = _g\.get\("allocation"\)\s*$', SRC, re.M) is None,
      'exposure reads the phantom top-level allocation key again, which '
      'nothing writes -- current_exposure would be permanently 0.0')
check('"allocation": design["allocation_usd"]' in SRC,
      'the allocation field the exposure sum reads must actually be set at '
      'deploy — a wrong key would silently report zero exposure, which is '
      'the same defect in a new spelling')
# It must read the INTERNAL state, not the API-shaped view that drops
# allocation entirely.
check('active_full = self.executor.active_grids' in SRC,
      'exposure must read the internal grid state — get_active_grids() '
      'strips `allocation`, so summing its output would be zero forever')


def exposure(grids):
    """Mirror of the shipped sum."""
    tot = 0.0
    for g in grids:
        if not isinstance(g, dict):
            continue
        a = g.get("allocation")
        if isinstance(a, (int, float)) and a > 0:
            tot += float(a)
    return tot


_live = [{"allocation": 47472.0}, {"allocation": 42957.0},
         {"allocation": 42952.0}, {"allocation": 35828.0},
         {"allocation": 29809.0}]
_got = exposure(_live)
check(abs(_got - 199018.0) < 1.0,
      'the five live grids must sum to ~$199,018 (their actual pool '
      'reservations), got %r' % _got)
_old = sum(5 * 10 for _ in _live)          # 5 lines * 10, per grid
check(_old == 250,
      'sanity: the old formula reported $250 for these same grids, got %r'
      % _old)
check(_got > 1_000_000 * 0.30 * 0.5,
      'the real exposure must be large enough to actually interact with a '
      'balance-derived ceiling — that is the whole point; got %r' % _got)

# Malformed entries must not crash or silently inflate.
check(exposure([{"allocation": None}, {"allocation": -5}, "notadict",
                {"allocation": 100.0}]) == 100.0,
      'malformed grid entries must be skipped, not summed as zero-ish junk')

# ── 2. Drawdown must be mark-to-market ──
check('grid["unrealized_pnl"] = round(_unrealized, 4)' in SRC,
      'the drawdown block must compute unrealized P/L on open inventory')
check(re.search(r'dd = \(grid\.get\("peak_pnl"\) or 0\) - _equity', SRC)
      is not None,
      'drawdown must be measured against mark-to-market equity, not '
      'grid_pnl (which is monotonically non-decreasing and yields dd=0 '
      'forever)')
check(re.search(r'if _equity > \(grid\.get\("peak_pnl"\) or 0\)', SRC)
      is not None,
      'the peak must track mark-to-market equity too, or dd is measured '
      'against the wrong high-water mark')


def dd_series(fills, prices, alloc_per_level=1000.0):
    """Mirror the shipped drawdown maths over a price path."""
    peak = 0.0
    maxdd = 0.0
    realized = 0.0
    for px in prices:
        unreal = 0.0
        for fp in fills:
            if fp > 0:
                unreal += (alloc_per_level / fp) * (px - fp)
        eq = realized + unreal
        if eq > peak:
            peak = eq
        if peak - eq > maxdd:
            maxdd = peak - eq
    return maxdd


# A grid that bought on the way down and is now underwater MUST show a
# real drawdown. Buys at 100 and 95, price now 85.
_dd = dd_series([100.0, 95.0], [100.0, 95.0, 90.0, 85.0])
check(_dd > 0,
      'a grid holding inventory bought above the current price must report '
      'a NON-ZERO drawdown — this is exactly the case the old measure '
      'reported as 0.0; got %r' % _dd)
# Size it: at 85, the two lots are down ~$150 and ~$105 => ~$255.
check(200 < _dd < 320,
      'the drawdown must be the actual unrealized loss (~$255 for these '
      'lots), got %r' % _dd)

# The old measure, for contrast: realized-only never moves.
_old_dd = 0.0
_peak = 0.0
for _r in (0.0, 5.0, 12.0, 20.0):     # realized pnl only ever rises
    if _r > _peak:
        _peak = _r
    _old_dd = max(_old_dd, _peak - _r)
check(_old_dd == 0.0,
      'sanity: realized-only P/L is monotonically non-decreasing, so the '
      'old drawdown is structurally 0.0; got %r' % _old_dd)

# A profitable grid must NOT report drawdown just because price wobbled up.
check(dd_series([100.0], [100.0, 105.0, 110.0]) == 0.0,
      'a grid whose inventory only appreciated must show no drawdown — the '
      'new measure must not cry wolf')

# ── 3. Restored grids must not KeyError on the new fields ──
check('.get("grid_pnl") or 0' in SRC and '.get("peak_pnl") or 0' in SRC,
      'state files written before these keys existed must not KeyError on '
      'the first scan after an upgrade restart')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  exposure is dollars; drawdown sees open inventory')
