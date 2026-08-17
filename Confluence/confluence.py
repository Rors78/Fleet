#!/usr/bin/env python3
"""
CONFLUENCE — Intel-Driven Fleet Trader
======================================
Port 8088. Kraken spot, bidirectional (LONG + SHORT), paper-gated.
Shorts re-enabled fleet-wide 2026-07-30 (fleet_config.FLEET_LONG_ONLY = False)
— the fleet is a signal product; short entries obey fleet_config.direction_allowed()
per-entry and are held to the same conviction gates as longs.

What makes this bot different from the other five traders:
  Rubberband, Arbitrageur, Gridzilla, TurtleSue and NexusBrain all read
  *charts*. This one reads *the fleet*. It holds no indicators of its own —
  its entire edge is aggregating intel the fleet already produces but that
  nothing currently trades on:

    Oracle    :8075  — scored trade candidates (entry/stop/target/rr/hurst)
    Deep Blue :8076  — whale accumulation scores + reliability
    NEXUS     :8082  — market character + volatility forecast
    Sentinel  :8071  — per-pair forecasts

An entry requires multi-source agreement. One source alone is never enough,
which is the whole point: Oracle already publishes signals, and acting on
them unfiltered would just be Oracle with extra steps.

Run:
    python confluence.py              # paper (default)
    python confluence.py --port 8088
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.request as urlreq
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# ── Fleet integration (soft imports — bot must run standalone too) ──
sys.path.insert(0, r"D:\CommandCenter")

try:
    from portfolio_client import PortfolioClient
except Exception:
    PortfolioClient = None

try:
    from event_publisher import EventPublisher
except Exception:
    EventPublisher = None

try:
    from bus_listener import BusListener
except Exception:
    BusListener = None

try:
    import fleet_config as _fc
except Exception:
    _fc = None

try:
    from fleet_config import is_blacklisted as _is_blacklisted
except Exception:
    def _is_blacklisted(pair):
        return False


# ── Constants ──────────────────────────────────────────────────────
BOT_ID = "confluence"
BOT_NAME = "Confluence"
DEFAULT_PORT = 8088
CC_URL = "http://127.0.0.1:9000"

ORACLE_URL = "http://127.0.0.1:8075/api/snapshot"
DEEPBLUE_URL = "http://127.0.0.1:8076/api/snapshot"
NEXUS_URL = "http://127.0.0.1:8082/api/snapshot"
SENTINEL_URL = "http://127.0.0.1:8071/api/snapshot"

SCAN_INTERVAL_S = 60          # intel refresh cadence
INTEL_TIMEOUT_S = 5
INTEL_STALE_S = 300           # intel older than this is not tradeable

# Entry gates
# Weighted agreement floor [0,1], scored against *available* source weight.
# Calibration basis (2026-07-28 live intel): with Oracle scores in the 55-60
# band and NEXUS at MIXED, candidates land 0.45-0.56. 0.55 admits only the
# strongest of a mediocre field; a stronger tape (Oracle 70+, NEXUS TRENDING,
# whale present) clears 0.70+ comfortably. Raise this after the first 20-30
# closed trades give a real win-rate to calibrate against.
MIN_CONFLUENCE = 0.55
MIN_SOURCES = 2               # distinct intel sources that must agree
MIN_ORACLE_SCORE = 55.0       # Oracle's own conviction floor
MIN_RR = 1.8                  # reject thin reward:risk
MIN_WHALE_SCORE = 60.0        # Deep Blue accumulation floor (when present)

# Sentinel 1h expected-drift that counts as full conviction.
#
# Calibrated against the live forecast distribution (53 pairs, 2026-07-28):
#   max 0.149% (MANA/USD) · p90 0.111% · median 0.037%
# The original 1.0% was roughly 8x too large — no real forecast ever came
# close, so conv_norm landed near 0.03 against a 0.5 floor and Sentinel could
# never confirm anything. 0.12% sits just above p90, so the strongest ~10% of
# bullish forecasts clear the bar and the rest scale proportionally.
SENTINEL_FULL_CONVICTION_DRIFT = 0.0012

# Source weights — Oracle is the only source giving a full trade thesis,
# so it anchors. The rest confirm or veto.
W_ORACLE = 0.45
W_WHALE = 0.25
W_NEXUS = 0.18
W_SENTINEL = 0.12

# Risk
MAX_OPEN_POSITIONS = 3

# ── Position sizing ──────────────────────────────────────────────────────
# Was a flat POSITION_SIZE_USD = 550.0, justified by CC's old size floor of
# 5% of a $9,970 pool. Both halves of that justification are gone: the pool is
# $1,000,000 and the floor became an absolute $100 on 2026-08-05.
#
# The deeper problem with a flat size is that it makes RISK a function of stop
# distance rather than conviction. Measured live on 2026-08-05, risk per
# position ranged $3.37 to $12.65 — nearly 4x — purely because stops ranged
# 0.61% to 2.30%. That is backwards: the tight-stop setup is usually the
# higher-confidence one, and it was taking the smallest risk.
#
# Sizing is now risk-first, the same principle TurtleSue uses (risk/N), just
# expressed through the stop Oracle already provides:
#
#     size = (pool_share * risk_pct) / stop_distance_pct
#
# so every position risks the same dollars regardless of where the stop sits.
RISK_POOL_SHARE_PCT = 10.0     # share of the CC pool this bot sizes against
RISK_PER_TRADE_PCT = 0.5       # of that share — $500 at a $100k basis
# There is deliberately NO fallback equity constant. FALLBACK_EQUITY_USD =
# 10000.0 used to stand here as the "basis when CC is unreachable"; against
# the real $209.88 pool it sized positions 476x too large — a single position
# at 12x the entire pool ($2,500 vs $5.25). MAX_POSITION_PCT_OF_POOL did not
# contain it, because _position_size back-derives its bounds from the basis
# it is handed, so the ceiling inflated from $31.48 to $15,000 in lockstep.
# An unreadable pool is not a $10,000 pool. Unreadable must fail ARMED:
# _sizing_basis returns None and the trade is skipped.

# Denominator for the reported pnl_pct, and the base of the notional "equity"
# index in snapshot(). This bot holds no capital of its own; both figures are
# presentational. Nothing may size against these, and nothing may sum them
# across bots as if they were money.
NOTIONAL_PNL_BASE = 10000.0

# Guard rails on the derived size. A near-zero stop would otherwise divide its
# way to an enormous position; a huge stop would produce dust.
#
# MIN_STOP_PCT_FOR_SIZING, not MAX_POSITION_USD, is the real defence against a
# tiny stop: capping the notional silently breaks the equal-risk property for
# exactly the tightest-stop trades, which are usually the best setups. At a
# 0.4% denominator floor and a $500 risk budget the derived size tops out
# around $125k, so the ceiling below only ever catches genuinely absurd input
# and is set above that natural maximum rather than inside it.
# These are FRACTIONS OF THE POOL, not dollar figures. They were $500 and
# $150,000 — sized for a $1M pool — and when the pool became $210.53 the
# $500 minimum FORCED every position to $500, which the pool then refused
# with "0.00 deployed + 500.00 requested > 126.32 cap". The bot computed a
# correct $3.51 and the floor overrode it. Ten denials in four minutes,
# and no trades at all.
#
# A hardcoded dollar bound goes stale at every resize; this is the same
# lesson as MIN_TRADE_USD, one layer up. Expressed as fractions they follow
# the pool automatically.
MIN_POSITION_PCT_OF_POOL = 0.005   # 0.5% — $1.05 at $210, $5k at $1M
MIN_STOP_PCT_FOR_SIZING = 0.004    # 0.4% — floor on the sizing denominator
MAX_POSITION_PCT_OF_POOL = 0.15    # 15% — the old $150k at a $1M pool
STOP_LOSS_PCT = 0.025          # 2.5% hard floor if Oracle gives no stop
MAX_POSITION_AGE_H = 36
# Signal-worthiness floor: the projected entry→target move must be at least
# this fraction of entry for a candidate to be worth broadcasting to
# subscribers. This is a quality gate, NOT a fee model — the fleet is a
# signal product and never pays exchange fees itself (subscribers pay their
# own exchanges' fees). Value preserved from the retired fee gate it
# replaced: 0.40% taker × 2 legs × 2 safety multiple = 1.6%.
MIN_TARGET_MOVE_PCT = 0.016
COOLDOWN_AFTER_LOSS_S = 900
SAME_PAIR_COOLDOWN_S = 3600

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "confluence_state.json")


def _now():
    return time.time()


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _get_json(url, timeout=INTEL_TIMEOUT_S):
    """Fetch JSON, returning None on any failure. Intel is best-effort."""
    try:
        resp = urlreq.urlopen(url, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# ── Position ───────────────────────────────────────────────────────

class Position:
    def __init__(self, pair, direction, entry, size_usd, stop, target, reservation_id, thesis):
        self.pair = pair
        self.direction = direction if direction in ("LONG", "SHORT") else "LONG"
        self.entry = entry
        self.size_usd = size_usd
        self.stop = stop            # SHORT: above entry. LONG: below entry.
        self.target = target        # SHORT: below entry. LONG: above entry.
        self.reservation_id = reservation_id
        self.thesis = thesis
        self.opened_at = _now()
        # Favorable extreme reached so far: the highest price for a LONG, the
        # lowest for a SHORT. Kept under the historical attribute name.
        self.high_water = entry

    @property
    def age_h(self):
        return (_now() - self.opened_at) / 3600.0

    def unrealized(self, price):
        if not self.entry:
            return 0.0
        if self.direction == "SHORT":
            # Paper short PnL: profit when price falls below entry.
            return (self.entry - price) / self.entry * self.size_usd
        return (price - self.entry) / self.entry * self.size_usd

    def to_dict(self, price=None):
        d = {
            "pair": self.pair,
            "direction": self.direction,
            "entry": self.entry,
            "size_usd": self.size_usd,
            "stop": self.stop,
            "target": self.target,
            "opened_at": self.opened_at,
            "age_h": round(self.age_h, 2),
            "thesis": self.thesis,
            "reservation_id": self.reservation_id,
        }
        if price:
            d["current_price"] = price
            d["unrealized_pnl"] = round(self.unrealized(price), 2)
        return d


# ── Engine ─────────────────────────────────────────────────────────

class ConfluenceEngine:
    def __init__(self):
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[dict] = []
        self.logs: list[dict] = []
        self.cycle = 0
        self.started_at = _now()
        self.last_scan = None
        self.last_scan_duration = None
        self.candidates: list[dict] = []
        self.rejections: list[dict] = []
        self.intel_status = {}
        self.pair_cooldowns: dict[str, float] = {}
        self.loss_cooldown_until = 0.0
        self.realized_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self._lock = threading.RLock()

        self._portfolio = None
        self._events = None
        self._bus = None
        self._pool_basis_warned = False
        self._init_fleet()
        self._load_state()

    # ── position sizing ──
    def _sizing_basis(self):
        """Capital this bot sizes against: a share of the CC pool.

        Returns None when the pool cannot be read. That is the whole point:
        this used to return FALLBACK_EQUITY_USD (10000.0), which against the
        real ~$210 pool sized positions 476x too large and put a single
        position at 12x the entire pool. The bounds in _position_size are
        derived from this basis, so a wrong basis inflates its own safety cap
        and the guard reads as protective while measuring against the lie.

        An unreadable pool is not a large pool. It is an unknown one, and the
        only safe response to an unknown basis is to not trade on it.
        """
        if not self._portfolio or RISK_POOL_SHARE_PCT <= 0:
            self._warn_no_basis("no portfolio client")
            return None
        total = None
        try:
            total = self._portfolio.pool_total()
        except Exception as e:
            self._warn_no_basis(f"pool_total() raised {e!r}")
            return None
        if not isinstance(total, (int, float)) or total <= 0:
            self._warn_no_basis(f"pool_total() returned {total!r}")
            return None
        self._pool_basis_warned = False
        return total * (RISK_POOL_SHARE_PCT / 100.0)

    def _warn_no_basis(self, why: str):
        """Say it once per outage, not every scan."""
        if not self._pool_basis_warned:
            self._pool_basis_warned = True
            self._log(
                f"Pool total unreadable ({why}) — NOT sizing. Trades are "
                f"skipped until the pool reads again; sizing off a guess "
                f"would put one position at many times the pool.", "WARNING")

    def _position_size(self, entry: float, stop: float) -> tuple:
        """Risk-normalized size in USD. Returns (size, basis, risk_usd, stop_pct).

        Every position risks the same dollars; the stop distance decides the
        notional, not the other way round.
        """
        basis = self._sizing_basis()
        if not isinstance(basis, (int, float)) or basis <= 0:
            # Pool unreadable. Return a zero size with a None basis so the
            # caller can tell "could not size" from "sized small" — those are
            # different facts and must not share a representation.
            return 0.0, None, 0.0, 0.0
        risk_usd = basis * (RISK_PER_TRADE_PCT / 100.0)

        stop_pct = abs(entry - stop) / entry if entry else STOP_LOSS_PCT
        # Floor the denominator: a 0.05% stop would otherwise divide its way
        # to a position many times the pool.
        stop_pct_eff = max(stop_pct, MIN_STOP_PCT_FOR_SIZING)

        size = risk_usd / stop_pct_eff
        # Bounds derived from the POOL, not hardcoded dollars. basis is
        # RISK_POOL_SHARE_PCT of the pool, so the pool is basis / share.
        # Deriving them here rather than reading pool_total() again keeps
        # the whole calculation on ONE reading — a second fetch could
        # return a different number mid-calculation and the bounds would
        # not match the risk they are bounding.
        _pool = basis / (RISK_POOL_SHARE_PCT / 100.0) if RISK_POOL_SHARE_PCT else basis
        _min = _pool * MIN_POSITION_PCT_OF_POOL
        _max = _pool * MAX_POSITION_PCT_OF_POOL
        size = max(_min, min(size, _max))
        return size, basis, risk_usd, stop_pct

    # ── fleet wiring ──
    def _init_fleet(self):
        if PortfolioClient:
            try:
                self._portfolio = PortfolioClient(CC_URL, BOT_ID)
                self._log("PortfolioClient connected", "INFO")
            except Exception as e:
                self._log(f"PortfolioClient init failed: {e}", "WARNING")
        else:
            self._log("PortfolioClient unavailable — running unreserved", "WARNING")

        if EventPublisher:
            try:
                self._events = EventPublisher(CC_URL, BOT_ID)
                self._log("EventPublisher connected", "INFO")
            except Exception as e:
                self._log(f"EventPublisher init failed: {e}", "WARNING")

        if BusListener:
            try:
                self._bus = BusListener(CC_URL)
                self._log("BusListener connected", "INFO")
            except Exception as e:
                self._log(f"BusListener init failed: {e}", "WARNING")

    def is_live(self):
        """Never execute real orders unless fleet_config says live.

        Direction legality is NOT checked here — a bidirectional bot's mode
        cannot hinge on any single direction. Each entry checks its own
        direction via _direction_allowed() at scoring and reservation time.
        (Live execution remains unimplemented regardless — see try_enter.)
        """
        if not _fc:
            return False
        try:
            return bool(_fc.is_live())
        except Exception:
            return False

    def _direction_allowed(self, direction, is_reentry=False):
        """(ok, reason) from fleet policy for holding capital in `direction`.

        Wraps fleet_config.direction_allowed(direction, is_reentry) — which
        enforces FLEET_LONG_ONLY / LIVE_LONG_ONLY and lets re-reservations for
        already-open positions through even after a policy flip.

        Falls back to the historical LONG-only stance when fleet_config (or
        its direction_allowed API) is unavailable: a policy we cannot read
        must not default to permitting shorts.
        """
        if _fc:
            try:
                return _fc.direction_allowed(direction, is_reentry=is_reentry)
            except Exception:
                pass
        if (direction or "").upper() == "LONG":
            return True, ""
        return False, "fleet direction policy unreadable — LONG-only fallback"

    def _log(self, msg, level="INFO"):
        entry = {"ts": _utc(), "level": level, "message": msg}
        self.logs.append(entry)
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]
        print(f"[{level}] {msg}", flush=True)

    def _emit(self, etype, data):
        if not self._events:
            return
        try:
            self._events.publish({"source": BOT_ID, "type": etype, "data": data})
        except Exception:
            pass

    # ── state ──
    def _save_state(self):
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "positions": {p: v.to_dict() for p, v in self.positions.items()},
                    "closed_trades": self.closed_trades[-200:],
                    "realized_pnl": self.realized_pnl,
                    "wins": self.wins,
                    "losses": self.losses,
                    "pair_cooldowns": self.pair_cooldowns,
                }, f, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            self._log(f"State save failed: {e}", "WARNING")

    def _load_state(self):
        """Restore state from disk.

        Sets self._state_unreadable when the file EXISTS but cannot be
        parsed. Three things fail OPEN on an unreadable read:

          - pair_cooldowns stays empty, and the gate reads
            `.get(pair, 0)`, so `_now() - 0` always exceeds
            SAME_PAIR_COOLDOWN_S and re-entry on any pair is permitted
            immediately.
          - realized_pnl / wins / losses reset to 0.0/0/0, so the bot
            reports a clean slate — the failure LOOKS like a fresh healthy
            start rather than lost history.
          - positions come back empty while their reservations stay booked
            in the pool.

        The restore is also a long sequential block, so a failure PART WAY
        through leaves earlier fields set and later ones at defaults. That
        partial state is just as unknown as none of it, and is treated the
        same way.

        Unreadable therefore fails toward ARMED: new entries are refused
        and the file is preserved. Mirrors TurtleSue's _load_positions.
        """
        self._state_unreadable = False
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            self.realized_pnl = d.get("realized_pnl", 0.0)
            self.wins = d.get("wins", 0)
            self.losses = d.get("losses", 0)
            self.closed_trades = d.get("closed_trades", [])

            # Legacy rows: trades closed before the gross-only rule (2026-07-30)
            # recorded net_pnl = gross_pnl - fees. realized_pnl accumulated the
            # NET figure, so those fees rode forward forever — the bot reported
            # -$23.69 when its gross P/L was -$14.90, an $8.80 phantom loss on
            # a signal product that never pays fees. Restate to gross on load
            # rather than editing history: the rows keep their original numbers
            # and gain a marker, and the running total is corrected once.
            _drift = 0.0
            for t in self.closed_trades:
                _fees = t.get("fees") or 0
                if _fees and not t.get("restated_gross"):
                    _gross = t.get("gross_pnl")
                    if isinstance(_gross, (int, float)):
                        _drift += _gross - (t.get("net_pnl") or 0)
                        t["net_pnl"] = round(_gross, 2)
                        t["fees"] = 0.0
                        t["restated_gross"] = True
            if _drift:
                self.realized_pnl += _drift
                self._log(
                    f"Restated {_drift:+.2f} of legacy fee drag to gross — "
                    f"P/L is gross fleet-wide (subscribers pay their own fees)",
                    "INFO")
            self.pair_cooldowns = d.get("pair_cooldowns", {})
            for pair, pd in (d.get("positions") or {}).items():
                # Direction restored from state; legacy state files predate
                # shorts and carried only LONGs, so that is the safe default.
                # An open SHORT surviving a restart is policy-legal even if
                # FLEET_LONG_ONLY flips back on (direction_allowed's
                # is_reentry semantics: "open no new shorts", not "liquidate
                # open ones") — restoring it here is the re-reservation path.
                # Legacy files (pre-shorts) genuinely carried only LONGs, so
                # LONG remains the fallback — but it is no longer SILENT.
                # Current state files all record a direction, so a missing one
                # now means corruption, and quietly restoring a live SHORT as
                # a LONG inverts its P/L, its stop and its target. A restart
                # is exactly when nobody is watching, so this must announce
                # itself rather than guess in the dark.
                _pdir = pd.get("direction")
                if not _pdir:
                    self._log(
                        f"State for {pair} has NO direction — assuming LONG "
                        f"(legacy file). If this position is actually short, "
                        f"its P/L, stop and target are now inverted.",
                        "WARNING")
                    _pdir = "LONG"
                pos = Position(pair, _pdir,
                               pd["entry"], pd["size_usd"], pd["stop"],
                               pd["target"], pd.get("reservation_id"), pd.get("thesis", {}))
                pos.opened_at = pd.get("opened_at", _now())
                self.positions[pair] = pos
            if self.positions:
                self._log(f"Restored {len(self.positions)} position(s) from state", "INFO")
        except Exception as e:
            # The file exists and could not be read (or only partly read).
            # A clean-looking zero state is the most dangerous outcome here:
            # it disarms the pair cooldown AND reports a fresh slate.
            self._state_unreadable = True
            self.positions = {}
            self.pair_cooldowns = {}
            self._log(
                f"UNREADABLE STATE {STATE_FILE}: {e} — positions, pair "
                f"cooldowns and realized P/L are UNKNOWN. New entries "
                f"blocked, file preserved for diagnosis.", "ERROR")
            try:
                _q = "%s.corrupt_%d" % (STATE_FILE, int(time.time()))
                os.replace(STATE_FILE, _q)
                self._log(f"Preserved unreadable state as {_q}", "ERROR")
            except Exception as _qe:
                self._log(f"Could not quarantine {STATE_FILE}: {_qe}", "ERROR")

    # ── intel gathering ──
    def gather_intel(self):
        """Pull all four intel sources. Each is independent and optional."""
        intel = {}
        status = {}

        oracle = _get_json(ORACLE_URL)
        if oracle:
            age = _now() - (oracle.get("timestamp") or 0)
            status["oracle"] = {"up": True, "age_s": round(age, 1),
                                "stale": age > INTEL_STALE_S}
            intel["oracle"] = oracle
        else:
            status["oracle"] = {"up": False}

        db = _get_json(DEEPBLUE_URL)
        if db:
            age = _now() - (db.get("timestamp") or 0)
            status["deepblue"] = {"up": True, "age_s": round(age, 1),
                                  "stale": age > INTEL_STALE_S}
            intel["deepblue"] = db
        else:
            status["deepblue"] = {"up": False}

        nx = _get_json(NEXUS_URL)
        if nx:
            age = _now() - (nx.get("timestamp") or 0)
            status["nexus"] = {"up": True, "age_s": round(age, 1),
                               "stale": age > INTEL_STALE_S}
            intel["nexus"] = nx
        else:
            status["nexus"] = {"up": False}

        sn = _get_json(SENTINEL_URL)
        if sn:
            age = _now() - (sn.get("timestamp") or 0)
            status["sentinel"] = {"up": True, "age_s": round(age, 1),
                                  "stale": age > INTEL_STALE_S}
            intel["sentinel"] = sn
        else:
            status["sentinel"] = {"up": False}

        self.intel_status = status
        return intel

    # ── scoring ──
    def _normalize_pair(self, raw):
        """Oracle emits bare symbols ('CRV'); the fleet speaks 'CRV/USD'."""
        if not raw:
            return None
        p = str(raw).strip().upper()
        if "/" in p:
            return p
        return f"{p}/USD"

    def score_candidates(self, intel):
        """Build confluence-scored candidates from all intel sources.

        Oracle supplies the thesis (entry/stop/target). Everything else
        confirms or vetoes. A candidate with only Oracle backing is rejected
        by the MIN_SOURCES gate — that is deliberate.

        Direction split (2026-07-30): each Oracle row carries its own
        direction, and every component below contributes to conviction in
        THAT direction only — LONG evidence never leaks into SHORT conviction
        or vice versa. Per-source direction policy:

          Oracle    — directional per row (`direction` field). Anchors both
                      sides identically. NEUTRAL rows have no thesis → reject.
          Deep Blue — whaleScore is direction-NEUTRAL by construction
                      (ADX + low-chopiness + volume-surge composite = "big
                      activity"); the record's `trend` field (+DI/−DI) is the
                      direction. Bullish trend feeds LONG only, bearish feeds
                      SHORT only, neutral feeds both.
          NEXUS     — market_character is fleet-wide ORDERLINESS, not
                      direction. Applied identically to both sides: an
                      orderly tape supports any well-formed thesis, chaos
                      blows stops both ways.
          Sentinel  — signed 1h drift. Positive drift feeds LONG (damped by
                      tail_risk_down); negative drift feeds SHORT (damped by
                      tail_risk_up — squeeze risk). Sentinel publishes both
                      tails, so the short-side damping is a real mirror, not
                      a guess.
        """
        oracle = intel.get("oracle") or {}
        signals = oracle.get("top_signals") or []
        oracle_stale = self.intel_status.get("oracle", {}).get("stale", True)

        # Whale scores keyed by normalized pair
        whales = {}
        for w in ((intel.get("deepblue") or {}).get("whales") or []):
            p = self._normalize_pair(w.get("pair"))
            if p:
                whales[p] = w

        # NEXUS market character — a fleet-wide risk posture, not per-pair
        nexus = intel.get("nexus") or {}
        character = (nexus.get("market_character") or "").upper()

        # Sentinel forecasts. Sentinel serves a dict keyed by pair
        # ({"HYPE/USD": {...}}), but tolerate a list of records too.
        forecasts = {}
        raw_fc = (intel.get("sentinel") or {}).get("forecasts")
        if isinstance(raw_fc, dict):
            for k, v in raw_fc.items():
                p = self._normalize_pair(k)
                if p and isinstance(v, dict):
                    forecasts[p] = v
        elif isinstance(raw_fc, list):
            for f in raw_fc:
                if not isinstance(f, dict):
                    continue
                p = self._normalize_pair(f.get("pair") or f.get("symbol"))
                if p:
                    forecasts[p] = f

        candidates = []
        rejections = []

        for sig in signals:
            pair = self._normalize_pair(sig.get("pair"))
            if not pair:
                continue

            direction = (sig.get("direction") or "").upper()
            score = sig.get("score") or 0.0
            rr = sig.get("rr") or 0.0
            entry = sig.get("entry")
            stop = sig.get("stop")
            target = sig.get("target")

            reasons = []
            sources = []
            weighted = 0.0

            # ── Direction gate ──
            # A row with no directional thesis (Oracle's NEUTRAL strategies)
            # has nothing to aggregate toward.
            if direction not in ("LONG", "SHORT"):
                rejections.append({"pair": pair, "gate": "direction",
                                   "detail": f"{direction or 'NONE'} — no directional thesis"})
                continue

            # Fleet direction policy (fleet_config.direction_allowed). Logged
            # as a rejection like every other gate, so a re-flip of
            # FLEET_LONG_ONLY is visible in /api/snapshot, not silent.
            allowed, policy_why = self._direction_allowed(direction)
            if not allowed:
                rejections.append({"pair": pair, "gate": "direction_policy",
                                   "detail": policy_why})
                continue

            if oracle_stale:
                rejections.append({"pair": pair, "gate": "oracle_stale",
                                   "detail": "Oracle intel older than 5min"})
                continue

            if _is_blacklisted(pair):
                rejections.append({"pair": pair, "gate": "blacklist",
                                   "detail": "pair blacklisted in fleet_config"})
                continue

            # ── Oracle component ──
            # `score` is read as Oracle's conviction in the row's OWN
            # direction and gated identically for LONG and SHORT — shorts are
            # held to conviction at least as strict as longs. Caveat, checked
            # 2026-07-30: Oracle's composite is historically a bullishness
            # scale, and all three bearish strategies (FALLING KNIFE,
            # DISTRIBUTION, TAKE PROFIT) are in its no-entry set (None
            # levels), so no SHORT row currently reaches top_signals at all.
            # If Oracle later publishes SHORT rows still scored on the bullish
            # composite, they will UNDER-fire this gate (low score), never
            # over-fire — the safe failure mode for a subscriber product.
            if score < MIN_ORACLE_SCORE:
                rejections.append({"pair": pair, "gate": "oracle_score",
                                   "detail": f"score {score:.1f} < {MIN_ORACLE_SCORE}"})
                continue
            oracle_norm = min(1.0, score / 100.0)
            weighted += W_ORACLE * oracle_norm
            sources.append("oracle")
            reasons.append(f"Oracle {score:.0f}/100 ({sig.get('confidence','?')})")

            if rr and rr < MIN_RR:
                rejections.append({"pair": pair, "gate": "risk_reward",
                                   "detail": f"rr {rr:.2f} < {MIN_RR}"})
                continue
            if rr:
                reasons.append(f"R:R {rr:.2f}")

            # ── Level orientation sanity ──
            # A SHORT protects with a stop ABOVE entry and targets BELOW;
            # mirrored for LONG. Malformed levels (e.g. a long-side level
            # generator run against a bearish row) must be rejected, not
            # traded. Only checked when Oracle supplied all three levels —
            # missing ones are synthesized direction-correctly in try_enter.
            if entry and stop and target:
                ok_levels = (stop < entry < target) if direction == "LONG" \
                    else (target < entry < stop)
                if not ok_levels:
                    rejections.append({
                        "pair": pair, "gate": "level_orientation",
                        "detail": (f"{direction} levels malformed "
                                   f"(stop {stop:.6g}, entry {entry:.6g}, "
                                   f"target {target:.6g})")})
                    continue

            # Track the weight that actually CONTRIBUTED, so the denominator
            # below matches the numerator. Adding a source's weight to the
            # denominator when it contributed nothing silently depresses the
            # score (see the avail_w comment).
            contributed_w = W_ORACLE

            # ── Deep Blue component ──
            # whaleScore measures ACTIVITY, not accumulation (0.4·ADX-strength
            # + 0.3·low-chopiness + 0.3·volume-surge) — direction-neutral by
            # construction. The record's `trend` field (+DI/−DI, ADX-gated)
            # carries the direction, so a whale only confirms a candidate its
            # trend agrees with: bullish→LONG, bearish→SHORT. A "neutral"
            # trend (big activity, direction unresolved) feeds both sides —
            # the same treatment the LONG-only code always gave it. A whale
            # whose trend DISAGREES contributes nothing. (The pre-split code
            # let a bearish-trend whale add to LONG conviction — a latent bug
            # fixed by this direction match.)
            w = whales.get(pair)
            if w:
                wscore = w.get("whaleScore") or 0.0
                rel = w.get("reliability") or 0.0
                wtrend = (w.get("trend") or "neutral").lower()
                trend_matches = (wtrend == "neutral"
                                 or (direction == "LONG" and wtrend == "bullish")
                                 or (direction == "SHORT" and wtrend == "bearish"))
                if wscore >= MIN_WHALE_SCORE and trend_matches:
                    whale_norm = min(1.0, (wscore / 100.0) * (rel / 100.0))
                    weighted += W_WHALE * whale_norm
                    contributed_w += W_WHALE
                    sources.append("deepblue")
                    reasons.append(f"Whale {wscore:.0f} (rel {rel:.0f}, {wtrend})")

            # ── NEXUS component: fleet-wide posture ──
            # market_character measures ORDERLINESS of the tape, not
            # direction, so the mapping is applied identically to LONG and
            # SHORT candidates: an ordered/trending market supports any
            # well-formed thesis, and a chaotic one blows stops both ways
            # (CHAOTIC is not treated as pro-short — that would conflate
            # "unpredictable" with "falling").
            if character:
                # Risk-on characters add conviction; risk-off subtracts it.
                # MIXED/NEUTRAL are explicitly half-weight rather than unmapped:
                # an unrecognised character must never silently vanish.
                # NEXUS._classify() emits exactly five values: SYSTEMIC,
                # INSTITUTIONAL, RETAIL_NOISE, STRUCTURAL_SHIFT, MIXED.
                # Before 2026-08-06 only SYSTEMIC and MIXED matched a branch,
                # so three of the five silently vanished (208 "not mapped"
                # warnings in one log window, all INSTITUTIONAL) and the
                # risk-on keywords TRENDING/EXPANSION/STABLE/ORDERED matched
                # NOTHING nexus can produce. The NEXUS component of the
                # confluence score was therefore incapable of ever being
                # positive: its only reachable outcomes were a penalty and a
                # half-weight neutral. Semantics below are taken from
                # nexus._interpret(), the producer.
                if "INSTITUTIONAL" in character:
                    # "Whale-driven move — likely to continue": a
                    # continuation signal, so it confirms a well-formed
                    # thesis in either direction.
                    weighted += W_NEXUS * 0.85
                    contributed_w += W_NEXUS
                    sources.append("nexus")
                    reasons.append("NEXUS Institutional (whale-led)")
                elif "RETAIL_NOISE" in character:
                    # "Retail noise — likely to revert". A move expected to
                    # revert does not support a continuation thesis; penalise
                    # without enlarging the denominator, as with risk-off.
                    weighted -= W_NEXUS * 0.5
                    reasons.append("NEXUS Retail Noise (penalty)")
                elif "STRUCTURAL_SHIFT" in character:
                    # "Slow bots leading" — real but slow-forming, and
                    # direction-agnostic. Half weight, like MIXED.
                    weighted += W_NEXUS * 0.45
                    contributed_w += W_NEXUS
                    sources.append("nexus")
                    reasons.append("NEXUS Structural Shift")
                elif any(k in character for k in ("TRENDING", "EXPANSION", "STABLE", "ORDERED")):
                    weighted += W_NEXUS * 0.85
                    contributed_w += W_NEXUS
                    sources.append("nexus")
                    reasons.append(f"NEXUS {character.title()}")
                elif any(k in character for k in ("CHAOTIC", "SYSTEMIC", "STRESSED", "TURBULENT")):
                    # Risk-off is a veto signal, not a weighted contribution:
                    # subtract from the numerator WITHOUT adding to the
                    # denominator, or the pair is penalised twice over.
                    weighted -= W_NEXUS * 0.5
                    reasons.append(f"NEXUS {character.title()} (penalty)")
                elif any(k in character for k in ("MIXED", "NEUTRAL", "RANGING", "TRANSITION")):
                    weighted += W_NEXUS * 0.45
                    contributed_w += W_NEXUS
                    sources.append("nexus")
                    reasons.append(f"NEXUS {character.title()} (neutral)")
                else:
                    # Unmapped character contributes nothing, so it must not
                    # enlarge the denominator either.
                    self._log(f"NEXUS character not mapped: {character!r}", "WARNING")

            # ── Sentinel component ──
            fc = forecasts.get(pair)
            if isinstance(fc, dict):
                # Sentinel forecasts carry a distribution, not a conviction
                # scalar: derive one from the SIGNED 1h expected move vs
                # current. Positive drift confirms a LONG, damped by downside
                # tail risk; negative drift confirms a SHORT, damped by UPSIDE
                # tail risk (squeeze risk) — Sentinel publishes both
                # tail_risk_up and tail_risk_down, so the short side is a real
                # mirror. Drift against the candidate's direction contributes
                # nothing to it (and can never leak into the other side,
                # because conviction is scored per-row, per-direction).
                # SENTINEL_FULL_CONVICTION_DRIFT was calibrated on the bullish
                # tail of the forecast distribution (p90 of positive drifts);
                # the same magnitude is assumed for bearish drifts until a
                # bearish-side calibration exists.
                conv_norm = None
                try:
                    h1 = fc.get("1h") or {}
                    cur = fc.get("current")
                    exp = h1.get("expected")
                    if cur and exp and cur > 0:
                        drift = (exp - cur) / cur          # signed expected move
                        if direction == "LONG" and drift > 0:
                            tail = float(h1.get("tail_risk_down") or 0.0)
                            conv_norm = (min(1.0, drift / SENTINEL_FULL_CONVICTION_DRIFT)
                                         * (1.0 - min(1.0, tail)))
                        elif direction == "SHORT" and drift < 0:
                            tail = float(h1.get("tail_risk_up") or 0.0)
                            conv_norm = (min(1.0, -drift / SENTINEL_FULL_CONVICTION_DRIFT)
                                         * (1.0 - min(1.0, tail)))
                except (TypeError, ValueError, ZeroDivisionError):
                    conv_norm = None

                if conv_norm and conv_norm > 0.5:
                    weighted += W_SENTINEL * conv_norm
                    contributed_w += W_SENTINEL
                    sources.append("sentinel")
                    sign = "+" if direction == "LONG" else "-"
                    reasons.append(f"Sentinel {sign}{conv_norm:.0%}")

            # ── Confluence gates ──
            if len(sources) < MIN_SOURCES:
                rejections.append({"pair": pair, "gate": "min_sources",
                                   "detail": f"{len(sources)} source(s), need {MIN_SOURCES}"})
                continue

            # Normalize against the weight that actually CONTRIBUTED, not the
            # theoretical all-four maximum. Deep Blue only covers a handful of
            # pairs fleet-wide, so scoring against the full denominator makes
            # the gate unreachable and the bot never trades.
            #
            # `contributed_w` is accumulated alongside each `weighted +=` above
            # rather than recomputed here from source *presence*. Presence is
            # not contribution: a whale below MIN_WHALE_SCORE, or an unmapped
            # NEXUS character, is present but adds nothing — counting it in the
            # denominator would make a weak signal score strictly worse than a
            # missing one.
            confluence = (max(0.0, min(1.0, weighted / contributed_w))
                          if contributed_w else 0.0)
            if confluence < MIN_CONFLUENCE:
                rejections.append({"pair": pair, "gate": "confluence",
                                   "detail": f"{confluence:.3f} < {MIN_CONFLUENCE}"})
                continue

            # ── Minimum move: target must be worth a subscriber's while ──
            if entry and target:
                # Move toward the target in the trade's own direction:
                # for a SHORT the target sits below entry, so the profitable
                # move is (entry − target).
                if direction == "SHORT":
                    move_pct = (entry - target) / entry
                else:
                    move_pct = (target - entry) / entry
                if move_pct < MIN_TARGET_MOVE_PCT:
                    rejections.append({
                        "pair": pair, "gate": "min_move",
                        "detail": f"target {move_pct:.2%} < {MIN_TARGET_MOVE_PCT:.2%} min move"})
                    continue

            candidates.append({
                "pair": pair,
                "direction": direction,
                "confluence": round(confluence, 4),
                "sources": sources,
                "source_count": len(sources),
                "oracle_score": score,
                "rr": rr,
                "entry": entry,
                "stop": stop,
                "target": target,
                "hurst": sig.get("hurst"),
                "mc_prob_up": sig.get("mc_prob_up"),
                "reasons": reasons,
                "thesis": " · ".join(reasons),
            })

        candidates.sort(key=lambda c: c["confluence"], reverse=True)
        self.candidates = candidates
        self.rejections = rejections[:40]
        return candidates

    # ── entry / exit ──
    def _can_enter(self, pair):
        # Unknown state fails toward ARMED. With the state file unreadable
        # the pair cooldown below is disarmed (empty dict -> _now()-0 always
        # exceeds the window) and self.positions may be missing positions
        # whose reservations the pool still holds.
        if getattr(self, "_state_unreadable", False):
            return False, "state unreadable — entries blocked"
        if pair in self.positions:
            return False, "already open"
        if len(self.positions) >= MAX_OPEN_POSITIONS:
            return False, f"max positions ({MAX_OPEN_POSITIONS})"
        if _now() < self.loss_cooldown_until:
            return False, f"loss cooldown {int(self.loss_cooldown_until - _now())}s"
        cd = self.pair_cooldowns.get(pair, 0)
        if _now() - cd < SAME_PAIR_COOLDOWN_S:
            return False, f"pair cooldown {int(SAME_PAIR_COOLDOWN_S - (_now() - cd))}s"
        return True, ""

    def try_enter(self, cand):
        pair = cand["pair"]
        direction = cand.get("direction", "LONG")
        ok, why = self._can_enter(pair)
        if not ok:
            self.rejections.append({"pair": pair, "gate": "position_mgmt", "detail": why})
            return False

        # Re-check fleet direction policy at reservation time — the policy
        # can flip between scoring and entry, and a stale candidate must not
        # slip a short past a re-enabled FLEET_LONG_ONLY.
        allowed, policy_why = self._direction_allowed(direction)
        if not allowed:
            self.rejections.append({"pair": pair, "gate": "direction_policy",
                                    "detail": policy_why})
            return False

        entry = cand.get("entry")
        if not entry or entry <= 0:
            self.rejections.append({"pair": pair, "gate": "no_price", "detail": "no entry price"})
            return False

        # Synthesized fallback levels mirror by direction: a SHORT stops
        # ABOVE entry and targets BELOW.
        if direction == "SHORT":
            stop = cand.get("stop") or entry * (1 + STOP_LOSS_PCT)
            target = cand.get("target") or entry * (1 - STOP_LOSS_PCT * 2)
        else:
            stop = cand.get("stop") or entry * (1 - STOP_LOSS_PCT)
            target = cand.get("target") or entry * (1 + STOP_LOSS_PCT * 2)
        size, _basis, _risk_usd, _stop_pct = self._position_size(entry, stop)
        if _basis is None or size <= 0:
            # The pool could not be read, so there is no honest basis to size
            # against. Skip rather than guess: the old fallback basis put a
            # single position at 12x the whole pool.
            self._log(
                f"SKIP {pair}: pool unreadable, no sizing basis — not "
                f"opening a position on a guessed basis", "WARNING")
            return False
        self._log(
            f"SIZE {pair}: ${size:,.0f} — risking ${_risk_usd:,.0f} "
            f"({RISK_PER_TRADE_PCT}% of ${_basis:,.0f}) on a {_stop_pct * 100:.2f}% stop",
            "INFO")

        # CHRONOS: temporal bias — soft influence only, never a hard block.
        # A fresh (<1h) statistically-gated TIME_ANOMALY opposing this
        # entry's direction shaves 25% off size; agreement is log-only (stay
        # conservative). SESSION_OVERLAP fresh (<15m) is a context log line
        # only, no size change.
        if self._bus:
            try:
                temporal = self._bus.chronos_temporal(max_age=3600)
                anomaly = temporal.get("time_anomaly") if temporal else None
                if anomaly:
                    anomaly_dir = "LONG" if anomaly.get("direction") == "bullish" else "SHORT"
                    if anomaly_dir != direction:
                        size *= 0.75
                        self._log(
                            f"TEMPORAL OPPOSE {pair}: TIME_ANOMALY {anomaly.get('direction')} "
                            f"(n={anomaly.get('n')}, bias={anomaly.get('bias_pct')}%) opposes "
                            f"{direction} — size x0.75", "INFO")
                    else:
                        self._log(
                            f"TEMPORAL AGREE {pair}: TIME_ANOMALY {anomaly.get('direction')} "
                            f"(n={anomaly.get('n')}, bias={anomaly.get('bias_pct')}%) agrees with "
                            f"{direction} — no size change", "INFO")
                for se in (temporal.get("session_events") or []) if temporal else []:
                    if se.get("type") == "SESSION_OVERLAP" and (_now() - se.get("ts", 0)) < 900:
                        self._log(f"TEMPORAL CONTEXT {pair}: SESSION_OVERLAP {se.get('window')} — high volatility window", "INFO")
                        break
            except Exception as e:
                self._log(f"Temporal bias check failed: {e}", "WARNING")

        # Reserve capital from Command Center before committing.
        rid = None
        if self._portfolio:
            stop_pct = abs(entry - stop) / entry if entry else STOP_LOSS_PCT
            got, res = self._portfolio.reserve(pair, direction, size, stop_loss_pct=stop_pct)
            if not got:
                self.rejections.append({"pair": pair, "gate": "portfolio",
                                        "detail": f"reservation denied: {res}"})
                return False
            rid = res

        if self.is_live():
            # Live execution intentionally not implemented — this bot has never
            # traded real capital and must not silently do so. Release and skip.
            self._log(f"LIVE mode requested for {pair} but live execution is not "
                      f"implemented — skipping entry", "WARNING")
            if rid and self._portfolio:
                self._portfolio.release(rid, pnl=0.0)
            return False

        pos = Position(pair, direction, entry, size, stop, target, rid, cand["thesis"])
        self.positions[pair] = pos
        self.pair_cooldowns[pair] = _now()
        self._log(f"OPEN {direction} {pair} @ {entry:.6g} conf={cand['confluence']:.3f} "
                  f"[{', '.join(cand['sources'])}] {cand['thesis']}", "INFO")
        self._emit("TRADE_OPEN", {
            "bot": BOT_ID, "pair": pair, "direction": direction, "entry": entry,
            "size_usd": size, "confluence": cand["confluence"],
            "sources": cand["sources"], "thesis": cand["thesis"],
        })
        self._save_state()
        return True

    def _price_for(self, pair, intel):
        """Current price from Oracle's signal set (the only live quote we have).

        Skips rows whose entry is None. Non-directional Oracle strategies
        (WATCH, SWING TRADE, TRAIL STOP, ...) publish null levels, and this
        used to return that None on the first pair match — so manage_positions
        would get no price and silently skip stop/target checks for a held
        position, for as long as Oracle kept classifying that pair as
        non-directional. Keep scanning for a row that actually has a quote.
        (2026-07-29 audit)
        """
        for sig in ((intel.get("oracle") or {}).get("all_signals")
                    or (intel.get("oracle") or {}).get("top_signals") or []):
            if self._normalize_pair(sig.get("pair")) == pair:
                entry = sig.get("entry")
                if entry is not None:
                    return entry
        return None

    def manage_positions(self, intel):
        for pair, pos in list(self.positions.items()):
            price = self._price_for(pair, intel)
            if not price:
                continue

            # Favorable extreme: highest price for a LONG, lowest for a SHORT.
            if pos.direction == "SHORT":
                if price < pos.high_water:
                    pos.high_water = price
            elif price > pos.high_water:
                pos.high_water = price

            # Exit checks mirrored by direction: a SHORT's stop is ABOVE
            # entry (price rising through it is the loss) and its target is
            # BELOW (price falling to it is the win).
            reason = None
            if pos.direction == "SHORT":
                if price >= pos.stop:
                    reason = "STOP"
                elif price <= pos.target:
                    reason = "TARGET"
            else:
                if price <= pos.stop:
                    reason = "STOP"
                elif price >= pos.target:
                    reason = "TARGET"
            if not reason and pos.age_h >= MAX_POSITION_AGE_H:
                reason = "TIME"

            if reason:
                self._close(pos, price, reason)

    def _close(self, pos, price, reason):
        # Gross price movement, direction-aware via unrealized(). The fleet
        # is a signal product — subscribers pay their own exchanges' fees,
        # so nothing is deducted here.
        pnl = pos.unrealized(price)

        self.realized_pnl += pnl
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1
            self.loss_cooldown_until = _now() + COOLDOWN_AFTER_LOSS_S

        if pos.reservation_id and self._portfolio:
            try:
                self._portfolio.release(pos.reservation_id, pnl=pnl,
                                        entry_price=pos.entry, exit_price=price)
            except Exception as e:
                self._log(f"Release failed for {pos.pair}: {e}", "WARNING")

        trade = {
            "pair": pos.pair, "direction": pos.direction, "entry": pos.entry,
            "exit": price, "size_usd": pos.size_usd, "pnl": round(pnl, 2),
            # Legacy keys kept for dashboard renderers that read
            # gross_pnl/net_pnl/fees: P/L is gross now, so gross == net and
            # fees are always 0 (subscriber-side).
            "gross_pnl": round(pnl, 2), "net_pnl": round(pnl, 2), "fees": 0.0,
            "exit_reason": reason,
            "age_h": round(pos.age_h, 2), "thesis": pos.thesis, "closed_at": _utc(),
        }
        self.closed_trades.append(trade)
        if len(self.closed_trades) > 500:
            self.closed_trades = self.closed_trades[-500:]

        self.positions.pop(pos.pair, None)
        self._log(f"CLOSE {pos.direction} {pos.pair} @ {price:.6g} {reason} "
                  f"pnl={pnl:+.2f} (gross)", "INFO")
        self._emit("TRADE_CLOSE", {
            "bot": BOT_ID, "pair": pos.pair, "direction": pos.direction,
            "entry": pos.entry, "exit": price, "pnl": round(pnl, 2),
            "exit_reason": reason,
        })
        self._save_state()

    # ── main loop ──
    def scan(self):
        t0 = _now()
        self.cycle += 1
        # Lease heartbeat (outside the lock — network call): declare held
        # reservation ids so the pool can sweep anything a wiring bug stranded.
        if self._portfolio:
            self._portfolio.confirm_reservations(
                [p.reservation_id for p in self.positions.values() if p.reservation_id])
        with self._lock:
            intel = self.gather_intel()

            up = [k for k, v in self.intel_status.items() if v.get("up")]
            if len(up) < MIN_SOURCES:
                self._log(f"Only {len(up)} intel source(s) up ({', '.join(up) or 'none'}) "
                          f"— holding", "WARNING")
                self.last_scan = _utc()
                self.last_scan_duration = round(_now() - t0, 3)
                return

            self.manage_positions(intel)
            cands = self.score_candidates(intel)

            for c in cands:
                if len(self.positions) >= MAX_OPEN_POSITIONS:
                    break
                self.try_enter(c)

            self.last_scan = _utc()
            self.last_scan_duration = round(_now() - t0, 3)

            # Checkpoint every scan, not only on entry/exit. _save_state was
            # called from two places, both trade events, so a bot holding
            # steady positions never rewrote the file — the live one was 880
            # minutes stale. That meant the legacy-fee restatement in
            # _load_state recomputed from unchanged disk on every boot
            # instead of being written once: live pnl read -14.89 while disk
            # still held the pre-restatement -23.69, forever.
            try:
                self._save_state()
            except Exception as e:
                self._log(f"Scan checkpoint save failed: {e}", "WARNING")

    def run_forever(self):
        self._log(f"{BOT_NAME} online — intel-driven, bidirectional (LONG+SHORT), "
                  f"mode={'LIVE' if self.is_live() else 'paper'}", "INFO")
        while True:
            try:
                self.scan()
            except Exception as e:
                import traceback
                self._log(f"Scan error: {e.__class__.__name__}: {e}", "ERROR")
                self._log(traceback.format_exc().strip().replace("\n", " | "), "ERROR")
            time.sleep(SCAN_INTERVAL_S)

    # ── snapshot ──
    def snapshot(self):
        with self._lock:
            total = self.wins + self.losses
            wr = (self.wins / total * 100.0) if total else 0.0
            # NOT an account balance. This bot holds no capital of its own —
            # it sizes against a share of the shared CC pool (~$210). This is
            # a notional index: a fixed base plus realized P/L, so the number
            # moves the right way and by the right amount. It is reported
            # only so pnl_pct has a stable denominator.
            #
            # It must never be summed as if it were money. Command Center's
            # aggregate.total_equity does exactly that across five bots and
            # gets ~$49,631, which the dashboard then used as a fallback POOL
            # figure — 236x the real pool. See tests/test_pool_absent.py.
            equity = NOTIONAL_PNL_BASE + self.realized_pnl
            # Cheap: _sizing_basis does one CC call, and snapshot is polled
            # once per CC cycle, not per candidate.
            _basis_now = self._sizing_basis()
            return {
                "timestamp": _now(),
                "bot_name": BOT_NAME,
                "bot_id": BOT_ID,
                "status": "running",
                "cycle": self.cycle,
                "mode": "live" if self.is_live() else "paper",
                "strategy": {
                    "name": "Intel Confluence (fleet-signal aggregation)",
                    "type": "BIDIRECTIONAL",
                    "sources": ["oracle", "deepblue", "nexus", "sentinel"],
                    "weights": {"oracle": W_ORACLE, "deepblue": W_WHALE,
                                "nexus": W_NEXUS, "sentinel": W_SENTINEL},
                    "min_confluence": MIN_CONFLUENCE,
                    "min_sources": MIN_SOURCES,
                },
                "equity": round(equity, 2),
                # What sizing is actually calculated from, and where it came
                # from. Without this the dashboard shows ~$10k equity while
                # the bot opens $25k positions and nothing says why.
                # None when the pool is unreadable: that is not a basis of
                # zero and not a basis of $10,000, it is the absence of one,
                # and it must not round() its way into a confident number.
                "sizing_basis": (round(_basis_now, 2)
                                 if isinstance(_basis_now, (int, float)) else None),
                "sizing_basis_source": ("pool_share"
                                        if isinstance(_basis_now, (int, float))
                                        else "unreadable"),
                "risk_per_trade_usd": (round(_basis_now * RISK_PER_TRADE_PCT / 100.0, 2)
                                       if isinstance(_basis_now, (int, float)) else None),
                "risk_per_trade_pct": RISK_PER_TRADE_PCT,
                "pnl": round(self.realized_pnl, 2),
                "pnl_pct": round(self.realized_pnl / NOTIONAL_PNL_BASE * 100.0, 3),
                "win_rate": round(wr, 1),
                "open_positions": len(self.positions),
                "total_trades": total,
                # Kept for CC's confluence normalizer, which passes this
                # field through. Always 0: the fleet is a signal product and
                # fees are subscriber-side.
                "fees_paid": 0.0,
                "uptime": round(_now() - self.started_at, 1),
                "regime": (self.intel_status.get("nexus", {}).get("up")
                           and "INTEL_DRIVEN" or "DEGRADED"),
                "signals_count": len(self.candidates),
                "intel_status": self.intel_status,
                "positions": [p.to_dict() for p in self.positions.values()],
                "candidates": self.candidates[:10],
                "rejections": self.rejections[:20],
                "recent_trades": self.closed_trades[-10:],
                "last_scan": self.last_scan,
                "scan_duration_s": self.last_scan_duration,
                "logs": self.logs[-30:],
            }


# ── HTTP server ────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    engine: ConfluenceEngine = None

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/api/snapshot", "/", "/health"):
            self._send(self.engine.snapshot())
        elif path == "/positions":
            self._send([p.to_dict() for p in self.engine.positions.values()])
        elif path == "/candidates":
            self._send(self.engine.candidates)
        elif path == "/trades":
            self._send(self.engine.closed_trades[-50:])
        else:
            self._send({"error": "not found"}, 404)


class _ThreadedHTTP(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    engine = ConfluenceEngine()
    _Handler.engine = engine

    t = threading.Thread(target=engine.run_forever, daemon=True, name="scan")
    t.start()

    srv = _ThreadedHTTP(("127.0.0.1", args.port), _Handler)
    print(f"  {BOT_NAME} — intel-driven trader")
    print(f"  API:  http://127.0.0.1:{args.port}/api/snapshot")
    print(f"  Mode: {'LIVE' if engine.is_live() else 'paper'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        engine._save_state()
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
