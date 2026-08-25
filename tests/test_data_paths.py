"""Offline checks for datasync/data_paths.py -- plan verification steps 1 and 2.

No hardware, no S: drive. Builds fake campaign trees in a temp dir with
ROOT_C monkeypatched, so nothing touches C:\\_Data.

Run:
  PYTHONPATH=<repo> python .claude/tmp_datasync_test/test_data_paths.py
"""

import datetime
import os
import shutil
import sys
import tempfile

import datasync.data_paths as dp

FAILS = []
N = 0


def check(label, got, want):
    global N
    N += 1
    ok = got == want
    print("%-4s %-58s got=%r want=%r" % ("ok" if ok else "FAIL", label, got, want))
    if not ok:
        FAILS.append(label)


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "a").close()


# =========================================================================
# 1. week naming
# =========================================================================
print("\n--- week naming ---")
check("week_name(2026-07-29, a Wednesday)",
      dp.week_name(datetime.date(2026, 7, 29)), "260727")
# the off-by-one that actually matters: Sunday belongs to the PRECEDING Monday
check("week_name(2026-08-02, a Sunday) -> preceding Monday",
      dp.week_name(datetime.date(2026, 8, 2)), "260727")
check("week_name(2026-08-03, next Monday)",
      dp.week_name(datetime.date(2026, 8, 3)), "260803")
check("week_name(2026-07-27, the Monday itself)",
      dp.week_name(datetime.date(2026, 7, 27)), "260727")
# year boundary: ISO-week numbering would be ambiguous here, a Monday date is not
check("week_name(2027-01-01, a Friday) -> Mon 2026-12-28",
      dp.week_name(datetime.date(2027, 1, 1)), "261228")

print("\n--- is_week_dir (structural: parses AND is a Monday) ---")
check("is_week_dir('260727') Monday", dp.is_week_dir("260727"), True)
check("is_week_dir('260728') Tuesday -> rejected",
      dp.is_week_dir("260728"), False)
check("is_week_dir('260803') Monday", dp.is_week_dir("260803"), True)
check("is_week_dir('Notebooks')", dp.is_week_dir("Notebooks"), False)
check("is_week_dir('_sync')", dp.is_week_dir("_sync"), False)
check("is_week_dir('summary_data')", dp.is_week_dir("summary_data"), False)
check("is_week_dir('261399') impossible date",
      dp.is_week_dir("261399"), False)
check("is_week_dir('2026-07-27') dashed -> rejected",
      dp.is_week_dir("2026-07-27"), False)

# =========================================================================
# 2. folder creation + index, in a temp tree
# =========================================================================
tmp = tempfile.mkdtemp(prefix="dp_test_")
dp.ROOT_C = os.path.join(tmp, "_Data")
dp.ROOT_S = os.path.join(tmp, "_S_Data")
PREFIX = "H10"
# The campaign is no longer a module constant -- it is declared, and the seed is
# derived from what is on disk (an empty temp tree here, so it derives to 0).
os.environ.pop(dp._ENV_CAMPAIGN, None)
CAMP = dp.declare_campaign("TESTCAMP_Pt1", prefix=PREFIX, verbose=False)
SEED = dp.index_seed()

try:
    print("\n--- current_outer_folder creates the week + all subfolders ---")
    week1 = dp.current_outer_folder(d=datetime.date(2026, 7, 29))
    check("week folder basename", os.path.basename(week1), "260727")
    check("week folder exists", os.path.isdir(week1), True)
    for sub in dp.SUBFOLDERS:
        check("  subfolder %s/" % sub,
              os.path.isdir(os.path.join(week1, sub)), True)
    root = dp.campaign_data_root()
    check("ledger created", os.path.isfile(os.path.join(root, dp.LEDGER)), True)

    print("\n--- mirror_path ---")
    check("mirror_path swaps only the root",
          dp.mirror_path(week1), os.path.join(dp.ROOT_S, CAMP, "data", "260727"))

    print("\n--- index: empty campaign seeds at the DERIVED seed ---")
    check("empty campaign -> derived seed",
          dp.next_campaign_index(week1, PREFIX), SEED)

    print("\n--- index: reserve-on-handout (each call consumes a UNIQUE index) ---")
    # Numbering reserves on HANDOUT: a number is consumed by being issued, not by
    # a file being written, so a run that dies before its first save cannot have
    # its number reissued to another run. Repeated construction therefore yields
    # strictly-increasing UNIQUE indices -- not a constant. (This deliberately
    # replaced an older "no index burn" contract; the price is a gap in the
    # numbering when a construction never saves, which beats two runs sharing a
    # name.)
    seen = [dp.next_campaign_index(week1, PREFIX) for _ in range(200)]
    check("200 constructions -> 200 UNIQUE indices", len(set(seen)), 200)
    check("issued indices strictly increasing (never reissued)",
          all(b > a for a, b in zip(seen, seen[1:])), True)
    # SEED itself was already handed out by the "empty campaign" check above, so
    # every index in this loop must be strictly past it -- i.e. never reissued.
    check("already-issued seed is never reissued (min > SEED)",
          min(seen) > SEED, True)

    print("\n--- index: never drops below the highest run ON DISK ---")
    # a png-only run must still count (base-class writes only a figure sometimes)
    touch(os.path.join(week1, "png", "H10_02599_SingleShot_AggronQA.png"))
    touch(os.path.join(week1, "H10_02600_T1_AggronQA.h5"))
    check("index is past the highest run on disk (> 2600)",
          dp.next_campaign_index(week1, PREFIX) > 2600, True)

    week2 = dp.current_outer_folder(d=datetime.date(2026, 8, 3))
    check("week2 basename", os.path.basename(week2), "260803")
    check("EMPTY week2 continues the campaign (does NOT restart at 0)",
          dp.next_campaign_index(week2, PREFIX) > 2600, True)

    print("\n--- index: sticky floor survives pruning week1 from C: ---")
    touch(os.path.join(week2, "H10_02601_T2_AggronQA.h5"))
    before = dp.next_campaign_index(week2, PREFIX)
    shutil.rmtree(week1)
    check("week1 deleted", os.path.isdir(week1), False)
    check("after deleting week1 the index does NOT go backwards",
          dp.next_campaign_index(week2, PREFIX) >= before, True)

    print("\n--- index: self-heals from a lost/corrupt ledger ---")
    # Rebuilt from the on-disk scan, so it stays past the highest run on disk
    # (H10_02601 in week2) even though the ledger's higher reserved floor is gone.
    os.remove(os.path.join(root, dp.LEDGER))
    check("ledger gone -> rebuilt, still past the highest run on disk (>= 2602)",
          dp.next_campaign_index(week2, PREFIX) >= 2602, True)
    with open(os.path.join(root, dp.LEDGER), "w") as fid:
        fid.write("{ this is not json")
    check("corrupt ledger -> rebuilt, still past the highest run on disk (>= 2602)",
          dp.next_campaign_index(week2, PREFIX) >= 2602, True)

    print("\n--- index: non-week paths fall back to local numbering ---")
    run_dir = os.path.join(week2, "H10_02601_T2_AggronQA")
    os.makedirs(run_dir, exist_ok=True)
    check("per-run folder (the DUST inner loop) -> None, use local max+1",
          dp.next_campaign_index(run_dir, PREFIX), None)
    oneoff = os.path.join(root, "readout_destruction_02411_02419")
    os.makedirs(oneoff, exist_ok=True)
    check("one-off analysis folder directly under data/ -> None",
          dp.next_campaign_index(oneoff, PREFIX), None)
    check("the campaign data root itself -> None",
          dp.next_campaign_index(root, PREFIX), None)

    print("\n--- week_folders ignores non-week siblings ---")
    os.makedirs(os.path.join(root, "260728"), exist_ok=True)   # a Tuesday
    dp.sync_dir(create=True)
    check("week_folders sees only real Mondays",
          [os.path.basename(w) for w in dp.week_folders()], ["260803"])

    print("\n--- find_run across weeks ---")
    check("find_run(int)", os.path.basename(dp.find_run(2601) or ""),
          "H10_02601_T2_AggronQA.h5")
    check("find_run('02601')", os.path.basename(dp.find_run("02601") or ""),
          "H10_02601_T2_AggronQA.h5")
    check("find_run(basename)",
          os.path.basename(dp.find_run("H10_02601_T2_AggronQA.h5") or ""),
          "H10_02601_T2_AggronQA.h5")
    check("find_run(missing) -> None", dp.find_run(99999), None)

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d checks, %d failed" % (N, len(FAILS)))
for f in FAILS:
    print("  FAILED:", f)
sys.exit(1 if FAILS else 0)
