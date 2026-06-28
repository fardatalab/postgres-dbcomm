# DPU DMA Backend Homer Service Implementation Checkpoint

## Current Status

This note tracks landed implementation stages for the DPU-pull Homer migration.
The target design remains
[`dpu_dma_backend_homer_service_current_scheduler_design.md`](../../../future-directions/citus/transport/dpu_dma_backend_homer_service_current_scheduler_design.md).

As of Stage 2, the default frontend and service path is still the existing
host-process SHM/RDMA implementation. The DPU frontend path exists only behind
the explicit hidden GUC `citus.enable_experimental_homer_dpu_frontend`; when the
GUC is enabled, the frontend runs a host-side bridge-memory smoke check and then
fails real Homer API calls with a deliberate not-implemented error.

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
- The one-word publication offset is pinned at offset zero for both hot lines.
  This is a protocol choice for efficient polling and separate one-word DMA
  publication; if a future multi-word sealed snapshot is needed, it should be a
  separate ABI struct rather than mutating these hot lines.

## Next Stage

Stage 3 should add `homer_service_dpu_dma.c/.h` with DPU-side engine lifecycle
state and zero-work scheduler facts. The first version should compile without
DOCA when DPU mode is off and should not add a private progress loop.
