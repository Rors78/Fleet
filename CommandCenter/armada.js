/**
 * ARMADA — WebGL trader-ship overlay for COSMOS v5
 * ============================================================
 * Standalone ES module. Renders six hard-sci-fi mining vessels
 * (the "species") on a transparent three.js canvas that composites
 * over the existing 2D orbital canvas. Positions are driven entirely
 * by the host dashboard's OrbNode physics (window._orbNodes) — this
 * module owns rendering only, never simulation.
 *
 * Public API (window.Armada):
 *   Armada.init(container, opts) -> handle
 *   Armada.syncFromNodes(nodesLike, view)
 *   Armada.onEvent(evt)
 *   Armada.setQuality(level)   "high" | "medium"
 *   Armada.dispose()
 *
 * Coordinate contract (see COSMOS_ARMADA_SPEC.md + integration notes
 * in armada_test.html): nodesLike[id] = {x,y,currentSize,alive,
 * celestialType,currentParent,direction,tradeFlash,...} in WORLD
 * space (CSS px, origin top-left of the orbital canvas, dimensions
 * _orbLW x _orbLH). view = {zoom,panX,panY,isFS}. Screen-space
 * (CSS px) transform used by the host 2D canvas is:
 *   screenX = worldX * zoom + panX
 *   screenY = worldY * zoom + panY
 * This module reproduces that exact formula so ships ride in lock-step
 * with the 2D bodies underneath.
 *
 * Import path (Phase 2, 2026-07-30): uses the bare "three" specifier,
 * resolved via the host page's <script type="importmap"> entry, NOT a
 * relative path. A relative './static/vendor/three.module.min.js' import
 * here previously 400'd in production — command_center.py's
 * _serve_static_file rejects any "/" inside the requested filename, so
 * static/vendor/* can never be served as-is from this backend (pre-existing
 * routing bug, out of scope for this visuals-only module). The importmap
 * in command_center_v4.html now points "three" at root-level copies
 * (three.module.min.js / three.core.min.js, same directory as this file),
 * which DO serve correctly via the root-asset route. armada_test.html's
 * dev harness may still use its own relative/importmap setup — check it
 * independently if this file's loading path ever changes again.
 */

import * as THREE from 'three';

// ---------------------------------------------------------------
// Fleet identity — six trader ships only. Matches BOTS_DEF colors
// in command_center_v4.html (read-only reference, not edited here).
// ---------------------------------------------------------------
const FLEET = {
  turtlesue: { name: 'TurtleSue', color: 0x4ade80, hull: 'dreadnought' },
  nexusbrain: { name: 'NexusBrain', color: 0xc084fc, hull: 'science' },
  gridzilla: { name: 'Gridzilla', color: 0xfacc15, hull: 'lattice' },
  rubberband: { name: 'Rubberband', color: 0x00e5ff, hull: 'skiff' },
  arbitrageur: { name: 'Arbitrageur', color: 0x7c4dff, hull: 'catamaran' },
  confluence: { name: 'Confluence', color: 0xff6d00, hull: 'refinery' },
};

const TRADER_IDS = Object.keys(FLEET);

// Layer bit used to mark emissive-only geometry (engine glow/plume, beam
// core/glow/flare/halo/spark, station core/superlaser, escort engine glow)
// as bloom-eligible. Kept as a plain object bit (THREE.Layers is a 32-bit
// mask) — layer 0 is the default "everything" layer every object already
// belongs to, so tagging an object with BLOOM_LAYER via .layers.enable()
// makes it visible on BOTH the normal camera (layer 0) and a
// bloom-restricted camera (layer BLOOM_LAYER only), without needing to
// touch every other mesh in the scene.
const BLOOM_LAYER = 1;

// ---------------------------------------------------------------
// Quality presets
// ---------------------------------------------------------------
const QUALITY = {
  high: {
    pixelRatioCap: 2,
    beamParticles: 40,
    exhaustParticles: 14,
    shadows: false,
    lights: 3,
    // SOTA UPSCALE (2026-08-18): selective bloom on emissive geometry only
    // (engine glow, beam plasma, superlaser, running lights) — see
    // BloomPipeline below. Cost is O(screen resolution), NOT O(mesh count),
    // because the bright pass renders with camera.layers restricted to
    // BLOOM_LAYER (most of the ~500-mesh scene is simply not drawn on that
    // pass) and the blur/composite run on a quarter-resolution offscreen
    // target. This is what makes it affordable on a modest GPU (RX 6400)
    // even with the heavy per-ship geometry from the adult-grade rebuild.
    bloom: { enabled: true, resScale: 0.5, blurIterations: 2 },
  },
  medium: {
    pixelRatioCap: 1.5,
    beamParticles: 18,
    exhaustParticles: 6,
    shadows: false,
    lights: 2,
    // Bloom fully off on medium — this tier exists specifically for a
    // struggling machine to degrade INTO, so it must shed the most
    // expensive optional effect first, not just shrink it.
    bloom: { enabled: false, resScale: 0.35, blurIterations: 1 },
  },
};

// ============================================================
// Module state
// ============================================================
let _renderer = null;
let _scene = null;
let _camera = null;
let _canvas = null;
let _container = null;
let _quality = QUALITY.high;
let _qualityLevel = 'high';
let _ships = {}; // id -> ShipRig
let _clock = null;
let _lastView = { zoom: 1, panX: 0, panY: 0, isFS: false };
let _worldW = 900, _worldH = 600;
let _rafId = null;
let _station = null; // CC mothership station rig (Task 3, 2026-07-30)
let _lastCCMeta = { health: 1, eventRate: 0, aegisScore: 0.02, pnlSign: 0 };
let _pendingCCNode = null;
let _disposed = true;
let _bloom = null; // BloomPipeline instance, built lazily in init()

// ============================================================
// Small deterministic PRNG so identical seeds always yield the
// same greeble layout per hull (no visible popping on rebuild).
// ============================================================
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ============================================================
// Shared material helpers — matte PBR-ish hulls, restrained palette
// ============================================================
// REALISM OVERHAUL (2026-07-30, finding 1): Jeremy's verdict on the
// grimdark pass was "cartoon crap... preschool cartoons" despite the hull
// already being dark — the remaining problem wasn't lightness, it was that
// EVERY ship still read as "a single flat color, just a darker one" with a
// thin colored hairline for a seam. Real industrial livery is neutral
// GUNMETAL GRAY (not a tinted-toward-the-fleet-color gray) with the
// identity color demoted to a deliberate painted STRIPE band + placard
// decals, the way a real mining/cargo fleet paints hull numbers and
// warning chevrons rather than dyeing the whole hull. Hull base color is
// now a genuinely neutral desaturated gray (sat<=0.05, was <=0.14 — still
// picking up a faint per-ship cast) so ships read as "the same fleet,
// individually marked" rather than "six different pastel toys".
function hullMaterial(colorHex, opts = {}) {
  // Neutral industrial gunmetal — NOT tinted by the ship's identity color.
  // A faction fleet shares one hull-paint spec; individuality comes from
  // livery stripes/decals (see livery stripe UVs in greebleTexture) and
  // accent emissives, not from tinting the base metal itself.
  const baseL = 0.10 + (opts.lightness || 0);
  const hullColor = new THREE.Color().setHSL(0.6, 0.04, baseL);
  return new THREE.MeshStandardMaterial({
    color: hullColor,
    metalness: opts.metalness != null ? opts.metalness : 0.62,
    roughness: opts.roughness != null ? opts.roughness : 0.58,
    flatShading: !!opts.flat,
    emissive: new THREE.Color(0x050508),
    emissiveIntensity: 1,
  });
}

function accentMaterial(colorHex, intensity = 1.9) {
  // Accents carry the fleet identity color — running lights, seams,
  // couplers, engine glow. Bumped intensity (was 1.4) so they read as
  // the "life of the ship" against the now much-darker hull per spec:
  // "engines, beam couplers, cargo glow, running lights get slightly
  // MORE intensity contrast against the now-dark hulls."
  return new THREE.MeshStandardMaterial({
    color: new THREE.Color(colorHex).multiplyScalar(0.35),
    emissive: new THREE.Color(colorHex),
    emissiveIntensity: intensity,
    metalness: 0.3,
    roughness: 0.35,
  });
}

// Cheap procedural environment map (a soft vertical gradient rendered
// into a small cubemap via PMREM) so low-metalness hull materials have
// something to reflect. Cached — built once per renderer.
let _envMapCache = null;
function buildProceduralEnvMap(renderer) {
  if (_envMapCache) return _envMapCache;
  const pmrem = new THREE.PMREMGenerator(renderer);
  const size = 64;
  const cv = document.createElement('canvas');
  cv.width = size; cv.height = size;
  const ctx = cv.getContext('2d');
  const grad = ctx.createLinearGradient(0, 0, 0, size);
  grad.addColorStop(0, '#7d8bb8');   // "sky" — cool highlight from above
  grad.addColorStop(0.5, '#33384a'); // horizon
  grad.addColorStop(1, '#0a0a10');   // "ground" — dark below
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(cv);
  tex.mapping = THREE.EquirectangularReflectionMapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  const rt = pmrem.fromEquirectangular(tex);
  _envMapCache = rt.texture;
  pmrem.dispose();
  tex.dispose();
  return _envMapCache;
}

function darkTrimMaterial() {
  // Trim is intentionally a touch lighter than pure black (0x1a1a22, not
  // 0x0a0a0e) so engine wells / seams stay READABLE at 24-40px on the
  // 50-inch display instead of vanishing into the space background.
  // Metalness raised (was 0.15) to match the grimdark hull pass — trim
  // is structural gunmetal (struts, nozzle housings, umbilicals), it
  // should catch the same hard specular the hull now does.
  return new THREE.MeshStandardMaterial({ color: 0x1a1a22, metalness: 0.55, roughness: 0.6 });
}

// Panel-line greeble texture generated procedurally (canvas -> CanvasTexture)
// so hulls read as machined/plated even at tiny screen sizes, without
// external assets. Cached per color so we don't regenerate per-ship.
//
// REALISM OVERHAUL (2026-07-30, finding 1): "flat saturated hulls" verdict
// applied even to the already-darkened grimdark pass, because the ONLY
// per-ship visual language was two thin colored hairlines — everything
// else was a uniform flat gray. Real industrial plating reads through
// VALUE variation panel-to-panel (some plates weathered darker, some
// brighter factory-fresh), grime accumulating in corners/seams (ambient
// occlusion), streak wear trailing from panel edges, and a genuine
// LIVERY BAND — a painted stripe with a stenciled placard, not a hairline
// — carrying the identity color. All still procedural/canvas-only, no
// external assets, cached per (color, seed) so cost is one-time.
//
// CRITICAL: map textures MULTIPLY against material.color in three.js
// (finalColor = color * map). The base fill MUST stay neutral/light
// (~0.75-0.85, not near-black) so it only ADDS plating detail without
// crushing the already-dark hull color to solid black (confirmed prior
// regression — see git history). Roughness uses a SEPARATE, low-contrast
// texture (roughness maps should nudge, not swing wildly).
const _greebleCache = new Map();
function greebleTexture(colorHex, seed) {
  const key = colorHex + '_' + seed;
  if (_greebleCache.has(key)) return _greebleCache.get(key);
  const size = 512; // doubled (was 256) — panel-line + wear detail aliased
                     // to mush at 256 once repeat counts dropped for the
                     // larger single-panel plating language below.
  const cv = document.createElement('canvas');
  cv.width = size; cv.height = size;
  const ctx = cv.getContext('2d');
  const rand = mulberry32(seed);
  ctx.fillStyle = '#c6c6cc';
  ctx.fillRect(0, 0, size, size);

  // --- 1. Per-panel value variation — the core "machined plating" cue.
  // A coarse grid of rectangular plates, each given its OWN flat fill
  // value (not a gradient) so adjacent plates read as physically
  // separate pieces of metal, the way individual hull plates on a real
  // ship never match in exact tone.
  const cell = 70 + Math.floor(rand() * 24);
  const cols = Math.ceil(size / cell), rows = Math.ceil(size / cell);
  for (let cy = 0; cy < rows; cy++) {
    for (let cx = 0; cx < cols; cx++) {
      const r = rand();
      // ~15% of panels noticeably darker (weathered/replaced plate),
      // most sit within a tight band near the base value.
      let shade;
      if (r < 0.15) shade = -0.16 - rand() * 0.08;
      else if (r < 0.30) shade = 0.06 + rand() * 0.05;
      else shade = (rand() - 0.5) * 0.05;
      const v = shade >= 0 ? `rgba(255,255,255,${shade.toFixed(3)})` : `rgba(0,0,0,${(-shade).toFixed(3)})`;
      ctx.fillStyle = v;
      ctx.fillRect(cx * cell, cy * cell, cell, cell);
    }
  }

  // --- 2. Thin dark panel-line grid over the value variation — sparse,
  // low-frequency (a dense fine grid aliases to gray mush at 24-40px on
  // screen).
  ctx.strokeStyle = 'rgba(0,0,0,0.42)';
  ctx.lineWidth = 2.5;
  for (let x = 0; x <= size; x += cell) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, size); ctx.stroke();
  }
  for (let y = 0; y <= size; y += cell) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(size, y); ctx.stroke();
  }

  // --- 3. Ambient-occlusion smudges at panel-line intersections/corners
  // — soft dark radial blobs where grime/shadow would naturally collect
  // on a real hull, breaking up the mechanical regularity of the grid.
  for (let cy = 0; cy <= rows; cy++) {
    for (let cx = 0; cx <= cols; cx++) {
      if (rand() > 0.55) continue; // not every joint — patchy, not uniform
      const jx = cx * cell, jy = cy * cell;
      const r = 10 + rand() * 16;
      const grad = ctx.createRadialGradient(jx, jy, 0, jx, jy, r);
      grad.addColorStop(0, `rgba(0,0,0,${(0.22 + rand() * 0.16).toFixed(3)})`);
      grad.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = grad;
      ctx.fillRect(jx - r, jy - r, r * 2, r * 2);
    }
  }

  // --- 4. Streak wear — thin vertical drips trailing down from a handful
  // of panel edges (atmospheric/coolant staining), the single detail that
  // most reads as "this ship has actually flown somewhere" rather than
  // factory-fresh CG plastic.
  for (let i = 0; i < 7; i++) {
    const x = rand() * size;
    const y0 = rand() * size * 0.5;
    const len = 40 + rand() * 90;
    const grad = ctx.createLinearGradient(x, y0, x, y0 + len);
    grad.addColorStop(0, 'rgba(0,0,0,0.24)');
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = grad;
    ctx.fillRect(x - (2 + rand() * 3), y0, 4 + rand() * 6, len);
  }

  // --- 5. LIVERY BAND — the real identity carrier (replaces the old
  // hairline seams). A painted horizontal stripe band with a darker
  // "stencil placard" block inset, at a fixed saturation so six ships
  // share one paint spec and differ only by hue — reads as fleet
  // markings, not a colored hull. Band position varies per-seed so it
  // doesn't always land in the same spot on every hull type.
  const identity = new THREE.Color(colorHex);
  const idHSL = { h: 0, s: 0, l: 0 };
  identity.getHSL(idHSL);
  const stripeColor = new THREE.Color().setHSL(idHSL.h, 0.55, 0.42);
  const stripeHex = '#' + stripeColor.getHexString();
  const bandY = size * (0.32 + rand() * 0.36);
  const bandH = size * 0.09;
  ctx.fillStyle = stripeHex;
  ctx.globalAlpha = 0.85;
  ctx.fillRect(0, bandY, size, bandH);
  ctx.globalAlpha = 1;
  // thin darker pinstripe borders on the band so it reads as applied
  // paint with masking-tape edges, not a texture bleed
  ctx.strokeStyle = 'rgba(0,0,0,0.4)';
  ctx.lineWidth = 3;
  ctx.beginPath(); ctx.moveTo(0, bandY); ctx.lineTo(size, bandY); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(0, bandY + bandH); ctx.lineTo(size, bandY + bandH); ctx.stroke();
  // stencil placard — a dark block with thin bright hazard-style corner
  // ticks, the "hull number" read at a glance without needing real text
  const plX = size * (0.12 + rand() * 0.5), plW = size * 0.16, plH = bandH * 0.72;
  const plY = bandY + (bandH - plH) / 2;
  ctx.fillStyle = 'rgba(10,10,14,0.55)';
  ctx.fillRect(plX, plY, plW, plH);
  ctx.strokeStyle = 'rgba(230,230,235,0.55)';
  ctx.lineWidth = 1.5;
  ctx.strokeRect(plX + 3, plY + 3, plW - 6, plH - 6);

  const tex = new THREE.CanvasTexture(cv);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  _greebleCache.set(key, tex);
  return tex;
}

// Separate roughness texture — same panel layout but rendered as a
// LOW-CONTRAST grayscale (roughness maps should nudge, not swing wildly)
const _roughCache = new Map();
function roughnessTexture(seed) {
  const key = 'rough_' + seed;
  if (_roughCache.has(key)) return _roughCache.get(key);
  const size = 256;
  const cv = document.createElement('canvas');
  cv.width = size; cv.height = size;
  const ctx = cv.getContext('2d');
  const rand = mulberry32(seed + 9999);
  ctx.fillStyle = '#a8a8a8'; // mid-gray base roughness
  ctx.fillRect(0, 0, size, size);
  ctx.strokeStyle = 'rgba(255,255,255,0.25)';
  ctx.lineWidth = 2;
  const cell = 42 + Math.floor(rand() * 20);
  for (let x = 0; x <= size; x += cell) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, size); ctx.stroke();
  }
  for (let y = 0; y <= size; y += cell) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(size, y); ctx.stroke();
  }
  const tex = new THREE.CanvasTexture(cv);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  _roughCache.set(key, tex);
  return tex;
}

// Soft radial-falloff sprite for engine glow discs (TARGET 2, 2026-07-30
// grimdark restyle pass). The engine glow was a hard-edged CircleGeometry
// at up to 0.95 opacity — a flat solid-color disc glued directly onto the
// dark nozzle cylinder behind it. At 30-60px that pair (bright hard disc +
// dark rod-shaped nozzle mesh) reads as "solid colored rod exhaust", not a
// glow — confirmed against the live screenshot description ("orange
// box-shaped ship with solid yellow rod exhausts"). This sprite gives the
// glow a real radial falloff (bright core -> soft additive edge, alpha
// reaching zero well before the sprite's own bounding circle) so it reads
// as light bleeding off the nozzle instead of a painted-on colored cap.
// White-based canvas (tinted by the mesh's own `color`, additive-blended)
// so one cached texture serves every fleet color.
const _softGlowCache = new Map();
function softGlowTexture() {
  if (_softGlowCache.has('g')) return _softGlowCache.get('g');
  const size = 128;
  const cv = document.createElement('canvas');
  cv.width = size; cv.height = size;
  const ctx = cv.getContext('2d');
  const grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, 'rgba(255,255,255,1)');
  grad.addColorStop(0.35, 'rgba(255,255,255,0.75)');
  grad.addColorStop(0.7, 'rgba(255,255,255,0.18)');
  grad.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  _softGlowCache.set('g', tex);
  return tex;
}

// 1D longitudinal fade for the mining beam (SOTA ROUND 4). Used as
// alphaMap on the beam core/glow cones: CylinderGeometry side UVs put
// v=1 at the +Y (impact) end and v=0 at the -Y (emitter) end, and
// CanvasTexture flipY maps v=1 to the canvas top row — so a white->dim
// top-to-bottom gradient makes the beam fully bright at the impact
// point and dissipating toward the ship, which is the "energy, not
// matter" longitudinal cue. Grayscale because alphaMap samples green.
const _beamFadeCache = new Map();
function _beamFadeTexture() {
  if (_beamFadeCache.has('f')) return _beamFadeCache.get('f');
  const cv = document.createElement('canvas');
  cv.width = 2; cv.height = 64;
  const ctx = cv.getContext('2d');
  const grad = ctx.createLinearGradient(0, 0, 0, 64);
  grad.addColorStop(0, 'rgb(255,255,255)');   // impact end — full alpha
  grad.addColorStop(0.55, 'rgb(190,190,190)');
  grad.addColorStop(1, 'rgb(70,70,70)');      // emitter end — dim, not gone
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, 2, 64);
  const tex = new THREE.CanvasTexture(cv);
  _beamFadeCache.set('f', tex);
  return tex;
}

function greebledHullMaterial(colorHex, seed, repeat = 3, opts) {
  const mat = hullMaterial(colorHex, opts);
  const tex = greebleTexture(colorHex, seed);
  const t = tex.clone();
  t.needsUpdate = true;
  t.repeat.set(repeat, repeat);
  mat.map = t;
  const rTex = roughnessTexture(seed).clone();
  rTex.needsUpdate = true;
  rTex.repeat.set(repeat, repeat);
  mat.roughnessMap = rTex;
  return mat;
}

// ============================================================
// Engine nozzle / running-light helper — shared across all hulls
//
// PHASE 3a REDESIGN (2026-07-30): at live scale (30-60px) these ships read
// as satellites, not vessels — Jeremy's verbatim callout. Spec principle:
// "engine light is what says ship at distance", lean on emissives over
// geometry. A round glow disc alone reads as a status LED; what sells
// "propulsion" is a short additive PLUME stretching away from the nozzle
// along the facing axis, whose LENGTH/OPACITY react to real orbital speed
// (see _tick's exhaust-scaling block) — a stationary ship shows a short
// idle flicker, a fast-moving one trails visible fire. Bumped the glow
// disc itself bigger too (0.65->1.0x radius) since it's now the dominant
// per-engine visual signature, not a small accent dot.
// ============================================================
function addEngineNozzle(group, x, y, z, radius, colorHex, facing = new THREE.Vector3(0, 0, 1)) {
  // Nozzle housing widened slightly at the hull-facing end (was a uniform
  // taper radius*0.7 -> radius) so the mesh flares into the hull instead
  // of meeting it as a thin bare cylinder — part of the "rod exhaust" fix,
  // this end is the one facing away from the glow/plume, toward the ship
  // body, and is the one most exposed when idle plumes are near-invisible.
  const nozzle = new THREE.Mesh(
    new THREE.CylinderGeometry(radius * 0.7, radius * 1.15, radius * 1.6, 10),
    darkTrimMaterial()
  );
  nozzle.rotation.x = Math.PI / 2;
  nozzle.position.set(x, y, z);
  group.add(nozzle);

  const mountPos = new THREE.Vector3(x, y, z).add(facing.clone().multiplyScalar(radius * 0.9));

  // Soft radial sprite (see softGlowTexture) replaces the old hard-edged
  // CircleGeometry disc — TARGET 2 restyle: this is what stops the glow
  // from reading as a solid-color cap welded onto the nozzle cylinder.
  const glowGeo = new THREE.PlaneGeometry(radius * 2.6, radius * 2.6);
  const glowMat = new THREE.MeshBasicMaterial({
    map: softGlowTexture(), color: colorHex, transparent: true, opacity: 0.95,
    blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
  });
  const glow = new THREE.Mesh(glowGeo, glowMat);
  glow.position.copy(mountPos);
  glow.layers.enable(BLOOM_LAYER); // SOTA upscale — engine glow blooms
  group.add(glow);

  // Exhaust plume — a cone stretched along the facing axis, additive,
  // narrow at the nozzle end. Base length/opacity are set here; _tick()
  // rescales .scale.z and the material opacity every frame from the
  // ship's real speed + engine state (idle/mining/dormant), so a parked
  // ship shows almost nothing and a traveling one trails visible fire —
  // this motion language is what reads as "vessel" at 30-60px where the
  // hull geometry itself is too small to carry the read.
  const plumeGeo = new THREE.ConeGeometry(radius * 0.55, radius * 3.2, 8, 1, true);
  plumeGeo.translate(0, radius * 1.6, 0); // base at cone apex origin, tip trails outward
  plumeGeo.rotateX(Math.PI / 2); // cone's local +Y -> local +Z (matches facing convention below)
  const plumeMat = new THREE.MeshBasicMaterial({
    color: colorHex, transparent: true, opacity: 0.35,
    blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
  });
  const plume = new THREE.Mesh(plumeGeo, plumeMat);
  plume.position.copy(mountPos);
  // Orient the plume's local +Z (post-rotate) to point along `facing`.
  const zAxis = new THREE.Vector3(0, 0, 1);
  const facingN = facing.clone().normalize();
  if (Math.abs(facingN.dot(zAxis)) < 0.9999) {
    plume.quaternion.setFromUnitVectors(zAxis, facingN);
  } else if (facingN.z < 0) {
    plume.rotation.x = Math.PI;
  }
  plume.scale.z = 0.15; // near-invisible at idle; _tick grows this with speed
  plume.layers.enable(BLOOM_LAYER); // SOTA upscale — exhaust plume blooms
  group.add(plume);

  return { nozzle, glow, glowMat, plume, plumeMat, facing: facingN };
}

function addRunningLight(group, x, y, z, colorHex, size = 0.35) {
  const geo = new THREE.SphereGeometry(size, 6, 6);
  const mat = new THREE.MeshBasicMaterial({ color: colorHex });
  const dot = new THREE.Mesh(geo, mat);
  dot.position.set(x, y, z);
  dot.layers.enable(BLOOM_LAYER); // SOTA upscale — running lights bloom
  group.add(dot);
  return dot;
}

// CARGO BAY GLOW MESH (finding 4, realism overhaul 2026-07-30): the P/L
// glow system used to drive `userData.cargoMesh.material.emissive` — and
// every hull pointed that at its MAIN hull mesh (sphere/spine/drum/etc),
// which is exactly why TurtleSue stayed neon green regardless of the dark
// jade base: the glow logic was literally repainting the whole hull's
// emissive color every close. Worse, several hulls share ONE material
// instance across multiple meshes (e.g. Gridzilla's spine/rings/scoop all
// reference the same `hullMat` object), so mutating "the cargo mesh"'s
// material silently glowed every mesh sharing that material too.
// Fix: every hull gets a dedicated small window-strip/vent mesh with its
// OWN unique MeshStandardMaterial instance (never shared, never the hull
// material), built here and returned for the builder to store as
// userData.cargoMesh. The hull base color itself is never touched again —
// only this small glow strip lights up on P/L.
function addCargoBayGlow(group, x, y, z, w, h, d, rotX = 0) {
  const mat = new THREE.MeshStandardMaterial({
    color: 0x101014, metalness: 0.2, roughness: 0.6,
    emissive: new THREE.Color(0x0a0a0e), emissiveIntensity: 0.15,
  });
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
  mesh.position.set(x, y, z);
  mesh.rotation.x = rotX;
  group.add(mesh);
  return mesh;
}

// ============================================================
// HULL BUILDERS — six procedural ships, unit scale ~ hull length 8-12
// units along local +Z (forward). Each returns {group, engines[], lights[]}
// ============================================================

// --- TurtleSue: armored jade shell-world (dreadnought-miner) -----------
// REDESIGN (2026-07-30, SOTA round 2, target 1): the previous "focusing
// dish" was a small dark hemisphere+ring mounted via a bare position offset
// 2.1 units off the hull with nothing visibly connecting it to the sphere —
// at 30-60px on the dashboard this reads exactly as "a stick poking out of
// a ball" (confirmed against a live screenshot: "green sphere with a
// protruding metal rod"), not as a mining turret. Two changes fix this:
// (1) the sphere itself gets faceted plate-tectonic geology (raised
// icosahedral panel seams via a displaced low-poly overlay, "armored jade
// shell" per spec) so the body has surface identity even before you notice
// any attachment, matching the terminator/greeble quality bar the eye
// planet and other identity bodies already clear; (2) the mining dish is
// rebuilt as a flush-mounted turret: a short visible foot merges it
// directly into the hull instead of floating in empty space.
// CORRECTION (2026-07-30, SOTA round 3): this comment previously claimed
// the turret "keeps a standoff scanner arm... foot -> visible strut ->
// housing" — that overpromised what actually got built. There is no
// separate strut/pylon mesh; the housing (dish+ring) is stacked directly
// on top of the foot with zero gap (see turretFoot/dish/dishRing below).
// That is correct and matches the "flush-mounted, not floating" goal, but
// it is a stacked turret, not an articulated arm — fixing the description
// to match the code, not the code to match the description, since the
// round-3 investigation confirmed this geometry was never the source of
// any reported rod (the real rod was TurtleSue's mining BEAM, see
// buildBeam/updateBeam, fixed separately this round).
// ADULT-GRADE REBUILD (2026-07-31, Jeremy's "adult watching" mandate): the
// SOTA-round sphere kept its Death Star identity but was still fundamentally
// ONE primitive (a sphere) plus attachments floating on its surface — the
// icosahedral wireframe fought the greeble texture instead of reinforcing
// it, and the turret/vents/antenna, while flush-mounted, were the only
// silhouette breaks the sphere had. Rebuild keeps the armored-sphere
// identity (Jeremy loves it, don't abandon it) but treats the sphere as a
// CHASSIS that dozens of real sub-assemblies are bolted to, ILM-miniature
// style: a belt of hull-plate caps (hexagonal armor plates proud of the
// surface, not a texture), a heavy polar drill assembly (not a smooth
// turret dome), a rear drive collar with visible manifold plumbing, and
// scattered maintenance clusters — so the silhouette in black reads as
// "armored industrial moon with machinery," not "green ball."
function buildTurtleSue(seed) {
  const g = new THREE.Group();
  const col = FLEET.turtlesue.color;
  const rand = mulberry32(seed);
  const trim = darkTrimMaterial();
  const accent = accentMaterial(col, 1.0);

  // Hull tint runs DARKER than the identity color (deep jade vs bright
  // mint) — screenshot-verified that the full-brightness identity green
  // reads as flat plastic at dashboard scale; the bright color stays on
  // beams/accents/lights where emissive-bright is correct.
  const hullMat = greebledHullMaterial(0x1f6b45, seed, 4);
  const sphere = new THREE.Mesh(new THREE.SphereGeometry(3.3, 26, 20), hullMat);
  g.add(sphere);

  // --- ARMOR PLATE BELT: a ring of proud hexagonal-ish plate caps (flat
  // hexagonal-prism meshes, not decals) breaking the equator so the eye
  // never reads a single unbroken curved surface across the widest part of
  // the silhouette. Each plate is individually seated flush to the sphere
  // normal at that point (position = normal*radius, orient to normal) —
  // real armor plating follows the hull curvature panel by panel.
  const plateGroup = new THREE.Group();
  const plateCount = 10;
  for (let i = 0; i < plateCount; i++) {
    const a = (i / plateCount) * Math.PI * 2;
    const dir = new THREE.Vector3(Math.cos(a), (rand() - 0.5) * 0.28, Math.sin(a)).normalize();
    const plate = new THREE.Mesh(new THREE.CylinderGeometry(0.62, 0.68, 0.22, 6), i % 3 === 0 ? trim : hullMat);
    plate.position.copy(dir.clone().multiplyScalar(3.32));
    plate.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir);
    plate.rotation.y += rand() * Math.PI;
    plateGroup.add(plate);
    // rivet-like corner bolts — tiny, only every other plate to avoid
    // repeat-fatigue at small scale while still reading as fastened metal
    if (i % 2 === 0) {
      const bolt = new THREE.Mesh(new THREE.SphereGeometry(0.05, 5, 4), trim);
      bolt.position.copy(dir.clone().multiplyScalar(3.44));
      plateGroup.add(bolt);
    }
  }
  g.add(plateGroup);

  // --- Secondary raised panel seam cage: kept from the prior pass but
  // dropped opacity further and de-emphasized — now a QUIET background
  // texture cue sitting under the much stronger armor-plate belt above,
  // not the primary "armored" signal anymore.
  const shellGeo = new THREE.IcosahedronGeometry(3.36, 1);
  const shellHSL = { h: 0, s: 0, l: 0 };
  new THREE.Color(col).getHSL(shellHSL);
  const shellLineColor = new THREE.Color().setHSL(shellHSL.h, Math.min(shellHSL.s, 0.55), 0.2);
  const shellMat = new THREE.MeshBasicMaterial({
    color: shellLineColor, transparent: true, opacity: 0.08, wireframe: true,
  });
  const shellPlates = new THREE.Mesh(shellGeo, shellMat);
  g.add(shellPlates);

  // equatorial trench band (Death Star silhouette cue, industrialized) —
  // widened and given a stepped double-ring profile (two nested toruses of
  // different radius/thickness) instead of one uniform ring, so the trench
  // reads as a recessed structural channel with a raised lip, not a single
  // thin painted line.
  const band = new THREE.Mesh(new THREE.TorusGeometry(3.4, 0.16, 6, 36), trim);
  band.rotation.x = Math.PI / 2;
  g.add(band);
  const bandLip = new THREE.Mesh(new THREE.TorusGeometry(3.4, 0.05, 5, 36), hullMat);
  bandLip.rotation.x = Math.PI / 2;
  bandLip.position.y = 0.14;
  g.add(bandLip);
  // trench conduit clusters — short pipe stubs racked along the trench,
  // evenly spaced, breaking the ring into segments with real plumbing read
  for (let i = 0; i < 12; i++) {
    const a = (i / 12) * Math.PI * 2;
    const pipe = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 0.3, 6), trim);
    pipe.position.set(Math.cos(a) * 3.4, 0, Math.sin(a) * 3.4);
    pipe.rotation.z = Math.PI / 2;
    pipe.rotation.y = a;
    g.add(pipe);
  }

  // CARGO BAY GLOW (finding 4): a row of small windowed cargo-bay slits
  // set INTO the trench, own material instance — this is what pulses on
  // P/L now, never the jade shell itself.
  const cargoBay = addCargoBayGlow(g, 2.3, 0, 2.2, 0.5, 0.22, 0.9, 0.15);
  // window strip flanking the cargo bay — small repeated bright slits
  // (scale-contrast greeble) selling "this is a big hull with many decks"
  for (let i = -2; i <= 2; i++) {
    if (i === 0) continue;
    const w = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.08, 0.08), accent);
    const wDir = new THREE.Vector3(2.3, 0, 2.2).normalize();
    const tangent = new THREE.Vector3(-wDir.z, 0, wDir.x);
    w.position.copy(wDir.clone().multiplyScalar(3.35).add(tangent.clone().multiplyScalar(i * 0.28)));
    g.add(w);
  }

  // --- POLAR DRILL ASSEMBLY (was a smooth stacked turret) — now a real
  // mechanical drill head: fluted bit, gearbox housing with visible ribs,
  // twin hydraulic-strut mounts flanking the base, and a rotating collar
  // ring. Flush-seated on the hull surface via the same normal-mount
  // technique as before (foot on surface, no floating gap).
  const dishGroup = new THREE.Group();
  const turretFoot = new THREE.Mesh(new THREE.CylinderGeometry(0.62, 0.85, 0.42, 12), trim);
  turretFoot.position.y = 0.21;
  dishGroup.add(turretFoot);
  // gearbox housing — ribbed cylinder (radial box "fins" around it) instead
  // of a bare drum, this is what says "mechanism," not "cap"
  const gearbox = new THREE.Mesh(new THREE.CylinderGeometry(0.7, 0.72, 0.6, 12), hullMat);
  gearbox.position.y = 0.72;
  dishGroup.add(gearbox);
  for (let i = 0; i < 8; i++) {
    const a = (i / 8) * Math.PI * 2;
    const rib = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.5, 0.1), trim);
    rib.position.set(Math.cos(a) * 0.74, 0.72, Math.sin(a) * 0.74);
    rib.rotation.y = -a;
    dishGroup.add(rib);
  }
  // rotating collar ring — sits between gearbox and drill bit, the
  // "moving part" cue
  const collarRing = new THREE.Mesh(new THREE.TorusGeometry(0.68, 0.09, 6, 20), accent);
  collarRing.rotation.x = Math.PI / 2;
  collarRing.position.y = 1.05;
  dishGroup.add(collarRing);
  // fluted drill bit — cone with longitudinal groove ribs (small boxes
  // radiating around the cone), the working tip a "dish" never sold
  const drillBit = new THREE.Mesh(new THREE.ConeGeometry(0.5, 1.15, 8), trim);
  drillBit.position.y = 1.65;
  dishGroup.add(drillBit);
  for (let i = 0; i < 6; i++) {
    const a = (i / 6) * Math.PI * 2;
    const flute = new THREE.Mesh(new THREE.BoxGeometry(0.05, 1.0, 0.05), hullMat);
    flute.position.set(Math.cos(a) * 0.32, 1.55, Math.sin(a) * 0.32);
    flute.rotation.y = -a;
    dishGroup.add(flute);
  }
  // twin hydraulic struts flanking the base — visible mounting hardware
  // connecting the drill housing back down to the hull surface, the exact
  // "mounting" cue the mandate calls for
  for (const sx of [-1, 1]) {
    const strutPivot = new THREE.Vector3(sx * 0.55, 0.35, 0.2);
    const strut = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.06, 0.75, 6), trim);
    strut.position.copy(strutPivot);
    strut.rotation.z = sx * 0.35;
    dishGroup.add(strut);
    const strutCap = new THREE.Mesh(new THREE.SphereGeometry(0.08, 6, 6), trim);
    strutCap.position.set(sx * 0.85, 0.62, 0.2);
    dishGroup.add(strutCap);
  }
  // Orient the whole assembly to sit normal-to-surface at a point ON the
  // sphere, foot flush against curvature.
  const turretDir = new THREE.Vector3(0.42, 0.62, 0.66).normalize();
  dishGroup.position.copy(turretDir.clone().multiplyScalar(3.3));
  dishGroup.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), turretDir);
  g.add(dishGroup);
  g.userData.dish = dishGroup;

  // --- REAR DRIVE COLLAR: replaces the bare 3-nozzle cluster with a real
  // drive assembly — a recessed heat-shield collar ring the three nozzles
  // are mounted INTO (visible mounting flanges), plus manifold plumbing
  // (curved pipe runs) connecting the nozzles to the hull, so the engine
  // end reads as a built structure, not three tubes stuck to a ball.
  const driveCollar = new THREE.Mesh(new THREE.TorusGeometry(2.15, 0.32, 8, 24, Math.PI * 1.3), trim);
  driveCollar.rotation.x = Math.PI / 2;
  driveCollar.rotation.z = Math.PI * 0.35;
  driveCollar.position.z = -2.55;
  driveCollar.scale.z = 0.55; // flatten into a shield-like collar, not a full donut
  g.add(driveCollar);

  const engines = [];
  const enginePositions = [[-1.4, -1.2, -3.2], [1.4, -1.2, -3.2], [0, -2.1, -2.9]];
  for (const [ex, ey, ez] of enginePositions) {
    // mounting flange plate behind each nozzle — the flush-seat cue
    const flange = new THREE.Mesh(new THREE.CylinderGeometry(0.72, 0.72, 0.14, 10), hullMat);
    flange.position.set(ex, ey, ez + 0.35);
    flange.rotation.x = Math.PI / 2;
    g.add(flange);
    engines.push(addEngineNozzle(g, ex, ey, ez, 0.55, col, new THREE.Vector3(0, 0, -1)));
  }
  // manifold pipe runs — curved-look pipe segments (short angled cylinder
  // chains) linking the three engine flanges back toward the hull core,
  // visible plumbing hint per the mandate's "mechanical logic" bar
  const manifoldPairs = [[[-1.4, -1.2, -3.0], [0, -1.7, -2.6]], [[1.4, -1.2, -3.0], [0, -1.7, -2.6]]];
  for (const [from, to] of manifoldPairs) {
    const fromV = new THREE.Vector3(...from), toV = new THREE.Vector3(...to);
    const mid = fromV.clone().add(toV).multiplyScalar(0.5);
    const len = fromV.distanceTo(toV);
    const pipe = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, len, 6), trim);
    pipe.position.copy(mid);
    pipe.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), toV.clone().sub(fromV).normalize());
    g.add(pipe);
  }

  // Engine ring — the spec's explicit exception for TurtleSue: a sphere
  // can't be elongated without abandoning the Death Star identity Jeremy
  // loves, so the "vessel not satellite" signal here is a visible glowing
  // quarter-band around the rear (drive) hemisphere.
  const engineRingArc = new THREE.Mesh(
    new THREE.TorusGeometry(2.55, 0.09, 6, 20, Math.PI * 1.15),
    accentMaterial(col, 1.1)
  );
  engineRingArc.rotation.x = Math.PI / 2;
  engineRingArc.rotation.z = Math.PI * 0.55;
  engineRingArc.position.z = -2.15;
  g.add(engineRingArc);
  g.userData.engineRingArc = engineRingArc;

  // running lights scattered on hull
  const lights = [];
  for (let i = 0; i < 5; i++) {
    const theta = rand() * Math.PI * 2, phi = rand() * Math.PI;
    const r = 3.45;
    lights.push(addRunningLight(g,
      r * Math.sin(phi) * Math.cos(theta),
      r * Math.cos(phi) * 0.6,
      r * Math.sin(phi) * Math.sin(theta),
      i % 2 === 0 ? 0xff3b30 : 0xffffff, 0.22));
  }

  // MINING-RIG ANATOMY (finding 3): comms/nav antenna array, now a small
  // CLUSTER (mast + two shorter whips + a small dish) instead of a single
  // mast, plus processing vent stacks — bolted assemblies that break the
  // sphere's silhouette in multiple places, not just one point.
  const antDir = new THREE.Vector3(-0.3, 0.85, 0.2).normalize();
  const antBase = antDir.clone().multiplyScalar(3.4);
  const antMast = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.045, 0.9, 5), trim);
  antMast.position.copy(antBase.clone().add(antDir.clone().multiplyScalar(0.45)));
  antMast.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), antDir);
  g.add(antMast);
  const antBaseCollar = new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.13, 0.1, 8), hullMat);
  antBaseCollar.position.copy(antBase);
  antBaseCollar.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), antDir);
  g.add(antBaseCollar);
  addRunningLight(g, ...antBase.clone().add(antDir.clone().multiplyScalar(0.9)).toArray(), 0xff3b30, 0.05);
  // small companion whip + dish, clustered near the main mast — reads as
  // an actual comms array rather than one lone stick
  const whip2Dir = new THREE.Vector3(-0.15, 0.9, 0.35).normalize();
  const whip2Base = whip2Dir.clone().multiplyScalar(3.38);
  const whip2 = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.025, 0.45, 5), trim);
  whip2.position.copy(whip2Base.clone().add(whip2Dir.clone().multiplyScalar(0.22)));
  whip2.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), whip2Dir);
  g.add(whip2);
  const commsDishDir = new THREE.Vector3(-0.42, 0.78, 0.05).normalize();
  const commsDish = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.02, 0.12, 10, 1, true), trim);
  commsDish.position.copy(commsDishDir.clone().multiplyScalar(3.44));
  commsDish.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), commsDishDir);
  g.add(commsDish);

  const ventDirs = [
    new THREE.Vector3(0.55, -0.6, -0.55).normalize(),
    new THREE.Vector3(-0.6, -0.55, -0.5).normalize(),
    new THREE.Vector3(0.15, -0.75, 0.35).normalize(),
  ];
  for (const vd of ventDirs) {
    const vBase = vd.clone().multiplyScalar(3.32);
    const vent = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.2, 0.3, 8), trim);
    vent.position.copy(vBase);
    vent.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), vd);
    g.add(vent);
    // grille cap on each vent — small ribbed disc, reads as louvered
    // exhaust vs a bare cylinder stub
    const grille = new THREE.Mesh(new THREE.CylinderGeometry(0.17, 0.17, 0.04, 8), hullMat);
    grille.position.copy(vBase.clone().add(vd.clone().multiplyScalar(0.17)));
    grille.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), vd);
    g.add(grille);
  }

  // small maintenance hatch cluster — three flush hatch plates with corner
  // bolts, a repeated-detail greeble run that sells "many decks/access
  // points" per the scale-contrast principle
  const hatchDirs = [
    new THREE.Vector3(-0.75, -0.15, 0.62).normalize(),
    new THREE.Vector3(-0.68, -0.05, 0.4).normalize(),
    new THREE.Vector3(-0.82, 0.1, 0.5).normalize(),
  ];
  for (const hd of hatchDirs) {
    const hatch = new THREE.Mesh(new THREE.BoxGeometry(0.28, 0.05, 0.22), trim);
    hatch.position.copy(hd.clone().multiplyScalar(3.35));
    hatch.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), hd);
    g.add(hatch);
  }

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  // Beam now originates from the drill assembly's tip, along the same
  // turretDir used to place/orient the assembly above.
  g.userData.beamMount = turretDir.clone().multiplyScalar(3.3 + 1.9);
  g.userData.cargoMesh = cargoBay;
  g.userData.cargoBaseColor = cargoBay.material.color.clone();
  g.userData.hullLength = 7;
  return { group: g, engines, lights };
}

// --- Gridzilla: lattice-frame harvester (visible truss) ---------------
// ADULT-GRADE REBUILD (2026-07-31): the open-truss identity is right (an
// exposed girder cage reads as industrial by construction) but the ring+4-
// strut repeat was too regular/thin to survive scrutiny — real trusses have
// diagonal cross-bracing (triangulated, not square, cells — square frames
// under load are a structural cliche that reads as "toy Erector set"),
// gusset plates at every joint (not bare strut-meets-strut), and the ore
// pods need visible rack RAILS, not pods floating mid-truss with no
// support. Added a service crane arm at the bow (working-machine cue) and
// thickened primary chords vs bracing so there's a clear structural
// hierarchy (like a real lattice boom) instead of every strut the same gauge.
function buildGridzilla(seed) {
  const g = new THREE.Group();
  const col = FLEET.gridzilla.color;
  const rand = mulberry32(seed);
  const trim = darkTrimMaterial();
  const hullMat = greebledHullMaterial(col, seed, 2);
  const accentDot = accentMaterial(col, 1.1);

  // central spine — the primary load-bearing chord, visibly thicker than
  // any bracing member so the truss reads as hierarchical structure
  const spine = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 7), hullMat);
  g.add(spine);
  // spine flange ribs — periodic collar rings around the spine where truss
  // rings attach, the "bolted assembly" mounting cue instead of struts
  // meeting a bare box with no visible joint
  for (let fz = -3; fz <= 3; fz += 1.5) {
    const flange = new THREE.Mesh(new THREE.BoxGeometry(0.62, 0.62, 0.12), trim);
    flange.position.z = fz;
    g.add(flange);
  }

  // truss frame — 4 primary corner CHORDS (continuous, run the full
  // length, thicker) plus diagonal cross-bracing between ring stations
  // (triangulated, not just perpendicular struts) plus gusset plates at
  // every joint. This is what turns "square wireframe box" into "the kind
  // of truss a crane boom or radio tower actually uses."
  const struts = new THREE.Group();
  const ringCount = 5;
  const ringZs = [], ringSizes = [];
  for (let i = 0; i < ringCount; i++) {
    ringZs.push(-3 + (i * 6) / (ringCount - 1));
    ringSizes.push(1.6 - Math.abs(i - (ringCount - 1) / 2) * 0.12);
  }
  // ring frames (square-ish, kept — the periodic bulkhead read)
  for (let i = 0; i < ringCount; i++) {
    const ringGeo = new THREE.TorusGeometry(ringSizes[i], 0.06, 5, 4);
    const ring = new THREE.Mesh(ringGeo, hullMat);
    ring.rotation.z = Math.PI / 4;
    ring.position.z = ringZs[i];
    struts.add(ring);
  }
  // 4 continuous primary chords — corner-to-corner, thicker than the ring
  // frames, running the whole spine length. Real lattice booms carry load
  // through the corner chords, not the cross-members.
  const chordAngles = [Math.PI / 4, Math.PI * 0.75, Math.PI * 1.25, Math.PI * 1.75];
  for (const a of chordAngles) {
    const avgR = (ringSizes[0] + ringSizes[ringSizes.length - 1]) / 2;
    const chord = new THREE.Mesh(new THREE.BoxGeometry(0.11, 0.11, 6.3), trim);
    chord.position.set(Math.cos(a) * avgR, Math.sin(a) * avgR, 0);
    struts.add(chord);
  }
  // diagonal cross-bracing between consecutive ring stations — triangulated
  // Warren-truss pattern (zig-zag diagonals), the real structural-logic cue
  for (let i = 0; i < ringCount - 1; i++) {
    const z0 = ringZs[i], z1 = ringZs[i + 1];
    const r0 = ringSizes[i], r1 = ringSizes[i + 1];
    const segLen = z1 - z0;
    for (let c = 0; c < 4; c++) {
      const a0 = chordAngles[c];
      const a1 = chordAngles[(c + 1) % 4];
      const p0 = new THREE.Vector3(Math.cos(a0) * r0, Math.sin(a0) * r0, z0);
      const p1 = new THREE.Vector3(Math.cos(a1) * r1, Math.sin(a1) * r1, z1);
      const mid = p0.clone().add(p1).multiplyScalar(0.5);
      const len = p0.distanceTo(p1);
      const diag = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.045, len, 5), trim);
      diag.position.copy(mid);
      diag.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), p1.clone().sub(p0).normalize());
      struts.add(diag);
      // gusset plate at each diagonal's midpoint-facing joint — small flat
      // triangle-ish plate reading as a fastened connector, not a bare
      // strut intersection
      if (i % 2 === 0) {
        const gusset = new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.16, 0.03), hullMat);
        gusset.position.copy(p0);
        struts.add(gusset);
      }
    }
    // strut joint emissive nodes — kept sparse per the "small highlight
    // points only" emissive rule
    if (i % 2 === 0) {
      for (const a of chordAngles) {
        const joint = new THREE.Mesh(new THREE.SphereGeometry(0.09, 6, 6), accentDot);
        joint.position.set(Math.cos(a) * r0, Math.sin(a) * r0, z0);
        struts.add(joint);
      }
    }
  }
  g.add(struts);

  // harvester scoop/collector at bow — small grid drones dock here
  const scoop = new THREE.Mesh(new THREE.ConeGeometry(1.3, 1.6, 4, 1, true), hullMat);
  scoop.rotation.x = -Math.PI / 2;
  scoop.position.z = 3.6;
  g.add(scoop);
  // scoop rim: thin emissive ring outline instead of a filled glowing disc
  const scoopGlow = new THREE.Mesh(new THREE.TorusGeometry(1.0, 0.045, 6, 4), accentMaterial(col, 1.3));
  scoopGlow.rotation.z = Math.PI / 4;
  scoopGlow.position.z = 4.3;
  g.add(scoopGlow);
  // scoop rim structural spokes — 4 short ribs from cone edge to the ring,
  // the "mechanical" read the smooth cone alone doesn't sell
  for (let i = 0; i < 4; i++) {
    const a = (i / 4) * Math.PI * 2 + Math.PI / 8;
    const spoke = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.05, 0.7), trim);
    spoke.position.set(Math.cos(a) * 1.1, Math.sin(a) * 1.1, 4.0);
    spoke.rotation.x = -0.25;
    g.add(spoke);
  }

  // --- SERVICE CRANE ARM (new): a small articulated-looking boom off the
  // dorsal spine, folded back along the hull — the "this rig actively
  // works" mechanical-logic cue the mandate calls out explicitly. Built
  // from a pivot mount, two boom segments at a slight angle (elbow read),
  // and a claw-like grapple head.
  const craneMount = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.2, 0.3, 8), trim);
  craneMount.rotation.x = Math.PI / 2;
  craneMount.position.set(0.3, 0.5, 1.2);
  g.add(craneMount);
  const craneSeg1 = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.12, 1.4), hullMat);
  craneSeg1.position.set(0.3, 0.62, 0.55);
  craneSeg1.rotation.x = 0.35;
  g.add(craneSeg1);
  const craneSeg2 = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 1.0), hullMat);
  craneSeg2.position.set(0.3, 1.0, -0.3);
  craneSeg2.rotation.x = -0.55;
  g.add(craneSeg2);
  const craneClaw = new THREE.Mesh(new THREE.ConeGeometry(0.12, 0.3, 5), trim);
  craneClaw.rotation.x = Math.PI * 0.65;
  craneClaw.position.set(0.3, 1.35, -0.75);
  g.add(craneClaw);

  // MINING-RIG ANATOMY (finding 3): ore container cluster racked inside
  // the open truss cage on visible RACK RAILS (a thin beam each pod's band
  // clips to) rather than floating unsupported mid-truss, a small lit crew
  // capsule dwarfed by the lattice, and a whip antenna array.
  const oreMat = darkTrimMaterial();
  const orePositions = [[0.75, 0.75, -0.4], [-0.75, 0.75, 0.8], [0.75, -0.75, 1.4], [-0.8, -0.7, -0.9]];
  // rack rail — a thin rod running the ore-pod bay length, the pods "clip"
  // to this the way real cargo racking constrains its load
  const rackRail = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 3.2, 5), trim);
  rackRail.position.set(0.78, 0.78, 0.3);
  g.add(rackRail);
  const rackRail2 = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 3.2, 5), trim);
  rackRail2.position.set(-0.78, -0.72, 0.3);
  g.add(rackRail2);
  for (const [px, py, pz] of orePositions) {
    const pod = new THREE.Mesh(new THREE.CylinderGeometry(0.32, 0.32, 0.9, 8), oreMat);
    pod.position.set(px, py, pz);
    pod.rotation.x = Math.PI / 2;
    g.add(pod);
    const band = new THREE.Mesh(new THREE.TorusGeometry(0.33, 0.025, 4, 10), accentMaterial(col, 0.7));
    band.rotation.y = Math.PI / 2;
    band.position.set(px, py, pz);
    g.add(band);
    // rack clip — small bracket where the pod meets its rail
    const clip = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.06, 0.1), trim);
    clip.position.set(px, py + (py > 0 ? 0.05 : -0.05), pz);
    g.add(clip);
  }
  // crew capsule — small, tucked against the spine, dwarfed by the truss
  const crewCapsule = new THREE.Mesh(new THREE.CapsuleGeometry(0.22, 0.5, 4, 6), hullMat);
  crewCapsule.rotation.x = Math.PI / 2;
  crewCapsule.position.set(0, 0.45, -1.5);
  g.add(crewCapsule);
  addRunningLight(g, 0.1, 0.6, -1.3, 0xcfe4ff, 0.06);
  addRunningLight(g, -0.1, 0.6, -1.7, 0xcfe4ff, 0.06);
  // whip antenna array — asymmetric, off the spine
  for (const [ax, ay, az, alen] of [[0.3, 1.15, -2.6, 1.1], [-0.55, 0.95, -2.3, 0.7]]) {
    const whip = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.03, alen, 5), darkTrimMaterial());
    whip.position.set(ax, ay + alen / 2, az);
    g.add(whip);
  }

  // CARGO BAY GLOW (finding 4): a dedicated glow strip along the spine's
  // cargo rack, own material instance — previously `cargoMesh` pointed at
  // `spine`, which SHARES `hullMat` with every truss ring/scoop, so the
  // old P/L glow silently lit the entire lattice every close, not just
  // "cargo". This mesh alone pulses now.
  const cargoBay = addCargoBayGlow(g, 0, -0.32, 0.9, 0.42, 0.16, 1.1);

  // engines at stern, arranged in the truss square, each on a mounting
  // flange plate (was bare nozzles welded to open air at the truss corners)
  const engines = [];
  for (const [ex, ey] of [[-1.1, -1.1], [1.1, -1.1], [-1.1, 1.1], [1.1, 1.1]]) {
    const flange = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.1), hullMat);
    flange.position.set(ex, ey, -3.35);
    g.add(flange);
    engines.push(addEngineNozzle(g, ex, ey, -3.6, 0.32, col, new THREE.Vector3(0, 0, -1)));
  }
  // radiator fins near the engine cluster — thin flat plates, the
  // "heat management near the drive" mechanical-logic cue
  for (const [rx, ry] of [[-1.1, 0], [1.1, 0], [0, -1.1]]) {
    const fin = new THREE.Mesh(new THREE.BoxGeometry(0.4, 0.02, 0.55), trim);
    fin.position.set(rx * 0.75, ry * 0.75 - 0.1, -2.75);
    g.add(fin);
  }

  const lights = [];
  for (let i = 0; i < 4; i++) {
    lights.push(addRunningLight(g, (rand() - 0.5) * 3, (rand() - 0.5) * 2, (rand() - 0.5) * 6, 0xffcc00, 0.18));
  }

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  g.userData.beamMount = new THREE.Vector3(0, 0, 4.6);
  g.userData.cargoMesh = cargoBay;
  g.userData.cargoBaseColor = cargoBay.material.color.clone();
  g.userData.hullLength = 8;
  return { group: g, engines, lights };
}

// --- Rubberband: agile skiff (sleek, banks hard) -----------------------
// PHASE 3a REWORK (2026-07-30): round 1's perpendicular wing box (a
// symmetric BoxGeometry crossing the fuselage at 90 degrees) is the exact
// "satellite with solar panels" silhouette Jeremy called out — confirmed
// via live 40px crop, it reads as a plus-sign, indistinguishable from
// Hubble's cross layout. Fix: sweep the wings BACK into a delta/interceptor
// shape (leading edge angles toward the tail, not perpendicular to the
// fuselage) so the silhouette reads as an arrowhead pointing along the
// direction of travel, and stretch the fuselage so length:beam clears the
// spec's 2.5:1 floor (was ~6.5 hullLength vs ~7.8 wingspan, i.e. WIDER than
// long — now ~9.5 long vs ~3.4 span, ~2.8:1).
// ADULT-GRADE REBUILD (2026-07-31): the delta silhouette is right (arrowhead,
// not a cross) but the fuselage was one bare tapered cylinder and the wings
// were one flat extruded quad each — exactly the "naked primitive" problem.
// Rebuild adds a raised dorsal spine ridge (breaks the cylinder's smooth
// profile), a canopy/cockpit bubble with a frame (interceptor needs a
// pilot-read greenhouse, not just a nose cone), wing root fairings (the
// wing-to-fuselage blend real aircraft always have — a flat wing meeting a
// round fuselage with zero transition is a paper-airplane tell), intake
// scoops ahead of the engine (mechanical logic: engines need to breathe),
// and hardpoint nubs under the wings (armed-vessel read, matches the escort
// wedge language). Kept the 2.5:1+ length:span ratio and hullLength within
// 20% of the current 9.5.
function buildRubberband(seed) {
  const g = new THREE.Group();
  const col = FLEET.rubberband.color;
  const rand = mulberry32(seed);
  const trim = darkTrimMaterial();
  const accent = accentMaterial(col, 1.2);

  const hullMat = greebledHullMaterial(col, seed, 2);
  const bodyShape = new THREE.CylinderGeometry(0.12, 0.62, 7.4, 8);
  const body = new THREE.Mesh(bodyShape, hullMat);
  body.rotation.x = Math.PI / 2;
  body.position.z = 0.4;
  g.add(body);

  // dorsal spine ridge — a thin raised box running the fuselage length,
  // breaks the cylinder's perfectly round cross-section silhouette
  const spineRidge = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.22, 5.6), trim);
  spineRidge.position.set(0, 0.42, 0.6);
  g.add(spineRidge);

  // canopy/cockpit bubble — a small flattened dome forward of the sensor
  // pod, with a thin frame ring at its base, the "someone is flying this"
  // read an interceptor silhouette needs
  const canopy = new THREE.Mesh(
    new THREE.SphereGeometry(0.34, 12, 8, 0, Math.PI * 2, 0, Math.PI / 1.8),
    new THREE.MeshStandardMaterial({ color: 0x0a1018, metalness: 0.2, roughness: 0.15, emissive: 0x0a141c, emissiveIntensity: 0.6 })
  );
  canopy.scale.set(1, 0.72, 1.5);
  canopy.position.set(0, 0.54, 2.1);
  g.add(canopy);
  const canopyFrame = new THREE.Mesh(new THREE.TorusGeometry(0.32, 0.03, 5, 14), trim);
  canopyFrame.rotation.x = Math.PI / 2;
  canopyFrame.scale.set(1, 1.45, 1);
  canopyFrame.position.set(0, 0.4, 2.1);
  g.add(canopyFrame);

  // Swept delta wings: a tapered quad built from a custom BufferGeometry
  // (root wide/forward, tip narrow/aft) instead of a perpendicular box —
  // this is what turns the silhouette from a "+" into an arrowhead.
  function buildDeltaWing(sign) {
    const shape = new THREE.Shape();
    shape.moveTo(0, 1.6);
    shape.lineTo(sign * 3.3, -1.2);
    shape.lineTo(sign * 1.7, -2.0);
    shape.lineTo(0, -1.4);
    shape.closePath();
    const geo = new THREE.ExtrudeGeometry(shape, { depth: 0.09, bevelEnabled: false });
    geo.rotateX(Math.PI / 2);
    geo.translate(0, -0.02, 0);
    return geo;
  }
  const wingMat = greebledHullMaterial(col, seed + 1, 2);
  const wingL = new THREE.Mesh(buildDeltaWing(1), wingMat);
  wingL.position.set(0.35, 0, 0.6);
  const wingR = new THREE.Mesh(buildDeltaWing(-1), wingMat);
  wingR.position.set(-0.35, 0, 0.6);
  g.add(wingL, wingR);

  // wing root fairings — a wedge block at the wing/fuselage junction on
  // each side, the aerodynamic-blend cue a flat wing meeting a round
  // fuselage with zero transition never has (real aircraft always fillet
  // this joint)
  for (const sign of [1, -1]) {
    const fairing = new THREE.Mesh(new THREE.BoxGeometry(0.45, 0.28, 1.3), hullMat);
    fairing.position.set(sign * 0.5, -0.08, 0.5);
    fairing.rotation.y = sign * 0.15;
    g.add(fairing);
  }

  // Wingtip running-light strakes
  const wingAccentL = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.08, 1.4), accent);
  wingAccentL.position.set(2.6, 0.02, -0.4);
  wingAccentL.rotation.y = 0.5;
  const wingAccentR = wingAccentL.clone();
  wingAccentR.position.x = -2.6;
  wingAccentR.rotation.y = -0.5;
  g.add(wingAccentL, wingAccentR);

  // hardpoint nubs under each wing — small angular blocks with a tiny
  // forward stub, the "this fighter is armed" cue the mandate calls out
  for (const sign of [1, -1]) {
    for (const wz of [1.1, -0.2]) {
      const pylon = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.16, 0.4), trim);
      pylon.position.set(sign * (1.6 + Math.abs(wz) * 0.3), -0.22, wz);
      g.add(pylon);
      const stub = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.06, 0.32, 6), trim);
      stub.rotation.x = Math.PI / 2;
      stub.position.set(sign * (1.6 + Math.abs(wz) * 0.3), -0.32, wz + 0.05);
      g.add(stub);
    }
  }

  // nose spike with a mounting collar (was a bare cone floating at the
  // fuselage tip — now visibly rooted)
  const noseCollar = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.2, 0.2, 8), trim);
  noseCollar.rotation.x = Math.PI / 2;
  noseCollar.position.z = 3.7;
  g.add(noseCollar);
  const nose = new THREE.Mesh(new THREE.ConeGeometry(0.18, 1.8, 6), trim);
  nose.rotation.x = Math.PI / 2;
  nose.position.z = 4.5;
  g.add(nose);
  // small nose sensor ring — thin accent band just behind the tip
  const noseRing = new THREE.Mesh(new THREE.TorusGeometry(0.14, 0.02, 4, 10), accent);
  noseRing.rotation.x = Math.PI / 2;
  noseRing.position.z = 4.0;
  g.add(noseRing);

  // MINING-RIG ANATOMY (finding 3): dorsal sensor pod, now with a mount
  // base + small dish detail instead of one bare box, plus whip antenna.
  const sensorBase = new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.1, 0.6), trim);
  sensorBase.position.set(0, 0.44, 1.6);
  g.add(sensorBase);
  const sensorPod = new THREE.Mesh(new THREE.BoxGeometry(0.28, 0.2, 0.5), hullMat);
  sensorPod.position.set(0, 0.55, 1.6);
  g.add(sensorPod);
  const sensorLens = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 0.05, 8), accent);
  sensorLens.rotation.z = Math.PI / 2;
  sensorLens.position.set(0.15, 0.55, 1.6);
  g.add(sensorLens);
  const whip = new THREE.Mesh(new THREE.CylinderGeometry(0.012, 0.02, 0.7, 5), trim);
  whip.position.set(0, 0.85, 0.9);
  g.add(whip);

  // engine housing ring — the "flush-mount" cue at the stern before the
  // nozzle itself, so the nozzle reads as mounted-into the hull rather
  // than glued to the aft cap
  const engineHousing = new THREE.Mesh(new THREE.CylinderGeometry(0.5, 0.58, 0.6, 10), hullMat);
  engineHousing.rotation.x = Math.PI / 2;
  engineHousing.position.set(0, -0.1, -3.4);
  g.add(engineHousing);
  // intake scoops flanking the engine housing — mechanical-logic cue:
  // engines need to breathe, this is what sells "working propulsion" up
  // close vs a bare nozzle stuck on the tail
  for (const sign of [1, -1]) {
    const intake = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.2, 0.5, 8, 1, true), trim);
    intake.rotation.z = Math.PI / 2;
    intake.position.set(sign * 0.55, -0.15, -2.4);
    g.add(intake);
    const intakeLip = new THREE.Mesh(new THREE.TorusGeometry(0.2, 0.03, 5, 12), hullMat);
    intakeLip.rotation.y = Math.PI / 2;
    intakeLip.position.set(sign * 0.8, -0.15, -2.4);
    g.add(intakeLip);
  }

  // Hull running-light line — small dots along the spine
  const spineLights = [];
  for (const zp of [2.6, 0.6, -1.4]) {
    spineLights.push(addRunningLight(g, 0, 0.42, zp, 0xffffff, 0.08));
  }

  const engines = [addEngineNozzle(g, 0, -0.1, -3.7, 0.42, col, new THREE.Vector3(0, 0, -1))];

  // CARGO BAY GLOW (finding 4): small ventral cargo strip, own material
  const cargoBay = addCargoBayGlow(g, 0, -0.5, -0.4, 0.3, 0.14, 1.6);
  // small access hatch flanking the cargo strip — panel-line greeble
  const hatchL = new THREE.Mesh(new THREE.BoxGeometry(0.18, 0.03, 0.3), trim);
  hatchL.position.set(0.22, -0.52, -0.4);
  g.add(hatchL);

  const lights = [
    addRunningLight(g, 2.9, 0, -0.9, 0xff3b30, 0.16),
    addRunningLight(g, -2.9, 0, -0.9, 0x30ff5f, 0.16),
    ...spineLights,
  ];

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  g.userData.beamMount = new THREE.Vector3(0, -0.3, 3.4);
  g.userData.cargoMesh = cargoBay;
  g.userData.cargoBaseColor = cargoBay.material.color.clone();
  g.userData.wingL = wingL;
  g.userData.wingR = wingR;
  g.userData.hullLength = 9.5;
  return { group: g, engines, lights };
}

// --- Arbitrageur: twin-hulled catamaran with connecting spar -----------
// PHASE 3a REWORK (2026-07-30): round 1's two identical capsules at ±1.7
// span joined by a perpendicular crossbar is a symmetric "+"/X silhouette
// from any top-down angle — the exact satellite read Jeremy flagged, and
// arguably the worst offender of the six (confirmed live at 40px: reads
// as antenna cross-panels, not a ship). Fix keeps the twin-hull
// "barycenter pair" identity (the whole point of Arbitrageur's design)
// but staggers the hulls fore/aft by a full hull-length instead of
// side-by-side, and turns the connector from a perpendicular crossbar
// into a forward-swept spine running ALONG the length axis — the
// silhouette reads as one elongated asymmetric vessel with a visible
// twin-drive identity, not a cross.
// ADULT-GRADE REBUILD (2026-07-31): the staggered-hull stagger already
// solved the mirror-symmetry problem, but each hull was still one bare
// capsule with a cone glued to the tip, and the connector spar was two flat
// boxes — none of the three primitives had any secondary structure. Rebuild
// adds hull collar rings (segment breaks along each capsule, so it doesn't
// read as one smooth pill), an equipment box cluster amidships on each hull
// (the "two rigs, not two balloons" cue), visible spar-to-hull mounting
// gussets (the connector currently just clips through both hulls with no
// joint), and asymmetric secondary antennae so the twin-hull pair reads as
// two distinct rigs cooperating, not a mirrored copy-paste.
function buildArbitrageur(seed) {
  const g = new THREE.Group();
  const col = FLEET.arbitrageur.color;
  const rand = mulberry32(seed);
  const trim = darkTrimMaterial();
  const accent = accentMaterial(col, 0.9);
  const hullMat = greebledHullMaterial(col, seed, 2);

  // Hulls staggered fore/aft (offset along Z, not just split along X) —
  // the stagger is what breaks the mirror-symmetry that reads as satellite
  // panels; a real barycenter pair orbiting each other is never perfectly
  // side-by-side at every angle anyway.
  function buildHalfHull(sign) {
    const half = new THREE.Group();
    const hull = new THREE.Mesh(new THREE.CapsuleGeometry(0.5, 4.4, 4, 8), hullMat);
    hull.rotation.x = Math.PI / 2;
    half.add(hull);
    // hull collar rings — segment breaks along the capsule length, the
    // "this is a built hull, not a smooth pill" cue
    for (const cz of [-1.6, -0.2, 1.2]) {
      const collar = new THREE.Mesh(new THREE.TorusGeometry(0.53, 0.05, 5, 14), trim);
      collar.rotation.x = Math.PI / 2;
      collar.position.z = cz;
      half.add(collar);
    }
    // equipment box cluster amidships — 2-3 small boxes bolted to the
    // hull's dorsal surface, the "two independent working rigs" cue that
    // a bare capsule alone can never sell no matter how it's staggered
    for (const [bx, bz, bw] of [[0.35, -0.4, 0.4], [-0.3, 0.3, 0.3], [0.15, 1.0, 0.25]]) {
      const box = new THREE.Mesh(new THREE.BoxGeometry(bw, 0.22, bw * 1.2), trim);
      box.position.set(bx, 0.42, bz);
      half.add(box);
    }
    half.position.set(sign * 1.5, 0, sign * 1.1);
    return { half, hull };
  }
  const left = buildHalfHull(-1);
  const right = buildHalfHull(1);
  g.add(left.half, right.half);

  // MINING-RIG ANATOMY (finding 3): a stubby drill/processing head capping
  // each hull's bow, now with a visible mounting flange (was the drill
  // meeting the capsule with zero transition) plus a small hydraulic strut
  // pair per drill — the "mounted assembly" cue applied consistently.
  for (const half of [left, right]) {
    const drillFlange = new THREE.Mesh(new THREE.CylinderGeometry(0.46, 0.5, 0.18, 10), hullMat);
    drillFlange.rotation.x = Math.PI / 2;
    drillFlange.position.set(0, 0, 2.05);
    half.half.add(drillFlange);
    const drillHead = new THREE.Mesh(new THREE.ConeGeometry(0.42, 1.0, 7), trim);
    drillHead.rotation.x = -Math.PI / 2;
    drillHead.position.set(0, 0, 2.65);
    half.half.add(drillHead);
    const drillRing = new THREE.Mesh(new THREE.TorusGeometry(0.44, 0.045, 5, 12), accent);
    drillRing.rotation.x = Math.PI / 2;
    drillRing.position.set(0, 0, 2.3);
    half.half.add(drillRing);
    // small flute ribs on the drill cone — repeated-detail scale contrast.
    // Added to half.half (each hull's own local group) so they inherit that
    // hull's fore/aft stagger offset instead of landing at world-origin.
    for (let i = 0; i < 5; i++) {
      const a = (i / 5) * Math.PI * 2;
      const flute = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.75, 0.04), hullMat);
      flute.position.set(Math.cos(a) * 0.22, Math.sin(a) * 0.22, 2.55);
      half.half.add(flute);
    }
  }

  // Connector spine — runs diagonally between the staggered hulls (along
  // the length axis), reinforcing elongation. Now with visible mounting
  // gussets where it meets each hull (was a flat box just clipping through
  // both capsules with no joint read).
  const spanX = 3.0, spanZ = 2.2;
  const sparLen = Math.hypot(spanX, spanZ);
  const spar = new THREE.Mesh(new THREE.BoxGeometry(sparLen, 0.18, 0.42), accent);
  spar.rotation.y = Math.atan2(spanX, spanZ);
  spar.position.set(0, 0, 0.15);
  g.add(spar);
  const spar2 = new THREE.Mesh(new THREE.BoxGeometry(sparLen, 0.16, 0.38), trim);
  spar2.rotation.y = Math.atan2(spanX, spanZ);
  spar2.position.set(0, -0.24, 0.15);
  g.add(spar2);
  // mounting gussets at both spar ends — a small flared collar where the
  // connector visibly meets each hull's surface
  for (const sign of [1, -1]) {
    const gusset = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.34, 0.5), trim);
    gusset.position.set(sign * 1.5, -0.05, sign * 1.1);
    gusset.rotation.y = Math.atan2(spanX, spanZ);
    g.add(gusset);
  }

  // central sensor/comm mast at the barycenter (between the staggered
  // hulls), with a secondary shorter asymmetric whip so the pair doesn't
  // mirror perfectly even at the connector
  const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 1.4, 6), trim);
  mast.position.set(0, 0.7, 0.15);
  g.add(mast);
  const mastBase = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.14, 0.14, 8), hullMat);
  mastBase.position.set(0, 0.05, 0.15);
  g.add(mastBase);
  const mastTip = addRunningLight(g, 0, 1.45, 0.15, col, 0.22);
  const secondaryWhip = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.03, 0.6, 5), trim);
  secondaryWhip.position.set(0.22, 0.4, -0.15);
  secondaryWhip.rotation.z = 0.3;
  g.add(secondaryWhip);

  const engines = [
    addEngineNozzle(g, -1.5, 0, -1.9, 0.4, col, new THREE.Vector3(0, 0, -1)),
    addEngineNozzle(g, 1.5, 0, -3.1, 0.4, col, new THREE.Vector3(0, 0, -1)),
  ];
  // radiator fin pair near the engines — heat-management mechanical cue
  const finL = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.02, 0.32), trim);
  finL.position.set(-1.5, -0.32, -1.7);
  g.add(finL);
  const finR = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.02, 0.32), trim);
  finR.position.set(1.5, -0.32, -2.9);
  g.add(finR);

  // CARGO BAY GLOW (finding 4): dedicated glow mesh riding the connector
  // spar — previously `cargoMesh` pointed at `spar` itself. Separated so
  // the spar keeps its steady identity glow and this small strip alone
  // carries P/L state.
  const cargoBay = addCargoBayGlow(g, 0, 0.02, 0.15, 0.5, 0.1, 0.24);
  cargoBay.rotation.y = Math.atan2(spanX, spanZ);

  const lights = [
    addRunningLight(g, -1.5, 0.3, 2.5, 0xff3b30, 0.16),
    addRunningLight(g, 1.5, 0.3, 3.7, 0x30ff5f, 0.16),
  ];

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  g.userData.beamMount = new THREE.Vector3(0, -0.4, 3.9);
  g.userData.cargoMesh = cargoBay;
  g.userData.cargoBaseColor = cargoBay.material.color.clone();
  g.userData.hullLength = 8.0;
  return { group: g, engines, lights };
}

// --- NexusBrain: science vessel, sensor booms (replaces cortex swirl) --
// ADULT-GRADE REBUILD (2026-07-31): the icosahedral core plus a bare cone
// spike is still two naked primitives glued together. Rebuild adds a ring
// of small equipment boxes girdling the core (breaks the icosahedron's
// crystalline silhouette with mounted hardware), a spike collar + fin
// fairings (the cone now visibly integrates with the core instead of
// piercing through it), and thickens the rear service module treatment
// with visible plumbing between it and the core — matches the same
// "mounted, not floating" discipline applied to every other hull this pass.
function buildNexusBrain(seed) {
  const g = new THREE.Group();
  const col = FLEET.nexusbrain.color;
  const rand = mulberry32(seed);
  const trim = darkTrimMaterial();
  const accent = accentMaterial(col, 1.0);
  const hullMat = greebledHullMaterial(col, seed, 2);

  const core = new THREE.Mesh(new THREE.IcosahedronGeometry(1.15, 1), hullMat);
  g.add(core);

  // equipment box girdle — small boxes ringing the core's midsection,
  // mounted hardware breaking the crystalline facets so the core reads as
  // an instrumented hull, not a bare geometric solid
  for (let i = 0; i < 6; i++) {
    const a = (i / 6) * Math.PI * 2;
    const dir = new THREE.Vector3(Math.cos(a), 0.1, Math.sin(a)).normalize();
    const box = new THREE.Mesh(new THREE.BoxGeometry(0.22, 0.18, 0.3), i % 2 === 0 ? trim : hullMat);
    box.position.copy(dir.clone().multiplyScalar(1.22));
    box.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), dir);
    g.add(box);
  }

  // forward command spike, now with a mounting collar fairing where it
  // meets the core (was a bare cone piercing straight through the surface)
  const spikeCollar = new THREE.Mesh(new THREE.CylinderGeometry(0.55, 0.62, 0.35, 10), hullMat);
  spikeCollar.rotation.x = Math.PI / 2;
  spikeCollar.position.z = 1.0;
  g.add(spikeCollar);
  for (let i = 0; i < 4; i++) {
    const a = (i / 4) * Math.PI * 2 + Math.PI / 4;
    const finlet = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.22, 0.4), trim);
    finlet.position.set(Math.cos(a) * 0.5, Math.sin(a) * 0.5, 1.0);
    finlet.rotation.z = a;
    g.add(finlet);
  }
  const spike = new THREE.Mesh(new THREE.ConeGeometry(0.5, 2.2, 6), trim);
  spike.rotation.x = Math.PI / 2;
  spike.position.z = 2.0;
  g.add(spike);
  // spike tip sensor ring — small accent band near the point
  const spikeTipRing = new THREE.Mesh(new THREE.TorusGeometry(0.16, 0.025, 4, 10), accent);
  spikeTipRing.rotation.x = Math.PI / 2;
  spikeTipRing.position.z = 2.85;
  g.add(spikeTipRing);

  // three sensor booms radiating outward — the analytical "reaching out
  // to sense the market" identity, built from hard mechanical parts
  // (rods + dish caps), not a decorative swirl.
  //
  // SOTA ROUND 3 FIX (2026-07-30, item 1): a live screenshot described as
  // "TurtleSue's rod" was actually this ship — TurtleSue's own builder was
  // re-verified exhaustively (every Mesh/Group enumerated, see armada.js
  // buildTurtleSue) and contains no matching geometry; NexusBrain's three
  // booms (CylinderGeometry radius 0.05-0.07, length 2.6 — 2.26x the 1.15
  // core radius, bare from the hull surface out to the tip) plus two
  // 0xffffff running lights are the exact "thin silver rod + white
  // claw/spike, repeated" match. Two fixes: (1) a flared root collar
  // (a short tapered cylinder, matte trim, flush against the core
  // surface) so the boom visibly MOUNTS to the hull instead of a bare
  // hairline cylinder emerging from nothing — same "flush, not floating"
  // principle as TurtleSue's round-2 turret fix; (2) the rod itself
  // thickened (0.05-0.07 -> 0.09-0.12) so it doesn't alias to a hairline
  // at 30-60px, and the tip dish shrunk + given a small mount flare of its
  // own (0.32 -> 0.22 radius, sits half-embedded in the rod tip via a
  // shared position rather than glued externally) so it stops reading as
  // a separate floating white ball.
  const booms = [];
  const boomAngles = [0, (Math.PI * 2) / 3, (Math.PI * 4) / 3];
  for (const a of boomAngles) {
    const boomGroup = new THREE.Group();
    // root collar: flares from the core surface (radius 1.15) outward,
    // eliminates the bare-cylinder-emerging-from-nothing read at the mount
    const collar = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.22, 0.5, 8), darkTrimMaterial());
    collar.rotation.z = Math.PI / 2;
    collar.position.x = 1.15;
    boomGroup.add(collar);
    const rod = new THREE.Mesh(new THREE.CylinderGeometry(0.09, 0.12, 2.2, 8), darkTrimMaterial());
    rod.rotation.z = Math.PI / 2;
    rod.position.x = 1.15 + 1.1;
    boomGroup.add(rod);
    const dish = new THREE.Mesh(new THREE.SphereGeometry(0.22, 10, 8, 0, Math.PI * 2, 0, Math.PI / 1.4), accentMaterial(col, 1.0));
    dish.rotation.z = -Math.PI / 2;
    dish.position.x = 1.15 + 2.2 - 0.1; // overlaps the rod tip slightly — no gap
    boomGroup.add(dish);
    boomGroup.rotation.z = a;
    boomGroup.position.z = -0.3;
    booms.push(boomGroup);
    g.add(boomGroup);
  }
  g.userData.booms = booms;

  // rear service module — now with a collar fairing at the core junction
  // and visible conduit runs (three curved-look pipe segments) linking it
  // back to the core, so the module reads as an attached, plumbed system
  // rather than a second bare cylinder floating behind the icosahedron.
  const rearCollar = new THREE.Mesh(new THREE.CylinderGeometry(0.8, 0.75, 0.3, 10), trim);
  rearCollar.rotation.x = Math.PI / 2;
  rearCollar.position.z = -0.75;
  g.add(rearCollar);
  const rear = new THREE.Mesh(new THREE.CylinderGeometry(0.75, 0.9, 1.6, 10), hullMat);
  rear.rotation.x = Math.PI / 2;
  rear.position.z = -1.6;
  g.add(rear);
  // ribbed detail bands on the rear module — breaks the smooth taper
  for (const rz of [-1.2, -2.0]) {
    const ridge = new THREE.Mesh(new THREE.TorusGeometry(0.83, 0.03, 5, 12), trim);
    ridge.rotation.x = Math.PI / 2;
    ridge.position.z = rz;
    g.add(ridge);
  }
  // conduit runs — three thin pipes bridging core surface to rear collar
  for (let i = 0; i < 3; i++) {
    const a = (i / 3) * Math.PI * 2 + 0.4;
    const from = new THREE.Vector3(Math.cos(a) * 0.5, Math.sin(a) * 0.5, -0.5);
    const to = new THREE.Vector3(Math.cos(a) * 0.65, Math.sin(a) * 0.65, -1.0);
    const mid = from.clone().add(to).multiplyScalar(0.5);
    const conduit = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, from.distanceTo(to), 5), trim);
    conduit.position.copy(mid);
    conduit.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), to.clone().sub(from).normalize());
    g.add(conduit);
  }

  // MINING-RIG ANATOMY (finding 3): a small lit crew module, dwarfed by
  // the icosahedral core + boom array around it — this is the "someone's
  // aboard this science rig" cue the bare geometric core alone can't sell.
  const crewMat = greebledHullMaterial(0x8a8a92, seed + 5, 1);
  const crewPod = new THREE.Mesh(new THREE.CapsuleGeometry(0.28, 0.6, 4, 8), crewMat);
  crewPod.rotation.x = Math.PI / 2;
  crewPod.position.set(0, -0.55, 0.7);
  g.add(crewPod);
  addRunningLight(g, 0.22, -0.55, 1.0, 0xcfe4ff, 0.06);
  addRunningLight(g, -0.22, -0.55, 1.0, 0xffd27a, 0.06);
  addRunningLight(g, 0, -0.55, 0.35, 0xcfe4ff, 0.05);

  const engines = [addEngineNozzle(g, 0, 0, -2.6, 0.5, col, new THREE.Vector3(0, 0, -1))];

  // CARGO BAY GLOW (finding 4): dedicated data-vault glow panel on the
  // rear service module, own material — previously `cargoMesh` was `rear`,
  // which SHARES `hullMat` with `core` (the whole icosahedral body), so
  // the old P/L glow silently lit the entire science-vessel core too.
  const cargoBay = addCargoBayGlow(g, 0, 0.5, -1.8, 0.5, 0.14, 0.7);

  const lights = [
    addRunningLight(g, 0, 1.2, -0.3, 0xffffff, 0.16),
    addRunningLight(g, 0, -1.2, -0.3, 0xffffff, 0.16),
  ];

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  g.userData.beamMount = new THREE.Vector3(0, 0, 3.0);
  g.userData.cargoMesh = cargoBay;
  g.userData.cargoBaseColor = cargoBay.material.color.clone();
  g.userData.hullLength = 6;
  return { group: g, engines, lights };
}

// --- Confluence: refinery flagship, four docking umbilicals ------------
// ADULT-GRADE REBUILD (2026-07-31): the drum already carries the most
// secondary structure of the six (edge lines, process rings, ore pods,
// umbilicals) — the remaining bare-primitive reads are the engine block
// (four nozzles on open air with no housing) and the bow cone (a smooth
// dark triangle with only an edge outline). Added an engine block manifold
// housing (visible mounting plate the four nozzles sit IN, plus tank/
// plumbing hints alongside) and bow collar ribbing so the collector cone
// integrates with the drum instead of just touching it.
function buildConfluence(seed) {
  const g = new THREE.Group();
  const col = FLEET.confluence.color;
  const rand = mulberry32(seed);
  const trim = darkTrimMaterial();
  const hullMat = greebledHullMaterial(col, seed, 3);

  // large drum-shaped refinery hull. Segment count raised 14->22 (cheap,
  // 6 ships total) so a specular highlight rolls smoothly across the
  // curve instead of snapping to one flat 25.7-degree face and clipping
  // to a hard white band under the key light — see the roughness-floor
  // comment in hullMaterial() for the full specular-blowout writeup.
  const drum = new THREE.Mesh(new THREE.CylinderGeometry(1.3, 1.3, 4.4, 22), hullMat);
  drum.rotation.x = Math.PI / 2;
  g.add(drum);

  // Thin silhouette line (TARGET 2, 2026-07-30 restyle) — a bare cylinder
  // drum at 30-60px with no edge definition is exactly the "clip-art box"
  // read the brief called out. A cheap EdgesGeometry wireframe (only the
  // cylinder's real structural edges — end caps + a handful of verticals,
  // not a dense mesh) traces the hull silhouette without adding a real
  // draw-call-heavy outline-shader pass. Same technique already used on
  // the CC station (see buildStation), applied here for the first time on
  // a ship hull.
  const drumEdges = new THREE.LineSegments(
    new THREE.EdgesGeometry(drum.geometry, 25),
    new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 0.22 })
  );
  drumEdges.rotation.copy(drum.rotation);
  g.add(drumEdges);

  // process rings around the drum
  for (const z of [-1.4, 0, 1.4]) {
    const ring = new THREE.Mesh(new THREE.TorusGeometry(1.35, 0.09, 6, 20), darkTrimMaterial());
    ring.rotation.y = Math.PI / 2;
    ring.position.z = z;
    g.add(ring);
  }

  // four docking umbilicals — the "four intel feeds" identity. ROUND 1
  // arranged these at perfect 90-degree radial spacing (i+PI/4 for i=0..3)
  // with equal-length struts — a plan-view silhouette that is, by
  // construction, a symmetric 4-spoke cross: exactly a satellite antenna
  // array. Fix keeps four arms (the "four feeds" identity must stay
  // legible) but sweeps them AFT at an angle (like trailing docking
  // booms on a real refinery ship, not perpendicular spokes) and varies
  // their length/angle per-arm so no two are mirror images — breaks the
  // rotational symmetry that read as "solar panels" while the umbilicals
  // still visibly radiate from the hull.
  const umbilicals = [];
  const umbilicalAngles = [Math.PI * 0.2, Math.PI * 0.75, Math.PI * 1.15, Math.PI * 1.85];
  for (let i = 0; i < 4; i++) {
    const a = umbilicalAngles[i];
    const armLen = 1.9 + (i % 2) * 0.5; // alternate lengths, not uniform
    const arm = new THREE.Group();
    const strut = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.1, armLen, 6), darkTrimMaterial());
    strut.rotation.z = Math.PI / 2;
    strut.position.x = armLen / 2;
    arm.add(strut);
    const coupler = new THREE.Mesh(new THREE.SphereGeometry(0.2, 8, 8), accentMaterial(col, 1.3));
    coupler.position.x = armLen;
    arm.add(coupler);
    // Swept AFT (negative Z bias) instead of a flat radial spoke — arms
    // trail backward off the drum like real docking booms, reinforcing
    // the length axis instead of fighting it with perpendicular spikes.
    arm.position.set(Math.cos(a) * 1.2, Math.sin(a) * 1.2, -0.6 - (i % 2) * 0.4);
    arm.rotation.z = a;
    arm.rotation.x = -0.35; // sweep aft
    umbilicals.push({ arm, coupler });
    g.add(arm);
  }
  g.userData.umbilicals = umbilicals;

  // bow collector cone — the "black triangle nose" silhouette. Same thin
  // edge-line treatment as the drum so the cone's profile stays legible
  // against the space background instead of reading as a flat dark
  // silhouette with no definition.
  const bow = new THREE.Mesh(new THREE.ConeGeometry(1.0, 1.6, 14), trim);
  bow.rotation.x = -Math.PI / 2;
  bow.position.z = 3.0;
  g.add(bow);
  const bowEdges = new THREE.LineSegments(
    new THREE.EdgesGeometry(bow.geometry, 20),
    new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 0.22 })
  );
  bowEdges.rotation.copy(bow.rotation);
  bowEdges.position.copy(bow.position);
  g.add(bowEdges);
  // bow collar — a stepped ring where the cone meets the drum, the "these
  // are two separately built assemblies, bolted together" cue instead of
  // one shape blending seamlessly into the other
  const bowCollar = new THREE.Mesh(new THREE.CylinderGeometry(1.15, 1.02, 0.3, 14), hullMat);
  bowCollar.rotation.x = Math.PI / 2;
  bowCollar.position.z = 2.3;
  g.add(bowCollar);
  for (let i = 0; i < 8; i++) {
    const a = (i / 8) * Math.PI * 2;
    const bolt = new THREE.Mesh(new THREE.SphereGeometry(0.045, 5, 4), trim);
    bolt.position.set(Math.cos(a) * 1.12, Math.sin(a) * 1.12, 2.3);
    g.add(bolt);
  }
  // small forward sensor cluster on the bow face — a purpose greeble the
  // smooth cone tip otherwise lacks
  const bowSensor = new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.13, 0.18, 8), trim);
  bowSensor.rotation.x = Math.PI / 2;
  bowSensor.position.z = 3.75;
  g.add(bowSensor);

  // MINING-RIG ANATOMY (finding 3): racked ore pods along the drum's
  // flanks — this is what turns "boxy hauler" into a proper container
  // ship (visible cargo, not just a smooth tank) while keeping the drum
  // silhouette identity intact. Pods sit in two rows, port/starboard,
  // clamped to the hull with a simple strap band.
  const podMat = darkTrimMaterial();
  const podRows = [-1.05, 1.05];
  const podZs = [-1.7, -0.55, 0.6, 1.75];
  for (const py of podRows) {
    for (let pi = 0; pi < podZs.length; pi++) {
      const pod = new THREE.Mesh(new THREE.BoxGeometry(0.62, 0.5, 0.95), podMat);
      pod.position.set(0, py * 1.15, podZs[pi]);
      g.add(pod);
      const strap = new THREE.Mesh(new THREE.BoxGeometry(0.66, 0.06, 1.0), accentMaterial(col, 0.6));
      strap.position.copy(pod.position);
      g.add(strap);
    }
  }

  // engine manifold housing — a flat mounting plate the four nozzles sit
  // IN (was four nozzles hanging in open air behind the drum with no
  // structure connecting them), plus tank-cluster plumbing hints (two
  // small cylindrical tanks with connecting pipe runs) alongside — the
  // "propellant feed" mechanical-logic cue a refinery hauler needs.
  const manifoldPlate = new THREE.Mesh(new THREE.CylinderGeometry(1.5, 1.4, 0.28, 20), hullMat);
  manifoldPlate.rotation.x = Math.PI / 2;
  manifoldPlate.position.z = -2.15;
  g.add(manifoldPlate);
  const engines = [];
  for (const [ex, ey] of [[-0.9, -0.9], [0.9, -0.9], [-0.9, 0.9], [0.9, 0.9]]) {
    const flange = new THREE.Mesh(new THREE.CylinderGeometry(0.42, 0.42, 0.1, 10), trim);
    flange.position.set(ex, ey, -2.28);
    flange.rotation.x = Math.PI / 2;
    g.add(flange);
    engines.push(addEngineNozzle(g, ex, ey, -2.4, 0.36, col, new THREE.Vector3(0, 0, -1)));
  }
  // twin propellant tanks flanking the manifold, each with a strap band
  // and a short feed pipe running to the manifold plate
  for (const sign of [1, -1]) {
    const tank = new THREE.Mesh(new THREE.CylinderGeometry(0.28, 0.28, 0.9, 10), trim);
    tank.rotation.z = Math.PI / 2;
    tank.position.set(sign * 1.55, 0, -1.5);
    g.add(tank);
    const tankBand = new THREE.Mesh(new THREE.TorusGeometry(0.29, 0.03, 4, 12), hullMat);
    tankBand.rotation.y = Math.PI / 2;
    tankBand.position.set(sign * 1.55, 0, -1.5);
    g.add(tankBand);
    const feedPipe = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, 0.55, 5), trim);
    feedPipe.position.set(sign * 1.55, 0, -1.85);
    g.add(feedPipe);
  }

  const lights = [];
  for (let i = 0; i < 4; i++) {
    lights.push(addRunningLight(g, (rand() - 0.5) * 2.6, (rand() - 0.5) * 2.6, (rand() - 0.5) * 4, 0xffffff, 0.16));
  }

  // CARGO BAY GLOW (finding 4): a dedicated viewport strip set into the
  // drum between two ore pod rows, own material — previously `cargoMesh`
  // was `drum` itself, so every P/L close repainted the ENTIRE refinery
  // hull's emissive rather than a bay.
  const cargoBay = addCargoBayGlow(g, 1.1, 0, 0.05, 0.24, 0.4, 0.9);

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  g.userData.beamMount = new THREE.Vector3(0, 0, 3.9);
  g.userData.cargoMesh = cargoBay;
  g.userData.cargoBaseColor = cargoBay.material.color.clone();
  g.userData.hullLength = 7.5;
  return { group: g, engines, lights };
}

const HULL_BUILDERS = {
  dreadnought: buildTurtleSue,
  lattice: buildGridzilla,
  skiff: buildRubberband,
  catamaran: buildArbitrageur,
  science: buildNexusBrain,
  refinery: buildConfluence,
};

// ============================================================
// STATION HULL MOSAIC (ITEM 3, round 4, 2026-07-31) — Jeremy's reference
// was the DS2 hull shot: a fine panel MOSAIC of thousands of subtly
// value-varied plates plus a city-light speckle, at a scale that reads as
// "moon-sized construction", not "another ship hull, just bigger". The
// generic greebleTexture() (ships: cell 70-94px on a 512px canvas, ~5-7
// cells across, repeat=5) was tuned for 24-60px ship silhouettes and was
// too coarse once stretched over a body this large — reusing it verbatim
// would read as the same blocky ship plating just scaled up. This is a
// SEPARATE, denser generator: smaller cell, wider per-panel value spread,
// and actual baked window-light speckle (small bright dots at panel
// corners/edges) rather than relying only on the discrete addRunningLight
// meshes for "the hull has lights on it" — at station scale a handful of
// discrete point lights reads as sparse, the reference's power is in the
// SHEER NUMBER of tiny lit windows.
const _stationHullCache = new Map();
function stationHullTexture(seed) {
  const key = 'station_' + seed;
  if (_stationHullCache.has(key)) return _stationHullCache.get(key);
  const size = 1024; // 2x the ship greeble canvas — station is viewed as
                      // the scene centerpiece, close enough that ship-scale
                      // texel density would look soft/blurred.
  const cv = document.createElement('canvas');
  cv.width = size; cv.height = size;
  const ctx = cv.getContext('2d');
  const rand = mulberry32(seed + 4242);
  ctx.fillStyle = '#c9c9cf';
  ctx.fillRect(0, 0, size, size);

  // --- 1. Fine panel mosaic — much smaller cells than the ship greeble
  // (22-34px vs 70-94px on a canvas twice the size = roughly 6x the panel
  // DENSITY per unit of hull), and a wider value spread per panel so
  // adjacent plates read as distinct pieces even at a glance from across
  // the room, not just on close inspection.
  const cell = 22 + Math.floor(rand() * 12);
  const cols = Math.ceil(size / cell), rows = Math.ceil(size / cell);
  for (let cy = 0; cy < rows; cy++) {
    for (let cx = 0; cx < cols; cx++) {
      const r = rand();
      let shade;
      if (r < 0.10) shade = -0.26 - rand() * 0.10;       // deep worn plates
      else if (r < 0.22) shade = 0.10 + rand() * 0.08;    // bright fresh plates
      else shade = (rand() - 0.5) * 0.09;                 // wide mid-band scatter
      const v = shade >= 0 ? `rgba(255,255,255,${shade.toFixed(3)})` : `rgba(0,0,0,${(-shade).toFixed(3)})`;
      ctx.fillStyle = v;
      ctx.fillRect(cx * cell, cy * cell, cell, cell);
    }
  }

  // --- 2. Panel-line grid, thin and dark, at the fine cell pitch.
  ctx.strokeStyle = 'rgba(0,0,0,0.38)';
  ctx.lineWidth = 1.4;
  for (let x = 0; x <= size; x += cell) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, size); ctx.stroke();
  }
  for (let y = 0; y <= size; y += cell) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(size, y); ctx.stroke();
  }

  // --- 3. A SECOND, coarser meta-grid (4-6x the fine cell) drawn as
  // slightly heavier lines with its own independent value wash per block
  // — this is what actually sells "mosaic of thousands of plates" rather
  // than "one uniform noisy texture": the eye reads structure at two
  // scales simultaneously, exactly like the reference's hull sections
  // being made of many smaller panels grouped into larger construction
  // blocks.
  const metaCell = cell * (4 + Math.floor(rand() * 3));
  ctx.strokeStyle = 'rgba(0,0,0,0.5)';
  ctx.lineWidth = 2.6;
  for (let x = 0; x <= size; x += metaCell) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, size); ctx.stroke();
  }
  for (let y = 0; y <= size; y += metaCell) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(size, y); ctx.stroke();
  }

  // --- 4. Ambient-occlusion smudges at fine-grid joints — denser than the
  // ship version since there are ~6x more joints to seed from.
  for (let cy = 0; cy <= rows; cy += 2) {
    for (let cx = 0; cx <= cols; cx += 2) {
      if (rand() > 0.45) continue;
      const jx = cx * cell, jy = cy * cell;
      const r = 5 + rand() * 9;
      const grad = ctx.createRadialGradient(jx, jy, 0, jx, jy, r);
      grad.addColorStop(0, `rgba(0,0,0,${(0.20 + rand() * 0.14).toFixed(3)})`);
      grad.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = grad;
      ctx.fillRect(jx - r, jy - r, r * 2, r * 2);
    }
  }

  // --- 5. City-light speckle — hundreds of tiny bright points scattered
  // across the mosaic (baked into the color map, NOT separate meshes),
  // the direct answer to "city-light speckle" in the brief. Two flavors
  // (cool white / warm amber) at low individual alpha so they read as a
  // field of distant lit windows rather than a scatter of bright dots;
  // slightly biased toward panel-line intersections (the natural place
  // for an airlock/viewport on a plated hull) without being locked to
  // them exactly.
  for (let i = 0; i < 900; i++) {
    const gx = Math.floor(rand() * cols), gy = Math.floor(rand() * rows);
    const jitter = cell * 0.5;
    const px = gx * cell + (rand() - 0.5) * jitter + cell * (rand() < 0.5 ? 0 : 1);
    const py = gy * cell + (rand() - 0.5) * jitter + cell * (rand() < 0.5 ? 0 : 1);
    const warm = rand() < 0.35;
    const a = 0.10 + rand() * 0.20;
    ctx.fillStyle = warm ? `rgba(255,214,150,${a.toFixed(3)})` : `rgba(210,228,255,${a.toFixed(3)})`;
    ctx.fillRect(px, py, 1.4, 1.4);
  }

  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  _stationHullCache.set(key, tex);
  return tex;
}

function stationHullMaterial(colorHex, seed, opts = {}) {
  const mat = hullMaterial(colorHex, opts);
  mat.map = stationHullTexture(seed);
  return mat;
}

// ============================================================
// CC STATION — the Command Center flagship (2026-08-25 refit: "change
// that deathstar into the Enterprise", operator directive). Replaces the
// battle-station geometry with a Galaxy-class silhouette (reference:
// D:\GoldenEye\Enterprise_Forward.jpg) built from three.js primitives —
// saucer section (dominant volume, forward), a neck down to the
// engineering/secondary hull, and two nacelles swept up+out on pylons
// with glowing bussard collectors (fore) and blue warp grilles (body).
// The fleet-data plumbing is carried over UNCHANGED: the deflector dish
// on the engineering hull's leading edge is the new "core" (health/AEGIS
// pulse, P/L mood color via StationRig.update — same contract, same
// material handles), and the ship "sometimes fires" its main deflector
// beam forward using the exact same charge/fire state machine that used
// to drive the superlaser (StationRig.update, `sl` return contract
// unchanged: rimMat/flare/flareMat/mainMat/glowMat). outerEdges/
// innerEdges are returned as empty groups so update()'s counter-rotation
// writes stay no-op-safe, same as before.
// ============================================================
function buildStation(seed) {
  const g = new THREE.Group();
  const rand = mulberry32(seed);

  // COMMAND CENTER — an abstract station, deliberately not a ship.
  //
  // This body was a Death Star, then an Enterprise-D (2026-08-25 refit),
  // and is now neither. Both were recognisable craft from someone else's
  // universe sitting on a display that gets shown to investors, which is
  // a distraction from what the node actually represents: the process
  // that holds the pool and arbitrates every reservation. It should read
  // as infrastructure.
  //
  // THE LIGHT BUDGET IS NOT TOUCHED. There is no sun in this scene. The
  // hull is a dark neutral metal, well below the near-white Starfleet
  // hull that preceded it, so it sits comfortably under the same key
  // without any light being raised to show it off (see the three-point
  // rig note near the light setup).
  const hullMat = stationHullMaterial(0x8d949e, seed, { lightness: 0.035, metalness: 0.62, roughness: 0.44 });
  hullMat.envMapIntensity = 0.7;
  const trim = darkTrimMaterial();

  // ---- CENTRAL HUB ----------------------------------------------------
  // An octagonal drum: a low-segment cylinder reads as machined structure
  // rather than an organic sphere, and the flat facets catch the key light
  // as distinct planes instead of a single specular smear.
  const HUB_R = 2.5;
  const hub = new THREE.Mesh(new THREE.CylinderGeometry(HUB_R, HUB_R, 2.2, 8), hullMat);
  g.add(hub);

  // Collar rings top and bottom — hard horizontal lines that give the drum
  // a manufactured edge and stop it reading as a plain barrel.
  for (const y of [1.16, -1.16]) {
    const collar = new THREE.Mesh(new THREE.TorusGeometry(HUB_R * 0.99, 0.10, 6, 24), trim);
    collar.rotation.x = Math.PI / 2;
    collar.position.y = y;
    g.add(collar);
  }

  // Spine through the hub, capped — the axis the rings rotate about, made
  // visible so the rotation below has something to be about.
  // Spine length and cap size set from the RENDERED result, not from the
  // numbers reading sensibly in isolation. At 5.6 long with 0.5 spherical
  // caps it stood 1.7 proud of a 2.2-tall hub at each end and read as a
  // dumbbell — two knobs on a stick — rather than a docking axis. The
  // harness could not catch this: the object graph was correct and every
  // structural assertion passed. Only the screenshot showed it.
  const spine = new THREE.Mesh(new THREE.CylinderGeometry(0.34, 0.34, 3.5, 12), trim);
  g.add(spine);
  for (const y of [1.75, -1.75]) {
    // Flat docking collars, not spheres. A short cylinder terminates the
    // axis instead of bulging off it.
    const cap = new THREE.Mesh(new THREE.CylinderGeometry(0.62, 0.44, 0.34, 12), hullMat);
    cap.position.y = y;
    g.add(cap);
  }

  // ---- RADIAL ARMS ----------------------------------------------------
  // Four box arms out to the ring line. Structure, and they visually tie
  // the hub to the rings so the assembly reads as one object.
  for (let i = 0; i < 4; i++) {
    const a = (i / 4) * Math.PI * 2 + Math.PI / 8;
    const arm = new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.34, 3.1), hullMat);
    arm.position.set(Math.cos(a) * (HUB_R + 1.2), 0, Math.sin(a) * (HUB_R + 1.2));
    arm.rotation.y = -a;
    g.add(arm);
  }

  const dishGroup = new THREE.Group();
  const dishRing = new THREE.Mesh(new THREE.TorusGeometry(0.95, 0.14, 8, 32), trim);
  dishGroup.add(dishRing);
  const dishBowlMat = new THREE.MeshStandardMaterial({ color: 0x2a2f38, metalness: 0.4, roughness: 0.7, side: THREE.DoubleSide });
  const dishBowl = new THREE.Mesh(new THREE.CircleGeometry(0.88, 24), dishBowlMat);
  dishBowl.position.z = -0.05;
  dishGroup.add(dishBowl);

  // Dish emitter = the station "core" — unchanged fleet-mood contract.
  // Recolored amber/gold (deflector-dish canon) instead of the old
  // dish's cool blue-white.
  const coreMat = new THREE.MeshBasicMaterial({
    color: 0xffd98a, transparent: true, opacity: 0.95, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const core = new THREE.Mesh(new THREE.SphereGeometry(0.30, 14, 12), coreMat);
  core.layers.enable(BLOOM_LAYER);
  dishGroup.add(core);
  const coreGlowMat = new THREE.MeshBasicMaterial({
    color: 0xffaa3d, transparent: true, opacity: 0.42, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const coreGlow = new THREE.Mesh(new THREE.SphereGeometry(0.55, 12, 10), coreGlowMat);
  coreGlow.layers.enable(BLOOM_LAYER);
  dishGroup.add(coreGlow);
  const coreHaloMat = new THREE.MeshBasicMaterial({
    color: 0xd4841a, transparent: true, opacity: 0.22, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const coreHalo = new THREE.Mesh(new THREE.SphereGeometry(0.88, 12, 10), coreHaloMat);
  coreHalo.layers.enable(BLOOM_LAYER);
  dishGroup.add(coreHalo);

  // Forward face of the hub. The emitter assembly itself is UNCHANGED —
  // same dish, same core/glow/halo contract StationRig animates, same
  // charge->fire lance. Only where it is mounted moved.
  dishGroup.position.set(0, 0, HUB_R + 0.30);
  g.add(dishGroup);

  // ---- DEFLECTOR BEAM — hidden until StationRig's state machine fires
  // it. Same charge (converging rim beams) -> fire (main lance) sequence,
  // now aimed straight ahead (+Z, the ship's forward vector) instead of
  // the old lance's up-left cheat — a deflector firing forward off the
  // bow reads correctly in this orthographic view without foreshortening
  // to a dot, since forward is not directly into the camera here (the
  // scene view looks down at a steep angle, not dead-on -Z).
  const slGroup = new THREE.Group();
  slGroup.position.copy(dishGroup.position);
  const fireDir = new THREE.Vector3(0, 0.10, 1).normalize();
  slGroup.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), fireDir);
  g.add(slGroup);
  const SL_COLOR = 0x6fb8ff; // deflector beam — cool blue, distinct from the amber dish core
  const slRimMat = new THREE.MeshBasicMaterial({
    color: SL_COLOR, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const focal = new THREE.Vector3(0, 2.2, 0);
  for (let i = 0; i < 8; i++) {
    const a = (i / 8) * Math.PI * 2;
    const from = new THREE.Vector3(Math.cos(a) * 1.0, 0.10, Math.sin(a) * 1.0);
    const segLen = from.distanceTo(focal);
    const seg = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.09, segLen, 5, 1, true), slRimMat);
    seg.position.copy(from.clone().add(focal).multiplyScalar(0.5));
    seg.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), focal.clone().sub(from).normalize());
    seg.layers.enable(BLOOM_LAYER);
    slGroup.add(seg);
  }
  const slFlareMat = new THREE.MeshBasicMaterial({
    color: 0xe8f4ff, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const slFlare = new THREE.Mesh(new THREE.SphereGeometry(0.38, 12, 10), slFlareMat);
  slFlare.position.copy(focal);
  slFlare.layers.enable(BLOOM_LAYER);
  slGroup.add(slFlare);
  const slMainMat = new THREE.MeshBasicMaterial({
    color: 0xffffff, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
  });
  const slMain = new THREE.Mesh(new THREE.CylinderGeometry(0.14, 0.045, 55, 8, 1, true), slMainMat);
  slMain.position.set(0, focal.y + 27.5, 0);
  slMain.layers.enable(BLOOM_LAYER);
  slGroup.add(slMain);
  const slGlowMat = new THREE.MeshBasicMaterial({
    color: SL_COLOR, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
  });
  const slGlow = new THREE.Mesh(new THREE.CylinderGeometry(0.44, 0.14, 55, 10, 1, true), slGlowMat);
  slGlow.position.set(0, focal.y + 27.5, 0);
  slGlow.layers.enable(BLOOM_LAYER);
  slGroup.add(slGlow);
  // ---- COUNTER-ROTATING RINGS ----------------------------------------
  // outerEdges / innerEdges are the two groups StationRig.update() spins
  // (outer +2pi/90s about Z, inner -2pi/60s about Z and Y). On the
  // Enterprise build these were EMPTY GROUPS and every one of those
  // rotation writes was an inert no-op — dead animation code kept alive
  // only so nothing else had to change. Here they carry real geometry
  // again, so the station has actual motion of its own rather than only
  // the gentle sway applied to the whole group.
  const outerEdges = new THREE.Group();
  const outerRing = new THREE.Mesh(new THREE.TorusGeometry(6.2, 0.17, 8, 64), hullMat);
  outerEdges.add(outerRing);
  // Struts riding the outer ring: without them a smooth torus gives the
  // eye nothing to track and the rotation is invisible.
  for (let i = 0; i < 12; i++) {
    const a = (i / 12) * Math.PI * 2;
    const strut = new THREE.Mesh(new THREE.BoxGeometry(0.20, 0.46, 0.20), trim);
    strut.position.set(Math.cos(a) * 6.2, Math.sin(a) * 6.2, 0);
    strut.rotation.z = a;
    outerEdges.add(strut);
  }
  g.add(outerEdges);

  const innerEdges = new THREE.Group();
  const innerRing = new THREE.Mesh(new THREE.TorusGeometry(4.3, 0.12, 8, 48), hullMat);
  // Tilted off the outer ring's plane. Coplanar rings read as one thick
  // ring from this camera angle and the counter-rotation is then invisible
  // — the whole point of putting geometry back on these groups. Confirmed
  // against the rendered frame, not assumed.
  innerRing.rotation.x = 0.42;
  innerEdges.add(innerRing);
  const innerRing2 = new THREE.Mesh(new THREE.TorusGeometry(4.3, 0.09, 8, 48), trim);
  innerRing2.rotation.y = Math.PI / 2;
  innerRing2.rotation.x = -0.30;
  innerEdges.add(innerRing2);
  g.add(innerEdges);

  // ---- RUNNING LIGHTS -------------------------------------------------
  // On the OUTER RING so they travel with its rotation — motion the eye
  // can follow. Modest and non-bloomed, per the light-budget mandate:
  // these are point emitters, not a second key light.
  const dockingLights = [];
  for (let i = 0; i < 10; i++) {
    const a = (i / 10) * Math.PI * 2;
    dockingLights.push(addRunningLight(
      outerEdges, Math.cos(a) * 6.2, Math.sin(a) * 6.2, 0,
      i % 3 === 0 ? 0xffd27a : 0x9fd0ff, 0.075));
  }
  // A few on the hub itself, scattered, so the centre is not dead.
  for (let i = 0; i < 4; i++) {
    const a = rand() * Math.PI * 2;
    addRunningLight(g, Math.cos(a) * HUB_R, (rand() - 0.5) * 1.6, Math.sin(a) * HUB_R,
                    0xcfe4ff, 0.06);
  }

  // hullLength anchors the 2D-body-size -> WebGL-scale mapping (see
  // StationRig.update's targetSpan). The outer ring is this station's
  // dominant dimension, the role the saucer diameter (11.2) and the old
  // sphere diameter (10.4) played before it.
  g.userData.hullLength = 12.4; // outer ring diameter
  return { group: g, core, coreGlow, coreHalo, coreMat, coreGlowMat, coreHaloMat, outerEdges, innerEdges, dockingLights,
    sl: { rimMat: slRimMat, flare: slFlare, flareMat: slFlareMat, mainMat: slMainMat, glowMat: slGlowMat } };
}

// ============================================================
// Mining beam — layered additive geometry, not a flat line.
// Cone (volumetric core) + two thin cylinders (inner hot core,
// outer soft glow) + a particle stream flowing toward the ship.
// ============================================================
// Beam color is driven by TRADE DIRECTION per the spec ("beam color =
// direction, green long / red short"), NOT by the ship's fleet identity
// color — buildBeam() takes an initial color only as a safe default
// before the first TRADE_OPEN; onEvent()/setBeamDirection() below
// repaint it per trade.
const BEAM_LONG_COLOR = 0x30ff6a;
const BEAM_SHORT_COLOR = 0xff3b30;

// Static radial spark-line geometry for the impact flare's third layer
// (hot core + soft halo + spark lines, per item 1d). Built ONCE and shared
// across every ship's beam — updateBeam only mutates rotation/scale/
// opacity on the LineSegments mesh per frame, never geometry, so this is
// free in the hot loop regardless of how many beams are active.
let _sparkGeoCache = null;
function sparkLinesGeometry() {
  if (_sparkGeoCache) return _sparkGeoCache;
  const n = 6;
  const pos = new Float32Array(n * 2 * 3);
  for (let i = 0; i < n; i++) {
    const a = (i / n) * Math.PI * 2;
    const len = 0.55 + (i % 2) * 0.25; // alternate short/long, less uniform burst
    pos[i * 6 + 0] = 0; pos[i * 6 + 1] = 0; pos[i * 6 + 2] = 0;
    pos[i * 6 + 3] = Math.cos(a) * len; pos[i * 6 + 4] = Math.sin(a) * len; pos[i * 6 + 5] = 0;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  _sparkGeoCache = geo;
  return geo;
}

function buildBeam(colorHex, particleCount) {
  const group = new THREE.Group();
  group.visible = false;

  const coreMat = new THREE.MeshBasicMaterial({
    color: colorHex, transparent: true, opacity: 0.85,
    blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
  });
  const glowMat = new THREE.MeshBasicMaterial({
    color: colorHex, transparent: true, opacity: 0.28,
    blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
  });

  // Core/glow beam cylinders are kept at their DEFAULT orientation
  // (axis along local +Y) with NO extra rotation. updateBeam() aligns
  // beam.group's quaternion so local +Y points from ship to target,
  // then scales core/glow along Y to set beam length — an earlier
  // version rotated these meshes 90° onto local Z, which desynced them
  // from the group's Y-axis alignment/scale and was part of why the
  // beam never appeared where expected. Radii bumped up (0.06->0.11,
  // 0.22->0.4) — the original was too thin to read at 24-40px on the
  // 50-inch display even when correctly positioned.
  // SOTA ROUND 4 (2026-07-30): the constant-radius cylinders read as a
  // solid metal ROD in any still frame — screenshot-confirmed as "green
  // sphere with a lollipop stick" at dashboard scale. Energy reads as
  // energy through three cues matter doesn't have: TAPER (flares toward
  // the work end), LONGITUDINAL FADE (dissipates toward the emitter),
  // and MOTION VISIBLE IN A FREEZE-FRAME (discrete pulse rings caught
  // mid-flight, not just a sine on opacity). Local +Y points ship ->
  // target (updateBeam aligns the group quaternion), so radiusTop is
  // the IMPACT end and radiusBottom the EMITTER end.
  const _beamFade = _beamFadeTexture();
  coreMat.alphaMap = _beamFade;
  glowMat.alphaMap = _beamFade;
  const core = new THREE.Mesh(new THREE.CylinderGeometry(0.17, 0.05, 1, 8, 1, true), coreMat);
  core.layers.enable(BLOOM_LAYER); // SOTA upscale — beam core blooms
  group.add(core);

  // outer volumetric glow: wider tapered cone, softer
  const glow = new THREE.Mesh(new THREE.CylinderGeometry(0.55, 0.13, 1, 10, 1, true), glowMat);
  glow.layers.enable(BLOOM_LAYER);
  group.add(glow);

  // traveling pulse rings — 3 thin tori riding the beam axis from
  // emitter to impact. These are what make a STILL frame read as flow:
  // discrete wavefronts caught mid-transit, unambiguous direction.
  const ringMat = new THREE.MeshBasicMaterial({
    color: colorHex, transparent: true, opacity: 0.55,
    blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
  });
  const rings = [];
  for (let ri = 0; ri < 3; ri++) {
    const ring = new THREE.Mesh(new THREE.TorusGeometry(0.3, 0.035, 6, 16), ringMat);
    ring.rotation.x = Math.PI / 2; // torus axis onto local Y (beam axis)
    ring.layers.enable(BLOOM_LAYER);
    group.add(ring);
    rings.push(ring);
  }

  // impact flare at the rock end — layered: hot core + soft halo
  const flareMat = new THREE.MeshBasicMaterial({
    color: colorHex, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const flare = new THREE.Mesh(new THREE.SphereGeometry(0.28, 10, 8), flareMat);
  flare.layers.enable(BLOOM_LAYER);
  group.add(flare);
  const haloMat = new THREE.MeshBasicMaterial({
    color: colorHex, transparent: true, opacity: 0.22, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const flareHalo = new THREE.Mesh(new THREE.SphereGeometry(0.6, 10, 8), haloMat);
  flareHalo.layers.enable(BLOOM_LAYER);
  group.add(flareHalo);

  // radial spark lines at the impact point (item 1d's third flare layer —
  // hot core + soft halo above, sparks here). Static geometry built once
  // module-wide and reused across every ship's beam (sparkLinesGeometry
  // caches after first call) — updateBeam only touches rotation/scale/
  // opacity per frame, never geometry, so this costs nothing in the hot
  // loop.
  const sparkMat = new THREE.LineBasicMaterial({
    color: colorHex, transparent: true, opacity: 0.7,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const sparks = new THREE.LineSegments(sparkLinesGeometry(), sparkMat);
  sparks.layers.enable(BLOOM_LAYER);
  group.add(sparks);

  // emitter glow — small hot point at the ship end so the beam visibly
  // ORIGINATES from the turret instead of materializing mid-space.
  const emitterMat = new THREE.MeshBasicMaterial({
    color: colorHex, transparent: true, opacity: 0.7, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const emitter = new THREE.Mesh(new THREE.SphereGeometry(0.18, 8, 6), emitterMat);
  emitter.layers.enable(BLOOM_LAYER);
  group.add(emitter);

  // extraction particles flowing from impact point back to the ship
  const pGeo = new THREE.BufferGeometry();
  const positions = new Float32Array(particleCount * 3);
  const seeds = new Float32Array(particleCount); // per-particle phase offset
  for (let i = 0; i < particleCount; i++) {
    seeds[i] = Math.random();
  }
  pGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  const pMat = new THREE.PointsMaterial({
    color: colorHex, size: 0.14, transparent: true, opacity: 0.9,
    blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true,
  });
  const points = new THREE.Points(pGeo, pMat);
  points.layers.enable(BLOOM_LAYER);
  group.add(points);

  return { group, core, glow, flare, flareHalo, sparks, rings, emitter, points, seeds,
    coreMat, glowMat, flareMat, haloMat, sparkMat, ringMat, emitterMat, pMat, particleCount, colorHex };
}

// Repaint a beam's four materials to the direction color (LONG=green,
// SHORT=red per spec). Cheap — just a .set() on each material's color,
// no geometry/texture rebuild.
function setBeamDirectionColor(beam, isLong) {
  const c = isLong ? BEAM_LONG_COLOR : BEAM_SHORT_COLOR;
  beam.coreMat.color.set(c);
  beam.glowMat.color.set(c);
  beam.flareMat.color.set(c);
  beam.pMat.color.set(c);
  // ROUND 4 layers — halo, pulse rings, emitter all carry direction color
  if (beam.haloMat) beam.haloMat.color.set(c);
  if (beam.ringMat) beam.ringMat.color.set(c);
  if (beam.emitterMat) beam.emitterMat.color.set(c);
  if (beam.sparkMat) beam.sparkMat.color.set(c);
}

function updateBeam(beam, shipWorldPos, targetWorldPos, t, intensity, radiusScale) {
  const dir = new THREE.Vector3().subVectors(targetWorldPos, shipWorldPos);
  const len = Math.max(0.05, dir.length()); // radius guard equivalent — never zero/negative
  dir.normalize();
  const mid = new THREE.Vector3().addVectors(shipWorldPos, targetWorldPos).multiplyScalar(0.5);
  beam.group.position.copy(mid);
  beam.group.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir);

  // radiusScale ties beam THICKNESS to the ship's current on-screen
  // scale (currentScale * zoom) — the beam is a scene-space object
  // fully independent of rig.group's own scale, so without this it
  // stayed a fixed ~0.11 scene-unit radius even as ships were scaled
  // up 5x+ to match currentSize, making it a near-invisible hairline
  // next to a now-much-bigger hull. Length (Y) is NOT scaled by this —
  // len already comes from real scene-space distance.
  const rs = Math.max(0.6, radiusScale || 1);
  beam.core.scale.set(rs, len, rs);
  beam.glow.scale.set(rs, len, rs);
  // subtle pulse so the beam reads as energy, not a static prop
  const pulse = 0.85 + 0.15 * Math.sin(t * 6.0);
  beam.coreMat.opacity = 0.7 * intensity * pulse;
  beam.glowMat.opacity = 0.22 * intensity;

  // beam.flare is a CHILD of beam.group (which is now positioned at
  // `mid` and rotated so local +Y points along `dir`) — its position
  // must be LOCAL, not the world-space targetWorldPos (that was the
  // same double-offset class of bug as the particle system below:
  // world pos would have been mid + targetWorldPos, sending the flare
  // far off into space instead of sitting at the beam's target end).
  // In local space the target end is simply +len/2 along local Y.
  beam.flare.position.set(0, len / 2, 0);
  const flarePulse = 0.8 + 0.4 * Math.sin(t * 9.0);
  beam.flare.scale.setScalar(flarePulse * intensity * rs);

  // ROUND 4: layered flare halo — breathes out of phase with the hot core
  // so the impact point shimmers instead of strobing as one blob.
  if (beam.flareHalo) {
    beam.flareHalo.position.set(0, len / 2, 0);
    beam.flareHalo.scale.setScalar((0.9 + 0.3 * Math.sin(t * 9.0 + 1.7)) * intensity * rs);
    beam.haloMat.opacity = 0.22 * intensity;
  }

  // Impact flare, third layer (item 1d): radial spark lines, slow
  // rotation + a sharper independent pulse on a different phase than the
  // hot core/halo so the burst doesn't read as one mechanically
  // synchronized blob.
  if (beam.sparks) {
    beam.sparks.position.set(0, len / 2, 0);
    beam.sparks.rotation.z = t * 0.6;
    const sparkPulse = 0.7 + 0.5 * Math.max(0, Math.sin(t * 7.0 + 0.4));
    beam.sparks.scale.setScalar(sparkPulse * intensity * rs);
    beam.sparkMat.opacity = 0.6 * intensity * sparkPulse;
  }

  // ROUND 4: emitter glow pinned to the ship end — beam visibly starts
  // at the turret, not in open space.
  if (beam.emitter) {
    beam.emitter.position.set(0, -len / 2, 0);
    beam.emitter.scale.setScalar((0.85 + 0.25 * Math.sin(t * 7.0)) * rs);
    beam.emitterMat.opacity = 0.7 * intensity;
  }

  // ROUND 4: traveling pulse rings — evenly phase-offset wavefronts
  // marching emitter -> impact. Position/scale mutation only (meshes are
  // created once in buildBeam), so nothing allocates per frame. Rings
  // swell and brighten slightly as they approach the impact end, matching
  // the cone taper so they always hug the beam surface.
  if (beam.rings) {
    for (let ri = 0; ri < beam.rings.length; ri++) {
      const ph = (t * 0.6 + ri / beam.rings.length) % 1; // 0 ship -> 1 impact
      const ring = beam.rings[ri];
      ring.position.set(0, (ph - 0.5) * len, 0);
      const ringR = (0.35 + ph * 0.85) * rs; // track the taper: thin at emitter, wide at impact
      ring.scale.set(ringR, ringR, ringR);
    }
    beam.ringMat.opacity = 0.55 * intensity;
  }

  // Particles: flow from target (rock) to ship (cargo), looping.
  // Computed directly in the beam group's LOCAL frame (along local Y,
  // from -len/2 at the ship end to +len/2 at the target end) rather
  // than in world space + subtracting `mid` — the earlier world-space
  // approach ignored the group's rotation (quaternion aligning local Y
  // to `dir`), so the subtraction only canceled the translation, not
  // the rotation, leaving particles offset by the rotated remainder.
  // That, plus a redundant beam.points.position.copy(mid) that double-
  // applied the group's own translation, is what sent the whole
  // particle stream far from the ship (visible as a disconnected
  // dotted sliver elsewhere on screen instead of a beam at the ship).
  // ROUND 4/3 (item 1c, "double density near the impact end"): particleCount
  // is fixed at construction (buildBeam's BufferAttribute is sized to it),
  // so adding more particles per frame would mean reallocating geometry in
  // the hot loop — not allowed. Instead the existing fixed set is
  // redistributed: warping `along` with a power curve packs more of the
  // SAME particles' time-in-view near phase=1 (impact) than phase=0 (ship),
  // reading as denser activity at the work end without a single new
  // particle or any geometry change.
  const posAttr = beam.points.geometry.getAttribute('position');
  for (let i = 0; i < beam.particleCount; i++) {
    const phase = (beam.seeds[i] + t * 0.35) % 1;
    const alongRaw = 1 - phase; // 0 -> ship end, 1 -> target end
    const along = Math.pow(alongRaw, 0.6); // bias time-in-view toward impact (along=1)
    const yLocal = (along - 0.5) * len; // -len/2 (ship) .. +len/2 (target)
    const jitter = (beam.seeds[i] - 0.5) * 0.18 * rs;
    posAttr.setXYZ(i, jitter, yLocal, jitter * 0.6);
  }
  posAttr.needsUpdate = true;
  beam.points.material.size = 0.14 * rs;
  // points stays at local origin (0,0,0) — it inherits beam.group's
  // position+rotation, so per-particle local coords above are correct
  // without any further offset.
  beam.points.material.opacity = 0.85 * intensity;
}

// ============================================================
// ESCORT WINGS (finding 5, realism overhaul 2026-07-30): each trader rig
// gets two small escort fighters holding loose formation off its flanks —
// "punctuation, not clutter" per spec. Geometry/material built ONCE and
// shared via clone() (three.js Mesh.clone() shares the material reference
// and the geometry reference by default — geometry is never mutated per-
// instance so that's safe, and sharing one material means all escorts in
// the scene are one draw-call-cheap family, no per-ship variation needed
// since these are anonymous wing-mates, not identity-bearing hulls).
// Dark angular wedge — small, cheap, reads as "fighter" purely from
// silhouette (a flattened arrowhead) at the tiny scale escorts render at.
// ============================================================
// ADULT-GRADE REBUILD (2026-07-31): the single flat extruded wedge was
// exactly the "naked primitive" problem at fighter scale — one shape, no
// sub-assembly, no armament read. Template stays cheap (geometry/materials
// built ONCE, shared via clone/instancing across every escort in the scene
// — up to 12 on screen at once, so per-mesh cost still matters here more
// than on the six named traders) but the hull is now composed from a
// canopy hump + two hardpoint nubs + a small intake scoop, all sharing the
// cached geometries/materials so the draw-call profile barely moves.
let _escortGeoCache = null;
let _escortMatCache = null;
let _escortEngineMatCache = null;
let _escortCanopyGeoCache = null, _escortCanopyMatCache = null;
let _escortHardpointGeoCache = null, _escortHardpointMatCache = null;
let _escortIntakeGeoCache = null;
function buildEscortTemplate() {
  if (_escortGeoCache) {
    return {
      geo: _escortGeoCache, mat: _escortMatCache, engineMat: _escortEngineMatCache,
      canopyGeo: _escortCanopyGeoCache, canopyMat: _escortCanopyMatCache,
      hardpointGeo: _escortHardpointGeoCache, hardpointMat: _escortHardpointMatCache,
      intakeGeo: _escortIntakeGeoCache,
    };
  }
  const shape = new THREE.Shape();
  shape.moveTo(0, 1.0);       // nose
  shape.lineTo(0.42, -0.55);  // right wingtip
  shape.lineTo(0.14, -0.4);
  shape.lineTo(0, -0.65);     // tail notch (engine sits here)
  shape.lineTo(-0.14, -0.4);
  shape.lineTo(-0.42, -0.55); // left wingtip
  shape.closePath();
  const geo = new THREE.ExtrudeGeometry(shape, { depth: 0.1, bevelEnabled: false });
  geo.rotateX(Math.PI / 2);
  geo.translate(0, -0.05, 0);
  // Same neutral gunmetal language as the trader hulls (finding 1) — dark,
  // angular, no faction color on the body itself; escorts are anonymous
  // wing-mates, not individually branded.
  const mat = new THREE.MeshStandardMaterial({
    color: new THREE.Color().setHSL(0.6, 0.03, 0.07),
    metalness: 0.6, roughness: 0.62,
    emissive: new THREE.Color(0x050508), emissiveIntensity: 1,
  });
  const engineMat = new THREE.MeshBasicMaterial({
    color: 0xbcd4ff, transparent: true, opacity: 0.85,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  // canopy glint — a tiny flattened dome forward of center, bright/glossy
  // so it catches the key light as a hard highlight point (the "canopy
  // glint" the mandate names explicitly) — cheap low-poly, shared geo/mat
  const canopyGeo = new THREE.SphereGeometry(0.09, 8, 6, 0, Math.PI * 2, 0, Math.PI / 1.9);
  const canopyMat = new THREE.MeshStandardMaterial({
    color: 0x0a1420, metalness: 0.15, roughness: 0.08,
    emissive: 0x0e1b28, emissiveIntensity: 0.5,
  });
  // hardpoint nubs — small angular blocks under each wing, the "armed
  // fighter" read; shared geo/mat, positioned per-side in buildEscort
  const hardpointGeo = new THREE.BoxGeometry(0.06, 0.05, 0.16);
  const hardpointMat = new THREE.MeshStandardMaterial({ color: 0x14141a, metalness: 0.55, roughness: 0.55 });
  // intake scoop — a short open-ended tapered cylinder tucked under the
  // nose, the "this fighter breathes" mechanical-logic cue at fighter scale
  const intakeGeo = new THREE.CylinderGeometry(0.05, 0.07, 0.14, 6, 1, true);
  _escortGeoCache = geo; _escortMatCache = mat; _escortEngineMatCache = engineMat;
  _escortCanopyGeoCache = canopyGeo; _escortCanopyMatCache = canopyMat;
  _escortHardpointGeoCache = hardpointGeo; _escortHardpointMatCache = hardpointMat;
  _escortIntakeGeoCache = intakeGeo;
  return {
    geo, mat, engineMat, canopyGeo, canopyMat, hardpointGeo, hardpointMat, intakeGeo,
  };
}

function buildEscort() {
  const tpl = buildEscortTemplate();
  const group = new THREE.Group();
  const hull = new THREE.Mesh(tpl.geo, tpl.mat);
  group.add(hull);

  // canopy hump — sits just forward of center, breaks the flat wedge top
  // surface and gives the silhouette a "pilot's compartment" bump when
  // viewed from the side, plus the specular glint from above
  const canopy = new THREE.Mesh(tpl.canopyGeo, tpl.canopyMat);
  canopy.scale.set(1, 0.6, 1.3);
  canopy.position.set(0, 0.04, 0.28);
  group.add(canopy);

  // hardpoint nubs — one under each wingtip, small angular blocks that
  // read as ordnance/sensor pods even at 6-10px on screen
  const hpL = new THREE.Mesh(tpl.hardpointGeo, tpl.hardpointMat);
  hpL.position.set(0.3, -0.04, -0.1);
  group.add(hpL);
  const hpR = new THREE.Mesh(tpl.hardpointGeo, tpl.hardpointMat);
  hpR.position.set(-0.3, -0.04, -0.1);
  group.add(hpR);

  // intake scoop tucked under the nose
  const intake = new THREE.Mesh(tpl.intakeGeo, tpl.mat);
  intake.rotation.x = Math.PI / 2;
  intake.position.set(0, -0.05, 0.45);
  group.add(intake);

  // tiny engine glow at the tail notch — the only light this small hull
  // carries, matches the "small dark angular wedge with tiny engine
  // glows" spec line.
  const engineGlow = new THREE.Mesh(new THREE.PlaneGeometry(0.16, 0.16), tpl.engineMat.clone());
  engineGlow.position.set(0, -0.02, -0.66);
  engineGlow.layers.enable(BLOOM_LAYER); // SOTA upscale — escort engine glow blooms
  group.add(engineGlow);
  return { group, engineGlow };
}

// ============================================================
// Ship rig — wraps a hull group with animation state machine
// ============================================================
class ShipRig {
  constructor(id, seed) {
    this.id = id;
    const def = FLEET[id];
    this.def = def;
    const builder = HULL_BUILDERS[def.hull];
    const built = builder(seed);
    this.hullGroup = built.group; // the actual mesh, built nose-forward along local +Z

    // ORIENTATION WRAPPER: every hull was modeled nose-forward along
    // local +Z (the natural axis for CylinderGeometry/ConeGeometry
    // built with rotation.x=PI/2 etc). But the per-frame heading logic
    // in _tick() turns ships by rotating around the Z axis (correct
    // for a 2D-composited top-down-ish view, where "which way the ship
    // points" must be a screen-plane rotation). Rotating a +Z-forward
    // mesh around its own Z axis just spins it in place — the nose
    // never actually turns to face the direction of travel, and
    // anything computed from "forward" (e.g. the mining beam target)
    // ends up disconnected from where the hull visually points.
    // Fix: wrap the hull in an outer group with a one-time -90°
    // X-rotation that remaps local +Z-forward to +Y-forward. _tick()
    // now positions/rotates THIS outer group (this.group), while the
    // hull keeps all its part geometry exactly as authored.
    this.group = new THREE.Group();
    built.group.rotation.x = -Math.PI / 2;
    this.group.add(built.group);
    // userData (beamMount, cargoMesh, hullLength, wingL/R, booms,
    // umbilicals, dish) was set on the hull group by each builder —
    // re-expose it on the outer group so _tick()'s existing
    // rig.group.userData.* lookups keep working unchanged.
    this.group.userData = built.group.userData;

    this.engines = built.engines;
    this.lights = built.lights;

    this.beam = buildBeam(def.color, _quality.beamParticles);

    // behavior state
    this.state = 'idle'; // idle | mining | retracting | dormant
    this.beamIntensity = 0;
    this.cargoGlow = 0; // -1..1 pulses (venting..profit)
    this.cargoGlowDecay = 0;
    this.direction = 0; // OrbNode.direction — market regime (bull/bear/neutral), NOT trade direction
    // tradeDirection is a SEPARATE field from `direction` above — they
    // were originally conflated under one name, which caused a bug:
    // _tick() overwrites `direction` every frame from the node's
    // regime signal, silently stomping the LONG/SHORT value onEvent()
    // set moments earlier, so a beam-color-preserving quality switch
    // (see setQuality) would read the wrong sign. tradeDirection is
    // ONLY ever written by onEvent() and is what beam coloring uses.
    this.tradeDirection = 1; // 1 = LONG (green), -1 = SHORT (red)
    // SOTA ROUND 3 (2026-07-30): the beam MATERIAL is constructed above
    // with the ship's fleet identity color (buildBeam(def.color,...)), not
    // a direction color — tradeDirection defaults to 1 (LONG) but that
    // default was never actually painted onto the beam. A naive
    // "repaint only if tradeDirection changed" guard in syncOpenPositions
    // would therefore silently skip repainting any position that happens
    // to be LONG (tradeDirection already numerically equals the default),
    // leaving its beam stuck on fleet-identity color forever. This flag
    // tracks whether the beam has EVER been painted to a real direction
    // color (by either onEvent's TRADE_OPEN or syncOpenPositions' cold-
    // start reconciliation) so the first paint always happens regardless
    // of whether the derived direction happens to match the numeric
    // default.
    this.beamDirectionPainted = false;
    this.targetWorldPos = new THREE.Vector3();
    this.shipWorldPos = new THREE.Vector3();
    this.lastEventPair = '';
    this.alive = true;
    this.idlePhase = Math.random() * Math.PI * 2;
    this.thrusterPuffTimer = Math.random() * 4;
    this.velocitySmoothed = new THREE.Vector3();
    this.prevPos = null;
    this.scaleTarget = 1;
    this.currentScale = 1;

    // ESCORT WINGS (finding 5): two small fighters per trader rig, loose
    // formation off the flanks. Each escort tracks its OWN lag-filtered
    // position (escort.lagPos) so it visibly trails the rig's motion by a
    // beat rather than being welded on — "piloted, not welded" per spec.
    // Left/right mirrored offsets + a phase seed so the bob/sway isn't
    // synchronized between the pair.
    this.escorts = [
      { ...buildEscort(), side: 1, phase: Math.random() * Math.PI * 2, lagPos: null },
      { ...buildEscort(), side: -1, phase: Math.random() * Math.PI * 2, lagPos: null },
    ];
  }

  setQuality(q) {
    // rebuild beam particle system if quality changed materially
    if (this.beam.particleCount !== q.beamParticles) {
      // beam.group was added directly to the SCENE in attachToScene()
      // (not to this.group/hullGroup), so removing it from this.group
      // here was always a no-op that silently left the old beam.group
      // attached to the scene forever — a leak on every quality switch
      // that changed particle count. Must remove from sceneRef instead.
      const isLong = this.tradeDirection >= 0;
      if (this.sceneRef) this.sceneRef.remove(this.beam.group);
      this.beam = buildBeam(this.def.color, q.beamParticles);
      // Preserve whatever direction color was active — otherwise a
      // mid-mining-run quality switch would silently revert an active
      // beam back to the ship's fleet-identity default color instead
      // of the trade's LONG/SHORT color.
      setBeamDirectionColor(this.beam, isLong);
      this.sceneRef && this.sceneRef.add(this.beam.group);
    }
  }

  attachToScene(scene) {
    this.sceneRef = scene;
    scene.add(this.group);
    scene.add(this.beam.group);
    for (const esc of this.escorts) scene.add(esc.group);
  }

  dispose() {
    if (this.sceneRef) {
      this.sceneRef.remove(this.group);
      this.sceneRef.remove(this.beam.group);
      for (const esc of this.escorts) this.sceneRef.remove(esc.group);
    }
  }
}

// ============================================================
// Station rig — wraps the CC mothership build with slow majestic
// rotation and data-driven core pulse (Task 3, 2026-07-30).
// ============================================================
class StationRig {
  constructor(seed) {
    const built = buildStation(seed);
    this.group = built.group;
    this.core = built.core;
    this.coreGlow = built.coreGlow;
    this.coreHalo = built.coreHalo;
    this.coreMat = built.coreMat;
    this.coreGlowMat = built.coreGlowMat;
    this.coreHaloMat = built.coreHaloMat;
    this.outerEdges = built.outerEdges;
    this.innerEdges = built.innerEdges;
    this.currentScale = 1;
    this.scaleTarget = 1;
    this.sceneRef = null;
    // Superlaser state machine (battle-station rebuild 2026-07-30; ITEM 4b
    // round 4 2026-07-31 removed the idle auto-fire timer entirely — see
    // update()'s 'idle' branch below): idle (waits indefinitely for
    // fireSuperlaser()/a qualifying event, no organic auto-fire) ->
    // charging (rim beams converge, 1.6s) -> firing (main lance, ~1.45s)
    // -> back to idle. this.slTimer is now ONLY a countdown used for the
    // brief charging/firing phase durations' bookkeeping is via slPhase;
    // slTimer is kept at 0 in idle and is meaningful only as "not yet
    // consumed" for fireSuperlaser()'s own idle-state check.
    this.sl = built.sl;
    this.slState = 'idle';
    this.slTimer = 0;
    this.slPhase = 0;
  }

  attachToScene(scene) {
    this.sceneRef = scene;
    scene.add(this.group);
  }

  // meta = {health, eventRate, aegisScore, pnlSign} — same read-only
  // inputs the 2D drawSun()/_drawSunCinematic() centerpiece consumes.
  update(dt, t, node, view, worldW, worldH, meta) {
    if (!node) { this.group.visible = false; return; }
    this.group.visible = true;

    const scenePos = worldToScene(node.x, node.y, view, worldW, worldH);
    const sz = Math.max(1, node.currentSize || 104);
    // Same "hullSpan proportional to 2D body radius" contract ships use
    // (see _tick's targetHullSpan), but stretched further (3.3x vs 2.6x)
    // since the CC hub is explicitly exempt from the general size caps
    // and should read as the scene's centerpiece.
    const targetSpan = sz * 3.3;
    this.scaleTarget = targetSpan / this.group.userData.hullLength;
    this.currentScale += (this.scaleTarget - this.currentScale) * Math.min(1, dt * 4);
    this.group.position.set(scenePos.x, scenePos.y, 0);
    this.group.scale.setScalar(this.currentScale * view.zoom);

    // SLOW MAJESTIC ROTATION — two axes at very different periods so it
    // never looks like it's spinning on a fixed axis. outerEdges/innerEdges
    // are empty groups on the Enterprise geometry (2026-08-25 refit) so
    // their counter-rotation writes below are inert no-ops — kept only so
    // nothing else that references them needs to change.
    // Oscillating sway, not a full revolution (carried over from the
    // battle-station build, still correct here): the dish and both
    // nacelles are fixed in the ship's own local frame (not scattered on a
    // sphere at a random angle like the old dish was), so EVERY emitter —
    // deflector dish, both bussard collectors, both warp grilles — stays
    // on the visible hemisphere through the full +-31 degree yaw sway; a
    // full spin isn't needed to keep them in view, and this way the ship
    // never presents its aft to the camera.
    this.group.rotation.y = Math.sin(t * (Math.PI * 2 / 180)) * 0.55;
    this.group.rotation.x = Math.sin(t * (Math.PI * 2 / 260)) * 0.18;
    this.outerEdges.rotation.z = t * (Math.PI * 2 / 90);
    this.innerEdges.rotation.z = -t * (Math.PI * 2 / 60);
    this.innerEdges.rotation.y = -t * (Math.PI * 2 / 60);

    // CORE PULSE — brightness/rate from real fleet data, not invented.
    const health = meta.health != null ? meta.health : 1;
    const eventRate = meta.eventRate || 0;
    const aegisScore = meta.aegisScore != null ? meta.aegisScore : 0.02;
    // Weave/poll breathing — mirrors drawSun's _weaveBright 10s-cycle
    // pulse (solar_system.js ~line 453-454) so the WebGL core beats in
    // the same rhythm as the 2D version did before it (visual continuity
    // across the fullscreen suppression swap).
    const weavePhase = (t % 10) / 10;
    const weaveBright = Math.max(0, 1 - weavePhase * 2.2);
    // Event-rate adds a faster shimmer on top — more fleet activity
    // (trades, signals, alerts) = more restless core, same idea as the
    // 2D data-surge arcs whose spawn chance scales with AEGIS score and
    // whose frequency reads as "the core is busy".
    const activityPulse = 0.5 + 0.5 * Math.sin(t * (2 + eventRate * 0.8));
    const coreBrightness = 0.55 + health * 0.25 + weaveBright * 0.25 + activityPulse * 0.15 * Math.min(1, eventRate / 3);
    this.coreMat.opacity = Math.min(1, coreBrightness);
    const coreScalePulse = 1 + weaveBright * 0.16 + Math.sin(t * 1.5) * 0.05;
    this.core.scale.setScalar(coreScalePulse);
    this.coreGlow.scale.setScalar(coreScalePulse * (1.15 + aegisScore * 0.35));
    this.coreGlowMat.opacity = 0.3 + aegisScore * 0.15 + weaveBright * 0.15;
    this.coreHalo.scale.setScalar(coreScalePulse * (1.05 + weaveBright * 0.2));
    this.coreHaloMat.opacity = 0.08 + weaveBright * 0.08 + Math.min(1, eventRate / 3) * 0.05;

    // PnL-SIGN COLOR BIAS — net-profitable fleet biases the core/glow
    // toward green, net-negative toward red, flat/unknown stays the
    // native cool blue. A gentle tint, not a full recolor — the station
    // should still read as "the CC hub", just with a mood.
    const pnlSign = meta.pnlSign || 0;
    if (pnlSign > 0) {
      this.coreMat.color.setRGB(0.78, 0.98, 0.88);
      this.coreGlowMat.color.setHex(0x30ff8a);
      this.coreHaloMat.color.setHex(0x1a8a4a);
    } else if (pnlSign < 0) {
      this.coreMat.color.setRGB(0.98, 0.82, 0.80);
      this.coreGlowMat.color.setHex(0xff4a3b);
      this.coreHaloMat.color.setHex(0x8a2a1a);
    } else {
      this.coreMat.color.setHex(0xe1f0ff);
      this.coreGlowMat.color.setHex(0x5a9aff);
      this.coreHaloMat.color.setHex(0x3a6fd0);
    }

    // SUPERLASER — ITEM 4b (round 4, 2026-07-31): "must only shoot out for
    // a special event" — the old 40-90s idle countdown that auto-fired
    // regardless of fleet activity is REMOVED. 'idle' is now a pure wait
    // state: nothing here transitions it. The only two ways to leave idle
    // are (a) window.Armada.fireSuperlaser() — the manual trigger, sets
    // slState directly (see the Armada object below) — or (b) a
    // qualifying TRADE_CLOSE event routed through Armada.onEvent (pnl >=
    // +$25, see onEvent below), which does the same slState flip. Once
    // out of idle the charge->fire->idle sequence is unchanged; opacity/
    // scale mutation on meshes built once in buildStation, nothing
    // allocates here. Sequence reads in stills: converging rim beams while
    // charging (ITEM 4a: bumped 0.85->1.0 peak — "make them slightly more
    // prominent, they're now the visual story of the charge"), then a
    // thin needle lance with a flickering focal flare (ITEM 4a: geometry
    // thinned at buildStation's slMain/slGlow definitions).
    const sl = this.sl;
    if (sl) {
      if (this.slState === 'charging') {
        this.slPhase += dt;
        const k = Math.min(1, this.slPhase / 1.6);
        sl.rimMat.opacity = 1.0 * k;
        sl.flareMat.opacity = 0.9 * k * (0.7 + 0.3 * Math.sin(t * 20));
        sl.flare.scale.setScalar(0.6 + k * 0.8);
        if (this.slPhase >= 1.6) { this.slState = 'firing'; this.slPhase = 0; }
      } else if (this.slState === 'firing') {
        this.slPhase += dt;
        const kIn = Math.min(1, this.slPhase / 0.12);
        const kOut = this.slPhase > 2.2 ? Math.max(0, 1 - (this.slPhase - 2.2) / 0.55) : 1;
        const flicker = 0.85 + 0.15 * Math.sin(t * 40);
        sl.mainMat.opacity = 0.9 * kIn * kOut * flicker;
        sl.glowMat.opacity = 0.3 * kIn * kOut;
        sl.rimMat.opacity = 0.6 * kOut;
        sl.flareMat.opacity = 1.0 * kOut;
        sl.flare.scale.setScalar(1.5 * flicker);
        if (this.slPhase >= 2.75 + (this.slHold || 0)) {
          this.slState = 'idle';
          this.slHold = 0; // manual hold never carries into the next fire
          sl.mainMat.opacity = 0; sl.glowMat.opacity = 0;
          sl.rimMat.opacity = 0; sl.flareMat.opacity = 0;
          sl.flare.scale.setScalar(0.6);
        }
      }
      // 'idle': intentionally no-op — see comment above.
    }
  }

  dispose() {
    if (this.sceneRef) this.sceneRef.remove(this.group);
  }
}

// ============================================================
// World-space helpers: map dashboard world px -> a fixed-scale
// three.js world using an orthographic camera whose visible area
// exactly equals the CSS pixel box of the container each frame.
// This keeps 1 dashboard px == 1 three.js unit at zoom=1, so ship
// hull sizes (defined in "px units") line up with currentSize.
// ============================================================
function updateCamera(cssW, cssH) {
  const halfW = cssW / 2, halfH = cssH / 2;
  if (!_camera) {
    _camera = new THREE.OrthographicCamera(-halfW, halfW, halfH, -halfH, -1000, 1000);
    _camera.position.set(0, 0, 100);
    _camera.lookAt(0, 0, 0);
  } else {
    _camera.left = -halfW; _camera.right = halfW;
    _camera.top = halfH; _camera.bottom = -halfH;
    _camera.updateProjectionMatrix();
  }
}

// Dashboard world (top-left origin, y-down) -> three.js scene space
// (center origin, y-up), matching the screen transform:
//   screenX = worldX*zoom + panX ; screenY = worldY*zoom + panY
// Our camera is sized to CSS box with (0,0) at its CENTER, so:
//   sceneX = screenX - cssW/2
//   sceneY = cssH/2 - screenY   (flip Y: canvas Y-down -> GL Y-up)
function worldToScene(wx, wy, view, cssW, cssH) {
  const screenX = wx * view.zoom + view.panX;
  const screenY = wy * view.zoom + view.panY;
  return {
    x: screenX - cssW / 2,
    y: cssH / 2 - screenY,
  };
}

// ============================================================
// BLOOM PIPELINE (SOTA upscale, 2026-08-18)
// ============================================================
// Selective bloom on emissive-only geometry (engine glow/plume, beam
// core/glow/flare/halo/spark, station core/coreGlow/coreHalo, superlaser
// rim/flare/main/glow, escort engine glow) — everything currently rendering
// as flat emissive color with zero light spill into the surrounding dark
// space canvas. This is the single biggest legibility+drama lever available
// without touching per-ship geometry: a glowing engine or a firing
// superlaser that visibly blooms reads as "real light source," a flat
// emissive quad reads as "colored sticker."
//
// COST MODEL (why this is safe on a modest GPU): the bright pass restricts
// the CAMERA to BLOOM_LAYER, not a brightness threshold shader — objects
// not on that layer are never submitted to the GPU at all on that pass, so
// the ~500-mesh scene collapses to just the handful of glow/beam/core
// meshes for that draw. The blur+composite passes run at resScale (0.5 on
// high, i.e. quarter-area) of the already-pixelRatio-capped canvas, so
// total added fragment work is bounded by SCREEN AREA, not scene
// complexity — it does not get more expensive as more ships/escorts are
// added or as hull mesh counts grow.
//
// Technique: bright-pass (camera.layers restricted) -> N-pass separable
// (horizontal+vertical) box blur at half-res -> additive composite over
// the normal full-scene render. No addon files (UnrealBloomPass etc. live
// in three/examples/jsm/postprocessing, NOT vendored here — pulling them in
// would mean new vendored files + importmap changes, more invasive than
// this self-contained approach). Built entirely from core THREE primitives
// already imported (WebGLRenderTarget, ShaderMaterial, OrthographicCamera).
class BloomPipeline {
  constructor(renderer, width, height, resScale) {
    this.renderer = renderer;
    this.resScale = resScale;
    this.width = Math.max(2, Math.round(width * resScale));
    this.height = Math.max(2, Math.round(height * resScale));

    const rtOpts = {
      minFilter: THREE.LinearFilter,
      magFilter: THREE.LinearFilter,
      format: THREE.RGBAFormat,
      // HalfFloat avoids banding on the additive composite of a handful of
      // very bright emissive sources — cheap on a discrete GPU, and the
      // targets are quarter-res so the memory/bandwidth cost is trivial.
      type: THREE.HalfFloatType,
      depthBuffer: false,
      stencilBuffer: false,
    };
    this.rtBright = new THREE.WebGLRenderTarget(this.width, this.height, rtOpts);
    this.rtBlurA = new THREE.WebGLRenderTarget(this.width, this.height, rtOpts);
    this.rtBlurB = new THREE.WebGLRenderTarget(this.width, this.height, rtOpts);

    // Full-screen-quad plumbing: one shared ortho camera + a single
    // reusable PlaneGeometry, both used for every blur/composite step by
    // swapping the material's uniforms.texture and the render target.
    this.quadCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    this.quadScene = new THREE.Scene();
    const quadGeo = new THREE.PlaneGeometry(2, 2);

    this.blurMat = new THREE.ShaderMaterial({
      uniforms: {
        tDiffuse: { value: null },
        direction: { value: new THREE.Vector2(1, 0) },
        texel: { value: new THREE.Vector2(1 / this.width, 1 / this.height) },
      },
      vertexShader: `
        varying vec2 vUv;
        void main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }
      `,
      // 9-tap separable Gaussian, run once horizontal + once vertical per
      // iteration. Two iterations (high quality) = 4 texture-sampling
      // passes total at quarter-res — cheap relative to the main scene pass.
      fragmentShader: `
        varying vec2 vUv;
        uniform sampler2D tDiffuse;
        uniform vec2 direction;
        uniform vec2 texel;
        void main() {
          vec2 uv = vUv;
          vec4 sum = vec4(0.0);
          float weights[5];
          weights[0] = 0.227027; weights[1] = 0.1945946; weights[2] = 0.1216216;
          weights[3] = 0.054054; weights[4] = 0.016216;
          sum += texture2D(tDiffuse, uv) * weights[0];
          for (int i = 1; i < 5; i++) {
            vec2 off = direction * texel * float(i) * 1.5;
            sum += texture2D(tDiffuse, uv + off) * weights[i];
            sum += texture2D(tDiffuse, uv - off) * weights[i];
          }
          gl_FragColor = sum;
        }
      `,
      depthTest: false,
      depthWrite: false,
    });
    this.blurQuad = new THREE.Mesh(quadGeo, this.blurMat);
    this.quadScene.add(this.blurQuad);

    this.compositeMat = new THREE.ShaderMaterial({
      uniforms: {
        tBloom: { value: null },
        intensity: { value: 1.15 },
      },
      vertexShader: `
        varying vec2 vUv;
        void main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }
      `,
      fragmentShader: `
        varying vec2 vUv;
        uniform sampler2D tBloom;
        uniform float intensity;
        void main() {
          vec4 b = texture2D(tBloom, vUv);
          gl_FragColor = vec4(b.rgb * intensity, b.a);
        }
      `,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthTest: false,
      depthWrite: false,
    });
    this.compositeQuad = new THREE.Mesh(quadGeo, this.compositeMat);
  }

  setSize(width, height) {
    this.width = Math.max(2, Math.round(width * this.resScale));
    this.height = Math.max(2, Math.round(height * this.resScale));
    this.rtBright.setSize(this.width, this.height);
    this.rtBlurA.setSize(this.width, this.height);
    this.rtBlurB.setSize(this.width, this.height);
    this.blurMat.uniforms.texel.value.set(1 / this.width, 1 / this.height);
  }

  // Renders bloom-eligible geometry only (camera restricted to BLOOM_LAYER)
  // into rtBright, blurs it `iterations` times into rtBlurA/B ping-pong,
  // then additively composites rtBlurA onto whatever is CURRENTLY bound as
  // the render target (the caller is expected to have already rendered the
  // full scene there first). Restores renderer state (target, autoClear,
  // camera.layers) before returning.
  render(scene, camera, iterations) {
    const renderer = this.renderer;
    const prevTarget = renderer.getRenderTarget();
    const prevAutoClear = renderer.autoClear;
    const prevMask = camera.layers.mask;

    // 1. Bright pass — camera sees ONLY BLOOM_LAYER objects. This is what
    // keeps the pass cheap: everything else in the ~500-mesh scene is
    // skipped at the GPU submission level, not just shaded transparent.
    camera.layers.set(BLOOM_LAYER);
    renderer.setRenderTarget(this.rtBright);
    renderer.autoClear = true;
    renderer.setClearColor(0x000000, 0);
    renderer.clear(true, true, true);
    renderer.render(scene, camera);

    // 2. Separable blur, ping-ponging between rtBlurA/B.
    let readRT = this.rtBright;
    let writeRT = this.rtBlurA;
    for (let i = 0; i < iterations; i++) {
      this.blurMat.uniforms.tDiffuse.value = readRT.texture;
      this.blurMat.uniforms.direction.value.set(1, 0);
      renderer.setRenderTarget(writeRT);
      renderer.clear(true, true, true);
      renderer.render(this.quadScene, this.quadCamera);

      readRT = writeRT;
      writeRT = (writeRT === this.rtBlurA) ? this.rtBlurB : this.rtBlurA;
      this.blurMat.uniforms.tDiffuse.value = readRT.texture;
      this.blurMat.uniforms.direction.value.set(0, 1);
      renderer.setRenderTarget(writeRT);
      renderer.clear(true, true, true);
      renderer.render(this.quadScene, this.quadCamera);

      readRT = writeRT;
      writeRT = (writeRT === this.rtBlurA) ? this.rtBlurB : this.rtBlurA;
    }

    // 3. Additive composite onto whatever target was bound before this
    // call (the caller's already-rendered full scene — usually null, i.e.
    // the canvas itself).
    this.compositeMat.uniforms.tBloom.value = readRT.texture;
    renderer.setRenderTarget(prevTarget);
    renderer.autoClear = false;
    this.quadScene.remove(this.blurQuad);
    this.quadScene.add(this.compositeQuad);
    renderer.render(this.quadScene, this.quadCamera);
    this.quadScene.remove(this.compositeQuad);
    this.quadScene.add(this.blurQuad);

    // Restore renderer/camera state exactly as found.
    renderer.autoClear = prevAutoClear;
    camera.layers.mask = prevMask;
    renderer.setRenderTarget(prevTarget);
  }

  dispose() {
    this.rtBright.dispose();
    this.rtBlurA.dispose();
    this.rtBlurB.dispose();
    this.blurMat.dispose();
    this.compositeMat.dispose();
    this.blurQuad.geometry.dispose();
    // compositeQuad shares the same geometry instance as blurQuad
    // (both built from the single `quadGeo` above) — do not dispose twice.
  }
}

// ============================================================
// Public API
// ============================================================
const Armada = {
  _initialized: false,

  init(container, opts = {}) {
    if (_renderer) this.dispose();
    _container = container;
    _quality = QUALITY[opts.quality] || QUALITY.high;
    _qualityLevel = opts.quality || 'high';

    _canvas = document.createElement('canvas');
    _canvas.id = opts.canvasId || 'armadaCanvas';
    _canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;pointer-events:none;';
    container.appendChild(_canvas);

    _renderer = new THREE.WebGLRenderer({ canvas: _canvas, alpha: true, antialias: true, powerPreference: 'high-performance' });
    _renderer.setClearColor(0x000000, 0);
    _renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, _quality.pixelRatioCap));
    // ACES tone mapping + explicit exposure. Without this, three.js r178's
    // physically-based light units (lux-scale DirectionalLight intensity)
    // combined with low-metalness materials render WAY darker than the
    // numbers suggest — this was the root cause of ships (esp. TurtleSue's
    // sphere and Confluence's drum) rendering as near-solid-black discs.
    _renderer.toneMapping = THREE.ACESFilmicToneMapping;
    // Lifted 1.35 -> 1.45 to partly offset the key-light cut above. Only
    // PARTLY: the scene is meant to end up darker, and raising exposure to
    // fully compensate would undo the fix while pretending to keep it.
    _renderer.toneMappingExposure = 1.45;
    _renderer.outputColorSpace = THREE.SRGBColorSpace;

    _scene = new THREE.Scene();
    _clock = new THREE.Clock();

    const rect = container.getBoundingClientRect();
    _worldW = rect.width || 900;
    _worldH = rect.height || 600;
    updateCamera(_worldW, _worldH);
    _renderer.setSize(_worldW, _worldH, false);

    // Generated environment map — gives MeshStandardMaterial something
    // to reflect. Without this, metalness>0 surfaces sample black (no
    // envmap = no specular contribution beyond the tiny direct-light
    // highlight), which reads as "unlit black sphere" exactly like the
    // failure seen in round 1. A cheap procedural gradient env is enough
    // to keep hulls looking like lit metal at 24-40px, no HDRI asset needed.
    _scene.environment = buildProceduralEnvMap(_renderer);

    // UNIFIED KEY LIGHT PASS (2026-07-30, finding 2 — realism overhaul):
    // the prior grimdark pass already cut ambient/fill hard, but the key
    // was a cool blue-white (0xdbe6ff) — this rework switches it to a true
    // WARM-WHITE sun color (matches the 2D scene's star side, upper-left-
    // front) since a cool key was fighting the "sunlit metal" read and
    // contributing to the flat/synthetic verdict. One light dominates
    // completely; everything else exists ONLY to keep the shadow
    // hemisphere from crushing to pure black, never to add a second
    // readable light direction. No hull material carries emissive-as-
    // color — emissive is reserved for engine glow/running lights/windows/
    // beams (see hullMaterial's near-black emissive floor) — so every
    // ship genuinely has a lit side and a shadow side driven by this key.
    // THE LIGHT BUDGET (2026-08-18). This was 0xfff2df at 4.6 -- warm WHITE
    // at sunlight intensity, 7.7x the rim and 15x the fill. A three-point
    // studio rig, and the reason the operator said "it looks like each
    // object has a lamp pointed at it... there is no sun so it should be
    // way darker".
    //
    // There is no star in this scene. sunX/sunY throughout the 2D layer is
    // CC's position; CC was the Death Star when this budget was calibrated
    // and is now the Enterprise (2026-08-25 refit, buildStation) -- the
    // "not a G-type star" reasoning is unchanged, only the hull under it.
    // What actually emits here is: starlight (weak, omnidirectional), each
    // body's OWN emissive surfaces (engine plumes, the mining beam, the
    // deflector dish/beam formerly called the superlaser, bussard
    // collectors, warp grilles, running lights -- all of which bloom via
    // BLOOM_LAYER), and CC's own glow, which is not a G-type star either.
    //
    // So the key survives -- the scene still needs a modelling cue and CC
    // does emit -- but at 30% strength and recoloured from white sunlight
    // to that glow. THIS BUDGET IS NOT CHANGED BY THE 2026-08-25 REFIT: a
    // white hull reads brighter than gray under the same lights, and that
    // is compensated in the hull material (see buildStation), never here.
    // The hemisphere fill rises to carry the starlight
    // floor so hulls stay legible in shadow: deep space is dark, not
    // invisible. Matches _LIGHT_KEY 0.30 / _LIGHT_AMBIENT 0.16 in
    // solar_system.js and _drawMoonOrb in command_center_v4.html, so all
    // three renderers agree about how much light exists.
    // CALIBRATED, not guessed: key 2.2 / rim 0.70 / hemi 0.36 / exposure
    // 1.45 gives a lit:shadow ratio of 4.0:1, matching the 2D star and
    // moon layers at 4.2-4.5:1 -- one scene, one physics. Overall lit
    // level drops 44% from the old rig (6.64 -> 3.72 relative). A first
    // pass at key 1.4 was REJECTED: it landed at 2.4:1, flatter than the
    // 2D layers, which would have made the WebGL bodies look washed out
    // next to the canvas ones.
    const key = new THREE.DirectionalLight(0xe8c88f, 2.2);
    key.position.set(-40, 60, 80);
    _scene.add(key);

    // Cool rim/back light — thin edge light from behind-below so a hull's
    // AWAY-from-key silhouette edge still separates from near-black space
    // instead of vanishing into it. Deliberately cool (contrasts the warm
    // key) since a cool rim against a warm key is the classic cue that
    // reads as "real light in a real scene" rather than "flat toy" — kept
    // dim enough that it never functions as a second front-facing key.
    // Raised 0.6 -> 0.70 alongside the key cut. This is the edge that keeps
    // a hull's away-from-key silhouette from vanishing into near-black
    // space, and with the key at 30% it carries proportionally more of the
    // read. Still well below the key, so it never becomes a second front
    // light.
    const rim = new THREE.DirectionalLight(0x4d6fff, 0.70);
    rim.position.set(50, -30, -60);
    _scene.add(rim);

    // Dim headlight-ish fill from near the camera — kept ONLY so a flat
    // surface normal to the view axis (e.g. Confluence's cylinder
    // end-cap) never goes to a 100%-unlit black disc. Stays far below key
    // strength so it never washes out the shadow side the pass is built
    // around.
    const fill = new THREE.DirectionalLight(0x8fa0d0, 0.3);
    fill.position.set(0, 10, 150);
    _scene.add(fill);

    // Hemisphere fill replaces the old flat AmbientLight — sky/ground
    // split (cool-blue "sky" above, near-black "ground" below) gives the
    // low-level fill a sense of DIRECTION even at minimum strength, which
    // a normal-agnostic AmbientLight can never do. This is what keeps the
    // terminator (lit vs shadow side) reading as real environmental light
    // rather than a light source glued to the camera. Intensity stays low
    // — this must never compete with the key.
    // STARLIGHT FLOOR. Raised 0.28 -> 0.36 to absorb what the key gave up.
    // This is the one physically honest ambient in the scene -- a real sky
    // full of stars, weak and omnidirectional -- and with no sun it is most
    // of what a hull in shadow actually receives.
    const hemi = new THREE.HemisphereLight(0x4a5a78, 0x08080a, 0.36);
    _scene.add(hemi);

    // Build the six trader ships
    _ships = {};
    let seed = 1000;
    for (const id of TRADER_IDS) {
      const rig = new ShipRig(id, seed);
      rig.attachToScene(_scene);
      _ships[id] = rig;
      seed += 137;
    }

    // Build the CC mothership station (Task 3, 2026-07-30)
    _station = new StationRig(9001);
    _station.attachToScene(_scene);

    // Bloom pipeline (SOTA upscale, 2026-08-18) — built any time the
    // current quality tier wants it; setQuality() below also owns
    // enabling/disabling this without a full re-init.
    if (_quality.bloom && _quality.bloom.enabled) {
      _bloom = new BloomPipeline(_renderer, _worldW, _worldH, _quality.bloom.resScale);
    }

    _disposed = false;
    this._initialized = true;

    const resizeObserver = new ResizeObserver(() => this._handleResize());
    resizeObserver.observe(container);
    this._resizeObserver = resizeObserver;

    this._startLoop();

    return {
      scene: _scene, camera: _camera, renderer: _renderer, canvas: _canvas, ships: _ships,
    };
  },

  _handleResize() {
    if (!_container || !_renderer) return;
    const rect = _container.getBoundingClientRect();
    if (rect.width < 4 || rect.height < 4) return;
    _worldW = rect.width; _worldH = rect.height;
    updateCamera(_worldW, _worldH);
    _renderer.setSize(_worldW, _worldH, false);
    if (_bloom) _bloom.setSize(_worldW, _worldH);
  },

  setQuality(level) {
    const q = QUALITY[level];
    if (!q) return;
    _quality = q;
    _qualityLevel = level;
    _renderer && _renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, q.pixelRatioCap));
    for (const id in _ships) _ships[id].setQuality(q);

    // Bloom on/off + resolution follow the quality tier. Rebuilt rather
    // than mutated in place when the enabled state or resScale changes —
    // render targets are cheap to recreate (quarter-res, only 3 of them)
    // and this guarantees no stale-size target survives a tier switch.
    const wantBloom = !!(q.bloom && q.bloom.enabled);
    const curResScale = _bloom ? _bloom.resScale : null;
    if (!wantBloom && _bloom) {
      _bloom.dispose();
      _bloom = null;
    } else if (wantBloom && (!_bloom || curResScale !== q.bloom.resScale)) {
      if (_bloom) _bloom.dispose();
      _bloom = _renderer ? new BloomPipeline(_renderer, _worldW, _worldH, q.bloom.resScale) : null;
    }
  },

  getQuality() { return _qualityLevel; },

  syncFromNodes(nodesLike, view, ccMeta) {
    if (!_scene || !nodesLike) return;
    _lastView = view || _lastView;
    for (const id of TRADER_IDS) {
      const rig = _ships[id];
      const node = nodesLike[id];
      if (!rig || !node) continue;
      rig._pendingNode = node;
    }
    // CC station (Task 3): position/size come from the same _orbNodes.cc
    // entry the 2D hub reads; pulse data comes from the optional third
    // arg (health/eventRate/aegisScore/pnlSign — see command_center_v4.html
    // _orbArmadaSync for the exact read sites this mirrors).
    _pendingCCNode = nodesLike.cc || null;
    if (ccMeta) _lastCCMeta = ccMeta;
  },

  // True once the station mesh exists AND has received at least one real
  // CC node sync — the host page gates its 2D-suppression on this so it
  // never hides the 2D hub before the WebGL replacement has any position
  // to render at (avoids a one-frame "CC vanishes" flash on cold load).
  get hasStation() {
    return !!(_station && _pendingCCNode);
  },

  // Manually trigger the station's superlaser sequence (charge -> fire).
  // Used for visual verification and available from the console:
  // window.Armada.fireSuperlaser()
  // Optional holdSeconds extends the burn beyond the default ~2.75s —
  // handy for demos on the big screen (and for catching it on camera).
  fireSuperlaser(holdSeconds) {
    // ITEM 4b (round 4): update()'s 'idle' branch no longer decrements a
    // timer (the auto-fire countdown is gone) — this now transitions the
    // state machine directly, the same way the TRADE_CLOSE big-win path
    // in onEvent() below does.
    if (_station && _station.slState === 'idle') {
      _station.slState = 'charging';
      _station.slPhase = 0;
      _station.slHold = Math.max(0, Number(holdSeconds) || 0);
    }
  },

  // Read-only view of the superlaser state machine, for debugging.
  get superlaserState() {
    return _station ? {
      state: _station.slState, timer: _station.slTimer, phase: _station.slPhase,
      hasSl: !!_station.sl,
      mainOpacity: _station.sl ? _station.sl.mainMat.opacity : null,
    } : null;
  },

  onEvent(evt) {
    if (!evt || !evt.type) return;
    const type = String(evt.type);
    const botId = String(evt.bot_id || evt.source || '').toLowerCase();
    const data = evt.data || {};

    // ITEM 4b (round 4, 2026-07-31): station superlaser fires ONLY on
    // this qualifying event — a real trade closing at +$25 or better,
    // fleet-wide (not scoped to any one bot, so this check runs BEFORE
    // the `rig` lookup/early-return below, which only exists for the 6
    // trader ships — the station is independent of any single ship rig).
    // No idle auto-fire timer exists anymore; this call plus the manual
    // window.Armada.fireSuperlaser() are the only two ways the sequence
    // starts (see StationRig.update's now-inert 'idle' branch).
    if (/TRADE_CLOSE/.test(type)) {
      const closePnl = Number(data.pnl) || 0;
      if (closePnl >= 25 && _station && _station.slState === 'idle') {
        _station.slState = 'charging';
        _station.slPhase = 0;
        _station.slHold = 0;
      }
    }
    // SUPERLASER_DEMO (2026-07-31): ops-side remote trigger. Published to
    // the event bus (POST /api/events/publish) it fires the lance on EVERY
    // connected dashboard — used for demos without faking a TRADE_CLOSE
    // (which would pollute trade history/expectancy). data.hold extends
    // the burn like fireSuperlaser(hold).
    if (/SUPERLASER_DEMO/.test(type)) {
      if (_station && _station.slState === 'idle') {
        _station.slState = 'charging';
        _station.slPhase = 0;
        _station.slHold = Math.max(0, Number(data.hold) || 0);
      }
    }

    const rig = _ships[botId];
    if (!rig) return;

    if (/TRADE_OPEN/.test(type)) {
      rig.state = 'mining';
      rig.lastEventPair = data.pair || '';
      const isShort = /SHORT/i.test(data.direction || 'LONG');
      rig.tradeDirection = isShort ? -1 : 1;
      rig.beamDirectionPainted = true;
      // Beam color = trade direction (green LONG / red SHORT) per spec —
      // NOT the ship's fleet identity color. Repainted on every open so
      // a ship that flips direction between trades gets the right color.
      setBeamDirectionColor(rig.beam, !isShort);
    } else if (/TRADE_CLOSE/.test(type)) {
      const pnl = data.pnl || 0;
      rig.state = 'retracting';
      // SOTA pass (2026-07-30, finding 6): was a flat +-1 (win/loss only,
      // every close read identically regardless of size) — the spec asks
      // for "cargo glow proportional to P/L". $40 is a soft normalization
      // point (a strong single-trade result for this fleet's pool size,
      // not a hard cap elsewhere in the codebase) picked so a typical
      // small win/loss still visibly glows rather than reading as barely-lit.
      const pnlMag = Math.min(1, Math.abs(pnl) / 40);
      rig.cargoGlow = pnl >= 0 ? Math.max(0.35, pnlMag) : -Math.max(0.35, pnlMag);
      rig.cargoGlowDecay = 1.0;
      rig.cargoGlowMag = pnlMag;
    }
  },

  // Cold-start / reconnect reconciliation (2026-07-30 fix): onEvent() above
  // ONLY flips a ship to 'mining' on a live TRADE_OPEN SSE event arriving
  // WHILE this page is already loaded and listening. A position that was
  // already open before that (page load, reload, or a missed/dropped SSE
  // message) never fires that event here, so its beam silently never
  // lights — the ship sits in 'idle' forever even though the bot has a
  // real open position. This reconciles ship state against ground-truth
  // reservation data (the host page's ovData.portfolio.reservations,
  // already polled independently on its own cadence — no new fetch added
  // here) every sync tick. Idempotent and side-effect-free by design:
  // unlike onEvent() it does NOT play a sound/xenolanguage transmission
  // (that must stay tied to the real moment a trade opens/closes, not to
  // a periodic poll). Only drives the idle<->mining transition PLUS the
  // beam direction color (see below) — no other rig state.
  //
  // SOTA ROUND 3 FIX (2026-07-30, item 1, "default-color trap"): this
  // function previously did NOT touch tradeDirection/beam color at all,
  // reasoning "direction isn't known from a bare reservation" — that
  // reasoning was WRONG. command_center.py's reservation dicts DO carry a
  // "direction" field ("LONG"/"SHORT", see command_center.py ~line 3612
  // `"direction": data["direction"]`), it was simply never read here. Net
  // effect: any position that was already open before page load (or before
  // the SSE listener attached) kept its beam on the ship's fleet-identity
  // default color forever, since only a live TRADE_OPEN event (onEvent
  // above) ever called setBeamDirectionColor. At a glance this looked
  // exactly like "the rod/beam is the wrong/default color" — e.g.
  // TurtleSue (fleet color mint) showing a pale mint beam instead of
  // LONG-green for its live UNI/USD position. Fixed by deriving direction
  // per bot from the same reservation array already being scanned (last
  // reservation seen per bot wins on directional conflict — cheap, no
  // extra pass, and matches the existing "one beam per bot regardless of
  // reservation count" simplification already documented below) and
  // repainting via setBeamDirectionColor whenever direction is known and
  // differs from what's already applied. This runs every sync tick (same
  // cadence as the idle<->mining reconciliation), so it also self-heals a
  // ship that flips direction across reservations without ever needing a
  // TRADE_OPEN event to correct it.
  syncOpenPositions(reservations) {
    if (!Array.isArray(reservations)) return;
    const openBotIds = new Set();
    const dirByBot = {}; // botId -> true(long)/false(short), last-reservation-wins
    for (let i = 0; i < reservations.length; i++) {
      const r = reservations[i];
      if (!r || !r.bot_id) continue;
      const id = String(r.bot_id).toLowerCase();
      openBotIds.add(id);
      if (r.direction) dirByBot[id] = !/SHORT/i.test(r.direction);
    }
    for (const id in _ships) {
      const rig = _ships[id];
      const shouldBeOpen = openBotIds.has(id);
      if (shouldBeOpen && (rig.state === 'idle' || rig.state === 'dormant') && rig.alive) {
        rig.state = 'mining';
      } else if (!shouldBeOpen && rig.state === 'mining') {
        // Bot has no live reservation anymore but we never saw its
        // TRADE_CLOSE (missed event / reconnect) — retract cleanly rather
        // than leaving a beam on for a position that's actually closed.
        rig.state = 'retracting';
      }
      if (shouldBeOpen && dirByBot.hasOwnProperty(id)) {
        const isLong = dirByBot[id];
        const wantDir = isLong ? 1 : -1;
        // !beamDirectionPainted forces the FIRST paint even when wantDir
        // already numerically matches the tradeDirection constructor
        // default (1/LONG) — see the flag's declaration in the ShipRig
        // constructor for why a plain !== guard alone would silently skip
        // repainting any position that happens to be LONG.
        if (rig.tradeDirection !== wantDir || !rig.beamDirectionPainted) {
          rig.tradeDirection = wantDir;
          rig.beamDirectionPainted = true;
          setBeamDirectionColor(rig.beam, isLong);
        }
      }
    }
  },

  dispose() {
    if (this._resizeObserver) { this._resizeObserver.disconnect(); this._resizeObserver = null; }
    if (_rafId) { cancelAnimationFrame(_rafId); _rafId = null; }
    for (const id in _ships) _ships[id].dispose();
    _ships = {};
    if (_station) { _station.dispose(); _station = null; }
    _pendingCCNode = null;
    if (_bloom) { _bloom.dispose(); _bloom = null; }
    if (_renderer) {
      _renderer.dispose();
      if (_canvas && _canvas.parentElement) _canvas.parentElement.removeChild(_canvas);
    }
    _renderer = null; _scene = null; _camera = null; _canvas = null; _container = null;
    _disposed = true;
    this._initialized = false;
  },

  // Exposed for the dev harness / debugging
  _debug() {
    return {
      shipCount: Object.keys(_ships).length,
      quality: _qualityLevel,
      worldW: _worldW, worldH: _worldH,
      view: _lastView,
      bloomEnabled: !!_bloom,
    };
  },

  _startLoop() {
    const loop = () => {
      if (_disposed) return;
      _rafId = requestAnimationFrame(loop);
      this._tick();
    };
    _rafId = requestAnimationFrame(loop);
  },

  _tick() {
    if (!_scene || !_camera || !_renderer) return;
    const dt = Math.min(0.05, _clock.getDelta());
    const t = _clock.elapsedTime;
    const view = _lastView;

    for (const id of TRADER_IDS) {
      const rig = _ships[id];
      const node = rig._pendingNode;
      if (!node) {
        rig.group.visible = false; rig.beam.group.visible = false;
        for (const esc of rig.escorts) esc.group.visible = false;
        continue;
      }

      const alive = !!node.alive;
      rig.alive = alive;
      rig.direction = typeof node.direction === 'number' ? node.direction : rig.direction;

      const scenePos = worldToScene(node.x, node.y, view, _worldW, _worldH);
      const sz = Math.max(1, node.currentSize || 14);

      // scale: hull length should read proportionally to the 2D body
      // radius so ships don't dwarf or vanish relative to their planet
      // marker. hullLength (~6-8 "px units") * scaleFactor ~= 2.4*sz
      //
      // SOTA pass (2026-07-30, finding 6 — "armada invisible"): 2.6x body
      // radius was legible in the Phase 1-3a dev harness (fixed formation,
      // no zoom range) but on the real fullscreen scene, node.currentSize
      // for trader bodies is often small (moon/planet tier, ~14-22px base)
      // and the whole scene additionally shrinks under _orbZoom at anything
      // less than 1x — compounding into genuinely sub-legible ship hulls on
      // a 50" panel viewed from across a room. Raised the proportional
      // multiplier AND added a floor in absolute screen-space px (applied
      // after the *view.zoom scale below is computed) so a ship never
      // shrinks below a legible minimum regardless of how small its host
      // node or how far zoomed out the camera is — this is the single
      // highest-leverage fix for this finding, per session memory noting
      // "engine light is what says ship at distance" only works if the
      // hull carrying that light is actually visible in the first place.
      const targetHullSpan = sz * 3.6;
      const baseLen = rig.group.userData.hullLength || 7;
      rig.scaleTarget = targetHullSpan / baseLen;
      rig.currentScale += (rig.scaleTarget - rig.currentScale) * Math.min(1, dt * 6);

      // Dead/dormant bots stay VISIBLE per spec ("engines dark, ship
      // drifts cold") — NOT hidden. An earlier version set
      // group.visible=alive here, which made dormant ships vanish
      // entirely instead of reading as a cold derelict.
      rig.group.visible = true;
      rig.group.position.set(scenePos.x, scenePos.y, 0);
      // Legibility floor (finding 6): rig.currentScale*view.zoom maps ~1:1
      // to CSS-px hull span in this camera setup (see worldToScene/ortho
      // camera contract), so a plain scalar floor here is a real min
      // on-screen size, not a proxy. 0.62 keeps ships readable even at
      // _ORB deep-zoom-out with a small host node, without visibly
      // "popping" bigger than nearby planet markers at normal zoom (the
      // proportional 3.6x formula above already dominates in the common
      // case; this floor only engages at the small/far extreme).
      const finalScale = Math.max(0.62, rig.currentScale * view.zoom);
      rig.group.scale.setScalar(finalScale);

      // orientation: face direction of travel using position delta;
      // fall back to a gentle idle yaw when nearly stationary.
      if (rig.prevPos) {
        const dx = scenePos.x - rig.prevPos.x, dy = scenePos.y - rig.prevPos.y;
        const speed = Math.hypot(dx, dy);
        // Smoothed speed (screen px/frame, not normalized to dt — deliberately
        // matches the existing `speed>0.05` heading-follow threshold's units
        // so the exhaust plume and the heading logic agree on what "moving"
        // means). Drives the engine exhaust plume length/opacity below —
        // real orbital motion, not a fake idle animation.
        rig.speedSmoothed = THREE.MathUtils.lerp(rig.speedSmoothed || 0, speed, Math.min(1, dt * 5));
        if (speed > 0.05) {
          const targetAngle = Math.atan2(dx, dy); // atan2(x,y): 0 = facing +Y (toward +Y = "up" in scene, ship built facing +Z)
          // Ship forward axis is +Z (screen-plane); rotate around Z axis
          // toward the 2D heading angle projected onto screen. Simpler:
          // rotate group.rotation.z toward the heading using atan2(dx,-dy)
          const heading = Math.atan2(dx, -dy);
          let cur = rig.group.rotation.z;
          let diff = heading - cur;
          while (diff > Math.PI) diff -= Math.PI * 2;
          while (diff < -Math.PI) diff += Math.PI * 2;
          rig.group.rotation.z = cur + diff * Math.min(1, dt * 3);
          // BANK FIX (2026-07-30): bank was driven directly off `diff`
          // (the raw per-frame heading error), which is NEVER zero for a
          // ship in steady circular orbit — every body on this dashboard
          // is always turning a little every frame, so bank sat pinned
          // near its 0.6rad (~34deg) cap almost continuously instead of
          // only during genuine sharp turns/migrations. A ship's own
          // wrapper Y axis is its forward/heading axis (see ORIENTATION
          // WRAPPER comment above), so a sustained large `rotation.y`
          // rolls the flat top-down silhouette out of the XY view plane
          // toward the camera — a round tapered hull (Rubberband's dart
          // body) rolled like this reads exactly as a foreshortened
          // side-profile fuselage ("airliner in the sky") instead of a
          // flat top-down ship, which was Jeremy's verbatim complaint.
          // Fix: normalize diff by dt so bank reflects genuine ANGULAR
          // RATE of the heading (rad/sec), not the raw per-frame delta,
          // and cut the cap by ~4x (0.6->0.15rad, ~8.6deg) so steady
          // orbital curvature reads as a subtle lean, not a full roll —
          // sharp direction changes (migration, retrograde) still bank
          // visibly since angular rate spikes briefly during those.
          const turnRate = dt > 0.0001 ? diff / dt : 0;
          const bank = THREE.MathUtils.clamp(turnRate * 0.06, -0.15, 0.15);
          rig.group.rotation.y = THREE.MathUtils.lerp(rig.group.rotation.y, bank, dt * 4);
          if (rig.group.userData.wingL) {
            rig.group.userData.wingL.rotation.z = THREE.MathUtils.lerp(rig.group.userData.wingL.rotation.z, -bank * 0.8, dt * 5);
            rig.group.userData.wingR.rotation.z = THREE.MathUtils.lerp(rig.group.userData.wingR.rotation.z, bank * 0.8, dt * 5);
          }
        } else {
          rig.group.rotation.y += (0 - rig.group.rotation.y) * dt * 2;
        }
      }
      rig.prevPos = scenePos;

      // idle bob/breathing
      rig.idlePhase += dt * (alive ? 1.1 : 0.2);
      const bob = Math.sin(rig.idlePhase) * (alive ? 1.4 : 0.4);
      rig.group.position.z = bob;

      // ESCORT WINGS (finding 5): loose flank formation, updated in _tick
      // so escorts inherit the rig's own scale/zoom math rather than
      // duplicating it. Skipped below a zoom-derived legibility floor —
      // at that scale the trader hull itself is barely readable, so a
      // pair of even-smaller escorts would be pure clutter/cost for zero
      // visual return ("skip escorts below a zoom threshold if perf
      // demands" per spec).
      const escortsVisible = alive && finalScale > 0.9;
      for (const esc of rig.escorts) {
        esc.group.visible = escortsVisible;
        if (!escortsVisible) continue;
        // Target flank position: offset from the ship along its OWN local
        // right/back axes (derived from group.rotation.z heading, matching
        // the ship's own screen-plane heading convention) so the pair
        // holds station relative to the hull's facing, not world axes.
        const heading = rig.group.rotation.z;
        const flankDist = rig.hullGroup.userData.hullLength ? rig.hullGroup.userData.hullLength * 0.85 : 6;
        const rightX = Math.cos(heading), rightY = Math.sin(heading);
        const backX = Math.sin(heading), backY = -Math.cos(heading);
        const targetX = scenePos.x + rightX * flankDist * esc.side - backX * flankDist * 0.55;
        const targetY = scenePos.y + rightY * flankDist * esc.side - backY * flankDist * 0.55;
        // Lag filter: escort chases a trailing copy of the target, not the
        // target itself — this is what reads as "piloted" (a real wingman
        // reacts a beat late) instead of rigidly welded to the lead ship.
        if (!esc.lagPos) esc.lagPos = { x: targetX, y: targetY };
        esc.lagPos.x += (targetX - esc.lagPos.x) * Math.min(1, dt * 2.2);
        esc.lagPos.y += (targetY - esc.lagPos.y) * Math.min(1, dt * 2.2);
        // Independent bob/sway per escort (own phase seed) so the pair
        // never moves in lockstep with each other or the lead ship.
        esc.phase += dt * 1.6;
        const sway = Math.sin(esc.phase) * 1.1;
        const bobZ = Math.cos(esc.phase * 0.8) * 0.9 + bob * 0.4;
        esc.group.position.set(esc.lagPos.x + rightX * sway * 0.3, esc.lagPos.y + rightY * sway * 0.3, bobZ);
        // Escort scale rides the SAME on-screen scale factor as the lead
        // ship (currentScale*zoom) so it shrinks/grows together at any
        // zoom level, just at its own small fixed fraction.
        esc.group.scale.setScalar(finalScale * 2.6);
        // Heading: mostly matches the lead ship, with a small independent
        // wobble so it doesn't look mechanically locked to the parent's
        // rotation.
        esc.group.rotation.z = heading + Math.sin(esc.phase * 0.5) * 0.12;
        if (esc.engineGlow) {
          esc.engineGlow.material.opacity = 0.55 + 0.3 * Math.sin(esc.phase * 3.0);
        }
      }

      // hull-specific idle flourishes
      if (rig.group.userData.booms) {
        for (let bi = 0; bi < rig.group.userData.booms.length; bi++) {
          rig.group.userData.booms[bi].rotation.x = Math.sin(t * 0.4 + bi) * 0.08;
        }
      }
      if (rig.group.userData.umbilicals) {
        for (let ui = 0; ui < rig.group.userData.umbilicals.length; ui++) {
          const flick = 0.7 + 0.3 * Math.sin(t * 2.3 + ui * 1.7);
          rig.group.userData.umbilicals[ui].coupler.material.emissiveIntensity = 1.0 * flick * (rig.state === 'mining' ? 1.8 : 1);
        }
      }
      if (rig.group.userData.dish) {
        // TurtleSue turret redesign (2026-07-30): the dish group now carries
        // a base orientation set via .quaternion (flush-mounted to the hull
        // surface, see buildTurtleSue) instead of sitting at the identity
        // rotation. Setting .rotation.z directly here would silently stomp
        // that base quaternion (three.js re-derives one from the other, and
        // writing to .rotation forces an Euler decomposition of whatever
        // quaternion is currently live) — so the wobble is now applied as a
        // small extra rotation On TOP of the mount orientation, composed
        // via quaternion multiplication, not a raw Euler set.
        const dishG = rig.group.userData.dish;
        if (!dishG.userData.baseQuat) dishG.userData.baseQuat = dishG.quaternion.clone();
        const wobble = new THREE.Quaternion().setFromAxisAngle(
          new THREE.Vector3(0, 1, 0), Math.sin(t * 0.3) * 0.15
        );
        dishG.quaternion.copy(dishG.userData.baseQuat).multiply(wobble);
        // Idle scanner pulse on the accent ring — without this the turret
        // is a dark, unlit blob against the hull (the exact "dead rod"
        // read the redesign is meant to fix). Mirrors the engine idle-pulse
        // language (0.55 +/- 0.25 sine) so it reads as active machinery,
        // not decoration.
        if (dishG.children[2] && dishG.children[2].material) {
          dishG.children[2].material.emissiveIntensity =
            (rig.state === 'mining' ? 1.6 : 0.6 + 0.35 * Math.sin(t * 1.6));
        }
      }

      // ---- state machine: idle -> mining -> retracting -> idle ----
      let targetBeamIntensity = 0;
      if (!alive) {
        rig.state = 'dormant';
      }
      if (rig.state === 'mining') {
        targetBeamIntensity = 1;
      } else if (rig.state === 'retracting') {
        targetBeamIntensity = 0;
        if (rig.beamIntensity < 0.02) rig.state = 'idle';
      }
      rig.beamIntensity += (targetBeamIntensity - rig.beamIntensity) * Math.min(1, dt * 4);

      // engine glow: brighter when mining (under load), idle pulse otherwise,
      // dark when dormant
      for (const eng of rig.engines) {
        let target;
        if (rig.state === 'dormant') target = 0.05;
        else if (rig.state === 'mining') target = 1.3;
        else target = 0.55 + 0.25 * Math.sin(t * 2 + rig.idlePhase);
        eng.glowMat.opacity = THREE.MathUtils.lerp(eng.glowMat.opacity, Math.min(1, target), dt * 4);
        const s = rig.state === 'mining' ? 1.15 : 1.0;
        eng.glow.scale.setScalar(THREE.MathUtils.lerp(eng.glow.scale.x, s, dt * 4));

        // EXHAUST PLUME (Phase 3a, 2026-07-30): this is the primary "vessel,
        // not satellite" signal at 30-60px per the spec — a glowing dot
        // reads as a status LED, a stretching plume reads as propulsion.
        // Length/opacity scale with rig.speedSmoothed (real orbital speed,
        // screen px/frame) so a body coasting through a slow arc shows a
        // short flicker and a body on a sharp migration/retrograde burn
        // trails visible fire — motion-linked, not a fixed decoration.
        // Dormant ships get a near-zero floor (cold engines, per spec
        // "engines dark, ship drifts cold"); mining adds a small constant
        // boost on top of speed since a ship holding position while mining
        // still has engines under load (station-keeping thrust).
        if (eng.plume) {
          const speedNorm = Math.min(1, (rig.speedSmoothed || 0) / 3.5);
          let plumeTarget = rig.state === 'dormant' ? 0.04 : 0.15 + speedNorm * 1.3;
          if (rig.state === 'mining') plumeTarget += 0.35;
          eng.plume.scale.z = THREE.MathUtils.lerp(eng.plume.scale.z, plumeTarget, dt * 5);
          const opTarget = rig.state === 'dormant' ? 0.03 : 0.22 + speedNorm * 0.45 + (rig.state === 'mining' ? 0.15 : 0);
          eng.plumeMat.opacity = THREE.MathUtils.lerp(eng.plumeMat.opacity, Math.min(0.9, opTarget), dt * 5);
        }
      }

      // cargo hold glow: profit green pulse / loss red vent, decaying.
      // FINDING 4 FIX (2026-07-30): userData.cargoMesh now points at a
      // small dedicated glow mesh built by addCargoBayGlow() (own unique
      // material instance) on every hull, never the main hull mesh — this
      // loop's material mutation logic is unchanged, but it now only ever
      // repaints that small bay, so the hull base color/livery from
      // finding 1 is never overwritten by P/L state.
      const cargoMesh = rig.group.userData.cargoMesh;
      if (cargoMesh && cargoMesh.material && rig.group.userData.cargoBaseColor) {
        if (rig.cargoGlowDecay > 0.001) {
          const isProfit = rig.cargoGlow > 0;
          const glowColor = isProfit ? new THREE.Color(0x30ff6a) : new THREE.Color(0xff3b30);
          const amt = rig.cargoGlowDecay;
          // SOTA pass (2026-07-30, finding 6): magnitude (rig.cargoGlowMag,
          // 0..1 from the real |pnl|/40 normalization at TRADE_CLOSE) now
          // scales peak brightness, not just decay envelope — a $2 win and
          // a $35 win used to glow identically at amt*1.4; now the bigger
          // result visibly reads as a bigger result.
          const mag = rig.cargoGlowMag != null ? rig.cargoGlowMag : 1;
          cargoMesh.material.emissive = glowColor;
          cargoMesh.material.emissiveIntensity = amt * (0.7 + mag * 1.1);
          rig.cargoGlowDecay -= dt * (isProfit ? 0.35 : 0.5); // loss vents faster than profit glow fades
        } else if (rig.state === 'mining') {
          // slow brighten while position held (holding cargo)
          cargoMesh.material.emissive = new THREE.Color(rig.def.color);
          cargoMesh.material.emissiveIntensity = Math.min(0.5, (cargoMesh.material.emissiveIntensity || 0) + dt * 0.08);
        } else {
          cargoMesh.material.emissiveIntensity = Math.max(0, (cargoMesh.material.emissiveIntensity || 0) - dt * 0.2);
        }
      }

      // beam target: nearest "rock" — approximate as a point offset
      // from the ship along its current heading (stands in for the
      // traded pair's position until Phase 2 wires a real pair anchor).
      if (rig.beamIntensity > 0.01) {
        // Force this ship's world matrix current NOW — matrixWorld is
        // normally only refreshed by the renderer/scene-wide update at
        // the end of the frame, which meant this beam math was reading
        // LAST frame's ship position/scale/rotation (a visible one-frame
        // lag that, combined with fast position deltas, made beams
        // appear anchored far from the ship instead of at its mount
        // point). Updating just this one small group is cheap.
        rig.group.updateMatrixWorld(true);
        // beamMount is authored in HULL-local space (each builder set it
        // relative to its own part positions, e.g. near the dish/scoop/
        // bow) — it must be transformed through hullGroup.matrixWorld,
        // NOT the outer orientation wrapper's matrix, or the mount point
        // lands somewhere unrelated to the actual hull geometry.
        const mountLocal = rig.hullGroup.userData.beamMount || new THREE.Vector3(0, 0, 3);
        const mountWorld = mountLocal.clone().applyMatrix4(rig.hullGroup.matrixWorld);
        // Forward direction: the outer group's local +Y axis in world
        // space (the ship's nose direction after the heading-follow
        // rotation.z spin — see the ShipRig constructor's orientation-
        // wrapper comment for why +Y and not +Z). Reading this directly
        // off the group's world matrix is robust to bank/roll, unlike
        // the old hand-derived sin/cos(rotation.z)-only approximation.
        const forward = new THREE.Vector3(0, 1, 0)
          .transformDirection(rig.group.matrixWorld)
          .normalize();
        const beamLen = 16 + sz * 0.6;
        const target = mountWorld.clone().add(forward.multiplyScalar(beamLen));
        updateBeam(rig.beam, mountWorld, target, t, rig.beamIntensity, rig.currentScale * view.zoom);
        rig.beam.group.visible = rig.beamIntensity > 0.02 && alive;
      } else {
        rig.beam.group.visible = false;
      }

      // dormant = cold drift, no rotation chase, dim everything
      if (rig.state === 'dormant') {
        rig.group.rotation.y += (0 - rig.group.rotation.y) * dt;
      }
    }

    // CC mothership station (Task 3, 2026-07-30)
    if (_station) {
      _station.update(dt, t, _pendingCCNode, view, _worldW, _worldH, _lastCCMeta);
    }

    // ensure world matrices are current before beam mount math above
    // reads them next frame (three.js auto-updates on render, but we
    // compute beam targets using this frame's matrices from last
    // render — acceptable 1-frame lag, imperceptible at 60fps)
    _scene.updateMatrixWorld(true);
    _renderer.render(_scene, _camera);
    // Bloom composite (SOTA upscale, 2026-08-18) — additive pass on top of
    // the normal render just performed above. See BloomPipeline for the
    // cost model; disabled entirely on the medium quality tier via
    // setQuality(), so a struggling machine sheds this first.
    if (_bloom) _bloom.render(_scene, _camera, (_quality.bloom && _quality.bloom.blurIterations) || 1);
  },
};

window.Armada = Armada;
export default Armada;
