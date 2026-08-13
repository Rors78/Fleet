"""The daily summary must count trades that were actually taken.

The daily accumulator has exactly one producer: _detect_events observing a
position present in one 60s poll and absent in the next. That is an inference
from snapshots, not a record of trades, and it misses two classes entirely:

  - anything opened and closed inside one poll interval. All 15 of
    Gridzilla's fills carry duration 0, so its positions never exist across
    two polls and the diff can never see them.
  - anything a bot closes via its own TRADE_CLOSE emit while the snapshot is
    stale or the bot is briefly unreachable.

Measured 2026-08-13: logs/daily for Aug 7-12 reported "total_trades": 0 and
"daily_pnl": 0 on every single day, while the canonical collector held 34
rows on Aug 7, 10 on Aug 8 and 2 on Aug 12. The weekly scorecard reads those
files, so it was about to tell subscribers the fleet stayed on the sidelines
for a week in which it traded. A comfortable zero over a real gap.

Command Center already solved this once: _collect_closed_trades() reads the
durable logs from BOTH producers and dedups in three stages. The daily
summary is the one consumer that never used it.

Two things this test pins that the first draft of the fix got wrong:
  1. _collect_closed_trades() returns EVERYTHING. The probe filter lives at
     the /api/trades handler and only TAGS rows synthetic. Sourcing from the
     collector without filtering pulled in 29 ZZPROBE rows carrying an
     identical fabricated $12.34 each -- which turned Aug 7 from its real
     -$201.91 into a reported +$155.95 and a 97.1% win rate. Sign-inverting
     contamination that looks like an excellent day.
  2. Unreadable must not read as empty. None means "could not read, fall
     back to the accumulator"; [] means "read it, that day is genuinely
     quiet". Collapsing them would score a day zero on any read failure --
     the exact defect being fixed.
"""
import os
import sys
import threading

sys.path.insert(0, 'D:/CommandCenter')

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


import fleet_logger as fl

_src = open('D:/CommandCenter/fleet_logger.py', encoding='utf-8',
            errors='replace').read()

# ── 1. The summary must not be sourced from the accumulator alone ──
check("_canonical_trades_for" in _src,
      'the daily summary must read the canonical deduped trade log, not only '
      'the poll-diff accumulator -- the accumulator structurally cannot see '
      'a trade that opens and closes inside one 60s poll')
check("_collect_closed_trades" in _src,
      'it must reuse command_center._collect_closed_trades rather than '
      'reimplementing dedup -- there is one canonical three-stage dedup and '
      'a second copy would drift from it')

# The import must stay INSIDE the function: command_center imports
# fleet_logger at module scope, so a top-level import back is circular.
_top = _src.split("class FleetLogger")[0]
check("from command_center import" not in _top,
      'the command_center import must be deferred inside the method -- '
      'command_center imports fleet_logger at module scope, so a top-level '
      'import back would be circular and break fleet startup')

# ── 2. Probe pairs must not reach the summary ──
check("_is_probe_pair" in _src,
      'synthetic probe rows must be filtered out of the daily summary -- '
      'the collector returns them and the /api/trades filter only tags them')

o = fl.FleetLogger.__new__(fl.FleetLogger)


def _canon_call(day):
    """Call the helper without dying if it does not exist.

    A test that raises AttributeError against the unfixed code prints no
    failures at all and reads as a clean run -- which is how a red proof
    turns into a false green. Return a sentinel and let the assertions
    report, rather than letting the defect kill the reporter.
    """
    fn = getattr(fl.FleetLogger, "_canonical_trades_for", None)
    if fn is None:
        return "MISSING"
    try:
        return fn(o, day)
    except Exception as exc:
        return "RAISED: %r" % (exc,)


def _merge(accum, canon):
    fn = getattr(fl.FleetLogger, "_merge_trades", None)
    if fn is None:
        return "MISSING"
    try:
        return fn(accum, canon)
    except Exception as exc:
        return "RAISED: %r" % (exc,)


def _n_of(v):
    return len(v) if isinstance(v, list) else -1


_rows = _canon_call("2026-08-07")
check(isinstance(_rows, list),
      'the canonical trade helper must exist and return a list -- the daily '
      'summary cannot be sourced from the canonical log without it; got %r'
      % (_rows,))
if not isinstance(_rows, list):
    _rows = []
if _rows:
    _probes = [r for r in _rows if str(r.get("pair", "")).upper()
               .startswith(("ZZPROBE", "ZZ", "NFNOK"))]
    check(not _probes,
          '%d synthetic probe rows survived into the daily summary for '
          '2026-08-07 -- each carries a fabricated $12.34 and they flip the '
          "day's sign" % len(_probes))
    check(len(_rows) == 5,
          '2026-08-07 must resolve to its 5 real trades (34 collector rows '
          'minus 29 probes); got %d' % len(_rows))

# ── 3. Absent, empty and unreadable must stay three different answers ──
_empty = _canon_call("2019-01-01")
check(_empty == [],
      'a day with no trades must return [] -- "read it, nothing there" is a '
      'measurement; got %r' % (_empty,))

# An unreadable canonical path must return None so the caller falls back to
# the accumulator, NOT [] which would silently score the day zero.
import command_center as _cc
_orig = _cc._collect_closed_trades
try:
    _cc._collect_closed_trades = lambda: (_ for _ in ()).throw(
        RuntimeError("log dir unreadable"))
    _broken = _canon_call("2026-08-07")
    check(_broken is None,
          'an unreadable canonical log must return None (fall back to the '
          'accumulator), never [] -- [] means "measured, no trades" and '
          'would score a real trading day as flat; got %r' % (_broken,))
finally:
    _cc._collect_closed_trades = _orig

# ── 4. The merge must dedup without dropping distinct trades ──
_canon = _canon_call("2026-08-07")
_canon = _canon if isinstance(_canon, list) else []
_n = len(_canon)

check(_n_of(_merge([], _canon)) == _n,
      'an empty accumulator plus %d canonical rows must yield %d' % (_n, _n))

if _canon:
    _c0 = _canon[0]
    # Same trade, seen by both producers. The accumulator's copy has no
    # reservation_id (the snapshot-diff path never sets one), so this must
    # dedup on the bot+pair+600s heuristic with a slashless pair.
    _dup = {"ts": _c0["ts"] + 120, "bot": _c0["bot"],
            "pair": str(_c0["pair"] or "").replace("/", ""), "pnl": 0.0}
    check(_n_of(_merge([_dup], _canon)) == _n,
          'a snapshot-diff row for the same bot+pair within 600s is the same '
          'trade and must not be double counted -- the canonical row carries '
          'the realized pnl, the accumulator row a stale unrealized one')

    # 20 minutes apart is a genuinely different trade and must survive. The
    # 90s same-pair open cooldown makes faster turnover impossible, so the
    # window cannot merge two real trades.
    _far = dict(_dup, ts=_c0["ts"] + 1200)
    check(_n_of(_merge([_far], _canon)) == _n + 1,
          'the dedup window must not swallow a genuinely separate trade on '
          'the same pair 20 minutes later')

    # Dedup by reservation_id, the strongest key.
    _rid_row = next((t for t in _canon if t.get("reservation_id")), None)
    if _rid_row:
        check(_n_of(_merge([dict(_rid_row)], _canon)) == _n,
              'a row sharing a reservation_id with a canonical row is the '
              'same trade')

    # A row the canonical log has never heard of must be kept, not dropped.
    _uniq = {"ts": 1, "bot": "ghostbot", "pair": "ZQQ/USD", "pnl": 1.23}
    check(_n_of(_merge([_uniq], _canon)) == _n + 1,
          'an accumulator row with no canonical counterpart must survive the '
          'merge -- the canonical log is a supplement, not a replacement')

# ── 5. End to end: a real day must stop reporting zero ──
import json
_out = os.environ.get("TMPDIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_tmp_daily")
os.makedirs(_out, exist_ok=True)
_saved_dir = fl.DAILY_DIR
try:
    fl.DAILY_DIR = _out
    _o = fl.FleetLogger.__new__(fl.FleetLogger)
    _o._daily_lock = threading.Lock()
    _o._starting_equity = None
    _o._get_state = lambda: {}
    _o._get_portfolio = lambda: {"total": 210.62}
    _o._daily = fl.FleetLogger._empty_daily(_o)
    # Exactly the rollover call, with the empty accumulator Aug 7 really had.
    fl.FleetLogger._write_daily_summary(
        _o, "2026-08-07", fl.FleetLogger._empty_daily(_o), [])
    _f = json.load(open(os.path.join(_out, "2026-08-07.json"),
                        encoding="utf-8"))["fleet"]
    check(_f.get("total_trades") == 5,
          'the regenerated 2026-08-07 summary must report its 5 real trades; '
          'got %r' % _f.get("total_trades"))
    check(isinstance(_f.get("daily_pnl"), (int, float))
          and _f["daily_pnl"] < 0,
          "2026-08-07 was a losing day (-$201.91 across 5 real trades). A "
          "positive figure here means probe rows leaked back in and flipped "
          "the sign; got %r" % _f.get("daily_pnl"))
    # Flats are capital movements, neither win nor loss -- the win rate must
    # be computed over DECIDED trades only.
    _dec = _f.get("decided")
    if isinstance(_dec, int) and _dec > 0:
        check(_f.get("wins", 0) + _f.get("losses", 0) == _dec,
              'wins + losses must equal decided; flats and unpriced trades '
              'must not be silently booked as losses')
finally:
    fl.DAILY_DIR = _saved_dir

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  daily summary reads the canonical log, probes filtered, '
      'unreadable != empty')
