function fmtNum(v,d){ return (v==null||isNaN(v))?'--':Number(v).toFixed(d); }
var fails=0;
function check(n,c,d){ console.log((c?'  PASS  ':'  FAIL  ')+n+(d?'  — '+d:'')); if(!c)fails++; }
console.log('=== the JS coercion trap this fix closes ===');
check('null >= 0 is TRUE in JS', (null>=0)===true, 'so an unreported P/L rendered GREEN with a "+"');
check('null > 0 is false', (null>0)===false, 'inconsistent with >=, which is why it hid');

function renderNP(np,npKnown){
  return {cls:(!npKnown?'':np>=0?'good':'bad'),
          txt:(npKnown&&np>=0?'+':'')+fmtNum(np,4)};
}
console.log('');
console.log('=== NET PNL rendering ===');
var a=renderNP(null,false);
console.log('     unreported -> class="'+a.cls+'" text="'+a.txt+'"');
check('unreported: no colour class', a.cls==='');
check('unreported: no fake + sign', a.txt==='--', 'was "+--"');
var b=renderNP(34.9016,true);
console.log('     real gain  -> class="'+b.cls+'" text="'+b.txt+'"');
check('real gain still green', b.cls==='good' && b.txt==='+34.9016');
var c=renderNP(-14.89,true);
console.log('     real loss  -> class="'+c.cls+'" text="'+c.txt+'"');
check('real loss still red', c.cls==='bad' && c.txt==='-14.8900');
var d=renderNP(0,true);
check('a REAL zero stays green', d.cls==='good' && d.txt==='+0.0000', 'measured flat is not unmeasured');
console.log('');
console.log(fails?(fails+' FAILED'):'ALL PASS');
process.exit(fails?1:0);
