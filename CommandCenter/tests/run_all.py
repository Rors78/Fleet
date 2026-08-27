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
    # A clamp that recomputes one quantity and reserves another is inert.
    # Turtlesue requested $600 against a $215 pool, 84 denials in 24h.
    "test_sizing_clamp_binds.py",
    # 2026-08-27 audit: ten test files existed on disk that this runner
    # never listed -- including test_gridzilla_records_losses.py, the guard
    # for the "a bot cannot record a loss" scar. A test that is not in this
    # list is not a check, however good it is.
    "test_granted_size_is_traded.py",
    "test_gridzilla_records_losses.py",
    "test_probe_predicate_single.py",
    "test_expectancy_store_absent_vs_unreadable.py",
    "test_ohlc_single_fetcher.py",
    "test_current_era_boundary.py",
    "test_min_trade_floor.py",
    "test_aggregate_traders_only.py",
    "test_confluence_exit_and_emit.py",
    "test_rubberband_timeframe_coherence.py",
    "test_gridzilla_sizes_off_pool.py",
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
    # DRIFT GUARD. On 2026-08-27 an audit found TEN test_*.py files on disk
    # that this list did not name -- among them
    # test_gridzilla_records_losses.py, the guard for the "a bot cannot
    # record a loss" scar. Each was a real, passing check that CI had never
    # once executed. A test that is not in the list is not a check, no
    # matter how well written, and nothing announced the gap.
    _on_disk = {f for f in os.listdir(HERE)
                if f.startswith("test_") and f.endswith(".py")}
    _unlisted = sorted(_on_disk - set(TESTS))
    if _unlisted:
        print("FAIL  %d test file(s) exist but are not in TESTS -- they have "
              "never run: %s" % (len(_unlisted), ", ".join(_unlisted)))
        print("      Add them to TESTS, or delete them. An unrun test is a "
              "false sense of coverage.")
        return 1

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
            # A MISSING JS test used to `continue` -- never appended to
            # results, so it vanished from the denominator with no MISS, no
            # SKIP, and no trace. Deleting a JS test made the suite GREENER,
            # which is the purest form of false green available: the count
            # goes up in confidence and down in coverage at the same time.
            # The Python loop above already records missing files as
            # failures; this now matches it.
            results.append((name, None, "missing"))
            continue
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
