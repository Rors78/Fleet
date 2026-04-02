"""
Fleet Standards — Shared constants and normalization functions.
Every bot should import and use these for consistency.
"""

# Canonical regime labels — every bot MUST normalize to one of these
REGIMES = ["BULL", "BEAR", "RANGING", "VOLATILE", "TRANSITIONING"]

REGIME_MAP = {
    # HiveMind / TrekBot HMM (lowercase)
    "bull": "BULL", "bear": "BEAR", "range": "RANGING", "chop": "VOLATILE",
    # Oracle
    "transitioning": "TRANSITIONING", "trending_up": "BULL", "trending_down": "BEAR",
    # Trinity
    "TRENDING": "BULL",
    # PHITEX
    "CRITICAL": "VOLATILE", "PRE_CRITICAL": "TRANSITIONING", "EQUILIBRIUM": "RANGING",
    # AEGIS
    "DEPLOY": "BULL", "CAUTIOUS": "TRANSITIONING", "DEFENSIVE": "RANGING",
    # NexusBrain
    "TREND_UP": "BULL", "TREND_DOWN": "BEAR",
    # Common / canonical passthrough
    "BULL": "BULL", "BEAR": "BEAR", "RANGING": "RANGING", "VOLATILE": "VOLATILE",
    "TRANSITIONING": "TRANSITIONING", "NORMAL": "RANGING", "NEUTRAL": "RANGING",
    "normal": "RANGING", "neutral": "RANGING",
}


def normalize_regime(label):
    """Normalize any regime label to one of the 5 canonical labels."""
    if not label:
        return "RANGING"
    return REGIME_MAP.get(label, REGIME_MAP.get(label.upper(), "RANGING"))


def normalize_pair(pair):
    """Normalize any pair format to 'BTC/USD' style."""
    if not pair:
        return pair
    # Already clean
    if "/" in pair and pair.endswith("USD") and not pair.endswith("USDT"):
        return pair
    # Kraken internal format
    clean = pair.replace("XXBT", "BTC").replace("XETH", "ETH").replace("XXRP", "XRP")
    clean = clean.replace("XLTC", "LTC").replace("XXLM", "XLM").replace("XXMR", "XMR")
    clean = clean.replace("XDGZ", "DOGE").replace("XDG", "DOGE")
    clean = clean.replace("ZUSD", "").replace("USD", "")
    if clean and not "/" in pair:
        return f"{clean}/USD"
    return pair
