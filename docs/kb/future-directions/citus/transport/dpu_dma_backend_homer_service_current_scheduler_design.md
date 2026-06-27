# DPU DMA Backend Homer Service Design For The Current Scheduler

## Scope

This document is a concrete implementation design for migrating Homer from the
current host-process service to a DPU-resident service while keeping the scheduler
model that exists today. It is a companion to
[`dpu_dma_backend_homer_service_plan.md`](dpu_dma_backend_homer_service_plan.md).

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

### Sealed cache-line snapshots

Each hot line should fit in one cache line and use a sealed sequence so a reader
can reject torn snapshots.

```c
#define HOMER_DPU_BRIDGE_CACHELINE_BYTES 64U

typedef struct __attribute__((aligned(64))) HomerDpuBridgeHostPublishLine
{
    uint64_t sequenceBegin;
    uint64_t publishedTail;
    uint64_t publishedObjectFrontier;
    uint64_t generation;
    uint64_t flags;
    uint64_t terminalCode;
    uint64_t sequenceEnd;
    uint64_t reserved0;
} HomerDpuBridgeHostPublishLine;

typedef struct __attribute__((aligned(64))) HomerDpuBridgeDpuCreditLine
{
    uint64_t sequenceBegin;
    uint64_t consumedHead;
    uint64_t completedTail;
    uint64_t generation;
    uint64_t flags;
    uint64_t errorCode;
    uint64_t sequenceEnd;
    uint64_t reserved0;
} HomerDpuBridgeDpuCreditLine;
```

Add static asserts for size and alignment.

Producer protocol:

```text
old = current even sequence
write sequenceBegin = old + 1
write all frontier, flag, generation, terminal/error fields
release fence or store-release appropriate to the producer side
write sequenceEnd = old + 2
release-store sequenceBegin = old + 2
```

Consumer protocol:

```text
copy or DMA-read one or more lines into local memory
accept line only if sequenceBegin == sequenceEnd and sequenceBegin is even
accept line only if generation matches the descriptor generation
accept line only if the frontier is monotonic for that ring
otherwise ignore this line and retry on a later scheduled poll
```

This is deliberately stricter than today's host-process SHM path. The DPU sees
host memory through DMA snapshots, so the bridge needs an explicit snapshot
contract.

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
```

The engine owns DOCA objects and physical movement state. It exposes facts and
bounded actions to the existing scheduler.

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

### Task owner records

Every submitted DOCA task needs an owner record:

```c
typedef struct HomerDpuDmaTaskOwner
{
    uint32_t workloadClass;
    uint32_t ringIndex;
    uint32_t taskKind;
    uint32_t direction;
    uint64_t ringGeneration;
    uint64_t firstTail;
    uint64_t lastTail;
    uint64_t taskGeneration;
    uint8_t sentinel;
} HomerDpuDmaTaskOwner;
```

Callbacks validate generation before touching ring state. A stale callback should
free/recycle its task owner and increment diagnostics, but must not publish any
host-visible frontier.

### Engine API

Use action-specific APIs rather than one generic progress function:

```c
bool HomerDpuDmaGetSchedulerFacts(HomerDpuDmaEngine *engine,
                                  HomerDpuDmaSchedulerFacts *facts);

bool HomerDpuDmaSubmitControlReads(HomerDpuDmaEngine *engine,
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

bool HomerDpuDmaSubmitCompletionPushes(HomerDpuDmaEngine *engine,
                                       const HomerProgressGrant *grant,
                                       HomerProgressResult *result);

bool HomerDpuDmaPublishConsumedHeads(HomerDpuDmaEngine *engine,
                                     const HomerProgressGrant *grant,
                                     HomerProgressResult *result);
```

`HomerDpuDmaDrainPe()` is the normal place where `doca_pe_progress()` is called.
Submit actions should normally submit work and return. If a prototype combines
submit and PE progress for bring-up, the inline progress must consume an explicit
poll budget and report it in `HomerProgressResult`.

Callbacks must not call `doca_pe_progress()` recursively.

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
5. DMA-writes the response body into the host slot.
6. After response-data DMA completion, publishes the host-visible response state
   or response frontier.
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
2. Observe the data-DMA completed contiguous frontier.
3. DMA-write the ready epoch/frontier or DPU credit line.
4. Host frontend consumes only slots at or below the published epoch/frontier.

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

## Implementation sequence

### Stage 0 — Freeze the migration contract

Deliverable: this document plus a short note in the original DPU plan linking to
it.

Tasks:

- Record that the async-continuation scheduler is deferred.
- Record that the DPU DMA integration target is today's `machine-baseline`
  collectors/actions.
- Keep SHM as the default runtime path.

Acceptance:

- No behavior change.
- The plan explicitly distinguishes current-scheduler migration from future
  scheduler cleanup.

### Stage 1 — Bridge ABI header

Deliverable: `homer_dpu_bridge_abi.h`.

Tasks:

- Add protocol version, feature flags, workload class, direction, entry state,
  ring descriptor, control-block header, host-publish line, DPU-credit line, and
  constants.
- Add static asserts for cache-line size, alignment, and descriptor sizes.
- Add inline validation helpers for sealed sequence lines.
- Add comments that define owner, producer, and consumer for every field.

Acceptance:

- Builds with normal Citus/Homer targets.
- Header can be included from host frontend code and service/DPU code without
  PostgreSQL-only or DOCA-only type leakage.
- Unit or compile-only test rejects malformed line sizes if a field is added.

### Stage 2 — Host frontend DMA skeleton

Deliverable: `homer_frontend_dma.c/.h` behind a build/runtime switch.

Tasks:

- Add channel-selection plumbing while keeping SHM default.
- Allocate aligned bridge memory and placeholder command/completion/payload
  regions.
- Implement sealed-line publish/read helpers.
- Add placeholder COMCH setup hooks or explicit `not implemented` errors behind
  the DPU channel switch.
- Do not submit DMA tasks from backend processes.

Acceptance:

- SHM path remains unchanged and passes existing tests.
- DPU channel can be selected only in an explicit experimental mode.
- Experimental mode initializes and tears down host bridge memory without command
  execution.

### Stage 3 — DPU DMA engine skeleton

Deliverable: `homer_service_dpu_dma.c/.h` with DOCA object lifecycle and zero
work facts.

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

### Stage 4 — Scheduler skeleton in `machine-baseline`

Deliverable: DPU collector/action identities and no-op bounded executors.

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

- With DPU mode off, generated plans are byte-for-byte or diagnostically
  equivalent to today's plans.
- With DPU mode on and no work, DPU actions are bounded and back off like other
  blind collectors.
- Ready-set building performs no DOCA calls.

### Stage 5 — Grouped-control poll action

Deliverable: scheduled DMA reads of host-publish-line slices.

Tasks:

- Import host mmap descriptor on the DPU side.
- Allocate reusable local buffers for grouped-control slices.
- Submit control-read DMA tasks only under `DPU_DMA_SUBMIT_CONTROL_READS` grants.
- Drain completions only under `DPU_DMA_DRAIN_PE` grants.
- Validate sealed lines and generations in callbacks.
- Populate DPU-local ready bits/counts for rings with new observed frontiers.

Acceptance:

- Synthetic host publisher can advance frontiers and DPU observes them without
  reading one tail at a time.
- Torn/odd/mismatched-generation lines are ignored and counted diagnostically.
- No payload DMA reads are submitted in this stage.

### Stage 6 — PE drain and task-owner retirement

Deliverable: bounded PE drain with real callback/frontier accounting.

Tasks:

- Implement task-owner allocation, generation validation, callback retirement,
  and task reuse.
- Implement bounded `doca_pe_progress()` loops controlled by grant budget.
- Report `cqesDrained`, `emptyPolls`, `stillReady`, and `budgetExhausted`.
- Prohibit nested PE progress from callbacks.

Acceptance:

- Drain action stops on first zero-progress return or budget exhaustion.
- In-flight tasks remain represented in DPU-local facts for later grants.
- Stale generation callbacks do not publish any host-visible state.

### Stage 7 — Command/control request pull

Deliverable: start-command and poll-completion through DPU-pulled host request
slots, still using existing semantic request/response structs.

Tasks:

- Make host frontend publish request-ready state through bridge lines.
- DMA-read ready request slots into DPU-local staging.
- Refactor service local-control handling behind an adapter that accepts staged
  request/response objects.
- DMA-write response bodies to host memory.
- Publish response-ready state only after response DMA completion.
- Preserve request sequence and owner validation.

Acceptance:

- `StartRemoteExecutionCommandThroughLocalService()` and
  `PollRemoteExecutionCommandCompletionThroughLocalService()` work through the DPU
  channel in a single-session test.
- The same APIs still work through SHM when DPU mode is off.
- Early response publication negative test fails as expected.

### Stage 8 — Completion/result push

Deliverable: DPU DMA writes to host completion/result mailboxes.

Tasks:

- Map current frontend client completion mailbox semantics onto DPU-owned publish
  lines or ready epochs.
- Implement completion slot DMA write task owners.
- Publish ready epoch/frontier after data-DMA completion.
- Update host frontend polling to validate DPU-owned credit/completion lines.

Acceptance:

- Multi-state frontend completion sequence is observed in order.
- Host never consumes a completion whose slot body is stale or partially written.
- Split-context no-wait publication is rejected or gated.

### Stage 9 — Payload and basebackup byte-stream pull

Deliverable: DPU pulls backend-produced byte-ring records and feeds existing
service payload transport logic.

Tasks:

- Register/import byte-ring host memory.
- Discover byte-ring `publishedTail` via grouped-control polling.
- Submit bounded payload DMA reads under payload grants using existing payload
  egress budget shape.
- Validate transport record headers/generations/checksums where available.
- Advance completed contiguous byte frontier only from callbacks.
- Publish consumed head to host only after safe release.

Acceptance:

- Single stream transfers records without stale/torn reads.
- Ring wrap is handled with two DMA ranges or equivalent staging.
- Basebackup-like large stream uses bounded windows and does not starve command
  collectors.

### Stage 10 — Teardown, failure, and generation reset

Deliverable: safe lifetime protocol.

Tasks:

- Add draining and failed states to ring descriptors and control lines.
- Host sets terminal/draining flags before unregistering memory.
- DPU stops submitting new tasks after terminal generation transition.
- DPU drains or invalidates in-flight tasks before acknowledging teardown.
- Host does not unmap registered memory until DPU ack or whole-service generation
  reset.

Acceptance:

- Forced backend exit does not allow stale DPU writes into reused memory.
- DPU DMA error callbacks publish terminal/error state when possible.
- Service restart increments generation and rejects old task owners.

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

Acceptance:

- Command latency does not regress unacceptably under background basebackup.
- Basebackup throughput remains stable under mixed foreground command load.
- Scheduler feedback shows bounded empty-poll behavior, not hot spinning.

## Invariants to assert early

Add assertions and diagnostics for these from the first non-stub stages:

```text
Ready-set building never calls DOCA and never DMA-reads host memory.
Every DPU DMA action is bounded by an explicit grant.
Callbacks never call doca_pe_progress().
Every task owner carries ring generation and task generation.
Stale callbacks cannot publish host-visible state.
DPU never reads payload beyond an accepted host-published frontier.
Host-visible completion frontier is published only after completion-data DMA.
Host-visible consumed head is published only after contiguous DMA completion and
semantic release.
Grouped-control lines are accepted only when sequenceBegin == sequenceEnd,
sequence is even, generation matches, and frontier is monotonic.
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
