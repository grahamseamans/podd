"""`python3 -m podd status`: the pool and the queues at a glance: podd's pods (its status file, daemon.STATUS_FILE)
and every stage of every queue directory podd watches, with its state and age. podd.job_contract says what the states
mean; `journalctl --user -u podd` says what happened; `pod.sh account` lists what the RunPod account has."""
from pathlib import Path

from podd import daemon, job_contract


def print_status():
    status = daemon.read_status()
    if status is None:
        print(f"pods: podd never wrote {daemon.STATUS_FILE}")
    else:
        print(f"pods ({len(status['pods'])}; at most {status['max_pods']['gpu']} GPU and {status['max_pods']['cpu']} "
              f"CPU, cards at most ${status['max_price_per_hour']}/h; as of {status['written_at']}):")
        for pod in status["pods"]:
            facts = pod.get("facts") or {}
            price = f"${facts['cost_per_hr']}/h" if facts.get("cost_per_hr") else ""
            used = " ".join(f"{resource} {pod['used'][resource]}/{facts.get(resource, '?')}"
                            for resource in daemon.RESOURCES)
            print(f"  {pod['name']:<8} {pod['state']:<11} {facts.get('gpu') or '':<24} {price:<9} "
                  f"{facts.get('datacenter') or '':<9} {(facts.get('image') or pod['requires']['image'])!s:<20.60} "
                  f"used {used} slowness {pod.get('slowness')} "
                  f"{' '.join(Path(job).name for job in pod['jobs'])} "
                  f"{pod.get('problem') or ''}")
            for job, progress in (pod.get("progress") or {}).items():
                print(f"           {Path(job).name}: {progress['done']} done, {progress['seconds_per_unit']} s a "
                      f"unit, slowness {progress['slowness']}")
        if not status["pods"]:
            print("  (none)")
        for kind, datacenters in (status.get("avoided_datacenters") or {}).items():
            if not all(isinstance(record, dict) for record in (datacenters or {}).values()):
                print(f"  avoided (not {{datacenter: record}}, printed as is): {kind} {datacenters}")
                continue
            for datacenter, record in (datacenters or {}).items():
                print(f"  {kind} pods avoid until {record.get('until')}: {datacenter} ({record.get('because')}: "
                      f"{record.get('why')})")
        for key, wait in (status.get("waiting_for_a_pod") or {}).items():
            print(f"  (╯°□°)╯ NO POD for {key}: {wait['misses']} miss(es) since {wait['since']}, next create at "
                  f"{wait['next_create_at']}; " + (f"NO POD since {wait['no_pod_since']}: its jobs FAIL at "
                                                    f"{wait['fails_at']}" if wait["no_pod_since"] else
                                                    "its pods run its jobs: they wait, never fail") +
                  f"\n      last: {wait['last']}")
    for stage, state, age_s in daemon.queue(job_contract.read_config()["queue_directories"]):
        print(f"{stage}  {state:<8} {age_s / 60:6.1f} min")
