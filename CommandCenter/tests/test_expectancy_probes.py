"""A synthetic probe row must never enter ANY expectancy figure.

The fleet writes synthetic test rows with marker pair names
(ZZPROBE032265B/USD and friends), each carrying an identical hardcoded
pnl=12.34. Measured in the durable bus log on 2026-08-26:

    turtlesue    97 TRADE_CLOSE rows = 92 PROBES + 5 real
                 probe P/L +$1,135.28   REAL P/L -$0.00
    gridzilla    43 closes, 0 probes
    confluence   19 closes, 0 probes

So the bot with the largest apparent profit in the fleet has in fact made
exactly nothing, and every dollar of that headline is a test fixture.

The canonical filter already existed in THREE places —
command_center.py:3504 (_is_probe, the source of truth),
signal_broadcaster.py:701, weekly_analysis.py:166 — and expectancy.py, the
fleet's primary signal-quality module, was the sibling that got missed. That
is the same fix-one-sibling-miss-the-others shape as the rest of this audit.

The bug has already bitten once, on 2026-08-07: 50 of 73 rows on /api/trades
were probes contributing +$617, flipping the raw sum positive while real
pairs summed -$167. The SIGN of the fleet's trade feed depended on test data.

expectancy.py filters at BOTH ends: at write time in record_trade (the
chokepoint all three Command Center write paths funnel through), and at read
time in get_bot_stats/get_fleet_stats (because the store is DURABLE and holds
rows written before the gate existed).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import expectancy as E

FAIL = []


def check(cond, msg):
    print(('  PASS  ' if cond else '  FAIL  ') + msg)
    if not cond:
        FAIL.append(msg)


# record_trade() calls _save(). Redirect persistence BEFORE anything is
# constructed — a test that skipped this once erased the live store.
_TMP = tempfile.mkdtemp(prefix='exp_probe_test_')
E.ExpectancyTracker.PERSIST_PATH = os.path.join(_TMP, 'expectancy.json')


def fresh():
    t = E.ExpectancyTracker()
    t.trades = {}
    return t


print('=== CASE 1: the predicate matches command_center.py:3504 exactly ===')
for pair, want in [('ZZPROBE032265B/USD', True), ('ZZPROBE1/USD', True),
                   ('ZZ/USD', True), ('ZZTOP/USD', True),
                   ('NFNOK/USD', True), ('NF138587OK/USD', True),
                   ('BTC/USD', False), ('ETH/USD', False), ('POL/USD', False),
                   ('NFT/USD', False), ('', False), (None, False)]:
    got = E._is_probe(pair)
    check(got is want, '_is_probe(%r) == %r' % (pair, want))

print()
print('=== CASE 2: a probe cannot be WRITTEN into the durable store ===')
t = fresh()
r = t.record_trade(bot_id='turtlesue', pair='ZZPROBE032265B/USD',
                   direction='LONG', entry_price=4.1, exit_price=4.21,
                   size_usd=100, duration=1, realized_pnl=12.34)
check(r is None, 'record_trade returns None for a probe (silent no-op, the '
                 'same shape as the trade_id dedup branch)')
check(not t.trades.get('turtlesue'),
      'nothing was appended — a probe in this DURABLE store is permanent and '
      'indistinguishable from a measurement once written')

print()
print('=== CASE 3: the live turtlesue shape — 92 probes + 5 real flats ===')
t = fresh()
for _ in range(92):
    t.record_trade(bot_id='turtlesue', pair='ZZPROBE032265B/USD',
                   direction='LONG', entry_price=4.1, exit_price=4.21,
                   size_usd=100, duration=1, realized_pnl=12.34)
for _ in range(5):
    t.record_trade(bot_id='turtlesue', pair='BTC/USD', direction='LONG',
                   entry_price=None, exit_price=None, size_usd=100,
                   duration=0, realized_pnl=0.0)
s = t.get_bot_stats('turtlesue')
print('     %s' % {k: s[k] for k in ('total_trades', 'wins', 'losses', 'flat',
                                     'decided', 'win_rate',
                                     'expectancy_per_trade',
                                     'total_gross_pnl', 'best_trade')})
check(s['total_trades'] == 5,
      'only the 5 REAL closes are counted, got %d' % s['total_trades'])
check(s['total_gross_pnl'] == 0.0,
      'real P/L is $0.00 — the +$1,135.28 is entirely fixtures; got %r'
      % s['total_gross_pnl'])
check(s['wins'] == 0, 'a probe must not become a win')
check(s['flat'] == 5, 'the five real flat closes survive')
check(s['win_rate'] is None,
      'nothing was DECIDED, so the rate is unmeasured — not a number')
check(s['expectancy_per_trade'] is None,
      'expectancy must be None, not +12.34: the probes all carry an '
      'identical hardcoded pnl and would report a fabricated edge')
check(s['best_trade'] == 0.0,
      'best_trade must not be the probe 12.34; got %r' % s['best_trade'])

print()
print('=== CASE 4: probes are excluded from EVERY figure, not just totals ===')
t = fresh()
# Probes that would, if admitted, invert the verdict: real record is losing.
for _ in range(55):
    t.record_trade(bot_id='b', pair='ZZPROBE1/USD', direction='LONG',
                   entry_price=4.1, exit_price=4.21, size_usd=100,
                   duration=1, realized_pnl=12.34)
for pnl in (-636.29, 162.98, -50.0):
    t.record_trade(bot_id='b', pair='ENA/USD', direction='LONG',
                   entry_price=None, exit_price=None, size_usd=100,
                   duration=0, realized_pnl=pnl)
s = t.get_bot_stats('b')
print('     %s' % {k: s[k] for k in ('total_trades', 'wins', 'losses',
                                     'decided', 'win_rate', 'avg_win',
                                     'avg_loss', 'profit_factor',
                                     'expectancy_per_trade', 'best_trade',
                                     'worst_trade',
                                     'max_consecutive_losses',
                                     'total_gross_pnl')})
check(s['total_trades'] == 3, 'total_trades excludes probes')
check(s['wins'] == 1 and s['losses'] == 2, 'wins/losses exclude probes')
check(s['decided'] == 3, 'decided excludes probes')
check(abs(s['win_rate'] - 33.3) < 0.1, 'win_rate over real rows only')
check(abs(s['avg_win'] - 162.98) < 0.01,
      'avg_win must not be dragged toward the probe 12.34; got %r'
      % s['avg_win'])
check(s['avg_loss'] is not None and s['avg_loss'] < 0, 'avg_loss is real')
check(s['profit_factor'] is not None and s['profit_factor'] < 1,
      'profit_factor reflects the real losing record')
check(s['expectancy_per_trade'] < 0,
      'THE VERDICT: real expectancy is NEGATIVE. If this reads positive the '
      'probes are leaking back in and inverting the headline; got %+.2f'
      % s['expectancy_per_trade'])
check(s['best_trade'] == 162.98, 'best_trade is a real trade')
check(s['worst_trade'] == -636.29, 'worst_trade is a real trade')
check(s['max_consecutive_losses'] == 1,
      'max_consecutive_losses counts only real rows; got %r'
      % s['max_consecutive_losses'])
check(s['total_gross_pnl'] < 0, 'the real sum is negative')

# The un-filtered version must be visibly different, or this proves nothing.
raw_sum = 55 * 12.34 + (-636.29) + 162.98 + (-50.0)
check(raw_sum > 0,
      'sanity: WITH probes included the sum is positive (%+.2f) — that sign '
      'flip is the defect this test exists to prevent' % raw_sum)

print()
print('=== CASE 5: read-time filter catches rows already on disk ===')
# Rows written BEFORE the write gate existed are still in the durable store.
t = fresh()
t.trades['legacy'] = [
    {'pair': 'ZZPROBE1/USD', 'gross_pnl': 12.34, 'net_pnl': 12.34,
     'r_multiple': None, 'duration': 1, 'won': True, 'timestamp': 0},
    {'pair': 'BTC/USD', 'gross_pnl': -25.0, 'net_pnl': -25.0,
     'r_multiple': None, 'duration': 1, 'won': False, 'timestamp': 0},
]
s = t.get_bot_stats('legacy')
check(s['total_trades'] == 1,
      'a probe injected directly into the store (as a pre-gate row would be) '
      'must still be filtered on READ; got %d' % s['total_trades'])
check(s['total_gross_pnl'] == -25.0, 'only the real row contributes P/L')

print()
print('=== CASE 6: the FLEET headline excludes probes too ===')
t = fresh()
for _ in range(30):
    t.record_trade(bot_id='turtlesue', pair='ZZPROBE1/USD', direction='LONG',
                   entry_price=4.1, exit_price=4.21, size_usd=100,
                   duration=1, realized_pnl=12.34)
t.trades.setdefault('turtlesue', []).append(
    {'pair': 'ZZPROBE9/USD', 'gross_pnl': 12.34, 'net_pnl': 12.34,
     'r_multiple': None, 'duration': 1, 'won': True, 'timestamp': 0})
t.record_trade(bot_id='confluence', pair='APE/USD', direction='LONG',
               entry_price=None, exit_price=None, size_usd=100,
               duration=0, realized_pnl=-100.0)
f = t.get_fleet_stats()
print('     total_trades=%r win_rate=%r fleet_expectancy=%r'
      % (f.get('total_trades'), f.get('win_rate'),
         f.get('fleet_expectancy')))
check(f.get('total_trades') == 1,
      'fleet total_trades counts only real closes; got %r'
      % f.get('total_trades'))
check(f.get('fleet_expectancy') is not None
      and f['fleet_expectancy'] < 0,
      'the fleet headline must stay negative — probes must not flip the '
      "fleet's primary signal-quality number; got %r"
      % f.get('fleet_expectancy'))

if FAIL:
    print()
    for f_ in FAIL:
        print('FAIL  ' + f_)
    sys.exit(1)
print()
print('ok  synthetic probes cannot enter any expectancy figure, at write '
      'time or read time, per-bot or fleet-wide')
