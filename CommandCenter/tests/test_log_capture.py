"""Bot stdout must actually reach its log file.

Python block-buffers stdout (~8KB) whenever it is a file rather than a
console, and nothing flushes it for a long-lived process. The fleet launcher
passed an open file as `stdout` with no PYTHONUNBUFFERED and no `-u`, so quiet
bots wrote the launcher's banner and then nothing at all.

Observed 2026-08-06: 11 of 18 bots had logs frozen at the launch line for
8.3 hours while every one of them was alive and scanning normally (aegis
scan_count=996, nexus=1591, phitex=485). Chatty bots looked healthy only
because they produced enough output to force a flush.

The real damage is diagnostic. Every "no errors in the log" check against
those files was reading a log the process was not writing to — the same
failure shape as the Command Center logging bug fixed the day before.

This test spawns a real child through the launcher's exact wiring and asserts
the output arrives, so a grep-only check cannot pass it.
"""
import os
import subprocess
import sys
import tempfile
import time

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ── 1. The launcher must set PYTHONUNBUFFERED on the child env ──
src = open("D:/CommandCenter/launch_fleet.py", encoding="utf-8", errors="replace").read()
check('_env["PYTHONUNBUFFERED"] = "1"' in src,
      "launch_fleet.py must set PYTHONUNBUFFERED for spawned bots")
check("env=_env," in src,
      "the prepared env must actually be passed to subprocess.Popen — "
      "building it and not passing it changes nothing")

# ── 2. Behavioural proof, using the launcher's exact wiring ──
tmpdir = tempfile.mkdtemp(prefix="logcap_")
child = os.path.join(tmpdir, "chatty.py")
with open(child, "w", encoding="utf-8") as fh:
    fh.write("import time\n"
             "for i in range(4):\n"
             "    print('[INFO] scan %d' % i)\n"
             "    time.sleep(0.4)\n"
             "time.sleep(30)\n")


def spawn_and_count(env_extra):
    """Spawn exactly as launch_fleet does; return lines captured after 5s."""
    log = os.path.join(tmpdir, "out_%d.log" % len(env_extra))
    fh = open(log, "a", encoding="utf-8", buffering=1)
    fh.write("=== LAUNCH banner ===\n")
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)
    env.update(env_extra)
    p = subprocess.Popen([sys.executable, child], stdout=fh,
                         stderr=subprocess.STDOUT, env=env)
    try:
        time.sleep(5)   # child finished printing ~3s ago
        fh.flush()
        body = open(log, encoding="utf-8", errors="replace").read()
        return len([l for l in body.splitlines() if l.startswith("[INFO]")])
    finally:
        p.kill()
        p.wait(timeout=10)
        fh.close()


got = spawn_and_count({"PYTHONUNBUFFERED": "1"})
check(got == 4,
      "with PYTHONUNBUFFERED the child's 4 printed lines must reach the log "
      "file within 5s, captured %d" % got)

# Control: without it, the lines are still stuck in the buffer. This asserts
# the mechanism is real, so the test above is meaningful rather than vacuous.
# Not asserted as ==0: a future Python could change buffering policy, and this
# test must fail loudly on a REGRESSION, not on an improvement upstream.
unbuf = spawn_and_count({})
if unbuf == 4:
    print("note  unbuffered control also captured 4 lines — platform stdout "
          "buffering may have changed; the guard above still holds")

if FAIL:
    for f in FAIL:
        print("FAIL  " + f)
    sys.exit(1)
print("ok  spawned bot stdout reaches its log file (%d/4 lines, control %d/4)"
      % (got, unbuf))
