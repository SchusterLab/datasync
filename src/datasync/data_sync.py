"""Background C: -> S: mirror for the current campaign.

A detached robocopy daemon keeps every week folder on the network share. It is
**additive only**: it copies and it never deletes, on either side. Nothing in
this module can remove data from C: -- ``prune_cmd`` only *prints* a command for
you to run, and only once datasync/data_verify.py has produced a clean receipt.

Why a daemon rather than a copy at the end of each experiment
------------------------------------------------------------
There is no single reliable "end of experiment" hook: saves are driven from the
notebook and figures are written from ~55 scattered ``plt.savefig`` sites. A
daemon that mirrors the whole week folder catches all of them without touching
any experiment code, and it survives a kernel restart. ``sync_now()`` (a file
touch, microseconds) is the "flush now" signal experiments send; it is
idempotent and coalesced, so calling it repeatedly mid-run is free.

State, under ``<campaign data root>\\_sync\\``::

    daemon.lock   pid of the running daemon
    heartbeat     mtime == daemon last alive (kept fresh DURING a long pass)
    sync_now      trigger file; presence means "start a pass now"
    sync.log      robocopy output, appended

Startup contract
----------------
``ensure_daemon()`` is O(1) -- it reads one file's mtime. It deliberately does
NOT diff the trees: enumerating the Pt2 campaign took 139 s for 2.43 M files, and
a startup check must never do that. Use datasync/data_verify.py ``--audit`` when
you want the real answer.
"""

import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

import datasync.data_paths as dp
import datasync.data_verify as dv

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# POLL_S does three jobs at once, and the third one carries a hard constraint:
#   1. idle wake interval -- how fast a sync_now() trigger is noticed
#   2. heartbeat cadence in the idle loop
#   3. heartbeat cadence DURING a pass (the communicate() timeout in _robocopy)
# Because of (3) it must stay well under HEARTBEAT_STALE_S, or a live multi-hour
# pass looks dead to ensure_daemon and a SECOND daemon is launched alongside it.
# Keep the ratio at >= 3x. Only (1) is a latency the user can feel, and a flush
# arriving up to 30 s after save_data is invisible against a minutes-to-hours run.
POLL_S = 30.0            # daemon wake interval (also the heartbeat cadence)
INTERVAL_S = 300.0       # max seconds between passes with no trigger
HEARTBEAT_STALE_S = 90.0 # no heartbeat for this long => daemon considered dead
MT = 32                  # robocopy threads; the cost here is per-FILE over SMB

# Never /MIR. /MIR (and /PURGE) DELETE on the destination, which would wipe the
# S: history that C: no longer holds -- including every week already pruned.
# /E   copy subdirs including empty ones
# /XO  skip when the destination is the same age or newer (cheap incremental)
# /FFT 2-second timestamp granularity, for the SMB share
# /R:1 /W:1  do not stall on the live .h5 being appended; the next pass fixes it
ROBOCOPY_FLAGS = ["/E", "/XO", "/FFT", "/MT:%d" % MT, "/R:1", "/W:1",
                  "/NP", "/NFL", "/NDL", "/NJH"]


# Every robocopy switch that can DESTROY data, and the only reason this list
# exists: exclusion operands (/XD, /XF) are read out of marker files written by
# another process, and a token in that position is parsed by robocopy as a SWITCH,
# not as a filename. That was reproduced -- passing "/MIR" as an exclusion operand
# made robocopy purge a destination-only file.
#
#   /MIR /PURGE  delete on the DESTINATION anything not in the source -- this
#                would wipe the S: history that C: no longer holds, including
#                every week already pruned
#   /MOV /MOVE   delete on the SOURCE after copying -- this would delete the
#                user's live data off C:
#
# _assert_nondestructive is the last line of defence before Popen. It is cheap,
# it runs on every invocation, and it converts "we are careful not to pass /MIR"
# into "no code path can pass /MIR".
DESTRUCTIVE_FLAGS = ("/MIR", "/PURGE", "/MOV", "/MOVE")


def _assert_nondestructive(cmd):
    """Raise unless *cmd* is incapable of deleting anything. Never bypassed.

    Normalises the switch prefix before comparing. **robocopy accepts "-MIR" as
    well as "/MIR"** -- measured: a "-MIR" token in an exclusion operand purged a
    destination-only file (rc=2) while a check that only looked for "/MIR" saw
    nothing wrong. Matching the literal "/" spelling is not enough.
    """
    for tok in cmd:
        t = str(tok).strip().upper().lstrip("/-")
        base = "/" + t.split(":", 1)[0]
        if base in DESTRUCTIVE_FLAGS:
            raise ValueError(
                "REFUSING to run robocopy: argument %r is the destructive switch "
                "%s. This backup is additive-only and must never delete on either "
                "side. Command was: %s" % (tok, base, " ".join(map(str, cmd))))
    return cmd


def _clean_exclusions(items, kind):
    """Keep only exclusion operands that cannot be mistaken for a switch.

    The boundary where foreign bytes enter the command line. A marker file is
    written by our own code, so a value that fails these tests means the file is
    corrupt -- dropping it is strictly safer than passing it on, because the
    failure modes are (a) a leading '/' parsed as a switch and (b) a NUL byte,
    which makes subprocess raise ValueError on EVERY pass thereafter, wedging the
    backup behind a still-green heartbeat.
    """
    out = []
    for it in (items or ()):
        s = str(it)
        if not s or "\x00" in s or s.lstrip().startswith(("/", "-")):
            print("[sync] IGNORING corrupt %s exclusion %r" % (kind, s[:80]))
            continue
        out.append(s)
    return out


def _state(name, campaign=None, create=False):
    return os.path.join(dp.sync_dir("C", campaign, create=create), name)


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a"):
        pass
    os.utime(path, None)


def _age(path):
    """Seconds since *path* was last touched, or None if it does not exist."""
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# One robocopy pass
# ---------------------------------------------------------------------------

def _parse_file_line(line):
    """``(size_bytes, path)`` for a robocopy per-file line, else None.

    With ``/NFL`` dropped and ``/BYTES`` on, a copied file prints one
    TAB-separated line (format verified empirically, not assumed)::

        '\\t    New File  \\t\\t    1500\\tC:\\\\path\\\\to\\\\file.h5'

    i.e. fields ``['', status, '', size, path]``. Two observed properties make
    counting these lines a *correct* progress measure, not an approximate one:

    * only files robocopy ACTUALLY copies get a line -- skipped / same-age files
      print nothing at all, so the count cannot drift above the real progress;
    * the summary rows (``'   Files :   9   9   0 ...'``) are space-aligned, not
      tab-separated, so they can never be mistaken for a file line.

    Anything unparseable returns None and is treated as log text, which is what
    keeps robocopy's ``ERROR n (0x...)`` lines out of the count and in the log.
    """
    f = line.split("\t")
    if len(f) < 5:
        return None
    try:
        return int(f[3].strip()), f[4]
    except ValueError:
        return None


def _progress_bar(total, desc):
    """tqdm over FILES. *total* None -> a count-up with rate but no ETA."""
    from tqdm import tqdm   # local import: the detached daemon never needs it
    return tqdm(total=total, desc=desc, unit="file", unit_scale=True,
                smoothing=0.05, dynamic_ncols=True, leave=True)


def _would_copy_count(src, dst, exclude_dirs=(), exclude_files=()):
    """How many files a real pass would copy, from a read-only ``/L`` pass.

    Runs through :func:`_robocopy` so it uses the SAME flags as the copy it is
    the denominator for. ``/XO`` is what decides whether a file gets copied, so a
    count taken under a different comparison -- e.g. data_verify.robocopy_pending,
    which drops ``/XO`` on purpose because a verifier wants the stricter
    criterion -- is a different, larger number, and a bar built on it could never
    reach 100%. The same applies to the exclusions: pass the SAME *exclude_dirs*
    and *exclude_files* the copy will use, or the count includes files the copy
    then skips and the bar stalls short of the end.

    Returns None when the count is unavailable; the caller then shows a count-up
    bar rather than a percentage that is quietly wrong.
    """
    rc, failed, out = _robocopy(src, dst, log_path=None, dry_run=True,
                               exclude_dirs=exclude_dirs,
                               exclude_files=exclude_files)
    if failed:
        return None
    summ = dv._parse_summary(out)
    return summ.get("copied") if summ.get("ok") else None


def _robocopy(src, dst, log_path=None, dry_run=False, heartbeat=None,
              exclude_dirs=(), exclude_files=(), progress=False,
              progress_total=None, progress_desc=None):
    """Additive robocopy of *src* -> *dst*. Returns (rc, failed_bool).

    robocopy's exit code is a BITMASK, not a status: 0 = nothing to do, 1 = files
    copied, 2 = extras present, 3 = both, ... Only ``>= 8`` means a real failure.
    Checking ``rc != 0`` would report every successful copy as an error.

    *heartbeat* is refreshed while the copy runs, so a multi-hour DUST week does
    not look like a dead daemon and get a second one launched alongside it.

    *progress* streams robocopy's per-file output into a tqdm bar instead of
    waiting for the process to exit. It costs the ``/NFL`` flag, and robocopy
    cannot be verbose on stdout while staying quiet in ``/LOG+`` -- one flag
    controls both -- so progress mode drops ``/LOG+ /TEE`` and appends the
    non-file lines to *log_path* itself once the pass ends. That is the same
    content the log held before (under ``/NFL`` it only ever contained the
    summary tables), so nothing is lost. The bar advances in bursts: ``/MT``
    flushes output per directory, not per file.
    """
    cmd = ["robocopy", os.path.abspath(src), os.path.abspath(dst)]
    if progress:
        # /BYTES so the per-file size field is a plain integer to parse.
        cmd += [f for f in ROBOCOPY_FLAGS if f != "/NFL"] + ["/BYTES"]
    else:
        cmd += ROBOCOPY_FLAGS
    # Sanitise at the boundary: these operands originate in marker FILES written
    # by another process, and robocopy parses a leading '/' OR '-' token in this
    # position as a SWITCH. Measured: a marker containing "-MIR" became /XF -MIR
    # and purged a destination-only file.
    for d in _clean_exclusions(exclude_dirs, "dir"):
        cmd += ["/XD", d]           # by NAME, at any depth -- matches walk_tree
    for f in _clean_exclusions(exclude_files, "file"):
        cmd += ["/XF", f]           # a live .h5 sits in the week folder, not in
                                    # a subfolder, so /XD alone cannot shield it
    if dry_run:
        cmd.append("/L")
    if log_path and not progress:
        # /TEE as well as /LOG+ -- /LOG+ alone sends ALL output to the file and
        # leaves stdout empty, so the summary could not be parsed and every
        # copied/skipped count came back None.
        cmd += ["/LOG+:%s" % log_path, "/TEE"]
    # CREATE_NO_WINDOW, or every pass flashes a console window on the desktop.
    # The daemon is launched DETACHED_PROCESS, so it has NO console of its own;
    # when a process with no console spawns a console application, Windows
    # allocates a brand new console WINDOW for it. Redirecting stdout to a pipe
    # does not prevent that -- the window still appears, just empty. At
    # INTERVAL_S that is a window popping up every few minutes, forever.
    # Must not be combined with DETACHED_PROCESS/CREATE_NEW_CONSOLE (mutually
    # exclusive); this call passes no other console flags, so it is safe here.
    # Last line of defence: no code path may launch a robocopy that can delete.
    _assert_nondestructive(cmd)
    try:
        # errors="replace": one undecodable byte in one of millions of paths must
        # not raise mid-copy and abandon the pass (robocopy would keep running,
        # detached from anything watching it).
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors="replace",
                                creationflags=getattr(subprocess,
                                                      "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        return None, True, "cannot run robocopy: %s" % exc

    if not progress:
        out = []
        while True:
            try:
                rest = proc.communicate(timeout=POLL_S)[0]
                if rest:
                    out.append(rest)
                break
            except subprocess.TimeoutExpired:
                if heartbeat:
                    _touch(heartbeat)
        rc = proc.returncode
        return rc, (rc is None or rc >= 8), "".join(out)[-4000:]

    # Streaming mode. Only non-file lines are retained, so memory stays flat over
    # millions of files; a bounded deque keeps the tail, and the summary table --
    # the only part anyone parses -- is always the last thing printed.
    kept = collections.deque(maxlen=2000)
    n_files, n_bytes, last_hb = 0, 0, time.time()
    bar = _progress_bar(progress_total, progress_desc
                        or os.path.basename(os.path.abspath(src)))
    try:
        for line in proc.stdout:
            hit = _parse_file_line(line.rstrip("\n"))
            if hit is None:
                kept.append(line)
                continue
            n_files += 1
            n_bytes += hit[0]
            if n_files % 256 == 0:      # cheap: redraw the postfix rarely
                bar.set_postfix_str("%.1f GB" % (n_bytes / 2.0 ** 30),
                                    refresh=False)
            bar.update(1)
            if heartbeat and time.time() - last_hb > POLL_S:
                _touch(heartbeat)
                last_hb = time.time()
        proc.wait()
    finally:
        bar.set_postfix_str("%.1f GB" % (n_bytes / 2.0 ** 30), refresh=False)
        bar.close()
    out = "".join(kept)
    if log_path:
        try:
            with open(log_path, "a") as fid:
                fid.write(out)
        except OSError as exc:
            print("[sync] could not append to %s: %s" % (log_path, exc))
    rc = proc.returncode
    return rc, (rc is None or rc >= 8), out[-4000:]


def sync_once(campaign=None, dry_run=False, verbose=True, heartbeat=None,
              progress=False):
    """One additive pass over every week folder. Returns a list of per-week dicts.

    Every week is visited each pass, not just the current one, so weeks written
    while the daemon was down are picked up with no separate backlog logic.

    *progress* shows a per-week bar (foreground use only -- the detached daemon
    has no console to draw one on).

    The result is published to last_pass.json in a **finally**, carrying any
    exception. Publishing only on success meant a pass that threw left the last
    GOOD numbers in place while _daemon_loop swallowed the error and kept the
    heartbeat fresh -- so the widget showed a green 'up to date' backup that had
    not copied anything since the failure began.
    """
    results = []
    err = None
    try:
        results = _sync_once_inner(campaign, dry_run, verbose, heartbeat,
                                   progress)
        return results
    except Exception as exc:
        err = repr(exc)
        raise
    finally:
        _write_last_pass(results, campaign, dry_run, error=err)


def _sync_once_inner(campaign=None, dry_run=False, verbose=True, heartbeat=None,
                     progress=False):
    log = _state("sync.log", campaign, create=True)
    results = []
    weeks = dp.week_folders(drive="C", campaign=campaign)

    # Whatever a running experiment has claimed is excluded from THIS pass and
    # mirrored by a later one. This first read is only for the summary line --
    # each target below re-reads, because robocopy's filters are fixed at process
    # start, so a claim made after a pass begins protects nothing for the rest of
    # it (tens of seconds for the daemon, HOURS for a backlog). Reported rather
    # than skipped silently: a folder quietly left out is indistinguishable from
    # one that was copied.
    claimed = active_paths(campaign)
    xd, xf = _exclusions_for(claimed)
    excl_d = list(dv.EXCLUDE_DIRS) + xd

    if verbose:
        print("[sync] %d week folder(s)%s"
              % (len(weeks), "  (DRY RUN)" if dry_run else ""))
        if claimed:
            print("[sync]   NOT copying %d path(s) an experiment is writing "
                  "(next pass picks them up):" % len(claimed))
            for p in claimed:
                print("[sync]     %s" % p)
    for week_dir in weeks:
        dst = dp.mirror_path(week_dir)
        t0 = time.time()
        # Fresh claim snapshot per target -- see the note above.
        claimed = active_paths(campaign)
        xd, xf = _exclusions_for(claimed)
        excl_d = list(dv.EXCLUDE_DIRS) + xd
        # A /L count first, so the bar has a denominator. Skipped for a dry run,
        # which IS that counting pass -- counting it twice would just double it.
        total = None
        if progress and not dry_run:
            total = _would_copy_count(week_dir, dst, exclude_dirs=excl_d,
                                      exclude_files=xf)
        rc, failed, out = _robocopy(week_dir, dst, log_path=log,
                                    dry_run=dry_run, heartbeat=heartbeat,
                                    exclude_dirs=excl_d, exclude_files=xf,
                                    progress=progress, progress_total=total,
                                    progress_desc=os.path.basename(week_dir))
        summ = dv._parse_summary(out)
        row = {"week": os.path.basename(week_dir), "rc": rc, "failed": failed,
               "seconds": round(time.time() - t0, 1),
               "copied": summ.get("copied"), "skipped": summ.get("skipped"),
               "robocopy_failed_files": summ.get("failed"),
               "deferred_active": list(claimed)}
        results.append(row)
        if verbose:
            print("[sync]   %s  rc=%s  copied=%s skipped=%s  %.1fs%s"
                  % (row["week"], rc, row["copied"], row["skipped"],
                     row["seconds"], "   *** FAILED ***" if failed else ""))

    # Everything under the data root that the week loop does NOT reach: loose
    # files and any non-week directory (a one-off analysis folder, a mis-dated
    # folder, a stray test file). week_folders() only matches YYMMDD Mondays, so
    # without this pass those are mirrored by nothing -- and the week-based audit
    # reported nothing either, so they were invisible in both directions.
    #
    # The week folders are excluded by FULL PATH (a /XD argument containing a
    # separator is an exact path), so this pass does not re-walk them and each
    # byte under the root is still copied exactly once.
    root = dp.campaign_data_root("C", campaign)
    if os.path.isdir(root):
        t0 = time.time()
        rc, failed, out = _robocopy(
            root, dp.mirror_path(root), log_path=log, dry_run=dry_run,
            heartbeat=heartbeat,
            exclude_dirs=excl_d + [os.path.abspath(w) for w in weeks],
            exclude_files=xf, progress=False)
        summ = dv._parse_summary(out)
        row = {"week": dv.ROOT_LABEL, "rc": rc, "failed": failed,
               "seconds": round(time.time() - t0, 1),
               "copied": summ.get("copied"), "skipped": summ.get("skipped"),
               "robocopy_failed_files": summ.get("failed"),
               "deferred_active": list(claimed)}
        results.append(row)
        if verbose:
            print("[sync]   %-8s rc=%s  copied=%s skipped=%s  %.1fs%s"
                  % (row["week"], rc, row["copied"], row["skipped"],
                     row["seconds"], "   *** FAILED ***" if failed else ""))

    # POLICY: everything under the CAMPAIGN root goes to S:, not only data\.
    # That means Notebooks\, a loose .ipynb, any analysis folder sitting beside
    # data\. data\ itself is excluded by FULL PATH here because the passes above
    # already cover it, so each byte is still copied exactly once.
    #
    # Note this mirrors notebooks verbatim, including .ipynb_checkpoints and any
    # embedded outputs; /XO means a notebook is re-copied only after it is saved.
    croot = dp.campaign_root("C", campaign)
    if os.path.isdir(croot):
        t0 = time.time()
        rc, failed, out = _robocopy(
            croot, dp.mirror_path(croot), log_path=log, dry_run=dry_run,
            heartbeat=heartbeat,
            exclude_dirs=excl_d + [os.path.abspath(root)],
            exclude_files=xf, progress=False)
        summ = dv._parse_summary(out)
        row = {"week": dv.PARALLEL_LABEL, "rc": rc, "failed": failed,
               "seconds": round(time.time() - t0, 1),
               "copied": summ.get("copied"), "skipped": summ.get("skipped"),
               "robocopy_failed_files": summ.get("failed"),
               "deferred_active": list(claimed)}
        results.append(row)
        if verbose:
            print("[sync]   %-15s rc=%s  copied=%s skipped=%s  %.1fs%s"
                  % (row["week"], rc, row["copied"], row["skipped"],
                     row["seconds"], "   *** FAILED ***" if failed else ""))

    return results


def _write_last_pass(results, campaign=None, dry_run=False, error=None):
    """Publish this sweep's per-target results as JSON. Never raises.

    Exists because the sweep's outcome cannot be recovered from sync.log by
    anything outside this process. The log is APPENDED to once per target, so a
    reader has no way to tell which summaries belong to the current sweep -- and
    taking the last one reports only the final target, which is the campaign-root
    pass and normally copies nothing. A status widget built on that would show
    "0 copied" while the week pass had just copied hundreds of files.

    Written atomically into _sync/, which is excluded from the mirror, so it can
    never become part of what the verifier compares.
    """
    try:
        payload = {
            "when": datetime.datetime.now().isoformat(timespec="seconds"),
            "unix": time.time(),
            "dry_run": bool(dry_run),
            "targets": [{k: r.get(k) for k in
                         ("week", "rc", "failed", "seconds", "copied", "skipped",
                          "robocopy_failed_files")} for r in results],
            "copied": sum(r.get("copied") or 0 for r in results),
            "skipped": sum(r.get("skipped") or 0 for r in results),
            "failed_files": sum(r.get("robocopy_failed_files") or 0 for r in results),
            "error": error,
            # An exception mid-pass is a FAILED pass whatever the per-target rows
            # say -- some targets may never have run at all.
            "any_failed": bool(error) or any(r.get("failed") for r in results),
            "seconds": round(sum(r.get("seconds") or 0 for r in results), 1),
            "deferred_active": (results[0].get("deferred_active") if results else []),
        }
        path = _state("last_pass.json", campaign, create=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fid:
            json.dump(payload, fid, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass          # a status file must never be able to fail a sync pass


# ---------------------------------------------------------------------------
# The trigger ("write to S: at the end of the experiment")
# ---------------------------------------------------------------------------

def sync_now(campaign=None):
    """Ask the daemon to start a pass. Non-blocking; safe to call constantly.

    Just touches a file, so it costs microseconds and cannot fail an
    acquisition. Called from ExperimentClass2.save_data, so every real run
    flushes to S: without any experiment needing to know about this module.
    """
    try:
        _touch(_state("sync_now", campaign, create=True))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Active-path coordination: never copy what the experiment is writing
# ---------------------------------------------------------------------------
#
# robocopy opens source files WITHOUT FILE_SHARE_WRITE. While it is copying a
# file, the experiment's own append to that same file fails with
# PermissionError/errno 13. Measured directly: a reader holding the file with
# FILE_SHARE_READ blocks an append; one holding FILE_SHARE_READ|FILE_SHARE_WRITE
# does not.
#
# ROBOCOPY_FLAGS' "/R:1 /W:1 do not stall on the live .h5 being appended" handled
# only the OTHER direction -- robocopy failing to READ a file the experiment has
# open. This is the missing half, and it is the more serious one, because it does
# not degrade the mirror, it CRASHES THE MEASUREMENT.
#
# Mechanism: the experiment marks the path it is writing; each pass excludes
# every live marker (/XD for a directory, /XF for a file) and picks that path up
# on a later pass once the run has finished. Markers live under _sync/, which
# dv.EXCLUDE_DIRS already keeps out of both the mirror and the verifier, so they
# cannot pollute either side.
#
# Liveness is the marker's own mtime, refreshed by the writer -- the same
# heartbeat pattern the daemon itself uses. Deliberately NOT os.kill(pid, 0):
# on Windows os.kill calls TerminateProcess for ANY signal, so a "is it alive"
# probe written that way would KILL the running experiment.

ACTIVE_SUBDIR = "active"

# A marker not refreshed for this long is ignored and deleted, so a run that dies
# without clearing its marker cannot keep its folder off the mirror for ever.
# Raise it if an experiment can go longer than this between writes.
ACTIVE_STALE_S = 900.0


def _active_dir(campaign=None, create=False):
    p = os.path.join(dp.sync_dir("C", campaign, create=create), ACTIVE_SUBDIR)
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def _marker_name(abs_path):
    """Marker filename for an absolute path.

    Basename for readability plus a short digest of the full path, so two runs
    with the same folder name in different weeks cannot share one marker (which
    would let clearing one un-protect the other).
    """
    base = os.path.basename(abs_path.rstrip("\\/")) or "root"
    digest = hashlib.sha1(abs_path.lower().encode("utf-8")).hexdigest()[:10]
    return "%s_%s.active" % (base, digest)


_RUN_RE = re.compile(r"^(?P<prefix>.+?)_(?P<idx>\d{2,})_")


def _run_id(abs_path):
    """``(prefix, index)`` for a run path like ``<PREFIX>_02568_<name>``, else None."""
    m = _RUN_RE.match(os.path.basename(abs_path.rstrip("\\/")))
    if not m:
        return None
    return m.group("prefix"), int(m.group("idx"))


def _read_marker(marker_path):
    """``(claimed_path, pid)`` from a marker file, or (None, None)."""
    try:
        with open(marker_path) as fid:
            claimed = fid.readline().strip()
            pid = int((fid.readline() or "0").strip() or 0)
        return (claimed or None), pid
    except Exception:
        return None, None


def _supersede_earlier(abs_path, campaign=None):
    """Release THIS process's claims on earlier runs. Returns how many it freed.

    Advancing to run N is proof that run N-1 has finished, so N-1's folder is
    safe to mirror. That makes the run index itself the end-of-run signal, which
    matters because there is no reliable end-of-run hook to hang one on -- the
    reason this whole module is a daemon rather than a post-run copy.

    Restricted to markers written by the SAME pid, the same run prefix, and the
    same PARENT DIRECTORY.

    Another kernel acquiring into the same campaign can legitimately hold a lower
    index that is still live; releasing that would put robocopy back onto a file
    it is actively writing, which is the exact crash this mechanism prevents.

    The directory restriction is there for the same reason. **Run numbers are
    per-folder** (data_paths.folder_max_index counts indices claimed directly
    inside one folder), so an index only orders runs that share a folder. A DUST
    run numbers its child measurements inside its OWN ``<run>\\`` folder, from
    zero, and a few hundred children later they are numbering above the parent's
    week-folder index -- H11_00412's children reached H11_01283. Comparing those
    two numbers compares two different counters, and the comparison then "proves"
    the parent finished while it is still writing: every child that started
    deleted the parent's claim, handed robocopy the live ``npz\\H11_00412_*.npz``,
    and killed the run on PermissionError. Measured on 2026-08-21: the parent's
    exclusion was torn down and rebuilt on alternate passes for the whole run.
    """
    mine = _run_id(abs_path)
    if mine is None:
        return 0                    # not a run path -- nothing to compare against
    prefix, idx = mine
    mine_dir = os.path.dirname(os.path.abspath(abs_path)).lower()
    me = os.getpid()
    freed = 0
    d = _active_dir(campaign)
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for n in names:
        p = os.path.join(d, n)
        claimed, pid = _read_marker(p)
        if not claimed or pid != me:
            continue
        other = _run_id(claimed)
        if other is None or other[0] != prefix or other[1] >= idx:
            continue
        if os.path.dirname(os.path.abspath(claimed)).lower() != mine_dir:
            continue                # different folder = different counter
        try:
            os.remove(p)
            freed += 1
        except OSError:
            pass
    return freed


def mark_active(path, campaign=None):
    """Claim *path*: the mirror will not copy it until it is released or goes stale.

    One small file write, so it is cheap enough to call on every point of a scan
    -- and calling it repeatedly is exactly how the claim stays fresh. **Never
    raises**: coordination is best-effort and must not be able to fail an
    acquisition that is otherwise fine.

    A NEW claim also supersedes this process's claims on earlier runs and fires
    sync_now(), so finishing run N-1 by starting run N gets N-1 mirrored within
    POLL_S with no explicit end-of-run call anywhere.
    """
    try:
        ap = os.path.abspath(path)
        m = os.path.join(_active_dir(campaign, create=True), _marker_name(ap))
        is_new = not os.path.exists(m)
        # ATOMIC. open(m, "w") truncates first, so a refresh left a window in
        # which the daemon could read an EMPTY marker, treat the claim as absent,
        # and hand the live folder straight back to robocopy -- reintroducing the
        # crash this mechanism exists to prevent, and only ever under load, when
        # refreshes are frequent. temp + os.replace makes the swap indivisible.
        tmp = m + ".tmp%d" % os.getpid()
        with open(tmp, "w") as fid:
            fid.write("%s\n%d\n%s\n"
                      % (ap, os.getpid(),
                         datetime.datetime.now().isoformat(timespec="seconds")))
        os.replace(tmp, m)
        # Only on a NEW claim: a refresh runs once per scan point, and rescanning
        # the marker directory that often would be pure waste.
        if is_new and _supersede_earlier(ap, campaign):
            sync_now(campaign)
        return m
    except Exception:
        return None


def clear_active(path, campaign=None):
    """Release *path* and ask the daemon to copy it NOW. Never raises.

    Releasing also fires sync_now(), so the freed folder is mirrored within
    POLL_S rather than waiting up to INTERVAL_S for the next scheduled pass. The
    trigger is fired even if no marker was there to remove: the caller's intent is
    "I have finished writing, go copy it", and that is worth acting on either way.
    """
    removed = False
    try:
        os.remove(os.path.join(_active_dir(campaign),
                               _marker_name(os.path.abspath(path))))
        removed = True
    except Exception:
        pass
    sync_now(campaign)          # already non-raising
    return removed


def active_paths(campaign=None, stale_s=None):
    """Absolute paths currently claimed by a writer, oldest marker first.

    Stale markers are both ignored and removed here, so the set self-heals after
    a crashed run with no separate cleanup step.
    """
    stale = ACTIVE_STALE_S if stale_s is None else stale_s
    out = []
    try:
        names = sorted(os.listdir(_active_dir(campaign)))
    except OSError:
        return out
    for n in names:
        # Only real markers are claims. A failed atomic refresh can leave a
        # ".tmp<pid>" behind; reading those as claims made them unreleasable,
        # because clear_active only ever removes the canonical marker name.
        if not n.endswith(".active"):
            continue
        p = os.path.join(_active_dir(campaign), n)
        age = _age(p)
        if age is None:
            continue
        if age < 0:
            # Mtime in the FUTURE (clock skew, a marker copied from another
            # machine, a restored file). The age is negative, so `age > stale`
            # can never fire and the claim would pin its path off the backup for
            # ever. Clamping the ARITHMETIC to 0 does not help -- it just makes
            # the claim permanently fresh instead of permanently negative.
            #
            # Re-stamp the marker to now instead: the claim stays live for one
            # normal ACTIVE_STALE_S window (so a genuinely running experiment
            # keeps its protection and the crash window does not reopen), and
            # then expires like any other. Self-healing, and it cannot pin.
            print("[sync] marker %s has a FUTURE mtime (%.0f s ahead) -- "
                  "re-stamping to now" % (n, -age))
            try:
                os.utime(p, None)
            except OSError:
                pass
            age = 0.0
        if age > stale:
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        try:
            with open(p) as fid:
                claimed = fid.readline().strip()
        except OSError:
            continue
        if claimed:
            out.append(claimed)
    return out


def _run_glob(abs_path):
    """``<PREFIX>_<index>_*`` for a run path, else None.

    Built from the LITERAL matched text, not from a reformatted integer, so a
    campaign whose indices are not 5 digits still produces a pattern that matches.
    """
    m = _RUN_RE.match(os.path.basename(abs_path.rstrip("\\/")))
    if not m:
        return None
    return "%s_%s_*" % (m.group("prefix"), m.group("idx"))


def _exclusions_for(paths):
    """Split claimed paths into robocopy ``/XD`` (dirs) and ``/XF`` (files+globs).

    **A run does not write only inside its own folder.** Its ``.h5`` sits at the
    top of the week folder and its artefacts land in the week's SHARED ``npz\\``,
    ``png\\``, ``cfg\\`` and ``summary_data\\`` directories -- mDUST.py:1675/1731,
    mResSpec.py:1333, mQubitPulse.py:3282 all write straight into ``npz\\``.

    Claiming only the run folder and the .h5 therefore left every npz/png/cfg
    write unprotected, and a live run duly died on
    ``npz\\H10_02576_..._pumpA.npz``. So each claimed run also contributes the
    wildcard ``<PREFIX>_<index>_*``, which covers all of its artefacts wherever
    they live. It cannot over-match: the run index is unique within a campaign.

    Verified empirically -- ``/XF`` with a wildcard matches at any depth, excluded
    the claimed run's .h5, npz, png and cfg, and left a neighbouring run's files
    to be copied.
    """
    xd, xf = [], []
    for p in paths:
        (xd if os.path.isdir(p) else xf).append(p)
        glob_pat = _run_glob(p)
        if glob_pat and glob_pat not in xf:
            xf.append(glob_pat)
    return xd, xf


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

def _daemon_loop(campaign=None):
    """Run until killed. Not called directly -- launched detached by ensure_daemon."""
    hb = _state("heartbeat", campaign, create=True)
    trigger = _state("sync_now", campaign)
    lock = _state("daemon.lock", campaign)
    with open(lock, "w") as fid:
        fid.write("%d\n%s\n" % (os.getpid(), datetime.datetime.now().isoformat()))

    last_pass = 0.0
    while True:
        _touch(hb)
        triggered = os.path.exists(trigger)
        due = (time.time() - last_pass) >= INTERVAL_S
        if triggered or due:
            # Clear the trigger BEFORE the pass, so anything written *during*
            # the pass sets it again and gets picked up next time round instead
            # of being swallowed by this one.
            if triggered:
                try:
                    os.remove(trigger)
                except OSError:
                    pass
            try:
                sync_once(campaign=campaign, verbose=True, heartbeat=hb)
            except Exception as exc:               # never let the daemon die
                print("[sync] pass failed: %r" % (exc,), flush=True)
            last_pass = time.time()
            _touch(hb)
        time.sleep(POLL_S)


def daemon_alive(campaign=None):
    """(bool, age_seconds_or_None) from the heartbeat alone -- O(1)."""
    age = _age(_state("heartbeat", campaign))
    return (age is not None and age < HEARTBEAT_STALE_S), age


def ensure_daemon(campaign=None, outer_folder=None, verbose=True):
    """Start the mirror daemon if it is not already running. **The startup call.**

    O(1): one mtime read. Prints a single status line, warns if *outer_folder*
    belongs to a different week than today (a kernel left running across a Monday
    midnight keeps writing into last week's folder), and reports C: free space
    plus any week that a clean receipt says is reclaimable.
    """
    alive, age = daemon_alive(campaign)
    if alive:
        if verbose:
            print("[sync] daemon alive (heartbeat %.0fs ago)" % age)
    else:
        pid = _launch_detached(campaign)
        if verbose:
            if pid:
                print("[sync] started mirror daemon (pid %d)%s"
                      % (pid, "" if age is None
                         else "; previous heartbeat was %.0fs stale" % age))
            else:
                print("[sync] WARNING could not start the mirror daemon -- "
                      "data will stay on C: only")
    if verbose:
        _print_status_extras(campaign, outer_folder)
    return alive


def _launch_detached(campaign=None):
    """Launch the daemon as a detached process that outlives this kernel."""
    d = dp.sync_dir("C", campaign, create=True)
    out = open(os.path.join(d, "daemon.out"), "a")
    cmd = [sys.executable, "-m", "datasync.data_sync", "--daemon"]
    if campaign:
        cmd += ["--campaign", campaign]
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    env = dict(os.environ)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    # The child re-imports data_paths, so pass the LIVE root values through
    # rather than letting it fall back to the module defaults -- otherwise an
    # overridden parent would spawn a daemon mirroring a different tree.
    env[dp._ENV_C] = dp.ROOT_C
    env[dp._ENV_S] = dp.ROOT_S
    # Resolved HERE, in the parent, and passed explicitly. The child must not be
    # left to resolve it for itself: it would re-read the state file, so a
    # campaign switched between launch and the child's first pass would silently
    # move the daemon onto a different tree.
    env[dp._ENV_CAMPAIGN] = dp.active_campaign(campaign)
    try:
        proc = subprocess.Popen(cmd, cwd=repo, env=env, stdout=out,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                creationflags=flags, close_fds=True)
    except OSError:
        return None
    # give it a moment to write its first heartbeat so the caller's status line
    # reflects reality rather than a race
    for _ in range(20):
        if daemon_alive(campaign)[0]:
            break
        time.sleep(0.1)
    return proc.pid


def _print_status_extras(campaign=None, outer_folder=None):
    # Print the RESOLVED campaign, not the one someone thinks is active. The
    # other entry points (display / transfer-function / twpatune) declare nothing
    # and inherit it from the state file, so this is where that becomes visible.
    print("[sync] campaign %s" % dp.active_campaign(campaign))
    ok, why = dp.check_roots()
    if not ok:
        print("[sync] *** DATA ROOTS UNUSABLE -- nothing can be mirrored: %s" % why)
    if outer_folder:
        wk = os.path.basename(os.path.normpath(outer_folder))
        if dp.is_week_dir(wk) and wk != dp.week_name():
            print("[sync] WARNING outerFolder is week %s but today is week %s "
                  "-- re-run the startup cell to roll over" % (wk, dp.week_name()))
    try:
        usage = shutil.disk_usage(dp.ROOT_C + os.sep)
        free_gb = usage.free / 1e9
        print("[sync] C: %.1f GB free of %.1f GB%s"
              % (free_gb, usage.total / 1e9,
                 "   <-- LOW" if free_gb < 100 else ""))
    except OSError:
        pass

    reclaimable = []
    for week_dir in dp.week_folders(drive="C", campaign=campaign):
        week = os.path.basename(week_dir)
        ok, _why = dv.is_prune_eligible(week, campaign=campaign)
        if ok:
            reclaimable.append(week)
    if reclaimable:
        print("[sync] verified on S: and safe to reclaim: %s"
              % ", ".join(reclaimable))
        print("[sync]   see the exact command:  python -m datasync.data_sync "
              "--prune-cmd %s" % reclaimable[0])


def stop_daemon(campaign=None, verbose=True):
    """Stop the running daemon and clear its state. Returns the pid it killed.

    Stopping the mirror is a normal operation (before a maintenance copy, or to
    take the share offline), and without this you are reduced to hunting the pid
    by hand. Only ever touches the daemon process and its own state files --
    never any data.
    """
    lock = _state("daemon.lock", campaign)
    pid = None
    try:
        with open(lock) as fid:
            pid = int(fid.read().split()[0])
    except (OSError, ValueError, IndexError):
        pass

    if pid is not None:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for name in ("daemon.lock", "heartbeat", "sync_now"):
        try:
            os.remove(_state(name, campaign))
        except OSError:
            pass
    if verbose:
        print("[sync] stopped daemon %s and cleared its state"
              % ("(pid %d)" % pid if pid else "(no lock file found)"))
    return pid


def status(campaign=None):
    """Human-readable daemon + per-week receipt state."""
    alive, age = daemon_alive(campaign)
    print("daemon: %s%s" % ("ALIVE" if alive else "not running",
                            "" if age is None else "  (heartbeat %.0fs ago)" % age))
    lock = _state("daemon.lock", campaign)
    if os.path.isfile(lock):
        with open(lock) as fid:
            print("lock:  ", " ".join(fid.read().split()))
    trig = _state("sync_now", campaign)
    print("pending trigger:", os.path.exists(trig))
    print("log:", _state("sync.log", campaign))
    print("\nper-week verification state:")
    for week_dir in dp.week_folders(drive="C", campaign=campaign):
        week = os.path.basename(week_dir)
        rec = dv.read_receipt(week, campaign=campaign)
        ok, why = dv.is_prune_eligible(week, campaign=campaign)
        print("  %-8s receipt=%-13s %s" % (
            week,
            (rec or {}).get("result", "none"),
            ("RECLAIMABLE -- " if ok else "keep on C: -- ") + why))


# ---------------------------------------------------------------------------
# Pruning: prints a command, never runs one
# ---------------------------------------------------------------------------

def prune_cmd(week, campaign=None, min_tier=None):
    """Print the delete command for a verified week. **Never deletes anything.**

    Refuses unless datasync/data_verify.is_prune_eligible agrees: a recent clean
    receipt at tier >= *min_tier*, both verification sources in agreement, C:
    unchanged since the receipt, **S: re-checked right now**, nothing still
    writing inside the week, and not the current week.

    *min_tier* defaults to dv.PRUNE_MIN_TIER ('sample'), which is deliberately
    stricter than the verifier's own default: a size-only comparison cannot see a
    same-size content change, and that is exactly how a corrupt mirror gets
    certified as clean.
    """
    week = os.path.basename(str(week))
    if min_tier is None:
        min_tier = dv.PRUNE_MIN_TIER
    ok, why = dv.is_prune_eligible(week, campaign=campaign, min_tier=min_tier)
    week_dir = os.path.join(dp.campaign_data_root("C", campaign), week)
    if not ok:
        print("REFUSING to suggest a delete for week %s\n  %s" % (week, why))
        print("\n  Verify it first:\n"
              "    python -m datasync.data_verify --verify %s --tier %s" % (week, min_tier))
        return None
    print("Week %s is verified on S: -- %s" % (week, why))
    print("\nThis is NOT run for you. To reclaim the space, run:\n")
    print('    Remove-Item -Recurse -Force "%s"\n' % week_dir)
    print("  Remove-Item   delete\n"
          "  -Recurse      including everything inside\n"
          "  -Force        do not prompt per read-only/hidden file\n")
    print("The run index is a sticky high-water mark held in %s, so deleting a\n"
          "week folder cannot make run numbers restart or collide."
          % os.path.join(dp.campaign_data_root("C", campaign), dp.LEDGER))
    return week_dir


# ---------------------------------------------------------------------------
# One-off backlog copy for an arbitrary (e.g. previous) campaign
# ---------------------------------------------------------------------------

def backlog(campaign, dry_run=True, verbose=True, progress=False):
    """Additively copy an entire campaign's ``data\\`` tree to S:.

    For a campaign predating the week layout, which is flat (no week folders).
    Expect **hours**: the cost is the per-file transfer over SMB, not the byte
    total and not the enumeration (a full 2.4 M-file / 82 k-directory tree
    enumerates in ~16 min, because SMB batches directory listings). This is why
    it is a deliberate command and never runs from kernel startup.

    *campaign* is required and used for BOTH ends and the log, so this works with
    no active campaign declared -- you are operating on a named old campaign, not
    on whatever the current session happens to be pointed at.
    """
    campaign = dp.active_campaign(campaign)   # normalises; rejects empty
    src = dp.campaign_data_root("C", campaign)
    dst = dp.campaign_data_root("S", campaign)
    if not os.path.isdir(src):
        print("no such campaign data root: %s" % src)
        print("campaigns present under %s: %s"
              % (dp.ROOT_C, ", ".join(dp.campaign_folders("C")) or "(none)"))
        return None
    print("%s\n  %s\n  ->  %s\n" % ("DRY RUN (/L, lists only)" if dry_run
                                    else "COPYING (additive, no deletes)", src, dst))
    # The log belongs to the campaign being copied, NOT to the active one. Passing
    # campaign through also means this command does not require a declared
    # campaign -- without it, _state resolved the active one and raised.
    log = _state("backlog.log", campaign, create=True)
    # For a FLAT campaign the src IS the data root, so _sync -- holding this very
    # log and the verifier's receipts -- is inside it. Copying it would make the
    # log a moving target inside its own source and, worse, leave receipt files on
    # C: that are not on S:, so the later verification could never come back
    # clean. dv.EXCLUDE_DIRS is the same list the verifier skips on both sides.
    #
    # Live claims must be honoured HERE too. This is a full-tree copy over hours;
    # without the exclusions it opens every file it meets without FILE_SHARE_WRITE
    # and kills any measurement writing into this campaign -- the same crash as a
    # daemon pass, but far more likely because the window is the whole run.
    claimed = active_paths(campaign)
    xd, xf = _exclusions_for(claimed)
    excl_d = list(dv.EXCLUDE_DIRS) + xd
    if claimed:
        print("NOT copying %d path(s) an experiment is writing (re-run later to "
              "pick them up):" % len(claimed))
        for p in claimed:
            print("   %s" % p)
        print()
    # Denominator for the bar. Expect a couple of minutes on a millions-of-files
    # campaign -- worth it, because without it "hours" has no numerator and the
    # only honest thing to show is a count-up. Skipped for a dry run, which IS
    # this counting pass.
    total = None
    if progress and not dry_run:
        t0 = time.time()
        print("counting what still needs copying (read-only robocopy /L) ...")
        total = _would_copy_count(src, dst, exclude_dirs=excl_d,
                                  exclude_files=xf)
        print("  %s file(s) to copy   (counted in %.0f s)\n"
              % ("{:,}".format(total) if total is not None
                 else "could not count -- bar will have no ETA;",
                 time.time() - t0))
    rc, failed, out = _robocopy(src, dst, log_path=log, dry_run=dry_run,
                                exclude_dirs=excl_d, exclude_files=xf,
                                progress=progress, progress_total=total,
                                progress_desc=campaign)
    summ = dv._parse_summary(out)
    print("rc=%s  files: total=%s would-copy=%s skipped=%s failed=%s extras=%s"
          % (rc, summ.get("total"), summ.get("copied"), summ.get("skipped"),
             summ.get("failed"), summ.get("extras")))
    print("log:", log)
    if failed:
        print("*** robocopy reported a FAILURE (rc >= 8) ***")
    if dry_run:
        print("\nNothing was copied. Re-run without --dry-run to do it for real.")
    return {"rc": rc, "failed": failed, "summary": summ, "log": log}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv=None):
    p = argparse.ArgumentParser(
        description="Background C: -> S: mirror. Additive only; never deletes.")
    p.add_argument("--campaign", default=None)
    p.add_argument("--daemon", action="store_true",
                   help="run the mirror loop in the foreground (used internally "
                        "by ensure_daemon, which launches it detached)")
    p.add_argument("--once", action="store_true", help="one blocking pass, then exit")
    p.add_argument("--dry-run", action="store_true",
                   help="with --once/--backlog: robocopy /L, copy nothing")
    p.add_argument("--status", action="store_true")
    p.add_argument("--ensure", action="store_true",
                   help="start the daemon if it is not already running")
    p.add_argument("--stop", action="store_true",
                   help="stop the running daemon and clear its state")
    p.add_argument("--prune-cmd", metavar="WEEK",
                   help="print (never run) the delete command for a verified week")
    p.add_argument("--backlog", metavar="CAMPAIGN",
                   help="one-off additive copy of another campaign's data tree")
    p.add_argument("--progress", action="store_true",
                   help="with --backlog/--once: live per-file progress bar. Adds "
                        "a read-only /L counting pass first (minutes on a big "
                        "campaign) so the bar has a percentage and an ETA")
    args = p.parse_args(argv)

    if args.daemon:
        _daemon_loop(args.campaign)
        return 0
    if args.once:
        rows = sync_once(campaign=args.campaign, dry_run=args.dry_run,
                         progress=args.progress)
        return 1 if any(r["failed"] for r in rows) else 0
    if args.status:
        status(args.campaign)
        return 0
    if args.ensure:
        ensure_daemon(args.campaign)
        return 0
    if args.stop:
        stop_daemon(args.campaign)
        return 0
    if args.prune_cmd:
        return 0 if prune_cmd(args.prune_cmd, args.campaign) else 1
    if args.backlog:
        res = backlog(args.backlog, dry_run=args.dry_run,
                      progress=args.progress)
        return 1 if (res is None or res["failed"]) else 0

    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
