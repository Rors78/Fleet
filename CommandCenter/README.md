# Command Center

Unified mission control for a 17-bot crypto trading fleet. Polls each bot's API, normalizes metrics, manages a shared $10,000 capital pool, runs an event bus for real-time inter-bot communication, and serves a single-page diagnostic dashboard on port 9000.

## Quick Start

```bash
# Launch entire fleet + Command Center
cd D:\CommandCenter && python launch_fleet.py

# Or just Command Center (bots started separately)
python command_center.py
```

Dashboard: **http://localhost:9000**

## Fleet

| Bot | Port | Role | Description |
|-----|------|------|-------------|
| TurtleSue | 8070 | Trader | Channel breakout, pool-funded |
| Sentinel | 8071 | Intel | Multi-timeframe forecast |
| Trinity | 8072 | Intel | Scanner, signal waterfall |
| HiveMind | 8073 | Intel | Ensemble optimizer |
| NexusBrain | 8074 | Trader | Confluence signals, pool-funded |
| TrekBot | 8080 | Trader | GoldenEye 27-signal adaptive system |
| TrekBot SHORT | 8087 | Trader | Inverse mode of TrekBot |
| Oracle | 8075 | Intel | Quant bridge, F&G gauge |
| Deep Blue | 8076 | Intel | Whale detection across 20 pairs |
| Gridzilla | 8077 | Trader | Grid trading with fee gates |
| PHITEX | 8078 | Intel | Thermodynamic market model |
| AEGIS | 8079 | Meta | Fleet self-assessment, deployment gating |
| NEXUS | 8082 | Intel | 14-engine mathematical council |
| Rubberband | 8083 | Trader | Mean-reversion, Bollinger-based |
| Contrarian | 8084 | Intel | Fear & Greed sentiment |
| Arbitrageur | 8085 | Trader | Statistical arbitrage, z-score spreads |
| Chronos | 8086 | Intel | Temporal/session analysis |
| Inference | 9001 | AI | Ollama proxy (Tesla P4) |
| **Command Center** | **9000** | **Aggregator** | **This repo** |

7 trading bots share the central portfolio. AEGIS score controls deployment limits (DEFENSIVE/CAUTIOUS/NORMAL/AGGRESSIVE).

## Dashboard

Single-page app (`command_center_v4.html`, ~18K lines) with:

- **Overview tab** — 12-panel fleet diagnostic: AEGIS gauge, bot scoreboard with expectancy, active positions with closed-trade fallback, regime source disagreement, Gridzilla pair scan, whale intel heatmap, 14-engine council status, signal quality with market microstructure, event stream with type distribution, fee analysis with trend, sentiment with funding rates
- **16 bot detail tabs** — each with 4x2 diagnostic grid tailored to the bot's function. Every panel wired to live API endpoints. Trade history survives bot restarts via persistent event logs.
- **Solar system visualization** — orbital layout showing bot relationships and health

## Architecture

```
command_center.py    Backend: polling, normalizers, portfolio, HTTP server, event bus
command_center_v4.html   Dashboard: single-file, all CSS/JS inline
fleet_config.py      Bot ports/paths (Python, single source of truth)
fleet_config.json    Bot registry (read by health monitor for auto-restart)
event_bus.py         In-process pub/sub with SSE broadcast
event_publisher.py   Fire-and-forget client for bots to push events
bus_listener.py      Client for bots to consume fleet intelligence
portfolio_client.py  Stdlib-only client for capital reservation
expectancy.py        Per-bot and fleet-wide expectancy tracking
collector.py         Brainiac: 5 threads collecting market data
fleet_logger.py      Snapshots, events, daily summaries to logs/
```

## API

**GET endpoints (port 9000):**
- `/api/master` — full fleet state
- `/api/portfolio` — pool state, reservations, cooldowns
- `/api/trades?bot=trekbot&limit=50` — persistent trade history from event logs
- `/api/expectancy` — fleet-wide and per-bot expectancy stats
- `/api/events/recent?n=50&type=TRADE_OPEN` — event bus catch-up
- `/api/events/stream` — SSE real-time stream
- `/api/bot/<id>` — raw passthrough to individual bot
- `/api/market/ohlc?pair=BTC/USD&interval=60` — OHLC proxy
- `/api/brainiac/{depth|trades|correlations|funding|metrics}` — market data

**POST endpoints:**
- `/api/portfolio/reserve` — request capital
- `/api/portfolio/release` — return capital with P&L
- `/api/events/publish` — push event to bus

## Analytics Engines

14 standalone modules wired into NEXUS for mathematical market analysis:

| Engine | What it detects |
|--------|----------------|
| Newton | Gravitational forces between price levels |
| Euclid | Support/resistance distance geometry |
| Einstein | Energy states, breakout potential |
| Schwarzschild | Price trapping near S/R (event horizons) |
| Quantum | Regime superposition and collapse |
| Shannon | Information theory — signal vs noise ratio |
| Lorenz | Strange attractors, Lyapunov predictability |
| Boltzmann | Order book thermodynamics (temperature, phase) |
| Prigogine | Dissipative structures, regime birth detection |
| Thom | Catastrophe theory, fold/cusp/swallowtail transitions |
| Fisher (Info Geometry) | Distribution shape change rate |
| Topology | Persistent homology, structural market features |
| Causal Flow | Granger causality between bots and pairs |
| Pythagorean | Harmonic consonance across the fleet |

## Key Metrics

- **Expectancy**: -$1.45/trade (110 trades). Fee ratio: 212%. Target: sub-100%.
- **Portfolio**: $10K pool, 30% max deployment in DEFENSIVE, 60% in CAUTIOUS
- **Trade gates**: $30 min trade size, 10-min per-pair cooldown, AEGIS regime gate
- **Auto-restart**: Health monitor detects dead bots, restarts from fleet_config.json

## Dependencies

- `requests` (only external dependency for Command Center)
- Bots use stdlib `urllib` only for fleet communication
