#!/usr/bin/env python3
"""
FLEET RESTART — state-of-the-art cold-start for the CommandCenter fleet
=======================================================================
One command: reap everything stale, verify the machine is clean, bring the
fleet up, wait for it to actually be healthy, open the dashboard.

    python fleet_restart.py              # full reap + launch + browser
    python fleet_restart.py --status     # report only, change nothing
    python fleet_restart.py --stop       # reap only, do not relaunch
    python fleet_restart.py --no-browser # launch without opening the dashboard

Why this exists rather than a .bat that calls taskkill:

  1. The launcher runs DETACHED, so its own parent is gone and it looks
     exactly like an orphan to a naive `ppid is dead` check. Killing it is
     the one thing a restart script must never do by accident. We identify
     the launcher by command line, never by parentage.

  2. `taskkill` WITHOUT /F sends WM_CLOSE, which console Python ignores.
     It reports success, the process lives, and its children are silently
     orphaned. We ask nicely via CTRL_BREAK, wait, then escalate to /F.

  3. Command Center runs as a THREAD inside the launcher process, not as a
     subprocess. Killing the launcher kills CC and every CC-dependent bot
     with it. So shutdown order is: children first, launcher last.

  4. A port can stay in TIME_WAIT after its owner dies. Relaunching into a
     half-released port produces a bot that binds nothing and gets
     circuit-breakered. We poll until every port is actually free.

  5. Inference needs Ollama. If Ollama is not serving, Inference burns all
     3 launcher restarts in 60s, trips the circuit breaker, and cannot
     recover without manual intervention. So Ollama comes up FIRST.
"""

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CC_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
LAUNCH_LOG = os.path.join(CC_DIR, "logs", "fleet_launch.log")
LAUNCH_ERR = os.path.join(CC_DIR, "logs", "fleet_launch.err")

OLLAMA_EXE = os.path.expandvars(
    r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
OLLAMA_PROBE = "http://127.0.0.1:11434/api/tags"

GRACE_SECONDS = 6          # how long a process gets to exit politely
PORT_FREE_TIMEOUT = 30     # max wait for ports to release
CC_BOOT_TIMEOUT = 90       # max wait for Command Center to bind
FLEET_SETTLE_TIMEOUT = 120 # max wait for bots to report alive
HEALTHY_FRACTION = 0.85    # fraction of registered bots that must be alive

# Marker substrings that identify our own processes on the command line.
LAUNCHER_MARK = "launch_fleet.py"
THIS_SCRIPT = "fleet_restart.py"


# ── console output ────────────────────────────────────────────────

class C:
    OK = "\033[92m"; WARN = "\033[93m"; ERR = "\033[91m"
    DIM = "\033[90m"; BOLD = "\033[1m"; OFF = "\033[0m"


def _enable_ansi():
    """Windows consoles need VT processing turned on explicitly."""
    if sys.platform != "win32":
        return
    try:
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
    except Exception:
        for a in ("OK", "WARN", "ERR", "DIM", "BOLD", "OFF"):
            setattr(C, a, "")


def say(msg, tag="", color=""):
    stamp = time.strftime("%H:%M:%S")
    label = f"{color}{tag:<7}{C.OFF}" if tag else " " * 7
    print(f"  {C.DIM}{stamp}{C.OFF} {label} {msg}", flush=True)


def ok(m):    say(m, "OK", C.OK)
def warn(m):  say(m, "WARN", C.WARN)
def err(m):   say(m, "ERROR", C.ERR)
def step(m):  print(f"\n  {C.BOLD}{m}{C.OFF}", flush=True)


# ── fleet config ──────────────────────────────────────────────────

def load_fleet():
    """Read ports and bot ids from fleet_config so this never goes stale."""
    try:
        import fleet_config as fc
        bots = {bid: cfg for bid, cfg in fc.BOTS.items()}
        ports = sorted({c["port"] for c in bots.values() if c.get("port")}
                       | {fc.CC_PORT})
        return bots, ports, fc.CC_PORT
    except Exception as e:
        err(f"Cannot import fleet_config: {e}")
        sys.exit(2)


# ── process discovery ─────────────────────────────────────────────

def _wmic_processes():
    """Return [(pid, ppid, cmdline)] for python processes.

    Uses CIM via PowerShell — wmic is deprecated and absent on newer Windows.
    """
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or "
        "Name='pythonw.exe'\" | Select-Object ProcessId,ParentProcessId,"
        "CommandLine | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60).stdout.strip()
        if not out:
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        rows = []
        for d in data:
            rows.append((d.get("ProcessId"), d.get("ParentProcessId"),
                         d.get("CommandLine") or ""))
        return rows
    except Exception as e:
        warn(f"Process enumeration failed: {e}")
        return []


def _alive(pid):
    if not pid:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
        return str(pid) in out
    except Exception:
        return False


def survey(bots):
    """Classify every fleet-related python process.

    Returns (launchers, children, orphans, strays).
      launchers — launch_fleet.py processes (identified by CMDLINE, never
                  by parentage: a detached launcher has no live parent and
                  would otherwise be misread as an orphan)
      children  — processes whose parent is a live launcher
      orphans   — fleet bots whose parent is NOT alive (real orphans)
      strays    — bots we recognise but that are neither of the above
    """
    rows = _wmic_processes()
    me = os.getpid()

    launchers, children, orphans, strays = [], [], [], []
    bot_scripts = set()
    for cfg in bots.values():
        cmd = cfg.get("cmd") or []
        for tok in cmd:
            if isinstance(tok, str) and tok.endswith(".py"):
                bot_scripts.add(os.path.basename(tok).lower())

    launcher_pids = {p for p, _, c in rows
                     if LAUNCHER_MARK in c and p != me}

    for pid, ppid, cmd in rows:
        if pid == me or THIS_SCRIPT in cmd:
            continue
        low = cmd.lower()
        if "vivarium" in low:          # unrelated project on this machine
            continue

        if LAUNCHER_MARK in cmd:
            launchers.append((pid, ppid, cmd))
            continue

        is_bot = any(s in low for s in bot_scripts) or "inference_server" in low
        if not is_bot:
            continue

        if ppid in launcher_pids:
            children.append((pid, ppid, cmd))
        elif not _alive(ppid):
            orphans.append((pid, ppid, cmd))
        else:
            strays.append((pid, ppid, cmd))

    return launchers, children, orphans, strays


# ── termination ───────────────────────────────────────────────────

def _ctrl_break(pid):
    """Politely ask a console process to exit.

    `taskkill` without /F posts WM_CLOSE, which a console Python process does
    not handle — it reports SUCCESS while the process keeps running, and its
    children get silently orphaned. So we deliver a real CTRL_BREAK_EVENT via
    the Win32 console API, which Python surfaces as KeyboardInterrupt and the
    launcher handles in its shutdown path.

    Each bot is spawned with CREATE_NEW_PROCESS_GROUP, so the event is
    targeted at that group and does not leak back into this console.
    """
    if sys.platform != "win32":
        try:
            os.kill(pid, 2)  # SIGINT
            return True
        except Exception:
            return False
    try:
        k = ctypes.windll.kernel32
        # 1 = CTRL_BREAK_EVENT, deliverable to a specific process group.
        if k.GenerateConsoleCtrlEvent(1, pid):
            return True
    except Exception:
        pass
    # Fall back to the WM_CLOSE attempt — harmless, occasionally works for
    # processes that do pump a message loop.
    try:
        subprocess.run(["taskkill", "/PID", str(pid)],
                       capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _force(pid):
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def reap(launchers, children, orphans, strays):
    """Shut everything down, children before launchers.

    Order matters: Command Center is a thread inside the launcher process.
    Killing the launcher first takes CC down and strands every CC-dependent
    bot as an orphan — which is exactly the failure this script exists to
    clean up.
    """
    victims = children + orphans + strays
    if victims:
        step(f"Stopping {len(victims)} bot process(es)")
        for pid, _, cmd in victims:
            _ctrl_break(pid)
        deadline = time.time() + GRACE_SECONDS
        while time.time() < deadline:
            if not any(_alive(p) for p, _, _ in victims):
                break
            time.sleep(0.5)
        stubborn = [(p, c) for p, _, c in victims if _alive(p)]
        for pid, cmd in stubborn:
            _force(pid)
        ok(f"{len(victims) - len(stubborn)} exited cleanly, "
           f"{len(stubborn)} force-killed")
    else:
        say("No bot processes running", "", C.DIM)

    if launchers:
        step(f"Stopping {len(launchers)} launcher(s)")
        for pid, _, _ in launchers:
            _ctrl_break(pid)
        deadline = time.time() + GRACE_SECONDS
        while time.time() < deadline:
            if not any(_alive(p) for p, _, _ in launchers):
                break
            time.sleep(0.5)
        for pid, _, _ in launchers:
            if _alive(pid):
                _force(pid)
        ok("Launcher(s) stopped")


# ── ports ─────────────────────────────────────────────────────────

def port_busy(port, timeout=0.35):
    """True if something is LISTENING on the port (i.e. a live service)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(("127.0.0.1", port))
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def port_bindable(port):
    """True if a fresh server could actually claim this port.

    This is the question that matters before relaunching, and it is NOT the
    same as "can I connect". A port with lingering TIME_WAIT connections
    accepts a connect() while being perfectly bindable — connect-probing it
    reports a false 'still occupied' and stalls the restart for the full
    timeout. Conversely a port held by a live listener is not bindable.

    NO SO_REUSEADDR on the test socket: on Windows that option lets the
    bind SUCCEED against a live listener (shadow-bind) — this check then
    reported "all ports free" while the fleet was still running, and the
    restart booted a second fleet (2026-07-28). A plain bind fails against
    a live listener but still succeeds over TIME_WAIT leftovers on
    Windows, which is exactly the distinction this check needs.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def wait_ports_free(ports, timeout=PORT_FREE_TIMEOUT):
    """Wait until every fleet port can actually be bound.

    Tests bindability, not connectability: a port with leftover TIME_WAIT
    connections still answers connect() while being fine to bind, and
    connect-probing it stalls here for the full timeout on a false positive.
    """
    step("Waiting for ports to release")
    deadline = time.time() + timeout
    while time.time() < deadline:
        blocked = [p for p in ports if not port_bindable(p)]
        if not blocked:
            ok(f"All {len(ports)} ports bindable")
            return True
        time.sleep(1)
    blocked = [p for p in ports if not port_bindable(p)]
    warn(f"Still held after {timeout}s: {blocked}")
    for p in blocked:
        warn(f"  :{p} has a live listener")
    return False


# ── ollama ────────────────────────────────────────────────────────

def ensure_ollama():
    """Inference (:9001) dies immediately without Ollama and burns all three
    launcher restarts inside a minute, tripping its circuit breaker for good.
    Start Ollama BEFORE the fleet so that never happens."""
    step("Checking Ollama (required by Inference)")
    try:
        urllib.request.urlopen(OLLAMA_PROBE, timeout=3)
        ok("Ollama already serving on :11434")
        return True
    except Exception:
        pass

    if not os.path.exists(OLLAMA_EXE):
        warn(f"Ollama not found at {OLLAMA_EXE}")
        warn("Inference will fail to start — fleet continues without it")
        return False

    say("Starting ollama serve...")
    try:
        subprocess.Popen([OLLAMA_EXE, "serve"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW
                         if sys.platform == "win32" else 0)
    except Exception as e:
        warn(f"Could not start Ollama: {e}")
        return False

    for _ in range(20):
        try:
            urllib.request.urlopen(OLLAMA_PROBE, timeout=2)
            ok("Ollama up")
            return True
        except Exception:
            time.sleep(1)
    warn("Ollama did not respond in 20s — Inference may fail")
    return False


# ── launch ────────────────────────────────────────────────────────

def launch():
    """Start the launcher DETACHED so it survives this script exiting and
    survives the console window closing."""
    step("Launching fleet")
    os.makedirs(os.path.dirname(LAUNCH_LOG), exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        # CREATE_NO_WINDOW, not DETACHED_PROCESS. DETACHED_PROCESS leaves the
        # launcher with no console at all, so every bot it spawns allocates
        # its OWN window — twenty console windows across the screen.
        # CREATE_NO_WINDOW gives the launcher a hidden console that its
        # children inherit, so the whole tree stays invisible.
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)
    with open(LAUNCH_LOG, "a", encoding="utf-8") as out, \
         open(LAUNCH_ERR, "a", encoding="utf-8") as errf:
        out.write(f"\n{'='*60}\n=== RESTART {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        out.flush()
        p = subprocess.Popen(
            [PYTHON, "launch_fleet.py", "--headless"],
            cwd=CC_DIR, stdout=out, stderr=errf,
            creationflags=flags)
    ok(f"Launcher started (pid {p.pid}, detached)")
    return p.pid


def wait_for_cc(cc_port):
    step("Waiting for Command Center")
    deadline = time.time() + CC_BOOT_TIMEOUT
    while time.time() < deadline:
        if port_busy(cc_port):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{cc_port}/api/master", timeout=4)
                ok(f"Command Center responding on :{cc_port}")
                return True
            except Exception:
                pass
        time.sleep(1)
    err(f"Command Center did not respond within {CC_BOOT_TIMEOUT}s")
    return False


def wait_for_fleet(cc_port, total_bots):
    """Wait until most bots report alive. Phase 2 bots boot after CC, and
    slow bots take ~30s, so 'CC is up' is not 'fleet is up'."""
    step("Waiting for bots to report in")
    target = max(1, int(total_bots * HEALTHY_FRACTION))
    deadline = time.time() + FLEET_SETTLE_TIMEOUT
    best = 0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{cc_port}/api/master", timeout=5) as r:
                d = json.loads(r.read())
            bots = d.get("bots", [])
            alive = sum(1 for b in bots if b.get("alive"))
            if alive > best:
                best = alive
                say(f"{alive}/{len(bots)} alive...", "", C.DIM)
            if alive >= target:
                ok(f"Fleet healthy: {alive}/{len(bots)} alive")
                return d
        except Exception:
            pass
        time.sleep(3)
    warn(f"Settled at {best}/{total_bots} after {FLEET_SETTLE_TIMEOUT}s")
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cc_port}/api/master", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ── reporting ─────────────────────────────────────────────────────

def report(master, cc_port):
    if not master:
        err("No fleet data available")
        return
    bots = master.get("bots", [])
    alive = [b for b in bots if b.get("alive")]
    down = [b for b in bots if not b.get("alive")]

    step("Fleet")
    print(f"    mode        {master.get('fleet_mode')}")
    print(f"    alive       {len(alive)}/{len(bots)}")
    print(f"    dashboard   http://localhost:{cc_port}")

    if down:
        print()
        for b in down:
            err(f"DOWN: {b.get('name')} (:{b.get('port')})")

    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cc_port}/api/portfolio", timeout=5) as r:
            p = json.loads(r.read())
        print()
        print(f"    portfolio   ${p.get('total', 0):,.2f} total · "
              f"${p.get('deployed', 0):,.2f} deployed "
              f"({p.get('deployed_pct', 0):.1f}%) · risk {p.get('risk_status')}")
        print(f"    reserved    {p.get('active_reservations', 0)} position(s)")
    except Exception:
        pass


def status_only(bots, ports, cc_port):
    launchers, children, orphans, strays = survey(bots)
    step("Process survey")
    print(f"    launchers   {len(launchers)}")
    print(f"    children    {len(children)}")
    print(f"    orphans     {len(orphans)}")
    print(f"    strays      {len(strays)}")
    for pid, ppid, cmd in orphans:
        warn(f"orphan pid={pid} ppid={ppid} (dead) — {cmd[:60]}")
    for pid, ppid, cmd in strays:
        warn(f"stray  pid={pid} ppid={ppid} — {cmd[:60]}")

    busy = [p for p in ports if port_busy(p)]
    step("Ports")
    print(f"    listening   {len(busy)}/{len(ports)}")

    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cc_port}/api/master", timeout=5) as r:
            report(json.loads(r.read()), cc_port)
    except Exception:
        warn("Command Center not responding")


# ── main ──────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Fleet restart with orphan reaping")
    ap.add_argument("--status", action="store_true", help="report only")
    ap.add_argument("--stop", action="store_true", help="reap, do not relaunch")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    _enable_ansi()
    bots, ports, cc_port = load_fleet()

    print()
    print(f"  {C.BOLD}FLEET RESTART{C.OFF}  {len(bots)} bots · CC :{cc_port}")

    if args.status:
        status_only(bots, ports, cc_port)
        print()
        return 0

    step("Surveying processes")
    launchers, children, orphans, strays = survey(bots)
    say(f"{len(launchers)} launcher(s), {len(children)} child(ren), "
        f"{len(orphans)} orphan(s), {len(strays)} stray(s)")
    if orphans:
        for pid, ppid, cmd in orphans:
            warn(f"orphan pid={pid} (parent {ppid} is dead)")

    if not launchers and not children and port_busy(cc_port):
        # The survey saw nothing to reap, yet Command Center is answering on
        # its port — process enumeration failed (or raced). Launching now
        # would boot a SECOND fleet next to the live one: duplicate
        # bot_responders, shadow-bound ports, doubled Telegram sends
        # (happened 2026-07-28). Refuse.
        err(f"Survey found no fleet processes but CC is live on :{cc_port} — "
            "process enumeration failed; refusing to launch a second fleet.")
        err("Investigate with --status, or stop the fleet manually first.")
        print()
        return 1

    reap(launchers, children, orphans, strays)

    if args.stop:
        wait_ports_free(ports)
        ok("Fleet stopped")
        print()
        return 0

    if not wait_ports_free(ports):
        err("Fleet ports still held by live listeners after reap — "
            "refusing to launch a second fleet on top of them.")
        err("Run with --status to see what survived, then retry.")
        print()
        return 1

    ensure_ollama()
    launch()

    if not wait_for_cc(cc_port):
        err("Aborting — check logs/fleet_launch.log")
        print()
        return 1

    master = wait_for_fleet(cc_port, len(bots))
    report(master, cc_port)

    if not args.no_browser:
        step("Opening dashboard")
        webbrowser.open(f"http://localhost:{cc_port}")
        ok("Browser opened")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
