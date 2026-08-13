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

# ── 6. Close attribution: absent pnl is not a loss; the warning tells the truth ──
# Live defect (2026-08-13): `pnl = edata.get("pnl", 0)` booked every
# unmeasured close as a LOSS (won = 0 > 0), and the "has no direction"
# warning was attached as the else of the DECAY-feed condition — it fired on
# turtlesue's XRP close (direction=SHORT present, flat P/L) while a close
# actually missing direction was skipped silently.
import textwrap

check('edata.get("pnl", 0)' not in src,
      'the handler still defaults an absent pnl to 0 — an unmeasured close '
      'is not a $0.00 loss')

_i_dir = src.find('has no direction')
_i_decay = src.find('Feed the decay tracker')
check(_i_dir != -1 and _i_decay != -1 and _i_dir < _i_decay,
      'the "has no direction" warning must live at the DIRECTION check, '
      'before the decay feed — not as the decay condition\'s else')
check(src.count('has no direction -- ') <= 2,   # TRADE_OPEN has its own
      'the misattached decay-else warning is back')

# The logger bridge (_on_logger_event) is the SECOND close path — it kept
# the full original defect set after the HTTP path was fixed. Both blocks
# must exist and both must behave.
check('direction = data.get("direction", "LONG")' not in src,
      'the logger bridge still calls every unlabelled close a long')
check('won=data.get("won", False)' not in src,
      '/api/signals/outcome still fabricates a loss from a malformed POST')
check('neither win nor loss' in src,
      '/api/signals/outcome must ignore flat/unmeasured pnl — turtlesue\'s '
      'forced XRP exit debited the SHORT hit rate with pnl -0.0')


class _Log:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *a):
        self.warnings.append(msg % a if a else msg)


class _Agg:
    def __init__(self):
        self.calls = []

    def record_outcome(self, *a):
        self.calls.append(a)


_blocks = {}
_mh = re.search(r'^[ ]*_praw = edata\.get\("pnl"\)\n[\s\S]*?nothing to attribute\.',
                src, re.M)
if _mh:
    _blocks['http'] = (textwrap.dedent(_mh.group(0)), 'edata')
_mb = re.search(r'^[ ]*_praw = data\.get\("pnl"\)\n[\s\S]*?record_outcome\(pair, direction, pnl > 0, pnl\)',
                src, re.M)
if _mb:
    _blocks['bridge'] = (textwrap.dedent(_mb.group(0)), 'data')
check('http' in _blocks, 'could not locate the HTTP close-attribution block')
check('bridge' in _blocks, 'could not locate the logger-bridge attribution block')

for _name, (_block, _var) in _blocks.items():
    def run_close(edata, _block=_block, _var=_var):
        ns = {_var: edata, 'source': 'bot', 'bot': 'bot', 'pair': 'X/USD',
              'log': _Log(), '_signal_aggregator': _Agg(), 'isinstance': isinstance}
        exec(_block, ns)
        return ns

    # A flat close WITH direction: no outcome, and NO warning — the old code
    # printed "has no direction" here, about a field that was present.
    n1 = run_close({'direction': 'SHORT', 'pnl': -0.0})
    check(not n1['_signal_aggregator'].calls and not n1['log'].warnings,
          '[%s] a flat close with direction present must attribute nothing '
          'and warn nothing; got calls=%r warnings=%r'
          % (_name, n1['_signal_aggregator'].calls, n1['log'].warnings))

    # Absent pnl: not a loss, and the warning says what is actually missing.
    n2 = run_close({'direction': 'LONG'})
    check(not n2['_signal_aggregator'].calls,
          '[%s] a close with NO pnl must not be booked as a loss' % _name)
    check(n2['pnl'] is None,
          '[%s] absent pnl must stay None downstream, got %r'
          % (_name, n2['pnl']))
    check(any('no measured pnl' in w for w in n2['log'].warnings),
          '[%s] the missing-pnl close must be warned about as missing PNL, '
          'got %r' % (_name, n2['log'].warnings))

    # Missing direction: warned as missing DIRECTION, loudly not silently.
    n3 = run_close({'pnl': 5.0})
    check(not n3['_signal_aggregator'].calls
          and any('no direction' in w for w in n3['log'].warnings),
          '[%s] a close missing direction must warn about direction; got '
          'calls=%r warnings=%r'
          % (_name, n3['_signal_aggregator'].calls, n3['log'].warnings))

    # A real measured close still attributes.
    n4 = run_close({'direction': 'long', 'pnl': 12.5})
    check(n4['_signal_aggregator'].calls == [('X/USD', 'LONG', True, 12.5)],
          '[%s] a measured close must still reach record_outcome, got %r'
          % (_name, n4['_signal_aggregator'].calls))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  unlabelled trades stay unlabelled; unmeasured proposals do not vote')
