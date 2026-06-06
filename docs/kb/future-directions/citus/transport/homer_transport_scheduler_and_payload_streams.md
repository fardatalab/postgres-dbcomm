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

## Current scheduler-ready state - June 3, 2026

The scheduler-ready milestone is complete in the current
`/data/dbcomm/citus-dbcomm` worktree. The service loop now runs through an
explicit two-layer scheduler boundary:

1. `main()` selects cold-start policies with
   [`HomerServiceReadProgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3211)
   and
   [`HomerServiceReadEgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3146),
   initializes the fixed progress registry, then repeatedly calls
   [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19774).
2. `TupleSinkServicePumpOnce()` builds a bounded coarse ready set with
   [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4532),
   dispatches to a pluggable service-progress policy through
   [`HomerServiceBuildProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4194),
   begins any pass-wide payload egress budget, executes selected sources, and
   merges executor feedback.
3. Send-capable payload sources request a second-layer egress grant at the payload
   executor boundary through
   [`HomerServiceBuildPayloadEgressReadyFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3018)
   and
   [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3077).

The current first-layer vocabulary is implemented in
[`HomerProgressSourceKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1193),
[`HomerProgressCpuClass`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1219),
[`HomerProgressSourceCore`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1270),
[`HomerProgressSourceFeedback`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1297),
[`HomerProgressReadySet`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1355),
[`HomerProgressSourcePlan`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1372),
and [`HomerProgressResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1543).
The default `fixed-priority` policy remains the baseline, while opt-in research
policies include `round-robin`, `unblocked-first`, and `cpu-liveness`
([policy parser](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3211),
[policy dispatch](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4194)).
The `cpu-liveness` policy is the first policy that consumes the new
service-progress CPU/liveness classes and emits bounded peer-control phase
subgrants through
[`HomerServiceBuildPeerControlProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5635)
and
[`TupleSinkServicePeerPumpGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:255).

The remaining work is post-milestone research/engineering, not missing
scheduler-ready substrate: choose and validate better default policies, add
adaptive/weighted/age-aware policy state if measurements justify it, and decide
whether service-to-service peer command completions should move from
`POLL_COMMAND_COMPLETION` to a fixed-size peer push completion ring. The root
handoff note
[`HOMER_SERVICE_PROGRESS_SCHEDULER_ONBOARDING.md`](/data/dbcomm/postgres-citus/HOMER_SERVICE_PROGRESS_SCHEDULER_ONBOARDING.md:1)
is the practical starting point for a coworker implementing new policies.

## Grounding

Historical grounding: this section originally described the pre-scheduler
standalone service loop, where `main()` hardcoded the order of service progress:

1. local control slots through
   [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19387)
2. peer-control RDMA mailbox progress through
   [`TupleSinkServicePumpPeerConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12572)
3. local socketless-backend completions through
   [`TupleSinkServiceConsumeAllCompletionMailboxes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8532)
4. every active payload state through
   [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15797)

At the time, that was a service-progress loop, not yet a scheduler.

The RDMA egress points that need real scheduling are lower:

- peer-control publication through
  [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4110)
- payload range publication through
  [`TupleSinkServicePostPeerRegisteredPayloadBatchWithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5642)
- sender payload batching in
  [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13998),
  especially the open-ended loop at
  [`tuple_sink_service_process.c:14296`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14296)
  and the current per-call cap through
  `HOMER_SERVICE_PAYLOAD_SEND_COMPLETION_BATCH` at
  [`tuple_sink_service_process.c:174`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:174)

That original grounding remains useful as design history, but the current code
has moved those paths under the scheduler-ready ready-set/source-plan/executor
boundary summarized above.

The current peer transport also shares send-CQ machinery between control writes
and payload write completions. The payload WR id tagging comment in
[`remote_execution_peer_transport_rdma.c:75`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:75)
is a useful signal: control and payload already contend for some of the same
transport resources.

## Local control slots, peer control, and completions

Local control slots are a local control-plane command queue between local actors
and the Homer service. The actors may be PostgreSQL backends, modified frontend
clients such as pgbench, or PostgreSQL backend code using the frontend-safe Homer
client library. The queue is
[`CitusRemoteExecControlRegion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:549),
which owns fixed
[`CitusRemoteExecControlSlot`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:539)
entries. Request kinds are defined in
[`CitusRemoteExecControlRequestKind`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:56):
open session, close session, report post-command state, start command, and poll
command completion.

Peer control is the service-to-service equivalent. Its request vocabulary is
[`CitusRemoteExecPeerRequestKind`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:40):
peer open, close sink, start command, poll command completion, and open command
session. The transport implementation currently publishes those fixed-width
messages over a persistent RDMA control mailbox. The mailbox is now a shared
fixed-width multi-slot ring, and normal remote open/start/poll callers use the
explicit async op substrate. The earlier close-specific peer-control sender has
been removed; finite tuple-result close/EOS is now represented in-band in the
payload stream and the local cleanup helper
[`TupleSinkServicePeerCloseSinkBestEffort()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8076)
no longer sends a remote close request. The common peer-control publication point is
[`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3975).

Backend completions are the local socketless-backend-to-service completion path.
The service publishes exactly one command record into a session command mailbox
through
[`TupleSinkServicePublishLocalCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2576),
and later consumes the backend's completion mailbox through
[`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2273).

## Target Peer-Control Substrate

The target design is a multi-slot peer-control substrate, not a short-term
serialization shim around the current one-message mailbox. This is required for
concurrent client sessions and for later transaction plus base-backup
interference experiments.

Historical note: this section was originally written when peer control used a
one-message waiting latch. That limitation has been removed. The current
transport has a shared multi-slot control ring, explicit op-id/generation state,
and async start/poll APIs:
[`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6928)
and
[`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6967).
The close/EOS ordering cleanup is complete for client SQL tuple results. The
remaining work is scheduler policy plus removing wait/poll scaffolding from the
older backend/backend peer command path, not rebuilding a generic blocking
request/response mailbox fallback.

The target substrate should provide:

- multi-slot RDMA-registered peer-control rings, replacing the single fixed
  mailbox slot described by
  [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2563)
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
`0.668 ms` in the same follow-up run shape). The service-side immediate command
experiment has since been removed because the multi-slot command ring cannot be
addressed by the old session-id-only immediate token. The current service path
uses direct slot-local `readySeq` publication: one WR when whole-message
placement is supported and the older record-plus-ready fallback otherwise. A
follow-up ordering fix had made the immediate drain publish the backend-visible
epoch before receive-WQE reposting via
[`TupleSinkServiceDrainPeerConnectionCommandDoorbellsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2583),
but warmed remote pgbench remained essentially unchanged (`4636 TPS` c1 and
`10100 TPS` c4). Keep that better ordering principle for any future
receiver-service fallback, but any future command immediate path must be
redesigned around slot/sequence routing rather than reviving the removed
service-side branch.

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
  [`CitusRemoteExecStartCommandResponse.commandCompletion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:494)
- if the command completes later on a local frontend session, the service should
  publish a terminal completion event into a caller-visible per-session
  completion mailbox and signal the waiting side, rather than requiring the
  caller to submit a separate poll request.
- `POLL_COMMAND_COMPLETION` should be removed from the normal command lifecycle
  once push completion is implemented end-to-end. While the transition is in
  progress, it may remain as a debug/compatibility fallback only.

The local backend-to-service path is already close to push. The backend writes a
fixed-width
[`CitusRemoteExecCommandCompletion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:422)
into
[`CitusRemoteExecLocalCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:146)
and bumps `publishedEpoch` in
[`PublishCompletionToMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:271).
The service then observes that epoch in
[`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2273).
That part does not require a caller-submitted poll request. The remaining
poll-like scaffolding is higher level: local clients can still fall back to
[`TupleSinkServiceHandlePollCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8237)
for compatibility/debugging, and peer services still call
[`TupleSinkServiceHandlePeerPollCommandCompletionRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8620)
until the peer completion ring exists.

For peer command completion, replacing the peer poll request in
[`CitusRemoteExecPeerRequestKind`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:40)
requires a responder-initiated completion publication. The completion should use
the same responder-visible RDMA publication rule described in
[rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md):
write completion metadata first, then publish with a doorbell.

Concrete no-poll plan:

Implementation detail for the service-to-service peer prerequisite is tracked in
[peer_service_push_completion_implementation_plan.md](peer_service_push_completion_implementation_plan.md).
That note narrows the next step to a requester-owned peer command completion
ring, the requester-side consumption path, and the replacement for
`terminalCompletionPendingPeerPoll`.

1. **Keep one in-flight command per local frontend session.** Preserve the
   single-slot command mailbox invariant documented by
   [`TupleSinkServicePublishLocalCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2576).
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
   [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2273)
   observes `STARTED`, `COMPLETED`, or `FAILED`, the service should copy the
   fixed-width `CitusRemoteExecCommandCompletion` into the bound caller-visible
   completion ring and publish it with an epoch/doorbell. This replaces the
   local-client branch in
   [`TupleSinkServiceHandlePollCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8237).

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
  [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:458)
- backend-to-service completion is also an 8-slot
  [`CitusRemoteExecLocalCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:146);
  the earlier single-slot backend completion mailbox was not correct once SQL
  result materialization could publish `STARTED` and terminal states close
  together
- the service publishes `STARTED`, `COMPLETED`, and `FAILED` local client
  completions through
  [`TupleSinkServicePublishClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1620)
- frontend clients map and observe the mailbox through
  [`HomerClientOpenCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:520),
  [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2033),
  and [`HomerClientWaitCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2148)
- pgbench now waits for terminal completion through
  [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2033)
  at [`HomerRunCommandAndWait()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3731)
  instead of issuing a `POLL_COMMAND_COMPLETION` request
- service-to-service peer push completion is still future work and still needs
  the fixed-size peer completion ring described above

## Client SQL wait-loop fix

Implementation progress: the old masked local client SQL wait is now a
transitional compatibility/debug helper, not the measured pgbench path. The
service has an internal
[`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8776)
progress helper with explicit masks. The main loop calls it with
[`TUPLE_SINK_SERVICE_PUMP_MAIN_LOOP`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:703)
at
[`main()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8942),
covering local control, peer control, all backend completions, sink/payload
progress, and the heartbeat.

[`TupleSinkServiceWaitForLocalCommandStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2796)
is still present in the source, but it is no longer called by the local
client-SQL `START_COMMAND` branch. If used by an older local synchronous path,
it keeps the command's own completion mailbox check on every spin and also runs
a periodic background
[`TUPLE_SINK_SERVICE_PUMP_LOCAL_WAIT_BACKGROUND`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:715)
pump at
[`tuple_sink_service_process.c:2850`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2850).
That background pump drains all completion mailboxes and active payload-stream
work, including deferred close/reclaim paths reached through
[`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6882).
That fallback cadence is controlled by
[`HOMER_SERVICE_LOCAL_WAIT_BACKGROUND_PUMP_INTERVAL`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:114),
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

- [`TupleSinkServiceHandleStartCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7997)
  now has a submit-only local client-SQL branch: it publishes the command to the
  socketless backend and returns `PENDING`; `STARTED` and terminal states are
  pushed later through the frontend completion mailbox.
- [`TupleSinkServiceWaitForLocalCommandStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2796)
  remains in the source for compatibility/debug paths and older local command
  shapes, but the measured pgbench path no longer depends on it.
- Peer command startup still waits in
  [`TupleSinkServiceWaitForCommandStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2701).
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
   [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2273).
3. The normal top-level service loop consumes backend state and pushes events to
   the appropriate caller-visible completion channel:
   - local frontend: the existing small-ring
     [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:458)
   - peer service: the requester-service-owned fixed-size peer completion ring
4. A row-producing SQL command publishes a `STARTED` event with result-sink
   readiness once
   [`RemoteExecSqlDestStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:678)
   has created/bound the result sink. The frontend/peer can then drain payload
   while the backend keeps executing.
5. Terminal `COMPLETED` / `FAILED` events are pushed later, independent of the
   original `START_COMMAND` response.
6. Session retirement waits for required completion delivery instead of waiting
   for a blocking handler response or a later poll.

Why this removes the peer-control acceptance issue:

- peer-control requests are accepted only from the normal top-level
  [`TupleSinkServicePumpPeerConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5786)
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
[`HomerServicePayloadStreamEntry`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:668).
The generic container is
[`HomerPayloadStreamState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:569),
tuple-family metadata is in
[`HomerTupleViewPayloadState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:629),
and basebackup-family receiver accounting is in
[`HomerBaseBackupPayloadState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:652).
The service derives and caches the payload object family at stream-open time via
[`TupleSinkServicePayloadFamilyForOpKind()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:795)
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
   [`HomerServicePayloadStreamEntry`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:668).
2. Move generic fields from
   [`HomerServicePayloadStreamEntry`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:668)
   into `stream`: queue mappings, slot geometry, peer binding, RDMA handles,
   sender/receiver frontiers, doorbell state, transport-broken state, and future
   scheduler metadata. **Done, except future scheduler fields are still future.**
3. Move tuple-only fields into `HomerTupleViewPayloadState`, and move
   basebackup counters currently at
   [`HomerBaseBackupPayloadState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:652).
   **Done.**
4. Add a cached `HomerPayloadObjectFamily` field. Do not use
   `semanticOpKind` as the long-term dispatch primitive; map session op kind to
   object family at open time. **Done** through
   [`TupleSinkServicePayloadFamilyForOpKind()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:795).
5. Refactor sender validation now embedded in
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6069)
   into family-specific helpers that validate the in-place object header and
   return the exact byte range to RDMA-write. **Done** in
   [`HomerServiceValidateOutgoingPayloadObject()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5843).
6. Refactor receiver validation and consumption now embedded in
   [`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6576)
   into family-specific helpers. Tuple-view consumption still decodes into the
   receive queue; basebackup consumption still blackholes/counts until a real
   materializer lands. **Done** in
   [`HomerServiceValidateReceivedPayloadObject()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5961).
7. Refactor local basebackup smoke consumption currently in
   [`HomerServicePumpLocalBaseBackupBlackhole()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6489)
   to call the same basebackup object-family validation/counting helper. **Done**
   with shared accounting in
   [`HomerServiceRecordBaseBackupPayloadObject()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5942).
8. Rename the outer table entry and pump functions only after the embedded
   stream split is stable. **Done** with `HomerServicePayloadStreamEntry`,
   [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6882),
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6069),
   and
   [`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6576).
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
  [`HomerPayloadStreamState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:569);
  tuple-view state lives under
  [`HomerTupleViewPayloadState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:629);
  basebackup counters live under
  [`HomerBaseBackupPayloadState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:652).
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
  [`RemoteExecEnsureSessionResultCopySlot()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:580),
  so [`RemoteExecSqlDestStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:678)
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
  [`--client-cpus=LIST`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:1115)
  so worker threads are assigned round-robin across separate CPUs.

## Execution lane removal

`executionLane` currently lives in
[`CitusRemoteExecSessionKey.executionLane`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:182),
with constants
[`CITUS_REMOTE_EXEC_LANE_DEFAULT`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:117)
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
  [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8889),
  with active payload-stream scanning at
  [`tuple_sink_service_process.c:8950`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8950).
- **Transport egress scheduling** decides which RDMA-sendable work is posted to
  the NIC next. Today
  [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6239)
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
[`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15170).
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
   [`HomerPayloadProgressDelta`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:858)
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
   [`TupleSinkServiceStashPayloadSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1222),
   [`TupleSinkServicePopStashedPayloadSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1250),
   and the owner-specific
   [`TupleSinkServicePollPeerPayloadSendCompletionRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6142)
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
     [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4884),
     scoped to one command-ring source and bounded by `maxItems`
   - local-control executor adapted from
     [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14972),
     bounded by slot count or per-pass item count
   - peer-control/lane executor adapted from
     [`TupleSinkServicePumpPeerConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8666)
     and
     [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7084),
     scoped to a lane/source
   - completion executor adapted from
     [`TupleSinkServiceConsumeAllCompletionMailboxes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4578),
     scoped to a session/completion source when possible
   - payload executor adapted from
     [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11509),
     which then calls the egress scheduler before posting RDMA work

   Unification is at the scheduling interface: source facts, progress grants,
   egress grants, and feedback. It is not a mandate to use one generic object
   format for commands, peer control, tuple payload, and basebackup payload.

6. **Build the scheduler-integrated service loop.**
   The current fixed ordering in
   [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15170)
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
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9703)
   should consume a byte/object/fragment/WR grant instead of choosing the final
   batch solely from internal caps such as
   `HOMER_SERVICE_PAYLOAD_SEND_COMPLETION_BATCH`. Later, peer-control publication
   through
   [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3975)
   and async peer requests through
   [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6928)
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
now in the code. [`HomerTransportTrafficClass`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:63)
and [`HomerTransportSchedulingMetadata`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:83)
define the internal traffic-class/scheduler metadata. Each payload stream caches
that metadata in
[`HomerPayloadStreamState.transportScheduling`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:630),
initialized by
[`HomerServiceInitPayloadTransportScheduling()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:844).
The current semantic-to-traffic-class mapping is:

- tuple/COPY/result payload -> foreground payload, selected by
  [`HomerServicePayloadTrafficClassForOpKind()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:826)
- basebackup payload -> bulk payload, selected by the same helper
- ordinary peer-control requests/responses -> critical control through the
  async op pair
  [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6928)
  and
  [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6967)
- payload open control -> the payload stream's traffic class through the same
  async op pair, driven by
  [`TupleSinkServiceProgressStreamOpenAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13080)
- payload close/EOS -> in-band payload-stream EOS for tuple results through
  [`CITUS_TUPLE_SINK_TRANSPORT_FLAG_EOS`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:37),
  followed by local sender-side cleanup in
  [`TupleSinkServicePeerCloseSinkBestEffort()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8076)

The transport now uses the traffic class as part of the outgoing connection
cache key in
[`TupleSinkServiceFindOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2599).
The bootstrap message carries the lane class at
[`TupleSinkServicePrepareBootstrapMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2011),
and
[`TupleSinkServiceApplyPeerBootstrapMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2034)
rejects invalid or mismatched lane classes. A connection handle is then guarded
against class mismatch in
[`TupleSinkServicePeerConnectionForTrafficClass()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2786).
The service loop drains lane-local CQs in fixed priority order using
[`HomerTransportServiceLoopPriority`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:78):
critical control, foreground payload, bulk payload, then maintenance. This is a
baseline policy, not yet the pluggable research scheduler.

Important implementation correction: payload peer-open must ride on the same
traffic-class lane as the payload writes. The receiver registers its byte
ring/tuple ring against the accepted connection handle, so if bulk payload
opened via the critical-control lane but payload bytes arrived on the bulk lane,
the remote rkey/PD ownership would be wrong. The sender-side open call is
[`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8065),
and best-effort close uses the matching class at
[`TupleSinkServicePeerCloseSinkBestEffort()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8196).

Second correction from validation: remote basebackup blackhole has no
frontend-visible POSIX receive queue on the responder. The receiver now returns
only the byte-ring RDMA descriptor for basebackup and leaves
`peerReceiveQueueDescriptor` zeroed at
[`TupleSinkServiceHandlePeerOpenRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8597).

Validation on the final multi-resource baseline binary:

- `make -j8`, `sudo -n make install-headers install-service-bin install`, and
  rsync to farnet0 passed in `/data/dbcomm/citus-dbcomm`.
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
  `/data/dbcomm/citus-dbcomm`.
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
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6255),
   especially validation through
   [`HomerServiceValidateOutgoingPayloadObject()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6029)
   and descriptor construction before
   [`TupleSinkServicePostPeerRegisteredPayloadBatchWithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3648).

   Reusable pattern: tuple/result/WAL payload streams should choose object sizes
   that amortize transport metadata, but they should not blindly make semantic
   objects so large that later foreground traffic cannot interleave. For very
   large objects, the longer-term answer is substrate-level fragmentation rather
   than mutating producer-level semantics.

2. **Asynchronous posted/completed frontiers removed a false source-slot
   serialization point.**

   The sender now distinguishes "posted to the NIC" from "safe to release the
   backend-visible source slot." That lets
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6255)
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
   [`TupleSinkServicePostPeerRegisteredPayloadBatchWithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3648).
   Earlier warmed 64 x 1 MiB runs reached the `5.08s` band after range
   publication, compared with `6.60s` after only the asynchronous frontier split.

   Reusable pattern: keep the object-family abstraction at the stream layer, but
   let the transport publish ranges when the ring contains adjacent ready
   objects. This applies to tuple batches, basebackup chunks, and future WAL
   chunks.

4. **The empty-CQ-poll guard removed wasted progress work without changing
   semantics.**

   The sender should only call
   [`TupleSinkServicePollPeerPayloadSendCompletionRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3941)
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
   [`HomerClientSubmitBaseBackupRecord()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1606)
   was actually present in the backend process.

   Reusable pattern: benchmark notes must record whether the running postmaster
   was restarted after `libhomer_client.a` or PostgreSQL relinks. Otherwise we
   can benchmark stale code and misattribute the result to transport design.

### Refuted or narrowed hypotheses

1. **Receiver ring credit was not the warmed bottleneck in the measured
   single-stream run.**

   Diagnostic counters showed `remote_waits=0` in the sender path. That means
   the branch in
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6551)
   was not constraining that run. Increasing receive slot count therefore did
   not help. Receiver credit can still matter in a different scale point or with
   a slower materializing receiver, but it was not the root cause for the warmed
   blackhole receiver experiment.

2. **Local source-slot / send-completion credit was not the warmed bottleneck in
   the same run.**

   Diagnostic counters also showed `local_waits=0`, so the local in-flight
   check at
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6477)
   was not blocking normal progress. This narrows the remaining cost toward WR
   count, doorbell count, validation/header work, service-loop overhead, or cold
   setup effects rather than hard credit stalls.

3. **Receiver consumed-head ACK frequency is not the first-order issue for the
   warmed blackhole receiver.**

   Receiver ACKs are already thresholded by
   [`HomerServiceShouldPublishReceiverHeadAck()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6911),
   capped by `HOMER_SERVICE_RECEIVER_HEAD_ACK_MAX_GRANULARITY`. The receiver
   publishes credit through
   [`HomerServicePostReceiverHeadAck()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6846).
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
   [`bbsink_homer_archive_contents()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:277)
   and
   [`bbsink_homer_manifest_contents()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:331),
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

   [`HomerClientSubmitBaseBackupRecord()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1606)
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

   [`TupleSinkServicePostPeerRegisteredPayloadBatchWithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3648)
   no longer posts a separate 8-byte tail `RDMA_WRITE_WITH_IMM`. The final
   payload WR in the batch uses `IBV_WR_RDMA_WRITE_WITH_IMM`, and the sender
   still tags the signaled completion with the completed semantic frontier for
   source-slot release.

   On the receiver,
   [`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7056)
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
   [`TupleSinkServicePostPeerRegisteredPayloadBatchWithTailImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3826),
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
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6260).
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
   [`basebackup_sink.h`](/data/dbcomm/postgres-citus/src/include/backup/basebackup_sink.h:82)
   says `bbs_buffer`/`bbs_buffer_length` are generally stable after creation.
   The current Homer sink already bends that by swapping to a newly reserved
   slot at callback boundaries; packing partial callbacks into a single slot by
   continually exposing suffix pointers would bend it further and can break
   callers that assert a BLCKSZ-multiple buffer length, such as
   [`sendFile()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup.c:1617).
   A safe version needs separate design: either accept an explicit copy into a
   Homer-owned aggregation slot, add a PostgreSQL-side sink API that supports
   appendable variable views, or move fragmentation/coalescing lower in the
   transport without changing `bbsink` semantics.

3. **Compile out redundant hot-path validation for performance runs after
   correctness is established.**

   Sender-side validation in
   [`HomerServiceValidateOutgoingPayloadObject()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6029),
   transport-header setup in
   [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6620),
   and receiver-side validation in
   [`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7094)
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
     remains in the tuple/result measured path. This is a completed validation
     point, not an open scheduler-ready concern.

   **Stage 2: optimize after the common byte-ring substrate is in place. Status:
   partially completed; transport scheduler and multiple QPs remain future
   work.**

   This stage should avoid polishing the deleted fixed-slot path. The goal is to
   squeeze overhead out of the byte-ring substrate and make it scheduler-ready.

   Completed in the first post-migration optimization pass:

   - tuple-sink send/receive batch handles are embedded in the sink handle, so
     the measured tuple/result path no longer pays per-batch `palloc`/`pfree`
   - fixed `pg_usleep(1000L)` sleeps were removed from the Citus Homer
     backend/worker wait loops and replaced with CPU-relax spinning; this is
     historical context for the current measured path, not remaining scheduler
     work
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

2. **Async command submission - local frontend and peer normal path completed**
   - make local and peer `START_COMMAND` handlers publish work and return
     immediately
   - push `STARTED`, result-sink readiness, `COMPLETED`, and `FAILED` through
     completion channels
   - remove local and peer startup waits from normal paths
   - keep peer completion polling only as fallback/debug while broader
     validation determines whether it can be deleted

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
   [`CitusRemoteExecLocalCommandMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:164),
   [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4870),
   and
   [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2131).
   Warmed remote c4/j4 runs around the `11k TPS` band, while c1/j1 is around the
   `4.3k-4.8k TPS` band on the current two-WR fallback path.

   The remaining command-transport limitations are now:

   - on the current farnet configuration, the one-WR self-publishing command slot
     fast path is disabled because
     [`ibv_query_qp_data_in_order()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1311)
     reports no whole-message RDMA write ordering support; the measured path
     therefore posts an unsignaled command-record write plus a tagged `readySeq`
     write
   - asynchronous source-slot retirement is implemented: the command sender no
     longer waits inline for the send CQE before advancing frontend source credit,
     and
     [`TupleSinkServicePollRemoteClientSqlCommandWriteCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1866)
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
     [`TupleSinkServicePeerConnectionForTrafficClass()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4737)
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
   [`TupleSinkServiceRetireClientSqlCommandWriteCompletionsThrough()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2199).
   It also gives the traffic-class QP split a natural owner for per-resource
   post/completion frontiers.

   The current RDMA layer still has CQ ownership details to clean up within each
   traffic-class connection. Physical lanes/QPs remove cross-class posted-WR HOL,
   but helpers still poll the lane send CQ for one owner at a time. For example,
   [`TupleSinkServicePollPeerPayloadSendCompletionRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6142)
   asks for completions for one `serviceSinkId`, while
   [`TupleSinkServicePollPeerCommandSendCompletionRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6320)
   asks for command write completions. Because the send CQ is lane/connection
   owned, either helper may see a valid CQE for a different owner. The current
   workaround is to stash unrelated tagged completions in
   [`TupleSinkServiceStashPayloadSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1222)
   and later recover them through
   [`TupleSinkServicePopStashedPayloadSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1250).

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
     [`HomerServicePayloadTrafficClassForOpKind()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1262):
     client SQL / tuple payloads map to `FOREGROUND_PAYLOAD`, while basebackup
     maps to `BULK_PAYLOAD`.
   - peer payload open already binds a stream to the class-specific connection in
     [`TupleSinkServiceOpenPeerSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8040),
     especially the class-specific connection lookup at
     [`tuple_sink_service_process.c:8047`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8047)
     and peer-open send at
     [`tuple_sink_service_process.c:8096`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8096).
   - the missing long-term piece is narrower: make the **critical-control lane**
     follow the same lane-owned, multi-slot, explicit-work-ownership model. Do
     not create per-session peer-control QPs/mailboxes merely because SQL sessions
     are per-session; peer-control open/bind/close happens across many sessions
     and even before a remote DB session exists.

   Current blocking points to remove:

   - outgoing RDMA-CM setup in
     [`TupleSinkServiceConnectOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2447)
     waits synchronously for `ADDR_RESOLVED`, `ROUTE_RESOLVED`, and
     `ESTABLISHED`
   - incoming accept in
     [`TupleSinkServiceAcceptIncomingPeerConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2638)
     waits synchronously for `ESTABLISHED`
   - peer-control request/response publication in
     [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3456)
     currently posts the mailbox write and tail `WRITE_WITH_IMM`, then waits
     inline for the send CQE before the scratch buffers may be reused
   - service-to-service peer request submission in
     [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5766)
     owns a stack-frame response wait loop instead of creating an operation that
     the service loop can advance
   - teardown in
     [`TupleSinkServiceBestEffortDisconnectPeerConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2391)
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
     [`TupleSinkServicePeerControlMailbox`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:116)
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
     [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5766).
   - cancellation and abandonment are first-class op transitions. If a frontend
     local control request times out or disconnects while an open has already
     spawned a socketless backend through the spawn path in
     [`TupleSinkServiceStartRemoteExecBackend()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4168),
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
     [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6145)
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
     [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3456)
     and still waits for the signaled tail `WRITE_WITH_IMM` send CQE before
     reusing the one-slot control scratch buffers.
   - A partial async-control experiment with preallocated publish slots and
     tagged WR ids was rejected. It preserved local RDMA source-buffer lifetime,
     but the current one-slot peer-control mailbox still depends on
     one-request-at-a-time sequencing and local control-slot progress. Under
     remote c4 pgbench it caused session-open/sql-execute timeouts. Do not
     revive that shape without the real multi-slot peer-control op ring
     described above.
   - The safe progress-isolation change that did land is nonblocking best-effort
     disconnect in
     [`TupleSinkServiceBestEffortDisconnectPeerConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2391):
     it calls `rdma_disconnect()` but does not wait inline for
     `RDMA_CM_EVENT_DISCONNECTED`.
   - Shared send-CQ demux for unrelated tagged completions remains in
     [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1126),
     but there is no async peer-control send-CQ drain in the top-level service
     loop. This avoids polling a mostly-empty send CQ on every pass.
   - Warm validation after a clean restart and a c1 peer warmup preserved the
     expected standalone numbers: remote c4 pgbench reached about `11.0k TPS`
     with p99 about `0.616 ms`; remote RDMA basebackup warmed at `4.28-4.35s`.
     Concurrent c4 pgbench plus remote RDMA basebackup completed correctly with
     pgbench around `9.36k TPS` and basebackup `4.45s`.
   - Cold c4 setup can still time out if clients abandon session-open requests
     while the peer path is still being established. The proper correctness fix
     is still the multi-slot peer-control op state machine, not a larger timeout.

   May 30 implementation checkpoint and cleanup status:

   - The peer-control request/response correlation state has landed.
     [`TupleSinkServiceReservePeerControlOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4058)
     and
     [`TupleSinkServiceCompletePeerControlOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4122)
     replaced the earlier connection-wide "currently waiting for response" shape
     with explicit op-index/generation/sequence matching.
   - The public async API also exists now:
     [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6456)
     publishes one request and returns a
     [`TupleSinkServicePeerControlAsyncOp`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:121),
     and
     [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6623)
     progresses that explicit op until it completes or fails. The old
     [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6731)
     is now a compatibility wrapper that starts an async op and waits on it.
   - Control publication is no longer locally blocking on the normal
     peer-control path.
     [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3786)
     writes the peer control slot and publishes the tail with a signaled
     `WRITE_WITH_IMM`, but it does not wait inline for the send CQE. Request
     publishes borrow op-owned registered source storage; response publishes
     borrow preallocated lane-owned response publish slots.
     [`TupleSinkServiceDrainTaggedSendCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1497)
     retires those sources later.
   - Tagged control WR ids carry op generation. This is required because a
     blocking compatibility adapter can time out or release an op before the
     local send CQE is drained; stale CQEs are ignored instead of mutating a
     later op that reused the slot. The hot path does not allocate heap state for
     this; request buffers, response publish slots, and op states are fixed
     lane-local arrays.
   - [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6919)
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

   - [`TupleSinkServicePeerControlAsyncOp`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:121)
     now owns the peer endpoint, request copy, traffic class, connection handle,
     and `requestPublished` flag. This lets `StartPeerRequest...` return with a
     request still waiting on RDMA-CM/bootstrap setup instead of requiring the
     peer-control message to have already been written.
   - Outgoing connect setup was split into
     [`TupleSinkServiceStartOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2918)
     plus
     [`TupleSinkServiceProgressOutgoingPeerConnectionSetup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3051).
     The phases now poll `ADDR_RESOLVED`, `ROUTE_RESOLVED`, `ESTABLISHED`, and
     bootstrap send/recv CQEs without sleeping inside the progress helper.
   - Incoming accept setup was split into
     [`TupleSinkServiceStartIncomingPeerConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3296)
     plus
     [`TupleSinkServiceProgressIncomingPeerConnectionSetup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3065).
     The passive side no longer treats a listener `CONNECT_REQUEST` as "accept
     and finish bootstrap right now." A subtle correctness fix was needed here:
     [`TupleSinkServiceApplyPeerBootstrapMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2519)
     sets `bootstrapComplete`, but the passive-side async path clears it until
     its own bootstrap response send retires and
     [`TupleSinkServiceFinishPeerConnectionSetup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2744)
     posts notification receives.
   - Peer-control async start/poll now defers publication until the lane is
     ready through
     [`TupleSinkServiceStartOrProgressOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6817),
     [`TupleSinkServiceTryPublishPeerControlAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6911),
     [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7023),
     and
     [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7062).
   - [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7370)
     now progresses active but not-yet-bootstrapped incoming/outgoing lane slots
     before placing ready lanes into the fixed-priority service-loop lists at
     [`remote_execution_peer_transport_rdma.c:7422`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7422)
     and
     [`remote_execution_peer_transport_rdma.c:7465`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7465).
   - Validation after rebuild/install/sync on farnet1 and farnet0:
     remote c1 pgbench warmed at `4830.79 TPS`, p50 `0.204 ms`, p99
     `0.227 ms`; remote c4 pgbench warmed at `11087.77 TPS`, p50 `0.348 ms`,
     p99 `0.613 ms`; warmed remote RDMA basebackup completed in `4.26s`; one
     concurrent remote c4 pgbench plus remote RDMA basebackup run completed with
     both rc=0, pgbench `9454.57 TPS`, p50 `0.394 ms`, p99 `0.790 ms`, and
     basebackup `4.65s` (`/tmp/homer_async_impl_20260530_194920`).
   - Historical remaining cleanup at this checkpoint:
     [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7185)
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
      [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13791)
      must never wait for an RDMA-CM event, send CQE, peer response, remote
      connection establishment, or remote sink/session setup. A pump pass may
      poll nonblocking readiness, post bounded work, record an in-flight op, and
      return. Synchronous compatibility wrappers may remain only for cold tools
      or tests outside the service loop.

   2. **Convert outgoing RDMA-CM connect into lane phases.** The blocking path is
      currently
      [`TupleSinkServiceConnectOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2780):
      it posts `rdma_resolve_addr()` and waits for `ADDR_RESOLVED` at
      [`remote_execution_peer_transport_rdma.c:2836`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2836),
      waits for `ROUTE_RESOLVED` at
      [`remote_execution_peer_transport_rdma.c:2857`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2857),
      then waits for `ESTABLISHED` at
      [`remote_execution_peer_transport_rdma.c:2889`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2889).
      Replace that with `StartOutgoingPeerConnection(...)` and
      `ProgressPeerLaneConnect(...)` style helpers. The lane should carry a
      phase enum such as `IDLE`, `ADDR_RESOLVE_POSTED`, `ADDR_RESOLVED`,
      `ROUTE_RESOLVE_POSTED`, `ROUTE_RESOLVED`, `CONNECT_POSTED`,
      `BOOTSTRAP_POSTED`, `READY`, `FAILED`, plus the small amount of
      preallocated storage needed by the current phase.

   3. **Change ensure-connection callers from "ready or fail" to "pending or
      ready".** [`TupleSinkServiceEnsureOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3145)
      and
      [`TupleSinkServiceEnsureOutgoingPeerConnectionHandleRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3189)
      currently return only after the connection is ready. Add an async return
      shape such as `READY`, `IN_PROGRESS`, `FAILED`. If a peer-control op or
      payload open needs a lane that is still connecting, bind that waiting work
      to the lane's pending list and let the service loop resume it after
      `READY`.

   4. **Convert incoming accept/bootstrap into bounded lane progress.** The
      listener pump in
      [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6919)
      already polls the listener nonblocking, but
      [`TupleSinkServiceHandleIncomingConnectRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7033)
      still needs to be treated as a state transition, not a "finish everything
      now" helper. Accepted connections should enter incoming-lane phases:
      allocate slot, init resources, post bootstrap recv, accept, wait for
      `ESTABLISHED`, exchange mailbox descriptors, then mark `READY`. Each phase
      should advance only when its nonblocking event/CQE is available.

   5. **Move peer request/response waits out of call stacks.** The async
      substrate is present:
      [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6456)
      and
      [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6623)
      operate on explicit op handles. The remaining blocker is
      [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6731),
      which starts an op and spins on it. Migrate service-loop callers to store
      `TupleSinkServicePeerControlAsyncOp` in the owning session/stream/control
      slot and return `IN_PROGRESS` instead of waiting on stack-local response
      state.

   6. **Add continuations for command-session open.** The remote client-SQL open
      path blocks today in
      [`TupleSinkServiceHandleOpenCommandSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11919)
      when it calls
      [`TupleSinkServicePeerOpenCommandSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4591)
      at
      [`tuple_sink_service_process.c:11866`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11866).
      Add a pending-open state under the local control slot or session. The
      handler should create the local session, register command/completion MRs
      as needed, start connection/open ops, return a pending heartbeat/accepted
      state to the control slot, and publish the final local open response only
      after the peer command session is open. If the frontend abandons the slot,
      cancellation should mark the pending op canceled and reclaim any created
      session/backend resources.

   7. **Add continuations for peer sink/result open.** Remote result and payload
      setup block today in
      [`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8030)
      and at call sites such as
      [`tuple_sink_service_process.c:12382`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12382)
      and
      [`tuple_sink_service_process.c:12418`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12418).
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
        [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13811)
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
     [`TupleSinkServiceEnsureOutgoingPeerConnectionHandleRdmaAsync()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6916).
     It progresses the outgoing lane setup once and returns `readyOut` instead
     of waiting until RDMA-CM/bootstrap is finished. The old
     [`TupleSinkServiceEnsureOutgoingPeerConnectionHandleRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3533)
     remains for compatibility callers outside the newly converted local-control
     path.
   - Remote `OPEN_SESSION` requests now use a fixed-size per-control-slot
     continuation table:
     [`LocalControlAsyncOps`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12576).
     The selector
     [`TupleSinkServiceOpenRequestNeedsAsyncLocalControl()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12582)
     diverts remote command-session opens and remote send-side payload/result
     stream opens away from the blocking handler.
   - Command-session opens progress through
     [`TupleSinkServiceProgressCommandOpenAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12776):
     create the local session, asynchronously ensure the critical-control lane
     when client-SQL mailbox registration needs a PD/QP, register the command
     and completion mailboxes, start the peer open through the async peer-control
     op, then publish the local response only after the peer response arrives.
   - Remote payload/result stream opens progress through
     [`TupleSinkServiceProgressStreamOpenAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13427):
     validate and create the local stream, asynchronously ensure the stream's
     traffic-class lane, register the sender-head mirror, start the peer open,
     bind peer descriptors, and finally publish the original local control
     response.
   - Old backend/backend remote `START_COMMAND` and
     `POLL_COMMAND_COMPLETION` local-control requests now also use the same
     continuation table when the session has a peer command endpoint. The
     selectors
     [`TupleSinkServiceStartCommandNeedsAsyncLocalControl()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12627)
     and
     [`TupleSinkServicePollCompletionNeedsAsyncLocalControl()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12648)
     divert those requests before the compatibility handlers run; the async
     phases are driven by
     [`TupleSinkServiceProgressLocalControlAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13544).
   - [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15204)
     now pumps pending local-control continuations before scanning new slots.
     A pending slot remains in `REQUEST_READY`; the service keeps the heartbeat
     moving and skips reprocessing until the continuation publishes
     `RESPONSE_READY`.
   - Performance cleanup: the pending-op table is guarded by
     [`LocalControlAsyncActiveCount`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12578),
     so warmed pgbench/basebackup loops do not scan the setup-only continuation
     array when no async local-control work is active. A follow-up hot-path
     cleanup removed the unconditional `memset()` of the large fixed-width
     peer-control message in
     [`TupleSinkServiceProgressPeerControlResponses()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6757);
     the mailbox-empty path no longer clears the embedded request/response
     union before it knows a response is present.
   - May 31 follow-up: the peer sink close exception has been removed. The
     earlier fire-and-forget close experiment was semantically unsafe because
     terminal command completion could become visible before result-stream EOS.
     The implemented fix is to make EOS an in-band tuple payload fact:
     [`MarkCitusTupleSinkPeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1801)
     marks the last tuple-view record or emits a zero-row EOS record, and
     [`HomerServicePayloadStreamResultEosPosted()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3568)
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
     [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11604),
     [`TupleSinkServiceHandleStartCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14054),
     and
     [`TupleSinkServiceHandlePollCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14213).
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
   - [`HomerPayloadProgressDelta`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:858)
     is a stack-local result object carrying `madeProgress`, future blocked/
     readiness fields, CQ/WR/byte counters, and the four frontiers:
     `transportCompletionFrontier`, `sourceReleaseFrontier`,
     `semanticObjectFrontier`, and `remoteCreditFrontier`
   - [`HomerServicePayloadProgressDeltaMade()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2235)
     derives the old `bool madeProgress` return shape from typed progress so the
     outer loop remains unchanged while callers stop treating all progress as the
     same event
   - byte-ring send completion now flows through
     [`HomerServiceApplyTrackedPayloadCompletionFrontier()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2530),
     which uses
     [`HomerServiceCompleteTrackedPayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2429)
     to retire transport resources independently from source-byte release and
     semantic-object completion
   - the fixed-slot sender path uses
     [`HomerServiceApplySequencePayloadCompletionFrontier()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2601);
     for that older layout the three frontiers still collapse to the same
     sequence, but the executor vocabulary now matches the byte-ring path
   - receiver consumed-head ACK CQ drain now reports transport-completion
     progress through
     [`HomerServiceDrainReceiverHeadAckCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11423)
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
     [`HomerServicePayloadProgressDeltaMade()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2235)
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
   - first scheduler-facing result slice landed on `homer-scheduler-ready-milestone`
     on June 2, 2026. It adds the source/result vocabulary in
     [`HomerProgressSourceKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1076),
     [`HomerProgressSourceCore`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1116),
     [`HomerProgressSourceFeedback`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1135),
     [`HomerProgressGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1155),
     [`HomerProgressPlan`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1165),
     and
     [`HomerProgressResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1178).
     This slice is intentionally behavior-preserving: it does not change the
     current fixed-priority service order, source readiness derivation, egress
     grant selection, or CQ ownership model yet.
   - [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12899)
     now reports progress through stack-local `HomerProgressResult` while
     returning success/failure as the scheduler-facing executor shape. The old
     payload-only delta is bridged through
     [`HomerProgressResultMergePayloadDelta()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2453)
     so the first slice reuses the landed frontier-aware payload accounting
     without changing RDMA posting behavior.
   - second scheduler-ready slice landed after the first validation: payload
     stream work first went through a bounded fixed-priority ready/grant plan.
     That early aggregate helper has since been removed by the twenty-first
     slice below. The current code derives stream-level payload readiness through
     [`HomerServiceBuildPayloadReadySources()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3143)
     and then executes each selected stream through
     [`HomerServiceExecutePayloadProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3698).
     The important invariant from the second slice remains: payload executors
     consume stack-local grants with already-resolved owner pointers and preserve
     RDMA posting behavior.
   - Optimization pass after the second slice: the payload source-ref helper
     [`HomerPayloadStreamProgressSourceRef()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2553)
     now uses explicit field stores rather than `memset` per active stream, and
     the tiny plan helper routines are `static inline`.
   - third scheduler-ready slice: remote client SQL command forwarding now also
     enters through the grant/result interface. The current command pump remains
     a single coarse foreground command-ring source because
     [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5985)
     intentionally performs both command-write CQE retirement and per-session
     command forwarding. [`HomerServiceBuildRemoteClientSqlCommandProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2711)
     builds one fixed-priority command grant when the session table has an active
     scan window, and
     [`HomerServiceExecuteRemoteClientSqlCommandProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2743)
     executes the existing command pump while reporting progress through
     `HomerProgressResult`. The later scheduler split should separate lane-CQ
     drain from per-session command-ring readiness, but this slice avoids changing
     the validated RDMA command publication path.
   - fourth scheduler-ready slice: the shared service-loop primitive now consumes
     `HomerProgressResult` directly. [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16698)
     returns success/failure and writes progress into the caller-provided result;
     this transitional slice folded direct bool-returning sub-pumps through a
     bool-to-result wrapper at one boundary. Later scheduler-ready slices removed
     that wrapper as the underlying pumps started reporting typed
     `HomerProgressResult` facts directly. The main loop derives its idle
     decision from
     `HomerProgressResultMade()` at
     [`tuple_sink_service_process.c:16942`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16942),
     and the startup wait loops do the same at
     [`tuple_sink_service_process.c:6426`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6426)
     and
     [`tuple_sink_service_process.c:6571`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6571).
   - fifth scheduler-ready slice: fixed source registration now exists for the
     sources that have entered the grant path. [`HomerServiceProgressRegistry`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1202)
     owns stable `HomerProgressSourceCore` / `HomerProgressSourceFeedback` slots
     for the coarse remote client SQL command source and every payload stream
     table slot. [`HomerServiceInitializeProgressRegistry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2856)
     initializes those refs once against the fixed `sessionStates` and
     `streamEntries` arrays at service startup
     ([`tuple_sink_service_process.c:16973`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16973)).
     The hot builders copy source refs from this registry through
     [`HomerPayloadStreamProgressSourceRef()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2590)
     and
     [`HomerServiceBuildRemoteClientSqlCommandProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2749).
     Per-plan payload generation still comes from the current `serviceStreamId`
     so stale-source validation can be added later without mutating the fixed
     registry on every service pass.
   - sixth scheduler-ready slice: backend completion mailbox scanning now has its
     own coarse `COMPLETION_RING` source in the same registry. The registry source
     is initialized at
     [`tuple_sink_service_process.c:2973`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2973).
     [`HomerServiceBuildCompletionProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2846)
     emits one fixed-priority completion grant, and
     [`HomerServiceExecuteCompletionProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2885)
     still calls the existing
     [`TupleSinkServiceConsumeAllCompletionMailboxes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5891)
     scan. The branch in
     [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16991)
     is therefore scheduler-shaped without changing completion mailbox ownership
     or per-session filtering.
   - seventh scheduler-ready slice: local control and peer control now also enter
     through coarse fixed-priority grants. The registry owns
     `localControlSource` and `peerControlSource` at
     [`tuple_sink_service_process.c:1207`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1207)
     and initializes them after `controlState` and `peerRequestContext` are ready
     at
     [`tuple_sink_service_process.c:17347`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17347).
     [`HomerServiceBuildLocalControlProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2953)
     /
     [`HomerServiceExecuteLocalControlProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2992)
     wrap the existing local control slot pump, and
     [`HomerServiceBuildPeerControlProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3039)
     /
     [`HomerServiceExecutePeerControlProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3079)
     wrap the existing peer connection/control pump. The corresponding
     [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17156)
     branches are now scheduler-shaped without changing local-control slot
     semantics or peer-control async state-machine behavior.
     Follow-up optimization removed redundant stack-local `HomerProgressResult`
     clears from the coarse command/completion/control executors; these wrappers
     now return the underlying pump's boolean directly after grant validation
     (for example
     [`HomerServiceExecuteRemoteClientSqlCommandProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2825),
     [`HomerServiceExecuteCompletionProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2905),
     [`HomerServiceExecuteLocalControlProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2983),
     and
     [`HomerServiceExecutePeerControlProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3065)).
   - The compatibility-close drain now folds payload-stream results into a local
     pass result at
     [`TupleSinkServiceFlushIdleSessionSinksForCompatibilityClose()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15234)
     and uses `HomerProgressResultMade()` for its bounded retry decision. This
     remains a local close-path helper, not the steady-state service-loop
     scheduler boundary.
   - Validation after `make -j8 service-bin client-bin`, `git diff --check`,
     install/sync/restart on farnet1/farnet0:
     warmed remote RDMA c1 pgbench `4612.47 TPS`, average `0.217 ms`, p50
     `0.214 ms`, p95 `0.225 ms`, p99 `0.237 ms`, zero failed transactions;
     warmed remote RDMA c4 pgbench `10942.34 TPS`, average `0.366 ms`, p50
     `0.349 ms`, p95 `0.510 ms`, p99 `0.609 ms`, zero failed transactions;
     warmed remote RDMA basebackup `4.36s` and `4.19s`; concurrent remote RDMA
     c4 pgbench plus remote RDMA basebackup completed with both rc=0, pgbench
     `9441.20 TPS`, average `0.424 ms`, and basebackup `4.73s`.
   - Validation after the payload ready/grant-plan slice and optimization pass:
     `make -j8 service-bin client-bin`, `git diff --check`,
     `make install-service-bin`, sync/restart on farnet1/farnet0; warmed remote
     RDMA c1 pgbench `4674.43 TPS`, average `0.214 ms`, p50 `0.211 ms`, p95
     `0.222 ms`, p99 `0.235 ms`, zero failed transactions; warmed remote RDMA
     c4 pgbench `10971.35 TPS`, average `0.365 ms`, p50 `0.349 ms`, p95
     `0.509 ms`, p99 `0.610 ms`, zero failed transactions; warmed remote RDMA
     basebackup `4.30s`. An earlier pre-optimization run of the same slice also
     completed concurrent remote c4 pgbench plus remote RDMA basebackup with both
     rc=0, pgbench `9551.39 TPS`, p50 `0.396 ms`, p99 `0.718 ms`, and
     basebackup `4.51s`.
   - Validation after the command-grant wrapper slice: `make -j8 service-bin
     client-bin`, `git diff --check`, `make install-service-bin`, sync/restart on
     farnet1/farnet0; warmed remote RDMA c1 pgbench `4673.28 TPS`, average
     `0.214 ms`, p50 `0.211 ms`, p95 `0.222 ms`, p99 `0.233 ms`, zero failed
     transactions; warmed remote RDMA c4 pgbench `11125.55 TPS`, average
     `0.360 ms`, p50 `0.345 ms`, p95 `0.499 ms`, p99 `0.588 ms`, zero failed
     transactions; warmed remote RDMA basebackup guardrail `4.40s`.
   - Validation after the direct-result `TupleSinkServicePumpOnce()` slice:
     `make -j8 service-bin client-bin`, `git diff --check`,
     `make install-service-bin`, sync/restart on farnet1/farnet0; warmed remote
     RDMA c1 pgbench `4641.75 TPS`, average `0.215 ms`, p50 `0.213 ms`, p95
     `0.224 ms`, p99 `0.235 ms`, zero failed transactions; warmed remote RDMA
     c4 pgbench `11039.11 TPS`, average `0.362 ms`, p50 `0.347 ms`, p95
     `0.505 ms`, p99 `0.595 ms`, zero failed transactions; warmed remote RDMA
     basebackup guardrail `4.40s`.
   - Validation after fixed source-registry registration: `make -j8 service-bin
     client-bin`, `git diff --check`, `make install-service-bin`, sync/restart on
     farnet1/farnet0; warmed remote RDMA c1 pgbench `4677.90 TPS`, average
     `0.214 ms`, p50 `0.211 ms`, p95 `0.222 ms`, p99 `0.233 ms`, zero failed
     transactions; warmed remote RDMA c4 pgbench `11090.46 TPS`, average
     `0.361 ms`, p50 `0.347 ms`, p95 `0.506 ms`, p99 `0.599 ms`, zero failed
     transactions; warmed remote RDMA basebackup guardrail `4.39s`.
   - Validation after the coarse completion-ring grant slice: `make -j8
     service-bin client-bin`, `git diff --check`, `make install-service-bin`,
     sync/restart on farnet1/farnet0; warmed remote RDMA c1 pgbench
     `4670.92 TPS`, average `0.214 ms`, p50 `0.211 ms`, p95 `0.222 ms`, p99
     `0.233 ms`, zero failed transactions; warmed remote RDMA c4 pgbench
     `11056.88 TPS`, average `0.362 ms`, p50 `0.347 ms`, p95 `0.506 ms`, p99
     `0.600 ms`, zero failed transactions; warmed remote RDMA basebackup
     guardrail `4.39s`.
   - Validation after the local/peer control grant wrapper slice: `make -j8
     service-bin client-bin`, `git diff --check`, `make install-service-bin`,
     sync/restart on farnet1/farnet0; warmed remote RDMA c1 pgbench
     `4661.52 TPS`, average `0.215 ms`, p50 `0.212 ms`, p95 `0.223 ms`, p99
     `0.235 ms`, zero failed transactions; warmed remote RDMA c4 pgbench
     `11101.70 TPS`, average `0.360 ms`, p50 `0.344 ms`, p95 `0.505 ms`, p99
     `0.606 ms`, zero failed transactions; warmed remote RDMA basebackup
     guardrail `4.40s`.
   - Validation after removing redundant coarse-executor result clears: `make -j8
     service-bin client-bin`, `git diff --check`, `make install-service-bin`,
     sync/restart on farnet1/farnet0; warmed remote RDMA c1 pgbench
     `4669.66 TPS`, average `0.214 ms`, p50 `0.211 ms`, p95 `0.223 ms`, p99
     `0.235 ms`, zero failed transactions; warmed remote RDMA c4 pgbench
     `11041.08 TPS`, average `0.362 ms`, p50 `0.346 ms`, p95 `0.507 ms`, p99
     `0.611 ms`, zero failed transactions; warmed remote RDMA basebackup
     guardrail `4.42s`.
   - Eighth scheduler-ready slice: lane send-CQ drain is now exposed as a
     scheduler progress source. The RDMA layer exports
     [`TupleSinkServiceDrainPeerTaggedSendCompletionsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1682)
     and supports direct command-CQE callbacks while preserving the old stash
     behavior for callers that still poll by owner. The service side builds and
     executes the coarse CQ-drain grant with
     [`HomerServiceBuildCqDrainProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2691)
     and
     [`HomerServiceExecuteCqDrainProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2911),
     then runs that grant before command/control/payload owners in
     [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17220).
     Command CQEs are retired directly via
     [`HomerServiceHandleCommandSendCqeFromDrain()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2734),
     so the command owner normally sees freed source-slot credit before it scans
     for more frontend commands.
   - Validation after the CQ-drain progress-source slice: `make -j8 service-bin
     client-bin`, `git diff --check`, `make install-service-bin`, sync/restart on
     farnet1/farnet0; warmup c1 showed the expected cold outliers but completed
     `1000/1000` with zero failures; warmed remote RDMA c1 pgbench completed
     `20000/20000` with zero failures at `4679.26 TPS`, average `0.214 ms`, p50
     `0.211 ms`, p95 `0.223 ms`, p99 `0.233 ms`; warmed remote RDMA c4 pgbench
     completed `40000/40000` with zero failures at `11078.26 TPS`, average
     `0.361 ms`, p50 `0.345 ms`, p95 `0.502 ms`, p99 `0.598 ms`; remote RDMA
     basebackup run 1 was `5.57s` and warmed run 2 was `4.39s`; concurrent
     remote c4 pgbench plus remote RDMA basebackup completed with both rc=0,
     pgbench `9236.55 TPS`, p50 `0.401 ms`, p99 `0.769 ms`, and basebackup
     `4.46s`.
   - Ninth scheduler-ready slice: payload send-CQEs now carry a typed payload
     completion kind, so the lane-owned CQ drain can retire payload-data
     completions and reverse-direction receiver-head ACK completions directly
     instead of forcing the normal path through the owner-polled stash. The RDMA
     WR id layout reserves one high bit for
     [`TupleSinkServicePeerPayloadCompletionKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:148)
     and encodes it in
     [`TupleSinkServiceEncodePayloadWrId()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1059).
     [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1375)
     dispatches payload CQEs through the typed callback when the CQ drain owns
     the poll, while
     [`TupleSinkServicePollPeerReceiverHeadAckCompletionRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6451)
     preserves the older owner-poll fallback for paths that have not yet been
     folded into the scheduler source. On the service side,
     [`HomerServiceHandlePayloadSendCqeFromDrain()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2760)
     routes `DATA` to the byte-ring or sequence payload completion frontier and
     routes `RECEIVER_HEAD_ACK` to
     [`HomerServiceApplyReceiverHeadAckCompletionFrontier()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12475).
     The remaining optimization caveat is that the direct payload callback still
     resolves `serviceSinkId` with the bounded
     [`HomerServiceFindPayloadStreamById()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8750)
     scan over at most `CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS` entries. This
     did not regress the current workloads, but the full scheduler-ready source
     registry should eventually provide direct source/stream pointers or compact
     source indices so per-CQE dispatch does not need an owner lookup.
   - Validation after the typed payload-CQE drain slice: `make -j8 service-bin
     client-bin`, `git diff --check`, `sudo -n make install-service-bin`,
     runtime artifact sync, and service restart on farnet1/farnet0 all
     succeeded. A first remote RDMA c1 pgbench run completed correctly but had a
     cold in-run outlier, so the warmed comparison uses the rerun: c1 completed
     `20000/20000` with zero failures at `4718.12 TPS`, average `0.212 ms`, p50
     `0.209 ms`, p95 `0.220 ms`, p99 `0.232 ms`; c4 completed `40000/40000`
     with zero failures at `11125.65 TPS`, average `0.360 ms`, p50 `0.343 ms`,
     p95 `0.499 ms`, p99 `0.598 ms`; remote RDMA basebackup run 1 was `5.77s`
     and warmed run 2 was `4.26s`; concurrent remote c4 pgbench plus remote RDMA
     basebackup completed with both rc=0, pgbench `9377.97 TPS`, p50 `0.386 ms`,
     p99 `0.736 ms`, and basebackup `4.47s`.
   - Tenth scheduler-ready slice: the coarse grant executors now use the planned
     scheduler result boundary. Instead of returning `bool madeProgress` to be
     re-wrapped by the service loop, the executors return success/failure and
     update the caller-provided `HomerProgressResult` directly. This landed for
     payload, CQ drain, command-ring, completion-ring, local-control, and
     peer-control grant executors:
     [`HomerServiceExecutePayloadProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2631),
     [`HomerServiceExecuteCqDrainProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2919),
     [`HomerServiceExecuteRemoteClientSqlCommandProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3041),
     [`HomerServiceExecuteCompletionProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3113),
     [`HomerServiceExecuteLocalControlProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3181),
     and
     [`HomerServiceExecutePeerControlProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3251).
     [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17257)
     now consumes those result-oriented executors directly. The specialized
     command/control/payload pumps underneath still retain their existing
     behavior and mostly still report progress as booleans; the cleanup here is
     specifically the scheduler boundary, not a full rewrite of every underlying
     pump.
   - Validation after the result-oriented executor-boundary slice: `make -j8
     service-bin client-bin`, `git diff --check`, `sudo -n make
     install-service-bin`, runtime artifact sync, and service restart on
     farnet1/farnet0 succeeded. The first c1 run after restart again had a cold
     in-run outlier, so the warmed rerun is the comparison point: c1 completed
     `20000/20000` with zero failures at `4669.36 TPS`, average `0.214 ms`, p50
     `0.211 ms`, p95 `0.222 ms`, p99 `0.232 ms`; c4 completed `40000/40000`
     with zero failures at `11049.10 TPS`, average `0.362 ms`, p50 `0.346 ms`,
     p95 `0.500 ms`, p99 `0.597 ms`; remote RDMA basebackup run 1 was `6.31s`
     and warmed run 2 was `4.31s`; concurrent remote c4 pgbench plus remote RDMA
     basebackup completed with both rc=0, pgbench `9449.99 TPS`, p50 `0.384 ms`,
     p99 `0.728 ms`, and basebackup `4.49s`.
   - Eleventh scheduler-ready slice: executor feedback is now written into the
     fixed source registry after every bounded grant. The feedback struct at
     [`HomerProgressSourceFeedback`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1130)
     now records last-grant empty polls, CQEs drained, items processed, WRs
     posted, bytes posted, ready object/byte hints, outstanding WR count, and the
     four frontier classes. [`HomerProgressResultMergeResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2518)
     folds per-grant results into the loop aggregate, while
     [`HomerServiceUpdateProgressFeedback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2667)
     updates the fixed source core/feedback pair and
     [`HomerServiceFinishProgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2791)
     performs both operations at the executor boundary. The progress registry now
     also owns a CQ-drain source/feedback slot
     ([`cqDrainSource`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1215)),
     so CQ drain no longer remains a special unregistered progress source.
   - This slice still preserves fixed-priority scheduling. The new facts are
     intentionally consumed only by future ready-set/policy code; they do not yet
     decide which source wins service. Payload source hints are derived cheaply
     from the already-owned source frontiers: byte-ring streams use producer
     `publishedTail - payloadSourceBytePostedTail`, fixed-slot streams use
     `publishedTail - payloadSenderPostedTail`, payload blocked flags distinguish
     remote-credit pressure, local completion/source-release pressure, and ready
     work, and CQ-drain outstanding WR count uses
     `ClientSqlCommandWriteSignaledCompletionActiveCount`. The code avoids heap
     event records and hash lookups; feedback updates are scalar writes into fixed
     arrays.
   - Validation after the executor-feedback/facts slice: `make -j8 service-bin
     client-bin`, `git diff --check`, `sudo -n make install-service-bin`, runtime
     artifact sync, and service restart on farnet1/farnet0 succeeded. The first
     c1 run after restart again had a cold in-run outlier, so the warmed rerun is
     the comparison point: c1 completed `20000/20000` with zero failures at
     `4658.10 TPS`, average `0.215 ms`, p50 `0.212 ms`, p95 `0.223 ms`, p99
     `0.235 ms`; c4 completed `40000/40000` with zero failures at
     `11134.54 TPS`, average `0.359 ms`, p50 `0.343 ms`, p95 `0.497 ms`, p99
     `0.591 ms`; remote RDMA basebackup run 1 was `6.40s` and warmed run 2 was
     `4.32s`; concurrent remote c4 pgbench plus remote RDMA basebackup completed
     with both rc=0, pgbench `9308.33 TPS`, p50 `0.391 ms`, p99 `0.787 ms`, and
     basebackup `4.51s`.
   - Twelfth scheduler-ready slice: the service loop now builds a coarse
     fixed-size ready set before executing the current fixed-priority order.
     [`HomerProgressReadySet`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1169)
     carries an ordered source array for the future policy interface plus a
     cache-local kind mask for the current membership tests. The ready set is
     built by
     [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2738)
     from the same cheap facts already used by the existing loop: command-write
     and payload send-CQ activity for `CQ_DRAIN`, active session scan windows
     for command/completion rings, local/peer control context availability, and
     active payload stream scan windows. The ready-set mask avoids doing any
     source-plan copy work when no source is ready, while the ordered array is
     preserved for policy work. [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17961)
     consumes that ready set to gate the same fixed-priority branches, so this
     slice changes loop structure and readiness vocabulary without changing the
     scheduling policy yet.
   - Validation after the coarse ready-set slice and the kind-mask optimization:
     `make -j8 service-bin client-bin`, `git diff --check`, `sudo -n make
     install-service-bin`, runtime artifact sync, and service restart on
     farnet1/farnet0 succeeded. The first c1 run after restart again had the
     expected cold in-run outlier, so the warmed rerun is the comparison point:
     c1 completed `20000/20000` with zero failures at `4776.11 TPS`, average
     `0.209 ms`, p50 `0.207 ms`, p95 `0.218 ms`, p99 `0.230 ms`; c4 completed
     `40000/40000` with zero failures at `11130.51 TPS`, average `0.359 ms`,
     p50 `0.347 ms`, p95 `0.503 ms`, p99 `0.604 ms`; remote RDMA basebackup run
     1 was `6.40s` and warmed run 2 was `4.22s`; concurrent remote c4 pgbench
     plus remote RDMA basebackup completed with both rc=0, pgbench `9344.63 TPS`,
     p50 `0.390 ms`, p99 `0.750 ms`, and basebackup `4.54s`
     (`/tmp/homer_concurrent_c4_readyset_mask_1780450651`).
   - Thirteenth scheduler-ready slice: the service loop now consumes a
     policy-produced ordered source plan. [`HomerProgressSourcePlan`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1184)
     is the stack-local handoff object from service-progress policy to
     execution. The first policy is intentionally fixed priority:
     [`HomerServiceBuildFixedPriorityProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2693)
     copies the ready sources in the order produced by the coarse ready-set
     builder. [`HomerServiceExecuteProgressSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17805)
     then dispatches by source kind to the existing specialized executors, so the
     loop no longer embeds a hand-written chain of source-specific `if` blocks.
     Target completion and heartbeat are now represented as service-progress
     source kinds too, which keeps the whole pump pass inside the same
     ready-set/source-plan structure. This is still not a real reordering
     scheduler; it is the scheduler-integrated loop substrate that later
     policies will replace.
   - Validation after the fixed-priority source-plan loop slice: `make -j8
     service-bin client-bin`, `git diff --check`, `sudo -n make
     install-service-bin`, runtime artifact sync, and service restart on
     farnet1/farnet0 succeeded. The first c1 run after restart again had the
     expected cold in-run outlier, so the warmed rerun is the comparison point:
     c1 completed `20000/20000` with zero failures at `4735.27 TPS`, average
     `0.211 ms`, p50 `0.209 ms`, p95 `0.219 ms`, p99 `0.231 ms`; c4 completed
     `40000/40000` with zero failures at `11075.37 TPS`, average `0.361 ms`,
     p50 `0.349 ms`, p95 `0.507 ms`, p99 `0.602 ms`; remote RDMA basebackup run
     1 was `6.44s` and warmed run 2 was `4.36s`; concurrent remote c4 pgbench
     plus remote RDMA basebackup completed with both rc=0, pgbench `9309.40 TPS`,
     p50 `0.390 ms`, p99 `0.762 ms`, and basebackup `4.52s`
     (`/tmp/homer_concurrent_c4_sourceplan_1780451075`).
   - Fourteenth scheduler-ready slice: source-plan construction now goes through
     an explicit service-progress policy interface. [`HomerProgressPolicyKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1190)
     and [`HomerProgressPolicy`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1200)
     define the policy object, and the current global policy at
     [`ProgressPolicy`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1261)
     is fixed priority. [`HomerServiceBuildProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2740)
     is the single policy entrypoint; it currently dispatches only to
     [`HomerServiceBuildFixedPriorityProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2711).
     [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17997)
     now calls the policy entrypoint rather than the fixed-priority builder
     directly. This preserves behavior while making policy selection an explicit
     replaceable layer.
   - Validation after the service-progress policy-interface slice: `make -j8
     service-bin client-bin`, `git diff --check`, `sudo -n make
     install-service-bin`, runtime artifact sync, and service restart on
     farnet1/farnet0 succeeded. The first c1 run after restart again had the
     expected cold in-run outlier, so the warmed rerun is the comparison point:
     c1 completed `20000/20000` with zero failures at `4719.87 TPS`, average
     `0.212 ms`, p50 `0.209 ms`, p95 `0.219 ms`, p99 `0.231 ms`; c4 completed
     `40000/40000` with zero failures at `11068.09 TPS`, average `0.361 ms`,
     p50 `0.350 ms`, p95 `0.504 ms`, p99 `0.592 ms`; remote RDMA basebackup run
     1 was `6.26s` and warmed run 2 was `4.38s`; concurrent remote c4 pgbench
     plus remote RDMA basebackup completed with both rc=0, pgbench `9194.30 TPS`,
     p50 `0.398 ms`, p99 `0.785 ms`, and basebackup `4.58s`
     (`/tmp/homer_concurrent_c4_policy_1780451324`).
   - Fifteenth scheduler-ready slice: payload senders now consume an explicit
     egress-grant shape while preserving the old hard caps. The grant type is
     the existing [`HomerTransportGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:106)
     with WR/object/byte budgets. The first behavior-preserving builder,
     [`HomerServiceBuildDefaultPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2441),
     translates current constants into a grant: byte-ring payload streams retain
     the prior max-inflight object window, while fixed-slot streams retain
     `HOMER_SERVICE_PAYLOAD_SEND_COMPLETION_BATCH`. The limit helpers
     [`HomerServiceTransportGrantMaxWrCount()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1212),
     [`HomerServiceTransportGrantMaxObjectCount()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1225),
     and
     [`HomerServiceTransportGrantMaxBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1238)
     keep zero/unset fields as "use the hard cap" so the current default path
     cannot accidentally produce a zero-sized grant. The byte-ring sender
     [`HomerServicePumpOutgoingByteRingBaseBackup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11610)
     now accepts the grant, and the fixed-slot sender
     [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12384)
     consumes it. Follow-up boundary cleanup moved default grant selection up to
     [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14279)
     so future egress policy can replace one executor-level selection point
     rather than editing the sender. Optimization pass: the executor uses
     [`HomerServicePayloadStreamCanTryOutgoing()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2466)
     to avoid grant-selection work for inactive or incoming-only streams; the
     sender still defensively rechecks its own outgoing predicates. This is the
     payload egress-grant hook, not the final egress scheduler policy.
   - Validation after the payload egress-grant slice and optimization pass:
     `make -j8 service-bin client-bin`, `git diff --check`, `sudo -n make
     install-service-bin`, runtime artifact sync, and service restart on
     farnet1/farnet0 succeeded. The first c1 run after restart again had the
     expected cold in-run outlier, so the warmed rerun is the comparison point:
     c1 completed `20000/20000` with zero failures at `4733.60 TPS`, average
     `0.211 ms`, p50 `0.209 ms`, p95 `0.219 ms`, p99 `0.231 ms`; c4 completed
     `40000/40000` with zero failures at `11055.41 TPS`, average `0.362 ms`,
     p50 `0.349 ms`, p95 `0.504 ms`, p99 `0.601 ms`; remote RDMA basebackup run
     1 was `5.88s` and warmed run 2 was `4.22s`; concurrent remote c4 pgbench
     plus remote RDMA basebackup completed with both rc=0, pgbench `9313.96 TPS`,
     p50 `0.391 ms`, p99 `0.775 ms`, and basebackup `4.43s`
     (`/tmp/homer_concurrent_c4_egress_grant_opt_1780451800`).
   - Validation after moving payload grant selection to the executor boundary:
     `make -j8 service-bin client-bin`, `git diff --check`, `sudo -n make
     install-service-bin`, runtime artifact sync, and service restart on
     farnet1/farnet0 succeeded. The first c1 run after restart again had the
     expected cold in-run outlier, so the warmed rerun is the comparison point:
     c1 completed `20000/20000` with zero failures at `4641.43 TPS`, average
     `0.215 ms`, p50 `0.212 ms`, p95 `0.225 ms`, p99 `0.237 ms`; c4 completed
     `40000/40000` with zero failures at `11118.20 TPS`, average `0.360 ms`,
     p50 `0.346 ms`, p95 `0.504 ms`, p99 `0.588 ms`; remote RDMA basebackup run
     1 was `6.61s` and warmed run 2 was `4.32s`; concurrent remote c4 pgbench
     plus remote RDMA basebackup completed with both rc=0, pgbench `9159.38 TPS`,
     p50 `0.397 ms`, p99 `0.804 ms`, and basebackup `4.44s`
     (`/tmp/homer_concurrent_c4_egress_grant_boundary_1780452157`). The lower
     warmed c1 value stayed within the recent run band; c4 recovered to the
     prior no-regression band after the outgoing-candidate optimization.
   - Sixteenth scheduler-ready slice: payload egress grant construction now goes
     through an explicit egress policy interface. [`HomerEgressPolicyKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1205)
     and [`HomerEgressPolicy`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1215)
     define the transport-egress policy object, and the current global
     [`EgressPolicy`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1322)
     is fixed/default. [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2486)
     is now the single payload grant-selection entrypoint called from
     [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14351).
     The active policy still returns the behavior-preserving default grant, but
     the scheduler replacement point is now explicit and outside the sender.
   - Correctness fix found during the egress-policy validation: concurrent
     pgbench plus basebackup exposed a tuple-result byte-ring race. A new
     `SINK_READY` completion could arrive while the previous result stream still
     had unretired payload send completions. The old path treated that as an
     error from
     [`HomerServicePrepareTupleResultStreamForCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9983),
     which left the frontend/backend result lifecycle inconsistent and produced
     a frontend `"tuple result byte-ring record header mismatch"`. The fix adds
     [`HomerServiceTupleResultStreamHasPendingSendCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10033)
     and makes
     [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5983)
     defer publishing that command-plane `SINK_READY` event until payload CQ
     drain retires the previous result bytes. This mirrors the existing terminal
     EOS deferral rule and keeps stream frontier reset out of the race window.
   - Validation after the egress-policy interface and tuple-result deferral fix:
     `make -j8 service-bin client-bin`, `git diff --check`, `sudo -n make
     install-service-bin`, runtime artifact sync, and service restart on
     farnet1/farnet0 succeeded. The first concurrent run after restart completed
     correctly but had cold outliers; the warm concurrent rerun completed with
     both rc=0, pgbench `9351.46 TPS`, p50 `0.394 ms`, p99 `0.773 ms`, and
     basebackup `4.66s`
     (`/tmp/homer_concurrent_c4_egress_policy_fix_warm_1780452567`). Standalone
     warmed c1 completed `20000/20000` with zero failures at `4660.80 TPS`,
     average `0.215 ms`, p50 `0.212 ms`, p95 `0.222 ms`, p99 `0.234 ms`;
     standalone c4 completed `40000/40000` with zero failures at `11013.22 TPS`,
     average `0.363 ms`, p50 `0.347 ms`, p95 `0.510 ms`, p99 `0.608 ms`;
     standalone remote RDMA basebackup completed at `4.29s` then `4.43s`.
     Post-fix service log tails showed no repeat of the result-stream reuse or
     pending peer-client completion publish failures.
   - Seventeenth scheduler-ready slice: payload egress policy now receives an
     explicit stack-local ready-facts object before choosing a grant.
     [`HomerPayloadEgressReadyFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1232)
     is the policy-facing handoff for one payload stream: object family,
     traffic class / priority / weight, byte-ring dispatch, and room for ready
     bytes, ready objects, remote credit, and local resource pressure. The helper
     [`HomerServiceBuildPayloadEgressReadyFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2514)
     currently fills only identity/class and cheap local bookkeeping needed by
     the fixed-default policy. It deliberately does not add unused frontier
     atomic loads or object-header parsing on the current baseline path. The
     grant entrypoint
     [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2558)
     now consumes those facts, and
     [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14418)
     builds the facts immediately before selecting the payload egress grant.
     This advances the scheduler interface without changing grant caps; dynamic
     grant sizing over frontier-derived hints remains a separate policy slice.
   - Validation after the payload egress-ready-facts slice: `make -j8
     service-bin client-bin`, `git diff --check`, `sudo -n make
     install-service-bin`, runtime artifact sync, and farnet1/farnet0 service
     restart succeeded. Warmed standalone c1 completed `20000/20000` with zero
     failures at `4742.48 TPS` and average `0.211 ms` after a cold first run
     with `1924 ms` initial connection time. Standalone c4 completed
     `40000/40000` with zero failures at `11021.32 TPS`, average `0.363 ms`,
     p50 `0.347 ms`, p95 `0.508 ms`, p99 `0.600 ms`. Remote RDMA basebackup
     completed at `5.75s` cold and `4.27s` warm. Concurrent remote c4 pgbench
     plus remote RDMA basebackup completed with both rc=0, pgbench
     `9244.94 TPS`, p50 `0.392 ms`, p95 `0.619 ms`, p99 `0.787 ms`, and
     basebackup `4.49s` (`/tmp/homer_concurrent_c4_egress_facts_1780453025`).
     Service log tails showed no result-stream reuse, pending peer-client
     completion publish, byte-ring header mismatch, or egress grant/facts
     failures.
   - Eighteenth scheduler-ready slice: the payload egress-ready facts now take
     the existing source feedback as their first cheap readiness source, rather
     than adding new shared-frontier reads at grant-selection time.
     [`HomerServiceExecutePayloadProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3398)
     passes
     [`HomerProgressSourceFeedback`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1132)
     into
     [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14452),
     which forwards it to
     [`HomerServiceBuildPayloadEgressReadyFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2515).
     The builder copies advisory `readyByteCountHint` and
     `readyObjectCountHint` only when they are nonzero, so a stale or empty
     feedback sample does not erase setup-time scheduling estimates. The sender
     still re-checks authoritative frontiers before posting RDMA work; these
     facts are policy hints, not ownership transfer or correctness state.
   - Validation after the feedback-fed egress-facts slice: `make -j8
     service-bin client-bin`, `git diff --check`, `sudo -n make
     install-service-bin`, runtime artifact sync, and farnet1/farnet0 service
     restart succeeded. Warmed standalone c1 completed `20000/20000` with zero
     failures at `4705.09 TPS`, p50 `0.210 ms`, p95 `0.221 ms`, p99
     `0.232 ms` after a cold first run. Standalone c4 completed `40000/40000`
     with zero failures at `11071.05 TPS`, average `0.361 ms`, p50 `0.350 ms`,
     p95 `0.509 ms`, p99 `0.608 ms`. Remote RDMA basebackup completed at
     `6.32s` cold and `4.19s` warm. Concurrent remote c4 pgbench plus remote
     RDMA basebackup completed with both rc=0, pgbench `9294.29 TPS`, p50
     `0.393 ms`, p95 `0.618 ms`, p99 `0.782 ms`, and basebackup `4.49s`
     (`/tmp/homer_concurrent_c4_egress_feedback_facts_1780453225`). Service log
     tails showed no result-stream reuse, pending peer-client completion publish,
     byte-ring header mismatch, or egress grant/facts failures.
   - Nineteenth scheduler-ready slice: the service-progress policy interface now
     has a real opt-in non-fixed policy. [`HomerProgressPolicyKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1190)
     includes `HOMER_PROGRESS_POLICY_ROUND_ROBIN`, and
     [`HomerProgressPolicy`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1201)
     stores the service-thread-local `roundRobinCursor`. The startup parser
     [`HomerServiceReadProgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2633)
     accepts `HOMER_PROGRESS_POLICY=fixed-priority|round-robin` and is called
     during service startup at
     [`main()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18539).
     The new planner
     [`HomerServiceBuildRoundRobinProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3028)
     rotates the existing stack-local ready array by cursor; it does not allocate
     work items, rescan owners, or change transport-egress byte/object/WR grant
     sizing. The default remains fixed-priority, so standard benchmark runs keep
     the previous baseline ordering unless the environment variable is set.
   - Validation after the round-robin service-progress policy slice: `make -j8
     service-bin client-bin`, `git diff --check`, install/sync, and service
     restarts succeeded. Default fixed-priority mode was revalidated first:
     warmed c1 completed `20000/20000` at `4734.48 TPS`, p50 `0.208 ms`, p99
     `0.232 ms`; c4 completed `40000/40000` at `11173.08 TPS`, p50 `0.347 ms`,
     p99 `0.603 ms`; remote RDMA basebackup completed at `5.70s` cold and
     `4.17s` warm; concurrent c4 pgbench plus basebackup completed with both
     rc=0, pgbench `9304.71 TPS`, p50 `0.394 ms`, p99 `0.763 ms`, and
     basebackup `4.51s`
     (`/tmp/homer_concurrent_c4_progress_default_1780453592`). With
     `HOMER_PROGRESS_POLICY=round-robin` on both farnet services, the first c4
     run after restart had the usual cold outlier (`7186.66 TPS`, max
     `1968.94 ms`), but the warmed c4 repeat returned to `10989.22 TPS`, p50
     `0.348 ms`, p99 `0.606 ms`. Round-robin concurrent c4 pgbench plus
     basebackup completed with both rc=0, pgbench `10388.35 TPS`, p50
     `0.360 ms`, p99 `0.622 ms`, while the basebackup side was cold at `6.02s`;
     warmed round-robin basebackup repeats were `4.50s` and `4.32s`. Service log
     tails showed no known result-stream, header, policy, or egress grant/facts
     failures. The services were restarted back to default fixed-priority after
     the experiment.
   - Twentieth scheduler-ready slice: the payload egress policy interface now
     has a real opt-in dynamic grant-sizing policy. [`HomerEgressPolicyKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1212)
     includes `HOMER_EGRESS_POLICY_READY_HINTS`, and
     [`HomerServiceReadEgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2770)
     accepts `HOMER_EGRESS_POLICY=fixed-default|ready-hints` during service
     startup at
     [`main()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18731).
     The default fixed egress policy does not derive frontier facts. The opt-in
     ready-hints policy asks
     [`HomerServicePopulatePayloadEgressFrontierFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2536)
     to read cheap current queue/byte-ring frontiers into
     [`HomerPayloadEgressReadyFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1240),
     then
     [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2693)
     caps object count, byte count, and WR count from those hints. The byte cap
     is deliberately conservative: the byte-ring sender still posts one whole
     record if the first ready record is larger than `maxBytes`, so the policy
     can limit coalescing without parsing object-family headers.
   - Validation after the ready-hints egress policy slice: `make -j8 service-bin
     client-bin`, `git diff --check`, install/sync, and service restarts
     succeeded. Default fixed egress was revalidated first: the first c4 run
     after restart had the known cold outlier (`7172.85 TPS`, max `1968.55 ms`),
     but the warmed c4 repeat completed `40000/40000` at `11062.90 TPS`, p50
     `0.346 ms`, p99 `0.599 ms`; remote RDMA basebackup completed at `5.90s`
     cold and `4.29s` warm. With `HOMER_EGRESS_POLICY=ready-hints` on both
     farnet services, the first c4 run again had the cold outlier (`7193.97 TPS`,
     max `1968.72 ms`), while the warmed c4 repeat completed at `11056.51 TPS`,
     p50 `0.347 ms`, p99 `0.608 ms`. Ready-hints basebackup completed at
     `6.41s` cold and `4.28s` warm. Ready-hints concurrent c4 pgbench plus
     basebackup completed with both rc=0, pgbench `9294.64 TPS`, p50 `0.395 ms`,
     p99 `0.763 ms`, and basebackup `4.43s`
     (`/tmp/homer_concurrent_c4_egress_ready_hints_1780453968`). Service log
     tails showed no result-stream, header, policy, or egress grant/facts
     failures. The services were restarted back to default fixed policies after
     the experiment.
   - Twenty-first scheduler-ready slice: payload streams are now first-class
     service-progress ready sources instead of one aggregate payload source.
     [`HomerServiceBuildPayloadReadySources()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3143)
     scans the active payload-stream window and appends one
     [`HomerProgressSourceRef`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1103)
     per active stream into the stack-local ready set. The coarse ready-set
     builder now calls that helper from
     [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3405)
     instead of appending `payloadStreamSources[0]` as an opaque aggregate. The
     payload branch in
     [`HomerServiceExecuteProgressSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18524)
     executes the selected source directly through a one-grant stack-local plan
     and no longer rescans every active payload stream. The old aggregate
     `HomerServiceBuildPayloadProgressPlan()` scanner was removed, so standard
     and experimental service-progress policies now see stream-level payload
     sources that can be ordered against other ready work. This does not yet
     implement a global byte/WR budget across sources; it removes the earlier
     structural blocker by making payload streams policy-visible.
   - Validation after the per-stream payload ready-source slice: `make -j8
     service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. Warmed standalone c1 completed `20000/20000`
     with zero failures at `4774.61 TPS`, p50 `0.207 ms`, p99 `0.229 ms` after
     the first cold run. Standalone c4 completed `40000/40000` with zero
     failures at `11089.50 TPS`, p50 `0.348 ms`, p99 `0.600 ms`. Remote RDMA
     basebackup completed at `5.73s` cold and `4.22s` warm. Concurrent c4
     pgbench plus basebackup completed with both rc=0, pgbench `9244.90 TPS`,
     p50 `0.393 ms`, p99 `0.794 ms`, and basebackup `4.62s`
     (`/tmp/homer_concurrent_c4_per_stream_ready_1780454313`). Service log tails
     showed no result-stream, header, selected-payload-source, progress-plan, or
     egress grant/facts failures. Because this slice changes what policies see,
     `HOMER_PROGRESS_POLICY=round-robin` was also revalidated: the first c4 run
     after restart had the known cold outlier (`7153.14 TPS`, max `1969.74 ms`),
     while the warmed repeat returned to `10981.22 TPS`, p50 `0.343 ms`, p99
     `0.602 ms`. The services were restarted back to default fixed policies
     after the experiment.
   - Twenty-second scheduler-ready slice: payload egress now has an opt-in
     service-pass budget shared across scheduler-selected payload streams.
     [`HomerPayloadEgressPassBudget`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1277)
     is a stack-local `TupleSinkServicePumpOnce()` object, initialized once per
     service pass by
     [`HomerServicePayloadEgressPassBudgetBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2629).
     The budgeted policy is selected with
     `HOMER_EGRESS_POLICY=budgeted-ready-hints`; optional caps come from
     `HOMER_EGRESS_PASS_MAX_WRS` and `HOMER_EGRESS_PASS_MAX_BYTES` at service
     startup in
     [`HomerServiceReadEgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2979).
     The payload executor passes the budget through
     [`HomerServiceExecutePayloadProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3903)
     into
     [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14961).
     Outgoing egress grants are capped by
     [`HomerServiceApplyPayloadEgressPassBudget()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2674),
     then actual posted WRs/bytes are charged back with
     [`HomerServicePayloadEgressPassBudgetConsume()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2703).
     This is intentionally not a default-policy behavior change: the normal
     `fixed-default` path still leaves the pass budget inactive. If an opt-in
     pass budget is exhausted, only outgoing payload egress is suppressed for
     the rest of that pass; incoming payload consumption, CQ/ACK cleanup, local
     blackhole progress, and close/reclaim paths still run so a send-side policy
     cap cannot become a receive-side deadlock.
   - Validation after the pass-budget egress slice: `make -j8 service-bin
     client-bin`, `git diff --check`, install/sync, and default service restarts
     succeeded. Default warmed c1 completed `20000/20000` with zero failures at
     `4749.79 TPS`, p50 `0.208 ms`, p99 `0.230 ms` after the first cold run.
     Default c4 completed `40000/40000` with zero failures at `11093.51 TPS`,
     p50 `0.348 ms`, p99 `0.605 ms`. Default remote RDMA basebackup completed
     at `6.09s` cold and `4.35s` warm. Default concurrent c4 pgbench plus
     basebackup completed with both rc=0, pgbench `9183.18 TPS`, p50
     `0.401 ms`, p99 `0.791 ms`, and basebackup `4.61s`
     (`/tmp/homer_concurrent_c4_pass_budget_default_1780454985`). The opt-in
     policy was then validated with
     `HOMER_EGRESS_POLICY=budgeted-ready-hints`,
     `HOMER_EGRESS_PASS_MAX_WRS=64`, and `HOMER_EGRESS_PASS_MAX_BYTES=0` on
     both services: warmed c4 completed `40000/40000` at `10940.75 TPS`, p50
     `0.347 ms`, p99 `0.611 ms`; remote RDMA basebackup completed at `5.93s`
     cold and `4.28s` warm; opt-in concurrent c4 pgbench plus basebackup
     completed with both rc=0, pgbench `9135.78 TPS`, p50 `0.395 ms`, p99
     `0.813 ms`, and basebackup `4.52s`
     (`/tmp/homer_concurrent_c4_pass_budget_optin_1780455064`). The services
     were restarted back to default fixed policies after the experiment, and
     current service logs showed no matching error/failure signatures.
   - Rejected follow-up attempt after the pass-budget slice: splitting the
     remote client SQL command source into one `COMMAND_RING` source per session
     was tried and reverted. The tempting shape mirrored the payload-stream
     split: derive command readiness from each session's command-mailbox
     `publishedEpoch`, `consumedEpoch`, and slot-local `readySeq`, then execute
     only that selected session. It was not correct as a narrow local refactor:
     a c1 pgbench validation run stopped making foreground progress, so the
     change was reverted. The lesson is that per-session command-source
     visibility must be co-designed with command
     source-slot credit and lane-CQ retirement. The aggregate
     [`HOMER_PROGRESS_SOURCE_COMMAND_RING`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1128)
     therefore remains the current implementation, with
     [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7879)
     still owning the command-session scan and fallback command send-CQE
     retirement. A future per-session command-source split should first make
     command send-CQE retirement lane-owned and sufficient for source-slot credit
     under every signaled/unsignaled checkpoint policy, or carry explicit
     per-session command-credit readiness into the scheduler facts.
   - Twenty-third scheduler-ready slice: the aggregate remote client SQL command
     source now reports command-forward progress facts instead of collapsing the
     command path to one boolean. The implementation deliberately keeps the
     aggregate
     [`HOMER_PROGRESS_SOURCE_COMMAND_RING`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1128)
     source after the rejected per-session split above. The executor
     [`HomerServiceExecuteRemoteClientSqlCommandProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4344)
     now passes a caller-owned `HomerProgressResult` into
     [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7879).
     Later slices removed the bool wrapper and the dead immediate experiment.
     The pump now reports completion-retirement source credits directly through
     `HomerProgressResult`, then adds command-forward facts for
     `itemsProcessed`, `wrsPosted`, and `bytesPosted`. The WR count is computed
     from the actual command-publication path: one WR for the current
     whole-message path when supported by the device and two WRs for the
     record-plus-ready fallback. This gives the service-progress feedback table
     enough information to distinguish "retired CQE work" from "forwarded
     command work" without changing command ordering or source granularity.
   - Validation after the aggregate command-progress facts slice: `make -j8
     service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. A clean warmed c1 pgbench run completed
     `20000/20000` with zero failures at `4618.95 TPS`, p50 `0.214 ms`, p99
     `0.238 ms`. A clean warmed c4 run completed `40000/40000` with zero
     failures at `10865.68 TPS`, p50 `0.350 ms`, p99 `0.631 ms`. Remote RDMA
     basebackup sanity completed at `5.57s` then `4.27s` warm. An accidental
     overlapped c1+c4 pgbench-only launch also completed correctly but showed
     measurement interference, so those throughput numbers were not used as the
     standalone baseline. The services were left on the default fixed policies.
   - Twenty-fourth scheduler-ready slice: the aggregate backend completion-ring
     source now reports completion progress facts directly. The helper
     [`HomerProgressResultMarkItems()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3206)
     was added so source executors can report multiple processed items without
     looping through single-item increments. The aggregate completion scan
     [`TupleSinkServiceConsumeAllCompletionMailboxes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7587)
     now takes a caller-owned `HomerProgressResult`, counts how many backend
     completion mailboxes actually advanced, and records that count as
     `itemsProcessed`. It intentionally preserves the existing scan and the
     direct client-SQL mailbox skip; it only removes the old bool-return wrapper
     at this scheduler boundary. The completion executor
     [`HomerServiceExecuteCompletionProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4433)
     now calls the completion scan directly and finishes the grant from the
     populated result object.
   - Validation after the aggregate completion-progress facts slice: `make -j8
     service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. The first post-restart c4 pgbench run showed
     the known cold max-latency outlier (`7178.32 TPS`, max `1976.85 ms`) with
     normal p50/p99, so it was not used as the warm comparison. The warmed c4
     repeat completed `40000/40000` with zero failures at `10970.27 TPS`, p50
     `0.349 ms`, p99 `0.617 ms`. A warmed c1 run completed `20000/20000` with
     zero failures at `4686.88 TPS`, p50 `0.210 ms`, p99 `0.233 ms`. Remote
     RDMA basebackup completed at `6.10s` cold and `4.29s` warm. Service log
     tails showed only already-ignored stale peer-control doorbells, not
     failed/invalid/underflow/broken signatures.
   - Twenty-fifth scheduler-ready slice: lane send-CQ drain now treats the
     caller-owned `HomerProgressResult` as the only scheduler-facing progress
     output. The helper
     [`HomerServiceDrainOnePeerSendCq()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4172)
     was changed from `bool` to `void`, stops requesting the lower RDMA helper's
     legacy `madeProgress` out-parameter, and records drained CQEs with
     saturating `cqesDrained` accounting. The CQ executor
     [`HomerServiceExecuteCqDrainProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4225)
     already merged payload frontier deltas through the drain callbacks; this
     slice removes the remaining bool-shaped progress output at that helper
     boundary without changing the selected source order, per-call CQ poll cap,
     seen-connection de-duplication, or RDMA drain behavior.
   - Validation after the CQ-drain result-boundary cleanup: `make -j8
     service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. The first post-restart c4 pgbench run had the
     known cold tail outlier (`7104.44 TPS`, max `1976.98 ms`) and was not used
     as the warm comparison. The warmed repeat completed `40000/40000` with zero
     failures at `10901.57 TPS`, p50 `0.346 ms`, p99 `0.611 ms`. Remote RDMA
     basebackup completed at `5.56s` cold and `4.31s` warm. Service log tails
     again showed only already-ignored stale peer-control doorbells, not
     failed/invalid/underflow/broken signatures.
   - Twenty-sixth scheduler-ready slice: the aggregate local-control source now
     reports scheduler progress through `HomerProgressResult` instead of a bool.
     [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18530)
     now takes the caller-owned result object, records one item when the async
     local-control continuation pump advances, and records one item for each
     control slot that this pass either claims for async ownership or answers
     synchronously. The async-owned slot invariant is unchanged: slots handed to
     `TupleSinkServiceStartLocalControlAsyncOpen()`,
     `TupleSinkServiceStartLocalControlAsyncStartCommand()`, or
     `TupleSinkServiceStartLocalControlAsyncPollCompletion()` remain
     `REQUEST_READY` and are skipped by `TupleSinkServiceLocalControlAsyncSlotMatches()`
     until the async pump publishes the real response. The executor
     [`HomerServiceExecuteLocalControlProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4505)
     now passes its grant-local result directly into the local-control pump.
   - Validation after the local-control result-boundary cleanup: `make -j8
     service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. The first post-restart c4 pgbench run again had
     the known cold tail outlier (`7218.38 TPS`, max `1977.21 ms`) and was not
     used as the warm comparison. The warmed repeat completed `40000/40000` with
     zero failures at `11061.32 TPS`, p50 `0.344 ms`, p99 `0.598 ms`. Remote
     RDMA basebackup completed at `5.55s` cold and `4.27s` warm. Service log
     tails again showed only already-ignored stale peer-control doorbells, not
     failed/invalid/underflow/broken signatures.
   - Twenty-seventh scheduler-ready slice: the aggregate peer-control source now
     reports scheduler progress through `HomerProgressResult` instead of
     returning a bool to the source executor. The pump
     [`TupleSinkServicePumpPeerConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11725)
     now takes the caller-owned result object, preserves the existing idle
     peer-pump skip rule, and records one item when
     `TupleSinkServicePumpPeerRequestsRdma()` reports peer-control progress.
     This is intentionally a boundary cleanup, not a richer peer-control policy:
     the lower RDMA peer-control helper still exposes only a coarse
     `madeProgress` bit. The executor
     [`HomerServiceExecutePeerControlProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4581)
     now passes its grant-local result directly into the peer-control pump.
   - Validation after the peer-control result-boundary cleanup: `make -j8
     service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. The first post-restart c4 pgbench run again had
     the known cold tail outlier (`7097.93 TPS`, max `1977.52 ms`) and was not
     used as the warm comparison. The warmed repeat completed `40000/40000` with
     zero failures at `10897.40 TPS`, p50 `0.349 ms`, p99 `0.616 ms`. Remote
     RDMA basebackup completed at `5.43s` cold and `4.22s` warm. Service log
     tails again showed only already-ignored stale peer-control doorbells, not
     failed/invalid/underflow/broken signatures.
   - Twenty-eighth scheduler-ready slice: delayed peer-client command completion
     publication now reports scheduler progress through `HomerProgressResult`.
     [`TupleSinkServicePublishPendingPeerClientCommandCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7653)
     now takes the caller-owned result object and counts how many pending
     peer-client completions were actually published by comparing the
     `lastPublishedCommandSequence`, `lastPublishedCommandState`, and
     `lastPublishedResultFlags` fields before and after the retry. The retry
     scan, failure logging, and payload-EOS ordering remain unchanged. The
     payload-source executor now invokes this helper directly after
     `HomerServiceExecutePayloadProgressPlan()` instead of wrapping a bool.
   - Validation after the delayed peer-client completion result-boundary cleanup:
     `make -j8 service-bin client-bin`, `git diff --check`, install/sync, and
     default service restarts succeeded. The first post-restart c4 pgbench run
     again had the known cold tail outlier (`7100.22 TPS`, max `1978.79 ms`) and
     was not used as the warm comparison. The warmed repeat completed
     `40000/40000` with zero failures at `10917.18 TPS`, p50 `0.346 ms`, p99
     `0.616 ms`. Remote RDMA basebackup completed at `6.11s` cold and `4.41s`
     warm. Service log tails again showed only already-ignored stale
     peer-control doorbells, not failed/invalid/underflow/broken signatures.
   - Twenty-ninth scheduler-ready slice: the aggregate remote client SQL command
     pump now treats `HomerProgressResult` as its only scheduler-facing progress
     output. [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7913)
     was changed from `bool` to `void`; the executor
     `HomerServiceExecuteRemoteClientSqlCommandProgressPlan()` already ignored
     the old return value, so this removes a stale compatibility shape rather
     than changing source selection. The pump still reports command-write
     completion retirement, forwarded command count, posted WR count, and posted
     bytes through the result object. The optional immediate-doorbell branch
     mentioned in this earlier slice was later removed from the service because
     it was unbuildable after the multi-slot command ring became authoritative.
   - Validation after the remote client SQL command result-only cleanup: `make
     -j8 service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. The first post-restart c4 pgbench run again had
     the known cold tail outlier (`7108.35 TPS`, max `1979.35 ms`) and was not
     used as the warm comparison. The warmed repeat completed `40000/40000` with
     zero failures at `10940.64 TPS`, p50 `0.346 ms`, p99 `0.607 ms`. Remote
     RDMA basebackup completed at `6.00s` cold and `4.25s` warm. Service log
     tails again showed only already-ignored stale peer-control doorbells, not
     failed/invalid/underflow/broken signatures.
   - Thirtieth scheduler-ready slice: the single target-completion source now
     reports progress through the mailbox consumer itself instead of wrapping a
     bool at the source executor. [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7427)
     now accepts an optional `HomerProgressResult *` and records one processed
     item after it consumes a backend completion and publishes the mailbox
     `consumedEpoch`. Legacy wait/poll callers pass `NULL` and keep their
     existing bool loops. The aggregate completion-ring scan still passes `NULL`
     because it already counts consumed completion mailboxes at the aggregate
     level. The `HOMER_PROGRESS_SOURCE_TARGET_COMPLETION` executor now passes
     its result object directly into the mailbox consumer.
   - Validation after the target-completion mailbox result cleanup: `make -j8
     service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. The first post-restart c4 pgbench run again had
     the known cold tail outlier (`7180.21 TPS`, max `1979.28 ms`) and was not
     used as the warm comparison. The warmed repeat completed `40000/40000` with
     zero failures at `11048.34 TPS`, p50 `0.346 ms`, p99 `0.606 ms`. Remote
     RDMA basebackup completed at `5.72s` cold and `4.28s` warm. Service log
     tails again showed only already-ignored stale peer-control doorbells, not
     failed/invalid/underflow/broken signatures.
   - Thirty-first scheduler-ready slice: local-control async continuation
     progress now reports touched async-op count through `HomerProgressResult`.
     [`TupleSinkServicePumpLocalControlAsyncOps()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17248)
     was changed from `bool` to `void` and now records how many active async
     local-control operations it touched in the pass. This intentionally
     preserves the old liveness semantics: a pending async op still counts as
     service progress even when the peer-side request remains pending, because
     the service advanced that continuation and refreshed the local heartbeat.
     Stale async ops and completed/failed async responses also count as touched
     ops. [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18530)
     now calls the async continuation pump directly with its result object
     instead of wrapping a bool.
   - Validation after the local-control async continuation result cleanup:
     `make -j8 service-bin client-bin`, `git diff --check`, install/sync, and
     default service restarts succeeded. The first post-restart c4 pgbench run
     again had the known cold tail outlier (`7142.34 TPS`, max `1979.32 ms`) and
     was not used as the warm comparison. The warmed repeat completed
     `40000/40000` with zero failures at `10954.76 TPS`, p50 `0.349 ms`, p99
     `0.618 ms`. Remote RDMA basebackup completed at `5.48s` cold and `4.29s`
     warm. Service log tails again showed only already-ignored stale
     peer-control doorbells, not failed/invalid/underflow/broken signatures.
   - Thirty-second scheduler-ready slice: command-write completion retirement
     now reports the number of command-source slots actually released, not only
     whether one CQE was observed. A signaled command-send checkpoint can retire
     multiple earlier unsignaled writes on the same RC QP, so
     [`TupleSinkServiceRetireClientSqlCommandWriteCompletionsThrough()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2199)
     now accepts a `HomerCommandProgressDelta *`, releases each covered source
     slot, and advances the source-release frontier in that delta.
     [`TupleSinkServicePollRemoteClientSqlCommandWriteCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2388)
     was changed from `bool` to `void` and passes the command delta through
     instead of collapsing retirement into one generic progress bit.
     [`HomerServiceHandleCommandSendCqeFromDrain()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3989)
     likewise merges the command delta into the drain result. One drained CQE can
     therefore update both `cqesDrained` and the source-slot release count when
     the checkpoint retires one or more write descriptors.
   - Validation after the command-write retirement accounting cleanup: `make
     -j8 service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. The first post-restart c4 pgbench run again had
     the known cold tail outlier (`7128.60 TPS`, max `1981.247 ms`) and was not
     used as the warm comparison. The warmed repeat completed `40000/40000` with
     zero failures at `11075.41 TPS`, p50 `0.345 ms`, p99 `0.605 ms`. Remote
     RDMA basebackup completed at `6.36s` cold and `4.47s` warm. Service log
     tails again showed only already-ignored stale peer-control doorbells, not
     failed/invalid/underflow/broken signatures.
   - Thirty-third scheduler-ready slice: the peer-control RDMA pump now returns
     typed scheduler progress instead of a single `madeProgress` bit.
     [`TupleSinkServicePeerPumpProgress`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:237)
     records setup steps, listener events, peer event drains, tagged send
     completions, control responses, control requests, and connection resets for
     one transport pump pass. The main transport helper
     [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7315)
     now fills that struct with cheap local booleans/counters at the existing
     progress points. [`TupleSinkServiceProgressPeerControlResponses()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6827)
     and [`TupleSinkServicePumpIncomingPeerMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4713)
     count consumed responses and dispatched requests directly. The service-side
     [`TupleSinkServicePumpPeerConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11455)
     maps tagged send completions to `cqesDrained` and the remaining
     control-plane categories to `itemsProcessed`. This keeps the existing
     fixed-priority lane order and does not add allocation, persistent work
     objects, or extra scans.
   - Validation after the peer-control typed progress cleanup: `make -j8
     service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. The first post-restart c4 pgbench run again had
     the known cold tail outlier (`7119.82 TPS`, max `1981.819 ms`) and was not
     used as the warm comparison. Warm c4 repeats completed `40000/40000` with
     zero failures at `10873.31 TPS` and `10987.43 TPS`; the second warm run had
     p50 `0.347 ms`, p99 `0.609 ms`, max `16.484 ms`. Remote RDMA basebackup
     completed at `5.63s` cold and `4.26s` warm. Service log tails again showed
     only already-ignored stale peer-control doorbells, not
     failed/invalid/underflow/broken signatures.
   - Thirty-fourth scheduler-ready slice: the dead client-SQL command
     immediate-publication experiment was removed from the service. The code had
     already rejected `HOMER_SERVICE_CLIENT_SQL_COMMAND_IMM_PUBLISH` with a
     compile-time error because the old immediate value carried only a session
     id, which is not enough to route or validate a multi-slot command-ring
     publish. [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7415)
     now has only the active direct-ready publication path: one RDMA write for
     the whole command slot when the connection reports whole-message in-order
     placement, or the record-plus-ready fallback otherwise. The disabled
     command-doorbell service helper and the last `HomerProgressResultMarkBool`
     wrapper were removed. Generic RDMA immediate helpers remain in the
     transport for payload/doorbell use and for any future redesigned command
     immediate path, but the service command source no longer carries an
     unbuildable scheduler branch.
   - Validation after removing the dead command immediate branch: `make -j8
     service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. The first post-restart c4 pgbench run again had
     the known cold tail outlier (`7134.59 TPS`, max `1983.371 ms`) and was not
     used as the warm comparison. The warmed repeat completed `40000/40000` with
     zero failures at `11031.37 TPS`, p50 `0.347 ms`, p99 `0.600 ms`, max
     `17.386 ms`. Remote RDMA basebackup completed at `6.16s` cold and `4.36s`
     warm. Service log tails again showed only already-ignored stale
     peer-control doorbells, not failed/invalid/underflow/broken signatures.
   - Thirty-fifth scheduler-ready slice: outgoing payload senders now report
     payload progress through caller-owned `HomerPayloadProgressDelta` objects
     instead of collapsing a send pass into one generic item. The byte-ring
     sender
     [`HomerServicePumpOutgoingByteRingBaseBackup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11804)
     and fixed-slot sender
     [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12565)
     now return success/failure only and fill the provided progress delta with
     posted WR count, posted byte count, transport-completion frontier,
     source-release frontier, semantic-object frontier, and remote-credit
     frontier facts. The outer payload source
     [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14496)
     merges the outgoing delta with
     [`HomerProgressResultMergePayloadDelta()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3103)
     after applying the payload egress grant. This keeps the source/grant split
     scheduler-ready: a future policy can distinguish "posted five WRs and
     40 MiB", "drained a completion frontier", and "learned remote credit"
     without reparsing transport state after the executor returns. The cleanup
     adds no heap allocation, no persistent work objects, and no extra scan; the
     fallback local delta remains stack-local for defensive NULL callers.
   - Validation after outgoing payload progress-delta reporting: `make -j8
     service-bin client-bin` had already succeeded before deployment; `git diff
     --check`, install/sync, and default service restarts succeeded. The first
     post-restart c4 pgbench run again had the known cold tail outlier
     (`7149.97 TPS`, max `1984.431 ms`) and was not used as the warm comparison.
     The warmed repeat completed `40000/40000` with zero failures at
     `10985.62 TPS`, p50 `0.346 ms`, p99 `0.609 ms`, max `15.837 ms`. Remote
     RDMA basebackup completed at `6.55s` cold and `4.35s` warm. Service log
     tails again showed only already-ignored stale peer-control doorbells, not
     failed/invalid/underflow/broken signatures.
   - Thirty-sixth scheduler-ready slice: incoming and local payload consumers
     now use the same payload-progress vocabulary as outgoing payload egress.
     The local basebackup smoke consumer
     [`HomerServicePumpLocalBaseBackupBlackhole()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13182)
     now returns success/failure and fills a caller-owned delta with consumed
     source-byte and semantic-object frontiers. Receiver consumed-head ACK
     posting through
     [`HomerServicePostReceiverHeadAck()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13465)
     now reports one posted WR plus the remote-credit frontier it grants. The
     byte-ring receive helper
     [`HomerServicePumpIncomingByteRingBaseBackup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13719)
     and fixed-slot receive helper
     [`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14094)
     now return success/failure only and report ACK CQEs, source/frontier
     consumption, semantic object completion, local backpressure, and ACK WR
     posting through `HomerPayloadProgressDelta`. The outer payload source
     merges those deltas at
     [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14550),
     and the now-unused `HomerServicePayloadProgressDeltaMade()` bool helper was
     removed. This keeps the payload source executor frontier-aware in both
     directions without adding heap allocation, persistent work objects, or
     extra source scans.
   - Validation after incoming/local payload progress-delta reporting: `make
     -j8 service-bin client-bin`, `git diff --check`, install/sync, and default
     service restarts succeeded. The first post-restart c4 pgbench run again had
     the known cold tail outlier (`7063.42 TPS`, max `1985.155 ms`) and was not
     used as the warm comparison. The warmed repeat completed `40000/40000` with
     zero failures at `10934.90 TPS`, p50 `0.348 ms`, p99 `0.611 ms`, max
     `16.466 ms`. Remote RDMA basebackup completed at `5.82s` cold and `4.39s`
     warm. Service log tails again showed only already-ignored stale
     peer-control doorbells, not failed/invalid/underflow/broken signatures.
   - Thirty-seventh scheduler-ready slice: backend completion rings are no
     longer represented only as one aggregate scheduler source. The progress
     registry now has fixed per-session completion source and feedback slots in
     `completionRingSources[]` at
     [`HomerServiceProgressRegistry`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1364),
     initialized during
     [`HomerServiceInitializeProgressRegistry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4430).
     Ready-set construction uses
     [`HomerServiceCompletionRingSourceReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3431)
     and
     [`HomerCompletionRingProgressSourceRef()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3402)
     to append only sessions whose completion mailbox published frontier has
     advanced, while preserving the existing client-SQL direct-frontend skip
     rule. The completion executor
     [`HomerServiceExecuteCompletionProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4211)
     now consumes the selected session mailbox directly when the grant owner is
     a session, and keeps the aggregate scan only as a fallback for older callers.
     The scheduler can therefore order completion work per session without heap
     work records or a global map lookup.
   - Important caveat from the same implementation pass: the remote client SQL
     command-ring source remains intentionally aggregate. A strict per-session
     command-source split was attempted and reverted after c4 pgbench smoke runs
     hung after peer sessions opened. Relaxing the readiness test and relying on
     the separate lane-CQ drain was not sufficient. This showed that the command
     source has more lifecycle coupling than completion consumption: it performs
     command-write CQE retirement, source-slot credit recovery, active-session
     scanning, and command forwarding together in
     [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7507).
     Do not split this source merely by checking mailbox `publishedEpoch`,
     per-slot `readySeq`, or active session state. The next command-source split
     needs explicit command credit/frontier facts and a lifecycle model that
     preserves source-slot retirement before per-session forwarding.
   - Validation after per-session completion-ring sources and the reverted
     command-source split: `make -j8 service-bin client-bin`, `git diff
     --check`, install/sync, and default service restarts succeeded. After
     restarting PostgreSQL and both Homer services, a short c4 smoke completed
     `4000/4000` with zero failures. Warm c4 repeats completed
     `40000/40000` with zero failures at `10784.72 TPS`, `10643.32 TPS`, and
     `10648.89 TPS`; the latter repeats had p50 `0.359 ms` and p99
     `0.629-0.632 ms`. Remote RDMA basebackup completed at `6.39s` cold and
     `4.44s` warm. Service log tails again showed only already-ignored stale
     peer-control doorbells, not failed/invalid/underflow/broken signatures.
     This is still in the recent warm-performance band, but lower than the best
     `10.8-11.1k TPS` c4 repeats; a follow-up performance pass should separate
     post-restart variance from overhead in per-session completion ready-source
     construction and feedback updates.
   - Thirty-eighth scheduler-ready slice: the aggregate remote client SQL
     command source now uses an explicit command-progress delta instead of
     writing command facts directly into the generic progress result at scattered
     call sites. The new stack-local
     [`HomerCommandProgressDelta`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:912)
     separates command send-CQE/source-slot retirement from backend-visible
     command publication. Command write retirement through
     [`TupleSinkServiceRetireClientSqlCommandWriteCompletionEntry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2096)
     now records released source slots and a source-release frontier in that
     delta. Owner-polled command send-CQ progress through
     [`TupleSinkServicePollRemoteClientSqlCommandWriteCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2388)
     records drained CQEs into the same delta, while lane-owned CQ drain merges
     command deltas at
     [`HomerServiceHandleCommandSendCqeFromDrain()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3989).
     Command forwarding in
     [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7560)
     records forwarded command count, WR count, bytes posted, source-credit
     blocking, local-resource blocking, and a command-publish frontier before
     merging through
     [`HomerProgressResultMergeCommandDelta()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3197).
     This is behavior-preserving: the source remains aggregate, the RDMA command
     publication path is unchanged, and no heap work records or extra source
     scans were added. The purpose is to make the credit/frontier vocabulary
     explicit before attempting another per-session command-source split.
     Post-cleanup validation rebuilt with `make -j8 service-bin client-bin`,
     passed `git diff --check` in both `/data/dbcomm/citus-dbcomm` and
     `/data/dbcomm/postgres-citus`, installed the rebuilt service binary on
     farnet1 and farnet0, and restarted both Homer services. The c4 pgbench
     warmup completed `4000/4000` with zero failures at `1679.642606 TPS`,
     p50 `0.381 ms`, p95 `0.543 ms`, p99 `0.651 ms`, and a cold-tail max of
     `1993.938 ms`. The warmed c4 run completed `40000/40000` with zero
     failures at `10724.228606 TPS`, p50 `0.356 ms`, p95 `0.517 ms`, p99
     `0.603 ms`, max `17.394 ms`, and initial connection time `23.316 ms`.
     Remote RDMA basebackup completed at `6.54s` cold and `4.29s` warm.
     Service log tails showed only already-ignored stale peer-control doorbells,
     not failed/invalid/underflow/broken/error signatures. This keeps the
     command-progress delta in the prior warm-performance band; the small TPS
     difference from the earlier `10802.323688 TPS` run is within the observed
     post-restart variance and is not evidence of a command-delta regression.
   - Thirty-ninth scheduler-ready slice: payload progress no longer stores a
     coarse `madeProgress` bit inside
     [`HomerPayloadProgressDelta`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:881).
     Instead, consumed doorbells and local publications are explicit boolean
     payload facts (`doorbellConsumed` and `localPublication`), while CQE drain,
     WR/byte posting, transport completion, source release, semantic-object, and
     remote-credit frontiers remain typed fields in the same stack-local delta.
     [`HomerPayloadProgressDeltaMade()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3125)
     derives the loop-level progress signal from those facts when
     [`HomerProgressResultMergePayloadDelta()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3145)
     merges the payload delta into the generic scheduler result. Doorbell and
     local-publication call sites now use
     [`HomerServicePayloadProgressMarkDoorbellConsumed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4622)
     and
     [`HomerServicePayloadProgressMarkLocalPublication()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4632)
     instead of writing a shared progress bit directly. This preserves the
     aggregate payload-source shape and current pump ordering; it adds no heap
     work records, no extra source scans, and no new source split. A small
     performance pass changed the new doorbell/publication facts from counters
     to booleans because only presence is consumed by the scheduler predicate,
     keeping the hot path close to the old idempotent progress-store cost.
     Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check` in `/data/dbcomm/citus-dbcomm`, installed the final
     service binary on farnet1 and farnet0 with matching SHA-256
     `5c888d28f1ad83b46d8821f53f8a7000951bb2a9f2ab5af835f2cdcc390b25fc`, and
     restarted both services. Final-binary c4 pgbench warmup completed
     `4000/4000` with zero failures at `1680.274792 TPS`, p50 `0.365 ms`, p95
     `0.529 ms`, p99 `0.650 ms`, and cold-tail max `1999.034 ms`. The warmed
     c4 run completed `40000/40000` with zero failures at `10758.834011 TPS`,
     p50 `0.358 ms`, p95 `0.521 ms`, p99 `0.622 ms`, max `15.299 ms`, and
     initial connection time `23.663 ms`; throughput is slightly above the
     thirty-eighth-slice warmed `10724.228606 TPS`, while p95/p99 are close but
     slightly higher. Final-binary remote RDMA basebackup completed at `6.71s`
     cold and then `4.65s` and `4.54s` warmed. Correctness logs showed only
     already-ignored stale peer-control doorbells, not
     failed/invalid/underflow/broken/error signatures. The SQL path therefore
     shows no throughput regression for this slice, but basebackup should stay
     on the watch list because the warmed band is slower than the prior single
     `4.29s` warm observation and this timing includes checkpoint/filesystem
     effects rather than isolated transport-only time.
   - Fortieth scheduler-ready slice: command write checkpoint retirement now
     treats the helper return value as success/failure only. The same
     [`TupleSinkServiceRetireClientSqlCommandWriteCompletionsThrough()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2199)
     call still retires every unsignaled command write covered by a signaled CQE,
     but it no longer carries a local `madeProgress` accumulator or returns
     progress as a bool. Released source slots and the source-release frontier
     remain explicit facts in
     [`HomerCommandProgressDelta`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:912),
     and the lane-owned drain callback still merges those facts through
     [`HomerProgressResultMergeCommandDelta()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3197).
     The owner-polled command send-CQ path now logs and stops that connection's
     poll loop if checkpoint retirement fails, rather than silently ignoring the
     helper result. This is behavior-preserving for successful command CQEs: the
     command source remains aggregate, no heap work records or source scans were
     added, and command credit/frontier facts remain scheduler-visible for the
     later command-source split. Validation rebuilt with
     `make -j8 service-bin client-bin`, passed `git diff --check` in
     `/data/dbcomm/citus-dbcomm`, installed the service binary on farnet1 and
     farnet0 with matching SHA-256
     `9d11ed6b5a421e40a280eca50fd1740d3384ce968973b78d7e4acbd6a4163c97`, and
     restarted both services. The c4 pgbench warmup completed `4000/4000` with
     zero failures at `1683.897144 TPS`, p50 `0.368 ms`, p95 `0.524 ms`, p99
     `0.640 ms`, and cold-tail max `1998.142 ms`. The warmed c4 run completed
     `40000/40000` with zero failures at `10877.226568 TPS`, p50 `0.354 ms`,
     p95 `0.513 ms`, p99 `0.608 ms`, max `15.700 ms`, and initial connection
     time `23.201 ms`, so this slice shows no SQL throughput regression against
     the thirty-ninth final `10758.834011 TPS`. Remote RDMA basebackup completed
     at `6.13s` cold and `4.49s` warm. Service log tails again showed only
     already-ignored stale peer-control doorbells, not
     failed/invalid/underflow/broken/error signatures.
   - Forty-first scheduler-ready slice: the aggregate remote client SQL command
     source now has a cheap source-readiness predicate before it is appended to
     the coarse scheduler ready set. Per-session
     [`HomerServiceRemoteClientSqlCommandSourceReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3568)
     checks only service/session state, `clientSqlRemoteSender`, the command
     mailbox pointer, and the `publishedEpoch != consumedEpoch` frontend-work
     condition. Aggregate
     [`HomerServiceAnyRemoteClientSqlCommandSourceReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3586)
     scans the active-session range, and
     [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3604)
     now appends `HOMER_PROGRESS_SOURCE_COMMAND_RING` only when that aggregate
     predicate is true. This removes idle command-source grants when no frontend
     command mailbox has published work. It deliberately does not fold command
     send-CQ work into the command source: signaled command write completions are
     still exposed through the separate CQ-drain source via
     [`ClientSqlCommandWriteSignaledCompletionActiveCount`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:747).
     The initial validation of this slice surfaced stale command write checkpoint
     messages on farnet0 after the new source gating reduced owner command-source
     polling frequency. The fix keeps
     [`TupleSinkServiceRetireClientSqlCommandWriteCompletionsThrough()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2206)
     strict for invalid checkpoint indices and lane-owned CQ draining, but lets
     the owner-polled send-CQ fallback accept an already-retired checkpoint at
     [`TupleSinkServiceDrainClientSqlCommandCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2495).
     That case is valid because
     [`HomerServiceHandleCommandSendCqeFromDrain()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4042)
     may have already retired the source slots and merged the source-release
     facts for the same CQE; the later owner-polled observation is CQ polling
     progress, not another source-credit release. Validation rebuilt with
     `make -j8 service-bin client-bin`, passed `git diff --check` in
     `/data/dbcomm/citus-dbcomm`, installed the fixed service binary on farnet1
     and farnet0 with matching SHA-256
     `764853dd70bd12ef275cc3a452b713c28665ce6ca3d70da9e2cebef52ad76ead`, and
     restarted both services. The c4 pgbench warmup completed `4000/4000` with
     zero failures at `1684.707072 TPS`, p50 `0.352 ms`, p95 `0.528 ms`, p99
     `0.656 ms`, and cold-tail max `1999.791 ms`. The warmed c4 run completed
     `40000/40000` with zero failures at `10800.882756 TPS`, p50 `0.353 ms`,
     p95 `0.520 ms`, p99 `0.614 ms`, max `17.031 ms`, and initial connection
     time `22.787 ms`; this is below the fortieth-slice `10877.226568 TPS` but
     still in the recent warmed band and does not show a clear SQL regression.
     Remote RDMA basebackup completed at `6.81s` cold and `4.76s` warm, so
     basebackup remains a watch item because it is slower than the recent
     `4.49s`/`4.57s` warmed observations and these wall-clock timings include
     checkpoint/filesystem effects. Post-fix service log tails showed only the
     known ignored stale peer-control doorbells; the command write checkpoint
     stale messages were gone.
   - Forty-second scheduler-ready slice: the service-progress registry now has
     fixed per-session command-source metadata slots, but command scheduling
     behavior is intentionally unchanged in this slice. The new
     `remoteClientSqlCommandSources[]` and `remoteClientSqlCommandFeedbacks[]`
     arrays in
     [`HomerServiceProgressRegistry`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1398)
     mirror the per-session completion-source layout. Source and feedback lookup
     through
     [`HomerServiceProgressCoreForSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3694)
     and
     [`HomerServiceProgressFeedbackForSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3734)
     can now resolve a future selected per-session `COMMAND_RING` source without
     a map lookup or heap work record. Startup registration in
     [`HomerServiceInitializeProgressRegistry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4578)
     initializes those per-session command refs with the fixed session-table
     owner pointers at
     [`remoteClientSqlCommandSources[]` initialization](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4611).
     This deliberately does not append per-session command refs to the ready set
     yet and does not change
     [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7614):
     the aggregate command executor still owns the command-session scan, command
     forwarding, and owner-polled command send-CQ fallback. The next command split
     should change the executor boundary so a selected session source is pumped
     once, instead of accidentally running the aggregate scan once per ready
     session. Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check` in `/data/dbcomm/citus-dbcomm`, installed the fixed
     service binary on farnet1 and farnet0 with matching SHA-256
     `4c0c0c585396c896fb7c2d1441152fdb5f08e54992b0d94109e09c8452f2e84c`, and
     restarted both services. The c4 pgbench warmup completed `4000/4000` with
     zero failures at `1680.612247 TPS`, p50 `0.368 ms`, p95 `0.527 ms`, p99
     `0.657 ms`, and cold-tail max `2001.115 ms`. The warmed c4 run completed
     `40000/40000` with zero failures at `10826.403758 TPS`, p50 `0.358 ms`,
     p95 `0.514 ms`, p99 `0.614 ms`, max `15.675 ms`, and initial connection
     time `22.438 ms`; this remains in the recent warmed band and shows no clear
     SQL regression for metadata-only command-source scaffolding. Remote RDMA
     basebackup completed at `6.34s` cold and `4.63s` warm. Service log tails
     showed only the known ignored stale peer-control doorbells, not command
     checkpoint stale messages or failed/invalid/underflow/broken/error
     signatures.
   - Forty-third scheduler-ready slice: the remote client SQL command source is
     now split at the scheduler ready-set and executor boundary. Ready-set
     construction in
     [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3634)
     scans active sessions and appends a selected per-session `COMMAND_RING` ref
     from
     [`HomerRemoteClientSqlCommandProgressSourceRef()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3539)
     only when
     [`HomerServiceRemoteClientSqlCommandSourceReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3598)
     sees frontend-published command work for that session. The command plan
     builder
     [`HomerServiceBuildRemoteClientSqlCommandProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4342)
     now preserves the selected source, and
     [`HomerServiceExecuteRemoteClientSqlCommandProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4382)
     validates the selected owner/index before calling
     [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7712)
     with a one-session scan range. The aggregate command source remains as a
     defensive fallback, but normal ready-set construction no longer schedules one
	     aggregate command scan for all sessions. A first version of this slice skipped
	     the owner-polled command send-CQ fallback for selected-session grants and the
	     c1 pgbench smoke timed out after `25s`. That reproduced the earlier split
	     hazard: command source-slot credit retirement still needs the fallback if the
	     command source is selected before the lane-owned CQ drain observes a CQE. The corrected
     version keeps the fallback in selected-session mode at
     [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7740),
     and the prior stale-checkpoint tolerance keeps already lane-retired CQEs
     benign. The fixed binary also treats only generation-zero command refs as
     aggregate, so session-table slot 0 is not misclassified just because its
     owner pointer equals `ActiveSessionTableBase`.
     Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check` in `/data/dbcomm/citus-dbcomm`, installed the fixed
     service binary on farnet1 and farnet0 with matching SHA-256
     `42ea7ddbf1fc69aec68ea3f4799bdc68576e4712b7f15d0578182f64d3b4a54b`, and
     restarted both services. The timeout-bounded c1 smoke completed `1000/1000`
     with zero failures at `448.872723 TPS`, p50 `0.220 ms`, p95 `0.236 ms`, p99
     `0.340 ms`, and cold-tail max `2001.847 ms`. The subsequent c4 warmup
     completed `4000/4000` with zero failures at `10134.150821 TPS`, p50
     `0.362 ms`, p95 `0.527 ms`, p99 `0.652 ms`, and max `16.476 ms`. The warmed
     c4 run completed `40000/40000` with zero failures at `10799.655059 TPS`,
     p50 `0.353 ms`, p95 `0.526 ms`, p99 `0.624 ms`, max `16.150 ms`, and
     initial connection time `23.053 ms`; this is in the recent warmed SQL band,
     though p95 is slightly softer than the forty-second slice. Remote RDMA
     basebackup completed at `6.36s` cold and `4.58s` warm. Service log tails
     showed only the known ignored stale peer-control doorbells, not command
     checkpoint stale messages or failed/invalid/underflow/broken/error
     signatures.
   - Forty-fourth scheduler-ready slice: selected command sources now write
     command-specific feedback facts after each grant. The
     [`HomerServiceUpdateProgressFeedback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3820)
     `HOMER_PROGRESS_SOURCE_COMMAND_RING` branch reads the selected session's
     command mailbox `publishedEpoch` and `consumedEpoch` at
     [`readyCommandCount` derivation](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3918),
     records the pending command count in `readyObjectCountHint`, records
     `clientSqlRemoteCommandOutstandingWriteCount` in `outstandingWrCount`, and
     marks `ready`, `creditBlocked`, `resourcePressure`, and
     `sourceReleaseBlocked` from the same fixed-session facts at
     [`command feedback update`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3941).
     This keeps command source readiness record-count based, not byte based, and
     intentionally leaves signaled send-CQ accounting on the separate CQ-drain
     source. The aggregate command-source fallback records only the global
     outstanding command write count, while the normal per-session source has the
     precise mailbox readiness and source-credit facts needed by future policies.
     Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check` in `/data/dbcomm/citus-dbcomm`, installed the fixed
     service binary on farnet1 and farnet0 with matching SHA-256
     `97a276cc744d1c7e70ab4305580dd4ae4ca9d378649bc01045b57e8c82e7aff3`, and
     restarted both services. The timeout-bounded c1 smoke completed `1000/1000`
     with zero failures at `449.348534 TPS`, p50 `0.216 ms`, p95 `0.230 ms`, p99
     `0.345 ms`, and cold-tail max `2002.782 ms`. The c4 warmup completed
     `4000/4000` with zero failures at `10431.281326 TPS`, p50 `0.351 ms`, p95
     `0.515 ms`, p99 `0.613 ms`, and max `16.390 ms`. The warmed c4 run
     completed `40000/40000` with zero failures at `10760.952699 TPS`, p50
     `0.354 ms`, p95 `0.521 ms`, p99 `0.616 ms`, max `16.487 ms`, and initial
     connection time `23.141 ms`; this is a small drop from the forty-third
     `10799.655059 TPS` but still in the recent warmed SQL band. Remote RDMA
     basebackup completed at `6.37s` cold and `4.75s` warm, so basebackup remains
     on the watch list because this warmed run is slower than the prior `4.58s`
     and the measurement is end-to-end wall clock. Service log tails showed only
     the known ignored stale peer-control doorbells, not command checkpoint stale
     messages or failed/invalid/underflow/broken/error signatures.
   - Forty-fifth scheduler-ready slice: command ready-count feedback no longer
     rereads the command mailbox during feedback update. The command delta now has
     `readyCommandsRemaining` in
     [`HomerCommandProgressDelta`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:920),
     and the generic
     [`HomerProgressResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1367)
     carries that as `readyObjectCountHint`. The selected command pump already
     has `publishedEpoch` and `nextSequence` after forwarding a command, so it
     records the remaining ready-command count at
     [`readyAfterForward`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8077)
     before
     [`HomerProgressResultMergeCommandDelta()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3226)
     merges it into the grant result. Cross-grant result merging also preserves
     the hint through
     [`HomerProgressResultMergeResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3255).
     The command feedback branch now consumes `grantResult->readyObjectCountHint`
     at
     [`command feedback update`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3931)
     and reads only the selected session's scalar outstanding command-write count.
     This removes the two extra mailbox epoch loads added by the forty-fourth
     slice while keeping per-session ready/credit/source-release facts available
     to future policies.
     Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check` in `/data/dbcomm/citus-dbcomm`, installed the fixed
     service binary on farnet1 and farnet0 with matching SHA-256
     `2574c0e4ba3e69b952602011243da0d433749f22735e6e34ef68f56082d14b09`, and
     restarted both services. The timeout-bounded c1 smoke completed `1000/1000`
     with zero failures at `448.159789 TPS`, p50 `0.219 ms`, p95 `0.234 ms`, p99
     `0.338 ms`, and cold-tail max `2006.586 ms`. The c4 warmup completed
     `4000/4000` with zero failures at `10162.549987 TPS`, p50 `0.362 ms`, p95
     `0.528 ms`, p99 `0.660 ms`, and max `16.641 ms`. Two warmed c4 repeats
     completed `40000/40000` with zero failures: first `10691.942419 TPS`, p50
     `0.358 ms`, p95 `0.533 ms`, p99 `0.637 ms`, max `17.114 ms`; second
     `10832.654713 TPS`, p50 `0.352 ms`, p95 `0.521 ms`, p99 `0.627 ms`, max
     `16.935 ms`. The recovered second repeat keeps the SQL path in the recent
     warmed band, while the first repeat should remain part of the variance
     record. Remote RDMA basebackup completed at `6.26s` cold and `4.46s` warm,
     which is back near the better recent warm observations. Service log tails
     showed only the known ignored stale peer-control doorbells, not command
     checkpoint stale messages or failed/invalid/underflow/broken/error
     signatures.
   - Rejected completion-source feedback follow-up after the forty-fifth slice:
     a simple completion-ring extension was tested but backed out because it
     regressed warmed client SQL throughput. The rejected change tried to apply
     the same generation-zero aggregate/source discriminator used by command
     sources to
     [`HomerCompletionRingProgressSourceRef()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3528),
     [`HomerServiceProgressCoreForSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3756),
     [`HomerServiceProgressFeedbackForSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3796),
     and
     [`HomerServiceExecuteCompletionProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4486),
     then had
     [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7282)
     publish completion backlog hints through `progressResult->stillReady` and
     `progressResult->readyObjectCountHint` after consuming one mailbox event.
     It compiled and deployed as service binary SHA-256
     `4753494e7dd4be393dbf44894888a06aaf3f36d217d85702392e862eb9fc3c93`.
     The timeout-bounded c1 smoke still completed `1000/1000` with zero
     failures at `448.532067 TPS`, but warmed c4 repeats dropped to
     `10625.448427 TPS` and `10503.923084 TPS`, below the accepted forty-fifth
     slice's warmed band. The change was therefore reverted, and the deployed
     service binary was restored to the accepted forty-fifth SHA-256
     `2574c0e4ba3e69b952602011243da0d433749f22735e6e34ef68f56082d14b09`.
     Recovery validation on that restored binary passed c1 `1000/1000` with zero
     failures at `448.938619 TPS`, p50 `0.214 ms`, p95 `0.228 ms`, p99
     `0.337 ms`, and cold-tail max `2006.852 ms`; c4 warmup `4000/4000` with
     zero failures at `10460.251046 TPS`, p50 `0.347 ms`, p95 `0.515 ms`, p99
     `0.621 ms`, and max `15.028 ms`; and warmed c4 confirmation `40000/40000`
     with zero failures at `10827.863232 TPS`, p50 `0.351 ms`, p95 `0.514 ms`,
     p99 `0.610 ms`, max `23.686 ms`, and initial connection time `23.534 ms`.
     Service log tails remained clean except for the known ignored stale
     peer-control doorbells. The design implication is that completion-source
     slot-0/generation handling and completion-backlog feedback need a separate
     performance pass; do not reapply the simple completion generation-zero plus
     backlog-hint patch as-is.
   - Forty-sixth scheduler-ready slice: payload ready-set construction now skips
     idle locally initiated sender streams. Previously,
     [`HomerServiceBuildPayloadReadySources()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3364)
     appended every active payload stream, so an active but idle sender still
     reached
     [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14822)
     on every service pass. The new
     [`HomerServicePayloadStreamSourceReadyForScheduler()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3689)
     keeps receive-side streams conservatively ready because new payload work is
     discovered by polling peer data doorbells, keeps local blackhole smoke
     streams always ready because they are not the measured remote-RDMA path, and
     keeps close/cleanup or pending completion/ACK states ready. Locally initiated
     sender streams are appended only when
     [`HomerServicePayloadSendQueueHasReadyWork()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3651)
     sees producer bytes/objects, tuple-result EOS publication work, or pending
     transport cleanup. This narrows scheduler-visible payload sources without
     changing the specialized sender/receiver pump internals or the existing
     egress grant behavior in
     [`HomerServiceBuildPayloadEgressReadyFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2823).
     Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check`, installed the service binary on farnet1 and farnet0 with
     matching SHA-256
     `34728c660c8f2d99f5de4eb24db7ddbd0e32cbb48413432ad4ca16cf047ce602`, and
     restarted both Homer services with service CPU `2` and backend CPU set
     `4,5,6,7`. The install command emitted Git safe-directory warnings while
     stamping version metadata, but the install completed and both installed
     binary hashes matched. The timeout-bounded remote c1 smoke completed
     `1000/1000` with zero failures at `447.935153 TPS`, p50 `0.216 ms`, p95
     `0.231 ms`, p99 `0.345 ms`, and cold-tail max `2009.391 ms`. The remote c4
     warmup completed `4000/4000` with zero failures at `10647.302772 TPS`, p50
     `0.342 ms`, p95 `0.495 ms`, p99 `0.573 ms`, and max `15.148 ms`. Two warmed
     remote c4 repeats completed `40000/40000` with zero failures: first
     `10979.482367 TPS`, p50 `0.344 ms`, p95 `0.507 ms`, p99 `0.593 ms`, max
     `16.373 ms`; second `10943.882506 TPS`, p50 `0.347 ms`, p95 `0.513 ms`, p99
     `0.608 ms`, max `16.826 ms`. Remote RDMA basebackup completed at `6.25s`
     for the first post-restart run and `4.24s` warm, so payload validation also
     stayed in the recent warmed band. Service logs on both farnet1 and farnet0
     had no failed/invalid/underflow/overrun/broken/error/stale/checkpoint/mismatch
     signatures, and process scans showed only PostgreSQL plus the two Homer
     services, not leftover pgbench, pg_basebackup, or remote-exec backends.
   - Forty-seventh scheduler-ready slice: selected payload grants now validate
     their fixed stream-table slot and generation before pumping the stream. The
     ready-set path already stamps payload source refs in
     [`HomerPayloadStreamProgressSourceRef()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4073)
     with `serviceStreamId`, but
     [`HomerServiceExecutePayloadProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4105)
     previously trusted the owner pointer unconditionally. The executor now also
     receives the fixed `streamEntries` table from
     [`HomerServiceExecuteProgressSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18683)
     and rejects a grant whose index is out of the active scan range, whose owner
     no longer matches the selected table slot, whose stream is inactive, or whose
     generation no longer matches the current `serviceStreamId`. A stale selected
     source logs the guarded
     [`stale payload progress grant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4148)
     diagnostic before returning failure. This does not change normal scheduling
     policy; it turns the existing source generation into an actual defensive
     executor invariant before future policies carry source plans through more
     scheduling logic.
     Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check`, installed the service binary on farnet1 and farnet0 with
     matching SHA-256
     `d39de9553fd8782f69890aade55a2517a5d239b169ebfce7d4ae83ce1f1789bc`, and
     restarted both Homer services with service CPU `2` and backend CPU set
     `4,5,6,7`. The install command again emitted Git safe-directory warnings
     while stamping version metadata, but the install completed and both
     installed binary hashes matched. The timeout-bounded remote c1 smoke
     completed `1000/1000` with zero failures at `448.563654 TPS`, p50
     `0.215 ms`, p95 `0.233 ms`, p99 `0.337 ms`, and cold-tail max
     `2007.917 ms`. The remote c4 warmup completed `4000/4000` with zero
     failures at `10586.799849 TPS`, p50 `0.344 ms`, p95 `0.504 ms`, p99
     `0.600 ms`, and max `15.093 ms`. Two warmed remote c4 repeats completed
     `40000/40000` with zero failures: first `10881.153054 TPS`, p50
     `0.347 ms`, p95 `0.508 ms`, p99 `0.600 ms`, max `15.614 ms`; second
     `10823.037919 TPS`, p50 `0.350 ms`, p95 `0.515 ms`, p99 `0.608 ms`, max
     `15.692 ms`. Remote RDMA basebackup completed at `6.00s` for the first
     post-restart run and `4.53s` / `4.58s` for warmed repeats. That warmed
     basebackup pair is softer than the forty-sixth slice's `4.24s`, but still
     in the recent accepted band, so keep basebackup on the watch list rather
     than attributing the difference to this branch-only stale-source guard.
     Service logs on both farnet1 and farnet0 had no
     failed/invalid/underflow/overrun/broken/error/stale/checkpoint/mismatch
     signatures, and process scans showed only PostgreSQL plus the two Homer
     services, not leftover pgbench, pg_basebackup, or remote-exec backends.
   - Forty-eighth scheduler-ready slice: selected remote client SQL command
     grants now validate the fixed session-table generation before touching the
     frontend command mailbox. The ready-set path stamps selected command refs in
     [`HomerRemoteClientSqlCommandProgressSourceRef()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3557)
     with the current `serviceSessionId`, and
     [`HomerServiceExecuteRemoteClientSqlCommandProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4533)
     already rejected a selected source whose owner pointer did not match the
     selected session-table slot. The executor now also rejects inactive selected
     sessions or a generation that no longer matches the slot's current
     `serviceSessionId`, logging the guarded
     [`stale remote-client command grant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4589)
     diagnostic before returning failure. The aggregate command-source fallback
     still uses generation zero, so this only hardens the normal per-session
     command source path for future policy reordering.
     Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check`, installed the service binary on farnet1 and farnet0 with
     matching SHA-256
     `2c247fc1009163b77bf9e0d4d74c35dcef56239db8558ddb714f97d2aa7f8759`, and
     restarted both Homer services with service CPU `2` and backend CPU set
     `4,5,6,7`. The install command again emitted Git safe-directory warnings
     while stamping version metadata, but the install completed and both
     installed binary hashes matched. The timeout-bounded remote c1 smoke
     completed `1000/1000` with zero failures at `448.109783 TPS`, p50
     `0.215 ms`, p95 `0.229 ms`, p99 `0.331 ms`, and cold-tail max
     `2010.130 ms`. The remote c4 warmup completed `4000/4000` with zero
     failures at `10430.220521 TPS`, p50 `0.346 ms`, p95 `0.514 ms`, p99
     `0.609 ms`, and max `15.284 ms`. Two warmed remote c4 repeats completed
     `40000/40000` with zero failures: first `10891.715652 TPS`, p50
     `0.347 ms`, p95 `0.516 ms`, p99 `0.606 ms`, max `16.918 ms`; second
     `10819.732704 TPS`, p50 `0.349 ms`, p95 `0.517 ms`, p99 `0.615 ms`, max
     `16.585 ms`. Remote RDMA basebackup completed at `6.31s` for the first
     post-restart run and `4.32s` warm, back near the better recent observations.
     Service logs on both farnet1 and farnet0 had no
     failed/invalid/underflow/overrun/broken/error/stale/checkpoint/mismatch
     signatures, and process scans showed only PostgreSQL plus the two Homer
     services, not leftover pgbench, pg_basebackup, or remote-exec backends.
   - Forty-ninth scheduler-ready slice: progress ready-set overflow is now
     explicit and rate-limited instead of silent truncation. The fixed ready set
     remains stack-local and capped by `HOMER_PROGRESS_PLAN_MAX_GRANTS`, but
     [`HomerProgressReadySet`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1223)
     now records `droppedSourceCount`, `overflowKindMask`, and `overflowed`.
     [`HomerProgressReadySetAppend()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3350)
     saturates the dropped-source count and tracks the source-kind mask when a
     later source cannot fit. After coarse ready-set construction,
     [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18816)
     calls
     [`HomerServiceReportReadySetOverflow()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3383),
     which logs the first few overflow events and then powers of two. This keeps
     the normal hot path heap-free and nearly unchanged while making scheduler
     starvation debuggable if many command, completion, and payload sources are
     ready in one pass.
     Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check`, installed the service binary on farnet1 and farnet0 with
     matching SHA-256
     `a6037fcd5252862e58308efeeedbd754ebe7b2946edf838381c58ac9b5ccaeb8`, and
     restarted both Homer services with service CPU `2` and backend CPU set
     `4,5,6,7`. The install command again emitted Git safe-directory warnings
     while stamping version metadata, but the install completed and both
     installed binary hashes matched. The timeout-bounded remote c1 smoke
     completed `1000/1000` with zero failures at `448.637913 TPS`, p50
     `0.211 ms`, p95 `0.234 ms`, p99 `0.326 ms`, and cold-tail max
     `2011.203 ms`. The remote c4 warmup completed `4000/4000` with zero
     failures at `10439.911783 TPS`, p50 `0.349 ms`, p95 `0.522 ms`, p99
     `0.622 ms`, and max `15.788 ms`. Two warmed remote c4 repeats completed
     `40000/40000` with zero failures: first `10806.167080 TPS`, p50
     `0.348 ms`, p95 `0.518 ms`, p99 `0.615 ms`, max `16.711 ms`; second
     `10848.575582 TPS`, p50 `0.348 ms`, p95 `0.515 ms`, p99 `0.613 ms`, max
     `16.951 ms`. Remote RDMA basebackup completed at `5.87s` for the first
     post-restart run and `4.18s` warm, better than the immediately prior warm
     basebackup observation. Service logs on both farnet1 and farnet0 had no
     failed/invalid/underflow/overrun/overflow/broken/error/stale/checkpoint/mismatch
     signatures, so the normal c1/c4/basebackup validation did not hit the new
     overflow diagnostic. Process scans showed only PostgreSQL plus the two Homer
     services, not leftover pgbench, pg_basebackup, or remote-exec backends.
   - Fiftieth scheduler-ready slice: ready-set overflow diagnostics now preserve
     the first dropped source identity. The overflow-only fields in
     [`HomerProgressReadySet`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1223)
     now include `firstDroppedSource`; the value is deliberately overwritten only
     by the first overflowing
     [`HomerProgressReadySetAppend()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3350)
     in a service pass so
     [`HomerProgressReadySetReset()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3336)
     does not add a per-pass `memset` to the normal non-overflow path.
     [`HomerServiceReportReadySetOverflow()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3383)
     now logs `first_dropped_kind`, `first_dropped_index`, and
     `first_dropped_generation` with the existing kept/dropped/kind-mask facts.
     That makes a future scheduler starvation or cap-sizing bug point at the
     first omitted source instead of only the omitted source class mask.
     Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check`, installed the service binary on farnet1 and farnet0 with
     matching SHA-256
     `f29340367626b2bc1b650865071940e050ff5408942cc31491f3fdb1c743c522`, and
     restarted both Homer services with service CPU `2` and backend CPU set
     `4,5,6,7`. The timeout-bounded remote c1 smoke completed `1000/1000` with
     zero failures at `447.600926 TPS`, p50 `0.216 ms`, p95 `0.231 ms`, p99
     `0.342 ms`, and cold-tail max `2011.654 ms`. The remote c4 warmup completed
     `4000/4000` with zero failures at `10551.110900 TPS`, p50 `0.343 ms`, p95
     `0.511 ms`, p99 `0.620 ms`, and max `15.581 ms`. Two warmed remote c4
     repeats completed `40000/40000` with zero failures: first `10924.475550 TPS`,
     p50 `0.347 ms`, p95 `0.513 ms`, p99 `0.607 ms`, max `15.800 ms`; second
     `10818.412939 TPS`, p50 `0.352 ms`, p95 `0.516 ms`, p99 `0.613 ms`, max
     `15.867 ms`. Remote RDMA basebackup completed at `5.74s` for the first
     post-restart run and `4.37s` warm. Service logs on both farnet1 and farnet0
     had no
     failed/invalid/underflow/overrun/overflow/broken/error/stale/checkpoint/mismatch
     signatures, so the normal c1/c4/basebackup validation still did not hit the
     overflow diagnostic. Process scans showed only PostgreSQL plus the two Homer
     services, not leftover pgbench, pg_basebackup, or remote-exec backends.
   - Fifty-first scheduler-ready slice: selected completion-ring grants now
     validate their fixed session-table owner and generation before consuming a
     backend completion mailbox. This is intentionally narrower than the rejected
     completion-backlog feedback patch above: it does not publish backlog hints,
     reread mailbox depth during feedback update, or change completion-source
     policy. It only turns the generation stamped by
     [`HomerCompletionRingProgressSourceRef()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3576)
     and the readiness observed by
     [`HomerServiceCompletionRingSourceReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3628)
     into an executor invariant in
     [`HomerServiceExecuteCompletionProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4653).
     Non-aggregate completion grants now reject a missing session table, an
     out-of-range session index, or a mismatched owner pointer at the
     `invalid completion session grant` diagnostic, then reject inactive or
     generation-mismatched slots at the `stale completion grant` diagnostic
     before calling
     [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4725).
     This preserves the current mailbox consumption semantics while preventing a
     future reordered policy plan from accidentally consuming a recycled session
     slot. Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check`, installed the service binary on farnet1 and farnet0 with
     matching SHA-256
     `39456035973ac7e0b02c0342e0eeb560732ea5529ac154ca5afc066734e35aa5`, and
     restarted both Homer services with service CPU `2` and backend CPU set
     `4,5,6,7`. The non-sudo local install attempt failed with a prefix
     permission error, so the accepted install used `sudo -n make
     install-service-bin`; the usual Git safe-directory version-stamping warnings
     appeared, but the install completed and the installed hashes matched. The
     timeout-bounded remote c1 smoke completed `1000/1000` with zero failures at
     `446.848689 TPS`, p50 `0.220 ms`, p95 `0.232 ms`, p99 `0.336 ms`, and
     cold-tail max `2013.854 ms`. The remote c4 warmup completed `4000/4000`
     with zero failures at `10249.393364 TPS`, p50 `0.360 ms`, p95 `0.515 ms`,
     p99 `0.624 ms`, and max `15.698 ms`. Two warmed remote c4 repeats completed
     `40000/40000` with zero failures: first `10975.737583 TPS`, p50
     `0.350 ms`, p95 `0.494 ms`, p99 `0.581 ms`, max `16.582 ms`; second
     `10927.397280 TPS`, p50 `0.351 ms`, p95 `0.495 ms`, p99 `0.590 ms`, max
     `17.151 ms`. Remote RDMA basebackup completed at `6.21s` for the first
     post-restart run and `4.47s` warm. Service logs on both farnet1 and farnet0
     had no
     failed/invalid/underflow/overrun/overflow/broken/error/stale/checkpoint/mismatch
     signatures, so normal validation did not hit the new stale/invalid
     completion diagnostics. Process scans showed only PostgreSQL plus the two
     Homer services, not leftover pgbench, pg_basebackup, or remote-exec
     backends.
   - Fifty-second scheduler-ready slice: target-completion waits now carry a
     stamped scheduler source ref and validate it before consuming the target
     mailbox. The target wait path is still a synchronous control-path helper,
     but
     [`HomerTargetCompletionProgressSourceRef()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3603)
     now stamps the target session generation and records the fixed session-table
     index when the target pointer matches a bounded scan of `sessionStates`.
     [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3873)
     uses that helper for `TUPLE_SINK_SERVICE_PUMP_TARGET_COMPLETION`, and
     [`HomerServiceExecuteProgressSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18767)
     now rejects a missing target, mismatched owner, mismatched table index,
     inactive target, or generation mismatch at the `stale target-completion
     grant` diagnostic before calling the completion mailbox consumer. This keeps
     the startup wait's mailbox semantics unchanged while preventing a future
     reordered source plan from consuming a recycled target session slot.
     Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check`, installed the service binary on farnet1 and farnet0 with
     matching SHA-256
     `2d734cdd1ff15550a06acf710a939ebdbad6a2710f320fd7599024198533eaef`, and
     restarted both Homer services with service CPU `2` and backend CPU set
     `4,5,6,7`. The timeout-bounded remote c1 smoke completed `1000/1000` with
     zero failures at `446.891423 TPS`, p50 `0.219 ms`, p95 `0.232 ms`, p99
     `0.342 ms`, and cold-tail max `2013.257 ms`. The remote c4 warmup completed
     `4000/4000` with zero failures at `10281.322692 TPS`, p50 `0.358 ms`, p95
     `0.515 ms`, p99 `0.629 ms`, and max `17.599 ms`. Two warmed remote c4
     repeats completed `40000/40000` with zero failures: first `10868.643745 TPS`,
     p50 `0.355 ms`, p95 `0.500 ms`, p99 `0.591 ms`, max `15.865 ms`; second
     `10867.002022 TPS`, p50 `0.354 ms`, p95 `0.497 ms`, p99 `0.593 ms`, max
     `15.783 ms`. Remote RDMA basebackup completed at `5.51s` for the first
     post-restart run, `4.64s` for the next warm sample, and `4.41s` for an extra
     warm confirmation run because `4.64s` was slightly above the recent watch
     band; the extra sample keeps this classified as variance rather than a clear
     regression. Service logs on both farnet1 and farnet0 had no
     failed/invalid/underflow/overrun/overflow/broken/error/stale/checkpoint/mismatch
     signatures, so normal validation did not hit the new stale target-completion
     diagnostic. Process scans showed only PostgreSQL plus the two Homer services,
     not leftover pgbench, pg_basebackup, or remote-exec backends.
   - Fifty-third scheduler-ready slice: the service-progress policy surface now
     has an opt-in feedback-aware ordering policy,
     `HOMER_PROGRESS_POLICY=unblocked-first`. The default remains fixed-priority,
     but
     [`HomerProgressPolicyKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1250)
     now includes `HOMER_PROGRESS_POLICY_UNBLOCKED_FIRST`, and
     [`HomerServiceReadProgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3022)
     accepts `unblocked-first`, `feedback-unblocked`, or `unblocked`.
     [`HomerServiceProgressSourceFeedbackBlocked()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3558)
     reads only fixed-table source-core feedback (`creditBlocked`,
     `resourcePressure`, and `sourceReleaseBlocked`), and
     [`HomerServiceBuildUnblockedFirstProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3579)
     performs two stable passes over the already-built ready set: prior-unblocked
     sources first, prior-blocked/pressured sources second. This is a scheduling
     hint, not a correctness filter: every ready source is still appended, no
     owner tables are rescanned, and no heap work items are allocated.
     [`HomerServiceBuildProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3620)
     dispatches the new policy while preserving fixed-priority as the default.
     Validation rebuilt with `make -j8 service-bin client-bin`, passed
     `git diff --check`, installed the service binary on farnet1 and farnet0 with
     matching SHA-256
     `dcc04d7d740e5f8e9ea06efb929e446eaf616c046016fd0d01ee37686b6b21f5`, and
     restarted both Homer services with service CPU `2` and backend CPU set
     `4,5,6,7`. Default fixed-priority validation completed c1 `1000/1000` with
     zero failures at `446.383933 TPS`, p50 `0.220 ms`, p95 `0.234 ms`, p99
     `0.345 ms`, max `2014.549 ms`; c4 warmup `4000/4000` with zero failures at
     `10295.294793 TPS`, p50 `0.358 ms`, p95 `0.516 ms`, p99 `0.626 ms`, max
     `15.989 ms`; warmed c4 repeats `10879.794589 TPS` and `10850.876941 TPS`
     with zero failures; and remote RDMA basebackup `6.25s` first / `4.49s`
     warm. Opt-in `HOMER_PROGRESS_POLICY=unblocked-first` was confirmed in both
     service process environments, then validated with c1 `1000/1000` zero
     failures at `446.219804 TPS`, p50 `0.221 ms`, p95 `0.236 ms`, p99
     `0.348 ms`, max `2014.999 ms`; c4 warmup `4000/4000` zero failures at
     `10239.816502 TPS`, p50 `0.359 ms`, p95 `0.499 ms`, p99 `0.607 ms`, max
     `17.547 ms`; warmed c4 repeats `10870.017150 TPS` and `10903.110058 TPS`
     with zero failures; and remote RDMA basebackup `6.39s` first / `4.42s`
     warm. Service logs on both farnet1 and farnet0 had no
     failed/invalid/underflow/overrun/overflow/broken/error/stale/checkpoint/mismatch
     signatures under the opt-in policy. The services were then restarted back to
     default fixed-priority, both installed hashes still matched, both service
     PIDs were pinned to CPU `2`, and `/proc/<pid>/environ` showed no
     `HOMER_PROGRESS_POLICY` override.
   - Rejected traffic-class follow-up after the fifty-third scheduler-ready
     slice: two related source-core/policy experiments were tested and backed
     out, so the current code keeps the accepted `unblocked-first` opt-in policy
     but does not carry a landed traffic-class policy or payload source-core
     traffic-class refresh. First, a payload source-core traffic-class metadata
     refresh built as SHA-256
     `a44c19374371f6a7758aeeb65947d635b07b13580764e3a32ac090a0f8133d8e`
     initially looked acceptable: c1 `445.432314 TPS`, c4 warmup
     `10086.567970 TPS`, warmed c4 `10860.897159 TPS` and `10827.461691 TPS`,
     and remote RDMA basebackup `6.03s` first / `4.26s` warm, with clean service
     logs and process scans. After a later traffic-class policy attempt and
     rollback, however, recovery on that metadata-refresh binary was mixed for
     default fixed-priority c4, with warmed repeats `10933.021305 TPS`,
     `10711.406068 TPS`, and `10723.613343 TPS`; that was not clean enough for
     a no-regression scheduler milestone, so the metadata refresh was backed out
     as well. Second, an opt-in `HOMER_PROGRESS_POLICY=traffic-class` policy
     attempt built as SHA-256
     `5747be3c4ef7e945ad7f7a9ac4c057941cc2aed2338b0eef991973af0dcdc92a`
     never reached opt-in validation because even default fixed-priority
     validation on that binary produced mixed c4 repeats: `10739.673535 TPS`,
     `10821.922293 TPS`, `10704.245705 TPS`, and `10712.524843 TPS`. The code was
     restored to the accepted fifty-third-slice binary SHA-256
     `dcc04d7d740e5f8e9ea06efb929e446eaf616c046016fd0d01ee37686b6b21f5`,
     where recovery validation completed c1 at `445.695363 TPS`, c4 warmup at
     `10450.030566 TPS`, warmed c4 at `10940.093144 TPS` and `10826.321711 TPS`,
     and remote RDMA basebackup at `6.61s` first / `4.22s` warm. Service logs on
     both farnet1 and farnet0 again had no
     failed/invalid/underflow/overrun/overflow/broken/error/stale/checkpoint/mismatch
     signatures, and process scans showed only PostgreSQL plus the two Homer
     services. Treat traffic-class policy and source-core traffic-class refresh
     as future work that needs cleaner isolation and warmed default-path
     validation before it can be reattempted.
   - Rejected source-release/resource-pressure feedback cleanup after the
     traffic-class rollback: a narrow semantic cleanup removed the generic
     `blockedOnLocalResources -> sourceReleaseBlocked` fold at
     [`HomerServiceUpdateProgressFeedback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4214)
     so source-release pressure would only come from the payload and command
     source-specific branches. That was conceptually cleaner, but not clean
     enough to land: the rebuilt binary SHA-256
     `5cf5e99db9e8cc174e63078ebf894795fa5bb2deb13f326287f94b9189fdb110`
     passed c1 correctness (`1000/1000`, zero failures, `443.541984 TPS`,
     p99 `0.344 ms`) but warmed c4 repeats stayed mixed/low at
     `10812.593428 TPS`, `10643.953888 TPS`, and `10626.961505 TPS`. The cleanup
     was backed out and the service binary returned to the accepted
     `dcc04d7d740e5f8e9ea06efb929e446eaf616c046016fd0d01ee37686b6b21f5`
     hash. Recovery validation on the accepted hash showed c1 `1000/1000`, zero
     failures, `444.018778 TPS`, p99 `0.340 ms`; c4 warmup `10265.254168 TPS`;
     restored c4 repeats `10788.842395 TPS`, `10579.625968 TPS`, and after a
     clean pgbench table reset `10620.581838 TPS`. A longer accepted-hash c4
     baseline after table cleanup completed `200000/200000` with zero failures at
     `10690.737075 TPS`, p50 `0.356 ms`, p95 `0.510 ms`, p99 `0.599 ms`, and
     max `15.876 ms`, confirming the current steady band was lower than the
     earlier `10.8-10.9k TPS` samples even after the cleanup was backed out.
     Remote RDMA basebackup using the correct farnet0 receiver target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` completed
     `6.45s` first / `4.30s` warm. Process scans were clean, and service-log
     tails had no failed/invalid/underflow/overrun/overflow/broken/error/
     checkpoint/timeout/could-not/fatal/segmentation/mismatch signatures. Treat
     this cleanup as future work that needs a stronger isolated explanation for
     the c4 band before reattempting.
   - Accepted fifty-fourth scheduler-ready slice: the unused legacy local
     START/CLOSE compatibility wait
     [`TupleSinkServiceWaitForLocalCommandStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8499)
     now derives mailbox idleness from a stack-local
     [`completionProgressResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8516)
     and
     [`HomerProgressResultMade()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8534)
     after calling
     [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8533),
     rather than consuming that mailbox with a bool-shaped progress return. This
     helper is marked `__attribute__((unused))`, so this is a boundary cleanup
     for the old local in-place control path, not a change to the measured remote
     pgbench service loop. The rebuilt service binary SHA-256 was
     `7b08b02c18a46b9ee902ff1d01ea74e052b13270f0f32ec2f7ef0bb097ba1fad` on both
     farnet1 and farnet0 after deployment. Validation completed c1 remote Homer
     pgbench at `1000/1000`, zero failures, `443.199743 TPS`, p50 `0.228 ms`,
     p95 `0.244 ms`, p99 `0.348 ms`; c4 warmup at `40000/40000`, zero failures,
     `10882.144740 TPS`, p50 `0.350 ms`, p95 `0.502 ms`, p99 `0.608 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10755.660435 TPS`, p50
     `0.354 ms`, p95 `0.500 ms`, p99 `0.607 ms`. Basebackup validation initially
     hit PostgreSQL `CheckpointStart` waits with leftover walsender cleanup
     needed after a client timeout, so those attempts are not performance
     samples. After cleanup, remote RDMA basebackup with `--checkpoint=fast`
     completed in `5.97s`, and the original default-checkpoint command with
     target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` completed
     warmed in `4.13s`. Final process scans showed only PostgreSQL plus the two
     Homer services, not leftover pgbench, pg_basebackup, remote-exec backend, or
     walsender processes. Homer service logs on both farnet1 and farnet0 had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures. PostgreSQL logs still
     showed maintenance-daemon remote-node warnings and the expected checkpoint
     lines around basebackup; those are separate from Homer service progress.
   - Accepted fifty-fifth scheduler-ready slice: the aggregate completion-ring
     scan in
     [`TupleSinkServiceConsumeAllCompletionMailboxes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7753)
     now gives each call to
     [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7798)
     a stack-local
     [`mailboxProgressResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7790)
     and increments `consumedCompletionCount` only when
     [`HomerProgressResultMade()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7800)
     reports typed progress. This does not change the direct client SQL hot path:
     direct frontend/backend client SQL sessions are still skipped before the
     aggregate scan so the service does not race the frontend-owned completion
     ring. The point is to keep aggregate completion feedback on the same typed
     result path as selected completion grants instead of using the mailbox
     helper's bool return as the scheduler-progress fact. The rebuilt and
     deployed service binary SHA-256 was
     `da05cfe75449b486c97e897634609e0217342807cd698e42b0d6b19cd60edbf9` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `442.957878 TPS`, p50 `0.227 ms`, p95
     `0.244 ms`, p99 `0.360 ms`; c4 warmup at `40000/40000`, zero failures,
     `10907.620364 TPS`, p50 `0.349 ms`, p95 `0.493 ms`, p99 `0.591 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10852.746410 TPS`, p50
     `0.350 ms`, p95 `0.502 ms`, p99 `0.608 ms`. A default-checkpoint
     basebackup sanity attempt again timed out before Homer transfer while
     PostgreSQL waited on checkpoint completion and left a walsender that had to
     be killed, so that attempt is not a Homer performance sample. A bounded
     remote RDMA basebackup target-path sanity run with `--checkpoint=fast` and
     target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` completed
     in `6.41s`. Final process scans showed only PostgreSQL plus the two Homer
     services, not leftover pgbench, pg_basebackup, remote-exec backend, or
     walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted fifty-sixth scheduler-ready slice: the backend completion mailbox
     helper
     [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7598)
     is now a result-only progress API. Its function contract explicitly says it
     consumes at most one backend completion and reports progress exclusively
     through `HomerProgressResult`; normal no-work and missing-mailbox cases
     return without marking progress
     ([contract comment](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7590)).
     Consuming a completion still marks exactly one item through
     [`HomerProgressResultMarkItem()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7672),
     and the aggregate scan continues to use a stack-local
     [`mailboxProgressResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7798)
     around each mailbox consume
     ([call site](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7806)).
     This removes the helper's bool return entirely; all callers already ignored
     that return or derived idleness from `HomerProgressResult`, so the behavioral
     surface is unchanged and direct frontend/backend client SQL sessions remain
     skipped before the aggregate scan. The rebuilt and deployed service binary
     SHA-256 was
     `d81da3fdc79c893edfb63695668bc746550cb221bfc6e1329e70c90d664284ca` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `442.691974 TPS`, p50 `0.228 ms`, p95
     `0.243 ms`, p99 `0.360 ms`; c4 warmup at `40000/40000`, zero failures,
     `10854.722564 TPS`, p50 `0.350 ms`, p95 `0.502 ms`, p99 `0.607 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10724.956088 TPS`, p50
     `0.350 ms`, p95 `0.500 ms`, p99 `0.607 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `6.17s`; this is a correctness guardrail rather than a
     default-checkpoint performance sample. Final process scans showed only
     PostgreSQL plus the two Homer services, not leftover pgbench,
     pg_basebackup, remote-exec backend, or walsender processes, and Homer
     service logs on both hosts had no failed/invalid/underflow/overrun/overflow/
     broken/error/checkpoint/timeout/could-not/fatal/segmentation/mismatch
     signatures.
   - Accepted fifty-seventh scheduler-ready slice: the public tagged send-CQ
     drain API
     [`TupleSinkServiceDrainPeerTaggedSendCompletionsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1666)
     no longer exposes an unused `bool *madeProgress` out-parameter. The exported
     declaration in
     [`remote_execution_peer_transport_rdma.h`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:606)
     now reports scheduler-visible send-CQ progress only through `cqesDrained`,
     while preserving the existing CQE count at the public scheduler/RDMA boundary
     ([internal bridge](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1694)).
     The only service caller,
     [`HomerServiceDrainOnePeerSendCq()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4502),
     already derived progress from `cqesDrained` before merging it into
     `HomerProgressResult`, so this is an API cleanup at the scheduler/RDMA
     boundary rather than a hot-path behavior change
     ([call site](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4529)).
     The rebuilt and deployed service binary SHA-256 was
     `bfebcdf566b61c193ce0bb7694fac8307689b9e67ac2b6dfd05e8974f0fddb68` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `442.500589 TPS`, p50 `0.228 ms`, p95
     `0.240 ms`, p99 `0.351 ms`; c4 warmup at `40000/40000`, zero failures,
     `10900.233974 TPS`, p50 `0.349 ms`, p95 `0.499 ms`, p99 `0.595 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10814.274301 TPS`, p50
     `0.353 ms`, p95 `0.493 ms`, p99 `0.597 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `5.52s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted fifty-eighth scheduler-ready slice: the internal tagged send-CQ
     drain helpers now use CQE count as their progress fact too.
     [`TupleSinkServiceDrainTaggedSendCompletionsInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1580)
     no longer has a `bool *madeProgress` out-parameter; it increments
     `cqesDrained` once for each handled tagged CQE
     ([increment site](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1645)).
     The lane-local wrapper
     [`TupleSinkServiceDrainTaggedSendCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1653)
     now exposes only that count. Async peer-control start/poll paths still
     opportunistically drain send CQEs for transport health, but they pass
     `NULL` for the count because they do not use a local progress decision there.
     The outgoing peer-control pump remains the scheduler-visible consumer: it
     requests `sendCqesDrained`, then records `progress->sendCompletions` only
     when the count is nonzero
     ([peer pump call site](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7510)).
     The rebuilt and deployed service binary SHA-256 was
     `71c1ca2088e49562b83b6243b1e37e2d031d2de0812212013944f37cd71411ea` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `441.952813 TPS`, p50 `0.230 ms`, p95
     `0.244 ms`, p99 `0.357 ms`; c4 warmup at `40000/40000`, zero failures,
     `10945.035400 TPS`, p50 `0.348 ms`, p95 `0.492 ms`, p99 `0.582 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10720.095623 TPS`, p50
     `0.354 ms`, p95 `0.499 ms`, p99 `0.596 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `6.12s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted fifty-ninth scheduler-ready slice: persistent peer event drains now
     report event counts instead of bool progress. The public
     [`TupleSinkServiceDrainPeerConnectionEventsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3619)
     wrapper and the command-doorbell variant now require a `uint16_t
     *eventsDrained` output, zero it on entry, and pass it into the shared event
     drain helper
     ([event wrapper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3620),
     [command-doorbell wrapper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3639)).
     The shared
     [`TupleSinkServiceDrainPeerConnectionEvents()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3675)
     increments that count for handled recv-CQ doorbells and CM events rather
     than setting one coarse bool
     ([doorbell count](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3864),
     [CM count](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3962)).
     Callers that only drain for transport health pass `NULL`; the scheduler
     peer pump requests the count and adds it to `progress->eventDrains` for both
     outgoing and incoming connections
     ([outgoing peer pump](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7457)).
     The rebuilt and deployed service binary SHA-256 was
     `a4f2ef6d3d81fbfd1d8b07e72e2057097093bc1b317d056069ee40ad91ffb29d` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `442.010245 TPS`, p50 `0.229 ms`, p95
     `0.244 ms`, p99 `0.358 ms`; c4 warmup at `40000/40000`, zero failures,
     `10671.381995 TPS`, p50 `0.358 ms`, p95 `0.500 ms`, p99 `0.613 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10846.686961 TPS`, p50
     `0.351 ms`, p95 `0.492 ms`, p99 `0.604 ms`. The lower c4 warmup stayed
     within restart/warmup noise and the warmed repeat remained in the current
     accepted band. Remote RDMA basebackup target-path sanity used
     `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `6.18s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted sixtieth scheduler-ready slice: outgoing peer-control response
     consumption now reports scheduler-visible progress only through the exact
     response count. The static
     [`TupleSinkServiceProgressPeerControlResponses()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6772)
     helper still returns `bool` for success/failure, but no longer accepts a
     `bool *madeProgress` out-parameter; after each
     [`TupleSinkServiceCompletePeerControlOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6818)
     success it increments `responsesConsumed` directly
     ([mark site](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6826)).
     The async start and poll paths keep opportunistically draining responses for
     transport health and pass `NULL`
     ([start path](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7024),
     [poll path](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7168)).
     The scheduler peer pump now requests `responsesConsumed` and adds that count
     to `progress->controlResponses` only when it is nonzero, removing the prior
     coarse `responseProgress` fallback
     ([peer pump](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7469)).
     The rebuilt and deployed service binary SHA-256 was
     `6d2ca3db0357e3f54a62c7dd0904dcf6128ea132c249ec727fde3a59b7c41a81` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `441.644029 TPS`, p50 `0.231 ms`, p95
     `0.244 ms`, p99 `0.363 ms`; c4 warmup at `40000/40000`, zero failures,
     `10827.974614 TPS`, p50 `0.352 ms`, p95 `0.501 ms`, p99 `0.594 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10759.791949 TPS`, p50
     `0.354 ms`, p95 `0.494 ms`, p99 `0.597 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `5.47s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted sixty-first scheduler-ready slice: RDMA connection setup progress
     now reports setup-step counts instead of bool progress. The active-side
     [`TupleSinkServiceProgressOutgoingPeerConnectionSetup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2900)
     and passive-side
     [`TupleSinkServiceProgressIncomingPeerConnectionSetup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3129)
     helpers still return `bool` for success/failure, but they accept `uint16_t
     *setupSteps` and increment it for each completed setup transition or
     bootstrap CQE
     ([outgoing transition mark](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2951),
     [outgoing bootstrap CQE marks](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3068),
     [incoming transition mark](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3179)).
     The shared
     [`TupleSinkServiceProgressPeerConnectionSetup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3303)
     wrapper forwards the optional count to the incoming or outgoing state
     machine. Async connection start/progress still passes `NULL` because it only
     needs transport-health readiness
     ([async call](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6846)).
     The scheduler peer pump now requests `setupSteps` for both outgoing and
     incoming inactive-bootstrap lanes and adds the exact count to
     `progress->setupSteps`
     ([outgoing pump](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7211),
     [incoming pump](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7257)).
     The rebuilt and deployed service binary SHA-256 was
     `f4912b76a82f2b75dbf31a1f2e96ad10eb554f8a4c8b64605099a55aa9308f70` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `441.457055 TPS`, p50 `0.230 ms`, p95
     `0.245 ms`, p99 `0.350 ms`; c4 warmup at `40000/40000`, zero failures,
     `10862.327601 TPS`, p50 `0.349 ms`, p95 `0.500 ms`, p99 `0.597 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10769.975140 TPS`, p50
     `0.354 ms`, p95 `0.503 ms`, p99 `0.597 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `5.56s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted sixty-second scheduler-ready slice: the incoming peer-control
     mailbox pump now reports request/response work only through exact counters.
     [`TupleSinkServicePumpIncomingPeerMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4608)
     still returns `bool` for success/failure, but no longer accepts a `bool
     *madeProgress` out-parameter. Response messages mark `responsesConsumed`
     after completing the peer-control op
     ([response mark](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4653)),
     and request messages mark `requestsDispatched` after dispatching the
     mailbox request
     ([request mark](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4678)).
     The scheduler peer pump now adds `progress->controlResponses` and
     `progress->controlRequests` independently when those counts are nonzero
     ([peer pump](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7465)).
     This removes the last `madeProgress`/`mailboxProgress` reference from
     `remote_execution_peer_transport_rdma.c` and `.h`; the remaining scheduler
     work is no longer this RDMA file's bool-shaped progress plumbing. The
     rebuilt and deployed service binary SHA-256 was
     `fc4b1bfc829dfae3d3595f035401d182983cdfcffb8f8b6bb7aed106306aa728` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `441.734355 TPS`, p50 `0.229 ms`, p95
     `0.244 ms`, p99 `0.345 ms`; c4 warmup at `40000/40000`, zero failures,
     `10862.401346 TPS`, p50 `0.350 ms`, p95 `0.498 ms`, p99 `0.598 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10895.887075 TPS`, p50
     `0.349 ms`, p95 `0.491 ms`, p99 `0.596 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `5.50s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted sixty-third scheduler-ready slice: the compatibility-session close
     flush loop now uses a pass-local `HomerProgressResult` instead of a separate
     `madeProgress` bool. The helper
     [`TupleSinkServiceFlushIdleSessionSinksForCompatibilityClose()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15234)
     still runs only on the local compatibility close path, not the hot
     service-loop scheduler boundary, but its bounded retry decision now follows
     the same scheduler progress vocabulary: each payload pump result is merged
     into `passProgressResult`, pump errors and local reclaim work mark an item,
     and the loop stops on `!HomerProgressResultMade(&passProgressResult)` or an
     empty `activeSinkCount`
     ([pass result](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15250),
     [merge site](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15273),
     [retry gate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15283)).
     The rebuilt and deployed service binary SHA-256 was
     `88fd789e3e60d347e72b4bf5c9ad3cc37d7f1880bb55cd2106703935332de505` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `441.196260 TPS`, p50 `0.229 ms`, p95
     `0.243 ms`, p99 `0.364 ms`; c4 warmup at `40000/40000`, zero failures,
     `10843.605390 TPS`, p50 `0.350 ms`, p95 `0.499 ms`, p99 `0.596 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10780.375189 TPS`, p50
     `0.352 ms`, p95 `0.505 ms`, p99 `0.594 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `6.37s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Rejected follow-up after the sixty-third scheduler-ready slice: an opt-in
     `HOMER_PROGRESS_POLICY=ready-hints-first` experiment was attempted but not
     landed. The experiment prioritized sources with ready-object, ready-byte, or
     signaled-CQE hints ahead of coarse ready sources; c1 remote Homer pgbench
     then hung beyond the normal timeout with the service processes, remote
     pgbench, ssh wrapper, and a local remote-exec backend still live, and the
     logs showed setup/doorbell traffic rather than an explicit crash. That
     failure indicates this ordering heuristic can starve required setup or
     completion work and needs a separate readiness/safety design before any
     retry. The code was removed: the current
     [`HomerServiceReadProgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3022)
     accepts only fixed-priority, round-robin, and unblocked-first progress
     policies, and
     [`HomerServiceBuildProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3620)
     dispatches only those three policies. After removal, the rebuilt local
     service binary SHA-256 returned to the accepted
     `88fd789e3e60d347e72b4bf5c9ad3cc37d7f1880bb55cd2106703935332de505`; the
     same binary was installed on farnet1 and farnet0. Cleanup validation
     completed c1 remote Homer pgbench at `1000/1000`, zero failures,
     `440.980883 TPS`, p50 `0.228 ms`, p95 `0.242 ms`, p99 `0.358 ms`; c4
     warmup at `40000/40000`, zero failures, `10816.350536 TPS`, p50
     `0.350 ms`, p95 `0.498 ms`, p99 `0.603 ms`; and a warmed c4 repeat at
     `40000/40000`, zero failures, `10824.244576 TPS`, p50 `0.351 ms`, p95
     `0.497 ms`, p99 `0.580 ms`. Remote RDMA basebackup target-path sanity used
     `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `5.49s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted sixty-fourth scheduler-ready slice: the live service wait/idle
     loops no longer keep a separate `madeProgress` bool derived from
     `HomerProgressResult`. The peer command startup wait now uses the
     pass-local result object directly for its idle decision
     ([startup wait](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8428),
     [idle test](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8486)).
     The legacy local command startup wait now uses one stack-local
     `waitProgressResult`, consumes the command completion mailbox into it, and
     merges the periodic background pump result before deciding to relax
     ([local wait](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8510),
     [merge site](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8573),
     [idle test](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8621)).
     The main service loop also tests
     `HomerProgressResultMade(&progressResult)` directly instead of caching it
     into a bool
     ([main-loop idle test](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19095)).
     This is mechanically small and not a hot-path data-movement optimization,
     but it removes the last live `madeProgress` local from the scheduler-facing
     service process; the remaining `madeProgress` spelling is only in the
     preserved commented-out Phase-1 payload mover reference block. The rebuilt
     and deployed service binary SHA-256 was
     `a5079f96e521f16265c93bab2f7450f47a8eaf18da8fef74727e44f715195c3a` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `440.665105 TPS`, p50 `0.230 ms`, p95
     `0.243 ms`, p99 `0.358 ms`; c4 warmup at `40000/40000`, zero failures,
     `10903.585590 TPS`, p50 `0.349 ms`, p95 `0.499 ms`, p99 `0.596 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10848.031286 TPS`, p50
     `0.350 ms`, p95 `0.496 ms`, p99 `0.606 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `5.47s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted sixty-fifth scheduler-ready slice: payload-delta merging no longer
     uses `HomerPayloadProgressDeltaMade()` to collapse rich payload facts back
     into one generic predicate. The replacement
     [`HomerPayloadProgressDeltaItemCount()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3191)
     maps counted transport facts, doorbell consumption, local publication, and
     frontier-only progress into a bounded scheduler item count. The merge path
     then passes that count directly to `HomerProgressResultMarkItems()`
     ([merge site](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3272)).
     This keeps payload idleness and `lastItemsProcessed` feedback driven by
     explicit facts instead of a payload-specific made-progress bool. The rebuilt
     and deployed service binary SHA-256 was
     `bffbd4a01c9627be0476ac91ad855554037d5f14f1b4bd20d37276cbb944ff2b` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `440.472751 TPS`, p50 `0.230 ms`, p95
     `0.244 ms`, p99 `0.358 ms`; c4 warmup at `40000/40000`, zero failures,
     `10814.306462 TPS`, p50 `0.350 ms`, p95 `0.500 ms`, p99 `0.606 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10783.269757 TPS`, p50
     `0.353 ms`, p95 `0.496 ms`, p99 `0.603 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `5.88s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted sixty-sixth scheduler-ready slice: owner-polled payload send and
     receiver-head-ACK completion polling now reports counted completions instead
     of a `bool *completionFound` out-parameter. The public RDMA helpers
     [`TupleSinkServicePollPeerPayloadSendCompletionRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:565)
     and
     [`TupleSinkServicePollPeerReceiverHeadAckCompletionRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:576)
     now return `uint16_t *completionsDrained` plus the latest completed frontier.
     The shared RDMA implementation initializes and increments that count while
     still stashing completions for other owners
     ([implementation](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6119),
     [match count](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6255)).
     Payload apply helpers now accept `completionCount`, and
     [`HomerServicePayloadProgressMarkTransportCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5088)
     adds that count to `cqesDrained` instead of assuming one CQE per poll
     ([tracked apply](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5377),
     [sequence apply](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5442),
     [ACK apply](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14012)).
     Fixed-slot sender diagnostics also add `completionsDrained` to
     `senderCompletionCqes` instead of incrementing by one. Command send
     completion polling is intentionally unchanged in this slice. The rebuilt and
     deployed service binary SHA-256 was
     `9b3e648f72cd723facb686015fe257956463ad846c0e45f0b6d49e81beafd59d` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `440.475661 TPS`, p50 `0.228 ms`, p95
     `0.242 ms`, p99 `0.353 ms`; c4 warmup at `40000/40000`, zero failures,
     `10837.567887 TPS`, p50 `0.351 ms`, p95 `0.496 ms`, p99 `0.590 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10891.875804 TPS`, p50
     `0.349 ms`, p95 `0.492 ms`, p99 `0.584 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `5.52s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted sixty-seventh scheduler-ready slice: owner-polled command send
     completion polling no longer uses a `bool *completionFound` out-parameter.
     The RDMA header now defines
     [`TupleSinkServicePeerCommandCompletionPollResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:587)
     with `completionsAvailable`, `cqesDrained`, and `outstandingIndex`, and
     [`TupleSinkServicePollPeerCommandSendCompletionRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:600)
     fills that result. A completion popped from the RDMA stash reports
     `completionsAvailable=1` with `cqesDrained=0`, while a send-CQ poll that
     consumes command CQEs reports the CQE count immediately and returns one
     outstanding index for source-slot retirement
     ([implementation](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6306),
     [stash pop](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6331),
     [first CQE](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6429),
     [extra CQEs](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6452)).
     The service caller now adds only the result's `cqesDrained` to
     `commandDelta->cqesDrained` before retiring source slots through
     `completionPollResult.outstandingIndex`
     ([caller](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2483),
     [CQE accounting](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2504),
     [slot retirement](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2510)).
     This fixes the older attribution where a stashed completion pop could be
     counted like a fresh CQE drain, and where multiple command CQEs consumed in
     one send-CQ batch were reduced to one bool at the service boundary. The
     rebuilt and deployed service binary SHA-256 was
     `d8d43502d800b77edc0a6275868fc71771e896731ec7b6328d18dcbdc773636b` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `440.303246 TPS`, p50 `0.228 ms`, p95
     `0.243 ms`, p99 `0.360 ms`; c4 warmup at `40000/40000`, zero failures,
     `10819.179592 TPS`, p50 `0.351 ms`, p95 `0.510 ms`, p99 `0.601 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10883.814734 TPS`, p50
     `0.349 ms`, p95 `0.494 ms`, p99 `0.593 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608` and
     completed in `5.44s`. Final process scans showed only PostgreSQL plus the
     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
     backend, or walsender processes, and Homer service logs on both hosts had no
     failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
     could-not/fatal/segmentation/mismatch signatures.
   - Accepted sixty-eighth scheduler-ready slice: basebackup byte-ring receive
     reassembly now reports semantic-object completion as a count instead of a
     bool-shaped `objectCompleted` flag. The helper
     [`HomerServiceProcessReceivedBaseBackupTransportRecord()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12107)
     now takes `uint16_t *objectsCompleted` and sets it to `1` only when a whole
     basebackup object or final reassembled fragment completes
     ([output setup](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12116),
     [whole record](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12165),
     [FIRST|LAST fragment](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12244),
     [LAST fragment](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12324)).
     The receiver loop carries `objectsCompleted` through stats and semantic
     progress instead of testing a bool
     ([caller](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14568),
     [stats](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14601),
     [semantic progress](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14606)).
     The terminal `baseBackupEnd` flag remains a terminal condition, not a
     scheduler progress fact. The rebuilt and deployed service binary SHA-256 was
     `b7118bcfe8eca26f80a809ad8fa0b9f21ba68ecee24e5fb7efaa690e23b7b7b0` on both
     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
     `1000/1000`, zero failures, `440.425998 TPS`, p50 `0.227 ms`, p95
     `0.242 ms`, p99 `0.357 ms`; c4 warmup at `40000/40000`, zero failures,
     `10794.793671 TPS`, p50 `0.353 ms`, p95 `0.502 ms`, p99 `0.593 ms`; and a
     warmed c4 repeat at `40000/40000`, zero failures, `10845.316504 TPS`, p50
     `0.350 ms`, p95 `0.492 ms`, p99 `0.594 ms`. Remote RDMA basebackup
     target-path sanity used `--checkpoint=fast` with target
     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608`; the first
     post-restart sample completed in `6.40s`, and the warmed repeat completed
     in `4.24s`, so the higher first sample was treated as checkpoint/warmup
     variation rather than a regression. Final process scans showed only
	     PostgreSQL plus the two Homer services, not leftover pgbench, pg_basebackup,
	     remote-exec backend, or walsender processes, and Homer service logs on both
	     hosts had no failed/invalid/underflow/overrun/overflow/broken/error/
	     checkpoint/timeout/could-not/fatal/segmentation/mismatch signatures.
	   - Accepted sixty-ninth scheduler-ready slice: the internal RDMA setup
	     state-machine poll helpers no longer expose a bool-shaped `completed`
	     output. `TupleSinkServicePollExpectedCmEvent()` now reports
	     `uint16_t *completedEvents`, with the function comment documenting that the
	     count is not itself the scheduler progress fact
	     ([helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:881),
	     [zero/init](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:892),
	     [event count](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:939)).
	     `TupleSinkServicePollCompletionOnce()` likewise reports
	     `uint16_t *completionsDrained` for bootstrap CQ polling
	     ([helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:950),
	     [zero/init](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:957),
	     [CQE count](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:995)).
	     The active and passive setup state machines now test those counts locally
	     before applying a state transition or bootstrap CQE and still publish the
	     scheduler-visible `setupSteps` accounting only after the transition/CQE has
	     been applied
	     ([active setup locals](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2898),
	     [active CM count gate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2922),
	     [active CQE count gate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3036),
	     [passive setup locals](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3138),
	     [passive CQE count gate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3173)).
	     This is a cold-path clarity cleanup, not a hot-path policy change. The
	     rebuilt and deployed service binary SHA-256 was
	     `4f80fbe7bf44efc29f4049907756e2bb29ada8b31d257fe481a874b45b386e69` on both
	     farnet1 and farnet0. Validation completed c1 remote Homer pgbench at
	     `1000/1000`, zero failures, `439.773657 TPS`, p50 `0.230 ms`, p95
	     `0.245 ms`, p99 `0.356 ms`; c4 warmup at `40000/40000`, zero failures,
	     `10868.815033 TPS`, p50 `0.351 ms`, p95 `0.501 ms`, p99 `0.597 ms`; and a
	     warmed c4 repeat at `40000/40000`, zero failures, `10903.166525 TPS`, p50
	     `0.348 ms`, p95 `0.495 ms`, p99 `0.581 ms`. Remote RDMA basebackup
	     target-path sanity used `--checkpoint=fast` with target
	     `homer:host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608`; the first
	     post-restart sample completed in `6.10s`, and the warmed repeat completed
	     in `4.28s`, so the higher first sample was again treated as
	     checkpoint/warmup variation rather than a regression. Final hash checks
	     matched on both hosts, final process scans showed only PostgreSQL plus the
	     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
	     backend, or walsender processes, and Homer service logs on both hosts had
	     no failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/
	     timeout/could-not/fatal/segmentation/mismatch signatures.
	   - Rejected follow-up after the sixty-ninth scheduler-ready slice: an opt-in
	     `HOMER_PROGRESS_POLICY=critical-fixed-data-rr` policy was implemented and
	     tested, then removed. The policy attempted to preserve fixed order for
	     dependency-unblocking sources (`CQ_DRAIN`, local/peer control, target
	     completion, completion rings) while round-robining only command and payload
	     data sources. That avoided the earlier `ready-hints-first` c1 hang: opt-in
	     c1 completed `1000/1000` with zero failures at `439.381790 TPS`, p50
	     `0.231 ms`, p95 `0.246 ms`, p99 `0.359 ms`. However c4 performance did not
	     hold the accepted fixed-priority band: opt-in c4 warmup completed
	     `40000/40000` with zero failures at `10785.316654 TPS`, p50 `0.352 ms`,
	     p95 `0.493 ms`, p99 `0.609 ms`; the first warmed repeat completed at
	     `10706.569337 TPS`, p50 `0.355 ms`, p95 `0.493 ms`, p99 `0.614 ms`; and an
	     extra warmed repeat dropped to `10581.349947 TPS`, p50 `0.359 ms`, p95
	     `0.493 ms`, p99 `0.588 ms`. The tested binary SHA-256 was
	     `a48b4460f4812e78a2fb619c7931b3e22b76e01d13ca5c07a31668b9cda1b4ed`.
	     The code was removed; the current policy parser again accepts only
	     fixed-priority, round-robin, and unblocked-first
	     ([parser](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3019),
	     [invalid-policy message](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3055)),
	     and the policy dispatcher only dispatches those three builders
	     ([dispatcher](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3644)).
	     The restored and deployed service binary returned to SHA-256
	     `4f80fbe7bf44efc29f4049907756e2bb29ada8b31d257fe481a874b45b386e69` on both
	     hosts. Recovery validation in default fixed-priority mode completed c1 at
	     `1000/1000`, zero failures, `440.687049 TPS`, p50 `0.223 ms`, p95
	     `0.238 ms`, p99 `0.344 ms`; c4 warmup at `40000/40000`, zero failures,
	     `10839.177231 TPS`, p50 `0.350 ms`, p95 `0.492 ms`, p99 `0.585 ms`; and a
	     warmed c4 repeat at `40000/40000`, zero failures, `10818.737729 TPS`, p50
	     `0.351 ms`, p95 `0.491 ms`, p99 `0.600 ms`. Remote RDMA basebackup restored
	     path completed first in `6.43s` and warmed in `4.46s`. Final hash checks
	     matched on both hosts, final process scans showed only PostgreSQL plus the
	     two Homer services, not leftover pgbench, pg_basebackup, remote-exec
	     backend, or walsender processes, and Homer service logs on both hosts had
	     no failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/
	     timeout/could-not/fatal/segmentation/mismatch signatures. Do not revive
	     this fixed-critical-plus-data-round-robin policy shape as-is; it was correct
	     enough for smoke but still paid a measurable c4 throughput cost.
	   - Accepted seventieth scheduler-ready slice: service-progress policy
	     diagnostics are now available behind a disabled-by-default compile-time
	     switch. `HOMER_SERVICE_PROGRESS_STATS` defaults to `0`
	     ([switch](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:236)),
	     so the normal performance build does not pay extra stores in every pump
	     pass. When explicitly built with
	     `CPPFLAGS=-DHOMER_SERVICE_PROGRESS_STATS=1`,
	     [`HomerServiceProgressStats`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1286)
	     records ready-set builds, empty/overflowed ready sets, per-policy plan
	     builds, per-source ready/plan/grant counts, and per-source grant items,
	     CQEs, WRs, and bytes. The hooks are at ready-set construction
	     ([ready-set hook](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19143)),
	     source-plan construction
	     ([plan hook](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19154)),
	     grant completion
	     ([grant hook](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4413)),
	     and service-exit logging
	     ([log hook](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19268)).
	     The log helpers emit one aggregate `progress_stats` line plus one
	     `progress_source_stats` line per source kind
	     ([logger](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3276)).
	     This intentionally does not add a new policy or change the default
	     fixed-priority path; it provides evidence for the next policy attempt after
	     the rejected `ready-hints-first` and fixed-critical-plus-data-round-robin
	     experiments. Verification compiled the normal default-disabled binary with
	     `make -j8 service-bin client-bin`, then type-checked the gated code by
	     direct diagnostic compile to `/tmp/citus_tuple_sink_service.progress_stats`
	     with SHA-256
	     `bc118c84d2b35f77f56a5989e732d726ee2a48f29f2abcfc831721ae4d6e9f62`.
	     The deployed normal binary SHA-256 was
	     `93008296a75c1bc138fe8371cc43992d575c6f0bc6cd9228bad7e5bc8db7abb3` on both
	     hosts. No-regression validation completed c1 remote Homer pgbench at
	     `1000/1000`, zero failures, `439.528175 TPS`, p50 `0.229 ms`, p95
	     `0.244 ms`, p99 `0.357 ms`; c4 warmup at `40000/40000`, zero failures,
	     `10903.799593 TPS`, p50 `0.349 ms`, p95 `0.488 ms`, p99 `0.584 ms`; and a
	     warmed c4 repeat at `40000/40000`, zero failures, `10890.351584 TPS`, p50
	     `0.350 ms`, p95 `0.485 ms`, p99 `0.584 ms`. Remote RDMA basebackup
	     completed first in `5.60s` and warmed in `4.34s`. Final hash checks matched
	     on both hosts, final process scans showed only PostgreSQL plus the two
	     Homer services, not leftover pgbench, pg_basebackup, remote-exec backend, or
	     walsender processes, and Homer service logs on both hosts had no failed/
	     invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
	     could-not/fatal/segmentation/mismatch signatures.
	   - Diagnostic progress-stats pass after the seventieth slice: the
	     `HOMER_SERVICE_PROGRESS_STATS=1` binary was run from
	     `/tmp/citus_tuple_sink_service.progress_stats` on both farnet services with
	     the default fixed-priority policy, then terminated cleanly to emit the new
	     service-exit counters. This was a diagnostic build, not the no-stats
	     performance baseline. Remote c4 warmup completed `40000/40000`, zero
	     failures, at `6985.814779 TPS`, p50 `0.350 ms`, p95 `0.496 ms`, p99
	     `0.577 ms`, max `2044.302 ms` because it included the expected cold-tail
	     setup outlier. The warmed diagnostic repeat completed `40000/40000`, zero
	     failures, at `10799.343076 TPS`, p50 `0.350 ms`, p95 `0.503 ms`, p99
	     `0.586 ms`, max `16.602 ms`. On farnet1, the progress log showed
	     `ready_sets=246501822`, all fixed-priority plans, no ready-set overflows,
	     peer-control planned/executed every pass with only `89002` productive grants
	     versus `246412820` empty grants, local-control planned/executed every pass
	     with `1700190` productive grants versus `244801632` empty grants,
	     completion-ring `640024/640024` productive grants, payload-stream `148864`
	     productive grants versus `237742` empty grants, and CQ-drain `19058`
	     productive grants versus `287548` empty grants. On farnet0, the log showed
	     `ready_sets=150999245`, command-ring `560024/560024` productive grants,
	     payload-stream `147449` productive grants versus `228836483` empty grants,
	     CQ-drain `388056` productive grants versus `14251288` empty grants,
	     peer-control `266332` productive grants versus `150732913` empty grants,
	     and local-control `1841310` productive grants versus `149157935` empty
	     grants. Both services also planned heartbeat every pass. The later
	     heartbeat audit corrected the initial interpretation here: the heartbeat
	     executor did increment `heartbeatCounter`, but it bypassed
	     `HomerServiceFinishProgressGrant()`, so the diagnostic grant counters made
	     it look unexecuted. The immediate design implication is still that the next
	     policy/refinement should first improve coarse readiness for always-present
	     control/heartbeat and payload sources; another pure reordering policy is
	     likely to keep paying many empty grants unless readiness is made sharper.
	     After the diagnostic
	     run, the normal deployed binary
	     `93008296a75c1bc138fe8371cc43992d575c6f0bc6cd9228bad7e5bc8db7abb3` was
	     restarted on both hosts. A restored-normal c1 smoke completed `1000/1000`,
	     zero failures, at `440.546101 TPS`, p50 `0.223 ms`, p95 `0.236 ms`, p99
	     `0.342 ms`; final hash checks matched on both hosts, final process scans
	     showed only PostgreSQL plus the two Homer services, and normal service logs
	     had no failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/
	     timeout/could-not/fatal/segmentation/mismatch signatures.
	   - Heartbeat coarse-readiness refinement after the diagnostic slice: the
	     service now inserts heartbeat into the scheduler ready set once per
	     `HOMER_SERVICE_HEARTBEAT_INTERVAL_PASSES` service passes, defaulting to
	     `1024`, instead of treating heartbeat as ready on every main-loop pass
	     ([macro](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:250),
	     [single-thread countdown](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1487),
	     [due helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3368),
	     [ready-set gate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4210)).
	     The first eligible pass still heartbeats immediately, and
	     `HOMER_SERVICE_HEARTBEAT_INTERVAL_PASSES=1` restores the previous per-pass
	     behavior for A/B checks. The heartbeat executor now creates a stack-local
	     `HomerProgressResult`, marks one item, and calls
	     `HomerServiceFinishProgressGrant()` so diagnostic builds count heartbeat
	     grants through the same path as other sources
	     ([executor](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19120)).
	     This is a scheduler-readiness cleanup, not a new scheduling policy: it
	     removes a known always-live source from most ready-set plans without adding
	     locks, heap work, atomics, or a hot-loop environment read. Verification
	     built the normal default-disabled binary with `make -j8 service-bin
	     client-bin`, installed it locally despite the expected `safe.directory`
	     warnings, copied it to farnet0, and restarted both services with SHA-256
	     `858bbe9d8c96138dc961709a1f0b3f3a48bfcd5245a57e9cefae0f736c35a88f`.
	     The diagnostic stats binary also compiled successfully with SHA-256
	     `f802f0f8d8129834584d85459bd42ca6aa632c9637a5709504560cdabc6817f2`.
	     Normal no-regression validation completed c1 remote Homer pgbench at
	     `1000/1000`, zero failures, `439.394725 TPS`, p50 `0.227 ms`, p95
	     `0.242 ms`, p99 `0.349 ms`; c4 warmup at `40000/40000`, zero failures,
	     `10915.371217 TPS`, p50 `0.348 ms`, p95 `0.493 ms`, p99 `0.597 ms`; and a
	     warmed c4 repeat at `40000/40000`, zero failures, `10882.230596 TPS`, p50
	     `0.350 ms`, p95 `0.486 ms`, p99 `0.594 ms`. Remote RDMA basebackup
	     completed first in `6.46s` and warmed in `4.36s`. A short diagnostic c4 run
	     completed `4000/4000`, zero failures, and after service exit showed the
	     intended heartbeat reduction: on farnet1, heartbeat was ready/planned/
	     granted `137999` times over `141310104` ready-set builds; on farnet0,
	     heartbeat was ready/planned/granted `95096` times over `97377636`
	     ready-set builds. In both logs, heartbeat `grant_progress` equaled grants,
	     confirming the accounting correction. After restoring the normal binary, a
	     final c1 smoke completed `1000/1000`, zero failures, at `440.231914 TPS`,
	     p50 `0.222 ms`, p95 `0.240 ms`, p99 `0.339 ms`; final hashes matched on
	     both hosts, final process scans showed only PostgreSQL plus the two normal
	     Homer services, and normal service logs on both hosts had no failed/
	     invalid/underflow/overrun/overflow/broken/error/checkpoint/timeout/
	     could-not/fatal/segmentation/mismatch signatures.
	   - Local-control coarse-readiness refinement after the heartbeat slice: the
	     aggregate local-control source is no longer inserted into every ready set.
	     `HomerServiceLocalControlSourceReadyForScheduler()` first keeps active
	     async local-control continuations visible, then scans the fixed
	     shared-memory control slot table for a non-async-owned
	     `CITUS_REMOTE_EXEC_CONTROL_SLOT_REQUEST_READY` slot
	     ([ready helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16409)).
	     The coarse ready-set builder now appends the local-control source only
	     when that helper reports ready
	     ([ready-set gate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4164)).
	     This is intentionally not a control request batching change: it does not
	     delay visible local requests, add locks, allocate work items, or rely on
	     producer-side hints. It still pays the fixed slot-table atomic loads in
	     ready-set construction, but avoids an always-empty scheduler grant and
	     prevents future policies from treating local control as permanently ready.
	     Verification built the normal binary with `make -j8 service-bin
	     client-bin`, compiled the diagnostic stats binary, installed/synced the
	     normal service to both hosts, and restarted both services with SHA-256
	     `04376eb7166b375b717c0031d59afcc0824369349758ed91e781f30a7404cebd`.
	     The diagnostic stats binary SHA-256 was
	     `425a1df3033a877e8febe076554e48ba697e88a99b0f79da64a369444c19329a`.
	     Normal no-regression validation completed c1 remote Homer pgbench at
	     `1000/1000`, zero failures, `438.869822 TPS`, p50 `0.229 ms`, p95
	     `0.243 ms`, p99 `0.356 ms`; c4 warmup at `40000/40000`, zero failures,
	     `10874.328646 TPS`, p50 `0.350 ms`, p95 `0.489 ms`, p99 `0.588 ms`; and a
	     warmed c4 repeat at `40000/40000`, zero failures, `10952.707578 TPS`, p50
	     `0.347 ms`, p95 `0.481 ms`, p99 `0.570 ms`. Remote RDMA basebackup
	     completed first in `5.78s` and warmed in `4.20s`. A short diagnostic c4 run
	     completed `4000/4000`, zero failures, and after service exit showed the
	     intended local-control reduction: on farnet1, local-control was
	     ready/planned/granted `2217360` times over `225614014` ready-set builds,
	     with `0` empty local-control grants; on farnet0, local-control was
	     ready/planned/granted `1868898` times over `164402512` ready-set builds,
	     also with `0` empty local-control grants. After restoring the normal
	     binary, a final c1 smoke completed `1000/1000`, zero failures, at
	     `440.082155 TPS`, p50 `0.221 ms`, p95 `0.236 ms`, p99 `0.351 ms`; final
	     hashes matched on both hosts, final process scans showed only PostgreSQL
	     plus the two normal Homer services, and normal service logs on both hosts
	     had no failed/invalid/underflow/overrun/overflow/broken/error/checkpoint/
	     timeout/could-not/fatal/segmentation/mismatch signatures.
	   - Rejected receive-side payload poll-gate follow-up after the local-control
	     slice: an attempted `HOMER_SERVICE_RECEIVE_PAYLOAD_POLL_INTERVAL_PASSES`
	     countdown made incoming payload streams advertise scheduler readiness only
	     periodically when no already observed local doorbell was pending. The idea
	     was to reduce broad payload readiness without adding locks, heap work, or
	     producer-side metadata, but it was too aggressive for the current receiver
	     semantics. A c1 smoke passed at `1000/1000`, zero failures,
	     `438.628042 TPS`, p50 `0.228 ms`, p95 `0.243 ms`, p99 `0.356 ms`, but c4
	     failed immediately with four `Homer sql_execute failed: Homer control
	     request failed` errors and left the remote pgbench process spinning until
	     killed. The service logs showed peer-control receive-completion failures,
	     async local-open reclaim, byte-ring receiver head-ACK completion drain
	     failure, lane send-CQ drain failure, and an outgoing peer transport reset.
	     That failure pattern means the receive-side path is not just speculative
	     discovery work: it also carries required doorbell/ACK/close progress that
	     can break multi-client transport liveness if delayed by a coarse pass
	     countdown.
	     The rejected fields/helper/macros were removed, and
	     `HomerServicePayloadStreamSourceReadyForScheduler()` now keeps the
	     receive-side peer-bound branch conservatively ready again while documenting
	     the failed experiment
	     ([payload readiness helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4072),
	     [receive-side conservative branch](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4101),
	     [sender-side ready check](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4112)).
	     The rebuilt normal service binary was installed/synced to both hosts with
	     SHA-256
	     `7a05cf1f1e32da49e8fbb9332f7a917138301eeac2339a8765111038c37b374b`.
	     Recovery validation completed c1 at `1000/1000`, zero failures,
	     `437.447424 TPS`, p50 `0.234 ms`, p95 `0.250 ms`, p99 `0.349 ms`; c4
	     after restart at `40000/40000`, zero failures, `10644.888644 TPS`, p50
	     `0.357 ms`, p95 `0.499 ms`, p99 `0.598 ms`; warmed c4 repeat at
	     `40000/40000`, zero failures, `10714.203445 TPS`, p50 `0.355 ms`, p95
	     `0.500 ms`, p99 `0.604 ms`; and another warmed c4 repeat at `40000/40000`,
	     zero failures, `10656.975923 TPS`, p50 `0.357 ms`, p95 `0.493 ms`, p99
	     `0.586 ms`. Remote RDMA basebackup completed first in `5.92s` and warmed
	     in `4.46s`. Both normal service logs had no recent failed/error/resetting/
	     broken/RDMA peer-control pump/completion-drain signatures after the
	     recovery runs. Treat the c4 band as functionally recovered but not a new
	     performance win: it is slightly below the earlier local-control warmed
	     datapoint, so future payload-readiness work should either expose a real
	     receiver-local ready frontier or keep receive-side streams conservative.
	   - Peer-control coarse-readiness refinement after the rejected receive-side
	     payload gate: the existing idle peer pump interval now gates ready-set
	     insertion instead of only skipping inside the peer-control executor. The
	     service still treats active peer command endpoints and bound peer payload
	     streams as ready on every pass, but an otherwise idle service advertises
	     peer-control only once per `HOMER_SERVICE_IDLE_PEER_PUMP_INTERVAL`
	     countdown so the RDMA listener is still polled periodically for first
	     connections
	     ([interval macro](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:78),
	     [idle countdown](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1490),
	     [ready helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3399),
	     [ready-set gate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4215)).
	     `TupleSinkServiceHasActivePeerWork()` is now NULL-safe and remains the
	     conservative active-work predicate over peer command endpoints, peer-bound
	     payload streams, connection handles, registered send-queue memory, and
	     pending receive doorbells
	     ([active-work predicate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12022)).
	     `TupleSinkServicePumpPeerConnections()` no longer has a second
	     `idlePeerPumpSkipCounter`, so an eligible peer-control grant performs the
	     actual RDMA peer pump once instead of stacking two periodic gates
	     ([peer pump executor](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12058)).
	     Verification built the normal binary with `make -j8 service-bin
	     client-bin`, installed/synced it to both hosts with SHA-256
	     `6da556e9afbf65f674deb7155dc7b03d28c33e216c6a3ad25ff51b23e18a6cda`,
	     and restarted both services. Normal validation completed c1 at
	     `1000/1000`, zero failures, `437.097897 TPS`, p50 `0.234 ms`, p95
	     `0.248 ms`, p99 `0.357 ms`; c4 at `40000/40000`, zero failures,
	     `10774.227939 TPS`, p50 `0.354 ms`, p95 `0.491 ms`, p99 `0.591 ms`; and a
	     warmed c4 repeat at `40000/40000`, zero failures, `10750.486056 TPS`, p50
	     `0.355 ms`, p95 `0.487 ms`, p99 `0.589 ms`. Remote RDMA basebackup
	     completed first in `5.92s` and warmed in `4.49s`. A diagnostic stats binary
	     compiled successfully with SHA-256
	     `81d95793942c22e4141e58af6284f1c7df57d5dc0f79ec22375c00dc8c98ad0e`;
	     a short diagnostic c4 run completed `4000/4000`, zero failures, and after
	     service exit showed the intended idle-side reduction. On farnet1,
	     peer-control was ready/planned/granted `2980615` times over `192955554`
	     ready-set builds, with `4292` productive grants and `2976323` empty
	     grants. On farnet0, peer-control was ready/planned/granted `21077890`
	     times over `123847439` ready-set builds, with `10285` productive grants
	     and `21067605` empty grants. This is an improvement over every-pass
	     insertion, but it deliberately leaves active peer/payload states broad:
	     active-connection readiness still needs a finer local frontier if we want
	     to reduce those empty grants further. After restoring the normal binary, a
	     final c1 smoke completed `1000/1000`, zero failures, at `438.404593 TPS`,
	     p50 `0.226 ms`, p95 `0.241 ms`, p99 `0.347 ms`; normal service logs on
	     both hosts had no recent failed/error/resetting/broken/RDMA peer-control
	     pump/completion-drain signatures.
	   - Rejected active peer-control transport-readiness follow-up: a later
	     attempt replaced the broad active-work predicate with a transport-private
	     readiness hook that checked active setup, visible control-mailbox tails,
	     pending control doorbells, response-publish slots awaiting send-CQ
	     retirement, and periodic active listener/CM maintenance. The hypothesis
	     was that bound payload streams should not make peer-control ready unless
	     the RDMA control transport had a cheap local progress fact. That was too
	     narrow for the current basebackup close/lifetime path. The attempted
	     normal binary built and synced with SHA-256
	     `924719ccd052f7f79396742c0eda6fdedcb269c5c2f80b6791991af5ab5008d3`,
	     and pgbench did not catch the issue: c1 completed `1000/1000`, zero
	     failures, `436.971265 TPS`, p99 `0.362 ms`; c4 completed `40000/40000`,
	     zero failures, `10749.492221 TPS`, p99 `0.592 ms`; and warmed c4 completed
	     `40000/40000`, zero failures, `10826.450643 TPS`, p99 `0.596 ms`.
	     Remote RDMA basebackup then failed during close stream after the receiver
	     logged `byte-ring basebackup blackhole complete`, with the client error
	     `Homer base backup target failed during close stream` and detail
	     `timed out waiting for Homer service progress owner_pid=4172313
	     request_sequence=2`. That is the rejection evidence for this patch: the
	     candidate passed c1/c4 pgbench but failed the basebackup close/lifetime
	     path, so pgbench command/control success alone is not a sufficient
	     validation proxy for peer-control readiness changes.
	     The active transport-private readiness hook, force flags, and extra
	     transport-state countdown were removed. The current code is back to the
	     accepted active-work predicate in `TupleSinkServiceHasActivePeerWork()`
	     and the idle peer-control gate in
	     `HomerServicePeerControlSourceReadyForScheduler()`
	     ([accepted ready helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3399),
	     [ready-set gate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4215),
	     [active-work predicate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12006)).
	     The recovery binary built, installed, and synced with SHA-256
	     `ad775c4c04ca846c5ea23b2d0b812c7606ee296c3e18add191205f391d82f9a9`.
	     Recovery validation completed the same basebackup first in `6.34s` and
	     warmed in `4.40s`, and a final c1 smoke completed `1000/1000`, zero
	     failures, `437.633889 TPS`, p50 `0.229 ms`, p95 `0.243 ms`, p99
	     `0.364 ms`; restored normal service logs on both hosts had no recent
	     failed/error/resetting/broken/RDMA peer-control pump/completion-drain/
	     timed-out signatures. This rejection leaves active peer-control readiness
	     as an open design problem: any future refinement must include the
	     basebackup close/lifetime path as a first-class validation gate, not only
	     pgbench.
	   - Receive-side queued-doorbell payload readiness refinement after the
	     rejected periodic poll gate: the transport now exposes
	     `TupleSinkServicePeerDataDoorbellPendingRdma()` as a non-consuming scan of
	     the connection's fixed pending data-doorbell array
	     ([header declaration](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:668),
	     [implementation](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6587)).
	     The receive-side payload readiness branch no longer advertises every
	     peer-bound receiver stream on every pass. It remains ready when the stream
	     already has `receivePayloadDoorbellPending` set, or when the transport has
	     already queued a data doorbell for that sink; actual doorbell consumption
	     and ordering remain owned by the payload pump
	     ([receive-side readiness branch](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4141)).
	     This is intentionally different from the failed periodic poll gate: it
	     never delays a doorbell that peer-control has already drained from the
	     recv CQ, and it relies on the still-conservative active peer-control pump
	     to keep filling the pending doorbell array.
	     Verification built the normal binary with `make -j8 service-bin
	     client-bin`, installed/synced it to both hosts with SHA-256
	     `6621eb6b9b01328dcc2f637446ea152cb025f3b19e0c2e1d105960ad6cd55da2`,
	     and restarted both services. Normal validation completed c1 at
	     `1000/1000`, zero failures, `437.012704 TPS`, p50 `0.231 ms`, p95
	     `0.251 ms`, p99 `0.351 ms`; c4 at `40000/40000`, zero failures,
	     `10837.923194 TPS`, p50 `0.351 ms`, p95 `0.491 ms`, p99 `0.591 ms`; and a
	     warmed c4 repeat at `40000/40000`, zero failures, `10735.698640 TPS`, p50
	     `0.356 ms`, p95 `0.488 ms`, p99 `0.587 ms`. Remote RDMA basebackup
	     completed first in `5.86s` and warmed in `4.64s`. A diagnostic stats
	     binary compiled successfully with SHA-256
	     `84e1f956ab7f403f2f724d560f5c3714198deda3a4e5d8f291dc863770f67e6c`;
	     a short diagnostic c4 run completed `4000/4000`, zero failures. On farnet0,
	     payload-stream ready/planned/grants dropped to `36552` over `203130738`
	     ready-set builds, with `6964` productive grants and `29588` empty grants;
	     this is the intended reduction versus the earlier broad receive-side
	     shape that produced tens of millions of payload-stream grants in the same
	     diagnostic workload. On farnet1, payload-stream was ready/planned/granted
	     `21383` times over `309070105` ready-set builds, with `6855` productive
	     grants and `14528` empty grants. After restoring the normal binary, a
	     final c1 smoke completed `1000/1000`, zero failures, `437.061982 TPS`,
	     p50 `0.230 ms`, p95 `0.245 ms`, p99 `0.349 ms`; restored normal service
	     logs on both hosts had no recent failed/error/resetting/broken/RDMA
	     peer-control pump/completion-drain/timed-out signatures.
	   - Diagnostic-only peer-control empty-grant classification after the
	     queued-doorbell payload refinement: no new readiness policy was applied.
	     The existing progress result already reports peer-control work as exact
	     `setupSteps`, `listenerEvents`, `eventDrains`, `sendCompletions`,
	     `controlResponses`, `controlRequests`, and `connectionResets` from
	     `TupleSinkServicePeerPumpProgress`
	     ([progress struct](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:242),
	     [result merge](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12053)).
	     A peer-control grant is empty when the selected transport pump returns
	     zero in all of those fields. The diagnostic transport counters can then
	     explain where that empty pump time went without adding another hot-path
	     readiness hook: `HOMER_SERVICE_PEER_TRANSPORT_STATS` reports listener
	     poll/skip counts, active table scans, per-class drain calls, per-lane
	     recv-CQ empty polls, mailbox empty returns, and send-CQ empty polls
	     ([diagnostic counters](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:243),
	     [stats logger](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4864)).
	     The combined diagnostic binary was compiled with
	     `HOMER_SERVICE_PROGRESS_STATS=1` and
	     `HOMER_SERVICE_PEER_TRANSPORT_STATS=1`, producing SHA-256
	     `64da6bbb80e2e5f2810c44fc50d69df0d9594d79276d4d3b2a2e3feadac39609`.
	     A short diagnostic c4 run completed `4000/4000`, zero failures, with
	     p50 `0.375 ms`, p95 `0.525 ms`, and p99 `0.641 ms`; its TPS is not a
	     normal-performance datapoint because this build includes heavy diagnostic
	     accounting. On farnet1, peer-control was ready/planned/granted `3763621`
	     times over `182711054` ready-set builds, with `4299` productive grants
	     and `3759322` empty grants. Its outgoing foreground-payload lane saw
	     `700825` empty recv-CQ polls versus `4004` recv CQEs, and its incoming
	     critical-control lane saw `3658240` mailbox empty returns versus only
	     `4` dispatched mailbox requests. On farnet0, peer-control was
	     ready/planned/granted `30616983` times over `125546020` ready-set builds,
	     with `13569` productive grants and `30603414` empty grants; its
	     connections were reset during orderly service shutdown before per-lane
	     stats were logged, but aggregate transport stats still showed
	     `30303327` outgoing critical-control drains and `23299064` incoming
	     foreground-payload drains. This diagnostic supports a scoped observation:
	     in this short stats run, peer-control empty grants were dominated by
	     active-connection CQ/mailbox probing, not by idle listener polling alone.
	     After restoring the normal binary, both hosts again reported SHA-256
	     `6621eb6b9b01328dcc2f637446ea152cb025f3b19e0c2e1d105960ad6cd55da2`.
	     A final restored-normal c1 smoke completed `1000/1000`, zero failures,
	     `437.243912 TPS`, p50 `0.228 ms`, p95 `0.241 ms`, p99 `0.345 ms`; normal
	     service logs on both hosts had no recent failed/error/resetting/broken/
	     RDMA peer-control pump/completion-drain/timed-out signatures. Do not read
	     this diagnostic as proving that no more counters are needed. It only
	     rules out idle-listener polling as the sole explanation in this run and
	     explains why another active peer-control readiness narrowing should first
	     define a basebackup-close-safe frontier/lifetime predicate instead of
	     relying on broad active connection presence or pgbench-only success.
	   - Accepted active-connection scan-limit refinement after the peer-control
	     diagnostic: this slice does not narrow peer-control readiness, skip
	     mailbox probes, or change CQ/mailbox semantics. Instead, the peer
	     transport state now owns `outgoingConnectionScanLimit` and
	     `incomingConnectionScanLimit`
	     ([scan-limit fields](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:546)).
	     Allocation records the fixed-table owner/index and extends the matching
	     scan limit
	     ([slot remember helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:566),
	     [outgoing allocation](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3482),
	     [incoming allocation](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3510)).
	     Reset trims only inactive suffix slots
	     ([scan-limit shrink helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:597),
	     [reset hook](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2823)).
	     The outgoing cache lookup and the top-level peer-control pump now scan
	     only the active prefix
	     ([outgoing lookup](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3445),
	     [peer pump outgoing scan](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7240),
	     [peer pump incoming scan](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7284)).
	     This targets the measured `*_slot_checks` cost while leaving every active
	     lane's setup, recv-CQ drain, send-CQ drain, response consumption, and
	     incoming mailbox pump intact.
	     Verification built `make -j8 service-bin client-bin`, installed/synced
	     the normal binary with SHA-256
	     `462c43117b63ae3f3ddbdb78b5d749729a2a0226eb08f7775a89fbd829638879`,
	     and restarted both services. Normal validation completed c1 at
	     `1000/1000`, zero failures, `436.035167 TPS`, p50 `0.234 ms`, p95
	     `0.249 ms`, p99 `0.365 ms`; c4 at `40000/40000`, zero failures,
	     `10840.190657 TPS`, p50 `0.352 ms`, p95 `0.490 ms`, p99 `0.580 ms`; and a
	     warmed c4 repeat at `40000/40000`, zero failures, `10844.434419 TPS`,
	     p50 `0.352 ms`, p95 `0.492 ms`, p99 `0.602 ms`. Remote RDMA basebackup
	     completed first in `6.28s` and warmed in `4.54s`. A combined diagnostic
	     stats binary built with SHA-256
	     `9ba6fa4e91f00931559d19ae1f7824da55cddf3ee3dcb9a273b31e6e4071c4bc`,
	     and a short diagnostic c4 completed `4000/4000`, zero failures. The
	     diagnostic counters showed the intended scan reduction. On farnet1,
	     peer-control had `3349724` pump/grant calls, but only `3211350` outgoing
	     slot checks and `3239427` incoming slot checks; before this slice the
	     same short diagnostic shape had full-table scans in the hundreds of
	     millions because each peer pump walked `64` outgoing and `64` incoming
	     slots. On farnet0, peer-control had `55706659` pump/grant calls,
	     `55024724` outgoing slot checks, and `54939338` incoming slot checks.
	     After restoring the normal binary, a final c1 smoke completed
	     `1000/1000`, zero failures, `437.180448 TPS`, p50 `0.226 ms`, p95
	     `0.242 ms`, p99 `0.346 ms`; both hosts were back on the normal scan-limit
	     SHA-256 above, and normal service logs on both hosts had no recent
	     failed/error/resetting/broken/RDMA peer-control pump/completion-drain/
	     timed-out signatures.
	   - Accepted peer-control mailbox empty-path precheck after the scan-limit
	     refinement: this slice still does not narrow scheduler readiness and does
	     not skip any queued control doorbell or visible-tail fallback. The new
	     `TupleSinkServiceLocalMailboxMayHaveMessage()` precheck returns true for
	     the same two publication signals that the full consume helper uses: a
	     queued control WRITE_WITH_IMM doorbell, or a visible
	     `publishedTail > consumedHead` fallback
	     ([precheck helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3946)).
	     The incoming mailbox pump now returns from the empty path before entering
	     `TupleSinkServiceTryConsumeLocalMailbox()` and before carrying the
	     fixed-width peer-control message object through the consume/validate path
	     when neither signal is present
	     ([incoming precheck call](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4681)).
	     Outgoing peer-control response consumption uses the same precheck before
	     the response consume path
	     ([response precheck call](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6810)).
	     Diagnostic-only peer-connection stats now expose
	     `mailbox_precheck_empty` so experiments can separate early-empty returns
	     from stale-doorbell/full-consume empty returns
	     ([diagnostic field](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:264),
	     [stats log field](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5046)).
	     Verification built `make -j8 service-bin client-bin`, installed/synced
	     the normal binary with SHA-256
	     `eda1ab914f95a9561c1a19b495f8b2222cca03c3faa91c71fa07909dd452edb3`,
	     and restarted both services. Normal validation completed c1 at
	     `1000/1000`, zero failures, `435.815871 TPS`, p50 `0.235 ms`, p95
	     `0.248 ms`, p99 `0.356 ms`; c4 at `40000/40000`, zero failures,
	     `10824.815782 TPS`, p50 `0.352 ms`, p95 `0.489 ms`, p99 `0.584 ms`; and a
	     warmed c4 repeat at `40000/40000`, zero failures, `10774.166995 TPS`,
	     p50 `0.353 ms`, p95 `0.489 ms`, p99 `0.585 ms`. Remote RDMA basebackup
	     completed first in `6.18s` and warmed in `4.56s`. After adding the
	     diagnostic field, a final normal c1 smoke completed `1000/1000`, zero
	     failures, `436.189414 TPS`, p99 `0.356 ms` before the diagnostic swap.
	     A combined diagnostic stats binary built with SHA-256
	     `f441e3dd95e251853701e32f564338c3d55ad1baa901d0c31f150196d325318a`,
	     and a short diagnostic c4 completed `4000/4000`, zero failures. On
	     farnet1's incoming critical-control lane, `mailbox_precheck_empty` was
	     `4305856` out of `4305859` total `mailbox_empty` returns, while the same
	     run still dispatched the `4` real mailbox messages. This supports the
	     intended local effect in that diagnostic run: the new early-empty branch
	     covered nearly all empty incoming mailbox returns without suppressing the
	     real requests observed by the test. After restoring
	     the normal binary, a final c1 smoke completed `1000/1000`, zero failures,
	     `437.244294 TPS`, p50 `0.225 ms`, p95 `0.238 ms`, p99 `0.349 ms`; both
	     hosts were back on the normal SHA-256 above, and normal service logs on
	     both hosts had no recent failed/error/resetting/broken/RDMA peer-control
	     pump/completion-drain/timed-out signatures.
	   - Accepted command send-CQ readiness coarsening after the mailbox precheck:
	     this slice narrows only the command side of the aggregate `cq-drain`
	     source and leaves payload send-CQ readiness unchanged. The service now
	     tracks `ClientSqlCommandWriteCqDrainReadySkipCounter`
	     ([counter](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:789))
	     and routes command send-CQ scheduler eligibility through
	     `HomerServiceCommandSendCqReadyForScheduler()`
	     ([helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4665)).
	     The helper returns false when there are no signaled command WRs, returns
	     true immediately when outstanding command-write completions reach the
	     existing
	     `HOMER_SERVICE_CLIENT_SQL_COMMAND_COMPLETION_POLL_MIN_ACTIVE` pressure
	     threshold, and otherwise polls periodically using the existing
	     `HOMER_SERVICE_CLIENT_SQL_COMMAND_COMPLETION_POLL_INTERVAL` cadence. The
	     coarse ready-set builder now appends `HOMER_PROGRESS_SOURCE_CQ_DRAIN`
	     when either that command predicate or the existing payload predicate is
	     true
	     ([ready-set gate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4203)).
	     This mirrors the older owner-polled command send-CQ throttle at
	     scheduler-readiness time, so low-pressure command checkpoint CQEs do not
	     make `cq-drain` scheduler-ready on every service pass just to observe an
	     empty CQ.
	     Verification built `make -j8 service-bin client-bin`, installed/synced
	     the normal binary with SHA-256
	     `532bf38e7dac546c7735580907ddfa8185e9521dcf6988a7d8e5e67986882f2f`,
	     and restarted both services. Normal validation completed c1 at
	     `1000/1000`, zero failures, `435.785294 TPS`, p50 `0.233 ms`, p95
	     `0.251 ms`, p99 `0.351 ms`; c4 at `40000/40000`, zero failures,
	     `10900.037932 TPS`, p50 `0.350 ms`, p95 `0.488 ms`, p99 `0.586 ms`; and
	     a warmed c4 repeat at `40000/40000`, zero failures, `10876.108615 TPS`,
	     p50 `0.351 ms`, p95 `0.486 ms`, p99 `0.592 ms`. Remote RDMA basebackup
	     completed first in `6.25s` and warmed in `4.39s`, and normal logs on
	     both hosts had no recent failed/error/resetting/broken/RDMA peer-control
	     pump/completion-drain/timed-out signatures.
	     A combined diagnostic stats binary built with SHA-256
	     `e632a2316bf11ac5c16012fe9b4b51e22174755d5eedddc6baaa43465a078a63`,
	     and a short diagnostic c4 completed `4000/4000`, zero failures. The
	     diagnostic result supports the intended scheduler-readiness reduction:
	     farnet0 `cq-drain` ready/planned/granted fell to `301690` with
	     `296688` empty grants, versus roughly `10.9M` ready/granted and
	     `10.88M` empty grants in the prior short diagnostic shape. farnet1
	     stayed in the same small command-CQ band (`19463` grants, `18084` empty),
	     which matches the expectation that farnet0 was the command-send-CQ hot
	     side. The diagnostic run logged two farnet0 peer resets during deliberate
	     stats-service shutdown, not during workload execution. After restoring
	     the normal binary, a final c1 smoke completed `1000/1000`, zero
	     failures, `436.671307 TPS`, p50 `0.226 ms`, p95 `0.249 ms`, p99
	     `0.347 ms`; both hosts were back on the normal SHA-256 above, and normal
	     service logs on both hosts had no recent failed/error/resetting/broken/
	     RDMA peer-control pump/completion-drain/timed-out signatures.
	   - Accepted active peer-control readiness interval after command-CQ
	     coarsening: this slice reduces the remaining broad active peer-control
	     empty grants without reviving the rejected transport-private active
	     narrowing. There is still no cheap verbs predicate for "recv CQ has work",
	     and the peer-control pump owns required liveness work: recv-CQ doorbells,
	     mailbox request/response dispatch, async disconnects, and send-CQ
	     retirement. The service therefore adds
	     `HOMER_SERVICE_ACTIVE_PEER_PUMP_INTERVAL`, default `64`, as a bounded
	     fallback; setting it to `1` restores the previous every-loop active
	     behavior
	     ([active interval](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:92)).
	     The first transition into active peer work still pumps immediately through
	     `HomerServiceActivePeerPumpCountdown = 0`
	     ([active countdown](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1525)).
	     `HomerServicePeerControlSourceReadyForScheduler()` now uses the active
	     interval only when `TupleSinkServiceHasActivePeerWork()` is true, resets
	     the idle listener countdown while active, and resets the active countdown
	     when the service returns to idle-peer mode
	     ([active readiness branch](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3446)).
	     Verification built `make -j8 service-bin client-bin`, installed/synced
	     the normal binary with SHA-256
	     `e1fb249c711f714f0d7f8d30793039876653982b095757f96f54c092d4f3dbf1`,
	     and restarted both services. Normal validation completed c1 at
	     `1000/1000`, zero failures, `436.235653 TPS`, p50 `0.229 ms`, p95
	     `0.244 ms`, p99 `0.359 ms`; c4 at `40000/40000`, zero failures,
	     `10867.397644 TPS`, p50 `0.351 ms`, p95 `0.490 ms`, p99 `0.581 ms`; and
	     a warmed c4 repeat at `40000/40000`, zero failures, `10914.028018 TPS`,
	     p50 `0.350 ms`, p95 `0.490 ms`, p99 `0.579 ms`. Remote RDMA basebackup
	     completed first in `6.45s` and warmed in `4.34s`, which is the required
	     liveness gate because a previous active peer-control narrowing timed out
	     in basebackup close/lifetime progress. Normal logs on both hosts had no
	     recent failed/error/resetting/broken/RDMA peer-control pump/
	     completion-drain/timed-out signatures.
	     A combined diagnostic stats binary built with SHA-256
	     `8a36ab9d7cd170d36048f22d89c039c8226f258d9cfc9ac43918eca599f61fa0`,
	     and a short diagnostic c4 completed `4000/4000`, zero failures. The
	     diagnostic counters showed the intended broad-grant reduction: farnet0
	     peer-control ready/planned/granted fell to `672299`, with `15506`
	     productive grants and `656793` empty grants, versus `15103249` grants and
	     `15074506` empty grants in the prior short diagnostic shape. farnet1
	     peer-control grants fell to `116249`, with `4008` productive grants and
	     `112241` empty grants, versus `2987247` grants and `2983025` empty grants
	     before this slice. The diagnostic run logged two farnet0 peer resets
	     during deliberate stats-service shutdown, not during workload execution.
	     After restoring the normal binary, a final c1 smoke completed
	     `1000/1000`, zero failures, `436.970883 TPS`, p50 `0.224 ms`, p95
	     `0.239 ms`, p99 `0.343 ms`; both hosts were back on the normal SHA-256
	     above, and normal service logs on both hosts had no recent failed/error/
	     resetting/broken/RDMA peer-control pump/completion-drain/timed-out
	     signatures.
	   - Accepted local-control async readiness interval after the active
	     peer-control interval: a diagnostic-only counter split first showed that
	     the remaining local-control "productive" grants were overwhelmingly
	     pending async continuation polls, not response publication. The
	     stats-only fields `localControlAsyncTouched`,
	     `localControlAsyncPending`, `localControlAsyncCompleted`,
	     `localControlAsyncFailed`, and `localControlAsyncStale` are part of
	     `HomerServiceProgressStats`
	     ([diagnostic fields](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1346)),
	     and `HomerServiceLogProgressStats()` now emits
	     `progress_local_control_async_stats`
	     ([diagnostic log](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3365)).
	     In the pre-interval short diagnostic c4 shape, farnet1 reported
	     `12118455` touched async continuations with `12118451` pending and only
	     `4` completed; farnet0 reported `4687886` touched, `4687882` pending, and
	     `4` completed. That counter split was the evidence for coarsening pending
	     async continuation readiness rather than changing the local-control
	     executor's completion semantics.
	     The accepted slice adds `HOMER_SERVICE_LOCAL_CONTROL_ASYNC_PUMP_INTERVAL`,
	     default `64`, with `1` restoring the previous every-pass polling behavior
	     ([async interval](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:144)).
	     `HomerServiceLocalControlAsyncPumpCountdown` is service-thread local
	     ([async countdown](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1526)).
	     `HomerServiceLocalControlSourceReadyForScheduler()` now keeps a pending
	     async continuation visible periodically while preserving the existing
	     fixed-slot scan when no async continuation is active
	     ([local-control readiness branch](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16516)).
	     The executor still treats each touched async continuation as progress once
	     selected, so the old liveness/accounting boundary is unchanged; only
	     ready-set insertion is coarsened.
	     Verification built `make -j8 service-bin client-bin`, installed/synced
	     the normal binary with SHA-256
	     `a5c59c8eef640a451675792e8204a5d10f953793d7e858ad0b970ea29f7faffc`,
	     and restarted both services. Normal validation completed c1 at
	     `1000/1000`, zero failures, `436.971074 TPS`, p50 `0.223 ms`, p95
	     `0.237 ms`, p99 `0.342 ms`; c4 at `40000/40000`, zero failures,
	     `10901.359863 TPS`, p50 `0.349 ms`, p95 `0.486 ms`, p99 `0.571 ms`; and
	     a warmed c4 repeat at `40000/40000`, zero failures, `10871.680468 TPS`,
	     p50 `0.351 ms`, p95 `0.489 ms`, p99 `0.579 ms`. Remote RDMA basebackup
	     completed first in `5.96s` and warmed in `4.48s`, and normal logs on both
	     hosts had no recent failed/error/resetting/broken/RDMA peer-control pump/
	     completion-drain/timed-out signatures.
	     A combined diagnostic stats binary built with SHA-256
	     `426fa767e761dc3a2b3964969c81ac28d045bb1ad2e0049e7b7850bfa60ab916`,
	     and a short diagnostic c4 completed `4000/4000`, zero failures. The
	     diagnostic counters showed the intended reduction: farnet1 local-control
	     grants fell to `621577` with `2480443` async touches, `2480439` pending,
	     and `4` completed; farnet0 local-control grants fell to `241508` with
	     `959359` async touches, `959355` pending, and `4` completed. The
	     diagnostic run logged two farnet0 peer resets during deliberate
	     stats-service shutdown, not during workload execution. After restoring
	     the normal binary, a final c1 smoke completed `1000/1000`, zero failures,
	     `436.643279 TPS`, p50 `0.224 ms`, p95 `0.238 ms`, p99 `0.347 ms`; both
	     hosts were back on the normal SHA-256 above, and normal service logs on
	     both hosts had no recent failed/error/resetting/broken/RDMA peer-control
	     pump/completion-drain/timed-out signatures.
	   - Rejected command-CQ executor-gate follow-up after the local-control async
	     interval: the hypothesis was that `HOMER_PROGRESS_SOURCE_CQ_DRAIN`
	     grants caused by payload send-CQ readiness could still piggyback command
	     send-CQ polling in the progress plan/executor even when the command
	     readiness throttle had skipped command CQ polling in the coarse ready-set.
	     The experimental patch carried the command ready decision from
	     `HomerServiceBuildCoarseProgressReadySet()`
	     ([ready-set gate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4231))
	     through `HomerServiceBuildCqDrainProgressPlan()` and
	     `HomerServiceExecuteCqDrainProgressPlan()` so command CQ draining could
	     only happen when `HomerServiceCommandSendCqReadyForScheduler()`
	     ([command CQ helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4693))
	     said the current pass was due. This was a useful boundary-cleanup idea,
	     but it did not improve the measured scheduler noise.
	     Verification of the experimental normal binary
	     `b440f183bae995dee8bc292bf5b4bbcb31d122c5a44ae6374b3be2fcd87655ce`
	     completed c1 at `1000/1000`, zero failures, `435.288679 TPS`, p50
	     `0.224 ms`, p95 `0.238 ms`, p99 `0.358 ms`; c4 at `40000/40000`, zero
	     failures, `10795.055864 TPS`, p50 `0.351 ms`, p95 `0.493 ms`, p99
	     `0.584 ms`; and a warmed c4 repeat at `40000/40000`, zero failures,
	     `10832.044546 TPS`, p50 `0.352 ms`, p95 `0.493 ms`, p99 `0.581 ms`.
	     Remote RDMA basebackup still completed in `5.78s` first and `4.37s`
	     warmed, and normal logs were clean. The diagnostic binary
	     `33bd9af1aabea28fd5346616ee580ba1d7ac92217fb81689690973d8b646b45c`
	     completed a short c4 at `4000/4000`, zero failures, but the counters did
	     not justify the change: farnet1 CQ-drain grants were `29140` with
	     `27474` empty grants, while farnet0 CQ-drain grants rose to `687664`
	     with `669757` empty grants, worse than the accepted local-control
	     interval diagnostic band (`634376` grants, `615453` empty on farnet0).
	     The patch was reverted, the accepted normal binary
	     `a5c59c8eef640a451675792e8204a5d10f953793d7e858ad0b970ea29f7faffc`
	     was restored on both hosts, and a final restored c1 smoke completed
	     `1000/1000`, zero failures, `436.207680 TPS`, p50 `0.224 ms`, p95
	     `0.239 ms`, p99 `0.341 ms`. The current code therefore keeps command CQ
	     readiness coarsened at ready-set construction, but does not add the
	     executor-side current-pass command gate.
	   - Accepted completion-ring backlog feedback after the command-CQ
	     executor-gate rejection: this does not change the fixed-priority default
	     source order. It makes the existing per-session completion-ring source
	     expose real backlog facts for later ready-hints policies. The new
	     `HomerServiceCompletionRingReadyCount()` helper centralizes the service-
	     owned completion mailbox readiness check and clamps overrun-sized backlog
	     hints to the fixed mailbox window
	     ([backlog helper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4069)).
	     `HomerServiceCompletionRingSourceReady()` now uses that helper instead of
	     a boolean epoch inequality
	     ([readiness wrapper](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4110)).
	     `TupleSinkServiceConsumeCompletionMailbox()` still consumes at most one
	     backend completion record per selected session grant, but now records the
	     remaining mailbox backlog in `HomerProgressResult.readyObjectCountHint`
	     after the consumed epoch is published
	     ([selected consumer hint](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8092)).
	     `TupleSinkServiceConsumeAllCompletionMailboxes()` preserves its
	     null-result compatibility path while summing selected mailbox backlog
	     hints for aggregate fallback scans
	     ([aggregate hint merge](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8226)).
	     `HomerServiceUpdateProgressFeedback()` now stores completion-ring backlog
	     in `HomerProgressSourceFeedback.readyObjectCountHint` and keeps the
	     source core ready when records remain
	     ([feedback branch](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4559)).
	     Verification built `make -j8 service-bin client-bin`, installed/synced
	     the normal binary with SHA-256
	     `fecf8decdb8d752b1a29b03584aeb63b6db5c28ff43d1f17e7dbcecb7576cd82`,
	     and restarted both services. Normal validation completed c1 at
	     `1000/1000`, zero failures, `435.163850 TPS`, p50 `0.231 ms`, p95
	     `0.244 ms`, p99 `0.356 ms`; c4 at `40000/40000`, zero failures,
	     `10783.633141 TPS`, p50 `0.352 ms`, p95 `0.492 ms`, p99 `0.597 ms`; and
	     a warmed c4 repeat at `40000/40000`, zero failures, `10830.789226 TPS`,
	     p50 `0.353 ms`, p95 `0.491 ms`, p99 `0.575 ms`. Remote RDMA basebackup
	     completed first in `6.19s` and warmed in `4.32s`. Normal service logs on
	     both hosts had no recent failed/error/resetting/broken/RDMA peer-control/
	     completion-mailbox-overrun/timed-out signatures, and `git diff --check`
	     was clean for the Citus worktree.
	     Follow-up measurement clarified that the short c1 `-t 1000` TPS is
	     setup/outlier-sensitive for this remote-Homer shape: a restored-normal
	     `-t 1000` smoke reported `436.211296 TPS`, p50 `0.223 ms`, p99
	     `0.339 ms`, and max `2063.845 ms`, while a restored-normal `-t 10000`
	     run on the same binary completed `10000/10000`, zero failures,
	     `4420.463919 TPS`, p50 `0.223 ms`, p95 `0.233 ms`, p99 `0.245 ms`, and
	     max `6.424 ms`. Treat short c1 TPS as a setup-sensitive smoke number;
	     use longer/warmed c1 or percentile latency when evaluating steady
	     transaction-path regressions.
	   - Diagnostic-only payload readiness reason split after completion backlog
	     feedback: this is instrumentation, not a scheduler-policy change. The
	     `HomerServiceProgressStats` diagnostic struct now records payload
	     readiness reasons
	     ([payload ready counters](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1351)),
	     `HomerServiceLogProgressStats()` emits `progress_payload_ready_stats`
	     ([payload ready log](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3379)),
	     and `HomerServicePayloadStreamSourceReadyForScheduler()` increments the
	     counters under `HOMER_SERVICE_PROGRESS_STATS`
	     ([payload readiness predicate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4212)).
	     A temporary stats binary
	     `80ec4ddf903bbe35f94628e5bb2e0f7caf88dd1849d6082b01e93eea5f14d43c`
	     completed short c4 `4000/4000`, zero failures. The diagnostic run is not
	     a throughput comparison because stats builds add heavy service-loop
	     stores, but it showed one concrete reason payload sources remain broad in
	     this workload: on farnet1,
	     payload ready reasons were `completion=29302`, `outgoing=4000`, and no
	     incoming doorbells; on farnet0, they were `completion=58212`,
	     `incoming_doorbell=4799`, and no outgoing work. In that diagnostic run,
	     payload empty grants were mostly completion/ACK-driven wakeups, not a
	     local-blackhole or transport-broken artifact. After the diagnostic run, the normal binary
	     `49ef329164bf959832fe004669dc660c2a3c485d14cdf2e3b1728c48d6829371`
	     was rebuilt, installed on both hosts, restarted, and checked with the c1
	     smokes above.
	   - Payload completion-only readiness removal remains unresolved after the
	     payload readiness reason split. The tempting next step was to stop making
	     `HOMER_PROGRESS_SOURCE_PAYLOAD_STREAM` ready solely because
	     `payloadCompletionCount > 0` or receiver-head-ACK completions were
	     outstanding, since `HOMER_PROGRESS_SOURCE_CQ_DRAIN` already scans active
	     streams with those same completion predicates and dispatches data/ACK CQEs
	     through the same frontier helpers. The attempted direct-removal run is not
	     recorded as evidence because it did not produce a clean, interpretable
	     validation result. The current code conservatively keeps payload
	     completion/ACK-completion readiness in
	     `HomerServicePayloadStreamSourceReadyForScheduler()`
	     ([payload readiness predicate](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4212)).
	     Future attempts to de-duplicate payload and CQ-drain readiness need a
	     more explicit CQ-drain/payload ordering design and clean repeated
	     validation before changing this readiness rule.
	   - Rejected active peer-control interval `128` after the payload-readiness
	     rejection: increasing `HOMER_SERVICE_ACTIVE_PEER_PUMP_INTERVAL`
	     ([active interval](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:92))
	     from the accepted `64` to `128` kept the same bounded active-poll
	     liveness model, but it did not meet the no-regression bar. The candidate
	     binary `ca35d2686b33c8c8e8cc567503352bb9b1aa2cbbfc8be65091f3e8c57e1d2508`
	     passed correctness: c1 `-t 10000` first run completed `10000/10000`,
	     zero failures, `2388.379103 TPS`, p50 `0.210 ms`, p99 `0.233 ms`, with
	     the known first-run max outlier `2064.137 ms`; warmed c1 completed
	     `10000/10000`, zero failures, `4669.558680 TPS`, p50 `0.211 ms`, p99
	     `0.234 ms`. c4 also completed without failures and had slightly higher
	     throughput (`11033.782684 TPS` then warmed `10985.492010 TPS`), but p99
	     regressed to `0.613 ms` and `0.618 ms`, and a third warmed c4 repeat
	     stayed at `0.617 ms`. Remote RDMA basebackup remained live (`6.44s`
	     first, `4.20s` warm), so this was a tail-latency rejection rather than a
	     correctness/lifetime rejection. The source and both hosts were restored
	     to the accepted interval `64` and safe binary
	     `49ef329164bf959832fe004669dc660c2a3c485d14cdf2e3b1728c48d6829371`;
	     restored warmed c1 `-t 10000` completed `10000/10000`, zero failures,
	     `4592.211609 TPS`, p50 `0.215 ms`, p95 `0.226 ms`, p99 `0.239 ms`, max
	     `6.410 ms`. Future active peer-control tuning should not simply increase
	     the interval unless it has a compensating policy for c4 tail latency.
	   - Combined peer-transport/progress diagnostic after the interval-128
	     rejection: a temporary binary built with both
	     `HOMER_SERVICE_PROGRESS_STATS=1` and
	     `HOMER_SERVICE_PEER_TRANSPORT_STATS=1` had SHA-256
	     `dd3bee0261e97c69e1a617248df68824a44d8a7d703a3fc6ed44ce10aaaffc28`.
	     It completed short c4 `4000/4000`, zero failures; the throughput from
	     this stats build is not a performance comparison. The counters provide a
	     concrete reason to be skeptical of simple active peer-control interval
	     tuning as the only next lever. On farnet1,
	     peer-control grants were `723729`, with only `4006` productive grants and
	     `719723` empty grants. The outgoing foreground connection saw
	     `45969` drain calls, `49963` recv-CQ polls, `45969` empty recv-CQ polls,
	     and `4004` recv CQEs; the incoming critical connection saw `725333`
	     drain calls, `725334` recv-CQ polls, `725333` empty recv-CQ polls, only
	     `4` recv CQEs, plus `693309` mailbox pumps with `693306` precheck-empty
	     returns and `4` dispatched mailbox requests. On farnet0, peer-control
	     grants were `672070`, with `15160` productive grants and `656910` empty
	     grants; transport-level active scans were similarly dominated by active
	     connection drains with sparse CQ/mailbox work. Together with the separate
	     interval-128 tail-latency rejection above, this supports making the next
	     peer-control refinement an active-connection readiness or phase-budget
	     design rather than another blind interval increase.
	   - Reverted incoming-mailbox precheck hoist after paired comparison: a small
	     candidate hoisted `TupleSinkServiceLocalMailboxMayHaveMessage()` ahead of
	     `TupleSinkServicePumpIncomingPeerMailbox()` in the active incoming
	     connection loop so empty incoming mailboxes would skip the full consume
	     helper frame. The candidate binary
	     `72be0cdcec9cc16c43e3f8588611015b1cf5d456413999e1eb967c90685c448f`
	     passed c1 after warmup (`4590.594698 TPS`, p50 `0.215 ms`, p99
	     `0.238 ms`) and basebackup (`6.04s` first, `4.20s` warm). c4 passed but
	     sat in the current elevated tail band (`10959.651768 TPS`, p99
	     `0.615 ms`; warmed `11068.763865 TPS`, p99 `0.605 ms`). To separate code
	     effect from host/run variance, the hoist was reverted and the paired
	     baseline binary
	     `49ef329164bf959832fe004669dc660c2a3c485d14cdf2e3b1728c48d6829371`
	     was reinstalled immediately; baseline c4 showed the same band
	     (`7014.939717 TPS` with the known first-run max outlier, p99 `0.594 ms`;
	     warmed `10980.645788 TPS`, p99 `0.599 ms`). Because the hoist did not
	     produce a clear normal-build win and is only a micro-optimization around a
	     still-broad active peer-control source, it was left reverted. Future work
	     should split/budget active recv-CQ polling and mailbox probing more
	     explicitly rather than only hoisting the existing empty check.
	   - first implementation slice: add fixed-table source refs plus source core /
	     executor-feedback state and stack-local `HomerProgressResult`, then make the
	     scheduler-integrated service loop call scheduler-facing executors directly.
     This is now mostly landed for the service-progress boundary: executor return
     values at the service-loop boundary are success/failure only, progress is
     reported through the result object, fixed source feedback is updated after
     every bounded grant, coarse ready-set construction derives current
     eligibility, ready-set overflow is accounted with rate-limited diagnostics
     that include the first dropped source identity instead of silent truncation,
     and an explicit fixed-priority policy feeds the
     scheduler-integrated dispatch loop. Opt-in non-fixed service-progress
     policies now include round-robin over the ready set and unblocked-first
     ordering from per-source feedback hints. Payload streams are now individually
     visible ready sources with conservative sender-side idleness filtering.
     Selected payload grants now validate stream-table slot/generation before
     pumping, selected completion-ring grants now validate session-table
     owner/generation before mailbox consumption, target-completion waits now
     validate their stamped target session owner/index/generation before mailbox
     consumption, and backend completion rings are now visible as per-session
     ready sources. The
     aggregate remote client SQL command source now reports command-forward
     WR/byte/item facts. Lane send-CQ drain now exposes CQE progress through the
     result object rather than a bool return.
     The aggregate local-control source now reports async-continuation and
     handled-slot progress directly, and the aggregate peer-control source now
     reports its lower helper's coarse RDMA progress bit through the result
     object. The delayed peer-client completion publication retry path now
     reports actually published completions directly as well. The aggregate
     remote client SQL command pump no longer returns a bool and now accounts
     for its optional immediate-doorbell progress in the result object. The
     target-completion source now lets the mailbox consumer mark processed
     completion progress directly. Local-control async continuation progress now
     reports touched async-op counts directly. Command-send CQ checkpoint
     retirement now reports the number of command-source slots actually released,
     so command-source credit is no longer reduced to one bool or one item per
     CQE. Peer-control transport pumping now reports typed control/setup/event
     facts rather than one coarse lower-helper bool. The dead service-side
     command immediate branch and last bool-to-result progress wrapper are gone,
     and outgoing payload senders now report posted WR/byte counts plus
     completion, source-release, semantic-object, and remote-credit frontiers
     through `HomerPayloadProgressDelta`. Incoming payload and local-blackhole
     consumers now report ACK CQEs, source/frontier consumption, semantic-object
     completion, local backpressure, ACK WR posting, and remote-credit frontiers
     through the same delta/result path. Backend completion mailboxes are now
     represented as per-session ready sources. Remote client SQL command sources
     are now appended to the coarse ready set per session when that session's
     frontend command mailbox has published work; signaled command-write
     completions remain eligible through the separate CQ-drain source. The
     command executor now accepts a selected session source and pumps only that
     fixed session slot after validating the session-table slot/generation,
     while preserving the aggregate command scan as a defensive fallback. The
     command path has an explicit stack-local
     command-progress delta that records source-slot release, command forwarding,
     WR/byte posting, command/source frontiers, and the remaining ready command
     count before merging into `HomerProgressResult`. Command source feedback now
     records per-session ready command count, outstanding command-write count,
     source-credit pressure, and source-release pressure; the ready command count
     is carried through the command delta/result from the command pump instead of
     rereading mailbox publish/consume epochs during feedback update. The unused
     local START/CLOSE compatibility wait now derives completion-mailbox idleness
     from `HomerProgressResult`, removing one more bool-shaped progress dependency
     at the local-control boundary. The aggregate completion-ring scan now counts
     consumed completions from each mailbox helper's typed result instead of from
     the helper's bool return, and the mailbox helper itself no longer has a bool
     return. The public tagged send-CQ drain helper now reports progress through
     `cqesDrained` only, and the internal tagged send-CQ drain helper now uses
     that same CQE-count progress fact instead of a bool out-parameter. Persistent
     peer event drains now report `eventsDrained` counts instead of bool progress,
     the peer-control pump adds those counts directly to event-drain feedback,
     and outgoing peer-control response consumption now reports exact
     `responsesConsumed` counts without a separate `responseProgress` bool. RDMA
     connection setup helpers now report exact `setupSteps` counts for completed
     state-machine transitions and bootstrap CQEs instead of a coarse setup
     progress bool. Incoming peer-control mailbox pumping now reports exact
     `responsesConsumed` and `requestsDispatched` counts, removing the last
     `madeProgress`/`mailboxProgress` reference from the RDMA peer transport
     implementation. The compatibility-session close flush loop now also uses a
     local `HomerProgressResult` for bounded retry/idleness instead of a separate
     `madeProgress` bool, while remaining outside the hot scheduler boundary.
     The live command-startup wait loops and main service loop now also derive
     idle/relax decisions directly from stack-local `HomerProgressResult`
     objects instead of separate `madeProgress` locals. Payload-delta merge now
     derives scheduler item counts from explicit payload facts instead of a
     payload-specific made-progress predicate. Owner-polled payload and
     receiver-head-ACK completion polling now reports counted completions into
     payload CQE progress instead of a bool found flag. Owner-polled command send
     completion polling now separates stashed completion availability, actual
     send-CQ drain counts, and command source-slot retirement through a typed
	     result object instead of a bool found flag. Basebackup receive-side
	     semantic-object completion now reports an object count instead of an
	     `objectCompleted` bool. Internal RDMA setup polling now reports completed
	     CM events and bootstrap CQEs as counts before the setup state machine folds
	     them into `setupSteps`. Disabled-by-default service-progress diagnostics
	     can now be compiled in to compare ready-set/source-plan/grant outcomes
	     across policy experiments without changing normal performance builds.
	     Heartbeat readiness is now coarsened by a compile-time pass interval and
	     heartbeat execution participates in grant-finish accounting. Local-control
	     readiness now reflects active async continuations or concrete REQUEST_READY
	     control slots instead of being permanently scheduler-ready. A receive-side
	     payload periodic-poll gate was tried and rejected because it broke c4
	     transport liveness. The landed replacement is an exact queued-doorbell
	     predicate: incoming peer-bound payload streams remain ready for already
	     pending stream work or already queued data doorbells, without consuming
	     the doorbell during readiness.
	     Peer-control idle polling now gates scheduler readiness with the existing
	     idle interval instead of granting every pass just to skip inside the
	     executor, while active peer/payload states remain intentionally broad.
	     A transport-private active-readiness narrowing was tried and rejected
	     because basebackup close/lifetime progress timed out even though c1/c4
	     pgbench passed, so future peer-control refinement must validate
	     basebackup close as a required gate.
	     A later diagnostic-only run combined service-progress and peer-transport
	     stats and showed that, in the recorded short c4 diagnostic, peer-control
	     empty grants were dominated by active-connection recv-CQ and control-
	     mailbox probes with no visible CQE or mailbox request, not idle-listener
	     polling. That diagnostic did not change normal readiness behavior. It
	     supports, but does not by itself prove, the next design direction:
	     peer-control refinement should expose basebackup-close-safe lifetime facts
	     and phase budgets before another active-readiness narrowing is attempted.
	     The peer transport now keeps outgoing/incoming active-prefix scan limits,
	     so the top-level peer-control pump no longer walks the cold unused suffix
	     of the fixed 64-slot outgoing and incoming connection tables on every
	     active peer-control grant.
	     The peer-control mailbox path now also has an empty-path precheck that
	     preserves queued control doorbells and the visible-tail fallback while
	     avoiding the full fixed-width message consume/validate path when neither
	     signal is present.
	     Command send-CQ scheduler readiness now also mirrors the existing
	     owner-polled command completion throttle, so low-pressure signaled
	     command WRs do not keep the aggregate `cq-drain` source ready on every
	     service pass while payload CQ readiness remains exact.
	     Active peer-control readiness is now bounded by an active interval rather
	     than permanently ready once peer work exists, preserving periodic
	     recv-CQ/mailbox/disconnect progress while eliminating most broad empty
	     peer-control grants.
	     Local-control async continuation readiness is now similarly periodic for
	     pending continuations, after diagnostics showed nearly all async
	     continuation touches were pending polls rather than completed responses.
	     Completion-ring sources now also feed remaining mailbox backlog into
	     per-source feedback, so ready-hints policies can see completion pressure
	     without rereading every mailbox or changing default fixed-priority order.
	     Payload readiness diagnostics now split scheduler-visible payload sources
	     into transport-broken, completion/ACK-completion, close, local blackhole,
	     incoming doorbell, and outgoing work reasons; the latest short c4
	     diagnostic showed completion/ACK-completion readiness dominated that
	     run's payload empty-grant population. The attempted direct removal did
	     not produce clean evidence, so no conclusion from that run is kept here.
	     Until a cleaner CQ-drain/payload ordering design is validated, the current
	     code keeps that source scheduler-visible while completions are outstanding.
	     A follow-up audit still finds live boolean outputs
	     such as outgoing connection `readyOut`, tagged-CQE `handled`, mailbox
	     `hadMessage`, payload `headerReady`, descriptor-builder `appended`, and
	     basebackup terminal `baseBackupEnd`, but these are readiness/status/parser/
	     builder/terminal predicates rather than scheduler-progress facts.
	     The `ready-hints-first` progress policy and the fixed-critical-plus-data-
	     round-robin policy shape are explicitly not landed after the rejected
	     follow-ups above.
	     At this audit point, the remaining scheduler-ready work narrowed to
	     policy/refinement: sharpen coarse readiness for the still-broad active
	     peer-control and overly-broad payload sources before trying another
	     reordering policy, tune or replace the pass-count heartbeat gate only if a
	     later liveness experiment requires it, use the existing per-source facts to
	     refine command/payload/completion scheduling, and only add more research
	     policies over the ready set after those safety constraints are explicit.
	     Current completion audit on June 3, 2026: the source/result vocabulary,
	     fixed registry, ready-set/source-plan boundary, executor validation,
	     command/completion per-session sources, CQ-drain source, payload egress
	     grant/facts hooks, opt-in progress/egress policies, diagnostic counters,
	     active-prefix transport scans, mailbox empty precheck, command-CQ
	     readiness coarsening, active peer-control interval, local-control async
	     interval, and completion backlog feedback are implemented in the current
	     worktree
	     ([source/result types](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1182),
	     [fixed registry](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5477),
	     [payload executor](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4691),
	     [CQ-drain executor](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5023),
	     [command executor](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5156),
	     [completion executor](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5230),
	     [peer-control executor](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5417),
	     [peer transport progress facts](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:242),
	     [peer transport pump](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7253)).
	     `git diff --check` is clean in both repos, both hosts are running the
	     restored normal service binary
	     `49ef329164bf959832fe004669dc660c2a3c485d14cdf2e3b1728c48d6829371`,
	     and no stale `pgbench` or `postgres: remote exec backend` process was
	     present in the latest process checks. Fresh restored-binary validation
	     after this audit completed c1 `-t 10000` at `10000/10000`, zero failures,
	     `4546.587059 TPS`, p50 `0.216 ms`, p95 `0.230 ms`, p99 `0.244 ms`, max
	     `6.158 ms`; c4 `-t 10000` at `40000/40000`, zero failures,
	     `11126.515153 TPS`, p50 `0.343 ms`, p95 `0.505 ms`, p99 `0.593 ms`;
	     and remote RDMA basebackup at `5.88s` first / `4.41s` warm. Recent
	     farnet1/farnet0 service logs had no failed/error/resetting/broken/RDMA
	     peer-control/completion-drain/timed-out signatures after those runs. The
	     observed `<500 TPS` c1 runs were short `-t 1000` measurements dominated
	     by setup/lifecycle outliers, not the steady-state comparison point. The
	     steady c1 gate remains the longer `-t 10000` run, which returned to the
	     `4.5k-4.6k TPS` band on the restored binary.
	     The recorded evidence does not support another blind active interval tweak
	     or direct payload completion-readiness deletion as the next code slice. A
	     scheduler-safe peer-control refinement now needs an explicit phase-budget
	     or split-source design that
	     keeps setup/CM events, recv-CQ doorbells, control mailbox dispatch,
	     send-CQ/response retirement, async disconnects, and basebackup close/lifetime
	     progress visible separately enough to validate. Required gates for that
	     next slice are: a clean process preflight on both hosts before every
	     candidate/diagnostic run, warmed c1 `-t 10000`, warmed c4 `-t 10000`,
	     remote RDMA basebackup cold/warm close completion, clean service logs,
	     binary hash equality across farnet1/farnet0, and discarding any timed-out
	     or manually cleaned-up run from acceptance/rejection performance evidence.
	   - implement cheap readiness/frontier metadata, policy selection, egress
	     grants, and feedback/progress accounting. Coarse ready-set construction and
	     the fixed-priority source-plan policy are now landed; payload senders now
	     consume a behavior-preserving default egress grant selected at the payload
     executor boundary through an explicit egress policy interface and an
     explicit payload egress-ready-facts handoff that can consume prior
     source-feedback hints without extra frontier reads. A first opt-in
     non-fixed service-progress policy is landed, a first opt-in dynamic egress
     grant policy can size byte/object/WR caps from cheap ready hints, payload
     streams are visible as independent ready sources, and an opt-in pass budget
     can now make those streams compete for one service-pass WR/byte cap. Richer
     research policies and adaptive budget selection remain.
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

   Scheduler design refinement after the June 3 scheduler-ready audit:
   the next scheduler step should explicitly separate two scheduler layers that
   were implicit in the original plan:

   - **Service-progress scheduling** decides how the single service loop spends
     CPU on this pass: which source to inspect, which CQ or mailbox to touch,
     which control phase to progress, and how large the progress grant should be.
     This layer consumes `HomerProgressSourceKind`,
     `HomerProgressSourceCore`, `HomerProgressSourceFeedback`, and
     `HomerProgressResult` facts from
     [`HomerProgressSourceKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1176)
     and the surrounding scheduler metadata in
     `tuple_sink_service_process.c`.
   - **Transport-egress/resource scheduling** decides how much transport work to
     issue once a send-capable source is selected: WR count, byte count,
     semantic-object count, fragmentation budget, and traffic-class/lane
     placement. This layer consumes payload egress ready facts and
     `HomerTransportGrant`-style budgets.

   The existing `HomerTransportTrafficClass` values in
   [`remote_execution_peer_transport_rdma.h`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:52)
   should remain the transport/resource classes for RDMA egress and CQ/resource
   separation. The missing first-layer mirror is a service-progress
   CPU/liveness class carried in the progress-source metadata or derived by the
   policy. Candidate CPU/liveness classes:

   - `HOT_DATA_PROGRESS`: payload streams, command rings, completion rings, and
     CQ drains with cheap ready frontiers. These sources should get most steady
     CPU budget when they have work.
   - `HOT_CONTROL_PROGRESS`: active peer-control work that is required while a
     peer command session or payload/basebackup stream is alive. This is not bulk
     data movement, but it can sit in the warmed service loop and affect latency
     if it burns empty polls or starves required control progress.
   - `COLD_SETUP_MAINTENANCE`: listener polling, RDMA-CM setup/listener
     maintenance, heartbeat-like maintenance, and other transition work. These
     sources should be interval- or event-driven unless they become part of an
     active lifetime.
   - `CLOSE_LIFETIME_PROGRESS`: low-volume but non-starvable progress needed to
     finish peer stream teardown, especially the basebackup close/lifetime path
     that caught the rejected active peer-control readiness narrowing.

   Peer-control is the main source that currently violates this separation.
   The scheduler sees one broad `HOMER_PROGRESS_SOURCE_PEER_CONTROL`, then
   [`TupleSinkServicePumpPeerConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12262)
   and
   [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7315)
   spend CPU across setup, listener/CM events, recv-CQ doorbells, mailbox
   dispatch, send-CQ/response retirement, async disconnects, and close/lifetime
   progress. The next design should split this into phase facts and bounded
   grants before another readiness-narrowing or policy-reordering experiment.
   Candidate split-source or subgrant phases:

   - `PEER_CONTROL_CM_SETUP`: setup/listener/CM transition work; low average
     budget, explicit liveness deadline, not part of the bulk hot data path.
   - `PEER_CONTROL_RECV_DOORBELL`: active recv-CQ doorbell drain for already-open
     peer connections; bounded poll budget while peer sessions/streams are
     active.
   - `PEER_CONTROL_MAILBOX`: dispatch already-visible fixed-width peer-control
     messages; small item budget and direct owner/session/stream pointers.
   - `PEER_CONTROL_SEND_CQ`: retire peer-control response and command/control
     send completions; CQE budget, source-release/frontier feedback.
   - `PEER_CONTROL_CLOSE_LIFETIME`: close/deferred-close/basebackup lifetime
     progress; low volume but non-starvable, and a required validation gate for
     every peer-control readiness change.

   Polling versus event-driven notification should not be mixed into this
   sub-milestone. Some choices are setup-time resource choices: an RDMA completion
   queue is created either with a completion channel or without one, and using
   verbs notifications requires arming and rechecking CQ state correctly.
   Therefore the scheduler cannot dynamically turn a plain polling CQ into a
   notification CQ without transport resources that were created for that mode.
   For the CPU/liveness phase-budget milestone, keep peer-control phases polled
   and let the service-progress scheduler decide which polled phase to service
   now and with what budget: poll a hot CQ for up to N CQEs, skip cold maintenance
   until its interval is due, or run a close-lifetime grant even when it has low
   throughput value. A future notification experiment should first add explicit
   notification-capable phase sources or dual-mode peer-control resources, then
   let the service-progress policy choose between those already-supported phase
   sources. It should not hide notification arming and missed-event ordering
   inside the current aggregate peer-control grant.

   At this refinement point, the facts missing or too coarse for the
   CPU/liveness phase-budget milestone were:

   - per-peer-control-phase readiness and backlog facts before execution, rather
     than only the post-execution `TupleSinkServicePeerPumpProgress` summary
   - phase-specific empty-poll, CQE, mailbox-message, setup-step, response, reset,
     and close-lifetime counters that can feed `HomerProgressSourceFeedback`
   - explicit CPU/liveness class metadata, separate from transport traffic class
   - bounded phase grants for recv-CQ polling, mailbox dispatch, send-CQ drain,
     CM/listener maintenance, and close/lifetime progress
   - validation gates that always include warmed c1, warmed c4, remote RDMA
     basebackup close, clean logs, matching service binary hashes, and stale
     process checks before interpreting performance

   First polling-only implementation slice after this refinement:
   `HomerProgressCpuClass` is now the service-progress CPU/liveness class
   vocabulary beside `HomerProgressSourceKind` in
   [`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1213).
   `HomerProgressSourceCore` now records both the existing transport
   `trafficClass` and the new `cpuClass`, and
   `HomerServiceDefaultCpuClassForProgressSource()` assigns conservative default
   classes for existing sources
   ([core field](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1258),
   [default classifier](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5643),
   [core init](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5680)).
   Peer-control now also has an explicit polled phase-grant substrate:
   `TupleSinkServicePeerPumpGrant` carries a phase mask plus CQ-poll and mailbox
   item budgets
   ([grant type](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:258)),
   `HomerServiceBuildPeerControlProgressPlan()` grants the aggregate
   peer-control source all phases with unbounded per-pass CQ/mailbox budgets for
   behavior preservation
   ([peer grant builder](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5535)),
   `TupleSinkServicePumpPeerConnections()` translates the scheduler grant into
   RDMA peer-pump facts
   ([service adapter](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12473)),
   and `TupleSinkServicePumpPeerRequestsRdma()` applies the phase mask to
   setup/listener, recv-CQ, send-CQ/response, and incoming-mailbox work
   ([RDMA peer pump](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7294)).
   The lower-level helpers now expose the needed accounting/budget hooks:
   `TupleSinkServiceDrainPeerConnectionEvents()` can separately drain recv CQ and
   CM events with a bounded poll-batch count, `TupleSinkServicePumpIncomingPeerMailbox()`
   can bound mailbox dispatch, and `TupleSinkServiceProgressPeerControlResponses()`
   can bound response consumption
   ([event drain](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3658),
   [mailbox pump](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4691),
   [response pump](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6832)).

   This slice is intentionally behavior-preserving at the policy boundary:
   fixed-priority ready-set construction, peer-control readiness intervals, and
   egress grants are unchanged, and the default peer-control grant still polls
   every phase. What changed is that the scheduler has explicit CPU/liveness
   metadata and peer-control phase/budget fields it can start consuming in the
   next policy slice.

   Verification for this slice built `make -j8 service-bin client-bin`, installed
   Citus/Homer, synced the normal service binary and `citus.so` to farnet0, and
   restarted from a clean process baseline. Both hosts ran service binary
   `5014d394605f14d2278a2e50ee504eb271e869cdb987d3d36d15aade33d6ea90` and
   `citus.so` `522a2657742cf7bff017dc8fac3d9d3fdd0c91cfffe53422685d74eeb4c805fd`.
   `git diff --check` was clean in both repos. Remote c1 from farnet0 to farnet1
   completed warmup at `10000/10000`, zero failures, `2368.700559 TPS`, p50
   `0.211 ms`, p95 `0.222 ms`, p99 `0.236 ms`, max `2090.622 ms`; warmed c1
   completed `10000/10000`, zero failures, `4661.574363 TPS`, p50 `0.212 ms`,
   p95 `0.223 ms`, p99 `0.234 ms`, max `5.402 ms`. Remote c4 completed
   `40000/40000` with zero failures in three repeats: `11081.385295 TPS`, p99
   `0.623 ms`; `11073.767905 TPS`, p99 `0.631 ms`; and `11057.198056 TPS`, p99
   `0.628 ms`. Remote RDMA basebackup completed and closed in `6.16s` first and
   `4.20s` warm. Final process checks had only PostgreSQL on farnet1 and one
   `citus_tuple_sink_service` per host, and both service logs had no matching
   failed/error/resetting/broken/RDMA peer-control/completion-drain/timed-out
   signatures.

   Second polling-only implementation slice after this refinement: an opt-in
   `HOMER_PROGRESS_POLICY=cpu-liveness` now consumes the recorded CPU/liveness
   classes. `HomerServiceReadProgressPolicyEnv()` accepts `cpu-liveness`, `cpu`,
   or `liveness`, and logs the active peer-control subgrant caps at service
   startup
   ([policy parser](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3178)).
   `HomerServiceProgressSourceCpuClass()` reads the source core's `cpuClass`
   when available and directly classifies transient target-completion/heartbeat
   refs, `HomerServiceProgressCpuClassRank()` maps classes to scheduler order,
   and `HomerServiceBuildCpuLivenessProgressSourcePlan()` performs stable passes
   over the existing ready set: hot data first, then hot control, then
   close/lifetime, then cold setup/maintenance
   ([class reader](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4016),
   [class rank](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4053),
   [CPU/liveness planner](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4077)).
   Under that policy only, `HomerServiceBuildPeerControlProgressPlan()` now emits
   two peer-control subgrants: a cold setup/listener/close-lifetime grant capped
   by `HOMER_SERVICE_CPU_LIVENESS_PEER_SETUP_POLL_BATCHES`, and an active
   recv-CQ/send-CQ-response/mailbox/close-lifetime grant capped by
   `HOMER_SERVICE_CPU_LIVENESS_PEER_ACTIVE_POLL_BATCHES` and
   `HOMER_SERVICE_CPU_LIVENESS_PEER_ACTIVE_MAILBOX_MESSAGES`
   ([peer cap macros](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:105),
   [phase subgrants](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5570)).
   The default fixed-priority policy remains available and continues to use the
   unbounded all-phase peer-control grant.

   Verification for the CPU/liveness policy built `make -j8 service-bin
   client-bin`, installed Citus/Homer, synced the service binary and `citus.so`
   to farnet0, and restarted both services with `HOMER_PROGRESS_POLICY=cpu-liveness`.
   Both hosts ran service binary
   `669162153b659d4055f56afd57e532a5bbb35324bed60499d8bf6926bb1f8c7d` and
   `citus.so` `522a2657742cf7bff017dc8fac3d9d3fdd0c91cfffe53422685d74eeb4c805fd`.
   Remote c1 from farnet0 to farnet1 completed warmup at `10000/10000`, zero
   failures, `2383.886832 TPS`, p50 `0.207 ms`, p95 `0.219 ms`, p99 `0.234 ms`,
   max `2092.452 ms`; warmed c1 completed `10000/10000`, zero failures,
   `4768.492093 TPS`, p50 `0.207 ms`, p95 `0.217 ms`, p99 `0.228 ms`, max
   `5.870 ms`. Remote c4 completed `40000/40000` with zero failures in three
   repeats: `11444.495627 TPS`, p99 `0.580 ms`; `11317.812086 TPS`, p99
   `0.578 ms`; and `11163.732605 TPS`, p99 `0.601 ms`. Remote RDMA basebackup
   completed and closed in `5.52s` first and `4.27s` warm. Final process checks
   had only PostgreSQL on farnet1 and one `citus_tuple_sink_service` per host,
   and both service logs had no matching failed/error/resetting/broken/RDMA
   peer-control/completion-drain/timed-out signatures.

   Scheduler-ready milestone completion after the CPU/liveness policy slice:
   the original scheduler-ready substrate is now present. The current service
   loop:

   1. builds cheap service-progress ready facts in
      [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4532)
   2. builds a bounded source plan in
      [`HomerServiceBuildProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4194),
      with `fixed-priority` as the baseline and `round-robin`,
      `unblocked-first`, and `cpu-liveness` available as opt-in policies
   3. executes selected source grants through
      [`HomerServiceExecuteProgressSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19590)
      and source-specific bounded executors
   4. requests transport-egress/resource grants for send-capable payload progress
      through
      [`HomerServiceBuildPayloadEgressReadyFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3018)
      and
      [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3077)
   5. merges result/frontier/empty-poll/credit feedback in
      [`HomerServiceFinishProgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4892)
      and
      [`HomerServiceUpdateProgressFeedback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4720).

   The milestone therefore closes as scheduler-ready, not policy-complete.
   What remains is research/engineering on policy quality and protocol cleanup:

   - decide whether `cpu-liveness` should stay opt-in or become the default after
     broader mixed-workload validation
   - design better adaptive/weighted/age-aware policies over the existing
     ready-set and feedback vocabulary
   - optionally split peer-control into true first-class phase sources if the
     aggregate source plus subgrant model is not expressive enough for policy work
   - peer service-to-service push completion is now implemented for the normal
     path; retain `POLL_COMMAND_COMPLETION` only as fallback/debug while mixed
     workloads are broadened
   - keep polling/event-driven notification experiments separate from this
     completed milestone unless the transport resources are explicitly created
     for notification mode.

10. **Push command completions - local frontend and peer normal path completed**
   - keep one in-flight command per session and use a small completion ring for
     the local frontend no-poll implementation
   - bind the local frontend completion destination during session/open setup
   - publish terminal completion from the producing side instead of requiring a
     separate `POLL_COMMAND_COMPLETION` request
   - service-to-service push completions now use a requester-service-owned
     fixed-size peer completion ring on the normal path
   - `terminalCompletionPendingPeerPoll` remains as fallback/debug retirement
     state when peer-ring publication is unavailable or fails
   - narrow or remove peer polling only after broader mixed-workload validation

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
