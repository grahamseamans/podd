# podd — a RunPod pod job runner (GPU and CPU pods)

**podd** (`python3 -m podd run`, the systemd user unit `systemd/podd.service`) owns your RunPod pods and runs jobs for
any number of clients: a client drops a job directory in a queue directory podd watches, podd runs it on a pod that
satisfies what the job requires (opening one if none does, on a GPU pod or, for a job that requires no card, a CPU
pod), brings the outputs home, and writes back what it ran on. It knows nothing about any client.

- The contract is `podd.job_contract` (`src/podd/job_contract.py`): the queue's file names, the JOB file's keys, the
  config. Standard library only, and so is all of podd (`tests/test_podd.py` checks every import).
- The pattern (who owns what, every rule and why) is `architecture/README.md`.
- Python 3.10+, bash, rsync, ssh, curl on the machine that runs podd. Nothing else.

## Install

    git clone <this repo> ~/podd                               # the unit expects ~/podd (else edit its PYTHONPATH)
    mkdir -m 700 -p ~/.secrets && $EDITOR ~/.secrets/runpod_api_key        # src/podd/secrets.sh says what goes there
    mkdir -p ~/.config/podd && cp ~/podd/config.example.json ~/.config/podd/config.json && $EDITOR ~/.config/podd/config.json
    ln -sf ~/podd/systemd/podd.service ~/.config/systemd/user/ && systemctl --user daemon-reload &&
        systemctl --user enable --now podd
    journalctl --user -u podd -f                                # every pod, every job, every failure
    PYTHONPATH=~/podd/src python3 -m podd status                # the pods, and every queue's stages with state and age
    systemctl --user restart podd                               # after a config change (read once, at start)

Restart only when nothing is RUNNING: a stop stops every running job, pulls it home and requeues or fails it before
the pods go (`SHUTDOWN_PULL_SECONDS` 840, `TimeoutStopSec` 900). Never run two podds: each deletes every pod pod.sh
records when it starts.

## Clients

A client imports the contract from the installed package:

    pip install -e ~/podd                     # into the client's environment (or uv pip install -e ~/podd)

    from podd import job_contract
    job_contract.require_queue_watched(queue_directory)     # podd's config lists this queue, or nothing would run it
    job_contract.write_job(stage, {...})                     # checks the contract; refuses a broken JOB loudly
    job_contract.stage_state(stage)                          # JOB | RUNNING | DONE | FAILED | None
    job_contract.read_state(stage, job_contract.DONE)["used"]   # what the job ran on
    job_contract.mark_stage_failed(stage, reason, where)     # the one FAILED writer

or writes the JSON by hand (the docstring of `job_contract.py` has every key). A client never talks to RunPod: no
pod.sh, no ssh, no rsync.

## The config

`~/.config/podd/config.json` (`$PODD_CONFIG` overrides it, for tests). Every key is checked
(`job_contract.read_config`); `config.example.json` shows each:

- `queue_directories`: absolute paths; each subdirectory is a stage (one job). A new client = one more directory here
  (and a restart).
- `max_pods`: `{"gpu": n, "cpu": n}`, GPU and CPU pods capped apart (2026-10-15: hour-long CPU jobs held all 8 shared
  slots and starved a GPU sweep).
- `idle_minutes`: a ready pod with no job this long is deleted.
- `max_price_per_hour`: a card (or a CPU pod's flavor x vCPUs) over it is never asked for (`pod.sh avail`: RunPod's
  securePrice and stock). create is offered EVERY accepted card under it, listed-in-stock first, cheapest first.
- `cards`: the cards a GPU pod may be, each with RunPod's own `gpuTypes.memoryInGb`.
- `min_cuda_version` (e.g. `"12.8"`): the lowest driver CUDA version (nvidia-smi's "CUDA Version") your images run.
  A GPU create asks RunPod for ANY host (its `allowedCudaVersions` enum stops at 13.0, so a list shut out every newer
  driver: 2026-10-29, every create refused for 30+ min while A40s and L4s under the cap were in stock); podd measures
  the driver on the pod (`cuda_version`) and deletes a pod below this at once, loudly, avoids its datacenter for GPU
  pods for `OLD_DRIVER_DATACENTER_HOURS` (even while short of stock: such a pod runs nothing), and counts it as a
  stock miss (the jobs stay JOB). An image that refuses the driver at container start (`NVIDIA_REQUIRE_CUDA`) never
  opens ssh: `pod.sh create` gives up after 15 min, a boot failure. The old key `allowed_cuda_versions` is refused by
  name.
- `cpu_flavors`: the RunPod CPU flavors a CPU pod may be (`pod.sh avail-cpu`: $/vCPU-h and RAM per vCPU from the REST
  v2 catalog).
- `boot_seconds` (default 150, the measured 97-155 s): podd opens another pod only while the backlog would take
  longer than this to drain on the pods up or booting.
- `max_wait_for_stock_minutes` (default 60): RunPod refusing a create for stock ("no instances currently available")
  is waited out, never a failure: the jobs stay JOB and podd retries one pod after 60 s, doubling to 10 min, avoiding
  no slow datacenter (an old driver's still: such a pod runs nothing). While a pod of the set runs, its jobs wait for it however long; a set with NO
  pod for this long FAILS its jobs with every miss. `podd status` shows each wait and when it would fail.

## A job

    <queue directory>/<stage>/JOB   {"command": "bash solve.sh input.json",
                                     "inputs": ["input.json", "solve.sh"],
                                     "outputs": ["result", "solve.log"],
                                     "requires": {"image": "ghcr.io/you/your-image:<sha>", "solver_version": 1,
                                                  "gpu_memory_gb_min": 20, "disk_gb_min": 80},
                                     "cpu": 2.5, "gpu_memory_gb": 1, "ram_gb": 2.5, "expected_s": 55,
                                     "label": "..."}
    a job that can resume also:     "progress": "progress", "resumable": true,
                                     "resume_from": ["result/manifest.json", "solve.log"]

`JOB` -> `RUNNING` (podd claimed it) -> `DONE` (+ `used`: the pod's facts, + `timings`: ship_bytes, ship_s, wait_s,
command_wall_s, pull_s) or `FAILED` (why, the traceback, command.log's tail), or back to `JOB` (a resumable job podd
stopped part-way, + a record in `attempts`).

- `outputs` come home LAID OVER the stage's copy: a file the attempt wrote replaces the stage's, a file only the stage
  has stays. podd never deletes a file of the stage.

- `requires.image` is what podd launches; `gpu` (`"any"`, a list of card names, or `"none"`: a CPU pod),
  `gpu_memory_gb_min`, `cpu_min` are measured on the pod; `disk_gb_min` (decimal GB of container disk, optional) is
  what podd creates the pod with when it is more than the default (30 GB on a GPU pod, 20 on a CPU pod; a CPU pod
  gets 1 vCPU for every 10 GB, so a large disk makes it larger), matched against RunPod's `containerDiskInGb` for the
  pod; every other key is matched against the facts the image declares in `/etc/podd-facts.json` (below).
- `cpu` (cores, or `"all"`: the pod's whole quota, alone), `gpu_memory_gb`, `ram_gb`: what it uses, packed against
  what podd measured the pod has.
- `expected_s` (required): its wall time ALONE on a FAST host. podd's drain estimate, and the deadline:
  `TIMEOUT_MARGIN` 4 x expected_s x the pod's measured slowness x the most jobs the pod ran at once during it.
- `progress` (optional): a file the command keeps rewriting as `<done> <total>`; podd measures the pod's speed from
  it: the job's pace (seconds per unit over expected_s / total) against the median pace of at least 2 other pods
  running the same command at the same load. At 1.5x that the pod is SLOW. So keep expected_s in proportion across a
  command's jobs: a job twice the work says twice the expected_s.
- `resumable` (optional, with `progress` and `resume_from`): podd requeues the job when it stops it part-way (a slow
  pod, its deadline after progress, its pod broke, podd stopping; at most MAX_ATTEMPTS) and the command, run again,
  does only the rest. What earlier attempts brought home STAYS HOME (2026-10-21: it went back up this machine's
  ~1 MB/s uplink, 0.2-2.2 GB a job, into the 600 s ssh timeout): the next attempt gets the inputs and the
  `resume_from` files only (the SMALL files inside the outputs that say what is done: a manifest, a log to append
  to), and its outputs are laid over the stage's. So the command writes the units it did plus its `resume_from`
  files brought up to date, never needs a finished unit's file on the pod, and never leaves on the pod a file that
  would be wrong laid over the stage.

A CPU job (`requires.gpu: "none"`, `gpu_memory_gb` 0) runs on a CPU pod that podd sizes from the set's waiting jobs
(`cpu_pod_plan`: as many of them as one pod holds by `cpu` and `ram_gb`, vCPUs a power of two, under
`max_price_per_hour`, the flavor cheapest per job) and creates with `pod.sh create-cpu`.

## Images

An image podd can run starts sshd with RunPod's PUBLIC_KEY on its own (port 22), carries bash, awk, procps
(ps / pkill / pgrep) and rsync, and declares its facts in `/etc/podd-facts.json` (a JSON object, e.g.
`{"image_commit": "...", "solver_version": 1}`). podd measures `pod_name`, `pod_id`, `image`, `gpu`,
`gpu_memory_gb`, `cuda_version`, `cpu`, `ram_gb`, `disk_gb`, `datacenter`, `cost_per_hr`, `ready_at` itself, so an
image may not declare those.
A pod whose image declares no facts is refused. Nothing is installed on a pod: a pod from the image runs jobs as soon
as ssh answers.

## What podd runs for one job

On pod `<name>` (`J=/workspace/jobs/<digest>-<stage>`):

    ssh runpod-<name> mkdir -p $J/.podd
    rsync -rtR <stage>/./<input>... [<stage>/./<resume_from>...] runpod-<name>:$J/
    ssh runpod-<name> "cat > $J/.podd/command"                          # the JOB's command, on stdin
    ssh runpod-<name> "nohup setsid bash /workspace/podd/podd-run.sh $J >/dev/null 2>&1 < /dev/null &"
    ssh runpod-<name> cat $J/.podd/exit [; cat $J/<progress>]          # every 2 s, until {"exit_status", "wall_s"}
    rsync -rtR runpod-<name>:$J/./<output>... runpod-<name>:$J/./command.log <stage>/      # laid over: no --delete
    ssh runpod-<name> rm -rf $J

`podd-run.sh` (shipped once per pod) runs the command in the job's own session (its pid = the session id, in
`.podd/pid`); past the job's deadline, or to move it off a slow pod, podd kills that session only (`pkill -9 -s`),
pulls back what exists, and requeues or FAILS the job.

## The pods: pod.sh

`src/podd/pod.sh` drives the RunPod API, each pod by NAME (podd names them podd-g1 .. podd-g<max_pods.gpu> and
podd-c1 .. podd-c<max_pods.cpu>). The key comes from `$SECRETS` (`src/podd/secrets.sh`: `~/.secrets` unless
`$PODD_SECRETS`) and reaches curl only through a header file, never argv; each pod's id, price and lease are in
`$SECRETS/runpod_pods/<name>.*`:

    pod.sh avail <cards>                 # JSON: each card's $/h and the datacenters stocking it now
    pod.sh create <name> <image> <cards> <min vCPU> <disk GB> [<avoid datacenters>]   # podd's call
    pod.sh avail-cpu <flavors>           # JSON: each CPU flavor's $/vCPU-h, RAM/vCPU, vCPU range, stock
    pod.sh create-cpu <name> <image> <flavors> <vCPU> <disk GB> [<avoid datacenters>]  # podd's call for a CPU pod
    pod.sh status <name> | delete <name> # delete prints the session's minutes and cost
    pod.sh recorded                      # the pods recorded here
    pod.sh account                       # every pod the RunPod account has: the leak check

`create` passes `imageName`, `ports` 22/tcp, the container disk podd asks (30 GB unless a job requires more), no volume,
no `allowedCudaVersions` (any host: podd checks the driver itself), any datacenter (or, given ones to
avoid, `dataCenterIds` = pod.sh's `RUNPOD_DATACENTERS` less them), and no `PUBLIC_KEY` (RunPod injects the account's
ssh keys). It writes the `runpod-<name>` block of `~/.ssh/config`: the key, `BatchMode`, `ConnectTimeout 10`,
`ServerAliveInterval 15` x `ServerAliveCountMax 4` (a dropped connection fails in ~60 s instead of hanging) and
`ControlMaster auto` / `ControlPersist 600` (every ssh / rsync to the pod is a channel on ONE connection). A pod is
never stopped and restarted (a stopped pod is pinned to its host's GPUs): delete it and create another. Pod disk dies
with the pod. Every pod has a watchdog on this machine (`MAX_POD_HOURS`, default 2, after the lease podd renews while
the pod has work): the backstop for a podd that died.

## State on this machine

| Path | What |
|---|---|
| `~/.config/podd/config.json` | the config (read once, at start) |
| `~/.local/state/podd/PODS.json` | the pool, rewritten whenever it changes (no secrets); `podd status` prints it |
| `~/.local/state/podd/avoided-datacenters.json` | `{"gpu" \| "cpu": {datacenter: {..., "because": "slow" \| "old driver"}}}`: datacenters that kind's creates avoid: a SLOW pod's for `SLOW_DATACENTER_HOURS` unless the set is short of stock, an old driver's (GPU) for `OLD_DRIVER_DATACENTER_HOURS` always (delete an entry to forgive one, then restart) |
| `$SECRETS/runpod_api_key`, `$SECRETS/runpod_pods/` | the key, and pod.sh's per-pod files |

## Tests

    PYTHONPATH=src python3 tests/test_podd.py       # no pod, no RunPod: fakes for ssh, pod.sh and curl

## References

`references/` holds what the code cites (`references/runpod/INDEX.md`); only the INDEX files are in git, the mirrors are
re-made by `tools/` scripts.
