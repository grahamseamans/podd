# 2026-10-19 -- podd extracted from the monitoring repo (level 3)

Source: speakers/monitoring at 4d846ecfb8d7edea29106f08b9347b9117e15a69 (tools/runpod/ + tools/tests/test_podd.py,
last changed 056933764ef2bdae19e6d5e0f7ad2684e3f3f82d, 2026-10-16). Started fresh here (one first commit naming that
source) instead of carrying monitoring's history: this repo is public, and that history is a speaker project's
(absolute paths of one machine, unrelated files, the account's config in docs).

User decisions: podd is its own repo, totally separate from the acoustic tooling (acoustic-solve) and from any
project; refactor level 3 (no shims; clients move to the new shape).

## What moved, and its new name

| monitoring | podd |
|---|---|
| tools/runpod/podjobs.py | src/podd/job_contract.py (`from podd import job_contract`) |
| tools/runpod/podd.py | src/podd/daemon.py (`python3 -m podd run`; `main` -> `run_forever`) |
| tools/runpod/podd-status | src/podd/status.py (`python3 -m podd status`) |
| tools/runpod/pod.sh, podd-run.sh, secrets.sh | src/podd/ (beside daemon.py, package data) |
| tools/runpod/podd.service | systemd/podd.service (`%h/podd/src` on PYTHONPATH) |
| tools/runpod/README.md (podd sections) | README.md; the BEAT image sections stayed in monitoring (tools/runpod/image/README.md) |
| architecture/solving.md (podd sections) | architecture/README.md |
| tools/tests/test_podd.py (podd's tests) | tests/test_podd.py; run-job.sh's resume test stayed (monitoring tools/tests/test_run_job.py) |
| tools/mirrors/runpod_docs.sh, references/runpod/INDEX.md, references/compute/runpod-rest-api-openapi-v1-2026-10-10.json | tools/mirror_runpod_docs.sh, references/runpod/INDEX.md (the mirror and the JSON on disk only) |

Stayed in monitoring (acoustic-solve's, a later task): tools/runpod/image/ (the Boundary Lab / BEAT image),
tools/run-job.sh (a blab solve).

## Shape changes

- A package with a pyproject (src layout, standard library only, no dependencies). Clients `pip install -e ~/podd`
  into their environment and `from podd import job_contract`; nothing reaches into podd by path.
- `max_price_per_hour` is a required config key (was a 0.60 default): the account's choices live in the config,
  never in code. `config.example.json` shows every key.
- secrets.sh: `$PODD_SECRETS` overrides `~/.secrets`; still a path convention only.
- Every speaker / room / blab word left podd's code and comments; measurements keep their dates and numbers.
- The import check covers every module of src/podd/.

## Steps

1. [done] Copy and rename; scrub; tests green here (`PYTHONPATH=src python3 tests/test_podd.py`).
2. [done] monitoring: run.py, room/build.py, test_run_guards.py import `podd.job_contract`; podd installed editable in
   ~/blab-venv; test_run_job.py split out; architecture/solving.md says podd is external; commit by path.
3. [done] The live unit: checked idle first (no stage in any queue, PODS.json `pods: []`, no pod recorded under
   $SECRETS/runpod_pods, `pod.sh account`: no pods), then `ln -sfn ~/podd/systemd/podd.service
   ~/.config/systemd/user/`, `systemctl --user daemon-reload`, `systemctl --user restart podd`; journal: "podd: up".
4. [open] GitHub: the repo grahamseamans/podd does not exist yet; remote `origin` is set, nothing pushed.
