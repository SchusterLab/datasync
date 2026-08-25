"""Daemon / mirror checks -- plan verification step 4.

The load-bearing test here is ADDITIVE-ONLY: delete a file from the fake C: and
the S: copy must survive. If robocopy ever gained /MIR, that test fails and the
S: archive would be silently destroyed.

Fake C:/S: roots in a temp dir. Real robocopy. No network, no hardware.
"""

import datetime
import os
import shutil
import sys
import tempfile
import time

import datasync.data_paths as dp
import datasync.data_sync as ds
import datasync.data_verify as dv

FAILS, N = [], 0


def check(label, got, want):
    global N
    N += 1
    ok = got == want
    print("%-4s %-62s got=%r want=%r" % ("ok" if ok else "FAIL", label, got, want))
    if not ok:
        FAILS.append(label)


def w(path, data):
    os.makedirs(os.path.dirname(dv._long(path)), exist_ok=True)
    with open(dv._long(path), "wb") as f:
        f.write(data)


tmp = tempfile.mkdtemp(prefix="dstest_")
dp.ROOT_C = os.path.join(tmp, "C_Data")
dp.ROOT_S = os.path.join(tmp, "S_Data")
os.environ.pop(dp._ENV_CAMPAIGN, None)
dp.declare_campaign("TESTSYNC_Pt1", verbose=False)  # campaign is declared now, not a constant

try:
    print("\n=== flags: no /MIR, no /PURGE anywhere ===")
    flags = " ".join(ds.ROBOCOPY_FLAGS).upper()
    check("/MIR absent", "/MIR" in flags, False)
    check("/PURGE absent", "/PURGE" in flags, False)
    check("/E present", "/E" in ds.ROBOCOPY_FLAGS, True)
    check("/XO present", "/XO" in ds.ROBOCOPY_FLAGS, True)
    check("/FFT present (SMB timestamp granularity)",
          "/FFT" in ds.ROBOCOPY_FLAGS, True)

    print("\n=== sync_once lands files on S: ===")
    week = dp.current_outer_folder()
    w(os.path.join(week, "H10_02568_TimeOfFlight_AggronQA.h5"), b"h5" * 500)
    w(os.path.join(week, "cfg", "H10_02568_TimeOfFlight_AggronQA.json"), b'{"q":0}')
    w(os.path.join(week, "png", "H10_02568_TimeOfFlight_AggronQA.png"), b"png" * 300)
    w(os.path.join(week, "npz", "H10_02568_thing.npz"), b"npz")
    rows = ds.sync_once(verbose=True)
    # One row per week folder PLUS a "(root)" row for the data root's loose files
    # and non-week directories, which the week loop alone never reaches.
    # One row per week folder, plus the data-root residue, plus everything beside
    # data\ (Notebooks\, loose notebooks): policy is that the WHOLE campaign root
    # reaches S:. Derived from verify_targets so it tracks that set.
    check("one row per mirror target", len(rows), len(dv.verify_targets()))
    labels = [r["week"] for r in rows]
    check("data-root residue pass ran", dv.ROOT_LABEL in labels, True)
    check("campaign-root pass ran", dv.PARALLEL_LABEL in labels, True)
    check("no extra pass failed",
          [r["week"] for r in rows if r["failed"]], [])
    check("pass did not fail", rows[0]["failed"], False)
    check("copied 4 files", rows[0]["copied"], 4)
    mirror = dp.mirror_path(week)
    for rel in ("H10_02568_TimeOfFlight_AggronQA.h5",
                "cfg/H10_02568_TimeOfFlight_AggronQA.json",
                "png/H10_02568_TimeOfFlight_AggronQA.png",
                "npz/H10_02568_thing.npz"):
        check("  on S: %s" % rel,
              os.path.isfile(dv._long(os.path.join(mirror, rel.replace("/", os.sep)))),
              True)

    print("\n=== and the verifier agrees it is clean ===")
    rec = dv.verify_week(week, tier="full", verbose=False)
    check("verify_week(full) -> clean", rec["result"], "clean")

    print("\n=== second pass copies nothing (/XO incremental) ===")
    rows = ds.sync_once(verbose=True)
    check("copied 0 on the second pass", rows[0]["copied"], 0)
    check("skipped 4", rows[0]["skipped"], 4)

    print("\n=== ADDITIVE ONLY: deleting from C: must NOT delete from S: ===")
    victim_rel = os.path.join("cfg", "H10_02568_TimeOfFlight_AggronQA.json")
    os.remove(dv._long(os.path.join(week, victim_rel)))
    check("gone from C:", os.path.isfile(dv._long(os.path.join(week, victim_rel))), False)
    rows = ds.sync_once(verbose=True)
    check("pass still succeeds", rows[0]["failed"], False)
    check("*** the S: copy SURVIVED (this is what /MIR would destroy) ***",
          os.path.isfile(dv._long(os.path.join(mirror, victim_rel))), True)

    print("\n=== a whole pruned week stays on S: ===")
    past = (datetime.datetime.strptime(dp.week_name(), "%y%m%d").date()
            - datetime.timedelta(days=7))
    old = dp.current_outer_folder(d=past)
    w(os.path.join(old, "H10_02400_old_AggronQA.h5"), b"old data")
    ds.sync_once(verbose=False)
    old_mirror = dp.mirror_path(old)
    check("old week mirrored",
          os.path.isfile(dv._long(os.path.join(old_mirror, "H10_02400_old_AggronQA.h5"))),
          True)
    shutil.rmtree(dv._long(old))
    check("old week gone from C:", os.path.isdir(dv._long(old)), False)
    ds.sync_once(verbose=False)
    check("*** pruned week's data still on S: after a pass ***",
          os.path.isfile(dv._long(os.path.join(old_mirror, "H10_02400_old_AggronQA.h5"))),
          True)

    print("\n=== dry run copies nothing ===")
    w(os.path.join(week, "H10_02569_new_AggronQA.h5"), b"brand new")
    rows = ds.sync_once(dry_run=True, verbose=True)
    check("dry run reports it WOULD copy", rows[0]["copied"], 1)
    check("but did not actually copy it",
          os.path.isfile(dv._long(os.path.join(mirror, "H10_02569_new_AggronQA.h5"))),
          False)
    ds.sync_once(verbose=False)
    check("real pass then copies it",
          os.path.isfile(dv._long(os.path.join(mirror, "H10_02569_new_AggronQA.h5"))),
          True)

    print("\n=== unreachable destination -> failure is reported, not swallowed ===")
    saved_s = dp.ROOT_S
    dp.ROOT_S = r"\\10.255.255.1\nonexistent_share"
    rows = ds.sync_once(verbose=True)
    check("rc >= 8 surfaced as failed", rows[0]["failed"], True)
    print("     rc =", rows[0]["rc"])
    dp.ROOT_S = saved_s

    print("\n=== sync_now trigger ===")
    trig = ds._state("sync_now")
    if os.path.exists(trig):
        os.remove(trig)
    check("sync_now() returns True", ds.sync_now(), True)
    check("trigger file created", os.path.exists(trig), True)
    check("idempotent", ds.sync_now() and os.path.exists(trig), True)

    print("\n=== daemon liveness from the heartbeat ===")
    hb = ds._state("heartbeat", create=True)
    if os.path.exists(hb):
        os.remove(hb)
    check("no heartbeat -> not alive", ds.daemon_alive()[0], False)
    ds._touch(hb)
    check("fresh heartbeat -> alive", ds.daemon_alive()[0], True)
    old_t = time.time() - (ds.HEARTBEAT_STALE_S + 10)
    os.utime(hb, (old_t, old_t))
    check("stale heartbeat -> not alive", ds.daemon_alive()[0], False)

    print("\n=== prune gate: refuses without a clean fresh receipt ===")
    for f in os.listdir(dp.sync_dir()):
        if f.startswith("verify_"):
            os.remove(os.path.join(dp.sync_dir(), f))
    check("no receipt -> prune_cmd returns None",
          ds.prune_cmd(past.strftime("%y%m%d")), None)
    check("current week -> prune_cmd returns None",
          ds.prune_cmd(dp.week_name()), None)

    print("\n=== prune gate: allows a verified PAST week, and only prints ===")
    old = dp.current_outer_folder(d=past)
    w(os.path.join(old, "H10_02400_old_AggronQA.h5"), b"old data")
    ds.sync_once(verbose=False)
    # Verify at the PRUNE FLOOR (dv.PRUNE_MIN_TIER), now stricter than the
    # verifier default: prune_cmd refuses a size-only receipt on purpose, because
    # 'size' cannot see a same-size content change.
    rec = dv.verify_week(old, tier=dv.PRUNE_MIN_TIER, verbose=False)
    check("past week verifies clean", rec["result"], "clean")
    out = ds.prune_cmd(past.strftime("%y%m%d"))
    check("prune_cmd returned the path", out, old)
    check("*** and did NOT delete it ***", os.path.isdir(dv._long(old)), True)

    print("\n=== full daemon: launch detached, verify it mirrors, then stop ===")
    for f in ("heartbeat", "sync_now", "daemon.lock"):
        p = ds._state(f)
        if os.path.exists(p):
            os.remove(p)
    alive_before = ds.daemon_alive()[0]
    check("not running before", alive_before, False)
    ds.ensure_daemon(outer_folder=week, verbose=True)
    check("daemon reports alive after ensure_daemon", ds.daemon_alive()[0], True)
    pid = int(open(ds._state("daemon.lock")).read().split()[0])
    print("     daemon pid =", pid)
    # a NEW file plus a trigger must reach S: without us calling sync_once
    w(os.path.join(week, "H10_02570_daemon_AggronQA.h5"), b"written by the test")
    ds.sync_now()
    landed = False
    target = dv._long(os.path.join(mirror, "H10_02570_daemon_AggronQA.h5"))
    # Budget DERIVED from POLL_S, not hardcoded. The daemon notices the trigger on
    # its next wake, so the wait must exceed POLL_S plus one pass; a literal 30 s
    # here silently became a coin-flip the moment POLL_S went from 3 to 30.
    deadline = time.time() + ds.POLL_S + 20.0
    while time.time() < deadline:
        if os.path.isfile(target):
            landed = True
            break
        time.sleep(0.5)
    check("*** daemon mirrored a new file on its own ***", landed, True)
    check("ensure_daemon does not launch a second one",
          ds.ensure_daemon(outer_folder=week, verbose=False), True)
    pid2 = int(open(ds._state("daemon.lock")).read().split()[0])
    check("same pid still holds the lock", pid2, pid)
    print("\n=== status() ===")
    ds.status()
    subprocess_kill = __import__("subprocess")
    subprocess_kill.run(["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True)
    print("     killed daemon pid", pid)

finally:
    for f in ("daemon.lock",):
        try:
            p = ds._state(f)
            if os.path.exists(p):
                pid = int(open(p).read().split()[0])
                __import__("subprocess").run(["taskkill", "/F", "/PID", str(pid)],
                                             capture_output=True)
        except Exception:
            pass
    time.sleep(0.5)
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d checks, %d failed" % (N, len(FAILS)))
for f in FAILS:
    print("  FAILED:", f)
sys.exit(1 if FAILS else 0)
