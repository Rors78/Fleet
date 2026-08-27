// Offline verification harness for the CC abstract-station rebuild
// (buildStation / StationRig in armada.js). Stubs just enough DOM to let
// the real module import real three.js r178 and construct real runtime
// objects, then asserts against those objects directly (mesh existence,
// layers.mask, geometry params, material colors, positions) rather than
// grepping source text.
//
// Run: "C:\Program Files\nodejs\node.exe" .verify_enterprise_harness.mjs

// ---- minimal fake 2D canvas context, enough for stationHullTexture /
// the beam gradient texture builder in armada.js to run without throwing.
function fakeCanvasContext() {
  return {
    fillStyle: '', strokeStyle: '', lineWidth: 1,
    fillRect() {}, strokeRect() {},
    beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {},
    createLinearGradient() { return { addColorStop() {} }; },
    createRadialGradient() { return { addColorStop() {} }; },
    drawImage() {}, clearRect() {},
    measureText() { return { width: 0 }; },
    save() {}, restore() {}, translate() {}, rotate() {}, scale() {},
  };
}
function fakeCanvasElement() {
  const ctx2d = fakeCanvasContext();
  return {
    width: 0, height: 0,
    getContext(kind) {
      if (kind === '2d') return ctx2d;
      return null; // webgl context never requested by buildStation path
    },
  };
}

globalThis.document = {
  createElement(tag) {
    if (tag === 'canvas') return fakeCanvasElement();
    return {};
  },
};
globalThis.window = globalThis; // armada.js sets window.Armada at module scope
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 16);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

// buildStation/StationRig are not exported — pull them off window.Armada's
// closure indirectly by re-importing the source text isn't necessary here:
// window.Armada is exported, and its public surface (fireSuperlaser,
// superlaserState, hasStation) plus the module's own top-level side effect
// of defining window.Armada is all we need PLUS we need direct access to
// buildStation/StationRig for structural assertions. Since those are not
// exported, re-require the file text and eval the two functions in an
// isolated scope that shares the same THREE instance via a small shim.
//
// Simpler + more honest: import three directly (same package the module
// resolves via the repo's node ESM setup) and eval armada.js's source with
// a wrapper that captures buildStation/StationRig onto a global before the
// IIFE-less module finishes, using Node's vm module against the REAL file
// so we are testing the actual shipped source, not a re-typed copy.

import vm from 'node:vm';
import fs from 'node:fs';
import * as THREE from './three.module.min.js';

const src = fs.readFileSync('D:/CommandCenter/armada.js', 'utf8');
// Strip the leading `import * as THREE from 'three';` line (we inject THREE
// directly into the vm context instead) — everything else is unmodified
// real source, executed as a classic script inside a sandbox that exposes
// buildStation/StationRig/mulberry32/BLOOM_LAYER etc. as sandbox globals by
// declaring them with `var` semantics (vm script top-level `function`/`var`
// declarations attach to the sandbox's global object).
const patched = src
  .replace(/import \* as THREE from 'three';/, '')
  .replace(/export default Armada;\s*$/, '');

const sandbox = {
  THREE,
  document: globalThis.document,
  window: {},
  ResizeObserver: globalThis.ResizeObserver,
  requestAnimationFrame: globalThis.requestAnimationFrame,
  cancelAnimationFrame: globalThis.cancelAnimationFrame,
  console,
};
sandbox.window.Armada = undefined;
vm.createContext(sandbox);
new vm.Script(patched, { filename: 'armada.js (sandboxed)' }).runInContext(sandbox);

// Pull the functions/classes we need for structural assertions out of the
// sandbox via a trailing expression evaluated in the SAME context (they are
// top-level `function`/`class` declarations, which vm attaches to the
// context's global object).
const buildStation = vm.runInContext('buildStation', sandbox);
const StationRig = vm.runInContext('StationRig', sandbox);
const BLOOM_LAYER = vm.runInContext('BLOOM_LAYER', sandbox);
const Armada = vm.runInContext('Armada', sandbox);

let failures = 0;
function check(label, cond) {
  if (cond) { console.log(`PASS  ${label}`); }
  else { console.log(`FAIL  ${label}`); failures++; }
}

const built = buildStation(9001);

// ============================================================
// 1. The contract StationRig consumes. Unchanged by the rebuild.
// ============================================================
check('returns a group', built.group && built.group.isGroup);
check('returns core/coreGlow/coreHalo meshes',
  built.core.isMesh && built.coreGlow.isMesh && built.coreHalo.isMesh);
check('returns coreMat/coreGlowMat/coreHaloMat',
  built.coreMat.isMaterial && built.coreGlowMat.isMaterial && built.coreHaloMat.isMaterial);
check('returns dockingLights array',
  Array.isArray(built.dockingLights) && built.dockingLights.length > 0);
check('returns sl.{rimMat,flare,flareMat,mainMat,glowMat}',
  built.sl && built.sl.rimMat.isMaterial && built.sl.flare.isMesh &&
  built.sl.flareMat.isMaterial && built.sl.mainMat.isMaterial && built.sl.glowMat.isMaterial);
check('hullLength set, comparable to prior bodies (10.4 sphere / 11.2 saucer)',
  typeof built.group.userData.hullLength === 'number' &&
  built.group.userData.hullLength > 9 && built.group.userData.hullLength < 15);
console.log(`  -> hullLength = ${built.group.userData.hullLength}`);

// ============================================================
// 2. THE RINGS ARE NO LONGER INERT.
// On the Enterprise build outerEdges/innerEdges were EMPTY groups, so
// every rotation write in StationRig.update() was a silent no-op. That
// is the specific regression this rebuild reverses.
// ============================================================
check('outerEdges carries geometry (was an empty no-op group)',
  built.outerEdges.isGroup && built.outerEdges.children.length > 0);
check('innerEdges carries geometry (was an empty no-op group)',
  built.innerEdges.isGroup && built.innerEdges.children.length > 0);
console.log(`  -> outerEdges ${built.outerEdges.children.length} children, innerEdges ${built.innerEdges.children.length}`);

{
  const probe = built.outerEdges.children.find(c => c.isMesh && c.geometry.type === 'BoxGeometry');
  built.group.updateMatrixWorld(true);
  const before = new THREE.Vector3(); probe.getWorldPosition(before);
  built.outerEdges.rotation.z = Math.PI / 2;
  built.group.updateMatrixWorld(true);
  const after = new THREE.Vector3(); probe.getWorldPosition(after);
  check('rotating outerEdges moves its children in world space',
    before.distanceTo(after) > 1.0);
  console.log(`  -> strut moved ${before.distanceTo(after).toFixed(2)} units on a 90deg ring turn`);
  built.outerEdges.rotation.z = 0;
  built.group.updateMatrixWorld(true);
}

// ============================================================
// 3. It is a station, not a ship.
// ============================================================
{
  let capsules = 0, lathes = 0, tori = 0, cylinders = 0;
  built.group.traverse(o => {
    if (!o.isMesh) return;
    const t = o.geometry.type;
    if (t === 'CapsuleGeometry') capsules++;
    if (t === 'LatheGeometry') lathes++;
    if (t === 'TorusGeometry') tori++;
    if (t === 'CylinderGeometry') cylinders++;
  });
  check('no CapsuleGeometry (the Enterprise nacelles / engineering hull)', capsules === 0);
  check('no LatheGeometry (the Enterprise saucer)', lathes === 0);
  check('has tori (hub collars + rings)', tori >= 3);
  check('has cylinders (hub drum + spine)', cylinders >= 2);
  console.log(`  -> capsules=${capsules} lathes=${lathes} tori=${tori} cylinders=${cylinders}`);
}

// ============================================================
// 4. Bloom discipline: emitters only, zero hull meshes.
// ============================================================
{
  let bloomed = 0, bloomedHull = 0;
  built.group.traverse(o => {
    if (!o.isMesh) return;
    if (!o.layers.isEnabled(BLOOM_LAYER)) return;
    bloomed++;
    // An emitter is either additively blended (beams/glows) or an UNLIT
    // MeshBasicMaterial (running-light dots -- addRunningLight tags these
    // deliberately). A hull mesh is MeshStandardMaterial: lit, opaque, and
    // large. Those are what blow out the bloom pass.
    const m = o.material;
    const isEmitter = m && (m.blending === THREE.AdditiveBlending || m.isMeshBasicMaterial);
    if (!isEmitter) bloomedHull++;
  });
  check('some meshes are bloom-tagged', bloomed > 0);
  check('ZERO non-additive (hull) meshes carry the bloom layer', bloomedHull === 0);
  console.log(`  -> ${bloomed} bloom-tagged meshes, ${bloomedHull} of them hull`);
}

// ============================================================
// 5. Emitter mounted forward and visible through the full sway.
// ============================================================
{
  built.group.updateMatrixWorld(true);
  const dishPos = new THREE.Vector3();
  built.core.getWorldPosition(dishPos);
  check('emitter core sits forward (+Z) of the hub centre', dishPos.z > 1.5);
  console.log(`  -> core world Z = ${dishPos.z.toFixed(2)}`);

  let minZ = Infinity;
  for (let f = 0; f < 200; f++) {
    const t = f * 1.7;
    built.group.rotation.y = Math.sin(t * (Math.PI * 2 / 180)) * 0.55;
    built.group.rotation.x = Math.sin(t * (Math.PI * 2 / 260)) * 0.18;
    built.group.updateMatrixWorld(true);
    const p = new THREE.Vector3(); built.core.getWorldPosition(p);
    if (p.z < minZ) minZ = p.z;
  }
  check('emitter never swings behind the station across the full sway', minZ > 0);
  console.log(`  -> min core world Z over 200 frames = ${minZ.toFixed(2)}`);
  built.group.rotation.set(0, 0, 0);
}

// ============================================================
// 6. The fire path must survive — it is wired to live fleet events.
// ============================================================
{
  const b2 = buildStation(4242);
  check('sl materials start fully transparent (nothing visible until fired)',
    b2.sl.mainMat.opacity === 0 && b2.sl.glowMat.opacity === 0 && b2.sl.rimMat.opacity === 0);
  check('sl lance meshes exist and are bloom-tagged',
    b2.sl.flare.isMesh && b2.sl.flare.layers.isEnabled(BLOOM_LAYER));
}

// ============================================================
// 7. Public API surface.
// ============================================================
check('Armada exposes fireSuperlaser', typeof Armada.fireSuperlaser === 'function');
// superlaserState is a GETTER (returns a status object), not a method --
// my first harness asserted typeof === 'function' and failed against
// correct code. Assert the shape it actually returns.
{
  // The getter returns null when no station is attached, which is the
  // correct answer in this sandbox: init() is never called, so _station
  // is unset. Asserting non-null here failed against correct code -- the
  // contract is "null OR a state object", never undefined/throwing.
  const st = Armada.superlaserState;
  check('Armada.superlaserState getter is reachable and null-safe pre-init',
    st === null || (typeof st === 'object' && 'state' in st));
  check('Armada exposes hasStation', typeof Armada.hasStation === 'boolean');
  console.log(`  -> superlaserState pre-init = ${JSON.stringify(st)}, hasStation = ${Armada.hasStation}`);
}

console.log('');
if (failures) { console.log(`${failures} FAILURE(S)`); process.exit(1); }
console.log('ALL CHECKS PASSED');
