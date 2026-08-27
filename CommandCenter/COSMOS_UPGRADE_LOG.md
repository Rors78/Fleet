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
