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
# DEPRECATED (2026-07-30): the fleet is a signal product — it never pays
# exchange fees; subscribers pay whatever their own venue charges. These
# constants are kept only so bots not yet migrated off them don't crash on
# import; at 0.0 any leftover fee math computes to zero (gross P/L).
KRAKEN_FEE_TAKER = 0.0
KRAKEN_FEE_MAKER = 0.0

# ── TIMING ──
POLL_INTERVAL = 10            # CC poll cycle (seconds) — raised from 4s to cut 429s (240→96 req/min)
HTTP_TIMEOUT = 5              # bot-to-bot HTTP timeout
REQUEST_TIMEOUT = 3           # per-request timeout in CC
UNIVERSE_REFRESH_HOURS = 6
WATCHDOG_INTERVAL = 30        # seconds between watchdog sweeps
WATCHDOG_FAILURES = 3         # consecutive failures before restart
WATCHDOG_COOLDOWN = 300       # seconds between restart attempts per bot

# ── PORTFOLIO ──
# Operator-set pool size (2026-08-13). portfolio.json's `total` overrides
# this once the file exists — see PortfolioManager._load — so BOTH are set
# to the same figure. Leaving them divergent means deleting or corrupting
# the state file silently restores a different pool size.
#
# Note the pool is not static: release() does `self.total += pnl`, so
# realized P/L moves it from here. This is the STARTING size.
PORTFOLIO_TOTAL = 210.53
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
# DEPRECATED (2026-07-30): fee-relative profit gate retired — signal product,
# no fleet-side fees. Kept at 0.0 so bots still reading it gate on nothing.
MIN_TRADE_PROFIT_VS_FEES = 0.0
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
    # HiveMind ran with --synthetic in production until 2026-07-29. That feeds
    # generate_synthetic_returns() -- random Student-t draws with a +27.4%/yr
    # drift baked in by construction -- and labels the columns BTC/ETH/SOL/...
    # so the output read as live allocations. It reported Sharpe 2.83 and a
    # "bull" regime while the same code on real Kraken prices gave Sharpe -1.01
    # and BEAR: a 3.84-unit swing of pure fiction, published into the fleet's
    # REGIME SOURCES panel next to genuinely-measured regimes.
    "hivemind":    {"port": 8073, "dir": os.path.join(DATA_DRIVE, "HiveMind"),                               "role": "support", "display": "HiveMind",   "color": "#ffab00", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "cli.py", "dashboard"],                       "phase": 1, "slow": True},
    "nexusbrain":  {"port": 8074, "dir": os.path.join(DATA_DRIVE, "NexusBrain"),                             "role": "trader",  "display": "NexusBrain", "color": "#d500f9", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "nexus_brain.py", "run-sim", "--auto"],       "phase": 1, "slow": False},
    "oracle":      {"port": 8075, "dir": os.path.join(DATA_DRIVE, "Oracle"),                                 "role": "intel",   "display": "Oracle",     "color": "#76ff03", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "server.py"],                                 "phase": 1, "slow": True},
    "deepblue":    {"port": 8076, "dir": os.path.join(DATA_DRIVE, "Whale Watcher", "apex_whale_finder.dir"), "role": "intel",   "display": "Deep Blue",  "color": "#18ffff", "endpoints": ["/api/snapshot"],                        "cmd": ["python", "main.py", "--headless"],                       "phase": 1, "slow": False},
    # GridPick executor -- cloned from Gridzilla 2026-09-19 with its own port,
    # state file and bot id. Registered here because LIVE_CAPABLE_BOTS entries
    # must be real fleet members: an armable bot the fleet cannot launch or
    # monitor is exactly the gap this registration closes.
    # phase 9 = NOT auto-launched; run by hand while the scanner-to-executor
    # wiring is still being built.
    "gridpick":    {"port": 8089, "dir": os.path.join(DATA_DRIVE, "GridPick", "executor"), "role": "trader", "display": "GridPick", "color": "#00e5ff", "endpoints": ["/api/snapshot"], "cmd": ["python", "gridpick_executor.py", "--auto"], "phase": 9, "slow": False},
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
    # Broadcaster polls TWO endpoints: CC's _fetch_bot merges multi-endpoint
    # bots key-namespaced ({"health": {...}, "stats": {...}}). /stats is the
    # LIVE process truth (sse state, per-channel sent/failed, queue sizes) —
    # CC's /api/signals/broadcaster/stats route is log-derived and can drift.
    "signal_broadcaster": {"port": 9002, "dir": CC_DIR,                                                        "role": "support", "display": "Broadcaster","color": "#c8a96e", "endpoints": ["/health", "/stats"],                    "cmd": ["python", "signal_broadcaster.py"],                     "phase": 2, "slow": False},
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
# Shorting on Kraken spot requires margin (borrowing mechanics, liquidation risk)
# and the fleet has near-zero data in bear/range regimes to validate short strategies.
LIVE_LONG_ONLY = True

# Shorts re-enabled fleet-wide in paper (2026-07-30). The fleet is a SIGNAL
# PRODUCT now — it will never trade real money itself; subscribers in
# jurisdictions that allow shorting need short signals. That inverts the old
# ban rationale: short expectancy/WR stats are no longer pollution steering
# the fleet's learning toward trades it would never take — they are quality
# control on a deliverable. LIVE_LONG_ONLY above still blocks shorts if the
# fleet is ever flipped live.
#
# Set True to retire short entries again (open shorts survive via is_reentry).
#
# ON since 2026-08-13, by operator decision: the fleet is long-only. Verified
# no SHORT positions were open at the time of the change, so nothing needed
# grandfathering — and is_reentry above would have covered them anyway, since
# the policy is "open no new shorts", not "liquidate open ones".
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
    # SHORT and SELL both mean "short entry" here. Every current bot reserves
    # with SHORT — the SELLs in the fleet are ORDER SIDES (closing a long, a
    # grid sell level), which never reach this function. Accepting the
    # synonym anyway means a bot that later adopts that spelling cannot slip
    # a short past a long-only fleet, which is the kind of gap that is only
    # discovered after it has been exploited.
    d = (direction or "").upper()
    if d not in ("SHORT", "SELL"):
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


# ── ORDER EXECUTION ──
# LIMIT ORDERS ONLY, fleet-wide, no exceptions. Market orders are refused at
# the Kraken client. A market order is a blank cheque on fill price: on a thin
# book it can slip well past the level the strategy chose — execution realism,
# and the signal a subscriber receives must be a price the strategy actually
# picked, not whatever the book happened to fill at.
#
# A limit order placed exactly at the last trade may never fill. So orders are
# priced MARKETABLE: cross the spread by this fraction, which fills like a
# market order in normal conditions but caps the worst case — if the book has
# gapped further than this, the order rests unfilled instead of eating the gap.
LIMIT_CROSS_PCT = 0.0015        # 15 bps


def limit_price(reference_price: float, direction: str,
                cross_pct: float = None) -> float:
    """Marketable limit price for `direction` around `reference_price`.

    BUY  -> slightly ABOVE reference (willing to pay up to this)
    SELL -> slightly BELOW reference (willing to accept down to this)

    Returns 0.0 for a non-positive reference, which the client rejects rather
    than turning into an unpriced order.
    """
    try:
        ref = float(reference_price)
    except (TypeError, ValueError):
        return 0.0
    if ref <= 0:
        return 0.0
    pct = LIMIT_CROSS_PCT if cross_pct is None else float(cross_pct)
    d = (direction or "").upper()
    if d in ("LONG", "BUY"):
        return ref * (1.0 + pct)
    return ref * (1.0 - pct)


# 3-state engage model: paper → live_armed → live_engaged
# paper: all bots on paper portfolio, normal operation
# live_armed: live portfolio is display, but no trading until ENGAGE
# live_engaged: live bots trade on live portfolio, paper bots continue on paper
FLEET_ENGAGE_STATE = "paper"

# Bots that can execute real Kraken orders
# "gridpick" is the GridPick executor (D:/GridPick/executor), cloned from
# Gridzilla 2026-09-19. It is listed here so it CAN be armed individually;
# listing is not arming -- LIVE_ARMED_BOTS is still empty and each bot also
# needs <BOT>_LIVE_ARM=1 on its own launcher.
LIVE_CAPABLE_BOTS = {"turtlesue", "nexusbrain", "gridzilla", "rubberband",
                     "arbitrageur", "confluence", "gridpick"}


# ── PER-BOT LIVE ARMING ──────────────────────────────────────────────────────
# FLEET_MODE is ONE switch read by seven bots (Arbitrageur, Confluence,
# Gridzilla, NexusBrain, Rubberband, TurtleSue, and this file). Flipping it to
# "live" arms every one of them that holds credentials, at the same instant.
# There was no way to take a single bot live.
#
# LIVE_ARMED_BOTS is the missing dimension: a bot must ALSO name itself here
# before it may place a real order. Empty by default, and deliberately not
# settable from the fleet-mode API -- arming is an out-of-band act (an env var
# on that bot's own launcher), so that no single HTTP call can take money live.
#
# This does NOT change is_live(). Bots that have not opted in behave exactly as
# before; is_bot_live() is a STRICTER check a bot opts into.
LIVE_ARMED_BOTS: set = set()


def _load_armed_bots() -> set:
    """Read per-bot arming from the environment at import.

    `<BOT>_LIVE_ARM=1` arms that bot, e.g. GRIDZILLA_LIVE_ARM=1. The variable
    lives on the bot's own launcher, so arming is visible in the thing the
    operator starts rather than buried in shared state.
    """
    # Derived from LIVE_CAPABLE_BOTS, never hand-listed. The first version of
    # this function carried a typed-out list that invented four bot names
    # (viper, trinity, hivemind, oracle) which are not live-capable, in a
    # fleet of 19 registered bots where the authoritative set was defined
    # lower in this same file. A roster that can drift from the roster is a
    # second source of truth, and this one governs real orders.
    armed = set()
    for bot in LIVE_CAPABLE_BOTS:
        if os.environ.get(f"{bot.upper()}_LIVE_ARM", "").strip() in ("1", "true", "yes", "on"):
            armed.add(bot)
    return armed


LIVE_ARMED_BOTS |= _load_armed_bots()


def is_bot_live(bot: str) -> bool:
    """True only when ALL THREE hold: the fleet is live, it is engaged, and
    THIS bot is individually armed.

    Any one of them false means simulate. A bot calls this instead of
    is_live() before placing a real exchange order.
    """
    return is_live() and is_engaged() and bot.lower() in LIVE_ARMED_BOTS


def why_not_live(bot: str) -> str:
    """Human-readable reason a bot is not trading live, for the startup log.

    A bot that silently stays paper when the operator believes it is live --
    or silently goes live -- is the failure this whole gate exists to prevent.
    Whichever branch is taken, the log must say which and why.
    """
    if is_bot_live(bot):
        return (f"LIVE: fleet_mode={FLEET_MODE}, engage={FLEET_ENGAGE_STATE}, "
                f"{bot} armed")
    reasons = []
    if not is_live():
        reasons.append(f"fleet_mode={FLEET_MODE!r} (need 'live')")
    if not is_engaged():
        reasons.append(f"engage={FLEET_ENGAGE_STATE!r} (need 'live_engaged')")
    if bot.lower() not in LIVE_ARMED_BOTS:
        reasons.append(f"{bot} not armed (set {bot.upper()}_LIVE_ARM=1)")
    return "PAPER: " + "; ".join(reasons)


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
