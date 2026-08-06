function escHtml(s){ return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
var $freshness={innerHTML:''};
function renderFreshness(bots){
  var now=Date.now()/1000;
  var unreachable=[], frozen=[];
  for(var i=0;i<bots.length;i++){
    var b=bots[i];
    if(!b.alive) continue;
    if(b.last_seen){
      var age=Math.round(now-b.last_seen);
      if(age>60) unreachable.push({name:b.name,age:age});
    }
    /* data_stale is the backend's per-bot verdict. Absent stale_age_s means
       the bot serves no timestamp — not evidence of staleness, so say
       nothing rather than guess. */
    if(b.data_stale===true){
      frozen.push({name:b.name,
                   age:(typeof b.stale_age_s==='number')?Math.round(b.stale_age_s):null});
    }
  }
  if(!unreachable.length && !frozen.length){
    $freshness.innerHTML='<span style="color:var(--green)">All data fresh</span>';
    return;
  }
  var h='';
  h+=frozen.map(function(s){
    return '<div style="color:#f97316">'+escHtml(s.name)+': data frozen'
      + (s.age!=null?' ('+s.age+'s old)':'')+'</div>';
  }).join("");
  h+=unreachable.map(function(s){
    return '<div style="color:var(--gold)">'+escHtml(s.name)+': unreachable ('+s.age+'s)</div>';
  }).join("");
  $freshness.innerHTML=h;
}
var fails=0;
function check(n,c,d){ console.log((c?'  PASS  ':'  FAIL  ')+n+(d?'  — '+d:'')); if(!c)fails++; }
var NOW=Date.now()/1000;

console.log('=== CASE 1: the failure the old rule could not see ===');
// Bot answers HTTP instantly but its scan loop died 3h ago.
renderFreshness([{name:'HiveMind',alive:true,last_seen:NOW-5,data_stale:true,stale_age_s:10800}]);
console.log('     '+$freshness.innerHTML);
check('frozen bot is reported', /data frozen/.test($freshness.innerHTML),
      'old rule saw last_seen=5s and said "All data fresh"');
check('shows the DATA age not the poll age', /10800s old/.test($freshness.innerHTML));

console.log('');
console.log('=== CASE 2: a healthy slow bot is NOT flagged ===');
// hivemind budget is 2100s; a 251.9s snapshot is fine. Backend says so.
renderFreshness([{name:'HiveMind',alive:true,last_seen:NOW-5,data_stale:false,stale_age_s:251.9}]);
console.log('     '+$freshness.innerHTML);
check('slow-cadence bot stays fresh', /All data fresh/.test($freshness.innerHTML),
      'a flat 60s rule would have flagged this healthy bot');

console.log('');
console.log('=== CASE 3: unreachable is still reported, and kept distinct ===');
renderFreshness([{name:'Oracle',alive:true,last_seen:NOW-300,data_stale:false,stale_age_s:0}]);
console.log('     '+$freshness.innerHTML);
check('unreachable reported', /unreachable \(300s\)/.test($freshness.innerHTML));
check('not called frozen', !/data frozen/.test($freshness.innerHTML),
      'stale data and an unreachable bot are different problems');

console.log('');
console.log('=== CASE 4: no timestamp means say nothing, not "stale" ===');
renderFreshness([{name:'TurtleSue',alive:true,last_seen:NOW-5,data_stale:false,stale_age_s:null}]);
check('absent timestamp is not evidence', /All data fresh/.test($freshness.innerHTML));

console.log('');
console.log('=== CASE 5: both problems at once, both shown ===');
renderFreshness([
  {name:'HiveMind',alive:true,last_seen:NOW-5,data_stale:true,stale_age_s:9000},
  {name:'Oracle',alive:true,last_seen:NOW-200,data_stale:false,stale_age_s:0},
]);
console.log('     '+$freshness.innerHTML.replace(/</g,'\n       <').trim());
check('both listed', /data frozen/.test($freshness.innerHTML)&&/unreachable/.test($freshness.innerHTML));

console.log('');
console.log('=== CASE 6: dead bots are skipped (handled elsewhere) ===');
renderFreshness([{name:'Dead',alive:false,last_seen:NOW-9999,data_stale:true,stale_age_s:9999}]);
check('offline bot not double-reported', /All data fresh/.test($freshness.innerHTML));

console.log('');
console.log(fails?(fails+' FAILED'):'ALL PASS — frozen data and unreachability are now distinguishable');
process.exit(fails?1:0);
