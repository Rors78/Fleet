// Reproduce the guard expressions exactly as they now appear in buildAegisView.
function fmtNum(v,d){ return (v==null||isNaN(v))?'--':Number(v).toFixed(d); }
function fmt$(v){ return v==null?'--':'$'+Number(v).toFixed(2); }
function render(raw, port){
  var score = raw.score!=null?+raw.score:null;
  var scoreKnown = score!=null;
  var scoreN = scoreKnown?score:0;
  var maxDeploy = raw.recommended_max_deployed!=null?raw.recommended_max_deployed:"--";
  var throttleKnown = typeof maxDeploy==="number";
  var throttlePct = throttleKnown?maxDeploy:null;
  var _portKnown = typeof port.total==='number' && port.total>0;
  var _portTotal = _portKnown?port.total:0;
  var _aegisCap = (throttleKnown&&_portKnown)?_portTotal*(throttlePct/100):null;
  var _portDeployed = port.deployed||0;
  var tier = scoreN>=0.8?3:scoreN>=0.5?2:scoreN>=0.2?1:0;
  return {
    scoreText: scoreKnown?fmtNum(scoreN,4):'--',
    tierShown: scoreKnown?tier:null,
    capText: _aegisCap!=null?fmt$(_aegisCap):'--',
    breach: _aegisCap!=null && _portDeployed>_aegisCap,
    buffer: _aegisCap!=null?fmt$(Math.max(0,_aegisCap-_portDeployed)):'--',
  };
}
var fails=0;
function check(n,c,d){ console.log((c?'  PASS  ':'  FAIL  ')+n+(d?'  — '+d:'')); if(!c)fails++; }

console.log('=== CASE 1: AEGIS data missing entirely ===');
var r = render({}, {});
console.log('     score='+r.scoreText+'  cap='+r.capText+'  buffer='+r.buffer+'  tier='+r.tierShown);
check('score shows -- not 0.0000', r.scoreText==='--', 'was "0.0000" styled DEFENSIVE-red');
check('no tier claimed', r.tierShown===null, 'a null score must not read as max danger');
check('cap shows -- not a dollar figure', r.capText==='--');
check('no fabricated breach', r.breach===false);

console.log('');
console.log('=== CASE 2: real live values still render ===');
var r2 = render({score:0.2349, recommended_max_deployed:60}, {total:1000034.90, deployed:132777.12});
console.log('     score='+r2.scoreText+'  cap='+r2.capText+'  buffer='+r2.buffer+'  tier='+r2.tierShown);
check('real score renders', r2.scoreText==='0.2349');
check('real tier computed', r2.tierShown===1);
check('cap = 60% of pool', r2.capText==='$600020.94', r2.capText);
check('no breach at 13% deployed', r2.breach===false);

console.log('');
console.log('=== CASE 3: a REAL breach must still show red ===');
var r3 = render({score:0.15, recommended_max_deployed:30}, {total:1000000, deployed:500000});
check('breach detected', r3.breach===true, 'deployed $500k > cap $300k');
check('buffer is zero, not --', r3.buffer==='$0.00');

console.log('');
console.log('=== CASE 4: score present but pool missing ===');
var r4 = render({score:0.9, recommended_max_deployed:90}, {});
check('score still shown', r4.scoreText==='0.9000');
check('cap suppressed without a pool', r4.capText==='--');
console.log('');
console.log(fails?(fails+' FAILED'):'ALL PASS — absence renders as "--", never as alarm or a fabricated dollar figure');
process.exit(fails?1:0);
