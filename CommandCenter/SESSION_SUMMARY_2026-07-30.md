# Session Summary — 2026-07-30

**Merged to `master` as `4f11d95`** via PRs #5 and #6 on origin `fleet-master`. Fleet restarted on this commit and verified: 18/18 alive, 4/4 reservations verified on disk.

This was a measurement-integrity session. The theme: several of the fleet's own instruments were lying — one analyzer was permanently blind, one logger manufactured trades that never happened, and one janitor was releasing capital out from under live positions. None of these were visible as errors. All three are now fixed.

---

## What Was Built

### Measurement instruments that were silently wrong

**`D:\CommandCenter\fleet_logger.py` — phantom TRADE_OPEN root cause fixed.**
`FleetLogger` derives trade events by diffing each bot's reported positions against `_prev_positions`. That dict started empty on every CC start, so the first poll after a restart looked like every already-open position had just been opened. **Every CC restart emitted one phantom `TRADE_OPEN` per open position.** Five occurred on 2026-07-30, each correlated with a restart visible in the bus as a ~30s silence gap. Cold-start now seeds `_prev_positions` silently on first observation of each bot. Also added `LOGGER_ERROR` on silent tick-skip; removed write-only `_prev_bots`.

**`D:\CommandCenter\denial_cost.py` — was permanently blind, now sees.**
Two compounding bugs: the type filter never read the `event` field, and it scanned only `logs/events` while the `pair`/`amount` fields it needs exist only in `logs/event_bus`. It reported **0 denials, ever, since it was written**. It now scans both directories and finds **103 denials in 24h**. Added a `None`-timestamp guard. Every denial-cost output predating this fix is meaningless.

**`D:\CommandCenter\command_center.py` — position-aware stale reservation sweep (new).**
The sweep was age-only at 48h, so it released Confluence's APE/USD and MANA/USD reservations out from under positions legitimately held 66h. Added `_active_position_keys` and `force_release_stale(active_positions=...)`, which now cross-checks bot-reported positions before releasing. Also 9 bug fixes in this file (fleet-mode `None` guard, adaptive cooldown display, `_pair_opens` prune, 3 lock races, unused `force=` param removed, dead `/v2` route deleted, `if False` scaffold removed, unused locals) and a helper dedupe (`_require_portfolio`, `_serve_locked_snapshot`, `_alert_feed_entries`).

### Dead weight removed

- **`ultron.py`** — 4 dead TrekBot analyses deleted. One of them **emitted a false "bus wiring may not be active" warning into every weekly report**, which had been quietly discrediting a healthy bus for weeks. Constructor arg removed.
- **`evolution.py`** — TrekBot purged; **Confluence (8088) added to `BOT_PORTS`** — its trades had never been analyzed. Any evolution report predating this omits Confluence entirely.
- **`fleet_audit.py`** — `TREKBOT_PATHS`, branch logic, and dead fee findings removed.
- Deleted both orphaned `design_system.css` files.

### Smaller fixes

- `expectancy.py` — best/worst trade now uses `gross_pnl` (last fee-era leftover); `__main__` block added so `python expectancy.py` runs.
- `inference_server.py` — Ollama pinned to `num_gpu:0`. Ollama v0.32.5's CUDA runtime crashes (`0xc0000005`) on the Tesla P4 (Pascal). CPU `gemma2:2b` verified end-to-end.
- `weekly_analysis.py` — twin JSONL readers merged.
- `signal_aggregator.py` — `_decision_snapshots` given a 24h TTL (was an unbounded memory leak).
- `collector.py` — now uses `pearson` from `indicators.py` instead of a local reimplementation.
- `analyze.py` — added `import sys` (the error path crashed).
- `kraken_client.py` — fee docstring corrected.

### Operational

- Fleet fully restarted **twice** on new code. 18/18 alive. Verified **zero phantom TRADE_OPENs post-fix — the first clean restarts in fleet history with open positions.**
- Orphan reservation `mflk` ($961) released. Confluence APE/USD + MANA/USD re-reserved ($550 each).
- 5 phantom `TRADE_OPEN` rows purged from `logs/events` + `logs/event_bus`. Backups at `D:\CommandCenter\logs\purged_phantoms_backup\`.
- Ollama restarted post-reboot. The machine had rebooted at 17:46, killing the fleet.
- **git:** removed `core.worktree=D:/` from `D:\.git\config`. It was causing every git worktree to silently operate on the main checkout.
- **Telegram token moved to the fleet** (see below).

---

## What Was Discovered

- **A restart-shaped bug can masquerade as trading activity for months.** The phantom TRADE_OPENs were indistinguishable from real opens in the event log. The tell was correlation with ~30s bus silence gaps — that gap signature is now the documented fingerprint of a CC restart.
- **A blind analyzer reports zero, not an error.** `denial_cost.py` returning "0 denials" read as good news for its entire lifetime. The real number was 103/24h. Any metric whose healthy state and broken state produce the same output is not a measurement.
- **An age-only janitor cannot distinguish a leak from patience.** 66h-held positions are legitimate; the 48h sweep could not tell them from orphans. Age is a proxy for abandonment, never evidence of it.
- **A dead code path was actively spreading misinformation** — Ultron's TrekBot bus analysis emitted a false wiring warning into every weekly report. Dead code that still runs is worse than dead code that doesn't.
- **`/api/expectancy` still reports a historical `trekbot` bucket** (6 trades) sourced from archived event logs. It is not a live bot. Do not read it as fleet state.
- **Ollama's CUDA runtime is broken on Pascal** in v0.32.5. The failure is silent from CC's side — it just looks like inference is down.

---

## What's Queued (Not Started)

**Negative gross expectancy — THE strategic problem.**
Current: **-$31.09/trade gross, n=14, 28.6% win rate, avg win $14.63 vs avg loss -$49.38 (3.4×)**. This is a **loss-size problem**, not a frequency or friction problem — the fee war is over and won. Do not act on n=14. The sample dates only from the 2026-07-30 gross-P/L cutover; **~30+ closed gross-era trades are needed before a measurement pass is meaningful.** When the sample arrives, the measurement to run first is per-bot, per-regime loss-size distribution — specifically whether the 3.4× ratio is driven by a few tail losses or is uniform across trades, because those two shapes imply completely different diagnoses. Pull `/api/expectancy` fresh; never quote from memory.

**No fleet boot autostart.**
A machine reboot leaves the fleet down until `launch_fleet.py` is run by hand. This happened this session (17:46 reboot). Needs a scheduled task or service wrapper. `D:\CommandCenter\launch_fleet.py` is the entry point; note it runs CC as a thread inside its own process.

**`fleet_intel_score.py:227` reads `net_flow`, which `causal_flow` never emits.**
Causal trade bias is therefore permanently 0. Needs a design decision: either `causal_flow` starts emitting `net_flow`, or `fleet_intel_score` stops reading it. Do not "fix" by defaulting the value — that hides the gap.

**`signal_aggregator` charges win/loss to sources that voted against the trade direction.**
Possibly intentional (a source that voted against a loser arguably deserves credit), possibly a sign error. Unreviewed. Needs a decision, not a patch.

**Confluence's stored reservation ids are dead.**
Its stored ids `d5eu` / `3xve` no longer exist; the live ids after manual re-reservation are `xgdr` / `d9ve`. Confluence's close-time releases will **fail silently** against the dead ids. The fixed sweeper will reap the new ids ~48h after those positions close, so this self-heals — but if Confluence is restarted before then, verify its reservation state rather than assuming.

**`port_guard` incumbent check cannot verify port-holder identity.**
It only confirms *something* answers HTTP on the port, not that it is the right bot.

**Dual `TRADE_OPEN` emission by design.**
Bots emit natively and `FleetLogger` emits from its state diff, with different size semantics (USD vs units). Bus consumers double-count opens. This is intentional but undocumented at the consumer level.

**`D:\CommandCenter\end_of_session_audit_fleet.md` contains stale TrekBot steps.**
The audit spec still sweeps ports 8080/8087 and says "17 bots" / "all 17 bots alive". Memory checklists (daily inspection) have the same drift. The spec needs a refresh pass.

**Gridzilla reports no `equity` field** through its normalizer.

**Dashboard paren delta +3** — non-blocking, traced to a prose artifact.

---

## What Changed in Docs

### `D:\CommandCenter\README.md`
Was substantially stale — it still described a 17-bot fleet with fee-era economics.
- Bot count 17 → **16**; added the signal-product framing and the gross-P/L rule up front.
- **Removed both TrekBot rows** (8080/8087) from the fleet table; added a note that TrekBot is now standalone GoldenEye and that `/api/expectancy`'s `trekbot` bucket is historical, not live.
- **Added missing rows**: Confluence (8088), Broadcaster (9002), Bot Responder (no port).
- Corrected "7 trading bots share the pool" → **6**, named explicitly.
- Gridzilla description "with fee gates" → "1.2% spacing floor".
- Added the **Telegram ownership** note (fleet owns `@OracleQNTMCoreBot`; GoldenEye is silent).
- `/api/trades` example `bot=trekbot` → `bot=confluence`.
- Dashboard line count ~18K → ~17.4K; removed "fee analysis with trend" from the panel list; "16 bot detail tabs" → "Bot detail tabs".
- **Key Metrics rewritten** — the old "-$1.45/trade (110 trades), fee ratio 212%, target sub-100%" was pure fee-era fiction. Replaced with current gross figures, the sample-size caveat, real `PORTFOLIO_LIMITS` values, and the no-boot-autostart warning.
- **Added a Known Issues section** (the README had none).

### `D:\CommandCenter\CLAUDE.md`
Was already partially updated this session (`max_per_trade` 20%, Ultron description, expectancy/fee framing were correct and left alone).
- Added two new gotcha sections: **"Phantom TRADE_OPENs on CC Restart"** and **"Stale Reservation Sweeps Must Be Position-Aware"**, both with root cause and the carried-forward Confluence reservation-id consequence.
- Added the **Telegram ownership** note to `bot_responder.py`, including where GoldenEye's restore instructions live and the one-poller-only constraint.
- Added the **Ollama CPU pin** to the Inference Model Chain section, with the silent-failure warning.
- Expanded `denial_cost.py` with its blindness history and the "treat pre-2026-07-30 output as meaningless" warning.
- Added Confluence to the `evolution.py` description with the "prior reports omit Confluence" caveat.
- Updated the expectancy line in Key Metrics with current hard numbers, the `__main__` addition, and the historical-`trekbot`-bucket note.
- Corrected line counts: `command_center.py` ~3550 → ~4100; `command_center_v4.html` ~16k/~15.6k → ~17.4k (two places).

Not touched, per instructions: `command_center_v4.html` and all files with uncommitted user changes (`armada.js`, `solar_system.js`, `bot_responder.py`, `notifier.py`, `info_geometry.py`, `notifier_config.json`, `Nexus/nexus.py`, `COSMOS_ARMADA_SPEC.md`).

---

## Known Issues Carried Forward

Everything under **What's Queued** remains open. The honest summary:

1. **The fleet loses money per trade and we do not yet have the sample size to know why.** -$31.09/trade gross. Avg loss is 3.4× avg win. This is the only issue that matters strategically; the rest are hygiene.
2. **A reboot silently kills the fleet** and nothing brings it back.
3. **Inference runs on CPU** because the GPU crashes.
4. **Two measurement paths are known-degraded**: causal trade bias permanently 0, and signal_aggregator's attribution is of uncertain correctness.
5. **The audit spec that produced this report is itself stale** (TrekBot ports, 17-bot counts).

---

## Agent Recommendations for Next Session

1. **Do not dispatch a measurement agent on expectancy yet.** n=14 cannot support a conclusion. Wait for ~30+ gross-era closed trades. Dispatching `simons-fleet-philosopher` now would produce a confident-sounding reading of noise.
2. **First dispatch: whoever fixes the boot autostart.** It is the highest-value low-effort item — every hour the fleet is down after a reboot is lost sample toward the expectancy question that is actually blocking everything else. It compounds with #1.
3. **Then `fleet-auditor`** to refresh `end_of_session_audit_fleet.md` — remove TrekBot ports 8080/8087, correct the bot counts, and re-verify the curl commands against current source. An audit spec that checks for dead bots will keep producing false FAILs.
4. **`nexus-council-physicist`** for the `net_flow` design decision (`fleet_intel_score.py:227` vs `causal_flow`) — this is a question about what the engines should mean, not a wiring bug.
5. **When expectancy sample arrives**, dispatch `simons-fleet-philosopher` with a specific brief: decompose the 3.4× loss-size ratio by bot and regime, and determine whether it is tail-driven or uniform.
