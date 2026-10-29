"""PODD -- THE POD JOB RUNNER (GPU pods, and CPU pods for jobs that require no card): the one process that owns the
RunPod pods, and runs the jobs that any client drops in the queue directories its config lists. It knows nothing
about any client: a job is a directory with a JOB file (podd.job_contract is the contract), and podd matches it to a
pod by what it requires. architecture/README.md is the pattern.

    systemctl --user enable --now podd           (systemd/podd.service: python3 -m podd run)
    journalctl --user -u podd -f                 its log: every pod, every job, every failure
    python3 -m podd status                       the pods and every queue: each stage, its state and age

THE CONFIG: ~/.config/podd/config.json ($PODD_CONFIG overrides; job_contract.read_config checks every key):
queue_directories (absolute paths; each subdirectory is a stage), max_pods ({"gpu": n, "cpu": n}: GPU pods and CPU
pods are capped apart, 2026-10-15), idle_minutes, max_price_per_hour, cards (the cards a pod may
be, each {name, memory_gb}), min_cuda_version (the lowest driver CUDA version a GPU pod may have), cpu_flavors, and,
with defaults,
boot_seconds (150) and max_wait_for_a_pod_minutes (60).

A POD'S FACTS, read ONCE when it boots: /etc/podd-facts.json (what its image declares: README.md "Images";
a pod without it is refused), the image tag podd launched it from, nvidia-smi's card name and memory (decimal GB; a
pod without nvidia-smi has gpu null, gpu_memory_gb 0 and cuda_version null: a CPU pod) and the driver's CUDA version
(cuda_version, nvidia-smi's "CUDA Version: X.Y"),
the cgroup CPU quota (else its cpuset's CPU count), the RAM (cgroup memory.max, else MemTotal, at most RunPod's
memoryInGb for the pod: a RunPod
container has no memory limit and sees the host's), and pod.sh's price, datacenter and container disk (RunPod's
containerDiskInGb for the pod: disk_gb). A job
runs on a pod whose facts satisfy its `requires`. The pod's CAPACITY is its cpu, gpu_memory_gb and ram_gb facts.

THE POOL: RESOURCE PACKING. A job declares the
cpu (cores, or "all": the pod's whole quota), gpu_memory_gb and ram_gb it uses; a pod runs jobs while their sum fits
its capacity. A waiting job goes to the FULLEST ready pod that satisfies it and has room for it (best fit: the least
CPU left after it), so the others empty and go; a job that does not fit even an EMPTY ready pod of its set FAILS with
the numbers (it would wait forever). The jobs behind a job that fits nowhere still go.
WHEN TO OPEN A POD: per requirement set with jobs waiting (oldest set first), the DRAIN TIME of its backlog on the
pods up or booting that serve it: for each of the set's running (remaining seconds) and waiting jobs, its expected
seconds over how many such jobs those pods run at once, summed. podd opens pods one at a time while the drain exceeds
boot_seconds (a pod opened later would come up after the work is done) and there are fewer pods of its kind (GPU or
CPU, booting or not) than max_pods of that kind. A job's
expected seconds: its `expected_s` (required). A job on a slow or broken pod stays there and is no backlog. A booting
pod's capacity is the last one podd measured (else
FIRST_GUESS_CAPACITY; its card memory the smallest card the set accepts), and it counts for at least one job of a set
it serves (opened for it, or a stricter one with an image it accepts: booting_pod_serves).
A pod is opened FOR a set: the set's first acceptable image, minVCPUPerGPU = ceil(cpu_min), a container disk of
container_disk_gb (the default, or the set's disk_gb_min when larger), and EVERY card of the
config that the set accepts (name, gpu_memory_gb_min) AND that costs at most max_price_per_hour (pod.sh avail, asked
at most every OFFERS_SECONDS), the ones listed in stock first, each group cheapest first: RunPod rents the first it
has (gpuTypePriority custom). Stock orders the list and never gates it: the listing races the create.
CPU PODS. A set with requires.gpu "none" runs on a pod with no card: RunPod's
computeType CPU, one of the config's cpu_flavors (cpu3c, cpu5c, ...: price per vCPU-hour and RAM per vCPU from pod.sh
avail-cpu), its vcpuCount a power of two. cpu_pod_plan sizes it from the set's waiting jobs: as many of them as one
pod of the flavor holds (by cpu and ram_gb, and enough vCPUs for its container disk at CPU_DISK_GB_PER_VCPU; at most
its vcpu_max, under max_price_per_hour), the flavor whose price
per job is the lowest; create is offered every flavor that holds the same jobs at that vcpuCount, listed-in-stock
first, cheapest first. A booting CPU pod's capacity is that plan. Facts, packing, idling and stock are the GPU pods'.
NO POD YET IS NOT A FAILURE. pod.sh create exits POD_SH_NO_STOCK_EXIT when RunPod has no instance of any card
offered; podd says so loudly once per attempt, the set's jobs stay JOB, and its next create waits
no_pod_backoff_seconds (60 s doubling to 10 min) and is ONE pod (a probe, not a burst), avoiding no slow datacenter,
until a create succeeds. No card under the cap counts the same way, and so does a pod on an old driver, and so does a
pod that did not boot (2026-10-29: pod.sh create exited 1, "no public ssh port after 15 min"; a pause of its own
FAILED the set's jobs, and a job queued during it, with no create of its own): ONE wait, NoPodWait. A set waiting
for a pod that has had NO pod (none ready or
slow serving it, no job of it running) for more than max_wait_for_a_pod_minutes FAILS its waiting jobs with every miss
(time, cards offered, RunPod's answer); while a pod serves it, they wait. The waits are in the status file. A pod whose facts do not
satisfy the set it was opened for is deleted and the set's waiting jobs FAIL with the mismatch, as do later jobs of
that set until podd restarts (a create loop would bill forever). A ready pod with no job for idle_minutes is deleted
(a pod no job matches is simply idle). Pods are named podd-g1 .. podd-g<max_pods.gpu> and podd-c1 ..
podd-c<max_pods.cpu> (pod.sh's name: its id file, lease,
alias runpod-<name>).

EACH POD'S LIFE. Booting: `pod.sh create` in its own thread (never in a datacenter recorded slow for its kind, unless
its set is waiting for a pod: below; never in one recorded for an old driver), then podd
ships podd-run.sh and reads the facts. OLD DRIVER (2026-10-29): a GPU create asks RunPod for any host (RunPod's
allowedCudaVersions enum stops at 13.0, so a list of versions shut out every newer driver, and the only stock under the
cap was on such hosts); a GPU pod whose measured cuda_version is below min_cuda_version (compared as integer tuples)
is deleted at once, its datacenter avoided by GPU creates for OLD_DRIVER_DATACENTER_HOURS even while its set waits
for a pod (an old-driver pod runs nothing, unlike a slow one), and the miss counts as a no-pod miss: the jobs stay JOB,
the next create backs off, and a set with no pod for max_wait_for_a_pod_minutes FAILS (Pool.reject_old_driver).
An image that refuses the old driver at container start (NVIDIA_REQUIRE_CUDA: "OCI runtime create failed") never
opens ssh: pod.sh create gives up after 15 min and exits 1, which is a boot failure like any other (a miss). Ready: takes jobs.
SLOW or BROKEN: takes no more, deleted once empty. Deleted
after idle_minutes with no job, when a job on it failed for a reason of the pod's own (ssh, rsync: it is BROKEN), once
SLOW and empty, or when podd stops (a booting create is stopped, its pod deleted). pod.sh's watchdog
is the backstop for a podd that died (MAX_POD_HOURS after its lease was last touched; podd touches the lease of every
pod with work on every pass). At start-up podd deletes every pod pod.sh still records, and FAILS every RUNNING (a
podd that died hard left them). Stopping, podd first stops every running job and lets it pull home and requeue or
fail (SHUTDOWN_PULL_SECONDS), then deletes its pods.

ONE JOB: ship its inputs (rsync; a resumable job's `resume_from` files too, never the rest of what came home), write
its command to <job>/.podd/command, start podd-run.sh under `nohup setsid` (the job's own session), poll
<job>/.podd/exit (and its `progress` file, when it has one), pull its outputs (each laid over the stage's copy: no file
of the stage is deleted) and command.log, then DONE (with `used`, the pod's facts, and
`timings`) or FAILED. A non-zero exit or a missing output fails the job, not the pod.
THE DEADLINE (job_deadline_s; 2026-10-16: a fixed 6 h timeout killed four jobs on slow hosts at 103-109 of 115
units): TIMEOUT_MARGIN x expected_s x the pod's measured slowness (at least 1) x the job's load (the most jobs its
pod ran at once during it: 2026-10-15, 19-20 jobs on one A40 took ~420 s each against 66-70 s at <= 3), re-read every
poll. Past it podd
kills exactly the job's session (`pkill -9 -s`), never a neighbour's.
KEEP PARTIAL WORK. A job interrupted -- podd stopped it (a slow pod,
podd stopping), its deadline passed, or its pod failed it -- first pulls home every output that exists and
command.log. Then a RESUMABLE job (job_contract `resumable`: run again, told what is done by its `resume_from` files,
it does only the rest; what came home stays home, 2026-10-21: it went back up at ~1 MB/s, 0.2-2.2 GB a job) is
REQUEUED (JOB again, + its record in `attempts`, its command.log kept as <stage>/.podd/attempt-<n>.command.log) when
should_requeue says so (a deadline only after progress; at most MAX_ATTEMPTS), and any other FAILS with what came
home.
SLOW HOSTS (judge_speeds, every pass). A job with `progress` (`<done> <total>`) is measured from its first finished
unit on (seconds_per_unit); its PACE is that over its own expected seconds per unit (pace_of), judged against the
MEDIAN pace of at least MIN_REFERENCE_PODS other pods running the same command AT THE SAME LOAD (as many jobs on its
pod; reference_pace), else not at all: a pod running N jobs at once is not slow for it, and expected_s alone only
scales a lone job's deadline. At SLOW_FACTOR or more its pod is SLOW (mark_slow) -- unless its set is waiting for a pod
(nothing would replace it: kept, said once): it takes no more jobs and serves no set (the opening rule replaces it),
its resumable jobs are stopped and requeued when the move saves more than a boot (worth_moving; each at most
MAX_SLOW_STOPS times), its others run on under the scaled deadline, and its datacenter goes in AVOIDED_DATACENTERS_FILE
under its kind: that kind's creates avoid it for SLOW_DATACENTER_HOURS. A pod's slowness (the largest its jobs
measured) scales its jobs' deadlines. All said loudly.

NOTHING FAILS QUIETLY. A pod that did not boot (pod.sh create failed, or podd could not ship podd-run.sh or read its
facts) is said, deleted, and is a miss of its set like a stock miss: its jobs stay JOB until max_wait_for_a_pod_minutes
with no pod, then FAIL with every miss. A job no pod can ever run (no pod size holds it, or a pod opened for its set
did not satisfy it) FAILS at once. A broken JOB (job_contract.job_problems) FAILS with
every problem. Only a resumable job is ever requeued, and only by podd (never a failed one).

THE STATUS FILE: ~/.local/state/podd/PODS.json, rewritten whenever the pool changes (no secrets). `python3 -m podd status` prints
it; nothing reads it to decide anything.

Python 3 standard library only (/usr/bin/python3); imports nothing but podd.job_contract (the contract).
"""
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from podd import job_contract  # the job contract: names, the JOB file, the config
from podd.job_contract import DONE, JOB, RUNNING

PACKAGE_DIRECTORY = Path(__file__).resolve().parent    # pod.sh, podd-run.sh and secrets.sh live beside this file

POLL_SECONDS = 2                     # the queue scan, and the exit poll of a running job
RESOURCES = ("cpu", "gpu_memory_gb", "ram_gb")       # what a job uses and a pod has (its facts of these names)
# THE DEADLINE (2026-10-16: one fixed 6 h timeout killed four jobs on slow US-MD-1 hosts at 103-109 of 115 units).
# A job may run TIMEOUT_MARGIN x its expected_s x its pod's measured slowness (at least 1): 4 covers the largest
# overrun measured so far (223 s against 85 s expected, three CPU-bound jobs sharing a pod) and a stall still ends
# (2026-10-05: 7.5 h of a 5090)
TIMEOUT_MARGIN = 4
# SLOW HOSTS. A job's PACE: its seconds per unit of its `progress` (measured from its first finished unit on, start-up
# excluded, over at least MIN_UNITS_TO_JUDGE units and MIN_SECONDS_TO_JUDGE seconds) over its own expected seconds per
# unit (expected_s / total): jobs of one command differ in size (2026-10-21: one command ran jobs of expected_s 1273,
# 1633 and 3218 s over 124 units, and per-unit rates compared across them marked five pods slow at once). A pace at
# SLOW_FACTOR x the reference or more marks its pod slow; the reference is the MEDIAN pace of at least
# MIN_REFERENCE_PODS other pods at the same load (never the fastest one: 2026-10-21, whole jobs of one shape ran at pace
# 0.24 on US-IL-1 and 0.35 on US-GA-2, 1.46x, both normal; never expected_s alone: that day a client's expected_s was 3x
# off).
# Measured: 2026-10-15/16 (one job shape, s per unit) fast hosts 6.7-8.9, slow (US-MD-1) 14.6-20: the fast maximum is
# 1.14x the fast median 7.8, the slow minimum 1.87x; 2026-10-21 (pace) normal 0.24-0.52 around a median ~0.40 (<= 1.3x),
# slow US-MO-1 / US-CA-2 0.69-0.79 (>= 1.7x). 1.5 sits between.
SLOW_FACTOR = 1.5
MIN_UNITS_TO_JUDGE = 3
MIN_SECONDS_TO_JUDGE = 60
MIN_REFERENCE_PODS = 2               # other pods with a measured pace at the same load before a pod is judged
REFERENCE_MAX_AGE_SECONDS = 3600     # a pace measured longer ago is no reference (a client may revise its expected_s)
SLOW_DATACENTER_HOURS = 12           # a datacenter a slow pod of a kind was in: podd's creates of that kind avoid it
OLD_DRIVER_DATACENTER_HOURS = 12     # a datacenter a GPU pod below min_cuda_version was in: GPU creates avoid it (a
                                     # host's driver, not the datacenter's: a guess that its neighbours are alike)
AVOIDED_BECAUSE = ("slow", "old driver")   # why a datacenter is avoided (AVOIDED_DATACENTERS_FILE's `because`)
MAX_ATTEMPTS = 4                     # a resumable job's attempts (the first included); the last one FAILS, never loops
MAX_SLOW_STOPS = 2                   # a job is moved off a slow pod at most this often (then it runs on where it is:
                                     # its expected_s may be optimistic, and every pod would look slow)
SHUTDOWN_PULL_SECONDS = 840          # podd stopping: its running jobs are stopped and pulled home for this long at most
                                     # (podd.service TimeoutStopSec 900)
PROGRESS_MARK = "--podd-progress--"  # separates the exit file from the progress file in one poll's output
OFFERS_SECONDS = 60                  # pod.sh avail (prices, stock) at most this often; the price-cap wait is said
                                     # this often
# A booting pod's capacity before its facts are read, until podd has measured one: an A40 of RunPod's secure cloud
# (cgroup quota 7.65 cores, 2026-10-13; RAM: RunPod's gpuTypes minMemory 50 GB for the A40, 2026-10-14). The card
# memory is never guessed from this: it is the smallest card the set accepts (the config's memory_gb).
FIRST_GUESS_CAPACITY = {"cpu": 7.65, "ram_gb": 50.0}
# THE CONTAINER DISK a pod is created with (pod.sh create / create-cpu say why these): the default of its kind, or the
# set's requires.disk_gb_min when larger (container_disk_gb; 2026-10-29: a training job decompresses a 46.3 GB
# dataset on the pod). A CPU pod's disk is capped at CPU_DISK_GB_PER_VCPU a vCPU (cpu3*: GraphQL
# cpuFlavors.diskLimitPerVcpu 2026-10-15, and references/runpod/docs/flash/configuration/cpu-types.mdx; cpu5c 15
# there: the smaller holds on every flavor), so cpu_pod_plan sizes a CPU pod's vCPUs for its disk too.
GPU_CONTAINER_DISK_GB = 30
CPU_CONTAINER_DISK_GB = 20
CPU_DISK_GB_PER_VCPU = 10
POD_SH_NO_STOCK_EXIT = 75            # pod.sh create's exit when RunPod has no instance of any card offered (EX_TEMPFAIL)
NO_POD_RETRY_FIRST_SECONDS = 60       # after a miss (NoPodWait) the set's next create waits this, doubling per miss in a row,
NO_POD_RETRY_MAX_SECONDS = 600        # at most this; the set's jobs stay JOB until max_wait_for_a_pod_minutes
DELETE_RETRY_SECONDS = 60           # a pod whose delete failed is deleted again this often (Pool.forget)
SSH_FAILURES_ALLOWED = 3             # consecutive failed exit polls that fail the job
SSH_COMMAND_TIMEOUT_S = 600          # one ssh / rsync; a hung connection is a failure, never a hang

STATUS_FILE = Path.home() / ".local" / "state" / "podd" / "PODS.json"
# {kind: {datacenter: {"until_unix", "until", "why", "pod", "because"}}}: the datacenters podd's creates avoid,
# kept across restarts; because "slow" (a slow pod was there: mark_slow) or "old driver" (a host whose driver was
# below min_cuda_version: Pool.reject_old_driver)
AVOIDED_DATACENTERS_FILE = STATUS_FILE.parent / "avoided-datacenters.json"
POD_SCRIPT = PACKAGE_DIRECTORY / "pod.sh"
SECRETS_SCRIPT = PACKAGE_DIRECTORY / "secrets.sh"
PODS_DIRECTORY_NAME = "runpod_pods"                  # pod.sh's PODS_DIRECTORY, in the secrets directory
POD_NAME_PREFIX = "podd-"
POD_JOBS = "/workspace/jobs"
RUNNER_SCRIPT = PACKAGE_DIRECTORY / "podd-run.sh"
RUNNER_ON_THE_POD = "/workspace/podd/podd-run.sh"    # shipped once per pod (Pod.boot)
FACTS_ON_THE_POD = "/etc/podd-facts.json"           # what the image declares (README.md "Images")
# the card, then nvidia-smi's own summary for the driver's CUDA version, or "none" when the pod has no nvidia-smi (a
# CPU pod: RunPod's container runtime injects it only with a card). The summary's "CUDA Version: X.Y" is the CUDA
# driver API version, cuDriverGetVersion()'s (NVIDIA's cuda-python, cuda/pathfinder/_utils/driver_info.py: "the version
# shown as CUDA Version in nvidia-smi ... reported by cuDriverGetVersion()", "not the graphics driver release shown as
# Driver Version"): the newest CUDA runtime the host's driver runs, the number an image's CUDA needs, read without a
# driver-to-CUDA table. --query-gpu has no field for it, so the summary's text is parsed (cuda_version_of).
GPU_ON_THE_POD = ("if command -v nvidia-smi >/dev/null; then nvidia-smi --query-gpu=name,memory.total "
                  "--format=csv,noheader,nounits && nvidia-smi; else echo none; fi")
# the CPU the container may use: cgroup v2's cpu.max ("<quota> <period>", or "max <period>": no quota), its cpuset
# (a RunPod CPU pod has no quota and a cpuset of hyperthreads: "5,101" on a 2-vCPU cpu3g, 2026-10-15, where nproc said
# 8, the image's OMP_NUM_THREADS), and nproc without the OpenMP variables it obeys (the last resort)
CPU_ON_THE_POD = ("cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo none; "
                  "cat /sys/fs/cgroup/cpuset.cpus.effective 2>/dev/null || echo none; "
                  "env -u OMP_NUM_THREADS -u OMP_THREAD_LIMIT nproc")
# the RAM the container may use: cgroup v2's memory.max (bytes, or "max": no limit -> MemTotal of /proc/meminfo, kB)
RAM_ON_THE_POD = "cat /sys/fs/cgroup/memory.max 2>/dev/null || echo none; grep MemTotal /proc/meminfo"
MEASURED_FACTS = ("pod_name", "pod_id", "image", "gpu", "gpu_memory_gb", "cuda_version", "cpu", "ram_gb", "disk_gb",
                  "datacenter", "cost_per_hr", "ready_at")


class PodError(RuntimeError):
    """A pod could not be made, read or used; the message says which and why. On a job: the pod's fault (broken)."""


class JobFailed(RuntimeError):
    """The job's own failure (its command exited non-zero, an output is missing): not the pod's."""


class JobTimedOut(JobFailed):
    """The job ran past its deadline (job_deadline_s)."""


class JobStopped(RuntimeError):
    """podd stopped the job (kind: "slow pod", "podd stopping"): neither the job's fault nor the pod's breakage."""

    def __init__(self, kind, why):
        super().__init__(why)
        self.kind = kind


def say(message):
    """One write per line: the boot and job threads say things at once, and print's two writes interleave."""
    sys.stdout.write(f"podd: {message}\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------------------------------------------------
# matching (pure)
# ---------------------------------------------------------------------------------------------------------------------

def requirement_key(requires):
    """One text per requirement set: the same set from any job is the same key."""
    return json.dumps(requires, sort_keys=True)


def unmet_requirements(requires, facts):
    """[why] for every requirement these pod facts do not meet ([] = the pod satisfies the job)."""
    unmet = []
    if facts.get("image") not in job_contract.acceptable(requires["image"]):
        unmet.append(f"image {facts.get('image')!r} is not one of {job_contract.acceptable(requires['image'])}")
    gpu = requires.get("gpu", "any")
    if gpu == "none":
        if facts.get("gpu") is not None:
            unmet.append(f"gpu {facts.get('gpu')!r} on a pod for a job that requires none (a CPU pod)")
    elif facts.get("gpu") is None:
        unmet.append(f"no card on the pod, and the job requires {gpu!r}")
    elif gpu != "any" and facts.get("gpu") not in gpu:
        unmet.append(f"gpu {facts.get('gpu')!r} is not one of {gpu}")
    if "gpu_memory_gb_min" in requires and not (facts.get("gpu_memory_gb") or 0) >= requires["gpu_memory_gb_min"]:
        unmet.append(f"gpu memory {facts.get('gpu_memory_gb')} GB < {requires['gpu_memory_gb_min']}")
    if "cpu_min" in requires and not (facts.get("cpu") or 0) >= requires["cpu_min"]:
        unmet.append(f"cpu {facts.get('cpu')} < {requires['cpu_min']}")
    if "disk_gb_min" in requires and not (facts.get("disk_gb") or 0) >= requires["disk_gb_min"]:
        unmet.append(f"container disk {facts.get('disk_gb')} GB < {requires['disk_gb_min']}")
    for key, value in requires.items():
        if key in job_contract.POD_MEASURED_REQUIREMENTS:
            continue
        if key not in facts:
            unmet.append(f"the image declares no {key!r} (wanted {job_contract.acceptable(value)})")
        elif facts[key] not in job_contract.acceptable(value):
            unmet.append(f"{key} {facts[key]!r} is not one of {job_contract.acceptable(value)}")
    return unmet


def cards_for(requires, cards):
    """The config's cards this set accepts: by name and by gpu_memory_gb_min (none for a CPU set)."""
    gpu = requires.get("gpu", "any")
    if gpu == "none":
        return []
    return [card["name"] for card in cards
            if (gpu == "any" or card["name"] in gpu)
            and card["memory_gb"] >= requires.get("gpu_memory_gb_min", 0)]


def booting_pod_serves(opened_for, wanted, cards):
    """Before its facts are read, is a pod booting FOR the set `opened_for` sure to satisfy the set `wanted`? Yes when
    the image it launches is acceptable to `wanted` and every other requirement of `wanted` is implied by
    `opened_for`: the cards it may land on are acceptable, its minimums are at least as high, and every image fact
    `wanted` asks is one `opened_for` pinned to acceptable values. (2026-10-14: a job asking only for an image opened a
    pod of its own while a pod of that image, opened for a stricter set, was booting.)"""
    if job_contract.acceptable(opened_for["image"])[0] not in job_contract.acceptable(wanted["image"]):
        return False
    if (opened_for.get("gpu") == "none") != (wanted.get("gpu") == "none"):
        return False                                     # a CPU pod never serves a card's set, nor a GPU pod a CPU set
    gpu = wanted.get("gpu", "any")
    if gpu != "any" and not set(cards_for(opened_for, cards)) <= set(gpu):
        return False
    for minimum in ("gpu_memory_gb_min", "cpu_min", "disk_gb_min"):
        if opened_for.get(minimum, 0) < wanted.get(minimum, 0):
            return False
    return all(key in opened_for and set(map(json.dumps, job_contract.acceptable(opened_for[key])))
               <= set(map(json.dumps, job_contract.acceptable(value)))
               for key, value in wanted.items() if key not in job_contract.POD_MEASURED_REQUIREMENTS)


def capacity_of(facts):
    """What a pod has, from its facts: {cpu, gpu_memory_gb, ram_gb}."""
    return {resource: facts[resource] for resource in RESOURCES}


def demand_of(job, capacity):
    """What a job uses on a pod of this capacity: cpu "all" is the pod's whole quota."""
    return {"cpu": capacity["cpu"] if job["cpu"] == "all" else job["cpu"],
            "gpu_memory_gb": job["gpu_memory_gb"], "ram_gb": job.get("ram_gb", 0)}


def covers(free, demand):
    """free covers demand in every resource (1e-9: three 2.5-core jobs on a 7.5 quota fit)."""
    return all(free[resource] >= demand[resource] - 1e-9 for resource in RESOURCES)


def less(free, demand):
    return {resource: free[resource] - demand[resource] for resource in RESOURCES}


def how_many_fit(job, capacity):
    """How many jobs like this one an empty pod of this capacity runs at once."""
    demand = demand_of(job, capacity)
    if not covers(capacity, demand):
        return 0
    return min(math.floor(capacity[resource] / demand[resource] + 1e-9) for resource in RESOURCES
               if demand[resource] > 0)


def place(waiting, pods):
    """THE PLACEMENT (pure). waiting: [(stage, job)] oldest first; pods: {name: (facts, free resources)} of the ready
    pods -> [(stage, pod name)]. A job goes to the FULLEST ready pod that satisfies it and whose free resources cover
    it (best fit: the least CPU left after it, then the name); a job that fits nowhere waits, and the jobs behind it
    still go."""
    free = {name: dict(pod_free) for name, (_, pod_free) in pods.items()}
    placed = []
    for stage, job in waiting:
        fitting = [name for name, (facts, _) in pods.items() if not unmet_requirements(job["requires"], facts)
                   and covers(free[name], demand_of(job, capacity_of(facts)))]
        if not fitting:
            continue
        chosen = min(fitting, key=lambda name: (free[name]["cpu"] - demand_of(job, capacity_of(pods[name][0]))["cpu"],
                                                name))
        free[chosen] = less(free[chosen], demand_of(job, capacity_of(pods[chosen][0])))
        placed.append((stage, chosen))
    return placed


def job_deadline_s(job, slowness, load=1):
    """THE DEADLINE (pure): TIMEOUT_MARGIN x the job's expected_s (its time ALONE on a fast pod) x its pod's measured
    slowness (at least 1) x its load: the most jobs its pod ran at once during it. Jobs packed on one pod share what
    their declared resources do not say (2026-10-15: 19-20 jobs on one A40's card took ~420 s
    each, 66-70 s at <= 3 at once; the pod's throughput was the same)."""
    return TIMEOUT_MARGIN * job["expected_s"] * max(1.0, slowness) * max(1, load)


def parse_progress(text):
    """A progress file's `<done> <total>` -> (done, total), or None when it is not that (yet)."""
    parts = text.split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts) or int(parts[1]) == 0:
        return None
    return int(parts[0]), int(parts[1])


def seconds_per_unit(observations):
    """A job's rate (pure): observations [(time, done, load)], one per change of its progress seen in THIS attempt,
    from its first finished unit on (so start-up never counts) -> seconds per unit, or None until it spans
    MIN_UNITS_TO_JUDGE units and MIN_SECONDS_TO_JUDGE seconds."""
    if len(observations) < 2:
        return None
    (first_s, first_done), (last_s, last_done) = observations[0][:2], observations[-1][:2]
    if last_done - first_done < MIN_UNITS_TO_JUDGE or last_s - first_s < MIN_SECONDS_TO_JUDGE:
        return None
    return (last_s - first_s) / (last_done - first_done)


def pace_of(seconds_per_unit, job, total):
    """A job's pace (pure): its measured seconds per unit over its own expected seconds per unit (expected_s / total).
    1 = as expected_s says; comparable across the jobs of one command whatever their size."""
    return seconds_per_unit / (job["expected_s"] / total)


def reference_pace(load, others, now):
    """What a pod of normal speed does AT THE SAME LOAD (pure): others {attempt key: (pace, load, pod id, measured
    at)} of other pods' attempts of the same command -> (the MEDIAN over pods of each pod's median pace, how many
    pods) when at least MIN_REFERENCE_PODS pods measured one at this load within REFERENCE_MAX_AGE_SECONDS, else None:
    no fair comparison, no judgement (a pod running N jobs at once is not slow for it)."""
    by_pod = {}
    for pace, other_load, pod_id, measured_s in others.values():
        if other_load == load and now - measured_s <= REFERENCE_MAX_AGE_SECONDS:
            by_pod.setdefault(pod_id, []).append(pace)
    if len(by_pod) < MIN_REFERENCE_PODS:
        return None
    return median([median(paces) for paces in by_pod.values()]), len(by_pod)


def median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def worth_moving(seconds_left_here, slowness, boot_seconds):
    """Is moving a job off a slow pod worth a new pod's boot (pure)? What it saves -- its seconds left on this pod x
    (1 - 1/slowness) -- must exceed boot_seconds: a job near its end runs on where it is."""
    return seconds_left_here * (1 - 1 / slowness) > boot_seconds


def seconds_left_here(running, slowness, now):
    """A running job's seconds left on its pod: its units left x its measured rate, else (not measured yet) its
    expected_s x the pod's slowness less what it has run."""
    rate = seconds_per_unit(running.observations)
    if rate is not None and running.total:
        return (running.total - running.done) * rate
    return max(running.job["expected_s"] * slowness - (now - running.started_s), 0.0)


def should_requeue(job, stopped_for, progressed):
    """A job stopped part-way goes back to JOB (pure) when it is resumable, has attempts left (MAX_ATTEMPTS), and the
    stop was podd's or the pod's ("slow pod", "podd stopping", "pod broke") or a deadline it made progress before
    (one that made none would only loop)."""
    attempts = len(job.get("attempts") or []) + 1
    return bool(job.get("resumable")) and attempts < MAX_ATTEMPTS and (stopped_for != "deadline" or progressed)


def drain_seconds(backlog, pods):
    """THE DRAIN TIME (pure). backlog: [(job, seconds left)]; pods: [(capacity, booting)] of the pods up or booting
    that serve it -> seconds: each job's seconds over how many such jobs those pods run at once, summed (a booting
    pod counts for at least one). No pod that can run a job: infinite."""
    total = 0.0
    for job, seconds in backlog:
        width = sum(max(how_many_fit(job, capacity), 1 if booting else 0) for capacity, booting in pods)
        if width == 0:
            return math.inf
        total += seconds / width
    return total


def all_at_once(backlog, pods):
    """Could the pods up or booting run every job of the backlog at the same time? (every job's width, how many such
    jobs they run at once, is at least the backlog's length): another pod would only idle."""
    return all(sum(max(how_many_fit(job, capacity), 1 if booting else 0) for capacity, booting in pods)
               >= len(backlog) for job, _ in backlog)


def pods_to_open(backlog, pods, new_pod_capacity, room, boot_seconds):
    """THE OPENING RULE (pure): how many pods to open for one requirement set: one at a time, while its drain time
    on the pods up or booting (plus the ones opened here, each of new_pod_capacity, booting) exceeds boot_seconds and
    they could not yet run the whole backlog at once (2026-10-15: one 400 s job opened three pods, the drain time
    spreading one job's seconds over pods it cannot use), at most room."""
    opened = 0
    while opened < room:
        serving = pods + [(new_pod_capacity, True)] * opened
        if drain_seconds(backlog, serving) <= boot_seconds or (serving and all_at_once(backlog, serving)):
            break
        opened += 1
    return opened


def affordable_cards(cards, offers, max_price_per_hour):
    """THE PRICE CAP (pure). cards: the names a set accepts; offers: pod.sh avail's JSON ({card: {price_per_hour,
    stock: {datacenter: status}}} or null) -> EVERY card at or under the cap, the ones some datacenter lists in stock
    first, each group cheapest first: the order create asks RunPod in (gpuTypePriority custom). Stock orders, it never
    gates: the listing races RunPod's create (2026-10-14 10:01: the 3090 was the one card listed under the cap, create
    answered "no instances currently available"; gated on it, create was offered that one card). A card RunPod does not
    price is never asked for."""
    priced = [card for card in cards if (offers.get(card) or {}).get("price_per_hour") is not None
              and offers[card]["price_per_hour"] <= max_price_per_hour + 1e-9]
    return sorted(priced, key=lambda card: (not offers[card]["stock"], offers[card]["price_per_hour"]))


def is_cpu_set(requires):
    """A set that requires no card: it runs on a CPU pod."""
    return requires.get("gpu") == "none"


def pod_kind(requires):
    """"cpu" for a set requiring no card, else "gpu": the max_pods cap it counts against."""
    return "cpu" if is_cpu_set(requires) else "gpu"


def container_disk_gb(requires):
    """The container disk (whole GB: RunPod's containerDiskInGb is an integer) a pod opened for this set is created
    with: its kind's default, or the set's disk_gb_min when larger."""
    default = CPU_CONTAINER_DISK_GB if is_cpu_set(requires) else GPU_CONTAINER_DISK_GB
    return max(default, math.ceil(requires.get("disk_gb_min", 0)))


def cpu_pod_plan(jobs, requires, flavors, offers, max_price_per_hour, vcpu_ceiling=None):
    """THE CPU POD'S SIZE (pure). jobs: the set's waiting jobs; flavors: the config's cpu_flavors; offers: pod.sh
    avail-cpu's JSON ({flavor: {price_per_vcpu_hour, ram_gb_per_vcpu, vcpu_min, vcpu_max, stock} or null}) ->
    ({vcpu, flavors, jobs_per_pod, capacity, price_per_hour}, None) or (None, why). Per flavor: k of the jobs (the
    largest cpu and ram_gb of them each; a cpu "all" job alone, sized by cpu_min) fit vcpu = the power of two at least
    max(k x cpu, k x ram_gb / ram_gb_per_vcpu, cpu_min, container_disk_gb / CPU_DISK_GB_PER_VCPU, vcpu_min); k as many
    as are waiting, fewer while vcpu exceeds
    vcpu_max or the price max_price_per_hour. The plan is the flavor with the lowest price per job (then more jobs a
    pod, then in stock, then the config's order); create is offered every flavor that holds those k jobs at that vcpu
    under the cap, listed-in-stock first, cheapest first (RunPod rents the first it has: cpuFlavorPriority custom).
    vcpu_ceiling: after RunPod refused a size for stock, pods of at most this many vCPUs (never below one job's
    size): the catalog's availability is per flavor, not per size (2026-10-15: every 16-vCPU flavor out, 8 in)."""
    alone = any(job["cpu"] == "all" for job in jobs)
    job_cpu = max([job["cpu"] for job in jobs if job["cpu"] != "all"] or [0])
    job_ram = max(job.get("ram_gb", 0) for job in jobs)
    cpu_min = requires.get("cpu_min", 0)
    disk_gb = container_disk_gb(requires)

    def largest(offer):
        ceiling = offer["vcpu_max"] if vcpu_ceiling is None else max(vcpu_ceiling, size(offer, 1))
        return min(offer["vcpu_max"], ceiling)

    def size(offer, count):
        need = max(count * job_cpu, count * job_ram / offer["ram_gb_per_vcpu"], cpu_min, disk_gb / CPU_DISK_GB_PER_VCPU,
                   offer["vcpu_min"], 1)
        vcpu = 1
        while vcpu < need - 1e-9:
            vcpu *= 2
        return vcpu

    candidates, misses = [], []
    for order, flavor in enumerate(flavors):
        offer = offers.get(flavor)
        if not offer:
            misses.append(f"{flavor}: RunPod lists no price for it")
            continue
        count = 1 if alone else len(jobs)
        while count >= 1 and (size(offer, count) > largest(offer)
                              or size(offer, count) * offer["price_per_vcpu_hour"] > max_price_per_hour + 1e-9):
            count -= 1
        if count < 1:
            misses.append(f"{flavor}: one job needs {size(offer, 1)} vCPU, "
                          f"${size(offer, 1) * offer['price_per_vcpu_hour']:.3f}/h (at most {offer['vcpu_max']} "
                          f"vCPU, ${max_price_per_hour}/h)")
            continue
        vcpu = size(offer, count)
        price = vcpu * offer["price_per_vcpu_hour"]
        candidates.append(((price / count, -count, not offer["stock"], order), flavor, vcpu, count))
    if not candidates:
        return None, ("PRICE CAP / SIZE: no CPU flavor holds a job of " + json.dumps(
            {"cpu": "all" if alone else job_cpu, "ram_gb": job_ram, "cpu_min": cpu_min, "disk_gb": disk_gb})
            + "; " + "; ".join(misses))
    _, best, vcpu, count = min(candidates)
    offered = [flavor for flavor in flavors if offers.get(flavor) and size(offers[flavor], count) <= vcpu
               and vcpu <= largest(offers[flavor])
               and vcpu * offers[flavor]["price_per_vcpu_hour"] <= max_price_per_hour + 1e-9]
    offered.sort(key=lambda flavor: (flavor != best, not offers[flavor]["stock"],
                                     offers[flavor]["price_per_vcpu_hour"]))
    ram_gb = min(vcpu * offers[flavor]["ram_gb_per_vcpu"] for flavor in offered)
    return {"vcpu": vcpu, "flavors": offered, "jobs_per_pod": count,
            "capacity": {"cpu": float(vcpu), "gpu_memory_gb": 0.0, "ram_gb": float(ram_gb)},
            "price_per_hour": round(vcpu * offers[best]["price_per_vcpu_hour"], 4)}, None


def no_pod_backoff_seconds(misses):
    """After the misses-th attempt in a row that got the set no pod it can run on (NoPodWait): NO_POD_RETRY_FIRST_SECONDS,
    doubling, at most NO_POD_RETRY_MAX_SECONDS."""
    return min(NO_POD_RETRY_FIRST_SECONDS * 2 ** (misses - 1), NO_POD_RETRY_MAX_SECONDS)


# ---------------------------------------------------------------------------------------------------------------------
# reading a pod (pure parts)
# ---------------------------------------------------------------------------------------------------------------------

def facts_of_pod(image_facts_text, gpu_text, cpu_text, ram_text, image, session, name, pod_id, now_text):
    """The pod's facts from what it printed. Image facts that would shadow what podd measures are refused."""
    try:
        image_facts = json.loads(image_facts_text)
    except json.JSONDecodeError as error:
        raise PodError(f"(╯°□°)╯ {FACTS_ON_THE_POD} on {name} is not JSON ({error}): {image_facts_text[:200]!r}")
    if not isinstance(image_facts, dict):
        raise PodError(f"(╯°□°)╯ {FACTS_ON_THE_POD} on {name} is not an object: {image_facts!r}")
    shadowing = sorted(set(image_facts) & set(MEASURED_FACTS))
    if shadowing:
        raise PodError(f"(╯°□°)╯ {FACTS_ON_THE_POD} on {name} declares {shadowing}, which podd measures itself")
    if gpu_text.strip() == "none":                # no nvidia-smi: a CPU pod
        gpu_name, gpu_memory_mib, cuda_version = None, "0", None
    else:
        gpu_name, gpu_memory_mib = [part.strip() for part in gpu_text.strip().splitlines()[0].rsplit(",", 1)]
        cuda_version = cuda_version_of(gpu_text, name)
    cpu = cpu_of(cpu_text)
    ram_lines = ram_text.strip().splitlines()
    unlimited = ram_lines[0].strip() in ("max", "none")
    ram_bytes = int(ram_lines[-1].split()[1]) * 1024 if unlimited else int(ram_lines[0])
    ram_gb = round(ram_bytes / 1e9, 2)
    if session.get("memory_gb"):                  # RunPod's Pod.memoryInGb: a RunPod container has no memory limit
        ram_gb = min(ram_gb, session["memory_gb"])  # and sees the host's RAM (540 GB on an A40 host, 2026-10-14)
    return {**image_facts, "pod_name": name, "pod_id": pod_id, "image": image, "gpu": gpu_name,
            "gpu_memory_gb": round(int(gpu_memory_mib) * 1048576 / 1e9, 2), "cuda_version": cuda_version,
            "cpu": cpu, "ram_gb": ram_gb,
            "disk_gb": session.get("container_disk_gb"), "datacenter": session.get("datacenter"),
            "cost_per_hr": session.get("cost_per_hr"), "ready_at": now_text}


def cuda_version_of(nvidia_smi_text, name):
    """nvidia-smi's "CUDA Version: X.Y" (its summary's header, or `-q`'s "CUDA Version : X.Y") -> "X.Y"; none on a
    pod with a card is podd's failure to measure, said loudly (PodError)."""
    found = re.search(r"CUDA Version\s*:\s*(\d+\.\d+)", nvidia_smi_text)
    if found is None:
        raise PodError(f"(╯°□°)╯ nvidia-smi on {name} printed no \"CUDA Version: X.Y\": podd cannot tell whether the "
                       f"host's driver runs the image (min_cuda_version). It printed: {nvidia_smi_text[:600]!r}")
    return found.group(1)


def cuda_version_tuple(version_text):
    """"13.10" -> (13, 10): versions compare as integers, so 13.10 > 13.2 (as text it would not)."""
    return tuple(int(part) for part in version_text.split("."))


def old_driver(facts, min_cuda_version):
    """Why this GPU pod's driver is too old for the account's images (None when it is not, or the pod has no card)."""
    if facts.get("gpu") is None:
        return None
    if cuda_version_tuple(facts["cuda_version"]) >= cuda_version_tuple(min_cuda_version):
        return None
    return (f"OLD DRIVER: CUDA {facts['cuda_version']} < min_cuda_version {min_cuda_version} on {facts.get('gpu')} in "
            f"{facts.get('datacenter')}")


def cpu_of(cpu_text):
    """CPU_ON_THE_POD's three lines -> the cores the pod may use: the cgroup quota, else the cpuset's CPU count (the
    smaller when both), else nproc."""
    quota_line, cpuset_line, nproc_line = [line.strip() for line in cpu_text.strip().splitlines()]
    found = []
    quota = quota_line.split()
    if quota[0] not in ("max", "none"):
        found.append(round(int(quota[0]) / int(quota[1]), 2))
    if cpuset_line not in ("none", ""):
        count = 0
        for part in cpuset_line.split(","):
            low, _, high = part.partition("-")
            count += int(high or low) - int(low) + 1
        found.append(float(count))
    return min(found) if found else float(nproc_line)


def wait_for_the_exit(read_exit, deadline_s, poll_s=POLL_SECONDS, sleep=time.sleep, clock=time.monotonic,
                      stop=lambda: None, saw_progress=lambda text, now: None):
    """Poll until the job's .podd/exit exists; returns its JSON. read_exit() -> CompletedProcess of the ssh (stdout
    the file, or empty while it runs; then PROGRESS_MARK and the progress file, when the job has one: saw_progress
    gets it). deadline_s: seconds, or a function of nothing (re-read every poll: the pod's slowness moves it). Ends,
    loudly, at the deadline (JobTimedOut: the caller kills the job), when stop() names a (kind, why) (JobStopped), or
    after SSH_FAILURES_ALLOWED failed reads in a row (PodError)."""
    started, failures_in_a_row = clock(), 0
    deadline = deadline_s if callable(deadline_s) else (lambda: deadline_s)
    while True:
        if clock() - started > deadline():
            raise JobTimedOut(f"timed out: no exit {deadline():.0f} s after it started (its deadline, "
                              f"podd.daemon job_deadline_s)")
        stopping = stop()
        if stopping:
            raise JobStopped(*stopping)
        sleep(poll_s)
        completed = read_exit()
        if completed.returncode != 0:
            failures_in_a_row += 1
            error = (completed.stderr or "").strip() or f"exit {completed.returncode}"
            say(f"(・_・;) reading the job's exit failed ({failures_in_a_row}/{SSH_FAILURES_ALLOWED} in a row): {error}")
            if failures_in_a_row >= SSH_FAILURES_ALLOWED:
                raise PodError(f"(╯°□°)╯ {SSH_FAILURES_ALLOWED} failed reads of the job's exit in a row; the last: "
                               f"{error}")
            continue
        failures_in_a_row = 0
        exit_text, marked, progress_text = completed.stdout.partition(PROGRESS_MARK)
        if marked:
            saw_progress(progress_text.strip(), clock())
        if exit_text.strip():
            return json.loads(exit_text)


# ---------------------------------------------------------------------------------------------------------------------
# a pod
# ---------------------------------------------------------------------------------------------------------------------

def secrets_directory():
    """$SECRETS as secrets.sh (the one place) sets it."""
    found = subprocess.run(["bash", "-c", f"source {SECRETS_SCRIPT} && printf %s \"$SECRETS\""], text=True,
                           capture_output=True, stdin=subprocess.DEVNULL)
    if found.returncode != 0 or not found.stdout:
        raise PodError(f"secrets.sh named no secrets directory: {found.stderr.strip()}")
    return Path(found.stdout)


def transfer(command):
    """rsync whose output goes to the journal; non-zero raises with the command."""
    say("$ " + " ".join(str(part) for part in command))
    subprocess.run([str(part) for part in command], check=True, stdin=subprocess.DEVNULL,
                   timeout=SSH_COMMAND_TIMEOUT_S)


class Pod:
    """One pod of the pool, by its name. state: booting | ready | slow | broken | no stock | boot failed | old driver |
    mismatch | gone. slowness: its jobs' measured seconds per unit over the reference, the largest seen (1 until
    measured): what its jobs' deadlines scale by."""

    def __init__(self, name, secrets, requires=None, config=None, cards=(), attempt=0, vcpu=None,
                 avoid_datacenters=()):
        self.name = name
        self.ssh_alias = f"runpod-{name}"                      # pod.sh's alias for this name
        self.pod_id_file = secrets / PODS_DIRECTORY_NAME / f"{name}.id"
        self.session_file = secrets / PODS_DIRECTORY_NAME / f"{name}.session.json"
        self.lease_file = secrets / PODS_DIRECTORY_NAME / f"{name}.lease"
        self.requires = requires                               # the requirement set it was opened for
        self.config = config
        self.cards = list(cards)                               # what create may take: accepted, affordable (cards,
                                                               # or a CPU pod's flavors)
        self.vcpu = vcpu                                       # a CPU pod's vcpuCount (cpu_pod_plan)
        self.attempt = attempt                                 # the set's create attempt it is part of (Pool.attempts)
        self.avoid_datacenters = list(avoid_datacenters)       # where create must not put it (slow ones, recently)
        self.slowness = 1.0
        self.created = False                                   # pod.sh create made it (RunPod had an instance)
        self.state = "booting"
        self.facts = None                 # once read: MEASURED_FACTS + the image's facts
        self.problem = None               # why it is broken, mismatched, or why its boot failed
        self.idle_since = None
        self.create_process = None

    def ssh(self, command, check=True, timeout_s=SSH_COMMAND_TIMEOUT_S, input_text=None):
        """One command on the pod through pod.sh's alias (its ControlMaster connection)."""
        completed = subprocess.run(["ssh", self.ssh_alias, command], text=True, capture_output=True,
                                   input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL,
                                   timeout=timeout_s)
        if check and completed.returncode != 0:
            raise PodError(f"ssh {self.ssh_alias} {command!r} exited {completed.returncode}: "
                           f"{completed.stderr.strip()}")
        return completed

    def recorded_pod_id(self):
        try:
            return self.pod_id_file.read_text().strip() or None
        except FileNotFoundError:
            return None

    def start_booting(self):
        threading.Thread(target=self.boot, daemon=True, name=f"boot {self.name}").start()

    def create_command(self):
        image = job_contract.acceptable(self.requires["image"])[0]
        avoid = [",".join(self.avoid_datacenters)] if self.avoid_datacenters else []
        if is_cpu_set(self.requires):
            return [str(POD_SCRIPT), "create-cpu", self.name, image, ",".join(self.cards), str(self.vcpu),
                    str(container_disk_gb(self.requires)), *avoid]
        return [str(POD_SCRIPT), "create", self.name, image, ",".join(self.cards),
                str(math.ceil(self.requires.get("cpu_min", 1))), str(container_disk_gb(self.requires)), *avoid]

    def boot(self):
        """pod.sh create, ship podd-run.sh, read the facts once; ready, old driver, mismatch, or boot failed, with the
        reason."""
        command = self.create_command()
        say(f"{self.name}: {' '.join(command[1:])} (＾_＾)")
        started_s = time.time()
        try:
            if not self.cards:
                raise PodError(f"no card or CPU flavor to ask for {self.requires} (podd opens a pod only with one)")
            # pod.sh's stderr goes on to the journal line by line, and its last lines are kept: RunPod's answer
            self.create_process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                                   text=True)
            said = []
            for line in self.create_process.stderr:
                sys.stderr.write(line)
                sys.stderr.flush()
                said.append(line.rstrip("\n"))
            returncode = self.create_process.wait()
            if returncode == POD_SH_NO_STOCK_EXIT:
                self.problem = " / ".join(said[-2:]) or f"pod.sh create exited {returncode} and said nothing"
                self.state = "no stock"
                return
            if returncode != 0:
                raise PodError(f"pod.sh create {self.name} exited {returncode}: " + " / ".join(said[-3:]))
            self.created = True
            self.ssh(f"mkdir -p {Path(RUNNER_ON_THE_POD).parent} {POD_JOBS}")
            transfer(["rsync", "-rt", RUNNER_SCRIPT, f"{self.ssh_alias}:{RUNNER_ON_THE_POD}"])
            self.facts = self.read_facts(command[3])
        except BaseException as error:
            traceback.print_exc()
            self.problem = f"{type(error).__name__}: {error}"
            self.state = "boot failed"
            return
        too_old = old_driver(self.facts, self.config["min_cuda_version"])
        if too_old:
            self.problem, self.state = too_old, "old driver"
            return
        unmet = unmet_requirements(self.requires, self.facts)
        if unmet:
            self.problem = (f"opened for {requirement_key(self.requires)} and does not satisfy it: "
                            + "; ".join(unmet))
            self.state = "mismatch"
            return
        self.state = "ready"
        say(f"{self.name}: pod {self.facts['pod_id']} ready in {time.time() - started_s:.0f} s: {self.facts}")

    def read_facts(self, image):
        found = self.ssh(f"cat {FACTS_ON_THE_POD}", check=False)
        if found.returncode != 0:
            raise PodError(f"(╯°□°)╯ {image} declares no facts ({FACTS_ON_THE_POD}: {found.stderr.strip()}): a job "
                           f"could not know what it ran on. Rebuild the image (podd's README.md, Images)")
        try:
            session = json.loads(self.session_file.read_text())
        except FileNotFoundError:
            say(f"(・_・;) {self.name}: no {self.session_file}: the pod's price and datacenter are unknown")
            session = {}
        return facts_of_pod(found.stdout, self.ssh(GPU_ON_THE_POD).stdout, self.ssh(CPU_ON_THE_POD).stdout,
                            self.ssh(RAM_ON_THE_POD).stdout, image, session, self.name, self.recorded_pod_id(),
                            job_contract.utc_now_text())

    def stop_booting(self):
        """podd is stopping: end a create still running (its pod, if one was made, is deleted after)."""
        if self.create_process is not None and self.create_process.poll() is None:
            say(f"{self.name}: stopping its pod.sh create")
            self.create_process.terminate()
            try:
                self.create_process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.create_process.kill()
                self.create_process.wait()

    def delete(self, reason):
        """pod.sh delete <name> (it prints the session's cost); a pod never recorded is said, not deleted."""
        self.state = "gone"
        recorded = self.recorded_pod_id()
        if recorded is None:
            say(f"{self.name}: no pod recorded to delete ({reason})")
            return
        say(f"{self.name}: deleting pod {recorded}: {reason}")
        deleted = subprocess.run([str(POD_SCRIPT), "delete", self.name], stdin=subprocess.DEVNULL)
        if deleted.returncode != 0:
            raise PodError(f"(╯°□°)╯ pod.sh delete {self.name} ({recorded}) exited {deleted.returncode}: THE POD MAY "
                           f"STILL BE BILLING; {POD_SCRIPT} account / delete {self.name} by hand")

    def renew_lease(self):
        """Touch pod.sh's watchdog lease: the pod has work. A missing lease is said loudly, and made."""
        if not self.lease_file.exists():
            say(f"(・_・;) {self.name}: no watchdog lease {self.lease_file}: making one")
        self.lease_file.touch()

    def still_recorded(self):
        """False (and said loudly) when this pod is no longer pod.sh's recorded pod for its name."""
        if self.recorded_pod_id() == self.facts["pod_id"]:
            return True
        say(f"(╯°□°)╯ {self.name}: pod {self.facts['pod_id']} is no longer recorded by pod.sh (its watchdog, "
            f"MAX_POD_HOURS, or a hand deleted it); the jobs on it will fail")
        self.state = "gone"
        return False


# ---------------------------------------------------------------------------------------------------------------------
# one job on a pod
# ---------------------------------------------------------------------------------------------------------------------

def remote_directory(stage):
    """The job's directory on the pod: unique across queues (two projects may name a stage alike)."""
    return f"{POD_JOBS}/{hashlib.sha256(str(stage).encode()).hexdigest()[:8]}-{stage.name}"


def kill_the_job(pod, remote):
    """SIGKILL the job's session on its pod (podd-run.sh's pid is its session id). Said; a kill that cannot be made
    is said loudly (the job may still be on the pod)."""
    try:
        completed = pod.ssh(f"pkill -9 -s \"$(cat {remote}/.podd/pid)\"; sleep 1; "
                            f"pgrep -s \"$(cat {remote}/.podd/pid)\" | wc -l", check=False, timeout_s=60)
        left = completed.stdout.strip()
        if completed.returncode != 0 or left != "0":
            say(f"(╯°□°)╯ killing the job in {remote} on {pod.name}: exit {completed.returncode}, {left!r} processes "
                f"left, {completed.stderr.strip()!r}: IT MAY STILL BE RUNNING")
        else:
            say(f"killed the job in {remote} on {pod.name} (its session)")
    except Exception as error:
        say(f"(╯°□°)╯ could not kill the job in {remote} on {pod.name} ({type(error).__name__}: {error}): IT MAY "
            f"STILL BE RUNNING")


def pull_back(pod, remote, stage, job):
    """Every listed output that exists on the pod, and command.log, into the stage, LAID OVER the stage's copy (no
    --delete: a file the attempt wrote replaces the stage's, a file only the stage has -- an earlier attempt's finished
    unit, never shipped back up -- stays); the missing outputs."""
    listed = pod.ssh(f"cd {remote} && for path in {' '.join(shlex.quote(p) for p in job['outputs'])}; do "
                     f"[ -e \"$path\" ] && echo \"$path\"; done; true").stdout.split("\n")
    present = [path for path in listed if path]
    transfer(["rsync", "-rtR", *[f"{pod.ssh_alias}:{remote}/./{path}"
                                 for path in present + [job_contract.COMMAND_LOG]], f"{stage}/"])
    return [path for path in job["outputs"] if path not in present]


def bytes_under(stage, paths):
    """The bytes of these stage paths (files, directories whole): what a ship sends up this machine's uplink."""
    total = 0
    for path in paths:
        top = Path(stage) / path
        if top.is_file():
            total += top.stat().st_size
        for directory, _, files in os.walk(top):
            total += sum((Path(directory) / name).stat().st_size for name in files)
    return total


def command_log_tail(stage, lines=30):
    try:
        return "\n".join((Path(stage) / job_contract.COMMAND_LOG).read_text().splitlines()[-lines:])
    except FileNotFoundError:
        return "(no command.log came back)"


def run_the_job(stage, job, pod, running):
    """Ship, start, wait for the exit, pull, then DONE; or, interrupted, pull what exists and REQUEUE or FAIL.
    A resumable job's `resume_from` files already in the stage are shipped with its inputs (never the rest of what
    earlier attempts brought home: the pull lays the new outputs over it). Interrupted:
    podd stopped it (JobStopped: a slow pod, podd stopping), its deadline passed (JobTimedOut), or its pod failed it
    (ssh, rsync: the pod is BROKEN, takes no more jobs and is deleted once empty); each pulls back every output that
    exists and command.log first, then should_requeue says JOB again or FAILED. The job's own failure (a non-zero exit,
    a missing output) FAILS it with what came back."""
    started = False
    remote = remote_directory(stage)
    label = job.get("label") or stage.name
    timings = {"claimed_at": job_contract.utc_now_text()}
    attempt = len(job.get("attempts") or []) + 1
    try:
        started_s = time.time()
        # a clean directory: a stage run again on the same pod finds its last run's files there otherwise (2026-10-16:
        # four requeued jobs found the killed runs' outputs and their command refused them, "Result directory already exists")
        pod.ssh(f"rm -rf {remote} && mkdir -p {remote}/.podd")
        # what earlier attempts finished stays home: only the files that say what it is go up (resume_from)
        resumed_from = [path for path in job.get("resume_from") or [] if (stage / path).exists()]
        shipped = list(job["inputs"]) + resumed_from
        timings["ship_bytes"] = bytes_under(stage, shipped)
        if shipped:
            transfer(["rsync", "-rtR", *[f"{stage}/./{path}" for path in shipped], f"{pod.ssh_alias}:{remote}/"])
        pod.ssh(f"cat > {remote}/.podd/command", input_text=job["command"] + "\n")
        pod.ssh(f"nohup setsid bash {RUNNER_ON_THE_POD} {remote} >/dev/null 2>&1 < /dev/null &")
        started = True
        timings["ship_s"] = round(time.time() - started_s, 1)
        say(f"{label}: running on {pod.name} (attempt {attempt}), shipped {timings['ship_bytes'] / 1e6:.1f} MB in "
            f"{timings['ship_s']} s{f' (resume_from: {resumed_from})' if resumed_from else ''}: {job['command']}")
        waiting_started_s = time.time()
        read = f"cat {remote}/.podd/exit 2>/dev/null; "
        if "progress" in job:
            read += f"echo {PROGRESS_MARK}; cat {remote}/{shlex.quote(job['progress'])} 2>/dev/null; "
        exited = wait_for_the_exit(lambda: subprocess.run(
            ["ssh", pod.ssh_alias, read + "true"],
            text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=SSH_COMMAND_TIMEOUT_S),
            deadline_s=lambda: job_deadline_s(job, pod.slowness, running.peak_load),
            stop=lambda: running.stop, saw_progress=running.saw_progress)
        started = False                                   # it exited: nothing left to kill
        timings["wait_s"] = round(time.time() - waiting_started_s, 1)
        timings["command_wall_s"] = exited["wall_s"]
        pulling_started_s = time.time()
        missing = pull_back(pod, remote, stage, job)
        pod.ssh(f"rm -rf {remote}")
        timings["pull_s"] = round(time.time() - pulling_started_s, 1)
        if exited["exit_status"] != 0:
            raise JobFailed(f"the command exited {exited['exit_status']} on {pod.name}; command.log's tail:\n"
                            f"{command_log_tail(stage)}")
        if missing:
            raise JobFailed(f"the command exited 0 on {pod.name} but left no {missing}; command.log's tail:\n"
                            f"{command_log_tail(stage)}")
        timings["finished_at"] = job_contract.utc_now_text()
        job_contract.write_json_atomically(stage / DONE, {**job, "used": pod.facts, "timings": timings})
        (stage / RUNNING).unlink(missing_ok=True)
        say(f"{label}: DONE on {pod.name} (＾▽＾) ship {timings['ship_s']} s, wait {timings['wait_s']} s (command "
            f"{timings['command_wall_s']} s), pull {timings['pull_s']} s")
    except BaseException as error:
        if started:
            kill_the_job(pod, remote)
        own_failure = isinstance(error, JobFailed) and not isinstance(error, JobTimedOut)
        pods_fault = not isinstance(error, (JobFailed, JobStopped))
        why = f"{type(error).__name__}: {error}"
        if isinstance(error, JobTimedOut):
            why += (f" (TIMEOUT_MARGIN {TIMEOUT_MARGIN} x expected_s {job['expected_s']} x its pod's slowness "
                    f"{pod.slowness:.2f} x {running.peak_load} job(s) at once)")
        if started and not own_failure:                  # what it wrote so far comes home, whatever stopped it
            try:                                         # (2026-10-15: four jobs timed out at 6 h, 103-109 of
                missing = pull_back(pod, remote, stage, job)    # 115 units done, and left nothing)
                why += f"; pulled back what existed (missing {missing}); command.log's tail:\n{command_log_tail(stage)}"
            except Exception as pull_error:
                why += f"; (╯°□°)╯ pulling what it wrote failed too: {type(pull_error).__name__}: {pull_error}"
                say(f"(╯°□°)╯ {label}: stopped ({type(error).__name__}), and pulling what it wrote failed too: "
                    f"{type(pull_error).__name__}: {pull_error}")
        if started or own_failure:                       # its files on the pod go once what matters came home
            try:
                pod.ssh(f"rm -rf {remote}", check=False)
            except Exception as cleaning_error:
                say(f"(・_・;) {label}: could not remove {remote} on {pod.name}: {cleaning_error}")
        if pods_fault and pod.state in ("ready", "slow"):
            pod.state, pod.problem = "broken", f"a job on it failed: {why.splitlines()[0]}"
            say(f"(╯°□°)╯ {pod.name} is BROKEN ({pod.problem}): it takes no more jobs and goes once empty")
        stopped_for = (error.kind if isinstance(error, JobStopped) else "deadline" if isinstance(error, JobTimedOut)
                       else "pod broke" if pods_fault else None)
        if stopped_for and should_requeue(job, stopped_for, running.progressed()):
            log = stage / job_contract.COMMAND_LOG
            if log.exists():
                (stage / job_contract.PODD_ATTEMPTS_DIRECTORY).mkdir(exist_ok=True)
                shutil.copy(log, stage / job_contract.PODD_ATTEMPTS_DIRECTORY / f"attempt-{attempt}.command.log")
            job_contract.requeue(stage, job, {
                "attempt": attempt, "pod": pod.name, "datacenter": (pod.facts or {}).get("datacenter"),
                "claimed_at": timings["claimed_at"], "stopped_at": job_contract.utc_now_text(), "stopped_for": stopped_for,
                "why": f"{type(error).__name__}: {error}".splitlines()[0], "progress": running.progress_text(),
                "seconds_per_unit": running.rate, "slowness": running.slowness})
        else:
            job_contract.mark_stage_failed(stage, why, f"podd ({pod.name})")


# ---------------------------------------------------------------------------------------------------------------------
# the pool
# ---------------------------------------------------------------------------------------------------------------------

def queue(queue_directories):
    """[(stage, state, age_s)] for every stage of every queue directory, oldest state file first."""
    found = []
    for directory in map(Path, queue_directories):
        if not directory.is_dir():
            continue
        for stage in directory.iterdir():
            state = job_contract.stage_state(stage) if stage.is_dir() else None
            if state:
                try:
                    found.append((stage, state, time.time() - (stage / state).stat().st_mtime))
                except FileNotFoundError:           # it moved on between the two reads: next pass
                    continue
    return sorted(found, key=lambda entry: -entry[2])


class Running:
    """One attempt of a job on a pod: its thread, the pod, what it uses there, the job, when it started, its key (the
    stage and attempt), its load (how many jobs its pod runs now, and the most during it: Pool.count_loads), and what
    the job thread saw of its progress: observations [(time, done, load)], one per change from its first finished
    unit on, and the total. stop: (kind, why) once podd stops it (the job thread raises JobStopped). rate, slowness:
    Pool.judge_speeds's last measure."""

    def __init__(self, thread, pod_name, demand, job, started_s, key):
        self.thread, self.pod_name, self.demand, self.job = thread, pod_name, demand, job
        self.started_s, self.key = started_s, key
        self.observations, self.done, self.total, self.unreadable_said = [], None, None, False
        self.stop, self.rate, self.slowness = None, None, None
        self.load, self.peak_load = 1, 1

    def saw_progress(self, text, now):
        """The job thread's: one poll's progress file. Unreadable (once there is text) is said once, loudly."""
        parsed = parse_progress(text)
        if parsed is None:
            if text and not self.unreadable_said:
                self.unreadable_said = True
                say(f"(╯°□°)╯ {self.key}: its progress file says {text[:80]!r}, not `<done> <total>`: podd cannot "
                    f"measure its pod's speed (podd.job_contract `progress`)")
            return
        done, self.total = parsed
        if self.done is not None and done != self.done:
            self.observations.append((now, done, self.load))
        self.done = done

    def progressed(self):
        return bool(self.observations)

    def progress_text(self):
        return None if self.done is None else f"{self.done}/{self.total}"


class NoPodWait:
    """A requirement set RunPod could not give a pod (no stock, no card under the cap, an old driver, a pod that did not
    boot: Pool.no_pod_miss): since when, since when it has had NO pod (None while a pod
    serves it: the max wait's clock, Pool.fail_the_jobs_that_waited_too_long_for_a_pod), how many attempts missed in a
    row, when the next create may go, the last attempt counted, and every miss (the story its jobs FAIL with past the
    max wait)."""

    def __init__(self, first_miss_s):
        self.first_miss_s, self.podless_since_s = first_miss_s, first_miss_s
        self.misses, self.retry_at_s, self.counted_attempt, self.story = 0, 0.0, -1, []


class Pool:
    def __init__(self, secrets, config):
        self.secrets, self.config = secrets, config
        self.pods = {}                     # name -> Pod (booting, ready, broken, ...)
        self.running = {}                  # stage path -> Running
        self.mismatched = {}               # requirement key -> reason: a pod opened for it did not satisfy it
        self.missing_queues_said = set()
        self.last_status = None
        self.paces_by_command = {}         # command -> {attempt key: (pace, load, pod id, measured at)}: judge_speeds
        self.avoided_datacenter_records = read_avoided_datacenters()   # kind -> datacenter -> {until_unix, until,
                                                                       # why, pod, because}
        self.slow_kept_said = set()        # pods measured slow but kept (their set is waiting for a pod): said once
        self.last_capacity = None          # the capacity of the last GPU pod podd read: a booting GPU pod's guess
        self.offers = None                 # (time, pod.sh avail's JSON) of the cards the config lists
        self.cpu_offers = None             # (time, pod.sh avail-cpu's JSON) of the config's cpu_flavors
        self.undeleted = {}                # pod name -> (pod, reason, when): its delete failed; retried (forget)
        self.cpu_vcpu_ceiling = {}         # requirement key -> the largest CPU pod to ask for after a stock miss
        self.attempts = {}                 # requirement key -> how many create attempts podd made for it
        self.waiting_for_a_pod = {}        # requirement key -> NoPodWait: its last attempt got no pod it can run on

    def free_name(self, kind):
        """podd-g1 .. / podd-c1 .. podd-<kind initial><max_pods[kind]>: the first name of the kind no pod of the pool
        has, and no job still runs under."""
        taken = set(self.pods) | set(self.undeleted) | {running.pod_name for running in self.running.values()}
        return next(f"{POD_NAME_PREFIX}{kind[0]}{number}" for number in range(1, self.config["max_pods"][kind] + 1)
                    if f"{POD_NAME_PREFIX}{kind[0]}{number}" not in taken)

    def pods_of(self, kind):
        """The pool's pods (booting or not) of a kind: "cpu" for a pod opened for a set requiring no card."""
        return [pod for pod in self.pods.values() if pod_kind(pod.requires) == kind]

    def jobs_on(self, name):
        return [running for running in self.running.values() if running.pod_name == name]

    def free(self, pod):
        """The pod's capacity less what its running jobs use."""
        free = capacity_of(pod.facts)
        for running in self.jobs_on(pod.name):
            free = less(free, running.demand)
        return free

    def forget(self, pod, reason):
        """Delete a pod and drop it from the pool. A failed delete is said loudly and RETRIED every
        DELETE_RETRY_SECONDS until RunPod takes it (retry_deletes): the pod may bill, but stopping podd would fail every
        job on every other pod (2026-10-15: one HTTP 500 "unexpected end of JSON input" on a DELETE stopped podd, failed
        two running jobs and left two pods billing)."""
        self.pods.pop(pod.name, None)
        self.slow_kept_said.discard(pod.name)
        try:
            pod.delete(reason)
        except PodError as error:
            say(f"{error} -- podd retries the delete every {DELETE_RETRY_SECONDS} s")
            self.undeleted[pod.name] = (pod, reason, time.time())

    def retry_deletes(self):
        """Every pod whose delete failed, again, at most every DELETE_RETRY_SECONDS; said each time it fails."""
        for name, (pod, reason, failed_s) in list(self.undeleted.items()):
            if time.time() - failed_s < DELETE_RETRY_SECONDS:
                continue
            del self.undeleted[name]
            self.forget(pod, f"retrying the failed delete ({reason})")

    def avoided_datacenters(self, kind, waiting_for_a_pod=False):
        """The datacenters this kind's creates avoid now (the expired ones are said and dropped): a slow pod's (not
        while the set is waiting for a pod: a slow pod is better than none) and an old driver's (always: such a pod runs
        nothing). A CPU host's slowness says nothing of a GPU host's in the same datacenter, and the reverse."""
        recorded = self.avoided_datacenter_records.get(kind, {})
        for datacenter, record in list(recorded.items()):
            if record["until_unix"] <= time.time():
                del recorded[datacenter]
                say(f"{datacenter}: no longer avoided for {kind} pods ({record['because']} until {record['until']}: "
                    f"{record['why']})")
                write_avoided_datacenters(self.avoided_datacenter_records)
        return sorted(datacenter for datacenter, record in recorded.items()
                      if not (waiting_for_a_pod and record["because"] == "slow"))

    def avoid_datacenter(self, pod, because, why, hours):
        """Record the pod's datacenter as avoided by creates of its kind for `hours` (AVOIDED_DATACENTERS_FILE); said
        loudly, as is a pod pod.sh recorded no datacenter for."""
        datacenter, kind = (pod.facts or {}).get("datacenter"), pod_kind(pod.requires)
        if not datacenter:
            say(f"(╯°□°)╯ {pod.name}: pod.sh recorded no datacenter for it, so podd cannot avoid where it was "
                f"({because})")
            return
        until_unix = time.time() + hours * 3600
        self.avoided_datacenter_records.setdefault(kind, {})[datacenter] = {
            "until_unix": until_unix, "pod": pod.name, "why": why, "because": because,
            "until": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(until_unix))}
        write_avoided_datacenters(self.avoided_datacenter_records)
        say(f"(╯°□°)╯ {datacenter} is recorded {because.upper()} for {kind} pods: podd's {kind} creates avoid it for "
            f"{hours} h{', unless RunPod has no stock elsewhere' if because == 'slow' else ''} "
            f"({AVOIDED_DATACENTERS_FILE})")

    def reject_old_driver(self, pod):
        """A GPU pod whose driver is below min_cuda_version (Pod.boot: old_driver): deleted at once, its datacenter
        avoided for OLD_DRIVER_DATACENTER_HOURS, and a no-pod miss for its set (RunPod gave no host the images run on):
        the jobs stay JOB and the next create backs off. All said loudly."""
        key = requirement_key(pod.requires)
        say(f"(╯°□°)╯ {pod.name} (pod {pod.facts.get('pod_id')}) {pod.problem}: deleting it; its jobs stay queued")
        self.avoid_datacenter(pod, "old driver", pod.problem, OLD_DRIVER_DATACENTER_HOURS)
        self.forget(pod, pod.problem)
        self.no_pod_miss(key, pod.attempt, f"{pod.name} landed on a host it cannot use: {pod.problem}")

    def count_loads(self):
        """Every running job's load: how many jobs its pod runs now, and the most during it (its deadline's factor)."""
        for running in self.running.values():
            running.load = len(self.jobs_on(running.pod_name))
            running.peak_load = max(running.peak_load, running.load)

    def judge_speeds(self):
        """SLOW HOSTS. Every running job with a measured rate (seconds_per_unit) has a pace (pace_of: over its own
        expected seconds per unit), judged against the median pace of other pods at the same load (reference_pace: the
        load of a rate is the most jobs its pod ran over the units measured); at SLOW_FACTOR or more its pod is SLOW
        (mark_slow) -- unless its set is waiting for a pod: nothing would replace it, so it is kept and said. A job with no
        reference is not judged; alone on its pod its slowness is its pace (expected_s's word, never a judgement). A
        pod's slowness, what its jobs' deadlines scale by, is the largest its jobs measured."""
        now = time.time()
        for paces in self.paces_by_command.values():         # too old to be a reference: forgotten
            for key in [key for key, measured in paces.items() if now - measured[3] > REFERENCE_MAX_AGE_SECONDS]:
                del paces[key]
        for running in list(self.running.values()):
            rate = seconds_per_unit(running.observations)
            if rate is None:
                continue
            load = max(observation[2] for observation in running.observations)
            pace = pace_of(rate, running.job, running.total)
            pod = self.pods.get(running.pod_name)
            pod_id = ((pod.facts if pod else None) or {}).get("pod_id") or running.pod_name
            paces = self.paces_by_command.setdefault(running.job["command"], {})
            paces[running.key] = (pace, load, pod_id, now)
            found = reference_pace(load, {key: measured for key, measured in paces.items() if measured[2] != pod_id},
                                   now)
            running.rate = round(rate, 2)
            if found is None:
                if load == 1:
                    running.slowness = round(pace, 2)
                    if pod is not None:
                        pod.slowness = max(pod.slowness, running.slowness)
                continue
            reference, pods_measured = found
            running.slowness = round(pace / reference, 2)
            if pod is None:
                continue
            pod.slowness = max(pod.slowness, running.slowness)
            if running.slowness < SLOW_FACTOR or pod.state != "ready":
                continue
            why = (f"{running.job.get('label') or running.key} does {rate:.1f} s a unit ({running.progress_text()}), "
                   f"pace {pace:.2f} of its expected_s, {running.slowness:.2f}x the median pace {reference:.2f} of "
                   f"{pods_measured} other pods running the same command, {load} job(s) at once >= SLOW_FACTOR "
                   f"{SLOW_FACTOR}")
            key = requirement_key(pod.requires)
            if key in self.waiting_for_a_pod:
                if pod.name not in self.slow_kept_said:
                    self.slow_kept_said.add(pod.name)
                    say(f"(・_・;) {pod.name} is slow ({why}), but its set is waiting for a pod (nothing would replace it): it "
                        f"is KEPT, its "
                        f"jobs' deadlines scaled by {pod.slowness:.2f}")
                continue
            self.mark_slow(pod, why)

    def mark_slow(self, pod, why):
        """A SLOW pod: it takes no more jobs and goes once empty (the opening rule replaces it: a slow pod serves no
        set); its resumable jobs are stopped (pulled home and requeued: run_the_job) when the move is worth a boot
        (worth_moving), each at most MAX_SLOW_STOPS times; the others run on under a deadline scaled by its slowness.
        Its datacenter is avoided by creates of its kind for SLOW_DATACENTER_HOURS. All said loudly."""
        pod.state, pod.problem = "slow", f"SLOW: {why}"
        self.slow_kept_said.discard(pod.name)
        say(f"(╯°□°)╯ {pod.name} ({(pod.facts or {}).get('datacenter')}, {(pod.facts or {}).get('gpu') or 'CPU'}) IS "
            f"SLOW: {why}. It takes no more jobs and goes once empty")
        self.avoid_datacenter(pod, "slow", why, SLOW_DATACENTER_HOURS)
        for running in self.jobs_on(pod.name):
            label = running.job.get("label") or running.key
            moved = sum(1 for attempt in running.job.get("attempts") or [] if attempt.get("stopped_for") == "slow pod")
            if not running.job.get("resumable"):
                why_not = "not resumable"
            elif moved >= MAX_SLOW_STOPS:
                why_not = f"moved off slow pods {moved} times already: its expected_s may be optimistic"
            elif not worth_moving(seconds_left_here(running, max(pod.slowness, 1.0), time.time()),
                                  max(pod.slowness, 1.0), self.config["boot_seconds"]):
                why_not = (f"{running.progress_text()} done: moving it saves less than a boot "
                           f"(boot_seconds {self.config['boot_seconds']})")
            else:
                running.stop = ("slow pod", f"its pod {pod.name} is slow ({why})")
                say(f"{label}: stopping it on slow {pod.name}: what it finished comes home and it is requeued")
                continue
            say(f"(・_・;) {label}: runs on, on slow {pod.name} ({why_not}); its deadline is now "
                f"{job_deadline_s(running.job, pod.slowness, running.peak_load):.0f} s")

    def fail_waiting(self, waiting, key, reason):
        for stage, job in [(stage, job) for stage, job in waiting if requirement_key(job["requires"]) == key]:
            job_contract.mark_stage_failed(stage, reason, "podd")
            waiting.remove((stage, job))

    def waiting_jobs(self):
        """[(stage, job)] of every JOB not yet running, oldest first; a JOB that breaks the contract FAILS here."""
        for directory in self.config["queue_directories"]:
            if not Path(directory).is_dir() and directory not in self.missing_queues_said:
                say(f"(・_・;) queue directory {directory} does not exist (yet); watching for it")
                self.missing_queues_said.add(directory)
        waiting = []
        for stage, state, _ in queue(self.config["queue_directories"]):
            if state != JOB or str(stage) in self.running:
                continue
            try:
                job = job_contract.read_state(stage, JOB)
            except (FileNotFoundError, json.JSONDecodeError) as error:
                say(f"(・_・;) {stage}: its JOB could not be read this pass ({type(error).__name__}); next pass")
                continue
            problems = job_contract.job_problems(job)
            if problems:
                job_contract.mark_stage_failed(stage, "the JOB breaks the contract (podd.job_contract):\n  - "
                                          + "\n  - ".join(problems), "podd")
                continue
            waiting.append((stage, job))
        return waiting

    def fail_the_jobs_no_pod_can_hold(self, waiting, ready):
        """A job bigger than an EMPTY ready pod of its set would wait forever: it FAILS with the numbers."""
        for stage, job in list(waiting):
            serving = [pod for pod in ready.values() if not unmet_requirements(job["requires"], pod.facts)]
            if serving and not any(how_many_fit(job, capacity_of(pod.facts)) for pod in serving):
                job_contract.mark_stage_failed(stage, f"no pod of its requires can hold it: it asks cpu {job['cpu']}, "
                                                 f"gpu_memory_gb {job['gpu_memory_gb']}, "
                                                 f"ram_gb {job.get('ram_gb', 0)}; "
                                                 f"the pods that satisfy its requires have "
                                                 f"{[capacity_of(pod.facts) for pod in serving]} "
                                                 f"(ask for a bigger pod: "
                                                 f"requires.cpu_min / gpu_memory_gb_min)", "podd")
                waiting.remove((stage, job))

    def cards_to_ask(self, requires):
        """(cards, why none): every card of the config this set accepts at or under the price cap, in-stock first,
        cheapest first (affordable_cards; pod.sh avail, at most every OFFERS_SECONDS). No card: why, for the stock
        wait (no_pod_miss), which says it and bounds it."""
        accepted = cards_for(requires, self.config["cards"])
        if self.offers is None or time.time() - self.offers[0] > OFFERS_SECONDS:
            asked = subprocess.run([str(POD_SCRIPT), "avail", ",".join(card["name"] for card in self.config["cards"])],
                                   text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
            if asked.returncode != 0:                    # podd goes on; it asks again in OFFERS_SECONDS
                self.offers = (time.time(), {})
                return [], (f"pod.sh avail exited {asked.returncode}: {asked.stderr.strip()} (RunPod's prices and "
                            f"stock could not be read)")
            self.offers = (time.time(), json.loads(asked.stdout))
        cards = affordable_cards(accepted, self.offers[1], self.config["max_price_per_hour"])
        if cards:
            return cards, None
        return [], (f"PRICE CAP: no card the set accepts is priced at or under ${self.config['max_price_per_hour']}"
                    f"/h. Wanted: " + "; ".join(f"{card} {self.offers[1].get(card)}" for card in accepted))

    def cpu_plan(self, requires, jobs):
        """(plan, why none) for a CPU set's waiting jobs: cpu_pod_plan on pod.sh avail-cpu (at most every
        OFFERS_SECONDS)."""
        if self.cpu_offers is None or time.time() - self.cpu_offers[0] > OFFERS_SECONDS:
            asked = subprocess.run([str(POD_SCRIPT), "avail-cpu", ",".join(self.config["cpu_flavors"])], text=True,
                                   capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
            if asked.returncode != 0:
                self.cpu_offers = (time.time(), {})
                return None, (f"pod.sh avail-cpu exited {asked.returncode}: {asked.stderr.strip()} (RunPod's CPU "
                              f"prices could not be read)")
            self.cpu_offers = (time.time(), json.loads(asked.stdout))
        return cpu_pod_plan(jobs, requires, self.config["cpu_flavors"], self.cpu_offers[1],
                            self.config["max_price_per_hour"], self.cpu_vcpu_ceiling.get(requirement_key(requires)))

    def no_pod_miss(self, key, attempt, why):
        """RunPod gave the set no pod (its create was refused for stock, no card is under the cap, its pod landed on an
        old driver, or its pod did not boot): the set's next
        create waits no_pod_backoff_seconds, its jobs stay JOB, and this is said loudly once per attempt (the creates
        of one attempt that all miss count once). attempt None: the miss was podd's own check, never a create."""
        wait = self.waiting_for_a_pod.setdefault(key, NoPodWait(time.time()))
        if attempt is not None and attempt <= wait.counted_attempt:
            wait.story[-1] += f"; and {why}"
            return
        wait.misses += 1
        wait.counted_attempt = attempt if attempt is not None else wait.counted_attempt
        backoff_s = no_pod_backoff_seconds(wait.misses)
        wait.retry_at_s = time.time() + backoff_s
        wait.story.append(f"{job_contract.utc_now_text()} {why}")
        say(f"(╯°□°)╯ NO POD for {key}: {why}. Its jobs stay queued; the next create (every card under the cap) in "
            f"{backoff_s:.0f} s (miss {wait.misses} in a row, waiting {(time.time() - wait.first_miss_s) / 60:.1f} of "
            f"max_wait_for_a_pod_minutes {self.config['max_wait_for_a_pod_minutes']})")

    def fail_the_jobs_that_waited_too_long_for_a_pod(self, waiting):
        """A set waiting for a pod that has had NO pod for max_wait_for_a_pod_minutes: its waiting jobs FAIL with every
        miss. While a pod serves it (ready or slow, its facts satisfying the set) or a job of it runs, its jobs wait
        and the clock is reset (2026-10-22: 20 jobs FAILED "no pod for 60 min" while 3 pods of their set ran other
        chunks). A set with no job waiting any more forgets its wait."""
        waiting_keys = {requirement_key(job["requires"]) for _, job in waiting}
        for key, wait in list(self.waiting_for_a_pod.items()):
            if key not in waiting_keys:
                del self.waiting_for_a_pod[key]
                continue
            if self.has_a_pod(key):
                wait.podless_since_s = None
                continue
            if wait.podless_since_s is None:
                wait.podless_since_s = time.time()
                say(f"(╯°□°)╯ {key}: waiting for a pod and now with no pod: its jobs FAIL in "
                    f"{self.config['max_wait_for_a_pod_minutes']} min (max_wait_for_a_pod_minutes) unless RunPod "
                    f"gives it one")
            if time.time() - wait.podless_since_s > self.config["max_wait_for_a_pod_minutes"] * 60:
                del self.waiting_for_a_pod[key]
                self.fail_waiting(waiting, key, f"no pod for {self.config['max_wait_for_a_pod_minutes']} min "
                                                f"(max_wait_for_a_pod_minutes): RunPod gave its requires no pod in "
                                                f"{wait.misses} attempts:\n  - " + "\n  - ".join(wait.story))

    def has_a_pod(self, key):
        """Does a pod serve this set now: a job of it running, or a ready or slow pod whose facts satisfy it?"""
        requires = json.loads(key)
        return (any(requirement_key(running.job["requires"]) == key for running in self.running.values())
                or any(pod.state in ("ready", "slow") and pod.facts and not unmet_requirements(requires, pod.facts)
                       for pod in self.pods.values()))

    def open_pods(self, unplaced, ready, waiting_keys):
        """THE OPENING RULE per requirement set with jobs waiting, oldest set first (pods_to_open)."""
        booting = [pod for pod in self.pods.values() if pod.state == "booting"]
        for key in waiting_keys:
            wait = self.waiting_for_a_pod.get(key)
            if wait and (time.time() < wait.retry_at_s
                         or any(requirement_key(pod.requires) == key for pod in booting)):
                continue                                 # backing off, or its one probe create is still out
            requires = json.loads(key)
            plan, why_no_plan = None, None
            if is_cpu_set(requires):                     # a CPU pod: its size is the plan, not a guess
                plan, why_no_plan = self.cpu_plan(requires, [job for _, job in unplaced
                                                             if requirement_key(job["requires"]) == key])
                guess = plan["capacity"] if plan else {"cpu": 0.0, "gpu_memory_gb": 0.0, "ram_gb": 0.0}
            else:
                memory = min([card["memory_gb"] for card in self.config["cards"]
                              if card["name"] in cards_for(requires, self.config["cards"])] or [0])
                guess = {**FIRST_GUESS_CAPACITY, **(self.last_capacity or {}), "gpu_memory_gb": memory}
            serving = [(capacity_of(pod.facts), False) for pod in ready.values()
                       if not unmet_requirements(requires, pod.facts)]
            serving += [(guess, True) for pod in booting if booting_pod_serves(pod.requires, requires,
                                                                              self.config["cards"])]
            backlog = [(running.job, max(running.job["expected_s"] - (time.time() - running.started_s), 0))
                       for running in self.running.values() if requirement_key(running.job["requires"]) == key
                       and running.pod_name in ready]        # a job on a slow or broken pod stays there
            backlog += [(job, job["expected_s"]) for _, job in unplaced if requirement_key(job["requires"]) == key]
            kind = pod_kind(requires)
            room = self.config["max_pods"][kind] - len(self.pods_of(kind))
            wanted = pods_to_open(backlog, serving, guess, room, self.config["boot_seconds"])
            if not wanted:
                continue
            if is_cpu_set(requires):
                if not plan:
                    self.no_pod_miss(key, None, why_no_plan)
                    continue
                cards = plan["flavors"]
                offered = (f"{plan['vcpu']} vCPU (${plan['price_per_hour']}/h, {plan['jobs_per_pod']} job(s) a pod, "
                           f"{plan['capacity']['ram_gb']:g} GB) of " + ", ".join(
                               f"{flavor} ${self.cpu_offers[1][flavor]['price_per_vcpu_hour']}/vCPU-h"
                               for flavor in cards))
            else:
                cards, why_none = self.cards_to_ask(requires)
                if not cards:
                    self.no_pod_miss(key, None, why_none)
                    continue
                offered = ", ".join(f"{card} ${self.offers[1][card]['price_per_hour']}/h" for card in cards)
            staggered = ""
            if wait and wait.counted_attempt >= 0:       # RunPod refused the last create: one probe, not a burst
                wanted, staggered = 1, " (one: the set's last create gave it no pod)"
            say(f"drain of {requirement_key(requires)}: {drain_seconds(backlog, serving):.0f} s on {len(serving)} "
                f"pod(s) up or booting > boot_seconds {self.config['boot_seconds']}: opening {wanted}{staggered} "
                f"(drain then {drain_seconds(backlog, serving + [(guess, True)] * wanted):.0f} s)")
            self.attempts[key] = self.attempts.get(key, 0) + 1
            avoided = self.avoided_datacenters(kind, waiting_for_a_pod=bool(wait))
            forgiven = sorted(set(self.avoided_datacenters(kind)) - set(avoided))
            if forgiven:                                 # avoiding a slow datacenter must never cost a pod
                say(f"(・_・;) {key} is waiting for a pod: its create does not avoid the slow datacenters "
                    f"{', '.join(forgiven)}: a slow pod is better than none")
            for _ in range(wanted):
                pod = Pod(self.free_name(kind), self.secrets, requires, self.config, cards, self.attempts[key],
                          plan["vcpu"] if plan else None, avoided)
                say(f"{pod.name}: opening a pod for requirement set {key} (attempt {self.attempts[key]}), offering "
                    f"{offered}{'; avoiding the datacenters ' + ', '.join(avoided) if avoided else ''}")
                self.pods[pod.name] = pod
                pod.start_booting()

    def step(self):
        """One pass: finished threads, boots, the placement, the opening rule, idle and broken pods, the status."""
        self.running = {stage: running for stage, running in self.running.items() if running.thread.is_alive()}
        self.count_loads()
        self.judge_speeds()
        self.retry_deletes()
        waiting = self.waiting_jobs()
        for pod in list(self.pods.values()):
            key = requirement_key(pod.requires)
            if (pod.state == "ready" and key in self.waiting_for_a_pod     # a pod the set can use (not one on an old
                    and pod.attempt > self.waiting_for_a_pod[key].counted_attempt):   # driver), made AFTER the miss
                wait = self.waiting_for_a_pod.pop(key)    # (2026-10-15: a pod made before it cleared the wait every pass)
                say(f"{pod.name}: RunPod gave {key} a pod after {wait.misses} miss(es), "
                    f"{(time.time() - wait.first_miss_s) / 60:.1f} min")
            if pod.state == "no stock":
                self.pods.pop(pod.name)
                if is_cpu_set(pod.requires) and pod.vcpu and pod.vcpu > 2:
                    ceiling = min(self.cpu_vcpu_ceiling.get(key, pod.vcpu), pod.vcpu // 2)
                    self.cpu_vcpu_ceiling[key] = ceiling
                    say(f"(・_・;) {pod.name}: no {pod.vcpu}-vCPU CPU pod in stock: the set's pods are at most {ceiling} "
                        f"vCPU from now on (one job's size at least)")
                self.no_pod_miss(key, pod.attempt, f"{pod.name} was offered {', '.join(pod.cards)}; {pod.problem}")
            elif pod.state == "boot failed":
                self.pods.pop(pod.name)
                say(f"(╯°□°)╯ {pod.name} did not boot: {pod.problem}; deleting it, its set's jobs stay queued")
                self.forget(pod, "it failed its create or its read")
                self.no_pod_miss(key, pod.attempt, f"{pod.name} did not boot: {pod.problem}")
            elif pod.state == "old driver":
                self.pods.pop(pod.name)
                self.reject_old_driver(pod)
            elif pod.state == "mismatch":
                self.pods.pop(pod.name)
                self.mismatched[key] = f"{pod.name} ({pod.facts.get('image')}) {pod.problem}"
                say(f"(╯°□°)╯ {self.mismatched[key]}: its jobs fail, and so will later jobs of that set")
                self.forget(pod, "its facts do not satisfy the set it was opened for")
            elif pod.state in ("ready", "slow", "broken") and not pod.still_recorded():
                self.pods.pop(pod.name)
            elif pod.state == "ready" and pod.facts.get("gpu") is not None:
                self.last_capacity = capacity_of(pod.facts)
        for key, reason in self.mismatched.items():
            self.fail_waiting(waiting, key, f"no pod can satisfy this job's requires: a pod opened for it did not "
                                            f"({reason})")
        self.fail_the_jobs_that_waited_too_long_for_a_pod(waiting)

        ready = {name: pod for name, pod in self.pods.items() if pod.state == "ready"}
        self.fail_the_jobs_no_pod_can_hold(waiting, ready)
        for stage, pod_name in place(waiting, {name: (pod.facts, self.free(pod)) for name, pod in ready.items()}):
            self.claim(stage, dict(waiting)[stage], ready[pod_name])
        unplaced = [(stage, job) for stage, job in waiting if str(stage) not in self.running]
        self.open_pods(unplaced, ready, list(dict.fromkeys(requirement_key(job["requires"]) for _, job in unplaced)))

        for pod in list(self.pods.values()):
            if pod.state not in ("ready", "slow", "broken"):
                continue
            if self.jobs_on(pod.name):
                pod.idle_since = None
                pod.renew_lease()
            elif pod.state in ("slow", "broken"):
                self.forget(pod, pod.problem)
            elif pod.idle_since is None:
                pod.idle_since = time.time()
            elif time.time() - pod.idle_since > self.config["idle_minutes"] * 60:
                self.forget(pod, f"no job for {self.config['idle_minutes']} min (idle_minutes)")
        self.write_status()

    def claim(self, stage, job, pod):
        """JOB -> RUNNING for this pod (its facts into the job), and the job's thread."""
        try:
            os.rename(stage / JOB, stage / RUNNING)
        except FileNotFoundError:
            say(f"(・_・;) {stage}: its JOB went before the claim (its requester stopped?); not run")
            return
        job_contract.write_json_atomically(stage / RUNNING, {**job, "pod": pod.facts, "claimed_at": job_contract.utc_now_text()})
        running = Running(None, pod.name, demand_of(job, capacity_of(pod.facts)), job, time.time(),
                          f"{stage}#{len(job.get('attempts') or []) + 1}")
        running.thread = threading.Thread(target=run_the_job, args=(stage, job, pod, running), daemon=True,
                                          name=f"job {stage.name}")
        self.running[str(stage)] = running
        self.count_loads()                               # before its first poll: its deadline knows its neighbours
        running.thread.start()

    def write_status(self):
        status = {"max_pods": self.config["max_pods"], "max_price_per_hour": self.config["max_price_per_hour"],
                  "queue_directories": self.config["queue_directories"], "pods": [
                      {"name": pod.name, "state": pod.state, "problem": pod.problem, "requires": pod.requires,
                       "facts": pod.facts, "slowness": pod.slowness,
                       "used": {resource: round(sum(running.demand[resource] for running in
                                                    self.jobs_on(pod.name)), 2) for resource in RESOURCES},
                       "jobs": sorted(stage for stage, running in self.running.items() if running.pod_name == pod.name),
                       "progress": {stage: {"done": running.progress_text(), "seconds_per_unit": running.rate,
                                            "slowness": running.slowness, "load": running.load}
                                    for stage, running in self.running.items() if running.pod_name == pod.name}}
                      for pod in sorted(self.pods.values(), key=lambda pod: pod.name)],
                  "avoided_datacenters": self.avoided_datacenter_records,
                  "waiting_for_a_pod": {key: {
                      "misses": wait.misses, "since": utc_text(wait.first_miss_s),
                      "no_pod_since": utc_text(wait.podless_since_s) if wait.podless_since_s is not None else None,
                      "fails_at": (utc_text(wait.podless_since_s + self.config["max_wait_for_a_pod_minutes"] * 60)
                                   if wait.podless_since_s is not None else None),
                      "next_create_at": utc_text(wait.retry_at_s), "last": wait.story[-1][:300] if wait.story else None}
                      for key, wait in self.waiting_for_a_pod.items()}}
        if status != self.last_status:
            STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            job_contract.write_json_atomically(STATUS_FILE, {**status, "written_at": job_contract.utc_now_text()})
            self.last_status = status

    def shut_down(self):
        """Every running job is stopped ("podd stopping") and pulls home what it wrote (a resumable one is requeued,
        another FAILS), for SHUTDOWN_PULL_SECONDS at most; a job still RUNNING after that FAILS; then every pod,
        booting or not, is deleted. A delete that fails is said and the next one still runs."""
        for running in self.running.values():
            running.stop = ("podd stopping", "podd stopped while it ran")
        until_s = time.time() + SHUTDOWN_PULL_SECONDS
        for running in list(self.running.values()):
            running.thread.join(max(0.0, until_s - time.time()))
        for stage, state, _ in queue(self.config["queue_directories"]):
            if state == RUNNING:
                job_contract.mark_stage_failed(stage, f"podd stopped while it ran, and the job did not stop and come home "
                                                 f"within SHUTDOWN_PULL_SECONDS {SHUTDOWN_PULL_SECONDS}", "podd")
        for pod in list(self.pods.values()):
            try:
                pod.stop_booting()
                pod.delete("podd stopped")
            except Exception:
                traceback.print_exc()
        self.pods.clear()
        self.write_status()


def utc_text(unix_s):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(unix_s))


def read_avoided_datacenters(path=None):
    """The avoided datacenters podd recorded per pod kind ({} when none); a file that is not that is said loudly and
    set aside."""
    path = Path(path or AVOIDED_DATACENTERS_FILE)
    try:
        recorded = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        say(f"(╯°□°)╯ {path} is not JSON ({error}): no datacenter is avoided; it is moved to {path}.broken")
        os.replace(path, path.with_name(path.name + ".broken"))
        return {}
    if not (isinstance(recorded, dict) and set(recorded) <= {"gpu", "cpu"}
            and all(isinstance(datacenters, dict) and all(
                isinstance(record, dict) and job_contract.is_number(record.get("until_unix"))
                and record.get("because") in AVOIDED_BECAUSE
                for record in datacenters.values()) for datacenters in recorded.values())):
        say(f"(╯°□°)╯ {path} is not {{\"gpu\" | \"cpu\": {{datacenter: {{until_unix, because {AVOIDED_BECAUSE}, "
            f"...}}}}}}: no datacenter is avoided; it is moved to {path}.broken")
        os.replace(path, path.with_name(path.name + ".broken"))
        return {}
    return recorded


def write_avoided_datacenters(recorded, path=None):
    path = Path(path or AVOIDED_DATACENTERS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    job_contract.write_json_atomically(path, recorded)


def read_status():
    """podd's status file, or None when podd never wrote one."""
    try:
        return json.loads(STATUS_FILE.read_text())
    except FileNotFoundError:
        return None


def delete_every_recorded_pod(secrets):
    """At start-up: a pod pod.sh still records was left by a podd that died; its jobs are FAILED, so it goes."""
    listed = subprocess.run([str(POD_SCRIPT), "recorded"], text=True, capture_output=True, stdin=subprocess.DEVNULL)
    if listed.returncode != 0:
        raise PodError(f"pod.sh recorded exited {listed.returncode}: {listed.stderr.strip()}")
    for name in listed.stdout.split():
        Pod(name, secrets).delete("recorded when podd started: a podd that died left it")


def stop_on_sigterm(signal_number, frame):
    """systemctl stop: a clean exit (status 0) through main's finally, which deletes every pod."""
    say(f"signal {signal_number}")
    raise SystemExit(0)


def run_forever():
    """`python3 -m podd run` (systemd/podd.service): read the config once, then serve every queue until stopped."""
    signal.signal(signal.SIGTERM, stop_on_sigterm)
    config = job_contract.read_config()
    say(f"up ({job_contract.CONFIG_PATH}): queues {config['queue_directories']}, up to "
        f"{config['max_pods']['gpu']} GPU and {config['max_pods']['cpu']} CPU pods, cards at "
        f"most ${config['max_price_per_hour']}/h, another pod only when the backlog drains in more than "
        f"{config['boot_seconds']} s, a pod deleted after {config['idle_minutes']} idle min")
    for stage, state, _ in queue(config["queue_directories"]):
        if state == RUNNING:
            job_contract.mark_stage_failed(stage, "RUNNING when podd started: the podd that ran it died; it may still be "
                                             "on a pod, and nothing will fetch it", "podd")
    secrets = secrets_directory()
    delete_every_recorded_pod(secrets)
    pool = Pool(secrets, config)
    try:
        while True:
            pool.step()
            time.sleep(POLL_SECONDS)
    finally:
        say("stopping: every running job is stopped and pulls home what it wrote (a resumable one is requeued), then "
            "every pod is deleted (never left billing)")
        pool.shut_down()

