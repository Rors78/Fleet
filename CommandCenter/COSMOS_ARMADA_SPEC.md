# COSMOS ARMADA — Trader Ships Spec

Jeremy's direction (2026-07-30, verbatim intent): "the bots that do actual paper trading should be a race of intelligent beings in their mining ships or something of that nature... I don't like cartoonish designs like the swirly on a moon, etc. SOTA! latest tech all the way! no limits!"

## Core concept
Two object classes in the COSMOS scene; the contrast is the design:
- **THE SPECIES — 6 trader ships** (TurtleSue, NexusBrain, Gridzilla, Rubberband, Arbitrageur, Confluence): intelligent beings working the market-cosmos in mining vessels. They MOVE with intent, deploy, extract, haul home.
- **THE INFRASTRUCTURE — everything else stays celestial/megastructure**: Oracle = observatory station (keep the eye, industrialize it), Deep Blue = deep-space sonar array, NEXUS = geodesic think-tank megastructure, AEGIS = shield grid, Sentinel = lighthouse relay, Chronos = clock spire, PHITEX/Contrarian/Trinity/HiveMind = keep celestial identity but purge anything cartoonish (glyph-on-ball designs are out; structural/mechanical identity only). CC icosahedron = the mothership.

## Mining = trading (the metaphor is literal)
- `TRADE_OPEN` → ship undocks, deploys mining beam on the pair's rock; beam color = direction (green long / red short)
- Position held → beam active, extraction particles flowing to cargo hold
- `TRADE_CLOSE` → beam retracts, haul home; cargo glow green (profit) / venting red (loss)
- Idle/scanning → patrol drift with thruster puffs; DEFENSIVE regime → ships hold near mothership
- Vivarium layer reinterpreted as civilization history: hull scoring from loss streaks, insignia/fleet marks from win streaks, accretion → service stripes. (Keep localStorage `cosmos_vivarium_v1` data, re-skin its rendering.)

## Design language
Hard sci-fi, NOT cartoon: Homeworld / The Expanse. Matte PBR-ish hulls, emissive engine trails, running lights, thruster puffs on maneuvers, spotlights. Ship hull per strategy:
- TurtleSue: dreadnought-miner (Death Star heritage — keep or evolve, Jeremy loves it)
- Gridzilla: lattice-frame harvester, deploys small grid drones at active levels
- Rubberband: agile skiff, visibly banks/flexes on entries
- Arbitrageur: twin-hulled catamaran (evolves its barycenter-pair identity)
- NexusBrain: science vessel, sensor booms (replaces the "swirly" cortex rings — called out as cartoonish)
- Confluence: refinery flagship, four docking umbilicals for its intel feeds

## SOTA technical path
Current renderer = CPU 2D canvas. The leap:
1. Add a WebGL layer via three.js — ALREADY VENDORED (2026-07-30): `D:\CommandCenter\static\vendor\three.module.min.js` + `three.core.min.js` (r178, MIT; the module build imports the core build — serve both, wire via `<script type="importmap">` or a relative module import). Offline-safe, no CDN. Rendered to a transparent canvas composited into the existing layer stack at the correct paint order.
2. Ships = real meshes (low-poly + emissive materials + point lights + particle exhaust). Celestial/infrastructure layers keep existing 2D canvas rendering where it already works.
3. Ship positions still driven by the existing OrbNode physics/migration — WebGL is the *renderer*, not a new simulation. `updateSpatial` audio panning keeps working from the same node positions.
4. 60fps on the 50-inch is non-negotiable; fall back to sprite-based 2D ships only if WebGL compositing genuinely fails, and report that honestly.

## Constraints
- Preserve: composition/spread work (fill-viewport), vivarium data, heartbeats, tanh sizing, migration physics, zoom, labels, audio hooks, all panel tabs.
- Owner: cosmos-dashboard-alchemist (visuals). Coordinate with cosmos-soundtrack-maestro — ships deserve engine/beam sounds later, but that's a separate maestro pass.
- Verification bar: screenshots judged against the stranger test + "does a trade visibly become a mining run?" Watch a real TRADE_OPEN/CLOSE from the event bus drive the ship behavior before claiming done.

## Status
- QUEUED behind: (1) fill-viewport composition agent, (2) soundtrack maestro first pass. One agent at a time on command_center_v4.html.
