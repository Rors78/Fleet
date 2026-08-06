var _tradeCacheLoaded = true, _TRADES = {};
function getBotTrades(id){ return _TRADES[id] || []; }
function fmtNum(v,d){ return (v==null||isNaN(v))?'--':Number(v).toFixed(d); }
function fmt$(v){ return v==null?'--':'$'+Number(v).toFixed(2); }
function getDurableTradeStats(botId, botStats) {
  var trades = getBotTrades(botId);
  botStats = botStats || {};
  if (!_tradeCacheLoaded || !trades.length) {
    /* Nothing durable yet — pass the bot's own numbers through untouched.
       `|| 0` used to turn every unmeasured field into a measured zero before
       any formatter could guard it, which is how six trader tabs rendered
       "0.0% win rate" in red for bots that had never traded. Pass null
       through instead; fmtNum/fmt$/fmtPct already print "--" for null. */
    var _n = function (v) { return (typeof v === 'number' && !isNaN(v)) ? v : null; };
    var _tot = _n(botStats.total);
    return {
      total:        _tot === null ? 0 : _tot,
      win_rate:     _tot ? _n(botStats.win_rate)      : null,
      profit_factor:_tot ? _n(botStats.profit_factor) : null,
      avg_win:      _tot ? _n(botStats.avg_win)       : null,
      avg_loss:     _tot ? _n(botStats.avg_loss)      : null,
      total_pnl:    _n(botStats.total_pnl),
      source:       _tradeCacheLoaded ? 'bot' : 'loading'
    };
  }
  var wins = 0, losses = 0, gross_win = 0, gross_loss = 0, total_pnl = 0;
  for (var i = 0; i < trades.length; i++) {
    var p = Number(trades[i].pnl) || 0;
    total_pnl += p;
    /* Zero-PnL closes (scratch exits) count as trades but as neither a win
       nor a loss — folding them into losses would understate win rate. */
    if (p > 0)      { wins++;   gross_win  += p; }
    else if (p < 0) { losses++; gross_loss += -p; }
  }
  var decided = wins + losses;
  return {
    total:         trades.length,
    /* null, not 0, when nothing was decided — every close was a scratch exit,
       so there is no win rate to report. A 0 here reads as "lost them all". */
    win_rate:      decided > 0 ? (wins / decided) : null,   /* 0-1, as panels expect */
    profit_factor: gross_loss > 0 ? (gross_win / gross_loss)
                                  : (gross_win > 0 ? Infinity : null),
    avg_win:       wins   > 0 ? (gross_win  / wins)   : null,
    avg_loss:      losses > 0 ? (gross_loss / losses) : null,
    total_pnl:     total_pnl,
    wins:          wins,
    losses:        losses,
    source:        'log'
  };
}

function amnesiaGuard(botId, rawEq, rawPnl, rawDd, startEq, botStats) {
  var stats = getDurableTradeStats(botId, botStats || {});
  /* Preserve null: a bot that reports no pnl has not reported a zero pnl.
     The amnesia arithmetic below needs numbers, so it uses the coerced
     locals, but the returned values keep the distinction for the caller. */
  var _eqKnown = typeof rawEq === 'number' && !isNaN(rawEq);
  var _pnlKnown = typeof rawPnl === 'number' && !isNaN(rawPnl);
  var _ddKnown = typeof rawDd === 'number' && !isNaN(rawDd);
  var eq = _eqKnown ? rawEq : null, pnl = _pnlKnown ? rawPnl : null,
      dd = _ddKnown ? rawDd : null;
  var start = startEq || 10000;
  /* Math.abs(null) is 0, so an UNREPORTED pnl used to satisfy the
     "bot claims flat" test as readily as a genuine 0.00. Require the bot to
     have actually reported a flat figure before overriding it. */
  var amnesia = (stats.source === 'log' && stats.total > 0 && _pnlKnown &&
                 Math.abs(pnl) < 0.005 &&
                 typeof stats.total_pnl === 'number' &&
                 Math.abs(stats.total_pnl) >= 0.01);
  if (amnesia) {
    pnl = stats.total_pnl;
    eq  = start + pnl;
    /* Drawdown from starting capital — a floor, not true peak-to-trough, but
       strictly better than the 0.0% a restarted bot reports. */
    dd  = pnl < 0 ? Math.abs(pnl) / start * 100 : 0;
  }
  return {eq: eq, pnl: pnl, dd: dd, amnesia: amnesia, stats: stats};
}
var fails=0;
function check(n,c,d){ console.log((c?'  PASS  ':'  FAIL  ')+n+(d?'  — '+d:'')); if(!c)fails++; }

console.log('=== CASE 1: bot has never traded ===');
_TRADES={}; var r=getDurableTradeStats('rubberband',{total:0,win_rate:0,profit_factor:0});
console.log('     win_rate='+r.win_rate+'  pf='+r.profit_factor+'  renders "'+fmtNum(r.win_rate,1)+'"');
check('win_rate is null not 0', r.win_rate===null, 'was 0 -> rendered "0.0%" in red');
check('profit_factor is null', r.profit_factor===null);
check('formatter prints --', fmtNum(r.win_rate,1)==='--');

console.log('');
console.log('=== CASE 2: real decided trades still measured ===');
_TRADES={turtlesue:[{pnl:50},{pnl:-20},{pnl:30},{pnl:-10}]};
var r2=getDurableTradeStats('turtlesue',{});
console.log('     total='+r2.total+' wins='+r2.wins+' losses='+r2.losses+' wr='+r2.win_rate);
check('win rate measured', Math.abs(r2.win_rate-0.5)<1e-9, r2.win_rate);
check('renders 50.0%', fmtNum(r2.win_rate*100,1)==='50.0');
check('profit factor real', Math.abs(r2.profit_factor-(80/30))<1e-9);

console.log('');
console.log('=== CASE 3: a REAL 0% must survive (all losers) ===');
_TRADES={gridzilla:[{pnl:-5},{pnl:-8}]};
var r3=getDurableTradeStats('gridzilla',{});
check('0% preserved on real losses', r3.win_rate===0, 'suppressing this would hide a losing streak');
check('renders 0.0 not --', fmtNum(r3.win_rate*100,1)==='0.0');

console.log('');
console.log('=== CASE 4: trades exist but none decided (all scratch) ===');
_TRADES={arbitrageur:[{pnl:0},{pnl:0}]};
var r4=getDurableTradeStats('arbitrageur',{});
console.log('     total='+r4.total+' win_rate='+r4.win_rate);
check('total counts scratches', r4.total===2);
check('win_rate null, not 0', r4.win_rate===null, '0 of 0 decided is not a 0% win rate');

console.log('');
console.log('=== CASE 5: amnesiaGuard must not fire on an UNREPORTED pnl ===');
_TRADES={confluence:[{pnl:-14.89}]};
var g=amnesiaGuard('confluence',null,null,null,10000,{});
check('no amnesia claim when pnl unreported', g.amnesia===false, 'Math.abs(null)===0 used to satisfy "claims flat"');
check('pnl stays null', g.pnl===null);
var g2=amnesiaGuard('confluence',10000,0,0,10000,{});
check('amnesia DOES fire on a real flat report', g2.amnesia===true, 'bot says 0.00, log says -14.89');
check('and swaps in the durable pnl', Math.abs(g2.pnl+14.89)<1e-9, g2.pnl);

console.log('');
console.log(fails?(fails+' FAILED'):'ALL PASS — unmeasured is null, measured zeros survive');
process.exit(fails?1:0);
