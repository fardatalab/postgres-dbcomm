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

However, the plan needs three corrections before implementation:

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
    uint32_t reserved0;

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
   handles with the DPU service over COMCH during setup.
4. Write backend-produced request records and payload bytes into host memory.
5. Publish host-owned frontiers through `HomerDpuBridgeHostPublishLine`.
6. Read DPU-owned credit/completion lines with acquire-side validation.
7. Keep `OpenRemoteExecutionSession()`, `StartRemoteExecutionCommand()`, and
   `PollRemoteExecutionCommandCompletion()` stable for ordinary callers.

Decision: the host-side COMCH client belongs in `homer_frontend_dma.c` for this
phase. It is part of the DMA channel setup path: it registers/exports host mmap
state, sends descriptor bytes to the DPU service, waits for the setup ack, and
then lets the hot path publish frontiers through DMA-visible memory. Because this
is cold-path setup, it may block while waiting for COMCH connection/setup
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
src/backend/distributed/utils/homer/homer_service_dpu_comch.c
src/backend/distributed/utils/homer/homer_service_dpu_comch.h
```

The engine owns DOCA objects and physical movement state. It exposes facts and
bounded actions to the existing scheduler.

Decision: keep new DPU implementation work out of
`tuple_sink_service_process.c` where the current scheduler-local type boundary
allows it. The large file should remain a thin scheduler adapter: it owns current
`HomerGrantVector`, `HomerProgressResult`, collector/action enums, and dispatch
from `HomerServiceExecuteDpuDmaAction()`. Real COMCH server lifecycle and message
handling should live in `homer_service_dpu_comch.c/.h`; real DMA lifecycle,
descriptor import, buffer/task ownership, PE drain, and callbacks should stay in
`homer_service_dpu_dma.c/.h`. Do not extract the scheduler-local types only to
move code out; do that later if the adapter boundary becomes the limiting factor.

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
run COMCH setup, call scheduler planning code, or spin on other completions. If a
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

### COMCH and mmap setup lifecycle

COMCH is cold/control-plane setup for this phase, not a hot-path publication
primitive. It should carry descriptors and lifecycle acks; data movement and
frontier publication stay in DMA-visible memory.

The DPU Homer service is the COMCH server. The host/frontend DMA channel is the
COMCH client. This matches the deployment shape where the DPU service is the
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

The exact C struct can be adjusted while implementing, but the required content
is fixed: protocol/versioning, message kind, bridge generation, feature flags,
ring count, descriptor size, mmap export length, exported mmap blob, bridge
header, descriptor table, and an ack/error result. The DPU COMCH server should
pass the received export blob to `HomerDpuDmaImportHostMmapDescriptor()` and
validate the bridge header/descriptors before accepting rings.

Initial setup sequence:

```text
1. Host frontend allocates long-lived bridge, command, completion, and payload
   memory with cache-line alignment required by homer_dpu_bridge_abi.h.
2. Host creates a doca_mmap, sets the memory range and PCI read/write
   permissions, adds the local device, starts the mmap, and exports it with
   doca_mmap_export_pci().
3. Host opens/attaches the COMCH client connection to the DPU service and sends:
   protocol version, feature flags, exported mmap blob, descriptor table,
   initial generation, ring count, and optional idle-wakeup handles.
4. DPU service receives the COMCH setup message, imports the mmap with
   doca_mmap_create_from_export(), validates protocol/descriptor geometry, and
   creates DPU-local ring/class state in SETUP.
5. DPU creates or reuses class DMA contexts, attaches them to the PE, configures
   task pools/callbacks, starts contexts, and marks descriptors ACTIVE only after
   all buffers/task-owner pools needed for the accepted rings exist.
6. DPU sends a COMCH setup ack containing accepted generation, feature bits,
   negotiated class windows, and any rejected ring descriptors.
7. Host publishes ring frontiers only after setup ack. Until then the DPU must
   ignore hot-path lines for that generation.
```

Teardown sequence:

```text
1. Host publishes DRAINING/CLOSED in the owner line and sends a COMCH close if
   the connection is still alive.
2. DPU stops submitting new DMA tasks for that generation, drains or invalidates
   in-flight tasks through scheduled PE-drain grants, and prevents stale
   callbacks from publishing host-visible state.
3. DPU publishes final consumed/error state if the host memory is still valid and
   sends a COMCH close ack.
4. Host waits for close ack or whole-service generation reset before unmapping,
   stopping the mmap, or reusing the address range for a new generation.
5. If COMCH dies, both sides advance service/ring generation and rely on stale
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

### Stage 0 — Freeze the migration contract

Deliverable: this document plus a short note in the original DPU plan linking to
it.

Tasks:

- Record that the async-continuation scheduler is deferred.
- Record that the DPU DMA integration target is today's `machine-baseline`
  collectors/actions.
- Keep SHM as the default DPU-off runtime path only while no selected DPU path is
  runnable.
- Record that promotion to normal DPU mode is not a separate late cleanup: later
  stages must replace each selected DPU-mode fallback with a real DPU DMA/COMCH
  path or an explicit bounded DPU error.

Acceptance:

- No behavior change.
- The plan explicitly distinguishes current-scheduler migration from future
  scheduler cleanup.
- The implementation sequence below carries the promotion work in the relevant
  stage tasks and acceptance gates, rather than leaving it as a standalone
  paragraph.

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
- Add placeholder COMCH setup hooks or explicit `not implemented` errors behind
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

### Stage 5 — DOCA/COMCH grouped-control lifecycle

Deliverable: real DOCA/COMCH lifecycle objects and grouped-control task-pool
scaffolding, but no real grouped-control DMA submission yet.

Status: completed for the service-side DOCA lifecycle and descriptor-import
boundary in `/data/dbcomm/citus-dbcomm`. The implementation creates bounded
task-slot/owner arrays and local grouped-control staging buffers, can compile an
opt-in DOCA lifecycle smoke with `HOMER_DPU_DMA_WITH_DOCA`, creates one PE plus
per-class DMA contexts, and imports PCI mmap export descriptors through
`HomerDpuDmaImportHostMmapDescriptor()`. The production COMCH peer that carries
those descriptor bytes from the host/frontend into the DPU service is not wired
yet; Stage 5 treats COMCH as the required control-plane transport boundary, while
the validated code consumes descriptor bytes through a scheduler-neutral import
API.

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
- Exchange the host mmap descriptor over COMCH for the real path. A file-based
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

- `HOMER_SERVICE_ENABLE_DOCA_DMA=1` can create and destroy the minimal DOCA/COMCH
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

### Stage 6 — Grouped-control submit, PE drain, and task-owner retirement

Deliverable: scheduled grouped-control DMA reads of host-publish-line slices plus
bounded PE drain with real callback/frontier accounting.

Stage 6 is split into two ordered sub-stages:

1. **Stage 6A — COMCH setup path**: implement minimal host/frontend COMCH client
   and DPU-service COMCH server. The host sends the setup message and waits for
   ack with a timeout. The DPU receives descriptor bytes, calls
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

Tasks:

- Implement the minimal COMCH setup message ABI and close/ack shell. The setup
  ABI and setup-ack struct are landed; the close/close-ack shell remains.
- Integrate the DPU COMCH server so service startup creates the listener and then
  returns to normal service pumping. Done for service ownership and bounded
  scheduler PE progress in Stage 6A.6; runtime validation of the exact production
  service binary on the DPU remains a later deployment gate. Startup must not
  wait indefinitely for a host DB backend to connect.
- Implement the host COMCH client in `homer_frontend_dma.c`. Done in Stage 6A.8
  for compile/link integration: the helper exports bridge memory as a PCI mmap,
  sends setup over COMCH, waits for ack with a timeout, and rejects selected DPU
  mode explicitly in non-DOCA builds. Runtime validation through the exact
  PostgreSQL backend frontend path passed against the standalone import-enabled
  DPU COMCH smoke server.
- Replace the current frontend smoke-and-error guard with a real DPU setup
  operation that either completes COMCH/mmap setup or fails before any SHM mapping
  is attempted in a selected DPU session. Stage 6A.8 wires this operation before
  the Stage 7 command-pull `not implemented` error; the backend/runtime
  validation reached that expected guard after DPU setup completed.
- Start the runtime promotion path here: after the DPU channel is selected, host
  frontend setup must attempt the real COMCH/mmap setup against the independently
  running DPU service and then either mark the DPU bridge setup-ready or return a
  bounded DPU setup error. It must not reopen the SHM frontend path.
- Keep the DPU selector experimental in Stage 6, but make the selected path real
  enough to validate setup: the old bridge-memory smoke may remain as a local
  preflight check, but it must no longer be the terminal behavior for a selected
  DPU frontend build that has DOCA enabled.
- Implement the DPU COMCH server in `homer_service_dpu_comch.c/.h`, keeping
  `tuple_sink_service_process.c` as only a scheduler/lifecycle adapter. The
  setup-payload handler and reusable server lifecycle are landed; the standalone
  transport smoke validates real server lifecycle and the current farnet1
  representor choice, and Stage 6A.6 wires service startup/teardown plus bounded
  `DPU_PE_DRAIN` progress.
- Keep COMCH ack buffers alive until the send completion/error callback. DOCA
  COMCH sends are asynchronous, so callbacks must not pass a stack ack or a
  shared scratch ack to `doca_comch_server_task_send_alloc_init()`. Stage 6A.5
  uses one heap `HomerDpuComchSetupAck` copy per send task and frees it from
  task user data in the send callback.
- Store imported host mmaps in a bounded engine table keyed by setup identity,
  not in a singleton. Done in Stage 6A.7 for bridge generation plus client
  instance ID. Later host/frontend setup should allocate one table entry per
  backend/frontend bridge setup and resolve grouped-control buffers through that
  entry.
- Implement task-owner allocation, generation validation, callback retirement,
  and task reuse for grouped-control DMA tasks after descriptor import.
- Submit control-read DMA tasks only under `DPU_DMA_SUBMIT_CONTROL_READS` grants.
- Implement bounded `doca_pe_progress()` loops controlled by grant budget.
- Drain completions only under `DPU_DMA_DRAIN_PE` grants.
- Validate task owner identity, publication words, generations, and frontiers in
  callbacks.
- Populate DPU-local ready bits/counts for rings with new observed frontiers.
- Report `cqesDrained`, `emptyPolls`, `stillReady`, and `budgetExhausted`.
- Prohibit nested PE progress from callbacks.

Acceptance:

- Host/frontend COMCH setup can send a real mmap export blob, bridge header, and
  ring descriptors to the DPU service and receive a setup ack before any hot-path
  frontier publication is used.
- DPU service startup succeeds and starts normal service pumping even when no host
  backend has connected yet.
- A selected DPU frontend path reaches a real COMCH setup attempt, not the old
  smoke-only guard. Missing service, malformed setup, representor/device mismatch,
  and timeout are reported as DPU setup failures before any SHM mapping or
  local-service SHM call.
- The selected DPU setup path has no SHM fallback branch. In non-DOCA builds it
  may fail with a compile-time capability error; in DOCA-enabled builds it must
  attempt COMCH/mmap setup or return a bounded DPU setup error.
- Before Stage 6A is closed, the production PostgreSQL backend/frontend path must
  be exercised against an independently running DPU COMCH service, not only
  compiled or validated through the standalone host client. Done against the
  import-enabled DPU COMCH smoke server; full DPU-resident
  `citus_tuple_sink_service` runtime validation is still separate.
- DPU COMCH setup imports the host mmap via `HomerDpuDmaImportHostMmapDescriptor()`
  and rejects malformed protocol version, descriptor size, generation, or ring
  geometry with an explicit setup error.
- Multiple host/frontend setup imports can coexist in the DPU DMA engine. A
  duplicate bridge generation plus client instance ID is rejected before later
  DMA task ownership can become ambiguous.
- Synthetic host publisher can advance frontiers and DPU observes them without
  reading one tail at a time.
- Drain action stops on first zero-progress return or budget exhaustion.
- In-flight tasks remain represented in DPU-local facts for later grants.
- Expected lifecycle staleness, such as old ring generation after teardown, is
  ignored and counted diagnostically.
- Stale generation callbacks do not publish any host-visible state.
- Bug-like validation failures, including task-slot generation mismatch,
  malformed owner metadata, unexpected generation mismatch, and non-monotonic
  frontiers, fail the DPU engine fatally instead of being silently ignored.
- Unchanged publication words are counted as empty/no-new-work, not as errors.
- Debug invariant mode proves submit actions do not call `doca_pe_progress()` and
  callbacks do not submit follow-on DMA work.
- A standalone host+DPU grouped-control test proves control-read DMA submission
  and callback retirement are separated: submit action queues reads, PE-drain
  action observes and validates them.

### Stage 7 — Command/control request pull

Deliverable: start-command and poll-completion through DPU-pulled host request
slots, still using existing semantic request/response structs.

Tasks:

- Make host frontend publish request-ready state through bridge lines.
- Require completed DPU COMCH/mmap setup before publishing any command request in
  DPU mode.
- Add DPU-mode frontend errors for missing setup, stale setup generation, absent
  DPU service, and setup timeout; these errors must occur before any SHM frontend
  mapping or SHM local-service call.
- DMA-read ready request slots into DPU-local staging.
- Refactor service local-control handling behind an adapter that accepts staged
  request/response objects.
- Submit response-body DMA writes followed immediately by the response-ready
  publication-word DMA write on the same ordered context.
- Preserve request sequence and owner validation.
- Keep the old SHM host-process APIs buildable only as DPU-off migration
  coexistence; do not add fallback branches from selected DPU command handling to
  SHM command handling.
- Replace the Stage 6 terminal `not implemented` guard for command APIs with the
  staged request-slot pull path. After this stage, a selected DPU command session
  should fail only for DPU setup/protocol/runtime errors, not because command
  execution is still deliberately blocked.

Acceptance:

- `StartRemoteExecutionCommandThroughLocalService()` and
  `PollRemoteExecutionCommandCompletionThroughLocalService()` work through the DPU
  channel in a single-session test.
- The same APIs still work through the old SHM host-process implementation when
  DPU mode is off during migration, but this is mode selection, not fallback from
  a selected DPU session.
- A DPU-mode command session that has not completed COMCH/mmap setup fails with
  an explicit DPU setup error before publishing a request, and does not remap or
  call the SHM frontend path.
- A DPU-mode command session whose DPU service is absent or times out fails with
  an explicit bounded setup/connection error.
- The hidden experimental selector remains required, but command API acceptance
  evidence must be collected through the selected DPU path. SHM command success
  is only a DPU-off regression check.
- Early response publication negative test fails as expected.
- Before real backend command execution, a synthetic request-slot pull test DMA
  reads one fixed request slot into DPU-local staging, validates owner/generation,
  and leaves command semantics unmodified.

### Stage 8 — Completion/result push

Deliverable: DPU DMA writes to host completion/result mailboxes.

Tasks:

- Map current frontend client completion mailbox semantics onto DPU-owned publish
  lines or ready epochs.
- Split DPU-mode completion polling from the old SHM completion mailbox path so a
  selected DPU session reads only DPU-published completion/credit lines.
- Add explicit DPU-mode completion errors for missing imported mmap, stale bridge
  generation, or absent DPU completion publication.
- Implement completion slot DMA write task owners.
- Submit completion-body DMA writes followed immediately by ready
  epoch/frontier publication-word DMA writes on the same ordered context.
- Update host frontend polling to validate DPU-owned credit/completion lines.
- Remove the selected-DPU completion dependency on host-process SHM completion
  mailboxes. The old mailbox code may remain for DPU-off mode, but DPU-mode
  command completion must be driven by DPU-published lines.

Acceptance:

- Multi-state frontend completion sequence is observed in order.
- Host never consumes a completion whose slot body is stale or partially written.
- Split-context no-wait publication is rejected or gated.
- Same-context body+publish test submits completion-body DMA tasks followed
  immediately by one publication-word DMA task, with no PE drain in the submit
  action; the host observes only the final publication word.
- In DPU mode, completion polling consumes only DPU-published completion/credit
  lines. A missing or invalid DPU completion path must return a DPU-path error,
  not poll the old SHM completion mailbox as a fallback.

### Stage 9 — Payload and basebackup byte-stream pull

Deliverable: DPU pulls backend-produced byte-ring records and feeds existing
service payload transport logic.

Tasks:

- Register/import byte-ring host memory.
- Require each DPU-mode payload/basebackup stream to be represented in the COMCH
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
- Mixed command plus basebackup validation runs with both workloads on the DPU
  boundary so foreground/background interference measurements are meaningful for
  the migrated design.

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
- Implement host backend reconnect as a fresh host-initiated COMCH setup with a
  new bridge generation against the already-running DPU service.
- Treat DPU service restart as a DPU-mode generation failure for existing host
  sessions; the recovery path is reconnect/re-setup, not SHM continuation.
- Convert setup/teardown/reconnect behavior from experimental smoke semantics
  into the lifecycle contract for selected DPU mode: selected DPU sessions either
  reconnect through COMCH with a new generation or fail boundedly.

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
  payload, and basebackup records used DPU DMA/COMCH during a DPU-mode run.
- Measure cold COMCH/mmap setup latency and reconnect latency separately from
  steady-state hot-path throughput.
- After Stage 7 through Stage 10 functional gates pass, replace the hidden
  experimental selector with the normal DPU transport/mode selector. This
  promotion is a Stage 11 deliverable because the final decision depends on both
  correctness gates and workload measurements.

Acceptance:

- Command latency does not regress unacceptably under background basebackup.
- Basebackup throughput remains stable under mixed foreground command load.
- Scheduler feedback shows bounded empty-poll behavior, not hot spinning.
- Performance measurements used for DPU-mode promotion must verify that command,
  completion, payload, and basebackup traffic all traverse the DPU DMA/COMCH
  boundary. Runs that fall back to the old host-process SHM path are invalid for
  DPU acceptance.
- Include cold-path setup latency and reconnect latency as separate measurements
  from steady-state hot-path command/basebackup throughput.
- Only after the Stage 7 through Stage 11 functional, lifecycle, and measurement
  gates pass should the hidden/experimental DPU selector be promoted to the
  normal DPU runtime mode. At that point SHM remains only a DPU-off comparison
  path, not a fallback inside DPU mode.

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
