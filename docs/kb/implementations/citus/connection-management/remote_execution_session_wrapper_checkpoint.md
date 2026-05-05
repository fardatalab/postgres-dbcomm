# RemoteExecutionSession wrapper checkpoint

## Scope

- **What this doc explains**: the earlier wrapper-layer checkpoint that introduced the backend-facing `RemoteExecutionSession` API, its session/batch semantics, and the first route-to-session convergence step.
- **What this doc does NOT cover**: the grounded original Citus control plane, low-level RDMA transport details, or the worker-side final tuple-consume/insert loop.
- **Primary directory**: `docs/kb/implementations/citus/connection-management/`
- **Doc type**: `implementation`

## Current status note

This note is still useful for the original backend-facing `RemoteExecutionSession` wrapper shape, but it is now a historical checkpoint rather than the latest implementation state.

The newer implementation note [local_service_owned_tuple_sink_control_checkpoint.md](local_service_owned_tuple_sink_control_checkpoint.md) records the next step where the standalone service took ownership of tuple-sink open/close, queue naming, and local shared-memory control.

The current canonical implementation note [cross_node_tuple_sink_peer_control_checkpoint.md](cross_node_tuple_sink_peer_control_checkpoint.md) now goes further and covers:

- full tuple-view contract exchange instead of the older digest-only discussion
- service-side semantic receive decode
- batching/backpressure under the session layer
- typed command/completion worker dispatch
- worker-side borrow/use/release instead of the older copy-out-only hot path

## Why this exists

The earlier tuple-route prototype started from the data plane and initially exposed a backend-facing exact-sink bootstrap API around [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:957) and what is now the exact tuple-sink key [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:50). That was enough to bring up the local queue and service loopback path, but it did not match the higher-level control-plane abstraction we settled on later in [remote_execution_session_control_plane.md](../../../future-directions/citus/connection-management/remote_execution_session_control_plane.md).

This checkpoint is the first code convergence step toward that design:

- it adds a backend-visible session-centric API in [`remote_execution_session.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:1)
- it keeps the current tuple-route machinery as the implementation substrate in [`tuple_sink_service.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:957)
- it converts the current sender path in [`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2559) to that session-centric API
- it removes the last direct backend caller of the raw tuple-route open path, so [`tuple_sink_service.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_service.h:1) now acts as an internal substrate header rather than the intended backend-facing control-plane API

So the wrapper is now the canonical backend-facing control-plane shape for the prototype, even though its internals still depend on the sink-local queue/batch substrate and preserved exact-flow bootstrap fields.

## Key code pointers

- backend-facing wrapper types and APIs:
  - [`RemoteExecutionSessionIntentSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h)
  - [`RemoteExecutionOperationSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h)
  - [`RemoteExecTupleSinkOperationSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h)
  - [`RemoteExecutionSessionPrototypeEnabled()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`RemoteExecutionSessionLoopbackValidationEnabled()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`ReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`BorrowRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`CopyOutRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`ReleaseRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
- wrapper internals and current tuple-route mapping:
  - [`ValidateTupleSinkSessionOpenSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`BuildTupleSinkKeyFromOperation()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`InitExplicitCitusTupleSinkKey()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c)
- sender-side integration:
  - session fields in [`CopyPlacementState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:192)
  - send/receive session open in [`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2559)
  - send/poll/copyout validation in [`MaterializeExperimentalRemoteExecutionSessionTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2637)
- historical worker-side bootstrap note:
  - this checkpoint briefly had a worker-side SQL bootstrap consumer for receive-side validation
  - that seam has since been removed from the current tree once the service-owned control path became end-to-end
- underlying tuple-route substrate:
  - [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:957)
  - [`ReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1168)
  - [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1404)
  - [`PollCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1450)
  - [`CitusTupleSinkBatchTupleCount()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1500)
  - [`ReleaseCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1513)
  - [`CopyOutTupleViewFromCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1562)

## Exact behavior implemented now

### 1. Backend-visible API is now session-centric

The current backend-facing control-plane vocabulary is no longer "open a tuple route". It is "open a remote execution session" through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:368).

Internally, [`RemoteExecutionSessionData`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:28) still stores:

- a copied [`RemoteExecutionSessionIntentSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h)
- a copied [`RemoteExecutionOperationSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h)
- one underlying [`CitusTupleSinkHandle`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:31)

So this is a wrapper, not a new transport or a new service implementation yet.

### 2. Session intent is structured, not a single mode enum

The wrapper now exposes the first concrete form of the future control-plane intent object in [`remote_execution_session.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:28):

- [`RemoteExecOpKind`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:28)
- [`RemoteExecAccessKind`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:38)
- [`RemoteExecTxPolicy`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:48)
- [`RemoteExecFreshnessPolicy`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:55)
- [`RemoteExecOwnershipPolicy`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:63)
- [`RemoteExecPlacementScope`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:70)

The current code only materializes the tuple-sink subset, but the wrapper has already moved away from an exact-sink bootstrap-only or single-mode interface.

### 3. The current implementation only supports the tuple-sink/COPY-ingest subset

[`ValidateTupleSinkSessionOpenSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c) rejects any `opKind` other than:

- [`REMOTE_EXEC_OP_TUPLE_SINK`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:33)
- [`REMOTE_EXEC_OP_COPY_INGEST`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:32)

So SQL-command mode, query-result-stream mode, and replication mode are still design-only. This checkpoint is only about re-expressing the tuple-route prototype in the newer session-centric vocabulary.

### 4. The operation spec, not the session intent, still carries bootstrap rendezvous fields

[`RemoteExecTupleSinkOperationSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h) still contains:

- [`CitusTupleSinkTransactionId`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:99)
- `shardId`
- `placementId`
- `relationId`
- `routeOrdinal`

That is not the intended final session-compatibility boundary. It is the current bootstrap bridge to the underlying tuple-route implementation. [`BuildTupleSinkKeyFromOperation()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c) converts the separate `session intent + operation spec` pair into the current deterministic exact tuple-sink identity by calling [`InitExplicitCitusTupleSinkKey()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c).

### 5. Send and receive use one session API but distinct batch lifetimes

The wrapper deliberately separates:

- session lifetime through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:368) and [`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:401)
- batch lifetime through [`RemoteExecutionBatchData`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:35)

Current send-side behavior:

- reserve one send batch with [`ReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:422)
- append tuple views with [`AppendTupleViewToRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:449)
- submit with [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:466)

Current receive-side behavior:

- poll with [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:503)
- inspect count with [`RemoteRecvBatchTupleCount()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:537)
- copy out with [`CopyOutRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:576)
- release with [`ReleaseRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:601)

One important current behavior change from the older tuple-route API is in [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:466): the wrapper submits and then immediately releases the underlying tuple-route send batch. So the public wrapper API does not currently expose a separate send-batch release step.

### 6. Borrow/use/release was added later in the newer checkpoint

At the time of this wrapper checkpoint, tuple access was still copy-out only. That is no longer true in the current tree.

The newer checkpoint [cross_node_tuple_sink_peer_control_checkpoint.md](cross_node_tuple_sink_peer_control_checkpoint.md) now covers the borrowed wrapper [`RemoteTupleView`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:200), [`BorrowNextRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3223), [`ReleaseBorrowedRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3254), and the worker-side borrow/use/release path in [`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:202).

### 7. Sender and worker bootstrap consumer now use the wrapper

On the sender side, [`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c) now constructs explicit send and optional receive `session intent + operation spec` pairs through:

- [`InitRemoteTupleSinkSendSessionIntent()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
- [`InitRemoteTupleSinkSendOperationSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
- [`InitRemoteTupleSinkReceiveSessionIntent()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
- [`InitRemoteTupleSinkReceiveOperationSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)

and then opens sessions with [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c).

This checkpoint also briefly carried a worker-side SQL bootstrap consumer so the receive-side session API could be validated independently. That temporary seam has now been removed from the current tree because the service-owned control path is already complete enough for the tuple-sink prototype.

## Invariants and assumptions

- The wrapper is now the canonical backend-facing control-plane API for the tuple-sink prototype.
- The wrapper still delegates to the tuple-route queue/batch substrate, but open/close ownership is now described more accurately in [local_service_owned_tuple_sink_control_checkpoint.md](local_service_owned_tuple_sink_control_checkpoint.md).
- Backend callers no longer include [`tuple_sink_service.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_service.h:1) directly for the experimental path. The wrapper now owns the prototype gate checks via [`RemoteExecutionSessionPrototypeEnabled()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:350) and [`RemoteExecutionSessionLoopbackValidationEnabled()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:361).
- Session open currently still implies opening the underlying tuple-sink queue substrate, but the newer checkpoint already moved the local backend<->service open/close path into a separate service-owned shared-memory control region.
- Exact tuple-flow identity still depends on bootstrap rendezvous fields in the operation spec, and those should later move behind the service boundary.
- The wrapper assumes the same current homogeneous-node and host-endian tuple batch format as the underlying tuple-route implementation.

## Improvisations and design choices made in code

- The current wrapper reuses [`CitusTupleSinkTransactionId`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:34) as the bootstrap transaction-identity carrier inside [`RemoteExecTupleSinkOperationSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h), instead of inventing a second provisional transaction identity struct.
- [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:466) auto-releases the underlying send batch so the public wrapper already matches the intended session/batch API shape more closely than the underlying tuple-sink substrate API.
- I kept the raw tuple-sink queue/batch functions in [`tuple_sink_service.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_service.h:1) as the wrapper's internal substrate, but removed the old constructor exports from that header and left the older bootstrap constructors commented out in [`tuple_sink_service.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:834) for reference.

## Known gaps / next steps

- This older note still predates the now-landed full tuple-view contract, service-side semantic receive decode, batching/backpressure checkpoint, and worker borrow/use/release path. Read [cross_node_tuple_sink_peer_control_checkpoint.md](cross_node_tuple_sink_peer_control_checkpoint.md) for those current facts.
- The wrapper still has broader unfinished work above that newer checkpoint:
  - non tuple-sink modes are still not implemented
  - route/bootstrap details are still more visible in the wrapper than the long-term abstraction wants
  - cross-transaction backend/session reuse is still not implemented

## Related

- [cross_node_tuple_sink_peer_control_checkpoint.md](cross_node_tuple_sink_peer_control_checkpoint.md): newer end-to-end control-plane checkpoint for the tuple-sink prototype.
- [local_service_owned_tuple_sink_control_checkpoint.md](local_service_owned_tuple_sink_control_checkpoint.md): newer checkpoint where the standalone service owns tuple-sink open/close and queue allocation.
- [remote_execution_session_control_plane.md](../../../future-directions/citus/connection-management/remote_execution_session_control_plane.md): chosen future-direction control-plane abstraction that this checkpoint is validating.
- [connection_management_control_plane.md](../../../citus/connection-management/connection_management_control_plane.md): grounded current Citus control-plane behavior that motivated the wrapper design.
- [local_batch_materialization_checkpoint.md](../tuple-route/local_batch_materialization_checkpoint.md): current tuple-route/data-plane checkpoint that the wrapper delegates to.
