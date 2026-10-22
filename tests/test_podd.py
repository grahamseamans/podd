"""podd, the pod job runner, and its job contract, each without a pod or RunPod (architecture/README.md): a JOB that breaks the contract is refused with every problem, the config is
checked, a pod satisfies a job only when every requirement is met (the image's declared facts by equality, a list =
any of them), a job goes to the fullest pod that satisfies it, jobs are packed on a pod by cpu / gpu memory / ram (best
fit), pods are opened PER REQUIREMENT SET only while its backlog would outlast a boot (the drain time), offered every
card under the price cap (in-stock first, cheapest first) and never one over it, RunPod short of stock leaves the jobs
queued and retries after a backoff (avoiding no datacenter) until max_wait_for_stock_minutes, a job requiring an image no running pod has opens a pod for that image, a pod's facts are read
from what it prints (CPU quota, decimal GB, shadowing refused), the exit poll ends, a pod's ssh is through its own
alias, a job requiring no card (requires.gpu "none") runs on a CPU pod sized by its jobs' cpu and RAM under the
cap and created through pod.sh create-cpu (its real request body, against a fake curl), an interrupted job brings
its outputs home and a resumable one is requeued to do the rest, a pod much slower than the median of the others (by pace, never by expected_s alone) is marked slow, moved off,
replaced and its datacenter avoided (never while its set is short of stock), a set short of stock fails only after
max_wait_for_stock_minutes with no pod, the deadline
scales with expected_s and the pod's slowness, and podd imports nothing but the standard library.

    PYTHONPATH=src /usr/bin/python3 tests/test_podd.py      (from the repo root; or with podd installed)
"""
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from podd import daemon as podd
from podd import job_contract as podjobs

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIRECTORY = REPOSITORY_ROOT / "src" / "podd"

# never the real state: Pool() reads the slow datacenters, mark_slow writes them
podd.SLOW_DATACENTERS_FILE = Path(tempfile.mkdtemp(prefix="podd-state-")) / "slow-datacenters.json"

IMAGE_A = "ghcr.io/example/solver:" + "a" * 40
IMAGE_B = "ghcr.io/example/solver:" + "b" * 40
FACTS_A = {"pod_name": "podd-g1", "pod_id": "pod-a", "image": IMAGE_A, "image_commit": "a" * 40, "engine": "1.0",
           "solver_version": 1, "gpu": "NVIDIA A40", "gpu_memory_gb": 48.3, "cpu": 7.65, "ram_gb": 50.0}
CARDS = [{"name": "NVIDIA RTX A4500", "memory_gb": 20}, {"name": "NVIDIA RTX A5000", "memory_gb": 24},
         {"name": "NVIDIA A40", "memory_gb": 48}]

OFFERS = {"NVIDIA RTX A4500": {"price_per_hour": 0.25, "stock": {}},                 # pod.sh avail's JSON
          "NVIDIA RTX A5000": {"price_per_hour": 0.27, "stock": {"EU-RO-1": "Low"}},
          "NVIDIA A40": {"price_per_hour": 0.59, "stock": {"CA-MTL-1": "Low"}}}


def a_job(**changes):
    job = {"command": "bash run.sh", "inputs": ["in"], "outputs": ["out"], "cpu": 2.5, "gpu_memory_gb": 1,
           "ram_gb": 2.5, "expected_s": 55,
           "requires": {"image": IMAGE_A, "engine": "1.0", "solver_version": 1}}
    job.update(changes)
    return job


def raises(exception, call, *words):
    try:
        call()
    except exception as error:
        for word in words:
            assert word in str(error), (word, str(error))
        return str(error)
    raise AssertionError(f"not raised: {call}")


# 1. THE CONTRACT
def test_a_job_that_keeps_the_contract_has_no_problems_and_a_broken_one_lists_every_problem():
    assert podjobs.job_problems(a_job()) == []
    assert podjobs.job_problems(a_job(cpu="all", requires={"image": [IMAGE_A, IMAGE_B], "gpu": ["NVIDIA A40"],
                                                             "gpu_memory_gb_min": 20, "cpu_min": 4})) == []
    broken = {"command": "", "inputs": ["../outside", "/abs"], "outputs": ["DONE"], "cpu": 0, "gpu_memory_gb": -1,
              "ram_gb": "lots", "expected_s": 0, "slots": 1, "timeout_s": 0,
              "requires": {"engine": "1.0", "gpu": "A40", "solver_version": []}, "colour": "red"}
    problems = "\n".join(podjobs.job_problems(broken))
    for words in ("`command`", "'../outside'", "'/abs'", "'DONE' is a name podd keeps", "`cpu` 0 is neither",
                  "`gpu_memory_gb` -1", "`ram_gb` 'lots'", "`expected_s`", "unknown key `slots`",
                  "`timeout_s`", "`requires.image`", "`requires.gpu`", "`requires.solver_version` is an empty list",
                  "unknown key `colour`"):
        assert words in problems, (words, problems)


def test_write_job_refuses_a_missing_input_and_writes_the_rest_atomically():
    stage = Path(tempfile.mkdtemp(prefix="podd-stage-"))
    raises(podjobs.JobContractError, lambda: podjobs.write_job(stage, a_job()), "input 'in' is not in")
    (stage / "in").write_text("x")
    written = podjobs.write_job(stage, a_job())
    assert podjobs.stage_state(stage) == podjobs.JOB and written["queued_at"]
    assert podjobs.read_state(stage, podjobs.JOB)["requires"]["engine"] == "1.0"


def test_the_config_is_checked_and_a_requester_learns_whether_its_queue_is_watched():
    directory = Path(tempfile.mkdtemp(prefix="podd-config-"))
    path = directory / "config.json"
    raises(podjobs.JobContractError, lambda: podjobs.read_config(path), "no podd config")
    path.write_text(json.dumps({"queue_directories": ["relative/queue"], "max_pods": 0}))
    message = raises(podjobs.JobContractError, lambda: podjobs.read_config(path), "no `idle_minutes`")
    path.write_text(json.dumps({"queue_directories": [str(directory / "queue")], "max_pods": {"gpu": 2, "cpu": 2}, "idle_minutes": 3,
                                "cards": CARDS, "allowed_cuda_versions": ["12.8"], "cpu_flavors": ["cpu3c"]}))
    raises(podjobs.JobContractError, lambda: podjobs.read_config(path), "no `max_price_per_hour`")
    path.write_text(json.dumps({"queue_directories": [str(directory / "queue")], "max_pods": {"gpu": 2, "cpu": 2}, "idle_minutes": 3,
                                "max_price_per_hour": 0.60, "cards": CARDS, "allowed_cuda_versions": ["12.8"]}))
    raises(podjobs.JobContractError, lambda: podjobs.read_config(path), "no `cpu_flavors`")
    path.write_text(json.dumps({"queue_directories": [str(directory / "queue")], "max_pods": {"gpu": 2, "cpu": 2}, "idle_minutes": 3,
                                "max_price_per_hour": 0.60, "cards": CARDS, "allowed_cuda_versions": ["12.8"],
                                "cpu_flavors": ["cpu4x"]}))
    raises(podjobs.JobContractError, lambda: podjobs.read_config(path), "`cpu_flavors` must be")
    path.write_text(json.dumps({"queue_directories": [str(directory / "queue")], "max_pods": {"gpu": 2, "cpu": 2}, "idle_minutes": 3,
                                "max_price_per_hour": 0.60, "cards": CARDS, "allowed_cuda_versions": ["12.8"],
                                "cpu_flavors": ["cpu3c"]}))
    config = podjobs.read_config(path)
    assert config["max_pods"] == {"gpu": 2, "cpu": 2} and config["max_price_per_hour"] == 0.60 and config["boot_seconds"] == 150, message
    assert config["max_wait_for_stock_minutes"] == 60
    path.write_text(json.dumps({**config, "slots_per_pod": 3}))
    raises(podjobs.JobContractError, lambda: podjobs.read_config(path), "unknown key `slots_per_pod`")
    path.write_text(json.dumps({**config, "max_price_per_hour": 0}))
    raises(podjobs.JobContractError, lambda: podjobs.read_config(path), "`max_price_per_hour` must be a positive")
    path.write_text(json.dumps(config))
    podjobs.require_queue_watched(directory / "queue", path)
    raises(podjobs.JobContractError, lambda: podjobs.require_queue_watched(directory / "other", path),
           "podd does not watch")


# 2. MATCHING
def test_a_pod_satisfies_a_job_only_when_every_requirement_is_met():
    assert podd.unmet_requirements(a_job()["requires"], FACTS_A) == []
    assert podd.unmet_requirements({"image": [IMAGE_B, IMAGE_A], "engine": ["0.9", "1.0"]}, FACTS_A) == []
    for requires, words in (({"image": IMAGE_B}, "image"), ({"image": IMAGE_A, "engine": "2.0"}, "engine '1.0'"),
                            ({"image": IMAGE_A, "flux": 1}, "declares no 'flux'"),
                            ({"image": IMAGE_A, "gpu": ["NVIDIA RTX A5000"]}, "gpu 'NVIDIA A40'"),
                            ({"image": IMAGE_A, "gpu_memory_gb_min": 80}, "gpu memory 48.3 GB < 80"),
                            ({"image": IMAGE_A, "cpu_min": 8}, "cpu 7.65 < 8")):
        unmet = podd.unmet_requirements(requires, FACTS_A)
        assert len(unmet) == 1 and words in unmet[0], (requires, unmet)


def test_the_cards_asked_for_a_set_are_the_configs_order_filtered_by_name_and_memory():
    assert podd.cards_for({"image": IMAGE_A}, CARDS) == ["NVIDIA RTX A4500", "NVIDIA RTX A5000", "NVIDIA A40"]
    assert podd.cards_for({"image": IMAGE_A, "gpu_memory_gb_min": 21}, CARDS) == ["NVIDIA RTX A5000", "NVIDIA A40"]
    assert podd.cards_for({"image": IMAGE_A, "gpu": ["NVIDIA A40", "NVIDIA H100"]}, CARDS) == ["NVIDIA A40"]


# 3. THE POOL (pure): packing, the drain time, the price cap
def free_of(facts):
    return podd.capacity_of(facts)


def test_jobs_are_packed_by_cpu_best_fit_three_on_a_7_65_core_pod_and_four_on_12():
    cuda_jobs = [(f"cuda-{n}", a_job()) for n in range(5)]
    assert [pod for _, pod in podd.place(cuda_jobs, {"podd-g1": (FACTS_A, free_of(FACTS_A))})] == ["podd-g1"] * 3
    twelve = dict(FACTS_A, cpu=12.0)
    assert [pod for _, pod in podd.place(cuda_jobs, {"podd-g1": (twelve, free_of(twelve))})] == ["podd-g1"] * 4
    assert podd.how_many_fit(a_job(), podd.capacity_of(FACTS_A)) == 3
    assert podd.how_many_fit(a_job(), podd.capacity_of(twelve)) == 4
    assert podd.how_many_fit(a_job(), dict(podd.capacity_of(twelve), ram_gb=6)) == 2, "RAM bounds it too"
    half_full = {"cpu": 2.65, "gpu_memory_gb": 47.3, "ram_gb": 47.5}
    placed = podd.place([("one", a_job())], {"podd-g1": (FACTS_A, free_of(FACTS_A)), "podd-g2": (FACTS_A, half_full)})
    assert placed == [("one", "podd-g2")], "best fit: the fullest pod that has room"
    assert podd.place([("big", a_job(gpu_memory_gb=48))], {"podd-g1": (FACTS_A, half_full)}) == [], "card memory"


def test_a_cpu_all_job_runs_alone():
    full = free_of(FACTS_A)
    waiting = [("all", a_job(cpu="all", gpu_memory_gb=0)), ("cuda", a_job()), ("all-2", a_job(cpu="all"))]
    assert podd.place(waiting, {"podd-g1": (FACTS_A, full)}) == [("all", "podd-g1")], "nothing shares it"
    one_running = podd.less(full, podd.demand_of(a_job(), podd.capacity_of(FACTS_A)))
    assert podd.place([("all", a_job(cpu="all"))], {"podd-g1": (FACTS_A, one_running)}) == [], "only an empty pod"
    assert podd.place([("all", a_job(cpu="all")), ("cuda", a_job())], {"podd-g1": (FACTS_A, one_running)}) == \
        [("cuda", "podd-g1")], "the job behind it still goes"
    assert podd.how_many_fit(a_job(cpu="all"), podd.capacity_of(FACTS_A)) == 1


def test_the_drain_time_opens_a_pod_only_when_the_backlog_outlasts_a_boot():
    capacity = podd.capacity_of(FACTS_A)                                    # 3 cuda jobs at once
    six = [(a_job(), 55)] * 6
    assert abs(podd.drain_seconds(six, [(capacity, False)]) - 110) < 1e-9
    assert podd.pods_to_open(six, [(capacity, False)], capacity, room=7, boot_seconds=150) == 0, "110 s < 150"
    sixty = [(a_job(), 55)] * 60
    assert abs(podd.drain_seconds(sixty, [(capacity, False)]) - 1100) < 1e-6
    assert podd.pods_to_open(sixty, [(capacity, False)], capacity, room=7, boot_seconds=150) == 7, "1100/8 = 137.5"
    assert podd.pods_to_open(sixty, [(capacity, False)], capacity, room=2, boot_seconds=150) == 2, "never past max_pods"
    assert podd.drain_seconds(six, []) == float("inf")
    assert podd.pods_to_open([(a_job(), 55)] * 12, [], capacity, room=8, boot_seconds=150) == 2, "220 s, then 110"
    assert podd.pods_to_open([(a_job(), 55)] * 6, [], capacity, room=8, boot_seconds=150) == 1, "a cold pool"
    assert podd.pods_to_open([], [], capacity, room=8, boot_seconds=150) == 0
    long_one = [(a_job(cpu="all"), 400)]                                     # 2026-10-15: one job opened 3 pods
    assert podd.pods_to_open(long_one, [], capacity, room=8, boot_seconds=150) == 1, "one job, one pod"
    assert podd.pods_to_open(long_one * 2, [], capacity, room=8, boot_seconds=150) == 2
    assert podd.pods_to_open(long_one, [(capacity, True)], capacity, room=8, boot_seconds=150) == 0
    huge = dict(capacity, gpu_memory_gb=0.5)
    assert podd.drain_seconds([(a_job(), 55)], [(huge, True)]) == 55, "a booting pod counts for one at least"
    assert podd.drain_seconds([(a_job(), 55)], [(huge, False)]) == float("inf")


def test_create_is_offered_every_card_under_the_cap_in_stock_first_cheapest_first_and_none_over_it():
    """2026-10-14 10:01: gated on the stock listing, create was offered the 3090 alone, and RunPod had none."""
    offers = {"NVIDIA L40S": {"price_per_hour": 1.09, "stock": {"OC-AU-1": "Low"}},
              "NVIDIA A40": {"price_per_hour": 0.59, "stock": {"CA-MTL-1": "Low"}},
              "NVIDIA GeForce RTX 3090": {"price_per_hour": 0.50, "stock": {"US-TX-3": "Low"}},
              "NVIDIA RTX A5000": {"price_per_hour": 0.27, "stock": {}},
              "NVIDIA RTX A4500": {"price_per_hour": 0.25, "stock": {}}, "NVIDIA L4": None}
    wanted = ["NVIDIA RTX A4500", "NVIDIA L40S", "NVIDIA A40", "NVIDIA RTX A5000", "NVIDIA GeForce RTX 3090",
              "NVIDIA L4"]
    assert podd.affordable_cards(wanted, offers, 0.60) == ["NVIDIA GeForce RTX 3090", "NVIDIA A40",
                                                           "NVIDIA RTX A4500", "NVIDIA RTX A5000"]
    assert podd.affordable_cards(wanted, offers, 2.00)[:3] == ["NVIDIA GeForce RTX 3090", "NVIDIA A40", "NVIDIA L40S"]
    assert podd.affordable_cards(["NVIDIA L40S", "NVIDIA L4"], offers, 0.60) == [], "over the cap, or not priced"


def test_a_pool_whose_only_card_is_over_the_cap_opens_nothing_and_says_so():
    root = Path(tempfile.mkdtemp(prefix="podd-pool-"))
    queue_directory, secrets = root / "queue", root / "secrets"
    (secrets / podd.PODS_DIRECTORY_NAME).mkdir(parents=True)
    for n in range(12):
        stage = queue_directory / f"cuda-{n}"
        stage.mkdir(parents=True)
        (stage / "in").write_text("x")
        podjobs.write_job(stage, a_job())
    config = {"queue_directories": [str(queue_directory)], "max_pods": {"gpu": 4, "cpu": 4}, "idle_minutes": 3,
              "max_price_per_hour": 0.60, "boot_seconds": 150, "max_wait_for_stock_minutes": 60, "cards": CARDS + [{"name": "NVIDIA L40S",
                                                                                  "memory_gb": 48}],
              "allowed_cuda_versions": ["12.8"]}
    pool = podd.Pool(secrets, config)
    pool.offers = (time.time(), {"NVIDIA L40S": {"price_per_hour": 1.09, "stock": {"OC-AU-1": "Low"}},
                                 **{card["name"]: {"price_per_hour": 0.90, "stock": {}} for card in CARDS}})
    saved = podd.Pod, podd.STATUS_FILE
    podd.Pod, podd.STATUS_FILE = PodThatDoesNotBoot, root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            pool.step()
            pool.step()
        assert not pool.pods and "PRICE CAP" in said.getvalue() and "NVIDIA L40S" in said.getvalue(), said.getvalue()
        assert said.getvalue().count("PRICE CAP") == 1, "said once per backoff, not every pass"
        assert all(podjobs.stage_state(stage) == podjobs.JOB for stage in queue_directory.iterdir()), "they wait"
        pool.offers[1]["NVIDIA A40"]["price_per_hour"] = 0.59
        next(iter(pool.short_of_stock.values())).retry_at_s = 0          # the backoff has passed
        with contextlib.redirect_stdout(io.StringIO()) as said:
            pool.step()
    finally:
        podd.Pod, podd.STATUS_FILE = saved
    assert sorted(pool.pods) == ["podd-g1", "podd-g2"], (sorted(pool.pods), said.getvalue())  # 12 x 55 s: 220 s, 110 s
    assert all(pod.create_command()[4] == "NVIDIA A40" for pod in pool.pods.values())


def test_a_pod_booting_for_a_stricter_set_with_an_acceptable_image_serves_a_looser_one():
    strict = {"image": IMAGE_A, "engine": "1.0", "solver_version": 1, "gpu_memory_gb_min": 20}
    assert podd.booting_pod_serves(strict, strict, CARDS)
    assert podd.booting_pod_serves(strict, {"image": IMAGE_A}, CARDS), "image only: the strict pod will do"
    assert podd.booting_pod_serves(strict, {"image": [IMAGE_B, IMAGE_A], "engine": ["1.0", "1.1"]}, CARDS)
    assert not podd.booting_pod_serves({"image": IMAGE_A}, strict, CARDS), "its engine is not known before it boots"
    assert not podd.booting_pod_serves(strict, {"image": IMAGE_B}, CARDS)
    assert not podd.booting_pod_serves(strict, dict(strict, gpu_memory_gb_min=40), CARDS)
    assert not podd.booting_pod_serves(strict, {"image": IMAGE_A, "gpu": ["NVIDIA A40"]}, CARDS), "it may land on an A4500"
    assert podd.booting_pod_serves(dict(strict, gpu=["NVIDIA A40"]), {"image": IMAGE_A, "gpu": ["NVIDIA A40"]}, CARDS)


def test_a_looser_job_does_not_open_a_pod_while_a_pod_that_serves_it_boots():
    """The 2026-10-14 measurement opened podd-4 for an image-only job while podd-2, of that image, booted for the
    first client's set; podd-2 then ran the job and podd-4 idled to its delete."""
    root = Path(tempfile.mkdtemp(prefix="podd-pool-"))
    queue_directory, secrets = root / "queue", root / "secrets"
    (secrets / podd.PODS_DIRECTORY_NAME).mkdir(parents=True)
    stage = queue_directory / "image-only"
    stage.mkdir(parents=True)
    (stage / "in").write_text("x")
    podjobs.write_job(stage, a_job(requires={"image": IMAGE_A}))
    config = {"queue_directories": [str(queue_directory)], "max_pods": {"gpu": 4, "cpu": 4}, "idle_minutes": 3,
              "max_price_per_hour": 0.60, "boot_seconds": 150, "max_wait_for_stock_minutes": 60, "cards": CARDS, "allowed_cuda_versions": ["12.8"]}
    pool = podd.Pool(secrets, config)
    pool.offers = (time.time(), json.loads(json.dumps(OFFERS)))      # never pod.sh avail in a test
    pool.pods["podd-g1"] = podd.Pod("podd-g1", secrets, a_job()["requires"], config)       # booting, stricter set
    saved = podd.Pod, podd.STATUS_FILE
    podd.Pod, podd.STATUS_FILE = PodThatDoesNotBoot, root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            pool.step()
    finally:
        podd.Pod, podd.STATUS_FILE = saved
    assert list(pool.pods) == ["podd-g1"], (list(pool.pods), said.getvalue())


# 4. THE MEASUREMENT'S (b), without a pod: a job requiring an image no running pod has makes the pool open a pod
# FOR that image, while the running pod's job is not disturbed (the real run is in the plan's Results)
class PodThatDoesNotBoot(podd.Pod):
    def start_booting(self):
        self.started = True


def test_a_job_requiring_an_image_no_running_pod_has_opens_a_pod_for_it():
    root = Path(tempfile.mkdtemp(prefix="podd-pool-"))
    queue_directory, secrets = root / "queue", root / "secrets"
    (secrets / podd.PODS_DIRECTORY_NAME).mkdir(parents=True)
    (secrets / podd.PODS_DIRECTORY_NAME / "podd-g1.id").write_text("pod-a\n")
    stage = queue_directory / "wants-b"
    stage.mkdir(parents=True)
    (stage / "in").write_text("x")
    podjobs.write_job(stage, a_job(requires={"image": IMAGE_B, "engine": "1.0", "solver_version": 1}))
    config = {"queue_directories": [str(queue_directory)], "max_pods": {"gpu": 4, "cpu": 4}, "idle_minutes": 3,
              "max_price_per_hour": 0.60, "boot_seconds": 150, "max_wait_for_stock_minutes": 60, "cards": CARDS, "allowed_cuda_versions": ["12.8"]}
    pool = podd.Pool(secrets, config)
    pool.offers = (time.time(), json.loads(json.dumps(OFFERS)))      # never pod.sh avail in a test
    running_pod = podd.Pod("podd-g1", secrets, a_job()["requires"], config)
    running_pod.state, running_pod.facts = "ready", FACTS_A
    pool.pods["podd-g1"] = running_pod
    saved = podd.Pod, podd.STATUS_FILE
    podd.Pod, podd.STATUS_FILE = PodThatDoesNotBoot, root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            pool.step()
    finally:
        podd.Pod, podd.STATUS_FILE = saved
    opened = [pod for name, pod in pool.pods.items() if name != "podd-g1"]
    assert len(opened) == 1 and opened[0].name == "podd-g2" and opened[0].started, (pool.pods, said.getvalue())
    assert opened[0].requires["image"] == IMAGE_B and opened[0].create_command()[3] == IMAGE_B
    assert opened[0].create_command()[4:] == ["NVIDIA RTX A5000,NVIDIA A40,NVIDIA RTX A4500", "1", "12.8"], \
        "every affordable card, in stock first, cheapest first"
    assert podjobs.stage_state(stage) == podjobs.JOB, "it waits for its pod; the running pod does not take it"
    assert "opening a pod for requirement set" in said.getvalue() and IMAGE_B in said.getvalue()
    assert json.loads((root / "PODS.json").read_text())["pods"][1]["requires"]["image"] == IMAGE_B


def test_a_pod_whose_facts_do_not_satisfy_its_set_fails_that_sets_jobs_and_is_never_reopened():
    root = Path(tempfile.mkdtemp(prefix="podd-pool-"))
    queue_directory, secrets = root / "queue", root / "secrets"
    (secrets / podd.PODS_DIRECTORY_NAME).mkdir(parents=True)
    stage = queue_directory / "wants-engine-2"
    stage.mkdir(parents=True)
    (stage / "in").write_text("x")
    podjobs.write_job(stage, a_job(requires={"image": IMAGE_A, "engine": "2.0"}))
    config = {"queue_directories": [str(queue_directory)], "max_pods": {"gpu": 4, "cpu": 4}, "idle_minutes": 3,
              "max_price_per_hour": 0.60, "boot_seconds": 150, "max_wait_for_stock_minutes": 60, "cards": CARDS, "allowed_cuda_versions": ["12.8"]}
    pool = podd.Pool(secrets, config)
    pool.offers = (time.time(), json.loads(json.dumps(OFFERS)))      # never pod.sh avail in a test
    mismatched = podd.Pod("podd-g1", secrets, {"image": IMAGE_A, "engine": "2.0"}, config)
    mismatched.state, mismatched.facts = "mismatch", FACTS_A
    mismatched.problem = "; ".join(podd.unmet_requirements(mismatched.requires, FACTS_A))
    pool.pods["podd-g1"] = mismatched
    saved = podd.Pod, podd.STATUS_FILE
    podd.Pod, podd.STATUS_FILE = PodThatDoesNotBoot, root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            pool.step()
            assert podjobs.stage_state(stage) == podjobs.FAILED and not pool.pods, pool.pods
            assert "engine '1.0' is not one of ['2.0']" in (stage / podjobs.FAILED).read_text()
            later = queue_directory / "later"
            later.mkdir()
            (later / "in").write_text("x")
            podjobs.write_job(later, a_job(requires={"image": IMAGE_A, "engine": "2.0"}))
            pool.step()
    finally:
        podd.Pod, podd.STATUS_FILE = saved
    assert podjobs.stage_state(later) == podjobs.FAILED and not pool.pods, "the same set never opens another pod"


# 4b. SHORT OF STOCK IS NOT A FAILURE (2026-10-14 10:01: six cpu "all" jobs FAILED because RunPod answered "no
# instances currently available" to four creates at once)
def stock_pool(root, jobs=6, **config_changes):
    queue_directory, secrets = root / "queue", root / "secrets"
    (secrets / podd.PODS_DIRECTORY_NAME).mkdir(parents=True)
    for n in range(jobs):
        stage = queue_directory / f"twin-{n}"
        stage.mkdir(parents=True)
        (stage / "in").write_text("x")
        podjobs.write_job(stage, a_job(cpu="all", expected_s=75))
    config = {"queue_directories": [str(queue_directory)], "max_pods": {"gpu": 8, "cpu": 8}, "idle_minutes": 3,
              "max_price_per_hour": 0.60, "boot_seconds": 150, "max_wait_for_stock_minutes": 60, "cards": CARDS,
              "allowed_cuda_versions": ["12.8"], **config_changes}
    pool = podd.Pool(secrets, config)
    pool.offers = (time.time(), json.loads(json.dumps(OFFERS)))      # never pod.sh avail in a test
    return pool, queue_directory


def refused_for_stock(pool):
    for pod in pool.pods.values():
        pod.state, pod.problem = "no stock", ('RunPod POST /pods -> HTTP 500: {"error":"create pod: There are no '
                                              'instances currently available","status":500}')


def test_no_instances_leaves_the_jobs_queued_and_retries_one_pod_with_every_card_after_a_backoff():
    root = Path(tempfile.mkdtemp(prefix="podd-stock-"))
    pool, queue_directory = stock_pool(root)
    saved = podd.Pod, podd.STATUS_FILE
    podd.Pod, podd.STATUS_FILE = PodThatDoesNotBoot, root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            pool.step()
            assert len(pool.pods) == 3, (sorted(pool.pods), said.getvalue())     # 6 alone x 75 s: a burst is fine
            refused_for_stock(pool)
            earlier = podd.Pod("podd-g8", pool.secrets, next(iter(pool.pods.values())).requires, pool.config,
                               attempt=0)               # a pod RunPod made before the miss, still up
            earlier.created, earlier.state, earlier.facts = True, "ready", dict(FACTS_A, image=IMAGE_B)  # runs none
            pool.pods["podd-g8"] = earlier
            (pool.secrets / podd.PODS_DIRECTORY_NAME / "podd-g8.id").write_text("pod-a\n")
            pool.step()
            assert pool.short_of_stock, "2026-10-15: a pod made BEFORE the miss does not end the wait"
            del pool.pods["podd-g8"]
            (pool.secrets / podd.PODS_DIRECTORY_NAME / "podd-g8.id").unlink()
            assert not pool.pods and said.getvalue().count("NO STOCK") == 1, "four misses of one attempt: said once"
            assert all(podjobs.stage_state(stage) == podjobs.JOB for stage in queue_directory.iterdir())
            wait = next(iter(pool.short_of_stock.values()))
            assert wait.misses == 1 and 55 < wait.retry_at_s - time.time() <= 60, wait.retry_at_s - time.time()
            pool.step()
            assert not pool.pods, "backing off: no create before retry_at"
            wait.retry_at_s = 0
            pool.step()
            assert len(pool.pods) == 1, "after a refusal: one probe, not a burst"
            probe = next(iter(pool.pods.values()))
            assert probe.create_command()[4] == "NVIDIA RTX A5000,NVIDIA A40,NVIDIA RTX A4500", "every card"
            pool.step()
            assert len(pool.pods) == 1, "nothing more while the probe is out"
            refused_for_stock(pool)
            pool.step()
            assert wait.misses == 2 and 115 < wait.retry_at_s - time.time() <= 120, "the backoff doubles"
            assert podd.stock_backoff_seconds(10) == podd.STOCK_RETRY_MAX_SECONDS
            wait.retry_at_s = 0
            pool.step()
            probe = next(iter(pool.pods.values()))
            probe.created, probe.state, probe.facts = True, "ready", FACTS_A  # RunPod had one (create returns at ssh)
            pool.step()
            assert not pool.short_of_stock, said.getvalue()[-400:]
        assert all(podjobs.stage_state(stage) == podjobs.JOB for stage in queue_directory.iterdir())
    finally:
        podd.Pod, podd.STATUS_FILE = saved


def test_a_set_short_of_stock_past_max_wait_fails_its_jobs_with_every_miss():
    root = Path(tempfile.mkdtemp(prefix="podd-stock-"))
    pool, queue_directory = stock_pool(root, jobs=2)
    saved = podd.Pod, podd.STATUS_FILE
    podd.Pod, podd.STATUS_FILE = PodThatDoesNotBoot, root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            pool.step()
            refused_for_stock(pool)
            pool.step()
            assert all(podjobs.stage_state(stage) == podjobs.JOB for stage in queue_directory.iterdir())
            next(iter(pool.short_of_stock.values())).podless_since_s -= 61 * 60
            pool.step()
    finally:
        podd.Pod, podd.STATUS_FILE = saved
    for stage in queue_directory.iterdir():
        assert podjobs.stage_state(stage) == podjobs.FAILED
        reason = (stage / podjobs.FAILED).read_text()
        assert "max_wait_for_stock_minutes" in reason and "no instances currently available" in reason, reason
        assert "NVIDIA RTX A5000, NVIDIA A40, NVIDIA RTX A4500" in reason, "the cards offered are in the story"
    assert not pool.short_of_stock


def test_a_set_short_of_stock_never_fails_while_a_pod_of_it_runs_and_its_clock_starts_when_none_does():
    """2026-10-22 00:15Z: 20 jobs FAILED "no pod for 60 min" while 3 pods of their set ran other chunks."""
    root = Path(tempfile.mkdtemp(prefix="podd-stock-"))
    pool, queue_directory = stock_pool(root, jobs=2)
    saved = podd.Pod, podd.STATUS_FILE
    podd.Pod, podd.STATUS_FILE = PodThatDoesNotBoot, root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            pool.step()
            refused_for_stock(pool)
            pool.step()
            wait = next(iter(pool.short_of_stock.values()))
            key = next(iter(pool.short_of_stock))
            working = podd.Pod("podd-g8", pool.secrets, json.loads(key), pool.config, attempt=0)
            working.created, working.state, working.facts = True, "ready", dict(FACTS_A, pod_name="podd-g8")
            pool.pods["podd-g8"] = working
            (pool.secrets / podd.PODS_DIRECTORY_NAME / "podd-g8.id").write_text("pod-a\n")
            busy = on_pod(pool, "other-chunk", "podd-g8", a_job(cpu="all", expected_s=75, progress="progress"), 1.0)
            wait.first_miss_s -= 3 * 3600
            wait.podless_since_s -= 3 * 3600                    # hours short of stock: and a pod of the set runs
            wait.retry_at_s = time.time() + 600
            pool.step()
            assert all(podjobs.stage_state(stage) == podjobs.JOB for stage in queue_directory.iterdir()), \
                said.getvalue()[-600:]
            assert wait.podless_since_s is None and pool.short_of_stock, "the set has a pod: the clock is off"
            status = json.loads((root / "PODS.json").read_text())["short_of_stock"]
            assert status[key]["no_pod_since"] is None and status[key]["misses"] == 1, status
            del pool.running["/q/other-chunk"], pool.pods["podd-g8"]       # its last pod went
            (pool.secrets / podd.PODS_DIRECTORY_NAME / "podd-g8.id").unlink()
            pool.step()
            assert wait.podless_since_s is not None and "now with no pod" in said.getvalue()
            assert all(podjobs.stage_state(stage) == podjobs.JOB for stage in queue_directory.iterdir())
            assert json.loads((root / "PODS.json").read_text())["short_of_stock"][key]["fails_at"]
            wait.podless_since_s -= 61 * 60
            pool.step()
    finally:
        podd.Pod, podd.STATUS_FILE = saved
    assert busy.stop is None
    assert all(podjobs.stage_state(stage) == podjobs.FAILED for stage in queue_directory.iterdir()), \
        "no pod for max_wait_for_stock_minutes: the README's give-up"


def test_a_set_short_of_stock_avoids_no_datacenter_and_a_cpu_ban_never_reaches_a_gpu_create():
    """2026-10-21: 15 datacenters avoided (US-GA-2, the one with CPU stock, among them): RunPod refused every create,
    and the CPU pods' bans cut the GPU creates too."""
    root = Path(tempfile.mkdtemp(prefix="podd-stock-"))
    pool, _ = stock_pool(root, jobs=1)
    record = {"until_unix": time.time() + 3600, "until": "later", "why": "test", "pod": "podd-c1"}
    pool.slow_datacenters = {"cpu": {"US-GA-2": dict(record)}, "gpu": {"US-MD-1": dict(record)}}
    saved = podd.Pod, podd.STATUS_FILE
    podd.Pod, podd.STATUS_FILE = PodThatDoesNotBoot, root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            pool.step()
            first = next(iter(pool.pods.values()))
            assert first.create_command()[-1] == "US-MD-1", "a GPU create avoids the GPU bans only"
            refused_for_stock(pool)
            pool.step()
            next(iter(pool.short_of_stock.values())).retry_at_s = 0
            pool.step()
            probe = next(iter(pool.pods.values()))
    finally:
        podd.Pod, podd.STATUS_FILE = saved
    assert probe.avoid_datacenters == [] and len(probe.create_command()) == 7, probe.create_command()
    assert "avoids no datacenter" in said.getvalue(), said.getvalue()[-600:]


def test_pod_sh_create_exiting_75_is_a_stock_miss_with_runpods_answer_not_a_boot_failure():
    root = Path(tempfile.mkdtemp(prefix="podd-stock-"))
    fake = root / "pod.sh"
    fake.write_text("#!/usr/bin/env bash\necho 'RunPod POST /pods -> HTTP 500: {\"error\":\"create pod: There are "
                    "no instances currently available\"}' >&2\nexit 75\n")
    fake.chmod(0o755)
    pod = podd.Pod("podd-g1", root, a_job()["requires"], {"allowed_cuda_versions": ["12.8"]}, ["NVIDIA A40"])
    saved = podd.POD_SCRIPT
    podd.POD_SCRIPT = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            pod.boot()
    finally:
        podd.POD_SCRIPT = saved
    assert pod.state == "no stock" and "no instances currently available" in pod.problem, (pod.state, pod.problem)
    assert "exit 75" in (PACKAGE_DIRECTORY / "pod.sh").read_text() and '*"no instances"*' in \
        (PACKAGE_DIRECTORY / "pod.sh").read_text(), "pod.sh maps RunPod's no-instances answer to exit 75"


# 5. A POD'S FACTS, from what it printed
def test_a_pods_facts_are_read_from_what_it_prints():
    facts = podd.facts_of_pod('{"engine": "1.0", "solver_version": 1}', "NVIDIA RTX A4500, 20470\n",
                              "765000 100000\nnone\n96\n", "53687091200\nMemTotal:  1056000000 kB\n", IMAGE_A, {"cost_per_hr": 0.25, "datacenter": "EU-RO-1"},
                              "podd-g3", "pod-x", "now")
    assert facts["gpu"] == "NVIDIA RTX A4500" and facts["gpu_memory_gb"] == 21.46 and facts["cpu"] == 7.65, facts
    assert facts["engine"] == "1.0" and facts["image"] == IMAGE_A and facts["cost_per_hr"] == 0.25
    assert facts["ram_gb"] == 53.69, "cgroup memory.max, decimal GB"
    unlimited = podd.facts_of_pod("{}", "X, 1024", "max 100000\nnone\n12\n", "max\nMemTotal:  32000000 kB\n", IMAGE_A, {},
                                  "p", "i", "now")
    assert unlimited["cpu"] == 12.0 and unlimited["ram_gb"] == 32.77, unlimited
    on_runpod = podd.facts_of_pod("{}", "X, 1024", "max 100000\nnone\n12\n", "max\nMemTotal:  540640000 kB\n", IMAGE_A,
                                  {"memory_gb": 50}, "p", "i", "now")
    assert on_runpod["ram_gb"] == 50, "no memory limit: RunPod's memoryInGb, not the host's 540 GB"
    raises(podd.PodError, lambda: podd.facts_of_pod('{"gpu": "fake"}', "X, 1", "max 1\n1\n", "1\n", IMAGE_A, {}, "p",
                                                    "i", "now"), "podd measures itself")
    raises(podd.PodError, lambda: podd.facts_of_pod("not json", "X, 1", "max 1\n1\n", "1\n", IMAGE_A, {}, "p", "i",
                                                    "now"), "is not JSON")


# 6. THE EXIT POLL ends (podd's, per job)
class FakeClock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


def reads(*results):
    queue = list(results)
    return lambda: queue.pop(0) if queue else subprocess.CompletedProcess([], 0, "", "")


def ok(text):
    return subprocess.CompletedProcess([], 0, text, "")


def ssh_failed(text="ssh: connect to host 1.2.3.4 port 22: Connection refused"):
    return subprocess.CompletedProcess([], 255, "", text)


def a_running(stage, job=None, pod_name="podd-c1"):
    return podd.Running(None, pod_name, {"cpu": 2.5, "gpu_memory_gb": 1, "ram_gb": 2.5}, job or a_job(), time.time(),
                        f"{stage}#1")


def test_the_poll_returns_the_exit_and_forgives_a_lone_ssh_failure():
    clock = FakeClock()
    with contextlib.redirect_stdout(io.StringIO()) as printed:
        exited = podd.wait_for_the_exit(reads(ok(""), ssh_failed(), ok(""), ssh_failed(),
                                              ok('{"exit_status": 0, "wall_s": 50.1}\n')), deadline_s=1800,
                                        sleep=clock.sleep, clock=clock)
    assert exited == {"exit_status": 0, "wall_s": 50.1}
    assert printed.getvalue().count("reading the job's exit failed (1/3") == 2, printed.getvalue()


def test_three_ssh_failures_in_a_row_fail_the_job_with_the_last_error():
    clock = FakeClock()
    with contextlib.redirect_stdout(io.StringIO()):
        raises(podd.PodError, lambda: podd.wait_for_the_exit(reads(ssh_failed("a"), ssh_failed("b"),
                                                                   ssh_failed("the last")), deadline_s=1800,
                                                             sleep=clock.sleep, clock=clock),
               "3 failed reads", "the last")


def test_podd_times_the_job_out_at_its_timeout_and_the_pod_side_has_no_timeout():
    clock = FakeClock()
    with contextlib.redirect_stdout(io.StringIO()):
        raises(podd.JobTimedOut, lambda: podd.wait_for_the_exit(reads(), deadline_s=90, sleep=clock.sleep,
                                                                clock=clock), "no exit 90 s after it started")
    assert clock.now <= 90 + podd.POLL_SECONDS
    runner = (PACKAGE_DIRECTORY / "podd-run.sh").read_text()
    code = "\n".join(line for line in runner.splitlines() if not line.lstrip().startswith("#"))
    for lifetime_word in ("pkill", "timeout", "TIMEOUT"):
        assert lifetime_word not in code, f"podd-run.sh manages a lifetime: {lifetime_word}"
    assert ".podd/pid" in code and ".podd/exit" in code


def test_a_job_that_times_out_still_brings_its_logs_home():
    """2026-10-15: four jobs timed out at 6 h and left nothing to say why: the timeout now pulls what exists."""
    stage = Path(tempfile.mkdtemp(prefix="podd-timeout-"))
    (stage / "in").write_text("x")
    podjobs.write_job(stage, a_job())
    os.rename(stage / podjobs.JOB, stage / podjobs.RUNNING)
    pulled = []

    def fake_pull(pod, remote, at, job):
        pulled.append(remote)
        (at / podjobs.COMMAND_LOG).write_text("unit 57 of 116, 190 s each\n")
        return ["raw"]

    def timed_out(*args, **kwargs):
        raise podd.JobTimedOut("timed out: no exit 220 s after it started")

    pod = podd.Pod("podd-c1", Path(tempfile.mkdtemp(prefix="secrets-")))
    pod.state, pod.facts = "ready", FACTS_A
    pod.ssh = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", "")
    saved = podd.pull_back, podd.wait_for_the_exit, podd.kill_the_job, podd.transfer
    saved_pull = podd.pull_back
    podd.pull_back, podd.wait_for_the_exit = fake_pull, timed_out
    podd.kill_the_job, podd.transfer = (lambda pod, remote: None), (lambda command: None)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            podd.run_the_job(stage, a_job(), pod, a_running(stage))
    finally:
        podd.pull_back, podd.wait_for_the_exit, podd.kill_the_job, podd.transfer = saved
    reason = (stage / podjobs.FAILED).read_text()
    assert pulled and "pulled back what existed" in reason and "unit 57 of 116" in reason, reason
    assert pod.state == "ready", "a job's timeout is the job's, not the pod's"
    commands = []
    pod.ssh = lambda command, **kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0, "", "")
    saved = podd.wait_for_the_exit, podd.kill_the_job, podd.transfer
    podd.wait_for_the_exit, podd.kill_the_job, podd.transfer = timed_out, (lambda pod, r: None), (lambda c: None)
    podd.pull_back = fake_pull
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            (stage / podjobs.FAILED).unlink()
            podd.run_the_job(stage, a_job(), pod, a_running(stage))
    finally:
        podd.wait_for_the_exit, podd.kill_the_job, podd.transfer = saved
        podd.pull_back = saved_pull
    remote = podd.remote_directory(stage)
    assert commands[0] == f"rm -rf {remote} && mkdir -p {remote}/.podd", "a job starts in a clean directory"
    assert f"rm -rf {remote}" in commands[-1], "a failed job's directory goes once its logs came home"


# 7. a pod's ssh goes through ITS alias, through the REAL Pod.ssh (only subprocess.run is stood in: 2026-10-10 the
# first stamped launch failed in a doubled `check`, which a stubbed helper would not have seen)
def test_a_pods_ssh_is_through_its_own_alias_and_fails_loudly():
    calls = []
    saved = podd.subprocess.run
    try:
        podd.subprocess.run = lambda command, **options: calls.append((command, options)) or \
            subprocess.CompletedProcess(command, 255, "", "Connection refused")
        pod = podd.Pod("podd-g2", Path(tempfile.mkdtemp(prefix="secrets-test-")))
        raises(podd.PodError, lambda: pod.ssh("true"), "runpod-podd-g2", "Connection refused")
        assert pod.ssh("cat > x", check=False, input_text="echo hi\n").returncode == 255
    finally:
        podd.subprocess.run = saved
    assert calls[0][0][:2] == ["ssh", "runpod-podd-g2"], calls
    assert calls[1][1]["input"] == "echo hi\n" and calls[1][1]["stdin"] is None, "the command text goes on stdin"


def test_two_queues_stages_of_one_name_get_two_directories_on_a_pod():
    first, second = Path("/q/one/stage-1"), Path("/q/two/stage-1")
    assert podd.remote_directory(first) != podd.remote_directory(second)
    assert podd.remote_directory(first).endswith("-stage-1") and podd.remote_directory(first).startswith("/workspace")


# 7b. every pod.sh command reaches its own arm (2026-10-14: an apostrophe in avail's usage text opened a quote that
# swallowed the create arm; `bash -n` passed, and every create printed the usage and exited 1). No API call: each is
# asked without a name, which its arm refuses before anything else.
def test_every_pod_sh_command_reaches_its_own_arm():
    for command in ("create", "create-cpu", "status", "delete", "id", "ssh-config"):
        refused = subprocess.run([str(podd.POD_SCRIPT), command], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL)
        assert refused.returncode == 1 and "a pod name ([a-z0-9-]+) is required" in refused.stderr, \
            (command, refused.stdout[:200], refused.stderr[:200])


# 7b2. a failed delete is said and retried, never podd's end (2026-10-15: RunPod's DELETE answered HTTP 500 "unexpected
# end of JSON input"; podd stopped, failing two running solves on other pods and leaving two pods billing)
def test_a_failed_delete_is_retried_and_does_not_stop_podd():
    root = Path(tempfile.mkdtemp(prefix="podd-delete-"))
    secrets = root / "secrets"
    (secrets / podd.PODS_DIRECTORY_NAME).mkdir(parents=True)
    (secrets / podd.PODS_DIRECTORY_NAME / "podd-g1.id").write_text("pod-a\n")
    answers = root / "answers"
    answers.write_text("1\n0\n")                                # the first delete fails, the retry succeeds
    fake = root / "pod.sh"
    fake.write_text(f"#!/usr/bin/env bash\nstatus=$(head -1 {answers}); sed -i 1d {answers}\n"
                    "echo 'RunPod DELETE /pods/pod-a -> HTTP 500: unexpected end of JSON input' >&2\nexit $status\n")
    fake.chmod(0o755)
    config = {"queue_directories": [str(root / "queue")], "max_pods": {"gpu": 2, "cpu": 2}, "idle_minutes": 3,
              "max_price_per_hour": 0.60, "boot_seconds": 150, "max_wait_for_stock_minutes": 60, "cards": CARDS,
              "allowed_cuda_versions": ["12.8"], "cpu_flavors": ["cpu3c"]}
    pool = podd.Pool(secrets, config)
    pod = podd.Pod("podd-g1", secrets, a_job()["requires"], config)
    pool.pods["podd-g1"] = pod
    saved = podd.POD_SCRIPT, podd.STATUS_FILE
    podd.POD_SCRIPT, podd.STATUS_FILE = fake, root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said, contextlib.redirect_stderr(io.StringIO()):
            pool.forget(pod, "idle")
            assert "podd-g1" in pool.undeleted and "retries the delete" in said.getvalue(), said.getvalue()
            assert pool.free_name("gpu") == "podd-g2", "a pod still recorded keeps its name"
            pool.undeleted["podd-g1"] = (pod, "idle", 0.0)              # its retry is due
            pool.step()
    finally:
        podd.POD_SCRIPT, podd.STATUS_FILE = saved
    assert not pool.undeleted and answers.read_text() == "", (pool.undeleted, answers.read_text())


# 7c. CPU PODS: a set requiring no card runs on a CPU pod sized by its jobs
FACTS_CPU = dict(FACTS_A, gpu=None, gpu_memory_gb=0.0, cpu=8.0, ram_gb=16.0)
CPU_OFFERS = {"cpu3c": {"price_per_vcpu_hour": 0.03, "ram_gb_per_vcpu": 2, "vcpu_min": 2, "vcpu_max": 32,
                        "stock": "HIGH"},                                   # pod.sh avail-cpu's JSON, 2026-10-15
              "cpu5c": {"price_per_vcpu_hour": 0.035, "ram_gb_per_vcpu": 2, "vcpu_min": 2, "vcpu_max": 32,
                        "stock": None},
              "cpu3g": {"price_per_vcpu_hour": 0.04, "ram_gb_per_vcpu": 4, "vcpu_min": 2, "vcpu_max": 32,
                        "stock": "LOW"}}
CPU_REQUIRES = {"image": IMAGE_A, "engine": "1.0", "solver_version": 1, "gpu": "none"}


def a_cpu_job(**changes):
    return a_job(**{"cpu": 2, "gpu_memory_gb": 0, "ram_gb": 7, "requires": CPU_REQUIRES, **changes})


def test_a_job_requiring_no_card_keeps_the_contract_and_card_memory_with_it_is_refused():
    assert podjobs.job_problems(a_cpu_job()) == []
    problems = "\n".join(podjobs.job_problems(a_cpu_job(gpu_memory_gb=1, requires={**CPU_REQUIRES,
                                                                                      "gpu_memory_gb_min": 20})))
    assert "`requires.gpu_memory_gb_min` with `requires.gpu` \"none\"" in problems, problems
    assert "`gpu_memory_gb` 1 with `requires.gpu` \"none\"" in problems, problems


def test_a_cpu_pod_serves_only_jobs_that_require_no_card_and_a_gpu_pod_never_serves_them():
    assert podd.unmet_requirements(CPU_REQUIRES, FACTS_CPU) == []
    assert "on a pod for a job that requires none" in podd.unmet_requirements(CPU_REQUIRES, FACTS_A)[0]
    assert "no card on the pod" in podd.unmet_requirements(a_job()["requires"], FACTS_CPU)[0]
    assert podd.cards_for(CPU_REQUIRES, CARDS) == []
    assert not podd.booting_pod_serves(CPU_REQUIRES, a_job()["requires"], CARDS)
    assert not podd.booting_pod_serves(a_job()["requires"], CPU_REQUIRES, CARDS)
    assert podd.booting_pod_serves(CPU_REQUIRES, {"image": IMAGE_A, "gpu": "none"}, CARDS)


def test_a_cpu_pods_facts_have_no_card():
    facts = podd.facts_of_pod('{"engine": "1.0"}', "none\n", "max 100000\n0-7\n8\n", "max\nMemTotal: 500000000 kB\n",
                              IMAGE_A, {"memory_gb": 16, "cost_per_hr": 0.24}, "podd-g1", "pod-c", "now")
    assert facts["gpu"] is None and facts["gpu_memory_gb"] == 0 and facts["cpu"] == 8.0 and facts["ram_gb"] == 16
    assert podd.cpu_of("max 100000\n5,101\n8\n") == 2.0, "a 2-vCPU cpu3g: two hyperthreads, whatever nproc says"
    assert podd.cpu_of("max 100000\n0-3,8-11\n96\n") == 8.0
    assert podd.cpu_of("765000 100000\n0-95\n96\n") == 7.65, "the quota when it is the smaller"


def test_the_cpu_pod_is_sized_by_its_jobs_cheapest_per_job_under_the_cap():
    # ten jobs of 2 cores, 7 GB: cpu3c needs 3.5 vCPU a job (RAM), 4 jobs on 16 vCPU ($0.48, $0.12 a job);
    # cpu3g 2 vCPU a job, 4 jobs on 8 vCPU ($0.32, $0.08 a job; 16 vCPU would be $0.64, over the cap)
    plan, why = podd.cpu_pod_plan([a_cpu_job()] * 10, CPU_REQUIRES, ["cpu3c", "cpu5c", "cpu3g"], CPU_OFFERS, 0.60)
    assert why is None and plan["vcpu"] == 8 and plan["flavors"] == ["cpu3g"] and plan["jobs_per_pod"] == 4, plan
    assert plan["capacity"] == {"cpu": 8.0, "gpu_memory_gb": 0.0, "ram_gb": 32.0} and plan["price_per_hour"] == 0.32
    one, _ = podd.cpu_pod_plan([a_cpu_job()], CPU_REQUIRES, ["cpu3c", "cpu5c", "cpu3g"], CPU_OFFERS, 0.60)
    assert one["vcpu"] == 2 and one["flavors"] == ["cpu3g"] and one["jobs_per_pod"] == 1, one
    # a cpu "all" job runs alone, sized by cpu_min: every flavor holds it at 8 vCPU; the cheapest first, then in stock
    alone, _ = podd.cpu_pod_plan([a_cpu_job(cpu="all", requires={**CPU_REQUIRES, "cpu_min": 8})] * 3,
                                 {**CPU_REQUIRES, "cpu_min": 8}, ["cpu3g", "cpu5c", "cpu3c"], CPU_OFFERS, 0.60)
    assert alone["vcpu"] == 8 and alone["jobs_per_pod"] == 1 and alone["flavors"] == ["cpu3c", "cpu3g", "cpu5c"], alone
    assert alone["capacity"]["ram_gb"] == 16.0, "the smallest RAM of the flavors offered"
    # after a stock miss at 8 vCPU the set asks for at most 4 (2 jobs of cpu3g), never below one job's size
    small, _ = podd.cpu_pod_plan([a_cpu_job()] * 10, CPU_REQUIRES, ["cpu3c", "cpu5c", "cpu3g"], CPU_OFFERS, 0.60,
                                 vcpu_ceiling=4)
    assert small["vcpu"] == 4 and small["jobs_per_pod"] == 2 and small["flavors"] == ["cpu3g"], small
    floor, _ = podd.cpu_pod_plan([a_cpu_job(cpu=8)], CPU_REQUIRES, ["cpu3c"], CPU_OFFERS, 0.60, vcpu_ceiling=2)
    assert floor["vcpu"] == 8, "a ceiling below one job's size is one job's size"
    none, why = podd.cpu_pod_plan([a_cpu_job()], CPU_REQUIRES, ["cpu3c", "cpu9x"], CPU_OFFERS, 0.05)
    assert none is None and "PRICE CAP" in why and "cpu9x: RunPod lists no price" in why, why


def test_a_set_requiring_no_card_opens_a_cpu_pod_through_create_cpu():
    root = Path(tempfile.mkdtemp(prefix="podd-cpu-"))
    queue_directory, secrets = root / "queue", root / "secrets"
    (secrets / podd.PODS_DIRECTORY_NAME).mkdir(parents=True)
    for number in range(10):
        stage = queue_directory / f"cpu-{number}"
        stage.mkdir(parents=True)
        (stage / "in").write_text("x")
        podjobs.write_job(stage, a_cpu_job(expected_s=3000))
    config = {"queue_directories": [str(queue_directory)], "max_pods": {"gpu": 2, "cpu": 2}, "idle_minutes": 3,
              "max_price_per_hour": 0.60, "boot_seconds": 150, "max_wait_for_stock_minutes": 60, "cards": CARDS,
              "allowed_cuda_versions": ["12.8"], "cpu_flavors": ["cpu3c", "cpu5c", "cpu3g"]}
    pool = podd.Pool(secrets, config)
    pool.cpu_offers = (time.time(), json.loads(json.dumps(CPU_OFFERS)))   # never pod.sh avail-cpu in a test
    saved = podd.Pod, podd.STATUS_FILE
    podd.Pod, podd.STATUS_FILE = PodThatDoesNotBoot, root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            pool.step()
    finally:
        podd.Pod, podd.STATUS_FILE = saved
    assert sorted(pool.pods) == ["podd-c1", "podd-c2"], (list(pool.pods), said.getvalue())
    assert pool.pods["podd-c1"].create_command()[1:] == ["create-cpu", "podd-c1", IMAGE_A, "cpu3g", "8"]
    assert "8 vCPU ($0.32/h, 4 job(s) a pod, 32 GB) of cpu3g $0.04/vCPU-h" in said.getvalue(), said.getvalue()


def test_gpu_and_cpu_pods_are_capped_apart():
    """2026-10-15: hour-long CPU jobs held all 8 shared slots and starved a 244-job GPU sweep."""
    root = Path(tempfile.mkdtemp(prefix="podd-caps-"))
    queue_directory, secrets = root / "queue", root / "secrets"
    (secrets / podd.PODS_DIRECTORY_NAME).mkdir(parents=True)
    for number in range(10):
        for kind, job in (("cpu", a_cpu_job(expected_s=3000)), ("cuda", a_job(cpu="all", expected_s=3000))):
            stage = queue_directory / f"{kind}-{number}"
            stage.mkdir(parents=True)
            (stage / "in").write_text("x")
            podjobs.write_job(stage, job)
    config = {"queue_directories": [str(queue_directory)], "max_pods": {"gpu": 3, "cpu": 2}, "idle_minutes": 3,
              "max_price_per_hour": 0.60, "boot_seconds": 150, "max_wait_for_stock_minutes": 60, "cards": CARDS,
              "allowed_cuda_versions": ["12.8"], "cpu_flavors": ["cpu3c", "cpu5c", "cpu3g"]}
    pool = podd.Pool(secrets, config)
    pool.cpu_offers = (time.time(), json.loads(json.dumps(CPU_OFFERS)))
    pool.offers = (time.time(), json.loads(json.dumps(OFFERS)))
    saved = podd.Pod, podd.STATUS_FILE
    podd.Pod, podd.STATUS_FILE = PodThatDoesNotBoot, root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            pool.step()
            pool.step()
    finally:
        podd.Pod, podd.STATUS_FILE = saved
    assert sorted(pool.pods) == ["podd-c1", "podd-c2", "podd-g1", "podd-g2", "podd-g3"], sorted(pool.pods)
    (root / "c.json").write_text(json.dumps({**config, "max_pods": 8}))       # the old shared int: refused
    raises(podjobs.JobContractError, lambda: podjobs.read_config(root / "c.json"),
           "`max_pods` must be {\"gpu\": n, \"cpu\": n}")


def test_pod_sh_create_cpu_asks_runpod_for_a_cpu_pod_of_the_flavors_and_vcpu():
    """The REAL pod.sh create-cpu against a fake curl (first on PATH) that records the request and answers RunPod's
    no-instances 500: pod.sh exits 75 and the body it sent is the CPU PodCreateInput."""
    root = Path(tempfile.mkdtemp(prefix="podsh-cpu-"))
    (root / ".secrets").mkdir(mode=0o700)
    (root / ".secrets" / "runpod_api_key").write_text("not-a-key\n")
    (root / "bin").mkdir()
    fake_curl = root / "bin" / "curl"
    fake_curl.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$HOME/curl-args\"\n"
                         "printf '%s\\n500' '{\"error\":\"create pod: There are no instances currently available\"}'\n")
    fake_curl.chmod(0o755)
    environment = {**os.environ, "HOME": str(root), "PATH": f"{root / 'bin'}:{os.environ['PATH']}"}
    created = subprocess.run([str(PACKAGE_DIRECTORY / "pod.sh"), "create-cpu", "podd-9", IMAGE_A, "cpu3g,cpu3c", "8"],
                             capture_output=True, text=True, env=environment, stdin=subprocess.DEVNULL)
    assert created.returncode == podd.POD_SH_NO_STOCK_EXIT, (created.returncode, created.stderr)
    arguments = (root / "curl-args").read_text().splitlines()
    body = json.loads(arguments[arguments.index("--data") + 1])
    assert body["computeType"] == "CPU" and body["cpuFlavorIds"] == ["cpu3g", "cpu3c"] and body["vcpuCount"] == 8
    assert body["cpuFlavorPriority"] == "custom" and body["imageName"] == IMAGE_A and "gpuTypeIds" not in body
    assert body["containerDiskInGb"] <= 10 * 2, "fits the smallest CPU pod's disk cap (10 GB a vCPU, 2 vCPU)"
    assert "not-a-key" not in "\n".join(arguments), "the key reaches curl only through a header file"
    fake_curl.write_text("#!/usr/bin/env bash\nprintf '%s\\n500' '{\"error\":\"create pod: There are no longer any "
                         "instances available with the requested specifications.\"}'\n")    # a CPU size out, 2026-10-15
    refused = subprocess.run([str(PACKAGE_DIRECTORY / "pod.sh"), "create-cpu", "podd-9", IMAGE_A, "cpu3c", "16"],
                             capture_output=True, text=True, env=environment, stdin=subprocess.DEVNULL)
    assert refused.returncode == podd.POD_SH_NO_STOCK_EXIT, (refused.returncode, refused.stderr)


# 7d. KEEP PARTIAL WORK, CATCH SLOW HOSTS, SCALE THE DEADLINE (2026-10-16: two US-MD-1 CPU pods ran four jobs into a
# fixed 6 h timeout at 103-109 of 115 units and the finished units were thrown away, ~$5.44)
def a_resumable_cpu_job(**changes):
    return a_cpu_job(**{"command": "bash solve.sh request.json 4", "inputs": ["in"],
                        "outputs": ["raw", "solve.log"], "expected_s": 1150, "progress": "progress",
                        "resumable": True, "resume_from": ["raw/manifest.json", "solve.log"], **changes})


def test_the_contract_requires_expected_s_and_checks_progress_and_resumable():
    assert podjobs.job_problems(a_resumable_cpu_job()) == []
    no_expected = {key: value for key, value in a_job().items() if key != "expected_s"}
    assert "no `expected_s`" in podjobs.job_problems(no_expected)
    problems = "\n".join(podjobs.job_problems(a_job(resumable=True)) + podjobs.job_problems(
        a_job(progress="../p", resumable="yes", attempts={"a": 1})) + podjobs.job_problems(a_job(timeout_s=60)))
    problems += "\n".join(podjobs.job_problems(a_resumable_cpu_job(resume_from=[]))
                           + podjobs.job_problems({k: v for k, v in a_resumable_cpu_job().items() if k != "resume_from"})
                           + podjobs.job_problems(a_resumable_cpu_job(resume_from=["build/manifest.json", "../x"]))
                           + podjobs.job_problems(a_job(resume_from=["out/manifest.json"])))
    for words in ("`resumable` without `progress`", "`progress`: '../p' is not inside the stage",
                  "`resumable` 'yes' must be true or false", "`attempts` (podd's) must be a list",
                  "unknown key `timeout_s`", "`resumable` without `resume_from`",
                  "`resume_from` must be a non-empty list", "'build/manifest.json' is inside none of the outputs",
                  "`resume_from`: '../x' is not inside the stage", "`resume_from` without `resumable` true"):
        assert words in problems, (words, problems)


def test_the_deadline_is_a_margin_over_expected_s_scaled_by_the_pods_slowness():
    assert podd.job_deadline_s(a_job(expected_s=55), 1.0) == podd.TIMEOUT_MARGIN * 55
    assert podd.job_deadline_s(a_job(expected_s=55), 0.8) == podd.TIMEOUT_MARGIN * 55, "a fast pod never shortens it"
    assert podd.job_deadline_s(a_job(expected_s=100), 2.0) == podd.TIMEOUT_MARGIN * 200


def test_a_jobs_rate_counts_from_its_first_finished_unit_over_enough_units_and_seconds():
    assert podd.parse_progress("57 115\n") == (57, 115)
    for unreadable in ("", "57", "57 of 115", "1 0", "-1 5"):
        assert podd.parse_progress(unreadable) is None, unreadable
    assert podd.seconds_per_unit([(100.0, 1, 1), (108.0, 2, 1)]) is None, "too few units"
    assert podd.seconds_per_unit([(100.0, 1, 1), (110.0, 4, 1)]) is None, "too few seconds"
    assert podd.seconds_per_unit([(100.0, 1, 1), (130.0, 2, 1), (160.0, 3, 1), (190.0, 4, 1)]) == 30.0
    running = a_running(Path("/q/s"), a_resumable_cpu_job())
    for now, text in ((0, ""), (2, "0 115"), (400, "0 115"), (410, "1 115"), (430, "2 115"), (450, "3 115"),
                      (470, "4 115")):
        running.saw_progress(text, now)
    assert running.observations == [(410, 1, 1), (430, 2, 1), (450, 3, 1), (470, 4, 1)], "start-up never counts"
    assert podd.seconds_per_unit(running.observations) == 20.0 and running.progress_text() == "4/115"
    with contextlib.redirect_stdout(io.StringIO()) as said:
        running.saw_progress("garbage", 480)
        running.saw_progress("garbage", 482)
    assert said.getvalue().count("not `<done> <total>`") == 1, said.getvalue()


def test_the_reference_is_the_median_pace_of_other_pods_at_the_same_load_never_expected_s_alone():
    job, now = a_resumable_cpu_job(expected_s=1150), 10_000.0
    assert podd.pace_of(10.0, job, 115) == 1.0 and podd.pace_of(5.0, job, 115) == 0.5, "over its own expected s a unit"
    assert podd.reference_pace(1, {}, now) is None, "expected_s alone is no reference (2026-10-21: 3x off)"
    assert podd.reference_pace(1, {"a#1": (0.7, 1, "pod-a", now)}, now) is None, "one other pod is too few"
    others = {"a#1": (0.7, 2, "pod-a", now), "a#2": (0.8, 2, "pod-a", now), "b#1": (0.9, 2, "pod-b", now),
              "c#1": (3.0, 2, "pod-c", now), "d#1": (0.1, 1, "pod-d", now), "e#1": (0.1, 2, "pod-e", now - 7200)}
    assert podd.reference_pace(2, others, now) == (0.9, 3), "the median over pods of each pod's median pace"
    assert podd.reference_pace(20, others, now) is None, "a pod running 20 jobs at once has no fair reference"
    # the measured spreads: every normal host under SLOW_FACTOR x the median, every slow one over
    fast_2026_10_16, slow_2026_10_16 = (6.7, 7.8, 8.9), (14.6, 20.0)          # s a unit, one job shape
    assert all(rate / podd.median(fast_2026_10_16) < podd.SLOW_FACTOR for rate in fast_2026_10_16)
    assert all(rate / podd.median(fast_2026_10_16) >= podd.SLOW_FACTOR for rate in slow_2026_10_16)
    normal_2026_10_21 = (0.24, 0.25, 0.27, 0.29, 0.35, 0.38, 0.40, 0.42, 0.45, 0.47, 0.48, 0.52)    # pace
    slow_2026_10_21 = (0.69, 0.77, 0.79)                                     # US-MO-1, US-CA-2
    assert all(pace / 0.40 < podd.SLOW_FACTOR for pace in normal_2026_10_21)
    assert all(pace / 0.40 >= podd.SLOW_FACTOR for pace in slow_2026_10_21)
    assert not podd.worth_moving((124 - 64) * 6.0, 1.1, 150), "a little slow, half done: it runs on"
    assert not podd.worth_moving((115 - 110) * 17.0, 2.0, 150), "twice as slow, near its end: it runs on"
    assert podd.worth_moving((115 - 12) * 17.0, 2.0, 150)


def test_wait_for_the_exit_hands_over_progress_stops_when_told_and_rereads_the_deadline():
    clock, seen = FakeClock(), []
    exited = podd.wait_for_the_exit(reads(ok(f"{podd.PROGRESS_MARK}\n3 115\n"),
                                          ok(f'{{"exit_status": 0, "wall_s": 9}}\n{podd.PROGRESS_MARK}\n115 115\n')),
                                    deadline_s=100, sleep=clock.sleep, clock=clock,
                                    saw_progress=lambda text, now: seen.append((text, now)))
    assert exited == {"exit_status": 0, "wall_s": 9} and seen == [("3 115", 2.0), ("115 115", 4.0)], seen
    raises(podd.JobStopped, lambda: podd.wait_for_the_exit(reads(), deadline_s=100, sleep=clock.sleep, clock=clock,
                                                           stop=lambda: ("slow pod", "its pod is slow")),
           "its pod is slow")
    clock, slowness = FakeClock(), [1.0]

    def slower():
        if clock.now >= 50:
            slowness[0] = 2.0                                  # measured mid-job: the deadline moves out
        return 60 * slowness[0]
    raises(podd.JobTimedOut, lambda: podd.wait_for_the_exit(reads(), deadline_s=slower, sleep=clock.sleep,
                                                            clock=clock), "no exit 120 s")
    assert 120 <= clock.now <= 120 + podd.POLL_SECONDS


class FakeJobPod:
    """A pod for run_the_job: its ssh answers, every command recorded; transfer, kill and the wait are stood in."""

    def __init__(self, stage):
        self.name, self.ssh_alias, self.state, self.problem, self.slowness = "podd-c1", "runpod-podd-c1", "ready", None, 1
        self.facts = dict(FACTS_CPU, datacenter="US-MD-1")
        self.stage, self.commands = stage, []

    def ssh(self, command, **options):
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")


def run_once(stage, job, running, outcome, pulled_raw=("000000", "000001")):
    """run_the_job with the pod side simulated: the wait ends with `outcome` (an exception, or an exit), and the pull
    brings back raw/ holding `pulled_raw` and command.log. -> (pod, every transfer command)."""
    pod, transfers = FakeJobPod(stage), []

    def fake_pull(pod, remote, at, job):
        (at / "raw").mkdir(exist_ok=True)
        for stem in pulled_raw:
            (at / "raw" / f"{stem}.json").write_text("{}")
        (at / podjobs.COMMAND_LOG).write_text(f"solved {len(pulled_raw)} units\n")
        (at / "solve.log").write_text(f"{len(pulled_raw)} units\n")
        return [path for path in job["outputs"] if not (at / path).exists()]

    def wait(*args, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    saved = podd.pull_back, podd.wait_for_the_exit, podd.kill_the_job, podd.transfer
    podd.pull_back, podd.wait_for_the_exit = fake_pull, wait
    podd.kill_the_job, podd.transfer = (lambda pod, remote: None), transfers.append
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            podd.run_the_job(stage, job, pod, running)
    finally:
        podd.pull_back, podd.wait_for_the_exit, podd.kill_the_job, podd.transfer = saved
    return pod, transfers


def a_running_stage(job):
    stage = Path(tempfile.mkdtemp(prefix="podd-resume-"))
    (stage / "in").write_text("x")
    podjobs.write_job(stage, job)
    os.rename(stage / podjobs.JOB, stage / podjobs.RUNNING)
    return stage


def test_a_resumable_job_stopped_on_a_slow_pod_brings_its_work_home_and_is_requeued_to_do_the_rest():
    job = a_resumable_cpu_job()
    stage = a_running_stage(job)
    running = a_running(stage, job)
    running.saw_progress("0 115", 0)
    running.saw_progress("1 115", 300)
    running.saw_progress("2 115", 600)
    pod, transfers = run_once(stage, job, running, podd.JobStopped("slow pod", "its pod podd-c1 is slow"))
    assert podjobs.stage_state(stage) == podjobs.JOB, (stage / podjobs.FAILED).read_text() if (
        stage / podjobs.FAILED).exists() else podjobs.stage_state(stage)
    requeued = podjobs.read_state(stage, podjobs.JOB)
    [attempt] = requeued["attempts"]
    assert attempt["stopped_for"] == "slow pod" and attempt["datacenter"] == "US-MD-1" and attempt["progress"] == \
        "2/115", attempt
    assert (stage / "raw" / "000001.json").exists(), "the finished units came home"
    assert (stage / ".podd" / "attempt-1.command.log").read_text() == "solved 2 units\n"
    assert pod.state == "ready", "a slow pod is not a broken one"
    # the next attempt ships its inputs and the resume_from files ONLY: the finished units never go back up
    # (2026-10-21: raw/ went back at ~1 MB/s, 0.2-2.2 GB a job, into the 600 s timeout)
    (stage / "raw" / "manifest.json").write_text('{"done": [0, 1]}')
    up = sum((stage / path).stat().st_size for path in ("in", "raw/manifest.json", "solve.log"))
    _, transfers = run_once(stage, requeued, a_running(stage, requeued), {"exit_status": 0, "wall_s": 40},
                            pulled_raw=[f"{n:06d}" for n in range(2, 115)])
    shipped = transfers[0]
    assert shipped == ["rsync", "-rtR", f"{stage}/./in", f"{stage}/./raw/manifest.json", f"{stage}/./solve.log",
                       f"runpod-podd-c1:{podd.remote_directory(stage)}/"], shipped
    done = podjobs.read_state(stage, podjobs.DONE)
    assert len(done["attempts"]) == 1 and done["used"]["datacenter"] == "US-MD-1", done
    assert done["timings"]["ship_bytes"] == up, (up, done["timings"])
    assert all((stage / "raw" / f"{n:06d}.json").exists() for n in range(115)), "kept units + the new ones = all"


def test_the_pull_lays_the_outputs_over_the_stage_and_deletes_nothing_there():
    """A resumed attempt's pod holds only the units it did (and the resume_from files): the pull must keep the units
    earlier attempts brought home, so it never --delete-s (the merge at home is podd's, by file)."""
    stage = Path(tempfile.mkdtemp(prefix="podd-pull-"))
    pod, transfers = FakeJobPod(stage), []
    pod.ssh = lambda command, **options: subprocess.CompletedProcess(command, 0, "raw\nsolve.log\n", "")
    saved = podd.transfer
    podd.transfer = transfers.append
    try:
        missing = podd.pull_back(pod, "/workspace/jobs/x", stage, a_resumable_cpu_job())
    finally:
        podd.transfer = saved
    assert missing == [] and transfers == [["rsync", "-rtR", "runpod-podd-c1:/workspace/jobs/x/./raw",
                                            "runpod-podd-c1:/workspace/jobs/x/./solve.log",
                                            f"runpod-podd-c1:/workspace/jobs/x/./{podjobs.COMMAND_LOG}", f"{stage}/"]], \
        transfers


def test_a_stopped_job_that_is_not_resumable_fails_with_what_it_wrote():
    job = a_job(outputs=["raw", "solve.log"])
    stage = a_running_stage(job)
    run_once(stage, job, a_running(stage, job), podd.JobStopped("podd stopping", "podd stopped while it ran"))
    reason = (stage / podjobs.FAILED).read_text()
    assert "podd stopped while it ran" in reason and "pulled back what existed" in reason, reason
    assert (stage / "raw" / "000000.json").exists()


def test_a_deadline_requeues_a_resumable_job_only_when_it_made_progress_and_never_past_max_attempts():
    job = a_resumable_cpu_job()
    stage = a_running_stage(job)
    run_once(stage, job, a_running(stage, job), podd.JobTimedOut("timed out: no exit 4600 s after it started"))
    reason = (stage / podjobs.FAILED).read_text()
    assert "TIMEOUT_MARGIN 4 x expected_s 1150 x its pod's slowness 1.00" in reason, reason
    assert "pulled back what existed" in reason, "even a failed attempt's work comes home"
    progressed = a_running(stage, job)
    for now, text in ((0, "5 115"), (100, "6 115")):
        progressed.saw_progress(text, now)
    stage = a_running_stage(job)
    run_once(stage, job, progressed, podd.JobTimedOut("timed out"))
    assert podjobs.stage_state(stage) == podjobs.JOB and \
        podjobs.read_state(stage, podjobs.JOB)["attempts"][0]["stopped_for"] == "deadline"
    tired = a_resumable_cpu_job(attempts=[{"stopped_for": "slow pod"}] * (podd.MAX_ATTEMPTS - 1))
    stage = a_running_stage(tired)
    run_once(stage, tired, a_running(stage, tired), podd.JobStopped("slow pod", "its pod is slow"))
    assert podjobs.stage_state(stage) == podjobs.FAILED, "the last attempt FAILS: never a loop"
    assert podd.should_requeue(a_resumable_cpu_job(), "pod broke", progressed=False)
    assert not podd.should_requeue(a_job(), "slow pod", progressed=True)


def slow_pool(root, pods=(("podd-c1", "EU-RO-1"), ("podd-c2", "US-MD-1"))):
    queue_directory, secrets = root / "queue", root / "secrets"
    (secrets / podd.PODS_DIRECTORY_NAME).mkdir(parents=True)
    queue_directory.mkdir(parents=True)
    config = {"queue_directories": [str(queue_directory)], "max_pods": {"gpu": 2, "cpu": 4}, "idle_minutes": 3,
              "max_price_per_hour": 0.60, "boot_seconds": 150, "max_wait_for_stock_minutes": 60, "cards": CARDS,
              "allowed_cuda_versions": ["12.8"], "cpu_flavors": ["cpu3c"]}
    pool = podd.Pool(secrets, config)
    pool.cpu_offers = (time.time(), json.loads(json.dumps(CPU_OFFERS)))
    for name, datacenter in pods:
        pod = podd.Pod(name, secrets, CPU_REQUIRES, config)
        pod.state, pod.facts = "ready", dict(FACTS_CPU, pod_name=name, pod_id=f"id-{name}", datacenter=datacenter)
        (secrets / podd.PODS_DIRECTORY_NAME / f"{name}.id").write_text(f"id-{name}\n")
        pool.pods[name] = pod
    return pool, queue_directory


class Alive:
    def is_alive(self):
        return True

    def join(self, timeout=None):
        pass


def on_pod(pool, stage_name, pod_name, job, seconds_per_unit, units=12, load=1, total=115):
    running = podd.Running(Alive(), pod_name, podd.demand_of(job, podd.capacity_of(FACTS_CPU)), job, time.time(),
                           f"/q/{stage_name}#1")
    running.load = running.peak_load = load
    running.saw_progress(f"0 {total}", 0)
    for done in range(1, units + 1):
        running.saw_progress(f"{done} {total}", 600 + done * seconds_per_unit)
    pool.running[f"/q/{stage_name}"] = running
    return running


THREE_PODS = (("podd-c1", "EU-RO-1"), ("podd-c2", "US-MD-1"), ("podd-c3", "CA-MTL-1"))


def judged(pool, root):
    saved = podd.STATUS_FILE
    podd.STATUS_FILE = root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            pool.judge_speeds()
            pool.judge_speeds()                                     # said once, not every pass
    finally:
        podd.STATUS_FILE = saved
    return said.getvalue()


def test_a_pod_much_slower_than_the_median_is_marked_slow_its_resumable_jobs_moved_and_its_datacenter_avoided():
    root = Path(tempfile.mkdtemp(prefix="podd-slow-"))
    pool, _ = slow_pool(root, THREE_PODS)
    fast = on_pod(pool, "c0", "podd-c1", a_resumable_cpu_job(), 7.8, load=2)          # two jobs a pod, as 2026-10-15
    on_pod(pool, "c3", "podd-c3", a_resumable_cpu_job(), 8.9, load=2)
    slow_resumable = on_pod(pool, "c1", "podd-c2", a_resumable_cpu_job(), 17.0, load=2)
    slow_fixed = on_pod(pool, "c2", "podd-c2", a_resumable_cpu_job(resumable=False, label="a short job"), 17.0, load=2)
    nearly_done = on_pod(pool, "c4", "podd-c2", a_resumable_cpu_job(label="nearly done"), 17.0, units=110, load=2)
    said = judged(pool, root)
    assert pool.pods["podd-c1"].state == "ready" and fast.stop is None and fast.slowness < 1, fast.slowness
    slow = pool.pods["podd-c2"]
    assert slow.state == "slow" and slow.slowness == round(1.7 / ((0.78 + 0.89) / 2), 2), (slow.state, slow.slowness)
    assert slow_resumable.stop and slow_resumable.stop[0] == "slow pod", "moved: what it finished comes home"
    assert slow_fixed.stop is None, "a job that cannot resume runs on, under a deadline scaled by the slowness"
    assert nearly_done.stop is None and "saves less than a boot" in said, said
    assert said.count("IS SLOW") == 1 and "US-MD-1 is recorded SLOW for cpu pods" in said, said
    assert "deadline is now" in said
    assert podd.job_deadline_s(slow_fixed.job, slow.slowness, 2) > podd.job_deadline_s(slow_fixed.job, 1.0, 2)
    recorded = json.loads(podd.SLOW_DATACENTERS_FILE.read_text())
    assert list(recorded) == ["cpu"] and list(recorded["cpu"]) == ["US-MD-1"], recorded
    assert recorded["cpu"]["US-MD-1"]["until_unix"] > time.time()
    assert podd.read_slow_datacenters() == recorded, "kept across a restart"
    assert pool.avoided_datacenters("cpu") == ["US-MD-1"] and pool.avoided_datacenters("gpu") == []
    # a slow pod serves no set: placement skips it, the opening rule replaces it, and the new pod avoids US-MD-1
    pod = podd.Pod("podd-c4", pool.secrets, CPU_REQUIRES, pool.config, ["cpu3c"], 1, 8,
                   pool.avoided_datacenters("cpu"))
    assert pod.create_command()[1:] == ["create-cpu", "podd-c4", IMAGE_A, "cpu3c", "8", "US-MD-1"]
    pool.slow_datacenters["cpu"]["US-MD-1"]["until_unix"] = time.time() - 1
    with contextlib.redirect_stdout(io.StringIO()) as said:
        assert pool.avoided_datacenters("cpu") == [], "avoided for SLOW_DATACENTER_HOURS, then forgiven"
    assert "no longer avoided" in said.getvalue()
    podd.SLOW_DATACENTERS_FILE.unlink()


def test_jobs_of_one_command_and_different_sizes_are_compared_by_pace_not_seconds_per_unit():
    """2026-10-21 16:13:50: five pods marked SLOW at once, large jobs (expected_s 3712 s, 124 units) at 6.5-7.0 s a
    unit against 3.9 s a unit measured on a smaller job of the same command."""
    root = Path(tempfile.mkdtemp(prefix="podd-slow-"))
    pool, _ = slow_pool(root, THREE_PODS)
    on_pod(pool, "small-c0", "podd-c1", a_resumable_cpu_job(expected_s=1411), 2.8, units=60, total=124)
    on_pod(pool, "small-c1", "podd-c3", a_resumable_cpu_job(expected_s=1411), 2.9, units=60, total=124)
    large = on_pod(pool, "large-c1", "podd-c2", a_resumable_cpu_job(expected_s=3712), 7.0, units=30, total=124)
    said = judged(pool, root)
    assert "IS SLOW" not in said and large.stop is None and large.slowness < podd.SLOW_FACTOR, (large.slowness, said)


def test_a_pod_in_the_normal_spread_is_not_slow_against_the_fastest_one():
    """2026-10-21 15:29: three US-GA-2 pods marked SLOW at 1.50-1.53x one US-IL-1 pod; whole jobs there ran at pace
    0.35 against US-IL-1's 0.24 and every other datacenter's 0.24-0.52."""
    root = Path(tempfile.mkdtemp(prefix="podd-slow-"))
    pool, _ = slow_pool(root, (("podd-c1", "US-IL-1"), ("podd-c2", "US-GA-2"), ("podd-c3", "US-GA-2")))
    for n, (pod_name, rate) in enumerate((("podd-c1", 7.0), ("podd-c2", 10.5), ("podd-c3", 10.9))):
        on_pod(pool, f"large-2s-c{n}", pod_name, a_resumable_cpu_job(expected_s=3712), rate, units=14, total=124)
    said = judged(pool, root)
    assert all(pod.state == "ready" for pod in pool.pods.values()) and "IS SLOW" not in said, said


def test_a_lone_pod_is_never_slow_by_expected_s_and_a_set_short_of_stock_keeps_its_slow_pod():
    """2026-10-21 10:03: US-CA-2 banned at 2.49x a client's expected_s, which was 3x off; 2026-10-21 23:15-02:33:
    pods stopped as slow while RunPod had no stock to replace them."""
    root = Path(tempfile.mkdtemp(prefix="podd-slow-"))
    pool, _ = slow_pool(root, (("podd-c1", "US-CA-2"),))
    lone = on_pod(pool, "small-c0", "podd-c1", a_resumable_cpu_job(expected_s=392), 7.9, units=10, total=124)
    said = judged(pool, root)
    assert pool.pods["podd-c1"].state == "ready" and "IS SLOW" not in said and lone.stop is None, said
    assert lone.slowness == 2.5 and pool.pods["podd-c1"].slowness == 2.5, "its pace still scales its deadline"
    pool, _ = slow_pool(root / "kept", THREE_PODS)
    on_pod(pool, "c0", "podd-c1", a_resumable_cpu_job(), 7.8)
    on_pod(pool, "c3", "podd-c3", a_resumable_cpu_job(), 8.9)
    slow_job = on_pod(pool, "c1", "podd-c2", a_resumable_cpu_job(), 17.0)
    pool.short_of_stock[podd.requirement_key(CPU_REQUIRES)] = podd.StockWait(time.time())
    said = judged(pool, root)
    assert pool.pods["podd-c2"].state == "ready" and slow_job.stop is None, "nothing would replace it: kept"
    assert said.count("KEPT") == 1 and not podd.SLOW_DATACENTERS_FILE.exists(), said


def test_twenty_jobs_packed_on_one_a40_are_neither_killed_nor_marked_slow():
    """2026-10-15: one client's 92-unit GPU jobs took 66-70 s at <= 3 at once and ~420 s each with 19-20 packed on one
    A40 (the pod's throughput the same). Their expected_s was 69 s; a deadline blind to the packing (4 x 69 = 276 s)
    killed them all."""
    expected_s, packed_s = 69, 420
    packed_job = a_job(expected_s=expected_s, progress="progress")
    assert podd.job_deadline_s(packed_job, 1.0, 1) < packed_s, "the deadline without the load would kill them"
    assert podd.job_deadline_s(packed_job, 1.0, 20) > packed_s
    root = Path(tempfile.mkdtemp(prefix="podd-packed-"))
    pool, _ = slow_pool(root)
    for name in ("podd-c1", "podd-c2"):
        pool.pods[name].facts = dict(FACTS_A, pod_name=name, pod_id=f"id-{name}", datacenter="CA-MTL-1", cpu=50.0)
    seconds_per_unit_alone = (70 - 50) / 92                 # the steady rate after start-up, <= 3 at once
    for n in range(3):
        on_pod(pool, f"alone-{n}", "podd-c1", packed_job, seconds_per_unit_alone, units=92, load=3)
    packed = [on_pod(pool, f"packed-{n}", "podd-c2", packed_job, (packed_s - 50) / 92, units=40, load=20)
              for n in range(20)]
    pool.count_loads()
    assert all(running.load == 20 and running.peak_load == 20 for running in packed)
    saved = podd.STATUS_FILE
    podd.STATUS_FILE = root / "PODS.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            pool.judge_speeds()
    finally:
        podd.STATUS_FILE = saved
    assert pool.pods["podd-c2"].state == "ready" and "IS SLOW" not in said.getvalue(), said.getvalue()
    # judged only against its equals (its 19 neighbours, once measured): the 3-at-once pod is no reference for it
    assert all(running.stop is None and running.slowness in (None, 1.0) for running in packed)
    assert podd.job_deadline_s(packed_job, pool.pods["podd-c2"].slowness, packed[0].peak_load) > packed_s
    # at EQUAL load a slow pod is still caught: against two pods of 20, a pod of 20 at twice the packed rate
    for name, datacenter, rate in (("podd-c4", "CA-MTL-1", (packed_s - 50) / 92),
                                   ("podd-c3", "US-MD-1", 2 * (packed_s - 50) / 92)):
        pool.pods[name] = podd.Pod(name, pool.secrets, CPU_REQUIRES, pool.config)
        pool.pods[name].state = "ready"
        pool.pods[name].facts = dict(FACTS_A, pod_name=name, pod_id=f"id-{name}", datacenter=datacenter, cpu=50.0)
        (pool.secrets / podd.PODS_DIRECTORY_NAME / f"{name}.id").write_text(f"id-{name}\n")
        for n in range(20):
            on_pod(pool, f"{name}-{n}", name, packed_job, rate, units=40, load=20)
    with contextlib.redirect_stdout(io.StringIO()):
        pool.judge_speeds()
    assert pool.pods["podd-c3"].state == "slow" and pool.pods["podd-c2"].state == "ready"
    assert pool.pods["podd-c4"].state == "ready"
    podd.SLOW_DATACENTERS_FILE.unlink()


def test_a_slow_pod_takes_no_job_is_replaced_and_goes_once_empty():
    root = Path(tempfile.mkdtemp(prefix="podd-slow-step-"))
    pool, queue_directory = slow_pool(root)
    pool.pods.pop("podd-c1")
    stage = queue_directory / "next-chunk"
    stage.mkdir()
    (stage / "in").write_text("x")
    podjobs.write_job(stage, a_resumable_cpu_job(expected_s=3000))
    pool.pods["podd-c2"].state, pool.pods["podd-c2"].problem = "slow", "SLOW: test"
    saved = podd.Pod, podd.STATUS_FILE, podd.POD_SCRIPT
    fake = root / "pod.sh"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake.chmod(0o755)
    podd.Pod, podd.STATUS_FILE, podd.POD_SCRIPT = PodThatDoesNotBoot, root / "PODS.json", fake
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            pool.step()
    finally:
        podd.Pod, podd.STATUS_FILE, podd.POD_SCRIPT = saved
    assert podjobs.stage_state(stage) == podjobs.JOB, "never placed on the slow pod"
    assert "podd-c2" not in pool.pods and "deleting pod id-podd-c2" in said.getvalue(), said.getvalue()
    assert any(name.startswith("podd-c") and pod.state == "booting" for name, pod in pool.pods.items()), \
        "a replacement opens"


def test_a_broken_slow_datacenters_file_is_said_and_set_aside():
    path = Path(tempfile.mkdtemp(prefix="podd-dc-")) / "slow-datacenters.json"
    path.write_text("{not json")
    with contextlib.redirect_stdout(io.StringIO()) as said:
        assert podd.read_slow_datacenters(path) == {}
    assert "is not JSON" in said.getvalue() and path.with_name(path.name + ".broken").exists()


def test_podd_stopping_stops_every_running_job_and_waits_for_it_to_come_home():
    root = Path(tempfile.mkdtemp(prefix="podd-stop-"))
    pool, _ = slow_pool(root)
    joined = []

    class Thread(Alive):
        def join(self, timeout=None):
            joined.append(timeout)
    running = on_pod(pool, "c0", "podd-c1", a_resumable_cpu_job(), 7.8)
    running.thread = Thread()
    fake = root / "pod.sh"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake.chmod(0o755)
    saved = podd.STATUS_FILE, podd.POD_SCRIPT
    podd.STATUS_FILE, podd.POD_SCRIPT = root / "PODS.json", fake
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            pool.shut_down()
    finally:
        podd.STATUS_FILE, podd.POD_SCRIPT = saved
    assert running.stop == ("podd stopping", "podd stopped while it ran") and joined and \
        joined[0] <= podd.SHUTDOWN_PULL_SECONDS
    assert not pool.pods
    service = (REPOSITORY_ROOT / "systemd" / "podd.service").read_text()
    assert f"TimeoutStopSec={podd.SHUTDOWN_PULL_SECONDS + 60}" in service, "systemd waits for the pulls"


def test_pod_sh_create_avoids_the_slow_datacenters():
    """The REAL pod.sh create and create-cpu against a fake curl: an avoid list sends RunPod's datacenters less those;
    none sends no dataCenterIds (any datacenter)."""
    root = Path(tempfile.mkdtemp(prefix="podsh-avoid-"))
    (root / ".secrets").mkdir(mode=0o700)
    (root / ".secrets" / "runpod_api_key").write_text("not-a-key\n")
    (root / "bin").mkdir()
    fake_curl = root / "bin" / "curl"
    fake_curl.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$HOME/curl-args\"\n"
                         "printf '%s\\n500' '{\"error\":\"create pod: There are no instances currently available\"}'\n")
    fake_curl.chmod(0o755)
    environment = {**os.environ, "HOME": str(root), "PATH": f"{root / 'bin'}:{os.environ['PATH']}"}

    def body_of(*arguments):
        created = subprocess.run([str(PACKAGE_DIRECTORY / "pod.sh"), *arguments], capture_output=True, text=True,
                                 env=environment, stdin=subprocess.DEVNULL)
        assert created.returncode == podd.POD_SH_NO_STOCK_EXIT, (created.returncode, created.stderr)
        sent = (root / "curl-args").read_text().splitlines()
        return json.loads(sent[sent.index("--data") + 1]), created.stderr
    body, _ = body_of("create-cpu", "podd-9", IMAGE_A, "cpu3c", "8", "US-MD-1,EU-SE-1")
    assert "US-MD-1" not in body["dataCenterIds"] and "EU-SE-1" not in body["dataCenterIds"]
    assert "EU-RO-1" in body["dataCenterIds"] and len(body["dataCenterIds"]) == 28, body["dataCenterIds"]
    body, _ = body_of("create", "podd-9", IMAGE_A, "NVIDIA A40", "8", "12.8", "US-MD-1")
    assert "US-MD-1" not in body["dataCenterIds"] and len(body["dataCenterIds"]) == 29
    body, _ = body_of("create-cpu", "podd-9", IMAGE_A, "cpu3c", "8")
    assert "dataCenterIds" not in body, "no avoid list: any datacenter"
    body, said = body_of("create-cpu", "podd-9", IMAGE_A, "cpu3c", "8", "XX-NOWHERE-1")
    assert "not in RUNPOD_DATACENTERS" in said, said


# 8. podd is anyone's: every module imports the standard library and podd itself, nothing else (a hard rule)
def test_podd_imports_nothing_but_the_standard_library_and_itself():
    modules = sorted(PACKAGE_DIRECTORY.glob("*.py"))
    assert {module.name for module in modules} >= {"daemon.py", "job_contract.py", "status.py", "__main__.py"}, modules
    for module in modules:
        tree = ast.parse(module.read_text())
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        foreign = {name for name in imported - {"podd"} if name not in sys.stdlib_module_names}
        assert not foreign, (module.name, foreign)


if __name__ == "__main__":
    started = time.time()
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
            print(f"PASS {name}")
    print(f"all passed in {time.time() - started:.1f} s")
