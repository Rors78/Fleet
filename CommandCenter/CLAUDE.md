# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is
Unified mission control for a 17-bot crypto trading fleet (16 original + TrekBot SHORT). Polls each bot's API, normalizes metrics, manages a shared capital pool, runs an event bus for real-time inter-bot communication, and serves a combined dashboard on port 9000. Includes AI inference (Ollama), market data collection (Brainiac), and self-evolution analysis (Ultron).

## Run
```bash
# Just Command Center (bots must be started separately)
cd D:\CommandCenter && python command_center.py

# Launch entire fleet + Command Center in one terminal
cd D:\CommandCenter && python launch_fleet.py

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
- `command_center.py` — Single-file backend (~3100 lines): bot polling, 16 normalizers (11 named functions + 5 inline lambdas), portfolio manager, HTTP server (port 9000) with route-table dispatch, universe discovery, market data proxy, event bus integration, Brainiac integration.
- `command_center_v4.html` — Current active dashboard (single-file, all CSS/JS inline). Served at `/`.
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
- `ultron.py` — Self-evolution engine: analyzes gate effectiveness, signal quality, regime stability, portfolio efficiency, bot utilization, timing patterns, bus effectiveness, and shadow trades. Configurable TrekBot log paths via constructor or `TREKBOT_DIR` env var. Feeds findings to AI for synthesis.
- `weekly_analysis.py` — Aggregates events + journals + Brainiac data + Ultron analysis into an AI-powered weekly report.
- `evolution.py` — Evolution engine: 5-step cycle (measure→analyze→propose→simulate→recommend). Reads event logs, queries live bots, identifies profitable/losing patterns, generates ranked parameter change proposals. Saves reports to `logs/evolution/`.

### Shared Libraries
- `standards.py` — Canonical regime labels (`BULL`, `BEAR`, `RANGING`, `VOLATILE`, `TRANSITIONING`), `REGIME_MAP` for normalizing any bot's regime label, `normalize_pair()` for Kraken pair formats.
- `portfolio_client.py` — Thin stdlib-only client for bots to reserve/release capital from the central pool.
- `fleet_config.py` — Python module single-source-of-truth for bot ports/paths (distinct from `fleet_config.json`). Bots and `launch_fleet.py` import this instead of hardcoding.
- `port_guard.py` — Call `ensure_port(port, bot_name)` on startup; kills zombie processes occupying the port before the HTTP server binds.
- `notifier.py` — Independent external watchdog (no fleet imports). Monitors CC health via Telegram alerts. Env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

### Advanced Analytics Engines
Standalone modules that read event logs / live bot data and return structured insights. None are imported by `command_center.py` — they run independently or are called by `evolution.py` / `weekly_analysis.py`.

**Signal quality:**
- `expectancy.py` — Tracks per-bot and fleet-wide E[V] = (WinRate × AvgWin) − (LossRate × AvgLoss) net of fees. The primary profitability signal.
- `signal_aggregator.py` — Ensemble engine: collects proposals from all bots, weights by historical accuracy, outputs a single BUY/SELL/HOLD score per pair.
- `signal_decomposition.py` — Attributes unique P/L contribution to each signal source (marginal value, accuracy, cost-adjusted expectancy).
- `signal_decay.py` — Applies empirical half-life decay to signals before consumption; tracks per-type half-lives.
- `fleet_intel_score.py` — Synthesizes all engine outputs into per-pair intelligence scores by polling NEXUS snapshot + event bus.
- `causal_flow.py` — Granger causality graph between bots, pairs, and events; answers "does X actually precede Y?" Wired in NEXUS; emits `CAUSAL_FLOW`.
- `shannon.py` — Information theory: mutual information between signals, channel capacity per bot-to-bot link, entropy of the event stream. Wired in NEXUS; emits `SHANNON_ENTROPY` (fires when fleet noise ratio > 70%).
- `denial_cost.py` — Portfolio denial opportunity cost analyzer. Reads `PORTFOLIO_RESERVE_DENIED` events, computes what denied trades would have netted. Usage: `python denial_cost.py --days 3 --hold-minutes 240`.
- `regime_expectancy.py` — Regime-conditional expectancy. Joins TrekBot factor logs with expectancy data to produce per-regime E[V] net of fees.
- `signal_attribution.py` — Fee-adjusted signal value. Attributes P/L to individual signals post-fees using `goldeneye_factors.log` + `logs/expectancy.json`.

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
- `launch_fleet.py` — Two-phase launcher. Imports `BOTS` from `fleet_config.py` (single source of truth). Phase 1: core bots. Phase 2 (after CC is up): CC-dependent bots (PHITEX, AEGIS, Inference, NEXUS, Rubberband, Contrarian, Arbitrageur, Chronos). Ctrl+C shuts down everything.
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
- Per-event daemon threads: `EventBus._check_reactions()` (spawned per published event)
- All shared state protected by `_lock` (bots/aggregate/feed), `PortfolioManager._lock` (instance-level), `_universe_lock`

## Fleet
| Bot | Port | API Route | Role |
|---|---|---|---|
| TurtleSue | 8070 | /api/snapshot | Trader (pool) |
| Sentinel | 8071 | /api/snapshot | Forecast |
| Trinity | 8072 | /api/snapshot | Scanner (intel only) |
| HiveMind | 8073 | /api/snapshot | Optimizer (intel only, slow start ~30s) |
| NexusBrain | 8074 | /api/snapshot | Trader (pool) |
| TrekBot | 8080 | /health + /positions + /analytics | Trader (pool) |
| Oracle | 8075 | /api/snapshot | Intel only |
| Deep Blue | 8076 | /api/snapshot | Intel only (whale detection) |
| Gridzilla | 8077 | /api/snapshot | Trader (pool) |
| PHITEX | 8078 | /api/snapshot | Novel (thermodynamic, Phase 2) |
| AEGIS | 8079 | /api/snapshot | Meta (self-assessment, Phase 2) |
| NEXUS | 8082 | /api/snapshot | Novel (Phase 2) |
| Rubberband | 8083 | /api/snapshot | Trader |
| Contrarian | 8084 | /api/snapshot | Trader |
| Arbitrageur | 8085 | /api/snapshot | Trader |
| TrekBot SHORT | 8087 | /health + /positions + /analytics | Trader (pool, Phase 2) |
| Chronos | 8086 | /api/snapshot | Temporal |
| **Inference** | **9001** | /api/ai/* | **AI (Ollama, Phase 2)** |
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
- `/api/bot/<id>` — raw passthrough to individual bot
- `/api/market/ohlc?pair=BTC/USD&interval=60&limit=100` — OHLC proxy
- `/api/market/ohlc/bulk?pairs=BTC/USD,ETH/USD&interval=60` — bulk OHLC
- `/api/market/ticker` — latest prices for all universe pairs
- `/api/events/stream` — SSE real-time event stream
- `/api/events/recent?n=50&type=TRADE_OPEN` — recent events (catch-up)
- `/api/events/stats` — bus statistics
- `/api/brainiac/{depth|trades|correlations|funding|metrics}` — Brainiac data

**POST endpoints:**
- `/api/portfolio/reserve` — bot requests capital `{bot_id, pair, direction, amount}`
- `/api/portfolio/release` — bot returns capital `{reservation_id, pnl}`
- `/api/events/publish` — bot pushes event `{source, type, data}`

**Inference API (Port 9001):**
- `POST /api/ai/trade-journal` — AI journal entry for a trade
- `POST /api/ai/post-mortem` — AI analysis of trade batch
- `POST /api/ai/fleet-assessment` — AI fleet state assessment
- `GET /health` — inference server status + model info

## Central Portfolio
7 trading bots (TurtleSue, NexusBrain, Gridzilla, TrekBot, TrekBot SHORT, Rubberband, Arbitrageur) share one $10,000 pool.

### Risk Limits
- Max total deployed: 80% (always keep 20% cash)
- Max per bot: 30%
- Max per pair: 20%
- Max per trade: 5%
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
- `requests` (only external dependency — all bot-side clients use stdlib urllib)

## Testing
No test suite. Validation is runtime only:
1. After wiring any module: `grep -n "from module_name" target_bot.py` must show the import in the actual bot file
2. Test the API endpoint: `curl -s http://localhost:9000/api/endpoint`
3. Check the event bus: `curl -s http://localhost:9000/api/events/recent?n=500` — the expected event type must appear
4. A file existing in `D:\CommandCenter\` is NOT done. An import line in a bot file is NOT done. Events in the bus = done.

**Never report a module as complete without all three of the above.**

## Key Patterns & Gotchas

### Normalizers
Each bot gets a normalizer function (in `command_center.py` ~line 878-1184) that translates its raw API response into a standard schema: `{equity, pnl, pnl_pct, win_rate, drawdown_pct, sharpe, open_positions, total_trades, regime, signals_count, uptime, ...}`. Win rates arrive in different scales (0-1 vs 0-100) — normalizers handle this. TrekBot is special: it uses 3 endpoints (`/health`, `/positions`, `/analytics`) instead of the single `/api/snapshot` the others use. NEXUS reports `market_character` instead of `regime`.

### Thread Safety
All shared state is protected by simple `threading.Lock()` (no RLock). Three locks: `_lock` (bots/aggregate/feed), `PortfolioManager._lock` (instance-level), and `_universe_lock`. Bot polling is serial within the poll loop, so aggregate state represents a ~1s window, not an atomic moment.

### Portfolio Persistence
Portfolio state is saved via atomic writes (temp file + `os.replace()`). History is capped at 200 entries — use `logs/events/` for full audit trail. Failed validations return a reason string but are not logged to history.

### Phase 2 Dependencies
Bots marked `"phase": 2` in `fleet_config.py` require Command Center to be running. `launch_fleet.py` derives its FLEET list from `fleet_config.py` (single source of truth). It waits 15s for CC to bind port 9000 before launching Phase 2. If inference server fails, Phase 2 features degrade silently. Note: `fleet_config.json` also has a `phase` field but it is for documentation/health-monitor reference only — it is not read by `launch_fleet.py`.

### Inference Model Chain
`inference_server.py` searches for models in order: `gemma2:2b` → `phi4-mini` → `mistral-nemo` → `qwen3.5:4b` → `deepseek-r1:7b` → first available. All prompts instruct JSON-only responses (no markdown).

### Event Bus Reactions
Reaction rules from `reactions.json` spawn in separate daemon threads to avoid blocking publish. Condition expressions use a regex parser: `count(EVENT_TYPE, last_Xmin) >= N`.

### Brainiac Collection
Collector threads silently swallow network errors. If data stops flowing, check `brainiac/` folder contents — no errors will appear in the main console.

## Key Metrics to Watch
- **Expectancy** (the primary health signal): Fleet at -$1.27/trade as of 2026-04-04 (107 trades), improving from -$1.45. Fee ratio compressing: was 650%, then 385%, now 272%. Bots have alpha but fees still erode it — target is sub-100% fee ratio.
- **Fee floor:** $30 minimum trade size enforced in portfolio manager (raised from $5 on 2026-04-04). NexusBrain min_confluence raised to 0.80.
- **Gridzilla spacing floor:** 1.2% minimum grid spacing (raised from 0.5% on 2026-04-04). Max 5 grid lines. $0.50 net profit floor per level.
- **Trade frequency governor:** 10-min per-pair cooldown in portfolio manager after any trade closes. Adaptive: 5 min when AEGIS score > 0.7. Gridzilla exempt (fee gate handles its frequency).
- **AEGIS score:** Controls deployment limit. Score ~0.008 → DEFENSIVE → 30% max deployment (2026-04-04, EXTREME_FEAR market).
- **Concentration limit:** Max 40% of deployed capital in any single pair.

## Working Rules
- NEVER overwrite working code based on assumptions
- ALWAYS read existing code before modifying
- No fake stats — ever
- A module is only "done" when: (1) import appears in the running bot file, (2) API endpoint responds, (3) event bus shows expected event type
