#!/bin/bash
# ON A POD: podd-run.sh <job-dir> -- runs one podd job's command and nothing else (podd ships it once per pod; podd.py
# starts it with `nohup setsid`, so it leads a session of its own and everything the command spawns is in it).
#
#   <job-dir>/.podd/command   the JOB's `command` (podd wrote it), run by bash in <job-dir>
#   <job-dir>/command.log     its stdout and stderr
#   <job-dir>/.podd/pid       this script's pid = the job's session id: podd kills the job with `pkill -9 -s <it>`
#   <job-dir>/.podd/exit      written last, atomically: {"exit_status": N, "wall_s": S}; podd polls for it
#
# Nothing about time: podd times the job and kills it (src/podd/daemon.py). Nothing about any project.
# What it needs of an image (so: what any podd image carries): bash, awk, ps/pkill/pgrep (procps), sshd, rsync.
set -u
cd "$1"
echo $$ > .podd/pid
# setsid makes the session in-process when the caller is not a group leader (a background job of a non-interactive
# shell is not), so the session id is this pid. If not, podd's kill would miss the command: refuse to run it.
session=$(ps -o sid= -p $$ | tr -d ' ')
if [ "$session" != "$$" ]; then
  echo "podd-run.sh (╯°□°)╯ not the leader of its own session (pid $$, session '$session'): podd could not kill" \
       "this job alone; the command was NOT run" > command.log
  printf '{"exit_status": 125, "wall_s": 0}\n' > .podd/exit.writing && mv .podd/exit.writing .podd/exit
  exit 125
fi
start=$(date +%s.%N)
bash .podd/command > command.log 2>&1 < /dev/null
status=$?
end=$(date +%s.%N)
awk -v status="$status" -v start="$start" -v end="$end" \
  'BEGIN { printf "{\"exit_status\": %d, \"wall_s\": %.1f}\n", status, end - start }' > .podd/exit.writing
mv .podd/exit.writing .podd/exit
