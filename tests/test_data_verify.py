"""Break the mirror on purpose and check the verifier notices -- plan step 3.

These are the tests that matter most: everything here is a scenario in which a
naive checker would say "yes, it's safely on S:" when it is not, and then the
data would be deleted from C:.

Fake C: and S: roots in a temp dir. Real robocopy. No network.
"""

import datetime
import json
import os
import shutil
import sys
import tempfile

import datasync.data_paths as dp
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


tmp = tempfile.mkdtemp(prefix="dvtest_")
dp.ROOT_C = os.path.join(tmp, "C_Data")
dp.ROOT_S = os.path.join(tmp, "S_Data")
os.environ.pop(dp._ENV_CAMPAIGN, None)
dp.declare_campaign("TESTVERIFY_Pt1", verbose=False)  # campaign is declared now, not a constant

# a PAST week: the current week is never prune-eligible by design, so the
# eligibility tests need a week that is genuinely finished.
PAST = (datetime.datetime.strptime(dp.week_name(), "%y%m%d").date()
        - datetime.timedelta(days=7))
WEEK = PAST.strftime("%y%m%d")


def build(extra_c=(), extra_s=()):
    """Fresh identical C:/S: week trees, plus optional per-side extras.

    BOTH sides are wiped first -- leaving C: dirty leaks files from one scenario
    into the next and silently changes the expected counts.
    """
    c = os.path.join(dp.campaign_data_root(), WEEK)
    shutil.rmtree(dv._long(c), ignore_errors=True)
    c = dp.current_outer_folder(d=PAST)
    s = dp.mirror_path(c)
    shutil.rmtree(dv._long(s), ignore_errors=True)
    for rel, data in (("H10_02500_T1_AggronQA.h5", b"H5DATA" * 100),
                      ("cfg/H10_02500_T1_AggronQA.json", b'{"a":1}'),
                      ("png/H10_02500_T1_AggronQA.png", b"PNG" * 50),
                      ("H10_02501_DUST_AggronQA/png/climb.png", b"x" * 20)):
        w(os.path.join(c, rel.replace("/", os.sep)), data)
    for rel, data in extra_c:
        w(os.path.join(c, rel.replace("/", os.sep)), data)
    # mirror C -> S exactly, long-path safe
    for root, _dirs, files in os.walk(dv._long(c)):
        for fn in files:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, dv._long(c))
            dstf = os.path.join(dv._long(s), rel)
            os.makedirs(os.path.dirname(dstf), exist_ok=True)
            shutil.copy2(src, dstf)
    for rel, data in extra_s:
        w(os.path.join(s, rel.replace("/", os.sep)), data)
    return c, s


try:
    print("\n=== 1. identical trees -> clean, and BOTH sources said clean ===")
    c, s = build()
    # tier from the PRUNE FLOOR, not "size": is_prune_eligible now requires
    # dv.PRUNE_MIN_TIER, because a size-only comparison cannot see a same-size
    # content change and that is how a corrupt mirror got certified clean.
    rec = dv.verify_week(c, tier=dv.PRUNE_MIN_TIER, verbose=False)
    check("result", rec["result"], "clean")
    check("source A verdict", rec["source_a"]["verdict"], "clean")
    check("source B verdict", rec["source_b_verdict"], "clean")
    check("sources agree", rec["sources_agree"], True)
    check("file counts agree (python vs robocopy)", rec["counts_agree"], True)
    check("C: quiesced", rec["c_quiesced"], True)
    check("receipt written", os.path.isfile(rec["receipt"]), True)
    ok, why = dv.is_prune_eligible(WEEK)
    check("past week with clean receipt IS prune-eligible", ok, True)

    print("\n=== 2. one file deleted from S: -> missing, NOT eligible ===")
    c, s = build()
    os.remove(dv._long(os.path.join(s, "cfg", "H10_02500_T1_AggronQA.json")))
    rec = dv.verify_week(c, tier="size", verbose=False)
    check("result", rec["result"], "divergent")
    check("n_missing_on_S", rec["source_a"]["n_missing_on_S"], 1)
    check("source B also saw it", rec["source_b"]["copied"], 1)
    check("sources agree on divergent", rec["sources_agree"], True)
    ok, why = dv.is_prune_eligible(WEEK)
    check("NOT prune-eligible", ok, False)
    print("     reason:", why)

    print("\n=== 3. file TRUNCATED on S: -> caught at tier 'size' ===")
    print("     (an existence-only check would wave this through)")
    c, s = build()
    victim = os.path.join(s, "H10_02500_T1_AggronQA.h5")
    with open(dv._long(victim), "r+b") as f:
        f.truncate(37)
    rec_inv = dv.verify_week(c, tier="inventory", verbose=False, write_receipt=False)
    check("tier 'inventory' MISSES a truncated file (why size is the default)",
          rec_inv["source_a"]["n_size_mismatch"], 0)
    rec = dv.verify_week(c, tier="size", verbose=False)
    check("tier 'size' catches it", rec["source_a"]["n_size_mismatch"], 1)
    check("result", rec["result"], "divergent")
    ok, _ = dv.is_prune_eligible(WEEK)
    check("NOT prune-eligible", ok, False)
    # source B must independently agree -- this is the cross-check that matters
    check("robocopy independently flags it", rec["source_b"]["copied"], 1)

    print("\n=== 4. SAME size, different bytes -> size passes, hash catches ===")
    c, s = build()
    victim = os.path.join(s, "H10_02500_T1_AggronQA.h5")
    n = os.path.getsize(dv._long(victim))
    w(victim, b"Z" * n)                      # identical length, wrong content
    os.utime(dv._long(victim),
             (os.path.getmtime(dv._long(os.path.join(c, "H10_02500_T1_AggronQA.h5"))),) * 2)
    a_size = dv.compare_trees(c, s, tier="size")
    check("tier 'size' cannot see it (documented limitation)",
          a_size["n_divergences"], 0)
    a_full = dv.compare_trees(c, s, tier="full")
    check("tier 'full' catches it", a_full["n_hash_mismatch"], 1)
    a_samp = dv.compare_trees(c, s, tier="sample")
    check("tier 'sample' catches it too (small file -> hashed whole)",
          a_samp["n_hash_mismatch"], 1)

    print("\n=== 5. extra file on S: -> reported, still clean ===")
    c, s = build(extra_s=[("H10_01999_ancient_AggronQA.h5", b"old")])
    rec = dv.verify_week(c, tier=dv.PRUNE_MIN_TIER, verbose=False)
    check("result stays clean", rec["result"], "clean")
    check("extra reported", rec["source_a"]["n_extra_on_S"], 1)
    check("robocopy calls it an Extra, not a copy", rec["source_b"]["extras"], 1)
    ok, _ = dv.is_prune_eligible(WEEK)
    check("still prune-eligible", ok, True)

    print("\n=== 6. path past MAX_PATH -> walker sees it, counts agree ===")
    deep = os.sep.join(["H10_02502_DUST_insituAC_AggronQA"]
                       + ["nested_frequency_folder_%02d_4500p0MHz" % i for i in range(6)]
                       + ["H10_00042_SingleShotQND_4500.0MHz_AggronQA.h5"])
    c, s = build(extra_c=[(deep.replace(os.sep, "/"), b"deep" * 10)])
    full_len = len(os.path.join(c, deep))
    check("the test path really is past 260 chars", full_len > 260, True)
    print("     length =", full_len)
    rec = dv.verify_week(c, tier="size", verbose=False)
    check("walker counted the deep file", rec["source_a"]["c_n_files"], 5)
    check("robocopy agrees on the count", rec["source_b"]["total"], 5)
    check("counts_agree", rec["counts_agree"], True)
    check("result clean", rec["result"], "clean")
    # and if the deep file is missing on S: it must be CAUGHT, not skipped
    os.remove(dv._long(os.path.join(s, deep)))
    rec = dv.verify_week(c, tier="size", verbose=False)
    check("a missing DEEP file is caught, not silently skipped",
          rec["source_a"]["n_missing_on_S"], 1)
    check("result divergent", rec["result"], "divergent")

    print("\n=== 7. stale receipt -> refuses ===")
    c, s = build()
    rec = dv.verify_week(c, tier=dv.PRUNE_MIN_TIER, verbose=False)
    check("clean to start", rec["result"], "clean")
    check("eligible to start", dv.is_prune_eligible(WEEK)[0], True)
    w(os.path.join(c, "H10_02502_NEW_AggronQA.h5"), b"new data not yet on S")
    ok, why = dv.is_prune_eligible(WEEK)
    check("new file on C: invalidates the receipt", ok, False)
    print("     reason:", why)

    print("\n=== 8. tier recorded in the receipt is enforced ===")
    c, s = build()
    dv.verify_week(c, tier="inventory", verbose=False)
    ok, why = dv.is_prune_eligible(WEEK, min_tier="size")
    check("an 'inventory' receipt does not satisfy min_tier='size'", ok, False)
    print("     reason:", why)

    print("\n=== 9. the CURRENT week is never eligible ===")
    cur = dp.current_outer_folder()
    w(os.path.join(cur, "H10_02600_live_AggronQA.h5"), b"being written")
    scur = dp.mirror_path(cur)
    for root, _d, files in os.walk(dv._long(cur)):
        for fn in files:
            src = os.path.join(root, fn)
            dstf = os.path.join(dv._long(scur), os.path.relpath(src, dv._long(cur)))
            os.makedirs(os.path.dirname(dstf), exist_ok=True)
            shutil.copy2(src, dstf)
    rec = dv.verify_week(cur, tier="size", verbose=False)
    check("current week can verify clean", rec["result"], "clean")
    ok, why = dv.is_prune_eligible(dp.week_name())
    check("...but is still NOT prune-eligible", ok, False)
    print("     reason:", why)

    print("\n=== 10. S: mirror missing entirely -> divergent, not clean ===")
    c, s = build()
    shutil.rmtree(dv._long(s))
    rec = dv.verify_week(c, tier="size", verbose=False)
    check("result", rec["result"], "divergent")
    check("everything flagged missing", rec["source_a"]["n_missing_on_S"], 4)
    check("NOT eligible", dv.is_prune_eligible(WEEK)[0], False)

    print("\n=== 11. no receipt at all -> refuses ===")
    for f in os.listdir(dp.sync_dir()):
        if f.startswith("verify_"):
            os.remove(os.path.join(dp.sync_dir(), f))
    ok, why = dv.is_prune_eligible(WEEK)
    check("absent receipt -> refuse", ok, False)
    print("     reason:", why)

    print("\n=== 12. audit table + figure ===")
    c, s = build()
    dv.verify_week(c, tier="size", verbose=False)
    rows = dv.audit(tier="size", verbose=True)
    # One row per week folder PLUS one "(root)" row covering the data root's own
    # loose files and non-week directories -- without that row they were mirrored
    # by nothing and reported by nothing.
    # Derived from verify_targets, not a literal: the mirror covers week folders,
    # the data-root residue AND everything beside data\ (Notebooks\ etc), and a
    # hardcoded count silently breaks every time that set changes.
    check("audit returned one row per verify target",
          len(rows), len(dv.verify_targets()))
    labels = [r["week"] for r in rows]
    check("...including the data-root residue row", dv.ROOT_LABEL in labels, True)
    check("...and the campaign-root row (everything beside data\)",
          dv.PARALLEL_LABEL in labels, True)
    fig = dv.audit_figure(rows, os.path.join(tmp, "audit.png"))
    check("figure written", os.path.isfile(fig), True)

    print("\n--- a real formatted receipt, for eyeballing ---")
    c, s = build()
    os.remove(dv._long(os.path.join(s, "png", "H10_02500_T1_AggronQA.png")))
    print(dv.format_receipt(dv.verify_week(c, tier="size", verbose=False)))

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d checks, %d failed" % (N, len(FAILS)))
for f in FAILS:
    print("  FAILED:", f)
sys.exit(1 if FAILS else 0)
