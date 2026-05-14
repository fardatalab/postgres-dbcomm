# Concurrent Pgbench Client Sessions

## Status

Concurrent `pgbench --homer` clients are intentionally unsupported in the
current milestone. The integrated pgbench frontend rejects Homer mode unless
`nclients == 1` and `nthreads == 1` in
[`pgbench.c`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:7926).
The single-client path opens one persistent Homer client SQL session per
`CState` in
[`openHomerSession()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:8585).

This note records the root cause of the previous two-client failure and the
required design work before lifting that guard.

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

## Required Fix Before Lifting The Pgbench Guard

The next milestone should not add PostgreSQL locks around the backend wrapper.
That would serialize at the wrong layer and hide the transport/session problem.

The correct fix has two parts:

1. Extend the remote-execution session reuse predicate with explicit
   service-owned lease/session state. A `CLIENT_SQL_SESSION` should be owned by
   one frontend client until explicit release/reset/close, while backend/backend
   SQL command sessions should be reusable only when the service knows they are
   transaction-idle and unowned/releasable. This extends the existing
   compatibility abstraction rather than bypassing it.

2. Make peer control concurrency-safe using the target substrate, not a
   temporary serialization shim. The desired direction is a multi-slot
   RDMA-registered peer-control ring/queue with sequence numbers, session ids,
   response routing, and traffic-class scheduling. See
   [homer_transport_scheduler_and_payload_streams.md](../../citus/transport/homer_transport_scheduler_and_payload_streams.md).

Once those are in place, the pgbench frontend guard can be relaxed from exactly
one client/thread to one Homer session per client, and multi-client correctness
can be validated before any throughput comparison.

## Multi-Client Scaling Culprit: Shared Local Command Relay

Implementation progress on May 14, 2026: the local `CLIENT_SQL_SESSION`
multi-client scaling fix has landed for pgbench. The service now creates the
session mailboxes and spawns the socketless backend during `OPEN_SESSION`, then
the frontend writes normal transaction commands directly to the per-session
backend command mailbox and reads backend completions directly from the
per-session backend completion mailbox. The service remains the lifecycle owner
for session open/close and shared-memory cleanup.

The notes below remain the design rationale and the guide for later
service-to-service/DPU transport work.

The single-client performance regression was caused by oversized fixed command
record copying/clearing and has been addressed separately. The multi-client
scaling problem had a different shape: normal client-SQL commands were
serialized through one service-owned local control path before reaching their
per-session backend mailboxes.

For the current pgbench client-SQL path, this is not yet "multiple sessions
queued before RDMA writes to different remote memory regions." The command path
is host-local shared memory:

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

The current hot serialization points are:

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

The planned fix is to remove the service from the per-command hot path for
`CLIENT_SQL_SESSION` while keeping it responsible for cold-path lifecycle work.
The target normal path is:

```text
pgbench frontend
  -> per-session command mailbox
  -> socketless backend
  -> per-session frontend completion mailbox
  -> pgbench frontend
```

In the host prototype this is still shared memory, but the abstraction should be
a session-local command/completion transport channel. The service should create,
name, map, and clean up those channels during `OPEN_SESSION` / `CLOSE_SESSION`,
but it should not scan a global control queue or copy command/completion records
for every pgbench statement.

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
