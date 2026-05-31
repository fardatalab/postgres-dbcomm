# Concurrent Pgbench Client Sessions

## Status

Concurrent `pgbench --homer` clients are supported for the current simple-mode
prototype, with one persistent Homer client SQL session per pgbench `CState`.
The integrated pgbench frontend still rejects unsupported Homer features such as
extended/prepared/pipeline mode, reconnect mode, retry mode, and scripts without
explicit transaction lifecycle commands in
[`validateHomerScriptSupport()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3875),
but it no longer has the old one-client/one-thread guard. Each client opens one
persistent Homer client SQL session in
[`openHomerSession()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:8585).

This note records the root cause of the previous two-client failure, the fixes
that are now landed, and the remaining transport/session work before the command
path can be called generally pipelined or scheduler-ready.

## Observed Failure Classes

The previous two-client experiment exposed two separate bugs.

First, two logical pgbench clients could open the same service command session.
That made two `TX_BEGIN_ATTACH` commands target the same session-owned backend,
which is invalid because one command session currently represents one backend
command mailbox and one backend transaction state.

Second, after SQL command sessions were made non-reusable, the two-client run
could create distinct peer sessions and begin executing both on the remote side,
but the RDMA peer-control transport wedged during concurrent peer
open/establish/disconnect activity.

The current readable service log under `/data/dbcomm/pg-citus/tuple_sink_service.log`
mostly shows the later single-client `CLIENT_SQL_SESSION` path. It confirms the
current guard/workaround shape: command sessions are opened as fresh local
`op=5` sessions and one backend executes one transaction stream at a time.

## Session Reuse Abstraction

The intended abstraction remains: compatibility, intent, and operation specs
are the way Homer determines whether a remote execution session can be reused.
That is the right framework-level design and should not be replaced by ad hoc
client-specific checks.

The refinement is that compatibility has two layers:

- static semantic compatibility: the intent/spec-derived session key says two
  uses belong to the same semantic class
- runtime lease/availability compatibility: the concrete service session says
  whether it is currently available for that compatible use

[`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:174)
is the static session key, and
[`BuildControlSessionKeyFromIntent()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:619)
lowers backend intent into that key. The runtime predicate is currently
[`TupleSinkServiceSessionAllowsReuse()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3514).
It already checks mailbox occupancy, in-flight command state, placement history,
and coarse post-command state, but it does not yet encode a first-class session
lease/owner or full transaction/session state.

This is the gap exposed by concurrent pgbench clients. It is not a rejection of
session reuse through compatibility; it is a missing part of the same reuse
abstraction. A session should be considered reusable only when both the static
intent/spec key and the runtime lease/session state say it is reusable.

That gap is called out in
[`TupleSinkServiceSessionShouldRetireWhenIdle()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4053):
backend/backend SQL command sessions cannot be retired after every command
because typed `TX_BEGIN_ATTACH` needs a persistent backend until `TX_COMMIT` or
`TX_ABORT`. Without a dynamic transaction/session state, an apparently idle
session can still be transaction-attached and owned by a different logical
client.

The current workaround is to allocate a fresh local command session for
command-session opens in
[`TupleSinkServiceHandleOpenCommandSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6664)
and a fresh peer command session in
[`TupleSinkServiceHandlePeerOpenCommandSessionRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8031).
This avoids the collision but is not the final multi-client model.

The final model should treat each pgbench client as a long-lived logical owner:

- each pgbench `CState` owns one persistent `CLIENT_SQL_SESSION`
- global reuse is allowed only when the static compatibility key matches and the
  concrete session lease says the session is released/reusable
- command-session reuse decisions must include runtime lease/session state, not
  only the fixed `CitusRemoteExecSessionKey`
- a session with an active command, pending completion, attached transaction, or
  active result sink is not globally reusable

There is no end-to-end workload today that needs global pooling/reuse of
frontend SQL sessions, so this can remain a future abstraction checkpoint. The
important point is to avoid confusing "compatible in principle" with "currently
available to a different logical owner."

For `CLIENT_SQL_SESSION`, the conservative default is that a frontend session is
owned like a `PGconn`-like semantic object. Even when transaction-idle, it may
later carry session-level state such as GUCs, temporary objects, prepared
statements, portals, or locks. Reuse across logical clients should therefore
require an explicit reset/release state, not merely a completed transaction.

## Root Cause: Peer Control Is Single-Request Transport

The backend/backend peer-control path still assumes one outstanding request on
one peer connection. [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4021)
documents that limitation and enforces it with `waitingForResponse` in
[`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4107).
There is no request-id demultiplexing for multiple in-flight peer-control
requests on the same connection.

The peer command startup path can also be re-entrant. While handling a peer
`START_COMMAND`, [`TupleSinkServiceHandlePeerStartCommandRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8128)
publishes a command to the local socketless backend and then waits in
[`TupleSinkServiceWaitForCommandStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2486).
That wait deliberately pumps service progress because the worker backend may
call back into `OpenRemoteExecutionSession()`. Under concurrent opens, this can
allow a nested peer-control request to try to use the same outgoing peer
connection while the first request is still waiting for a response.

The connection setup side has the same one-channel assumption. A persistent
outgoing connection is found or created by
[`TupleSinkServiceEnsureOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2196),
while incoming connect requests are pumped by
[`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4339).
The transport has connection slots, but the peer-control request/response
mailbox is still serialized per connection and does not yet have a scheduler or
queue that can safely arbitrate concurrent command-session opens.

## Landed Fix For The Pgbench Guard

The pgbench guard was lifted without adding PostgreSQL locks around the backend
wrapper. That matters because locks would serialize at the wrong layer and hide
the transport/session problem.

The fix has three landed parts:

1. The integrated pgbench path creates one persistent `CLIENT_SQL_SESSION` per
   `CState`. That gives every concurrent client its own backend command stream
   and avoids the old cross-client reuse bug.

2. Local client-SQL command/completion traffic moved off the global control-slot
   relay. The service owns cold lifecycle setup, while pgbench and the
   socketless backend exchange hot commands/completions through per-session
   mailboxes.

3. The real farnet0 -> farnet1 RDMA path now uses a 64-slot command mailbox:
   the frontend publishes slot-local `readySeq`, the service forwards directly
   from that registered frontend slot, and the remote backend polls
   `readySeq == expectedCommandSequence`. The checkpoint is documented in
   [client_sql_session_pgbench_checkpoint.md](../../../../implementations/postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md).

The remaining future work is narrower: explicit service-owned lease/session
state for global session reuse, remote receiver-credit frontiers before true
command pipelining, and a multi-slot peer-control request/response substrate for
backend/backend command-session opens. See
[homer_transport_scheduler_and_payload_streams.md](../../citus/transport/homer_transport_scheduler_and_payload_streams.md).

## Multi-Client Scaling Culprit: Shared Local Command Relay

Implementation progress on May 14, 2026: the local `CLIENT_SQL_SESSION`
multi-client scaling fix landed for pgbench. The service now creates the
session mailboxes and spawns the socketless backend during `OPEN_SESSION`, then
the frontend writes normal transaction commands directly to the per-session
backend command mailbox and reads backend completions directly from the
per-session backend completion mailbox. The service remains the lifecycle owner
for session open/close and shared-memory cleanup.

Implementation progress on May 17, 2026: the real two-node RDMA path now uses a
multi-slot command mailbox and direct registered-source forwarding. Warmed
farnet0 -> farnet1 c4/j4 pgbench repeats completed with zero failures around
`10.7k TPS`, average latency `0.372-0.373 ms`, and p99 around `0.63-0.64 ms`.
This fixes the old "all clients serialize through one local control slot"
failure mode for the measured simple-mode workload, but it is still one command
in flight per session rather than arbitrary SQL command pipelining.

The notes below remain the design rationale and the guide for later
service-to-service/DPU transport work.

The single-client performance regression was caused by oversized fixed command
record copying/clearing and has been addressed separately. The multi-client
scaling problem had a different shape: normal client-SQL commands were
serialized through one service-owned local control path before reaching their
per-session backend mailboxes.

Before the local direct-mailbox fix, the command path was host-local shared
memory mediated by one service loop:

```text
pgbench frontend
  -> shared CitusRemoteExecControlRegion slot
  -> single Homer service loop
  -> per-session socketless-backend command mailbox
  -> socketless backend
```

The completion path has the same extra service mediation:

```text
socketless backend
  -> per-session backend completion mailbox
  -> single Homer service loop
  -> per-session frontend completion mailbox
  -> pgbench frontend
```

The old hot serialization points were:

- frontend slot reservation in
  [`HomerClientReserveControlSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:309)
- synchronous local control submission in
  [`HomerClientSubmitControlRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:644)
- service-wide control-slot scanning and dispatch in
  [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8466)
- local `START_COMMAND` dispatch through
  [`TupleSinkServiceHandleStartCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7787)
- backend completion relay in
  [`TupleSinkServiceConsumeCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2208)
  followed by frontend completion publication in
  [`TupleSinkServicePublishClientCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1552)

The landed local fix removes the service from the per-command hot path for
`CLIENT_SQL_SESSION` while keeping it responsible for cold-path lifecycle work.
The normal local path is:

```text
pgbench frontend
  -> per-session command mailbox
  -> socketless backend
  -> per-session frontend completion mailbox
  -> pgbench frontend
```

In the host-local prototype this is shared memory. In the real two-node path,
the frontend-visible command mailbox is a registered RDMA source and the remote
backend-visible mailbox is the destination. The service still creates, names,
maps, registers, and cleans up those channels during `OPEN_SESSION` /
`CLOSE_SESSION`, but it should not route every pgbench statement through a
global control queue.

This is also the right co-design point for the later multiple-QP milestone. The
logical abstraction should be a per-session transport-channel descriptor with
direction and traffic-class metadata. Today that descriptor can name local
shared-memory mailboxes. In the later service-to-service or DPU version, the
same logical descriptor can map to an RDMA queue pair, memory region, or QP pool
chosen by traffic class. The important caveat is to avoid baking "one global
service queue" or "one peer equals one QP" into the session abstraction.

Cold-path lifecycle work that should remain service-owned:

- session open/close and lease state
- socketless backend spawning
- shared-memory / future MR naming and cleanup
- fallback/error cleanup
- RDMA peer setup for remote service traffic

Hot-path command work that should move to session-local channels:

- `CLIENT_SQL_TX_BEGIN`
- `SQL_EXECUTE`
- `TX_COMMIT`
- `TX_ABORT`
- client-visible `STARTED`, result-sink-ready, `COMPLETED`, and `FAILED`
  completions

## Result Sink Caveat

Result sinks are not the current multi-client command-scaling culprit. For local
client SQL, row payloads do not pass through the shared local control-slot queue.
The backend creates or reuses one session-owned result queue in
[`RemoteExecEnsureSessionResultQueue()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:409),
writes tuple batches through
[`RemoteExecSqlDestReceiveSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:774),
and the frontend maps/reuses/drains that queue through
[`HomerClientOpenResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1773)
and
[`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1911).

Therefore the next multi-client scaling work should not redesign result sinks.
The required result-sink adjustment is narrower: when command completions move
directly from backend to frontend, the `STARTED` completion still has to carry
the result-sink descriptor and tuple-view contract. The sink payload path can
remain as-is for now.

Two longer-term caveats remain:

- the current result queue is backend-created POSIX shared memory, not yet a
  service-owned/DPU-owned payload stream
- the current DestReceiver still performs the tuple-view materialization work
  needed by the sink abstraction; that is the correct semantic path for this
  milestone, but not a zero-copy executor-to-transport design
