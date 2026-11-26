#!/usr/bin/env bash
# THE PODS against the RunPod API: create, delete, status, the ssh alias, and the watchdog that deletes a pod whose
# owner (podd) died. Several pods at once, each by NAME (podd names them podd-g1 .. podd-g<n> and podd-c1 .. podd-c<n>): its own id file,
# price, lease and ssh alias (runpod-<name>, one ControlMaster connection each). Nothing about jobs or projects: podd
# (src/podd/daemon.py, the pod job runner) decides which image, which cards, how many CPUs, and runs the
# jobs (architecture/README.md). RunPod's REST v2 (https://api.runpod.io/v2; references/runpod/docs/api-reference-v2/
# openapi.json and migrate-from-v1.mdx: REST v1, rest.runpod.io, was retired 2026-11-15 and its host is gone). A pod
# runs the image it is created with (no template: POST /v2/pods takes image, CreatePodRequest). No volume (no mounts):
# the pod's own disk holds a
# job while it runs and podd pulls results home, so a pod can be in ANY datacenter on the first free card (user,
# 2026-10-05: "we made the image, we don't care what datacenter"). Reads the API key from $SECRETS
# (secrets.sh, the one place) and keeps each pod's files in $SECRETS/pods/.
#
#   pod.sh avail <cards>         # JSON: each card's $/h and the datacenters stocking it now
#   pod.sh create <name> <image> <cards> <min vCPU> <disk GB> [<avoid datacenters>]
#                                             # a pod of <image> on the first free card of <cards> (comma-separated,
#                                             # asked one at a time in that order), >= <min vCPU> per GPU, on any host
#                                             # (podd measures the driver's CUDA version on the pod), a container disk
#                                             # of <disk GB> (whole GB; podd's container_disk_gb), any datacenter
#                                             # but <avoid datacenters> (comma-separated: podd's avoided ones);
#                                             # save its id, arm its watchdog (deleted $MAX_POD_HOURS (default 2) after
#                                             # its lease was last renewed: podd renews it while the pod has work), wait
#                                             # for ssh (exit 1 once its container fails to start twice:
#                                             # watch_the_container, or RunPod says the pod is in ERROR), write its
#                                             # ssh alias. Exit 75 (EX_TEMPFAIL): RunPod placed no card offered now (a
#                                             # stock race, not an error: retry); exit 1: a refusal of the request
#   pod.sh avail-cpu <flavors>   # JSON: each CPU flavor's $/vCPU-h, RAM per vCPU, vCPU range, stock
#   pod.sh create-cpu <name> <image> <flavors> <vCPU> <disk GB> [<avoid datacenters>]
#                                             # a CPU pod (no card) of <image>: the first free flavor of <flavors>
#                                             # (comma-separated, in that order), exactly <vCPU> vCPUs, a container
#                                             # disk of <disk GB>; the rest as create
#   pod.sh ssh-config <name>     # (re)write the runpod-<name> block of ~/.ssh/config
#   pod.sh id <name> | status <name> | delete <name>
#   pod.sh recorded              # the names of every recorded pod (one per line)
#   pod.sh account               # every pod the RunPod ACCOUNT has, recorded or not (the leak check)
#
# Pods are never restarted: delete and create another (a stopped pod is pinned to its host's GPUs). Pod disk is gone
# with the pod: never leave results only on the pod.
set -euo pipefail

source "$(dirname "$0")/secrets.sh"
PODS_DIRECTORY=$SECRETS/pods
WATCHDOG_LOG_FILE=$HOME/.local/state/podd/pod-watchdog.log   # a log, not a secret: beside PODS.json
API=https://api.runpod.io/v2
[ -s $SECRETS/api_key ] || { echo "pod.sh: no API key in $SECRETS/api_key" >&2; exit 1; }
# The key reaches curl only through a header file (curl -H @file); printf is a builtin, so it is never in any argv.
auth() { printf 'Authorization: Bearer %s\n' "$(<$SECRETS/api_key)"; }

# ask <method> <path> [json-body]: one REST v2 call; sets ANSWER_CODE (HTTP status) and ANSWER (RunPod's body). A call
# that reaches no server (2026-11-26: rest.runpod.io NXDOMAIN broke every create) is a loud exit 1.
ask() {
  local out
  if ! out=$(curl -sS -w '\n%{http_code}' -X "$1" -H @<(auth) -H 'Content-Type: application/json' \
        ${3:+--data "$3"} "$API$2"); then
    echo "pod.sh: (╯°□°)╯ RunPod $1 $API$2: curl got no answer (curl's error above)" >&2
    exit 1
  fi
  ANSWER_CODE=${out##*$'\n'}; ANSWER=${out%$'\n'*}
}

rest() {  # rest <method> <path> [json-body]; fails loudly on any non-2xx, printing RunPod's body
  ask "$@"
  if [[ $ANSWER_CODE != 2* ]]; then
    echo "RunPod $1 $2 -> HTTP $ANSWER_CODE: $ANSWER" >&2
    exit 1
  fi
  printf '%s' "$ANSWER"
}

# RunPod placing no machine for a create, by its words. REST v2 answers capacity with HTTP 400 and only a human
# `detail` (POST /v2/pods' description: "this GPU and data center combination could not be placed"; no
# machine-readable code). REST v1 said HTTP 500 "There are no instances currently available" (2026-10-14) and, for a
# CPU size, "There are no longer any instances available with the requested specifications" (2026-10-15). v2's exact
# wording is not documented: a capacity refusal these miss is still a refusal (exit 1, podd's "did not boot", also a
# miss of the set's wait), only named less well. Lowercase fragments.
NO_STOCK_WORDS=("no instances" "no longer any instances" "could not be placed" "capacity" "not available"
                "unavailable")
said_no_stock() { local word; for word in "${NO_STOCK_WORDS[@]}"; do [[ ${1,,} == *"$word"* ]] && return 0; done; return 1; }

# Every datacenter RunPod's create took (REST v1 PodCreateInput.dataCenterIds' enum,
# references/runpod/runpod-rest-api-openapi-v1-2026-10-10.json; v2's CreatePodRequest.dataCenterIds is a free list of
# strings, GET /v2/catalog/datacenters lists them). Without an avoid list create sends no dataCenterIds
# (any datacenter, new ones too); with one it sends this list less the avoided (a datacenter added after 2026-10-10 is
# then not asked for: re-mirror the API and update this list).
RUNPOD_DATACENTERS=EU-RO-1,CA-MTL-1,EU-SE-1,US-IL-1,EUR-IS-1,EU-CZ-1,US-TX-3,EUR-IS-2,US-KS-2,US-GA-2,US-WA-1,US-TX-1,CA-MTL-3,EU-NL-1,US-TX-4,US-CA-2,US-NC-1,OC-AU-1,US-DE-1,EUR-IS-3,CA-MTL-2,AP-JP-1,EUR-NO-1,EU-FR-1,US-KS-3,US-GA-1,AP-IN-1,US-MD-1,US-MO-1,US-MO-2

# whole_gb <disk GB>: the container disk asked for, a positive whole number (RunPod's `disk` is an integer), or a loud exit: never a default chosen here (podd's container_disk_gb is the one place)
whole_gb() {
  [[ $1 =~ ^[1-9][0-9]*$ ]] || { echo "pod.sh: (╯°□°)╯ the container disk must be a positive whole number of GB, got '$1'" >&2; exit 1; }
  printf '%s' "$1"
}

# datacenters_json <avoid, comma-separated>: the dataCenterIds to send, as JSON ("null": none, any datacenter). An
# avoided name RunPod does not list is said; avoiding every one is an error.
datacenters_json() {
  AVOID=$1 ALL=$RUNPOD_DATACENTERS python3 -c '
import json, os, sys
avoid = [d for d in os.environ["AVOID"].split(",") if d]
everything = os.environ["ALL"].split(",")
if not avoid:
    print("null"); sys.exit()
for unknown in sorted(set(avoid) - set(everything)):
    print(f"pod.sh: (・_・;) asked to avoid {unknown}, which is not in RUNPOD_DATACENTERS", file=sys.stderr)
allowed = [d for d in everything if d not in avoid]
if not allowed:
    sys.exit("pod.sh: (╯°□°)╯ every datacenter is avoided: " + ",".join(avoid))
print(json.dumps(allowed))'
}

# field '<python expr over d>': the expressions are this script's own literals, never input, so eval is safe here
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {}, {'d': d}))" "$1"; }

# THE CONTAINER WHILE A POD BOOTS. RunPod's GraphQL Pod.runtime (references/runpod/docs/sdks/graphql/manage-pods.mdx:
# runtime { uptimeInSeconds ports }) is null until the pod's container first starts; the REST v1 Pod had no such
# field, and its lastStatusChange / desiredStatus said nothing of a failed start ("Rented by User: <time>" / RUNNING
# throughout, observed). What RunPod answered, 2026-10-30, polled every ~5-30 s on throwaway L4 pods (US-MO-2,
# EUR-IS-*):
#   - a container that starts: runtime null while the image pulls (nvidia/cuda base: 13 s; podd's ai_research image:
#     ~6 min, null every reading), then uptimeInSeconds 6, 37, 69 ... 342 (only rising, never below 0), ports listed;
#   - a container whose start fails (dockerEntrypoint a missing file: runc's "OCI runtime create failed", the same
#     error an image refusing an old driver gives, NVIDIA_REQUIRE_CUDA): uptimeInSeconds -5, -6, 0, -4, -3, 0, 1, -10,
#     -3 with ports null (RunPod restarting it; one flicker at 1 with the ssh port mapped), for 5 min;
#   - a container that starts and dies at once (dockerStartCmd a missing file): -9, -8, -5, 1, -14, -10, 1, ...
# An old-driver host could not be had on demand (any create lands on any host), so that case is these two.
# A FAILED START is a reading below 0, or below the one before (a restart). FAILED_CONTAINER_STARTS of them fail the
# create (exit 1: podd's "did not boot", a miss of the set's wait; it deletes the pod).
FAILED_CONTAINER_STARTS=2

# container_uptime <pod id>: the container's GraphQL runtime.uptimeInSeconds, one word: an integer, "none" (runtime
# null: not started yet) or "unknown" (RunPod did not answer the question: said loudly, the wait goes on, bounded by
# its 15 min). An answer of any other shape -- the pod gone, a runtime without a whole uptime -- is a loud exit 1.
container_uptime() {
  local answer
  if ! answer=$(curl -sS --fail-with-body -H 'Content-Type: application/json' -H @<(auth) https://api.runpod.io/graphql \
      -d "{\"query\":\"query { pod(input: {podId: \\\"$1\\\"}) { runtime { uptimeInSeconds } } }\"}" 2>&1); then
    echo "pod.sh: (・_・;) RunPod GraphQL did not answer for pod $1's container (the wait goes on): ${answer:0:300}" >&2
    echo unknown
    return
  fi
  ANSWER=$answer POD=$1 python3 -c '
import json, os, sys
said = os.environ["ANSWER"]
try:
    answer = json.loads(said)
except ValueError:
    answer = None
if not isinstance(answer, dict) or answer.get("errors") or not isinstance(answer.get("data"), dict) \
        or "pod" not in answer["data"]:
    print("pod.sh: (・_・;) RunPod GraphQL did not answer for pod %s'"'"'s container (the wait goes on): %s"
          % (os.environ["POD"], said[:300]), file=sys.stderr)
    print("unknown"); sys.exit()
pod = answer["data"]["pod"]
if pod is None:
    sys.exit("pod.sh: (╯°□°)╯ RunPod GraphQL has no pod %s any more (deleted while it booted?): %s"
             % (os.environ["POD"], said[:300]))
runtime = pod.get("runtime") if isinstance(pod, dict) else "not a pod"
if runtime is None:
    print("none"); sys.exit()
uptime = runtime.get("uptimeInSeconds") if isinstance(runtime, dict) else None
if type(uptime) is not int:
    sys.exit("pod.sh: (╯°□°)╯ RunPod GraphQL answered a container runtime pod.sh does not know for pod %s (a whole "
             "uptimeInSeconds or null expected): %s" % (os.environ["POD"], said[:300]))
print(uptime)'
}

# watch_the_container <pod id>: one reading of the container (container_uptime); a failed start (below 0, or below the
# last reading) is said and counted in failed_container_starts, and the FAILED_CONTAINER_STARTS-th fails the create.
# create_the_pod sets failed_container_starts=0 and last_container_uptime="" before its waits.
watch_the_container() {
  local uptime
  uptime=$(container_uptime "$1")
  [[ $uptime =~ ^-?[0-9]+$ ]] || return 0
  if (( uptime < 0 )) || { [[ -n $last_container_uptime ]] && (( uptime < last_container_uptime )); }; then
    failed_container_starts=$(( failed_container_starts + 1 ))
    echo "pod.sh: (・_・;) pod $NAME ($1): its container is not staying up (RunPod's runtime.uptimeInSeconds" \
         "$uptime after ${last_container_uptime:-nothing}), failed start $failed_container_starts of" \
         "$FAILED_CONTAINER_STARTS" >&2
    if (( failed_container_starts >= FAILED_CONTAINER_STARTS )); then
      echo "pod $NAME ($1): its container failed to start $failed_container_starts times (RunPod restarting it:" \
           "an image refusing the host's driver, OCI runtime create failed, looks like this)" >&2
      exit 1
    fi
  fi
  last_container_uptime=$uptime
}

# create_the_pod <name> <gpu|cpu> <candidates, comma-separated> <CreatePodRequest JSON, its gpu / cpu without id>:
# POST /v2/pods once per candidate (card or CPU flavor) in order until one is placed -- v2 takes ONE gpu type or CPU
# flavor per create, no fallback list (migrate-from-v1.mdx, "Removed": "retry with the next type in your own code")
# -- then save the id, arm the watchdog, record price + RAM + container disk + datacenter, wait for ssh, write the
# alias. GPU and CPU pods alike (create, create-cpu). Per candidate (POST /v2/pods' "Which failures are worth
# retrying"): 422 (a body v2 does not take) and 402 (no balance) end the create at once, exit 1; any other refusal
# tries the next. Every candidate refused for stock (said_no_stock, 429, 5xx): exit 75; else exit 1.
create_the_pod() {
  local kind=$2 candidate body refusals="" refused_for_more_than_stock="" t0 created="" pod lease_s cost_per_hr
  local datacenter card memory_gb container_disk_gb candidates
  mkdir -p -m 700 $PODS_DIRECTORY && mkdir -p "${WATCHDOG_LOG_FILE%/*}"
  [ ! -s $POD_ID_FILE ] || { echo "pod.sh: a pod named $NAME is recorded already: $(pod_id) (pod.sh delete $NAME first)" >&2; exit 1; }
  t0=$(date +%s)
  IFS=, read -r -a candidates <<<"$3"
  for candidate in "${candidates[@]}"; do
    body=$(BODY=$4 KIND=$kind ID=$candidate python3 -c 'import json, os; body = json.loads(os.environ["BODY"]); body[os.environ["KIND"]]["id"] = os.environ["ID"]; print(json.dumps(body))')
    ask POST /pods "$body"
    if [[ $ANSWER_CODE == 201 ]]; then created=$ANSWER; break; fi
    echo "RunPod POST /pods ($kind $candidate) -> HTTP $ANSWER_CODE: $ANSWER" >&2
    if [[ $ANSWER_CODE == 422 || $ANSWER_CODE == 402 || $ANSWER_CODE == 2* ]]; then
      echo "pod.sh: (╯°□°)╯ RunPod refused the create itself (422: a body v2 does not take; 402: no balance; 202: a" \
           "queued pod, which pod.sh never asks for): no other candidate can succeed" >&2
      exit 1
    fi
    refusals+="${refusals:+ / }$candidate: HTTP $ANSWER_CODE"
    if ! said_no_stock "$ANSWER" && [[ $ANSWER_CODE != 429 && $ANSWER_CODE != 5* ]]; then
      refused_for_more_than_stock=yes
    fi
  done
  if [ -z "$created" ]; then
    if [ -z "$refused_for_more_than_stock" ]; then
      echo "pod $NAME: RunPod placed none of $3 now, no instances ($refusals)" >&2
      exit 75
    fi
    echo "pod $NAME: RunPod refused every one of $3 ($refusals), not all for stock (every candidate the same 400:" \
         "the request itself, POST /v2/pods' description)" >&2
    exit 1
  fi
  pod=$(field 'd["id"]' <<<"$created")
  echo "$pod" > $POD_ID_FILE
  # Watchdog, armed the moment the id is saved (before anything here can fail): whatever the caller does, this pod
  # is deleted once its lease is $MAX_POD_HOURS (default 2) old. 2026-10-05: a hung job and a patient wait loop left
  # a 5090 running 8 h for 11 min of work ($7.9). It runs here, not on the pod (the pod never holds the API key),
  # checks once a minute, and exits when the pod is no longer the one recorded under this name.
  touch $POD_LEASE_FILE
  lease_s=$(python3 -c "print(int(float('${MAX_POD_HOURS:-2}') * 3600))")
  nohup bash -c "while [ \"\$(cat $POD_ID_FILE 2>/dev/null)\" = '$pod' ]; do \
      if [ \$(( \$(date +%s) - \$(stat -c %Y $POD_LEASE_FILE 2>/dev/null || echo 0) )) -ge $lease_s ]; then \
        echo \"pod.sh watchdog: pod $NAME ($pod)'s lease ($POD_LEASE_FILE) is ${MAX_POD_HOURS:-2} h old, deleting it\"; \
        '$0' delete '$NAME'; exit; fi; sleep 60; done" \
    >> "$WATCHDOG_LOG_FILE" 2>&1 &
  echo "pod $NAME ($pod): watchdog armed, deleted ${MAX_POD_HOURS:-2} h (MAX_POD_HOURS) after its lease was last renewed"
  # What the v2 Pod says (openapi.json Pod): `cost` ($/h); `dataCenterId` (assigned by the scheduler: read again
  # from GET /v2/pods/{id} when the create answer has none yet); the card or flavor rented, `gpu.id` / `cpu.id`
  # (2026-11-06: two pods that ran nothing left no card name in the journal); the pod's RAM, `gpu.memory` /
  # `cpu.memory` (the container has no cgroup memory limit, memory.max "max", MemTotal the host's 540 GB, 2026-10-14,
  # so this is the RAM podd packs against); its container disk, `disk` (podd matches requires.disk_gb_min against
  # it). None of these reads may stop the create here: a missing field is said loudly and recorded as null.
  cost_per_hr=$(field 'd.get("cost") or ""' <<<"$created")
  [ -n "$cost_per_hr" ] || echo "pod.sh: (・_・;) the create response carries no cost: $created" >&2
  datacenter=$(field 'd.get("dataCenterId") or ""' <<<"$created")
  if [ -z "$datacenter" ]; then
    datacenter=$(rest GET "/pods/$pod" | field 'd.get("dataCenterId") or ""')
    [ -n "$datacenter" ] || echo "pod.sh: (・_・;) neither the create nor GET /pods/$pod named a dataCenterId: $created" >&2
  fi
  card=$(field '(d.get("gpu") or {}).get("id") or (d.get("cpu") or {}).get("id") or ""' <<<"$created")
  [ -n "$card" ] || echo "pod.sh: (・_・;) the create response named no gpu.id or cpu.id: $created" >&2
  memory_gb=$(field '(d.get("gpu") or {}).get("memory") or (d.get("cpu") or {}).get("memory") or ""' <<<"$created")
  [ -n "$memory_gb" ] || echo "pod.sh: (・_・;) the create response carries no gpu.memory or cpu.memory: $created" >&2
  container_disk_gb=$(field 'd.get("disk") or ""' <<<"$created")
  [ -n "$container_disk_gb" ] || echo "pod.sh: (・_・;) the create response carries no disk: $created" >&2
  COST=$cost_per_hr T0=$t0 DC=$datacenter CARD=$card MEMORY=$memory_gb DISK=$container_disk_gb python3 -c 'import json, os; print(json.dumps({"cost_per_hr": float(os.environ["COST"]) if os.environ["COST"] else None, "created_at_unix_s": int(os.environ["T0"]), "datacenter": os.environ["DC"] or None, "card": os.environ["CARD"] or None, "memory_gb": float(os.environ["MEMORY"]) if os.environ["MEMORY"] else None, "container_disk_gb": float(os.environ["DISK"]) if os.environ["DISK"] else None}))' > $POD_SESSION_FILE
  echo "pod $NAME ($pod) created: ${card:-an unnamed card} in ${datacenter:-an unnamed datacenter} at \$$cost_per_hr/h; waiting for its ssh port"
  # Every wait for the pod also watches its container (watch_the_container): one that does not stay up fails the
  # create in seconds, not after the 15 min below. Both waits: a failing container's ssh port flickers up (observed
  # 2026-10-30), so the port alone can pass the first wait into an ssh that never answers.
  failed_container_starts=0 last_container_uptime=""
  wait_for_the_ssh_port "$pod"
  "$0" ssh-config "$NAME"
  until ssh -o ConnectTimeout=5 -o BatchMode=yes $SSH_ALIAS true 2>/dev/null; do
    watch_the_container "$pod"
    [ $(( $(date +%s) - t0 )) -lt 900 ] || { echo "pod $NAME ($pod): ssh never answered" >&2; exit 1; }
    sleep 5
  done
  echo "pod $NAME: ssh up $(( $(date +%s) - t0 )) s after create; container started (image pulled) $(( $(ssh $SSH_ALIAS 'date -d "$(ps -o lstart= -p 1)" +%s') - t0 )) s after create"
  ssh $SSH_ALIAS 'if command -v nvidia-smi >/dev/null; then nvidia-smi --query-gpu=name,memory.total --format=csv,noheader; else echo "no card (a CPU pod): cpuset $(cat /sys/fs/cgroup/cpuset.cpus.effective 2>/dev/null || echo none), cgroup cpu.max $(cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo none)"; fi'
}

# ssh_endpoint <pod id>: one word-pair. "<host> <port>": the pod's direct ssh (v2 Pod.ssh.direct: null until 22/tcp
# is mapped; the docs: check status RUNNING before connecting); "GONE <status>": RunPod put the pod in ERROR or
# TERMINATED (migrate-from-v1.mdx: "Handle ERROR in polling loops"); nothing: not yet, or RunPod did not answer this
# time (said loudly; the caller's wait is bounded).
ssh_endpoint() {
  ask GET "/pods/$1"
  if [[ $ANSWER_CODE != 2* ]]; then
    echo "pod.sh: (・_・;) RunPod GET /pods/$1 -> HTTP $ANSWER_CODE: ${ANSWER:0:300} (the wait goes on)" >&2
    return 0
  fi
  field '"GONE " + d["status"] if d.get("status") in ("ERROR", "TERMINATED") else "%s %s" % (d["ssh"]["direct"]["host"], d["ssh"]["direct"]["port"]) if d.get("status") == "RUNNING" and (d.get("ssh") or {}).get("direct") else ""' <<<"$ANSWER"
}

# wait_for_the_ssh_port <pod id>: until ssh_endpoint names the port, watching the container (watch_the_container)
# every turn; a pod RunPod calls ERROR / TERMINATED, or no port after 15 min, fails the create (exit 1: podd's "did
# not boot")
wait_for_the_ssh_port() {
  local endpoint
  while :; do
    endpoint=$(ssh_endpoint "$1")
    if [[ $endpoint == GONE* ]]; then echo "pod $NAME ($1): RunPod says the pod is ${endpoint#GONE } while it boots" >&2; exit 1; fi
    [ -z "$endpoint" ] || return 0
    watch_the_container "$1"
    [ $(( $(date +%s) - t0 )) -lt 900 ] || { echo "pod $NAME ($1): no public ssh port after 15 min" >&2; exit 1; }
    sleep 5
  done
}

# one pod's files, by its name (a name is podd's: letters, digits, dashes)
name_the_pod() {
  NAME=${1:-}
  [[ $NAME =~ ^[a-z0-9-]+$ ]] || { echo "pod.sh: a pod name ([a-z0-9-]+) is required, got '${NAME}'" >&2; exit 1; }
  POD_ID_FILE=$PODS_DIRECTORY/$NAME.id
  POD_SESSION_FILE=$PODS_DIRECTORY/$NAME.session.json    # its price and create time: delete says what it cost
  # The watchdog's lease: the pod dies MAX_POD_HOURS after this file's mtime. create touches it; podd touches it
  # while the pod has work (podd.py Pod.renew_lease), so a batch longer than MAX_POD_HOURS keeps its pod and a
  # forgotten pod (podd dead, or a hand-made pod nobody renews) still dies MAX_POD_HOURS after the last renewal.
  POD_LEASE_FILE=$PODS_DIRECTORY/$NAME.lease
  SSH_ALIAS=runpod-$NAME
}
# every command reads the id as `pod=$(pod_id)` on its own line first: under set -e an assignment from a failing
# substitution exits, where `rest GET /pods/$(pod_id)` would call GET /pods/ with an empty id and go on
pod_id() { [ -s $POD_ID_FILE ] || { echo "pod.sh: no pod named $NAME ($POD_ID_FILE)" >&2; exit 1; }; cat $POD_ID_FILE; }

# ssh_config write|remove: pod.sh owns the whole Host $SSH_ALIAS block of ~/.ssh/config
ssh_config() {
  mkdir -p -m 700 "$HOME/.ssh"
  python3 - "$1" "$HOME/.ssh/config" "$SSH_ALIAS" "${2:-}" "${3:-}" "$SECRETS/ssh_ed25519" <<'EOF'
import os, re, sys
mode, path, alias, ip, port, key = sys.argv[1:]
text = open(path).read() if os.path.exists(path) else ""
block = "" if mode == "remove" else (
    f"Host {alias}\n  HostName {ip}\n  Port {port}\n  User root\n  IdentityFile {key}\n  IdentitiesOnly yes\n"
    f"  BatchMode yes\n  StrictHostKeyChecking accept-new\n  ConnectTimeout 10\n"
    f"  ServerAliveInterval 15\n  ServerAliveCountMax 4\n"
    f"  ControlMaster auto\n  ControlPath ~/.ssh/cm-%C\n  ControlPersist 600\n")
old = re.search(rf"^Host {re.escape(alias)}\n(?:[ \t]+.*\n?)*\n?", text, re.M)
if old:
    text = text[:old.start()] + (block + "\n" if block else "") + text[old.end():]
elif block:
    text = (text.rstrip("\n") + "\n\n" if text else "") + block
with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as config:
    config.write(text)
os.chmod(path, 0o600)  # ssh refuses a group-writable config, and O_CREAT's mode only applies to a new file
EOF
}

case "${1:-}" in
avail)
  GPU_TYPES=${2:?pod.sh avail <cards, comma-separated> (the cards of ~/.config/podd/config.json)}
  # One JSON object, podd's input to the price cap (podd.py affordable_cards): per card asked, its secure-cloud price
  # (GraphQL gpuTypes.securePrice, $/h: what create was charged, 2026-10-14: A40 0.59, L40S 1.09) and the
  # datacenters stocking it (GraphQL dataCenters.gpuAvailability.stockStatus: High / Medium / Low; None = none). The
  # REST API has neither. A card RunPod does not list is null: said, never guessed.
  curl -sS --fail-with-body -H 'Content-Type: application/json' -H @<(auth) https://api.runpod.io/graphql \
    -d '{"query":"query { gpuTypes { id securePrice } dataCenters { id gpuAvailability { gpuTypeId stockStatus } } }"}' |
  GPU_TYPES=$GPU_TYPES python3 -c '
import json, os, sys
answer = json.load(sys.stdin)
if answer.get("errors") or not answer.get("data"):
    sys.exit("pod.sh avail: RunPod GraphQL answered " + json.dumps(answer)[:500])
wanted = os.environ["GPU_TYPES"].split(",")
price = {g["id"]: g["securePrice"] for g in answer["data"]["gpuTypes"]}
offers = {gpu: None if gpu not in price else {"price_per_hour": price[gpu], "stock": {}} for gpu in wanted}
for dc in answer["data"]["dataCenters"]:
    for g in dc["gpuAvailability"] or []:
        if offers.get(g["gpuTypeId"]) is not None and g["stockStatus"] not in (None, "None"):
            offers[g["gpuTypeId"]]["stock"][dc["id"]] = g["stockStatus"]
print(json.dumps(offers, indent=1))'
  ;;
create)
  name_the_pod "${2:-}"
  IMAGE=${3:?pod.sh create <name> <image> <cards> <min vCPU> <disk GB>}
  CARDS=${4:?pod.sh create <name> <image> <cards> <min vCPU> <disk GB>}
  MIN_VCPU=${5:?pod.sh create <name> <image> <cards> <min vCPU> <disk GB>}
  DISK_GB=$(whole_gb "${6:-}")
  DATACENTERS=$(datacenters_json "${7:-}")
  # CreatePodRequest (references/runpod/docs/api-reference-v2/openapi.json; v2 answers a field it does not take with
  # a 422): cloud SECURE; gpu {count 1, minVcpuCountPerGpu <min vCPU>}, its id set per card by create_the_pod; ports
  # 22/tcp only (full ssh, no Jupyter; v2 has no default ports); disk <disk GB>, podd's default 30 (daemon.py
  # GPU_CONTAINER_DISK_GB; more when a job's requires.disk_gb_min asks: the image does not count against it: / showed
  # 16 MB used with /opt at 5.7 GB, 2026-10-04); no mounts (no pod volume: results come home; podd's /workspace is a
  # directory on the container disk); env {}; startSsh: RunPod injects the account's ssh keys as PUBLIC_KEY
  # ($SECRETS/ssh_ed25519.pub is one of them; v2 does so only when asked, v1 always did); no entrypoint/cmd: the
  # image's. NO gpu.allowedCudaVersions / minCudaVersion: podd measures the driver on the pod and deletes one below
  # min_cuda_version (daemon.py old_driver; 2026-10-29: v1's allowedCudaVersions enum stopped at 13.0 and shut out
  # every newer host). dataCenterIds only to avoid podd's avoided datacenters (RUNPOD_DATACENTERS less them); else
  # none: any datacenter.
  body=$(IMAGE=$IMAGE MIN_VCPU=$MIN_VCPU DISK_GB=$DISK_GB NAME=$NAME DATACENTERS=$DATACENTERS python3 -c '
import json, os
body = {"name": os.environ["NAME"], "image": os.environ["IMAGE"], "cloud": "SECURE",
    "gpu": {"count": 1, "minVcpuCountPerGpu": int(os.environ["MIN_VCPU"])},
    "ports": ["22/tcp"], "disk": int(os.environ["DISK_GB"]), "env": {}, "startSsh": True}
if json.loads(os.environ["DATACENTERS"]) is not None:
    body["dataCenterIds"] = json.loads(os.environ["DATACENTERS"])
print(json.dumps(body))')
  create_the_pod "$NAME" gpu "$CARDS" "$body"
  ;;
create-cpu)
  name_the_pod "${2:-}"
  IMAGE=${3:?pod.sh create-cpu <name> <image> <flavors> <vCPU> <disk GB>}
  FLAVORS=${4:?pod.sh create-cpu <name> <image> <flavors> <vCPU> <disk GB>}
  VCPU=${5:?pod.sh create-cpu <name> <image> <flavors> <vCPU> <disk GB>}
  DISK_GB=$(whole_gb "${6:-}")
  DATACENTERS=$(datacenters_json "${7:-}")
  # CreatePodRequest with cpu (references/runpod/docs/api-reference-v2/openapi.json; the docs mirror
  # references/runpod/, tools/mirror_runpod_docs.sh): cpu {vcpuCount <vCPU>} (a power of two: BaseCpuConfig), its id
  # set per flavor by create_the_pod in podd's order; RunPod derives the RAM from the flavor's per-vCPU multiple. The
  # rest as create. disk <disk GB>, podd's default 20 (daemon.py CPU_CONTAINER_DISK_GB): the CPU flavors cap the disk at
  # 10 GB per vCPU (cpu3*: GraphQL cpuFlavors.diskLimitPerVcpu, 2026-10-15), so 20 fits every size from 2 vCPU, and a
  # larger requires.disk_gb_min gets the vCPUs for it from podd's cpu_pod_plan (or no plan, said there, never a
  # create RunPod refuses); the image does not count against it. No CUDA, no card.
  body=$(IMAGE=$IMAGE VCPU=$VCPU DISK_GB=$DISK_GB NAME=$NAME DATACENTERS=$DATACENTERS python3 -c '
import json, os
body = {"name": os.environ["NAME"], "image": os.environ["IMAGE"], "cloud": "SECURE",
    "cpu": {"vcpuCount": int(os.environ["VCPU"])}, "ports": ["22/tcp"], "disk": int(os.environ["DISK_GB"]),
    "env": {}, "startSsh": True}
if json.loads(os.environ["DATACENTERS"]) is not None:
    body["dataCenterIds"] = json.loads(os.environ["DATACENTERS"])
print(json.dumps(body))')
  create_the_pod "$NAME" cpu "$FLAVORS" "$body"
  ;;
avail-cpu)
  FLAVORS=${2:?pod.sh avail-cpu <flavors, comma-separated> (the cpu_flavors of ~/.config/podd/config.json)}
  # One JSON object, podd's input to cpu_pod_plan: per flavor asked, RunPod's REST v2 catalog
  # (GET /v2/catalog/cpus?product=POD&include=AVAILABILITY, references/runpod/docs/api-reference-v2/openapi.json
  # CpuType): price.securePerVcpu ($/vCPU-h), ramGbPerVcpu, vcpu.min / max, availability (HIGH / MEDIUM / LOW, or
  # null). A flavor RunPod does not list is null: said, never guessed.
  curl -sS --fail-with-body -H @<(auth) 'https://api.runpod.io/v2/catalog/cpus?product=POD&include=AVAILABILITY' |
  FLAVORS=$FLAVORS python3 -c '
import json, os, sys
answer = json.load(sys.stdin)
if not isinstance(answer.get("cpus"), list):
    sys.exit("pod.sh avail-cpu: RunPod answered " + json.dumps(answer)[:500])
listed = {cpu["id"]: cpu for cpu in answer["cpus"]}
offers = {}
for flavor in os.environ["FLAVORS"].split(","):
    cpu = listed.get(flavor)
    offers[flavor] = None if cpu is None else {
        "price_per_vcpu_hour": cpu["price"]["securePerVcpu"], "ram_gb_per_vcpu": cpu["ramGbPerVcpu"],
        "vcpu_min": cpu["vcpu"]["min"], "vcpu_max": cpu["vcpu"]["max"],
        "stock": cpu.get("availability") if cpu.get("availability") not in (None, "NONE") else None}
print(json.dumps(offers, indent=1))'
  ;;
ssh-config)
  # Every ssh / scp / rsync to the pod goes through its alias, so the timeouts live here once: ConnectTimeout bounds a
  # dead host, ServerAlive* drop a silent connection after ~60 s with exit 255 (study 3, 2026-10-07: one dropped
  # connection hung a trial ~18 min). accept-new: every pod is a new host key. ONE connection per pod:
  # ControlMaster/ControlPersist make every later ssh/scp/rsync a channel on it (~1.5 s of handshake each before, nine
  # per job; PLANS/2026-10-13-pod-manager.md); %C is per host and port, so each pod has its own master.
  name_the_pod "${2:-}"
  [ -s $SECRETS/ssh_ed25519 ] || { echo "pod.sh: no ssh key $SECRETS/ssh_ed25519" >&2; exit 1; }
  pod=$(pod_id)
  endpoint=$(ssh_endpoint "$pod")
  [[ -n $endpoint && $endpoint != GONE* ]] || { echo "pod.sh: pod $NAME ($pod) has no direct ssh port (${endpoint:-not RUNNING with 22/tcp mapped})" >&2; exit 1; }
  read -r ip port <<<"$endpoint"
  ssh_config write "$ip" "$port"
  ssh-keygen -R "[$ip]:$port" >/dev/null 2>&1 || true
  echo "$SSH_ALIAS -> $ip:$port"
  ;;
id)
  name_the_pod "${2:-}"
  pod_id
  ;;
recorded)
  for id_file in $PODS_DIRECTORY/*.id; do [ -s "$id_file" ] && basename "$id_file" .id; done 2>/dev/null || true
  ;;
account)
  # GET /v2/pods: {"pods": [...], "pagination": {hasNextPage, nextCursor}}, up to 1000 a page: every page is read
  # (migrate-from-v1.mdx, "List responses")
  cursor="" listed=0
  while :; do
    page=$(rest GET "/pods?limit=1000${cursor:+&cursor=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$cursor")}")
    lines=$(field '"\n".join("%s %s %s $%s/h" % (p.get("id"), p.get("name"), p.get("status"), p.get("cost")) for p in d["pods"])' <<<"$page")
    [ -z "$lines" ] || { echo "$lines"; listed=1; }
    cursor=$(field '((d.get("pagination") or {}).get("nextCursor") or "") if (d.get("pagination") or {}).get("hasNextPage") else ""' <<<"$page")
    [ -n "$cursor" ] || break
  done
  [ "$listed" = 1 ] || echo "(no pods on the account)"
  ;;
status)
  name_the_pod "${2:-}"
  pod=$(pod_id)
  rest GET /pods/$pod | field '{**{k: d.get(k) for k in ("id", "name", "status", "cost", "dataCenterId")}, "ssh": (d.get("ssh") or {}).get("direct")}'
  ssh -o ConnectTimeout=5 $SSH_ALIAS 'if command -v nvidia-smi >/dev/null; then nvidia-smi --query-gpu=name,memory.total --format=csv,noheader; else echo "no card (a CPU pod)"; fi'
  ;;
delete)
  name_the_pod "${2:-}"
  pod=$(pod_id)
  rest DELETE /pods/$pod >/dev/null
  if [ -s $POD_SESSION_FILE ]; then
    python3 -c '
import json, sys, time
session = json.load(open(sys.argv[1]))
minutes = (time.time() - session["created_at_unix_s"]) / 60
rate = session["cost_per_hr"]
card = session.get("card") or "card not recorded"
if rate is None:
    print("pod %s (%s) deleted after %.1f min; (・_・;) its create response carried no cost per hour, so no total" % (sys.argv[2], card, minutes))
else:
    print("pod %s (%s) deleted after %.1f min at $%.2f/h: $%.4f this session" % (sys.argv[2], card, minutes, rate, rate * minutes / 60))' $POD_SESSION_FILE "$NAME ($pod)"
  else
    echo "pod $NAME ($pod) deleted (pod.sh (・_・;) no $POD_SESSION_FILE: its create never recorded the price, so no cost)"
  fi
  ssh_config remove
  rm -f $POD_SESSION_FILE $POD_LEASE_FILE; rm $POD_ID_FILE
  ;;
*)
  sed -n '2,35p' "$0"; exit 1
  ;;
esac
