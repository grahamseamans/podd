# 2026-11-27 -- a datacenter where a pod did not boot is avoided (branch boot-failure-memory)

Reported on cork topic 2212 (audio/waveguide): podd opened an RTX 3090 in EU-CZ-1 four times; the first ran out the
450 s boot deadline ($0.063), and ~/.local/state/podd/slow-datacenters.json is named `.broken`.

## What the journal and the transcripts show
- `slow-datacenters.json.broken` is a leftover: 193ac85 (2026-10-22 06:34) made the file per pod kind; podd's restart
  at 06:52 found the old flat file, said so, and set it aside as `.broken`. 8f52328 (2026-10-29) renamed the memory to
  `avoided-datacenters.json`. That file works; it was absent because nothing had been recorded.
- The gap: only "slow" and "old driver" pods got their datacenter avoided. A pod past the boot deadline
  (Pool.cancel_boots) and a pod whose pod.sh create failed after RunPod made it were misses with no memory, and the
  replacement opened in the cancel's own pass could not have avoided it anyway (11-13: US-CA-2 twice in a row).
- 3 of the 4 EU-CZ-1 pods on 11-27 (07:53, 07:58, 07:59, 08:01 PST creates) were deleted by the client with
  `pod.sh delete` at 07:57:47, 08:01:21 and 08:01:59 while podd was booting them; one (36x4lcv9sihmim) had booted
  in 198 s, and the delete broke a job's rsync (the job FAILED). podd's deadline did not cause those.
- Journal 2026-11-06..27 boots (260): median ~115 s; slowest GPU boots all 3090s in EU-CZ-1 (169-289 s, 630 s on
  11-06, deadline on 11-27); every CPU pod past the deadline in US-CA-2 (11-11, 11-13 x2).

## Steps
1. DID_NOT_BOOT_DATACENTER_HOURS (12) and `because` "did not boot" (never forgiven while a set waits). -- done
2. Pod.session_of_this_boot / recorded_datacenter: the datacenter from pod.sh's session file before the facts. -- done
3. cancel_boots records the avoidance at the deadline, before the same pass's open_pods. -- done
4. step's "boot failed": avoid only when RunPod made the pod and pod.sh create failed it (not podd's own steps after
   the create, not a pod whose record is gone). -- done
5. Tests (no pod, no RunPod); pod.sh's boot poll overridable (POD_SH_POLL_SECONDS) so the pod.sh tests take 7 s, not
   42 s; the runner takes name words. -- done
6. Push the branch; merge waits for the user's word.

## Open (guesses, say if wrong)
- TODO 12 h is the slow / old-driver figure; the evidence (EU-CZ-1 3090 slow on 11-06, 11-26 and 11-27) says a
  datacenter stays bad for days, so longer may be right.
- TODO the avoidance is per datacenter, not per (datacenter, card): pod.sh posts one card per create, so a per-card
  dataCenterIds is possible if an avoided datacenter's other cards are ever missed.
