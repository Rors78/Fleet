"""The one definition of a synthetic probe pair.

WHY THIS MODULE EXISTS. This predicate lived in four places — command_center,
expectancy, signal_broadcaster and weekly_analysis — and the copies had
already drifted: three upper-cased the pair before testing it and
weekly_analysis did not, so a lowercase probe row was filtered everywhere
except the weekly report. Four copies, two behaviours, and nothing to make
them disagree loudly.

That drift is not hypothetical damage. It is the same shape that produced the
incidents this predicate exists to prevent:

  - 2026-08-07: 50 of 73 rows on /api/trades were probes contributing +$617 of
    fabricated P/L, flipping the raw sum positive while real pairs summed
    -$167. The SIGN of the fleet's trade feed depended on test data.
  - 2026-08-26: expectancy.py — the module feeding the dashboard — had no
    probe filter at all, while three siblings did. TurtleSue's 92 "wins" were
    ZZPROBE rows carrying an identical hardcoded pnl=12.34, and its real
    trading P/L was $0.00.

DELIBERATELY A LEAF. This module imports nothing from the fleet, so every
consumer can import it without a cycle (command_center imports expectancy,
which is why expectancy could not simply import back). Keep it that way: the
moment this file needs a fleet import, the copies come back.

IF PROBE ROWS EVER GAIN A `synthetic: true` FIELD at write time, key on that
instead and leave this name-based test as the fallback for rows already in the
durable store — they outlive the process and cannot be re-tagged.
"""

__all__ = ["is_probe_pair", "PROBE_PREFIXES"]

# Marker prefixes no real market carries. ZZ and NFNOK are the bare forms;
# ZZPROBE is the current generator's. Order does not matter — startswith takes
# the tuple.
PROBE_PREFIXES = ("ZZPROBE", "ZZ", "NFNOK")


def is_probe_pair(pair) -> bool:
    """True if `pair` names a synthetic probe rather than a real market.

    Case-insensitive, and None-safe: a missing pair is not a probe, it is a
    row that failed to say what it was, and the caller decides what that
    means. Never raises — this runs inside request handlers and log readers
    where an exception would take down more than it protects.

    >>> is_probe_pair("ZZPROBE032265B/USD")
    True
    >>> is_probe_pair("nf138587ok/usd")     # the case weekly_analysis missed
    True
    >>> is_probe_pair("BTC/USD")
    False
    >>> is_probe_pair(None)
    False
    """
    p = str(pair or "").upper()
    if p.startswith(PROBE_PREFIXES):
        return True
    # NF<digits>..., e.g. NF138587OK/USD. Written as a prefix test rather than
    # a regex on purpose: this module is imported into request handlers, and
    # the `re`-in-a-handler NameError class has bitten this codebase before.
    # NFT/USD and NF/USD are NOT probes — the digit is what distinguishes a
    # generated marker from a real ticker.
    return p.startswith("NF") and len(p) > 2 and p[2].isdigit()
