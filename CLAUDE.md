# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

This is the **fleet monorepo** (`github.com/Rors78/Fleet`, branch `main`): an
18-bot crypto **signal product** — it paper-trades to measure signal quality
and broadcasts signals to Telegram subscribers. It will never trade real money
itself. Each top-level directory is one bot or service; they run as separate
processes coordinated by Command Center.

**The operational reference is `CommandCenter/CLAUDE.md`** — ports, APIs,
event bus, portfolio pool, launch/restart procedures, gotchas. Per-bot
CLAUDE.md files live in each bot's directory. Read those before editing
anything; this root file is only the map.

## Repo history (read before assuming anything from old docs)

- Until 2026-07-31 this history lived on the `fleet-master` branch of
  `github.com/Rors78/GoldenEye.git` (formerly TrekBot's repo). It was split
  into this dedicated repo; `fleet-master` no longer exists there.
- **`GoldenEye/` is gone from this repo** — GoldenEye is a separate standalone
  product (the $10/mo subscription bot) living at `D:\GoldenEye`, its own repo
  (`github.com/Rors78/GoldenEye`, branch `main`). It shares no runtime with
  the fleet: no pool, no event bus, no fleet_config entry. The directory is
  gitignored here.
- Older session docs/PRs (#1–#11) reference GoldenEye.git URLs — historical
  record, still valid reading, wrong repo for new fleet work.

## Quick start

```bash
cd D:\CommandCenter
python fleet_restart.py            # full cold start (also: --status | --stop)
```
Dashboard: http://localhost:9000 — fleet health, portfolio, COSMOS display.

## Working Rules (fleet-wide)

- NEVER overwrite working code based on assumptions; ALWAYS read existing code first
- No fake stats — ever
- A module is "done" only when it's imported by the running bot, its endpoint
  responds, and its events land on the bus (see CommandCenter/CLAUDE.md,
  "Testing")
- P/L is GROSS fleet-wide (fees removed 2026-07-30) — never reintroduce fee math
- Identify fleet processes by PORT OWNER, never command-line substring
