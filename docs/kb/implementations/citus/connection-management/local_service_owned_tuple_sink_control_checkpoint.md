# Local service-owned session and tuple-sink control checkpoint

<!-- kb-summary: Earlier local checkpoint for service-owned SHM control, compatibility sessions, and exact tuple sinks. -->

## Scope

- **What this doc explains**: the current implementation checkpoint where the standalone homer service owns the local shared-memory control path, resolves compatibility at a service-session layer, and then manages one or more tuple sinks under that session for the experimental [`RemoteExecutionSession`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h).
- **What this doc does NOT cover**: cross-node control, RDMA transport, or non-tuple operation kinds beyond the current tuple-sink / COPY-ingest subset.
- **Primary directory**: `docs/kb/implementations/citus/connection-management/`
- **Doc type**: `implementation`

## Current status note

This note remains the canonical description of the local-only `session -> sink`
split, but it is no longer the latest end-to-end control checkpoint. The newer
cross-node implementation note
[cross_node_tuple_sink_peer_control_checkpoint.md](cross_node_tuple_sink_peer_control_checkpoint.md)
records the later milestone where sender-side local open performs peer open,
the service carries the full tuple-view contract, receive-side publication is
semantic decode instead of slot-image forwarding, batching/backpressure is
landed under the session layer, and the worker hot path uses borrow/use/release.

## Why this exists

The earlier wrapper checkpoint in [remote_execution_session_wrapper_checkpoint.md](remote_execution_session_wrapper_checkpoint.md) moved backend callers onto the session-centric API, but the service-side control model was still too narrow. The sink was acting as the top-level object, so exact sink-key equality was doing double duty for:

- compatibility / reuse
- queue naming and queue ownership
- backend<->service rendezvous

That was the wrong layering. The current checkpoint fixes that shape for the local prototype:

- backend open/close still use a fixed-width local shared-memory control protocol in [`remote_execution_control_protocol.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h)
- the service now resolves a higher-level compatibility session first, using [`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h)
- the service then finds or creates one exact tuple sink under that compatibility session, keyed by the bootstrap [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h)
- the backend-side tuple-route substrate remains only the local queue/batch implementation under [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c)

So the current ownership split is:

- backend-visible abstraction: `RemoteExecutionSession`
- local backend<->service control IPC: shared-memory control region
- service-owned control objects: compatibility session table above sink table
- local data-plane substrate: tuple-view send/receive queues and batch lifecycle

## Key code pointers

- fixed-width local control protocol:
  - [`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h)
  - [`CitusRemoteExecOpenSessionRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h)
  - [`CitusRemoteExecOpenSessionResponse`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h)
  - [`CitusRemoteExecCloseSessionRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h)
  - [`CitusRemoteExecCloseSessionResponse`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h)
  - [`CitusRemoteExecControlSlot`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h)
  - [`CitusRemoteExecControlRegion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h)
- backend-side control-path client:
  - [`BuildControlSessionKeyFromIntent()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`MapRemoteExecutionControlRegion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`ReserveRemoteExecutionControlSlot()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`WaitForRemoteExecutionControlResponse()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`OpenTupleSinkSessionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`CloseTupleSinkSessionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
- service-owned control objects:
  - [`TupleSinkServiceSessionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`TupleSinkServiceSinkState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`TupleSinkServiceSessionAllowsReuse()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`TupleSinkServiceFindReusableSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`TupleSinkServiceFindSinkBySessionAndKey()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`TupleSinkServiceCreateSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`TupleSinkServiceCreateSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`TupleSinkServiceHandleCloseSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
- queue descriptor handoff into the local tuple-sink substrate:
  - [`CitusTupleSinkQueueDescriptor`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h)
  - [`ValidateTupleSinkQueueDescriptor()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c)
  - [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c)
  - [`ReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c)
  - [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c)
  - [`PollCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c)
  - [`ReleaseCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c)

## Exact behavior implemented now

### 1. Session open/close uses a local shared-memory control region

[`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c) now does three explicit steps:

1. validate and stringify the `session intent + operation spec` pair in [`ValidateTupleSinkSessionOpenSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c) and [`RemoteExecutionSessionOpenSpecToString()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
2. build both:
   - a fixed-width compatibility key through [`BuildControlSessionKeyFromIntent()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c), using only [`RemoteExecutionSessionIntentSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h). This key exists only to answer the service-side open-time question: "do I already have a compatible service session?"
   - an exact tuple-sink key through [`BuildTupleSinkKeyFromOperation()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c), using the separate [`RemoteExecutionOperationSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h)
3. ask the local service to open the session/sink through [`OpenTupleSinkSessionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c), which returns:
   - `serviceSessionId`
   - `serviceSinkId`
   - one concrete [`CitusTupleSinkQueueDescriptor`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h)

The backend then maps only the returned queue descriptor through [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c). It no longer discovers queues or names them itself.

[`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c) closes in the reverse order:

- release the backend-local queue mapping through [`CloseCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c)
- publish a close request carrying both `serviceSessionId` and `serviceSinkId` through [`CloseTupleSinkSessionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)

### 2. The service now resolves `compatibility session -> sink`

The service no longer treats the sink as the top-level owner.

At open time, [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c):

1. validates the fixed-width session key and sink request
2. tries to find a reusable compatibility session through [`TupleSinkServiceFindReusableSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
3. creates a new session through [`TupleSinkServiceCreateSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c) if no reusable one exists
4. finds or creates the exact sink under that session through [`TupleSinkServiceFindSinkBySessionAndKey()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c) and [`TupleSinkServiceCreateSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
5. enforces the per-direction SPSC rule and returns queue descriptors

So the service now owns two levels:

- a compatibility session keyed by [`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h)
- an exact tuple sink keyed by [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h)

One important scope correction:

- for the current tuple-view data path, one session will usually only open one sink per logical tuple stream
- the implementation still keeps the session->sink layering separate because the service model allows more than one exact sink under one compatible session when the ownership policy permits it
- this is a structural property of the control-plane model, not a claim that the current tuple-view workflow needs many sinks to function

### 3. The current compatibility predicate is real, but still local and narrow

The service now has a real compatibility predicate in [`TupleSinkServiceSessionAllowsReuse()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c), but it is still only the local prototype version.

Current behavior:

- exact compatibility match requires equality on the fixed-width session key
- `FORCE_FRESH` disables reuse
- `REQUIRE_CLEAN` only reuses a session when no active sinks remain
- `PIN_EXCLUSIVE` only reuses a session when no active sinks remain
- `SHARE_IF_COMPATIBLE` may reuse a session even while other sinks are active under that session

Important caveat:

- for the current tuple-sink subset, “clean” only means “no active sinks remain under that local service session”
- the service does **not yet** own remote SQL state, remote transaction state, or RDMA transport state
- so current reuse is structurally correct for the control-plane layering, but it is not yet reusing the expensive remote state we ultimately care about

### 4. Sink identity is now distinct from session identity

The local control protocol now carries and returns separate IDs:

- `serviceSessionId`: the higher-level compatibility session
- `serviceSinkId`: the exact tuple sink under that session

That separation matters because close requests must identify the exact sink being closed. Reusing a compatibility session must not collapse multiple exact sink lifetimes into one ambiguous identifier.

### 5. Sink resources, key, id, and descriptor are different things

The current prototype now has four distinct sink-related concepts:

- **sink resources**
  - the actual local send/receive queue pair plus their mapped memory and slot storage
  - currently owned inside [`TupleSinkServiceSinkState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c) and reclaimed by [`TupleSinkServiceResetSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - these resources can be reclaimed and later reused for another sink without preserving the old sink's semantic identity

- **sink key**
  - the current exact open-time compare key for "same sink or different sink?"
  - currently [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h), built by [`BuildTupleSinkKeyFromOperation()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - this is the current tuple-sink-specific active-operation identity; it does **not** describe the higher-level compatibility session
  - used only to find or create the exact sink under an already compatible session

- **sink id**
  - the opaque service-owned long-lived identifier of one opened sink instance
  - currently `serviceSinkId`, returned from [`CitusRemoteExecOpenSessionResponse`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h) and matched during close in [`TupleSinkServiceHandleCloseSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - this is not a POSIX file descriptor and should be treated as pure service-level control identity
  - keeping it separate from the sink key leaves the design space open even though the current prototype often ends up with one stable sink id per exact sink instance

- **sink descriptor**
  - the local queue attachment info for the backend data path
  - currently [`CitusTupleSinkQueueDescriptor`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h)
  - used by [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c) to map the queue, not to decide semantic equality

So the clean interpretation is:

- the **sink key** is for open-time matching
- the **sink id** is for later control references
- the **sink descriptor** is for local data-path attachment
- the **sink resources** are the underlying queue objects that can be reclaimed independently of the old sink identity

### 6. Current session-table lookup is local and linear

Today the local service resolves a session open like this:

1. the backend builds a fixed-width compatibility key in [`BuildControlSessionKeyFromIntent()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
2. the backend publishes that plus the exact sink key through [`OpenTupleSinkSessionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
3. the service linearly scans its in-memory session table through [`TupleSinkServiceFindReusableSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
4. compatibility is exact fixed-width key equality in [`TupleSinkServiceSessionKeysEqual()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c) plus policy checks in [`TupleSinkServiceSessionAllowsReuse()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
5. if no reusable session exists, the service allocates one through [`TupleSinkServiceCreateSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
6. only then does it find or create the exact sink under that session through [`TupleSinkServiceFindSinkBySessionAndKey()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c) and [`TupleSinkServiceCreateSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)

So the current lookup is:

- local to one service process
- linear over a small fixed-size session table
- keyed by logical destination node id plus compatibility semantics, not by host/port
- not yet backed by real remote SQL/RDMA state

### 7. The prototype still enforces SPSC per sink direction

The queue substrate is still SPSC, so [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c) still rejects:

- a second send-side opener on the same sink
- a second receive-side opener on the same sink

What changed is only the ownership layer above that rule. The rule itself remains.

### 8. The data path is still local queue loopback, not RDMA

[`TupleSinkServicePumpSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c) still just copies one published send slot into the receive queue.

So the current split is:

- local backend<->service control IPC: shared-memory control region
- local service-side control model: compatibility session table above sink table
- local tuple-view buffer ownership: send/receive queue pair per sink
- transport: still local slot mirroring, not wire transport and not RDMA yet

## Invariants and assumptions

- `RemoteExecutionSession` remains the only backend-facing abstraction on this prototype path.
- The current local control queue is **not** the final cross-node control plane. It exists only because local backends still need to invoke and coordinate with the standalone local service.
- The tuple-route substrate remains the local queue/batch implementation under the session layer. It is not the intended long-term control abstraction.
- The service resets the shared-memory control region on startup in [`TupleSinkServiceMapControlRegion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c), so stale slots from an older crashed service instance do not survive across service restarts.
- The backend uses PostgreSQL error unwinding in [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c) so partial-open failures still release the claimed control slot and service-owned sink/session state. This is why the backend wrapper uses `PG_TRY`/`PG_CATCH` instead of only logging and continuing.

## Improvisations and design choices made in code

- I renamed the active exact-sink compare object to [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h) once the service-owned session layer was in place. The remaining `tuple_sink_*` names are now limited to the local queue/batch substrate rather than the exact sink identity itself.
- I introduced a fixed-width compatibility key [`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h) in the local control protocol rather than trying to reuse backend structs directly inside the standalone service.
- The current control key is intentionally derived only from [`RemoteExecutionSessionIntentSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h), while the exact tuple-flow bootstrap fields remain in the separate [`RemoteExecutionOperationSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h). That split keeps compatibility/reuse semantics distinct from exact sink identity even before the final service-owned remote state exists.
- I split `serviceSessionId` from `serviceSinkId` in the local control protocol because one compatibility session can now own multiple exact sinks, and close requests need an unambiguous sink identifier.
- I reclaim idle compatibility sessions eagerly only when they are `FORCE_FRESH`, and otherwise keep them around for future compatible opens. This is structural scaffolding for the eventual real reuse story; today it mostly preserves local service-side compatibility state rather than expensive remote state.
- I removed the older socket-based local IPC reference code from both the implementation and the KB. The local backend<->service path is now SHM-only in the prototype.

## Known gaps / next steps

- Rename the remaining internal `tuple_sink_*` vocabulary to `tuple_sink_*` once the service-owned model is less volatile.
- Move beyond the current local-only compatibility semantics so the session layer can own real remote SQL / RDMA state rather than just local control metadata.
- Replace local slot mirroring in [`TupleSinkServicePumpSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c) with the real service-to-service transport path.
- Add the real cross-node service-to-service control path on top of the now cleaner `session -> sink` layering, using the newer point-to-point bidirectional peer-session design in [cross_node_service_to_service_control_path.md](../../../future-directions/citus/connection-management/cross_node_service_to_service_control_path.md), where the common success path uses one optimistic peer-open exchange rather than multiple mandatory application-level control RTTs.
- The later cross-node checkpoint already landed tuple borrow/use/release and worker-side borrow-first insertion; do not treat that as an open item at this older local-only layer.

## Related

- [cross_node_tuple_sink_peer_control_checkpoint.md](cross_node_tuple_sink_peer_control_checkpoint.md): newer checkpoint where the service extends the same local `session -> sink` model across nodes with peer open/close.
- [remote_execution_session_wrapper_checkpoint.md](remote_execution_session_wrapper_checkpoint.md): earlier wrapper-layer checkpoint that introduced the backend-facing session API.
- [remote_execution_session_control_plane.md](../../../future-directions/citus/connection-management/remote_execution_session_control_plane.md): longer-term control-plane design that this checkpoint is converging toward.
- [cross_node_service_to_service_control_path.md](../../../future-directions/citus/connection-management/cross_node_service_to_service_control_path.md): concrete next-step plan for extending this local-only checkpoint into an end-to-end control path.
- [connection_management_control_plane.md](../../../citus/connection-management/connection_management_control_plane.md): grounded current Citus control-plane behavior that motivated the session abstraction.
- [local_batch_materialization_checkpoint.md](../tuple-route/local_batch_materialization_checkpoint.md): queue/batch substrate and tuple-view layout that still sit underneath this control path.
