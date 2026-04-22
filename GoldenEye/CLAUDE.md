# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

GoldenEye is a **standalone** live crypto trading bot for Kraken. No dependency on any fleet. It publishes Telegram signal cards via `subscribers.json` and exposes its own HTTP APIs. Runs in `long` or `short` mode via `--mode`.

As of 2026-04-22, it has a **self-learning brain** composed of three cooperating subsystems: post-trade autopsy journal, regime sanity cross-check, and fleet memory query. Every entry decision carries a similarity-query readout from the closed trade log; every closed trade writes a narrative to `output/autopsy.jsonl`.

## Running the Bot

Actual launcher is `D:\Desktop\launch.bat`. Local `launch.bat` exists but the desktop one is authoritative.

```bat
# Manual run
python goldeneye.py --output-dir output --mode long

# Backtest (180 days, canonical 20 pairs)
python bt_harness.py

# Parameter sweep
python bt_tune.py

# Regime-specific backtest
python bt_tune_regime.py
```

## Ports and Endpoints

| Port | Service |
|------|---------|
| 18095 | Bot HTTP API |
| 18065 | Flask mission-control dashboard (served by `dashboard.py` as in-process thread) |

### Bot endpoints (port 18095)

**Core state:**
- `/health` — status, threads, balance, heat, CB state
- `/positions` — open positions with entry/SL/TPs/P&L/R
- `/symbols` — 20 tracked pairs with regime + WR
- `/analytics` — lifetime trade stats + per-signal breakdown
- `/decisions` — last N entry decisions (carries `memory` field for fleet memory enrichment)
- `/api/funnel` — 60s rolling gate funnel
- `/logs` — last 8 TUI log lines

**Learning layer (added 2026-04-22):**
- `/api/brain` — per-symbol Bayesian stats + fleet signal quality
- `/api/brain/global` — fleet Bayesian factor model (mu coefficients per regime, arming countdown)
- `/api/autopsy/recent?limit=N` — tail of `autopsy.jsonl`
- `/api/regime/sanity` — HMM-vs-actual-return sanity snapshot (updates every 5 min)
- `/api/memory/similar?trend=&momentum=&volume=&structure=&volatility=&order_flow=&regime=&dir=&k=&max_distance=` — live factor-vector k-nearest query

**Position deep-dive:**
- `/api/ohlc/<sym>?limit=N` — last N candles + full position overlay (entry, SL, TPs, unrealized P&L, R, regime, signals). Timestamps normalized to unix seconds at the endpoint.
- `/position/<sym>`, `/symbol/<sym>`, `/signal/<letter>`, `/trade/<idx>` — detail pages

**Broadcaster-compatible:**
- `/api/trades` (today's trades), `/api/expectancy` (fleet summary)

## Architecture

**One monolith**: `goldeneye.py` (~6600 lines). ~20 daemon threads in one process.

**Signal pipeline per symbol (inside `stream()` loop):**
1. `top_sym(n=20)` ranks Kraken USD pairs by movement score (0.75× volatility + 0.25× spread tightness), refreshed every 2h by `_universe_rescan`
2. `ind(df)` — EMA9/21/200, RSI, MACD, BB, ATR, OBV, MFI, Stoch, ADX
3. `sigs(df)` → `_sigs_long` / `_sigs_short` — 27 signals (a–z, 2); disabled signals are commented-out blocks
4. `regime(df)` — HMM 4-state classifier via `HMMRegimeDetector`
5. `compute_factors(df, ...)` — 6 continuous factor scores [0,1]: trend, momentum, volume, volatility, structure, order_flow
6. `compute_confidence(factors, rgm)` — regime-weighted factor sum + interactions → [0,1]
7. Entry gate chain (15 gates, funnel ordering: correlation → factor_floors → confluence → dir_wr → regime_mult → whale → ai_conf → max_positions → drawdown → sentiment → atr_fees → min_size → notional → duplicate → entry)
8. Decision record appended to in-memory `_decision_log` with `memory` field from `find_similar_trades()`

**Regime-adaptive TP multipliers** (`_TP_BY_REGIME`, `_TP_BY_REGIME_SHORT`): bull 0.75/1.50/2.50, bear/range 0.50/0.75/1.25, chop 0.50/0.75/1.00. TP1 is load-bearing — triggers breakeven-stop move. Don't widen without re-running `bt_tune_regime.py`.

**Regime confidence floors** (`_RGM_CONF_MIN`, long mode): bull 0.40, bear 0.55, range 0.55, chop 0.65.

**Execution**: `LiveExecutor` wraps ccxt Kraken. All private API calls serialized through `_kraken_api_lock` (RLock) to prevent nonce races. Fee hardcoded at `0.0040` (Kraken taker 0.40%).

**Risk controls:**
- `_MAX_HEAT = $30` portfolio heat cap
- `_MAX_OPEN_POS = 20` max concurrent positions
- Circuit breaker: −$15 daily / −$35 weekly, persisted to `output/circuit_breaker_state.json`
- Kill switch: 20% drawdown from peak equity — manual reset (delete `output/kill_switch.json`)
- min_size gate: Kraken min × price must fit in 10% balance cap (blocks GWEI and similar dead-zone symbols)

## Learning Layer (2026-04-22)

### Autopsy journal — `output/autopsy.jsonl`

`record_autopsy(sym, direction, r, pnl, exit_type, detail)` is called from `record_live_trade()` **before** metrics mutation so the similarity query sees only prior trades (self-reference would be tautological). Writes one JSON line per close with:
- Outcome fields (r, pnl, exit_type, regime, confidence, signals, factors, tp_hit, duration_h)
- `similar_count`, `similar_wr`, `similar_avg_r` from fleet memory query
- `narrative`: plain-English story generated by `_autopsy_narrative()`

Log also emits `AUTOPSY <sym> <dir> <R>R: <narrative>` at INFO level.

All calls wrapped in try/except — cannot block trade close.

### Regime sanity loop

`compute_regime_sanity()` runs every 5 min in a daemon thread (started after 120s warm-up for HMM fitting). For each symbol:
- Compute 24-bar return from `st.df['close']`
- Compare against `br.regime`:
  - `bull` + ret < −2% → mismatch
  - `bear` + ret > +2% → mismatch
  - `range`/`chop` + |ret| > 5% → mismatch
- Write to `output/regime_sanity.json`:
  - `checked`, `mismatched`, `details` (list of {sym, regime, ret_24h_pct, why}), `regime_counts`, `summary`
  - Summary buckets: `healthy` (<10%), `yellow` (<30%), `RED` (≥30%)

RED triggers a `REGIME SANITY RED` WARN log.

### Fleet memory query

`find_similar_trades(factors, regime, direction, k, max_distance)`:
- Factor vector: `[trend, momentum, volume, structure, volatility, order_flow]`
- Distance: Euclidean in 6D
- Default params: k=10, max_distance=0.6
- Filters optionally by regime and direction
- Returns list of `{distance, sym, r, pnl, exit, when, factors, sigs, confidence, regime, direction, tp_hit}` sorted by distance

**Wired into the decision log**: every decision with factors computes `memory = {n, wins, losses, win_rate, avg_r}` and attaches it to the decision record. Surfaces in the dashboard as per-row chip + Nearest Candidate card.

## Global Bayesian Brain

One `BayesianFactorModel` per regime (bull/bear/range/chop) instead of per-symbol. Lives at `output/global_brain.json`.

- Features: 6 factors + 3 interactions (trend×momentum, structure×volume, order_flow×momentum) + bias → 10 features
- Posterior: conjugate Bayesian linear regression (weak prior, precision = I × 0.1)
- Updates: called from `record_live_trade` with factors at entry + outcome (r > 0 → label = 1)
- **Cold-start bootstrap**: on first load, copies posterior from the per-symbol brain with highest `total_n`. Prevents starting from zero when per-symbol brains have history.
- **Override gate**: once `regime_n ≥ 30`, a candidate that fails confluence can still enter if `P(win) ≥ 0.58`. Logs `BAYES-OVERRIDE` when it fires.

Current state: bull regime_n=19 (11 trades from arming); bear/range/chop still at 0.

## Per-Symbol AdaptiveBrain

Each symbol has an `AdaptiveBrain` instance at `output/<SYM>_USD/brain.json`:
- Per-signal stats: `{sig: {f, w}}` — fired count + win count
- Bayesian blend: per-symbol posterior + global prior (weight 20). Suppresses signals with blended WR < 40%.
- `signal_weight(sig)` returns weight ∈ [0,1] used by `compute_weighted_confluence()`

Tracks are decayed over time (half-life applied on load).

## Dashboard (port 18065)

`dashboard.py` — Flask app (~5100 lines). Loaded by `goldeneye.py` as a background thread. Serves one HTML page at `/` with 8 panels:

1. **Equity Curve** — live-tick line, extends to "now" with balance + unrealized P&L
2. **COMMAND** — position module (when open) OR situational-awareness module (when empty: NEAREST CANDIDATE + NEXT IN QUEUE + FLEET STATE)
3. **Conviction · Analytics** — score gauge with historical-WR fallback
4. **Signal Intelligence** — top-5 decisions with gate pass/fail
5. **Decision Stream** — live narrative feed with fleet-memory WR chip per row
6. **Symbol Grid** — 20-cell grid
7. **Live Log** — 9 events, tight mono
8. **Global Brain** — regime tiles + arming countdown

Top bar has a **dual clock**: MDT (primary) · UTC (dim). MDT = UTC−6 fixed offset.

All rendering driven by `/api/*` polls (2s / 5s / 15s tiers).

## State Files in `output/`

- `metrics.json` — cumulative trade log (r_history, trade_log, pnl_history, by_direction)
- `equity_curve.json` — ts/bal/upnl/pos/spnl/hwm timeline
- `global_brain.json` — fleet Bayesian factor model
- `autopsy.jsonl` — post-trade autopsies (one JSON per line)
- `regime_sanity.json` — latest sanity snapshot
- `circuit_breaker_state.json` — daily/weekly P&L accumulators
- `kill_switch.json` — presence = kill switch active
- `deviation_cooldowns.json` — 24h cooldown timestamps
- `<SYM>_USD/position.json` — open position (atomic write via `.tmp` + `os.replace`)
- `<SYM>_USD/brain.json` — per-symbol AdaptiveBrain state

## Subscribers / Telegram

`subscribers.json` defines named groups (`fleet_intelligence`, `fleet_pulse`). Token read from `${GOLDENEYE_TELEGRAM_TOKEN}`. `card_renderer.py` (`OracleCardRenderer`) renders PNG cards at 2400px, downsampled to 1200px via Pillow + Windows system fonts. Card types: POSITION OPENED, POSITION CLOSED (win/loss), WHALE ALERT, END OF DAY (trades/flat).

## Key Constants to Know Before Editing

- `get_fee()` → `0.0040` hardcoded. Kraken taker 0.40% (not 0.26%).
- `_TP_BY_REGIME` tuned via 180-day cross-regime backtest. Don't adjust without re-running `bt_tune_regime.py`.
- `_RGM_CONF_MIN` is backtest-calibrated. Comments explain why each value was chosen.
- `_MAX_OPEN_POS = 20` (was 10 in older revs — updated when tiered polling + heat cap added).
- Kill switch at 20% drawdown → **manual reset**: stop bot → delete `output/kill_switch.json` → restart.
- Bayes-override gate params (`min_regime_n=30`, `min_pwin=0.58`) are conservative. Don't relax without justification.

## Disabled Signals (Do Not Re-enable Without Backtest)

In `_sigs_long`:
- `h` — BB %B recovery, 37.5% WR
- `j` — OBV bull divergence, −0.39R
- `k` — BB squeeze, sub-27% WR

Commented out with reason inline. Confirm in backtest before flipping.

## Orphan Files

`archive/orphan_dashboards_2026-04-22/` contains three dead HTML files (`goldeneye_mission_control.html`, `goldeneye_sota.html`, `goldeneye_v3.html`) that were superseded by the Flask-served dashboard at port 18065. Kept for trace / recovery. **Do not serve from archive.**

## Safety Invariants

- All autopsy/sanity/memory code paths are wrapped in try/except — trade close cannot be blocked by learning-layer errors
- All position state writes are atomic via `.tmp` + `os.replace`
- All private Kraken API calls go through `_kraken_api_lock` (RLock, serialized)
- Thread health snapshot (`/health`) reads from a pre-computed cache — never blocks on live compute
- Decision log is ring-buffer bounded (`_MAX_DECISION_LOG`); autopsy jsonl is append-only with no cap (rotate manually if needed)
