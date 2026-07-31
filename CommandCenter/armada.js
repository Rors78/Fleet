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
  },
  medium: {
    pixelRatioCap: 1.5,
    beamParticles: 18,
    exhaustParticles: 6,
    shadows: false,
    lights: 2,
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
let _disposed = true;

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
function hullMaterial(colorHex, opts = {}) {
  const c = new THREE.Color(colorHex);
  // GRIMDARK PASS (2026-07-30): hull albedo is dark oxidized gunmetal —
  // the bot's fleet color is now an ACCENT ONLY (panel seams, engine glow,
  // running lights, ~5-10% of surface area), never the hull base. Round 1
  // used lightness 0.32 + sat<=0.35 which read as pastel painted plastic
  // ("mint green", "lavender", "peach") under flat even lighting — Jeremy's
  // explicit callout. Fix is two-part: (1) hull lightness drops to a
  // near-black 0.10-0.16 band with a faint per-ship color tint (sat<=0.14)
  // so ships stay distinguishable from each other without reading as
  // "colored", and (2) metalness/roughness are varied (0.55-0.75 / 0.35-0.55)
  // so specular highlights go tight and hard instead of soft-and-waxy.
  // The procedural env map (buildProceduralEnvMap) + harsh key light below
  // supply the specular response higher metalness needs — confirmed at
  // 30-60px this does NOT collapse to a black blob (see roughness floor).
  const hsl = { h: 0, s: 0, l: 0 };
  c.getHSL(hsl);
  const hullColor = new THREE.Color().setHSL(hsl.h, Math.min(hsl.s, 0.14), 0.12 + (opts.lightness || 0));
  // Tiny emissive floor (NOT the accent color — a near-black neutral
  // lift) so a flat face aimed directly at the camera (e.g. Confluence's
  // cylinder end-cap) is never a 100%-unlit black silhouette. This is
  // a "never fully dark" floor, not a glow — imperceptible except on
  // the specific geometry/angle combos that would otherwise go black.
  return new THREE.MeshStandardMaterial({
    color: hullColor,
    metalness: opts.metalness != null ? opts.metalness : 0.62,
    roughness: opts.roughness != null ? opts.roughness : 0.45,
    flatShading: !!opts.flat,
    emissive: new THREE.Color(0x0a0a10),
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
// CRITICAL: map textures MULTIPLY against material.color in three.js
// (finalColor = color * map). An early version used a near-black
// (#14141a) base fill here, which crushed every hull to near-solid-black
// once multiplied against the already-dark hull color — this was the
// root cause of TurtleSue/Confluence/NexusBrain rendering as unreadable
// black discs. The base MUST be neutral/light (~0.8-1.0) so it only
// ADDS panel-line detail without darkening the underlying hull color.
// Roughness uses a SEPARATE, low-contrast texture (roughness maps don't
// want the same high-contrast pattern as an albedo detail map).
const _greebleCache = new Map();
function greebleTexture(colorHex, seed) {
  const key = colorHex + '_' + seed;
  if (_greebleCache.has(key)) return _greebleCache.get(key);
  const size = 256;
  const cv = document.createElement('canvas');
  cv.width = size; cv.height = size;
  const ctx = cv.getContext('2d');
  const rand = mulberry32(seed);
  // Neutral light-gray base so multiplying against hullColor PRESERVES
  // the intended color instead of darkening it.
  ctx.fillStyle = '#d8d8de';
  ctx.fillRect(0, 0, size, size);
  const accent = '#' + new THREE.Color(colorHex).getHexString();
  // Sparse, LOW-frequency panel lines — at 24-40px on-screen a dense
  // fine grid just aliases to gray mush, so use fewer/thicker cells.
  ctx.strokeStyle = 'rgba(0,0,0,0.35)';
  ctx.lineWidth = 2;
  const cell = 42 + Math.floor(rand() * 20);
  for (let x = 0; x <= size; x += cell) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, size); ctx.stroke();
  }
  for (let y = 0; y <= size; y += cell) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(size, y); ctx.stroke();
  }
  // A handful of larger panel blocks (subtle shade variation, not deep shadow)
  for (let i = 0; i < 10; i++) {
    const w = 20 + rand() * 40, h = 20 + rand() * 40;
    const x = rand() * size, y = rand() * size;
    ctx.fillStyle = rand() > 0.5 ? 'rgba(0,0,0,0.12)' : 'rgba(255,255,255,0.10)';
    ctx.fillRect(x, y, w, h);
  }
  // Bright accent seams — the only color hint on the hull. Bumped from
  // '55' (33% alpha) to '88' (53%) for the grimdark pass: against a much
  // darker hull base these seams are now doing more identity work, so
  // they need to survive the multiply and still read at 30-60px.
  ctx.strokeStyle = accent + '88';
  ctx.lineWidth = 2.5;
  for (let i = 0; i < 2; i++) {
    const y = rand() * size;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(size, y); ctx.stroke();
  }
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

function greebledHullMaterial(colorHex, seed, repeat = 3) {
  const mat = hullMaterial(colorHex);
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
// ============================================================
function addEngineNozzle(group, x, y, z, radius, colorHex, facing = new THREE.Vector3(0, 0, 1)) {
  const nozzle = new THREE.Mesh(
    new THREE.CylinderGeometry(radius * 0.7, radius, radius * 1.6, 10),
    darkTrimMaterial()
  );
  nozzle.rotation.x = Math.PI / 2;
  nozzle.position.set(x, y, z);
  group.add(nozzle);

  const glowGeo = new THREE.CircleGeometry(radius * 0.65, 12);
  const glowMat = new THREE.MeshBasicMaterial({
    color: colorHex, transparent: true, opacity: 0.95,
    blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
  });
  const glow = new THREE.Mesh(glowGeo, glowMat);
  glow.position.set(x, y, z + radius * 0.85 * facing.z + radius * 0.85 * facing.y * 0 );
  glow.position.copy(new THREE.Vector3(x, y, z).add(facing.clone().multiplyScalar(radius * 0.9)));
  group.add(glow);
  return { nozzle, glow, glowMat };
}

function addRunningLight(group, x, y, z, colorHex, size = 0.35) {
  const geo = new THREE.SphereGeometry(size, 6, 6);
  const mat = new THREE.MeshBasicMaterial({ color: colorHex });
  const dot = new THREE.Mesh(geo, mat);
  dot.position.set(x, y, z);
  group.add(dot);
  return dot;
}

// ============================================================
// HULL BUILDERS — six procedural ships, unit scale ~ hull length 8-12
// units along local +Z (forward). Each returns {group, engines[], lights[]}
// ============================================================

// --- TurtleSue: dreadnought-miner (Death Star heritage) ---------------
function buildTurtleSue(seed) {
  const g = new THREE.Group();
  const col = FLEET.turtlesue.color;
  const rand = mulberry32(seed);

  const hullMat = greebledHullMaterial(col, seed, 4);
  const sphere = new THREE.Mesh(new THREE.SphereGeometry(3.4, 24, 18), hullMat);
  g.add(sphere);

  // equatorial trench band (Death Star silhouette cue, industrialized)
  const band = new THREE.Mesh(
    new THREE.TorusGeometry(3.42, 0.14, 6, 32),
    darkTrimMaterial()
  );
  band.rotation.x = Math.PI / 2;
  g.add(band);

  // focusing dish (mining beam projector — replaces "superlaser" framing)
  const dishGroup = new THREE.Group();
  const dish = new THREE.Mesh(
    new THREE.SphereGeometry(1.05, 16, 12, 0, Math.PI * 2, 0, Math.PI / 2),
    darkTrimMaterial()
  );
  dish.rotation.x = Math.PI;
  dishGroup.add(dish);
  const dishRing = new THREE.Mesh(new THREE.TorusGeometry(1.05, 0.06, 6, 20), accentMaterial(col, 0.8));
  dishRing.rotation.x = Math.PI / 2;
  dishGroup.add(dishRing);
  dishGroup.position.set(1.6, 1.2, 2.1);
  dishGroup.scale.setScalar(1.0);
  g.add(dishGroup);
  g.userData.dish = dishGroup;

  // rear engine cluster (dreadnought pushed by 3 heavy nozzles)
  const engines = [];
  const enginePositions = [[-1.4, -1.2, -3.2], [1.4, -1.2, -3.2], [0, -2.1, -2.9]];
  for (const [ex, ey, ez] of enginePositions) {
    engines.push(addEngineNozzle(g, ex, ey, ez, 0.55, col, new THREE.Vector3(0, 0, -1)));
  }

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

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  g.userData.beamMount = new THREE.Vector3(1.6, 1.2, 3.1);
  g.userData.cargoMesh = sphere;
  g.userData.cargoBaseColor = hullMat.color.clone();
  g.userData.hullLength = 7;
  return { group: g, engines, lights };
}

// --- Gridzilla: lattice-frame harvester (visible truss) ---------------
function buildGridzilla(seed) {
  const g = new THREE.Group();
  const col = FLEET.gridzilla.color;
  const rand = mulberry32(seed);
  const trim = darkTrimMaterial();
  const hullMat = greebledHullMaterial(col, seed, 2);
  // Small emissive ACCENT used sparingly (strut tips + scoop rim only) —
  // an earlier version used this on all 16 main struts + the scoop glow,
  // which combined with ACES tone mapping blew the whole silhouette out
  // to a solid flat-white/yellow blob with zero readable structure.
  // Structural members must stay on matte hull/trim material; emissive
  // is reserved for small highlight points so the lattice reads as
  // machinery, not a glowing lantern.
  const accentDot = accentMaterial(col, 1.1);

  // central spine
  const spine = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 7), hullMat);
  g.add(spine);

  // truss frame — repeated box struts forming an open lattice cage
  // around the spine (harvester silhouette, very non-spherical).
  // Structural rings/struts use matte hull/trim material so the cage
  // reads as machined metal; only the strut JOINTS get a small emissive
  // dot for detail, keeping total glowing surface area low.
  const struts = new THREE.Group();
  const ringCount = 5;
  for (let i = 0; i < ringCount; i++) {
    const z = -3 + (i * 6) / (ringCount - 1);
    const rSize = 1.6 - Math.abs(i - (ringCount - 1) / 2) * 0.12;
    const ringGeo = new THREE.TorusGeometry(rSize, 0.06, 5, 4); // square-ish frame
    const ring = new THREE.Mesh(ringGeo, hullMat);
    ring.rotation.z = Math.PI / 4;
    ring.position.z = z;
    struts.add(ring);
    if (i < ringCount - 1) {
      for (let c = 0; c < 4; c++) {
        const a = (Math.PI / 2) * c + Math.PI / 4;
        const strut = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.08, 6 / (ringCount - 1) + 0.15), trim);
        strut.position.set(Math.cos(a) * rSize, Math.sin(a) * rSize, z + 3 / (ringCount - 1));
        struts.add(strut);
        // small emissive joint node — the only glow on the truss itself
        if (i % 2 === 0) {
          const joint = new THREE.Mesh(new THREE.SphereGeometry(0.09, 6, 6), accentDot);
          joint.position.copy(strut.position);
          struts.add(joint);
        }
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

  // engines at stern, arranged in the truss square
  const engines = [];
  for (const [ex, ey] of [[-1.1, -1.1], [1.1, -1.1], [-1.1, 1.1], [1.1, 1.1]]) {
    engines.push(addEngineNozzle(g, ex, ey, -3.6, 0.32, col, new THREE.Vector3(0, 0, -1)));
  }

  const lights = [];
  for (let i = 0; i < 4; i++) {
    lights.push(addRunningLight(g, (rand() - 0.5) * 3, (rand() - 0.5) * 2, (rand() - 0.5) * 6, 0xffcc00, 0.18));
  }

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  g.userData.beamMount = new THREE.Vector3(0, 0, 4.6);
  g.userData.cargoMesh = spine;
  g.userData.cargoBaseColor = trim.color.clone();
  g.userData.hullLength = 8;
  return { group: g, engines, lights };
}

// --- Rubberband: agile skiff (sleek, banks hard) -----------------------
function buildRubberband(seed) {
  const g = new THREE.Group();
  const col = FLEET.rubberband.color;
  const rand = mulberry32(seed);

  const hullMat = greebledHullMaterial(col, seed, 2);
  // elongated tapered hull via lathe-ish stacked cylinders for a sleek dart
  const bodyShape = new THREE.CylinderGeometry(0.15, 0.85, 5.6, 8);
  const body = new THREE.Mesh(bodyShape, hullMat);
  body.rotation.x = Math.PI / 2;
  body.position.z = 0.2;
  g.add(body);

  // swept wings (flex visually on bank via userData.wingL/R rotation)
  // Large flat unbroken faces are the one geometry shape where the harsh
  // grimdark key light overexposes badly — a plain hullMaterial() here
  // (no greeble map to break up the surface) caught the key light nearly
  // face-on and rendered as a flat pale slab, visibly lighter than every
  // other hull on the ship at closeup. Switched to greebledHullMaterial
  // (repeat=2) so panel-line detail interrupts the flat highlight, and
  // dropped the +0.03 lightness bump now that the base hull is already
  // much darker than round 1.
  const wingGeo = new THREE.BoxGeometry(3.4, 0.08, 1.3);
  wingGeo.translate(1.9, 0, 0);
  const wingMat = greebledHullMaterial(col, seed + 1, 2);
  const wingL = new THREE.Mesh(wingGeo, wingMat);
  wingL.position.set(0.5, 0, -0.6);
  const wingR = wingL.clone();
  wingR.scale.x = -1;
  g.add(wingL, wingR);
  const wingAccentL = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.1, 1.2), accentMaterial(col, 1.2));
  wingAccentL.position.set(3.9, 0, -0.6);
  const wingAccentR = wingAccentL.clone();
  wingAccentR.position.x = -3.9;
  g.add(wingAccentL, wingAccentR);

  // nose spike
  const nose = new THREE.Mesh(new THREE.ConeGeometry(0.22, 1.4, 6), darkTrimMaterial());
  nose.rotation.x = Math.PI / 2;
  nose.position.z = 3.2;
  g.add(nose);

  const engines = [addEngineNozzle(g, 0, -0.1, -2.9, 0.42, col, new THREE.Vector3(0, 0, -1))];

  const lights = [
    addRunningLight(g, 3.9, 0, -0.6, 0xff3b30, 0.16),
    addRunningLight(g, -3.9, 0, -0.6, 0x30ff5f, 0.16),
  ];

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  g.userData.beamMount = new THREE.Vector3(0, -0.3, 2.6);
  g.userData.cargoMesh = body;
  g.userData.cargoBaseColor = hullMat.color.clone();
  g.userData.wingL = wingL;
  g.userData.wingR = wingR;
  g.userData.hullLength = 6.5;
  return { group: g, engines, lights };
}

// --- Arbitrageur: twin-hulled catamaran with connecting spar -----------
function buildArbitrageur(seed) {
  const g = new THREE.Group();
  const col = FLEET.arbitrageur.color;
  const rand = mulberry32(seed);
  const hullMat = greebledHullMaterial(col, seed, 2);

  function buildHalfHull(sign) {
    const half = new THREE.Group();
    const hull = new THREE.Mesh(new THREE.CapsuleGeometry(0.55, 3.6, 4, 8), hullMat);
    hull.rotation.x = Math.PI / 2;
    half.add(hull);
    half.position.set(sign * 1.7, 0, 0);
    return { half, hull };
  }
  const left = buildHalfHull(-1);
  const right = buildHalfHull(1);
  g.add(left.half, right.half);

  // connecting spar (the "barycenter" identity — literally links the two hulls)
  const sparMat = accentMaterial(col, 0.9);
  const spar = new THREE.Mesh(new THREE.BoxGeometry(3.4, 0.18, 0.5), sparMat);
  spar.position.set(0, 0, 0.3);
  g.add(spar);
  const spar2 = new THREE.Mesh(new THREE.BoxGeometry(3.4, 0.18, 0.5), darkTrimMaterial());
  spar2.position.set(0, 0, -1.4);
  g.add(spar2);

  // small central sensor/comm mast at the barycenter
  const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 1.4, 6), darkTrimMaterial());
  mast.position.set(0, 0.7, -0.4);
  g.add(mast);
  const mastTip = addRunningLight(g, 0, 1.45, -0.4, col, 0.22);

  const engines = [
    addEngineNozzle(g, -1.7, 0, -2.2, 0.4, col, new THREE.Vector3(0, 0, -1)),
    addEngineNozzle(g, 1.7, 0, -2.2, 0.4, col, new THREE.Vector3(0, 0, -1)),
  ];

  const lights = [
    addRunningLight(g, -1.7, 0.4, 1.9, 0xff3b30, 0.16),
    addRunningLight(g, 1.7, 0.4, 1.9, 0x30ff5f, 0.16),
  ];

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  g.userData.beamMount = new THREE.Vector3(0, -0.4, 1.2);
  g.userData.cargoMesh = spar;
  g.userData.cargoBaseColor = sparMat.color.clone();
  g.userData.hullLength = 5.5;
  return { group: g, engines, lights };
}

// --- NexusBrain: science vessel, sensor booms (replaces cortex swirl) --
function buildNexusBrain(seed) {
  const g = new THREE.Group();
  const col = FLEET.nexusbrain.color;
  const rand = mulberry32(seed);
  const hullMat = greebledHullMaterial(col, seed, 2);

  const core = new THREE.Mesh(new THREE.IcosahedronGeometry(1.15, 1), hullMat);
  g.add(core);

  // forward command spike
  const spike = new THREE.Mesh(new THREE.ConeGeometry(0.5, 2.2, 6), darkTrimMaterial());
  spike.rotation.x = Math.PI / 2;
  spike.position.z = 2.0;
  g.add(spike);

  // three sensor booms radiating outward — the analytical "reaching out
  // to sense the market" identity, built from hard mechanical parts
  // (rods + dish caps), not a decorative swirl.
  const booms = [];
  const boomAngles = [0, (Math.PI * 2) / 3, (Math.PI * 4) / 3];
  for (const a of boomAngles) {
    const boomGroup = new THREE.Group();
    const rod = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.07, 2.6, 6), darkTrimMaterial());
    rod.rotation.z = Math.PI / 2;
    rod.position.x = 1.3;
    boomGroup.add(rod);
    const dish = new THREE.Mesh(new THREE.SphereGeometry(0.32, 10, 8, 0, Math.PI * 2, 0, Math.PI / 1.6), accentMaterial(col, 1.0));
    dish.rotation.z = -Math.PI / 2;
    dish.position.x = 2.6;
    boomGroup.add(dish);
    boomGroup.rotation.z = a;
    boomGroup.position.z = -0.3;
    booms.push(boomGroup);
    g.add(boomGroup);
  }
  g.userData.booms = booms;

  // rear service module
  const rear = new THREE.Mesh(new THREE.CylinderGeometry(0.75, 0.9, 1.6, 10), hullMat);
  rear.rotation.x = Math.PI / 2;
  rear.position.z = -1.6;
  g.add(rear);

  const engines = [addEngineNozzle(g, 0, 0, -2.6, 0.5, col, new THREE.Vector3(0, 0, -1))];
  const lights = [
    addRunningLight(g, 0, 1.2, -0.3, 0xffffff, 0.16),
    addRunningLight(g, 0, -1.2, -0.3, 0xffffff, 0.16),
  ];

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  g.userData.beamMount = new THREE.Vector3(0, 0, 3.0);
  g.userData.cargoMesh = rear;
  g.userData.cargoBaseColor = hullMat.color.clone();
  g.userData.hullLength = 6;
  return { group: g, engines, lights };
}

// --- Confluence: refinery flagship, four docking umbilicals ------------
function buildConfluence(seed) {
  const g = new THREE.Group();
  const col = FLEET.confluence.color;
  const rand = mulberry32(seed);
  const hullMat = greebledHullMaterial(col, seed, 3);

  // large drum-shaped refinery hull
  const drum = new THREE.Mesh(new THREE.CylinderGeometry(1.3, 1.3, 4.4, 14), hullMat);
  drum.rotation.x = Math.PI / 2;
  g.add(drum);

  // process rings around the drum
  for (const z of [-1.4, 0, 1.4]) {
    const ring = new THREE.Mesh(new THREE.TorusGeometry(1.35, 0.09, 6, 20), darkTrimMaterial());
    ring.rotation.y = Math.PI / 2;
    ring.position.z = z;
    g.add(ring);
  }

  // four docking umbilicals — the "four intel feeds" identity, arranged
  // radially, each ending in a small emissive coupler
  const umbilicals = [];
  for (let i = 0; i < 4; i++) {
    const a = (Math.PI / 2) * i + Math.PI / 4;
    const arm = new THREE.Group();
    const strut = new THREE.Mesh(new THREE.CylinderGeometry(0.08, 0.1, 1.5, 6), darkTrimMaterial());
    strut.rotation.z = Math.PI / 2;
    strut.position.x = 0.75;
    arm.add(strut);
    const coupler = new THREE.Mesh(new THREE.SphereGeometry(0.22, 8, 8), accentMaterial(col, 1.3));
    coupler.position.x = 1.55;
    arm.add(coupler);
    arm.position.set(Math.cos(a) * 1.25, Math.sin(a) * 1.25, 0.6);
    arm.rotation.z = a;
    umbilicals.push({ arm, coupler });
    g.add(arm);
  }
  g.userData.umbilicals = umbilicals;

  // bow collector cone
  const bow = new THREE.Mesh(new THREE.ConeGeometry(1.0, 1.6, 14), darkTrimMaterial());
  bow.rotation.x = -Math.PI / 2;
  bow.position.z = 3.0;
  g.add(bow);

  const engines = [];
  for (const [ex, ey] of [[-0.9, -0.9], [0.9, -0.9], [-0.9, 0.9], [0.9, 0.9]]) {
    engines.push(addEngineNozzle(g, ex, ey, -2.4, 0.36, col, new THREE.Vector3(0, 0, -1)));
  }

  const lights = [];
  for (let i = 0; i < 4; i++) {
    lights.push(addRunningLight(g, (rand() - 0.5) * 2.6, (rand() - 0.5) * 2.6, (rand() - 0.5) * 4, 0xffffff, 0.16));
  }

  g.userData.forwardAxis = new THREE.Vector3(0, 0, 1);
  g.userData.beamMount = new THREE.Vector3(0, 0, 3.9);
  g.userData.cargoMesh = drum;
  g.userData.cargoBaseColor = hullMat.color.clone();
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
  const core = new THREE.Mesh(new THREE.CylinderGeometry(0.11, 0.11, 1, 8, 1, true), coreMat);
  group.add(core);

  // outer volumetric glow: wider cylinder, softer
  const glow = new THREE.Mesh(new THREE.CylinderGeometry(0.4, 0.4, 1, 10, 1, true), glowMat);
  group.add(glow);

  // impact flare at the rock end
  const flareMat = new THREE.MeshBasicMaterial({
    color: colorHex, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const flare = new THREE.Mesh(new THREE.SphereGeometry(0.28, 10, 8), flareMat);
  group.add(flare);

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
  group.add(points);

  return { group, core, glow, flare, points, seeds, coreMat, glowMat, flareMat, pMat, particleCount, colorHex };
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
  const posAttr = beam.points.geometry.getAttribute('position');
  for (let i = 0; i < beam.particleCount; i++) {
    const phase = (beam.seeds[i] + t * 0.35) % 1;
    const along = 1 - phase; // 0 -> ship end, 1 -> target end
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
  }

  dispose() {
    if (this.sceneRef) {
      this.sceneRef.remove(this.group);
      this.sceneRef.remove(this.beam.group);
    }
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
    _renderer.toneMappingExposure = 1.35;
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

    // GRIMDARK LIGHTING PASS (2026-07-30): round 1's four roughly-equal
    // omnidirectional lights (key 3.2, rim 1.6, fill 2.2, ambient 1.1)
    // flattened every hull to soft even brightness — no dominant shadow
    // side, which read as "toy plastic under a softbox" per Jeremy's
    // callout. Contrast IS the aesthetic now: one hard, slightly-cool
    // key light dominates (single clear shadow direction), rim/fill are
    // dropped to genuinely dim accents that only keep the AWAY-from-key
    // hemisphere from going pure black, and ambient is cut hard so
    // unlit panels can actually read as unlit. This is deliberately a
    // harsher ratio than "realistic" three-point lighting — silhouette
    // + running lights/accents carry readability at 30-60px, not ambient
    // fill, per the spec's "dark-but-defined" bar.
    const key = new THREE.DirectionalLight(0xdbe6ff, 4.4);
    key.position.set(-40, 60, 80);
    _scene.add(key);

    // Rim: was a near-key-strength fill light (1.6) — cut to a thin cool
    // edge light so silhouettes stay separable from near-black space
    // without lighting the whole away-facing hemisphere.
    const rim = new THREE.DirectionalLight(0x6f88ff, 0.55);
    rim.position.set(50, -30, -60);
    _scene.add(rim);

    // Dim headlight-ish fill from near the camera — kept ONLY so a flat
    // surface normal to the view axis (e.g. Confluence's cylinder
    // end-cap) never goes to a 100%-unlit black disc. Cut from 2.2 to
    // 0.35: round 1's value was strong enough to act as a second key
    // light and wash out the shadow side the whole pass is built around.
    const fill = new THREE.DirectionalLight(0x8fa0d0, 0.35);
    fill.position.set(0, 10, 150);
    _scene.add(fill);

    // Ambient floor cut hard (was 1.1) — this was the single biggest
    // contributor to the flat pastel look, since ambient light ignores
    // normal direction entirely and lifts every face equally regardless
    // of the key light's direction. Low value here is what lets the
    // higher metalness (hullMaterial) and darker albedo actually show
    // deep shadow falloff instead of being overridden.
    const ambient = new THREE.AmbientLight(0x404858, 0.32);
    _scene.add(ambient);

    // Build the six trader ships
    _ships = {};
    let seed = 1000;
    for (const id of TRADER_IDS) {
      const rig = new ShipRig(id, seed);
      rig.attachToScene(_scene);
      _ships[id] = rig;
      seed += 137;
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
  },

  setQuality(level) {
    const q = QUALITY[level];
    if (!q) return;
    _quality = q;
    _qualityLevel = level;
    _renderer && _renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, q.pixelRatioCap));
    for (const id in _ships) _ships[id].setQuality(q);
  },

  getQuality() { return _qualityLevel; },

  syncFromNodes(nodesLike, view) {
    if (!_scene || !nodesLike) return;
    _lastView = view || _lastView;
    for (const id of TRADER_IDS) {
      const rig = _ships[id];
      const node = nodesLike[id];
      if (!rig || !node) continue;
      rig._pendingNode = node;
    }
  },

  onEvent(evt) {
    if (!evt || !evt.type) return;
    const type = String(evt.type);
    const botId = String(evt.bot_id || evt.source || '').toLowerCase();
    const rig = _ships[botId];
    if (!rig) return;
    const data = evt.data || {};

    if (/TRADE_OPEN/.test(type)) {
      rig.state = 'mining';
      rig.lastEventPair = data.pair || '';
      const isShort = /SHORT/i.test(data.direction || 'LONG');
      rig.tradeDirection = isShort ? -1 : 1;
      // Beam color = trade direction (green LONG / red SHORT) per spec —
      // NOT the ship's fleet identity color. Repainted on every open so
      // a ship that flips direction between trades gets the right color.
      setBeamDirectionColor(rig.beam, !isShort);
    } else if (/TRADE_CLOSE/.test(type)) {
      const pnl = data.pnl || 0;
      rig.state = 'retracting';
      rig.cargoGlow = pnl >= 0 ? 1 : -1;
      rig.cargoGlowDecay = 1.0;
    }
  },

  dispose() {
    if (this._resizeObserver) { this._resizeObserver.disconnect(); this._resizeObserver = null; }
    if (_rafId) { cancelAnimationFrame(_rafId); _rafId = null; }
    for (const id in _ships) _ships[id].dispose();
    _ships = {};
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
      if (!node) { rig.group.visible = false; rig.beam.group.visible = false; continue; }

      const alive = !!node.alive;
      rig.alive = alive;
      rig.direction = typeof node.direction === 'number' ? node.direction : rig.direction;

      const scenePos = worldToScene(node.x, node.y, view, _worldW, _worldH);
      const sz = Math.max(1, node.currentSize || 14);

      // scale: hull length should read proportionally to the 2D body
      // radius so ships don't dwarf or vanish relative to their planet
      // marker. hullLength (~6-8 "px units") * scaleFactor ~= 2.4*sz
      const targetHullSpan = sz * 2.6;
      const baseLen = rig.group.userData.hullLength || 7;
      rig.scaleTarget = targetHullSpan / baseLen;
      rig.currentScale += (rig.scaleTarget - rig.currentScale) * Math.min(1, dt * 6);

      // Dead/dormant bots stay VISIBLE per spec ("engines dark, ship
      // drifts cold") — NOT hidden. An earlier version set
      // group.visible=alive here, which made dormant ships vanish
      // entirely instead of reading as a cold derelict.
      rig.group.visible = true;
      rig.group.position.set(scenePos.x, scenePos.y, 0);
      rig.group.scale.setScalar(rig.currentScale * view.zoom);

      // orientation: face direction of travel using position delta;
      // fall back to a gentle idle yaw when nearly stationary.
      if (rig.prevPos) {
        const dx = scenePos.x - rig.prevPos.x, dy = scenePos.y - rig.prevPos.y;
        const speed = Math.hypot(dx, dy);
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
          // bank into turns (Rubberband flexes wings extra on bank)
          const bank = THREE.MathUtils.clamp(diff * 1.5, -0.6, 0.6);
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
        rig.group.userData.dish.rotation.z = Math.sin(t * 0.3) * 0.15;
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
      }

      // cargo hold glow: profit green pulse / loss red vent, decaying
      const cargoMesh = rig.group.userData.cargoMesh;
      if (cargoMesh && cargoMesh.material && rig.group.userData.cargoBaseColor) {
        if (rig.cargoGlowDecay > 0.001) {
          const isProfit = rig.cargoGlow > 0;
          const glowColor = isProfit ? new THREE.Color(0x30ff6a) : new THREE.Color(0xff3b30);
          const amt = rig.cargoGlowDecay;
          cargoMesh.material.emissive = glowColor;
          cargoMesh.material.emissiveIntensity = amt * 1.4;
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

    // ensure world matrices are current before beam mount math above
    // reads them next frame (three.js auto-updates on render, but we
    // compute beam targets using this frame's matrices from last
    // render — acceptable 1-frame lag, imperceptible at 60fps)
    _scene.updateMatrixWorld(true);
    _renderer.render(_scene, _camera);
  },
};

window.Armada = Armada;
export default Armada;
