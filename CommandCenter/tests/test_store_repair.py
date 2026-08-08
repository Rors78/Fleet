"""Repair fabricated $0.00 prices without disturbing any real figure.

Three writers used to persist `entry_price=data.get(..., 0)` into the DURABLE
expectancy store. A trade whose bot reported P/L but no prices was therefore
stored claiming an entry of $0.00 — a price nobody measured, indistinguishable
from a real one, and the reason r_multiple is null on those rows. All three
writers are fixed (8cac8f7, e1763b2, f7b0481) but the rows they already wrote
remain, and they cannot be cleaned on disk: ExpectancyTracker loads once at
construction and _save() writes the in-memory copy, so a file edit under a
running Command Center is silently overwritten by the next trade.

Separately, the release path keyed its dedup on the reservation id while the
bus path keyed on the event id, so one close arriving by both routes was
stored TWICE. A double-count inflates total_trades and understates true
per-trade expectancy — live, gridzilla read $86.17/trade over 6 rows when the
truth was $70.81 over 5.

The repair must be conservative: it may only null price fields that are
exactly 0 and drop a row that duplicates another's (pair, gross_pnl) while
carrying strictly less price information. Every real figure survives.
"""
import importlib
import os
import sys
import tempfile

sys.path.insert(0, 'D:/CommandCenter')
import expectancy as ex
importlib.reload(ex)

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def fresh():
    t = ex.ExpectancyTracker()
    t.PERSIST_PATH = os.path.join(tempfile.mkdtemp(), 'e.json')
    t.trades = {}
    return t


def row(pair, entry, exit_, gross, **kw):
    d = {'pair': pair, 'direction': 'LONG', 'entry_price': entry,
         'exit_price': exit_, 'gross_pnl': gross, 'net_pnl': gross,
         'size_usd': 1000.0, 'duration': 900, 'won': gross > 0,
         'r_multiple': None, 'timestamp': 1786100000}
    d.update(kw)
    return d


# ── 1. Fabricated zeros are nulled; real prices are not ──
t = fresh()
t.trades['gridzilla'] = [
    row('ETH/USD', 0.0, 0.0, 34.9016),
    row('ETH/USD', 0, 1915.0, 30.3418),
]
t.trades['confluence'] = [row('ENA/USD', 0.0968934, 0.0954, -636.2851)]

res = t.repair_fabricated_prices()
check(res['nulled'] == 3,
      'three price fields were exactly 0 and must be nulled, got %d'
      % res['nulled'])

g = t.trades['gridzilla']
check(all(r.get('entry_price') is None for r in g),
      'every fabricated entry price must become None, got %r'
      % [r.get('entry_price') for r in g])
check(g[1].get('exit_price') == 1915.0,
      'a REAL exit price must survive the repair, got %r' % g[1].get('exit_price'))

c = t.trades['confluence'][0]
check(c.get('entry_price') == 0.0968934 and c.get('exit_price') == 0.0954,
      'a row with two real prices must be untouched, got %r/%r'
      % (c.get('entry_price'), c.get('exit_price')))

# ── 2. Real figures must never be modified ──
check(abs(g[0].get('gross_pnl') - 34.9016) < 1e-9,
      'gross_pnl must never be touched, got %r' % g[0].get('gross_pnl'))
check(g[0].get('size_usd') == 1000.0, 'size_usd must never be touched')
check(g[0].get('duration') == 900, 'duration must never be touched')
check(g[0].get('direction') == 'LONG', 'direction must never be touched')
check(g[0].get('won') is True, 'the win verdict must never be touched')

# ── 3. A double-counted close collapses to one row, keeping the richer ──
t2 = fresh()
t2.trades['gridzilla'] = [
    row('POL/USD', 0, 0.07574, 162.9797),    # bus route: has an exit price
    row('POL/USD', 0.0, 0.0, 162.9797),      # release route: no prices
]
res2 = t2.repair_fabricated_prices()
rows = t2.trades['gridzilla']
check(res2['dropped'] == 1,
      'the duplicate close must be dropped, dropped=%d' % res2['dropped'])
check(len(rows) == 1, 'one close must leave one row, got %d' % len(rows))
if rows:
    check(rows[0].get('exit_price') == 0.07574,
          'the surviving row must be the one carrying the REAL price, got %r'
          % rows[0].get('exit_price'))
    check(abs(rows[0].get('gross_pnl') - 162.9797) < 1e-9,
          'the P/L of the surviving row must be exact')

# ── 4. Expectancy corrects because the double-count is gone ──
t3 = fresh()
t3.trades['gridzilla'] = [
    row('POL/USD', 0, 0.07574, 162.9797),
    row('POL/USD', 0.0, 0.0, 162.9797),
    row('ETH/USD', 0, 1915.0, 30.3418),
]
before = t3.get_bot_stats('gridzilla').get('expectancy_per_trade')
t3.repair_fabricated_prices()
after = t3.get_bot_stats('gridzilla').get('expectancy_per_trade')
check(t3.get_bot_stats('gridzilla').get('total_trades') == 2,
      'the double-counted close must stop inflating total_trades')
check(after < before,
      'removing a double-count must LOWER per-trade expectancy (it was '
      'counting one win twice): before=%r after=%r' % (before, after))

# ── 5. Two DIFFERENT closes with the same P/L are not duplicates ──
t4 = fresh()
t4.trades['x'] = [
    row('AAA/USD', 1.0, 2.0, 50.0),
    row('BBB/USD', 3.0, 4.0, 50.0),   # same gross, different pair
]
res4 = t4.repair_fabricated_prices()
check(res4['dropped'] == 0,
      'same P/L on DIFFERENT pairs is a coincidence, not a double-count')
check(len(t4.trades['x']) == 2, 'both genuine trades must survive')

# ── 6. Idempotent: a second run must change nothing ──
t5 = fresh()
t5.trades['g'] = [row('ETH/USD', 0, 1915.0, 30.0)]
t5.repair_fabricated_prices()
res6 = t5.repair_fabricated_prices()
check(res6['nulled'] == 0 and res6['dropped'] == 0,
      'repair must be idempotent, second run reported %r' % res6)

# ── 7. Unmeasurable capital movements are dropped; measured flats survive ──
# turtlesue's stale-reservation cleanups (gross $0.00, size 0) slipped past
# the writers' guards and inflated total_trades. A flat close with a REAL
# size is a measured break-even and must survive — different thing entirely.
t7 = fresh()
t7.trades['turtlesue'] = [
    row('UNIUSD', None, 4.2027, 0.0, size_usd=0),
    row('XXLMZUSD', None, 0.1628, 0.0, size_usd=0),
]
t7.trades['rubberband'] = [row('BTC/USD', 100.0, 100.0, 0.0, size_usd=1000.0)]
res7 = t7.repair_fabricated_prices()
check(len(t7.trades['turtlesue']) == 0,
      'zero-P/L zero-size rows measure nothing and must be dropped, got %d'
      % len(t7.trades['turtlesue']))
check(len(t7.trades['rubberband']) == 1,
      'a measured break-even (flat P/L on a REAL size) must survive — '
      'dropping it would erase a genuine trade')
check(res7['dropped'] == 2,
      'the drop must be counted, got %r' % res7['dropped'])

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  fabricated prices nulled, double-counts dropped, real figures intact')
