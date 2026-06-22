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

Add per-session receiver state:

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

uint64_t frontendCompletionPublishedEpoch;
PendingClientCompletionPublication pendingCompletionPublication;
```

On a completion WIMM CQE:

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
- fail after eight identical retries where the expected epoch, body fingerprint,
  completion seal, descriptor body/seal, and connection generation are
  unchanged;
- reset the retry count if the observed image changes.

This retry is a receiver-service visibility bridge before CPU publication. It
does not weaken the client-side invariant: after CPU-published ready, a
body/seal mismatch is fatal.

### 1.7 Client Changes

- Initialize `lastConsumedCompletionEpoch` from frontend mailbox
  `consumedEpoch`, not removed `publishedEpoch`.
- Keep ready-copy-ready validation and trailing completion seal validation in
  `HomerClientCopyClientCompletionSlotStable()`.
- Split statuses so logs and scheduler facts distinguish:

```text
SLOT_NOT_READY
SLOT_READY
SLOT_DESCRIPTOR_PENDING
SLOT_BODY_VISIBILITY_PENDING
SLOT_OVERRUN
SLOT_SEAL_MISMATCH
SLOT_PROTOCOL_MISMATCH
```

`DESCRIPTOR_PENDING` and `BODY_VISIBILITY_PENDING` are retryable before
receiver CPU publication. `SEAL_MISMATCH` after CPU-published ready is fatal.

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

## Slice 2: Canonical Recv-CQ Ownership

The current optional-handler design in
`TupleSinkServiceDrainPeerConnectionEvents()` was an intermediate patch. Replace
it with one permanent typed dispatcher installed at peer transport creation:

```c
typedef struct HomerPeerRecvDispatcher
{
    TupleSinkServicePeerCommandDoorbellHandler commandHandler;
    TupleSinkServicePeerClientCompletionDoorbellHandler clientCompletionHandler;
    TupleSinkServicePeerPayloadDoorbellHandler payloadHandler;
    void *serviceContext;
} HomerPeerRecvDispatcher;
```

Callback signatures should include connection identity:

```c
bool ClientCompletionHandler(
    TupleSinkServicePeerConnectionHandle *connection,
    uint64_t connectionGeneration,
    uint32_t token,
    void *context,
    char *error,
    size_t errorBytes);
```

Simplify the drain API so it always invokes the dispatcher and only takes
physical drain choices:

```c
TupleSinkServiceDrainPeerConnectionEvents(
    connection,
    drainRecvCq,
    drainCm,
    budget,
    result,
    ...);
```

Delete `pendingClientCompletionDoorbellTokens` and
`TupleSinkServiceDrainQueuedPeerClientCompletionDoorbells()`. A completion
arriving without a valid token-to-session binding is a protocol error. Preserve
CQE order as returned by `ibv_poll_cq`; batch receive-WQE reposting remains
valid.

## Slice 3: Complete Stage 6 Before More Stage 7 Work

The earlier Stage 6 plan reserved command, completion, and resource-pressure
facts, but command readiness is the only substantially implemented part. Finish
the missing readiness before doing more Stage 7 topology/performance work.

### 3A Backend-Completion-Ready Bitmap

- Add `serviceSessionIndex` to backend startup identity.
- Let socketless backends map `CITUS_REMOTE_EXEC_CONTROL_SHM_NAME` and validate
  protocol/size.
- In backend `PublishCompletionToMailbox()`, fill the completion slot, publish
  the backend local completion mailbox frontier, then set the control-region
  completion-ready bit.
- Add `HomerServiceAppendBackendCompletionBitmapSources()` to exchange the
  bitmap, validate generation/mailbox state, append exact candidates, and
  re-set a bit when more records remain or staging is blocked.
- Keep one fallback broad scan every 1024 service passes; warmed c1/c4 should
  have zero fallback discoveries.

### 3B Critical Recv-CQ Demand Facts

Maintain per critical connection:

```c
uint32_t clientCommandsAwaitingTerminal;
bool criticalRecvPollDemanded;
```

When a remote client command is forwarded, increment demand. When its terminal
completion is CPU-published, decrement demand. While demand is nonzero, poll
the exact critical recv CQ every service pass with no empty-poll backoff.

The scheduler action is:

```text
DRAIN_CRITICAL_CLIENT_COMPLETION_RECV_CQ
```

It should run before peer-control mailbox work, foreground payload, bulk
payload, and maintenance. Connections without outstanding client commands keep a
low-rate periodic recv-CQ fallback for control/lifetime traffic.

### 3C Resource-Pressure Readiness

Use `resourcePressureSessions` for session-owned send resources. Set the bit
when typed send-CQ retirement frees a command source slot, peer-client
completion source slot, or owner FIFO entry that previously blocked the session.

The candidate builder exchanges the bitmap and schedules exact blocked
sessions. Payload streams remain on indexed stream-ready structures because
they are not all session-owned.

## Slice 4: Critical-First Stage 7d Service Scheduling

Do not add another QP/CQ as the next step. The current transport already maps
traffic classes onto separate QP/CQ-owning connections, and basebackup opens on
the replication lane. Stage 7d should become service CPU/progress isolation:

1. Assert lane placement at bind/open:
   `CRITICAL_CONTROL` for client command/completion,
   `FOREGROUND_PAYLOAD` for SQL tuple payload, and `BULK_PAYLOAD` for
   basebackup.
2. Use fixed service-loop priority:

   ```text
   pending CPU completion publication
   critical client-completion recv CQ
   command forwarding
   typed send-CQ retirement under source pressure
   frontend SQL payload
   peer control
   bulk basebackup payload
   maintenance/lifetime
   ```

3. Suppress basebackup's bounded producer-frontier busy spin while critical
   command demand is nonzero.
4. Batch physical work: decode one CQ poll batch into stack-local typed events,
   process client-completion events first, batch receive-WQE reposts, then
   process payload/control events.

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
    const HomerResultDescriptorSlot *descriptor;
} HomerClientCompletionView;
```

Move full legacy conversion behind explicitly named compatibility APIs.

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

- no second reservation while one is active;
- failure before command publication clears the reservation and marks the
  command slot unused;
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
clientCompletionRecvCqPolls
clientCompletionRecvCqEmptyPolls
clientCompletionCqesPerPoll
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

Add low-overhead timestamp/cycle samples for:

```text
sender WIMM post -> receiver CQE observation
receiver CQE observation -> readyEpoch CPU store
readyEpoch CPU store -> frontend lease acquisition
```

These separate network/RNIC latency, service scheduling latency, and frontend
polling latency.

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
- Backend-completion bitmap work maps the control region into socketless
  backends. That is straightforward but not tiny: startup data, protocol
  validation, cleanup, and fallback broad-scan metrics must all move together.
- Direct command reservation abort should start as a tight invariant for the
  pgbench-Homer direct path, not as a complex general rollback mechanism.

## Final Order

```text
1. Completion protocol v27:
   one full-slot WIMM, descriptor CPU gate, remove client publishedEpoch.

2. Canonical recv-CQ dispatcher:
   permanent typed ownership, no optional handler/FIFO path.

3. Complete Stage 6:
   backend completion bitmap, resource-pressure bitmap,
   critical recv-CQ demand facts.

4. Critical-first Stage 7d service scheduling:
   no additional QP, no dedicated thread, suppress bulk spin under foreground load.

5. Remove client legacy completion reconstruction and full pre-arm copies.

6. Close partial-post, mismatch-escalation, ERROR+EOS, and stale-grant caveats.

7. Complete lower-priority basebackup fragment/reservation cleanup.
```

