"""Public-facing bot aliases — the names subscribers actually see.

Single source of truth. This table and `_human_regime` below were previously
duplicated byte-for-byte in card_renderer.py and signal_broadcaster.py, both of
which carried "keep in sync" comments and no enforcement. When those drift, one
Telegram message says "Stalker" in the rendered image and something else in the
caption — for the same event.

Deliberately stdlib-only. card_renderer needs Pillow, and signal_broadcaster
imports it lazily so broadcasting degrades to text-only when Pillow is absent;
importing the shared table from card_renderer would have made Pillow a hard
dependency of all broadcasting.
"""

__all__ = ["BOT_DISPLAY_NAMES", "display_name", "REGIME_PROSE", "human_regime"]


BOT_DISPLAY_NAMES = {
    'confluence':    'Concord',
    'turtlesue':     'Stalker',
    'turtlebot':     'Stalker',
    'nexusbrain':    'Prism',
    'nexus_brain':   'Prism',
    'oracle':        'Atlas',
    'deepblue':      'Leviathan',
    'deep_blue':     'Leviathan',
    'gridzilla':     'Ironweb',
    'nexus':         'The Council',
    'aegis':         'Sovereign',
    'sentinel':      'Watcher',
    'trinity':       'Trident',
    'hivemind':      'Chorus',
    'hive_mind':     'Chorus',
    'phitex':        'Pulse',
    'rubberband':    'Slingshot',
    'contrarian':    'Heretic',
    'arbitrageur':   'Ghost',
    'chronos':       'Meridian',
    'inference':     'Inference',
}


def display_name(internal: str) -> str:
    """Map internal bot name to public display name."""
    return BOT_DISPLAY_NAMES.get(internal.lower().replace('-', '_'),
                                 internal.title())


# Internal regime labels -> prose. Same duplication story as the names above:
# the image card and its caption render the same regime from what used to be
# two separate copies.
REGIME_PROSE = {
    "TRENDING":       "Trending (bullish bias)",
    "TRENDING_UP":    "Strong uptrend",
    "TRENDING_DOWN":  "Downtrend",
    "BULL":           "Bullish",
    "BEAR":           "Bearish",
    "RANGING":        "Range-bound (sideways)",
    "NORMAL":         "Neutral",
    "DEFENSIVE":      "Defensive (risk-off)",
    "CAUTIOUS":       "Cautious",
    "EQUILIBRIUM":    "Balanced",
    "MIXED":          "Mixed signals",
    "EXTREME_FEAR":   "Extreme fear",
    "EXTREME_GREED":  "Extreme greed",
    "HIGH_ACTIVITY":  "High activity",
}


def human_regime(regime) -> str:
    """Translate an internal regime label to human-readable prose."""
    return REGIME_PROSE.get(
        (regime or "").upper().replace(" ", "_"),
        (regime or "Unknown").replace("_", " ").title())
