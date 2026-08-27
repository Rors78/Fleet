"""The trade store must tell "nothing to restore" from "could not read it".

WHY THIS EXISTS. logs/expectancy.json is the fleet's only durable record of
what it has actually traded -- 60 trades at the time this was written, and
every published expectancy, win rate and P/L figure derives from it.

Both ends of that file were `except Exception: pass`:

    _load()  a CORRUPTED store was indistinguishable from a FIRST RUN. Both
             produced {} and a normal startup. The fleet would publish a
             zeroed history as though it had never traded.

    _save()  a disk-full, permission denial, or bad path all reported
             success. Trades recorded since the last good write were lost
             and nothing said so.

This is the founding absent-vs-empty shape sitting on the most load-bearing
file in the project. The three cases -- absent / empty / unreadable -- must
produce three observable outcomes, and unreadable must NOT be treated as
empty.

The store is also left ON DISK on a failed read. Overwriting it would let
the next _save persist {} and destroy whatever was recoverable.
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
sys.path.insert(0, CC)

failures = []

try:
    from expectancy import ExpectancyTracker
except Exception as e:                                    # pragma: no cover
    print("FAIL: cannot import ExpectancyTracker: %r" % e)
    sys.exit(1)

tmpdir = tempfile.mkdtemp(prefix="exp_store_")

# Redirect the CLASS attribute, not just each instance. ExpectancyTracker
# persists on its own inside record_trade(), and logs/expectancy.json has been
# erased once already by a test that only redirected per-instance -- any
# construction this file does not explicitly own would still land on the live
# path. Enforced by test_no_live_writes.py.
ExpectancyTracker.PERSIST_PATH = os.path.join(tmpdir, "_class_default.json")


def _tracker(path):
    """A tracker pointed at a scratch path, without touching the live store."""
    t = ExpectancyTracker.__new__(ExpectancyTracker)
    t.trades = {}
    t.PERSIST_PATH = path
    t.load_failed = False
    t.load_error = None
    return t

try:
    # ---------------------------------------------------------------- absent
    p_absent = os.path.join(tmpdir, "missing", "expectancy.json")
    t = _tracker(p_absent)
    t._load()
    if t.trades != {}:
        failures.append("absent store did not yield an empty dict")
    if t.load_failed:
        failures.append("absent store set load_failed -- a first run is not "
                        "a failure, and flagging it would cry wolf forever")

    # ----------------------------------------------------------------- empty
    p_empty = os.path.join(tmpdir, "empty.json")
    with open(p_empty, "w") as fh:
        json.dump({}, fh)
    t = _tracker(p_empty)
    t._load()
    if t.trades != {}:
        failures.append("empty store did not yield an empty dict")
    if t.load_failed:
        failures.append("legitimately empty store set load_failed -- we "
                        "looked and there was nothing, which is not an error")

    # ------------------------------------------------------------ POPULATED
    p_good = os.path.join(tmpdir, "good.json")
    payload = {"confluence": [{"pair": "AAVE/USD", "realized_pnl": 2.11}]}
    with open(p_good, "w") as fh:
        json.dump(payload, fh)
    t = _tracker(p_good)
    t._load()
    if t.trades != payload:
        failures.append("a valid store did not round-trip: got %r" % (t.trades,))
    if t.load_failed:
        failures.append("a valid store set load_failed")

    # ------------------------------------------------------------ UNREADABLE
    # The case the old code could not express.
    p_corrupt = os.path.join(tmpdir, "corrupt.json")
    with open(p_corrupt, "w") as fh:
        fh.write('{"confluence": [{"pair": "AAVE/USD",')   # truncated
    t = _tracker(p_corrupt)
    t._load()
    if not t.load_failed:
        failures.append(
            "a CORRUPTED store did not set load_failed -- it is "
            "indistinguishable from a first run, so the fleet would publish "
            "a zeroed history as though it had never traded")
    if t.load_error is None:
        failures.append("load_failed set but load_error is None -- the reason "
                        "must be recoverable from the object")
    if t.trades != {}:
        failures.append("corrupted store left partial data in self.trades")
    # And the file must survive for recovery.
    if not os.path.exists(p_corrupt):
        failures.append("the unreadable store was DELETED -- it must be left "
                        "on disk, or recovery is impossible")
    with open(p_corrupt) as fh:
        if fh.read() == "":
            failures.append("the unreadable store was TRUNCATED on read")

    # ------------------------------------------------------- WRONG TOP LEVEL
    # Valid JSON, wrong shape. Parses fine, would corrupt every downstream
    # .items() call if loaded.
    p_list = os.path.join(tmpdir, "list.json")
    with open(p_list, "w") as fh:
        json.dump(["not", "a", "dict"], fh)
    t = _tracker(p_list)
    t._load()
    if not t.load_failed:
        failures.append("a JSON list parsed as the store did not set "
                        "load_failed -- it is valid JSON of the wrong shape")
    if t.trades != {}:
        failures.append("a wrong-shaped store was loaded into self.trades")

    # -------------------------------------------------------- SAVE REPORTING
    p_ok = os.path.join(tmpdir, "sub", "save_ok.json")
    t = _tracker(p_ok)
    t.trades = {"confluence": [{"pair": "X/USD"}]}
    if t._save() is not True:
        failures.append("_save() did not return True on a successful write")
    if not os.path.exists(p_ok):
        failures.append("_save() reported success but wrote no file")

    # A path that cannot be written: point at a location under an existing
    # FILE, so makedirs and open both fail.
    blocker = os.path.join(tmpdir, "blocker")
    with open(blocker, "w") as fh:
        fh.write("x")
    p_bad = os.path.join(blocker, "nested", "save.json")
    t = _tracker(p_bad)
    t.trades = {"confluence": [{"pair": "X/USD"}]}
    res = t._save()
    if res is not False:
        failures.append(
            "_save() returned %r on an UNWRITEABLE path -- a write that "
            "silently does nothing loses trades permanently and the loss is "
            "invisible until someone counts" % (res,))

finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------------------------- report
print("cases: absent / empty / populated / corrupted / wrong-shape / save")

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: absent and empty load clean, unreadable and wrong-shaped stores "
      "flag load_failed and are left on disk, and _save reports write failure")
