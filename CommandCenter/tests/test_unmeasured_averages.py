"""An average over zero trades is not zero, and a missing confidence is not 0%.

Two published fabrications, both found by sweeping for the same shape after
the per-bot fake balances were purged.

--- 1. avg_loss = 0 on a bot that has never lost ---

expectancy.py computed

    avg_loss = sum(...) / loss_count if losses else 0

"The average losing trade lost $0.00" is a claim about trades that do not
exist. Gridzilla sits at 15W/0L and published avg_loss 0 to /api/expectancy
and the dashboard.

The tell is the neighbours: win_rate (line ~272), expectancy and
profit_factor in the SAME function each return None on exactly this
condition, each with a comment explaining that 0 would read as a
measurement. avg_win/avg_loss were the pair left behind -- and the
dashboard's own client-side recompute already used
`losses > 0 ? gross_loss/losses : null`, so client and server disagreed
about the same number.

--- 2. "Confidence: 0%" on every Oracle/Trinity regime card ---

signal_broadcaster.py had

    confidence = data.get("confidence", 0) or 0

rendered as f"Confidence: {confidence:.0%}" in the Intelligence card sent to
PAYING SUBSCRIBERS. Oracle (server.py:194) and Trinity (overwatch.py:931)
emit REGIME_CHANGE with no confidence key at all -- only HiveMind sends one
-- so every regime card from either bot published "Confidence: 0%".

That is worse than a silent absence: 0% does not read as "not scored", it
reads as an assertion that the regime change is worthless.

Same asymmetry again: fifteen lines above, risk_level/deployed_pct/bot_count
were hardened in an earlier pass under the comment "None, not a plausible
default". This line sat in the same function and was missed.
"""
import os
import re
import sys

sys.path.insert(0, 'D:/CommandCenter')

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ── 1. Behavioural: a zero-loss bot must not publish an average loss ──
import tempfile

from expectancy import ExpectancyTracker

# Redirect persistence to a throwaway file BEFORE recording anything.
#
# The first version of this test built the tracker with __new__ to "avoid
# disk" -- but record_trade() calls _save() itself, so it wrote two probe
# bots straight over logs/expectancy.json and erased the real history for
# gridzilla, confluence and turtlesue. It had to be rebuilt from the durable
# event_bus TRADE_CLOSE record.
#
# A test must never be able to touch live state. PERSIST_PATH is a class
# attribute, so pointing it at a temp file makes that structural rather than
# a matter of remembering.
_tmpdir = tempfile.mkdtemp(prefix='expectancy_test_')
ExpectancyTracker.PERSIST_PATH = os.path.join(_tmpdir, 'expectancy.json')

_t = ExpectancyTracker.__new__(ExpectancyTracker)
_t.trades = {}
_t.max_trades_per_bot = 500

# Three wins, no losses -- the Gridzilla shape.
for _i in range(3):
    _t.record_trade('probe_wins_only', 'BTC/USD', 'LONG',
                    entry_price=100.0, exit_price=110.0,
                    size_usd=10.0, duration=60, trade_id='w%d' % _i)
_s = _t.get_bot_stats('probe_wins_only')
check(_s is not None, 'get_bot_stats returned nothing for a bot with trades')
if _s:
    check(_s.get('avg_loss') is None,
          'a bot with 3 wins and 0 losses published avg_loss=%r. Zero losing '
          'trades is not "the average loss was $0.00" -- profit_factor and '
          'win_rate beside it already return None on this exact condition'
          % (_s.get('avg_loss'),))
    check(_s.get('avg_win') is not None,
          'avg_win must still be a real number when wins exist, got %r'
          % (_s.get('avg_win'),))

# And the mirror: losses only, no wins.
for _i in range(3):
    _t.record_trade('probe_loss_only', 'BTC/USD', 'LONG',
                    entry_price=100.0, exit_price=90.0,
                    size_usd=10.0, duration=60, trade_id='l%d' % _i)
_s2 = _t.get_bot_stats('probe_loss_only')
if _s2:
    check(_s2.get('avg_win') is None,
          'a bot with 0 wins published avg_win=%r' % (_s2.get('avg_win'),))
    check(_s2.get('avg_loss') is not None,
          'avg_loss must be real when losses exist, got %r'
          % (_s2.get('avg_loss'),))

# A bot with no trades at all must not report averages either.
_t.trades['probe_empty'] = []
_s3 = _t.get_bot_stats('probe_empty')
if _s3:
    check(_s3.get('avg_win') is None and _s3.get('avg_loss') is None,
          'a bot with no trades reported avg_win=%r avg_loss=%r'
          % (_s3.get('avg_win'), _s3.get('avg_loss')))

# ── 2. Source: no `or 0` may survive on a published average ──
_src = open('D:/CommandCenter/expectancy.py', encoding='utf-8',
            errors='replace').read()
_code = re.sub(r'#[^\n]*', '', _src)
for _m in re.finditer(r'avg_(?:win|loss)\w*\s*=\s*\([^)]*else 0\)', _code):
    check(False,
          'expectancy.py still defaults an average to 0: %r -- that is a '
          'measurement claim about trades that do not exist'
          % (_m.group(0)[:80],))

# ── 3. Behavioural: a regime card with no confidence must omit the line ──
import signal_broadcaster as _sb

_fmt = getattr(_sb, 'format_regime_or_aegis_narrator', None)
check(_fmt is not None,
      'format_regime_or_aegis_narrator not found -- test needs updating')
if _fmt is not None:
    # Exactly what Oracle emits: from/to/source, no confidence key.
    _oracle = {'from': 'RANGING', 'to': 'TRENDING', 'source': 'oracle'}
    try:
        _pulse, _intel = _fmt(_oracle, 'REGIME_CHANGE')
    except Exception as _e:
        _intel = ''
        FAIL.append('regime card raised on a confidence-less payload: %r'
                    % (_e,))
    check('Confidence: 0%' not in _intel,
          'a REGIME_CHANGE carrying no confidence published '
          '"Confidence: 0%" to paying subscribers. 0%% does not read as '
          '"not scored", it reads as an assertion that the shift is '
          'worthless -- omit the line instead')

    # A real confidence must still render.
    _hm = dict(_oracle, confidence=0.82)
    try:
        _p2, _i2 = _fmt(_hm, 'REGIME_CHANGE')
    except Exception as _e:
        _i2 = ''
        FAIL.append('regime card raised on a payload WITH confidence: %r'
                    % (_e,))
    check('Confidence: 82%' in _i2,
          'a genuine confidence of 0.82 must still render as 82%%; the card '
          'body was:\n%s' % (_i2[:400],))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  unmeasured averages and unscored confidences read as absent')
