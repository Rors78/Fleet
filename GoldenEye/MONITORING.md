# External monitoring — dead man's switch

Set up 2026-07-28. **Code is done and tested; needs ~5 minutes of signup to activate.**

## Why push, not poll

The watchdog (`WATCHDOG.md`) only works while this machine is alive. Power loss,
an ISP outage, a dead PSU, or Windows failing to boot all look identical to
"everything is fine" from inside the box — because nothing is left running to
say otherwise.

So the bot **pings outward** on a schedule. If the pings stop, the external
service alerts you. This is a *dead man's switch*: silence is the alarm.

Chosen over exposing a port to the internet because it needs **no inbound
access at all** — no port forwarding, no dynamic DNS, no Flask dev server facing
the world, no new attack surface. Outbound HTTPS only.

## The two switches

| Switch | Pinged by | Answers |
|---|---|---|
| `GOLDENEYE_HEARTBEAT_URL` | the bot, every 5 min | "the bot is healthy and trading" |
| `GOLDENEYE_WATCHDOG_HEARTBEAT_URL` | the watchdog task, every 3 min | "the machine and watchdog are alive" |

Two switches because they answer different questions. The bot's own heartbeat
goes silent whenever the bot is down — *including the seconds while the watchdog
is legitimately restarting it*. The watchdog's heartbeat is what distinguishes
"bot crashed, being handled" from "the whole box is gone".

The second one is optional. Start with the first.

## It only pings when genuinely healthy

This is the part that matters. `_heartbeat_assess()` refuses to ping when:

- any registered thread has been silent > 180s (**wedged**)
- Kraken is disconnected
- the kill switch is tripped

A process that is *running* but has stopped trading must not report itself
alive — that would make the monitor a liar at the exact moment you need it.
When unhealthy it POSTs to `<url>/fail`, which trips healthchecks.io
immediately rather than waiting out the grace period.

Verified: wedged a thread mid-run and watched it switch to `/fail`.

## Setup (5 minutes)

1. Sign up free at **https://healthchecks.io** (20 checks on the free tier;
   cronitor / betteruptime work identically).
2. Create a check:
   - Name: `GoldenEye bot`
   - **Period: 5 minutes**, **Grace: 5 minutes**
     (period must match `GOLDENEYE_HEARTBEAT_INTERVAL`; grace absorbs one
     missed ping so a single network blip is not an alert)
3. Copy its ping URL — looks like `https://hc-ping.com/<uuid>`.
4. Add it to `C:\Users\Miner\Desktop\launch.bat`, above the `python` line:

   ```bat
   set GOLDENEYE_HEARTBEAT_URL=https://hc-ping.com/your-uuid-here
   ```

5. Restart the bot (desktop shortcut). Confirm in the log:

   ```
   External heartbeat started -> every 300s
   ```

   healthchecks.io should flip to green within ~1 minute.

6. In healthchecks.io, add a notification method — **Telegram is supported**,
   so alerts land in the same place as your trading cards. Email as backup.

### Optional second switch

Create a second check (`GoldenEye machine`, period 3 min / grace 6 min), then
set the env var **system-wide** so the scheduled task inherits it — a `set` in
`launch.bat` will NOT reach the watchdog:

```powershell
[Environment]::SetEnvironmentVariable(
  'GOLDENEYE_WATCHDOG_HEARTBEAT_URL','https://hc-ping.com/second-uuid','User')
```

Then restart the two scheduled tasks (or just sign out and back in).

## Tuning

| Env var | Default | Notes |
|---|---|---|
| `GOLDENEYE_HEARTBEAT_URL` | unset (disabled) | Bot's ping URL |
| `GOLDENEYE_HEARTBEAT_INTERVAL` | `300` (5 min) | Keep the check's period in sync |
| `GOLDENEYE_WATCHDOG_HEARTBEAT_URL` | unset (disabled) | Watchdog's ping URL |

Leave both unset and monitoring is simply off — the bot logs
`External heartbeat disabled` and runs exactly as before.

## Failure behavior

- A failed ping is **not** a trading problem: it is caught, logged, and never
  touches the trade path. The bot keeps trading if the monitor is unreachable.
- Repeated failures are logged only on the 3rd and 12th consecutive miss, so a
  network outage cannot flood the log.
- The first ping is delayed 45s after boot so it does not race the health
  snapshot into a false negative.

## What this finally covers

| Failure | Watchdog | External heartbeat |
|---|---|---|
| Bot crashes | restarts it | — (watchdog handles) |
| Bot wedges (stale threads) | restarts it | `/fail` if it persists |
| Kraken disconnects | no | yes |
| Kill switch trips | no | yes |
| Windows Update reboot | restarts at logon | yes, if logon doesn't happen |
| **Power loss / dead PSU** | **no** | **yes** |
| **ISP outage** | **no** | **yes** |
| **Machine stolen/on fire** | **no** | **yes** |

The bottom three rows are the entire reason this exists.
