#!/usr/bin/env bash
# Mirror RunPod's whole documentation source (github.com/runpod/docs, the Mintlify source of docs.runpod.io) into
# references/runpod/docs/ (on disk only; .gitignore keeps everything under references/ but INDEX.md out of git: keep
# scripts, not scrapes). Records the commit and date in references/runpod/MIRRORED.txt (INDEX.md cites it). What podd
# reads from it: the REST v1 and v2 OpenAPI specs (pod.sh create / create-cpu / avail-cpu), the CPU flavors.
#   usage: tools/mirror_runpod_docs.sh            (re-run to refresh; it replaces the mirror whole)
set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
mirror_dir="$repo_root/references/runpod"
scratch_dir="$(mktemp -d)"
trap 'rm -rf "$scratch_dir"' EXIT
git clone --quiet --depth 1 https://github.com/runpod/docs "$scratch_dir/docs" \
  || { echo "mirror_runpod_docs.sh: (╯°□°)╯ git clone failed" >&2; exit 1; }
mkdir -p "$mirror_dir"
rm -rf "$mirror_dir/docs"
cp -r "$scratch_dir/docs" "$mirror_dir/docs"
rm -rf "$mirror_dir/docs/.git"
commit_and_date="$(git -C "$scratch_dir/docs" log -1 --format='%H %cI')"
printf 'commit %s\nmirrored %s\n' "$commit_and_date" "$(date -Iseconds)" > "$mirror_dir/MIRRORED.txt"
echo "mirror_runpod_docs.sh: mirrored $commit_and_date -> $mirror_dir (ﾉ◕ヮ◕)ﾉ"
