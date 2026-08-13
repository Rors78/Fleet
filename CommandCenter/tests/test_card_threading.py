"""A close 13 hours after its open must carry the entry with it.

Subscriber complaint, and it is correct: "if ada opens and then ada closes
13 hrs later then the subscriber will have to scroll through alot of shit."
A POSITION CLOSED card arriving half a day and dozens of cards after its
POSITION OPENED had nothing tying the two together.

Two fixes, both pinned here:

1. THREADING. The close is sent as a Telegram reply to its own open, so the
   entry is quoted inline above the exit. Keyed on the pair parsed from the
   card text rather than threading a new argument through five call sites —
   the pair is already on every card, and a failed parse degrades to "no
   reply", never to a WRONG reply pointing at another pair's position.

2. THE CLOSE CARD STANDS ALONE ANYWAY. Threading depends on the open still
   existing in Telegram; the card itself now carries entry, exit, return %
   and holding time, so a subscriber who never sees the parent still gets
   the whole trade.

Also fixed in the same pass, from the same review:
  - POSITION SIZE REMOVED. "$10,058.82" is this fleet's sizing on a $1M
    paper pool. A subscriber cannot use it and should not mirror it.
  - PRICE PRECISION follows the pair. "Entry 8.76393" on an $8 asset is
    five decimals of false precision; a sub-cent token genuinely needs them.
  - FLAT and UNMEASURED closes are no longer labelled LOSS — the same
    defect the fleet's expectancy classifier already had.
"""
import re
import sys
import time

sys.path.insert(0, 'D:/CommandCenter')

import signal_broadcaster as sb

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def strip(s):
    return re.sub(r'<[^>]+>', '', s or '')


fmt = sb.CardFormatter.__new__(sb.CardFormatter)


def ch():
    c = sb.ChannelOps.__new__(sb.ChannelOps)
    c._open_msgs = {}
    return c


def call(obj, name, *a):
    """Call a threading helper, reporting absence instead of dying on it.

    Without the fix these methods do not exist, and a bare attribute access
    raises AttributeError before any assertion prints — the red proof then
    reads as 'no failures'. Seventh time this exact flaw has bitten a test
    in this session; see the safe() wrapper in test_aegis_seed.
    """
    fn = getattr(obj, name, None)
    if fn is None:
        return 'MISSING %s' % name
    try:
        return fn(*a)
    except Exception as e:
        return 'RAISED %s' % type(e).__name__


# ── 1. A close threads to its own open ──
c = ch()
open_card = fmt._free_trade("TRADE_OPEN", {
    "pair": "ADA/USD", "direction": "LONG", "entry_price": 0.1832,
    "stop_loss": 0.1750, "source": "ironweb"})
close_card = fmt._free_trade("TRADE_CLOSE", {
    "pair": "ADA/USD", "direction": "LONG", "entry_price": 0.1832,
    "exit_price": 0.1904, "pnl": 41.27, "duration_s": 13 * 3600,
    "source": "ironweb"})
_pp = call(c, "_card_pair", open_card)
check(_pp == "ADA/USD",
      'the pair must be parseable from the rendered card, got %r' % (_pp,))
c._last_message_id = 4242
call(c, "_remember_open", "TRADE_OPEN", open_card)
check(call(c, "_thread_parent", "TRADE_CLOSE", close_card) == 4242,
      'the close must reply to its own open — this is the whole point')

# ── 2. It must not thread twice, or to the wrong pair ──
check(call(c, "_thread_parent", "TRADE_CLOSE", close_card) is None,
      'a consumed open must not thread a second close — two closes on one '
      'pair would otherwise both point at the same entry')
c2 = ch()
c2._last_message_id = 7
call(c2, "_remember_open", "TRADE_OPEN", open_card)
_link_close = fmt._free_trade("TRADE_CLOSE", {
    "pair": "LINK/USD", "direction": "LONG", "pnl": 5.0, "source": "ironweb"})
check(call(c2, "_thread_parent", "TRADE_CLOSE", _link_close) is None,
      'a close must NEVER thread to a different pair\'s open — a wrong '
      'parent is worse than no parent')

# ── 3. A stale open expires rather than threading to ancient history ──
c3 = ch()
c3._open_msgs["ADA/USD"] = (99, time.time() - 8 * 24 * 3600)
check(call(c3, "_thread_parent", "TRADE_CLOSE", close_card) is None,
      'an open older than the TTL must not be threaded to')

# ── 4. Opens never thread ──
check(call(ch(), "_thread_parent", "TRADE_OPEN", open_card) is None,
      'a POSITION OPENED card must never be sent as a reply')

# ── 5. The close card stands alone: entry, exit, return, duration ──
_txt = strip(close_card)
for _need in ("Entry", "Exit", "Return", "Held"):
    check(_need in _txt,
          'the close card must carry %r so it is readable WITHOUT the '
          'threaded parent; got:\n%s' % (_need, _txt))
check("+3.93%" in _txt,
      'return %% must be computed from entry/exit — it is the figure a '
      'subscriber can apply to their own sizing; got:\n%s' % _txt)
check("13.0h" in _txt,
      'holding time must be shown; got:\n%s' % _txt)

# ── 6. Position size must NOT appear on a subscriber card ──
_open_txt = strip(fmt._free_trade("TRADE_OPEN", {
    "pair": "LINK/USD", "direction": "LONG", "entry_price": 8.76393,
    "size_usd": 10058.82, "stop_loss": 8.31, "source": "ironweb"}))
check("10,058" not in _open_txt and "10058" not in _open_txt,
      'position size must not be published — it is this fleet\'s sizing on '
      'its own pool, not something a subscriber can use; got:\n%s'
      % _open_txt)
check("Size" not in _open_txt,
      'the Size row must be gone from subscriber cards; got:\n%s' % _open_txt)

# ── 7. Price precision follows the pair ──
check("8.7639" in _open_txt and "8.76393" not in _open_txt,
      'an $8 asset must not show 5 decimals of false precision; got:\n%s'
      % _open_txt)
_sub = strip(fmt._free_trade("TRADE_OPEN", {
    "pair": "SHIB/USD", "direction": "LONG", "entry_price": 0.00001234,
    "source": "x"}))
check("0.00001234" in _sub,
      'a sub-cent asset genuinely needs its decimals — precision must '
      'follow the pair, not a fixed format; got:\n%s' % _sub)

# ── 8. Flat and unmeasured closes are not losses ──
_flat = strip(fmt._free_trade("TRADE_CLOSE", {
    "pair": "X/USD", "direction": "LONG", "pnl": 0.0, "source": "x"}))
check("LOSS" not in _flat and "FLAT" in _flat,
      'a $0.00 close is FLAT, not a LOSS; got:\n%s' % _flat)
_unk = strip(fmt._free_trade("TRADE_CLOSE", {
    "pair": "X/USD", "direction": "LONG", "source": "x"}))
check("LOSS" not in _unk and "UNMEASURED" in _unk,
      'a close with no P/L is UNMEASURED, not a LOSS; got:\n%s' % _unk)

# ── 9. Synthetic test probes must never reach a subscriber ──
# Observed live 2026-08-13: two "POSITION CLOSED ZZPROBE645420B/USD — WIN
# +$12.34" cards were delivered to 8 real subscribers. Command Center has
# filtered probe pairs from /api/trades since 2026-08-07 (50 of 73 rows
# were probes worth +$617 of fabricated P/L, flipping the sign of the
# fleet's trade feed) — the SUBSCRIBER path never got the same filter, and
# that is the one that leaves the building.
_cfg = {"min_conviction_threshold": 0.8, "free_delay_hours": 4,
        "routing_overrides": {}}
_R = sb.TierRouter

for _p in ("ZZPROBE645420B/USD", "ZZTEST/USD", "NFNOK1/USD", "NF138587OK/USD"):
    _ev = {"type": "TRADE_CLOSE",
           "data": {"pair": _p, "pnl": 12.34, "direction": "LONG",
                    "entry_price": 4.1, "exit_price": 4.21}}
    check(_R.route(_ev, _cfg) is None,
          'probe pair %r must be suppressed before routing — it reached 8 '
          'live subscribers as a fabricated WIN' % _p)

# ...and a real pair with the SAME shape must still route.
_real_ev = {"type": "TRADE_CLOSE",
            "data": {"pair": "ADA/USD", "pnl": 41.27, "direction": "LONG",
                     "entry_price": 0.1832, "exit_price": 0.1904}}
check(_R.route(_real_ev, _cfg) is not None,
      'a real priced close must still route — the probe filter must not '
      'suppress genuine trades')

# The predicate must not over-match real tickers that merely start with N/NF.
for _ok in ("NEAR/USD", "NFT/USD", "BTC/USD", "ADA/USD"):
    check(_R._is_probe_pair(_ok) is False,
          '%r is a real pair and must NOT be treated as a probe' % _ok)

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  closes thread to their opens and read standalone; no size leak')
