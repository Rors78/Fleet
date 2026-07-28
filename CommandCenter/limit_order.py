"""
Shared marketable-limit pricing for fleet bots.

LIMIT ORDERS ONLY is fleet policy, no exceptions — market orders are refused
at the Kraken client. This module is the single place that decides what price
a limit order carries, so every bot prices identically.

Why marketable rather than passive: a limit order resting exactly at the last
trade may never fill, and a strategy that decided to act now should act now. So
orders cross the spread by a small fixed amount — they fill like a market order
under normal conditions, but if the book has gapped further than the cross, the
order rests unfilled instead of eating the gap. That is the whole point: a
market order has no worst case, a marketable limit does.

Usage:
    from limit_order import limit_price
    ok, txid = client.buy(pair, qty, price=limit_price(ref_price, "BUY"))
"""

# Fraction of price to cross the spread by. 15 bps.
# Kept in step with fleet_config.LIMIT_CROSS_PCT; this module falls back to the
# local value when fleet_config is unreachable so a bot never silently loses
# its pricing rule (and therefore never falls back to an unpriced order).
DEFAULT_CROSS_PCT = 0.0015

try:
    from fleet_config import LIMIT_CROSS_PCT as _CROSS
except Exception:
    _CROSS = DEFAULT_CROSS_PCT


def limit_price(reference_price, direction, cross_pct=None):
    """Marketable limit price for `direction` around `reference_price`.

    BUY / LONG   -> slightly ABOVE reference (willing to pay up to this)
    SELL / SHORT -> slightly BELOW reference (willing to accept down to this)

    Returns 0.0 for a missing or non-positive reference. Callers pass that
    straight through to the Kraken client, which rejects it — an unpriced order
    must fail loudly, never degrade into a market order.
    """
    try:
        ref = float(reference_price)
    except (TypeError, ValueError):
        return 0.0
    if ref <= 0:
        return 0.0

    pct = _CROSS if cross_pct is None else float(cross_pct)
    d = (direction or "").upper()
    if d in ("LONG", "BUY"):
        return ref * (1.0 + pct)
    return ref * (1.0 - pct)
