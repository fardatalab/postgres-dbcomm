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

## Implementation Progress

As of June 22, 2026, Slice 1 has landed in `/data/dbcomm/citus-dbcomm` and is
recorded in
[`peer_client_completion_v27_checkpoint.md`](../../../implementations/citus/transport/peer_client_completion_v27_checkpoint.md).
The landed checkpoint includes:

- frontend completion mailbox protocol version `27`;
- control/client-completion/peer-completion shared-memory names bumped to `v27`
  for stale-region isolation;
- removal of frontend-client aggregate `publishedEpoch` and
  `peerCompletionDoorbellScratch`;
- one full-slot `RDMA_WRITE_WITH_IMM` for peer-client completion publication on
  descriptor-cache hits;
- descriptor-body WRITE plus full-slot completion WIMM on descriptor-cache
  misses;
- receiver-service validation and CPU publication of descriptor `readyVersion`
  and completion `readyEpochSlots`;
- descriptor trailing `sealVersion`;
- receiver acquire fence after WIMM CQE observation;
- send-CQ owner-table cleanup if the final WIMM post is rejected before a CQE
  can ever arrive.

Validation for this checkpoint used no-stats binaries and the farnet0-to-farnet1
remote RDMA pgbench path. The current accepted evidence is remote c1 and c4
correctness, plus warmed c4 performance in the `9.4k-9.8k TPS` band with zero
failed transactions. The deterministic injection matrix in Slice 1.8 was not
implemented in this commit and remains a follow-up hardening task.

Slice 2A has also landed as the first implementable recv-CQ dispatcher
checkpoint and is recorded in
[`peer_recv_dispatcher_v14_checkpoint.md`](../../../implementations/citus/transport/peer_recv_dispatcher_v14_checkpoint.md).
That checkpoint freezes the peer immediate ABI at protocol v14, installs a
permanent transport-owned dispatcher, removes the per-grant optional completion
handler path, and routes all steady-state WIMM CQEs through one decoder/switch.

Important scope correction: the validated 2A checkpoint does **not** yet include
direct binding tables, split recv-CQ/CM APIs, explicit recv-CQ owner phases, or
typed ready-state replacement for command/control/payload. Those remain the
next Slice 2 sub-stages. The callback-heavy first 2A attempt was correct but
regressed remote c4 to roughly `7.7k-8.8k TPS`; the landed form keeps
command/payload/control materialization transport-local and only leaves
peer-client completion publication as a service callback, recovering the remote
c4 band to roughly `9.2k-9.7k TPS`.

Do not add new optional handlers or expand the generic FIFO/stash path while
implementing later slices.

Slice 2B is also complete for peer-client completion FIFO cleanup. It removed
the residual `pendingClientCompletionDoorbell*` fields, the completion FIFO
consume helper, and the unused optional handler typedefs. A zero-reference check
for those symbols returned empty, and remote c1/c4 validation stayed in the
Slice 1/2A band. This narrows the next Slice 2 work to command, control, and
payload typed readiness plus the larger split collector/owner-phase changes.

The command-doorbell token FIFO cleanup is also complete. The dispatcher now
validates command WIMM tokens without materializing them into a transport token
array. This is safe because the previous command token array had no live
service-side pop consumer; scheduler readiness already comes from the durable
per-session command mailbox fact. Direct command ready bits remain future work.

Post-2C control-path review tightened the Slice 2D target. Before Slice 2D, the
control mailbox path had two publication signals: a decoded control WIMM increments
`pendingControlDoorbellCount`, while the mailbox consumer may also consume by
observing remote `publishedTail > consumedHead`. That visible-tail fallback is a
liveness workaround for delayed recv-CQ polling, not an accepted publication
protocol. It relies on the same class of separate-WQE visibility assumption that
v27 removed from peer-client completion publication. Slice 2D must therefore
move control to a one full-message `WRITE_WITH_IMM` publication and a
CQE-derived arrival frontier; tail observation may be retained only as temporary
diagnostic evidence that exact recv-CQ demand is insufficient, and must not
consume records or set semantic readiness.

Slice 2D0a is now landed as owner-phase scaffolding. It adds explicit
`UNUSED`/`BOOTSTRAP`/`CANONICAL`/`TEARDOWN` recv-CQ owner state, records
transitions around resource initialization, setup completion, and reset, and
guards the existing recv-CQ drain against polling with no owner or during
teardown. This checkpoint intentionally preserves current polling behavior:
`TupleSinkServiceApplyPeerBootstrapMessage()` still sets `bootstrapComplete`,
`TupleSinkServicePrepareConnectionForWrite()` still performs hidden recv-CQ/CM
progress, and control publication is still the old message WRITE plus tail
WIMM shape. Remote RDMA validation stayed correct and performance-neutral:
c1 `100000/100000`, zero failures, `3958 TPS`; c4 warmup `9693 TPS`, measured
`9586 TPS` and `9348 TPS`, all zero failures.

Slice 2D0b is also landed. `TupleSinkServiceApplyPeerBootstrapMessage()` now
only validates and stores the peer mailbox descriptor; it no longer sets
`bootstrapComplete`. `TupleSinkServiceFinishPeerConnectionSetup()` is now the
only transition into `bootstrapComplete`/canonical recv-CQ ownership. Remote
RDMA validation stayed correct and performance-neutral: c1 `100000/100000`,
zero failures, `3948 TPS`; c4 warmup `9760 TPS`, measured `9565 TPS` and
`9401 TPS`, all zero failures.

Slice 2D0c is landed as the first hidden recv-CQ ownership cleanup.
`TupleSinkServiceDrainPeerConnectionRecvCq()` and
`TupleSinkServiceDrainPeerConnectionCmEvents()` split physical recv-CQ polling
from RDMA-CM event polling. `TupleSinkServicePrepareConnectionForWrite()`,
`TupleSinkServiceTryPublishPeerControlAsyncOp()`, and
`TupleSinkServicePollPeerRequestRdma()` now use the CM-only helper instead of
silently draining recv CQEs. The scheduled peer pump remains the recv-CQ owner.
Remote RDMA validation stayed correct: c1 `100000/100000`, zero failures,
`3910 TPS`; c4 warmup `9720 TPS`, measured `9669 TPS` and `9375 TPS`, all zero
failures.

Slice 2D1 is now landed. The peer control mailbox protocol version is bumped to
`2`; decoded control CQEs now advance `controlDoorbellArrivedTail` and set
`controlMailboxReady`; `TupleSinkServicePublishControlMessage()` posts one
full-message `RDMA_WRITE_WITH_IMM` to the remote mailbox slot; and the receiver
no longer treats remote `publishedTail` as a publication gate. The visible-tail
fallback and stale-late-doorbell handling are gone from the semantic path.
Remote RDMA validation stayed correct: c1 `100000/100000`, zero failures,
`3929 TPS`; c4 warmup `9654 TPS`, measured `9254 TPS` and `9345 TPS`, all zero
failures. This is correctness-equivalent and performance-neutral relative to the
2D0 band; the expected c4 throughput recovery still depends on later hot-path
cleanup rather than on this ownership slice alone.

## Current Code Pointers

- [`CitusRemoteExecClientCompletionSeal`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:560)
  is the trailing content proof currently appended to a client completion slot.
- [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:879)
  now contains `consumedEpoch`, slot-local `readyEpochSlots`, completion slots,
  and the result descriptor table; frontend-client aggregate `publishedEpoch`
  and `peerCompletionDoorbellScratch` are gone in v27.
- [`HomerResultDescriptorSlot`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:604)
  now has both a leading `bodyVersion` and trailing `sealVersion`.
- [`CitusRemoteExecPublishResultDescriptor()`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:774)
  and
  [`CitusRemoteExecReadResultDescriptorStable()`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:798)
  implement current descriptor publication/read validation.
- [`HomerClientOpenCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:588)
  initializes the remote client completion cursor from `consumedEpoch`.
- [`HomerClientCopyClientCompletionSlotStable()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1108)
  is the frontend ready-copy-ready and trailing-seal validator.
- [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14200)
  is the backend-node peer-client completion publisher.
- [`TupleSinkServicePostPeerRegisteredClientCompletionBytesWithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5936)
  is the registered-source `RDMA_WRITE_WITH_IMM` posting surface.
- [`TupleSinkServiceHandlePeerClientCompletionDoorbell()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20139)
  is the receiver-side doorbell handler that validates completion content and
  CPU-publishes frontend visibility.
- [`HomerPeerRecvDispatcher`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:215)
  is the current permanent recv dispatcher for steady-state WIMM CQEs.
- [`TupleSinkServiceDecodePeerDoorbellImmediate()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:567)
  implements the v14 immediate-data kind/token decoder.
- [`TupleSinkServiceDrainPeerConnectionEvents()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3955)
  no longer accepts optional semantic handlers. It still combines recv-CQ and
  CM draining, and residual `pendingClientCompletionDoorbell*` fields remain as
  dead/cleanup state until the Slice 2B typed-completion cleanup deletes them.
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

Freeze the immediate-data ABI before changing the collector. The current
implementation uses the high two bits as kind and lower 30 bits as value, but
raw zero is special-cased for control and overlaps the payload namespace. Replace
that with a complete four-kind namespace:

```c
#define HOMER_PEER_DOORBELL_KIND_SHIFT 30U
#define HOMER_PEER_DOORBELL_KIND_MASK  UINT32_C(0xC0000000)
#define HOMER_PEER_DOORBELL_TOKEN_MASK UINT32_C(0x3FFFFFFF)

typedef enum HomerPeerDoorbellKind
{
    HOMER_PEER_DOORBELL_PAYLOAD = 0,
    HOMER_PEER_DOORBELL_CLIENT_COMPLETION = 1,
    HOMER_PEER_DOORBELL_CLIENT_COMMAND = 2,
    HOMER_PEER_DOORBELL_CONTROL = 3
} HomerPeerDoorbellKind;
```

Immediate values:

```text
00xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx = payload
01xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx = client completion
10xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx = client command
11xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx = control
```

Control uses token zero:

```text
kind = CONTROL
token = 0
```

Every other kind requires a nonzero token. This preserves the current payload,
completion, and command high-bit encodings while replacing the raw-zero control
special case with an explicit kind. Bump
`CITUS_REMOTE_EXEC_PEER_PROTOCOL_VERSION` from `13` to `14` for this wire
change.

Use the lower 30 bits as a direct token:

```text
bits 29:12 = binding generation, 18 bits
bits 11:0  = binding index + 1, 12 bits
```

```c
#define HOMER_PEER_DOORBELL_BINDING_INDEX_BITS 12U
#define HOMER_PEER_DOORBELL_BINDING_INDEX_MASK UINT32_C(0x00000FFF)
#define HOMER_PEER_DOORBELL_BINDING_GENERATION_SHIFT 12U
#define HOMER_PEER_DOORBELL_BINDING_GENERATION_MASK UINT32_C(0x0003FFFF)
```

Encoding:

```c
token =
    (bindingGeneration << HOMER_PEER_DOORBELL_BINDING_GENERATION_SHIFT) |
    (bindingIndex + 1U);
```

Decoding:

```c
encodedIndex = token & HOMER_PEER_DOORBELL_BINDING_INDEX_MASK;
bindingIndex = encodedIndex - 1U;
bindingGeneration =
    token >> HOMER_PEER_DOORBELL_BINDING_GENERATION_SHIFT;
```

Rules:

- index encoding zero is invalid;
- generation zero is invalid;
- command-session, completion-session, and payload-stream tables must
  statically fit within 4095 entries;
- a binding-generation counter must not wrap within one connection generation;
  reset the peer connection before an 18-bit binding generation would wrap to
  zero.

Use separate kind-specific direct tables:

```c
HomerPeerDoorbellBinding commandBindings[COMMAND_BINDING_CAPACITY];
HomerPeerDoorbellBinding completionBindings[COMPLETION_BINDING_CAPACITY];
HomerPeerDoorbellBinding payloadBindings[PAYLOAD_BINDING_CAPACITY];
```

Each entry stores:

```c
typedef struct HomerPeerDoorbellBinding
{
    bool active;
    uint32_t tokenGeneration;

    uint32_t objectIndex;
    uint64_t objectGeneration;
    uint64_t connectionGeneration;
} HomerPeerDoorbellBinding;
```

The hot lookup is:

```text
kind -> kind-specific table -> direct binding index -> generation validation
     -> exact session or stream
```

No service-session or stream-table scan is allowed on the CQE hot path.

The immediate-data ABI must be documented in code comments next to the encode
and decode helpers during implementation. After implementation, mirror the final
ABI into a KB implementation note, not only this future-direction plan.

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

    void *context;
} HomerPeerRecvDispatcher;
```

This is intentionally not a four-callback semantic hook table. The physical
recv-CQ collector owns kind decoding. Command, payload, and control readiness
are transport-owned today, so Slice 2A materializes them directly in the
transport while they still use legacy pending state. Peer-client completion
publication is the only service callback in the checkpoint because it must
validate v27 completion content and CPU-publish frontend-ready state through the
service session table. When later slices add direct binding tables and typed
ready state, keep that ownership split: do not reintroduce optional handlers for
every kind.

Use the transport-owned dispatcher form:

```c
struct TupleSinkServicePeerTransportState
{
    ...
    HomerPeerRecvDispatcher recvDispatcher;
    bool recvDispatcherInstalled;
};
```

Every connection already points back to its transport state, so the collector
accesses:

```c
&connectionState->transportState->recvDispatcher
```

Change creation to:

```c
TupleSinkServicePeerTransportState *
TupleSinkServiceCreatePeerTransportState(
    const char *listenHost,
    uint32_t listenPort,
    const HomerPeerRecvDispatcher *dispatcher,
    char *error,
    size_t errorBytes);
```

The constructor copies the dispatcher by value and currently requires the
service-owned client-completion callback to be non-null. This guarantees that the
dispatcher exists before any outgoing connection is started or incoming
connection is accepted. An active steady-state connection with
`recvDispatcherInstalled == false` is a fatal internal error.

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

Define recv-CQ ownership explicitly:

```c
typedef enum HomerRecvCqOwnerPhase
{
    HOMER_RECV_CQ_OWNER_BOOTSTRAP = 0,
    HOMER_RECV_CQ_OWNER_CANONICAL,
    HOMER_RECV_CQ_OWNER_TEARDOWN
} HomerRecvCqOwnerPhase;
```

Every new connection starts as:

```text
recvCqOwnerPhase = BOOTSTRAP
bootstrapComplete = false
```

`TupleSinkServiceApplyPeerBootstrapMessage()` validates and stores the peer
descriptor only. It must not set `bootstrapComplete`.

`TupleSinkServiceFinishPeerConnectionSetup()` is the only function that
transitions a connection into steady-state recv-CQ ownership:

```text
1. Assert RDMA CM connection is established.
2. Assert bootstrap send and receive have completed.
3. Assert peer bootstrap descriptor has been validated and applied.
4. Assert the permanent recv dispatcher is installed.
5. Initialize connection-local doorbell binding tables.
6. Post every steady-state notification receive WQE.
7. Set setupPhase = READY.
8. Set recvCqOwnerPhase = CANONICAL.
9. Set bootstrapComplete = true as the final transition.
10. Register the connection in steady-state collector facts.
```

A WIMM may arrive after receive WQEs are posted but before step 9. It may sit in
the hardware CQ, but no setup poller may consume it. The canonical collector
will consume it after the final transition.

All recv-CQ polling goes through one internal wrapper:

```c
int TupleSinkServicePollRecvCq(
    TupleSinkServicePeerConnectionState *connection,
    HomerRecvCqOwnerPhase expectedOwner,
    int maxCqes,
    struct ibv_wc *cqes,
    char *error,
    size_t errorBytes);
```

Assertions:

```text
setup poller:
    expectedOwner == BOOTSTRAP
    bootstrapComplete == false

canonical collector:
    expectedOwner == CANONICAL
    bootstrapComplete == true
    setupPhase == READY

teardown poller:
    expectedOwner == TEARDOWN
```

Before QP teardown:

```text
recvCqOwnerPhase = TEARDOWN
```

Keep `bootstrapComplete` during the migration, but assert:

```c
bootstrapComplete ==
    (setupPhase == READY &&
     recvCqOwnerPhase == HOMER_RECV_CQ_OWNER_CANONICAL);
```

Eventually `bootstrapComplete` can be removed and derived from setup and owner
phases.

Inside the collector, dispatch by immediate kind:

```c
switch (doorbellKind)
{
    case CONTROL:
        materialize transport-owned control readiness;
        break;
    case CLIENT_COMMAND:
        materialize transport-owned command readiness;
        break;
    case CLIENT_COMPLETION:
        dispatcher->onClientCompletion(...);
        break;
    case PAYLOAD:
        materialize transport-owned payload readiness;
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
| Payload | Payload ring and published frontier/in-band headers | Intrusive per-traffic-class ready-list membership | No |
| Control mailbox | Control mailbox sequence/slots | Connection mailbox-ready bit | No |
| Send completion | Source-owner FIFO/table | Immediate owner retirement and resource-pressure ready bit | No |
| Disconnect/lifetime | Connection state | Connection lifetime-ready state | No |

Exact ready-state storage:

| Event | Ready state |
| --- | --- |
| Client completion | Per-session pending count plus pending-publication bitmap |
| Client command | Service-local 64-bit session bitmap |
| Payload | Intrusive per-traffic-class ready list |
| Control mailbox | Per-connection boolean plus ready connection bitmap |
| Connection lifetime | Per-connection lifetime boolean plus ready connection bitmap |
| Send-resource relief | Per-session blocked-reason mask plus resource-pressure bitmap |

For fixed incoming/outgoing connection tables of at most 64 entries each, use:

```c
uint64_t incomingControlReadyConnections;
uint64_t outgoingControlReadyConnections;
uint64_t incomingLifetimeReadyConnections;
uint64_t outgoingLifetimeReadyConnections;
```

This avoids scanning all connections after the collector materializes a control
or lifetime fact.

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
materialized state: remoteCommandDoorbellReadySessions bitmap
dispatcher action: set bit for exact session
executor: drain commands within budget, recheck mailbox, re-set bit when unread commands remain
```

Multiple command doorbells safely coalesce because the command mailbox retains
all unread commands.

Payload readiness is level-triggered and uses an intrusive ready queue, not an
open choice between queue and bitmap. Each payload stream entry gets:

```c
bool recvReadyEnqueued;
uint32_t recvReadyPrevious;
uint32_t recvReadyNext;
uint64_t recvReadyGeneration;
```

The service owns one doubly linked ready list per traffic class:

```c
uint32_t payloadRecvReadyHead[HOMER_TRANSPORT_TRAFFIC_CLASS_COUNT];
uint32_t payloadRecvReadyTail[HOMER_TRANSPORT_TRAFFIC_CLASS_COUNT];
```

Use an invalid index sentinel for empty links.

```text
payload dispatcher:
    decode the direct payload binding
    validate stream index, stream generation, and connection generation
    append the stream only when recvReadyEnqueued == false

payload executor:
    remove one ready stream
    validate stored generation
    drain within byte/record budget
    recheck ring
    append stream to the tail again when unread payload remains
```

On stream close or reclamation, remove an enqueued stream in O(1) using the
doubly linked pointers.

Control mailbox readiness is level-triggered and CQE-authorized:

```c
uint64_t controlDoorbellArrivedTail;
bool controlMailboxReady;
```

The accepted control publisher uses one full-message `WRITE_WITH_IMM` for the
control slot itself, not a message WRITE followed by a separate tail WIMM. The
control dispatcher increments `controlDoorbellArrivedTail`, checks that the
arrival frontier has not overrun the fixed mailbox slot window, materializes
`controlMailboxReady = true`, and sets the ready connection bitmap. The control
executor drains only while `consumedHead < controlDoorbellArrivedTail`; it
copies the corresponding mailbox slot, validates `ringSequence`, dispatches the
request or response, and release-stores `consumedHead`. It leaves
`controlMailboxReady` true when the arrival frontier still exceeds
`consumedHead`, and clears it when the CQE-authorized frontier is drained.
Multiple control WIMMs safely coalesce because `controlDoorbellArrivedTail`
records how far the executor is allowed to consume.

Do not treat a visible remote `publishedTail` as an alternate publication gate.
During migration, a temporary stats-only check may count
`remotePublishedTail > controlDoorbellArrivedTail` and demand an exact recv-CQ
poll, but it must not set `controlMailboxReady`, consume a mailbox record,
advance `consumedHead`, or classify the later CQE as harmless stale work. A
persistent tail-ahead condition after exact CQ drain is a collector ownership
bug or transport failure, not normal fallback behavior.

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

Do not introduce the two-phase service pass in the same patch that first adds
the canonical dispatcher. Use this migration order:

```text
2A:
    canonical dispatcher under current scheduler structure

2B-2E:
    typed ready state by event family

2F:
    remove old token storage

2G:
    split each service pass into collector phase and semantic phase
```

This keeps regressions attributable. The final collector phase contains only
physical resources:

```text
connection identity
recv/send/CM CQ identity
poll-batch budget
CQE budget
```

It never contains an event-kind filter. The final semantic phase consumes only
typed ready state produced by collectors and other producers.

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

Do not keep all old token arrays until the end, and do not delete them all in
one large patch. Stage deletion by event family.

Slice 2A, canonical collector skeleton:

```text
implement permanent transport-owned dispatcher
implement canonical physical recv-CQ collector
split recv-CQ and CM drain APIs
add explicit bootstrap/canonical/teardown ownership
add direct immediate kind decoder
add direct token-binding validation
remove optional handler parameters immediately
```

For event families not migrated yet, the permanent dispatcher may temporarily
write into their existing pending structures. This preserves behavior while
ensuring that only the canonical collector polls the CQ. End state:

```text
one recv-CQ poll owner
one permanent dispatcher
old semantic storage still temporarily present
```

Slice 2B, client-completion migration:

```text
validated as a cleanup-only stage after the v14 permanent dispatcher:
    peer-client completion WIMM CQEs are handled immediately
    matching CQEs CPU-publish readyEpochSlots
    stale CQEs are discarded by the session-generation check
    no intermediate completion-token FIFO producer remains
```

Delete immediately after targeted validation:

```text
pendingClientCompletionDoorbellTokens
pendingClientCompletionDoorbellHead
pendingClientCompletionDoorbellCount
TupleSinkServicePopPeerClientCompletionDoorbell
TupleSinkServiceDrainQueuedPeerClientCompletionDoorbells
TupleSinkServiceConsumePeerClientCompletionDoorbellRdma
unused optional doorbell-handler typedefs
```

This is the first family removed because the v27 completion protocol already
supplies its replacement.

Slice 2C, client-command migration:

```text
validated cleanup now:
    dispatcher decodes and validates command WIMM token
    no transport token FIFO remains
    readiness still comes from command mailbox published/accepted epoch facts

future optimization still needed:
    decode direct command binding
    validate session index, session generation, and connection generation
    set remoteCommandDoorbellReadySessions bit
    executor drains exact command mailbox within budget
    executor rechecks command mailbox and re-sets bit when unread records remain
```

Use a separate service-local bitmap from the shared frontend command-ready
bitmap:

```c
uint64_t remoteCommandDoorbellReadySessions;
```

After command-doorbell tests pass, delete:

```text
pendingCommandDoorbellSessionIds
pendingCommandDoorbellCount
command token pop/consume helpers
command-specific recv-CQ poll helper
```

Slice 2D0, recv-CQ ownership prerequisite for control:

```text
finish before replacing control mailbox readiness:
    add BOOTSTRAP, CANONICAL, and TEARDOWN recv-CQ owner phases
    make FinishPeerConnectionSetup() the only transition into CANONICAL
    make ApplyPeerBootstrapMessage() only validate/store the peer descriptor
    split recv-CQ draining from CM-event draining
    remove steady-state recv-CQ polling from write preparation and op polling
    have those helpers set exact recv-CQ demand facts and return pending
    add noncanonicalRecvCqPollAttempts
    require zero noncanonical recv-CQ polls in normal runs
```

This prerequisite is now partially satisfied by Slice 2D0a through 2D0c:
bootstrap ownership is centralized in `TupleSinkServiceFinishPeerConnectionSetup()`,
and write preparation/op polling no longer drain the physical recv CQ. Remaining
peer-op ownership cleanup is separate: the op helpers still drain send CQ and
response mailbox until the later peer-control helper cleanup slice.

Slice 2D1, control-mailbox one-WIMM publication:

```text
protocol bump for peer control mailbox if layout or publish semantics change
sender:
    choose slot using outgoingControlPublishedTail and peerControlConsumedHeadMirror
    fill full TupleSinkServicePeerControlMessage including ringSequence
    post one full-message RDMA_WRITE_WITH_IMM to the remote slot
    do not RDMA-write remote publishedTail as the publication gate
receiver:
    decoded CONTROL CQE increments connection->controlDoorbellArrivedTail
    overrun if arrivedTail - consumedHead > slotCount
    set connection->controlMailboxReady = true
    set incoming/outgoing control-ready bitmap bit
executor:
    drain only while consumedHead < controlDoorbellArrivedTail
    acquire before copying the slot
    require slot.ringSequence == consumedHead + 1
    dispatch request or response
    release-store local consumedHead
    keep ready bit set while unread CQE-authorized messages remain
    clear ready bit after the arrival frontier is drained
```

Do not retain visible-tail consumption as fallback. A temporary stats-only check
may count `remotePublishedTail > controlDoorbellArrivedTail` and mark exact
recv-CQ demand, but it must not consume a record or advance `consumedHead`.
After exact CQ drain, a persistent tail-ahead condition is a transport or
collector-ownership failure.

Implementation status: landed with protocol version `2`. The current code keeps
the `publishedTail` field physically present in `TupleSinkServicePeerControlMailbox`
to avoid mailbox layout churn during this slice, but receiver-side control
readiness is now solely `controlDoorbellArrivedTail`/`controlMailboxReady`.

`CONTROL` immediate token zero remains acceptable because control is
connection-scoped and the physical CQ identifies the connection. That depends on
the recv-CQ owner-phase invariant:

```text
one connection generation owns one QP and its CQs
canonical polling stops before teardown
QP/CQs are drained or destroyed during TEARDOWN
no software control-ready state survives teardown
the connection table slot is reused only after teardown completes
```

If this invariant cannot be asserted cleanly during implementation, change
control to use a generation-bearing token instead of token zero.

Delete after control tests pass:

```text
pendingControlDoorbellCount - deleted in Slice 2D1
control-doorbell count-specific paths - replaced by controlDoorbellArrivedTail in Slice 2D1
tail-based control mailbox consumption - deleted in Slice 2D1
stale-late-control-doorbell handling - deleted in Slice 2D1
remote publishedTail as a receiver-side publication gate - removed semantically in Slice 2D1
special immediateData == 0 decoder branch - already absent after v14 explicit CONTROL kind
```

Control acceptance gates:

```text
one full-message control WIMM is the only publication gate
remote publishedTail is not required for receiver readiness
controlDoorbellArrivedTail never exceeds consumedHead + slotCount
multiple control CQEs coalesce through arrivedTail and one ready bit
late/stale CQE after teardown is rejected by owner phase or destroyed CQ
noncanonicalRecvCqPollAttempts remains zero in steady-state c1/c4 runs
temporary tail-ahead diagnostic counter is zero after exact CQ demand is active
```

Validated status for Slice 2D1:

```text
one full-message control WIMM is the only publication gate - yes
remote publishedTail is not required for receiver readiness - yes
controlDoorbellArrivedTail overrun is fail-fast - yes
multiple CQEs coalesce through arrivedTail and one ready bit - implemented
late/stale CQE after teardown - still relies on owner phase/destroyed CQ invariant
noncanonicalRecvCqPollAttempts - stats-gated counter exists; no-stats validation used for performance
tail-ahead diagnostic counter - not added because visible-tail fallback was removed rather than retained diagnostically
```

Slice 2E, payload migration:

```text
use intrusive per-traffic-class payload ready lists
append stream only when recvReadyEnqueued == false
remove/requeue streams through O(1) doubly linked pointers
```

After payload tests pass, delete:

```text
pendingDataDoorbellSinkIds
pendingDataDoorbellCount
payload token scans
payload pending/consume helpers
```

Decision after Slice 2D1:

```text
Use a dedicated generation-bearing payload doorbell token, an O(1)
service-stream lookup, and a service-owned intrusive ready list.

Do not retain serviceSinkId as the immediate token, and do not build a direct
map keyed by serviceSinkId.

serviceSinkId is a semantic identifier, not a safe transport binding:
    it has no generation encoded in the immediate
    it is wider than the 30-bit token namespace
    it does not directly identify a fixed stream-table slot
    reusing or resolving it still requires separate lookup and lifetime machinery

The payload token is an opaque receiver-issued capability that identifies one
local payload stream table slot and one allocation generation.
```

Pre-implementation finding after Slice 2D1:

```text
2E is no longer blocked, but it must be implemented through the staged token and
ready-list plan below rather than as a mechanical cleanup.

Current code:
    TupleSinkServiceQueuePeerPayloadDoorbellRdma() is the recv-CQ materializer.
    It validates the payload token as a local service sink id and stores that id
    in connection->pendingDataDoorbellSinkIds[].
    TupleSinkServicePeerDataDoorbellPendingRdma() and
    TupleSinkServiceConsumePeerDataDoorbellRdma() linearly scan/remove that
    transport-owned array.
    Service-side readiness then combines that transport fact with the
    stream-local receivePayloadDoorbellPending bit.

Implication:
    The planned intrusive ready list is service-owned, because the list nodes
    must live in HomerServicePayloadStreamEntry/HomerPayloadStreamState and must
    be ordered by traffic class. The physical recv-CQ owner currently has no
    service-stream pointer, binding table, or generation-bearing payload token
    that would let it append a stream directly.
```

Do not replace `pendingDataDoorbellSinkIds[]` with another transport-local FIFO
or scan. The payload-doorbell ownership contract is now:

```text
Accepted shape:
    make payload doorbells one fixed permanent dispatcher callback, like
    peer-client completion, not an optional per-grant handler
    encode a payload binding token with stream index plus generation
    validate connection generation, stream active state, stream generation,
    token generation, peer binding state, and traffic class at CQE time
    append HomerServicePayloadStreamEntry to an intrusive ready list exactly
    once while recvReadyEnqueued is false
    have the payload executor unlink/requeue in O(1) after draining work

Rejected shortcuts:
    keep serviceSinkId as the immediate token
    build a direct map keyed by serviceSinkId
    keep the current transport-owned sink-id array and merely add a service-side
    scan over it. That preserves the old ownership leak and does not satisfy the
    Slice 2E deletion gate.
```

2E.0 - Define payload token ABI v15:

```text
Keep the v14 top-level immediate layout:
    bits 31:30 = kind
    bits 29:0  = kind-local token

Payload kind-local token:
    bits 29:12 = token generation, 18 bits
    bits 11:0  = payload stream index + 1, 12 bits

#define HOMER_PAYLOAD_TOKEN_INDEX_BITS       12U
#define HOMER_PAYLOAD_TOKEN_INDEX_MASK       UINT32_C(0x00000fff)
#define HOMER_PAYLOAD_TOKEN_GENERATION_SHIFT 12U
#define HOMER_PAYLOAD_TOKEN_GENERATION_MASK  UINT32_C(0x0003ffff)

token = (tokenGeneration << 12) | (streamIndex + 1U)

Rules:
    encoded index zero is invalid
    generation zero is invalid
    static assert that CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS <= 4095
    do not silently wrap the 18-bit generation
    force peer connection reset before token generation would wrap
    bump CITUS_REMOTE_EXEC_PEER_PROTOCOL_VERSION from 14 to 15

Helpers:
    HomerEncodePayloadDoorbellToken(streamIndex, tokenGeneration, tokenOut)
    HomerDecodePayloadDoorbellToken(token, streamIndexOut, tokenGenerationOut)
```

2E.1 - Exchange receiver-issued tokens during stream open:

```text
Keep service sink ids for semantic identity and diagnostics.

Add peer stream-open fields:
    uint32_t localPayloadDoorbellToken;  // request
    uint32_t peerPayloadDoorbellToken;   // response

Receiver allocates its token only after:
    stream table entry is active
    stream generation is fixed
    peer connection and traffic class are bound

Peer stores the returned token in its outgoing stream state and uses it for every
payload WIMM targeting that local stream.

Direction semantics:
    local receive stream: WIMM means new payload/frontier is available
    local send stream:    WIMM means remote consumed-head/credit state changed

No additional immediate kind is needed.
```

2E.2 - Keep token generations outside resettable stream entries:

```text
Do not store the only generation counter inside HomerServicePayloadStreamEntry,
because stream reclamation can memset that entry.

Add service-lifetime storage:
    uint32_t PayloadDoorbellTokenGenerations[CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS];

On stream allocation:
    generation = ++PayloadDoorbellTokenGenerations[streamIndex]
    generation &= HOMER_PAYLOAD_TOKEN_GENERATION_MASK
    if generation == 0, require peer connection reset before reuse
    store resulting token and generation in the live stream entry
```

2E.3 - Add one permanent payload dispatcher callback:

```text
Extend HomerPeerRecvDispatcher with exactly one additional service callback:

bool (*onPayloadReady)(
    TupleSinkServicePeerConnectionHandle *connection,
    uint64_t connectionGeneration,
    uint32_t payloadToken,
    void *context,
    char *error,
    size_t errorBytes);

Do not restore generic callbacks for command and control. The rejected
all-callback version dropped c4 to roughly 7.7k-8.8k TPS; this narrow callback
does one direct O(1) lookup and leaves command/control transport-local.

Callback allowed work:
    decode stream index and token generation
    validate index
    load stream entry directly
    validate active state, token generation, stream generation, connection
    handle, connection generation, peer binding, and traffic class
    enqueue the stream if it is not already ready

Callback forbidden work:
    scan the stream table
    inspect or drain payload records
    run the payload executor
    allocate memory
    format logs on success

A stale token is a transport lifetime violation. Mark the stream/connection
failed rather than silently dropping it.
```

2E.4 - Add service-owned ready lists:

```c
typedef struct HomerPayloadReadyList
{
    uint32_t head;
    uint32_t tail;
    uint32_t count;
} HomerPayloadReadyList;

HomerPayloadReadyList PayloadReadyLists[HOMER_TRANSPORT_TRAFFIC_CLASS_COUNT];
```

Use `UINT32_MAX` as the invalid index. Add to each payload stream entry:

```c
bool recvReadyEnqueued;
uint32_t recvReadyPrevious;
uint32_t recvReadyNext;
uint64_t recvReadyStreamGeneration;
uint32_t payloadDoorbellTokenGeneration;
uint32_t localPayloadDoorbellToken;
uint32_t peerPayloadDoorbellToken;
```

List helpers:

```text
HomerServiceEnqueuePayloadReady(service, streamIndex)
HomerServiceRemovePayloadReady(service, streamIndex)
HomerServiceClaimPayloadReady(service, streamIndex, streamGeneration)
```

Invariants:

```text
a stream appears in at most one list
its list corresponds to its bound traffic class
repeated WIMMs coalesce while recvReadyEnqueued is true
stream reclamation removes it from the list before the entry is cleared
list entries always carry/validate the current stream generation
```

2E.5 - Integrate the list with the scheduler safely:

```text
Do not unlink streams while merely constructing candidates. A candidate may be
built but not selected.

Candidate construction traverses the ready list and emits:

typedef struct HomerPayloadReadyCandidate
{
    uint32_t streamIndex;
    uint64_t streamGeneration;
    HomerTransportTrafficClass trafficClass;
} HomerPayloadReadyCandidate;

At executor entry:
    validate index and generation
    claim and unlink the stream
    drain within granted record/byte/WR budget
    recheck the underlying ring/frontier
    re-enqueue at tail if unread payload remains, budget was exhausted with more
    ready work, or a temporary dependency prevented draining
    leave it off-list only after a stable empty recheck

Only receive-side payload readiness discovery moves from the broad stream scan
to this list. Producer-side outgoing payload discovery can keep existing indexed
facts until its own scheduler work is completed.
```

2E.6 - Define stream close and token invalidation:

```text
Do not release the payload binding merely because a close request was received.

Release only after existing stream state proves:
    peer can no longer post new payload WIMMs
    final payload/EOS frontier has been observed
    local receive ring is drained through that frontier
    stream is not on a payload ready list
    no payload executor action references its generation
    peer close/lifetime protocol permits reclamation

Then:
    remove it from any ready list
    mark token binding inactive
    clear connection binding
    preserve the external token-generation counter for the next allocation
    reclaim the stream entry

A payload WIMM received after binding invalidation on the same connection
generation is a protocol error and should reset the affected connection.
```

2E.7 - Delete legacy payload pending state:

```text
After direct-token and ready-list validation, delete:
    pendingDataDoorbellSinkIds
    pendingDataDoorbellCount
    TupleSinkServiceQueuePeerPayloadDoorbellRdma
    TupleSinkServiceConsumePeerDataDoorbellRdma
    TupleSinkServicePeerDataDoorbellPendingRdma

Also delete receivePayloadDoorbellPending after zero-reference check proves the
intrusive ready state fully replaces it.
```

Recommended commit staging:

```text
2E-A Token protocol:
    peer protocol v15
    token encode/decode helpers
    stream token generation and lifecycle
    peer open request/response token exchange
    sender uses new payload token
    existing pending array remains temporarily as materialization target

2E-B Direct service materialization:
    add permanent onPayloadReady callback
    add direct stream lookup and validation
    add intrusive ready lists
    switch receive-side scheduler readiness to the lists
    keep legacy array only behind diagnostic comparison macro for one stats run;
    it must not drive execution

2E-C Delete legacy state:
    remove pending sink-id array and APIs
    remove legacy stream pending flag
    add zero-reference checks
    run full acceptance
```

Required tests and counters:

```text
Tests:
    encode/decode boundary stream indexes and generations
    reject zero token, zero generation, and out-of-range index
    reject stale token generation
    reject wrong connection generation
    reject wrong traffic class
    reject closed/reused stream entry
    two or more WIMMs for one stream produce one ready-list node
    multiple unread ring records drain after coalesced WIMMs
    multiple streams in one traffic class make round-robin progress
    concurrent foreground and bulk lists retain class priority
    stream close while enqueued removes the node safely
    ring wrap and EOS remain correct
    remote pgbench c1/c4
    local and remote basebackup
    concurrent c4 plus remote basebackup

Counters:
    payloadDoorbellsDecoded
    payloadDoorbellBindingLookups
    payloadDoorbellBindingFailures
    payloadReadyEnqueues
    payloadReadyAlreadyEnqueued
    payloadReadyClaims
    payloadReadyRequeues
    payloadReadyListHighWater
    payloadStaleTokenErrors
    payloadReadyFallbackDiscoveries

Acceptance:
    payloadReadyFallbackDiscoveries = 0
    payloadStaleTokenErrors = 0
    no legacy pending-array references
    no payload stream scan used for receive-doorbell discovery
```

Slice 2F, final ownership cleanup:

```text
remove all token-FIFO compatibility adapters
remove optional handlers from TupleSinkServicePeerPumpGrant
remove every steady-state recv-CQ polling helper other than the canonical collector
require zero source references to old pending arrays
add debug counter for every attempted noncanonical recv-CQ poll
require that counter to remain zero in c1, c4, and basebackup validation
```

Bootstrap/setup code may retain setup-specific polling until the permanent
dispatcher is installed. After `bootstrapComplete`, a debug assertion rejects
any noncanonical recv-CQ poll.

Remaining caveats from completed Slice 2 stages:

```text
Single recv-CQ ownership is not fully enforced yet. Owner phases exist, but 2F
still needs to make the canonical collector the only post-bootstrap recv-CQ poll
entry point, not merely require the connection to be in CANONICAL.

Control typed readiness is correct but not indexed. controlMailboxReady is now
the right semantic fact, but the service still scans active connections to find
ready control mailboxes. An indexed ready-connection structure remains follow-up
work.

Some older KB pointers may still describe combined recv-CQ/CM draining or
residual completion FIFO state that has already been removed. Keep updating those
as touched; they do not block 2E.
```

Use the kind-specific direct binding tables defined in Slice 2A for stale-token
protection. The dispatcher resolves:

```text
kind -> kind-specific binding table -> binding index -> token generation
     -> object index -> object generation -> bound connection generation
```

A stale token generation, closed object, wrong object generation, or wrong
connection generation is fatal. No linear session-table or stream-table scan may
occur on the CQE hot path.

Deterministic tests before deleting the family FIFOs:

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

Exact Slice 2 acceptance gates:

```text
Immediate ABI:
    every kind encodes/decodes correctly
    token zero rejected for command/completion/payload
    nonzero token rejected for control
    stale binding generation rejected
    binding-index overflow rejected
    connection reset forced before token generation wrap

Bootstrap:
    ApplyPeerBootstrapMessage never sets bootstrapComplete
    FinishPeerConnectionSetup is the only true transition
    setup poll after CANONICAL transition asserts
    canonical poll before transition asserts
    notification CQE arriving just before final transition is later consumed exactly once

Family migration:
    after 2B, zero completion FIFO references
    after 2C, zero command pending-array references
    after 2D, zero control doorbell-count references
    after 2E, zero payload pending-array references
    after 2F, zero optional recv-handler parameters

Ownership:
    exactly one steady-state poll owner per recv CQ
    zero noncanonical poll attempts
    every polled CQE increments exactly one decoded-kind counter
```

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
- The current control visible-tail fallback is not part of the accepted
  publication protocol. It exists because recv-CQ ownership is not yet clean
  enough for demanded control-response polling. Replace it with
  `controlDoorbellArrivedTail`, a level-triggered ready bit, exact recv-CQ demand,
  and one full-message control `WRITE_WITH_IMM`; do not consume control records
  solely because remote `publishedTail` is visible.
- Backend-completion bitmap work maps the control region into socketless
  backends. That is straightforward but not tiny: startup data, protocol
  validation, cleanup, and fallback broad-scan metrics must all move together.
- Direct command reservation abort should start as a tight invariant for the
  pgbench-Homer direct path, not as a complex general rollback mechanism.
- The immediate-data ABI is a wire contract. During implementation, document it
  next to the encode/decode helpers and the protocol-version bump in code
  comments. After validation, create or update a factual implementation KB note
  under `docs/kb/implementations/...` that records the landed ABI, helper names,
  version number, and validation evidence.
- Slices 6 and 7 are not implementation-ready from this note alone. Before a
  developer starts either one, expand the target slice with: files/functions
  changed, new fields/API, state transitions, failure behavior, ABI bump,
  targeted tests, acceptance, and old code removed. `ERROR+EOS` is a protocol
  change, and partial-post cleanup needs a per-path matrix rather than one
  shared bullet.

## Slice Readiness Assessment

| Slice       | Status                                                                               |
| ----------- | ------------------------------------------------------------------------------------ |
| Slice 1     | Implementation-ready; start here before Stage 6 or Stage 7 work                      |
| Slice 2A-2C | Landed through command FIFO cleanup; direct command ready bits remain future work     |
| Slice 2D    | Landed through 2D1 one-WIMM control publication; peer-op helper cleanup remains later |
| Slice 2E    | Implementation-ready as staged 2E-A/2E-B/2E-C payload token and ready-list work       |
| Slice 2F    | Follow-up ownership cleanup; enforce canonical recv-CQ poll owner and delete leftovers |
| Slice 3     | Sufficiently detailed to start after Slice 2                                         |
| Slice 4     | Sufficiently detailed to start after Slice 2/3 readiness                             |
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
