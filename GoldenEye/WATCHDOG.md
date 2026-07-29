# GoldenEye Watchdog — uptime hardening

Set up 2026-07-28. Keeps the bot alive without a human watching it.

## What runs

| Scheduled task | Trigger | Purpose |
|---|---|---|
| **GoldenEye Watchdog** | every 3 min | Check `/health`; restart if dead, unreachable, or wedged |
| **GoldenEye Boot** | 90s after logon | Bring the bot up after a reboot / Windows Update restart |

Both run `watchdog_goldeneye.ps1` as `DESKTOP-MQUFNAN\Miner`, hidden, non-interactive.
The 90s logon delay lets the network and D: drive settle before touching Kraken.

## What the watchdog does

1. Polls `http://localhost:18095/health` and classifies the bot:
   - **Healthy** → log one line, exit 0. No Telegram noise.
   - **Starting** → leave it alone (a boot in progress is not a failure).
   - **Degraded** → `/health` answers but reports stale threads. Treated as a
     failure and restarted: a wedged process is worse than a crashed one,
     because nothing *looks* wrong while it silently stops trading.
   - **Unreachable** → dead or hung. Restart.
2. On restart: kills bot processes, their host consoles, and anything squatting
   on 18095/18065/18096; waits for the ports to actually release; clears `.tmp`
   partial writes; relaunches via `launch.bat`; waits up to 150s for a *real*
   health snapshot (not just a bound port).
3. Alerts Telegram on every restart, on backoff, and on recovery.

## Restart-storm guard

If the bot dies immediately and repeatedly (bad config, corrupt state, Kraken
lockout), restarting forever makes things worse and floods subscribers.

After **4 restarts in 60 minutes** the watchdog stops retrying, sends ONE alert,
and sets `backoff_alerted` in `output/watchdog_state.json`. It resumes
automatically once the window clears; a successful restart clears the flag
immediately and sends a recovery notice.

To clear manually: delete `output/watchdog_state.json`.

## Files

- `watchdog_goldeneye.ps1` — the script (non-interactive; never calls `Read-Host`)
- `output/watchdog.log` — rolling log, auto-trimmed to 2000 lines at 1 MB
- `output/watchdog_state.json` — restart timestamps + backoff flag

## Manual use

```powershell
# Report only, change nothing
powershell -File D:\GoldenEye\watchdog_goldeneye.ps1 -WhatIf

# Check and heal
powershell -File D:\GoldenEye\watchdog_goldeneye.ps1
```

## Windows PowerShell 5.1 gotchas (found the hard way)

Task Scheduler runs **5.1**, not pwsh 7. Three bugs this surfaced, all of which
would have made the watchdog silently useless:

1. **Encoding.** A BOM-less UTF-8 script is read as ANSI by 5.1, mangling every
   non-ASCII char into a parse error. The script is now pure ASCII *and* saved
   with a BOM. Keep it that way — do not paste em-dashes or emoji into it.
2. **Emoji.** `[char]0x1F7E1` throws under 5.1 (16-bit chars). Use the `Emoji`
   helper, which builds surrogate pairs and works in both editions.
3. **Variable shadowing.** PowerShell variables are case-insensitive, so a
   parameter named `$state` shadowed the `$STATE` path variable and every state
   write landed on a garbage filename — silently disabling the storm guard. The
   path variable is now `$STATE_FILE` and the parameter `$stateObj`.

## Verified

- Killed the bot; the scheduled task healed it in **18 seconds, unattended**.
- Storm guard: seeded 4 restarts, watchdog refused to restart and set the flag.
- Both tasks force-run with `LastTaskResult=0`.
- Telegram restart alerts confirmed delivered.

## Not covered by this

- **Machine power loss.** The tasks only run once Windows is up and logged in.
  If the PC is off, the bot is down. Auto-login would close that gap.
- **Internet/Kraken outage.** The watchdog checks that the bot is *running*,
  not that Kraken is reachable. `/health` reports `kraken_api` — worth alerting
  on separately if it stays disconnected.
- **External monitoring.** Everything here runs on the same box. If the machine
  dies, nothing tells you. An external uptime pinger is the remaining gap
  before charging subscribers.
