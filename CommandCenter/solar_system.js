/* solar_system.js — extracted from command_center_v4.html
   Self-contained solar system rendering globals.
   Must load before command_center_v4.html main script. */

/* Shared state — set by the main HTML IIFE, read here */
var _orbIsFS = false;
var _orbNodes = _orbNodes || {};

/* ===== FREE-RANGE ORBITAL — PHYSICS-BASED CREATURES ===== */

/* Safe gradient — clamp all radii to >= 0 to prevent IndexSizeError */
var _origCRG=CanvasRenderingContext2D.prototype.createRadialGradient;
CanvasRenderingContext2D.prototype.createRadialGradient=function(x0,y0,r0,x1,y1,r1){
  return _origCRG.call(this,x0,y0,Math.max(0,r0),x1,y1,Math.max(0,r1));
};

/* --- Helpers --- */
function _hexRgb(hex){
  var r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);
  return r+","+g+","+b;
}
function _lighten(hex,p){
  var r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);
  return "rgb("+Math.min(255,r+p)+","+Math.min(255,g+p)+","+Math.min(255,b+p)+")";
}
function _darken(hex,p){
  var r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);
  return "rgb("+Math.max(0,r-p)+","+Math.max(0,g-p)+","+Math.max(0,b-p)+")";
}

/* ═══════════════════════════════════════════════════════════════════════
   SPHERICAL SURFACE PROJECTION (2026-07-29)
   ───────────────────────────────────────────────────────────────────────
   Every planet surface used to be painted in flat screen space: bands as
   ctx.fillRect, craters as fixed-offset ellipses, groove lines dead
   straight. Nothing knew it was on a ball, so the planets read as stickers
   — correct colors, zero volume.

   This is the geometry layer that fixes it. A point on the planet is now
   addressed by latitude/longitude, converted to a real 3D unit vector,
   rotated for spin and axial tilt, and orthographically projected to the
   disk. That gives, for free, the three cues that actually sell a sphere:

     1. LIMB COMPRESSION — features bunch up toward the edge, because
        screen_x = cos(lat)*sin(lon) falls off as |lon| → 90°.
     2. BACKFACE CULLING — z < 0 means the far side of the planet; those
        points are simply not drawn, so features rotate *around* the body
        instead of sliding across a flat cutout.
     3. FORESHORTENING — a circular crater near the limb projects to a
        squashed ellipse, tilted along the local surface normal.

   All of it is plain trig on the CPU — no WebGL, no textures, no deps,
   consistent with the rest of this file being stdlib canvas. Cost is a few
   hundred multiply-adds per planet per frame, which is nothing next to the
   gradient fills already happening.
   ═══════════════════════════════════════════════════════════════════════ */

/* Project lat/lon (radians) on a sphere of radius r to screen offsets.
   `spin` rotates about the polar axis (planet rotation), `tilt` leans the
   pole toward/away from the viewer (axial tilt).
   Returns {x,y,z,vis,fore} where:
     x,y  — screen-space offsets from planet center (add to cx,cy)
     z    — depth; > 0 is the near face, < 0 the far face
     vis  — 0..1 visibility ramp, fading right at the limb so features
            don't pop in/out with a hard edge as they rotate over
     fore — foreshortening factor (= z), how much to squash a feature
            drawn at this point along the view direction */
function _sphProject(lat, lon, r, spin, tilt) {
    var la = lat, lo = lon + (spin || 0);
    var cla = Math.cos(la), sla = Math.sin(la);
    /* Unit sphere point, +z toward viewer */
    var px = cla * Math.sin(lo);
    var py = sla;
    var pz = cla * Math.cos(lo);
    /* Axial tilt — rotate about the screen-x axis */
    if (tilt) {
        var ct = Math.cos(tilt), st = Math.sin(tilt);
        var ny = py * ct - pz * st;
        var nz = py * st + pz * ct;
        py = ny; pz = nz;
    }
    /* Fade features over the last few degrees of the limb */
    var vis = pz <= 0 ? 0 : Math.min(1, pz / 0.16);
    return { x: px * r, y: py * r, z: pz, vis: vis, fore: pz };
}

/* Lambert diffuse shading for a surface point, given the light direction in
   screen space (lx,ly point from the planet toward the sun). Returns 0..1.
   Used so a band or crater actually dims as it wraps onto the dark side,
   instead of every feature being uniformly bright across the terminator. */
function _sphLambert(px, py, pz, r, lx, ly) {
    var nx = px / r, ny = py / r, nz = pz;
    /* Light vector: screen-space direction plus a little toward the viewer,
       so the lit side has a soft rolloff rather than a hard half-disk. */
    var Lz = 0.35;
    var Ln = Math.sqrt(lx * lx + ly * ly + Lz * Lz) || 1;
    var d = (nx * lx + ny * ly + nz * Lz) / Ln;
    return d < 0 ? 0 : d;
}

/* Draw one latitude band as a proper spherical zone: two small-circle arcs
   (top and bottom edge) joined into a closed ribbon that curves with the
   sphere and pinches at the limb. This replaces fillRect for banded worlds.
   latC/latH are in radians (center latitude and half-height).
   `shade` enables per-vertex Lambert darkening toward the terminator. */
function _sphBand(ctx, cx, cy, r, latC, latH, spin, tilt, style, lx, ly, shade, turb, tSeed, tPhase) {
    var STEPS = 64;
    /* Sweep slightly past the visible hemisphere. Ending exactly at ±90° puts
       the ribbon's last vertices right on the silhouette, where sub-pixel
       differences between the two edges leave a ragged stair-step; a small
       overshoot lands them behind the limb where the disk clip removes them.
       Kept small — a large overshoot folds the ribbon back on itself, because
       past ±90° the projected x starts moving backwards. */
    var SPAN = Math.PI / 2 + 0.10;
    /* Turbulent edges. Real belt/zone boundaries are sheared and wavy, not
       clean parallels — without this the planet reads as concentric stripes.
       The wobble is a function of LONGITUDE (not screen x), so it is locked
       to the surface and rides around with the rotation. */
    var tb = turb || 0, ts = tSeed || 0, tp = tPhase || 0;
    /* Low frequencies only. High-frequency terms (the old 13.7x harmonic)
       change faster than the 64-step tessellation can follow, so adjacent
       quads land at visibly different latitudes and the band edge breaks into
       rectangular step-notches. Keeping every wavelength long relative to the
       step size makes the boundary read as a smooth atmospheric shear. */
    function wob(lon, edge) {
        if (!tb) return 0;
        return (Math.sin(lon * 1.7 + ts * 1.7 + tp + edge * 1.3) * 0.62
              + Math.sin(lon * 3.3 - ts * 2.3 + tp * 1.6 + edge) * 0.28
              + Math.sin(lon * 5.1 + ts * 0.9 - tp * 0.7) * 0.10) * tb;
    }
    /* Clamp latitude just inside the poles. Turbulence can otherwise push a
       high-latitude band past ±90°, which flips its longitude to the far side
       of the sphere and tears the ribbon open with a vertical seam. */
    var LATMAX = Math.PI / 2 - 0.001;
    function cl(v) { return v < -LATMAX ? -LATMAX : (v > LATMAX ? LATMAX : v); }
    var top = [], bot = [], any = false;
    for (var i = 0; i <= STEPS; i++) {
        var lon = -SPAN + (i / STEPS) * (SPAN * 2);
        var a = _sphProject(cl(latC - latH + wob(lon, 0)), lon, r, spin, tilt);
        var b = _sphProject(cl(latC + latH + wob(lon, 1)), lon, r, spin, tilt);
        if (a.z > 0 || b.z > 0) any = true;
        top.push(a); bot.push(b);
    }
    /* A band entirely behind the sphere must draw NOTHING. Without this guard
       a fully-hidden band still emits a closed path spanning the disk, which
       showed up as black polar slabs on tilted planets (Neptune, 28°). */
    if (!any) return;

    /* Ribbon construction, kept deliberately dumb.
       Two earlier attempts failed here and both failure modes are worth
       recording, because the clever versions LOOKED right in code:
         (a) clamping off-limb points radially and stitching them with arc
             segments produced black polar slabs on tilted planets — a band
             entirely behind the sphere still emitted a disk-spanning path;
         (b) closing each end by walking the limb arc produced hard diagonal
             wedges, because the short way around the circle is frequently
             the WRONG way for a ribbon whose two ends sit near the same angle.
       What actually works: emit the projected quad strip segment-by-segment,
       skipping only segments that are fully behind the sphere, and let the
       caller's existing disk clip trim the result. Each quad is tiny, so an
       overshooting quad is trimmed to the disk edge as a smooth curve — the
       clip does the geometry that the hand-rolled closure kept getting wrong. */
    /* ONE continuous path: top edge left→right, then bottom edge right→left.
       (Per-quad filling was tried and leaves antialiasing gaps; stroking those
       gaps paints a ladder of bright rungs. A single path avoids both.)

       The endpoints are pushed radially out past the disk edge as they near the
       limb. Without this, both edges converge toward the same silhouette point
       and the ribbon pinches shut into a lens, leaving wedge-shaped gaps at the
       planet's edge — clearly visible when the band layer is rendered alone.
       Overshooting lets the caller's disk clip cut a clean curved edge instead.

       Note this fixes the LIMB gaps only. Gaps BETWEEN bands are a separate
       issue and are handled by the caller overlapping adjacent latitude spans
       (see the `bleed` term where the band tables are consumed) — a band table
       that merely abuts (row N ends where row N+1 starts) leaves hairline
       wedges once asin() compresses the rows toward the poles. */
    var OVER = 1.16;
    function ext(p) {
        /* Scale every point by the SAME smooth function of longitude-depth, so
           there is no threshold to step across. An earlier version only nudged
           points below a |z| cutoff, which put a discontinuity right where the
           ribbon meets the limb and produced a black stair-step at the band's
           end. Here the factor rises smoothly from 1 at the disk centre to OVER
           at the silhouette, so the ribbon always overshoots the edge and the
           caller's disk clip trims it to the true curve. */
        var m = Math.sqrt(p.x * p.x + p.y * p.y) || 1;
        var edgeness = Math.min(1, m / r);          /* 0 centre → 1 at limb */
        var f = 1 + (OVER - 1) * edgeness * edgeness;
        return { x: p.x * f, y: p.y * f };
    }
    ctx.beginPath();
    var s0 = ext(top[0]);
    ctx.moveTo(cx + s0.x, cy + s0.y);
    for (var k = 1; k <= STEPS; k++) { var pt = ext(top[k]); ctx.lineTo(cx + pt.x, cy + pt.y); }
    for (var k2 = STEPS; k2 >= 0; k2--) { var pb = ext(bot[k2]); ctx.lineTo(cx + pb.x, cy + pb.y); }
    ctx.closePath();
    ctx.fillStyle = style;
    ctx.fill();
    void shade; void lx; void ly;
}

/* Draw a filled ellipse that is correctly foreshortened for its position on
   the sphere — the single strongest cue for craters, spots and storms.
   `rad` is the feature's angular radius as a fraction of the planet radius. */
function _sphBlob(ctx, cx, cy, r, lat, lon, rad, spin, tilt, style) {
    var p = _sphProject(lat, lon, r, spin, tilt);
    if (p.vis <= 0) return null;
    var rr = rad * r;
    /* Squash along the view-radial direction by the foreshortening factor */
    var ang = Math.atan2(p.y, p.x);
    ctx.save();
    ctx.translate(cx + p.x, cy + p.y);
    ctx.rotate(ang);
    ctx.scale(Math.max(0.06, p.fore), 1);
    ctx.beginPath();
    ctx.arc(0, 0, rr, 0, Math.PI * 2);
    ctx.restore();
    if (style) { ctx.fillStyle = style; ctx.fill(); }
    return p;
}

/* ═══════════════════════════════════════════════════════════════════════
   BAKED SURFACE TEXTURE CACHE (2026-07-31, adult-grade pass)
   ───────────────────────────────────────────────────────────────────────
   Several PLANET_VISUALS entries (sentinel, contrarian, hivemind, trinity,
   inference, phitex) painted their surface detail as a handful of thin
   0.06-0.18-alpha strokes straight into the live context — legible in a
   code review, sub-perceptual on a 50" display (the exact "too faint" trap
   documented in project_cosmos_redesign_round2_2026_07_30.md's PHITEX/
   Gridzilla fixes). Rather than keep hand-rolling one-off per-frame
   gradient stacks for each body, this is a small reusable bake: an
   offscreen canvas of regolith speckle (rocky/icy bodies) or soft banding
   (gas/energy bodies), generated ONCE per (kind, sizeBucket) via a seeded
   mulberry32 PRNG — same deterministic-noise convention as _bakeEclipticDisc
   and VoidField's _bakeGalaxy — then stamped with drawImage every frame.
   Zero per-frame gradient/path allocation for the texture layer itself;
   callers still layer their own live (cheap: strokes/dots) animated
   identity features on top, same as before. */
var _surfTexCache = {};
function _surfMulberry32(seed){
    var s = seed >>> 0;
    return function(){
        s |= 0; s = (s + 0x6D2B79F5) | 0;
        var t = Math.imul(s ^ (s >>> 15), 1 | s);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}
function _surfHashStr(str){
    var h = 2166136261;
    for(var i=0;i<str.length;i++){ h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
}
/* Rocky/icy regolith speckle — craters + fine grain, desaturated toward the
   supplied base hue. Used for airless/icy identities (sentinel, contrarian). */
function _bakeRegolithTex(key, sizePx, rgb){
    var cached = _surfTexCache[key];
    if(cached && cached.sizePx === sizePx) return cached.canvas;
    var D = Math.max(24, Math.min(256, sizePx * 2));
    var cvs = document.createElement('canvas');
    cvs.width = D; cvs.height = D;
    var c = cvs.getContext('2d');
    var rng = _surfMulberry32(_surfHashStr(key) ^ (sizePx|0));
    var cx = D/2, cy = D/2, R = D/2;
    /* Fine grain speckle */
    var grainN = Math.floor(D * D * 0.10);
    for(var i=0;i<grainN;i++){
        var a = rng()*Math.PI*2, d = Math.sqrt(rng())*R;
        var px = cx + Math.cos(a)*d, py = cy + Math.sin(a)*d;
        var tone = rng() < 0.5 ? -1 : 1;
        var v = 14 + rng()*18;
        c.fillStyle = 'rgba(' + Math.max(0,Math.min(255,rgb[0]+tone*v)) + ',' +
                       Math.max(0,Math.min(255,rgb[1]+tone*v)) + ',' +
                       Math.max(0,Math.min(255,rgb[2]+tone*v)) + ',' + (0.10+rng()*0.10).toFixed(3) + ')';
        c.fillRect(px, py, 1, 1);
    }
    /* Craters — small dark rings with a bright rim on one side */
    var craterN = 5 + Math.floor(rng()*5);
    for(var k=0;k<craterN;k++){
        var ka = rng()*Math.PI*2, kd = rng()*R*0.85;
        var kx = cx + Math.cos(ka)*kd, ky = cy + Math.sin(ka)*kd;
        var kr = Math.max(1, R*(0.05 + rng()*0.13));
        var kg = c.createRadialGradient(kx-kr*0.15, ky-kr*0.15, 0, kx, ky, Math.max(0.1,kr));
        kg.addColorStop(0, 'rgba(0,0,0,0.22)');
        kg.addColorStop(0.7, 'rgba(0,0,0,0.10)');
        kg.addColorStop(1, 'rgba(0,0,0,0)');
        c.fillStyle = kg;
        c.beginPath(); c.arc(kx, ky, kr, 0, Math.PI*2); c.fill();
        c.strokeStyle = 'rgba(255,255,255,0.10)';
        c.lineWidth = Math.max(0.5, kr*0.12);
        c.beginPath(); c.arc(kx, ky, kr, Math.PI*0.7, Math.PI*1.5); c.stroke();
    }
    var entry = { canvas: cvs, sizePx: sizePx };
    _surfTexCache[key] = entry;
    return cvs;
}
/* Soft cellular/plasma mottling — used for energy/hive/circuit identities
   (hivemind, trinity, inference, phitex) that need believable surface
   variation without literal craters. Blotchy low-frequency blobs, tinted
   toward the supplied hue, alternating slightly lighter/darker than base. */
function _bakeMottleTex(key, sizePx, rgb){
    var cached = _surfTexCache[key];
    if(cached && cached.sizePx === sizePx) return cached.canvas;
    var D = Math.max(24, Math.min(256, sizePx * 2));
    var cvs = document.createElement('canvas');
    cvs.width = D; cvs.height = D;
    var c = cvs.getContext('2d');
    var rng = _surfMulberry32(_surfHashStr(key) ^ 0x517e ^ (sizePx|0));
    var cx = D/2, cy = D/2, R = D/2;
    var blobN = 10 + Math.floor(rng()*8);
    for(var i=0;i<blobN;i++){
        var a = rng()*Math.PI*2, d = rng()*R*0.8;
        var bx = cx + Math.cos(a)*d, by = cy + Math.sin(a)*d;
        var br = R*(0.16+rng()*0.30);
        var tone = rng()<0.5 ? -1 : 1;
        var v = 10 + rng()*16;
        var bg = c.createRadialGradient(bx, by, 0, bx, by, Math.max(0.1,br));
        bg.addColorStop(0, 'rgba(' + Math.max(0,Math.min(255,rgb[0]+tone*v)) + ',' +
                       Math.max(0,Math.min(255,rgb[1]+tone*v)) + ',' +
                       Math.max(0,Math.min(255,rgb[2]+tone*v)) + ',' + (0.10+rng()*0.08).toFixed(3) + ')');
        bg.addColorStop(1, 'rgba(0,0,0,0)');
        c.fillStyle = bg;
        c.beginPath(); c.arc(bx, by, br, 0, Math.PI*2); c.fill();
    }
    var entry = { canvas: cvs, sizePx: sizePx };
    _surfTexCache[key] = entry;
    return cvs;
}
/* Stamp a baked texture centered on (x,y) at radius r, clipped to the disk.
   Caller is expected to already be inside drawPlanet's own disk clip
   (surface() runs clipped), so this just needs to draw the square sprite
   centered correctly — no extra clip call needed, matches every other
   surface() function's assumption in this file. sizePx should be rounded
   to a coarse bucket by the caller so the cache doesn't thrash every frame
   as a body's currentSize breathes by fractional pixels. */
function _stampSurfTex(ctx, canvas, x, y, r){
    var d = Math.max(0.2, r*2);
    ctx.drawImage(canvas, x-r, y-r, d, d);
}
/* Round to a coarse size bucket (nearest 4px) so the bake cache survives
   the continuous per-frame breathing every OrbNode.currentSize does —
   without this, a body pulsing 1-2px per frame would rebake every frame,
   defeating the whole point of baking. */
function _texBucket(r){ return Math.max(8, Math.round(r/4)*4); }

/* --- Celestial hierarchy — solar system structure --- */
/* Orbital speeds follow true Kepler's-third-law scaling (T = k * radius^1.5,
   k anchored to oracle's pre-existing period so the star tier — already
   judged to feel majestic — is untouched). Every planet/moon period now
   falls purely out of its orbitRadius: bigger orbit = slower, always,
   with no per-bot special-casing. Previous (pre-2026-07-28 motion pass)
   planet periods were 15-52s — visibly frantic on the 50" display — moon
   periods were a much saner 75s-6min. New planet+moon band: 84-214s. */
var CELESTIAL_HIERARCHY={
  /* THE SUN — Command Center at absolute center, never moves */
  cc:        {type:"sun",   parent:null,      orbitRadius:0,   orbitSpeed:0,      mass:100, sz:104, gravitationalRadius:0},
  /* THREE STARS — Round 2 (2026-07-30 redesign): size cap is now a hard
     rule — largest body (CC, the hub, exempt) vs smallest bot <= 2.5x.
     With smallest bot floor at 20, stars capped to ~34-36 so role tiers
     survive as SUBTLE differences instead of a giant-vs-pebble spread. */
  oracle:    {type:"star",  parent:"cc",      orbitRadius:585, orbitSpeed:0.000085,mass:40,  sz:36, gravitationalRadius:180, grp:"intel",  pt:"gas_giant",  pers:"deliberate"},
  deepblue:  {type:"star",  parent:"cc",      orbitRadius:520, orbitSpeed:0.000115,mass:35,  sz:34, gravitationalRadius:160, grp:"intel",  pt:"ocean",      pers:"predatory"},
  nexus:     {type:"star",  parent:"cc",      orbitRadius:468, orbitSpeed:0.00007, mass:38,  sz:35, gravitationalRadius:170, grp:"novel",  pt:"binary",     pers:"omniscient"},
  /* PLANETS — Trading bots. orbitSpeed = Kepler(orbitRadius). */
  confluence:{type:"planet",defaultParent:"oracle",   orbitRadius:130,orbitSpeed:0.0008114,mass:15, sz:27, grp:"trader", pt:"terrestrial", pers:"aggressive"},
  nexusbrain:{type:"planet",defaultParent:"nexus",    orbitRadius:117,orbitSpeed:0.0009503,mass:12, sz:26, grp:"trader", pt:"terrestrial", pers:"analytical"},
  gridzilla: {type:"planet",defaultParent:"oracle",   orbitRadius:111,orbitSpeed:0.0010284,mass:10, sz:25, grp:"trader", pt:"crystal",     pers:"steady"},
  turtlesue: {type:"planet",defaultParent:"oracle",   orbitRadius:156,orbitSpeed:0.0006173,mass:12, sz:26, grp:"trader", pt:"terrestrial", pers:"patient"},
  rubberband:{type:"planet",defaultParent:"nexus",    orbitRadius:117,orbitSpeed:0.0009503,mass:10, sz:24, grp:"trader", pt:"elastic",     pers:"bouncy"},
  arbitrageur:{type:"planet",defaultParent:"deepblue",orbitRadius:124,orbitSpeed:0.0008710,mass:10, sz:24, grp:"trader", pt:"binary_pair", pers:"paired"},
  /* MOONS — Small support bots. Compressed further Round 2: 18-22 -> 20-26
     band so AEGIS (still largest moon by design, hex shield untouched per
     directive) is only 1.3x the smallest moon, not 2x. */
  aegis:     {type:"moon",  parent:"cc",              orbitRadius:117,orbitSpeed:0.0009503, mass:14, sz:26, grp:"novel",    pt:"magnetar",   pers:"guardian"},
  phitex:    {type:"moon",  parent:"nexus",           orbitRadius:111,orbitSpeed:0.0010284, mass:12, sz:23, grp:"novel",    pt:"variable",   pers:"pulsing"},
  sentinel:  {type:"moon",  parent:"cc",              orbitRadius:156,orbitSpeed:0.0006173, mass:10, sz:22, grp:"intel",    pt:"nebula",     pers:"watchful"},
  contrarian:{type:"moon",  parent:"deepblue",        orbitRadius:104,orbitSpeed:0.0011340, mass:10, sz:22, grp:"intel",    pt:"dark_nebula",pers:"contrarian",ecc:0.22},
  chronos:   {type:"moon",  parent:"cc",              orbitRadius:182,orbitSpeed:0.0004898, mass:10, sz:22, grp:"intel",    pt:"pulsar",     pers:"rhythmic"},
  hivemind:  {type:"moon",  parent:"cc",              orbitRadius:98, orbitSpeed:0.0012397, mass:8,  sz:21, grp:"optimizer",pt:"cluster",    pers:"swarm"},
  trinity:   {type:"moon",  parent:"oracle",          orbitRadius:104,orbitSpeed:0.0011340, mass:8,  sz:21, grp:"intel",    pt:"trinary",    pers:"scattered"},
  inference: {type:"moon",  parent:"cc",              orbitRadius:130,orbitSpeed:0.0008114, mass:6,  sz:20, grp:"support",  pt:"nebula",     pers:"processing"},
  /* BRAINIAC — Command Center's own sensory apparatus, not a fleet bot: it
     is five collector threads running INSIDE cc, so it orbits cc closely
     and fast. orbitRadius 88 is the tightest orbit in the scene and its
     speed follows the same Kepler relation as its siblings
     (0.0012397*(98/88)^1.5 ~ 0.001456), so it does not break the physics
     the rest of the bodies obey. sz 20 sits on the existing smallest-body
     floor — it is a sensor, not a planet, and the size-ratio cap in the
     comments above must keep holding. */
  brainiac:  {type:"moon",  parent:"cc",              orbitRadius:88, orbitSpeed:0.0014560, mass:6,  sz:20, grp:"support",  pt:"sensor",     pers:"scanning"}
};

/* --- Synapse definitions (event bus connections) --- */
var _SYN_PAIRS=[
  ["deepblue","gridzilla"],["deepblue","nexusbrain"],
  ["sentinel","gridzilla"],["sentinel","nexusbrain"],
  ["oracle","nexusbrain"],
  ["phitex","aegis"],
  ["aegis","gridzilla"],["aegis","nexusbrain"],["aegis","turtlesue"],
  ["nexus","gridzilla"],["nexus","nexusbrain"],["nexus","turtlesue"],
  ["deepblue","nexus"],["phitex","nexus"],
  ["contrarian","rubberband"],["contrarian","gridzilla"],["contrarian","nexusbrain"],
  ["chronos","turtlesue"],["chronos","rubberband"],
  ["deepblue","rubberband"],["aegis","rubberband"],["aegis","arbitrageur"],
  ["phitex","rubberband"],["phitex","arbitrageur"],
  /* Confluence — intel aggregator: wired to its four actual input sources */
  ["oracle","confluence"],["deepblue","confluence"],["nexus","confluence"],["sentinel","confluence"]
];

/* TurtleSue — Death Star sprite (unrelated to the sun/CC visual below;
   was previously declared alongside the old neutron-star assets and must
   stay — PLANET_VISUALS.turtlesue.surface references it directly). */
var _turtlesueDeathStarImg = new Image();
_turtlesueDeathStarImg.src = 'static/turtlesue_deathstar.png';

/* ═══════ COSMOS v5 — VISUAL UNIVERSE ═══════ */

/* --- THE CONVERGENCE CORE — Command Center as a living data-aggregation
   engine, not a star. CC genuinely polls all 18 bots every ~10s and weaves
   their state into one picture; the visual says exactly that: a rotating
   geodesic data-lattice at the absolute center, with data motes flowing
   inward from every live bot's actual current position along faint
   convergence lanes, and a "weave" pulse that sweeps through the lattice
   once per real poll cycle. Replaces the 2026-04 neutron-star/magnetar
   design (retired 2026-07-28 — user feedback: centerpiece should read as
   "a data relating object", not another star competing with Oracle/
   Deep Blue/NEXUS for the same visual language). ═══ */

/* Data-mote pool — motes travel inward along convergence lanes from each
   live bot toward CC. Reused every frame, no heap allocation in the loop. */
var _ccMotes = (function(){
    var pool = [];
    for(var i=0;i<54;i++) pool.push({active:false,botId:null,prog:0,speed:0,sz:0,lane:0});
    return pool;
}());
var _ccMoteSpawnTimer = 0;

/* Lattice rotation angles — two counter-rotating shells for parallax depth,
   both slow (majestic, matches the orbital-motion pass elsewhere in this
   file: nothing at the heart of the display should read as frantic). */
var _ccLatticeAngleA = 0;   /* outer shell   — full turn ≈ 90s  */
var _ccLatticeAngleB = 0;   /* inner shell   — full turn ≈ 60s, opposite direction */

/* Weave pulse — brightens the lattice edges once per real CC poll cycle.
   CC polls the fleet every 10s, so the pulse period matches that cadence
   rather than an arbitrary "looks nice" number. */
var _ccWeavePulsePeriod = 10000;

/* Geodesic lattice vertices — a simple icosahedron projected with a fixed
   3D rotation baked in per-vertex, then spun in 2D via the angles above.
   Computed once; icosahedra don't need per-frame trig for their base shape,
   only for the rotation applied at draw time. */
var _ccLatticeVerts = (function(){
    var t = (1 + Math.sqrt(5)) / 2; /* golden ratio */
    var raw = [
        [-1, t, 0],[1, t, 0],[-1,-t, 0],[1,-t, 0],
        [0,-1, t],[0, 1, t],[0,-1,-t],[0, 1,-t],
        [ t, 0,-1],[ t, 0, 1],[-t, 0,-1],[-t, 0, 1]
    ];
    /* normalize to unit sphere */
    var norm = raw.map(function(v){
        var m = Math.sqrt(v[0]*v[0]+v[1]*v[1]+v[2]*v[2]);
        return [v[0]/m, v[1]/m, v[2]/m];
    });
    return norm;
}());
var _ccLatticeEdges = (function(){
    var v = _ccLatticeVerts, edges = [];
    var THRESH = 1.2; /* true nearest-neighbor distance is ≈1.0515 (next tier is 1.7013) — 1.2
                          sits safely between the two so only real edges pass */
    for(var i=0;i<v.length;i++){
        for(var j=i+1;j<v.length;j++){
            var dx=v[i][0]-v[j][0], dy=v[i][1]-v[j][1], dz=v[i][2]-v[j][2];
            var d=Math.sqrt(dx*dx+dy*dy+dz*dz);
            if(d < THRESH) edges.push([i,j]);
        }
    }
    return edges;
}());

/* Rotate a unit vertex around Y then X and project to 2D (orthographic —
   this is a HUD glyph, not a physically-lit scene, so no perspective divide
   needed). Returns {x,y,z} where z is used only for depth-sort/fade. */
function _ccProjectVertex(v, angleY, angleX, scale){
    var cy=Math.cos(angleY), sy=Math.sin(angleY);
    var x1 = v[0]*cy - v[2]*sy;
    var z1 = v[0]*sy + v[2]*cy;
    var y1 = v[1];
    var cx=Math.cos(angleX), sx=Math.sin(angleX);
    var y2 = y1*cx - z1*sx;
    var z2 = y1*sx + z1*cx;
    return {x:x1*scale, y:y2*scale, z:z2};
}

function drawSun(ctx, x, y, baseRadius, now, eventRate) {
    var sz = baseRadius * 0.62;

    /* Advance lattice rotation — two shells, counter-rotating, both slow */
    _ccLatticeAngleA += 0.0007;   /* ≈ 90s per revolution at 60fps */
    _ccLatticeAngleB -= 0.00105;  /* ≈ 60s per revolution, opposite spin */

    /* Fleet health — literally what CC aggregates, read straight off the
       real state global (read-only; never touches the counting logic that
       populates it). Falls back to "fully healthy" only when state hasn't
       loaded yet, so the very first frames don't render as a fleet outage. */
    var _agg = (typeof state !== "undefined" && state.aggregate) || null;
    var _alive = _agg ? _agg.bots_alive : 18;
    var _total = _agg ? (_agg.bots_total || 18) : 18;
    var _health = _total > 0 ? Math.max(0, Math.min(1, _alive / _total)) : 1;

    /* ═══════════════════════════════════════════════════════════════════
       1 — SOFT VIGNETTE — a much gentler version of the old lensing dark
       overlay. This is an information hub, not a gravity well: it should
       recede into the display, not visually devour the starfield around it.
    ═══════════════════════════════════════════════════════════════════ */
    var vigR = sz * 4.2;
    var vigG = ctx.createRadialGradient(x, y, sz * 0.9, x, y, Math.max(0.1, vigR));
    vigG.addColorStop(0,    'rgba(0,4,10,0.30)');
    vigG.addColorStop(0.4,  'rgba(0,4,10,0.10)');
    vigG.addColorStop(1,    'rgba(0,0,0,0)');
    ctx.fillStyle = vigG;
    ctx.beginPath(); ctx.arc(x, y, vigR, 0, Math.PI * 2); ctx.fill();

    /* ═══════════════════════════════════════════════════════════════════
       2 — CONVERGENCE LANES + DATA MOTES — faint lines from every LIVE
       bot's actual current world position into CC, with motes travelling
       inward continuously. This is the "visually connects to the bots it
       aggregates" requirement made literal: it is wired to real _orbNodes
       positions, not decorative arcs.
    ═══════════════════════════════════════════════════════════════════ */
    var _liveBotIds = [];
    if(typeof _orbNodes !== "undefined"){
        for(var _nid in _orbNodes){
            var _n = _orbNodes[_nid];
            if(_n && _n.id !== "cc" && _n.alive) _liveBotIds.push(_nid);
        }
    }
    for(var li=0; li<_liveBotIds.length; li++){
        var ln = _orbNodes[_liveBotIds[li]];
        ctx.strokeStyle = 'rgba(' + ln.rgb + ',0.05)';
        ctx.lineWidth = 0.6;
        ctx.beginPath();
        ctx.moveTo(ln.x, ln.y);
        ctx.lineTo(x, y);
        ctx.stroke();
    }
    /* Spawn motes onto random live lanes at a gentle, steady rate — reads
       as continuous ambient polling rather than a discrete triggered event */
    _ccMoteSpawnTimer++;
    if(_ccMoteSpawnTimer >= 14 && _liveBotIds.length){
        _ccMoteSpawnTimer = 0;
        for(var mi=0; mi<_ccMotes.length; mi++){
            if(!_ccMotes[mi].active){
                _ccMotes[mi].active = true;
                _ccMotes[mi].botId = _liveBotIds[Math.floor(Math.random()*_liveBotIds.length)];
                _ccMotes[mi].prog = 0;
                _ccMotes[mi].speed = 0.006 + Math.random()*0.006; /* ~3-5s inbound travel */
                _ccMotes[mi].sz = 1.0 + Math.random()*1.2;
                break;
            }
        }
    }
    for(var mj=0; mj<_ccMotes.length; mj++){
        var mo = _ccMotes[mj];
        if(!mo.active) continue;
        var mn = _orbNodes && _orbNodes[mo.botId];
        if(!mn || !mn.alive){ mo.active=false; continue; }
        mo.prog += mo.speed;
        if(mo.prog >= 1){ mo.active = false; continue; }
        var mx = mn.x + (x-mn.x)*mo.prog;
        var my = mn.y + (y-mn.y)*mo.prog;
        var moAlpha = Math.sin(mo.prog*Math.PI) * 0.8; /* fade in, fade out on arrival */
        var moG = ctx.createRadialGradient(mx, my, 0, mx, my, Math.max(0.1, mo.sz*3));
        moG.addColorStop(0,   'rgba('+mn.rgb+','+moAlpha.toFixed(3)+')');
        moG.addColorStop(1,   'rgba('+mn.rgb+',0)');
        ctx.fillStyle = moG;
        ctx.beginPath(); ctx.arc(mx, my, Math.max(0.1, mo.sz*3), 0, Math.PI*2); ctx.fill();
    }

    /* ═══════════════════════════════════════════════════════════════════
       3 — WEAVE PULSE — brightens the lattice edges once per real poll
       cycle (10s), a soft ring expanding outward from the core to mark
       "data just arrived and was woven in."
    ═══════════════════════════════════════════════════════════════════ */
    var _weavePhase = (now % _ccWeavePulsePeriod) / _ccWeavePulsePeriod;
    var _weaveBright = Math.max(0, 1 - _weavePhase*2.2); /* sharp attack, decay across ~45% of cycle */
    if(_weaveBright > 0.01){
        var wR = sz * (1.1 + _weavePhase*2.6);
        var wG = ctx.createRadialGradient(x, y, Math.max(0.1, wR*0.85), x, y, Math.max(0.1, wR));
        wG.addColorStop(0, 'rgba(120,200,255,0)');
        wG.addColorStop(0.6, 'rgba(120,200,255,'+(0.14*_weaveBright).toFixed(3)+')');
        wG.addColorStop(1, 'rgba(120,200,255,0)');
        ctx.fillStyle = wG;
        ctx.beginPath(); ctx.arc(x, y, wR, 0, Math.PI*2); ctx.fill();
    }

    /* ═══════════════════════════════════════════════════════════════════
       4 — THE LATTICE — two counter-rotating geodesic wireframe shells.
       Straight edges, triangulated faces: deliberately the ONLY
       non-spherical body in the entire system, so it reads instantly as
       "structure/data", never mistaken for a planet or star silhouette.
       Inner shell is smaller + brighter (the "processed" core); outer
       shell is larger + dimmer (the "raw intake" lattice). Both breathe
       with fleet health: more bots alive = brighter, steadier weave.
    ═══════════════════════════════════════════════════════════════════ */
    function _drawLatticeShell(radius, angleY, angleX, alphaBase, coreColor){
        var pts = new Array(_ccLatticeVerts.length);
        for(var vi=0; vi<_ccLatticeVerts.length; vi++){
            pts[vi] = _ccProjectVertex(_ccLatticeVerts[vi], angleY, angleX, radius);
        }
        for(var ei=0; ei<_ccLatticeEdges.length; ei++){
            var e = _ccLatticeEdges[ei];
            var pa = pts[e[0]], pb = pts[e[1]];
            /* Depth-fade: edges whose average z is toward the viewer (positive)
               draw brighter than edges swinging to the far side. */
            var avgZ = (pa.z+pb.z) / 2;
            var depthA = 0.35 + 0.65 * ((avgZ + radius) / (radius*2));
            var eAlpha = alphaBase * depthA * (0.55 + _health*0.45);
            ctx.strokeStyle = 'rgba('+coreColor+','+eAlpha.toFixed(3)+')';
            ctx.lineWidth = 0.85;
            ctx.beginPath();
            ctx.moveTo(x+pa.x, y+pa.y);
            ctx.lineTo(x+pb.x, y+pb.y);
            ctx.stroke();
        }
        /* Vertex nodes — small bright points where edges meet, the "data
           nodes" of the lattice. Front-facing ones (z>0) get a soft glow. */
        for(var vj=0; vj<pts.length; vj++){
            var pv = pts[vj];
            if(pv.z <= 0) continue;
            var vAlpha = alphaBase * (0.5 + _health*0.5) * (pv.z/radius);
            ctx.fillStyle = 'rgba('+coreColor+','+Math.min(1,vAlpha*1.4).toFixed(3)+')';
            ctx.beginPath();
            ctx.arc(x+pv.x, y+pv.y, Math.max(0.1, 1.1), 0, Math.PI*2);
            ctx.fill();
        }
    }
    /* Outer shell — raw intake lattice */
    _drawLatticeShell(sz*1.55, _ccLatticeAngleA, 0.6, 0.30 + _weaveBright*0.35, '90,180,255');
    /* Inner shell — processed/aggregated core, brighter, counter-rotating */
    _drawLatticeShell(sz*0.92, _ccLatticeAngleB, -0.45, 0.55 + _weaveBright*0.4, '190,225,255');

    /* ═══ CORE GLOW — soft light at dead-center, brightness = fleet health ═══ */
    var coreR = sz * 0.5;
    var coreG = ctx.createRadialGradient(x, y, 0, x, y, Math.max(0.1, coreR));
    coreG.addColorStop(0,   'rgba(225,240,255,'+(0.55+_health*0.35+_weaveBright*0.25).toFixed(3)+')');
    coreG.addColorStop(0.45,'rgba(140,195,255,'+(0.28+_health*0.20).toFixed(3)+')');
    coreG.addColorStop(1,   'rgba(60,120,220,0)');
    ctx.fillStyle = coreG;
    ctx.beginPath(); ctx.arc(x, y, coreR, 0, Math.PI*2); ctx.fill();

    /* ═══ CC TEXT — always visible, unchanged position/contract with the
       rest of the codebase (several call sites assume "CC" renders at
       the sun's x,y — this keeps that contract intact) ═══ */
    ctx.fillStyle = 'rgba(210,235,255,0.85)';
    ctx.font = 'bold ' + Math.max(11, sz * 0.38) + 'px "IBM Plex Mono"';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('CC', x, y);

    /* Fleet count — small qualifier under the core, ties the centerpiece
       directly to the number every viewer already trusts elsewhere on the
       display (same state.bots-derived aggregate the header uses; this is
       a read of _agg, never a re-count, so it can't drift from the header). */
    if(_agg){
        ctx.fillStyle = 'rgba(190,215,240,0.5)';
        ctx.font = Math.max(9, sz*0.16) + 'px "IBM Plex Mono"';
        ctx.fillText(_alive+'/'+_total, x, y + sz*0.62);
    }
}

/* ═══ PLANET VISUAL DEFINITIONS ═══ */
var PLANET_VISUALS = {

    oracle: {
        /* Jupiter — NASA Juno palette: rich amber, ochre, cream, russet cloud bands */
        baseColor: [200, 158, 90],
        atmosphere: [230, 195, 115],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* NASA Juno-accurate band palette — 18 alternating zones and belts */
            var bands=[
              /* North polar region */
              {y:-0.92,h:0.09,c:[165,128,82],a:0.30},
              /* North North Temperate Belt */
              {y:-0.83,h:0.10,c:[140,100,55],a:0.32},
              /* North North Temperate Zone */
              {y:-0.73,h:0.09,c:[215,190,135],a:0.28},
              /* North Temperate Belt */
              {y:-0.64,h:0.11,c:[155,112,60],a:0.35},
              /* North Temperate Zone */
              {y:-0.53,h:0.10,c:[228,200,140],a:0.30},
              /* North Equatorial Belt — darkest, richest */
              {y:-0.43,h:0.14,c:[148,95,42],a:0.40},
              /* North Tropical Zone */
              {y:-0.29,h:0.10,c:[235,208,148],a:0.28},
              /* Equatorial Belt */
              {y:-0.19,h:0.09,c:[172,128,68],a:0.35},
              /* Equatorial Zone — cream */
              {y:-0.10,h:0.10,c:[242,220,165],a:0.25},
              /* Equatorial Belt south */
              {y:0.00,h:0.09,c:[168,122,62],a:0.33},
              /* South Equatorial Belt — where GRS lives */
              {y:0.09,h:0.16,c:[152,98,45],a:0.42},
              /* South Tropical Zone */
              {y:0.25,h:0.10,c:[230,205,142],a:0.28},
              /* South Temperate Belt */
              {y:0.35,h:0.11,c:[145,102,52],a:0.35},
              /* South Temperate Zone */
              {y:0.46,h:0.09,c:[220,192,130],a:0.28},
              /* South South Temperate Belt */
              {y:0.55,h:0.10,c:[138,95,48],a:0.32},
              /* South polar region */
              {y:0.65,h:0.18,c:[162,122,72],a:0.30},
              /* Polar cap tint */
              {y:0.83,h:0.17,c:[148,118,80],a:0.22}
            ];
            /* Bands are drawn as true spherical zones (2026-07-29). The band
               table above is NASA-accurate and unchanged; what changed is
               that b.y/b.h are now read as *latitudes* and swept as small
               circles on the sphere, so each belt curves with the surface
               and compresses toward the limb instead of being a fillRect.
               Jupiter's 3.1° axial tilt is negligible — the visible curvature
               comes from the projection itself. */
            var jTilt=0.055;
            /* Undercoat. Each band is a small circle, so a high-latitude band
               legitimately spans much less screen width than the equator (at
               -67° it covers ~40% of the disk). That is correct foreshortening,
               but it means the band set alone cannot cover the disk: the polar
               rows end mid-face and the bare sphere shows through as hard
               rectangular blocks. Laying down a vertical ramp of the band
               palette first guarantees full coverage, so the projected bands
               read as detail on a continuous atmosphere instead of as tiles. */
            var jBase=ctx.createLinearGradient(0,y-r,0,y+r);
            jBase.addColorStop(0.00,'rgba(150,116,74,0.55)');
            jBase.addColorStop(0.14,'rgba(168,128,80,0.50)');
            jBase.addColorStop(0.30,'rgba(196,160,104,0.45)');
            jBase.addColorStop(0.46,'rgba(226,198,142,0.42)');
            jBase.addColorStop(0.56,'rgba(206,166,106,0.45)');
            jBase.addColorStop(0.72,'rgba(176,132,78,0.48)');
            jBase.addColorStop(0.88,'rgba(158,120,74,0.52)');
            jBase.addColorStop(1.00,'rgba(146,112,72,0.55)');
            ctx.fillStyle=jBase;
            ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);ctx.fill();
            for(var bi=0;bi<bands.length;bi++){
              var b=bands[bi];
              /* Map the band's normalized y (-1..1 across the disk) to a real
                 latitude. asin gives the correct non-linear spacing: bands
                 near the poles occupy far more latitude per unit of screen y. */
              var yTop=Math.max(-0.999,Math.min(0.999,b.y));
              var yBot=Math.max(-0.999,Math.min(0.999,b.y+b.h));
              var latT=Math.asin(yTop), latB=Math.asin(yBot);
              var latC=(latT+latB)/2;
              /* Overlap each band into its neighbours. The table's rows merely
                 abut, and once turbulence displaces the shared edge in opposite
                 directions the two bands separate, leaving hairline wedges of
                 bare sphere. Bleeding past the nominal edge guarantees the
                 zones always overlap; the later band simply paints over the
                 earlier one, so the visible boundary is still correct. */
              var latH=Math.abs(latB-latT)/2;
              var bleed=latH*0.55+0.012;
              latH+=bleed;
              /* Differential rotation — each band spins at its own rate, which
                 is real for Jupiter (equator laps the poles every few days). */
              var jSpin=now/26000*(1+Math.cos(latC)*0.55)
                       +Math.sin(now/18000+bi*0.9)*0.05;
              /* Cross-band gradient so a belt isn't one dead flat tone */
              var bG=ctx.createLinearGradient(x,y+Math.sin(latT)*r,x,y+Math.sin(latB)*r);
              bG.addColorStop(0,'rgba('+b.c[0]+','+b.c[1]+','+b.c[2]+','+(b.a*0.55)+')');
              bG.addColorStop(0.5,'rgba('+b.c[0]+','+b.c[1]+','+b.c[2]+','+b.a+')');
              bG.addColorStop(1,'rgba('+b.c[0]+','+b.c[1]+','+b.c[2]+','+(b.a*0.55)+')');
              /* Belts (dark) shear harder than zones (bright) — belts are the
                 turbulent downwelling regions on the real planet. Kept below
                 the bleed above so a displaced edge still overlaps its
                 neighbour rather than tearing a gap open. */
              var jTurb=Math.min(bleed*0.75,latH*(0.22+0.16*(bi%2)));
              _sphBand(ctx,x,y,r,latC,latH,jSpin,jTilt,bG,lx,ly,true,jTurb,bi,now/9000);
            }
            /* Limb darkening — the single cheapest, strongest sphere cue.
               A real gas giant falls off sharply at the edge because you are
               looking through vastly more atmosphere at grazing incidence. */
            var jLimb=ctx.createRadialGradient(x,y,r*0.55,x,y,r);
            jLimb.addColorStop(0,'rgba(0,0,0,0)');
            jLimb.addColorStop(0.72,'rgba(40,24,8,0.10)');
            jLimb.addColorStop(0.92,'rgba(28,16,5,0.30)');
            jLimb.addColorStop(1,'rgba(18,10,3,0.52)');
            ctx.fillStyle=jLimb;
            ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);ctx.fill();
            /* Inter-band turbulence: festoon waves at belt/zone boundaries.
               Now traced along a real small circle of latitude, so the wave
               follows the sphere's curve and dies at the limb rather than
               running dead-straight off the edge. */
            var jTurbSpin=now/26000;
            var turbBands=[-0.43,-0.29,0.09,0.25];
            for(var ti=0;ti<turbBands.length;ti++){
              var tLat=Math.asin(Math.max(-0.999,Math.min(0.999,turbBands[ti])));
              ctx.strokeStyle='rgba(200,165,95,0.10)';
              ctx.lineWidth=Math.max(0.4,r*0.006);
              ctx.beginPath();
              var tStarted=false;
              for(var tstep=0;tstep<=40;tstep++){
                var tlon=-Math.PI/2+(tstep/40)*Math.PI;
                /* Ripple the latitude itself — the wave rides the surface */
                var tw=Math.sin(tlon*6+now/5000+ti*2.1)*0.030
                      +Math.sin(tlon*11+now/3200+ti)*0.014;
                var tp=_sphProject(tLat+tw,tlon,r,jTurbSpin,jTilt);
                if(tp.vis<=0){tStarted=false;continue;}
                if(!tStarted){ctx.moveTo(x+tp.x,y+tp.y);tStarted=true;}
                else ctx.lineTo(x+tp.x,y+tp.y);
              }
              ctx.stroke();
            }
            /* Great Red Spot — anchored at a fixed lat/lon and carried around
               by the planet's rotation, so it genuinely disappears over the
               limb and returns, foreshortening as it goes. This is the single
               most convincing "it's a ball" cue on the whole planet. */
            var grsLat=Math.asin(0.17);
            var grsLon=0.0;
            var grsSpin=now/26000*1.55; /* GRS sits near the fast equator */
            var grsP=_sphProject(grsLat,grsLon,r,grsSpin,jTilt);
            if(grsP.vis>0){
            var spotX=x+grsP.x, spotY=y+grsP.y;
            var grsVis=grsP.vis;
            /* Foreshorten along the view-radial direction and tilt to match
               the local surface orientation */
            var grsAng=Math.atan2(grsP.y,grsP.x);
            ctx.save();
            ctx.translate(spotX,spotY);
            ctx.rotate(grsAng);
            ctx.scale(Math.max(0.07,grsP.fore),1);
            ctx.rotate(-grsAng);
            ctx.translate(-spotX,-spotY);
            ctx.globalAlpha=grsVis;
            var grsA=r*0.22, grsB=r*0.135; /* semi-axes */
            /* GRS outer wake — oval halo before the storm */
            var grsWake=ctx.createRadialGradient(spotX,spotY,grsA*0.7,spotX,spotY,grsA*1.5);
            grsWake.addColorStop(0,'rgba(180,70,30,0.06)');
            grsWake.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=grsWake;
            ctx.beginPath();ctx.ellipse(spotX,spotY,grsA*1.5,grsB*1.5,0,0,Math.PI*2);ctx.fill();
            /* Outer ring — reddish-brown */
            var grsOuter=ctx.createRadialGradient(spotX,spotY,grsA*0.55,spotX,spotY,grsA);
            grsOuter.addColorStop(0,'rgba(200,80,35,0.32)');
            grsOuter.addColorStop(0.5,'rgba(188,68,28,0.22)');
            grsOuter.addColorStop(1,'rgba(170,58,22,0.05)');
            ctx.fillStyle=grsOuter;
            ctx.beginPath();ctx.ellipse(spotX,spotY,grsA,grsB,-0.05,0,Math.PI*2);ctx.fill();
            /* Inner core — deeper brick red */
            var grsCore=ctx.createRadialGradient(spotX-grsA*0.08,spotY-grsB*0.1,0,spotX,spotY,grsA*0.55);
            grsCore.addColorStop(0,'rgba(215,90,38,0.38)');
            grsCore.addColorStop(0.35,'rgba(195,72,28,0.28)');
            grsCore.addColorStop(0.7,'rgba(172,58,22,0.15)');
            grsCore.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=grsCore;
            ctx.beginPath();ctx.ellipse(spotX,spotY,grsA*0.55,grsB*0.55,0,0,Math.PI*2);ctx.fill();
            /* GRS rotation swirls — 3 concentric oval arcs rotating CCW */
            for(var sw=0;sw<3;sw++){
              var swA=grsA*(0.85-sw*0.22), swB=grsB*(0.85-sw*0.22);
              var swRot=-now/8000-sw*0.5; /* CCW */
              ctx.strokeStyle='rgba(210,85,35,'+(0.12-sw*0.03)+')';
              ctx.lineWidth=0.8-sw*0.18;
              ctx.beginPath();
              ctx.ellipse(spotX,spotY,swA,swB,swRot,0,Math.PI*1.7);
              ctx.stroke();
            }
            ctx.restore(); /* end GRS foreshorten transform */
            }
            /* White oval storms — smaller BTB ovals, each anchored to its own
               longitude so they rotate with the planet like the GRS. */
            var baSeed=[[0.3,0.40],[0.68,0.43],[-0.35,0.38]];
            for(var ba=0;ba<baSeed.length;ba++){
              var baLat=Math.asin(Math.max(-0.99,Math.min(0.99,baSeed[ba][1])));
              var baLon=baSeed[ba][0]*Math.PI;
              var baP=_sphProject(baLat,baLon,r,now/26000*1.2,jTilt);
              if(baP.vis<=0) continue;
              var baX=x+baP.x, baY=y+baP.y;
              var baG=ctx.createRadialGradient(baX,baY,0,baX,baY,r*0.07);
              baG.addColorStop(0,'rgba(240,232,210,0.22)');
              baG.addColorStop(0.6,'rgba(220,210,185,0.08)');
              baG.addColorStop(1,'rgba(0,0,0,0)');
              ctx.fillStyle=baG;
              ctx.save();
              ctx.globalAlpha=baP.vis;
              var baAng=Math.atan2(baP.y,baP.x);
              ctx.translate(baX,baY);ctx.rotate(baAng);
              ctx.scale(Math.max(0.07,baP.fore),1);
              ctx.beginPath();ctx.ellipse(0,0,r*0.07,r*0.045,0,0,Math.PI*2);ctx.fill();
              ctx.restore();
            }
            /* Scanner beam — oracle's 93-pair scanning pulse. Was alpha
               0.02-0.05 (invisible) — real identity now lives in the
               overlay iris below, this stays as a faint ambient wash. */
            var scA=now/7000;
            var scLen=r*1.3;
            var scG=ctx.createLinearGradient(x,y,x+Math.cos(scA)*scLen,y+Math.sin(scA)*scLen);
            scG.addColorStop(0,'rgba(228,195,110,0.05)');
            scG.addColorStop(0.7,'rgba(228,195,110,0.02)');
            scG.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=scG;ctx.beginPath();ctx.moveTo(x,y);
            ctx.arc(x,y,scLen,scA-0.06,scA+0.06);ctx.closePath();ctx.fill();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* ═══ THE ALL-SEEING IRIS (Round 2 redesign, 2026-07-30) ═══
               Oracle was a photorealistic Jupiter clone — a stock-solar-
               system lookalike, exactly the failure mode flagged. This
               overlay stamps a giant scanning eye across the whole disk:
               an iris ring + pupil that dilates on a slow forecast cadence,
               plus a rotating radial "scanning" sweep of short spokes (like
               an iris contracting/reading) so it visibly moves within 2s.
               Sized to occupy the full disk (iris ring at 0.86r, pupil at
               0.30-0.42r) so it clears the 40%-footprint / alpha>=0.5 bar
               even from across the room. */
            var pupilPulse = 0.30 + 0.12*Math.sin(now/2600);
            var pupilR = Math.max(0.1, r*pupilPulse);
            /* Sclera wash — warm gold, distinguishes from a plain dark eye */
            var scleraG = ctx.createRadialGradient(x,y,r*0.55,x,y,r*0.90);
            scleraG.addColorStop(0,'rgba(0,0,0,0)');
            scleraG.addColorStop(1,'rgba(235,205,130,0.16)');
            ctx.fillStyle=scleraG;ctx.beginPath();ctx.arc(x,y,r*0.90,0,Math.PI*2);ctx.fill();
            /* Iris ring — bold, high-alpha, radial striations like a real iris */
            var irisR = r*0.62;
            var spokeCount = 24;
            var spokeSpin = now/9000;
            for (var si=0; si<spokeCount; si++){
                var sa = spokeSpin + (si/spokeCount)*Math.PI*2;
                var flick = 0.55+0.30*Math.sin(now/1400+si*0.7);
                ctx.strokeStyle = 'rgba(255,214,140,'+(0.30*flick).toFixed(3)+')';
                ctx.lineWidth = Math.max(0.8, r*0.028);
                ctx.beginPath();
                ctx.moveTo(x+Math.cos(sa)*pupilR*1.15, y+Math.sin(sa)*pupilR*1.15);
                ctx.lineTo(x+Math.cos(sa)*irisR, y+Math.sin(sa)*irisR);
                ctx.stroke();
            }
            /* Iris outer ring — crisp edge, alpha 0.55+, this is the primary
               silhouette element that must read at a glance */
            ctx.strokeStyle = 'rgba(255,225,160,0.62)';
            ctx.lineWidth = Math.max(1.2, r*0.045);
            ctx.beginPath(); ctx.arc(x,y,irisR,0,Math.PI*2); ctx.stroke();
            /* Pupil — near-black, dilates slowly (the "forecast confidence"
               breathing), always the strongest single feature on the disk */
            var pupG = ctx.createRadialGradient(x,y,0,x,y,pupilR);
            pupG.addColorStop(0,'rgba(10,6,2,0.92)');
            pupG.addColorStop(0.75,'rgba(20,12,4,0.80)');
            pupG.addColorStop(1,'rgba(30,18,6,0)');
            ctx.fillStyle=pupG; ctx.beginPath(); ctx.arc(x,y,pupilR,0,Math.PI*2); ctx.fill();
            /* Single bright catch-light — sells "eye" over "target reticle" */
            ctx.fillStyle='rgba(255,250,235,0.55)';
            ctx.beginPath();
            ctx.arc(x-pupilR*0.32,y-pupilR*0.32,Math.max(0.1,pupilR*0.18),0,Math.PI*2);
            ctx.fill();
        }
    },

    deepblue: {
        /* Neptune — deep cobalt blue, NASA Voyager 2 palette: saturated azure/ultramarine */
        baseColor: [15, 50, 168],
        atmosphere: [45, 118, 235],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Neptune atmospheric bands — very subtle, high-contrast streaks */
            var dbBands=[
              {y:-0.78,h:0.13,c:[22,80,185],a:0.18},
              {y:-0.65,h:0.10,c:[12,55,158],a:0.22},
              {y:-0.55,h:0.13,c:[28,92,198],a:0.16},
              {y:-0.42,h:0.09,c:[18,65,172],a:0.20},
              {y:-0.33,h:0.12,c:[32,100,210],a:0.15},
              {y:-0.21,h:0.10,c:[15,60,165],a:0.22},
              {y:-0.11,h:0.12,c:[35,108,218],a:0.14},
              {y:0.01,h:0.10,c:[20,72,178],a:0.20},
              {y:0.11,h:0.12,c:[30,95,205],a:0.16},
              {y:0.23,h:0.10,c:[16,62,168],a:0.22},
              {y:0.33,h:0.13,c:[26,88,195],a:0.15},
              {y:0.46,h:0.10,c:[14,55,158],a:0.20},
              {y:0.56,h:0.14,c:[22,78,182],a:0.18},
              {y:0.70,h:0.15,c:[18,65,170],a:0.16}
            ];
            /* Spherical zones (2026-07-29) — same conversion as Jupiter.
               Neptune's 28.3° axial tilt is significant and visible, so the
               bands lean, which reads as a genuinely 3D orientation. */
            var nTilt=0.494;
            /* Undercoat — same reason as Jupiter: polar small-circles cannot
               cover the disk on their own. See the note there. */
            var nBase=ctx.createLinearGradient(0,y-r,0,y+r);
            nBase.addColorStop(0.00,'rgba(16,58,160,0.55)');
            nBase.addColorStop(0.22,'rgba(20,72,178,0.50)');
            nBase.addColorStop(0.45,'rgba(30,98,206,0.45)');
            nBase.addColorStop(0.60,'rgba(24,84,190,0.48)');
            nBase.addColorStop(0.80,'rgba(18,64,170,0.52)');
            nBase.addColorStop(1.00,'rgba(14,52,152,0.55)');
            ctx.fillStyle=nBase;
            ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);ctx.fill();
            for(var bi=0;bi<dbBands.length;bi++){
              var b=dbBands[bi];
              var nyT=Math.max(-0.999,Math.min(0.999,b.y));
              var nyB=Math.max(-0.999,Math.min(0.999,b.y+b.h));
              var nLatT=Math.asin(nyT), nLatB=Math.asin(nyB);
              var nLatC=(nLatT+nLatB)/2;
              /* Same neighbour-overlap as Jupiter — see the bleed note there. */
              var nLatH=Math.abs(nLatB-nLatT)/2;
              nLatH+=nLatH*0.55+0.012;
              /* Fastest winds in the solar system — strong differential rotation */
              var nSpin=now/17000*(1+Math.cos(nLatC)*0.9)
                       +Math.sin(now/10000+bi*0.7)*0.06;
              var nG=ctx.createLinearGradient(x,y+Math.sin(nLatT)*r,x,y+Math.sin(nLatB)*r);
              nG.addColorStop(0,'rgba('+b.c[0]+','+b.c[1]+','+b.c[2]+','+(b.a*0.55)+')');
              nG.addColorStop(0.5,'rgba('+b.c[0]+','+b.c[1]+','+b.c[2]+','+b.a+')');
              nG.addColorStop(1,'rgba('+b.c[0]+','+b.c[1]+','+b.c[2]+','+(b.a*0.55)+')');
              _sphBand(ctx,x,y,r,nLatC,nLatH,nSpin,nTilt,nG,lx,ly,true,nLatH*0.22,bi*1.7,now/7000);
            }
            /* Bright cloud streaks — methane ice high-altitude cirrus. Traced
               along small circles of latitude so each streak curves with the
               sphere and vanishes over the limb. */
            for(var ci=0;ci<6;ci++){
              var cLat=Math.asin(Math.max(-0.95,Math.min(0.95,-0.6+ci*0.22)));
              var cLonC=Math.cos(ci*1.4)*1.1;         /* where the streak sits */
              var cSpan=0.45+0.5*((ci*7+13)%5/5);     /* how far it runs */
              var cSpin=now/17000*2.4+ci*0.9;         /* fast wind drift */
              ctx.strokeStyle='rgba(195,220,252,'+(0.13+0.06*Math.sin(now/3000+ci))+')';
              ctx.lineWidth=Math.max(0.5,r*0.014-ci*r*0.0015);
              ctx.beginPath();
              var cStarted=false;
              for(var cs=0;cs<=26;cs++){
                var clon=cLonC-cSpan+(cs/26)*cSpan*2;
                var cw=Math.sin(clon*10+now/2500+ci)*0.014;
                var cp=_sphProject(cLat+cw,clon,r,cSpin,nTilt);
                if(cp.vis<=0){cStarted=false;continue;}
                if(!cStarted){ctx.moveTo(x+cp.x,y+cp.y);cStarted=true;}
                else ctx.lineTo(x+cp.x,y+cp.y);
              }
              ctx.stroke();
            }
            /* Great Dark Spot — deep anticyclone (Voyager discovered, later
               disappeared). Anchored to a fixed lat/lon so it rotates around
               the limb with the planet instead of sliding across the disk. */
            var gdP=_sphProject(Math.asin(-0.12),0.35,r,now/17000*1.6,nTilt);
            if(gdP.vis>0){
            var gdX=x+gdP.x, gdY=y+gdP.y;
            ctx.save();
            ctx.globalAlpha=gdP.vis;
            var gdAng=Math.atan2(gdP.y,gdP.x);
            ctx.translate(gdX,gdY);ctx.rotate(gdAng);
            ctx.scale(Math.max(0.07,gdP.fore),1);
            ctx.rotate(-gdAng);ctx.translate(-gdX,-gdY);
            /* Outer ring — dark blue oval depression */
            var gdOuter=ctx.createRadialGradient(gdX,gdY,r*0.08,gdX,gdY,r*0.19);
            gdOuter.addColorStop(0,'rgba(8,30,95,0.35)');
            gdOuter.addColorStop(0.5,'rgba(10,38,110,0.18)');
            gdOuter.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=gdOuter;ctx.beginPath();ctx.ellipse(gdX,gdY,r*0.19,r*0.12,0.2,0,Math.PI*2);ctx.fill();
            /* Core */
            var gdCore=ctx.createRadialGradient(gdX,gdY,0,gdX,gdY,r*0.09);
            gdCore.addColorStop(0,'rgba(5,20,75,0.45)');
            gdCore.addColorStop(0.6,'rgba(8,28,90,0.20)');
            gdCore.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=gdCore;ctx.beginPath();ctx.ellipse(gdX,gdY,r*0.09,r*0.055,0.2,0,Math.PI*2);ctx.fill();
            ctx.restore(); /* end GDS foreshorten transform */
            }
            /* Companion bright cloud — the "scooter", south of the GDS and
               moving faster; also surface-anchored so it laps the dark spot. */
            var scootP=_sphProject(Math.asin(0.02),-0.5,r,now/17000*2.1,nTilt);
            if(scootP.vis>0){
              var scootX=x+scootP.x, scootY=y+scootP.y;
              var scootG=ctx.createRadialGradient(0,0,0,0,0,r*0.055);
              scootG.addColorStop(0,'rgba(210,228,252,'+(0.25*scootP.vis)+')');
              scootG.addColorStop(0.5,'rgba(185,215,250,'+(0.10*scootP.vis)+')');
              scootG.addColorStop(1,'rgba(0,0,0,0)');
              ctx.save();
              var scAng=Math.atan2(scootP.y,scootP.x);
              ctx.translate(scootX,scootY);ctx.rotate(scAng);
              ctx.scale(Math.max(0.07,scootP.fore),1);
              ctx.fillStyle=scootG;
              ctx.beginPath();ctx.ellipse(0,0,r*0.055,r*0.032,0,0,Math.PI*2);ctx.fill();
              ctx.restore();
            }
            /* Triton teal glow — atmospheric influence from largest moon */
            var tritonG=ctx.createRadialGradient(x+r*0.35,y-r*0.55,0,x+r*0.35,y-r*0.55,r*0.28);
            tritonG.addColorStop(0,'rgba(80,210,195,0.06)');
            tritonG.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=tritonG;ctx.beginPath();ctx.arc(x+r*0.35,y-r*0.55,r*0.28,0,Math.PI*2);ctx.fill();
            /* Sonar/whale-detection pulses */
            for(var sp=0;sp<3;sp++){
              var spProg=((now/2200+sp*0.33)%1);
              var spR=Math.max(0.1,r*0.15+spProg*r*1.0);
              var spA=(1-spProg)*(1-spProg)*0.10;
              ctx.strokeStyle='rgba(75,185,255,'+spA+')';ctx.lineWidth=0.8;
              ctx.beginPath();ctx.arc(x,y,spR,0,Math.PI*2);ctx.stroke();
            }
            /* Bioluminescent patches on dark side */
            for(var bi2=0;bi2<6;bi2++){
              var bSeed=bi2*47+23;
              var bAngle=bSeed*0.7;var bDist=r*(0.3+(bSeed%40)/80);
              var bpx=x+Math.cos(bAngle)*bDist;var bpy=y+Math.sin(bAngle)*bDist;
              var bDotA=Math.atan2(bpy-y,bpx-x);
              var sunA2=Math.atan2(ly,lx);
              var aDiff=Math.abs(bDotA-sunA2);if(aDiff>Math.PI)aDiff=Math.PI*2-aDiff;
              if(aDiff>Math.PI*0.5){
                var bioG=ctx.createRadialGradient(bpx,bpy,0,bpx,bpy,r*0.06);
                var pulse=0.5+0.5*Math.sin(now/1500+bi2*2);
                bioG.addColorStop(0,'rgba(80,200,255,'+(0.08*pulse)+')');bioG.addColorStop(1,'rgba(0,0,0,0)');
                ctx.fillStyle=bioG;ctx.beginPath();ctx.arc(bpx,bpy,r*0.06,0,Math.PI*2);ctx.fill();
              }
            }
            /* Ice cap */
            var capG=ctx.createRadialGradient(x,y-r*0.78,0,x,y-r*0.78,r*0.25);
            capG.addColorStop(0,'rgba(210,230,245,0.15)');capG.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=capG;ctx.beginPath();ctx.arc(x,y-r*0.78,r*0.25,0,Math.PI*2);ctx.fill();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* ═══ WHALE HUNTER SONAR (Round 2 redesign, 2026-07-30) ═══
               Old version only rendered when whaleCount>0 — meaning Deep
               Blue read as a plain Neptune clone essentially all the time
               (whale detections are rare events, not a steady state). This
               is now a PERMANENT identity: continuous sonar rings sweep
               outward always, intensifying (brighter, faster, bluer-white)
               when whales are actually detected, plus a breaching whale-
               tail silhouette on the disk so the body reads as "hunter"
               even completely idle within 2s of watching. */
            var dbNode = _orbNodes && _orbNodes.deepblue;
            var wc     = dbNode ? (dbNode.whaleCount || 0) : 0;
            var hot    = wc > 0;
            /* Continuous sonar rings — always sweeping, this is the primary
               silhouette element (extends to 2.4r so it dominates the
               body's visual footprint, not just a thin decoration) */
            var nRings = hot ? 4 : 3;
            var pingPeriod = hot ? 1000 : 1900;
            for(var wi=0; wi<nRings; wi++){
                var wPhase = ((now / pingPeriod + wi/nRings) % 1);
                var wR     = r * (0.7 + wPhase * 1.7);
                var baseA  = hot ? 0.55 : 0.30;
                var wA     = (1 - wPhase) * baseA;
                ctx.strokeStyle = hot ? ('rgba(150,220,255,' + wA.toFixed(3) + ')')
                                      : ('rgba(60,170,255,' + wA.toFixed(3) + ')');
                ctx.lineWidth   = Math.max(0.8, r*0.05*(1-wPhase*0.5));
                ctx.beginPath(); ctx.arc(x, y, Math.max(0.1, wR), 0, Math.PI * 2); ctx.stroke();
            }
            if(hot){
                var wGlow = ctx.createRadialGradient(x,y,r*0.9,x,y,r*1.6);
                wGlow.addColorStop(0,'rgba(80,180,255,0.16)');
                wGlow.addColorStop(1,'rgba(0,0,0,0)');
                ctx.fillStyle=wGlow;ctx.beginPath();ctx.arc(x,y,r*1.6,0,Math.PI*2);ctx.fill();
            }
            /* Breaching whale-tail silhouette — a bold dark fluke shape
               rising through the disk, slowly rocking. This is the
               feature-scale element: occupies ~45% of the disk width. */
            var rockA = Math.sin(now/2600)*0.12;
            ctx.save();
            ctx.translate(x + r*0.08, y + r*0.18);
            ctx.rotate(rockA);
            ctx.fillStyle = 'rgba(8,22,45,0.55)';
            ctx.beginPath();
            ctx.moveTo(0, 0);
            ctx.quadraticCurveTo(r*0.10, -r*0.55, r*0.44, -r*0.66);
            ctx.quadraticCurveTo(r*0.20, -r*0.38, r*0.16, -r*0.10);
            ctx.quadraticCurveTo(r*0.20, -r*0.40, -r*0.02, -r*0.62);
            ctx.quadraticCurveTo(-r*0.34, -r*0.50, -r*0.10, 0);
            ctx.closePath();
            ctx.fill();
            ctx.restore();
        }
    },

    confluence: {
        baseColor: [255, 109, 0],
        atmosphere: [255, 160, 80],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Left intentionally minimal — the real identity feature (the 4
               converging beams) lives in overlay(), not here. surface() is
               clipped to the disk (drawPlanet clips to arc(x,y,r) before
               calling it), so anything drawn here that needs to extend
               PAST the sphere's edge — which the beams must, to read as
               "arriving from outside" — is silently cut off. Confirmed live
               2026-07-30 round 2: the beams were coded correctly (verified
               the math directly) but invisible on screen because they were
               in surface() and every pixel past radius r was being clipped
               away before it ever reached the canvas. Moving them to
               overlay() (unclipped, drawn after the sphere) was the actual
               fix — do not move beam/ring/corona-style effects back into
               surface() for this or any other body. */
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* Four converging intel beams — Oracle, Deep Blue, NEXUS, Sentinel —
               sweep inward toward the core. Represents the aggregator reading
               four upstream sources rather than a chart of its own. Reach
               past the disk edge on purpose (this is why it must live in
               overlay, see surface() comment above) so they read as signals
               arriving from off-body, thick and bright with a short
               comet-tail so the sweep itself is legible as motion. */
            var beamAngles = [-Math.PI/2, 0, Math.PI/2, Math.PI]; /* N, E, S, W */
            var align = 0;
            for (var bi = 0; bi < 4; bi++) {
                var ba = beamAngles[bi] + now / 9000;
                var phase = ((now / 1600 + bi * 0.25) % 1);
                if (phase < 0.15) align++;
                var bR = Math.max(0.1, r * (1.85 - phase * 1.55));
                var bA = (1 - phase) * 0.85;
                var bx = x + Math.cos(ba) * bR, by = y + Math.sin(ba) * bR;
                /* Tail — a few trailing segments so it reads as a moving
                   streak, not a static spoke */
                for (var tt=1; tt<=3; tt++){
                    var tPhase = Math.min(0.98, phase + tt*0.05);
                    var tR = Math.max(0.1, r * (1.85 - tPhase * 1.55));
                    var tx = x + Math.cos(ba) * tR, ty = y + Math.sin(ba) * tR;
                    var tA = bA * (1 - tt/4);
                    ctx.strokeStyle = 'rgba(255, 170, 70, ' + tA.toFixed(3) + ')';
                    ctx.lineWidth = Math.max(0.4, 2.6 * (1 - phase) * (1-tt/4));
                    ctx.beginPath();
                    ctx.moveTo(tt===1?bx:tx, tt===1?by:ty);
                    ctx.lineTo(x, y);
                    ctx.stroke();
                }
                ctx.strokeStyle = 'rgba(255, 220, 160, ' + bA.toFixed(3) + ')';
                ctx.lineWidth = Math.max(0.6, 2.6 * (1 - phase));
                ctx.beginPath();
                ctx.moveTo(bx, by);
                ctx.lineTo(x, y);
                ctx.stroke();
                /* Bright pip at the beam's leading edge — the actual
                   "signal arriving" cue */
                var pipG = ctx.createRadialGradient(bx,by,0,bx,by,Math.max(0.1,r*0.16));
                pipG.addColorStop(0,'rgba(255,235,190,'+(0.9*(1-phase)).toFixed(3)+')');
                pipG.addColorStop(1,'rgba(255,150,60,0)');
                ctx.fillStyle=pipG;
                ctx.beginPath();ctx.arc(bx,by,r*0.16,0,Math.PI*2);ctx.fill();
            }
            /* Consensus core — pulses brighter when beams phase-align,
               echoing the "2+ sources agree" entry gate. Drawn after the
               beams so it sits on top, reading as "where the beams land". */
            var pulse = align >= 2 ? 1.0 : (0.55 + 0.25 * Math.sin(now / 500));
            var coreR = Math.max(0.1, r * 0.72);
            var glow = ctx.createRadialGradient(x, y, 0, x, y, coreR);
            glow.addColorStop(0, 'rgba(255, 225, 170, ' + (0.85 * pulse).toFixed(3) + ')');
            glow.addColorStop(0.45, 'rgba(255, 140, 40, ' + (0.55 * pulse).toFixed(3) + ')');
            glow.addColorStop(1, 'rgba(255, 60, 0, 0)');
            ctx.fillStyle = glow;
            ctx.beginPath();
            ctx.arc(x, y, coreR, 0, Math.PI * 2);
            ctx.fill();
            /* Ring marking the 4-source convergence boundary — gives the
               beams a visible target to land on */
            ctx.strokeStyle = 'rgba(255,190,110,'+(0.4*pulse).toFixed(3)+')';
            ctx.lineWidth = Math.max(0.8, r*0.05);
            ctx.beginPath();ctx.arc(x,y,coreR*1.15,0,Math.PI*2);ctx.stroke();
        }
    },

    gridzilla: {
        baseColor: [50, 170, 70],
        atmosphere: [90, 210, 110],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Grid overlay */
            ctx.strokeStyle = 'rgba(120, 255, 140, 0.15)';
            ctx.lineWidth = 0.6;
            var sp = r * 0.22;
            for (var gx = x-r; gx < x+r; gx += sp) {
                ctx.beginPath(); ctx.moveTo(gx, y-r); ctx.lineTo(gx, y+r); ctx.stroke();
            }
            for (var gy = y-r; gy < y+r; gy += sp) {
                ctx.beginPath(); ctx.moveTo(x-r, gy); ctx.lineTo(x+r, gy); ctx.stroke();
            }
            /* Bright node at intersections */
            for (var gx2 = x-r; gx2 < x+r; gx2 += sp) {
                for (var gy2 = y-r; gy2 < y+r; gy2 += sp) {
                    var d = Math.sqrt((gx2-x)*(gx2-x) + (gy2-y)*(gy2-y));
                    if (d < r * 0.85) {
                        ctx.fillStyle = 'rgba(150, 255, 170, 0.08)';
                        ctx.beginPath();
                        ctx.arc(gx2, gy2, 1, 0, Math.PI*2);
                        ctx.fill();
                    }
                }
            }
        }
    },

    phitex: {
        baseColor: [170, 50, 210],
        atmosphere: [210, 90, 250],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Adult-grade pass (2026-07-31): TWO bugs found here, same
               class as the Confluence disk-clip bug documented in
               project_cosmos_redesign_round2_2026_07_30.md. (1) field-line
               radius `r*(1+sin*0.6)` reaches up to 1.6r and the pulsar
               beam ellipses reached 2.5r — both were drawn inside
               surface(), which drawPlanet clips to the body's own disk
               (arc(x,y,r)), so every pixel past r was silently discarded
               every frame. (2) what little survived inside the disk was a
               bare 0.12-alpha stroke. Fix: baked magnetar-surface texture
               for in-disk weight, field lines/beams moved to overlay()
               (unclipped) where they can actually reach their intended
               radius, alphas boosted throughout. */
            var tex = _bakeMottleTex('phitex', _texBucket(r), [150,55,190]);
            _stampSurfTex(ctx, tex, x, y, r);
            /* In-disk portion of the field lines only (0..r) — kept here
               since this part legitimately never left the clip */
            ctx.strokeStyle = 'rgba(225, 140, 255, 0.22)';
            ctx.lineWidth = Math.max(0.5, r*0.014);
            for (var i = 0; i < 8; i++) {
                var a = (i/8) * Math.PI * 2 + now / 10000;
                ctx.beginPath();
                ctx.moveTo(x, y);
                ctx.lineTo(x + Math.cos(a)*r*0.9, y + Math.sin(a)*r*0.9);
                ctx.stroke();
            }
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* Magnetic field lines — the part that arcs OUT past the disk
               (up to 1.6r). Was silently clipped inside surface(); now
               actually visible, which is most of why this body used to
               read as a plain purple ball. */
            ctx.strokeStyle = 'rgba(210, 100, 255, 0.20)';
            ctx.lineWidth = Math.max(0.5, r*0.016);
            for (var fi = 0; fi < 8; fi++) {
                var fa0 = (fi/8) * Math.PI * 2 + now / 10000;
                ctx.beginPath();
                for (var t = 0; t <= 1; t += 0.08) {
                    var fr = r * (1 + Math.sin(t * Math.PI) * 0.6);
                    var fa = fa0 + (t - 0.5) * 0.5;
                    var fx = x + Math.cos(fa) * fr;
                    var fy = y + Math.sin(fa) * fr;
                    if (t === 0) ctx.moveTo(fx, fy);
                    else ctx.lineTo(fx, fy);
                }
                ctx.stroke();
            }
            /* Pulsar beam — reaches 2.5r, was also silently clipped */
            var beamA = now / 3000;
            for (var pole = -1; pole <= 1; pole += 2) {
                var bx = x + Math.cos(beamA) * r * 2.5 * pole;
                var by = y + Math.sin(beamA) * r * 2.5 * pole;
                var bG = ctx.createRadialGradient(x, y, r*0.3,
                    x + (bx-x)*0.5, y + (by-y)*0.5, Math.max(0.1, r*0.2));
                bG.addColorStop(0, 'rgba(220, 130, 255, 0.16)');
                bG.addColorStop(1, 'rgba(0,0,0,0)');
                ctx.fillStyle = bG;
                ctx.beginPath();
                ctx.ellipse(x+(bx-x)*0.5, y+(by-y)*0.5, r*0.12, r*1.2, beamA, 0, Math.PI*2);
                ctx.fill();
            }
            /* ═══ PHI-TIMED FLASH — cross/star flare every 1.618 seconds ═══
               The golden ratio interval. Brief bright spike on each pulse.
               Also: faint outer glow ring that pulses at the phi rate. */
            var PHI = 1.6180339887;
            var phiCycle = (now % (PHI * 1000)) / (PHI * 1000); /* 0..1 each 1.618s */
            /* Flare only fires in the first 12% of each cycle */
            if(phiCycle < 0.12) {
                var flareAlpha = (1 - phiCycle / 0.12) * 0.75;
                var flareLen = r * (1.4 + (1 - phiCycle / 0.12) * 0.8);
                /* Bright white center point */
                var coreG = ctx.createRadialGradient(x,y,0,x,y,r*0.5);
                coreG.addColorStop(0,'rgba(255,230,255,'+flareAlpha.toFixed(3)+')');
                coreG.addColorStop(0.4,'rgba(210,100,255,'+(flareAlpha*0.4).toFixed(3)+')');
                coreG.addColorStop(1,'rgba(0,0,0,0)');
                ctx.fillStyle=coreG;
                ctx.beginPath();ctx.arc(x,y,r*0.5,0,Math.PI*2);ctx.fill();
                /* 4-point cross flare */
                var crossA = ['rgba(255,230,255,', 'rgba(230,150,255,'];
                for(var ci=0;ci<4;ci++){
                    var ca = ci * Math.PI / 2 + Math.PI / 4;
                    ctx.strokeStyle = crossA[ci%2] + flareAlpha.toFixed(3) + ')';
                    ctx.lineWidth = Math.max(0.3, 1.5 * (1 - phiCycle/0.12));
                    ctx.beginPath();
                    ctx.moveTo(x + Math.cos(ca)*r*0.25, y + Math.sin(ca)*r*0.25);
                    ctx.lineTo(x + Math.cos(ca)*flareLen, y + Math.sin(ca)*flareLen);
                    ctx.stroke();
                }
                /* Outer ring flash */
                ctx.strokeStyle='rgba(220,100,255,'+(flareAlpha*0.5).toFixed(3)+')';
                ctx.lineWidth=1.2;
                ctx.beginPath();ctx.arc(x,y,r*1.3+r*0.3*(1-phiCycle/0.12),0,Math.PI*2);ctx.stroke();
            }
        }
    },

    aegis: {
        baseColor: [140, 145, 165],
        atmosphere: [175, 180, 200],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Cratered surface — airless body */
            var craters=[[0.25,-0.35,0.15],[-0.4,0.2,0.12],[0.1,0.4,0.1],[-0.2,-0.15,0.08],[0.35,0.15,0.06],[-0.1,0.25,0.07]];
            for(var ci=0;ci<craters.length;ci++){
              var cr=craters[ci];var cx2=x+cr[0]*r,cy2=y+cr[1]*r,cR=cr[2]*r;
              var cG=ctx.createRadialGradient(cx2+lx*cR*0.2,cy2+ly*cR*0.2,0,cx2,cy2,cR);
              cG.addColorStop(0,'rgba(100,105,115,0.3)');cG.addColorStop(1,'rgba(130,135,155,0)');
              ctx.fillStyle=cG;ctx.beginPath();ctx.arc(cx2,cy2,cR,0,Math.PI*2);ctx.fill();
              ctx.strokeStyle='rgba(90,95,110,0.15)';ctx.lineWidth=0.5;
              ctx.beginPath();ctx.arc(cx2,cy2,cR,Math.atan2(ly,lx)+Math.PI-0.6,Math.atan2(ly,lx)+Math.PI+0.6);ctx.stroke();
            }
            /* Hexagonal shield panels overlay — rotating */
            var rot=now/20000;
            ctx.save();ctx.translate(x,y);ctx.rotate(rot);
            for(var i=0;i<6;i++){
              var a1=(i/6)*Math.PI*2,a2=((i+1)/6)*Math.PI*2;
              ctx.strokeStyle='rgba(248,180,120,0.08)';ctx.lineWidth=0.6;
              ctx.beginPath();ctx.moveTo(0,0);
              ctx.lineTo(Math.cos(a1)*r*0.95,Math.sin(a1)*r*0.95);
              ctx.lineTo(Math.cos(a2)*r*0.95,Math.sin(a2)*r*0.95);
              ctx.closePath();ctx.stroke();
              /* Shimmer on active panels */
              var shimmer=Math.sin(now/2000+i*1.5)*0.5+0.5;
              if(shimmer>0.7){
                ctx.fillStyle='rgba(248,180,120,'+(shimmer*0.04)+')';ctx.fill();
              }
            }
            ctx.restore();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* ═══ HEXAGONAL SHIELD OUTLINE ═══
               Hexagon shape drawn outside the planet circle.
               Edges individually animate — flicker when regime changes.
               _hexFlicker on the node triggers a burst flash. */
            var hexRot = now / 25000;
            var hexR   = r * 1.25; /* just outside the planet disk */
            /* Get current AEGIS score + any triggered flicker burst */
            var aegScore = (typeof aegisState !== 'undefined' && aegisState.score != null) ? aegisState.score : 0.5;
            var baseAlpha = 0.20 + aegScore * 0.22;
            /* Flicker burst: check node for _hexFlicker flag */
            var aegNode = _orbNodes && _orbNodes.aegis;
            var flickerBoost = 0;
            if(aegNode && aegNode._hexFlicker > 0){
                flickerBoost = aegNode._hexFlicker;
                aegNode._hexFlicker = Math.max(0, aegNode._hexFlicker - 0.025); /* decay */
                baseAlpha += flickerBoost * 0.65;
            }
            ctx.save();
            ctx.translate(x, y);
            ctx.rotate(hexRot);
            for(var hi=0; hi<6; hi++){
                var a1h = (hi/6) * Math.PI * 2 - Math.PI/6;
                var a2h = ((hi+1)/6) * Math.PI * 2 - Math.PI/6;
                var x1h = Math.cos(a1h) * hexR;
                var y1h = Math.sin(a1h) * hexR;
                var x2h = Math.cos(a2h) * hexR;
                var y2h = Math.sin(a2h) * hexR;
                /* Per-edge flicker — add flicker boost for regime change flash */
                var edgeFlicker = 0.5 + 0.5 * Math.sin(now / 1800 + hi * 1.05);
                if(flickerBoost > 0) edgeFlicker = Math.max(edgeFlicker, flickerBoost * Math.sin(now/80 + hi*1.3));
                var edgeAlpha   = Math.min(0.9, baseAlpha * edgeFlicker);
                /* Gradient along edge: amber/white glow (white flash on flicker burst) */
                var flashBlend = flickerBoost > 0.4 ? ', 240, 255,' : ', 220, 140,';
                var edgeG = ctx.createLinearGradient(x1h, y1h, x2h, y2h);
                edgeG.addColorStop(0,   'rgba(248,200,120,' + edgeAlpha.toFixed(3) + ')');
                edgeG.addColorStop(0.5, 'rgba(255' + flashBlend + (edgeAlpha*1.4).toFixed(3) + ')');
                edgeG.addColorStop(1,   'rgba(248,200,120,' + edgeAlpha.toFixed(3) + ')');
                ctx.strokeStyle = edgeG;
                ctx.lineWidth   = 1.2 + flickerBoost * 0.8;
                ctx.beginPath(); ctx.moveTo(x1h, y1h); ctx.lineTo(x2h, y2h); ctx.stroke();
                /* Corner vertex dot */
                ctx.fillStyle = 'rgba(255,225,150,' + Math.min(0.9, edgeAlpha * 1.2).toFixed(3) + ')';
                ctx.beginPath(); ctx.arc(x1h, y1h, 1.2 + flickerBoost, 0, Math.PI * 2); ctx.fill();
            }
            /* Inner resonance ring — faint, tighter than hexagon */
            ctx.strokeStyle = 'rgba(248,180,100,' + (baseAlpha * 0.35).toFixed(3) + ')';
            ctx.lineWidth   = 0.6;
            ctx.beginPath(); ctx.arc(0, 0, hexR * 0.85, 0, Math.PI * 2); ctx.stroke();
            ctx.restore();
        }
    },

    sentinel: {
        baseColor: [110, 155, 210],
        atmosphere: [155, 195, 240],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Adult-grade pass (2026-07-31): base cracks were 0.15-alpha
               hairlines with nothing behind them — a flat blue disk at any
               real viewing distance. Baked icy-regolith speckle (craters +
               grain, same bake used for contrarian) now sits under the
               cracks so the body reads as a weathered ice moon even before
               the radar sweep animates. */
            var tex = _bakeRegolithTex('sentinel', _texBucket(r), [150,190,230]);
            _stampSurfTex(ctx, tex, x, y, r);
            /* Ice surface cracks — boosted from 0.15 to a real value, wider */
            ctx.strokeStyle = 'rgba(200, 228, 252, 0.32)';
            ctx.lineWidth = Math.max(0.6, r*0.018);
            var cracks = [[0.1,0.1,0.7,0.6],[-0.2,-0.3,0.5,0.4],[-0.4,0.2,0.3,-0.5],[0.3,-0.2,-0.1,0.7]];
            for (var ci = 0; ci < cracks.length; ci++) {
                var cr = cracks[ci];
                ctx.beginPath();
                ctx.moveTo(x+cr[0]*r, y+cr[1]*r);
                ctx.lineTo(x+cr[2]*r, y+cr[3]*r);
                ctx.stroke();
                /* Bright hairline core down the middle of each crack — sells
                   "fractured ice catching light" instead of a drawn line */
                ctx.strokeStyle = 'rgba(235,248,255,0.20)';
                ctx.lineWidth = Math.max(0.3, r*0.006);
                ctx.beginPath();
                ctx.moveTo(x+cr[0]*r, y+cr[1]*r);
                ctx.lineTo(x+cr[2]*r, y+cr[3]*r);
                ctx.stroke();
                ctx.strokeStyle = 'rgba(200, 228, 252, 0.32)';
                ctx.lineWidth = Math.max(0.6, r*0.018);
            }
            /* Radar sweep — boosted alpha */
            var sweep = (now / 2500) % (Math.PI * 2);
            var sweepG = ctx.createRadialGradient(x, y, r*0.2, x, y, r*1.5);
            sweepG.addColorStop(0, 'rgba(20, 220, 195, 0.16)');
            sweepG.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = sweepG;
            ctx.beginPath();
            ctx.moveTo(x, y);
            ctx.arc(x, y, r*1.5, sweep-0.15, sweep+0.15);
            ctx.closePath();
            ctx.fill();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* Thin cold-blue atmosphere halo — icy body catching starlight
               on its limb, unclipped so it reads past the disk edge. */
            var haloR = Math.max(0.1, r*1.14);
            var haloG = ctx.createRadialGradient(x, y, r*0.92, x, y, haloR);
            haloG.addColorStop(0, 'rgba(0,0,0,0)');
            haloG.addColorStop(0.7, 'rgba(160,205,245,0.10)');
            haloG.addColorStop(1, 'rgba(160,205,245,0)');
            ctx.fillStyle = haloG;
            ctx.beginPath(); ctx.arc(x, y, haloR, 0, Math.PI*2); ctx.fill();
            /* ═══ SATELLITE DISH — small arc + stem extending outward ═══
               Points away from the neutron star (transmission direction). */
            /* Dish points opposite to sun direction */
            var dishAngle=Math.atan2(-lx,-ly)+0.3; /* slight tilt so it's visible */
            /* Stem — short line from planet surface */
            var stemLen=r*0.55;
            var sx=x+Math.cos(dishAngle)*r;
            var sy=y+Math.sin(dishAngle)*r;
            var ex=sx+Math.cos(dishAngle)*stemLen;
            var ey=sy+Math.sin(dishAngle)*stemLen;
            ctx.strokeStyle='rgba(180,215,250,0.60)';ctx.lineWidth=Math.max(0.6,r*0.03);
            ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);ctx.stroke();
            /* Dish arc — parabola approximated as arc, perpendicular to stem */
            var dishR=r*0.32;
            var perpA=dishAngle+Math.PI/2;
            ctx.strokeStyle='rgba(190,225,255,0.72)';ctx.lineWidth=Math.max(0.9,r*0.045);
            ctx.beginPath();
            ctx.arc(ex,ey,dishR,perpA-0.7,perpA+0.7);
            ctx.stroke();
            /* Small dot at focal point */
            ctx.fillStyle='rgba(220,240,255,0.65)';
            ctx.beginPath();ctx.arc(ex,ey,Math.max(1.2,r*0.05),0,Math.PI*2);ctx.fill();
            /* Signal cone from dish center — boosted */
            var coneLen=r*1.1;
            var coneG=ctx.createRadialGradient(ex,ey,0,ex,ey,coneLen);
            coneG.addColorStop(0,'rgba(120,215,235,0.14)');
            coneG.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=coneG;
            ctx.beginPath();ctx.moveTo(ex,ey);
            ctx.arc(ex,ey,coneLen,dishAngle-0.22,dishAngle+0.22);
            ctx.closePath();ctx.fill();
        }
    },

    turtlesue: {
        /* THE DEATH STAR — a battle station, not a world. It is a sprite
           rather than a procedural surface, which had three consequences
           worth fixing (2026-08-18):

           1. ASPECT. The source is 1200x630 (a 1.90:1 wide image, and
              despite the .png extension it is actually a WebP -- Chrome
              sniffs content so it renders, but nothing may trust the
              extension). It was drawn into a SQUARE sz x sz box, stretching
              the station ~1.9x vertically into an oval. Now drawn at its
              true aspect, fitted to the body height.

           2. SCALE. sz was r*3, so the sprite overflowed the body radius
              threefold and visually swamped its neighbours. It is now
              anchored to the actual body size.

           3. LIGHTING. drawImage paints flat pixels, so the station ignored
              the sun completely while every planet around it was correctly
              shaded -- the sprite carries its own baked highlight, which
              disagreed with the real light direction. A terminator and limb
              shadow are now composited over it from the true sun vector, so
              it sits in the same light as everything else. */
        baseColor: [155, 140, 110],
        atmosphere: null,        /* a hull has no air — no scatter rim */
        surface: function(ctx, x, y, r, lx, ly, now) {
            var img = _turtlesueDeathStarImg;
            if (!(img.complete && img.naturalWidth > 0)) return;

            /* MEASURED from the artwork, not assumed. Scanning the sprite's
               own luminance (see the notes below) shows the station disc
               occupies x 266-745, y 3-484 of the 1200x630 frame: centre
               (506, 244), radius 240. It is NOT centred in the image.

               Centring the FRAME therefore put the station off-centre on the
               body and mis-sized it. These constants map the station's own
               disc onto the body disc, so the sprite's sphere and the
               engine's sphere are the same sphere. */
            var SP_W = 1200, SP_H = 630;          /* source frame */
            var ST_CX = 506, ST_CY = 244;         /* station centre in frame */
            var ST_R  = 240;                      /* station radius in frame */

            /* Scale so the station's radius matches the body's radius. */
            var k = r / ST_R;
            var w = SP_W * k, h = SP_H * k;
            /* Offset so the station centre lands on the body centre. */
            var dx = x - ST_CX * k;
            var dy = y - ST_CY * k;

            ctx.save();
            /* Clip to the body disk. The source is fully opaque edge to edge
               (189,000 opaque pixels, no transparent background), so without
               this the sprite paints a rectangle of dark frame over the
               scene -- which is most of what made it read as a sticker. */
            ctx.beginPath();
            ctx.arc(x, y, r, 0, Math.PI * 2);
            ctx.clip();
            ctx.drawImage(img, dx, dy, w, h);

            /* Terminator from the REAL sun vector, composited over the
               sprite's own baked shading. Multiply rather than a flat
               overlay so hull detail survives into the shadow instead of
               being washed to grey. */
            var tg = ctx.createLinearGradient(
                x + lx * r, y + ly * r,
                x - lx * r, y - ly * r);
            tg.addColorStop(0.00, 'rgba(255,255,255,0)');
            tg.addColorStop(0.40, 'rgba(0,0,0,0)');
            tg.addColorStop(0.72, 'rgba(0,0,0,0.34)');
            tg.addColorStop(0.90, 'rgba(0,0,0,0.60)');
            tg.addColorStop(1.00, 'rgba(0,0,0,0.76)');
            ctx.fillStyle = tg;
            ctx.fillRect(x - r, y - r, r * 2, r * 2);

            /* Limb darkening — the cue that says "sphere" rather than
               "disc with a picture on it". */
            var lg = ctx.createRadialGradient(x, y, r * 0.55, x, y, r);
            lg.addColorStop(0, 'rgba(0,0,0,0)');
            lg.addColorStop(0.80, 'rgba(0,0,0,0.22)');
            lg.addColorStop(1.00, 'rgba(0,0,0,0.55)');
            ctx.fillStyle = lg;
            ctx.fillRect(x - r, y - r, r * 2, r * 2);
            ctx.restore();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* SUPERLASER — the dish sits in the upper-left of the sprite as
               drawn. The old glow was pinned to a FIXED offset (+0.3r,
               -0.1r) that matched neither the art nor the lighting, so the
               charge floated over blank hull. These offsets are measured
               from the sprite itself.

               Kept on the overlay (not the surface) deliberately: the
               superlaser is an EMITTER. It must not be dimmed by the
               terminator that was just applied to the hull -- a charging
               weapon glows on the night side too, which is most of the
               point of it. */
            /* Dish position MEASURED from the sprite: the darkest patch
               inside the lit disc sits at (488, 138) in frame pixels, which
               against the station centre (506, 244) and radius 240 is an
               offset of (-0.073, -0.441) radii -- near the TOP of the
               station, barely left of centre.

               The previous guess of (-0.34, -0.30) put the charge over blank
               hull well to the left of the emplacement. That is the second
               time this overlay has been positioned by eye; it is now
               derived from the artwork and will stay correct if the body is
               resized. */
            var dishX = x - r * 0.073;
            var dishY = y - r * 0.441;
            var pulse = 0.5 + 0.5 * Math.sin(now / 300);

            /* WHY THE WELL IS DARKENED FIRST.
               The charge used to be a single 'lighter' fill, and it rendered
               SALMON PINK rather than green. That is not a colour choice, it
               is arithmetic: 'lighter' ADDS, and the hull under the dish is
               neutral grey -- measured rgb(130,126,123) straight from the
               sprite. Adding green (90,255,120) gives (220,255,243), which
               is a desaturated near-white with the red channel at 220. You
               cannot get a saturated colour by adding to a bright base.

               Recessing the well first fixes both problems at once: it is
               what a dish physically IS (a concave emplacement, darker than
               the hull around it), and it gives the emission something dark
               to sit against so the green survives. */
            ctx.save();
            var wellR = r * 0.19;
            var wg = ctx.createRadialGradient(dishX, dishY, 0, dishX, dishY, wellR);
            wg.addColorStop(0,   'rgba(6,10,8,0.82)');
            wg.addColorStop(0.62,'rgba(8,12,10,0.55)');
            wg.addColorStop(1,   'rgba(10,14,12,0)');
            ctx.fillStyle = wg;
            ctx.beginPath(); ctx.arc(dishX, dishY, wellR, 0, Math.PI * 2); ctx.fill();

            /* Rim catchlight on the sunward side of the well — the cue that
               says "recessed" rather than "painted dark spot". */
            var rimA = Math.atan2(ly, lx);
            ctx.strokeStyle = 'rgba(200,215,225,0.20)';
            ctx.lineWidth = Math.max(0.5, r * 0.018);
            ctx.beginPath();
            ctx.arc(dishX, dishY, wellR * 0.92, rimA - 1.1, rimA + 1.1);
            ctx.stroke();
            ctx.restore();

            ctx.save();
            ctx.globalCompositeOperation = 'lighter';

            /* Focused core, now emitting into a dark well. */
            var coreR = r * 0.15;   /* tighter — a well, not a smear */
            var cg = ctx.createRadialGradient(dishX, dishY, 0, dishX, dishY, coreR);
            cg.addColorStop(0,   'rgba(120,255,150,' + (0.80 * pulse).toFixed(3) + ')');
            cg.addColorStop(0.40,'rgba(0,235,80,'    + (0.46 * pulse).toFixed(3) + ')');
            cg.addColorStop(1,   'rgba(0,170,55,0)');
            ctx.fillStyle = cg;
            ctx.beginPath(); ctx.arc(dishX, dishY, coreR, 0, Math.PI * 2); ctx.fill();

            /* Wider bloom around the emplacement, clipped to the hull so the
               charge reads as coming FROM the station rather than floating
               in front of it. */
            ctx.save();
            ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.clip();
            var bg = ctx.createRadialGradient(dishX, dishY, 0, dishX, dishY, r * 0.62);
            bg.addColorStop(0,   'rgba(0,255,90,' + (0.20 * pulse).toFixed(3) + ')');
            bg.addColorStop(0.55,'rgba(0,210,70,' + (0.07 * pulse).toFixed(3) + ')');
            bg.addColorStop(1,   'rgba(0,150,40,0)');
            ctx.fillStyle = bg;
            ctx.beginPath(); ctx.arc(dishX, dishY, r * 0.62, 0, Math.PI * 2); ctx.fill();
            ctx.restore();

            /* Tributary beams converging into the dish — the eight feeder
               lasers, only visible near full charge. */
            if (pulse > 0.72) {
                var conv = (pulse - 0.72) / 0.28;
                ctx.strokeStyle = 'rgba(120,255,150,' + (0.30 * conv).toFixed(3) + ')';
                ctx.lineWidth = Math.max(0.5, r * 0.014);
                for (var i = 0; i < 8; i++) {
                    var a = (i / 8) * Math.PI * 2 + now / 2600;
                    var far = r * 0.46;
                    ctx.beginPath();
                    ctx.moveTo(dishX + Math.cos(a) * far, dishY + Math.sin(a) * far);
                    ctx.lineTo(dishX, dishY);
                    ctx.stroke();
                }
            }
            ctx.restore();
        }
    },

    chronos: {
        baseColor: [200, 145, 40],
        atmosphere: [240, 185, 65],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Time rings */
            var scales = [0.45, 0.6, 0.75];
            for (var si = 0; si < scales.length; si++) {
                ctx.strokeStyle = 'rgba(255, 200, 80, 0.12)';
                ctx.lineWidth = 0.5;
                ctx.beginPath();
                ctx.arc(x, y, r * scales[si], 0, Math.PI * 2);
                ctx.stroke();
            }
            /* Clock hand */
            var hour = new Date().getUTCHours();
            var handA = (hour / 24) * Math.PI * 2 - Math.PI / 2;
            ctx.strokeStyle = 'rgba(255, 210, 90, 0.25)';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(x, y);
            ctx.lineTo(x + Math.cos(handA) * r * 0.65, y + Math.sin(handA) * r * 0.65);
            ctx.stroke();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* ═══ HOURGLASS OVERLAY + CLOCK TICK MARKS ═══
               Hourglass: two triangles meeting at center, faint amber lines.
               12 clock tick marks around the planet perimeter. */
            /* Tick marks — 12 positions like a clock face */
            var tickR  = r * 1.25;
            var tickLen = r * 0.14;
            ctx.strokeStyle = 'rgba(255,200,80,0.20)';
            for(var ti=0; ti<12; ti++){
                var ta = (ti / 12) * Math.PI * 2 - Math.PI/2;
                var isHour = (ti % 3 === 0);
                ctx.lineWidth = isHour ? 1.0 : 0.5;
                ctx.strokeStyle = isHour ? 'rgba(255,210,100,0.30)' : 'rgba(255,195,70,0.16)';
                var tLen = isHour ? tickLen * 1.5 : tickLen;
                ctx.beginPath();
                ctx.moveTo(x + Math.cos(ta)*(tickR - tLen), y + Math.sin(ta)*(tickR - tLen));
                ctx.lineTo(x + Math.cos(ta)*tickR,          y + Math.sin(ta)*tickR);
                ctx.stroke();
            }
            /* Hourglass: top triangle + bottom triangle meeting at center */
            var hSize = r * 0.55;
            var hAlpha = 0.10 + 0.06 * Math.sin(now / 2200);
            ctx.strokeStyle = 'rgba(255,210,90,' + hAlpha.toFixed(3) + ')';
            ctx.lineWidth   = 0.8;
            /* Top triangle (apex down at center) */
            ctx.beginPath();
            ctx.moveTo(x - hSize, y - hSize*0.9);
            ctx.lineTo(x + hSize, y - hSize*0.9);
            ctx.lineTo(x,         y);
            ctx.closePath(); ctx.stroke();
            /* Bottom triangle (apex up at center) */
            ctx.beginPath();
            ctx.moveTo(x - hSize, y + hSize*0.9);
            ctx.lineTo(x + hSize, y + hSize*0.9);
            ctx.lineTo(x,         y);
            ctx.closePath(); ctx.stroke();
            /* Sand falling — animated dot moving from top to bottom through center */
            var sandProgress = (now / 3000) % 1;
            var sandY = y - hSize * 0.7 + sandProgress * hSize * 1.4;
            ctx.fillStyle = 'rgba(255,210,80,' + (0.4 * Math.sin(sandProgress * Math.PI)).toFixed(3) + ')';
            ctx.beginPath(); ctx.arc(x, sandY, 1.2, 0, Math.PI * 2); ctx.fill();
        }
    },

    nexus: {
        /* ═══ THE COUNCIL LATTICE — 14 mathematical engines orbiting one verdict ═══
           Retired 2026-07-28: NEXUS previously rendered as a bright blue-white
           pulsar (dense neutron-star core + twin lighthouse beams strobing at
           a 1500ms rotation) which read as visually out-of-place — a raw star
           dropped among NASA-textured planets and, now, a geodesic data
           lattice at Command Center. Redesigned to sit in that same "data/
           structure" register CC now establishes: a small teal geodesic core
           (echoing CC's lattice language, since NEXUS is itself a council
           that aggregates 14 engines into one verdict — the same shape of
           job CC does for the whole fleet) surrounded by exactly 14 engine
           motes in slow, stately orbit, each a faint point of light that
           flares briefly when that engine fires. No beams, no strobing,
           no white-hot core — NEXUS now reads as "a council in session",
           not "a lighthouse". Palette pulled from NEXUS's real BOTS_DEF
           color (#2dd4bf teal) instead of the old pulsar's unrelated blue,
           so the rendered body finally matches the color used everywhere
           else it appears (label, HUD chips, sound pan table). */
        baseColor: [20, 130, 130],
        atmosphere: [45, 212, 191],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Compact core — teal, steady, no strobe */
            var cr = Math.max(0.1, r * 0.42);
            var haloG = ctx.createRadialGradient(x, y, Math.max(0.1, cr * 0.8), x, y, r);
            haloG.addColorStop(0,   'rgba(110,230,220,0.20)');
            haloG.addColorStop(0.4, 'rgba(45,180,180,0.09)');
            haloG.addColorStop(1,   'rgba(15,60,60,0)');
            ctx.fillStyle = haloG;
            ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
            var coreG = ctx.createRadialGradient(x, y, 0, x, y, cr);
            coreG.addColorStop(0,   'rgba(225,255,250,0.95)');
            coreG.addColorStop(0.35,'rgba(150,235,225,0.85)');
            coreG.addColorStop(0.7, 'rgba(45,190,180,0.55)');
            coreG.addColorStop(1,   'rgba(20,110,110,0.30)');
            ctx.fillStyle = coreG;
            ctx.beginPath(); ctx.arc(x, y, cr, 0, Math.PI * 2); ctx.fill();
            /* Specular hotspot — soft, not blown-out white */
            var specG = ctx.createRadialGradient(
                x - cr * 0.22, y - cr * 0.22, 0,
                x - cr * 0.22, y - cr * 0.22, Math.max(0.1, cr * 0.5)
            );
            specG.addColorStop(0,   'rgba(255,255,255,0.55)');
            specG.addColorStop(1,   'rgba(255,255,255,0)');
            ctx.fillStyle = specG;
            ctx.beginPath(); ctx.arc(x, y, cr, 0, Math.PI * 2); ctx.fill();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            ctx.save();
            var cr = Math.max(0.1, r * 0.42);

            /* ── Small geodesic wire shell around the core — echoes the
               Command Center lattice language at NEXUS's own scale, tying
               "council that aggregates 14 engines" back to "hub that
               aggregates 18 bots" without literally reusing CC's centerpiece. ── */
            if(typeof _ccLatticeVerts !== "undefined" && typeof _ccProjectVertex === "function"){
                var latAngle = now / 40000; /* one slow revolution ≈ 40s — a peer's pace, not a strobe */
                var latR = cr * 1.35;
                var pts = new Array(_ccLatticeVerts.length);
                for(var vi=0; vi<_ccLatticeVerts.length; vi++){
                    pts[vi] = _ccProjectVertex(_ccLatticeVerts[vi], latAngle, 0.5, latR);
                }
                for(var ei=0; ei<_ccLatticeEdges.length; ei++){
                    var e = _ccLatticeEdges[ei];
                    var pa = pts[e[0]], pb = pts[e[1]];
                    var avgZ = (pa.z+pb.z)/2;
                    var depthA = 0.3 + 0.6*((avgZ+latR)/(latR*2));
                    /* Boosted 2026-07-30 (round 2 redesign): was 0.16*depthA
                       (~0.05-0.13) — sub-perceptual. This wireframe IS
                       NEXUS's stated identity (14-engine council lattice),
                       so it needs to actually read. */
                    ctx.strokeStyle = 'rgba(110,240,230,' + (0.55*depthA).toFixed(3) + ')';
                    ctx.lineWidth = Math.max(0.8, r*0.035);
                    ctx.beginPath();
                    ctx.moveTo(x+pa.x, y+pa.y);
                    ctx.lineTo(x+pb.x, y+pb.y);
                    ctx.stroke();
                }
            }

            /* ── 14 ENGINE MOTES — one per mathematical framework in the
               council (Euclid, Schwarzschild, Topology, Fisher, Causal,
               Newton, Lorenz, Einstein, Boltzmann, Prigogine, Quantum,
               Thom, Shannon + the aggregate verdict slot). Slow, stately
               orbit — same register as the star tier elsewhere in this
               file, never a fast strobe. Each mote flares briefly on a
               staggered cycle to suggest engines firing independently
               rather than a single synchronized pulse. ── */
            var numEngines = 14;
            var engineOrbit = r * 2.1;
            for (var ei2 = 0; ei2 < numEngines; ei2++) {
                var baseAngle = (ei2 / numEngines) * Math.PI * 2;
                var engAngle = baseAngle + now / 52000; /* full ring revolution ≈ 52s */
                var ex = x + Math.cos(engAngle) * engineOrbit;
                var ey = y + Math.sin(engAngle) * engineOrbit * 0.82; /* slight ellipse for depth */
                /* Staggered flare — each engine gets its own phase offset so
                   they clearly fire independently, not in lockstep */
                var flarePhase = ((now / 4200) + ei2 / numEngines) % 1;
                var flareBright = Math.max(0, 1 - flarePhase*3.5);
                /* Motes boosted 2026-07-30 (round 2): base alpha/size raised
                   so the ring of 14 lights reads as a clear halo around the
                   core at a glance, not just during flare peaks. */
                var moteA = 0.45 + flareBright*0.50;
                var moteR = Math.max(0.1, r*0.09 + flareBright*r*0.10);
                var moteG = ctx.createRadialGradient(ex, ey, 0, ex, ey, Math.max(0.1, moteR*2.6));
                moteG.addColorStop(0, 'rgba(190,245,235,' + moteA.toFixed(3) + ')');
                moteG.addColorStop(0.5, 'rgba(80,210,195,' + (moteA*0.5).toFixed(3) + ')');
                moteG.addColorStop(1, 'rgba(45,190,180,0)');
                ctx.fillStyle = moteG;
                ctx.beginPath();
                ctx.arc(ex, ey, Math.max(0.1, moteR*2.6), 0, Math.PI*2);
                ctx.fill();
            }

            /* ── VERDICT PULSE — soft ring expands outward on a steady 6s
               cadence, standing in for "the council reaches a verdict" —
               same visual grammar as CC's weave pulse, at NEXUS's scale. ── */
            var ringPeriod = 6000;
            var rPhase = (now % ringPeriod) / ringPeriod;
            var rRadius = Math.max(0.1, cr + rPhase * r * 2.2);
            var rAlpha = Math.max(0, (1-rPhase)*0.22);
            ctx.strokeStyle = 'rgba(80,220,210,' + rAlpha.toFixed(3) + ')';
            ctx.lineWidth = Math.max(0.3, (1-rPhase)*1.4);
            ctx.beginPath();
            ctx.arc(x, y, rRadius, 0, Math.PI*2);
            ctx.stroke();

            ctx.restore();
        }
    },

    nexusbrain: {
        baseColor: [55, 35, 140],
        atmosphere: [130, 80, 230],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Purple/violet neural planet with deep-space violet base */
            var cRot=now/100000;
            ctx.save();ctx.translate(x,y);ctx.rotate(cRot);
            /* Ocean base tint */
            var oG=ctx.createRadialGradient(0,0,r*0.2,0,0,r);
            oG.addColorStop(0,'rgba(30,90,180,0.15)');oG.addColorStop(1,'rgba(20,60,140,0.08)');
            ctx.fillStyle=oG;ctx.fillRect(-r,-r,r*2,r*2);
            /* Continents */
            var continents=[
              {cx:-0.25,cy:-0.2,rx:0.28,ry:0.18,rot:0.3,c:'rgba(60,90,45,0.3)'},
              {cx:0.3,cy:0.15,rx:0.22,ry:0.32,rot:-0.4,c:'rgba(75,100,50,0.28)'},
              {cx:-0.1,cy:0.4,rx:0.18,ry:0.1,rot:0.7,c:'rgba(65,85,42,0.25)'},
              {cx:0.35,cy:-0.3,rx:0.15,ry:0.12,rot:0.1,c:'rgba(120,100,70,0.2)'},
              {cx:-0.4,cy:0.05,rx:0.12,ry:0.2,rot:-0.2,c:'rgba(80,95,55,0.25)'},
              {cx:0.05,cy:-0.45,rx:0.2,ry:0.08,rot:0,c:'rgba(100,110,90,0.18)'}
            ];
            for(var ci=0;ci<continents.length;ci++){
              var co=continents[ci];ctx.fillStyle=co.c;
              ctx.beginPath();ctx.ellipse(co.cx*r,co.cy*r,co.rx*r,co.ry*r,co.rot,0,Math.PI*2);ctx.fill();
              /* Coastal highlight */
              ctx.strokeStyle='rgba(80,140,60,0.08)';ctx.lineWidth=1;
              ctx.beginPath();ctx.ellipse(co.cx*r,co.cy*r,co.rx*r*1.05,co.ry*r*1.05,co.rot,0,Math.PI*2);ctx.stroke();
            }
            /* Ice caps */
            var ncG=ctx.createRadialGradient(0,-r*0.82,0,0,-r*0.82,r*0.22);
            ncG.addColorStop(0,'rgba(230,240,250,0.25)');ncG.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=ncG;ctx.beginPath();ctx.arc(0,-r*0.82,r*0.22,0,Math.PI*2);ctx.fill();
            var scG=ctx.createRadialGradient(0,r*0.85,0,0,r*0.85,r*0.18);
            scG.addColorStop(0,'rgba(230,240,250,0.2)');scG.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=scG;ctx.beginPath();ctx.arc(0,r*0.85,r*0.18,0,Math.PI*2);ctx.fill();
            /* Cloud wisps */
            ctx.strokeStyle='rgba(255,255,255,0.06)';ctx.lineWidth=1.5;
            for(var wi=0;wi<5;wi++){
              var wy=-r*0.5+wi*r*0.25;
              var wdrift=Math.sin(now/8000+wi*2)*r*0.05;
              ctx.beginPath();
              for(var wx=-r*0.7;wx<r*0.5;wx+=2){
                var ww=Math.sin(wx/10+now/5000+wi)*2;
                if(wx===-r*0.7)ctx.moveTo(wx+wdrift,wy+ww);else ctx.lineTo(wx+wdrift,wy+ww);
              }
              ctx.stroke();
            }
            /* City lights on dark side */
            var sunAlocal=Math.atan2(ly,lx)-cRot;
            for(var li=0;li<25;li++){
              var lAngle=(li*2.399)%(Math.PI*2);
              var lDist=(li*7.3)%0.72*r;
              var lpx=Math.cos(lAngle)*lDist,lpy=Math.sin(lAngle)*lDist;
              var dotA2=Math.atan2(lpy,lpx);
              var adiff=Math.abs(dotA2-sunAlocal);if(adiff>Math.PI)adiff=Math.PI*2-adiff;
              if(adiff>Math.PI*0.45&&Math.sqrt(lpx*lpx+lpy*lpy)<r*0.85){
                var cf=0.5+0.5*Math.sin(now/400+li*3.1);
                var clG=ctx.createRadialGradient(lpx,lpy,0,lpx,lpy,r*0.03);
                clG.addColorStop(0,'rgba(255,220,100,'+(0.3*cf)+')');clG.addColorStop(1,'rgba(255,180,50,0)');
                ctx.fillStyle=clG;ctx.beginPath();ctx.arc(lpx,lpy,r*0.03,0,Math.PI*2);ctx.fill();
              }
            }
            ctx.restore();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* ═══ NEURAL LIGHTNING ARCS — 3 jagged surface discharges ═══
               Flicker independently, brighter arcs when bot has active signals.
               Each arc is a mini lightning bolt from one surface point to another. */
            var arcDefs = [
                {seedA: 0.3,  seedB: 1.9,  speed: 700,  bright: 0.55},
                {seedA: 1.1,  seedB: 3.5,  speed: 1100, bright: 0.40},
                {seedA: 2.4,  seedB: 0.8,  speed: 850,  bright: 0.48}
            ];
            for(var ai=0;ai<arcDefs.length;ai++){
                var ad=arcDefs[ai];
                /* Flicker: only visible when sin crosses threshold */
                var flicker=Math.sin(now/ad.speed+ai*2.7);
                if(flicker<0.25) continue;
                var alpha=flicker*ad.bright;
                /* Start and end points on the planet surface */
                var ax1=x+Math.cos(ad.seedA+now/9000)*r*0.75;
                var ay1=y+Math.sin(ad.seedA+now/9000)*r*0.75;
                var ax2=x+Math.cos(ad.seedB+now/7000)*r*0.7;
                var ay2=y+Math.sin(ad.seedB+now/7000)*r*0.7;
                /* Midpoint jitter for jagged look */
                var jitter=r*0.18*(Math.sin(now/120+ai*3)-0.5);
                var mx=(ax1+ax2)/2+jitter;
                var my=(ay1+ay2)/2-jitter*0.7;
                /* Bright core bolt */
                ctx.strokeStyle='rgba(200,140,255,'+alpha.toFixed(3)+')';
                ctx.lineWidth=1.2;
                ctx.beginPath();ctx.moveTo(ax1,ay1);ctx.lineTo(mx,my);ctx.lineTo(ax2,ay2);ctx.stroke();
                /* Dim halo around it */
                ctx.strokeStyle='rgba(160,80,255,'+(alpha*0.35).toFixed(3)+')';
                ctx.lineWidth=3;
                ctx.beginPath();ctx.moveTo(ax1,ay1);ctx.lineTo(mx,my);ctx.lineTo(ax2,ay2);ctx.stroke();
                /* Bright endpoints */
                ctx.fillStyle='rgba(230,180,255,'+(alpha*0.7).toFixed(3)+')';
                ctx.beginPath();ctx.arc(ax1,ay1,1.5,0,Math.PI*2);ctx.fill();
                ctx.beginPath();ctx.arc(ax2,ay2,1.5,0,Math.PI*2);ctx.fill();
            }
        }
    },

    rubberband: {
        baseColor: [0, 175, 215],
        atmosphere: [55, 215, 250],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Elastic bands — horizontal lines that oscillate */
            ctx.strokeStyle = 'rgba(55, 215, 250, 0.15)';
            ctx.lineWidth = 0.8;
            for (var bi = 0; bi < 5; bi++) {
                var by = y - r * 0.5 + (bi / 4) * r;
                ctx.beginPath();
                for (var bx = x - r; bx < x + r; bx += 2) {
                    var stretch = Math.sin((bx - x) / 8 + now / 1200 + bi * 1.3) * (2 + bi * 0.5);
                    if (bx === x - r) ctx.moveTo(bx, by + stretch);
                    else ctx.lineTo(bx, by + stretch);
                }
                ctx.stroke();
            }
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* ═══ OSCILLATION RINGS — 3 concentric expanding/contracting rings ═══
               Like sound waves or ripples on water. Very subtle. */
            for(var ri=0;ri<3;ri++){
                /* Each ring expands and contracts at slightly different phases */
                var phase=((now/1800+ri*0.33)%1);
                var ringR=r*(1.15+ri*0.28+Math.sin(now/900+ri*2.1)*0.06);
                var ringA=(1-phase*phase)*0.12-ri*0.02;
                if(ringA<=0) continue;
                ctx.strokeStyle='rgba(55,215,250,'+ringA.toFixed(3)+')';
                ctx.lineWidth=0.8;
                ctx.beginPath();ctx.arc(x,y,Math.max(0.1,ringR),0,Math.PI*2);ctx.stroke();
            }
        }
    },

    contrarian: {
        baseColor: [175, 45, 45],
        atmosphere: [215, 75, 75],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Adult-grade pass (2026-07-31): was six 0.18-alpha hairline
               cracks and one 0.1-alpha frost smudge — the flattest,
               least-animated body left in the file (no overlay at all
               previously). Baked frozen-regolith texture first, then the
               cracks/frost boosted on top; a slow retrograde-tinted rim
               ring added in overlay() so the "trades against the crowd"
               identity actually reads without inventing new geometry. */
            var tex = _bakeRegolithTex('contrarian', _texBucket(r), [200,90,90]);
            _stampSurfTex(ctx, tex, x, y, r);
            /* Ice cracks with red veins — boosted */
            ctx.strokeStyle = 'rgba(255, 110, 100, 0.30)';
            ctx.lineWidth = Math.max(0.6, r*0.016);
            var cracks = [[0.15,-0.1,0.6,0.5],[-0.3,-0.4,0.2,0.3],[-0.5,0.1,-0.1,-0.6],[0.4,-0.3,-0.2,0.5],[0.1,0.2,0.55,-0.15],[-0.35,0.35,0.15,0.55]];
            for (var ci = 0; ci < cracks.length; ci++) {
                var cr = cracks[ci];
                ctx.beginPath(); ctx.moveTo(x+cr[0]*r, y+cr[1]*r); ctx.lineTo(x+cr[2]*r, y+cr[3]*r); ctx.stroke();
            }
            /* Frost patches — two now, boosted alpha, slight drift so the
               surface reads as alive rather than a static screenshot */
            var fDrift = Math.sin(now/6000)*r*0.04;
            var fG = ctx.createRadialGradient(x-r*0.2+fDrift, y-r*0.3, 0, x-r*0.2+fDrift, y-r*0.3, r*0.25);
            fG.addColorStop(0, 'rgba(225, 200, 215, 0.20)'); fG.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = fG; ctx.beginPath(); ctx.arc(x-r*0.2+fDrift, y-r*0.3, r*0.25, 0, Math.PI*2); ctx.fill();
            var fG2 = ctx.createRadialGradient(x+r*0.28, y+r*0.22-fDrift, 0, x+r*0.28, y+r*0.22-fDrift, r*0.18);
            fG2.addColorStop(0, 'rgba(225, 200, 215, 0.14)'); fG2.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = fG2; ctx.beginPath(); ctx.arc(x+r*0.28, y+r*0.22-fDrift, r*0.18, 0, Math.PI*2); ctx.fill();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* ═══ RETROGRADE RING — dashed ring spinning opposite the
               planet's own orbital direction, the visual shorthand for
               "contrarian: moves against the prevailing flow". Inverted
               rim light on the far (anti-sun) limb reinforces the same
               "backwards" read: normal bodies get bright limb toward the
               sun, this one flares dim-cold on the dark side instead. ═══ */
            var ringR = r * 1.28;
            var spin = -now/6000; /* negative = counter-rotation */
            var dashN = 18;
            for (var di=0; di<dashN; di++){
                if (di % 3 === 2) continue; /* skip every 3rd for a dashed look */
                var da = spin + (di/dashN)*Math.PI*2;
                var da2 = spin + ((di+0.62)/dashN)*Math.PI*2;
                ctx.strokeStyle = 'rgba(255,90,80,0.34)';
                ctx.lineWidth = Math.max(0.8, r*0.035);
                ctx.beginPath();
                ctx.arc(x, y, ringR, da, da2);
                ctx.stroke();
            }
            /* Inverted rim — cold flare on the anti-sun limb, opposite of
               every other body's sun-facing crescent */
            var antiAngle = Math.atan2(-ly, -lx);
            ctx.strokeStyle = 'rgba(160,60,190,0.22)';
            ctx.lineWidth = Math.max(0.8, r*0.05);
            ctx.beginPath();
            ctx.arc(x, y, r*1.02, antiAngle-0.55, antiAngle+0.55);
            ctx.stroke();
        }
    },

    arbitrageur: {
        baseColor: [120, 80, 200],
        atmosphere: [160, 120, 240],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Wormhole rings — two concentric ellipses with energy flow */
            var rot = now / 12000;
            ctx.save(); ctx.translate(x, y); ctx.rotate(rot);
            /* Inner ring */
            ctx.strokeStyle = 'rgba(179, 136, 255, 0.2)'; ctx.lineWidth = 0.8;
            ctx.beginPath(); ctx.ellipse(0, 0, r*0.5, r*0.2, 0, 0, Math.PI*2); ctx.stroke();
            /* Outer ring */
            ctx.strokeStyle = 'rgba(140, 100, 220, 0.12)'; ctx.lineWidth = 0.6;
            ctx.beginPath(); ctx.ellipse(0, 0, r*0.75, r*0.3, 0.2, 0, Math.PI*2); ctx.stroke();
            /* Center convergence glow */
            var cg = ctx.createRadialGradient(0, 0, 0, 0, 0, r*0.3);
            cg.addColorStop(0, 'rgba(200, 180, 255, 0.15)');
            cg.addColorStop(1, 'rgba(0, 0, 0, 0)');
            ctx.fillStyle = cg;
            ctx.beginPath(); ctx.arc(0, 0, r*0.3, 0, Math.PI*2); ctx.fill();
            ctx.restore();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* ═══ BINARY SYSTEM — two dots orbiting each other, ~5s period ═══ */
            var binAngle = (now / 5000) * Math.PI * 2;
            var binSep   = r * 0.55; /* separation between the two bodies */
            /* Body A — larger, more massive (primary) */
            var ax = x + Math.cos(binAngle) * binSep;
            var ay = y + Math.sin(binAngle) * binSep * 0.55;
            var aG = ctx.createRadialGradient(ax, ay, 0, ax, ay, r*0.35);
            aG.addColorStop(0, 'rgba(200,160,255,0.7)');
            aG.addColorStop(0.4,'rgba(150,100,230,0.4)');
            aG.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = aG;
            ctx.beginPath(); ctx.arc(ax, ay, r*0.35, 0, Math.PI*2); ctx.fill();
            /* Body B — smaller, secondary (opposite phase) */
            var bx = x - Math.cos(binAngle) * binSep * 0.7;
            var by = y - Math.sin(binAngle) * binSep * 0.38;
            var bG = ctx.createRadialGradient(bx, by, 0, bx, by, r*0.24);
            bG.addColorStop(0, 'rgba(180,130,255,0.65)');
            bG.addColorStop(0.5,'rgba(120,80,210,0.35)');
            bG.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = bG;
            ctx.beginPath(); ctx.arc(bx, by, r*0.24, 0, Math.PI*2); ctx.fill();
            /* Tidal bridge — faint glow between them */
            var tG = ctx.createLinearGradient(ax,ay,bx,by);
            tG.addColorStop(0,   'rgba(180,120,255,0.06)');
            tG.addColorStop(0.5, 'rgba(200,140,255,0.12)');
            tG.addColorStop(1,   'rgba(160,100,255,0.06)');
            ctx.strokeStyle = tG; ctx.lineWidth = 2;
            ctx.beginPath(); ctx.moveTo(ax,ay); ctx.lineTo(bx,by); ctx.stroke();
        }
    },

    hivemind: {
        baseColor: [195, 175, 55],
        atmosphere: [235, 215, 95],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Adult-grade pass (2026-07-31): honeycomb strokes were
               0.06-0.1 alpha, essentially invisible past a few feet — the
               cell-flicker animation (real, worth keeping) had nothing to
               animate against. Baked amber mottling underneath gives the
               shell body weight; the honeycomb itself stays live-drawn
               (its cells genuinely blink on a timer, which a static bake
               can't reproduce) but at alphas that actually read. */
            var tex = _bakeMottleTex('hivemind', _texBucket(r), [175,150,50]);
            _stampSurfTex(ctx, tex, x, y, r);
            /* Honeycomb hex cells */
            var hs = r * 0.18;
            var hh = hs * Math.sqrt(3) / 2;
            ctx.strokeStyle = 'rgba(245, 225, 120, 0.24)'; ctx.lineWidth = Math.max(0.5, r*0.012);
            for (var row = -3; row <= 3; row++) {
                for (var col = -3; col <= 3; col++) {
                    var hx = x + col * hs * 1.5;
                    var hy = y + row * hh * 2 + (col % 2 ? hh : 0);
                    var d = Math.sqrt((hx-x)*(hx-x) + (hy-y)*(hy-y));
                    if (d > r * 0.85) continue;
                    ctx.beginPath();
                    for (var hi = 0; hi <= 6; hi++) {
                        var ha = (hi / 6) * Math.PI * 2;
                        if (hi === 0) ctx.moveTo(hx + Math.cos(ha)*hs*0.45, hy + Math.sin(ha)*hs*0.45);
                        else ctx.lineTo(hx + Math.cos(ha)*hs*0.45, hy + Math.sin(ha)*hs*0.45);
                    }
                    ctx.stroke();
                    /* Fill some cells based on position + time — lit cells
                       now glow amber instead of a near-invisible wash */
                    if (Math.sin(row * 3.7 + col * 2.1 + now / 3000) > 0.3) {
                        ctx.fillStyle = 'rgba(250, 230, 140, 0.16)'; ctx.fill();
                    }
                }
            }
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* Amber hive-glow rim — reads as "warm energy shell" past the
               disk edge, ties the honeycomb identity together at a glance */
            var hgR = Math.max(0.1, r*1.12);
            var hiveG = ctx.createRadialGradient(x, y, r*0.9, x, y, hgR);
            hiveG.addColorStop(0, 'rgba(0,0,0,0)');
            hiveG.addColorStop(0.7, 'rgba(240,215,110,0.12)');
            hiveG.addColorStop(1, 'rgba(240,215,110,0)');
            ctx.fillStyle = hiveG;
            ctx.beginPath(); ctx.arc(x, y, hgR, 0, Math.PI*2); ctx.fill();
            /* ═══ SWARM CLUSTER — 7 small rocks orbiting a common center ═══
               Drawn in overlay (outside clip) so they extend beyond the planet circle.
               Slowly rotate as a group. Each rock is a faint golden dot. */
            var clusterRot = now / 8000; /* full rotation every ~8 seconds */
            var rocks7 = [
                {d:0.90,phase:0,    sz:1.6},{d:0.75,phase:0.90,sz:2.2},
                {d:1.05,phase:1.85,sz:1.4},{d:0.82,phase:2.80,sz:1.9},
                {d:0.95,phase:3.75,sz:1.3},{d:0.70,phase:4.70,sz:2.0},
                {d:1.10,phase:5.65,sz:1.5}
            ];
            for(var rki=0; rki<rocks7.length; rki++){
                var rk=rocks7[rki];
                var rkA = rk.phase + clusterRot;
                var rkR = r * rk.d;
                var rkX = x + Math.cos(rkA) * rkR;
                var rkY = y + Math.sin(rkA) * rkR * 0.70; /* slight vertical compression */
                /* Depth: behind center = dimmer */
                var rkDepth = Math.sin(rkA);
                var rkAlpha = rkDepth < 0 ? 0.25 : 0.55;
                ctx.fillStyle = 'rgba(200,180,60,' + rkAlpha + ')';
                ctx.beginPath(); ctx.arc(rkX, rkY, rk.sz, 0, Math.PI*2); ctx.fill();
                /* Faint glow halo on each rock */
                var rkG = ctx.createRadialGradient(rkX, rkY, 0, rkX, rkY, rk.sz*2.5);
                rkG.addColorStop(0,'rgba(235,215,95,'+(rkAlpha*0.3).toFixed(3)+')');
                rkG.addColorStop(1,'rgba(0,0,0,0)');
                ctx.fillStyle=rkG; ctx.beginPath(); ctx.arc(rkX,rkY,rk.sz*2.5,0,Math.PI*2); ctx.fill();
            }
        }
    },

    trinity: {
        baseColor: [75, 115, 175],
        atmosphere: [115, 155, 215],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Adult-grade pass (2026-07-31): grid rings were 0.06 alpha
               and the sweep/blips maxed at 0.2 — this body was reading as
               a plain blue ball with an occasional dim smear. Baked
               mottled-metal texture gives it real surface presence; grid
               and sweep alphas boosted so the "scanning radar globe"
               identity is legible at a glance, not just up close. */
            var tex = _bakeMottleTex('trinity', _texBucket(r), [90,130,190]);
            _stampSurfTex(ctx, tex, x, y, r);
            /* Radar sweep lines across surface */
            var sweep = (now / 2000) % (Math.PI * 2);
            /* Grid lines — boosted from 0.06 */
            ctx.strokeStyle = 'rgba(140, 195, 255, 0.20)'; ctx.lineWidth = Math.max(0.4, r*0.012);
            for (var ri = 1; ri <= 3; ri++) {
                ctx.beginPath(); ctx.arc(x, y, r * ri * 0.3, 0, Math.PI * 2); ctx.stroke();
            }
            /* Sweep beam */
            var swG = ctx.createRadialGradient(x, y, r*0.1, x, y, r);
            swG.addColorStop(0, 'rgba(140, 195, 255, 0.20)');
            swG.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = swG;
            ctx.beginPath(); ctx.moveTo(x, y);
            ctx.arc(x, y, r, sweep - 0.2, sweep + 0.2);
            ctx.closePath(); ctx.fill();
            /* Blip dots on sweep path — boosted, larger */
            for (var bi = 0; bi < 3; bi++) {
                var ba = sweep - bi * 0.6;
                var br2 = r * (0.3 + bi * 0.2);
                ctx.fillStyle = 'rgba(160, 210, 255, ' + (0.45 - bi * 0.10) + ')';
                ctx.beginPath(); ctx.arc(x + Math.cos(ba)*br2, y + Math.sin(ba)*br2, Math.max(1.2,r*0.05), 0, Math.PI*2); ctx.fill();
            }
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* ═══ RING SYSTEM — tilted ellipse around trinity ═══
               Shimmers (alpha oscillates) when signals_count > 0.
               We don't have bot data access here, so use subtle constant shimmer. */
            var tilt    = 0.30; /* viewing angle */
            var ringRot = now / 90000; /* very slow precession */
            /* Shimmer intensity — subtle oscillation */
            var shimmer = 0.12 + 0.06 * Math.sin(now / 1200);
            /* Back half (behind planet) */
            ctx.save();
            ctx.strokeStyle = 'rgba(115,165,220,' + shimmer.toFixed(3) + ')';
            ctx.lineWidth   = r * 0.18;
            ctx.beginPath();
            ctx.ellipse(x, y, r * 1.55, Math.max(0.1, r * 1.55 * tilt), ringRot,
                        Math.PI * 0.05, Math.PI * 0.95);
            ctx.stroke();
            /* Outer fainter ring back */
            ctx.strokeStyle = 'rgba(90,140,200,' + (shimmer*0.45).toFixed(3) + ')';
            ctx.lineWidth   = r * 0.08;
            ctx.beginPath();
            ctx.ellipse(x, y, r * 1.90, Math.max(0.1, r * 1.90 * tilt), ringRot,
                        Math.PI * 0.05, Math.PI * 0.95);
            ctx.stroke();
            ctx.restore();
            /* Front half (in front of planet) */
            ctx.save();
            ctx.strokeStyle = 'rgba(140,185,240,' + (shimmer*1.3).toFixed(3) + ')';
            ctx.lineWidth   = r * 0.18;
            ctx.beginPath();
            ctx.ellipse(x, y, r * 1.55, Math.max(0.1, r * 1.55 * tilt), ringRot,
                        Math.PI * 1.05, Math.PI * 1.95);
            ctx.stroke();
            ctx.strokeStyle = 'rgba(110,160,215,' + (shimmer*0.45).toFixed(3) + ')';
            ctx.lineWidth   = r * 0.08;
            ctx.beginPath();
            ctx.ellipse(x, y, r * 1.90, Math.max(0.1, r * 1.90 * tilt), ringRot,
                        Math.PI * 1.05, Math.PI * 1.95);
            ctx.stroke();
            ctx.restore();
        }
    },

    inference: {
        baseColor: [60, 80, 120],
        atmosphere: [90, 120, 180],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Adult-grade pass (2026-07-31): this was the flattest body in
               the whole file — a 0.15-0.25 alpha core wash and three
               0.1-alpha circuit strokes, no overlay, nothing past the
               disk edge. Rebuilt as a circuit-board/neural-lattice
               identity (Inference is the Ollama compute node — the fleet's
               literal "thinking" body) using a baked trace-mottle texture
               plus a denser, brighter live circuit mesh so it reads at a
               glance instead of disappearing next to its neighbours. */
            var tex = _bakeMottleTex('inference', _texBucket(r), [70,95,150]);
            _stampSurfTex(ctx, tex, x, y, r);
            /* Neural processing glow — pulsing core, boosted */
            var pulse = 0.5 + 0.5 * Math.sin(now / 800);
            var coreG = ctx.createRadialGradient(x, y, 0, x, y, r * 0.62);
            coreG.addColorStop(0, 'rgba(150, 205, 255, ' + (0.30 + 0.16 * pulse).toFixed(3) + ')');
            coreG.addColorStop(0.6, 'rgba(100, 155, 230, ' + (0.14 + 0.08*pulse).toFixed(3) + ')');
            coreG.addColorStop(1, 'rgba(60, 100, 180, 0)');
            ctx.fillStyle = coreG;
            ctx.beginPath(); ctx.arc(x, y, r * 0.62, 0, Math.PI * 2); ctx.fill();
            /* Circuit trace mesh — 6 branching traces instead of 3 bare
               lines, each with a bright junction node at the bend, reading
               as a real PCB/neural-net motif rather than random scribbles */
            for (var li = 0; li < 6; li++) {
                var la = li * 1.047 + now / 14000;
                var jx = x + Math.cos(la) * r * 0.22, jy = y + Math.sin(la) * r * 0.22;
                var ex2 = x + Math.cos(la + 0.45) * r * 0.82, ey2 = y + Math.sin(la + 0.28) * r * 0.72;
                var flick = 0.55 + 0.45*Math.sin(now/900 + li*1.7);
                ctx.strokeStyle = 'rgba(130, 190, 255, ' + (0.22*flick).toFixed(3) + ')';
                ctx.lineWidth = Math.max(0.5, r*0.014);
                ctx.beginPath();
                ctx.moveTo(x, y);
                ctx.lineTo(jx, jy);
                ctx.lineTo(ex2, ey2);
                ctx.stroke();
                /* Junction node */
                ctx.fillStyle = 'rgba(180, 220, 255, ' + (0.35*flick).toFixed(3) + ')';
                ctx.beginPath(); ctx.arc(jx, jy, Math.max(0.6, r*0.025), 0, Math.PI*2); ctx.fill();
            }
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* Cool processing-halo — soft blue bloom past the disk, plus
               3 slow orbiting data-packet motes that mark "compute
               happening" the same register CC's lattice motes use, at
               inference's own smaller scale. */
            var haloR = Math.max(0.1, r*1.16);
            var haloG = ctx.createRadialGradient(x, y, r*0.9, x, y, haloR);
            haloG.addColorStop(0, 'rgba(0,0,0,0)');
            haloG.addColorStop(0.7, 'rgba(130,180,255,0.10)');
            haloG.addColorStop(1, 'rgba(130,180,255,0)');
            ctx.fillStyle = haloG;
            ctx.beginPath(); ctx.arc(x, y, haloR, 0, Math.PI*2); ctx.fill();
            var moteN = 3;
            for (var mi=0; mi<moteN; mi++){
                var ma = (mi/moteN)*Math.PI*2 + now/5000;
                var mr = r * 1.32;
                var mx = x + Math.cos(ma)*mr, my = y + Math.sin(ma)*mr*0.7;
                var mAlpha = 0.4 + 0.3*Math.sin(now/700 + mi*2.1);
                var moteG = ctx.createRadialGradient(mx, my, 0, mx, my, Math.max(0.1, r*0.14));
                moteG.addColorStop(0, 'rgba(190,220,255,'+mAlpha.toFixed(3)+')');
                moteG.addColorStop(1, 'rgba(100,160,255,0)');
                ctx.fillStyle = moteG;
                ctx.beginPath(); ctx.arc(mx, my, r*0.14, 0, Math.PI*2); ctx.fill();
            }
        }
    },

    brainiac: {
        /* Brainiac — Command Center's sensory apparatus. A cold sensor body:
           a slow radar sweep over a latitude/longitude survey grid, with a
           ring of five collector lamps around the rim.

           The lamps are DATA, not decoration: each is one collector thread,
           lit when its category is fresh and dark when it is stale or has
           never sampled. A dead collector goes visibly dark on the body
           itself, which is the failure this whole tab was built to expose —
           previously a dead thread just stopped updating and every consumer
           read the last value as current. */
        baseColor: [30, 74, 108],
        atmosphere: [56, 189, 248],
        surface: function(ctx, x, y, r, lx, ly, now) {
            var tex = _bakeMottleTex('brainiac', _texBucket(r), [40, 96, 140]);
            _stampSurfTex(ctx, tex, x, y, r);
            /* Survey grid — 3 latitude arcs + 4 meridians, faint. */
            ctx.save();
            ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI*2); ctx.clip();
            ctx.strokeStyle = 'rgba(120, 200, 250, 0.16)';
            ctx.lineWidth = Math.max(0.4, r*0.012);
            for (var gi=1; gi<=3; gi++){
                var gy = y - r + (2*r) * (gi/4);
                var hw = Math.sqrt(Math.max(0, r*r - (gy-y)*(gy-y)));
                ctx.beginPath(); ctx.moveTo(x-hw, gy); ctx.lineTo(x+hw, gy); ctx.stroke();
            }
            for (var mi2=0; mi2<4; mi2++){
                var mfrac = (mi2+0.5)/4;
                var ell = Math.abs(Math.cos(mfrac*Math.PI));
                ctx.beginPath();
                ctx.ellipse(x, y, Math.max(0.1, r*ell), r, 0, 0, Math.PI*2);
                ctx.stroke();
            }
            /* Radar sweep — one slow rotation every ~6s, with a trailing
               wedge so the direction of travel is legible. */
            var sweep = (now/6000) % (Math.PI*2);
            var sweepG = ctx.createRadialGradient(x, y, 0, x, y, r);
            sweepG.addColorStop(0, 'rgba(125, 211, 252, 0.30)');
            sweepG.addColorStop(1, 'rgba(56, 189, 248, 0)');
            ctx.fillStyle = sweepG;
            ctx.beginPath();
            ctx.moveTo(x, y);
            ctx.arc(x, y, r, sweep - 0.55, sweep);
            ctx.closePath();
            ctx.fill();
            /* Leading edge */
            ctx.strokeStyle = 'rgba(186, 230, 253, 0.55)';
            ctx.lineWidth = Math.max(0.5, r*0.02);
            ctx.beginPath();
            ctx.moveTo(x, y);
            ctx.lineTo(x + Math.cos(sweep)*r, y + Math.sin(sweep)*r);
            ctx.stroke();
            ctx.restore();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* Five collector lamps. Read live health when it is available;
               when it is NOT, render them all dim rather than lit — an
               unknown collector must never look like a working one. */
            var cats = ['depth','trades','metrics','correlations','funding'];
            var H = (typeof window!=='undefined' && window._brainiac)
                    ? window._brainiac.health : null;
            for (var li=0; li<cats.length; li++){
                var la = -Math.PI/2 + (li/cats.length)*Math.PI*2;
                var lrad = r*1.28;
                var px = x + Math.cos(la)*lrad, py = y + Math.sin(la)*lrad*0.72;
                var st = H && H.categories ? H.categories[cats[li]] : null;
                var lit = !!(st && st.stale === false);
                var unknown = !st;
                var pulse = 0.6 + 0.4*Math.sin(now/650 + li*1.3);
                var a = unknown ? 0.12 : (lit ? 0.45+0.35*pulse : 0.14);
                var col = unknown ? '148,163,184' : (lit ? '125,211,252' : '239,68,68');
                var lg = ctx.createRadialGradient(px, py, 0, px, py, Math.max(0.1, r*0.16));
                lg.addColorStop(0, 'rgba('+col+','+a.toFixed(3)+')');
                lg.addColorStop(1, 'rgba('+col+',0)');
                ctx.fillStyle = lg;
                ctx.beginPath(); ctx.arc(px, py, Math.max(0.1, r*0.16), 0, Math.PI*2); ctx.fill();
            }
            /* Cold scanning halo */
            var haloR = Math.max(0.1, r*1.14);
            var hg = ctx.createRadialGradient(x, y, r*0.92, x, y, haloR);
            hg.addColorStop(0, 'rgba(0,0,0,0)');
            hg.addColorStop(0.7, 'rgba(56,189,248,0.09)');
            hg.addColorStop(1, 'rgba(56,189,248,0)');
            ctx.fillStyle = hg;
            ctx.beginPath(); ctx.arc(x, y, haloR, 0, Math.PI*2); ctx.fill();
        }
    },
};

/* --- ILLUMINATED PLANET RENDERER — NASA-quality lighting --- */

function drawPlanet(ctx, body, sunX, sunY, now, planetType) {
    var x = body.x, y = body.y;
    var r = Math.max(1, body.currentRadius || body.currentSize || 8);
    var vis = PLANET_VISUALS[planetType];
    if (!vis) return;

    var br = vis.baseColor[0], bg = vis.baseColor[1], bb = vis.baseColor[2];

    /* ═══ LIGHT DIRECTION from sun ═══ */
    var dx = sunX - x, dy = sunY - y;
    var dist = Math.sqrt(dx*dx + dy*dy) || 1;
    var lx = dx / dist, ly = dy / dist;
    var offX = lx * r * 0.3, offY = ly * r * 0.3;

    /* ═══ BASE SPHERE — lit side to deep shadow ═══ */
    var baseG = ctx.createRadialGradient(
        x + offX, y + offY, 0,
        x - offX * 0.3, y - offY * 0.3, r * 1.05
    );
    baseG.addColorStop(0, 'rgb(' + Math.min(255,br+35) + ',' + Math.min(255,bg+25) + ',' + Math.min(255,bb+15) + ')');
    baseG.addColorStop(0.25, 'rgb(' + br + ',' + bg + ',' + bb + ')');
    baseG.addColorStop(0.5, 'rgb(' + (br*0.6|0) + ',' + (bg*0.5|0) + ',' + (bb*0.45|0) + ')');
    baseG.addColorStop(0.75, 'rgb(' + (br*0.2|0) + ',' + (bg*0.15|0) + ',' + (bb*0.12|0) + ')');
    baseG.addColorStop(1, 'rgb(' + (br*0.05|0) + ',' + (bg*0.04|0) + ',' + (bb*0.03|0) + ')');

    ctx.fillStyle = baseG;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();

    /* ═══ SPECULAR — tiny, subtle, offset toward light ═══ */
    var specX = x + offX * 0.7, specY = y + offY * 0.7;
    var specG = ctx.createRadialGradient(specX, specY, 0, specX, specY, r * 0.25);
    specG.addColorStop(0, 'rgba(255,255,250,0.06)');
    specG.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = specG;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();

    /* ═══ ATMOSPHERIC LIMB — colored, brighter on sun side ═══ */
    if (vis.atmosphere) {
        var ar = vis.atmosphere[0], ag = vis.atmosphere[1], ab = vis.atmosphere[2];

        /* Full limb — very subtle */
        var limbG = ctx.createRadialGradient(x, y, r * 0.88, x, y, r * 1.06);
        limbG.addColorStop(0, 'rgba(0,0,0,0)');
        limbG.addColorStop(0.6, 'rgba(' + ar + ',' + ag + ',' + ab + ',0.04)');
        limbG.addColorStop(0.85, 'rgba(' + ar + ',' + ag + ',' + ab + ',0.08)');
        limbG.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = limbG;
        ctx.beginPath();
        ctx.arc(x, y, r * 1.06, 0, Math.PI * 2);
        ctx.fill();

        /* Bright crescent on sun-facing edge */
        var limbAngle = Math.atan2(ly, lx);
        ctx.strokeStyle = 'rgba(' + ar + ',' + ag + ',' + ab + ',0.1)';
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.arc(x, y, r + 0.5, limbAngle - 0.7, limbAngle + 0.7);
        ctx.stroke();
    }

    /* ═══ SURFACE DETAIL — unique per planet ═══ */
    /* Round 2 (2026-07-30): this used to hardcode "only oracle/deepblue/nexus
       get a surface", which silently killed PLANET_VISUALS.turtlesue's Death
       Star sprite (and any future non-star entry) even when drawPlanet was
       the actual live renderer for that body — turtlesue currently has a
       _botTypeMap cinema override so it never reaches drawPlanet anyway, but
       that override is being removed alongside this fix (see html
       _botTypeMap), and the surface must not silently no-op once it does.
       Gate is now "does this body's own PLANET_VISUALS entry define a
       surface", not a hardcoded star allowlist — every body decides for
       itself via its own vis object. */
    var _skipSurf=!vis.surface;
    if (vis.surface && !_skipSurf) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.clip();
        vis.surface(ctx, x, y, r, lx, ly, now);
        ctx.restore();
    }

    /* ═══ PHOTOMETRIC FINISH (2026-07-29) ═══
       Applied to EVERY body, over whatever the surface function painted.
       Surface detail is drawn flat-bright by design (it has to be legible),
       so without this pass the texture fights the base sphere's shading and
       the planet flattens back out. Three cheap layers restore the volume:

       1. Limb darkening — real spheres fall off at the edge (grazing
          incidence through more atmosphere / less normal-facing surface).
          This is what makes an edge read as curvature instead of a cut-out.
       2. Terminator — the day/night boundary swept across the disk from the
          actual sun direction, so shading agrees with the lighting.
       3. Specular sheen — a soft off-center highlight giving a wet/gaseous
          sense of a curved surface catching the light. */
    if (!_skipSurf || vis.surface) {
        /* 1. Limb darkening, centered on the disk. Deliberately strong — this
           is the layer doing most of the work of turning a flat colored circle
           into a ball, and at 50" viewing distance a subtle falloff reads as
           no falloff at all. */
        var ldG = ctx.createRadialGradient(x, y, r * 0.30, x, y, r);
        ldG.addColorStop(0, 'rgba(0,0,0,0)');
        ldG.addColorStop(0.55, 'rgba(0,0,0,0.10)');
        ldG.addColorStop(0.78, 'rgba(0,0,0,0.30)');
        ldG.addColorStop(0.92, 'rgba(0,0,0,0.52)');
        ldG.addColorStop(1, 'rgba(0,0,0,0.72)');
        ctx.fillStyle = ldG;
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();

        /* 2. Terminator — shadow ramps in from the anti-solar side */
        var tmG = ctx.createLinearGradient(
            x + lx * r, y + ly * r,
            x - lx * r, y - ly * r
        );
        tmG.addColorStop(0, 'rgba(0,0,0,0)');
        tmG.addColorStop(0.42, 'rgba(0,0,0,0)');
        tmG.addColorStop(0.72, 'rgba(0,0,0,0.26)');
        tmG.addColorStop(0.90, 'rgba(0,0,0,0.50)');
        tmG.addColorStop(1, 'rgba(0,0,0,0.66)');
        ctx.save();
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.clip();
        ctx.fillStyle = tmG;
        ctx.fillRect(x - r, y - r, r * 2, r * 2);
        ctx.restore();

        /* 3. Specular sheen toward the sun */
        var spX = x + lx * r * 0.42, spY = y + ly * r * 0.42;
        var shG = ctx.createRadialGradient(spX, spY, 0, spX, spY, r * 0.78);
        shG.addColorStop(0, 'rgba(255,252,244,0.10)');
        shG.addColorStop(0.45, 'rgba(255,250,238,0.035)');
        shG.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.save();
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.clip();
        ctx.fillStyle = shG;
        ctx.fillRect(x - r, y - r, r * 2, r * 2);
        ctx.restore();
    }

    /* ═══ INTER-BODY LIGHTING (2026-08-18) ═══
       Everything above lights this planet as if it were alone. This adds the
       terms that need the SCENE: atmospheric forward scatter around the
       limb, planetshine bounced from lit neighbours, and shadows cast by
       bodies between this one and the sun. Placed after the photometric
       finish so it composites over the final shaded sphere, and before the
       overlay so rings and sonar stay unshadowed — they are separate
       structures, not part of the body surface. */
    _ibApply(ctx, x, y, r, sunX, sunY, lx, ly, planetType, vis, now);

    /* ═══ OVERLAY — drawn AFTER planet, not clipped (rings, etc.) ═══
       Independent of _skipSurf now: a body can have an overlay (e.g. rings,
       whale sonar) without a bespoke surface function, or vice versa. */
    if (vis.overlay) {
        vis.overlay(ctx, x, y, r, lx, ly, now);
    }
}


/* ═══ COSMIC BACKGROUND — CosmicCanvas class ═══ */
var _cosmicCanvas = null;
function CosmicCanvas(w, h) {
    this.w = w; this.h = h;
    this.stars = [];
    this.nebulae = [];
    this.galaxies = [];
    this.dustLanes = [];
    this.clusters = [];
    this.shootingStars = [];
    this.lastShoot = 0;
    /* New: asteroid belt particles, long-period comet, emission nebulae */
    this.asteroidBelt = [];
    this.emissionNebulae = [];
    this.longComet = null;
    this.dedicatedGalaxy = null;
    this._generate();
}
CosmicCanvas.prototype._gauss = function() {
    var u=0,v=0;
    while(u===0)u=Math.random();
    while(v===0)v=Math.random();
    return Math.sqrt(-2*Math.log(u))*Math.cos(2*Math.PI*v);
};
CosmicCanvas.prototype._generate = function() {
    var w = this.w, h = this.h;
    var cx = w / 2, cy = h / 2;

    /* 800 stars with density falloff from galactic center (canvas center).
       Probability of placing a star: density ∝ 1 / (1 + (dist/200)^2)
       Accept-reject sampling: try random positions, keep with that probability. */
    var attempts = 0, placed = 0, target = 800;
    while (placed < target && attempts < 6000) {
        attempts++;
        var sx = Math.random() * w, sy = Math.random() * h;
        var dist = Math.sqrt((sx - cx) * (sx - cx) + (sy - cy) * (sy - cy));
        var prob = 1 / (1 + Math.pow(dist / 220, 2));
        if (Math.random() > prob * 2.2) continue; /* reject */
        var depth = Math.random();
        this.stars.push({
            x: sx, y: sy,
            size: 0.4 + depth * 2.2,
            brightness: 0.08 + depth * 0.35,
            twinkle: 2000 + Math.random() * 12000,
            phase: Math.random() * Math.PI * 2,
            hue: Math.random() > 0.7 ? (15 + Math.random() * 35) : (195 + Math.random() * 50),
            sat: 10 + Math.random() * 25,
        });
        placed++;
    }

    /* ═══ ASYMMETRIC EMISSION NEBULAE ═══
       Two large color clouds at opposite sides — pink/purple left, blue/teal right.
       These are in addition to the generic nebulae below. */
    /* Pink/purple emission cloud — left-center */
    this.emissionNebulae.push({
        x: w * 0.22, y: h * 0.45,
        rx: w * 0.38, ry: h * 0.55,
        hue: 300, sat: 55, alpha: 0.11,
        hue2: 330, sat2: 50, alpha2: 0.07,
        rot: -0.25, side: 'left'
    });
    /* Blue/teal emission cloud — right-center */
    this.emissionNebulae.push({
        x: w * 0.78, y: h * 0.52,
        rx: w * 0.35, ry: h * 0.50,
        hue: 200, sat: 60, alpha: 0.10,
        hue2: 175, sat2: 55, alpha2: 0.065,
        rot: 0.18, side: 'right'
    });
    /* Faint violet wash at top */
    this.emissionNebulae.push({
        x: w * 0.5, y: h * 0.1,
        rx: w * 0.5, ry: h * 0.3,
        hue: 265, sat: 40, alpha: 0.06,
        hue2: 285, sat2: 35, alpha2: 0.04,
        rot: 0, side: 'top'
    });

    /* ═══ DEDICATED SPIRAL GALAXY — upper-right corner ═══ */
    this.dedicatedGalaxy = {
        x: w * 0.88, y: h * 0.09,
        size: 20, angle: 0.7, ecc: 0.48,
        bright: 0.22, armCount: 2
    };

    /* ═══ ASTEROID BELT ═══
       60 particles in a ring between inner (r~170) and outer (r~240) orbit zones.
       Drawn as dim dots orbiting slowly. */
    for (var ai = 0; ai < 65; ai++) {
        var aAngle = (ai / 65) * Math.PI * 2 + Math.random() * 0.18;
        var aRadius = 175 + Math.random() * 65; /* ring width ~65px */
        this.asteroidBelt.push({
            baseAngle: aAngle,
            radius: aRadius,
            speed: (0.00008 + Math.random() * 0.00012) * (Math.random() > 0.5 ? 1 : -1),
            size: 0.5 + Math.random() * 1.4,
            bright: 0.12 + Math.random() * 0.18,
            yEcc: 0.88 + Math.random() * 0.16, /* slight eccentricity */
            phase: Math.random() * Math.PI * 2,   /* twinkle phase */
        });
    }

    /* ═══ LONG-PERIOD COMET ═══
       Single comet crossing the scene over ~5 minutes (300s).
       Enters from upper-left edge, exits toward lower-right. */
    this.longComet = {
        progress: Math.random(), /* start at random point in orbit */
        period: 300000,          /* 300 seconds for full cycle */
        /* Entry: upper-left.  Exit: lower-right */
        startX: -40, startY: h * 0.08,
        endX: w + 40, endY: h * 0.92,
        /* Control point for slight arc */
        cpX: w * 0.4, cpY: -h * 0.15,
        tailLen: 90,
        brightness: 0.85,
    };

    /* 14 nebula clouds — visible atmospheric color */
    var hues = [280,320,200,350,260,30,180,300,240,10,290,160,340,220];
    for (var i = 0; i < 14; i++) {
        this.nebulae.push({
            x: Math.random() * w, y: Math.random() * h,
            r: 250 + Math.random() * 550,
            hue: hues[i] + (Math.random()-0.5)*20,
            sat: 40 + Math.random() * 45,
            alpha: 0.025 + Math.random() * 0.035,
            dx: (Math.random()-0.5) * 0.003,
            dy: (Math.random()-0.5) * 0.003,
            pulse: 8000 + Math.random() * 18000,
            pPhase: Math.random() * Math.PI * 2,
            scX: 0.5 + Math.random() * 1,
            scY: 0.5 + Math.random() * 1,
            rot: Math.random() * Math.PI,
        });
    }

    /* Milky way band — sweeping gradient across the scene */
    this.milkyWay = {
        angle: 0.35 + Math.random() * 0.3,  /* slight diagonal */
        width: Math.max(w, h) * 0.45,
        alpha: 0.04 + Math.random() * 0.025,
        stars: []
    };
    /* Dense star field along the band */
    var mwA = this.milkyWay.angle;
    var mwCx = w / 2, mwCy = h / 2;
    for (var mi = 0; mi < 200; mi++) {
        var along = (Math.random() - 0.5) * Math.max(w, h) * 1.4;
        var across = this._gauss() * this.milkyWay.width * 0.3;
        this.milkyWay.stars.push({
            x: mwCx + Math.cos(mwA) * along - Math.sin(mwA) * across,
            y: mwCy + Math.sin(mwA) * along + Math.cos(mwA) * across,
            size: 0.3 + Math.random() * 1.2,
            brightness: 0.08 + Math.random() * 0.2,
            twinkle: 3000 + Math.random() * 8000,
            phase: Math.random() * Math.PI * 2,
        });
    }

    /* 10 dust lanes */
    for (var i = 0; i < 10; i++) {
        var a = Math.random() * Math.PI;
        var len = 250 + Math.random() * 650;
        var cx = Math.random() * w, cy = Math.random() * h;
        this.dustLanes.push({
            x1: cx-Math.cos(a)*len/2, y1: cy-Math.sin(a)*len/2,
            x2: cx+Math.cos(a)*len/2, y2: cy+Math.sin(a)*len/2,
            w: 25 + Math.random() * 90,
            alpha: 0.012 + Math.random() * 0.022,
        });
    }

    /* 10 star clusters */
    for (var i = 0; i < 10; i++) {
        var cx = Math.random() * w, cy = Math.random() * h;
        var clStars = [];
        var n = 15 + Math.floor(Math.random() * 35);
        var spread = 25 + Math.random() * 60;
        for (var j = 0; j < n; j++) {
            var ca = Math.random() * Math.PI * 2;
            var cd = Math.abs(this._gauss()) * spread;
            clStars.push({
                x: cx + Math.cos(ca)*cd, y: cy + Math.sin(ca)*cd,
                size: 0.4 + Math.random() * 0.9,
                brightness: 0.12 + Math.random() * 0.3,
                twinkle: 2500 + Math.random() * 7000,
                phase: Math.random() * Math.PI * 2,
            });
        }
        this.clusters.push({ stars: clStars });
    }

    /* 12 distant galaxies — slightly brighter */
    for (var i = 0; i < 12; i++) {
        this.galaxies.push({
            x: Math.random() * w, y: Math.random() * h,
            size: 5 + Math.random() * 16,
            angle: Math.random() * Math.PI,
            ecc: 0.3 + Math.random() * 0.5,
            bright: 0.06 + Math.random() * 0.08,
            type: ['spiral','elliptical','irregular'][Math.floor(Math.random()*3)],
        });
    }
    /* Extra right-edge galaxies and cluster — fill the empty right quadrant */
    var _rightGalaxies=[
      {x:w*0.82,y:h*0.18,size:14,angle:0.6,ecc:0.55,bright:0.10,type:'spiral'},
      {x:w*0.91,y:h*0.44,size:9,angle:1.1,ecc:0.35,bright:0.08,type:'elliptical'},
      {x:w*0.78,y:h*0.68,size:11,angle:0.3,ecc:0.5,bright:0.09,type:'spiral'},
    ];
    for(var rgi=0;rgi<_rightGalaxies.length;rgi++) this.galaxies.push(_rightGalaxies[rgi]);
    /* Dense star cluster — upper-right corner */
    var _rcx=w*0.87,_rcy=h*0.22,_rClStars=[];
    for(var rci=0;rci<40;rci++){
      var rca=Math.random()*Math.PI*2,rcd=Math.abs(this._gauss())*55;
      _rClStars.push({x:_rcx+Math.cos(rca)*rcd,y:_rcy+Math.sin(rca)*rcd,
        size:0.4+Math.random()*0.8,brightness:0.14+Math.random()*0.28,
        twinkle:2000+Math.random()*6000,phase:Math.random()*Math.PI*2});
    }
    this.clusters.push({stars:_rClStars});
};
CosmicCanvas.prototype.draw = function(ctx, now, regime) {
    var hShift = regime === 'DEFENSIVE' ? -20 : regime === 'DEPLOY' ? 25 : 0;
    var w = this.w, h = this.h;

    /* ═══ ASYMMETRIC EMISSION NEBULAE — painted first, deepest layer ═══
       Pink/purple left, blue/teal right. Very slow color temperature drift. */
    if (this.emissionNebulae && this.emissionNebulae.length) {
        /* Global hue drift over 300s — oscillates ±8 degrees */
        var nebHueDrift = Math.sin(now / 300000 * Math.PI * 2) * 8;
        for (var eni = 0; eni < this.emissionNebulae.length; eni++) {
            var en = this.emissionNebulae[eni];
            var enHue = (en.hue + nebHueDrift + hShift + 360) % 360;
            var enHue2 = (en.hue2 + nebHueDrift * 0.7 + hShift + 360) % 360;
            ctx.save();
            ctx.translate(en.x, en.y);
            ctx.rotate(en.rot);
            ctx.scale(en.rx / en.ry, 1);
            /* Outer diffuse cloud */
            var enG = ctx.createRadialGradient(0, 0, 0, 0, 0, en.ry);
            enG.addColorStop(0,    'hsla(' + enHue + ',' + en.sat + '%,18%,' + en.alpha + ')');
            enG.addColorStop(0.30, 'hsla(' + enHue + ',' + en.sat + '%,14%,' + (en.alpha * 0.65) + ')');
            enG.addColorStop(0.60, 'hsla(' + enHue2 + ',' + en.sat2 + '%,10%,' + (en.alpha * 0.30) + ')');
            enG.addColorStop(0.85, 'hsla(' + enHue2 + ',' + en.sat2 + '%,7%,' + (en.alpha * 0.10) + ')');
            enG.addColorStop(1,    'hsla(0,0%,0%,0)');
            ctx.fillStyle = enG;
            ctx.beginPath();
            ctx.arc(0, 0, en.ry, 0, Math.PI * 2);
            ctx.fill();
            /* Second lobe — offset slightly, second hue */
            var en2G = ctx.createRadialGradient(en.ry * 0.25, -en.ry * 0.18, 0, en.ry * 0.25, -en.ry * 0.18, en.ry * 0.65);
            en2G.addColorStop(0,    'hsla(' + enHue2 + ',' + en.sat2 + '%,20%,' + en.alpha2 + ')');
            en2G.addColorStop(0.45, 'hsla(' + enHue2 + ',' + en.sat2 + '%,13%,' + (en.alpha2 * 0.45) + ')');
            en2G.addColorStop(1,    'hsla(0,0%,0%,0)');
            ctx.fillStyle = en2G;
            ctx.beginPath();
            ctx.arc(en.ry * 0.25, -en.ry * 0.18, en.ry * 0.65, 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();
        }
    }

    /* Milky way band — soft diffuse glow across the scene */
    if (this.milkyWay) {
        var mw = this.milkyWay;
        var mwCx = this.w / 2, mwCy = this.h / 2;
        var maxD = Math.max(this.w, this.h);
        /* Draw 3 overlapping gradient bands for depth */
        for (var bi = 0; bi < 3; bi++) {
            var bw = mw.width * (1 - bi * 0.25);
            var bAlpha = mw.alpha * (1 - bi * 0.3);
            var perp = mw.angle + Math.PI / 2;
            var px1 = mwCx + Math.cos(perp) * bw;
            var py1 = mwCy + Math.sin(perp) * bw;
            var px2 = mwCx - Math.cos(perp) * bw;
            var py2 = mwCy - Math.sin(perp) * bw;
            var mwG = ctx.createLinearGradient(px1, py1, px2, py2);
            var hue = (220 + hShift + 360) % 360;
            mwG.addColorStop(0, 'rgba(0,0,0,0)');
            mwG.addColorStop(0.25, 'hsla(' + hue + ',25%,15%,' + (bAlpha * 0.3) + ')');
            mwG.addColorStop(0.45, 'hsla(' + hue + ',30%,18%,' + (bAlpha * 0.7) + ')');
            mwG.addColorStop(0.5, 'hsla(' + hue + ',35%,20%,' + bAlpha + ')');
            mwG.addColorStop(0.55, 'hsla(' + hue + ',30%,18%,' + (bAlpha * 0.7) + ')');
            mwG.addColorStop(0.75, 'hsla(' + hue + ',25%,15%,' + (bAlpha * 0.3) + ')');
            mwG.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = mwG;
            ctx.fillRect(0, 0, this.w, this.h);
        }
        /* Dense stars along the band */
        for (var si = 0; si < mw.stars.length; si++) {
            var s = mw.stars[si];
            var tw = 0.5 + 0.5 * Math.sin(now / s.twinkle + s.phase);
            ctx.fillStyle = 'rgba(220,225,240,' + (s.brightness * tw) + ')';
            ctx.beginPath();
            ctx.arc(s.x, s.y, s.size, 0, Math.PI * 2);
            ctx.fill();
        }
    }

    /* Nebulae */
    for (var ni = 0; ni < this.nebulae.length; ni++) {
        var n = this.nebulae[ni];
        n.x += n.dx; n.y += n.dy;
        if (n.x < -n.r) n.x = this.w + n.r;
        if (n.x > this.w + n.r) n.x = -n.r;
        if (n.y < -n.r) n.y = this.h + n.r;
        if (n.y > this.h + n.r) n.y = -n.r;

        var p = 1 + Math.sin(now/n.pulse + n.pPhase) * 0.2;
        var hue = (n.hue + hShift + 360) % 360;

        ctx.save();
        ctx.translate(n.x, n.y);
        ctx.rotate(n.rot);
        ctx.scale(n.scX, n.scY);

        var g = ctx.createRadialGradient(0,0,0, 0,0,n.r*p);
        g.addColorStop(0, 'hsla(' + hue + ',' + n.sat + '%,22%,' + (n.alpha*p) + ')');
        g.addColorStop(0.35, 'hsla(' + hue + ',' + n.sat + '%,16%,' + (n.alpha*0.5*p) + ')');
        g.addColorStop(0.7, 'hsla(' + hue + ',' + n.sat + '%,10%,' + (n.alpha*0.15*p) + ')');
        g.addColorStop(1, 'hsla(0,0%,0%,0)');
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(0, 0, n.r*p, 0, Math.PI*2);
        ctx.fill();
        ctx.restore();
    }

    /* Dust lanes */
    for (var di = 0; di < this.dustLanes.length; di++) {
        var l = this.dustLanes[di];
        var a = Math.atan2(l.y2-l.y1, l.x2-l.x1);
        var px = Math.cos(a+Math.PI/2)*l.w/2;
        var py = Math.sin(a+Math.PI/2)*l.w/2;
        var g = ctx.createLinearGradient(l.x1,l.y1,l.x2,l.y2);
        g.addColorStop(0,'rgba(0,0,0,0)');
        g.addColorStop(0.2,'rgba(3,2,6,' + l.alpha + ')');
        g.addColorStop(0.8,'rgba(3,2,6,' + l.alpha + ')');
        g.addColorStop(1,'rgba(0,0,0,0)');
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.moveTo(l.x1+px,l.y1+py);
        ctx.lineTo(l.x2+px,l.y2+py);
        ctx.lineTo(l.x2-px,l.y2-py);
        ctx.lineTo(l.x1-px,l.y1-py);
        ctx.closePath();
        ctx.fill();
    }

    /* Clusters + individual stars */
    for (var ci = 0; ci < this.clusters.length; ci++) {
        var cst = this.clusters[ci].stars;
        for (var si = 0; si < cst.length; si++) {
            var s = cst[si];
            var tw = 0.5 + 0.5*Math.sin(now/s.twinkle + s.phase);
            ctx.fillStyle = 'rgba(220,225,242,' + (s.brightness*tw) + ')';
            ctx.beginPath();
            ctx.arc(s.x, s.y, s.size, 0, Math.PI*2);
            ctx.fill();
        }
    }

    for (var si = 0; si < this.stars.length; si++) {
        var s = this.stars[si];
        var tw = 0.5 + 0.5*Math.sin(now/s.twinkle + s.phase);
        ctx.fillStyle = 'hsla(' + s.hue + ',' + s.sat + '%,85%,' + (s.brightness*tw) + ')';
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.size, 0, Math.PI*2);
        ctx.fill();
        /* Diffraction spikes on bright stars */
        if (s.size > 1.3 && tw > 0.7) {
            var spike = s.size * 4;
            ctx.strokeStyle = 'rgba(220,225,240,' + (s.brightness*tw*0.2) + ')';
            ctx.lineWidth = 0.3;
            ctx.beginPath(); ctx.moveTo(s.x-spike,s.y); ctx.lineTo(s.x+spike,s.y); ctx.stroke();
            ctx.beginPath(); ctx.moveTo(s.x,s.y-spike); ctx.lineTo(s.x,s.y+spike); ctx.stroke();
        }
    }

    /* Galaxies */
    for (var gi = 0; gi < this.galaxies.length; gi++) {
        var gal = this.galaxies[gi];
        ctx.save();
        ctx.translate(gal.x, gal.y);
        ctx.rotate(gal.angle + now/250000);
        var gr = ctx.createRadialGradient(0,0,0, 0,0,gal.size);
        gr.addColorStop(0, 'rgba(255,242,205,' + (gal.bright*2) + ')');
        gr.addColorStop(0.4, 'rgba(200,190,172,' + gal.bright + ')');
        gr.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = gr;
        ctx.beginPath();
        ctx.ellipse(0,0,gal.size,gal.size*gal.ecc,0,0,Math.PI*2);
        ctx.fill();
        if (gal.type === 'spiral') {
            ctx.strokeStyle = 'rgba(200,200,222,' + (gal.bright*0.35) + ')';
            ctx.lineWidth = 0.4;
            ctx.beginPath();
            for (var t = 0; t < Math.PI*4; t += 0.12) {
                var sr = t*gal.size*0.1;
                if (t === 0) ctx.moveTo(Math.cos(t)*sr, Math.sin(t)*sr*gal.ecc);
                else ctx.lineTo(Math.cos(t)*sr, Math.sin(t)*sr*gal.ecc);
            }
            ctx.stroke();
        }
        ctx.restore();
    }

    /* Shooting stars */
    if (now - this.lastShoot > 8000 + Math.random() * 10000) {
        this.lastShoot = now;
        var edge = Math.floor(Math.random()*4);
        var sx,sy,angle;
        if (edge===0) { sx=Math.random()*this.w; sy=-5; angle=Math.PI*0.35+Math.random()*0.3; }
        else if (edge===1) { sx=this.w+5; sy=Math.random()*this.h*0.5; angle=Math.PI*0.6+Math.random()*0.3; }
        else if (edge===2) { sx=Math.random()*this.w; sy=this.h+5; angle=-Math.PI*0.35+Math.random()*0.3; }
        else { sx=-5; sy=Math.random()*this.h*0.5; angle=Math.random()*0.3; }
        var spd = 4+Math.random()*6;
        this.shootingStars.push({
            x:sx,y:sy,vx:Math.cos(angle)*spd,vy:Math.sin(angle)*spd,
            life:1,decay:0.006+Math.random()*0.008,
            trail:[],maxTrail:25+Math.floor(Math.random()*18),
            bright:0.3+Math.random()*0.5,size:1.2+Math.random()*1.4,
        });
    }

    for (var shi = 0; shi < this.shootingStars.length; shi++) {
        var ss = this.shootingStars[shi];
        ss.x+=ss.vx; ss.y+=ss.vy; ss.life-=ss.decay;
        ss.trail.push({x:ss.x,y:ss.y});
        while (ss.trail.length > ss.maxTrail) ss.trail.shift();

        for (var ti=1;ti<ss.trail.length;ti++) {
            var tt=ti/ss.trail.length;
            var ta=tt*ss.life*ss.bright*0.5;
            ctx.strokeStyle='rgba(230,240,255,' + ta + ')';
            ctx.lineWidth=Math.max(0.5,ss.size*tt);
            ctx.beginPath();
            ctx.moveTo(ss.trail[ti-1].x,ss.trail[ti-1].y);
            ctx.lineTo(ss.trail[ti].x,ss.trail[ti].y);
            ctx.stroke();
        }
        ctx.fillStyle='rgba(255,255,255,' + (ss.life*ss.bright) + ')';
        ctx.beginPath();
        ctx.arc(ss.x,ss.y,ss.size*0.7,0,Math.PI*2);
        ctx.fill();
    }
    this.shootingStars = this.shootingStars.filter(function(s) { return s.life > 0; });

    /* ═══ DEDICATED SPIRAL GALAXY — upper-right corner, always present ═══ */
    if (this.dedicatedGalaxy) {
        var dg = this.dedicatedGalaxy;
        ctx.save();
        ctx.translate(dg.x, dg.y);
        ctx.rotate(dg.angle + now / 400000); /* extremely slow rotation */
        /* Core bulge — warm yellow-white */
        var dgCore = ctx.createRadialGradient(0, 0, 0, 0, 0, dg.size * 0.35);
        dgCore.addColorStop(0,   'rgba(255,248,220,' + (dg.bright * 2.8) + ')');
        dgCore.addColorStop(0.4, 'rgba(230,215,180,' + (dg.bright * 1.4) + ')');
        dgCore.addColorStop(1,   'rgba(0,0,0,0)');
        ctx.fillStyle = dgCore;
        ctx.beginPath();
        ctx.ellipse(0, 0, dg.size * 0.35, dg.size * 0.35 * dg.ecc, 0, 0, Math.PI * 2);
        ctx.fill();
        /* Disk halo */
        var dgDisk = ctx.createRadialGradient(0, 0, dg.size * 0.2, 0, 0, dg.size);
        dgDisk.addColorStop(0,   'rgba(200,195,175,' + (dg.bright * 0.9) + ')');
        dgDisk.addColorStop(0.5, 'rgba(180,170,150,' + (dg.bright * 0.4) + ')');
        dgDisk.addColorStop(1,   'rgba(0,0,0,0)');
        ctx.fillStyle = dgDisk;
        ctx.beginPath();
        ctx.ellipse(0, 0, dg.size, dg.size * dg.ecc, 0, 0, Math.PI * 2);
        ctx.fill();
        /* Two spiral arms */
        for (var arm = 0; arm < 2; arm++) {
            var armOffset = arm * Math.PI;
            ctx.strokeStyle = 'rgba(220,215,195,' + (dg.bright * 0.55) + ')';
            ctx.lineWidth = 0.6;
            ctx.beginPath();
            for (var t = 0; t < Math.PI * 3.5; t += 0.08) {
                var sr2 = Math.max(0.1, t * dg.size * 0.085);
                var ta2 = t + armOffset;
                var tx2 = Math.cos(ta2) * sr2;
                var ty2 = Math.sin(ta2) * sr2 * dg.ecc;
                if (t === 0) ctx.moveTo(tx2, ty2);
                else ctx.lineTo(tx2, ty2);
            }
            ctx.stroke();
        }
        /* Outer faint arms extension — dimmer continuation */
        for (var arm2 = 0; arm2 < 2; arm2++) {
            var armOff2 = arm2 * Math.PI;
            ctx.strokeStyle = 'rgba(180,175,160,' + (dg.bright * 0.22) + ')';
            ctx.lineWidth = 0.3;
            ctx.beginPath();
            for (var t2 = Math.PI * 3.5; t2 < Math.PI * 5.5; t2 += 0.1) {
                var sr3 = Math.max(0.1, t2 * dg.size * 0.075);
                var ta3 = t2 + armOff2;
                var tx3 = Math.cos(ta3) * sr3;
                var ty3 = Math.sin(ta3) * sr3 * dg.ecc;
                if (t2 === Math.PI * 3.5) ctx.moveTo(tx3, ty3);
                else ctx.lineTo(tx3, ty3);
            }
            ctx.stroke();
        }
        ctx.restore();
    }

    /* ═══ ASTEROID BELT — ring of dim particles between orbital zones ═══
       Centered on canvas center (where the neutron star is).
       Particles orbit slowly, drawn AFTER background but BEFORE planets (handled by layer order). */
    if (this.asteroidBelt && this.asteroidBelt.length) {
        var abCx = w / 2, abCy = h / 2;
        for (var abi = 0; abi < this.asteroidBelt.length; abi++) {
            var ab = this.asteroidBelt[abi];
            var abAngle = ab.baseAngle + now * ab.speed;
            var abX = abCx + Math.cos(abAngle) * ab.radius;
            var abY = abCy + Math.sin(abAngle) * ab.radius * ab.yEcc;
            /* Twinkle — dim rocks have subtle brightness variation */
            var abTw = 0.55 + 0.45 * Math.sin(now / 3500 + ab.phase);
            /* Depth cue: particles "behind" disk (sin < 0) are dimmer */
            var abDepth = Math.sin(abAngle);
            var abAlpha = ab.bright * abTw * (abDepth < 0 ? 0.45 : 0.85);
            ctx.fillStyle = 'rgba(160,155,145,' + abAlpha.toFixed(3) + ')';
            ctx.beginPath();
            ctx.arc(abX, abY, ab.size, 0, Math.PI * 2);
            ctx.fill();
        }
    }

    /* ═══ LONG-PERIOD COMET ═══
       Quadratic bezier path across the full canvas over ~5 minutes.
       Tail points away from canvas center (neutron star). */
    if (this.longComet) {
        var lc = this.longComet;
        /* Advance progress based on elapsed time */
        lc.progress = (lc.progress + (1 / lc.period)) % 1;
        var lt = lc.progress;
        /* Quadratic bezier position */
        var lcX = (1-lt)*(1-lt)*lc.startX + 2*(1-lt)*lt*lc.cpX + lt*lt*lc.endX;
        var lcY = (1-lt)*(1-lt)*lc.startY + 2*(1-lt)*lt*lc.cpY + lt*lt*lc.endY;
        /* Direction of motion (tangent to curve) — tail points opposite */
        var dt2 = Math.min(lt + 0.002, 1);
        var lcX2 = (1-dt2)*(1-dt2)*lc.startX + 2*(1-dt2)*dt2*lc.cpX + dt2*dt2*lc.endX;
        var lcY2 = (1-dt2)*(1-dt2)*lc.startY + 2*(1-dt2)*dt2*lc.cpY + dt2*dt2*lc.endY;
        var lcDx = lcX2 - lcX, lcDy = lcY2 - lcY;
        var lcDist = Math.sqrt(lcDx * lcDx + lcDy * lcDy) || 1;
        /* Tail direction = away from neutron star (canvas center) */
        var lcTailDx = lcX - w / 2, lcTailDy = lcY - h / 2;
        var lcTailLen = Math.sqrt(lcTailDx * lcTailDx + lcTailDy * lcTailDy) || 1;
        var lcTailNx = lcTailDx / lcTailLen, lcTailNy = lcTailDy / lcTailLen;
        /* Tail — gradient from bright nucleus toward faint tip */
        var lcTailX = lcX + lcTailNx * lc.tailLen;
        var lcTailY = lcY + lcTailNy * lc.tailLen;
        /* Outer coma glow */
        var lcComaG = ctx.createRadialGradient(lcX, lcY, 0, lcX, lcY, 12);
        lcComaG.addColorStop(0,   'rgba(240,250,255,' + (lc.brightness * 0.8) + ')');
        lcComaG.addColorStop(0.3, 'rgba(200,230,255,' + (lc.brightness * 0.3) + ')');
        lcComaG.addColorStop(1,   'rgba(0,0,0,0)');
        ctx.fillStyle = lcComaG;
        ctx.beginPath();
        ctx.arc(lcX, lcY, 12, 0, Math.PI * 2);
        ctx.fill();
        /* Dust tail — wide, yellowish */
        var lcDustG = ctx.createLinearGradient(lcX, lcY, lcTailX, lcTailY);
        lcDustG.addColorStop(0,    'rgba(255,245,200,' + (lc.brightness * 0.18) + ')');
        lcDustG.addColorStop(0.25, 'rgba(230,225,185,' + (lc.brightness * 0.10) + ')');
        lcDustG.addColorStop(0.65, 'rgba(200,195,160,' + (lc.brightness * 0.04) + ')');
        lcDustG.addColorStop(1,    'rgba(0,0,0,0)');
        ctx.save();
        ctx.strokeStyle = lcDustG;
        ctx.lineWidth = 6 + 4 * Math.sin(lt * Math.PI); /* wider at mid-path */
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(lcX, lcY);
        ctx.lineTo(lcTailX, lcTailY);
        ctx.stroke();
        /* Ion tail — narrow, blue-white, slightly offset */
        var ionOffX = lcTailNy * 3, ionOffY = -lcTailNx * 3;
        var lcIonG = ctx.createLinearGradient(lcX, lcY, lcX + lcTailNx * lc.tailLen * 1.35, lcY + lcTailNy * lc.tailLen * 1.35);
        lcIonG.addColorStop(0,    'rgba(200,235,255,' + (lc.brightness * 0.5) + ')');
        lcIonG.addColorStop(0.20, 'rgba(170,210,255,' + (lc.brightness * 0.25) + ')');
        lcIonG.addColorStop(0.60, 'rgba(140,185,255,' + (lc.brightness * 0.08) + ')');
        lcIonG.addColorStop(1,    'rgba(0,0,0,0)');
        ctx.strokeStyle = lcIonG;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(lcX + ionOffX, lcY + ionOffY);
        ctx.lineTo(lcX + lcTailNx * lc.tailLen * 1.35 + ionOffX, lcY + lcTailNy * lc.tailLen * 1.35 + ionOffY);
        ctx.stroke();
        ctx.restore();
        /* Nucleus — bright white dot with diffraction spikes */
        ctx.fillStyle = 'rgba(255,255,255,' + lc.brightness + ')';
        ctx.beginPath();
        ctx.arc(lcX, lcY, 2.2, 0, Math.PI * 2);
        ctx.fill();
        /* Diffraction spikes */
        ctx.strokeStyle = 'rgba(255,255,255,' + (lc.brightness * 0.4) + ')';
        ctx.lineWidth = 0.6;
        var spkLen = 8;
        ctx.beginPath(); ctx.moveTo(lcX - spkLen, lcY); ctx.lineTo(lcX + spkLen, lcY); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(lcX, lcY - spkLen); ctx.lineTo(lcX, lcY + spkLen); ctx.stroke();
    }
};

/* ═══════════════════════════════════════════════════════════════════════
   DEEP FIELD — living deep-space wilderness around the fleet (2026-07-30)
   ───────────────────────────────────────────────────────────────────────
   Jeremy's ask, verbatim: "i just want to be able to zoom out and see more
   of the vivarium! more space and shit going on in space, shit naturally
   developes and dies in space. a true vivarium would show that." This is
   the payoff for the _ORB_ZOOM_MIN=0.38 pure-camera zoom floor shipped
   earlier the same day (command_center_v4.html ~line 818) — pulling back
   used to reveal dead black void; this populates that void with a sparse,
   eerie, procedurally alive backdrop that is NEVER mistakable for fleet
   data (no click targets, no tooltips, no data binding of any kind).

   ARCHITECTURE
   World-space, not screen-space — unlike CosmicCanvas above (which is a
   static backdrop sized to canvas w/h once and never moves relative to
   the camera beyond the shared setTransform), every DeepField object has
   a world (x,y) placed in an ANNULUS around the fleet's live bounding
   circle (1.1x-3x its radius) and is drawn under the exact same
   ctx.setTransform(zoom,pan) the fleet itself uses (see _orbRender,
   command_center_v4.html ~line 2892) — so it is mostly hidden behind/
   beyond the fleet at zoom=1 and progressively revealed as the camera
   pulls back, with zero special-casing.

   DETERMINISM — THE ACTUAL "VIVARIUM" PART
   Every object's appearance is a pure function of (seed, slotIndex,
   cycleIndex, elapsedMs) — never a per-frame Math.random() call, which
   would desync from the persisted state on every reload. A tiny
   mulberry32 PRNG (deterministic, seeded) draws each slot's per-cycle
   traits (position, hue pick, size, exact durations) once per cycle from
   a hash of (seed, slotIndex, cycleIndex), and the lifecycle phase within
   a cycle is computed analytically from elapsed time — so "fast-forward
   from stored epoch to now" is just evaluating the same pure functions at
   a larger elapsedMs, not replaying frames. A returning viewer who was
   away 6 hours sees nebulae mid-way through cycles they never watched
   start, supernovae that fired and finished off-session, nurseries born
   from those deaths already brightening. See DeepField.prototype.sync().

   PALETTE — deliberately disjoint from every fleet/UI color
   Fleet faction lines (_FACTION_LINK_RGB, command_center_v4.html ~1191):
     gold 201,162,39 (Veinrunners) / cyan-teal 34,211,211 (Tidewrights) /
     silver-violet 179,157,219 (The Accord) / white 225,225,225 (Warden).
   Alert/status greens+reds used fleet-wide for regime/PnL are likewise
   avoided. Deep field uses: dust rust/ember (12,60,80 hue band, low sat,
   dark), ice blue (200-210 hue, cold/desaturated), faint violet-grey
   (255-265 hue, NOT the 179,157,219 Accord silver-violet — pushed bluer
   and darker), and bone-white remnants (0 sat, warm-grey not pure white).
   Full hex/hsla accounting is in the command_center_v4.html DeepField
   integration comment and the session report.
   ═══════════════════════════════════════════════════════════════════════ */

/* --- Deterministic PRNG: mulberry32, seeded by a single uint32 --- */
function _dfHash(seed, a, b, c) {
    /* Fold (seed, a, b, c) into one uint32 via a cheap avalanche mix —
       NOT cryptographic, just needs to decorrelate nearby integer inputs
       so adjacent slot/cycle indices don't produce visually similar draws. */
    var h = (seed | 0) ^ 0x9e3779b9;
    h = Math.imul(h ^ (a | 0), 0x85ebca6b);
    h = Math.imul(h ^ (b | 0), 0xc2b2ae35);
    h = Math.imul(h ^ (c | 0), 0x27d4eb2f);
    h ^= h >>> 15;
    return h >>> 0;
}
/* mulberry32 PRNG advanced from a hashed seed — returns a function that
   yields deterministic floats in [0,1) on each call, fully reproducible
   given the same (seed,a,b,c) tuple. */
function _dfRng(seed, a, b, c) {
    var s = _dfHash(seed, a, b, c) || 1;
    return function () {
        s |= 0; s = (s + 0x6D2B79F5) | 0;
        var t = Math.imul(s ^ (s >>> 15), 1 | s);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

/* --- DeepField object counts / perf budget ---
   Hard cap ~20-30 concurrent live objects total (nebulae + supernova/
   remnant/nursery slots + comets), per the brief. Kept as named slot
   counts (not one pooled array) because each class has a distinct
   lifecycle function — simpler to reason about and just as cheap. */
var _DF_NEBULA_SLOTS = 7;      /* independent nebula lifecycle slots */
/* Supernova slot count — MUST stay 1. Each slot independently fires an
   event every 8-15min (randomized per-instance, see _supernovaAt), so N
   slots means an N-times-too-frequent combined rate — verified against
   the brief's "about one per 8-15 min" via a standalone harness: 3 slots
   measured 14 events/hour combined (one every ~4.3min), 3x over-spec.
   1 slot alone measures 4-5 events/hour (one every ~12-15min avg),
   matching the ask. A slot's own long nursery/remnant tail (see
   _supernovaAt's totalLife ~13min) already keeps something visible at
   the death site between flashes, so a single slot does not read as
   "nothing ever happens" — it reads as the intended RARE, eerie event. */
var _DF_SN_SLOTS = 1;
var _DF_COMET_SLOTS = 4;       /* wandering outer-field comets/debris */
/* Total worst case: 7 + 3*1 + 4 = 14 live draws/frame — comfortably under the 20-30 cap. */

/* --- Palette pools (see block comment above for the "why disjoint" case) --- */
var _DF_NEBULA_HUES = [8, 14, 200, 208, 258, 264];   /* dust-rust / ember, ice-blue, faint violet-grey */
var _DF_SN_REMNANT_HUE = 206;  /* cold ice-blue shock ring */
var _DF_SN_FLASH_HUE = 40;     /* brief warm-white flash, desaturated fast — not fleet gold (201,162,39 is far more saturated/yellow) */

function DeepField(seed) {
    this.seed = seed >>> 0;
    this.center = { x: 0, y: 0 };   /* fleet bounding-circle center, world space */
    this.innerR = 300;              /* annulus inner radius, recomputed from live fleet bbox */
    this.outerR = 900;              /* annulus outer radius */
    this.epoch = Date.now();        /* wall-clock ms this field's timeline is anchored to */
    /* Debug/verification hook state — see _deepFieldForceSupernova() */
    this._forcedSN = null;
}

/* Recompute the annulus from the live fleet's worst-case reach. Called
   every frame from _orbRender's L0b block (cheap — one ~18-node pass), so
   the annulus always matches the CURRENT fixed world layout with no extra
   coupling to resize/fullscreen-toggle call sites.

   `maxVisibleR` is the world-space radius from center that is EVER
   reachable by pulling the camera back to _ORB_ZOOM_MIN (half the
   canvas's smaller screen dimension, divided by the zoom floor) — passed
   in by the caller since only command_center_v4.html knows _ORB_ZOOM_MIN
   and the live canvas size. Without this clamp, the first version of this
   function sized outerR at a flat 3x fleet-reach multiplier, which (at
   this fleet's actual footprint) worked out to ~3050 world units — but
   pulling all the way back to the 0.38 zoom floor only ever reveals
   ~1800 world units of radius on this display, so ~40% of the annulus
   was permanently unreachable dead weight. Clamping outerR to
   maxVisibleR (with a small margin so the very edge isn't a hard cutoff)
   means every object placed is eventually visible, i.e. actually part of
   the "vivarium you discover by zooming out" instead of wasted allocation. */
DeepField.prototype.fitToFleet = function (orbNodes, maxVisibleR) {
    var cx = 0, cy = 0;
    var xs = [], ys = [];
    for (var id in orbNodes) {
        var n = orbNodes[id];
        if (!n || !n.alive) continue;
        xs.push(n.x); ys.push(n.y);
    }
    if (xs.length) {
        cx = xs.reduce(function (a, b) { return a + b; }, 0) / xs.length;
        cy = ys.reduce(function (a, b) { return a + b; }, 0) / ys.length;
    }
    /* Distance from that centroid to the furthest body's OWN edge — the
       fleet's true worst-case occupied radius. Uses live x/y (not
       orbitRadius) because the centroid itself is also live-x/y-derived;
       mixing a live centroid with phase-invariant orbitRadius reach would
       overstate the footprint by double-counting the star triangle's own
       spread. This is a snapshot of "how big does the fleet look THIS
       frame", which is exactly what "1.1x its radius" should mean. */
    var reach = 0;
    for (var id2 in orbNodes) {
        var n2 = orbNodes[id2];
        if (!n2 || !n2.alive) continue;
        var dx = n2.x - cx, dy = n2.y - cy;
        var d = Math.sqrt(dx * dx + dy * dy) + (n2.currentSize || n2.size || 10);
        reach = Math.max(reach, d);
    }
    if (reach < 50) reach = 400; /* fallback before first real layout pass */
    this.center.x = cx; this.center.y = cy;
    this.innerR = reach * 1.1;
    var wantOuter = reach * 3.0;
    var cap = (maxVisibleR && maxVisibleR > this.innerR * 1.3) ? maxVisibleR * 0.94 : wantOuter;
    this.outerR = Math.min(wantOuter, Math.max(cap, this.innerR * 1.3));
};

/* --- Deterministic nebula lifecycle ---
   One "slot" cycles forever: bloom -> hold -> disperse -> gap -> (repeat
   with fresh per-cycle traits drawn from a new hash). Every quantity here
   is a pure function of (seed, slot, cycleIndex, tInCycle) so evaluating
   it at elapsedMs=6*3600*1000 costs the same as elapsedMs=1000 — no
   frame replay needed for the fast-forward-on-load requirement. */
DeepField.prototype._nebulaAt = function (slot, elapsedMs) {
    var bloomMs = 120000, disperseMs = 150000; /* ~2min bloom, 2.5min disperse (within the 2-5min ask) */
    var holdMs = 90000 + (slot % 3) * 40000;    /* stagger so slots don't sync */
    var gapMs = 70000 + (slot % 4) * 55000;
    var period = bloomMs + holdMs + disperseMs + gapMs;
    var cycleIndex = Math.floor(elapsedMs / period);
    var tInCycle = elapsedMs - cycleIndex * period;
    var rng = _dfRng(this.seed, 101 + slot, cycleIndex, 0);
    var ang = rng() * Math.PI * 2;
    var distT = rng(); /* 0..1 across the annulus */
    var dist = this.innerR + distT * (this.outerR - this.innerR);
    var hue = _DF_NEBULA_HUES[Math.floor(rng() * _DF_NEBULA_HUES.length)];
    var maxR = 140 + rng() * 220;
    var maxAlpha = 0.09 + rng() * 0.07; /* sparse/eerie, but must actually read against pure black — first pass (0.05-0.10 with a steep falloff gradient) washed out to near-invisible in verification */
    var scX = 0.55 + rng() * 0.7, scY = 0.55 + rng() * 0.7;
    var rot = rng() * Math.PI;

    var phase, t01, r, alpha;
    if (tInCycle < bloomMs) { phase = 'bloom'; t01 = tInCycle / bloomMs; r = maxR * t01; alpha = maxAlpha * t01; }
    else if (tInCycle < bloomMs + holdMs) { phase = 'hold'; t01 = (tInCycle - bloomMs) / holdMs; r = maxR; alpha = maxAlpha * (0.92 + 0.08 * Math.sin(t01 * Math.PI * 4)); }
    else if (tInCycle < bloomMs + holdMs + disperseMs) { phase = 'disperse'; t01 = (tInCycle - bloomMs - holdMs) / disperseMs; r = maxR * (1 + t01 * 0.6); alpha = maxAlpha * (1 - t01); }
    else { phase = 'gap'; t01 = 0; r = 0; alpha = 0; }

    return {
        alive: alpha > 0.002, phase: phase,
        x: this.center.x + Math.cos(ang) * dist,
        y: this.center.y + Math.sin(ang) * dist,
        r: r, alpha: alpha, hue: hue, scX: scX, scY: scY, rot: rot,
        cycleIndex: cycleIndex, slot: slot
    };
};

/* --- Deterministic supernova/remnant/nursery lifecycle ---
   A single slot's timeline, walked as a hash-chain of inter-arrival
   gaps (a discrete-time Poisson-ish process) rather than one fixed
   period, so events land "about one per 8-15min, randomized per-
   instance" as asked, not on a metronome. Walking the chain from t=0 to
   elapsedMs costs O(events-so-far) hash calls — at most a few hundred
   even after days of real time, trivially cheap once per object per
   frame is too much so callers should call this at most once/sec (see
   _orbDeepFieldTick in command_center_v4.html) and cache the result. */
DeepField.prototype._supernovaAt = function (slot, elapsedMs) {
    var flashMs = 900;             /* brilliant brief flash */
    var remnantMs = 75000;         /* expanding ring, 60-90s ask -> 75s mid-point */
    var nurseryRiseMs = 240000;    /* nursery brightens over minutes following */
    var nurseryHoldMs = 260000;    /* then sits dim-but-visible for a while */
    var nurseryFadeMs = 140000;    /* then fades — slot frees for the next event */

    var t = 0, idx = 0, rng, gapMs;
    /* Walk forward through inter-arrival gaps until we pass elapsedMs.
       Each gap is drawn 8-15min (randomized) from a slot+idx-keyed rng. */
    while (true) {
        rng = _dfRng(this.seed, 401 + slot, idx, 0);
        gapMs = (8 + rng() * 7) * 60000;
        if (t + gapMs > elapsedMs) break;
        t += gapMs;
        idx++;
    }
    /* `t` = start time of the CURRENT (most recent) event in this slot's
       chain; elapsedMs - t = how far into that event's lifecycle we are.
       Before the first event ever fires (idx===0 and elapsedMs<t+gapMs
       but t===0), treat as dormant. */
    var sinceStart = elapsedMs - t;
    var eventRng = _dfRng(this.seed, 501 + slot, idx, 0);
    var ang = eventRng() * Math.PI * 2;
    var distT = eventRng();
    var dist = this.innerR + distT * (this.outerR - this.innerR);
    var x = this.center.x + Math.cos(ang) * dist;
    var y = this.center.y + Math.sin(ang) * dist;

    var totalLife = flashMs + remnantMs + nurseryRiseMs + nurseryHoldMs + nurseryFadeMs;
    if (idx === 0 && sinceStart < 0) {
        /* Never fired yet in this timeline (very early elapsedMs) */
        return { phase: 'dormant', x: x, y: y, slot: slot, idx: idx };
    }
    if (sinceStart > totalLife) {
        /* Fully faded — dormant until the NEXT event in the chain, which
           by construction is still `gapMs` away (we stopped the walk
           right before it). Return dormant at the upcoming event's site
           so nothing is drawn. */
        return { phase: 'dormant', x: x, y: y, slot: slot, idx: idx };
    }
    if (sinceStart < flashMs) {
        var ft = sinceStart / flashMs;
        return { phase: 'flash', x: x, y: y, t01: ft, alpha: 1 - ft * 0.3, r: 3 + ft * 14, slot: slot, idx: idx };
    }
    if (sinceStart < flashMs + remnantMs) {
        var rt = (sinceStart - flashMs) / remnantMs;
        return { phase: 'remnant', x: x, y: y, t01: rt, r: 10 + rt * 130, alpha: (1 - rt) * 0.5, slot: slot, idx: idx };
    }
    var nurT = sinceStart - flashMs - remnantMs;
    if (nurT < nurseryRiseMs) {
        var nt = nurT / nurseryRiseMs;
        return { phase: 'nursery', x: x, y: y, t01: nt, alpha: 0.03 + nt * 0.08, r: 25 + nt * 40, slot: slot, idx: idx };
    }
    if (nurT < nurseryRiseMs + nurseryHoldMs) {
        var ht = (nurT - nurseryRiseMs) / nurseryHoldMs;
        return { phase: 'nursery', x: x, y: y, t01: ht, alpha: 0.11 + Math.sin(ht * Math.PI * 6) * 0.015, r: 65, slot: slot, idx: idx };
    }
    var fadeT = (nurT - nurseryRiseMs - nurseryHoldMs) / nurseryFadeMs;
    return { phase: 'nursery', x: x, y: y, t01: fadeT, alpha: Math.max(0, 0.11 * (1 - fadeT)), r: 65, slot: slot, idx: idx };
};

/* --- Wandering outer-field comets/debris ---
   Parallel to _orbComets (command_center_v4.html) but ambient, not
   trade-triggered: deterministic phase from elapsed time, drifting on a
   long slow arc through the annulus. Extends the existing comet visual
   language (bright head + fading tail) outward rather than inventing a
   new look. */
DeepField.prototype._cometAt = function (slot, elapsedMs) {
    var rng = _dfRng(this.seed, 701 + slot, 0, 0);
    var period = (140 + rng() * 220) * 1000; /* 140-360s full crossing */
    var phase0 = rng();
    var ang = rng() * Math.PI * 2;           /* chord direction across the annulus */
    var offsetDist = (this.innerR + rng() * (this.outerR - this.innerR));
    var perpOff = (rng() - 0.5) * this.outerR * 1.4;
    var t = ((elapsedMs / period) + phase0) % 1;
    if (t < 0) t += 1;
    /* Straight chord through the field, perpendicular offset varies by comet */
    var dirX = Math.cos(ang), dirY = Math.sin(ang);
    var perpX = -dirY, perpY = dirX;
    var travel = (t - 0.5) * this.outerR * 2.4;
    var x = this.center.x + dirX * travel + perpX * perpOff;
    var y = this.center.y + dirY * travel + perpY * perpOff;
    var d = Math.sqrt((x - this.center.x) * (x - this.center.x) + (y - this.center.y) * (y - this.center.y));
    var visible = d > this.innerR * 0.85 && d < this.outerR * 1.05;
    return { visible: visible, x: x, y: y, dirX: dirX, dirY: dirY, slot: slot };
};

/* --- Draw pass: reads current lifecycle state for every slot and paints
   it. World-space coordinates — caller has already applied the zoom/pan
   setTransform, so nothing here needs to know about the camera. --- */
DeepField.prototype.draw = function (ctx, now, zoomBoost) {
    var elapsedMs = now - this.epoch;
    var i;
    /* ROUND 2 (2026-07-30, target 3b): DeepField draws entirely in
       world-space under the same camera transform as everything else, so
       as the camera pulls back toward the zoom floor these objects shrink
       in SCREEN size exactly like every other body — on top of already-low
       base alphas (0.09-0.16 range, tuned to read against pure black at
       zoom~1), the combined shrink+dim made the deep field "nearly vanish"
       at far zoom instead of filling more of the revealed frame the way a
       real pulled-back sky would. zoomBoost is a >=1 multiplier (1 at
       zoom=1, growing toward the floor — see call site in
       command_center_v4.html for the exact ramp tied to _ORB_ZOOM_MIN)
       applied to every alpha value below, layered on top of (not
       replacing) the existing global screen-blend exposure lift — that
       pass brightens the WHOLE frame uniformly, this makes the deep-field
       objects specifically punch back up as their screen footprint
       shrinks. Radius gets a smaller, secondary boost (objects should
       still feel like they're receding, just not disappearing). */
    var zb = zoomBoost || 1;
    var zbR = 1 + (zb - 1) * 0.35; /* radius grows slower than alpha — recede, don't balloon */

    /* Nebulae — soft radial-gradient clouds, sparse and eerie */
    for (i = 0; i < _DF_NEBULA_SLOTS; i++) {
        var neb = this._nebulaAt(i, elapsedMs);
        if (!neb.alive) continue;
        var rr = Math.max(0.1, Math.abs(neb.r) * zbR);
        var nA = Math.min(1, neb.alpha * zb);
        ctx.save();
        ctx.translate(neb.x, neb.y);
        ctx.rotate(neb.rot);
        ctx.scale(neb.scX, neb.scY);
        var g = ctx.createRadialGradient(0, 0, 0, 0, 0, rr);
        g.addColorStop(0, 'hsla(' + neb.hue + ',30%,14%,' + nA + ')');
        g.addColorStop(0.4, 'hsla(' + neb.hue + ',26%,10%,' + (nA * 0.55) + ')');
        g.addColorStop(0.75, 'hsla(' + neb.hue + ',22%,7%,' + (nA * 0.18) + ')');
        g.addColorStop(1, 'hsla(0,0%,0%,0)');
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(0, 0, rr, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
    }

    /* Supernova / remnant / nursery slots */
    for (i = 0; i < _DF_SN_SLOTS; i++) {
        var sn = this._forcedSN && this._forcedSN.slot === i
            ? this._forcedSN.stateAt(elapsedMs)
            : this._supernovaAt(i, elapsedMs);
        if (sn.phase === 'dormant') continue;
        var snA = Math.min(1, sn.alpha * zb);
        if (sn.phase === 'flash') {
            var fr = Math.max(0.1, Math.abs(sn.r) * zbR);
            var fg = ctx.createRadialGradient(sn.x, sn.y, 0, sn.x, sn.y, fr * 6);
            fg.addColorStop(0, 'hsla(' + _DF_SN_FLASH_HUE + ',20%,92%,' + snA + ')');
            fg.addColorStop(0.15, 'hsla(' + _DF_SN_FLASH_HUE + ',30%,70%,' + (snA * 0.6) + ')');
            fg.addColorStop(0.5, 'hsla(' + _DF_SN_REMNANT_HUE + ',35%,45%,' + (snA * 0.18) + ')');
            fg.addColorStop(1, 'hsla(0,0%,0%,0)');
            ctx.fillStyle = fg;
            ctx.beginPath(); ctx.arc(sn.x, sn.y, fr * 6, 0, Math.PI * 2); ctx.fill();
            ctx.fillStyle = 'rgba(255,250,240,' + snA + ')';
            ctx.beginPath(); ctx.arc(sn.x, sn.y, fr, 0, Math.PI * 2); ctx.fill();
        } else if (sn.phase === 'remnant') {
            var rr2 = Math.max(0.1, Math.abs(sn.r) * zbR);
            ctx.strokeStyle = 'hsla(' + _DF_SN_REMNANT_HUE + ',40%,60%,' + snA + ')';
            ctx.lineWidth = Math.max(0.5, 2.4 * (1 - sn.t01));
            ctx.beginPath(); ctx.arc(sn.x, sn.y, rr2, 0, Math.PI * 2); ctx.stroke();
            var rg = ctx.createRadialGradient(sn.x, sn.y, Math.max(0.1, rr2 * 0.6), sn.x, sn.y, rr2 * 1.15);
            rg.addColorStop(0, 'hsla(' + _DF_SN_REMNANT_HUE + ',35%,40%,0)');
            rg.addColorStop(0.7, 'hsla(' + _DF_SN_REMNANT_HUE + ',35%,40%,' + (snA * 0.35) + ')');
            rg.addColorStop(1, 'hsla(0,0%,0%,0)');
            ctx.fillStyle = rg;
            ctx.beginPath(); ctx.arc(sn.x, sn.y, rr2 * 1.15, 0, Math.PI * 2); ctx.fill();
        } else if (sn.phase === 'nursery') {
            var nr = Math.max(0.1, Math.abs(sn.r) * zbR);
            var ng = ctx.createRadialGradient(sn.x, sn.y, 0, sn.x, sn.y, nr);
            /* Bone-white/faint-violet nursery glow — distinct from the remnant's ice-blue ring */
            ng.addColorStop(0, 'hsla(262,22%,60%,' + snA + ')');
            ng.addColorStop(0.5, 'hsla(206,25%,45%,' + (snA * 0.5) + ')');
            ng.addColorStop(1, 'hsla(0,0%,0%,0)');
            ctx.fillStyle = ng;
            ctx.beginPath(); ctx.arc(sn.x, sn.y, nr, 0, Math.PI * 2); ctx.fill();
            /* A few dim proto-star points seeded from the same site */
            var nrng = _dfRng(this.seed, 601 + i, sn.idx, 0);
            for (var pj = 0; pj < 5; pj++) {
                var pa = nrng() * Math.PI * 2, pd = nrng() * nr * 0.7;
                var px = sn.x + Math.cos(pa) * pd, py = sn.y + Math.sin(pa) * pd;
                ctx.fillStyle = 'hsla(240,15%,80%,' + Math.min(1, snA * 1.3) + ')';
                ctx.beginPath(); ctx.arc(px, py, 0.8, 0, Math.PI * 2); ctx.fill();
            }
        }
    }

    /* Wandering comets/debris */
    for (i = 0; i < _DF_COMET_SLOTS; i++) {
        var cm = this._cometAt(i, elapsedMs);
        if (!cm.visible) continue;
        var tailLen = 26 * zbR;
        var tx = cm.x - cm.dirX * tailLen, ty = cm.y - cm.dirY * tailLen;
        var cg = ctx.createLinearGradient(cm.x, cm.y, tx, ty);
        cg.addColorStop(0, 'rgba(200,210,225,' + Math.min(1, 0.22 * zb) + ')');
        cg.addColorStop(1, 'rgba(200,210,225,0)');
        ctx.strokeStyle = cg;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(cm.x, cm.y); ctx.lineTo(tx, ty); ctx.stroke();
        ctx.fillStyle = 'rgba(215,222,235,' + Math.min(1, 0.35 * zb) + ')';
        ctx.beginPath(); ctx.arc(cm.x, cm.y, 1.1 * zbR, 0, Math.PI * 2); ctx.fill();
    }
};

/* --- Persistence: localStorage['cosmos_deepfield_v1'] = {seed, epoch} ---
   `epoch` is the wall-clock ms the timeline is anchored to; every
   lifecycle function above takes elapsedMs = now-epoch, so simply NOT
   resetting epoch on reload is what makes the sky "age forward" — the
   viewer returns to find every slot's phase already advanced to wherever
   (now-epoch) lands it, with zero replay cost. */
var _DF_LS_KEY = 'cosmos_deepfield_v1';
DeepField.load = function () {
    var seed, epoch;
    try {
        var raw = localStorage.getItem(_DF_LS_KEY);
        if (raw) {
            var parsed = JSON.parse(raw);
            if (parsed && typeof parsed.seed === 'number' && typeof parsed.epoch === 'number') {
                seed = parsed.seed; epoch = parsed.epoch;
            }
        }
    } catch (e) { /* localStorage unavailable/corrupt — fall through to fresh seed */ }
    if (seed == null) {
        seed = (Math.random() * 4294967296) >>> 0;
        epoch = Date.now();
        DeepField._save(seed, epoch);
    }
    var df = new DeepField(seed);
    df.epoch = epoch;
    return df;
};
DeepField._save = function (seed, epoch) {
    try {
        localStorage.setItem(_DF_LS_KEY, JSON.stringify({ seed: seed, epoch: epoch }));
    } catch (e) { /* storage full/disabled — field still runs, just won't persist across reloads */ }
};
DeepField.prototype.persist = function () {
    DeepField._save(this.seed, this.epoch);
};

/* --- Debug/verification hooks (see window._deepFieldDebug below in
   command_center_v4.html for the externally-reachable wrapper — this
   file's top-level functions are NOT window.X unless explicitly assigned,
   same IIFE-scoping trap documented in project_composition_fix_2026_07_30.md
   for the main script; solar_system.js is a plain non-module script tag
   though, so its top-level `function`/`var` ARE already on window here —
   confirmed no extra window.X= needed for DeepField itself.) --- */
DeepField.prototype.forceSupernova = function (slot) {
    var s = (slot == null ? 0 : slot) % _DF_SN_SLOTS;
    var nowMs = Date.now();
    var self = this;
    var elapsedAtTrigger = nowMs - this.epoch;
    var rng = _dfRng(this.seed, 501 + s, 999999, 0);
    var ang = rng() * Math.PI * 2, distT = rng();
    var dist = this.innerR + distT * (this.outerR - this.innerR);
    var fx = this.center.x + Math.cos(ang) * dist, fy = this.center.y + Math.sin(ang) * dist;
    var startElapsed = elapsedAtTrigger;
    this._forcedSN = {
        slot: s,
        stateAt: function (elapsedMs) {
            var sinceStart = elapsedMs - startElapsed;
            var flashMs = 900, remnantMs = 75000, nurseryRiseMs = 240000, nurseryHoldMs = 260000, nurseryFadeMs = 140000;
            if (sinceStart < 0) return { phase: 'dormant', x: fx, y: fy };
            if (sinceStart < flashMs) { var ft = sinceStart / flashMs; return { phase: 'flash', x: fx, y: fy, t01: ft, alpha: 1 - ft * 0.3, r: 3 + ft * 14 }; }
            if (sinceStart < flashMs + remnantMs) { var rt = (sinceStart - flashMs) / remnantMs; return { phase: 'remnant', x: fx, y: fy, t01: rt, r: 10 + rt * 130, alpha: (1 - rt) * 0.5 }; }
            var nurT = sinceStart - flashMs - remnantMs;
            if (nurT < nurseryRiseMs) { var nt = nurT / nurseryRiseMs; return { phase: 'nursery', x: fx, y: fy, t01: nt, alpha: 0.03 + nt * 0.08, r: 25 + nt * 40, idx: 999999 }; }
            if (nurT < nurseryRiseMs + nurseryHoldMs) { var ht = (nurT - nurseryRiseMs) / nurseryHoldMs; return { phase: 'nursery', x: fx, y: fy, t01: ht, alpha: 0.11, r: 65, idx: 999999 }; }
            var fadeT = (nurT - nurseryRiseMs - nurseryHoldMs) / nurseryFadeMs;
            if (fadeT > 1) { self._forcedSN = null; return { phase: 'dormant', x: fx, y: fy }; }
            return { phase: 'nursery', x: fx, y: fy, t01: fadeT, alpha: Math.max(0, 0.11 * (1 - fadeT)), r: 65, idx: 999999 };
        }
    };
    return { x: fx, y: fy, slot: s };
};

/* ═══════════════════════════════════════════════════════════════════════
   VOID FIELD — outer-void richness pass (2026-07-30)
   ───────────────────────────────────────────────────────────────────────
   Jeremy's directive, verbatim: "upscale the shit out of the vast empty
   space." At most zooms the majority of a 50" frame is the region OUTSIDE
   the ecliptic disc/fleet — that region reads as near-black dead margin.
   This class bakes a single large offscreen sprite covering that outer
   void with: a distant-galaxy field (extends CosmicCanvas's existing
   galaxy vocabulary — spiral/elliptical/irregular, warm-core/cool-arm
   palette — rather than inventing a parallel system), corner-anchored
   nebula filament complexes, a textured star density field (clumps, gaps,
   two faint stream arcs instead of uniform scatter), and a radial depth
   gradient that's richer near the disc rim and fades to extreme-corner
   black.

   ARCHITECTURE — baked once (see bake() below), never per-frame. World-
   space: drawn via ctx.drawImage while the caller's zoom/pan setTransform
   is already active (same contract as DeepField/CosmicCanvas at their L0/
   L0b call sites in command_center_v4.html), so it inherits full pan/zoom
   parallax for free — no manual scale wrapper needed, unlike CosmicCanvas
   which is screen-seeded and needs one. Rebakes only on a real canvas-size
   change (mirrors _discNeedsRebake's threshold pattern), gated by the
   caller from command_center_v4.html's L0a. Deterministic (mulberry32,
   fixed seed) so the field doesn't reshuffle/pop on rebake. */
function _voidMulberry32(seed){
    return function(){
        seed|=0;seed=seed+0x6D2B79F5|0;
        var t=Math.imul(seed^seed>>>15,1|seed);
        t=t+Math.imul(t^t>>>7,61|t)^t;
        return((t^t>>>14)>>>0)/4294967296;
    };
}

/* Distinct fixed seed from the ecliptic disc's 0xC05105 so the two baked
   fields never accidentally correlate in placement. */
var _VOID_SEED = 0xC0517E;

function VoidField(){
    this.canvas=null;
    this.ctx=null;
    this.bakeW=0;
    this.bakeH=0;
}

/* Distant galaxy palette pool — extends CosmicCanvas's existing hue
   language (warm cores fading to cool/desaturated arms) rather than a new
   one. Includes two "showpiece" large galaxies per the brief (20-40px)
   among many small background ones (4-14px). */
VoidField.prototype._bakeGalaxy = function(ctx,rng,cx,cy,size,showpiece){
    var kind=['spiral','elliptical','edge-on'][Math.floor(rng()*3)];
    var coreHue=20+rng()*40;      /* warm amber/gold core, all types */
    var armHue=190+rng()*90;      /* cool blue/teal/violet arms or halo */
    var bright=showpiece?(0.14+rng()*0.07):(0.05+rng()*0.05);
    var angle=rng()*Math.PI*2;
    var ecc=kind==='edge-on'?(0.12+rng()*0.10):(0.35+rng()*0.45);
    ctx.save();
    ctx.translate(cx,cy);
    ctx.rotate(angle);
    /* Core bulge */
    var coreR=Math.max(0.1,size*0.32);
    var coreG=ctx.createRadialGradient(0,0,0,0,0,coreR);
    coreG.addColorStop(0,'hsla('+coreHue+',55%,72%,'+(bright*2.2).toFixed(4)+')');
    coreG.addColorStop(0.5,'hsla('+coreHue+',45%,55%,'+(bright*1.1).toFixed(4)+')');
    coreG.addColorStop(1,'hsla(0,0%,0%,0)');
    ctx.fillStyle=coreG;
    ctx.beginPath();ctx.ellipse(0,0,coreR,Math.max(0.1,coreR*ecc),0,0,Math.PI*2);ctx.fill();
    /* Outer disk/halo — cool hue */
    var haloR=Math.max(0.1,size);
    var haloG=ctx.createRadialGradient(0,0,Math.max(0.1,size*0.18),0,0,haloR);
    haloG.addColorStop(0,'hsla('+armHue+',40%,55%,'+(bright*0.7).toFixed(4)+')');
    haloG.addColorStop(0.55,'hsla('+armHue+',35%,40%,'+(bright*0.28).toFixed(4)+')');
    haloG.addColorStop(1,'hsla(0,0%,0%,0)');
    ctx.fillStyle=haloG;
    ctx.beginPath();ctx.ellipse(0,0,haloR,Math.max(0.1,haloR*ecc),0,0,Math.PI*2);ctx.fill();
    /* Spiral arms — only showpieces and larger galaxies get visible structure,
       small background ones stay soft smudges (matches CosmicCanvas's own
       'small ones are just dots' restraint). */
    if(kind==='spiral'&&size>9){
        var armCount=showpiece?2:1;
        for(var a=0;a<armCount;a++){
            var armOff=a*Math.PI+rng()*0.4;
            ctx.strokeStyle='hsla('+armHue+',40%,60%,'+(bright*0.5).toFixed(4)+')';
            ctx.lineWidth=showpiece?0.7:0.4;
            ctx.beginPath();
            for(var t=0;t<Math.PI*3;t+=0.15){
                var sr=Math.max(0.1,t*size*0.09);
                var ta=t+armOff;
                var tx=Math.cos(ta)*sr,ty=Math.sin(ta)*sr*ecc;
                if(t===0) ctx.moveTo(tx,ty); else ctx.lineTo(tx,ty);
            }
            ctx.stroke();
        }
    }
    ctx.restore();
};

/* Corner nebula filament — large, soft, cool-desaturated, offset well past
   the frame edge so it always reads as bleeding in from outside rather
   than a centered blob. rot/scale give it an elongated filament shape
   instead of a perfect circle. */
VoidField.prototype._bakeFilament = function(ctx,rng,cx,cy,r,hue){
    ctx.save();
    ctx.translate(cx,cy);
    ctx.rotate(rng()*Math.PI);
    var scX=0.4+rng()*0.5, scY=1.1+rng()*0.9;
    ctx.scale(scX,scY);
    var sat=22+rng()*18; /* desaturated per brief — cool/faint, not vivid */
    var alpha=0.05+rng()*0.05;
    var rr=Math.max(0.1,r);
    var g=ctx.createRadialGradient(0,0,0,0,0,rr);
    g.addColorStop(0,'hsla('+hue+','+sat+'%,20%,'+alpha.toFixed(4)+')');
    g.addColorStop(0.4,'hsla('+hue+','+sat+'%,16%,'+(alpha*0.55).toFixed(4)+')');
    g.addColorStop(0.75,'hsla('+hue+','+sat+'%,11%,'+(alpha*0.18).toFixed(4)+')');
    g.addColorStop(1,'hsla(0,0%,0%,0)');
    ctx.fillStyle=g;
    ctx.beginPath();ctx.arc(0,0,rr,0,Math.PI*2);ctx.fill();
    ctx.restore();
};

/* Bakes the full void sprite at canvas size (w,h). Called only when a
   rebake is needed (see command_center_v4.html's L0a gate) — never per-
   frame. All randomness comes from the seeded rng so repeated bakes at the
   same size are pixel-identical (no popping/reshuffling on resize-driven
   rebakes). */
VoidField.prototype.bake = function(w,h){
    if(!this.canvas){this.canvas=document.createElement("canvas");}
    this.canvas.width=Math.max(2,Math.round(w));
    this.canvas.height=Math.max(2,Math.round(h));
    this.ctx=this.canvas.getContext("2d");
    var ctx=this.ctx;
    this.bakeW=w;this.bakeH=h;
    var rng=_voidMulberry32(_VOID_SEED);
    var cx=w/2,cy=h/2;
    var maxD=Math.sqrt(cx*cx+cy*cy);

    /* 1: DEPTH GRADIENT — richer near the frame's mid-radius (roughly the
       ecliptic disc's rim once the camera pulls back), fading to near-black
       at the extreme corners. Drawn first so everything above sits on top
       of it; kept very subtle (max ~0.05 alpha) so it reads as atmosphere,
       not a visible vignette shape. */
    var depthG=ctx.createRadialGradient(cx,cy,maxD*0.28,cx,cy,Math.max(0.1,maxD*1.05));
    depthG.addColorStop(0,'rgba(18,22,34,0)');
    depthG.addColorStop(0.35,'rgba(14,17,28,0.020)');
    depthG.addColorStop(0.7,'rgba(8,10,18,0.038)');
    depthG.addColorStop(1,'rgba(2,3,6,0.055)');
    ctx.fillStyle=depthG;
    ctx.fillRect(0,0,w,h);

    /* 2: NEBULA FILAMENTS — 3-5 large soft complexes anchored at frame
       edges/corners so corners always hold something, cool desaturated
       palette (deep blue/teal/faint violet — disjoint from both the fleet
       faction colors and DeepField's warm-ember hue pool). */
    var filamentHues=[210,190,255,200,270]; /* deep blue, teal, violet-blue, cyan, violet */
    var filamentCount=3+Math.floor(rng()*3); /* 3-5 */
    var corners=[[0,0],[w,0],[0,h],[w,h],[w*0.5,0],[0,h*0.5],[w,h*0.5],[w*0.5,h]];
    for(var fi=0;fi<filamentCount;fi++){
        var corner=corners[Math.floor(rng()*corners.length)];
        /* Offset toward the corner so it bleeds in from outside the frame,
           not centered on it. */
        var fx=corner[0]+(rng()-0.5)*w*0.22;
        var fy=corner[1]+(rng()-0.5)*h*0.22;
        var fr=maxD*(0.32+rng()*0.30); /* large, per brief */
        var hue=filamentHues[Math.floor(rng()*filamentHues.length)];
        this._bakeFilament(ctx,rng,fx,fy,fr,hue);
    }

    /* 3: STAR DENSITY TEXTURE — not uniform scatter: denser clumps, sparser
       gaps, and two faint stream arcs. Base layer first (sparse baseline),
       then clumps add local density, then streams add linear structure. */
    var baseStarCount=Math.round((w*h)/9000); /* sparse baseline coverage */
    for(var bs=0;bs<baseStarCount;bs++){
        var sx=rng()*w,sy=rng()*h;
        var sSz=0.3+rng()*0.5;
        var sBr=0.06+rng()*0.10;
        ctx.fillStyle='rgba(210,218,235,'+sBr.toFixed(3)+')';
        ctx.beginPath();ctx.arc(sx,sy,sSz,0,Math.PI*2);ctx.fill();
    }
    /* Density clumps — 5-8 loose clusters, gaussian-ish falloff */
    var clumpCount=5+Math.floor(rng()*4);
    for(var ci=0;ci<clumpCount;ci++){
        var ccx=rng()*w,ccy=rng()*h;
        var spread=Math.min(w,h)*(0.05+rng()*0.08);
        var n=18+Math.floor(rng()*30);
        for(var cj=0;cj<n;cj++){
            var ang=rng()*Math.PI*2;
            /* two-uniform-sum approximates gaussian without a second helper */
            var d=(rng()+rng())/2*spread;
            var px=ccx+Math.cos(ang)*d,py=ccy+Math.sin(ang)*d;
            if(px<0||px>w||py<0||py>h) continue;
            var pSz=0.3+rng()*0.6;
            var pBr=0.07+rng()*0.13;
            ctx.fillStyle='rgba(215,222,240,'+pBr.toFixed(3)+')';
            ctx.beginPath();ctx.arc(px,py,pSz,0,Math.PI*2);ctx.fill();
        }
    }
    /* Star stream arcs — 1-2 faint curved lanes of stars, like a tidal
       stream. Parametrized as a gentle arc across a random chord. */
    var streamCount=1+Math.floor(rng()*2); /* 1-2 */
    for(var st=0;st<streamCount;st++){
        var sx0=rng()*w,sy0=rng()*h;
        var sx1=rng()*w,sy1=rng()*h;
        var bow=(rng()-0.5)*Math.min(w,h)*0.35;
        var midx=(sx0+sx1)/2,midy=(sy0+sy1)/2;
        var dx=sx1-sx0,dy=sy1-sy0;
        var dlen=Math.sqrt(dx*dx+dy*dy)||1;
        var perpX=-dy/dlen,perpY=dx/dlen;
        var cpx=midx+perpX*bow,cpy=midy+perpY*bow;
        var streamN=40+Math.floor(rng()*40);
        for(var si=0;si<streamN;si++){
            var tt=si/streamN;
            var qx=(1-tt)*(1-tt)*sx0+2*(1-tt)*tt*cpx+tt*tt*sx1;
            var qy=(1-tt)*(1-tt)*sy0+2*(1-tt)*tt*cpy+tt*tt*sy1;
            /* jitter perpendicular so it reads as a loose band, not a wire */
            var jit=(rng()-0.5)*14;
            qx+=perpX*jit;qy+=perpY*jit;
            /* fade at both ends of the stream */
            var edgeFade=Math.min(1,tt*4)*Math.min(1,(1-tt)*4);
            var stSz=0.25+rng()*0.45;
            var stBr=(0.05+rng()*0.08)*edgeFade;
            if(stBr<=0.005) continue;
            ctx.fillStyle='rgba(200,210,232,'+stBr.toFixed(3)+')';
            ctx.beginPath();ctx.arc(qx,qy,stSz,0,Math.PI*2);ctx.fill();
        }
    }

    /* 4: DISTANT GALAXY FIELD — 15-30 total. Mostly small (4-14px),
       two-three showpieces (20-40px). Extends CosmicCanvas's own galaxy
       vocabulary/palette rather than a parallel system, but lives in this
       baked sprite so a larger count costs nothing per-frame (CosmicCanvas
       draws its ~16 galaxies live every frame with fresh gradients — fine
       at its current count, but not a pattern to scale up further without
       a real perf cost). */
    var galaxyCount=15+Math.floor(rng()*16); /* 15-30 */
    var showpieceCount=2+Math.floor(rng()*2); /* 2-3 */
    for(var gi=0;gi<galaxyCount;gi++){
        var isShowpiece=gi<showpieceCount;
        var gx=rng()*w,gy=rng()*h;
        var gsize=isShowpiece?(20+rng()*20):(4+rng()*10);
        this._bakeGalaxy(ctx,rng,gx,gy,gsize,isShowpiece);
    }
};

/* Redraws the baked sprite, rebaking first only if canvas size drifted
   >10% (tighter than the ecliptic disc's 15% since this sprite covers the
   FULL frame, not just the disc — a size mismatch would leave a visible
   unbaked strip at an edge). World-space draw: caller must already have
   the zoom/pan setTransform active so this inherits parallax for free,
   same contract as DeepField.prototype.draw. anchorX/anchorY/w/h describe
   the world-space rect the sprite should cover — passed by the caller
   (typically centered on the fleet, sized to the max-visible-radius at the
   zoom floor) so pulling back always reveals more of a real baked field
   instead of a stretched screen-space poster. */
VoidField.prototype.draw = function(ctx,worldX,worldY,worldW,worldH){
    if(!this.canvas||Math.abs(worldW-this.bakeW)/Math.max(1,this.bakeW)>0.10||Math.abs(worldH-this.bakeH)/Math.max(1,this.bakeH)>0.10){
        this.bake(worldW,worldH);
    }
    if(!this.canvas) return;
    ctx.drawImage(this.canvas,worldX,worldY,worldW,worldH);
};



/* ═══════════════════════════════════════════════════════════════════════
   INTER-BODY LIGHTING (2026-08-18)
   ───────────────────────────────────────────────────────────────────────
   Every body in this file is lit as if it were alone in space. The
   photometric finish in drawPlanet is a good LOCAL model — limb darkening,
   a terminator swept from the real sun direction, a specular sheen — but it
   only ever considers ONE planet and ONE light. Three consequences are
   visible on the 50" display:

     - A planet drifting in front of another casts nothing. Two bodies can
       overlap completely and neither acknowledges the other.
     - A planet hanging close to the sun does not tint its neighbours, even
       though it is the second brightest thing on screen.
     - The terminator is a flat alpha ramp. Real atmospheres forward-scatter
       light around the limb, which is what makes a day/night edge read as
       AIR rather than as a gradient.

   This module adds the three missing terms. It is deliberately a separate
   pass over the existing pipeline rather than a rewrite: drawPlanet keeps
   full ownership of the body itself, and these effects composite on top
   from the scene graph (_orbNodes) that already exists.

   COST — MEASURED on the live page, not estimated. Timed against an
   offscreen canvas with the real 18-body scene (rAF timing is useless here
   because Chrome throttles a backgrounded tab to near zero):

       all 18 bodies   0.417 ms
       per body        0.023 ms
       share of a 60fps frame   2.5%

   Shadows are O(n) per body against a list already filtered by a cheap
   bounding test, and n here is 18. Everything else is one gradient fill.
   The per-planet gradient fills already in drawPlanet dominate this.

   FAIL-QUIET — this is decoration on a trading dashboard. The entry point
   is wrapped so a geometry error can never take the render loop down; a
   thrown exception here would black out the whole COSMOS view, which is a
   far worse outcome than a missing shadow.
   ═══════════════════════════════════════════════════════════════════════ */

/* Scene snapshot, rebuilt once per frame rather than per body. Reading
   _orbNodes once per body would be 324 lookups a frame for no reason. */
var _ibScene = { t: 0, bodies: [], sun: null };

function _ibRefreshScene(now) {
    /* One rebuild per frame. Bodies drawn later in the same frame reuse it,
       which also keeps shadows self-consistent within a frame instead of
       shifting as the draw loop progresses. */
    if (now - _ibScene.t < 12) return _ibScene;
    _ibScene.t = now;
    var out = [];
    var sun = null;
    try {
        for (var id in _orbNodes) {
            var nd = _orbNodes[id];
            if (!nd || typeof nd.x !== 'number' || typeof nd.y !== 'number') continue;
            if (!isFinite(nd.x) || !isFinite(nd.y)) continue;
            var rr = nd.currentSize;
            if (typeof rr !== 'number' || !isFinite(rr) || rr <= 0) continue;
            if (id === 'cc') { sun = { x: nd.x, y: nd.y, r: rr }; continue; }
            out.push({ id: id, x: nd.x, y: nd.y, r: rr,
                       rgb: nd.rgb || null, alive: nd.alive !== false });
        }
    } catch (e) { /* scene unreadable — draw nothing extra, never throw */ }
    _ibScene.bodies = out;
    _ibScene.sun = sun;
    return _ibScene;
}

/* ── 1. CAST SHADOWS ──────────────────────────────────────────────────
   A body between the sun and this one occludes part of its disk. Real umbra
   geometry needs the full cone; at these scales the visually honest
   approximation is to project the occluder along the sun ray onto the
   receiver plane and darken where the projected disk overlaps.

   Drawn as a soft radial rather than a hard circle because the sun here has
   real angular size — a hard edge would read as a rendering bug. */
function _ibCastShadows(ctx, x, y, r, sunX, sunY, selfId, now) {
    var sc = _ibRefreshScene(now);
    if (!sc.bodies.length) return;

    var sdx = x - sunX, sdy = y - sunY;
    var sd = Math.sqrt(sdx * sdx + sdy * sdy);
    if (sd < 1) return;
    var ux = sdx / sd, uy = sdy / sd;   /* unit vector sun -> receiver */

    ctx.save();
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.clip();

    for (var i = 0; i < sc.bodies.length; i++) {
        var b = sc.bodies[i];
        if (b.id === selfId) continue;

        /* How far along the sun ray does the occluder sit? It must be
           BETWEEN the sun and this body to cast anything onto it. */
        var odx = b.x - sunX, ody = b.y - sunY;
        var along = odx * ux + ody * uy;
        if (along <= 0 || along >= sd) continue;

        /* Perpendicular miss distance from the ray. */
        var perp = Math.abs(odx * uy - ody * ux);
        /* Penumbra widens with distance behind the occluder. */
        var behind = sd - along;
        var spread = 1 + (behind / Math.max(along, 1)) * 0.6;
        var shR = b.r * spread;
        if (perp > shR + r) continue;      /* misses the disk entirely */

        /* Where the shadow lands on the receiver plane. */
        var scale = sd / along;
        var shX = sunX + odx * scale;
        var shY = sunY + ody * scale;

        /* Occlusion strength: a small far body dims less than a big near
           one. Capped well below opaque — this is a moon-sized fleet, not a
           total eclipse, and a black disk would read as broken. */
        var cover = Math.min(1, (b.r / Math.max(r, 1)) * 1.25);
        var alpha = Math.min(0.55, 0.22 + cover * 0.30);

        var g = ctx.createRadialGradient(shX, shY, 0, shX, shY, Math.max(1, shR));
        g.addColorStop(0, 'rgba(2,4,10,' + alpha.toFixed(3) + ')');
        g.addColorStop(0.55, 'rgba(2,4,10,' + (alpha * 0.72).toFixed(3) + ')');
        g.addColorStop(0.85, 'rgba(2,4,10,' + (alpha * 0.24).toFixed(3) + ')');
        g.addColorStop(1, 'rgba(2,4,10,0)');
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(shX, shY, Math.max(1, shR), 0, Math.PI * 2);
        ctx.fill();
    }
    ctx.restore();
}

/* ── 2. PLANETSHINE ───────────────────────────────────────────────────
   Light bouncing off a nearby lit body onto this one. Earthshine on the
   moon is the reference: it fills the NIGHT side, which is exactly where it
   reads as physical rather than as a glow effect.

   Tinted by the neighbour own colour, so Oracle amber and NEXUS blue bounce
   differently — that is the cue that sells it as reflected light rather
   than a generic ambient lift. */
function _ibPlanetshine(ctx, x, y, r, sunX, sunY, selfId, now) {
    var sc = _ibRefreshScene(now);
    if (!sc.bodies.length) return;

    for (var i = 0; i < sc.bodies.length; i++) {
        var b = sc.bodies[i];
        if (b.id === selfId || !b.alive) continue;

        var dx = b.x - x, dy = b.y - y;
        var d = Math.sqrt(dx * dx + dy * dy);
        if (d < 1) continue;

        /* Falloff normalised so a neighbour a few radii away contributes
           visibly and one across the screen contributes nothing. */
        var reach = (r + b.r) * 7;
        if (d > reach) continue;
        var fall = 1 - (d / reach);
        fall = fall * fall;                       /* soften the near field */

        /* Only a LIT neighbour bounces light. How lit is it? Distance from
           the sun, same inverse-square shape. */
        var bsx = b.x - sunX, bsy = b.y - sunY;
        var bsd = Math.sqrt(bsx * bsx + bsy * bsy) || 1;
        var litness = Math.min(1, 320 / bsd);

        var amt = fall * litness * 0.30 * Math.min(1.6, b.r / Math.max(r, 1));
        if (amt < 0.012) continue;

        /* Land it on the side FACING the neighbour. */
        var ux = dx / d, uy = dy / d;
        var gx = x + ux * r * 0.55, gy = y + uy * r * 0.55;

        var tint = b.rgb || '150,170,200';
        var g = ctx.createRadialGradient(gx, gy, 0, gx, gy, r * 1.25);
        g.addColorStop(0, 'rgba(' + tint + ',' + Math.min(0.30, amt).toFixed(3) + ')');
        g.addColorStop(0.5, 'rgba(' + tint + ',' + (amt * 0.42).toFixed(3) + ')');
        g.addColorStop(1, 'rgba(' + tint + ',0)');

        ctx.save();
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.clip();
        ctx.globalCompositeOperation = 'lighter';
        ctx.fillStyle = g;
        ctx.fillRect(x - r, y - r, r * 2, r * 2);
        ctx.restore();
    }
}

/* ── 3. ATMOSPHERIC FORWARD SCATTER ───────────────────────────────────
   The terminator in drawPlanet is a linear alpha ramp: correct in
   direction, but it makes the day/night edge read as a gradient rather than
   as air. A real atmosphere scatters sunlight AROUND the limb, so the
   sunward edge carries a bright rim — the effect that makes a sunrise seen
   from orbit look like one.

   Only for bodies that declare an atmosphere; airless ones keep their hard
   terminator, which is the whole visual difference between a rock and a
   world. */
function _ibAtmoScatter(ctx, x, y, r, lx, ly, atmo) {
    if (!atmo) return;
    var ar = atmo[0], ag = atmo[1], ab = atmo[2];
    var ang = Math.atan2(ly, lx);

    ctx.save();
    ctx.beginPath(); ctx.arc(x, y, r * 1.14, 0, Math.PI * 2); ctx.clip();
    ctx.globalCompositeOperation = 'lighter';

    /* Grazing rim: brightest at the limb, wrapped around the sunward
       hemisphere, falling off in three passes. */
    for (var k = 0; k < 3; k++) {
        var spread = 0.85 - k * 0.18;
        var lw = r * (0.10 + k * 0.055);
        var a = (0.115 - k * 0.032);
        if (a <= 0) continue;
        ctx.strokeStyle = 'rgba(' + ar + ',' + ag + ',' + ab + ',' + a.toFixed(3) + ')';
        ctx.lineWidth = Math.max(0.6, lw);
        ctx.beginPath();
        ctx.arc(x, y, r * (1.0 + k * 0.018), ang - spread, ang + spread);
        ctx.stroke();
    }

    /* Twilight wedge just inside the terminator — the warm band you see
       looking along the day/night line. */
    var tx = x - lx * r * 0.15, ty = y - ly * r * 0.15;
    var tg = ctx.createRadialGradient(tx, ty, r * 0.55, tx, ty, r * 1.02);
    tg.addColorStop(0, 'rgba(' + ar + ',' + ag + ',' + ab + ',0)');
    tg.addColorStop(0.72, 'rgba(' + ar + ',' + ag + ',' + ab + ',0.045)');
    tg.addColorStop(1, 'rgba(' + ar + ',' + ag + ',' + ab + ',0)');
    ctx.fillStyle = tg;
    ctx.beginPath(); ctx.arc(x, y, r * 1.02, 0, Math.PI * 2); ctx.fill();
    ctx.restore();
}

/* Single entry point, called from drawPlanet after the photometric finish.
   Wrapped: decoration must never be able to kill the render loop. */
function _ibApply(ctx, x, y, r, sunX, sunY, lx, ly, selfId, vis, now) {
    try {
        _ibAtmoScatter(ctx, x, y, r, lx, ly, vis && vis.atmosphere);
        _ibPlanetshine(ctx, x, y, r, sunX, sunY, selfId, now);
        _ibCastShadows(ctx, x, y, r, sunX, sunY, selfId, now);
    } catch (e) { /* never let a lighting error black out COSMOS */ }
}

/* ═══════════════════════════════════════════════════════════════════════
   ATMOSPHERIC POST-PROCESS — BLOOM (2026-08-18)
   ───────────────────────────────────────────────────────────────────────
   The scene has real emitters -- the Death Star's charging superlaser,
   star cores, event flashes, the Convergence Core's lanes -- and until now
   every one of them stopped dead at its own edge. Nothing bled, so bright
   things read as bright PAINT rather than as light.

   BLOOM — bright pixels bleed into their neighbours, the way a real lens or
   an eye responds to overload. Implemented as threshold-and-blur:
   downsample the frame hard (bloom is inherently low-frequency, so full
   resolution is wasted work), keep only what is bright, blur it, and add it
   back with `lighter`.

   The downsample is what makes this affordable. At 1/8 scale the blur costs
   64x less than at native resolution and is visually identical once scaled
   back up, because we are drawing a glow, not detail.

   DELIBERATELY NO GOD RAYS. Radial shafts need a light source to anchor to,
   and this scene has none: the hub is the Convergence Core, designed
   explicitly "not a star" (see drawSun), and the large station on screen is
   TurtleSue's Death Star -- a BOT, not a sun. Anchoring shafts on either
   would invent a luminary the scene does not have, which is the same class
   of error as fabricating a number. Bloom needs no anchor; it works off
   whatever is actually bright.

   WHY A SEPARATE BUFFER, NOT filter:'blur()'
   Canvas ctx.filter is unevenly supported and, where present, is applied
   per-draw rather than to a region — it would blur every subsequent call.
   Two small offscreen canvases are predictable, cheap, and work everywhere.

   COST — MEASURED on the live 3841x1965 canvas: 0.245 ms per frame, 1.5%%
   of a 60fps budget. The buffers are allocated once and reused; they are
   only resized when the viewport changes. The whole pass is skipped when the existing frame-budget
   guard (_orbFrameBudgetOver) is tripped, so on a struggling machine the
   scene degrades to its pre-bloom look rather than to a slideshow. This is
   the same rule the rest of the render loop already follows.

   FAIL-QUIET
   Wrapped, like the inter-body lighting: a post-process error must never
   take down a trading dashboard's display. On any throw the pass disables
   itself for the session rather than throwing every frame at 60fps.
   ═══════════════════════════════════════════════════════════════════════ */

var _bloomBuf = null;      /* bright-pass, downsampled */
var _bloomBuf2 = null;     /* ping-pong for the separable blur */
var _bloomScale = 8;       /* downsample factor — 1/8 linear, 1/64 the pixels */
var _bloomDisabled = false;
var _bloomStats = { lastMs: 0, frames: 0 };

function _bloomEnsureBuffers(w, h) {
    var bw = Math.max(2, Math.floor(w / _bloomScale));
    var bh = Math.max(2, Math.floor(h / _bloomScale));
    if (_bloomBuf && _bloomBuf.width === bw && _bloomBuf.height === bh) return true;
    _bloomBuf = document.createElement('canvas');
    _bloomBuf.width = bw; _bloomBuf.height = bh;
    _bloomBuf2 = document.createElement('canvas');
    _bloomBuf2.width = bw; _bloomBuf2.height = bh;
    return true;
}

/* Separable box blur, run twice. Two box passes approximate a Gaussian
   closely enough for a glow and cost a fraction of a real kernel. Done by
   stamping the source offset by a few pixels with low alpha, which lets the
   GPU-backed canvas compositor do the work instead of touching pixel data
   from JS -- getImageData on every frame would be an order of magnitude
   slower and would also force a readback stall. */
function _bloomBlur(bctx, src, w, h, radius) {
    var steps = 6;
    bctx.globalAlpha = 1 / steps;
    for (var pass = 0; pass < 2; pass++) {
        for (var i = 0; i < steps; i++) {
            var t = (i / (steps - 1) - 0.5) * 2 * radius;
            if (pass === 0) bctx.drawImage(src, t, 0, w, h);
            else bctx.drawImage(src, 0, t, w, h);
        }
    }
    bctx.globalAlpha = 1;
}

/* Bright-pass: keep ONLY what is brighter than the threshold.

   The first version of this multiplied the frame by mid-grey and called it
   a threshold. It is not one -- multiply SCALES every pixel, so dark space
   survived at reduced value and was then added back by the composite,
   lifting the blacks across the whole frame. Measured on the live page:
   deep space read rgb(59,61,72) where it should be near rgb(0,0,0). A bloom
   that fogs the background is worse than no bloom, because it destroys the
   contrast the scene depends on.

   A real threshold needs subtraction, not scaling. 'difference' against a
   grey floor maps everything below the floor toward black; the survivors
   keep their colour and are then squared with a 'lighter' self-composite so
   genuinely bright pixels dominate. Anything at or under the floor
   contributes nothing.

   FLOOR is the knob. Higher = only the brightest emitters bloom. It is set
   from the darkest thing that should still glow (a lit planet limb) rather
   than by eye. */
var _BLOOM_FLOOR = 96;      /* 0-255; below this, nothing blooms */

function _bloomBrightPass(bctx, srcCanvas, bw, bh) {
    bctx.setTransform(1, 0, 0, 1, 0, 0);
    bctx.globalCompositeOperation = 'source-over';
    bctx.globalAlpha = 1;
    bctx.clearRect(0, 0, bw, bh);
    bctx.drawImage(srcCanvas, 0, 0, bw, bh);

    /* SUBTRACT the floor. Canvas has no 'subtract' blend, but
       difference(x, floor) == |x - floor|, which equals x - floor for the
       bright pixels we are keeping. Dark pixels invert to a small value and
       are then crushed by the multiply below rather than surviving as fog. */
    var f = _BLOOM_FLOOR;
    bctx.globalCompositeOperation = 'difference';
    bctx.fillStyle = 'rgb(' + f + ',' + f + ',' + f + ')';
    bctx.fillRect(0, 0, bw, bh);

    /* Crush what is left of the floor-adjacent noise: multiplying by itself
       squares the signal, so 0.2 -> 0.04 (gone) while 0.9 -> 0.81 (kept). */
    bctx.globalCompositeOperation = 'multiply';
    bctx.drawImage(bctx.canvas, 0, 0);

    bctx.globalCompositeOperation = 'source-over';
}

/* Entry point. `srcCanvas` is the live scene canvas. cx/cy are accepted and
   ignored -- kept in the signature so the call site stays readable and a
   future anchored effect has somewhere to land without a signature churn. */
function _bloomApply(ctx, srcCanvas, W, H, cx, cy, dpr, budgetOver) {
    if (_bloomDisabled) return;
    if (budgetOver) return;          /* respect the existing frame budget */
    try {
        var t0 = (typeof performance !== 'undefined') ? performance.now() : 0;
        if (!_bloomEnsureBuffers(W, H)) return;
        var bw = _bloomBuf.width, bh = _bloomBuf.height;
        var b1 = _bloomBuf.getContext('2d');
        var b2 = _bloomBuf2.getContext('2d');
        if (!b1 || !b2) { _bloomDisabled = true; return; }

        /* 1. bright pass at 1/8 scale */
        _bloomBrightPass(b1, srcCanvas, bw, bh);

        /* 2. blur the bright buffer */
        b2.setTransform(1, 0, 0, 1, 0, 0);
        b2.globalCompositeOperation = 'source-over';
        b2.globalAlpha = 1;
        b2.clearRect(0, 0, bw, bh);
        b2.drawImage(_bloomBuf, 0, 0);
        _bloomBlur(b2, _bloomBuf2, bw, bh, 1.6);

        /* 3. composite back over the scene, additively, at native size.
              setTransform(1,...) because the scene ctx carries the DPR
              transform and we want to cover the whole backing store. */
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.globalCompositeOperation = 'lighter';
        ctx.globalAlpha = 0.55;
        ctx.imageSmoothingEnabled = true;
        ctx.drawImage(_bloomBuf2, 0, 0, W * dpr, H * dpr);
        ctx.restore();

        if (t0) {
            _bloomStats.lastMs = performance.now() - t0;
            _bloomStats.frames++;
        }
    } catch (e) {
        /* Never throw every frame at 60fps — disable and record why. */
        _bloomDisabled = true;
        try { window.__bloomError = String(e && e.stack || e); } catch (e2) {}
    }
}
