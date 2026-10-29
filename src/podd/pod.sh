#!/usr/bin/env bash
# THE PODS against the RunPod API: create, delete, status, the ssh alias, and the watchdog that deletes a pod whose
# owner (podd) died. Several pods at once, each by NAME (podd names them podd-g1 .. podd-g<n> and podd-c1 .. podd-c<n>): its own id file,
# price, lease and ssh alias (runpod-<name>, one ControlMaster connection each). Nothing about jobs or projects: podd
# (src/podd/daemon.py, the pod job runner) decides which image, which cards, how many CPUs, and runs the
# jobs (architecture/README.md). A pod runs the image it is created with (no template: POST /pods takes imageName,
# references/runpod/runpod-rest-api-openapi-v1-2026-10-10.json PodCreateInput). No volume: the pod's own disk holds a
# job while it runs and podd pulls results home, so a pod can be in ANY datacenter on the first free card (user,
# 2026-10-05: "we made the image, we don't care what datacenter"). Reads the API key from $SECRETS
# (secrets.sh, the one place) and keeps each pod's files in $SECRETS/runpod_pods/.
#
#   pod.sh avail <cards>         # JSON: each card's $/h and the datacenters stocking it now
#   pod.sh create <name> <image> <cards> <min vCPU> <cuda versions> <disk GB> [<avoid datacenters>]
#                                             # a pod of <image> on the first free card of <cards> (comma-separated,
#                                             # in the order RunPod tries them), >= <min vCPU> per GPU, on a host whose
#                                             # driver runs one of <cuda versions> (comma-separated), a container disk
#                                             # of <disk GB> (whole GB; podd's container_disk_gb), any datacenter
#                                             # but <avoid datacenters> (comma-separated: podd's slow ones);
#                                             # save its id, arm its watchdog (deleted $MAX_POD_HOURS (default 2) after
#                                             # its lease was last renewed: podd renews it while the pod has work), wait
#                                             # for ssh, write its ssh alias. Exit 75 (EX_TEMPFAIL): RunPod has no
#                                             # instance of any card offered now (a stock race, not an error: retry)
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
PODS_DIRECTORY=$SECRETS/runpod_pods
API=https://rest.runpod.io/v1
[ -s $SECRETS/runpod_api_key ] || { echo "pod.sh: no API key in $SECRETS/runpod_api_key" >&2; exit 1; }
# The key reaches curl only through a header file (curl -H @file); printf is a builtin, so it is never in any argv.
auth() { printf 'Authorization: Bearer %s\n' "$(<$SECRETS/runpod_api_key)"; }

rest() {  # rest <method> <path> [json-body]; fails loudly on any non-2xx, printing RunPod's body
  local out code
  out=$(curl -sS -w '\n%{http_code}' -X "$1" -H @<(auth) -H 'Content-Type: application/json' \
        ${3:+--data "$3"} "$API$2")
  code=${out##*$'\n'}; out=${out%$'\n'*}
  if [[ $code != 2* ]]; then
    echo "RunPod $1 $2 -> HTTP $code: $out" >&2
    # RunPod has no machine for the request right now (HTTP 500 "create pod: There are no instances currently
    # available", 2026-10-14; for a CPU pod "There are no longer any instances available with the requested
    # specifications", 2026-10-15, every 16-vCPU flavor while 8 vCPU created): a stock race, exit 75 (EX_TEMPFAIL) so
    # the caller retries instead of failing
    if [[ $1 == POST && $2 == /pods && ( ${out,,} == *"no instances"* || ${out,,} == *"no longer any instances"* ) ]]; then
      exit 75
    fi
    exit 1
  fi
  printf '%s' "$out"
}

# Every datacenter RunPod's create takes (PodCreateInput.dataCenterIds' enum,
# references/runpod/runpod-rest-api-openapi-v1-2026-10-10.json). Without an avoid list create sends no dataCenterIds
# (any datacenter, new ones too); with one it sends this list less the avoided (a datacenter added after 2026-10-10 is
# then not asked for: re-mirror the API and update this list).
RUNPOD_DATACENTERS=EU-RO-1,CA-MTL-1,EU-SE-1,US-IL-1,EUR-IS-1,EU-CZ-1,US-TX-3,EUR-IS-2,US-KS-2,US-GA-2,US-WA-1,US-TX-1,CA-MTL-3,EU-NL-1,US-TX-4,US-CA-2,US-NC-1,OC-AU-1,US-DE-1,EUR-IS-3,CA-MTL-2,AP-JP-1,EUR-NO-1,EU-FR-1,US-KS-3,US-GA-1,AP-IN-1,US-MD-1,US-MO-1,US-MO-2

# whole_gb <disk GB>: the container disk asked for, a positive whole number (RunPod's containerDiskInGb is an
# integer), or a loud exit: never a default chosen here (podd's container_disk_gb is the one place)
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

# create_the_pod <name> <PodCreateInput JSON>: POST /pods, save the id, arm the watchdog, record price + RAM +
# container disk + datacenter, wait for ssh, write the alias. GPU and CPU pods alike (create, create-cpu).
create_the_pod() {
  local body=$2 t0 created pod lease_s cost_per_hr machine datacenter memory_gb container_disk_gb
  mkdir -p -m 700 $PODS_DIRECTORY
  [ ! -s $POD_ID_FILE ] || { echo "pod.sh: a pod named $NAME is recorded already: $(pod_id) (pod.sh delete $NAME first)" >&2; exit 1; }
  t0=$(date +%s)
  created=$(rest POST /pods "$body")
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
    >> $SECRETS/pod-watchdog.log 2>&1 &
  echo "pod $NAME ($pod): watchdog armed, deleted ${MAX_POD_HOURS:-2} h (MAX_POD_HOURS) after its lease was last renewed"
  # the price is on the create response (Pod.costPerHr); the datacenter is on the pod's machine, which GET /pods/{id}
  # returns only with includeMachine=true (references/runpod/runpod-rest-api-openapi-v1-2026-10-10.json)
  # Neither read may stop the create here: a missing field is said loudly and recorded as null.
  cost_per_hr=$(field 'd.get("costPerHr") or ""' <<<"$created")
  [ -n "$cost_per_hr" ] || echo "pod.sh: (・_・;) the create response carries no costPerHr: $created" >&2
  machine=$(rest GET "/pods/$pod?includeMachine=true")
  datacenter=$(field '(d.get("machine") or {}).get("dataCenterId") or ""' <<<"$machine")
  [ -n "$datacenter" ] || echo "pod.sh: (・_・;) GET /pods/$pod?includeMachine=true named no machine.dataCenterId: $machine" >&2
  # the pod's RAM (Pod.memoryInGb): the container has no cgroup memory limit (memory.max "max", MemTotal the host's
  # 540 GB, 2026-10-14), so this is the RAM podd packs against
  memory_gb=$(field 'd.get("memoryInGb") or ""' <<<"$created")
  [ -n "$memory_gb" ] || echo "pod.sh: (・_・;) the create response carries no memoryInGb: $created" >&2
  # the pod's container disk as RunPod made it (Pod.containerDiskInGb): podd matches requires.disk_gb_min against it
  container_disk_gb=$(field 'd.get("containerDiskInGb") or ""' <<<"$created")
  [ -n "$container_disk_gb" ] || echo "pod.sh: (・_・;) the create response carries no containerDiskInGb: $created" >&2
  COST=$cost_per_hr T0=$t0 DC=$datacenter MEMORY=$memory_gb DISK=$container_disk_gb python3 -c 'import json, os; print(json.dumps({"cost_per_hr": float(os.environ["COST"]) if os.environ["COST"] else None, "created_at_unix_s": int(os.environ["T0"]), "datacenter": os.environ["DC"] or None, "memory_gb": float(os.environ["MEMORY"]) if os.environ["MEMORY"] else None, "container_disk_gb": float(os.environ["DISK"]) if os.environ["DISK"] else None}))' > $POD_SESSION_FILE
  echo "pod $NAME ($pod) created in ${datacenter:-an unnamed datacenter} at \$$cost_per_hr/h; waiting for its ssh port"
  until rest GET /pods/$pod | field 'd.get("publicIp") and (d.get("portMappings") or {}).get("22") or ""' | grep -q .; do
    [ $(( $(date +%s) - t0 )) -lt 900 ] || { echo "pod $NAME ($pod): no public ssh port after 15 min" >&2; exit 1; }
    sleep 5
  done
  "$0" ssh-config "$NAME"
  until ssh -o ConnectTimeout=5 -o BatchMode=yes $SSH_ALIAS true 2>/dev/null; do
    [ $(( $(date +%s) - t0 )) -lt 900 ] || { echo "pod $NAME ($pod): ssh never answered" >&2; exit 1; }
    sleep 5
  done
  echo "pod $NAME: ssh up $(( $(date +%s) - t0 )) s after create; container started (image pulled) $(( $(ssh $SSH_ALIAS 'date -d "$(ps -o lstart= -p 1)" +%s') - t0 )) s after create"
  ssh $SSH_ALIAS 'if command -v nvidia-smi >/dev/null; then nvidia-smi --query-gpu=name,memory.total --format=csv,noheader; else echo "no card (a CPU pod): cpuset $(cat /sys/fs/cgroup/cpuset.cpus.effective 2>/dev/null || echo none), cgroup cpu.max $(cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo none)"; fi'
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
  python3 - "$1" "$HOME/.ssh/config" "$SSH_ALIAS" "${2:-}" "${3:-}" "$SECRETS/runpod_ssh_ed25519" <<'EOF'
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
  IMAGE=${3:?pod.sh create <name> <image> <cards> <min vCPU> <cuda versions> <disk GB>}
  CARDS=${4:?pod.sh create <name> <image> <cards> <min vCPU> <cuda versions> <disk GB>}
  MIN_VCPU=${5:?pod.sh create <name> <image> <cards> <min vCPU> <cuda versions> <disk GB>}
  CUDA_VERSIONS=${6:?pod.sh create <name> <image> <cards> <min vCPU> <cuda versions> <disk GB>}
  DISK_GB=$(whole_gb "${7:-}")
  DATACENTERS=$(datacenters_json "${8:-}")
  # PodCreateInput (references/runpod/runpod-rest-api-openapi-v1-2026-10-10.json): ports 22/tcp only (full ssh, no
  # Jupyter); containerDiskInGb <disk GB>, podd's default 30 (daemon.py GPU_CONTAINER_DISK_GB; more when a job's
  # requires.disk_gb_min asks: the image does not count against it: / showed 16 MB used with /opt at 5.7 GB,
  # 2026-10-04); volumeInGb 0 (no pod volume: results come home); env {}: no PUBLIC_KEY, so RunPod injects the
  # account's ssh keys ($SECRETS/runpod_ssh_ed25519.pub is one of them); no dockerEntrypoint/StartCmd: the image's.
  # allowedCudaVersions: the host driver must run the image's CUDA runtime. dataCenterIds only to avoid podd's slow
  # datacenters (RUNPOD_DATACENTERS less them); else none: any datacenter.
  body=$(IMAGE=$IMAGE CARDS=$CARDS MIN_VCPU=$MIN_VCPU CUDA_VERSIONS=$CUDA_VERSIONS DISK_GB=$DISK_GB NAME=$NAME DATACENTERS=$DATACENTERS python3 -c '
import json, os
body = {"name": os.environ["NAME"], "imageName": os.environ["IMAGE"], "cloudType": "SECURE",
    "gpuCount": 1, "gpuTypeIds": os.environ["CARDS"].split(","), "gpuTypePriority": "custom",
    "minVCPUPerGPU": int(os.environ["MIN_VCPU"]), "allowedCudaVersions": os.environ["CUDA_VERSIONS"].split(","),
    "ports": ["22/tcp"], "containerDiskInGb": int(os.environ["DISK_GB"]), "volumeInGb": 0, "volumeMountPath": "/workspace", "env": {},
    "supportPublicIp": True}
if json.loads(os.environ["DATACENTERS"]) is not None:
    body["dataCenterIds"] = json.loads(os.environ["DATACENTERS"])
print(json.dumps(body))')
  create_the_pod "$NAME" "$body"
  ;;
create-cpu)
  name_the_pod "${2:-}"
  IMAGE=${3:?pod.sh create-cpu <name> <image> <flavors> <vCPU> <disk GB>}
  FLAVORS=${4:?pod.sh create-cpu <name> <image> <flavors> <vCPU> <disk GB>}
  VCPU=${5:?pod.sh create-cpu <name> <image> <flavors> <vCPU> <disk GB>}
  DISK_GB=$(whole_gb "${6:-}")
  DATACENTERS=$(datacenters_json "${7:-}")
  # PodCreateInput with computeType CPU (references/runpod/runpod-rest-api-openapi-v1-2026-10-10.json; the docs
  # mirror references/runpod/, tools/mirror_runpod_docs.sh): cpuFlavorIds in podd's order (cpuFlavorPriority
  # custom), vcpuCount (a power of two: api-reference-v2 BaseCpuConfig), the RAM is the flavor's per-vCPU multiple.
  # containerDiskInGb <disk GB>, podd's default 20 (daemon.py CPU_CONTAINER_DISK_GB): the CPU flavors cap the disk at
  # 10 GB per vCPU (cpu3*: GraphQL cpuFlavors.diskLimitPerVcpu, 2026-10-15), so 20 fits every size from 2 vCPU, and a
  # larger requires.disk_gb_min gets the vCPUs for it from podd's cpu_pod_plan (or no plan, said there, never a
  # create RunPod refuses); the image does not count against it. No CUDA, no card.
  body=$(IMAGE=$IMAGE FLAVORS=$FLAVORS VCPU=$VCPU DISK_GB=$DISK_GB NAME=$NAME DATACENTERS=$DATACENTERS python3 -c '
import json, os
body = {"name": os.environ["NAME"], "imageName": os.environ["IMAGE"], "cloudType": "SECURE",
    "computeType": "CPU", "cpuFlavorIds": os.environ["FLAVORS"].split(","), "cpuFlavorPriority": "custom",
    "vcpuCount": int(os.environ["VCPU"]), "ports": ["22/tcp"], "containerDiskInGb": int(os.environ["DISK_GB"]),
    "volumeInGb": 0,
    "volumeMountPath": "/workspace", "env": {}, "supportPublicIp": True}
if json.loads(os.environ["DATACENTERS"]) is not None:
    body["dataCenterIds"] = json.loads(os.environ["DATACENTERS"])
print(json.dumps(body))')
  create_the_pod "$NAME" "$body"
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
  [ -s $SECRETS/runpod_ssh_ed25519 ] || { echo "pod.sh: no ssh key $SECRETS/runpod_ssh_ed25519" >&2; exit 1; }
  pod=$(pod_id)
  read -r ip port < <(rest GET /pods/$pod | field 'd["publicIp"] + " " + str(d["portMappings"]["22"])')
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
  rest GET /pods | field '"\n".join("%s %s %s $%s/h" % (p.get("id"), p.get("name"), p.get("desiredStatus"), p.get("costPerHr")) for p in d) or "(no pods on the account)"'
  ;;
status)
  name_the_pod "${2:-}"
  pod=$(pod_id)
  rest GET /pods/$pod | field '{k: d.get(k) for k in ("id", "name", "desiredStatus", "costPerHr", "publicIp", "portMappings")}'
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
if rate is None:
    print("pod %s deleted after %.1f min; (・_・;) its create response carried no costPerHr, so no cost" % (sys.argv[2], minutes))
else:
    print("pod %s deleted after %.1f min at $%.2f/h: $%.4f this session" % (sys.argv[2], minutes, rate, rate * minutes / 60))' $POD_SESSION_FILE "$NAME ($pod)"
  else
    echo "pod $NAME ($pod) deleted (pod.sh (・_・;) no $POD_SESSION_FILE: its create never recorded the price, so no cost)"
  fi
  ssh_config remove
  rm -f $POD_SESSION_FILE $POD_LEASE_FILE; rm $POD_ID_FILE
  ;;
*)
  sed -n '2,32p' "$0"; exit 1
  ;;
esac
