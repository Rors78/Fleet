"""An error a bot logs must reach disk, not just an in-memory buffer.

Aegis, Sentinel and PhiTex route every internal log line through
`self._log()`, which appended to `deque(maxlen=100)` and nothing else. The API
exposes the last 20 entries; nothing persists them. So an error existed only
in memory: absent from logs/bots/<bot>.log and gone entirely on restart.

The severity is in the caller. AEGIS's scan loop prints nothing on a healthy
pass and routes its ONLY error path through `_log`:

    try:
        _engine.compute()
    except Exception as e:
        _engine._log(f"ERROR: {e}")

so a crash-looping compute() left no trace on disk at all — the log file would
look exactly as it does when everything is fine. That is the same
"absence reads as health" shape as the launcher buffering bug, reached by a
different route.

Errors/warnings are now mirrored to stdout, which the launcher captures.
Healthy lines must stay quiet so the log is not flooded.
"""
import ast
import io
import re
import sys
from collections import deque
from datetime import datetime, timezone

FAIL = []
BOTS = (('aegis', 'D:/Aegis/aegis.py'),
        ('sentinel', 'D:/Sentinel/sentinel.py'),
        ('phitex', 'D:/PhiTex/phitex.py'))


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


for name, path in BOTS:
    src = open(path, encoding='utf-8', errors='replace').read()
    try:
        ast.parse(src)
    except SyntaxError as e:
        FAIL.append('%s does not parse: %s' % (name, e))
        continue

    m = re.search(r'    def _log\(self, msg\):\n(?:.*\n)*?(?=\n    def )', src)
    if not m:
        FAIL.append('could not locate _log in %s' % path)
        continue

    ns = {}
    exec('class Shim:\n    def __init__(self):\n        self._log_buf = deque(maxlen=100)\n'
         + m.group(0),
         {'deque': deque, 'datetime': datetime, 'timezone': timezone}, ns)
    shim = ns['Shim']()

    cap = io.StringIO()
    real, sys.stdout = sys.stdout, cap
    try:
        shim._log('compute cycle ok')          # healthy — must NOT print
        shim._log('scan complete, 56 pairs')   # healthy — must NOT print
        shim._log('ERROR: fetch failed')
        shim._log('WARNING: stale snapshot')
        shim._log('regime FAILOVER engaged')
    finally:
        sys.stdout = real

    printed = [l for l in cap.getvalue().splitlines() if l.strip()]

    check(len(shim._log_buf) == 5,
          '%s: all 5 lines must still reach the in-memory buffer, got %d'
          % (name, len(shim._log_buf)))
    check(len(printed) == 3,
          '%s: the 3 error/warning lines must be mirrored to stdout, got %d'
          % (name, len(printed)))
    check(not any('cycle ok' in p or 'scan complete' in p for p in printed),
          '%s: healthy lines must NOT be mirrored — that floods the log and '
          'buries real errors' % name)
    check(any('ERROR: fetch failed' in p for p in printed),
          '%s: an ERROR line must reach stdout so the launcher captures it '
          'to disk' % name)
    check(any('FAILOVER' in p for p in printed),
          '%s: a FAIL* line must reach stdout' % name)

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  %d bots mirror errors to disk; healthy lines stay quiet'
      % len(BOTS))
