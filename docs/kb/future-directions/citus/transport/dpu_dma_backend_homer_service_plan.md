# DPU DMA Backend Homer Service Plan

## Scope

- **What this doc explains**: the implementation-facing plan for replacing the current host-side standalone Homer service boundary with a DPU-resident Homer transfer service. The service uses DOCA DMA for host-DPU command/control, host-DPU staging, and DPU-to-host publication, and can use DPU-controlled RDMA to forward selected records without first copying them into DPU-local memory.
- **What this doc does not cover**: a final committed implementation, a DPA-kernel port, peer-service RDMA redesign, or a new SQL/session semantic model.
- **Header snapshot**: DOCA API headers referenced below are copied under [`dpu_dma_backend_homer_service_plan_doca_headers/`](dpu_dma_backend_homer_service_plan_doca_headers/).
- **Current-scheduler companion**: [`dpu_dma_backend_homer_service_current_scheduler_design.md`](dpu_dma_backend_homer_service_current_scheduler_design.md) is the authoritative execution plan for today's `machine-baseline` scheduler, including stage order, callback retirement, grouped-control ABI details, grant wiring, TCP/mmap setup, and staged implementation constraints. This document is the mechanism/rationale reference for the DPU transfer engine and direct-vs-staged data movement.
- **Doc type**: `future-direction`

## Current Decision

The target host-DPU boundary is DPU pull from host memory, not host push through backend-submitted DMA tasks. After the DPU has accepted a host-published record, forwarding can be either direct DPU-controlled RDMA from PCI-imported host memory or staged DMA into DPU-local memory followed by RDMA.

The PostgreSQL/Citus backend remains a CPU producer into host memory. It writes command records, payload descriptors, and byte-stream chunks into SPSC rings or byte rings, then release-publishes a grouped frontier. The DPU Homer service owns the hardware work: it discovers published host frontiers, posts async DMA reads where the DPU needs local bytes, posts direct RDMA where a host source can be forwarded safely, tracks completed contiguous records, executes or forwards the work, and writes consumed head/credit updates back to host memory.

Sync-event is not required for the hot path. The current empirical decision is to use memory-resident grouped control blocks/frontiers as the authoritative publication mechanism. Sync-event can remain an optional cold/idle wakeup experiment, but the default hot path should work by polling grouped control blocks with DMA and by explicit DMA completion tracking.

COMCH is no longer the planned setup transport. It was only meant to carry cold descriptor/setup bytes, and stock DOCA COMCH currently fails on farnet1 below Homer while standalone DOCA DMA still validates in both directions. Use a regular TCP setup socket for host frontend to DPU service setup; keep hot-path data movement and publication on exported memory controlled by DOCA DMA, DOCA RDMA, or verbs RDMA lanes owned by the DPU service.

The implementation should keep three layers separate:

1. `homer_frontend_dma.c/.h`: host-side frontend channel replacement beside [`homer_frontend_shm.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:1).
2. `homer_dpu_bridge_abi.h`: fixed-width bridge ABI shared by the host frontend and DPU service.
3. `homer_service_dpu_dma.c/.h`: DPU-side transfer engine integrated into the service scheduler near [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36127). The initial file name can remain DMA-focused, but the module should own both DOCA DMA lanes and DPU-controlled RDMA forwarding lanes so scheduling, ownership, and teardown are handled in one place.

## Current Code Anchors

The frontend/service split is the enabling refactor:

- Public backend API callers go through [`homer_frontend.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_frontend.h:448) and the implementation coordinator [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:265).
- The current host-side channel mechanics are isolated in [`homer_frontend_shm.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:1). [`MapRemoteExecutionControlRegion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:45), [`ReserveRemoteExecutionControlSlot()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:145), [`PublishRemoteExecutionControlRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:186), and [`WaitForRemoteExecutionControlResponse()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:216) are the current local IPC primitives to replace or bypass.
- Semantic control request construction lives in [`homer_frontend_control.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:1). [`StartRemoteExecutionCommandThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:1013) and [`PollRemoteExecutionCommandCompletionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:1164) currently use the SHM channel but should not know about DOCA internals.
- The existing SPSC queue ABI is [`CitusTupleSinkQueueControl`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_queue_abi.h:63), with `publishedTail` and `consumedHead`.
- The current frontend completion mailbox is [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_completion_abi.h:425), and the current peer command-completion ring is [`CitusRemoteExecPeerCommandCompletionRing`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_completion_abi.h:471).
- The service scheduler is centered on [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36127) and [`HomerServicePumpMachineBaselineOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36089).
- The scheduler's first-layer source vocabulary is [`HomerProgressSourceKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1936). Its comment explicitly says sources are "which executor can make progress if I spend CPU here?", not SQL operations.
- One pass builds cheap ready candidates in [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12238), turns them into source/action plans through [`HomerServiceBuildProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10968) or policy `buildActionPlan`, then executes action grants in [`HomerServiceExecuteProgressExecutionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:35958).
- Executors report source-neutral work through [`HomerProgressResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3018), including `emptyPolls`, `cqesDrained`, `itemsProcessed`, `wrsPosted`, `bytesPosted`, and frontier fields.
- The current payload scheduler derives egress budgets through [`HomerServicePayloadEgressPassBudgetBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6046) and [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6271). The DPU transfer engine should reuse this shape for work budgeting rather than inventing an unbounded DMA/RDMA loop.
- The current host-service byte-ring sender, [`HomerServicePumpOutgoingByteRingPayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26843), documents the existing invariant: backend producer writes complete transport records into registered shared memory, and the service validates/coalesces/transports those records.

## Workload Classes

Start with four DPU transfer workload classes:

1. **Host-DPU commands and local control**: host-produced command/control records and setup-adjacent command mailboxes. These are latency-sensitive and often have `K=1` per session because the next command may depend on the previous completion. Direction is host memory to DPU service, initiated by DOCA DMA reads.
2. **DPU-to-host completions, responses, and credits**: DPU/service-produced completion, response, and credit records consumed by the host frontend/backend. Direction is DPU DMA write into host memory, followed by a separate publication write while farnet is configured with `PCI_WR_ORDERING=force relaxed`.
3. **Small forwarded records**: transaction command/completion/result records that should move from a host-owned source buffer to a peer/frontend-visible RDMA target. The preferred path is direct DPU-controlled RDMA from PCI-imported host memory when the record is small enough that avoiding the extra DMA copy matters more than peak bandwidth.
4. **Bulk byte-stream chunks**: basebackup and large tuple/result byte-stream records. The preferred path is staged transfer: DOCA DMA pulls from host memory into a DPU-local mirrored ring/sink, then RDMA writes from that DPU-local staging memory.

Each class should have its own transfer-class state first. For DMA work, context-per-class gives independent queue-depth, completion-report, and priority policy. For RDMA forwarding, class state should also include the QP/CQ or lane policy needed to keep command-like small records from being blocked behind bulk transfers. Sharing one PE or CQ-drain source is acceptable initially if the scheduler has explicit progress budgets; split PEs/CQs only if shared progress causes measurable interference.

Do not use one semantic tail across sessions. Multiple sessions can share one transfer context for initiator-side completion-report coalescing, but each session/ring still owns its own published frontier and consumed head. Cross-session coalescing is a completion-report and scheduling optimization, not an application-visible batching rule.

## Direct DPU-Controlled RDMA Forwarding Probe

After the initial DPU-pull design work, we validated one important possible
copy-reduction path for result/payload forwarding. The standalone probe
[`doca_pci_rdma_bridge_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_pci_rdma_bridge_validation.c:1)
tests this composition:

```text
backend-host producer memory
  --doca_mmap_export_pci()-->
DPU-imported PCI mmap / source doca_buf
  --doca_rdma_task_write-->
frontend-visible memory exported with doca_mmap_export_rdma()
```

This is different from ordinary DPU-pull DMA into a DPU-local staging ring. The
DPU still controls the operation, but the RDMA write source buffer is backed by
host memory imported over PCI rather than by DPU-local memory. On July 2, 2026,
the 64 B and 4 KiB farnet1 host-to-DPU probes passed with host PCI device `0000:21:00.0`,
DPU DOCA device `0000:03:00.0`, host RDMA device `mlx5_0` GID index `3`, and
DPU RDMA device `mlx5_2` GID index `1`. The host validated the bytes in the
RDMA-exported target buffer after the DPU completed the RDMA write task.

Implementation details that matter:

- Host source memory is registered/exported with `doca_mmap_export_pci()`.
- Host/frontend target memory is registered/exported with
  `doca_mmap_export_rdma()`.
- The DPU imports both descriptors with `doca_mmap_create_from_export()`.
- The DPU creates the RDMA write source `doca_buf` with
  `doca_buf_inventory_buf_get_by_data()` so the source buffer has an explicit
  data length. An earlier harness attempt used `doca_buf_inventory_buf_get_by_addr()`;
  the DPU write task completed, but the host did not validate the target bytes.
- The target buffer remains an address-only remote buffer created with
  `doca_buf_inventory_buf_get_by_addr()`, matching the DOCA RDMA write requester
  sample shape.

The corrected reusable-task performance comparison favors the direct source path
for small records. The same harness can run `--mode=direct-pci-rdma` and
`--mode=dma-stage-rdma`, where the staged mode first DMA-pulls the host source
into DPU-local memory and then issues the RDMA write. The benchmark uses a fixed
window of task/buffer lanes, repositions reusable `doca_buf` handles with
`doca_buf_inventory_buf_reuse_by_data()` or
`doca_buf_inventory_buf_reuse_by_addr()`, updates task fields, and resubmits
tasks from callbacks after ownership returns to the application.

One important validation correction: the harness must not time the DPU before
the host RDMA endpoint is fully connected, and the wrapper must not poll the DPU
with repeated SSH commands during the timed section. The harness now has a
`--ready=PATH` gate; the host writes it after RDMA connect, and the DPU waits
for it before starting the timed loop. This replaces two invalid intermediate
measurement shapes: a per-operation-task harness that reached only a few
thousand ops/s, and a non-ready-gated harness that falsely reported about
`1 GiB/s` for 1 MiB transfers.

With the same farnet1 devices and the ready gate, direct PCI-source RDMA reached
about `4.47M` ops/s for 64 B records at window 64 and `4.62M` ops/s /
`4514` MiB/s for 1 KiB records at window 128. The staged path reached about
`2.47M` ops/s for 64 B and `2.63M` ops/s / `2570` MiB/s for 1 KiB, lower
because it performs DMA plus RDMA per record. At 1 MiB/window 128, direct
PCI-source RDMA reached about `11821` MiB/s and staged DMA-then-RDMA reached
about `15238` MiB/s. A later direct rerun reached about `14917` MiB/s at
1 MiB/window 128, so the large-transfer ranking should be treated as sensitive
to run conditions until repeated warmed runs are collected.

A separate hybrid harness,
[`doca_dma_ibverbs_rdma_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_dma_ibverbs_rdma_validation.c:1),
now tests the path that first uses DOCA DMA to pull a PCI-imported host source
into DPU-local memory and then uses ordinary ibverbs RC RDMA write from that
DPU-local staging memory. The first implementation intentionally keeps lane
ownership simple: one reusable DOCA DMA task and one reusable verbs WR/SGE per
lane, with the stage slice reused only after the verbs completion for that lane.
The harness originally recovered lanes with `op_index % window`; out-of-order
completions made that invalid and could resubmit a DMA task for a still-busy
lane. The fixed version encodes both lane and operation in the DOCA task user
data and verbs `wr_id`.

Fresh direct-vs-hybrid results confirm that DMA plus ibverbs is not a better
small-record default. At 64 B/window 1, direct PCI-source DOCA RDMA reached
about `397k` ops/s while DMA+ibverbs reached about `261k`; at 64 B/window 16,
direct reached about `4.44M` while DMA+ibverbs reached about `2.27M`. At
1 KiB/window 1, direct reached about `386k` vs `253k`; at 1 KiB/window 16,
direct reached about `4.22M` vs `2.20M`. At high depth the gap narrows:
64 B/window 128 was effectively tied around `4.12M` ops/s, and 1 MiB/window
128 was also effectively tied in the fresh run (`14917` vs `15152` MiB/s).
This preserves the design guidance: direct DPU-controlled RDMA from a
PCI-imported backend/source ring remains the small command/completion candidate;
DPU-local staging remains useful for lifetime, scheduler ownership, wrap
handling, and throughput-oriented payload streams.

Both DOCA harnesses now expose `--optimize-reports`,
`--flush-report-sentinel`, and `--report-interval=N`, using
`doca_task_submit_ex()` with `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS` for
non-sentinel tasks and a non-optimized, optionally flushed sentinel every `N`
tasks and at the final task. The harness rejects `N > window` because the
initial window must contain a report boundary or all completion callbacks can be
deferred. In the selected runs, direct DOCA RDMA did not materially improve with
opt+flush. The hybrid path improved the 1 MiB/window 128 point from about
`15152` to `16745` MiB/s, but hurt 64 B and was mixed for 1 KiB. A follow-up
all-DOCA staged sweep closed the missing comparison: at 1 MiB/window 128,
non-optimized staged DMA-then-DOCA-RDMA reached about `13334` MiB/s, while
opt+flush reached `15628` MiB/s even at `report_interval=2` and up to `15970`
MiB/s at `report_interval=128`. So the large-transfer opt+flush benefit is not
only a very-large-window artifact. Small staged records still stayed far below
direct DOCA RDMA: 64 B/window 128 improved only from about `2.36M` to `2.64M`
ops/s, and 1 KiB/window 128 from about `2.66M` to `2.72M` ops/s. Use opt+flush
as a throughput-batch optimization, not as the low-latency command publication
default.

The Homer rule should be stricter than the harness: if adjacent bytes share the
same lifetime and publication boundary, combine them into one larger DMA/RDMA
task rather than issuing many optimized tasks plus a sentinel. Opt+flush is
interesting when the device work is genuinely multiple tasks before one boundary
because of wraparound, non-contiguous buffers, multiple already-ready ranges, or
multiple sessions/classes being drained by the same ctx. For the dependent
pgbench command/completion path, there is usually no useful report batch; for
basebackup/bulk payloads, use large contiguous range tasks first and apply
opt+flush only around unavoidable multi-task batches.

### DPU-Backed Homer Transport Decisions From These Experiments

Use two data-movement policies by workload class:

1. **Small command/completion records, including pgbench-style command
   completions**: use direct DPU-controlled DOCA RDMA from PCI-imported host
   source memory to the destination memory. This avoids the extra DMA pull and
   is the measured winner for low-depth and moderate-depth small records. Do not
   add an opt+flush batch boundary to dependent per-session command completions
   unless later profiling finds a real multi-task batch in that path.
2. **Large bulk records, currently basebackup payloads and future bulk
   transfers**: use DOCA DMA pulls into DPU-local staging memory followed by
   ibverbs RDMA writes from that DPU-local memory. Apply
   `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS` plus a flushed report sentinel on
   the DOCA DMA side when the DPU genuinely submits multiple DMA tasks before a
   publication boundary. Prefer one large contiguous DMA/RDMA task over many
   small optimized tasks whenever the byte-ring layout, wrap point, and
   publication boundary allow it.

This is now an implementation decision for the first DPU-backed Homer design,
not merely an open experiment. The rationale is empirical and structural:
small records are dominated by per-operation overhead, where direct DOCA RDMA
removes one device operation; large bulk records benefit from DPU-local staging
because ibverbs RDMA from local memory is the strongest observed high-throughput
path and gives the scheduler explicit ownership of staged bytes. All-DOCA
staged opt+flush remains a close fallback for bulk transfer (`~15.6-16.0 GiB/s`
in the 1 MiB/window-128 sweep), but the selected basebackup path should be
DOCA DMA plus ibverbs RDMA (`~16.7 GiB/s` in the selected opt+flush run).

The low-window command/completion regime is different from the high-depth
throughput regime. A ready-gated small-message sweep showed direct PCI-source
RDMA at about `390k` ops/s for 64 B/window 1, `787k` at window 2, `1.38M` at
window 4, `2.50M` at window 8, and roughly `4.4M-4.6M` from windows 16 through
128. For 1 KiB records it was about `374k` ops/s at window 1, `1.25M` at window
4, `3.96M` at window 16, and `4.88M` at window 128. The ibverbs companion was
close at window 1 (`483k` ops/s for 64 B and `444k` for 1 KiB), but kept scaling
at larger windows (`19.7M` ops/s for 64 B/window 128 and `18.0M` ops/s for
1 KiB/window 128). This matters for Homer: a single transaction session with
dependent commands should expect low-depth behavior, while many sessions or
throughput streams can expose the higher-depth gap.

The ibverbs calibration confirms that the host-DPU RDMA path is not inherently
1 GiB/s-limited. A companion `ibverbs_rdma_write_validation` harness using
DPU-local source memory and the same ready-gated fixed-window lane shape reached
about `9.77M` ops/s at 64 B, `14.07M` ops/s / `13736` MiB/s at 1 KiB, and
`18013` MiB/s at 1 MiB with spread buffers; a single-buffer 1 MiB variant
reached `29001` MiB/s. `ib_write_bw` on the same devices reported `242.99`
Gb/s for 1 MiB/TX-depth-128, and `--use_old_post_send` still reported
`240.75` Gb/s.

Design consequence: the Homer payload/result path no longer has to assume a
DPU-local staging copy is mandatory for every backend-host-to-frontend transfer.
For same-DPU-controlled forwarding, direct RDMA write from the PCI-imported
backend/source ring to the frontend-visible RDMA target ring is now the better
candidate for small Homer command/completion/result records, especially at low
per-session pipeline depth where it avoids the DMA-plus-RDMA double operation.
The staging-ring design remains a large-transfer candidate and a fallback for
cases where lifetime, ordering, publication, wrap handling, or scheduler
ownership are simpler with DPU-local bytes.

The current DOCA harness still submits each completed semantic record with an
immediate per-task completion callback and resubmit. That is the right shape for
low-latency command publication, but it likely leaves high-depth throughput on
the table compared with verbs. The next throughput-oriented experiment should
test `doca_task_submit_ex()` with non-flushed submissions plus a flushed
sentinel, and possibly `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS`, for work where
per-record callback latency is not required. Before using either direct or
staged forwarding as the production default, we still need a Homer-shaped
validation that covers byte-ring wrap, per-session lifetime, remote frontend
targets, publication semantics, and throughput/latency under the service
scheduler.

Implementation consequence: the next code should not model the service as a
pure "DMA puller." It should model a transfer lane as one of these shapes:

1. **Direct small-record lane**: source `doca_buf` references PCI-imported host
   memory, target `doca_buf` references the RDMA-visible destination, and the
   DPU posts a DOCA RDMA write. Host source credit cannot be published until the
   RDMA completion says the source bytes are no longer needed by the device.
2. **Staged bulk lane**: source `doca_buf` references PCI-imported host memory,
   destination `doca_buf` references DPU-local staging memory, and the DPU posts
   a DOCA DMA read. Once the DMA completion advances the staged frontier, host
   source credit can be published; the DPU-local staging lane itself cannot be
   reused until the later RDMA write from that staging memory completes.
3. **DPU-to-host publication lane**: source bytes are DPU-local service state,
   destination is host/frontend memory, and the DPU uses DOCA DMA write. With
   current force-relaxed PCI ordering, host-visible publication remains a
   two-phase DMA operation: body write completes first, then a separate ready
   epoch/tail write publishes the body.

## Shared Bridge ABI

Create `src/include/distributed/homer/homer_dpu_bridge_abi.h` for fixed-width shared layout. It should avoid PostgreSQL types, DOCA pointer types, service-private table pointers, and variable-length C structs.

Initial ABI objects should include:

- `HomerDpuBridgeHeader`: protocol version, total bytes, feature flags, number of grouped control entries, and generation.
- `HomerDpuRingDescriptor`: ring id, workload class, direction, host virtual address or offset, ring bytes, record/slot geometry, and flags.
- `HomerDpuControlEntry`: cache-line-sized grouped frontier entry with `publishedTail`, `consumedHead`, `flags`, generation, and optional ready bits for a session/ring.
- `HomerDpuControlBlock`: array of grouped control entries laid out so the DPU can DMA-read several ring frontiers in one cacheline-sized or page-sized transfer.
- `HomerDpuCompletionEntry`: DPU-produced completion/result frontier metadata for host consumption.
- `HomerDpuDescriptorTable`: setup-time descriptors that connect service session id, sink id, workload class, ring geometry, mmap export id, and optional sync-event id.

The grouped control block is the key hot-path layout. The DPU should not poll one 8-byte tail per session with separate tiny DMA reads. It should DMA-read one grouped control block, inspect many frontiers locally on the DPU, and then post payload DMA reads only for rings with new published work.

Keep producer-owned and consumer-owned cachelines separate:

- host-owned publication cacheline: host writes `publishedTail` and flags
- DPU-owned credit cacheline: DPU writes `consumedHead` and error/terminal bits
- optional diagnostics cacheline: counters not required for correctness

## Host Frontend Channel

`homer_frontend_dma.c/.h` should be the host-side replacement for `homer_frontend_shm.c`. It should implement the frontend channel mechanics while keeping semantic request construction in `homer_frontend_control.c`.

Responsibilities:

- allocate or attach long-lived host memory regions for control blocks, command rings, completion rings, and byte-stream rings
- register those regions with DOCA mmap
- export mmap descriptors for DPU PCI access
- exchange descriptors and ring metadata with the DPU service over TCP during setup
- write backend-produced command/payload records into host rings
- release-publish grouped frontier entries after records are complete
- read DPU-written consumed heads/completion frontiers with acquire semantics
- keep `OpenRemoteExecutionSession()`, `StartRemoteExecutionCommand()`, and `PollRemoteExecutionCommandCompletion()` stable for ordinary callers

The host frontend should not submit hot-path DMA tasks for backend-produced records. That would move hardware submission cost into PostgreSQL/Citus backends and would undo the scheduler/offload goal.

Memory registration should be long-lived. A first prototype can register per service/session/stream lifetime; do not register or export per command, per completion, or per chunk.

## DPU Service Transfer Engine

`homer_service_dpu_dma.c/.h` should own DPU-side DOCA DMA objects, DPU-controlled RDMA lane objects, imported host mmap descriptors, and DPU-local staging buffers. The file name can stay DMA-focused during migration, but the module boundary should be a transfer engine rather than a pure DMA helper.

Expected service API shape:

```c
typedef struct HomerDpuTransferEngine HomerDpuTransferEngine;

bool HomerDpuTransferEngineCreate(HomerDpuTransferEngine **engine,
                                  const HomerDpuTransferConfig *config);
void HomerDpuTransferEngineDestroy(HomerDpuTransferEngine *engine);
bool HomerDpuTransferRegisterDescriptor(HomerDpuTransferEngine *engine,
                                        const HomerDpuRingDescriptor *descriptor);
bool HomerDpuTransferPollGroupedControl(HomerDpuTransferEngine *engine,
                                        const HomerProgressGrant *grant,
                                        HomerProgressResult *result);
bool HomerDpuTransferDrainDocaPe(HomerDpuTransferEngine *engine,
                                 const HomerProgressGrant *grant,
                                 HomerProgressResult *result);
bool HomerDpuTransferDrainRdmaCq(HomerDpuTransferEngine *engine,
                                 const HomerProgressGrant *grant,
                                 HomerProgressResult *result);
bool HomerDpuTransferSubmitDirectRdmaForwards(HomerDpuTransferEngine *engine,
                                              const HomerTransportGrant *transportGrant,
                                              HomerProgressResult *result);
bool HomerDpuTransferSubmitStagedDmaPulls(HomerDpuTransferEngine *engine,
                                          const HomerTransportGrant *transportGrant,
                                          HomerProgressResult *result);
bool HomerDpuTransferSubmitStagedRdmaWrites(HomerDpuTransferEngine *engine,
                                            const HomerTransportGrant *transportGrant,
                                            HomerProgressResult *result);
bool HomerDpuTransferPushHostDma(HomerDpuTransferEngine *engine,
                                 const HomerProgressGrant *grant,
                                 HomerProgressResult *result);
```

Internal state:

- one DMA context per DMA-facing workload class initially: commands/control,
  DPU-to-host publication, grouped-control polling, and staged bulk pulls
- one PE initially, with explicit per-class drain budgets
- preallocated RDMA lane state per forwarding class, including QP/CQ ownership or
  references, WR ids, destination metadata, and source/staging lifetime state
- preallocated `doca_dma_task_memcpy` pools per DMA ctx and per direction
- preallocated DOCA RDMA task pools for direct small-record forwarding when
  using the all-DOCA RDMA path
- preallocated `doca_buf` objects or reusable buffer inventory entries for
  grouped control blocks, payload windows, DPU-local staging, direct source
  buffers, RDMA target buffers, and completion writes
- per-ring state: host frontier last observed, submitted tail, completed
  contiguous tail, DPU-consumed head, host-visible consumed head last published
- per-staging-lane state: source ring identity, source range, staging range,
  DMA-completed flag/frontier, RDMA-completed flag/frontier, generation, op id
- per-class policy: async window, sentinel interval, flush/report flags, max bytes per pass, max completions per pass

Progress loop shape:

```text
DMA-read grouped control block for one workload class or all classes
inspect frontiers on DPU
for each ring with publishedTail > submittedTail:
  reserve task slots up to class window and service grant
  enqueue direct RDMA refs for small records or staged DMA refs for bulk records
submit direct RDMA writes for small ready records under a transfer grant
submit DMA reads into DPU-local staging for bulk ready records under a transfer grant
drain PE and RDMA CQ with class budgets, stopping on budget exhaustion or first no-progress return
advance per-ring and per-staging completed contiguous frontiers from callbacks/CQEs
publish consumedHead/credits to host only when the relevant source frontier is safe
submit DPU DMA writes for completions/results when service produces them
leave remaining completions for the next scheduler pass if the budget expires
```

Do not call synchronous helper loops on every DMA or RDMA operation in the hot path. The clean design is async submission, explicit PE/CQ progress, and contiguous-frontier retirement.

## Scheduler Integration Plan

The DPU transfer engine must be scheduled like existing RDMA WR posting, CQ
polling, mailbox draining, local-control advancement, and payload-stream work.
It should not run a private busy loop that polls grouped control blocks, drains
the DOCA PE, or drains RDMA CQs outside
[`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36127).

The right integration is to expose DPU transfer work as first-layer scheduler
sources and second-layer action grants:

```text
ready-set builder:
  cheap checks: any in-flight DOCA completions? any RDMA CQEs likely pending? any control-poll due? any ready transfer queue nonempty? any credit publish pending?
policy:
  rank DPU transfer sources against existing RDMA CQ drain, local control, command rings, completion rings, payload streams, heartbeat
action executor:
  run one bounded DPU transfer action: submit poll, drain PE, drain RDMA CQ, submit DMA reads, submit direct RDMA writes, submit staged RDMA writes, publish credits
result:
  report empty polls, completions drained, DMA tasks/RDMA WRs submitted, bytes moved, frontiers advanced, stillReady/budgetExhausted
```

Add new source kinds rather than hiding all DPU work under the existing
`HOMER_PROGRESS_SOURCE_PAYLOAD_STREAM` source. The exact enum names can be
shortened during implementation, but the scheduler should preserve these
separate roles:

- `HOMER_PROGRESS_SOURCE_DPU_TRANSFER_CONTROL_POLL`: DMA-read grouped control blocks and discover host-published frontiers.
- `HOMER_PROGRESS_SOURCE_DPU_TRANSFER_DMA_PE_DRAIN`: drain DOCA PE completions across classes when in-flight DOCA tasks exist.
- `HOMER_PROGRESS_SOURCE_DPU_TRANSFER_RDMA_CQ_DRAIN`: drain RDMA send/write completions for direct and staged forwarding lanes.
- `HOMER_PROGRESS_SOURCE_DPU_TRANSFER_COMMAND_PULL`: submit/retire DMA reads for host-produced command/control records.
- `HOMER_PROGRESS_SOURCE_DPU_TRANSFER_DIRECT_RDMA_FORWARD`: submit direct DPU-controlled RDMA writes from PCI-imported host source buffers.
- `HOMER_PROGRESS_SOURCE_DPU_TRANSFER_STAGED_DMA_PULL`: submit/retire DMA reads from host byte rings into DPU-local mirrored staging rings.
- `HOMER_PROGRESS_SOURCE_DPU_TRANSFER_STAGED_RDMA_FORWARD`: submit RDMA writes from DPU-local staging memory after staged DMA completion.
- `HOMER_PROGRESS_SOURCE_DPU_TRANSFER_HOST_DMA_PUSH`: submit/retire DMA writes for DPU-produced host completions, responses, and credits.
- Optional later source: `HOMER_PROGRESS_SOURCE_DPU_TRANSFER_IDLE_WAKEUP` if sync-event or an OS wait primitive is added for idle mode.

These source kinds should map to the existing CPU-class vocabulary:

- control poll, command pull, and small direct RDMA forwarding: `HOMER_PROGRESS_CPU_CLASS_HOT_CONTROL` or a new latency-sensitive class if the current enum becomes too coarse
- staged DMA pull, staged RDMA forwarding, and host completion/result push: `HOMER_PROGRESS_CPU_CLASS_HOT_DATA`
- TCP setup/import/export: `HOMER_PROGRESS_CPU_CLASS_COLD_SETUP_MAINTENANCE`
- teardown/failure cleanup: `HOMER_PROGRESS_CPU_CLASS_CLOSE_LIFETIME`

The ready-set phase must stay cheap. [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12238)
should not DMA-read host memory or drain the PE while deciding readiness. It
should only inspect DPU-local counters maintained by the transfer engine:

- `inflightDocaTaskCount[class] > 0` means PE drain is ready
- `rdmaSendCqOutstanding[class] > 0` means RDMA CQ drain is ready
- `controlPollDue[class]` or `controlPollInFlight == false` means grouped-control poll can be scheduled
- `directForwardReadyDepth[class] > 0` means direct RDMA forwarding can submit work
- `stagedDmaReadyDepth[class] > 0` means host-to-DPU staging DMA can submit work
- `stagedRdmaReadyDepth[class] > 0` means RDMA from DPU-local staging can submit work
- `hostDmaPublishQueueDepth > 0` means completion/response/credit DMA push can submit work
- `consumedHeadPublishPending > 0` means credit publication can run
- `classBlockedOnTaskPool` or `classBlockedOnHostCredit` should appear as blocked reason hints, not as a reason to spin inside the executor

The counters are not enough for efficient execution. They tell the scheduler
that a class has work, but they do not tell the action executor which imported
ring, mailbox, forwarding lane, or staging slice should be serviced. The
transfer engine should therefore maintain
fixed-size DPU-local ready queues alongside the counters. This is an internal
scheduler fact cache, not a host-DPU ABI:

- host-to-DPU queues are filled when grouped-control or mailbox-control DMA reads
  accept a newly advanced host-published frontier
- DPU-to-host queues are filled when DPU service workflow state produces a
  command, response, completion, or credit update that must be DMA-written to the
  host
- direct RDMA queues are filled when an accepted host frontier exposes a small
  record whose source bytes can be forwarded directly from PCI-imported memory
- staged queues are filled in two phases: host source ranges ready for DMA pull,
  then DPU-local staging slices ready for RDMA write after DMA completion
- each queue entry should identify the DMA object by at least `{importIndex,
  ringIndex}`; `ringIndex` alone is not globally unique because it is scoped to
  one imported descriptor table
- the entry may also carry a small generation or accepted epoch for stale-entry
  rejection, but the executor must still revalidate the live ring runtime before
  submitting DMA
- per-ring/runtime flags such as `discoveredReady` suppress duplicate queue
  entries; per-class counters remain the cheap ready-set predicate
- stale queue entries caused by bounded grants, teardown, or already-consumed
  frontiers are cleared as normal scheduler state unless they reveal a generation
  or descriptor identity violation

Use separate ready queues by workload and direction rather than one mixed global
queue. Command/control latency, backend completion latency, and payload
throughput need different scheduler policy, queue-depth, and grant treatment:

```text
host_to_dpu_command_pull_ready           grouped-control discovered command/control records
host_small_direct_rdma_ready             small host records ready for direct PCI-source RDMA
host_bulk_stage_dma_ready                byte-ring/basebackup ranges ready for DMA into DPU-local staging
dpu_staged_rdma_ready                    completed DPU-local staging slices ready for RDMA write
dpu_to_host_dma_publish_ready            frontend/backend response, completion, and credit writes
source_credit_publish_ready              source-ring consumed-head or credit-line writes
```

This is the same broad scheduler pattern Homer already needs on the RDMA side:
network CQEs, remote doorbells, and RDMA send/write completions make local
service work newly available, but ready-set construction should still consume
cheap local facts rather than poll hardware or scan all sessions. The DPU DMA
queues should therefore mirror the RDMA direction: hardware/protocol discovery
updates local ready facts, then bounded scheduler grants consume the exact ready
objects. The difference is mechanism, not scheduler ownership: RDMA gets facts
from CQ/doorbell drains, while DPU DMA gets facts from grouped-control reads,
DOCA callbacks, RDMA CQ drains, and DPU-local workflow queues. This plan does
not require changing the existing RDMA-side scheduler now; it keeps the current
DPU migration compatible with a later typed-wait/continuation scheduler by
making every discovered transfer event an explicit local ready fact.

The action executor is where real work happens. Extend
[`HomerServiceExecuteProgressActionGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:35511)
or the source-plan dispatch path with DPU transfer actions such as:

- `HOMER_PROGRESS_ACTION_DPU_TRANSFER_POLL_GROUPED_CONTROL`
- `HOMER_PROGRESS_ACTION_DPU_TRANSFER_DRAIN_DOCA_PE`
- `HOMER_PROGRESS_ACTION_DPU_TRANSFER_DRAIN_RDMA_CQ`
- `HOMER_PROGRESS_ACTION_DPU_TRANSFER_PULL_COMMANDS`
- `HOMER_PROGRESS_ACTION_DPU_TRANSFER_DIRECT_RDMA_FORWARD`
- `HOMER_PROGRESS_ACTION_DPU_TRANSFER_STAGE_DMA_PULL`
- `HOMER_PROGRESS_ACTION_DPU_TRANSFER_STAGE_RDMA_WRITE`
- `HOMER_PROGRESS_ACTION_DPU_TRANSFER_PUSH_HOST_DMA`
- `HOMER_PROGRESS_ACTION_DPU_TRANSFER_PUBLISH_SOURCE_CREDITS`
- `HOMER_PROGRESS_ACTION_DPU_TRANSFER_ADVANCE_SETUP`
- `HOMER_PROGRESS_ACTION_DPU_TRANSFER_ADVANCE_TEARDOWN`

Each action must be bounded by the grant:

- `maxPolls`: max `doca_pe_progress()` calls, RDMA CQ poll calls, or grouped-control poll completions
- `maxItems`: max rings/records to inspect or retire
- `maxPumpCalls`: max inner engine operations
- transport grant / class policy: max DMA tasks, RDMA WRs, and bytes posted

The DPU transfer engine should expose action-specific entry points instead of one
generic "make progress" function. That keeps ownership clear and makes
scheduler accounting meaningful:

```c
bool HomerDpuTransferPollGroupedControl(HomerDpuTransferEngine *engine,
                                        const HomerProgressGrant *grant,
                                        HomerProgressResult *result);
bool HomerDpuTransferDrainDocaPe(HomerDpuTransferEngine *engine,
                                 const HomerProgressGrant *grant,
                                 HomerProgressResult *result);
bool HomerDpuTransferDrainRdmaCq(HomerDpuTransferEngine *engine,
                                 const HomerProgressGrant *grant,
                                 HomerProgressResult *result);
bool HomerDpuTransferSubmitCommandPulls(HomerDpuTransferEngine *engine,
                                        const HomerProgressGrant *grant,
                                        HomerProgressResult *result);
bool HomerDpuTransferSubmitDirectRdmaForwards(HomerDpuTransferEngine *engine,
                                              const HomerTransportGrant *transportGrant,
                                              const HomerProgressGrant *grant,
                                              HomerProgressResult *result);
bool HomerDpuTransferSubmitStagedDmaPulls(HomerDpuTransferEngine *engine,
                                          const HomerTransportGrant *transportGrant,
                                          const HomerProgressGrant *grant,
                                          HomerProgressResult *result);
bool HomerDpuTransferSubmitStagedRdmaWrites(HomerDpuTransferEngine *engine,
                                            const HomerTransportGrant *transportGrant,
                                            const HomerProgressGrant *grant,
                                            HomerProgressResult *result);
bool HomerDpuTransferPushHostDma(HomerDpuTransferEngine *engine,
                                 const HomerProgressGrant *grant,
                                 HomerProgressResult *result);
bool HomerDpuTransferPublishSourceCredits(HomerDpuTransferEngine *engine,
                                          const HomerProgressGrant *grant,
                                          HomerProgressResult *result);
```

Use `HomerProgressResult` fields consistently:

- `emptyPolls`: grouped-control poll found no new frontier, PE drain made no progress, or RDMA CQ drain found no CQEs
- `cqesDrained`: DOCA task completions/callbacks retired by PE progress plus RDMA CQEs drained by CQ actions
- `itemsProcessed`: rings inspected, records retired, forwarding entries accepted, or completion entries published
- `wrsPosted`: DMA tasks and RDMA WRs submitted
- `bytesPosted`: payload bytes scheduled for DMA read/write or RDMA forwarding
- `transportCompletionFrontier`: highest contiguous DMA-completed or RDMA-completed record/byte frontier for the reported class
- `sourceReleaseFrontier`: highest source consumed head/credit that became safe to publish back to host
- `semanticObjectFrontier`: highest command/completion object made semantically visible
- `stillReady`: there is more known DPU-local work after this bounded grant
- `budgetExhausted`: the action stopped because the scheduler grant, task pool, or class window was exhausted
- `blockedOnCredit` / `blockedOnLocalResources`: host ring credit or DPU task-pool/window pressure blocked submission

The ordering between transfer actions matters. A useful default plan order is:

1. drain DOCA PE and RDMA CQ first if local completion facts say completions are likely, so task pools, RDMA lanes, and contiguous frontiers are refreshed
2. publish already-safe source credits and destination frontiers whose dependent data movement has completed
3. poll grouped control blocks to discover new host-published work
4. submit command pulls, with small bounded windows and latency-sensitive priority
5. submit direct RDMA forwards for small records that should avoid DPU-local staging
6. submit staged DMA pulls for bulk byte-stream ranges with remaining data-plane budget
7. submit staged RDMA writes for completed DPU-local staging slices
8. submit DPU-to-host DMA pushes for frontend/backend responses, completions, and credits
9. drain PE or RDMA CQ again only if the policy grants another bounded drain action

This is a policy default, not a hard correctness rule. Correctness still comes
from per-ring frontiers, DOCA completion gating, and RDMA completion gating. The
scheduler can later reorder actions to favor latency or throughput as long as
publication and source-credit release never run ahead of the data movement that
makes them safe.

DOCA PE drain and RDMA CQ drain are both nonblocking. A drain action may call
`doca_pe_progress()` or `ibv_poll_cq()` repeatedly while the grant budget
remains and calls are returning progress, but it must return to the scheduler as
soon as progress stops. It must not spin waiting for a particular task,
sentinel, CQE, or frontier to complete. If in-flight DMA or RDMA remains after a
zero-progress return, the engine records that state locally; the ready-set
builder can mark the appropriate drain source ready again in a later service
pass. Publication actions that depend on a completed frontier must simply
remain not-ready until drain actions have advanced that frontier.

This means "drain until empty" is a bounded service-pass operation:

```text
for polls < grant.maxPolls:
  progressed = doca_pe_progress(pe)
  if progressed == 0:
    result.emptyPolls++
    break
  retire callbacks/frontiers
```

It does not mean "spin until the in-flight queue becomes empty." The outer
service loop may be busy-polling, but each scheduler grant must stay bounded so
RDMA CQ drain, local control, command/completion publication, and payload work
can still interleave.

For the first implementation, keep DPU transfer sources coarse by workload class, not
one source per session. The grouped control block already lets the DPU inspect
many sessions after one scheduled control-poll action. Per-session source refs
would recreate the scheduler overhead we are trying to avoid. Once correctness
is stable, add exact per-session sources only if latency measurements show that
one busy class-level source hides individual-session fairness problems.

## DOCA API Plan

Use the copied headers in [`dpu_dma_backend_homer_service_plan_doca_headers/`](dpu_dma_backend_homer_service_plan_doca_headers/) for exact prototypes.

### Device And Context Setup

- Open/select DOCA devices with `doca_dev` APIs from [`doca_dev.h`](dpu_dma_backend_homer_service_plan_doca_headers/doca_dev.h). The exact device-open helper can follow existing DOCA sample style, but the service should hide device selection behind a config object.
- Create a DMA context with [`doca_dma_create()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_dma.h:106).
- Convert it to a generic context with [`doca_dma_as_ctx()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_dma.h:147).
- Attach the context to a PE with [`doca_pe_connect_ctx()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_pe.h:593).
- Start the context with [`doca_ctx_start()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_ctx.h:156).
- Enable ordered completions for contexts where sentinel completion is used as the completed-frontier signal via [`doca_dma_set_ordered_completions()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_dma.h:340).

### Memory Registration And Export

Host side:

- create mmap with [`doca_mmap_create()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_mmap.h:102)
- add the DPU-visible device with [`doca_mmap_add_dev()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_mmap.h:199)
- set permissions with [`doca_mmap_set_permissions()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_mmap.h:669)
- set the host memory range with [`doca_mmap_set_memrange()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_mmap.h:427)
- start the mmap with [`doca_mmap_start()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_mmap.h:152)
- export for same-PCI DPU access with [`doca_mmap_export_pci()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_mmap.h:277)

DPU side:

- import the host mmap descriptor with [`doca_mmap_create_from_export()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_mmap.h:394)
- create or use a buffer inventory for registered regions
- get `doca_buf` objects by address/range using buffer APIs from [`doca_buf.h`](dpu_dma_backend_homer_service_plan_doca_headers/doca_buf.h)
- set reusable buffer data and lengths with [`doca_buf_set_data()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_buf.h:221) or [`doca_buf_set_data_len()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_buf.h:255)

Permissions:

- backend-to-DPU producer rings need DPU PCI read access, so start with `DOCA_ACCESS_FLAG_PCI_READ_ONLY` from [`doca_types.h`](dpu_dma_backend_homer_service_plan_doca_headers/doca_types.h)
- DPU-produced completion/result rings and consumed-head/credit cachelines need DPU PCI write access, so use `DOCA_ACCESS_FLAG_PCI_READ_WRITE`
- do not depend on `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING`; the current PCI-export harness rejected useful combinations with PCI read/write permissions, and the design must remain correct under global `PCI_WR_ORDERING=force_relax`

### DMA Task Setup And Reuse

- configure DMA task callbacks and task count with [`doca_dma_task_memcpy_set_conf()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_dma.h:222)
- allocate tasks with [`doca_dma_task_memcpy_alloc_init()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_dma.h:247)
- retarget reusable tasks with [`doca_dma_task_memcpy_set_src()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_dma.h:273) and [`doca_dma_task_memcpy_set_dst()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_dma.h:295)
- submit tasks asynchronously with [`doca_task_submit()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_pe.h:330)
- use [`doca_task_submit_ex()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_pe.h:348) for `FLUSH` and `OPTIMIZE_REPORTS`
- treat [`doca_task_batch_submit()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_pe.h:367) as a later optimization after the normal task path is correct and measured

### PE Progress

- create a PE with [`doca_pe_create()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_pe.h:164)
- progress it with [`doca_pe_progress()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_pe.h:197)
- drain explicitly when needed; the header comment at [`doca_pe.h:283`](dpu_dma_backend_homer_service_plan_doca_headers/doca_pe.h:283) describes calling `doca_pe_progress()` until it returns `0`, but in Homer this loop must also obey the scheduler grant budget and must not wait for a specific completion after a zero-progress return
- never call `doca_pe_progress()` from inside a task completion callback; the callback comment in [`doca_pe.h`](dpu_dma_backend_homer_service_plan_doca_headers/doca_pe.h:121) warns against nested progress

### TCP Setup Channel

Use a regular TCP socket for cold setup, not hot payload movement:

- DPU service side creates a normal TCP listener on the DPU fast-link control/data interface, initially `enp3s0f0s0`.
- host frontend side connects to that listener during DPU channel setup.
- exchange the same setup payload bytes the COMCH prototype used: mmap export blob, bridge header, ring descriptors, generation, feature bits, and ack/error status.

TCP setup messages should carry:

- protocol version and feature flags
- one descriptor table for the whole backend/session setup
- one mmap-export entry table with one entry per exported host mapping
- mmap export blobs referenced by that entry table
- grouped control block descriptor
- optional sync-event export if idle wakeup mode is enabled later
- service/session/sink ids

The setup-payload ABI now supports one descriptor table plus a mmap-export entry
table and multiple mmap export blobs in one setup message. This is the desired
production shape: a single TCP setup roundtrip with multiple exported mappings,
not several TCP setup messages for one backend spawn. The backend lifecycle
naturally has separate mappings: the frontend bridge control slot, the backend
command mailbox, the backend completion mailbox, and later payload byte rings.
The backend command/completion mailboxes must remain POSIX SHM objects because
the spawned PostgreSQL backend derives their names from `serviceSessionId` and
opens them locally.

Use the existing descriptor-table idea, but add a setup-time mmap-export table.
Each export table entry should include:

- `mmapExportId`, matching `HomerDpuBridgeRingDescriptor.mmapExportId`
- `descriptorFirst` and `descriptorCount`, defining which descriptors belong to
  that imported mapping
- `mmapExportOffset` and `mmapExportBytes`, pointing into the variable-length
  setup payload

The DPU service validates and imports the setup as one all-or-nothing operation.
If any export import, descriptor range check, role/shape check, or
session/generation identity check fails, the DPU rejects the setup ack and
releases any imports already created for that message. This keeps selected-DPU
backend spawn latency to one cold-path roundtrip while avoiding partial
multi-message session state.

The copied DOCA COMCH headers remain in the header snapshot as historical/reference material for the abandoned setup path. They are not on the immediate implementation path.

### Host Lifecycle Shim For PostgreSQL-Local Work

The DPU service cannot directly perform host-local PostgreSQL lifecycle work. It
cannot `shm_open()` host `/dev/shm` objects, map socketless backend mailboxes,
or signal the host postmaster. That does not mean the old host Homer service
stays on the hot path. It means selected-DPU mode needs a small host lifecycle
shim, owned by the host frontend DMA channel, to establish the resources that
DMA will later use.

The host lifecycle shim should live in or beside `homer_frontend_dma.c/.h`.
Split it into a separate private source file if that keeps the frontend channel
readable, but keep it conceptually under the host DMA frontend, not under the
DPU service engine.

The first implementation keeps this as a separate host-frontend module,
`homer_frontend_dma_lifecycle.c/.h`, so new selected-DPU lifecycle code does not
grow the already large service source and does not mix with DOCA setup helpers
until the wiring is ready.

Responsibilities:

- create host-local SHM/mailbox objects that PostgreSQL backends must open by
  name, including command and completion mailboxes;
- allocate/register/export the host memory regions that the DPU will DMA;
- connect to the DPU TCP setup listener and exchange mmap export descriptors,
  ring descriptors, feature bits, setup generation, and ack/error status;
- obtain or confirm DPU-owned Homer session identity such as service session id,
  generation, and bridge generation;
- submit the backend spawn request to the host postmaster after the mailboxes
  and DPU-visible descriptors are established;
- report bounded setup, spawn, and teardown errors before any selected-DPU
  frontend path would otherwise be tempted to fall back to the old SHM channel.

Current host-local code that motivates this split:

- `TupleSinkServiceEnsureSessionMailboxes()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18105`
  creates and maps the backend command/completion mailboxes today.
- `TupleSinkServiceSubmitBackendSpawnRequest()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18260`
  opens the postmaster-owned spawn region, fills
  `CitusRemoteExecBackendSpawnRequest`, signals postmaster, and waits for the
  response.
- `CitusRemoteExecInitializeBackendSpawnRegion()` and
  `ProcessSpawnRequestSlot()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1638`
  and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2792`
  are host/PostgreSQL-local operations.
- `ExecuteRemoteExecBackendCommand()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2456`
  later maps the command/completion mailboxes by name and runs the socketless
  backend command loop.

Mailbox naming decision:

- selected-DPU lifecycle mailboxes use the backend-visible untagged naming rule
  from `RemoteExecFormatMailboxNames()`, not the service-only tagged variant in
  `TupleSinkServiceFormatSessionMailboxNames()`. A spawned PostgreSQL backend
  derives the names from `serviceSessionId` alone, so a tag would make the
  backend fail to attach.
- mailbox creation uses exclusive create semantics. A stale or colliding
  `serviceSessionId` is a setup error that should be inspected, not silently
  overwritten.
- teardown unlinks only mailbox objects this lifecycle owner actually created.
  Partial setup rollback must not delete an older live or stale object that
  caused an `EEXIST` failure.

Setup-export decision:

- the selected-DPU setup payload should export the frontend bridge, backend
  command mailbox, and backend completion mailbox in the same multi-export TCP
  setup message;
- the frontend bridge descriptor remains
  `HOMER_DPU_BRIDGE_DESCRIPTOR_ROLE_FRONTEND_CONTROL_SLOT`;
- the command mailbox descriptor uses
  `HOMER_DPU_BRIDGE_DESCRIPTOR_ROLE_BACKEND_COMMAND_MAILBOX`, workload
  `COMMAND`, direction `DPU_TO_HOST`, fixed-slot geometry, and the command
  mailbox control/slot offsets;
- the completion mailbox descriptor uses
  `HOMER_DPU_BRIDGE_DESCRIPTOR_ROLE_BACKEND_COMPLETION_MAILBOX`, workload
  `COMPLETION`, direction `HOST_TO_DPU`, fixed-slot geometry, and the completion
  mailbox control/slot offsets.

Backend-spawn decision:

- backend spawn remains a host-local postmaster operation. The TCP setup socket is
  only for host frontend <-> DPU service descriptor exchange; it does not replace
  the host postmaster spawn shared-memory protocol.
- selected-DPU backend spawn should be driven from the real command-session open
  path, not from the current setup-smoke guard. The real open path has the
  `RemoteExecutionSessionIntentSpec` needed for `sessionOpKind`, `databaseId`,
  and `effectiveUserId`.
- after DPU setup/import succeeds, the host frontend lifecycle shim submits a
  `CitusRemoteExecBackendSpawnRequest` to the host postmaster spawn region,
  using the DPU session id/generation and backend-visible mailbox names already
  created by the shim.
- for selected-DPU mode, do not use the old host-service completion-ready bitmap
  identity: set `completionReadyBitmapEnabled = 0` and
  `serviceSessionIndex = CITUS_REMOTE_EXEC_CONTROL_INVALID_SESSION_INDEX`. The
  DPU owns completion discovery by DMA, so there is no host service session table
  to index.
- selected-DPU backend startup needs an explicit backend channel mode in
  `CitusRemoteExecBackendSpawnRequest` and `CitusRemoteExecBackendStartupData`
  in
  `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h`.
  The selected-DPU mode should not pass DOCA descriptors into the backend: the
  backend is still a host PostgreSQL process and opens the lifecycle-created
  POSIX command/completion mailboxes by the existing service-session-id naming
  rule. The descriptors remain for the DPU service import path.
- in selected-DPU backend mode, `ExecuteRemoteExecBackendCommand()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c`
  must skip `RemoteExecMapControlRegion()` and leave
  `RemoteExecBackendSessionState.controlRegion == NULL`. Completion publication
  still writes the backend completion mailbox; `RemoteExecBackendMarkCompletionReady()`
  becomes a no-op because `completionReadyBitmapEnabled == 0`, and the DPU
  discovers completion epochs by DMA-reading the exported completion mailbox.
- the backend normal-exit cleanup must also treat the control region as optional.
  The error path already guards `controlRegion` and `controlFileDescriptor`; the
  normal path should do the same so selected-DPU backends can exit without
  unmapping a NULL control region or closing `-1`.
- use a bounded wait for the postmaster spawn response. Do not invent a cancel
  protocol in this stage. If the wait times out, treat selected-DPU session setup
  as failed/fatal for that open attempt, tear down the DPU setup/mailboxes, and
  rely on operator/runtime cleanup if a late backend appears.
- selected-DPU backend startup mode is necessary but not sufficient for SQL
  command acceptance. Validation showed the backend can correctly skip the old
  control-region attach and still later reach the same old control-region open
  through SQL result-sink allocation. In selected-DPU mode,
  `RemoteExecEnsureSessionResultQueue()` must not call
  `RemoteExecOpenServiceOwnedResultSink()`, because that helper allocates result
  queues by sending an old host-service control request.
- result/payload queues for selected-DPU SQL should follow the same lifecycle
  rule as command/completion mailboxes: host-local allocation and naming happen
  in the lifecycle shim, descriptors are exported in the setup message, the DPU
  imports them, and completions publish descriptor/frontier state through the DPU
  completion path. Do not add a hidden host-service result-sink fallback for
  selected-DPU mode.
- SQL result payloads are tuple sinks, even for pgbench's one-column
  `SELECT abalance` result. Do not add a selected-DPU scalar-only shortcut and do
  not accept validation that depends on `RemoteExecutionCommandCompletion` scalar
  fields. The wrapper should drain the tuple-result sink by borrowing, reading,
  and releasing the returned tuple/batch while keeping the persistent result-sink
  mapping alive until session close.
- The first implementation slice for this decision landed on June 30, 2026:
  `RemoteExecutionCommandCompletion` exposes tuple-result metadata through the
  public frontend API, shared frontend helpers bind/drain command result tuple
  sinks, and the pgbench wrapper no longer reads public scalar completion fields.
  Lower-level wire/smoke scalar members are still transitional and should not be
  used for selected-DPU acceptance.
- selected-DPU result byte rings should be allocated eagerly during session
  lifecycle setup. Lazy result-ring allocation would require a second DPU mmap
  export/import path after the command session is live, and it would reintroduce
  hot-path setup complexity into the first row-producing command.

Completion-discovery decision:

- do not add a second DMA-visible completion-ready bitmap or hint structure in
  the first implementation.
- the first DPU completion pull path should use the exported backend completion
  mailbox/control metadata that already exists, and poll it in the same
  scheduler-bounded grouped-control style used elsewhere. This is an
  `O(active sessions)` scan over compact control/header metadata, not a scan over
  full payload bodies.
- a separate discovery hint is only worth adding after measurement shows the
  compact scan is a bottleneck. If added later, prefer owner-separated
  monotonic epochs over a shared clearable bitmap, because backend CPU ORs and
  DPU DMA clears on the same word can lose updates without an explicit protocol.
- implement backend-completion migration in explicit slices: first an
  engine-only TCP smoke where the host publishes one backend completion mailbox
  slot and the DPU DMA-reads it and writes `consumedEpoch`; then production DMA
  engine APIs for completion-control reads, completion-slot reads, staged
  completions, and consumed-epoch publication; then scheduler semantic
  integration that maps staged completions into the existing frontend poll
  response path. Do not remove the selected-DPU command guards before this
  sequence has positive and negative no-fallback evidence.

Selected-DPU backend completion event decision:

- a backend completion mailbox record is an ordered command-state event, not
  necessarily a terminal completion. It may carry `STARTED`, `COMPLETED`, or
  `FAILED`. `STARTED` is semantically meaningful because row-producing commands
  use it to publish result-sink readiness and result metadata before terminal
  completion.
- mirror the existing host-process service semantics in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
  `TupleSinkServiceConsumeCompletionMailbox()` consumes one physical mailbox
  epoch, copies the command-state event into session state, publishes
  push-visible `STARTED` or terminal events, and only terminal states drive
  command-finished cleanup. `TupleSinkServicePublishClientCommandCompletion()`
  preserves the ordered frontend-visible event stream where a row-producing
  command can publish `STARTED` with a result descriptor and later publish
  `COMPLETED` or `FAILED`.
- the selected-DPU path must therefore treat `STARTED` as nonterminal but not
  ignorable. Accepting a `STARTED` event must not clear `commandInFlight`, must
  not clear `inFlightCommandSequence`, and must not allow the next command for
  that session to be staged. Only a terminal `COMPLETED` or `FAILED` event may
  clear the one-command-in-flight state after the frontend-visible terminal
  response has been queued or published according to the current response
  publication state machine.
- validate the semantic command sequence from
  `CitusRemoteExecCommandCompletion.commandSequence` against the selected-DPU
  session's in-flight command sequence. Do not compare the physical backend
  mailbox epoch to the semantic command sequence. The mailbox epoch is only the
  consumed-credit frontier to write back to the backend mailbox.
- replace the single selected-DPU `backendCompletionReady` slot with an ordered
  frontend completion-event queue or equivalent per-session event stream. It
  must be able to hold `STARTED` and terminal events for the same command in
  order. The temporary `POLL_COMMAND_COMPLETION` compatibility bridge may pop
  from this queue, but it must not clear all command state when it returns a
  nonterminal `STARTED`.
- replace any engine "peek/copy staged completion" API with claim/borrow
  ownership transfer. A staged backend completion should become service-visible
  exactly once. The claim handle should identify the local staging buffer, import
  and descriptor identity, bridge/ring generation, service session, physical
  mailbox epoch, and command sequence. A borrowed staging buffer may avoid a copy
  as long as its lifetime is pinned until the service releases the claim or until
  response publication no longer needs that memory.
- keep separate state for service-visible readiness, claimed local-buffer
  ownership, and consumed-credit DMA. A claimed event may still need a
  `consumedEpoch` DMA write in flight, but it must no longer be counted as an
  unclaimed ready event. Observing the same physical completion epoch again after
  it has been claimed is a service/engine bug to diagnose or fail fatally; do
  not treat it as a harmless duplicate.

Selected-DPU command-sequence decision:

- the DPU service owns per-session `commandSequence` assignment for
  selected-DPU command sessions. The sequence is DPU-local session state keyed by
  `serviceSessionId`, and is materialized into the backend command mailbox record
  before backend-command DMA publication.
- the host frontend learns the assigned sequence through the normal DMA response
  publication for `CitusRemoteExecStartCommandResponse`; `commandSequence` is a
  response-body field, so there is no separate DMA just to return the sequence.
  The response publication remains a two-part DPU-to-host operation: write the
  response body, then write the response-ready publication word. Under the
  current farnet setup (`PCI_WR_ORDERING=force relaxed`), do not submit those two
  tasks back-to-back and treat the second as a host-visible publication
  guarantee. The selected-DPU hot path should use async two-phase DMA
  publication: response-body completion callback submits the response-ready DMA
  task, without calling `doca_pe_progress()` or running semantic service logic.
- do not preserve the old host-process shared-memory response path for
  selected-DPU commands. That path is only the behavior being replaced: the
  current frontend waits for the local service to fill the control-slot response
  and then reads `response->commandSequence`.
- repeated frontend `POLL_COMMAND_COMPLETION` requests are a temporary
  compatibility bridge from the host-process Homer path, not the DPU target hot
  path. Selected-DPU command execution should become one host-published command
  request plus one host-local wait on a DPU-published frontend completion
  mailbox/slot.
- after backend-command DMA submission succeeds, the DPU service may enqueue the
  corresponding frontend `START_COMMAND` response, but host-visible publication of
  that response must follow the async two-phase body-completion-then-publish
  rule above. Callback retirement remains resource cleanup and fatal-error
  detection for unrelated tasks; only same-chain publication callbacks may submit
  the next DMA task.
- the backend-command DMA body byte count must come from the finalized
  `TupleSinkServiceLocalCommandRecordRdmaBytes()` result stored with the queued
  command. The default compact build writes the finalized command prefix; a
  non-compact diagnostic build can still request the fixed-size body.
- later performance work should revisit PCIe write ordering. If the machine is
  switched away from `PCI_WR_ORDERING=force relaxed`, or if per-MKey/non-relaxed
  ordering is available for these host publication regions, re-test whether a
  one-phase/single-DMA or back-to-back body-plus-publish shape is correct and
  faster. Until then, correctness assumes force-relaxed ordering and uses the
  two-phase DMA chain.

The selected-DPU setup order for a command-capable session should be:

1. The real command-session open path detects selected-DPU mode and calls a real
   DPU frontend open helper, rather than the setup-smoke guard.
2. Host frontend asks the DPU service over TCP setup for a DPU session allocation
   or validates a host-proposed identity.
3. Host lifecycle shim creates the command/completion mailboxes using the agreed
   service session id and the existing naming rule.
4. Host lifecycle shim registers/exports the grouped control block, request
   slots, backend mailboxes, and completion/credit memory that the DPU must DMA.
5. Host lifecycle shim sends one TCP setup payload containing the descriptor
   table plus all mmap-export entries/blobs required for this session.
6. DPU service imports those exports atomically as one setup operation and
   acknowledges the exact generation, ring geometry, and session identity.
7. Host lifecycle shim submits `CitusRemoteExecBackendSpawnRequest` locally to
   postmaster with the same session identity and backend startup metadata.
8. Only after setup/import/spawn succeeds may the host publish hot-path DMA
   frontiers for DPU pull.

The hot path after this setup remains DPU DMA: the host produces records and
frontiers in exported memory; the DPU pulls commands/payloads and writes
completion/credit state. The lifecycle shim is not a transport fallback and must
not forward every command back to the old host Homer service.

### Sync-Event Optional Path

Do not put sync-event on the default hot path. If we later add idle wakeup:

- host creates or imports a sync-event using [`doca_sync_event_create()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_sync_event.h:182) or [`doca_sync_event_create_from_export()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_sync_event.h:215)
- configure publisher/subscriber locations with the add-location APIs around [`doca_sync_event.h:332`](dpu_dma_backend_homer_service_plan_doca_headers/doca_sync_event.h:332)
- host or DPU updates a value with [`doca_sync_event_update_set()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_sync_event.h:1502)
- wait with [`doca_sync_event_wait_gt()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_sync_event.h:1523) or async wait/notify tasks

Even in that mode, the grouped control block remains authoritative. Sync-event is only a doorbell saying "go look."

## Publication And Ordering Invariants

Host-produce / DPU-pull:

1. Host reserves ring space using local `consumedHead`/credit.
2. Host writes the complete record body and descriptor.
3. Host performs a release publish of the ring's `publishedTail` or the grouped control entry.
4. DPU DMA-reads the grouped control block.
5. DPU posts DMA reads only for ranges at or below the observed published frontier.
6. DPU validates record sequence/generation/checksum if present.
7. DPU marks records complete only when DMA completion callbacks retire a contiguous prefix.
8. DPU DMA-writes `consumedHead`/credit back to host after the contiguous prefix is safe to release.

DPU-produce / host-consume:

1. DPU writes completion/result slot bytes into host memory with DMA.
2. DPU waits for the data-DMA completed frontier.
3. DPU publishes the host-visible completion frontier by DMA writing a ready epoch/tail or grouped completion entry.
4. Host consumes only slots at or below the published frontier with acquire-side loads.

Cross-context rule:

- payload DMA and the publication operation that depends on it should be on the same DMA context whenever possible
- if they are split across contexts, publication must be explicitly completion-gated on the data context's completed contiguous frontier
- same-thread submission to two DMA contexts is not an ordering primitive

Grouped control rule:

- the DPU may observe new work only from grouped control DMA reads or a future equivalent
- per-ring payload DMA reads must not race ahead of the observed frontier
- a host control-block update must be monotonically increasing per ring/generation

Direct DPU-controlled RDMA forwarding rule:

1. Host writes and release-publishes the source record exactly as in the
   host-produce / DPU-pull rule.
2. DPU observes the published frontier from grouped control or an accepted
   mailbox/control entry.
3. DPU builds or reuses a source `doca_buf` that points at the PCI-imported host
   bytes and has the correct data length.
4. DPU builds or reuses a target `doca_buf` that points at the RDMA-visible
   destination address.
5. DPU posts the RDMA write and tracks the lane until RDMA completion.
6. DPU publishes source consumed credit only after the RDMA completion retires
   the contiguous source frontier for that ring/session.

This is the zero-extra-copy candidate for small command/completion/result
records. It is not a reason to let the host free or reuse the source bytes early;
host memory lifetime still extends until the DPU publishes source credit.

Staged bulk forwarding rule:

1. Host writes and release-publishes the source byte-ring range.
2. DPU DMA-pulls the range into a DPU-local mirrored staging ring/sink.
3. DMA completion makes the DPU-local staging bytes valid and advances the
   source-consumed frontier that can be published back to the host.
4. DPU posts RDMA writes from the DPU-local staging lane to the peer/frontend
   destination.
5. RDMA completion releases the DPU-local staging lane for reuse.

This is the preferred default for bulk byte-stream/basebackup ranges when a
large DMA read plus DPU-local RDMA source gives better throughput or simpler
wrap/lifetime ownership than direct PCI-source RDMA.

Callback rule:

- DOCA callbacks and RDMA CQ drains may update lane state, completion frontiers,
  and local ready queues.
- Callbacks must not call `doca_pe_progress()` or run scheduler policy.
- A callback may mechanically submit a same-lane publication task only when all
  task objects, buffers, and destination metadata are preallocated and the action
  is a fixed continuation of the completed task. Any semantic decision or grant
  choice belongs in a later scheduled action.

## Completion Optimization Policy

The opt+flush strategy is a DPU-initiator completion-report optimization, not application batching.

For one DOCA context and one class:

```text
for each DOCA task in a submit group except the sentinel:
  submit with DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS
submit sentinel task without OPTIMIZE_REPORTS and with DOCA_TASK_SUBMIT_FLAG_FLUSH
drain PE only while progress is immediately available and the bounded progress budget remains
retire all tasks whose callbacks are delivered and advance contiguous frontiers
```

Use it where it helps:

- many small command records across sessions sharing one command-pull DMA context
- byte-stream records when records are separate or non-contiguous but still benefit from lower completion-report overhead
- grouped control-block polling if multiple control-block reads are submitted in one class window
- staged bulk transfer, where delayed completion reports can be amortized across
  multiple non-semantic chunks before a flushed sentinel

Avoid misusing it:

- it does not merge per-session tails
- it does not replace the semantic publish frontier
- it is not helpful for a strict one-record ping-pong where latency dominates and each command must publish before the next command can proceed
- if records are contiguous and have the same fate, prefer one larger DMA read instead of many small reads plus one tail
- it should not be applied blindly to direct small-record RDMA until latency
  measurements show the callback delay is acceptable

Suggested initial queue-depth policy from the standalone sweep:

- command/control `64 B` and `1 KiB`: start near window `16`
- mixed command and medium records: start at window `32`
- `4 KiB`: measure `16`, `32`, and `64`
- `16 KiB` and larger byte-stream chunks: start at `8-16`, then tune

Reusable DOCA buffer/task policy:

- Preallocate a fixed lane array per class. A lane owns its DOCA task handle,
  source `doca_buf`, destination `doca_buf`, generation, operation id, source
  ring identity, and destination identity while it is in flight.
- Reposition reusable direct-RDMA source buffers with
  `doca_buf_inventory_buf_reuse_by_data()`, matching the validated source setup
  in [`doca_pci_rdma_bridge_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_pci_rdma_bridge_validation.c:839). The explicit data length is required for the PCI-imported source buffer.
- Reposition address-only RDMA target buffers with
  `doca_buf_inventory_buf_reuse_by_addr()`, matching the target setup in
  [`doca_pci_rdma_bridge_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_pci_rdma_bridge_validation.c:842).
- Update task fields and user data before resubmission, as in the direct RDMA
  path around [`doca_pci_rdma_bridge_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_pci_rdma_bridge_validation.c:845) and the staged DMA/RDMA paths around [`doca_pci_rdma_bridge_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_pci_rdma_bridge_validation.c:858) and [`doca_pci_rdma_bridge_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_pci_rdma_bridge_validation.c:886).
- Production code should encode an explicit lane id plus generation or operation
  id in `doca_task_set_user_data()` or verbs `wr_id`. Do not infer ownership
  from `op_index % window`; the ibverbs hybrid harness uses explicit lane/op
  packing around [`doca_dma_ibverbs_rdma_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_dma_ibverbs_rdma_validation.c:575), posts it through `wr_id` around [`doca_dma_ibverbs_rdma_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_dma_ibverbs_rdma_validation.c:965), and unpacks it on completion around [`doca_dma_ibverbs_rdma_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_dma_ibverbs_rdma_validation.c:1106).
- Reuse the task itself instead of allocating on the hot path. The hybrid
  harness shows reusable DMA task field updates around [`doca_dma_ibverbs_rdma_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_dma_ibverbs_rdma_validation.c:939) and opt+flush submission around [`doca_dma_ibverbs_rdma_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_dma_ibverbs_rdma_validation.c:795).
- For staged bulk RDMA through verbs, batching can use an `ibv_send_wr` chain as
  in [`doca_dma_ibverbs_rdma_validation.c`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_dma_ibverbs_rdma_validation.c:975), but Homer still needs per-lane ownership and completion accounting before source credit or staging reuse.

## Discovery Strategy

Do not poll each ring tail with one tiny DMA read. The DPU should discover work by reading grouped control blocks:

```text
HomerDpuControlBlock
  header
  command_entries[session_count]
  completion_credit_entries[session_count]
  byte_stream_entries[stream_count]
```

The DPU service should DMA-read one or more cacheline/page-sized slices of this block, compare each entry's published frontier against its local submitted/completed state, and only then post payload DMA reads.

Polling cadence should be scheduler-controlled:

- latency class: frequent small grouped-control reads, bounded payload window
- throughput class: less frequent discovery, larger byte/task budget
- idle class: optional sync-event or a future TCP-side wakeup to restart polling after quiescence

## Lifecycle And Ownership

Setup:

1. Host frontend creates/registers host memory and grouped control block.
2. Host frontend starts TCP setup and sends one descriptor table plus the
   mmap-export table/blobs for the selected-DPU session.
3. DPU imports all mmap descriptors in that setup message and creates local
   `doca_buf` views.
4. DPU registers ring descriptors with `HomerDpuTransferEngine` only after every
   export and descriptor in the setup message validates.
5. Host publishes a generation-valid control entry only after DPU acknowledges setup.

Steady state:

- host owns writes to backend-produced record memory and host publication frontiers
- host owns the lifetime of exported source memory until the DPU publishes
  consumed credit for the relevant ring/frontier
- DPU owns DOCA task submission, DOCA callbacks, RDMA WR posting, RDMA CQ
  retirement, consumed-head writes, and DPU-produced completion/result writes
- DPU owns all `doca_buf` handles, DOCA task objects, RDMA lane metadata, and
  generation/op-id validation associated with imported host memory, but not the
  host memory lifetime itself
- DPU owns DPU-local staging memory for staged bulk transfer; a staging lane is
  reusable only after the RDMA completion for the staged bytes has retired
- service scheduler owns work selection, budgets, and fairness

Teardown:

- host sets terminal flags in the grouped control entry
- DPU stops admitting new ready-queue entries for that ring/session generation
- DPU drains submitted DOCA DMA tasks and RDMA WRs that reference that ring,
  imported mmap, or DPU-local staging lane
- DPU publishes final consumed head or error state
- host waits for final credit/ack before unregistering memory
- DPU destroys or invalidates `doca_buf` views, task-lane ownership, and imported
  mmap descriptors before the host stops/destroys the exported mmap

Failure handling:

- every ring descriptor needs generation and state so stale DPU work cannot write into a reused host region
- DPU DMA error callbacks must mark the ring/session failed and publish a terminal/error state when possible
- RDMA completion errors on direct or staged forwarding are fatal to the affected
  Homer service generation unless a narrower recovery policy has been designed
- host must not unmap or destroy a registered region until the DPU has acknowledged teardown or the whole DPU service generation has been reset

## Mechanism Milestones And Stage Mapping

The numbered items below are mechanism milestones, not a second implementation
stage schedule. Continue to execute the current branch through
[`dpu_dma_backend_homer_service_current_scheduler_design.md`](dpu_dma_backend_homer_service_current_scheduler_design.md):

- current Stage 8B / 8A implements host-DPU command/control DMA,
  backend-completion pull, and DPU-to-host two-phase DMA publication
- current Stage 8C implements direct small-record RDMA forwarding from
  PCI-imported host source memory to RDMA-visible destinations, with source
  credit gated by RDMA completion
- current Stage 9 implements staged bulk byte-stream transfer: host byte ring to
  DPU-local staging by DOCA DMA, then RDMA from staging, with source credit and
  staging reuse gated by the correct completion frontier
- current Stage 10 implements transfer teardown and generation reset
- current Stage 11 implements tuning, mixed-workload validation, and selector
  promotion
- direct small-record RDMA is a transfer-engine mechanism for command,
  completion, and result records; in the current-scheduler execution plan it is
  Stage 8C rather than a replacement for Stage 9's staged bulk path

1. **Bridge ABI and descriptor roles**: keep `homer_dpu_bridge_abi.h` as the
   shared fixed-width ABI. Add explicit descriptor roles for host source rings,
   host destination/publication rings, DPU-local staging rings, direct-RDMA
   target exports, and source-credit lines. Validation: compile-only ABI include
   check plus a descriptor-size/static-assert smoke.
2. **Host frontend lifecycle/export setup**: keep the TCP setup socket as the
   cold path. Send one setup message per backend/session open containing the
   descriptor table and all mmap-export blobs needed by that session. Allocate
   command, completion, result, and byte-stream rings eagerly on open; do not
   lazily allocate the tuple result sink on first row-producing command.
   Validation: setup smoke proves the DPU imports all descriptors and rejects a
   missing role or generation mismatch.
3. **DPU transfer engine skeleton**: extend the current `homer_service_dpu_dma`
   module into the owner of DOCA DMA contexts/PEs, imported mmap descriptors,
   RDMA/QP/CQ lane metadata, DPU-local staging memory, and fixed lane pools.
   Keep allocation off the hot path. Validation: create/destroy/import smoke
   with no hot-path transfer.
4. **Grouped-control discovery and local ready queues**: keep grouped control as
   the only default work-discovery source. DMA-read grouped control blocks,
   compare frontiers locally, and enqueue exact ready refs into fixed per-class
   queues. Validation: multi-session frontier changes produce one queue entry
   per ready ring, duplicate discovery is suppressed, and stale generation
   entries are rejected.
5. **Scheduler source/action skeleton**: add transfer source kinds, action
   kinds, ready counters, and no-op bounded executors. Use `HomerGrantVector`
   for DPU transfer work. Validation: scheduler traces show the transfer actions
   can be selected without hidden DOCA/RDMA calls in ready-set building.
6. **Nonblocking drain actions**: implement `DRAIN_DOCA_PE` and `DRAIN_RDMA_CQ`
   as separate bounded actions. They may drain while progress is immediately
   available, but return on the first zero-progress call or grant exhaustion.
   Validation: in-flight counters, lane reuse, and completion frontiers advance
   only from drain actions or allowed same-lane fixed callbacks.
7. **Host-DPU command/control DMA path**: implement DMA pull of host-published
   command/control records and two-phase DPU-to-host DMA publication for local
   frontend/backend responses, completions, and credits. Validation: long 64 B
   stress with force-relaxed PCI ordering; negative controls should expose early
   publication if the body-completion gate is removed.
8. **Direct small-record RDMA forwarding**: implement direct DPU-controlled RDMA
   lanes for small forwarded records. Source buffers use PCI-imported host
   memory with `doca_buf_inventory_buf_reuse_by_data()`, target buffers use
   RDMA-visible destination memory with `doca_buf_inventory_buf_reuse_by_addr()`,
   and source credit is published only after RDMA completion. Validation: 64 B,
   1 KiB, and 4 KiB multi-session stress with lane-id/generation checks and no
   source reuse before RDMA completion.
9. **Staged bulk transfer**: implement host-to-DPU DMA into a DPU-local mirrored
   staging ring/sink, then RDMA write from staging memory. Source credit may
   advance after DMA completion of the contiguous staged frontier; staging lanes
   are reusable only after RDMA completion. Validation: 16 KiB through 1 MiB
   byte-stream stress with wrap, source-credit, staging-reuse, and teardown
   checks.
10. **Tuple result path integration**: remove the local same-host result fast
    path from selected-DPU validation. Row-producing SQL should use the same
    tuple/result sink semantics as the real remote path: STARTED carries result
    metadata, terminal completion closes the command, and the frontend borrows,
    drains, and releases result tuples without rebinding or closing the session
    sink per command. Validation: selected-DPU row-producing pgbench command
    uses the DMA/RDMA path and does not map the old host-service control region.
11. **Basebackup bulk integration**: wire basebackup byte-stream records to the
    staged bulk path first, then compare direct RDMA for medium chunks only if
    measurements show a reason. Validation: local blackhole-equivalent DPU path
    and remote RDMA blackhole path both preserve bytes, frontiers, and teardown.
12. **Publication, credit, and teardown hardening**: make teardown a scheduled
    phase that first stops new admissions, then drains DOCA/RDMA in-flight work,
    then publishes final credit/terminal state, then destroys imported resources.
    Validation: repeated open/close, timeout, and early host-close tests produce
    no DOCA I/O errors and no stale callbacks into reused generations.
13. **Reusable lane and opt+flush tuning**: replace per-operation allocation with
    preallocated reusable lanes for DMA and DOCA RDMA tasks. Enable
    `OPTIMIZE_REPORTS` plus flushed sentinel only where measurements justify it,
    starting with staged/bulk and grouped-control classes. Validation: queue-depth
    sweeps for 64 B, 1 KiB, 4 KiB, 16 KiB, 64 KiB, 256 KiB, and 1 MiB under the
    scheduler, with CPU usage and tail latency recorded.
14. **Policy tuning and fairness**: tune class windows, grant order, direct vs
    staged thresholds, and PE/CQ sharing. Compare class-level sources against
    exact per-session ready refs only if latency measurements show a fairness
    problem. Validation: foreground pgbench with background basebackup, c1 and
    c4, with warmed runs and service log scans.
15. **Promotion gates**: remove transient selected-DPU shims and old local
    fast-path assumptions as their replacements pass validation. The final DPU
    path should not rely on SHM fallback for correctness; SHM may remain only as
    unrelated legacy code until it is deleted intentionally.

## Things We Should Not Do First

- Do not put DOCA DMA submission in ordinary PostgreSQL backend hot paths.
- Do not poll one 8-byte tail per ring with a separate DMA read.
- Do not use sync-event as the default hot-path publication mechanism.
- Do not build one monolithic "bridge" source file that mixes PostgreSQL frontend code, service scheduler code, and DPU DOCA state.
- Do not run a private DPU transfer busy loop outside the service scheduler.
- Do not drain the DOCA PE opportunistically from unrelated synchronous waits; PE drain should be a scheduled action with a bounded budget.
- Do not drain RDMA CQs opportunistically from unrelated synchronous waits; CQ drain should also be a scheduled action with a bounded budget.
- Do not assume `OPTIMIZE_REPORTS` is application batching.
- Do not split payload and dependent publication/source-credit release across contexts or lanes unless publication is completion-gated on the data movement that makes it safe.
- Do not enable or depend on per-mmap relaxed ordering until a supported API path exists and the exact Homer bridge layout passes stress.

## Validation Gates

Before treating the design as ready for Homer integration:

- grouped-control DMA polling must not miss tail updates or observe invalid generations
- command ring pull must pass long 64 B stress with multiple sessions and no stale/torn records
- completion/result DMA write must pass host-consume stress with early-publish negative controls
- direct small-record RDMA forwarding must pass 64 B, 1 KiB, and 4 KiB
  multi-session stress with PCI-imported source buffers, explicit lane/op ids,
  source credit held until RDMA completion, and no modulo-window ownership
  inference
- staged bulk transfer must pass 16 KiB through 1 MiB byte-stream stress with
  source credit released after DMA completion, DPU-local staging released after
  RDMA completion, and wrap/teardown coverage
- split-context variants must fail or pass exactly as expected: no-wait split should remain rejected, completion-gated split should pass
- selected-DPU backend completion handling must preserve the existing
  push-visible event stream: a row-producing command may produce `STARTED` with
  result metadata and then one terminal event. `STARTED` must not clear
  one-command-in-flight state, the physical mailbox epoch must not be compared to
  the semantic command sequence, and each physical backend completion epoch must
  be claimed exactly once before consumed-credit DMA publication.
- queue-depth sweep must be repeated in the Homer-like grouped-control path
- opt+flush sentinel policy must be measured against reliable completions and no `doca_pe_progress()` starvation
- teardown must prove host memory is not unmapped while DOCA DMA tasks, DOCA
  RDMA tasks, or verbs RDMA WRs can still target or source it
- repeated open/close must prove all host-side frontend state and DPU-side
  transfer state are reset before session identity or descriptor generations are
  reused
- selected-DPU SQL validation must include at least one row-producing command and
  prove that result-sink setup does not call the old host-service control region
  (`/citus_remote_execution_control_v27`)

## Cross-Links

- [doca_host_dpu_homer_boundary.md](doca_host_dpu_homer_boundary.md): broader host-DPU boundary and rationale.
- [doca_dma_ordering_visibility_validation_plan.md](doca_dma_ordering_visibility_validation_plan.md): empirical DOCA ordering, publication, queue-depth, opt+flush, and split-context evidence.
- [homer_frontend_service_separation_plan.md](homer_frontend_service_separation_plan.md): completed frontend/service split that created the `homer_frontend_dma.c` replacement point.
- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md): RDMA publication rule that motivates explicit host-DPU publication frontiers.
- [service_progress_control_data_plane_scheduler.md](service_progress_control_data_plane_scheduler.md): service-progress scheduler shape that the DPU transfer engine should integrate with.
