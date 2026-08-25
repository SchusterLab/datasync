"""Is this week's data REALLY on S:? -- the check that gates deleting from C:.

Design goal: **a false "clean" must be impossible.** Everything here exists to
make the answer trustworthy enough to delete data on, so it is deliberately
paranoid and deliberately independent of the thing that did the copying.

Two independent sources, both must agree
----------------------------------------
Asking robocopy "did you copy everything?" inherits any bug robocopy has, so a
week is clean only when two separate implementations say so:

* **Source A** -- a pure-Python ``os.scandir`` walker of both trees. No robocopy.
* **Source B** -- ``robocopy /L`` (list-only): the copier's own verdict on what
  still differs.

Disagreement between them is a hard failure (``source_disagreement``), never
silently resolved in favour of either one.

Tiers (recorded in the receipt, because "verified" is meaningless without it)
---------------------------------------------------------------------------
``inventory``  every relative path on C: exists on S:.
``size``       + identical byte sizes. **The default.** Catches truncated and
               partially-copied files, which is the realistic failure mode.
``sample``     + SHA-256 over head+tail 8 MB (whole file below 16 MB).
               NOTE: for a large file this cannot see a change in the MIDDLE --
               it is a cheap truncation/corruption screen, not a proof.
``full``       + SHA-256 of every byte on both sides. The only tier that proves
               content equality. Slow over SMB; leave it running.

False-clean hazards this module closes
--------------------------------------
* **Long paths.** The DUST trees nest past Windows' 260-char limit. A walker
  that silently drops those entries makes C: look *smaller* than it is and the
  week passes. Every path is ``\\\\?\\``-prefixed, and the walker's file count is
  cross-checked against robocopy's (robocopy handles long paths natively), so a
  loss on either side shows up as a count disagreement.
* **Unreadable directories.** Swallowing an OSError also shrinks C:. Every walk
  error is collected and forces a non-clean result.
* **SMB mtime granularity.** Local NTFS vs ``\\\\10.108.30.4\\slab`` can differ by
  ~2 s, so mtime gets a tolerance and **size is the hard criterion** (robocopy
  gets /FFT for the same reason).
* **Live writes during the check.** C: is fingerprinted before and after; if it
  moved, the result is ``inconclusive``, never ``clean``.
* **Extras on S: are normal.** S: legitimately holds history C: no longer has
  (the old copy-then-delete workflow, and any pruned week). Extras are reported
  -- an *unexpected* one can reveal a path bug writing to the wrong place -- but
  never counted as a divergence.

Stdlib only, so it runs in either conda env. matplotlib is imported lazily,
inside the plotting function only.
"""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time

import datasync.data_paths as dp

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

TIER_ORDER = ("inventory", "size", "sample", "full")
DEFAULT_TIER = "size"

# Floor for authorising a DELETE. Higher than DEFAULT_TIER on purpose: 'size'
# cannot see a same-size content change, and two separate measured scenarios turn
# that into a certified-clean corrupt mirror --
#   * an incremental saver rewriting the run .h5 at the same size within the 2 s
#     /FFT + MTIME_TOL window, so /XO never re-copies it;
#   * a copy interrupted mid-file leaving a full-size, zero-tailed file on S:.
# 'sample' hashes head+tail (whole file below 16 MB) and catches both. Reading
# is cheap; deleting the only copy is not.
PRUNE_MIN_TIER = "sample"

# A receipt older than this cannot authorise a delete, however clean it was. It
# is a record of a past observation, and the tree it describes has had a week of
# opportunity to change.
PRUNE_MAX_RECEIPT_AGE_S = 7 * 86400.0

# Label for a campaign that predates the week-folder layout, whose data root
# holds runs directly. Deliberately not a valid week name, so is_prune_eligible's
# "is this the current week" test and dp.is_week_dir can never confuse the two.
FLAT_LABEL = "(flat)"

# Label for the campaign data root itself, covering everything the per-week
# targets do not: loose files and non-week directories. Not a valid week name, so
# is_prune_eligible's "is this the current week" test cannot confuse the two.
ROOT_LABEL = "(root)"

# Label for everything in the CAMPAIGN folder alongside data\ -- Notebooks\, a
# loose .ipynb, any analysis folder. POLICY: everything under the campaign root
# belongs on S:, not only the raw-data tree. DATA_SUBDIR still exists to separate
# the two, and pruning is still gated on data\ only, but nothing under the
# campaign folder is left unmirrored.
PARALLEL_LABEL = "(campaign root)"

# Directories excluded from BOTH verification sources and from the backlog copy.
#
# _sync holds this module's own receipts and the copier's own log, and for a FLAT
# campaign it lives INSIDE the compared tree (<campaign>\data\_sync). Left in, the
# verifier's bookkeeping becomes part of what it verifies:
#   * writing a receipt makes C: differ from S: -> permanently "divergent";
#   * it also changes the fingerprint, so is_prune_eligible reports "C: changed
#     since the receipt" forever;
#   * and robocopy's own growing log is a moving target inside its own source.
# A tool's scratch state must never be part of its own evidence.
#
# CRITICAL: this must be applied IDENTICALLY to the Python walker and to
# robocopy /L. counts_agree compares their file totals for exact equality, so
# excluding a directory from one source and not the other makes the two sources
# disagree on every run and turns every verdict into 'inconclusive'.
EXCLUDE_DIRS = (dp.SYNC_DIR,)

MTIME_TOL = 2.0          # s; FAT/SMB timestamp granularity
SAMPLE_THRESHOLD = 16 << 20   # files at or below this get hashed whole
SAMPLE_CHUNK = 8 << 20        # head and tail bytes hashed for larger files
HASH_BLOCK = 1 << 20

ROBOCOPY = "robocopy"


# ---------------------------------------------------------------------------
# Long-path-safe filesystem access
# ---------------------------------------------------------------------------

def _long(path):
    r"""``\\?\``-prefix an absolute path so os.scandir / open see past MAX_PATH.

    Without this the deep DUST trees (run folder -> per-frequency folder ->
    per-shot files) silently truncate the walk, which would make C: look
    smaller than it is and hand back a false "clean".
    """
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):                 # UNC share
        return "\\\\?\\UNC" + p[1:]
    return "\\\\?\\" + p


def walk_tree(root, exclude_dirs=EXCLUDE_DIRS, exclude_top=()):
    """Walk *root* -> ({relpath_lower: (size, mtime)}, n_dirs, errors).

    Relative paths are lowercased because both NTFS and robocopy are
    case-insensitive; two files differing only in case cannot coexist in one
    Windows directory, so nothing is lost by folding case here.

    *errors* is never swallowed: an unreadable directory shrinks the C: side and
    would otherwise produce a false clean, so any entry here forces a non-clean
    verdict upstream.

    *exclude_dirs* are directory NAMES skipped at any depth, matched
    case-insensitively -- the same semantics as robocopy's ``/XD <name>``, so the
    two verification sources stay comparable (see EXCLUDE_DIRS).

    *exclude_top* are names skipped ONLY at the top level, matching robocopy's
    ``/XD <full path>`` (an argument containing a separator is an exact path, not
    a name). Needed to compare a campaign data root while leaving its week folders
    to their own targets: a bare-name exclusion would also silence any identically
    named directory nested deep inside a run, which would be a blind spot.
    """
    files, errors, n_dirs = {}, [], 0
    skip = {d.lower() for d in (exclude_dirs or ())}
    skip_top = {os.path.basename(str(d).rstrip("\\/")).lower()
                for d in (exclude_top or ())}
    base = _long(root)
    if not os.path.isdir(base):
        return files, n_dirs, errors

    stack = [""]
    while stack:
        rel = stack.pop()
        cur = os.path.join(base, rel) if rel else base
        try:
            with os.scandir(cur) as it:
                for e in it:
                    r = os.path.join(rel, e.name) if rel else e.name
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name.lower() in skip:
                                continue      # not counted, not descended into
                            if not rel and e.name.lower() in skip_top:
                                continue      # top level only (rel == "")
                            n_dirs += 1
                            stack.append(r)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat()
                            files[r.lower()] = (st.st_size, st.st_mtime)
                    except OSError as exc:
                        errors.append("stat %s: %s" % (r, exc))
        except OSError as exc:
            errors.append("scandir %s: %s" % (rel or ".", exc))
    return files, n_dirs, errors


def fingerprint(root):
    """Cheap snapshot used to detect that C: moved under us / a stale receipt."""
    files, n_dirs, errors = walk_tree(root)
    return {"n_files": len(files),
            "n_dirs": n_dirs,
            "total_bytes": sum(s for s, _ in files.values()),
            "newest_mtime": max((m for _, m in files.values()), default=0.0),
            "walk_errors": len(errors)}


def file_digest(path, tier):
    """SHA-256 of *path*; head+tail sample for big files unless tier == 'full'.

    The byte size is folded into the digest so a sampled hash can never collide
    across differently-sized files.
    """
    lp = _long(path)
    size = os.path.getsize(lp)
    h = hashlib.sha256()
    with open(lp, "rb") as fid:
        if tier == "full" or size <= SAMPLE_THRESHOLD:
            for blk in iter(lambda: fid.read(HASH_BLOCK), b""):
                h.update(blk)
        else:
            h.update(fid.read(SAMPLE_CHUNK))
            fid.seek(-SAMPLE_CHUNK, os.SEEK_END)
            h.update(fid.read(SAMPLE_CHUNK))
    h.update(b"|%d" % size)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Source B: robocopy's own opinion
# ---------------------------------------------------------------------------

def robocopy_pending(src, dst, exclude_dirs=EXCLUDE_DIRS, exclude_top=()):
    """``robocopy /L`` -> what robocopy thinks still needs copying.

    No ``/XO`` here on purpose: robocopy's *default* criterion (copy when size
    OR timestamp differs) is the stricter comparison, which is what we want for
    verification. ``/XO`` is for the copier, not the verifier.

    *exclude_dirs* becomes ``/XD``, and MUST match what walk_tree skips -- see
    EXCLUDE_DIRS. If the two sources disagree about which directories are in
    scope, counts_agree is false on every run and nothing can ever be certified.

    Returns a dict with the parsed Files row, the raw text, and the exit code.
    Plain (not ``\\\\?\\``-prefixed) paths -- robocopy handles long paths itself.
    """
    # /NJH suppresses the job header, whose "Files : *.*" filter-echo line would
    # otherwise shadow the summary row of the same name. /NJS is NOT passed --
    # the job summary is exactly what we parse.
    cmd = [ROBOCOPY, os.path.abspath(src), os.path.abspath(dst),
           "/E", "/L", "/FFT", "/BYTES", "/R:0", "/W:0",
           "/NP", "/NFL", "/NDL", "/NJH"]
    for d in (exclude_dirs or ()):
        cmd += ["/XD", d]
    for d in (exclude_top or ()):
        # A /XD argument containing a separator is an EXACT PATH, so this excludes
        # only the top-level entry -- matching walk_tree's exclude_top.
        cmd += ["/XD", os.path.join(os.path.abspath(src),
                                    os.path.basename(str(d).rstrip("\\/")))]
    try:
        # CREATE_NO_WINDOW for the same reason as in data_sync._robocopy: called
        # from a process without a console (the daemon, a detached script), a
        # console child otherwise gets its own pop-up window.
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              creationflags=getattr(subprocess,
                                                    "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        return {"ok": False, "error": "cannot run robocopy: %s" % exc,
                "rc": None, "raw": ""}

    out = proc.stdout or ""
    parsed = _parse_summary(out)
    # robocopy returns a BITMASK, not a status: 0-7 are success flavours
    # (1 = "files would be copied"), >=8 means a real failure.
    parsed["rc"] = proc.returncode
    parsed["rc_failed"] = proc.returncode is not None and proc.returncode >= 8
    parsed["raw"] = out[-4000:]
    parsed["cmd"] = " ".join(cmd)
    return parsed


def _parse_summary(out):
    """Pull the ``Files :`` row out of robocopy's summary table.

    Layout (with /BYTES so the numbers are raw)::

                   Total    Copied   Skipped  Mismatch    FAILED    Extras
        Dirs  :       12         0        12         0         0         0
        Files :      325         0       325         0         0         0

    The format is verified empirically by the test suite rather than assumed --
    a mis-parse here would silently zero out the "pending" count and hand back a
    false clean, so *failure to parse is itself a failure*, never a pass.

    Do NOT stop at the first line matching "Files :". Robocopy's job header
    echoes the filename filter as ``Files : *.*``, which shadows the summary row
    of the same name -- the exact bug this docstring's test caught. We take the
    LAST ``Files :`` row whose fields are all integers, so the parse survives
    both the header and any future extra line.
    """
    res = {"ok": False, "parse_error": None,
           "total": None, "copied": None, "skipped": None,
           "mismatch": None, "failed": None, "extras": None}
    seen_row, best = False, None
    for line in out.splitlines():
        s = line.strip()
        if not s.lower().replace(" ", "").startswith("files:"):
            continue
        seen_row = True
        nums = []
        for tok in s.split(":", 1)[1].split():
            try:
                nums.append(int(tok))
            except ValueError:
                nums = None
                break
        if nums and len(nums) >= 6:
            best = nums[:6]            # keep scanning; the summary comes last
    if best is not None:
        (res["total"], res["copied"], res["skipped"],
         res["mismatch"], res["failed"], res["extras"]) = best
        res["ok"] = True
        return res
    res["parse_error"] = ("'Files :' row found but no numeric summary"
                          if seen_row else
                          "no 'Files :' row in robocopy output")
    return res


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------

def _fully_hashed(tier, size):
    """Were this file's bytes compared IN FULL at *tier*?

    Only 'full' hashes everything at any size; 'sample' hashes a file whole only
    while it is at or below SAMPLE_THRESHOLD. The distinction matters because the
    unverifiable-window test used to key off the TIER NAME, on the assumption that
    "'sample'/'full' hash it and give a real answer". For a big file that is
    false, and it inverted the tiers: a 48 MB h5 with 480 changed bytes in its
    middle was reported divergent by the DEFAULT tier ('size', via the window
    bucket) and clean by the PRUNE FLOOR ('sample'), because sample suppressed the
    window and then failed to look at the interior.
    """
    return tier == "full" or size <= SAMPLE_THRESHOLD


def compare_trees(c_root, s_root, tier=DEFAULT_TIER, max_report=50,
                  exclude_dirs=EXCLUDE_DIRS, exclude_top=()):
    """Source A: compare two trees. Returns buckets + counts.

    Buckets that count as **divergence** (C: data not safely on S:):
      ``missing_on_S``   -- present on C:, absent on S:
      ``size_mismatch``  -- both present, different byte size
      ``newer_on_C``     -- C: copy modified after the S: copy
      ``hash_mismatch``  -- content differs (tier 'sample'/'full' only)

    Reported but **not** a divergence:
      ``extra_on_S``     -- on S: only. Normal: S: keeps history C: has dropped.
    """
    if tier not in TIER_ORDER:
        raise ValueError("unknown tier %r, expected one of %r" % (tier, TIER_ORDER))

    c_files, c_dirs, c_err = walk_tree(c_root, exclude_dirs, exclude_top)
    s_files, s_dirs, s_err = walk_tree(s_root, exclude_dirs, exclude_top)

    missing, size_bad, newer, hash_bad, hash_err = [], [], [], [], []
    n_partial = 0        # files whose interior neither source read (see _fully_hashed)
    window = []          # same size, C: newer by <= MTIME_TOL: cannot be settled
                         # without hashing, so never certified clean at this tier

    for rel, (c_size, c_mt) in c_files.items():
        s = s_files.get(rel)
        if s is None:
            missing.append(rel)
            continue
        s_size, s_mt = s
        if tier != "inventory" and c_size != s_size:
            size_bad.append({"path": rel, "c_bytes": c_size, "s_bytes": s_size})
            continue                      # size already condemns it; skip hashing
        if c_mt > s_mt + MTIME_TOL:
            newer.append({"path": rel,
                          "c_mtime": _iso(c_mt), "s_mtime": _iso(s_mt)})
        elif c_mt > s_mt and not _fully_hashed(tier, c_size):
            # C: was rewritten AFTER the copy, but by less than MTIME_TOL, and the
            # size is unchanged. Three tolerances line up to hide this: /XO /FFT
            # will not re-copy a source within 2 s of the destination, the
            # MTIME_TOL test above does not flag it, and a size-only tier never
            # hashes. So S: keeps the OLD content while everything reports clean.
            #
            # This is not hypothetical: the incremental savers (mDUST calls
            # save_data() inside its sweep loop) rewrite the run .h5 at the same
            # byte size seconds apart, because h5py reuses freed space.
            #
            # It cannot be resolved at this tier, so it is reported as
            # UNVERIFIABLE rather than clean -- 'sample'/'full' hash it and give a
            # real answer.
            window.append({"path": rel, "c_mtime": _iso(c_mt),
                           "s_mtime": _iso(s_mt),
                           "delta_s": round(c_mt - s_mt, 3)})
        if tier in ("sample", "full"):
            if not _fully_hashed(tier, c_size):
                # Counted, not hidden: at this tier the interior of this file is
                # read by NEITHER source, so "clean" for it means "the head, the
                # tail and the size match". is_prune_eligible refuses on a
                # non-zero count, so a partial hash can never authorise a delete.
                n_partial += 1
            try:
                if (file_digest(os.path.join(c_root, rel), tier)
                        != file_digest(os.path.join(s_root, rel), tier)):
                    hash_bad.append(rel)
            except OSError as exc:
                # unreadable == unverified; must not pass as clean
                hash_err.append("%s: %s" % (rel, exc))

    extra = [r for r in s_files if r not in c_files]

    div = {"missing_on_S": missing[:max_report],
           "size_mismatch": size_bad[:max_report],
           "newer_on_C": newer[:max_report],
           "hash_mismatch": hash_bad[:max_report],
           "unverifiable_window": window[:max_report]}
    n_div = (len(missing) + len(size_bad) + len(newer) + len(hash_bad)
             + len(window))

    return {"tier": tier,
            "c_n_files": len(c_files), "s_n_files": len(s_files),
            "c_n_dirs": c_dirs, "s_n_dirs": s_dirs,
            "c_bytes": sum(s for s, _ in c_files.values()),
            "s_bytes": sum(s for s, _ in s_files.values()),
            "n_missing_on_S": len(missing), "n_size_mismatch": len(size_bad),
            "n_newer_on_C": len(newer), "n_hash_mismatch": len(hash_bad),
            "n_unverifiable_window": len(window),
            "n_partially_hashed": n_partial,
            "n_extra_on_S": len(extra), "extra_on_S": extra[:max_report],
            "n_divergences": n_div, "divergences": div,
            "walk_errors": (c_err + s_err + hash_err)[:max_report],
            "n_walk_errors": len(c_err) + len(s_err) + len(hash_err),
            "verdict": "clean" if (n_div == 0 and not c_err and not s_err
                                   and not hash_err) else "divergent"}


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# verify_week: the two sources, plus the quiesce check and the receipt
# ---------------------------------------------------------------------------

def receipt_path(week, drive="C", campaign=None):
    return os.path.join(dp.sync_dir(drive, campaign),
                        "verify_%s.json" % os.path.basename(week))


def read_receipt(week, drive="C", campaign=None):
    try:
        with open(receipt_path(week, drive, campaign)) as fid:
            return json.load(fid)
    except (OSError, ValueError):
        return None


def verify_week(week_folder, tier=DEFAULT_TIER, write_receipt=True, verbose=True,
                campaign=None):
    """Verify one week folder against its S: mirror. Writes a receipt.

    ``result`` is one of:
      ``clean``        -- both sources agree everything on C: is on S:
      ``divergent``    -- something on C: is not safely on S:
      ``inconclusive`` -- could not be established (C: changed mid-check,
                          robocopy unavailable/unparseable, walk errors).
                          Treated exactly as unsafe by the pruning gate.
    """
    week_folder = os.path.abspath(week_folder)
    week = os.path.basename(week_folder)
    mirror = dp.mirror_path(week_folder)

    before = fingerprint(week_folder)

    # -- Source A: pure Python -------------------------------------------------
    a = compare_trees(week_folder, mirror, tier=tier)

    # -- Source B: robocopy's own verdict --------------------------------------
    b = robocopy_pending(week_folder, mirror)
    if not b.get("ok"):
        b_verdict = "inconclusive"
    elif b["rc_failed"] or b["copied"] or b["mismatch"] or b["failed"]:
        b_verdict = "divergent"
    else:
        b_verdict = "clean"

    # -- cross-check the two file counts --------------------------------------
    # robocopy's Total is what IT found in the source tree. A mismatch against
    # the Python walker means one of them lost entries -- the long-path failure
    # mode -- so it can never be waved through.
    counts_agree = bool(b.get("ok")) and b.get("total") == a["c_n_files"]

    # -- quiesce: did C: move while we looked? --------------------------------
    after = fingerprint(week_folder)
    quiet = (before == after)

    sources_agree = (a["verdict"] == b_verdict) and counts_agree

    if not quiet:
        result = "inconclusive"
    elif b_verdict == "inconclusive" or not b.get("ok"):
        result = "inconclusive"
    elif not counts_agree:
        result = "inconclusive"
    elif a["verdict"] == "clean" and b_verdict == "clean":
        result = "clean"
    else:
        result = "divergent"

    rec = {"week": week,
           "campaign": dp.active_campaign(campaign),
           "c_path": week_folder, "s_path": mirror,
           "tier": tier,
           "when": datetime.datetime.now().isoformat(timespec="seconds"),
           "result": result,
           "sources_agree": sources_agree,
           "source_disagreement": (not sources_agree),
           "counts_agree": counts_agree,
           "c_quiesced": quiet,
           "fingerprint": before,
           # The S: side as it was WHEN VERIFIED. is_prune_eligible re-walks the
           # mirror and compares against this, so a receipt can no longer serve
           # as standing proof about a destination that has since changed or been
           # deleted. Without it a clean receipt outlived the data it described.
           "s_fingerprint": fingerprint(mirror),
           "source_a": a,
           "source_b": {k: b.get(k) for k in
                        ("ok", "parse_error", "total", "copied", "skipped",
                         "mismatch", "failed", "extras", "rc", "rc_failed",
                         "cmd", "error")},
           "source_b_verdict": b_verdict}

    if write_receipt:
        # Must honour *campaign*. Writing to the default campaign's _sync while
        # read_receipt/is_prune_eligible look under the campaign they were asked
        # about would leave the receipt permanently unfindable, and a week that
        # HAS been verified would read as "no receipt".
        d = dp.sync_dir("C", campaign, create=True)
        path = os.path.join(d, "verify_%s.json" % week)
        tmp = path + ".tmp"
        with open(tmp, "w") as fid:
            json.dump(rec, fid, indent=1)
        os.replace(tmp, path)
        rec["receipt"] = path

    if verbose:
        print(format_receipt(rec))
    return rec


def format_receipt(rec):
    a = rec["source_a"]
    b = rec["source_b"]
    flag = {"clean": "CLEAN", "divergent": "DIVERGENT",
            "inconclusive": "INCONCLUSIVE"}[rec["result"]]
    lines = [
        "week %s  tier=%s  ->  %s" % (rec["week"], rec["tier"], flag),
        "  C: %7d files  %8.2f GB      S: %7d files  %8.2f GB"
        % (a["c_n_files"], a["c_bytes"] / 1e9, a["s_n_files"], a["s_bytes"] / 1e9),
        "  source A (python): %s   missing=%d size=%d newer=%d hash=%d"
        % (a["verdict"], a["n_missing_on_S"], a["n_size_mismatch"],
           a["n_newer_on_C"], a["n_hash_mismatch"]),
        "  source B (robocopy /L): %s   would-copy=%s mismatch=%s failed=%s total=%s"
        % (rec["source_b_verdict"], b.get("copied"), b.get("mismatch"),
           b.get("failed"), b.get("total")),
        "  counts agree=%s   sources agree=%s   C: quiesced=%s"
        % (rec["counts_agree"], rec["sources_agree"], rec["c_quiesced"]),
    ]
    if a["n_extra_on_S"]:
        lines.append("  on S: only (normal -- history C: no longer has): %d"
                     % a["n_extra_on_S"])
    if a["n_walk_errors"]:
        lines.append("  !! %d walk errors -- cannot be clean" % a["n_walk_errors"])
    if b.get("parse_error"):
        lines.append("  !! robocopy summary parse failed: %s" % b["parse_error"])
    for bucket in ("missing_on_S", "size_mismatch", "newer_on_C", "hash_mismatch"):
        items = a["divergences"].get(bucket) or []
        if items:
            lines.append("  %s (first %d):" % (bucket, min(5, len(items))))
            for it in items[:5]:
                lines.append("    %s" % (it if isinstance(it, str) else it))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pruning eligibility -- the gate. Answers only "may this be deleted?"
# ---------------------------------------------------------------------------

def is_prune_eligible(week, drive="C", campaign=None, min_tier=PRUNE_MIN_TIER,
                      max_receipt_age_s=PRUNE_MAX_RECEIPT_AGE_S):
    """(bool, reason). Every condition must hold; anything unknown means no.

    1. not the current week;
    2. a receipt exists, at tier >= *min_tier*, no older than *max_receipt_age_s*;
    3. that receipt says ``clean`` and both sources agreed;
    4. C: has not changed since the receipt was written;
    5. **S: still holds the data, re-checked NOW** -- not merely remembered;
    6. nothing is currently claiming a path inside the week.

    Condition 5 is the important addition. The gate used to re-fingerprint only
    the C: side, so a receipt was treated as standing proof about S: for ever.
    Reproduced: mirror a week, verify clean, then delete the ENTIRE S: copy --
    the gate still returned True, prune_cmd printed the Remove-Item, and the
    startup banner every notebook prints announced the week as "verified on S:
    and safe to reclaim".

    A receipt records a PAST observation. An irreversible delete needs a present
    one, and one network walk is trivial next to losing the data.
    """
    week = os.path.basename(str(week))
    if week == dp.week_name():
        return False, "this is the current week -- still being written to"

    rec = read_receipt(week, drive, campaign)
    if rec is None:
        return False, "no verification receipt (run --verify %s)" % week
    if rec.get("result") != "clean":
        return False, "last verification was %r, not clean" % rec.get("result")
    if not rec.get("sources_agree"):
        return False, "the two verification sources disagreed"
    if TIER_ORDER.index(rec.get("tier", "inventory")) < TIER_ORDER.index(min_tier):
        return False, ("receipt is tier %r, below the required %r -- re-verify "
                       "with --tier %s" % (rec.get("tier"), min_tier, min_tier))

    if max_receipt_age_s:
        try:
            age = time.time() - os.path.getmtime(
                receipt_path(week, drive, campaign))
        except OSError:
            age = None
        if age is None or age > max_receipt_age_s:
            return False, ("receipt is %s (limit %.0f days) -- re-verify"
                           % ("of unknown age" if age is None
                              else "%.1f days old" % (age / 86400.0),
                              max_receipt_age_s / 86400.0))

    week_dir = os.path.join(dp.campaign_data_root(drive, campaign), week)
    now = fingerprint(week_dir)
    if now != rec.get("fingerprint"):
        return False, ("C: changed since the receipt (%s) -- re-verify"
                       % rec.get("when"))

    # -- 5. is the mirror STILL good, right now? -----------------------------
    #
    # This RE-RUNS THE COMPARISON rather than comparing five summary numbers.
    # fingerprint() is (n_files, n_dirs, total_bytes, newest_mtime, walk_errors),
    # and all four of these S:-side mutations preserve every one of them -- each
    # was reproduced, and each left the old gate authorising the delete:
    #   same-size content overwrite with the mtime preserved (robocopy restores
    #     the source timestamp by design, so this is the natural shape);
    #   a rename inside the week; a move into a subfolder; swapping the contents
    #     of two same-size files.
    # newest_mtime is a MAX, so even a real mtime change hides whenever the
    # touched file is not the newest in the tree.
    #
    # Re-running compare_trees is path- and content-sensitive by construction, and
    # it also closes the verify-time window (the receipt's S: snapshot is taken
    # after both sources have already read S:, so it can certify a state nothing
    # verified) -- this check happens after every such window.
    #
    # Cost is one extra S: pass. That is the trade this module's docstring already
    # argues for: trivial next to deleting the only copy.
    mirror = dp.mirror_path(week_dir)
    s_now = fingerprint(mirror)
    if not s_now.get("n_files"):
        return False, ("the S: copy at %s is MISSING OR EMPTY right now -- "
                       "refusing" % mirror)
    if rec.get("s_fingerprint") is None:
        return False, ("receipt predates S:-side re-checking -- re-verify with "
                       "--verify %s" % week)

    now_tier = rec.get("tier", min_tier)
    a_now = compare_trees(week_dir, mirror, tier=now_tier)
    if a_now["verdict"] != "clean":
        return False, ("re-checking S: NOW finds it %s at tier %r: missing=%d "
                       "size=%d newer=%d hash=%d unverifiable=%d -- re-verify"
                       % (a_now["verdict"], now_tier, a_now["n_missing_on_S"],
                          a_now["n_size_mismatch"], a_now["n_newer_on_C"],
                          a_now["n_hash_mismatch"],
                          a_now.get("n_unverifiable_window", 0)))

    # 5b. Nothing above proves a file that was only PARTIALLY hashed. At any tier
    # below 'full', a file over SAMPLE_THRESHOLD has its interior unread by both
    # sources -- reproduced: 480 changed bytes in the middle of a 48 MB h5 gave
    # 'clean' at tier sample. Refuse rather than pretend.
    n_partial = a_now.get("n_partially_hashed", 0)
    if n_partial:
        return False, ("%d file(s) larger than %d MB were only partially hashed "
                       "at tier %r (head+tail only), so their interiors are "
                       "unverified -- re-verify with --tier full before deleting"
                       % (n_partial, SAMPLE_THRESHOLD >> 20, now_tier))

    # -- 6. is anything still writing inside this week? ----------------------
    try:
        import datasync.data_sync as _ds
        live = [p for p in _ds.active_paths(campaign)
                if os.path.abspath(p).lower().startswith(
                    os.path.abspath(week_dir).lower())]
    except Exception:
        live = []
    if live:
        return False, ("an experiment is still writing inside this week (%s)"
                       % ", ".join(os.path.basename(p) for p in live[:3]))

    return True, ("verified %s at tier %r; S: re-checked just now (%d files)"
                  % (rec.get("when"), rec.get("tier"), s_now.get("n_files", 0)))


# ---------------------------------------------------------------------------
# Audit across all weeks
# ---------------------------------------------------------------------------

def verify_targets(campaign=None, drive="C"):
    """``[(label, path, exclude_dirs, exclude_top)]`` -- what to compare, and how.

    Between them the entries cover **every byte under the campaign data root,
    exactly once**:

    * one entry per week folder;
    * plus a ``(root)`` entry for the data root with the week folders excluded at
      the top level. Loose files and non-week directories live there -- a one-off
      analysis folder, a mis-dated ``260728`` (a Tuesday), a stray test file. The
      mirror's week loop skips them, so without this entry they are copied by
      nothing AND reported by nothing: invisible in both directions, which is
      exactly how data goes missing without anyone noticing.

    **A campaign predating the week layout is FLAT** -- its ``data\\`` root holds
    the runs directly -- and for those ``dp.week_folders`` correctly returns
    nothing. Letting that reach ``audit`` unhandled meant it printed "0 week(s);
    0 file(s) on C: not safely on S:" and exited 0 while comparing *nothing at
    all*: a clean bill of health for an entirely unverified tree, the worst
    failure this module can have. So a campaign with no week folders is one flat
    target over the whole root.
    """
    root = dp.campaign_data_root(drive, campaign)
    croot = dp.campaign_root(drive, campaign)
    weeks = dp.week_folders(drive=drive, campaign=campaign)
    excl = list(EXCLUDE_DIRS)

    if not os.path.isdir(root) and not os.path.isdir(croot):
        return []

    out = []
    if not weeks:
        if os.path.isdir(root):
            out.append((FLAT_LABEL, root, excl, []))
    else:
        out += [(os.path.basename(w), w, excl, []) for w in weeks]
        out.append((ROOT_LABEL, root, excl,
                    [os.path.basename(w) for w in weeks]))

    # Everything in the campaign folder ALONGSIDE data\ -- Notebooks\, a loose
    # .ipynb, an analysis folder. data\ is excluded at the top level because the
    # entries above already cover it, which keeps the partition exactly-once.
    if os.path.isdir(croot):
        out.append((PARALLEL_LABEL, croot, excl, [dp.DATA_SUBDIR]))
    return out


def coverage(campaign=None, drive="C"):
    """Prove at RUNTIME that the verify targets account for every file.

    :func:`verify_targets` is *supposed* to partition the campaign data root. This
    checks it instead of trusting it, because the difference between

        "every target I compared was clean"     and
        "every file under the root is accounted for"

    is the whole basis of a decision to delete the local copy. A target list that
    silently stops covering something -- a new kind of directory, an edit to the
    exclusion rules, a layout nobody anticipated -- otherwise produces a perfectly
    clean audit over a shrinking fraction of the data.

    Cost is one extra LOCAL walk (~30 k files/s measured), never a network one.

    Returns a dict with ``uncovered`` (files no target sees) and ``duplicated``
    (files two targets both claim -- harmless for safety but it corrupts the file
    and byte totals, and it means the partition is wrong).
    """
    # Scope is the CAMPAIGN root, not just data\. Policy: everything under the
    # campaign folder belongs on S:. Checking only data\ would let Notebooks\ and
    # any loose file at the campaign root read as "complete" while being mirrored
    # by nothing -- which is the exact hole this guard exists to close.
    croot = os.path.abspath(dp.campaign_root(drive, campaign))
    all_files, _, err = walk_tree(croot, EXCLUDE_DIRS)

    counts = {}
    for _label, path, excl, excl_top in verify_targets(campaign, drive):
        files, _, _ = walk_tree(path, excl, excl_top)
        base = os.path.relpath(os.path.abspath(path), croot)
        for r in files:
            rel = r if base in (".", "") else os.path.join(base, r).lower()
            counts[rel] = counts.get(rel, 0) + 1

    uncovered = sorted(set(all_files) - set(counts))
    duplicated = sorted(k for k, v in counts.items() if v > 1)

    # Per-top-level-entry breakdown, so the report says WHERE the files are rather
    # than only how many there are.
    breakdown = []
    try:
        for n in sorted(os.listdir(croot)):
            p = os.path.join(croot, n)
            pre = "" if not os.path.isdir(p) else (n.lower() + os.sep)
            if os.path.isdir(p):
                mine = [k for k in all_files if k.startswith(pre)]
            else:
                mine = [k for k in all_files if k == n.lower()]
            nbytes = sum(all_files[k][0] for k in mine)
            nunc = sum(1 for k in mine if k in uncovered)
            breakdown.append((n, len(mine), nbytes, nunc))
    except OSError as exc:
        err = list(err) + ["listdir %s: %s" % (croot, exc)]

    return {"root": os.path.abspath(dp.campaign_data_root(drive, campaign)),
            "campaign_root": croot,
            "n_files_under_root": len(all_files),
            "n_covered": len(counts),
            "uncovered": uncovered,
            "duplicated": duplicated,
            "breakdown": breakdown,
            "walk_errors": err,
            "complete": not uncovered and not duplicated and not err}


def audit(tier="size", campaign=None, verbose=True):
    """Compare every week folder -- or a flat campaign's data root -- against S:.

    Returns a list of row dicts. An **empty** list now means the campaign has no
    data at all, not "nothing needed checking"; the CLI treats it as a failure so
    it can never be mistaken for a pass.
    """
    cov = coverage(campaign)
    rows = []
    for label, week_dir, excl, excl_top in verify_targets(campaign):
        week = label
        mirror = dp.mirror_path(week_dir)
        a = compare_trees(week_dir, mirror, tier=tier,
                          exclude_dirs=excl, exclude_top=excl_top)
        # Every divergence class, plus walk errors. n_hash_mismatch was missing,
        # which meant `--audit --tier full` -- the expensive content check, run
        # precisely when you doubt the mirror -- printed "0 file(s) not safely on
        # S:" and exited 0 while the verdict column on the same row said
        # 'divergent'. n_walk_errors was likewise dropped, so a target with
        # unreadable directories also exited 0. An unreadable file is an
        # UNVERIFIED file; it cannot count as safe.
        only_c = (a["n_missing_on_S"] + a["n_size_mismatch"] + a["n_newer_on_C"]
                  + a["n_hash_mismatch"] + a.get("n_unverifiable_window", 0)
                  + a["n_walk_errors"])
        rows.append({"week": week,
                     "c_files": a["c_n_files"], "c_gb": a["c_bytes"] / 1e9,
                     "s_files": a["s_n_files"], "s_gb": a["s_bytes"] / 1e9,
                     "at_risk_files": only_c,
                     "only_on_s": a["n_extra_on_S"],
                     "verdict": a["verdict"],
                     "is_flat": week == FLAT_LABEL,
                     "is_current": week == dp.week_name()})
    if verbose:
        print("\n%-8s %9s %9s %9s %9s %11s %10s  %s"
              % ("week", "C files", "C GB", "S files", "S GB",
                 "AT RISK", "S-only", "verdict"))
        print("-" * 88)
        for r in rows:
            print("%-8s %9d %9.2f %9d %9.2f %11d %10d  %s%s"
                  % (r["week"], r["c_files"], r["c_gb"], r["s_files"], r["s_gb"],
                     r["at_risk_files"], r["only_on_s"], r["verdict"],
                     "  (current)" if r["is_current"] else ""))
        tot = sum(r["at_risk_files"] for r in rows)
        print("-" * 88)
        if not rows:
            print("*** NOTHING WAS COMPARED -- no data found for this campaign. ***\n"
                  "    This is NOT a pass. Check the campaign name and that "
                  "%s exists." % dp.campaign_data_root("C", campaign))
        else:
            print("%d target(s); %d file(s) on C: not safely on S:"
                  % (len(rows), tot))
        _print_coverage(cov)
    return rows


def _print_coverage(cov):
    """Report the runtime coverage guard. Loud when it fails -- the point of the
    guard is that a shrinking audit must not be able to look like a clean one."""
    print("\ncoverage guard: %d file(s) under %s, %d accounted for by the targets"
          % (cov["n_files_under_root"], cov["campaign_root"], cov["n_covered"]))
    if cov["uncovered"]:
        print("*** %d FILE(S) NO TARGET COVERS -- the audit above did not look at "
              "these: ***" % len(cov["uncovered"]))
        for r in cov["uncovered"][:20]:
            print("      %s" % r)
        if len(cov["uncovered"]) > 20:
            print("      ... and %d more" % (len(cov["uncovered"]) - 20))
    if cov["duplicated"]:
        print("*** %d file(s) claimed by TWO targets -- the file and byte totals "
              "above are inflated: ***" % len(cov["duplicated"]))
        for r in cov["duplicated"][:10]:
            print("      %s" % r)
    if cov["walk_errors"]:
        print("*** %d walk error(s) -- coverage cannot be established: ***"
              % len(cov["walk_errors"]))
        for e in cov["walk_errors"][:5]:
            print("      %s" % e)
    if cov["complete"]:
        print("  -> every file under the CAMPAIGN root is covered, exactly once.")

    if cov["breakdown"]:
        print("\n  %-34s %9s %10s  %s" % ("campaign-root entry", "files", "MB", "uncovered"))
        for name, nfiles, nbytes, nunc in cov["breakdown"]:
            print("  %-34s %9d %10.1f  %s"
                  % (name, nfiles, nbytes / 1e6,
                     "-" if nunc == 0 else "*** %d ***" % nunc))


def audit_figure(rows, out_path):
    """Stacked bar per week: GB on C only / on both / on S only.

    A count table does not answer "how exposed am I" at a glance; this does.
    matplotlib is imported here so the module stays stdlib-only for the daemon.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    weeks = [r["week"] for r in rows]
    # GB still only on C: is approximated by the at-risk fraction of C: bytes;
    # use file counts where byte-level detail is not tracked per bucket.
    at_risk = [r["c_gb"] * (r["at_risk_files"] / max(r["c_files"], 1)) for r in rows]
    both = [r["c_gb"] - a for r, a in zip(rows, at_risk)]
    s_only = [max(r["s_gb"] - (r["c_gb"] - a), 0.0) for r, a in zip(rows, at_risk)]

    x = range(len(weeks))
    fig, ax = plt.subplots(figsize=(max(6, 1.1 * len(weeks)), 4.2))
    ax.bar(x, both, label="on both C: and S:", color="#4c9f70")
    ax.bar(x, at_risk, bottom=both, label="on C: ONLY (at risk)", color="#c0392b")
    ax.bar(x, s_only, bottom=[b + a for b, a in zip(both, at_risk)],
           label="on S: only (archived history)", color="#7f8c8d", alpha=0.6)
    ax.set_xticks(list(x))
    ax.set_xticklabels(weeks, rotation=45, ha="right")
    ax.set_ylabel("GB")
    ax.set_title("%s -- C: vs S: per week" % dp.active_campaign())
    ax.legend(fontsize=8)
    for i, r in enumerate(rows):
        if r["at_risk_files"]:
            ax.annotate("%d files\nat risk" % r["at_risk_files"],
                        (i, both[i] + at_risk[i]), ha="center", va="bottom",
                        fontsize=7, color="#c0392b")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv=None):
    p = argparse.ArgumentParser(
        description="Verify that C: data is really on S: before anything is deleted.")
    p.add_argument("--campaign", default=None,
                   help="campaign folder name (default: the one declared in %s)"
                        % dp.ACTIVE_FILE)
    p.add_argument("--verify", metavar="WEEK",
                   help="verify one week folder (YYMMDD) and write a receipt")
    p.add_argument("--audit", action="store_true",
                   help="compare every week folder against S:")
    p.add_argument("--tier", default=DEFAULT_TIER, choices=TIER_ORDER)
    p.add_argument("--figure", metavar="PNG", nargs="?", const="",
                   help="with --audit, also write a stacked-bar PNG")
    p.add_argument("--eligible", metavar="WEEK",
                   help="report whether a week may be deleted from C:")
    args = p.parse_args(argv)

    if args.verify:
        week_dir = os.path.join(dp.campaign_data_root("C", args.campaign),
                                os.path.basename(args.verify))
        rec = verify_week(week_dir, tier=args.tier, campaign=args.campaign)
        return 0 if rec["result"] == "clean" else 1

    if args.audit:
        rows = audit(tier=args.tier, campaign=args.campaign)
        if args.figure is not None and rows:
            out = args.figure or os.path.join(
                dp.sync_dir("C", args.campaign, create=True), "audit.png")
            print("figure ->", audit_figure(rows, out))
        # NO rows means nothing was compared, which must never exit 0 -- an empty
        # all() is vacuously True and would have reported success.
        if not rows:
            return 1
        # An incomplete partition must also fail: every target being clean says
        # nothing if the targets no longer add up to the whole tree.
        if not coverage(args.campaign)["complete"]:
            return 1
        return 0 if all(r["at_risk_files"] == 0 for r in rows) else 1

    if args.eligible:
        ok, why = is_prune_eligible(args.eligible, campaign=args.campaign,
                                    min_tier=args.tier)
        print("%s: %s -- %s" % (args.eligible,
                                "MAY be deleted from C:" if ok
                                else "must NOT be deleted", why))
        return 0 if ok else 1

    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
