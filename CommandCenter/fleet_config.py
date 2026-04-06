"""
Fleet Configuration — Single source of truth.
Every bot and tool imports this instead of hardcoding paths/ports.
"""

import os

# ── FLEET ROOT ──
CC_DIR = os.path.dirname(os.path.abspath(__file__))        # D:\CommandCenter
DATA_DRIVE = os.path.splitdrive(CC_DIR)[0] + os.sep        # D:\

# ── COMMAND CENTER ──
CC_PORT = 9000
CC_URL = f"http://localhost:{CC_PORT}"

# ── INFERENCE ──
INFERENCE_PORT = 9001
INFERENCE_URL = f"http://localhost:{INFERENCE_PORT}"

# ── KRAKEN ──
KRAKEN_REST = "https://api.kraken.com/0/public"
KRAKEN_FEE_TAKER = 0.0026
KRAKEN_FEE_MAKER = 0.0016

# ── TIMING ──
POLL_INTERVAL = 10            # CC poll cycle (seconds) — raised from 4s to cut 429s (240→96 req/min)
HTTP_TIMEOUT = 5              # bot-to-bot HTTP timeout
REQUEST_TIMEOUT = 3           # per-request timeout in CC
UNIVERSE_REFRESH_HOURS = 6
WATCHDOG_INTERVAL = 30        # seconds between watchdog sweeps
WATCHDOG_FAILURES = 3         # consecutive failures before restart
WATCHDOG_COOLDOWN = 300       # seconds between restart attempts per bot

# ── PORTFOLIO ──
PORTFOLIO_TOTAL = 10000.00
PORTFOLIO_LIMITS = {
    "max_deployed_pct": 80,
    "max_per_bot_pct": 30,
    "max_per_pair_pct": 20,
    "max_directional_pct": 60,
    "max_per_trade_pct": 5,
}
TRADING_BOTS = {"turtlesue", "nexusbrain", "gridzilla", "trekbot", "trekbot_short", "rubberband", "arbitrageur"}

# ── TRADING LIMITS ──
MAX_CONCENTRATION_PER_PAIR = 0.40
MAX_DIRECTION_IMBALANCE = 0.70
MIN_TRADE_PROFIT_VS_FEES = 2.0
MIN_TRADE_SIZE_USD = 5.0

# ── BOT REGISTRY ──
# Every bot defined once.  Keys match bot IDs used in CC, event bus, and portfolio.
# Fields used by launch_fleet.py:
#   cmd   — subprocess argv list; None means the bot has no standalone launcher
#   phase — 1 = starts before Command Center, 2 = starts after CC is up
#   slow  — True = bot needs ~30s to bind its port (adds a 2s inter-launch delay)
BOTS = {
    "turtlesue":   {"port": 8070, "dir": os.path.join(DATA_DRIVE, "TurtleSue"),                             "role": "trader",  "display": "TurtleSue",  "color": "#00e676", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "turtlebot.py", "--auto"],                    "phase": 1, "slow": False},
    "sentinel":    {"port": 8071, "dir": os.path.join(DATA_DRIVE, "Sentinel"),                               "role": "support", "display": "Sentinel",   "color": "#00bfa5", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "sentinel.py"],                               "phase": 2, "slow": True},   # needs CC market data
    "trinity":     {"port": 8072, "dir": os.path.join(DATA_DRIVE, "Trinity"),                                "role": "support", "display": "Trinity",    "color": "#00b0ff", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "overwatch.py", "--auto"],                    "phase": 1, "slow": False},
    "hivemind":    {"port": 8073, "dir": os.path.join(DATA_DRIVE, "HiveMind"),                               "role": "support", "display": "HiveMind",   "color": "#ffab00", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "cli.py", "dashboard", "--synthetic"],        "phase": 1, "slow": True},
    "nexusbrain":  {"port": 8074, "dir": os.path.join(DATA_DRIVE, "NexusBrain"),                             "role": "trader",  "display": "NexusBrain", "color": "#d500f9", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "nexus_brain.py", "dashboard", "--auto"],     "phase": 1, "slow": False},
    "oracle":      {"port": 8075, "dir": os.path.join(DATA_DRIVE, "Oracle"),                                 "role": "intel",   "display": "Oracle",     "color": "#76ff03", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "server.py"],                                 "phase": 1, "slow": True},
    "deepblue":    {"port": 8076, "dir": os.path.join(DATA_DRIVE, "Whale Watcher", "apex_whale_finder.dir"), "role": "intel",   "display": "Deep Blue",  "color": "#18ffff", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "main.py", "--headless"],                       "phase": 1, "slow": False},
    "gridzilla":   {"port": 8077, "dir": os.path.join(DATA_DRIVE, "Gridzilla"),                              "role": "trader",  "display": "Gridzilla",  "color": "#ffd600", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "gridzilla.py", "--auto"],                    "phase": 1, "slow": False},
    "phitex":      {"port": 8078, "dir": os.path.join(DATA_DRIVE, "PhiTex"),                                 "role": "support", "display": "PHITEX",     "color": "#e040fb", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "phitex.py"],                                 "phase": 2, "slow": True},
    "aegis":       {"port": 8079, "dir": os.path.join(DATA_DRIVE, "Aegis"),                                  "role": "support", "display": "AEGIS",      "color": "#e0e0e0", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "aegis.py"],                                  "phase": 2, "slow": False},
    "trekbot":     {"port": 8080, "dir": os.path.join(DATA_DRIVE, "TrekBot"),                                "role": "trader",  "display": "TrekBot",    "color": "#ff6d00", "endpoints": ["/health", "/positions", "/analytics"],  "cmd": ["python", "trekbot.py"],                                "phase": 1, "slow": True},
    "trekbot_short":{"port": 8087, "dir": os.path.join(DATA_DRIVE, "TrekBot"),                               "role": "trader",  "display": "TrekBot SHORT","color": "#ff3d00", "endpoints": ["/health", "/positions", "/analytics"],  "cmd": ["python", "trekbot.py", "--mode", "short"],             "phase": 2, "slow": True},
    "inference":   {"port": 9001, "dir": os.path.join(CC_DIR),                                               "role": "support", "display": "Inference",  "color": "#b0bec5", "endpoints": ["/health"],                              "cmd": ["python", "inference_server.py"],                       "phase": 2, "slow": False},
    "nexus":       {"port": 8082, "dir": os.path.join(DATA_DRIVE, "Nexus"),                                  "role": "intel",   "display": "NEXUS",      "color": "#26c6da", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "nexus.py"],                                  "phase": 2, "slow": False},
    "rubberband":  {"port": 8083, "dir": os.path.join(DATA_DRIVE, "Rubberband"),                             "role": "trader",  "display": "Rubberband", "color": "#00e5ff", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "rubberband.py", "--auto"],                   "phase": 2, "slow": False},
    "contrarian":  {"port": 8084, "dir": os.path.join(DATA_DRIVE, "Contrarian"),                             "role": "support", "display": "Contrarian", "color": "#ff1744", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "contrarian.py"],                             "phase": 2, "slow": False},
    "arbitrageur": {"port": 8085, "dir": os.path.join(DATA_DRIVE, "Arbitrageur"),                            "role": "trader",  "display": "Arbitrageur","color": "#7c4dff", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "arbitrageur.py"],                            "phase": 2, "slow": False},
    "chronos":     {"port": 8086, "dir": os.path.join(DATA_DRIVE, "Chronos"),                                "role": "support", "display": "Chronos",    "color": "#ff9100", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "chronos.py"],                                "phase": 2, "slow": False},
}

# ── PATHS ──
PIDS_DIR = os.path.join(CC_DIR, "pids")
AUDITS_DIR = os.path.join(CC_DIR, "audits")
BACKUPS_DIR = os.path.join(CC_DIR, "backups")
LOGS_DIR = os.path.join(CC_DIR, "logs")
BRAINIAC_DIR = os.path.join(CC_DIR, "brainiac")
PORTFOLIO_FILE = os.path.join(CC_DIR, "portfolio.json")

# Create dirs if missing
for _d in [PIDS_DIR, AUDITS_DIR, BACKUPS_DIR, LOGS_DIR]:
    os.makedirs(_d, exist_ok=True)

# ── ENGINE CONFIG ──
ENGINES = [
    "shannon", "boltzmann", "lorenz", "prigogine", "thom",
    "topology", "quantum_state", "info_geometry", "causal_flow",
    "propagation", "lens_dispersion", "temporal_echo", "pythagorean",
]

ENGINE_EVENT_MAP = {
    "info_geometry":  "MANIFOLD_WARNING",
    "topology":       "CYCLE_DETECTED",
    "quantum_state":  "QUANTUM_COLLAPSE",
    "causal_flow":    "CAUSAL_FLOW",
    "shannon":        "SHANNON_ENTROPY",
    "boltzmann":      "BOOK_PHASE",
    "lorenz":         "CHAOS_STATE",
    "prigogine":      "STRUCTURE_FORMING",
    "thom":           "CATASTROPHE_WARNING",
}

# ── PAIR BLACKLIST ──
# Pairs with 0% WR and deeply negative expectancy across fleet.
# Checked by CC portfolio manager — blocks capital reservation for these pairs.
BLACKLISTED_PAIRS = {
    "SOL/USD",     # 0% WR, -$9.74 net, 5 trades across rubberband+trekbot
    "DOT/USD",     # 0% WR, -$9.69 net (as DOTUSD via nexusbrain)
    "GBP/USD",     # 0% WR, -$9.19 net, 6 trades (rubberband)
}


def is_blacklisted(pair: str) -> bool:
    """Check if a pair is blacklisted (normalizes Kraken formats)."""
    from standards import normalize_pair as _np
    normalized = _np(pair) or pair
    return normalized in BLACKLISTED_PAIRS


# ── CONVENIENCE ──

def get_bot(bot_id: str) -> dict | None:
    """Look up a bot by ID (case-insensitive, tolerates hyphens/spaces)."""
    return BOTS.get(bot_id.lower().replace(" ", "_").replace("-", "_"))

def get_port(bot_id: str) -> int | None:
    b = get_bot(bot_id)
    return b["port"] if b else None

def get_url(bot_id: str) -> str | None:
    b = get_bot(bot_id)
    return f"http://localhost:{b['port']}" if b else None

def get_traders() -> dict:
    return {k: v for k, v in BOTS.items() if v["role"] == "trader"}

def get_intel() -> dict:
    return {k: v for k, v in BOTS.items() if v["role"] == "intel"}

def get_support() -> dict:
    return {k: v for k, v in BOTS.items() if v["role"] == "support"}

def bot_registry_list() -> list[dict]:
    """Return BOT_REGISTRY in the list-of-dicts format CC expects."""
    return [
        {"id": bid, "name": cfg["display"], "port": cfg["port"],
         "color": cfg["color"], "endpoints": cfg["endpoints"]}
        for bid, cfg in BOTS.items()
    ]


def get_deployment_limits(cc_unreachable: bool = False) -> dict:
    """Return deployment limits. When CC unreachable, use conservative fallback."""
    if cc_unreachable:
        return {
            "max_position_pct": 0.05,
            "max_pairs": 2,
            "max_daily_trades": 1,
        }
    return {
        "max_position_pct": 0.30,
        "max_pairs": 10,
        "max_daily_trades": 10,
    }
