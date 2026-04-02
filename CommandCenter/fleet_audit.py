#!/usr/bin/env python3
"""
FLEET AUDIT — Trust Nothing Full System Diagnostic
====================================================
Performs a 6-phase audit of the entire 16-bot crypto trading fleet.
Outputs JSON + human-readable text reports.

Usage:  cd D:\\CommandCenter && python fleet_audit.py
"""

import importlib
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# HTTP helper — requests with urllib fallback
# ---------------------------------------------------------------------------

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False
    import urllib.request
    import urllib.error

CC_BASE = "http://127.0.0.1:9000"
OLLAMA_BASE = "http://127.0.0.1:9001"
PROBE_TIMEOUT = 5
CC_DIR = Path(r"D:\CommandCenter")
NEXUS_DIR = Path(r"D:\Nexus")

PROBE_PATHS = ["/api/snapshot", "/status", "/health", "/"]
TREKBOT_PATHS = ["/health", "/status", "/api/snapshot", "/"]

COUNCIL_MODULES = {
    "signal_aggregator":    {"class": "SignalAggregator",           "consumers": ["command_center.py"]},
    "signal_decomposition": {"class": "SignalDecomposition",        "consumers": ["command_center.py"]},
    "expectancy":           {"class": "ExpectancyTracker",          "consumers": ["command_center.py"]},
    "signal_decay":         {"class": "SignalDecay",                "consumers": ["command_center.py"]},
    "shannon":              {"class": "ShannonEngine",              "consumers": []},
    "boltzmann":            {"class": "BoltzmannEngine",            "consumers": []},
    "lorenz":               {"class": "LorenzEngine",              "consumers": []},
    "prigogine":            {"class": "PrigogineEngine",            "consumers": []},
    "thom":                 {"class": "ThomEngine",                 "consumers": []},
    "info_geometry":        {"class": "InformationGeometryEngine",  "consumers": []},
    "topology":             {"class": "TopologicalAnalyzer",        "consumers": []},
    "quantum_state":        {"class": "QuantumMarketState",         "consumers": []},
    "causal_flow":          {"class": "CausalFlowNetwork",          "consumers": []},
}

TRADING_BOTS = ["turtlesue", "nexusbrain", "gridzilla", "trekbot", "rubberband", "arbitrageur"]


# ---------------------------------------------------------------------------
# Safe HTTP
# ---------------------------------------------------------------------------

def http_get(url, timeout=PROBE_TIMEOUT):
    """GET url, return {ok, status, body, elapsed_ms, error}. Never raises."""
    t0 = time.time()
    try:
        if _HAS_REQUESTS:
            r = _requests.get(url, timeout=timeout)
            body = r.text[:2000]
            return {"ok": True, "status": r.status_code, "body": body,
                    "elapsed_ms": round((time.time() - t0) * 1000, 1), "error": None}
        else:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(2000).decode("utf-8", errors="replace")
                return {"ok": True, "status": resp.status, "body": body,
                        "elapsed_ms": round((time.time() - t0) * 1000, 1), "error": None}
    except Exception as e:
        return {"ok": False, "status": None, "body": None,
                "elapsed_ms": round((time.time() - t0) * 1000, 1), "error": str(e)[:300]}


def http_get_json(url, timeout=PROBE_TIMEOUT):
    """GET url expecting JSON. Returns {ok, data, status, elapsed_ms, error}."""
    result = http_get(url, timeout)
    if not result["ok"]:
        return {"ok": False, "data": None, "status": result["status"],
                "elapsed_ms": result["elapsed_ms"], "error": result["error"]}
    try:
        data = json.loads(result["body"]) if result["body"] else None
        return {"ok": True, "data": data, "status": result["status"],
                "elapsed_ms": result["elapsed_ms"], "error": None}
    except json.JSONDecodeError as e:
        return {"ok": True, "data": None, "status": result["status"],
                "elapsed_ms": result["elapsed_ms"], "error": f"JSON parse error: {e}"}


def http_get_json_full(url, timeout=PROBE_TIMEOUT):
    """GET url, read full body (not truncated) and parse JSON."""
    t0 = time.time()
    try:
        if _HAS_REQUESTS:
            r = _requests.get(url, timeout=timeout)
            data = r.json()
            return {"ok": True, "data": data, "status": r.status_code,
                    "elapsed_ms": round((time.time() - t0) * 1000, 1), "error": None}
        else:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                return {"ok": True, "data": data, "status": resp.status,
                        "elapsed_ms": round((time.time() - t0) * 1000, 1), "error": None}
    except Exception as e:
        return {"ok": False, "data": None, "status": None,
                "elapsed_ms": round((time.time() - t0) * 1000, 1), "error": str(e)[:300]}


def safe_read(filepath):
    """Read file, return content or None."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return None


def safe_json_read(filepath):
    """Read and parse JSON file, return dict or None."""
    content = safe_read(filepath)
    if content is None:
        return None
    try:
        return json.loads(content)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PHASE 1: HEARTBEAT
# ---------------------------------------------------------------------------

def phase_heartbeat(fleet_config):
    """Probe every expected endpoint. Returns phase result dict."""
    print("  Phase 1: HEARTBEAT — probing all endpoints...")
    t0 = time.time()

    bots = fleet_config.get("bots", {})
    targets = []
    for bot_id, info in bots.items():
        port = info.get("port")
        name = bot_id
        paths = TREKBOT_PATHS if bot_id == "trekbot" else PROBE_PATHS
        targets.append({"id": bot_id, "name": name, "port": port, "paths": paths})

    # Add CC itself
    targets.append({"id": "command_center", "name": "CommandCenter", "port": 9000,
                     "paths": ["/api/master", "/api/manifest", "/"]})

    def probe_one(target):
        port = target["port"]
        best = None
        for path in target["paths"]:
            url = f"http://127.0.0.1:{port}{path}"
            result = http_get(url, timeout=PROBE_TIMEOUT)
            if result["ok"]:
                return {
                    "id": target["id"], "name": target["name"], "port": port,
                    "alive": True, "endpoint_hit": path,
                    "response_code": result["status"],
                    "response_time_ms": result["elapsed_ms"],
                    "response_preview": (result["body"] or "")[:500],
                    "error": None,
                    "checked_at": datetime.now(timezone.utc).isoformat()
                }
            if best is None:
                best = result
        return {
            "id": target["id"], "name": target["name"], "port": port,
            "alive": False, "endpoint_hit": None,
            "response_code": None,
            "response_time_ms": (best or {}).get("elapsed_ms"),
            "response_preview": None,
            "error": (best or {}).get("error", "All probes failed"),
            "checked_at": datetime.now(timezone.utc).isoformat()
        }

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(probe_one, t): t for t in targets}
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except Exception as e:
                t = futures[f]
                results.append({"id": t["id"], "name": t["name"], "port": t["port"],
                                "alive": False, "error": str(e)})

    alive = sum(1 for r in results if r.get("alive"))
    total = len(results)
    elapsed = round((time.time() - t0) * 1000)
    print(f"    => {alive}/{total} alive ({elapsed}ms)")

    return {
        "phase": "heartbeat",
        "duration_ms": elapsed,
        "endpoints_total": total,
        "endpoints_alive": alive,
        "endpoints_dead": total - alive,
        "results": sorted(results, key=lambda r: r.get("port", 0))
    }


# ---------------------------------------------------------------------------
# PHASE 2: MANIFEST & MODULE WIRING
# ---------------------------------------------------------------------------

def phase_manifest_and_modules(fleet_config):
    """Verify CC manifest and module integration. Returns phase result dict."""
    print("  Phase 2: MANIFEST & MODULE WIRING...")
    t0 = time.time()

    # 2A: Pull manifest
    manifest_resp = http_get_json_full(f"{CC_BASE}/api/manifest", timeout=10)
    manifest = manifest_resp.get("data") if manifest_resp["ok"] else None

    # Read CC source for module analysis
    cc_source = safe_read(CC_DIR / "command_center.py") or ""
    nexus_source = safe_read(NEXUS_DIR / "nexus.py") or ""

    # 2B: Per-module wiring check
    module_results = {}
    for mod_name, mod_info in COUNCIL_MODULES.items():
        cls_name = mod_info["class"]
        mod_file = CC_DIR / f"{mod_name}.py"
        file_exists = mod_file.exists()
        line_count = 0
        if file_exists:
            try:
                line_count = len(open(mod_file, encoding="utf-8", errors="replace").readlines())
            except Exception:
                pass

        # Checkpoint 1: Import found
        import_locations = []
        # Check CC
        for i, line in enumerate(cc_source.splitlines(), 1):
            if f"from {mod_name} import" in line or f"import {mod_name}" in line:
                import_locations.append(f"command_center.py:{i}")
        # Check Nexus
        for i, line in enumerate(nexus_source.splitlines(), 1):
            if f"from {mod_name} import" in line or f"import {mod_name}" in line:
                import_locations.append(f"nexus.py:{i}")

        # Checkpoint 2: Instantiation
        instantiation_locations = []
        for i, line in enumerate(cc_source.splitlines(), 1):
            if f"{cls_name}(" in line and "class " not in line and "def " not in line:
                instantiation_locations.append(f"command_center.py:{i}")
        for i, line in enumerate(nexus_source.splitlines(), 1):
            if f"{cls_name}(" in line and "class " not in line and "def " not in line:
                instantiation_locations.append(f"nexus.py:{i}")

        # Checkpoint 3: Runtime calls — look for instance usage
        # Search for any variable containing the module name followed by a method call
        call_locations = []
        # Build multiple search patterns for CC:
        #   _signal_aggregator.  _expectancy_tracker.  _signal_decay.  etc.
        cc_call_patterns = [f"_{mod_name}.", f"_{mod_name} "]
        # Also search for patterns like _expectancy_tracker (variable names containing module name)
        for i, line in enumerate(cc_source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "import" in stripped:
                continue
            for pat in cc_call_patterns:
                if pat in line:
                    call_locations.append(f"command_center.py:{i}")
                    break
            else:
                # Catch variable names like _expectancy_tracker that contain the module name
                if f"_{mod_name}_" in line and "import" not in line:
                    call_locations.append(f"command_center.py:{i}")
        # For Nexus, check BOTH self.<attr>. AND module-level _<name>. patterns
        # Nexus uses module-level globals: _info_geo, _topology, _quantum, _causal,
        # _shannon, _boltzmann, _lorenz, _prigogine, _thom
        nexus_patterns = [f"self.{mod_name}", f"self._{mod_name}",
                          cls_name[0].lower() + cls_name[1:],
                          f"_{mod_name}.", f"_{mod_name}("]
        # Also check shortened variable names used in Nexus
        short_aliases = {
            "info_geometry": ["_info_geo"],
            "causal_flow": ["_causal"],
            "quantum_state": ["_quantum"],
        }
        for alias in short_aliases.get(mod_name, []):
            nexus_patterns.append(f"{alias}.")
            nexus_patterns.append(f"{alias}(")
        for i, line in enumerate(nexus_source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "import" in stripped:
                continue
            for pat in nexus_patterns:
                if pat in line:
                    call_locations.append(f"nexus.py:{i}")
                    break

        # Runtime import test
        import_ok = False
        import_error = None
        if file_exists:
            try:
                sys.path.insert(0, str(CC_DIR))
                mod = importlib.import_module(mod_name)
                cls = getattr(mod, cls_name)
                methods = [m for m in dir(cls) if not m.startswith("_")]
                import_ok = True
            except Exception as e:
                import_error = str(e)[:200]
            finally:
                if str(CC_DIR) in sys.path:
                    sys.path.remove(str(CC_DIR))

        # Manifest claim
        manifest_status = None
        if manifest and "modules" in manifest:
            manifest_status = manifest["modules"].get(mod_name)

        # Event bus ground truth — check if this engine is producing events
        engine_event_map = {
            "info_geometry": "MANIFOLD_WARNING",
            "topology": "CYCLE_DETECTED",
            "quantum_state": "QUANTUM_COLLAPSE",
            "causal_flow": "CAUSAL_FLOW",
            "shannon": "SHANNON_ENTROPY",
            "boltzmann": "BOOK_PHASE",
            "lorenz": "CHAOS_STATE",
            "prigogine": "STRUCTURE_FORMING",
            "thom": "CATASTROPHE_WARNING",
        }
        bus_event_count = 0
        expected_event = engine_event_map.get(mod_name)
        if expected_event and manifest and "event_bus" in manifest:
            # Check recent events for this type
            events_resp = http_get_json_full(f"{CC_BASE}/api/events/recent?n=500", timeout=5)
            if events_resp["ok"] and events_resp["data"]:
                for evt in events_resp["data"]:
                    if evt.get("type") == expected_event:
                        bus_event_count += 1

        # Verdict — event bus overrides grep heuristics
        if not file_exists:
            verdict = "FILE MISSING"
        elif import_error:
            verdict = "IMPORT ERRORS"
        elif bus_event_count > 0:
            verdict = "PRODUCING"
        elif call_locations:
            verdict = "FULLY WIRED"
        elif instantiation_locations and (import_locations or manifest_status):
            # Imported + instantiated in a consumer = likely wired, grep may have missed calls
            verdict = "WIRED (no events yet)"
        elif instantiation_locations:
            verdict = "INSTANTIATED BUT NOT CALLED"
        elif import_locations:
            verdict = "IMPORTED BUT NOT CALLED"
        else:
            verdict = "FILE EXISTS ONLY"

        module_results[mod_name] = {
            "file_exists": file_exists,
            "file_path": str(mod_file),
            "line_count": line_count,
            "class_name": cls_name,
            "import_found_in": import_locations,
            "instantiation_found_in": instantiation_locations,
            "runtime_calls_found_in": call_locations[:5],  # Cap for readability
            "runtime_call_count": len(call_locations),
            "runtime_import_ok": import_ok,
            "import_error": import_error,
            "manifest_claims": manifest_status,
            "nexus_imports": any("nexus.py" in loc for loc in import_locations),
            "verdict": verdict,
        }

    # 2C: Nexus cross-check
    nexus_council = {}
    for mod_name in ["info_geometry", "topology", "quantum_state", "causal_flow"]:
        imported = any("nexus.py" in loc for loc in module_results.get(mod_name, {}).get("import_found_in", []))
        nexus_council[mod_name] = imported

    # 2D: Orphan scan — all .py files in CC
    known_py = set(COUNCIL_MODULES.keys()) | {
        "command_center", "event_bus", "event_publisher", "bus_listener",
        "collector", "fleet_logger", "analyze", "evolution", "ultron",
        "weekly_analysis", "inference_server", "launch_fleet",
        "portfolio_client", "standards", "fleet_audit", "fleet_intel_score",
    }
    all_py = []
    unregistered = []
    for f in sorted(CC_DIR.glob("*.py")):
        stem = f.stem
        lc = 0
        try:
            lc = len(open(f, encoding="utf-8", errors="replace").readlines())
        except Exception:
            pass
        imported_by_cc = f"from {stem} import" in cc_source or f"import {stem}" in cc_source
        entry = {"file": f.name, "stem": stem, "lines": lc, "imported_by_cc": imported_by_cc}
        all_py.append(entry)
        if stem not in known_py:
            unregistered.append(entry)

    elapsed = round((time.time() - t0) * 1000)
    print(f"    => {sum(1 for m in module_results.values() if m['verdict'] == 'FULLY WIRED')}/{len(COUNCIL_MODULES)} fully wired ({elapsed}ms)")

    return {
        "phase": "manifest_and_modules",
        "duration_ms": elapsed,
        "manifest_from_cc": manifest,
        "module_wiring": module_results,
        "nexus_council_imports": nexus_council,
        "all_py_files": all_py,
        "unregistered_py_files": unregistered,
    }


# ---------------------------------------------------------------------------
# PHASE 3: MONEY AUDIT
# ---------------------------------------------------------------------------

def phase_money_audit(fleet_config, heartbeat_results):
    """Audit portfolio, expectancy, cross-reference. Returns phase result dict."""
    print("  Phase 3: MONEY AUDIT...")
    t0 = time.time()

    # 3A: Portfolio from CC
    portfolio_resp = http_get_json_full(f"{CC_BASE}/api/portfolio", timeout=10)
    portfolio = portfolio_resp.get("data") if portfolio_resp["ok"] else None

    master_resp = http_get_json_full(f"{CC_BASE}/api/master", timeout=10)
    master = master_resp.get("data") if master_resp["ok"] else None

    # 3B: Fleet expectancy
    fleet_exp_resp = http_get_json_full(f"{CC_BASE}/api/expectancy", timeout=10)
    fleet_exp = fleet_exp_resp.get("data") if fleet_exp_resp["ok"] else None

    bot_exp = {}
    for bot_id in TRADING_BOTS:
        resp = http_get_json_full(f"{CC_BASE}/api/expectancy/{bot_id}", timeout=5)
        bot_exp[bot_id] = resp.get("data") if resp["ok"] else {"error": resp.get("error")}

    # 3C: Cross-reference — CC reservations vs bot self-reports
    discrepancies = []
    if portfolio and "by_bot" in portfolio:
        for bot_id, bot_data in portfolio["by_bot"].items():
            cc_positions = bot_data.get("positions", 0)
            cc_amount = bot_data.get("amount", 0)
            cc_pairs = bot_data.get("pairs", [])

            # Find this bot's heartbeat data
            hb = None
            if heartbeat_results:
                for r in heartbeat_results.get("results", []):
                    if r.get("id") == bot_id:
                        hb = r
                        break

            if not hb or not hb.get("alive"):
                discrepancies.append({
                    "bot": bot_id, "type": "BOT_DEAD_WITH_RESERVATIONS",
                    "detail": f"CC has ${cc_amount:.2f} reserved across {cc_positions} positions, but bot is not responding"
                })
                continue

            # Try to get bot's own position count from its snapshot
            port = fleet_config.get("bots", {}).get(bot_id, {}).get("port")
            if port:
                if bot_id == "trekbot":
                    bot_snap = http_get_json_full(f"http://127.0.0.1:{port}/positions", timeout=5)
                else:
                    bot_snap = http_get_json_full(f"http://127.0.0.1:{port}/api/snapshot", timeout=5)
                if bot_snap["ok"] and bot_snap["data"]:
                    bd = bot_snap["data"]
                    # Try to extract position count from bot's own report
                    bot_pos = None
                    if "open_positions" in bd:
                        op = bd["open_positions"]
                        if isinstance(op, list):
                            bot_pos = len(op)
                        elif isinstance(op, (int, float)):
                            bot_pos = int(op)
                    elif "positions" in bd and isinstance(bd["positions"], list):
                        bot_pos = len(bd["positions"])
                    elif "n_active_grids" in bd:
                        bot_pos = bd["n_active_grids"]
                    elif "active_grids" in bd and isinstance(bd["active_grids"], dict):
                        bot_pos = len(bd["active_grids"])

                    if bot_pos is not None and bot_pos != cc_positions:
                        discrepancies.append({
                            "bot": bot_id, "type": "POSITION_COUNT_MISMATCH",
                            "detail": f"CC says {cc_positions} reservations, bot self-reports {bot_pos} positions"
                        })

    # 3D: Fleet exposure
    exposure_resp = http_get_json_full(f"{CC_BASE}/api/fleet/exposure", timeout=10)
    exposure = exposure_resp.get("data") if exposure_resp["ok"] else None

    # Concentration flags
    concentration_flags = []
    if portfolio:
        deployed = portfolio.get("deployed", 0)
        if deployed > 0:
            for pair, pair_data in portfolio.get("by_pair", {}).items():
                pair_amt = pair_data.get("amount", 0)
                pct = (pair_amt / deployed) * 100
                if pct > 25:
                    concentration_flags.append(f"{pair} = ${pair_amt:.0f} ({pct:.1f}% of deployed)")

        # Limit violations
        total = portfolio.get("total", 10000)
        limits = portfolio.get("limits", {})
        deployed_pct = portfolio.get("deployed_pct", 0)
        if deployed_pct > limits.get("max_deployed_pct", 80):
            concentration_flags.append(f"DEPLOYED {deployed_pct}% > {limits.get('max_deployed_pct', 80)}% limit")

        for bot_id, bot_data in portfolio.get("by_bot", {}).items():
            bot_pct = (bot_data.get("amount", 0) / total) * 100 if total else 0
            if bot_pct > limits.get("max_per_bot_pct", 30):
                concentration_flags.append(f"{bot_id} at {bot_pct:.1f}% > {limits.get('max_per_bot_pct', 30)}% per-bot limit")

    elapsed = round((time.time() - t0) * 1000)
    print(f"    => Portfolio ${portfolio.get('total', '?') if portfolio else '?'}, {len(discrepancies)} discrepancies ({elapsed}ms)")

    return {
        "phase": "money_audit",
        "duration_ms": elapsed,
        "portfolio": portfolio,
        "fleet_expectancy": fleet_exp,
        "bot_expectancy": bot_exp,
        "discrepancies": discrepancies,
        "concentration_flags": concentration_flags,
        "exposure": exposure,
    }


# ---------------------------------------------------------------------------
# PHASE 4: DASHBOARD vs REALITY
# ---------------------------------------------------------------------------

def phase_dashboard():
    """Analyze dashboard HTML for data sources. Returns phase result dict."""
    print("  Phase 4: DASHBOARD vs REALITY...")
    t0 = time.time()

    html_path = CC_DIR / "command_center_v3.html"
    html = safe_read(html_path) or ""

    if not html:
        return {"phase": "dashboard_vs_reality", "error": "Could not read command_center_v3.html",
                "duration_ms": round((time.time() - t0) * 1000)}

    # Find all fetch() calls
    fetch_calls = []
    for i, line in enumerate(html.splitlines(), 1):
        matches = re.findall(r'fetch\s*\(\s*["\']([^"\']+)["\']', line)
        for url in matches:
            # Try to find enclosing function
            fetch_calls.append({"url": url, "line": i})
    # Also find template literal fetches
    for i, line in enumerate(html.splitlines(), 1):
        matches = re.findall(r'fetch\s*\(\s*`([^`]+)`', line)
        for url in matches:
            fetch_calls.append({"url": url, "line": i, "template_literal": True})

    # Find EventSource usage
    sse_results = []
    for i, line in enumerate(html.splitlines(), 1):
        if "EventSource" in line or "evtSource" in line or "/events/stream" in line:
            sse_results.append({"line": i, "content": line.strip()[:200]})

    # Find setInterval calls
    intervals = []
    for i, line in enumerate(html.splitlines(), 1):
        match = re.search(r'setInterval\s*\(\s*(\w+)\s*,\s*(\d+)', line)
        if match:
            intervals.append({"function": match.group(1), "interval_ms": int(match.group(2)), "line": i})
        # Also match anonymous function intervals
        match2 = re.search(r'setInterval\s*\(\s*(?:function|\(\)|=>).*?,\s*(\d+)', line)
        if match2 and not match:
            intervals.append({"function": "(anonymous)", "interval_ms": int(match2.group(1)), "line": i})

    # Find polling constants
    poll_constants = []
    for i, line in enumerate(html.splitlines(), 1):
        match = re.search(r'(?:POLL_MS|REFRESH|INTERVAL|poll_interval|refreshInterval)\s*[=:]\s*(\d+)', line, re.IGNORECASE)
        if match:
            poll_constants.append({"name": match.group(0).split("=")[0].split(":")[0].strip(),
                                   "value_ms": int(match.group(1)), "line": i})

    # Read CC route table for cross-reference
    cc_source = safe_read(CC_DIR / "command_center.py") or ""
    routes = []
    for i, line in enumerate(cc_source.splitlines(), 1):
        match = re.search(r'"(/api/[^"]+)"', line)
        if match and ("_serve" in line or "_handle" in line or "routes" in line.lower()):
            routes.append(match.group(1))
    routes = sorted(set(routes))

    # Cross reference
    fetch_urls = set()
    for fc in fetch_calls:
        url = fc["url"]
        # Strip query params and template vars
        url = re.sub(r'\?.*', '', url)
        url = re.sub(r'\$\{[^}]+\}', '*', url)
        fetch_urls.add(url)

    routes_set = set(routes)
    dashboard_only = [u for u in sorted(fetch_urls) if u.startswith("/api/") and u not in routes_set
                      and not any(u.startswith(r.rstrip("/")) for r in routes_set)]
    routes_not_fetched = [r for r in routes if r not in fetch_urls
                          and not any(fu.startswith(r.rstrip("/")) for fu in fetch_urls)]

    uses_sse = len(sse_results) > 0
    uses_polling = len(intervals) > 0

    elapsed = round((time.time() - t0) * 1000)
    print(f"    => {len(fetch_calls)} fetch calls, SSE={'YES' if uses_sse else 'NO'}, {len(intervals)} intervals ({elapsed}ms)")

    return {
        "phase": "dashboard_vs_reality",
        "duration_ms": elapsed,
        "html_lines": len(html.splitlines()),
        "fetch_calls": fetch_calls,
        "fetch_call_count": len(fetch_calls),
        "sse_usage": sse_results,
        "uses_sse": uses_sse,
        "uses_polling": uses_polling,
        "polling_intervals": intervals,
        "poll_constants": poll_constants,
        "cc_routes": routes,
        "dashboard_fetches_not_in_routes": dashboard_only,
        "routes_not_fetched_by_dashboard": routes_not_fetched,
    }


# ---------------------------------------------------------------------------
# PHASE 5: EVENT BUS HEALTH
# ---------------------------------------------------------------------------

def phase_event_bus():
    """Audit event bus health. Returns phase result dict."""
    print("  Phase 5: EVENT BUS HEALTH...")
    t0 = time.time()

    # 5A: Bus stats (from manifest)
    manifest_resp = http_get_json_full(f"{CC_BASE}/api/manifest", timeout=10)
    manifest = manifest_resp.get("data") if manifest_resp["ok"] else None
    bus_stats = manifest.get("event_bus") if manifest else None

    # 5B: Recent events
    events_resp = http_get_json_full(f"{CC_BASE}/api/events/recent?n=200", timeout=10)
    recent_events = events_resp.get("data") if events_resp["ok"] else None

    event_summary = {"count": 0, "by_type": {}, "by_source": {},
                     "newest_age_s": None, "oldest_age_s": None}
    if recent_events and isinstance(recent_events, list):
        now = time.time()
        event_summary["count"] = len(recent_events)
        for evt in recent_events:
            etype = evt.get("type", "UNKNOWN")
            esrc = evt.get("source", "unknown")
            event_summary["by_type"][etype] = event_summary["by_type"].get(etype, 0) + 1
            event_summary["by_source"][esrc] = event_summary["by_source"].get(esrc, 0) + 1

        timestamps = [e.get("timestamp", 0) for e in recent_events if e.get("timestamp")]
        if timestamps:
            event_summary["newest_age_s"] = round(now - max(timestamps), 1)
            event_summary["oldest_age_s"] = round(now - min(timestamps), 1)

    # 5C: Reaction rules from file
    reactions = safe_json_read(CC_DIR / "reactions.json")
    reaction_rules = []
    if reactions and "rules" in reactions:
        for rule in reactions["rules"]:
            reaction_rules.append({
                "name": rule.get("name"),
                "trigger_type": rule.get("trigger", {}).get("type"),
                "condition": rule.get("condition"),
                "action": rule.get("action"),
                "message_type": rule.get("message", {}).get("type"),
            })

    # Check which rules have matching events
    event_types_seen = set(event_summary.get("by_type", {}).keys())
    dormant_rules = []
    active_rules = []
    for rule in reaction_rules:
        trigger = rule.get("trigger_type")
        if trigger and trigger in event_types_seen:
            active_rules.append(rule["name"])
        else:
            dormant_rules.append(rule["name"])

    # 5D: Signal flow
    signals_decide = http_get_json_full(f"{CC_BASE}/api/signals/decide", timeout=5)
    signals_rankings = http_get_json_full(f"{CC_BASE}/api/signals/rankings", timeout=5)
    signals_decomp = http_get_json_full(f"{CC_BASE}/api/signals/decomposition", timeout=5)
    signals_decay = http_get_json_full(f"{CC_BASE}/api/signals/decay", timeout=5)

    signal_flow_status = "EMPTY"
    for resp in [signals_rankings, signals_decomp, signals_decay]:
        if resp["ok"] and resp["data"]:
            data = resp["data"]
            if isinstance(data, dict) and any(v for v in data.values() if v):
                signal_flow_status = "ACTIVE"
                break
            elif isinstance(data, list) and len(data) > 0:
                signal_flow_status = "ACTIVE"
                break
    if signal_flow_status == "EMPTY":
        # Check if there's at least some data
        for resp in [signals_rankings, signals_decomp, signals_decay]:
            if resp["ok"] and resp["data"] is not None:
                signal_flow_status = "STALE"
                break

    elapsed = round((time.time() - t0) * 1000)
    print(f"    => {event_summary['count']} events, {len(reaction_rules)} rules, signals={signal_flow_status} ({elapsed}ms)")

    return {
        "phase": "event_bus_health",
        "duration_ms": elapsed,
        "bus_stats": bus_stats,
        "recent_events": event_summary,
        "reaction_rules_count": len(reaction_rules),
        "reaction_rules": reaction_rules,
        "active_rules": active_rules,
        "dormant_rules": dormant_rules,
        "signal_flow": {
            "status": signal_flow_status,
            "decide": signals_decide.get("data") if signals_decide["ok"] else {"error": signals_decide.get("error")},
            "rankings": signals_rankings.get("data") if signals_rankings["ok"] else {"error": signals_rankings.get("error")},
            "decomposition": signals_decomp.get("data") if signals_decomp["ok"] else {"error": signals_decomp.get("error")},
            "decay": signals_decay.get("data") if signals_decay["ok"] else {"error": signals_decay.get("error")},
        },
    }


# ---------------------------------------------------------------------------
# PHASE 6: BOT-BY-BOT DEEP SCAN
# ---------------------------------------------------------------------------

def phase_bot_deep_scan(fleet_config, heartbeat_results):
    """Deep scan each bot directory. Returns phase result dict."""
    print("  Phase 6: BOT-BY-BOT DEEP SCAN...")
    t0 = time.time()

    bots = fleet_config.get("bots", {})
    cc_modules = ["event_publisher", "bus_listener", "portfolio_client", "standards",
                  "signal_aggregator", "signal_decomposition", "expectancy", "signal_decay",
                  "info_geometry", "topology", "quantum_state", "causal_flow",
                  "shannon", "boltzmann", "lorenz", "prigogine", "thom"]

    bot_profiles = []

    for bot_id, info in bots.items():
        bot_dir = Path(info.get("dir", ""))
        main_file = info.get("cmd", [""])[0] if info.get("cmd") else ""
        port = info.get("port")
        bot_type = info.get("type", "unknown")

        profile = {
            "id": bot_id,
            "name": bot_id,
            "port": port,
            "directory": str(bot_dir),
            "type": bot_type,
            "main_file": main_file,
            "main_exists": False,
            "main_file_lines": 0,
            "py_file_count": 0,
            "imports_from_cc": [],
            "log_files": [],
            "recent_errors": {"count": 0, "samples": []},
        }

        # Find heartbeat status
        if heartbeat_results:
            for r in heartbeat_results.get("results", []):
                if r.get("id") == bot_id:
                    profile["alive"] = r.get("alive", False)
                    profile["response_time_ms"] = r.get("response_time_ms")
                    break

        # Check directory exists
        if not bot_dir.exists():
            profile["error"] = f"Directory not found: {bot_dir}"
            bot_profiles.append(profile)
            continue

        # Main file
        main_path = bot_dir / main_file
        if main_path.exists():
            profile["main_exists"] = True
            try:
                lines = open(main_path, encoding="utf-8", errors="replace").readlines()
                profile["main_file_lines"] = len(lines)
                source = "".join(lines)

                # Check CC imports
                for mod in cc_modules:
                    if f"from {mod} import" in source or f"import {mod}" in source:
                        profile["imports_from_cc"].append(mod)
                # Also check sys.path CommandCenter
                if "CommandCenter" in source:
                    profile["imports_from_cc"].append("(sys.path to CommandCenter)")
            except Exception as e:
                profile["error"] = f"Could not read {main_file}: {e}"

        # Count all .py files
        try:
            py_files = list(bot_dir.glob("*.py"))
            profile["py_file_count"] = len(py_files)
        except Exception:
            pass

        # Check for log files
        try:
            log_files = list(bot_dir.glob("*.log"))
            profile["log_files"] = [f.name for f in log_files]

            # Scan last 200 lines of each log for errors
            error_count = 0
            error_samples = []
            for lf in log_files[:3]:  # Cap at 3 log files
                try:
                    with open(lf, encoding="utf-8", errors="replace") as fh:
                        tail = fh.readlines()[-200:]
                    for line in tail:
                        if "ERROR" in line or "Traceback" in line or "Exception" in line:
                            error_count += 1
                            if len(error_samples) < 5:
                                error_samples.append(line.strip()[:200])
                except Exception:
                    pass
            profile["recent_errors"] = {"count": error_count, "samples": error_samples}
        except Exception:
            pass

        bot_profiles.append(profile)

    elapsed = round((time.time() - t0) * 1000)
    print(f"    => {len(bot_profiles)} bots scanned ({elapsed}ms)")

    return {
        "phase": "bot_deep_scan",
        "duration_ms": elapsed,
        "bot_count": len(bot_profiles),
        "bots": bot_profiles,
    }


# ---------------------------------------------------------------------------
# REPORT GENERATION
# ---------------------------------------------------------------------------

def generate_text_report(report):
    """Generate human-readable text report from all phase data."""
    lines = []
    w = lines.append

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    w("=" * 60)
    w("FLEET AUDIT REPORT")
    w(now_str)
    w("=" * 60)
    w("")

    # --- HEARTBEAT ---
    hb = report.get("heartbeat", {})
    w(f"HEARTBEAT: {hb.get('endpoints_alive', '?')}/{hb.get('endpoints_total', '?')} endpoints responding")
    for r in hb.get("results", []):
        alive = r.get("alive", False)
        icon = "[OK]" if alive else "[DEAD]"
        name = r.get("name", r.get("id", "?"))
        port = r.get("port", "?")
        if alive:
            ms = r.get("response_time_ms", "?")
            path = r.get("endpoint_hit", "?")
            w(f"  {icon:6s} {name:<16s} :{port:<5}  {ms}ms  {path}")
        else:
            err = (r.get("error") or "unknown")[:60]
            w(f"  {icon:6s} {name:<16s} :{port:<5}  {err}")
    w("")

    # --- MODULE WIRING ---
    mw = report.get("manifest_and_modules", {})
    modules = mw.get("module_wiring", {})
    wired = sum(1 for m in modules.values() if m.get("verdict") == "FULLY WIRED")
    w(f"MODULE WIRING: {wired}/{len(modules)} fully wired")
    for name, info in sorted(modules.items(), key=lambda x: x[1].get("verdict", "")):
        verdict = info.get("verdict", "UNKNOWN")
        lc = info.get("line_count", 0)
        manifest_claim = info.get("manifest_claims")
        nexus = " (Nexus)" if info.get("nexus_imports") else ""
        claim_str = ""
        if manifest_claim is True and verdict != "FULLY WIRED":
            claim_str = " [MANIFEST SAYS TRUE!]"
        elif manifest_claim is False and verdict == "FULLY WIRED":
            claim_str = " [MANIFEST SAYS FALSE!]"

        if verdict in ("FULLY WIRED", "PRODUCING"):
            icon = "[OK]"
        elif verdict in ("WIRED (no events yet)",):
            icon = "[~OK]"
        elif verdict in ("IMPORTED BUT NOT CALLED", "INSTANTIATED BUT NOT CALLED"):
            icon = "[WARN]"
        else:
            icon = "[----]"

        consumers = ", ".join(info.get("import_found_in", []))[:60]
        w(f"  {icon:6s} {name:<26s} {lc:>5} lines  {verdict}{nexus}{claim_str}")
        if consumers:
            w(f"         consumers: {consumers}")
    w("")

    # Unregistered files
    unreg = mw.get("unregistered_py_files", [])
    if unreg:
        w("  UNREGISTERED .py FILES:")
        for f in unreg:
            w(f"    {f['file']:<30s} {f['lines']:>5} lines  imported_by_cc={f['imported_by_cc']}")
        w("")

    # --- MONEY ---
    money = report.get("money_audit", {})
    portfolio = money.get("portfolio") or {}
    w("MONEY:")
    w(f"  Pool:      ${portfolio.get('total', '?'):>10}")
    w(f"  Deployed:  ${portfolio.get('deployed', '?'):>10} ({portfolio.get('deployed_pct', '?')}%)")
    w(f"  Available: ${portfolio.get('available', '?'):>10}")
    w(f"  Risk Status: {portfolio.get('risk_status', '?')}")
    w(f"  Active Reservations: {portfolio.get('active_reservations', '?')}")
    w("")

    w("  Per-bot deployment:")
    for bot_id, bd in (portfolio.get("by_bot") or {}).items():
        amt = bd.get("amount", 0)
        pos = bd.get("positions", 0)
        pairs = ", ".join(bd.get("pairs", []))
        w(f"    {bot_id:<14s} ${amt:>10.2f}  ({pos} positions)  [{pairs}]")
    w("")

    w("  Direction split:")
    for direction, amt in (portfolio.get("by_direction") or {}).items():
        w(f"    {direction:<8s} ${amt:>10.2f}")
    w("")

    # Discrepancies
    discs = money.get("discrepancies", [])
    if discs:
        w("  DISCREPANCIES:")
        for d in discs:
            w(f"    [{d.get('type')}] {d.get('bot')}: {d.get('detail')}")
    else:
        w("  DISCREPANCIES: None found")
    w("")

    # Concentration
    conc = money.get("concentration_flags", [])
    if conc:
        w("  CONCENTRATION FLAGS:")
        for c in conc:
            w(f"    ! {c}")
    w("")

    # Expectancy
    fleet_exp = money.get("fleet_expectancy") or {}
    w("  EXPECTANCY:")
    w(f"    Fleet:       ${fleet_exp.get('fleet_expectancy', '?')}/trade ({fleet_exp.get('total_trades', '?')} trades, {fleet_exp.get('win_rate', '?')}% WR)")
    w(f"    Net PnL:     ${fleet_exp.get('total_net_pnl', '?')}")
    w(f"    Gross PnL:   ${fleet_exp.get('total_gross_pnl', '?')}")
    w(f"    Total Fees:  ${fleet_exp.get('total_fees', '?')}")
    w(f"    Fees/Gross:  {fleet_exp.get('fees_ate_pct', '?')}%")
    w("")

    bot_rankings = fleet_exp.get("bot_rankings", [])
    if bot_rankings:
        w("    Bot rankings (by expectancy):")
        for br in bot_rankings:
            w(f"      {br.get('bot', '?'):<14s} ${br.get('expectancy', '?'):>8}/trade  {br.get('trades', '?'):>4} trades  {br.get('verdict', '?')}")
    w("")

    # --- DASHBOARD ---
    dash = report.get("dashboard_vs_reality", {})
    w("DASHBOARD:")
    w(f"  HTML lines: {dash.get('html_lines', '?')}")
    w(f"  fetch() calls: {dash.get('fetch_call_count', '?')}")
    w(f"  Uses SSE: {'YES' if dash.get('uses_sse') else 'NO'}")
    w(f"  Uses polling: {'YES' if dash.get('uses_polling') else 'NO'}")
    w("")

    if dash.get("polling_intervals"):
        w("  Polling intervals:")
        for pi in dash["polling_intervals"]:
            w(f"    {pi.get('function', '?')}: {pi.get('interval_ms', '?')}ms (line {pi.get('line', '?')})")
    if dash.get("poll_constants"):
        w("  Poll constants:")
        for pc in dash["poll_constants"]:
            w(f"    {pc.get('name', '?')} = {pc.get('value_ms', '?')}ms (line {pc.get('line', '?')})")
    w("")

    if dash.get("sse_usage"):
        w("  SSE references:")
        for sse in dash["sse_usage"]:
            w(f"    line {sse.get('line', '?')}: {sse.get('content', '')[:100]}")
    w("")

    not_in_routes = dash.get("dashboard_fetches_not_in_routes", [])
    if not_in_routes:
        w("  Dashboard fetches NOT in CC route table:")
        for u in not_in_routes:
            w(f"    {u}")

    not_fetched = dash.get("routes_not_fetched_by_dashboard", [])
    if not_fetched:
        w("  CC routes NOT fetched by dashboard:")
        for u in not_fetched:
            w(f"    {u}")
    w("")

    # --- EVENT BUS ---
    bus = report.get("event_bus_health", {})
    bus_stats = bus.get("bus_stats") or {}
    evt = bus.get("recent_events", {})
    w("EVENT BUS:")
    w(f"  Total events this session: {bus_stats.get('total_events', '?')}")
    w(f"  SSE subscribers: {bus_stats.get('subscribers', '?')}")
    w(f"  Reactions loaded: {bus_stats.get('reactions_loaded', '?')}")
    w(f"  Recent events sampled: {evt.get('count', '?')}")
    if evt.get("newest_age_s") is not None:
        w(f"  Newest event: {evt['newest_age_s']}s ago")
        w(f"  Oldest event: {evt['oldest_age_s']}s ago")
    w("")

    if evt.get("by_type"):
        w("  Events by type:")
        for etype, count in sorted(evt["by_type"].items(), key=lambda x: -x[1]):
            w(f"    {etype:<30s} {count:>4}")
    w("")

    if evt.get("by_source"):
        w("  Events by source:")
        for src, count in sorted(evt["by_source"].items(), key=lambda x: -x[1]):
            w(f"    {src:<20s} {count:>4}")
    w("")

    w(f"  Reaction rules: {bus.get('reaction_rules_count', '?')} total")
    active_r = bus.get("active_rules", [])
    dormant_r = bus.get("dormant_rules", [])
    if active_r:
        w(f"  Active (trigger seen):  {', '.join(active_r)}")
    if dormant_r:
        w(f"  Dormant (no trigger):   {', '.join(dormant_r)}")
    w("")

    sig = bus.get("signal_flow", {})
    w(f"  Signal flow: {sig.get('status', '?')}")
    w("")

    # --- BOT-BY-BOT ---
    bots_phase = report.get("bot_deep_scan", {})
    w("BOT-BY-BOT DEEP SCAN:")
    w(f"  {'Name':<16s} {'Port':>5}  {'Alive':>5}  {'Type':<10s} {'Lines':>6}  {'CC Imports':>10}  {'Logs':>5}  {'Errors':>6}")
    w("  " + "-" * 85)
    for bp in bots_phase.get("bots", []):
        bname = bp.get("id", "?")
        bport = bp.get("port", "?")
        balive = "YES" if bp.get("alive") else "NO"
        btype = bp.get("type", "?")
        blines = bp.get("main_file_lines", 0)
        cc_imp = len(bp.get("imports_from_cc", []))
        blogs = len(bp.get("log_files", []))
        errs = bp.get("recent_errors", {}).get("count", 0)
        w(f"  {bname:<16s} {bport:>5}  {balive:>5}  {btype:<10s} {blines:>6}  {cc_imp:>10}  {blogs:>5}  {errs:>6}")
    w("")

    # Detail per bot
    for bp in bots_phase.get("bots", []):
        cc_imports = bp.get("imports_from_cc", [])
        if cc_imports:
            w(f"  {bp['id']}: imports [{', '.join(cc_imports)}]")
        errors = bp.get("recent_errors", {})
        if errors.get("samples"):
            w(f"  {bp['id']}: {errors['count']} errors in logs:")
            for s in errors["samples"][:3]:
                w(f"    {s[:120]}")
    w("")

    # --- UNWIRED MODULES ---
    w("UNWIRED MODULES (action needed):")
    idx = 0
    for name, info in sorted(modules.items()):
        if info.get("verdict") not in ("FULLY WIRED", "PRODUCING", "WIRED (no events yet)"):
            idx += 1
            lc = info.get("line_count", 0)
            nexus = " (Nexus imports it)" if info.get("nexus_imports") else ""
            w(f"  {idx}. {name:<26s} {info.get('class_name', '?')}, {lc} lines, {info.get('verdict')}{nexus}")
    if idx == 0:
        w("  None — all modules fully wired!")
    w("")

    # --- CRITICAL FINDINGS ---
    w("CRITICAL FINDINGS:")
    findings = []

    # Dead bots with money
    for d in discs:
        if d.get("type") == "BOT_DEAD_WITH_RESERVATIONS":
            findings.append(f"DEAD BOT WITH MONEY: {d['bot']} — {d['detail']}")

    # Position mismatches
    for d in discs:
        if d.get("type") == "POSITION_COUNT_MISMATCH":
            findings.append(f"POSITION MISMATCH: {d['bot']} — {d['detail']}")

    # Fee problem
    if fleet_exp.get("fees_ate_pct") and fleet_exp["fees_ate_pct"] > 100:
        findings.append(f"FEES DESTROYING PROFIT: Fees are {fleet_exp['fees_ate_pct']}% of gross PnL")

    # Negative expectancy
    if fleet_exp.get("fleet_expectancy") and fleet_exp["fleet_expectancy"] < 0:
        findings.append(f"NEGATIVE FLEET EXPECTANCY: ${fleet_exp['fleet_expectancy']}/trade across {fleet_exp.get('total_trades', '?')} trades")

    # Concentration
    for c in conc:
        findings.append(f"CONCENTRATION: {c}")

    # Manifest vs reality mismatches
    wired_verdicts = ("FULLY WIRED", "PRODUCING", "WIRED (no events yet)")
    for name, info in modules.items():
        mc = info.get("manifest_claims")
        v = info.get("verdict")
        if mc is True and v not in wired_verdicts:
            findings.append(f"MANIFEST LIE: {name} claims integrated=True but verdict is {v}")
        if mc is False and v in wired_verdicts:
            findings.append(f"MANIFEST UNDERCOUNT: {name} claims integrated=False but is {v}")

    # Dead bots
    for r in hb.get("results", []):
        if not r.get("alive") and r.get("id") != "command_center":
            findings.append(f"DEAD: {r.get('name', r.get('id'))} on port {r.get('port')}")

    if findings:
        for i, f in enumerate(findings, 1):
            w(f"  {i}. {f}")
    else:
        w("  None — fleet looking clean!")
    w("")

    w("=" * 60)
    w("END OF AUDIT")
    w("=" * 60)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON safety
# ---------------------------------------------------------------------------

def _json_default(obj):
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return 0.0
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  FLEET AUDIT — Trust Nothing Full System Diagnostic")
    print("=" * 60)
    print()

    total_start = time.time()

    # Load fleet config
    fleet_config = safe_json_read(CC_DIR / "fleet_config.json")
    if not fleet_config:
        print("  [!] Could not load fleet_config.json, using hardcoded defaults")
        fleet_config = {"bots": {}}

    report = {}
    errors = []

    # Phase 1
    try:
        report["heartbeat"] = phase_heartbeat(fleet_config)
    except Exception as e:
        print(f"  [!] Phase 1 crashed: {e}")
        errors.append(f"Phase 1: {traceback.format_exc()}")
        report["heartbeat"] = {"error": str(e)}

    # Phase 2
    try:
        report["manifest_and_modules"] = phase_manifest_and_modules(fleet_config)
    except Exception as e:
        print(f"  [!] Phase 2 crashed: {e}")
        errors.append(f"Phase 2: {traceback.format_exc()}")
        report["manifest_and_modules"] = {"error": str(e)}

    # Phase 3
    try:
        report["money_audit"] = phase_money_audit(fleet_config, report.get("heartbeat"))
    except Exception as e:
        print(f"  [!] Phase 3 crashed: {e}")
        errors.append(f"Phase 3: {traceback.format_exc()}")
        report["money_audit"] = {"error": str(e)}

    # Phase 4
    try:
        report["dashboard_vs_reality"] = phase_dashboard()
    except Exception as e:
        print(f"  [!] Phase 4 crashed: {e}")
        errors.append(f"Phase 4: {traceback.format_exc()}")
        report["dashboard_vs_reality"] = {"error": str(e)}

    # Phase 5
    try:
        report["event_bus_health"] = phase_event_bus()
    except Exception as e:
        print(f"  [!] Phase 5 crashed: {e}")
        errors.append(f"Phase 5: {traceback.format_exc()}")
        report["event_bus_health"] = {"error": str(e)}

    # Phase 6
    try:
        report["bot_deep_scan"] = phase_bot_deep_scan(fleet_config, report.get("heartbeat"))
    except Exception as e:
        print(f"  [!] Phase 6 crashed: {e}")
        errors.append(f"Phase 6: {traceback.format_exc()}")
        report["bot_deep_scan"] = {"error": str(e)}

    total_elapsed = round((time.time() - total_start) * 1000)
    report["_meta"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_duration_ms": total_elapsed,
        "errors": errors,
    }

    # Generate filenames
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = CC_DIR / f"audit_report_{ts}.json"
    txt_path = CC_DIR / f"audit_report_{ts}.txt"

    # Write JSON
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=_json_default)
        print(f"\n  JSON report: {json_path}")
    except Exception as e:
        print(f"  [!] Failed to write JSON: {e}")

    # Write text
    try:
        txt = generate_text_report(report)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"  Text report: {txt_path}")
        print(f"\n  Total time: {total_elapsed}ms")
        print()
        print(txt)
    except Exception as e:
        print(f"  [!] Failed to write text report: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()
