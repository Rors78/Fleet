"""
kraken_ohlc.py — Canonical OHLC fetch for the trading fleet.
=============================================================
Single source of truth for fetching candlestick data.

Strategy:
  1. Try Command Center proxy: GET {cc_url}/api/market/ohlc?pair=…&interval=…&limit=…
  2. On any failure or empty response, fall back to direct Kraken REST.

Return format: list of lists, one entry per candle —
    [timestamp, open, high, low, close, volume, count]
  - timestamp : int   (Unix seconds)
  - open/high/low/close : float
  - volume    : float
  - count     : int   (trade count; 0 when not available)

Returns [] on total failure, never raises.

Stdlib only (urllib.request + json) — no third-party dependencies.
"""

import json
import urllib.request
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Pair normalisation
# ---------------------------------------------------------------------------

# Canonical display → Kraken REST symbol overrides.
# Kraken uses XBT for Bitcoin and has a few other quirks.
_PAIR_OVERRIDES = {
    "BTC/USD":  "XBTUSD",
    "BTC/EUR":  "XBTEUR",
    "ETH/USD":  "ETHUSD",
    "ETH/BTC":  "ETHXBT",
    "LTC/USD":  "LTCUSD",
    "LTC/BTC":  "LTCXBT",
    "XMR/USD":  "XMRUSD",
    "XMR/BTC":  "XMRXBT",
    "XRP/USD":  "XRPUSD",
    "XRP/BTC":  "XRPXBT",
}


def kraken_pair(pair: str) -> str:
    """Convert a display pair like 'BTC/USD' to a Kraken REST symbol.

    Uses the override table for known quirks (XBT, XMR prefix rules, etc.).
    For everything else, strips the slash: 'SOL/USD' → 'SOLUSD'.
    """
    if pair in _PAIR_OVERRIDES:
        return _PAIR_OVERRIDES[pair]
    return pair.replace("/", "")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_url(url: str, timeout: int = 10) -> object:
    """GET url, return parsed JSON or None."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _normalise_candle(row) -> list:
    """Convert a raw Kraken OHLC row (list) to the canonical 7-element list.

    Kraken format: [time, open, high, low, close, vwap, volume, count]
    We keep:       [time, open, high, low, close, volume, count]
    """
    try:
        return [
            int(row[0]),
            float(row[1]),
            float(row[2]),
            float(row[3]),
            float(row[4]),
            float(row[6]),   # volume (index 6, skipping vwap at 5)
            int(row[7]) if len(row) > 7 else 0,
        ]
    except (IndexError, TypeError, ValueError):
        return []


def _normalise_cc_candle(row) -> list:
    """Normalise a candle returned by the CC proxy.

    The CC proxy may return either:
      - a list  [time, open, high, low, close, vwap, volume, count]  (raw Kraken)
      - a dict  {"time": …, "open": …, "high": …, "low": …, "close": …, "volume": …}
    """
    try:
        if isinstance(row, list):
            # Same raw Kraken format
            return _normalise_candle(row)
        # Dict form
        return [
            int(row.get("time", 0)),
            float(row.get("open", 0)),
            float(row.get("high", 0)),
            float(row.get("low", 0)),
            float(row.get("close", 0)),
            float(row.get("volume", 0)),
            int(row.get("count", 0)),
        ]
    except (KeyError, TypeError, ValueError):
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_ohlc(
    pair: str,
    interval: int,
    limit: int = 100,
    cc_url: str = "http://localhost:9000",
) -> list:
    """Fetch OHLC candles for *pair* at *interval*-minute bars.

    Parameters
    ----------
    pair     : Display-format pair, e.g. ``"BTC/USD"``.
    interval : Bar size in minutes (1, 5, 15, 60, 240, 1440 …).
    limit    : Maximum number of candles to return.
    cc_url   : Base URL of the Command Center proxy.

    Returns
    -------
    list[list]
        Each element is ``[timestamp, open, high, low, close, volume, count]``.
        Returns ``[]`` on total failure, never raises.
    """
    # --- 1. Try Command Center proxy -----------------------------------------
    try:
        qs = urlencode({"pair": pair, "interval": interval, "limit": limit})
        data = _fetch_url(f"{cc_url}/api/market/ohlc?{qs}", timeout=10)
        if data and data.get("candles"):
            candles = [_normalise_cc_candle(r) for r in data["candles"]]
            candles = [c for c in candles if c]  # drop any failed rows
            if candles:
                return candles[-limit:] if len(candles) > limit else candles
    except Exception:
        pass

    # --- 2. Kraken direct fallback -------------------------------------------
    try:
        kp = kraken_pair(pair)
        qs = urlencode({"pair": kp, "interval": interval})
        data = _fetch_url(
            f"https://api.kraken.com/0/public/OHLC?{qs}", timeout=10
        )
        if data and not data.get("error"):
            for key, rows in data.get("result", {}).items():
                if key != "last" and isinstance(rows, list):
                    candles = [_normalise_candle(r) for r in rows]
                    candles = [c for c in candles if c]
                    return candles[-limit:] if len(candles) > limit else candles
    except Exception:
        pass

    return []
