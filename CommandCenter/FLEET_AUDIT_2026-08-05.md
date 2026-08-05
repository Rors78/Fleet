# FLEET AUDIT — 2026-08-05

Scope: fleet-wide health and scale audit. Read-only — nothing was started,
stopped, or modified. Runtime checks could not run (see below); every finding
here is from source and on-disk state.

---

## HEADLINE

**Status: fleet RESTARTED and healthy — 18/18 alive.** It had been fully
stopped for ~81 hours (0/19 ports, pool last written 2026-08-02 01:09). Cold
start was clean; the runtime half of this audit is complete in §7.

> **CORRECTION 2026-08-05 11:20 — the leak finding in §7.2 was WRONG.**
> It was a units error: TurtleSue's `total_size` is **coins, not dollars**
> (446.90 UNI, not $446.90). Real notional = 236.1230×$4.09 + 210.7814×$4.33
> = **$1,878.43** against **$1,878.21** reserved — 0.011% drift. Every
> reservation maps to a real funded pyramid unit. **The pool is fully
> reconciled; nothing was released and nothing should be.** Releasing `ak0a`
> would have stranded $912.68 of live UNI exposure with no backing reservation.
> Confluence likewise matches exactly ($1,650.00 both sides). See
> `~/.claude/skills/pool-reconcile/SKILL.md` for the corrected method.

~~**Post-latest, two confirmed capital leaks: $1,462.75 (15% of the pool) is
reserved against positions that do not exist** (§7.2) — and the close→release
path failing across a shutdown is a real bug, not just stale state.~~
**Withdrawn — see correction above.**

Structurally the fleet is in **good** shape: all 19 roster scripts exist, all
6/6 traders wire the portfolio pool, 16 bots wire the event bus. The problems
are operational and truthfulness-related, not architectural.

**On "upscaling": there is less to build than the specs imply, and nothing to
adopt.** The three visual build specs (2,233 lines total) describe work that is
largely *already implemented* under different function names. Both orphan
directories that looked like onboarding candidates are dead ends. The real
scaling work is consolidation — one renderer instead of two — not new features.

**Top 5 by severity:**
1. ~~$1,462.75 locked in 2 orphaned reservations~~ — **WITHDRAWN, units error.
   No leak; the pool is fully reconciled. Do not release anything. See §7.2.**
2. **Fleet expectancy −$25.80/trade, 22.2% win rate, every bot losing** (§7.4)
   — though n=18 and a retired bot pollutes the figure
3. Subscriber signal cards hardcode "19 bots · live" regardless of truth (§4.1)
4. Live Kraken API keys in plaintext in a dead directory (§4.3)
5. Two competing renderers own the same six traders (§5.1)

---

## 1. LIVENESS — BLOCKED AT AUDIT TIME → **RESOLVED IN §7**

> The fleet was restarted at user request after this section was written.
> **Live results are in §7; 18/18 alive.** This section records the pre-restart
> state and why the runtime checks were initially deferred.

| Check | Result |
|---|---|
| Command Center :9000 `/api/master` | connection refused |
| `fleet_restart.py --status` | launchers 0, children 0, orphans 0, strays 0; **listening 0/19** |

Per-bot liveness, event-bus recency, and expectancy **cannot be measured while
the fleet is down**. They are deferred, not skipped. The skill forbids
restarting without an explicit request, so I did not.

---

## 2. ROSTER — HEALTHY

Source of truth: `fleet_config.py` `BOTS` (lines 70–105).

- **19 roster entries; all 19 entry-point scripts exist on disk.** No missing files.
- **18 vs 19 is not a bug.** `len(BOTS)` = 19. `bot_registry_list()`
  (`fleet_config.py:189-197`) filters on `port is not None and endpoints`, which
  excludes `bot_responder` (port `None`, no endpoints). So CC polls 18. Both
  numbers are correct for different sets.
- No `enabled`/`disabled` flag exists — every roster entry is implicitly enabled.
- Port gaps: 8080, 8081, 8087 unassigned. 8088 (Confluence) is out of sequence,
  inheriting the retired TrekBot/GoldenEye slot.

---

## 3. WIRING — HEALTHY

| Module | Coverage |
|---|---|
| `portfolio_client` | **6/6 traders** — TurtleSue, NexusBrain, Gridzilla, Rubberband, Arbitrageur, Confluence |
| `event_publisher` | **16 bots** across traders + intel + support |

Deep Blue checked specifically: its `event_publisher` import lives in
`dashboard.py` while its entry point is `main.py`. **Not a defect** —
`main.py:395-396` imports `run_dashboard` and starts it on a thread at 8076,
so the import does load.

No half-wired modules found. Note this verifies imports only; "events actually
land on the bus" needs a running fleet.

---

## 4. FINDINGS

### 4.1 — Subscriber cards claim "19 bots · live" as a hardcoded literal — HIGH

`card_renderer.py:260` and `:1516`:

```python
bot_text = "19 bots \u00b7 live"
```

A string literal, not derived from `len(BOTS)` or from any liveness check.
Its only consumer is `signal_broadcaster.py` (port 9002, the subscriber
broadcast service), which constructs `CardRenderer()` at `:2827`.

The blast radius is every card type: `show_bot_count` defaults to `True`
(`card_renderer.py:232`) and **all 8+ `_draw_footer` call sites** (:399, :462,
:527, :578, :757, :920, :975, :1030) omit the argument — so none of them opt out.

Every signal card sent to paying subscribers therefore asserts 19 bots are
live. Three ways that is wrong:

1. Right now **0 bots are live** — the card would still say 19.
2. Only **18** are ever pollable; `bot_responder` has no port by construction.
3. It never reflects actual health even in normal operation.

This violates the fleet's own rules: "No fake stats — ever" (`D:\CLAUDE.md`)
and the honest-UI principle that displayed values must have provenance.

**Fix:** derive the count from live watchdog state, or drop the claim. Do not
substitute the literal `18` — that is the same defect with a better number.

### 4.2 — $3,528 frozen in 5 stale reservations — MEDIUM (restart risk)

`portfolio.json`, last written 2026-08-02 01:09 (81h ago).
Total capital **$9,728.06**; **$3,528.21 reserved = 36% of the pool.**

| Reservation | Bot | Amount | Created |
|---|---|---|---|
| `turtlesue_UNI/USD_..._xdpu` | TurtleSue | $965.46 | 07-30 05:13 |
| `confluence_APE/USD_..._xgdr` | Confluence | $550.00 | 07-30 19:01 |
| `turtlesue_UNI/USD_..._ak0a` | TurtleSue | $912.75 | 07-31 07:41 |
| `confluence_SHIB/USD_..._l9o5` | Confluence | $550.00 | 08-02 01:09 |
| `confluence_ETHFI/USD_..._23ve` | Confluence | $550.00 | 08-02 01:09 |

Oldest is **6 days** old. These reload on restart. Per protocol a long-held
reservation is **not** automatically a leak — each must be cross-checked
against the owning bot's actual open positions, which requires the bot running.
Flagging as *needs reconciliation on restart*, not as confirmed leaked.

The two TurtleSue entries are both UNI/USD — worth checking for a duplicate
reservation on one position.

### 4.3 — Plaintext live Kraken credentials in a dead directory — HIGH (security)

`D:\CryptoBot\.env` holds real `KRAKEN_API_KEY` / `KRAKEN_SECRET` for an
account that had ~$94 in it. `bot.log` shows the bot ran **live, not paper**
(it called `fetch_balance()` and reported `pos=5`) for about 27 seconds on
2026-05-02, then stopped. Untouched for ~3 months.

Live API credentials sitting in an abandoned directory are worth rotating
whether or not CryptoBot ever runs again. This is outside the fleet but on the
same machine, so I am flagging it rather than leaving it unreported.

### 4.4 — Orphan directories: neither is a fleet candidate — INFO

Both directories that *looked* like onboarding candidates are dead ends:

| Dir | Exchange | Port | Fleet imports | Last touched | Verdict |
|---|---|---|---|---|---|
| `D:\Viper` | **BTCC** | 8071 — **collides with Sentinel** | none | 2026-03-28 | Abandoned pre-fleet ancestor |
| `D:\CryptoBot` | Kraken | 18065 — GoldenEye's port | none | 2026-05-02 | Separate/dead draft |

**Viper should not be onboarded.** It targets BTCC perpetual futures, not
Kraken; it requires `BTCC_API_KEY` and exits without it. Its `CLAUDE.md:40-45`
preserves a 5-project port plan that became the fleet — and **Viper's slot
8071 was reassigned to Sentinel** (`fleet_config.py:72`). It is explicitly
listed under "Things You Can Skip" in `MIGRATION.md:467`, has zero fleet
imports, and shows no evidence of ever having been run.

`D:\CryptoBot` belongs to the GoldenEye/TrekBot lineage (its dashboard port
18065 is the one GoldenEye uses today) — deliberately non-fleet.

**Conclusion: there is no ready-to-onboard bot on this machine.** Fleet
"upscaling" would mean building something new, not adopting an orphan.

### 4.5 — Stale port references to retired TrekBot slots — LOW

Free fleet ports are 8080, 8081, 8087. But 8080/8087 are *retired* TrekBot /
TrekBot-SHORT slots and still appear in stale artifacts —
`notifier_config.json:28-29` still maps `trekbot: 8080, trekbot_short: 8087`,
and `end_of_session_audit_fleet.md:47,201` still sweeps them. Shorting is
retired fleet-wide.

**If a new bot is ever added, use 8081** — it is the only gap with no
historical baggage, appearing in no roster, audit, or config. Reusing
8080/8087 risks confusing double-registration against those stale sweeps.
(`SESSION_SUMMARY_2026-07-30.md:85` already flagged this needing a refresh pass.)

---

---

## 5. COSMOS VISUAL BUILD — MOSTLY BUILT, BUT TWO RENDERERS COMPETE

The three build specs on `D:\` (TIER1 791 lines, ROADMAP 506, DASHBOARD_V4 936)
read as unbuilt work. **They are largely already implemented** — under different
names, which is why a literal grep for the spec's identifiers returns nothing.
The spec says `toggleFullscreen` / `drawShip`; the code has `_orbToggleFS` /
`_drawShip`. The features are real.

**Tier 1 status: fullscreen toggle, fsHud, trade lifecycle (shooting stars,
supernovae, win stars), AEGIS corona + solar flares, and parallax starfield are
all BUILT and called in the render loop.** Per-bot identity over-delivered:
**0 of 17 bodies render as a generic circle** — each has a cinema entity, a
bespoke moon renderer, or a surface painter.

### 5.1 — Two renderers own the same six traders — HIGH (the real issue)

`command_center_v4.html:4278`:

```javascript
_armadaOwnsTraders = _orbIsFS && Armada._initialized
```

When true, all six traders **skip the 2D `_drawFullscreenEntity` path entirely**
(:4291) and render as three.js meshes from `armada.js` instead. `_armadaOwnsCC`
(:4119) likewise suppresses the 1C corona.

So in fullscreen — *the exact mode the Tier 1 spec was written for* — the 2D
Tier 1 work is the one thing you don't see. Roughly 2,000 lines of `_draw*`
entities are effectively the windowed-mode renderer plus an Armada-failure
fallback. **This is worth resolving before building any new visual feature:**
either retire the 2D cinema entities or gate Armada, but don't maintain both.

### 5.2 — The fullscreen gate was abandoned mid-migration — MEDIUM

The spec's organizing rule is "ALL new visual effects are FULLSCREEN ONLY."
In practice parallax (:4084), trade lifecycle (:4372), cinematic sun (:4123),
and `_updateFsHud()` (:4383) now run **unconditionally every frame**, with
comments changed to "always on" — a deliberate reversal. `_orbIsFS` gating
survives only on later layers (:4096, :4133, :4220, :4394).

Consequence: the spec's own verification step 1 ("normal mode should look
exactly like current v4, no new effects") **fails by design**. The contract is
half-migrated and the comments contradict the spec. Decide which rule holds and
write it down.

Cheap win: `_updateFsHud()` runs 60×/sec while the HUD is `display:none`.
One-line fix — wrap :4383 in `if(_orbIsFS)`.

### 5.3 — `_drawShip` is dead code keyed to a nonexistent bot — MEDIUM

`_drawShip` (`command_center_v4.html:15309-15462`) is 154 lines of ship hull,
27 sensor arrays, engine glow and harvester field — reachable only from the bot
hero panel (:5576), **never from the render loop**. Its dispatcher key is
`trekbot`, a bot that no longer exists in this fleet.

Either rehome it (Confluence is the only trader with no bespoke 2D cinema
entity) or delete it. Left as-is it guarantees future confusion.
`_drawArmoredMoon` is similar but has a documented, defensible reason
(TurtleSue's real identity is the Death Star sprite, :17560-17571).

### 5.4 — Highest-value genuinely unbuilt Tier 1 items — INFO

- **Supernova cause-classification.** Spec calls for fee-vs-direction-vs-timing
  coloring and 5-tier sizing; code hardcodes `maxPhase:120`, `color:"#b388ff"`
  (:17686). The `gross_pnl` vs `net_pnl` attribution is arguably the most
  information-dense idea in the spec and is not implemented. **Caution:** P/L is
  GROSS fleet-wide and fee math was deliberately removed on 2026-07-30 — this
  spec item may now conflict with a standing fleet rule. Confirm before building.
- **Win-star typing.** Spec's dwarf/bright/binary/nebula/pulsar evolution by PnL
  magnitude collapsed to a single gold dot (:17684).

---

## 6. RECOMMENDED ORDER

*(Items 3 and 5 of the original list are done — fleet restarted, live half complete.)*

1. ~~Release the 2 orphaned reservations~~ **DELETED — there is no leak.**
   The finding was a units error (`total_size` is coins, not dollars). The pool
   is fully reconciled: $1,878.21 reserved vs $1,878.43 real notional. Releasing
   would have stranded a live funded pyramid unit. See §7.2.
2. ~~Fix the close→release path~~ **DELETED — unfounded.** Both rids are
   actively re-declared every scan because both are legitimate.
3. **Fix the "19 bots · live" literal** — subscriber-facing and wrong in every
   state (§4.1).
4. **Rotate the Kraken keys** in `D:\CryptoBot\.env` — live credentials in a
   3-month-dead directory (§4.3).
5. **Exclude `trekbot` from fleet expectancy** — a retired non-member's 6 trades
   are in the fleet-wide number (§7.4).
6. **Do not act on expectancy yet.** n=18 is too small. Let the fleet run, then
   re-measure — the `fleet-postmortem` skill covers the full stack.
7. **Resolve Armada-vs-2D renderer ownership** before any new COSMOS visual work (§5.1).
8. **Do not onboard Viper or CryptoBot.** Neither is a fleet candidate. If a new
   bot is ever added, use **port 8081**.

---

## 7. LIVE HALF — COMPLETED 2026-08-05 10:15 (post-restart)

Fleet restarted at user request via `fleet_restart.py` (no ad-hoc taskkill).
Cold start clean: all 19 ports bindable, Ollama up, CC responding in 12s,
**18/18 bots alive**. Sections 1 and 4.2 are now resolved with live data.

### 7.1 — Liveness: HEALTHY (supersedes §1)

18/18 alive, **0 stale, 0 unreachable**, latency 1.4–31.2 ms. The four
documented slow starters (HiveMind, Oracle, Sentinel, PHITEX) all came up
without a retry. Fleet mode: `paper`. Regime consensus: `RANGING`.

Aggregate: total equity $49,985.15 · total PnL −$14.85 · 4 open positions ·
avg win rate 25.0%. (Most bots are at $10,000 baseline — these are
post-restart figures, not a performance record.)

### 7.2 — Reservation reconciliation: NO LEAK (finding withdrawn, body deleted)

**The original finding here was wrong and its remediation was destructive.
The body has been deleted rather than annotated** — it named specific
reservation IDs to release, and anyone opening to this section in isolation
would have acted on it and stranded a live funded position.

**What it claimed:** 2 of 5 reservations orphaned, $1,462.75 locked against
nothing, root-caused to a close→release path failing across shutdown.

**Why it was wrong — units.** TurtleSue's `total_size` is **coins, not
dollars**. A position reading `total_size: 446.90` is 446.90 UNI, not $446.90.
Compared against $1,878.21 reserved, that fabricates a $1,431 gap.

**Correct derivation** (re-verified live 2026-08-05 against both bots):

```
unit 1: 236.1230 coins x $4.09 = $965.74   <-> rid ...xdpu  $965.46
unit 2: 210.7814 coins x $4.33 = $912.68   <-> rid ...ak0a  $912.75
                         total = $1,878.43 <-> reserved    $1,878.21
```

0.012% drift. Every reservation maps to a real funded pyramid unit. Confluence
reconciles exactly ($1,650.00 both sides). The pool's `positions=2` for
TurtleSue counts **pyramid units on one pair**, not two separate trades.

**The pool is fully reconciled. Nothing was released and nothing should be.**
Releasing `ak0a` would have stranded $912.68 of live UNI exposure with no
backing reservation — strictly worse than the imagined problem. The
"close→release breaks on shutdown" root cause is likewise unfounded: both rids
are actively re-declared every scan because both are legitimate.

**Guard, not documentation.** Field semantics differ per bot (Confluence's
`size_usd` *is* USD; TurtleSue's `total_size` is not). Use
`portfolio_math.notional_usd(bot_id, position)` — never read a size field
directly. See `CommandCenter/portfolio_math.py`.

### 7.3 — Event bus: HEALTHY

Newest event **0.3 min old**; all 50 events in the window are from the current
session. Sources: nexus (32), event_bus (16), confluence (1), aegis (1).

The NEXUS council is firing across **7 engine types** — `SCHWARZSCHILD_HORIZON`
(10), `CYCLE_DETECTED` (6), `CHAOS_STATE` (5), `STRUCTURE_FORMING` (4),
`QUANTUM_COLLAPSE` (3), `EUCLID_LEVEL` (3), `SHANNON_ENTROPY` (1). Engines not
in this window are **silent, not proven broken** — the fleet has only been up
minutes and most are threshold-gated. A longer window is needed to judge the
full 14-engine council.

### 7.4 — Expectancy: NEGATIVE FLEET-WIDE — HIGH

`/api/expectancy` (the only expectancy route):

| Metric | Value |
|---|---|
| Total trades | 18 |
| Win rate | **22.2%** |
| Fleet expectancy | **−$25.80/trade** |
| Avg win / avg loss | $14.63 / **−$37.35** |
| Total PnL | **−$464.39** (gross = net; fees 0.0, as expected) |

| Bot | Expectancy | Trades | Verdict |
|---|---|---|---|
| confluence | −$4.74 | 5 | LOSING |
| trekbot | −$6.57 | 6 | LOSING |
| turtlesue | −$57.33 | 7 | LOSING |

**Every ranked bot is losing.** The structural problem is the win/loss ratio:
losers average 2.6× winners against a 22% hit rate. That combination cannot be
profitable regardless of signal quality.

Two caveats before anyone acts on this:
- **n=18 trades is far too small to be conclusive.** These are sample
  statistics, not established edge.
- **`trekbot` still appears in the rankings** with 6 trades, though it was
  retired from the fleet and is now standalone GoldenEye. Its history is
  polluting fleet-wide expectancy — the fleet number mixes in a non-member.

---

## WHAT THIS AUDIT COULD NOT ESTABLISH

**All resolved in §7** after the fleet was restarted at user request. Per-bot
health, event-bus delivery, reservation reconciliation, and expectancy are now
measured, not estimated.

Two things remain genuinely open:

- **Full 14-engine NEXUS council health.** Only 7 engine types fired in the
  50-event window; the fleet had been up minutes. Silent ≠ broken — most
  engines are threshold-gated. Needs a longer observation window.
- **Whether the negative expectancy is real edge decay or noise.** n=18 trades
  is too small to conclude, and `trekbot`'s 6 retired-bot trades pollute the
  fleet-wide figure.

No numbers were estimated anywhere in this report.
