# GoldenEye

Standalone live crypto trading bot for Kraken with a self-learning brain, a mission-control dashboard, and a post-trade autopsy journal. Paper-live hybrid: runs on real Kraken balances with small position sizes and full risk controls.

Not a fleet component, not a signal broadcaster — GoldenEye trades its own account end-to-end: streams prices, computes signals, fires factor-scored entries through a 15-gate chain, manages positions with breakeven-stop + trailing, and writes a plain-English autopsy to disk every time a trade closes.

## What it does

- **Streams 20 Kraken USD pairs** (auto-ranked every 2h by movement score)
- **27 long signals** (a–z, 2) compressed into **6 continuous factors** (trend, momentum, structure, volume, volatility, order_flow)
- **Regime-adaptive**: HMM 4-state classifier (bull/bear/range/chop) drives per-regime confidence thresholds, TP distances, and the factor-weighted confluence gate
- **Fleet-wide Bayesian brain**: one model learns across all 20 symbols and all regimes, not per-symbol
- **Post-trade autopsy**: writes a narrative to `output/autopsy.jsonl` after every close, including similar-setup statistics from fleet memory
- **Regime sanity check**: cross-validates HMM labels against actual 24h price returns every 5 min, flags drift
- **Mission-control dashboard** at `http://localhost:18065` — live equity curve, command panel, decision stream, global brain panel

## Running the bot

```bash
# Standard run (long mode, paper balance synced to live Kraken)
python goldeneye.py --output-dir output --mode long

# Short mode
python goldeneye.py --output-dir output --mode short

# Historical backtest (180 days, canonical 20 pairs)
python bt_harness.py

# Parameter sweep
python bt_tune.py

# Regime-specific backtest
python bt_tune_regime.py
```

Actual launcher is `D:\Desktop\launch.bat` (authoritative for live operation). Local `launch.bat` exists for reference.

## Architecture

**One monolith**: `goldeneye.py` (~6600 lines). Everything runs in one process with ~20 daemon threads:

- **Per-symbol stream loop** (`stream()`): 2s polling on `hot` tier symbols, 15s on `watch` tier. Each tick: fetch price → update DataFrame → compute indicators → fire signals → classify regime → compute factors → run gate chain → execute or skip
- **Universe rescan**: every 2h, re-ranks 20 pairs by volatility × spread
- **Periodic reconciler**: every 5 min, reconciles local position state against Kraken orders (catches SL/TP fills missed by WebSocket)
- **Health snapshot updater**: 1s refresh, feeds `/health` without inline compute
- **Balance refresh**: 60s, syncs live Kraken balance into the paper ledger
- **Regime sanity loop**: 5 min, writes `output/regime_sanity.json`
- **Equity logger**: 60s, appends to `output/equity_curve.json`
- **HTTP server**: port 18095, serves 20+ endpoints
- **Flask dashboard**: port 18065, imports `dashboard.py` as a module

## The learning layer (added 2026-04-22)

Three subsystems that make the bot introspect its own trading:

### Autopsy journal — `output/autopsy.jsonl`
Every trade close triggers `record_autopsy()` which writes a JSON line with:
- The closed trade (symbol, R, pnl, exit reason, factors, signals, regime, duration)
- Similarity query against prior closed trades (factor-vector Euclidean distance, k=10, max_distance=0.6)
- A plain-English `narrative` field combining outcome, entry quality, weak factors, and historical peer performance

Log emits e.g. `AUTOPSY GUN long -1.02R: Full stop -1.02R via SL after 6.1h. Entry confidence was very low (0.25). Weak factors at entry: volume. Fired on signals ['f', 'p'] in bull regime. Similar historical setups: 5 found, 1W/4L (20% WR, avg -0.60R).`

### Regime sanity check — `output/regime_sanity.json`
Every 5 min, for each symbol: compute actual 24-bar return and compare against the HMM's assigned label. Mismatches logged:
- `bull` labeled but 24h return < −2% → flagged
- `bear` labeled but 24h return > +2% → flagged
- `range`/`chop` labeled but |24h return| > 5% → flagged

Summary bucket: `healthy` (<10%), `yellow` (<30%), `RED` (≥30% mismatched). RED emits a `REGIME SANITY RED` warning to the log.

### Fleet memory query
Every entry decision is enriched with `memory = {n, wins, losses, win_rate, avg_r}` computed by `find_similar_trades()` against the closed trade log. Surfaces in the Decision Stream panel as a colored WR chip per row and as a "Brain says go/no/mixed" line in the Nearest Candidate card.

## Ports and endpoints

| Port | Service |
|------|---------|
| 18095 | Bot HTTP API (health, analytics, positions, funnel, brain, ohlc, autopsy, regime, memory) |
| 18065 | Flask dashboard (mission control UI) |

### Bot API (port 18095)

**Core state:**
- `GET /health` — status, uptime, threads, balance, positions, heat, circuit breaker state
- `GET /positions` — open positions with entry/SL/TPs/P&L/R-multiple
- `GET /symbols` — all 20 tracked pairs with price, regime, WR, historical P&L
- `GET /analytics` — lifetime trade stats (trades, WR, avg R, P&L, DD) + per-signal breakdown
- `GET /decisions` — last N entry decisions with factors, gates_passed/failed, memory
- `GET /api/funnel` — 60s rolling gate funnel (per-gate pass/fail counts)
- `GET /logs` — last 8 TUI log lines

**Learning layer:**
- `GET /api/brain` — per-symbol Bayesian stats + fleet signal quality
- `GET /api/brain/global` — fleet Bayesian factor model (mu coefficients per regime, arming countdown)
- `GET /api/autopsy/recent?limit=N` — tail of `autopsy.jsonl`
- `GET /api/regime/sanity` — latest HMM-vs-actual-return sanity snapshot
- `GET /api/memory/similar?trend=&momentum=&volume=&structure=&volatility=&order_flow=&regime=&dir=&k=&max_distance=` — live factor-vector k-nearest query

**Position deep-dive:**
- `GET /api/ohlc/<sym>?limit=N` — last N candles (1h bars) + full position overlay (entry, SL, TPs, unrealized P&L, R-multiple, regime, signals)
- `GET /position/<sym>` — richer position payload (TP progress, R-mult, time clocks)
- `GET /symbol/<sym>` — indicators + signals + regime + direction history
- `GET /signal/<letter>` — per-signal WR across symbols
- `GET /trade/<idx>` — individual closed-trade detail with factor snapshot

**Broadcaster-compatible:**
- `GET /api/trades` — today's trades (for EOD card)
- `GET /api/expectancy` — fleet expectancy summary

## Dashboard (port 18065)

Single-screen mission control at `http://localhost:18065`, polled every 2s (fast tier), 5s (positions/decisions), 15s (analytics/equity/brain).

**Top bar**: Kraken status · balance · trade size · open positions · thread health · mode · uptime · CPU · memory · **MDT / UTC dual clock**

**Main panels**:
- **Equity Curve** — live-ticking line with trade markers, extends to "now" with balance + unrealized P&L
- **COMMAND** — when position open: mini chart with entry/SL/TP lines, risk bar (SL/IN/TP1/TP2/TP3), P&L strip, thesis, trajectory. When empty: NEAREST CANDIDATE card with factors + "Brain says go/no/mixed" memory readout, NEXT IN QUEUE (deduped), FLEET STATE (regime mix, avg whale, bottleneck, time since last entry)
- **Conviction · Analytics** — score gauge with historical-WR fallback (never flatlines), trades/WR/avg R/P&L/DD/heat stats
- **Signal Intelligence** — top-5 decisions showing gate pass/fail chain
- **Decision Stream** — live narrative feed: `MON L [z,2] blocked @ confluence:0.38 ... T64 M79 V61 S25 F49 W38 C44 BUL [M 4W/1L 80%]` with color-coded verdict chip (ENTE/BLOC/SHAD/OVR) and fleet-memory WR chip
- **Symbol Grid** — 20-cell grid with price, direction, regime, WR, active-signal glow
- **Live Log** — last 9 events (tight JetBrains Mono, ~10px)
- **Global Brain** — fleet Bayesian state: regime tiles (bull/bear/range/chop), override status (STANDBY/ACTIVE), arming countdown

## Key constants

| Setting | Value | Location |
|---|---|---|
| Fee | 0.40% | `get_fee()` — Kraken taker |
| Max heat | $30 | `_MAX_HEAT` |
| Max positions | 20 | `_MAX_OPEN_POS` |
| Circuit breaker daily | −$15 | `update_circuit_breaker()` |
| Circuit breaker weekly | −$35 | `update_circuit_breaker()` |
| Kill switch | 20% drawdown, **manual reset** | delete `output/kill_switch.json` |
| Bayes-override threshold | `regime_n ≥ 30 AND P(win) ≥ 0.58` | `_get_global_bayes()` |

### Regime-adaptive TP multipliers (long mode)

| Regime | TP1 | TP2 | TP3 |
|---|---|---|---|
| bull | 0.75R | 1.50R | 2.50R |
| bear | 0.50R | 0.75R | 1.25R |
| range | 0.50R | 0.75R | 1.25R |
| chop | 0.50R | 0.75R | 1.00R |

TP1 is load-bearing: hitting it triggers breakeven-stop move.

### Regime confidence floors (long mode)

| Regime | Min confidence | Rationale |
|---|---|---|
| bull | 0.40 | trending — standard bar |
| bear | 0.55 | counter-trend needs stronger conviction |
| range | 0.55 | demand clear structure/momentum |
| chop | 0.65 | only very strong setups survive chop |

## Entry gate chain (top-to-bottom)

1. **correlation** — max 0.5 returns-correlation with open positions
2. **factor_floors** — trend ≥ 0.25, momentum ≥ 0.20, volume ≥ 0.15
3. **confluence** — weighted sum of active signals vs regime-specific min; Bayes-override can bypass when fleet regime_n ≥ 30 and P(win) ≥ 0.58
4. **dir_wr** — directional WR ≥ 20% over last 10 trades (tail-cut)
5. **regime_mult** — regime probability-weighted multiplier
6. **whale** — whale_score ≥ 30 (or SHADOW position logged for learning)
7. **ai_conf** — stub (ensemble disabled)
8. **max_positions** — `< _MAX_OPEN_POS`
9. **drawdown** — not in 0x tier
10. **sentiment** — placeholder
11. **atr_fees** — TP1 distance must cover 2× fee
12. **min_size** — Kraken min × price must fit in 10% balance cap
13. **notional** — above exchange minimum cost
14. **duplicate** — per-symbol entry lock
15. **entry** — final confirmation, decision logged with memory enrichment

## State files in `output/`

- `metrics.json` — cumulative trade history (r_history, trade_log, pnl_history, by_direction)
- `equity_curve.json` — HWM + equity timeline (ts, bal, upnl, pos, spnl, hwm)
- `global_brain.json` — fleet Bayesian factor model (mu, precision, n per regime)
- `autopsy.jsonl` — post-trade autopsies (one JSON per line)
- `regime_sanity.json` — latest HMM-vs-actual-return sanity snapshot
- `circuit_breaker_state.json` — daily/weekly P&L accumulators
- `kill_switch.json` — presence = kill switch active; delete to re-arm
- `deviation_cooldowns.json` — 24h cooldown timestamps
- `<SYM>_USD/position.json` — open position, atomic write via `.tmp` + `os.replace`
- `<SYM>_USD/brain.json` — per-symbol AdaptiveBrain stats + Bayesian posterior

## Subscribers / Telegram

`subscribers.json` defines named subscriber groups (`fleet_intelligence`, `fleet_pulse`). Bot token read from `${GOLDENEYE_TELEGRAM_TOKEN}`. `card_renderer.py` renders signal cards (POSITION OPENED, POSITION CLOSED win/loss, WHALE ALERT, END OF DAY trades/flat) at 2400px → downsampled to 1200px via Pillow + Windows system fonts.

## Backtest harness

`bt_harness.py` imports signal logic directly from `goldeneye.py`, runs against 20 canonical Kraken pairs over 180 days. Returns R-based results, not dollar P&L. Sets dummy env keys before import to bypass Kraken auth.

`bt_tune.py` sweeps parameters. `bt_tune_regime.py` does regime-specific tuning.

## Project files

```
goldeneye.py              Main bot (~6600 lines)
dashboard.py              Flask mission-control dashboard (~5100 lines)
backtest.py               Standalone backtester (~2050 lines)
card_renderer.py          Telegram signal card renderer
analyze_confidence.py     Factor-confidence post-hoc analyzer
whale_indicators.py       Whale score computation
benchmark_goldeneye.py    Live performance benchmark
config.yaml               Bot configuration
subscribers.json          Telegram subscriber groups
launch.bat                Local launcher (reference)
launch_desktop.bat        Actual launcher (from Desktop)
output/                   Runtime state (metrics, equity, brain, autopsy, per-symbol)
archive/                  Quarantined orphan files with trace logs
```

## Configuration

`config.yaml`:

```yaml
poll_interval_seconds: 2.0
reconciliation_interval_seconds: 300
order_monitor_interval_seconds: 30
# symbols omitted — auto-discovered from Kraken top-20 by movement score
```

Environment:
- `GOLDENEYE_TELEGRAM_TOKEN` — bot token for Telegram signal cards (optional)
- `KRAKEN_API_KEY`, `KRAKEN_SECRET` — live trading (paper mode runs without)
- `DASHBOARD_PORT` — override default 18065
- `DASHBOARD_PORT_SHORT` — short-mode dashboard port

## Disabled signals (do not re-enable without backtest)

- `h` (BB %B recovery, 37.5% WR)
- `j` (OBV bull divergence, −0.39R)
- `k` (BB squeeze, sub-27% WR)

Commented out in `_sigs_long` with reason inline. Confirm with `bt_tune_regime.py` before flipping.

## Operational notes

- **Kill switch manual reset**: stop bot → `rm output/kill_switch.json` → restart
- **Per-signal suppression**: Bayesian blend auto-suppresses signals with blended WR < 40% (20 prior count for shrinkage)
- **Adaptive brain**: `AdaptiveBrain` tracks per-signal win rates per symbol, blends with global prior for weight adjustment
- **Tiered polling**: `hot` (2s full pipeline) / `watch` (15s ticker + signals only); promotion threshold lifts `watch` to `hot` on signal activity
- **AI ensemble disabled**: `is_ai_enabled()` returns False; infrastructure kept for future re-enable; LLM Brier score tracking dormant

## License

Private. Not for redistribution.
