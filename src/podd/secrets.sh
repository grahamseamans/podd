# THE ONE PLACE podd keeps where the RunPod secrets live (sourced by pod.sh; daemon.py asks it for $SECRETS). A path
# convention only: no key is ever written in this repo (.gitignore keeps secret-shaped files out).
# $SECRETS (default ~/.secrets/runpod, mode 700: the box keeps every secret in ~/.secrets/<owner>/) holds: api_key (the
# account's API key), ssh_ed25519 (+ .pub, one of the account's ssh keys: RunPod injects the account's keys into every
# pod), and pod.sh's own pods/<name>.{id,session.json,lease} (one per pod). The watchdog's log is not a secret: it is
# ~/.local/state/podd/pod-watchdog.log, beside PODS.json.
SECRETS=${PODD_SECRETS:-$HOME/.secrets/runpod}
[ -d "$SECRETS" ] || { echo "secrets.sh: (╯°□°)╯ no secrets directory $SECRETS (make it, mode 700, with api_key in it)" >&2; exit 1; }
