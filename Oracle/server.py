#!/usr/bin/env python3
"""
ORACLE HTTP Wrapper — Bot #7
==============================
Lightweight HTTP server wrapping Oracle's CLI scanner.
Runs oracle.py --json periodically, caches results, serves on port 8075.

Usage: python server.py
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Fleet event bus integration
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "CommandCenter"))
try:
    from event_publisher import EventPublisher
except ImportError:
    EventPublisher = None

try:
    from fleet_config import is_blacklisted as _is_blacklisted
except ImportError:
    _is_blacklisted = lambda pair: False

PORT = 8075
SCAN_INTERVAL = 300  # 5 minutes
ORACLE_DIR = os.path.dirname(os.path.abspath(__file__))
ORACLE_SCRIPT = os.path.join(ORACLE_DIR, "src", "oracle.py")
REPORTS_DIR = os.path.join(ORACLE_DIR, "reports")

# Strategy → direction mapping (from Oracle strategy engine)
_BULLISH_STRATEGIES = {
    "STRONG LONG", "TREND CONTINUATION", "PULLBACK BUY", "DIVERGENCE BUY",
    "DEEP VALUE", "BREAKOUT", "MOMENTUM SURGE", "SWING LONG",
}
_BEARISH_STRATEGIES = {"FALLING KNIFE", "DISTRIBUTION", "TAKE PROFIT"}


def _strategy_direction(strategy):
    s = (strategy or "").upper().strip()
    if s in _BULLISH_STRATEGIES:
        return "LONG"
    if s in _BEARISH_STRATEGIES:
        return "SHORT"
    return "NEUTRAL"


# ── State ────────────────────────────────────────────────────

_lock = threading.Lock()
_state = {
    "status": "starting",
    "scan_json": None,
    "last_scan_time": 0,
    "next_scan_time": 0,
    "scan_duration_s": 0,
    "scan_count": 0,
    "scanning": False,
    "error": None,
}

# Fleet event bus
_event_pub = EventPublisher("http://127.0.0.1:9000", "oracle") if EventPublisher else None
_last_regime = None


# ── Scanner ──────────────────────────────────────────────────

def _run_scan():
    """Run oracle.py --json, parse output, cache results."""
    with _lock:
        if _state["scanning"]:
            return  # skip if already running
        _state["scanning"] = True
        _state["status"] = "scanning"

    t0 = time.time()
    error = None
    scan_data = None

    try:
        # Run JSON scan
        result = subprocess.run(
            [sys.executable, ORACLE_SCRIPT, "--json"],
            capture_output=True, text=True, timeout=120,
            cwd=ORACLE_DIR,
        )

        if result.returncode == 0 and result.stdout.strip():
            # Oracle outputs ANSI + JSON. Find the JSON object in stdout.
            stdout = result.stdout
            # The JSON is typically the last block -- find the outermost { }
            json_start = stdout.rfind('\n{')
            if json_start == -1:
                json_start = stdout.find('{')
            if json_start >= 0:
                json_str = stdout[json_start:].strip()
                # Find matching closing brace
                depth = 0
                end = -1
                for i, ch in enumerate(json_str):
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                if end > 0:
                    scan_data = json.loads(json_str[:end])
        else:
            error = result.stderr[:200] if result.stderr else "No output"

    except subprocess.TimeoutExpired:
        error = "Scan timed out (120s limit)"
    except json.JSONDecodeError as e:
        error = f"JSON parse error: {e}"
    except Exception as e:
        error = str(e)

    duration = time.time() - t0

    # Also run HTML report (fire and forget)
    try:
        subprocess.Popen(
            [sys.executable, ORACLE_SCRIPT, "--html"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=ORACLE_DIR,
        )
    except Exception:
        pass

    with _lock:
        _state["scanning"] = False
        _state["scan_duration_s"] = round(duration, 1)
        _state["last_scan_time"] = time.time()
        _state["next_scan_time"] = time.time() + SCAN_INTERVAL
        _state["scan_count"] += 1
        if scan_data:
            _state["scan_json"] = scan_data
            _state["status"] = "running"
            _state["error"] = None
        elif error:
            _state["status"] = "error"
            _state["error"] = error
        else:
            _state["status"] = "running"

    # ── Fleet event bus publishing ──────────────────────────
    global _last_regime
    if _event_pub and scan_data:
        regime = scan_data.get("regime", "unknown")
        fg = scan_data.get("fear_greed")
        pairs_count = scan_data.get("pairs_analyzed", 0)
        ranked = scan_data.get("ranked", [])

        # Top signal for fleet = first non-blacklisted pair
        top_pair = None
        top_score = None
        for _r in ranked:
            _sym = _r.get("symbol", "")
            if not _is_blacklisted(_sym):
                top_pair = _sym
                top_score = _r.get("composite", 0)
                break

        try:
            _event_pub.emit("SCAN_COMPLETE", {
                "pairs_scanned": pairs_count,
                "duration_s": round(duration, 1),
                "regime": regime,
                "fear_greed": fg,
                "top_signal": {"pair": top_pair, "score": top_score} if top_pair else None,
            })
        except Exception:
            pass

        # Regime change detection
        if _last_regime is not None and _last_regime != regime:
            try:
                _event_pub.emit("REGIME_CHANGE", {
                    "from": _last_regime,
                    "to": regime,
                    "source": "oracle",
                })
            except Exception:
                pass
        _last_regime = regime


def _scan_loop():
    """Background scan loop."""
    # First scan immediately
    _run_scan()
    while True:
        time.sleep(SCAN_INTERVAL)
        _run_scan()


# ── Snapshot Builder ─────────────────────────────────────────

def _build_snapshot():
    """Build fleet-compatible snapshot from cached scan data."""
    with _lock:
        status = _state["status"]
        scan = _state["scan_json"]
        last_scan = _state["last_scan_time"]
        next_scan = _state["next_scan_time"]
        duration = _state["scan_duration_s"]
        count = _state["scan_count"]
        error = _state["error"]

    if status == "scanning" and scan is None:
        return {
            "timestamp": time.time(),
            "bot_name": "Oracle",
            "status": "scanning",
            "message": "Initial scan in progress...",
        }

    if scan is None:
        return {
            "timestamp": time.time(),
            "bot_name": "Oracle",
            "status": "error",
            "message": error or "No scan data available",
        }

    # Extract key fields from oracle.py --json output
    regime = scan.get("regime", "unknown")
    fear_greed = scan.get("fear_greed")
    pairs_analyzed = scan.get("pairs_analyzed", 0)
    scan_time = scan.get("scan_time", duration)
    ranked = scan.get("ranked", [])

    # Top 10 signals — scan all, flag blacklisted, filter from actionable list
    def _signal_row(r):
        pair = r.get("symbol", "")
        strategy = r.get("strategy", "")
        direction = _strategy_direction(strategy)
        # `score` is Oracle's conviction in the row's OWN direction, always
        # on the same 0-100 scale (Confluence gates score >= 55 for both
        # directions). The composite is a bullishness scale, so LONG rows use
        # it directly; SHORT rows use bear_composite — the mirrored
        # bearish-case score from src/scoring.compute_bear_composite. The
        # 100-composite fallback covers a cached scan_json from before
        # bear_composite existed; it is the closest honest approximation
        # available for such rows. (2026-07-30, short ban lifted)
        score = r.get("composite", 0)
        if direction == "SHORT":
            bc = r.get("bear_composite")
            score = bc if bc is not None else round(100 - (r.get("composite") or 50), 1)
        return {
            "pair": pair,
            "score": score,
            "composite": r.get("composite", 0),
            "bear_composite": r.get("bear_composite"),
            "strategy": strategy,
            "confidence": r.get("confidence", ""),
            "direction": direction,
            "entry": r.get("entry"),
            "stop": r.get("stop"),
            "target": r.get("target"),
            "rr": r.get("rr"),
            "hurst": r.get("hurst"),
            "mc_prob_up": r.get("mc_prob_up"),
            "order_flow": r.get("order_flow"),
            "blacklisted": _is_blacklisted(pair),
        }

    all_signals = [_signal_row(r) for r in ranked[:10]]

    # SHORT rows live at the BOTTOM of `ranked` (oracle.py sorts by the
    # bullish composite, descending), so ranked[:10] structurally never
    # surfaces them — even now that DISTRIBUTION carries real levels. Scan
    # the rest of the list for actionable bearish rows and append the
    # strongest few by bearish score, so downstream consumers see them
    # (Confluence anchors its SHORT candidates on these rows, and its
    # _price_for() quotes open positions from all_signals — a short must
    # appear here to stay quotable). Quality over quantity: most scans will
    # contribute zero. (2026-07-30, short ban lifted)
    seen_pairs = {s["pair"] for s in all_signals}
    short_rows = [_signal_row(r) for r in ranked[10:]
                  if _strategy_direction(r.get("strategy", "")) == "SHORT"]
    short_rows = [s for s in short_rows
                  if s["pair"] not in seen_pairs
                  and not s["blacklisted"]
                  and s["entry"] is not None
                  and s["stop"] is not None
                  and s["target"] is not None]
    short_rows.sort(key=lambda s: s["score"] or 0, reverse=True)
    all_signals.extend(short_rows[:5])

    # Actionable signals exclude blacklisted pairs AND anything without a
    # complete entry/stop/target. Non-directional strategies (WATCH, SWING
    # TRADE, TRAIL STOP, AVOID, ...) legitimately return None levels, but they
    # were still listed in top_signals, so the dashboard rendered them as
    # actionable rows with blank columns and Confluence's _price_for() could
    # read `entry: None` as a quote. top_signals is what trading bots consume;
    # it should contain only things that can actually be traded.
    # (2026-07-29 audit)
    def _actionable(s):
        return (not s["blacklisted"]
                and s.get("entry") is not None
                and s.get("stop") is not None
                and s.get("target") is not None)

    top_signals = [s for s in all_signals if _actionable(s)]

    return {
        "timestamp": time.time(),
        "bot_name": "Oracle",
        "status": status,
        "scan_duration_s": round(scan_time if scan_time else duration, 1),
        "last_scan_time": last_scan,
        "next_scan_time": next_scan,
        "scan_count": count,
        "pairs_scanned": pairs_analyzed,
        "regime": regime,
        "fear_greed": fear_greed,
        "top_signals": top_signals,
        "all_signals": all_signals,
        "signals_count": len(top_signals),
        "raw": scan,
    }


# ── HTTP Server ──────────────────────────────────────────────

class OracleHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.rstrip("/") or "/"

        if path == "/api/snapshot":
            snapshot = _build_snapshot()
            body = json.dumps(snapshot, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        elif path == "/" or path == "/index.html":
            # Serve latest HTML report
            self._serve_latest_report()

        elif path == "/health":
            with _lock:
                health = {"status": _state["status"], "scan_count": _state["scan_count"],
                          "last_scan": _state["last_scan_time"]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(health).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def _serve_latest_report(self):
        """Find and serve the most recent HTML report."""
        try:
            os.makedirs(REPORTS_DIR, exist_ok=True)
            reports = [f for f in os.listdir(REPORTS_DIR) if f.endswith(".html")]
            if not reports:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body style='background:#111;color:#ccc;font-family:monospace;padding:40px'>"
                                 b"<h1>Oracle</h1><p>No reports generated yet. First scan in progress...</p></body></html>")
                return

            # Sort by name (they contain timestamps) and pick the latest
            reports.sort(reverse=True)
            latest = os.path.join(REPORTS_DIR, reports[0])

            with open(latest, "r", encoding="utf-8") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Error: {e}".encode("utf-8"))


# ── Main ─────────────────────────────────────────────────────

def main():
    import sys as _sys
    _sys.path.insert(0, r"D:\CommandCenter")

    # SINGLE-INSTANCE MUTEX — bind the port before doing ANYTHING else.
    # HTTPServer defaults allow_reuse_address=True (SO_REUSEADDR), which on
    # Windows lets a second process bind an already-served port. Two watchdogs
    # (CC's _health_monitor + launch_fleet's monitor) can each respawn an
    # Oracle within seconds of each other, and the old port_guard.ensure_port()
    # call here made that fatal: each fresh spawn KILLED the healthy serving
    # instance to free the port, the other watchdog saw its child die and
    # respawned, and the fleet ran two Oracles indefinitely (observed
    # 2026-07-30). Exclusive bind inverts the rule: the incumbent wins, the
    # duplicate exits fast — before the scanner thread can publish anything.
    # Trade-off: a genuine zombie squatting the port at cold start now needs
    # a manual kill instead of being cleared automatically.
    class _ExclusiveServer(ThreadingHTTPServer):
        allow_reuse_address = False

    try:
        server = _ExclusiveServer(("0.0.0.0", PORT), OracleHandler)
    except OSError as e:
        print(f"[MUTEX] Port {PORT} already bound — another Oracle is serving; exiting. ({e})")
        return
    server.daemon_threads = True

    try:
        from port_guard import write_pidfile, cleanup_pidfile
        import atexit
        write_pidfile("oracle", PORT)
        atexit.register(cleanup_pidfile, "oracle")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    print()
    print("  +==========================================+")
    print("  |         ORACLE v1.0 -- Bot #7            |")
    print("  |         Intelligence Layer               |")
    print("  +==========================================+")
    print(f"  |  Port:     {PORT}                          |")
    print(f"  |  Scan:     every {SCAN_INTERVAL}s                    |")
    print("  |  Script:   src/oracle.py                 |")
    print(f"  |  Events:   {'connected' if _event_pub else 'disabled'}                     |")
    print("  +==========================================+")
    print()

    # Verify oracle.py exists
    if not os.path.exists(ORACLE_SCRIPT):
        print(f"  ERROR: {ORACLE_SCRIPT} not found")
        sys.exit(1)

    # Start scanner thread
    scanner = threading.Thread(target=_scan_loop, daemon=True)
    scanner.start()
    print("  Scanner started (first scan running...)")

    # HTTP server was already constructed (and the port bound) at the top of
    # main() as the single-instance mutex; ThreadingHTTPServer because the
    # plain HTTPServer serves one request at a time, so while this bot
    # computes, its port stops answering and Command Center's health check
    # reports it DOWN even though it is healthy.
    print(f"  Dashboard: http://localhost:{PORT}")
    print(f"  API:       http://localhost:{PORT}/api/snapshot")
    print("  Press Ctrl+C to stop")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Oracle stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
