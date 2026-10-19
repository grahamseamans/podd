# THE ONE PLACE podd keeps where the RunPod secrets live (sourced by pod.sh; daemon.py asks it for $SECRETS). A path
# convention only: no key is ever written in this repo (.gitignore keeps secret-shaped files out).
# $SECRETS (default ~/.secrets, mode 700) holds: runpod_api_key (the account's API key), runpod_ssh_ed25519 (+ .pub,
# one of the account's ssh keys: RunPod injects the account's keys into every pod), and pod.sh's own
# runpod_pods/<name>.{id,session.json,lease} (one per pod) and pod-watchdog.log.
SECRETS=${PODD_SECRETS:-$HOME/.secrets}
[ -d "$SECRETS" ] || { echo "secrets.sh: (╯°□°)╯ no secrets directory $SECRETS (make it, mode 700, with runpod_api_key in it)" >&2; exit 1; }
