/* Deep field must be able to REACH the whole frame, and fill it evenly.

   THE BUG THIS PINS. maxVisibleR in command_center_v4.html was
   (Math.min(W,H)/2)/_ORB_ZOOM_MIN — the radius of the largest circle
   that fits INSIDE the viewport. On a square display that is the corner
   distance; on every real display here it is not. Measured at 2000x1125
   the annulus was capped at 1480 world units while the frame corners sit
   at 3019, so the deep field was STRUCTURALLY incapable of drawing into
   61% of the visible frame no matter how many objects were added. It
   read as "the sky is empty" and the instinct is to add content, which
   would not have helped — the missing quantity was radius.

   Second, compounding bug: placement was uniform in RADIUS, so areal
   density fell as 1/r — measured 2095 at the inner edge against 500 at
   the outer, a 4.2x thinning exactly where the newly-opened space is.

   Both are geometry, both are silent, and both look like a taste problem
   from the screenshot. This test asserts the math directly so neither
   can come back as "the cosmos feels empty again". */
import fs from 'fs';
let src=fs.readFileSync(new URL('../solar_system.js',import.meta.url),'utf8');
const stub=`
var CanvasRenderingContext2D=function(){}; CanvasRenderingContext2D.prototype={};
var window={}, document={createElement:function(){return {getContext:function(){return {}},width:0,height:0}}};
var localStorage={getItem:function(){return null},setItem:function(){}};
var requestAnimationFrame=function(){}, performance={now:function(){return 0}};
var Image=function(){}; var Path2D=function(){}; var OffscreenCanvas=function(){this.getContext=function(){return {}}};
var navigator={userAgent:"node"}; var screen={width:1920,height:1080}; var AudioContext=function(){};
`;
let M;
try{
  M=new Function(stub+src+'; return {DeepField:DeepField,S:_DF_DRIFTER_SLOTS,H:_DF_DRIFT_HUES,N:_DF_NEBULA_SLOTS,C:_DF_COMET_SLOTS,A:_dfAnnulusR,VS:_DF_VISITOR_SLOTS,VB:_DF_VISITOR_BASE,NS:_DF_NEBULA_SLOTS,NB:_DF_NEBULA_BASE,EP:_DF_EPOCHS};')();
}catch(e){ console.log("LOAD FAIL:",e.message); process.exit(1); }
const df=new M.DeepField(12345);
df.center={x:0,y:0}; df.innerR=420; df.outerR=2838;
const bandIn=420+(2838-420)*0.45;
let fails=[], maxA=0, kinds={galaxy:0,dust:0};
for(let i=0;i<M.S;i++){
  const d=df._drifterAt(i,60000);
  const r=Math.hypot(d.x,d.y);
  if(r<bandIn-1||r>2839) fails.push(`slot ${i} r=${r.toFixed(0)} outside band [${bandIn.toFixed(0)},2838]`);
  if(!M.H.includes(d.hue)) fails.push(`slot ${i} hue ${d.hue} not in drifter palette`);
  if(!(d.flat>0&&d.flat<=1)) fails.push(`slot ${i} flat ${d.flat} out of range`);
  maxA=Math.max(maxA,d.alpha); kinds[d.kind]++;
}
if(maxA>0.085) fails.push(`max alpha ${maxA.toFixed(4)} exceeds 0.085`);
const a=df._drifterAt(3,777000), b=df._drifterAt(3,777000);
if(a.x!==b.x||a.rot!==b.rot) fails.push('not deterministic');
const far=df._drifterAt(3,6*3600*1000);
if(!isFinite(far.x)||!isFinite(far.rot)||!isFinite(far.alpha)) fails.push('non-finite at 6h');
const t0=df._drifterAt(2,0).rot,t1=df._drifterAt(2,45*60*1000).rot;
if(Math.abs(t1-t0)<0.5) fails.push(`rotation stalls (${(t1-t0).toFixed(3)} rad/45min)`);
// separation: drifter hues must not collide with nebula pool
const nebHues=[8,14,200,208,258,264];
const overlap=M.H.filter(h=>nebHues.includes(h));
if(overlap.length) fails.push('drifter hues overlap nebula pool: '+overlap);
console.log("slots -> drifters:",M.S,"nebulae:",M.N,"comets:",M.C);
console.log("kinds:",JSON.stringify(kinds),"maxAlpha:",maxA.toFixed(4));
console.log("45min rotation:",(t1-t0).toFixed(3),"rad");

/* --- frame coverage: the annulus must reach the viewport corners --- */
const ZMIN=0.38;
function maxVisibleR(W,H){ return Math.sqrt(W*W+H*H)/2/ZMIN; }
[[2000,1125],[3840,2160],[1920,1080],[2560,1080]].forEach(([W,H])=>{
  const corner=Math.hypot(W/2,H/2)/ZMIN;
  const got=maxVisibleR(W,H);
  if(Math.abs(got-corner)>1e-6) fails.push(`maxVisibleR(${W}x${H}) = ${got.toFixed(0)}, corner reach = ${corner.toFixed(0)}`);
  const inscribed=(Math.min(W,H)/2)/ZMIN;
  if(W!==H && got<=inscribed+1) fails.push(`maxVisibleR(${W}x${H}) still using inscribed circle — the wide-display bug is back`);
});
/* The shipped dashboard must actually use the diagonal form. */
{
  const cc=fs.readFileSync(new URL('../command_center_v4.html',import.meta.url),'utf8');
  if(!/_dfMaxVisibleR\s*=\s*Math\.sqrt\(W\*W\+H\*H\)/.test(cc))
    fails.push('command_center_v4.html no longer computes _dfMaxVisibleR from the half-diagonal');
  if(/_dfMaxVisibleR\s*=\s*\(Math\.min\(W,H\)/.test(cc))
    fails.push('command_center_v4.html reverted to the inscribed-circle cap');
}
/* --- areal density must be flat across the annulus --- */
{
  const inner=420,outer=2838,BINS=6,N=120000;
  const c=new Array(BINS).fill(0);
  for(let k=0;k<N;k++){
    const r=M.A(Math.random(),inner,outer);
    const b=Math.min(BINS-1,Math.floor((r-inner)/(outer-inner)*BINS));
    if(b>=0)c[b]++;
  }
  const d=[];
  for(let b=0;b<BINS;b++){
    const r0=inner+(outer-inner)*b/BINS, r1=inner+(outer-inner)*(b+1)/BINS;
    d.push(c[b]/(Math.PI*(r1*r1-r0*r0)));
  }
  const lo=Math.min(...d), hi=Math.max(...d);
  const spread=hi/lo;
  console.log("areal density spread across annulus:",spread.toFixed(3)+"x (uniform-in-radius would be ~4.2x)");
  if(spread>1.25) fails.push(`areal density spread ${spread.toFixed(2)}x — placement is not uniform in area`);
}


/* --- interstellar visitors: pacing, boundedness, no-repeat --- */
{
  const dfv=new M.DeepField(20260827);
  dfv.center={x:0,y:0}; dfv.innerR=420; dfv.outerR=2838;
  const H=3*3600;
  let visible=0; const seen=new Set(); let maxLive=0; let bad=0;
  for(let t=0;t<H;t++){
    let live=0;
    /* Walk only the chains the CURRENT EPOCH actually runs -- draw()
       iterates Math.round(VB * ep.vis), not the whole pool. An earlier
       version of this test looped over all 9 pool slots and reported a
       sighting every 2.7 min when the shipped ordinary-era rate is
       ~7.8 min: it was measuring a configuration that never renders. */
    const epv=dfv.epochAt(t*1000);
    const active=Math.max(1,Math.min(M.VS,Math.round(M.VB*epv.vis)));
    for(let s=0;s<active;s++){
      const v=dfv._visitorAt(s,t*1000);
      if(v.phase!=='transit') continue;
      live++; seen.add(s+':'+v.idx);
      if(!isFinite(v.x)||!isFinite(v.y)||!isFinite(v.alpha)||!isFinite(v.len)) bad++;
      if(v.alpha<0||v.alpha>1) bad++;
      if(v.len<=0) bad++;
    }
    if(live>0) visible++;
    maxLive=Math.max(maxLive,live);
  }
  const perMin=H/60/seen.size, occ=visible/H*100;
  console.log("visitors: one every "+perMin.toFixed(1)+" min, sky occupied "+occ.toFixed(1)+"%, max concurrent "+maxLive);
  if(bad) fails.push(`${bad} visitor samples had non-finite or out-of-range fields`);
  /* The brief is "interesting enough to stay glued to the screen" on one
     side and "an event, not traffic" on the other. Both edges are real
     failures, so both are asserted. Measured 2026-08-27: 2.5 min / 28%. */
  /* Averaged across epochs. 'silence' legitimately goes minutes with an
     empty sky and 'convergence' legitimately swarms -- the average is what
     must stay watchable. */
  if(perMin>12) fails.push(`visitors too rare on average (one every ${perMin.toFixed(1)} min) - a viewer can watch for a long time and see nothing`);
  if(perMin<1.5) fails.push(`visitors too frequent on average (one every ${perMin.toFixed(1)} min) - they read as traffic, not as events`);
  if(occ>45) fails.push(`sky occupied ${occ.toFixed(0)}% of the time - a visitor is supposed to be an event, not the normal state`);
  if(occ<5) fails.push(`sky occupied only ${occ.toFixed(0)}% of the time - too sparse to reward watching`);
  /* Determinism + never-repeats: same slot/idx must be one continuous
     transit, and a visitor must never come back around. */
  const a=dfv._visitorAt(0,900000), b=dfv._visitorAt(0,900000);
  if(a.phase!==b.phase||a.x!==b.x) fails.push('visitor not deterministic for the same elapsedMs');
  const far=dfv._visitorAt(0,9*3600*1000);
  if(far.phase==='transit'&&(!isFinite(far.x)||!isFinite(far.alpha))) fails.push('visitor non-finite at 9h fast-forward');
  /* Visitors must NOT encode fleet data - they are declared scenery. */
  const vsrc=fs.readFileSync(new URL('../solar_system.js',import.meta.url),'utf8');
  let vfn=vsrc.slice(vsrc.indexOf('DeepField.prototype._visitorAt'), vsrc.indexOf('DeepField.prototype._cometAt'));
  /* Strip comments before scanning. The first version of this check matched
     the word "position" inside _visitorAt's OWN explanatory comment and
     failed against correct code -- the same docstring-vs-source trap that
     bit test_confluence_exit_and_emit.py. Only executable text counts. */
  vfn=vfn.replace(/\/\*[\s\S]*?\*\//g,'').replace(/^\s*\/\/.*$/gm,'');
  if(/orbNodes|_orbNodes|pnl|positions|trades/i.test(vfn))
    fails.push('_visitorAt reads fleet state - visitors are declared scenery and must encode no measurement');
}


/* --- epochs: the field must actually evolve, and rare must be rare --- */
{
  const dfe=new M.DeepField(20260827);
  dfe.center={x:0,y:0}; dfe.innerR=420; dfe.outerR=2838;
  const names=new Set(); let transitions=0, last=null, badBlend=0;
  const dwell={};
  for(let t=0;t<24*3600;t+=30){
    const e=dfe.epochAt(t*1000);
    names.add(e.name); dwell[e.name]=(dwell[e.name]||0)+30;
    if(e.idx!==last){transitions++;last=e.idx;}
    if(!(e.blend>=0&&e.blend<=1)) badBlend++;
    for(const k of ['neb','sat','vis','drift']) if(!isFinite(e[k])||e[k]<=0) badBlend++;
  }
  console.log("epochs in 24h:",names.size,"distinct,",transitions,"transitions");
  if(badBlend) fails.push(`${badBlend} epoch samples had an out-of-range blend or multiplier`);
  if(names.size<4) fails.push(`only ${names.size} distinct epochs in 24h - the field is not evolving`);
  if(transitions<12) fails.push(`only ${transitions} epoch transitions in 24h - eras are too long to notice`);
  if(transitions>80) fails.push(`${transitions} epoch transitions in 24h - eras churn too fast to read as weather`);
  /* Determinism + fast-forward, same contract as everything else here. */
  const e1=dfe.epochAt(5000000), e2=dfe.epochAt(5000000);
  if(e1.name!==e2.name||e1.t01!==e2.t01) fails.push('epochAt is not deterministic');
  const eFar=dfe.epochAt(30*3600*1000);
  if(!eFar||!isFinite(eFar.t01)) fails.push('epochAt broken at 30h fast-forward');
  /* Every epoch must be visually DISTINCT in what it renders. The first
     version clamped 6 of 8 epochs to an identical chain count, so the
     rarest era in the table rendered exactly like the most common one. */
  const shapes=new Set();
  for(const e of M.EP){
    const v=Math.max(1,Math.min(M.VS,Math.round(M.VB*e.vis)));
    const n=Math.max(2,Math.min(M.NS,Math.round(M.NB*e.neb)));
    shapes.add(v+'/'+n);
  }
  console.log("distinct epoch render-shapes:",shapes.size,"of",M.EP.length,"epochs");
  if(shapes.size<M.EP.length-1)
    fails.push(`only ${shapes.size} distinct render shapes across ${M.EP.length} epochs - eras that clamp to the same counts are invisible to a viewer`);
}

if(fails.length){console.log("FAIL:");fails.forEach(f=>console.log("  "+f));process.exit(1);}
console.log("PASS: deep field reaches frame corners, density flat, drifters bounded/deterministic");
