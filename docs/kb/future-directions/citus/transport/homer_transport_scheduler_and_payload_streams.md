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
messages over a persistent RDMA control mailbox. The mailbox is now a shared
fixed-width multi-slot ring, and normal remote open/start/poll callers use the
explicit async op substrate. The earlier close-specific peer-control sender has
been removed; finite tuple-result close/EOS is now represented in-band in the
payload stream and the local cleanup helper
[`TupleSinkServicePeerCloseSinkBestEffort()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8076)
no longer sends a remote close request. The common peer-control publication point is
[`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3975).

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

Historical note: this section was originally written when peer control used a
one-message waiting latch. That limitation has been removed. The current
transport has a shared multi-slot control ring, explicit op-id/generation state,
and async start/poll APIs:
[`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6928)
and
[`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6967).
The close/EOS ordering cleanup is complete for client SQL tuple results. The
remaining work is scheduler policy plus removing wait/poll scaffolding from the
older backend/backend peer command path, not rebuilding a generic blocking
request/response mailbox fallback.

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

### Client SQL command/completion transport optimization checkpoint

The farnet0-to-farnet1 client SQL path has reached correctness, but the current
remote command/completion substrate still leaves avoidable work on the
transaction hot path. The detailed pgbench-facing plan is in
[`../../../postgres/client-sql-session/client_sql_session_offload_plan.md`](../../../postgres/client-sql-session/client_sql_session_offload_plan.md);
the transport-level requirements are:

- command and completion mailboxes should become small power-of-two registered
  rings instead of a single source/scratch record
- source slot lifetime should be retired by deferred send-CQE checkpoints; an
  initial policy of 64 slots with a signaled checkpoint every 32 posted slots is
  sufficient to bound SQ pressure while avoiding per-command CQ waits
- RDMA writes should use stable registered mailbox slots as source buffers, not
  intermediate service scratch copies, whenever lifetime allows
- compact typed layouts should replace the current full-width command and
  completion records for no-result lifecycle/update commands
- for backend-polled client SQL command rings, `RDMA_WRITE_WITH_IMM` should not
  be required on the normal hot path; the backend should poll a per-slot
  `ready_seq == expected_seq` value instead of waiting for receiver-service CQ
  progress
- `RDMA_WRITE_WITH_IMM` remains appropriate for peer-control, payload stream
  publication, rare wakeup/debug paths, and future DPU scheduler events; if used
  for commands, the immediate value is only a routing hint and full validation
  stays in the slot header
- one-RDMA-write command publication is a fast path gated by receiver-side
  in-order data placement (`ibv_query_qp_data_in_order(qp, IBV_WR_RDMA_WRITE,
  0) == 1`) and non-relaxed target MRs; otherwise the multi-slot ring should use
  a conservative payload/header write plus a small ready write
- sink-ready result metadata should be pretranslated during stream setup/bind
  when possible, so publishing a completion event does not rewrite large
  descriptor/contract state in the hot path
- sink-ready and terminal completion can be coalesced only when runtime stream
  state proves the result has already reached EOS; large or still-active streams
  must continue to publish sink-ready early so payload draining overlaps backend
  execution

This checkpoint does not replace the multiple-QP/scheduler milestone. It is the
single-lane cleanup that should happen first, so later scheduler measurements do
not confuse avoidable command-plane copies and per-event CQ waits with real
traffic-class policy effects.

May 17, 2026 progress: the first successful single-lane cleanup is prefix-sized
remote completion publication. The peer service now RDMA-writes only the hot
prefix for no-result successful completions, and terminal EOS events no longer
repeat result descriptor/contract bytes after an ordered sink-ready event has
already delivered that metadata. This improved warmed real-RDMA c4 pgbench from
the previous `~4.8k TPS` / `~7.9 ms` p99 checkpoint to stable repeats around
`10.3k TPS` / `0.67 ms` p99. The attempted backend-side sink-ready coalescing
for one-row pgbench results was reverted because it lost payload-drain overlap
and regressed c1 to about `4.0k TPS`. The detailed measurements and code
pointers are in
[`../../../postgres/client-sql-session/client_sql_session_offload_plan.md`](../../../postgres/client-sql-session/client_sql_session_offload_plan.md).

Follow-up on the same day: the backend command mailbox record was cleaned up so
typed command payloads live in one hot union before cold result metadata. That
layout cleanup is kept because it matches the typed-command semantics and avoids
preserving a structurally bad sequential-spec layout. However, the narrower
variable-length command RDMA write was left disabled by default through
`HOMER_SERVICE_COMPACT_CLIENT_SQL_COMMAND_RDMA=0`: it was correct but reduced c4
from the `~10.3k TPS` band to about `9.6k TPS`. The useful conclusion for the
transport scheduler work is that command-byte volume is not the current
bottleneck by itself; the next command-plane optimization should reduce publish
WR count and source-slot lifetime together, preferably via the planned
multi-slot mailbox plus deferred send-CQE retirement and one-doorbell publish.

Another same-day A/B tried command-record `RDMA_WRITE_WITH_IMM` as the only
cross-node command publication WR. The sender wrote the command record and used
the immediate event to make the receiver service publish the peer-local backend
mailbox epoch with a host-local store. This was correct, but it lost to the
direct epoch-publish path: warmed farnet0 -> farnet1 pgbench measured about
`4664 TPS` c1 and `9962 TPS` c4 with the immediate path, versus about
`4691 TPS` c1 and `10433 TPS` c4 after returning to direct RDMA epoch publish.
Optimization pass: the receiver now uses a compact receiver-local
`peerCommandDoorbellToken`, pops pending command doorbells in O(1), batch-reposts
notification receive slots only when the recv CQ actually returns a batch,
publishes `mailbox->record.commandSequence` instead of inferring
`consumedEpoch + 1`, purges pending command doorbells on session reset, and
shrinks `ActiveSessionScanLimit` when high slots are reset. That recovered c1 to
the direct-publish band (`4647 TPS`, p99 `0.236 ms`) but c4 stayed lower
(`10084 TPS`, p99 `0.656 ms`) than the restored direct path (`10632 TPS`, p99
`0.668 ms` in the same follow-up run shape). The immediate path is kept as a
compile-time experiment through
`HOMER_SERVICE_CLIENT_SQL_COMMAND_IMM_PUBLISH=0`, but the default remains the
two-WR command publish. A follow-up ordering fix made the immediate drain publish
the backend-visible epoch before receive-WQE reposting via
[`TupleSinkServiceDrainPeerConnectionCommandDoorbellsRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2583),
but warmed remote pgbench remained essentially unchanged (`4636 TPS` c1 and
`10100 TPS` c4). Keep that better ordering principle for any future
receiver-service fallback, but it does not remove the receiver-service CQ
dependency.

The important lesson is that the one-WR idea is still plausible, but this
implementation shape is not the right proof point. It saves a tiny direct RDMA
epoch write on the sender, then adds a receiver-service hop: recv CQ polling,
notification WQE reposting, command-doorbell routing, and a host-local
`publishedEpoch` store before the backend can observe the command. A future
one-doorbell design needs a mailbox/ring layout where one RDMA publication makes
the backend-visible command epoch safe without receiver-service participation,
or a multi-slot command ring with deferred source-slot retirement so receiver
software can be amortized across a real batch. The refined plan is to implement
the multi-slot command ring first, then add a self-publishing slot fast path:
`[header][payload][ready_seq footer]` in one RDMA write only when whole-message
in-order placement is reported and the target MR is not relaxed-ordered. In that
fast path the backend polls memory directly, so command `WRITE_WITH_IMM` is not
needed for notification.

A direct-source command attempt also failed correctness and was reverted. The
idea was to register the frontend command mailbox itself as the RDMA source and
avoid copying the command record into service-owned scratch. The problem is
lifetime ordering: remote completion can reach pgbench before the sender service
has observed its local send fence and released the frontend command slot, so the
next pgbench command sees `published != consumed` and aborts with a busy
mailbox. This validates the current scratch-copy design despite its copy cost.
Removing that copy requires a real multi-slot source ring with deferred
send-CQE retirement, not a single-slot source borrowed directly from the
frontend command mailbox.

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

June 2, 2026 design checkpoint: the two layers should be implemented through a
shared scheduling framework, not as two unrelated local mechanisms. There are
three information flows:

1. **Readiness publication**: payload, command, control, completion, and ACK
   producers expose that DB-semantic or transport work is visible. This should be
   frontier-plus-bitset metadata, not heap work items per object. The normal rule
   remains prompt producer publication: do not hide complete records in the
   database/backend producer just to form a larger batch.
2. **Egress grants**: the scheduler grants a ready item a bounded amount of RDMA
   posting work in bytes, records/objects, fragments, and WRs. The specialized
   sender still knows how to validate and post its object family; the scheduler
   controls how much it may post in this pass.
3. **Feedback/accounting**: lanes and streams report completion pressure,
   outstanding signaled WRs, remote-credit blockage, empty-CQ polling, and useful
   progress. This is the information that prevents local shortsightedness between
   the service-progress layer and the RDMA-egress layer.

The forced-fragmentation implementation exposed an important frontier rule for
the scheduler interface: **completion progress is not the same as semantic/source
frontier progress**. The byte-ring sender now tracks signaled payload batches in
`HomerServiceTrackPayloadCompletion()` at
`src/backend/distributed/utils/homer/tuple_sink_service_process.c:2210` and
drains completions while `payloadCompletionCount > 0` in
`HomerServicePumpOutgoingPayloadStream()` at
`tuple_sink_service_process.c:9722`. That is necessary because a partial
fragment's send CQE can retire WRs, generated-header slots, and QP pressure
without advancing the producer source `consumedHead`; the producer object is only
safe to release after the `FRAGMENT_LAST` batch retires. Therefore a future
scheduler-integrated loop should model at least four separate progress frontiers:

- **transport completion/resource frontier**: local NIC send CQEs have retired
  WRs and any lane-owned temporary publish/header resources
- **source-release frontier**: source ring bytes/slots may be returned to the DB
  producer
- **semantic-object frontier**: complete tuple/basebackup/WAL objects are posted,
  delivered, or materialized
- **remote-credit frontier**: receiver-consumed bytes/slots have been published
  back to the sender

Service-progress grants should be able to make useful progress by draining CQEs
even when no source bytes become reusable in that pass. Executor feedback should
therefore report which frontier moved, not just a single `madeProgress` bit. The
first implementation can keep simple booleans/counters, for example
`completionEvents`, `resourceCreditsFreed`, `sourceBytesReleased`,
`semanticObjectsCompleted`, `remoteCreditsReceived`, and `emptyPolls`. This keeps
the scheduler from under-prioritizing CQ drain work merely because the semantic
object frontier did not advance, and it preserves the DPU-facing distinction
between local resource retirement and DB-visible object progress.

The eventual DPU version makes the readiness path especially important. If the
payload/control rings live on DPU memory, readiness bitsets can be DPU-local and
cheap, and they do not need host atomics for the normal case. If a producer still
publishes from host memory, the design should avoid DMA-updating one shared bitset
or cache line per object. Prefer owner-written frontiers plus idempotent ready
bits, with notification coalescing only at the metadata level. Coalescing
readiness metadata is allowed; delaying producer publication of complete semantic
records is not the default policy.

Commands and control already have traffic classes and lanes, but that is not the
same as being scheduler-integrated. The current service loop still progresses
command/control through dedicated pump logic in
[`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15170).
The target is that command writes, completion writes, peer-control requests,
payload writes, and ACK/maintenance work all present scheduler-ready items and
consume grants. The first implementation may schedule only payload egress, but
the abstraction must not make command/control a permanent side path.

Service-progress scheduling should use a cheaper view than transport-egress
scheduling. It should not run a heavy policy calculation per RDMA WR. It should
decide which source to touch and for how long using small lane/source facts:
ready bit, traffic class, last useful progress tick, blocked-on-credit flag,
outstanding CQ work, bounded per-pass quantum, and starvation/debt counters.
Transport-egress scheduling then decides the RDMA grant using byte/object/WR
facts. In other words, both layers share readiness and feedback metadata, but
they do not need identical decision variables.

The service-progress scheduler should produce a **progress plan**, not an egress
batch. A progress plan is a small fixed array of CPU/progress grants such as
"poll this lane CQ up to 8 completions," "process this command ring up to 16
slots," or "pump this payload stream once." This is what "called once per service
pass or per small batch" means: the policy should be invoked once to select a
bounded set of progress actions, not once per RDMA WR. The transport-egress
scheduler is then called by the sender/progress action to decide byte/object/
fragment/WR grants.

Cheaper service-progress facts:

```c
typedef enum HomerProgressSourceKind
{
    HOMER_PROGRESS_SOURCE_RDMA_CM,
    HOMER_PROGRESS_SOURCE_PEER_CONTROL,
    HOMER_PROGRESS_SOURCE_COMMAND_RING,
    HOMER_PROGRESS_SOURCE_COMPLETION_RING,
    HOMER_PROGRESS_SOURCE_PAYLOAD_STREAM,
    HOMER_PROGRESS_SOURCE_CQ_DRAIN
} HomerProgressSourceKind;

typedef struct HomerProgressSourceId
{
    uint16 kind;
    uint16 index;
    uint16 generation;
    uint16 reserved;
} HomerProgressSourceId;

typedef struct HomerProgressSourceRef
{
    HomerProgressSourceId id;
    void *owner;
} HomerProgressSourceRef;

typedef struct HomerProgressSourceCore
{
    HomerProgressSourceRef source;
    HomerTransportTrafficClass trafficClass;
    bool ready;
    bool creditBlocked;
    bool signaledWrPending;
    bool resourcePressure;
    bool sourceReleaseBlocked;
    uint16 quantumHint;
    uint32 outstandingSignaledWrCount;
} HomerProgressSourceCore;

typedef struct HomerProgressSourceFeedback
{
    uint16 emptyPollScore;
    uint32 outstandingWrCount;
    uint32 readyObjectCountHint;
    uint64 readyByteCountHint;
    uint64 transportCompletionFrontier;
    uint64 sourceReleaseFrontier;
    uint64 semanticObjectFrontier;
    uint64 remoteCreditFrontier;
    uint64 lastTouchedTick;
    uint64 lastProgressTick;
} HomerProgressSourceFeedback;
```

The facts above are deliberately source/lane level. They do not carry full
payload byte ranges, tuple contracts, basebackup headers, or per-object work
records. `kind/index/generation` is a compact typed handle into fixed per-kind
arrays, not a hash/map lookup key. The hot scheduler ref also carries `owner`, so
the executor can use a direct pointer after readiness construction validates the
generation. If measurements show generation checks are too expensive, keep them
at source registration / ready-list construction or compile them under a debug
gate; do not add a runtime lookup table to the hot executor path.

Split the source state in implementation for cache locality and policy
neutrality, not because fixed priority is the only target. `HomerProgressSourceCore`
is the compact, frequently read routing/control state that every policy needs:
ready bit, traffic class, blocked flags, quantum hint, outstanding signaled WR
count, and the direct owner pointer. `HomerProgressSourceFeedback` is the
canonical executor feedback: counters, byte/object hints, frontiers, empty-poll
score, and last-touch/progress ticks. It is updated when executors report
movement, but each policy chooses the subset it reads. A fixed-priority policy
can read mostly `Core`; DRR can read byte/WR feedback and its own deficit state;
age/deadline policies can read ready-since or last-progress facts. Policy-private
mutable state such as round-robin cursors, deficit counters, weights, and
deadline parameters should live outside both canonical structs in
policy-specific state.

The four frontier fields are optional per source kind: a CQ-drain source may
mainly expose `transportCompletionFrontier`, while a payload source exposes all
four. Keeping them in the same feedback vocabulary prevents the policy from
treating "CQ drained but source object not yet releasable" as no progress. ACK
credit is not a separate first-class source in the first scheduler design; it is
a lane-CQ/transport-resource event kind that updates remote-credit or
transport-completion facts for the affected owner.

Progress grants should also be small:

```c
typedef struct HomerProgressGrant
{
    HomerProgressSourceRef source;
    uint16 maxPolls;
    uint16 maxItems;
    uint16 maxPumpCalls;
    uint16 flags;
} HomerProgressGrant;

typedef struct HomerProgressPlan
{
    uint16 grantCount;
    HomerProgressGrant grants[HOMER_PROGRESS_PLAN_MAX_GRANTS];
} HomerProgressPlan;
```

Readiness and planning are two separate data structures:

- **Ready bitsets** are persistent per-kind/per-traffic-class sieves. A set bit
  means "this source may have useful work"; a clear bit means "do not touch this
  source in the normal service pass." Bits are set/cleared from authoritative
  owner-written frontiers, not from per-object heap work items.
- **Ready arrays / progress plans** are stack-local bounded work orders for one
  service pass. The fixed-priority policy takes a limited number of set bits,
  validates generation, resolves the direct owner pointer, checks cheap hot
  facts such as blocked flags and outstanding signaled WRs, emits grants, and
  stops when the plan or policy budget is full. It should not build grants for
  every ready source on every pass.

For the first fixed-priority policy, grant construction should be simple: scan
priority-class bitsets in order, choose a bounded number of sources, fill
`HomerProgressGrant` with the hot source ref and small quanta, then execute the
stable plan. Executors may update readiness while running, but the current pass
does not recompute policy per RDMA WR or per object.

Scheduler-facing executors should return frontier-specific progress, not a
`bool madeProgress` result. There is no target compatibility-wrapper layer: the
scheduler-integrated service loop should consume this rich result directly, and
any loop-level "did anything happen" decision should be derived locally from the
result fields rather than exported as an executor API:

```c
typedef struct HomerProgressResult
{
    bool stillReady;
    bool blockedOnCredit;
    bool blockedOnLocalResources;
    uint16 emptyPolls;
    uint16 cqesDrained;
    uint16 itemsProcessed;
    uint16 wrsPosted;
    uint64 bytesPosted;
    uint64 transportCompletionFrontier;
    uint64 sourceReleaseFrontier;
    uint64 semanticObjectFrontier;
    uint64 remoteCreditFrontier;
} HomerProgressResult;
```

This result is stack-local per executor call. It is not a heap event object and
does not need to preserve a history. The service loop folds it back into the
persistent core/feedback state for that source/lane. Do not carry
`bool madeProgress` into the scheduler source state, policy input, or executor
API. Executor function return values should instead mean success/failure, with
all progress facts reported through the result object.

Candidate service-progress policies:

- **fixed priority bounded**: critical control/completion feedback first, then
  foreground, then bulk, with a hard per-source quantum. This is the first
  implementation target.
- **round robin over active sources**: useful as a fairness sanity check when
  fixed priority over-favors one class.
- **deficit over CPU quanta**: sources accumulate progress debt in units such as
  poll calls, command slots, or pump calls rather than bytes.
- **feedback-aware polling/backoff**: sources with repeated empty polls are
  skipped for a few passes unless another readiness bit/frontier changes.
- **foreground-biased progress**: foreground commands/results get lower latency
  while bulk still receives bounded progress.

This policy surface can be pluggable, but it should remain much narrower and
cheaper than the egress policy surface. It is a CPU-use strategy, not the primary
research policy for RDMA byte/WR allocation. The first version should compile to
simple branches over active bitsets/fixed arrays and should avoid heap
allocation, broad scans, and per-WR callbacks.

Eventual scheduler-enabled Homer plan:

1. **Define scheduler-facing interfaces and invariants.**
   Define `HomerProgressSourceId`, `HomerProgressSourceRef`,
   `HomerProgressSourceCore`, `HomerProgressSourceFeedback`,
   policy-specific state, `HomerProgressGrant`, `HomerProgressPlan`,
   `HomerProgressResult`, and later `HomerEgressGrant` before changing pump
   behavior. Scheduler-facing executor return values mean success/failure only;
   progress is reported through the result object. The current
   [`HomerPayloadProgressDelta`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:858)
   is the payload-specific prototype for the generic result shape, not the final
   public scheduler type.

2. **Register stable progress sources and ownership.**
   Source registration happens at lane/session/stream/control setup time and uses
   fixed tables, not heap work records. Candidate source kinds are:
   peer-control lane, local control slots, remote client SQL command ring,
   command/completion ring, payload stream, lane-CQ drain, and RDMA-CM setup.
   Use per-kind arrays that already match the current ownership model
   (sessions, streams, lanes, control slots) and compact `kind/index/generation`
   identifiers. Hot grants carry the already-resolved owner pointer after the
   ready-plan builder validates the generation; executors should not perform a
   hash/map lookup.

3. **Derive source readiness from existing frontiers.**
   Producers should not write scheduler-ready bits. The service derives them:
   payload ready when producer `publishedTail` exceeds the service
   scheduled/posted frontier; command ready when command-slot `readySeq` or the
   command-ring frontier is ahead of consumed/forwarded state; completion ready
   when a completion ring/mailbox has unconsumed events; peer-control ready when
   the lane mailbox, CM state, or CQ indicates pending work; local-control ready
   when a fixed control slot is `REQUEST_READY`; CQ-drain ready when a lane has
   signaled WRs pending. Persistent ready bitsets are the cheap sieve that avoid
   broad scans. A bounded stack-local ready array / progress plan is built from
   those bitsets each service pass by taking only enough sources to fill the
   fixed-priority plan. This preserves the DPU-facing rule that readiness bits
   are scheduler-local hints derived from authoritative owner-written frontiers.

4. **Make CQ draining lane-owned, not owner-polled.**
   This cleanup should happen before or as the first part of the
   scheduler-integrated service loop. Traffic-class lanes/QPs solved posted-WR
   head-of-line blocking between classes, but the current send-CQ API is still
   pull-oriented: a payload stream or command path calls a helper asking "is
   there a completion for this owner?" When that helper polls the lane's CQ and
   sees a tagged completion for a different owner, it must stash the unrelated
   completion so it is not lost. That is why
   [`TupleSinkServiceStashPayloadSendCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1222),
   [`TupleSinkServicePopStashedPayloadSendCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1250),
   and the owner-specific
   [`TupleSinkServicePollPeerPayloadSendCompletionRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6142)
   helper still exist.

   The target service-progress model should instead have a lane-CQ progress
   source. A progress grant such as "drain bulk-payload lane send CQ up to N
   completions" should call a lane-owned CQ drain helper, decode every CQE's
   tagged `wr_id`, and dispatch it immediately to the owning fixed state:

   - payload CQE -> `{serviceStreamId, completionToken}` -> payload stream
     completion tracker / frontier
   - command CQE -> outstanding command-post slot or command-ring post frontier
   - peer-control CQE -> lane-owned peer-control op slot / publish slot
   - unknown generation or owner -> stale event counter, then ignore or mark only
     the affected owner failed

   The CQ drain should update owner state immediately through small apply
   functions that only adjust frontiers, counters, and ready bits; those apply
   functions must not call back into the service pump or post unrelated new
   work. A small bounded stack-local event array is only a fallback if a concrete
   layering issue prevents direct dispatch. It should not allocate heap work
   items or stash unrelated CQEs in side queues. If an event/dispatch budget
   fills, stop polling and leave remaining CQEs in the verbs CQ for the next
   lane-CQ grant.

   This changes completion flow from:

   ```text
   stream pump asks for stream X completion
     -> lane CQ may return stream Y / command / control CQE
     -> stash unrelated event
     -> stream Y later pops from stash
   ```

   to:

   ```text
   scheduler grants lane CQ drain
     -> drain up to N CQEs from that lane
     -> dispatch each CQE directly to its owner
     -> owner source facts/frontiers update before egress grants are chosen
   ```

   With this shape, the scheduler sees completion pressure as lane feedback, not
   as a hidden side effect of whichever stream happened to poll the CQ. It also
   removes the `memmove`/linear-search side queues from the normal completion
   path and makes future DPU/offload placement cleaner: the DPU-side lane owner
   drains one CQ and updates lane-local/stream-local frontiers without bouncing
   unrelated completions through host-visible temporary queues.

5. **Turn current dedicated pumps into specialized grant executors.**
   The unified scheduler should not erase object-specific logic. It should route
   grants to specialized executors that fill `HomerProgressResult` directly:
   - command executor adapted from
     [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4884),
     scoped to one command-ring source and bounded by `maxItems`
   - local-control executor adapted from
     [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14972),
     bounded by slot count or per-pass item count
   - peer-control/lane executor adapted from
     [`TupleSinkServicePumpPeerConnections()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8666)
     and
     [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7084),
     scoped to a lane/source
   - completion executor adapted from
     [`TupleSinkServiceConsumeAllCompletionMailboxes()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4578),
     scoped to a session/completion source when possible
   - payload executor adapted from
     [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11509),
     which then calls the egress scheduler before posting RDMA work

   Unification is at the scheduling interface: source facts, progress grants,
   egress grants, and feedback. It is not a mandate to use one generic object
   format for commands, peer control, tuple payload, and basebackup payload.

6. **Build the scheduler-integrated service loop.**
   The current fixed ordering in
   [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15170)
   should become:

   ```text
   derive source readiness from authoritative frontiers
   build a ready set over registered sources
   choose a stack-local progress plan
   execute bounded grants through specialized executors
   fold each HomerProgressResult into persistent source/lane facts
   derive any loop-level idle/backoff decision from those results
   ```

   This is the milestone where the service loop consumes
   `HomerProgressResult` directly. There should be no persistent compatibility
   wrapper layer that converts scheduler results back to `bool madeProgress`.
   The first policy is still fixed-priority bounded and should intentionally
   mimic the current ordering so validation isolates loop refactoring from policy
   changes.

7. **Move egress grants under all send-capable sources.**
   Payload egress is the first target. The sender path in
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9703)
   should consume a byte/object/fragment/WR grant instead of choosing the final
   batch solely from internal caps such as
   `HOMER_SERVICE_PAYLOAD_SEND_COMPLETION_BATCH`. Later, peer-control publication
   through
   [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3975)
   and async peer requests through
   [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6928)
   should also become grant-consuming critical-control work rather than
   open-coded "publish now" side paths.

8. **Add real scheduling policies.**
   Once the fixed-priority bounded loop is correct and no-regression, add
   round-robin, weighted/deficit, and age/deadline-aware policies. This is the
   research scheduler layer. It should come after source facts, lane-owned CQ
   drain, grant executors, and fixed-priority service-loop integration are
   measurable.

Initial service-progress policy should be deliberately simple:

```text
1. drain bounded critical control and command/completion events
2. drain bounded CQ/credit feedback for lanes that have outstanding work
3. build or refresh ready items from active ready bits/frontiers
4. ask the egress scheduler for byte/object/fragment/WR grants
5. consume grants with specialized senders
6. clear a ready bit only if frontiers prove no ready work remains
```

This keeps the one-thread service-loop implementation viable today while leaving
room to split service-progress and egress scheduling onto separate threads later.
If split, readiness/frontier metadata, explicit grant records, and lane feedback
become the inter-thread interface.

Candidate work items should be explicit metadata, not a bit mask:

```c
typedef struct HomerTransportReadyItem
{
    HomerTransportItemKind kind;
    HomerTrafficClass trafficClass;
    uint64 serviceSessionId;
    uint64 serviceStreamId;
    uint32 priority;
    uint32 weight;
    uint32 estimatedWrCount;
    uint32 estimatedObjectCount;
    uint32 estimatedFragmentCount;
    uint64 estimatedBytes;
    uint64 sourceReleaseFrontier;
    uint64 semanticObjectFrontier;
    uint64 remoteCreditFrontier;
    uint64 readySinceTick;
    void *owner;
} HomerTransportReadyItem;
```

The ready set is the current fixed-size array of eligible send work. It does not
encode ordering. The selected policy owns ordering and grants. Ready-item
frontiers are hints used to size a grant and avoid selecting work that cannot
make egress progress; authoritative ownership still remains in the stream/lane
state.

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
    uint32 maxFragments;
    uint32 maxWrCount;
    uint64 maxBytes;
    uint32 targetFragmentBytes;
    uint32 flags;
} HomerTransportGrant;
```

The transport publisher reports actual typed progress. As with
`HomerProgressResult`, it should not return or store a generic
`madeProgress` field; any loop-level idle/backoff decision is derived from the
typed counters/frontiers:

```c
typedef struct HomerTransportProgress
{
    bool stillReady;
    bool blockedOnRemoteCredit;
    bool blockedOnLocalCompletion;
    uint32 postedObjects;
    uint32 postedFragments;
    uint32 postedWrCount;
    uint32 completedCqCount;
    uint64 postedBytes;
    uint64 transportCompletionFrontier;
    uint64 sourceReleaseFrontier;
    uint64 semanticObjectFrontier;
    uint64 remoteCreditFrontier;
} HomerTransportProgress;
```

Deficit policies should account bytes or WRs rather than just objects. Otherwise
a tiny control message and an 8 MiB basebackup chunk look equally expensive.

Fragmentation is part of the scheduler design, even if the first implementation
does not fragment objects. The scheduler should be able to grant a prefix of a
large basebackup/WAL/tuple-result transport object so smaller foreground work can
interleave. The first policies can require whole-record grants; later policies
should expose tunables such as `maxGrantBytes`, `maxGrantFragments`, and an
MTU-like target fragment size. Upper layers should still observe complete
semantic objects only; fragmentation and reassembly are transport-substrate
responsibilities.

June 2, 2026 clarification: the first fragmentation design should stay much
lighter than a standalone generic `HomerPayloadFragmentHeader`. Under the
initial constraints that one stream is ordered on one RC QP/lane and transport
records do not wrap around the byte ring, the existing
`CitusTupleSinkTransportHeader` can remain the only transport envelope:
`payloadBytes` means fragment byte count, `sinkSequence` remains the semantic
object sequence, and small `FRAGMENT_FIRST` / `FRAGMENT_LAST` flags identify the
object boundary. `FIRST | LAST` means the current whole-record behavior. `FIRST`
starts one active reassembly object, plain continuation appends to it, and
`LAST` makes the complete object deliverable.

This design keeps object-family headers as DB-semantic metadata, not transport
scheduler metadata. The first fragment of a tuple/result object still contains
`CitusTupleSinkBatchHeader`; the first fragment of a basebackup object still
contains `CitusRemoteBaseBackupMessageHeader`; and the tuple shape remains a
setup/bind-time `CitusTupleViewContract`. The transport reassembly path does not
need to parse those headers on every fragment just to know whether the object is
complete. It can use the transport flags on the hot path, then invoke the
object-family validator/materializer only after the `LAST` fragment has made a
complete semantic object available.

Terminology note: an RDMA **WR chain** is several work requests submitted
together, typically linked by `wr.next` and posted with one `ibv_post_send()`.
It is different from one large WR. One RDMA WRITE WR targets one contiguous
remote address range, even if local memory is represented by one or more SGEs.
The sender needs a WR chain when a grant crosses local/remote byte-ring wrap
boundaries, covers non-contiguous fixed slots, or exceeds device/implementation
limits for one WR. The goal of the byte-ring substrate and egress scheduler is to
make common grants become one large contiguous WR plus one publication event; WR
chains remain the fallback for wrap and non-contiguous cases.

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

The scheduler eventually chooses a traffic class and grant. The transport maps
that class to a lane, and the lane maps to one QP/CQ or to a QP/CQ pool. For the
next milestone, there is no pluggable scheduler yet: the service loop provides a
fixed-priority baseline by progressing lanes in class order. Link-level
contention still exists, but a control write is no longer queued behind
already-posted bulk WRs in the same verbs work queue once the lanes map to
different QPs.

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
2. Next, introduce explicit lane/resource ownership and split physical resources
   into the simple 1:1 baseline: critical control -> one lane/QP/CQ, foreground
   payload -> one lane/QP/CQ, bulk payload -> one lane/QP/CQ, and optional
   maintenance later. This step is expected to reduce posted-WR head-of-line
   blocking and to remove the shared-CQ stashing workaround from the normal path.
3. During that lane split, keep the scheduling policy hard-coded in the service
   loop: progress critical control first, foreground payload second, and bulk
   payload third, with bounded work per lane. This is the baseline scheduling
   behavior for the multi-resource milestone, not the research scheduler.
4. Scheduler policy experiments come after the physical lane split is measurable.
   The pluggable scheduler should replace the hard-coded service-loop order and
   budgets with policy-selected grants, but should reuse the same lane/resource
   ownership model.

Implementation progress on May 17, 2026: the simple multi-resource baseline is
now in the code. [`HomerTransportTrafficClass`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:63)
and [`HomerTransportSchedulingMetadata`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:83)
define the internal traffic-class/scheduler metadata. Each payload stream caches
that metadata in
[`HomerPayloadStreamState.transportScheduling`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:630),
initialized by
[`HomerServiceInitPayloadTransportScheduling()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:844).
The current semantic-to-traffic-class mapping is:

- tuple/COPY/result payload -> foreground payload, selected by
  [`HomerServicePayloadTrafficClassForOpKind()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:826)
- basebackup payload -> bulk payload, selected by the same helper
- ordinary peer-control requests/responses -> critical control through the
  async op pair
  [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6928)
  and
  [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6967)
- payload open control -> the payload stream's traffic class through the same
  async op pair, driven by
  [`TupleSinkServiceProgressStreamOpenAsyncOp()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13080)
- payload close/EOS -> in-band payload-stream EOS for tuple results through
  [`CITUS_TUPLE_SINK_TRANSPORT_FLAG_EOS`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:37),
  followed by local sender-side cleanup in
  [`TupleSinkServicePeerCloseSinkBestEffort()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8076)

The transport now uses the traffic class as part of the outgoing connection
cache key in
[`TupleSinkServiceFindOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2599).
The bootstrap message carries the lane class at
[`TupleSinkServicePrepareBootstrapMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2011),
and
[`TupleSinkServiceApplyPeerBootstrapMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2034)
rejects invalid or mismatched lane classes. A connection handle is then guarded
against class mismatch in
[`TupleSinkServicePeerConnectionForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2786).
The service loop drains lane-local CQs in fixed priority order using
[`HomerTransportServiceLoopPriority`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:78):
critical control, foreground payload, bulk payload, then maintenance. This is a
baseline policy, not yet the pluggable research scheduler.

Important implementation correction: payload peer-open must ride on the same
traffic-class lane as the payload writes. The receiver registers its byte
ring/tuple ring against the accepted connection handle, so if bulk payload
opened via the critical-control lane but payload bytes arrived on the bulk lane,
the remote rkey/PD ownership would be wrong. The sender-side open call is
[`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8065),
and best-effort close uses the matching class at
[`TupleSinkServicePeerCloseSinkBestEffort()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8196).

Second correction from validation: remote basebackup blackhole has no
frontend-visible POSIX receive queue on the responder. The receiver now returns
only the byte-ring RDMA descriptor for basebackup and leaves
`peerReceiveQueueDescriptor` zeroed at
[`TupleSinkServiceHandlePeerOpenRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8597).

Validation on the final multi-resource baseline binary:

- `make -j8`, `sudo -n make install-headers install-service-bin install`, and
  rsync to farnet0 passed in `/data/dbcomm/citus-dbcomm-separate-comm-stack`.
- Single-client remote RDMA Homer pgbench, c1/j1/t50000, after clean
  `pgbench_history` reset: repeat warmed run completed `50000/50000`, zero
  failures, `4835.21 TPS`, p50 `0.205 ms`, p99 `0.225 ms`, max `5.903 ms`.
- A c4/j4/t10000 sanity run completed `40000/40000`, zero failures, but still
  showed only `7598.59 TPS` with p99 `8.136 ms`. Treat this as correctness
  evidence for multiple sessions, not as a solved scaling result; all client SQL
  command sessions still share the critical-control traffic class.
- Remote RDMA basebackup blackhole with
  `slots=8,bytes=8388608` completed repeatedly: cold/warm `5.35s`, then warmed
  `4.37s`, `4.34s`, and `4.29s`.
- Concurrent correctness run completed with one remote RDMA basebackup and one
  c1/j1/t10000 Homer pgbench run overlapping. Both returned exit code 0;
  pgbench processed `10000/10000` with zero failures, and basebackup completed
  in `4.60s`.
- A second concurrent correctness run used c4/j4/t2500 Homer pgbench while the
  same remote RDMA basebackup ran. Both returned exit code 0; pgbench processed
  `10000/10000` with zero failures, and basebackup completed in `4.49s`.

Validation on the earlier final single-lane binary:

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

The first scheduler can grant whole semantic objects only. That avoids changing
receiver reassembly before the rest of the scheduler is stable.

However, the long-term design should not forbid slicing a very large semantic
object. Large basebackup objects may be too coarse as the minimum scheduling
unit. With RC QPs, writes posted on one QP are observed by the receiver in order
for that QP, but the upper layer should still not see an incomplete object.

Current terminology and code anchors:

- Object-family headers are DB-semantic metadata inside the payload. Tuple/result
  objects use `CitusTupleSinkBatchHeader` in
  `src/include/distributed/homer/tuple_sink_protocol.h`; basebackup objects use
  `CitusRemoteBaseBackupMessageHeader` in the same file. The tuple shape contract
  is `CitusTupleViewContract`, but that is setup/bind metadata rather than a
  per-object transport header.
- Transport records are the neutral Homer/RDMA envelope:
  `[CitusTupleSinkTransportHeader][object-family bytes]`. The current whole-record
  byte-ring sender in `HomerServicePumpOutgoingByteRingBaseBackup()` parses
  complete producer records, validates the object-family header, coalesces
  contiguous RDMA ranges, and publishes the receiver byte tail. The receiver in
  `HomerServicePumpIncomingByteRingBaseBackup()` parses records only after the
  published byte tail makes them visible.

Minimal future fragmentation rule:

- the RDMA substrate may publish fragments of one large semantic object
- a fragment is still a transport record:
  `[CitusTupleSinkTransportHeader][fragment bytes]`
- `CitusTupleSinkTransportHeader.payloadBytes` means fragment byte count
- `CitusTupleSinkTransportHeader.sinkSequence` remains the semantic object
  sequence, not a fragment sequence
- add small transport flags, for example `FRAGMENT_FIRST` and `FRAGMENT_LAST`
  - `FIRST | LAST`: complete object in one record, matching today's behavior
  - `FIRST`: first fragment, whose bytes start with the object-family header
  - no fragment flag: continuation of the current ordered object
  - `LAST`: final fragment; the completed object may be validated and delivered
- no large standalone fragment header is needed for the first design
- no fragment offset/count is needed while a stream is ordered on one RC QP/lane
  and the receiver allows only one active fragmented object per stream
- transport records should still not wrap around the byte ring in the initial
  implementation; the sender may skip trailing ring bytes before wrap as it does
  today
- fragmentation is hidden below tuple/basebackup/WAL object consumers; the object
  family callback is invoked only after the full semantic object is available

Sender-side representation decision:

- Keep the backend/DB producer ring as complete semantic objects. The producer
  should still publish prompt complete object records into the local byte ring;
  it should not pre-fragment just because the current scheduler policy wants a
  smaller egress unit. This preserves the scheduler's authority over fragment
  size and avoids moving traffic-class policy into the DB engine.
- Make the remote receiver byte ring naturally contain baked-in transport
  records. The remote representation remains
  `[CitusTupleSinkTransportHeader][fragment bytes]`, so the receiver can parse
  the byte ring without a side table.
- Generate per-fragment transport headers in the service/sender and store them in
  a small pre-registered **per-lane** header source pool. The pool should live
  with the lane-local RDMA connection resources, because the current baseline maps
  each traffic class to one lane-local RDMA connection/QP and many payload streams
  may share that lane. The fragment/reassembly frontiers remain stream-owned in
  `HomerPayloadStreamState`; only the registered header source memory is lane-owned.
  This pool is not a second remote data ring.
- Post each fragment as one RDMA write with two local SGEs when the helper grows
  that shape:
  `SGE 0 = generated CitusTupleSinkTransportHeader`,
  `SGE 1 = producer-ring object byte slice`, targeting one contiguous remote
  byte-ring range. This keeps object bytes zero-copy while still producing the
  desired remote `[header][fragment]` layout.
- Track header-pool slot lifetime until the signaled send completion that covers
  the corresponding fragment/batch. Reusing a header slot before the NIC has
  retired the WR would corrupt the remote transport header. Use fixed rings and
  frontiers; do not allocate header records on the hot path.
- The existing RDMA helpers currently post one SGE per payload write. For
  example, `TupleSinkServicePostPeerRegisteredPayloadBatchWithTailImmediateRdma()`
  in `src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c`
  sets `num_sge = 1` for payload WRs. The fragmentation implementation should
  extend the payload write descriptor/helper to support a small fixed SGE count
  rather than adding a bounce buffer copy.

Rejected or scoped alternatives:

- A service staging/bounce buffer is acceptable only for debugging; it copies
  fragment bytes on the hot path and should not be used for performance numbers.
- Producer-side pre-fragmentation can be a temporary smoke-test shortcut, but it
  bakes the fragment size into the backend producer and weakens the scheduler
  abstraction. It is not the target design.
- Header-sized gaps pre-reserved inside the producer object layout also bake in a
  fixed fragment size and waste byte-ring space. Avoid it for the scheduler-ready
  design.

The important frontier distinction is responder visibility versus semantic
delivery. `publishedTail` says bytes/transport records are visible on the receiver
ring; it does not by itself mean the semantic object is complete. The receiver
should track a tiny per-stream reassembly state such as active object sequence,
object start byte head, and accumulated fragment bytes. For blackhole basebackup,
parsed bytes and reclaimable bytes can advance together. For tuple/result
materialization, reclamation may need to stay at completed-object boundaries
unless the backend-facing consumer can safely consume and release partial
fragments.

This can be deferred until scheduler experiments show object granularity is too
coarse. The scheduler and QP design should nevertheless leave room for it by
granting bytes, fragments, and WRs in addition to object counts.

Implementation-ready first slice:

1. Add `FRAGMENT_FIRST` and `FRAGMENT_LAST` transport flags while preserving the
   current `FIRST | LAST` whole-record behavior when fragmentation is disabled.
2. Add a small registered per-lane fragment-header pool to the sending service
   RDMA connection state, with fixed-size slots containing
   `CitusTupleSinkTransportHeader` and completion-frontier based reuse. Stream
   state should track which posted fragment frontier depends on which lane header
   slot, but MR ownership stays lane-local.
3. Extend `TupleSinkServicePeerPayloadWriteDescriptor` and
   `TupleSinkServicePostPeerRegisteredPayloadBatchWithTailImmediateRdma()` so one
   payload write can carry either one contiguous registered source range or two
   SGEs for `[generated header][producer byte slice]`.
4. Add a forced-fragment-size knob for bulk byte-ring streams. Start with remote
   RDMA basebackup only; tuple/result fragmentation is deferred because tuple
   byte-ring receive currently has service-passive/backend-visible behavior.
5. In `HomerServicePumpOutgoingByteRingBaseBackup()`, validate the complete
   source semantic object once, then emit one or more transport fragments from the
   source payload range according to the forced size or later egress grant.
6. In `HomerServicePumpIncomingByteRingBaseBackup()`, replace immediate
   whole-record delivery with one active per-stream reassembly state. Accumulate
   transport fragments and invoke the basebackup validator/accounting only when
   `FRAGMENT_LAST` completes the semantic object.
7. Keep transport fragments no-wrap in the first implementation. If a fragment
   header plus bytes cannot fit before the receiver byte-ring wrap, skip the
   remote trailer and start the fragment at offset zero, matching the existing
   gap-skip convention for whole records.
8. Verify no-regression with fragmentation disabled, then force a small fragment
   size and verify remote RDMA basebackup correctness and warmed performance.
   Treat forced fragmentation as a substrate validation step before adding dynamic
   scheduler egress grants.

Completion and verification criteria:

- **No-fragment regression path.** Run with fragmentation disabled, or with the
  configured fragment size larger than the largest observed object, so
  `FIRST | LAST` remains the only normal transport-record shape. Correctness and
  performance should remain on par for:
  - remote Homer pgbench c1/c4 throughput and latency
  - standalone remote RDMA basebackup warm runs
  - concurrent remote c4 pgbench plus remote RDMA basebackup
  - service logs should not report invalid transport headers, sequence mismatch,
    reassembly error, header-pool exhaustion, send-completion/frontier mismatch,
    or remote-credit corruption
- **Operator/debug knob.** The first implementation should expose a narrow
  forced-fragment-size setting for bulk byte-ring basebackup, for example an
  environment variable such as `HOMER_BULK_FRAGMENT_BYTES`. The default must be
  `0`/disabled so normal pgbench, standalone basebackup, and concurrent
  pgbench+basebackup runs exercise the current no-extra-fragment path. A value
  larger than the maximum emitted basebackup object is also a useful
  no-fragment sanity check because it forces the code through the configured
  path while still producing only `FIRST | LAST` transport records.
- **Forced-fragment correctness path.** Run remote RDMA basebackup with a small
  fixed fragment size, for example 4 KiB, so large basebackup objects are split
  into multiple transport records. This test is for correctness, not performance.
  It should verify:
  - the stream reaches `CITUS_REMOTE_BASEBACKUP_OBJECT_END`
  - received object count and payload byte count match the no-fragment run
  - each semantic object sequence completes exactly once
  - the receiver does not invoke the object-family validator/materializer until
    `FRAGMENT_LAST`
  - invalid fragment sequences are detected defensively: continuation without an
    active object, `FIRST` while active, `LAST` without active state, sequence
    mismatch, and accumulated bytes exceeding the expected object size
  - remote ring credit advances only after bytes are safe to reclaim
  - per-lane header-pool slots are not reused before the covering send completion
- **Forced-fragment stress path.** Optionally run with very small fragments such
  as 512 B or 1 KiB to exercise many fragments per object, byte-ring wrap/gap-skip
  behavior, completion tracking, and reassembly state. Do not use this stress mode
  as a steady-state performance comparison; it intentionally increases header and
  WR overhead.
- **Regression standard after implementation.** The pass/fail bar for the
  substrate change is intentionally conservative: with the forced-fragment knob
  disabled, warmed pgbench, warmed basebackup, and concurrent pgbench+basebackup
  should not regress materially. Forced-fragment runs only need to prove the
  receiver waits for complete semantic objects and preserves credit/frontier
  correctness; throughput in that mode is diagnostic until a scheduler chooses
  fragment size dynamically.

June 2, 2026 implementation checkpoint:

- Fragmentation/reassembly is implemented for remote RDMA basebackup byte-ring
  streams. The active protocol flags are
  `CITUS_TUPLE_SINK_TRANSPORT_FLAG_FRAGMENT_FIRST` and
  `CITUS_TUPLE_SINK_TRANSPORT_FLAG_FRAGMENT_LAST` in
  `src/include/distributed/homer/tuple_sink_protocol.h:38`.
- Sender-side fragmentation is forced only by the diagnostic
  `HOMER_BULK_FRAGMENT_BYTES` knob. The default is disabled, so pgbench,
  standalone basebackup, and concurrent pgbench+basebackup still use the
  no-extra-fragment path.
- The sending service keeps the DB producer ring as complete semantic records
  and emits transport fragments from
  `HomerServiceAppendOutgoingBaseBackupFragmentWrite()` in
  `src/backend/distributed/utils/homer/tuple_sink_service_process.c:3047`.
  The receiver reassembles below object-family delivery in
  `HomerServiceProcessReceivedBaseBackupTransportRecord()` at
  `tuple_sink_service_process.c:9262`.
- The RDMA helper now supports two local SGEs per payload WR. This required the
  QP to request `max_send_sge = 2` through
  `CITUS_REMOTE_EXEC_PEER_MAX_SEND_SGE` at
  `src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:71`.
  A validation bug showed why the final tail SGE must use
  `tailSgeIndex = payloadWriteCount * 2U` at
  `remote_execution_peer_transport_rdma.c:5928`; using `payloadWriteCount`
  overlaps the SGE area for two-SGE payload WRs and corrupts later transport
  records.
- Completion tracking now uses explicit monotonic completion tokens and per-batch
  WR counts. This was necessary because partial fragments can complete without
  advancing the source-byte or semantic-object frontier. The WR cap
  `HOMER_SERVICE_PAYLOAD_MAX_OUTSTANDING_WRS` at
  `tuple_sink_service_process.c:153` keeps forced fragmentation from overfilling
  the QP send queue before completions retire posted WR chains.
- Validation used default/no-fragment remote RDMA basebackup and pgbench, forced
  7 MiB basebackup fragmentation, forced 1 MiB basebackup fragmentation, forced
  4 KiB basebackup fragmentation, and a concurrent c4 pgbench plus basebackup
  smoke. Final observed checks after the optimization pass:
  no-fragment warmed remote basebackup `4.30s`; remote c1 Homer pgbench
  `4694.98 TPS`, p50 `0.210 ms`, p99 `0.233 ms`; remote c4 Homer pgbench
  `10753.54 TPS`, p99 `0.616 ms`; concurrent c4 pgbench plus basebackup
  completed with pgbench `9236.29 TPS`, p99 `0.773 ms`, and basebackup `4.45s`.
- June 2, 2026 rerun after the two-SGE tail-index fix showed that the earlier
  stopped `4 KiB` and `1 MiB` diagnostic attempts were pre-fix artifacts, not
  inherent fragmentation failures. With the same remote RDMA basebackup command
  and restarted services, the first post-restart run was slower for every mode:
  default/no-fragment `5.86s`, forced 1 MiB `6.19s`, and forced 4 KiB `6.01s`.
  Warmed repeats converged to the same band: default `4.25s` and `4.33s`;
  forced 1 MiB `4.32s` and `4.30s`; forced 4 KiB `4.23s`, `4.21s`, and
  `4.24s`.
- Practical caveat: tiny forced fragments are still a diagnostic stress setting,
  not the target scheduler policy. The current workload does not show warmed
  steady-state degradation at 4 KiB because the service can batch many posted
  WRs and the 400 Gbps link is not the bottleneck here. This result is useful as
  a correctness and batching sanity check, but future scheduler work still needs
  explicit fragment/WR counters and grant sizing because a different object mix,
  traffic mix, or DPU placement could make per-fragment CPU and CQ work visible.

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

   6. **Traffic-class peer transport lanes - completed May 17, 2026 for the 1:1
      baseline**
   - split peer transport resources by traffic class using the simple 1:1
     baseline topology: one lane/QP/CQ for critical control, one for foreground
     payload, and one for bulk payload
   - keep lane pools as a later extension; this milestone should not yet shard
     one traffic class across multiple QPs
   - use one critical-control lane per peer transport initially
   - update after the May 30 cleanup: the peer-control mailbox is now a
     shared fixed-width multi-slot ring with explicit op-id/generation matching,
     but some setup/legacy call sites still wait through blocking adapters and
     RDMA-CM connection setup is still a blocking island
   - map foreground payload and bulk payload to separate lanes/QPs; keep lane
     pools possible for high-throughput payload classes in the API, but do not
     implement pools before the 1:1 baseline is correct and measured
   - preserve the existing WRITE_WITH_IMM publication rule per QP/channel
   - use the service loop as the baseline scheduler: progress critical control,
     then foreground payload, then bulk payload, with bounded work per lane
   - validation showed pgbench and basebackup standalone correctness/performance
     in the warmed expected bands and a concurrent pgbench plus remote RDMA
     basebackup run completed correctly; see the implementation-progress note
     above for exact numbers

   Client-SQL performance evidence from the May 17, 2026 real-RDMA command-ring
   and deferred-CQE checkpoints still strengthens this priority. The command
   mailbox ABI is a 64-slot ring, frontend commands are forwarded directly from
   registered ring slots, and the backend consumes by polling slot-local
   `readySeq`:
   [`CitusRemoteExecLocalCommandMailbox`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_backend_protocol.h:164),
   [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4870),
   and
   [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2131).
   Warmed remote c4/j4 runs around the `11k TPS` band, while c1/j1 is around the
   `4.3k-4.8k TPS` band on the current two-WR fallback path.

   The remaining command-transport limitations are now:

   - on the current farnet configuration, the one-WR self-publishing command slot
     fast path is disabled because
     [`ibv_query_qp_data_in_order()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1311)
     reports no whole-message RDMA write ordering support; the measured path
     therefore posts an unsignaled command-record write plus a tagged `readySeq`
     write
   - asynchronous source-slot retirement is implemented: the command sender no
     longer waits inline for the send CQE before advancing frontend source credit,
     and
     [`TupleSinkServicePollRemoteClientSqlCommandWriteCompletions()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1866)
     retires signaled command send CQEs from the service loop
   - deferred command CQE experiments with larger signal intervals were correct
     but did not improve warmed c1/c4 pgbench; the default remains
     `HOMER_SERVICE_CLIENT_SQL_COMMAND_SIGNAL_INTERVAL=1`
   - true command pipelining is still separate future work and needs an explicit
     remote receiver-credit frontier before the sender can safely wrap remote
     command slots; the current simple-mode pgbench workload still has one
     command in flight per session
   - command send-retirement state still needs to move fully into a lane-owned
     transport resource instead of being managed by mailbox-specific helper state
   - physical traffic classes are now split in the 1:1 baseline topology: payload
     streams choose `FOREGROUND_PAYLOAD` or `BULK_PAYLOAD` at stream creation, and
     [`TupleSinkServicePeerConnectionForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4737)
     validates that payload writes use the correct traffic-class connection

   Do not revive the old command `RDMA_WRITE_WITH_IMM` experiment as-is. Its
   useful successor is a backend-polled self-publishing command slot, not a
   receiver-service CQ path. Compact variable-size command wire slots should also
   wait until the fixed-slot self-publish path is usable and measured on hardware
   that reports the required in-order RDMA write capability; the earlier
   compact-size experiment showed that byte count alone was not the dominant
   cost in the two-WR publication shape.

   Therefore the next transport-lane step should introduce explicit
   `HomerTransportLane`/transport-resource ownership, split physical resources by
   traffic class in the 1:1 baseline topology, and move send-retirement state
   into each transport resource rather than adding more ad hoc command mailbox
   special cases. Each transport resource/QP should own:

   - `nextPostOrdinal`
   - `completedPostOrdinal`
   - an ordered pending FIFO of minimal posted-command descriptors
   - `pendingHead` / `pendingTail`

   A signaled send CQE then advances the per-resource completion frontier and
   pops FIFO entries in order. This replaces the current global outstanding table
   and scan in
   [`TupleSinkServiceRetireClientSqlCommandWriteCompletionsThrough()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1684).
   It also gives the traffic-class QP split a natural owner for per-resource
   post/completion frontiers.

   The current RDMA layer still has CQ ownership details to clean up within each
   traffic-class connection. Physical lanes/QPs remove cross-class posted-WR HOL,
   but helpers still poll the lane send CQ for one owner at a time. For example,
   [`TupleSinkServicePollPeerPayloadSendCompletionRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6142)
   asks for completions for one `serviceSinkId`, while
   [`TupleSinkServicePollPeerCommandSendCompletionRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6320)
   asks for command write completions. Because the send CQ is lane/connection
   owned, either helper may see a valid CQE for a different owner. The current
   workaround is to stash unrelated tagged completions in
   [`TupleSinkServiceStashPayloadSendCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1222)
   and later recover them through
   [`TupleSinkServicePopStashedPayloadSendCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1250).

   The scheduler-integrated service loop should delete that pattern. Each
   `HomerTransportLane` / transport resource should own a lane-CQ drain source.
   The service-progress scheduler grants the lane a bounded CQ-drain budget; the
   lane drain decodes each tagged `wr_id` and dispatches the completion directly
   to fixed owner state, such as payload-stream completion trackers, command-post
   slots, or peer-control op slots. Completion demux then becomes a normal
   lane-progress result rather than a side effect of whichever payload or command
   helper happened to poll first. If the per-pass event budget fills, leave
   remaining CQEs in the verbs CQ for the next pass; do not move them into
   temporary side queues. This removes normal-path stash search/move work and
   gives the scheduler accurate per-lane feedback for outstanding WR pressure,
   completed CQ count, and stale generation-safe CQEs.

   The implementation checkpoint has exact measurement details:
   [client_sql_session_pgbench_checkpoint.md](../../../implementations/postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md).

7. **Nonblocking peer transport/control state machines - next correctness
   milestone**

   The May 2026 concurrent pgbench plus remote RDMA basebackup validation exposed
   an important gap in the multi-lane implementation: physical traffic-class
   lanes avoid posted-WR head-of-line blocking for payload traffic, but the
   peer-control and connection-management code still contains blocking islands
   that can stop service-loop progress for other lanes.

   Important current-state distinction:

   - payload lanes already use the desired high-level pattern: shared lane-owned
     resources with multi-slot / byte-ring stream state, not one physical RDMA
     resource per DB session. The mapping is established by
     [`HomerServicePayloadTrafficClassForOpKind()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1262):
     client SQL / tuple payloads map to `FOREGROUND_PAYLOAD`, while basebackup
     maps to `BULK_PAYLOAD`.
   - peer payload open already binds a stream to the class-specific connection in
     [`TupleSinkServiceOpenPeerSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8040),
     especially the class-specific connection lookup at
     [`tuple_sink_service_process.c:8047`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8047)
     and peer-open send at
     [`tuple_sink_service_process.c:8096`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8096).
   - the missing long-term piece is narrower: make the **critical-control lane**
     follow the same lane-owned, multi-slot, explicit-work-ownership model. Do
     not create per-session peer-control QPs/mailboxes merely because SQL sessions
     are per-session; peer-control open/bind/close happens across many sessions
     and even before a remote DB session exists.

   Current blocking points to remove:

   - outgoing RDMA-CM setup in
     [`TupleSinkServiceConnectOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2447)
     waits synchronously for `ADDR_RESOLVED`, `ROUTE_RESOLVED`, and
     `ESTABLISHED`
   - incoming accept in
     [`TupleSinkServiceAcceptIncomingPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2638)
     waits synchronously for `ESTABLISHED`
   - peer-control request/response publication in
     [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3456)
     currently posts the mailbox write and tail `WRITE_WITH_IMM`, then waits
     inline for the send CQE before the scratch buffers may be reused
   - service-to-service peer request submission in
     [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5766)
     owns a stack-frame response wait loop instead of creating an operation that
     the service loop can advance
   - teardown in
     [`TupleSinkServiceBestEffortDisconnectPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2391)
     waits for `DISCONNECTED` even on best-effort error cleanup

   The target design is not "wait while pumping" from inside these helpers. That
   risks re-entering the same lane, reusing scratch buffers while an outer stack
   frame still assumes ownership, or resetting a connection while the caller still
   treats it as live. The cleaner design is explicit state owned by the lane and
   progressed only by the service loop.

   Proposed lane connection phases:

   ```c
   typedef enum HomerPeerLanePhase
   {
       HOMER_PEER_LANE_UNUSED = 0,
       HOMER_PEER_LANE_RESOLVE_ADDR_POSTED,
       HOMER_PEER_LANE_WAIT_ADDR_RESOLVED,
       HOMER_PEER_LANE_RESOLVE_ROUTE_POSTED,
       HOMER_PEER_LANE_WAIT_ROUTE_RESOLVED,
       HOMER_PEER_LANE_RESOURCES_READY,
       HOMER_PEER_LANE_CONNECT_POSTED,
       HOMER_PEER_LANE_ACCEPT_POSTED,
       HOMER_PEER_LANE_WAIT_ESTABLISHED,
       HOMER_PEER_LANE_BOOTSTRAP,
       HOMER_PEER_LANE_READY,
       HOMER_PEER_LANE_CLOSING,
       HOMER_PEER_LANE_FAILED
   } HomerPeerLanePhase;
   ```

   Proposed peer-control operation phases:

   ```c
   typedef enum HomerPeerControlOpPhase
   {
       HOMER_PEER_CONTROL_OP_UNUSED = 0,
       HOMER_PEER_CONTROL_OP_NEED_LANE,
       HOMER_PEER_CONTROL_OP_WAIT_LANE_READY,
       HOMER_PEER_CONTROL_OP_POST_REQUEST,
       HOMER_PEER_CONTROL_OP_WAIT_REQUEST_SEND_RETIRE,
       HOMER_PEER_CONTROL_OP_WAIT_RESPONSE,
       HOMER_PEER_CONTROL_OP_POST_RESPONSE,
       HOMER_PEER_CONTROL_OP_WAIT_RESPONSE_SEND_RETIRE,
       HOMER_PEER_CONTROL_OP_COMPLETE_LOCAL_SLOT,
       HOMER_PEER_CONTROL_OP_FAILED
   } HomerPeerControlOpPhase;
   ```

   Storage and WR lifetime rules:

   - allocate fixed per-lane rings at lane setup time; do **not** heap-allocate
     one control operation or publish buffer per request
   - use a small fixed `HomerPeerControlOp` ring for in-flight peer-control
     requests/responses; `32` or `64` entries is enough for the current concurrent
     pgbench plus basebackup setup path and keeps the structure cache-friendly
   - use a separate fixed `HomerPeerControlPublishSlot` ring for RDMA source
     buffers that must remain stable until the send CQE retires
   - each publish slot should contain only the fixed control message, the publish
     tail word, a generation, and compact flags; large or cold metadata belongs in
     the operation table or setup-time lane state
   - encode WR ids as tagged values containing at least `{kind, lane index,
     slot index, generation}`; CQ polling decodes the tag, checks the generation,
     and frees exactly that publish slot
   - avoid storing raw pointers in WR ids; pointer WR ids are harder to validate
     after lane reset/reconnect and make stale-CQE handling less explicit
   - when the fixed op/publish ring is full, treat it as control-plane
     backpressure: skip posting new work for that lane on this pass and continue
     progressing other lanes

   Suggested data shape:

   ```c
   typedef struct HomerPeerControlPublishSlot
   {
       TupleSinkServicePeerControlMessage message;
       uint64 publishedTail;
       uint32 generation;
       uint8 inUse;
       uint8 reserved[3];
   } HomerPeerControlPublishSlot;

   typedef struct HomerPeerControlOp
   {
       HomerPeerControlOpPhase phase;
       uint64 sequence;
       uint32 publishSlotIndex;
       uint32 generation;
       CitusRemoteExecPeerRequestUnion request;
       CitusRemoteExecPeerResponseUnion response;
       uint32 localControlSlotIndex;
       uint32 serviceSessionId;
   } HomerPeerControlOp;
   ```

   The exact fields should be tightened during implementation. The important
   invariant is ownership: an async WR borrows a publish slot until its CQE is
   retired; a peer-control op owns request/response progress until it completes
   the local control slot or fails affected semantic state.

   Critical-control lane target:

   - introduce an explicit `HomerTransportLane` / `HomerCriticalControlLane`
     object for `HOMER_TRANSPORT_TRAFFIC_CLASS_CRITICAL_CONTROL`. It should own
     the RDMA CM id, QP/CQs, protection domain / registered memory ownership,
     inbound control ring descriptor, outbound pending-op ring, publish-source
     slots, and send-completion frontier.
   - replace the one-message
     [`TupleSinkServicePeerControlMailbox`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:116)
     with a lane-owned multi-slot peer-control ring. This ring is **shared by
     all sessions using the lane**, but every slot carries owner/correlation
     metadata: local control-slot index or waiter, service session id when known,
     peer session id when known, op id, message sequence, generation, request
     kind, and response destination.
   - keep per-session SQL command ordering unchanged. A single DB session still
     has at most one active simple-mode SQL command; the multi-slot control ring
     allows multiple session opens/binds/closes and peer-sink setup operations
     across different sessions to make progress concurrently.
   - requests and responses should be matched by `{origin lane generation, op id,
     message sequence}` rather than by "the one response currently being waited
     on." This removes the stack-frame response ownership in
     [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5766).
   - cancellation and abandonment are first-class op transitions. If a frontend
     local control request times out or disconnects while an open has already
     spawned a socketless backend through the spawn path in
     [`TupleSinkServiceStartRemoteExecBackend()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4168),
     the op must either complete the local slot, mark the semantic session failed,
     or send/perform a best-effort close for the spawned backend. It must not leave
     an unowned remote exec backend spinning on a pinned CPU.
   - ring-full is control-plane backpressure, not fatal hot-path failure: the
     service loop should skip posting more critical-control work for that lane on
     that pass and continue progressing existing control ops and payload lanes.

   Suggested critical-control slot shapes:

   ```c
   typedef struct HomerPeerControlRingHeader
   {
       uint32 protocolVersion;
       uint32 slotCount;
       uint32 slotBytes;
       uint32 flags;
       uint64 publishedTail;
       uint64 consumedHead;
   } HomerPeerControlRingHeader;

   typedef struct HomerPeerControlSlotHeader
   {
       uint32 readySeq;
       uint16 messageKind;
       uint16 requestKind;
       uint32 opIndex;
       uint32 generation;
       uint64 messageSequence;
       uint64 ownerServiceSessionId;
       uint64 peerServiceSessionId;
       uint32 localControlSlotIndex;
       uint32 payloadBytes;
   } HomerPeerControlSlotHeader;
   ```

   The first implementation can keep fixed-size request/response unions in each
   slot to avoid variable-size parsing in the control path. Compact variable-size
   control slots are a later cleanup only if control-ring memory footprint becomes
   a real issue; it is not a normal-path performance concern at current scale.

   Service-loop integration:

   - add lane progress helpers that each do bounded work and return:
     `HomerPeerLaneProgressCm()`, `HomerPeerLaneProgressBootstrap()`,
     `HomerPeerLaneProgressSendCq()`, `HomerPeerLaneProgressRecvCq()`, and
     `HomerPeerLaneProgressMailbox()`
   - update
     [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6145)
     so each pass advances connection phases, send-CQ retirement, incoming
     mailbox consumption, and pending peer-control ops with a small per-lane work
     budget
   - keep the baseline order fixed priority for now: critical control, foreground
     payload, bulk payload; the rule is "bounded work per lane, then return," not
     "drain one lane until idle"
   - make peer-control open/send APIs either return `IN_PROGRESS` or bind the
     pending op to the caller's local control slot; do not block the caller until
     the remote response arrives
   - make disconnect/reset best effort and nonblocking in the service loop; a
     lane may enter `CLOSING`/`FAILED`, but it must not spend a full RDMA timeout
     waiting for `DISCONNECTED` while other lanes are ready

   Relationship to semantic dynamic session state:

   - this transport state machine is **not** the same as the future authoritative
     remote-execution session state in
     [command_dispatch_completion_plane.md](../data-movement/command_dispatch_completion_plane.md#future-fix-explicit-dynamic-session-state)
   - transport lane state answers whether RDMA resources are connecting, ready,
     closing, or failed
   - peer-control op state answers whether a service-to-service request has been
     posted, sent, answered, and completed to the local control slot
   - remote-execution session state answers DB semantics: idle, command active,
     in transaction, failed transaction, closing, failed, and whether reuse is
     allowed
   - lane failure should mark affected peer-control ops failed, and those ops then
     update the relevant remote-execution sessions; do not collapse all three
     state domains into one enum

   Validation plan:

   1. Preserve standalone c1/c4 pgbench warmed performance before enabling
      concurrent basebackup.
   2. Preserve standalone remote RDMA basebackup warmed performance.
   3. Run concurrent c4 pgbench plus remote RDMA basebackup and verify no class-1
      peer-control reset occurs while class-3/bulk setup is pending.
   4. Add compile-time-gated counters for lane phase transitions, op-ring full
      events, publish-ring full events, async send CQ retirements, stale CQEs by
      generation, and max bounded-work iterations per pump pass.
   5. Keep the counters/logging disabled for performance measurements.

   This is a correctness and progress-isolation milestone before the pluggable
   scheduler. It is performance-relevant because blocking connection/control
   setup can stall foreground transaction lanes, but it should still keep all
   per-op storage preallocated and cache-local so the normal service-loop path
   does not acquire allocator, lock, or scan costs.

   May 29 implementation checkpoint:

   - Historical May 29 state: the code had **not** landed the full async
     peer-control operation state machine above. The safe landed subset kept
     peer-control publication
     blocking in
     [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3456)
     and still waits for the signaled tail `WRITE_WITH_IMM` send CQE before
     reusing the one-slot control scratch buffers.
   - A partial async-control experiment with preallocated publish slots and
     tagged WR ids was rejected. It preserved local RDMA source-buffer lifetime,
     but the current one-slot peer-control mailbox still depends on
     one-request-at-a-time sequencing and local control-slot progress. Under
     remote c4 pgbench it caused session-open/sql-execute timeouts and leaked
     remote exec backends that continued spinning on backend CPUs, which then
     distorted later warm performance. Do not revive that shape without the real
     multi-slot peer-control op ring described above.
   - The safe progress-isolation change that did land is nonblocking best-effort
     disconnect in
     [`TupleSinkServiceBestEffortDisconnectPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2391):
     it calls `rdma_disconnect()` but does not wait inline for
     `RDMA_CM_EVENT_DISCONNECTED`.
   - Shared send-CQ demux for unrelated tagged completions remains in
     [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1126),
     but there is no async peer-control send-CQ drain in the top-level service
     loop. This avoids polling a mostly-empty send CQ on every pass.
   - Warm validation after a clean restart and a c1 peer warmup preserved the
     expected standalone numbers: remote c4 pgbench reached about `11.0k TPS`
     with p99 about `0.616 ms`; remote RDMA basebackup warmed at `4.28-4.35s`.
     Concurrent c4 pgbench plus remote RDMA basebackup completed correctly with
     pgbench around `9.36k TPS` and basebackup `4.45s`.
   - Cold c4 setup can still time out and leave remote exec backend processes if
     clients abandon session-open requests while the peer path is still being
     established. For performance runs, clean up stale `remote exec backend`
     processes and treat the first post-restart run as warmup. The proper
     correctness fix is still the multi-slot peer-control op state machine, not
     a larger timeout.

   May 30 implementation checkpoint and cleanup status:

   - The peer-control request/response correlation state has landed.
     [`TupleSinkServiceReservePeerControlOp()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4058)
     and
     [`TupleSinkServiceCompletePeerControlOp()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4122)
     replaced the earlier connection-wide "currently waiting for response" shape
     with explicit op-index/generation/sequence matching.
   - The public async API also exists now:
     [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6456)
     publishes one request and returns a
     [`TupleSinkServicePeerControlAsyncOp`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:121),
     and
     [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6623)
     progresses that explicit op until it completes or fails. The old
     [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6731)
     is now a compatibility wrapper that starts an async op and waits on it.
   - Control publication is no longer locally blocking on the normal
     peer-control path.
     [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3786)
     writes the peer control slot and publishes the tail with a signaled
     `WRITE_WITH_IMM`, but it does not wait inline for the send CQE. Request
     publishes borrow op-owned registered source storage; response publishes
     borrow preallocated lane-owned response publish slots.
     [`TupleSinkServiceDrainTaggedSendCompletions()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1497)
     retires those sources later.
   - Tagged control WR ids carry op generation. This is required because a
     blocking compatibility adapter can time out or release an op before the
     local send CQE is drained; stale CQEs are ignored instead of mutating a
     later op that reused the slot. The hot path does not allocate heap state for
     this; request buffers, response publish slots, and op states are fixed
     lane-local arrays.
   - [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6919)
     now drains tagged send completions and peer-control responses as part of
     normal peer progress. A final optimization removed a redundant per-publish
     send-CQ drain and unused request-send bookkeeping stores after validation
     showed the start/poll path already drives completion progress.
   - Call-site migration should be staged. Peer open/bind/close and peer-sink
     setup should first bind their pending op to the local control slot or to an
     explicit continuation state, return `IN_PROGRESS`, and resume when the
     lane-local completion queue reports the op done. RDMA-CM connect/accept
     phases should follow the same rule: each pump pass performs bounded work and
     never waits inline for an RDMA timeout while other lanes have ready work.
   - Remaining cleanup details: migrate any remaining setup/legacy call sites off
     blocking adapters;
     propagate cancellation and lane failure by marking only the affected ops
     failed; keep debug counters/logs compile-time gated so the measured hot path
     does not pay for them.
   - Performance caveat from validation: an earlier concurrent c4 Homer pgbench
     plus remote RDMA basebackup run in this note measured about `9.36k TPS` for
     pgbench and `4.45s` for basebackup. A later run measured standalone c4 at
     about `11.41k TPS` and standalone basebackup at about `4.42s`, but one
     concurrent c4 plus basebackup artifact dropped to about `4.68k TPS` and
     `15.38s` basebackup. A May 30 rerun did **not** reproduce that drop:
     standalone c4/t10000 measured `11429.95 TPS`; concurrent c4/t2500 repeats
     measured `9545.20`, `9636.47`, and `9473.30 TPS` while basebackup completed
     in `4.33`, `4.45`, and `4.31s`; and a concurrent c4/t10000 run measured
     `9733.60 TPS` with basebackup `4.65s`. Treat the `4.68k / 15.38s` artifact
     as a stale/transient run condition unless it can be reproduced after
     cleaning stale `remote exec backend` processes and following the warm-run
     procedure.

   Final May 30 validation after rebuilding, installing, syncing the farnet0
   service binary, and restarting PostgreSQL plus both Homer services:

   - first post-restart remote c1 pgbench was a cold/warmup outlier because
     initial setup produced a `1173 ms` max latency; the warmed repeat completed
     `20000/20000` with zero failures at `4869.43 TPS`, p50 `0.203 ms`, p99
     `0.224 ms`
   - warmed remote c4 pgbench completed `40000/40000` with zero failures at
     `11457.72 TPS`, p50 `0.337 ms`, p99 `0.594 ms`
   - remote RDMA basebackup warmed at `4.32s`
   - concurrent remote c4 pgbench plus remote RDMA basebackup completed with
     both rc=0; pgbench completed `10000/10000` at `9486.10 TPS`, p50
     `0.383 ms`, p99 `0.772 ms`, while basebackup completed in `4.45s`
   - artifacts for the final concurrent run are under
     `/tmp/homer_async_cleanup_final_concurrent_20260530_101222`

   May 30 implementation checkpoint: transport-level async setup is now landed,
   while service-level local-control continuations are still the remaining
   nonblocking cleanup.

   - [`TupleSinkServicePeerControlAsyncOp`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:121)
     now owns the peer endpoint, request copy, traffic class, connection handle,
     and `requestPublished` flag. This lets `StartPeerRequest...` return with a
     request still waiting on RDMA-CM/bootstrap setup instead of requiring the
     peer-control message to have already been written.
   - Outgoing connect setup was split into
     [`TupleSinkServiceStartOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2918)
     plus
     [`TupleSinkServiceProgressOutgoingPeerConnectionSetup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3051).
     The phases now poll `ADDR_RESOLVED`, `ROUTE_RESOLVED`, `ESTABLISHED`, and
     bootstrap send/recv CQEs without sleeping inside the progress helper.
   - Incoming accept setup was split into
     [`TupleSinkServiceStartIncomingPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3296)
     plus
     [`TupleSinkServiceProgressIncomingPeerConnectionSetup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3065).
     The passive side no longer treats a listener `CONNECT_REQUEST` as "accept
     and finish bootstrap right now." A subtle correctness fix was needed here:
     [`TupleSinkServiceApplyPeerBootstrapMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2519)
     sets `bootstrapComplete`, but the passive-side async path clears it until
     its own bootstrap response send retires and
     [`TupleSinkServiceFinishPeerConnectionSetup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2744)
     posts notification receives.
   - Peer-control async start/poll now defers publication until the lane is
     ready through
     [`TupleSinkServiceStartOrProgressOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6817),
     [`TupleSinkServiceTryPublishPeerControlAsyncOp()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6911),
     [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7023),
     and
     [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7062).
   - [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7370)
     now progresses active but not-yet-bootstrapped incoming/outgoing lane slots
     before placing ready lanes into the fixed-priority service-loop lists at
     [`remote_execution_peer_transport_rdma.c:7422`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7422)
     and
     [`remote_execution_peer_transport_rdma.c:7465`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7465).
   - Validation after rebuild/install/sync on farnet1 and farnet0:
     remote c1 pgbench warmed at `4830.79 TPS`, p50 `0.204 ms`, p99
     `0.227 ms`; remote c4 pgbench warmed at `11087.77 TPS`, p50 `0.348 ms`,
     p99 `0.613 ms`; warmed remote RDMA basebackup completed in `4.26s`; one
     concurrent remote c4 pgbench plus remote RDMA basebackup run completed with
     both rc=0, pgbench `9454.57 TPS`, p50 `0.394 ms`, p99 `0.790 ms`, and
     basebackup `4.65s` (`/tmp/homer_async_impl_20260530_194920`).
   - Historical remaining cleanup at this checkpoint:
     [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7185)
     was still the blocking compatibility adapter, and service-level callers
     such as command-session open and peer-sink open still publish their local
     control response only after that wrapper returns. To satisfy the strict
     "service loop never waits on remote setup" invariant, the next cleanup must
     move these callers onto explicit pending local-control/session/stream
     continuations. The transport substrate is ready for that because async ops
     can now cover both "lane still connecting" and "peer request published,
     waiting response."

   Implementation plan for the remaining fully-async/nonblocking critical-control
   lane:

   1. **Define the no-blocking service-loop invariant.** The top-level progress
      loop in
      [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13791)
      must never wait for an RDMA-CM event, send CQE, peer response, remote
      connection establishment, or remote sink/session setup. A pump pass may
      poll nonblocking readiness, post bounded work, record an in-flight op, and
      return. Synchronous compatibility wrappers may remain only for cold tools
      or tests outside the service loop.

   2. **Convert outgoing RDMA-CM connect into lane phases.** The blocking path is
      currently
      [`TupleSinkServiceConnectOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2780):
      it posts `rdma_resolve_addr()` and waits for `ADDR_RESOLVED` at
      [`remote_execution_peer_transport_rdma.c:2836`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2836),
      waits for `ROUTE_RESOLVED` at
      [`remote_execution_peer_transport_rdma.c:2857`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2857),
      then waits for `ESTABLISHED` at
      [`remote_execution_peer_transport_rdma.c:2889`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2889).
      Replace that with `StartOutgoingPeerConnection(...)` and
      `ProgressPeerLaneConnect(...)` style helpers. The lane should carry a
      phase enum such as `IDLE`, `ADDR_RESOLVE_POSTED`, `ADDR_RESOLVED`,
      `ROUTE_RESOLVE_POSTED`, `ROUTE_RESOLVED`, `CONNECT_POSTED`,
      `BOOTSTRAP_POSTED`, `READY`, `FAILED`, plus the small amount of
      preallocated storage needed by the current phase.

   3. **Change ensure-connection callers from "ready or fail" to "pending or
      ready".** [`TupleSinkServiceEnsureOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3145)
      and
      [`TupleSinkServiceEnsureOutgoingPeerConnectionHandleRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3189)
      currently return only after the connection is ready. Add an async return
      shape such as `READY`, `IN_PROGRESS`, `FAILED`. If a peer-control op or
      payload open needs a lane that is still connecting, bind that waiting work
      to the lane's pending list and let the service loop resume it after
      `READY`.

   4. **Convert incoming accept/bootstrap into bounded lane progress.** The
      listener pump in
      [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6919)
      already polls the listener nonblocking, but
      [`TupleSinkServiceHandleIncomingConnectRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7033)
      still needs to be treated as a state transition, not a "finish everything
      now" helper. Accepted connections should enter incoming-lane phases:
      allocate slot, init resources, post bootstrap recv, accept, wait for
      `ESTABLISHED`, exchange mailbox descriptors, then mark `READY`. Each phase
      should advance only when its nonblocking event/CQE is available.

   5. **Move peer request/response waits out of call stacks.** The async
      substrate is present:
      [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6456)
      and
      [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6623)
      operate on explicit op handles. The remaining blocker is
      [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6731),
      which starts an op and spins on it. Migrate service-loop callers to store
      `TupleSinkServicePeerControlAsyncOp` in the owning session/stream/control
      slot and return `IN_PROGRESS` instead of waiting on stack-local response
      state.

   6. **Add continuations for command-session open.** The remote client-SQL open
      path blocks today in
      [`TupleSinkServiceHandleOpenCommandSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11919)
      when it calls
      [`TupleSinkServicePeerOpenCommandSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4591)
      at
      [`tuple_sink_service_process.c:11866`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11866).
      Add a pending-open state under the local control slot or session. The
      handler should create the local session, register command/completion MRs
      as needed, start connection/open ops, return a pending heartbeat/accepted
      state to the control slot, and publish the final local open response only
      after the peer command session is open. If the frontend abandons the slot,
      cancellation should mark the pending op canceled and reclaim any created
      session/backend resources.

   7. **Add continuations for peer sink/result open.** Remote result and payload
      setup block today in
      [`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8030)
      and at call sites such as
      [`tuple_sink_service_process.c:12382`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12382)
      and
      [`tuple_sink_service_process.c:12418`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12418).
      Add a stream state such as `PEER_OPEN_PENDING`; payload pumps must skip or
      cheaply test streams in that state until the peer-open op completes and
      installs the peer receive descriptor. This is a setup-path state check, not
      a per-record payload branch.

   8. **Make close/reset best-effort and lane-owned.** Close operations should
      enqueue peer close requests when possible but must not block foreground
      lanes on a remote close response. Reset should mark lane-local ops failed,
      return response-publish slots/source buffers to their pools when their
      generation no longer matches, and release semantic sessions/streams owned
      by those ops. The already-landed generation check in tagged control WR
      retirement is the model for stale CQE safety.

   9. **Performance constraints for the async design.**
      - No heap allocation, mutexes, or dynamic list nodes on the service-loop hot
        path. Op tables, pending open records, response slots, and lane phases
        should be fixed-size arrays or embedded in existing session/stream
        entries.
      - Use active lists or bitsets for lanes/ops needing progress. Do not add a
        full session/stream/op table scan to every pgbench command iteration.
      - Keep listener and cold connection progress out of the steady-state remote
        client-SQL command path. The warmed transaction hot path should still
        primarily run
        [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13811)
        plus result/completion/payload progress.
      - Poll send/recv CQs only for lanes with outstanding work or recent
        readiness. Avoid adding empty-CQ polling for idle bulk/basebackup lanes
        to every foreground transaction loop.
      - Compile out expensive counters/logging by default. Keep enough
        `HOMER_SERVICE_*_STATS` hooks to measure empty polls, pending-op counts,
        phase transitions, stale CQEs, and wait time during investigations.
      - Separate setup-path improvement from steady-state claims. Fully async
        connect/open primarily improves cold setup, tail latency, and concurrent
        interference. It should not be expected to raise warmed c1 TPS unless it
        also removes work from the already-open command/data path.

   10. **Validation gates before scheduler policy work.**
       - correctness: cold c4 remote pgbench setup with no leaked `remote exec
         backend` processes after cancellation/failure
       - correctness: standalone remote RDMA basebackup and remote client SQL
         pgbench still pass separately
       - correctness: concurrent remote c4 pgbench plus remote RDMA basebackup
         completes with both rc=0
       - performance: warmed remote c4 pgbench remains in the established
         `~11k TPS` band with p99 in the same `~0.6 ms` band
       - performance: warmed remote RDMA basebackup remains around the `4.3s`
         band
       - diagnostics: if a regression appears, counters must identify whether it
         comes from extra empty polling, broader active scans, connection phase
         churn, or added branches in the warmed transaction path

   May 31 implementation checkpoint: the service-level local-control
   continuation layer has landed for the remote open paths and the old
   backend/backend command start/poll paths that were still blocking the service
   loop.

   - The RDMA transport now exposes
     [`TupleSinkServiceEnsureOutgoingPeerConnectionHandleRdmaAsync()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6916).
     It progresses the outgoing lane setup once and returns `readyOut` instead
     of waiting until RDMA-CM/bootstrap is finished. The old
     [`TupleSinkServiceEnsureOutgoingPeerConnectionHandleRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3533)
     remains for compatibility callers outside the newly converted local-control
     path.
   - Remote `OPEN_SESSION` requests now use a fixed-size per-control-slot
     continuation table:
     [`LocalControlAsyncOps`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12576).
     The selector
     [`TupleSinkServiceOpenRequestNeedsAsyncLocalControl()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12582)
     diverts remote command-session opens and remote send-side payload/result
     stream opens away from the blocking handler.
   - Command-session opens progress through
     [`TupleSinkServiceProgressCommandOpenAsyncOp()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12776):
     create the local session, asynchronously ensure the critical-control lane
     when client-SQL mailbox registration needs a PD/QP, register the command
     and completion mailboxes, start the peer open through the async peer-control
     op, then publish the local response only after the peer response arrives.
   - Remote payload/result stream opens progress through
     [`TupleSinkServiceProgressStreamOpenAsyncOp()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13427):
     validate and create the local stream, asynchronously ensure the stream's
     traffic-class lane, register the sender-head mirror, start the peer open,
     bind peer descriptors, and finally publish the original local control
     response.
   - Old backend/backend remote `START_COMMAND` and
     `POLL_COMMAND_COMPLETION` local-control requests now also use the same
     continuation table when the session has a peer command endpoint. The
     selectors
     [`TupleSinkServiceStartCommandNeedsAsyncLocalControl()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12627)
     and
     [`TupleSinkServicePollCompletionNeedsAsyncLocalControl()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12648)
     divert those requests before the compatibility handlers run; the async
     phases are driven by
     [`TupleSinkServiceProgressLocalControlAsyncOp()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13544).
   - [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15204)
     now pumps pending local-control continuations before scanning new slots.
     A pending slot remains in `REQUEST_READY`; the service keeps the heartbeat
     moving and skips reprocessing until the continuation publishes
     `RESPONSE_READY`.
   - Performance cleanup: the pending-op table is guarded by
     [`LocalControlAsyncActiveCount`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12578),
     so warmed pgbench/basebackup loops do not scan the setup-only continuation
     array when no async local-control work is active. A follow-up hot-path
     cleanup removed the unconditional `memset()` of the large fixed-width
     peer-control message in
     [`TupleSinkServiceProgressPeerControlResponses()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6757);
     the mailbox-empty path no longer clears the embedded request/response
     union before it knows a response is present.
   - May 31 follow-up: the peer sink close exception has been removed. The
     earlier fire-and-forget close experiment was semantically unsafe because
     terminal command completion could become visible before result-stream EOS.
     The implemented fix is to make EOS an in-band tuple payload fact:
     [`MarkCitusTupleSinkPeerClosed()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1801)
     marks the last tuple-view record or emits a zero-row EOS record, and
     [`HomerServicePayloadStreamResultEosPosted()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3568)
     gates terminal completion until EOS is posted and the source bytes are
     retired. `TupleSinkServicePeerCloseSinkBestEffort()` is now local cleanup
     only.
   - Validation after rebuild/install/sync/restart on farnet1/farnet0:
     warmed remote c1 pgbench `4810.71 TPS`, p50 `0.205 ms`, p99 `0.229 ms`;
     warmed remote c4 pgbench `10979.91 TPS`, p50 `0.349 ms`, p99 `0.620 ms`;
     standalone remote RDMA basebackup cold/checkpoint run `6.39s` and warmed
     repeat `4.30s`; concurrent remote c4 pgbench plus remote RDMA basebackup
     completed with both rc=0, pgbench `9597.32 TPS`, p50 `0.386 ms`, p99
     `0.764 ms`, and basebackup `4.53s`
     (`/tmp/homer_async_full_final_20260531_083402`).
   - Follow-up cleanup: the generic blocking peer-request wrappers and
     blocking peer-open helpers have been removed. The old local-control
     handler bodies still exist for local/client-SQL cases, but their remote
     branches now fail defensively if reached:
     [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11604),
     [`TupleSinkServiceHandleStartCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14054),
     and
     [`TupleSinkServiceHandlePollCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14213).
     Normal remote open/start/poll therefore has one intended path: the async
     local-control continuation. Remote pgbench client SQL still uses direct
     command/completion rings after session open.
   - Validation after removing the generic blocking wrappers: `make -j8
     service-bin client-bin`, `git diff --check`, install/sync/restart, warmed
     remote c4 pgbench `11106.19 TPS`, p50 `0.348 ms`, p99 `0.616 ms`, zero
     failed transactions, and warmed remote RDMA basebackup `4.36s`. Service
     logs had no `must use async`, failed, error, mismatch, overrun, invalid, or
     ran-out diagnostics.
   - Validation after removing the peer-close/EOS exception: `make -j8
     service-bin client-bin`, `git diff --check`, rebuild/install/sync/restart,
     warmed remote c1 pgbench `4820.286 TPS`, p50 `0.205 ms`, p99 `0.228 ms`;
     warmed remote c4 pgbench `11144.880 TPS`, p50 `0.350 ms`, p99 `0.605 ms`;
     and warmed remote RDMA basebackup `4.35s`. A final short smoke completed
     `100/100` transactions with zero failures.

8. **Frontier-aware progress accounting cleanup - implemented June 2**
   - this pre-scheduler cleanup has landed for the current fixed-priority
     service loop; it does not implement scheduler policy yet
   - [`HomerPayloadProgressDelta`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:858)
     is a stack-local result object carrying `madeProgress`, future blocked/
     readiness fields, CQ/WR/byte counters, and the four frontiers:
     `transportCompletionFrontier`, `sourceReleaseFrontier`,
     `semanticObjectFrontier`, and `remoteCreditFrontier`
   - [`HomerServicePayloadProgressDeltaMade()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2235)
     derives the old `bool madeProgress` return shape from typed progress so the
     outer loop remains unchanged while callers stop treating all progress as the
     same event
   - byte-ring send completion now flows through
     [`HomerServiceApplyTrackedPayloadCompletionFrontier()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2530),
     which uses
     [`HomerServiceCompleteTrackedPayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2429)
     to retire transport resources independently from source-byte release and
     semantic-object completion
   - the fixed-slot sender path uses
     [`HomerServiceApplySequencePayloadCompletionFrontier()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2601);
     for that older layout the three frontiers still collapse to the same
     sequence, but the executor vocabulary now matches the byte-ring path
   - receiver consumed-head ACK CQ drain now reports transport-completion
     progress through
     [`HomerServiceDrainReceiverHeadAckCompletions()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11423)
     instead of mutating a caller-owned boolean
   - the cleanup is allocation-free on the payload hot path: the delta is
     stack-local per pump call and folds back into the existing boolean at the
     function boundary
   - validation after `make -j8 service-bin client-bin`, `git diff --check`,
     install/sync/restart on farnet1/farnet0:
     warmed remote RDMA basebackup `4.34s` and `4.46s`; warmed remote c1
     pgbench `4721.21 TPS`, p50 `0.209 ms`, p99 `0.231 ms`; warmed remote c4
     pgbench `11107.73 TPS`, p50 `0.347 ms`, p99 `0.608 ms`; concurrent remote
     c4 pgbench plus remote RDMA basebackup completed with both rc=0, pgbench
     `9366.28 TPS`, p50 `0.391 ms`, p99 `0.775 ms`, and basebackup `4.50s`
     (`/tmp/homer_concurrent_c4_frontier_cleanup_1780427101`)
   - next cleanup remains: replace owner-polled send-CQ helpers with lane-owned
     CQ drain that emits typed completion events/deltas; only after that should
     the full ready-set / progress-plan / egress-grant scheduler prototype
     consume these facts

   Design takeaways from this cleanup:

   - The scheduler should start from **executor feedback**, not from a new
     egress policy. The landed delta shape proved that the current fixed-order
     loop can preserve performance while returning typed progress. The first
     scheduler slice should therefore convert more pumps to return stack-local
     `HomerProgressResult`-style feedback before changing which source wins.
   - Completion drain must be a first-class progress source. A CQE can retire
     transport resources and reduce QP/source-resource pressure even when no
     producer bytes or semantic objects become releasable, especially with
     fragmentation. If scheduler readiness only watches producer frontiers, it
     will under-service CQ drain work and can create artificial backpressure.
   - ACK completion is transport-resource progress, not payload semantic
     progress. Receiver-head ACK CQEs protect source-word reuse and close/reclaim
     ordering, but they do not mean tuple/basebackup/WAL objects advanced. The
     first scheduler design should fold ACK/credit maintenance into lane-CQ
     drain as a transport-resource event kind rather than adding a separate
     first-class source.
   - The old `bool madeProgress` boundary is a current-code artifact to remove,
     not an API to preserve. New scheduler-facing executors should not return
     `madeProgress`; they should return success/failure and fill a rich progress
     result. Any loop-level idle/backoff decision should be derived inside the
     scheduler-integrated service loop from counters and frontiers, as
     [`HomerServicePayloadProgressDeltaMade()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2235)
     now does for the current fixed-order loop.
   - The `stillReady`, `blockedOnRemoteCredit`, and `blockedOnLocalCompletion`
     fields should be populated in the next slice. They are intentionally present
     in the landed delta but not yet fully used; the scheduler needs them to
     avoid repeatedly granting a source that made transport progress but cannot
     post more egress work.
   - Keep progress results stack-local and folded into fixed source/lane facts.
     Do not create heap event objects, per-object scheduler records, or broad
     scans to remember these deltas. The cleanup validated that the normal path
     can keep the accounting vocabulary without adding allocation or measurable
     overhead.

9. **Scheduler prototype**
   - first implementation slice: add fixed-table source refs plus source core /
     executor-feedback state and stack-local `HomerProgressResult`, then make the
     scheduler-integrated service loop call scheduler-facing executors directly.
     Executor return values are success/failure only; progress is reported only
     through the result object. Do not add a persistent compatibility-wrapper
     layer that converts results back to `bool madeProgress`.
   - implement cheap readiness/frontier metadata, ready-set construction, policy
     selection, egress grants, and feedback/progress accounting
   - add fixed-table source registration for lanes, sessions, command rings,
     completion rings, local control slots, payload streams, lane-CQ drain, and
     RDMA-CM setup. ACK/credit completion is folded into lane-CQ/transport
     resource progress for the first scheduler design.
   - make progress facts frontier-aware: track transport completion/resource
     frontier, source-release frontier, semantic-object frontier, and
     remote-credit frontier independently; do not use a single generic
     `madeProgress` bit to decide whether a source deserves future service
   - adapt existing completion loops first:
     `HomerServiceTrackPayloadCompletion()` /
     `HomerServiceCompleteTrackedPayload()` become the model for reporting
     transport-completion progress even when source release does not move, and
     the owner-polled send-CQ helpers should be replaced with lane-owned CQ drain
     before the full egress scheduler lands
   - model the two scheduling layers explicitly: service-progress grants decide
     which source/lane to touch and for how long; transport-egress grants decide
     bytes, records/objects, fragments, and WRs to post
   - add a lightweight service-progress policy interface that returns a bounded
     progress plan, not a per-WR callback and not an RDMA byte-batching decision
   - convert existing dedicated pumps into bounded grant executors while keeping
     their command/control/payload-specific logic specialized
   - integrate command/control/completion work into the same ready/grant model
     instead of leaving them as permanent dedicated service-loop side paths
   - treat the fixed-priority service loop from the multi-resource milestone as
     the baseline policy to replace, not as the final scheduler abstraction
   - cap sender payload publication by scheduler grant instead of only by
     `HOMER_SERVICE_PAYLOAD_SEND_COMPLETION_BATCH`
   - update executor feedback after every bounded grant: completed CQ count,
     empty polls, WRs posted, bytes posted, resource credits freed, source bytes
     released, semantic objects completed, remote credits received, and
     still-ready / blocked flags. These counters should be cheap and mostly
     compile-time-gated for diagnostics, but the frontier fields themselves are
     normal scheduler state.
   - keep DPU offload constraints in the metadata design: avoid per-object
     host-DMA readiness writes on the normal path; use owner-written frontiers and
     idempotent ready bits/active sets
   - add weighted/deficit/age-aware policies only after traffic-class lanes are
     measurable

10. **Push command completions - local frontend completed, peer still future**
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

11. **Fragmentation/reassembly path - basebackup RDMA implemented June 2, 2026**
   - keep fragmentation as a first-class grant dimension from the scheduler's
     first design, even if the first implementation grants only whole records
   - start with the minimal ordered-stream design: extend
     `CitusTupleSinkTransportHeader` with `FRAGMENT_FIRST` and `FRAGMENT_LAST`
     flags, keep `payloadBytes` as fragment byte count, and keep `sinkSequence`
     as the semantic object sequence
   - avoid a standalone generic fragment header in the first implementation; rely
     on the existing transport record envelope plus one active reassembly object
     per ordered stream
   - keep backend producers publishing complete semantic objects; generate
     per-fragment transport headers in the sending service from a pre-registered
     fixed header pool
   - extend the RDMA payload write descriptor/helper to support a two-SGE fragment
     write, with `SGE 0` as the generated transport header and `SGE 1` as the
     zero-copy producer byte slice
   - keep transport records no-wrap initially; use the existing byte-ring gap-skip
     behavior at ring wrap instead of teaching the first reassembly path to join
     a header split across the end of the ring
   - add receiver-side reassembly state below object-family delivery; invoke
     the basebackup validator/accounting only after `FRAGMENT_LAST`
   - add RDMA-substrate fragmentation/reassembly when object-level grants are too
     coarse for latency/throughput goals, especially for large basebackup/WAL
     records
   - make fragment sizing tunable, with at least a `maxGrantBytes` or MTU-like
     target fragment size and room for a later dynamic policy

## Related

- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md): responder-visible RDMA publication rule this scheduler must preserve.
- [rdma_transport_control_plane_abstraction.md](rdma_transport_control_plane_abstraction.md): broader transport/control abstraction context.
- [command_dispatch_completion_plane.md](../data-movement/command_dispatch_completion_plane.md): command lifecycle and completion plane that should move from polling toward push completion.
- [base_backup_service_abstraction.md](../../postgres/replication/base_backup_service_abstraction.md): basebackup object-family semantics above the neutral payload stream.
