# 2026-10-22 -- the stock wait and the slow-host rule (two bugs seen 2026-10-21/22)

Small plan: delete once done and older than 24 h.

## Bug 1: a stock wait failed jobs while the set had pods

Seen: 2026-10-22 00:15Z, 20 CPU jobs FAILED "no pod for 60 min" while 3 pods of their set ran other chunks
(journal: "drain ... on 3 pod(s) up or booting"). The clock was `first_miss_s` (the first refused create), never
reset while the set's pods ran. README: "A set with no pod for this long FAILS" -- the clock must be the time the set
has had NO pod. 01:32Z and 02:33Z: 0 pods, but every create avoided 15 datacenters (bug 2).

Fix: StockWait.podless_since (None while a ready / slow pod serves the set or a job of it runs); fail only past
max_wait_for_stock_minutes of that. The wait is in the status file and `podd status`.

## Bug 2: the slow rule banned 15 datacenters in a day

Measured (podd's journal 2026-10-21 + the DONE files and `attempts` of one client's stages):
- one command string ran small, medium and large jobs (its inputs differ)
  (expected_s 1273 / 1633 / 3218 for 124 units): rates per unit were compared across job sizes. 16:13:50 local: five
  pods marked SLOW at once against a 3.9 s/unit reference measured on a smaller job.
- the reference was the single FASTEST pod. Whole-job paces (wall / expected_s) of finished jobs: one job family
  0.24-0.35 (US-IL-1 0.24 vs US-GA-2 0.35 = 1.46x; US-GA-2 was banned at 1.50x), another 0.26-0.52. The fastest is no
  "normal" pod.
- per-unit pace (rate x total / expected_s) of the attempts podd stopped as slow: EU-CZ-1 / EU-NL-1 0.41-0.48 (the
  normal band); US-MO-1 0.69-0.79 and US-CA-2 0.77 (~1.9x the median ~0.40: really slow).
- US-CA-2's first ban came from expected_s alone (392 s, later 1273 s by the client: the estimate was 3x off).
- CPU-pod bans also cut GPU creates (one list), and the probe after a stock miss still avoided 15 datacenters.

Fix:
1. Compare PACE (seconds per unit over the job's own expected seconds per unit), not seconds per unit.
2. Reference = the MEDIAN pace of at least MIN_REFERENCE_PODS other pods (one value per pod: its median), same load,
   measured within REFERENCE_MAX_AGE_SECONDS. No ban from expected_s alone (that pace only scales deadlines).
3. A set short of stock never has a pod marked slow (nothing would replace it), and its create avoids no datacenter.
4. A job is moved off a slow pod only when what it saves exceeds a boot: (left units) x rate x (1 - 1/slowness) >
   boot_seconds.
5. Avoided datacenters are per pod kind (gpu / cpu).

Tests: replay of the 2026-10-21 shapes (cross-size, fastest-vs-median, last datacenter with stock), the stock clock.
