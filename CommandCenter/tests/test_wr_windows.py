"""Two win rates on one screen must each name their window.

The dashboard header chip and the EXPECTANCY panel both label themselves the
fleet win rate, and on 2026-08-13 they read 55.3% (n=38) and 73.1% (n=27) at
the same instant. Neither is wrong:

    header : each bot's own lifetime wins/losses counters, restored from its
             state file across restarts. Confluence's reach to 2026-07-28.
    panel  : logs/expectancy.json, whose earliest record is 2026-08-06.

Eight Confluence trades closed before 2026-08-07 12:28 live in the bot's
records and not in the store, and that gap is the entire difference. The
figures are two honest measurements over different spans -- but presented as
one quantity with nothing to tell them apart, which reads as a contradiction
and invites treating one of them as broken.

This is the same disclosure rule the cards already follow: a number that
cannot be compared must say so next to itself, not in someone's head. It is
NOT a bug to fix by forcing the two to agree -- the windows genuinely differ,
and collapsing them would throw away either the longer history or the
size-normalised store.

Also pins the parser check. A regex brace-counter cannot decide this file:
83 comment lines contain apostrophes and regex literals look like division,
and it returned contradictory answers depending on whether strings or
comments were stripped first -- once reporting that a harmless edit had
broken the page. Only a real JS parser settles it.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, 'D:/CommandCenter')

HTML = 'D:/CommandCenter/command_center_v4.html'
FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


_src = open(HTML, encoding='utf-8', errors='replace').read()

# ── 1. Each figure must name its own window ──
check("Fleet win rate over each bot" in _src,
      'the header WR chip must disclose that it measures bot LIFETIME '
      'counters -- without it, the panel showing a different number reads '
      'as one of them being broken')
check("Win rate over the recorded trade store" in _src,
      'the EXPECTANCY panel win rate must disclose that it measures the '
      'trade store, which begins later than the bot counters')

# Both disclosures must carry the sample size, so the reader can see WHY the
# windows differ rather than just being told that they do.
for _needle, _who in (("Fleet win rate over each bot", "header"),
                      ("Win rate over the recorded trade store", "panel")):
    _i = _src.find(_needle)
    _window = _src[_i:_i + 420] if _i > 0 else ""
    check("n=" in _window,
          '[%s] the disclosure must carry the sample size -- "these differ" '
          'without the two n values is an assertion, not evidence' % _who)

# The disclosures must be double-quoted JS strings. Both contain apostrophes
# ("bot's", "Confluence's"), and an apostrophe inside a single-quoted string
# is exactly what blacked out this page earlier in the session.
for _line in _src.splitlines():
    if ('wrEl.title=' in _line.replace(' ', '')
            or 'ratioPctEl.title=' in _line.replace(' ', '')):
        check('"' in _line,
              'a title assignment must open with a double quote -- these '
              "strings contain apostrophes and a single-quoted one would "
              'break the whole inline script; got: %s' % _line.strip()[:90])

# ── 2. The page must PARSE. Authoritative, via a real JS engine. ──
_node = None
for _cand in (r"C:\Program Files\nodejs\node.exe", "node"):
    try:
        subprocess.run([_cand, "--version"], capture_output=True, timeout=30,
                       check=True)
        _node = _cand
        break
    except Exception:
        continue

check(_node is not None,
      'node was not found -- the syntax check cannot run, and a regex '
      'brace-counter is not an acceptable substitute on this file')

if _node:
    _script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "_jscheck.js")
    with open(_script, "w", encoding="utf-8") as fh:
        fh.write(
            'const fs=require("fs");\n'
            'const src=fs.readFileSync(process.argv[2],"utf8");\n'
            # Only real <script> OPEN tags at line start; the file contains
            # prose mentioning "<script type=module>" inside a comment, which
            # a looser pattern picks up and then fails to parse as JS.
            'const re=/^<script([^>]*)>([\\s\\S]*?)<\\/script>/gm;\n'
            'let bad=0,n=0;\n'
            'for(const m of src.matchAll(re)){\n'
            '  const a=m[1]||"",b=m[2]||"";\n'
            '  if(/\\bsrc\\s*=/.test(a)) continue;\n'
            '  if(/type\\s*=\\s*["\\x27]importmap["\\x27]/.test(a)) continue;\n'
            '  n++;\n'
            '  try{ new Function(b); }\n'
            '  catch(e){ bad++; console.log("SYNTAX ERROR: "+e.message); }\n'
            '}\n'
            'console.log("parsed="+n+" errors="+bad);\n'
            'process.exit(bad?1:0);\n')
    try:
        _r = subprocess.run([_node, _script, HTML], capture_output=True,
                            text=True, timeout=180)
        _out = (_r.stdout or "") + (_r.stderr or "")
        check(_r.returncode == 0,
              'command_center_v4.html has a JavaScript syntax error -- a '
              'single one blacks out the ENTIRE page with no console '
              'output; node says: %s' % _out.strip()[:300])
        check("parsed=" in _out and "parsed=0" not in _out,
              'the syntax check parsed no script blocks, so it proved '
              'nothing -- a check that measures nothing is worse than none; '
              'got: %s' % _out.strip()[:200])
    except subprocess.TimeoutExpired:
        FAIL.append('the JS syntax check timed out')
    finally:
        try:
            os.remove(_script)
        except OSError:
            pass

# ── 3. The two sources must remain genuinely different populations ──
# If someone "fixes" the disagreement by pointing both at one source, the
# disclosures above become lies. Pin that they still read different fields.
check("agg.avg_win_rate" in _src,
      'the header must still read the aggregate bot-counter win rate')
check("ovFeeBotBreakdown" in _src or "ratioPctEl" in _src,
      'the expectancy panel win rate element must still exist')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  both win rates name their window, and the page parses')
