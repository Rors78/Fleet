"""
Event Publisher — Lightweight client for bots to push events to the fleet bus.
~20 lines of actual logic. Stdlib only (urllib).

Usage:
    from event_publisher import EventPublisher
    pub = EventPublisher("http://127.0.0.1:9000", "trekbot")
    pub.emit("TRADE_OPEN", {"pair": "BTC/USD", "direction": "LONG", "size": 37.50})
    pub.emit("SIGNAL", {"pair": "ETH/USD", "direction": "LONG", "confidence": 0.85})
"""

import json
import threading
import time
import urllib.request as urlreq


class EventPublisher:
    """Fire-and-forget event publisher. Non-blocking — sends in background thread."""

    def __init__(self, base_url: str, source: str):
        self.url = f"{base_url.rstrip('/')}/api/events/publish"
        self.source = source

    def emit(self, event_type: str, data: dict = None):
        """Publish an event to the fleet bus. Non-blocking."""
        event = {
            "source": self.source,
            "type": event_type,
            "data": data or {},
            "ts": time.time(),
        }
        # Fire and forget — don't block the bot's main loop
        threading.Thread(target=self._send, args=(event,), daemon=True).start()

    def _send(self, event: dict):
        try:
            body = json.dumps(event).encode("utf-8")
            req = urlreq.Request(self.url, data=body,
                                 headers={"Content-Type": "application/json"})
            urlreq.urlopen(req, timeout=2)
        except Exception:
            pass  # bus down? ignore — bots must never block on event publishing
