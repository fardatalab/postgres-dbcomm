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

Slice 2E-A is now landed as the payload token-protocol checkpoint. Peer protocol
version is bumped to `15`; payload WIMM immediates now use a receiver-issued
generation-bearing token instead of `serviceSinkId`; the token is exchanged in
peer open request/response; and per-stream token generations live in a
service-lifetime array outside resettable stream entries. The old
`pendingDataDoorbellSinkIds[]` array still exists only as a temporary bridge,
but it now stores payload tokens and is still scheduled/pumped through the old
broad stream scan. This intentionally defers direct O(1) service materialization
and fixed ready queues to Slice 2E-B. Validation used no-stats binaries, rebuilt
and installed as `dbcomm`, synced to `farnet0`, then restarted both Homer
services. Remote farnet0-to-farnet1 validation passed: cold c1 `2000/2000`,
zero failures; warm c1 `5000/5000`, zero failures, `3816 TPS`, p99 `0.295 ms`;
remote RDMA basebackup first post-restart correctness check completed
successfully in `5.96 s`; short c4 `12000/12000`, zero failures, `9458 TPS`,
p99 `0.664 ms`.

Slice 2E-B is now landed as the direct service materialization checkpoint.
Payload recv-CQ CQEs call the narrow permanent `onPayloadReady` dispatcher;
the service decodes the payload token, validates stream index, token
generation, peer binding, connection generation, and traffic class, then
coalesces readiness into fixed per-traffic-class array-backed queues. Scheduler
candidate construction appends exact queued payload-stream candidates without
claiming them; selected payload executors claim and recheck the stream before
draining. The dense aggregate payload scan is not appended in the same pass as
exact queued payload-doorbell candidates, so receive-doorbell discovery no
longer depends on the broad active-stream scan. The legacy pending-array fields
and helper APIs still exist but no longer drive recv-CQ payload materialization
or scheduler readiness. Validation used no-stats binaries, rebuilt and
installed as `dbcomm`, synced to `farnet0`, then restarted both Homer services:
remote cold c1 `2000/2000`, zero failures with one cold outlier; warm c1
`5000/5000`, zero failures, `3839 TPS`, p99 `0.287 ms`; short c4
`12000/12000`, zero failures, `9505 TPS`, p99 `0.636 ms`; remote RDMA
basebackup first post-restart correctness check completed successfully in
`5.97 s`.

Slice 2E-C is now landed as the legacy pending-array deletion checkpoint. The
connection-local `pendingDataDoorbell*` fields and
`TupleSinkServiceConsumePeerDataDoorbellRdma()` /
`TupleSinkServicePeerDataDoorbellPendingRdma()` APIs are deleted, and the
post-delete zero-reference check for those names returned no matches.
`receivePayloadDoorbellPending` remains intentionally as executor-local
already-claimed drain state, not as a transport FIFO shadow. Validation stayed
correct and performance-neutral: remote cold c1 `2000/2000`, zero failures with
the same one-time cold outlier shape; warm c1 `5000/5000`, zero failures,
`3878 TPS`, p99 `0.282 ms`; short c4 `12000/12000`, zero failures, `9726 TPS`,
p99 `0.651 ms`; remote RDMA basebackup first post-restart correctness check
completed successfully in `5.81 s`. A later four-repeat run without restarting
services measured `4.32 s`, `4.17 s`, `4.16 s`, and `4.24 s`, so the warmed
2E-C basebackup band is `4.16-4.24 s`.

Slice 2F is now landed as the final physical recv-CQ ownership cleanup for the
current dispatcher architecture. `TupleSinkServiceRecvCqPhaseAllowsPoll()` was
replaced by role-specific ownership validation; bootstrap setup consumes
bootstrap receive CQEs only through `TupleSinkServicePollBootstrapRecvCompletion()`;
steady-state WIMM CQEs are drained only by the static
`TupleSinkServiceDrainCanonicalRecvCq()` path reached through a
generation-checked `HomerRecvCqCollectorAction`; and the public combined
`TupleSinkServiceDrainPeerConnectionEventsRdma()` wrapper was deleted. The old
combined `drainRecvCq/drainCmEvents` implementation shape is gone: recv-CQ
polling and RDMA-CM polling are physically separate functions. Runtime
validation used no-stats binaries for performance and a short stats-enabled
pass for ownership counters. No-stats validation passed remote cold c1
`2000/2000`, zero failures; warm c1 `5000/5000`, zero failures, `3954 TPS`,
p99 `0.275 ms`; short c4 `48000/48000`, zero failures, `9761 TPS`, p99
`0.666 ms`; and remote RDMA basebackup runs `5.44 s`, `4.22 s`, `4.16 s`,
`4.15 s`, so the warmed post-2F basebackup band is `4.15-4.22 s`. The
stats-enabled ownership pass completed c1, c4, and basebackup correctness; the
connection stats showed `noncanonical_recv_cq_polls=0`,
`bootstrap_unexpected_wimm=0`, `canonical_unexpected_opcode=0`, and
`stale_recv_cq_collector_actions=0` on active connections. The installed runtime
was restored to no-stats binaries afterward.

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
- [`TupleSinkServicePostPeerRegisteredClientCompletionBytesWithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6288)
  is the registered-source `RDMA_WRITE_WITH_IMM` posting surface.
- [`TupleSinkServiceHandlePeerClientCompletionDoorbell()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20139)
  is the receiver-side doorbell handler that validates completion content and
  CPU-publishes frontend visibility.
- [`HomerPeerRecvDispatcher`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:215)
  is the current permanent recv dispatcher for steady-state WIMM CQEs.
- [`TupleSinkServiceDecodePeerDoorbellImmediate()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:606)
  implements the v14 immediate-data kind/token decoder.
- [`TupleSinkServicePollBootstrapRecvCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1595)
  is the bootstrap-only recv-CQ poller for the setup descriptor exchange.
- [`TupleSinkServiceDrainCanonicalRecvCq()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4304)
  is the static canonical steady-state recv-CQ drain that decodes WIMM CQEs and
  never polls RDMA-CM events.
- [`TupleSinkServiceDrainRecvCqCollectorAction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4506)
  revalidates connection index/generation before calling the canonical recv-CQ
  drain.
- [`TupleSinkServiceDrainPeerConnectionCmEvents()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4565)
  is the CM-only peer connection event drain.
- [`ibv_query_qp_data_in_order()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2609)
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
| Payload | Payload ring and published frontier/in-band headers | Fixed per-traffic-class ready-queue membership | No |
| Control mailbox | Control mailbox sequence/slots | Connection mailbox-ready bit | No |
| Send completion | Source-owner FIFO/table | Immediate owner retirement and resource-pressure ready bit | No |
| Disconnect/lifetime | Connection state | Connection lifetime-ready state | No |

Exact ready-state storage:

| Event | Ready state |
| --- | --- |
| Client completion | Per-session pending count plus pending-publication bitmap |
| Client command | Service-local 64-bit session bitmap |
| Payload | Fixed per-traffic-class array-backed ready queue |
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

Payload readiness is level-triggered and uses a fixed array-backed ready queue,
not an open choice between queue and bitmap. Each payload stream entry gets only
membership/generation state, not linked-list pointers:

```c
bool recvReadyEnqueued;
uint64_t recvReadyGeneration;
```

The service owns one small ring queue per traffic class:

```c
typedef struct HomerPayloadReadyQueueEntry
{
    uint32_t streamIndex;
    uint64_t streamGeneration;
} HomerPayloadReadyQueueEntry;

typedef struct HomerPayloadReadyQueue
{
    uint32_t head;
    uint32_t tail;
    uint32_t count;
    HomerPayloadReadyQueueEntry entries[HOMER_PAYLOAD_READY_QUEUE_CAPACITY];
} HomerPayloadReadyQueue;

HomerPayloadReadyQueue payloadReadyQueues[HOMER_TRANSPORT_TRAFFIC_CLASS_COUNT];
```

Use fixed capacity sized above the active stream table, for example
`2 * CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS` or
`4 * CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS`. The current capacity is only
64 streams, so the queue can stay small and cache-local.

```text
payload dispatcher:
    decode the direct payload binding
    validate stream index, stream generation, and connection generation
    enqueue {streamIndex, streamGeneration} only when recvReadyEnqueued == false

payload executor:
    pop or peek one ready queue entry
    validate index, stored generation, active state, binding, and membership bit
    discard stale entries at the head without executing payload work
    drain within byte/record budget
    recheck ring
    enqueue a fresh tail entry when unread payload remains
```

On stream close or reclamation, clear `recvReadyEnqueued` and invalidate the
binding/generation; do not scan the queue. Any old queue entries are lazily
discarded when they reach the head. If a queue reaches a pressure high-water mark
or fills, compact it in place by keeping only entries that still validate. If it
still overflows after compaction, treat that as a correctness/resource bug rather
than silently falling back to a stream scan.

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
use fixed per-traffic-class payload ready queues
enqueue stream only when recvReadyEnqueued == false
claim/requeue streams through O(1) ring-queue operations
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
service-stream lookup, and service-owned fixed array-backed ready queues.

Do not retain serviceSinkId as the immediate token, and do not build a direct
map keyed by serviceSinkId.

serviceSinkId is a semantic identifier, not a safe transport binding:
    it has no generation encoded in the immediate
    it is wider than the 30-bit token namespace
    it does not directly identify a fixed stream-table slot
    reusing or resolving it still requires separate lookup and lifetime machinery

The payload token is an opaque receiver-issued capability that identifies one
local payload stream table slot and one allocation generation. The ready queue is
not a linked list; it is a small non-concurrent ring per traffic class.
```

Pre-implementation finding after Slice 2D1:

```text
2E is no longer blocked, but it must be implemented through the staged token and
ready-queue plan below rather than as a mechanical cleanup.

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
    The planned ready queue is service-owned, because the queue entries identify
    HomerServicePayloadStreamEntry/HomerPayloadStreamState objects and must be
    ordered by traffic class. The physical recv-CQ owner currently has no
    service-stream pointer, binding table, or generation-bearing payload token
    that would let it enqueue a stream directly.
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
    enqueue HomerServicePayloadStreamEntry into a fixed ready queue exactly
    once while recvReadyEnqueued is false
    have the payload executor claim/requeue in O(1) after draining work

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

2E.4 - Add service-owned array-backed ready queues:

```c
typedef struct HomerPayloadReadyQueueEntry
{
    uint32_t streamIndex;
    uint64_t streamGeneration;
} HomerPayloadReadyQueueEntry;

typedef struct HomerPayloadReadyQueue
{
    uint32_t head;
    uint32_t tail;
    uint32_t count;
    HomerPayloadReadyQueueEntry entries[HOMER_PAYLOAD_READY_QUEUE_CAPACITY];
} HomerPayloadReadyQueue;

HomerPayloadReadyQueue PayloadReadyQueues[HOMER_TRANSPORT_TRAFFIC_CLASS_COUNT];
```

Use fixed queue capacity sized above the stream table, initially
`2 * CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS` or
`4 * CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS`. The current table capacity is
64, so a small fixed queue is enough. Add to each payload stream entry:

```c
bool recvReadyEnqueued;
uint64_t recvReadyStreamGeneration;
uint32_t payloadDoorbellTokenGeneration;
uint32_t localPayloadDoorbellToken;
uint32_t peerPayloadDoorbellToken;
```

Queue helpers:

```text
HomerServiceEnqueuePayloadReady(service, streamIndex)
HomerServiceClaimPayloadReady(service, streamIndex, streamGeneration)
HomerServiceCompactPayloadReadyQueue(service, trafficClass)
```

Invariants:

```text
a stream has at most one live ready-queue membership
its queue corresponds to its bound traffic class
repeated WIMMs coalesce while recvReadyEnqueued is true
stream reclamation clears recvReadyEnqueued before the entry is cleared
old queue entries are lazily discarded when they reach the queue head
queue entries always carry/validate the current stream generation
queue pressure compaction keeps only entries that still validate
overflow after compaction is a correctness/resource bug, not a fallback trigger
```

2E.5 - Integrate the queue with the scheduler safely:

```text
Do not claim streams while merely constructing candidates. A candidate may be
built but not selected.

Candidate construction peeks/traverses the ready queue and emits:

typedef struct HomerPayloadReadyCandidate
{
    uint32_t streamIndex;
    uint64_t streamGeneration;
    HomerTransportTrafficClass trafficClass;
} HomerPayloadReadyCandidate;

At executor entry:
    validate index and generation
    claim the stream by popping a valid queue entry
    discard stale head entries without executing payload work
    drain within granted record/byte/WR budget
    recheck the underlying ring/frontier
    re-enqueue at tail if unread payload remains, budget was exhausted with more
    ready work, or a temporary dependency prevented draining
    leave it off-queue only after a stable empty recheck

Only receive-side payload readiness discovery moves from the broad stream scan
to this queue. Producer-side outgoing payload discovery can keep existing indexed
facts until its own scheduler work is completed.
```

2E.6 - Define stream close and token invalidation:

```text
Do not release the payload binding merely because a close request was received.

Release only after existing stream state proves:
    peer can no longer post new payload WIMMs
    final payload/EOS frontier has been observed
    local receive ring is drained through that frontier
    stream has no live payload ready-queue membership
    no payload executor action references its generation
    peer close/lifetime protocol permits reclamation

Then:
    clear recvReadyEnqueued
    mark token binding inactive
    clear connection binding
    preserve the external token-generation counter for the next allocation
    reclaim the stream entry

Do not scan/remove stale queue entries at close time. They are discarded at the
queue head or removed by pressure compaction.

A payload WIMM received after binding invalidation on the same connection
generation is a protocol error and should reset the affected connection.
```

2E.7 - Delete legacy payload pending state:

```text
After direct-token and ready-queue validation, delete:
    pendingDataDoorbellSinkIds
    pendingDataDoorbellCount
    TupleSinkServiceQueuePeerPayloadDoorbellRdma
    TupleSinkServiceConsumePeerDataDoorbellRdma
    TupleSinkServicePeerDataDoorbellPendingRdma

Also delete receivePayloadDoorbellPending after zero-reference check proves the
ready-queue state fully replaces it.
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
    add fixed array-backed ready queues
    switch receive-side scheduler readiness to the queues
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
    two or more WIMMs for one stream produce one ready-queue entry
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
    payloadReadyQueueHighWater
    payloadReadyQueueCompactions
    payloadReadyQueueStaleDrops
    payloadStaleTokenErrors
    payloadReadyFallbackDiscoveries

Acceptance:
    payloadReadyFallbackDiscoveries = 0
    payloadStaleTokenErrors = 0
    no legacy pending-array references
    no payload stream scan used for receive-doorbell discovery
```

Slice 2F, final recv-CQ ownership cleanup:

Implementation status: landed on June 22, 2026.

Current code pointers:

- [`TupleSinkServiceValidateRecvCqPollOwner()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:870)
  is the role-specific owner check. It accepts only
  `HOMER_RECV_CQ_POLL_BOOTSTRAP_SETUP` in bootstrap wait phases and only
  `HOMER_RECV_CQ_POLL_CANONICAL_COLLECTOR` after the ready dispatcher handoff.
- [`TupleSinkServicePollBootstrapRecvCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1582)
  is the only bootstrap recv-CQ poller. The outgoing and incoming bootstrap
  setup paths call it at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3801`
  and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3925`.
- [`TupleSinkServicePollBootstrapSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1523)
  remains the send-CQ-only bootstrap helper. It is deliberately separate from
  recv-CQ ownership.
- [`TupleSinkServiceFinishPeerConnectionSetup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3579)
  is the only handoff into canonical ownership. It checks bootstrap owner state,
  completed send/receive bootstrap CQEs, a valid peer descriptor, and permanent
  dispatcher installation; then posts steady-state notification receives, sets
  `setupPhase = READY`, sets owner phase to `CANONICAL`, and finally publishes
  `bootstrapComplete = true`.
- [`TupleSinkServiceDrainCanonicalRecvCq()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4290)
  is the sole steady-state recv-CQ drain. It is static and is reached through
  [`TupleSinkServiceDrainRecvCqCollectorAction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4476),
  which revalidates connection index, generation, and traffic class before
  polling.
- [`TupleSinkServiceDrainPeerConnectionCmEvents()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4536)
  is now physically CM-only and remains callable by write-preparation and
  peer-control helper paths until their later ownership cleanup.
- [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7639)
  is the only steady-state caller that builds recv-CQ collector actions. The
  outgoing and incoming scheduled action call sites are at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7886`
  and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:8011`.
- `TupleSinkServiceDrainPeerConnectionEventsRdma()`,
  `TupleSinkServiceDrainPeerConnectionEvents()`,
  `TupleSinkServiceDrainPeerConnectionRecvCq()`,
  `TupleSinkServiceRecvCqPhaseAllowsPoll()`, and
  `TupleSinkServicePollCompletionOnce()` have zero references after 2F.

2F contract:

```text
BOOTSTRAP:
    only the connection setup state machine may poll the recv CQ
    only while bootstrapComplete == false

CANONICAL:
    only the scheduled canonical peer recv-CQ collector may poll the recv CQ

TEARDOWN / UNUSED:
    no recv-CQ polling is permitted
```

Direct bootstrap polling is legitimate because setup is waiting for the
bootstrap `IBV_WC_RECV` exchange before steady-state WIMM receive buffers and
the permanent dispatcher become active. After the handoff, bootstrap polling
must never run again; WIMMs that arrive around the handoff remain queued in the
hardware CQ until the first canonical collector pass.

Implemented steps:

```text
1. Replace TupleSinkServiceRecvCqPhaseAllowsPoll() with role-specific validation.

   typedef enum HomerRecvCqPollRole
   {
       HOMER_RECV_CQ_POLL_BOOTSTRAP_SETUP = 1,
       HOMER_RECV_CQ_POLL_CANONICAL_COLLECTOR = 2
   } HomerRecvCqPollRole;

   static bool TupleSinkServiceValidateRecvCqPollOwner(
       TupleSinkServicePeerConnectionState *connection,
       HomerRecvCqPollRole role,
       char *errorMessage,
       size_t errorMessageBytes);

2. Bootstrap setup role is valid only when:
       connection active
       recvCqOwnerPhase == BOOTSTRAP
       bootstrapComplete == false
       setupPhase == OUTGOING_BOOTSTRAP_WAIT or INCOMING_BOOTSTRAP_WAIT

3. Canonical collector role is valid only when:
       connection active
       recvCqOwnerPhase == CANONICAL
       bootstrapComplete == true
       setupPhase == READY
       permanent recv dispatcher installed

4. TEARDOWN and UNUSED always reject. Every rejection increments
   noncanonicalRecvCqPollAttempts. Debug builds should assert after recording a
   useful diagnostic; normal builds should return failure so the caller can reset
   the affected connection.

5. Add TupleSinkServicePollBootstrapRecvCompletion(). The setup state machine
   must call this wrapper instead of polling connection->recvCompletionQueue
   directly. It validates HOMER_RECV_CQ_POLL_BOOTSTRAP_SETUP, polls only the
   connection recv CQ, accepts only successful IBV_WC_RECV for the bootstrap
   receive WR ID, and rejects IBV_WC_RECV_RDMA_WITH_IMM as an invalid bootstrap
   event.

6. Reorder TupleSinkServiceFinishPeerConnectionSetup() into the precise handoff:
       assert recvCqOwnerPhase == BOOTSTRAP
       assert bootstrapComplete == false
       assert bootstrap send and receive completed
       assert peer descriptor is valid
       assert permanent dispatcher is installed
       initialize connection-local token/ready state
       post all steady-state notification receive WQEs
       set setupPhase = READY
       set recvCqOwnerPhase = CANONICAL
       set bootstrapComplete = true as final readiness publication
       register/mark canonical collector facts

   No function that polls recv CQ may run between posting steady-state receives
   and setting bootstrapComplete true.

7. Delete TupleSinkServiceDrainPeerConnectionEventsRdma() from the header and
   implementation. Do not preserve it as a public canonical wrapper because a
   public helper cannot prove that its caller is the scheduled collector.

8. Physically split recv-CQ and CM implementations. Remove the combined
   drainRecvCq/drainCmEvents implementation shape. Keep a CM-only helper for
   connection lifetime/write-preparation paths. Rename the steady-state recv-CQ
   helper to static TupleSinkServiceDrainCanonicalRecvCq().

9. Restrict TupleSinkServiceDrainCanonicalRecvCq() to the two scheduled
   TupleSinkServicePumpPeerRequestsRdma() call sites. No control-op poller,
   payload helper, write-preparation helper, or public API may call it.

10. Add generation-safe collector actions now:

    typedef struct HomerRecvCqCollectorAction
    {
        bool incoming;
        uint32_t connectionIndex;
        uint64_t connectionGeneration;
        HomerTransportTrafficClass trafficClass;
        uint16_t maxPollBatches;
        uint16_t maxCqes;
    } HomerRecvCqCollectorAction;

    Execution skips stale inactive or generation-mismatched actions without
    polling. A generation match with non-CANONICAL ownership is an ownership bug
    and must fail/assert. This prevents an old action from polling a reused
    connection-table slot.

11. Enforce teardown ordering:
       remove collector demand/ready facts
       remove control and payload ready memberships
       invalidate command/completion/payload token bindings
       set recvCqOwnerPhase = TEARDOWN
       set bootstrapComplete = false
       destroy/reset QP and CQs
       release MRs and remaining connection resources
       clear table entry to UNUSED

    Do not add a teardown recv-CQ poll role. QP reset/destruction owns flushed
    receive WQEs; semantic events are not dispatched during teardown.
```

Low-level audit after implementation:

```text
Search for:
    ibv_poll_cq
    TupleSinkServicePollCompletionOnce
    recvCompletionQueue
    TupleSinkServiceDrainPeerConnectionEventsRdma

Raw peer recv-CQ polling may exist only in:
    TupleSinkServicePollBootstrapRecvCompletion()
    TupleSinkServiceDrainCanonicalRecvCq()

TupleSinkServiceDrainCanonicalRecvCq should have:
    one static function definition
    two scheduled peer-pump call sites
```

Instrumentation:

```text
bootstrapRecvCqPolls
canonicalRecvCqPolls
bootstrapRecvCompletions
canonicalRecvCompletions
bootstrapUnexpectedWimm
canonicalUnexpectedOpcode
noncanonicalRecvCqPollAttempts
staleRecvCqCollectorActions
recvCqOwnerTransitions
recvCqPollsByOwnerPhase[4]
```

Required steady-state results:

```text
noncanonicalRecvCqPollAttempts = 0
bootstrapUnexpectedWimm = 0
```

2F acceptance:

```text
bootstrap setup receives are consumed only by TupleSinkServicePollBootstrapRecvCompletion()
canonical collector is rejected before setup completion
bootstrap poller is rejected after canonical transition
all polling is rejected in TEARDOWN and UNUSED
TupleSinkServiceFinishPeerConnectionSetup() is the only canonical transition
WIMM arriving immediately around the handoff is consumed exactly once
stale collector action after reset performs no CQ poll
connection-table slot reuse cannot be targeted by an old collector action
TupleSinkServiceDrainPeerConnectionEventsRdma has zero references
raw recv-CQ polls exist only in the two owner functions
noncanonicalRecvCqPollAttempts remains zero in c1, c4, and basebackup validation
bootstrapUnexpectedWimm remains zero
```

Remaining caveats from completed Slice 2 stages:

```text
Control typed readiness is correct but not indexed. controlMailboxReady is now
the right semantic fact, but the service still scans active connections to find
ready control mailboxes. Slice 3D below turns this caveat into explicit indexed
readiness work.

Some older KB pointers may still describe residual completion FIFO state that
has already been removed. Keep updating those as touched; they do not block the
next slice.
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

Status: landed and validated through the persistent-pending repair. June 22
validation showed that the command and completion bitmap path was missing
durable service-owned readiness; the repaired Citus tree now transfers producer
signals into service-local pending bits and removes production fallback/re-arm
recovery.

The bitmap lives in `CitusRemoteExecControlRegion`, currently as
`completionReadySessions`. Backend code must not open-code bitmap layout or
atomic operations. Add a narrow helper/interface on the backend side, for
example:

```c
bool HomerBackendMarkCompletionReady(CitusRemoteExecControlRegion *controlRegion,
                                     uint32_t serviceSessionIndex,
                                     uint64_t serviceSessionGeneration);
```

The helper is the abstraction boundary between today's host shared-memory
implementation and a future DPU/DMA-visible readiness update. For now it
performs the cheap validation available in the backend and then atomically ORs
the session bit into shared memory with release semantics. Later the helper can
be replaced by a DMA-visible bitmap/doorbell update without changing completion
publisher call sites.

Add both fields to backend startup identity:

```c
uint32_t serviceSessionIndex;
uint32_t completionReadyBitmapEnabled;
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
call HomerBackendMarkCompletionReady(), which fetch_or's completionReadySessions
```

Add `HomerServiceAppendBackendCompletionBitmapSources()` to enumerate durable
service-local pending state, validate session index, session generation, mailbox
protocol, and append exact candidates. It must not exchange producer bitmaps,
clear local pending bits, re-set shared producer bits, or perform fallback
recovery. The executor finalizer is the only place that may discharge
service-local pending state.

Implemented shape:

- `CITUS_REMOTE_EXEC_BACKEND_PROTOCOL_VERSION` is now v12, and backend spawn,
  command-mailbox, and completion-mailbox shared-memory names use v12 prefixes
  in `src/include/distributed/homer/remote_execution_backend_protocol.h`.
- `CitusRemoteExecBackendStartupData` and
  `CitusRemoteExecBackendSpawnRequest` carry `serviceSessionIndex`,
  `completionReadyBitmapEnabled`, and `serviceSessionGeneration`.
  `completionReadyBitmapEnabled` is set only for completion mailboxes consumed
  by the service. Direct local `CLIENT_SQL_SESSION` frontend-polled completions
  intentionally leave it clear to avoid a pointless backend atomic and service
  wakeup.
- Backend publication calls
  `RemoteExecBackendMarkCompletionReady()` in
  `src/backend/distributed/utils/homer/remote_execution_backend_bridge.c`
  immediately after the release-store to the backend completion mailbox
  `publishedEpoch`.
- The service materializes completion-ring sources in
  `HomerServiceAppendBackendCompletionBitmapSources()` in
  `src/backend/distributed/utils/homer/tuple_sink_service_process.c`; normal
  ready-set construction no longer scans every session for backend completion
  readiness when the progress registry is active.
- The validated direction is now stricter: all service-consumed command and
  completion mailboxes must be discovered through the shared producer bitmap
  and then retained through service-local pending state until semantic execution
  discharges them. Direct frontend-consumed completion mailboxes remain outside
  this path because the service does not consume them.
- The current re-arm/fallback patch series should be treated as diagnostic
  scaffolding, not target architecture. In particular, service code should stop
  re-setting `commandReadySessions` or `completionReadySessions`; those shared
  bitmaps are producer-owned notifications, not service-owned pending work.
- Persistent pending implementation:
  - `HomerServiceSessionPendingState` in
    `src/backend/distributed/utils/homer/tuple_sink_service_process.c` owns
    service-local `commandPendingSessions` and `completionPendingSessions`.
  - `HomerServiceCollectSessionReadySignals()` is the only service-side
    consumer of the shared producer bitmaps; it exchanges producer bits to zero
    and ORs them into local pending state before ready-set construction.
  - `HomerServiceAppendRemoteClientSqlCommandBitmapSources()` and
    `HomerServiceAppendBackendCompletionBitmapSources()` enumerate local pending
    bits non-destructively and append generation-bearing exact source refs.
  - `HomerServiceFinalizeCommandPendingBit()` and
    `HomerServiceFinalizeCompletionPendingBit()` clear local pending only after
    selected semantic execution proves the machine has no remaining work.
  - `HomerServiceCommandMachineHasWork()` uses the monotonic
    `publishedEpoch > clientSqlRemoteCommandAcceptedEpoch` predicate.
    `HomerServiceCompletionMachineHasWork()` includes backend completion-ring
    unread work plus staged/deferred peer-client completion publication.
  - `TupleSinkServiceResetSession()` calls
    `HomerServiceClearSessionPendingState()` before clearing the table slot so
    stale local/shared bits cannot target a reused session index.
  - `HOMER_SERVICE_READY_BITMAP_AUDIT` is a disabled-by-default invariant check.
    When enabled it scans active sessions and fails if durable machine work has
    neither local pending nor shared producer signal before/after the predicate
    check; it never repairs progress.
  - Production references to `HomerServiceRearmUnplannedBitmapSources()`,
    `HomerProgressExecutionPlanContainsSource()`,
    `HomerServiceMarkCommandReadyByIndex()`,
    `HomerServiceMarkCompletionReadyByIndex()`,
    `CommandReadyBitmapFallbackCountdown`, and
    `CompletionReadyBitmapFallbackCountdown` were deleted.

Validation evidence:

- Build/install:
  `sudo -n -u dbcomm make -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'`,
  `sudo -n -u dbcomm make -j8 CPPFLAGS='-D_GNU_SOURCE'`,
  `sudo -n -u dbcomm make install-headers install-service-bin install`,
  Postgres `ninja -C build src/backend/postgres src/bin/pgbench/pgbench src/bin/pg_basebackup/pg_basebackup`,
  and `meson install -C build --no-rebuild`.
- Installed artifact check: `pgbench`, `citus_tuple_sink_service`, and
  `libhomer_client.a` all contained v12 mailbox names and no v11 mailbox names.
- Earlier remote RDMA pgbench after restoring no-stats binaries:
  cold c1 `5000/5000` with 0 failures, warmed c1 `10000/10000` with 0 failures,
  `3965.690433 TPS`, p95 `0.264 ms`, p99 `0.279 ms`; warmed c4 `40000/40000`
  with 0 failures, `9917.272591 TPS`, p95 `0.534 ms`, p99 `0.614 ms`.
- Remote RDMA basebackup after restoring no-stats binaries: cold run completed
  in `5.95s`; warmed repeat completed in `4.25s`.
- Stats diagnostic with `HOMER_SERVICE_PROGRESS_STATS=1`: backend-host
  service-exit counters reported `completion_fallback_discoveries=0` while
  completion-ring work was present (`ready=61010`, `planned=64012`,
  `grant_progress=64012`). `completion_set_calls=0` in that diagnostic because
  the socketless backend bridge object was not compiled with the stats macro;
  the bitmap functionality was validated by zero fallback discoveries and
  completion-ring progress. Client-host `command_fallback_discoveries=12` is an
  existing command-ready bitmap follow-up and is not part of this completion
  bitmap slice.
- Follow-up fail-fast validation:
  - Command fallback first fired with `command_sequence=0`; the original
    command readiness predicate used `publishedEpoch != acceptedEpoch`, which
    could classify stale or non-forward epochs as work. It now uses
    `publishedEpoch > acceptedEpoch`.
  - After raced-in bit claiming and exchange/read re-arm, remote c4 `20000/20000`
    completed with 0 failures under the stats build; no fallback fatal appeared
    in service logs.
  - No-stats c1 remained correct (`20000/20000`, 0 failures, about
    `3939 TPS`, p99 `0.276 ms`).
  - No-stats c4 still failed the new completion fallback invariant. Observed
    fatal examples on `farnet1`:
    `completion-ready bitmap fallback discovered hidden work session_index=0 session=11 command_sequence=2535`,
    `session_index=2 session=7 command_sequence=3642`, and after the unplanned
    ready-set re-arm attempt `session_index=3 session=4 command_sequence=9135`.
    This became the blocking issue that led to the persistent-pending repair
    below.
- Persistent-pending validation:
  - No-stats build/install/sync:
    `sudo -n -u dbcomm make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'`,
    `sudo -n -u dbcomm make install-headers install-service-bin install`, and
    installed-prefix `rsync` to `farnet0`.
  - Source audit found no production references to the deleted re-arm/fallback
    symbols listed above.
  - Runtime cleanup note: `pkill -x citus_tuple_sink_service` does not match the
    service because Linux truncates `comm` to `citus_tuple_sin`; use exact
    command-path PID matching with `sudo kill` for validation cleanup. A stale
    `farnet0` service initially contaminated one run and logged
    `rdma_bind_addr failed ... Address already in use`.
  - After restarting both services with the new binary, remote RDMA c1 warmup
    completed `1000/1000` with 0 failures; warmed c1 completed `20000/20000`
    with 0 failures, `3927.452105 TPS`, p95 `0.268 ms`, p99 `0.282 ms`.
  - Remote RDMA c4 warmup completed `10000/10000` with 0 failures,
    `9321.766885 TPS`; warmed c4 completed `40000/40000` with 0 failures,
    `9301.285135 TPS`, p95 `0.618 ms`, p99 `0.710 ms`.
  - Remote RDMA basebackup completed a cold run in `6.28s` and a warmed run in
    `4.25s`.
  - Concurrent remote RDMA c4 plus remote RDMA basebackup completed with
    pgbench `10000/10000`, 0 failures, `7799.387280 TPS`; basebackup completed
    in `4.23s`.

Resolved blocker and corrected diagnosis:

The earlier c4 failures were not best modeled as four independent races. They exposed one
ownership error: `atomic_exchange(bitmap, 0)` removes the producer signal during
discovery, but the service currently transfers that signal only into a transient
ready set and execution plan. If the plan does not select the source, a guard
rejects execution, the action makes only staged peer-completion progress, the
budget ends, or the source blocks on another dependency, the temporary ready-set
state disappears. Every branch then needs to remember to recreate readiness, and
that is inherently fragile.

The current "bit visible before mailbox tail" explanation should not be treated
as a normal-path assumption. Frontend command producers and socketless backend
completion producers release-publish mailbox state before setting the shared
ready bit, and the service's acquire exchange is intended to observe the
preceding mailbox publication. A bit with no ready mailbox record can still be a
redundant signal for already-drained work, a stale lifecycle signal, or a real
producer/publication bug. It should not be papered over by blindly re-arming the
shared producer bitmap from service code.

Replacement design:

```text
durable mailbox/ring
    contains command/completion records

shared producer bitmap
    coalesced producer-to-service notification only

service-local pending bitmap
    durable scheduler-owned readiness until semantic work is discharged
```

Add persistent service-local pending state:

```c
typedef struct HomerServiceSessionPendingState
{
    uint64_t commandPendingSessions;
    uint64_t completionPendingSessions;
} HomerServiceSessionPendingState;
```

These fields are service-owned and non-atomic because the service loop is
single-threaded. At the beginning of each service pass, before ready-set
construction, collect shared producer signals:

```c
static void
HomerServiceCollectSessionReadySignals(CitusRemoteExecControlRegion *controlRegion)
{
    uint64_t commandBits =
        TupleSinkServiceAtomicExchangeU64(&controlRegion->commandReadySessions.bits, 0);
    uint64_t completionBits =
        TupleSinkServiceAtomicExchangeU64(&controlRegion->completionReadySessions.bits, 0);

    SessionPendingState.commandPendingSessions |= commandBits;
    SessionPendingState.completionPendingSessions |= completionBits;
}
```

Rewrite `HomerServiceAppendRemoteClientSqlCommandBitmapSources()` and
`HomerServiceAppendBackendCompletionBitmapSources()` to enumerate local pending
words non-destructively. They should validate the fixed session index,
`serviceSessionId`/generation, mailbox ownership, and append a generation-bearing
source reference. They must not require the mailbox predicate to be true before
appending, because coalesced producer signals can be redundant by the time
execution runs. If the slot is inactive or reused, clear the stale local bit and
increment `pendingStaleBitsCleared`.

The planner must never clear or re-arm readiness. Instead, command and
completion executors should return a centralized result:

```c
typedef enum HomerSessionSourceResult
{
    HOMER_SESSION_SOURCE_DRAINED = 0,
    HOMER_SESSION_SOURCE_MORE_READY,
    HOMER_SESSION_SOURCE_BLOCKED,
    HOMER_SESSION_SOURCE_STALE,
    HOMER_SESSION_SOURCE_FAILED
} HomerSessionSourceResult;
```

Only the executor finalizer may clear a local pending bit:

```text
DRAINED or STALE:
    clear the local pending bit

MORE_READY or BLOCKED:
    leave the local pending bit set

FAILED:
    leave cleanup to session teardown
```

Define authoritative machine-work predicates for finalization and audit:

```c
static bool HomerServiceCommandMachineHasWork(const TupleSinkServiceSessionState *session);
static bool HomerServiceCompletionMachineHasWork(const TupleSinkServiceSessionState *session);
```

The command predicate includes `publishedEpoch > clientSqlRemoteCommandAcceptedEpoch`
plus staged/retry work owned by the same command action. The completion predicate
includes `publishedEpoch > lastConsumedPublishedEpoch`, staged peer-client
completion publication, and any descriptor, payload-frontier, or source-credit
retry state progressed by the same completion action. Keep monotonic `>` checks;
do not use `!=`.

Session teardown must clear both local pending and shared producer bits only
after frontend/backend producers can no longer publish and before the table slot
can be reused:

```text
stop/close producer
wait for producer ownership to end
clear shared and local readiness
reset mailbox/session state
increment session generation
permit slot reuse
```

The repair should delete the current bandaid code after the pending layer is
active:

```text
HomerProgressExecutionPlanContainsSource()
HomerServiceRearmUnplannedBitmapSources()
HomerServiceMarkCommandReadyByIndex()
HomerServiceMarkCompletionReadyByIndex()
fallback raced-bit fetch_and recovery
exchange-path "predicate false, re-arm bit" branches
staged-completion shared-bit re-arm
remaining-record shared-bit re-arm
production command/completion fallback discovery
CommandReadyBitmapFallbackCountdown
CompletionReadyBitmapFallbackCountdown
```

During one validation stage, keep only a compile-time audit:

```c
#if HOMER_SERVICE_READY_BITMAP_AUDIT
```

The audit may scan active sessions, but it must never append candidates, claim or
re-set shared bits, advance mailboxes, or recover progress. Its assertion is:

```text
if machine has durable work
and neither local pending nor shared producer bit is set before/after the check:
    report ready-bitmap invariant violation
```

Use two signal snapshots around the durable-state check to avoid flagging a
producer racing the audit.

Recommended repair sequence:

1. `3A-R1`: add `HomerServiceSessionPendingState`, exchange shared command and
   completion bits into local pending, build candidates from local pending, and
   keep old fallback only as diagnostics.
2. `3A-R2`: add `HomerSessionSourceResult`, centralize command/completion
   pending-bit finalization, add authoritative machine predicates, remove every
   shared-bit re-arm from service code, and remove
   `HomerServiceRearmUnplannedBitmapSources()`.
3. `3A-R3`: delete normal fallback recovery, replace it with the temporary
   compile-time invariant audit, and rename fallback counters to audit
   violations for diagnostic output.
4. `3A-R4`: clear local/shared readiness during ordered session teardown, add
   stale-generation checks, run the full acceptance matrix, and disable the
   audit for accepted performance binaries.

Implementation note: `3A-R1` and `3A-R2` landed as one validated code stage.
Local pending without executor discharge is not a runnable service state because
pending bits would never clear. `3A-R3` and the session-teardown portion of
`3A-R4` landed in the same stage; additional deterministic fault-injection hooks
remain future validation tooling rather than production behavior. The landed
code did not thread a new `HomerSessionSourceResult` enum through every legacy
early-return branch; instead, selected command/completion executors run the
existing bounded helpers and then call centralized finalizers that consult
`HomerServiceCommandMachineHasWork()` and
`HomerServiceCompletionMachineHasWork()`. That preserves the ownership invariant
with a smaller hot-path edit.

Deterministic validation scenarios:

```text
producer publishes before bitmap exchange
producer publishes immediately after bitmap exchange
two records are published while one local pending bit is set
ready candidate is not selected because of plan budget
candidate is selected but a guard rejects execution
completion action publishes a staged peer completion and returns
completion action consumes one of multiple mailbox records
command/completion action blocks on source or payload credit
producer publishes while executor is draining
session closes and table slot is reused
c4 runs with deliberately one-source execution budget
c4 plus concurrent basebackup
```

Acceptance:

```text
no production fallback-discovery code
no service-side re-setting of producer-owned command/completion bitmaps
no ready mailbox without shared-or-local signal in audit builds
no lost command/completion progress
no stale session candidate execution
10 warmed c4 runs pass
5 c4-plus-basebackup runs pass
```

Useful counters:

```text
commandSignalsCollected
completionSignalsCollected
commandPendingHighWater
completionPendingHighWater
pendingCandidatesBuilt
pendingCandidatesSelected
pendingActionsDrained
pendingActionsRetainedMore
pendingActionsRetainedBlocked
pendingStaleBitsCleared
readyBitmapAuditViolations
```

Post-3A optimization note:

The landed persistent-pending repair preserves correctness, but it still
conflates "the service owns unfinished work" with "the work is runnable right
now" until 3C splits owned/runnable state. That means a completion action may
remain selectable while blocked on completion-publication source credit,
send-CQ retirement, payload/EOS visibility, remote mailbox credit, or deferred
peer-client publication. Retaining blocked work as perpetually runnable is safe
but can burn scheduler cycles; 3C must add the owned/runnable split and
dependency re-arm described below.

There is also a small c4 regression after 3A: c4 fell from about `9.9k TPS` to
about `9.3k TPS`, while c1 stayed near `3.9k TPS`. That pattern is consistent
with shared-cacheline contention on the command and completion ready bitmap
words. After the 3B correctness recovery is validated, optimize collection by
loading before exchanging:

```c
static uint64_t
HomerServiceCollectReadyWord(uint64_t *word)
{
    uint64_t observed = __atomic_load_n(word, __ATOMIC_ACQUIRE);

    if (observed == 0)
        return 0;

    return __atomic_exchange_n(word, 0, __ATOMIC_ACQ_REL);
}
```

This avoids a locked exchange when no producer bit is set. It is race-safe:
producer-before-exchange is collected by the exchange, while producer-after-zero
load remains set for the next pass. Also skip the command or completion
ready-word load entirely when the service has no active producer sessions of
that type. If warmed c4 remains more than about 3 percent below the 2F baseline
after this change, split each bitmap into eight cacheline-isolated shards:

```text
shard = sessionIndex & 7
bit = sessionIndex >> 3
```

This spreads sessions `0..3` across separate cache lines instead of making all
four producers serialize on one atomic word. Do this only after the smaller
load-before-exchange change is measured.

### 3B Critical Recv-CQ Demand Facts

This is a physical polling-demand fact for the critical client-command
completion recv CQ, not a replacement for control mailbox readiness and not just
the existing `terminalCompletionPendingPeerPoll` semantic flag.

Current status: the first implementation attempt is rejected as a scheduling
shape, even though several of its diagnostic fields remain useful. It maintained
only an aggregate `criticalClientCompletionDemandConnectionCount` and routed a
"demand only" filter through `TupleSinkServicePumpPeerRequestsRdma()`. That pump
still builds active incoming/outgoing connection arrays by scanning connection
tables and then filters only the recv-CQ branch. The result is not an exact
collector action; it is:

```text
aggregate source permanently ready
    -> broad peer pump
    -> active connection-table scans
    -> poll selected critical CQ
```

The validation failure made the scheduling bug visible. The demanded recv-CQ
source was granted `100,028,004` times in about 12 seconds while producing only
five useful CQ events. Empty CQ polling is observation, not semantic progress.
The demand fact should make a critical CQ eligible and timely; it must not let an
empty collector monopolize the service plan.

The same diagnostic run showed a likely starvation consequence on the backend
side:

```text
peer_receiver_completions_consumed = 4
peer_completion_publish_posted = 4
peer_completion_publish_retired = 0
```

This points at send-CQ/resource-retirement starvation: peer-client completion
publication posted signaled WIMMs but did not retire them, so publication source
or owner slots can fill and block the next terminal completion. Therefore 3B is
not independently complete until the minimal exact send-CQ demand part of 3C is
pulled forward.

Temporary recovery rule: disable the current scheduler insertion of filtered
`PEER_RECV_CQ | CRITICAL_CLIENT_COMPLETION_DEMAND` while developing the
replacement. Keep the session demand fields, connection counters, and mark/clear
diagnostics. First verify that the 3A-only c1/c4 path completes again so new
failures are not hidden by hot empty polling.

The target implementation still keeps the semantic "one remote client command is
waiting for terminal visibility" latch in the client-SQL session, but the
physical readiness fact must identify exact peer connections and generations.
The service scheduler should consume exact transport facts for critical
connections with client-completion demand instead of scanning sessions or
connections itself.

Track per session:

```c
typedef struct HomerRemoteTerminalDemandTicket
{
    bool active;
    bool incoming;
    uint32_t connectionIndex;
    uint64_t connectionGeneration;
    uint64_t commandSequence;
} HomerRemoteTerminalDemandTicket;
```

The ticket replaces the raw connection-handle-only latch. It is generated only
after command body publication succeeds, final command ready publication
succeeds, and `clientSqlRemoteCommandAcceptedEpoch` advances. If demand marking
fails after the command was published, the command cannot be rolled back; mark
the affected session/connection failed and enter teardown.

Track per critical connection:

```c
uint32_t clientCommandsAwaitingTerminal;
uint16_t criticalRecvEmptyPollStreak;
uint64_t criticalRecvNextEligiblePass;
```

Track exact demand sets per peer transport:

```c
typedef struct HomerCriticalRecvDemandSet
{
    uint64_t incomingBits;
    uint64_t outgoingBits;
    uint32_t demandedConnectionCount;
    uint32_t incomingRoundRobinCursor;
    uint32_t outgoingRoundRobinCursor;
} HomerCriticalRecvDemandSet;
```

The aggregate count may remain as a summary, but it must no longer be the only
routing fact. On `clientCommandsAwaitingTerminal` transition `0 -> 1`, set the
exact incoming/outgoing bit and increment `demandedConnectionCount`. On
transition `1 -> 0`, clear the exact bit and decrement the count. Connection
reset clears its exact bit before clearing the connection slot.

On successful remote command publication, after the command is actually
committed to the peer transport rather than merely selected as a candidate:

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

`STARTED` does not decrement demand. For the remote pgbench path, the decrement
belongs on the receiver-side peer-client completion WIMM handler after the
completion slot has been validated and CPU-published to the frontend-polled
ready word. The backend-node sender posting the completion WIMM is not sufficient
because it has not made the completion visible to the host client process.
Session failure/teardown clears the flag and decrements the connection count
exactly once when the original connection generation is still live; connection
reset clears any remaining connection-owned demand as transport teardown state.

The scheduler action should become exact:

```text
DRAIN_CRITICAL_CLIENT_COMPLETION_RECV_CQ
```

Expose a narrow transport API rather than routing demanded polling through the
broad peer pump:

```c
bool TupleSinkServiceDrainCriticalCompletionRecvCqRdma(
    TupleSinkServicePeerTransportState *transport,
    bool incoming,
    uint32_t connectionIndex,
    uint64_t connectionGeneration,
    uint16_t maxPollBatches,
    uint16_t maxCqes,
    HomerRecvCqDrainResult *result,
    char *errorMessage,
    size_t errorMessageBytes);
```

The function resolves the exact fixed connection slot, validates active state
and generation, validates critical-control traffic class, validates
`clientCommandsAwaitingTerminal > 0`, and invokes the canonical recv-CQ
collector directly. It must not scan connection tables, poll listeners or CM
events, poll send CQs, or process mailboxes. Initial budget:
`maxPollBatches = 1`, `maxCqes = 8`. The source identity carries direction,
connection index, and connection generation so a plan cannot add duplicate
grants for the same demanded CQ in one pass.

Add bounded empty-poll pacing per demanded connection:

```text
new command demand:
    empty streak = 0
    eligible immediately

CQE observed:
    empty streak = 0
    eligible next pass

empty poll 1-4:
    eligible next pass

empty poll 5-8:
    skip 1 pass

empty poll 9-16:
    skip 2 passes

later empty polls:
    skip at most 8 passes
```

A service pass is much shorter than command/network latency, so this remains
latency-sensitive while preventing another 100-million-grant spin. Empty polls
increment an empty-poll counter, do not count as useful semantic progress, and
must not reset unrelated collector backoff or suppress send-CQ/resource-relief
work.

Fairness requirement for each service pass:

```text
at most one critical recv-CQ grant per demanded connection
at least one send-CQ/resource-retirement grant when such work is pending
at least one semantic-machine grant when runnable work exists
```

Critical recv-CQ may be high priority, but it may not consume the whole
collector or action budget.

Implementation guardrail: before coding this slice, audit and record the exact
increment and decrement hooks. The increment belongs at the successful remote
command publication point. The decrement belongs at terminal CPU publication to
the client completion mailbox, currently near the same success path that clears
`terminalCompletionPendingPeerPoll`. Failure, teardown, and connection reset
must clear the per-session flag and decrement exactly once if the flag is set.
The session flag and connection counter must never disagree.

Current hook audit:

- increment after the successful post path in
  `TupleSinkServicePumpRemoteClientSqlCommands()` once
  `clientSqlRemoteCommandAcceptedEpoch` advances;
- strict terminal decrement in
  `TupleSinkServiceHandlePeerClientCompletionDoorbell()` after
  `readyEpochSlots[slotIndex]` is CPU-published and the WIMM body reports
  `COMPLETED` or `FAILED`;
- local-path decrement in `TupleSinkServicePublishClientCommandCompletion()` is
  a no-op unless the session was a remote sender with an outstanding demand;
- best-effort cleanup in `TupleSinkServiceResetSession()`;
- transport reset cleanup in `TupleSinkServiceResetPeerConnection()` before the
  connection slot is cleared.

Validation status on 2026-06-22: implementation builds, but Slice 3B is not yet
accepted. A no-stats remote c1 run did not complete in the expected warm c1 band.
One diagnostic run with
`HOMER_SERVICE_CLIENT_SQL_STATS=1`, `HOMER_SERVICE_PEER_TRANSPORT_STATS=1`, and
`HOMER_SERVICE_PROGRESS_STATS=1` used remote `pgbench --homer -c 1 -j 1 -t 1000`
under a 12 second timeout and was killed after timing out.

Code state at the failed validation:

```text
citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h
    TUPLE_SINK_SERVICE_PEER_PUMP_PHASE_CRITICAL_CLIENT_COMPLETION_DEMAND
    TupleSinkServicePeerSchedulerFacts.criticalClientCompletionDemandConnectionCount

citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c
    TupleSinkServicePeerConnectionHandle.clientCommandsAwaitingTerminal
    TupleSinkServicePeerTransportState.criticalClientCompletionDemandConnectionCount
    TupleSinkServicePeerConnectionMarkClientCommandAwaitingTerminalRdma()
    TupleSinkServicePeerConnectionClearClientCommandAwaitingTerminalRdma()
    TupleSinkServicePumpPeerRequestsRdma() recv-CQ branch filter

citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c
    TupleSinkServiceSessionState.remoteCommandAwaitingTerminal*
    HomerServiceMarkRemoteCommandAwaitingTerminal()
    HomerServiceClearRemoteCommandAwaitingTerminal()
    mark after TupleSinkServicePumpRemoteClientSqlCommands() accepts the peer write
    clear after TupleSinkServiceHandlePeerClientCompletionDoorbell() CPU-publishes
    filtered PEER_RECV_CQ collector before REMOTE_CLIENT_SQL_SENDER actions
```

Observed diagnostic counters:

```text
farnet0 opener:
    remote_command_forwarded=5
    command-ring grants=5
    peer-recv-cq grants=100028004
    peer-recv-cq grant_progress=5
    cq-drain grants=12, grant_progress=1
    terminal demand remained for session=1 sequence=5 at teardown

farnet1 backend side:
    peer_receiver_completions_consumed=4
    peer_completion_published=4
    peer_completion_publish_posted=4
    peer_completion_publish_retired=0
    completion-ring grants=4
```

Important symptom details:

```text
remote pgbench did not finish 1000 transactions within 12 seconds
timeout killed the client; service teardown then cleared one remaining demand
farnet0 log reported:
    clearing 1 client-completion recv-CQ demand entries during peer connection reset
    could not clear remote client SQL terminal demand session=1 sequence=5
        reason=session-reset detail=cannot clear terminal command demand on stale peer connection
farnet1 log showed backend-side peer completions were consumed and WIMMs were posted
both services stayed alive until explicit teardown; no fail-fast assertion fired
no production fallback discovery was reintroduced
```

This means the opener did make command-forwarding progress and the backend side
did publish several peer-client completions, but the full pgbench transaction did
not complete. The primary diagnosis is now a scheduler/ownership deviation:

- demanded recv-CQ work is still an aggregate source that calls a broad peer pump
  and scans connection tables;
- the source remains highest-priority and runnable while the CQ is empty;
- empty polling does not make semantic progress but was granted roughly twenty
  million times per useful CQE;
- resource-relief/send-CQ retirement work was likely starved, shown by posted
  peer-client completions with zero retirement;
- terminal demand clears for earlier commands, then remains on the last observed
  sequence;
- the fifth command was forwarded by the opener but its backend completion was
  not observed before timeout.

What the diagnostic appears to rule out:

- the opener never sending commands: `remote_command_forwarded=5`;
- the backend-side completion machine being entirely dead:
  `peer_receiver_completions_consumed=4`;
- the peer-client completion WIMM publication path being entirely dead:
  `peer_completion_published=4`;
- the demanded recv-CQ collector never being scheduled:
  `peer-recv-cq grants=100028004`.

What remains ambiguous:

- whether sequence 5 was not made visible in the backend-side command mailbox;
- whether sequence 5 was executed but its peer-client completion publication was
  blocked before posting;
- whether peer-client completion WIMMs for earlier sequences were handled by the
  opener but the stats are insufficient to prove each callback and demand-clear;
- whether the always-hot recv-CQ demand path causes the machine-baseline policy
  to spend too many grants on empty observation and starve the resource-relief or
  command-machine step needed for sequence 5.

Do not treat Slice 3B as complete until a follow-up diagnostic distinguishes:

1. whether the fifth command body/ready publication is not becoming visible to
   the backend-side command machine;
2. whether backend execution is producing the fifth completion but publication
   is blocked behind peer-client completion source credit / send-CQ retirement;
3. whether the always-hot demanded recv-CQ action is starving another required
   scheduler action under machine-baseline budgets.

Required recovery sequence:

1. `3B-R0`: disable current hot-spin scheduler insertion of filtered
   `PEER_RECV_CQ | CRITICAL_CLIENT_COMPLETION_DEMAND`; keep mark/clear
   diagnostics and verify 3A-only c1/c4 still completes.
2. `3B-R1`: replace aggregate-only demand with exact incoming/outgoing demand
   bitmaps and round-robin cursors in peer transport.
3. `3B-R2`: replace the raw session latch with
   `HomerRemoteTerminalDemandTicket` carrying direction, connection index,
   connection generation, and command sequence.
4. `3B-R3`: add the exact recv-CQ drain API and schedule exact
   generation-safe collector actions with small CQ budgets.
5. `3B-R4`: add bounded empty-poll pacing so a critical CQ stays timely without
   becoming a permanent hot-spin source.
6. `3B-R5`: enforce collector fairness: critical recv-CQ grants do not suppress
   send-CQ/resource-retirement or runnable semantic-machine grants.
7. `3B-R6`: pull forward exact send-CQ demand for signaled peer-client
   completion publication WIMMs, described in 3C below.
8. `3B-R7`: record resource-blocking reasons when completion publication cannot
   reserve source/owner credit; blocked owned work is not runnable until the
   relevant send-CQ/resource dependency is satisfied.

Demand-clear lifecycle:

```text
normal terminal completion:
    TupleSinkServiceHandlePeerClientCompletionDoorbell() validates slot/seal
    CPU-publishes readyEpochSlots[slot]
    terminal state requires a matching active demand ticket
    matching direction/index/generation/sequence decrements connection demand
    clear session ticket

STARTED:
    never clears the ticket

connection reset:
    clear exact critical recv-demand bit
    clear clientCommandsAwaitingTerminal
    clear demanded-connection count for that connection
    record generation as dead

session cleanup after connection-led teardown:
    if ticket's connection is still live and matching, decrement transport demand
    if ticket's connection is stale/reset, transport already cleared physical demand
    clear session ticket locally and increment staleDemandAbandoned
```

The current "could not clear terminal command demand on stale peer connection"
message should become expected stale-ticket cleanup after connection-led
teardown, not a hard demand-clear error.

3B-R0 implementation and validation:

- Citus commit-in-progress removed machine-baseline consumption of
  `criticalClientCompletionDemandConnectionCount` in
  `HomerServiceBuildProgressCollectorCandidates()` and removed the early
  filtered recv-CQ collector grant in
  `HomerServiceBuildMachineBaselineProgressActionPlan()`. The transport-side
  demand counters, session mark/clear hooks, and the unused peer-pump filter
  remain as diagnostics/scaffolding until the exact-action replacement lands.
- Build/install/sync used no-stats binaries:
  `sudo -n -u dbcomm make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'`,
  `sudo -n -u dbcomm make install-headers install-service-bin install`, and
  installed-prefix `rsync` to `farnet0`.
- Remote RDMA smoke after service restart completed `1000/1000` c1 with zero
  failures, proving the rejected hot-spin action was the proximate cause of the
  prior 12-second c1 timeout.
- Sequential warmed checks completed:

```text
remote c1, -t 20000:
    20000/20000, 0 failures
    3308.045531 TPS
    p95 0.313 ms, p99 0.332 ms

remote c4, -t 10000 per client:
    40000/40000, 0 failures
    8847.670541 TPS
    p95 0.654 ms, p99 0.771 ms
```

- An earlier c1 and c4 pair was accidentally launched concurrently and is
  discarded as performance evidence; it is only additional correctness signal.
- `farnet1` and `farnet0` service logs showed no terminal-demand clear errors,
  client-completion recv-CQ demand reset errors, fallback discoveries, or reset
  messages in the post-R0 run window.

R0 acceptance: complete for correctness recovery. Performance is not final: c1
and c4 are below the earlier 3A validated band, and the remaining 3B work must
replace the disabled aggregate path with exact generation-safe recv-CQ actions,
bounded empty-poll pacing, and pulled-forward exact send-CQ demand.

The next diagnostic should use a short no-stats remote c1 run plus narrow
one-shot counters for peer-client completion WIMM callback success, terminal
demand clear count, command-machine consume count on the backend side, and
peer-client completion source-ring reserved count/high-water. Avoid adding
production fallback recovery; this is an ownership/scheduling bug to root-cause,
not a reason to reintroduce fallback discovery.

Suggested next instrumentation points:

- increment a receiver-side counter at the top and success exit of
  `TupleSinkServiceHandlePeerClientCompletionDoorbell()` with token, session
  index, command sequence, and terminal/non-terminal state in debug builds;
- count `HomerServiceClearRemoteCommandAwaitingTerminal()` success/failure by
  reason and command sequence;
- count backend-side command mailbox records consumed by the peer receiver path,
  including the last command sequence accepted by the backend-side command
  machine;
- expose `clientSqlPeerCompletionPublishSourceRing.reservedCount`,
  `PeerClientCompletionPublishCompletionActiveCount`, and
  `PeerClientCompletionPublishSignaledCompletionActiveCount` in the exit stats;
- add a bounded diagnostic that reports the first pass where
  `criticalClientCompletionDemandConnectionCount > 0` and the plan contains no
  command-send-CQ/resource-relief grant while peer-client completion publish
  resources are active.

### 3C Resource-Pressure Readiness

Slice 3C is now split into a minimal physical send-CQ demand subset required by
3B, followed by the broader owned-versus-runnable resource-pressure work.

3C-minimal for 3B: add exact send-CQ demand for peer-client completion
publication.

Add per peer transport:

```c
uint64_t incomingSendCqDemandBits;
uint64_t outgoingSendCqDemandBits;
```

Add per connection:

```c
uint32_t signaledSendOwnersOutstanding;
```

When any signaled WR is successfully posted, transition `0 -> 1` outstanding
sets the exact send-CQ demand bit. When typed send-CQ retirement removes owners
and the remaining count becomes zero, clear the bit. For peer-client completion
publication, the final full-slot WIMM is signaled, so posting it must arm
send-CQ demand immediately. Add an exact send-CQ collector action carrying
direction, connection index, connection generation, and one poll-batch budget.
Schedule it alongside or before critical recv-CQ polling whenever signaled
owners are outstanding. Do not wait until source allocation fails before
beginning send-CQ polling.

Acceptance for the pulled-forward subset:

```text
peer_completion_publish_posted > 0 with peer_completion_publish_retired == 0
cannot persist indefinitely while the connection remains healthy
send-CQ collector does not scan unrelated connections
send-CQ collector uses connection generation to reject stale actions
critical recv-CQ hot polling cannot suppress exact send-CQ grants
```

Broader 3C: separate owned work from runnable work for session-owned send
resources. Extend `HomerServiceSessionPendingState` from a single pending word
per kind into owned and runnable words:

```c
typedef struct HomerServiceSessionPendingState
{
    uint64_t commandOwnedSessions;
    uint64_t commandRunnableSessions;
    uint64_t completionOwnedSessions;
    uint64_t completionRunnableSessions;
} HomerServiceSessionPendingState;
```

Transitions:

```text
producer signal:
    set owned
    set runnable

executor DRAINED:
    clear owned
    clear runnable

executor MORE_READY:
    keep owned
    keep runnable

executor BLOCKED(reason):
    keep owned
    clear runnable
    register exact blocked reason

dependency becomes satisfiable:
    set runnable
```

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

### 3D Control Indexed Readiness

Replace broad active-connection scans for control mailbox discovery with an
indexed readiness structure. This slice uses the existing `controlMailboxReady`
fact as the semantic per-connection state, but makes discovery exact.

Do not implement this as a FIFO queue. Control connections already have stable
table indexes and generations, so the primary structure should be fixed and
indexed:

```c
controlReadyBits[direction][trafficClass][word]
controlReadyRoundRobinCursor[direction][trafficClass]
```

The ready bitmap is level-triggered and uses arm/disarm semantics:

```text
control WIMM CQE decoded:
    set connection->controlMailboxReady
    set the indexed ready bit for that connection

candidate construction:
    scan ready bitmap words from the traffic-class round-robin cursor
    emit {incoming/outgoing, connectionIndex, connectionGeneration, trafficClass}
    do not clear the ready bit while merely building a candidate

executor:
    validate connection index and generation
    consume control mailbox records within budget
    if stable empty recheck says consumedHead >= arrivedTail:
        clear controlMailboxReady
        clear the indexed ready bit
    else:
        leave the bit armed

teardown/reset:
    clear the indexed bit
    invalidate stale candidates by generation
```

Repeated control WIMMs coalesce while the ready bit is set. Budget exhaustion
does not re-enqueue anything; the bit simply remains armed until the mailbox is
stable-empty. This is intentionally different from payload ready queues, where
many streams need round-robin stream-level fairness. Control readiness is
connection-indexed and should avoid both active-connection scans and stale FIFO
entries.

Acceptance:

```text
control WIMM sets controlMailboxReady and the indexed ready bit
candidate construction does not clear readiness
executor clears the bit only after a stable empty recheck
budget exhaustion leaves the bit armed
connection reset/teardown invalidates stale generation candidates
no active-connection scan is used for control mailbox discovery
```

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
| Slice 2E    | Landed through 2E-C; detailed payload-ready counters remain follow-up work            |
| Slice 2F    | Landed; strict bootstrap/canonical recv-CQ ownership and wrapper deletion validated    |
| Slice 3A    | Landed; persistent service-local pending readiness replaces production re-arm/fallback recovery |
| Slice 3B    | First attempt rejected; next step is 3B-R0 through 3B-R7 exact critical recv-CQ demand plus bounded pacing |
| Slice 3C    | Pull forward exact send-CQ demand for 3B, then implement owned/runnable resource-pressure readiness |
| Slice 3D    | Planned; control indexed readiness added                                            |
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
   backend completion bitmap, critical recv-CQ demand facts,
   control indexed readiness, resource-pressure bitmap.

4. Critical-first Stage 7d service scheduling:
   no additional QP, no dedicated thread, suppress bulk spin under foreground load.

5. Remove client legacy completion reconstruction and full pre-arm copies.

6. Close partial-post, mismatch-escalation, ERROR+EOS, and stale-grant caveats.

7. Complete lower-priority basebackup fragment/reservation cleanup.
```
