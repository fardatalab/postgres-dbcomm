# DPU DMA Backend Homer Service Implementation Checkpoint

## Current Status

This note tracks landed implementation stages for the DPU-pull Homer migration.
The target design remains
[`dpu_dma_backend_homer_service_current_scheduler_design.md`](../../../future-directions/citus/transport/dpu_dma_backend_homer_service_current_scheduler_design.md).

As of Stage 6A.3, the default frontend and service path is still the existing
host-process SHM/RDMA implementation. The DPU frontend path exists only behind
the explicit hidden GUC `citus.enable_experimental_homer_dpu_frontend`; when the
GUC is enabled, the frontend runs a host-side bridge-memory smoke check and still
fails real Homer API calls with a deliberate not-implemented error. The
frontend now also has a COMCH setup-payload builder, but no DOCA COMCH client
transport yet. The service-side DPU DMA scheduler path is opt-in behind
`HOMER_SERVICE_ENABLE_DPU_DMA=1`; it reads maintained zero-work facts and wires
no-op bounded DPU actions through `machine-baseline`. The engine now has Stage 5
service-side DOCA lifecycle scaffolding behind a compile-time
`HOMER_DPU_DMA_WITH_DOCA` gate: task-slot/owner arrays, local grouped-control
staging buffers, one PE plus per-class DMA contexts, and an imported-host-mmap
descriptor API. Stage 6A.1 adds a shared COMCH setup-message ABI and
service-side setup handler that validates received setup bytes and routes mmap
descriptor import through `HomerDpuDmaImportHostMmapDescriptor()`. It still does
not submit DOCA DMA tasks or drain a DOCA PE. Stage 6A.2 adds a standalone real
DOCA COMCH transport smoke for the setup bytes and validates the farnet1 host to
farnet1 DPU control-channel path. The service and frontend do not yet call that
transport lifecycle directly. Stage 6A.3 replaces first-capable-device DMA
selection with an explicit local DOCA device PCI: the service defaults to the
DPU-local `0000:03:00.0` and can be overridden with
`HOMER_SERVICE_DOCA_DEV_PCI`.

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

## Next Stage

Stage 6A should next integrate the real DOCA COMCH endpoint lifecycle into the
host frontend setup path and DPU service startup path, using the validated
farnet1 COMCH defaults DPU server `dev=0000:03:00.0` and `rep=0000:21:00.0`
unless the deployment is explicitly configured otherwise. The DPU DMA engine now
separately defaults its local DMA device to `0000:03:00.0` and accepts
`HOMER_SERVICE_DOCA_DEV_PCI` as an override. Stage 6B should then implement
grouped-control DMA submit/drain.

Decisions recorded for Stage 6:

- Keep new DPU implementation code out of
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  where sensible. That file remains the current-scheduler adapter for
  `HomerGrantVector`, `HomerProgressResult`, collector/action enums, and
  `HomerServiceExecuteDpuDmaAction()`.
- Put the host COMCH client in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c`,
  because it is part of DMA-channel setup.
- Add a service-side `homer_service_dpu_comch.c/.h` for the DPU COMCH server and
  setup-message handling. It should deliver mmap export bytes to
  `HomerDpuDmaImportHostMmapDescriptor()` rather than embedding COMCH receive
  state inside the DMA engine.
- The DPU Homer service is the COMCH server; the host/frontend DMA channel is
  the COMCH client.
- COMCH setup is cold path. It may block while waiting for connection/setup ack,
  but must use a timeout and explicit diagnostics. Scheduler ready-set building,
  DMA submit actions, PE-drain actions, and callbacks remain bounded and
  nonblocking.
- The setup message includes protocol/versioning, message kind, bridge
  generation, feature flags, ring count, descriptor size, mmap export length,
  exported mmap blob, `HomerDpuBridgeControlBlockHeader`,
  `HomerDpuBridgeRingDescriptor[]`, and an ack/error result. This ABI is now
  implemented and validated by Stage 6A.1.

Remaining Stage 6A work should move the validated standalone COMCH lifecycle
into production setup: service-side server creation during DPU service startup,
host-side client connect from the frontend DMA setup path, blocking cold-path
send/wait with timeout, ack receive, close/close-ack shell, and diagnostics.
Stage 6B should allocate or arm concrete `doca_dma_task_memcpy` tasks after
descriptor import provides remote source buffers and local staging destination
buffers. Control-read DMA tasks should be submitted only under
`DPU_DMA_SUBMIT_CONTROL_READS` grants, completions should be retired only under
`DPU_DMA_DRAIN_PE` grants, and callbacks should validate task-owner identity,
publication epochs, generations, and monotonic frontiers.

The important standalone synthetic host/DPU publisher validation gate moves to
Stage 6. That gate proves the DPU can submit grouped control-line DMA reads under
scheduler grants, retire the read completions only through bounded PE-drain
grants, validate publication epochs/generations/frontiers, and populate
DPU-local ready facts without reading one tail at a time.
