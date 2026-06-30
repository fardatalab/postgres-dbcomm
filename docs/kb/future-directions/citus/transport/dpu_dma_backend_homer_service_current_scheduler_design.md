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
8B, payload/basebackup pull in Stage 9, lifecycle/reconnect in Stage 10, and
only the user-facing selector flip in Stage 11.

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
   response owner, and expected backend completion epoch.
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
   sequence, and expected epoch, stages the completion in DPU/service memory,
   and submits or queues a DMA write of the backend completion mailbox
   `consumedEpoch` after the completion body has been safely copied. It replaces
   the service's direct completion consumption in
   `TupleSinkServiceConsumeCompletionMailbox()` at
   `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18473`.
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
   descriptor identity, command sequence, expected backend completion epoch, and
   generation tokens. The existing Stage 7 dispatch/execute queues may be reused
   only if they are renamed or documented as frontend-command and
   pending-response queues; do not let `TupleSinkServiceDispatchLocalControlSlot()`
   remain the production SQL execution step for selected-DPU sessions.
   Implementation progress: Stage 8B.10 adds the selected-DPU session table and
   backend-command publish queue. The queue now carries the pulled frontend
   response owner, DPU-owned command sequence, compact
   `CitusRemoteExecLocalCommandRecord`, backend command mailbox descriptor ref,
   and expected backend completion epoch. Stage 8B.11 consumes that queue through
   the real backend-command DMA submission path and then queues the frontend
   START response for response publication. Backend-completion staging is still
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
      the lifecycle result-ring slice, but the acceptance validation is still
      pending. Keep this sub-slice open until a selected-DPU row-producing SQL
      smoke proves that `RemoteExecEnsureSessionResultQueue()` uses the
      lifecycle-created queue and does not reach the old control-region path.
   4. **End-to-end tuple-result smoke slice.** Run selected-DPU `SELECT abalance`
      through the pgbench wrapper and verify the frontend reads the returned
      tuple from the result sink, not from scalar completion fields. This is the
      acceptance gate for Stage 8B.16 and then Stage 8B.15 can be retried.

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

### Stage 9 — Payload and basebackup byte-stream pull

Deliverable: DPU pulls backend-produced byte-ring records and feeds existing
service payload transport logic.

Tasks:

- Register/import byte-ring host memory.
- Require each DPU-mode payload/basebackup stream to be represented in the TCP
  setup descriptors and imported mmap state before the host publishes stream
  payload readiness.
- Discover byte-ring `publishedTail` via grouped-control polling.
- Submit bounded payload DMA reads under payload grants using existing payload
  egress budget shape.
- Validate transport record headers/generations/checksums where available.
- Advance completed contiguous byte frontier only from callbacks.
- Publish consumed head to host only after safe release.
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

Acceptance:

- Single stream transfers records without stale/torn reads.
- Ring wrap is handled with two DMA ranges or equivalent staging.
- Basebackup-like large stream uses bounded windows and does not starve command
  collectors.
- Synthetic byte-ring pull test runs before real basebackup: host publishes
  generated records, DPU pulls only bytes at or below the accepted frontier,
  callbacks advance the completed contiguous frontier, and consumed-head
  publication happens only after semantic release.
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

Tasks:

- Add draining/closed states to ring descriptors and control lines. DMA task
  errors request fatal service shutdown rather than per-ring recovery.
- Host sets terminal/draining flags before unregistering memory.
- DPU stops submitting new tasks after terminal generation transition.
- DPU drains or invalidates in-flight tasks before acknowledging teardown.
- Host does not unmap registered memory until DPU ack or whole-service generation
  reset.
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
