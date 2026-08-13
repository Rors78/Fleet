"""
FLEET EVENT BUS — Real-Time Pub/Sub for the Trading Fleet
==========================================================
SSE-based event bus. Bots publish events via POST, consumers
subscribe via SSE stream. Replaces 4-second polling with
instant event propagation.

Events:
    TRADE_OPEN, TRADE_CLOSE, SIGNAL, REGIME_CHANGE, WHALE_ALERT,
    PORTFOLIO_RESERVE, PORTFOLIO_DENIAL, BOT_STATUS, PRICE_ALERT,
    ATTENTION, FLEET_ALERT, HIGH_CONVICTION, EMERGENCY_REDUCE

Architecture:
    POST /api/events/publish  → bot pushes an event
    GET  /api/events/stream   → SSE stream (real-time)
    GET  /api/events/recent   → last N events (catch-up)

Usage from bots:
    from event_publisher import EventPublisher
    pub = EventPublisher("http://127.0.0.1:9000", "trekbot")
    pub.emit("TRADE_OPEN", {"pair": "BTC/USD", "direction": "LONG", "size": 37.50})
"""

import json
import logging
import os
import threading
import time

# This module had no logger at all, so a skipped or malformed reaction had
# nowhere to report. Command Center configures the root handler; this rides
# it rather than printing into a hot event path.
log = logging.getLogger("event_bus")
from collections import deque
from typing import List, Optional


# Durable event log (append-only JSONL, one file per day).
# Every published event is appended here before any in-memory routing so that
# reboots never lose history. Dashboards and /api/trades read from these files.
EVENT_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "logs", "event_bus")


def _event_log_path_for(ts: float) -> str:
    """Return the JSONL path for the UTC date of a given timestamp."""
    day = time.strftime("%Y-%m-%d", time.gmtime(ts))
    return os.path.join(EVENT_LOG_DIR, f"{day}.jsonl")


class EventBus:
    """In-process event bus with SSE broadcast, reaction rules, and durable JSONL log."""

    def __init__(self, max_history: int = 500):
        self._subscribers: List[dict] = []  # [{"queue": deque, "filter": dict|None}]
        self._lock = threading.Lock()
        self._history = deque(maxlen=max_history)
        self._reactions: List[dict] = []
        self._reaction_lock = threading.Lock()
        # Rolling window for condition evaluation
        self._recent_events = deque(maxlen=2000)
        # Durable log write serialization (separate from state lock)
        self._disk_lock = threading.Lock()
        try:
            os.makedirs(EVENT_LOG_DIR, exist_ok=True)
        except Exception:
            pass
        # Rehydrate in-memory history from today's log so /api/events/recent
        # returns useful data immediately after a restart instead of going blank.
        self._rehydrate_history()

    def _rehydrate_history(self) -> None:
        """Load today's JSONL file into self._history on startup.

        Silent on failure — a missing or corrupt file should not prevent CC from
        starting. Subscribers created after rehydration start with clean queues.
        """
        path = _event_log_path_for(time.time())
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                # Only load the tail — max_history caps what we keep in RAM anyway.
                lines = fh.readlines()
            # Walk from the end so we fill self._history with the most recent events
            # up to its maxlen, preserving chronological order.
            maxlen = self._history.maxlen or 500
            tail = lines[-maxlen:]
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                self._history.append(ev)
                self._recent_events.append(ev)
        except Exception:
            # Never block startup on a damaged log file.
            pass

    def _append_to_disk(self, event: dict) -> None:
        """Append an event to today's JSONL file. Safe to call from any thread."""
        try:
            line = json.dumps(event, default=str) + "\n"
        except Exception:
            return  # Unserializable event; don't crash the publish path.
        path = _event_log_path_for(event.get("ts", time.time()))
        with self._disk_lock:
            try:
                # Ensure directory exists even if it was deleted mid-run.
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except Exception:
                # Swallow — disk errors must not break live trading signal flow.
                pass

    # ── Publishing ──

    MAX_REACTION_DEPTH = 3

    def publish(self, event: dict):
        """Publish an event to all subscribers, check reactions, and durably log.

        Disk write happens AFTER in-memory routing so subscriber latency is not
        affected by I/O. If CC crashes between the in-memory append and the disk
        append, we lose at most one event — acceptable.
        """
        event.setdefault("ts", time.time())
        event.setdefault("id", f"{event.get('source', 'unknown')}_{int(event['ts'] * 1000)}")

        with self._lock:
            self._history.append(event)
            self._recent_events.append(event)
            etype = event.get("type")
            for sub in self._subscribers:
                if sub["filter"] and etype not in sub["filter"]:
                    continue  # pre-filter: don't waste queue space
                sub["queue"].append(event)

        # Durable append (outside the state lock — disk I/O must not block routing).
        self._append_to_disk(event)

        # Check reactions in a separate thread to avoid blocking publish
        depth = event.get("_reaction_depth", 0)
        if depth < self.MAX_REACTION_DEPTH:
            threading.Thread(target=self._check_reactions, args=(event,), daemon=True).start()

    # ── Subscribing (for SSE) ──

    def subscribe(self, filter_types: Optional[List[str]] = None) -> dict:
        """Create a new subscriber. Returns subscriber dict with 'queue' to read from."""
        sub = {
            "queue": deque(maxlen=500),
            "filter": set(filter_types) if filter_types else None,
            "created": time.time(),
        }
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: dict):
        """Remove a subscriber."""
        with self._lock:
            try:
                self._subscribers.remove(sub)
            except ValueError:
                pass

    def get_recent(self, n: int = 50, event_type: Optional[str] = None) -> List[dict]:
        """Get last N events, optionally filtered by type."""
        with self._lock:
            events = list(self._history)
        if event_type:
            events = [e for e in events if e.get("type") == event_type]
        return events[-n:]

    # ── Reaction Rules ──

    def load_reactions(self, rules: List[dict]):
        """Load reaction rules from config."""
        with self._reaction_lock:
            self._reactions = rules

    def _check_reactions(self, event: dict):
        """Evaluate reaction rules against the event."""
        with self._reaction_lock:
            rules = list(self._reactions)

        for rule in rules:
            try:
                if self._matches_trigger(event, rule.get("trigger", {})):
                    condition = rule.get("condition")
                    if condition and not self._evaluate_condition(condition, event):
                        continue
                    self._fire_reaction(rule, event)
            except Exception:
                pass  # never crash the bus on a bad rule

    def _matches_trigger(self, event: dict, trigger: dict) -> bool:
        """Check if event matches trigger criteria."""
        for key, val in trigger.items():
            # Support nested keys like "data.tier"
            parts = key.split(".")
            obj = event
            for part in parts:
                if isinstance(obj, dict):
                    obj = obj.get(part)
                else:
                    obj = None
                    break
            if obj != val:
                return False
        return True

    def _evaluate_condition(self, condition: str, event: dict) -> bool:
        """Evaluate a condition string against recent events.

        Supports:
            count(TYPE, last_Xmin) >= N
            count(TYPE, same_pair, same_direction, last_Xs) >= N
        """
        import re
        # Parse: count(ARGS) >= N
        m = re.match(r"count\((.+)\)\s*(>=|>|==|<=|<)\s*(\d+)", condition)
        if not m:
            return False

        args_str, op, threshold = m.group(1), m.group(2), int(m.group(3))
        args = [a.strip() for a in args_str.split(",")]

        # First arg is event type (or type.subfield)
        event_type = args[0]
        # Parse time window
        window_sec = 300  # default 5 min
        same_fields = []
        for arg in args[1:]:
            if arg.startswith("last_"):
                t = arg[5:]
                if t.endswith("min"):
                    window_sec = int(t[:-3]) * 60
                elif t.endswith("s"):
                    window_sec = int(t[:-1])
                elif t.endswith("h"):
                    window_sec = int(t[:-1]) * 3600
            elif arg.startswith("same_"):
                same_fields.append(arg[5:])

        cutoff = time.time() - window_sec
        count = 0
        with self._lock:
            for e in self._recent_events:
                if e.get("ts", 0) < cutoff:
                    continue
                # Type match (support "TYPE.subfield" like TRADE_CLOSE.stop_loss)
                if "." in event_type:
                    etype, subval = event_type.split(".", 1)
                    if e.get("type") != etype:
                        continue
                    # Check subfield in data (case-insensitive)
                    data = e.get("data", {})
                    if isinstance(data, dict):
                        actual = data.get("exit_reason", "")
                        if str(actual).lower() != subval.lower():
                            continue
                    else:
                        continue
                else:
                    if e.get("type") != event_type:
                        continue
                # Same field checks
                all_match = True
                for field in same_fields:
                    if e.get(field) != event.get(field) and e.get("data", {}).get(field) != event.get("data", {}).get(field):
                        all_match = False
                        break
                if all_match:
                    count += 1

        ops = {">=": count >= threshold, ">": count > threshold,
               "==": count == threshold, "<=": count <= threshold, "<": count < threshold}
        return ops.get(op, False)

    def _fire_reaction(self, rule: dict, trigger_event: dict):
        """Execute a reaction rule's action."""
        action = rule.get("action", "broadcast")
        message_template = rule.get("message", {})

        # Template substitution: replace {field} with trigger event values.
        #
        # A placeholder that resolves to NOTHING must not silently become an
        # empty string. `obj.get(p, "")` did exactly that, and the result
        # reached subscribers: the convergent_signal rule templates
        # {data.pair} and {data.direction} from a SIGNAL event that carries
        # NEITHER (reaction-generated SIGNALs have only type/reason/
        # suggested_action), so it emitted "Fleet convergence on  " with an
        # empty pair and empty direction 184 times a day, and the broadcaster
        # rendered each as a HIGH CONVICTION SIGNAL card.
        #
        # A reaction whose own message cannot be filled in is not a reaction
        # worth firing. Skip it and say so once per rule, rather than paging
        # subscribers with a hollow card.
        message = {}
        _unresolved = set()
        for k, v in message_template.items():
            if isinstance(v, str) and "{" in v:
                try:
                    # Simple template: {pair}, {direction}, {data.pair}, {count}
                    result = v
                    for match in set(__import__("re").findall(r"\{([^}]+)\}", v)):
                        parts = match.split(".")
                        obj = trigger_event
                        _missing = False
                        for p in parts:
                            if isinstance(obj, dict) and p in obj:
                                obj = obj[p]
                            else:
                                _missing = True
                                break
                        if _missing or obj is None or str(obj).strip() == "":
                            _unresolved.add(match)
                            obj = ""
                        result = result.replace(f"{{{match}}}", str(obj))
                    message[k] = result
                except Exception:
                    message[k] = v
            else:
                message[k] = v

        if _unresolved:
            _rn = rule.get("name", "unknown")
            if not hasattr(self, "_warned_unresolved"):
                self._warned_unresolved = set()
            if _rn not in self._warned_unresolved:
                self._warned_unresolved.add(_rn)
                log.warning(
                    "Reaction %r SKIPPED: its message templates %s could not "
                    "be resolved from the %s trigger — that event does not "
                    "carry those fields. Firing anyway would emit a message "
                    "with empty values. (warned once per rule)",
                    _rn, sorted(_unresolved), trigger_event.get("type"))
            return

        if action == "broadcast":
            # Publish the reaction as a new event (with depth tracking)
            reaction_event = {
                "source": "event_bus",
                "type": message.get("type", "REACTION"),
                "data": message,
                "triggered_by": rule.get("name", "unknown"),
                "original_event": trigger_event.get("id"),
                "_reaction_depth": trigger_event.get("_reaction_depth", 0) + 1,
            }
            self.publish(reaction_event)

    # ── SSE Generator ──

    def sse_generator(self, sub: dict):
        """Yield SSE-formatted events for a subscriber. Use in HTTP handler."""
        while True:
            if sub["queue"]:
                event = sub["queue"].popleft()
                # Apply type filter if set
                if sub["filter"] and event.get("type") not in sub["filter"]:
                    continue
                data = json.dumps(event, default=str)
                yield f"data: {data}\n\n"
            else:
                # Heartbeat every 15 seconds to keep connection alive
                yield ": heartbeat\n\n"
                time.sleep(1)

    # ── Stats ──

    def stats(self) -> dict:
        """Return bus statistics."""
        with self._lock:
            return {
                "subscribers": len(self._subscribers),
                "total_events": len(self._history),
                "recent_events": len(self._recent_events),
                "reactions_loaded": len(self._reactions),
            }
