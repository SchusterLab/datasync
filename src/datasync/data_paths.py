"""Campaign / week-folder layout and the campaign-global run index.

Single source of truth for WHERE data goes. Replaces the hand-edited
``outerFolder = "C:\\_Data\\...\\data"`` literal that used to be duplicated
across every notebook and entry point.

Layout::

    <ROOT_C>\\<campaign>\\                        <- campaign, DECLARED BY THE NOTEBOOK
        data\\                                    <- campaign data root (holds the ledger)
            <YYMMDD of a Monday>\\                <- week folder == outerFolder
                <PREFIX>_<index>_<name>.h5        <- runs, flat at the top level
                <PREFIX>_<index>_<name>\\         <- per-run subtree
                png\\  cfg\\  npz\\  viz\\  summary_data\\
            <YYMMDD of the next Monday>\\
            _run_index.json                       <- sticky run-index high-water
                                                     mark, per <PREFIX>
            _sync\\                               <- daemon + verifier state
        Notebooks\\
    <ROOT_C>\\_active_campaign.json               <- which campaign is live right now

**This module is deliberately agnostic about what is being measured.** No
campaign name, device name, run number or seed is hardcoded here. Which campaign
is live is a property of the experiment, so it is declared once in the notebook
via :func:`declare_campaign` and recorded in ``_active_campaign.json`` beside the
data -- which is also how a detached daemon and a bare CLI invocation resolve the
same campaign the notebook did. With nothing declared, every path function
**raises**; it never falls back to a default, because a silent default writes an
experiment into the wrong tree.

Week folders are named ``YYMMDD`` of the **Monday** of the ISO week, matching the
campaign folder's own date convention. The week folder IS ``outerFolder``, laid
out exactly like the old flat ``data\\``, so every existing save path -- the ~55
hand-built ``self.datapath + '/png/' + ...`` sites, the ``self.localFolder``
sites, every .npz/.gif/.html writer -- follows with no edits.

Keep it import-light (stdlib only) so it works in ``tprocv2_365_analyze`` and in
the display kernel. Do NOT import the host project's heavy experiment modules here --
they pull slab/qick/Pyro4.
"""

import datetime
import glob
import json
import os
import re

# ---------------------------------------------------------------------------
# Campaign configuration -- the one place to edit when a new campaign starts
# ---------------------------------------------------------------------------

#: Environment overrides exist because the sync daemon runs as a SEPARATE
#: process: it re-imports this module, so an in-process reassignment of the
#: roots would not reach it and it would happily mirror the wrong tree.
#: datasync/data_sync.py propagates the live values to the child through these.
#: Unset in normal use -- the defaults below are the truth.
_ENV_C, _ENV_S, _ENV_CAMPAIGN = ("TPROCV2_DATA_ROOT_C", "TPROCV2_DATA_ROOT_S",
                                 "TPROCV2_CAMPAIGN")

# local drive: where experiments write
ROOT_C = os.environ.get(_ENV_C) or r"C:\_Data"
# network share (\\10.108.30.4\slab): the mirror
ROOT_S = os.environ.get(_ENV_S) or r"S:\_Data"

DATA_SUBDIR = "data"        # kept between the campaign folder and the weeks so
                            # Notebooks\ and one-off analysis dirs stay outside
                            # the mirrored raw-data tree

# WHICH campaign is being measured is NOT this module's business -- it is a
# property of the experiment you are running, so it is declared in the notebook
# (see declare_campaign) and recorded in this file under ROOT_C. Nothing about a
# specific device, campaign or run number is hardcoded here.
ACTIVE_FILE = "_active_campaign.json"

# Default run-number prefix, used only when deriving a seed for a brand-new
# campaign. It is the filename prefix the base class already writes ("H11_00000")
# and is passed explicitly by every real caller. The base class takes the prefix
# it WRITES from cfg["campaign_name"], so that key is what names the files; this
# default only decides whose numbering declare_campaign reasons about.
DEFAULT_PREFIX = "H11"

# Created eagerly with every week folder. npz/ in particular is makedirs'd
# NOWHERE else in the repo -- mDUST.py:1675/1731, mResSpec.py:1333 and
# mQubitPulse.py:3282 all assume it already exists, so a fresh campaign folder
# would raise FileNotFoundError on the first DUST / ResFluxSweep run.
SUBFOLDERS = ("png", "cfg", "npz", "viz", "summary_data")

LEDGER = "_run_index.json"  # in the campaign data root, NOT in a week folder
SYNC_DIR = "_sync"          # daemon lock/heartbeat/log + verification receipts

_WEEK_FMT = "%y%m%d"


# ---------------------------------------------------------------------------
# The active campaign -- declared by the notebook, recorded for other processes
# ---------------------------------------------------------------------------

def _drive_root(drive="C"):
    return {"C": ROOT_C, "S": ROOT_S}[str(drive).upper().rstrip(":")]


def active_file():
    """``<ROOT_C>\\_active_campaign.json`` -- outside every campaign, so it can
    name one. Lives beside the data rather than in the repo: two checkouts or a
    worktree must not be able to disagree about where data is going."""
    return os.path.join(ROOT_C, ACTIVE_FILE)


def _read_active():
    try:
        with open(active_file()) as fid:
            st = json.load(fid)
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict) or not isinstance(st.get("active"), str) \
            or not st["active"].strip():
        return None
    st["high_water"] = _norm_high_water(st.get("high_water"), st.get("prefix"))
    return st


def _norm_high_water(hw, prefix):
    """``{prefix: {campaign: highest index}}``, migrating the legacy flat form.

    The mark has to be per PREFIX as well as per campaign, because a run is named
    ``<prefix>_<index>``: ``H11_00000`` cannot collide with ``H10_02822``, so H10's
    mark must not set a floor under the first run of H11. The legacy file stored a
    flat ``{campaign: index}`` belonging to whichever prefix was live when it was
    written -- which is recorded in the same file, so migrate it under THAT prefix.
    Never under the prefix asking now: that is exactly the conflation being removed.
    A flat entry with no recorded prefix is dropped rather than guessed; the on-disk
    scan in :func:`_scan_per_campaign` rebuilds it for anything still on C:.
    """
    if not isinstance(hw, dict):
        return {}
    nested = {p: {c: int(i) for c, i in m.items()}
              for p, m in hw.items() if isinstance(m, dict)}
    flat = {c: int(i) for c, i in hw.items() if not isinstance(i, dict)}
    if flat and prefix:
        marks = nested.setdefault(prefix, {})
        for camp, hi in flat.items():
            marks[camp] = max(int(marks.get(camp, -1)), hi)
    return nested


def _write_active(st):
    """Atomic (temp + os.replace) so a crash cannot leave a half-written file
    that would read back as "no campaign declared"."""
    path = active_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    st["declared"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = path + ".tmp"
    with open(tmp, "w") as fid:
        json.dump(st, fid, indent=1)
    os.replace(tmp, path)
    return path


def active_campaign(campaign=None):
    """Which campaign are we writing to? **Never guesses -- raises instead.**

    Precedence, most explicit first:

    1. an explicit *campaign* argument;
    2. ``TPROCV2_CAMPAIGN`` -- how a detached child inherits the parent's choice
       (``data_sync._launch_detached`` sets it, because the daemon re-imports
       this module in its own process and would otherwise resolve independently);
    3. the ``_active_campaign.json`` written by :func:`declare_campaign`, so a
       bare ``python -m datasync.data_sync --status`` in a fresh shell resolves
       the same campaign the notebook did.

    There is deliberately no default. A hardcoded fallback is what lets a
    half-configured session write an experiment into the wrong campaign tree and
    number it against the wrong ledger; that has to fail loudly, not quietly.
    """
    if campaign:
        return os.path.basename(str(campaign).strip().rstrip("\\/"))
    env = (os.environ.get(_ENV_CAMPAIGN) or "").strip()
    if env:
        return os.path.basename(env.rstrip("\\/"))
    st = _read_active()
    if st:
        return st["active"]
    raise RuntimeError(
        "No active campaign has been declared, so there is no safe place to "
        "read or write data.\nDeclare it once, in the notebook, before anything "
        "else:\n\n    import datasync.data_paths as dp\n"
        "    dp.declare_campaign(CAMPAIGN)      # the campaign folder name\n\n"
        "That records it in %s, which is how the mirror daemon and the "
        "command-line tools resolve the same campaign.\nExisting campaign "
        "folders under %s: %s"
        % (active_file(), ROOT_C, ", ".join(campaign_folders("C")) or "(none)"))


def campaign_folders(drive="C"):
    """Every campaign folder under the data root, i.e. anything with a ``data\\``.

    Used to derive a new campaign's run-index seed from what already exists,
    rather than from a hand-maintained literal.
    """
    root = _drive_root(drive)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [n for n in names
            if os.path.isdir(os.path.join(root, n, DATA_SUBDIR))]


def folder_max_index(folder, prefix, suffix=".h5"):
    """Highest ``<prefix>_<int>_`` index claimed directly inside *folder*, else -1.

    Three kinds of evidence, all at *folder*'s top level: the primary data files
    (``*suffix``), the figures in the ``png`` subfolder -- so an experiment that saves only a
    figure still advances the index -- and the **per-run subfolders**.

    The subfolders are what make an interrupted run visible. A run creates its own
    ``<prefix>_<index>_<name>`` subtree before it writes anything at the top
    level, so a run that died before its first save used to leave nothing this scan
    could see, and its number was handed out a second time -- two different runs
    answering to one name. Per-point folders live INSIDE a per-run folder, never
    here, so they are never counted.
    """
    pat = re.compile(r"^%s_(\d+)_" % re.escape(prefix))
    paths = glob.glob(os.path.join(folder, "*" + suffix))
    paths += glob.glob(os.path.join(folder, "png", "*.png"))
    # Trailing separator == match directories ONLY, so this costs one directory
    # read rather than an isdir() stat per entry.
    paths += glob.glob(os.path.join(folder, prefix + "_*", ""))
    best = -1
    for p in paths:
        m = pat.match(os.path.basename(os.path.normpath(p)))
        if m:
            best = max(best, int(m.group(1)))
    return best


def _scan_per_campaign(prefix=DEFAULT_PREFIX, drive="C", suffix=".h5"):
    """``{campaign: highest run index}`` over every campaign on *drive*.

    **Shallow on purpose.** One glob of the campaign data root (the old flat
    layout) plus one per week folder (the current layout) plus their ``png\\``
    -- the same two globs the base class has always numbered from. It never
    recurses into the per-run subtrees, so it costs a handful of directory reads
    even for a campaign holding millions of files.
    """
    out = {}
    for camp in campaign_folders(drive):
        data_root = os.path.join(_drive_root(drive), camp, DATA_SUBDIR)
        best = max([folder_max_index(d, prefix, suffix)
                    for d in [data_root] + week_folders(root=data_root)],
                   default=-1)
        if best >= 0:
            out[camp] = best
    return out


def scan_high_water(prefix=DEFAULT_PREFIX, drive="C", suffix=".h5"):
    """Highest run index in use by ANY campaign on *drive*, or -1 if none."""
    return max(_scan_per_campaign(prefix, drive, suffix).values(), default=-1)


def _ledger_seed(led, prefix):
    """*prefix*'s first run number as recorded in ledger *led*, or None.

    Per prefix, because one campaign can host runs under more than one prefix --
    the one that was live when it opened, and the one it was renumbered to.

    A legacy ledger carries ONE campaign-wide ``seed`` with no record of the prefix
    it belonged to; its ``floors`` name the prefixes it was actually numbering, so
    the legacy seed is honoured only for those. Handing it to a new prefix would
    push that prefix's first run up into the old prefix's numbers, which is the
    whole thing this split exists to prevent.
    """
    seeds = led.get("seeds")
    if isinstance(seeds, dict) and seeds.get(prefix) is not None:
        return int(seeds[prefix])
    if led.get("seed") is not None and prefix in (led.get("floors") or {}):
        return int(led["seed"])
    return None


def _peek_ledger_seed(campaign, prefix, drive="C"):
    """*prefix*'s recorded seed in *campaign*, or None if there is none yet."""
    try:
        with open(_ledger_path(campaign_data_root(drive, campaign))) as fid:
            return _ledger_seed(json.load(fid), prefix)
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def index_seed(campaign=None, prefix=DEFAULT_PREFIX):
    """First run number for *prefix* in *campaign*. Never a hardcoded literal.

    The ledger's seed for that prefix if it has one, else the seed recorded at
    declaration time (only if it was declared for this same prefix), else derived
    from the runs already on disk under that prefix.
    """
    campaign = active_campaign(campaign)
    led = _peek_ledger_seed(campaign, prefix)
    if led is not None:
        return led
    st = _read_active() or {}
    if st.get("active") == campaign and st.get("prefix") == prefix \
            and st.get("index_seed") is not None:
        return int(st["index_seed"])
    return scan_high_water(prefix) + 1


def declare_campaign(name, index_seed=None, prefix=DEFAULT_PREFIX, verbose=True):
    """Declare which campaign this session writes to. **Call from the notebook.**

    *index_seed* -- the first run number of a **new** campaign. Leave it None and
    it is derived: one past the highest run index found in any campaign on C:,
    or the sticky high-water mark recorded in the state file, whichever is
    greater. Deriving it is the whole point. A hand-maintained seed that is not
    raised when the campaign changes makes the new campaign's first run reuse the
    old one's number -- two different datasets answering to one name, which is a
    silent overwrite waiting to happen on the mirror.

    *prefix* -- the run-number namespace, i.e. the ``H11`` in ``H11_00000``. Run
    numbers, seeds and high-water marks are all tracked **per prefix**, because a
    run's identity is the pair: ``H11_00000`` cannot collide with ``H10_02822``, so
    a new prefix legitimately restarts at 0 even in a campaign folder that already
    holds runs under the old one. Note this argument only tells THIS module which
    numbering to reason about -- the prefix actually written into filenames is
    ``cfg["campaign_name"]``, read by the experiment base class, so the two have
    to be set to the same string.

    A campaign that already has a ledger seed *for this prefix* keeps it;
    *index_seed* can only ever affect a prefix that has never been numbered here.
    Re-declaring the current campaign every session is therefore free and
    idempotent.

    The sticky high-water mark matters because the scan only sees what is still
    on C:. Archiving an old campaign off the local disk must not be able to lower
    the next run number back into numbers that campaign already used on S:.
    """
    name = os.path.basename(str(name).strip().rstrip("\\/"))
    if not name:
        raise ValueError("campaign name must not be empty")

    st = _read_active() or {"active": None, "high_water": {}}
    prev = st.get("active")

    marks = st["high_water"].setdefault(prefix, {})
    for camp, hi in _scan_per_campaign(prefix).items():   # sticky per prefix+campaign
        marks[camp] = max(int(marks.get(camp, -1)), hi)
    floor = max(list(marks.values()) + [-1])

    existing = _peek_ledger_seed(name, prefix)
    if existing is not None:
        seed = existing
        if index_seed is not None and int(index_seed) != existing:
            print("[campaign] NOTE %s already has a ledger seeded at %d; "
                  "index_seed=%s ignored (a seed only applies to a new campaign)"
                  % (name, existing, index_seed))
    elif index_seed is None:
        seed = floor + 1
    else:
        seed = int(index_seed)
        if seed <= floor:
            raise ValueError(
                "index_seed=%d would collide with run %s_%05d, which already "
                "exists.\nThe first run of a new campaign must be above every run "
                "number ever used UNDER THIS PREFIX, or two different datasets end "
                "up sharing a name.\nPass index_seed=%d, leave it None to derive "
                "it, or start a new prefix -- numbering is per prefix, so a new "
                "one restarts at 0."
                % (seed, prefix, floor, floor + 1))

    st["active"] = name
    st["index_seed"] = seed
    st["prefix"] = prefix
    marks[name] = max(int(marks.get(name, -1)), seed)
    # Set the env var too, so anything this process spawns inherits the choice
    # even before the state file is consulted.
    os.environ[_ENV_CAMPAIGN] = name
    path = _write_active(st)
    _pin_ledger_seed(campaign_data_root("C", name), prefix, seed)

    if verbose:
        print("[campaign] %s%s" % (name, "" if prev in (None, name)
                                   else "   (was %s)" % prev))
        print("[campaign]   first run of this campaign: %s_%05d%s"
              % (prefix, seed,
                 "   (already established)" if existing is not None
                 else "   (derived: highest existing run is %05d)" % floor
                 if floor >= 0 else "   (derived: no existing runs found)"))
        print("[campaign]   declared in %s" % path)
        if prev and prev != name:
            print("[campaign]   RESTART THE MIRROR DAEMON -- the running one has "
                  "%s baked into its environment and will keep mirroring it:\n"
                  "[campaign]     python -m datasync.data_sync --stop" % prev)
    return name


# ---------------------------------------------------------------------------
# Week naming
# ---------------------------------------------------------------------------

def week_name(d=None):
    """``YYMMDD`` of the Monday of *d*'s week (default today).

    >>> week_name(datetime.date(2026, 7, 29))   # a Wednesday
    '260727'
    """
    if d is None:
        d = datetime.date.today()
    if isinstance(d, datetime.datetime):
        d = d.date()
    monday = d - datetime.timedelta(days=d.weekday())
    return monday.strftime(_WEEK_FMT)


def is_week_dir(name):
    """True only for a ``YYMMDD`` name that is a real Monday.

    Structural, not a regex: requiring ``weekday() == 0`` means a stray 6-digit
    folder (or a Tuesday-dated one) can never be mistaken for a week folder and
    pulled into the index scan or the sync scope. ``Notebooks``, ``_sync`` and
    any one-off analysis folder are excluded by this test rather than by a name
    blacklist that would need maintaining.
    """
    try:
        d = datetime.datetime.strptime(os.path.basename(name), _WEEK_FMT).date()
    except (ValueError, TypeError):
        return False
    return d.weekday() == 0


def week_date(name):
    """``YYMMDD`` -> ``datetime.date``, or None if *name* is not a week folder."""
    if not is_week_dir(name):
        return None
    return datetime.datetime.strptime(os.path.basename(name), _WEEK_FMT).date()


# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------

def campaign_root(drive="C", campaign=None):
    """``<ROOT_C|ROOT_S>\\<campaign>``. Raises if no campaign has been declared."""
    return os.path.join(_drive_root(drive), active_campaign(campaign))


def campaign_data_root(drive="C", campaign=None):
    """``<campaign>\\data`` -- the parent of the week folders, holds the ledger."""
    return os.path.join(campaign_root(drive, campaign), DATA_SUBDIR)


def check_roots():
    """``(ok, problem_or_None)`` -- are ROOT_C and ROOT_S usable as endpoints?

    Requires each to carry a real root: a drive letter (``S:\\...``) or a UNC
    share (``\\\\server\\share\\...``). ``os.path.splitdrive`` returns an empty
    drive for anything else.

    The failure this exists for is a **UNC path written with one leading
    separator**. ``\\\\server\\share`` mistyped as ``\\server\\share`` is not an
    error to Windows -- it is a ROOT-RELATIVE path, so it silently resolves onto
    the current drive. The mirror then copies to ``C:\\server\\share\\...``,
    robocopy reports success (rc=1), and the verifier compares C: against another
    directory ON C: and reports **clean** -- which would authorise deleting the
    only real copy. Measured: a proper UNC survives ``abspath`` unchanged, the
    one-separator form becomes ``C:\\...``.

    Deliberately NOT a same-volume test: mirroring to another folder on one disk
    is a legitimate (if unwise) choice, and the test suites rely on it.
    """
    for name, path in (("ROOT_C", ROOT_C), ("ROOT_S", ROOT_S)):
        if not str(path).strip():
            return False, "%s is empty" % name
        if not os.path.splitdrive(path)[0]:
            return False, (
                "%s = %r has no drive or UNC root, so Windows resolves it onto "
                "the CURRENT drive (%r). A UNC path needs TWO leading separators."
                % (name, path, os.path.abspath(path)))

    # The destination must not BE the source. Reproduced: with ROOT_S = ROOT_C the
    # old check returned ok=True, so the verifier compared the tree with itself and
    # certified 'full' clean -- the strongest possible false clean, on the path
    # that authorises deleting the only copy.
    #
    # os.path.samefile, not a string compare: it resolves subst drives, junctions
    # and \\localhost\C$ aliases, which all name the same place with different
    # text. Falls back to a normalised string compare when the paths do not exist
    # yet (samefile raises), so a fresh campaign is still checked.
    c, s = os.path.abspath(ROOT_C), os.path.abspath(ROOT_S)
    same = None
    try:
        same = os.path.samefile(c, s)
    except OSError:
        same = os.path.normcase(os.path.normpath(c)) == os.path.normcase(
            os.path.normpath(s))
    if same:
        return False, ("ROOT_S and ROOT_C are the SAME location (%r vs %r) -- the "
                       "backup would copy onto its own source and the verifier "
                       "would compare the tree with itself and call it clean"
                       % (ROOT_C, ROOT_S))

    # Nor may one contain the other: mirror_path would then map a path to a
    # destination inside the tree being copied, so the copy feeds itself.
    for a, b, an, bn in ((c, s, "ROOT_C", "ROOT_S"), (s, c, "ROOT_S", "ROOT_C")):
        if os.path.normcase(b).startswith(os.path.normcase(a) + os.sep):
            return False, ("%s (%r) is INSIDE %s (%r) -- the backup would copy "
                           "into its own tree" % (bn, b, an, a))
    return True, None


def mirror_path(local):
    """Local (C:) path -> the corresponding path on the S: mirror.

    Only rewrites the drive root, so it works for a week folder, a run folder or
    a single file alike. Raises if the roots are unusable -- see check_roots; a
    mirror that silently lands back on C: is worse than no mirror, because the
    verifier would then certify it.
    """
    ok, why = check_roots()
    if not ok:
        raise ValueError("unusable data roots: %s" % why)
    local = os.path.abspath(local)
    if not local.upper().startswith(ROOT_C.upper()):
        raise ValueError(
            "not under the local data root %s: %r" % (ROOT_C, local))
    return ROOT_S + local[len(ROOT_C):]


def sync_dir(drive="C", campaign=None, create=False):
    """``<campaign data root>\\_sync`` -- daemon state + verification receipts."""
    p = os.path.join(campaign_data_root(drive, campaign), SYNC_DIR)
    if create:
        os.makedirs(p, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Week folders
# ---------------------------------------------------------------------------

def ensure_subfolders(week_folder):
    """makedirs the week folder and every entry of SUBFOLDERS."""
    os.makedirs(week_folder, exist_ok=True)
    for sub in SUBFOLDERS:
        os.makedirs(os.path.join(week_folder, sub), exist_ok=True)
    return week_folder


def current_outer_folder(d=None, create=True, drive="C", campaign=None):
    """The week folder for *d* (default today). This is ``outerFolder``.

    With *create*, the week folder, its SUBFOLDERS and the ledger are all
    materialised, so a brand-new campaign or a Monday rollover needs no manual
    setup.
    """
    root = campaign_data_root(drive, campaign)
    week = os.path.join(root, week_name(d))
    if create:
        ensure_subfolders(week)
        _read_ledger(root)          # creates the seeded ledger if absent
    return week


def week_folders(root=None, drive="C", campaign=None):
    """Sorted week folders under the campaign data root (oldest first).

    ``YYMMDD`` sorts lexicographically == chronologically within the century.
    """
    root = root or campaign_data_root(drive, campaign)
    if not os.path.isdir(root):
        return []
    return [os.path.join(root, n) for n in sorted(os.listdir(root))
            if is_week_dir(n) and os.path.isdir(os.path.join(root, n))]


def find_run(dfile_or_idx, root=None, drive="C", campaign=None):
    """Locate a run's ``.h5`` across the week folders. Newest week first.

    Accepts a full basename (``<PREFIX>_<index>_<name>.h5``), a stem without the
    extension, or a bare index (``2570`` / ``'02570'``).

    Needed because ``ExperimentClass2.dfile_to_data_cfg`` takes a bare *dfile*
    plus an explicit *outerFolder* -- a contract that breaks as soon as runs span
    weeks. Readers handed a full path are already week-safe (they derive their
    sidecars from ``dirname(h5_path)``).
    """
    weeks = week_folders(root, drive, campaign)[::-1]     # newest first

    if isinstance(dfile_or_idx, int):
        pattern = "*_%05d_*.h5" % dfile_or_idx
    else:
        s = str(dfile_or_idx)
        if re.fullmatch(r"\d+", s):
            pattern = "*_%05d_*.h5" % int(s)
        else:
            # a basename: match it exactly, tolerating a missing .h5
            pattern = s if s.endswith(".h5") else s + ".h5"

    for week in weeks:
        hits = sorted(glob.glob(os.path.join(week, pattern)))
        if hits:
            return hits[0]
    return None


def outer_folder_for(dfile_or_idx, root=None, drive="C", campaign=None):
    """The week folder holding a given run -- the ``outerFolder`` to read it with.

    For the display / analysis side: ``dfile_to_data_cfg(outerFolder=..., dfile=...)``
    needs the folder the run actually lives in, which is no longer a single fixed
    directory once runs span weeks.

    >>> outerFolder = outer_folder_for('H10_02568_ResSpec.h5')   # or just 2568
    """
    hit = find_run(dfile_or_idx, root, drive, campaign)
    return os.path.dirname(hit) if hit else None


def _find_run_on_mirror(dfile, root=None, drive="C", campaign=None):
    """Second pass for :func:`resolve_run_path`, on the S: mirror. Never raises.

    Only reachable after the local search has already missed, so the cost (a
    network ``isdir`` per week folder, or a timeout when S: is not mounted) is
    paid only on a genuine miss.

    Returns None rather than raising for every "there is no mirror to look at"
    case: *root* already being on S: (``mirror_path`` refuses anything not under
    ROOT_C), an unusable root pair, an unmounted share.
    """
    try:
        if root is None:
            if str(drive).upper().startswith("S"):
                return None             # the primary pass already searched S:
            return find_run(dfile, drive="S", campaign=campaign)
        return find_run(dfile, root=mirror_path(root))
    except Exception:
        return None


def resolve_run_path(dfile, outerFolder=None, drive="C", campaign=None,
                     mirror_fallback=True):
    """Full path to a run's ``.h5``, redirected to the week folder that holds it.

    The loader contract is ``(outerFolder, dfile)`` and a notebook's *outerFolder*
    is THIS week's folder, so re-plotting a run from an earlier week used to raise
    FileNotFoundError on a path that never existed. This resolves the pair:

    * ``join(outerFolder, dfile)`` exists  -> returned untouched (one ``isfile``,
      so the live-run path is unaffected).
    * it does not exist                    -> search the sibling week folders,
      newest first, and return the hit.
    * still nothing, *mirror_fallback*     -> repeat the search on the S: mirror,
      so a week folder that has been cleaned off C: still loads. Costs network
      I/O, but only after a local miss. The returned path is then ON S:, which
      also means a re-plot's ``png/`` output lands on the mirror -- the loader
      says so when it happens.

    The search root is *outerFolder*'s own parent, so a run can never be resolved
    into a different campaign than the caller asked for. Searching is gated on
    *outerFolder* actually BEING a week folder (``is_week_dir``, i.e. a real
    Monday) rather than on a name test: a caller pointing at a per-run subtree, a
    one-off analysis dir or an old flat ``data\\`` campaign gets its path back
    unchanged instead of a guess from somewhere else.

    Returns the unchanged candidate -- not None -- when the run is nowhere, so the
    caller's own ``open()`` still raises naming the path that was asked for. Only
    a call with no *outerFolder* at all can return None.

    >>> # loads even though 00412 is last week's run and outerFolder is this week's
    >>> h5 = resolve_run_path('H11_00412_DUST_insituAC_AggronQA.h5', current_outer_folder())
    """
    candidate = os.path.join(outerFolder, str(dfile)) if outerFolder else None
    if candidate and os.path.isfile(candidate):
        return candidate

    root = None
    if outerFolder:
        outer = os.path.normpath(outerFolder)
        if not is_week_dir(os.path.basename(outer)):
            return candidate            # not a week folder: caller's path stands
        root = os.path.dirname(outer)
    try:
        hit = find_run(dfile, root=root, drive=drive, campaign=campaign)
    except Exception:
        hit = None      # no campaign declared / unreadable root: not our problem
    if hit is None and mirror_fallback:
        hit = _find_run_on_mirror(dfile, root, drive, campaign)
    return hit or candidate


# ---------------------------------------------------------------------------
# Campaign-global run index
# ---------------------------------------------------------------------------
#
# The base class used to take max(index)+1 over ONE folder, so a fresh week
# folder restarted at 0 and collided with the previous week. This replaces that
# with a sticky high-water mark, which buys three properties:
#
#   no index burn -- the per-point throwaway objects (make_folders=False, the
#       DUST chiac inner loop that builds one SingleShot per point) are built with
#       outerFolder=<the per-run folder>, which is not a week folder, so they never
#       reach the ledger at all: nothing is scanned, reserved or consumed for them.
#       Only a week-folder allocation reserves a number, i.e. once per real run.
#   collision-proof -- a number is consumed by being HANDED OUT (the floor is left
#       one past it) and the scan also counts per-run FOLDERS, so a run that dies
#       before its first save can neither hide from the scan nor see its number
#       reissued to a different run.
#   prune-safe    -- deleting a fully-verified past week from C: cannot lower
#       the next number, because the floor never decreases.
#   self-healing  -- a lost or corrupt ledger is rebuilt from the on-disk scan.
#
# All of it is tracked per PREFIX (the ledger's "seeds" and "floors" are both
# keyed by it, and so is the state file's high_water), because a run's identity is
# the <prefix>_<index> PAIR. Starting a new prefix therefore opens a fresh
# numbering line at 0 instead of continuing the old one, even inside a campaign
# folder that already holds runs under the previous prefix -- H11_00000 cannot
# overwrite H10_02822.
#
# Concurrency: two kernels constructing an experiment at the same instant see
# the same disk state and get the same index. That is pre-existing behaviour of
# the glob approach, not a regression; the ledger write itself is atomic.

def _ledger_path(data_root):
    return os.path.join(data_root, LEDGER)


def _read_ledger(data_root, prefix=None):
    """Load (creating if absent/corrupt) the ledger for *data_root*.

    *prefix* is the run prefix being numbered, and is used only to seed a ledger
    that does not exist yet. Omit it (the ``current_outer_folder`` case, which just
    materialises the file) and the ledger is created with no seed recorded; the
    seed is then filled in by :func:`next_campaign_index`, which knows the prefix.
    """
    path = _ledger_path(data_root)
    try:
        with open(path) as fid:
            led = json.load(fid)
        if isinstance(led.get("floors"), dict):
            if not isinstance(led.get("seeds"), dict):
                led["seeds"] = {}       # a legacy campaign-wide "seed" is kept as
            return led                  # is; _ledger_seed decides who may use it
    except (OSError, ValueError):
        pass
    # absent, unreadable or malformed -> reseed; the on-disk scan in
    # next_campaign_index will lift the floor back up to the real high-water
    # mark, so nothing is lost by rebuilding.
    camp = os.path.basename(os.path.dirname(data_root))
    led = {"campaign": camp, "seeds": {}, "floors": {}}
    if prefix:
        led["seeds"][prefix] = index_seed(camp, prefix)
    _write_ledger(data_root, led)
    return led


def _write_ledger(data_root, led):
    """Atomic write (temp + os.replace) so a crash can't truncate the ledger."""
    path = _ledger_path(data_root)
    os.makedirs(data_root, exist_ok=True)
    led["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = path + ".tmp"
    with open(tmp, "w") as fid:
        json.dump(led, fid, indent=1)
    os.replace(tmp, path)


def _pin_ledger_seed(data_root, prefix, seed):
    """Record *prefix*'s first run number in the campaign's ledger, once.

    Makes re-declaring the same campaign idempotent. Without it the seed lives only
    in ``_active_campaign.json``, where it is stored as the *next free* number and
    is re-derived on every declaration -- so declaring twice before the first run
    of a new prefix pushed that run to 00001. Never overwrites a seed that is
    already there: the first run of a prefix is decided once.
    """
    led = _read_ledger(data_root)
    if led.setdefault("seeds", {}).get(prefix) is None:
        led["seeds"][prefix] = int(seed)
        _write_ledger(data_root, led)


def scan_max_index(data_root, prefix, suffix=".h5"):
    """Highest ``<prefix>_<int>_`` index across every week folder, else -1.

    Per folder the evidence is data files, ``png/*.png`` and per-run subfolders --
    see :func:`folder_max_index`.
    """
    return max([folder_max_index(week, prefix, suffix)
                for week in week_folders(data_root)], default=-1)


def next_campaign_index(datapath, prefix, suffix=".h5"):
    """Next campaign-global run index, or **None** if *datapath* is not a week folder.

    Returning None is what keeps the change surgical: the caller falls back to
    the original single-folder ``max+1``.

    The discriminator is purely ``is_week_dir(basename(datapath))`` -- a real
    Monday ``YYMMDD``. That is structural (no hardcoded path) and complete: a
    per-run folder is named ``<PREFIX>_<index>_<name>``, a per-point folder
    ``4500.0MHz``, a one-off analysis folder something descriptive, and the
    campaign data root ``data`` -- none of which is a Monday date.

    Deliberately NOT gated on the ledger file existing. Gating on it meant a
    deleted ledger silently fell through to per-week local numbering, i.e. run
    numbers restarted at 0 and collided with the previous week -- a data-losing
    failure triggered by misplacing one small JSON file. The ledger is now
    created on demand and its floor is rebuilt from the on-disk scan, so both a
    missing and a corrupt ledger self-heal.

    Nested experiments are constructed with ``outerFolder=<the per-run folder>``
    (mDUST.py:164, 253, 1089, 1556, 1633), so they keep their documented
    per-run 0-based numbering and never pay for the multi-week scan.
    """
    if not is_week_dir(os.path.basename(os.path.normpath(datapath))):
        return None
    data_root = os.path.dirname(os.path.normpath(datapath))

    led = _read_ledger(data_root, prefix)
    camp = os.path.basename(os.path.dirname(data_root))
    dirty = False

    # Explicit None tests, never `or`: a seed of 0 -- the first run of a fresh
    # prefix -- is falsy, and `led.get("seed") or index_seed(...)` would silently
    # discard it and renumber the run.
    floor = led["floors"].get(prefix)
    if floor is None:
        floor = _ledger_seed(led, prefix)
    if floor is None:                       # first run of this prefix here
        floor = index_seed(camp, prefix)
        led.setdefault("seeds", {})[prefix] = int(floor)
        dirty = True
    floor = int(floor)

    scanned = scan_max_index(data_root, prefix, suffix)
    idx = max(floor, scanned + 1)

    # RESERVE it. Handing a number out is what consumes it -- not writing a file
    # named with it -- so the floor is left one PAST the number just issued. Without
    # this the floor sat ON the issued number, and a run interrupted before its
    # first top-level save had its number handed to the next experiment (H11_00132
    # was issued twice). The price is a gap in the numbering when a construction
    # never gets as far as saving, which is strictly better than two runs sharing a
    # name. The scan above still raises the floor on its own, so a lost ledger
    # self-heals to the on-disk truth.
    if led["floors"].get(prefix) != idx + 1:
        led["floors"][prefix] = idx + 1
        dirty = True
    if dirty:
        _write_ledger(data_root, led)
    return idx
