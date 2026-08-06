var fails=0;
function check(n,c,d){ console.log((c?'  PASS  ':'  FAIL  ')+n+(d?'  — '+d:'')); if(!c)fails++; }

console.log('=== the JS trap behind both verdicts ===');
check('null <= 20 is TRUE', (null<=20)===true, 'so a missing feed matched the FIRST branch');
check('null <= 45 is TRUE', (null<=45)===true);

// Contrarian, exactly as it now ships.
function ctOutlook(raw){
  var scKnown=(typeof raw.sentiment_score==="number"&&!isNaN(raw.sentiment_score))
            ||(typeof raw.fear_greed==="number"&&!isNaN(raw.fear_greed));
  var sc=scKnown?(typeof raw.sentiment_score==="number"?raw.sentiment_score:raw.fear_greed):null;
  var o='';
  if(!scKnown){o='Sentiment feed unavailable — no contrarian read';}
  else if(sc<=20){o='CONTRARIAN OUTLOOK: BULLISH — Extreme fear detected, reversal probability elevated';}
  else if(sc<=40){o='Contrarian leaning BULLISH — Crowd fearful, opportunity may be forming';}
  else if(sc>=80){o='CONTRARIAN OUTLOOK: BEARISH — Extreme greed detected, correction risk high';}
  else if(sc>=60){o='Contrarian leaning BEARISH — Crowd euphoric, caution warranted';}
  else{o='No extreme detected — monitoring for divergence';}
  return {sc:sc, outlook:o, blocker:(scKnown&&(sc<=20||sc>=80))};
}
console.log('');
console.log('=== CONTRARIAN ===');
var a=ctOutlook({});
console.log('     no feed -> "'+a.outlook+'"');
check('no fabricated BUY signal', !/BULLISH/.test(a.outlook),
      'used to assert bold "Extreme fear detected" from nothing');
check('no trade-blocker panel', a.blocker===false);
var b=ctOutlook({sentiment_score:12});
console.log('     real 12 -> "'+b.outlook+'"');
check('a REAL extreme still fires', /CONTRARIAN OUTLOOK: BULLISH/.test(b.outlook),
      'the signal must survive; only the fabrication is removed');
check('and still blocks', b.blocker===true);
var c=ctOutlook({fear_greed:50});
check('a real neutral reads neutral', /No extreme detected/.test(c.outlook));

// Oracle, exactly as it now ships.
function orVerdict(raw, bearPct, ranked){
  var fgKnown=typeof raw.fear_greed==="number"&&!isNaN(raw.fear_greed);
  var fg=fgKnown?raw.fear_greed:null;
  var label=!fgKnown?"--":fg<=10?"EXTREME FEAR":fg<=25?"FEAR":fg<=45?"BEARISH":fg<=55?"NEUTRAL":fg<=75?"BULLISH":fg<=90?"GREED":"EXTREME GREED";
  var v;
  if(!fgKnown) v='Sentiment feed unavailable — no Fear/Greed reading';
  else if(fg<=15&&bearPct>=75) v='Extreme fear + '+bearPct+'% bearish — capitulation or contrarian opportunity';
  else if(fg<=25&&bearPct>=60) v='High fear + bearish technicals — caution, watch for reversal signals';
  else if(fg>=80&&bearPct<=30) v='Extreme greed + bullish technicals — momentum or blow-off top risk';
  else if(fg<=45) v='Bearish sentiment';
  else if(fg>=55) v='Bullish sentiment';
  else v='Neutral — mixed signals';
  return {label:label, verdict:v};
}
console.log('');
console.log('=== ORACLE ===');
var d=orVerdict({},50,[]);
console.log('     no feed -> label="'+d.label+'" verdict="'+d.verdict+'"');
check('label is --, not EXTREME FEAR', d.label==='--', 'a 0 landed in the fg<=10 red bucket');
check('no fabricated bearish verdict', !/Bearish sentiment/.test(d.verdict));
var e=orVerdict({fear_greed:25},50,[]);
console.log('     real 25 -> label="'+e.label+'" verdict="'+e.verdict+'"');
check('live oracle value renders', e.label==='FEAR');
var f=orVerdict({fear_greed:8},80,[]);
check('a REAL extreme fear still fires', /capitulation/.test(f.verdict));
console.log('');
console.log(fails?(fails+' FAILED'):'ALL PASS — absence says so; real extremes still fire');
process.exit(fails?1:0);
