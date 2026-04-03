# Oracle — Autonomous Crypto Trading Fleet

> 16-bot autonomous trading fleet with cinematic solar system dashboard, mathematical council engines, and Jim Simons-inspired measurement layer. Live on Kraken with $10K deployed capital.

---

## Overview

Oracle is a self-coordinating fleet of 16 specialized trading bots operating on Kraken. Each bot runs its own strategy — turtle trading, whale detection, adaptive grids, statistical arbitrage, phase transitions, mean reversion — and shares a central capital pool, event bus, and real-time intelligence layer coordinated by Command Center.

The fleet is not a collection of independent bots. It is a system. Bots publish intelligence to a shared event bus. A central portfolio manager allocates capital. AEGIS monitors fleet health and throttles exposure. A 14-engine mathematical council in Nexus runs continuous regime analysis across information geometry, topology, quantum state superposition, Granger causality, thermodynamics, strange attractors, and catastrophe theory. A Jim Simons-inspired measurement layer tracks expectancy, signal attribution, and fee-adjusted returns on every trade.

The dashboard — COSMOS v5 — renders the fleet as a living solar system on a 50-inch display. Planets orbit stars. Moons orbit planets. The sun pulses with fleet health. Sound responds to market events in real time.

---

## Architecture

```
+-------------------------------------------------------------+
|                    COMMAND CENTER :9000                     |
|  Event Bus (SSE) . Portfolio Manager . Market Data Cache   |
|  Jim Simons Measurement Layer . COSMOS v5 Dashboard        |
|  AI Inference :9001 (gemma2:2b on Tesla P4 via Ollama)     |
+---------------------------+--------------------------------+
                            | SSE pub/sub . REST APIs
         +------------------+------------------+
         |                  |                  |
   SIGNAL SYSTEM       FLOW SYSTEM        MATH SYSTEM
   (Oracle star)    (Deep Blue star)    (Nexus star)
   TrekBot           Arbitrageur         NexusBrain
   Gridzilla         Contrarian          Rubberband
   TurtleSue                             PHITEX
   Trinity

   CC Moons: AEGIS . Sentinel . Chronos . HiveMind
```

---

## Bot Roster

| Port | Bot | Strategy | Status |
|------|-----|----------|--------|
| 8070 | **TurtleSue** | Turtle Trading — patient trend following | ALIVE |
| 8071 | **Sentinel** | Probabilistic regime forecasting | ALIVE |
| 8072 | **Trinity** | Multi-timeframe scanner, 5 signals | ALIVE |
| 8073 | **HiveMind** | Differential evolution portfolio optimizer | ALIVE |
| 8074 | **NexusBrain** | Multi-timeframe confluence, 50 signal pairs | ALIVE |
| 8075 | **Oracle** | 93-pair quantitative scanner | ALIVE |
| 8076 | **Deep Blue** | Whale detection, order flow analysis | ALIVE |
| 8077 | **Gridzilla** | Adaptive intelligent grid trading v2 | ALIVE |
| 8078 | **PHITEX** | Phase transition detection, thermodynamic model | ALIVE |
| 8079 | **AEGIS** | Fleet self-assessment, health monitoring, capital throttle | ALIVE |
| 8080 | **TrekBot** | 27-signal adaptive trading — fleet's best performer | ALIVE |
| 8082 | **Nexus** | 14-engine mathematical council | ALIVE |
| 8083 | **Rubberband** | Mean reversion | ALIVE |
| 8084 | **Contrarian** | Sentiment extreme detection | ALIVE |
| 8085 | **Arbitrageur** | Statistical arbitrage | ALIVE |
| 8086 | **Chronos** | Session and time-based strategy | ALIVE |

---

## Mathematical Council

Nexus runs 14 mathematical engines continuously across all pairs, publishing regime intelligence to the event bus every 15 seconds.

| Engine | Framework | Bus Event | What It Detects |
|--------|-----------|-----------|-----------------|
| Information Geometry | Fisher information metric | MANIFOLD_WARNING | Distribution morphing before regime change |
| Topology | Persistent homology, Takens embedding | CYCLE_DETECTED | Market cycles from reconstructed phase space |
| Quantum State | Regime superposition with interference | QUANTUM_COLLAPSE | Regime collapse when entropy < 0.30 |
| Causal Flow | Granger causality | CAUSAL_GRAPH_UPDATE | Which signals causally precede price moves |
| Shannon Information | Mutual information, transfer entropy | ENTROPY_SIGNAL | Information flow between market variables |
| Boltzmann Order Book | Order book thermodynamics | THERMAL_REGIME | Phase transitions in liquidity structure |
| Lorenz Attractor | Strange attractors, Lyapunov exponent | CHAOS_WARNING | Chaos vs. order regime classification |
| Thom Catastrophe | Catastrophe theory early warning | BIFURCATION_ALERT | Pre-bifurcation structural warnings |
| Prigogine Entropy | Dissipative structures, entropy production | STRUCTURE_FORMING | Self-organization in price action |
| Newton | Newtonian force model | NEWTON_FORCE | Momentum, mass, velocity of price |
| Euclid | Geometric S/R levels | EUCLID_LEVEL | Support and resistance from price geometry |
| Einstein | Relativistic energy | EINSTEIN_ENERGY | Breakout force and mass |
| Schwarzschild | Gravitational event horizon | SCHWARZSCHILD_HORIZON | Price capture near S/R |
| Riemann | Manifold curvature | — | Curvature in return space |

---

## Jim Simons Measurement Layer

Named after the Renaissance Technologies founder. The fleet had 45,000 lines of code and 10 mathematical frameworks before this layer was built. It was losing money. The measurement layer found out why.

```
Fleet gross profit (before fees):   +$52
Fleet total fees paid:              -$51
Fee ratio:                          650% of gross profit
Fleet expectancy:                   -$1.50 / trade

Conclusion: strategies have edge — fees erase it entirely.
```

**Core modules:**

| Module | Endpoint | Purpose |
|--------|----------|---------|
| expectancy.py | GET /api/expectancy | Per-bot, per-pair P/L with full Kraken fee accounting |
| signal_aggregator.py | POST /api/signals/propose | Ensemble trade proposals — bots submit, council decides |
| signal_decomposition.py | GET /api/signals/decomposition | Per-signal marginal value: KEEP / CUT |
| signal_decay.py | GET /api/signals/decay | 40 signal types with measured half-lives |

**Analysis scripts:**

| Script | Purpose |
|--------|---------|
| regime_expectancy.py | Per-regime W/R, fees, expectancy net of fees |
| signal_attribution.py | Fee-adjusted per-signal expectancy, flags MISLEADING signals |
| denial_cost.py | Opportunity cost of portfolio manager denials via OHLC replay |
| ultron.py | Full fleet self-analysis |
| evolution.py | Weekly optimization recommendations |

**Key discovery — Signal i+f:**

Across 411 TrekBot trades, only one signal combination showed positive net expectancy after Kraken fees:

```
Signal i (MFI Oversold Cross, 14-period, threshold 20):

  With MACD histogram rising (signal f):    11 trades   63.6% WR   avg R +0.524   KEEP
  Without MACD histogram rising:            12 trades   33.3% WR   avg R -0.159   CUT
  Fleet baseline (all signals):            411 trades   24.9% WR   avg R -0.205
```

Signal i without MACD histogram confluence is now gated out. The 12 fee-negative trades per sample period are suppressed.

---

## COSMOS v5 Dashboard

5,524 lines. 272KB. Single HTML file. Served from Command Center on port 9000.

**19 render layers:** CosmicCanvas (600 stars, 14 nebulae, 10 dust lanes, 12 galaxies, shooting stars) → ambient particles → sun with corona and prominences → gravity zones → orbit paths → orbit trails → migration trails → constellations → wormholes → gravitational lensing → synaptic connections → comets → shockwaves → stars → planets → moons

**Planet rendering:** Illuminated spheres lit by the sun's position. Each planet has unique surface detail — Oracle has cloud bands and a Great Red Spot, Deep Blue has ocean waves and ice caps, Gridzilla renders green grid lines, PHITEX has pulsar beams and magnetic field lines, AEGIS shows hexagonal shield geometry, Chronos shows time rings with a live UTC clock hand, Nexus orbits 14 colored engine moons.

**Sound system:** Full WebAudio graph. Compressor → master → destination. Three layer buses (cosmic / planetary / events). Convolver reverb. 16 planet voices. Fleet harmony chord driven by regime. Sidechain ducking. Spatial audio. Event sounds: whale calls, trade bells, supernova, emergency klaxon.

**Planet migration:** Planets migrate between star systems based on which intelligence source is dominant. 60-second cooldown prevents ping-ponging.

**Interaction:** Mouse drag to pan, scroll to zoom, fullscreen toggle, minimap at >1.5x zoom, hover tooltips, planet click for detail view, sun click for System Manifest panel.

---

## Tech Stack

| Component | Spec |
|-----------|------|
| CPU | AMD Ryzen 9 5900XT |
| GPU | NVIDIA Tesla P4 (AI inference) |
| RAM | 32GB DDR4 |
| OS | Windows 11 |
| Python | 3.14 |
| Exchange | Kraken live API |
| AI Runtime | Ollama + gemma2:2b |
| Dashboard | Vanilla JS + HTML5 Canvas + WebAudio API |
| Event system | Server-Sent Events (SSE) |
| External deps | requests only |

---

## Launch

```bash
cd D:\CommandCenter
python launch_fleet.py
```

Phased boot — Command Center first, then bots in dependency order with health verification between phases.

**Health checks:**
```bash
curl http://localhost:8080/health          # TrekBot
curl http://localhost:9000/api/master      # Full fleet state
curl http://localhost:9000/api/expectancy  # P/L summary
```

---

## Current Status

- **Live** on Kraken, real trades, real capital
- **Pool:** $10,000
- **AEGIS:** Defensive (30% max deployment)
- **Best performer:** TrekBot (only bot with positive net expectancy after fees)
- **Measurement layer:** Active across all 6 trading bots

---

## Repository Structure

```
Oracle/
├── CommandCenter/
│   ├── command_center.py          Main server (port 9000)
│   ├── command_center_v4.html     COSMOS v5 dashboard (272KB)
│   ├── event_bus.py               SSE pub/sub
│   ├── portfolio_client.py        Capital allocation client
│   ├── launch_fleet.py            Phased fleet launcher
│   ├── expectancy.py              Trade P/L + fee tracking
│   ├── signal_aggregator.py       Ensemble proposals
│   ├── signal_decomposition.py    Signal attribution
│   ├── signal_decay.py            Event half-life tracking
│   ├── info_geometry.py           Fisher information metric
│   ├── topology.py                Persistent homology
│   ├── quantum_state.py           Market superposition
│   ├── causal_flow.py             Granger causality
│   ├── shannon.py                 Information theory
│   ├── boltzmann.py               Order book thermodynamics
│   ├── lorenz.py                  Strange attractors
│   ├── thom.py                    Catastrophe theory
│   ├── prigogine.py               Dissipative structures
│   ├── ultron.py                  Fleet self-analysis
│   ├── evolution.py               Weekly optimization
│   ├── regime_expectancy.py       Per-regime P/L analysis
│   ├── signal_attribution.py      Fee-adjusted signal analysis
│   └── denial_cost.py             Portfolio denial cost analysis
└── Gridzilla/
    └── gridzilla.py               Adaptive grid engine v2
```

---

*Built by one person with AI assistance. Not a hedge fund. A laboratory.*
