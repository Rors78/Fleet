#!/usr/bin/env python3
"""
BRAINIAC COLLECTOR — Absorb everything. Process later.
========================================================
Background service that continuously collects market data the fleet
doesn't currently see. Stores to ``brainiac/`` (relative to script dir).

Data streams:
  1. Order book depth    — every 30s for top 10 pairs
  2. Recent trades       — every 60s for top 10 pairs
  3. Global metrics      — every 5 min (CoinGecko)
  4. Correlation matrix  — every 15 min (top 20 pairs)
  5. Funding rates       — every 5 min (Kraken Futures)

Usage: imported by command_center.py or run standalone
"""

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import requests

log = logging.getLogger("brainiac")

BRAINIAC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brainiac")
KRAKEN_REST = "https://api.kraken.com/0/public"
CC_URL = "http://localhost:9000"

# In-memory cache of latest values per category/key — avoids full file scans.
_latest_cache = {}   # {(category, key): entry_dict}
_latest_lock = threading.Lock()


def _store(category, key, data):
    """Append-only JSONL storage. Brainiac never deletes."""
    dirpath = os.path.join(BRAINIAC_DIR, category)
    os.makedirs(dirpath, exist_ok=True)
    filename = os.path.join(dirpath, f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl")
    entry = {"ts": time.time(), "key": key, "data": data}
    try:
        with open(filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        log.warning("Failed to write brainiac/%s/%s", category, key, exc_info=True)
        return
    # Update in-memory cache so get_latest doesn't need to scan the file
    with _latest_lock:
        _latest_cache[(category, key)] = entry


def get_latest(category, key):
    """Get most recent entry for a category/key.

    Checks the in-memory cache first (populated by _store). Falls back to
    scanning today's JSONL file on cache miss (e.g. after process restart).
    """
    with _latest_lock:
        cached = _latest_cache.get((category, key))
    if cached:
        return cached

    # Cache miss — scan today's file and populate cache
    filename = os.path.join(BRAINIAC_DIR, category,
                            f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl")
    if not os.path.exists(filename):
        return None
    try:
        last = None
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("key") == key:
                        last = entry
                except json.JSONDecodeError:
                    continue
        if last:
            with _latest_lock:
                _latest_cache[(category, key)] = last
        return last
    except OSError:
        return None


def _get_universe_pairs():
    """Fetch universe pairs from Command Center."""
    try:
        resp = requests.get(f"{CC_URL}/api/universe", timeout=5)
        data = resp.json()
        return [p["display"] for p in data.get("pairs", [])]
    except Exception:
        return ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
                "DOGE/USD", "DOT/USD", "AVAX/USD", "LINK/USD", "LTC/USD"]


_UNIVERSE_REFRESH_SECONDS = 6 * 3600  # Re-fetch universe every 6 hours


class BrainiacCollector:
    """Continuous data collection engine."""

    def __init__(self):
        self.pairs = []
        self._pairs_lock = threading.Lock()
        self._pairs_last_refresh = 0
        self.running = True
        self.stats = {"depth": 0, "trades": 0, "metrics": 0,
                      "correlations": 0, "funding": 0}

    def _refresh_pairs(self):
        """Re-fetch universe pairs if stale."""
        now = time.time()
        if now - self._pairs_last_refresh < _UNIVERSE_REFRESH_SECONDS and self.pairs:
            return
        new_pairs = _get_universe_pairs()
        if new_pairs:
            with self._pairs_lock:
                self.pairs = new_pairs
                self._pairs_last_refresh = now

    def _get_pairs(self, limit=None):
        """Get current pairs list, thread-safe, with optional limit."""
        with self._pairs_lock:
            return list(self.pairs[:limit]) if limit else list(self.pairs)

    def start(self):
        """Launch all collection threads."""
        self.pairs = _get_universe_pairs()
        self._pairs_last_refresh = time.time()
        threads = [
            threading.Thread(target=self._collect_depth, daemon=True, name="Brainiac-Depth"),
            threading.Thread(target=self._collect_trades, daemon=True, name="Brainiac-Trades"),
            threading.Thread(target=self._collect_global, daemon=True, name="Brainiac-Global"),
            threading.Thread(target=self._collect_correlations, daemon=True, name="Brainiac-Corr"),
            threading.Thread(target=self._collect_funding, daemon=True, name="Brainiac-Funding"),
        ]
        for t in threads:
            t.start()
        return threads

    def _collect_depth(self):
        """Order book snapshots — reveals institutional positioning."""
        time.sleep(5)
        while self.running:
            self._refresh_pairs()
            for pair in self._get_pairs(limit=10):
                try:
                    resp = requests.get(f"{KRAKEN_REST}/Depth",
                                        params={"pair": pair, "count": 25}, timeout=10)
                    data = resp.json()
                    if not data.get("error"):
                        for key, book in data.get("result", {}).items():
                            if isinstance(book, dict):
                                bids = book.get("bids", [])
                                asks = book.get("asks", [])
                                bid_depth = sum(float(b[1]) * float(b[0]) for b in bids[:10])
                                ask_depth = sum(float(a[1]) * float(a[0]) for a in asks[:10])
                                spread = (float(asks[0][0]) - float(bids[0][0])) / float(bids[0][0]) * 100 if bids and asks else 0
                                _store("depth", pair, {
                                    "bid_depth_usd": round(bid_depth, 2),
                                    "ask_depth_usd": round(ask_depth, 2),
                                    "imbalance": round((bid_depth - ask_depth) / (bid_depth + ask_depth + 1e-10), 4),
                                    "spread_pct": round(spread, 4),
                                    "levels": min(len(bids), len(asks)),
                                })
                                self.stats["depth"] += 1
                except Exception:
                    pass
                time.sleep(1.5)
            time.sleep(15)

    def _collect_trades(self):
        """Recent trades — reveals order flow patterns."""
        time.sleep(10)
        last_ids = {}
        while self.running:
            self._refresh_pairs()
            for pair in self._get_pairs(limit=10):
                try:
                    params = {"pair": pair, "count": 50}
                    if pair in last_ids:
                        params["since"] = last_ids[pair]
                    resp = requests.get(f"{KRAKEN_REST}/Trades",
                                        params=params, timeout=10)
                    data = resp.json()
                    if not data.get("error"):
                        for key, val in data.get("result", {}).items():
                            if key == "last":
                                last_ids[pair] = val
                            elif isinstance(val, list) and val:
                                sizes = [float(t[1]) * float(t[0]) for t in val]
                                buys = sum(1 for t in val if t[3] == "b")
                                sells = sum(1 for t in val if t[3] == "s")
                                _store("trades", pair, {
                                    "count": len(val),
                                    "buys": buys,
                                    "sells": sells,
                                    "avg_size_usd": round(sum(sizes) / len(sizes), 2) if sizes else 0,
                                    "max_size_usd": round(max(sizes), 2) if sizes else 0,
                                    "total_volume_usd": round(sum(sizes), 2),
                                    "buy_ratio": round(buys / len(val), 3) if val else 0.5,
                                })
                                self.stats["trades"] += 1
                except Exception:
                    pass
                time.sleep(1.5)
            time.sleep(35)

    def _collect_global(self):
        """Global market state from CoinGecko."""
        time.sleep(15)
        while self.running:
            try:
                resp = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
                data = resp.json().get("data", {})
                mcp = data.get("market_cap_percentage", {})
                _store("metrics", "global", {
                    "total_market_cap_usd": data.get("total_market_cap", {}).get("usd"),
                    "total_volume_usd": data.get("total_volume", {}).get("usd"),
                    "btc_dominance": round(mcp.get("btc", 0), 2),
                    "eth_dominance": round(mcp.get("eth", 0), 2),
                    "active_cryptos": data.get("active_cryptocurrencies"),
                    "market_cap_change_24h": round(data.get("market_cap_change_percentage_24h_usd", 0), 2),
                })
                self.stats["metrics"] += 1
            except Exception:
                pass
            time.sleep(300)

    def _collect_correlations(self):
        """Full pair correlation matrix from 1h returns (top 20 pairs)."""
        time.sleep(20)
        while self.running:
            self._refresh_pairs()
            try:
                returns = {}
                for pair in self._get_pairs(limit=20):
                    try:
                        resp = requests.get(f"{KRAKEN_REST}/OHLC",
                                            params={"pair": pair, "interval": 60}, timeout=10)
                        data = resp.json()
                        if not data.get("error"):
                            for key, candles in data.get("result", {}).items():
                                if key != "last" and isinstance(candles, list):
                                    closes = [float(c[4]) for c in candles[-48:]]
                                    if len(closes) > 2:
                                        rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
                                                for i in range(1, len(closes))]
                                        returns[pair] = rets
                    except Exception:
                        pass
                    time.sleep(0.5)

                if len(returns) < 3:
                    time.sleep(900)
                    continue

                # Compute correlation matrix
                pairs_list = list(returns.keys())
                n = len(pairs_list)
                matrix = {}
                for i in range(n):
                    for j in range(i + 1, n):
                        raw_a, raw_b = returns[pairs_list[i]], returns[pairs_list[j]]
                        min_len = min(len(raw_a), len(raw_b))
                        if min_len < 10:
                            continue
                        a, b = raw_a[-min_len:], raw_b[-min_len:]
                        m_a = sum(a) / len(a)
                        m_b = sum(b) / len(b)
                        num = sum((a[k] - m_a) * (b[k] - m_b) for k in range(len(a)))
                        d_a = math.sqrt(sum((x - m_a) ** 2 for x in a)) + 1e-10
                        d_b = math.sqrt(sum((x - m_b) ** 2 for x in b)) + 1e-10
                        corr = num / (d_a * d_b)
                        matrix[f"{pairs_list[i]}|{pairs_list[j]}"] = round(corr, 4)

                avg_abs = sum(abs(v) for v in matrix.values()) / max(len(matrix), 1)
                _store("correlations", "matrix", {
                    "pairs_count": n,
                    "correlations_count": len(matrix),
                    "avg_abs_correlation": round(avg_abs, 4),
                    "top_correlated": sorted(matrix.items(), key=lambda x: abs(x[1]), reverse=True)[:5],
                    "least_correlated": sorted(matrix.items(), key=lambda x: abs(x[1]))[:5],
                })
                self.stats["correlations"] += 1
            except Exception:
                pass
            time.sleep(900)

    def _collect_funding(self):
        """Funding rates — leveraged positioning indicator."""
        time.sleep(25)
        while self.running:
            try:
                # Use Kraken Futures REST API directly
                resp = requests.get("https://futures.kraken.com/derivatives/api/v3/tickers", timeout=10)
                data = resp.json()
                rates = {}
                for ticker in data.get("tickers", []):
                    symbol = ticker.get("symbol", "")
                    if "pf_" in symbol.lower():  # perpetual futures
                        fr = ticker.get("fundingRate")
                        if fr is not None:
                            base = symbol.replace("pf_", "").replace("usd", "").upper()
                            rates[base] = {
                                "rate": round(fr, 6),
                                "mark_price": ticker.get("markPrice"),
                                "open_interest": ticker.get("openInterest"),
                            }
                if rates:
                    # Classify: positive funding = longs pay shorts = overleveraged long
                    extreme_long = {k: v for k, v in rates.items() if v["rate"] > 0.0005}
                    extreme_short = {k: v for k, v in rates.items() if v["rate"] < -0.0005}
                    _store("funding", "rates", {
                        "pairs": len(rates),
                        "avg_rate": round(sum(v["rate"] for v in rates.values()) / max(len(rates), 1), 6),
                        "extreme_long_count": len(extreme_long),
                        "extreme_short_count": len(extreme_short),
                        "rates": rates,
                    })
                    self.stats["funding"] += 1
            except Exception:
                pass
            time.sleep(300)


# ---------------------------------------------------------------------------
# Serve via Command Center endpoints
# ---------------------------------------------------------------------------

def register_brainiac_endpoints(handler_class):
    """Add Brainiac data endpoints to an existing HTTP handler.

    Monkey-patches do_GET to intercept /api/brainiac/* requests. Uses
    the handler's _send_json for consistent response formatting.
    """
    if getattr(handler_class, '_brainiac_registered', False):
        return
    handler_class._brainiac_registered = True

    original_do_GET = handler_class.do_GET

    def new_do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/brainiac/"):
            qs = parse_qs(parsed.query)
            category = path.split("/api/brainiac/")[1]
            pair = qs.get("pair", [None])[0]

            if category == "depth" and pair:
                data = get_latest("depth", pair)
            elif category == "correlations":
                data = get_latest("correlations", "matrix")
            elif category == "funding":
                data = get_latest("funding", "rates")
            elif category == "metrics":
                data = get_latest("metrics", "global")
            elif category == "trades" and pair:
                data = get_latest("trades", pair)
            elif category == "stats":
                data = {"ts": time.time(), "data": _collector.stats if _collector else {}}
            else:
                data = None

            self._send_json(data or {"error": "not found"}, 200 if data else 404)
            return

        return original_do_GET(self)

    handler_class.do_GET = new_do_GET


_collector = None


def _rotate_brainiac(max_days=90):
    """Remove Brainiac JSONL files older than max_days.

    Uses the date from the filename (YYYY-MM-DD.jsonl) rather than mtime,
    so copied or touched files are still rotated correctly.
    """
    import glob as _glob
    cutoff = time.time() - max_days * 86400
    removed = 0
    for cat in ("depth", "trades", "metrics", "correlations", "funding"):
        pattern = os.path.join(BRAINIAC_DIR, cat, "*.jsonl")
        for f in _glob.glob(pattern):
            try:
                basename = os.path.basename(f).replace(".jsonl", "")
                file_date = datetime.strptime(basename, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if file_date.timestamp() < cutoff:
                    os.remove(f)
                    removed += 1
            except (ValueError, OSError):
                pass
    return removed


def start_collector():
    """Start the Brainiac collector. Returns the instance.

    Safe to call multiple times — returns existing instance if already running.
    """
    global _collector
    if _collector is not None:
        return _collector
    removed = _rotate_brainiac(90)
    if removed:
        print(f"  Brainiac: rotated {removed} files older than 90 days")
    _collector = BrainiacCollector()
    _collector.start()
    return _collector


if __name__ == "__main__":
    print("\n  BRAINIAC COLLECTOR v1.0")
    print("  Absorbing market data...")
    print(f"  Storage: {BRAINIAC_DIR}")
    print()
    c = start_collector()
    try:
        while True:
            time.sleep(60)
            print(f"  Stats: {c.stats}")
    except KeyboardInterrupt:
        print("\n  Collector stopped.")
