#!/usr/bin/env python3
"""
FLEET LAUNCHER v1.0
====================
Starts all bots as background subprocesses, then runs Command Center
in the foreground. One terminal. Ctrl+C stops everything.

Usage: python launch_fleet.py
"""

import os
import socket
import subprocess
import sys
import threading
import time

from fleet_config import BOTS, CC_PORT as COMMAND_CENTER_PORT

# ── Fleet Registry ──────────────────────────────────────────────
# Derived from fleet_config.py — single source of truth for bot dirs/ports/cmds.
# To add, remove, or reconfigure a bot: edit fleet_config.py BOTS only.
FLEET = [
    {
        "name":  cfg["display"],
        "dir":   cfg["dir"],
        "cmd":   cfg["cmd"],
        "port":  cfg["port"],
        "phase": cfg.get("phase", 1),
        "slow":  cfg.get("slow", False),
    }
    for cfg in BOTS.values()
    if cfg.get("cmd")  # bots without a cmd have no subprocess to launch
]

# ── Helpers ─────────────────────────────────────────────────────

def check_port(port, timeout=1.0):
    """Quick TCP check to see if something is listening on a port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(("127.0.0.1", port))
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def kill_zombies_on_port(port):
    """Kill any zombie processes holding a port before launching a new bot."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10
        )
        killed = []
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                try:
                    pid = int(parts[-1])
                    if pid > 0:
                        subprocess.run(
                            ["taskkill", "/F", "/PID", str(pid)],
                            capture_output=True, timeout=5,
                        )
                        killed.append(pid)
                except (ValueError, IndexError):
                    pass
        if killed:
            time.sleep(1)  # let OS release port
            print(f"  [port_guard] Killed zombie PID(s) {killed} on :{port}")
    except Exception as e:
        print(f"  [port_guard] Warning: zombie kill failed for :{port}: {e}")


# ── Main ────────────────────────────────────────────────────────

def main():
    print()
    print("  +==========================================+")
    print("  |         FLEET LAUNCHER v1.0              |")
    print("  +==========================================+")
    print()
    print("  Launching fleet...")
    print()

    processes = []
    launched = 0
    skipped = 0

    def launch_bot(bot):
        nonlocal launched, skipped
        name = bot["name"]
        port = bot["port"]

        if bot.get("optional"):
            print(f"  o {name:<12s} :{port}  skipped ({bot.get('skip_reason', '')})")
            skipped += 1
            return

        if not os.path.isdir(bot["dir"]):
            print(f"  ! {name:<12s} :{port}  skipped (dir not found)")
            skipped += 1
            return

        if check_port(port, timeout=0.3):
            print(f"  * {name:<12s} :{port}  already running")
            launched += 1
            return

        # Clear any zombie processes on this port before binding
        kill_zombies_on_port(port)

        try:
            proc = subprocess.Popen(
                bot["cmd"], cwd=bot["dir"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
            processes.append({"name": name, "port": port, "proc": proc})
            suffix = " (slow boot ~30s)" if bot.get("slow") else ""
            print(f"  * {name:<12s} :{port}  launched{suffix}")
            launched += 1
        except Exception as e:
            print(f"  ! {name:<12s} :{port}  FAILED: {e}")
            skipped += 1

        # Brief delay between launches to avoid Kraken API rate-limiting on startup
        if bot.get("slow"):
            time.sleep(2)
        else:
            time.sleep(0.5)

    # Phase 1: All bots EXCEPT those that depend on Command Center
    phase1 = [b for b in FLEET if b["phase"] == 1]
    phase2 = [b for b in FLEET if b["phase"] == 2]

    print("  Phase 1: Core fleet...")
    for bot in phase1:
        launch_bot(bot)

    # Start Command Center in a background thread so Phase 2 bots can reach it
    cc_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, cc_dir)
    print()
    print(f"  Starting Command Center on :{COMMAND_CENTER_PORT}...")

    try:
        from command_center import main as cc_main
    except Exception as e:
        print(f"  ! Command Center import failed: {e}")
        print(f"  ! Phase 1 bots are running. Phase 2 bots will NOT be launched.")
        print(f"  ! Fix the import error and restart.")
        # Fall through to the keep-alive loop so Phase 1 bots stay managed
        cc_main = None

    if cc_main:
        cc_thread = threading.Thread(target=cc_main, daemon=True, name="CommandCenter")
        cc_thread.start()

        # Wait for CC to bind
        for _ in range(15):
            if check_port(COMMAND_CENTER_PORT, timeout=1.0):
                print(f"  Command Center responding on :{COMMAND_CENTER_PORT}")
                break
            time.sleep(1)
        else:
            print(f"  Command Center not ready after 15s — launching Phase 2 anyway (they have fallbacks)")

        print("  Phase 2: CC-dependent bots...")
        for bot in phase2:
            launch_bot(bot)

    total = len(FLEET)
    print()
    print(f"  {launched}/{total} bots launched, {skipped} skipped.")
    print()

    # Health check
    time.sleep(5)
    print("  Health check...")
    alive = 0
    for bot in FLEET:
        if bot.get("optional"):
            continue
        port = bot["port"]
        name = bot["name"]
        responding = check_port(port, timeout=2.0)
        status = "OK" if responding else "not yet"
        if responding:
            alive += 1
        print(f"    {name:<12s} :{port}  {status}")
    print()

    # Keep this process alive as the fleet manager
    print(f"  Command Center: http://localhost:{COMMAND_CENTER_PORT}")
    print(f"  Press Ctrl+C to stop everything.")
    print()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        # Shutdown all child processes
        print()
        print("  Shutting down fleet...")

        for p in processes:
            name = p["name"]
            proc = p["proc"]
            if proc.poll() is None:
                try:
                    proc.terminate()
                    print(f"    {name:<12s} terminated")
                except Exception:
                    pass

        # Wait for graceful exit — give bots time to close positions/write state
        time.sleep(3)

        # Force kill stragglers
        for p in processes:
            name = p["name"]
            proc = p["proc"]
            if proc.poll() is None:
                try:
                    proc.kill()
                    print(f"    {name:<12s} killed (forced)")
                except Exception:
                    pass
            else:
                exit_code = proc.returncode
                if exit_code and exit_code != 0:
                    print(f"    {name:<12s} exited (code {exit_code})")

        print()
        print("  Fleet stopped.")
        print()


if __name__ == "__main__":
    main()
