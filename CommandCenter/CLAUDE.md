# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is
Unified mission control for a 16-bot crypto trading fleet. (TrekBot and TrekBot SHORT were removed from the fleet — TrekBot lives on as the standalone GoldenEye project. Confluence on port 8088 is the newest trader.) Polls each bot's API, normalizes metrics, manages a shared capital pool, runs an event bus for real-time inter-bot communication, and serves a combined dashboard on port 9000. Includes AI inference (Ollama), market data collection (Brainiac), self-evolution analysis (Ultron), and a Telegram signal broadcaster (port 9002).

**Purpose (2026-07-30):** the fleet is a SIGNAL PRODUCT — it will never trade real money itself. It paper-trades to measure signal quality, then broadcasts signals to subscribers (some in jurisdictions that allow shorting). Judge every design decision against signal quality, not live-execution safety.

See `README.md` for the high-level fleet overview and engine table. This file is the operational reference.

## Run
```bash
# Just Command Center (bots must be started separately)
cd D:\CommandCenter && python command_center.py

# Launch entire fleet + Command Center in one terminal
cd D:\CommandCenter && python launch_fleet.py

# Full cold restart (reap stale processes, relaunch, wait for health, open dashboard)
python fleet_restart.py            # also: --status | --stop | --no-browser

# AI inference server (requires Ollama running)
python inference_server.py

# Self-evolution analysis
python ultron.py --days 7

# Weekly AI-powered fleet report
python weekly_analysis.py --days 7
```
Dashboard: http://localhost:9000

## Analyze Fleet Logs
```bash
python analyze.py bot nexusbrain --last 7d --trades
python analyze.py trades --pair BTC/USD --last 7d
python analyze.py equity --last 7d
python analyze.py leaderboard --last 30d
python analyze.py regimes --last 7d
python analyze.py pairs --sort pnl --last 7d
python analyze.py correlation whale_alerts trades --last 30d
python analyze.py uptime trinity --last 7d

# Evolution engine — self-improvement analysis
python evolution.py --days 7
```

## Architecture

### Core Files
- `command_center.py` — Single-file backend (~4100 lines): bot polling, 16 normalizers (11 named functions + 5 inline lambdas), portfolio manager, HTTP server (port 9000) with route-table dispatch, universe discovery, market data proxy, event bus integration, Brainiac integration. Imports `signal_aggregator`, `signal_decomposition`, `expectancy`, `signal_decay`, and `fleet_intel_score` directly — these are NOT standalone-only.
- `command_center_v4.html` — Current active dashboard (~17.4k lines of inline HTML/CSS/JS). Served at `/`. No longer fully single-file: the COSMOS visualization lives in two companion scripts it loads — `solar_system.js` (canvas solar system: planets, star systems, DeepField) and `armada.js` (WebGL mining-ship armada per `COSMOS_ARMADA_SPEC.md`, a deferred ES module with its own rAF render loop). three.js r178 is vendored as root-level `three.module.min.js` / `three.core.min.js` (NOT under `static/`) because CC's static route rejects subdirectory paths — an importmap maps the bare `"three"` specifier to the root copy. `audio/` holds the NASA sample library for the `_sound` system.
- `signal_broadcaster.py` — Standalone Telegram signal service on port 9002. Subscribes to CC event bus via SSE, formats alerts, pushes to Telegram. Runs as Phase 2 support bot. Dashboard at `signal_dashboard.html` (served via CC at `/signals`).
- `bot_responder.py` — Telegram bot command responder (paired with signal_broadcaster). **The fleet owns `@OracleQNTMCoreBot` outright as of 2026-07-30** — `bot_responder` is the sole poller and sender. GoldenEye was made Telegram-silent for this: its `Desktop\launch.bat` token lines were replaced with explicit empty sets (restore instructions are in comments there), and `D:\GoldenEye\subscribers.json` has `fleet_pulse` set `active:false`. GoldenEye's subscriber channel (`-1003615829313`) no longer receives cards. Only one process may hold the token — a second poller causes 409s and duplicate sends.
- `card_renderer.py` — PNG signal-card generator (PIL/Pillow) for Telegram sendPhoto; paid tier gets images, free tier stays text. Maps internal bot names to subscriber-facing display names (e.g. confluence→Concord, turtlesue→Stalker) — internal names are never shown to subscribers. The card narrative prose is the product voice — don't strip it.
- `channel_content.py` — CLI to publish/pin channel descriptions and guide posts to both Telegram channels (`--preview`, `--send-all`, `--set-descriptions`). Reads `signal_config.json`.
- `backups/` — Archived older dashboard versions (v3, etc.).

### Event Bus System
Real-time pub/sub replacing 4-second polling for inter-bot communication:
- `event_bus.py` — In-process `EventBus` class with SSE broadcast and reaction rules. Supports filtered subscriptions, configurable reactions with condition evaluation (e.g., `count(REGIME_CHANGE, last_5min) >= 3`).
- `event_publisher.py` — Lightweight fire-and-forget client for bots to push events. Stdlib only (urllib), non-blocking via daemon threads.
- `bus_listener.py` — Client for bots to consume fleet intelligence. Polls `/api/events/recent` with configurable interval (default 10s) and exponential backoff on failure. Constructor accepts `cc_url` and `poll_interval` overrides. Provides typed accessors: `whale_alerts()`, `phitex_status()`, `aegis_regime()`, `convergent_signals()`, `emergency_active()`, `newton_force()`, `euclid_levels()`, `chronos_alerts()`, etc.
- `reactions.json` — Declarative reaction rules (whale amplifier, regime shift storm, convergent signal detection, cascade stop warning, portfolio overweight alert).

### Data Collection & Analysis
- `collector.py` — Brainiac: 5 background threads collecting order book depth, recent trades, global metrics (CoinGecko), correlation matrix, and funding rates. Stores to `brainiac/` as JSONL. Also registers `/api/brainiac/*` endpoints on the HTTP handler.
- `fleet_logger.py` — Writes snapshots (60s), events (trade opens/closes, regime changes), daily summaries, and AI trade journals to `logs/`. Imported by command_center.py.
- `analyze.py` — CLI tool: 8 subcommands for fleet diagnostics reading `logs/` JSONL.
- `ultron.py` — Self-evolution engine: analyzes regime stability, portfolio efficiency, bot utilization, and timing patterns. (The four TrekBot-log analyses — gates, signal quality, bus effectiveness, shadow trades — were deleted 2026-07-30; their data sources died when TrekBot left the fleet, and the bus check emitted a false warning weekly.) Feeds findings to AI for synthesis.
- `weekly_analysis.py` — Aggregates events + journals + Brainiac data + Ultron analysis into an AI-powered weekly report.
- `evolution.py` — Evolution engine: 5-step cycle (measure→analyze→propose→simulate→recommend). Reads event logs, queries live bots, identifies profitable/losing patterns, generates ranked parameter change proposals. Saves reports to `logs/evolution/`. TrekBot was purged and **Confluence (8088) was added to `BOT_PORTS` on 2026-07-30** — its trades had never been analyzed by this engine. Any evolution report predating that omits Confluence entirely.

### Shared Libraries
- `standards.py` — Canonical regime labels (`BULL`, `BEAR`, `RANGING`, `VOLATILE`, `TRANSITIONING`), `REGIME_MAP` for normalizing any bot's regime label, `normalize_pair()` for Kraken pair formats.
- `portfolio_client.py` — Thin stdlib-only client for bots to reserve/release capital from the central pool.
- `fleet_config.py` — Python module single-source-of-truth for bot ports/paths (distinct from `fleet_config.json`). Bots and `launch_fleet.py` import this instead of hardcoding. Key helpers: `get_bot()`, `get_traders()`, `get_intel()`, `is_blacklisted()`, `get_deployment_limits()`.
- `indicators.py` — Fleet-wide shared technical indicator library (stdlib only, no deps). Single source of truth for EMA, SMA, RSI, StochRSI, MACD, Bollinger Bands, ATR, ADX, Donchian, VWAP, volume momentum, Pearson correlation. Extracted from Trinity. Bots should import from here rather than reimplementing.
- `kraken_ohlc.py` — Canonical OHLC fetch (stdlib only). Tries CC proxy first (`/api/market/ohlc`), falls back to direct Kraken REST. Returns `[timestamp, open, high, low, close, volume, count]`. Never raises — returns `[]` on failure.
- `port_guard.py` — Call `ensure_port(port, bot_name)` on startup; kills zombie processes occupying the port before the HTTP server binds.
- `notifier.py` — Independent external watchdog (no fleet imports). Monitors CC health via Telegram alerts. Config in `notifier_config.json`. Env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- `kraken_client.py` — Shared Kraken spot trading client (stdlib only, no ccxt). Signs private REST requests; long spot only in live mode; refuses market orders. Reads `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` from env.
- `limit_order.py` — Shared marketable-limit pricing. LIMIT ORDERS ONLY is fleet policy — `limit_price(ref_price, side)` crosses the spread by 15 bps (kept in step with `fleet_config.LIMIT_CROSS_PCT`, with a local fallback) so orders fill like market orders normally but rest instead of eating a gapped book.

### Advanced Analytics Engines
Standalone modules that read event logs / live bot data and return structured insights. None are imported by `command_center.py` — they run independently or are called by `evolution.py` / `weekly_analysis.py`.

**Signal quality** (all imported directly by `command_center.py`; also exposed via `/api/signals/*`):
- `expectancy.py` — Tracks per-bot and fleet-wide E[V] = (WinRate × AvgWin) − (LossRate × AvgLoss), gross. The primary profitability signal.
- `signal_aggregator.py` — Ensemble engine: collects proposals from all bots, weights by historical accuracy, outputs a single BUY/SELL/HOLD score per pair. State persisted to `aggregator_state.json`.
- `signal_decomposition.py` — Attributes unique P/L contribution to each signal source (marginal value, accuracy, cost-adjusted expectancy).
- `signal_decay.py` — Applies empirical half-life decay to signals before consumption; tracks per-type half-lives.
- `fleet_intel_score.py` — Synthesizes all engine outputs into per-pair intelligence scores by polling NEXUS snapshot + event bus.
- `causal_flow.py` — Granger causality graph between bots, pairs, and events; answers "does X actually precede Y?" Wired in NEXUS; emits `CAUSAL_FLOW`.
- `shannon.py` — Information theory: mutual information between signals, channel capacity per bot-to-bot link, entropy of the event stream. Wired in NEXUS; emits `SHANNON_ENTROPY` (fires when fleet noise ratio > 70%).
- `denial_cost.py` — Portfolio denial opportunity cost analyzer. Reads `PORTFOLIO_RESERVE_DENIED` events, computes what denied trades would have netted. Usage: `python denial_cost.py --days 3 --hold-minutes 240`. **Was permanently blind until 2026-07-30** — its type filter never read the `event` field, and it scanned only `logs/events` while the `pair`/`amount` fields it needs exist only in `logs/event_bus`. It now scans both and finds ~103 denials/24h where it previously reported 0 forever. Treat any pre-2026-07-30 denial-cost output as meaningless.

Retired analytics (`regime_expectancy.py`, `signal_attribution.py`, `factor_calibration.py` — all depended on TrekBot's `goldeneye_factors.log`, frozen since TrekBot left the fleet) live in `graveyard/` with the evidence in `graveyard/MANIFEST.md`.

**Market geometry / physics:**
- `info_geometry.py` — Fisher Information Metric: measures how fast the market's return distribution is changing shape (low = stable regime, high = transition). Wired in NEXUS; emits `MANIFOLD_WARNING`.
- `topology.py` — Persistent homology on price point clouds: detects structural market features that survive across time scales. Wired in NEXUS; emits `CYCLE_DETECTED`.
- `quantum_state.py` — Superposition model: market holds multiple regime states simultaneously with amplitudes; collapses on measurement. Wired in NEXUS; emits `QUANTUM_COLLAPSE` (silent — threshold not met in current regime).
- `lorenz.py` — Strange attractor mapping in phase space; Lyapunov exponent estimates predictability horizon. Wired in NEXUS; emits `CHAOS_STATE` (fires when attractor departure > 0.5).
- `boltzmann.py` — Statistical mechanics of the order book: temperature (spread × volume), pressure (bid/ask imbalance), entropy (disorder). Wired in NEXUS; emits `BOOK_PHASE` (fires on BOILING/PLASMA state).
- `prigogine.py` — Dissipative structures: detects when the market is far-from-equilibrium and spontaneously self-organizing (regime birth). Wired in NEXUS; emits `STRUCTURE_FORMING` (fires when structure_formation_score > 0.4). Requires 210 candles — NEXUS fetches limit=300 to ensure margin.
- `thom.py` — Catastrophe theory: classifies imminent regime transitions as fold, cusp, or swallowtail catastrophes. Wired in NEXUS; emits `CATASTROPHE_WARNING` (fires when ews_score > 0.6).

**Diagnostics:**
- `fleet_audit.py` — 6-phase full-system diagnostic; outputs JSON + human-readable report. Run ad-hoc: `python fleet_audit.py`.

### Infrastructure
- `launch_fleet.py` — Two-phase launcher. Imports `BOTS` from `fleet_config.py` (single source of truth). Phase 1: core bots. Phase 2 (after CC is up): CC-dependent bots (Sentinel, PHITEX, AEGIS, Confluence, Inference, NEXUS, Rubberband, Contrarian, Arbitrageur, Chronos, Broadcaster, Bot Responder). Ctrl+C shuts down everything. **Runs CC as a thread inside its own process** — restarting CC means restarting the launcher. Never run two launch_fleet processes: their watchdogs (plus CC's `_health_monitor`) will double-spawn support bots (caused duplicate Telegram sends 2026-07-28).
- `fleet_restart.py` — State-of-the-art cold start: reap stale processes, verify clean, launch, wait for actual health, open dashboard. Flags: `--status` (report only), `--stop` (reap only), `--no-browser`. Prefer this over ad-hoc taskkill: it identifies the detached launcher by command line (a naive orphan check would kill it), escalates CTRL_BREAK→`/F` (plain `taskkill` sends WM_CLOSE which console Python ignores while reporting success), and knows CC runs as a thread inside the launcher.
- `morning_briefing.py` — Daily 07:00 mission-briefing digest to Jeremy's PERSONAL Telegram chat (never subscriber channels). Standalone, stdlib only, no fleet imports; runs once and exits via Windows Task Scheduler task "FleetMorningBriefing". Config: `briefing_config.json` (env vars override). Every section degrades gracefully — sends FLEET OFFLINE if CC is down, attempts a minimal failure message if the build itself blows up. `--no-send` builds and prints only. Logs to `briefing.log`.
- `inference_server.py` — Port 9001. Proxies to Ollama (Tesla P4). Endpoints: `/api/ai/trade-journal`, `/api/ai/post-mortem`, `/api/ai/fleet-assessment`.
- `fleet_config.json` — Bot registry with ports/dirs/cmds/phases (read by `_health_monitor` for auto-restart). Also documents fleet-wide settings (portfolio limits, event bus, Brainiac intervals, market data) but these sections are **not loaded by code** — the corresponding constants are hardcoded in `command_center.py` and `collector.py`. Keep in sync manually.
- `universe.json` — Config for auto-discovering top 50 Kraken USD pairs by volume.
- `portfolio.json` — Persisted portfolio state (created at runtime, atomic writes).

### Data Flow
1. `_poll_loop()` polls all bots every 4s via HTTP
2. Per-bot normalizer standardizes responses into common schema: `{pnl, positions, win_rate, regime, signals, ...}`
3. `_compute_aggregate()` builds fleet-wide stats
4. `_extract_feed()` merges signal/alert feeds, dedupes
5. `EventBus` provides real-time pub/sub — bots publish via POST, consume via SSE or `BusListener`
6. `FleetLogger` diffs state every 60s, emits events for trade opens/closes/regime changes
7. `BrainiacCollector` runs 5 threads collecting market data to `brainiac/`
8. `CommandCenterHandler` (stdlib HTTPServer) serves HTML + JSON APIs
9. `_universe_worker()` refreshes tradeable pairs from Kraken every 6 hours

### Thread Model
- Main thread: HTTP server
- Daemon thread 1: `_poll_loop()` (4s interval)
- Daemon thread 2: `FleetLogger._run()` (60s interval)
- Daemon thread 3: `_universe_worker()` (6h interval)
- Daemon threads 4-8: Brainiac collectors (depth 30s, trades 60s, global 5min, correlations 15min, funding 5min)
- Daemon thread 9: `_health_monitor()` (30s interval) — detects crashed bots, auto-restarts from `fleet_config.json`, publishes `BOT_DOWN`/`BOT_RECOVERED`/`BOT_RESTARTED` events after 3 consecutive failures
- Per-event daemon threads: `EventBus._check_reactions()` (spawned per published event)
- All shared state protected by `_lock` (bots/aggregate/feed), `PortfolioManager._lock` (instance-level), `_universe_lock`

## Fleet
Authoritative roster: `fleet_config.py` (`BOTS` dict) — trust it over this table if they disagree.

| Bot | Port | API Route | Role |
|---|---|---|---|
| TurtleSue | 8070 | /api/snapshot | Trader (pool), `turtlebot.py` |
| Sentinel | 8071 | /api/snapshot | Forecast (Phase 2, slow start) |
| Trinity | 8072 | /api/snapshot | Scanner (intel only), `overwatch.py` |
| HiveMind | 8073 | /api/snapshot | Optimizer (intel only, slow start ~30s), `cli.py dashboard --synthetic` |
| NexusBrain | 8074 | /api/snapshot | Trader (pool), `nexus_brain.py` — distinct from NEXUS |
| Oracle | 8075 | /api/snapshot | Intel only, `server.py` (slow start) |
| Deep Blue | 8076 | /api/snapshot | Intel only (whale detection), dir `D:\Whale Watcher\apex_whale_finder.dir\` |
| Gridzilla | 8077 | /api/snapshot | Trader (pool) |
| PHITEX | 8078 | /api/snapshot | Novel (thermodynamic, Phase 2, slow start) |
| AEGIS | 8079 | /api/snapshot | Meta (self-assessment, Phase 2) |
| NEXUS | 8082 | /api/snapshot | Novel (14-engine math council, Phase 2) — distinct from NexusBrain |
| Rubberband | 8083 | /api/snapshot | Trader (pool, Phase 2) |
| Contrarian | 8084 | /api/snapshot | Intel only (sentiment, Phase 2) |
| Arbitrageur | 8085 | /api/snapshot | Trader (pool, Phase 2) |
| Chronos | 8086 | /api/snapshot | Temporal (Phase 2) |
| Confluence | 8088 | /api/snapshot | Trader (pool, Phase 2), dir `D:\Confluence\` — newest trader |
| **Inference** | **9001** | /api/ai/* + /health | **AI (Ollama, Phase 2)** |
| **Broadcaster** | **9002** | /health + /stats + /feed | **Telegram signals (Phase 2)** |
| **Bot Responder** | — | (no port) | **Telegram commands (Phase 2)** |
| **Command Center** | **9000** | serves all APIs below | **Aggregator** |

## HTTP API (Port 9000)

**GET endpoints:**
- `/` — HTML dashboard
- `/api/master` — aggregated fleet state (bots + aggregate + feed + portfolio)
- `/api/universe` — current tradeable pairs list
- `/api/portfolio` — full portfolio state
- `/api/portfolio/available` — free capital
- `/api/portfolio/exposure` — breakdown by bot/pair/direction
- `/api/fleet/daily` — today's daily stats from FleetLogger
- `/api/trades?bot=confluence&limit=50` — persistent trade history from event logs (survives bot restarts)
- `/api/expectancy` — fleet-wide and per-bot expectancy stats (the ONLY expectancy route — `/api/signals/expectancy` never existed despite being documented here for months; verified against git history 2026-07-30)
- `/api/bot/<id>` — raw passthrough to individual bot
- `/api/market/ohlc?pair=BTC/USD&interval=60&limit=100` — OHLC proxy
- `/api/market/ohlc/bulk?pairs=BTC/USD,ETH/USD&interval=60` — bulk OHLC
- `/api/market/ticker` — latest prices for all universe pairs
- `/api/events/stream` — SSE real-time event stream
- `/api/events/recent?n=50&type=TRADE_OPEN` — recent events (catch-up)
- `/api/events/stats` — bus statistics
- `/api/brainiac/{depth|trades|correlations|funding|metrics}` — Brainiac data
- `/api/signals/decide?pair=BTC/USD` — ensemble BUY/SELL/HOLD decision from `SignalAggregator`
- `/api/signals/rankings` — per-source accuracy rankings
- `/api/signals/decomposition` — per-signal P/L attribution from `SignalDecomposition`
- `/api/signals/decay` — per-signal type half-life data from `SignalDecay`
- `/api/signals/intel?pair=BTC/USD` — composite intelligence score from `FleetIntelScore`
- `/api/fleet/mode` — current fleet trading mode (paper/live)

**POST endpoints:**
- `/api/portfolio/reserve` — bot requests capital `{bot_id, pair, direction, amount}`
- `/api/portfolio/release` — bot returns capital `{reservation_id, pnl}`
- `/api/events/publish` — bot pushes event `{source, type, data}`
- `/api/signals/propose` — bot submits signal proposal to ensemble `{source, pair, direction, confidence, ...}`
- `/api/signals/outcome` — bot reports trade outcome for ensemble learning `{pair, direction, won, pnl}`
- `/api/fleet/mode` — set fleet mode `{mode: "paper"|"live"}` (requires API keys for live)

**Inference API (Port 9001):**
- `POST /api/ai/trade-journal` — AI journal entry for a trade
- `POST /api/ai/post-mortem` — AI analysis of trade batch
- `POST /api/ai/fleet-assessment` — AI fleet state assessment
- `GET /health` — inference server status + model info

## Central Portfolio
6 trading bots (TurtleSue, NexusBrain, Gridzilla, Rubberband, Arbitrageur, Confluence) share one $10,000 pool.

### Risk Limits
- Max total deployed: 80% (always keep 20% cash)
- Max per bot: 30%
- Max per pair: 20%
- Max per trade: 20% (`max_per_trade_pct` in `fleet_config.py`)
- Max directional: 60% (long or short)

### Bot Integration
Bots opt in via config flags `use_central_portfolio: True` + `command_center_url`. They call `/api/portfolio/reserve` before opening and `/api/portfolio/release` after closing. Use `PortfolioClient` from `portfolio_client.py` (stdlib only). Fallback to local balance when Command Center is unreachable.

## Event Bus Integration
Bots publish events via `EventPublisher` (from `event_publisher.py`) and consume via `BusListener` (from `bus_listener.py`). Event types: `TRADE_OPEN`, `TRADE_CLOSE`, `SIGNAL`, `REGIME_CHANGE`, `WHALE_ALERT`, `PORTFOLIO_RESERVE`, `PORTFOLIO_DENIAL`, `BOT_STATUS`, `PRICE_ALERT`, `ATTENTION`, `FLEET_ALERT`, `HIGH_CONVICTION`, `EMERGENCY_REDUCE`, `PHITEX_UPDATE`, `AEGIS_UPDATE`, `NEWTON_FORCE`, `NEWTON_REACTION`, `EUCLID_LEVEL`, `SENTIMENT_EXTREME`, `CHRONOS_ALERT`.

Reaction rules in `reactions.json` trigger automatic fleet-wide alerts (e.g., 3+ stops in 5 min triggers `EMERGENCY_REDUCE`).

## Logs Structure
```
logs/
  snapshots/YYYY-MM-DD.jsonl  — full fleet state every 60s
  events/YYYY-MM-DD.jsonl     — trade opens/closes, regime changes, whale alerts, status changes
  daily/YYYY-MM-DD.json       — midnight summary (fleet PnL, per-bot stats, uptime)
  journals/YYYY-MM-DD.jsonl   — AI trade journal entries
  weekly/YYYY-MM-DD.json      — weekly analysis reports
  ultron/YYYY-MM-DD.json      — self-evolution analysis
brainiac/
  depth/YYYY-MM-DD.jsonl      — order book snapshots
  trades/YYYY-MM-DD.jsonl     — recent trade flow
  metrics/YYYY-MM-DD.jsonl    — global market metrics (CoinGecko)
  correlations/YYYY-MM-DD.jsonl — pair correlation matrices
  funding/YYYY-MM-DD.jsonl    — funding rates (Kraken Futures)
```
Retention: snapshots 90d, events 365d, daily 365d. Rotated on startup.

## Dependencies
- `requests` (Command Center backend — all bot-side clients use stdlib urllib)
- `Pillow` (only for `card_renderer.py` signal-card PNGs)

`requirements.txt` is a full machine-environment pip freeze, not a curated list for this project — don't treat everything in it as a fleet dependency.

## Testing
No test suite. Validation is runtime only:
1. After wiring any module: `grep -n "from module_name" target_bot.py` must show the import in the actual bot file
2. Test the API endpoint: `curl -s http://localhost:9000/api/endpoint`
3. Check the event bus: `curl -s http://localhost:9000/api/events/recent?n=500` — the expected event type must appear
4. A file existing in `D:\CommandCenter\` is NOT done. An import line in a bot file is NOT done. Events in the bus = done.

**Never report a module as complete without all three of the above.**

## Key Patterns & Gotchas

### Broadcast Transport Layer (Stage 1, 2026-08-05)
`signal_broadcaster.py` now has a platform-agnostic send seam. `TierRouter`,
`CardFormatter`, and `CardRenderer` were already platform-neutral; only the
send path knew about Telegram.

- **`Transport`** — protocol: `send_text(tier, ...)`, `send_image(tier, ...)`,
  `stats()`. `tier` stays in the interface because routing produces tier
  decisions; each transport maps tiers onto its own reality.
- **`ChannelOps(Transport)`** — `name = "telegram"`. Its tier-specific methods
  (`send_free`/`send_paid`/`send_*_image`) remain the Telegram-native API and
  are still what the 12 existing call sites use; `send_text`/`send_image`
  dispatch to them and add no behavior.
- **`TransportFan(Transport)`** — drop-in for a single transport; fans to N and
  **counts attempted / delivered / failed per platform**, logging any
  non-delivery. Both 2026-08-05 signal bugs had the signature "content went
  nowhere and nothing said so"; every added platform multiplies that surface,
  so absence of output must produce evidence of itself. Exceptions in one
  transport are contained and counted, never kill the fan.

**Verified no behavior change**: a golden-output harness rendered all 40 event
types through router + both formatters; pre- and post-refactor digests are
identical (`093bd7d1…`). Note the cards embed a live wall-clock timestamp
("Fleet Pulse HH:MM UTC"), so any such harness MUST normalize it or the digest
changes every minute regardless of code.

**Stage 2 (done 2026-08-05): `XTransport`.** Implements `Transport` for X API
v2. OAuth 1.0a HMAC-SHA1 signing is **stdlib-only** (verified against the RFC
5849 reference vector) — no tweepy/requests-oauthlib dependency.

- **Opt-in and off by default.** Activates only when `x_enabled` is true;
  otherwise `self._channel` is the bare `ChannelOps` and the path is
  byte-identical to before. Config keys: `x_enabled`, `x_consumer_key`,
  `x_consumer_secret`, `x_access_token`, `x_access_secret`, `x_post_paid`.
- **Markup modes**: `CardFormatter.to_plain()` strips Telegram HTML and
  box-drawing rules; `.fit(text, limit)` trims on a line boundary. Applied at
  the two format entry points rather than forking 34 tag sites. Measured on
  real cards: 310–312 chars → **151–153**, inside X's 280.
- **Tier mapping**: X has no free/paid split. `free` posts; `paid` is skipped
  unless `x_post_paid=true` (posting paid content publicly gives it away).
  **`x_post_paid` is TRUE here (set 2026-08-05 by operator decision)** — every
  paid-tier card is mirrored to the public X timeline. Coherent while the paid
  Telegram channel does not resolve and trade cards already route free; revisit
  if a real paid tier is ever stood up, or paid subscribers get nothing X
  followers don't.
- **Unconfigured is loud**: missing credentials log a warning and count as
  `failed`, never a silent skip.
- **Images not yet supported** — v1.1 multipart upload needs its own signing
  pass. `send_image` posts the text fallback and says so rather than
  pretending an image went out.

**`TransportFan` mirrors the Telegram-named API** (`send_free`, `send_paid`,
`send_*_image`, `send_personal`) on top of the neutral protocol. Without this
the 12 tier-named call sites break the moment a second transport is enabled —
the fan must be a true drop-in for `ChannelOps`.

**Untested against the live X API**: no credentials exist on this machine, so
signing, rate limits, and error shapes are unverified end to end. Supply
credentials and post one card before trusting it.

Remaining: per-platform rate-limit/backoff — the 429 retry and `_daily_stats`
are still Telegram-shaped.

**Superseded — original Stage 2 estimate:** Cards are 312–315 chars as
sent; stripping the Telegram HTML and box-drawing borders brings the same
content to ~153 chars, inside X's 280 limit. PNG cards are ~16KB against a 5MB
limit. Remaining work: a `markup` mode on `CardFormatter` (36 hardcoded
`<code>`/`<b>` sites) and per-platform rate-limit/backoff state — the 429 retry
and `_daily_stats` are currently Telegram-shaped.

### Telegram Signal Mix (fixed 2026-08-05)
Two independent bugs made the subscriber feed repetitive — 93% of a week's
sends were REGIME_CHANGE at a suspiciously regular ~61-minute cadence.

**1. REGIME_CHANGE shared one dedup bucket.** `_FLEET_DEDUP_TYPES` collapsed the
key to the bare type, so all ten regime-emitting bots competed for one slot and
`dedup_window_s=3600` released exactly one card per hour — a clock, not a signal.
Worse, the bots have unrelated vocabularies (trinity `TRENDING/RANGING` per pair,
nexus `SYSTEMIC/MIXED`, contrarian `NEUTRAL/FEAR`, chronos
`session_overlap/high_activity`) and **60.9% of transitions were pure A→B→A
flip-flops** (trinity: 635 of 765). The code comment claimed "only fires when
AEGIS actually flips" — there was no such check.

Now: **AEGIS only** (the fleet risk authority), no-op changes (`from == to`)
suppressed, source-scoped dedup key, plus **hysteresis** in `AlertGate`
(`check_hysteresis`, `regime_hysteresis_window_s`, default 1800s) that swallows a
reversal of the previous transition. Replay of 1,633 real bus events: 1,633 → 37
sends; on recent data ≈2 cards/day.

**TEMPORARY (2026-08-05): TRADE_OPEN/TRADE_CLOSE route `free=True`.** They were
paid-only and therefore going nowhere (see below). Free bodies already redact
entry/bot behind "🔒 Full details for subscribers", so the tier distinction
survives; `delay_free_s=0` because a 4h-delayed trade signal is worthless.
**Revert to `free=False` once `telegram_paid_chat_id` is set and verified with
`getChat`** — this currently makes paid-tier content free. The supplied ID
`-1002891043721` returns `Bad Request: chat not found` (token is valid and the
free channel resolves fine), so it was NOT written to config.

**2. `telegram_paid_chat_id` is empty — all paid cards silently dropped.**
`ChannelOps.send_paid` returns `False` immediately when the paid chat is unset
(`if not self._paid_chat: return False`) with **no log line**. 745 paid sends
succeeded through 2026-04-11; every attempt after failed, and on 2026-07-28 alone
156 were dropped — including **all 58 TRADE_OPEN and 6 TRADE_CLOSE**. Trade
events still reach the bus and still route correctly (`paid=True, free=False`);
they die at the channel. **This is unfixed — it needs a real paid chat ID in
`signal_config.json`.** Until then the fleet's most valuable cards go nowhere.

Never let a send path fail silently: an unset channel should log a warning, not
return False quietly.

### Broadcaster counters: the day boundary is UTC
`ChannelOps._daily_stats` (`free_sent_today` / `paid_sent_today` /
`failed_today`) resets on a **UTC** day boundary — `_reset_if_new_day()` uses
`datetime.now(timezone.utc)`, not the bare `datetime.now()` whose local default
made `%(asctime)s` local and broke a scheduler verification on 2026-08-05.
Verified, not inferred: at 02:00Z the UTC date has rolled while Mountain local
has not, and the code reads the UTC one.

This matters beyond bookkeeping. The counters are a **pull** instrument — the
dashboard KPI (`command_center_v4.html:12402`) reports only when someone opens
the page, and the numbers wipe at that boundary. So **UTC midnight is the
deadline for noticing a day's drops**, and the same boundary re-arms the
once-per-day `PAID CHANNEL UNSET` warning. Any push-based alerting built on
these counters must fire before it.

### Subscriber Card Bot Count (fixed 2026-08-05)
`card_renderer.py` footers print the live fleet size. Until 2026-08-05 both
footers (`_draw_footer`, `_eod_footer`) hardcoded the literal `"19 bots · live"`
— every subscriber card asserted 19 live bots regardless of actual state, and
19 is unreachable anyway (`bot_responder` has no port, so CC can only ever poll
18). Direct violation of "No fake stats — ever".

Now: `CardRenderer.set_bots_alive(n)` feeds `aggregate.bots_alive` from
`/api/master`; `_bot_count_text()` returns `None` when the count is unknown and
**the footer omits the line entirely** rather than showing a stale or invented
number. `signal_broadcaster.py` sets it per-card from the already-enriched
`edata["bots_alive"]`. Non-int/negative input is treated as unknown.

Never reintroduce a literal count here — not `19`, not `18`. If the count isn't
known, print nothing.

### Normalizers
Each bot gets a normalizer function (in `command_center.py`, registered in the normalizer map ~line 1334) that translates its raw API response into a standard schema: `{equity, pnl, pnl_pct, win_rate, drawdown_pct, sharpe, open_positions, total_trades, regime, signals_count, uptime, ...}`. Win rates arrive in different scales (0-1 vs 0-100) — normalizers handle this. All bots use `/api/snapshot`; Inference and Broadcaster use `/health`. NEXUS reports `market_character` instead of `regime`.

### Thread Safety
All shared state is protected by simple `threading.Lock()` (no RLock). Three locks: `_lock` (bots/aggregate/feed), `PortfolioManager._lock` (instance-level), and `_universe_lock`. Bot polling is serial within the poll loop, so aggregate state represents a ~1s window, not an atomic moment.

### Portfolio Persistence
Portfolio state is saved via atomic writes (temp file + `os.replace()`). History is capped at 200 entries — use `logs/events/` for full audit trail. Failed validations return a reason string but are not logged to history.

### Phase 2 Dependencies
Bots marked `"phase": 2` in `fleet_config.py` require Command Center to be running. `launch_fleet.py` derives its FLEET list from `fleet_config.py` (single source of truth). It waits 15s for CC to bind port 9000 before launching Phase 2. If inference server fails, Phase 2 features degrade silently. Note: `fleet_config.json` also has a `phase` field but it is for documentation/health-monitor reference only — it is not read by `launch_fleet.py`.

### Inference Model Chain
`inference_server.py` searches for models in order: `gemma2:2b` → `phi4-mini` → `mistral-nemo` → `qwen3.5:4b` → `deepseek-r1:7b` → first available. All prompts instruct JSON-only responses (no markdown).

**Ollama is pinned to CPU (`num_gpu:0`) as of 2026-07-30.** Ollama v0.32.5's CUDA runtime crashes with `0xc0000005` on the Tesla P4 (Pascal). CPU `gemma2:2b` is verified end-to-end. Re-enable GPU only after confirming an Ollama release has fixed Pascal — the crash is silent from CC's side, it just looks like inference is down.

### Event Bus Reactions
Reaction rules from `reactions.json` spawn in separate daemon threads to avoid blocking publish. Condition expressions use a regex parser: `count(EVENT_TYPE, last_Xmin) >= N`.

### Brainiac Collection
Collector threads silently swallow network errors. If data stops flowing, check `brainiac/` folder contents — no errors will appear in the main console.

### Dashboard v4 Editing Convention
`command_center_v4.html` is ~17.4k lines of inline HTML/CSS/JS, plus two companion scripts: `solar_system.js` (COSMOS canvas layer — planets, star systems, DeepField) and `armada.js` (WebGL armada). `armada.js` is a deferred ES module with **no access to the inline `<script>` closure** — interop happens through `window.*` globals (`_orbNodes`, `_orbIsFS`, plain-object snapshots stamped for zoom/pan). three.js is vendored at repo root (not `static/`) because the static route rejects subdirectory paths; the importmap maps `"three"` to `./three.module.min.js`. When adding or upgrading bot panels:
1. Canvas elements must be injected **after** the panel HTML is in the DOM (post-inject pattern — referencing the canvas in the same template string where it's declared will fail because the element doesn't exist yet at script parse time).
2. After any edit, do a brace-balance check on the `<script>` block — unbalanced braces silently break the whole dashboard with no console error.
3. Match existing v4 conventions (color palette, panel structure, data-binding pattern) rather than inventing new ones. Copy a working panel and modify it.
4. Test by reloading the browser and checking that **all** bot panels still render — a single syntax error in one panel blacks out the whole page.

### Portfolio Reservation Leaks on Bot Restart
When a bot crashes or is restarted mid-trade, its in-flight reservations in `portfolio.json` are orphaned (the bot has no memory of them after restart). Defense: bots must call `/api/portfolio/release` for any stale reservations on startup, or the portfolio manager will leak capital until manually cleared. (Historical offender was TrekBot, fixed in milestone v3 before it left the fleet.) When wiring a new bot to the pool, verify restart hygiene. Note: the dashboard's "Stale reservations" counter flags any reservation older than 2h — long-held positions trip it legitimately; cross-check against the bot's actual open positions before treating it as a leak.

### Phantom TRADE_OPENs on CC Restart (fixed 2026-07-30)
`FleetLogger` derives trade events by diffing each bot's reported positions against `_prev_positions`. Because that dict started empty on every CC start, the first poll after a restart looked like every already-open position had just been opened — CC emitted **one phantom `TRADE_OPEN` per open position on every restart**. Five landed on 2026-07-30, each correlated with a restart visible in the bus as a ~30s silence gap. The fix seeds `_prev_positions` silently on first observation of each bot, so the first poll establishes a baseline instead of a diff. The five phantom rows were purged from `logs/events` + `logs/event_bus` (backups in `logs/purged_phantoms_backup/`).

Two consequences worth knowing: a silence gap in the event bus of roughly 30s is the signature of a CC restart, and `LOGGER_ERROR` is now emitted when a logger tick is skipped rather than failing silently.

### Stale Reservation Sweeps Must Be Position-Aware (fixed 2026-07-30)
The stale-reservation sweep was age-only at a 48h threshold, so it released Confluence's APE/USD and MANA/USD reservations out from under positions that had legitimately been held 66h. `force_release_stale()` now takes `active_positions=` and cross-checks `_active_position_keys` before releasing anything. **A long-held position is not a leak** — always cross-check against the bot's actual reported positions before treating an old reservation as orphaned.

Carried-forward consequence: Confluence's stored reservation ids (`d5eu`, `3xve`) are dead after the manual re-reservation; the live ids are `xgdr` and `d9ve`. Confluence's close-time releases will fail silently against the dead ids, and the fixed sweeper will reap the new ids roughly 48h after those positions close.

### Watchdogs, Threaded Servers, and Silent Kills (learned 2026-07-28)
- **Two watchdogs restart bots**: CC's `_health_monitor` thread AND `launch_fleet.py`'s monitor loop. A bot process that exits with **code 1 and no traceback was taskkilled by a watchdog**, not crashed — check both watchdogs' logs before hunting a Python bug.
- **Every bot HTTP server must be threaded** (`ThreadingHTTPServer` / `ThreadingMixIn`). A plain `HTTPServer` gets its health probe blocked by browser keep-alive connections (the dashboard polls some bot ports directly) and the watchdog kills the healthy process. Fleet-wide fix in commit a2359e9; broadcaster was missed and crash-looped 18× until 5bcdeed.
- **Support services should bind their port first thing at startup** as a single-instance mutex, with `allow_reuse_address = False` — on Windows, SO_REUSEADDR lets two processes bind the same port, and simultaneous watchdog spawns can otherwise both survive and double-send (broadcaster fix in d6c5d0f).
- **CC's `/api/signals/broadcaster/stats` always serves from `signals_sent.log`** (`source: "log_fallback"` — the live proxy is disabled due to a Python 3.14 HTTP/1.0 quirk). Its counts are log-derived, not live process stats; the broadcaster's own `/stats` on port 9002 is the live source.
- **`port_guard.ensure_port` yields to healthy incumbents (hardened 2026-07-30).** It used to kill whatever held the port unconditionally — so if both watchdogs ever double-spawned a bot (the documented broadcaster failure mode), each fresh spawn would assassinate the healthy serving instance. Now: port holder answers HTTP → the duplicate exits via `sys.exit(0)` (bots' `except Exception` blocks don't catch SystemExit — this is load-bearing, don't change them to bare `except:`); port held but HTTP dead → still killed as a zombie. Oracle additionally binds its port exclusively first-thing (`allow_reuse_address = False`, the broadcaster pattern) — when adding a new bot, copy that pattern.
- **Identify fleet processes by PORT OWNER, not command-line substring (lesson 2026-07-30).** A pattern like `server.py` also matches `inference_server.py`. During an Oracle restart this misidentified the Inference server as a "duplicate Oracle" three times; each kill looked like watchdog double-spawn ping-pong and nearly led to misdiagnosing a phantom infrastructure bug. `Get-NetTCPConnection -LocalPort <port>` → `OwningProcess` is the ground truth for which process is which.

## Fleet Mode (Paper/Live Toggle)
`fleet_config.FLEET_MODE` is the single source of truth for paper vs live trading. Default: `"paper"`.

- **Paper mode** (default): all trades simulated, no Kraken orders placed even if API keys are present
- **Live mode**: real Kraken orders via raw REST (TurtleSue only — the sole bot with live execution code since TrekBot left the fleet)
- Toggle via: `POST http://localhost:9000/api/fleet/mode {"mode": "paper"|"live"}`
- Read via: `GET http://localhost:9000/api/fleet/mode` or the `fleet_mode` field in `/api/master`
- Dashboard: red pulsing LIVE button in header, header border turns red
- Bots check `fleet_config.is_live()` dynamically at trade time (no restart needed to switch modes)
- Going live requires `KRAKEN_API_KEY` set as environment variable; the API rejects live mode without it
- Gridzilla, NexusBrain, Rubberband, Arbitrageur, Confluence are paper-only (no live execution code)

## Directionality (Shorts)
Shorts were re-enabled fleet-wide in paper on 2026-07-30 (`fleet_config.FLEET_LONG_ONLY = False`) — the fleet is a signal product and subscribers in shorting-allowed jurisdictions need short signals. `LIVE_LONG_ONLY = True` still blocks shorts if the fleet is ever flipped live. The portfolio direction gate (`fleet_config.direction_allowed(direction, is_reentry)`) fires before the size floor; `is_reentry=True` lets a restarting bot re-claim capital for an already-open short.

- Short-capable: TurtleSue (symmetric turtle), Rubberband (rip-fade: downtrend + upper BB + RSI > 60), NexusBrain (mirrored confluence behind the same 0.70 gate), Confluence (per-source short aggregation, reports `strategy.type: BIDIRECTIONAL`)
- Oracle publishes actionable SHORT rows for DISTRIBUTION only; FALLING KNIFE and TAKE PROFIT are deliberately non-actionable advisories (`D:\Oracle\src\strategy.py` documents why)
- Gridzilla and Arbitrageur are long-only by design (grids and rotation don't map to directional shorts)
- Broadcaster formats ▼ SHORT natively — no changes were needed there

## Key Metrics to Watch
**FEES ARE GONE (2026-07-30, Jeremy's directive).** The fleet is a signal product — subscribers pay their own exchanges' fees, so the fleet neither models nor deducts fees anywhere. All P/L and expectancy are GROSS price movement. The quality gates that fee-survival accidentally taught us (R:R ratios, minimum-move floors, cooldowns, confluence bars) SURVIVE at the same thresholds, re-rationalized as signal-worthiness standards — do not remove them, and do not reintroduce fee math. Historical logs before 2026-07-30 are net-of-fee; after, gross — mind the discontinuity when comparing eras. (The old fee-ratio war, 650% → 32%, is preserved in git history; the fee-slayer agent's domain is retired.)
- **Expectancy** (the primary health signal): Target positive gross E[V] per trade. As of 2026-07-30: **-$31.09/trade gross, n=14, 28.6% win rate, avg win $14.63 vs avg loss -$49.38 (3.4×)**. This is a loss-size problem, not a frequency or friction problem. The sample dates only from the gross-P/L cutover — ~30+ closed gross-era trades are needed before a measurement pass means anything. Run `python expectancy.py` (a `__main__` block was added 2026-07-30) or hit `/api/expectancy` for current state; never quote the figure from memory. Note `/api/expectancy` still reports a historical `trekbot` bucket from archived logs — it is not a live bot.
- NexusBrain `min_confluence` is **0.70** (lowered from 0.80 on 2026-04-08 after 8h with zero trades; note the real cause of that drought was two broken signal components, fixed 2026-07-29 — see `score_volume_confirmation` and `score_macd_momentum`).
- **Gridzilla spacing floor:** 1.2% minimum grid spacing. Max 5 grid lines. $0.50 gross profit floor per level (signal-worthiness density floor, not fee survival).
- **Trade frequency governor:** 10-min per-pair cooldown in portfolio manager after any trade closes. Adaptive: 5 min when AEGIS score > 0.7. Gridzilla exempt (fee gate handles its frequency).
- **AEGIS score:** Controls deployment limit. Low score → DEFENSIVE → reduced max deployment.
- **Concentration limit:** Max 40% of deployed capital in any single pair.

## Working Rules
- NEVER overwrite working code based on assumptions
- ALWAYS read existing code before modifying
- No fake stats — ever
- A module is only "done" when: (1) import appears in the running bot file, (2) API endpoint responds, (3) event bus shows expected event type
- **Prefer specialized agents over general-purpose.** Six domain agents are available (fleet-wire-master, fleet-auditor, nexus-council-physicist, simons-fleet-philosopher, cosmos-dashboard-alchemist, cosmos-soundtrack-maestro; fee-slayer retired 2026-07-30 to `agents_retired/` when fees were removed). Each owns a specific slice of the fleet and has internalized the conventions for that slice. The alchemist owns COSMOS visuals; the maestro owns the `_sound` system and `audio/` library — never let both edit `command_center_v4.html` concurrently. Use `general-purpose` only when nothing else fits.
- **Event bus is the ground truth for wiring verification, not file grep.** A file existing or even an import line is not proof that an engine is firing. The only definitive check is seeing the expected event type land on `/api/events/recent`.
