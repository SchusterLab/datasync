r"""Always-on-top desktop widget for the C: -> S: data backup.

    python -m datasync.data_sync_ui

A small frameless card that floats above other windows, drag it anywhere. Shows
whether the daemon is alive, whether a copy is in flight, what the last pass did,
and which run folders the mirror is currently avoiding.

Why a pure READER
-----------------
It touches nothing the daemon owns. Every value comes from state the daemon
already publishes -- the heartbeat mtime, daemon.lock, sync_now, the active/
markers, sync.log -- plus a process-list check for a live robocopy. There is no
IPC, no shared memory and no daemon-side change, so the widget cannot desync from
the daemon, cannot crash it, and can be started or killed at any time.

Two properties were verified before writing this, because getting either wrong
would make the widget actively harmful:

* **A Python reader does not lock a file against a writer.** Measured: holding a
  file open for reading still allows another process to append. This matters
  enormously here -- robocopy opens its SOURCES without FILE_SHARE_WRITE, and that
  is what crashed a real DUST run when the mirror hit a live timing.csv. A widget
  polling sync.log every couple of seconds must not be able to do the same thing
  to the daemon. It cannot.
* **The widget can still read a file robocopy is holding restrictively**, so
  polling does not blank out mid-pass. Every read is still wrapped, because
  "tolerates a lock" must not rest on that continuing to be true.

Stdlib only (tkinter + ctypes), so there is no dependency to vet and it runs in
the experiment env as-is.

What it deliberately does NOT show
----------------------------------
Percentage progress *within* a pass. The daemon runs robocopy with /NFL, so
per-file output does not exist to be counted, and its own prints are block-
buffered into daemon.out. Elapsed time of the running pass is honest and
available; a fake percentage would be worse than no percentage.
"""

import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

import datasync.data_paths as dp

POLL_MS = 2000              # a few local stat() calls; nothing over the network
STALE_MULT = 1.0            # heartbeat older than HEARTBEAT_STALE_S => not alive
LOG_TAIL = 12000            # bytes of sync.log to parse for the last summary

BG = "#161a21"
FG = "#e6e9ef"
DIM = "#8b95a7"
OK = "#4caf50"
WARN = "#ffb300"
BAD = "#ef5350"
ACCENT = "#5b9dd9"

# What each internal state is CALLED in the window. The internal names are kept
# (other code and --once JSON use them); only the wording shown to a human
# changes. "queued" in particular read as "a queue of jobs" when it means "a copy
# was requested and the daemon has not woken up to it yet".
_STATE_WORDS = {
    "idle": "up to date",
    "copying": "copying now",
    "queued": "copy requested",
    "stale": "not responding",
    "stopped": "not running",
    "no campaign": "no campaign set",
}


# ---------------------------------------------------------------------------
# State reading -- every function here is total: it never raises
# ---------------------------------------------------------------------------

def _age(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def _tail(path, nbytes=LOG_TAIL):
    """Last *nbytes* of a text file, or "" -- tolerant of a concurrent writer."""
    try:
        with open(path, "rb") as fid:
            try:
                fid.seek(-nbytes, os.SEEK_END)
            except OSError:
                fid.seek(0)
            return fid.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _fmt_age(sec):
    """Duration with an UNAMBIGUOUS unit.

    'm' next to a file count reads as a number, not a minute: "3m ago: +4 copied"
    was read as "4 minutes". Spelled-out units cost three characters and remove
    the ambiguity entirely.
    """
    if sec is None:
        return "-"
    if sec < 60:
        return "%.0f sec" % sec
    if sec < 3600:
        return "%.0f min" % (sec / 60)
    if sec < 86400:
        return "%.1f hr" % (sec / 3600)
    return "%.1f days" % (sec / 86400)


def running_process_names():
    """Lowercased exe names of running processes, via Toolhelp32.

    ctypes rather than a subprocess: shelling out to tasklist every POLL_MS would
    spawn a process (and, from a console-less parent, a console WINDOW) on every
    tick -- the exact annoyance this widget exists to replace.
    """
    try:
        import ctypes
        from ctypes import wintypes

        MAX_PATH = 260
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_char * MAX_PATH)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == wintypes.HANDLE(-1).value:
            return set()
        try:
            e = PROCESSENTRY32()
            e.dwSize = ctypes.sizeof(PROCESSENTRY32)
            names = set()
            if not k32.Process32First(snap, ctypes.byref(e)):
                return names
            while True:
                names.add(e.szExeFile.decode("mbcs", "replace").lower())
                if not k32.Process32Next(snap, ctypes.byref(e)):
                    break
            return names
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return set()


def work_area():
    """``(left, top, right, bottom)`` of the desktop EXCLUDING the taskbar.

    ``winfo_screenheight()`` is the whole screen, so positioning from it puts the
    widget UNDERNEATH the taskbar. SPI_GETWORKAREA is the rectangle Windows itself
    reserves for normal windows, which is what "just above the taskbar" means.
    Falls back to the full screen if the call fails.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        SPI_GETWORKAREA = 0x0030
        r = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


def make_borderless_but_taskbarred(tk_window):
    """Strip the title bar while KEEPING a taskbar button. Returns True on success.

    tkinter's ``overrideredirect(True)`` gives a clean frameless card but removes
    the window from the taskbar entirely -- so once it slid behind another window
    there was no way to get it back short of relaunching. Windows decides taskbar
    presence from the window STYLES, not from tkinter, so the fix is to keep an
    ordinary top-level window (which does get a button) and clear the frame bits
    by hand:

      * GWL_STYLE  -- drop WS_CAPTION / WS_THICKFRAME, so no title bar and no
        resize border. WS_MINIMIZEBOX is KEPT, or iconify() cannot work and the
        taskbar button would have nothing to restore.
      * GWL_EXSTYLE -- set WS_EX_APPWINDOW and clear WS_EX_TOOLWINDOW. A tool
        window is deliberately hidden from the taskbar; that is the pair of bits
        that actually decides it.
      * SWP_FRAMECHANGED -- without it Windows keeps drawing the old frame until
        something else forces a recalculation.
    """
    try:
        import ctypes
        from ctypes import wintypes

        GWL_STYLE, GWL_EXSTYLE = -16, -20
        WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000
        WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00020000, 0x00010000
        WS_EX_APPWINDOW, WS_EX_TOOLWINDOW = 0x00040000, 0x00000080
        SWP_FRAMECHANGED = 0x0020
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER = 0x0002, 0x0001, 0x0004

        u = ctypes.windll.user32
        get_l = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
        set_l = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)

        tk_window.update_idletasks()
        # winfo_id() is the Tk child; the real top-level HWND is its parent.
        hwnd = u.GetParent(tk_window.winfo_id()) or tk_window.winfo_id()

        st = get_l(hwnd, GWL_STYLE)
        st &= ~(WS_CAPTION | WS_THICKFRAME | WS_MAXIMIZEBOX)
        st |= WS_MINIMIZEBOX
        set_l(hwnd, GWL_STYLE, st)

        ex = get_l(hwnd, GWL_EXSTYLE)
        ex &= ~WS_EX_TOOLWINDOW
        ex |= WS_EX_APPWINDOW
        set_l(hwnd, GWL_EXSTYLE, ex)

        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                       SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)
        return True
    except Exception:
        return False


def _last_summary(log_text):
    """(copied, skipped, failed, ended_text) from the LAST summary in sync.log.

    sync.log is appended across every pass, so only the final table is current --
    reading an earlier one would report a stale pass as the live one.
    """
    copied = skipped = failed = None
    ended = None
    for line in log_text.splitlines():
        s = line.strip()
        low = s.lower().replace(" ", "")
        if low.startswith("files:"):
            nums = []
            for tok in s.split(":", 1)[1].split():
                try:
                    nums.append(int(tok))
                except ValueError:
                    nums = None
                    break
            if nums and len(nums) >= 6:
                copied, skipped, failed = nums[1], nums[2], nums[4]
        elif low.startswith("ended:"):
            ended = s.split(":", 1)[1].strip()
    return copied, skipped, failed, ended


def snapshot(campaign=None):
    """Everything the widget shows, as a plain dict. Never raises."""
    snap = {"error": None, "campaign": None, "alive": False, "state": "unknown",
            "pid": None, "hb_age": None, "pending": False, "claims": [],
            "copying": False, "n_robocopy": 0, "pass_age": None,
            "copied": None, "skipped": None, "failed": None, "ended": None,
            "free_gb": None, "total_gb": None, "source": None, "targets": [],
            "any_failed": False, "pass_seconds": None, "partial": False}
    try:
        snap["campaign"] = dp.active_campaign(campaign)
    except Exception as exc:
        # No campaign declared is a normal, expected state -- say so plainly
        # instead of showing a traceback on the desktop.
        snap["error"] = str(exc).splitlines()[0]
        snap["state"] = "no campaign"
        return snap

    try:
        import datasync.data_sync as ds
        sync = dp.sync_dir("C", campaign)
        stale_s = ds.HEARTBEAT_STALE_S * STALE_MULT

        snap["hb_age"] = _age(os.path.join(sync, "heartbeat"))
        snap["alive"] = snap["hb_age"] is not None and snap["hb_age"] < stale_s
        try:
            with open(os.path.join(sync, "daemon.lock")) as fid:
                snap["pid"] = int(fid.read().split()[0])
        except Exception:
            snap["pid"] = None
        snap["pending"] = os.path.exists(os.path.join(sync, "sync_now"))
        try:
            snap["claims"] = [os.path.basename(p.rstrip("\\/"))
                              for p in ds.active_paths(campaign)]
        except Exception:
            snap["claims"] = []

        snap["n_robocopy"] = sum(1 for n in running_process_names()
                                 if n == "robocopy.exe")
        snap["copying"] = snap["n_robocopy"] > 0

        # Prefer last_pass.json: it is the daemon's OWN per-target totals for the
        # whole sweep. sync.log cannot give that -- it is appended once per target,
        # so a reader cannot tell which summaries belong to the current sweep, and
        # its LAST table is the campaign-root pass, which normally copies nothing.
        # Falling back to the log keeps the widget useful against a daemon started
        # before last_pass.json existed (it stays in memory until restarted).
        lp = os.path.join(sync, "last_pass.json")
        payload = None
        try:
            with open(lp) as fid:
                payload = json.load(fid)
        except Exception:
            payload = None

        if isinstance(payload, dict):
            snap["source"] = "last_pass.json"
            snap["pass_age"] = _age(lp)
            snap["copied"] = payload.get("copied")
            snap["skipped"] = payload.get("skipped")
            snap["failed"] = payload.get("failed_files")
            snap["any_failed"] = bool(payload.get("any_failed"))
            snap["targets"] = [t.get("week") for t in (payload.get("targets") or [])]
            snap["ended"] = payload.get("when")
            snap["pass_seconds"] = payload.get("seconds")
        else:
            snap["source"] = "sync.log (stale daemon)"
            log = os.path.join(sync, "sync.log")
            snap["pass_age"] = _age(log)
            c, s, f, ended = _last_summary(_tail(log))
            (snap["copied"], snap["skipped"],
             snap["failed"], snap["ended"]) = c, s, f, ended
            snap["partial"] = True      # one target only; see the comment above

        if not snap["alive"]:
            snap["state"] = "stopped" if snap["hb_age"] is None else "stale"
        elif snap["copying"]:
            snap["state"] = "copying"
        elif snap["pending"]:
            snap["state"] = "queued"
        else:
            snap["state"] = "idle"
    except Exception as exc:
        snap["error"] = "%s: %s" % (type(exc).__name__, exc)

    try:
        import shutil
        u = shutil.disk_usage(dp.ROOT_C + os.sep)
        snap["free_gb"], snap["total_gb"] = u.free / 1e9, u.total / 1e9
    except Exception:
        pass
    return snap


# ---------------------------------------------------------------------------
# The widget
# ---------------------------------------------------------------------------

class MirrorWidget(tk.Tk):

    def __init__(self, campaign=None, dock_corner="bottom-right"):
        tk.Tk.__init__(self)
        self.campaign = campaign
        self.dock_corner = dock_corner
        self._drag = (0, 0)

        # State for the off-thread "is this folder on S:?" check. The comparison
        # walks both trees (S: is over the network), so it runs in a worker thread
        # and hands the result back through this queue, drained on the UI thread.
        self._check_q = queue.Queue()
        self._checking = False
        self._result_win = None
        self._result_txt = None

        # NOT overrideredirect: that removes the taskbar button. Borderless is
        # achieved by clearing the frame styles instead, which keeps the button.
        self.title("Data backup status")
        self.attributes("-topmost", True)     # floats above everything
        self.attributes("-alpha", 0.94)
        self.configure(bg=BG)
        self.iconname("Backup")
        # Custom window + taskbar icon. The taskbar button that
        # make_borderless_but_taskbarred deliberately keeps would otherwise show
        # the default Tk feather; `default=` also hands the same icon to the
        # "check S:" result window. Defensive: a missing or blocked icon file must
        # never stop the widget from coming up.
        try:
            _ico = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "backup.ico")
            if os.path.isfile(_ico):
                self.iconbitmap(default=_ico)
        except Exception:
            pass

        mono = tkfont.Font(family="Consolas", size=9)
        bold = tkfont.Font(family="Consolas", size=9, weight="bold")
        title = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        card = tk.Frame(self, bg=BG, highlightthickness=1,
                        highlightbackground="#2b3240")
        card.pack(fill="both", expand=True)

        # -- title row doubles as the drag handle ---------------------------
        head = tk.Frame(card, bg=BG)
        head.pack(fill="x", padx=8, pady=(6, 2))
        self.dot = tk.Label(head, text="●", bg=BG, fg=DIM, font=bold)
        self.dot.pack(side="left")
        self.title_lbl = tk.Label(head, text="backup", bg=BG, fg=FG, font=title)
        self.title_lbl.pack(side="left", padx=(4, 0))
        close = tk.Label(head, text="✕", bg=BG, fg=DIM, font=mono,
                         cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.destroy())
        # Minimise is only useful because the window now HAS a taskbar button to
        # come back from; WS_MINIMIZEBOX is kept for exactly this.
        mini = tk.Label(head, text="–", bg=BG, fg=DIM, font=mono,
                        cursor="hand2")
        mini.pack(side="right", padx=(0, 8))
        mini.bind("<Button-1>", lambda e: self.iconify())
        for w in (head, self.dot, self.title_lbl):
            w.bind("<Button-1>", self._grab)
            w.bind("<B1-Motion>", self._move)

        self.camp_lbl = tk.Label(card, text="", bg=BG, fg=DIM, font=mono,
                                 anchor="w", justify="left")
        self.camp_lbl.pack(fill="x", padx=8)

        self.rows = {}
        for key in ("state", "last", "claims", "disk"):
            lbl = tk.Label(card, text="", bg=BG, fg=FG, font=mono,
                           anchor="w", justify="left", wraplength=300)
            lbl.pack(fill="x", padx=8, pady=(2, 0))
            self.rows[key] = lbl

        btns = tk.Frame(card, bg=BG)
        btns.pack(fill="x", padx=8, pady=(6, 7))
        # The widget owns the daemon's lifecycle now, so it needs BOTH directions.
        # The notebooks no longer start it.
        self.b_start = self._btn(btns, "start", self._start)
        self.b_stop = self._btn(btns, "stop", self._stop)
        self._btn(btns, "sync now", self._sync_now)
        # Read-only spot check: pick a C: folder, see whether S: already has all
        # of it. Distinct from "sync now" -- it copies nothing, it only compares.
        self._btn(btns, "check S:", self._check_folder)
        self.msg = tk.Label(btns, text="", bg=BG, fg=ACCENT, font=mono)
        self.msg.pack(side="right")

        # Borderless-with-taskbar-button, then dock. Both need the window to have
        # been realised, so they run after the layout is built.
        self.update_idletasks()
        self.borderless = make_borderless_but_taskbarred(self)
        self.dock(self.dock_corner)
        self._tick()

    def dock(self, corner="bottom-right", margin=12):
        """Park the card in a corner of the WORK AREA (never under the taskbar)."""
        self.update_idletasks()
        w = self.winfo_width() or self.winfo_reqwidth()
        h = self.winfo_height() or self.winfo_reqheight()
        wa = work_area()
        if wa:
            left, top, right, bottom = wa
        else:
            left, top = 0, 0
            right, bottom = self.winfo_screenwidth(), self.winfo_screenheight()
        x = (right - w - margin) if corner.endswith("right") else (left + margin)
        y = (bottom - h - margin) if corner.startswith("bottom") else (top + margin)
        self.geometry("+%d+%d" % (x, y))
        return x, y

    def _btn(self, parent, text, cmd):
        b = tk.Label(parent, text=text, bg="#232a35", fg=FG,
                     font=tkfont.Font(family="Consolas", size=9),
                     padx=8, pady=2, cursor="hand2")
        b.pack(side="left", padx=(0, 6))
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg="#2d3644"))
        b.bind("<Leave>", lambda e: b.configure(bg="#232a35"))
        return b

    # -- dragging ----------------------------------------------------------
    def _grab(self, ev):
        self._drag = (ev.x_root - self.winfo_x(), ev.y_root - self.winfo_y())

    def _move(self, ev):
        self.geometry("+%d+%d" % (ev.x_root - self._drag[0],
                                  ev.y_root - self._drag[1]))

    # -- actions -----------------------------------------------------------
    def _flash(self, text):
        self.msg.configure(text=text)
        self.after(2500, lambda: self.msg.configure(text=""))

    def _sync_now(self):
        try:
            import datasync.data_sync as ds
            self._flash("triggered" if ds.sync_now(self.campaign) else "failed")
        except Exception as exc:
            self._flash(type(exc).__name__)

    def _start(self):
        """Start the daemon, REPLACING any existing one.

        ensure_daemon alone is not enough: it is a no-op whenever the heartbeat is
        fresh, so it silently keeps an OLD process alive -- which is how a daemon
        running pre-fix code survived every "restart". Stopping first makes this
        button mean "the daemon is now running current code", which is what a
        Start button should guarantee.
        """
        try:
            import datasync.data_sync as ds
            alive, _ = ds.daemon_alive(self.campaign)
            if alive:
                ds.stop_daemon(self.campaign, verbose=False)
                time.sleep(0.6)
            ds.ensure_daemon(self.campaign, verbose=False)
            alive, _ = ds.daemon_alive(self.campaign)
            self._flash("started" if alive else "failed to start")
        except Exception as exc:
            self._flash(type(exc).__name__)

    def _stop(self):
        """Confirm first: stopping the backup leaves new data on C: only."""
        from tkinter import messagebox
        if not messagebox.askokcancel(
                "Stop the background backup?",
                "This stops the background copy from C: to S:.\n\n"
                "Nothing is deleted and no data is lost -- but new data stays on "
                "C: only until you press start again.\n\nStop it?"):
            return
        try:
            import datasync.data_sync as ds
            pid = ds.stop_daemon(self.campaign, verbose=False)
            self._flash("stopped %s" % (pid or "-"))
        except Exception as exc:
            self._flash(type(exc).__name__)

    # -- "is this folder already on S:?" check -----------------------------
    def _check_folder(self):
        """Pick a C: folder and compare it, verbatim, against its S: mirror.

        Read-only: it walks both trees and reports what S: is missing or has a
        different-sized copy of -- it never copies anything (that is "sync now").
        The walk runs in a worker thread so a large folder cannot freeze the
        widget, and the result comes back through a queue drained on the UI thread
        because tkinter may only be touched from the thread that created it.
        """
        from tkinter import filedialog, messagebox
        try:
            if self._checking:
                self._flash("check running")
                return
            initial = dp.ROOT_C if os.path.isdir(dp.ROOT_C) else None
            folder = filedialog.askdirectory(
                parent=self, title="Pick a C: folder to check against S:",
                initialdir=initial, mustexist=True)
            if not folder:
                return
            folder = os.path.abspath(folder)
            try:
                mirror = dp.mirror_path(folder)     # C: path -> its S: counterpart
            except Exception as exc:
                messagebox.showerror(
                    "Can't check this folder",
                    "This folder can't be compared with S:.\n\n%s\n\nPick a "
                    "folder under %s." % (exc, dp.ROOT_C))
                return
            self._checking = True
            self._flash("checking S: ...")
            self._open_result_window(folder, mirror)
            threading.Thread(target=self._check_worker,
                             args=(folder, mirror), daemon=True).start()
            self.after(200, self._poll_check)
        except Exception as exc:                     # a UI action must not crash
            self._checking = False
            self._flash(type(exc).__name__)

    def _check_worker(self, c_root, s_root):
        """Thread body: the two-tree comparison. Puts a plain dict on the queue.

        Touches no widget -- everything it produces is handed to the UI thread via
        the queue, which :meth:`_poll_check` drains.
        """
        payload = {"c_root": c_root, "s_root": s_root}
        try:
            import datasync.data_verify as dv
            t0 = time.time()
            payload["result"] = dv.compare_trees(c_root, s_root,
                                                 tier=dv.DEFAULT_TIER)
            payload["seconds"] = time.time() - t0
        except Exception as exc:
            payload["error"] = "%s: %s" % (type(exc).__name__, exc)
        self._check_q.put(payload)

    def _poll_check(self):
        """UI-thread drain of the check queue -- the ONLY place a check result is
        turned into widgets, so all tkinter access stays on the main thread."""
        try:
            payload = self._check_q.get_nowait()
        except queue.Empty:
            if self._checking:
                self.after(200, self._poll_check)
            return
        self._checking = False
        res = payload.get("result")
        if res is None:
            self._flash("check failed")
        elif res["n_divergences"] == 0 and not res["n_walk_errors"]:
            self._flash("S: has everything")
        else:
            self._flash("S: missing %d" % res["n_divergences"])
        self._render_result(payload)

    # -- result window -----------------------------------------------------
    def _ensure_result_window(self):
        """A single reused, scrollable, read-only report window. Recreated if the
        user closed the previous one."""
        win = self._result_win
        if win is not None:
            try:
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    return
            except tk.TclError:
                pass
        win = tk.Toplevel(self)
        win.title("Is this folder already on S:?")
        win.configure(bg=BG)
        txt = tk.Text(win, bg=BG, fg=FG, insertbackground=FG, wrap="none",
                      width=94, height=30, borderwidth=0, highlightthickness=0,
                      font=tkfont.Font(family="Consolas", size=9))
        ys = tk.Scrollbar(win, orient="vertical", command=txt.yview)
        xs = tk.Scrollbar(win, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        txt.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        win.rowconfigure(0, weight=1)
        win.columnconfigure(0, weight=1)
        self._result_win = win
        self._result_txt = txt

    def _set_result_text(self, text):
        self._ensure_result_window()
        txt = self._result_txt
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        txt.insert("1.0", text)
        txt.configure(state="disabled")

    def _open_result_window(self, c_root, s_root):
        self._set_result_text(
            "Comparing this folder on C: with S: -- please wait.\n\n"
            "C:  %s\nS:  %s\n\nWalking both trees. S: is over the network, so a "
            "large folder can take a little while; the widget stays live."
            % (c_root, s_root))

    def _render_result(self, payload):
        if payload.get("result") is None:
            self._set_result_text(
                "The check could not run.\n\nC:  %s\nS:  %s\n\n%s"
                % (payload["c_root"], payload["s_root"],
                   payload.get("error", "unknown error")))
            return
        import datasync.data_verify as dv
        text = dv.format_comparison(payload["c_root"], payload["s_root"],
                                    payload["result"])
        if payload.get("seconds") is not None:
            text += "\n\n(checked in %.1f s)" % payload["seconds"]
        self._set_result_text(text)

    # -- refresh -----------------------------------------------------------
    def _tick(self):
        try:
            self._render(snapshot(self.campaign))
        except Exception as exc:                 # a UI bug must not kill the UI
            self.rows["state"].configure(text="ui error: %r" % (exc,), fg=BAD)
        self.after(POLL_MS, self._tick)

    def _render(self, s):
        colour = {"copying": ACCENT, "idle": OK, "queued": WARN,
                  "stale": WARN, "stopped": BAD}.get(s["state"], DIM)
        self.dot.configure(fg=colour)
        # Dim the action that does not apply, so the buttons say what state we are
        # in as well as what they do.
        running = s["state"] in ("idle", "copying", "queued")
        self.b_start.configure(fg=DIM if running else FG)
        self.b_stop.configure(fg=FG if running else DIM)
        self.title_lbl.configure(text="backup · %s" % _STATE_WORDS.get(s["state"], s["state"]))
        self.camp_lbl.configure(
            text=(s["campaign"] or s.get("error") or "no campaign"))

        if s["state"] == "no campaign":
            self.rows["state"].configure(
                text="no campaign set - run the notebook startup cell", fg=WARN)
            for k in ("last", "claims", "disk"):
                self.rows[k].configure(text="")
            return

        bits = []
        if s["pid"]:
            bits.append("pid %d" % s["pid"])
        bits.append("heartbeat %s ago" % _fmt_age(s["hb_age"]))
        if s["n_robocopy"]:
            bits.append("%d robocopy" % s["n_robocopy"])
        if s["pending"]:
            bits.append("copy requested")
        self.rows["state"].configure(text="  ".join(bits), fg=colour)

        if s["copied"] is None:
            last = "no copy done yet"
        else:
            last = ("last copy %s ago:\n  %s new files, %s already on S:, "
                    "%s failed%s"
                    % (_fmt_age(s["pass_age"]), s["copied"], s["skipped"],
                       s["failed"],
                       "\n  (partial count - press start to refresh)"
                       if s["partial"] else ""))
        self.rows["last"].configure(
            text=last,
            fg=BAD if (s["failed"] or 0) or s["any_failed"]
            else (WARN if s["partial"] else DIM))

        if s["claims"]:
            self.rows["claims"].configure(
                text=("waiting for the experiment to finish:\n  "
                      + "\n  ".join(s["claims"][:3])),
                fg=WARN)
        else:
            self.rows["claims"].configure(
                text="no experiment is writing right now", fg=DIM)

        if s["free_gb"] is not None:
            self.rows["disk"].configure(
                text="C: %.0f GB free of %.0f" % (s["free_gb"], s["total_gb"]),
                fg=BAD if s["free_gb"] < 100 else DIM)


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Floating status widget for the "
                                            "C: -> S: mirror daemon.")
    p.add_argument("--campaign", default=None)
    p.add_argument("--dock", default="bottom-right",
                   choices=("bottom-right", "bottom-left", "top-right", "top-left"),
                   help="corner of the work area to park in (default bottom-right, "
                        "just above the taskbar)")
    p.add_argument("--once", action="store_true",
                   help="print one snapshot as JSON and exit (no window)")
    a = p.parse_args(argv)
    if a.once:
        print(json.dumps(snapshot(a.campaign), indent=1, default=str))
        return 0
    MirrorWidget(a.campaign, dock_corner=a.dock).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
