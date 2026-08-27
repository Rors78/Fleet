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
- Phase 1 (ships standalone) + Phase 2 (live integration) SHIPPED 2026-07-30 (commit fb45c49).

## PHASE 3 — FULL 3D MIGRATION (Jeremy, 2026-07-30 evening)
Verdict on Phase 2: "those ships look like satellites. and the planets and everything else need it also."
1. **Ship silhouette redesign**: at live scale (30-60px) the hulls read as satellites (tube + panels = Hubble). Ships must read as VESSELS: elongated directional hulls, prominent engine glow/trails as the dominant visual signature, motion language (banking, thruster flare). Engine light is what says "ship" at distance — lean on emissives over geometry.
2. **Everything goes 3D**: migrate ALL celestial bodies to the three.js layer — the 10 intel/novel bodies keep their approved identities but as real 3D objects (Oracle = 3D observatory eye-station, Deep Blue = ocean world w/ sonar, Chronos = clock spire station, AEGIS = 3D hex shield array, NEXUS = geodesic megastructure w/ 14 engine lights, PHITEX = golden spiral structure, Sentinel = lighthouse station, Contrarian = retrograde two-tone body, HiveMind = queen + drone swarm, Trinity =三-body). The 2D canvas keeps: starfield background, orbit paths, labels, HUD, decorative deep-space objects. 2D body drawing retires in fullscreen once each 3D replacement is approved.
3. Keep: vivarium data driving 3D visuals, dark-tone/xenolanguage audio hooks, stable-camera zoom semantics, migration physics as the simulation.
- Verification bar unchanged: screenshots judged by orchestrator's eyes, stranger test, live-event proof.

## FACTIONS (Jeremy 2026-07-30: "make three factions and they get 2 each")
Faction = trading thesis. Faction P/L (sum of members, gross, from real data only) is a live scoreboard of which philosophy is winning.
- **VEINRUNNERS** ("the vein keeps running" — momentum): TurtleSue + NexusBrain. Warm gold trim/insignia.
- **TIDEWRIGHTS** ("everything comes back" — mean reversion): Rubberband + Gridzilla. Cyan-teal trim.
- **THE ACCORD** ("truth lives between things" — relative value/consensus): Arbitrageur + Confluence. Silver-violet trim.
Expression: shared hull trim + small insignia glyph per faction; xenolanguage dialects within a faction share a phonetic family (Veinrunners rising contours, Tidewrights cyclical vowel returns, Accord paired call-echo structures); optional faction totals on the CC HUD. Never fabricate faction stats — sum real member data only.

**Support roles (Jeremy 2026-07-30: "make the other ten have roles within the factions")** — assigned by real data-service affinity:
- Veinrunners: Deep Blue = Prospector (whale flow), Sentinel = Pathfinder (drift forecasts), Chronos = Timekeeper (temporal windows)
- Tidewrights: NEXUS = Surveyor (market character/structure), PHITEX = Engineer (thermodynamic equilibrium), Contrarian = Diver (sentiment fade)
- The Accord: Oracle = Cartographer (ranks all claims; feeds Confluence in real code), HiveMind = Quartermaster (portfolio relations), Trinity = Herald (breadth scanning)
- AEGIS = the Warden — faction-NEUTRAL by design: it governs all factions' deployment (true to code); shield of the mothership.
Expression: stations carry faction trim lights; color the EXISTING real data-flow link lines by source faction; station tooltips show role title. Links must reflect real wiring only — never draw a faction link that isn't a real data flow.
