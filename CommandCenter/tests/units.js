function fmtUptime(s){ if(s==null||isNaN(s))return "--";
  s=Math.round(s); if(s<60)return s+"s"; if(s<3600)return Math.round(s/60)+"m";
  if(s<86400)return (s/3600).toFixed(1)+"h"; return (s/86400).toFixed(1)+"d"; }
function ago(sec){ if(!sec)return "--"; var d=Date.now()/1000-sec;
  if(d<60)return Math.round(d)+"s ago"; if(d<3600)return Math.round(d/60)+"m ago";
  return Math.round(d/3600)+"h ago"; }
var fails=0;
function check(n,c,d){ console.log((c?'  PASS  ':'  FAIL  ')+n+(d?'  — '+d:'')); if(!c)fails++; }

console.log('=== PHITEX last_update: seconds, not milliseconds ===');
var now=Date.now()/1000;
console.log('     old: ago(ts/1000) -> "'+ago(now/1000)+'"');
console.log('     new: ago(ts)      -> "'+ago(now)+'"');
check('fresh snapshot reads as fresh', /s ago|0m ago/.test(ago(now)),
      'was "495624h ago" — the Unix epoch rendered as a measurement');
check('the old form was ~56 years', Math.round((now-now/1000)/3600)>400000);
check('absent still renders --', ago(0)==='--');

console.log('');
console.log('=== uptime: a NUMBER is seconds and must be formatted ===');
function uptimeStr(norm,rawH){
  var _upRaw=(norm.uptime!=null)?norm.uptime:rawH.uptime;
  var s=(typeof _upRaw==="number"&&!isNaN(_upRaw))?fmtUptime(_upRaw):(_upRaw||null);
  if(!s&&rawH.uptime_s!=null) s=fmtUptime(rawH.uptime_s);
  if(!s) s="--";
  return s;
}
var t=uptimeStr({uptime:299.36459970474243},{});
console.log('     trinity 299.36459970474243 -> "'+t+'"');
check('float is formatted', t==='5m', 'was the raw float on screen');
check('a pre-formatted string passes through', uptimeStr({uptime:"4m"},{})==='4m');
check('seconds variant still works', uptimeStr({},{uptime_s:7200})==='2.0h');
check('absent renders --', uptimeStr({},{})==='--');
check('zero uptime is not mistaken for absent',
      uptimeStr({uptime:0},{uptime_s:45})==='0s' || uptimeStr({uptime:0},{uptime_s:45})==='45s',
      'either is defensible; must not be "--"');

console.log('');
console.log(fails?(fails+' FAILED'):'ALL PASS — units respected, absence still renders --');
process.exit(fails?1:0);
