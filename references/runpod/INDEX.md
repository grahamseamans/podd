# runpod

What podd's code cites. Only this INDEX is in git (keep scripts, not scrapes); the files are re-made on disk:

- `docs/` + `MIRRORED.txt` — RunPod's whole documentation source (github.com/runpod/docs, the Mintlify source of
  docs.runpod.io), cloned by `tools/mirror_runpod_docs.sh`, which records the commit in `MIRRORED.txt` (2026-10-15:
  3fa043f06c7c313c6b296bdb650caace1758dd0e). What podd reads from it:
  - `docs/api-reference/openapi.json` — REST v1 (https://rest.runpod.io/v1): `PodCreateInput.computeType` (GPU | CPU),
    `cpuFlavorIds` (cpu3c, cpu3g, cpu3m, cpu5c, cpu5g, cpu5m), `cpuFlavorPriority`, `vcpuCount`: `pod.sh create-cpu`.
  - `docs/api-reference-v2/openapi.json` — REST v2 (https://api.runpod.io/v2): `GET /v2/catalog/cpus?product=POD&
    include=AVAILABILITY` (`CpuType`: `price.securePerVcpu`, `ramGbPerVcpu`, `vcpu.min/max`, `availability`), and
    `BaseCpuConfig.vcpuCount` "must be a power of two": `pod.sh avail-cpu`, `daemon.cpu_pod_plan`.
  - `docs/flash/configuration/cpu-types.mdx` — the instance ids (`cpu3c-8-16` = 8 vCPU, 16 GB) and the container disk
    cap (vCPU x 10 GB on cpu3*), which `pod.sh create-cpu`'s 20 GB disk fits from 2 vCPU.
  - `docs/references/cpu-types.mdx` — the CPU models behind the flavors.
- `runpod-rest-api-openapi-v1-2026-10-10.json` — the REST v1 OpenAPI spec as fetched 2026-10-10 from
  https://rest.runpod.io/v1/openapi.json (info.version 0.1.0), the one `pod.sh create` was written against:
  `PodCreateInput` (imageName, ports, dataCenterIds' enum = pod.sh's RUNPOD_DATACENTERS), `Pod.costPerHr` on the
  POST /pods response, `machine.dataCenterId` only with `GET /pods/{podId}?includeMachine=true`. Re-fetch with
  `curl -sS https://rest.runpod.io/v1/openapi.json` (today's copy is also `docs/api-reference/openapi.json`).
