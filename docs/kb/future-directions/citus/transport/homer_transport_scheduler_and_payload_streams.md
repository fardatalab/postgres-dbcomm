# Homer transport scheduler and neutral payload streams

## Scope

- **What this doc explains**: the future design for Homer service progress,
  RDMA egress scheduling, neutral payload-stream state, push-style command
  completions, traffic classes, multiple QPs, and large-object fragmentation.
- **What this doc does NOT cover**: implementing a complete basebackup receiver,
  changing PostgreSQL backup consistency semantics, or exposing a user-facing
  scheduling API.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`
- **Doc type**: `future-direction`

## Grounding

The current standalone service loop in
[`main()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8885)
hardcodes the order of service progress:

1. local control slots through
   [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8689)
2. peer-control RDMA mailbox progress through
   [`TupleSinkServicePumpPeerConnections()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5794)
3. local socketless-backend completions through
   [`TupleSinkServiceConsumeAllCompletionMailboxes()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2429)
4. every active payload state through
   [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6882)

That is a service-progress loop, not yet a scheduler.

The RDMA egress points that need real scheduling are lower:

- peer-control publication through
  [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2563)
- payload range publication through
  [`TupleSinkServicePostPeerRegisteredPayloadBatchWithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3486)
- sender payload batching in
  [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6069),
  especially the open-ended loop at
  [`tuple_sink_service_process.c:6288`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6288)
  and the current per-call cap through
  `HOMER_SERVICE_PAYLOAD_SEND_COMPLETION_BATCH` at
  [`tuple_sink_service_process.c:6366`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6366)

The current peer transport also shares send-CQ machinery between control writes
and payload write completions. The payload WR id tagging comment in
[`remote_execution_peer_transport_rdma.c:657`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:657)
is a useful signal: control and payload already contend for some of the same
transport resources.

## Local control slots, peer control, and completions

Local control slots are a local control-plane command queue between local actors
and the Homer service. The actors may be PostgreSQL backends, modified frontend
clients such as pgbench, or PostgreSQL backend code using the frontend-safe Homer
client library. The queue is
[`CitusRemoteExecControlRegion`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:549),
which owns fixed
[`CitusRemoteExecControlSlot`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:539)
entries. Request kinds are defined in
[`CitusRemoteExecControlRequestKind`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:56):
open session, close session, report post-command state, start command, and poll
command completion.

Peer control is the service-to-service equivalent. Its request vocabulary is
[`CitusRemoteExecPeerRequestKind`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_peer_control_protocol.h:40):
peer open, close sink, start command, poll command completion, and open command
session. The transport implementation currently publishes those fixed-width
messages over a persistent RDMA control mailbox; the one-message-at-a-time
mailbox rule is documented in
[`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2563).

Backend completions are the local socketless-backend-to-service completion path.
The service publishes exactly one command record into a session command mailbox
through
[`TupleSinkServicePublishLocalCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2576),
and later consumes the backend's completion mailbox through
[`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2273).

## Target Peer-Control Substrate

The target design is a multi-slot peer-control substrate, not a short-term
serialization shim around the current one-message mailbox. This is required for
concurrent client sessions and for later transaction plus base-backup
interference experiments.

The current limitation is explicit in
[`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4021):
the transport assumes at most one outstanding request per peer connection and
uses `waitingForResponse` in
[`remote_execution_peer_transport_rdma.c`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4107)
to reject overlap. That shape cannot support concurrent command-session opens,
responder-initiated completions, or independent traffic classes cleanly.

The target substrate should provide:

- multi-slot RDMA-registered peer-control rings, replacing the single fixed
  mailbox slot described by
  [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2563)
- compact fixed-width control records containing at least request kind,
  session id, request sequence/id, response-routing metadata, and payload length
  or inline payload fields
- per-session logical control queues so each remote execution session has an
  ordered control stream
- response/completion demultiplexing by request id and session id rather than
  by "the only outstanding request on this connection"
- a high-priority control traffic class that cannot be blocked behind bulk base
  backup payload work

Logical queues and physical RDMA resources must remain decoupled. A per-session
logical queue is the semantic abstraction; one QP per session is only one
possible mapping policy and should not be baked into the public/internal
session abstraction. The scheduler should be able to map logical queues onto:

- one shared control QP
- one control QP per traffic class
- a QP pool per traffic class
- a per-session QP policy for experiments

Performance constraints:

- do not allocate queue nodes on the hot path
- do not copy control messages into intermediate heap buffers
- reserve a ring slot and write the fixed-width control object directly into
  the RDMA-registered slot
- publish with tail update plus doorbell after the slot is fully written
- let the scheduler operate over descriptors such as queue id, ring index,
  traffic class, priority, and byte/WR budget; the command/control object itself
  should stay in place
- keep optional cold metadata out of the normal hot record when possible

This design is also the bridge to multiple QPs. Multiple QPs should be treated
as a physical transport/scheduling policy under the logical queues, not as the
thing that defines session semantics.

## Poll completion is a scaffold, not the target command-completion design

The current local and peer protocols expose `POLL_COMMAND_COMPLETION` because
`START_COMMAND` can return before the database command reaches a terminal state.
For example, sink-backed COPY ingest may need to report `STARTED` so payload can
flow and only later report `COMPLETED` or `FAILED`.

This is analogous to the old libpq state machine in the sense that callers can
send a command, observe that work is in progress, and later fetch results. But
it is not the ideal Homer command-completion abstraction. A pull/poll path adds
extra request-response exchanges and can add latency for short commands if the
caller has to ask again for terminal state.

The target design is push completion, using small fixed-size completion rings
even while the local frontend prototype keeps one in-flight command per session.
That nuance matters: one SQL command can publish multiple visible states
(`STARTED` before sink metadata, `STARTED` with sink-ready metadata, and terminal
completion), so a single slot or two-slot event record is not a safe handoff.

- `START_COMMAND` remains the lifecycle transition that publishes a command to a
  backend or remote service.
- if the command completes immediately, the start response may still carry the
  terminal completion as it does today through
  [`CitusRemoteExecStartCommandResponse.commandCompletion`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:494)
- if the command completes later on a local frontend session, the service should
  publish a terminal completion event into a caller-visible per-session
  completion mailbox and signal the waiting side, rather than requiring the
  caller to submit a separate poll request.
- `POLL_COMMAND_COMPLETION` should be removed from the normal command lifecycle
  once push completion is implemented end-to-end. While the transition is in
  progress, it may remain as a debug/compatibility fallback only.

The local backend-to-service path is already close to push. The backend writes a
fixed-width
[`CitusRemoteExecCommandCompletion`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:422)
into
[`CitusRemoteExecLocalCompletionMailbox`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_backend_protocol.h:146)
and bumps `publishedEpoch` in
[`PublishCompletionToMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:271).
The service then observes that epoch in
[`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2273).
That part does not require a caller-submitted poll request. The remaining
poll-like scaffolding is higher level: local clients can still fall back to
[`TupleSinkServiceHandlePollCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8237)
for compatibility/debugging, and peer services still call
[`TupleSinkServiceHandlePeerPollCommandCompletionRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8620)
until the peer completion ring exists.

For peer command completion, replacing the peer poll request in
[`CitusRemoteExecPeerRequestKind`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_peer_control_protocol.h:40)
requires a responder-initiated completion publication. The completion should use
the same responder-visible RDMA publication rule described in
[rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md):
write completion metadata first, then publish with a doorbell.

Concrete no-poll plan:

1. **Keep one in-flight command per local frontend session.** Preserve the
   single-slot command mailbox invariant documented by
   [`TupleSinkServicePublishLocalCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2576).
   This keeps the first local push-completion implementation simple and avoids
   adding a local frontend control queue before there is true command
   pipelining.

2. **Add an explicit local completion-consumer binding at session/open time.**
   For local frontend clients, prefer a per-session caller-visible completion
   ring over reusing the original request/response control slot.
   Reusing the control slot would keep that slot logically tied to delayed
   completion and would reduce control-plane availability. The caller maps or
   receives this completion record before command start, so `START_COMMAND` does
   not need a later poll request to discover where terminal state should appear.

3. **On visible local completion, publish to the bound consumer.** After
   [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2273)
   observes `STARTED`, `COMPLETED`, or `FAILED`, the service should copy the
   fixed-width `CitusRemoteExecCommandCompletion` into the bound caller-visible
   completion ring and publish it with an epoch/doorbell. This replaces the
   local-client branch in
   [`TupleSinkServiceHandlePollCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8237).

4. **Use a peer completion ring for service-to-service push completion.** Peer
   completions are responder-initiated events, so they can arrive independently
   of the initiating peer's current request/response work on the control
   channel. A single-slot peer mailbox would either preserve the old serialized
   request/response bottleneck or require fragile special cases when completion
   and other peer control work overlap. The target should be a small fixed-size
   peer completion ring, separate from tuple/result sinks.

5. **Drive completion delivery from common service progress.** The future
   `TupleSinkServicePumpOnce(mask)` helper should include a completion-delivery
   phase after backend mailbox consumption. That way terminal completion
   propagation happens whenever the service makes progress, including while a
   local SQL command wait loop is active.

6. **Retire `terminalCompletionPendingPeerPoll`.** The current
   `terminalCompletionPendingPeerPoll` field exists because terminal peer
   completions are held until the peer polls. With push completion, the state
   should become either "completion not yet delivered" or "completion delivered"
   to the bound local completion mailbox or peer completion ring. Session
   retirement should wait for delivery, not for a future poll.

7. **Delete normal callers of `POLL_COMMAND_COMPLETION`.** After local and peer
   push delivery are validated, the client library and session APIs should wait
   on their completion slot/epoch or peer completion ring directly. The protocol
   enum values may remain temporarily for debug, but production
   pgbench/client-SQL and backend-backend command paths should not issue
   poll-completion requests.

The peer completion ring is a control-plane event queue, not a tuple/result
payload sink. It should remain separate from payload sinks and from the neutral
payload-stream abstraction below. The ring can be implemented after the local
frontend no-poll path is validated, but it should be treated as the peer
push-completion target, not merely as an optional optimization afterthought.

Implementation progress on the merged client-SQL/basebackup branch:

- local frontend push completion is implemented with the 8-slot
  [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:458)
- backend-to-service completion is also an 8-slot
  [`CitusRemoteExecLocalCompletionMailbox`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_backend_protocol.h:146);
  the earlier single-slot backend completion mailbox was not correct once SQL
  result materialization could publish `STARTED` and terminal states close
  together
- the service publishes `STARTED`, `COMPLETED`, and `FAILED` local client
  completions through
  [`TupleSinkServicePublishClientCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1620)
- frontend clients map and observe the mailbox through
  [`HomerClientOpenCompletionMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:520),
  [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:2033),
  and [`HomerClientWaitCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:2148)
- pgbench now waits for terminal completion through
  [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:2033)
  at [`HomerRunCommandAndWait()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3731)
  instead of issuing a `POLL_COMMAND_COMPLETION` request
- service-to-service peer push completion is still future work and still needs
  the fixed-size peer completion ring described above

## Client SQL wait-loop fix

Implementation progress: the old masked local client SQL wait is now a
transitional compatibility/debug helper, not the measured pgbench path. The
service has an internal
[`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8776)
progress helper with explicit masks. The main loop calls it with
[`TUPLE_SINK_SERVICE_PUMP_MAIN_LOOP`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:703)
at
[`main()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8942),
covering local control, peer control, all backend completions, sink/payload
progress, and the heartbeat.

[`TupleSinkServiceWaitForLocalCommandStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2796)
is still present in the source, but it is no longer called by the local
client-SQL `START_COMMAND` branch. If used by an older local synchronous path,
it keeps the command's own completion mailbox check on every spin and also runs
a periodic background
[`TUPLE_SINK_SERVICE_PUMP_LOCAL_WAIT_BACKGROUND`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:715)
pump at
[`tuple_sink_service_process.c:2850`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2850).
That background pump drains all completion mailboxes and active payload-stream
work, including deferred close/reclaim paths reached through
[`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6882).
That fallback cadence is controlled by
[`HOMER_SERVICE_LOCAL_WAIT_BACKGROUND_PUMP_INTERVAL`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:114),
currently `64`, so a synchronous fallback path would not scan all 64 sessions
and 64 sinks on every spin.

Important scoped limitation: peer-control acceptance is still deliberately
excluded from the local wait. A peer `START_COMMAND` handler can itself need
local control-slot progress during backend startup; accepting such a peer
request while the local request slot is still `REQUEST_READY` can replace the
original starvation artifact with a nested wait that cannot safely pump local
control. The next design step for this specific gap is a nonblocking/deferred
peer-control dispatch mode, not simply adding peer-control to the local wait
mask.

Completed parts of the earlier plan:

1. `TupleSinkServicePumpOnce(mask)` exists.
2. the main loop uses the full mask.
3. the local client SQL `START_COMMAND` branch now avoids the wait entirely for
   the measured pgbench path; the fallback wait keeps local control disabled and
   periodically pumps non-control background work.
4. early-child-exit detection remains in the local wait, after normal completion
   checks and the periodic background pump.

Still deferred:

- peer-control progress from inside a local control-slot wait
- bounded per-class scheduling/budgeting; the current helper is a progress
  primitive, not the research scheduler

This masked helper is a transitional implementation, not the target
architecture. The deeper issue is not "which flags are safe in a nested wait";
it is that command handlers should not block while waiting for backend startup
or terminal completion. The local frontend path now follows that rule; the peer
path still needs the same treatment.

## Async command submission and pushed state events

The cleaner next design is to make `START_COMMAND` submission nonblocking for
both local frontend sessions and service-to-service peer sessions.

Current shape:

- [`TupleSinkServiceHandleStartCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7997)
  now has a submit-only local client-SQL branch: it publishes the command to the
  socketless backend and returns `PENDING`; `STARTED` and terminal states are
  pushed later through the frontend completion mailbox.
- [`TupleSinkServiceWaitForLocalCommandStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2796)
  remains in the source for compatibility/debug paths and older local command
  shapes, but the measured pgbench path no longer depends on it.
- Peer command startup still waits in
  [`TupleSinkServiceWaitForCommandStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2701).
- The remaining nested-wait issue is therefore mostly the service-to-service
  peer command path. The local pgbench path has moved to pushed completion, but
  peer push completion still needs the fixed-size peer completion ring.

Target nonblocking shape:

1. `START_COMMAND` validates request/session state, publishes the backend
   command mailbox entry, records `commandSequence`, marks the session as
   `PENDING`, and returns a fixed-width `SUBMITTED` / accepted response
   immediately.
2. Backend `STARTED`, `COMPLETED`, and `FAILED` state remains authoritative in
   the session-owned completion mailbox consumed by
   [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2273).
3. The normal top-level service loop consumes backend state and pushes events to
   the appropriate caller-visible completion channel:
   - local frontend: the existing small-ring
     [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:458)
   - peer service: the future fixed-size peer completion ring
4. A row-producing SQL command publishes a `STARTED` event with result-sink
   readiness once
   [`RemoteExecSqlDestStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:678)
   has created/bound the result sink. The frontend/peer can then drain payload
   while the backend keeps executing.
5. Terminal `COMPLETED` / `FAILED` events are pushed later, independent of the
   original `START_COMMAND` response.
6. Session retirement waits for required completion delivery instead of waiting
   for a blocking handler response or a later poll.

Why this removes the peer-control acceptance issue:

- peer-control requests are accepted only from the normal top-level
  [`TupleSinkServicePumpPeerConnections()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5786)
  path
- peer `START_COMMAND` handling returns after publishing work and does not enter
  its own startup wait
- local `START_COMMAND` handling also returns after publishing work
- there is no nested service-progress context that has to decide whether local
  control, peer control, or sinks are safe to pump

This design means `TupleSinkServiceWaitForLocalCommandStartup()` should stay out
of the normal local client SQL path, and `TupleSinkServiceWaitForCommandStartup()`
should disappear from the peer command path. A small synchronous helper may
remain only for debug or explicitly blocking control operations, not for the
normal pgbench/basebackup data-plane workload.

Implementation steps:

1. add an explicit command state/response status such as `SUBMITTED` to the
   fixed-width local and peer `START_COMMAND` responses, or define that an `OK`
   response with `commandState=PENDING` means accepted-not-started
2. change local `START_COMMAND` handling to publish the backend command and
   return immediately; do not call `TupleSinkServiceWaitForLocalCommandStartup()`
   in the measured pgbench path. **Done.**
3. change peer `START_COMMAND` handling to publish the backend command and return
   immediately; do not call `TupleSinkServiceWaitForCommandStartup()`
4. make client/pgbench wait exclusively on pushed `STARTED` and terminal events
   via `HomerClientTryCommandCompletion()` /
   `HomerClientWaitCommandCompletion()`. **Done for local frontend sessions.**
5. add the peer completion ring and make peer command callers wait on that ring
   instead of issuing `POLL_COMMAND_COMPLETION`
6. keep result-sink draining driven by `STARTED` readiness metadata, then terminal
   completion follows as a separate event
7. remove or demote `POLL_COMMAND_COMPLETION`,
   `terminalCompletionPendingPeerPoll`, and the two startup wait helpers from
   normal production paths

The key performance goal is that command handlers do only bounded control-plane
work: validate, publish, record, return. Progress, fairness, payload movement,
and later scheduling all happen in the top-level service loop.

## Neutral payload stream state

Status as of May 14, 2026: the neutral payload stream abstraction milestone is
implemented in the service process, and the first internal naming cleanup is
done. The service table entry and payload pump functions now use neutral
`HomerServicePayloadStream*` names. Some control-protocol fields and header
names still say "sink" because the wire protocol started as the tuple-view path,
but the framework split is in place: generic queue/RDMA stream state is separate
from tuple-view metadata and basebackup-specific accounting.

Before the May 14, 2026 neutral-stream refactor, `TupleSinkServiceSinkState`
was overloaded. It combined:

- generic queue/RDMA payload-stream state
- tuple-specific `CitusTupleSinkKey` and `CitusTupleViewContract`
- basebackup-specific semantic branch state

The code now has an embedded neutral payload stream in the service table entry
[`HomerServicePayloadStreamEntry`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:668).
The generic container is
[`HomerPayloadStreamState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:569),
tuple-family metadata is in
[`HomerTupleViewPayloadState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:629),
and basebackup-family receiver accounting is in
[`HomerBaseBackupPayloadState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:652).
The service derives and caches the payload object family at stream-open time via
[`TupleSinkServicePayloadFamilyForOpKind()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:795)
instead of branching directly on the higher-level remote-execution op kind in
the hot payload path.

"Tuple sink" should describe only the tuple-view object family, while the common
container should be a neutral Homer payload stream. The remaining future cleanup
is protocol/API surface: control request fields, protocol/header names, and
client-facing "serviceSinkId" terminology still need a separate wire/API cleanup.

Target split:

```c
typedef enum HomerPayloadObjectFamily
{
    HOMER_PAYLOAD_OBJECT_FAMILY_TUPLE_VIEW_BATCH,
    HOMER_PAYLOAD_OBJECT_FAMILY_BASE_BACKUP_STREAM,
    HOMER_PAYLOAD_OBJECT_FAMILY_WAL_STREAM,
} HomerPayloadObjectFamily;

typedef struct HomerPayloadStreamState
{
    /* Generic stream/container state, not tuple-specific state. */
    uint64_t streamId;
    uint64_t parentServiceSessionId;
    uint32_t slotCount;
    uint32_t slotCapacityBytes;

    TupleSinkServiceQueueMapping sendQueue;
    TupleSinkServiceQueueMapping receiveQueue;
    TupleSinkServicePayloadRingState localReceivePayloadRing;
    TupleSinkServicePayloadHeadMirrorState localSenderHeadMirror;
    TupleSinkServicePeerMemoryRegionHandle *sendQueueMemoryRegionHandle;
    CitusTupleSinkPayloadRingDescriptor peerReceivePayloadRing;
    CitusTupleSinkPayloadHeadMirrorDescriptor peerSenderHeadMirror;

    bool peerBindingActive;
    bool peerBindingLocallyInitiated;
    bool localPeerClosePending;
    bool receivedPeerClosePending;
    bool payloadTransportBroken;
    bool receivePayloadDoorbellPending;

    uint64_t peerServiceSessionId;
    uint64_t peerServiceStreamId;
    int32_t peerNodeId;
    uint32_t peerControlPort;
    TupleSinkServicePeerConnectionHandle *peerConnectionHandle;
    char peerHost[CITUS_REMOTE_EXEC_CONTROL_HOST_BYTES];

    uint64_t senderVisibleRemoteConsumedHead;
    uint64_t payloadSenderPostedTail;
    uint64_t payloadSenderCompletedHead;
    uint64_t payloadSenderLastSignaledTail;

    /* Later scheduler metadata: traffic class, priority, weight, grant caps. */
} HomerPayloadStreamState;
```

Family-specific state should sit beside that generic stream state:

```c
typedef struct HomerTupleViewPayloadState
{
    CitusTupleSinkKey sinkKey;
    CitusTupleViewContract tupleViewContract;
} HomerTupleViewPayloadState;

typedef struct HomerBaseBackupPayloadState
{
    uint64_t receivedObjects;
    uint64_t receivedPayloadBytes;
    uint64_t lastArchiveIndex;
} HomerBaseBackupPayloadState;
```

Then object families become:

- `TUPLE_VIEW_BATCH`: owns `CitusTupleSinkKey`, `CitusTupleViewContract`, tuple
  batch validation, and receive-side tuple-view decode
- `BASE_BACKUP_STREAM`: owns `CitusRemoteBaseBackupMessageHeader` validation,
  basebackup byte accounting, and later receiver-side materialization
- later `WAL_STREAM` or other DB-semantic byte/object streams

The common payload stream remains the RDMA/ring container. Tuple views become
one object family on that container, not the container abstraction.

Implementation migration:

1. Keep the existing service table entry temporarily, but embed
   `HomerPayloadStreamState stream` in it. This made the first patch a
   structural split, not a global rename. **Done**, and the table entry is now
   named
   [`HomerServicePayloadStreamEntry`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:668).
2. Move generic fields from
   [`HomerServicePayloadStreamEntry`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:668)
   into `stream`: queue mappings, slot geometry, peer binding, RDMA handles,
   sender/receiver frontiers, doorbell state, transport-broken state, and future
   scheduler metadata. **Done, except future scheduler fields are still future.**
3. Move tuple-only fields into `HomerTupleViewPayloadState`, and move
   basebackup counters currently at
   [`HomerBaseBackupPayloadState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:652).
   **Done.**
4. Add a cached `HomerPayloadObjectFamily` field. Do not use
   `semanticOpKind` as the long-term dispatch primitive; map session op kind to
   object family at open time. **Done** through
   [`TupleSinkServicePayloadFamilyForOpKind()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:795).
5. Refactor sender validation now embedded in
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6069)
   into family-specific helpers that validate the in-place object header and
   return the exact byte range to RDMA-write. **Done** in
   [`HomerServiceValidateOutgoingPayloadObject()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5843).
6. Refactor receiver validation and consumption now embedded in
   [`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6576)
   into family-specific helpers. Tuple-view consumption still decodes into the
   receive queue; basebackup consumption still blackholes/counts until a real
   materializer lands. **Done** in
   [`HomerServiceValidateReceivedPayloadObject()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5961).
7. Refactor local basebackup smoke consumption currently in
   [`HomerServicePumpLocalBaseBackupBlackhole()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6489)
   to call the same basebackup object-family validation/counting helper. **Done**
   with shared accounting in
   [`HomerServiceRecordBaseBackupPayloadObject()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5942).
8. Rename the outer table entry and pump functions only after the embedded
   stream split is stable. **Done** with `HomerServicePayloadStreamEntry`,
   [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6882),
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6069),
   and
   [`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6576).
9. Leave protocol/header file renames for a later cleanup. The first goal is to
   fix the framework abstraction without mixing it with broad mechanical churn.
   **Still future.**

Hot-path dispatch decision:

- Use enum + `switch` dispatch in the payload pump hot path, not a per-object
  function-pointer ops table.
- A function-pointer ops table is cleaner for extensibility but adds an
  indirect call and pointer load, blocks normal inlining, and is harder to reason
  about for small tuple/result objects.
- A cached enum branch is highly predictable because a stream's object family is
  fixed at open time. The switch should be placed once per pump or per batch when
  practical, not inside per-byte loops.
- Cold/control paths may still use helper tables or named helper functions for
  readability, but the normal data path should stay direct and compiler-visible.

Implementation progress on May 14, 2026:

- The neutral-state split is implemented in the service process without changing
  external control protocol names. Generic stream fields now live under
  [`HomerPayloadStreamState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:569);
  tuple-view state lives under
  [`HomerTupleViewPayloadState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:629);
  basebackup counters live under
  [`HomerBaseBackupPayloadState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:652).
- The hot helpers are `static inline`; the subagent performance review flagged
  this explicitly to avoid function-pointer-like per-object call overhead.
- The local basebackup smoke path now uses the same basebackup object-family
  validator/accounting helper as the RDMA receive path and avoids clearing the
  full error buffer per object.
- Follow-up performance investigation showed the apparent pgbench regression was
  dominated by benchmark state, not the neutral stream abstraction:
  `pgbench_history` had grown to roughly 5.5M rows. After truncating
  `pgbench_history` and vacuuming the pgbench tables, single-client Homer
  pgbench recovered to the previous band with repeats at `6033 TPS`, `7329 TPS`,
  and `7333 TPS`.
- A real hot-path cleanup also moved client-SQL result material copying to a
  session-owned reusable tuple slot in
  [`RemoteExecEnsureSessionResultCopySlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:580),
  so [`RemoteExecSqlDestStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:678)
  no longer allocates a fresh copy slot per command. After that cleanup and a
  benchmark reset, single-client Homer pgbench repeats stayed in the `5.6k` to
  `6.8k TPS` band after restart variance, and a clean c4/j4 run completed at
  about `15104 TPS` with p99 `0.460 ms`. Local
  `TARGET 'homer:mode=blackhole,...'` basebackup remained in the previous local
  blackhole band.
- A c4 run with `--client-cpu=3` pinned all pgbench worker threads to one CPU
  and produced false tail-latency stalls (`595 TPS`, p99 `96 ms`). Do not use
  that single-CPU frontend pinning mode as the multi-client Homer scaling
  comparison; use no frontend pinning or pgbench
  [`--client-cpus=LIST`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1115)
  so worker threads are assigned round-robin across separate CPUs.

## Execution lane removal

`executionLane` currently lives in
[`CitusRemoteExecSessionKey.executionLane`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:182),
with constants
[`CITUS_REMOTE_EXEC_LANE_DEFAULT`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:117)
and `CITUS_REMOTE_EXEC_LANE_REPLICATION`.

It is too weak and too indirect to be a scheduler primitive. It also pollutes the
session compatibility key with a field whose real use is traffic treatment.

Target direction:

- remove `executionLane` from the long-term compatibility key
- replace it with explicit scheduler-facing metadata on streams/commands:
  `trafficClass`, `priority`, `weight`, `latencySensitive`,
  `maxGrantBytes`, and `maxGrantObjects`
- derive those fields at open/start time from the object family and operation
  options, not from a generic lane enum
- keep a compatibility shim only as long as older request structs still carry
  the field

## Transport egress scheduler

The scheduler that matters for the research contribution is the RDMA egress
scheduler. It decides which ready RDMA-sendable work is posted next when several
traffic classes are active.

Use these terms consistently:

- **Traffic class** is semantic transport treatment, not merely numeric
  priority. Examples are critical control, normal control, foreground payload,
  bulk payload, and maintenance. A policy may map traffic class to default
  priority, weight, or latency sensitivity, but those are scheduler parameters,
  not the class itself.
- **Transport lane** is the physical/resource lane used to carry one traffic
  class or a small class group. A lane owns the relevant RDMA resources: QP/CQ
  state, registered control/ring structures, doorbell/notification state,
  completion handling, and any lane-local posted-WR accounting.
- **Logical channel** is the session/control/payload abstraction above the
  transport. Examples are service-to-service peer control, peer command
  completion, a foreground tuple/result payload stream, and a bulk basebackup
  stream. Logical channels choose traffic classes; the transport maps those
  classes onto lanes.
- **Ready item** is the scheduler-visible unit of pending egress work. It names
  the owner/channel and carries traffic class, priority/weight hints, estimated
  bytes, estimated WR count, object count, and age.
- **Grant** is the scheduler decision for one ready item: how many objects, WRs,
  or bytes it may post in this scheduling step.

There are two scheduling layers and they should stay distinct:

- **Service progress scheduling** decides how the Homer service spends CPU
  cycles among local control, peer control, backend completions, payload streams,
  and heartbeat/progress maintenance. The current fixed-order primitive is
  [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8889),
  with active payload-stream scanning at
  [`tuple_sink_service_process.c:8950`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8950).
- **Transport egress scheduling** decides which RDMA-sendable work is posted to
  the NIC next. Today
  [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6239)
  still computes and posts its own payload batches directly, so it is not yet a
  transport scheduler.

Candidate work items should be explicit metadata, not a bit mask:

```c
typedef struct HomerTransportReadyItem
{
    HomerTransportItemKind kind;
    HomerTrafficClass trafficClass;
    uint64 serviceSessionId;
    uint64 serviceStreamId;
    uint32 priority;
    uint32 estimatedWrCount;
    uint64 estimatedBytes;
    uint64 readySinceTick;
    void *owner;
} HomerTransportReadyItem;
```

The ready set is the current fixed-size array of eligible send work. It does not
encode ordering. The selected policy owns ordering and grants.

Candidate traffic classes:

```c
typedef enum HomerTrafficClass
{
    HOMER_TRAFFIC_CONTROL_CRITICAL,
    HOMER_TRAFFIC_CONTROL_NORMAL,
    HOMER_TRAFFIC_FOREGROUND_PAYLOAD,
    HOMER_TRAFFIC_BULK_PAYLOAD,
    HOMER_TRAFFIC_MAINTENANCE
} HomerTrafficClass;
```

Policies to support:

- strict foreground/control priority
- weighted round robin
- deficit round robin by bytes/WRs
- later deadline/age-aware policies for tail-latency experiments

The scheduler grant should be object-aware:

```c
typedef struct HomerTransportGrant
{
    uint32 maxObjects;
    uint32 maxWrCount;
    uint64 maxBytes;
} HomerTransportGrant;
```

The transport publisher reports actual progress:

```c
typedef struct HomerTransportProgress
{
    bool madeProgress;
    uint32 postedObjects;
    uint32 postedWrCount;
    uint64 postedBytes;
    bool stillReady;
} HomerTransportProgress;
```

Deficit policies should account bytes or WRs rather than just objects. Otherwise
a tiny control message and an 8 MiB basebackup chunk look equally expensive.

## Multiple QPs from the start

The design should allow multiple lanes/QPs per peer transport from the start,
but service code should target a traffic-class transport API rather than
reaching directly for "the peer connection's QP."

One QP can preserve correctness, but it cannot reorder work already posted to
that QP. If the service posts a long basebackup WR chain and then a tiny control
message becomes ready, the control message cannot jump ahead on the same QP.
That is posted-WR head-of-line blocking. It is logically real even before
measurement.

Target peer transport shape:

```c
typedef struct HomerTransportLane
{
    HomerTrafficClass trafficClass;
    /* QP/CQ, control/ring state, doorbells, completion accounting. */
} HomerTransportLane;

typedef struct HomerPeerTransport
{
    HomerTransportLane criticalControl;
    HomerTransportLane normalControl;       /* optional initially */
    HomerTransportLane foregroundPayload;
    HomerTransportLane bulkPayload;
    HomerTransportLane maintenance;         /* optional */

    /*
     * Later: lane pools are allowed for high-throughput classes.
     * The API must not assume exactly one QP per traffic class.
     */
} HomerPeerTransport;
```

The scheduler chooses a traffic class and grant. The transport maps that class
to a QP. Link-level contention still exists, but a control write is no longer
queued behind already-posted bulk WRs in the verbs work queue.

Initial lane policy:

- start with one **critical-control lane** per peer transport
- that lane carries peer-control requests and peer completion/event records
- the critical-control lane uses a multi-slot peer-control request ring plus a
  peer completion/event ring, not many one-slot mailboxes
- keep this as one lane initially to preserve simple total ordering for critical
  control events and avoid over-engineering control-plane sharding before there
  is evidence it is needed
- keep the abstraction capable of a future critical-control lane pool, but only
  shard critical control later if records carry explicit request/session
  ordering and measurements show the single lane is a bottleneck

Payload classes are the first realistic candidates for multiple lanes/QPs within
one class. Foreground tuple/result payloads may use a foreground QP or QP pool;
basebackup and future WAL streams may use a bulk QP or QP pool.

The first implementation may keep one QP internally while the scheduler API is
introduced. That first step is a correctness/performance-preservation
checkpoint only; it does not claim to reduce posted-WR head-of-line blocking.
The API must not bake in "one peer == one QP." The right abstraction is "one peer
has one or more traffic-class transports."

Multiple QPs within a traffic class should also remain possible for throughput
experiments, especially for bulk basebackup or future WAL streams.

Staged implementation rule:

1. First land the traffic-class metadata and transport API while mapping every
   traffic class to the existing peer connection lane/QP. This is now complete
   for the single-lane checkpoint; it preserves behavior and does not claim to
   reduce posted-WR head-of-line blocking.
2. Only after that checkpoint passes, split physical resources so critical
   control, foreground payload, and bulk payload map to distinct lanes/QPs. This
   second step is the one expected to reduce posted-WR head-of-line blocking.
3. Scheduler policy experiments come after the physical lane split is measurable.
   Before that, policy decisions cannot fully express the intended transport
   priority because all work still enters one QP.

Implementation progress on May 14, 2026: the single-lane checkpoint is in the
code. [`HomerTransportTrafficClass`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:62)
and [`HomerTransportSchedulingMetadata`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:82)
define the internal traffic-class/scheduler metadata. Each payload stream caches
that metadata in
[`HomerPayloadStreamState.transportScheduling`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:630),
initialized by
[`HomerServiceInitPayloadTransportScheduling()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:844).
The current mapping is:

- tuple/COPY/result payload -> foreground payload, selected by
  [`HomerServicePayloadTrafficClassForOpKind()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:826)
- basebackup payload -> bulk payload, selected by the same helper
- peer-control requests/responses -> critical control, passed through
  [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2634)

All classes still resolve to the existing peer connection/QP through the
single-lane resolver inside
[`TupleSinkServicePeerConnectionForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2315).
Payload posting now goes through the traffic-class API at
[`TupleSinkServicePostPeerRegisteredPayloadBatchWithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3571),
with the call site in
[`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6534).
Outgoing peer-connection acquisition also accepts the class at
[`TupleSinkServiceEnsureOutgoingPeerConnectionHandleRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2267),
but the connection lookup key is intentionally unchanged for this checkpoint.

Validation on the final single-lane binary:

- `make -j8`, `git diff --check`, and
  `sudo -n make install-headers install-service-bin install` passed in
  `/data/dbcomm/citus-dbcomm-separate-comm-stack`.
- Single-client Homer pgbench, c1/j1/t50000, after clean
  `pgbench_history` reset: first post-restart run showed the known low
  cold/variance band at `5170.8 TPS`, repeat recovered to `6049.99 TPS` with
  p99 `0.183 ms` and zero failures.
- Multi-client Homer pgbench, c4/j4/t20000, after clean reset: `15044.83 TPS`,
  p99 `0.462 ms`, zero failures. This remains in the current `~15k TPS`
  performance band.
- Local Homer basebackup blackhole with the previously used explicit geometry
  `slots=64,bytes=1048576` remained in the old local band after the traffic-class
  patch: `4.44s`, `4.48s`, and `4.44s`.
- Remote RDMA basebackup blackhole to farnet0 completed successfully after
  syncing/restarting the farnet0 service binary: `real 6.70s`. This is a
  different path from local blackhole and should not be used as the local
  blackhole no-regression baseline. A prior rerun failed during receiver binary
  replacement/stale service state with a local control timeout, then succeeded
  immediately after restarting the local service; do not treat that failed run
  as a traffic-class payload regression.

## Basebackup RDMA performance investigation lessons

This section records what the basebackup performance work taught us before the
multiple-QP/scheduler milestone. The important correction is that **mixed
traffic scheduling is not the explanation for a single bulk-stream basebackup
regression**. The scheduler remains necessary for concurrent foreground SQL plus
bulk basebackup, but the single-stream basebackup gap should first be explained
by measurement discipline and the RDMA payload publication path itself.

### Measurement discipline: warm steady state, not first-run timing

Cold first runs on farnet can include setup effects that are not the target
metric: peer connection establishment, memory registration/page faults, service
state after restart, PostgreSQL checkpoint timing, and binary replacement
artifacts. The target measurement is a warmed run that lasts at least about five
seconds or an aggregate of warmed repeats.

Evidence from May 15, 2026:

- A cold/diagnostic remote RDMA run had previously shown about `9.15s` with
  `slots=8,bytes=8388608`, which made the transport look much worse than local
  blackhole.
- Without restarting either service, warmed remote RDMA repeats on the same
  farnet1 -> farnet0 setup completed in `4.50s`, `4.69s`, and `4.65s`.
- Warm local Homer blackhole repeats completed in `4.37s` and `4.37s`.

Those runs are slightly shorter than the desired final measurement window, so
they are not final experiment numbers. They are still enough to refute the
earlier interpretation that the steady-state RDMA path has a multi-second
transport bottleneck. For this workload, final claims should use larger input,
multiple warmed repeats, or both.

### Successes and reusable patterns

The successful optimizations share a common shape: remove serialized progress
points from the hot path while preserving the RDMA publication rule that payload
bytes become responder-visible only after a responder-visible doorbell.

1. **Bigger payload slots removed first-order per-object overhead.**

   The original 64 x 1 MiB default made the service pay validation, header,
   verbs, and doorbell overhead for about `25k` objects per backup. Moving to
   larger slots cut the object count to about `6.4k` for 8 MiB chunks and
   changed two-service RDMA from the `31s` band to roughly the `9s` band before
   later transport fixes. The code path this helped is the per-object loop in
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6255),
   especially validation through
   [`HomerServiceValidateOutgoingPayloadObject()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6029)
   and descriptor construction before
   [`TupleSinkServicePostPeerRegisteredPayloadBatchWithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3648).

   Reusable pattern: tuple/result/WAL payload streams should choose object sizes
   that amortize transport metadata, but they should not blindly make semantic
   objects so large that later foreground traffic cannot interleave. For very
   large objects, the longer-term answer is substrate-level fragmentation rather
   than mutating producer-level semantics.

2. **Asynchronous posted/completed frontiers removed a false source-slot
   serialization point.**

   The sender now distinguishes "posted to the NIC" from "safe to release the
   backend-visible source slot." That lets
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6255)
   keep more WRs in flight while local completions advance
   `payloadSenderCompletedHead`. Earlier measurements recorded the 8 MiB path
   improving from about `9.28s` to about `6.01s`, and 1 MiB chunks stopped being
   pathologically slow.

   Reusable pattern: any future payload family should track three frontiers
   separately: producer-published, NIC-posted, and locally completed/reusable.
   Collapsing those frontiers serializes the data path even when RDMA ordering
   would allow more in-flight work.

3. **Range publication reduced responder-visible doorbells and completion
   checkpoints.**

   The service builds up to
   `CITUS_REMOTE_EXEC_PEER_PAYLOAD_BATCH_MAX_WRITES` descriptors and publishes
   one contiguous semantic range through
   [`TupleSinkServicePostPeerRegisteredPayloadBatchWithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3648).
   Earlier warmed 64 x 1 MiB runs reached the `5.08s` band after range
   publication, compared with `6.60s` after only the asynchronous frontier split.

   Reusable pattern: keep the object-family abstraction at the stream layer, but
   let the transport publish ranges when the ring contains adjacent ready
   objects. This applies to tuple batches, basebackup chunks, and future WAL
   chunks.

4. **The empty-CQ-poll guard removed wasted progress work without changing
   semantics.**

   The sender should only call
   [`TupleSinkServicePollPeerPayloadSendCompletionRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3941)
   when `payloadSenderCompletedHead < payloadSenderLastSignaledTail`. A
   diagnostic 8x8MiB run cut empty payload send-CQ polls from about `30M` to
   about `12.5M`.

   Reusable pattern: the service loop should avoid polling a resource whose
   outstanding frontier proves there is no possible completion. This is a first
   layer service-loop optimization, independent of the later RDMA egress
   scheduler.

5. **Restart discipline matters when PostgreSQL links the Homer client
   statically.**

   One apparent regression came from reinstalling the Homer client library but
   not restarting the running postmaster. After PostgreSQL was reinstalled and
   restarted, batching in
	   the earlier producer-side publication helpers,
	   and
	   [`HomerClientSubmitBaseBackupRecord()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1606)
	   was actually present in the backend process.

   Reusable pattern: benchmark notes must record whether the running postmaster
   was restarted after `libhomer_client.a` or PostgreSQL relinks. Otherwise we
   can benchmark stale code and misattribute the result to transport design.

### Refuted or narrowed hypotheses

1. **Receiver ring credit was not the warmed bottleneck in the measured
   single-stream run.**

   Diagnostic counters showed `remote_waits=0` in the sender path. That means
   the branch in
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6551)
   was not constraining that run. Increasing receive slot count therefore did
   not help. Receiver credit can still matter in a different scale point or with
   a slower materializing receiver, but it was not the root cause for the warmed
   blackhole receiver experiment.

2. **Local source-slot / send-completion credit was not the warmed bottleneck in
   the same run.**

   Diagnostic counters also showed `local_waits=0`, so the local in-flight
   check at
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6477)
   was not blocking normal progress. This narrows the remaining cost toward WR
   count, doorbell count, validation/header work, service-loop overhead, or cold
   setup effects rather than hard credit stalls.

3. **Receiver consumed-head ACK frequency is not the first-order issue for the
   warmed blackhole receiver.**

   Receiver ACKs are already thresholded by
   [`HomerServiceShouldPublishReceiverHeadAck()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6911),
   capped by `HOMER_SERVICE_RECEIVER_HEAD_ACK_MAX_GRANULARITY`. The receiver
   publishes credit through
   [`HomerServicePostReceiverHeadAck()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6846).
   Coalescing consumed-head publication by pump pass did not materially improve
   the earlier basebackup run. With `remote_waits=0`, making ACKs more frequent
   should not help; making them less frequent only helps if it does not create
   sender credit stalls.

4. **Forcing full batches can improve counters while hurting elapsed time.**

   Waiting for full 8-object bulk ranges produced clean counters (`810` sender
   batches and `809` receiver head publishes in one diagnostic), but slowed the
   run to about `10.3s` because it sacrificed producer/RDMA overlap. This does
   not mean coalescing is a bad direction. It means coalescing must not be
   implemented as a blocking wait for a full batch. It should happen
   opportunistically over the ready set already present in the ring.

5. **Producer-side tar-byte aggregation above the Homer slot boundary is not a
   safe shortcut.**

   An experiment reduced basebackup object count from about `6481` to `2765`
   and sender batches from about `1620` to `692`, but it was backed out. It
   mutated the upstream-visible `bbs_buffer`/`bbs_buffer_length` suffix inside
   [`bbsink_homer_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_homer.c:277)
   and
   [`bbsink_homer_manifest_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_homer.c:331),
   violated subtle basebackup sink lifetime assumptions, and caused
   intermittent `BASE_BACKUP` backend segmentation faults after clean restarts.
   It also did not improve successful elapsed-time runs enough to justify the
   semantic risk.

   The useful lesson is not "aggregation is bad"; it is "aggregation belongs
   below the typed object boundary or inside the RDMA substrate, not by
   rewriting PostgreSQL's active sink buffer contract."

### Implemented May 15 publication and doorbell changes

The current code now implements the two optimizations that survived the warm-run
A/B checks:

1. **Immediate producer publication is the default.**

   [`HomerClientSubmitBaseBackupRecord()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1606)
   now publishes each byte-ring record immediately, while `publish=N` in the
   pg_basebackup target detail remains available for diagnostics. This was
   based on the warmed
   remote RDMA sweep with `slots=8,bytes=8388608`:

   - old `publish=auto` (`4`): about `5.02-5.09s` warmed
   - `publish=2`: `4.84-5.08s`, with about `472k-496k` reserve-wait loops
   - `publish=3`: `4.96-5.08s`, with about `1.15M-1.18M` reserve-wait loops
   - `publish=1`: about `4.86-4.94s` before doorbell piggybacking, with only
     about `114k-135k` reserve-wait loops

   This refuted the earlier idea that producer publication batching should be
   the default way to create larger service ranges. It improves sender-side
   batch shape, but it hides ready work from the service and costs pipeline
   overlap. Batching should happen downstream over already-visible work.

2. **The payload doorbell is piggybacked onto the final payload write.**

   [`TupleSinkServicePostPeerRegisteredPayloadBatchWithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3648)
   no longer posts a separate 8-byte tail `RDMA_WRITE_WITH_IMM`. The final
   payload WR in the batch uses `IBV_WR_RDMA_WRITE_WITH_IMM`, and the sender
   still tags the signaled completion with the completed semantic frontier for
   source-slot release.

   On the receiver,
   [`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7056)
   derives the local receive-ring frontier from validated in-band
   `CitusTupleSinkTransportHeader` records before consuming payload objects.
   A first attempt treated "doorbell arrived but the next header still looks
   stale" as corruption; farnet showed this can happen at ring wrap
   (`sequence=65` still showing stale `sequence=57`). The current implementation
   keeps the pending-doorbell flag set and retries instead of breaking the
   stream. This preserves correctness while avoiding the explicit tail WR on
   the hot path.

   With the no-stats measurement build (`CPPFLAGS='-D_GNU_SOURCE'`), warmed
   remote RDMA runs after the change were:

   - cold first run after restart: `8.29s` (not the steady-state number)
   - warmed runs: `4.76`, `4.76`, `4.73`, `4.88`, `4.95`, `4.62`, `4.76`,
     `4.62s`
   - a later warmed default/`publish=auto` rerun, where `auto` now means the
     immediate-publication default, produced `4.66`, `4.53`, and `4.48s`

   The stable comparison point is therefore roughly a `4.6-4.9s` warmed band,
   improved from the previous best `publish=1` explicit-tail band of about
   `4.86-4.94s` and from the old `publish=auto` band of about `5.0s`.

   Scope correction after the byte-ring checkpoint: this piggyback optimization
   applies to the fixed-slot payload-ring path where the receiver can derive the
   visible frontier by validating in-band `CitusTupleSinkTransportHeader` records.
   The first service-to-service byte-ring checkpoint deliberately uses the
   separate tail-publish helper
   [`TupleSinkServicePostPeerRegisteredPayloadBatchWithTailImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3826),
   because the receiver needs an explicit absolute byte frontier for the byte
   ring. A future one-RDMA-op byte-ring optimization may recover piggybacking by
   carrying grant-boundary metadata inside the byte stream, but that is not the
   current implementation.

   After piggybacking, small explicit producer batches are no longer obviously
   harmful, but they also did not justify changing the default:

   - `publish=2`: `4.67`, `4.79`, `4.53s`
   - `publish=3`: `4.67`, `4.70`, `4.64s`
   - `publish=4`: `4.66`, `4.66`, `4.96s`
   - `publish=8`: `5.79`, `5.62`, `5.97s`

   Keep the default as immediate publication for now. It has the best observed
   low end and avoids hiding ready work. Treat `publish=2..4` as sensitivity
   knobs, not as a new default, unless a longer run shows a stable win.

3. **Larger objects did not help this workload.**

   A warmed remote geometry sweep with immediate publication showed:

   - `slots=8,bytes=16777216`: about `5.60-5.77s`
   - `slots=4,bytes=33554432`: about `6.09-6.11s`
   - `slots=16,bytes=8388608`: about `5.12-5.22s`

   This refutes "object count alone dominates" for the current basebackup
   stream. Larger objects reduce object count but hurt overlap/credit
   granularity enough to lose. The current default geometry should stay at
   `slots=8,bytes=8388608` until a broader scheduler/fragmentation design
   changes the tradeoff.

### Remaining plausible optimizations

Current next-step plan after the May 15 publication and piggyback changes:

1. **Coalesce adjacent no-wrap payload slots into fewer RDMA WRs.**

   Piggybacking removes the separate tail WR, but the best path still emits
   almost one payload WR per object because immediate producer publication makes
   the service see mostly one-object ready ranges. When local send slots and
   remote payload slots are contiguous and do not wrap, the sender can publish
   one larger byte range while the upper layer still sees complete semantic
   objects through the in-band headers. This should be opportunistic over
   already-visible objects only; do not wait for a full batch, because the
   earlier full-batch experiment improved counters but slowed the run to about
   `10.3s`.

   Keep `WRITE_WITH_IMM` as the data-publish notification. The target transport
   shape is not memory polling; it is "one responder-visible immediate per
   coalesced posted range." The immediate should stay on the final payload WR in
   that range, so the receiver still gets a CQ event and the sender still gets a
   tagged completion frontier. Coalescing is useful because it reduces how often
   that notification is needed.

   The coalescing rule must be byte-contiguity, not just sequence adjacency:
   merge only when the previous descriptor's local end equals the next slot's
   local start and the previous remote end equals the next slot's remote start.
   This avoids RDMA-writing unused padding or stale bytes between variable-size
   objects. Full basebackup archive chunks usually qualify; partial archive,
   manifest, and END records usually do not.

   This is also the smallest concrete piece of the eventual adaptive scheduler.
   Long term, the scheduler should keep producers publishing promptly, observe
   already-ready items, and choose a grant in objects/bytes/WRs based on traffic
   class, age, and competing work. Small grants preserve foreground latency;
   larger grants let a lone bulk stream amortize WR and doorbell cost. The
   scheduler should adapt this transport batching decision instead of making the
   producer hide ready objects behind a fixed `publish=N` threshold.

   Implementation progress on May 15, 2026: byte-contiguous coalescing is now
   implemented in
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6260).
   The sender still validates and headers each semantic object, but it merges
   adjacent RDMA write descriptors only when the previous descriptor's local end
   and remote end exactly match the next slot. This keeps the object-family
   contract unchanged while reducing verbs descriptors when the ring has
   adjacent full-slot payloads.

   Diagnostic remote RDMA results with stats enabled:

   - default `publish=auto`, which currently means immediate `publish=1`:
     cold first run `8.41s`, warm repeats `4.54s` and `4.51s`;
     sender `sender_wrs=6484`, `sender_coalesced_objects=0`,
     `sender_batches` about `6473-6475`, receiver doorbells about
     `6440-6452`.
   - `publish=3`: `4.69s`, `4.79s`, and `4.79s`; sender
     `sender_batches=2161`, `sender_wrs=4876`,
     `sender_coalesced_objects=1608`, receiver doorbells about `2135-2141`.
     Producer reserve-wait loops rose to about `1.13M-1.16M`.

   This confirms that no-wrap coalescing works mechanically: it reduces WRs and
   receiver-visible doorbells when it is fed adjacent full slots. It also
   confirms that producer-side batching is the wrong way to feed it for this
   workload. Hiding ready slots behind `publish=3` improves transport counters
   but loses enough pipeline overlap to slow elapsed time. The next useful
   experiment is a bounded service-side/adaptive scheduler microbatch over
   already-visible or imminently-visible bulk work, not changing the producer
   default away from immediate publication.

2. **Consider a tiny service-side microbatch window only with evidence.**

   `publish=1` proves early visibility matters, but it also prevents the
   service from naturally seeing adjacent slots. A very small bounded retry in
   the service, after seeing one ready object, might let it coalesce work that
   is already about to become visible. This is risky because it can reintroduce
   hidden waiting. Only try it with counters that separately show elapsed time,
   reserve-wait loops, effective batch histogram, and receiver doorbells.

   Experiment result on May 15, 2026: a bulk-only ready-spin build with
   `HOMER_SERVICE_PAYLOAD_READY_SPIN_LIMIT=32` and target `3` was slower than
   the no-spin path. Warm runs were `4.76`, `4.72`, and `4.74s`. The sender
   performed about `206k` ready-spin pause/load loops per backup, gained only
   about `26-31` additional ready objects, and still recorded
   `sender_coalesced_objects=0`. This refutes the idea that the next adjacent
   object is usually only a few pause cycles away. A tiny service-side spin is
   mostly wasted service CPU for this workload.

   A no-spin shape counter run showed why: each backup has `6484` semantic
   objects, but only `2746` are full-slot objects while `3738` are partial-slot
   objects. With immediate `publish=1`, the service almost always sees a
   one-object range (`sender_batches` about `6471-6475`) and therefore cannot
   use byte-contiguous no-wrap coalescing. The earlier `publish=3` run created
   enough adjacency to coalesce `1608` objects, but it did so by hiding producer
   work and increasing producer reserve-wait loops by roughly an order of
   magnitude.

   Consequence: keep this ready-spin knob as diagnostic-only and disabled by
   default. If we want fuller objects, the next design should be a safe
   object-formation change, not a service spin. The tempting approach is to pack
   several upstream basebackup callbacks into one Homer object, but the
   PostgreSQL `bbsink` contract in
   [`basebackup_sink.h`](/data/dbcomm/postgres-citus-separate-comm-stack/src/include/backup/basebackup_sink.h:82)
   says `bbs_buffer`/`bbs_buffer_length` are generally stable after creation.
   The current Homer sink already bends that by swapping to a newly reserved
   slot at callback boundaries; packing partial callbacks into a single slot by
   continually exposing suffix pointers would bend it further and can break
   callers that assert a BLCKSZ-multiple buffer length, such as
   [`sendFile()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1617).
   A safe version needs separate design: either accept an explicit copy into a
   Homer-owned aggregation slot, add a PostgreSQL-side sink API that supports
   appendable variable views, or move fragmentation/coalescing lower in the
   transport without changing `bbsink` semantics.

3. **Compile out redundant hot-path validation for performance runs after
   correctness is established.**

   Sender-side validation in
   [`HomerServiceValidateOutgoingPayloadObject()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6029),
   transport-header setup in
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6620),
   and receiver-side validation in
   [`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7094)
   are valuable during bring-up. For final steady-state experiments, a
   compile-time performance build can keep only the checks required to avoid
   silent memory corruption. This should be done with explicit debug/perf macros
   so the diagnostic path remains easy to re-enable.

4. **Measure service CPU and frontier behavior with narrow counters before
   blaming the link.**

   The 400 Gbps link/NIC should not be the limiting factor for the observed
   basebackup rates. When a warm RDMA run remains slower than local blackhole,
   prefer counters around `publishedTail`, `payloadSenderPostedTail`,
   `payloadSenderCompletedHead`, `senderVisibleRemoteConsumedHead`, batch size
   histogram, CQ poll count, and receiver doorbells. Avoid high-volume logging
   on the hot path; use final-only counters gated by compile-time macros.

5. **Treat send-CQ polling as a narrowed, not solved, hot-path lead.**

   A diagnostic build with
   `HOMER_SERVICE_PAYLOAD_COMPLETION_POLL_MIN_INFLIGHT=4` cut sender empty-CQ
   polls from about `8.4M` per backup to about `2.5k`, proving the old path was
   doing a large amount of empty polling. The first version also exposed a
   correctness/lifetime problem: if fewer than four objects remained in flight
   at close, sender-side peer-binding cleanup did not run because the tail
   completion was never polled. The code now forces completion polling when
   `localPeerClosePending` is set so deferred close can drain.

   After that fix, warm elapsed time was `4.77` and `4.85s` in the short
   confirmation run, despite correct cleanup and far fewer empty polls. This
   means empty CQ polling is real wasted service-loop work, but simply delaying
   completion polling can also delay source-slot release and does not clearly
   improve end-to-end basebackup throughput at the current `slots=8` geometry.
   Keep the default completion-poll threshold at `1`; revisit this as an
   adaptive scheduler/local-progress policy rather than a fixed threshold.

   The installed tree was restored to the no-stats default build after the
   diagnostics. A final no-stats remote RDMA sanity check after restarting
   PostgreSQL and both Homer services produced cold `8.33s`, then warm `4.94`
   and `4.87s`. That is correct but still above the best earlier warmed
   `4.5-4.8s` band, so do not claim the remaining performance gap is closed.

6. **Move bulk payloads toward a byte-record stream ring.**

   The current payload ring is object-slot based: one semantic object sequence
   maps to one fixed remote payload slot. The sender already RDMA-writes only
   the valid bytes for a partial object, so the substrate is not restricted to
   full-slot writes. The problem is coalescing: when object `N` uses only a
   small prefix of its 8 MiB slot, object `N+1` still lives at the next 8 MiB
   slot address. A single contiguous RDMA write would include the unused gap, so
   the sender correctly refuses to merge those descriptors.

   The target design should decouple three granularities:

   - **Semantic record granularity**: basebackup chunk, tuple batch, WAL record
     group, etc. These should be produced promptly so we keep pipeline overlap
     and later scheduler fairness.
   - **Transport record granularity**: one or more semantic records or record
     fragments packed into a contiguous byte-ring region.
   - **RDMA posting granularity**: one scheduler grant that may post one or more
     RDMA WRs and carry one responder-visible `WRITE_WITH_IMM` publication
     event for the newly visible byte range.

   The producer rule is deliberately the opposite of "form larger objects by
   waiting." The database backend should publish each complete semantic record
   as soon as that record is ready. Waiting in the backend hides ready work from
   the service and repeats the `publish=N` failure mode: better-looking batches
   at the cost of worse pipeline overlap and higher producer reserve waits.

   The batching tradeoff belongs after publication. Once records are visible in
   the Homer substrate, the service/transport scheduler decides how much
   already-ready work to post in this turn. Posting immediately gives the best
   overlap and latency but can create too many WRs and doorbells; granting a
   larger byte range improves software/NIC efficiency but too much delay harms
   overlap. The byte-record ring is useful because it lets us keep small
   semantic granularity while granting larger contiguous transport byte ranges.

   Keep the two scheduling layers separate:

   - **Service progress scheduling** decides how the Homer service spends CPU
     cycles across producer-tail checks, peer-control work, receiver credit,
     send-CQ completions, and active payload streams. This layer should avoid
     wasting CPU, but it should not hide producer work.
   - **Transport egress scheduling** decides the RDMA grant over visible work:
     bytes, records, WR count, age, traffic class, and competing streams. This is
     the right layer for adaptive batching/coalescing policy.

   Proposed data structures:

   ```c
   typedef struct HomerPayloadProducerFrontier
   {
       uint64_t producerReservedTail;     /* local producer owns bytes below this */
       uint64_t producerPublishedTail;    /* complete records visible to service */
   } HomerPayloadProducerFrontier;

   typedef struct HomerPayloadTransportFrontier
   {
       uint64_t transportPostedTail;      /* bytes posted to RDMA */
       uint64_t transportCompletedHead;   /* bytes safe to reuse locally */
   } HomerPayloadTransportFrontier;

   typedef struct HomerPayloadReceiverFrontier
   {
       uint64_t receiverConsumedHead;     /* peer has parsed/delivered bytes */
       uint64_t receiverAckedHead;        /* sender-visible consumed frontier */
   } HomerPayloadReceiverFrontier;

   typedef struct HomerPayloadByteRingDescriptor
   {
       uint32_t ringBytes;
       uint32_t flags;
   } HomerPayloadByteRingDescriptor;

   typedef struct HomerPayloadRecordHeader
   {
       uint32_t magic;
       uint16_t protocolVersion;
       uint16_t objectFamily;
       uint32_t headerBytes;
       uint32_t recordBytes;
       uint32_t semanticHeaderBytes;
       uint64_t streamSequence;
       uint64_t absoluteRecordOffset;
       uint32_t fragmentOrdinal;
       uint32_t fragmentCount;
       uint32_t flags;                    /* begin/end/error/fragmented */
   } HomerPayloadRecordHeader;
   ```

   The structs above are conceptual names, not a mandate to put all frontiers in
   one physical cache line or even one shared-memory object. The current service
   already has separate local payload-ring state and peer head mirrors in
   `HomerPayloadStreamState` in
   `src/backend/distributed/utils/homer/tuple_sink_service_process.c`, and the
   byte-ring implementation should preserve that ownership split. Source-ring
   lifetime and remote receive-ring credit are different windows:

   - **Local source reuse** is governed by local NIC send completion. The
     producer may not reuse bytes until the service has advanced
     `transportCompletedHead` for the RDMA WRs that referenced those bytes.
   - **Remote destination credit** is governed by peer receiver consumption. The
     sender may not post into remote byte-ring space until its sender-visible
     remote consumed head says those destination bytes are free.

   Owner-written frontiers should be cache-line separated where possible:
   producer-only fields, service/transport-only fields, and receiver/ACK-mirror
   fields should not share a hot line. This matters for today's host service and
   more for the eventual DPU version, where every normal-path host-memory poll or
   cache-line bounce can become DMA traffic.

   The byte-ring sender rule is: producers may publish only complete records;
   the transport may post any contiguous byte range ending at or before
   `producerPublishedTail`; the receiver may advance `receiverConsumedHead` only
   after parsing complete records and delivering complete semantic objects upward.
   This preserves typed DB semantics while letting the transport batch already
   ready small/partial records without requiring fixed-slot adjacency.

   RDMA posting options:

   - If ready bytes are contiguous in the local byte ring and contiguous in the
     remote byte ring, post one RDMA WRITE or WRITE_WITH_IMM over that range.
   - If local ready bytes wrap but remote space is contiguous, use multiple WRs
     or scatter/gather if the remote destination is still one contiguous byte
     range. Scatter/gather is a tool for local buffer gathering; it does not by
     itself fix the current fixed-slot remote-layout gap.
   - If either local or remote byte rings wrap, split at the wrap boundary. The
     receiver must not publish/deliver a partial record until all fragments of
     that record are visible.

   The first byte-ring version should be whole-record-only. If a complete
   semantic record will not fit in the remaining contiguous tail space, publish a
   padding/wrap record, advance to offset zero, and write the real semantic record
   at the start of the ring. That avoids introducing record reassembly in the
   first patch. A semantic object larger than the ring, or larger than the
   largest contiguous grant we choose to support, should be rejected or forced
   onto a later explicit fragmentation path. Once fragmentation is implemented,
   object-family parsing should accept a two-slice or iovec-style view so a
   wrapped record is not relinearized into scratch memory.

   Publication and visibility invariants:

   - The backend producer writes the record header and payload first, then
     release-stores `producerPublishedTail`.
   - The Homer service acquire-loads `producerPublishedTail` before building an
     RDMA grant.
   - RC QP ordering plus a final `WRITE_WITH_IMM` makes the byte range visible to
     the peer before the peer observes the publish event.
   - The receiver parses only up to the byte frontier carried by the publish
     event or by the corresponding event metadata; it must not infer readiness by
     scanning stale ring bytes.
   - `magic`, `streamSequence`, and `absoluteRecordOffset` are defensive checks
     against stale headers after wrap and against accidental parsing before the
     intended byte frontier is visible.

   The current `WRITE_WITH_IMM` immediate value is already used to identify the
   stream, so the byte-ring design still needs a bounded way to tell the receiver
   where the newly published byte range ends. The preferred first implementation
   is a receiver-local per-stream `publishedTail` word written with
   `RDMA_WRITE_WITH_IMM`: the write payload is the absolute byte tail, and the
   immediate value identifies the stream. This is not a round trip and not a
   separate metadata write before the notification; it is one small final
   one-way WR after the payload WR chain. It does cost one more WQE than making
   the final payload write itself carry the immediate, but it keeps the payload
   byte stream simple and makes the receiver parse exactly from its local cursor
   up to the published tail. Add an epoch only if later wrap/diagnostic evidence
   shows the absolute byte offset and record checks are insufficient.

   A stream-delimited variant is still possible later: put grant-boundary
   metadata in the byte stream itself, either by mutating a transport-owned bit in
   the final record header or by appending a tiny grant-end marker record. That can
   remove the separate tail WR, but it makes the receiver parser responsible for
   grant-boundary metadata as well as semantic record parsing. The tail/epoch
   `RDMA_WRITE_WITH_IMM` path is the simpler first step.

   Keep the one-RDMA-op publication design as an explicit future optimization.
   If the scheduler grant is contiguous in the local byte ring and lands in
   contiguous free space in the receiver byte ring, the sender can post a single
   `RDMA_WRITE_WITH_IMM` whose payload is the packed byte-stream grant and whose
   immediate still identifies the stream. In that design the receiver learns the
   grant boundary by parsing transport-owned record metadata already present in
   the byte stream, for example a `grantEnd` bit in the final record or a tiny
   grant-end marker record. If either ring wraps, the sender still posts a WR
   chain and puts `WRITE_WITH_IMM` only on the final WR. This path saves the
   extra tail/epoch WR, but it should wait until the byte-ring substrate is stable
   because it deliberately couples receiver parsing to transport grant boundaries.

   Scheduler integration:

   - Ready items should be byte ranges, not just object slots. Each ready item
     carries traffic class, bytes ready, record count, estimated WR count, age,
     and whether the first/last record is fragmented.
   - Initial policy can grant whole records only. Later, for very large basebackup
     or WAL records, allow record fragmentation with explicit reassembly metadata.
   - `WRITE_WITH_IMM` remains the data-publish notification, attached to the
     final WR in the granted byte range. Coalescing reduces notification count;
     it should not replace the notification with remote memory polling.
   - Grants must be clipped by device and implementation limits: send-queue
     depth, maximum WR count per post call, maximum SGE count, maximum message
     size, and the preallocated descriptor space used to build the WR chain.
   - The first policy can be simple and deterministic: grant up to
     `min(readyBytes, localContiguousBytes, remoteCreditContiguousBytes,
     maxGrantBytes, maxGrantWrBytes)`, and force a smaller grant once the oldest
     ready record exceeds an age threshold. That preserves overlap without
     reintroducing producer-side waiting.

   Original basebackup migration plan:

   1. Add a new `HomerPayloadStreamKind`/mode for byte-ring streams alongside
      the existing fixed-slot stream. Do not rewrite tuple/result sinks in the
      first patch.
   2. Implement byte-ring allocation/registration and control descriptors in the
      service/peer transport, mapping bulk basebackup streams to the byte-ring
      mode first.
   3. Keep the producer API semantically object-based, but use a
      reserve-capacity/commit-length rule:
      `reserve maximum record capacity -> fill semantic header/payload -> commit
      actual record bytes -> publish record`. This matches PostgreSQL `bbsink`
      behavior, where `bbs_buffer` is exposed before the caller knows the final
      callback length. The active reservation may temporarily cover more bytes
      than the final record; on commit, the next reservation starts at the actual
      record end rather than leaving fixed-slot slack in the transport layout.
   4. Change the basebackup sender to write each callback into a byte-ring record
      rather than a fixed object slot. Do not pack by waiting; let the transport
      pack already-published records.
   5. Change the RDMA sender pump to build byte-range grants from
      `producerPublishedTail - transportPostedTail`, split only on byte-ring
      wrap, and post one immediate on the final WR for the grant.
   6. Change the receiver pump to parse `HomerPayloadRecordHeader` records from
      the byte ring, call the existing object-family validator/materializer only
      for complete records, and advance receiver credit in bytes.
   7. After basebackup is stable, migrate tuple/result payloads too. This is no
      longer an open question: the fixed-slot tuple/result layout is a temporary
      compatibility layer, not the target substrate.

   Close/error/cancel handling is lifecycle-path logic, not part of the regular
   hot path. The steady-state payload path should assume successful reserve,
   commit, publish, send, parse, and credit return, with debug-only validation
   where useful. Still, the implementation needs cold-path cleanup rules so a
   failed setup or teardown does not leak registered memory or publish stale
   bytes: a producer that reserves but cannot commit cancels the active
   reservation; a normal close publishes an END record and drains outstanding byte
   grants through local send completion before MR deregistration; the receiver
   delivers the END/error record, advances byte credit, and forces the final
   receiver-head ACK so the sender can reclaim the remote-credit window before
   teardown.

   Implementation checkpoint on May 16, 2026:

   - Stage 1 is complete for the current payload families. Basebackup streams
     and tuple/result streams now use byte rings on the producer side and the
     service-to-service RDMA side. PostgreSQL reserves and commits byte-ring
     records; the sender service parses complete records in place, coalesces
     adjacent local/remote byte ranges, and publishes the receiver byte tail
     with a final `RDMA_WRITE_WITH_IMM`.
   - Tuple/result payloads remain tuple-view semantic objects. The byte ring is
     only the neutral payload substrate. `CitusTupleSinkQueueDescriptor` uses
     `CITUS_TUPLE_SINK_QUEUE_DESCRIPTOR_FLAG_BYTE_RING`, while
     `CitusTupleViewContract` continues to describe result shape.
   - `HomerServicePayloadStreamUsesByteRing()` now covers
     `HOMER_PAYLOAD_OBJECT_FAMILY_BASE_BACKUP_STREAM` and
     `HOMER_PAYLOAD_OBJECT_FAMILY_TUPLE_VIEW_BATCH`.
   - Client SQL result queues are byte-ring backed, and the frontend drains them
     through `HomerClientOpenResultSink()` / `HomerClientDrainResultSink()`.
   - Tuple-view service receive uses direct RDMA into the backend-visible
     receive byte ring. The service does not rebuild/copy tuple records on the
     receive side; it only handles doorbells and receiver-head ACKs while the
     worker/frontend tuple sink consumer parses complete records.
   - The old standalone `src/bin/homer_pgbench.c` scaffold has been removed from
     the Citus client-bin/install targets. Integrated `pgbench --homer` is the
     benchmark path for client SQL.
   - The byte-ring checkpoint verifies correctness and warmed remote RDMA
     basebackup in roughly the `4.17-4.25s` band with
     `slots=8,bytes=8388608`; local blackhole warmed repeats were about
     `3.95-4.08s`. After the tuple/result byte-ring migration, a warmed remote
     run measured `4.21s` and a warmed local blackhole run measured `3.96s`; see
     [byte_ring_payload_stream_checkpoint.md](../../../implementations/citus/transport/byte_ring_payload_stream_checkpoint.md)
     for the current code paths, caveats, and measurements.
   - `pgbench --homer` correctness/performance was rechecked after the migration:
     c1/j1 `-t 3000` ran at `5654`, `5679`, and `5784 TPS`; c4/j4 ran at
     `14656`, `15921`, and `15229 TPS`. The same installed binary's libpq path
     ran at `4209-4398 TPS` for c1 and `10003-10966 TPS` for c4.
   - The producer-side fixed-slot WR shape is no longer the next blocker for the
     current basebackup or tuple/result paths. The next performance/design steps
     are transport-side: traffic-class lanes/QPs, RDMA egress scheduling over
     ready byte ranges, possible one-RDMA-op in-band publication, naming cleanup,
     and later fragmentation for oversized semantic records.

   Two-stage full byte-ring migration plan:

   **Stage 1: make byte rings the common payload substrate without changing
   upper-level DB semantics. Status: completed on May 16, 2026 for basebackup
   and tuple/result streams.**

   The goal of this stage was representation cleanup and correctness, not final
   tuning. Tuple/result payloads keep their DB-semantic object identity:
   tuple-view batches remain tuple-view batches, basebackup records remain
   basebackup records, and future WAL streams remain WAL records or record
   groups. The neutral payload stream now carries the implemented object
   families as packed byte-ring records instead of fixed slots.

   - Extend the byte-ring mode currently selected by
     `HomerServicePayloadStreamUsesByteRing()` in
     `src/backend/distributed/utils/homer/tuple_sink_service_process.c` from
     `HOMER_PAYLOAD_OBJECT_FAMILY_BASE_BACKUP_STREAM` to
     `HOMER_PAYLOAD_OBJECT_FAMILY_TUPLE_VIEW_BATCH`, then to every payload
     object family that needs Homer transport.
   - Keep `CitusTupleViewContract` as tuple/result semantic metadata. Do not
     rename tuple-view objects into generic byte streams; the byte ring is the
     container/substrate, while tuple-view batches are one object family inside
     it.
   - Add a tuple-view byte-ring record shape that carries a transport header,
     the tuple-view batch metadata, and row bytes. Prefer reusing the existing
     tuple-view batch header and contract ids instead of inventing a new result
     schema path. The receiver should be able to validate object family,
     protocol version, stream sequence, record length, and contract id before
     delivering rows upward.
   - Replace fixed-slot source reservation for tuple/result send queues. Current
     backend-facing reservation APIs such as `TryReserveCitusTupleSinkBatch()`
     in `src/backend/distributed/utils/homer/tuple_sink_service.c` should become
     wrappers around persistent byte-ring reservation state, or be replaced by a
     byte-ring reserve/commit API. Avoid per-batch allocation on the measured
     path.
   - Replace fixed-slot result-sink draining in the frontend client library.
     `HomerClientOpenResultSink()` and `HomerClientDrainResultSink()` in
     `src/bin/homer_client.c` should map the byte-ring descriptor, parse
     complete tuple-view records, count/materialize rows, and advance byte
     credit. The integrated `pgbench --homer` path in
     `src/bin/pgbench/pgbench.c` should not need semantic changes because it
     already requests `CITUS_REMOTE_EXEC_SQL_RESULT_TUPLE` for row-producing SQL
     and drains result sinks through the Homer client API.
   - Replace the service-to-service fixed-slot sender path in
     `HomerServicePumpOutgoingPayloadStream()` with a byte-ring generic sender.
     Basebackup's current `HomerServicePumpOutgoingByteRingBaseBackup()` should
     become the model: parse complete producer records, validate object-family
     metadata in place, build byte-contiguous RDMA descriptors, and publish the
     receiver byte tail with the current final `RDMA_WRITE_WITH_IMM` rule.
   - Replace the fixed-slot receive path in
     `HomerServicePumpIncomingPayloadStream()` with a byte-ring receiver. This is
     implemented differently for the two current object families: basebackup is
     parsed/blackholed by the service, while tuple-view batch receive aliases the
     receiver byte ring to the backend-visible queue and lets
     `PollCitusTupleSinkBatch()` parse complete records directly. That removes
     the old tuple-batch rebuild/copy tax from the service receive path.
   - Preserve the current byte-ring publication rule from the basebackup
     checkpoint: payload WRs first, then one final tail
     `RDMA_WRITE_WITH_IMM`. The one-RDMA-op grant-boundary record design remains
     a later optimization, not part of the Stage 1 correctness target.
   - Keep producer publication prompt. Backend producers should commit each
     complete tuple-view record as soon as it is ready. Do not introduce
     producer-side waiting merely to form larger tuple batches; batching belongs
     in the service/transport grant over already-visible records.
   - Update lifecycle and close handling so fixed-slot drain tests are replaced
     by byte-frontier drain tests for every payload family. Normal hot-path
     close/error checks should stay out of the steady-state data path, but setup
     failure and teardown must still avoid stale bytes, leaked MRs, or missing
     receiver-head ACKs.
   - Remove dead benchmark scaffolding after the integrated pgbench path remains
     correct. This is complete: the standalone `src/bin/homer_pgbench.c` driver
     was removed from `client-bin` and install targets once `pgbench --homer`
     was confirmed as the canonical benchmark path.

   Stage 1 validation criteria:

   - Basebackup remote RDMA warm runs remain in the current checkpoint band, or
     any movement is explained by counters rather than left as noise.
   - Integrated `pgbench --homer` still runs simple-mode transactions with tuple
     result materialization for `SELECT abalance`; no standalone
     `homer_pgbench` result-discard shortcut is used for claims.
   - The same byte-ring sender/receiver core handles basebackup and
     tuple/result streams, with object-family-specific validation/materializing
     isolated behind dispatch.
   - No hot-path dynamic allocation, heap wrapper creation, or fixed 1 ms sleep
     remains in the tuple/result measured path.

   **Stage 2: optimize after the common byte-ring substrate is in place. Status:
   partially completed; transport scheduler and multiple QPs remain future
   work.**

   This stage should avoid polishing the deleted fixed-slot path. The goal is to
   squeeze overhead out of the byte-ring substrate and make it scheduler-ready.

   Completed in the first post-migration optimization pass:

   - tuple-sink send/receive batch handles are embedded in the sink handle, so
     the measured tuple/result path no longer pays per-batch `palloc`/`pfree`
   - fixed `pg_usleep(1000L)` sleeps were removed from the Citus Homer
     backend/worker wait loops and replaced with CPU-relax spinning
   - the obsolete standalone `homer_pgbench` benchmark scaffold was removed, so
     performance claims use integrated `pgbench --homer`
   - result-sink byte-ring draining validates transport sequence and tuple batch
     headers while advancing byte `consumedHead` directly
   - tuple-view service receive avoids service-side materialization by RDMA
     writing directly into the backend-visible receive byte ring
   - tuple/result queues are now byte-ring-only on the active backend/client API
     path. `OpenCitusTupleSink()`, `TryReserveCitusTupleSinkBatch()`,
     `SubmitCitusTupleSinkBatch()`, `PollCitusTupleSinkBatch()`, and
     `HomerClientDrainResultSink()` no longer carry a measured runtime
     fixed-slot-vs-byte-ring dispatch path.
   - client-SQL result sinks are now allocated/bound by the Homer service under
     the parent client SQL session instead of being backend-created local POSIX
     shm queues.

   Remaining Stage 2 items:

   - Preserve tuple-view semantics as a continuing invariant. This is not a
     request to make tuple results generic byte blobs: `CitusTupleViewContract`
     remains the semantic schema contract, and tuple-view batch records remain
     the payload object. Only the physical queue backing is unconditional
     byte-ring storage.
   - Keep any remaining fixed-slot-era protocol/control structures only if they
     are genuinely needed for command/control compatibility or historical
     scaffolding. Payload data structures should not reintroduce a slot-vs-byte
     runtime branch in the measured path.
   - Add low-overhead counters under compile-time macros for each payload
     family: produced records, produced bytes, ready bytes at service pickup,
     RDMA grants, WRs per grant, bytes per grant, coalesced ranges, wrap splits,
     send-CQ completions, empty CQ polls, remote-credit waits, local source
     completion waits, and receiver doorbells. Keep final summaries cheap and
     avoid logging inside per-record loops.
   - Tune transport grants over byte ranges, not object slots. The first policy
     should grant already-visible contiguous bytes up to the minimum of local
     contiguous bytes, remote contiguous credit, device/implementation WR caps,
     and a configurable `maxGrantBytes`. Add an age threshold so a small
     latency-sensitive record is not held indefinitely behind a desire for a
     larger batch.
   - Reuse basebackup lessons across tuple/result and WAL paths: producer
     publication should stay prompt, service-side batching should operate on
     already-ready bytes, coalescing should require byte-contiguous local and
     remote ranges, and send completions should release source bytes in batches
     without delaying close.
   - Remove remaining broad copy costs in the tuple/result materializer:
     full-struct clears, null-bitmap resets for untouched fields, byte copies
     into identical layouts, and temporary ownership conversions. Use tuple-view
     contracts to describe existing bytes whenever possible.
   - Preallocate descriptor arrays, completion trackers, and result-drain state
     at stream/session setup. Per-grant stack arrays are acceptable for fixed
     small caps, but there should be no malloc/palloc/free cycle in steady-state
     tuple/result transport.
   - Revisit publication style only after counters show the final tail
     `RDMA_WRITE_WITH_IMM` is material. If it is, evaluate the one-RDMA-op
     grant-boundary design where the final payload WR carries `WRITE_WITH_IMM`
     and the receiver learns the grant end from transport-owned metadata in the
     byte stream. Do not make this part of the correctness migration.
   - Integrate the scheduler abstraction over ready byte ranges. Ready items
     should carry traffic class, priority/weight, ready bytes, record count,
     estimated WR count, age, and lane/QP placement hints. The scheduler returns
     a byte/record/WR grant; the sender consumes that grant without copying the
     payload object.
   - Introduce traffic-class transports and multiple QPs after the common
     byte-ring path has stable counters. Start with one QP behind the new
     abstraction only as a correctness checkpoint, then split critical control,
     foreground tuple/result payload, and bulk basebackup payload so posted-WR
     head-of-line blocking can actually be reduced.
   - Measure warmed steady state only. The first run after service restart,
     rebuild, memory registration change, or Postgres restart is setup/warmup
     evidence, not the main performance number.

   Stage 2 validation criteria:

   - Basebackup remote RDMA warm runs are no worse than the Stage 1 baseline and
     should move closer to local blackhole if transport overhead was removed.
   - Single-client `pgbench --homer` remains at least on par with the pre-migration
     Homer band once result semantics are matched.
   - Multi-client `pgbench --homer` scales without reintroducing a shared
     fixed-slot or peer-control bottleneck.
   - Counter evidence shows larger effective RDMA grants or fewer WRs/doorbells
     without increased producer waiting or worse tail latency.

   Remaining open decisions:

   - Whether basebackup records should include the existing
     `CitusRemoteBaseBackupMessageHeader` as the semantic header unchanged, or
     fold common fields into `HomerPayloadRecordHeader`.
   - How to evolve the current PostgreSQL shared-memory producer byte ring into
     a DPU-visible DMA allocation abstraction. The control words should continue
     to be designed as DMA-polled fields even while the current implementation
     uses host shared memory.
   - Whether a later optimization should remove the final tail/epoch
     `RDMA_WRITE_WITH_IMM` by carrying grant-boundary metadata in the byte stream
     itself. The first implementation should prefer the explicit tail/epoch word
     unless measurements show that one extra WQE is a dominant cost after
     byte-ring packing.
   - Exact default values for `maxGrantBytes`, `maxGrantWrBytes`, and the age
     threshold. These should be tuned with counters for effective grant size,
     WRs per grant, completion count, remote-credit wait time, and producer
     reserve wait time.

   Evidence run on May 15, 2026: a stats-only sender shape run with
   `slots=8,bytes=8388608` produced three successful remote RDMA basebackup
   runs at `5.02s`, `4.95s`, and `4.84s`. The object distribution was stable:

   - `6484` total semantic objects
   - `6477` archive chunk objects
   - `1` manifest chunk object, about `308 KiB`
   - `6` zero-payload control objects: begin/archive-begin/archive-end/
     manifest-begin/manifest-end/end
   - `2746` full-slot objects
   - `3738` partial-slot objects
   - size buckets among all objects: `6` zero, about `2268-2269 <=4 KiB`,
     about `1353-1354 <=64 KiB`, `93 <=1 MiB`, `17` large partial, and
     `2746` full

   This strongly supports the byte-ring direction. The partial objects are not
   just rare terminal records; thousands are small or medium archive chunks.
   Fixed-slot coalescing cannot collapse those without writing gaps. Producer
   packing could reduce object count, but if implemented as waiting for larger
   producer batches it repeats the `publish=N` problem and sacrifices overlap.
   A byte-record ring keeps small semantic records visible promptly while
   letting the transport/scheduler pack already-ready bytes into larger RDMA
   grants.

### How this informs other code paths

- For client SQL result sinks, avoid per-command setup/free in the hot path and
  keep session-owned result queues reusable, following the same frontier split
  principle as payload streams.
- For future WAL replication, do not expose shared WAL pages directly to RDMA
  without a source-lifetime frontier equivalent to
  `payloadSenderCompletedHead`. Posted-to-NIC and reusable-by-PostgreSQL are
  different events.
- For tuple-view batches, preserve the typed object contract, but let the
  transport coalesce ready adjacent slots and piggyback publication events below
  that contract.
- For the future scheduler, do not use it as a substitute for basic hot-path
  efficiency. The scheduler should decide between already-efficient traffic
  classes; it should not have to compensate for extra WRs, avoidable polling, or
  stale-code benchmark artifacts.

## Session-local command channels and QP co-design

Concurrent client-SQL sessions exposed a related but host-local bottleneck
before the RDMA scheduler was even involved: frontend commands entered one
shared local control-slot queue, the single service loop relayed each command to
the target socketless backend, and the service later relayed backend completion
metadata back to the frontend. That original path is documented in
[concurrent_pgbench_client_sessions.md](../../postgres/client-sql-session/concurrent_pgbench_client_sessions.md).

Implementation progress on May 14, 2026: the local client-SQL path now uses the
first session-local channel mapping. The service still opens/closes sessions,
creates the command/completion mailboxes, and spawns the socketless backend, but
normal pgbench commands are written directly from the frontend into the
per-session backend command mailbox and backend completions are read directly
from the per-session backend completion mailbox. The RDMA peer-control and
payload-stream mappings below remain future work.

The target abstraction should treat command and completion paths as
session-local transport channels:

```text
logical session channel
  direction: frontend -> backend command, backend -> frontend completion,
             service -> service peer control, or payload stream
  traffic class: control critical, control normal, foreground payload,
                 bulk payload, maintenance
  physical mapping: shared-memory mailbox today; RDMA MR/QP/QP-pool later
```

That keeps the near-term pgbench fix aligned with the later multiple-QP work.
The command-channel API should not expose a global service queue, and it should
not expose "one QP per session" as the semantic model. The scheduler/transport
layer owns the mapping from logical session channels and traffic classes to
physical QPs. Candidate mappings include:

- local host prototype: one command mailbox and one completion mailbox per
  client SQL session
- service-to-service control: multi-slot peer-control ring plus peer completion
  ring on the critical-control lane
- foreground tuple/result payloads: foreground-payload QP or QP pool
- basebackup and future WAL bulk streams: bulk-payload QP or QP pool

This co-design matters for performance: removing the shared service command
relay fixes local multi-client pgbench serialization, while keeping the logical
channel descriptor traffic-classed makes the later RDMA implementation able to
avoid posted-WR head-of-line blocking without changing the remote-execution
session semantics again.

Result sinks are intentionally not part of this immediate command-channel
rewrite. Local client-SQL result payload already moves through a session-owned
queue that the frontend drains directly; the command-completion channel merely
carries the sink descriptor and tuple-view contract. When completions become
direct backend-to-frontend events, that descriptor must still be delivered, but
the sink payload path itself does not need to be routed through the service
control queue.

## Large-object fragmentation

The first scheduler can grant whole semantic objects only. That avoids adding
fragment headers and reassembly before the rest of the scheduler is stable.

However, the long-term design should not forbid slicing a very large semantic
object. Large basebackup objects may be too coarse as the minimum scheduling
unit. With RC QPs, writes posted on one QP are observed by the receiver in order
for that QP, but the upper layer should still not see an incomplete object.

Future fragmentation rule:

- the RDMA substrate may publish fragments of one large object
- fragments include object id, fragment offset, fragment length, and final flag
- the receiver-side substrate tracks object assembly state
- the object family callback is invoked only after the full semantic object is
  available
- fragmentation is hidden below tuple/basebackup/WAL object consumers

This can be deferred until scheduler experiments show object granularity is too
coarse. The scheduler and QP design should nevertheless leave room for it by
granting bytes and WRs in addition to object counts.

## Implementation sequence

1. **Service progress fix - completed as a transitional primitive**
   - introduce `TupleSinkServicePumpOnce(mask)`
   - use it from the main loop and the client SQL local wait loop
   - keep local control disabled while already handling one local control slot
   - remaining direction: remove nested waits from normal paths rather than
     expanding the mask policy

2. **Async command submission - local frontend completed, peer still future**
   - make local and peer `START_COMMAND` handlers publish work and return
     immediately
   - push `STARTED`, result-sink readiness, `COMPLETED`, and `FAILED` through
     completion channels
   - remove local and peer startup waits from normal paths
   - add the peer completion ring before deleting peer completion polling

3. **Neutral payload stream refactor - completed May 14, 2026**
   - split generic stream state from tuple object-family state
   - move `CitusTupleViewContract` behind tuple object-family state
   - keep basebackup header validation behind a basebackup object-family callback
   - internal table/pump names now use `HomerServicePayloadStream*`
   - remaining naming work is protocol/API cleanup: `serviceSinkId`,
     `CitusTupleSink*` wire structs, and the service binary/header names still
     reflect the original tuple-only prototype

4. **Traffic-class metadata and transport API - completed May 14, 2026 for the
   single-lane checkpoint**
   - added explicit payload-stream `trafficClass`, `priority`, `weight`,
     estimated bytes, estimated WR count, object count, and grant metadata
   - kept `executionLane` as deprecated session-compatibility input rather than
     turning it into a scheduler primitive
   - routed control and payload publication through a traffic-class transport
     API instead of direct service code access to one peer connection QP
   - intentionally maps all traffic classes to the existing lane/QP to preserve
     behavior
   - verified no correctness or measured performance regression on
     single/multi-client Homer pgbench and remote RDMA basebackup smoke; see the
     implementation-progress note above for exact numbers

5. **Basebackup byte-ring substrate - producer and service-to-service
   checkpoint completed May 16, 2026**
   - implemented receiver-owned `CitusHomerPayloadByteRingControl` and
     `CitusHomerPayloadByteRingDescriptor`
   - exchanged the byte-ring descriptor during peer open for basebackup streams
   - replaced the backend-visible fixed producer queue with a producer-owned
     byte-ring record reservation/commit API for basebackup
   - added a basebackup sender that parses producer byte-ring records, coalesces
     adjacent local/remote byte ranges, and publishes an absolute receiver byte
     tail with `RDMA_WRITE_WITH_IMM`
   - added a byte-ring receiver that parses complete records, skips wrap
     trailers, and returns byte-credit ACKs
   - verified warm remote RDMA basebackup correctness/performance in the
     `4.2-4.3s` band after the final producer header-clear cleanup
   - remaining work: transport lanes/QPs, real egress scheduling, optional
     one-RDMA-op publication, and future fragmentation/reassembly

6. **Traffic-class peer transport lanes**
   - split peer transport resources by traffic class
   - use one critical-control lane per peer transport initially
   - replace the one-message peer-control mailbox with a multi-slot
     peer-control request ring plus peer completion/event ring on that lane
   - map foreground payload and bulk payload to separate lanes/QPs; keep lane
     pools possible for high-throughput payload classes
   - preserve the existing WRITE_WITH_IMM publication rule per QP/channel

7. **Scheduler prototype**
   - implement ready-set construction, policy selection, grants, and progress
     accounting
   - start with a trivial strict policy: critical control, then foreground
     payload, then bulk payload
   - cap sender payload publication by scheduler grant instead of only by
     `HOMER_SERVICE_PAYLOAD_SEND_COMPLETION_BATCH`
   - add weighted/deficit/age-aware policies only after traffic-class lanes are
     measurable

8. **Push command completions - local frontend completed, peer still future**
   - keep one in-flight command per session and use a small completion ring for
     the local frontend no-poll implementation
   - bind the local frontend completion destination during session/open setup
   - publish terminal completion from the producing side instead of requiring a
     separate `POLL_COMMAND_COMPLETION` request
   - add a small fixed-size peer completion ring for service-to-service push
     completions before removing peer completion polling from the normal path
   - retire `terminalCompletionPendingPeerPoll` once delivery acknowledgement is
     tied to the push destination instead of a future poll
   - retain poll only as a fallback/debug path until the push path is validated

9. **Optional fragmentation**
   - add RDMA-substrate fragmentation only if object-level grants are too coarse
     for latency/throughput goals

## Related

- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md): responder-visible RDMA publication rule this scheduler must preserve.
- [rdma_transport_control_plane_abstraction.md](rdma_transport_control_plane_abstraction.md): broader transport/control abstraction context.
- [command_dispatch_completion_plane.md](../data-movement/command_dispatch_completion_plane.md): command lifecycle and completion plane that should move from polling toward push completion.
- [base_backup_service_abstraction.md](../../postgres/replication/base_backup_service_abstraction.md): basebackup object-family semantics above the neutral payload stream.
