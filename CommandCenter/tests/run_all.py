"""Run the broadcaster card suite.

    python tests/run_all.py

These exercise the REAL scheduled jobs (DailySummaryJob, WeeklyReportJob,
EndOfDayJob) with a capture transport standing in for ChannelOps, so nothing
is published. Some tests hit the live Command Center on :9000 and will fail
if the fleet is down — that is a fleet problem, not a card problem.

What this suite exists to catch, in one line: a card that reports a figure
nobody measured.

  test_daily    — daily summary fires with live figures and a coverage denominator
  test_weekly   — weekly scorecard against whatever is on disk right now
  test_weekly2  — weekly scorecard against fixtures: real trades, no files,
                  recorded-but-flat, and schema drift (needs no live CC)
  test_eod      — end-of-day data path returns None, not 0, when nothing traded
  test_eod2     — paid/free image tiers actually differ; public bot names are
                  unique across the fleet and consistent between modules
  test_rows     — _drop_empty_rows placeholder stripping
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = [
    "test_daily.py",
    "test_weekly.py",
    "test_weekly2.py",
    "test_eod.py",
    "test_eod2.py",
    "test_rows.py",
    "test_turtle_sizing.py",
    "test_confluence_sizing.py",
    "test_reservation_sweep.py",
    "test_size_units.py",
    "test_grid_cycle_accounting.py",
    "test_no_fabricated_stats.py",
    "test_unreadable_state.py",
    "test_state_checkpoint.py",
    "test_release_publish.py",
    "test_nonfinite_guard.py",
    "test_lease_sweep_guard.py",
    "test_harmonic_absent.py",
    "test_nexus_character_map.py",
    "test_log_capture.py",
    "test_error_visibility.py",
    "test_fusion_accuracy.py",
    "test_intel_gates.py",
    "test_signal_log_dedupe.py",
    "test_recorded_prices.py",
    "test_trade_attribution.py",
    "test_bus_listener_absence.py",
    "test_direction_restore.py",
    "test_store_repair.py",
    "test_weekly_probes.py",
    "test_daily_aggregation.py",
    "test_verdict_ci.py",
    "test_decay_wired.py",
    "test_correlation_undefined.py",
    "test_init_order.py",
    "test_synthetic_disclosure.py",
    "test_writer_guard.py",
    "test_aegis_hold.py",
    "test_emit_on_change.py",
    "test_unreadable_siblings.py",
    "test_portfolio_unreadable.py",
    "test_aegis_correlation.py",
    "test_brainiac_health.py",
    "test_aegis_seed.py",
    "test_sizing_multiplier.py",
    "test_grid_risk_gates.py",
    "test_sentinel_conviction.py",
    "test_hivemind_weight_cap.py",
    "test_universe_blacklist.py",
    "test_silent_handlers.py",
    "test_reaction_templates.py",
    "test_card_threading.py",
    "test_trade_floor.py",
    "test_weekly_era.py",
    "test_daily_source.py",
    "test_wr_windows.py",
    "test_causal_stats.py",
    "test_pool_absent.py",
    "test_sizing_fallback.py",
    "test_one_pool.py",
    "test_null_basis_guards.py",
    "test_unmeasured_averages.py",
    "test_no_live_writes.py",
    "test_component_renorm.py",
    "test_staleness_honesty.py",
    "test_fleet_wr_pooled.py",
    # 2026-08-26 display-honesty audit: a win rate rendered without its
    # denominator (gridzilla's "100.0%" in confident green off 16W/0L/17flat
    # beside "Total cycles 0"), and expectancy.py being the one major
    # consumer with no synthetic-probe filter.
    "test_win_rate_denominator.py",
    "test_expectancy_probes.py",
    # 2026-08-27: BLUR/USD proved a held position can outlive Oracle's
    # coverage of its pair, leaving stop, target and time exits all blind
    # at once. The fallback quote that fixes it must stay held-positions
    # only -- on the entry path it would silently widen the traded universe.
    "test_confluence_fallback_quote.py",
    # 2026-08-27 investor audit: the fleet WR chip absorbed gridzilla's
    # 16W/0L -- a rate expectancy.py had already marked degenerate. The
    # flag reached two display sites but never the aggregate.
    "test_degenerate_rate_excluded.py",
    # A protection that needs no price (max-age exit, drawdown kill) must
    # not be skipped by the guard that exists for price-based exits.
    # Confluence and Gridzilla both had this; four more traders still do.
    "test_price_free_exits_reachable.py",
    # The same close reached the store by two routes under different
    # trade_ids, so dedup could never fire. Fixed once on the consumer side
    # and it came back -- a consumer fix cannot help an emitter that never
    # sends the key. Both ends pinned now.
    "test_double_record_collapse.py",
]

# Dashboard-side guards. These were written alongside the Python tests but
# were never run by this runner, so a regression in the rendering layer went
# unnoticed by CI while the Python suite stayed green.
JS_TESTS = [
    "nullcmp.js",
    "ov.js",
    "tile.js",
    "aegis_guard.js",
    "units.js",
    "stale.js",
    "verdict.js",
    "hivemind.js",
    "helper_test.js",
    "intel_render.js",
    "scoreboard_grain.js",
    "expectancy_panel.js",
    "dash_no_fake_balance.js",
    "cosmos_lighting.js",
    "deepfield_coverage.js",
]


def main() -> int:
    results = []
    for name in TESTS:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            results.append((name, None, "missing"))
            continue
        proc = subprocess.run([sys.executable, path], cwd=HERE,
                              capture_output=True, text=True)
        results.append((name, proc.returncode == 0, proc))

    node = shutil.which("node")
    for name in JS_TESTS:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            continue          # optional
        if not node:
            print(f"SKIP  {name} (node not on PATH)")
            continue
        proc = subprocess.run([node, path], cwd=HERE,
                              capture_output=True, text=True)
        results.append((name, proc.returncode == 0, proc))

    width = max(len(n) for n, _, _ in results)
    failed = []
    for name, ok, proc in results:
        if ok is None:
            print(f"MISS  {name}")
            failed.append(name)
            continue
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}")
        if not ok:
            failed.append(name)
            tail = (proc.stdout or "").strip().splitlines()[-25:]
            for line in tail:
                print(f"        {line}")
            err = (proc.stderr or "").strip().splitlines()[-8:]
            for line in err:
                print(f"      ! {line}")

    print()
    print(f"suite: {len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
