#!/usr/bin/env python3
"""
FLEET LAUNCHER v2.0
═══════════════════
State-of-the-art fleet process manager.

Features:
  • Two-phase launch with dependency ordering
  • Per-bot stdout/stderr capture to individual log files
  • Continuous health monitoring with auto-restart
  • Live TUI showing all bot statuses in real time
  • PID file tracking for external tooling
  • Graceful shutdown with state-save window
  • Parallel launch within each phase (threaded)
  • Crash loop detection (circuit breaker)

Usage:
  python launch_fleet.py              # full fleet + TUI
  python launch_fleet.py --headless   # no TUI, just logs
  python launch_fleet.py --dry-run    # show what would launch
"""

import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

from fleet_config import BOTS, CC_PORT, CC_DIR, PIDS_DIR, LOGS_DIR

# ── Constants ──────────────────────────────────────────────────────
HEALTH_INTERVAL = 15          # seconds between health checks
RESTART_GRACE = 5             # seconds between terminate and kill
MAX_RESTARTS = 3              # max restarts per bot before circuit breaker
RESTART_WINDOW = 300          # seconds — restart counter resets after this
BOOT_TIMEOUT = 30             # seconds to wait for a bot to bind its port
CC_BOOT_TIMEOUT = 20          # seconds to wait for Command Center
LAUNCH_STAGGER = 0.3          # seconds between parallel launches (rate limit)
SLOW_BOT_STAGGER = 2.0        # seconds for slow-boot bots
SHUTDOWN_TIMEOUT = 5          # seconds for graceful shutdown window


# ── Bot Process Wrapper ───────────────────────────────────────────

class BotProcess:
    """Manages a single bot subprocess with logging and health tracking."""

    def __init__(self, bot_id: str, cfg: dict):
        self.id = bot_id
        self.name = cfg["display"]
        self.port = cfg["port"]
        self.dir = cfg["dir"]
        self.cmd = cfg["cmd"]
        self.phase = cfg.get("phase", 1)
        self.slow = cfg.get("slow", False)
        self.role = cfg.get("role", "unknown")
        self.endpoints = cfg.get("endpoints", ["/api/snapshot"])

        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.state = "pending"       # pending, starting, alive, dead, disabled
        self.started_at: float | None = None
        self.last_health: float | None = None
        self.consecutive_failures = 0
        self.restart_count = 0
        self.restart_times: list[float] = []
        self.exit_code: int | None = None
        self._log_file = None

    @property
    def uptime(self) -> str:
        if not self.started_at:
            return "—"
        elapsed = time.time() - self.started_at
        if elapsed < 60:
            return f"{elapsed:.0f}s"
        elif elapsed < 3600:
            return f"{elapsed/60:.0f}m"
        else:
            return f"{elapsed/3600:.1f}h"

    def _open_log(self):
        """Open per-bot log file for stdout/stderr capture."""
        log_dir = os.path.join(LOGS_DIR, "bots")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"{self.id}.log")
        self._log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        self._log_file.write(f"\n{'='*60}\n")
        port_info = f"port {self.port}" if self.port else "no port"
        self._log_file.write(f"[{_now()}] === LAUNCH: {self.name} ({port_info}) ===\n")
        self._log_file.write(f"[{_now()}] cmd: {' '.join(self.cmd)}\n")
        self._log_file.write(f"[{_now()}] dir: {self.dir}\n")
        self._log_file.write(f"{'='*60}\n")

    def launch(self) -> bool:
        """Start the bot subprocess. Returns True on success."""
        if not os.path.isdir(self.dir):
            self.state = "disabled"
            return False

        # Kill zombies on this port
        if self.port:
            _kill_zombies(self.port)

        self._open_log()
        self.state = "starting"

        try:
            # PYTHONUNBUFFERED is required, not cosmetic. Python block-buffers
            # stdout (~8KB) whenever it is a file rather than a console, and
            # nothing flushes it for a long-lived process. Quiet bots therefore
            # wrote the launcher's banner and then nothing for hours: on
            # 2026-08-06, 11 of 18 bots had logs frozen at the launch line for
            # 8.3h while every one of them was alive and scanning normally
            # (aegis scan_count=996, nexus=1591). Chatty bots looked fine only
            # because they generate enough output to force a flush.
            #
            # The real cost is diagnostic: every check against those files was
            # reading a log the process was not writing to, so "no errors in
            # the log" meant nothing. Measured with the launcher's exact
            # wiring — 4 printed lines over 3s, read after 6s: without this
            # env, 0 lines captured; with it, 4.
            _env = dict(os.environ)
            _env["PYTHONUNBUFFERED"] = "1"

            self.proc = subprocess.Popen(
                self.cmd,
                cwd=self.dir,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                env=_env,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                               if sys.platform == "win32" else 0),
            )
            self.pid = self.proc.pid
            self.started_at = time.time()
            self.exit_code = None
            _write_pid(self.id, self.pid)
            return True
        except Exception as e:
            self.state = "dead"
            if self._log_file:
                self._log_file.write(f"[{_now()}] LAUNCH FAILED: {e}\n")
            return False

    def check_health(self) -> bool:
        """Check if bot is alive (port responding or process running). Updates state."""
        # Check if process is still running
        if self.proc and self.proc.poll() is not None:
            self.exit_code = self.proc.returncode
            self.state = "dead"
            self.consecutive_failures += 1
            if self._log_file:
                self._log_file.write(f"[{_now()}] PROCESS EXITED: code {self.exit_code}\n")
            return False

        # No port — health is just "process alive"
        if not self.port:
            if self.proc and self.proc.poll() is None:
                self.state = "alive"
                self.consecutive_failures = 0
                self.last_health = time.time()
                return True
            return False

        # Check port
        alive = _check_port(self.port, timeout=2.0)
        self.last_health = time.time()

        if alive:
            self.state = "alive"
            self.consecutive_failures = 0
            return True
        else:
            if self.state == "starting":
                # Still booting — give it time
                if time.time() - self.started_at < BOOT_TIMEOUT:
                    return False
            self.state = "dead"
            self.consecutive_failures += 1
            return False

    def can_restart(self) -> bool:
        """Check if restart is allowed (circuit breaker)."""
        if self.state == "disabled":
            return False
        now = time.time()
        # Prune old restart times
        self.restart_times = [t for t in self.restart_times if now - t < RESTART_WINDOW]
        return len(self.restart_times) < MAX_RESTARTS

    def restart(self) -> bool:
        """Kill and relaunch the bot."""
        self.stop()
        self.restart_count += 1
        self.restart_times.append(time.time())
        if self._log_file:
            self._log_file.write(f"[{_now()}] RESTARTING (attempt #{self.restart_count})\n")
        return self.launch()

    def stop(self):
        """Graceful terminate, then force kill."""
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=RESTART_GRACE)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
            except Exception:
                pass
        self.state = "dead"
        _remove_pid(self.id)
        if self._log_file:
            self._log_file.write(f"[{_now()}] STOPPED\n")
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None


# ── Fleet Manager ─────────────────────────────────────────────────

class FleetManager:
    """Orchestrates the entire fleet lifecycle."""

    def __init__(self, headless=False, dry_run=False):
        self.headless = headless
        self.dry_run = dry_run
        self.bots: dict[str, BotProcess] = {}
        self.cc_thread: threading.Thread | None = None
        self.cc_alive = False
        self.shutdown_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._tui_thread: threading.Thread | None = None
        self._start_time = time.time()

        # Build bot list from fleet_config
        for bot_id, cfg in BOTS.items():
            if cfg.get("cmd"):
                self.bots[bot_id] = BotProcess(bot_id, cfg)

    @property
    def phase1(self) -> list[BotProcess]:
        return [b for b in self.bots.values() if b.phase == 1]

    @property
    def phase2(self) -> list[BotProcess]:
        return [b for b in self.bots.values() if b.phase == 2]

    @property
    def alive_count(self) -> int:
        return sum(1 for b in self.bots.values() if b.state == "alive")

    @property
    def total_count(self) -> int:
        return len(self.bots)

    def run(self):
        """Main entry point — launch fleet, monitor, handle shutdown."""
        self._print_banner()

        if self.dry_run:
            self._print_dry_run()
            return

        # Phase 1: Core bots
        self._log("Phase 1: Launching core fleet...")
        self._launch_phase(self.phase1)

        # Command Center
        self._log(f"Starting Command Center on :{CC_PORT}...")
        self._start_command_center()

        # Phase 2: CC-dependent bots
        self._log("Phase 2: Launching CC-dependent bots...")
        self._launch_phase(self.phase2)

        # Initial health check
        time.sleep(3)
        self._health_sweep()
        self._print_status()

        # Start background threads
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="HealthMonitor")
        self._monitor_thread.start()

        if not self.headless:
            self._tui_thread = threading.Thread(
                target=self._tui_loop, daemon=True, name="TUI")
            self._tui_thread.start()

        # Main thread: wait for shutdown signal
        self._log(f"Fleet online: {self.alive_count}/{self.total_count} bots")
        self._log(f"Dashboard: http://localhost:{CC_PORT}")
        self._log("Press Ctrl+C to stop everything.")

        try:
            while not self.shutdown_event.is_set():
                self.shutdown_event.wait(1)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _launch_phase(self, bots: list[BotProcess]):
        """Launch a group of bots with staggered starts."""
        threads = []
        for bot in bots:
            if bot.port and _check_port(bot.port, timeout=0.3):
                bot.state = "alive"
                bot.started_at = time.time()
                self._log(f"  {_icon('alive')} {bot.name:<16s} :{bot.port}  already running")
                continue

            t = threading.Thread(target=self._launch_one, args=(bot,), daemon=True)
            threads.append(t)
            t.start()
            stagger = SLOW_BOT_STAGGER if bot.slow else LAUNCH_STAGGER
            time.sleep(stagger)

        # Wait for all launches to complete
        for t in threads:
            t.join(timeout=10)

    def _launch_one(self, bot: BotProcess):
        """Launch a single bot and report result."""
        ok = bot.launch()
        port_str = f":{bot.port}" if bot.port else "  —  "
        if ok:
            suffix = " (slow boot)" if bot.slow else ""
            self._log(f"  {_icon('starting')} {bot.name:<16s} {port_str}  pid={bot.pid}{suffix}")
        else:
            self._log(f"  {_icon('dead')} {bot.name:<16s} {port_str}  FAILED")

    def _start_command_center(self):
        """Import and run CC in a background thread."""
        sys.path.insert(0, CC_DIR)
        try:
            from command_center import main as cc_main
        except Exception as e:
            self._log(f"  Command Center import FAILED: {e}")
            self._log("  Phase 2 bots will NOT be launched.")
            return

        self.cc_thread = threading.Thread(target=cc_main, daemon=True, name="CommandCenter")
        self.cc_thread.start()

        # Wait for CC to bind
        for i in range(CC_BOOT_TIMEOUT):
            if _check_port(CC_PORT, timeout=1.0):
                self.cc_alive = True
                self._log(f"  Command Center online on :{CC_PORT}")
                return
            time.sleep(1)
        self._log(f"  Command Center not ready after {CC_BOOT_TIMEOUT}s — launching Phase 2 anyway")

    def _health_sweep(self):
        """Check health of all bots."""
        for bot in self.bots.values():
            if bot.state in ("disabled", "pending"):
                continue
            bot.check_health()

        self.cc_alive = _check_port(CC_PORT, timeout=2.0)

    def _monitor_loop(self):
        """Background thread: periodic health checks and auto-restart."""
        while not self.shutdown_event.is_set():
            self.shutdown_event.wait(HEALTH_INTERVAL)
            if self.shutdown_event.is_set():
                break

            self._health_sweep()

            # Auto-restart dead bots
            for bot in self.bots.values():
                if bot.state == "dead" and bot.can_restart():
                    self._log(f"  Auto-restarting {bot.name} (attempt {bot.restart_count + 1}/{MAX_RESTARTS})...")
                    if bot.restart():
                        time.sleep(3)  # Give it a moment
                        bot.check_health()
                        if bot.state == "alive":
                            self._log(f"  {bot.name} recovered")
                        else:
                            self._log(f"  {bot.name} restarted but not yet responding")
                    else:
                        self._log(f"  {bot.name} restart failed")
                elif bot.state == "dead" and not bot.can_restart():
                    if bot.state != "disabled":
                        bot.state = "disabled"
                        self._log(f"  {bot.name} circuit breaker tripped ({MAX_RESTARTS} restarts in {RESTART_WINDOW}s)")

    def _tui_loop(self):
        """Background thread: live terminal status display."""
        while not self.shutdown_event.is_set():
            self.shutdown_event.wait(5)
            if self.shutdown_event.is_set():
                break
            self._print_tui()

    def _print_tui(self):
        """Render a compact status table to the terminal."""
        elapsed = time.time() - self._start_time
        elapsed_str = f"{elapsed/3600:.1f}h" if elapsed > 3600 else f"{elapsed/60:.0f}m"

        lines = []
        lines.append("")
        lines.append(f"  FLEET STATUS  {self.alive_count}/{self.total_count} alive  |  uptime {elapsed_str}  |  CC {'OK' if self.cc_alive else 'DOWN'}")
        lines.append(f"  {'-'*62}")

        for bot in self.bots.values():
            icon = _icon(bot.state)
            restarts = f"r={bot.restart_count}" if bot.restart_count > 0 else ""
            exit_info = f"exit={bot.exit_code}" if bot.exit_code is not None else ""
            extra = " ".join(filter(None, [restarts, exit_info]))
            if extra:
                extra = f"  ({extra})"
            port_str = f":{bot.port}" if bot.port else "  —  "
            lines.append(
                f"  {icon} {bot.name:<16s} {port_str:<6s} {bot.state:<10s} {bot.uptime:>6s}  {bot.role:<8s}{extra}"
            )

        lines.append(f"  {'-'*62}")
        lines.append("")

        # Clear and reprint (simple approach — no curses dependency)
        output = "\n".join(lines)
        sys.stdout.write(f"\033[2J\033[H{output}")
        sys.stdout.flush()

    def _shutdown(self):
        """Graceful fleet shutdown."""
        self.shutdown_event.set()
        self._log("")
        self._log("Shutting down fleet...")

        # Stop bots in reverse order (Phase 2 first, then Phase 1)
        all_bots = list(self.bots.values())
        all_bots.sort(key=lambda b: b.phase, reverse=True)

        # Parallel shutdown
        threads = []
        for bot in all_bots:
            if bot.proc and bot.proc.poll() is None:
                t = threading.Thread(target=self._stop_one, args=(bot,), daemon=True)
                threads.append(t)
                t.start()

        # Wait for all shutdowns
        deadline = time.time() + SHUTDOWN_TIMEOUT + RESTART_GRACE
        for t in threads:
            remaining = max(0.1, deadline - time.time())
            t.join(timeout=remaining)

        # Report final state
        stopped = sum(1 for b in all_bots if b.state == "dead")
        self._log(f"  {stopped}/{len(all_bots)} bots stopped")
        self._log("Fleet offline.")

    def _stop_one(self, bot: BotProcess):
        """Stop a single bot and report."""
        bot.stop()
        self._log(f"  {bot.name:<16s} stopped")

    def _print_banner(self):
        print()
        print("  +----------------------------------------------+")
        print("  |           FLEET LAUNCHER v2.0                |")
        print("  |      Process Manager + Health Monitor        |")
        print("  +----------------------------------------------+")
        print(f"  |  Bots: {self.total_count:<3d}  |  Phase 1: {len(self.phase1):<3d}  |  Phase 2: {len(self.phase2):<3d} |")
        print(f"  |  CC:   :{CC_PORT}  |  Mode: {'headless' if self.headless else 'TUI':<9s}           |")
        print("  +----------------------------------------------+")
        print()

    def _print_dry_run(self):
        print("  DRY RUN — would launch:")
        print()
        for phase_num, bots in [(1, self.phase1), (2, self.phase2)]:
            print(f"  Phase {phase_num}:")
            for bot in bots:
                exists = os.path.isdir(bot.dir)
                occupied = _check_port(bot.port, timeout=0.3) if bot.port else False
                status = "already running" if occupied else ("ready" if exists else "DIR MISSING")
                port_str = f":{bot.port}" if bot.port else "  —  "
                print(f"    {bot.name:<16s} {port_str:<6s} {' '.join(bot.cmd):<45s}  [{status}]")
            print()

    def _print_status(self):
        print()
        print(f"  Health check: {self.alive_count}/{self.total_count} alive")
        for bot in self.bots.values():
            icon = _icon(bot.state)
            port_str = f":{bot.port}" if bot.port else "  —  "
            print(f"    {icon} {bot.name:<16s} {port_str}  {bot.state}")
        if self.cc_alive:
            print(f"    {_icon('alive')} {'Command Center':<16s} :{CC_PORT}  alive")
        print()

    def _log(self, msg: str):
        ts = _now()
        print(f"  [{ts}] {msg}" if msg.strip() else msg)


# ── Utility Functions ─────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")

def _icon(state: str) -> str:
    return {
        "alive": "+",
        "starting": "~",
        "dead": "X",
        "disabled": "-",
        "pending": ".",
    }.get(state, "?")

def _check_port(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(("127.0.0.1", port))
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False

def _kill_zombies(port: int):
    """Kill zombie processes holding a port."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                try:
                    pid = int(parts[-1])
                    if pid > 0:
                        subprocess.run(
                            ["taskkill", "/F", "/PID", str(pid)],
                            capture_output=True, timeout=5)
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass

def _write_pid(bot_id: str, pid: int):
    """Write PID file for external tooling."""
    os.makedirs(PIDS_DIR, exist_ok=True)
    path = os.path.join(PIDS_DIR, f"{bot_id}.pid")
    with open(path, "w") as f:
        f.write(str(pid))

def _remove_pid(bot_id: str):
    """Remove PID file on shutdown."""
    path = os.path.join(PIDS_DIR, f"{bot_id}.pid")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ── Entry Point ───────────────────────────────────────────────────

def main():
    headless = "--headless" in sys.argv
    dry_run = "--dry-run" in sys.argv

    manager = FleetManager(headless=headless, dry_run=dry_run)
    manager.run()


if __name__ == "__main__":
    main()
