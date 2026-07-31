import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from typing import Dict, Any, List, Optional

from config import Config
from kraken_api import KrakenAPI
from indicators import IndicatorsEngine
from brain import Brain
from logger import setup_logger, WhaleDetectorLogger

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "CommandCenter"))
try:
    from event_publisher import EventPublisher
except ImportError:
    EventPublisher = None

try:
    from fleet_config import is_blacklisted as _is_blacklisted
except ImportError:
    _is_blacklisted = lambda pair: False


class ScannerBackend:
    """Backend scanner that runs in background thread"""
    
    def __init__(self, config: Config):
        self.config = config
        self.running = False
        self.cycle = 0
        self.pairs: Dict[str, Dict[str, Any]] = {}
        self.whales: List[Dict[str, Any]] = []
        self.logs: List[str] = []
        self.scan_duration = 0.0
        
        self.api = KrakenAPI(
            base_url=config.api.base_url,
            timeout=config.api.timeout,
            max_retries=config.api.max_retries
        )
        
        self.indicators = IndicatorsEngine()
        self.indicators._use_numba = False  # Numba ADX is buggy (NaN on 50%+ of rows)
        self.brain = Brain(
            memory_cycles=config.brain.memory_cycles,
            signal_threshold=config.brain.signal_threshold
        )
        
        self.logger = setup_logger("apex_whale_finder")
        self.whale_logger = WhaleDetectorLogger(self.logger)

        self._event_pub = None
        if EventPublisher:
            self._event_pub = EventPublisher("http://127.0.0.1:9000", "deepblue")

    def add_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{timestamp}] {message}")
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]
    
    def scan_pair(self, pair: str) -> Optional[Dict[str, Any]]:
        try:
            df = self.api.get_ohlc(
                pair,
                interval=self.config.scanner.candle_interval
            )
            
            if df is None or len(df) < self.config.scanner.min_data_points:
                self.add_log(f"WARNING: Insufficient data for {pair}")
                return None
            
            df = self.indicators.add_indicators(df)
            latest = df.iloc[-1]
            
            indicators_dict = {
                'adx': float(latest.get('ADX', 20)),
                # +DI / -DI carry the DIRECTION that ADX only measures the
                # strength of. Without them the brain had to infer direction
                # from a short price-average comparison, which could never
                # fire (see get_trend_direction). (2026-07-29 audit)
                'plus_di': float(latest.get('+DI', 0) or 0),
                'minus_di': float(latest.get('-DI', 0) or 0),
                'chopiness': float(latest.get('chopiness', 50)),
                'volume_surge': float(latest.get('volume_surge', 0)),
                'whale_score': float(latest.get('whale_score', 0)),
                'close': float(latest['close']),
                'volume': float(latest['volume'])
            }
            
            self.brain.update_memory(pair, indicators_dict, latest.name)
            trend = self.brain.get_trend_direction(pair)
            
            del df
            
            # `volume` is BASE-ASSET volume for a single bar, so it is not
            # comparable across pairs — live values ranged from BTC 0.166 to
            # XRP 4836.191 purely because of unit price, making BTC look
            # ~29,000x less active than XRP. Publish the USD notional
            # alongside it (and keep the raw figure under an explicit name) so
            # consumers can compare pairs without dividing by price
            # themselves. volumeSurge is a ratio and was always the sound
            # field. (2026-07-29 audit)
            _vol_base = indicators_dict['volume']
            _close = indicators_dict['close']
            _vol_usd = (_vol_base * _close) if (_vol_base and _close) else 0.0

            return {
                'pair': pair,
                'close': _close,
                # Kept for backwards compatibility with existing consumers.
                'volume': _vol_base,
                'volumeBase': _vol_base,
                'volumeUsd': round(_vol_usd, 2),
                'adx': indicators_dict['adx'],
                'chopiness': indicators_dict['chopiness'],
                'volumeSurge': indicators_dict['volume_surge'],
                'whaleScore': indicators_dict['whale_score'],
                'trend': trend,
                'valid': True,
                'blacklisted': _is_blacklisted(pair),
            }
            
        except Exception as e:
            self.add_log(f"ERROR: {pair} - {str(e)}")
            return None
    
    def scan_all(self) -> None:
      try:
        self.cycle += 1
        watchlist = self.config.get_watchlist()

        self.add_log(f"[CYCLE_START] {self.cycle} | {len(watchlist)} pairs")

        start_time = time.time()

        for pair in watchlist:
            result = self.scan_pair(pair)
            if result:
                self.pairs[pair] = result
            time.sleep(0.5)  # Rate limit: don't hammer Kraken

        # Get whale candidates
        all_indicators = {
            pair: {
                'adx': data['adx'],
                'chopiness': data['chopiness'],
                'volume_surge': data['volumeSurge'],
                'whale_score': data['whaleScore'],
                'close': data['close'],
                'volume': data['volume']
            }
            for pair, data in self.pairs.items()
            if data.get('valid')
        }

        self.whales = self.brain.get_whale_candidates(all_indicators)

        if self._event_pub and self.whales:
            for w in self.whales:
                try:
                    self._event_pub.emit("WHALE_ALERT", {
                        "pair": w.get("pair", ""),
                        "score": w.get("whale_score", 0),
                        "tier": "EXTREME" if w.get("whale_score", 0) >= 80 else "HIGH",
                        "reliability": w.get("reliability", 0),
                    })
                except Exception:
                    pass

        self.scan_duration = time.time() - start_time

        whale_count = len(self.whales)
        self.add_log(f"[CYCLE_COMPLETE] {self.cycle} | {self.scan_duration:.1f}s | {whale_count} whales")

        if self._event_pub:
            try:
                self._event_pub.emit("SCAN_COMPLETE", {
                    "pairs_scanned": len(self.pairs),
                    "whales_detected": len(self.whales),
                    "duration_s": round(self.scan_duration, 1),
                })
            except Exception:
                pass
      except Exception as e:
        self.add_log(f"[CYCLE_ERROR] {e}")

    def start(self) -> None:
        self.running = True
        self.add_log("Scanner backend started")
        
        while self.running:
            self.scan_all()
            time.sleep(self.config.scanner.interval)
    
    def stop(self) -> None:
        self.running = False
        self.add_log("Scanner backend stopped")


class DashboardHandler(SimpleHTTPRequestHandler):
    """HTTP handler for dashboard"""
    
    backend: ScannerBackend = None
    
    def do_GET(self):
        if self.path == '/api/data' or self.path == '/api/snapshot':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            whales_list = [
                {
                    'pair': w['pair'],
                    'whaleScore': w['whale_score'],
                    'reliability': w['reliability'],
                    'explanation': w['explanation'],
                    'trend': w['trend'],
                    'momentum': w.get('momentum', {}),
                    'blacklisted': _is_blacklisted(w['pair']),
                }
                for w in self.backend.whales
            ]

            data = {
                'timestamp': time.time(),
                'bot_name': 'Deep Blue',
                'status': 'running' if self.backend.cycle > 0 else 'scanning',
                'cycle': self.backend.cycle,
                'pairs': self.backend.pairs,
                'whales': whales_list,
                'whale_count': len(whales_list),
                'pairs_scanned': len(self.backend.pairs),
                'logs': self.backend.logs[-50:],
                'duration': self.backend.scan_duration
            }

            self.wfile.write(json.dumps(data).encode())

        elif self.path == '/' or self.path == '/index.html' or self.path == '/dashboard':
            self.path = '/templates/dashboard.html'
            return super().do_GET()
        
        else:
            return super().do_GET()
    
    def log_message(self, format, *args):
        pass  # Suppress HTTP server logs


def run_dashboard(port: int = 9090):
    """Run the dashboard server"""
    config = Config()
    
    # Initialize backend
    backend = ScannerBackend(config)
    DashboardHandler.backend = backend
    
    # Start scanner in background thread
    scanner_thread = threading.Thread(target=backend.start, daemon=True)
    scanner_thread.start()
    
    # Start HTTP server.
    # ThreadingHTTPServer, not HTTPServer: the plain server handles one request
    # at a time, so while the whale scan is running the port stops answering.
    # Command Center's health check times out and reports Deep Blue DOWN even
    # though it is healthy — observed as 18/18 flickering to 15/18, with probes
    # returning sub-millisecond responses except for occasional 4s timeouts.
    # daemon_threads so request threads never block shutdown.
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    server.daemon_threads = True
    
    print(f"\n{'='*60}")
    print(f"  [WHALE] MOBYWATCHBOT DASHBOARD")
    print(f"{'='*60}")
    print(f"\n  Dashboard: http://localhost:{port}")
    print(f"  API:       http://localhost:{port}/api/data")
    print(f"  Pairs:     {len(config.get_watchlist())}")
    print(f"  Interval:  {config.scanner.interval}s")
    print(f"\n  Press Ctrl+C to stop")
    print(f"{'='*60}\n")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        backend.stop()
        server.shutdown()


if __name__ == "__main__":
    run_dashboard()