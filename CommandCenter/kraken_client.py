#!/usr/bin/env python3
"""
Shared Kraken Spot Trading Client
===================================
Stdlib-only (no ccxt). Signs and sends Kraken private REST API requests.
Long spot only in live mode — buy crypto with USD, sell crypto for USD.

Usage:
    from kraken_client import KrakenSpotClient

    client = KrakenSpotClient()  # reads KRAKEN_API_KEY / KRAKEN_API_SECRET from env
    ok, txid = client.buy("XXBTZUSD", 0.001)        # buy 0.001 BTC
    ok, txid = client.sell("XXBTZUSD", 0.001)        # sell 0.001 BTC
    price = client.get_fill_price(txid, fallback=84000.0)

    # Or use the generic method:
    ok, txid = client.place_order("XXBTZUSD", "buy", 0.001)
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request


class KrakenSpotClient:
    """Kraken spot trading via private REST API. Stdlib only, no deps."""

    BASE = "https://api.kraken.com"

    def __init__(self, key: str = None, secret: str = None):
        self.key = key or os.environ.get("KRAKEN_API_KEY", "")
        self.secret = secret or os.environ.get("KRAKEN_API_SECRET", "")

    @property
    def has_credentials(self) -> bool:
        return bool(self.key and self.secret)

    def _sign(self, urlpath: str, data: dict) -> str:
        nonce = str(int(time.time() * 1000))
        data["nonce"] = nonce
        postdata = urllib.parse.urlencode(data)
        encoded = (nonce + postdata).encode("utf-8")
        message = urlpath.encode("utf-8") + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _private(self, method: str, params: dict = None) -> dict:
        """Call a Kraken private API method. Returns parsed JSON response."""
        if not self.has_credentials:
            return {"error": ["No API credentials (KRAKEN_API_KEY / KRAKEN_API_SECRET)"]}
        params = dict(params or {})
        urlpath = f"/0/private/{method}"
        sig = self._sign(urlpath, params)
        url = self.BASE + urlpath
        postdata = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=postdata, headers={
            "API-Key": self.key,
            "API-Sign": sig,
            "User-Agent": "FleetBot/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            return {"error": [str(e)]}

    def place_order(self, pair: str, side: str, volume: float,
                    ordertype: str = "limit", price: float = None,
                    post_only: bool = False, leverage=None) -> tuple:
        """Place a spot order. side='buy' or 'sell'. Returns (ok, txid_or_error).

        LIMIT ORDERS ONLY — fleet policy, no exceptions. `ordertype` defaults to
        "limit" and anything else is refused here rather than sent to Kraken.
        A market order is a blank cheque on fill price: on a thin book it can
        slip far past the level the strategy decided on, and the published
        signal price would no longer match what was actually filled.

        `price` is REQUIRED. It is not defaulted, because a limit order without
        an explicit price is exactly the mistake this gate exists to prevent —
        silently falling back to market is how "limit only" becomes untrue.
        """
        if ordertype != "limit":
            return False, (f"LIMIT_ONLY: ordertype={ordertype!r} refused — "
                           f"fleet policy permits limit orders only")
        if price is None or float(price) <= 0:
            return False, ("LIMIT_ONLY: a limit order requires an explicit "
                           "positive price")

        payload = {
            "pair": pair,
            "type": side,
            "ordertype": "limit",
            "price": f"{float(price):.8f}",
            "volume": f"{volume:.8f}",
        }
        if post_only:
            # oflags=post makes Kraken REJECT the order rather than let it
            # cross the spread. For a grid that is the point: a rung is a
            # resting bid/ask, and one that would take liquidity is not the
            # trade the strategy designed -- it is a worse price AND the
            # taker fee (0.60 vs 0.30 on this account). A rejection here is
            # correct behaviour; the caller leaves the level unfilled and
            # retries on the next price check.
            payload["oflags"] = "post"
        if leverage and float(leverage) > 1:
            # SCARS, all earned on this account:
            #  - the pair must name the MARGIN book (ALTNAME:BTNL). Sending
            #    leverage against the spot name is refused as "Non-ECP",
            #    which reads like an account problem and is not.
            #  - leverage must also be on the CLOSING order, or the position
            #    stays open after what looks like a successful close.
            payload["leverage"] = str(int(float(leverage)))
        result = self._private("AddOrder", payload)
        if result.get("error"):
            errs = result["error"]
            # Filter out non-errors (Kraken returns warnings in error array)
            real_errors = [e for e in errs if not e.startswith("W")]
            if real_errors:
                return False, "; ".join(real_errors)
        txids = result.get("result", {}).get("txid", [])
        return True, txids[0] if txids else "unknown"

    def buy(self, pair: str, volume: float, price: float = None,
            ordertype: str = "limit", post_only: bool = False,
            leverage=None) -> tuple:
        """Buy crypto (spot) as a LIMIT order. `price` is required.

        Note the signature: price is the third positional arg. Callers that
        pass only (pair, volume) now get a clear LIMIT_ONLY refusal instead of
        silently placing a market order.
        """
        return self.place_order(pair, "buy", volume, ordertype, price,
                                post_only=post_only, leverage=leverage)

    def sell(self, pair: str, volume: float, price: float = None,
             ordertype: str = "limit", post_only: bool = False,
             leverage=None) -> tuple:
        """Sell crypto (spot) as a LIMIT order. `price` is required."""
        return self.place_order(pair, "sell", volume, ordertype, price,
                                post_only=post_only, leverage=leverage)

    def get_fill_price(self, txid: str, fallback: float) -> float:
        """Query order to get actual fill price. Returns fallback on failure."""
        result = self._private("QueryOrders", {"txid": txid, "trades": True})
        if result.get("error"):
            return fallback
        order = result.get("result", {}).get(txid, {})
        try:
            price = float(order.get("price", 0))
            return price if price > 0 else fallback
        except (ValueError, TypeError):
            return fallback

    def get_balance(self) -> dict:
        """Get account balances. Returns {asset: balance_str} or empty dict on failure."""
        result = self._private("Balance")
        if result.get("error"):
            return {}
        return result.get("result", {})

    def get_trade_balance(self, asset: str = "ZUSD") -> dict:
        """Get trade balance (equity, margin, free margin). Returns dict or empty on failure."""
        result = self._private("TradeBalance", {"asset": asset})
        if result.get("error"):
            return {}
        return result.get("result", {})
