/* When the dollar mean and the size-normalized mean disagree in SIGN,
 * showing only dollars publishes a size-weighting artifact as the edge.
 *
 * Live render (2026-08-13): the panel read "-$19.28 / trade" over 16
 * trades while expectancy_pct for those same 16 was +0.28%/trade. The
 * cause is visible per-bot: gridzilla's 12 winners were $22-$163 each,
 * confluence's worst single loser was -$1,040.51. Averaging DOLLARS
 * across positions whose notionals differ by multiples lets one large
 * loser outweigh a dozen smaller winners.
 *
 * The API has published expectancy_pct since the verdict work; the
 * dashboard just never rendered it. This pins the disclosure.
 *
 * Extracts and EXECUTES the shipped subline block — no reimplementation.
 */
var fail = 0;
function check(cond, msg) { if (!cond) { console.log("FAIL  " + msg); fail++; } }

var fs = require('fs');
var HTML = fs.readFileSync('D:/CommandCenter/command_center_v4.html', 'utf8');

var m = HTML.match(/var _pct = ex\.expectancy_pct[\s\S]*?subEl\.innerHTML = \(trades > 0[^\n]*\n/);
check(m !== null, 'could not locate the expectancy subline block in the shipped HTML');

if (m) {
  function render(ex, exp, expKnown, trades) {
    var subEl = { innerHTML: '' };
    var _cov = '';
    eval(m[0]);
    return subEl.innerHTML;
  }

  /* The live case: signs disagree -> the correction must be on screen. */
  var out = render({ expectancy_pct: 0.2789, expectancy_pct_n: 16 },
                   -19.28, true, 16);
  check(out.indexOf('size-normalized') !== -1 && out.indexOf('+0.28%') !== -1,
        'a sign disagreement must publish the size-normalized figure; got: ' + out);
  check(out.indexOf('n=16') !== -1,
        'the normalized n must ship with the figure; got: ' + out);
  check(out.indexOf('16 trades recorded') !== -1,
        'the trade count must survive the addition; got: ' + out);

  /* Agreement -> no second figure. The panel must not grow noise when
     both measures say the same thing. */
  var out2 = render({ expectancy_pct: 0.31, expectancy_pct_n: 16 },
                    45.10, true, 16);
  check(out2.indexOf('size-normalized') === -1,
        'when the signs AGREE the panel must stay quiet; got: ' + out2);

  /* Absent normalization -> no fabricated figure, no crash. */
  var out3 = render({}, -19.28, true, 16);
  check(out3.indexOf('size-normalized') === -1 && out3.indexOf('NaN') === -1,
        'an absent expectancy_pct must render nothing extra; got: ' + out3);

  /* n=0 is not a measurement even if a pct value is present. */
  var out4 = render({ expectancy_pct: 0.5, expectancy_pct_n: 0 },
                    -19.28, true, 16);
  check(out4.indexOf('size-normalized') === -1,
        'a normalized mean over ZERO sizeable trades must not be shown; got: ' + out4);

  /* Nothing measured at all stays honest. */
  var out5 = render({ expectancy_pct: 0.28, expectancy_pct_n: 16 },
                    null, false, 0);
  check(out5.indexOf('no trades recorded') !== -1
        && out5.indexOf('size-normalized') === -1,
        'an unmeasured fleet must read "no trades recorded" alone; got: ' + out5);
}

if (fail) { console.log(fail + " FAILED"); process.exit(1); }
console.log("ok  a sign disagreement between dollar and size-normalized means is disclosed");
