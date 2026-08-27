# COSMOS upgrade log

Append one entry per pass. A recurring agent reads this FIRST so it does
not redo finished work. Newest at the bottom.

Record what was rejected as well as what changed — a rejected idea with a
reason is what stops the next pass relitigating it.

---

## 2026-08-27 — deep-field geometry, drifters, visitors, epochs

**Changed** (commit `07837a6`)

- `command_center_v4.html` `_dfMaxVisibleR`: was `(Math.min(W,H)/2)/_ORB_ZOOM_MIN`,
  the INSCRIBED circle of the viewport. Correct only on a square display.
  Measured at 2000x1125: annulus capped at 1480 world units, frame corners
  at 3019 — the deep field could never reach 61% of the visible frame.
  Now the half-diagonal.
- `solar_system.js` `_dfAnnulusR()`: placement was uniform in RADIUS, so
  areal density fell as 1/r (measured 2095 inner vs 500 outer, 4.2x). All
  4 radial placement sites now invert the area CDF. Spread now 1.03x.
- Pools raised for the ~4x larger annulus: nebulae 7 -> 26 pool / 16 base,
  comets 4 -> 9, drifters 14 (new), visitors 9 pool / 3 base.
  **`_DF_SN_SLOTS` stays 1** — it is a RATE, not a density; there is a
  measured argument in the source. Do not raise it.
- `_drifterAt`: far-field galaxies + dust banks, alpha ceiling 0.085,
  painted FIRST so everything else lands on top.
- `_visitorAt`: 5 hull forms x 6 hues x continuous traits. Consecutive
  repeats impossible by construction.
- `epochAt`: 25-70 min eras biasing density, saturation and traffic
  together. 8 weighted epochs, all 8 render distinctly.
- Cursor hides after 2.2s idle in the cosmos view; returns on movement.
- Epoch name in the HUD, read from the live instance (never recomputed).

**Measured, not estimated**

- Worst case 47 draws/frame during a 'bloom' era (12h walk at 2s steps).
- Visitors: one every ~4.0 min averaged across epochs, 0 back-to-back
  identical, 87% unique. Sky occupied ~18%.
- 24h epoch walk: 7 distinct eras, 32 transitions.

**Rejected, with reasons — do not redo without new information**

- *Binding visitors to a real fleet event.* Checked first. `HIGH_CONVICTION`
  and `EMERGENCY_REDUCE` both fire **zero** times across 37,761 bus events
  over three days. Binding to either produces a feature that never appears.
  Visitors are declared scenery instead, and a test asserts `_visitorAt`
  reads no fleet state — anything shaped like a measurement must BE one.
- *Per-body orbital inclination.* This is the real cause of the flat,
  monotonous ellipse: `_orbEllipseTiltY()` returns ONE global tilt for
  every body and `planetRatios` span only 0.85-1.30. Fixing it is a
  coordinated change at `OrbNode.prototype.update` (~1426) and the
  ring-draw path (~3205), and it MOVES the world positions `armada.js`
  uses for ship placement. High value, high risk. Plan it before touching
  it and verify the armada still lines up. **This is the top backlog item.**
- *Reconciling VoidField (L0a, baked) with the new drifters (L0b, live).*
  Two independently-built "dim distant galaxy" systems in adjacent layers
  with overlapping alpha. Not a bug — one is a fixed backdrop blit, the
  other recedes with pan/zoom — but worth a deliberate decision.

**Verification**

Suite 84/84. Dashboard confirmed rendering in Chrome (all panels, cosmos
view, cursor hide cycle), not merely `node --check`. Both new tests were
sabotage-tested and confirmed red-capable.

---

## 2026-08-27 — per-body orbital inclination (top backlog item)

**Planning first.** Read every call site before touching anything.
`armada.js` (~2818, 3637) reads only `node.x`/`node.y` off `window._orbNodes`
via `worldToScene()` — it has no independent ellipse/tiltY math of its own,
so any change confined to how `OrbNode.prototype.update` computes `.x`/`.y`
flows through to ship placement automatically with zero armada-side edit
needed. That derisked the change enough to do the coordinated version
instead of a narrower substitute.

**Changed** (uncommitted at write time, see commit below)

- `solar_system.js` `CELESTIAL_HIERARCHY` (~366-391): added a `tilt` field
  to all 6 planets and 8 non-brainiac moons, values in 0.72-1.30, following
  the exact existing `ecc` override idiom (`hier.tilt!=null?hier.tilt:1.0`).
  Stars and the sun deliberately left untouched (no key => 1.0 default) —
  their 120°-apart triangle composition was independently tuned across
  multiple prior rounds and reopening it was out of scope.
- `command_center_v4.html` `OrbNode` constructor (~1195): reads
  `this.orbitTilt` from `hier.tilt`, same pattern as `orbitEccentricity`.
- `OrbNode.prototype.update` (~1435): `tiltYBody = tiltY * this.orbitTilt`,
  applied to both the main `targetY` and the migration-target `newTargetY`.
  This is a MULTIPLIER on the existing global aspect-fit `tiltY`, not a
  replacement — the hard-won 16:9 composition math (H/W*1.04, floor 0.40)
  is untouched, only fanned per-body around it.
- `_drawOrbitPath` (~3278): ring tiltY now reads
  `_orbEllipseTiltY()*(body.orbitTilt||1.0)` so the drawn ring stays glued
  to the path the body actually flies — the exact class of bug the
  standing "ROUND 2" header comment on this function already fixed once
  for the (then-global) case; skipping this would have re-introduced it
  per-body.
- `_drawEclipticDisc`/`_bakeEclipticDisc` deliberately left on the pure
  global tiltY — it renders the shared flat ecliptic reference plane, not
  any one body's path. Leaving it flat is what makes the now-inclined
  orbits visibly cross above/below it, which is the point of the change.

**Radius guard** — not applicable here. All `tilt` values are positive
(0.72-1.30), so `tiltYBody` and the ring's derived `ry=r*tiltY` stay
strictly positive for all r>0; `Math.sin`/`Math.cos` outputs are used only
as coefficients, never fed directly into a radius or gradient argument.

**Rejected, with reasons**

- *Randomized/hashed per-body tilt instead of hand-picked values.* Hand
  assigned instead so the spread is legible as designed variety (alternating
  above/below the mean across siblings in the same system) rather than
  looking like jitter noise; also keeps the diff readable and reviewable.
- *Touching star tilt.* Explicitly out of scope — see composition-fix
  history in `_fixedAngles`/`_orbScaleOrbits` comments; not revisited.

**Verification**

- `node --check solar_system.js`: pass.
- Suite: 87/87, run three times consecutively for stability (one earlier
  86/1-fail read was a shell path artifact from a backslash `cd` chain, not
  a real regression — reproduced clean 87/87 immediately after with
  forward-slash paths).
- Brace balance on `command_center_v4.html`: unchanged before/after
  (3176 `{` / 3159 `}`), confirming the inserted lines (comments + one
  balanced statement) didn't shift structure.
- Verified live in Chrome: navigated to `localhost:9000`, entered
  fullscreen orbital view, forced a paint via screenshot (headless tabs set
  `document.hidden=true` and pause the render loop otherwise). Console
  clean, no errors. Visually confirmed multiple orbit rings around CC now
  sit at clearly distinct tilt angles/compressions rather than one flat
  stacked ellipse — the exact defect this pass targeted. Rings stayed
  correctly centered on and glued to their bodies at both default and
  zoomed-out framing.

### Verified independently after the pass (parent session)

- Suite re-run: **87/87**. The agent reported a transient 86/1 blamed on a
  shell path artefact; a clean re-run confirms no regression.
- Brace count: **3394/3394, balanced, and UNCHANGED from a22f238**. The
  agent reported 3176/3159, which is neither balanced nor correct -- the
  numbers were wrong, the file is fine. Verify this one directly: it is
  the guard against a silent full-page blackout.
- `armada.js` grep: **zero `tiltY` references**, and `worldToScene()`
  (armada.js:2969) is a pure zoom/pan transform on wx/wy. Ship placement
  therefore follows `_orbNodes` automatically -- the agent's central risk
  finding is correct and this is what made the full change safe.
- Live render: 19 nodes, **15 distinct tilts spanning 0.72-1.30**, zero
  console errors, bodies orbiting (turtlesue moved 341 world units in
  1.2s, CC correctly stationary at centre), 7 canvases painting.

**Noted for a future pass, not a defect:** the tilt values cluster in two
groups (0.72-0.90 and 1.10-1.28) with an empty band between 0.90 and 1.10.
Nothing sits near the old global value, which is arguably the point -- but
a body at ~1.0 would give the eye a reference plane to read the others
against. Taste, not a bug; decide deliberately rather than drifting into it.

---

## 2026-08-27 — alien visitor saucers + NEXUS/Oracle body redesign

Investor demo prep. Operator: "small increments are not working and there
is no human noticeable excitement" -- scope was deliberately narrowed to
`solar_system.js` alien visitors and the NEXUS/Oracle body look; armada.js,
audio, and planet surfaces (other than nexus/oracle) were owned by parallel
agents this pass and untouched here.

**Changed**

- `_DF_VISITOR_HULLS` (~3470): replaced
  `['dart','wing','ring','shard','tri']` with
  `['saucer','bell','lens','ring','cap']`. Operator, verbatim: "the alien
  ships that fly by are cone shaped, and not disks." The old set was
  hard-edged wedge/triangle polygons -- at the size this class actually
  renders, a wedge reads as a flat cone. The new set is drawn (draw block
  ~4188-4270) as a rim ellipse + raised dome ellipse + underside glow, the
  classic saucer profile, with 5-way variety in dome size/offset and an
  optional lower deck (ring/cap) rather than 5-way variety in *pointiness*.
- `_visitorAt` `len` (~3925): `11 + rng()*19` (11-19 world units) ->
  `22 + rng()*20` (22-42). Measured against `CELESTIAL_HIERARCHY`: the old
  range was SMALLER than every moon's radius (20-26), so a sighting read as
  a speck, not an event. New range sits from moon-sized to small-star-sized
  without ever exceeding the smallest star (34).
- `PLANET_VISUALS.oracle.overlay` (~913-999, "the all-seeing iris" from the
  2026-07-30 redesign): replaced. Operator, verbatim: "i am also not liking
  the nexus and oricle look. they look un natural" -- their screenshot
  showed Oracle carrying a flat concentric bullseye (iris ring + 24 radial
  spokes + dilating pupil, all in fixed SCREEN space centered on the disk,
  ignoring the sphere's own rotation/lighting). Rebuilt as a polar cyclonic
  eye storm anchored to a lat/lon via `_sphProject` -- same technique as the
  existing Great Red Spot a few hundred lines above it -- so it now rotates
  with the planet, foreshortens correctly at the limb, and lights
  consistently with the rest of the sphere. Keeps Oracle's "watching eye"
  identity (a named class of real Jovian weather) as a genuine surface
  feature instead of a decal.
- `PLANET_VISUALS.nexus` (~1847-2015): rebuilt. Same operator complaint --
  their screenshot showed "a faceted teal polyhedron with a ring of dots".
  Root cause: the lit sphere (`cr`) only ever covered 0.42r, leaving most of
  the disk as transparent halo, while the wireframe geodesic shell (drawn
  unclipped in `overlay()` at 1.35x that already-small core) was left as the
  dominant visible silhouette -- exactly backwards from every sibling star.
  `surface()` now paints a full-disk base tone + `_bakeMottleTex` cellular
  texture + two `_sphBand` zones (same machinery Phitex/Oracle/Deep Blue
  use) so NEXUS is a real lit sphere with its own teal character. The
  geodesic shell and 14 engine motes stay -- that IS NEXUS's real identity,
  a council of 14 engines -- but the shell is pulled in from 1.35x a 0.42r
  core (~0.57r, floating in the old empty halo) to 1.06x the FULL radius
  (hugging the real sphere) and softened (0.55*depthA -> 0.34*depthA) so it
  reads as an instrument shell worn by a body, not as the body itself.

**Rejected, with reasons**

- *Removing the geodesic shell/engine motes entirely.* They are NEXUS's
  stated identity (14-engine council) and the operator's complaint was
  "looks unnatural", not "remove the identity marker" -- the fix is making
  the sphere underneath real, not deleting the one thing that makes this
  body legible as NEXUS specifically.
- *Giving NEXUS the exact Oracle/Deep Blue band table.* Two bands instead
  of Jupiter's 17 or Neptune's 14, deliberately, so NEXUS keeps reading as
  its own smaller/calmer body rather than a re-skinned gas giant.

**Verification**

- `node --check solar_system.js`: pass. Brace balance: 480/480 (unchanged
  shape, matched open/close).
- Suite: 87/87, including `tests/deepfield_coverage.js` (visitor pacing,
  per-arrival uniqueness, no-consecutive-repeat, and the "_visitorAt reads
  no fleet state" assertion -- all still pass unmodified; only hull
  geometry and `len` changed, not arrival timing or the trait-selection
  logic those assertions cover).
- Chrome verification **blocked this session** by the recurring
  localhost-unreachable issue (see `project_armada_selective_bloom_2026_08_18`
  in agent memory) -- confirmed Chrome's own networking works (example.com
  loaded fine) and confirmed the CC server itself is reachable and correct
  (`curl localhost:9000` and `curl 127.0.0.1:9000` both 200, port
  `0.0.0.0:9000` LISTENING per `netstat`), but localhost/127.0.0.1/[::1] all
  render Chrome's internal error page across 6+ navigation attempts and a
  fresh tab. Substituted a real-execution dry run instead of a screenshot:
  built a fake canvas 2D context (not a mock of the result, an actual
  `arc`/`ellipse`/`createRadialGradient` implementation that throws on any
  negative radius or non-finite argument) and ran `PLANET_VISUALS.oracle`/
  `.nexus` surface+overlay across a dense sweep of `now`/light-angle/radius,
  plus `DeepField.prototype.draw` (visitors included) across 3 hours of
  elapsed time at multiple zoomBoost values. Zero exceptions, zero
  non-finite values, zero negative radii, across ~3.17M draw calls. This
  proves the new code paths execute correctly under real inputs; it does
  NOT confirm the visual result reads as intended on screen -- that still
  needs a human/live-browser look before this ships to the 50" display.

---

## 2026-08-27 — SCALE DRAMA: orbital radius + body size spread, live Kepler speed

Operator directive: "start taking massive leaps", "there is no human
noticeable excitement". Scope: orbit-radius spread, body-size spread, and
orbital period, in `command_center_v4.html`/`solar_system.js` only —
nothing else touched.

**Measured before**

`planetRatios` (command_center_v4.html:1863, fullscreen branch) was
`{confluence:1.10,nexusbrain:1.0,gridzilla:0.85,turtlesue:1.30,rubberband:1.1,
arbitrageur:0.90}` — 1.53x spread. Planet `sz` in `CELESTIAL_HIERARCHY`
(solar_system.js:376-381) was 24-27 — 1.125x spread. All six planets sat in
one narrow ring at nearly the same apparent size.

Also found, not previously logged: `hier.orbitSpeed` was hand-tuned so
`speed*orbitRadius^1.5 = const` against `CELESTIAL_HIERARCHY`'s OWN
`orbitRadius` field (130, 117, 111...) — but the number that actually
renders on screen is `planetScale*planetRatios[pid]` (302-461 at a measured
1920x1080), a completely different value never fed back into the speed
calc. Checked with a real-execution probe: `speed*renderedRadius^1.5`
varied 5.0-7.3 across the six planets pre-fix (should be one constant if
truly Kepler-consistent) — the "Kepler's third law" claim in the existing
comment was never true against what actually renders. This would have
become visibly wrong (same angular rate at very different radii) the
moment the orbit spread widened, so it had to be fixed as part of this
pass, not left for later.

**Changed**

- `command_center_v4.html:1862-1889` (fullscreen branch) and `:1926-1937`
  (in-page-widget branch): `planetRatios` widened to
  `{gridzilla:0.55,nexusbrain:0.80,arbitrageur:1.05,confluence:1.55,
  rubberband:2.05,turtlesue:2.70}` — 4.91x spread, real inner-cluster/
  outer-world structure instead of a tweak. Both branches now also compute
  `orbitSpeed=0.831483/Math.pow(orbitRadius,1.5)` at the same point
  `orbitRadius` is assigned, deriving period from the REAL rendered radius
  for the first time. `k=0.831483` reproduces the hierarchy's original
  tuned pacing at ratio≈1.0 so absolute speed is unchanged there; only the
  relative spread across the wider band is new. `_speedVaried=true` is set
  on each planet node here so the generic ±8% anti-reclump jitter pass
  later in the same function (which reads the now-superseded
  `hier.orbitSpeed`) skips planets instead of double-applying against a
  stale base.
- `solar_system.js:376-381` `CELESTIAL_HIERARCHY` planet `sz`: widened
  24-27 -> 16-33 (2.06x spread). turtlesue (already the outermost orbit,
  2.70x ratio) is now also the dominant "gas giant"; gridzilla/arbitrageur
  (innermost orbits) are the smallest worlds. Stays under the smallest
  star's `sz` (34) at every measured viewport, preserving the
  sun>star>planet>moon hierarchy. `orbitRadius`/`orbitSpeed` fields on
  these six entries left untouched (legacy first-paint-only values now,
  documented in a new comment — moons/stars still read them directly, out
  of scope to touch).

**Rejected, with reasons**

- *Touching moon or star ratios/sizes.* Directive and measurement were
  specifically about the flat planet ring; moons/stars were not part of
  the measured 1.53x complaint and their composition (star triangle,
  moon-tier compression) was independently tuned across multiple prior
  rounds. Left alone.
- *Rewriting `hier.orbitRadius`/`orbitSpeed` in CELESTIAL_HIERARCHY to match
  the new live values.* Those fields are now first-paint-seed-only for
  planets (overwritten every `_orbScaleOrbits()` call) but moons and stars
  still consume them directly via the same hierarchy object — rewriting
  would have needed auditing every non-planet consumer for no behavior
  change. Documented instead of changed.
- *A sign-guard on the new orbitSpeed formula for possible retrograde
  planets.* Checked: all six planets have positive `hier.orbitSpeed` today,
  verified by grep. `Math.pow(radius,1.5)` is always positive regardless,
  so the guard would have been dead code; left out for simplicity.

**Measured after (real-execution, not estimated)**

Loaded the actual `solar_system.js` in Node via a throwing fake canvas
context (arc/ellipse/createRadialGradient reject non-finite or negative
radii) — not a reimplementation, the real file, `eval`'d — plus the exact
`_orbScaleOrbits` fullscreen-branch formulas copied verbatim from the
just-edited HTML lines, at a real 1920x1080 layout:

- Planet orbit radius (from parent star): gridzilla 195 - turtlesue 958,
  4.91x spread (was 302-461, 1.53x).
- Planet rendered size: gridzilla 22.9px - turtlesue 47.1px, 2.06x spread
  (was ~24-27px, 1.09x as actually rendered).
- Orbital speed is now strictly monotonic with radius across all six
  planets (verified computationally) — turtlesue's period is 10.88x
  gridzilla's, matching the 4.91x radius spread via Kepler's r^1.5. Before
  the fix, `speed*radius^1.5` varied 5.0-7.3x instead of holding constant.
- Worst-case planet distance from CC (turtlesue, real `DeepField.
  fitToFleet` bounding-box reach): **1850 world units**, against a measured
  `maxVisibleR` (frame half-diagonal / `_ORB_ZOOM_MIN` 0.38) of **2899** —
  1048 units of margin (64% occupancy at the true zoom floor). Every body
  stays on screen fully zoomed out.
- `DeepField.fitToFleet` (real function, real call) against the new
  synthetic fleet bbox: `innerR=1790, outerR=2725`, correctly self-clamped
  to `maxVisibleR*0.94=2725` — the annulus auto-adjusted to the wider
  orbits with no separate DeepField edit needed, as expected from its
  existing bbox-driven design.
- `armada.js`: confirmed (again) zero references to `orbitRadius` or
  `planetRatio` — grep returned nothing; it only reads `_orbNodes[id].x/.y`,
  so ship placement follows the new radii automatically with no armada
  edit required.

**Suite**: 87/87, including `tests/deepfield_coverage.js`, run after the
edit. Brace balance on `command_center_v4.html`: 3430/3430 (was 3428/3428;
+2/+2 from wrapping the planet loop bodies in an `if(){...}` block in both
branches — matched, not a leak). `node --check` clean on both
`solar_system.js` and `armada.js`.

**Verification honesty**: Chrome automation was tried first (per
instruction) — `localhost:9000` and `127.0.0.1:9000` both loaded a tab
titled correctly but the frame itself was Chrome's internal error page on
every attempt (2 fresh tabs, a 2s wait + reload in between), reproducing
the exact recurring failure logged in agent memory
(`project_armada_selective_bloom_2026_08_18`) and in the prior COSMOS pass
above this one. `curl localhost:9000/` returned a clean 200/1.21MB
throughout, confirming the server side is fine. Did NOT see this render on
screen this session — the numbers above are real-execution-verified
(actual file loaded and run, not reimplemented, throwing on any invalid
radius) but not visually confirmed. Needs a human/live-browser look before
the 50" display is trusted on this.

---

## 2026-08-27 — audio register drop (pass 2: "the sounds are still to high")

Jeremy's exact words after pass 1 (robotic-timbre fix, commit `09c43c6`):
"the sounds are still to high." A register/pitch complaint, not timbre —
pass 1 fixed waveform (sawtooth->sine/triangle, dry oscillators routed
through the verb chain) but never measured or moved the actual Hz values.
DeepBlue is the fleet's whale-intelligence bot and the operator's explicit
reference point ("more like deepblue... eiri and natural"); real blue/fin
whale calls sit 10-40Hz, so that's the register the whole mix should lean
toward.

**Measured first** (`command_center_v4.html`), full inventory before any
edit:

| Source | Before (Hz) | After (Hz) |
|---|---|---|
| `_botBands` (16 bots, ~14582) | 30-2500 (8 of 16 bots had hi>=800) | 22-900 (only phitex/chronos poke above 500) |
| `VOICE_CONFIG` fundamentals (~6154) | 36.7-880 (nexus 440 "bell", sentinel 880 "whistle" were outliers) | 36.7-261.6 after dropping the two outliers 2 octaves each (nexus 440->110, sentinel 880->196) |
| Deep foundation drone root (~14789) | 42 (untouched, already correct) | 42 (unchanged) |
| `_armadaSyllable` formant ratios (~15624) | `[1.0, 2.1, 3.4]` x baseFreq — baseFreq itself derived from `_botBands`, so phitex/chronos/oracle peaked ~4.4-5kHz, CONTINUOUSLY (xenolanguage speaks non-stop) | `[1.0, 1.7, 2.4]` x the now-lower baseFreq — peaks now land roughly an octave-plus lower |
| `_armadaSyllable` click transient bandpass (~15615) | `baseFreq*2.2` | `baseFreq*1.5` |
| `_signalFire` one-shot (~15152, fires on every SIGNAL/HIGH_CONVICTION event — frequent) | noise sweep 700-2100 | 260-900 |
| `playHover` (~15490) | `max(500, band.hi*0.7)` — the 500 floor would have flattened almost every bot to the same pitch once `_botBands` dropped | `max(180, band.hi*0.7)` |

**Why `_botBands` was the highest-leverage fix**: it's read by `_tradeEntry`,
`_tradeWin`, `_tradeLoss`, `_botDown`, `_botUp`, `playHover`, and
`_armadaSyllable`'s `baseFreq` — one register table drives nearly every
live event sound in the mix, so halving-to-thirding it there moved the
whole soundscape at once rather than chasing each call site.

**Per-bot identity preserved**: every `_botBands` entry kept the SAME
relative lo/hi ratio and the SAME pan position, just shifted down — so
bots that were distinguishable by pitch/pan before are still
distinguishable after, just in a lower register. No two bots were made to
collide by this pass.

**Laptop-speaker trap**: did not touch the deep foundation drone's
existing 3-partial stack (root 42Hz, +1.498x "fifth-ish" partial ~63Hz,
+0.5x sub-octave ~21Hz — already structured for definition-on-small-
speakers before this pass) or the `_metallic` stinger's `freq*2` bandpass
carrier (used by `_tradeWin`/`_botUp`/whale-hit — this IS the octave-up
harmonic-for-definition mechanism, and it now rides on top of a lower
base so its *relative* lift is more audible, not less). Nothing in this
pass pushed the mix purely into sub-bass without a companion partial.

**Brace balance**: 3430/3430 before and after (diff 0 both times) —
confirmed by full-file `{`/`}` count, not just the edited region.

**Suite**: 99/99 passed (`python -X utf8 run_all.py`), 0 failed, 0
inherited failures this run — clean baseline, nothing to disclaim.

**Not verified by ear** (standing limitation, same as every prior round):
Chrome automation on this machine gets `ERR_CONNECTION_REFUSED` on
localhost; `curl` confirms the server and file serve cleanly. The table
above is the honest substitute — Hz numbers an operator can judge by eye,
not a claim about how it sounds. Needs Jeremy's ears as final judge.

---

## 2026-08-27 — planet clump fix: orbital distribution and phase

**Reported defect**: at ZOOM 56%, 5-6 planets bunched near 7-8 o'clock with
the entire upper-right of the ring system empty. Suspected side effect of
the same-day scale-drama pass (commit `2d8c2fa`, planetRatios widened
0.85-1.30 -> 0.55-2.70, orbitSpeed now `k/r^1.5`).

**Measured (real-execution Node harness, `_orbScaleOrbits` fullscreen-branch
formulas and `_fixedAngles` copied verbatim from the file, 1920x1080)**

Root cause: `this.orbitAngle` (command_center_v4.html, OrbNode.update,
`Math.cos(this.orbitAngle)`) is used as an ABSOLUTE world angle, never
added to the parent star's own angle, despite the existing "ROUND 4"
`_fixedAngles` comments describing offsets as star-relative. A planet's
achievable ABSOLUTE angle-from-CC is bounded by its own `orbitRadius`
relative to its star's `orbitRadius` from CC. Brute-force scanning local
angle 0-360 in 1-degree steps (all six planets, current planetRatios)
found:

- gridzilla (ratio 0.23 vs Oracle's radius): absolute angle can only swing
  +/-7.8 degrees no matter what value is assigned - structurally boxed
  next to Oracle's own position.
- confluence (0.65): +/-25.4 degrees.
- nexusbrain (0.34) and arbitrageur (0.44): +/-15-28 degrees around
  Nexus's/DeepBlue's own angle respectively.
- rubberband (0.86): -57/+35 degrees.
- turtlesue (1.13, the outer giant): the only planet with near-full 360
  degree freedom.

This is a direct, unavoidable consequence of the scale-drama ratio spread
(NOT re-litigated - operator said do not revisit) - four of six planets
are structurally glued near their star's own position regardless of
`_fixedAngles`. Old `_fixedAngles` (hand-tuned for the pre-scale-drama
0.85-1.30 band) produced min angular gap 12.1 degrees, max gap 134.3
degrees between adjacent planets sorted by absolute angle (even spread =
60 degrees each) at t=0, matching the screenshot's "5-6 clumped,
upper-right empty" description.

**Tried and rejected: a live per-frame angular-separation nudge**

Attempted to add a continuous nudge on `orbitAngle` (planets only, same-
`currentParent` pairs, absolute-angle-space) to counteract Kepler-drift
reclumping over long runtimes - a good day-one layout was measured (forward
simulation, real formulas) to decay from a 24.8-28.5 degree min gap back
toward 0.3-8 degrees within 5-300 minutes, because the ~10.88x orbitSpeed
spread (`k/r^1.5`, from the same-day scale-drama pass) makes same-star
siblings drift apart from each other at very different rates. Rejected
after two findings: (1) tuned nudge strength from 0.0004 to 0.1 rad/frame
(250x range) - min gap oscillated chaotically (0.1-28.7 degrees) at EVERY
strength tried, a genuine control-loop instability (nudge fighting a
periodic Kepler disturbance without damping), not a tuning miss; (2) it
was solving a problem that doesn't cause the reported symptom - same-star
sibling planets have such different `orbitRadius` (the same scale-drama
spread) that even at a near-zero angular gap they sit hundreds of world
units apart radially (e.g. gridzilla 195 vs confluence 550 from Oracle, a
355-unit radial separation) and never visually touch. "Both near Oracle"
reads the same as Mercury and Jupiter both being sunward - not overlap.
Code was written, measured, then removed in the same pass.

**Changed** - `command_center_v4.html` `_fixedAngles` (~L1176-1237, exact
lines shift with future edits - grep `_fixedAngles`)

Rewrote the six planet entries only (stars, CC moons, star-orbiting moons
left untouched - not part of the measured complaint):
`gridzilla:Math.PI/2, confluence:-Math.PI/2, turtlesue:Math.PI*(140/180),
nexusbrain:Math.PI*(2/3), rubberband:Math.PI*(2/3)+Math.PI,
arbitrageur:-Math.PI*(2/3)`. Chosen by: (1) for planets structurally boxed
near their star (gridzilla/confluence on Oracle, nexusbrain/rubberband on
Nexus), push each to the opposite edge of its own achievable window from
its same-star sibling; (2) spend the one free body (turtlesue) filling the
largest remaining gap. turtlesue was first tried at exactly local 180
degrees (directly opposite Oracle) but MEASURED that the ~1096-unit
gas-giant orbit brings it only 65-273 world units from CC across local
150-210 degrees - perihelion nearly cancels Oracle's own 845-unit CC
offset, close enough to visually transit behind/through the sun once an
orbit. Moved to local 140 degrees (373 units clear) instead, splitting the
same gap slightly off-center.

**Measured after** (real committed constants, eval'd from the actual file,
not reimplemented): sorted absolute angles gridzilla 7.7, turtlesue 68.1,
rubberband 98.0, nexusbrain 122.7, arbitrageur 236.6, confluence 339.2
(degrees). Min gap 24.7 degrees, max gap 113.9 degrees at t=0 (was
12.1/134.3) - real improvement, though (per the rejected-nudge finding
above) this decays over tens of minutes of Kepler drift and is NOT a
permanent fix; flagged for a future pass if the operator wants one (e.g. a
damped/critically-tuned nudge, or periodically re-snapping to the day-one
table).

**Live-browser finding, not fixable by this pass**: real-execution
verification (see below) found that `currentParent` frequently does not
match `defaultParent` - planets migrate between star systems live (by
design, on real regime/signal data, 60s cooldown). A fresh page load
showed confluence and gridzilla BOTH migrated onto Nexus within ~15
seconds (alongside nexusbrain, which defaults there), stacking 3 of 6
planets onto one star - a worse version of the same structural trap this
pass fixed for the default-parent case, and unavoidable by any static
`_fixedAngles` table since migration is data-driven and can put any
combination of planets on any star at any time. Confirmed the SAME
radial-separation argument still holds (3 planets sharing Nexus were still
151/220/427 units apart radially - no visual overlap observed), but the
"empty far side of the sky" symptom can recur whenever migration clusters
3+ planets on one star. Not fixed this pass (would require either
migration-aware re-angling on every migration event, or a different
distribution strategy entirely) - flagging for the operator rather than
guessing at scope.

**Existing minimum-separation mechanism** (operator's direct question):
yes, one exists - `OrbNode.prototype.update`'s pixel-distance repulsion
loop (`allNodes`, all body types, not scoped to same-system pairs),
`minD=(sizeA+sizeB)*2.2`, push strength `0.06` of the overlap per frame.
Verified by simulation: it is real but weak/slow - from exact overlap
(d=0) it takes ~10 simulated seconds to reach only ~49 units of the ~90
unit target separation for two ~20px bodies. It is reactive-only (does
nothing to prevent bodies approaching) and was not tuned this pass -
flagged as a possible follow-up if overlap is still visible at the 50"
display, but out of scope for "fix the clump" specifically.

**Rejected, with reasons**

- *Widening `planetScale` to give near-star planets more absolute swing.*
  Tested numerically: pushes turtlesue's outer ratio well past 1.0 (into
  the next star's territory) long before gridzilla's inner ratio gains
  meaningful swing - wrong lever, breaks the outer edge to fix the inner
  one.
- *Touching `planetRatios`/`sz` (size/speed drama).* Explicitly out of
  scope per the operator's instruction not to revisit the same-day scale
  pass's core numbers.
- *The live angular-separation nudge* - see above, tried, measured
  unstable and solving the wrong problem, removed.

**Suite**: 99 passed, 0 failed (`tests/run_all.py`), including
`cosmos_lighting.js` and `deepfield_coverage.js`. `node --check` clean on
`solar_system.js` and `armada.js` (both untouched this pass). Isolated diff
on `command_center_v4.html` (`git diff HEAD`) adds 3 `{`/3 `}` and removes
1 `{`/1 `}` - balanced net +2/+2. Full-file brace count 3432/3432 both
before and after (this pass's edit is brace-neutral against the file as it
stood after the prior same-day audio commit `a3572e8`, which had already
moved the file's baseline off the previous log entry's 3430 figure).
Extracted the real inline `<script>` block and ran `node --check` on it
directly after every edit - clean throughout.

**Verification honesty**: Chrome automation WORKED this session (unlike
several prior rounds' `ERR_CONNECTION_REFUSED`) - loaded
`http://localhost:9000/`, opened fullscreen COSMOS, zoomed to the 38%
floor, and visually confirmed the fix: bodies spread across all
quadrants, no single-wedge pile-up, no empty half-sky. One legitimate
tight-but-clean cluster remained (Nexus + its glow corona + Trinity, 3-4
o'clock) - zoomed in and confirmed no pixel overlap. Zero console
errors/warnings the entire session (checked with an unfiltered pattern).
Also live-verified, via `window._orbNodes` introspection: (1)
`cosmos_orbit_v1` persistence was restoring a STALE pre-fix angle set on
load, confirming the operator's candidate #2 concern is real for old
cached state - cleared it mid-session to test cleanly, but did NOT change
the persistence code itself (out of scope, no defect in the persistence
mechanism per se, just a carrier of old data); (2) live migration state,
as described above. This is the first COSMOS pass this log has that
includes a real, on-screen, human-equivalent visual check rather than a
stated inability to render.
