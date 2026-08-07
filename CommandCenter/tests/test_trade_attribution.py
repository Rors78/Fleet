"""An unlabelled trade is not a long, and a silent proposal is not 0.5 confident.

command_center.py defaulted `direction` to "LONG" in seven places and
`confidence` to 0.5 in four. Three of those mattered:

1. The reservation-release path recorded into the DURABLE expectancy store
   with direction="LONG", entry_price=0 and exit_price=0. This is the path
   that put entry_price 0.0 / exit_price 0.0 on gridzilla's real +$34.90
   ETH/USD record — the P/L genuine, the prices fiction, r_multiple null as a
   direct consequence. Values written here outlive the process.

2. TRADE_OPEN fed `direction or "LONG"` and `confidence or 0.5` straight into
   SignalAggregator.submit_proposal, which computes
   `weighted = confidence * weight * freshness`. A fabricated 0.5 outvoted
   genuinely low-conviction proposals in a CONFIDENCE-WEIGHTED decision.

3. TRADE_CLOSE fed `direction or "LONG"` into record_outcome, which credits
   the per-direction hit rate — so every unlabelled close was attributed to
   the long side, skewing any later long/short comparison.

Live evidence that these were not hypothetical: both HIGH_CONVICTION events on
the bus carry NO direction field at all.

expectancy.record_trade has an explicit PNL-DRIVEN mode and only reads
`direction` on the price-driven branch, so None is safe and correct there.
"""
import importlib
import os
import re
import sys
import tempfile

sys.path.insert(0, 'D:/CommandCenter')

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


src = open('D:/CommandCenter/command_center.py', encoding='utf-8',
           errors='replace').read()

# ── 1. The durable-store path must not fabricate ──
check('direction=res_info.get("direction", "LONG")' not in src,
      'the release path still defaults direction to LONG when writing to the '
      'durable expectancy store')
check('entry_price=data.get("entry_price", 0)' not in src,
      'the release path still writes entry_price 0 into the durable store — '
      'that is a price nobody measured')
check('exit_price=data.get("exit_price", 0)' not in src,
      'the release path still writes exit_price 0 into the durable store')

# ── 2. The weighted vote must not be fed invented confidence ──
check('confidence=edata.get("confidence", 0.5)' not in src,
      'TRADE_OPEN still feeds a fabricated 0.5 confidence into the '
      'confidence-weighted aggregator vote')
check('direction=edata.get("direction", "LONG").upper()' not in src,
      'TRADE_OPEN still defaults direction to LONG')

# ── 3. Outcome attribution must not default to long ──
check('direction = edata.get("direction", "LONG").upper()' not in src,
      'TRADE_CLOSE still attributes an unlabelled close to the long side')

# ── 4. Behavioural: the aggregator must reject unmeasured confidence ──
import signal_aggregator as sa
importlib.reload(sa)
agg = sa.SignalAggregator()

check(agg.submit_proposal('trinity', 'BTC/USD', 'LONG', 0.8) is True,
      'a proposal with real confidence must be accepted')
check(agg.submit_proposal('oracle', 'BTC/USD', 'LONG', 0.0) is True,
      'a confidence of exactly 0.0 is a MEASUREMENT ("no conviction") and '
      'must be accepted — only absence is rejected')
for _bad in (None, 'high'):
    try:
        _res = agg.submit_proposal('ghost', 'BTC/USD', 'LONG', _bad)
    except Exception as _e:
        # Without the guard this raises TypeError inside max(0, min(1, ...)).
        # Report it as the finding it is rather than dying on a traceback.
        _res = 'RAISED %s' % type(_e).__name__
    check(_res is False,
          'a proposal with confidence=%r must be rejected, not weighted as '
          '0.5 — it would outvote a genuinely low-conviction proposal; got %r'
          % (_bad, _res))
check(len(agg.proposals.get('BTC/USD') or []) == 2,
      'only the two real proposals may be stored, got %d'
      % len(agg.proposals.get('BTC/USD') or []))

# The decision must still work on the surviving real proposals.
d = agg.decide('BTC/USD')
check(d.get('direction') == 'LONG',
      'the vote must still resolve from measured proposals, got %r'
      % d.get('direction'))

# ── 5. Behavioural: expectancy must accept None direction AND prices ──
import expectancy as ex
importlib.reload(ex)
t = ex.ExpectancyTracker()
t.PERSIST_PATH = os.path.join(tempfile.mkdtemp(), 'e.json')
t.trades = {}

t.record_trade(bot_id='turtlesue', pair='XRP/USD', direction=None,
               entry_price=None, exit_price=None, size_usd=15220.0,
               duration=900, realized_pnl=-3.24, trade_id='r1')
r = t.trades['turtlesue'][-1]
check(r.get('direction') is None,
      'an unlabelled trade must record direction None, got %r' % r.get('direction'))
check(r.get('entry_price') is None,
      'an unpriced trade must record entry_price None, got %r' % r.get('entry_price'))
check(abs(r.get('gross_pnl', 0) - (-3.24)) < 1e-9,
      'the real P/L must survive intact, got %r' % r.get('gross_pnl'))
check(r.get('won') is False,
      'the win/loss verdict must still derive from the real P/L')

# A genuine priced SHORT must still compute correctly (not silently long).
t.record_trade(bot_id='turtlesue', pair='XRP/USD', direction='SHORT',
               entry_price=100.0, exit_price=90.0, size_usd=1000.0,
               duration=60, trade_id='r2')
r2 = t.trades['turtlesue'][-1]
check(abs(r2.get('gross_pnl', 0) - 100.0) < 1e-9,
      'a real SHORT that fell 10%% must book +$100, got %r — if this reads '
      '-100 the direction is being ignored' % r2.get('gross_pnl'))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  unlabelled trades stay unlabelled; unmeasured proposals do not vote')
