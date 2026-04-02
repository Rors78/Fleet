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

# ── Fleet Registry ──────────────────────────────────────────────
# This is the authoritative source for launch commands and phase ordering.
# fleet_config.json and command_center.py BOT_REGISTRY have overlapping
# data — keep them in sync manually when adding/removing bots.

FLEET = [
    {
        "name": "TurtleSue",
        "port": 8070,
        "dir": r"D:\TurtleSue",
        "cmd": ["python", "turtlebot.py", "--auto"],
    },
    {
        "name": "Sentinel",
        "port": 8071,
        "dir": r"D:\Sentinel",
        "cmd": ["python", "sentinel.py"],
        "phase": 2,  # needs CC market data
        "slow": True,
    },
    {
        "name": "Trinity",
        "port": 8072,
        "dir": r"D:\Trinity",
        "cmd": ["python", "overwatch.py", "--auto"],
    },
    {
        "name": "HiveMind",
        "port": 8073,
        "dir": r"D:\HiveMind",
        "cmd": ["python", "cli.py", "dashboard", "--synthetic"],
        "slow": True,
    },
    {
        "name": "NexusBrain",
        "port": 8074,
        "dir": r"D:\NexusBrain",
        "cmd": ["python", "nexus_brain.py", "dashboard", "--auto"],
    },
    {
        "name": "TrekBot",
        "port": 8080,
        "dir": r"D:\TrekBot",
        "cmd": ["python", "trekbot.py"],
        "slow": True,
    },
    {
        "name": "Oracle",
        "port": 8075,
        "dir": r"D:\Oracle",
        "cmd": ["python", "server.py"],
        "slow": True,
    },
    {
        "name": "Deep Blue",
        "port": 8076,
        "dir": r"D:\Whale Watcher\apex_whale_finder.dir",
        "cmd": ["python", "main.py"],
    },
    {
        "name": "Gridzilla",
        "port": 8077,
        "dir": r"D:\Gridzilla",
        "cmd": ["python", "gridzilla.py", "--auto"],
    },
    {
        "name": "PHITEX",
        "port": 8078,
        "dir": r"D:\PhiTex",
        "cmd": ["python", "phitex.py"],
        "slow": True,
        "phase": 2,   # starts AFTER Command Center is confirmed up
    },
    {
        "name": "AEGIS",
        "port": 8079,
        "dir": r"D:\Aegis",
        "cmd": ["python", "aegis.py"],
        "phase": 2,   # starts AFTER Command Center is confirmed up
    },
    {
        "name": "Inference",
        "port": 9001,
        "dir": r"D:\CommandCenter",
        "cmd": ["python", "inference_server.py"],
        "phase": 2,
    },
    {
        "name": "NEXUS",
        "port": 8082,
        "dir": r"D:\Nexus",
        "cmd": ["python", "nexus.py"],
        "phase": 2,
    },
    {
        "name": "Rubberband",
        "port": 8083,
        "dir": r"D:\Rubberband",
        "cmd": ["python", "rubberband.py", "--auto"],
        "phase": 2,
    },
    {
        "name": "Contrarian",
        "port": 8084,
        "dir": r"D:\Contrarian",
        "cmd": ["python", "contrarian.py"],
        "phase": 2,
    },
    {
        "name": "Arbitrageur",
        "port": 8085,
        "dir": r"D:\Arbitrageur",
        "cmd": ["python", "arbitrageur.py"],
        "phase": 2,
    },
    {
        "name": "Chronos",
        "port": 8086,
        "dir": r"D:\Chronos",
        "cmd": ["python", "chronos.py"],
        "phase": 2,
    },
]

COMMAND_CENTER_PORT = 9000

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
    phase1 = [b for b in FLEET if b.get("phase", 1) == 1]
    phase2 = [b for b in FLEET if b.get("phase", 1) == 2]

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
