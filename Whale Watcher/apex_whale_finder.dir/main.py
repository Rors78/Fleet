import time
import signal
import threading
from datetime import datetime
from typing import Dict, List
from dataclasses import dataclass
from collections import OrderedDict

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.layout import Layout

from config import Config
from kraken_api import KrakenAPI
from indicators import IndicatorsEngine
from brain import Brain
from logger import setup_logger, WhaleDetectorLogger, setup_global_exception_handler

from concurrent.futures import ThreadPoolExecutor, as_completed


@dataclass
class ScanResult:
    pair: str
    timestamp: datetime
    close: float = 0.0
    volume: float = 0.0
    adx: float = 0.0
    chopiness: float = 0.0
    volume_surge: float = 0.0
    whale_score: float = 0.0
    trend: str = "neutral"
    valid: bool = True
    error: str | None = None


class WhaleScannerTUI:
    def __init__(self, config: Config):
        self.config = config
        self.console = Console()
        self.running = False
        self.paused = False
        self.shutdown_event = threading.Event()
        self.force_rescan = False
        self.show_help = False
        self.cycle = 0
        self.results: Dict[str, ScanResult] = {}
        self.sorted_valid_results: List = []
        self.whale_candidates: List[dict] = []
        self.log_messages: List[str] = []
        self.start_time = datetime.now()
        self.last_cycle_duration = 0.0

        self.ohlc_cache: OrderedDict = OrderedDict()
        self.max_cache_size = 60

        self.api = KrakenAPI(base_url=config.api.base_url, timeout=config.api.timeout, max_retries=config.api.max_retries)
        self.indicators = IndicatorsEngine()
        self.brain = Brain(memory_cycles=config.brain.memory_cycles, signal_threshold=config.brain.signal_threshold)

        self.logger = setup_logger(name="moby_watch_bot", log_dir=config.logging.log_dir,
                                   level=config.get_logging_level(), file_rotation=config.logging.rotation)
        self.whale_logger = WhaleDetectorLogger(self.logger)
        setup_global_exception_handler(self.logger)

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Disable Numba in parallel mode - has threading issues with ThreadPoolExecutor
        # Use pandas mode for reliable parallel execution
        self.indicators._use_numba = False

        # Start keyboard listener thread
        threading.Thread(target=self._keyboard_listener, daemon=True).start()

        self.logger.info("MobyWatchBot PRODUCTION v1.0 started with interactive controls")

    def _signal_handler(self, signum, frame):
        self.logger.info(f"Shutdown signal {signum}")
        self.running = False
        self.shutdown_event.set()

    def add_log(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_messages.append(f"[{ts}] {message}")
        if len(self.log_messages) > 120:
            self.log_messages = self.log_messages[-120:]
        self.logger.info(message)

    def _cleanup_cache(self):
        while len(self.ohlc_cache) > self.max_cache_size:
            self.ohlc_cache.popitem(last=False)

    # ==================== INTERACTIVE KEYBINDINGS ====================
    def _keyboard_listener(self):
        """Cross-platform keyboard listener (no extra dependencies)"""
        import sys
        if sys.platform == "win32":
            import msvcrt
            while not self.shutdown_event.is_set():
                if msvcrt.kbhit():
                    key = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                    self._handle_key(key)
                time.sleep(0.05)
        else:
            import termios, tty, select
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while not self.shutdown_event.is_set():
                    if select.select([sys.stdin], [], [], 0.05)[0]:
                        key = sys.stdin.read(1).lower()
                        self._handle_key(key)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _handle_key(self, key: str):
        if key in ('q', '\x1b'):  # q or ESC
            self.running = False
            self.shutdown_event.set()
        elif key == 'r':
            self.force_rescan = True
            self.add_log("Manual rescan triggered")
        elif key in ('p', ' '):
            self.paused = not self.paused
            self.add_log(f"Scanner {'PAUSED' if self.paused else 'RESUMED'}")
        elif key == 'c':
            self.log_messages.clear()
            self.add_log("Log cleared")
        elif key == 'h':
            self.show_help = not self.show_help
        elif key == 's':
            # Optional: switch watchlist (main ↔ smallcap)
            current = self.config.get_watchlist()
            if len(current) > 15:  # rough check for main list
                self.config.set_watchlist(self.config.watchlist_smallcap)
                self.add_log("Switched to SMALLCAP watchlist")
            else:
                self.config.set_watchlist(self.config.watchlist)
                self.add_log("Switched to MAIN watchlist")

    def scan_pair(self, pair: str) -> ScanResult:
        # (same as before - unchanged)
        try:
            key = f"{pair}_{self.config.scanner.candle_interval}"
            df = self.ohlc_cache.get(key)
            if df is None:
                df = self.api.get_ohlc(pair, self.config.scanner.candle_interval)
                if df is not None:
                    self.ohlc_cache[key] = df
                    self._cleanup_cache()

            if df is None or len(df) < self.config.scanner.min_data_points:
                self.whale_logger.log_data_quality(pair, "insufficient_data", len(df) if df is not None else 0)
                return ScanResult(pair=pair, timestamp=datetime.now(), valid=False, error="Insufficient data")

            df = self.indicators.add_indicators(df, self.config.indicators.adx_window,
                                                self.config.indicators.chopiness_window,
                                                self.config.indicators.volume_window)

            required = ['close', 'volume', 'ADX', 'chopiness', 'volume_surge', 'whale_score']
            if any(c not in df.columns for c in required):
                raise ValueError(f"Missing indicators for {pair}")

            latest = df.iloc[-1]
            ts = df.index[-1] if hasattr(df.index, '__getitem__') else datetime.now()

            ind = {k: float(latest[v]) for k, v in [
                ('adx', 'ADX'), ('chopiness', 'chopiness'), ('volume_surge', 'volume_surge'),
                ('whale_score', 'whale_score'), ('close', 'close'), ('volume', 'volume')
            ]}
            # +DI / -DI supply direction; ADX only supplies strength. Optional
            # so this keeps working if an older indicator frame lacks them.
            # (2026-07-29 audit — see get_trend_direction)
            for _k, _col in (('plus_di', '+DI'), ('minus_di', '-DI')):
                if _col in df.columns:
                    try:
                        ind[_k] = float(latest[_col])
                    except (TypeError, ValueError):
                        pass

            self.brain.update_memory(pair, ind, latest.name)
            trend = self.brain.get_trend_direction(pair)

            if self.brain.should_alert(pair, ind):
                exp = self.brain.generate_explanation(pair, ind)
                rel = self.brain.calculate_signal_reliability(pair)
                self.whale_logger.log_whale_detection(pair, ind['whale_score'], rel, exp)

            return ScanResult(pair=pair, timestamp=ts, trend=trend, valid=True, **ind)

        except Exception as e:
            self.whale_logger.log_api_error(pair, type(e).__name__, str(e))
            self.add_log(f"ERROR {pair}: {str(e)}")
            return ScanResult(pair=pair, timestamp=datetime.now(), valid=False, error=str(e))

    def scan_all_pairs(self):
        # Clear OHLC cache each cycle so we fetch fresh data
        self.ohlc_cache.clear()
        self.cycle += 1
        watchlist = self.config.get_watchlist()
        if not watchlist:
            self.add_log("Watchlist is empty.")
            return

        self.whale_logger.log_cycle_start(self.cycle, watchlist)
        self.add_log(f"Cycle {self.cycle}: Scanning {len(watchlist)} pairs...")

        start = time.time()
        errors = 0
        max_workers = getattr(self.config.scanner, 'max_workers', 8)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(self.scan_pair, p): p for p in watchlist}
            for future in as_completed(futures):
                p = futures[future]
                try:
                    self.results[p] = future.result()
                    if not self.results[p].valid:
                        errors += 1
                except Exception as e:
                    self.add_log(f"Failed {p}: {e}")
                    errors += 1

        valid = {p: r for p, r in self.results.items() if r.valid}
        inds = {p: {k: getattr(r, k) for k in ['adx','chopiness','volume_surge','whale_score','close','volume']}
                for p, r in valid.items()}

        if inds:
            self.whale_candidates = self.brain.get_whale_candidates(inds)

        b = sum(1 for r in valid.values() if r.trend == "bullish")
        be = sum(1 for r in valid.values() if r.trend == "bearish")
        sentiment = "BULLISH" if b > be else "BEARISH" if be > b else "NEUTRAL"
        self.whale_logger.log_market_sentiment(sentiment, b, be, len(valid) - b - be)

        self.sorted_valid_results = sorted(valid.items(), key=lambda x: x[1].whale_score, reverse=True)

        dur = time.time() - start
        self.last_cycle_duration = dur
        self.whale_logger.log_cycle_complete(self.cycle, dur, len(self.whale_candidates), errors)
        self.add_log(f"Cycle complete: {len(self.whale_candidates)} whales, {errors} errors, {dur:.1f}s")

    # ==================== NEW TUI ====================
    def get_terminal_size(self):
        import shutil
        return shutil.get_terminal_size(fallback=(120, 36))

    def create_header(self, width: int):
        title = Text("WHALE TRACKER v4.20", justify="center", style="bold #60a5fa on #0a0a0e")
        subtitle = Text("Dark gunmetal edition - real-time detection", justify="center", style="#64748b")
        return Panel(
            Text.assemble(title, "\n", subtitle),
            style="on #0a0a0e",
            border_style="#1e293b",
            padding=(1, 2),
            expand=False
        )

    def create_status_bar(self, width: int):
        uptime = datetime.now() - self.start_time
        h, r = divmod(int(uptime.total_seconds()), 3600)
        m, s = divmod(r, 60)
        
        whale_count = len(self.whale_candidates)
        pair_count = len([r for r in self.results.values() if r.valid])
        
        status_text = Text(
            f"Cycle {self.cycle}   |   {whale_count} active signals   |   {pair_count}/20 pairs   |   latency {self.last_cycle_duration:.1f}s   |   uptime {h:02d}:{m:02d}:{s:02d}",
            style="#94a3b8",
            justify="center"
        )
        return Panel(
            status_text,
            style="#121218 on #0a0a0e",
            border_style="#3b82f6",
            padding=(0, 2),
            height=3
        )

    def create_main_table(self, width: int):
        table = Table(
            show_header=True,
            header_style="bold #60a5fa",
            border_style="#1e293b",
            expand=True,
            padding=(0, 1)
        )

        col_pair_w = max(10, min(16, width // 10))
        col_score_w = max(8, min(12, width // 15))
        col_trend_w = 10
        col_adx_w = max(8, min(12, width // 15))
        col_chop_w = max(8, min(12, width // 15))

        table.add_column("Pair", style="#e2e8f0", width=col_pair_w)
        table.add_column("Score", justify="right", style="#fbbf24", width=col_score_w)
        table.add_column("Trend", justify="center", style="#34d399", width=col_trend_w)
        table.add_column("ADX", justify="right", style="#60a5fa", width=col_adx_w)
        table.add_column("Chop", justify="right", style="#a855f7", width=col_chop_w)

        for pair, r in self.sorted_valid_results[:12]:
            if r.whale_score >= 80:
                score_style = "bold #ef4444"
            elif r.whale_score >= 65:
                score_style = "bold #f59e0b"
            elif r.whale_score >= 50:
                score_style = "#10b981"
            else:
                score_style = "#64748b"
            
            trend_sym = "^" if r.trend == "bullish" else "v" if r.trend == "bearish" else "-"
            trend_color = "#34d399" if r.trend == "bullish" else "#f87171" if r.trend == "bearish" else "#64748b"
            
            # Extract color name for closing tag
            color = score_style.split()[-1] if "bold" in score_style else score_style
            
            table.add_row(
                pair,
                f"[{color}]{r.whale_score:.1f}[/{color}]" if "bold" not in score_style else f"[{score_style}]{r.whale_score:.1f}[/{score_style}]",
                f"[{trend_color}]{trend_sym}[/{trend_color}]",
                f"{r.adx:.1f}",
                f"{r.chopiness:.1f}"
            )

        return Panel(
            table,
            title="[bold #60a5fa]WHALE ACTIVITY[/]",
            border_style="#3b82f6",
            padding=(1, 2),
            expand=True
        )

    def build_layout(self) -> Layout:
        w, h = self.get_terminal_size()
        
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=5, minimum_size=5),
            Layout(name="main", ratio=1),
            Layout(name="status", size=3, minimum_size=3)
        )

        layout["header"].update(self.create_header(w))
        layout["main"].update(self.create_main_table(w))
        layout["status"].update(self.create_status_bar(w))

        return layout

    def run(self):
        self.running = True
        self.add_log("Interactive Whale Scanner started - press h for keybindings")

        with Live(console=self.console, refresh_per_second=4, screen=True) as live:
            while self.running and not self.shutdown_event.is_set():
                try:
                    if self.force_rescan or not self.paused:
                        self.scan_all_pairs()
                        self.force_rescan = False

                    live.update(self.build_layout())
                except Exception as e:
                    self.logger.error(f"Render error: {e}")

                # Sleep with shutdown & pause check
                for _ in range(self.config.scanner.interval):
                    if self.shutdown_event.is_set() or self.force_rescan:
                        break
                    time.sleep(1.0)


def main():
    import sys as _sys
    import argparse
    _sys.path.insert(0, r"D:\CommandCenter")
    
    parser = argparse.ArgumentParser(description="Deep Blue Whale Scanner")
    parser.add_argument("--headless", action="store_true", help="Run without TUI (for fleet deployment)")
    args = parser.parse_args()
    
    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        ensure_port(8076, "deepblue")
        write_pidfile("deepblue", 8076)
        atexit.register(cleanup_pidfile, "deepblue")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    config = Config()

    from dashboard import run_dashboard
    threading.Thread(target=lambda: run_dashboard(8076), daemon=True).start()

    if args.headless:
        print("[Deep Blue] Running in headless mode - API server active, TUI disabled")
        while True:
            time.sleep(3600)
    
    tui = WhaleScannerTUI(config)
    try:
        tui.run()
    except KeyboardInterrupt:
        pass
    finally:
        tui.running = False
        tui.shutdown_event.set()
        tui.logger.info("MobyWatchBot shutdown complete")


if __name__ == "__main__":
    main()