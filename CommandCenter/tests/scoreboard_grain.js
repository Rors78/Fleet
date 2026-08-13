/* The scoreboard's Trd and WR% cells must come from the same source.
 *
 * Live render (2026-08-13, fresh boot): TurtleSue read "0 trades, 33% WR".
 * The trade count came from the expectancy store (0 rows after repair); the
 * win rate fell back PER-FIELD to the bot's own lifetime snapshot (33%).
 * Two measurement grains in one row — a rate with no trades behind it.
 *
 * The rule: if the store has an entry for the bot, BOTH cells are the
 * store's (win_rate None renders as a dash). Only a bot absent from the
 * store entirely falls back to its own snapshot, for both cells together.
 *
 * This test extracts and EXECUTES the shipped lines from
 * command_center_v4.html — it does not reimplement them.
 */
var fail = 0;
function check(cond, msg) { if (!cond) { console.log("FAIL  " + msg); fail++; } }

var fs = require('fs');
var HTML = fs.readFileSync('D:/CommandCenter/command_center_v4.html', 'utf8');

/* --- PIN: the per-field mixed-source fallback must be gone --- */
check(HTML.indexOf('bex && bex.win_rate != null ? bex.win_rate : (n.win_rate') === -1,
      'the per-field fallback (store trades + snapshot WR in one row) is back');

/* --- Extract the shipped block and run it --- */
var m = HTML.match(/var trades, wr;[\s\S]*?wr = wr \* 100;/);
check(m !== null, 'could not locate the trades/wr source block in the shipped HTML');

if (m) {
  function run(bex, n) {
    var trades, wr;
    eval(m[0]);
    return { trades: trades, wr: wr };
  }

  /* TurtleSue after repair: store entry exists, nothing measured. */
  var r1 = run({ total_trades: 0, win_rate: null }, { total_trades: 3, win_rate: 33.3 });
  check(r1.trades === 0 && r1.wr == null,
        'store entry with 0 trades must show 0 trades AND no WR — got trades=' +
        r1.trades + ' wr=' + r1.wr + ' (the 33% came from the snapshot grain)');

  /* Bot absent from the store: both cells from the snapshot, together. */
  var r2 = run(null, { total_trades: 3, win_rate: 33.3 });
  check(r2.trades === 3 && r2.wr === 33.3,
        'a bot absent from the store must fall back for BOTH cells — got trades=' +
        r2.trades + ' wr=' + r2.wr);

  /* A measured store entry renders its own rate. */
  var r3 = run({ total_trades: 12, win_rate: 100.0 }, { total_trades: 99, win_rate: 1 });
  check(r3.trades === 12 && r3.wr === 100.0,
        'a measured store entry must use store values — got trades=' +
        r3.trades + ' wr=' + r3.wr);

  /* Absent everywhere stays absent. */
  var r4 = run(null, {});
  check(r4.trades === null && r4.wr == null,
        'no data anywhere must stay dashes — got trades=' + r4.trades + ' wr=' + r4.wr);
}

if (fail) { console.log(fail + " FAILED"); process.exit(1); }
console.log("ok  scoreboard Trd and WR% come from one grain");
