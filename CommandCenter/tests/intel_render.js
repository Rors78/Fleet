/* An unscored pair must not render as a measured, approved one.
 *
 * The dashboard used `s.risk_multiplier||1` and `summary.avg_risk_multiplier||1`.
 * fleet_intel_score deliberately returns null for a pair no engine has scored,
 * so `||1` turned that back into "1.000" painted GREEN — the colour for
 * full-size-approved — undoing the API-side fix at the display layer.
 *
 * `||` is doubly wrong here: it also swallows a genuine 0, which is a TOTAL
 * RISK VETO and the single most important value the table can show.
 *
 * Mirrors the shipped expressions from command_center_v4.html.
 */
var fail = 0;
function check(cond, msg) { if (!cond) { console.log("FAIL  " + msg); fail++; } }

/* --- PIN THE SHIPPED SOURCE ---------------------------------------------
 * This file used to only REIMPLEMENT the dashboard expressions below, which
 * meant it asserted its own copy and nothing else. Proven on 2026-08-07: with
 * `var rmRaw=s.risk_multiplier||1;` restored in command_center_v4.html, this
 * test still printed "ok" and exited 0. The bug it exists to guard was back
 * and the guard never noticed.
 *
 * So before exercising any logic, read the real file and require the guard
 * expressions to actually be present in it. A negative check ("the bug string
 * is absent") only forbids one spelling; these are POSITIVE checks that pin
 * the fix itself.
 */
var fs = require('fs');
var HTML = fs.readFileSync('D:/CommandCenter/command_center_v4.html', 'utf8');

var REQUIRED = [
  ['risk_multiplier guard',
   'var rmKnown=(typeof rmRaw===\'number\'&&isFinite(rmRaw));'],
  ['avg_risk_multiplier guard',
   'var avgRknown=(typeof avgRraw===\'number\'&&isFinite(avgRraw));'],
  ['pool total guard',
   "var total = (typeof p.total==='number'&&isFinite(p.total)&&p.total>0)?p.total:null;"],
  ['unknown deployment reads UNKNOWN',
   'riskLabel="UNKNOWN"'],
  ['whale score null-guard (null.toFixed throws)',
   "(item.score!=null?item.score.toFixed(2):'&mdash;')"],
  ['HUD pnl guard (unreported P/L rendered "+0.00" in gain-teal)',
   'var pnl=(typeof agg.total_pnl==="number"&&isFinite(agg.total_pnl))?agg.total_pnl:null;'],
];
REQUIRED.forEach(function (r) {
  check(HTML.indexOf(r[1]) !== -1,
        'command_center_v4.html no longer contains the ' + r[0] +
        ' — the shipped file regressed even if the logic below still passes');
});

/* The banned shapes, checked against the real file rather than assumed. */
check(HTML.indexOf('s.risk_multiplier||1') === -1,
      'the `s.risk_multiplier||1` fabrication is back in the shipped dashboard');
check(HTML.indexOf('p.total || 10000') === -1 &&
      HTML.indexOf('p.total||10000') === -1,
      'the `p.total||10000` fabricated portfolio is back in the shipped dashboard');

/* --- behavioural checks, mirroring the pinned expressions --- */
function known(v) { return typeof v === 'number' && isFinite(v); }

function rowRisk(s) {
  var rmRaw = s.risk_multiplier;
  var rmKnown = known(rmRaw);
  var rm = rmKnown ? rmRaw : null;
  var rc = !rmKnown ? "rgba(255,255,255,0.35)"
         : rm > 0.7 ? "#00ff88" : rm > 0.4 ? "#fbbf24" : "#ff4466";
  return { text: rmKnown ? rm.toFixed(3) : "&mdash;", color: rc };
}

function avgRisk(summary) {
  var raw = summary.avg_risk_multiplier;
  var k = known(raw);
  return { text: k ? raw.toFixed(3) : "&mdash;",
           green: k && raw > 0.7 };
}

/* --- an unscored pair must look unscored --- */
var un = rowRisk({ risk_multiplier: null });
check(un.text === "&mdash;",
      "unscored pair must render a dash, got " + un.text);
check(un.color !== "#00ff88",
      "unscored pair must NOT be painted green — that reads as full size approved");

var undef = rowRisk({});
check(undef.text === "&mdash;", "missing key must render a dash, got " + undef.text);

/* --- a measured value must still render, including the extremes --- */
var veto = rowRisk({ risk_multiplier: 0 });
check(veto.text === "0.000",
      "a measured 0 is a TOTAL RISK VETO and must render as 0.000, got " + veto.text);
check(veto.color === "#ff4466",
      "a measured 0 must be painted red, got " + veto.color);

var ok = rowRisk({ risk_multiplier: 1.4 });
check(ok.text === "1.400", "a measured 1.4 must render, got " + ok.text);
check(ok.color === "#00ff88", "a measured 1.4 must be green");

var mid = rowRisk({ risk_multiplier: 0.5 });
check(mid.color === "#fbbf24", "a measured 0.5 must be amber, got " + mid.color);

/* --- fleet average --- */
var noneAvg = avgRisk({ avg_risk_multiplier: null });
check(noneAvg.text === "&mdash;",
      "an unmeasured fleet average must render a dash, got " + noneAvg.text);
check(noneAvg.green === false,
      "an unmeasured fleet average must not be green — with zero pairs scored " +
      "the old ||1 read as fleet-wide full size approved");

var realAvg = avgRisk({ avg_risk_multiplier: 0.8 });
check(realAvg.text === "0.800", "a real average must render, got " + realAvg.text);
check(realAvg.green === true, "a real 0.8 average must be green");

/* --- pool total must never be invented --- */
function poolTotal(p) {
  return (typeof p.total === 'number' && isFinite(p.total) && p.total > 0) ? p.total : null;
}
check(poolTotal({}) === null,
      "an absent pool total must be null, not 10000 — the live pool is ~$1M, " +
      "so a fabricated 10000 misstates every percentage by ~100x");
check(poolTotal({ total: 1000540.84 }) === 1000540.84, "a real pool total must pass through");
check(poolTotal({ total: 0 }) === null, "a zero pool total is not a usable denominator");

/* --- unknown deployment must not read as OK --- */
function riskLabel(depPct) {
  var n = (depPct === null) ? null : parseFloat(depPct);
  if (n === null || !isFinite(n)) return "UNKNOWN";
  if (n >= 70) return "HIGH RISK";
  if (n >= 50) return "MODERATE";
  return "OK";
}
check(riskLabel(null) === "UNKNOWN",
      "unknown deployment must read UNKNOWN — parseFloat(null) is NaN and " +
      "every NaN comparison is false, so it used to fall through to green OK");
check(riskLabel("85.0") === "HIGH RISK", "85% deployed must read HIGH RISK");
check(riskLabel("10.0") === "OK", "10% deployed must read OK");

/* --- a whale with no score gets no derived tier --- */
function deriveTier(p) {
  var sc = [p.score, p.whale_score, p.intensity].filter(function (v) { return known(v); });
  var score = sc.length ? sc[0] : null;
  return p.tier || (score === null ? null : (score > 0.8 ? "EXTREME" : score > 0.5 ? "HIGH" : "MEDIUM"));
}
check(deriveTier({}) === null,
      "an unscored whale must get no tier — the old chain fell to score 0, " +
      "which fails both thresholds and was classified MEDIUM");
check(deriveTier({ score: 0.9 }) === "EXTREME", "a measured 0.9 must derive EXTREME");
check(deriveTier({ score: 0.1 }) === "MEDIUM", "a measured 0.1 must derive MEDIUM");
check(deriveTier({ tier: "HIGH" }) === "HIGH", "an explicit tier must win");

if (fail) { console.log(fail + " failure(s)"); process.exit(1); }
console.log("ok  unscored intel renders as unscored; measured extremes survive");
