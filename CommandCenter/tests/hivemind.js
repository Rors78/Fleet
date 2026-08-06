function fmtNum(v,d){ return (v==null||isNaN(v))?'--':Number(v).toFixed(d); }
function fmt$(v){ if(v==null) return '--';
  var n=Number(v); return '$'+n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); }
var fails=0;
function check(n,c,d){ console.log((c?'  PASS  ':'  FAIL  ')+n+(d?'  — '+d:'')); if(!c)fails++; }

function alloc(state, weight){
  var _hmPool=(state.portfolio&&typeof state.portfolio.total==='number'&&state.portfolio.total>0)
    ? state.portfolio.total : null;
  var wDollar=(_hmPool!=null)?fmt$(_hmPool*weight):null;
  var wPct=fmtNum(weight*100,1);
  return wPct+'%'+(wDollar!=null?' '+wDollar:'');
}

console.log('=== CASE 1: live pool, a 37% weight ===');
var real=alloc({portfolio:{total:1000034.9016}},0.37);
console.log('     '+real);
check('uses the real pool', /370,012/.test(real), real);
var old='$'+(10000*0.37).toFixed(2);
console.log('     old hardcoded rendering: 37.0% '+old);
check('old figure was ~100x understated', Math.abs((1000034.9016*0.37)/(10000*0.37)-100)<0.01);

console.log('');
console.log('=== CASE 2: pool unavailable ===');
var none=alloc({},0.37);
console.log('     '+none);
check('shows the weight alone', none==='37.0%', none);
check('no invented dollar figure', !/\$/.test(none),
      'a dollar amount from an unknown pool is a fabrication');

console.log('');
console.log('=== CASE 3: degenerate pool values ===');
check('zero pool suppresses dollars', alloc({portfolio:{total:0}},0.5)==='50.0%');
check('null pool suppresses dollars', alloc({portfolio:{total:null}},0.5)==='50.0%');
check('string pool suppresses dollars', alloc({portfolio:{total:'1000000'}},0.5)==='50.0%',
      'a string would have multiplied to NaN');

console.log('');
console.log(fails?(fails+' FAILED'):'ALL PASS — allocation dollars come from the real pool or not at all');
process.exit(fails?1:0);
