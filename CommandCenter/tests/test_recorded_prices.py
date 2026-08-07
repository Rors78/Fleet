"""A recorded price of 0.0 is not a price anybody measured.

Gridzilla records completed grid cycles into the DURABLE expectancy store:

    entry_price=matching_buy.get("price", 0)

When the matching buy fill carried no price, that default wrote 0.0 into a
store that outlives the process, where it is indistinguishable from a real
reading. The live store still holds the evidence: a gridzilla ETH/USD trade
with entry_price 0.0 and exit_price 0.0 beside a genuine +$34.90 on a
$20,781 position. The P/L is real; the prices are fiction, and r_multiple is
null precisely because of it.

The bot's own P/L math already handled this correctly (`buy_price > 0` guarded
the qty calculation) — only the expectancy record fabricated a number.

expectancy.record_trade explicitly supports a PNL-DRIVEN mode for bots that
report P/L without prices, so None is the intended value here, not 0.
"""
import importlib
import os
import re
import sys
import tempfile

sys.path.insert(0, 'D:/CommandCenter')
import expectancy as ex
importlib.reload(ex)

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ── Structural: gridzilla must not default a missing price to 0 ──
src = open('D:/Gridzilla/gridzilla.py', encoding='utf-8', errors='replace').read()
check('entry_price=matching_buy.get("price", 0)' not in src,
      'gridzilla still defaults a missing buy price to 0 when recording to '
      'the durable expectancy store')
check(re.search(r'entry_price=\(buy_price if buy_price > 0\s*\n?\s*else None\)', src)
      is not None,
      'gridzilla must record entry_price None when no buy price was captured')

# ── Behavioural: the tracker must accept None and stay honest ──
t = ex.ExpectancyTracker()
t.PERSIST_PATH = os.path.join(tempfile.mkdtemp(), 'e.json')
# The tracker loads the real durable store on construction, so start from a
# clean slate — otherwise this asserts against whatever the live fleet has
# recorded and passes or fails for reasons unrelated to the code under test.
t.trades = {}

t.record_trade(bot_id='gridzilla', pair='ETH/USD', direction='LONG',
               entry_price=None, exit_price=2450.0, size_usd=20781.17,
               duration=4142.3, realized_pnl=34.9016, trade_id='g_none')
rec = t.trades['gridzilla'][-1]

check(rec.get('entry_price') is None,
      'an unknown entry price must be stored as None, got %r'
      % rec.get('entry_price'))
check(rec.get('entry_price') != 0.0,
      'an unknown entry price must never be stored as 0.0 — that reads as a '
      'measured price of zero')
check(abs(rec.get('gross_pnl', 0) - 34.9016) < 1e-6,
      'the real P/L must be preserved exactly, got %r' % rec.get('gross_pnl'))
check(rec.get('won') is True,
      'the win/loss verdict must still be derived from the real P/L')
check(rec.get('r_multiple') is None,
      'r_multiple must stay None when the entry price is unknown — it cannot '
      'be computed without one')

# ── A genuine price-driven trade must still record normally ──
t.record_trade(bot_id='gridzilla', pair='BTC/USD', direction='LONG',
               entry_price=100.0, exit_price=110.0, size_usd=1000.0,
               duration=60, realized_pnl=100.0, trade_id='g_ok')
r2 = t.trades['gridzilla'][-1]
check(r2.get('entry_price') == 100.0,
      'a measured entry price must still be recorded, got %r'
      % r2.get('entry_price'))

# ── Derived stats must be unaffected by the missing price ──
s = t.get_bot_stats('gridzilla')
check(s.get('total_trades') == 2,
      'both trades must count toward expectancy, got %r' % s.get('total_trades'))
check(s.get('win_rate') == 100.0,
      'win rate is derived from P/L, not price, and must be unaffected; got %r'
      % s.get('win_rate'))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  unknown prices record as None; real P/L and derived stats intact')
