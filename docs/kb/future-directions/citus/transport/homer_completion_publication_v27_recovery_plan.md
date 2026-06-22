# Homer Completion Publication v27 Recovery Plan

## Scope

- **What this doc explains**: the audited recovery plan after the v26
  peer-client completion publication fix: one full-slot
  `RDMA_WRITE_WITH_IMM`, receiver-service CPU publication, removal of the
  frontend client-completion aggregate `publishedEpoch`, canonical recv-CQ
  dispatch, and the Stage 6 readiness work that should precede further Stage 7
  topology/performance changes.
- **What this doc does NOT cover**: a full rewrite of the guarded-action
  scheduler, DPU offload, service multi-threading, or the lower-priority
  basebackup fragment/reservation cleanups except as follow-on slices.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`
- **Doc type**: `future-direction`

This plan supersedes the Stage 7d blocker section of
[`homer_transport_hot_path_optimization_plan.md`](homer_transport_hot_path_optimization_plan.md)
for peer-client completion publication. That larger plan remains the broad
transport optimization roadmap.

## Current Code Pointers

- [`CitusRemoteExecClientCompletionSeal`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:560)
  is the trailing content proof currently appended to a client completion slot.
- [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:867)
  still contains frontend-client `publishedEpoch`, `consumedEpoch`,
  `peerCompletionDoorbellScratch`, slot-local `readyEpochSlots`, completion
  slots, and the result descriptor table.
- [`HomerResultDescriptorSlot`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:604)
  currently has a leading `bodyVersion` but no trailing seal.
- [`CitusRemoteExecPublishResultDescriptor()`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:767)
  and
  [`CitusRemoteExecReadResultDescriptorStable()`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:790)
  implement current descriptor publication/read validation.
- [`HomerClientOpenSqlSession()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:680)
  initializes the remote client completion cursor from
  `completionMailbox->publishedEpoch`; v27 should initialize from
  `consumedEpoch` instead.
- [`HomerClientCopyClientCompletionSlotStable()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1108)
  is the frontend ready-copy-ready and trailing-seal validator.
- [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14200)
  is the backend-node peer-client completion publisher.
- [`TupleSinkServicePostPeerRegisteredClientCompletionBytesWithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5936)
  is the registered-source `RDMA_WRITE_WITH_IMM` posting surface.
- [`TupleSinkServiceHandlePeerClientCompletionDoorbell()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20175)
  is the receiver-side doorbell handler that validates completion content and
  CPU-publishes frontend visibility.
- [`TupleSinkServiceDrainPeerConnectionEvents()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3906)
  still accepts optional semantic handlers and still has a fallback
  `pendingClientCompletionDoorbellTokens` FIFO rooted at
  [`remote_execution_peer_transport_rdma.c:473`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:473).
- [`ibv_query_qp_data_in_order()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2217)
  is currently logged for capability diagnostics.

## Corrected Diagnosis

Do not state that `ibv_query_qp_data_in_order()` returning zero directly proves
that the old two-WQE body-write/ready-write sequence is unordered. That API
reports whether data is written in order within one WQE for CPU readers polling
memory, and the documented scope does not establish ordering between separate
WQEs.

The accurate diagnosis is narrower and stronger:

- the implementation had no documented contract that allowed a CPU frontend
  client to use an RNIC-written ready word as proof that a separately written
  completion body was coherent;
- the captured `readyEpochSlots[slot] == new epoch` plus stale/mixed completion
  body observation empirically proved that the path was unsafe on the current
  hardware/software configuration;
- a future positive `WHOLE_MSG` result would support a single-WQE
  body-plus-footer design under the API's documented constraints, but would not
  automatically validate two separate WQEs for body and ready.

Therefore the v27 target is one full completion-slot
`RDMA_WRITE_WITH_IMM`, followed by receiver-service validation and CPU
publication of frontend-visible ready. Do not keep the v26 two-WQE shape
`body WRITE + scratch WIMM` as the target design.

## Seal Purpose

The trailing completion seal is not a replacement for WRITE WITH IMM, and WRITE
WITH IMM is not a replacement for the seal.

WRITE WITH IMM gives the receiver service a CQE that says a specific WQE
completed and carries a compact token. It does not by itself prove to the
frontend client that the completion slot it later reads:

- belongs to the expected command epoch;
- carries the expected command sequence, kind, state, and flags;
- was not published through a stale token/session binding;
- was not read as a mixed or stale slot image because of a local publication bug;
- has a descriptor reference whose descriptor-table entry is also coherent.

The seal is filled after the hot completion body and repeats the epoch plus the
semantic command identity. The client and receiver service can then validate the
copied body against a post-body proof. After v27, the expected chain is:

```text
sender fills body
sender fills trailing seal
sender posts one full-slot WRITE_WITH_IMM
receiver gets CQE
receiver validates body/seal and descriptor visibility
receiver CPU-publishes readyEpochSlots[slot]
frontend client ready-copy-ready reads slot
frontend client validates body/seal before applying completion
```

The seal is cheap insurance against semantic and publication-owner bugs. It is
also the correct fail-fast signal after the receiver service has CPU-published
ready: once ready is visible to the client, a body/seal mismatch is corruption,
not a normal retry.

## Slice 1: Completion Publication Protocol v27

### 1.1 One Full-Slot WIMM

Change peer completion publication from:

```text
WR 1: RDMA WRITE completion slot body + seal
WR 2: RDMA WRITE WITH IMM inert scratch word
```

to:

```text
WR 1: RDMA WRITE WITH IMM sizeof(CitusRemoteExecClientCompletionSlot)
```

The immediate value remains the peer-client completion doorbell token. The
receiver CQE now corresponds to the same WQE that wrote the completion body and
seal. This removes one WR per completion, removes the scratch write, and removes
the remaining cross-WQE body-versus-doorbell assumption.

Implementation notes:

- keep the source slot stable until the covering send-CQ checkpoint retires;
- keep periodic signaled checkpoints, but force one before source-ring wrap,
  under source-credit pressure, and before session teardown;
- after verbs accepts the WQE, local cleanup is CQ retirement or QP teardown,
  not ordinary rollback.

Descriptor-cache misses use a two-WR chain on the same QP:

```text
Descriptor cache hit:
    WR 1: full completion slot WRITE_WITH_IMM

Descriptor cache miss:
    WR 1: descriptor body + trailing descriptor seal, ordinary RDMA WRITE
    WR 2: full completion slot WRITE_WITH_IMM
```

One source owner covers both source ranges. The final completion-slot WIMM is
the receiver notification and optional signaled checkpoint. The receiver then
validates descriptor body/seal, CPU-publishes descriptor `readyVersion`, and
only then CPU-publishes completion `readyEpochSlots[slot]`.

Failure rules:

```text
descriptor WR not posted:
    normal rollback

descriptor WR posted but completion WIMM not posted:
    QP reset-required
    source remains teardown-owned

completion WIMM posted:
    CQ retirement or completed QP teardown owns release
```

### 1.2 Frontend Completion Mailbox v27 Layout

Remove frontend-client `publishedEpoch` and `peerCompletionDoorbellScratch`
from `CitusRemoteExecClientCompletionMailbox`. Keep `publishedEpoch` in:

- `CitusRemoteExecLocalCompletionMailbox`, where the service consumes
  backend-produced local completion records;
- `CitusRemoteExecPeerCommandCompletionRing`, where the peer service-to-service
  completion ring still uses aggregate ring semantics.

Target frontend mailbox shape:

```c
typedef struct CitusRemoteExecClientCompletionMailbox
{
    uint32_t protocolVersion;
    uint32_t mailboxBytes;
    uint32_t slotCount;
    uint32_t reserved0;

    _Alignas(64) uint64_t consumedEpoch;
    uint8_t consumedPadding[56];

    _Alignas(64)
    uint64_t readyEpochSlots[CITUS_REMOTE_EXEC_CLIENT_COMPLETION_MAILBOX_SLOTS];

    CitusRemoteExecClientCompletionSlot
        completionSlots[CITUS_REMOTE_EXEC_CLIENT_COMPLETION_MAILBOX_SLOTS];

    HomerResultDescriptorTable resultDescriptorTable;
} CitusRemoteExecClientCompletionMailbox;
```

On mailbox open, initialize:

```c
session->lastConsumedCompletionEpoch =
    acquire_load(&mailbox->consumedEpoch);
```

The receiver service owns a session-local publication cursor:

```c
uint64_t frontendCompletionPublishedEpoch;
```

Producer credit is:

```text
frontendCompletionPublishedEpoch - acquire_load(mailbox->consumedEpoch)
    < CITUS_REMOTE_EXEC_CLIENT_COMPLETION_MAILBOX_SLOTS
```

Cursor lifecycle:

```text
Mailbox creation:
    consumedEpoch = 0
    all readyEpochSlots = 0
    frontendCompletionPublishedEpoch = 0
    pending doorbell count = 0
    no completion source owners

First peer/local binding:
    assert frontendCompletionPublishedEpoch == consumedEpoch
    assert no pending publication
    assert no posted completion source owners

Normal publication:
    nextEpoch = frontendCompletionPublishedEpoch + 1
    credit requires nextEpoch - consumedEpoch <= slotCount

Successful CPU publication:
    release-store readyEpochSlots[slot] = nextEpoch
    frontendCompletionPublishedEpoch = nextEpoch

Normal close:
    close-command completion has been ACKed
    frontendCompletionPublishedEpoch == consumedEpoch
    no pending completion doorbells
    no pending visibility publication
    no posted source owners

Failure teardown:
    mark session failed
    drain/reset QP
    retire teardown-owned sources
    do not reuse the mailbox for another session generation
```

Both the local CPU publisher and the peer receiver publisher use the same
service-owned cursor. Removing the mailbox field forces all direct
`mailbox->publishedEpoch` references on this frontend-completion path to be
replaced with service-local cursor state.

### 1.3 Descriptor Trailing Seal

Add a trailing descriptor seal to `HomerResultDescriptorSlot`:

```c
typedef struct HomerResultDescriptorSlot
{
    uint64_t bodyVersion;
    uint32_t descriptorBytes;
    uint32_t flags;
    CitusTupleSinkQueueAttachment queue;
    CitusTupleViewContract contract;
    uint64_t sealVersion;
} HomerResultDescriptorSlot;
```

The peer sender fills `bodyVersion`, descriptor body, then `sealVersion`. The
peer must not RDMA-write frontend-visible descriptor `readyVersion`. The
receiver service validates descriptor body/seal and CPU-publishes descriptor
`readyVersion` before CPU-publishing completion `readyEpochSlots[slot]`.

### 1.4 Source-Slot State

Use a simple monotonic source state:

```text
FREE -> RESERVED -> FILLED -> POSTED -> RETIRED -> FREE
```

Suggested enum:

```c
typedef enum PeerClientCompletionSourceState
{
    SOURCE_FREE,
    SOURCE_RESERVED,
    SOURCE_FILLED,
    SOURCE_POSTED,
    SOURCE_RETIRED,
    SOURCE_RESET_REQUIRED
} PeerClientCompletionSourceState;
```

Rules:

- failure before posting rolls back the reservation;
- after one or more WRs are accepted, ownership is send-CQ retirement or QP
  teardown;
- no cleanup helper may clear a posted owner merely to empty a table;
- source reuse before retirement is fatal in debug builds and counted in stats
  builds.

### 1.5 Receiver Publication State

Add per-session receiver state. A single `PendingClientCompletionPublication`
is not sufficient by itself because the recv CQ can deliver multiple
same-session completion CQEs before the first completion body becomes
CPU-visible. The generic connection FIFO should still be removed, but consumed
CQEs must be retained as a per-session count.

```c
typedef struct PendingClientCompletionPublication
{
    bool active;
    uint64_t epoch;
    uint32_t mailboxSlot;
    uint32_t descriptorVersion;
    uint32_t visibilityRetryCount;
    uint64_t connectionGeneration;
} PendingClientCompletionPublication;

typedef struct ClientCompletionReceiverState
{
    uint64_t publishedEpoch;
    uint16_t pendingDoorbellCount;
    bool visibilityPending;
    PendingClientCompletionPublication pendingPublication;
} ClientCompletionReceiverState;
```

Canonical recv-CQ behavior:

```text
on valid client-completion CQE:
    resolve token to session
    pendingDoorbellCount++
    add session to pending-publication ready set

exact publication action:
    while budget remains and pendingDoorbellCount > 0:
        process publishedEpoch + 1
        on success:
            pendingDoorbellCount--
            continue
        on visibility pending:
            stop and retain count
        on corruption:
            fail session
```

Bound `pendingDoorbellCount` by the completion mailbox slot count. A count above
`CITUS_REMOTE_EXEC_CLIENT_COMPLETION_MAILBOX_SLOTS` is an overrun/protocol
error.

For each completion selected by the exact publication action:

1. Resolve the token to an exact session index and session generation.
2. Verify the CQE came from the connection generation currently bound to the
   session.
3. Compute `expectedEpoch = frontendCompletionPublishedEpoch + 1` and the
   mailbox slot from that epoch.
4. Acquire-fence after CQE observation.
5. Copy the entire completion slot to stack-local storage.
6. Validate completion seal, protocol/header sizes, command sequence, command
   kind, command state, and flags.
7. If a new descriptor is referenced, copy and validate the descriptor body plus
   trailing seal, then CPU release-store descriptor `readyVersion`.
8. CPU release-store completion `readyEpochSlots[slot]`.
9. Advance service-local `frontendCompletionPublishedEpoch`.

Do not write an aggregate frontend `publishedEpoch` into shared memory.

### 1.6 Visibility Retry

A CQE should normally imply the WQE's body is available, but the receiver should
not misclassify a transient host-visibility delay as corruption. Use an exact
session-local pending state rather than a generic doorbell FIFO:

- record expected epoch, slot, descriptor version, connection generation, and a
  compact fingerprint of the observed image;
- retry from a scheduler-visible exact action on the next pass;
- do not process a later completion for that session while this one is pending;
- do not CPU-publish ready while visibility is pending;
- fail an unchanged invalid image only after both 64 identical observations and
  at least 10 microseconds since the first observation;
- reset the retry count if the observed image changes.

This retry is a receiver-service visibility bridge before CPU publication. It
does not weaken the client-side invariant: after CPU-published ready, a
body/seal mismatch is fatal.

Keep the slow-path retry state only after the first failed validation. Do not
compute a fingerprint on every successful completion. The fingerprint should
include:

```text
expected epoch
completion protocol/header bytes
hot command sequence/kind/state/flags
completion seal
descriptor bodyVersion/sealVersion
connection generation
```

### 1.7 Client Changes

- Initialize `lastConsumedCompletionEpoch` from frontend mailbox
  `consumedEpoch`, not removed `publishedEpoch`.
- Keep ready-copy-ready validation and trailing completion seal validation in
  `HomerClientCopyClientCompletionSlotStable()`.
- Use separate receiver-service and frontend-client statuses. Receiver pending
  states are retryable only before CPU publication.

Receiver-service validation:

```text
RECEIVER_PUBLICATION_READY
RECEIVER_BODY_VISIBILITY_PENDING
RECEIVER_DESCRIPTOR_VISIBILITY_PENDING
RECEIVER_PROTOCOL_MISMATCH
```

Frontend client slot status:

```text
SLOT_NOT_READY
SLOT_READY
SLOT_OVERRUN
SLOT_SEAL_MISMATCH
SLOT_DESCRIPTOR_MISMATCH
SLOT_PROTOCOL_MISMATCH
```

Any body/seal/descriptor mismatch after CPU-published ready is fatal. If the
frontend sees ready but finds descriptor pending or inconsistent, the receiver
published too early or the descriptor protocol is broken.

### 1.8 Slice 1 Acceptance

Run no-stats builds unless a diagnostic counter run is explicitly requested:

- remote RDMA c1, 100,000 transactions;
- remote RDMA c4, 10 consecutive warmed runs;
- remote RDMA c4 plus concurrent remote basebackup, five repetitions;
- forced descriptor miss and shape change;
- eight-slot mailbox wrap stress;
- delayed send-CQ retirement;
- body-post failure injection;
- session close with outstanding completion owners.

Required results:

```text
0 seal mismatches
0 descriptor mismatches
0 source reuse before retirement
0 completion ordering gaps
0 failed transactions
```

## Slice 2: Canonical Recv-CQ Collector And Typed Event Materialization

The current optional-handler design in
`TupleSinkServiceDrainPeerConnectionEvents()` was an intermediate patch. It lets
callers poll the same physical recv CQ with different semantic knowledge: when
the completion handler is absent, the code queues tokens in
`pendingClientCompletionDoorbellTokens`; when the main pump supplies the
handler, it dispatches immediately. That model should be removed.

The physical recv-CQ action is indivisible:

```text
poll CQ
-> validate every returned CQE
-> decode every CQE
-> perform the mandatory transport transition
-> materialize any deferred semantic readiness
-> batch-repost receive WQEs
```

The scheduler may choose which physical CQ to poll and how many CQ batches to
consume. Once `ibv_poll_cq()` returns a CQE, the scheduler must not defer or
selectively suppress decoding it because the CQE has already been removed from
the hardware CQ. Decoding does not necessarily mean running expensive semantic
work; it means converting the CQE into durable typed state.

### 2A Permanent Recv Dispatcher

Introduce one permanent dispatcher in
`remote_execution_peer_transport_rdma.h`:

```c
typedef struct HomerPeerRecvDispatcher
{
    bool (*onClientCompletion)(
        TupleSinkServicePeerConnectionHandle *connection,
        uint64_t connectionGeneration,
        uint32_t token,
        void *context,
        char *error,
        size_t errorBytes);

    bool (*onClientCommand)(
        TupleSinkServicePeerConnectionHandle *connection,
        uint64_t connectionGeneration,
        uint32_t token,
        void *context,
        char *error,
        size_t errorBytes);

    bool (*onPayload)(
        TupleSinkServicePeerConnectionHandle *connection,
        uint64_t connectionGeneration,
        uint32_t sinkToken,
        void *context,
        char *error,
        size_t errorBytes);

    bool (*onControl)(
        TupleSinkServicePeerConnectionHandle *connection,
        uint64_t connectionGeneration,
        void *context,
        char *error,
        size_t errorBytes);

    void *context;
} HomerPeerRecvDispatcher;
```

Store it in `TupleSinkServicePeerTransportState` or as a pointer from each
connection to the transport-global dispatcher:

```c
const HomerPeerRecvDispatcher *recvDispatcher;
```

Install it once during service/peer-transport initialization. An active
steady-state connection without a dispatcher is a fatal internal error.

Replace the current optional-handler drain API with a physical collector API:

```c
typedef struct HomerRecvCqBudget
{
    uint16_t maxPollBatches;
    uint16_t maxCqes;
} HomerRecvCqBudget;

typedef struct HomerRecvCqResult
{
    uint16_t polls;
    uint16_t emptyPolls;
    uint16_t cqes;
    uint16_t controlDoorbells;
    uint16_t commandDoorbells;
    uint16_t completionDoorbells;
    uint16_t payloadDoorbells;
    uint16_t recvWqesReposted;
} HomerRecvCqResult;

bool TupleSinkServiceDrainPeerConnectionRecvCq(
    TupleSinkServicePeerConnectionHandle *connection,
    const HomerRecvCqBudget *budget,
    HomerRecvCqResult *result,
    char *error,
    size_t errorBytes);
```

This function always uses the permanent dispatcher. CM processing should become
a separate function:

```c
bool TupleSinkServiceDrainPeerConnectionCmEvents(...);
```

That prevents a caller interested only in connection lifetime from accidentally
polling the mixed recv CQ.

Inside the collector, dispatch by immediate kind:

```c
switch (doorbellKind)
{
    case CONTROL:
        dispatcher->onControl(...);
        break;
    case CLIENT_COMMAND:
        dispatcher->onClientCommand(...);
        break;
    case CLIENT_COMPLETION:
        dispatcher->onClientCompletion(...);
        break;
    case PAYLOAD:
        dispatcher->onPayload(...);
        break;
    default:
        protocol error;
}
```

No scheduler phase mask may suppress a branch in this switch. The collector
processes CQEs in the order returned by `ibv_poll_cq()`. It may batch
receive-WQE reposts, but must not reorder decoded events within one CQ.

### 2B Typed Event Materialization

Do not replace the old FIFO with another generic queue of opaque CQEs or
immediate tokens. The RDMA-written mailbox/ring memory already contains the
semantic object. The dispatcher materializes only compact typed readiness.

| CQE kind | Durable semantic data | Dispatcher materializes | Generic FIFO needed? |
| --- | --- | --- | --- |
| Client completion | Completion slot and descriptor table | Per-session pending doorbell count, next expected epoch, visibility-pending state | No |
| Client command | Command mailbox/ring | Session command-ready bit | No |
| Payload | Payload ring and published frontier/in-band headers | Stream-ready bit or coalesced frontier | No |
| Control mailbox | Control mailbox sequence/slots | Connection mailbox-ready bit | No |
| Send completion | Source-owner FIFO/table | Immediate owner retirement and resource-pressure ready bit | No |
| Disconnect/lifetime | Connection state | Connection lifetime-ready state | No |

Client completion is edge-counted:

```c
session->clientCompletionReceiver.pendingDoorbellCount;
session->clientCompletionReceiver.pendingPublication;
pendingCompletionPublicationSessions bitmap;
```

The dispatcher increments `pendingDoorbellCount` and sets the session bit. On
the normal fast path, it immediately tries to publish one event:

```c
receiver->pendingDoorbellCount++;
HomerServiceSetPendingCompletionPublicationReady(sessionIndex);

HomerServiceTryPublishPendingClientCompletions(
    session,
    /* maxEvents = */ 1,
    error,
    errorBytes);
```

Usually the body and descriptor are already visible, so the count goes from
`0 -> 1 -> 0` in the same collector call. Only exceptional body/descriptor
visibility-pending cases remain in the ready set for the semantic phase.

Client command readiness is level-triggered:

```text
durable data: remote command mailbox and epochs/ready words
materialized state: remoteCommandReadySessions bitmap
dispatcher action: set bit for exact session
executor: drain commands within budget, recheck mailbox, re-set bit when unread commands remain
```

Multiple command doorbells safely coalesce because the command mailbox retains
all unread commands.

Payload readiness is level-triggered:

```c
bool recvReadyEnqueued;
uint64_t observedDoorbellCount; /* diagnostic only */
```

Use an intrusive ready queue or indexed ready bitmap for payload streams:

```text
doorbell:
    enqueue stream only when not already enqueued

executor:
    drain within byte/record budget
    recheck ring
    remain/re-enqueue when data remains
```

Control mailbox readiness is level-triggered:

```c
bool controlMailboxReady;
```

The control executor drains within message budget, rechecks the mailbox, and
leaves the connection ready when more messages remain.

Connection lifetime CQEs apply the transport fact immediately:

```text
connection state = disconnected / failed / closing
lifetimeWorkReady = true
```

Heavy teardown runs later from the lifetime executor.

Send-CQ collection remains separate but follows the same principle:

```text
poll send CQ
-> decode every owner
-> retire source ownership immediately
-> set exact resource-pressure ready facts
```

Source retirement is not deferrable semantic work because buffer lifetime
depends on it.

### 2C Two-Phase Service Pass

Replace the single plan that mixes discovery and semantic work with:

```c
HomerServiceProgressPass()
{
    BuildCollectorPlan();
    ExecuteCollectorPlan();

    BuildSemanticPlanFromMaterializedReadyState();
    ExecuteSemanticPlan();

    ReinsertStillReadyWork();
}
```

Collector actions are physical:

```text
DRAIN_CRITICAL_RECV_CQ(connection)
DRAIN_FOREGROUND_RECV_CQ(connection)
DRAIN_BULK_RECV_CQ(connection)
DRAIN_SEND_CQ(connection)
DRAIN_CM_EVENTS(connection)
```

A collector grant contains only:

```text
physical connection
poll-batch budget
CQE budget
```

It never contains an allowed semantic event kind.

Semantic actions are typed:

```text
PUBLISH_PENDING_CLIENT_COMPLETIONS(session)
FORWARD_REMOTE_COMMANDS(session)
PUMP_PAYLOAD_STREAM(stream)
DRAIN_CONTROL_MAILBOX(connection)
RUN_RESOURCE_RELIEF(session)
RUN_CONNECTION_LIFETIME(connection)
```

The semantic plan is built after the collector phase, so events discovered in
this service pass can produce work in the same pass.

Client completion publication remains in the collector fast path only as a
mandatory materialization step for already-visible completions. The semantic
phase handles only visibility-pending completion publication,
descriptor-pending publication, retry, and escalation.

Collector priority:

```text
1. demanded critical recv CQs
2. send CQs under source pressure
3. foreground recv CQs
4. periodic control/lifetime CQs
5. bulk recv CQs
```

Semantic priority:

```text
1. pending client-completion publication
2. remote command forwarding
3. resource-pressure relief
4. foreground payload
5. control mailbox
6. bulk payload
7. lifetime/maintenance
```

### 2D Remove Optional Handlers And Generic Token FIFOs

After the collector and typed ready state pass validation:

1. Remove optional handlers from `TupleSinkServicePeerPumpGrant`.
2. Remove all steady-state recv-CQ polling outside the collector.
3. Remove completion, command, and payload pending token arrays.
4. Remove queue-drain compatibility helpers.
5. Add debug counters asserting exactly one steady-state poll owner per CQ.
6. Treat any unknown or unbound token as a protocol error.
7. Preserve bootstrap-only polling behind an explicit
   `connection->bootstrapComplete == false` assertion.

Bootstrap/setup code may retain setup-specific polling until the permanent
dispatcher is installed. After `bootstrapComplete`, a debug assertion rejects
any noncanonical recv-CQ poll.

Use a direct connection-local binding table for stale-token protection:

```c
typedef struct HomerClientCompletionTokenBinding
{
    bool active;
    uint32_t token;
    uint32_t sessionIndex;
    uint64_t sessionGeneration;
    uint64_t connectionGeneration;
} HomerClientCompletionTokenBinding;
```

The dispatcher resolves:

```text
token -> session index -> session generation -> bound connection generation
```

A stale token, closed session, or wrong connection generation is fatal. No
linear session-table scan may occur on the CQE hot path.

Deterministic tests before deleting the FIFO:

1. One CQ batch containing control, command, completion, and payload events.
2. Semantic plan grants only control work, but collector receives a completion;
   completion must still be published or materialized.
3. Two completion WIMMs for one session while the first body is
   visibility-pending.
4. Pending count reaches two, then both epochs publish in order.
5. Multiple command doorbells coalesce into one ready bit and all ring records
   drain.
6. Multiple payload doorbells coalesce without losing payload.
7. Session token reuse happens only after old connection-generation teardown.
8. Hidden peer-control helper runs while mixed CQEs are pending and does not
   poll the recv CQ.
9. Connection disconnect arrives with semantic work ready; connection state
   changes immediately and teardown runs later.
10. CQ batch receive buffers are reposted once as a batch.
11. Ten consecutive c4 runs plus five c4-with-basebackup runs pass.
12. Zero references to deleted pending-token FIFOs remain in the source tree.

## Slice 3: Complete Stage 6 Before More Stage 7 Work

The earlier Stage 6 plan reserved command, completion, and resource-pressure
facts, but command readiness is the only substantially implemented part. Finish
the missing readiness before doing more Stage 7 topology/performance work.

### 3A Backend-Completion-Ready Bitmap

Add both fields to backend startup identity:

```c
uint32_t serviceSessionIndex;
uint64_t serviceSessionGeneration;
```

Backend startup must:

```text
open control shm
validate protocol and mapping size
mmap control region
retain mapping for backend lifetime
munmap/close during backend exit
```

After local completion publication:

```text
release-store backend completion publishedEpoch
fetch_or completionReadySessions bit
```

Add `HomerServiceAppendBackendCompletionBitmapSources()` to exchange the bitmap
and then validate session index, session generation, mailbox protocol, and
actual unread completion state before appending exact candidates. Re-set a bit
when more records remain or staging was blocked. Keep one fallback broad scan
every 1024 service passes; warmed c1/c4 should have zero fallback discoveries.

### 3B Critical Recv-CQ Demand Facts

Track per session:

```c
bool remoteCommandAwaitingTerminal;
```

Track per critical connection:

```c
uint32_t clientCommandsAwaitingTerminal;
bool criticalRecvPollDemanded;
```

On successful remote command publication:

```text
assert session flag is false
session flag = true
connection count++
```

On CPU publication of `COMPLETED` or `FAILED`:

```text
assert session flag is true
session flag = false
connection count--
```

`STARTED` does not decrement demand. Session failure/teardown clears the flag
and decrements the connection count exactly once. While demand is nonzero, poll
the exact critical recv CQ every service pass with no empty-poll backoff.

The scheduler action is:

```text
DRAIN_CRITICAL_CLIENT_COMPLETION_RECV_CQ
```

It should run before peer-control mailbox work, foreground payload, bulk
payload, and maintenance. Connections without outstanding client commands keep a
low-rate periodic recv-CQ fallback for control/lifetime traffic.

### 3C Resource-Pressure Readiness

Use `resourcePressureSessions` for session-owned send resources. Add per
session:

```c
uint32_t blockedReasonMask;
```

Possible bits:

```text
COMMAND_SOURCE_CREDIT
COMPLETION_SOURCE_CREDIT
COMMAND_OWNER_FIFO
COMPLETION_OWNER_FIFO
```

Set a reason when work actually blocks. On typed send-CQ retirement, set
`resourcePressureSessions` only when a blocked reason has become satisfiable.
Do not set the bitmap on every CQ retirement.

The candidate builder exchanges the bitmap and schedules exact blocked
sessions. Payload streams remain on indexed stream-ready structures because
they are not all session-owned.

## Slice 4: Critical-First Stage 7d Service Scheduling

Do not add another QP/CQ as the next step. The current transport already maps
traffic classes onto separate QP/CQ-owning connections, and basebackup opens on
the replication lane. Stage 7d should tune the two-phase collector/semantic
service pass from Slice 2 into service CPU/progress isolation:

1. Assert lane placement at bind/open:
   `CRITICAL_CONTROL` for client command/completion,
   `FOREGROUND_PAYLOAD` for SQL tuple payload, and `BULK_PAYLOAD` for
   basebackup.
2. Use fixed service-loop priority with bounded per-pass work:

   ```text
   pending CPU completion publications: up to 32
   critical recv-CQ: one CQ batch per demanded connection
   remote command forwarding: up to 8 commands
   source-pressure send-CQ retirement: one CQ batch
   foreground payload: up to 2 grants
   peer control: one grant
   bulk payload: at least one grant when ready
   maintenance/lifetime: once every 64 passes or immediately when overdue
   ```

3. Suppress basebackup's bounded producer-frontier busy spin while critical
   command demand is nonzero.
4. Prioritize which connection/CQ is polled first in the collector phase:

   ```text
   critical-control connection recv CQ first
   foreground connection second
   bulk connection later
   ```

   Do not reorder CQEs returned from one CQ. Within each CQ batch, dispatch CQEs
   in returned order and batch only receive-WQE reposting. Do not decode a
   single CQ batch and process client-completion events ahead of earlier
   payload/control events from the same CQ. Semantic prioritization happens only
   after the collector materializes typed ready state.

These bounds are initial compile-time constants. Performance validation may tune
their values without changing ownership semantics. Basebackup producer-frontier
busy spin is disabled whenever any critical connection has
`clientCommandsAwaitingTerminal > 0`.

Minimum no-stats medians before calling Stage 7d performance recovered:

```text
remote c1: at least 4.0k TPS
remote c4: at least 10.8k TPS
remote basebackup: no slower than 4.4 s
concurrent c1 p99 increase: below 10%
concurrent c4: zero failures and zero completion mismatches
```

The target band remains roughly `4.5k` c1 and `11.2k` c4.

## Slice 5: Remove Remaining Client Hot-Path Copies

### 5A Completion View

Stop reconstructing large legacy completions in the hot path. Introduce a view:

```c
typedef struct HomerClientCompletionView
{
    CitusRemoteExecCommandCompletionHot hot;
    uint64_t descriptorVersion;
    const HomerClientCachedResultDescriptor *descriptor;
} HomerClientCompletionView;
```

The pointer must reference a session-owned immutable cached descriptor copy, not
the shared-memory descriptor slot directly. Move full legacy conversion behind
explicitly named compatibility APIs.

### 5B Narrow Result-Sink Rebind

Replace synthetic full-completion pre-arm with:

```c
bool HomerClientRebindResultSink(
    HomerClientResultSink *sink,
    uint64_t descriptorVersion,
    uint64_t resultGeneration,
    uint64_t startByteTail,
    uint64_t firstRecordOrdinal,
    char *error,
    size_t errorBytes);
```

It updates only command-generation binding and consumed-head fields.

### 5C Exact Command Sequence Reservation

The audited plan proposes:

```text
HomerClientReserveDirectCommand()
-> pre-arm exact reserved sequence
-> fill command
-> HomerClientCommitDirectCommand()
```

The hard part is abort/rollback. Do not build a broad undo protocol unless an
actual caller needs it. For the current pgbench-Homer direct-command client, the
reasonable invariant is:

```text
a successful direct-command reservation must be followed by commit in the same
control-flow path before returning to the event loop
```

Implementation can enforce this with a small session-local reservation state:

```c
typedef struct HomerClientDirectCommandReservation
{
    bool active;
    uint64_t sequence;
    uint32_t slotIndex;
} HomerClientDirectCommandReservation;
```

API behavior:

```text
Reserve:
    check no active reservation
    check ring credit
    choose exact sequence and slot
    set reservation active
    do not publish ready or mailbox tail

Fill/pre-arm:
    may fail
    failure clears reservation only
    slot bytes remain unpublished and may be overwritten later

Commit:
    release-store slot readySeq
    release-store mailbox publishedEpoch
    set command-ready bitmap
    clear reservation

Return to event loop:
    debug assert reservation.active == false
```

- no second reservation while one is active;
- failure before command publication clears only the reservation; no reader can
  observe unpublished slot bytes, so do not zero or mark the slot unused;
- after the command ready word is published, there is no rollback; completion,
  session close, or session teardown must resolve it;
- debug builds assert no active unpublished reservation at API boundaries that
  return to pgbench.

If a future caller wants to reserve and then perform arbitrary fallible work
before commit, that should be a separate API design with explicit abort
semantics. It should not be hidden inside the hot pgbench path.

## Slice 6: Close Earlier Correctness Caveats

- **Partial-post reset semantics**: zero WRs posted means normal rollback; one
  or more WRs accepted before final publication means QP reset-required and
  teardown-owned owners; final publication posted means CQ retirement or QP
  teardown owns release.
- **Bounded result-header mismatch escalation**: retain head/tail/generation and
  header fingerprint; retry while observations change; hard error after eight
  identical event-loop retries.
- **Failed-query `ERROR+EOS`**: publish a terminal error record at the exact
  result frontier, discarding unpublished partial batches.
- **Generation-safe stale grants**: carry stream index plus generation in every
  payload grant; stale grants perform no stream access and increment
  `staleGrantRejected`.
- **Work-budget feedback**: when an executor reports
  `budgetExhausted && moreReady`, reinsert the exact machine into the ready set
  instead of relying on broad rediscovery.

## Slice 7: Lower-Priority Basebackup Cleanups

- Add fragment identity: `fragmentIndex`, `fragmentCount`, and
  `fragmentOffset`, while keeping `recordOrdinal` as semantic object identity.
- Reserve exact basebackup producer geometry using the actual semantic header
  size.
- Assert direct-registered producer-ring teardown: posted source frontier equals
  CQ-retired source frontier, no owner FIFO references remain for the stream
  generation, QP is drained or reset complete, and MR deregistration happens
  afterward.

## Required Instrumentation

Add these counters before Slice 1 validation:

```text
clientCompletionWimms
clientCompletionBodyWrs
clientCompletionWrsPerEvent
clientCompletionDirectDispatches
clientCompletionPendingVisibility
clientCompletionSealMismatches
clientCompletionDescriptorPending
clientCompletionDescriptorMismatches
clientCompletionSourceReserved
clientCompletionSourcePosted
clientCompletionSourceRetired
clientCompletionSourceReuseBeforeRetire
clientCompletionCpuReadyPublishes

recvCqCollectorCalls
recvCqCollectorPolls
recvCqCollectorEmptyPolls
recvCqCqes
recvCqCqesByKind[control]
recvCqCqesByKind[command]
recvCqCqesByKind[completion]
recvCqCqesByKind[payload]

completionDoorbellsDecoded
completionDoorbellsPublishedInline
completionDoorbellsDeferred
completionPendingCountHighWater
completionVisibilityRetries

commandReadyAlreadySet
payloadReadyAlreadySet
controlReadyAlreadySet

semanticActionsBuiltAfterCollector
semanticActionsExecutedSamePass

unknownDoorbellKind
unknownDoorbellToken
staleDoorbellGeneration
noncanonicalRecvCqPollAttempts

criticalConnectionsDemanded
criticalRecvPollActions
criticalRecvPollEmpty
backendCompletionBitmapSets
backendCompletionBitmapExchanges
backendCompletionBitmapFallbackDiscoveries
resourcePressureBitmapSets
resourcePressureBitmapExchanges
staleGrantRejected
```

Add low-overhead timestamp/cycle samples for same-host intervals:

```text
frontend command commit -> frontend completion lease acquisition
receiver CQE observation -> readyEpoch CPU store
readyEpoch CPU store -> frontend lease acquisition
sender WIMM post -> sender local send-CQE retirement
```

The first and third intervals are on the frontend host; the second is entirely
inside the receiver service; the fourth is entirely on the sender host. Use
sampled `CLOCK_MONOTONIC_RAW` or verified invariant-TSC measurements. Do not
subtract unsynchronized host-local TSC values across machines.

Key performance measurements:

```text
CQE observation -> completion ready CPU store
CQE observation -> semantic action execution
collector polls per transaction
empty critical polls per transaction
```

## Things That Need Care Before Implementation

- The one-WQE full-slot WIMM plan is the right correction, but only if the
  receiver service does not CPU-publish ready until descriptor side-table
  visibility is also validated.
- Visibility retry must remain pre-publication only. Retrying after
  CPU-published ready would hide corruption.
- Removing frontend `publishedEpoch` touches cursor initialization, receiver
  credit checks, lifecycle checks, and teardown assertions. Do it in the same
  protocol bump as the one-WQE WIMM change.
- The permanent recv-CQ dispatcher is desirable, but it is a transport ownership
  refactor. Keep it after the v27 correctness slice so we can validate the
  publication contract independently.
- The scheduler may defer semantic execution, but it must never defer or
  selectively suppress decoding of a CQE that has already been polled. The
  replacement for optional handlers/FIFOs is durable RDMA mailbox/ring data,
  typed ready state, and per-session completion counts where edges cannot
  coalesce.
- Backend-completion bitmap work maps the control region into socketless
  backends. That is straightforward but not tiny: startup data, protocol
  validation, cleanup, and fallback broad-scan metrics must all move together.
- Direct command reservation abort should start as a tight invariant for the
  pgbench-Homer direct path, not as a complex general rollback mechanism.
- Slices 6 and 7 are not implementation-ready from this note alone. Before a
  developer starts either one, expand the target slice with: files/functions
  changed, new fields/API, state transitions, failure behavior, ABI bump,
  targeted tests, acceptance, and old code removed. `ERROR+EOS` is a protocol
  change, and partial-post cleanup needs a per-path matrix rather than one
  shared bullet.

## Slice Readiness Assessment

| Slice       | Status                                                                               |
| ----------- | ------------------------------------------------------------------------------------ |
| Slice 1     | Implementation-ready with cursor lifecycle, descriptor WR chain, and pending count   |
| Slice 2     | Implementation-ready as canonical collector, typed ready state, and FIFO removal     |
| Slice 3     | Direction correct; implement exact startup mapping and blocked-reason transitions    |
| Slice 4     | Direction correct; tune two-phase pass, bounded fairness, and intra-CQ order         |
| Slice 5A/5B | Implementation-ready with descriptor-cache lifetime clarification                    |
| Slice 5C    | Correct and intentionally narrow                                                     |
| Slice 6     | Backlog summary; needs a stage-template expansion before implementation              |
| Slice 7     | Lower-priority summary; needs a stage-template expansion before implementation        |

## Final Order

```text
1. Completion protocol v27:
   one full-slot WIMM, descriptor CPU gate, remove client publishedEpoch.

2. Canonical recv-CQ collector:
   permanent dispatcher, typed event materialization, no optional handler/FIFO path.

3. Complete Stage 6:
   backend completion bitmap, resource-pressure bitmap,
   critical recv-CQ demand facts.

4. Critical-first Stage 7d service scheduling:
   no additional QP, no dedicated thread, suppress bulk spin under foreground load.

5. Remove client legacy completion reconstruction and full pre-arm copies.

6. Close partial-post, mismatch-escalation, ERROR+EOS, and stale-grant caveats.

7. Complete lower-priority basebackup fragment/reservation cleanup.
```
