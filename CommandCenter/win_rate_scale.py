"""The one rule for reading a win rate whose scale is not declared.

WHY THIS EXISTS. Bots publish win rates on two different scales — TurtleSue
and Gridzilla pass raw 0-1 fractions through, NexusBrain scales to 0-100 —
and nothing in the payload says which. Three modules each guessed
independently:

    command_center.py:2038     wr if wr > 1 else wr * 100
    ultron.py:106              wr if wr > 1 else wr * 100
    signal_broadcaster.py:1222 v / 100.0 if v > 1.0 else v

Two convert up, one converts down, all three pivot on the same `> 1`
threshold, and none of them agreed on what to do at the boundary.

THE SENTINEL COLLISION. `1.0` is a legitimate reading on BOTH scales: 100% as
a fraction, or 1% as a percentage. So is any value in (0, 1.5]. The `> 1`
heuristic silently resolves that ambiguity in favour of the flattering
reading: **a bot with a genuine 1% win rate, publishing on the 0-100 scale,
renders as 100%.** That is the same fabricated-success shape as the Gridzilla
incident (a 100% win rate that no market outcome could lower), arrived at from
a different direction.

No live bot sits in the ambiguous band today (checked 2026-08-26: turtlesue
25.0, gridzilla 100.0, confluence 29.6), which is exactly why this is worth
fixing now — the drift is real but has not yet been triggered by live data.

THE REAL FIX, for whoever gets there: make bots declare their scale, or have
every normalizer emit a single agreed scale at the source. Until then this
module at least makes the guess ONE guess, documented, with the ambiguity
reported rather than hidden.
"""

__all__ = ["to_percent", "to_fraction", "AMBIGUOUS_MAX"]

# Above this, a value cannot be a fraction (no win rate exceeds 100%), so the
# 0-100 reading is certain. At or below it, both readings are legitimate and
# the caller is guessing.
AMBIGUOUS_MAX = 1.0


def _coerce(value):
    """Return a finite float, or None. Never raises."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):   # NaN / inf
        return None
    return v


def to_percent(value, ambiguous=None):
    """Read an undeclared-scale win rate as a percentage (0-100).

    Returns None when the value is missing or unreadable — absence must stay
    absent, never become 0.0, which is a legitimate win rate.

    `ambiguous` is an optional one-argument callback invoked with the raw
    value when both scale readings are legitimate (0 < v <= 1.0). Use it to
    log or flag the guess; the return value is ignored.

    >>> to_percent(0.65)
    65.0
    >>> to_percent(65.0)
    65.0
    >>> to_percent(None) is None
    True
    >>> to_percent(0.0)
    0.0
    """
    v = _coerce(value)
    if v is None:
        return None
    if v < 0:
        return None
    if 0 < v <= AMBIGUOUS_MAX:
        if ambiguous is not None:
            try:
                ambiguous(v)
            except Exception:
                pass
        return v * 100.0
    return v


def to_fraction(value, ambiguous=None):
    """Read an undeclared-scale win rate as a fraction (0-1).

    The exact inverse of to_percent, sharing its threshold so the two can
    never disagree about where the boundary sits — which is how the three
    original copies drifted.

    >>> to_fraction(65.0)
    0.65
    >>> to_fraction(0.65)
    0.65
    >>> to_fraction(None) is None
    True
    """
    pct = to_percent(value, ambiguous=ambiguous)
    if pct is None:
        return None
    return pct / 100.0
