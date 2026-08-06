import sys, os
sys.path.insert(0, r'D:\CommandCenter')
import os as _os
_OUT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'out')
_os.makedirs(_OUT, exist_ok=True)
from signal_broadcaster import CardFormatter as C

cases = [
    ('Avg P/L   \u2014 per trade', 'drop'),
    ('Regime    \u2014', 'drop'),
    ('Stop      None', 'drop'),
    ('Size      null', 'drop'),
    ('Entry     0.1628', 'keep'),
    ('Range     \u2014 to 1.23', 'keep'),
    ('<code>\u2501\u2501\u2501</code>', 'keep'),
    ('Win Rate  8%', 'keep'),
    ('Avg P/L   $-35.42 (2/18 bots) per trade', 'keep'),
    ('Bot       Stalker', 'keep'),
]
lines, bad = [], 0
for txt, exp in cases:
    got = C._drop_empty_rows(txt)
    ok = (got == '') if exp == 'drop' else (got == txt)
    bad += not ok
    lines.append('%-6s %-45s %s' % (exp, repr(txt)[:45], 'OK' if ok else '*** WRONG ***'))
lines.append('')
lines.append('ALL CORRECT' if not bad else '%d WRONG' % bad)
dest = os.path.join(_OUT, 'rows.txt')
open(dest, 'w', encoding='utf-8').write('\n'.join(lines))
print('wrong:', bad)
