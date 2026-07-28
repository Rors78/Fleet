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
  /* THREE STARS — intelligence giants ×1.5, orbital radii ×1.3 */
  oracle:    {type:"star",  parent:"cc",      orbitRadius:585, orbitSpeed:0.000085,mass:40,  sz:57, gravitationalRadius:180, grp:"intel",  pt:"gas_giant",  pers:"deliberate"},
  deepblue:  {type:"star",  parent:"cc",      orbitRadius:520, orbitSpeed:0.000115,mass:35,  sz:54, gravitationalRadius:160, grp:"intel",  pt:"ocean",      pers:"predatory"},
  nexus:     {type:"star",  parent:"cc",      orbitRadius:468, orbitSpeed:0.00007, mass:38,  sz:56, gravitationalRadius:170, grp:"novel",  pt:"binary",     pers:"omniscient"},
  /* PLANETS — Trading bots ×1.3, orbital radii ×1.3. orbitSpeed = Kepler(orbitRadius). */
  confluence:{type:"planet",defaultParent:"oracle",   orbitRadius:130,orbitSpeed:0.0008114,mass:15, sz:29, grp:"trader", pt:"terrestrial", pers:"aggressive"},
  nexusbrain:{type:"planet",defaultParent:"nexus",    orbitRadius:117,orbitSpeed:0.0009503,mass:12, sz:27, grp:"trader", pt:"terrestrial", pers:"analytical"},
  gridzilla: {type:"planet",defaultParent:"oracle",   orbitRadius:111,orbitSpeed:0.0010284,mass:10, sz:26, grp:"trader", pt:"crystal",     pers:"steady"},
  turtlesue: {type:"planet",defaultParent:"oracle",   orbitRadius:156,orbitSpeed:0.0006173,mass:12, sz:27, grp:"trader", pt:"terrestrial", pers:"patient"},
  rubberband:{type:"planet",defaultParent:"nexus",    orbitRadius:117,orbitSpeed:0.0009503,mass:10, sz:25, grp:"trader", pt:"elastic",     pers:"bouncy"},
  arbitrageur:{type:"planet",defaultParent:"deepblue",orbitRadius:124,orbitSpeed:0.0008710,mass:10, sz:25, grp:"trader", pt:"binary_pair", pers:"paired"},
  /* MOONS — Small support bots ×1.2, orbital radii ×1.3. orbitSpeed = Kepler(orbitRadius). */
  aegis:     {type:"moon",  parent:"cc",              orbitRadius:117,orbitSpeed:0.0009503, mass:14, sz:36, grp:"novel",    pt:"magnetar",   pers:"guardian"},
  phitex:    {type:"moon",  parent:"nexus",           orbitRadius:111,orbitSpeed:0.0010284, mass:12, sz:16, grp:"novel",    pt:"variable",   pers:"pulsing"},
  sentinel:  {type:"moon",  parent:"cc",              orbitRadius:156,orbitSpeed:0.0006173, mass:10, sz:14, grp:"intel",    pt:"nebula",     pers:"watchful"},
  contrarian:{type:"moon",  parent:"deepblue",        orbitRadius:104,orbitSpeed:0.0011340, mass:10, sz:14, grp:"intel",    pt:"dark_nebula",pers:"contrarian",ecc:0.22},
  chronos:   {type:"moon",  parent:"cc",              orbitRadius:182,orbitSpeed:0.0004898, mass:10, sz:14, grp:"intel",    pt:"pulsar",     pers:"rhythmic"},
  hivemind:  {type:"moon",  parent:"cc",              orbitRadius:98, orbitSpeed:0.0012397, mass:8,  sz:13, grp:"optimizer",pt:"cluster",    pers:"swarm"},
  trinity:   {type:"moon",  parent:"oracle",          orbitRadius:104,orbitSpeed:0.0011340, mass:8,  sz:13, grp:"intel",    pt:"trinary",    pers:"scattered"},
  inference: {type:"moon",  parent:"cc",              orbitRadius:130,orbitSpeed:0.0008114, mass:6,  sz:11, grp:"support",  pt:"nebula",     pers:"processing"}
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
            for(var bi=0;bi<bands.length;bi++){
              var b=bands[bi];
              /* Each band drifts at slightly different speed — differential rotation */
              var drift=(bi%2===0?1:-1)*Math.sin(now/18000+bi*0.9)*r*0.04
                       +(bi<8?1:-1)*Math.sin(now/11000+bi*1.7)*r*0.02;
              var bG=ctx.createLinearGradient(x-r,y+b.y*r,x+r,y+(b.y+b.h)*r);
              bG.addColorStop(0,'rgba('+b.c[0]+','+b.c[1]+','+b.c[2]+','+(b.a*0.45)+')');
              bG.addColorStop(0.15,'rgba('+b.c[0]+','+b.c[1]+','+b.c[2]+','+b.a+')');
              bG.addColorStop(0.85,'rgba('+b.c[0]+','+b.c[1]+','+b.c[2]+','+b.a+')');
              bG.addColorStop(1,'rgba('+b.c[0]+','+b.c[1]+','+b.c[2]+','+(b.a*0.45)+')');
              ctx.save();
              ctx.translate(drift,0);
              ctx.fillStyle=bG;
              ctx.fillRect(x-r-Math.abs(drift)-2,y+b.y*r,r*2+Math.abs(drift)*2+4,b.h*r+1);
              ctx.restore();
            }
            /* Inter-band turbulence: festoon waves at belt/zone boundaries */
            ctx.lineWidth=0.7;
            var turbBands=[-0.43,-0.29,0.09,0.25];
            for(var ti=0;ti<turbBands.length;ti++){
              var ty=y+turbBands[ti]*r;
              ctx.strokeStyle='rgba(200,165,95,0.09)';
              ctx.beginPath();
              for(var tx=x-r;tx<=x+r;tx+=2){
                var tw=Math.sin((tx-x)/r*8+now/5000+ti*2.1)*r*0.025
                      +Math.sin((tx-x)/r*14+now/3200+ti)*r*0.012;
                if(tx===x-r) ctx.moveTo(tx,ty+tw); else ctx.lineTo(tx,ty+tw);
              }
              ctx.stroke();
              /* Festoon wisps: short curved filaments dipping south */
              for(var fi=0;fi<5;fi++){
                var fx=x-r*0.7+fi*r*0.35+Math.sin(now/8000+fi+ti)*r*0.05;
                var festA=now/12000+fi*0.8+ti;
                ctx.strokeStyle='rgba(175,130,65,0.08)';
                ctx.lineWidth=0.5;
                ctx.beginPath();
                ctx.moveTo(fx,ty);
                ctx.bezierCurveTo(fx+r*0.04,ty+r*0.05,fx+r*0.07,ty+r*0.06,fx+r*0.08,ty+r*0.04);
                ctx.stroke();
              }
            }
            /* Great Red Spot — oval storm, 1.5x planet-width, south equatorial belt */
            var grsPhase=now/18000; /* slow westward drift */
            var spotX=x+Math.cos(grsPhase)*r*0.28;
            var spotY=y+r*0.17;
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
            /* White oval storms — smaller BTB ovals in south temperate belt */
            var baSeed=[[0.3,0.40],[0.68,0.43],[-0.35,0.38]];
            for(var ba=0;ba<baSeed.length;ba++){
              var baX=x+baSeed[ba][0]*r+Math.sin(now/25000+ba)*r*0.03;
              var baY=y+baSeed[ba][1]*r;
              var baG=ctx.createRadialGradient(baX,baY,0,baX,baY,r*0.07);
              baG.addColorStop(0,'rgba(240,232,210,0.22)');
              baG.addColorStop(0.6,'rgba(220,210,185,0.08)');
              baG.addColorStop(1,'rgba(0,0,0,0)');
              ctx.fillStyle=baG;
              ctx.beginPath();ctx.ellipse(baX,baY,r*0.07,r*0.045,0,0,Math.PI*2);ctx.fill();
            }
            /* Scanner beam — oracle's 93-pair scanning pulse */
            var scA=now/7000;
            var scLen=r*1.3;
            var scG=ctx.createLinearGradient(x,y,x+Math.cos(scA)*scLen,y+Math.sin(scA)*scLen);
            scG.addColorStop(0,'rgba(228,195,110,0.05)');
            scG.addColorStop(0.7,'rgba(228,195,110,0.02)');
            scG.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=scG;ctx.beginPath();ctx.moveTo(x,y);
            ctx.arc(x,y,scLen,scA-0.06,scA+0.06);ctx.closePath();ctx.fill();
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
            for(var bi=0;bi<dbBands.length;bi++){
              var b=dbBands[bi];
              /* Neptune has fastest winds in solar system — strong differential rotation */
              var drift=(bi%2===0?1.2:-0.8)*Math.sin(now/10000+bi*0.7)*r*0.06;
              ctx.fillStyle='rgba('+b.c[0]+','+b.c[1]+','+b.c[2]+','+b.a+')';
              ctx.fillRect(x-r+drift,y+b.y*r,r*2,b.h*r);
            }
            /* Bright cloud streaks — methane ice high-altitude cirrus */
            for(var ci=0;ci<6;ci++){
              var cy2=y+(-0.6+ci*0.22)*r;
              var cdrift=Math.sin(now/6000+ci*2.8)*r*0.15; /* fast wind drift */
              /* Streak length varies — wispy and discontinuous */
              var cLen=r*(0.3+0.4*((ci*7+13)%5/5));
              var cxStart=x-cLen*0.5+Math.cos(ci*1.4)*r*0.2;
              ctx.strokeStyle='rgba(195,220,252,'+(0.12+0.06*Math.sin(now/3000+ci))+')';
              ctx.lineWidth=Math.max(0.5,1.5-ci*0.15);
              ctx.beginPath();
              for(var cx2=cxStart;cx2<cxStart+cLen;cx2+=1.5){
                var cw=Math.sin((cx2-x)/r*12+now/2500+ci)*r*0.012;
                if(cx2===cxStart)ctx.moveTo(cx2+cdrift,cy2+cw);
                else ctx.lineTo(cx2+cdrift,cy2+cw);
              }
              ctx.stroke();
            }
            /* Great Dark Spot — deep anticyclone (Voyager discovered, later disappeared) */
            var gdX=x+Math.cos(now/14000)*r*0.18;
            var gdY=y-r*0.12;
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
            /* Companion bright cloud — "scooter" feature just south, moving faster */
            var scootX=x+Math.cos(now/8000)*r*0.22;
            var scootY=gdY+r*0.14;
            var scootG=ctx.createRadialGradient(scootX,scootY,0,scootX,scootY,r*0.055);
            scootG.addColorStop(0,'rgba(210,228,252,0.25)');
            scootG.addColorStop(0.5,'rgba(185,215,250,0.10)');
            scootG.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=scootG;ctx.beginPath();ctx.ellipse(scootX,scootY,r*0.055,r*0.032,0,0,Math.PI*2);ctx.fill();
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
            /* ═══ WHALE ALERT PULSE RING — bright blue expanding ring ═══
               Fires when Deep Blue's whaleCount > 0. Check global node data. */
            var dbNode = _orbNodes && _orbNodes.deepblue;
            var wc     = dbNode ? (dbNode.whaleCount || 0) : 0;
            if(wc > 0) {
                /* 2-3 concentric expanding rings, pulsing at ~1s interval */
                var nRings = Math.min(3, wc);
                for(var wi=0; wi<nRings; wi++){
                    var wPhase = ((now / 1400 + wi * 0.33) % 1);
                    var wR     = r * (1.1 + wPhase * 2.0);
                    var wA     = (1 - wPhase) * (1 - wPhase) * 0.45;
                    ctx.strokeStyle = 'rgba(50,180,255,' + wA.toFixed(3) + ')';
                    ctx.lineWidth   = 1.5 - wPhase;
                    ctx.beginPath(); ctx.arc(x, y, Math.max(0.1, wR), 0, Math.PI * 2); ctx.stroke();
                }
                /* Steady inner glow when whales active */
                var wGlow = ctx.createRadialGradient(x,y,r*0.9,x,y,r*1.6);
                wGlow.addColorStop(0,'rgba(30,140,255,0.08)');
                wGlow.addColorStop(1,'rgba(0,0,0,0)');
                ctx.fillStyle=wGlow;ctx.beginPath();ctx.arc(x,y,r*1.6,0,Math.PI*2);ctx.fill();
            }
        }
    },

    confluence: {
        baseColor: [255, 109, 0],
        atmosphere: [255, 160, 80],
        surface: function(ctx, x, y, r, lx, ly, now) {
            /* Four converging intel beams — Oracle, Deep Blue, NEXUS, Sentinel —
               sweep inward toward the core. Represents the aggregator reading
               four upstream sources rather than a chart of its own. */
            var beamAngles = [-Math.PI/2, 0, Math.PI/2, Math.PI]; /* N, E, S, W */
            for (var bi = 0; bi < 4; bi++) {
                var ba = beamAngles[bi] + now / 9000;
                var phase = ((now / 1600 + bi * 0.25) % 1);
                var bR = Math.max(0.1, r * (1.35 - phase * 1.1));
                var bA = (1 - phase) * 0.4;
                var bx = x + Math.cos(ba) * bR, by = y + Math.sin(ba) * bR;
                ctx.strokeStyle = 'rgba(255, 150, 60, ' + bA.toFixed(3) + ')';
                ctx.lineWidth = Math.max(0.3, 1.4 * (1 - phase));
                ctx.beginPath();
                ctx.moveTo(bx, by);
                ctx.lineTo(x, y);
                ctx.stroke();
            }
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* Consensus core — pulses brighter when beams phase-align,
               echoing the "2+ sources agree" entry gate. */
            var align = 0;
            for (var bi = 0; bi < 4; bi++) {
                var phase = ((now / 1600 + bi * 0.25) % 1);
                if (phase < 0.15) align++;
            }
            var pulse = align >= 2 ? 0.85 : (0.35 + 0.15 * Math.sin(now / 500));
            var coreR = Math.max(0.1, r * 0.55);
            var glow = ctx.createRadialGradient(x, y, 0, x, y, coreR);
            glow.addColorStop(0, 'rgba(255, 200, 120, ' + (0.55 * pulse).toFixed(3) + ')');
            glow.addColorStop(0.5, 'rgba(255, 109, 0, ' + (0.25 * pulse).toFixed(3) + ')');
            glow.addColorStop(1, 'rgba(255, 60, 0, 0)');
            ctx.fillStyle = glow;
            ctx.beginPath();
            ctx.arc(x, y, coreR, 0, Math.PI * 2);
            ctx.fill();
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
            /* Magnetic field lines radiating out */
            ctx.strokeStyle = 'rgba(210, 100, 255, 0.12)';
            ctx.lineWidth = 0.6;
            for (var i = 0; i < 8; i++) {
                var a = (i/8) * Math.PI * 2 + now / 10000;
                ctx.beginPath();
                for (var t = 0; t <= 1; t += 0.08) {
                    var fr = r * (1 + Math.sin(t * Math.PI) * 0.6);
                    var fa = a + (t - 0.5) * 0.5;
                    var fx = x + Math.cos(fa) * fr;
                    var fy = y + Math.sin(fa) * fr;
                    if (t === 0) ctx.moveTo(fx, fy);
                    else ctx.lineTo(fx, fy);
                }
                ctx.stroke();
            }
            /* Pulsar beam */
            var beamA = now / 3000;
            for (var pole = -1; pole <= 1; pole += 2) {
                var bx = x + Math.cos(beamA) * r * 2.5 * pole;
                var by = y + Math.sin(beamA) * r * 2.5 * pole;
                var bG = ctx.createRadialGradient(x, y, r*0.3,
                    x + (bx-x)*0.5, y + (by-y)*0.5, r*0.2);
                bG.addColorStop(0, 'rgba(210, 100, 255, 0.08)');
                bG.addColorStop(1, 'rgba(0,0,0,0)');
                ctx.fillStyle = bG;
                ctx.beginPath();
                ctx.ellipse(x+(bx-x)*0.5, y+(by-y)*0.5, r*0.12, r*1.2, beamA, 0, Math.PI*2);
                ctx.fill();
            }
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
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
            /* Ice surface cracks */
            ctx.strokeStyle = 'rgba(180, 215, 245, 0.15)';
            ctx.lineWidth = 0.5;
            var cracks = [[0.1,0.1,0.7,0.6],[-0.2,-0.3,0.5,0.4],[-0.4,0.2,0.3,-0.5],[0.3,-0.2,-0.1,0.7]];
            for (var ci = 0; ci < cracks.length; ci++) {
                var cr = cracks[ci];
                ctx.beginPath();
                ctx.moveTo(x+cr[0]*r, y+cr[1]*r);
                ctx.lineTo(x+cr[2]*r, y+cr[3]*r);
                ctx.stroke();
            }
            /* Radar sweep */
            var sweep = (now / 2500) % (Math.PI * 2);
            var sweepG = ctx.createRadialGradient(x, y, r*0.2, x, y, r*1.5);
            sweepG.addColorStop(0, 'rgba(0, 200, 180, 0.08)');
            sweepG.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = sweepG;
            ctx.beginPath();
            ctx.moveTo(x, y);
            ctx.arc(x, y, r*1.5, sweep-0.15, sweep+0.15);
            ctx.closePath();
            ctx.fill();
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
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
            ctx.strokeStyle='rgba(155,195,240,0.45)';ctx.lineWidth=0.8;
            ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);ctx.stroke();
            /* Dish arc — parabola approximated as arc, perpendicular to stem */
            var dishR=r*0.32;
            var perpA=dishAngle+Math.PI/2;
            ctx.strokeStyle='rgba(155,195,240,0.55)';ctx.lineWidth=1;
            ctx.beginPath();
            ctx.arc(ex,ey,dishR,perpA-0.7,perpA+0.7);
            ctx.stroke();
            /* Small dot at focal point */
            ctx.fillStyle='rgba(200,230,255,0.5)';
            ctx.beginPath();ctx.arc(ex,ey,1.2,0,Math.PI*2);ctx.fill();
            /* Faint signal cone from dish center */
            var coneLen=r*1.1;
            var coneG=ctx.createRadialGradient(ex,ey,0,ex,ey,coneLen);
            coneG.addColorStop(0,'rgba(100,200,220,0.06)');
            coneG.addColorStop(1,'rgba(0,0,0,0)');
            ctx.fillStyle=coneG;
            ctx.beginPath();ctx.moveTo(ex,ey);
            ctx.arc(ex,ey,coneLen,dishAngle-0.22,dishAngle+0.22);
            ctx.closePath();ctx.fill();
        }
    },

    turtlesue: {
        baseColor: [155, 140, 110],
        atmosphere: [185, 170, 140],
        surface: function(ctx, x, y, r, lx, ly, now) {
            if (_turtlesueDeathStarImg.complete && _turtlesueDeathStarImg.naturalWidth > 0) {
                ctx.save();
                var sz = r * 3;
                ctx.drawImage(_turtlesueDeathStarImg, x - sz/2, y - sz/2, sz, sz);
                ctx.restore();
            }
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
            /* Superlaser dish glow — green charging pulse at upper-right quadrant */
            var pulse = 0.5 + 0.5 * Math.sin(now / 300);
            var dishX = x + r * 0.3;
            var dishY = y - r * 0.1;
            var laserG = ctx.createRadialGradient(dishX, dishY, 0, dishX, dishY, r * 0.8);
            laserG.addColorStop(0, 'rgba(0, 255, 80, ' + (0.40 * pulse).toFixed(3) + ')');
            laserG.addColorStop(0.5, 'rgba(0, 200, 60, ' + (0.15 * pulse).toFixed(3) + ')');
            laserG.addColorStop(1, 'rgba(0, 150, 40, 0)');
            ctx.fillStyle = laserG;
            ctx.beginPath();
            ctx.arc(dishX, dishY, r * 0.8, 0, Math.PI * 2);
            ctx.fill();
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
                    ctx.strokeStyle = 'rgba(80,220,210,' + (0.16*depthA).toFixed(3) + ')';
                    ctx.lineWidth = 0.6;
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
                var moteA = 0.28 + flareBright*0.55;
                var moteR = Math.max(0.1, 1.3 + flareBright*1.6);
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
            /* Ice cracks with red veins */
            ctx.strokeStyle = 'rgba(255, 80, 80, 0.18)';
            ctx.lineWidth = 0.6;
            var cracks = [[0.15,-0.1,0.6,0.5],[-0.3,-0.4,0.2,0.3],[-0.5,0.1,-0.1,-0.6],[0.4,-0.3,-0.2,0.5],[0.1,0.2,0.55,-0.15],[-0.35,0.35,0.15,0.55]];
            for (var ci = 0; ci < cracks.length; ci++) {
                var cr = cracks[ci];
                ctx.beginPath(); ctx.moveTo(x+cr[0]*r, y+cr[1]*r); ctx.lineTo(x+cr[2]*r, y+cr[3]*r); ctx.stroke();
            }
            /* Frost patches */
            var fG = ctx.createRadialGradient(x-r*0.2, y-r*0.3, 0, x-r*0.2, y-r*0.3, r*0.25);
            fG.addColorStop(0, 'rgba(200, 180, 200, 0.1)'); fG.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = fG; ctx.beginPath(); ctx.arc(x-r*0.2, y-r*0.3, r*0.25, 0, Math.PI*2); ctx.fill();
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
            /* Honeycomb hex cells */
            var hs = r * 0.18;
            var hh = hs * Math.sqrt(3) / 2;
            ctx.strokeStyle = 'rgba(235, 215, 95, 0.1)'; ctx.lineWidth = 0.5;
            var pulse = Math.sin(now / 2000);
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
                    /* Fill some cells based on position + time */
                    if (Math.sin(row * 3.7 + col * 2.1 + now / 3000) > 0.3) {
                        ctx.fillStyle = 'rgba(235, 215, 95, 0.06)'; ctx.fill();
                    }
                }
            }
        },
        overlay: function(ctx, x, y, r, lx, ly, now) {
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
            /* Radar sweep lines across surface */
            var sweep = (now / 2000) % (Math.PI * 2);
            /* Grid lines — faint */
            ctx.strokeStyle = 'rgba(96, 165, 250, 0.06)'; ctx.lineWidth = 0.4;
            for (var ri = 1; ri <= 3; ri++) {
                ctx.beginPath(); ctx.arc(x, y, r * ri * 0.3, 0, Math.PI * 2); ctx.stroke();
            }
            /* Sweep beam */
            var swG = ctx.createRadialGradient(x, y, r*0.1, x, y, r);
            swG.addColorStop(0, 'rgba(96, 165, 250, 0.1)');
            swG.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = swG;
            ctx.beginPath(); ctx.moveTo(x, y);
            ctx.arc(x, y, r, sweep - 0.2, sweep + 0.2);
            ctx.closePath(); ctx.fill();
            /* Blip dots on sweep path */
            for (var bi = 0; bi < 3; bi++) {
                var ba = sweep - bi * 0.6;
                var br2 = r * (0.3 + bi * 0.2);
                ctx.fillStyle = 'rgba(96, 165, 250, ' + (0.2 - bi * 0.06) + ')';
                ctx.beginPath(); ctx.arc(x + Math.cos(ba)*br2, y + Math.sin(ba)*br2, 1.2, 0, Math.PI*2); ctx.fill();
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
            /* Neural processing glow — pulsing core with data circuit lines */
            var pulse = 0.5 + 0.5 * Math.sin(now / 800);
            var coreG = ctx.createRadialGradient(x, y, 0, x, y, r * 0.6);
            coreG.addColorStop(0, 'rgba(120, 180, 255, ' + (0.15 + 0.1 * pulse).toFixed(3) + ')');
            coreG.addColorStop(1, 'rgba(60, 100, 180, 0)');
            ctx.fillStyle = coreG;
            ctx.beginPath(); ctx.arc(x, y, r * 0.6, 0, Math.PI * 2); ctx.fill();
            /* Circuit traces */
            ctx.strokeStyle = 'rgba(100, 160, 255, 0.1)';
            ctx.lineWidth = 0.4;
            for (var li = 0; li < 3; li++) {
                var la = li * 2.1 + now / 12000;
                ctx.beginPath();
                ctx.moveTo(x + Math.cos(la) * r * 0.2, y + Math.sin(la) * r * 0.2);
                ctx.lineTo(x + Math.cos(la + 0.5) * r * 0.7, y + Math.sin(la + 0.3) * r * 0.6);
                ctx.stroke();
            }
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
    /* Skip surface for planets/moons with custom fullscreen renderers;
       keep it for stars (oracle, deepblue, nexus) which still use drawPlanet */
    var _skipSurf=(planetType!=="oracle"&&planetType!=="deepblue"&&planetType!=="nexus");
    if (vis.surface && !_skipSurf) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.clip();
        vis.surface(ctx, x, y, r, lx, ly, now);
        ctx.restore();
    }

    /* ═══ OVERLAY — drawn AFTER planet, not clipped (rings, etc.) ═══ */
    if (vis.overlay && !_skipSurf) {
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


