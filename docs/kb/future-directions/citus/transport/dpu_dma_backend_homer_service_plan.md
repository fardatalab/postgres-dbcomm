# DPU DMA Backend Homer Service Plan

## Scope

- **What this doc explains**: the implementation-facing plan for replacing the current host-side standalone Homer service boundary with a DPU-resident Homer service that pulls backend-produced records from host memory using DOCA DMA.
- **What this doc does not cover**: a final committed implementation, a DPA-kernel port, peer-service RDMA redesign, or a new SQL/session semantic model.
- **Header snapshot**: DOCA API headers referenced below are copied under [`dpu_dma_backend_homer_service_plan_doca_headers/`](dpu_dma_backend_homer_service_plan_doca_headers/).
- **Doc type**: `future-direction`

## Current Decision

The target hot path is DPU pull from host memory, not host push through backend-submitted DMA tasks.

The PostgreSQL/Citus backend remains a CPU producer into host memory. It writes command records, payload descriptors, and byte-stream chunks into SPSC rings or byte rings, then release-publishes a grouped frontier. The DPU Homer service is the DOCA DMA initiator: it discovers published host frontiers, posts async DMA reads, tracks completed contiguous records, executes or forwards the work, and writes consumed head/credit updates back to host memory.

Sync-event is not required for the hot path. The current empirical decision is to use memory-resident grouped control blocks/frontiers as the authoritative publication mechanism. Sync-event can remain an optional cold/idle wakeup experiment, but the default hot path should work by polling grouped control blocks with DMA and by explicit DMA completion tracking.

The implementation should keep three layers separate:

1. `homer_frontend_dma.c/.h`: host-side frontend channel replacement beside [`homer_frontend_shm.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:1).
2. `homer_dpu_bridge_abi.h`: fixed-width bridge ABI shared by the host frontend and DPU service.
3. `homer_service_dpu_dma.c/.h`: DPU-side DMA engine integrated into the service scheduler near [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36127).

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
- The current payload scheduler derives egress budgets through [`HomerServicePayloadEgressPassBudgetBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6046) and [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6271). The DPU DMA engine should reuse this shape for work budgeting rather than inventing an unbounded DMA loop.
- The current host-service byte-ring sender, [`HomerServicePumpOutgoingByteRingPayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26843), documents the existing invariant: backend producer writes complete transport records into registered shared memory, and the service validates/coalesces/transports those records.

## Workload Classes

Start with three DPU DMA workload classes:

1. **Commands**: backend-produced command/control records. These are latency-sensitive and often have `K=1` per session because the next command may depend on the previous completion.
2. **Completions/results**: DPU/service-produced completion or result records consumed by the host backend. Direction is DPU DMA write into host memory, then host consumes from a published frontier.
3. **Basebackup and byte-stream chunks**: backend-produced throughput-oriented byte-ring records. These can use larger contiguous DMA tasks and less frequent consumed-head publication when the byte-ring layout allows it.

Each class should have its own DMA context first. Context-per-class gives independent queue-depth, completion-report, and priority policy. Sharing one PE is acceptable initially if the scheduler has explicit progress budgets; split PEs only if shared progress causes measurable interference.

Do not use one semantic tail across sessions. Multiple sessions can share one DMA context for initiator-side completion-report coalescing, but each session/ring still owns its own published frontier and consumed head.

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
- exchange descriptors and ring metadata with the DPU service over COMCH during setup
- write backend-produced command/payload records into host rings
- release-publish grouped frontier entries after records are complete
- read DPU-written consumed heads/completion frontiers with acquire semantics
- keep `OpenRemoteExecutionSession()`, `StartRemoteExecutionCommand()`, and `PollRemoteExecutionCommandCompletion()` stable for ordinary callers

The host frontend should not submit hot-path DMA tasks for backend-produced records. That would move hardware submission cost into PostgreSQL/Citus backends and would undo the scheduler/offload goal.

Memory registration should be long-lived. A first prototype can register per service/session/stream lifetime; do not register or export per command, per completion, or per chunk.

## DPU Service DMA Engine

`homer_service_dpu_dma.c/.h` should own DPU-side DOCA objects and expose a service-facing progress API.

Expected service API shape:

```c
typedef struct HomerDpuDmaEngine HomerDpuDmaEngine;

bool HomerDpuDmaEngineCreate(HomerDpuDmaEngine **engine, const HomerDpuDmaConfig *config);
void HomerDpuDmaEngineDestroy(HomerDpuDmaEngine *engine);
bool HomerDpuDmaRegisterRing(HomerDpuDmaEngine *engine, const HomerDpuRingDescriptor *descriptor);
bool HomerDpuDmaProgressControlPoll(HomerDpuDmaEngine *engine, HomerProgressResult *result);
bool HomerDpuDmaProgressPayloadPull(HomerDpuDmaEngine *engine, const HomerTransportGrant *grant, HomerProgressResult *result);
bool HomerDpuDmaProgressCompletionPush(HomerDpuDmaEngine *engine, HomerProgressResult *result);
bool HomerDpuDmaDrainPe(HomerDpuDmaEngine *engine, const HomerProgressGrant *grant, HomerProgressResult *result);
```

Internal state:

- one DMA context per workload class initially: commands, completions/results, byte-stream chunks
- one PE initially, with explicit per-class drain budgets
- preallocated `doca_dma_task_memcpy` pools per ctx and per direction
- preallocated `doca_buf` objects or reusable buffer inventory entries for grouped control blocks, payload windows, and completion writes
- per-ring state: host frontier last observed, DMA-submitted tail, DMA-completed contiguous tail, DPU-consumed head, host-visible consumed head last published
- per-class policy: async window, sentinel interval, flush/report flags, max bytes per pass, max completions per pass

Progress loop shape:

```text
DMA-read grouped control block for one workload class or all classes
inspect frontiers on DPU
for each ring with publishedTail > submittedTail:
  reserve task slots up to class window and service grant
  submit DMA reads for complete record ranges
drain PE with class budget, stopping on budget exhaustion or first no-progress return
advance per-ring completed contiguous frontier from callbacks
publish consumedHead/credits to host for completed contiguous records
submit DPU DMA writes for completions/results when service produces them
drain PE again or leave completions for next scheduler pass based on budget
```

Do not call synchronous helper loops on every DMA task in the hot path. The clean design is async submission, explicit PE progress, and contiguous-frontier retirement.

## Scheduler Integration Plan

The DPU DMA engine must be scheduled like existing RDMA WR posting, CQ polling,
mailbox draining, local-control advancement, and payload-stream work. It should
not run a private busy loop that polls grouped control blocks and drains the PE
outside [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36127).

The right integration is to expose DPU DMA as first-layer scheduler sources and
second-layer action grants:

```text
ready-set builder:
  cheap checks: any in-flight DMA completions? any control-poll due? any class has queued frontier? any consumed-head publish pending?
policy:
  rank DPU DMA sources against RDMA CQ drain, local control, command rings, completion rings, payload streams, heartbeat
action executor:
  run one bounded DPU DMA action: submit poll, drain PE, submit payload reads, publish consumed head, push completions
result:
  report empty polls, completions drained, DMA tasks submitted, bytes moved, frontiers advanced, stillReady/budgetExhausted
```

Add new source kinds rather than hiding all DPU work under the existing
`HOMER_PROGRESS_SOURCE_PAYLOAD_STREAM` source:

- `HOMER_PROGRESS_SOURCE_DPU_DMA_CONTROL_POLL`: DMA-read grouped control blocks and discover host-published frontiers.
- `HOMER_PROGRESS_SOURCE_DPU_DMA_COMMAND_PULL`: submit/retire DMA reads for backend-produced command records.
- `HOMER_PROGRESS_SOURCE_DPU_DMA_PAYLOAD_PULL`: submit/retire DMA reads for byte-stream/basebackup or tuple payload records.
- `HOMER_PROGRESS_SOURCE_DPU_DMA_COMPLETION_PUSH`: submit/retire DMA writes for DPU-produced host completions/results.
- `HOMER_PROGRESS_SOURCE_DPU_DMA_PE_DRAIN`: drain DOCA PE completions across classes when in-flight tasks exist.
- Optional later source: `HOMER_PROGRESS_SOURCE_DPU_DMA_IDLE_WAKEUP` if sync-event or an OS wait primitive is added for idle mode.

These source kinds should map to the existing CPU-class vocabulary:

- control poll and command pull: `HOMER_PROGRESS_CPU_CLASS_HOT_CONTROL` or a new latency-sensitive class if the current enum becomes too coarse
- payload pull and completion push: `HOMER_PROGRESS_CPU_CLASS_HOT_DATA`
- COMCH setup/import/export: `HOMER_PROGRESS_CPU_CLASS_COLD_SETUP_MAINTENANCE`
- teardown/failure cleanup: `HOMER_PROGRESS_CPU_CLASS_CLOSE_LIFETIME`

The ready-set phase must stay cheap. [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12238)
should not DMA-read host memory or drain the PE while deciding readiness. It
should only inspect DPU-local counters maintained by the DMA engine:

- `inflightTaskCount[class] > 0` means PE drain is ready
- `controlPollDue[class]` or `controlPollInFlight == false` means grouped-control poll can be scheduled
- `discoveredReadyRings[class] > 0` means payload/command pull can submit work
- `completionPushQueueDepth > 0` means completion push can submit work
- `consumedHeadPublishPending > 0` means credit publication can run
- `classBlockedOnTaskPool` or `classBlockedOnHostCredit` should appear as blocked reason hints, not as a reason to spin inside the executor

The action executor is where real work happens. Extend
[`HomerServiceExecuteProgressActionGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:35511)
or the source-plan dispatch path with DPU DMA actions such as:

- `HOMER_PROGRESS_ACTION_DPU_DMA_POLL_GROUPED_CONTROL`
- `HOMER_PROGRESS_ACTION_DPU_DMA_DRAIN_PE`
- `HOMER_PROGRESS_ACTION_DPU_DMA_PULL_COMMANDS`
- `HOMER_PROGRESS_ACTION_DPU_DMA_PULL_PAYLOAD`
- `HOMER_PROGRESS_ACTION_DPU_DMA_PUSH_COMPLETIONS`
- `HOMER_PROGRESS_ACTION_DPU_DMA_PUBLISH_CONSUMED_HEADS`
- `HOMER_PROGRESS_ACTION_DPU_DMA_ADVANCE_SETUP`
- `HOMER_PROGRESS_ACTION_DPU_DMA_ADVANCE_TEARDOWN`

Each action must be bounded by the grant:

- `maxPolls`: max `doca_pe_progress()` calls or grouped-control poll completions
- `maxItems`: max rings/records to inspect or retire
- `maxPumpCalls`: max inner engine operations
- transport grant / class policy: max DMA tasks submitted and max bytes posted

The DPU DMA engine should expose action-specific entry points instead of one
generic "make progress" function. That keeps ownership clear and makes
scheduler accounting meaningful:

```c
bool HomerDpuDmaPollGroupedControl(HomerDpuDmaEngine *engine,
                                   const HomerProgressGrant *grant,
                                   HomerProgressResult *result);
bool HomerDpuDmaDrainPe(HomerDpuDmaEngine *engine,
                        const HomerProgressGrant *grant,
                        HomerProgressResult *result);
bool HomerDpuDmaSubmitCommandPulls(HomerDpuDmaEngine *engine,
                                   const HomerProgressGrant *grant,
                                   HomerProgressResult *result);
bool HomerDpuDmaSubmitPayloadPulls(HomerDpuDmaEngine *engine,
                                   const HomerTransportGrant *transportGrant,
                                   const HomerProgressGrant *grant,
                                   HomerProgressResult *result);
bool HomerDpuDmaPushCompletions(HomerDpuDmaEngine *engine,
                                const HomerProgressGrant *grant,
                                HomerProgressResult *result);
bool HomerDpuDmaPublishConsumedHeads(HomerDpuDmaEngine *engine,
                                     const HomerProgressGrant *grant,
                                     HomerProgressResult *result);
```

Use `HomerProgressResult` fields consistently:

- `emptyPolls`: grouped-control poll found no new frontier, or PE drain made no progress
- `cqesDrained`: DOCA task completions/callbacks retired by PE progress
- `itemsProcessed`: rings inspected, records retired, or completion entries published
- `wrsPosted`: DMA tasks submitted; it is still a useful "hardware work request" counter even though the API is DOCA rather than verbs
- `bytesPosted`: payload bytes scheduled for DMA read/write
- `transportCompletionFrontier`: highest contiguous DMA-completed record/byte frontier
- `sourceReleaseFrontier`: highest consumed head/credit published back to host
- `semanticObjectFrontier`: highest command/completion object made semantically visible
- `stillReady`: there is more known DPU-local work after this bounded grant
- `budgetExhausted`: the action stopped because the scheduler grant, task pool, or class window was exhausted
- `blockedOnCredit` / `blockedOnLocalResources`: host ring credit or DPU task-pool/window pressure blocked submission

The ordering between DMA actions matters. A useful default plan order is:

1. drain PE first if in-flight completions are present, so task pools and contiguous frontiers are refreshed
2. publish consumed heads/completion frontiers whose data DMA is already complete
3. poll grouped control blocks to discover new host-published work
4. submit command pulls, with small bounded windows and latency-sensitive priority
5. submit completion pushes that unblock host backends
6. submit payload/byte-stream pulls with remaining data-plane budget
7. drain PE again only if the policy grants another bounded PE action

This is a policy default, not a hard correctness rule. Correctness still comes
from per-ring frontiers and DMA completion gating. The scheduler can later
reorder actions to favor latency or throughput as long as publication never
runs ahead of completed data DMA.

PE drain is nonblocking. A drain action may call `doca_pe_progress()` repeatedly
while the grant budget remains and calls are returning progress, but it must
return to the scheduler as soon as `doca_pe_progress()` returns `0`. It must not
spin waiting for a particular task, sentinel, or frontier to complete. If
in-flight DMA remains after a zero-progress return, the engine records that
state locally; the ready-set builder can mark `DPU_DMA_PE_DRAIN` ready again in
a later service pass. Publication actions that depend on a completed frontier
must simply remain not-ready until the PE drain action has advanced that
frontier.

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

For the first implementation, keep DPU DMA sources coarse by workload class, not
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

### COMCH Setup

Use COMCH for cold setup, not hot payload movement:

- service side creates a server with [`doca_comch_server_create()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_comch.h:198)
- host side creates a client with [`doca_comch_client_create()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_comch.h:396)
- exchange setup messages with [`doca_comch_server_task_send_alloc_init()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_comch.h:652), [`doca_comch_client_task_send_alloc_init()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_comch.h:679), [`doca_comch_server_event_msg_recv_register()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_comch.h:738), and [`doca_comch_client_event_msg_recv_register()`](dpu_dma_backend_homer_service_plan_doca_headers/doca_comch.h:756)

COMCH messages should carry:

- protocol version and feature flags
- mmap export blobs
- ring descriptors
- grouped control block descriptor
- optional sync-event export if idle wakeup mode is enabled
- service/session/sink ids

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

## Completion Optimization Policy

The opt+flush strategy is a DPU-initiator completion-report optimization, not application batching.

For one DMA context and one class:

```text
for each DMA task in a submit group except the sentinel:
  submit with DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS
submit sentinel task without OPTIMIZE_REPORTS and with DOCA_TASK_SUBMIT_FLAG_FLUSH
drain PE only while progress is immediately available and the bounded progress budget remains
retire all tasks whose callbacks are delivered and advance contiguous frontiers
```

Use it where it helps:

- many small command records across sessions sharing one command-pull DMA context
- byte-stream records when records are separate or non-contiguous but still benefit from lower completion-report overhead
- grouped control-block polling if multiple control-block reads are submitted in one class window

Avoid misusing it:

- it does not merge per-session tails
- it does not replace the semantic publish frontier
- it is not helpful for a strict one-record ping-pong where latency dominates and each command must publish before the next command can proceed
- if records are contiguous and have the same fate, prefer one larger DMA read instead of many small reads plus one tail

Suggested initial queue-depth policy from the standalone sweep:

- command/control `64 B` and `1 KiB`: start near window `16`
- mixed command and medium records: start at window `32`
- `4 KiB`: measure `16`, `32`, and `64`
- `16 KiB` and larger byte-stream chunks: start at `8-16`, then tune

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
- idle class: optional sync-event or COMCH wakeup to restart polling after quiescence

## Lifecycle And Ownership

Setup:

1. Host frontend creates/registers host memory and grouped control block.
2. Host frontend starts COMCH and sends descriptors to the DPU service.
3. DPU imports mmap descriptors and creates local `doca_buf` views.
4. DPU registers ring descriptors with `HomerDpuDmaEngine`.
5. Host publishes a generation-valid control entry only after DPU acknowledges setup.

Steady state:

- host owns writes to backend-produced record memory and host publication frontiers
- DPU owns DMA task submission, DPU completion callbacks, consumed-head writes, and DPU-produced completion/result writes
- service scheduler owns work selection, budgets, and fairness

Teardown:

- host sets terminal flags in the grouped control entry
- DPU drains submitted DMA tasks for that ring
- DPU publishes final consumed head or error state
- host waits for final credit/ack before unregistering memory
- DPU stops using imported mmap before host stops/destroys the exported mmap

Failure handling:

- every ring descriptor needs generation and state so stale DPU work cannot write into a reused host region
- DPU DMA error callbacks must mark the ring/session failed and publish a terminal/error state when possible
- host must not unmap or destroy a registered region until the DPU has acknowledged teardown or the whole DPU service generation has been reset

## Implementation Stages

1. **Bridge ABI only**: add `homer_dpu_bridge_abi.h` with grouped control block and descriptor structs; no behavior change.
2. **Host frontend DMA skeleton**: add `homer_frontend_dma.c/.h` with setup/teardown scaffolding and a build-time switch, but keep SHM as default.
3. **COMCH descriptor exchange prototype**: exchange one mmap descriptor and one grouped control block with the DPU process; no payload movement.
4. **DPU DMA engine skeleton**: create DMA ctx/PE/task pools and import mmap descriptors; add explicit PE drain API.
5. **Grouped control polling**: DPU DMA-reads grouped control entries and reports observed frontiers without pulling payload.
6. **Scheduler source skeleton**: add DPU DMA source kinds, ready-set counters, action kinds, and no-op bounded executors so the scheduler can choose DPU work before real payload movement exists.
7. **Grouped control polling action**: DPU DMA-reads grouped control entries only when the scheduler grants `DPU_DMA_CONTROL_POLL`; report empty polls and discovered frontiers through `HomerProgressResult`.
8. **PE drain action**: drain the DOCA PE only under a scheduler grant, with explicit poll/completion budgets and no nested progress from callbacks.
9. **Command ring pull**: implement backend-produced command ring records with DPU async DMA reads and consumed-head DMA writes under command-pull grants.
10. **Completion/result push**: implement DPU DMA writes into host completion/result rings and host consumption through existing frontend completion APIs.
11. **Basebackup byte-stream pull**: move byte-ring chunk pulling to DPU DMA with larger windows and size-aware queue depth.
12. **Opt+flush tuning**: enable `OPTIMIZE_REPORTS` plus flushed sentinel by class; measure latency and throughput.
13. **Policy tuning**: compare class-level versus exact per-session DPU DMA sources, and tune grant order against RDMA CQ drain, local control, command, completion, and payload sources.

## Things We Should Not Do First

- Do not put DOCA DMA submission in ordinary PostgreSQL backend hot paths.
- Do not poll one 8-byte tail per ring with a separate DMA read.
- Do not use sync-event as the default hot-path publication mechanism.
- Do not build one monolithic "bridge" source file that mixes PostgreSQL frontend code, service scheduler code, and DPU DOCA state.
- Do not run a private DPU DMA busy loop outside the service scheduler.
- Do not drain the DOCA PE opportunistically from unrelated synchronous waits; PE drain should be a scheduled action with a bounded budget.
- Do not assume `OPTIMIZE_REPORTS` is application batching.
- Do not split payload and dependent publication across DMA contexts unless publication is completion-gated.
- Do not enable or depend on per-mmap relaxed ordering until a supported API path exists and the exact Homer bridge layout passes stress.

## Validation Gates

Before treating the design as ready for Homer integration:

- grouped-control DMA polling must not miss tail updates or observe invalid generations
- command ring pull must pass long 64 B stress with multiple sessions and no stale/torn records
- completion/result DMA write must pass host-consume stress with early-publish negative controls
- split-context variants must fail or pass exactly as expected: no-wait split should remain rejected, completion-gated split should pass
- queue-depth sweep must be repeated in the Homer-like grouped-control path
- opt+flush sentinel policy must be measured against reliable completions and no `doca_pe_progress()` starvation
- teardown must prove host memory is not unmapped while DPU tasks can still target it

## Cross-Links

- [doca_host_dpu_homer_boundary.md](doca_host_dpu_homer_boundary.md): broader host-DPU boundary and rationale.
- [doca_dma_ordering_visibility_validation_plan.md](doca_dma_ordering_visibility_validation_plan.md): empirical DOCA ordering, publication, queue-depth, opt+flush, and split-context evidence.
- [homer_frontend_service_separation_plan.md](homer_frontend_service_separation_plan.md): completed frontend/service split that created the `homer_frontend_dma.c` replacement point.
- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md): RDMA publication rule that motivates explicit host-DPU publication frontiers.
- [service_progress_control_data_plane_scheduler.md](service_progress_control_data_plane_scheduler.md): service-progress scheduler shape that the DPU DMA engine should integrate with.
