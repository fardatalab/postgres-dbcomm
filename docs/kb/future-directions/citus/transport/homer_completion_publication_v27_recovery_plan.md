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

3A optimization checkpoint on 2026-06-22:

- Implemented the smaller load-before-exchange optimization in the service
  collector. `HomerServiceCollectReadyWord()` first performs an acquire load of
  the producer-owned ready word and only performs the locked acquire-release
  exchange when the word is nonzero. A producer racing after a zero load leaves
  its bit set for the next service pass, so this preserves the producer-to-
  service ownership transfer contract.
- `commandReadyBitmapExchanges` and `completionReadyBitmapExchanges` now count
  actual nonzero ownership transfers in stats builds, not every empty service
  pass.
- Did not implement sharded ready-bitmaps in this checkpoint. The warmed c4
  numbers stayed within about 3 percent of the immediately prior 3B-R6
  checkpoint, so the larger ABI/data-structure change is not justified yet.

Validated on 2026-06-22 with no-stats binaries after build/install/sync and
fresh service restart:

```text
remote c1 smoke, -t 1000:
    1000/1000, 0 failures
    p95 0.245 ms, p99 0.268 ms

remote c1 warmed, -t 20000:
    20000/20000, 0 failures
    4316.070132 TPS
    p95 0.242 ms, p99 0.253 ms

remote c4 warmed repeat 1, -t 10000 per client:
    40000/40000, 0 failures
    10635.424045 TPS
    p95 0.507 ms, p99 0.587 ms

remote c4 warmed repeat 2, -t 10000 per client:
    40000/40000, 0 failures
    10595.490400 TPS
    p95 0.507 ms, p99 0.581 ms
```

Post-run service-log scan on both hosts found no terminal-demand clear errors,
critical recv-demand stale/reset diagnostics, fallback discoveries, reset
messages, source-ring-full errors, sequence mismatches, ready-bitmap errors, or
lane send-CQ drain failures. Only the intended PostgreSQL service on `farnet1`
and one Homer service on each host remained running.

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

3B-R1 through 3B-R4 implementation and validation:

- The peer transport now keeps exact critical recv-CQ demand bitmaps in
  `HomerCriticalRecvDemandSet`, with separate incoming/outgoing words,
  round-robin cursors, a demanded-connection summary, and a scheduler-pass
  counter. The aggregate
  `criticalClientCompletionDemandConnectionCount` remains only as a cheap
  summary used to decide whether querying exact demand is worthwhile.
- Each critical-control connection now carries
  `criticalRecvEmptyPollStreak` and `criticalRecvNextEligiblePass`. New terminal
  demand arms the connection immediately; successful CQE observation resets the
  streak; empty polls apply the planned bounded skip policy.
- The session semantic latch is now `HomerRemoteTerminalDemandTicket`, carrying
  direction, connection index, full connection generation, and command sequence.
  The ticket is captured after the command publication path succeeds and before
  the connection-owned physical demand is armed.
- The scheduler no longer routes demanded critical recv-CQ work through
  `TupleSinkServicePumpPeerRequestsRdma()`. It asks the transport for one exact
  `HomerCriticalRecvDemandAction`, then executes
  `HOMER_PROGRESS_ACTION_DRAIN_CRITICAL_CLIENT_COMPLETION_RECV_CQ` with a small
  budget (`maxPollBatches = 1`, `maxCqes = 8`).
- The exact drain path resolves the fixed incoming/outgoing connection slot,
  validates the captured generation and critical-control demand, and calls the
  canonical recv-CQ collector directly. It does not scan connection tables, poll
  CM/listener events, drain send CQs, or process mailboxes.
- Transitional implementation caveat: the current machine-baseline scheduler
  still builds and executes a plan within one `TupleSinkServicePumpOnce()` pass.
  The exact action therefore stores a direct `sourceRef.owner` pointer to the
  stack-local `HomerCriticalRecvDemandAction` candidate, because
  `HomerProgressSourceId` cannot hold the full 64-bit connection generation.
  This is acceptable only for immediate same-pass execution. The future
  two-phase scheduler must copy the exact action payload into durable plan
  storage rather than carrying this pointer.

Validated on 2026-06-22 with no-stats binaries after build/install/sync and
fresh service restart on `farnet1` and `farnet0`:

```text
remote c1 smoke, -t 1000:
    1000/1000, 0 failures
    p95 0.247 ms, p99 0.271 ms

remote c1 warmed, -t 20000:
    20000/20000, 0 failures
    4378.353682 TPS
    p95 0.238 ms, p99 0.248 ms

remote c4 warmed, -t 10000 per client:
    40000/40000, 0 failures
    10821.377740 TPS
    p95 0.516 ms, p99 0.592 ms
```

Post-run service-log scan on both hosts found no terminal-demand clear errors,
critical recv-demand stale/reset diagnostics, fallback discoveries, reset
messages, double-mark reports, or sequence mismatches. Only the intended
PostgreSQL service on `farnet1` and one Homer service on each host remained
running after the validation commands.

R1-R4 acceptance: complete. The exact-demand path fixes the 100-million-grant
hot-spin shape from the rejected first attempt and restores remote c4 near the
pre-regression target band. Remaining 3B work is still required before moving to
3A optimization work: R5 must preserve explicit collector fairness in the plan,
R6 must add exact send-CQ demand for signaled peer-client completion
publication WIMMs, and R7 must distinguish owned-but-blocked completion work
from runnable completion work.

3B-R5/R6 implementation and validation:

- R5 fairness is represented in the machine-baseline plan order: the command
  send-CQ collector is appended before local-control and before critical
  recv-CQ demanded actions, so resource-relief work cannot sit behind an
  always-eligible critical recv-CQ poll. The critical recv-CQ action remains
  bounded to one CQ batch and eight CQEs.
- R6 is implemented at the current service-owned lane-FIFO layer rather than as
  a new transport-side bitmap. This is the cleanest exact owner available today:
  `ClientSqlCommandWriteLaneFifos[]` and
  `PeerClientCompletionPublishLaneFifos[]` are already keyed by the
  `TupleSinkServicePeerConnectionHandle` whose QP owns the send CQ. The CQ drain
  executor now walks only lane FIFOs that contain a signaled owner and calls the
  typed send-CQ dispatcher on those exact lanes. It no longer scans active
  sessions to rediscover command/completion send-CQ connection handles.
- Peer-client completion publication signaled checkpoints now make command
  send-CQ relief immediately scheduler-ready. This avoids waiting for
  source-ring pressure or the old polling interval before retiring full-slot
  WIMM source slots.
- Payload send-CQ ownership is still stream-indexed, so payload CQ work remains
  discovered through the payload stream table until its own scheduler slice
  replaces that scan.
- R7 for peer-client completion source credit is already represented by the
  current completion-machine facts: a staged peer-client completion blocked on
  `HOMER_PROGRESS_REASON_COMPLETION_PUBLISH_SOURCE_CREDIT` is not runnable and
  waits on `HOMER_PROGRESS_WAIT_COMMAND_SEND_CQ`. The broader owned-versus-
  runnable split for all resource dependencies remains part of the later 3C
  cleanup.

Validated on 2026-06-22 with no-stats binaries after build/install/sync and
fresh service restart:

```text
remote c1 smoke, -t 1000:
    1000/1000, 0 failures
    p95 0.246 ms, p99 0.284 ms

remote c1 warmed, -t 20000:
    20000/20000, 0 failures
    4377.247854 TPS
    p95 0.237 ms, p99 0.247 ms

remote c4 warmed, -t 10000 per client:
    40000/40000, 0 failures
    10792.376265 TPS
    p95 0.516 ms, p99 0.595 ms
```

Post-run service-log scan on both hosts found no terminal-demand clear errors,
critical recv-demand stale/reset diagnostics, fallback discoveries, reset
messages, source-ring-full errors, sequence mismatches, or lane send-CQ drain
failures. Only the intended PostgreSQL service on `farnet1` and one Homer
service on each host remained running.

R5/R6 acceptance: complete for the current service-owned send-CQ owner model.
The remaining 3B/3C boundary is not a correctness blocker for pgbench c1/c4, but
the later scheduler should still replace payload stream scans and formalize the
owned-versus-runnable state for every blocked dependency, not only peer-client
completion source credit.

Pre-3D cleanup decisions:

- Do not copy 3B's exact-action payload style into 3D. The current 3B action is
  copied into `HomerProgressMachineCandidateSet.criticalRecvDemands[]`, but the
  compiled action still reaches it through `sourceRef.owner`. That is acceptable
  only for same-pass execution. Before adding control-mailbox exact actions,
  extend the compiled action grant with a durable typed payload union and
  migrate critical-recv actions to that storage.
- Add direction fairness to critical recv-CQ demand. The current iterator checks
  outgoing demand before incoming demand. Add a
  `nextCriticalRecvDirectionIncoming` flag and alternate the first direction
  examined after every successful critical-recv action selection.
- After the exact critical-recv action path is validated with typed payload
  storage, remove the rejected broad-filter compatibility path
  `TUPLE_SINK_SERVICE_PEER_PUMP_PHASE_CRITICAL_CLIENT_COMPLETION_DEMAND` so it
  cannot be accidentally re-enabled.

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

Slice 3C was originally split into a minimal physical send-CQ demand subset
required by 3B, followed by broader owned-versus-runnable resource-pressure
work. The pulled-forward subset has now landed as part of 3B-R5/R6 using the
service-owned lane FIFOs rather than new transport-side bitmaps.

Accepted 3C-minimal / 3B-R6 shape:

```text
ClientSqlCommandWriteLaneFifos[]
PeerClientCompletionPublishLaneFifos[]
    are the exact service-owned send-CQ owner sets for command writes and
    peer-client completion WIMM publications.

HomerServiceExecuteCqDrainProgressPlan()
    walks only active lane FIFOs that contain signaled owners
    drains each exact connection/QP through the typed send-CQ dispatcher
    keeps payload send-CQ discovery on the payload stream table for now

HomerMachineBaselinePolicyCommandSendCqDue()
HomerServiceCommandSendCqReadyForScheduler()
TupleSinkServiceRemoteClientSqlCommandSendCqShouldDrain()
    treat peer-client completion signaled checkpoints as immediate
    resource-relief demand
```

Do not add parallel transport-side send-CQ demand bitmaps for the same
command/completion owner classes unless ownership moves from the service lane
FIFOs into the peer transport layer. The current exact owner identity is the
`TupleSinkServicePeerConnectionHandle` stored in the service FIFO, and the typed
send-CQ drain validates the live connection before polling.

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

3C owned/runnable checkpoint on 2026-06-22:

- `HomerServiceSessionPendingState` now distinguishes owned and runnable bits:
  command owned/runnable, and completion owned/runnable. Producer-owned shared
  bitmap collection sets both owned and runnable. Executor finalization clears
  owned+runnable when the machine is drained.
- Command work remains effectively owned=runnable in this checkpoint. That keeps
  the command source behavior conservative while the command-side source-credit
  blocking model is refined later.
- Completion work now uses `HomerServiceCompletionMachineRunnable()` to avoid
  repeatedly selecting staged peer-client completion publication when it is
  known blocked on completion source credit, payload EOS visibility, or result
  send-CQ visibility. The service keeps the owned bit so the work is not lost.
- Peer-client completion source-credit blockage is rearmed exactly when
  `TupleSinkServiceRetirePeerClientCompletionPublishCompletionEntry()` observes a
  send CQE and releases a peer-client completion source slot. If the retry still
  cannot reserve source credit, the completion finalizer clears runnable again.
- This is intentionally not the full 3C generalization. Payload send-CQ and
  payload/frontier dependencies still use their existing stream-indexed facts,
  and command-side blocked ownership remains a follow-up.
- Follow-up rearm invariant: every blocked reason that clears completion
  runnable must have an exact rearm transition. Source-credit rearm is now
  explicit at peer-client completion source-slot retirement. The remaining
  follow-ups are payload/EOS frontier visibility and result send-CQ frontier
  retirement. They must set completion runnable when the dependency becomes
  satisfiable rather than relying on periodic semantic scans.
- Follow-up runnable-order invariant: if a staged peer-client completion exists,
  the completion machine's runnable test must evaluate the staged publication
  first. Unread backend completion records must not make the machine runnable
  while the staged publication that the executor will service first is blocked.
- Later hot-path cleanup: add `signaledOwnerCount` to each command/completion
  lane FIFO and update it on signaled owner insertion and CQ retirement. Today
  the scheduler predicates scan FIFO entries looking for a signaled owner. That
  is correct at current FIFO sizes, but the O(1) count is the cleaner owner fact
  before broader send-CQ scheduling work.

Validated on 2026-06-22 with no-stats binaries after build/install/sync and
fresh service restart:

```text
remote c1 smoke, -t 1000:
    1000/1000, 0 failures
    p95 0.237 ms, p99 0.259 ms

remote c1 warmed, -t 20000:
    20000/20000, 0 failures
    4342.431088 TPS
    p95 0.240 ms, p99 0.254 ms

remote c4 warmed repeat 1, -t 10000 per client:
    40000/40000, 0 failures
    10416.891823 TPS
    p95 0.526 ms, p99 0.635 ms

remote c4 warmed repeat 2, -t 10000 per client:
    40000/40000, 0 failures
    10580.524270 TPS
    p95 0.521 ms, p99 0.598 ms
```

The first c4 repeat was lower than the prior bitmap checkpoint, but the second
repeat returned to that same band. Treat this checkpoint as correctness-accepted
and performance-neutral within current run variance, not as a throughput
improvement. Post-run service-log scan on both hosts found no terminal-demand
clear errors, critical recv-demand stale/reset diagnostics, fallback
discoveries, reset messages, source-ring-full errors, ready-bitmap errors, or
lane send-CQ drain failures.

3C staged-completion rearm checkpoint on 2026-06-23:

- Implemented the remaining completion owned/runnable follow-ups in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
  `HomerServiceCompletionMachineRunnable()` now mirrors executor ordering: if a
  staged peer-client completion exists, its known blockers are checked before
  unread backend completion records are allowed to make the completion machine
  runnable. This prevents later backend-ring work from hiding a first staged
  publication that cannot yet make progress.
- Added `HomerServiceRefreshPeerClientCompletionDependencyReadiness()`. It
  inspects only the exact staged completion and its named result stream, clears
  `HOMER_PROGRESS_REASON_DEPENDENT_COMPLETION_WAIT_PAYLOAD_EOS` once
  `HomerServicePayloadStreamResultEosPosted()` is true, and clears
  `HOMER_PROGRESS_REASON_DEPENDENT_COMPLETION_WAIT_RESULT_SEND_CQ` once
  `HomerServiceTupleResultStreamHasPendingSendCompletions()` is false. When no
  known blocker remains, it rearms that exact completion session.
- Added `HomerServiceRefreshDeferredPeerClientCompletionReadiness()`, which
  walks only the service-owned deferred-completion list. This is not a broad
  fallback scan: every visited session already owns a staged payload-dependent
  completion. Typed send-CQ drains call this after retiring payload CQEs, and
  `TupleSinkServicePublishPendingPeerClientCommandCompletions()` refreshes the
  current deferred session before retrying publication.
- `HomerServiceFinalizeCompletionPendingBit()` now refreshes dependency
  readiness before deciding whether the owned completion bit is runnable. The
  existing source-credit rearm in
  `TupleSinkServiceRetirePeerClientCompletionPublishCompletionEntry()` remains
  the exact transition for source-slot pressure.
- Validation used no-stats binaries, installed as `dbcomm`, synced to
  `farnet0`, then restarted PostgreSQL on `farnet1` and both Homer services:
  - Remote RDMA pgbench c1 first run: `20000/20000`, 0 failures,
    `3022.093924 TPS`, p95 `0.239 ms`, p99 `0.250 ms`, with a one-time
    2-second max-latency outlier.
  - Remote RDMA pgbench c1 rerun: `20000/20000`, 0 failures,
    `4310.735023 TPS`, p95 `0.242 ms`, p99 `0.255 ms`.
  - Remote RDMA pgbench c4: `40000/40000`, 0 failures,
    `10744.577749 TPS`, p95 `0.527 ms`, p99 `0.616 ms`.
  - Remote RDMA basebackup: cold/warmup `5.44 s`, warmed repeats `4.17 s` and
    `4.15 s`.
  - Targeted service-log scans on both hosts found no `ERROR`, `FATAL`, payload
    doorbell binding mismatch, late-WIMM, stale-token, protocol, reset,
    fallback, generic failure, or deferred-list corruption signatures.
- Remaining 3C cleanup is now performance-oriented rather than required for the
  current correctness boundary: add `signaledOwnerCount` to the command and
  peer-client completion lane FIFOs so send-CQ readiness becomes O(1) instead of
  scanning fixed FIFO entries. Command-side owned/runnable source-credit
  blocking is still conservative and can be refined when command publication
  actually reports a blocked source-credit state.

### 3D Control Indexed Readiness

Replace broad active-connection scans for control mailbox discovery and semantic
control mailbox consumption with exact, connection-indexed actions. This slice
uses the existing `controlMailboxReady` fact as the per-connection semantic
state, but makes discovery and execution exact.

3D models the physical fact:

```text
CONTROL_MAILBOX_READY(connection)
```

Do not split this into `REQUEST_READY(connection)` and
`RESPONSE_READY(connection)`. The current local control mailbox is one ordered
FIFO. Outgoing connection mailboxes normally carry responses and reject
requests, but incoming connection mailboxes may carry either requests or
responses. A response at the FIFO head must be consumed before a later request
can be reached. Therefore the exact executor must peek/copy one mailbox record
and branch on `messageKind`.

#### 3D-0 Durable Typed Action Payloads

Before adding control-mailbox actions, remove the remaining exact-action
dependency on stack-local payloads carried through `sourceRef.owner`.

Add a tagged payload union to the compiled action grant:

```c
typedef union HomerProgressActionPayload
{
    HomerCriticalRecvDemandAction criticalRecv;
    HomerControlMailboxAction controlMailbox;
} HomerProgressActionPayload;

typedef struct HomerProgressCompiledActionGrant
{
    HomerProgressActionKind actionKind;
    HomerActionClass primaryClass;
    uint16_t planFlags;
    uint32_t reasonFlags;
    HomerGrantVector grantVector;
    HomerProgressGrant sourceGrant;
    HomerProgressMachineRef machine;
    HomerProgressActionPayload payload;
} HomerProgressCompiledActionGrant;
```

Add the control-mailbox payload:

```c
typedef struct HomerControlMailboxAction
{
    bool incoming;
    uint32_t connectionIndex;
    uint64_t connectionGeneration;
    HomerTransportTrafficClass trafficClass;
} HomerControlMailboxAction;
```

Migrate `HOMER_PROGRESS_ACTION_DRAIN_CRITICAL_CLIENT_COMPLETION_RECV_CQ` to
read `actionGrant->payload.criticalRecv`. Do not add another exact action that
depends on a pointer to a candidate-array or stack object.

Acceptance:

```text
execution plans may be copied or delayed without retaining stack pointers
critical recv-CQ exact action no longer dereferences sourceRef.owner
```

3D-0 implementation and validation checkpoint on 2026-06-22:

- `HomerProgressCompiledActionGrant` now carries
  `HomerProgressActionPayload`, with durable storage for
  `HomerCriticalRecvDemandAction` and the planned
  `HomerControlMailboxAction`.
- `HOMER_PROGRESS_ACTION_DRAIN_CRITICAL_CLIENT_COMPLETION_RECV_CQ` now reads
  the exact critical recv-CQ identity from the compiled grant payload instead
  of dereferencing `sourceRef.owner`. A zero connection-generation payload is
  treated as an invalid grant and reported before any CQ is polled.
- `HomerCriticalRecvDemandSet` now carries `nextDirectionIncoming`, and
  `TupleSinkServiceNextCriticalCompletionRecvDemandRdma()` alternates the first
  direction examined after each successful exact critical recv-CQ action
  selection. This removes the previous outgoing-before-incoming bias while
  preserving per-direction round-robin cursors.
- The rejected broad `CRITICAL_CLIENT_COMPLETION_DEMAND` phase/filter remains a
  follow-up deletion after the next exact-control action stage is validated.

Validated with no-stats binaries after build/install/sync and fresh Homer
service restart:

```text
remote c1 smoke, -t 1000:
    1000/1000, 0 failures
    p95 0.249 ms, p99 0.279 ms

remote c1 warmed, -t 20000:
    20000/20000, 0 failures
    4316.352371 TPS
    p95 0.242 ms, p99 0.256 ms

remote c4 warmed, -t 10000 per client:
    40000/40000, 0 failures
    10731.695015 TPS
    p95 0.524 ms, p99 0.609 ms
```

Post-run service-log scans on both hosts found no terminal-demand clear errors,
critical recv-demand stale/reset diagnostics, fallback discoveries, reset
messages, source-ring-full errors, ready-bitmap errors, lane send-CQ drain
failures, or missing generation-bearing payload diagnostics. Only the intended
PostgreSQL service on `farnet1` and one Homer service on each host remained
running after validation.

#### 3D-1 Transport-Owned Indexed Readiness

Do not implement control readiness as a FIFO queue. Control connections already
have stable table indexes and generations, so the primary structure should be a
fixed indexed bitmap:

```c
typedef struct HomerControlReadyIndex
{
    uint64_t incomingBits[HOMER_TRANSPORT_TRAFFIC_CLASS_COUNT];
    uint64_t outgoingBits[HOMER_TRANSPORT_TRAFFIC_CLASS_COUNT];

    uint32_t incomingCursor[HOMER_TRANSPORT_TRAFFIC_CLASS_COUNT];
    uint32_t outgoingCursor[HOMER_TRANSPORT_TRAFFIC_CLASS_COUNT];

    bool nextDirectionIncoming[HOMER_TRANSPORT_TRAFFIC_CLASS_COUNT];

    uint32_t readyCount;
} HomerControlReadyIndex;
```

Store it in `TupleSinkServicePeerTransportState`.

Add helpers:

```c
TupleSinkServiceArmControlMailboxReady(connection);
TupleSinkServiceDisarmControlMailboxReady(connection);
TupleSinkServiceClearControlMailboxReadyOnReset(connection);
```

Arm in `TupleSinkServiceQueuePeerControlDoorbellRdma()` after advancing
`controlDoorbellArrivedTail`. Reset/teardown must clear the indexed bit before
the table slot can be reused.

The ready bitmap is level-triggered:

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

3D-1 implementation and validation checkpoint on 2026-06-22:

- `remote_execution_peer_transport_rdma.c` now has a transport-owned
  `HomerControlReadyIndex` with incoming/outgoing bitmap words per traffic class,
  per-class cursors, direction alternation state, and a total ready-bit count.
- `TupleSinkServiceQueuePeerControlDoorbellRdma()` still advances
  `controlDoorbellArrivedTail` from the control WIMM CQE, but now arms
  `TupleSinkServiceArmControlMailboxReady()` instead of only setting the
  per-connection `controlMailboxReady` flag.
- The existing broad control-mailbox consumers remain in place for this slice,
  but their stable-empty paths call
  `TupleSinkServiceDisarmControlMailboxReady()` so the old per-connection mirror
  and the new indexed bit stay in lockstep.
- `TupleSinkServiceResetPeerConnection()` clears the indexed control-ready bit
  before unregistering facts, switching the recv-CQ owner to teardown, and
  zeroing/reusing the connection slot.
- This slice deliberately does not yet build exact control-mailbox candidates.
  3D-2 remains responsible for enumerating `HomerControlMailboxAction` values
  from the indexed bitmap and validating connection generation before execution.
- Validation used no-stats binaries, installed under `dbcomm`, synced to
  `farnet0`, and restarted Homer services on both hosts. Remote RDMA pgbench from
  `farnet0` to `farnet1` passed:
  - c1 smoke: `1000/1000`, 0 failures, about `455.6 TPS` excluding initial
    connection, with the expected cold setup outlier.
  - warmed c1: `20000/20000`, 0 failures, about `4285.8 TPS`, p95 `0.244 ms`,
    p99 `0.255 ms`.
  - warmed c4: `40000/40000`, 0 failures, about `10689.7 TPS`, p95 `0.517 ms`,
    p99 `0.593 ms`.
- Service-log scans on both hosts found no stale/reset/fallback/control-mailbox
  error patterns, and the post-run process preflight showed only the intended
  PostgreSQL service on `farnet1` plus one Homer service on each host.

Required cleanup before the exact-only control-mailbox path is considered
complete:

- `TupleSinkServiceArmControlMailboxReady()` currently represents the right
  ownership boundary, but in the exact-only end state it must not have a
  flag-only fallback. If an active canonical connection cannot be mapped to an
  indexed control-ready bit, that is a connection-fatal invariant violation:
  returning with only `controlMailboxReady = true` would create undiscoverable
  semantic readiness once broad mailbox scans are gone.
- Change the helper from `void` to `bool`, surface a useful diagnostic, and make
  active/canonical indexing failure reset the affected connection. Reset/teardown
  paths may still clear idempotently.
- Add or retain low-cost counters for `staleControlReadyBits`,
  `staleControlMailboxActions`, and `controlReadyCountUnderflow`. A stale action
  after candidate construction skips by generation; a stale ready bit during
  candidate construction is a lifecycle cleanup bug that should be counted and
  cleared.

3D-1 exact-only cleanup checkpoint on 2026-06-23:

- `TupleSinkServiceArmControlMailboxReady()` now returns `bool` and fails with a
  detailed diagnostic instead of setting only the per-connection
  `controlMailboxReady` flag when an active/canonical connection cannot be
  represented in `HomerControlReadyIndex`.
- Control WIMM materialization through
  `TupleSinkServiceQueuePeerControlDoorbellRdma()` and post-commit rearming in
  `TupleSinkServiceCommitLocalControlMessage()` propagate that failure rather
  than creating undiscoverable semantic work.
- Reset/disarm remains idempotent because teardown paths still need to clear
  best-effort state safely.
- Stats builds now expose `staleControlReadyBits`,
  `staleControlMailboxActions`, and `controlReadyCountUnderflow` through
  `peer_control_ready_stats`.
- Validation used no-stats binaries, installed as `dbcomm`, synced to `farnet0`,
  and restarted Homer services on both hosts. An accidental parallel c1/c4 run
  was discarded as contaminated. After truncating `pgbench_history` and refreshing
  pgbench table stats, sequential remote RDMA validation passed:
  - c1 smoke: `1000/1000`, 0 failures, p95 `0.248 ms`, p99 `0.265 ms`.
  - warmed c1: `20000/20000`, 0 failures, about `4231.4 TPS`, p95 `0.248 ms`,
    p99 `0.261 ms`.
  - warmed c4: `40000/40000`, 0 failures, about `10485.7 TPS`, p95 `0.531 ms`,
    p99 `0.610 ms`.
- Targeted service-log scans on both hosts found no
  `control mailbox ready cannot be indexed`, stale, fallback, reset, protocol,
  noncanonical, failure, or binding-mismatch diagnostics. Post-run process
  preflight showed only the intended PostgreSQL service on `farnet1` and one
  Homer service on each host.

#### 3D-2 Exact Candidate Enumeration

Add:

```c
bool TupleSinkServiceAppendReadyControlMailboxActionsRdma(
    TupleSinkServicePeerTransportState *transport,
    HomerControlMailboxAction *actions,
    uint16_t actionCapacity,
    uint16_t *actionCount,
    char *errorMessage,
    size_t errorMessageBytes);
```

Emit up to `HOMER_CONTROL_READY_CANDIDATE_BUDGET` exact connection candidates
per service pass, limited by remaining candidate-set capacity. Initial value:

```c
#define HOMER_CONTROL_READY_CANDIDATE_BUDGET 8U
```

Ordering:

```text
critical-control traffic class
foreground traffic class
bulk traffic class
maintenance traffic class
```

Within each class, alternate incoming/outgoing starting direction using
`nextDirectionIncoming[class]`, then use that direction's round-robin cursor.
Candidate construction is non-destructive; it must not clear ready bits.

Candidate validation:

```text
connection active
generation nonzero
canonical/ready connection
traffic class matches the ready-index bucket
controlMailboxReady == true
consumedHead < controlDoorbellArrivedTail
```

If a ready bit points at inactive/non-ready state during candidate construction,
reset should already have cleared it. Treat this as a lifecycle invariant
defect: clear the stale bit, increment `staleControlReadyBits`, assert in
diagnostic builds, and continue in production.

If a candidate becomes stale after construction because the connection resets
before execution, increment `staleControlMailboxActions` and skip without
touching the mailbox or clearing the bit; the table slot may already belong to a
new generation with real work.

3D-2 implementation and validation checkpoint on 2026-06-22:

- `TupleSinkServiceAppendReadyControlMailboxActionsRdma()` now enumerates exact
  `HomerControlMailboxAction` candidates from `HomerControlReadyIndex`.
- Enumeration is bounded by the caller's `actionCapacity`; the service currently
  passes `HOMER_CONTROL_READY_CANDIDATE_BUDGET == 8`.
- The transport emits at most one action per direction per traffic class per
  call, in `HomerTransportServiceLoopPriority` order, so the first candidate-only
  shape covers critical/foreground/bulk/maintenance and incoming/outgoing
  fairness without a queue.
- Candidate construction validates active/canonical connection state,
  generation, traffic class, `controlMailboxReady`, and
  `consumedHead < controlDoorbellArrivedTail`. Impossible stale indexed bits are
  cleared in the transport; valid candidate emission is non-destructive.
- `HomerProgressMachineCandidateSet` now carries bounded
  `controlMailboxActions[]` separately from collector facts and critical recv-CQ
  demand. The machine-baseline candidate bridge populates this array from the
  transport API.
- This slice intentionally does not plan or execute the exact control-mailbox
  action yet. Keeping 3D-2 candidate-only avoids installing a second mailbox
  consumer before 3D-3's peek/apply/commit ownership refactor.
- Validation used no-stats binaries, installed under `dbcomm`, synced to
  `farnet0`, and restarted Homer services on both hosts. Remote RDMA pgbench from
  `farnet0` to `farnet1` passed:
  - c1 smoke: `1000/1000`, 0 failures, about `456.0 TPS` excluding initial
    connection, with the expected cold setup outlier.
  - warmed c1: `20000/20000`, 0 failures, about `4270.8 TPS`, p95 `0.243 ms`,
    p99 `0.253 ms`.
  - warmed c4: `40000/40000`, 0 failures, about `10799.4 TPS`, p95 `0.495 ms`,
    p99 `0.567 ms`.
- Service-log scans on both hosts found no stale/reset/fallback/control-mailbox
  error patterns, and post-run process preflight showed only the intended
  PostgreSQL service on `farnet1` plus one Homer service on each host.

#### 3D-3 Peek / Apply / Commit Mailbox API

Refactor mailbox consumption before installing the exact executor. The current
`TupleSinkServiceTryConsumeLocalMailbox()` advances `consumedHead` before the
caller validates and semantically applies the message. Replace the execution
path with:

```c
bool TupleSinkServicePeekLocalControlMessage(
    connection,
    TupleSinkServicePeerControlMessage *message,
    uint64_t *messageSequence,
    bool *ready,
    ...);

void TupleSinkServiceCommitLocalControlMessage(
    connection,
    uint64_t messageSequence);
```

Flow:

```text
peek and stable-copy the next control record
validate protocol and ring sequence
dispatch request or complete response
commit consumedHead only after successful semantic application
```

No message is acknowledged before response correlation succeeds or the request
handler accepts/stages the request response.

3D-3 implementation and validation checkpoint on 2026-06-22:

- `TupleSinkServiceTryConsumeLocalMailbox()` has been removed.
- `TupleSinkServicePeekLocalControlMessage()` now stable-copies the next
  WIMM-published control mailbox record and returns the mailbox ring sequence
  without advancing `localMailbox.consumedHead`.
- `TupleSinkServiceCommitLocalControlMessage()` advances `consumedHead` only
  after semantic success. It also applies the peer `senderConsumedHead` mirror
  and arms/disarms `controlMailboxReady` plus the indexed ready bit.
- The outgoing response path now commits only after
  `TupleSinkServiceCompletePeerControlOp()` succeeds.
- The incoming mailbox path now commits only after either
  `TupleSinkServiceCompletePeerControlOp()` succeeds for a response at the FIFO
  head, or `TupleSinkServiceProcessIncomingMailboxRequest()` accepts/stages the
  response for a request.
- Implementation note: the first 3D-3 attempt reloaded
  `localMailbox.consumedHead` inside the commit helper as a defensive check. That
  kept correctness but dropped warmed remote c1 into roughly the `3.8k TPS` band.
  The accepted version treats the service loop as the sole local mailbox
  consumer and avoids the second shared-memory acquire load; it still checks the
  copied ring sequence and WIMM-derived arrived tail before committing.
- Validation used no-stats binaries, installed under `dbcomm`, synced to
  `farnet0`, and restarted Homer services on both hosts. After discarding the
  first post-restart cold setup run, remote RDMA pgbench from `farnet0` to
  `farnet1` passed:
  - warmed c1: `20000/20000`, 0 failures, about `4278.8 TPS`, p95 `0.243 ms`,
    p99 `0.262 ms`.
  - warmed c4 repeat 1: `40000/40000`, 0 failures, about `10594.6 TPS`,
    p95 `0.487 ms`, p99 `0.564 ms`.
  - warmed c4 repeat 2: `40000/40000`, 0 failures, about `10623.9 TPS`,
    p95 `0.506 ms`, p99 `0.569 ms`.
- Service-log scans on both hosts found no stale/reset/fallback/control-mailbox
  or commit-sequence error patterns, and post-run process preflight showed only
  the intended PostgreSQL service on `farnet1` plus one Homer service on each
  host.

#### 3D-4 Exact Control-Mailbox Executor

Add one transport API and make it the sole steady-state consumer of local
control mailbox records:

```c
bool TupleSinkServiceDrainPeerControlMailboxRdma(
    TupleSinkServicePeerTransportState *transport,
    const HomerControlMailboxAction *action,
    TupleSinkServicePeerRequestHandler requestHandler,
    void *requestContext,
    uint16_t maxMessages,
    HomerControlMailboxDrainResult *result,
    char *errorMessage,
    size_t errorMessageBytes);
```

The transport owns:

```text
exact connection lookup
connection/generation validation
mailbox sequence validation
response correlation
consumed-head advancement
ready-bit arm/disarm
```

The service handler owns semantic processing of request messages.

Drain result:

```c
typedef struct HomerControlMailboxDrainResult
{
    uint16_t messagesConsumed;
    uint16_t requestsDispatched;
    uint16_t responsesCompleted;
    bool moreReady;
    bool staleAction;
} HomerControlMailboxDrainResult;
```

Direction rules:

```text
outgoing connection:
    RESPONSE valid
    REQUEST protocol error

incoming connection:
    REQUEST valid
    RESPONSE valid
```

Execution policy should initially grant at most:

```c
#define HOMER_CONTROL_MAILBOX_ACTIONS_PER_PASS 2U
#define HOMER_CONTROL_MAILBOX_MESSAGES_PER_ACTION 16U
```

After each action budget:

```text
consumedHead < controlDoorbellArrivedTail:
    retain controlMailboxReady and indexed bit

stable empty:
    clear controlMailboxReady and indexed bit
```

3D-4 implementation and validation checkpoint on 2026-06-22:

- `HomerControlMailboxDrainResult` is now part of the peer transport API
  alongside `HomerControlMailboxAction`.
- `TupleSinkServiceDrainPeerControlMailboxRdma()` is the exact control-mailbox
  executor. It resolves the action's direction/index/generation to one fixed
  connection slot, rejects non-canonical or wrong-traffic-class ownership,
  treats inactive or generation-mismatched actions as stale, and then drains at
  most `maxMessages` records through the 3D-3 peek/apply/commit API.
- The exact executor preserves the ordered FIFO semantics: responses complete
  the matching peer-control op before commit; requests on incoming connections
  are dispatched through `TupleSinkServiceDispatchPeerRequest()` before commit;
  requests on outgoing connections are protocol errors.
- `HOMER_PROGRESS_ACTION_DRAIN_PEER_CONTROL_MAILBOX` is now scheduled by the
  machine-baseline policy from the 3D-2 exact candidates. The initial grant is
  bounded to at most two control-mailbox actions per pass and sixteen mailbox
  messages per action.
- This checkpoint intentionally leaves the old broad semantic mailbox consumers
  compiled and reachable. 3D-5/3D-6 still need to remove hidden response
  progress from `TupleSinkServicePollPeerRequestRdma()` and delete the broad
  response/request mailbox scans after this exact path is proven in isolation.
- Code pointers for the checkpoint:
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:236`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6424`,
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8870`,
  and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:30507`.
- Validation used no-stats binaries, installed under `dbcomm`, synced to
  `farnet0`, and restarted Homer services on both hosts. Remote RDMA pgbench from
  `farnet0` to `farnet1` passed:
  - c1 smoke: `1000/1000`, 0 failures, about `452.3 TPS` excluding initial
    connection, with the expected cold setup outlier.
  - warmed c1: `20000/20000`, 0 failures, about `4240.4 TPS`, p95 `0.246 ms`,
    p99 `0.258 ms`.
  - warmed c4 repeat 1: `40000/40000`, 0 failures, about `10415.7 TPS`,
    p95 `0.498 ms`, p99 `0.582 ms`.
  - warmed c4 repeat 2: `40000/40000`, 0 failures, about `10453.7 TPS`,
    p95 `0.492 ms`, p99 `0.574 ms`.
- The c4 numbers are slightly below the best 3D-2/3D-3 repeats but still in the
  current warmed correctness/performance band. Targeted service-log scans on
  both hosts found no exact-control mailbox errors, protocol errors, stale/reset
  diagnostics, fallback discoveries, or noncanonical polling diagnostics. The
  post-run process preflight showed only the intended PostgreSQL service on
  `farnet1` plus one Homer service on each host.

#### 3D-5 Remove Hidden Response Progress

After exact mailbox scheduling lands, change
`TupleSinkServicePollPeerRequestRdma()` to be non-observational with respect to
peer transport:

```text
publish request if not yet published
inspect op state
return PENDING / COMPLETED / FAILED
```

It must not consume response mailboxes, drain recv CQs, drain send CQs, poll CM,
or make unrelated peer transport progress. CM, send-CQ, recv-CQ, and control
mailbox progress belong to their typed collectors/executors.

3D-5 implementation and validation checkpoint on 2026-06-22:

- `TupleSinkServicePollPeerRequestRdma()` no longer drains peer CM events, send
  CQEs, or response mailbox records before inspecting the explicit async op
  state.
- `TupleSinkServiceTryPublishPeerControlAsyncOp()` no longer drains peer CM
  events, send CQEs, or response mailbox records before reserving and publishing
  a request. The old `TupleSinkServiceRecordPeerOpHiddenProgress()` hook was
  deleted because the op helper no longer records hidden CQ/mailbox progress.
- The peer-op poller now relies on typed progress:
  recv-CQ collectors decode control WIMMs, exact control-mailbox actions consume
  responses, and exact send-CQ demand retires signaled WR owners. A response
  that has not been consumed by those owners leaves the async op pending instead
  of being consumed behind the poller's back.
- Scope note: request publication may still call the existing outgoing
  connection setup helper when an async op was started before the lane became
  ready. This is the pre-existing setup lifecycle path, not response-mailbox
  progress. A later setup-owner cleanup should make that boundary stricter, but
  3D-5's validated change is removal of hidden response/CQ consumption from
  peer-op helpers.
- Validation used no-stats binaries, installed under `dbcomm`, synced to
  `farnet0`, and restarted Homer services on both hosts. Remote RDMA pgbench from
  `farnet0` to `farnet1` passed:
  - c1 smoke: `1000/1000`, 0 failures, about `455.2 TPS` excluding initial
    connection, with the expected cold setup outlier.
  - warmed c1: `20000/20000`, 0 failures, about `4292.3 TPS`, p95 `0.242 ms`,
    p99 `0.254 ms`.
  - warmed c4: `40000/40000`, 0 failures, about `10685.9 TPS`, p95 `0.517 ms`,
    p99 `0.583 ms`.
- Targeted service-log scans on both hosts found no exact-control mailbox
  errors, protocol errors, stale/reset diagnostics, fallback discoveries,
  noncanonical polling diagnostics, or old `async-start-progress` reset paths.
  The post-run process preflight showed only the intended PostgreSQL service on
  `farnet1` plus one Homer service on each host.

#### 3D-6 Delete Broad Semantic Mailbox Scans

After outgoing and incoming exact mailbox paths pass validation:

```text
remove RESPONSE_MAILBOX processing from the outgoing broad pump
remove REQUEST_MAILBOX processing from the incoming broad pump
remove their phase masks from machine-baseline planning
migrate compatibility policies to exact indexed mailbox actions
retain no semantic fallback scan
```

The broad peer pump may remain for setup, listener/CM, periodic recv-CQ liveness,
and remaining send-CQ work until their exact migrations are complete. It must no
longer be the steady-state consumer of control mailbox records.

Add one exact action kind:

```text
HOMER_PROGRESS_ACTION_DRAIN_PEER_CONTROL_MAILBOX
```

3D-6 committed implementation and validation blocker on 2026-06-22/23:

- Citus commit `5d2dd5c36` removed the broad peer-pump request/response
  mailbox phases from `TupleSinkServicePeerPumpPhaseMask`, deleted the old
  `TupleSinkServicePumpIncomingPeerMailbox()` and
  `TupleSinkServiceProgressPeerControlResponses()` consumers, stopped
  machine-baseline from translating request/response mailbox collectors into
  peer-pump grants, and left exact
  `HOMER_PROGRESS_ACTION_DRAIN_PEER_CONTROL_MAILBOX` as the only semantic
  control-mailbox consumer.
- Remote RDMA pgbench still passed:
  - c1 smoke: `1000/1000`, 0 failures, about `453.8 TPS` excluding initial
    connection, with the expected cold setup outlier.
  - warmed c1: `20000/20000`, 0 failures, about `4265.8 TPS`, p95 `0.244 ms`,
    p99 `0.255 ms`.
  - warmed c4: `40000/40000`, 0 failures, about `10810.7 TPS`, p95 `0.506 ms`,
    p99 `0.574 ms`.
- Remote RDMA basebackup also completed client-visible work, with a first run
  `6.05 s` and a warmed repeat `4.19 s`, but the `farnet1` service log then
  reported late payload doorbells after local stream reclamation:

```text
tuple-sink service: RDMA peer-control pump failed: payload doorbell binding mismatch token=16385 index=0 active=0 token_generation=4 stream_token_generation=0 peer_binding=0 connection_generation=3 stream_connection_generation=0 traffic_class=3 stream_traffic_class=0
tuple-sink service: RDMA peer-control pump failed: payload doorbell binding mismatch token=20481 index=0 active=0 token_generation=5 stream_token_generation=0 peer_binding=0 connection_generation=3 stream_connection_generation=0 traffic_class=3 stream_traffic_class=0
```

- Surrounding log context shows the errors occur after
  `marked send byte-ring terminal`, `reclaiming sink`, and
  `reclaiming idle non-reusable compatibility session` for the basebackup send
  streams. Decoding the tokens gives generation 4 / stream index 0 and
  generation 5 / stream index 0, so the payload-token decoder is working: two
  successive stream generations were reclaimed before a WIMM for that generation
  reached the local recv-CQ dispatcher.
- This should be treated as a pre-existing payload close/lifetime bug exposed by
  exact control-mailbox progress, not as a reason to restore broad mailbox
  scans. 3D-5 removed hidden peer-op transport progress and 3D-6 removed broad
  semantic mailbox consumers; together they allow a close response to be applied
  without the incidental delay that previously let more payload/credit WIMMs
  drain first.
- The late WIMM is probably a reverse consumed-head/credit WIMM for a local send
  stream, not late payload data. That distinction matters for the fix but does
  not make the mismatch harmless: the RDMA write targeting the mirrored head has
  already happened before the immediate token is decoded. If the stream slot or
  mirror storage has been reused, a stale peer write could corrupt a new stream
  generation.
- Do not mark 3D-6 complete by downgrading this mismatch to a warning, adding a
  grace period, or restoring broad scans. The required fix is an explicit
  payload close/quiescence protocol that prevents normal token invalidation
  until the peer can no longer post WIMMs for that token and all final payload or
  credit/head updates have been applied.

Current code pointers for this blocker:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21504`
  dispatches payload WIMMs in `TupleSinkServiceDispatchPeerPayloadDoorbell()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20256`
  clears stream peer binding in `HomerServiceClearPayloadStreamPeerBinding()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20673`
  runs the current best-effort sender-side close in
  `TupleSinkServicePeerCloseSinkBestEffort()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21137`
  handles peer close requests in `TupleSinkServiceHandlePeerCloseSinkRequest()`.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:207`
  defines the current close request/response messages, which do not carry the
  payload token-pair identity, close flags, or final payload/head frontier.

Updated 3D-6 decision on 2026-06-23:

- Proceed with the full payload-close handshake now. The local final-credit
  guard is a useful safety repair, but it is not a protocol-level quiescence
  proof. It proves that the sender observed the receiver consume through the
  final published frontier; it does not prove that the receiver has stopped
  issuing WIMMs for the old token.
- Bind the close protocol to the already exchanged receiver-issued payload
  tokens, not to a one-sided local stream generation. `peerPayloadDoorbellToken`
  identifies the receiver-local payload/frontier binding being closed.
  `localPayloadDoorbellToken` identifies the reverse credit/head channel. Their
  embedded token generations reject stream-table reuse, and token-generation
  wrap already requires connection reset.
- Keep local `streamGeneration` in scheduler actions, close tickets, and
  reclaim checks. Do not put a one-sided local `streamGeneration` on the wire
  unless a later open-handshake extension also makes it a peer-validated
  identity.
- The close request and response must use the exact stream-bound connection/QP.
  Endpoint plus traffic-class lookup is not sufficient for normal close because
  the correctness proof depends on this same-QP order:

```text
sender:
    final payload WIMM
    CLOSE_SINK request

receiver:
    final consumed-head WIMM
    CLOSE_SINK response
```

  The recv-CQ dispatcher applies send-side credit/head WIMMs immediately, and
  exact control-mailbox execution semantically consumes the close response only
  after the control CQE/mailbox event is materialized. With the same RC QP, the
  sender therefore applies the final head update before accepting the quiesced
  close response.
- Normal close must be owned by a stream-local continuation driven by
  `HOMER_PROGRESS_ACTION_PAYLOAD_CLOSE_RECLAIM`. The peer-control request
  handler may update close state and report current status, but it must not
  directly reclaim a normal stream.
- `HomerPeerControlOpOwner` and retired-binding tombstones are acceptable. They
  are close/control lifetime metadata, not per-record payload hot-path work. The
  owner tag is consulted when a control op response completes. The tombstone is
  written on reclaim and used for duplicate-close/mismatch diagnostics; the
  successful WIMM fast path remains direct token decode plus stream-slot
  validation.

Close-1 - wire ABI and exact connection API:

- Bump `CITUS_REMOTE_EXEC_PEER_PROTOCOL_VERSION` to 16.
- Define close flags:

```c
#define HOMER_PAYLOAD_CLOSE_FLAG_NORMAL_DRAIN (1U << 0)
#define HOMER_PAYLOAD_CLOSE_FLAG_ABORT_RESET  (1U << 1)
#define HOMER_PAYLOAD_CLOSE_FLAG_QUIESCED     (1U << 2)
```

- Extend `CitusRemoteExecPeerCloseSinkRequest` with:

```c
uint32_t closeFlags;
uint32_t peerPayloadDoorbellToken;
uint32_t localPayloadDoorbellToken;
uint64_t finalPublishedTail;
```

- Extend `CitusRemoteExecPeerCloseSinkResponse` with:

```c
uint32_t closeFlags;
uint32_t peerPayloadDoorbellToken;
uint32_t localPayloadDoorbellToken;
uint64_t finalPublishedTail;
uint64_t finalConsumedHead;
```

- Add `TupleSinkServiceStartPeerRequestOnConnectionRdma()`. It must require an
  existing canonical connection, validate the exact connection generation,
  reserve and publish the control operation on that QP, never open or substitute
  another connection, and perform no hidden CQ or mailbox progress.

Close-1 implementation checkpoint on 2026-06-23:

- Citus commit `3e051525b` implements the v16 wire ABI and exact-connection
  control request substrate.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h`
  now bumps `CITUS_REMOTE_EXEC_PEER_PROTOCOL_VERSION` to 16, defines
  `HOMER_PAYLOAD_CLOSE_FLAG_NORMAL_DRAIN`,
  `HOMER_PAYLOAD_CLOSE_FLAG_ABORT_RESET`, and
  `HOMER_PAYLOAD_CLOSE_FLAG_QUIESCED`, and extends
  `CitusRemoteExecPeerCloseSinkRequest` / `CitusRemoteExecPeerCloseSinkResponse`
  with the token-pair identity and final-frontier fields.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h`
  adds `exactConnectionOnly` and `exactConnectionGeneration` to
  `TupleSinkServicePeerControlAsyncOp`, plus the public
  `TupleSinkServiceStartPeerRequestOnConnectionRdma()` API.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c`
  factors control-op reservation/publication into
  `TupleSinkServicePublishPeerControlAsyncOpOnConnection()`. Endpoint-routed
  async ops still use the existing traffic-class path. Exact-connection ops
  validate transport ownership, connection generation, and canonical readiness
  before publishing, and never perform endpoint lookup or setup progress.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  echoes the new close response fields in the current handler so the expanded
  v16 structs compile and remain well-formed before Close-2/Close-3 install the
  stream-owned continuation. This is not the final close semantics.
- Validation: no-stats `sudo -n -u dbcomm make -j8 service-bin client-bin
  CPPFLAGS='-D_GNU_SOURCE'` completed successfully. This stage intentionally did
  not install or run remote workloads because no runtime path starts using the
  exact close API yet.

Close-2 - persistent stream close state:

- Replace the ad hoc sender/receiver close booleans with a stream-owned close
  state:

```c
typedef enum HomerPayloadClosePhase
{
    HOMER_PAYLOAD_CLOSE_OPEN = 0,
    HOMER_PAYLOAD_CLOSE_LOCAL_TERMINAL,
    HOMER_PAYLOAD_CLOSE_REQUEST_READY,
    HOMER_PAYLOAD_CLOSE_REQUEST_INFLIGHT,
    HOMER_PAYLOAD_CLOSE_WAIT_PEER,
    HOMER_PAYLOAD_CLOSE_PEER_QUIESCED,
    HOMER_PAYLOAD_CLOSE_RECLAIMABLE,
    HOMER_PAYLOAD_CLOSE_ABORTING
} HomerPayloadClosePhase;
```

  The state carries blocked reasons, stream and connection generations, local
  and peer payload tokens, final published and consumed frontiers, final payload
  and final-head WIMM posted/retired/applied flags, a close async op, and a cold
  retry pass.
- Freeze `finalPublishedTail` exactly once after the producer is terminal and
  immutable EOS is posted.
- Normal `HomerServiceClearPayloadStreamPeerBinding()` must assert the stream is
  reclaimable. The only bypass is forced cleanup after the owning QP/CQ has been
  reset or destroyed.

Close-2 implementation checkpoint on 2026-06-23:

- Citus commit `913b9159d` installs the persistent close state in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
  `HomerPayloadStreamState.closeState` now snapshots the token pair, local stream
  generation, connection generation, final published/consumed frontiers, final
  payload/head WIMM flags, close-op storage, and blocked-reason mask.
- `HomerServiceBindPeerToPayloadStream()` resets the close state at bind time.
  `HomerServiceEnsurePayloadCloseStarted()` snapshots the close identity once
  and fails fast if a later close phase observes a different stream generation,
  connection generation, or token pair. `HomerServiceFreezePayloadCloseFinalTail()`
  records the immutable final tail and fails fast if the close path later tries
  to move it.
- `HomerServiceClearPayloadStreamPeerBinding()` now refuses normal active binding
  clear unless the close state is `RECLAIMABLE` or `ABORTING`. This is a
  deliberate fail-fast boundary for the old local close paths while Close-3 and
  Close-4 replace them with the real v16 peer-control handshake.
- The existing local final-credit guard is wired into the close state:
  `TupleSinkServicePeerCloseSinkBestEffort()` and the in-band byte-ring peer
  close path start/freeze close state, sender-side credit WIMMs mark final head
  observation, and `HomerServiceProgressPayloadCloseAndReclaim()` marks receiver
  streams reclaimable only after the final head ACK work is complete.
- This checkpoint is still not the complete close protocol. It intentionally
  keeps the binding alive until the local guard proves reclaimability, but it
  does not yet post exact `CLOSE_SINK` operations, validate echoed tokens/final
  frontiers, or drive sender retry from a stream-owned async continuation. Those
  remain Close-3 and Close-4.
- Validation:
  - no-stats `sudo -n -u dbcomm make -j8 service-bin client-bin
    CPPFLAGS='-D_GNU_SOURCE'` completed successfully;
  - installed and synced `/data/dbcomm/pg-citus` to `farnet0`;
  - warmed remote RDMA pgbench c1 completed `20000/20000` at `4364.701351 TPS`
    with p95 `0.239 ms`, p99 `0.250 ms`, max `6.169 ms`;
  - remote RDMA pgbench c4 completed `40000/40000` at `10661.779307 TPS` with
    p95 `0.519 ms`, p99 `0.597 ms`, max `16.189 ms`;
  - three warmed remote RDMA basebackup runs completed in `4.29`, `4.26`, and
    `4.18` seconds;
  - service-log scans on both hosts found zero occurrences of the binding
    mismatch, late-WIMM, stale-token, protocol, reset, fallback, or close
    fail-fast signatures.

Close-3 - sender continuation:

- Replace `TupleSinkServicePeerCloseSinkBestEffort()` with explicit sender
  progression under the payload close/reclaim action:

```text
freeze producer and final tail
post final payload WIMM
start exact CLOSE_SINK on the stream-bound QP
wait for typed control-op completion
retry coldly while the response says active
validate quiesced response token echo and final frontier
wait for local WR retirement, final remote head, and no ready/action reference
transition to RECLAIMABLE
```

- Add a peer-control async-op owner tag:

```c
typedef enum HomerPeerControlOpOwnerKind
{
    HOMER_PEER_CONTROL_OP_OWNER_NONE = 0,
    HOMER_PEER_CONTROL_OP_OWNER_PAYLOAD_CLOSE
} HomerPeerControlOpOwnerKind;
```

  The owner payload should be `kind/index/generation`. When the exact
  control-mailbox executor completes a response, it invokes one permanent
  service callback that validates the stream index/generation and marks the
  close continuation runnable. This is typed semantic materialization, not the
  rejected optional recv-CQ handler/FIFO design.

Close-3 implementation checkpoint on 2026-06-23:

- Citus commit `2ded30270` starts the exact sender-side normal-close handshake
  in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
- The sender now builds a v16 `CLOSE_SINK` request from the frozen close-state
  identity and posts it with
  `TupleSinkServiceStartPeerRequestOnConnectionRdma()` on the stream-bound
  connection and captured connection generation. The request carries the exact
  peer/local payload doorbell token pair and immutable final published frontier.
- `HomerServiceValidatePayloadCloseResponse()` validates the response status,
  peer stream ids, echoed token pair, echoed final tail, `QUIESCED` semantics,
  and `finalConsumedHead >= finalPublishedTail` before setting `peerQuiesced`.
  A response with `peerBindingStillActive=1` is treated as a cold retry, not as
  reclaim permission.
- Close/reclaim readiness now distinguishes owned from runnable close work:
  `HomerServicePayloadCloseActionReady()` returns runnable only when the exact
  close op is terminal or a bounded retry is due. In-flight response waits set
  `HOMER_PROGRESS_REASON_CLOSE_OR_RECLAIM_BLOCKED`, and
  `HomerServicePopulatePayloadMachineFacts()` advertises the peer recv-CQ,
  peer send-CQ, remote-credit, and maintenance collectors as possible unblockers
  without granting the close executor on every service pass.
- Scoped implementation choice: this checkpoint deliberately does not add the
  future global peer-control async-op owner callback. The stream-owned close
  action owns its preallocated async op and polls only terminal state. This keeps
  Close-3 local and avoids introducing a half-built generic callback surface;
  a typed owner callback remains available if later Close-4/Close-5 dependency
  wiring needs exact rearm without payload-stream scans.
- Receiver-side quiescence is still the existing Close-2 path: an active close
  request records deferred receiver close, the payload close/reclaim action posts
  and retires the final head ACK, clears the binding, and a later exact close
  retry receives a quiesced/already-gone response. Close-4 still needs to make
  receiver response semantics fully token/final-tail aware before this becomes
  the final protocol.
- Validation:
  - `git clang-format HEAD -- src/backend/distributed/utils/homer/tuple_sink_service_process.c`
    left the staged diff formatted;
  - no-stats `sudo -n -u dbcomm make -j8 service-bin client-bin
    CPPFLAGS='-D_GNU_SOURCE'` completed successfully;
  - installed and synced `/data/dbcomm/pg-citus` to `farnet0`;
  - remote RDMA pgbench c1 cold smoke completed `20000/20000` at
    `3043.973237 TPS` with the expected cold max-latency outlier;
  - warmed remote RDMA pgbench c1 completed `20000/20000` at `4417.024538 TPS`
    with p95 `0.237 ms`, p99 `0.253 ms`, max `5.583 ms`;
  - remote RDMA pgbench c4 completed `40000/40000` at `11071.484423 TPS` with
    p95 `0.499 ms`, p99 `0.575 ms`, max `15.247 ms`;
  - remote RDMA basebackup completed at `6.29`, `4.24`, and `4.24` seconds,
    where run 1 was the post-restart warmup;
  - service-log scans on both hosts found zero occurrences of the binding
    mismatch, late-WIMM, stale-token, protocol, reset, fallback, close response
    validation failure, close async poll failure, exact CLOSE_SINK publish
    failure, or close fail-fast signatures. The `farnet0` log showed the
    expected `peer-close deferred final head ACK` then `peer-close-drained`
    sequence for pgbench and basebackup receive streams.

Close-3 optimization required before Close-4:

- The current sender continuation still waits for
  `senderVisibleRemoteConsumedHead >= finalPublishedTail` before starting or
  completing the peer close. In current code this gate lives in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  inside `TupleSinkServicePeerCloseSinkBestEffort()` after
  `HomerServiceFreezePayloadCloseFinalTail()`.
- This gate is too strong for close-request publication. The normal close
  request should be sent after the sender has:
  - frozen the producer and final published frontier;
  - posted immutable EOS/final payload;
  - preserved the stream entry, token binding, source ownership, and connection
    generation snapshot.
- The sender-visible remote consumed head is still required before reclaiming
  the sender binding, but it must not block the first `CLOSE_SINK` request.
  Otherwise the sender can wait for final credit while the receiver waits for
  close to force the final-credit WIMM, creating a circular dependency.
- Implementation step:
  - move the `senderVisibleRemoteConsumedHead >= finalPublishedTail` test from
    the "may post/poll CLOSE_SINK" path to the final reclaim predicate;
  - allow `HomerServiceBuildPayloadCloseRequest()` and
    `TupleSinkServiceStartPeerRequestOnConnectionRdma()` to run once final
    payload/EOS is posted, even if final credit has not arrived yet;
  - keep `HOMER_PAYLOAD_CLOSE_BLOCK_REMOTE_HEAD` set while waiting for final
    credit, but do not let it suppress request publication;
  - retain the final reclaim check that the validated quiesced response and
    sender-visible remote head both cover the frozen final tail.
- Acceptance for this micro-stage:
  - receiver can force the final head WIMM in response to the first close
    request;
  - no sender-side close stalls when final credit is absent before the request;
  - remote pgbench c1/c4 and warmed remote basebackup remain in the Close-3
    validation band;
  - no binding mismatch, late-WIMM, stale-token, close response validation, or
    exact `CLOSE_SINK` publish failures appear in service logs.

Close-3 optimization checkpoint on 2026-06-24:

- Implemented in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`:
  - `TupleSinkServicePeerCloseSinkBestEffort()` now records
    `HOMER_PAYLOAD_CLOSE_BLOCK_REMOTE_HEAD` when final sender-visible credit has
    not arrived, but it continues into the exact `CLOSE_SINK` publish/poll path
    once the final tail is frozen.
  - The final `senderVisibleRemoteConsumedHead >= finalPublishedTail` check
    remains immediately before normal sender-side binding clear/reclaim.
  - `HomerServicePayloadCloseActionReady()` and
    `HomerServicePayloadStreamSourceReadyForScheduler()` now treat the
    post-quiescence remote-head wait as blocked close work, so a quiesced sender
    waiting only for the final credit WIMM does not become a hot-spin runnable
    payload source.
- Validation used no-stats Citus/Homer binaries, installed as `dbcomm`, synced
  to `farnet0`, and restarted both Homer services from a clean process baseline.
- Remote RDMA pgbench from `farnet0` to `farnet1`:
  - cold c1: `20000/20000`, zero failures, `3373.952915 TPS`, p99 `0.256 ms`,
    cold max `1319.741 ms`;
  - warmed c1: `20000/20000`, zero failures, `4313.164187 TPS`, p99
    `0.253 ms`, max `6.476 ms`;
  - c4: `40000/40000`, zero failures, `10828.590185 TPS`, p95 `0.520 ms`,
    p99 `0.600 ms`, max `16.459 ms`.
- Remote RDMA basebackup to the `farnet0` service passed four repeats:
  `5.85`, `4.28`, `4.15`, `4.28` seconds. Treat run 1 as warmup; warmed runs
  remain in the current `4.1`-`4.3` second band.
- Service-log scan on both hosts found no binding mismatch, late WIMM,
  stale-token, protocol/reset/fallback, payload-close response validation, or
  exact `CLOSE_SINK` publish failure signatures. The earlier Close-3 receiver
  still used a temporary deferred final-head ACK path; Close-4 below replaces
  that migration behavior with handler-side final-head posting and
  tombstone-backed duplicate handling.

Close-4 - receiver quiescence:

- Decision: use a hybrid ownership model.
  - The payload-close state machine owns the final-head transition.
  - The control request handler must advance or verify that transition before it
    returns `QUIESCED`.
  - Send-CQ retirement and stream reclamation remain owned by the payload
    close/reclaim action.
- The control handler may post the final consumed-head WIMM because the close
  request arrived on the exact stream-bound QP. It must not wait for the final
  head send CQE. Posting the final-head WIMM before the close response gives the
  same-QP ordering proof; the CQE is needed only for local source lifetime and
  reclaim.
- The old receiver sequence remains a temporary migration path only:

```text
close request
    -> return ACTIVE
payload action posts/retires final head
payload action clears binding
later retry sees missing stream and returns QUIESCED
```

  This sequence must not remain as the final protocol because it can reclaim the
  binding before the sender receives an identity-validated quiesced response, and
  "missing stream" is not a proof of quiescence unless it matches a retired
  tombstone.

Close-4A - receiver request validation:

- On an incoming normal close request, resolve by semantic IDs and validate:

```text
request.peerPayloadDoorbellToken == stream.localPayloadDoorbellToken
request.localPayloadDoorbellToken == stream.peerPayloadDoorbellToken
accepted connection handle/generation == stream-bound connection handle/generation
accepted traffic class == stream traffic class
final tail is not behind an already recorded close tail
```

- Repeated close requests with the same tokens and final tail are idempotent.
  Repeated requests with different tokens or final tail are protocol violations.
- Validate `localDirection`, request flags, normal-vs-abort mode, and the exact
  stream-bound connection before touching close state.
- Remove the existing "missing session/stream means quiesced" behavior from
  `TupleSinkServiceHandlePeerCloseSinkRequest()`. A missing active stream may
  return quiesced only after Close-4E adds a matching retired-binding tombstone.
  Until then, missing state is a protocol/lifetime error.
- Record the immutable close request identity in the stream close state. If a
  later request repeats the same identity, return the same logical state. If it
  changes the token pair, final tail, direction, or peer/local ids, mark the
  connection/stream failed.

Close-4B - idempotent final-head post transition:

- Add one shared helper, callable by both the control handler and the payload
  close/reclaim action:

```c
typedef enum HomerFinalHeadPostResult
{
    HOMER_FINAL_HEAD_NOT_DRAINED = 0,
    HOMER_FINAL_HEAD_BLOCKED,
    HOMER_FINAL_HEAD_POSTED,
    HOMER_FINAL_HEAD_ALREADY_POSTED,
    HOMER_FINAL_HEAD_FAILED
} HomerFinalHeadPostResult;

static HomerFinalHeadPostResult
HomerServiceTryPostFinalReceiverHead(
    HomerServicePayloadStreamEntry *streamEntry,
    TupleSinkServicePeerConnectionHandle *expectedConnection,
    uint64_t expectedConnectionGeneration,
    char *errorMessage,
    size_t errorMessageBytes);
```

- The helper must:
  - validate the captured token pair and connection generation;
  - require a recorded normal close request;
  - require EOS/final frontier visibility;
  - require `consumedHead >= finalPublishedTail`;
  - return `ALREADY_POSTED` if the final WIMM was already posted;
  - stop new ordinary thresholded head ACKs by setting
    `ordinaryHeadAcksStopped`;
  - allow already-posted ordinary ACK WRs to remain outstanding, since the final
    WIMM follows them on the same QP;
  - reserve the existing receiver-head ACK source/owner without blocking;
  - post a signaled final consumed-head `WRITE_WITH_IMM` on the exact stream
    connection;
  - set `finalHeadWimmPosted`, `finalConsumedHead`, and the close phase to
    final-head-posted;
  - never poll or wait for the send CQ.
- If source or owner credit is unavailable, set
  `HOMER_PAYLOAD_CLOSE_BLOCK_FINAL_HEAD_SEND_CQ` and return `BLOCKED`.
  The canonical send-CQ retirement path later clears the reason and rearms close
  work.
- The existing `HomerServicePostFinalReceiverHeadAckIfNeeded()` path is not
  sufficient for handler-side Close-4 because it can call through code that
  drains ACK completions under source pressure. The Close-4 helper must be
  nonblocking from the control-handler perspective: post if credit is available,
  otherwise return `BLOCKED`.

Close-4C - quiesced response boundary:

- `TupleSinkServiceHandlePeerCloseSinkRequest()` should follow this ownership
  sequence:

```text
1. Resolve active stream or matching retired tombstone.
2. Validate request identity and exact connection/QP.
3. Record immutable close request identity.
4. If receiver is not drained through final tail, return ACTIVE.
5. Try the final-head post transition.
6. Return ACTIVE for NOT_DRAINED or BLOCKED.
7. Return QUIESCED only for POSTED or ALREADY_POSTED.
8. Never reclaim the stream directly.
```

- The handler returns `QUIESCED` only when:

```text
finalHeadWimmPosted == true
ordinaryHeadAcksStopped == true
finalConsumedHead >= finalPublishedTail
```

- The handler must not require `finalHeadWimmRetired == true`.
- The handler prepares a response, but the transport posts it after the request
  callback returns. Therefore it must not set `quiescedResponsePosted` directly.
  Add a response-post ticket/result from the exact control-mailbox request path:

```c
typedef struct HomerPeerResponsePostTicket
{
    bool valid;
    HomerPeerControlOpOwnerKind kind;
    uint32_t streamIndex;
    uint64_t serviceStreamIdSnapshot;
    bool quiescedCloseResponse;
} HomerPeerResponsePostTicket;
```

- After the transport successfully posts the response on the accepted connection,
  validate the stream index and `serviceStreamIdSnapshot`, then set
  `quiescedResponsePosted`. If response publication fails, keep the stream and
  final-head state, do not mark the response posted, and enter the existing
  connection abort/reset path.
- Same-QP assertion: the accepted close-request connection must equal
  `streamEntry->stream.peerConnectionHandle`, its generation must equal the close
  state's connection generation, and its traffic class must match the stream's
  traffic class. The close response must be posted through that same accepted
  connection.

Close-4D - receiver reclaim gate:

- The payload close/reclaim action remains responsible for:
  - draining receive payload;
  - invoking `HomerServiceTryPostFinalReceiverHead()` after an earlier ACTIVE
    response once draining completes;
  - handling final-head source/CQ blocked state;
  - waiting for final-head send CQ retirement;
  - waiting for `quiescedResponsePosted`;
  - marking the stream reclaimable;
  - clearing the token/binding.
- Receive-side normal reclaim requires:

```text
peer close request recorded
EOS/final frontier observed
ring drained through finalPublishedTail
finalHeadWimmPosted
quiescedResponsePosted
finalHeadWimmRetired
ordinaryHeadAcksStopped
no payload-ready membership
no active executor/candidate reference
```

- `HomerServiceMarkPayloadCloseReclaimable()` must become a phase transition
  over already-proven facts. It must not synthesize facts such as
  `peerQuiesced` or `finalHeadObserved`; those are set only by validated
  responses or actual WIMM application.

Close-4E - retired-binding tombstone:

- Add one tombstone per stream-table slot outside the resettable live stream
  entry:

```c
typedef struct HomerRetiredPayloadBinding
{
    bool valid;

    uint64_t localServiceSessionId;
    uint64_t localServiceSinkId;
    uint64_t peerServiceSessionId;
    uint64_t peerServiceSinkId;

    uint32_t localPayloadDoorbellToken;
    uint32_t peerPayloadDoorbellToken;

    uint64_t serviceStreamIdSnapshot;
    uint64_t connectionGeneration;

    uint64_t finalPublishedTail;
    uint64_t finalConsumedHead;
} HomerRetiredPayloadBinding;
```

- Create the tombstone immediately before normal binding clear/reclaim.
- A close request for a missing active stream may return `QUIESCED` only if it
  matches the tombstone exactly. Missing state without a matching tombstone is a
  stale/invalid request and must fail.
- Late payload/credit WIMMs matching a tombstone remain fatal diagnostics. The
  tombstone explains the violation; it does not authorize the old RDMA write.

Close-4 tests:

```text
close arrives before drain:
    ACTIVE, no final head posted, binding retained
close arrives after drain:
    handler posts final head and same request returns QUIESCED
final-head source/owner credit unavailable:
    ACTIVE, send-CQ/resource relief rearms close
payload action drains after ACTIVE:
    action posts final head and next retry returns QUIESCED
QUIESCED response post fails:
    no reclaim, retry can reproduce QUIESCED or connection aborts safely
final head posted but not CQ-retired:
    QUIESCED may be returned, local stream cannot reclaim yet
duplicate close before reclaim:
    live close state returns identical response
duplicate close after reclaim:
    matching tombstone returns identical response
missing stream without tombstone:
    protocol error
old token after reclaim:
    fatal late-WIMM diagnostic
```

- Acceptance requires remote pgbench c1/c4, warmed remote basebackup, and
  concurrent pgbench-plus-basebackup to complete with zero late-WIMM,
  stale-token, missing-tombstone, close-response-post, or binding-clear
  fail-fast signatures.

Close-4 implementation progress and validation:

- Status as of June 24, 2026: Close-4A through Close-4E are implemented in the
  Citus/Homer tree on top of Citus commit `869f772f7` and are validated with
  no-stats binaries installed as `dbcomm` and synced to `farnet0`.
- Receiver request validation is in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21776`
  (`TupleSinkServiceHandlePeerCloseSinkRequest`). It validates the v16 token
  pair, direction, flags, final frontier, exact stream-bound connection, and
  traffic class before touching live close state.
- The transport response-post materialization boundary is in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6222`
  (`TupleSinkServiceProcessIncomingMailboxRequest`) and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:253`
  (`HomerPeerResponsePostTicket`). The request handler prepares a ticket, and
  the transport invokes the service callback only after response publication
  succeeds.
- `HomerServiceTryPostFinalReceiverHead()` at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26198`
  is now the shared nonblocking final-head transition used by both the exact
  close handler and the payload close/reclaim action. It posts a signaled final
  consumed-head WIMM on the exact stream-bound QP and never polls the send CQ.
- `HomerServiceHandlePeerResponsePostTicket()` at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22002`
  sets `quiescedResponsePosted` only after the exact control response publish
  succeeds, then rearms the close/reclaim action.
- Retired-binding tombstones are represented by `HomerRetiredPayloadBinding`
  and are stored outside the resettable stream entry. A missing active stream
  can answer a duplicate close request only when the token pair, semantic IDs,
  and final tail match the tombstone exactly; otherwise missing state is a
  protocol/lifetime error.
- Validation exposed three important implementation corrections:
  - Receive-side close was initially kept perpetually runnable after a close
    request was recorded. With small payload action budgets, stale close
    actions from earlier streams could starve later basebackup payload drain.
    `HomerServicePayloadCloseActionReady()` at
    `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20347`
    now separates close-owned from close-runnable on the receiver side.
  - The compatibility local-blackhole payload action could outrank close
    reclaim after a stream had already drained, leaving a quiesced receiver
    binding alive. `HomerServiceMachineBaselinePayloadActionKind()` now grants
    `HOMER_PROGRESS_ACTION_PAYLOAD_CLOSE_RECLAIM` before
    `HOMER_PROGRESS_ACTION_PAYLOAD_LOCAL_BLACKHOLE`.
  - The final-head send CQE could remain undiscovered when its posted frontier
    equaled the already-completed ordinary ACK frontier but
    `receiverHeadAckOutstandingCount` was still nonzero. Both
    `HomerServicePayloadSendCqMayHaveWork()` at
    `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11652`
    and `HomerServiceExecuteCqDrainProgressPlan()` at
    `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11969`
    now treat outstanding receiver-head ACK owners as send-CQ demand.
- Validation commands/results:
  - Focused repeated remote basebackup after clean restart:
    `5.88`, `4.18` seconds.
  - Warm remote c1 pgbench from `farnet0` to `farnet1`:
    `20000/20000`, zero failures, `4385.126353 TPS`, p99 `0.246 ms`.
  - Remote c4 pgbench:
    `40000/40000`, zero failures, `10844.566723 TPS`, p95 `0.521 ms`,
    p99 `0.607 ms`.
  - Four-run remote basebackup sequence:
    `4.30`, `4.24`, `4.20`, `4.18` seconds.
  - Concurrent c4 plus remote basebackup:
    pgbench `10000/10000`, zero failures, `9780.257182 TPS`, while
    basebackup completed in `4.26` seconds.
  - After removing repeated wait-loop debug prints, the final no-stats binary
    was rebuilt, reinstalled, synced, and rerun through a focused two-run
    basebackup smoke: `5.45`, `4.13` seconds.
- Receiver log evidence showed 17 successful close-clear cycles after the
  restart and mixed validation. The final sweep found no `lane send-CQ drain
  failed`, `without a matching tombstone`, `late`, `failed`, `error`, `stale`,
  `protocol`, or `payload close waiting` signatures on either host.

Close-4 current-code corrections applied while implementing:

- `closeState.streamGeneration` stored `serviceStreamId`, which implied a
  stronger allocation-generation proof than the code actually had. It has been
  renamed to `serviceStreamIdSnapshot` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1199`
  so the code matches the protocol model. The token pair remains the
  authoritative wire generation identity.
- Add receiver-side fields for the hybrid protocol, including:

```c
bool peerCloseRequestRecorded;
bool ordinaryHeadAcksStopped;
bool finalHeadWimmPosted;
bool finalHeadWimmRetired;
bool quiescedResponsePrepared;
bool quiescedResponsePosted;
```

- The exact enum names may reuse the existing close phases, but the semantics
  must distinguish: close received, draining, final-head postable, final-head
  posted, quiesced response posted, final-head retired, reclaimable, and
  aborting.

Close-5 - 3C owned/runnable dependency wiring:

- Add close blockage reasons:

```c
#define HOMER_PAYLOAD_CLOSE_BLOCK_FINAL_PAYLOAD_SEND_CQ (1U << 0)
#define HOMER_PAYLOAD_CLOSE_BLOCK_PEER_RESPONSE         (1U << 1)
#define HOMER_PAYLOAD_CLOSE_BLOCK_REMOTE_HEAD           (1U << 2)
#define HOMER_PAYLOAD_CLOSE_BLOCK_RECEIVER_DRAIN        (1U << 3)
#define HOMER_PAYLOAD_CLOSE_BLOCK_FINAL_HEAD_SEND_CQ    (1U << 4)
#define HOMER_PAYLOAD_CLOSE_BLOCK_READY_REFERENCE       (1U << 5)
```

- Maintain close-owned versus close-runnable state. No broad stream scan should
  rediscover close progress.
- Exact rearm hooks:

```text
payload WR retirement reaches final tail:
    payload send-CQ owner callback
sender receives final credit WIMM:
    HomerServiceApplyPayloadSenderCreditDoorbell()
receiver drains through final tail:
    payload executor
final head WIMM send CQE retires:
    receiver-head ACK completion callback
CLOSE_SINK response completes:
    control-op completion callback
cold retry interval expires:
    close retry scheduler fact
```

Close-5 implementation checkpoint:

- Status as of June 24, 2026: the normal-close owned/runnable wiring required
  by Close-5 is implemented as part of the Close-3 and Close-4 code.
- Close blockage reasons are defined in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1181`.
- Receiver-side close ownership is no longer advertised as runnable while the
  receive payload is still draining, while the exact control response has not
  been posted, or while final credit is waiting on the canonical recv-CQ path.
  The predicate is `HomerServicePayloadCloseActionReady()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20347`.
- Sender-side remote-head blocking is cleared by
  `HomerServiceApplyPayloadSenderCreditDoorbell()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22308`.
- Receiver final-head send-CQ blocking is exposed through exact send-CQ demand:
  both `HomerServicePayloadSendCqMayHaveWork()` and
  `HomerServiceExecuteCqDrainProgressPlan()` treat a nonzero
  `receiverHeadAckOutstandingCount` as work, even when the byte frontier equals
  the already-completed ordinary ACK frontier.
- Exact close-response publication rearms close/reclaim through
  `HomerServiceHandlePeerResponsePostTicket()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22002`.
- The cold retry interval remains on the sender-side `WAIT_PEER` path; it
  suppresses hot retry after an active close response.

Close-5 residual normal-close audit:

- The initial `CLOSE_SINK` request is no longer gated on already observing the
  final remote consumed head. `HomerServiceProgressPayloadCloseAndReclaim()`
  can start the exact stream-bound close request after freezing the final
  published tail; the sender-visible final head remains a reclamation
  prerequisite rather than a request-publication prerequisite.
- Pre-Close-6 cleanup completed on June 24, 2026: `HomerServiceMarkPayloadCloseReclaimable()`
  in `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20323`
  no longer sets `peerQuiesced` or `finalHeadObserved` while marking a stream
  reclaimable. It now fails fast unless the side-specific facts were already
  established by their real events:
  - `peerQuiesced` by a validated `QUIESCED` close response;
  - `finalHeadObserved` by an actual sender-side credit WIMM or equivalent
    receiver-side final-head fact.
- Diagnostic counters for blackhole priority hardening were added under
  `HOMER_SERVICE_PROGRESS_STATS`:
  `blackholeActionsSelectedAfterDrain` and
  `blackholeActionsSelectedAfterQuiescedResponse`. These should normally be
  zero; the current close-before-blackhole priority rule should not conceal
  stale blackhole readiness.
- Validation for this cleanup:
  - no-stats compile passed;
  - `HOMER_SERVICE_PROGRESS_STATS=1` compile passed;
  - repeated remote basebackup after service restart: `6.09`, `4.18` seconds;
  - remote pgbench c1: `5000/5000`, zero failures, p99 `0.256 ms` with a cold
    first-connection max-latency outlier;
  - remote pgbench c4: `10000/10000`, zero failures, `10599.800300 TPS`,
    p99 `0.614 ms`;
  - service-log sweep found no `refusing to mark`, late-WIMM, stale, protocol,
    tombstone, or send-CQ failure signatures.

Close-6 - reclamation and abort cleanup:

- Local send stream normal reclaim requires: close phase `PEER_QUIESCED`, final
  payload/EOS posted, all local payload WRs through final tail retired,
  sender-visible remote consumed head at least final tail, quiesced response
  `finalConsumedHead` at least final tail, close op inactive, no payload-ready
  membership, and no active executor/candidate for the stream generation.
- Local receive stream normal reclaim requires: peer close requested, EOS
  observed, ring drained through requested final tail, final head WIMM posted,
  final head WIMM send CQE retired, no more ordinary head WIMMs permitted, no
  payload-ready membership, and no active executor/candidate for the stream
  generation.
- Abort/error close after protocol mismatch, connection loss, partial multi-WR
  post, final-tail inconsistency, or control-op failure must reset/destroy the
  affected QP/CQ before invalidating tokens and MRs. QP teardown is the proof
  that no old-generation RDMA write or WIMM can arrive.
- Close-4E owns the retired-binding tombstone implementation because duplicate
  close after normal reclaim cannot be made correct without it. Close-6 should
  keep only the broader cleanup around normal reclaim and abort/error teardown:
  ensure the tombstone is populated immediately before normal clear, ensure late
  WIMMs matching the tombstone remain fatal diagnostics, and ensure abort paths
  reset/destroy the owning QP/CQ before invalidating tokens or MRs.
- Implementation-readiness caveat: normal reclaim and tombstones are now
  implemented, but abort/error teardown is not yet implementation-ready from
  this note alone. Current payload code generally marks
  `payloadTransportBroken` on protocol or transport errors, while
  `TupleSinkServiceResetPeerConnection()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4228`
  tears down the transport slot and states that callers must separately repair
  semantic state. There is not yet a service-owned reverse index from a peer
  connection to all payload streams bound to that connection, nor a single
  connection-led abort owner that can:
  - mark every affected stream as `HOMER_PAYLOAD_CLOSE_ABORTING`;
  - remove payload-ready membership and close/runnable facts;
  - reset or destroy the affected QP/CQ as the old-WIMM proof;
  - only then invalidate payload tokens, MRs, and stream bindings.
- Do not implement Close-6 as another local `payloadTransportBroken` branch.
  First decide the connection-to-stream ownership/index shape and the exact
  entry point that connection reset calls before or during QP teardown.

Close-6 ownership decision:

- Do not add a persistent connection-to-stream reverse index. Use the stream
  table as the authoritative ownership source because reset is a cold path,
  `CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS` is currently `64U`, and every
  active stream already carries:

```text
peerConnectionHandle
peerConnectionGeneration
```

- On connection reset, scan the fixed stream table and select entries matching
  both the connection handle and generation. Use a reset-time snapshot bitset:

```c
uint64_t affectedStreamBits;
```

- Add a static assertion that the fixed stream-table capacity is at most 64
  while this cookie is represented as one `uint64_t`. If the table grows later,
  replace the cookie with a fixed word array instead of truncating the snapshot.
- The transport treats the bitset as an opaque semantic cookie: the service
  computes it during reset-begin, and the transport returns it during
  reset-complete after QP/CQ teardown.

Close-6 lifecycle ABI:

- Keep this separate from `HomerPeerRecvDispatcher`, which remains the
  steady-state CQE decoder. Add a connection lifecycle observer to
  `TupleSinkServicePeerTransportState`:

```c
typedef struct HomerPeerConnectionIdentity
{
    TupleSinkServicePeerConnectionHandle *handle;

    bool incoming;
    uint32_t connectionIndex;
    uint64_t connectionGeneration;

    HomerTransportTrafficClass trafficClass;
    int32_t peerNodeId;
} HomerPeerConnectionIdentity;

typedef enum HomerPeerConnectionResetReason
{
    HOMER_PEER_RESET_CM_DISCONNECT = 1,
    HOMER_PEER_RESET_RECV_CQ_FAILURE,
    HOMER_PEER_RESET_SEND_CQ_FAILURE,
    HOMER_PEER_RESET_PAYLOAD_PROTOCOL,
    HOMER_PEER_RESET_PARTIAL_POST,
    HOMER_PEER_RESET_CLOSE_PROTOCOL,
    HOMER_PEER_RESET_SERVICE_SHUTDOWN
} HomerPeerConnectionResetReason;

typedef struct HomerPeerConnectionLifecycleObserver
{
    uint64_t (*onResetBegin)(
        const HomerPeerConnectionIdentity *identity,
        HomerPeerConnectionResetReason reason,
        void *context);

    void (*onResetComplete)(
        const HomerPeerConnectionIdentity *identity,
        HomerPeerConnectionResetReason reason,
        uint64_t semanticCookie,
        const HomerPeerConnectionAbortReport *abortReport,
        void *context);

    void *context;
} HomerPeerConnectionLifecycleObserver;
```

- `onResetBegin()` must not fail or invoke transport progress. For the current
  service, it returns `affectedStreamBits`.
- `onResetComplete()` runs only after QP/CQ teardown proves no old WIMM or RDMA
  write can arrive.

Close-6 transport reset ordering:

- Refactor `TupleSinkServiceResetPeerConnection()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4228`
  so abort reset follows this order:

```text
1. Snapshot connection identity.
2. Remove connection from scheduler facts; set recv-CQ owner TEARDOWN,
   bootstrapComplete=false, resetInProgress=true.
3. Invoke onResetBegin(); service marks matching streams ABORTING but keeps
   tokens, MRs, bindings, and owner counts.
4. Best-effort rdma_disconnect(); do not wait for DISCONNECTED.
5. Destroy the QP. This is the old-WIMM/old-RDMA-write cutoff.
6. Destroy or abandon CQs; no normal CQ owner callback runs afterward.
7. Abort-clear transport-private software owners and build a
   `HomerPeerConnectionAbortReport`.
8. Deregister caller-owned and connection-owned MRs.
9. Release PD and remaining CM resources.
10. Invoke onResetComplete() with the semantic cookie and abort report.
11. Zero/reuse the connection table slot.
```

- Split the current resource cleanup into explicit helpers:

```c
TupleSinkServiceDestroyConnectionQp();
TupleSinkServiceDestroyConnectionCqs();
TupleSinkServiceInvalidateRegisteredMemoryRegions();
TupleSinkServiceReleaseConnectionMemoryResources();
TupleSinkServiceReleaseConnectionCmResources();
```

- The important ordering correction is that QP destruction precedes MR
  deregistration on abort/reset paths. The current reset path invalidates
  registered MRs before `TupleSinkServiceReleaseConnectionResources()`, which is
  not the right proof when outstanding WRs may still reference those MRs.

Close-6 reset-begin service callback:

- Scan the fixed stream table and set one bit for every active stream where:

```text
stream.peerConnectionHandle == identity.handle
stream.peerConnectionGeneration == identity.connectionGeneration
```

- For every matching stream:

```text
closeState.phase = HOMER_PAYLOAD_CLOSE_ABORTING
payloadTransportBroken = true
remove payload-ready membership
clear receivePayloadDoorbellPending
clear close-runnable state
cancel future CLOSE_SINK retries
prevent new payload/head/control posts
retain tokens, MR handles, connection identity, and outstanding owner counts
```

- Do not call `HomerServiceClearPayloadStreamPeerBinding()` from reset-begin.

Close-6 reset-complete service callback:

- Iterate only the reset snapshot. Revalidate that each stream still matches the
  captured connection generation before mutating it. Reset-begin and
  reset-complete are synchronous around one connection slot, so a mismatch is an
  invariant failure, not a stale bit that can be silently ignored.
- Use teardown-specific owner cleanup, not normal CQ callbacks:

```c
HomerServiceAbortPayloadSendOwnersForConnection();
HomerServiceAbortReceiverHeadAckOwnersForConnection();
```

- Abort-remove payload send owners, receiver-head ACK owners, pending final-head
  owners, and source-slot/source-range reservations. Decrement exact owner
  counts exactly once.
- Validate any stream-owned close async op against the transport abort report:
  the op must be exact-connection-only, must name the reset connection handle
  and generation, and its op index/generation must appear in
  `abortedControlOpBits` and `abortedControlOpGenerations[]`. Then clear the
  stream-local `closeOp` handle and `closeOpActive` flag. Do not expose
  `TupleSinkServiceReleasePeerControlOp()` publicly and do not poll the op.
- Response-post tickets are not persistent ownership. The transport
  abort-clears persistent response publication slots; the service clears
  per-stream response-post semantic flags such as
  `quiescedResponsePrepared`/`quiescedResponsePosted`. There is no
  response-ticket table to abort.
- Require:

```text
receiverHeadAckOutstandingCount == 0
no payload send owner remains
no close op remains active
```

- Close-6D ends with the stream still in `ABORTING`: `abortOwnersReconciled`
  is true, binding/token identity remains available, all physical/software
  owners have been removed, and normal success facts are not synthesized.
  Close-6F records the abort tombstone, fails/cancels the owning operation,
  clears invalidated MR references, clears the peer binding through the
  ABORTING-authorized path, and reclaims local stream storage. Do not advance
  semantic payload frontiers as though WRs completed.

Close-6D hybrid ownership decision:

- Payload accounting is service-owned. The transport only decodes the tagged WR
  ID and calls:

```text
payloadCompletion(completionKind, serviceSinkId, completedFrontier)
```

  Normal service callbacks such as `HomerServiceApplyTrackedPayloadCompletionFrontier()`
  and `HomerServiceApplyReceiverHeadAckCompletionFrontier()` mean successful NIC
  completion and must not be invoked during reset cleanup.
- Control-op slots and response publication slots are transport-owned because
  they live inside the peer connection state. Reset cleanup must bulk-abort
  them inside the transport, then report exactly what was removed to the
  service.
- Add one aggregate transport-private abort helper that runs after QP/CQ
  destruction:

```c
typedef struct HomerPeerConnectionAbortReport
{
    uint64_t abortedControlOpBits;
    uint32_t abortedControlOpGenerations[
        CITUS_REMOTE_EXEC_PEER_CONTROL_OP_SLOTS];

    uint64_t abortedResponsePublishSlotBits;

    uint32_t waitingControlOpsAborted;
    uint32_t completedControlOpsDiscarded;
    uint32_t failedControlOpsDiscarded;
    uint32_t responsePublishSlotsAborted;

    bool transportSoftwareOwnersCleared;
} HomerPeerConnectionAbortReport;

static void
TupleSinkServiceAbortConnectionSoftwareOwnersOnReset(
    TupleSinkServicePeerConnectionState *connection,
    HomerPeerConnectionAbortReport *report);
```

- Add static assertions that the fixed control-op table and response-publish
  slot table fit in the report bitsets. If either table grows beyond 64 entries,
  convert the report to fixed word arrays instead of truncating.
- Control-op reset handling:
  - `UNUSED`: no action.
  - `WAIT_RESPONSE`, `COMPLETED`, `FAILED`: record index and generation,
    classify/count the old phase, and clear the transport slot without
    delivering semantic success.
  - A `COMPLETED` op that was not yet consumed by its owner is discarded because
    connection reset invalidates the operation.
- Response publication reset handling:
  - for every in-use `controlResponsePublishSlots[]` entry, record the bit,
    clear `inUse`, clear message/tail storage, and do not invoke the
    response-post callback.

Close-6D service owner states:

- Add explicit owner state to tracked payload send and receiver-head ACK owner
  entries:

```c
typedef enum HomerTrackedWrOwnerState
{
    HOMER_TRACKED_WR_OWNER_FREE = 0,
    HOMER_TRACKED_WR_OWNER_POSTED,
    HOMER_TRACKED_WR_OWNER_RETIRED,
    HOMER_TRACKED_WR_OWNER_ABORTED
} HomerTrackedWrOwnerState;
```

- Normal send-CQ retirement performs `POSTED -> RETIRED`.
- Reset-complete performs `POSTED -> ABORTED`.
- `FREE`, `RETIRED`, and `ABORTED` entries must not decrement owner counts
  again. Double-retire or count mismatch is an invariant failure.

Close-6D payload send owner abort:

```c
static bool
HomerServiceAbortTrackedPayloadSendOwners(
    HomerServicePayloadStreamEntry *stream,
    const HomerPeerConnectionIdentity *identity,
    HomerPayloadAbortSummary *summary,
    char *error,
    size_t errorBytes);
```

- For every posted payload owner belonging to the reset connection generation:
  mark it `ABORTED`, release local source-slot/source-range ownership, decrement
  `payloadCompletionCount`, remove it from the completion FIFO/ring, and
  increment `payloadSendOwnersAborted`.
- Do not call `HomerServiceApplyTrackedPayloadCompletionFrontier()`.
- Do not update `payloadSenderCompletedTail`,
  `payloadSourceByteCompletedHead`, semantic completed frontier, or
  `finalPayloadSendRetired`. Those are success facts and retain their pre-reset
  diagnostic values.

Close-6D receiver-head ACK owner abort:

```c
static bool
HomerServiceAbortReceiverHeadAckOwners(
    HomerServicePayloadStreamEntry *stream,
    const HomerPeerConnectionIdentity *identity,
    HomerPayloadAbortSummary *summary,
    char *error,
    size_t errorBytes);
```

- For every outstanding ordinary or final head-ACK owner: transition
  `POSTED -> ABORTED`, release its source slot, decrement
  `receiverHeadAckOutstandingCount`, remove the entry from the ACK owner queue,
  and increment `receiverHeadOwnersAborted`.
- Do not call `HomerServiceApplyReceiverHeadAckCompletionFrontier()`.
- Do not advance `receiverHeadAckCompletedHead` or set
  `finalHeadWimmRetired`.
- At completion, require `receiverHeadAckOutstandingCount == 0` and no ACK owner
  entry remains `POSTED`. This preserves the equal-frontier case where ordinary
  and final ACKs publish the same head value but remain distinct physical WR
  owners.

Close-6D abort summary:

```c
typedef struct HomerPayloadAbortSummary
{
    uint32_t payloadSendOwnersAborted;
    uint32_t receiverHeadOwnersAborted;
    uint32_t sourceReservationsReleased;
    uint32_t closeOpsCancelled;

    bool allPayloadOwnersCleared;
    bool allHeadOwnersCleared;
    bool closeControlStateCleared;
} HomerPayloadAbortSummary;
```

- After each stream reset-complete cleanup:

```text
payloadCompletionCount == 0
receiverHeadAckOutstandingCount == 0
no payload owner is POSTED
no ACK owner is POSTED
closeOpActive == false
```

- Set `closeState->abortOwnersReconciled = true`.
- Do not mark `peerQuiesced`, `finalHeadObserved`,
  `finalPayloadSendRetired`, or `finalHeadWimmRetired`.

Close-6 exact reset scheduling:

- Do not destroy a QP from inside a recv-CQ decoder or send-CQ owner callback.
  Add:

```c
bool TupleSinkServiceRequestPeerConnectionResetRdma(
    TupleSinkServicePeerConnectionHandle *connection,
    uint64_t expectedGeneration,
    HomerPeerConnectionResetReason reason);
```

- This sets exact reset-pending state. A close/lifetime scheduler action later
  performs reset:

```c
typedef struct HomerPeerConnectionResetAction
{
    bool incoming;
    uint32_t connectionIndex;
    uint64_t connectionGeneration;
    HomerPeerConnectionResetReason reason;
} HomerPeerConnectionResetAction;
```

- Prioritize reset ahead of further work on the affected connection.
- Convert these failures to reset requests: live payload token/connection
  generation mismatch, consumed-head regression or head beyond posted tail,
  partial multi-WR publication, payload/control send-CQ failure, final-tail
  inconsistency, close token/frontier mismatch, `QUIESCED` response publication
  failure after final-head post, late WIMM matching a retired binding, and owner
  table corruption.
- A normal backend/query error with valid transport should use `ERROR+EOS` and
  normal close, not QP reset.

Close-6 tombstone disposition:

- Extend tombstones with disposition:

```c
typedef enum HomerRetiredPayloadDisposition
{
    HOMER_RETIRED_PAYLOAD_QUIESCED = 1,
    HOMER_RETIRED_PAYLOAD_ABORTED_RESET = 2
} HomerRetiredPayloadDisposition;
```

- Only `HOMER_RETIRED_PAYLOAD_QUIESCED` may answer a duplicate close request
  with `QUIESCED`. `HOMER_RETIRED_PAYLOAD_ABORTED_RESET` is diagnostic only.

Close-6 patch sequence:

1. Close-6A - lifecycle ABI:
   add connection identity, reset reason enum, lifecycle observer, and service
   observer registration; no semantic behavior change.
2. Close-6B - safe transport teardown:
   add `resetInProgress`, reorder QP/CQ destruction before MR deregistration,
   and call reset-begin/reset-complete observers while preserving identity.
3. Close-6C - payload reset snapshot:
   implement the bounded stream-table scan, return `affectedStreamBits`, mark
   matching streams ABORTING/non-runnable, and remove ready-queue membership
   without clearing binding.
4. Close-6D - teardown-owned owner cleanup:
   use the hybrid cleanup model: transport bulk-aborts control ops and response
   publication slots into `HomerPeerConnectionAbortReport`; service aborts
   payload send and receiver-head ACK owners from `affectedStreamBits`, validates
   stream-owned close ops against the report, clears response-post semantic
   flags, reconciles counts, and leaves binding/token identity intact for
   Close-6F.
5. Close-6E - exact reset scheduling:
   add reset-request API and exact reset action; convert protocol/partial-post
   failures to reset requests; prevent recursive reset inside CQ dispatch.
6. Close-6F - tombstone and session failure:
   add normal-versus-abort tombstone disposition, publish terminal local failure
   where applicable, reclaim streams sharing the failed connection, and remove
   remaining ad hoc `payloadTransportBroken` cleanup branches.

Close-6A implementation checkpoint:

- Status as of June 24, 2026: Close-6A is implemented in the Citus/Homer tree.
- Added `HomerPeerConnectionIdentity`,
  `HomerPeerConnectionResetReason`, and
  `HomerPeerConnectionLifecycleObserver` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h`.
- Extended `TupleSinkServiceCreatePeerTransportState()` to accept the lifecycle
  observer, and stored the copied observer in
  `TupleSinkServicePeerTransportState`.
- Registered service-side reset-begin/reset-complete callbacks from
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
  These callbacks are intentionally no-ops in Close-6A: reset-begin returns
  cookie `0`, reset-complete does nothing, and no reset ordering or semantic
  cleanup behavior changes in this stage.
- Validation: no-stats Citus/Homer `service-bin client-bin` build completed
  successfully with `CPPFLAGS='-D_GNU_SOURCE'`.
- Next stage remains Close-6B. It must be the first behavior-changing reset
  patch: add `resetInProgress`, snapshot connection identity, call the observer
  around teardown, and reorder QP/CQ destruction before MR invalidation.

Close-6B implementation checkpoint:

- Status as of June 24, 2026: Close-6B is implemented in the Citus/Homer tree.
- Added `resetInProgress` to the peer connection state and made
  `TupleSinkServiceResetPeerConnection()` reject recursive reset attempts with a
  diagnostic instead of reentering teardown.
- Added a reset identity snapshot helper and lifecycle observer begin/complete
  calls around the physical teardown boundary. The service callbacks still
  return/do nothing until Close-6C adds the payload-stream snapshot.
- Split the previous all-in-one connection cleanup into explicit teardown
  phases:
  - `TupleSinkServiceDestroyConnectionQp()`;
  - `TupleSinkServiceDestroyConnectionCqs()`;
  - `TupleSinkServiceInvalidateRegisteredMemoryRegions()`;
  - `TupleSinkServiceReleaseConnectionMemoryResources()`;
  - `TupleSinkServiceReleaseConnectionCmResources()`.
- The reset path now sets recv-CQ ownership to `TEARDOWN`, clears
  `bootstrapComplete`, sets `resetInProgress`, invokes reset-begin, optionally
  performs best-effort disconnect, destroys QP/CQs, and only then invalidates
  registered MRs and releases connection-owned memory/CM resources.
- Temporary caveat: reset call sites still pass diagnostic strings. Close-6B
  maps those strings to `HomerPeerConnectionResetReason` for the lifecycle
  observer; Close-6E should replace this with exact reset requests carrying the
  typed reason directly.
- Validation:
  - no-stats Citus/Homer `service-bin client-bin` build completed with
    `CPPFLAGS='-D_GNU_SOURCE'`;
  - installed and synced `/data/dbcomm/pg-citus` to `farnet0`;
  - restarted Homer services on `farnet1` and `farnet0`;
  - remote RDMA basebackup smoke completed in `5.41s` cold and `4.22s` warmed;
  - service-log scan found no reset recursion, stale/late payload token,
    tombstone, protocol, send-CQ, or close-wait diagnostics.
- Next stage remains Close-6C: implement the reset-begin payload stream-table
  scan, return `affectedStreamBits`, mark matching streams `ABORTING`, and
  remove ready/runnable memberships without clearing bindings.

Close-6C implementation checkpoint:

- Status as of June 24, 2026: Close-6C is implemented in the Citus/Homer tree.
- Added a static assertion that `CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS`
  fits in the current one-word reset cookie.
- Implemented reset-begin stream matching by exact
  `peerConnectionHandle` plus `peerConnectionGeneration`.
- `HomerServicePeerConnectionResetBegin()` now scans the fixed payload stream
  table, marks every matching active peer-bound stream `ABORTING`, removes its
  payload-ready queue membership, clears pending receive-doorbell readiness,
  prevents future normal close retries/posts by clearing local/received close
  pending flags, and returns `affectedStreamBits` to the transport as the
  reset-complete cookie.
- ABORTING streams now report transport-broken blocked state but are not
  scheduler-runnable payload sources. This is intentional: Close-6D must consume
  the reset cookie and do teardown-owned owner cleanup after QP/CQ teardown,
  rather than letting the normal payload executor race against reset-begin.
- The marker preserves peer binding, token identity, MR handles, and outstanding
  owner counts. It snapshots close identity if the stream had not already
  entered the close state machine.
- Validation:
  - no-stats Citus/Homer `service-bin client-bin` build completed with
    `CPPFLAGS='-D_GNU_SOURCE'`;
  - installed and synced `/data/dbcomm/pg-citus` to `farnet0`;
  - restarted Homer services on `farnet1` and `farnet0`;
  - remote RDMA basebackup smoke completed in `6.02s` cold and `4.22s` warmed;
  - service-log scan found no reset-begin captured-stream logs during the
    normal path and no reset recursion, stale/late payload token, tombstone,
    protocol, send-CQ, or close-wait diagnostics.
- Next stage remains Close-6D. It must consume the `affectedStreamBits` cookie in
  reset-complete, use the transport abort report for control/response
  publication state, abort-retire payload send owners and receiver-head ACK
  owners, validate/clear stream-owned close ops, clear response-post semantic
  flags, and keep the binding intact for Close-6F.

Close-6D pre-implementation note:

- Decision as of June 24, 2026: implement Close-6D with the hybrid ownership
  model above. Do not implement it by simply zeroing stream counters.
- Current payload send-CQ retirement path:
  - `TupleSinkServiceHandleTaggedSendCompletion()` in
    `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c`
    decodes payload WR IDs and invokes the service `payloadCompletion` callback.
  - `HomerServiceApplyTrackedPayloadCompletionFrontier()` and
    `HomerServiceApplyReceiverHeadAckCompletionFrontier()` in
    `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
    update source-release, semantic, and ACK outstanding counters from those
    callbacks.
  - After Close-6B reset ordering, reset-complete runs after QP/CQ destruction,
    so no normal send-CQ callback can arrive for the affected connection.
- Current control ownership path:
  - `TupleSinkServiceReleasePeerControlOp()` clears transport op slots, but it is
    private to the peer transport implementation.
  - `HomerPeerResponsePostTicket` is a stack-local ticket in
    `TupleSinkServiceProcessIncomingMailboxRequest()`: it is reported after a
    response publish succeeds, but there is no persistent response-ticket owner
    table for reset-complete to cancel.
- Accepted 6D split:
  - transport bulk-aborts transport-private `controlOps[]` and
    `controlResponsePublishSlots[]` after QP/CQ destruction and before MR
    invalidation, then passes `HomerPeerConnectionAbortReport` to
    reset-complete;
  - service abort-reconciles payload send owners and receiver-head ACK owners
    from `affectedStreamBits`;
  - service validates and clears stream-owned close-op handles against the
    transport report;
  - service clears response-post semantic flags, but there is no persistent
    response-post ticket table.
- Required invariant for the accepted 6D design: abort cleanup must not advance
  source or semantic frontiers as though RDMA WRs completed successfully, must
  decrement each outstanding owner count exactly once, must leave no active
  close op or response-publication ownership for the stream, and must keep the
  peer binding/token identity intact until Close-6F records the abort tombstone
  and performs ABORTING-authorized clear.

Close-6D recommended commit sequence:

1. Close-6D1 - transport owner report:
   add `HomerPeerConnectionAbortReport`; abort-clear control-op and response
   publication slots after QP/CQ destruction; pass the report into
   reset-complete; no service owner cleanup yet.
2. Close-6D2 - explicit service owner states:
   add `HomerTrackedWrOwnerState`; convert normal payload and ACK CQ callbacks
   to use `POSTED -> RETIRED`; preserve successful frontier semantics.
3. Close-6D3 - payload abort cleanup:
   abort payload send owners and source reservations from `affectedStreamBits`;
   do not advance completed frontiers; assert exact count reconciliation.
4. Close-6D4 - ACK and close-op cleanup:
   abort ordinary/final ACK owners; validate close op against the transport
   abort report; clear stream-owned close-op handle and response semantic flags.
5. Close-6D5 - reconciled ABORTING state:
   set `abortOwnersReconciled`, keep binding intact for Close-6F, add counters
   and deterministic reset tests.

Close-6D counters:

```text
transportControlOpsAbortCleared
transportResponseSlotsAbortCleared
payloadSendOwnersAbortRetired
receiverHeadOwnersAbortRetired
payloadSourceReservationsAbortReleased
payloadCloseOpsAbortCancelled
payloadAbortOwnerDoubleRetire
payloadAbortOwnerCountMismatch
```

Close-6D1 implementation checkpoint:

- Status as of June 24, 2026: Close-6D1 is implemented in the Citus/Homer tree
  at commit `efe62c574`.
- Added `HomerPeerConnectionAbortReport` and moved the fixed control-op and
  response-publication slot counts into
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h`.
- Extended `HomerPeerConnectionLifecycleObserver.onResetComplete()` to receive
  the transport abort report. The service callback in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  currently accepts and ignores the report; later 6D sub-stages consume it when
  validating stream-owned close ops.
- Added the transport-private
  `TupleSinkServiceAbortConnectionSoftwareOwnersOnReset()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c`.
  It clears `controlOps[]` in `WAIT_RESPONSE`, `COMPLETED`, or `FAILED` phase,
  records the slot generation and phase class in the report, clears in-use
  `controlResponsePublishSlots[]`, and never invokes response-post callbacks or
  semantic success paths.
- `TupleSinkServiceResetPeerConnection()` now calls this helper after QP/CQ
  destruction and before MR invalidation, then passes the report into
  reset-complete. This preserves the ordering proof: old WR completions are cut
  off before transport-private software owners are force-cleared.
- Validation: no-stats Citus/Homer `service-bin client-bin` build completed
  successfully with `CPPFLAGS='-D_GNU_SOURCE'`.
- Next stage is Close-6D2: add explicit service-side tracked-WR owner states and
  make normal payload/receiver-head send-CQ retirement transition
  `POSTED -> RETIRED` before adding abort transitions.

Close-6D2 implementation checkpoint:

- Status as of June 24, 2026: Close-6D2 is implemented in the Citus/Homer tree
  at commit `4e77598db`.
- Added `HomerTrackedWrOwnerState` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  with `FREE`, `POSTED`, `RETIRED`, and `ABORTED` states.
- Byte-ring payload completion tracking now stores a parallel
  `payloadCompletionOwnerStates[]` entry. `HomerServiceTrackPayloadCompletion()`
  sets `POSTED`, and `HomerServiceCompleteTrackedPayload()` requires `POSTED`
  before changing the owner to `RETIRED`. It still advances
  `payloadSourceByteCompletedHead`, `payloadSenderCompletedHead`, and progress
  deltas only through the existing successful CQ path.
- Receiver consumed-head ACKs now have an explicit FIFO:
  `receiverHeadAckTails[]`, `receiverHeadAckOwnerStates[]`,
  `receiverHeadAckHeadIndex`, and `receiverHeadAckTailIndex`. This fixes the
  modeling gap where two ACK WRs can carry the same byte frontier but still need
  distinct source-lifetime retirements.
- `HomerServiceTrackReceiverHeadAckOwner()` records one `POSTED` owner before
  posting the one-WR ACK. `HomerServiceRollbackLastReceiverHeadAckOwner()` rolls
  that reservation back if the post helper fails. `HomerServiceRetireReceiverHeadAckOwners()`
  retires FIFO owners on successful send-CQ callbacks before
  `receiverHeadAckCompletedHead` is updated.
- Validation: no-stats Citus/Homer `service-bin client-bin` build completed
  successfully with `CPPFLAGS='-D_GNU_SOURCE'`.
- Next stage is Close-6D3: use the explicit payload owner states to abort
  posted payload send owners from `affectedStreamBits` during reset-complete,
  release local source reservations, and preserve successful-completion
  frontiers as diagnostics.

Close-6D3 implementation checkpoint:

- Status as of June 24, 2026: Close-6D3 is implemented in the Citus/Homer tree
  at commit `c26432f1c`.
- Added `HomerPayloadAbortSummary` and
  `HomerServiceAbortTrackedPayloadSendOwners()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
- `HomerServicePeerConnectionResetComplete()` now consumes the
  `affectedStreamBits` cookie from reset-begin and, for each captured stream,
  revalidates the live stream against the copied connection identity before
  touching payload send-owner state. A mismatch is logged as an invariant
  failure rather than treated as a stale bit.
- Outstanding byte-ring payload send owners transition `POSTED -> ABORTED`.
  Their WR counts are subtracted from `payloadCompletionOutstandingWrs`, their
  entries are removed from the completion FIFO, and the backend/source
  byte-ring `consumedHead` is advanced to release local source storage for the
  failed stream.
- The abort path deliberately does not call
  `HomerServiceApplyTrackedPayloadCompletionFrontier()` and does not update
  `payloadSourceByteCompletedHead`, `payloadSenderCompletedHead`,
  `finalPayloadSendRetired`, or semantic completed frontiers. Those fields
  remain successful-completion diagnostics.
- Validation: no-stats Citus/Homer `service-bin client-bin` build completed
  successfully with `CPPFLAGS='-D_GNU_SOURCE'`.
- Next stage is Close-6D4: abort receiver-head ACK owners, validate and clear
  stream-owned close async ops using `HomerPeerConnectionAbortReport`, and clear
  per-stream response-post semantic flags.

Close-6D4 implementation checkpoint:

- Status as of June 24, 2026: Close-6D4 is implemented in the Citus/Homer tree
  at commit `982ab6783`.
- Renamed `HomerPeerResponsePostTicket.streamGeneration` to
  `serviceStreamIdSnapshot` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h`
  and updated the close response-post handler in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
- Added `HomerServiceAbortReceiverHeadAckOwners()`. Reset-complete now
  transitions outstanding ordinary/final receiver-head ACK owners
  `POSTED -> ABORTED`, decrements `receiverHeadAckOutstandingCount`, and leaves
  `receiverHeadAckCompletedHead` and `finalHeadWimmRetired` unchanged because
  those are successful send-CQE facts.
- Added `HomerServiceAbortPayloadCloseControlOp()`. If a stream-owned close op
  was published, reset-complete requires the exact connection handle,
  generation, op index, and op generation to match the
  `HomerPeerConnectionAbortReport` before clearing the service handle. If the
  close op was active but not yet published, it has no transport slot and is
  cleared locally.
- Reset-complete clears `quiescedResponsePrepared` and
  `quiescedResponsePosted` for aborting streams. A quiesced response that was
  prepared or even posted before reset no longer authorizes normal reclaim once
  the stream enters `ABORTING`.
- Validation: no-stats Citus/Homer `service-bin client-bin` build completed
  successfully with `CPPFLAGS='-D_GNU_SOURCE'`.
- Next stage is Close-6D5: mark owner reconciliation complete, add final
  assertions/counters, keep binding intact for Close-6F, and run real
  cross-machine validation.

Close-6D5 implementation checkpoint:

- Status as of June 24, 2026: Close-6D5 is implemented in the Citus/Homer tree
  at commit `e24ea66c8`.
- Added cold-path abort reconciliation counters in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`:
  `PayloadSendOwnersAbortRetired`, `ReceiverHeadOwnersAbortRetired`,
  `PayloadSourceReservationsAbortReleased`, `PayloadCloseOpsAbortCancelled`,
  and `PayloadAbortOwnerCountMismatch`.
- Added `abortOwnersReconciled` to `HomerPayloadCloseState`. Reset-begin sets
  it false when a stream enters `ABORTING`; reset-complete sets it true only
  after all service-owned payload, receiver-head ACK, and close-op owners have
  been reconciled.
- Added `HomerServicePayloadAbortOwnersAreReconciled()` as the final
  reset-complete invariant check. It requires the abort summary flags to be
  true, `payloadCompletionCount == 0`,
  `payloadCompletionOutstandingWrs == 0`,
  `receiverHeadAckOutstandingCount == 0`, `closeOpActive == false`, and no
  payload or ACK owner entry still in `POSTED`.
- The reconciliation checkpoint still deliberately keeps the stream binding and
  token identity intact. Close-6D ends with `phase == ABORTING` and
  `abortOwnersReconciled == true`; Close-6F remains responsible for recording
  an `ABORTED_RESET` tombstone, failing/canceling the owning operation, and
  clearing the binding through the ABORTING-authorized path.
- Validation: no-stats Citus/Homer `service-bin client-bin` build completed
  successfully with `CPPFLAGS='-D_GNU_SOURCE'`. The committed binaries were
  installed as `dbcomm`, synced to `farnet0`, and both services were restarted.
  Remote RDMA basebackup completed twice after the restart: run 1 was the cold
  warmup at `5.48s`, and run 2 was the warmed result at `4.24s`.
- Post-validation service-log scan on both `farnet1` and `farnet0` found no
  reset-begin/reset-complete, owner-reconciliation, abort, late-WIMM,
  tombstone, stale, protocol, mismatch, error, failed, or send-CQ drain-failure
  diagnostics in the validation run.
- Operational note: service restarts should avoid `pkill -f
  citus_tuple_sink_service` inside a shell command that also contains the
  service path; the pattern can match the invoking shell. Use an exact process
  name or split cleanup and start into separate commands.
- Next stage is Close-6E: add typed reset requests and exact reset actions so
  payload/control protocol failures request reset from the current collector or
  callback stack and a scheduler-owned lifetime action performs the reset.

Close-6E1 implementation checkpoint:

- Status as of June 24, 2026: the first Close-6E slice is implemented in the
  Citus/Homer tree at commit `264ef0ce3`.
- Added `HomerPeerConnectionResetAction` and the public reset scheduling API in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h`:
  `TupleSinkServiceRequestPeerConnectionResetRdma()`,
  `TupleSinkServiceAppendPendingPeerConnectionResetActionsRdma()`, and
  `TupleSinkServiceExecutePeerConnectionResetActionRdma()`.
- Added per-connection `resetRequested` plus `pendingResetReason`, and
  transport-level `pendingConnectionResetCount`. A reset request records the
  first typed `HomerPeerConnectionResetReason`; repeated requests with a
  different reason are diagnostic-only and do not overwrite the original poison
  reason.
- Split the reset implementation into a typed internal reset path and the
  existing string-based compatibility wrapper. Exact reset actions execute the
  typed path, while old setup/lifetime callers still use the wrapper until the
  rest of 6E migrates them.
- Added exact reset action materialization to the service scheduler in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
  Pending resets are copied into durable `HomerProgressActionPayload` storage
  and scheduled before critical recv-CQ and control-mailbox actions. Older exact
  recv/control actions skip a connection once `resetRequested` is set.
- Migrated the first dangerous reset sites:
  - canonical recv-CQ drain failure now requests `HOMER_PEER_RESET_RECV_CQ_FAILURE`;
  - async CM disconnect/error events now request `HOMER_PEER_RESET_CM_DISCONNECT`;
  - peer send-CQ drain failure in the peer pump now requests
    `HOMER_PEER_RESET_SEND_CQ_FAILURE`.
- Normal peer-pump table scans now skip `resetRequested` connections after
  counting them as active for listener cadence. This prevents ordinary setup,
  recv-CQ, CM, or send-CQ work from running on a slot whose teardown is owned by
  the reset action.
- Validation: formatted with `git clang-format HEAD -- ...`; no-stats
  Citus/Homer `service-bin client-bin` build completed with
  `CPPFLAGS='-D_GNU_SOURCE'`; installed as `dbcomm`, synced to `farnet0`, and
  restarted both Homer services. Remote RDMA basebackup completed twice after
  restart: run 1 cold `5.98s`, run 2 warmed `4.20s`.
- Post-validation service-log scan on both hosts found no reset request,
  reset-begin/reset-complete, owner-reconciliation, abort, late-WIMM, stale,
  protocol, mismatch, tombstone, error, failed, or send-CQ drain-failure
  diagnostics in the validation run.
- Remaining Close-6E work: migrate protocol/partial-post failures and the
  remaining direct string-based reset call sites where reset can be requested
  from a callback or owner path. Setup/startup compatibility reset calls may stay
  direct until their lifecycle is reviewed, but CQ decoders, send-owner
  callbacks, response-post paths, and payload/control protocol checks must use
  the typed request path.

Close-6E2 implementation checkpoint:

- Status as of June 24, 2026: the second Close-6E migration slice is
  implemented in the Citus/Homer tree at commit `89dd81125`.
- Migrated async peer-control request publication failure in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c`
  from inline `TupleSinkServiceResetPeerConnection()` to
  `TupleSinkServiceRequestPeerConnectionResetRdma(...,
  HOMER_PEER_RESET_PARTIAL_POST)`. The transport still releases the reserved
  op slot immediately because the request was not durably published, but QP/CQ
  teardown is now scheduler-owned.
- Migrated payload doorbell binding mismatch in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  to request `HOMER_PEER_RESET_PAYLOAD_PROTOCOL`. This preserves the exact
  protocol poison reason before the recv-CQ collector observes the callback
  failure.
- Migrated invalid CLOSE_SINK response validation on the sender-side payload
  close continuation to request `HOMER_PEER_RESET_CLOSE_PROTOCOL` through the
  stream's captured peer connection handle/generation.
- Remaining direct string-based reset calls after this slice are setup/startup
  compatibility paths: incoming accept failure, outgoing async connect start or
  bootstrap progress failure, and setup progress failure inside the broad peer
  pump. They are not CQ decoder or send-owner callback resets; keep them direct
  until their setup lifecycle is reviewed.
- Validation: formatted with `git clang-format HEAD -- ...`; no-stats
  Citus/Homer `service-bin client-bin` build completed with
  `CPPFLAGS='-D_GNU_SOURCE'`; installed as `dbcomm`, synced to `farnet0`, and
  restarted both Homer services. Remote RDMA basebackup completed twice after
  restart: run 1 cold `6.38s`, run 2 warmed `4.14s`.
- Post-validation service-log scan on both hosts found no reset request,
  reset-begin/reset-complete, owner-reconciliation, abort, late-WIMM, stale,
  protocol, mismatch, tombstone, error, failed, or send-CQ drain-failure
  diagnostics in the validation run.
- Remaining Close-6E work is now mostly audit/fault-injection: confirm no new
  CQ decoder, send-owner callback, response-post path, or payload/control
  protocol path calls inline reset; then add deterministic reset-request tests
  for stale generation, double request, and reset action execution after slot
  reuse.

Close-6F1 implementation checkpoint:

- Status as of June 24, 2026: the first Close-6F slice is implemented in the
  Citus/Homer tree at commit `3f8472333`.
- This slice deliberately scoped Close-6F to the tombstone/clear-boundary work
  that was already specified and locally checkable. It did not attempt the
  broader operation/session failure publication or remove every
  `payloadTransportBroken` branch, because those paths still need a separate
  failure-surfacing design rather than another ad hoc cleanup branch.
- `HomerRetiredPayloadBinding` now carries an explicit
  `HomerRetiredPayloadDisposition`:

```c
typedef enum HomerRetiredPayloadDisposition
{
    HOMER_RETIRED_PAYLOAD_QUIESCED = 1,
    HOMER_RETIRED_PAYLOAD_ABORTED_RESET = 2
} HomerRetiredPayloadDisposition;
```

- Duplicate `CLOSE_SINK` requests may be answered from a retired tombstone only
  when the disposition is `HOMER_RETIRED_PAYLOAD_QUIESCED` and the existing
  semantic IDs, token pair, and final tail match. An
  `HOMER_RETIRED_PAYLOAD_ABORTED_RESET` tombstone is diagnostic-only and cannot
  certify quiescence.
- The tombstone field formerly named `streamGeneration` has been renamed to
  `serviceStreamIdSnapshot`, matching what the field actually stores. The
  exchanged payload token pair remains the wire generation identity.
- `HomerServicePayloadCloseCanClearBinding()` now treats `ABORTING` as clearable
  only after reset-complete has set `abortOwnersReconciled` and all stream-owned
  payload/ACK/control owners are gone. `ABORTING` alone is no longer a binding
  clear proof.
- `HomerServicePeerConnectionResetComplete()` now records an
  `ABORTED_RESET` tombstone after owner reconciliation, publishes local terminal
  state for receiver-side streams, clears the peer binding through the guarded
  abort path, and then lets `TupleSinkServiceMaybeReclaimSinkAndSession()`
  reclaim the local stream/session if no local handle remains.
- Added cold-path counters:
  `PayloadAbortTombstonesRecorded` and `PayloadBindingsAbortCleared`.
- Validation: formatted with `git clang-format HEAD -- ...`; `git diff
  --cached --check` passed; no-stats Citus/Homer `service-bin client-bin` build
  passed with `CPPFLAGS='-D_GNU_SOURCE'`; installed as `dbcomm`, synced to
  `farnet0`, and restarted both Homer services.
- Remote RDMA basebackup after restart completed twice: run 1 cold `5.30s`,
  run 2 warmed `4.20s`.
- Remote RDMA pgbench c4:
  - first post-restart run was treated as warmup/diagnostic because it had a
    large connection outlier and reported `7959.354759 TPS`;
  - warmed rerun completed `40000/40000` transactions with zero failures,
    `10719.035587 TPS`, p95 `0.525 ms`, p99 `0.619 ms`.
- Post-validation service-log scan on both hosts found no reset request,
  reset-begin/reset-complete, owner-reconciliation, abort, late-WIMM, stale,
  protocol, mismatch, tombstone, non-quiesced-retired-binding, error, failed,
  or send-CQ drain-failure diagnostics.
- After committing, the no-stats service/client binaries were rebuilt,
  installed, and synced again so installed artifacts carry `GIT_VERSION`
  `homer-state-machine-scheduler-milestone(sha: 3f8472333)`.
- Remaining Close-6F work: define how a transport-aborted payload stream
  publishes terminal failure to its owning DB/client operation, remove or
  replace the remaining local `payloadTransportBroken` cleanup branches once
  that failure-surfacing model exists, and add fault-injection coverage for
  abort tombstone matching and duplicate close after reset.

Close-6 later-slice dependencies:

- Close-6D can land before exact reset scheduling, but do not add new call sites
  that destroy a QP from inside recv-CQ decoding, send-CQ owner callbacks, or
  response-post callbacks. Close-6E owns typed reset requests and exact reset
  actions.
- The current string-based reset reason classifier is temporary. Close-6E must
  replace it with typed reset requests carrying `HomerPeerConnectionResetReason`
  directly.
- Close-6F owns abort tombstone disposition, operation/session failure
  publication, invalidated stream-MR reference cleanup, ABORTING-authorized
  binding clear, and removal of remaining ad hoc `payloadTransportBroken`
  cleanup branches.
- Close-6F1 completed abort tombstone disposition and ABORTING-authorized
  binding clear. Operation/session failure publication and removal of the
  remaining ad hoc `payloadTransportBroken` cleanup branches are still future
  Close-6F work.
- `HomerRetiredPayloadBinding.serviceStreamIdSnapshot` and
  `HomerPeerResponsePostTicket.serviceStreamIdSnapshot` should stay named as
  snapshots, not stream generations; the wire generation identity remains the
  exchanged payload token pair.

Close-6 fault-injection and acceptance:

```text
two payload streams share one QP; one stream detects protocol error:
    both enter ABORTING and one QP reset cleans both
reset with outstanding final-head ACK owner:
    owner abort-retired exactly once
reset with ordinary and final ACKs carrying same frontier:
    outstanding owner count still reaches zero
reset with payload send WRs outstanding:
    no semantic frontier is advanced as completed
reset with Close-3 async op in flight:
    close op cancelled by teardown owner
reset after final-head WIMM post before quiesced-response post:
    abort tombstone only, no duplicate QUIESCED response
reset after quiesced-response post before final-head send CQ retirement:
    owner cleanup retires local lifetime before clear
partial payload batch post:
    accepted WRs are handled by reset, not normal completion
stream ready-queue entry exists during reset:
    ready membership removed before binding clear
connection slot immediately reused:
    stale reset action/cookie cannot target new generation
late old-token WIMM after claimed reset cutoff:
    impossible or fatal diagnostic
normal close and 20 sequential basebackups:
    use quiesced path, no reset
concurrent c4 plus basebackup with forced connection reset:
    no old-token WIMM reaches reused stream
```

- Add counters:

```text
peerConnectionResetRequests
peerConnectionResetBegins
peerConnectionResetCompletes
payloadStreamsMarkedAborting
payloadSendOwnersAbortRetired
receiverHeadOwnersAbortRetired
payloadCloseOpsAbortCancelled
payloadBindingsAbortCleared
payloadAbortTombstonesRecorded
lateWimmAfterQpReset
connectionResetStreamSnapshotMismatch
```

3D-6 local close/credit repair checkpoint on 2026-06-23:

- Implemented the local lifetime part of Patch A/B/D in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
  The full peer-control protocol-v16 close handshake is still pending; this
  checkpoint deliberately does not claim 3D-6 complete until validation confirms
  the local guard is sufficient and the remaining wire-level close work is
  either implemented or explicitly deferred.
- Added sender-side close-frontier fields to `HomerPayloadStreamState`:
  `payloadCloseFinalTail` and `payloadCloseFinalHeadObserved`. These are reset
  on bind and clear, and protect normal sender-side binding invalidation.
- Added `HomerServicePayloadSenderFinalTail()` and changed
  `TupleSinkServicePeerCloseSinkBestEffort()` so a local send stream does not
  call `HomerServiceClearPayloadStreamPeerBinding()` merely because EOS payload
  WRs have locally completed. It now keeps `localPeerClosePending` set until the
  sender has observed `senderVisibleRemoteConsumedHead >= payloadCloseFinalTail`.
  This directly targets the observed late reverse consumed-head/credit WIMMs.
- Split `TupleSinkServiceDispatchPeerPayloadDoorbell()` by stream direction:
  local receive streams still enqueue exact payload readiness, while local send
  streams call `HomerServiceApplyPayloadSenderCreditDoorbell()` to acquire-load
  the peer-written sender-head mirror immediately, validate monotonicity and
  posted-tail bounds, update `senderVisibleRemoteConsumedHead`, and mark the
  close final-head condition when applicable. A send-side credit WIMM is no
  longer converted into a generic payload-ready queue entry.
- Added `HomerServicePostFinalReceiverHeadAckIfNeeded()` on the receive-side
  deferred close path. The normal credit path is thresholded for payload
  throughput, but close now forces the final consumed-head WIMM before the
  existing `HomerServiceDrainReceiverHeadAckCompletions()` wait and before
  `HomerServiceClearPayloadStreamPeerBinding()`.
- Changed `TupleSinkServiceHandlePeerCloseSinkRequest()` so even an already
  drained receive stream does not clear/reclaim directly. It marks
  `receivedPeerClosePending` and returns `peerBindingStillActive = 1`; the
  payload close/reclaim action remains the owner of final ACK publication,
  send-CQ retirement, and normal binding clear.
- Caveat: this checkpoint still lacks the planned v16 close request/response
  fields (`peerPayloadDoorbellToken`, `localPayloadDoorbellToken`,
  `finalPublishedTail`, `finalConsumedHead`, close flags), exact-connection
  control request publication, and a sender-owned `CLOSE_SINK`
  retry/confirmation continuation. The next step is not another grace period or
  scan fallback; it is the Close-1 through Close-6 protocol plan above.
- Validation for this checkpoint:
  - No-stats Citus/Homer build and install completed as `dbcomm`, then the
    installed prefix was synced to `farnet0` and both Homer services were
    restarted.
  - Remote RDMA basebackup ran three times. Run 1 was the cold/warmup run at
    `6.04 s`; warmed repeats were `4.14 s` and `4.16 s`.
  - Targeted service-log scans on both hosts found no
    `payload doorbell binding mismatch`, stale-token, protocol, reset,
    fallback, or generic error signatures after the basebackup and pgbench
    validation runs.
  - Remote RDMA pgbench after truncating `pgbench_history` and vacuum/analyzing
    the pgbench tables:
    - c4: `40000/40000`, 0 failures, `10631.605051 TPS`, p95 `0.502 ms`,
      p99 `0.567 ms`.
    - c1 first run had a 2-second max-latency outlier and only
      `3028.157168 TPS`, so it was treated as diagnostic. Immediate rerun:
      `20000/20000`, 0 failures, `4292.642304 TPS`, p95 `0.243 ms`,
      p99 `0.254 ms`.
  - Post-run process preflight showed only the intended PostgreSQL service on
    `farnet1` and one Homer service on each host.

3D-6 residual broad-mailbox cleanup checkpoint on 2026-06-23:

- Implemented the previously planned residual broad-mailbox cleanup in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.[ch]`.
  The cleanup deletes the obsolete broad request/response mailbox source,
  collector, action, wait-mask, registry, feedback, and stats-name vocabulary.
  `HOMER_PROGRESS_ACTION_DRAIN_PEER_CONTROL_MAILBOX` remains the only semantic
  control-mailbox executor.
- `TupleSinkServicePeerPumpGrant` no longer carries `maxMailboxMessages`, and
  `TupleSinkServicePumpPeerRequestsRdma()` no longer accepts a request handler or
  handler context. The broad pump is now explicitly transport-only: setup,
  listener/CM, canonical recv-CQ liveness, send-CQ retirement, and close/lifetime
  progress. Exact mailbox actions still pass
  `TupleSinkServiceDispatchPeerRequest()` directly to
  `TupleSinkServiceDrainPeerControlMailboxRdma()`.
- The local-control wait mask for peer responses now names only the remaining
  physical prerequisites (`HOMER_PROGRESS_WAIT_PEER_RECV_CQ` and
  `HOMER_PROGRESS_WAIT_PEER_SEND_CQ`). Control-mailbox readiness is a typed
  indexed fact, not a broad peer-pump wait source.
- Zero-reference audit for the removed names returned no matches in the touched
  Homer transport/service files:
  `PEER_REQUEST_MAILBOX`, `PEER_RESPONSE_MAILBOX`,
  `COLLECT_PEER_REQUEST_MAILBOX`, `COLLECT_PEER_RESPONSE_MAILBOX`,
  `WAIT_PEER_REQUEST_MAILBOX`, `WAIT_PEER_RESPONSE_MAILBOX`,
  `peerRequestMailbox`, `peerResponseMailbox`, and `maxMailboxMessages`.
- Validation for this checkpoint:
  - No-stats `service-bin client-bin` build passed before and after
    `git clang-format`; install completed as `dbcomm`; the installed prefix was
    synced to `farnet0`; PostgreSQL on `farnet1` and both Homer services were
    restarted.
  - Remote RDMA pgbench c1 smoke: `20000/20000`, 0 failures, `3043.965825 TPS`,
    p95 `0.238 ms`, p99 `0.251 ms`, with a large one-time max-latency outlier.
  - Remote RDMA pgbench c4: `40000/40000`, 0 failures, `10791.086455 TPS`,
    p95 `0.524 ms`, p99 `0.616 ms`.
  - Remote RDMA basebackup runs: cold/warmup `5.95 s`, warmed repeats `4.24 s`
    and `4.17 s`.
  - Targeted service-log scans on both hosts found no `ERROR`, `FATAL`,
    payload doorbell binding mismatch, late-WIMM, stale-token, protocol, reset,
    fallback, or generic failure signatures after validation.

Required acceptance before 3D-6 is complete:

```text
close request before EOS drain:
    response active=1
    stream remains bound

final head WIMM delayed:
    sender does not reclaim

exact control action before payload action:
    no reclaim

final head WIMM arrives:
    send-side head is applied in recv-CQ dispatcher
    close retry returns inactive

immediate stream-table slot reuse:
    no old-generation RDMA/WIMM reaches new stream

20 sequential remote basebackups over persistent connection:
    zero stale-token events

connection abort with active stream:
    QP reset precedes token invalidation

concurrent c4 plus basebackup:
    zero stream binding mismatches
```

Add counters:

```text
payloadCloseStarted
payloadCloseRequestsPosted
payloadCloseActiveResponses
payloadCloseQuiescedResponses
payloadCloseRetries
payloadCloseRetryDeferrals
payloadFinalHeadPosted
payloadFinalHeadRetired
payloadFinalHeadApplied
payloadCloseBlockedSendCq
payloadCloseBlockedRemoteHead
payloadCloseBlockedReceiverDrain
payloadCloseBlockedControlResponse
payloadCloseNormalReclaims
payloadCloseAbortResets
payloadLateWimmAfterQuiescence
```

Acceptance requires `payloadLateWimmAfterQuiescence` to remain zero.

#### 3D Validation

Acceptance:

```text
control WIMM sets controlMailboxReady and the indexed ready bit
candidate construction does not clear readiness
executor clears the bit only after a stable empty recheck
budget exhaustion leaves the bit armed
connection reset/teardown invalidates stale generation candidates
outgoing REQUEST is rejected as protocol error
incoming REQUEST and incoming RESPONSE both progress through the exact executor
async peer-control poll does not consume mailboxes or CQs
broad outgoing RESPONSE_MAILBOX and incoming REQUEST_MAILBOX branches are gone
machine-baseline grants at most two control-mailbox actions per pass initially
zero active-connection scan is used for control mailbox discovery
zero semantic fallback mailbox scans remain
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
| Slice 3A    | Landed; persistent pending plus load-before-exchange optimization validated; sharding deferred |
| Slice 3B    | R0 through R6 landed and validated; remaining owned/runnable generalization moves into 3C |
| Slice 3C    | Completion owned/runnable rearm correctness validated; remaining signaled-owner FIFO counts are performance cleanup |
| Slice 3D    | 3D-0 through 3D-6 exact-control cleanup, Close-1 through Close-3, the Close-3 remote-head gate optimization, and Close-4A through Close-4E receiver quiescence/tombstone are implemented and validated |
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
