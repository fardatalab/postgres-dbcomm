# DPU DMA Backend Homer Service Implementation Checkpoint

## Current Status

This note tracks landed implementation stages for the DPU-pull Homer migration.
The target design remains
[`dpu_dma_backend_homer_service_current_scheduler_design.md`](../../../future-directions/citus/transport/dpu_dma_backend_homer_service_current_scheduler_design.md).

As of Stage 8B.3, the default frontend and service path is still the existing
host-process SHM/RDMA implementation. The DPU frontend path exists only behind
the explicit hidden GUC `citus.enable_experimental_homer_dpu_frontend`; when the
GUC is enabled, the default non-DOCA frontend build fails explicitly before any
SHM mapping. A DOCA-enabled frontend build exports a synthetic bridge as a DOCA
PCI mmap and now sends the setup bytes over a regular TCP setup socket, not
DOCA COMCH. The setup path has been refactored into a persistent opaque
frontend control channel, but real Homer API calls still fail with a deliberate
not-implemented error because the SQL frontend has not yet been promoted into a
runnable selected-DPU command path.

The service-side DPU DMA scheduler path is opt-in behind
`HOMER_SERVICE_ENABLE_DPU_DMA=1`; it reads maintained facts and wires bounded
DPU actions through `machine-baseline`. The engine now has service-side DOCA
lifecycle scaffolding behind `HOMER_DPU_DMA_WITH_DOCA`: task-slot/owner arrays,
local grouped-control staging buffers, one PE plus per-class DMA contexts, and a
bounded imported-host-mmap table keyed by bridge generation plus client instance
ID. The setup ABI still uses the existing `HomerDpuComch*` struct names, but the
active transport is TCP. `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_setup_tcp.c`
owns the nonblocking DPU listener and delivers received setup payloads to
`HomerDpuDmaImportHostMmapDescriptorForSetup()`. The production service creates
that listener when both `HOMER_SERVICE_ENABLE_DPU_DMA=1` and
`HOMER_SERVICE_ENABLE_DOCA_DMA=1`, progresses it through the bounded
`HOMER_PROGRESS_ACTION_DPU_PE_DRAIN` action, and destroys it before the DMA
engine.

Stages 6B and 7 remain the physical DPU-pull command path: grouped-control DMA
reads produce maintained ready-ring facts, command-slot pulls stage complete
`CitusRemoteExecControlSlot` records, and service-owned dispatch entries preserve
the DMA owner metadata needed for response publication. Stage 8A adds a bounded
pending-response queue and `HOMER_PROGRESS_ACTION_DPU_COMPLETION_PUSH`, which
submits same-context response-body DMA writes followed immediately by the
response-ready publication-word DMA write through
`HomerDpuDmaSubmitCommandResponsePublication()`. The standalone TCP transport
smoke validates the host-DPU composition end to end: real host mmap export over
TCP setup, DPU mmap import, grouped-control read, command pull, and DPU-written
response publication observed by host memory polling. Full selected-DPU SQL
frontend execution is still pending and must not be claimed until the frontend
guard is removed and the negative no-fallback completion gate is validated.
Stage 8B.3 adds the scheduler scaffold for the production backend-mailbox path:
`DPU_BACKEND_COMMAND_STAGE`, `DPU_BACKEND_COMMAND_PUBLISH`, and
`DPU_BACKEND_COMPLETION_PULL` now exist as distinct bounded DPU scheduler
actions with maintained zero-valued facts. They are intentionally inert until
the backend-mailbox DMA queues and submission APIs land.
Stage 8B.4 adds explicit bridge descriptor roles so setup/import can
distinguish frontend control slots, backend command mailboxes, backend
completion mailboxes, and payload byte rings without inferring role from only
workload class, direction, and geometry.

The next production selected-DPU command milestone must also add a host
lifecycle shim, not a host-service hot-path fallback. The DPU service cannot
directly create host `/dev/shm` mailbox objects or signal the host postmaster.
Those operations are currently performed by `TupleSinkServiceEnsureSessionMailboxes()`
and `TupleSinkServiceSubmitBackendSpawnRequest()` in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`,
with postmaster-side handling in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c`.
Selected-DPU mode should move that PostgreSQL-local lifecycle work into a small
host-side shim in or beside `homer_frontend_dma.c/.h`: create/register/export
mailboxes and bridge memory, complete TCP setup/import with the DPU, submit the
backend spawn request locally, and only then publish hot-path DMA frontiers.
This is a setup/lifecycle path, not a fallback that forwards every command to
the old host Homer service.

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
  yet. When selected, it intentionally fails before any SHM mapping rather than
  falling back to the old host-process path.
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

## Stage 5: DOCA Lifecycle And Descriptor Import Boundary

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:31`
  adds Stage 5 sizing fields for grouped-control staging buffers.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:67`
  exposes scheduler facts for task-slot count, free slot count, grouped-control
  buffer bytes, and whether a host mmap descriptor has been imported.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:80`
  declares `HomerDpuDmaImportHostMmapDescriptor()`, the scheduler-neutral entry
  point that future COMCH receive code should call after receiving host mmap
  export bytes.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:33`
  defines the bounded task-slot state, grouped-control owner metadata, and
  future task owner identity fields.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:166`
  sets default Stage 5 local-buffer geometry to 1024 cache-line buffers.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:28`
  defines `HOMER_DPU_DMA_DEFAULT_DOCA_DEVICE_PCI` as `0000:03:00.0`, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:169`
  installs that value in `HomerDpuDmaDefaultConfig()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:423`
  allocates the Stage 5 task-slot/owner pool and aligned local grouped-control
  buffers. It deliberately does not allocate `doca_dma_task_memcpy` handles yet:
  DOCA requires real source and destination `doca_buf` objects at
  `doca_dma_task_memcpy_alloc_init()` time, and the remote source buffers cannot
  exist before host mmap descriptor import.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:324`
  imports a host PCI mmap export descriptor into the DPU engine after the DOCA
  lifecycle is enabled.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:522`
  creates the opt-in DOCA lifecycle: configured local DMA-capable device, PE,
  buffer inventory, local mmap for grouped-control staging, and one DMA context
  per workload class.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:725`
  opens the exact configured local DOCA device with
  `doca_devinfo_is_equal_pci_addr()`, verifies DMA memcpy support, and fails
  explicitly if the configured PCI is absent, unsupported, or cannot be opened.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36681`
  reads `HOMER_SERVICE_DOCA_DEV_PCI` during service startup, after
  `HomerDpuDmaDefaultConfig()` has installed the DPU default.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_dma_smoke.c:30`
  keeps the default no-DOCA smoke deterministic and adds a
  `HOMER_DPU_DMA_WITH_DOCA` path that creates the lifecycle, exports a synthetic
  PCI mmap descriptor, imports it through the Stage 6A COMCH setup handler, and
  tears down cleanly.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_dma_smoke.c:343`
  selects a smoke-specific DOCA device. Host-side validation defaults to
  `0000:21:00.0`; DPU-side validation sets
  `HOMER_DPU_DMA_SMOKE_DOCA_DEV_PCI=0000:03:00.0`.
- `/data/dbcomm/citus-dbcomm/Makefile:20` adds `DOCA_CFLAGS`/`DOCA_LIBS`, and
  `/data/dbcomm/citus-dbcomm/Makefile:47` adds the opt-in
  `service-dpu-dma-doca-smoke` target.

Current limitation:

- The production COMCH peer that carries descriptor bytes from the host/frontend
  to the DPU service is not implemented in Stage 5. This is intentional after
  separating the DMA engine from control-plane message transport: the validated
  engine consumes descriptor bytes through `HomerDpuDmaImportHostMmapDescriptor()`,
  while the future COMCH endpoint should only deliver those bytes and connection
  lifecycle events. Stage 6A.1 implements that byte-level boundary and service
  setup handler, but not the actual DOCA COMCH client/server endpoint lifecycle.

## Validation

Stage 5 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make service-dpu-dma-smoke dpu-bridge-abi-check frontend-dma-smoke
sudo -n -u dbcomm make -j8 service-bin client-bin
sudo -n -u dbcomm make service-dpu-dma-doca-smoke
```

Observed result:

- `homer_service_dpu_dma_smoke: ok`
- `homer_dpu_bridge_abi_check: ok`
- `homer_frontend_dma_smoke: ok`
- `service-bin` and `client-bin` completed successfully.
- `service-dpu-dma-doca-smoke` created three DOCA DMA contexts, observed DOCA
  context state transitions `0 -> 2` and `2 -> 0`, exported a synthetic PCI mmap
  descriptor, imported it through the Stage 6A COMCH setup handler, and printed
  `homer_service_dpu_dma_smoke: ok`.
- The DOCA-enabled compile emitted warnings from the installed
  `/opt/mellanox/doca/include/doca_buf_inventory.h` inline helpers about
  deprecated experimental reuse APIs; the warnings come from the header include,
  not from Homer calling those helpers.

## Stage 6A.1: COMCH Setup ABI And Service Handoff

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_comch_abi.h`
  defines the fixed COMCH setup protocol, message kinds, setup status codes,
  one-cache-line setup ack, setup message sizing helper, setup-header builder,
  and receiver-side validation helper.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_comch_abi.h`
  validates that a setup payload contains one `HomerDpuBridgeControlBlockHeader`,
  a descriptor table of `HomerDpuBridgeRingDescriptor[]`, and a nonempty mmap
  export blob; it rejects malformed COMCH protocol version, message kind, total
  byte count, descriptor size, bridge generation, descriptor generation, ring
  index, workload class, direction, and record geometry.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c`
  adds `HomerFrontendDmaSetupPayloadBytes()` and
  `HomerFrontendDmaBuildSetupPayload()`. These are byte-buffer builders for the
  future host COMCH client. They do not register a DOCA mmap or send on COMCH
  yet.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c`
  adds `HomerServiceDpuComchHandleSetupPayload()`. It validates received setup
  bytes, fills a setup ack, reports explicit setup status, treats a missing DMA
  engine as an internal setup bug, and calls
  `HomerDpuDmaImportHostMmapDescriptor()` for the mmap export bytes.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_abi_check.c` adds a
  standalone host-only setup ABI checker covering accepted setup payloads,
  rejected short payloads, rejected protocol mismatch, rejected descriptor
  mismatch, rejected total-size mismatch, and ack initialization.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_dma_smoke.c` now routes
  the DOCA synthetic mmap export through `HomerServiceDpuComchHandleSetupPayload()`
  instead of importing it directly. The no-DOCA path also checks that a valid
  setup payload returns `HOMER_DPU_COMCH_SETUP_IMPORT_FAILED` rather than being
  silently accepted by an engine that cannot import host mmap descriptors.
- `/data/dbcomm/citus-dbcomm/Makefile` compiles `homer_service_dpu_comch.c`
  into `citus_tuple_sink_service`, adds `dpu-comch-abi-check`, and links the
  COMCH receiver module into the DPU DMA smoke binaries.

Scope boundary:

- This is **not** the full Stage 6A real COMCH transport. No code calls
  `doca_comch_server_create()`, `doca_comch_client_create()`, or a COMCH send
  task yet. The new code defines and validates the message bytes that the real
  COMCH callbacks should exchange.
- This stage deliberately leaves `tuple_sink_service_process.c` unchanged. The
  current scheduler still sees no new DPU DMA work, because grouped-control DMA
  submission starts in Stage 6B after real setup imports a host mmap descriptor.

## Validation

Stage 6A.1 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make dpu-bridge-abi-check dpu-comch-abi-check frontend-dma-smoke service-dpu-dma-smoke service-dpu-dma-doca-smoke service-bin client-bin
sudo -n -u dbcomm make -j8
```

Observed result:

- `homer_dpu_bridge_abi_check: ok`
- `homer_dpu_comch_abi_check: ok`
- `homer_frontend_dma_smoke: ok`
- `homer_service_dpu_dma_smoke: ok`
- `service-dpu-dma-doca-smoke` created three DOCA DMA contexts, exported a
  synthetic PCI mmap descriptor, imported it through
  `HomerServiceDpuComchHandleSetupPayload()`, and printed
  `homer_service_dpu_dma_smoke: ok`.
- `service-bin` and `client-bin` completed successfully.
- The full Citus build completed and compiled
  `utils/homer/homer_frontend_dma.o` and `utils/homer/homer_service_dpu_comch.o`
  into `citus.so`.
- The DOCA-enabled compile still emits warnings from the installed
  `/opt/mellanox/doca/include/doca_buf_inventory.h` inline helpers about
  deprecated experimental reuse APIs. These warnings remain from DOCA headers,
  not from Homer calling those helpers.

## Stage 6A.2: Standalone DOCA COMCH Transport Smoke

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c` adds a
  standalone server/client smoke for the real DOCA COMCH endpoint lifecycle. The
  DPU-side server opens a DOCA device and representor, creates a COMCH server,
  validates one received `HomerDpuComchSetupHeader` payload, and replies with a
  `HomerDpuComchSetupAck`. The host-side client creates a COMCH client, sends a
  synthetic setup payload using the shared ABI, waits for the ack, and validates
  the ack status.
- `/data/dbcomm/citus-dbcomm/Makefile` adds `DOCA_COMCH_CFLAGS`,
  `DOCA_COMCH_LIBS`, and `dpu-comch-transport-smoke-bin`.

Scope boundary:

- This stage proves the real DOCA COMCH control channel can carry the Homer
  setup bytes between host and DPU. It still uses a synthetic mmap export blob,
  so it does not replace the Stage 6A.1 DMA-engine import smoke.
- The new smoke does not link into `citus.so`, `homer_frontend_dma.c`, or the
  production `citus_tuple_sink_service` startup path yet. That integration is
  the next Stage 6A step.
- DPU source is not checked out under `/data/dbcomm/citus-dbcomm` on the DPU, so
  validation copied only the smoke source and ABI headers into
  `/tmp/homer_dpu_comch_smoke` and compiled there with the DPU aarch64 DOCA
  libraries.

Representor finding:

- The initial assumption that the DPU-side SF representor
  `0000:03:00.0` / `en3f0pf0sf0` should be used as the COMCH server representor
  did **not** work for the host-DPU setup channel. With DPU server
  `--dev-pci 0000:03:00.0 --rep-pci 0000:03:00.0`, the server started and
  timed out after 15 seconds, while the host client using `--dev-pci
  0000:21:00.0` failed with `Connection aborted`.
- The working DPU-side representor for the farnet1 host-to-DPU COMCH smoke is
  the host PF representor `0000:21:00.0` under DPU local device
  `0000:03:00.0`. With DPU server `--dev-pci 0000:03:00.0 --rep-pci
  0000:21:00.0` and host client `--dev-pci 0000:21:00.0`, the host received a
  valid setup ack and both processes exited successfully.

## Validation

Stage 6A.2 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make dpu-comch-transport-smoke-bin dpu-comch-abi-check dpu-bridge-abi-check

ssh dpu "rm -rf /tmp/homer_dpu_comch_smoke && mkdir -p /tmp/homer_dpu_comch_smoke/src/bin /tmp/homer_dpu_comch_smoke/src/include/distributed/homer"
rsync -az src/bin/homer_dpu_comch_transport_smoke.c dpu:/tmp/homer_dpu_comch_smoke/src/bin/
rsync -az src/include/distributed/homer/homer_abi_version.h src/include/distributed/homer/homer_dpu_bridge_abi.h src/include/distributed/homer/homer_dpu_comch_abi.h dpu:/tmp/homer_dpu_comch_smoke/src/include/distributed/homer/
ssh dpu 'cd /tmp/homer_dpu_comch_smoke && gcc -std=gnu99 -Wall -Wextra -Werror=vla -I src/include $(pkg-config --cflags doca-comch doca-common) -o homer_dpu_comch_transport_smoke src/bin/homer_dpu_comch_transport_smoke.c $(pkg-config --libs doca-comch doca-common)'

ssh dpu 'cd /tmp/homer_dpu_comch_smoke && ./homer_dpu_comch_transport_smoke --server --name homer-dpu-comch-default-test --timeout-ms 15000'
./build/homer/homer_dpu_comch_transport_smoke --client --name homer-dpu-comch-default-test --timeout-ms 15000
```

Observed result:

- Host build of `homer_dpu_comch_transport_smoke` completed successfully.
- DPU aarch64 build under `/tmp/homer_dpu_comch_smoke` completed successfully.
- Failed representor attempt:
  - Host client: `homer_dpu_comch_transport_smoke: failed: Connection aborted`
  - DPU server with `--rep-pci 0000:03:00.0`: `COMCH smoke timed out after
    15000 ms`
- Working default attempt:
  - Host client printed `client received setup ack generation=1 rings=1
    imported_bytes=16` and `homer_dpu_comch_transport_smoke: ok`.
  - DPU server printed `server ready name=homer-dpu-comch-default-test
    dev=0000:03:00.0 rep=0000:21:00.0 timeout_ms=15000` and
    `homer_dpu_comch_transport_smoke: ok`.
- Existing source validation still passed:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make dpu-comch-transport-smoke-bin dpu-comch-abi-check dpu-bridge-abi-check frontend-dma-smoke service-dpu-dma-smoke service-dpu-dma-doca-smoke service-bin client-bin
sudo -n -u dbcomm make -j8
```

## Stage 6A.3: Explicit DOCA DMA Device Selection

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:28`
  adds `HOMER_DPU_DMA_DEVICE_PCI_BYTES` and
  `HOMER_DPU_DMA_DEFAULT_DOCA_DEVICE_PCI`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:43`
  adds `HomerDpuDmaEngineConfig.docaDevicePci`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:169`
  defaults that field to the farnet1 DPU-local DOCA device `0000:03:00.0`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:725`
  replaces the previous first-capable-device scan with
  `HomerDpuDmaOpenConfiguredDevice()`. The function uses
  `doca_devinfo_is_equal_pci_addr()` to match the configured PCI, requires
  `doca_dma_cap_task_memcpy_is_supported()`, and reports separate errors for
  missing, unsupported, or unopenable configured devices.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36681`
  adds `HOMER_SERVICE_DOCA_DEV_PCI` as the service startup override. The startup
  log now prints the selected `doca_dev_pci` so a failed DPU deployment does not
  hide which device was attempted.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_dma_smoke.c:343`
  adds `HOMER_DPU_DMA_SMOKE_DOCA_DEV_PCI`, defaulting the host smoke to
  `0000:21:00.0` while allowing the same smoke source to validate the DPU-local
  `0000:03:00.0`.

Design note:

- DMA still uses only a local `doca_dev`; there is no representor in the DMA
  engine. The representor remains COMCH-specific, where the working farnet1
  setup channel uses DPU server `dev=0000:03:00.0` and `rep=0000:21:00.0`.

Stage 6A.3 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make service-dpu-dma-smoke service-dpu-dma-doca-smoke dpu-comch-transport-smoke-bin dpu-comch-abi-check dpu-bridge-abi-check service-bin client-bin
git diff --check --cached

ssh dpu 'rm -rf /tmp/homer_dpu_dma_smoke && mkdir -p /tmp/homer_dpu_dma_smoke'
rsync -azR src/bin/homer_service_dpu_dma_smoke.c src/backend/distributed/utils/homer/homer_service_dpu_dma.c src/backend/distributed/utils/homer/homer_service_dpu_dma.h src/backend/distributed/utils/homer/homer_service_dpu_comch.c src/backend/distributed/utils/homer/homer_service_dpu_comch.h src/include/distributed/homer/homer_abi_version.h src/include/distributed/homer/homer_dpu_bridge_abi.h src/include/distributed/homer/homer_dpu_comch_abi.h dpu:/tmp/homer_dpu_dma_smoke/
ssh dpu 'cd /tmp/homer_dpu_dma_smoke && gcc -std=gnu99 -Wall -Wextra -Wno-unused-parameter -Wno-sign-compare -Wno-missing-field-initializers -Werror=vla -Werror=implicit-int -Werror=implicit-function-declaration -Werror=return-type -DHOMER_DPU_DMA_WITH_DOCA -I src/include -I src/backend/distributed/utils/homer -I/opt/mellanox/doca/include -I/usr/include/libnl3 -DALLOW_EXPERIMENTAL_API -o homer_service_dpu_dma_smoke src/bin/homer_service_dpu_dma_smoke.c src/backend/distributed/utils/homer/homer_service_dpu_dma.c src/backend/distributed/utils/homer/homer_service_dpu_comch.c -L/opt/mellanox/doca/lib/aarch64-linux-gnu -ldoca_dma -ldoca_common && LD_LIBRARY_PATH=/opt/mellanox/doca/lib/aarch64-linux-gnu HOMER_DPU_DMA_SMOKE_DOCA_DEV_PCI=0000:03:00.0 ./homer_service_dpu_dma_smoke'
```

Observed result:

- Host no-DOCA smoke printed `homer_service_dpu_dma_smoke: ok`.
- Host DOCA smoke printed `using DOCA DMA device PCI 0000:21:00.0`, observed
  three DMA context start/stop transitions, and printed
  `homer_service_dpu_dma_smoke: ok`.
- DPU aarch64 smoke printed `using DOCA DMA device PCI 0000:03:00.0`, observed
  three DMA context start/stop transitions, and printed
  `homer_service_dpu_dma_smoke: ok`.
- `dpu-comch-abi-check`, `dpu-bridge-abi-check`, `service-bin`, and `client-bin`
  completed successfully.
- The DOCA-enabled host and DPU compiles still emit warnings from installed
  `doca_buf_inventory.h` inline helpers about deprecated experimental reuse APIs.
  Homer does not call those helpers directly.

## Stage 6A.4: Real Mmap COMCH Transport Into DMA Import

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:55`
  extends the standalone COMCH smoke options with `--import-dma` for the
  DPU-side server and `--real-mmap` for the host-side client. The flags are
  deliberately role-specific so the original transport-only smoke remains
  available.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:551`
  creates a real service-side `HomerDpuDmaEngine` for the server import smoke,
  using the same `HOMER_SERVICE_DOCA_DEV_PCI` override as production service
  startup.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:578`
  allocates a cache-line-aligned host buffer, registers it with `doca_mmap`,
  grants `DOCA_ACCESS_FLAG_PCI_READ_WRITE`, starts the mmap, and exports a PCI
  descriptor with `doca_mmap_export_pci()`.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:905`
  routes a received setup payload through
  `HomerServiceDpuComchHandleSetupPayload()` when `--import-dma` is set. That
  means the DPU server validates setup bytes, imports the host mmap descriptor
  through `HomerDpuDmaImportHostMmapDescriptor()`, and replies with the normal
  `HomerDpuComchSetupAck`.
- `/data/dbcomm/citus-dbcomm/Makefile:120` links the COMCH transport smoke with
  `homer_service_dpu_dma.c`, `homer_service_dpu_comch.c`, `doca-comch`, and
  `doca-dma` so the standalone binary exercises the same service import helper
  as the eventual production setup path.

Scope boundary:

- Stage 6A.4 is still a standalone validation binary, not production startup
  integration. It proves that the cold-path pieces compose correctly across the
  actual host-DPU boundary: host mmap export, COMCH setup send, DPU COMCH
  receive, DPU DMA engine init, and DPU mmap import.
- The next production integration step still needs to decide the exact lifetime
  of the server/client COMCH objects inside `citus_tuple_sink_service` and
  `homer_frontend_dma.c`, including whether the cold setup blocks before normal
  service pumping or is exposed as an explicit experimental setup operation.

Stage 6A.4 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make -B dpu-comch-transport-smoke-bin

ssh dpu 'rm -rf /tmp/homer_dpu_comch_smoke && mkdir -p /tmp/homer_dpu_comch_smoke'
rsync -azR src/bin/homer_dpu_comch_transport_smoke.c src/backend/distributed/utils/homer/homer_service_dpu_dma.c src/backend/distributed/utils/homer/homer_service_dpu_dma.h src/backend/distributed/utils/homer/homer_service_dpu_comch.c src/backend/distributed/utils/homer/homer_service_dpu_comch.h src/include/distributed/homer/homer_abi_version.h src/include/distributed/homer/homer_dpu_bridge_abi.h src/include/distributed/homer/homer_dpu_comch_abi.h dpu:/tmp/homer_dpu_comch_smoke/
ssh dpu 'cd /tmp/homer_dpu_comch_smoke && gcc -std=gnu99 -Wall -Wextra -Wno-unused-parameter -Wno-sign-compare -Wno-missing-field-initializers -Werror=vla -Werror=implicit-int -Werror=implicit-function-declaration -Werror=return-type -DHOMER_DPU_DMA_WITH_DOCA -I src/include -I src/backend/distributed/utils/homer -I/opt/mellanox/doca/include -I/usr/include/libnl3 -DALLOW_EXPERIMENTAL_API -o homer_dpu_comch_transport_smoke src/bin/homer_dpu_comch_transport_smoke.c src/backend/distributed/utils/homer/homer_service_dpu_dma.c src/backend/distributed/utils/homer/homer_service_dpu_comch.c -L/opt/mellanox/doca/lib/aarch64-linux-gnu -ldoca_comch -ldoca_dma -ldoca_common'

# Regression: transport-only synthetic setup still passes.
ssh dpu 'cd /tmp/homer_dpu_comch_smoke && LD_LIBRARY_PATH=/opt/mellanox/doca/lib/aarch64-linux-gnu ./homer_dpu_comch_transport_smoke --server --name homer-dpu-comch-regression --dev-pci 0000:03:00.0 --rep-pci 0000:21:00.0 --timeout-ms 15000'
./build/homer/homer_dpu_comch_transport_smoke --client --name homer-dpu-comch-regression --dev-pci 0000:21:00.0 --timeout-ms 15000

# Real setup: host exports a real mmap descriptor; DPU imports it into DMA.
ssh dpu 'cd /tmp/homer_dpu_comch_smoke && LD_LIBRARY_PATH=/opt/mellanox/doca/lib/aarch64-linux-gnu ./homer_dpu_comch_transport_smoke --server --import-dma --name homer-dpu-comch-dma-import --dev-pci 0000:03:00.0 --rep-pci 0000:21:00.0 --timeout-ms 15000'
./build/homer/homer_dpu_comch_transport_smoke --client --real-mmap --name homer-dpu-comch-dma-import --dev-pci 0000:21:00.0 --timeout-ms 15000
```

Observed result:

- The regression transport-only path still printed `client received setup ack
  generation=1 rings=1 imported_bytes=16` and both host and DPU processes exited
  with `homer_dpu_comch_transport_smoke: ok`.
- The real mmap path printed `client exported real PCI mmap bytes=277
  buffer_bytes=192` on the host, and the host ack reported `imported_bytes=277`.
- The DPU server printed `server DMA import enabled dma_dev=0000:03:00.0`,
  observed three DOCA DMA context start/stop transitions, and exited with
  `homer_dpu_comch_transport_smoke: ok`.
- The follow-up affected-target build passed:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make dpu-comch-transport-smoke-bin dpu-comch-abi-check dpu-bridge-abi-check frontend-dma-smoke service-dpu-dma-smoke service-dpu-dma-doca-smoke service-bin client-bin
```

## Stage 6A.5: Reusable Service-Side COMCH Server Lifecycle

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.h:27`
  defines the validated farnet1 defaults for the service-side COMCH listener:
  server name `homer-dpu-comch`, DPU-local DOCA device `0000:03:00.0`, and host
  PF representor `0000:21:00.0`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.h:34`
  adds `HomerServiceDpuComchServerConfig`, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.h:44`
  adds `HomerServiceDpuComchServerFacts`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.h:56`
  declares the reusable server lifecycle API:
  `HomerServiceDpuComchDefaultServerConfig()`,
  `HomerServiceDpuComchServerCreate()`,
  `HomerServiceDpuComchServerProgress()`,
  `HomerServiceDpuComchServerGetFacts()`, and
  `HomerServiceDpuComchServerDestroy()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c:117`
  implements `HomerServiceDpuComchServerCreate()`. It starts the DPU-side COMCH
  listener and returns without waiting for a host backend to connect. In a build
  without `HOMER_DPU_DMA_WITH_DOCA`, it fails explicitly instead of creating a
  fake server.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c:175`
  implements `HomerServiceDpuComchServerProgress()`. It runs at most the caller
  supplied number of `doca_pe_progress()` polls and stops on the first
  zero-progress poll, so the future scheduler adapter can keep COMCH progress
  bounded.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c:237`
  implements bounded cleanup-time stop progress in
  `HomerServiceDpuComchServerDestroy()`. This is teardown cleanup, not a
  service-private hot-path progress loop.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c:282`
  now checks the setup-ack output pointer before initializing it. This fixes the
  pre-existing NULL-ack bug in the setup handler.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c:548`
  allocates one `HomerDpuComchSetupAck` copy per async COMCH send and stores it
  in DOCA task user data; `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c:570`
  and `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c:583`
  free that copy in the send completion/error callbacks. This avoids the
  tempting but unsafe shared-ack-buffer pattern.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c:667`
  receives setup messages in the COMCH callback, routes them through
  `HomerServiceDpuComchHandleSetupPayload()`, counts setup errors, and sends the
  ack. The callback does not submit DMA work and does not call
  `doca_pe_progress()`.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_comch_smoke.c:39`
  adds a DPU-side lifecycle smoke that creates a real DMA engine, creates the
  COMCH listener, performs bounded progress polls without any host peer, checks
  maintained facts, and destroys the listener.
- `/data/dbcomm/citus-dbcomm/Makefile:25` adds
  `homer_service_dpu_comch_smoke`, and
  `/data/dbcomm/citus-dbcomm/Makefile:180` links it with `doca-comch`,
  `doca-dma`, `homer_service_dpu_comch.c`, and `homer_service_dpu_dma.c`.

Scope boundary:

- Stage 6A.5 is the service-side lifecycle boundary, not full production setup.
  `tuple_sink_service_process.c` still does not create a
  `HomerServiceDpuComchServer` during service startup, and
  `homer_frontend_dma.c` still does not implement the host COMCH client.
- The server API is deliberately nonblocking for normal operation. Startup
  creates the listener, scheduler-controlled progress later drains the COMCH PE,
  and callbacks only perform setup-message validation/import plus ack send.

Stage 6A.5 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make service-dpu-dma-smoke dpu-comch-abi-check service-bin client-bin service-dpu-comch-smoke-bin dpu-comch-transport-smoke-bin

rsync -azR src/bin/homer_service_dpu_comch_smoke.c src/bin/homer_dpu_comch_transport_smoke.c src/backend/distributed/utils/homer/homer_service_dpu_comch.c src/backend/distributed/utils/homer/homer_service_dpu_comch.h src/backend/distributed/utils/homer/homer_service_dpu_dma.c src/backend/distributed/utils/homer/homer_service_dpu_dma.h src/include/distributed/homer/homer_dpu_comch_abi.h src/include/distributed/homer/homer_dpu_bridge_abi.h src/include/distributed/homer/homer_abi_version.h dpu:/tmp/homer_dpu_comch_stage6a5_final/
ssh dpu 'cd /tmp/homer_dpu_comch_stage6a5_final && gcc -std=gnu99 -Wall -Wextra -Wno-unused-parameter -Wno-sign-compare -Wno-missing-field-initializers -Werror=vla -Werror=implicit-int -Werror=implicit-function-declaration -Werror=return-type -DHOMER_DPU_DMA_WITH_DOCA -I src/include -I src/backend/distributed/utils/homer -I/opt/mellanox/doca/include -I/usr/include/libnl3 -DALLOW_EXPERIMENTAL_API -o homer_service_dpu_comch_smoke src/bin/homer_service_dpu_comch_smoke.c src/backend/distributed/utils/homer/homer_service_dpu_comch.c src/backend/distributed/utils/homer/homer_service_dpu_dma.c -L/opt/mellanox/doca/lib/aarch64-linux-gnu -ldoca_comch -ldoca_dma -ldoca_common && gcc -std=gnu99 -Wall -Wextra -Wno-unused-parameter -Wno-sign-compare -Wno-missing-field-initializers -Werror=vla -Werror=implicit-int -Werror=implicit-function-declaration -Werror=return-type -DHOMER_DPU_DMA_WITH_DOCA -I src/include -I src/backend/distributed/utils/homer -I/opt/mellanox/doca/include -I/usr/include/libnl3 -DALLOW_EXPERIMENTAL_API -o homer_dpu_comch_transport_smoke src/bin/homer_dpu_comch_transport_smoke.c src/backend/distributed/utils/homer/homer_service_dpu_comch.c src/backend/distributed/utils/homer/homer_service_dpu_dma.c -L/opt/mellanox/doca/lib/aarch64-linux-gnu -ldoca_comch -ldoca_dma -ldoca_common && LD_LIBRARY_PATH=/opt/mellanox/doca/lib/aarch64-linux-gnu ./homer_service_dpu_comch_smoke --dev-pci 0000:03:00.0 --rep-pci 0000:21:00.0 --name homer-dpu-comch-stage6a5-final --progress-iters 16'

# Regression: real mmap setup still works through the existing standalone transport smoke.
ssh dpu 'cd /tmp/homer_dpu_comch_stage6a5_final && LD_LIBRARY_PATH=/opt/mellanox/doca/lib/aarch64-linux-gnu ./homer_dpu_comch_transport_smoke --server --import-dma --name homer-dpu-comch-stage6a5-final-real --dev-pci 0000:03:00.0 --rep-pci 0000:21:00.0 --timeout-ms 10000'
LD_LIBRARY_PATH=/opt/mellanox/doca/lib/x86_64-linux-gnu build/homer/homer_dpu_comch_transport_smoke --client --real-mmap --name homer-dpu-comch-stage6a5-final-real --dev-pci 0000:21:00.0 --timeout-ms 10000
```

Observed result:

- The DPU lifecycle smoke printed
  `homer_service_dpu_comch_smoke: server started name=homer-dpu-comch-stage6a5-final dev=0000:03:00.0 rep=0000:21:00.0 running=1`
  followed by `homer_service_dpu_comch_smoke: ok`.
- The real mmap transport regression printed `client received setup ack
  generation=1 rings=1 imported_bytes=267` and both host and DPU processes
  exited with `homer_dpu_comch_transport_smoke: ok`.
- The only compile warnings were the known DOCA header deprecation warnings for
  `doca_buf_inventory_buf_reuse_by_args`; no project warning remained.

## Stage 6A.6: Production Service COMCH Lifecycle Wiring

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:59`
  includes the service-side COMCH lifecycle header.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:505`
  extends `HomerServiceDpuDmaSchedulerState` with a
  `HomerServiceDpuComchServer *comchServer` owned by the service lifecycle.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34425`
  now reads `HomerServiceDpuComchServerFacts` while constructing DPU collector
  candidates. This remains ready-set-safe: it reads maintained service-local
  facts and does not call DOCA.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34463`
  makes `HOMER_PROGRESS_COLLECTOR_DPU_PE_DRAIN` ready when either DMA has
  in-flight PE work or the COMCH listener needs service. COMCH startup progress
  is known work while the context is not yet running; an already-running
  listener is blind cold-path polling and remains subject to scheduler
  feedback/backoff.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:35870`
  executes `HOMER_PROGRESS_ACTION_DPU_PE_DRAIN` by calling
  `HomerServiceDpuComchServerProgress()` with the granted poll budget. The
  action stops when the server reports no progress and reports either
  `cqesDrained` or `emptyPolls` through the existing scheduler result path.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36730`
  creates the DPU DMA engine when `HOMER_SERVICE_ENABLE_DPU_DMA=1` as before.
  If `HOMER_SERVICE_ENABLE_DOCA_DMA=1`, it now also creates the COMCH listener
  using `HOMER_SERVICE_DPU_COMCH_NAME`, `HOMER_SERVICE_COMCH_DEV_PCI`, and
  `HOMER_SERVICE_COMCH_REP_PCI` overrides, defaulting to the validated DPU
  `0000:03:00.0` local device and `0000:21:00.0` host PF representor.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36853`
  destroys the COMCH server before destroying the DMA engine.
- `/data/dbcomm/citus-dbcomm/Makefile:81` links the production service binary
  with `DOCA_COMCH_CFLAGS` and `DOCA_COMCH_LIBS`, so a service build with
  `CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA'` resolves the COMCH
  lifecycle calls.

Scope boundary:

- Stage 6A.6 wires production service ownership and bounded scheduler progress
  for the DPU COMCH listener. It does not yet implement the host/frontend COMCH
  client, and no production Homer command can complete DPU setup yet.
- The full production service was compile/link validated with and without
  `HOMER_DPU_DMA_WITH_DOCA`. Runtime validation of the exact production
  `citus_tuple_sink_service` binary on the DPU is still pending; the runtime
  evidence for the COMCH lifecycle remains the DPU-side Stage 6A.5 lifecycle
  smoke and real mmap transport smoke.

Stage 6A.6 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make service-bin client-bin service-dpu-comch-smoke-bin
sudo -n -u dbcomm make -B service-bin CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA'
sudo -n -u dbcomm make -B service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'
```

Observed result:

- The default service/client build passed with the new COMCH object linked into
  the service binary but with real DOCA code still behind
  `HOMER_DPU_DMA_WITH_DOCA`.
- The forced DOCA-enabled service build linked successfully against
  `doca-comch`, `doca-dma`, and `doca-common`.
- The normal no-extra-macro service/client binaries were rebuilt afterward so
  the local build tree was left in the usual state.
- The only compile warnings in the forced DOCA build were the known NVIDIA DOCA
  header deprecation warnings for `doca_buf_inventory_buf_reuse_by_args`.

## Stage 6A.7: Bounded Host Mmap Import Table

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:42`
  adds `hostMmapImportCapacity` to `HomerDpuDmaEngineConfig`; the default is
  `1024` imports in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:185`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:72`
  exposes `hostMmapImportCapacity`, `hostMmapImportCount`, and
  `importedRingCount` through scheduler facts so ready-set construction can see
  setup state without calling DOCA.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:97`
  defines `HomerDpuDmaHostMmapImport`, the per-import metadata table entry. It
  records active state, ring count, descriptor size, bridge generation, client
  instance ID, export-descriptor byte count, and the imported `doca_mmap *` in
  DOCA builds.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:367`
  implements `HomerDpuDmaImportHostMmapDescriptorForSetup()`. It validates setup
  identity, rejects duplicate `(bridgeGeneration, clientInstanceId)` imports,
  imports the descriptor with `doca_mmap_create_from_export()`, stores metadata
  in a free table slot, and updates import/ring counters.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:355`
  keeps the older `HomerDpuDmaImportHostMmapDescriptor()` entry point as a
  compatibility wrapper for tests that do not yet carry full setup identity.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c:315`
  routes COMCH setup imports through the setup-aware import API, passing the
  bridge generation, client instance ID, ring count, and descriptor size from the
  setup header.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:778`
  destroys every active imported mmap during DOCA lifecycle teardown before DMA
  contexts and the device are destroyed.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_dma_smoke.c:162`
  now exports two synthetic host mmaps, imports both through the COMCH setup
  handler, verifies import/ring counts reach two, and verifies re-sending the
  first setup fails with `HOMER_DPU_COMCH_SETUP_IMPORT_FAILED`.
- `/data/dbcomm/citus-dbcomm/Makefile:170` links the DOCA DMA smoke with
  `DOCA_COMCH_CFLAGS` and `DOCA_COMCH_LIBS`, because the smoke now compiles the
  COMCH setup handler in DOCA mode.

Scope boundary:

- Stage 6A.7 removes a setup-time singleton bottleneck before the production
  host/frontend COMCH client exists. It does not yet add host-side COMCH client
  code, per-session ring descriptors beyond the current setup payload, imported
  mmap reclaim by close message, or grouped-control DMA submission.
- The table key is deliberately setup identity, not ring identity. Future
  grouped-control and payload task owners still need ring/session identifiers
  when they resolve source buffers inside an imported mmap.
- Duplicate setup identity is treated as an explicit setup error. Later teardown
  work can make a new generation legal after close/reclaim; this stage only
  supports monotonically distinct setup imports during one engine lifecycle.

Stage 6A.7 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make service-dpu-dma-smoke dpu-comch-abi-check service-bin client-bin service-dpu-comch-smoke-bin dpu-comch-transport-smoke-bin
sudo -n -u dbcomm make service-dpu-dma-doca-smoke

# DPU-side DOCA DMA smoke, cross-compiled on the DPU from a copied source subset.
LD_LIBRARY_PATH=/opt/mellanox/doca/lib/aarch64-linux-gnu \
HOMER_DPU_DMA_SMOKE_DOCA_DEV_PCI=0000:03:00.0 \
./homer_service_dpu_dma_smoke

# Real host-to-DPU COMCH regression after the import-table change.
LD_LIBRARY_PATH=/opt/mellanox/doca/lib/x86_64-linux-gnu \
build/homer/homer_dpu_comch_transport_smoke \
  --client --real-mmap \
  --name homer-dpu-comch-multi-import-regression-1782625310 \
  --dev-pci 0000:21:00.0 \
  --timeout-ms 20000
```

Observed result:

- The default host build/smoke passed, including `service-bin`, `client-bin`, the
  COMCH ABI check, the service-side COMCH smoke binary, and the transport smoke
  binary.
- `service-dpu-dma-doca-smoke` passed on the host and printed
  `homer_service_dpu_dma_smoke: ok`.
- The DPU-side DOCA DMA smoke passed on `ssh dpu` with
  `HOMER_DPU_DMA_SMOKE_DOCA_DEV_PCI=0000:03:00.0` and printed
  `homer_service_dpu_dma_smoke: ok`.
- The real host-to-DPU COMCH regression passed with both processes returning
  zero. The host log printed `client received setup ack generation=1 rings=1
  imported_bytes=284`; the DPU server log printed
  `homer_dpu_comch_transport_smoke: ok`.
- The only compile warnings were known DOCA header deprecation warnings for
  `doca_buf_inventory_buf_reuse_by_args`.

## Stage 6A.8: Production Frontend COMCH Setup Client

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.h:45`
  defines `HomerFrontendDmaSetupSmokeResult`, the temporary result struct for
  the Stage 6A setup-smoke path.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:432`
  implements `HomerFrontendDmaRunDpuSetupSmoke()`. In normal builds it fails
  with an explicit `FEATURE_NOT_SUPPORTED` error before SHM fallback is possible.
  In DOCA-enabled builds it loads cold-path env config and attempts the real
  COMCH/mmap setup handshake.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:615`
  implements `HomerFrontendDmaRunDocaSetup()`: allocate a one-ring frontend
  bridge, build a synthetic ring descriptor, open the configured host DOCA
  device, export the bridge mmap, build and locally validate the COMCH setup
  payload, start the COMCH client, wait for ack, copy ack facts into the result,
  and clean up.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:822`
  registers the bridge memory with DOCA mmap permissions
  `DOCA_ACCESS_FLAG_PCI_READ_WRITE` and exports the PCI mmap descriptor with
  `doca_mmap_export_pci()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:871`
  creates the DOCA COMCH client, attaches it to a PE, installs state/send/receive
  callbacks, configures message/queue sizing, and starts the ctx.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:970`
  implements the cold-path blocking wait. It progresses only the setup COMCH PE,
  sleeps briefly on zero progress, and enforces
  `HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:1087`
  validates the setup ack received from the DPU service and treats malformed or
  negative acks as DPU setup failures.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:641`
  now calls `HomerFrontendDmaRunDpuSetupSmoke()` from
  `RemoteExecutionRejectDpuFrontendChannelIfSelected()` after the host-only
  bridge-memory smoke and before the Stage 7 `not implemented` error.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/Makefile:9` adds opt-in
  DOCA DMA/COMCH `pkg-config` flags, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/Makefile:96` links those
  flags into `citus.so` only when `CPPFLAGS` contains
  `HOMER_DPU_DMA_WITH_DOCA`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/Makefile:60` marks
  `homer_service_dpu_comch.o` as service-only so the DOCA-enabled frontend
  extension link keeps service-side COMCH code out of `citus.so`.

Stage 6A.8 design notes:

- The host-side COMCH client lives in `homer_frontend_dma.c` because it is part
  of the frontend DMA channel setup boundary. This matches the earlier decision
  to keep semantic request construction in `homer_frontend_control.c` and channel
  mechanics in the private frontend channel file.
- COMCH setup remains a cold-path blocking operation and uses a finite timeout.
  This does not change the hot-path scheduler rule: no scheduler action or
  callback spins waiting for DOCA progress.
- The bridge mmap is destroyed immediately after setup ack in this sub-stage.
  That is valid only because Stage 7 command DMA reads are not submitted yet.
  The first Stage 7 functional path must retain the frontend bridge allocation
  and DOCA mmap for the session lifetime.
- This sub-stage now validates both compile/link integration and the exact
  PostgreSQL backend/frontend call path that invokes the setup helper. The DPU
  peer used for this validation is the standalone import-enabled COMCH smoke
  server; a full DPU-resident `citus_tuple_sink_service` runtime remains a later
  deployment gate.

Stage 6A.8 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make frontend-dma-smoke dpu-comch-abi-check service-bin client-bin
sudo -n -u dbcomm make -C src/backend/distributed utils/homer/homer_frontend_dma.o
sudo -n -u dbcomm make -B -C src/backend/distributed \
  utils/homer/homer_frontend_dma.o \
  CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA'
sudo -n -u dbcomm make -B -C src/backend/distributed citus.so \
  CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA' \
  > /tmp/homer_dpu_frontend_doca_extension_build.log 2>&1
sudo -n -u dbcomm make -B -C src/backend/distributed \
  utils/homer/homer_frontend_dma.o \
  CPPFLAGS='-D_GNU_SOURCE'
git diff --cached --check

# Runtime validation of the exact PostgreSQL backend/frontend path. This
# temporarily installed a DOCA-enabled citus.so, restarted PostgreSQL, and then
# restored the default non-DOCA install afterward.
sudo -n -u dbcomm make -B -C src/backend/distributed install \
  CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA'
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_ctl \
  -D /data/dbcomm/pg-citus/data \
  -l /data/dbcomm/pg-citus/data/postgres.log \
  restart -w
ssh dpu "cd /tmp/homer_dpu_comch_stage6a5_final && \
  LD_LIBRARY_PATH=/opt/mellanox/doca/lib/aarch64-linux-gnu \
  ./homer_dpu_comch_transport_smoke \
    --server --import-dma \
    --name homer-dpu-comch \
    --dev-pci 0000:03:00.0 \
    --rep-pci 0000:21:00.0 \
    --timeout-ms 20000 \
    > /tmp/homer_backend_setup_server.log 2>&1" &
DPU_SERVER_PID=$!
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql \
  -h /tmp -p 5432 -U dbcomm -X postgres \
  -v ON_ERROR_STOP=0 \
  -c "SET citus.enable_experimental_homer_dpu_frontend = on;
      SELECT pg_catalog.citus_remote_exec_pgbench_transaction(1, 1, 1, 1, 1);"
wait "$DPU_SERVER_PID"
ssh dpu "cat /tmp/homer_backend_setup_server.log"
sudo -n -u dbcomm make -B -C src/backend/distributed install \
  CPPFLAGS='-D_GNU_SOURCE'
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_ctl \
  -D /data/dbcomm/pg-citus/data \
  -l /data/dbcomm/pg-citus/data/postgres.log \
  restart -w
```

Observed result:

- The default host build/smoke passed, including `homer_frontend_dma_smoke: ok`,
  `homer_dpu_comch_abi_check: ok`, `service-bin`, `client-bin`, and a default
  non-DOCA compile of `homer_frontend_dma.o`.
- The DOCA-enabled compile of `homer_frontend_dma.o` passed with the DOCA headers
  available under `/opt/mellanox/doca`.
- The DOCA-enabled `citus.so` link passed. The final link line included
  `utils/homer/homer_frontend_dma.o`, `-ldoca_comch`, and `-ldoca_dma`; it did
  not include `utils/homer/homer_service_dpu_comch.o`.
- The only warnings in the forced extension build were existing unrelated
  warnings from nested `PG_TRY` macro shadowing in `commands/multi_copy.c` and
  the existing missing prototype for
  `CitusInstallRemoteExecutionBackendHooks()` in
  `remote_execution_backend_bridge.c`.
- After the DOCA-enabled compile/link check, `homer_frontend_dma.o` was rebuilt
  again with default `CPPFLAGS='-D_GNU_SOURCE'` so local build artifacts were not
  left in a DOCA-forced shape.
- The runtime validation installed a DOCA-enabled `citus.so`; `ldd` showed
  `libdoca_comch.so.2` and `libdoca_common.so.2` while the validation build was
  installed, proving PostgreSQL would load the frontend COMCH client code.
- The SQL call through
  `citus_remote_exec_pgbench_transaction(1, 1, 1, 1, 1)` with
  `citus.enable_experimental_homer_dpu_frontend=on` returned the expected Stage 7
  error:
  `experimental Homer DPU frontend channel is not implemented yet`,
  `operation=OpenRemoteExecutionCommandSession`.
  It did not fail with `Homer DPU COMCH setup failed`, which means the frontend
  setup helper completed before the command-pull guard fired.
- The DPU peer log printed
  `server DMA import enabled dma_dev=0000:03:00.0`,
  `server ready name=homer-dpu-comch dev=0000:03:00.0 rep=0000:21:00.0
  timeout_ms=20000`, and `homer_dpu_comch_transport_smoke: ok`.
- After runtime validation, the default non-DOCA extension was reinstalled,
  `ldd /data/dbcomm/pg-citus/lib/x86_64-linux-gnu/postgresql/citus.so` showed no
  DOCA dependency, and PostgreSQL was restarted back into that default runtime
  shape.

## Stage 6B.1: Grouped-Control DMA Submit And PE Drain

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:82`
  defines `HomerDpuDmaProgressResult`, the scheduler-facing result summary for
  submitted tasks, completed tasks, failed tasks, PE progress calls, empty polls,
  budget exhaustion, and fatal errors.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:101`
  extends `HomerDpuDmaImportHostMmapDescriptorForSetup()` so COMCH setup passes
  the parsed `HomerDpuBridgeControlBlockHeader` and
  `HomerDpuBridgeRingDescriptor[]` into the DMA engine import table.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c:315`
  now forwards those parsed bridge metadata pointers from
  `HomerServiceDpuComchHandleSetupPayload()` into the DMA engine instead of
  importing only raw mmap export bytes.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:109`
  exposes `HomerDpuDmaSubmitGroupedControlReads()`,
  `HomerDpuDmaDrainPe()`, and `HomerDpuDmaCopyGroupedControlSnapshot()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:376`
  implements the bounded submit API. It walks active imported host mmaps, skips
  rings with an in-flight control read, submits at most the granted task budget,
  and does not call `doca_pe_progress()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:462`
  implements the bounded PE-drain API. It calls `doca_pe_progress()` at most the
  requested poll budget and stops on the first zero-progress return.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:530`
  copies a completed grouped-control snapshot only after the callback has marked
  that ring snapshot valid.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1015`
  builds the source and destination `doca_buf` objects for one host-publish-line
  read. The source buffer must call `doca_buf_set_data()` at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1023`;
  without this, the real DPU read task submitted but completed with
  `Input/Output Operation Failed`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1064`
  submits grouped-control discovery reads with `doca_task_submit_ex(...,
  DOCA_TASK_SUBMIT_FLAG_FLUSH)`. Discovery reads are boundary tasks, not
  optimized-report payload tasks, because there may be no later sentinel to flush
  them.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1487`
  and `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1513`
  retire successful/error DMA callbacks. Callback code validates the task slot
  identity/generation, does not call PE progress, frees task/buffer ownership,
  marks successful snapshots visible, and treats DMA task failure as fatal.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:35870`
  now lets `HOMER_PROGRESS_ACTION_DPU_PE_DRAIN` drain both the COMCH server PE
  and the DMA engine PE under the same granted poll budget.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:35929`
  now lets `HOMER_PROGRESS_ACTION_DPU_GROUPED_CONTROL_READ` submit bounded
  grouped-control reads through the DMA engine.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:58`
  adds `--submit-control-read` to the host/DPU COMCH transport smoke. The host
  client writes a synthetic host-publish line into the exported mmap, and the DPU
  server imports the mmap, submits one grouped-control DMA read, drains it, checks
  the copied epoch/tail/cookie, and only then sends setup ack.

Important implementation details and decisions:

- Stage 6B.1 explicitly defers close/close-ack to Stage 10 teardown. The current
  slice needed the real grouped-control DMA submit/drain path before command
  pulling can start; adding close now would not validate the hot discovery
  invariant.
- The production engine stores copied bridge metadata in
  `HomerDpuDmaHostMmapImport`. A later task must not rely on transient COMCH
  receive buffers after the callback returns.
- The submit action and drain action are intentionally separate. Submit queues
  DMA tasks and returns; only PE drain invokes callbacks and makes snapshots
  visible.
- The host-local DOCA DMA smoke keeps the grouped-control DMA read behind
  `HOMER_DPU_DMA_SMOKE_RUN_GROUPED_CONTROL_READ`, because a same-host
  create-from-export path produced a DMA `Input/Output Operation Failed` and is
  not the target host-DPU mapping. The meaningful validation is the
  host-to-DPU transport smoke below.

Stage 6B.1 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make service-dpu-dma-smoke service-dpu-dma-doca-smoke \
  dpu-comch-transport-smoke-bin service-bin client-bin

# DPU-side AArch64 build from the same changed sources.
DPU_DIR=/tmp/homer_dpu_comch_stage6b_control_read
ssh dpu "cd $DPU_DIR && gcc -std=gnu99 -Wall -Wextra \
  -Wno-unused-parameter -Wno-sign-compare -Wno-missing-field-initializers \
  -Wno-deprecated-declarations -DHOMER_DPU_DMA_WITH_DOCA \
  -DALLOW_EXPERIMENTAL_API -I/opt/mellanox/doca/include \
  -I/usr/include/libnl3 -Isrc/include \
  -Isrc/backend/distributed/utils/homer \
  -o homer_dpu_comch_transport_smoke \
  src/bin/homer_dpu_comch_transport_smoke.c \
  src/backend/distributed/utils/homer/homer_service_dpu_dma.c \
  src/backend/distributed/utils/homer/homer_service_dpu_comch.c \
  -L/opt/mellanox/doca/lib/aarch64-linux-gnu \
  -ldoca_comch -ldoca_dma -ldoca_common"

ssh dpu "cd /tmp/homer_dpu_comch_stage6b_control_read && \
  LD_LIBRARY_PATH=/opt/mellanox/doca/lib/aarch64-linux-gnu \
  ./homer_dpu_comch_transport_smoke --server --import-dma \
  --submit-control-read --name homer-dpu-comch-stage6b-data \
  --dev-pci 0000:03:00.0 --rep-pci 0000:21:00.0 \
  --timeout-ms 20000 \
  > /tmp/homer-dpu-comch-stage6b-data-server.log 2>&1" &

LD_LIBRARY_PATH=/opt/mellanox/doca/lib/x86_64-linux-gnu \
  ./build/homer/homer_dpu_comch_transport_smoke --client --real-mmap \
  --name homer-dpu-comch-stage6b-data --dev-pci 0000:21:00.0 \
  --timeout-ms 20000
```

Observed result:

- `homer_service_dpu_dma_smoke: ok`
- `homer_service_dpu_dma_smoke: ok` for the DOCA-enabled build, with only DOCA
  experimental/deprecation header warnings.
- `service-bin` and `client-bin` built.
- The first real host-to-DPU grouped-control DMA read attempt failed until the
  source `doca_buf_set_data()` call was added. After that fix, the host client
  printed `client received setup ack generation=1 rings=1 imported_bytes=280` and
  exited `ok`.
- The DPU server log contained
  `server DMA grouped-control read complete epoch=1 tail=17 cookie=65261` and
  `homer_dpu_comch_transport_smoke: ok`.

## Stage 6B.2: Grouped-Control Semantic Ready Facts

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:99`
  extends `HomerDpuDmaRingRuntime` with accepted host-publish state:
  `hostPublishAccepted`, `discoveredReady`, `acceptedPublishedEpoch`,
  `acceptedPublishedTail`, and the last accepted
  `HomerDpuBridgeHostPublishLine`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:894`
  keeps physical DMA task retirement as the only place that can turn a completed
  grouped-control read into semantic state. The completion callback still does
  not call `doca_pe_progress()` or submit follow-on work.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1108`
  adds `HomerDpuDmaAcceptGroupedControlSnapshot()`. It treats an unchanged
  `publishedEpoch` as no new semantic work, because the host may already be
  preparing the next line body while the old publication word is still visible.
  Only an advanced nonzero epoch with matching generation/ring identity and a
  monotonic `publishedTail` can update the accepted frontier.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:367`
  already rolls per-class `discoveredReadyRingCount` into
  `HomerDpuDmaSchedulerFacts.totalDiscoveredReadyRingCount`; Stage 6B.2 now
  populates that count after semantic acceptance.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:766`
  checks that the real DPU server observes
  `totalDiscoveredReadyRingCount == 1` after the grouped-control read completes.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_dma_smoke.c:248`
  checks the same ready-ring fact in the optional same-process grouped-control
  read path.

Important implementation details and decisions:

- The accepted publication epoch is the semantic gate. Stage 6B.2 deliberately
  does **not** validate body fields when the epoch is unchanged; doing so would
  turn a harmless observation of a host-side in-progress rewrite into a false
  fatal error.
- Identity mismatch, generation mismatch, unknown entry state, fatal ring flag,
  unknown workload class, and frontier regression are treated as engine-fatal
  bug-like validation failures. This matches the current migration assumption
  that malformed bridge control data means the selected DPU path is unsafe.
- `discoveredReadyRingCount` counts rings with accepted unread frontier, not
  individual records. Stage 7 should consume this by adding a command-pull API
  that reads the accepted ring/frontier state and clears or advances the ready
  bit only after command request DMA pull has accepted the corresponding work.

Stage 6B.2 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make service-dpu-dma-smoke service-dpu-dma-doca-smoke \
  dpu-comch-transport-smoke-bin service-bin client-bin

# DPU-side AArch64 rebuild from the changed sources.
DPU_DIR=/tmp/homer_dpu_comch_stage6b_ready_facts
ssh dpu "cd $DPU_DIR && gcc -std=gnu99 -Wall -Wextra \
  -Wno-unused-parameter -Wno-sign-compare -Wno-missing-field-initializers \
  -Wno-deprecated-declarations -DHOMER_DPU_DMA_WITH_DOCA \
  -DALLOW_EXPERIMENTAL_API -I/opt/mellanox/doca/include \
  -I/usr/include/libnl3 -Isrc/include \
  -Isrc/backend/distributed/utils/homer \
  -o homer_dpu_comch_transport_smoke \
  src/bin/homer_dpu_comch_transport_smoke.c \
  src/backend/distributed/utils/homer/homer_service_dpu_dma.c \
  src/backend/distributed/utils/homer/homer_service_dpu_comch.c \
  -L/opt/mellanox/doca/lib/aarch64-linux-gnu \
  -ldoca_comch -ldoca_dma -ldoca_common"

ssh dpu "cd /tmp/homer_dpu_comch_stage6b_ready_facts && \
  LD_LIBRARY_PATH=/opt/mellanox/doca/lib/aarch64-linux-gnu \
  ./homer_dpu_comch_transport_smoke --server --import-dma \
  --submit-control-read --name homer-dpu-comch-stage6b-ready \
  --dev-pci 0000:03:00.0 --rep-pci 0000:21:00.0 \
  --timeout-ms 20000 \
  > /tmp/homer-dpu-comch-stage6b-ready-server.log 2>&1" &

LD_LIBRARY_PATH=/opt/mellanox/doca/lib/x86_64-linux-gnu \
  ./build/homer/homer_dpu_comch_transport_smoke --client --real-mmap \
  --name homer-dpu-comch-stage6b-ready --dev-pci 0000:21:00.0 \
  --timeout-ms 20000
```

Observed result:

- `homer_service_dpu_dma_smoke: ok`
- `homer_service_dpu_dma_smoke: ok` for the DOCA-enabled build, with only the
  known DOCA experimental/deprecation warnings.
- `dpu-comch-transport-smoke-bin`, `service-bin`, and `client-bin` built.
- Host client printed `client received setup ack generation=1 rings=1
  imported_bytes=276` and exited `ok`.
- The DPU server log contained
  `server DMA grouped-control read complete epoch=1 tail=17 cookie=65261` and
  `homer_dpu_comch_transport_smoke: ok`. The server would have failed before ack
  if the new ready-fact assertion had not observed exactly one discovered ready
  ring.

## Stage 7A: Synthetic Fixed Command-Slot Pull

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:174`
  gives each descriptor a `hostRingOffset` separate from `hostControlOffset`.
  This lets one host mmap carry grouped-control lines and command/control slots
  without assuming the slot area begins at the same offset as the publication
  line.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:118`
  exposes `HomerDpuDmaSubmitCommandPulls()`, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:126`
  exposes `HomerDpuDmaCopyStagedCommandSlot()` for the Stage 7 service adapter.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:508`
  implements the bounded command-pull submit action. It consumes only accepted
  Stage 6B ready facts, submits DMA tasks without calling `doca_pe_progress()`,
  and treats already-consumed ready facts as stale scheduler state to clear, not
  as action failures.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1366`
  implements `HomerDpuDmaSubmitOneCommandPull()`. It validates command
  descriptor identity, generation, direction, slot geometry, and
  `hostRingOffset`, then DMA-reads one full `CitusRemoteExecControlSlot` into a
  preallocated DPU-local command staging buffer.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1656`
  implements `HomerDpuDmaAcceptCommandPullSlot()`. It validates task owner
  identity, accepted frontier epoch, slot state, owner pid, request sequence,
  request protocol version, and known request kind before marking one staged
  command request visible in DPU-local facts.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:35985`
  wires `HOMER_PROGRESS_ACTION_DPU_COMMAND_PULL` to
  `HomerDpuDmaSubmitCommandPulls()` through the current scheduler adapter.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:452`
  builds a setup descriptor whose `hostRingOffset` points past the three bridge
  control cache lines to a synthetic fixed control slot.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:798`
  validates the staged command slot after the DPU command-pull DMA completes:
  state `REQUEST_READY`, owner pid `4242`, request sequence `42`,
  `START_COMMAND`, command kind `CITUS_REMOTE_EXEC_COMMAND_SQL_EXECUTE`, and SQL
  bytes `select 1`.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:877`
  submits the command pull only after the grouped-control DMA read has completed
  and produced an accepted ready fact.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_abi_check.c:93`
  now exercises `hostRingOffset` and the full fixed-control-slot size in the
  host-only COMCH ABI check.

Important implementation details and decisions:

- Stage 7A deliberately pulls a complete `CitusRemoteExecControlSlot`, not only
  `CitusRemoteExecControlRequestUnion`. This is heavier than the eventual
  minimum request bytes, but it keeps the first migration slice faithful to the
  current fixed-slot semantic boundary: state, owner pid, request sequence,
  request body, and response storage stay together until the service adapter is
  refactored.
- The command staging buffers are separate from task slots. A completed command
  pull keeps its staging buffer reserved while
  `HomerDpuDmaCopyStagedCommandSlot()` can copy it for the later service adapter;
  task-slot retirement and staged-command consumption are separate lifetimes.
- A stale ready fact whose accepted frontier has already been consumed is cleared
  and counted as empty scheduler state. It is not a DMA error and not a fatal
  protocol condition.

Stage 7A was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make dpu-comch-abi-check dpu-comch-transport-smoke-bin \
  service-dpu-dma-smoke service-dpu-dma-doca-smoke service-bin client-bin

DPU_DIR=/tmp/homer_dpu_comch_stage7_command_pull
ssh dpu "rm -rf $DPU_DIR && mkdir -p \
  $DPU_DIR/src/bin \
  $DPU_DIR/src/backend/distributed/utils/homer \
  $DPU_DIR/src/include/distributed/homer"
rsync -az src/bin/homer_dpu_comch_transport_smoke.c \
  dpu:$DPU_DIR/src/bin/
rsync -az src/backend/distributed/utils/homer/homer_service_dpu_dma.c \
  src/backend/distributed/utils/homer/homer_service_dpu_dma.h \
  src/backend/distributed/utils/homer/homer_service_dpu_comch.c \
  src/backend/distributed/utils/homer/homer_service_dpu_comch.h \
  dpu:$DPU_DIR/src/backend/distributed/utils/homer/
rsync -az src/include/distributed/homer/*.h \
  dpu:$DPU_DIR/src/include/distributed/homer/
ssh dpu "cd $DPU_DIR && gcc -std=gnu99 -Wall -Wextra \
  -Wno-unused-parameter -Wno-sign-compare -Wno-missing-field-initializers \
  -Wno-deprecated-declarations -DHOMER_DPU_DMA_WITH_DOCA \
  -DALLOW_EXPERIMENTAL_API -I/opt/mellanox/doca/include \
  -I/usr/include/libnl3 -Isrc/include \
  -Isrc/backend/distributed/utils/homer \
  -o homer_dpu_comch_transport_smoke \
  src/bin/homer_dpu_comch_transport_smoke.c \
  src/backend/distributed/utils/homer/homer_service_dpu_dma.c \
  src/backend/distributed/utils/homer/homer_service_dpu_comch.c \
  -L/opt/mellanox/doca/lib/aarch64-linux-gnu \
  -ldoca_comch -ldoca_dma -ldoca_common"

ssh dpu "cd /tmp/homer_dpu_comch_stage7_command_pull && \
  LD_LIBRARY_PATH=/opt/mellanox/doca/lib/aarch64-linux-gnu \
  ./homer_dpu_comch_transport_smoke --server --import-dma \
  --submit-control-read --submit-command-pull \
  --name homer-dpu-comch-stage7-command \
  --dev-pci 0000:03:00.0 --rep-pci 0000:21:00.0 \
  --timeout-ms 20000 \
  > /tmp/homer-dpu-comch-stage7-command-server.log 2>&1" &

LD_LIBRARY_PATH=/opt/mellanox/doca/lib/x86_64-linux-gnu \
  ./build/homer/homer_dpu_comch_transport_smoke --client --real-mmap \
  --name homer-dpu-comch-stage7-command --dev-pci 0000:21:00.0 \
  --timeout-ms 20000

ssh dpu "cat /tmp/homer-dpu-comch-stage7-command-server.log"
```

Observed result:

- `homer_dpu_comch_abi_check: ok`
- `homer_service_dpu_dma_smoke: ok`
- `homer_service_dpu_dma_smoke: ok` for the DOCA-enabled build, with only the
  known DOCA experimental/deprecation warnings.
- `dpu-comch-transport-smoke-bin`, `service-bin`, and `client-bin` built.
- Host client printed `client exported real PCI mmap bytes=279
  buffer_bytes=68056`, `client received setup ack generation=1 rings=1
  imported_bytes=279`, and exited `ok`.
- The DPU server log contained
  `server DMA grouped-control read complete epoch=1 tail=1 cookie=65261`,
  `server DMA command-pull submitted after accepted grouped-control frontier`,
  `server DMA command-pull complete owner=4242 seq=42 command=6 sql="select 1"`,
  and `homer_dpu_comch_transport_smoke: ok`.

## Stage 7B.1: Local-Control Dispatch Boundary

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31389`
  adds `TupleSinkServiceLocalControlDispatchResult`, which distinguishes a
  request that completed synchronously from one whose slot is owned by an async
  local-control continuation.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34217`
  adds `TupleSinkServiceDispatchLocalControlSlot()`. It runs the existing
  local-control semantic request dispatch for open, close,
  report-post-command-state, start-command, poll-completion, and unknown request
  errors without publishing the transport-specific response-visible state.
- Stage 7B.1 originally made async continuations explicit through an
  `allowAsyncSlotContinuation` guard. Stage 7B.2 replaces that guard with an
  explicit response-owner argument, so the current code no longer carries the
  boolean form.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34330`
  changes `TupleSinkServicePumpControlSlots()` to call the shared dispatcher and
  keep only the SHM-specific publication step: store
  `CITUS_REMOTE_EXEC_CONTROL_SLOT_RESPONSE_READY`, publish heartbeat, and update
  local-control feedback.

Important implementation details and decisions:

- This slice intentionally does not make DPU-staged commands executable yet. It
  removes the first coupling between semantic dispatch and SHM response
  publication. Stage 7B.2 removes the remaining raw `slotIndex` ownership from
  async continuation state, but selected-DPU commands still need a DPU response
  owner and DMA publication path before they can run.
- The next DPU command-execution slice needs a response-owner abstraction for
  staged commands. That owner must tell an async continuation where to publish
  the eventual response: SHM slot today, DPU DMA response body plus publication
  word later. Without that owner, enabling async DPU commands would risk writing
  a response into the wrong SHM slot.
- Synchronous requests can now be driven through the same helper once the DPU
  path has a response-publish action, but selected-DPU command API success should
  still wait for response DMA publication rather than returning a host-local
  result.

Stage 7B.1 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
git clang-format HEAD -- src/backend/distributed/utils/homer/tuple_sink_service_process.c
sudo -n -u dbcomm make service-bin client-bin
git diff --check
```

Observed result:

- `service-bin` rebuilt successfully.
- `client-bin` was up to date.
- `git diff --check` reported no whitespace errors.

## Stage 7B.2: SHM Local-Control Response Owner

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31395`
  adds `TupleSinkServiceLocalControlResponseOwnerKind`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31406`
  adds `TupleSinkServiceLocalControlResponseOwner`, which currently has one
  implemented owner kind: an SHM control slot.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31418`
  changes `TupleSinkServiceLocalControlAsyncOp` to store `responseOwner` instead
  of a raw `slotIndex`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31447`
  adds `TupleSinkServiceMakeShmLocalControlResponseOwner()` for the current SHM
  control-slot caller.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31463`
  adds `TupleSinkServiceLocalControlResponseOwnerShmSlotIndex()`. It deliberately
  accepts only SHM-slot owners so a future DPU owner cannot be silently coerced
  into a host-process control slot.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31593`
  updates `TupleSinkServiceLocalControlAsyncSlotMatches()` to require that the
  active async op is owned by the same SHM slot before suppressing duplicate
  local-control collection.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:32864`
  makes the SHM async pump resolve through the owner before touching
  `controlRegion->slots[]`. A non-SHM or invalid owner aborts because that means
  a future DPU continuation reached the wrong publication path.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:32988`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:33033`,
  and `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:33105`
  change async open, start-command, and poll-completion registration to accept a
  response owner and copy it into the async op.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34217`
  changes `TupleSinkServiceDispatchLocalControlSlot()` to take a response-owner
  pointer instead of a boolean async guard.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34361`
  creates the SHM response owner in `TupleSinkServicePumpControlSlots()` before
  calling the shared dispatcher.

Important implementation details and decisions:

- Stage 7B.2 is intentionally SHM-owner-only. This is enough to remove the raw
  slot-index assumption from current async state without inventing the DPU
  response publication owner before Stage 8 defines the DMA body+publish shape.
- A non-SHM response owner in the SHM async pump is treated as a service bug and
  aborts. Expected lifecycle staleness is still handled by the existing
  request-sequence and slot-state checks; the abort is only for an impossible
  owner/path mismatch.
- This is a refactor validation stage. It preserves current SHM command
  behavior and does not remove the selected-DPU command `not implemented` guard.

Stage 7B.2 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
git clang-format HEAD -- src/backend/distributed/utils/homer/tuple_sink_service_process.c
sudo -n -u dbcomm make service-bin client-bin
git diff --check
```

Observed result:

- `service-bin` rebuilt successfully.
- `client-bin` was up to date.
- `git diff --check` reported no whitespace errors.

## Stage 7B.3: Staged Command Handoff Lifetime

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:101`
  adds `HomerDpuDmaStagedCommandSlot`, the service-side handoff record for a
  pulled command slot plus import, ring, bridge generation, ring generation,
  command ordinal, request sequence, and accepted publication epoch.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:144`
  declares `HomerDpuDmaCopyStagedCommand()`, which returns the copied command
  slot plus owner metadata.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:150`
  declares `HomerDpuDmaReleaseStagedCommandSlot()`, which releases the
  engine-owned command-pull staging buffer after the service has copied the
  request and owner metadata into its own dispatch state.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:723`
  implements `HomerDpuDmaCopyStagedCommand()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:803`
  implements `HomerDpuDmaReleaseStagedCommandSlot()`. It validates import/ring
  identity, bridge/ring generation, command ordinal, accepted publication epoch,
  request sequence, and request kind before releasing the staging buffer.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1023`
  adds `HomerDpuDmaClearStagedCommandSlot()` and reuses it from teardown so
  explicit release and import clearing maintain the same staged-count and buffer
  state.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:798`
  changes the command-pull smoke to copy the rich staged-command record.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:838`
  releases the staged command after validation and checks that scheduler facts
  report `totalStagedCommandRequestCount == 0`.

Important implementation details and decisions:

- The staging buffer is DMA-engine-owned until the service has copied both the
  protocol slot and owner metadata into service-local dispatch state. Releasing
  it earlier would lose the owner metadata needed for Stage 8 response
  publication; keeping it forever would block additional command pulls for that
  ring.
- Stage 7B.3 deliberately does not execute the staged command or publish a
  response. It only defines and validates the handoff lifetime needed before a
  service adapter can safely consume staged commands.
- The older `HomerDpuDmaCopyStagedCommandSlot()` remains as a compatibility
  helper for tests that only need the copied protocol slot. New service adapter
  code should use `HomerDpuDmaCopyStagedCommand()`.

Stage 7B.3 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
git clang-format HEAD -- \
  src/backend/distributed/utils/homer/homer_service_dpu_dma.h \
  src/backend/distributed/utils/homer/homer_service_dpu_dma.c \
  src/bin/homer_dpu_comch_transport_smoke.c
sudo -n -u dbcomm make dpu-comch-transport-smoke-bin service-bin client-bin
git diff --check

DPU_DIR=/tmp/homer_dpu_comch_stage7b3_release
ssh dpu "rm -rf $DPU_DIR && mkdir -p \
  $DPU_DIR/src/bin \
  $DPU_DIR/src/backend/distributed/utils/homer \
  $DPU_DIR/src/include/distributed/homer"
rsync -az src/bin/homer_dpu_comch_transport_smoke.c \
  dpu:$DPU_DIR/src/bin/
rsync -az src/backend/distributed/utils/homer/homer_service_dpu_dma.c \
  src/backend/distributed/utils/homer/homer_service_dpu_dma.h \
  src/backend/distributed/utils/homer/homer_service_dpu_comch.c \
  src/backend/distributed/utils/homer/homer_service_dpu_comch.h \
  dpu:$DPU_DIR/src/backend/distributed/utils/homer/
rsync -az src/include/distributed/homer/*.h \
  dpu:$DPU_DIR/src/include/distributed/homer/
ssh dpu "cd $DPU_DIR && gcc -std=gnu99 -Wall -Wextra \
  -Wno-unused-parameter -Wno-sign-compare -Wno-missing-field-initializers \
  -Wno-deprecated-declarations -DHOMER_DPU_DMA_WITH_DOCA \
  -DALLOW_EXPERIMENTAL_API -I/opt/mellanox/doca/include \
  -I/usr/include/libnl3 -Isrc/include \
  -Isrc/backend/distributed/utils/homer \
  -o homer_dpu_comch_transport_smoke \
  src/bin/homer_dpu_comch_transport_smoke.c \
  src/backend/distributed/utils/homer/homer_service_dpu_dma.c \
  src/backend/distributed/utils/homer/homer_service_dpu_comch.c \
  -L/opt/mellanox/doca/lib/aarch64-linux-gnu \
  -ldoca_comch -ldoca_dma -ldoca_common"
```

The final host-to-DPU smoke used:

```sh
ssh dpu "cd /tmp/homer_dpu_comch_stage7b3_release && \
  LD_LIBRARY_PATH=/opt/mellanox/doca/lib/aarch64-linux-gnu \
  ./homer_dpu_comch_transport_smoke --server --import-dma \
  --submit-control-read --submit-command-pull \
  --name homer-dpu-comch-stage7b3-release \
  --dev-pci 0000:03:00.0 --rep-pci 0000:21:00.0 \
  --timeout-ms 20000 \
  > /tmp/homer-dpu-comch-stage7b3-release-server.log 2>&1" &

LD_LIBRARY_PATH=/opt/mellanox/doca/lib/x86_64-linux-gnu \
  ./build/homer/homer_dpu_comch_transport_smoke --client --real-mmap \
  --name homer-dpu-comch-stage7b3-release \
  --dev-pci 0000:21:00.0 \
  --timeout-ms 20000
```

Observed result:

- The local `dpu-comch-transport-smoke-bin`, `service-bin`, and `client-bin`
  build passed. DOCA headers emitted only the known experimental/deprecation
  warnings.
- `git diff --check` reported no whitespace errors.
- The DPU-side aarch64 smoke binary built under
  `/tmp/homer_dpu_comch_stage7b3_release`.
- The host client printed `homer_dpu_comch_transport_smoke: ok`.
- The DPU server log contained:

  ```text
  server DMA grouped-control read complete epoch=1 tail=1 cookie=65261
  server DMA command-pull submitted after accepted grouped-control frontier
  server DMA command-pull complete owner=4242 seq=42 ordinal=0 command=6 sql="select 1"
  homer_dpu_comch_transport_smoke: ok
  ```

- The wrapper reported `client_rc=0 server_rc=0`.

## Stage 7B.4: DPU Response-Owner Async Guard

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31399`
  adds `TUPLE_SINK_SERVICE_LOCAL_CONTROL_RESPONSE_OWNER_DPU_STAGED_COMMAND`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31407`
  adds `TupleSinkServiceLocalControlDpuStagedCommandResponseOwner`, carrying the
  import index, ring index, bridge generation, ring generation, command ordinal,
  request sequence, and accepted publication epoch that Stage 8 will need for
  response DMA body+publish work.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31424`
  adds that DPU owner to the `TupleSinkServiceLocalControlResponseOwner` union.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31499`
  adds `TupleSinkServiceLocalControlResponseOwnerCanRegisterAsync()`. It returns
  true only for SHM-slot owners because only the SHM async pump can currently
  resume and publish an async response.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34257`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34298`,
  and `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34329`
  make async OPEN_SESSION, START_COMMAND, and POLL_COMMAND_COMPLETION return
  explicit Stage 8-style errors if a non-SHM owner tries to register an async
  continuation.

Important implementation details and decisions:

- DPU response-owner metadata now has a concrete service-local shape, but there
  is still no DPU async continuation queue and no DMA response publication path.
  Therefore the dispatcher refuses to register async work for DPU-staged owners
  instead of creating an op that the SHM async pump cannot safely publish.
- This slice prepares the next staged-command dispatch adapter: synchronous
  local-control requests can carry a DPU owner through the shared dispatcher, and
  async-shaped requests fail explicitly until Stage 8 adds response publication.

Stage 7B.4 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
git clang-format HEAD -- src/backend/distributed/utils/homer/tuple_sink_service_process.c
sudo -n -u dbcomm make service-bin client-bin
git diff --check
```

Observed result:

- `service-bin` rebuilt successfully.
- `client-bin` was up to date.
- `git diff --check` reported no whitespace errors.

## Stage 7B.5: Service-Owned Staged Command Dispatch Queue

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:147`
  adds `HomerDpuDmaCopyNextStagedCommand()`, an engine-level helper that finds
  the next engine-owned staged command without exposing the import/ring tables to
  the scheduler adapter.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:778`
  implements that helper by scanning active imports and rings for a
  `stagedCommandSlotValid` runtime entry, then delegating to
  `HomerDpuDmaCopyStagedCommand()` for the validated copy.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:493`
  adds `HOMER_SERVICE_DPU_STAGED_COMMAND_DISPATCH_SLOT_COUNT`, currently `16`.
  This is a bounded service-owned handoff queue, not an unbounded semantic work
  list.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:508`
  adds `HomerServiceDpuStagedCommandDispatchSlot`; and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:517`
  extends `HomerServiceDpuDmaSchedulerState` with the queue, current count, and
  high-water diagnostic.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2128`
  and `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2165`
  add `HOMER_PROGRESS_COLLECTOR_DPU_STAGED_COMMAND_DISPATCH` and
  `HOMER_PROGRESS_ACTION_DPU_STAGED_COMMAND_DISPATCH`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34613`
  through `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34705`
  add the bounded queue helpers and `HomerServiceDpuStageOneCommandForDispatch()`.
  The helper copies the staged command into a stack handoff record, releases the
  engine staging buffer with `HomerDpuDmaReleaseStagedCommandSlot()`, then makes
  the service-owned dispatch slot visible.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34775`
  appends the new collector only when
  `HomerDpuDmaSchedulerFacts.totalStagedCommandRequestCount > 0` and the
  service-owned queue has free slots.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36311`
  implements the bounded action executor. It consumes at most the grant's
  `maxItems`, does not call `doca_pe_progress()`, and reports
  `itemsProcessed`, `emptyPolls`, `stillReady`, and `budgetExhausted` through the
  existing grant-result path.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_transport_smoke.c:798`
  now validates the new engine helper by using
  `HomerDpuDmaCopyNextStagedCommand()` before releasing the staged command.

Important implementation details and decisions:

- The new dispatch queue is intentionally service-owned but not yet consumed by
  semantic command execution. This prevents Stage 7 from dropping a response
  before Stage 8 has DPU response-body and publication-word DMA writes.
- The scheduler action is separate from `DPU_COMMAND_PULL`: command-pull submits
  physical DMA reads, PE drain retires them, and staged-command dispatch moves
  completed request records into service-owned state. Keeping those as separate
  actions preserves the nonblocking PE-drain invariant and makes later response
  publication easier to schedule.
- The engine helper hides import/ring iteration from
  `tuple_sink_service_process.c`. The large file remains a scheduler adapter; it
  does not learn the DMA engine's import table layout.
- The dispatch queue can fill because Stage 7B.5 does not consume it yet. When
  full, candidate construction stops scheduling staged-command dispatch. Later
  semantic dispatch/response-publication work must consume entries before the
  selected-DPU command path can be accepted.

Stage 7B.5 was validated with:

```sh
cd /data/dbcomm/citus-dbcomm
git clang-format HEAD -- \
  src/backend/distributed/utils/homer/homer_service_dpu_dma.c \
  src/backend/distributed/utils/homer/homer_service_dpu_dma.h \
  src/backend/distributed/utils/homer/tuple_sink_service_process.c \
  src/bin/homer_dpu_comch_transport_smoke.c
sudo -n -u dbcomm make dpu-comch-transport-smoke-bin service-bin client-bin
git diff --check
```

The DPU runtime smoke was rebuilt natively on `ssh dpu` under
`/tmp/homer_dpu_comch_stage7_dispatch_1782632654` and run with:

```sh
# DPU
./homer_dpu_comch_transport_smoke --server --import-dma \
  --submit-control-read --submit-command-pull \
  --name homer-dpu-comch-stage7-dispatch-rerun-1782632703 \
  --dev-pci 0000:03:00.0 --rep-pci 0000:21:00.0 --timeout-ms 20000

# Host
./build/homer/homer_dpu_comch_transport_smoke --client --real-mmap \
  --name homer-dpu-comch-stage7-dispatch-rerun-1782632703 \
  --dev-pci 0000:21:00.0 --timeout-ms 20000
```

Observed result:

- `service-bin`, `client-bin`, and `dpu-comch-transport-smoke-bin` built
  successfully. The only warnings were the existing DOCA deprecated/experimental
  API warnings for `doca_buf_inventory_buf_reuse_by_args()` and
  `doca_task_submit_ex()`.
- `git diff --check` reported no whitespace errors.
- The runtime smoke reported `client_rc=0 server_rc=0`.
- The client printed `homer_dpu_comch_transport_smoke: ok`.
- The server printed `server DMA grouped-control read complete epoch=1 tail=1
  cookie=65261`, `server DMA command-pull submitted after accepted
  grouped-control frontier`, `server DMA command-pull complete owner=4242 seq=42
  ordinal=0 command=6 sql="select 1"`, and
  `homer_dpu_comch_transport_smoke: ok`.

## Stage 6/8 Transport Pivot: TCP Setup Replaces COMCH

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_setup_tcp.c`
  adds a reusable service-side setup listener. It accepts one nonblocking TCP
  client at a time, reads exactly one existing setup ABI payload, validates and
  imports the host mmap descriptor through the DPU DMA engine, writes one setup
  ack, and closes the client. The module intentionally performs no hot-path
  data movement.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c`
  keeps ownership of host-side DOCA mmap export but replaces the COMCH client
  with a bounded TCP connect/write/read setup exchange. The active overrides are
  `HOMER_FRONTEND_DPU_SETUP_HOST`, `HOMER_FRONTEND_DPU_SETUP_PORT`,
  `HOMER_FRONTEND_DOCA_DEV_PCI`, and `HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  now creates `HomerServiceDpuSetupTcpServer` when DPU DMA and DOCA are enabled,
  progresses it from `HOMER_PROGRESS_ACTION_DPU_PE_DRAIN`, and treats an active
  setup connection as known scheduler work. An idle listener remains blind
  polling under scheduler feedback.
- `/data/dbcomm/citus-dbcomm/Makefile` links production
  `citus_tuple_sink_service` against `homer_service_dpu_setup_tcp.c` plus
  `doca-dma`/`doca-common` only. The active production path no longer links
  `doca-comch`.

Important decision:

- COMCH is no longer a required Homer setup dependency. It was only a cold-path
  descriptor transport, and it failed below Homer during later farnet1
  validation even after driver/firmware resets. TCP is sufficient for this phase
  because all performance-critical publication and data movement still happen
  through DOCA DMA-visible host memory. The old `HomerDpuComch*` ABI names are a
  temporary compatibility artifact and should be renamed only after the migration
  stabilizes.

## Stage 8A: DPU DMA Response Publication

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c`
  implements `HomerDpuDmaSubmitCommandResponsePublication()`. It DMA-writes the
  completed control response body and then submits the response-ready publication
  word on the same ordered command DMA context. The body task uses optimized
  completion reporting, while the publication task is the explicit flush/report
  boundary.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  adds the pending-response slots, the
  `HOMER_PROGRESS_ACTION_DPU_STAGED_COMMAND_EXECUTE` action that copies executed
  semantic responses into service-owned pending state, and the
  `HOMER_PROGRESS_ACTION_DPU_COMPLETION_PUSH` action that submits the DMA
  response publication work. Completion retirement still happens through the
  bounded PE-drain action; submit actions do not spin waiting for their own DMA
  completions.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_tcp_transport_smoke.c` validates
  the current end-to-end DPU pipeline without requiring the SQL frontend path to
  be runnable yet. The server side uses the production TCP setup listener, then
  directly drives grouped-control read, command pull, and response publication.
  The host side exports a real DOCA mmap, sends setup over TCP, and polls the
  command slot until the DPU-written response-ready state is visible.

Validation on June 28, 2026:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make -j8 service-bin client-bin \
  CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA'
sudo -n -u dbcomm make -j8 -C src/backend/distributed \
  CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA'
sudo -n -u dbcomm make dpu-tcp-transport-smoke-bin CPPFLAGS='-D_GNU_SOURCE'
git diff --check
```

DPU-side smoke compile/run:

```sh
# DPU native compile used copied source/header files under /tmp/homer_dpu_tcp_smoke.
gcc -std=gnu99 -Wall -Wextra -Werror=vla \
  -Wno-unused-parameter -Wno-sign-compare \
  -Wno-missing-field-initializers -Wno-declaration-after-statement \
  -DHOMER_DPU_DMA_WITH_DOCA \
  -I src/include -I src/backend/distributed/utils/homer \
  $(pkg-config --cflags doca-dma doca-common) \
  -o homer_dpu_tcp_transport_smoke \
  src/bin/homer_dpu_tcp_transport_smoke.c \
  src/backend/distributed/utils/homer/homer_service_dpu_dma.c \
  src/backend/distributed/utils/homer/homer_service_dpu_setup_tcp.c \
  $(pkg-config --libs doca-dma doca-common)

# DPU
./homer_dpu_tcp_transport_smoke --server \
  --port 9727 --timeout-ms 15000

# Host
./build/homer/homer_dpu_tcp_transport_smoke --client \
  --host 10.10.1.201 --port 9727 --timeout-ms 15000 \
  --expect-response-publish
```

Observed result:

- `service-bin`, `client-bin`, extension build, and
  `dpu-tcp-transport-smoke-bin` built successfully. `ldd` on
  `build/homer/citus_tuple_sink_service` and `src/backend/distributed/citus.so`
  showed `libdoca_common.so.2` and `libdoca_dma.so.2`, with no
  `libdoca_comch`.
- `git diff --check` reported no whitespace errors.
- The cross host-DPU TCP smoke returned `client_rc=0 server_rc=0`.
- The DPU server printed `server TCP setup listening host=0.0.0.0 port=9727
  dma_dev=0000:03:00.0` and `server DMA response publication complete tasks=2`.
- The host client printed `client received TCP setup ack generation=1 rings=1
  imported_bytes=282`, then `client observed DMA response publication state=4
  command_seq=7001`.

Current limitation:

- This is strong Stage 8 mechanism evidence, but not final Stage 8 promotion
  evidence. The SQL frontend still calls `HomerFrontendDmaRaiseNotImplemented()`
  after setup smoke, so there is not yet a selected-DPU pgbench command path that
  proves completion polling has abandoned the old SHM mailbox. The next
  promotion slice must wire real frontend commands to the DPU bridge and include
  the negative acceptance run where absent/stale DPU completion publication fails
  boundedly instead of falling back to SHM.

## Stage 8B.1: Frontend Bridge Control Slot Layout

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.h`
  extends `HomerFrontendDmaBridgeState` with command-slot count, offset, size,
  and a `CitusRemoteExecControlSlot *` pointer.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c`
  now allocates one fixed control slot after the bridge header, host publish
  line, and DPU credit line. This makes the real frontend bridge layout match
  the host-DPU TCP transport smoke shape instead of depending on smoke-only
  manual allocation.
- `HomerFrontendDmaBuildSyntheticDescriptor()` now advertises
  `slotBytes = sizeof(CitusRemoteExecControlSlot)` and `hostRingOffset` equal to
  the bridge-owned command slot offset. The DPU command-pull engine already
  validates `slotBytes >= sizeof(CitusRemoteExecControlSlot)`, so this change
  turns the setup-smoke descriptor into the same layout expected by the command
  pull path.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_frontend_dma_smoke.c` checks that the
  exported layout includes the command slot and that it starts in the free
  state.

Validation:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make frontend-dma-smoke dpu-bridge-abi-check
sudo -n -u dbcomm make -j8 service-bin client-bin
sudo -n -u dbcomm make -C src/backend/distributed all
sudo -n -u dbcomm make -B -j8 service-bin client-bin \
  CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA'
git diff --check
```

Observed result:

- `homer_frontend_dma_smoke: ok`
- `homer_dpu_bridge_abi_check: ok`
- the extension build recompiled `homer_frontend_control.o` and
  `homer_frontend_dma.o`, then relinked `citus.so`;
- the forced DOCA-enabled service/client build linked successfully with
  `libdoca_dma`/`libdoca_common`;
- `git diff --check` reported no whitespace errors.

This is a layout and descriptor correctness substep, not full Stage 8B
promotion. Real selected-DPU command execution still needs the persistent
frontend channel, host lifecycle shim, backend-mailbox DMA path, and positive
plus negative no-fallback validation.

## Stage 8B.2: Persistent Frontend Control Channel Setup

Implemented in `/data/dbcomm/citus-dbcomm`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.h:40`
  declares `HomerFrontendDmaControlChannel` as an opaque frontend-owned handle.
  The handle deliberately hides the current DOCA mmap/export state so later
  frontend command code does not couple directly to setup internals.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.h:77`
  exposes `HomerFrontendDmaOpenControlChannel()`, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.h:78`
  exposes `HomerFrontendDmaCloseControlChannel()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:393`
  implements `HomerFrontendDmaOpenControlChannel()`. In non-DOCA builds it keeps
  the previous explicit error behavior. In DOCA-enabled builds it loads the
  TCP/DOCA setup configuration and calls the persistent DOCA setup opener.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:422`
  implements `HomerFrontendDmaCloseControlChannel()`. It is intentionally safe
  on `NULL` and centralizes teardown through `HomerFrontendDmaCleanupDocaSetup()`
  before freeing the opaque channel.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:500`
  keeps `HomerFrontendDmaRunDpuSetupSmoke()` as a compatibility validation path,
  but it now opens the real persistent channel and immediately closes it. This
  prevents the smoke from becoming a separate setup implementation that can
  drift from the production selected-DPU path.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:688`
  replaces the old stack-owned setup smoke with
  `HomerFrontendDmaOpenDocaControlChannel()`. That function allocates the
  channel, initializes the bridge layout, opens the DOCA device, exports the PCI
  mmap, builds and validates the TCP setup payload, waits for the DPU setup ack,
  frees only the transient setup payload, and leaves the bridge/mmap/device
  state alive until channel close.

Validation:

```sh
cd /data/dbcomm/citus-dbcomm
git diff --check
sudo -n -u dbcomm make frontend-dma-smoke dpu-bridge-abi-check
sudo -n -u dbcomm make -C src/backend/distributed all
sudo -n -u dbcomm make -B -j8 service-bin client-bin \
  CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA'
```

Observed result:

- `homer_frontend_dma_smoke: ok`
- `homer_dpu_bridge_abi_check: ok`
- the extension build completed successfully;
- the forced DOCA-enabled service/client build completed successfully, with
  only the known experimental/deprecated DOCA warnings from DOCA headers,
  `doca_task_submit_ex()`, and `doca_dma_set_ordered_completions()`;
- `git diff --check` reported no whitespace errors.

This is still not full selected-DPU SQL execution. It only turns setup from a
one-shot smoke into an owned lifecycle object. The next promotion step must be
careful not to wire the SQL API directly to DPU-local staged command dispatch:
for real `CLIENT_SQL_SESSION`, the DPU still needs a host lifecycle/backend
mailbox path so it can DMA-write commands into the host socketless backend
mailbox and DMA-read backend completions before publishing frontend responses.

## Stage 8B.3: Backend-Mailbox Scheduler Scaffold

Implemented in `/data/dbcomm/citus-dbcomm` as commit `3e5f44700`:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2138`
  adds the backend-mailbox DPU collector kinds
  `HOMER_PROGRESS_COLLECTOR_DPU_BACKEND_COMMAND_STAGE`,
  `HOMER_PROGRESS_COLLECTOR_DPU_BACKEND_COMMAND_PUBLISH`, and
  `HOMER_PROGRESS_COLLECTOR_DPU_BACKEND_COMPLETION_PULL`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2179`
  adds matching action kinds
  `HOMER_PROGRESS_ACTION_DPU_BACKEND_COMMAND_STAGE`,
  `HOMER_PROGRESS_ACTION_DPU_BACKEND_COMMAND_PUBLISH`, and
  `HOMER_PROGRESS_ACTION_DPU_BACKEND_COMPLETION_PULL`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6995`
  gives those actions stable diagnostic names, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8038`
  classifies backend command stage/publish as critical-control and backend
  completion pull as terminal-visibility.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9525`
  maps the new collectors to explicit action grants using the collector item
  budget as `grantVector.maxItems`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10395`
  places the new collectors in the `machine-baseline` collector phase around
  command pull, backend command publication, and backend completion pull. This
  preserves the rule that DPU DMA progress is scheduled as bounded collector
  work, not through a private DPU loop.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14590`
  registers the new collectors with the DPU DMA source and existing DPU wait
  classes. Backend command stage and completion pull wait on grouped-control
  discovery; backend command publish waits on host credit/task capacity.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:35093`
  extends DPU ready-set construction to use maintained facts for backend command
  staging, backend command publication, backend completion pull, and PE-drain
  work. Candidate building still only reads facts and TCP setup state; it does
  not submit DOCA tasks, call `doca_pe_progress()`, or inspect host memory.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36693`
  adds explicit bounded no-op executor branches for the new actions. They
  report one empty poll until the real backend-mailbox DMA queues and engine
  APIs exist, so the scaffold cannot accidentally fall through to the older
  synthetic staged-command dispatch path.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:48`
  exposes per-class and total scheduler facts for frontend commands staged,
  backend commands queued/in-flight for publication, backend completions
  ready/in-flight/staged, and frontend responses pending publication.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:116`
  adds the corresponding class-state counters, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:441`
  copies and totals them in `HomerDpuDmaGetSchedulerFacts()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:43`
  reserves internal task-kind names for backend command body, backend command
  ready-sequence publication, and backend completion read work. They are not
  submitted yet.

Validation:

```sh
cd /data/dbcomm/citus-dbcomm
git diff --cached --check
sudo -n -u dbcomm make -C src/backend/distributed all
sudo -n -u dbcomm make -j8 service-bin client-bin
sudo -n -u dbcomm make -B -j8 service-bin client-bin \
  CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA'
sudo -n -u dbcomm make service-dpu-dma-smoke frontend-dma-smoke dpu-bridge-abi-check
```

Observed result:

- `git diff --cached --check` reported no whitespace errors before commit;
- the extension build completed successfully;
- the normal service/client build completed successfully;
- the forced DOCA-enabled service/client build completed successfully, with
  only the known DOCA experimental/deprecated warnings from NVIDIA headers,
  `doca_task_submit_ex()`, and `doca_dma_set_ordered_completions()`;
- `homer_service_dpu_dma_smoke: ok`;
- `homer_frontend_dma_smoke: ok`;
- `homer_dpu_bridge_abi_check: ok`.

This substage is intentionally a scheduler and fact scaffold, not a functional
selected-DPU command path. All new backend-mailbox action facts are still zero
unless a later engine slice populates them. The selected-DPU frontend guard must
remain in place until the host lifecycle shim and backend-mailbox DMA publish
and pull APIs are implemented and validated.

## Stage 8B.4: Descriptor Roles and Role-Aware Setup Import

Implemented in `/data/dbcomm/citus-dbcomm` as commit `2cac3e457`:

- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:75`
  adds `HomerDpuBridgeDescriptorRole` with roles for frontend control slots,
  backend command mailboxes, backend completion mailboxes, and payload byte
  rings.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:163`
  extends `HomerDpuBridgeRingDescriptor` with `descriptorRole` and `reserved0`;
  the descriptor size is now pinned at 120 bytes at
  `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:210`.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:213`
  adds `HomerDpuBridgeKnownDescriptorRole()`, and
  `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:221`
  adds `HomerDpuBridgeDescriptorRoleMatchesShape()`. The shape check rejects
  role/workload/direction/geometry/flag mismatches before a descriptor can
  become a DMA target.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_comch_abi.h:321`
  now applies the role/shape check during setup-message validation.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1177`
  adds `HomerDpuDmaValidateDescriptorForImport()`, which validates descriptor
  identity, role shape, control range, slot range, and role-specific minimum
  slot sizes before the DOCA mmap import is accepted.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1321`
  calls that validator for every descriptor in the setup import path.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2026`
  makes command-pull require the frontend-control role, and the response
  publication path now also requires that role before DMA-writing the frontend
  response slot.
- The grouped-control submit loop skips backend command/completion mailbox
  descriptors for now. This prevents the current host-publish-line parser from
  treating a backend mailbox header as `HomerDpuBridgeHostPublishLine`; the
  backend mailbox-specific parser remains a later Stage 8B slice.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c`
  and the standalone TCP/legacy-COMCH transport smokes now mark their existing
  one-ring descriptor as `FRONTEND_CONTROL_SLOT`.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_dma_smoke.c:165`
  expands the synthetic exported mmap layout, and
  `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_dma_smoke.c:310`
  builds a three-descriptor setup payload: frontend control slot, backend
  command mailbox, and backend completion mailbox. When DOCA lifecycle is
  available, the smoke expects one import to add three rings and two imports to
  add six rings.
- `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_bridge_abi_check.c` now validates
  descriptor role/shape helpers, and
  `/data/dbcomm/citus-dbcomm/src/bin/homer_dpu_comch_abi_check.c` now rejects a
  setup descriptor whose role does not match its shape.

Validation:

```sh
cd /data/dbcomm/citus-dbcomm
git diff --cached --check
sudo -n -u dbcomm make dpu-bridge-abi-check dpu-comch-abi-check \
  service-dpu-dma-smoke frontend-dma-smoke
sudo -n -u dbcomm make -j8 service-bin client-bin
sudo -n -u dbcomm make -C src/backend/distributed all
sudo -n -u dbcomm make -B -j8 service-bin client-bin \
  CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA'
sudo -n -u dbcomm make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'
```

Observed result:

- `homer_dpu_bridge_abi_check: ok`;
- `homer_dpu_comch_abi_check: ok`;
- `homer_service_dpu_dma_smoke: ok`;
- `homer_frontend_dma_smoke: ok`;
- the normal service/client build completed successfully;
- the extension build completed successfully;
- the forced DOCA-enabled service/client build completed successfully, with
  only the known DOCA experimental/deprecated warnings from NVIDIA headers,
  `doca_task_submit_ex()`, and `doca_dma_set_ordered_completions()`;
- a final forced normal service/client rebuild completed successfully so the
  local service binary was not left in the forced DOCA macro build state.

Validation note:

- Running `make -B service-dpu-dma-smoke
  CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_DMA_WITH_DOCA'` is not currently a valid
  target invocation because `service-dpu-dma-smoke` does not add the DOCA include
  and link flags that `service-bin` adds. The DOCA-enabled compile coverage for
  this slice therefore comes from the forced `service-bin client-bin` build, not
  from a DOCA-enabled standalone smoke run.
- A validation-command mistake briefly ran two configure-triggering builds in
  parallel, which left `src/include/citus_version.h` with undefined version
  macros. Running `./config.status --recheck && ./config.status` serially as
  `dbcomm` regenerated the header, and the subsequent serial extension build
  passed. This was a build-system race from the validation command ordering, not
  a source-code defect in the descriptor-role slice.

This substage still does not implement backend command publication or backend
completion pulling. It makes those next engine APIs safe to target by role:
backend-command DMA must use `BACKEND_COMMAND_MAILBOX`, backend-completion DMA
must use `BACKEND_COMPLETION_MAILBOX`, and frontend response publication must
continue using `FRONTEND_CONTROL_SLOT`.

## Next Stage

The next implementation slice should keep the selected-DPU `not implemented`
guard in place and add the first real backend-mailbox queues behind the Stage
8B.3 scheduler scaffold and Stage 8B.4 descriptor roles. The immediate target is
not end-to-end SQL yet; it is to populate maintained facts and implement bounded
engine APIs for backend command publication and backend completion pull so the
new scheduler actions can become productive without changing scheduler taxonomy
or descriptor identity again. Candidate building must continue to read only
maintained facts and must not call DOCA or inspect host memory.

Current Stage 6 setup decisions:

- Keep new DPU implementation code out of
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  where sensible. That file remains the current-scheduler adapter for
  `HomerGrantVector`, `HomerProgressResult`, collector/action enums, and
  `HomerServiceExecuteDpuDmaAction()`.
- Put the host TCP setup client in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c`,
  because it is part of DMA-channel setup.
- Add a service-side `homer_service_dpu_setup_tcp.c/.h` for the DPU TCP setup
  listener and setup-message handling. It should deliver mmap export bytes to
  `HomerDpuDmaImportHostMmapDescriptor()` rather than embedding setup receive
  state inside the DMA engine.
- The DPU Homer service is the TCP listener; the host/frontend DMA channel is
  the TCP client.
- TCP setup is cold path. It may block while waiting for connection/setup ack,
  but must use a timeout and explicit diagnostics. Scheduler ready-set building,
  DMA submit actions, PE-drain actions, and callbacks remain bounded and
  nonblocking.
- The independent service-startup rule is: service startup creates the DPU DMA
  engine and TCP setup listener, then returns to normal service pumping. Host DB
  backends connect later from the DPU frontend setup path, export bridge memory,
  send setup over TCP, and wait for ack with a finite timeout.
- TCP setup uses explicit message framing and short-read/short-write handling.
  It does not require COMCH send-completion ownership, but receive payloads and
  ack bytes must remain owned until the bounded setup exchange completes or
  fails.
- The setup message includes protocol/versioning, message kind, bridge
  generation, feature flags, ring count, descriptor size, mmap export length,
  exported mmap blob, `HomerDpuBridgeControlBlockHeader`,
  `HomerDpuBridgeRingDescriptor[]`, and an ack/error result. The struct names
  still say `Comch` for now, but the active transport is TCP.

After Stage 6B.2, grouped-control discovery can produce maintained DPU-local
ready facts. Close/close-ack and reclaim remain folded into Stage 10 teardown
rather than blocking Stage 7 command-pull work.
