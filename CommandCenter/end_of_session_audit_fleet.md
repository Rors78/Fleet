# END-OF-SESSION AUDIT FLEET
## Dispatch Guide — April 2026

---

## PURPOSE

End-of-session cleanup is the most dangerous moment in the build. Context is about to evaporate. Whatever isn't verified, documented, and persisted RIGHT NOW becomes a ghost that the next session has to rediscover.

This is a fleet of 7 audit agents. Each has a single scope, a concrete checklist, exact verification commands, and clear PASS/FAIL criteria. Dispatch all 7 in parallel at the end of any session where code, config, or documentation changed.

**All curl commands in this document have been verified against the actual source code of `command_center.py`, `signal_broadcaster.py`, and `inference_server.py`. Do not modify endpoints without re-verifying them against source.**

---

## AGENT 1: WIRING AUDITOR
**Assigned to:** `fleet-auditor`
**Scope:** Did every code change this session actually land in running code?

### Checklist
- [ ] For every module added/modified this session: `grep -rn "import <module>" <bot_file>` confirms the import exists in the RUNNING bot, not just in `D:\CommandCenter\`
- [ ] For every new API endpoint: `curl -s http://localhost:9000/<endpoint>` returns valid response (not 404, not connection refused)
- [ ] For every new event type: `curl -s http://localhost:9000/api/events/recent` confirms at least one event of that type has been published
- [ ] For every new bus reaction rule: verify it exists in `reactions.json` AND test with a synthetic event publish
- [ ] For every dashboard change: reload browser, open DevTools console, confirm zero JS errors on load

### Verification Commands
```bash
# List all bot processes and ports
netstat -ano | findstr "807[0-9] 808[0-9] 9000 9001 9002"

# Verify CC is responding (CC's aggregated state endpoint is /api/master — NOT /api/status)
curl -s http://localhost:9000/api/master | python -m json.tool | head -40

# Verify event bus is flowing (watch for 10 seconds)
curl -s -N http://localhost:9000/api/events/stream --max-time 10

# Check recent events for new event types
curl -s http://localhost:9000/api/events/recent | python -m json.tool | grep '"type"'

# Sweep all bots — most use /api/snapshot, TrekBot uses /health
for port in 8070 8071 8072 8073 8074 8075 8076 8077 8078 8079 8082 8083 8084 8085 8086; do
  resp=$(curl -s --max-time 2 http://localhost:$port/api/snapshot 2>/dev/null | head -c 80)
  echo "Port $port (snapshot): ${resp:-DEAD}"
done
# TrekBot LONG and SHORT use /health + /positions + /analytics
for port in 8080 8087; do
  resp=$(curl -s --max-time 2 http://localhost:$port/health 2>/dev/null | head -c 80)
  echo "Port $port (health): ${resp:-DEAD}"
done
```

### PASS Criteria
Every change made this session has a runtime verification (grep + curl + live test). Zero "file exists but nothing imports it" situations.

### FAIL Triggers
- Any module that was "created" but has zero grep hits in running bot files
- Any endpoint that returns 404
- Any event type that was "added" but has zero bus events
- Any JS error in browser console after dashboard reload

---

## AGENT 2: DASHBOARD INTEGRITY AUDITOR
**Assigned to:** `cosmos-dashboard-alchemist`
**Scope:** Is `command_center_v4.html` syntactically valid and rendering correctly?

### Checklist
- [ ] Brace balance: total `{` count equals total `}` count
- [ ] Parenthesis balance: total `(` equals total `)`
- [ ] Bracket balance: total `[` equals total `]`
- [ ] Backtick count is even (no unclosed template literals)
- [ ] Browser console shows zero errors on fresh load (both grid view AND solar system view)
- [ ] All bot panels render in grid view (count panels = count bots)
- [ ] Solar system view: all planets render, labels visible, orbit paths visible
- [ ] Canvas post-inject pattern intact for any new/modified panels

### Verification Commands
```bash
# Delimiter balance — the ONE check that reliably catches the killer bug class
# (one unclosed brace silently kills the entire dashboard with no console error)
python -c "
t = open(r'D:\CommandCenter\command_center_v4.html', encoding='utf-8').read()
print(f'Braces:   {{ {t.count(chr(123))}  }} {t.count(chr(125))}  delta={t.count(chr(123))-t.count(chr(125))}')
print(f'Parens:   ( {t.count(chr(40))}  ) {t.count(chr(41))}  delta={t.count(chr(40))-t.count(chr(41))}')
print(f'Brackets: [ {t.count(chr(91))}  ] {t.count(chr(93))}  delta={t.count(chr(91))-t.count(chr(93))}')
bt = t.count(chr(96))
print(f'Backticks: {bt}  even={bt%2==0}')
"

# Line count — track bloat
wc -l D:\CommandCenter\command_center_v4.html
```

**NOTE: No automated JS syntax check.** A regex-strip `node -e` approach produces false FAILs on a 16k-line file with inline CSS, template literals containing `<` and `>`, and inline event handlers. The browser console on a fresh reload is the ground truth for JS validity on this file. If the delimiter balance passes and the browser console is clean on reload, the file is valid.

### PASS Criteria
All four delimiters balanced. Zero console errors on both views. All bots visible in both grid and solar system views.

### FAIL Triggers
- Any delimiter imbalance (one unclosed brace kills the entire dashboard)
- Console errors on load
- Missing bot panels or planets
- Canvas rendering artifacts

---

## AGENT 3: PERSISTENCE AUDITOR
**Assigned to:** `fleet-auditor`
**Scope:** Is anything new this session living only in RAM?

### Checklist
- [ ] Every new metric/counter/state variable: trace its write path — does it hit disk (JSON, SQLite, file) or only live in a Python dict/list?
- [ ] Every new configuration value: is it in `fleet_config.py` or hardcoded in a bot file? Hardcoded = reboot fragile
- [ ] Portfolio state: `curl -s http://localhost:9000/api/portfolio` — are all reservations accounted for? Any stale locks?
- [ ] Event bus history: does `/api/events/recent` survive a CC restart? (It's a 500-slot FIFO — confirm it writes to disk or accept it's ephemeral by design)
- [ ] Open trade state: every bot with open positions — does it persist them to disk? `grep -rn "json.dump\|sqlite\|write\|save" <bot_file>` for each trading bot
- [ ] AEGIS health scores: persisted or RAM-only?
- [ ] Dashboard state (solar system positions, panel states): acceptable to be ephemeral (UI state resets on reload are fine)

### The Rule
- If it matters for trading decisions, metrics, or history → it MUST write to disk on every update.
- If it's pure UI state → ephemeral is acceptable.
- If it's ambiguous → it writes to disk. Default to persistence.

### Verification
```bash
# Check for RAM-only patterns in any files modified this session
grep -rn "= {}" <modified_files> | grep -v "json\|sqlite\|save\|write\|persist"

# Portfolio reservation check
curl -s http://localhost:9000/api/portfolio | python -m json.tool

# Check stale reservations count
curl -s http://localhost:9000/api/portfolio | python -c "
import sys, json
d = json.load(sys.stdin)
res = d.get('reservations', {})
print(f'Active reservations: {len(res)}')
for k, v in res.items():
    print(f'  {k}: {v}')
"
```

### PASS Criteria
Zero new RAM-only state that matters for trading/metrics. All reservations accounted for.

### FAIL Triggers
- New state dict with no disk-write path
- Stale reservations from crashed/restarted bots
- Metrics that reset to zero on bot restart

---

## AGENT 4: API & CONNECTIVITY AUDITOR
**Assigned to:** `fleet-auditor`
**Scope:** Are all external connections and internal APIs healthy?

### Checklist
- [ ] Kraken live API: CC ticker endpoint returns live market data
- [ ] Telegram signal broadcaster (port 9002): health endpoint responding, feed endpoint serving
- [ ] SSE stream: produces events within 60 seconds
- [ ] AI Inference server (port 9001): health endpoint responding, Ollama reachable through it
- [ ] All 17 bots alive on their configured ports

### Verification Commands — ALL VERIFIED AGAINST SOURCE

```bash
# Kraken connectivity — CC ticker proxies Kraken, returns latest prices for universe pairs
# Verified endpoint at command_center.py:2316 "/api/market/ticker"
# NOTE: /api/market/overview does NOT exist — do not use it
curl -s http://localhost:9000/api/market/ticker | python -m json.tool | head -30

# Signal broadcaster health (port 9002)
# Verified endpoint at signal_broadcaster.py:326 "/health"
# NOTE: /api/status does NOT exist on this service
curl -s --max-time 5 http://localhost:9002/health
curl -s --max-time 5 http://localhost:9002/stats
curl -s --max-time 5 http://localhost:9002/feed | head -c 200

# SSE stream test (5 second sample)
timeout 5 curl -s -N http://localhost:9000/api/events/stream 2>/dev/null | head -20

# Inference server health (port 9001) — this also verifies Ollama is reachable,
# because /health internally queries Ollama's /api/tags
# Verified endpoint at inference_server.py:175 "/health"
# NOTE: /api/tags is Ollama's native endpoint on port 11434, NOT inference_server on 9001
curl -s http://localhost:9001/health | python -m json.tool

# If inference server /health is down, check Ollama directly on port 11434
curl -s http://localhost:11434/api/tags | python -m json.tool | head -10

# Full 17-bot sweep — use correct endpoint per bot type
echo "=== Standard bots (/api/snapshot) ==="
for port in 8070 8071 8072 8073 8074 8075 8076 8077 8078 8079 8082 8083 8084 8085 8086; do
  status=$(curl -s --max-time 2 http://localhost:$port/api/snapshot 2>/dev/null)
  if [ -z "$status" ]; then echo "Port $port: DEAD"; else echo "Port $port: ALIVE"; fi
done

echo "=== TrekBot LONG + SHORT (/health) ==="
for port in 8080 8087; do
  status=$(curl -s --max-time 2 http://localhost:$port/health 2>/dev/null)
  if [ -z "$status" ]; then echo "Port $port: DEAD"; else echo "Port $port: ALIVE"; fi
done

# CC itself uses /api/master for aggregated state (NOT /api/status)
echo "=== Command Center (/api/master) ==="
curl -s --max-time 2 http://localhost:9000/api/master > /dev/null && echo "CC: ALIVE" || echo "CC: DEAD"
```

### Port/Endpoint Reference (verified against source)
| Service | Port | Health Endpoint | Notes |
|---|---|---|---|
| Command Center | 9000 | `/api/master` | NOT `/api/status` |
| Inference Server | 9001 | `/health` | Verifies Ollama reachable |
| Signal Broadcaster | 9002 | `/health` | Also has `/stats` and `/feed` |
| Ollama (native) | 11434 | `/api/tags` | Upstream of inference_server |
| Standard bots | 8070-8086 (most) | `/api/snapshot` | 16 bots |
| TrekBot LONG | 8080 | `/health` + `/positions` + `/analytics` | 3 endpoints |
| TrekBot SHORT | 8087 | `/health` + `/positions` + `/analytics` | 3 endpoints |

### Kraken Market Endpoints (verified)
- `/api/market/ohlc?pair=BTC/USD&interval=60&limit=100` — single pair OHLC
- `/api/market/ohlc/bulk?pairs=BTC/USD,ETH/USD&interval=60` — bulk OHLC
- `/api/market/ticker` — **latest prices for all universe pairs, best for health check**

### PASS Criteria
All 17 bots alive. CC alive. Kraken ticker returning data. Telegram /health responding. SSE flowing. Inference /health responding.

### FAIL Triggers
- Any bot returning DEAD
- Kraken ticker returning errors or stale data
- Telegram /health not responding
- SSE stream silent for >60 seconds
- Inference /health down

---

## AGENT 5: DOCUMENTATION SYNC AUDITOR
**Assigned to:** `simons-fleet-philosopher`
**Scope:** Do the docs match what was actually built this session?

### Files to Check
1. **`D:\CommandCenter\README.md`** — Fleet overview. Update if: bots added/removed, ports changed, new infrastructure, new API endpoints, architecture changes.
2. **`D:\CommandCenter\CLAUDE.md`** — Claude Code working instructions. Update if: new conventions established, new gotchas discovered, agent selection guidance changed.
3. **Fleet context document** (the one Opus consumes) — Update if: any architectural change, new patterns discovered, new bugs found, new agents defined.
4. **Milestone doc** (`project_fleet_milestone_v*.md` in memory) — Append a session summary if significant work was done. Include: what was built, what was discovered, what's queued.
5. **Spec files in memory** — Any specs written this session should be verified as saved on disk and referenced in `MEMORY.md`.

### Checklist
- [ ] README.md reflects current bot count, port assignments, and architecture
- [ ] CLAUDE.md reflects any new conventions or gotchas from this session
- [ ] Fleet context doc has an entry for this session's work
- [ ] Any queued specs are saved to disk (not just in chat memory)
- [ ] No stale information in docs that contradicts what was built (old port numbers, removed features, renamed modules)

### Verification
```bash
# Check README last modified
stat D:\CommandCenter\README.md

# Check CLAUDE.md last modified
stat D:\CommandCenter\CLAUDE.md

# Grep README for current bot count / port assignments
grep -c "807[0-9]\|808[0-9]" D:\CommandCenter\README.md

# Check for stale references to removed/renamed things
grep -n "REMOVED_MODULE\|OLD_PORT\|DEPRECATED" D:\CommandCenter\README.md
```

### PASS Criteria
All docs reflect the current state of the fleet. A fresh Claude instance reading these docs would have an accurate picture of what exists.

### FAIL Triggers
- README says 16 bots when there are 17
- CLAUDE.md missing a convention that was established this session
- Fleet context doc doesn't mention work that was done
- Queued specs exist only in chat, not on disk

---

## AGENT 6: FLEET CONFIG CONSISTENCY AUDITOR
**Assigned to:** `fleet-auditor`
**Scope:** Are all config files, launch paths, and port assignments consistent?

### Checklist
- [ ] `launch_fleet.py` — every bot path points to an existing file, every port matches the bot's actual configured port
- [ ] `fleet_config.py` — all values match what bots are actually using at runtime
- [ ] `fleet_config.json` — ports match `fleet_config.py` (the `.py` is source of truth for launch; the `.json` is documentation/health-monitor reference)
- [ ] Port conflicts: no two bots on the same port (TrekBot LONG is 8080, TrekBot SHORT is 8087 — they are distinct)
- [ ] Directory paths: every bot directory in launch_fleet.py actually exists
- [ ] Python version compatibility: any new code uses syntax compatible with Python 3.14

### Verification Commands
```bash
# Extract paths from launch_fleet.py and verify they exist
python -c "
import re, os
with open(r'D:\CommandCenter\launch_fleet.py') as f:
    content = f.read()
paths = re.findall(r'[A-Z]:\\\\[^\"\\'\\s]+\\.py', content)
for p in sorted(set(paths)):
    exists = os.path.exists(p)
    print(f'{\"PASS\" if exists else \"FAIL\"}: {p}')
"

# Check for port conflicts in fleet_config.py
python -c "
import re
from collections import Counter
with open(r'D:\CommandCenter\fleet_config.py') as f:
    content = f.read()
ports = re.findall(r'[\"\\']port[\"\\']\\s*:\\s*(\\d+)', content)
ports += re.findall(r'port\\s*=\\s*(\\d+)', content)
dupes = {k: v for k, v in Counter(ports).items() if v > 1}
if dupes: print(f'DUPLICATE PORTS: {dupes}')
else: print(f'PASS: No port conflicts in {len(set(ports))} unique ports')
"
```

### PASS Criteria
All paths exist. All ports unique (TrekBot LONG/SHORT correctly on separate ports 8080/8087). All config values match runtime.

### FAIL Triggers
- Path to a bot file that doesn't exist (Deep Blue wrong-directory bug class)
- Port conflict
- fleet_config.py value that doesn't match what a bot is actually using

---

## AGENT 7: SESSION SUMMARY WRITER
**Assigned to:** `simons-fleet-philosopher`
**Scope:** Write the session summary that the next session reads first.

### Output Format
```markdown
## Session Summary — [DATE]

### What Was Built
- [Concrete list of changes with file paths]

### What Was Discovered
- [Bugs found, patterns identified, architectural insights]

### What's Queued (Not Started)
- [Specs written but not dispatched]
- [Known bugs not yet fixed]
- [Features discussed but not scoped]

### What Changed in Docs
- [Which docs were updated and what changed]

### Known Issues Carried Forward
- [Anything broken that wasn't fixed this session]
- [Anything that needs investigation next session]

### Agent Recommendations for Next Session
- [Which agent should be dispatched first and why]
```

### Checklist
- [ ] Every change this session is listed with its file path
- [ ] Every discovery (bug, pattern, insight) is documented
- [ ] Every queued item has enough context that a fresh session can pick it up without re-deriving it
- [ ] Known issues are honest — don't hide problems, don't minimize them
- [ ] Memory entries are updated if session context changed significantly

### PASS Criteria
A fresh Claude instance reading this summary + the fleet context doc + README + CLAUDE.md can start the next session without asking "what happened last time?"

### FAIL Triggers
- Changes made but not listed
- Queued items with insufficient context ("fix the thing" instead of specific file paths and line numbers)
- Known issues omitted

---

## DISPATCH ORDER

All 7 can run in parallel, but if dispatching sequentially:

1. **Wiring Auditor** (Agent 1) — catches highest-severity bugs (code that doesn't run)
2. **Dashboard Integrity** (Agent 2) — catches most visible bugs (broken UI)
3. **API & Connectivity** (Agent 4) — catches external failures (Kraken, Telegram)
4. **Persistence Auditor** (Agent 3) — catches reboot ghosts
5. **Fleet Config Consistency** (Agent 6) — catches launch/config drift
6. **Documentation Sync** (Agent 5) — updates docs based on what Agents 1-4 found
7. **Session Summary Writer** (Agent 7) — writes summary LAST, after all other audits report

Agents 5 and 7 run AFTER 1-4 because the audit agents may discover issues that need to be documented.

---

## QUICK-DISPATCH TEMPLATE

Copy-paste for end of session. Three dispatches cover all seven agents:

```
Dispatch to fleet-auditor (Agents 1, 3, 4, 6):
"Run end-of-session audit. This session's changes: [LIST CHANGES].
Execute Wiring Auditor, Persistence Auditor, API & Connectivity Auditor,
and Fleet Config Consistency checks per D:\CommandCenter\end_of_session_audit_fleet.md.
Show PASS/FAIL for every check. Do not report 'done' without evidence."

Dispatch to cosmos-dashboard-alchemist (Agent 2):
"Run dashboard integrity audit per D:\CommandCenter\end_of_session_audit_fleet.md.
Delimiter balance, console errors, both views, all panels.
Show PASS/FAIL for every check."

Dispatch to simons-fleet-philosopher (Agents 5, 7):
"Run documentation sync and write session summary per D:\CommandCenter\end_of_session_audit_fleet.md.
This session's changes: [LIST CHANGES].
Update README.md, CLAUDE.md, fleet context. Write session summary.
Every queued item must have enough context for a cold start."
```

---

*This document lives at `D:\CommandCenter\end_of_session_audit_fleet.md`.
Update it when new systems are added, new gotchas are discovered, or new audit patterns emerge.
All curl commands were verified against `command_center.py`, `signal_broadcaster.py`, and `inference_server.py` source code on first write. Re-verify before changing any endpoint.*
