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

# ── FLEET MODE ──
# "paper" = simulated fills only, no exchange orders (safe default)
# "live"  = real Kraken orders will be placed
# Toggle via: POST http://localhost:9000/api/fleet/mode  {"mode": "paper"|"live"}
# Bots check this at trade time, not just at startup.
FLEET_MODE = "paper"

# ── KRAKEN ──
KRAKEN_REST = "https://api.kraken.com/0/public"
# Fee schedule as of 2026-04: tier 0 ($0-$10K/month volume)
# Taker 0.40%, Maker 0.25%, Round-trip (taker both sides) 0.80%
KRAKEN_FEE_TAKER = 0.0040
KRAKEN_FEE_MAKER = 0.0025

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
    "max_per_trade_pct": 20,
}
TRADING_BOTS = {"turtlesue", "nexusbrain", "gridzilla", "rubberband", "arbitrageur", "confluence"}

# ── TRADING LIMITS ──
MAX_CONCENTRATION_PER_PAIR = 0.40
MAX_DIRECTION_IMBALANCE = 0.70
MIN_TRADE_PROFIT_VS_FEES = 2.0
MIN_TRADE_SIZE_USD = 0

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
    "nexusbrain":  {"port": 8074, "dir": os.path.join(DATA_DRIVE, "NexusBrain"),                             "role": "trader",  "display": "NexusBrain", "color": "#d500f9", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "nexus_brain.py", "run-sim", "--auto"],       "phase": 1, "slow": False},
    "oracle":      {"port": 8075, "dir": os.path.join(DATA_DRIVE, "Oracle"),                                 "role": "intel",   "display": "Oracle",     "color": "#76ff03", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "server.py"],                                 "phase": 1, "slow": True},
    "deepblue":    {"port": 8076, "dir": os.path.join(DATA_DRIVE, "Whale Watcher", "apex_whale_finder.dir"), "role": "intel",   "display": "Deep Blue",  "color": "#18ffff", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "main.py", "--headless"],                       "phase": 1, "slow": False},
    "gridzilla":   {"port": 8077, "dir": os.path.join(DATA_DRIVE, "Gridzilla"),                              "role": "trader",  "display": "Gridzilla",  "color": "#ffd600", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "gridzilla.py", "--auto"],                    "phase": 1, "slow": False},
    "phitex":      {"port": 8078, "dir": os.path.join(DATA_DRIVE, "PhiTex"),                                 "role": "support", "display": "PHITEX",     "color": "#e040fb", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "phitex.py"],                                 "phase": 2, "slow": True},
    "aegis":       {"port": 8079, "dir": os.path.join(DATA_DRIVE, "Aegis"),                                  "role": "support", "display": "AEGIS",      "color": "#e0e0e0", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "aegis.py"],                                  "phase": 2, "slow": False},
    # TrekBot was renamed GoldenEye and now runs standalone from D:\GoldenEye on
    # port 18095 with its own launcher (Desktop\launch.bat) and dashboard (18065).
    # It is deliberately NOT fleet-managed. Its slot is filled by "confluence".
    # Short mode is retired fleet-wide — no shorting.
    "confluence":  {"port": 8088, "dir": os.path.join(DATA_DRIVE, "Confluence"),                             "role": "trader",  "display": "Confluence", "color": "#ff6d00", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "confluence.py"],                             "phase": 2, "slow": False},
    "inference":   {"port": 9001, "dir": os.path.join(CC_DIR),                                               "role": "support", "display": "Inference",  "color": "#b0bec5", "endpoints": ["/health"],                              "cmd": ["python", "inference_server.py"],                       "phase": 2, "slow": False},
    "nexus":       {"port": 8082, "dir": os.path.join(DATA_DRIVE, "Nexus"),                                  "role": "intel",   "display": "NEXUS",      "color": "#26c6da", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "nexus.py"],                                  "phase": 2, "slow": False},
    "rubberband":  {"port": 8083, "dir": os.path.join(DATA_DRIVE, "Rubberband"),                             "role": "trader",  "display": "Rubberband", "color": "#00e5ff", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "rubberband.py", "--auto"],                   "phase": 2, "slow": False},
    "contrarian":  {"port": 8084, "dir": os.path.join(DATA_DRIVE, "Contrarian"),                             "role": "support", "display": "Contrarian", "color": "#ff1744", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "contrarian.py"],                             "phase": 2, "slow": False},
    "arbitrageur": {"port": 8085, "dir": os.path.join(DATA_DRIVE, "Arbitrageur"),                            "role": "trader",  "display": "Arbitrageur","color": "#7c4dff", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "arbitrageur.py"],                            "phase": 2, "slow": False},
    "chronos":     {"port": 8086, "dir": os.path.join(DATA_DRIVE, "Chronos"),                                "role": "support", "display": "Chronos",    "color": "#ff9100", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "chronos.py"],                                "phase": 2, "slow": False},
    "signal_broadcaster": {"port": 9002, "dir": CC_DIR,                                                        "role": "support", "display": "Broadcaster","color": "#c8a96e", "endpoints": ["/health"],                              "cmd": ["python", "signal_broadcaster.py"],                     "phase": 2, "slow": False},
    "bot_responder":      {"port": None, "dir": CC_DIR,                                                        "role": "support", "display": "Bot Responder","color": "#c8a96e", "endpoints": [],                                     "cmd": ["python", "bot_responder.py"],                          "phase": 2, "slow": False},
}

# ── PATHS ──
PIDS_DIR = os.path.join(CC_DIR, "pids")
AUDITS_DIR = os.path.join(CC_DIR, "audits")
BACKUPS_DIR = os.path.join(CC_DIR, "backups")
LOGS_DIR = os.path.join(CC_DIR, "logs")
BRAINIAC_DIR = os.path.join(CC_DIR, "brainiac")
PORTFOLIO_FILE = os.path.join(CC_DIR, "portfolio.json")       # paper (legacy compat)
PORTFOLIO_LIVE_FILE = os.path.join(CC_DIR, "portfolio_live.json")

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
# Two distinct reasons a pair lands here:
#   (a) 0% WR and deeply negative expectancy across the fleet
#   (b) not tradeable on Kraken at all — no OHLC, so no forecast and no fill
# Checked by CC portfolio manager — blocks capital reservation for these pairs.
BLACKLISTED_PAIRS = {
    # (a) negative expectancy
    "SOL/USD",     # 0% WR, -$9.74 net, 5 trades across rubberband+trekbot
    "DOT/USD",     # 0% WR, -$9.69 net (as DOTUSD via nexusbrain)
    "GBP/USD",     # 0% WR, -$9.19 net, 6 trades (rubberband)

    # (b) not on Kraken — Oracle signals these but they cannot be traded or
    #     confirmed. Kraken OHLC returns EQuery:Unknown asset pair, so Sentinel
    #     produces no forecast and Confluence can never reach multi-source
    #     agreement on them. Left unblocked they burn a candidate slot every
    #     cycle and show as a permanent overlap "miss".
    "XNO/USD",     # Kraken: EQuery:Unknown asset pair (verified 2026-07-28)
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
    """Return BOT_REGISTRY in the list-of-dicts format CC expects.
    Excludes bots with no port (e.g. bot_responder) since CC cannot poll them."""
    return [
        {"id": bid, "name": cfg["display"], "port": cfg["port"],
         "color": cfg["color"], "endpoints": cfg["endpoints"]}
        for bid, cfg in BOTS.items()
        if cfg["port"] is not None and cfg["endpoints"]
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


def is_live() -> bool:
    """True when fleet is in live trading mode. Bots call this before placing exchange orders."""
    return FLEET_MODE == "live"


# Live mode: LONG positions only. Shorts allowed in paper only.
# Shorting on Kraken spot requires margin (different fees, borrowing costs, liquidation risk)
# and the fleet has near-zero data in bear/range regimes to validate short strategies.
LIVE_LONG_ONLY = True

# Shorting is retired FLEET-WIDE, in paper as well as live.
#
# LIVE_LONG_ONLY above only bites once FLEET_MODE == "live", so in paper mode
# bots could still open shorts freely — TurtleSue was observed holding an
# AAVE/USD SHORT while the fleet was nominally long-only. Paper positions feed
# expectancy, signal decomposition and every WR/attribution statistic the fleet
# tunes itself on, so shorts that will never be traded live were still steering
# the fleet's learning.
#
# Set False to re-enable short entries in paper for research.
FLEET_LONG_ONLY = True


def direction_allowed(direction: str, is_reentry: bool = False) -> tuple:
    """Return (allowed, reason) for reserving capital in `direction`.

    Applies in BOTH paper and live mode — see FLEET_LONG_ONLY.

    `is_reentry=True` marks a re-reservation for a position that is ALREADY
    open (e.g. a bot restarting and re-claiming capital for existing trades).
    Those must be allowed through: the policy is "open no new shorts", not
    "liquidate open ones". Blocking them makes a bot read its own position as
    unfunded and force-close it at market — turning a config change into an
    unintended liquidation.
    """
    d = (direction or "").upper()
    if d != "SHORT":
        return True, ""
    if is_reentry:
        return True, ""
    if FLEET_LONG_ONLY:
        return False, "FLEET_LONG_ONLY: short entries are retired fleet-wide"
    if FLEET_MODE == "live" and LIVE_LONG_ONLY:
        return False, "LIVE_LONG_ONLY: SHORT positions blocked in live mode"
    return True, ""


def live_direction_allowed(direction: str) -> bool:
    """Backwards-compatible boolean wrapper around direction_allowed()."""
    return direction_allowed(direction)[0]


# 3-state engage model: paper → live_armed → live_engaged
# paper: all bots on paper portfolio, normal operation
# live_armed: live portfolio is display, but no trading until ENGAGE
# live_engaged: live bots trade on live portfolio, paper bots continue on paper
FLEET_ENGAGE_STATE = "paper"

# Bots that can execute real Kraken orders
LIVE_CAPABLE_BOTS = {"turtlesue", "nexusbrain", "gridzilla", "rubberband", "arbitrageur", "confluence"}


def set_fleet_mode(mode: str) -> str:
    """Set fleet mode. Accepts: paper, live, engage, disengage. Returns new engage state."""
    global FLEET_MODE, FLEET_ENGAGE_STATE
    mode = mode.lower().strip()
    if mode == "paper":
        FLEET_MODE = "paper"
        FLEET_ENGAGE_STATE = "paper"
    elif mode == "live":
        FLEET_MODE = "live"
        FLEET_ENGAGE_STATE = "live_armed"
    elif mode == "engage":
        if FLEET_MODE != "live":
            raise ValueError("Cannot engage: not in live mode")
        FLEET_ENGAGE_STATE = "live_engaged"
    elif mode == "disengage":
        if FLEET_MODE != "live":
            raise ValueError("Cannot disengage: not in live mode")
        FLEET_ENGAGE_STATE = "live_armed"
    else:
        raise ValueError(f"Invalid mode: {mode!r} — must be 'paper', 'live', 'engage', or 'disengage'")
    return FLEET_ENGAGE_STATE


def is_engaged() -> bool:
    """True when fleet is live AND engaged (trades executing)."""
    return FLEET_ENGAGE_STATE == "live_engaged"
