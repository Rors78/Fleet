"""A win rate must never be displayed without the population it was taken over.

Two independent defects, both live on the dashboard on 2026-08-26, both
visible in one GRIDZILLA panel at the same moment:

  "Total cycles 0"   "Active grids 0"   "No completed grids yet"
  "Win rate 100.0%"  "Wins/Losses 16/0"  "Trades 33"

with the win-rate bar painted FULL GREEN to 100% against "target: 50%+".

  1. THE DENOMINATOR WAS INVISIBLE. win_rate is wins/DECIDED (16/16), which
     is correct and deliberate — a $0.00 close is a capital movement, not a
     lost trade, and bucketing flats as losses was itself a bug fixed
     earlier. But 17 of the 33 trades were flat, and the panel rendered the
     bare rate with nothing to say so. 16/33 is 48.5%.

  2. THE RATE WAS DEGENERATE. Gridzilla has recorded ZERO losses, ever. A
     grid that goes against it exits by range-break with its levels
     cancelled, booking $0.00 — measured in the bus: 167 GRID_KILLED events,
     not one with negative P/L. So 100% is what the arithmetic MUST return
     when one side of the ledger is empty. It is not a result, and it must
     not be painted like one.

  3. And the two figures contradicted each other outright: `losses: 0` was
     published beside `max_consecutive_losses: 17`, because that helper
     counted `not gross_pnl > 0` and so still treated flats as losses — the
     one place in the module that had not been fixed. "A streak of 17 losses
     in a bot that has never lost."

This test pins the CONTRACT the display depends on. It does not assert that
win_rate is 48.5% — changing that denominator silently would break every
fleet-wide consumer. It asserts that whatever the rate is, it arrives with
its n, its basis, and an honest flag when it is not a measurement.
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


# ── Persistence redirect ────────────────────────────────────────────────
# record_trade() calls _save() itself. A test that skips this clobbers
# logs/expectancy.json and erases real trade history — that has already
# happened once here (see tests/test_no_live_writes.py). Redirect BEFORE
# constructing anything.
_TMP = tempfile.mkdtemp(prefix='exp_wr_test_')
E.ExpectancyTracker.PERSIST_PATH = os.path.join(_TMP, 'expectancy.json')


def fresh():
    t = E.ExpectancyTracker()
    t.trades = {}
    return t


def add(t, bot, pair, pnl):
    t.record_trade(bot_id=bot, pair=pair, direction='LONG',
                   entry_price=None, exit_price=None, size_usd=100,
                   duration=0, realized_pnl=pnl)


print('=== CASE 1: the live gridzilla shape — 16W / 0L / 17 flat ===')
t = fresh()
for _ in range(16):
    add(t, 'gridzilla', 'ETH/USD', 60.06)
for _ in range(17):
    add(t, 'gridzilla', 'ADA/USD', 0.0)
s = t.get_bot_stats('gridzilla')
print('     %s' % {k: s[k] for k in ('total_trades', 'wins', 'losses', 'flat',
                                     'decided', 'win_rate', 'win_rate_n',
                                     'win_rate_degenerate',
                                     'win_rate_of_total',
                                     'max_consecutive_losses')})

check(s['total_trades'] == 33, 'all 33 closes are counted in total_trades')
check(s['wins'] + s['losses'] + s['flat'] == s['total_trades'],
      'wins + losses + flat must RECONCILE with total_trades — the panel '
      'showed 16/0 against a population of 33 with 17 unaccounted for')
check(s['decided'] == 16, 'decided is the wins+losses population (16)')

# The denominator must travel WITH the rate.
check(s.get('win_rate_n') == s['decided'],
      'win_rate_n must equal the denominator actually used, so a panel '
      'cannot render the rate without its n')
check(s.get('win_rate_basis') == 'decided',
      "win_rate_basis must name the population in words ('decided')")
check(s.get('win_rate_of_total') == 48.5,
      'win_rate_of_total must offer the all-closed-trades rate (16/33 = '
      '48.5%%) so a consumer need not recompute it; got %r'
      % s.get('win_rate_of_total'))
check(s.get('win_rate_of_total_n') == 33,
      'win_rate_of_total_n must be the full closed population')

# The degeneracy flag is the one that stops the green paint.
check(s.get('win_rate_degenerate') is True,
      '100%% off ZERO losses is forced by the formula, not measured — it '
      'MUST be flagged degenerate so the display refuses a confident colour')

# The contradiction.
check(s['max_consecutive_losses'] == 0,
      'max_consecutive_losses must not count FLAT trades as losses: it '
      'published 17 beside losses:0, describing a different population from '
      'the same list; got %r' % s['max_consecutive_losses'])
check(not (s['losses'] == 0 and s['max_consecutive_losses'] > 0),
      'losses:0 and max_consecutive_losses>0 can never both be true of one '
      'population — this is the exact pair that shipped')

print()
print('=== CASE 2: a genuinely measured rate is NOT flagged degenerate ===')
t2 = fresh()
for _ in range(7):
    add(t2, 'confluence', 'BTC/USD', 545.47)
for _ in range(12):
    add(t2, 'confluence', 'APE/USD', -336.92)
s2 = t2.get_bot_stats('confluence')
print('     %s' % {k: s2[k] for k in ('total_trades', 'wins', 'losses', 'flat',
                                      'decided', 'win_rate', 'win_rate_n',
                                      'win_rate_degenerate')})
check(s2['win_rate'] == 36.8, 'real mixed record keeps its rate (7/19)')
check(s2.get('win_rate_degenerate') is False,
      'a rate with BOTH sides populated is a real measurement and must not '
      'be suppressed — the guard must not swallow honest bad news')
check(s2.get('win_rate_n') == 19, 'n travels with it here too')
check(s2['max_consecutive_losses'] == 12,
      'a real losing streak is still counted; got %r'
      % s2['max_consecutive_losses'])

print()
print('=== CASE 3: 0%% off zero wins is equally degenerate ===')
t3 = fresh()
for _ in range(4):
    add(t3, 'lossbot', 'BTC/USD', -50.0)
s3 = t3.get_bot_stats('lossbot')
check(s3['win_rate'] == 0.0, 'rate is 0%% over 4 decided')
check(s3.get('win_rate_degenerate') is True,
      'an empty WIN side is as forced as an empty loss side — the flag must '
      'not be a one-sided "good news only" filter')
check(s3['max_consecutive_losses'] == 4, 'four real losses in a row')

print()
print('=== CASE 4: all-flat is UNMEASURED, never 0%% ===')
t4 = fresh()
for _ in range(5):
    add(t4, 'turtlesue', 'BTC/USD', 0.0)
s4 = t4.get_bot_stats('turtlesue')
print('     %s' % {k: s4[k] for k in ('total_trades', 'flat', 'decided',
                                      'win_rate', 'win_rate_n',
                                      'win_rate_degenerate')})
check(s4['win_rate'] is None,
      'win_rate over ZERO decided trades must be None — a 0.0 here reads as '
      '"measured, and it lost every trade"')
check(s4.get('win_rate_n') == 0, 'n is a real count: zero decided')
check(s4.get('win_rate_degenerate') is False,
      'nothing was measured at all, so there is no forced rate to flag; '
      'win_rate is None and the null path already handles the display')
check(s4['total_trades'] == 5, 'the five closes are still counted')

print()
print('=== CASE 5: payload SHAPE is stable across every state ===')
# A payload whose KEYS change with its values is a trap: a consumer doing
# s.get('win_rate_n') cannot tell "no trades" from "field absent".
empty = t4._empty_stats('nobody')
for k in ('win_rate_n', 'win_rate_basis', 'win_rate_degenerate',
          'win_rate_of_total', 'win_rate_of_total_n'):
    check(k in empty, "a bot with NO trades must still carry '%s'" % k)
check(set(empty) == set(s), 'a silent bot and an active bot must have '
                            'IDENTICAL keys; differing: %r'
                            % (set(empty) ^ set(s)))

print()
print('=== CASE 6: fleet payload carries the same companions ===')
f = t.get_fleet_stats()
for k in ('win_rate_n', 'win_rate_basis', 'win_rate_degenerate'):
    check(k in f, "fleet stats must carry '%s' so a consumer can treat bot "
                  "and fleet payloads identically" % k)
check(f.get('win_rate_n') == f.get('fleet_decided'),
      'fleet win_rate_n must equal fleet_decided (the denominator in use)')

if FAIL:
    print()
    for f_ in FAIL:
        print('FAIL  ' + f_)
    sys.exit(1)
print()
print('ok  win rate always arrives with its denominator, and a rate forced '
      'by an empty ledger side is flagged rather than painted as a result')
