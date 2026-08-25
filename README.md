# datasync

An **additive** `C:` → `S:` mirror daemon plus an **independent two-source
verifier**, built on Windows `robocopy`. The daemon keeps a local data tree
mirrored to a network share and *never deletes* on either side; the verifier is
the paranoid check that gates ever reclaiming local space.

Extracted (lift-and-shift) from the `tprocv2_fluxonium` experiment repo, where it
keeps a lab's measurement data continuously backed up from a local SSD to an SMB
share.

> **Windows only.** It shells out to `robocopy` and relies on `\\?\` long paths,
> detached-process creation flags, and `taskkill`. It will not run on Linux/macOS.

## The safety model

- **Additive, always.** `/MIR`, `/PURGE`, `/MOV`, `/MOVE` are refused by a guard
  (`_assert_nondestructive`) on *every* invocation — no code path can pass a
  switch that deletes on either side. The share keeps history the local disk no
  longer holds.
- **A false "clean" must be impossible.** Before anything may be deleted from the
  local disk, two independent implementations must agree it is safely on the
  mirror:
  - **Source A** — a pure-Python `os.scandir` walker of both trees (long-path
    safe, unreadable dirs are errors, not silently skipped).
  - **Source B** — `robocopy /L` (list-only): the copier's own verdict.
  Disagreement is a hard failure, never resolved in favour of either.
- **Tiers** (recorded in every receipt): `inventory` → `size` (default) →
  `sample` (head+tail SHA-256) → `full` (every byte). Deleting requires `sample`
  or better, plus a re-check of the mirror *right now*, plus proof the local tree
  has not changed since the receipt.
- **Never crashes a live writer.** The daemon coordinates with an active-path
  marker protocol so it does not open a file another process is writing.

## Requirements

- Windows with `robocopy` on `PATH` (ships with Windows).
- Python ≥ 3.9. Core is **stdlib-only**; two optional extras are imported lazily:
  - `progress` (`tqdm`) — live per-file progress bar.
  - `viz` (`matplotlib`) — the stacked-bar audit figure.

## Install

```bash
pip install -e "C:\path\to\datasync"
# with the optional extras:
pip install -e "C:\path\to\datasync[progress,viz]"
```

## Configure the endpoints

The local and mirror roots default to `C:\_Data` and `S:\_Data`. Override without
editing code via environment variables (this is also how the detached daemon
inherits them from its parent):

```bash
set TPROCV2_DATA_ROOT_C=C:\_Data
set TPROCV2_DATA_ROOT_S=S:\_Data
set TPROCV2_CAMPAIGN=<campaign folder name>
```

## Quickstart

```bash
# start the detached mirror daemon (idempotent; O(1) if already running)
python -m datasync.data_sync --ensure --campaign <name>

# one blocking pass, then exit
python -m datasync.data_sync --once --campaign <name>

# daemon + per-week receipt status
python -m datasync.data_sync --status --campaign <name>

# compare every week folder against the mirror (read-only)
python -m datasync.data_verify --audit --campaign <name>

# verify one week and write a delete-authorising receipt
python -m datasync.data_verify --verify <YYMMDD> --tier sample --campaign <name>

# an always-on-top status widget
python -m datasync.data_sync_ui
```

The installed console scripts `datasync-sync` and `datasync-verify` are aliases
for `python -m datasync.data_sync` / `datasync.data_verify`.

## Layout / domain model

`data_paths` encodes a specific on-disk layout: campaigns under the data root,
each holding `data\<YYMMDD-of-a-Monday>\` week folders, plus a sticky per-prefix
run-index ledger. `data_sync` and `data_verify` are otherwise generic engines
that call into `data_paths` for the roots, the units to mirror, and the mirror
mapping. If you adopt this for a different layout, `data_paths` is the module to
adapt. (The run-index ledger — `declare_campaign`, `next_campaign_index` — is
experiment-numbering, not backup logic; it rides along here but is a candidate to
split out later.)

## Tests

Offline, no hardware and no network share required — each builds fake trees in a
temp dir with the roots monkeypatched:

```bash
python tests/test_data_paths.py
python tests/test_data_sync.py
python tests/test_data_verify.py
```

(`test_data_sync.py` / `test_data_verify.py` exercise the real `robocopy` binary,
so they must run on Windows.)

## License

TODO — not yet chosen. Internal until then.
