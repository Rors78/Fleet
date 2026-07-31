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

    def reserve(self, pair, direction, amount, stop_loss_pct=None,
                is_reentry=False):
        """Request capital. Returns (ok, reservation_id_or_reason).

        Set is_reentry=True when re-claiming capital for a position that is
        ALREADY open (e.g. after a restart, or when a reservation went stale).
        Direction policy gates — such as the fleet-wide short ban — are skipped
        for re-entries, because refusing them makes the bot read its own open
        position as unfunded and close it at market.
        """
        data = {
            "bot_id": self.bot_id,
            "pair": pair,
            "direction": direction,
            "amount": amount,
        }
        if stop_loss_pct is not None:
            data["stop_loss_pct"] = stop_loss_pct
        if is_reentry:
            data["is_reentry"] = True
        result = self._post("/api/portfolio/reserve", data)
        if result is None:
            return False, "Command Center unreachable"
        if result.get("ok"):
            return True, result["reservation_id"]
        return False, result.get("reason", "Unknown rejection")

    def release(self, reservation_id, pnl=0.0, entry_price=0.0, exit_price=0.0):
        """Return capital after trade closes.
        entry_price and exit_price enable accurate expectancy recording at Command Center.
        """
        result = self._post("/api/portfolio/release", {
            "reservation_id": reservation_id,
            "pnl": pnl,
            "entry_price": entry_price,
            "exit_price": exit_price,
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

    def confirm_reservations(self, reservation_ids) -> dict | None:
        """Lease heartbeat: declare the reservation ids this bot still holds.

        Call once per scan cycle with the FULL set of rids the bot references
        (an empty list is a valid declaration meaning "I hold nothing").
        Pool-side, reservations booked to this bot that stay undeclared past
        a grace window are released as orphans — a bot wiring bug can strand
        capital for minutes, not forever. Returns the pool's response dict
        ({ok, confirmed, swept}) or None on network error. Never raises.
        """
        try:
            return self._post("/api/portfolio/confirm", {
                "bot_id": self.bot_id,
                "reservation_ids": list(reservation_ids or []),
            })
        except Exception:
            return None
