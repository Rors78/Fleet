/* Inter-body lighting geometry — COSMOS solar system (2026-08-18).
 *
 * Every body in solar_system.js was lit as if alone in space: no shadows
 * cast between bodies, no planetshine, and a terminator that was a flat
 * alpha ramp rather than scattered air. This checks the geometry of the
 * three terms that fixed that, because they are real trig and a sign error
 * would be invisible until someone stared at the 50" display.
 *
 * Calibrated against the ACTUAL fleet layout from CELESTIAL_HIERARCHY:
 * planets sz 25-27 orbit their star at 111-156px; stars sz 34-36 orbit the
 * sun at 468-585px. An early version of this test placed a "near"
 * neighbour 306px away and reported a false failure when the code
 * correctly rejected it as out of reach.
 *
 * Runs headless: the module is eval'd out of solar_system.js with a stub
 * canvas that records which primitives were issued, so no DOM is needed.
 */
/* Verify the shadow/planetshine geometry against hand-checkable cases. */
global.CanvasRenderingContext2D = function(){};
CanvasRenderingContext2D.prototype = { createRadialGradient: function(){ return {addColorStop:function(){}}; } };

var calls = [];
function mkCtx(){
  return {
    save:function(){}, restore:function(){}, beginPath:function(){},
    arc:function(x,y,r){ this._lastArc=[x,y,r]; }, clip:function(){},
    fill:function(){ calls.push({type:'shadow', arc:this._lastArc}); },
    fillRect:function(){ calls.push({type:'shine'}); },
    stroke:function(){ calls.push({type:'rim'}); },
    createRadialGradient:function(){ return {addColorStop:function(){}}; },
    set fillStyle(v){}, set strokeStyle(v){}, set lineWidth(v){},
    set globalCompositeOperation(v){}
  };
}

var src = require('fs').readFileSync('D:/CommandCenter/solar_system.js','utf8');
/* Pull just the inter-body block so we do not need the whole DOM. */
var start = src.indexOf('var _ibScene =');
eval(src.slice(start));

/* --- Case 1: occluder directly between sun and receiver --- */
global._orbNodes = {
  cc:      {x:0,   y:0,   currentSize:40},
  blocker: {x:100, y:0,   currentSize:20, rgb:'200,150,90', alive:true},
  target:  {x:200, y:0,   currentSize:20, rgb:'90,150,200', alive:true}
};
calls = [];
_ibScene.t = 0;
_ibCastShadows(mkCtx(), 200, 0, 20, 0, 0, 'target', 1000);
var shadows = calls.filter(function(c){return c.type==='shadow';});
console.log('case1 directly-between      : ' + shadows.length + ' shadow(s)  (expect >=1)');
if (shadows.length) {
  var a = shadows[0].arc;
  console.log('   shadow lands at x=' + a[0].toFixed(1) + ' y=' + a[1].toFixed(1) +
              '  (expect x~200, y~0 — on the target)');
}

/* --- Case 2: occluder BEHIND the receiver casts nothing --- */
global._orbNodes.blocker.x = 400;   /* past the target */
calls = []; _ibScene.t = 0;
_ibCastShadows(mkCtx(), 200, 0, 20, 0, 0, 'target', 2000);
console.log('case2 occluder behind       : ' + calls.filter(function(c){return c.type==='shadow';}).length +
            ' shadow(s)  (expect 0)');

/* --- Case 3: occluder far off the ray casts nothing --- */
global._orbNodes.blocker.x = 100; global._orbNodes.blocker.y = 5000;
calls = []; _ibScene.t = 0;
_ibCastShadows(mkCtx(), 200, 0, 20, 0, 0, 'target', 3000);
console.log('case3 occluder off-axis     : ' + calls.filter(function(c){return c.type==='shadow';}).length +
            ' shadow(s)  (expect 0)');

/* --- Case 4: planetshine falls off with distance --- */
function shineCount(dist){
  global._orbNodes = {
    cc:     {x:0, y:0, currentSize:40},
    near:   {x:dist, y:0, currentSize:20, rgb:'200,150,90', alive:true},
    target: {x:0, y:0, currentSize:20, rgb:'90,150,200', alive:true}
  };
  global._orbNodes.target.x = 0; global._orbNodes.target.y = 0;
  global._orbNodes.near.x = dist; global._orbNodes.near.y = 0;
  global._orbNodes.cc.x = 0; global._orbNodes.cc.y = -585;
  calls = []; _ibScene.t = 0;
  _ibPlanetshine(mkCtx(), 0, 0, 26, 0, -585, 'target', 4000);
  return calls.filter(function(c){return c.type==='shine';}).length;
}
console.log('case4 shine  sibling 60px   : ' + shineCount(60)  + '  (expect >=1)');
console.log('case4 shine  parent  130px  : ' + shineCount(130) + '  (expect >=1)');
console.log('case4 shine  far  neighbour : ' + shineCount(99999) + '  (expect 0)');

/* --- Case 5: atmosphere gates the scatter rim --- */
calls = [];
_ibAtmoScatter(mkCtx(), 100, 100, 20, 1, 0, [230,195,115]);
console.log('case5 rim with atmosphere   : ' + calls.filter(function(c){return c.type==='rim';}).length + '  (expect 3)');
calls = [];
_ibAtmoScatter(mkCtx(), 100, 100, 20, 1, 0, null);
console.log('case5 rim airless body      : ' + calls.filter(function(c){return c.type==='rim';}).length + '  (expect 0)');

/* --- Case 6: garbage scene must not throw --- */
global._orbNodes = { cc:null, bad:{x:NaN,y:0,currentSize:10}, worse:{}, nul:null };
calls = []; _ibScene.t = 0;
try {
  _ibApply(mkCtx(), 10, 10, 5, 0, 0, 1, 0, 'x', {atmosphere:[1,2,3]}, 5000);
  console.log('case6 garbage scene         : survived');
} catch(e) {
  console.log('case6 garbage scene         : THREW ' + e);
}

/* ── verdict ──────────────────────────────────────────────────────────
   The checks above printed their findings; this turns them into a pass or
   fail. A test that always exits 0 is not a test — it is a log. */
var FAIL = [];
function expect(label, got, want) {
    if (got !== want) FAIL.push(label + ': got ' + got + ', expected ' + want);
}

/* Re-run each case and assert rather than narrate. */
function shadowsFor(blockerX, blockerY) {
    global._orbNodes = {
        cc:      {x:0,   y:0,   currentSize:40},
        blocker: {x:blockerX, y:blockerY, currentSize:20, rgb:'200,150,90', alive:true},
        target:  {x:200, y:0,   currentSize:20, rgb:'90,150,200', alive:true}
    };
    calls = []; _ibScene.t = 0;
    _ibCastShadows(mkCtx(), 200, 0, 20, 0, 0, 'target', Math.random()*1e6);
    return calls.filter(function(c){ return c.type === 'shadow'; }).length;
}

expect('occluder directly between sun and target casts a shadow',
       shadowsFor(100, 0) >= 1, true);
expect('occluder BEHIND the target casts nothing',
       shadowsFor(400, 0), 0);
expect('occluder far off the sun ray casts nothing',
       shadowsFor(100, 5000), 0);

/* Shine, at real fleet distances. */
function shineAt(dist) {
    global._orbNodes = {
        cc:     {x:0, y:-585, currentSize:40},
        near:   {x:dist, y:0, currentSize:20, rgb:'200,150,90', alive:true},
        target: {x:0, y:0, currentSize:26, rgb:'90,150,200', alive:true}
    };
    calls = []; _ibScene.t = 0;
    _ibPlanetshine(mkCtx(), 0, 0, 26, 0, -585, 'target', Math.random()*1e6);
    return calls.filter(function(c){ return c.type === 'shine'; }).length;
}
expect('a sibling 60px away bounces light',  shineAt(60) >= 1, true);
expect('a parent star 130px away bounces light', shineAt(130) >= 1, true);
expect('a body across the system bounces nothing', shineAt(99999), 0);

/* Atmosphere gates the scatter rim: this is the visual difference between
   a world and a rock, so it must not be accidental. */
calls = [];
_ibAtmoScatter(mkCtx(), 100, 100, 20, 1, 0, [230,195,115]);
expect('an atmosphere draws the scatter rim',
       calls.filter(function(c){ return c.type === 'rim'; }).length, 3);
calls = [];
_ibAtmoScatter(mkCtx(), 100, 100, 20, 1, 0, null);
expect('an AIRLESS body draws no rim',
       calls.filter(function(c){ return c.type === 'rim'; }).length, 0);

/* Fail-quiet: this is decoration on a trading dashboard, and a thrown
   exception here would black out the entire COSMOS view. */
global._orbNodes = { cc:null, bad:{x:NaN,y:0,currentSize:10}, worse:{}, nul:null };
_ibScene.t = 0;
var survived = true;
try {
    _ibApply(mkCtx(), 10, 10, 5, 0, 0, 1, 0, 'x', {atmosphere:[1,2,3]}, 9e5);
} catch (e) { survived = false; FAIL.push('garbage scene threw: ' + e); }
expect('a malformed scene never throws', survived, true);

if (FAIL.length) {
    FAIL.forEach(function(f){ console.log('FAIL  ' + f); });
    process.exit(1);
}
console.log('ok  inter-body lighting geometry: shadows, planetshine, atmospheric scatter');
