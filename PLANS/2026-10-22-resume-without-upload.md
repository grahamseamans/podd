# 2026-10-22 -- a resumed attempt never uploads what earlier attempts finished

Small plan: delete once done and older than 24 h.

Seen 2026-10-21/22: a resumable job requeued part-way shipped its kept outputs (acoustic-solve's raw/, unreduced
because the stopped attempt never reached the reduction: ~24 MB a frequency) from this box back to the next pod over
this box's ~1 MB/s uplink. 15 attempts ended in a 600 s upload timeout counted as "pod broke" (each burning an
attempt and marking a healthy pod BROKEN); 2026-10-22 13:26Z seven walls chunks uploaded 0.2-2.2 GB each at once.

Design (README "A job"):
1. JOB gains `resume_from` (required with `resumable`): the small files inside the outputs that say what is done.
   podd ships the inputs + those, never the rest of the outputs.
2. The pull lays every output over the stage's copy (rsync without --delete): a file the attempt wrote replaces the
   stage's, every other file stays. Kept work + the new attempt's files = the whole result, merged at home.
3. timings gain ship_bytes (what went up), so the uplink cost stays measured.

acoustic-solve (its run-job.sh): blab solves into .solving/ (never an output); a collector moves each finished
frequency into raw/ (renumbered to raw's frequency list, reduced to the kept nodes per frequency when the room keeps
nodes) and rewrites raw/manifest.json; first and resumed attempts are one path. resume_from = raw/manifest.json +
solve.log.
