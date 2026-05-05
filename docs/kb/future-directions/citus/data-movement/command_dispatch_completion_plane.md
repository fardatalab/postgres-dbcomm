# Command Dispatch / Completion Plane

## Scope

- **What this doc explains**: the design space for replacing Citus's current worker command start/wait path with a dedicated command dispatch/completion plane, and how that should relate to the already-landed tuple-sink control plane and tuple payload data plane.
- **What this doc does NOT cover**: replacing every Citus libpq command path immediately, generic SQL result streaming, or the existing tuple-sink payload transport details.
- **Primary directory**: `docs/kb/future-directions/citus/data-movement/`
- **Doc type**: `future-direction`

## Why this exists

The current tuple-sink prototype has a split architecture:

- session/sink ownership and peer provisioning are already handled by the tuple-sink control plane through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099), [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2710), and the peer transport under [`TupleSinkServiceCreatePeerTransportState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2542)
- tuple payload already moves over the tuple-sink data path through [`MaterializeExperimentalRemoteExecutionSessionTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2892), [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2256), and [`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2414)
- but worker-side execution startup/completion is still bootstrapped through a SQL-callable worker function launched by [`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2597) and waited by [`FinishExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2705)

That SQL UDF seam is not the target architecture.

The important grounding correction is:

- original Citus already uses a remote command/result path in the distributed COPY workflow
- but it uses [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565) plus [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683), not the parameterized [`SendRemoteCommandParams()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:525)
- the parameterized call only appeared in the current prototype because the temporary worker seam is a SQL UDF with arguments

So the next communication-stack replacement problem is not “more tuple transport.” It is:

- how do we dispatch worker execution directly
- how do we observe completion/error directly
- without going through SQL/libpq command/result machinery

## Grounded current path in original Citus

For the placement-level distributed COPY path, original Citus currently does:

1. [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2321) decides a placement should become active
2. it starts worker COPY through [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4616)
3. [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4616) sends the SQL `COPY ... FROM STDIN` command via [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565)
4. it waits for the worker to enter `PGRES_COPY_IN` via [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683) at the call site in [multi_copy.c:4631](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4631)
5. tuple bytes then move via [`SendCopyDataToPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:1166) and [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:738)
6. COPY is finished via [`EndPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4643)

So the current libpq split is already:

- **command dispatch/completion** over SQL/libpq result state
- **tuple payload** over COPY byte stream

Our current tuple-sink prototype already replaced the second half for the experimental path, and the experimental COPY target now also uses the first typed command/completion slice. This note is about the remaining design space around broadening that replacement and closing the remaining semantic gaps.

## Grounded current path in the prototype

The current experimental path now does this:

1. sender-side tuple-sink open in [`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2829)
2. coordinator starts the worker transaction/work sequence through [`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2634)
3. that function first sends typed `TX_BEGIN_ATTACH` via [`InitRemoteTransactionBeginAttachCommandSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:870), [`StartRemoteExecutionCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2160), and [`WaitForRemoteExecutionCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2244)
4. coordinator then sends typed `COPY_INGEST_FROM_TUPLE_SINK` on that same session and registers the session for later transaction-end finalization through [`RegisterRemoteExecutionSessionForTransactionFinalization()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2362)
5. worker-side service forwards the request into the local command mailbox through [`TupleSinkServiceHandlePeerStartCommandRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4906), spawns a socketless backend if needed via [`TupleSinkServiceSubmitBackendSpawnRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1166), and waits for `STARTED` / terminal completion through [`TupleSinkServiceWaitForCommandStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1549)
6. the socketless backend enters [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:582), applies typed transaction lifecycle in [`RemoteExecBackendExecuteTxBeginAttachCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:396), [`RemoteExecBackendExecuteTxPrepareCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:465), [`RemoteExecBackendExecuteTxCommitCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:502), and [`RemoteExecBackendExecuteTxAbortCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:535), replays the shared raw attach suffix in [`RemoteExecBackendReplayTxBeginAttachText()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:297), and runs the shared receive loop in [`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:201)
7. tuples are published through [`MaterializeExperimentalRemoteExecutionSessionTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2942)
8. statement-end sender cleanup closes only the tuple data plane, waits for COPY completion in [`FinishExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2763), and leaves the session-owned backend alive for xact-end finalization in [`RemoteExecutionSessionsPrepareAtPreCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2405), [`RemoteExecutionSessionsCommitAtCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2481), and [`RemoteExecutionSessionsAbortAtAbort()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2529)

Important correction:

- the experimental COPY target now does use the typed command/completion plane plus the socketless backend bridge
- the tx-end bookkeeping list is not a reuse pool: [`RemoteExecutionTxFinalizationList`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:92) exists because [`CloseExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3075) releases placement ownership before the later xact callback runs, so tx-end control still needs to find session-owned worker backends that already joined the distributed transaction
- 1PC still closes the session at PRE_COMMIT on purpose, because [`RemoteExecutionSessionsPrepareAtPreCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2405) sends `TX_COMMIT` directly when `use2PC` is false, matching original Citus timing
- typed `TX_BEGIN_ATTACH` now does carry the old `SET LOCAL` / savepoint replay surface by sharing [`BuildCurrentTransactionBeginReplayText()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:1137) with the legacy [`StartRemoteTransactionBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:242); the remaining caveat is that the control path still uses a fixed-width replay buffer in [`CITUS_REMOTE_EXEC_TX_BEGIN_ATTACH_REPLAY_BYTES`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:37)

This means the first narrow command plane is already active for the experimental COPY target. The remaining work is not basic worker startup anymore; it is closing the remaining transaction-begin parity gap and then broadening the plane to more workflows.

The still-open boundaries are:

- transaction lifecycle commands now exist too: `TX_BEGIN_ATTACH`, `TX_PREPARE`, `TX_COMMIT`, and `TX_ABORT`
- the current scaffold policy is still one fresh socketless backend per session-owned worker transaction lifecycle
- the current scaffold policy is therefore still transaction-scoped and does **not** yet preserve cross-transaction connection/backend reuse the way ordinary Citus can when a libpq worker session survives commit
- there is still no attach-to-existing-backend path or backend pooling beneath the session layer

### What the current control plane already replaced, and what it did not

Another important correction:

- the current tuple-sink control plane already replaced the **service-owned session/sink open/close and peer provisioning** path for the tuple-sink prototype
- it did **not** replace general Citus worker-backend creation or all `MultiConnection`/libpq usage

That scope is visible in the current prototype call order:

- [`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2775) opens the sender-side tuple-sink session first through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099) at [multi_copy.c:2827](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2827)
- [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099) hands local control to the standalone service via [`OpenTupleSinkSessionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:947)
- the local service then peer-opens the remote sink through [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2710) and [`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1699), specifically at [tuple_sink_service_process.c:2945](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2945)
- only **after** that sender-side service/session open succeeds does the current bootstrap path still launch the worker command through [`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2597) at [multi_copy.c:2859](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2859)

So there is no chicken-and-egg between the current RDMA peer channel and the future command plane:

- the service-to-service tuple-sink control path already exists independently of worker backend startup
- the missing command/completion plane is the next layer that should run **on top of** that already-established service/service communication path

## Correct architectural split

The clean separation should now be:

1. **Remote-execution control plane**
   - open/close compatible sessions
   - open/close exact sinks
   - peer provisioning
   - example grounding: [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2710)

2. **Command dispatch / completion plane**
   - start worker execution for one typed operation
   - optionally acknowledge “started / attached / ready”
   - report completion / error / cancellation
   - this is now the active worker-execution communication plane for the experimental COPY target, but it still needs more command families and fuller parity with `StartRemoteTransactionBegin()`

3. **Payload / result planes**
   - tuple payload ring for `COPY`-style ingest
   - later other bulk traffic classes such as result streaming
   - example grounding for the current tuple class: [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2256)

Important correction:

- the command dispatch/completion plane is **not** the same thing as the session control plane
- and for the current COPY target it is also **not** the same thing as a generic SQL-text data plane

For the current target, what we need first is a **typed worker execution dispatch plane**, not a general “SQL sink.”

## First operation family to support

For the current distributed COPY target, the first command-dispatch operation should be something like:

- `COPY_INGEST_FROM_TUPLE_SINK`

Semantically, that means:

- attach a worker execution context to one receive-direction tuple sink
- drain tuples
- insert into the target shard relation
- report final completion

That is exactly what the shared worker body [`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:201) does today. The old SQL wrapper [`worker_tuple_sink_insert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:394) is now only the compatibility shell for the legacy path, not the active experimental COPY start/wait mechanism.

## Transaction lifecycle belongs in the command plane too

An important design correction is now explicit:

- distributed transaction lifecycle traffic for the worker path should be modeled as a **typed command family on the same session-scoped command/completion plane**
- it should **not** be treated as tuple payload
- it also does **not** need a separate first-class transport plane for the current COPY target

The grounding for that decision is the current Citus worker-connection path:

- COPY enters coordinated transaction mode in [`CitusCopyFrom()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2130) through [`UseCoordinatedTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:341) and [`Use2PCForCoordinatedTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:427)
- worker-side transaction begin is carried over the placement connection by [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:859) and [`StartRemoteTransactionBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:242)
- that begin path also carries coordinator transaction characteristics and current subtransaction / `SET LOCAL` replay state through [`BeginTransactionCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:365) and the now-shared raw replay builder [`BuildCurrentTransactionBeginReplayText()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:1137), consumed by both [`StartRemoteTransactionBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:242) and typed `TX_BEGIN_ATTACH`
- the worker-side distributed transaction id is then attached through [`AssignDistributedTransactionIdCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:407)
- later 2PC prepare is carried over the same worker connection in [`CoordinatedRemoteTransactionsPrepare()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1060)

So the next command families should be narrow, typed transaction-control verbs such as:

- `TX_BEGIN_ATTACH`
- `TX_PREPARE`
- `TX_COMMIT`
- `TX_ABORT`

And the correct session chronology for the current COPY target should become:

1. control plane opens or reuses the `RemoteExecutionSession` plus its sinks
2. the first command on the command plane is `TX_BEGIN_ATTACH`
3. that may trigger cold-path backend spawn if no worker backend is attached yet
4. once the backend acknowledges `STARTED` / ready, the command plane issues `COPY_INGEST_FROM_TUPLE_SINK`
5. tuple payload flows on the tuple sink
6. later `TX_PREPARE` / `TX_COMMIT` / `TX_ABORT` complete the same worker transaction lifecycle that current Citus already uses over `MultiConnection`

### Not the same thing as distributed locking

Another scope correction:

- distributed locking / remote advisory-lock traffic is **not** the same thing as the worker transaction lifecycle for this COPY target
- for the current COPY path, the important preconditions still happen coordinator-side in [`LockShardListMetadata()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/resource_lock.c:651) and [`SerializeNonCommutativeWrites()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/resource_lock.c:704), called from [`multi_copy.c:2122`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2122) and [`multi_copy.c:2128`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2128)
- broader Citus workflows do use remote locking traffic, for example the first-worker path under [`resource_lock.c:339`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/resource_lock.c:339)

That means distributed locking should be noted as a **later typed command family** for wider command-plane replacement, but it is not the first blocker for switching the COPY worker path.

## What the dispatch object should carry

For the first operation kind, the dispatch object should be **typed and fixed-width**, not SQL text.

Recommended first fields:

- operation kind: `COPY_INGEST_FROM_TUPLE_SINK`
- shard id
- placement id
- relation id
- exact sink identity fields needed to attach to the already-open receive sink
- distributed transaction identity

The current bootstrap seam already exposes the likely minimum field set in [`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2597), where it serializes:

- `shardId`
- `placementId`
- `relationId`
- `routeOrdinal`
- distributed transaction identity

### Typed op-spec vs opaque service ids

There are two ways to identify the receive-side object to the worker execution layer:

1. **Typed op-spec replay**
   - the worker dispatcher receives the high-level receive operation spec
   - the worker backend then opens the receive side locally through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099)

2. **Opaque local service-id attach**
   - the worker dispatcher receives the already-allocated local service sink/session ids
   - the worker backend attaches directly to them through a new local API

Recommendation for the first real command plane:

- use **typed op-spec replay**

Why:

- it preserves the current backend-visible `RemoteExecutionSession` boundary
- it avoids leaking service-owned opaque ids upward into worker execution semantics
- it lets the existing local control path keep deciding “find existing sink vs create/attach”

Longer-term, a direct attach-by-id fast path may be worth adding, but it should not be the first command-plane shape.

## What the completion object should carry

For the first operation family, completion should also be typed and fixed-width.

Recommended first completion kinds:

- `STARTED`
- `COMPLETED`
- `FAILED`
- later `CANCELLED`

Recommended first fields:

- operation id / dispatch sequence
- operation kind
- status
- rows inserted for the COPY-ingest operation
- compact error code / SQLSTATE / local status code
- optionally a truncated human-readable detail for logs only

Important distinction:

- `STARTED` means the worker execution context successfully attached and began consuming
- `COMPLETED` means the worker has drained EOS and committed its operation-level work
- `FAILED` means the worker-side execution could not proceed or terminated with error

## Channel design

An important latency refinement is now explicit in [postgres_local_command_dispatch_bridge.md](postgres_local_command_dispatch_bridge.md): the first typed command can piggyback on the slow-path session creation request, but the semantic split should remain “session/channel establishment” vs “command dispatch/completion.” That is, use a fused wire fast path where it helps, without collapsing the abstractions themselves.

### Option A: reuse the existing control channel

Put command dispatch/completion messages on the same current control mailbox/channel used for session/sink open/close.

Pros:

- smallest amount of new transport code
- easiest short-term prototype

Cons:

- mixes worker execution lifecycle with session/sink lifecycle
- makes later scheduling/fairness harder
- current control mailbox is intentionally one-message-at-a-time, which is the wrong shape for sustained execution dispatch

### Option B: dedicated command ring/channel under the same RDMA transport substrate

Keep the same per-peer RDMA transport owner, but add a separate command-dispatch / completion channel.

Pros:

- preserves the traffic-class split we already want
- keeps worker execution messages separate from session/sink control
- scales naturally to more than one operation kind
- still reuses the same RDMA peer bootstrap, QP ownership, registration, and `WRITE_WITH_IMM` publication rules

Cons:

- more implementation work now

### Option C: fully separate transport/QP family for commands

Pros:

- strongest isolation

Cons:

- more transport resources
- likely unnecessary if we already trust the shared peer transport substrate

Recommendation:

- choose **Option B**

That matches the current transport direction in [rdma_transport_control_plane_abstraction.md](../transport/rdma_transport_control_plane_abstraction.md): one shared peer RDMA substrate, separate logical channels by traffic class.

The important local-dispatch refinement beyond this note now lives in [postgres_local_command_dispatch_bridge.md](postgres_local_command_dispatch_bridge.md): the command channel itself should stay typed and shared, but the PostgreSQL-side executor pool should be keyed by execution context such as `(databaseOid, userOid, executionClass)`, not by command kind.

## Do we need a different sink per command type?

Not as the first design.

The right first split is:

- one **typed command-dispatch queue** per worker execution context
- one **typed completion queue** back to the originator
- message headers carry `operationKind`, `operationId`, and status

That is better than “one sink per command type” for the current target, because the first worker COPY-like flow does not need multiple independent command drainers competing on the same backend.

### Why one active command at a time is a good first assumption here

For the original Citus distributed COPY flow, one worker libpq connection/backend is effectively driven as one active placement at a time:

- [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2321) tracks one [`activePlacementState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2407) per [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:159)
- other placements on that same connection are buffered until switch-over in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2428)
- shutdown drains them sequentially in [`ShutdownCopyConnectionState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3339)
- worker COPY startup itself is one command per placement/backend connection through [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4616)

So for the current `COPY_INGEST_FROM_TUPLE_SINK` target, one worker execution context handling one active dispatch at a time is a grounded first model.

Important correction:

- this is **not** a general theorem that a PostgreSQL backend can only ever care about one command kind
- it is only the right first assumption for this current worker COPY placement target
- the grounded serialization point is the current COPY worker execution context represented by [`CopyConnectionState.activePlacementState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:176), not some universal per-backend law

The design should still leave the space open for future multiple command streams or multiple local sinks, but the split should be justified by execution context or concurrency domain, not by operation kind alone. In other words: if we ever add more than one local command sink, it should still preserve the property that a consumer only drains work meant for that execution context, rather than pulling mixed messages and discarding the ones it does not own.

That distinction matters because a PostgreSQL backend may still encounter different higher-level stages over its lifetime, and future operation families may want different local dispatch bridges or concurrency domains. The current COPY target simply does not need that complexity yet.

If later operation families need independent concurrency domains, different drainers, or materially different buffering semantics, then separate command sinks may become justified.

### One typed queue vs per-command-type queues

There are two realistic ways to structure the first worker-local command plane.

1. **One typed dispatch/completion queue per worker execution context**
   - one local drainer owns the queue
   - each message header carries `operationKind`, `operationId`, and status
   - the drainer either handles that kind directly or dispatches internally after decoding the typed header

2. **Separate queues/sinks per command family**
   - each command family has its own local queue and drainer
   - no local demux step is needed once the message reaches PostgreSQL

Recommendation for the current prototype:

- choose **one typed dispatch queue plus one typed completion queue per worker execution context**

Why:

- it matches the current original Citus COPY serialization point in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2321), [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:159), and [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4616)
- it avoids prematurely exploding the local PostgreSQL-side dispatch surface into one sink per possible future command family
- it avoids the “wrong drainer reads the wrong message” problem by construction, because only one local drainer owns the queue for that execution context and the typed header still carries the operation kind explicitly

Why not separate queues yet:

- for the current COPY target we do not yet have multiple independent worker command families competing concurrently on the same execution context
- separate queues only become justified when we can point to a real concurrency domain or a materially different buffering/scheduling requirement

### Recommended first queueing rule

For the current target:

- one inbound dispatch queue per worker execution context
- one outbound completion queue per worker execution context
- typed messages, not separate queues by command type
- initially, at most one in-flight dispatch per worker execution context

That keeps the first worker bridge simple and still leaves room to generalize later.

## Local worker-dispatch problem

The biggest remaining nuance is local execution dispatch on the worker node.

The external service can already:

- receive peer-control/data messages over RDMA
- own sink/session state
- own the transport substrate

What it cannot do directly is:

- jump from an RDMA message into PostgreSQL executor code by itself

So the command-dispatch plane also needs a **worker-local execution bridge**.

### Option 1: keep SQL/libpq start/wait

This is what the current bootstrap does through [`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2597) and [`FinishExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2705).

Pros:

- already works

Cons:

- exactly the communication stack we intend to replace
- requires SQL UDF entry points

### Option 2: integrate into the original worker COPY execution flow

This keeps the **execution semantics** aligned with original Citus's COPY placement flow in [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4616), but replaces the worker-side byte-consumption mechanism.

Pros:

- best near-term integration target for the current COPY workflow
- preserves original placement/transaction structure
- removes the ad hoc worker UDF seam

Cons:

- still requires designing a non-SQL trigger path into that worker execution flow

### Option 3: dedicated in-Postgres dispatcher / background-worker pool

The external service publishes typed dispatch messages into a local shared-memory command queue, and a resident PostgreSQL-side dispatcher receives them and starts the corresponding worker execution directly.

Pros:

- closest to the long-term goal of “RDMA message to execution dispatch directly”
- no SQL UDF boundary
- reusable beyond the current COPY target

Cons:

- biggest new subsystem
- backend lifecycle / transaction / role / error propagation semantics must be designed explicitly

Recommendation:

- **near-term target**: align with original worker COPY execution semantics
- **long-term mechanism**: move toward a local PostgreSQL dispatcher / background-worker style bridge instead of SQL/libpq start/wait

That is the only direction that truly removes the command start/wait libpq path rather than just hiding it.

## Near-term integration target for the current prototype

The near-term target discussed earlier has now partially landed for the experimental COPY path.

What is implemented now:

- preserve the **original worker COPY execution semantics**
- replace the worker-side command start/wait mechanism and the worker-side byte source
- do **not** preserve the current SQL UDF seam as the active worker start/wait path

That is now visible in:

- the existing worker COPY flow in [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4616), [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2321), and [`EndPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4643) remaining the semantic reference
- the experimental worker start/wait path now going through [`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2634) and [`FinishExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2682), which already use the typed command-dispatch/completion plane rather than the old SQL UDF seam

So the near-term progression has changed. It is no longer “design and replace the worker UDF seam” at a purely abstract level. It is now:

1. keep the current experimental COPY path semantically aligned with original worker COPY / distributed transaction behavior
2. keep the shared raw replay path grounded in [`BuildCurrentTransactionBeginReplayText()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:1137) and [`RemoteExecBackendReplayTxBeginAttachText()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:297), and later decide whether a more compact typed representation is worthwhile
3. only then generalize the command plane for broader non-COPY operation families

## Interaction with the tuple-sink path

The command-dispatch plane does not replace the tuple sink. It coordinates it.

For the current COPY target, the intended final flow is:

1. coordinator backend opens the sender-side tuple-sink session
2. worker service already has the peer-provisioned receive sink
3. coordinator side sends one typed `COPY_INGEST_FROM_TUPLE_SINK` dispatch message
4. worker-local dispatcher starts the worker execution path
5. worker execution opens/attaches the local receive-side `RemoteExecutionSession`
6. tuple payload continues to flow over the tuple-sink payload ring
7. worker completion is reported back over the completion channel

So tuple payload and worker execution dispatch are parallel planes:

- tuple plane carries tuples
- command plane carries execution lifecycle

## Error and disconnect handling

Current user scope says we do not need to solve silent remote death yet, but we should handle explicit RDMA shutdown/disconnect.

That should map into the future command plane as:

- peer transport disconnect noticed in [`TupleSinkServiceDrainPeerConnectionEvents()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1887)
- local service marks pending command dispatches/completions as failed
- worker-side receive paths also gain a backend-visible “transport broken” terminal state separate from EOS

Important distinction:

- EOS and transport failure should stay separate
- the current [`RemoteExecutionSessionReceivePeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1329) path is EOS only

## Pitfalls and corrected assumptions

- Transaction lifecycle for the worker path belongs on the **same typed command/completion plane** as work dispatch. Treating it as a separate third “transaction data plane” would lose the simple session-ordered relationship that current Citus gets from one worker connection.
- The control plane and command plane are still different layers even when they share the same overall `RemoteExecutionSession`. Session/sink open through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2018) and worker execution dispatch through [`StartRemoteExecutionCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2168) solve different problems and should not be collapsed conceptually.
- “Session must survive past tuple EOS” is a transaction-lifecycle rule for the current COPY target. [`CloseRemoteExecutionSessionDataPlane()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2120) closes only tuple payload state, while worker transaction finalization later runs through [`RemoteExecutionSessionsPrepareAtPreCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2405), [`RemoteExecutionSessionsCommitAtCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2481), and [`RemoteExecutionSessionsAbortAtAbort()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2529).
- Removing SQL/libpq from the worker start path does **not** mean transaction semantics disappear or should be reinvented. The correct replacement is typed `TX_BEGIN_ATTACH` / `TX_PREPARE` / `TX_COMMIT` / `TX_ABORT`, not an ad hoc local transaction shell around the COPY work command.
- The new path should prefer backend-local helpers over replaying SQL text once the backend is already running socketless. [`SetCurrentDistributedTransactionId()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c:171) is the canonical example: the old SQL UDF [`assign_distributed_transaction_id()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c:145) is only the legacy transport shell.
- Distributed locking is a later command family, not the first blocker for this COPY path. The current COPY preconditions still happen coordinator-side in [`LockShardListMetadata()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/resource_lock.c:651) and [`SerializeNonCommutativeWrites()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/resource_lock.c:704).
- The active experimental implementation is still one fresh socketless backend per session-owned worker transaction lifecycle, not a general session/backend reuse layer. That is a scoped limitation, not the final design target.

## Recommended next concrete steps

1. Keep the shared raw replay serializer/executor path grounded in [`BuildCurrentTransactionBeginReplayText()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:1137) and [`RemoteExecBackendReplayTxBeginAttachText()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:297) until there is a clear reason to replace it with a more compact typed representation.
2. Decide whether the next broadening step should be another command family on the existing session-scoped command plane or a larger reshaping of the command channel itself.
3. Keep the current experimental COPY path as the canonical semantic checkpoint for command-plane work, instead of reintroducing any SQL UDF bootstrap dependency.
4. Treat distributed locking and other non-COPY orchestration traffic as later typed command families, grounded in the existing Citus coordinator-side call paths.
5. Revisit session/backend reuse only after the current command-plane semantics are stable.

## Related

- [tuple_sink_data_plane_completion_plan.md](tuple_sink_data_plane_completion_plan.md): current tuple-sink data-plane plan that now needs a proper command-dispatch replacement for the temporary worker UDF seam.
- [postgres_local_command_dispatch_bridge.md](postgres_local_command_dispatch_bridge.md): long-term PostgreSQL-side dispatcher and executor-pool design beneath the command plane.
- [cross_node_service_to_service_control_path.md](../connection-management/cross_node_service_to_service_control_path.md): completed tuple-sink control-plane direction beneath this future command plane.
- [rdma_transport_control_plane_abstraction.md](../transport/rdma_transport_control_plane_abstraction.md): shared-peer-transport and traffic-class split that the future command plane should reuse.
- [rdma_publication_visibility_and_doorbells.md](../transport/rdma_publication_visibility_and_doorbells.md): responder-visible publication rule the future command channel should also obey.
