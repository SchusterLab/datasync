r"""datasync -- additive Windows robocopy mirror plus an independent verifier.

A detached ``robocopy`` daemon keeps a local tree mirrored to a network share.
It is **additive only**: it copies, it never deletes, on either side (no
``/MIR``, no ``/PURGE``, no ``/MOV``). A separate, deliberately paranoid verifier
answers the one question a delete depends on -- "is this really on the mirror?"
-- using two independent sources (a pure-Python walker and ``robocopy /L``) that
must agree.

Submodules
----------
``data_paths``    campaign / week-folder layout + the sticky run-index ledger
``data_sync``     the additive mirror daemon (``python -m datasync.data_sync``)
``data_verify``   two-source verifier + prune-eligibility gate
                  (``python -m datasync.data_verify``)
``data_sync_ui``  an always-on-top tkinter status widget (Windows)

Windows-only: it shells out to ``robocopy`` and uses ``\\?\`` long paths,
detached-process creation flags, and ``taskkill``.
"""

__version__ = "0.1.0"
