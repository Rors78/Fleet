"""An unmeasured close is not a loss on the daily summary.

fleet_logger.py builds /api/fleet/daily and the file the subscriber-facing
daily card reads. Its aggregation was:

    pnl = pos.get("unrealized_pnl", 0)         # absent P/L seeded as 0
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    losses = total_trades - wins               # everything not a win is a loss

Three collapses in two lines: an ABSENT P/L became a measured $0.00, a flat
$0.00 capital movement became a loss, and the win rate divided by every close
rather than decided ones. A bot whose trade log omitted P/L silently deflated
the fleet's published daily win rate. This is the same defect the weekly
report had (fixed in 2b1146a); this was the other aggregation path, found by
the beacon-x-broadcaster agent and deliberately left for a focused change.

The event side also seeded `pnl` from `unrealized_pnl` with a 0 default, so
the None never even reached the aggregation — both halves had to move
together, and `t.get("pnl", 0)` does NOT default a key that is present with a
null value, so the old aggregation would have raised TypeError the moment the
seed was fixed. That coupling is why this test exercises the REAL
_write_daily_summary end to end rather than a reimplementation.
"""
import importlib
import json
import os
import sys
import tempfile

sys.path.insert(0, 'D:/CommandCenter')
import fleet_logger as fl
importlib.reload(fl)

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


tmpdir = tempfile.mkdtemp()
fl.DAILY_DIR = tmpdir
lg = fl.FleetLogger.__new__(fl.FleetLogger)
lg._starting_equity = 0

TRADES = [
    {'bot': 'gridzilla', 'pair': 'ETH/USD', 'pnl': 34.90, 'duration_s': 60},
    {'bot': 'gridzilla', 'pair': 'POL/USD', 'pnl': 162.98, 'duration_s': 60},
    {'bot': 'confluence', 'pair': 'ENA/USD', 'pnl': -636.29, 'duration_s': 60},
    {'bot': 'gridzilla', 'pair': 'LINK/USD', 'pnl': 0.0, 'duration_s': 60},
    {'bot': 'turtlesue', 'pair': 'XRP/USD', 'pnl': None, 'duration_s': 60},
    {'bot': 'turtlesue', 'pair': 'XLM/USD', 'pnl': None, 'duration_s': 60},
]


def daily(name, trades):
    lg._write_daily_summary(name, daily_data={
        'trades': trades, 'status_changes': [], 'regime_changes': 0,
        'whale_alerts': 0, 'portfolio_denials': 0}, trades_list=trades)
    return json.load(open(os.path.join(tmpdir, name + '.json'),
                          encoding='utf-8'))


d = daily('2099-01-01', TRADES)
f = d['fleet']

# ── Wins/losses only from measured, non-zero P/L ──
check(f['wins'] == 2 and f['losses'] == 1,
      'expected 2W/1L from the measured closes, got %sW/%sL — the old '
      '`losses = total - wins` read this as 2W/4L'
      % (f['wins'], f['losses']))
check(f.get('flat') == 1,
      'the $0.00 capital movement must be counted as flat, not a loss')
check(f.get('unpriced') == 2,
      'the two closes with no P/L must be counted as unpriced, not losses; '
      'got %r' % f.get('unpriced'))

# ── Win rate over decided trades ──
check(f.get('decided') == 3, 'decided must be wins+losses = 3')
check(abs(f['win_rate'] - 66.7) < 0.1,
      'win rate must be 2/3 = 66.7%% over decided trades, got %r — over all '
      'six closes it would read a deflated 33%%' % f['win_rate'])

# ── The total must be a sum of measurements only ──
check(abs(f['daily_pnl'] - (-438.41)) < 0.01,
      'daily P/L must sum only measured values, got %r' % f['daily_pnl'])

# ── Per-bot: a bot with only unpriced closes has no W/L record ──
tb = d['per_bot']['turtlesue']
check(tb['unpriced'] == 2 and tb['wins'] == 0 and tb['losses'] == 0,
      'a bot whose closes carried no P/L must show unpriced=2, 0W/0L — not '
      'two losses; got %r' % tb)
check(tb['worst_trade'] is None,
      'worst_trade cannot be derived from unmeasured P/L, got %r'
      % tb['worst_trade'])

# ── A zero-decided day must not claim a 0% win rate ──
d2 = daily('2099-01-02',
           [{'bot': 'x', 'pair': 'A/USD', 'pnl': None, 'duration_s': 1}])
check(d2['fleet']['win_rate'] is None,
      'a day with nothing decided must report win_rate None — 0.0 reads as '
      '"measured, and the fleet lost every trade"; got %r'
      % d2['fleet']['win_rate'])

# ── A clean all-win day still reads 100 ──
d3 = daily('2099-01-03',
           [{'bot': 'g', 'pair': 'B/USD', 'pnl': 5.0, 'duration_s': 1},
            {'bot': 'g', 'pair': 'C/USD', 'pnl': 7.0, 'duration_s': 1}])
check(d3['fleet']['win_rate'] == 100.0,
      'a genuine 2W/0L day must still read 100%%, got %r'
      % d3['fleet']['win_rate'])

if FAIL:
    for f_ in FAIL:
        print('FAIL  ' + f_)
    sys.exit(1)
print('ok  unmeasured closes are unpriced, not losses; win rate over decided only')
