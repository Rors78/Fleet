"""Test data must be labelled as test data everywhere it is served.

Three related defects, all found by the ten-agent audit's measurement pass:

1. /api/trades served 50 unmarked synthetic probe rows (ZZPROBE*/NF<digits>*)
   worth +$617 beside 23 real closes worth -$167. The raw sum read +$450 —
   the SIGN of the fleet's trade feed depended on test data, and nothing in
   the payload distinguished the rows.

2. The TRADE_OPEN handler accepted `signals` as any truthy value. A bot that
   sent a plain STRING passed the check, and every consumer's
   `for sig in signals` iterated its CHARACTERS. The live decomposition store
   is the evidence: sources named "trekbot:n", "trekbot:z", bare "f" and "2"
   — each one a single character of some signal string.

3. /api/signals/decomposition computed its fleet-wide CUT/KEEP recommendation
   over exactly that confetti, from a bot retired from the fleet.

The store cannot be cleaned on disk (CC holds it in memory), so artifacts are
filtered at serve time with disclosure — excluded_artifacts / excluded_retired
name what was dropped rather than silently vanishing it.
"""
import importlib
import sys

sys.path.insert(0, 'D:/CommandCenter')

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


src = open('D:/CommandCenter/command_center.py', encoding='utf-8',
           errors='replace').read()

# ── 1. The probe predicate, both directions ──
import command_center as cc
importlib.reload(cc)

for pair in ('ZZPROBE138564B/USD', 'NF138587OK/USD', 'ZZ/USD', 'NFNOK/USD'):
    check(cc._is_probe_pair(pair), 'must classify %r as synthetic' % pair)
for pair in ('BTC/USD', 'ETH/USD', 'NEAR/USD', 'NFT/USD', 'ADA/USD'):
    check(not cc._is_probe_pair(pair),
          'must NOT classify the real pair %r as synthetic — excluding real '
          'trades is the opposite failure; NFT/USD starts with NF but its '
          'third character is a letter, not a digit' % pair)

# ── 2. /api/trades must stamp and disclose ──
check('synthetic=_is_probe_pair(t.get("pair"))' in src,
      'every served trade row must carry a synthetic flag')
for key in ('"real_total"', '"synthetic_total"', '"real_pnl"', '"synthetic_pnl"'):
    check(key in src,
          '/api/trades must disclose the real/synthetic split (%s) — an '
          'aggregate whose sign depends on unmarked test rows is the defect'
          % key)

# ── 3. The signals guard: strings must not be char-iterated ──
check('if isinstance(_sigs, str):' in src and '_sigs = [_sigs]' in src,
      'a lone signal STRING must be wrapped, not iterated character by '
      'character — the decomposition store is full of single-char sources '
      'from exactly this')
check('[s for s in _sigs if isinstance(s, str) and len(s) > 1]' in src,
      'single-character signal names are the artifact signature and must be '
      'filtered wherever they came from')

# Behavioural mirror of the guard:
def clean(sigs):
    if isinstance(sigs, str):
        sigs = [sigs]
    elif not isinstance(sigs, (list, tuple)):
        sigs = []
    return [s for s in sigs if isinstance(s, str) and len(s) > 1]


check(clean('momentum') == ['momentum'],
      'a lone real signal string must survive as one signal')
check(clean('2f') == ['2f'],
      'a lone string wraps to ONE name — the defect was iterating it into '
      'characters ["2","f"]; wrapped-and-kept is correct, got %r'
      % clean('2f'))
check(clean(['oracle_conf', 'x', 42, 'grid_fill']) == ['oracle_conf', 'grid_fill'],
      'lists keep real names, drop single chars and non-strings')
check(clean({'a': 1}) == [], 'a dict is not a signal list')

# ── 4. Decomposition serve-time filter, against the LIVE garbage shapes ──
import fleet_config as _fc
roster = set(_fc.BOTS)
rankings = {'trekbot:n': {}, 'f': {}, '2': {}, 'trekbot:zz_sig': {},
            'confluence:oracle_conf': {}, 'gridzilla:grid_fill': {}}
kept, artifacts, retired = {}, [], []
for key, row in rankings.items():
    bot, _, sig = str(key).partition(':')
    if len(sig if sig else bot) <= 1:
        artifacts.append(key)
    elif roster and bot not in roster:
        retired.append(key)
    else:
        kept[key] = row

check(sorted(kept) == ['confluence:oracle_conf', 'gridzilla:grid_fill'],
      'real current-fleet signals must survive the filter, got %r'
      % sorted(kept))
check(sorted(artifacts) == ['2', 'f', 'trekbot:n'],
      'single-character sources are artifacts, got %r' % sorted(artifacts))
check(retired == ['trekbot:zz_sig'],
      'a multi-char signal from a bot outside the registry is retired '
      'history, got %r' % retired)

check('excluded_artifacts' in src and 'excluded_retired' in src,
      'the endpoint must DISCLOSE what it filtered — silently vanishing rows '
      'is the same defect as silently serving them')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  synthetic rows stamped and disclosed; char-confetti filtered with receipts')
