"""THE JOB CONTRACT between podd (podd.daemon, the pod job runner) and anything that wants work done on a RunPod pod
(a GPU pod, or a CPU pod: requires.gpu "none"): one definition of the queue's names, the JOB file's keys and podd's
config file, standard library only, so any client's python imports it (`from podd import job_contract`, with podd
installed: README.md "Clients"). architecture/README.md is the pattern.

A JOB IS A DIRECTORY (a "stage") in one of the queue directories podd's config lists. It holds the job's input files
and ONE state file (when several are present, the first of STATES wins):

    JOB       the requester wrote it (write_job): JSON, the keys below. podd may run it.
    RUNNING   podd claimed it (rename JOB -> RUNNING) for one pod: JOB's keys + `pod` (that pod's facts).
    DONE      the command exited 0 and every output is back in the stage: JOB's keys + `used` (the pod's facts: what
              the job ran on) + `timings`. The requester reads it and removes it (or the whole stage).
    FAILED    why, when, the traceback, and the command log's tail. The stage is kept as it was, with every output the
              job wrote before it stopped (podd pulls them back whatever ended it). Delete it once read.

Every write is a rename or an atomic replace. Who writes what: the requester JOB, podd RUNNING and DONE, either FAILED,
and podd JOB again when it REQUEUES a resumable job (requeue: RUNNING -> JOB, + one record in `attempts`).

THE JOB FILE:
    command      shell text run on the pod IN THE JOB'S DIRECTORY, in a session of its own; stdout and stderr go to
                 command.log, which always comes back. Exit 0 = success.
    inputs       [path in the stage]: shipped to the pod before the command (directories whole).
    outputs      [path in the stage]: pulled back after it, each LAID OVER the stage's copy: a file the attempt has
                 replaces the stage's, a file only the stage has stays (podd never deletes a file of the stage). What
                 exists is pulled even when the command failed (so its logs come home); a listed output missing after
                 exit 0 FAILS the job.
    requires     what the pod must be. `image`: a tag, or a list of acceptable tags (podd launches the FIRST when it
                 opens a pod for the job). Optional: `gpu` ("any" card, the default; a list of acceptable card
                 names; or "none": a CPU pod, no card at all, one of the config's cpu_flavors, sized by the job's
                 cpu / ram_gb: podd.daemon cpu_pod_plan), `gpu_memory_gb_min` (decimal GB, nvidia-smi's
                 total; never with "none"), `cpu_min` (the pod's CPU quota), `disk_gb_min` (decimal GB of the
                 pod's container disk, RunPod's containerDiskInGb for it: podd creates a pod for the job with at
                 least that; on a CPU pod at most 10 GB a vCPU, podd.daemon cpu_pod_plan). EVERY OTHER
                 KEY is a fact the image declares in /etc/podd-facts.json, matched by equality: a value, or a list of
                 acceptable values (an image without the key does not match).
    cpu          the cores it keeps busy (a positive number), or "all": the pod's whole CPU quota (it runs alone).
    gpu_memory_gb  the card memory it uses (decimal GB; 0 for a CPU job, and 0 whenever requires.gpu is "none").
    expected_s   its wall time ALONE on a pod of normal speed (a FAST host: podd measures the slow ones), from the
                 job's own size (the client knows its job; podd does not). podd's drain estimate (when to open another
                 pod) and the job's DEADLINE come from it: podd kills the job's session (and only it) past
                 TIMEOUT_MARGIN x expected_s x its pod's measured slowness x the most jobs its pod ran at once during
                 it (podd.daemon job_deadline_s; there is no fixed timeout, 2026-10-16).
    ram_gb       optional: the RAM it uses (decimal GB; default 0).
    progress     optional: a path in the stage that the command keeps rewriting, one line `<done> <total>` (two
                 integers, units of its own: frequencies, ...). podd reads it every poll and measures the job's rate
                 from it: a job whose pace (seconds per unit over expected_s / total) is SLOW_FACTOR x the median pace
                 of other pods running the same command at the same load marks its pod SLOW (podd.daemon
                 judge_speeds; never expected_s alone).
    resumable    optional, true only with `progress` and `resume_from`: run again, the command does only what earlier
                 attempts did not. podd REQUEUES the job (JOB again) instead of failing it when podd stops it (a slow
                 pod, podd stopping), its pod breaks, or its deadline passes after it made progress; at most
                 MAX_ATTEMPTS attempts (podd.daemon). WHAT EARLIER ATTEMPTS BROUGHT HOME STAYS HOME (2026-10-21: the
                 kept outputs went back up this machine's uplink, 0.2-2.2 GB a job, and timed out): the next attempt
                 gets the inputs and the `resume_from` files only, and its outputs are laid over the stage's
                 (`outputs`), so the command writes the units it did plus the `resume_from` files brought up to date,
                 and never needs a finished unit's file on the pod.
    resume_from  with `resumable` (required there, nowhere else): [path in the stage, inside one of the outputs]: the
                 SMALL files that say what earlier attempts finished (a manifest; a log to append to), never the work
                 itself. podd ships those present in the stage with the inputs; none present = a first attempt.
    label        optional: how podd's log names the job.
    attempts     podd's: one record per earlier attempt of a requeued job (pod, datacenter, why it stopped, progress,
                 seconds per unit); a requester never writes it.
    podd packs jobs onto a pod by cpu / gpu_memory_gb / ram_gb against what it measured the pod has.

AN IMAGE PODD CAN RUN starts sshd with RunPod's PUBLIC_KEY on its own (port 22), carries bash, awk, procps
(ps/pkill/pgrep) and rsync, and declares its facts in /etc/podd-facts.json (a JSON object; podd measures pod_name,
pod_id, image, gpu, gpu_memory_gb, cpu, ram_gb, disk_gb, datacenter, cost_per_hr, ready_at itself, so an image may
not declare those).
"""
import datetime
import json
import os
import traceback
from pathlib import Path

JOB, RUNNING, DONE, FAILED = "JOB", "RUNNING", "DONE", "FAILED"
STATES = (FAILED, DONE, RUNNING, JOB)                  # a stage's state: the first of these present
COMMAND_LOG = "command.log"                            # the command's stdout + stderr, always pulled back
POD_MEASURED_REQUIREMENTS = ("image", "gpu", "gpu_memory_gb_min", "cpu_min", "disk_gb_min")   # podd's own; any
                                                                                     # other key is a fact the image
                                                                                     # declares
JOB_KEYS = ("command", "inputs", "outputs", "requires", "cpu", "gpu_memory_gb", "expected_s")
OPTIONAL_JOB_KEYS = ("ram_gb", "progress", "resumable", "resume_from", "label", "queued_at", "attempts")
PODD_ATTEMPTS_DIRECTORY = ".podd"                      # in the stage: each requeued attempt's command.log
                                                       # (attempt-<n>.command.log), podd's

CONFIG_PATH = Path(os.environ.get("PODD_CONFIG", Path.home() / ".config" / "podd" / "config.json"))
CONFIG_KEYS = ("queue_directories", "max_pods", "idle_minutes", "max_price_per_hour", "cards", "allowed_cuda_versions",
               "cpu_flavors")    # every one the account's own choice: config.example.json shows each
POD_KINDS = ("gpu", "cpu")                      # max_pods caps each kind apart; a set requiring gpu "none" is cpu
CPU_FLAVORS = ("cpu3c", "cpu3g", "cpu3m", "cpu5c", "cpu5g", "cpu5m")   # PodCreateInput.cpuFlavorIds' enum
                                    # (references/runpod/runpod-rest-api-openapi-v1-2026-10-10.json)
CONFIG_DEFAULTS = {"boot_seconds": 150,           # create -> ready, measured 97-155 s (2026-10-13..14): podd opens
                                                  # another pod only when the backlog would outlast a boot
                   "max_wait_for_stock_minutes": 60}  # a set RunPod has no pod for this long FAILS its jobs (podd.py
                                                      # stock_miss): short of stock is waited out, never forever


class JobContractError(ValueError):
    """A JOB file, or podd's config, that breaks the contract; the message lists every problem."""


def utc_now_text():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def acceptable(value):
    """A requirement's acceptable values: a list as given, anything else as the one value."""
    return list(value) if isinstance(value, list) else [value]


# ---------------------------------------------------------------------------------------------------------------------
# the files
# ---------------------------------------------------------------------------------------------------------------------

def write_json_atomically(path, value):
    temporary = Path(path).with_name(Path(path).name + ".writing")
    temporary.write_text(json.dumps(value, indent=1) + "\n")
    os.replace(temporary, path)


def stage_state(stage):
    return next((state for state in STATES if (Path(stage) / state).is_file()), None)


def read_state(stage, state):
    return json.loads((Path(stage) / state).read_text())


def mark_stage_failed(stage, reason, where):
    """FAILED: when, who, why and the traceback being handled (if any); every other state file goes. Said loudly."""
    stage = Path(stage)
    trace = traceback.format_exc()
    (stage / FAILED).write_text(f"{utc_now_text()} ({where})\n{reason}\n\n"
                                f"{trace if trace.strip() != 'NoneType: None' else '(no exception)'}")
    for state in (DONE, RUNNING, JOB):
        (stage / state).unlink(missing_ok=True)
    print(f"{where}: (╯°□°)╯ FAILED {stage / FAILED}: {reason}", flush=True)


# ---------------------------------------------------------------------------------------------------------------------
# the JOB file
# ---------------------------------------------------------------------------------------------------------------------

def stage_relative_path_problem(path):
    if not isinstance(path, str) or not path:
        return f"{path!r} is not a path"
    parts = Path(path).parts
    if Path(path).is_absolute() or ".." in parts:
        return f"{path!r} is not inside the stage (absolute, or ..)"
    if parts[0] in STATES or parts[0] in (".podd", COMMAND_LOG):
        return f"{path!r} is a name podd keeps for itself"
    return None


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def job_problems(job):
    """Every way this JOB breaks the contract ([] when it keeps it)."""
    if not isinstance(job, dict):
        return [f"the JOB is {type(job).__name__}, not a JSON object"]
    problems = [f"no `{key}`" for key in JOB_KEYS if key not in job]
    problems += [f"unknown key `{key}` (the contract: {JOB_KEYS + OPTIONAL_JOB_KEYS})" for key in job
                 if key not in JOB_KEYS + OPTIONAL_JOB_KEYS]
    if "command" in job and (not isinstance(job["command"], str) or not job["command"].strip()):
        problems.append("`command` is not shell text")
    for key in ("inputs", "outputs"):
        if key in job:
            if not isinstance(job[key], list):
                problems.append(f"`{key}` is not a list of paths")
            else:
                problems += [f"`{key}`: {problem}" for problem in map(stage_relative_path_problem, job[key])
                             if problem]
    requires = job.get("requires")
    if "requires" in job:
        if not isinstance(requires, dict):
            problems.append("`requires` is not an object")
        else:
            images = acceptable(requires.get("image"))
            if "image" not in requires or not images or not all(isinstance(i, str) and ":" in i for i in images):
                problems.append("`requires.image` must be an image tag (name:tag) or a non-empty list of them")
            gpu = requires.get("gpu", "any")
            if gpu not in ("any", "none") and not (isinstance(gpu, list) and gpu
                                                   and all(isinstance(g, str) for g in gpu)):
                problems.append("`requires.gpu` must be \"any\", \"none\" (a CPU pod) or a non-empty list of card "
                                "names")
            if gpu == "none" and "gpu_memory_gb_min" in requires:
                problems.append("`requires.gpu_memory_gb_min` with `requires.gpu` \"none\": a CPU pod has no card")
            if gpu == "none" and job.get("gpu_memory_gb", 0) != 0:
                problems.append(f"`gpu_memory_gb` {job.get('gpu_memory_gb')!r} with `requires.gpu` \"none\": a CPU "
                                f"pod has no card (0)")
            for key in ("gpu_memory_gb_min", "cpu_min", "disk_gb_min"):
                if key in requires and not (is_number(requires[key]) and requires[key] > 0):
                    problems.append(f"`requires.{key}` must be a positive number")
            for key, value in requires.items():
                if key not in POD_MEASURED_REQUIREMENTS and acceptable(value) == []:
                    problems.append(f"`requires.{key}` is an empty list: nothing could match")
    if "cpu" in job and job["cpu"] != "all" and not (is_number(job["cpu"]) and job["cpu"] > 0):
        problems.append(f"`cpu` {job['cpu']!r} is neither a positive number of cores nor \"all\"")
    for key in ("gpu_memory_gb", "ram_gb"):
        if key in job and not (is_number(job[key]) and job[key] >= 0):
            problems.append(f"`{key}` {job[key]!r} must be a number >= 0 (decimal GB)")
    if "expected_s" in job and not (is_number(job["expected_s"]) and job["expected_s"] > 0):
        problems.append("`expected_s` must be a positive number of seconds")
    if "progress" in job:
        problem = stage_relative_path_problem(job["progress"])
        if problem:
            problems.append(f"`progress`: {problem}")
    if "resumable" in job:
        if not isinstance(job["resumable"], bool):
            problems.append(f"`resumable` {job['resumable']!r} must be true or false")
        elif job["resumable"] and "progress" not in job:
            problems.append("`resumable` without `progress`: podd could not tell whether an attempt got anywhere")
        elif job["resumable"] and "resume_from" not in job:
            problems.append("`resumable` without `resume_from`: the next attempt would not know what is done (podd "
                            "never ships the outputs back)")
    if "resume_from" in job:
        if job.get("resumable") is not True:
            problems.append("`resume_from` without `resumable` true: only a resumed attempt reads it")
        if not (isinstance(job["resume_from"], list) and job["resume_from"]):
            problems.append("`resume_from` must be a non-empty list of paths inside the outputs")
        else:
            outputs = [o for o in job.get("outputs") or [] if isinstance(o, str)]
            for path in job["resume_from"]:
                problem = stage_relative_path_problem(path)
                if problem:
                    problems.append(f"`resume_from`: {problem}")
                elif not any(path == output or path.startswith(output.rstrip("/") + "/") for output in outputs):
                    problems.append(f"`resume_from`: {path!r} is inside none of the outputs {outputs}")
    if "attempts" in job and not (isinstance(job["attempts"], list) and all(isinstance(a, dict)
                                                                            for a in job["attempts"])):
        problems.append("`attempts` (podd's) must be a list of records")
    return problems


def write_job(stage, job):
    """The requester's one write: JOB, checked against the contract first (a broken job is refused here, loudly,
    not found by podd later), every input present in the stage."""
    stage = Path(stage)
    job = {**job, "queued_at": job.get("queued_at") or utc_now_text()}
    problems = job_problems(job)
    problems += [f"input {path!r} is not in {stage}" for path in job.get("inputs") or []
                 if isinstance(path, str) and not (stage / path).exists()]
    if problems:
        raise JobContractError(f"(╯°□°)╯ {stage}: the JOB breaks the contract (podd.job_contract):\n  - "
                               + "\n  - ".join(problems))
    write_json_atomically(stage / JOB, job)
    return job


def requeue(stage, job, attempt):
    """podd's: a resumable job stopped part-way goes back to JOB with this attempt's record appended to `attempts`
    (JOB first, then RUNNING goes: the stage is never stateless). Said loudly."""
    stage = Path(stage)
    job = {**job, "attempts": list(job.get("attempts") or []) + [attempt]}
    write_json_atomically(stage / JOB, job)
    (stage / RUNNING).unlink(missing_ok=True)
    print(f"podd: (・_・;) REQUEUED {stage} (attempt {len(job['attempts'])} stopped: {attempt.get('why')}); "
          f"what it finished came home and the next attempt does the rest", flush=True)
    return job


# ---------------------------------------------------------------------------------------------------------------------
# podd's config (~/.config/podd/config.json, or $PODD_CONFIG)
# ---------------------------------------------------------------------------------------------------------------------

def read_config(path=None):
    """podd's config, every key checked, CONFIG_DEFAULTS filled in; a requester reads it only to check that podd
    watches its queue."""
    path = Path(path or CONFIG_PATH)
    try:
        config = json.loads(path.read_text())
    except FileNotFoundError:
        raise JobContractError(f"(╯°□°)╯ no podd config {path} (README.md, The config, and config.example.json say what goes in it)")
    except json.JSONDecodeError as error:
        raise JobContractError(f"(╯°□°)╯ podd config {path} is not JSON: {error}")
    config = {**CONFIG_DEFAULTS, **config} if isinstance(config, dict) else config
    problems = [f"no `{key}`" for key in CONFIG_KEYS if key not in config]
    problems += [f"unknown key `{key}`" for key in config if key not in CONFIG_KEYS + tuple(CONFIG_DEFAULTS)]
    if not problems:
        if not (isinstance(config["queue_directories"], list) and config["queue_directories"]
                and all(isinstance(d, str) and Path(d).is_absolute() for d in config["queue_directories"])):
            problems.append("`queue_directories` must be a non-empty list of absolute paths")
        caps = config["max_pods"]
        if not (isinstance(caps, dict) and set(caps) == set(POD_KINDS)
                and all(isinstance(caps[kind], int) and not isinstance(caps[kind], bool) and caps[kind] >= 1
                        for kind in POD_KINDS)):
            problems.append(f"`max_pods` must be {{\"gpu\": n, \"cpu\": n}}, positive integers: GPU and CPU pods are "
                            f"capped apart (2026-10-15: hour-long CPU jobs held all 8 shared slots and starved a "
                            f"GPU sweep); got {caps!r}")
        for key in ("idle_minutes", "max_price_per_hour", "boot_seconds", "max_wait_for_stock_minutes"):
            if not (is_number(config[key]) and config[key] > 0):
                problems.append(f"`{key}` must be a positive number")
        if not (isinstance(config["cards"], list) and config["cards"] and all(
                isinstance(card, dict) and isinstance(card.get("name"), str)
                and isinstance(card.get("memory_gb"), (int, float)) for card in config["cards"])):
            problems.append("`cards` must be a non-empty list of {name, memory_gb}")
        if not (isinstance(config["allowed_cuda_versions"], list) and config["allowed_cuda_versions"]):
            problems.append("`allowed_cuda_versions` must be a non-empty list")
        if not (isinstance(config["cpu_flavors"], list) and config["cpu_flavors"]
                and all(flavor in CPU_FLAVORS for flavor in config["cpu_flavors"])):
            problems.append(f"`cpu_flavors` must be a non-empty list of RunPod CPU flavors {CPU_FLAVORS}")
    if problems:
        raise JobContractError(f"(╯°□°)╯ podd config {path}:\n  - " + "\n  - ".join(problems))
    return config


def require_queue_watched(queue_directory, path=None):
    """A requester's check before it writes a JOB: podd's config lists this queue, or nothing would ever run it."""
    watched = [Path(d).resolve() for d in read_config(path)["queue_directories"]]
    if Path(queue_directory).resolve() not in watched:
        raise JobContractError(f"(╯°□°)╯ podd does not watch {queue_directory}: its config "
                               f"{path or CONFIG_PATH} lists {[str(d) for d in watched]}")
