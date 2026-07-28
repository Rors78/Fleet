"""
AEGIS Event Tap — standalone external logger.

Captures ONLY these 4 event types from the Command Center event bus:
  - TRADE_OPEN
  - SIGNAL
  - WHALE_ALERT
  - REGIME_CHANGE

Runs for exactly 900 seconds, writes each event as a JSONL line to
aegis_tap_log.jsonl, prints per-minute progress, then prints an analysis
report on exit.

stdlib only. Polls /api/events/recent?n=500 every 3s and dedupes by event id.
(SSE via stdlib urllib on Windows can be flaky for long-lived streams; polling
is the robust path for a 15-minute measurement run.)
"""

import json
import time
import urllib.request
import urllib.error
from collections import defaultdict, Counter
from datetime import datetime

CC_URL = "http://localhost:9000"
TARGET_TYPES = {"TRADE_OPEN", "SIGNAL", "WHALE_ALERT", "REGIME_CHANGE"}
LOG_PATH = "D:/CommandCenter/aegis_tap_log.jsonl"
RUN_SECONDS = 900
POLL_INTERVAL = 3.0
PROGRESS_INTERVAL = 60.0


def fetch_recent(n=500):
    url = f"{CC_URL}/api/events/recent?n={n}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                if "events" in data and isinstance(data["events"], list):
                    return data["events"]
                if "data" in data and isinstance(data["data"], list):
                    return data["data"]
            return []
    except Exception as e:
        print(f"[fetch error] {type(e).__name__}: {e}")
        return []


def fetch_recent_typed(event_type, n=500):
    url = f"{CC_URL}/api/events/recent?n={n}&type={event_type}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                if "events" in data and isinstance(data["events"], list):
                    return data["events"]
                if "data" in data and isinstance(data["data"], list):
                    return data["data"]
            return []
    except Exception as e:
        print(f"[fetch typed error] {event_type} {type(e).__name__}: {e}")
        return []


def event_key(ev):
    """Stable dedup key for an event dict."""
    if not isinstance(ev, dict):
        return None
    if "id" in ev and ev["id"] is not None:
        return ("id", ev["id"])
    # fallback — hash of (type, source, ts, data-sig)
    t = ev.get("type", "")
    s = ev.get("source", "")
    ts = ev.get("timestamp") or ev.get("ts") or ""
    d = ev.get("data", {})
    dsig = ""
    if isinstance(d, dict):
        try:
            dsig = json.dumps(d, sort_keys=True)[:200]
        except Exception:
            dsig = str(d)[:200]
    return ("fb", t, s, str(ts), dsig)


def extract_row(ev):
    data = ev.get("data", {}) if isinstance(ev, dict) else {}
    if not isinstance(data, dict):
        data = {}
    ts = ev.get("timestamp") or ev.get("ts") or time.time()
    try:
        ts = float(ts)
    except Exception:
        ts = time.time()
    return {
        "ts": ts,
        "type": ev.get("type", ""),
        "source": ev.get("source", ""),
        "pair": data.get("pair", "") or data.get("symbol", "") or "",
        "direction": data.get("direction", "") or data.get("side", "") or "",
        "tier": data.get("tier", "") or data.get("severity", "") or "",
        "extra": data,
    }


def analyze(rows, start_iso, end_iso):
    print()
    print(f"=== AEGIS EVENT TAP: 15 min run ({start_iso} -> {end_iso}) ===")
    print()

    # TOTALS
    by_type = Counter(r["type"] for r in rows)
    whale_high_ext = [
        r for r in rows
        if r["type"] == "WHALE_ALERT"
        and str(r.get("tier", "")).upper() in ("HIGH", "EXTREME")
    ]
    print("TOTALS:")
    print(f"  TRADE_OPEN: {by_type.get('TRADE_OPEN', 0)}")
    print(f"  SIGNAL: {by_type.get('SIGNAL', 0)}")
    print(f"  WHALE_ALERT: {by_type.get('WHALE_ALERT', 0)}  (HIGH/EXTREME: {len(whale_high_ext)})")
    print(f"  REGIME_CHANGE: {by_type.get('REGIME_CHANGE', 0)}")
    print()

    # Rates
    minutes = RUN_SECONDS / 60.0
    print("RATES (events/min):")
    for t in ("TRADE_OPEN", "SIGNAL", "WHALE_ALERT", "REGIME_CHANGE"):
        print(f"  {t}: {by_type.get(t, 0) / minutes:.2f}")
    print()

    # PER-BOT TRADE ACTIVITY (TRADE_OPEN + SIGNAL)
    print("PER-BOT TRADE ACTIVITY (TRADE_OPEN + SIGNAL):")
    trade_signal_rows = [r for r in rows if r["type"] in ("TRADE_OPEN", "SIGNAL")]
    trade_signal_rows.sort(key=lambda r: r["ts"])
    by_bot = defaultdict(list)
    for r in trade_signal_rows:
        by_bot[r["source"] or "(unknown)"].append(r)

    print(f"  {'bot':<20} {'total':>6} {'uniq_pairs':>11} {'max_streak':>11} {'persistence':>12}")
    any_persistence = False
    for bot, events in sorted(by_bot.items(), key=lambda kv: -len(kv[1])):
        total = len(events)
        pairs = [e["pair"] for e in events if e["pair"]]
        uniq_pairs = len(set(pairs))
        max_streak = 0
        cur = 0
        prev = None
        for p in pairs:
            if p == prev:
                cur += 1
            else:
                cur = 1
                prev = p
            if cur > max_streak:
                max_streak = cur
        has_persistence = max_streak >= 2
        if has_persistence:
            any_persistence = True
        print(f"  {bot:<20} {total:>6} {uniq_pairs:>11} {max_streak:>11} {'YES' if has_persistence else 'no':>12}")
    if not by_bot:
        print("  (none)")
    print()

    # WHALE PAIR OVERLAP
    whale_pairs = set(r["pair"] for r in whale_high_ext if r["pair"])
    trade_pairs = set(r["pair"] for r in trade_signal_rows if r["pair"])
    intersection = whale_pairs & trade_pairs
    print("WHALE PAIR OVERLAP:")
    print(f"  Whale HIGH/EXT pairs ({len(whale_pairs)}): {sorted(whale_pairs) if whale_pairs else '[]'}")
    print(f"  Trade/signal pairs ({len(trade_pairs)}): {sorted(trade_pairs) if trade_pairs else '[]'}")
    print(f"  Intersection ({len(intersection)}): {sorted(intersection) if intersection else '[]'}")
    print()

    # REGIME CHANGES
    print("REGIME CHANGES:")
    regime_rows = [r for r in rows if r["type"] == "REGIME_CHANGE"]
    regime_counter = Counter()
    for r in regime_rows:
        src = r["source"] or "(unknown)"
        d = r.get("extra", {}) or {}
        old = d.get("from") or d.get("old") or d.get("previous") or ""
        new = d.get("to") or d.get("new") or d.get("regime") or ""
        regime_counter[(src, str(old), str(new))] += 1
    if regime_counter:
        for (src, old, new), c in sorted(regime_counter.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {src}: {old} -> {new} (x{c})")
    else:
        print("  (none)")
    print()

    # RETENTION STARVATION TEST
    print("RETENTION STARVATION TEST:")
    logger_trade_count = by_type.get("TRADE_OPEN", 0)
    logger_signal_count = by_type.get("SIGNAL", 0)
    api_trade = fetch_recent_typed("TRADE_OPEN", n=500)
    api_signal = fetch_recent_typed("SIGNAL", n=500)
    api_trade_count = len(api_trade)
    api_signal_count = len(api_signal)
    print(f"  Logger captured TRADE_OPEN: {logger_trade_count} over 15min")
    print(f"  /api/events/recent?type=TRADE_OPEN at end: {api_trade_count}")
    tratio = f"{logger_trade_count}/{api_trade_count}"
    print(f"  Ratio: {tratio}")
    print(f"  Logger captured SIGNAL: {logger_signal_count} over 15min")
    print(f"  /api/events/recent?type=SIGNAL at end: {api_signal_count}")
    sratio = f"{logger_signal_count}/{api_signal_count}"
    print(f"  Ratio: {sratio}")
    starved = (logger_trade_count > api_trade_count) or (logger_signal_count > api_signal_count)
    verdict = "STARVED" if starved else "CLEAN"
    print(f"  Verdict: {verdict}")
    print()

    # DECISION
    total_trade_signal = logger_trade_count + logger_signal_count
    if total_trade_signal < 10:
        case = "Case 1"
        justification = (
            f"Only {total_trade_signal} TRADE_OPEN+SIGNAL events in 15 minutes — "
            f"fleet is genuinely quiet, not a bus retention problem."
        )
    elif starved:
        case = "Case 2"
        justification = (
            f"Logger captured {total_trade_signal} trade/signal events but "
            f"/api/events/recent returned fewer ({api_trade_count}+{api_signal_count}) — "
            f"retention eviction confirmed."
        )
    else:
        if any_persistence:
            case = "Case 2 or mixed"
            justification = (
                f"Captured {total_trade_signal} events, counts match API, and persistence exists "
                f"— not starvation, not drive-by; deeper investigation needed."
            )
        else:
            case = "Case 3"
            justification = (
                f"Captured {total_trade_signal} events, counts match API, but no bot showed "
                f">=2 consecutive same-pair events — signal persistence model mismatch (drive-by pattern)."
            )
    print(f"DECISION: {case}")
    print(f"JUSTIFICATION: {justification}")
    print()


def main():
    print(f"[aegis_tap] starting, will run for {RUN_SECONDS}s")
    print(f"[aegis_tap] log path: {LOG_PATH}")
    print(f"[aegis_tap] target types: {sorted(TARGET_TYPES)}")
    print(f"[aegis_tap] polling {CC_URL}/api/events/recent every {POLL_INTERVAL}s")

    start = time.time()
    start_iso = datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M")
    seen_keys = set()
    rows = []
    counts = Counter()
    last_progress = start

    # Truncate log file
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write("")

    while True:
        now = time.time()
        elapsed = now - start
        if elapsed >= RUN_SECONDS:
            break

        events = fetch_recent(n=500)
        new_this_tick = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            t = ev.get("type", "")
            if t not in TARGET_TYPES:
                continue
            k = event_key(ev)
            if k is None or k in seen_keys:
                continue
            seen_keys.add(k)
            row = extract_row(ev)
            rows.append(row)
            counts[t] += 1
            new_this_tick += 1
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, default=str) + "\n")
            except Exception as e:
                print(f"[write error] {type(e).__name__}: {e}")

        if now - last_progress >= PROGRESS_INTERVAL:
            mins = int(elapsed // 60)
            print(
                f"[{mins:02d}m] TRADE_OPEN={counts['TRADE_OPEN']} "
                f"SIGNAL={counts['SIGNAL']} "
                f"WHALE_ALERT={counts['WHALE_ALERT']} "
                f"REGIME_CHANGE={counts['REGIME_CHANGE']} "
                f"(new this tick: {new_this_tick})"
            )
            last_progress = now

        # Sleep remainder of poll interval
        sleep_for = POLL_INTERVAL - (time.time() - now)
        if sleep_for > 0:
            time.sleep(sleep_for)

    end = time.time()
    end_iso = datetime.fromtimestamp(end).strftime("%Y-%m-%d %H:%M")
    print(f"[aegis_tap] run complete. total captured: {len(rows)}")
    analyze(rows, start_iso, end_iso)


if __name__ == "__main__":
    main()
