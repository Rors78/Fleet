"""A verdict from five trades is a coin-read, not a measurement.

/api/expectancy published 'PROFITABLE' for gridzilla at 5W/0L and 'LOSING'
for confluence from exactly one trade. What those samples establish: a fair
coin produces the fleet's 5W/1L 10.9% of the time, gridzilla's Wilson 95% CI
is [56.6%, 100%], and profit_factor 999 is a division-by-zero placeholder.
~188 trades are needed to resolve an effect of the observed size at 80% power.

Separately, fleet_expectancy averages DOLLARS while stored notionals span
5.7x, so one large loser outweighs five small winners — and on 2026-08-07 the
dollar mean and the size-normalized mean DISAGREED IN SIGN (-$47.04/trade vs
+0.23%/trade). Publishing only dollars presents a size-weighted artefact as
the fleet's edge.

Now: below MIN_VERDICT_N decided trades the verdict is EARLY_POSITIVE /
EARLY_NEGATIVE (the sign without the certainty), every ranking carries the
Wilson CI and n_decided, and expectancy_pct ships beside the dollar figure
with its sd and n.
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


def add(t, bot, pnl, size, i):
    t.record_trade(bot_id=bot, pair='X/USD', direction='LONG',
                   entry_price=None, exit_price=None, size_usd=size,
                   duration=60, realized_pnl=pnl, trade_id='%s%d' % (bot, i))


# ── 1. Small samples get EARLY_*, not verdicts ──
t = fresh()
for i, (p, s) in enumerate([(34.9, 20781), (162.98, 41857), (30.34, 8371),
                            (47.74, 12000), (78.08, 15000)]):
    add(t, 'gridzilla', p, s, i)
add(t, 'confluence', -636.29, 41283, 0)

fs = t.get_fleet_stats()
rk = {r['bot']: r for r in fs['bot_rankings']}

check(rk['gridzilla']['verdict'] == 'EARLY_POSITIVE',
      "5W/0L must read EARLY_POSITIVE, not PROFITABLE — profit_factor 999 is "
      "a division-by-zero placeholder, not a measurement; got %r"
      % rk['gridzilla']['verdict'])
check(rk['confluence']['verdict'] == 'EARLY_NEGATIVE',
      "a verdict from exactly ONE trade must read EARLY_NEGATIVE, not "
      "LOSING; got %r" % rk['confluence']['verdict'])

# ── 2. The CI ships with the ranking ──
ci = rk['gridzilla']['win_rate_ci_95']
check(ci is not None and abs(ci[0] - 56.6) < 0.2 and ci[1] == 100.0,
      "gridzilla's 5W/0L Wilson CI must be ~[56.6, 100.0] — the interval IS "
      "what n=5 establishes; got %r" % (ci,))
check(rk['gridzilla']['n_decided'] == 5 and rk['confluence']['n_decided'] == 1,
      'n_decided must ship beside every verdict')

# ── 3. Size-normalized expectancy ships beside dollars ──
check(fs['fleet_expectancy'] < 0,
      'the dollar mean of this sample is negative (one large loser), got %r'
      % fs['fleet_expectancy'])
check(fs['expectancy_pct'] is not None and fs['expectancy_pct'] > 0,
      'the size-normalized mean of the same sample is POSITIVE — the two '
      'disagree in sign, which is exactly why both must be published; got %r'
      % fs['expectancy_pct'])
check(fs['expectancy_pct_n'] == 6,
      'the normalized n must be disclosed, got %r' % fs['expectancy_pct_n'])

# ── 4. A genuinely sampled bot still earns a real verdict ──
t2 = fresh()
for i in range(12):
    add(t2, 'rubberband', 10.0 if i % 3 else -5.0, 1000, i)
fs2 = t2.get_fleet_stats()
v = {r['bot']: r for r in fs2['bot_rankings']}['rubberband']
check(v['verdict'] == 'PROFITABLE',
      'a bot with %d decided trades and positive expectancy must still read '
      'PROFITABLE — the gate must not neuter real verdicts; got %r'
      % (v['n_decided'], v['verdict']))

# ── 5. A truly unmeasured bot stays UNMEASURED ──
t3 = fresh()
t3.trades['arbitrageur'] = []
add(t3, 'nexusbrain', 5.0, 1000, 0)
fs3 = t3.get_fleet_stats()
v3 = {r['bot']: r for r in fs3['bot_rankings']}
check(v3['arbitrageur']['verdict'] == 'UNMEASURED',
      'zero trades must stay UNMEASURED, got %r' % v3['arbitrageur']['verdict'])
check(v3['arbitrageur']['win_rate_ci_95'] is None,
      'no CI can be computed from zero trades, got %r'
      % v3['arbitrageur']['win_rate_ci_95'])

# ── 6. Trades without a real size are excluded from the pct, disclosed ──
t4 = fresh()
add(t4, 'turtlesue', 10.0, 1000, 0)
t4.record_trade(bot_id='turtlesue', pair='Y/USD', direction='LONG', entry_price=None,
                exit_price=None, size_usd=0, duration=60, realized_pnl=5.0,
                trade_id='ts-nosize')
fs4 = t4.get_fleet_stats()
check(fs4['expectancy_pct_n'] == 1,
      'a trade with size 0 cannot be normalized and must not enter the pct '
      '(division by zero disguised as a return); got n=%r'
      % fs4['expectancy_pct_n'])

# ── 7. Flat trades are not losses, and a flat-only bot is UNMEASURED ──
# Live defect (2026-08-08): turtlesue's two stale-reservation cleanups —
# gross $0.00, size 0 — rendered as "2 losses, 0% win rate, EARLY_NEGATIVE".
# The old classifier bucketed `gross_pnl <= 0` as a loss, so two closes that
# measured NOTHING produced a losing verdict. Third sibling of the defect
# already fixed in weekly_analysis and fleet_logger.
t5 = fresh()
add(t5, 'turtlesue', 0.0, 0, 0)
add(t5, 'turtlesue', 0.0, 0, 1)
add(t5, 'gridzilla', 25.0, 1000, 0)
fs5 = t5.get_fleet_stats()
r5 = {r['bot']: r for r in fs5['bot_rankings']}

check(r5['turtlesue']['verdict'] == 'UNMEASURED',
      'a bot whose closes were ALL $0.00 capital movements must read '
      'UNMEASURED — a losing verdict from trades that measured nothing is '
      'the defect; got %r' % r5['turtlesue']['verdict'])
check(r5['turtlesue']['n_decided'] == 0 and r5['turtlesue']['flat'] == 2,
      'flats must be counted as flat, not decided; got n_decided=%r flat=%r'
      % (r5['turtlesue']['n_decided'], r5['turtlesue']['flat']))
ts_stats = fs5['bot_stats']['turtlesue']
check(ts_stats['losses'] == 0 and ts_stats['win_rate'] is None,
      'flat closes must not count as losses and the win rate over zero '
      'decided trades must be None, not 0%%; got losses=%r win_rate=%r'
      % (ts_stats['losses'], ts_stats['win_rate']))

# A measured break-even on a REAL size is still flat — but a real loss is a loss.
t6 = fresh()
add(t6, 'rubberband', -10.0, 1000, 0)
fs6 = t6.get_fleet_stats()
rb = fs6['bot_stats']['rubberband']
check(rb['losses'] == 1 and rb['win_rate'] == 0.0,
      'a genuine measured loss must still count as a loss with a real 0%% '
      'rate; got losses=%r win_rate=%r' % (rb['losses'], rb['win_rate']))

# ── 8. Profit factor with no losing side is None, not a 999 placeholder ──
# Live render (2026-08-13): gridzilla 12W/0L put "999.00" in the scoreboard's
# PF column — a division-by-zero placeholder wearing the costume of a
# measurement. The ratio has no denominator; the wins/losses counts already
# carry the information.
gz = fs['bot_stats']['gridzilla']          # 5W/0L fixture from case 1
check(gz['profit_factor'] is None,
      'profit factor with ZERO gross losses must be None — 999 is a '
      'placeholder, not a ratio; got %r' % gz['profit_factor'])
rb2 = fs2['bot_stats']['rubberband']       # case 4: real wins AND losses
check(rb2['profit_factor'] is not None and rb2['profit_factor'] > 0,
      'a bot with both sides measured must still get a real profit factor; '
      'got %r' % rb2['profit_factor'])
cf = fs['bot_stats']['confluence']         # case 1: one loss, no wins
check(cf['profit_factor'] == 0.0,
      'all-losses is a measured 0.0 ratio (numerator zero, denominator '
      'real), not None; got %r' % cf['profit_factor'])

# ── 9. Payload SHAPE must not change with its values ──
# Live 2026-08-13: /api/expectancy returned turtlesue with flat=None
# decided=None while gridzilla and confluence carried real integers,
# because the zero-trade return omitted those keys entirely. A consumer
# doing s.get('decided') could not tell "no trades" from "field absent".
# They are COUNTS, so the empty case is 0 — the same rule this module
# applies everywhere else (counts 0, rates None).
t9 = fresh()
add(t9, 'active', 10.0, 1000, 0)
_full_bot = set(t9.get_bot_stats('active').keys())
_empty_bot = set(t9.get_bot_stats('never_traded').keys())
check(_full_bot == _empty_bot,
      'a bot with NO trades must return the same KEYS as one with trades — '
      'missing: %r, extra: %r'
      % (sorted(_full_bot - _empty_bot), sorted(_empty_bot - _full_bot)))
_e = t9.get_bot_stats('never_traded')
# .get with a MISSING sentinel, not [] — the whole point is that these keys
# may be absent, so indexing would KeyError on exactly the defect under
# test and print nothing at all.
check(_e.get('flat', 'MISSING') == 0 and _e.get('decided', 'MISSING') == 0,
      'flat/decided are counts and must be 0 for a silent bot, not None or '
      'absent; got flat=%r decided=%r'
      % (_e.get('flat', 'MISSING'), _e.get('decided', 'MISSING')))
check(_e.get('win_rate') is None and _e.get('profit_factor') is None,
      'rates and ratios must stay None over zero samples — the counts-vs-'
      'rates distinction must survive this fix')

t10 = fresh()
_empty_fleet = set(t10.get_fleet_stats().keys())
add(t10, 'a', 5.0, 1000, 0)
_full_fleet = set(t10.get_fleet_stats().keys())
check(_full_fleet == _empty_fleet,
      'the FLEET payload must be shape-stable too — missing when empty: %r'
      % sorted(_full_fleet - _empty_fleet))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  small samples read EARLY, CIs ship with verdicts, pct beside dollars')
