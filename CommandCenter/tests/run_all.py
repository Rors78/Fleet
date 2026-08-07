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
