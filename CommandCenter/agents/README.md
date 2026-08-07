# Fleet agents — versioned mirror

These are copies of the agent definitions that Claude Code loads from
`C:\Users\Miner\.claude\agents\`. **That directory is not a git repository**,
so three sessions of agent authoring lived in exactly one place with no
history. This mirror exists so the definitions are backed up, diffable and
reviewable.

**The live copy is `~/.claude/agents/`.** This directory is a mirror, not the
source of truth. After editing an agent, re-copy it here, or the two drift and
a reader cannot tell which is current:

```bash
cp C:/Users/Miner/.claude/agents/*.md D:/CommandCenter/agents/
```

## Roster (2026-08-07)

Coverage was audited against the 19-entry `fleet_config.BOTS` roster and 14
infrastructure concerns. All 14 have an owner.

| Agent | Owns |
|---|---|
| `capital-risk-warden` | the shared pool, reservations, deployment caps, position sizing, the `reserve()` gate chain |
| `regression-suite-keeper` | `tests/` and `run_all.py` — whether a green suite means anything |
| `log-integrity-auditor` | whether a log can be trusted as evidence (LIVE-AND-QUIET / BUFFERED / UNWIRED / DEAD) |
| `fleet-auditor` | proof that a module is alive and wired, fleet-wide |
| `fleet-wire-master` | wiring a standalone module into a bot and verifying it lands on the bus |
| `nexus-council-physicist` | the NEXUS mathematical engines, silent-by-design vs broken |
| `simons-fleet-philosopher` | measurement blind spots — what the fleet does not know about itself |
| `cosmos-dashboard-alchemist` | the COSMOS dashboard rendering layer |
| `cosmos-soundtrack-maestro` | the dashboard audio system |
| `beacon-x-broadcaster` | X/Twitter publishing — anything reaching a public timeline |

## Two notes for whoever reads this next

**Discovery is a snapshot.** Claude Code reads this registry at session start,
so an agent authored mid-session is on disk and valid but not directly
loadable until the next session. Until then it runs by pointing
`general-purpose` at the file — which is how both the fleet log audit and the
`capital-risk-warden` smoke test were executed.

**Agent docs go stale like code does, and more quietly.** The
`simons-fleet-philosopher` audit found it was built around fee-drag analysis
on a fleet that has been GROSS with no fee modeling since 2026-07-30 — it
would have fabricated an entire analysis from a measurement surface that no
longer exists. When a fleet-wide fact changes (fee semantics, bot count, where
a log is written), grep this directory for it.

Smoke-testing a new agent by having a subagent execute its definition against
live source — with an explicit instruction to report where **source disagrees
with the doc** — found five factual errors in `capital-risk-warden` within a
minute of writing it. Do that before trusting a new agent.
