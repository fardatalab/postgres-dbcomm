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

The design should allow multiple QPs per peer transport from the start.

One QP can preserve correctness, but it cannot reorder work already posted to
that QP. If the service posts a long basebackup WR chain and then a tiny control
message becomes ready, the control message cannot jump ahead on the same QP.
That is posted-WR head-of-line blocking. It is logically real even before
measurement.

Target peer transport shape:

```c
HomerPeerTransport
{
    controlCriticalQp;
    foregroundPayloadQp;
    bulkPayloadQp;
    maintenanceQp;          /* optional */
}
```

The scheduler chooses a traffic class and grant. The transport maps that class
to a QP. Link-level contention still exists, but a control write is no longer
queued behind already-posted bulk WRs in the verbs work queue.

The first implementation may keep one QP internally while the scheduler API is
introduced, but the API must not bake in "one peer == one QP." The right
abstraction is "one peer has one or more traffic-class transports."

Multiple QPs within a traffic class should also remain possible for throughput
experiments, especially for bulk basebackup or future WAL streams.

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
  ring on a control-critical QP
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

4. **Scheduler metadata**
   - add explicit `trafficClass`, `priority`, `weight`, and grant hints to
     sessions/streams/commands
   - treat `executionLane` as deprecated compatibility input

5. **Single-QP scheduler prototype**
   - implement ready-set construction, policy selection, grants, and progress
     accounting
   - cap sender payload publication by scheduler grant instead of only by
     `HOMER_SERVICE_PAYLOAD_SEND_COMPLETION_BATCH`

6. **Multi-QP peer transport**
   - split peer transport resources by traffic class
   - map scheduler class selection to the appropriate QP
   - preserve the existing WRITE_WITH_IMM publication rule per QP/channel

7. **Push command completions - local frontend completed, peer still future**
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

8. **Optional fragmentation**
   - add RDMA-substrate fragmentation only if object-level grants are too coarse
     for latency/throughput goals

## Related

- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md): responder-visible RDMA publication rule this scheduler must preserve.
- [rdma_transport_control_plane_abstraction.md](rdma_transport_control_plane_abstraction.md): broader transport/control abstraction context.
- [command_dispatch_completion_plane.md](../data-movement/command_dispatch_completion_plane.md): command lifecycle and completion plane that should move from polling toward push completion.
- [base_backup_service_abstraction.md](../../postgres/replication/base_backup_service_abstraction.md): basebackup object-family semantics above the neutral payload stream.
