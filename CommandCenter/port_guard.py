"""
Port Guardian — import this in every bot's main file.
Call ensure_port(port, bot_name) before starting your HTTP server.
Kills any zombie process occupying the port, waits for confirmation, then returns.

Usage in each bot:
    import sys
    sys.path.insert(0, r"D:\\CommandCenter")
    from port_guard import ensure_port, write_pidfile, cleanup_pidfile
    import atexit

    BOT_NAME = "trinity"
    BOT_PORT = 8072

    ensure_port(BOT_PORT, BOT_NAME)
    write_pidfile(BOT_NAME, BOT_PORT)
    atexit.register(cleanup_pidfile, BOT_NAME)
"""

import os
import socket
import subprocess
import sys
import time

# Use fleet_config if available, fall back to relative path
try:
    from fleet_config import PIDS_DIR as _PID_DIR
except ImportError:
    _PID_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pids")


def _get_pids_on_port(port: int) -> list[int]:
    """Return all PIDs listening on a port via netstat."""
    pids = []
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                try:
                    pid = int(parts[-1])
                    if pid > 0 and pid not in pids:
                        pids.append(pid)
                except (ValueError, IndexError):
                    pass
    except Exception as e:
        print(f"[PORT_GUARD] netstat failed: {e}", file=sys.stderr)
    return pids


def is_port_free(port: int) -> bool:
    """Return True if the port can be bound."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.settimeout(1)
            s.bind(("0.0.0.0", port))
            return True
    except OSError:
        return False


def _kill_pid(pid: int) -> None:
    """Kill a process: graceful first, then force."""
    my_pid = os.getpid()
    if pid == my_pid:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid)],
            capture_output=True, timeout=5,
        )
        time.sleep(1)
        # Check if still alive
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=5,
        )
        if str(pid) in result.stdout:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
            time.sleep(1)
    except Exception as e:
        print(f"[PORT_GUARD] kill PID {pid} failed: {e}", file=sys.stderr)


def ensure_port(port: int, bot_name: str = "unknown", max_retries: int = 3) -> bool:
    """
    Guarantee port is free before returning.
    Kills any zombie processes occupying it.
    Call this BEFORE starting your HTTP server.
    """
    my_pid = os.getpid()

    for attempt in range(max_retries):
        if is_port_free(port):
            print(f"[PORT_GUARD] Port {port} free for {bot_name} (PID {my_pid})")
            return True

        zombie_pids = _get_pids_on_port(port)
        # Filter out ourselves
        zombie_pids = [p for p in zombie_pids if p != my_pid]

        if not zombie_pids:
            # Port busy but no foreign PID found — socket in TIME_WAIT, wait it out
            print(f"[PORT_GUARD] Port {port} busy, no zombie PID found, waiting... (attempt {attempt + 1})")
            time.sleep(3)
            continue

        for zpid in zombie_pids:
            print(f"[PORT_GUARD] Port {port} occupied by PID {zpid} — killing zombie")
            _kill_pid(zpid)

        time.sleep(1)
        if is_port_free(port):
            print(f"[PORT_GUARD] Zombie(s) cleared. Port {port} free for {bot_name} (PID {my_pid})")
            return True

        print(f"[PORT_GUARD] Port {port} still busy after kill attempt {attempt + 1}/{max_retries}")

    print(f"[PORT_GUARD] FATAL: Could not free port {port} after {max_retries} attempts", file=sys.stderr)
    return False


def write_pidfile(bot_name: str, port: int) -> None:
    """Write PID file so the fleet watchdog can track this process."""
    os.makedirs(_PID_DIR, exist_ok=True)
    pid_file = os.path.join(_PID_DIR, f"{bot_name}.pid")
    with open(pid_file, "w") as f:
        f.write(f"{os.getpid()}\n{port}\n{time.time()}\n")


def cleanup_pidfile(bot_name: str) -> None:
    """Remove PID file on clean shutdown (register with atexit)."""
    pid_file = os.path.join(_PID_DIR, f"{bot_name}.pid")
    try:
        os.remove(pid_file)
    except OSError:
        pass
