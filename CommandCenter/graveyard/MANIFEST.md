# Graveyard Manifest

Dead files moved here by the 2026-07-30 dead-code sweep. Nothing here is imported,
invoked, or documented as a live tool anywhere on D:\. Moved with `git mv` (history
preserved). Restore with `git mv graveyard/<file> .` if ever needed.

Evidence standard used for every file below:
- zero `import`/`from` references across ALL `*.py` on D:\ (every fleet dir, not just CommandCenter)
- zero string/dynamic-dispatch references in CommandCenter `*.py` (route tables, subprocess calls)
- zero references in `*.bat` / `*.ps1` / `*.json` on D:\, Desktop launchers, and Windows scheduled tasks
- not an entrypoint in `fleet_config.py` cmds or `launch_fleet.py`
- not documented CLI usage in CLAUDE.md / README.md / MIGRATION.md (CLAUDE.md updated where it was)

| File | Why dead | Evidence |
|---|---|---|
| `aegis_measure.py` | One-off diagnostic from the 2026-04-09 AEGIS session. Runs at import time against frozen snapshots `aegis_master.json` / `aegis_all.json` (both untouched since Apr 9). | Zero importers/invokers anywhere on D:\; input files frozen 2026-04-09. |
| `aegis_tap.py` | Standalone 900-second event-bus tap built for the same AEGIS session measurement run. Output `aegis_tap_log.jsonl` frozen since Apr 9. | Zero importers/invokers; single-run design ("Runs for exactly 900 seconds"); session artifact. |
| `announce.py` | Self-described "One-time beta access announcement" to Fleet Pulse / Fleet Intelligence. The announcement was sent when the channels went live (2026-07-28). One-time by design. | Zero importers/invokers; docstring declares it one-time. |
| `factor_calibration.py` | Confidence-calibration analysis of TrekBot's `goldeneye_factors.log`. Default log path is `..\TrekBot\goldeneye_factors.log` — `D:\TrekBot` no longer exists; the local `goldeneye_factors.log` copy is 0 bytes, untouched since Apr 3. | Zero importers/invokers; data source directory deleted; local log empty. |
| `regime_expectancy.py` | Regime-conditional expectancy joined on TrekBot factor logs. Self-marked LEGACY in its docstring; sole data source (`goldeneye_factors.log`) frozen at 0 bytes since Apr 3 (TrekBot left the fleet). | Zero importers/invokers; CLAUDE.md Advanced Analytics entry removed by this sweep. |
| `signal_attribution.py` | Per-signal P/L attribution from the same TrekBot factor log. Self-marked LEGACY; same frozen data source. | Zero importers/invokers (the `"signal_attribution"` strings in `signal_broadcaster.py` are dict keys, not this module); CLAUDE.md entry removed by this sweep. |

Deliberately NOT graveyarded despite zero importers:
- `channel_content.py` — reusable Telegram channel management CLI (pinned posts, descriptions) for the live Fleet Pulse / Fleet Intelligence channels. Operational tooling, not dead code.
