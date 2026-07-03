# DPU DMA Backend Homer Service Design For The Current Scheduler

## Scope

This document is a concrete implementation design for migrating Homer from the
current host-process service to a DPU-resident service while keeping the scheduler
model that exists today. It is a companion to
[`dpu_dma_backend_homer_service_plan.md`](dpu_dma_backend_homer_service_plan.md).

Implementation progress is tracked in
[`../../../implementations/citus/transport/dpu_dma_backend_homer_service_implementation_checkpoint.md`](../../../implementations/citus/transport/dpu_dma_backend_homer_service_implementation_checkpoint.md).

The important constraint is:

> Do not introduce the future async-continuation/workflow scheduler as part of
> the DPU migration.

That continuation model remains a later cleanup after the host-DPU migration is
stable. For this phase, the DPU DMA engine must plug into the existing
`machine-baseline` service-progress scheduler through current-style collectors,
machines where needed, action grants, feedback, and bounded execution.

## Design conclusion

The preliminary plan is directionally correct on the biggest architectural
choice: backend processes should remain CPU producers into host memory, and the
DPU-side Homer service should pull records with DOCA DMA. Host backends should
not submit hot-path DMA tasks.

However, the plan needs five corrections before implementation:

1. **The grouped control block is new ABI.** Today's host-process Homer has SHM
   control slots, ready bitmaps, completion mailboxes, SPSC queues, and byte
   rings. It does not have a DPU-friendly grouped frontier block. Treat that
   block as a first-class bridge ABI with generation, descriptor, ownership, and
   snapshot rules.
2. **DPU work must be scheduled as current collectors/actions.** The right
   integration point is the existing `machine-baseline` scheduler, not a private
   DPU loop and not the future continuation runtime. Ready-set building may read
   only maintained DPU-local facts; actual DMA submit and PE progress happen only
   under bounded action grants.
3. **DMA completion frontiers are physical, not semantic.** A basebackup or
   payload grant may issue many DMA tasks. Those task owners are below the
   scheduler's semantic layer. Publication, command completion, close/reclaim,
   and teardown still need explicit existing-machine state and generation checks.
4. **The setup transport is TCP, not DOCA COMCH.** COMCH was only intended to
   carry cold setup bytes: mmap export descriptors, bridge descriptors, feature
   bits, and acks. It is no longer an implementation dependency because stock
   DOCA COMCH currently fails on farnet1 below Homer, while standalone DOCA DMA
   validation still passes in both host-produce/DPU-pull and DPU-push
   directions. Use a regular TCP setup socket for the same cold-path bytes and
   keep all hot-path movement and publication in DOCA DMA-visible memory.
5. **PostgreSQL-local lifecycle needs a host shim, not a hot-path fallback.**
   A DPU-resident service cannot directly `shm_open()` host mailbox objects,
   signal the host postmaster, or run PostgreSQL postmaster-local backend-spawn
   hooks. Selected-DPU mode therefore needs a small host lifecycle shim in or
   beside the host DMA frontend. That shim creates/exports host memory, performs
   TCP setup, submits backend spawn requests locally, and then leaves the hot
   command/completion/payload path to DPU DMA. It must not forward every command
   through the old host Homer service.

## Current code shape to preserve

The current branch already has the split we need:

- `homer_frontend_shm.c` owns the current private host-side channel mechanics:
  mapping the control region, reserving a slot, publishing request-ready state,
  waiting for response-ready state, and mapping local completion objects.
- `homer_frontend_control.c` builds semantic requests such as start-command and
  poll-completion. It should continue to call a private channel API rather than
  learn DOCA.
- `homer_queue_abi.h` already provides SPSC published-tail / consumed-head queue
  and byte-ring contracts. The DPU bridge should preserve this monotonic frontier
  model.
- `homer_completion_abi.h` already separates frontend client completion mailboxes
  from peer command-completion rings. DPU-produced host completions should map to
  those concepts.
- The scheduler is currently state-machine aware. Collectors discover or refresh
  facts; machines represent async work already known to the service; action
  grants execute bounded nonblocking work and report feedback.

Therefore, the first DPU migration should replace the host-service IPC boundary,
not the service's high-level command/session/payload semantics.

The exception is PostgreSQL-local lifecycle setup. The current host service owns
mailbox creation in `TupleSinkServiceEnsureSessionMailboxes()` at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18033`
and backend spawn in `TupleSinkServiceSubmitBackendSpawnRequest()` at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18188`.
The postmaster side creates the spawn region and launches backends in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1638`
and
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2792`.
Those operations must move to a selected-DPU host lifecycle shim because the DPU
cannot perform them through DMA. The resulting backend command/completion
mailboxes, grouped control block, and frontend request/response slots are still
exported to the DPU and used by the DPU DMA hot path.

## Non-goals for this phase

Do not do these during the DPU migration:

- no async-continuation scheduler;
- no durable ready catalog / continuation graph rewrite;
- no DPA-kernel port;
- no peer RDMA redesign unless DPU userspace compilation exposes a hard blocker;
- no host-backend hot-path DOCA task submission;
- no one-DMA-read-per-tail polling design;
- no private DPU progress thread outside the Homer scheduler;
- no sync-event dependency as the authoritative hot-path publication mechanism.

Sync-event may be added later as an idle wakeup hint. The authoritative hot path
should be memory-resident frontiers plus DMA completion tracking.

## Bridge ABI: grouped control block

Add:

```text
src/include/distributed/homer/homer_dpu_bridge_abi.h
```

This header should use fixed-width C types only. It must not contain PostgreSQL
server types, DOCA pointers, service-private table pointers, or host-only object
names.

### Workload classes

Start with three workload classes:

```c
typedef enum HomerDpuBridgeWorkloadClass
{
    HOMER_DPU_BRIDGE_WORKLOAD_COMMAND = 1,
    HOMER_DPU_BRIDGE_WORKLOAD_COMPLETION = 2,
    HOMER_DPU_BRIDGE_WORKLOAD_BYTE_STREAM = 3
} HomerDpuBridgeWorkloadClass;
```

Use separate DMA contexts per class initially. Multiple rings/sessions can share
one class context for DOCA completion-report coalescing, but each ring still owns
its own semantic frontier and consumed head.

### Direction and state

```c
typedef enum HomerDpuBridgeRingDirection
{
    HOMER_DPU_BRIDGE_RING_HOST_TO_DPU = 1,
    HOMER_DPU_BRIDGE_RING_DPU_TO_HOST = 2,
    HOMER_DPU_BRIDGE_RING_CREDIT_ONLY = 3
} HomerDpuBridgeRingDirection;

typedef enum HomerDpuBridgeEntryState
{
    HOMER_DPU_BRIDGE_ENTRY_FREE = 0,
    HOMER_DPU_BRIDGE_ENTRY_SETUP = 1,
    HOMER_DPU_BRIDGE_ENTRY_ACTIVE = 2,
    HOMER_DPU_BRIDGE_ENTRY_DRAINING = 3,
    HOMER_DPU_BRIDGE_ENTRY_CLOSED = 4,
    HOMER_DPU_BRIDGE_ENTRY_FAILED = 5
} HomerDpuBridgeEntryState;
```

Every hot-path entry must include a generation. Stale DPU task callbacks after
teardown must be rejected by generation before they can publish host-visible
credit, completion, or error state.

### Owner-separated control lines

Use owner-separated arrays, not one interleaved mega-entry:

```text
HomerDpuBridgeControlBlock
  header
  hostPublishLines[ring_count]       host writes, DPU reads
  dpuCreditLines[ring_count]         DPU writes, host reads
  diagnostics[ring_count]            optional; not correctness-critical
```

This matches the hot operations: the DPU often wants to DMA-read only host-owned
publication lines, while host backends often want to read only DPU-owned credit or
completion lines.

### Publication-line snapshots

Each hot line should fit in one cache line. The current design decision is to
use a single monotonic publication word for the DPU migration hot path. There is
no known target case in the current byte-ring or slot design that requires a
begin/end sealed cache-line protocol.

The preferred hot-path protocol is a single monotonic publication word. Use this
when the publication word is itself the authoritative frontier/epoch, or when it
points to body bytes in slots that are not overwritten until the consumer has
returned credit. In that shape, the producer writes the body first and then
performs one final release/publish write:

```c
typedef struct __attribute__((aligned(64))) HomerDpuBridgeSimpleCreditLine
{
    uint64_t publishedCreditEpoch;       /* final publication word */
    uint64_t consumedHead;
    uint64_t completedTail;
    uint64_t generation;
    uint64_t flags;
    uint64_t errorCode;
    uint64_t reserved0;
    uint64_t reserved1;
} HomerDpuBridgeSimpleCreditLine;
```

For DPU-produced host-visible lines, submit body DMA tasks first and then submit
the one-word publication task immediately after them on the same ordered DMA
context. Do not wait for body completions before submitting the publish task in
this same-context path. The host polls `publishedCreditEpoch` or the equivalent
frontier word and accepts it when it matches the expected next value or advances
monotonically for that ring. If the host must read non-monotonic body fields from
a reusable control line, it should re-check the publication word after reading
the body. That double-read publication check is the first fallback before
introducing a heavier sealed line.

Keep the sealed begin/end sequence only as a fallback pattern for a future ABI
that needs a consistent multi-word snapshot of a reusable line while the producer
may start writing the next epoch before the consumer has finished reading the
current body. The current Homer DPU design should avoid that shape: byte-ring
contents live in credit-protected ring ranges, and fixed request/response slots
are not reused until the publication/credit protocol makes reuse legal.

If this fallback is ever introduced, add it as a separate ABI struct with static
asserts for size and alignment. The begin/end form is stricter and more
expensive than the simple publication-word form; it is not part of the planned
hot path and should not be implemented in the first migration stage.

For DPU-produced lines, do not assume a 64-byte DMA write is an atomic publish
operation. Treat body fields and the final accepted publication word as different
protocol roles:

```text
DPU writes body fields or completion/result slot bytes with DMA tasks.
DPU immediately submits the final publication-word DMA task after the body tasks
on the same ordered context.
Host accepts the body only after the publication word reaches the expected epoch
or frontier and generation/frontier checks pass.
```

Make same-context publication an invariant: body DMA tasks and the publication
DMA task for the corresponding frontier must be submitted on the same DOCA DMA
context, with `doca_dma_set_ordered_completions()` enabled. The publication task
should be submitted without `OPTIMIZE_REPORTS` and with
`DOCA_TASK_SUBMIT_FLAG_FLUSH`. Completion callbacks retire physical task state
later under scheduler-controlled PE-drain grants; they are separate from the
submission path. For this prototype, any DOCA DMA task error should be treated as
a fatal Homer service failure, not as a recoverable per-ring condition.

### Descriptor table

The descriptor table is setup-time metadata. Do not rewrite it in the hot path.

```c
typedef struct HomerDpuBridgeRingDescriptor
{
    uint32_t protocolVersion;
    uint32_t descriptorBytes;
    uint32_t ringIndex;
    uint32_t ringId;

    uint32_t workloadClass;
    uint32_t direction;
    uint32_t flags;
    uint32_t recordGeometry;

    uint64_t generation;
    uint64_t hostControlOffset;
    uint64_t hostRingAddress;
    uint64_t ringBytes;

    uint32_t slotCount;
    uint32_t slotBytes;
    uint32_t controlBytes;
    uint32_t hostRingOffset;

    uint64_t serviceSessionId;
    uint64_t serviceSinkId;
    uint64_t firstRecordOrdinal;
    uint64_t mmapExportId;
} HomerDpuBridgeRingDescriptor;
```

Use `hostRingAddress` only as the address token required to construct DOCA buffers
against the imported mmap. The code must still validate identity through
`ringIndex`, `ringId`, `generation`, `serviceSessionId`, and `serviceSinkId`.

## Host frontend DMA channel

Add:

```text
src/backend/distributed/utils/homer/homer_frontend_dma.c
src/backend/distributed/utils/homer/homer_frontend_dma.h
```

This should be a private channel implementation beside `homer_frontend_shm.c`.
It should not replace semantic request construction in `homer_frontend_control.c`.

Responsibilities:

1. Allocate or attach long-lived host memory for the bridge control block,
   command/control slots or rings, completion mailboxes, and byte-stream rings.
2. Register those host regions with DOCA mmap and export descriptors for DPU PCI
   access.
3. Exchange mmap exports, bridge descriptors, generations, and optional wakeup
   handles with the DPU service over the TCP setup socket during setup.
4. Write backend-produced request records and payload bytes into host memory.
5. Publish host-owned frontiers through `HomerDpuBridgeHostPublishLine`.
6. Read DPU-owned credit/completion lines with acquire-side validation.
7. Keep `OpenRemoteExecutionSession()`, `StartRemoteExecutionCommand()`, and
   `PollRemoteExecutionCommandCompletion()` stable for ordinary callers.

Decision: the host-side TCP setup client belongs in `homer_frontend_dma.c` for
this phase. It is part of the DMA channel setup path: it registers/exports host
mmap state, sends descriptor bytes to the DPU service, waits for the setup ack,
and then lets the hot path publish frontiers through DMA-visible memory. Because
this is cold-path setup, it may block while waiting for TCP connection/setup
completion, but it must use a timeout and emit explicit diagnostics rather than
spinning indefinitely.

The first command-plane implementation may preserve the current fixed control
slot request/response union as the command record body. That is less invasive
than inventing a new semantic command ring immediately. The bridge then uses a
DPU-visible publication line or bitmap to tell the DPU which slots need pulling.
A later cleanup can convert the control plane to per-session SPSC command rings
if measurements justify it.

## DPU DMA engine

Add:

```text
src/backend/distributed/utils/homer/homer_service_dpu_dma.c
src/backend/distributed/utils/homer/homer_service_dpu_dma.h
src/backend/distributed/utils/homer/homer_service_dpu_setup_tcp.c
src/backend/distributed/utils/homer/homer_service_dpu_setup_tcp.h
```

The engine owns DOCA objects and physical movement state. It exposes facts and
bounded actions to the existing scheduler.

Decision: keep new DPU implementation work out of
`tuple_sink_service_process.c` where the current scheduler-local type boundary
allows it. The large file should remain a thin scheduler adapter: it owns current
`HomerGrantVector`, `HomerProgressResult`, collector/action enums, and dispatch
from `HomerServiceExecuteDpuDmaAction()`. Real TCP setup listener lifecycle and
message handling should live in `homer_service_dpu_setup_tcp.c/.h`; real DMA
lifecycle, descriptor import, buffer/task ownership, PE drain, and callbacks
should stay in `homer_service_dpu_dma.c/.h`. Do not extract the scheduler-local
types only to move code out; do that later if the adapter boundary becomes the
limiting factor.

### Minimum state

```c
typedef struct HomerDpuDmaRingState
{
    uint32_t ringIndex;
    uint32_t workloadClass;
    uint32_t direction;
    uint32_t flags;

    uint64_t generation;
    uint64_t observedPublishedTail;
    uint64_t submittedTail;
    uint64_t completedContiguousTail;
    uint64_t semanticVisibleTail;
    uint64_t consumedHeadPublishedToHost;

    uint32_t inflightTaskCount;
    uint32_t readyCatalogMember;
    uint32_t blockedOnTaskPool;
    uint32_t blockedOnHostCredit;
} HomerDpuDmaRingState;

typedef struct HomerDpuDmaClassState
{
    uint32_t workloadClass;
    struct doca_dma *dma;
    struct doca_ctx *ctx;

    uint32_t asyncWindow;
    uint32_t taskPoolSize;
    uint32_t freeTaskCount;
    uint32_t inflightTaskCount;

    uint32_t discoveredReadyRingCount;
    uint32_t consumedHeadPublishPendingCount;
    uint32_t completionPushQueueDepth;

    uint64_t lastControlPollPass;
    uint64_t controlPollIntervalPasses;
    uint32_t controlReadInFlight;
} HomerDpuDmaClassState;
```

`readyCatalogMember` here means an engine-local ready bit/list for rings with
observed work. It is not the future continuation ready catalog.

The service should own one `HomerDpuDmaEngine *` in a long-lived service runtime
object, not as a local variable inside the pump. Today's
[`TupleSinkServiceControlState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1856) is mostly the current SHM control-region wrapper, so the cleaner target is a
new broader service runtime object that contains the control state plus the DPU
engine and is passed down to [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36127). A first scaffolding patch may pass an explicit `HomerDpuDmaEngine *` beside
the existing arguments to avoid a broad runtime-struct refactor. Do not use a
file-static singleton except as a temporary bring-up stub; a singleton makes
multi-service tests and restart/teardown generation checks harder.

### Task slots and owner records

Every submitted DOCA task must come from a bounded preallocated task slot. The
slot owns the DOCA task handle, source/destination buffers where applicable, and
the metadata that lets the callback interpret the physical completion. Do not
allocate owner records on the hot path.

The shape should be one owner record per task slot:

```c
typedef enum HomerDpuDmaTaskSlotState
{
    HOMER_DPU_DMA_TASK_SLOT_FREE = 0,
    HOMER_DPU_DMA_TASK_SLOT_SUBMITTED,
    HOMER_DPU_DMA_TASK_SLOT_COMPLETED,
    HOMER_DPU_DMA_TASK_SLOT_FAILED
} HomerDpuDmaTaskSlotState;

typedef struct HomerDpuDmaGroupedControlOwner
{
    uint32_t controlFirstIndex;
    uint32_t controlCount;
    uint32_t localBufferIndex;
    uint32_t localBufferBytes;
} HomerDpuDmaGroupedControlOwner;

typedef struct HomerDpuDmaTaskOwner
{
    uint32_t taskKind;
    uint32_t workloadClass;
    uint32_t taskSlotIndex;
    uint32_t ringIndex;
    uint64_t taskGeneration;
    uint64_t ringGeneration;
    uint64_t bridgeGeneration;
    union
    {
        HomerDpuDmaGroupedControlOwner groupedControl;
        /* Later stages add slot and byte-ring offset/range owners here. */
    } u;
} HomerDpuDmaTaskOwner;

typedef struct HomerDpuDmaTaskSlot
{
    struct doca_dma_task_memcpy *task;
    struct doca_buf *srcBuf;
    struct doca_buf *dstBuf;
    HomerDpuDmaTaskOwner owner;
    uint64_t slotGeneration;
    HomerDpuDmaTaskSlotState state;
} HomerDpuDmaTaskSlot;
```

The callback recovers the task slot through DOCA task user data. `owner` is the
completion identity: it tells the callback which task slot can be reused, which
workload class owns the completion, which ring/control slice or future byte-ring
range it belongs to, which local staging buffer contains copied bytes, and which
retirement path should run next.

Generation fields have specific roles:

- `taskGeneration`/`slotGeneration` protects task-slot reuse. A mismatch should
  be treated as a fatal service bug because DOCA should not callback a task after
  the slot was returned and reused.
- `ringGeneration` protects logical ring/mailbox reuse. A callback submitted for
  an old ring generation after teardown or reinitialization is a legitimate
  lifecycle race; recycle the task and count it diagnostically, but do not mark
  DPU-local readiness or publish host-visible state.
- `bridgeGeneration` protects wholesale bridge/control-block replacement. A
  mismatch is also a lifecycle race if teardown is in progress; otherwise it is
  suspicious enough to fail fast in debug builds.

For Stage 5 grouped-control reads, do not add payload/slot-specific owner fields
yet. The required metadata is the local buffer slice plus the first control-line
index and count. Later payload/slot stages add `slotIndex`, `byteOffset`,
`byteLength`, `firstTail`, `lastTail`, and related retirement fields.

### Engine API

Use action-specific APIs rather than one generic progress function:

```c
bool HomerDpuDmaGetSchedulerFacts(HomerDpuDmaEngine *engine,
                                  HomerDpuDmaSchedulerFacts *facts);

bool HomerDpuDmaSubmitControlReads(HomerDpuDmaEngine *engine,
                                   const HomerGrantVector *grantVector,
                                   const HomerProgressSourceRef *source,
                                   HomerProgressResult *result);

bool HomerDpuDmaDrainPe(HomerDpuDmaEngine *engine,
                        const HomerGrantVector *grantVector,
                        const HomerProgressSourceRef *source,
                        HomerProgressResult *result);

bool HomerDpuDmaSubmitCommandPulls(HomerDpuDmaEngine *engine,
                                   const HomerGrantVector *grantVector,
                                   const HomerProgressSourceRef *source,
                                   HomerProgressResult *result);

bool HomerDpuDmaSubmitPayloadPulls(HomerDpuDmaEngine *engine,
                                   const HomerGrantVector *grantVector,
                                   const HomerProgressSourceRef *source,
                                   HomerProgressResult *result);

bool HomerDpuDmaSubmitCompletionPushes(HomerDpuDmaEngine *engine,
                                       const HomerGrantVector *grantVector,
                                       const HomerProgressSourceRef *source,
                                       HomerProgressResult *result);

bool HomerDpuDmaPublishConsumedHeads(HomerDpuDmaEngine *engine,
                                     const HomerGrantVector *grantVector,
                                     const HomerProgressSourceRef *source,
                                     HomerProgressResult *result);
```

`HomerDpuDmaDrainPe()` is the normal place where `doca_pe_progress()` is called.
Submit actions should normally submit work and return. If a prototype combines
submit and PE progress for bring-up, the inline progress must consume an explicit
poll budget and report it in `HomerProgressResult`.

Callbacks must not call `doca_pe_progress()` recursively.

### PE callback usage

DOCA DMA callbacks are task-retirement hooks, not a second Homer scheduler. The
local headers make this explicit:

- `doca_dma_task_memcpy_set_conf()` installs the memcpy task completion and error
  callbacks before `doca_ctx_start()`.
- `doca_pe_progress()` is the API that invokes callbacks; it returns `1` when it
  made progress and `0` otherwise.
- `doca_task_submit_ex()` accepts `DOCA_TASK_SUBMIT_FLAG_FLUSH` and
  `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS`. `FLUSH` asks the context to flush
  this and earlier tasks to hardware; `OPTIMIZE_REPORTS` allows completion
  callbacks to be deferred until a later non-optimized task/report boundary.
- The callback documentation says ownership of the task returns to the user
  inside the callback, and callbacks must not call `doca_pe_progress()`.

Use callbacks for only these operations:

```text
1. Recover HomerDpuDmaTaskOwner from task_user_data.
2. Check taskKind, workloadClass, taskSlotIndex, taskGeneration/slotGeneration,
   and the relevant ring/bridge generation fields before touching logical state.
3. On expected lifecycle staleness, such as old ringGeneration after teardown,
   recycle the task owner and count a stale-lifecycle callback without publishing
   readiness or host-visible state.
4. On impossible owner mismatches, task-slot generation mismatch, malformed
   owner fields, non-monotonic frontiers, or unexpected generation mismatch
   outside teardown, record diagnostics and request fatal Homer service shutdown.
5. On error callback, record diagnostics and request fatal Homer service
   shutdown. The first DPU prototype treats DMA task failure as a service bug or
   unrecoverable hardware/runtime failure, not as a recoverable per-ring event.
6. On successful data task, mark the physical DMA range complete in per-ring
   completion state and advance completedContiguousTail if possible.
7. On successful grouped-control read, validate publication words, generations,
   and monotonic frontiers in the local staging buffer, then set DPU-local ready
   bits/counts for rings with new frontiers.
8. On successful publication task, mark the host-visible frontier as published.
9. Return the task, buffers, and owner record to reusable pools.
```

Callbacks should not dispatch semantic command handlers, submit new DMA work,
run TCP setup, call scheduler planning code, or spin on other completions. If a
callback discovers follow-on work, it records maintained facts such as
`discoveredReadyRingCount`, `completionPushQueueDepth`,
`consumedHeadPublishPendingCount`, or failure bits. The next bounded scheduler
grant chooses whether to spend CPU on that work.

## Current scheduler integration

### Add scheduler identities

Add DPU DMA collector kinds:

```text
HOMER_PROGRESS_COLLECTOR_DPU_DMA_PE
HOMER_PROGRESS_COLLECTOR_DPU_DMA_GROUPED_CONTROL
HOMER_PROGRESS_COLLECTOR_DPU_DMA_CREDIT_PUBLICATION
HOMER_PROGRESS_COLLECTOR_DPU_DMA_COMPLETION_PUBLICATION
```

Add DPU DMA action kinds:

```text
HOMER_PROGRESS_ACTION_DPU_DMA_DRAIN_PE
HOMER_PROGRESS_ACTION_DPU_DMA_SUBMIT_CONTROL_READS
HOMER_PROGRESS_ACTION_DPU_DMA_SUBMIT_COMMAND_PULLS
HOMER_PROGRESS_ACTION_DPU_DMA_SUBMIT_PAYLOAD_PULLS
HOMER_PROGRESS_ACTION_DPU_DMA_SUBMIT_COMPLETION_PUSHES
HOMER_PROGRESS_ACTION_DPU_DMA_PUBLISH_CONSUMED_HEADS
HOMER_PROGRESS_ACTION_DPU_DMA_ADVANCE_SETUP
HOMER_PROGRESS_ACTION_DPU_DMA_ADVANCE_TEARDOWN
```

Add a DPU machine kind only if the current policy needs one to represent
known ring-class work after grouped-control discovery. It is acceptable in the
first implementation to keep DPU DMA as collector/action work while existing
command, completion, payload, and peer machines remain the semantic layer.

### Candidate-building rules

Ready-set building must be cheap. It may call `HomerDpuDmaGetSchedulerFacts()`
and inspect DPU-local counters. It must not call DOCA and must not DMA-read host
memory.

Use these interpretations:

```text
PE drain:
  known expected work if inflightTaskCount[class] > 0

Grouped-control poll:
  due-only collector work if controlPollDue[class]
  dependency-demanded collector work if an existing machine is waiting for DPU
  host-frontier discovery

Command/payload pull:
  known expected work if discoveredReadyRingCount[class] > 0
  and freeTaskCount[class] > 0
  and class window has room

Completion push:
  known expected work if completionPushQueueDepth > 0
  and freeTaskCount[completion] > 0

Consumed-head publication:
  known expected work if consumedHeadPublishPendingCount[class] > 0
  and freeTaskCount[class] > 0

Task-pool or host-credit pressure:
  blocked fact, not ready work
```

This mirrors the existing distinction among known expected work, known blocked
work, and unknown external arrivals. A periodic grouped-control poll is like a
blind collector: useful and necessary, but not proof that work exists until it
returns a new frontier.

### DPU-local ready-ref queues

The counter facts above are the right interface for ready-set construction, but
they are not enough for the bounded action executor. A counter can make a DPU DMA
collector ready, but it does not tell the executor which imported ring or mailbox
to service. The DMA engine therefore needs fixed-size DPU-local ready-ref queues
in addition to the counters.

The ready-ref queues are internal scheduler facts, not a host-DPU ABI and not a
replacement for the grouped control block. A ready ref should identify the DMA
object by at least `{importIndex, ringIndex}`. `ringIndex` alone is not globally
unique because it is scoped to one imported descriptor table. When useful, carry
a generation or accepted epoch so stale queue entries can be rejected cheaply,
but still revalidate the live descriptor and ring runtime before submitting DMA.

Use separate queues by workload and direction:

```text
host_to_dpu_frontend_command_ready       frontend command/control records to DMA-read
host_to_dpu_backend_completion_ready     backend completion mailbox records to DMA-read
host_to_dpu_payload_ready                byte-ring/basebackup ranges to DMA-read
dpu_to_host_backend_command_publish      backend command mailbox records to DMA-write
dpu_to_host_frontend_response_publish    frontend response/completion records to DMA-write
dpu_to_host_credit_publish               consumed-head/credit-line records to DMA-write
```

Host-to-DPU queues are filled by discovery paths such as grouped-control or
mailbox-control DMA reads after a host-published frontier is accepted. DPU-to-host
queues are filled by local service workflow state when the DPU has semantic work
ready to publish to host memory. Per-ring runtime flags such as `discoveredReady`
should suppress duplicate refs, while per-class counters remain the cheap
ready-set predicate.

Action executors should pop refs under scheduler grants, submit DMA for the
validated refs, and leave the queue intact when a grant is exhausted. Stale refs
from teardown, bounded grants, or already-consumed frontiers are normal scheduler
state and should be cleared with diagnostics; descriptor identity or generation
violations remain fatal prototype bugs.

This is also the right mental model for RDMA-side Homer work discovery. RDMA CQ
drains, remote doorbells, and send/write completions discover exact service work
and update local facts; ready-set construction should then read only cheap local
counters, while bounded grants consume exact ready objects. DPU DMA differs in
the discovery mechanism, not in scheduler ownership: grouped-control reads,
DOCA callbacks, and DPU-local workflow queues play the same role as CQ/doorbell
drains on the RDMA side.

### Grant model

Decision: DPU DMA actions should use [`HomerGrantVector`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2155) as their authoritative
budget. `HomerProgressGrant` remains relevant only because the current scheduler
still stores source identity and legacy source-local limits there.

There are two grant records in the current scheduler:

- [`HomerProgressGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2506) is the source-local grant stored in every action. It carries `maxPolls`,
  `maxItems`, `maxPumpCalls`, and `flags`. This is the right place for PE poll
  calls, grouped-control slices, ring inspections, and small control-object
  counts.
- [`HomerGrantVector`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2155) is the compiled action's richer budget vector. It carries `maxObjects`,
  `maxWrs`, and `maxBytes`, which the DPU DMA engine needs for task-submission
  and byte movement limits.

So DPU submit actions should receive the compiled action grant, or at least the
`HomerGrantVector` plus a source ref for identity. Do not design new DPU
executors around `HomerProgressGrant` as the primary budget carrier:

```c
bool HomerDpuDmaSubmitPayloadPulls(HomerDpuDmaEngine *engine,
                                   const HomerGrantVector *grantVector,
                                   const HomerProgressSourceRef *source,
                                   HomerProgressResult *result);
```

`HomerProgressResult` remains the unified result type. The distinction is only
that the current scheduler has a source-local grant plus a richer compiled vector
rather than a single struct that contains every budget dimension. The DPU path
should help finish the migration toward vector-based budgeting instead of adding
new dependencies on the older grant shape.

### Planning policy

The default plan order should be:

1. Drain PE when in-flight completions exist.
2. Publish consumed heads or completion frontiers whose data DMA is complete.
3. Submit grouped-control reads when due or demanded.
4. Submit command/control pulls with a small latency-oriented budget.
5. Submit completion pushes that unblock host backends.
6. Submit payload/byte-stream pulls with remaining data-plane budget.
7. Drain PE again only if the policy explicitly grants another PE action.

This is a policy default, not a correctness dependency. Correctness comes from
per-ring frontiers, task-owner generation checks, and completion-gated
publication.

### Physical coalescing is allowed

Do not create one scheduler action per session. Keep scheduler facts split, but
allow a physical action to walk a DPU-local ready bitset for one class and submit
several bounded DMA tasks. This matches the current peer-control lesson: split
scheduler facts do not require repeated physical scans.

For example, one `DPU_DMA_SUBMIT_COMMAND_PULLS` grant may inspect up to
`maxRings` ready command rings and submit up to `maxDmaTasks` total tasks. It
reports `itemsProcessed`, `wrsPosted`, `bytesPosted`, `stillReady`, and
`budgetExhausted` so feedback remains meaningful.

### Grant budgets

Add DPU-specific grant fields only if the existing grant payload cannot express
them. Minimum budgets:

```text
maxPeProgressCalls
maxControlReadSlices
maxControlEntriesInspected
maxRingsInspected
maxDmaTasksSubmitted
maxBytesSubmitted
maxTaskCallbacksRetired
maxConsumedHeadPublishes
maxCompletionPublishes
```

Every DPU action must return when its grant is exhausted, when it observes no
progress, or when it cannot submit because a real dependency is blocked. It must
not spin waiting for a particular DMA task or frontier.

### TCP setup and mmap import lifecycle

The TCP setup socket is cold/control-plane setup for this phase, not a hot-path
publication primitive. It should carry descriptors and lifecycle acks; data
movement and frontier publication stay in DMA-visible memory. This replaces the
earlier COMCH plan because COMCH currently fails at the stock DOCA sample level
on farnet1, while DOCA DMA itself still validates independently.

The DPU Homer service is the TCP listener. The host/frontend DMA channel is the
TCP client. This matches the deployment shape where the DPU service is the
long-lived endpoint and host backends/frontends initiate bridge setup for their
exported memory.

Initial setup may block because it is outside the workload hot path. Blocking is
allowed only in explicit setup/teardown routines; ready-set construction,
scheduled DMA submit actions, PE-drain actions, and callbacks must stay bounded
and nonblocking.

Minimal setup message ABI:

```text
struct HomerDpuComchSetupHeader {
    uint32_t protocolVersion;
    uint32_t messageKind;        // SETUP, SETUP_ACK, CLOSE, CLOSE_ACK
    uint32_t headerBytes;
    uint32_t flags;
    uint64_t bridgeGeneration;
    uint32_t featureFlags;
    uint32_t ringCount;
    uint32_t descriptorBytes;
    uint32_t mmapExportBytes;
    uint32_t reserved0;
};

payload for SETUP:
    mmap export blob bytes
    HomerDpuBridgeControlBlockHeader
    HomerDpuBridgeRingDescriptor[ringCount]

payload for SETUP_ACK:
    accepted bridgeGeneration
    accepted featureFlags
    status/error code
    rejected ring index or HOMER_DPU_BRIDGE_INVALID_RING_INDEX
```

The exact C struct can be adjusted while implementing, and the current
`Comch`-named setup structs may be preserved temporarily to avoid churn. The
transport, however, is TCP. The required content is fixed:
protocol/versioning, message kind, bridge generation, feature flags, ring count,
descriptor size, mmap export length, exported mmap blob, bridge header,
descriptor table, and an ack/error result. The DPU TCP setup handler should pass
the received export blob to `HomerDpuDmaImportHostMmapDescriptor()` and validate
the bridge header/descriptors before accepting rings.

Initial setup sequence:

```text
1. Host frontend allocates long-lived bridge, command, completion, and payload
   memory with cache-line alignment required by homer_dpu_bridge_abi.h.
2. Host creates a doca_mmap, sets the memory range and PCI read/write
   permissions, adds the local device, starts the mmap, and exports it with
   doca_mmap_export_pci().
3. Host opens the TCP setup connection to the DPU service and sends:
   protocol version, feature flags, exported mmap blob, descriptor table,
   initial generation, ring count, and optional idle-wakeup handles.
4. DPU service receives the TCP setup message, imports the mmap with
   doca_mmap_create_from_export(), validates protocol/descriptor geometry, and
   creates DPU-local ring/class state in SETUP.
5. DPU creates or reuses class DMA contexts, attaches them to the PE, configures
   task pools/callbacks, starts contexts, and marks descriptors ACTIVE only after
   all buffers/task-owner pools needed for the accepted rings exist.
6. DPU sends a TCP setup ack containing accepted generation, feature bits,
   negotiated class windows, and any rejected ring descriptors.
7. Host publishes ring frontiers only after setup ack. Until then the DPU must
   ignore hot-path lines for that generation.
```

Teardown sequence:

```text
1. Host publishes DRAINING/CLOSED in the owner line and sends a TCP close if
   the connection is still alive.
2. DPU stops submitting new DMA tasks for that generation, drains or invalidates
   in-flight tasks through scheduled PE-drain grants, and prevents stale
   callbacks from publishing host-visible state.
3. DPU publishes final consumed/error state if the host memory is still valid and
   sends a TCP close ack.
4. Host waits for close ack or whole-service generation reset before unmapping,
   stopping the mmap, or reusing the address range for a new generation.
5. If the TCP setup connection dies, both sides advance service/ring generation and rely on stale
   task-owner generation checks to prevent old DMA callbacks from publishing into
   a reused semantic object.
```

The DOCA mmap headers state that `doca_mmap_export_pci()` requires a started
mmap with PCI permission, and that stopping the mmap invalidates imported mmaps.
That is why the host must not stop/unregister exported memory until the DPU has
acknowledged teardown or the whole service generation is being abandoned.

### Result accounting

Map DPU results into `HomerProgressResult` consistently:

```text
emptyPolls:
  PE returned 0; control read found no usable frontier; ready ring was stale

cqesDrained:
  DOCA task completions retired by PE progress

itemsProcessed:
  control entries inspected, rings advanced, command records staged, completion
  records published, or consumed-head lines written

wrsPosted:
  DOCA DMA tasks submitted

bytesPosted:
  bytes scheduled for DMA read/write

transportCompletionFrontier:
  physical DMA completed contiguous frontier

sourceReleaseFrontier:
  host-visible consumed-head/credit frontier

semanticObjectFrontier:
  command/completion object made visible to the existing Homer semantic machine

stillReady:
  DPU-local facts say more work remains after this bounded grant

budgetExhausted:
  stopped on grant, class window, task pool, or byte budget
```

## Workload-specific design

### Command/control path

For the first implementation, keep the existing request/response union as the
semantic record. The host frontend writes a complete request slot in registered
host memory and publishes a bridge frontier/ready bit. The DPU service then:

1. DMA-reads the grouped control line or ready-slot slice.
2. DMA-reads the full request slot into DPU-local staging.
3. Validates slot state, request sequence, owner pid/session id, and generation.
4. Runs the existing service-side control handler against the staged request.
5. Submits response-body DMA writes followed immediately by the response
   publication-word DMA write on the same ordered DMA context.
6. Host observes the response only after the publication word reaches the
   expected epoch/frontier.
7. Publishes credit/free state only after the host can safely reuse the slot.

This requires refactoring service-side local-control handling so it can process a
staged request/response object rather than assuming direct host-process SHM
pointer access. Keep this as a narrow adapter; do not rewrite command semantics.

Later, after the DPU path is stable, the command plane may move from fixed
multi-producer slots to per-session SPSC command rings. That is a performance and
cleanup decision, not a prerequisite for the migration.

### Completion/result push

DPU-produced host completions follow the same two-step publication rule as the
validated DOCA harness:

1. DMA-write completion/result slot bytes into host memory.
2. Submit the ready epoch/frontier or DPU credit-line publication word
   immediately after the body writes on the same ordered DMA context.
3. Host frontend consumes only slots at or below the published epoch/frontier.

Do not publish a ready epoch from a different DMA context unless it is explicitly
gated on the data context's completed frontier.

### Payload and basebackup byte streams

Use the existing byte-ring absolute frontier contract. The host backend writes
complete transport records, then release-publishes `publishedTail` in the bridge
line. The DPU engine:

1. Discovers `publishedTail` through grouped-control DMA reads.
2. Submits DMA reads only for complete records at or below the observed tail.
3. Tracks per-record or per-range task owners.
4. Advances `completedContiguousTail` only from completed DMA callbacks.
5. Makes records visible to the existing payload/peer transport path only after
   the contiguous frontier is complete.
6. Publishes `consumedHead` back to the host only after records are safe to
   release.

A basebackup pump should remain one bounded class/ring action that can issue many
DMA tasks under one scheduler grant. Do not create one scheduler-visible item per
DMA task.

## Correctness validation gates

Do not wait for a full runnable Homer path before validating correctness. Each
stage below should add an executable check that exercises the invariant it
introduces. The validation ladder is:

1. **ABI/protocol validation**: host-only compile or unit-style checks for struct
   sizes, cache-line alignment, publication-word monotonicity, generation
   rejection, frontier monotonicity, byte-ring wrap arithmetic, and slot reuse
   only after credit.
2. **DOCA engine standalone validation**: a small host+DPU harness using the same
   `HomerDpuDmaEngine` internals, before real Homer command semantics. It should
   allocate/export host memory, import it on the DPU, DMA-read grouped control,
   DMA-pull synthetic records, DMA-write synthetic credit/completion lines, and
   verify same-context body+publish ordering.
3. **Scheduler skeleton validation**: DPU mode off must preserve current plans;
   DPU mode on with no work must produce bounded empty work and backoff; ready-set
   building must inspect only maintained DPU-local facts; submit actions must not
   call `doca_pe_progress()`.
4. **Thin vertical slices**: bring up grouped-control observation, request-slot
   pull, response push, one single-session command round trip, synthetic
   byte-ring pull, and only then full pgbench/basebackup workloads.

Add a debug-only invariant mode early. It should assert same DMA context for
body+publish, no PE progress from submit actions, no stale-generation
publication, no DMA beyond accepted frontiers, no slot reuse before credit, and
fatal shutdown on any DMA task error. This mode can be slow and noisy; it is for
bring-up correctness, not performance.

## Implementation sequence

Promotion is stage-scoped, not a separate cleanup track. When a stage first makes
a selected-DPU behavior meaningful, that same stage must replace the
corresponding experimental guard, placeholder, or old host-process SHM
dependency and must add acceptance evidence for the replacement. Later stages
should not inherit a vague "remove fallback before production" TODO. Instead,
any remaining promotion work belongs directly in the stage that owns the
behavior: setup in Stage 6, command-pull mechanism in Stage 7, response
publication mechanism in Stage 8A, selected-DPU command-path promotion in Stage
8B, payload/basebackup pull in Stage 9, lifecycle/reconnect in Stage 10,
user-facing selector flip in Stage 11, and old non-DPU fallback removal in
Stage 12.

### Stage 0 — Freeze the migration contract

Deliverable: this document plus a short note in the original DPU plan linking to
it.

Tasks:

- Record that the async-continuation scheduler is deferred.
- Record that the DPU DMA integration target is today's `machine-baseline`
  collectors/actions.
- Keep SHM as the default DPU-off runtime path only while no selected DPU path is
  runnable.
- Record the promotion rule that later stages carry directly: every selected
  DPU-mode guard, placeholder, or old host-process dependency must be replaced
  in the stage that first needs that behavior, not saved for a final cleanup
  pass.

Acceptance:

- No behavior change.
- The plan explicitly distinguishes current-scheduler migration from future
  scheduler cleanup.
- Stages 6 through 11 each name the selected-DPU fallback or placeholder they
  must eliminate before that stage can be considered complete.
- Stages 6 through 11 each include acceptance evidence that distinguishes a real
  selected-DPU path from a DPU-off SHM comparison path.

### Stage 1 — Bridge ABI header

Deliverable: `homer_dpu_bridge_abi.h`.

Status: completed in `/data/dbcomm/citus-dbcomm` with
`src/include/distributed/homer/homer_dpu_bridge_abi.h`,
`src/bin/homer_dpu_bridge_abi_check.c`, and the `dpu-bridge-abi-check` Makefile
target. See the implementation checkpoint for validation evidence.

Tasks:

- Add protocol version, feature flags, workload class, direction, entry state,
  ring descriptor, control-block header, host-publish line, DPU-credit line, and
  constants.
- Add static asserts for cache-line size, alignment, and descriptor sizes.
- Add inline validation helpers for publication words, generations, and
  monotonic frontiers.
- Add comments that define owner, producer, and consumer for every field.

Acceptance:

- Builds with normal Citus/Homer targets.
- Header can be included from host frontend code and service/DPU code without
  PostgreSQL-only or DOCA-only type leakage.
- Unit or compile-only test rejects malformed line sizes if a field is added.
- Host-only protocol check covers publication-word expected value, stale
  generation rejection, monotonic frontier rejection, byte-ring wrap splitting,
  and slot reuse only after consumed-credit advancement.

### Stage 2 — Host frontend DMA skeleton

Deliverable: `homer_frontend_dma.c/.h` behind a build/runtime switch.

Status: completed in `/data/dbcomm/citus-dbcomm` with the hidden
`citus.enable_experimental_homer_dpu_frontend` switch, bridge-memory skeleton,
and `frontend-dma-smoke` validation target. Real DPU command execution remains
intentionally not implemented and fails explicitly instead of falling back to SHM
when selected.

Tasks:

- Add channel-selection plumbing while keeping SHM as the DPU-off path during
  migration.
- Allocate aligned bridge memory and placeholder command/completion/payload
  regions.
- Implement publication-word publish/read helpers.
- Add placeholder TCP setup hooks or explicit `not implemented` errors behind
  the DPU channel switch.
- Do not submit DMA tasks from backend processes.
- Treat the hidden experimental selector as temporary scaffolding: selecting DPU
  mode must never silently fall through to SHM after the guard accepts the mode.

Acceptance:

- SHM path remains unchanged and passes existing tests.
- DPU channel can be selected only in an explicit experimental mode.
- Experimental mode initializes and tears down host bridge memory without command
  execution.
- Host-only experimental-mode smoke can publish synthetic request/credit
  frontiers in bridge memory and verify that ordinary frontend API callers still
  fail with explicit `not implemented` errors before the DPU service is present.
- The selected DPU path fails before any SHM frontend mapping or local-service
  SHM call. Keeping SHM buildable here is mode coexistence, not DPU fallback.

### Stage 3 — DPU DMA engine skeleton

Deliverable: `homer_service_dpu_dma.c/.h` with DOCA object lifecycle and zero
work facts.

Status: completed as a no-DOCA lifecycle/facts skeleton in
`/data/dbcomm/citus-dbcomm`. The engine creates per-workload-class state,
reports zero-work scheduler facts, rejects `enableDoca=true` explicitly, links
into the standalone service binary, and is excluded from `citus.so`. The API
does not yet take `HomerGrantVector` or `HomerProgressResult` because those
types are still local to `tuple_sink_service_process.c`; Stage 4 must bridge
that scheduler-type boundary deliberately.

Tasks:

- Create/destroy engine object.
- Create one PE and one DMA context per workload class, or stubs if DOCA is not
  compiled in.
- Configure task pools and callback prototypes.
- Add per-class and per-ring state arrays.
- Add `HomerDpuDmaGetSchedulerFacts()` returning maintained facts.

Acceptance:

- Service builds without DOCA when DPU mode is off.
- DPU build creates/destroys engine without leaks.
- No private progress thread or busy loop exists.
- Standalone engine lifecycle smoke verifies one PE plus per-class DMA contexts,
  task pool sizing, callback installation, zero-work facts, and clean teardown on
  the DPU without registering Homer sessions.

### Stage 4 — Scheduler skeleton in `machine-baseline`

Deliverable: DPU collector/action identities and no-op bounded executors.

Status: completed as a current-scheduler adapter in
`/data/dbcomm/citus-dbcomm`. The service now has a fixed
`HOMER_PROGRESS_SOURCE_DPU_DMA`, DPU collector/action identities, an opt-in
`HOMER_SERVICE_ENABLE_DPU_DMA` service knob, DPU collector-candidate construction
from `HomerDpuDmaGetSchedulerFacts()`, and Stage 4 no-op DPU actions that report
empty grants through scheduler feedback. The DPU engine API still does not take
`HomerGrantVector` or `HomerProgressResult`; the adapter inside
`tuple_sink_service_process.c` owns that translation until real DMA actions make
the stable shared budget/result surface obvious.

Tasks:

- Extend current collector/action enums.
- Extend candidate-building to append DPU collectors/actions only when DPU mode is
  enabled.
- Add no-op executors that fill `HomerProgressResult` and report empty/productive
  feedback correctly.
- Add policy budget caps for DPU collector grants, or reuse existing collector
  budgets conservatively.
- Ensure grouped-control due-only polling counts against blind collector budget
  unless demanded by a known waiter.

Acceptance:

- With DPU mode off, generated plans stay on the existing SHM/RDMA path because
  no DPU engine is created and the DPU collector builder returns immediately.
- With DPU mode on and no work, grouped-control discovery is admitted as blind
  collector work and reports `emptyPolls` through the fixed DPU source feedback.
- Ready-set building performs no DOCA calls; it only reads maintained Stage 3
  engine facts.
- No-op DPU action executors are bounded by current collector grant fields,
  report `emptyPolls`, and never call `doca_pe_progress()`. Real byte/object/WR
  `HomerGrantVector` consumption remains a Stage 5+ implementation item once
  the actions submit actual DOCA DMA tasks.

### Stage 5 — DOCA/TCP grouped-control lifecycle

Deliverable: real DOCA DMA lifecycle objects, TCP setup lifecycle objects, and grouped-control task-pool
scaffolding, but no real grouped-control DMA submission yet.

Status: completed for the service-side DOCA lifecycle and descriptor-import
boundary in `/data/dbcomm/citus-dbcomm`. The implementation creates bounded
task-slot/owner arrays and local grouped-control staging buffers, can compile an
opt-in DOCA lifecycle smoke with `HOMER_DPU_DMA_WITH_DOCA`, creates one PE plus
per-class DMA contexts, and imports PCI mmap export descriptors through
`HomerDpuDmaImportHostMmapDescriptor()`. The production setup peer that carries
those descriptor bytes from the host/frontend into the DPU service is being
pivoted from COMCH to TCP; the validated code already consumes descriptor bytes
through a scheduler-neutral import API.

Implementation correction from the DOCA headers: preallocating
`doca_dma_task_memcpy` handles before descriptor import is not valid, because
`doca_dma_task_memcpy_alloc_init()` requires real source and destination
`doca_buf` objects. Stage 5 therefore preallocates bounded `HomerDpuDmaTaskSlot`
and `HomerDpuDmaTaskOwner` records plus local buffers. Stage 6 allocates or
arms concrete memcpy tasks once the imported host mmap can produce source
buffers and the local staging mmap can produce destination buffers.

Tasks:

- Enable the minimal real DOCA lifecycle behind `HOMER_SERVICE_ENABLE_DOCA_DMA=1`
  for grouped-control reads only. Payload pulls, completion pushes, consumed-head
  writes, and real grouped-control task submission remain stubbed.
- Keep Stage 5 as infrastructure only: it may introduce DOCA DMA and TCP setup objects,
  task-slot pools, and debug harnesses, but it must not claim any selected-DPU
  runtime behavior has been promoted yet.
- Exchange the host mmap descriptor over TCP for the real path. A file-based
  descriptor path may be kept only as a standalone debug harness, not as the
  Homer service path.
- Import host mmap descriptor on the DPU side.
- Allocate reusable local buffers for grouped-control slices.
- Preallocate a bounded pool of `HomerDpuDmaTaskSlot` records with one
  `HomerDpuDmaTaskOwner` per future DOCA memcpy task.
- Install callback functions, but keep callbacks unreachable from the scheduler
  until Stage 6 starts real submissions. Concrete memcpy task handles are created
  only after descriptor import provides remote source buffers.
- Keep `DPU_DMA_SUBMIT_CONTROL_READS` and `DPU_DMA_DRAIN_PE` scheduler actions
  bounded and nonblocking, but let them report empty/not-ready work until Stage 6
  connects real submit and PE-drain behavior.

Acceptance:

- `HOMER_SERVICE_ENABLE_DOCA_DMA=1` can create and destroy the minimal DOCA DMA and TCP setup
  lifecycle on the intended DPU environment without registering real Homer rings
  or submitting DMA tasks.
- Host mmap descriptor exchange/import succeeds in a synthetic setup, or fails
  with an explicit diagnostic before any scheduler action can submit work.
- Task-pool sizing, local buffer allocation, task-owner initialization, and clean
  teardown are validated by a lifecycle smoke.
- No payload DMA reads, completion DMA writes, consumed-head writes, or
  grouped-control DMA reads are submitted in this stage.
- The meaningful host/DPU grouped-control correctness test is intentionally
  deferred to Stage 6, because it requires bounded PE drain plus callback
  retirement to observe completions safely.
- Stage 5 acceptance is not promotion evidence for user-facing DPU mode. It only
  proves that the later stage-owned replacements have the DOCA DMA/TCP setup lifecycle
  substrate they need.

### Stage 6 — Grouped-control submit, PE drain, and task-owner retirement

Deliverable: scheduled grouped-control DMA reads of host-publish-line slices plus
bounded PE drain with real callback/frontier accounting.

Stage 6 is split into two ordered sub-stages:

1. **Stage 6A — TCP setup path**: implement minimal host/frontend TCP setup client
   and DPU-service TCP setup listener. The host sends the setup message and waits
   for ack with a timeout. The DPU receives descriptor bytes, calls
   `HomerDpuDmaImportHostMmapDescriptor()`, validates bridge header/descriptors,
   and marks accepted rings setup-ready. The DPU service starts independently and
   listens for host setup; host DB backends connect later when they initialize the
   DPU frontend path. No grouped-control DMA reads are submitted yet.
2. **Stage 6B — grouped-control DMA submit/drain**: allocate or arm concrete
   `doca_dma_task_memcpy` tasks after descriptor import provides remote source
   buffers and local staging destination buffers. Submit grouped-control reads
   under scheduler grants and retire completions through bounded PE-drain grants.

Implementation progress: Stage 6A.1 landed the setup-message ABI, host-side
setup-payload builder, service-side setup-payload validation/ack handler, and
DMA-engine mmap-import handoff in `/data/dbcomm/citus-dbcomm`. Stage 6A.2 added
a standalone real DOCA COMCH transport smoke for those setup bytes. On farnet1,
the working DPU server endpoint was local DOCA device `0000:03:00.0` with host
PF representor `0000:21:00.0`; the SF representor `0000:03:00.0` /
`en3f0pf0sf0` timed out and the host client aborted. Stage 6A.3 made the DPU
DMA engine open an explicitly configured local DOCA device instead of the first
DMA-capable device; the DPU default is `0000:03:00.0`, with
`HOMER_SERVICE_DOCA_DEV_PCI` as the service override. Stage 6A.4 then validated
the full standalone cold setup composition: the host exported a real PCI mmap
descriptor, sent it over COMCH, and the DPU server imported it through the
service DMA engine. The next Stage 6A slice should integrate this validated
COMCH lifecycle into service/frontend setup rather than using the standalone
smoke binary. Stage 6A.5 added a reusable service-side
`HomerServiceDpuComchServer` lifecycle API in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_comch.c`
and `.h`: it creates the DPU COMCH listener, exposes bounded PE progress,
reports maintained setup facts, routes received setup messages through
`HomerServiceDpuComchHandleSetupPayload()`, and uses per-send ack storage so
async COMCH sends cannot race on a shared ack buffer. It was validated by a
DPU-side lifecycle smoke with no host peer and by the existing real mmap
transport smoke. Production service startup still does not allocate this server
object, and the production host/frontend COMCH client remains pending. Stage
6A.6 then wired that server object into production service startup behind
`HOMER_SERVICE_ENABLE_DPU_DMA=1` plus `HOMER_SERVICE_ENABLE_DOCA_DMA=1`, added
the service-side COMCH env overrides `HOMER_SERVICE_DPU_COMCH_NAME`,
`HOMER_SERVICE_COMCH_DEV_PCI`, and `HOMER_SERVICE_COMCH_REP_PCI`, linked the
service binary with `doca-comch`, and progressed COMCH events only through the
bounded DPU `PE_DRAIN` scheduler action. Full production service runtime on the
DPU is still pending; the Stage 6A.6 validation is compile/link coverage plus
the Stage 6A.5 DPU lifecycle and real mmap transport runtime smokes. Stage
6A.7 removes the singleton imported-host-mmap assumption in the DPU DMA engine:
COMCH setup now records each imported mmap in a bounded table keyed by bridge
generation plus client instance ID, exposes import counts through scheduler
facts, rejects duplicate setup identity, and destroys every active imported mmap
during engine teardown. This is still setup/lifecycle work; it does not submit
grouped-control DMA reads. Stage 6A.8 then added the production
host/frontend-side COMCH setup client path in `homer_frontend_dma.c`: normal
non-DOCA frontend builds fail selected DPU setup explicitly, while DOCA-enabled
frontend extension builds can export the frontend bridge as a PCI mmap, send the
setup payload over COMCH, wait for ack with a timeout, and then stop at the
Stage 7 command-pull `not implemented` guard. The exact PostgreSQL backend
runtime invocation of that helper has been validated against the standalone
import-enabled DPU COMCH smoke server. Full DPU-resident
`citus_tuple_sink_service` runtime validation remains a later deployment gate.
Stage 6B.1 then added the first real grouped-control DMA submit/drain slice:
COMCH setup now copies the bridge header and descriptor table into the DPU DMA
import table, `HomerDpuDmaSubmitGroupedControlReads()` submits cache-line reads
under bounded scheduler grants, `HomerDpuDmaDrainPe()` runs bounded PE progress
and retires callbacks, and `HomerDpuDmaCopyGroupedControlSnapshot()` exposes a
snapshot only after the callback marks it valid. The current scheduler adapter
now calls those APIs for `HOMER_PROGRESS_ACTION_DPU_GROUPED_CONTROL_READ` and
`HOMER_PROGRESS_ACTION_DPU_PE_DRAIN`. A real farnet1 host-to-DPU validation
passed with the standalone COMCH transport smoke after adding
`doca_buf_set_data()` for the source `doca_buf`; without that call the DPU DMA
task submitted but completed with `Input/Output Operation Failed`.
Stage 6B.2 then added semantic ready-frontier acceptance for completed
grouped-control reads: the DPU ignores unchanged publication epochs, treats an
advanced epoch as the semantic gate, validates generation/ring identity and
monotonic `publishedTail`, and increments maintained ready-ring facts only for
rings with new accepted frontier.

Stage 6A COMCH work is now historical prototype evidence, not the production
setup direction. On June 28, 2026, stock DOCA COMCH failed on farnet1 below
Homer even after driver restart, DPU reset, machine power cycle, and BlueField
system reset, while the standalone DOCA DMA validation harness still passed both
host-produce/DPU-pull and DPU-push directions. The immediate Stage 6A repair is
therefore to replace the COMCH transport lifecycle with a regular TCP setup
socket carrying the same setup payload and ack bytes. Keep the setup-payload
builder/parser and DPU DMA import path; remove COMCH as an acceptance
requirement for future stages.

Tasks:

- Implement the minimal TCP setup message ABI and close/ack shell. The existing
  setup ABI and setup-ack struct may be reused initially even if their C names
  still contain `Comch`; later rename them to neutral setup names after the TCP
  path is stable.
- Integrate the DPU TCP setup listener so service startup creates the listener and then
  returns to normal service pumping. Done for service ownership and bounded
  scheduler PE progress in Stage 6A.6 for the COMCH prototype; the TCP listener
  is the immediate replacement and must be validated on the exact production
  service binary on the DPU. Startup must not
  wait indefinitely for a host DB backend to connect.
- Implement the host TCP setup client in `homer_frontend_dma.c`. It should export
  bridge memory as a PCI mmap, send setup over TCP, wait for ack with a timeout,
  and reject selected DPU mode explicitly in non-DOCA builds.
- Replace the current frontend smoke-and-error guard with a real DPU setup
  operation that either completes TCP/mmap setup or fails before any SHM mapping
  is attempted in a selected DPU session. Stage 6A.8 wires this operation before
  the Stage 7 command-pull `not implemented` error for the COMCH prototype; the
  TCP path should preserve the same selected-DPU guard behavior.
- Start the runtime promotion path here: after the DPU channel is selected, host
  frontend setup must attempt the real TCP/mmap setup against the independently
  running DPU service and then either mark the DPU bridge setup-ready or return a
  bounded DPU setup error. It must not reopen the SHM frontend path.
- Treat Stage 6 promotion work as the setup replacement gate: the selected-DPU
  setup path may still end at the Stage 7 command guard, but it must already use
  real TCP/mmap setup or fail with a bounded DPU setup error.
- Keep the DPU selector experimental in Stage 6, but make the selected path real
  enough to validate setup: the old bridge-memory smoke may remain as a local
  preflight check, but it must no longer be the terminal behavior for a selected
  DPU frontend build that has DOCA enabled.
- Add Stage 6 promotion evidence to the smoke/runtime output: selected DPU setup
  must report TCP connection, mmap export/import, bridge generation, accepted
  descriptor count, and the expected Stage 7 command guard. A run that reaches
  the guard through SHM setup is invalid.
- Implement the DPU TCP setup listener in `homer_service_dpu_setup_tcp.c/.h`,
  keeping `tuple_sink_service_process.c` as only a scheduler/lifecycle adapter.
  The old COMCH server code may remain temporarily as a prototype/reference, but
  it is not the production setup path.
- For the TCP setup path, keep receive buffers, parsed setup payload, and ack
  buffers owned until the blocking setup exchange returns or the bounded timeout
  fails. Unlike COMCH sends, this does not require DOCA send-completion callback
  ownership; it does require explicit message framing and short-read handling.
- Store imported host mmaps in a bounded engine table keyed by setup identity,
  not in a singleton. Done in Stage 6A.7 for bridge generation plus client
  instance ID. Later host/frontend setup should allocate one table entry per
  backend/frontend bridge setup and resolve grouped-control buffers through that
  entry.
- Implement task-owner allocation, generation validation, callback retirement,
  and task reuse for grouped-control DMA tasks after descriptor import. Done for
  the physical task/snapshot lifecycle in Stage 6B.1; semantic ready-frontier
  validation remains next.
- Submit control-read DMA tasks only under `DPU_DMA_SUBMIT_CONTROL_READS` grants.
  Done in Stage 6B.1 via `HOMER_PROGRESS_ACTION_DPU_GROUPED_CONTROL_READ`.
- Implement bounded `doca_pe_progress()` loops controlled by grant budget. Done
  for DMA engine PE drain in Stage 6B.1.
- Drain completions only under `DPU_DMA_DRAIN_PE` grants. Done for grouped
  control reads in Stage 6B.1.
- Validate task owner identity in callbacks. Done in Stage 6B.1. Publication
  words, generations, ring identity, entry state, and monotonic frontiers are
  validated when completed snapshots are converted into ready-ring facts. Done in
  Stage 6B.2.
- Populate DPU-local ready bits/counts for rings with new observed frontiers.
  Done in Stage 6B.2 for grouped-control discovery counts; Stage 7 must add the
  command-pull API that consumes those ready facts.
- Report `cqesDrained`, `emptyPolls`, `stillReady`, and `budgetExhausted`.
  Stage 6B.1 reports submitted/completed/empty work through the adapter; richer
  `stillReady`/`budgetExhausted` feedback should be preserved when ready facts
  are added.
- Prohibit nested PE progress from callbacks. Done for grouped-control DMA
  callbacks in Stage 6B.1.

Acceptance:

- Host/frontend TCP setup can send a real mmap export blob, bridge header, and
  ring descriptors to the DPU service and receive a setup ack before any hot-path
  frontier publication is used.
- DPU service startup succeeds and starts normal service pumping even when no host
  backend has connected yet.
- A selected DPU frontend path reaches a real TCP setup attempt, not the old
  smoke-only guard. Missing service, malformed setup, address/device mismatch,
  and timeout are reported as DPU setup failures before any SHM mapping or
  local-service SHM call.
- The selected DPU setup path has no SHM fallback branch. In non-DOCA builds it
  may fail with a compile-time capability error; in DOCA-enabled builds it must
  attempt TCP/mmap setup or return a bounded DPU setup error.
- Stage 6 is not accepted if selecting DPU mode can still complete frontend
  initialization by remapping the host-process SHM channel after the DPU setup
  guard has accepted the mode.
- Before Stage 6A is closed, the production PostgreSQL backend/frontend path must
  be exercised against an independently running DPU TCP setup listener, not only
  compiled or validated through the standalone host client. Full DPU-resident
  `citus_tuple_sink_service` runtime validation is required for acceptance.
- DPU TCP setup imports the host mmap via `HomerDpuDmaImportHostMmapDescriptor()`
  and rejects malformed protocol version, descriptor size, generation, or ring
  geometry with an explicit setup error.
- Multiple host/frontend setup imports can coexist in the DPU DMA engine. A
  duplicate bridge generation plus client instance ID is rejected before later
  DMA task ownership can become ambiguous.
- Synthetic host publisher can advance frontiers and DPU observes them without
  reading one tail at a time. This was proven in the Stage 6B COMCH smoke, but
  Stage 6A must revalidate the same grouped-control observation through the TCP
  setup path before accepting the production setup replacement.
- Drain action stops on first zero-progress return or budget exhaustion. Done for
  `HomerDpuDmaDrainPe()` in Stage 6B.1.
- In-flight tasks remain represented in DPU-local facts for later grants. Done
  for grouped-control in-flight counts in Stage 6B.1.
- Expected lifecycle staleness, such as old ring generation after teardown, is
  ignored and counted diagnostically.
- Stale generation callbacks do not publish any host-visible state.
- Bug-like validation failures, including task-slot generation mismatch,
  malformed owner metadata, unexpected generation mismatch, and non-monotonic
  frontiers, fail the DPU engine fatally instead of being silently ignored. Done
  for grouped-control semantic acceptance in Stage 6B.2.
- Unchanged publication words are counted as empty/no-new-work, not as errors.
  Stage 6B.2 implements this by ignoring body fields when `publishedEpoch` is
  unchanged; a targeted negative/stress smoke remains useful before performance
  tuning.
- Debug invariant mode proves submit actions do not call `doca_pe_progress()`.
  Callbacks may not recurse into PE progress or run semantic service logic. A
  narrow exception is allowed for latency-critical DMA-chain continuations: a
  completion callback may submit a bounded, pre-owned follow-on DMA task for the
  same chain, such as a host-visible publication word after a response-body DMA
  completion. Stage 6B.1 implements the stricter retire-only shape for
  grouped-control reads; response/completion publication needs the chained shape
  before selected-DPU command acceptance.
- A standalone host+DPU grouped-control test proves control-read DMA submission
  and callback retirement are separated: submit action queues reads, PE-drain
  action observes and validates them. The existing COMCH smoke is historical
  evidence; the accepted production path should use the TCP setup smoke or exact
  service/frontend path with the same DMA assertions.
- Stage 6 promotion evidence must include a selected-DPU setup trace from the
  host/frontend path through TCP/mmap import. The trace may still stop at the
  Stage 7 command guard, but it must prove that setup did not remap the
  host-process SHM frontend channel.

### Stage 8B — Selected-DPU command path promotion

Deliverable: promote the frontend command APIs from setup-only smoke to a real
selected-DPU command path. Command requests must be published into DPU-visible
host slots, pulled by DPU DMA, executed through the service scheduler, and
answered by DPU DMA response publication. The path still uses existing
`CitusRemoteExecControlSlot` request/response structs.

Implementation progress: Stage 7A validates the first command-pull slice in
`/data/dbcomm/citus-dbcomm`. The DPU DMA engine now consumes a Stage 6B
discovered-ready command ring, DMA-pulls one full
`CitusRemoteExecControlSlot` from `hostRingOffset` into DPU-local staging,
validates slot state, owner pid, request sequence, protocol version, and request
kind, clears the ready fact after staging, and exposes the staged slot to later
service adapters. The standalone host-to-DPU COMCH smoke historically validated
this path with a synthetic `START_COMMAND` SQL request; the TCP setup path must
revalidate the same command-pull slice before the production setup replacement is
accepted. Stage 7B.1 then extracts the
local-control semantic dispatcher from SHM slot response publication, while
keeping async continuations explicitly SHM-slot-owned until a response-owner
abstraction exists. Stage 7B.2 adds the first response-owner abstraction for the
current SHM slot path: async state now carries a response owner instead of a raw
slot index, and the SHM async pump rejects non-SHM owners as a service bug.
Stage 7B.3 adds the DPU command-staging handoff lifetime API: the service can
copy a staged command together with bridge/ring/ordinal metadata and explicitly
release the DMA staging buffer after making its own local copy. Real DPU
response-owner metadata was added in Stage 7B.4, along with an explicit guard
that rejects async DPU-staged local-control continuations until Stage 8 response
publication exists. Stage 7B.5 then adds the service-owned staged-command
dispatch queue and a distinct
`HOMER_PROGRESS_ACTION_DPU_STAGED_COMMAND_DISPATCH` collector/action. That
action copies engine-staged commands into service-owned queue entries, releases
the engine command-pull staging buffer, and remains bounded by scheduler
`maxItems`; it deliberately does not execute command semantics yet. Stage 7B.6
then adds a second bounded internal queue for executed responses pending DPU
publication and a separate
`HOMER_PROGRESS_ACTION_DPU_STAGED_COMMAND_EXECUTE` collector/action. The execute
action consumes service-owned dispatch entries only when pending-response
capacity exists, runs `TupleSinkServiceDispatchLocalControlSlot()` with a DPU
response owner, and stores the completed response plus bridge/ring/generation
metadata for Stage 8. This separation is intentional: dispatch is the physical
DMA-staging handoff, execute is the semantic state mutation, and response
publication is the only host-visible completion point. Stage 8A adds the
same-context response-body DMA plus response-ready publication-word DMA
mechanism; Stage 8B wires the real selected-DPU frontend command path to it and
adds the no-fallback acceptance evidence. Stage 8B.10 implements the first
production selected-DPU backend-command semantic stage: DPU-local
`serviceSessionId` state owns `commandSequence`, frontend `START_COMMAND`
requests are materialized into compact backend command records, and the records
are queued with backend-mailbox descriptor refs. Stage 8B.11 implements the
backend-command DMA publication action: body, `readySeq`, and `publishedEpoch`
DMA writes are submitted on the same ordered command context, and the frontend
START response is queued for the existing response-publication action after
successful backend-command submission. Backend-completion pull remains the next
Stage 8B sub-step.

Tasks:

- Add a host lifecycle shim in or beside `homer_frontend_dma.c/.h` for selected
  DPU command sessions. It owns host-local mailbox creation, mmap
  registration/export, TCP setup, DPU session identity exchange, postmaster
  spawn request submission, and bounded setup/spawn/teardown errors.
- Do not treat `TupleSinkServiceDispatchLocalControlSlot()` as a drop-in
  production dispatcher for selected-DPU command-session open if it would require
  the DPU service to perform host-local backend spawn work. Use it only for
  DPU-owned or already-lifecycle-prepared command/control records, or refactor
  the host-local lifecycle pieces behind the shim first.
- Establish the selected-DPU setup ordering: DPU session identity, host mailbox
  creation, host mmap export, DPU import ack, host postmaster spawn, then
  hot-path request publication.
- Make host frontend publish request-ready state through bridge lines.
- Require completed DPU TCP/mmap setup before publishing any command request in
  DPU mode.
- Consume Stage 6B.2 discovered-ready ring facts through a command-pull engine
  API. The API should expose accepted ring identity/frontier, DMA-pull request
  slots up to that accepted frontier, and clear or advance the ring ready bit
  only after the pulled command work has been accepted into DPU-local staging.
- Add DPU-mode frontend errors for missing setup, stale setup generation, absent
  DPU service, and setup timeout; these errors must occur before any SHM frontend
  mapping or SHM local-service call.
- DMA-read ready request slots into DPU-local staging.
- Refactor service local-control handling behind an adapter that accepts staged
  request/response objects.
- Add a response-owner abstraction before enabling async staged commands. SHM
  slots are one owner kind for DPU-off mode; selected DPU mode must use a
  DPU-response owner that carries enough bridge/ring/generation information for
  Stage 8 DMA publication.
- Keep DPU staged-command dispatch, semantic execution, and response publication
  as separate scheduler actions. Dispatch owns the copy from transient DMA-engine
  staging into service-owned state; execute owns local-control state mutation and
  may run only when the bounded pending-response queue has capacity; response
  publication owns host-visible DMA writes and retirement.
- Do not execute a service-owned DPU staged command unless the response has a
  safe publication destination. The acceptable intermediate shape is an explicit
  service-owned "executed response pending DPU publication" queue that preserves
  the response body plus DPU response-owner metadata and is consumed only by a
  bounded Stage 8 response-publish action. The unacceptable shape is executing
  `TupleSinkServiceDispatchLocalControlSlot()` for a DPU-staged command, mutating
  service state, and then leaving the host with no response-ready publication
  path.
- Do not use repeated `POLL_COMMAND_COMPLETION` request/response loops as the
  selected-DPU hot-path wait mechanism. The current compatibility bridge can use
  it only as temporary scaffolding. The target selected-DPU command path is one
  frontend request plus one host-local wait on a DPU-published frontend
  completion mailbox/slot.
- Publish DPU-to-host response/completion bodies through an async two-phase DMA
  chain under the same command/completion context: submit body DMA first, let PE
  drain retire the body task, have the body completion callback submit the
  publication-word DMA, and release the buffer only after the publication task
  retires. The callback must not call `doca_pe_progress()`, allocate on the hot
  path, or run command semantics.
- Preserve request sequence and owner validation.
- Keep the old SHM host-process APIs buildable only as DPU-off migration
  coexistence; do not add fallback branches from selected DPU command handling to
  SHM command handling.
- Add a stage-local diagnostic or counter that proves a selected-DPU command API
  call entered through TCP setup, grouped-control discovery, command-slot DMA
  pull, and staged local-control dispatch rather than the old SHM command ring.
- Replace the Stage 6 terminal `not implemented` guard for command APIs with the
  staged request-slot pull path. After this stage, a selected DPU command session
  should fail only for DPU setup/protocol/runtime errors, not because command
  execution is still deliberately blocked.
- Treat Stage 8B promotion work as the command replacement gate: selected-DPU
  command APIs must publish host request slots for DPU pull, and any remaining
  command failure must be a DPU setup/protocol/runtime failure rather than a
  deliberate migration placeholder.
- Add Stage 8B promotion evidence to command-path diagnostics: one accepted
  command must be traceable through host request publication, grouped-control
  discovery, command-slot DMA pull, DPU-local staged dispatch, and response-owner
  selection. SHM command-ring evidence is valid only as a DPU-off regression
  check.
- Add lifecycle-shim evidence to Stage 8B diagnostics: selected-DPU open must show
  host mailbox creation, DPU TCP/mmap import ack, host postmaster spawn request,
  and then DPU-pulled request publication. The old host service may remain
  buildable for DPU-off mode, but it must not be the selected-DPU hot-path
  command transport.
- Add an explicit selected-DPU backend startup mode to the postmaster spawn ABI.
  The current startup payload in
  `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h`
  carries only service-session identity and the optional old host-service
  completion bitmap. That is not enough to tell
  `ExecuteRemoteExecBackendCommand()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c`
  to skip the old host-service control region. Extend the spawn request and
  backend startup data with a small channel/mode field. For selected-DPU mode,
  the host lifecycle shim sets that mode, keeps
  `completionReadyBitmapEnabled = 0`, and keeps
  `serviceSessionIndex = CITUS_REMOTE_EXEC_CONTROL_INVALID_SESSION_INDEX`.
- Keep the backend mailbox interface name-based, not descriptor-based. The
  lifecycle shim already creates backend-visible command/completion mailboxes and
  exports their memory to the DPU service; the spawned PostgreSQL backend should
  open those POSIX SHM mailboxes by `serviceSessionId`, just as the current
  socketless backend does. Only the DPU service consumes the TCP/mmap descriptor
  table.
- In selected-DPU backend mode, `ExecuteRemoteExecBackendCommand()` must not call
  `RemoteExecMapControlRegion()`. It should map only the command mailbox and
  completion mailbox, set `RemoteExecBackendSessionState.controlRegion = NULL`,
  and rely on `PublishCompletionToMailbox()` plus the backend completion mailbox
  `publishedEpoch` as the DPU-visible completion publication. This is compatible
  with `RemoteExecBackendMarkCompletionReady()` because the bitmap flag is
  disabled.
- Make backend cleanup optional-control-region safe on both error and normal
  exits. The current error path guards the control-region pointer and descriptor,
  while the normal path still assumes they are valid. Selected-DPU backend mode
  must not unmap NULL or close `-1`.

Acceptance:

- `StartRemoteExecutionCommandThroughLocalService()` and
  `PollRemoteExecutionCommandCompletionThroughLocalService()` work through the DPU
  channel in a single-session test.
- Selected-DPU command-session open does not require the DPU service to open host
  `/dev/shm` objects or signal the host postmaster. Those operations are carried
  by the host lifecycle shim before hot-path DMA publication begins.
- The same APIs still work through the old SHM host-process implementation when
  DPU mode is off during migration, but this is mode selection, not fallback from
  a selected DPU session.
- A DPU-mode command session that has not completed TCP/mmap setup fails with
  an explicit DPU setup error before publishing a request, and does not remap or
  call the SHM frontend path.
- A DPU-mode command session whose DPU service is absent or times out fails with
  an explicit bounded setup/connection error.
- A selected-DPU socketless backend log shows mailbox attach without
  `RemoteExecMapControlRegion()` and without opening
  `/citus_remote_execution_control_v*`; completion publication advances only the
  lifecycle-created completion mailbox, and the DPU completion-pull path observes
  that mailbox by DMA.
- A negative selected-DPU startup smoke that accidentally reaches the old
  control-region attach must fail the stage; it is not acceptable to satisfy the
  selected-DPU smoke by running the backend through old host-service control SHM.
- The hidden experimental selector remains required, but command API acceptance
  evidence must be collected through the selected DPU path. SHM command success
  is only a DPU-off regression check.
- Stage 8B is not accepted if selected-DPU command execution can still reach the
  old host-process SHM command ring, even as a convenience fallback after a DPU
  setup or command-pull error.
- Stage 8B is not accepted if selected-DPU command-session open is implemented by
  forwarding every semantic command back to a host service. Host-local lifecycle
  setup may use TCP and postmaster SHM, but steady-state command/completion
  movement must be through exported memory and DPU DMA.
- Stage 8B is not accepted if response-owner plumbing still assumes an SHM slot
  for any selected-DPU command request that can become asynchronous.
- Stage 8B is not accepted if semantic execution of a DPU-staged command can
  mutate service state before the completed response is either DMA-published to
  the host or durably staged in a bounded service-owned pending-publication queue.
- Stage 7B.6 compile validation is not a substitute for Stage 7 acceptance. It
  proves only that the bounded pending-response queue and execute action are
  integrated into the scheduler; end-to-end selected-DPU command acceptance still
  requires Stage 8 response publication evidence.
- Early response publication negative test fails as expected.
- Before real backend command execution, a synthetic request-slot pull test DMA
  reads one fixed request slot into DPU-local staging, validates owner/generation,
  and leaves command semantics unmodified.
- Ready-fact consumption test proves a grouped-control accepted frontier makes a
  command ring eligible once, command-pull work consumes or advances that ready
  fact, and the scheduler no longer sees `totalDiscoveredReadyRingCount > 0`
  after all accepted command slots have been staged.
- Stage 8B promotion evidence must include the command-path diagnostic/counter
  proving selected-DPU command work entered through the DPU pull path. A command
  smoke that succeeds only through the host-process SHM command ring does not
  satisfy Stage 8B.

#### Stage 8B scheduler-action contract for backend-mailbox DMA

The selected-DPU command path needs a more concrete scheduler contract than the
earlier "staged command execute" wording. Stage 7B's DPU
staged-command dispatch/execute path is useful for synthetic control-slot
smokes and for DPU-resident service-control work, but it is not the final
`CLIENT_SQL_SESSION` hot path. Real SQL command execution must still happen in
the host socketless backend loop at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2456`.
Therefore Stage 8B must schedule DMA operations that bridge between the
host-exported frontend command slot and the host-exported backend command and
completion mailboxes.

Keep these operations inside the existing `machine-baseline` DPU collector/action
framework. The current registry already creates DPU collectors in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14519`
through
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14547`,
and all DPU work is executed through
`HomerServiceExecuteDpuDmaAction()` at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36420`.
Do not add a private DPU loop and do not block inside PE drain; PE progress
remains bounded by the granted poll budget.

The Stage 8B command scheduler should be implemented as these bounded actions:

1. **Grouped-control read**: keep
   `HOMER_PROGRESS_ACTION_DPU_GROUPED_CONTROL_READ`, but generalize its snapshot
   parser beyond `HomerDpuBridgeHostPublishLine`. Frontend-control descriptors
   still use the bridge publish line. Backend command and completion mailbox
   descriptors use the first cache line of
   `CitusRemoteExecLocalCommandMailbox` or
   `CitusRemoteExecLocalCompletionMailbox`, whose `publishedEpoch` and
   `consumedEpoch` fields are defined in
   `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:193`
   and
   `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:216`.
   Candidate building must still read only maintained facts; no host-memory DMA
   or DOCA call is allowed in `HomerServiceAppendDpuDmaCollectorCandidates()` at
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34975`.
2. **Frontend command pull**: keep
   `HOMER_PROGRESS_ACTION_DPU_COMMAND_PULL` for descriptors with the frontend
   control-slot role. It DMA-reads a ready `CitusRemoteExecControlSlot` into
   DPU-local staging and records bridge/ring/request-sequence ownership. This is
   the only action that consumes frontend request readiness.
3. **Frontend-to-backend command staging**: replace the production use of
   `HOMER_PROGRESS_ACTION_DPU_STAGED_COMMAND_EXECUTE` for
   `CLIENT_SQL_SESSION` with a new explicit action, for example
   `HOMER_PROGRESS_ACTION_DPU_BACKEND_COMMAND_STAGE`. It consumes a pulled
   frontend control slot, validates it is a command `START_COMMAND` for a
   lifecycle-prepared selected-DPU session, and materializes a
   `CitusRemoteExecLocalCommandRecord` exactly as
   `TupleSinkServicePublishLocalCommand()` does at
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18840`.
   The selected-DPU service owns the per-session command sequence in
   DPU-local session state keyed by `serviceSessionId`; the host frontend does
   not allocate `commandSequence`, and the old host-service
   `TupleSinkServiceSessionState` is not a fallback owner for selected-DPU
   sessions. This mirrors the current host-process path where
   `TupleSinkServicePublishLocalCommand()` increments
   `currentCommandSequence` and returns it in
   `CitusRemoteExecStartCommandResponse`.
   It must not mutate a host backend mailbox and must not publish a frontend
   response. Its output is a service-owned queue entry carrying the local command
   record, command sequence, backend-command descriptor identity, frontend
   response owner, selected-DPU in-flight command state, and the backend
   completion mailbox descriptor/credit frontier needed to consume later
   physical completion epochs.
4. **Backend command mailbox DMA write**: add a new action such as
   `HOMER_PROGRESS_ACTION_DPU_BACKEND_COMMAND_PUBLISH`. It submits the DMA
   writes that replace `TupleSinkServicePublishLocalCommand()`'s direct CPU
   stores at
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19014`.
   For each queued command, submit command-record body bytes first, then
   `readySeq`, then `publishedEpoch` on the same ordered DOCA DMA context. The
   backend's stable reader at
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:405`
   only wakes on `readySeq == expectedCommandSequence`, but updating
   `publishedEpoch` keeps mailbox fullness/debug state consistent with the
   original host-service path. This action does not call `doca_pe_progress()`;
   callback retirement is owned by `DPU_PE_DRAIN`.
5. **Backend completion mailbox pull**: add a new action such as
   `HOMER_PROGRESS_ACTION_DPU_BACKEND_COMPLETION_PULL`. It runs when maintained
   backend-completion facts show `publishedEpoch > lastConsumedEpoch` for a
   selected-DPU session. It DMA-reads the next
   `CitusRemoteExecCommandCompletion` slot, validates protocol version, command
   sequence, descriptor identity, generation, and physical mailbox epoch, stages
   the completion in DPU/service memory, and submits or queues a DMA write of
   the backend completion mailbox
   `consumedEpoch` after the completion body has been safely copied. It replaces
   the service's direct completion consumption in
   `TupleSinkServiceConsumeCompletionMailbox()` at
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18473`.
   The physical mailbox epoch is not the semantic command sequence. The
   selected-DPU service must validate
   `CitusRemoteExecCommandCompletion.commandSequence` against the selected
   session's in-flight command sequence, and must use the mailbox epoch only as
   the consumed-credit frontier written back to the backend.
   The implementation should not try to hide this inside the existing
   grouped-control read path: `CitusRemoteExecLocalCompletionMailbox` at
   `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:215`
   is a fixed-slot backend mailbox, not a `HomerDpuBridgeHostPublishLine`.
   Completion discovery therefore needs a mailbox-specific control/header DMA
   read, followed by completion-slot body DMA reads and a consumed-epoch DMA
   write.
6. **Frontend response publication**: reuse the Stage 8A
   `HOMER_PROGRESS_ACTION_DPU_COMPLETION_PUSH` shape, but treat it explicitly as
   frontend-response publication. It consumes staged backend completions, writes
   the frontend response body first, then writes the response-ready publication
   word on the same ordered context. It publishes to the frontend
   control-slot/response descriptor role, not to the backend completion mailbox.
   For `START_COMMAND`, the assigned `commandSequence` is just a field in the
   existing `CitusRemoteExecStartCommandResponse` response body. Returning it
   does not require an extra DMA operation beyond this normal response-body DMA
   write plus the response-ready publication word. This replaces the old
   host-process shared-memory response path where
   `StartRemoteExecutionCommandThroughLocalService()` reads
   `response->commandSequence` after
   `WaitForRemoteExecutionControlResponse()`.
7. **PE drain and task retirement**: keep
   `HOMER_PROGRESS_ACTION_DPU_PE_DRAIN` as the only action that calls
   `doca_pe_progress()` and retires DOCA callbacks. Submission actions must not
   spin waiting for completion. They set owner metadata for callbacks to update
   physical frontiers, mark staged records ready, publish local facts, and
   recycle task slots later.

The production command path order is therefore:

```text
frontend request publish
  -> DPU_GROUPED_CONTROL_READ
  -> DPU_PE_DRAIN
  -> DPU_COMMAND_PULL
  -> DPU_PE_DRAIN
  -> DPU_BACKEND_COMMAND_STAGE
  -> DPU_BACKEND_COMMAND_PUBLISH
  -> DPU_PE_DRAIN
  -> host socketless backend executes
  -> DPU_GROUPED_CONTROL_READ over backend completion mailbox control line
  -> DPU_PE_DRAIN
  -> DPU_BACKEND_COMPLETION_PULL
  -> DPU_PE_DRAIN
  -> DPU_COMPLETION_PUSH
  -> DPU_PE_DRAIN
  -> host frontend observes DPU-published response
```

This order is causal, not a single monolithic scheduler grant. The
`machine-baseline` planner can interleave other sessions, payload pulls, and PE
drains between these actions as long as each action observes its maintained
facts and owner-generation checks.

The DPU DMA engine therefore needs new task-owner kinds for backend-command
write body, backend-command `readySeq`/`publishedEpoch` publication,
backend-completion body read, and backend-completion `consumedEpoch` credit
write, in addition to the existing grouped-control, frontend command-pull, and
frontend response-publish owners in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:80`.
Each owner must carry import index, descriptor role, ring index, bridge
generation, serviceSessionId, command sequence, slot index or mailbox epoch, and
local staging-buffer identity. A stale owner due to teardown or generation
change may be counted and recycled, but a role mismatch, impossible command
sequence, or publication-order violation is a fatal service bug.

These actions imply a small descriptor-role ABI extension. The existing
`HomerDpuBridgeRingDescriptor` at
`/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:154`
already has `workloadClass`, `direction`, `recordGeometry`,
`serviceSessionId`, and `serviceSinkId`, but role must not be inferred only from
that tuple. Add an explicit descriptor role for at least:

- frontend control slot: host frontend request slot and DPU response target;
- backend command mailbox: DPU writes `CitusRemoteExecLocalCommandRecord` bodies
  and `readySeq` publication words;
- backend completion mailbox: DPU reads backend-published completions;
- payload byte ring: host-published byte stream for Stage 9 DPU pull.

`serviceSessionId` identifies the selected-DPU SQL session. `serviceSinkId`
remains meaningful for tuple/basebackup byte streams and should be zero or an
invalid sentinel for per-session command/completion mailbox descriptors unless
the implementation later needs multiple command-like sinks per session.

Implementation substeps for this scheduler contract:

1. **Scheduler enum and registry scaffold**: add explicit collector/action kinds
   for backend command staging, backend command publish, and backend completion
   pull near the existing DPU enum entries at
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2126`
   and
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2150`.
   Update action names, stats indexing, primary action class, reason flags,
   collector feedback mapping, collector registration, collector-to-source
   mapping, collector-to-action mapping, machine-baseline ordering, and the
   `HomerServiceExecuteDpuDmaAction()` switch at
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36420`.
   Stub actions should return bounded empty progress until facts and engine APIs
   exist; this keeps the scheduler buildable without pretending the hot path
   works.
2. **Descriptor roles and multi-export setup**: add descriptor roles and
   role-aware validation, while keeping the existing frontend-control descriptor
   working. The setup import path must accept descriptors for frontend control,
   backend command mailbox, and backend completion mailbox. Descriptor lookup in
   the DPU engine should be by `(clientInstanceId, serviceSessionId, role,
   serviceSinkId/ringId)` rather than by ring index alone once multiple exported
   mappings per session exist.
3. **Maintained facts only**: extend `HomerDpuDmaClassFacts` and
   `HomerDpuDmaSchedulerFacts` in
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:48`
   with separate counters for frontend commands staged, backend commands queued
   for publish, backend-command DMA in flight, backend completion epochs ready,
   backend-completion DMA in flight, backend completions staged for frontend
   response, and frontend responses pending publication. Populate them in
   `HomerDpuDmaGetSchedulerFacts()` at
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:403`.
   `HomerServiceAppendDpuDmaCollectorCandidates()` must use only those counters
   plus service-owned queue depths.
4. **DPU DMA ready-ref queues**: add fixed-array per-workload/per-direction
   ready-ref queues inside the DPU DMA engine. These queues hold exact DMA object
   identities for executor use, while the counters above remain the scheduler
   predicate. Host-to-DPU queues are filled by accepted grouped-control or
   mailbox-control snapshots. DPU-to-host queues are filled by local service
   workflow state. Entries must identify `{importIndex, ringIndex}` and must be
   revalidated before DMA submission. Do not add a host-exported completion-ready
   bitmap/hint ABI in this stage.
5. **Service-owned semantic queues**: add a selected-DPU command pipeline in
   `HomerServiceDpuDmaSchedulerState`: pulled frontend command queue,
   backend-command publish queue, backend-completion staging queue, and frontend
   response pending queue. Queue entries must include the original
   `CitusRemoteExecControlSlot` request/response owner, backend mailbox
   descriptor identity, command sequence, backend-completion credit frontier, and
   generation tokens. The existing Stage 7 dispatch/execute queues may be reused
   only if they are renamed or documented as frontend-command and
   pending-response queues; do not let `TupleSinkServiceDispatchLocalControlSlot()`
   remain the production SQL execution step for selected-DPU sessions.
   Implementation progress: Stage 8B.10 adds the selected-DPU session table and
   backend-command publish queue. The queue now carries the pulled frontend
   response owner, DPU-owned command sequence, compact
   `CitusRemoteExecLocalCommandRecord`, backend command mailbox descriptor ref,
   and selected-DPU in-flight state. Stage 8B.11 consumes that queue through the
   real backend-command DMA submission path and then queues the frontend START
   response for response publication. Backend-completion staging is still
   pending.
6. **Backend command publish API**: add a DPU engine API such as
   `HomerDpuDmaSubmitBackendCommandPublication()`. It submits one command record
   body write plus `readySeq` and `publishedEpoch` publication writes under the
   command workload context, checks task-window capacity before consuming the
   service queue entry, and reports `submittedTasks`, `budgetExhausted`, and
   `stillReady` through `HomerDpuDmaProgressResult`.
   Implementation progress: Stage 8B.11 adds this API. The current submission
   shape copies the command record into engine-owned source buffers, submits the
   body and `readySeq` writes with optimized completion reports, submits
   `publishedEpoch` with `FLUSH`, and releases the source buffer only when the
   final publication task retires.
7. **Backend completion pull API**: add a DPU engine API such as
   `HomerDpuDmaSubmitBackendCompletionPulls()`. It submits completion-slot body
   reads for accepted backend completion frontiers, stages completed bodies in a
   bounded local buffer, and submits/queues the matching backend
   `consumedEpoch` DMA write after the body read callback succeeds. If a backend
   completion contains tuple-result metadata, the staged frontend response must
   carry that metadata forward before response publication.
8. **Candidate rules and ordering**: update
   `HomerServiceAppendDpuDmaCollectorCandidates()` so:
   - grouped-control read remains blind/speculative and may be backed off after
     empty grants;
   - PE drain is planned whenever any DPU task is in flight or TCP setup has an
     active connection;
   - frontend command pull is planned only from frontend-control ready facts;
   - backend command stage/publish is planned only when queue capacity and DMA
     task capacity exist;
   - backend completion pull is planned only when maintained completion frontier
     facts show work or a selected-DPU command is waiting for backend terminal
     visibility;
   - frontend response publication is planned only when staged backend
     completions are ready and response DMA task capacity exists.
8. **Grant budgets**: use `grantVector.maxItems` as the semantic record budget
   for the first implementation. Each executor must derive its worst-case DMA
   task count internally and refuse/return `stillReady` when the class async
   window or free task-slot pool cannot cover the whole record. Do not partially
   consume a backend-command publish queue entry unless all body and publication
   tasks can be submitted. A later cleanup can make `grantVector.maxWrs`
   authoritative; it is not required for the first correct Stage 8B path.
9. **Validation gates**:
   - scheduler scaffold compile: new enum/action cases build with empty actions
     and DPU disabled behavior unchanged;
   - descriptor-role setup smoke: one frontend-control, one backend-command, and
     one backend-completion descriptor import and appear in scheduler facts;
   - backend-command DMA smoke: DPU writes one
     `CitusRemoteExecLocalCommandRecord` body plus `readySeq`/`publishedEpoch`
     into an exported host mailbox; host CPU validation observes the exact body
     only after `readySeq`. Stage 8B.11 extends the TCP transport smoke for this
     gate and validates it live on farnet1: the host observed
     `ready_seq=7001 published_epoch=7001`, and the DPU server completed five
     backend-command plus response-publication DMA tasks;
   - backend-completion DMA smoke: host writes one completion body plus
     `publishedEpoch`; DPU pulls it, validates command sequence/epoch, and
     DMA-writes `consumedEpoch`;
   - socketless-backend smoke: host lifecycle shim spawns a backend, DPU writes
     a minimal command to the backend mailbox, DPU reads the backend completion,
     and DPU publishes the frontend response;
   - negative no-fallback smoke: if backend completion or frontend response
     publication is disabled/stale, selected-DPU frontend polling fails boundedly
     and never polls the old host-process SHM completion path.

#### Stage 8B.12-8B.15 backend-completion implementation order

The backend-completion direction is now detailed enough to proceed without a new
design discussion, but it should be implemented in narrower validation slices
than the earlier single "backend completion pull" bullet implied.

1. **Stage 8B.12: engine-only completion-mailbox DMA smoke.** Extend the TCP
   smoke/setup path to export one backend completion mailbox descriptor with
   role `HOMER_DPU_BRIDGE_DESCRIPTOR_ROLE_BACKEND_COMPLETION_MAILBOX`. The host
   writes one `CitusRemoteExecCommandCompletion` body into
   `CitusRemoteExecLocalCompletionMailbox.completionSlots[...]` and then
   publishes the matching `publishedEpoch`. The DPU engine performs a
   mailbox-control/header DMA read, reads exactly the published completion slot
   body, validates the command sequence and epoch against the smoke expectation,
   and DMA-writes `consumedEpoch` back to the host. The host validates
   `consumedEpoch == publishedEpoch`. This slice deliberately does not route the
   completion into the production scheduler semantic path.
   Implementation progress: Stage 8B.12 landed this engine-only smoke. The live
   farnet1 host-DPU validation on June 29, 2026 observed setup `rings=3`, host
   backend-command publication `ready_seq=7001 published_epoch=7001`, frontend
   response publication `state=4 command_seq=7001`, backend completion
   `consumed_epoch=1`, and DPU server completion with eight DMA tasks.
2. **Stage 8B.13: production DMA engine completion APIs.** Add task kinds,
   task-owner metadata, staging buffers, and bounded APIs for completion control
   reads, completion body reads, and consumed-epoch publication. The expected
   shape is `HomerDpuDmaSubmitBackendCompletionPulls()` plus a small accessor
   pair for staged completions, for example
   `HomerDpuDmaCopyNextStagedBackendCompletion()` and
   `HomerDpuDmaReleaseStagedBackendCompletion()`. A completion body is accepted
   into the DPU-owned staging queue before the consumed epoch is published back
   to the backend mailbox. Completion DMA failures remain fatal prototype bugs,
   not recoverable per-command errors.
   Implementation progress: Stage 8B.13 landed
   `HomerDpuDmaSubmitBackendCompletionPulls()`,
   `HomerDpuDmaCopyNextStagedBackendCompletion()`, and
   `HomerDpuDmaSubmitBackendCompletionCreditPublication()`. The TCP smoke now
   exercises that production-shaped path instead of only smoke-only helper
   functions. The live farnet1 host-DPU validation on June 29, 2026 again
   observed setup `rings=3`, backend-command publication
   `ready_seq=7001 published_epoch=7001`, frontend response publication
   `state=4 command_seq=7001`, backend completion `consumed_epoch=1`, and DPU
   server completion with eight DMA tasks.
3. **Stage 8B.14: scheduler semantic integration.** Wire
   `HOMER_PROGRESS_ACTION_DPU_BACKEND_COMPLETION_PULL` so it consumes staged
   completion facts under `grantVector.maxItems`, validates
   `serviceSessionId`, bridge generation, descriptor role, command sequence, and
   the selected session's one-command-in-flight state, then clears/advances that
   in-flight state. It should enqueue the existing frontend poll/terminal
   response for `HOMER_PROGRESS_ACTION_DPU_COMPLETION_PUSH`; it must not revive
   the old host-process SHM completion path. For the first narrow selected-DPU
   SQL command smoke, terminal command completion should be returned through the
   frontend `POLL_COMMAND_COMPLETION` response shape only as a temporary
   compatibility bridge. It is not the target hot path: selected-DPU command
   execution should move to one command request plus one frontend completion
   wait, because issuing repeated POLL requests over DPU DMA is unnecessary
   latency and DMA traffic.
   Implementation progress: Stage 8B.14 landed the scheduler semantic bridge.
   `HOMER_PROGRESS_ACTION_DPU_BACKEND_COMPLETION_PULL` now submits bounded
   backend-completion DMA pulls, accepts staged completions into selected-DPU
   session state after consumed-epoch credit submission, and stages frontend
   `POLL_COMMAND_COMPLETION` responses for the existing DPU completion-push
   action. The frontend poll slot is not held while waiting for the backend; a
   not-yet-complete command returns PENDING and a later poll receives the copied
   backend completion. Validation for this slice is compile/build only; the live
   selected-DPU command smoke remains Stage 8B.15.
4. **Stage 8B.15: response-publication correction and selected-DPU command
   smoke.**
   After the engine and scheduler paths above land, remove the selected-DPU
   start/poll guards only for the narrow command-session path being validated.
   Before accepting the smoke, replace the current same-action response body plus
   `RESPONSE_READY` submit with a nonblocking two-phase DMA chain. The body DMA
   completion callback should submit the response-ready publication DMA directly
   as a DMA-chain continuation; this keeps latency low without spinning in the
   submit action or waiting for another scheduler round to arm the publish task.
   Acceptance requires one real selected-DPU command whose request reaches the
   backend mailbox by DPU DMA, whose backend completion is pulled by DPU DMA, and
   whose frontend-visible response/completion is published by DPU DMA. A negative
   smoke must prove that disabling or corrupting the backend-completion/frontend
   publication path fails boundedly instead of falling back to host SHM completion
   polling.

   Implementation finding from the first Stage 8B.15 attempt: submitting the
   response body DMA and response-ready DMA back-to-back on the same ordered DOCA
   context was not sufficient for the host frontend, which polls host memory, to
   consume a coherent response under the current farnet setup
   (`PCI_WR_ORDERING=force relaxed`). A temporary diagnostic gate that waited for
   the response-body DMA completion before submitting the response-ready DMA
   removed the stale/partially visible POLL response symptom. The production fix
   must be the async callback-chained form above, not the diagnostic blocking
   gate.

   The same validation attempt then exposed a separate lifecycle blocker: the
   spawned socketless backend still reached
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:653`
   and tried to map the old host-process control SHM region
   `/citus_remote_execution_control_v27`. The backend-spawn/open half of that
   blocker has now been narrowed: traced validation showed
   `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest()` publishing selected-DPU
   mode, `ProcessSpawnRequestSlot()` launching the backend with channel mode 1,
   and `ExecuteRemoteExecBackendCommand()` skipping the host-service control
   region while mapping only `/citus_remote_exec_cmd_v13_*` and
   `/citus_remote_exec_cpl_v13_*`. Keep that backend startup mode.

   Stage 8B.15 still is not accepted. The next old-control-region dependency is
   SQL result-sink allocation: row-producing commands call
   `RemoteExecEnsureSessionResultQueue()`, which calls
   `RemoteExecOpenServiceOwnedResultSink()` and still opens the old host-service
   control region to allocate a service-owned tuple/result byte ring. The
   pgbench wrapper reaches this through its `SELECT abalance` command. Selected-DPU
   mode needs a result/payload queue path that does not ask the old host service
   to allocate or publish result sinks.

   Concrete implementation order for this blocker:

   1. Extend the backend spawn/startup ABI with a small backend channel mode,
      for example host-service SHM versus selected-DPU DMA, in
      `remote_execution_backend_protocol.h`. Keep the extension explicit and
      versioned by the existing protocol version check.
   2. Have `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest()` set selected
      DPU mode when it reserves a postmaster spawn slot, along with
      `completionReadyBitmapEnabled = 0` and invalid service-session index.
   3. Copy the mode through `ProcessSpawnRequestSlot()` into
      `CitusRemoteExecBackendStartupData`.
   4. Teach `ExecuteRemoteExecBackendCommand()` to branch on that mode: selected
      DPU mode maps only the backend command/completion mailboxes and skips
      `RemoteExecMapControlRegion()`, while host-service mode keeps the existing
      behavior.
   5. Guard normal backend cleanup for optional control-region mapping.
   6. Add diagnostic logging/counters that make the selected-DPU backend startup
      path visible in the smoke logs before retrying the full selected-DPU SQL
      smoke.
   7. Add the selected-DPU result-sink replacement before accepting the SQL smoke:
      either export a lifecycle-created backend result byte ring in the TCP setup
      descriptor set, or add an explicitly selected-DPU local result queue that is
      visible to the DPU and published through backend completions. It must not
      call `RemoteExecOpenServiceOwnedResultSink()` or any old service-owned
      control-region request in selected-DPU mode.

5. **Stage 8B.16: selected-DPU SQL result/payload queue replacement.**
   This stage is required before full pgbench-style selected-DPU SQL validation.
   The immediate failing path is:
   `citus_remote_exec_pgbench_transaction()` ->
   `RemoteExecPgbenchExecuteSql(... REMOTE_EXEC_SQL_RESULT_TUPLE ...)` ->
   `RemoteExecBackendExecuteSqlCommand()` ->
   `RemoteExecSqlDestStartup()` ->
   `RemoteExecEnsureSessionResultQueue()` ->
   `RemoteExecOpenServiceOwnedResultSink()` ->
   `RemoteExecMapControlRegion()`. Replace that path for selected-DPU backends.

   Implementation direction:

   1. Treat the SQL result payload queue as a real tuple sink, not as a scalar
      shortcut. `citus_remote_exec_pgbench_transaction()` currently requests
      `REMOTE_EXEC_SQL_RESULT_TUPLE` for `SELECT abalance`; the selected-DPU path
      must preserve that semantic and eventually stop depending on
      `RemoteExecutionCommandCompletion.scalarInt64`.
   2. Allocate the selected-DPU result byte ring eagerly with the command and
      completion mailboxes during selected-DPU session lifecycle setup. Do not lazy
      allocate it on the first row-producing command: lazy allocation would require
      a second mmap export/import protocol after the DPU session is live.
   3. Extend the TCP setup descriptor set with a result byte-ring descriptor. The
      DPU imports it through the same setup message shape as the backend command
      and completion mailboxes. The descriptor role should make it clear that this
      is the backend-produced SQL result tuple sink.
   4. Extend the backend spawn/startup metadata enough for the selected-DPU
      socketless backend to map the pre-created result queue directly. The backend
      should derive or receive the queue SHM name, slot count, slot capacity, and
      persistent sink identity without consulting the old host-service control
      region.
   5. In selected-DPU backend mode, make `RemoteExecEnsureSessionResultQueue()`
      choose the pre-created DPU-visible result queue path instead of
      `RemoteExecOpenServiceOwnedResultSink()`. It should still call
      `RemoteExecPrepareResultQueueGeneration()` for each row-producing command so
      every command publishes a generation-local descriptor with the current
      `startByteTail`.
   6. Keep the existing tuple serialization machinery: `RemoteExecSqlDestStartup()`
      opens or reuses an `OpenCitusTupleSink()` send handle, and
      `RemoteExecSqlDestReceiveSlot()` appends ordinary tuple batches. Do not add a
      scalar-only result receiver for pgbench.
   7. Expose tuple-result metadata through the public frontend completion. Today
      the backend completion includes `resultFlags`, `resultQueueDescriptor`, and
      `resultTupleViewContract`, but `RemoteExecutionFillCommandCompletion()` only
      copies scalar fields into `RemoteExecutionCommandCompletion`. Add the result
      descriptor/contract to the public completion surface so frontend callers can
      open the result tuple sink from the completed command.
   8. Add a shared frontend result-drain API, adapting the existing
      `homer_client.c` result-sink binding logic into the reusable Homer frontend
      layer. The pgbench SQL-callable wrapper should use this API to keep one
      persistent result-sink mapping for the command session, refresh only the
      per-command generation/start-tail bookkeeping from each completed command,
      borrow/read the one-column tuple, and release the borrowed tuple/batch.
      It should not close/remap the result sink per command.
   9. Remove the pgbench wrapper's dependence on
      `RemoteExecutionCommandCompletion.scalarInt64`. If complete ABI cleanup is too
      large for the same patch, leave lower-level scalar fields temporarily unused
      and documented as obsolete, but do not use them for selected-DPU validation.
   10. Add a negative validation where selected-DPU result queue setup is disabled
       and the pgbench SELECT fails boundedly, rather than falling through to
       `/citus_remote_execution_control_v27`.

   Implementation slices:

   1. **Frontend completion/result API slice.** Completed and validated on
      June 30, 2026 for the existing non-DPU Homer path. The public
      `RemoteExecutionCommandCompletion` now carries tuple-result metadata,
      `RemoteExecutionFillCommandCompletion()` copies it, shared frontend helpers
      bind/drain the command result tuple sink, and the pgbench wrapper reads
      `SELECT abalance` by borrow/read/release instead of reading public scalar
      completion fields. The result-sink mapping remains session-owned and is
      not closed/remapped per command; only per-command generation/start-tail
      bookkeeping is refreshed when the physical queue and tuple shape are
      stable.
   2. **Selected-DPU lifecycle result-ring slice.** Extend
      `HomerFrontendDmaLifecycleMailboxes` and
      `HomerFrontendDmaBuildDocaSetupDescriptors()` with one eagerly created SQL
      result byte ring. Export/import it with the initial TCP setup. Validation:
      the standalone TCP transport smoke should report the extra descriptor and
      still pass command/completion setup.

      Implementation progress: completed on June 30, 2026 for the host-side
      lifecycle, setup descriptor, backend spawn/startup ABI, and selected-DPU
      backend result-queue binding code. The slice uses a distinct
      `SQL_RESULT_BYTE_RING` bridge descriptor role, exports the result queue in
      the initial TCP setup, and passes the same queue descriptor through backend
      spawn so selected-DPU socketless backends can map it without calling the
      old host-service control region. Host validation passed through the
      extended `homer_frontend_dma_smoke`, `homer_service_dpu_dma_smoke`, full
      Citus/Postgres rebuild+install, and the existing non-DPU Homer pgbench
      smoke after a forced pgbench relink. Full selected-DPU runtime validation
      remains pending until the DPU service binary is deployed or built on the
      DPU filesystem.
   3. **Selected-DPU backend mapping slice.** Extend the spawn/startup ABI and
      teach `RemoteExecEnsureSessionResultQueue()` to bind the pre-created result
      queue in selected-DPU mode. Validation: a row-producing selected-DPU SQL
      command must not open `/citus_remote_execution_control_v27`.

      Implementation progress: code for the backend mapping portion landed with
      the lifecycle result-ring slice. A follow-up validation found and fixed
      the result-queue binding bug where selected-DPU result queue memory was not
      cleanly distinguished from tuple-contract/generation binding. It also
      exposed a separate selected-DPU backend-completion state-machine bug:
      repeated physical backend-completion observation is not a harmless
      duplicate to suppress. The engine/service boundary must make staged
      backend-completion ownership single-claim, and the selected-DPU semantic
      layer must handle `STARTED` as a push-visible nonterminal event instead of
      clearing command in-flight state. The selected-DPU smoke no longer fails at
      the old result-sink fallback, but the sub-slice remains open because the
      next retry failed earlier with a 64-byte DOCA command/control memcpy I/O
      error and a timeout waiting for command sequence 2 to reach
      backend-started state. Do not treat Stage 8B.16 as accepted until that
      DMA/import-lifetime failure is understood and a row-producing selected-DPU
      SQL smoke passes after the Stage 8B.17 completion-event correction.
   4. **End-to-end tuple-result smoke slice.** Run selected-DPU `SELECT abalance`
      through the pgbench wrapper and verify the frontend reads the returned
      tuple from the result sink, not from scalar completion fields. This is the
      acceptance gate for Stage 8B.16 and then Stage 8B.15 can be retried.

6. **Stage 8B.17: selected-DPU backend completion event-stream correction.**
   This is the next required corrective slice before another row-producing SQL
   acceptance attempt. The bug exposed by validation is that the selected-DPU
   path treated a backend completion mailbox record as if it were always the
   terminal command completion. That is not the existing Homer contract. In the
   host-process service,
   `TupleSinkServiceConsumeCompletionMailbox()` consumes one physical mailbox
   epoch and `TupleSinkServicePublishClientCommandCompletion()` may publish a
   push-visible `STARTED` event with result metadata before a later terminal
   `COMPLETED` or `FAILED` event for the same command.

   Implementation direction:

   1. Replace the selected-DPU single `backendCompletionReady` state with an
      ordered frontend completion-event queue or equivalent per-session event
      stream. The stream must preserve `STARTED` and terminal events for the
      same `commandSequence` in mailbox order.
   2. In `HomerServiceDpuAcceptOneBackendCompletion()`, validate
      `completion.commandSequence` against the selected session's
      `inFlightCommandSequence`. Do not compare the physical backend mailbox
      epoch to the semantic command sequence. Store the physical epoch only as
      the consumed-credit frontier for the backend completion mailbox.
   3. On `STARTED`, enqueue or stage the frontend-visible event, including
      result-sink metadata, but keep `commandInFlight = true` and preserve
      `inFlightCommandSequence`. A `STARTED` event must not make the session
      eligible to stage the next command.
   4. On terminal `COMPLETED` or `FAILED`, enqueue or stage the terminal
      frontend-visible event. Clear selected-DPU one-command-in-flight state only
      after the terminal event has been accepted into the response-publication
      state machine, or keep a terminal-pending state until DMA publication
      retires if the implementation needs that stronger ownership.
   5. In the temporary `POLL_COMMAND_COMPLETION` compatibility bridge,
      `HomerServiceDpuStageOnePollCompletionResponse()` should pop the next
      queued event for the requested command. Returning a nonterminal `STARTED`
      must not clear all backend-completion or in-flight state.
   6. Replace `HomerDpuDmaCopyNextStagedBackendCompletion()`-style peek/copy
      behavior with a claim/borrow API. The engine should expose each staged
      physical backend-completion epoch to the service exactly once, for example
      through a `HomerDpuDmaClaimNextBackendCompletion()` handle that pins the
      local staging buffer until the service releases it. Borrowing the staging
      memory is preferred over copying when the response-publication path can
      respect the borrow lifetime.
   7. Split engine state into at least three concepts: service-visible
      unclaimed staged completion, service-claimed local-buffer ownership, and
      consumed-credit DMA in flight. A claimed event may still need
      `consumedEpoch` DMA publication, but it must no longer appear in ready
      facts. If the same physical completion epoch is surfaced twice after
      claim, fail a debug assertion or fatal diagnostic instead of suppressing it
      as a duplicate.
   8. Keep completion DMA failures fatal for this prototype. The Stage 8B.16
      retry also saw a 64-byte DOCA command/control memcpy I/O error; that must
      be debugged as an invalid source/destination/buffer-lifetime problem, not
      converted into a command-level retry policy.

   Acceptance:

   - a selected-DPU row-producing command observes `STARTED` with result metadata
     and then one terminal event in order, or explicitly validates an existing
     prearmed-result optimization that suppresses only redundant `STARTED`
     without losing result metadata.
   - `STARTED` never clears `commandInFlight`, never clears
     `inFlightCommandSequence`, and never allows a second command for the same
     session to be staged before terminal completion.
   - the service validates semantic `commandSequence` independently from the
     physical mailbox epoch; a test or diagnostic must catch any path that uses
     `completionEpoch == commandSequence` as a correctness check.
   - each backend completion mailbox epoch is claimed once, credited once, and
     not re-observed by scheduler facts while consumed-credit DMA is still in
     flight.
   - row-producing selected-DPU SQL drains the tuple result sink by
     borrow/read/release and does not read the obsolete public scalar shortcut.

7. **Stage 8B.18: stale grouped-control export teardown correction.**
   This corrective slice is now implemented and validated for repeated
   independent setup cycles. The 64-byte `Input/Output Operation Failed` observed
   during the Stage 8B.17 retry was initially suspected to be command-pull buffer
   lifetime, but code inspection corrected that: `task_kind=1` is
   `HOMER_DPU_DMA_TASK_KIND_GROUPED_CONTROL_READ` in
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:50`.
   The DPU service was issuing a blind grouped-control discovery read against a
   one-shot host bridge export after the host frontend had already completed the
   selected-DPU command and destroyed its exported DOCA mmaps.

   The implemented policy is narrow:

   1. Treat only `DOCA_ERROR_IO_FAILED` on grouped-control discovery reads as an
      expected stale setup-export teardown condition.
   2. Mark the import `staleTeardownPending`, skip new grouped-control reads to
      that import, and delay import destruction until no live task slot still
      references the same bridge/client setup.
   3. Keep command pull, response publish, backend-command publish,
      backend-completion pull, and consumed-credit DMA failures fatal.
   4. Restart the DOCA context from IDLE on the next real submission after the
      stale grouped-control failure moves the context through STOPPING to IDLE.

   Validation on June 30, 2026: ten independent selected-DPU SQL calls succeeded
   against the DPU service on `10.10.1.201:9727`. The DPU log showed the expected
   stale grouped-control teardown, import removal, context STOPPING/IDLE, and
   next-submission restart pattern without `tuple-sink service: DPU DMA PE drain
   failed`.

   Remaining issue from the same validation session: one SQL statement that calls
   `citus_remote_exec_pgbench_transaction(...)` through `generate_series(1, 100)`
   timed out at `command_sequence=8`. A standalone single selected-DPU call and
   ten independent setup cycles succeeded afterward, so this is a separate
   repeated-command sequencing/lifecycle problem, not the stale grouped-control
   DOCA I/O failure. Do not fold that bug into the grouped-control teardown
   policy.

8. **Stage 8B.19: remove selected-DPU compatibility poll requests.**
   This is the next selected-DPU command-path cleanup after the completion event
   stream is present. The selected-DPU frontend currently preserves the original
   public `START_COMMAND` contract by looping inside
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:1040`
   and issuing repeated `POLL_COMMAND_COMPLETION` request/response cycles through
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:1081`.
   That loop is transitional and must not be the DPU hot path.

   Corrected target design:

   1. In selected-DPU mode, `START_COMMAND` means the DPU accepted the command,
      assigned a `commandSequence`, and reserved the bounded publication resources
      needed to release the original START slot. It must not wait for the
      socketless backend to publish `STARTED`.
   2. The START response carries `PENDING` plus `commandSequence`. This is not a
      failure and not a compatibility shortcut; backend-visible states are separate
      completion events.
   3. Backend `STARTED`, `COMPLETED`, and `FAILED` all flow through the same
      selected-DPU backend-completion event stream. The first backend completion
      must not be special-cased because START is waiting.
   4. Resource capacity is reserved at selected-DPU command acceptance. Later
      backend-command publication must not discover that there is no completion
      publication owner for the command it already accepted.
   5. The final cleanup remains to replace selected-DPU
      `POLL_COMMAND_COMPLETION` request slots with a host-local wait/poll over a
      DPU-published completion event line. Until that event line exists, the
      temporary DPU poll request path is only a validation bridge and not the
      target hot path.

   Substage acceptance:

   - Stage 8B.19A: selected-DPU `START_COMMAND` returns `PENDING` plus
     `commandSequence`, reserves START ACK publication capacity during command
     staging, and never holds the original START slot until backend `STARTED`.
     Backend completions are accepted uniformly into the selected-DPU completion
     event queue.
   - Stage 8B.19B: selected-DPU command waiting observes a DPU-published
     completion event line directly from host memory and no longer submits
     `POLL_COMMAND_COMPLETION` request slots. Diagnostic counters or logs must
     prove the selected-DPU path did not submit poll request slots.

   June 30, 2026 validation attempt: this stage is **not accepted**. A narrow
   implementation that kept the original START slot open and queued the START
   response only after backend-completion acceptance removed the frontend's
   internal compatibility poll loop, but installed selected-DPU SQL still timed
   out waiting for the START response. The first fix attempt also showed that
   selected-session in-flight state must itself keep the
   `DPU_BACKEND_COMPLETION_PULL` collector eligible; relying on staged frontend
   `POLL_COMMAND_COMPLETION` requests was an accidental progress trigger. After
   adding that bounded selected-session backend-completion wait fact, validation
   exposed a harder DOCA failure instead:
      `HOMER_DPU_DMA_TASK_KIND_BACKEND_COMPLETION_CONTROL_READ` failed with
      `DOCA_ERROR_IO_FAILED` for the exported backend-completion mailbox
      (`task_kind=8 workload=2 host_bytes=64`). The immediate root cause was that
      `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:4865`
      used the size of the DPU's cache-line-aligned local snapshot as the remote
      DMA read length. The exported backend-completion mailbox ABI has only a
      24-byte control prefix:
      `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:233`.
      The fix is to DMA-read exactly
      `offsetof(CitusRemoteExecLocalCompletionMailbox, completionSlots)`, leaving
      the local snapshot alignment as private DPU storage. After that fix, one
      selected-DPU SQL smoke and ten repeated independent selected-DPU SQL calls
      completed without a task-kind-8 DOCA failure.

      Design correction from the same review: keeping the original START control
      slot open and later queuing a START response is rejected. START is an
      accepted/sequenced ACK; backend STARTED and terminal states then flow
      uniformly through the backend-completion event stream and DPU
      response-publication path. The first backend completion must not be
      special-cased only because START is waiting.

      July 1, 2026 Stage 8B.19A implementation result: selected-DPU START now
      returns `PENDING` plus `commandSequence` after command staging, reserves the
      START ACK publication slot before consuming the pulled START command, and
      removes `startResponsePending`/`startResponseCommand`. Backend completions
      are accepted uniformly into the selected-DPU completion-event queue. Host
      and DPU service builds passed; one selected-DPU SQL smoke plus ten repeated
      independent selected-DPU SQL calls completed without DPU log errors. Stage
      8B.19B remains open because the host still needs a persistent
      DPU-published completion event line before selected-DPU waits can stop
      using temporary `POLL_COMMAND_COMPLETION` request slots.

      July 1, 2026 Stage 8B.19B implementation result: selected-DPU command
      waiting now uses a DPU-published frontend completion event line exported in
      the bridge mmap. The ABI adds
      `HOMER_DPU_BRIDGE_DESCRIPTOR_ROLE_FRONTEND_COMPLETION_EVENT` and
      `HomerDpuBridgeFrontendCompletionEvent`; the host frontend exports one
      cache-line-aligned event line and
      `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:1078`
      polls its `publishedEpoch` instead of reserving and publishing a
      `POLL_COMMAND_COMPLETION` control slot. The DPU service submits the
      event body and final `publishedEpoch` as two ordered DPU-to-host DMA tasks
      through
      `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1196`,
      and the scheduler wires queued terminal selected-DPU completions into that
      path at
      `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:35698`.

      Important scoped correction: the Stage 8B.19B event line is terminal-only.
      Publishing both backend `STARTED` and terminal events into one host-polled
      event line would let the DPU overwrite the body while the host CPU is still
      copying it. Until we add either a consumed ack or a multi-slot event ring,
      `STARTED` is accepted and credited from the backend mailbox but is not
      exposed to the host frontend. This matches the target hot path for current
      pgbench commands: the host sends one command and waits for the terminal
      completion/result.

      Validation: host Citus build/install, host Postgres build/install, and DPU
      service build passed with only existing DOCA deprecated/experimental
      warnings. PostgreSQL was restarted with
      `HOMER_FRONTEND_DPU_SETUP_HOST=10.10.1.201`,
      `HOMER_FRONTEND_DPU_SETUP_PORT=9727`,
      `HOMER_FRONTEND_DOCA_DEV_PCI=0000:21:00.0`, and
      `HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS=15000`; the DPU service was started
      from `/tmp/citus-dbcomm-stage8b16` with
      `HOMER_SERVICE_ENABLE_DPU_DMA=1`, `HOMER_SERVICE_ENABLE_DOCA_DMA=1`, and
      `HOMER_SERVICE_DOCA_DEV_PCI=0000:03:00.0`, then confirmed listening on TCP
      setup port `9727`. One selected-DPU SQL smoke and ten independent
      selected-DPU SQL calls succeeded:

      ```sh
      sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql \
        -h /tmp -p 5432 -U dbcomm -v ON_ERROR_STOP=1 -AtX postgres \
        -c "SET client_min_messages = warning;
            SET citus.enable_experimental_tuple_sink_routing = on;
            SET citus.enable_experimental_homer_dpu_frontend = on;
            SELECT pg_catalog.citus_remote_exec_pgbench_transaction(1, 1, 1, 1, 1);"
      ```

      The single smoke returned `36`; the ten-call run returned `37` through
      `46`. The DPU service log had no `fatal`, `failed`, `error`, `stale`,
      `timeout`, `mismatch`, `invalid`, or `poll` matches during the accepted
      run. The source-level check for no selected-DPU poll request is that
      `HomerFrontendDmaPollCommandCompletion()` no longer calls
      `HomerFrontendDmaReserveControlSlot()` or
      `HomerFrontendDmaPublishControlSlotRequest()`; those control-slot
      publications are now limited to START/close setup operations.

9. **Stage 8B.20: reduce selected-DPU backend-command publication to two DMA
   tasks.**
   The current selected-DPU backend-command publication in
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1173`
   submits three DMA tasks: command record body, slot-local `readySeq`, and
   mailbox `publishedEpoch`. This mirrors the existing host/RDMA backend ABI,
   where `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:422`
   waits for `readySeq == expectedCommandSequence`. For selected-DPU mode this is
   too expensive and redundant.

   Target design:

   1. Use two DMA tasks for selected-DPU backend command delivery: record body
      first, mailbox `publishedEpoch` second.
   2. The final `publishedEpoch` DMA is the publication gate and must be submitted
      on the same ordered command DMA context after the record-body DMA, with the
      final task using `DOCA_TASK_SUBMIT_FLAG_FLUSH`.
   3. Do not use a single large DMA task for the publication word under the
      current farnet setup because `PCI_WR_ORDERING=force relaxed` means the host
      CPU could observe the publication word before earlier bytes in that same
      write are safely consumable.
   4. Remove `readySeq` from the selected-DPU backend command semantics. The
      field may remain for existing host-process/RDMA paths until those ABIs are
      separately simplified, but selected-DPU backend startup/dispatch should
      validate command readiness from mailbox `publishedEpoch` plus the copied
      command record's `commandSequence`/metadata.

   Implementation status on July 1, 2026: the two-task publication path is
   implemented in
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:1380`.
   The selected-DPU backend read path in
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:404`
   now waits on mailbox `publishedEpoch` instead of slot-local `readySeq` and
   fatals if `publishedEpoch` skips the expected command sequence.

   Acceptance: selected-DPU backend-command publication uses exactly two DOCA DMA
   submissions per command before later batching/coalescing work. The backend no
   longer waits on slot-local `readySeq` for selected-DPU mode, and validation
   runs under `PCI_WR_ORDERING=force relaxed`.

10. **Stage 8B.21: explicit selected-DPU setup teardown and DMA quiesce.**
   Stage 8B.18 made a grouped-control `DOCA_ERROR_IO_FAILED` recoverable only so
   the current prototype could survive one-shot host mmap destruction. That is
   not the target lifecycle. Normal selected-DPU teardown must prevent stale
   exports from being read in the first place.

   Implementation status: the first explicit TCP close/quiesce/ack path landed
   after Stage 8B.18. The implemented code adds a close request over the setup
   TCP socket, a DPU import lifecycle state, per-import in-flight task counters,
   close-drain waiting in the TCP setup server, and a TCP/DMA smoke validation
   that covers setup, command/response/completion DMA, and close ack. The
   implementation details and evidence are recorded in
   `/data/dbcomm/postgres-citus/docs/kb/implementations/citus/transport/dpu_dma_backend_homer_service_implementation_checkpoint.md`.

   Target design:

   1. Add an explicit selected-DPU teardown operation over the setup/lifecycle
      control channel. The host identifies the `bridgeGeneration` and
      `clientInstanceId` being closed before destroying any exported DOCA mmap.
   2. On teardown request, the DPU marks the import closing and immediately stops
      submitting new grouped-control, command-pull, completion-pull, payload, or
      credit-publication work for every descriptor in that import.
   3. The DPU drains already-submitted tasks for the import through normal
      scheduler-granted PE progress. No callback may publish new readiness or
      host-visible state after the import is closing.
   4. Replace the current task-slot scan with per-import in-flight accounting
      before making this path performance-relevant. The scan in
      `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:3254`
      is a transitional guardrail, not the final hot/lifecycle design.
   5. Once the per-import in-flight count reaches zero, the DPU must finalize
      service-side semantic state for every selected-DPU session owned by the
      closing setup identity before destroying imported descriptors. This is
      separate from physical DMA quiesce: command sequence, in-flight command,
      backend-completion event queue, pending backend-command publication,
      pending frontend-response publication, and staged dispatch state all belong
      to the service scheduler and can outlive the imported mmap unless reset
      explicitly.
   6. Only after semantic close finalization and import destruction may the DPU
      send a teardown ack. Only after that ack may the host call
      `doca_mmap_destroy()` in
      `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:1304`.
   7. After this stage, `DOCA_ERROR_IO_FAILED` on grouped-control read during
      normal teardown is a bug and should be fatal during validation. The Stage
      8B.18 nonfatal stale handler may remain only as an abnormal-crash diagnostic
      until a deliberate crash-recovery policy exists, but it must not be part of
      the accepted steady path.

   Enforcement mechanism:

   - The DMA engine owns the teardown correctness invariant; scheduler ordering is
     not sufficient. Each imported setup needs an explicit lifecycle state such
     as `ACTIVE`, `CLOSING`, and `CLOSED`.
   - Every submit path must require `import->state == ACTIVE` before allocating
     or submitting a DOCA task. A teardown request transitions the import to
     `CLOSING`, and from that point all grouped-control, command-pull,
     backend-completion-pull, payload, response, and credit-publication submit
     actions must skip or reject that import without touching host memory.
   - Every DOCA task owner already identifies its import through the owner
     metadata consumed by
     `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:3190`.
     Stage 8B.21 should add per-import in-flight accounting: increment before a
     submitted task becomes visible to DOCA, decrement only in the centralized
     retirement path at
     `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:3682`.
   - Callbacks for a `CLOSING` import are retirement-only. They may free task
     handles/bufs and decrement counters, but they must not enqueue ready refs,
     publish host-visible state, or schedule follow-on semantic work. If normal
     teardown observes unfinished command/response work that would require a
     semantic completion, treat that as a teardown-ordering bug until explicit
     cancellation semantics are designed.
   - PE draining remains a normal scheduler-granted action. Teardown must not spin
     inside the close path waiting for completions; it should keep scheduling
     bounded `DPU_PE_DRAIN` work while per-import or global in-flight counts are
     nonzero.
   - The initial implementation keeps close-finalize inside the setup TCP
     progress action: a close connection enters `WAITING_CLOSE_DRAIN`, normal
     PE-drain actions retire DMA tasks, and a later bounded setup TCP progress
     call finalizes once `import->inflightTaskCount == 0`. If teardown starts to
     contend with hotter work, split this into a separate scheduler fact/action
     without changing the engine-owned invariant.
   - Close finalization must call a scheduler-owned semantic reset before the DMA
     engine destroys descriptors. The implementation uses the TCP close-finalize
     callback in
     `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_setup_tcp.c:475`,
     maps the closing `(bridgeGeneration, clientInstanceId)` to descriptor
     `serviceSessionId` values through
     `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:3801`,
     and clears selected-session state in
     `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:34835`.
     The reset must fail, not silently discard work, if any matching command is
     still in flight, any backend-completion event is queued, or any matching
     staged dispatch/backend-command/frontend-response publication slot remains
     occupied.
   - The task-slot scan in
     `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:3254`
     should become a debug assertion/check against the per-import counter, not
     the production gate.

   Required invariant:

   ```text
   ACTIVE permits new DMA task submission.
   CLOSING forbids new submission and permits retirement only.
   CLOSING && inflightTaskCount == 0 permits scheduler semantic close reset.
   Semantic close reset clears selected-session state only when no unfinished
   semantic work remains for that session.
   Semantic close reset plus DPU import removal permits teardown ack.
   The host may destroy exported DOCA mmaps only after receiving teardown ack.
   ```

   Acceptance: closing a selected-DPU pgbench client/session produces an explicit
   teardown request and ack, DPU logs show import quiesce before host mmap
   destruction, and no grouped-control read to the closed import reaches
   `DOCA_ERROR_IO_FAILED`. Repeated selected-DPU open/close validation must also
   prove command sequence/frontier state does not leak into the next open if the
   current prototype reuses a `serviceSessionId`. The standalone TCP/DMA smoke
   passed the physical close/quiesce gate; installed PostgreSQL selected-DPU SQL
   must pass the semantic close/reopen gate as well.

### Stage 8A — Completion/result push mechanism

Deliverable: DPU DMA writes to host completion/result mailboxes.

Tasks:

- Map current frontend client completion mailbox semantics onto DPU-owned publish
  lines or ready epochs.
- Split DPU-mode completion polling from the old SHM completion mailbox path so a
  selected DPU session reads only DPU-published completion/credit lines.
- Add explicit DPU-mode completion errors for missing imported mmap, stale bridge
  generation, or absent DPU completion publication.
- Implement completion slot DMA write task owners.
- Submit completion/response body DMA writes before ready epoch/frontier
  publication-word DMA writes, but do not assume back-to-back submission is a
  host-visible publication guarantee under forced relaxed PCIe write ordering.
  For selected-DPU hot-path frontend completion publication, use the same async
  two-phase DMA-chain rule as Stage 8B.15: body completion callback submits the
  publication-word DMA, and publication completion retires the buffer.
- Update host frontend polling to validate DPU-owned credit/completion lines.
- Remove the selected-DPU completion dependency on host-process SHM completion
  mailboxes. The old mailbox code may remain for DPU-off mode, but DPU-mode
  command completion must be driven by DPU-published lines.
- Add a selected-DPU completion-path diagnostic or counter that proves command
  completion became visible through DPU-written publication lines, not through
  the SHM completion mailbox.
- The Stage 7B/8A "executed response pending DPU publication" queue is drained
  with same-context response-body DMA writes followed by response-ready
  publication-word DMA writes, but publication must be gated by response-body DMA
  completion. Use callback-chained publication for the hot path; do not spin in
  the submit action. Stage 8A must also own bounded queue retirement and failure
  handling; the queue is not a substitute for host-visible response publication.
- Treat Stage 8B promotion work as the completion replacement gate: selected-DPU
  completion polling must consume DPU-written completion/credit publication
  lines and must not use host-process SHM completion mailboxes after selection.
- Add Stage 8B promotion evidence to completion-path diagnostics: for at least
  one selected-DPU command, the request must arrive through DPU-pulled staging
  and the response must become visible through DPU-written body-plus-publication
  DMA, not through the host-process SHM completion mailbox.

Acceptance:

- Multi-state frontend completion sequence is observed in order.
- Host never consumes a completion whose slot body is stale or partially written.
- Split-context no-wait publication is rejected or gated.
- Same-context body+publish test validates the async two-phase chain: body DMA
  task submitted first, PE drain retires body completion, the body completion
  callback submits the publication-word DMA without calling `doca_pe_progress()`,
  and the host observes only the final publication word after the body is
  coherent.
- In DPU mode, completion polling consumes only DPU-published completion/credit
  lines. A missing or invalid DPU completion path must return a DPU-path error,
  not poll the old SHM completion mailbox as a fallback.
- Stage 8B acceptance must include one command path where the request arrives via
  DPU-pulled staging and completion becomes visible via the DPU publication
  line. A SHM-only command smoke is insufficient for this stage.
- Stage 8B is not accepted if a selected-DPU session can report command completion
  from the old host-process mailbox when the DPU completion path is absent,
  stale, or invalid.
- Stage 8B promotion evidence must include both a positive DPU completion
  publication run and a negative run where an absent/stale DPU completion path
  fails boundedly instead of falling back to SHM.

### Stage 8D — Selected-DPU local transaction fast-path removal

Deliverable: selected-DPU transaction validation cannot consume a same-host local
result queue as a shortcut. This is intentionally separate from Stage 9
basebackup/payload byte-stream work. Stage 9 concerns large/background byte
streams; this cleanup concerns pgbench-style transaction commands and their
tuple-result path.

Current code facts:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1057`
  binds one result sink per persistent client SQL session in
  `RemoteExecEnsureSessionResultQueue()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1157`
  already makes selected-DPU backend mode fail if the lifecycle-created result
  queue is missing, instead of opening the old host-service result sink.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:751`
  binds frontend command results in `BindRemoteExecutionCommandResult()`, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:823`
  drains them through `PollRemoteExecutionCommandResultBatch()`.

Implementation slices:

1. **Identify selected-DPU result-drain entry points.** Trace the installed
   selected-DPU pgbench SQL path from command completion through
   `BindRemoteExecutionCommandResult()` and
   `PollRemoteExecutionCommandResultBatch()`. Record which descriptor role,
   queue name, and result flags prove the result queue was lifecycle-created for
   selected-DPU mode.
2. **Add a selected-DPU guard.** In selected-DPU mode, result binding must reject
   a descriptor that belongs to the old host-process service or an unexported
   local-only queue. The guard may be a descriptor flag/role, a session/channel
   mode check, or an explicit selected-DPU result-queue identity carried in the
   completion; the important invariant is that selected-DPU result drain cannot
   silently fall back to the local host-service SHM result path.
3. **Keep DPU-off comparison behavior separate.** The existing local host-process
   result path may remain buildable for non-DPU comparison, but selected-DPU mode
   must either use the lifecycle-created DPU-visible result queue or fail
   boundedly.
4. **Add path evidence.** Add debug counters or logs that distinguish
   selected-DPU result binding/drain from host-process SHM result binding/drain.
   They should be cheap enough to leave disabled by default and easy to enable
   for validation.

Validation:

- Positive selected-DPU pgbench transaction smoke returns the expected tuple
  result after `START_COMMAND` is staged by DPU DMA and terminal completion is
  published by DPU DMA.
- Log/counter scan proves the selected-DPU result queue was the lifecycle-created
  DPU-visible queue and not a host-process service allocation.
- Negative selected-DPU run with the result-queue descriptor missing, stale, or
  marked non-DPU fails boundedly before result drain. It must not open or drain
  the old host-service result queue.
- DPU-off local Homer pgbench comparison still runs, if we intentionally keep
  that comparison path available during migration.

Implementation result on July 2, 2026:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:822`
  adds `HomerFrontendDmaValidateResultDescriptor()` for DOCA-enabled builds. It
  accepts only the receive-side descriptor for the lifecycle-owned selected-DPU
  SQL result byte ring. It intentionally ignores per-command fields such as
  `resultGeneration`, `startByteTail`, and `firstRingRecordOrdinal` because the
  backend refreshes those on every command result.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_dma.c:801`
  adds a non-DOCA fail-fast implementation so a selected-DPU result binding
  cannot silently succeed in a build that could not have created a real DPU
  channel.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:779`
  calls that validator before `BindRemoteExecutionCommandResult()` opens or
  resets the tuple-sink receive handle. A selected-DPU session therefore cannot
  reintroduce the local host-service SHM result fast path by accepting an
  arbitrary result descriptor.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:800`
  emits a `DEBUG1` path-evidence line with the selected-DPU queue name,
  generation, and start tail when validation succeeds.

Validation result on July 2, 2026:

- Host Citus/Homer build and install passed with `sudo -n -u dbcomm make -j8
  CPPFLAGS='-D_GNU_SOURCE'` and `sudo -n -u dbcomm make install-headers
  install-service-bin install`.
- PostgreSQL was restarted with `HOMER_FRONTEND_DPU_SETUP_HOST=10.10.1.201`,
  `HOMER_FRONTEND_DPU_SETUP_PORT=9727`,
  `HOMER_FRONTEND_DOCA_DEV_PCI=0000:21:00.0`, and
  `HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS=15000`.
- The DPU service was started from `/tmp/citus-dbcomm-stage8b16` with
  `HOMER_SERVICE_ENABLE_DPU_DMA=1`, `HOMER_SERVICE_ENABLE_DOCA_DMA=1`,
  `HOMER_SERVICE_DOCA_DEV_PCI=0000:03:00.0`, and setup TCP port `9727`.
- One selected-DPU SQL smoke with `client_min_messages=debug1` returned `279`
  and printed:

  ```text
  DEBUG:  validated selected-DPU command result descriptor
  DETAIL:  session=4359152199742370 command_sequence=3 queue=/citus_remote_exec_res_v14_4359152199742370 generation=3 start_tail=0
  ```

- A ten-call selected-DPU run returned `280` through `289`:

  ```sh
  sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql \
    -h /tmp -p 5432 -U dbcomm -v ON_ERROR_STOP=1 -AtX postgres \
    -c "SET client_min_messages = warning;
        SET citus.enable_experimental_tuple_sink_routing = on;
        SET citus.enable_experimental_homer_dpu_frontend = on;
        SELECT pg_catalog.citus_remote_exec_pgbench_transaction(1, 1, g, 1, 1)
        FROM generate_series(1, 10) g;"
  ```

- A DPU service log scan over `/tmp/homer_dpu_stage8d.log` found no `fatal`,
  `failed`, `error`, `stale`, `timeout`, `mismatch`, `invalid`, `poll`,
  `io_failed`, or `DOCA_ERROR` matches. A narrowed PostgreSQL log scan for
  Homer/selected-DPU/protocol lines was clean. The broad PostgreSQL log contains
  unrelated background Citus warnings for `10.10.1.100:5432`, so do not use that
  broad failure regex as selected-DPU evidence.
- A descriptor-corruption negative run was not added in this slice. The
  production mismatch path is implemented and fail-fast, but manufacturing a
  stale/missing descriptor would require an explicit debug/test hook or a
  purpose-built harness. Add such a hook deliberately if future promotion gates
  require runtime negative evidence.

### Stage 9 — Payload and basebackup byte-stream pull

Deliverable: DPU pulls backend-produced byte-ring records into DPU-local
mirrored rings and feeds existing service payload transport logic from those
mirrors.

Tasks:

- Register/import byte-ring host memory and allocate a DPU-local mirrored ring
  for each selected-DPU payload/basebackup byte stream.
- Require each DPU-mode payload/basebackup stream to be represented in the TCP
  setup descriptors and imported mmap state before the host publishes stream
  payload readiness.
- Discover byte-ring `publishedTail` via grouped-control polling. The accepted
  grouped-control snapshot is the only host-tail frontier the DPU may pull up
  to; Stage 9 must not use speculative byte DMA reads beyond that accepted host
  tail even if the host cannot overwrite beyond the last DPU-published head.
- Submit bounded payload DMA reads under payload grants using existing payload
  egress budget shape. DMA destinations are slices of the DPU-local mirrored
  ring, not transient copy-out buffers.
- Validate transport record headers/generations/checksums where available.
- Advance the DPU-local mirrored-ring tail only from DMA callbacks and only up
  to the contiguous completed frontier that is also at or below the accepted
  host tail.
- Publish consumed head to the host byte ring after the mirrored ring has safely
  accepted the copied bytes. This releases host producer space only; it does not
  release DPU mirror space.
- Reuse DPU mirrored-ring space only after the downstream consumer has released
  it. For RDMA-backed payload transport, that release point is the send-CQ
  completion that retires the DPU mirror range used as the RDMA source.
- Keep mixed foreground/background workload validation on the DPU boundary; do not
  use local SHM queues as the background-stream escape path in DPU mode.
- Replace any selected-DPU byte-stream placeholder that still routes through
  host-process SHM queues with host-published byte rings and DPU DMA pulls. The
  SHM byte-stream path remains useful only for DPU-off comparison during
  migration.
- Add payload/basebackup path counters that distinguish host-published byte-ring
  frontiers, payload DMA pulls, contiguous completed frontiers, and consumed-head
  DMA writes. These counters are part of the Stage 9 promotion evidence, not just
  performance instrumentation.
- Treat Stage 9 promotion work as the payload/basebackup replacement gate:
  selected-DPU byte streams must publish host byte-ring frontiers for DPU pull and
  must not use host-process SHM queues as an escape path for large transfers.
- Add Stage 9 promotion evidence to stream diagnostics: selected-DPU runs must
  report host byte-ring frontier publication, payload/basebackup DMA pulls,
  contiguous completion-frontier advancement, and consumed-head DMA publication.
  DPU-off SHM/RDMA stream counters remain comparison evidence only.
- Use the grouped-control mechanism already introduced for selected-DPU command
  discovery as the Stage 9 byte-stream discovery path:
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:740`
  `HomerDpuDmaSubmitGroupedControlReads()` submits grouped-control reads,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:6094`
  `HomerDpuDmaAcceptGroupedControlSnapshot()` accepts snapshots, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:3304`
  `HomerDpuDmaReadyQueueKindForDescriptor()` maps accepted descriptors into
  local ready work. Stage 9 extends that path for byte-stream frontier changes;
  it does not add a private payload polling loop.

Stage 9 design decisions as of 2026-07-02:

- **Mirror, not staging.** A pulled byte range lands directly in a DPU-owned
  mirrored ring. The earlier synthetic smoke used a temporary pull buffer and
  copy-out helper; that remains useful validation evidence but is not the
  production ownership model.
- **No post-DMA memcpy.** The DPU mirror is the DMA destination and the
  downstream payload/basebackup source. If the downstream transport is RDMA, the
  RDMA source should be the DPU mirror range itself after the DMA callback
  advances the mirror tail.
- **Be aggressive within accepted frontiers.** A payload pull action should pull
  the largest useful contiguous range allowed by the scheduler grant, DPU mirror
  free space, max DMA batch size, and accepted host tail. Split only on physical
  host-ring or mirror-ring wrap, so the normal case is one DMA task and the wrap
  case is at most two.
- **Pipeline discovery and pulls.** Grouped-control reads should stay ahead of
  payload pulls when grants allow it, so the service usually has a fresh accepted
  host-tail frontier by the time payload grant is available. The payload action
  still consumes only already accepted snapshots; it must not block waiting for a
  fresh grouped-control completion.
- **Separate release frontiers.** Host byte-ring credit and DPU mirror reuse are
  different lifecycle events. Host credit can be published after mirror
  acceptance because host bytes are no longer needed. DPU mirror space cannot be
  reused until the semantic/downstream consumer, including RDMA send-CQ
  retirement where applicable, releases the range.
- **Separate publication epochs by direction.** The host-owned
  `HomerDpuBridgeHostPublishLine.publishedEpoch` and the DPU-owned
  `HomerDpuBridgeDpuCreditLine.publishedCreditEpoch` are separate monotonic
  publication streams. The DPU must not reuse a host publication epoch when it
  publishes consumed-head credit, because the DPU may legitimately publish
  multiple partial consumed-head frontiers while processing one accepted host
  byte-ring tail. Each DPU credit publication gets a fresh DPU credit epoch.
  The symmetric rule applies to host publication: a host tail publication is a
  host-side event and should not be coupled to DPU credit epochs.
- **Corrected byte-ring wrap protocol: prove gaps from regular
  headers/frontiers, not stale bytes.** A selected-DPU byte-ring consumer must
  never treat an arbitrary nonmatching DMA-read header as a wrap gap. The
  producer-side protocol should make the gap provable:
  - If the pre-wrap trailer has room for a complete
    `CitusTupleSinkTransportHeader`, the producer writes the regular transport
    header for the next record at the pre-wrap cursor before writing the same
    record at ring offset zero. The pre-wrap header is not a padding record, a
    distinct record kind, or a separate semantic object; it is a normal header
    whose `headerBytes + payloadBytes` describes the next record. When that
    record length exceeds the remaining trailer bytes, the consumer has a
    deterministic proof that the record cannot start at the current physical
    cursor and that the valid record body starts after wrap at offset zero.
  - If the trailer is smaller than a transport header, the producer writes no
    header; the consumer may skip only after the accepted absolute host tail has
    crossed the physical ring boundary.
  - The DPU parser may inspect a transport header only when the DMA-mirrored
    range contains the full header. A short header at the end of a staged range
    requests another DMA append unless the physical trailer itself is smaller
    than a header and the accepted tail proves wrap.
  - The accepted host tail from grouped-control discovery is the producer
    frontier, but a scheduler-granted DMA pull may expose only a capped local
    mirror frontier. The parser must therefore use both bounds: it may consume
    only bytes at or below the completed DPU mirror tail, and it may prove wrap
    only against the accepted host tail. A normal record whose full transport
    header is mirrored but whose `headerBytes + payloadBytes` extends beyond the
    completed mirror range is a torn local view, not a bad producer record; the
    action requests a DMA append and stops without advancing host credit past
    bytes it has not mirrored.
  - A complete, matching header whose advertised record length fits before the
    physical boundary but whose body is incomplete is likewise an incomplete
    local mirror range. A complete, matching header whose advertised record
    length does not fit before the physical boundary is the regular pre-wrap
    proof described above. A complete, nonmatching header is a protocol or
    visibility bug and must fail loudly; it must not be skipped as a gap.
  - After wrap is proven, the DPU records a local source-skip frontier and
    advances only the source-byte/credit frontier through the gap. It must not
    advance the semantic record frontier for the gap. Source-skip represents
    physical byte-ring trailer gaps only; it must not be reused to hide
    producer-side slack inside a synthetic record envelope.

  This supersedes the temporary selected-DPU padding-record direction. Do not
  make `CITUS_TUPLE_SINK_RECORD_KIND_PADDING` part of Stage 9 acceptance, and do
  not use `CITUS_TUPLE_SINK_TRANSPORT_FLAG_WRAP` for this protocol. The
  provisional flag value overlapped the existing EOS flag before removal; even
  if that overlap were fixed, no extra wrap flag is needed because the regular
  header length compared with the physical trailer length is the
  publication-independent wrap proof.
- **Reject fixed basebackup producer envelopes.** The selected-DPU byte-ring
  protocol does not require fixed-size source objects. The transport header is
  fixed-size, and its `headerBytes + payloadBytes` value should truthfully
  describe the complete source record. If that record cannot fit before the
  physical ring end, the producer writes a regular pre-wrap transport header
  when a full header fits, then starts the real record at offset zero. A fixed
  8 MiB source envelope was useful as Stage 9 bring-up scaffolding because the
  generic PostgreSQL `bbsink` contract exposes `bbs_buffer` before
  `archive_contents(len)` reports the actual payload length, but it is not an
  acceptable steady-state protocol: it wastes ring/DMA bandwidth, forces the DPU
  egress path to distinguish source-envelope slack from semantic data, and makes
  RDMA fragment LAST publication differ from source-byte retirement. Stage 9 is
  not accepted until selected-DPU basebackup uses exact source-record sizes.
- **Keep resource-moving work grant-bounded.** `DPU_PAYLOAD_PULL` remains a
  scheduler-granted action because it spends PCIe/DMA bandwidth, DOCA task
  slots, and DPU mirror capacity. `DPU_CONSUMED_HEAD_PUBLISH` also remains a
  scheduler-granted action for now, even though it is small, because it submits
  DPU-to-host DMA tasks and competes for the same DOCA context/window resources
  as command/completion work. By contrast, DPU-local mirror release is not a
  separate grant: when the existing payload send-CQ drain retires the RDMA batch
  whose source was a borrowed mirror range, that callback should release the
  range and advance the DPU mirror free/head frontier immediately.
- **Generalize payload source ownership before enabling DPU payload egress.**
  The current outgoing byte-ring sender reads from `sendQueue` and posts RDMA
  using `sendQueueMemoryRegionHandle` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:27251`
  `HomerServicePumpOutgoingByteRingPayload()`. Stage 9 should introduce a
  payload-source span abstraction that can represent either the existing
  `sendQueue` source or a borrowed `HomerDpuDmaMirroredByteRange` from
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:177`.
  The selected-DPU path must post RDMA directly from the borrowed mirror memory
  and must not copy mirror bytes into `sendQueue`.
- **Remove selected-DPU `sendQueue` dependency as the completion gate.**
  During implementation it is acceptable for the generic source-span code to
  support both the existing `sendQueue` source and the borrowed DPU mirror source,
  because that keeps the non-DPU comparison path alive while the shared RDMA
  posting machinery is being generalized. Stage 9 is not complete, however, until
  selected-DPU payload/basebackup streams do not allocate, populate, read from,
  register, or retire `stream.sendQueue` for their hot payload egress path.
  `sendQueue` may remain only for DPU-off comparison paths or for unrelated
  non-selected streams.
- **Make send-CQ identity slot-local, not stream-id-scanned.** Payload send-CQ
  completions should identify the local stream slot and local owner token
  directly. The implemented layout in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3189`
  `TupleSinkServiceEncodePayloadWrId()` is: payload tag, completion kind, 6-bit
  stream table index, 16-bit stream generation, and 40-bit local completion
  token. `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22046`
  `HomerServicePayloadStreamSendCqIdentity()` derives the compact identity from
  the active stream table entry, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13658`
  `HomerServiceHandlePayloadSendCqeFromDrain()` validates the decoded generation
  before retiring owner state. This replaces a hot scan by `serviceStreamId` and
  avoids treating the monotonically allocated service stream id as a dense
  transport index.
- **Use owner tokens for receiver-head ACK completions.** Receiver-head ACK WR ids
  should carry a local ACK-owner token, not the consumed-head byte frontier. The
  consumed head remains the 64-bit value written to the peer and is stored in the
  ACK owner slot; the CQE token only identifies which local source word/owner can
  be retired. The implemented state lives in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1636`
  (`receiverHeadAckNextToken`, `receiverHeadAckCompletedToken`, and
  `receiverHeadAckTokens[]`). `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:29124`
  `HomerServiceTrackReceiverHeadAckOwner()` assigns the token, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:29211`
  `HomerServiceRetireReceiverHeadAckOwners()` requires the CQE token to match
  the next FIFO ACK owner exactly, then advances `receiverHeadAckCompletedHead`
  from the owner slot's stored consumed-head value. This keeps DATA and ACK
  send-CQ retirement on the same `{streamIndex, generation, ownerToken}` model
  and prevents byte-frontier size or wrap behavior from consuming WR-id bits.

Implementation slices:

1. **Promote smoke helpers into production APIs.** Replace or wrap
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2232`
   `HomerDpuDmaSubmitByteRingSmokePull()`,
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2374`
   `HomerDpuDmaCopyByteRingSmokeBuffer()`, and
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2429`
   `HomerDpuDmaSubmitByteRingConsumedHeadSmokePublication()` with production
   APIs that express:
   - a bounded byte-ring pull range,
   - an engine-owned mirrored-ring destination range,
   - completed contiguous mirrored-ring tail advancement from callbacks, and
   - consumed-head publication after mirror acceptance.
   The smoke copy-out helper should not be used as the production handoff.
2. **Add production mirrored-ring ownership.** A pulled range moves from
   host-owned byte-ring memory to DPU-engine-owned mirror memory. The host
   byte-ring source may receive consumed-head credit only after the service has
   accepted the bytes into the DPU mirror. If RDMA transport uses the mirrored
   range as a source, the mirrored range remains owned by the DPU engine until
   the existing send-CQ completion retires that source range. This mirrors the
   existing service payload-source ownership pattern around
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1503`
   `HomerPayloadStreamState`, but the selected-DPU source is a DPU mirror rather
   than the current in-process `sendQueue`.
3. **Wire scheduler actions.** Replace the placeholder branch at
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:38381`
   for `HOMER_PROGRESS_ACTION_DPU_PAYLOAD_PULL` and
   `HOMER_PROGRESS_ACTION_DPU_CONSUMED_HEAD_PUBLISH` with bounded grant
   execution. These actions must submit at most the granted work, report
   `stillReady`/`budgetExhausted` when accepted host frontiers, mirror free
   space, in-flight DMA slots, or consumed-head publications still have work,
   and return on resource pressure rather than spinning.
4. **Add payload source spans for zero-copy DPU mirror egress.** The outgoing
   payload sender should stop assuming that its source is always
   `stream.sendQueue`. Add a small source-span interface that carries the local
   address, byte count, local memory-region handle, source frontier, source kind,
   and release metadata. Existing SHM/host-process payloads fill the span from
   `sendQueue`; selected-DPU payloads fill it by claiming
   `HomerDpuDmaMirroredByteRange`. The common RDMA posting path should keep the
   existing remote-credit, batching, tail-publication, and send-owner mechanics.
5. **Extend payload send-owner retirement for borrowed mirror ranges.** The
   payload send-owner slot currently tracks completion tokens, byte tails,
   semantic tails, and WR counts around
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15086`
   `HomerServiceCompleteTrackedPayload()`. Add source-kind-specific ownership so
   a send-CQ completion for a selected-DPU payload batch releases the borrowed
   mirror range with
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2983`
   `HomerDpuDmaReleaseMirroredByteRange()`. This release is part of the existing
   `PAYLOAD_SEND_CQ` resource-relief action, not a new DPU scheduler grant.
5a. **Replace all-or-nothing mirror release with prefix release/resume.** Stage
   9.9 proved the direct DPU-mirror RDMA source path, but its all-or-nothing
   release is too restrictive for large records, remote-credit limits, and WR
   budget limits. Add a DMA-engine API such as
   `HomerDpuDmaReleaseMirroredByteRangePrefix(engine, range, releaseEnd, ...)`.
   It should validate that `releaseEnd` is monotonic and lies in the claimed
   range, advance the staged range's absolute start and local data offset by the
   released byte count, and free/requeue the mirror buffer only when
   `releaseEnd == absoluteEnd`. A retryable zero-WR post failure still calls
   `HomerDpuDmaUnclaimMirroredByteRange()` and releases nothing.
   Implementation progress: Stage 9.10 has implemented this API in
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c`
   and validated prefix/suffix resume in the grouped-control DPU DMA smoke.
5b. **Store prefix ownership in payload send-owner slots.** The owner slot for a
   selected-DPU DATA post must store the exact source prefix posted by that RDMA
   batch, not the whole borrowed mirror range returned by the claim API.
   Send-CQ retirement releases that prefix and advances host consumed-head credit
   only to the retired prefix frontier. The next borrow of the same staged
   mirror buffer resumes from the unreleased suffix. Partial/poisoned
   `ibv_post_send()` failures still follow reset/abort cleanup after the QP is
   fenced; cleanup should release only the accepted prefix that can no longer be
   retried.
   Implementation progress: Stage 9.10 stores the posted prefix in the
   `DPU_MIRROR` payload send-owner metadata. The helper still publishes only
   complete-record prefixes; record-fragment generation remains the next Stage 9
   work.
5c. **Keep peer publication at transport frontiers.** RDMA writes may be split
   at arbitrary byte sizes for WR budget, remote-ring wrap, and source mirror
   prefix boundaries, but the peer-visible published tail must advance only to a
   valid transport-record frontier under the current receiver protocol. Large
   basebackup records should be expressed as existing transport fragments from
   borrowed mirror storage, with generated fragment transport headers when
   needed, rather than by publishing a peer tail into the middle of a whole-record
   transport envelope.
   Implementation progress: Stage 9.11 adds DPU-mirror basebackup fragment
   generation in
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
   It uses the existing RDMA connection's generated-header pool for transport
   fragment headers while the payload SGE points directly into the borrowed
   DPU-local mirror range.
6. **Build real ready facts.** Grouped-control discovery should turn accepted
   `PAYLOAD_BYTE_RING` frontier changes into exact local ready entries keyed by
   import id, ring index, generation, absolute start, and accepted tail. Duplicate
   discovery of an unchanged frontier must not enqueue duplicate work. Any
   helper currently specialized around command-tail requeue state, such as
   pulled-command-tail bookkeeping, must be generalized before it is reused for
   byte streams.
7. **Connect real producers.** DPU-mode basebackup/payload streams must export
   their producer byte rings in TCP setup before publishing payload readiness.
   Missing, stale, or generation-mismatched setup is a selected-DPU path error,
   not a reason to use host-process SHM queues.
7a. **Migrate the basebackup producer/open path to selected-DPU setup.** Runtime
   validation exposed that this was implied by Stage 9 but not explicit enough:
   `pg_basebackup` still reaches
   `/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:254`
   `HomerClientOpenControl()`, whose implementation at
   `/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:286` opens the old local
   host-service control SHM region. Stage 9 cannot be accepted until the
   basebackup Homer client has a selected-DPU open path that:
   - creates or reuses the existing producer byte-ring layout used by
     `/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1744`
     `HomerClientOpenBaseBackupStream()`,
   - exports that byte ring and its control/credit metadata through the
     host-side DOCA mmap export device,
   - sends one bounded TCP setup message to the DPU setup listener with a
     `HOMER_DPU_BRIDGE_DESCRIPTOR_ROLE_PAYLOAD_BYTE_RING` descriptor and the
     basebackup stream identity,
   - binds the existing basebackup reserve/submit code to DPU-published credit
     instead of local service-updated SHM credit, and
   - fails explicitly if selected-DPU setup is disabled, stale, or rejected.

   This should be implemented by factoring the reusable TCP setup / DOCA mmap
   export pieces out of `homer_frontend_dma.c` into a frontend-neutral helper
   that can be used by both the SQL command-session frontend and
   `homer_client.c`. The helper must not depend on PostgreSQL backend-only
   allocation or error-reporting APIs. The selected-DPU basebackup path should
   not call `HomerClientOpenControl()` and should not allocate a local
   host-service stream slot as a hidden fallback. If the existing producer
   byte-ring control word layout cannot directly host DPU credit publication,
   add an explicit mapping from the DPU credit line to the producer reservation
   logic rather than reintroducing service SHM control.
7b. **Make selected-DPU basebackup open externally blocking but internally
   scheduler-progressed.** Remote stream open may need peer RDMA connection
   setup, memory registration, local sender-head mirror setup, peer-open
   request publication, and peer response consumption. The PostgreSQL caller may
   block in `HomerClientOpenBaseBackupStreamSelectedDpu()` until that work is
   complete, but the service must represent the internal work through the
   existing local-control async machine and DPU response-owner publication path,
   not by spinning inside a single handler. The same rule applies to remote
   transaction/session opens. Acceptance for this substage requires evidence
   that a DPU-pulled basebackup `OPEN_SESSION` that takes the async path later
   publishes its open response through the selected-DPU DMA response path, with
   no SHM control-slot fallback.
7c. **Replace selected-DPU padding records with the corrected wrap protocol.**
   Remove the selected-DPU-only padding-marker path from the basebackup producer
   and DPU mirror parser. Implement the regular-header wrap proof described in
   the design decisions above:
   - reserve basebackup records with their exact transport-record size:
     `sizeof(CitusTupleSinkTransportHeader)` plus the exact semantic basebackup
     header bytes plus the exact payload bytes;
   - when wrapping and at least one full transport header fits in the trailer,
     write and flush the regular next-record transport header at the pre-wrap
     cursor before publishing the host tail, then write the complete record at
     ring offset zero;
   - when less than one transport header fits, publish no gap header and rely on
     accepted absolute tail crossing the physical boundary;
   - teach the DPU mirror parser to request DMA append for incomplete headers,
     parse only full mirrored headers, enter local source-skip state for proven
     gaps, and release source-byte credit through that source-skip frontier
     without advancing semantic tail;
   - distinguish the uncapped accepted host tail from the completed capped DPU
     mirror tail. The parser may use the accepted tail to prove a wrap gap, but
     it may parse or forward only bytes that are present in the local mirror;
   - for selected-DPU basebackup, source-record length and semantic object
     length should agree after the transport header; downstream RDMA
     fragmentation may still regenerate fragment transport headers, but there is
     no source-envelope slack to retire separately;
   - reject nonmatching full headers whose regular record length does not prove
     wrap as protocol errors, rather than skipping them as gaps.

   Acceptance for this substage requires a synthetic selected-DPU byte-ring
   smoke with: no wrap, wrap with trailer smaller than a header, wrap with a
   complete pre-wrap regular header, a DMA segment ending inside a large wrap
   gap, a DMA segment ending inside a transport header, and a large basebackup
   record fragmented downstream while source-byte credit advances independently
   from semantic tail. The production runtime gate is a selected-DPU remote RDMA
   basebackup run that completes through the DPU mirror-only path with no
   selected-DPU `sendQueue` fallback, no fixed producer envelope, and no
   protocol-reset, stale-header, or DOCA task failure logs.
7d. **Move selected-DPU basebackup reservation into the producer loop.** The
   generic PostgreSQL `bbsink` contract in
   `/data/dbcomm/postgres-citus/src/include/backup/basebackup_sink.h:127`
   requires callers to write into `sink->bbs_buffer` before invoking
   `archive_contents(len)`. The current Homer sink therefore reserves early in
   `/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:267`
   `bbsink_homer_begin_archive()` and only learns the actual payload length later
   in `/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:285`
   `bbsink_homer_archive_contents()`. That ordering caused the fixed-envelope
   workaround. Replace it with a selected-DPU producer-loop path:
   - add a Homer-specific exact-reserve helper that takes object kind, optional
     object name, archive index, stream offset, and exact payload bytes, then
     returns the byte-ring payload pointer for that exact record;
   - in archive read/copy sites such as
     `/data/dbcomm/postgres-citus/src/backend/backup/basebackup.c:566`, compute
     the intended read length first, reserve an exact Homer record, read directly
     into the returned payload pointer, and publish the exact record after the
     read succeeds;
   - in buffered tar/manifest helper paths such as
     `/data/dbcomm/postgres-citus/src/backend/backup/basebackup.c:1960`
     `push_to_sink()`, preserve the generic sink path for non-Homer targets but
     route selected-DPU Homer targets through exact reserve/write/publish helpers
     so the ring location is chosen after the byte count is known and before
     bytes are written;
   - keep metadata-only BEGIN/END/archive-boundary objects on the same exact
     reservation API with payload length zero;
   - remove selected-DPU use of `stream->queueDescriptor.slotCapacityBytes` as
     the source transport envelope in
     `/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2786`
     `HomerClientReserveBaseBackupRecordForObject()` and
     `/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2989`
     `HomerClientSubmitBaseBackupRecord()`;
   - simplify the DPU egress parser in
     `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16587`
     `HomerServiceAppendOutgoingDpuMirrorBaseBackupFragmentWrite()` so LAST is
     based on the truthful transport record payload, not a separate semantic
     length hidden inside a larger source envelope;
   - keep source-skip only for proven physical wrap gaps in
     `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:28830`
     `HomerServicePumpOutgoingDpuMirrorByteRingPayload()`.

   Acceptance for this substage requires: selected-DPU basebackup validation with
   exact source-record sizes, a forced small final archive/manifest object near a
   physical ring end, a large fragmented basebackup object, and log evidence that
   no fixed-envelope slack/source-skip path ran for semantic objects. If a generic
   bbsink-only fallback remains temporarily for non-selected-DPU targets, it must
   be unreachable for selected-DPU basebackup.
8. **Retire selected-DPU `sendQueue` usage.** After source spans and borrowed
   mirror send-owner retirement are working, remove the selected-DPU payload path
   dependency on `stream.sendQueue` and `sendQueueMemoryRegionHandle`. This is
   the final Stage 9 completion gate: selected-DPU payload egress must use DPU
   mirror memory as the RDMA source, while `sendQueue` remains only for DPU-off
   or non-selected comparison paths.
9. **Add counters and diagnostics.** Track host-published byte-ring frontiers,
   payload DMA pull submissions, callback-completed bytes, mirror-accepted bytes,
   consumed-head publications, mirror-free-space blocks, borrowed-mirror ranges,
   send-CQ mirror releases, DMA-slot pressure, and resource-pressure skips.
   These counters are the Stage 9 path evidence.

Acceptance:

- Single stream transfers records without stale/torn reads.
- Ring wrap is handled with one DMA range in the contiguous case and at most two
  DMA ranges when the host ring or DPU mirror wraps.
- No payload DMA reads bytes beyond the latest accepted grouped-control host-tail
  frontier.
- The DPU mirrored-ring tail never advances beyond both the contiguous completed
  DMA frontier and the accepted host-tail frontier.
- The payload path does not introduce an extra memcpy after DMA completion; the
  DMA destination is DPU mirror storage, and downstream payload/RDMA logic reads
  from that mirror storage.
- Selected-DPU payload/basebackup egress does not allocate, populate, read from,
  register, or retire `stream.sendQueue`; any remaining `sendQueue` usage is
  limited to DPU-off comparison paths or unrelated non-selected streams.
- A selected-DPU payload send owner cannot retire without releasing its borrowed
  mirror range, and mirror range release cannot happen before the send-CQ
  completion for the RDMA batch that used that range as source.
- Basebackup-like large stream uses bounded windows and does not starve command
  collectors.
- Synthetic byte-ring pull test runs before real basebackup: host publishes
  generated records, DPU pulls only bytes at or below the accepted frontier,
  callbacks advance the completed contiguous frontier, and consumed-head
  publication happens only after semantic release.
  Implementation progress: Stage 9.1 has passed this synthetic gate for a
  wrapped 64-byte payload over the host-DPU TCP setup smoke. The accepted run
  validates grouped-control discovery of a `PAYLOAD_BYTE_RING`, two host-to-DPU
  payload DMA reads for wrap split, and two-phase DPU-to-host consumed-credit
  publication. That smoke used a temporary pull buffer; real selected-DPU
  basebackup/result byte-stream integration must replace it with the DPU-local
  mirrored-ring handoff, production pull-window scheduling, and payload counters.
  Implementation progress: Stage 9.2 has passed a compile/smoke gate for the
  engine-side mirrored-range API. The added API is
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:177`
  `HomerDpuDmaMirroredByteRange`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2617`
  `HomerDpuDmaSubmitMirroredByteRingPulls()`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2795`
  `HomerDpuDmaSubmitMirroredByteRingConsumedHeadPublications()`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2913`
  `HomerDpuDmaClaimNextMirroredByteRange()`, and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2983`
  `HomerDpuDmaReleaseMirroredByteRange()`. This slice also generalized
  byte-stream ready-frontier validation away from command-tail-only bookkeeping
  through
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:3801`
  `HomerDpuDmaDescriptorIsHostToDpuByteRing()` and the byte-stream
  `completedByteTail` path. This slice intentionally kept scheduler payload
  actions disabled until the mirrored-range API and consumed-credit publication
  protocol were validated.
  Implementation progress: Stage 9.3 added and passed a standalone DOCA
  correctness smoke for the mirrored-range API. The validation lives in
  `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_dma_smoke.c:40` and uses
  a four-descriptor synthetic bridge with a `PAYLOAD_BYTE_RING` at ring index 3.
  With `HOMER_DPU_DMA_SMOKE_RUN_GROUPED_CONTROL_READ=1`, it verifies grouped
  control discovery of the payload host-publish line, a wrapped 64-byte
  published range over a 128-byte host ring, two 32-byte mirrored DMA pulls,
  byte-pattern equality from the borrowed mirror ranges, mirror release/requeue,
  and per-segment consumed-head publication into the DPU credit line. The
  current smoke checks that the first 32-byte mirror segment can be released as
  a 16-byte prefix plus a 16-byte suffix: DPU credit epoch 1 publishes
  `consumedHead=112`, epoch 2 publishes `consumedHead=128`, and the second
  32-byte segment publishes epoch 3 with `consumedHead=160`; see
  `/data/dbcomm/citus-dbcomm/src/bin/homer_service_dpu_dma_smoke.c:397`.
  This validation also exposed a protocol nuance that has since been corrected:
  consumed-head credit publication must use a DPU-owned monotonic
  `publishedCreditEpoch`, not the accepted host publication epoch. With separate
  host and DPU publication streams, the DPU may publish partial consumed-head
  credit for each completed mirrored frontier, and the host can observe each
  update as a distinct DPU publication event.
  Implementation progress: Stage 9.4 wired the current scheduler actions for
  mirrored payload movement. The placeholder branch at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:38381`
  now maps `HOMER_PROGRESS_ACTION_DPU_PAYLOAD_PULL` to
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2617`
  `HomerDpuDmaSubmitMirroredByteRingPulls()` and
  `HOMER_PROGRESS_ACTION_DPU_CONSUMED_HEAD_PUBLISH` to
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2795`
  `HomerDpuDmaSubmitMirroredByteRingConsumedHeadPublications()`. Both actions
  are bounded by the scheduler grant, submit at most the granted task count, and
  propagate the engine's `stillReady` and `budgetExhausted` signals back through
  the current action-grant feedback path. Validation on July 2, 2026:
  `sudo -n -u dbcomm make -j8 CPPFLAGS='-D_GNU_SOURCE'` in
  `/data/dbcomm/citus-dbcomm` passed, including the default
  `build/homer/homer_service_dpu_dma_smoke`; then the grouped-control smoke
  command
  `sudo -n -u dbcomm env HOMER_DPU_DMA_SMOKE_RUN_GROUPED_CONTROL_READ=1 ./build/homer/homer_service_dpu_dma_smoke`
  passed. This is a scheduler wiring
  and engine-regression gate, not the final egress gate: selected-DPU payload
  egress still must borrow mirrored ranges as RDMA sources and retire them on
  send-CQ completion before Stage 9 is accepted.
  Implementation progress: Stage 9.5 added payload send-owner source metadata
  as the first egress handoff scaffold. The send-owner ring in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1583`
  now stores a `HomerPayloadSendOwnerSourceKind` and optional
  `HomerDpuDmaMirroredByteRange` per owner. Existing byte-ring sends still
  reserve owners as `SEND_QUEUE` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14906`
  `HomerServiceReservePayloadSendOwner()`, while commit, CQ retirement, rollback,
  and abort cleanup validate and clear the source metadata. This makes the next
  selected-DPU egress slice mechanical: when a batch posts RDMA from a borrowed
  mirror range, the owner slot must be retagged as `DPU_MIRROR`, and the
  send-CQ retirement path must release that range instead of updating
  `sendQueue` credit. Validation on July 2, 2026: the same
  `sudo -n -u dbcomm make -j8 CPPFLAGS='-D_GNU_SOURCE'` build and grouped-control
  smoke command passed after the scaffold landed.
  Implementation progress: Stage 9.6 made send-CQ and abort cleanup
  source-aware. The shared release helper at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14926`
  `HomerServiceReleasePayloadSendOwnerSource()` no-ops for `SEND_QUEUE` owners
  and releases `DPU_MIRROR` owners through
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2983`
  `HomerDpuDmaReleaseMirroredByteRange()`. The byte-ring send-CQ callback at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13655`
  `HomerServiceHandlePayloadSendCqeFromDrain()` now passes the DPU DMA engine
  into
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15301`
  `HomerServiceApplyTrackedPayloadCompletionFrontier()`, and the abort path at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25310`
  `HomerServiceAbortTrackedPayloadSendOwners()` uses the same release helper.
  The retirement path also rejects a single cumulative CQ frontier that mixes
  `SEND_QUEUE` and `DPU_MIRROR` owners, because crossing that source boundary
  should be an explicit stream transition. Validation on July 2, 2026: the
  `sudo -n -u dbcomm make -j8 CPPFLAGS='-D_GNU_SOURCE'` build and the grouped-control
  mirrored-ring smoke both passed.
  Implementation progress: Stage 9.7 exposed the DPU-local byte-stream mirror
  allocation for later RDMA registration. The new API
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.h:286`
  `HomerDpuDmaGetByteStreamMirrorMemory()` returns the stable engine-owned
  `byteStreamBuffers` base and byte length, implemented at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2912`.
  This keeps ownership with the DMA engine while allowing the service to
  register one contiguous DPU-local mirror region with ibverbs and post
  downstream RDMA writes directly from borrowed `HomerDpuDmaMirroredByteRange`
  addresses. Validation on July 2, 2026: the no-stats Citus/Homer build and the
  grouped-control mirrored-ring smoke both passed.
  Implementation progress: Stage 9.8 replaced payload send-CQ `serviceStreamId`
  scans with compact stream identity in the verbs WR id. The encoding in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3189`
  carries payload tag, completion kind, 6-bit stream table index, 16-bit stream
  generation, and a 40-bit local completion token. The send-CQ callback at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13658`
  now direct-indexes the stream table and validates the generation before
  retiring DATA or receiver-head ACK owners. Receiver-head ACK completions use
  owner tokens instead of byte frontiers as their CQE identity; a duplicate,
  stale, or skipped ACK token is a protocol error, not a harmless duplicate.
  Validation on July 2, 2026: no-stats build/install passed, artifacts were
  synced to farnet0, remote farnet0-to-farnet1 Homer pgbench completed
  `1000/1000` transactions with zero failures, remote RDMA basebackup blackhole
  completed successfully, and fresh Homer service log scans on both hosts had
  zero send-CQ/token/DOCA/error matches. The implementation checkpoint records
  the exact commands and observed outputs.
  Open issue before the next egress slice: the service must preserve transport
  record boundaries when forwarding borrowed mirror ranges. The current mirror
  pull path at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2617`
  `HomerDpuDmaSubmitMirroredByteRingPulls()` stages raw contiguous byte ranges
  and caps one pull at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:38`
  `HOMER_DPU_DMA_BYTE_STREAM_BUFFER_BYTES` (64 KiB), with the cap applied at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:2720`.
  The existing sender at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:27406`
  `HomerServicePumpOutgoingByteRingPayload()` parses complete transport records,
  and its basebackup branch uses
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16158`
  `HomerServiceAppendOutgoingBaseBackupFragmentWrite()` when one semantic object
  needs fragmentation. A selected-DPU egress implementation that blindly RDMA
  writes one borrowed mirror byte segment can split a large record in the middle
  and publish an invalid peer byte-ring frontier.
  Implementation progress: Stage 9.9 implements the constrained first selected-DPU
  egress slice. `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:28357`
  `HomerServicePumpOutgoingDpuMirrorByteRingPayload()` parses a completed
  `HomerDpuDmaMirroredByteRange`, posts peer RDMA writes directly from the
  registered DPU mirror allocation, and attaches the borrowed range to the
  payload send-owner so `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14986`
  releases it only during send-CQ retirement or abort cleanup. It deliberately
  claims a mirror range only when the whole staged range can be consumed as
  complete transport records; retryable post failure calls
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:3190`
  `HomerDpuDmaUnclaimMirroredByteRange()` instead of releasing host credit.
  A trailing producer wrap gap can be claimed and released without an RDMA
  payload post, but real partial records remain deferred to the next Stage 9
  substep. `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:28927`
  dispatches ready selected-DPU byte-ring streams to this mirror-source helper
  before registering the legacy `sendQueue`.
  Validation on July 2, 2026: the no-stats Citus/Homer service/client build,
  explicit `service-dpu-dma-smoke` rebuild, grouped-control mirrored-ring smoke,
  and `git diff --check` passed. This is a compile/engine-smoke gate, not yet
  the Stage 9 runtime acceptance gate for remote basebackup over the selected-DPU
  mirror path.
  Design correction after Stage 9.9: the next implementation direction is
  **prefix release/resume**, not all-or-nothing mirror release and not
  record-aware DMA pulls. A borrow describes the currently staged DPU-local
  bytes, but send-CQ retirement should be allowed to release only the prefix
  that was actually posted and retired downstream. The unreleased suffix remains
  staged and the next borrow resumes from that new local/absolute start. This
  decouples borrow granularity from release granularity and lets the RDMA egress
  path spend arbitrary byte-sized WR budget without copying split records into
  `sendQueue`.
  The key invariant is that arbitrary RDMA WR fragmentation is not the same as
  arbitrary peer-visible publication. The peer receiver still expects published
  byte-ring frontiers to expose parseable transport records through
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:30272`
  `HomerServicePayloadTransportHeaderReady()` and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:30425`
  `HomerServicePumpIncomingByteRingPayload()`. Therefore the egress path may
  split one transport record across several RDMA WRs internally, but the final
  WRITE_WITH_IMM / published-tail update must advance only to a valid transport
  frontier, or the receiver protocol must first grow explicit partial-record
  buffering. For this stage, keep the receiver protocol unchanged and implement
  prefix release plus existing transport fragmentation semantics.
  Implementation progress: Stage 9.10 implements prefix release/resume and
  prefix send-owner storage. The DMA engine now exposes
  `HomerDpuDmaReleaseMirroredByteRangePrefix()`, resumed borrows point at
  `byteBuffer->bytes + dataOffset`, and the selected-DPU mirror egress helper
  stores only the posted prefix in the send-owner. Validation on July 2, 2026:
  the no-stats Citus/Homer service/client build, explicit
  `service-dpu-dma-smoke` rebuild, grouped-control mirrored-ring smoke with a
  16-byte prefix release, and `git diff --check` passed. Remaining work is
  borrowed-mirror transport fragmentation for records that cross mirror or
  grant boundaries, plus runtime selected-DPU payload/basebackup validation.
  Implementation progress: Stage 9.11 implements borrowed-mirror basebackup
  fragment generation. `HomerServiceAppendOutgoingDpuMirrorBaseBackupFragmentWrite()`
  emits generated FIRST/continuation/LAST transport headers from the RDMA
  connection header pool, uses DPU-local mirror bytes as the payload SGE, and
  advances source-byte tails after every posted prefix while leaving the semantic
  tail unchanged until LAST. Validation on July 2, 2026: the no-stats
  service/client build, explicit `service-dpu-dma-smoke` rebuild,
  grouped-control mirrored-ring smoke, and `git diff --check` passed. Remaining
  work is real selected-DPU payload/basebackup runtime validation and removal of
  selected-DPU hot-path `sendQueue` dependency.
  Implementation progress: Stage 9.12 removes the selected-DPU byte-ring egress
  fallback to the legacy host-process `sendQueue` source. The new helper
  `HomerServicePayloadStreamUsesDpuMirrorSource()` makes selected-DPU
  byte-ring readiness, scheduler hints, stream-open registration, and outgoing
  dispatch mirror-only: if no completed DPU mirror range is ready, the payload
  stream waits for `DPU_PAYLOAD_PULL`/PE-drain progress instead of registering or
  reading `stream.sendQueue`. `TupleSinkServiceEnsureSendQueueMemoryRegion()`
  now fails explicitly if a selected-DPU byte-ring stream reaches legacy source
  registration. Validation on July 2, 2026: the no-stats service/client build,
  explicit `service-dpu-dma-smoke` rebuild, grouped-control mirrored-ring smoke,
  and `git diff --check` passed. This is still not full Stage 9 acceptance:
  real selected-DPU payload/basebackup traffic must complete through the
  mirror-only path before `sendQueue` fields can be removed globally.
  Validation stop on July 2, 2026: attempting the first runtime basebackup
  acceptance run exposed a missing selected-DPU frontend path for basebackup,
  before payload DMA/RDMA egress could be validated. The host and DPU builds
  passed, the farnet1 DPU service was rebuilt natively under
  `/tmp/citus-dbcomm-stage8b16`, and a selected-DPU SQL smoke returned
  `count=20, min=290, max=309`. However, `pg_basebackup` with
  `TARGET 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608'`
  failed during `open control` with
  `could not open Homer control region /citus_remote_execution_control_v27`.
  Root cause from code reading: `/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:254`
  still calls `HomerClientOpenControl()`, and
  `/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:286` implements that API by
  opening the old local service control SHM only. It does not use
  `homer_frontend_dma.c`, does not send TCP setup to the DPU, and does not
  export the producer byte ring as a DPU-visible descriptor. Stage 9 therefore
  cannot be accepted for basebackup until basebackup gets an explicit
  selected-DPU frontend/setup path or a shared DPU setup helper usable by
  `homer_client.c`.
  Design correction on July 3, 2026: follow-up discussion identified the
  selected-DPU padding-record workaround as the wrong long-term byte-ring
  protocol. Stage 9 should instead restore/properly formalize the no-padding
  wrap-gap proof used by the existing byte-ring protocol, with a producer-written
  regular next-record header at the pre-wrap cursor when a full header fits in
  the trailer. The existing selected-DPU padding-marker implementation should be
  treated as provisional debug scaffolding, not an accepted Stage 9 design.
  Implementation progress on July 3, 2026: Stage 9.13 removes the provisional
  selected-DPU padding ABI from
  `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_tuple_abi.h`,
  removes the selected-DPU padding writer from
  `/data/dbcomm/citus-dbcomm/src/bin/homer_client.c`, and implements the
  regular-header wrap proof in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
  `HomerClientReserveBaseBackupRecordForObject()` reserves the fixed selected-DPU
  source envelope, `HomerClientSubmitBaseBackupRecord()` writes a regular
  pre-wrap header when a full header fits in the trailer and flushes the fixed
  envelope before host-tail publication,
  `HomerServicePumpOutgoingDpuMirrorByteRingPayload()` treats capped DMA pulls as
  incomplete local views until appended, and
  `HomerServiceAppendOutgoingDpuMirrorBaseBackupFragmentWrite()` posts the
  semantic LAST fragment as soon as the semantic basebackup object bytes are
  mirrored. Any remaining fixed-envelope slack is tracked as source-skip state
  and released only as DMA-mirrored prefixes retire. The outgoing RDMA write path
  regenerates the peer-visible transport header with the semantic basebackup
  object length, so the fixed source envelope does not leak into the peer
  byte-ring protocol.
  Validation also exposed two scheduler/protocol bugs that are now part of the
  Stage 9 plan:
  - the final selected-DPU build must rebuild/install the server-side PostgreSQL
    `postgres` binary, not just `pg_basebackup`, because basebackup records are
    produced by the server-side `bbsink` path;
  - grouped-control reads cannot be indefinitely suppressed as blind empty work
    once host rings are imported. Host byte-ring tail publication has no
    separate doorbell, so the grouped-control collector remains bounded by
    grants but is marked as known expected work while imported rings exist.
  Runtime validation on July 3, 2026 completed first with diagnostic payload
  stats enabled and then with the no-stats production build. The no-stats run
  completed
  `pg_basebackup -X none -c fast -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2'`
  through the selected-DPU mirror path in `real 51.34`; the receiver posted
  final head WIMM with `final_tail=23732382600` and the service log scan showed
  no protocol-reset, stale-header, DOCA task failure, timeout, fatal, panic, or
  assertion lines. The DPU-side log scan matched only expected reclaim and setup
  import-closing lifecycle lines.
  Design correction after that validation: the fixed selected-DPU source
  envelope validated the DPU mirror parser and RDMA egress mechanics but is not
  the accepted producer protocol. The correct next step is to change the
  PostgreSQL basebackup producer loop so selected-DPU Homer reserves exact
  source-record sizes before bytes are written. The current generic `bbsink`
  contract exposes `bbs_buffer` before `archive_contents(len)` reports the
  actual payload length, so fixing this requires moving selected-DPU reservation
  to producer-loop sites such as
  `/data/dbcomm/postgres-citus/src/backend/backup/basebackup.c:566`, not merely
  changing `/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2989`
  `HomerClientSubmitBaseBackupRecord()`.
  Implementation progress on July 3, 2026: Stage 9.14 removes the fixed source
  envelope from the selected-DPU basebackup producer path. The current
  correctness-first implementation adapts the generic `bbsink` contract with a
  scratch buffer: it learns the actual basebackup object length in
  `/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c`,
  reserves an exact Homer byte-ring record with
  `/data/dbcomm/citus-dbcomm/src/bin/homer_client.c`
  `HomerClientReserveBaseBackupRecordForObjectPayload()`, copies the object into
  that exact record, and publishes it. The service-side selected-DPU egress path
  no longer carries fixed-envelope slack; source-skip is reserved for physical
  wrap gaps. This is not the final no-copy producer shape, but it removes the
  protocol ambiguity caused by reserving bytes that were never semantic payload.
  Stage 9.14 also tightens DPU import teardown ownership: CLOSING imports reject
  new DMA submits/requeues but allow already-borrowed DPU-local mirror ranges to
  be released by send-CQ retirement or abort cleanup before descriptor
  destruction.
  Validation on July 3, 2026: host Citus build/install, host Postgres
  build/install, farnet0 install sync, DPU service rebuild, and
  `pg_basebackup -X none -c fast -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2'`
  completed in `real 27.60`. PostgreSQL logged no errors and the earlier
  fixed-envelope/wrap parser issue did not recur. This is still not full Stage 9
  acceptance: the DPU and farnet0 service logs reported RDMA send/recv CQ
  close/reset messages after receiver quiesce. The next cleanup is peer close
  ordering, so successful selected-DPU basebackup does not depend on reset
  cleanup to retire outstanding peer transport owners.
  Follow-up validation on the reverted timeout-fix state completed
  `pg_basebackup -X none -c fast -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608'`
  in `real 27.09`, so the visible selected-DPU close timeout did not recur.
  That run was still not clean: DPU logs showed setup import teardown beginning
  with two in-flight grouped-control reads, followed by RDMA send/recv CQ reset
  messages and a `mirrored byte-range release import/ring is inactive` cleanup
  failure after receiver quiesce. Stage 9 therefore remains blocked on the
  Stage 10 close-drain protocol below; a user-visible successful basebackup is
  not enough acceptance evidence while service logs depend on peer reset to
  clean up owners.
- A DPU-mode payload/basebackup stream whose DPU setup is missing, stale, or
  generation-mismatched fails explicitly before payload publication; it must not
  switch the stream back to local SHM queues.
- Stage 9 is not accepted if foreground commands use the selected-DPU boundary
  while background payload/basebackup traffic silently remains on local
  host-process SHM queues.
- Mixed command plus basebackup validation runs with both workloads on the DPU
  boundary so foreground/background interference measurements are meaningful for
  the migrated design.
- Stage 9 acceptance must include path evidence for both foreground command
  traffic and background byte-stream traffic. It is not enough for one workload
  class to be DPU-backed while the other remains on host-process SHM.
- Stage 9 promotion evidence must be collected under the mixed foreground
  command plus background basebackup shape, because this is where a silent
  host-process SHM stream fallback would invalidate the migration measurement.

### Stage 10 — Teardown, failure, and generation reset

Deliverable: safe lifetime protocol.

Selected-DPU setup close must be promoted from "host sent close and DPU import
eventually disappears" to an explicit final close-drained message. The host
frontend sends setup `CLOSE`, keeps all exported mmap/DOCA resources alive, and
waits for DPU `CLOSE_ACK`. The DPU may send `CLOSE_ACK` only after it has made
the full lifetime proof below; until that ack, host memory remains valid for any
already-posted DMA or RDMA owner that can still reference it.

The current code has a partial skeleton for this in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_setup_tcp.c`
`HomerServiceDpuSetupTcpProgressCloseDrain()` and
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c`
`HomerDpuDmaHostMmapImportTeardownDrained()`, but the current proof is too
narrow: it waits for DPU DMA import tasks and does not prove that RDMA send
owners, receiver-head/final-close owners, or borrowed mirrored byte ranges have
all retired. The July 3, 2026 validation showed that narrow proof can still let
RDMA reset cleanup run after the selected-DPU import/ring is already inactive.

The target close proof before `CLOSE_ACK` is:

- The import generation is marked closing, and every scheduler path that can
  submit new work for that import observes the closing state. In particular,
  grouped-control reads, command pulls, byte-ring pulls, DPU-to-host credit
  publications, and selected-DPU response publications must reject new work for
  closing imports.
- All in-flight DOCA DMA task slots for the import have completed, failed
  fatally, or been retired without publishing stale data. Already-submitted
  grouped-control reads may finish after close begins; that is acceptable, but
  they must be retired before the close ack.
- No ready refs, discovered-ready flags, staged command dispatch slots,
  pending frontend response publications, backend-command publications, or
  completion-event queues remain for the closing generation.
- No borrowed DPU mirrored byte range for the import remains owned by an RDMA
  send owner. Send-CQ retirement must release or abort every owner before the
  import/ring descriptors are destroyed.
- Peer close for payload/basebackup streams has reached the semantic close
  frontier: receiver final-head WIMM/ACK ownership is retired, close response
  ownership is retired, and no payload close control op still references the
  stream.
- The peer RDMA connection is either still healthy with all selected-DPU owners
  retired, or its reset path has completed owner aborts without requiring access
  to an already-inactive import/ring. Reset is allowed as a failure/recovery
  path, not as the normal way to make a successful close safe.
- Only after all of the above can the DPU finalize the imported mmap descriptor,
  reset selected-DPU session/ring state for that generation, and send
  `CLOSE_ACK`. The host destroys DOCA mmap/dev resources only after reading
  that ack.

Scheduler shape: treat close-drain as scheduler-owned progress, not as a
blocking spin inside the TCP setup thread. Setup close moves the import/session
to `CLOSING` and registers a close-drain wait/fact. Normal PE drains, RDMA CQ
drains, payload close actions, and owner-retirement actions continue under
their existing grants. A final `DPU_CLOSE_ACK_PUBLISH`/setup-close action is
eligible only when the close-drain proof facts are all true; executing it
finalizes the import and writes the TCP `CLOSE_ACK`. The action should be
cheap and deterministic: it checks precomputed counters/bitsets and direct
owner counts, not an unbounded scan of all service state. Diagnostic scans may
run only on failed proof or timeout.

Tasks:

- Add draining/closed states to ring descriptors and control lines. DMA task
  errors request fatal service shutdown rather than per-ring recovery.
- Host sends setup `CLOSE` and keeps exported memory registered until final
  `CLOSE_ACK`; it must not unregister memory merely because PostgreSQL has
  finished producing basebackup bytes or because the receiver posted quiesce.
- DPU stops submitting new work for the closing generation immediately when
  setup close is accepted. This covers grouped-control reads and all semantic
  work built from grouped-control snapshots.
- DPU drains or fatally fails all in-flight DOCA DMA tasks, RDMA send owners,
  receiver-head/final-close owners, and selected-DPU scheduler queues before
  acknowledging teardown.
- Implement close-drain proof counters/bitsets per selected-DPU import/session:
  DMA task count, grouped-control in-flight count, ready-ref count, borrowed
  mirror-range owner count, RDMA send-owner count, receiver-head/final-close
  owner count, pending response/publication counts, and close-control op state.
- Add a scheduler action for final close ack publication. It is grantable only
  after the proof counters/bitsets show that the closing generation has no
  remaining owners; it finalizes the import and sends `CLOSE_ACK`.
- Host does not unmap registered memory until DPU `CLOSE_ACK` or whole-service
  generation reset/fatal failure.
- Implement host backend reconnect as a fresh host-initiated TCP setup with a
  new bridge generation against the already-running DPU service.
- Treat DPU service restart as a DPU-mode generation failure for existing host
  sessions; the recovery path is reconnect/re-setup, not SHM continuation.
- Convert setup/teardown/reconnect behavior from experimental smoke semantics
  into the lifecycle contract for selected DPU mode: selected DPU sessions either
  reconnect through TCP setup with a new generation or fail boundedly.
- Add lifecycle diagnostics that record selected-DPU close, generation reset,
  reconnect setup, and fatal DMA-error shutdown paths separately from DPU-off SHM
  teardown.
- Treat Stage 10 promotion work as the lifecycle replacement gate: selected-DPU
  teardown, reconnect, and service-restart handling must use generation reset and
  fresh TCP setup, not SHM continuation.
- Add Stage 10 promotion evidence to lifecycle diagnostics: selected-DPU close,
  reconnect, DPU service restart, stale-generation rejection, and fatal DMA-error
  shutdown must be distinguishable from DPU-off SHM teardown/recovery.

Acceptance:

- Forced backend exit does not allow stale DPU writes into reused memory.
- DPU DMA error callbacks record diagnostics and request fatal service shutdown.
- Service restart increments generation and rejects old task owners.
- Selected-DPU setup close does not send `CLOSE_ACK` while any DMA task,
  grouped-control read, ready ref, borrowed mirrored range, RDMA send owner,
  receiver-head/final-close owner, pending response publication, staged command,
  or payload close op still references the closing import/session generation.
- Selected-DPU basebackup close completes with no DPU-side
  `mirrored byte-range release import/ring is inactive` message and no normal
  success-path peer reset after receiver quiesce.
- A diagnostic build can intentionally delay grouped-control completion at close
  and show that `CLOSE_ACK` waits for the already-submitted grouped-control
  tasks to retire while rejecting new grouped-control submissions.
- Host backend disconnect/teardown followed by reconnect performs a new
  host-initiated setup against an already-running DPU service, with a new bridge
  generation and no reuse of stale imported mmap descriptors.
- DPU service restart while host backends are alive causes bounded DPU-mode
  failures and generation rejection; surviving host sessions must not continue on
  SHM as an implicit recovery path.
- Stage 10 acceptance must exercise reconnect/re-setup on an already-running DPU
  service and a DPU-service restart failure case. Both outcomes must be visible
  as DPU lifecycle behavior, not as host-process SHM recovery.
- Stage 10 is not accepted if teardown, backend reconnect, or DPU service
  restart can recover a selected-DPU session by switching that session to the
  host-process SHM transport.
- Stage 10 promotion evidence must include one successful reconnect/re-setup
  case and one bounded failure case after DPU service restart. Neither case may
  use SHM continuation for the selected session.

### Stage 11 — Measurement and tuning

Deliverable: tuned class budgets, queue depths, and report policy.

Tasks:

- Sweep async windows by class: small command records, mixed command records,
  4 KiB records, and large byte-stream chunks.
- Test `OPTIMIZE_REPORTS` plus flushed sentinel only within one DMA context.
- Compare one shared PE with per-class contexts against split PEs if progress
  interference appears.
- Tune grouped-control poll cadence: command class frequent/small; byte-stream
  class less frequent/larger; idle wakeup optional.
- Add measurement preflight checks or counters that prove command, completion,
  payload, and basebackup records used DPU DMA with TCP setup during a DPU-mode run.
- Measure cold TCP/mmap setup latency and reconnect latency separately from
  steady-state hot-path throughput.
- After Stage 8B through Stage 10 functional gates pass, replace the hidden
  experimental selector with the normal DPU transport/mode selector. This
  promotion is a Stage 11 deliverable because the final decision depends on both
  correctness gates and workload measurements.
- Treat Stage 11 promotion work as selector promotion only: by this point the
  selected-DPU command, completion, payload, and lifecycle paths should already
  be real DPU paths from Stages 6 through 10. Stage 11 should not contain a bulk
  fallback-removal cleanup.
- Keep DPU-off SHM/RDMA runs as comparison baselines, but remove any code path
  where normal DPU mode can silently demote a selected session back to
  host-process SHM after setup or runtime failure.
- Add Stage 11 promotion evidence as a measurement preflight requirement:
  workload runs used to justify the selector flip must first assert that the
  Stage 6 through Stage 10 path counters are present and nonzero for the selected
  workload classes.

Acceptance:

- Command latency does not regress unacceptably under background basebackup.
- Basebackup throughput remains stable under mixed foreground command load.
- Scheduler feedback shows bounded empty-poll behavior, not hot spinning.
- Performance measurements used for DPU-mode promotion must verify that command,
  completion, payload, and basebackup traffic all traverse the DPU DMA boundary
  after TCP setup. Runs that fall back to the old host-process SHM path are
  invalid for DPU acceptance.
- Include cold-path setup latency and reconnect latency as separate measurements
  from steady-state hot-path command/basebackup throughput.
- Only after the Stage 8B through Stage 11 functional, lifecycle, and measurement
  gates pass should the hidden/experimental DPU selector be promoted to the
  normal DPU runtime mode. At that point SHM remains only a DPU-off comparison
  path, not a fallback inside DPU mode.
- Stage 11 is not accepted if measurements cannot prove that command,
  completion, payload, and basebackup records crossed the DPU DMA boundary after
  TCP setup during the selected-DPU run.
- Stage 11 is not accepted if it discovers deferred fallback-removal work that
  should have belonged to Stages 6 through 10; in that case, reopen the owning
  stage gate rather than promoting the selector.

### Stage 12 — Old non-DPU fallback removal and archival

Deliverable: selected-DPU Homer is the only active Homer runtime path; old
host-process SHM transport code is either removed from the build or archived in
clearly unreachable legacy files.

This is a cleanup/promotion stage after both target workloads are covered by the
selected-DPU path. "Old non-DPU path" does **not** mean every host shared-memory
object. Selected-DPU still uses host-owned exported memory for command
mailboxes, completion mailboxes, result byte rings, payload byte rings, and TCP
setup descriptors. The code to remove is the old host-process transport and its
fallback/control mechanics: frontend SHM control-channel mapping, selected-DPU
fallback branches into `homer_frontend_shm.c`, local-only transaction shortcuts
that bypass peer/RDMA transport, legacy selected-DPU `sendQueue` egress paths,
and DPU-off service-control command rings that are no longer used by the normal
Homer runtime.

Tasks:

- First classify all candidate symbols and files into three buckets:
  1. **Keep**: host-exported DMA memory objects, lifecycle mailbox/result-queue
     creation, backend spawn shim, DPU TCP setup, DPU DMA engine, selected-DPU
     scheduler actions, RDMA peer transport, and validation harnesses still used
     to test the active DPU path.
  2. **Archive**: old host-process SHM transport implementations that are useful
     as historical/debug reference but must not be compiled into or referenced by
     the normal runtime. Candidate locations include the old frontend SHM channel
     helpers in
     `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c`
     and old service-control request/response helpers once no active call sites
     remain.
  3. **Delete**: selected-DPU fallback branches, local-only fast-path shortcuts,
     obsolete scalar-result shortcuts, dead `sendQueue` code for selected-DPU
     streams, stale feature flags, and comments/docs that still present DPU-off
     SHM as the normal runtime.
- Preserve old implementations by mechanically moving them into explicit legacy
  source files where that helps debugging. Prefer names that make their status
  impossible to miss, for example `homer_frontend_shm_legacy.c` or a
  `homer_legacy_host_process_*.c` grouping. These legacy files must not be
  linked into the normal service/frontend build after the stage completes.
- Use deterministic command-line transformations for code movement/deletion, not
  hand-edited `apply_patch` ranges over large C files. Whole-file moves should
  use `git mv`. Function/struct/variable moves should use a checked-in or
  one-shot deterministic extraction command that derives exact symbol ranges from
  static source structure, for example `ctags --fields=+ne`, a libclang/AST-based
  script, or another reproducible symbol-range tool, then rewrites files from
  those ranges. Review the resulting diff, but do not manually retype or
  reformat old implementation bodies.
- Keep any mechanical archival commit separate from semantic cleanup commits.
  The archival commit should preserve old code bytes as much as practical so
  later diffs can distinguish "moved legacy implementation" from "changed active
  DPU implementation."
- Remove active references before deleting or excluding legacy code from the
  build. `rg` checks must prove that selected-DPU code no longer calls or
  branches to old frontend SHM control helpers, old host-process command rings,
  local-only pgbench shortcuts, or selected-DPU `sendQueue` payload egress.
- Replace permissive runtime switches with explicit selected-DPU behavior. The
  old experimental selector may remain only as a compatibility spelling during a
  short transition, but normal Homer command/basebackup execution must not choose
  DPU-off SHM when DPU setup fails.
- Update runbooks and KB notes that still describe DPU-off SHM as a supported
  normal path. Keep a short "legacy archived path" note with the final archive
  filenames and the last commit where the old path was active.
- Keep validation harnesses that test DOCA DMA ordering, TCP setup import, and
  DPU mirror/RDMA behavior. These are not old runtime fallbacks even if they use
  synthetic local memory.

Acceptance:

- `rg` over the source tree shows no active selected-DPU call path to
  `homer_frontend_shm.c`, old host-process service-control command rings,
  local-only transaction fast-path helpers, or selected-DPU `sendQueue` egress.
- Legacy/archive files, if retained, are not compiled or linked into the normal
  Postgres, Citus/Homer frontend, or `citus_tuple_sink_service` build.
- The normal Homer frontend path requires selected-DPU TCP setup plus exported
  host memory. If setup fails, the operation fails boundedly and visibly instead
  of falling back to host-process SHM.
- Remote pgbench transaction validation and remote basebackup validation still
  pass after the archival/removal stage. Mixed foreground pgbench plus background
  basebackup validation must also pass before treating this stage as complete.
- Path counters/log evidence still prove command, completion, payload/result, and
  basebackup traffic crossed the host-DPU DMA boundary and, where applicable, the
  RDMA peer boundary.
- The final cleanup commit contains no broad formatter churn. Mechanical moves
  preserve old implementation bodies, and semantic edits are small, reviewable,
  and tied to explicit removal of now-unreachable fallback paths.

## Invariants to assert early

Add assertions and diagnostics for these from the first non-stub stages:

```text
Ready-set building never calls DOCA and never DMA-reads host memory.
Every DPU DMA action is bounded by an explicit grant.
Callbacks never call doca_pe_progress().
Every DOCA task comes from a bounded preallocated task slot with one embedded
owner record.
Every task owner carries task-slot generation and the relevant ring/bridge
generation.
Expected lifecycle-stale callbacks cannot publish DPU-local readiness or
host-visible state.
Bug-like owner/generation/frontier validation failures are fatal until a specific
recoverable policy is designed and validated.
DPU never reads payload beyond an accepted host-published frontier.
Host-visible completion frontier is the final same-context publication-word DMA
task after completion-body DMA tasks.
Host-visible consumed head is published only after contiguous DMA completion and
semantic release.
Grouped-control lines are accepted only when the publication word reaches the
expected epoch or frontier, generation matches, and frontier is monotonic.
Periodic grouped-control polling is due-only work unless demanded by a waiter.
Task-pool exhaustion and host-credit exhaustion are blocked facts, not reasons to
spin inside an executor.
No host backend submits hot-path DOCA DMA tasks.
No private DPU DMA progress loop runs outside TupleSinkServicePumpOnce().
```

## Summary

The DPU migration should be an incremental replacement of the local host-process
boundary, not a scheduler rewrite. Build a real grouped-control ABI, add a DPU
DMA engine with maintained local facts, and schedule that engine through today's
`machine-baseline` collector/action path. Preserve existing command, completion,
payload, and byte-ring semantics until the DPU path is correct and measured.

Once the DPU userspace service is stable, the future async-continuation scheduler
can cleanly replace the remaining action-priority and ready-mask machinery. It is
not required for the migration and should not be on the critical path.
