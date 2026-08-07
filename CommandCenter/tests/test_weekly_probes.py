"""The weekly report must not count test data as fleet results.

Three separate defects stacked in weekly_analysis.py, each hiding the next:

1. EVENT_DIR pointed at `logs/events` — 1.1 MB, five event types, ZERO
   TRADE_CLOSE. The live bus writes to `logs/event_bus` (724 MB, 77 closes).
   Both hold real .jsonl files, so the glob matched, nothing raised, and every
   run reported "0 closes / No trades to analyze". A silent zero rendering as
   health.

2. With the directory fixed it read `t.get("pnl", 0)` on the OUTER event
   envelope, but P/L lives at `data.pnl`. Every close returned 0, and
   `losses = total - wins` turned all 77 into losses: "0W / 77L, $+0.00" —
   incoherent on its face, which is what exposed it.

3. With the key fixed it counted 17 exactly-$0.00 closes (capital movements:
   grid teardowns, cancelled entries, re-reservations) in the win-rate
   denominator, deflating 98% to 77%.

Then the real one. 55 of 79 closes were SYNTHETIC probe rows from test
harnesses exercising the live path — 55W/0L worth +$678.70. They flipped the
weekly headline from a true -$119.27 to a reported +$559.43. A report whose
SIGN depends on test data is worse than no report.

Nothing in the payload marks a probe, so the pair-name prefix is the only
signal available. That is a workaround, not a contract — if probes ever gain
a `synthetic: true` field, this should key on it instead.
"""
import re
import sys

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


src = open('D:/CommandCenter/weekly_analysis.py', encoding='utf-8',
           errors='replace').read()

# ── 1. The directory must be the one the bus actually writes ──
check('EVENT_DIR = os.path.join(LOG_DIR, "event_bus")' in src,
      'weekly_analysis EVENT_DIR must point at logs/event_bus — logs/events '
      'contains zero TRADE_CLOSE, so every figure would come from an empty '
      'input while nothing errors')

ev = open('D:/CommandCenter/evolution.py', encoding='utf-8',
          errors='replace').read()
check('EVENT_DIR = os.path.join(LOG_DIR, "event_bus")' in ev,
      'evolution EVENT_DIR must point at logs/event_bus')
check('def assert_event_dir_has_closes' in ev,
      'evolution must carry the guard that names a misconfigured EVENT_DIR '
      'rather than reporting a confident zero')

# ── 2. P/L must be read from data.pnl, not the envelope ──
check('d.get("pnl", t.get("pnl"))' in src,
      'P/L must be read from data.pnl — reading the outer envelope returned '
      'None for every close and reported 0W/77L at $+0.00')

# ── 3/4. Probes excluded, flat closes excluded from the win rate ──
check('def _is_probe' in src, 'weekly must identify synthetic probe closes')
check('trade_closes = [t for t in trade_closes if not _is_probe(t)]' in src,
      'probe closes must be REMOVED from the trade set, not merely counted')
check('_resolved = wins + losses' in src,
      'the win-rate denominator must be decided trades, not every close — a '
      '$0.00 close is a capital movement, neither a win nor a loss')

# ── Behavioural: replicate the shipped predicates ──
_NF = re.compile(r"^NF\d+")


def is_probe(pair):
    p = str(pair or '').upper()
    return p.startswith(("ZZPROBE", "ZZ", "NFNOK")) or bool(_NF.match(p))


for pair in ('ZZPROBE138564B/USD', 'NF138587OK/USD', 'ZZ/USD', 'NFNOK/USD'):
    check(is_probe(pair), 'must classify %r as a synthetic probe' % pair)
for pair in ('BTC/USD', 'ETH/USD', 'POL/USD', 'ENA/USD', 'NEAR/USD'):
    check(not is_probe(pair),
          'must NOT classify the real pair %r as a probe — excluding real '
          'trades is the opposite failure and just as bad' % pair)


def summarize(rows):
    """Mirrors the shipped counting: probes out, flat out of the denominator."""
    real = [r for r in rows if not is_probe(r[0])]
    decided = [p for _, p in real if isinstance(p, (int, float))]
    wins = sum(1 for p in decided if p > 0)
    losses = sum(1 for p in decided if p < 0)
    flat = sum(1 for p in decided if p == 0)
    resolved = wins + losses
    return {'closes': len(real), 'wins': wins, 'losses': losses, 'flat': flat,
            'wr': (wins / resolved * 100) if resolved else None,
            'pnl': sum(decided)}


# The live shape on 2026-08-07: probes dominate and flip the sign.
rows = ([('ZZPROBE1/USD', 12.34)] * 55
        + [('ENA/USD', -636.29), ('POL/USD', 162.98), ('ETH/USD', 78.08),
           ('ETH/USD', 47.74), ('ETH/USD', 30.34), ('ETH/USD', 34.90),
           ('BTC/USD', 162.98)]
        + [('LINK/USD', 0.0)] * 17)
s = summarize(rows)

check(s['closes'] == 24,
      'the 55 probe rows must be excluded, leaving 24 real closes, got %d'
      % s['closes'])
check(s['pnl'] < 0,
      'with probes excluded the real P/L is NEGATIVE — if this reads positive '
      'the probes are leaking back in and inverting the headline; got %+.2f'
      % s['pnl'])
check(s['wins'] == 6 and s['losses'] == 1,
      'expected 6W/1L on the real rows, got %dW/%dL' % (s['wins'], s['losses']))
check(s['flat'] == 17, 'the 17 flat closes must be counted separately')
check(s['wins'] + s['losses'] == 7,
      'W+L must equal the decided count — 6W/1L over n=7, not over all 24')

# The un-excluded version must be visibly different, or the test proves nothing.
all_decided = [p for _, p in rows]
check(sum(all_decided) > 0,
      'sanity: with probes INCLUDED the total should be positive — that is '
      'the defect this test exists to prevent')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  probes excluded, flat closes disclosed, real P/L reported honestly')
