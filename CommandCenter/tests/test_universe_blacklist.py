"""A blacklisted pair must not be served in the universe bots scan.

Observed 2026-08-13: SOL/USD is in fleet_config.BLACKLISTED_PAIRS and was
still present in all 50 entries of /api/universe.

No capital was at risk — reserve() gate 1 refuses blacklisted pairs, and
there were no SOL reservations in the pool. But the universe is what every
bot SCANS, so a blacklisted pair still consumed scan cycles, OHLC fetches
and Brainiac depth/trade collection for something the fleet can never
trade. And the bus showed ZERO blacklist denials for it — meaning nothing
ever got far enough to be refused, so the enforcement gate produced no
evidence the pair was being considered at all.

Two properties this pins:
  - blacklisted pairs are filtered at the SOURCE (universe discovery)
  - the reserve gate STILL refuses them independently, because a filter is
    a convenience and the gate is the enforcement. Defence in depth: if the
    filter is ever bypassed (config reload, import failure), capital must
    still be safe.
"""
import sys

sys.path.insert(0, 'D:/CommandCenter')

import fleet_config as fc

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


SRC = open('D:/CommandCenter/command_center.py', encoding='utf-8',
           errors='replace').read()

# ── 1. The universe filter exists, and runs BEFORE the pin logic ──
check('Drop blacklisted pairs at the SOURCE' in SRC,
      'universe discovery must filter blacklisted pairs')
_i_filter = SRC.find('qualified = [p for p in qualified if not _is_bl')
_i_pin = SRC.find('pinned = [p for p in deduped if p["display"] in pin]')
check(_i_filter != -1, 'the blacklist filter is missing from discovery')
check(_i_filter != -1 and _i_pin != -1 and _i_filter < _i_pin,
      'the filter must run BEFORE pinning — a pinned blacklisted pair '
      'would otherwise be reintroduced after the filter')

# ── 2. A blacklist import failure must NOT empty the universe ──
# Failing closed here would take the whole fleet offline over a config
# problem, and the reserve gate is the real enforcement anyway.
check('blacklist filter unavailable' in SRC,
      'an import failure must serve the universe unfiltered with a warning, '
      'not empty it — the reserve gate still enforces')

# ── 3. The reserve gate must remain independent of the filter ──
check('if is_blacklisted(pair):' in SRC,
      'reserve() must keep refusing blacklisted pairs on its own — the '
      'universe filter is a convenience, not the enforcement')

# ── 4. Behavioural: the filter actually removes the right pairs ──
_bl = sorted(fc.BLACKLISTED_PAIRS)
check(len(_bl) > 0, 'sanity: the blacklist must be non-empty for this test '
                    'to mean anything; got %r' % (_bl,))

_universe = [{"display": d} for d in
             ["BTC/USD", "ETH/USD"] + _bl + ["XRP/USD"]]
_filtered = [p for p in _universe if not fc.is_blacklisted(p["display"])]
_names = [p["display"] for p in _filtered]
for b in _bl:
    check(b not in _names,
          'blacklisted pair %r survived the filter' % b)
check("BTC/USD" in _names and "ETH/USD" in _names and "XRP/USD" in _names,
      'the filter must not drop tradeable pairs; got %r' % _names)
check(len(_filtered) == len(_universe) - len(_bl),
      'exactly the blacklisted pairs must be removed — %d in, %d out, %d '
      'blacklisted' % (len(_universe), len(_filtered), len(_bl)))

# ── 5. Kraken-format pairs must be caught too ──
# is_blacklisted normalizes, so the filter must work on either spelling.
check(fc.is_blacklisted("SOLUSD") or not fc.is_blacklisted("SOL/USD"),
      'is_blacklisted must normalize Kraken formats — a raw-format pair '
      'would otherwise slip past the filter while the display format is '
      'caught')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  blacklisted pairs leave the universe; the reserve gate still guards')
