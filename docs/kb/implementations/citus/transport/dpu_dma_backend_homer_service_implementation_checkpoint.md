# DPU DMA Backend Homer Service Implementation Checkpoint

## Current Status

This note tracks landed implementation stages for the DPU-pull Homer migration.
The target design remains
[`dpu_dma_backend_homer_service_current_scheduler_design.md`](../../../future-directions/citus/transport/dpu_dma_backend_homer_service_current_scheduler_design.md).

As of Stage 1, no runtime Homer behavior has changed. The default frontend and
service path is still the existing host-process SHM/RDMA implementation. The
landed code only introduces the host-DPU bridge ABI and a host-only protocol
checker.

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

## Current Caveats

- Stage 1 does not allocate bridge memory, register DOCA mmap objects, connect
  COMCH, or submit DMA tasks.
- The new ABI is not yet included by the existing SHM frontend/service runtime
  paths; Stage 2 will introduce the experimental host-side DMA channel skeleton.
- The one-word publication offset is pinned at offset zero for both hot lines.
  This is a protocol choice for efficient polling and separate one-word DMA
  publication; if a future multi-word sealed snapshot is needed, it should be a
  separate ABI struct rather than mutating these hot lines.

## Next Stage

Stage 2 should add `homer_frontend_dma.c/.h` behind an explicit experimental
channel switch. The SHM path must remain the default, and the experimental mode
should only initialize/tear down aligned bridge memory plus synthetic
publication/credit checks before any DPU service exists.
