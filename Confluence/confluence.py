#!/usr/bin/env python3
"""
CONFLUENCE — Intel-Driven Fleet Trader
======================================
Port 8088. Kraken spot, LONG-only, paper-gated.

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

# Source weights — Oracle is the only source giving a full trade thesis,
# so it anchors. The rest confirm or veto.
W_ORACLE = 0.45
W_WHALE = 0.25
W_NEXUS = 0.18
W_SENTINEL = 0.12

# Risk
MAX_OPEN_POSITIONS = 3
# CC's portfolio manager enforces a size floor of 5% of the pool (~$498 at a
# $9,970 pool) and rejects anything under it. Sized above that floor with
# headroom so pool drift doesn't start silently failing reservations.
POSITION_SIZE_USD = 550.0
STOP_LOSS_PCT = 0.025          # 2.5% hard floor if Oracle gives no stop
MAX_POSITION_AGE_H = 36
FEE_RATE = 0.0040              # Kraken taker 0.40%
MIN_PROFIT_VS_FEES = 2.0       # target must clear 2x round-trip fees
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
    def __init__(self, pair, entry, size_usd, stop, target, reservation_id, thesis):
        self.pair = pair
        self.direction = "LONG"
        self.entry = entry
        self.size_usd = size_usd
        self.stop = stop
        self.target = target
        self.reservation_id = reservation_id
        self.thesis = thesis
        self.opened_at = _now()
        self.high_water = entry

    @property
    def age_h(self):
        return (_now() - self.opened_at) / 3600.0

    def unrealized(self, price):
        if not self.entry:
            return 0.0
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
        self.fees_paid = 0.0
        self.wins = 0
        self.losses = 0
        self._lock = threading.RLock()

        self._portfolio = None
        self._events = None
        self._init_fleet()
        self._load_state()

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

    def is_live(self):
        """Never execute real orders unless fleet_config says live AND allows LONG."""
        if not _fc:
            return False
        try:
            if not _fc.is_live():
                return False
            return _fc.live_direction_allowed("LONG")
        except Exception:
            return False

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
                    "fees_paid": self.fees_paid,
                    "wins": self.wins,
                    "losses": self.losses,
                    "pair_cooldowns": self.pair_cooldowns,
                }, f, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            self._log(f"State save failed: {e}", "WARNING")

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            self.realized_pnl = d.get("realized_pnl", 0.0)
            self.fees_paid = d.get("fees_paid", 0.0)
            self.wins = d.get("wins", 0)
            self.losses = d.get("losses", 0)
            self.closed_trades = d.get("closed_trades", [])
            self.pair_cooldowns = d.get("pair_cooldowns", {})
            for pair, pd in (d.get("positions") or {}).items():
                pos = Position(pair, pd["entry"], pd["size_usd"], pd["stop"],
                               pd["target"], pd.get("reservation_id"), pd.get("thesis", {}))
                pos.opened_at = pd.get("opened_at", _now())
                self.positions[pair] = pos
            if self.positions:
                self._log(f"Restored {len(self.positions)} position(s) from state", "INFO")
        except Exception as e:
            self._log(f"State load failed: {e}", "WARNING")

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
        vol_forecast = nexus.get("volatility_forecast")

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

            # ── LONG-only mandate ──
            if direction != "LONG":
                rejections.append({"pair": pair, "gate": "direction",
                                   "detail": f"{direction} — bot is LONG-only"})
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

            # Track the weight that actually CONTRIBUTED, so the denominator
            # below matches the numerator. Adding a source's weight to the
            # denominator when it contributed nothing silently depresses the
            # score (see the avail_w comment).
            contributed_w = W_ORACLE

            # ── Deep Blue component ──
            w = whales.get(pair)
            if w:
                wscore = w.get("whaleScore") or 0.0
                rel = w.get("reliability") or 0.0
                if wscore >= MIN_WHALE_SCORE:
                    whale_norm = min(1.0, (wscore / 100.0) * (rel / 100.0))
                    weighted += W_WHALE * whale_norm
                    contributed_w += W_WHALE
                    sources.append("deepblue")
                    reasons.append(f"Whale {wscore:.0f} (rel {rel:.0f})")

            # ── NEXUS component: fleet-wide posture ──
            if character:
                # Risk-on characters add conviction; risk-off subtracts it.
                # MIXED/NEUTRAL are explicitly half-weight rather than unmapped:
                # an unrecognised character must never silently vanish.
                if any(k in character for k in ("TRENDING", "EXPANSION", "STABLE", "ORDERED")):
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
                # scalar: derive one from the 1h expected move vs current,
                # damped by downside tail risk.
                conv_norm = None
                try:
                    h1 = fc.get("1h") or {}
                    cur = fc.get("current")
                    exp = h1.get("expected")
                    if cur and exp and cur > 0:
                        drift = (exp - cur) / cur          # signed expected move
                        tail_dn = float(h1.get("tail_risk_down") or 0.0)
                        if drift > 0:
                            # 1% expected upside ≈ full conviction, minus tail risk
                            conv_norm = max(0.0, min(1.0, drift / 0.01)) * (1.0 - min(1.0, tail_dn))
                except (TypeError, ValueError, ZeroDivisionError):
                    conv_norm = None

                if conv_norm and conv_norm > 0.5:
                    weighted += W_SENTINEL * conv_norm
                    contributed_w += W_SENTINEL
                    sources.append("sentinel")
                    reasons.append(f"Sentinel +{conv_norm:.0%}")

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

            # ── Fee viability: target must clear 2x round-trip ──
            if entry and target:
                gross_pct = (target - entry) / entry
                fee_pct = FEE_RATE * 2
                if gross_pct < fee_pct * MIN_PROFIT_VS_FEES:
                    rejections.append({
                        "pair": pair, "gate": "fee_floor",
                        "detail": f"target {gross_pct:.2%} < {fee_pct * MIN_PROFIT_VS_FEES:.2%} fee floor"})
                    continue

            candidates.append({
                "pair": pair,
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
        ok, why = self._can_enter(pair)
        if not ok:
            self.rejections.append({"pair": pair, "gate": "position_mgmt", "detail": why})
            return False

        entry = cand.get("entry")
        if not entry or entry <= 0:
            self.rejections.append({"pair": pair, "gate": "no_price", "detail": "no entry price"})
            return False

        stop = cand.get("stop") or entry * (1 - STOP_LOSS_PCT)
        target = cand.get("target") or entry * (1 + STOP_LOSS_PCT * 2)
        size = POSITION_SIZE_USD

        # Reserve capital from Command Center before committing.
        rid = None
        if self._portfolio:
            stop_pct = abs(entry - stop) / entry if entry else STOP_LOSS_PCT
            got, res = self._portfolio.reserve(pair, "LONG", size, stop_loss_pct=stop_pct)
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

        pos = Position(pair, entry, size, stop, target, rid, cand["thesis"])
        self.positions[pair] = pos
        self.pair_cooldowns[pair] = _now()
        self._log(f"OPEN {pair} @ {entry:.6g} conf={cand['confluence']:.3f} "
                  f"[{', '.join(cand['sources'])}] {cand['thesis']}", "INFO")
        self._emit("TRADE_OPEN", {
            "bot": BOT_ID, "pair": pair, "direction": "LONG", "entry": entry,
            "size_usd": size, "confluence": cand["confluence"],
            "sources": cand["sources"], "thesis": cand["thesis"],
        })
        self._save_state()
        return True

    def _price_for(self, pair, intel):
        """Current price from Oracle's signal set (the only live quote we have)."""
        for sig in ((intel.get("oracle") or {}).get("all_signals")
                    or (intel.get("oracle") or {}).get("top_signals") or []):
            if self._normalize_pair(sig.get("pair")) == pair:
                return sig.get("entry")
        return None

    def manage_positions(self, intel):
        for pair, pos in list(self.positions.items()):
            price = self._price_for(pair, intel)
            if not price:
                continue

            if price > pos.high_water:
                pos.high_water = price

            reason = None
            if price <= pos.stop:
                reason = "STOP"
            elif price >= pos.target:
                reason = "TARGET"
            elif pos.age_h >= MAX_POSITION_AGE_H:
                reason = "TIME"

            if reason:
                self._close(pos, price, reason)

    def _close(self, pos, price, reason):
        gross = pos.unrealized(price)
        fees = pos.size_usd * FEE_RATE * 2
        net = gross - fees

        self.realized_pnl += net
        self.fees_paid += fees
        if net > 0:
            self.wins += 1
        else:
            self.losses += 1
            self.loss_cooldown_until = _now() + COOLDOWN_AFTER_LOSS_S

        if pos.reservation_id and self._portfolio:
            try:
                self._portfolio.release(pos.reservation_id, pnl=net,
                                        entry_price=pos.entry, exit_price=price)
            except Exception as e:
                self._log(f"Release failed for {pos.pair}: {e}", "WARNING")

        trade = {
            "pair": pos.pair, "direction": "LONG", "entry": pos.entry,
            "exit": price, "size_usd": pos.size_usd, "gross_pnl": round(gross, 2),
            "fees": round(fees, 2), "net_pnl": round(net, 2), "exit_reason": reason,
            "age_h": round(pos.age_h, 2), "thesis": pos.thesis, "closed_at": _utc(),
        }
        self.closed_trades.append(trade)
        if len(self.closed_trades) > 500:
            self.closed_trades = self.closed_trades[-500:]

        self.positions.pop(pos.pair, None)
        self._log(f"CLOSE {pos.pair} @ {price:.6g} {reason} net={net:+.2f} "
                  f"(gross {gross:+.2f}, fees {fees:.2f})", "INFO")
        self._emit("TRADE_CLOSE", {
            "bot": BOT_ID, "pair": pos.pair, "direction": "LONG",
            "entry": pos.entry, "exit": price, "pnl": round(net, 2),
            "exit_reason": reason,
        })
        self._save_state()

    # ── main loop ──
    def scan(self):
        t0 = _now()
        self.cycle += 1
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

    def run_forever(self):
        self._log(f"{BOT_NAME} online — intel-driven, LONG-only, "
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
            equity = 10000.0 + self.realized_pnl
            return {
                "timestamp": _now(),
                "bot_name": BOT_NAME,
                "bot_id": BOT_ID,
                "status": "running",
                "cycle": self.cycle,
                "mode": "live" if self.is_live() else "paper",
                "strategy": {
                    "name": "Intel Confluence (fleet-signal aggregation)",
                    "type": "LONG_only",
                    "sources": ["oracle", "deepblue", "nexus", "sentinel"],
                    "weights": {"oracle": W_ORACLE, "deepblue": W_WHALE,
                                "nexus": W_NEXUS, "sentinel": W_SENTINEL},
                    "min_confluence": MIN_CONFLUENCE,
                    "min_sources": MIN_SOURCES,
                },
                "equity": round(equity, 2),
                "pnl": round(self.realized_pnl, 2),
                "pnl_pct": round(self.realized_pnl / 10000.0 * 100.0, 3),
                "win_rate": round(wr, 1),
                "open_positions": len(self.positions),
                "total_trades": total,
                "fees_paid": round(self.fees_paid, 2),
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
