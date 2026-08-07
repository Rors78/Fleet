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
        ('phitex', 'D:/PhiTex/phitex.py'),
        # nexus was MISSED by the first pass of this fix — it has the same
        # buffer-only _log and is the busiest engine in the fleet. Its log sat
        # at 672 bytes while its cycle counter advanced 54 -> 77.
        ('nexus', 'D:/Nexus/nexus.py'))

# Severities that MUST reach disk. The first version of the mirror gate
# matched only ERROR/WARN/FAIL, which excluded the most severe lines the
# fleet emits: PhiTex sends "CRITICAL:"/"PRE_CRITICAL:" and Sentinel sends
# "Degraded cycle ...". A gate that catches warnings but drops criticals is
# worse than no gate, because a quiet log then reads as calm at exactly the
# moment it should not.
MUST_MIRROR = ('ERROR: fetch failed',
               'WARNING: stale snapshot',
               'regime FAILOVER engaged',
               'CRITICAL: fusion meltdown',
               'PRE_CRITICAL: ALGO/USD approaching transition',
               'Degraded cycle 4: 2 sources unreachable')
MUST_STAY_QUIET = ('compute cycle ok',
                   'scan complete, 56 pairs')


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

    # nexus routes its gate through a module-level _is_severe(); pull the real
    # one in rather than reimplementing it, so this tests the shipped policy.
    g = {'deque': deque, 'datetime': datetime, 'timezone': timezone}
    sev = re.search(r'def _is_severe\(msg\):\n(?:.*\n)*?(?=\n\ndef )', src)
    if sev:
        exec(sev.group(0), g)
    ns = {}
    exec('class Shim:\n    def __init__(self):\n        self._log_buf = deque(maxlen=100)\n'
         + m.group(0), g, ns)
    shim = ns['Shim']()

    cap = io.StringIO()
    real, sys.stdout = sys.stdout, cap
    try:
        for m in MUST_STAY_QUIET:
            shim._log(m)
        for m in MUST_MIRROR:
            shim._log(m)
    finally:
        sys.stdout = real

    printed = [l for l in cap.getvalue().splitlines() if l.strip()]
    total = len(MUST_MIRROR) + len(MUST_STAY_QUIET)

    check(len(shim._log_buf) == total,
          '%s: all %d lines must still reach the in-memory buffer, got %d'
          % (name, total, len(shim._log_buf)))
    check(len(printed) == len(MUST_MIRROR),
          '%s: all %d severe lines must be mirrored to stdout, got %d'
          % (name, len(MUST_MIRROR), len(printed)))
    for m in MUST_MIRROR:
        check(any(m in p for p in printed),
              '%s: %r must reach stdout so the launcher captures it to disk'
              % (name, m))
    for m in MUST_STAY_QUIET:
        check(not any(m in p for p in printed),
              '%s: healthy line %r must NOT be mirrored — that floods the log '
              'and buries real errors' % (name, m))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  %d bots mirror errors to disk; healthy lines stay quiet'
      % len(BOTS))
