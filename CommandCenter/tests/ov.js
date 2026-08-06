// Reproduce renderOvFeeAnalysis's value derivation exactly as it now ships.
function derive(ex){
  var _expRaw = ex.fleet_expectancy != null ? ex.fleet_expectancy
              : (ex.expectancy_per_trade != null ? ex.expectancy_per_trade : null);
  var trades = ex.total_trades || ex.trades || 0;
  var _wrRaw = ex.win_rate != null ? ex.win_rate
             : (ex.fleet_win_rate != null ? ex.fleet_win_rate : null);
  var expKnown = trades > 0 && typeof _expRaw === "number" && !isNaN(_expRaw);
  var wrKnownOv = trades > 0 && typeof _wrRaw === "number" && !isNaN(_wrRaw);
  var exp = expKnown ? _expRaw : null;
  var winRate = wrKnownOv ? (_wrRaw <= 1.01 ? _wrRaw * 100 : _wrRaw) : null;
  var expSign = !expKnown ? "" : (exp < 0 ? "-" : "+");
  var wrColor = !wrKnownOv ? "#475569" : (winRate>=55?"#22c55e":winRate>=45?"#f59e0b":"#ef4444");
  return {
    big: expKnown ? expSign+"$"+Math.abs(exp).toFixed(2)+" / trade" : "-- / trade",
    wrText: wrKnownOv ? Math.round(winRate)+"%" : "--",
    wrColor: wrColor,
    gauge: (wrKnownOv ? Math.min(100,Math.max(0,winRate)) : 0).toFixed(1)+"%",
    sub: trades>0 ? trades+" trades" : "no trades yet",
  };
}
var fails=0;
function check(n,c,d){ console.log((c?'  PASS  ':'  FAIL  ')+n+(d?'  — '+d:'')); if(!c)fails++; }

console.log('=== CASE 1: live fleet, nothing measured ===');
var r=derive({total_trades:0,fleet_expectancy:null,win_rate:null,participating_bots:0,fleet_members:18});
console.log('     big="'+r.big+'"  wr="'+r.wrText+'"  gauge='+r.gauge+'  sub="'+r.sub+'"');
check('expectancy shows --', r.big==="-- / trade", 'was "+$0.00 / trade" in green');
check('win rate shows --', r.wrText==="--", 'was "0%" in red');
check('gauge neutral grey', r.wrColor==="#475569", 'was #ef4444 red');
check('sub says no trades', r.sub==="no trades yet", 'was "0 trades"');

console.log('');
console.log('=== CASE 2: real measured fleet ===');
var r2=derive({total_trades:7,fleet_expectancy:-2.13,win_rate:0.2857});
console.log('     big="'+r2.big+'"  wr="'+r2.wrText+'"  gauge='+r2.gauge);
check('negative expectancy renders', r2.big==="-$2.13 / trade");
check('fraction win rate scaled', r2.wrText==="29%", r2.wrText);
check('low WR still red', r2.wrColor==="#ef4444", 'a real bad number must stay alarming');

console.log('');
console.log('=== CASE 3: a REAL 0% on real trades must stay red ===');
var r3=derive({total_trades:4,fleet_expectancy:-5.0,win_rate:0});
check('real 0% renders as 0%', r3.wrText==="0%", 'not "--" — this is a measured losing streak');
check('and stays red', r3.wrColor==="#ef4444");
console.log('');
console.log(fails?(fails+' FAILED'):'ALL PASS');
process.exit(fails?1:0);
