# runpod

What podd's code cites. Only this INDEX is in git (keep scripts, not scrapes); the files are re-made on disk:

- `docs/` + `MIRRORED.txt` — RunPod's whole documentation source (github.com/runpod/docs, the Mintlify source of
  docs.runpod.io), cloned by `tools/mirror_runpod_docs.sh`, which records the commit in `MIRRORED.txt` (2026-11-26:
  c845f73c104cb4b69f4caa11f27180533ad8546f, committed 2026-11-25). What podd reads from it:
  - `docs/api-reference-v2/openapi.json` — REST v2 (https://api.runpod.io/v2), the API pod.sh speaks since
    2026-11-26: `POST /v2/pods` (`CreatePodRequest`: `image`, `cloud`, `gpu {id, count, minVcpuCountPerGpu}` or
    `cpu {id, vcpuCount}`, ONE card or flavor per create, `ports`, `disk`, `env`, `startSsh`, `dataCenterIds`; unknown
    fields are a 422; capacity is a 400 with only a human `detail`, the operation's description), `GET /v2/pods/{id}`
    (`Pod`: `status`, `cost`, `dataCenterId`, `gpu.memory` / `cpu.memory`, `disk`, `ssh.direct {host, port}`),
    `GET /v2/pods` (`{"pods": [...], "pagination"}`), `DELETE /v2/pods/{id}` (204): `pod.sh create / create-cpu /
    status / account / delete / ssh-config`. Also `GET /v2/catalog/cpus?product=POD&include=AVAILABILITY`
    (`CpuType`: `price.securePerVcpu`, `ramGbPerVcpu`, `vcpu.min/max`, `availability`) and
    `BaseCpuConfig.vcpuCount` "must be a power of two": `pod.sh avail-cpu`, `daemon.cpu_pod_plan`.
  - `docs/api-reference-v2/migrate-from-v1.mdx` — v1 retired 2026-11-15 (its host rest.runpod.io was NXDOMAIN on
    2026-11-26), the endpoint mapping, the breaking changes pod.sh follows (no GPU fallback list, `startSsh`, no
    default ports, `status` not `desiredStatus`, wrapped and paginated lists, RFC 9457 errors).
  - `docs/api-reference/openapi.json` — REST v1 (https://rest.runpod.io/v1, gone), kept for the history in pod.sh's
    comments.
  - `docs/flash/configuration/cpu-types.mdx` — the instance ids (`cpu3c-8-16` = 8 vCPU, 16 GB) and the container disk
    cap (vCPU x 10 GB on cpu3*), which `pod.sh create-cpu`'s 20 GB disk fits from 2 vCPU.
  - `docs/references/cpu-types.mdx` — the CPU models behind the flavors.
  - `docs/sdks/graphql/manage-pods.mdx` — GraphQL `pod(input: {podId}) { runtime { uptimeInSeconds ports } }`:
    `pod.sh create`'s watch of the container while a pod boots (watch_the_container). The docs give no meaning
    to a negative uptime; what a failing container answers was observed on throwaway pods, 2026-10-30, and is
    written in pod.sh (THE CONTAINER WHILE A POD BOOTS). `lastStatusChange` (REST and GraphQL) stayed
    "Rented by User: <time>" through a failing start: not a signal.
- `runpod-rest-api-openapi-v1-2026-10-10.json` — the REST v1 OpenAPI spec as fetched 2026-10-10 from
  https://rest.runpod.io/v1/openapi.json (info.version 0.1.0; the host is gone since v1's retirement), the one
  `pod.sh create` was written against until 2026-11-26, and the source of RUNPOD_DATACENTERS:
  `PodCreateInput` (imageName, ports, dataCenterIds' enum = pod.sh's RUNPOD_DATACENTERS), `Pod.costPerHr` on the
  POST /pods response, `machine.dataCenterId` only with `GET /pods/{podId}?includeMachine=true`. v2's spec: `https://api.runpod.io/v2/openapi.json` (also
  `docs/api-reference-v2/openapi.json`).
