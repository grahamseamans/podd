# SHORTCUTS

Jank taken on purpose, to come back to. Each entry: when it was taken, what, and what the proper fix is. An entry
under 24 hours old is dealt with and deleted, not left.

- 2026-10-19 14:55 PDT -- `systemd/podd.service` finds the package by `Environment=PYTHONPATH=%h/podd/src`, so the
  repo must be cloned at ~/podd (README.md says so). Proper fix: an install step (`pip install --user` into a
  dedicated venv, `ExecStart=<venv>/bin/podd run`) once there is a second machine; today there is one.
- 2026-10-19 14:55 PDT -- carried over, not new: `src/podd/daemon.py` is one ~1450-line module (pool, pod, job
  lifetimes, slow-host judging; architecture/README.md "KNOWN SHAPE DEBT"). Proper fix: split by owner (pool / pod /
  one job), with the tests' module patches (`daemon.subprocess`, `daemon.STATUS_FILE`, `daemon.POD_SCRIPT`) moved to
  the module that owns each.
