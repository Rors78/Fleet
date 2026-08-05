"""Position sizing with per-bot unit semantics baked in.

WHY THIS EXISTS
---------------
Position size fields do NOT mean the same thing across the fleet:

    turtlesue    total_size  -> COINS  (Turtle units; USD = sum(size * entry))
    confluence   size_usd    -> USD
    nexusbrain   size_usd    -> USD
    gridzilla    size_usd    -> USD
    rubberband   size_usd    -> USD
    arbitrageur  size_usd    -> USD

On 2026-08-05 a fleet audit read TurtleSue's `total_size: 446.90` as dollars.
It is 446.90 UNI. Against $1,878.21 reserved that fabricates a $1,431 "leak"
with a plausible root cause, and the recommended remediation — release the
reservation — would have stranded a live funded pyramid unit with no backing
reservation. Strictly worse than the imagined problem.

That was the third coins-vs-dollars incident on this project. Documentation
saying "field semantics differ per bot" is a rule you must remember at the
moment you are least likely to. This module refuses instead.

RULE: never read a size field directly. Route every position through
`notional_usd()`. An unknown bot raises rather than guessing — a wrong number
here moves capital.
"""

from typing import Mapping

__all__ = ["notional_usd", "position_notionals", "UnknownBotError",
           "AmbiguousPositionError", "COIN_DENOMINATED", "USD_DENOMINATED"]


class UnknownBotError(ValueError):
    """Bot has no registered size semantics. Register it; do not guess."""


class AmbiguousPositionError(ValueError):
    """Position lacks the fields its bot's semantics require."""


# Bots whose size field is a COIN quantity — USD needs a price multiply.
COIN_DENOMINATED = {
    "turtlesue": {"size": "total_size", "price": "avg_entry",
                  "units": "units", "unit_size": "size",
                  "unit_price": "entry_price"},
}

# Bots whose size field is already USD.
USD_DENOMINATED = {
    "confluence":  "size_usd",
    "nexusbrain":  "size_usd",
    "gridzilla":   "size_usd",
    "rubberband":  "size_usd",
    "arbitrageur": "size_usd",
}


def _num(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AmbiguousPositionError(f"non-numeric size/price: {value!r}")
    return float(value)


def notional_usd(bot_id: str, position: Mapping) -> float:
    """USD notional of one position, honoring that bot's unit semantics.

    Raises UnknownBotError for an unregistered bot and
    AmbiguousPositionError when the required fields are missing — never
    returns a guess, because the caller usually acts on this number.
    """
    if not isinstance(position, Mapping):
        raise AmbiguousPositionError(f"position must be a mapping, got "
                                     f"{type(position).__name__}")
    bot = (bot_id or "").strip().lower()

    if bot in USD_DENOMINATED:
        field = USD_DENOMINATED[bot]
        if field not in position:
            raise AmbiguousPositionError(
                f"{bot}: expected USD field {field!r}; have "
                f"{sorted(position)}")
        return _num(position[field])

    if bot in COIN_DENOMINATED:
        spec = COIN_DENOMINATED[bot]
        # Prefer per-unit rows: each unit has its own fill price, so summing
        # them is exact where size * avg_entry is an approximation.
        units = position.get(spec["units"])
        if isinstance(units, (list, tuple)) and units:
            total = 0.0
            for u in units:
                if not isinstance(u, Mapping):
                    raise AmbiguousPositionError(f"{bot}: bad unit row {u!r}")
                if spec["unit_size"] not in u or spec["unit_price"] not in u:
                    raise AmbiguousPositionError(
                        f"{bot}: unit row missing "
                        f"{spec['unit_size']!r}/{spec['unit_price']!r}")
                total += _num(u[spec["unit_size"]]) * _num(u[spec["unit_price"]])
            return total
        if spec["size"] in position and spec["price"] in position:
            return _num(position[spec["size"]]) * _num(position[spec["price"]])
        raise AmbiguousPositionError(
            f"{bot}: coin-denominated; need {spec['units']!r} rows or "
            f"{spec['size']!r}+{spec['price']!r}; have {sorted(position)}")

    raise UnknownBotError(
        f"no size semantics registered for {bot_id!r}. Add it to "
        f"COIN_DENOMINATED or USD_DENOMINATED in portfolio_math.py — do not "
        f"assume USD."
    )


def position_notionals(bot_id: str, positions) -> dict:
    """Map identifier -> USD notional for a bot's positions block.

    Accepts either shape the fleet uses: a dict keyed by pair, or a list of
    position dicts (keyed by their 'pair').
    """
    out = {}
    if isinstance(positions, Mapping):
        items = positions.items()
    elif isinstance(positions, (list, tuple)):
        items = ((p.get("pair", i) if isinstance(p, Mapping) else i, p)
                 for i, p in enumerate(positions))
    else:
        raise AmbiguousPositionError(
            f"positions must be dict or list, got {type(positions).__name__}")
    for key, pos in items:
        out[key] = notional_usd(bot_id, pos)
    return out
