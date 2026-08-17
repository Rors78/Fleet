"""An unreadable pool renders as the sum of five bots' demo balances.

/api/master's `aggregate.total_equity` is the sum of each bot's INTERNAL
account equity -- five bots report a $10,000 paper balance, so it currently
totals $49,631.58. The fleet's actual capital pool is $209.88. These are
different quantities by a factor of 236; total_equity is not a pool figure
and never was.

Three display sites used it as the fallback when `state.portfolio` is absent:

    3478  var pool = port.total!=null ? port.total : (agg.total_equity||0)
    4735  poolEl.textContent = fmt$(agg.total_equity)          <- header POOL
    4970  el2.textContent = p ? fmt$(p.total) : (agg?fmt$(agg.total_equity):"--")

The server emits `"portfolio": _active_portfolio().state() if
_active_portfolio() else None` (command_center.py:3740), so a null portfolio
is a real, reachable path -- not dead code.

Proven against the shipped renderHeader under node, real function body, DOM
stubbed:

    portfolio PRESENT -> hPool=$209.88     hFree=$190.67
    portfolio NULL    -> hPool=$49631.58   hFree=--

That is the absent-vs-empty defect exactly. FREE degrades honestly to "--".
POOL fabricates a confident number 236x too large, in the same style as a
real reading, with nothing to indicate the read failed. Two adjacent chips
disagree about whether data exists and the confident-looking one is lying.

The file already carries a comment at 3483 recording that this same class of
bug was fixed on the sibling `pnl` value in this very function -- "pool at
line ~3462, wr at ~3464 already got the !=null treatment; pnl was the one
left behind." The pool's total_equity fallback survived that pass because it
reads as a sensible fallback rather than an obvious `||0`.

A pool that cannot be read must render as unavailable, never as a number
derived from an unrelated quantity.
"""
import re
import sys

SRC = 'D:/CommandCenter/command_center_v4.html'
FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


_src = open(SRC, encoding='utf-8', errors='replace').read()

# ── 1. No display site may substitute total_equity for a pool figure ──
# total_equity is a sum of bot-internal demo balances; using it where a pool
# is displayed is a category error, not a fallback.
_pool_ctx = re.compile(
    r'(?:poolEl|statPool|el2|var\s+pool)\s*[.=][^;\n]*total_equity')
_hits = [m.group(0) for m in _pool_ctx.finditer(_src)]
check(not _hits,
      'a pool display still falls back to aggregate.total_equity, which is '
      'the sum of bot-internal demo balances ($49,631.58) and not the pool '
      '($209.88) -- a 236x category error rendered as a confident number: %r'
      % (_hits[:3],))

# ── 2. The header POOL chip must degrade like its FREE sibling ──
# Both live in renderHeader's else-branch. FREE already renders "--".
_hdr = re.search(r'function renderHeader\(agg\)\s*\{', _src)
check(_hdr is not None, 'renderHeader(agg) not found -- test needs updating')
if _hdr:
    _start = _hdr.start()
    _depth, _end = 0, None
    for _i in range(_start, len(_src)):
        if _src[_i] == '{':
            _depth += 1
        elif _src[_i] == '}':
            _depth -= 1
            if _depth == 0:
                _end = _i + 1
                break
    _body = _src[_start:_end or len(_src)]
    check('total_equity' not in _body,
          'renderHeader still reads total_equity. Its hFree sibling renders '
          '"--" when the portfolio is absent; hPool must not fabricate a '
          'number where FREE admits it has none')

# ── 3. Absence must be visually distinct from a reading ──
# Whatever the unreadable branch does, it must produce the unavailable marker
# rather than a formatted dollar amount.
if _hdr and _end:
    _else = re.search(r'\}\s*else\s*\{(.*?)\}', _body, re.S)
    if _else:
        _eb = _else.group(1)
        check('"--"' in _eb or "'--'" in _eb,
              'the unreadable-portfolio branch of renderHeader must render '
              'the unavailable marker; it currently formats a dollar value, '
              'which is indistinguishable from a real pool reading')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  an unreadable pool renders as unavailable, not as demo equity')
