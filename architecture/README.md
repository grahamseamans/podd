# architecture

podd is one partition: the pods' and the jobs' lifetimes on RunPod, for any client. Its pieces and their one pattern
are below; violations should be very rare, and each one is written down here or in SHORTCUTS.md.

Why it exists: a pod costs money by the minute, takes ~2 min to make and dies with everything on it, and the work
around a job (building inputs, filing results) is local and free. The pattern keeps every RunPod call in one
long-lived process, so a batch makes the pods its backlog needs (capped), and nothing that builds or files a result
can leak a pod.

```
  a client                          WRITES JOBs: what to run and what it needs; reads DONE / FAILED; nothing else
        |                           (from podd import job_contract)
        v
  <queue directory>/<stage>/        THE CONTRACT (src/podd/job_contract.py): one directory per job, one state file
        |   JOB                     command, inputs, outputs, requires, cpu, gpu_memory_gb, expected_s (+ ram_gb,
        |                           progress, resumable, label): the client; podd again when it requeues (+ attempts)
        |   RUNNING                 podd claimed it (rename) for one pod (that pod's facts in it)
        |   DONE | FAILED           + used (what it ran on) + timings | why, the traceback, command.log's tail
        v
  src/podd/daemon.py                THE RUNNER (python3 -m podd run; systemd/podd.service): reads the config once;
        |                           up to max_pods.gpu GPU and max_pods.cpu CPU pods; a job goes to the fullest
        |                           ready pod whose facts satisfy its requires and whose free cpu / card memory / RAM
        |                           cover it; a pod is opened FOR a requirement set only while the set's backlog
        |                           would outlast a boot; idle pods deleted after idle_minutes, every pod when podd
        |                           stops or starts; deadlines, slow hosts, requeues.
        v
  src/podd/pod.sh <cmd> <name>      THE PODS' LIFECYCLE, each by name: create / create-cpu / delete / status against
        |                           the RunPod API, its ssh alias runpod-<name> (one ControlMaster each), its
        |                           watchdog (MAX_POD_HOURS after the lease podd renews); recorded, account, avail,
        |                           avail-cpu. Nothing about jobs.
        v
  src/podd/podd-run.sh              ON A POD: one job's command in its own session (pid = session id), command.log,
                                    .podd/exit {exit_status, wall_s}. Nothing about time.
  src/podd/status.py                python3 -m podd status: prints the status file and every queue. Decides nothing.
```

THE PATTERN.

- Owners, nothing shared: **the daemon = the pods' and the jobs' lifetimes** (when a pod exists, which pod a job runs
  on, how long it may run -- TIMEOUT_MARGIN x expected_s x the pod's measured slowness x the jobs at once --, its
  kill: `pkill -9 -s` of the job's session, how fast a pod is, and whether a job is requeued); **podd-run.sh = one
  command in its own session** (no timeout, no kill); **pod.sh = the RunPod API** plus the watchdog for a dead podd;
  **the client = what to run and what it needs** (command, inputs, outputs, requires, cpu / gpu_memory_gb / ram_gb,
  expected_s on a fast host, and whether it reports `progress` and can resume) and what it does with the result.
  podd never knows a client, a backend or an engine: the resources and `requires` say it all; `progress` is two
  numbers, never a unit podd reads.
- Standard library only, everywhere in `src/podd/` (a hard rule; `tests/test_podd.py` parses every module's imports).
  A client imports `podd.job_contract` and nothing else of podd.
- Requirements are matched, never assumed: `requires.image` is launched, `gpu` / `gpu_memory_gb_min` / `cpu_min` are
  measured on the pod, every other key is matched against the facts the IMAGE declares (/etc/podd-facts.json). A pod
  without declared facts is refused; a pod opened for a set that turns out not to satisfy it is deleted and the set's
  jobs FAIL (no create loop). DONE says what was used; the client records it.
- A client never talks to RunPod: no `pod.sh`, no ssh, no rsync. What it learns of a pod is DONE's `used`.
- The daemon is the only caller of `pod.sh create` / `delete` and the only process that ssh-es to a pod.
- The queue is files, and every state change is a rename or an atomic replace (`job_contract.write_json_atomically`).
  Who writes what: the client JOB (`job_contract.write_job`, which checks the contract), the daemon RUNNING and DONE,
  either one FAILED (`job_contract.mark_stage_failed`, the one writer); `job_contract.stage_state` the one reader of a
  stage's state.
- The account's choices live in the config, never in code: price cap, cards, CUDA versions, CPU flavors, caps, queues
  (`job_contract.read_config` requires each). Code defaults exist only for measured RunPod behaviour (boot_seconds,
  max_wait_for_stock_minutes) and podd's own timing constants (daemon.py's top).
- Resource packing, and a pod only when it would help: a job declares the cpu, card memory and RAM it uses; a pod
  runs jobs while they fit what podd measured it has (best fit: the fullest pod first, so the others empty and go).
  Another pod is opened only while a requirement set's backlog would take longer than boot_seconds to drain on the
  pods up or booting, up to max_pods of the set's kind (GPU and CPU pods capped apart), and only on a card under
  max_price_per_hour (2026-10-14: a second pod, an L40S, came up after six jobs were done: $0.032 a job instead of
  $0.011). create is offered EVERY accepted card under the cap (listed-in-stock first, cheapest first); RunPod rents
  the first it has. `cpu: "all"` runs ALONE on its pod (2026-10-13: three CPU-bound jobs at once on a 7.65-CPU quota
  took 152-223 s each against 75 s alone).
- A job that needs no card says so (`requires.gpu: "none"`) and runs on a CPU pod, sized from the set's jobs (cpu,
  ram_gb) and the flavors' prices (cpu_pod_plan); a GPU pod never serves a CPU set nor a CPU pod a card's set.
  Everything else (facts, packing, idle, stock) is the GPU pods'.
- Short of stock is waited out, not failed: pod.sh create exits 75 when RunPod has no instance of any card offered
  (2026-10-14: six jobs FAILED on one "no instances currently available"); the jobs stay JOB, podd says it loudly (and
  in the status file) and retries one pod after 60 s doubling to 10 min, avoiding no datacenter, and only a set with
  NO pod for max_wait_for_stock_minutes FAILS, with every miss: the clock runs only while no pod serves the set
  (2026-10-22: 20 jobs FAILED while 3 pods of their set ran).
- Nothing fails quietly: every failure is a FAILED file (and the journal for podd's); a stage that failed stays until
  a person reads it and deletes it by name. A failure on one pod fails only its jobs; a pod whose ssh or rsync failed
  is BROKEN: it takes no more jobs and is deleted once empty. A job's own failure (non-zero exit, a missing output) is
  the job's, not the pod's.
- Work is never thrown away (2026-10-16: four jobs killed at 103-109 of 115 units, ~$5.44): every interrupted job
  (podd stopped it, its deadline, its pod broke, podd stopping) pulls home every output that exists first. Only a job
  that declares itself `resumable` is requeued -- by the daemon, the one writer of that JOB, with an `attempts` record
  -- and its next attempt gets those outputs back and does the rest; at most MAX_ATTEMPTS.
- Slow hosts are caught, not waited out: from a job's `progress` podd measures its PACE (seconds per unit over the
  job's own expected seconds per unit: one command runs jobs of many sizes) against the MEDIAN pace of at least
  MIN_REFERENCE_PODS other pods running the same command with as many jobs at once (packing is never slowness; never
  the fastest pod, never expected_s alone: 2026-10-21, 15 datacenters banned in a day, US-GA-2 the one with CPU stock
  among them); at SLOW_FACTOR (1.5; measured 2026-10-16 and 2026-10-21, daemon.py) the pod is SLOW, serves nothing (so
  the opening rule replaces it), moves the resumable jobs a move saves more than a boot, goes once empty, and its
  datacenter is avoided by creates of its kind (gpu / cpu) for SLOW_DATACENTER_HOURS
  (~/.local/state/podd/slow-datacenters.json). A set short of stock keeps its slow pod and avoids nothing: a slow pod
  is better than none.
- A failed pod delete is said loudly and retried every minute, never podd's end (2026-10-15: one RunPod HTTP 500 on
  a DELETE stopped podd, failing two running jobs on other pods and leaving two pods billing).
- Pods never outlive their work: idle -> deleted, podd stopped -> deleted (a booting create is stopped first), podd
  killed -> pod.sh's watchdog (its lease stops being renewed), podd started -> every pod pod.sh still records deleted.

KNOWN SHAPE DEBT: daemon.py is one ~1450-line module (pool, pod, job, slow-host judging). Splitting it is a level-3
refactor of its own (the tests patch module attributes such as `daemon.subprocess` and `daemon.STATUS_FILE`); not done
in the 2026-10-19 extraction.
