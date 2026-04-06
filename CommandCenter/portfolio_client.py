"""
Shared PortfolioClient for fleet bots.
Thin client to Command Center's portfolio manager.
Uses urllib (stdlib only) — no external dependencies.

Usage:
    from portfolio_client import PortfolioClient
    client = PortfolioClient("http://127.0.0.1:9000", bot_id="gridzilla")
    ok, rid = client.reserve("BTC/USD", "LONG", 500.0)
    if ok:
        # ... trade ...
        client.release(rid, pnl=12.50)
"""

import json
import time
import urllib.request as urlreq
import urllib.error


class PortfolioClient:
    """Thin client to Command Center's portfolio manager."""

    def __init__(self, base_url, bot_id, max_retries=3, timeout=5):
        self.base_url = base_url.rstrip("/")
        self.bot_id = bot_id
        self.max_retries = max_retries
        self.timeout = timeout

    def _post(self, path, data):
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8")
        last_error = None
        for attempt in range(self.max_retries):
            try:
                req = urlreq.Request(url, data=body, headers={"Content-Type": "application/json"})
                resp = urlreq.urlopen(req, timeout=self.timeout)
                return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    return json.loads(e.read().decode("utf-8"))
                except Exception as e2:
                    last_error = e2
            except Exception as e:
                last_error = e
            if attempt < self.max_retries - 1:
                time.sleep(0.5 * (2 ** attempt))
        return None

    def _get(self, path):
        url = f"{self.base_url}{path}"
        last_error = None
        for attempt in range(self.max_retries):
            try:
                resp = urlreq.urlopen(url, timeout=self.timeout)
                return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                last_error = e
            if attempt < self.max_retries - 1:
                time.sleep(0.5 * (2 ** attempt))
        return None

    def reserve(self, pair, direction, amount, stop_loss_pct=None):
        """Request capital. Returns (ok, reservation_id_or_reason)."""
        data = {
            "bot_id": self.bot_id,
            "pair": pair,
            "direction": direction,
            "amount": amount,
        }
        if stop_loss_pct is not None:
            data["stop_loss_pct"] = stop_loss_pct
        result = self._post("/api/portfolio/reserve", data)
        if result is None:
            return False, "Command Center unreachable"
        if result.get("ok"):
            return True, result["reservation_id"]
        return False, result.get("reason", "Unknown rejection")

    def release(self, reservation_id, pnl=0.0):
        """Return capital after trade closes."""
        result = self._post("/api/portfolio/release", {
            "reservation_id": reservation_id,
            "pnl": pnl,
        })
        if result is None:
            return False, "Command Center unreachable"
        return result.get("ok", False), result.get("reason", "")

    def available(self):
        """Check available capital. Returns float or None on error."""
        result = self._get("/api/portfolio/available")
        if result is None:
            return None
        return result.get("available")

    def get_reservations(self) -> dict | None:
        """Get all active reservations. Returns {reservation_id: {...}} or None on error."""
        result = self._get("/api/portfolio/reservations")
        if result is None:
            return None
        return result.get("reservations", {})
