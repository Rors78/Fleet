/* The dashboard kept its own copy of the per-bot fake balance.
 *
 * After purging a hardcoded $10,000 from five bots, the dashboard still
 * carried TEN of them:
 *
 *   6 x amnesiaGuard(..., 10000, ...)   -- one per bot panel, two passing the
 *                                          bare literal with no fallback chain
 *   var equity = perf.final_equity || perf.initial_capital || 10000
 *   var eq     = raw.equity || 10000
 *   var startEq= raw.starting_equity || 10000
 *   fmt$(cfg.default_equity || 10000)
 *
 * Against the real ~$210 pool a fabricated $10,000 is ~48x. Worse,
 * amnesiaGuard used it as a DENOMINATOR:
 *
 *   dd = Math.abs(pnl) / start * 100
 *
 * so a $50 loss rendered as 0.5% drawdown when against the actual pool it is
 * nearly 24% -- the same fake-denominator defect that was fixed in
 * TurtleSue's drawdown brake, living on in the display layer.
 *
 * This file already carried THREE comments recording that this exact bug had
 * been found and fixed in other spots ("Was ||10000 -- a fabricated pool
 * baseline", "total is NOT 10000 when absent ... misstates by ~100x", "The
 * pool total is NOT 10000 when absent"). It kept growing back, because each
 * fix targeted one site.
 *
 * So this test bans the shape, not the instance.
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'command_center_v4.html');
const src = fs.readFileSync(SRC, 'utf8');

const fails = [];
function check(cond, msg) { if (!cond) fails.push(msg); }

/* Strip comments so the prose explaining the removal does not trip the
 * check that removed it. Block comments only -- line comments in this file
 * are rare and // inside string literals would break naive stripping. */
const code = src.replace(/\/\*[\s\S]*?\*\//g, '');

/* 1. No balance-shaped literal may be used as a fallback for a money value.
 *    Matches `|| 10000`, `||10_000`, `|| 100000` on equity/balance/capital/
 *    pool identifiers. */
/*  before the money word: a bare `eq` alternative matched hiFreq
 * (an audio frequency in Hz) and flagged correct code. */
const FALLBACK = /\b(\w*(?:equity|balance|capital)\w*|eq|pool)\s*\|\|\s*(\d{4,})/gi;
let m;
while ((m = FALLBACK.exec(code)) !== null) {
  check(false,
    'fabricated balance fallback: `' + m[0] + '`. No bot holds capital of ' +
    'its own -- there is ONE pool (~$210), so a hardcoded ' + m[2] + ' is ' +
    'off by ~' + Math.round(Number(m[2]) / 210) + 'x. Render unknown as ' +
    '"--", never as a plausible number.');
}

/* 2. amnesiaGuard must not be handed a hardcoded starting balance. Its 5th
 *    argument is the start-equity slot; every caller used to pass 10000. */
const GUARD_CALL = /amnesiaGuard\s*\(([^)]*)\)/g;
while ((m = GUARD_CALL.exec(code)) !== null) {
  const args = m[1].split(',');
  if (args.length < 5) continue;
  const startArg = args[4].trim();
  check(!/^\d{3,}$/.test(startArg) && !/\|\|\s*\d{3,}/.test(startArg),
    'amnesiaGuard called with a hardcoded start equity `' + startArg +
    '`: ' + m[0].slice(0, 70) + '. Bot equity is a zero-based realized-P/L ' +
    'ledger; there is no starting capital to add, and using one as a ' +
    'drawdown denominator understates the loss by ~48x.');
}

/* 3. A drawdown percentage must not divide by a start-equity variable.
 *    The pool is the only legitimate denominator. */
const DD_DIV = /\/\s*start\s*\*\s*100/g;
while ((m = DD_DIV.exec(code)) !== null) {
  check(false,
    'drawdown divides by a start-equity variable (`' + m[0].trim() + '`). ' +
    'The denominator must be the live pool -- the only capital there is.');
}

/* 4. paper_balance is gone fleet-wide; reading it re-creates the fiction. */
check(!/raw\.paper_balance|\.paper_balance\s*\|\|/.test(code),
  'dashboard still reads a paper_balance field. No bot publishes one: ' +
  'balances were removed fleet-wide in favour of the single pool.');

if (fails.length) {
  fails.forEach(f => console.log('FAIL  ' + f));
  process.exit(1);
}
console.log('ok  dashboard fabricates no balances -- one pool, rendered honestly');
