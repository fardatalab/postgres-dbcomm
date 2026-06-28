# DPU DMA Backend Homer Service Implementation Checkpoint

## Current Status

This note tracks landed implementation stages for the DPU-pull Homer migration.
The target design remains
[`dpu_dma_backend_homer_service_current_scheduler_design.md`](../../../future-directions/citus/transport/dpu_dma_backend_homer_service_current_scheduler_design.md).

As of Stage 4, the default frontend and service path is still the existing
host-process SHM/RDMA implementation. The DPU frontend path exists only behind
the explicit hidden GUC `citus.enable_experimental_homer_dpu_frontend`; when the
GUC is enabled, the frontend runs a host-side bridge-memory smoke check and then
fails real Homer API calls with a deliberate not-implemented error. The
service-side DPU DMA scheduler path is opt-in behind
`HOMER_SERVICE_ENABLE_DPU_DMA=1`; it creates the Stage 3 no-DOCA engine, reads
zero-work facts, and wires no-op bounded DPU actions through `machine-baseline`.
It still does not submit DOCA DMA tasks or drain a DOCA PE.

## Stage 1: Bridge ABI Header

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:26`
  defines the bridge protocol version, cache-line size, feature flags, and ring
  flags.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:42`
  defines workload classes for command, completion, and byte-stream work.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:49`
  defines ring direction, and
  `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:56`
  defines entry lifecycle state.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:91`
  defines the one-cache-line bridge control header.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:112`
  defines the host-owned publish line. The host writes body fields first and
  release-publishes `publishedEpoch`.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:134`
  defines the DPU-owned credit/completion line. The intended DMA protocol is
  body DMA writes followed by a separate one-word DMA write to
  `publishedCreditEpoch` on the same ordered DOCA DMA context.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:154`
  defines setup-time ring descriptors exchanged over COMCH.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:182`
  pins cache-line sizes, alignments, publication-word offsets, and descriptor
  size with static assertions.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:202`
  adds inline helpers for generation matching, publication epochs, monotonic
  frontiers, fixed-slot credit, and byte-ring wrap splitting.

The host-only checker is:

- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_bridge_abi_check.c:32`
  checks exact publication epoch acceptance and stale generation rejection.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_bridge_abi_check.c:47`
  checks monotonic frontier rejection.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_bridge_abi_check.c:54`
  checks byte-ring no-wrap, wrap, zero-length, invalid reverse range, and
  over-capacity cases.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_bridge_abi_check.c:82`
  checks fixed-slot reuse only after consumed credit advances.
- `/data/dbcomm/citus-dbcomm/Makefile:33` adds the `dpu-bridge-abi-check`
  target, and `/data/dbcomm/citus-dbcomm/Makefile:74` compiles the checker.

## Validation

Stage 1 was validated on `homer-dpu-migration` with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make dpu-bridge-abi-check
sudo -n -u dbcomm make -j8 service-bin client-bin
git diff --check
```

Observed result:

- `homer_dpu_bridge_abi_check: ok`
- `service-bin` and `client-bin` completed successfully.
- `git diff --check` reported no whitespace errors.

The initial attempt to run the checker as the interactive shell user failed with
`cannot open output file build/homer/homer_dpu_bridge_abi_check: Permission
denied` because `build/homer` is owned by `dbcomm`. The successful validation
used the runbook-required `sudo -n -u dbcomm` build style and did not change
ownership.

## Stage 2: Host Frontend DMA Skeleton

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:24`
  defines the hidden GUC backing variable `EnableExperimentalHomerDpuFrontend`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c:2593`
  registers `citus.enable_experimental_homer_dpu_frontend`, defaulting false.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:58`
  allocates an aligned in-process bridge block with one control header, host
  publish lines, and DPU credit lines. This is not DOCA mmap registration yet.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:150`
  publishes a host-owned line by writing body fields first and release-storing
  the `publishedEpoch` word last.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:174`
  reads a DPU-owned credit line only after the publication epoch and generation
  match the expected values.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:208`
  runs the Stage 2 bridge-memory smoke path used by the experimental frontend
  guard.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:639`
  is the current channel-selection guard. With the GUC disabled it returns and
  the existing SHM path continues. With the GUC enabled it runs the smoke and
  raises an explicit DPU-not-implemented error before any SHM mapping.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:714`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:794`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:861`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:922`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:992`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:1073`,
  and `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:1213`
  call that guard from the current local-service frontend operations.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_frontend_dma_smoke.c:32` is the
  standalone host-only bridge-memory smoke. `/data/dbcomm/citus-dbcomm/Makefile:37`
  adds the `frontend-dma-smoke` target.

Stage 2 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make frontend-dma-smoke
sudo -n -u dbcomm make -C src/backend/distributed utils/homer/homer_frontend_dma.o
sudo -n -u dbcomm make -j8
sudo -n -u dbcomm make -j8 service-bin client-bin frontend-dma-smoke dpu-bridge-abi-check
git diff --check
```

Observed result:

- `homer_frontend_dma_smoke: ok`
- `homer_dpu_bridge_abi_check: ok`
- `utils/homer/homer_frontend_dma.o` compiled cleanly.
- The broader Citus build completed and linked `utils/homer/homer_frontend_dma.o`
  into `citus.so`.
- `service-bin` and `client-bin` remained buildable/up to date.
- `git diff --check` reported no whitespace errors.

During validation, the first backend-object compile failed because
`mul_size()` was not visible from `homer_frontend_dma.c`; replacing it with
explicit `Size` arithmetic then exposed that a separate `MaxAllocSize` check was
also not visible in this context. The final code keeps the skeleton independent
of those backend allocation helpers because it currently allocates with
`posix_memalign()` and only needs to guard `Size` addition overflow.

## Current Caveats

- Stage 2 does not register DOCA mmap objects, connect COMCH, or submit DMA
  tasks.
- The experimental DPU frontend channel is not runnable for real Homer commands
  yet. It intentionally fails before SHM fallback when selected.
- Stage 4 kept DPU engine APIs independent of `HomerGrantVector` and
  `HomerProgressResult`. The current adapter lives inside
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  and maps engine facts into collector/action grants. Later real DMA actions
  can either stay behind this adapter or move stable scheduler budget/result
  types into a shared service header.
- The one-word publication offset is pinned at offset zero for both hot lines.
  This is a protocol choice for efficient polling and separate one-word DMA
  publication; if a future multi-word sealed snapshot is needed, it should be a
  separate ABI struct rather than mutating these hot lines.

## Stage 3: DPU DMA Engine Skeleton

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:30`
  defines the engine configuration, including per-class async-window placeholders
  and `enableDoca`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:40`
  and `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:54`
  define class facts and aggregate scheduler facts that ready-set builders can
  read without performing DOCA calls or DMA reads.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:57`
  initializes conservative stub defaults.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:78`
  creates the no-DOCA engine and rejects `enableDoca=true` explicitly.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:148`
  copies maintained zero-work facts.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_dma_smoke.c:33`
  validates create/facts/destroy and confirms accidental DOCA enablement fails
  with a not-implemented error.
- `/data/dbcomm/citus-dbcomm/Makefile:42` adds `service-dpu-dma-smoke`, and
  `/data/dbcomm/citus-dbcomm/Makefile:45` links `homer_service_dpu_dma.c` into
  the standalone service binary.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/Makefile:56` marks
  `homer_service_dpu_dma.o` as service-only so it is filtered out of `citus.so`.

Stage 3 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make service-dpu-dma-smoke
sudo -n -u dbcomm make -j8 service-bin client-bin
sudo -n -u dbcomm make -C src/backend/distributed all
sudo -n -u dbcomm make dpu-bridge-abi-check frontend-dma-smoke service-dpu-dma-smoke
git diff --check
```

Observed result:

- `homer_service_dpu_dma_smoke: ok`
- `homer_dpu_bridge_abi_check: ok`
- `homer_frontend_dma_smoke: ok`
- `service-bin` and `client-bin` remained buildable/up to date.
- The extension build completed with `homer_service_dpu_dma.o` excluded from
  `citus.so` by the service-object filter.

One validation command initially raced two parallel builds/executions of
`build/homer/homer_service_dpu_dma_smoke` and produced `Permission denied` while
the file was being rewritten. Rerunning the same checks sequentially passed; the
failure was a validation-command race, not a code issue.

## Stage 4: Current Scheduler Skeleton

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1986`
  adds the fixed `HOMER_PROGRESS_SOURCE_DPU_DMA` source.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2109`
  adds DPU collector identities for grouped-control reads, PE drains, command
  pulls, completion pushes, payload pulls, and consumed-head publication.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2145`
  adds matching scheduler action identities.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3134`
  adds a fixed DPU DMA source and feedback slot to `ProgressRegistry`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9300`
  maps every DPU collector to the fixed DPU DMA source.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9440`
  maps DPU collectors into bounded action grants using the current collector
  item-budget helper. `DPU_PE_DRAIN` uses the poll budget; the pull/push/publish
  actions use item budgets.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10283`
  admits DPU physical-progress collectors in the `machine-baseline` collector
  phase before semantic session/payload work.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34417`
  bridges Stage 3 engine facts into scheduler collector candidates. Ready-set
  building only reads maintained facts through `HomerDpuDmaGetSchedulerFacts()`;
  it does not submit DMA, poll host memory, or call `doca_pe_progress()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:35818`
  implements the Stage 4 no-op DPU action executor. It validates the DPU source
  owner, reports an empty bounded grant through `HomerServiceFinishProgressGrant()`,
  and explicitly documents that real PE draining must be nonblocking and must
  not spin when `doca_pe_progress()` returns no work.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36674`
  adds the opt-in service env var `HOMER_SERVICE_ENABLE_DPU_DMA`. When enabled,
  the service creates the no-DOCA Stage 3 engine. `HOMER_SERVICE_ENABLE_DOCA_DMA`
  is parsed separately and still fails through the Stage 3 not-implemented
  guard if set.

Stage 4 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make -j8 service-bin client-bin
sudo -n -u dbcomm make service-dpu-dma-smoke dpu-bridge-abi-check frontend-dma-smoke
git diff --check
```

Observed result:

- `citus_tuple_sink_service` rebuilt successfully with the DPU scheduler
  skeleton linked in.
- `client-bin` remained buildable/up to date.
- `homer_service_dpu_dma_smoke: ok`
- `homer_dpu_bridge_abi_check: ok`
- `homer_frontend_dma_smoke: ok`
- `git diff --check` reported no whitespace errors.

## Next Stage

Stage 5 should implement only the minimal DOCA/COMCH grouped-control lifecycle:
descriptor exchange/import, local grouped-control buffers, bounded task-slot
pool allocation, callback installation, and clean teardown behind
`HOMER_SERVICE_ENABLE_DOCA_DMA=1`. It should not submit real grouped-control DMA
tasks yet.

The important standalone synthetic host/DPU publisher validation gate moves to
Stage 6. That gate proves the DPU can submit grouped control-line DMA reads under
scheduler grants, retire the read completions only through bounded PE-drain
grants, validate publication epochs/generations/frontiers, and populate
DPU-local ready facts without reading one tail at a time.
