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

Updated milestone scope:

- for the already-landed experimental COPY target, the above correction still holds: COPY ingest should remain a typed `COPY_INGEST_FROM_TUPLE_SINK` work command plus typed transaction lifecycle commands
- for the next foreground-workload milestone, the target has changed: we want to run pgbench-style transactions through the external service communication stack before we compare interference or performance
- that pgbench milestone should add a **generic SQL-string work command** on the existing session-scoped command envelope, not a tuple sink and not a bulk result-stream plane
- transaction lifecycle must remain typed (`TX_BEGIN_ATTACH`, `TX_COMMIT`, `TX_ABORT`, and later `TX_PREPARE`), while SQL strings represent only the transaction body statements such as `UPDATE ...` and `SELECT ...`

## First operation family to support

For the current distributed COPY target, the first command-dispatch operation should be something like:

- `COPY_INGEST_FROM_TUPLE_SINK`

Semantically, that means:

- attach a worker execution context to one receive-direction tuple sink
- drain tuples
- insert into the target shard relation
- report final completion

That is exactly what the shared worker body [`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:201) does today. The old SQL wrapper [`worker_tuple_sink_insert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:394) is now only the compatibility shell for the legacy path, not the active experimental COPY start/wait mechanism.

## Next operation family: generic SQL command for pgbench transactions

The next concrete milestone is not the basebackup interference benchmark itself and not a performance comparison. It is to get enough of the external service communication stack implemented to run the foreground pgbench transaction workload shape.

The target workload shape comes from the client-private pgbench script:

1. choose `aid`
2. choose `delta`
3. `BEGIN`
4. `UPDATE ... SET abalance = abalance + :delta WHERE aid = :aid`
5. `SELECT abalance ... WHERE aid = :aid`
6. `COMMIT`

For this milestone, `BEGIN` and `COMMIT` should **not** be arbitrary SQL strings. They should remain typed lifecycle commands on the command plane:

- `TX_BEGIN_ATTACH`
- `TX_COMMIT`
- `TX_ABORT` for cleanup
- `TX_PREPARE` only when the higher-level transaction mode needs 2PC

The transaction body should be carried by a new generic SQL-string command, tentatively:

- `SQL_EXECUTE`

Important design correction:

- `SQL_EXECUTE` should use the existing sinkless command plane as an envelope
- it should not introduce a separate tuple-style sink for commands
- the current sinkless command plane is already single-command-at-a-time per session: [`TupleSinkServicePublishLocalCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1603) publishes one `CitusRemoteExecLocalCommandRecord`, and rejects publication while the mailbox is still busy
- [`TupleSinkServiceCommandRequiresSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1036) already models lifecycle commands as sinkless and `COPY_INGEST_FROM_TUPLE_SINK` as sink-backed, so `SQL_EXECUTE` should join the sinkless family

### Why this still needs a new session-only open/bind path

The existing command envelope is sinkless, but current session establishment is still tuple-sink-shaped:

- [`RemoteExecOpKind`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:30) already includes `REMOTE_EXEC_OP_SQL_COMMAND`
- [`ValidateTupleSinkSessionOpenSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:378) still rejects anything except tuple-sink and COPY-ingest operations
- [`CitusRemoteExecControlOpKind`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:73) currently has only `CITUS_REMOTE_EXEC_CONTROL_OP_TUPLE_SINK`
- [`CitusRemoteExecOpenSessionRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:276) carries tuple-sink-specific fields such as direction, slot geometry, tuple-view contract, and sink key
- [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5158) rejects non tuple-sink open requests and then allocates an exact sink
- the peer command endpoint is currently recorded only after a tuple-sink peer binding exists through [`TupleSinkServiceRecordPeerCommandEndpoint()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1553), called from the send-side tuple-sink open path

Therefore the pgbench milestone needs a session-only open/bind path:

1. coordinator backend opens or reuses a `REMOTE_EXEC_OP_SQL_COMMAND` session
2. local service creates/reuses only `TupleSinkServiceSessionState`
3. local service peer-opens or peer-binds the remote compatibility session without allocating `TupleSinkServiceSinkState`
4. both services record the peer command endpoint and peer service-session id
5. subsequent `TX_BEGIN_ATTACH`, `SQL_EXECUTE`, and terminal lifecycle commands use the existing command start/poll path with `serviceSinkId = 0`

This should be a new open/bind operation rather than a fake zero-column tuple sink. A fake sink would unnecessarily allocate queues/rings, require meaningless tuple contracts, and blur the distinction between command lifecycle traffic and tuple payload traffic.

### SQL execution path inside the socketless backend

The worker-side command handler should add a new case in [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:785), beside the existing lifecycle and COPY-ingest cases.

Recommended first handler:

- `RemoteExecBackendExecuteSqlCommand()`

It should require `backendSessionState.transactionAttached` for the pgbench transaction-body path, so body statements cannot run before `TX_BEGIN_ATTACH`.

For executing a generic SQL string, the first implementation should reuse the existing query-string executor path rather than hand-writing parser/planner/executor plumbing:

- [`ExecuteQueryStringIntoDestReceiver()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c:618)
- [`ParseQueryString()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c:632)
- [`RewriteRawQueryStmt()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c:648)
- [`pg_plan_query()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c:682)
- [`ExecutePlanIntoDestReceiver()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c:692)
- `PortalRun()` at [`multi_executor.c:721`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c:721)

This is the non-SPI executor path already present in the tree. It also has a useful first-milestone guard: [`ExecuteQueryIntoDestReceiver()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c:670) rejects `CMD_UTILITY` statements at [`multi_executor.c:675`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c:675). That keeps transaction-control SQL out of `SQL_EXECUTE`; transaction control stays typed.

The first implementation result assertions should remain intentionally narrow, but the result transport should not be narrow:

- `UPDATE`: pass `None_Receiver` and return `QueryCompletion.nprocessed` as affected row count
- row-producing SQL: use a result `DestReceiver` that derives a tuple-view contract from the executor `TupleDesc` and writes returned slots into a result sink
- `SELECT abalance ...`: first acceptance-test shape for that generic tuple-result path, with an assertion that it returns exactly one `(int8)` row; it is not a special result mode or a reason to preserve scalar-only fields
- no generic frontend protocol emulation yet

Important target-design correction:

- command completion has two parts: command/control metadata in the completion mailbox, plus optional tuple payloads in a result sink
- one scalar is a one-column tuple result, not a distinct result kind
- the command response should stay control metadata: command state, SQL command tag, processed row count, result contract, result sink id/readiness, errors, and dynamic session state
- tuple-shaped data results should use a sink-backed result path, reusing tuple-view contracts and tuple batch machinery rather than inventing a second row transport
- the existing `scalarInt64` / `ONE_INT8` shortcut is scaffolding to remove from the SQL-result path, not a target API or acceptable milestone API

For the first SQL-command milestone, use generic tuple-result sinks rather than
an inline public result path:

1. the command spec declares whether row output is expected and may include cardinality/debug expectations
2. the backend executes into a result `DestReceiver` for row-producing SQL
3. `DestReceiver` startup builds or validates a tuple-view contract from the executor `TupleDesc`
4. the backend writes bounded row tuples into a pre-bound or per-command result sink
5. the service forwards command completion with SQL command tag / processed row count plus result-sink readiness / EOS metadata
6. the caller consumes row payload through the same `RemoteTupleView` / sink API used for larger tuple batches

This keeps the command/control channel from becoming a hidden data plane while
still letting it carry PostgreSQL-style command-completion facts. If a
tiny-result optimization later stores row bytes inline in the service or
completion record, it must be hidden behind the sink/result-handle interface so
callers do not branch between inline and sink result locations.

### Protocol payloads to extend

The first patch set should extend the same fixed-width command structs that already carry lifecycle and COPY-ingest payloads:

- backend-facing `RemoteExecutionCommandSpec` in [`remote_execution_session.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:315)
- local control `CitusRemoteExecStartCommandRequest` in [`remote_execution_control_protocol.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:392)
- service-to-backend `CitusRemoteExecLocalCommandRecord` in [`remote_execution_backend_protocol.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:106)
- service-to-service `CitusRemoteExecPeerStartCommandRequest` in [`remote_execution_peer_control_protocol.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:136)

Minimal `SQL_EXECUTE` payload:

- command kind `SQL_EXECUTE`
- SQL byte length
- fixed-width SQL buffer for the first prototype
- result mode: no row result or tuple row result
- optional expected row count / expected column count for first-milestone assertions
- flags for read/write expectation if useful for assertions and logging

Minimal result/control additions:

- processed row count
- SQL command tag / compact command-tag code for `CommandComplete`-style status
- result contract / tuple-view contract for executor-derived row shapes
- result sink id or result handle readiness for row-producing SQL, first exercised by pgbench `SELECT abalance`
- truncated detail string remains for diagnostics, not as a structured result channel
- dynamic session state after the command

The current `scalarInt64` fields should disappear from the SQL-result path rather
than become a public, semi-public, or first-milestone result channel. If a
tiny-result optimization is needed later, it should be hidden behind the
result-sink/result-handle API and still present a tuple contract to the caller.

### First pgbench-facing integration

Status update: the first wrapper-function integration was useful as a backend
command bridge smoke path, but it is no longer the completed foreground
transaction milestone. The completed client-facing milestone is the standalone
no-libpq runner documented in
[`../../postgres/client-sql-session/client_sql_session_offload_plan.md`](../../postgres/client-sql-session/client_sql_session_offload_plan.md)
and
[`../../../implementations/postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md`](../../../implementations/postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md).

The implemented flow is:

1. [`homer_pgbench`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_pgbench.c:1) maps the Homer local control shared memory directly, without `PGconn`
2. [`HomerOpenClientSqlSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_pgbench.c:468) opens a `REMOTE_EXEC_OP_CLIENT_SQL_SESSION`
3. [`HomerRunOneTransaction()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_pgbench.c:851) sends typed `CLIENT_SQL_TX_BEGIN`
4. it sends five `SQL_EXECUTE` commands for the pgbench body statements
5. it sends typed `TX_COMMIT`, or typed `TX_ABORT` on failure if the session may still be attached
6. it verifies transaction correctness with command completion row counts and `pgbench_history`

Transparent execution of the original pgbench script through PostgreSQL planner
or FE/BE protocol interception is still a later milestone. The first completed
milestone is service communication stack correctness for one frontend command
stream, not frontend protocol transparency and not performance comparison.

### Milestone status: single-client pgbench transactions

The first pgbench transaction milestone is now deliberately scoped to **single
client** correctness. Concurrent clients/sessions are not part of this milestone
because they expose peer-command transport behavior that needs a separate design
discussion before we optimize or paper over it.

Current implementation facts:

- [`citus_remote_exec_pgbench_transaction()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/worker/homer/remote_exec_pgbench_transaction.c:171) remains a backend wrapper proof path for `REMOTE_EXEC_OP_SQL_COMMAND`, but it is not the completed client milestone because it is invoked through normal SQL/libpq.
- [`homer_pgbench`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_pgbench.c:1) is now the active no-libpq single-client smoke target.
- [`TupleSinkServiceHandleStartCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6226) has a local `CITUS_REMOTE_EXEC_OP_CLIENT_SQL_SESSION` path that bypasses peer RDMA and publishes commands to a local socketless backend.
- [`RemoteExecBackendExecuteClientSqlTxBeginCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:686) starts the client-owned transaction, while [`RemoteExecBackendExecuteSqlCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:846) executes body SQL through the non-SPI executor helper path.
- The wrapper and no-libpq runner do **not** use advisory locks or other serialization locks. A lock-based workaround would hide the real concurrent-session issue and add unacceptable performance overhead to the path we want to study.

Validation state:

- `make -j8 service-bin`, `make -j8 client-bin`, and `make -j8 extension` completed.
- `sudo -n make install` installed the extension, service binary, and `homer_pgbench` into `/data/dbcomm/pg-citus`.
- After restarting Postgres and the Homer service, one `homer_pgbench` transaction succeeded, followed by a three-transaction repeat.
- `select count(*) from pgbench_history;` returned `4` after those four total offloaded transactions.
- Multi-client pgbench is intentionally deferred.

### Deferred: concurrent command sessions and clients

Two-client pgbench previously exposed two separate classes of problems:

- service/session reuse was too aggressive for SQL-command sessions that keep an
  open remote transaction between `TX_BEGIN_ATTACH` and `TX_COMMIT` /
  `TX_ABORT`; this was addressed by forcing fresh command sessions in the local
  and peer command-session open paths
- the RDMA peer control mailbox is still fundamentally a one-message-at-a-time
  request/response channel, and [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3198) explicitly documents that it assumes at most one outstanding request per peer connection and does not support arbitrary concurrent bidirectional initiation

Do not “fix” multi-client pgbench by adding backend-visible PostgreSQL locks
around the wrapper. That would serialize the benchmark at the wrong layer and
make later performance observations misleading. The real follow-up design space
is one of:

- a multi-slot peer control ring with sequence numbers and independent
  completion routing
- separate peer command connections or traffic classes per concurrent command
  session
- an explicit service-side scheduler/queue that serializes only the current
  one-slot peer transport while preserving clear backpressure and observability

The right choice should be discussed before implementation because it affects
latency, concurrency, connection reuse, and how the base-backup interference
workload will be interpreted later.

### Future fix: explicit dynamic session state

The forced-fresh SQL-command-session workaround exists because the service has
only static compatibility metadata plus coarse post-command retire/reuse hints.
It does not know whether a session with no command currently in flight is:

- transaction-idle and reusable
- transaction-attached and owned by an existing logical command/session
- in failed-transaction state and requiring abort/reset
- terminally failed and needing retirement

The future command/session protocol should add an authoritative dynamic session
state to the service-owned session record:

```c
REMOTE_EXEC_SESSION_IDLE
REMOTE_EXEC_SESSION_COMMAND_ACTIVE
REMOTE_EXEC_SESSION_IN_TRANSACTION
REMOTE_EXEC_SESSION_IN_FAILED_TRANSACTION
REMOTE_EXEC_SESSION_CLOSING
REMOTE_EXEC_SESSION_FAILED
```

This should be reported by the socketless backend at every command boundary and
stored by the service before it considers the session for reuse. The existing
`CITUS_REMOTE_EXEC_POST_COMMAND_STATE_*` values are too coarse: they describe
reuse disposition, not transaction/session state. The replacement should either
extend that report path or introduce a new session-state report field carried
in command results.

Required transitions:

- `TX_BEGIN_ATTACH` success: `IDLE -> COMMAND_ACTIVE -> IN_TRANSACTION`
- SQL body success inside an attached transaction: remain `IN_TRANSACTION`
- SQL body error inside a transaction: `IN_FAILED_TRANSACTION` if abort/reset is still possible, otherwise `FAILED`
- `TX_COMMIT` success: `IN_TRANSACTION -> COMMAND_ACTIVE -> IDLE`
- `TX_ABORT` success: `IN_TRANSACTION` or `IN_FAILED_TRANSACTION -> COMMAND_ACTIVE -> IDLE`
- backend crash, peer disconnect, or unrecoverable protocol mismatch: `FAILED`

Reuse policy once this exists:

- `IDLE`: reusable if the static `RemoteExecutionSessionIntentSpec` compatibility key and freshness/ownership policies allow it
- `COMMAND_ACTIVE`: not reusable and should reject another command unless explicit pipelining exists
- `IN_TRANSACTION`: not reusable by another logical client/session
- `IN_FAILED_TRANSACTION`: only the owning logical session may abort/reset it
- `FAILED`: retire

That state model removes the information gap that forced fresh SQL-command
sessions. It also gives a principled way to diagnose and eventually relax the
current workaround without introducing locks or accidentally sharing a
transaction-attached backend.

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

## What the command response should carry

For the first operation family, the command response should remain typed and
fixed-width. It is command/control metadata plus a pointer to result delivery,
not the result payload abstraction.

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
- result contract id or compact shape code for small results
- result sink id / result handle / result-ready state
- dynamic session state after the command
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
- The sinkless command plane is an envelope, not a queue family per operation kind. The first SQL-command milestone should add `SQL_EXECUTE` to the same one-command-at-a-time session mailbox rather than inventing a command sink per command type. This is a milestone decision based on one command in flight per session; it is not a claim that command sinks will never be useful. Command sinks become worth revisiting for pipelined commands, multiple frontend streams sharing one service context, or separate execution contexts that need independent local drainers.
- A session-only SQL-command open still needs a peer bind step. The existing command start path can be sinkless, but it needs `peerCommandServiceSessionId` and the peer command endpoint to be recorded before `StartRemoteExecutionCommand()` can route the command.
- SQL command tag, processed row count, error status, and dynamic session state belong in the command-completion mailbox. Row payloads belong in result sinks. This means `UPDATE` / `INSERT` do not require command-status tuples in a sink for the first pgbench milestone, while any row-producing SQL, first exercised by `SELECT abalance`, uses a generic tuple result sink with an executor-derived contract. The implementation should not special-case `(abalance int8)` beyond acceptance-test assertions.
- Generic SQL execution does not require SPI specifically. The existing non-SPI executor helper [`ExecuteQueryStringIntoDestReceiver()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c:618) is the preferred first implementation path because it already parses, rewrites, plans, and runs a single non-utility query through a `DestReceiver`.
- Generic SQL execution should still reject transaction-control SQL for this milestone. Lifecycle stays typed; `SQL_EXECUTE` is for transaction body statements.
- “Session must survive past tuple EOS” is a transaction-lifecycle rule for the current COPY target. [`CloseRemoteExecutionSessionDataPlane()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2120) closes only tuple payload state, while worker transaction finalization later runs through [`RemoteExecutionSessionsPrepareAtPreCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2405), [`RemoteExecutionSessionsCommitAtCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2481), and [`RemoteExecutionSessionsAbortAtAbort()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2529).
- Removing SQL/libpq from the worker start path does **not** mean transaction semantics disappear or should be reinvented. The correct replacement is typed `TX_BEGIN_ATTACH` / `TX_PREPARE` / `TX_COMMIT` / `TX_ABORT`, not an ad hoc local transaction shell around the COPY work command.
- The new path should prefer backend-local helpers over replaying SQL text once the backend is already running socketless. [`SetCurrentDistributedTransactionId()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c:171) is the canonical example: the old SQL UDF [`assign_distributed_transaction_id()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c:145) is only the legacy transport shell.
- Distributed locking is a later command family, not the first blocker for this COPY path. The current COPY preconditions still happen coordinator-side in [`LockShardListMetadata()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/resource_lock.c:651) and [`SerializeNonCommutativeWrites()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/resource_lock.c:704).
- The active experimental implementation is still one fresh socketless backend per session-owned worker transaction lifecycle, not a general session/backend reuse layer. That is a scoped limitation, not the final design target.

## Recommended next concrete steps

Completed for the single-client foreground transaction checkpoint:

1. Add a session-only command path so the service can open command sessions without allocating tuple queues, tuple contracts, or exact sink state.
2. Add `SQL_EXECUTE` to the existing single-command-at-a-time command envelope across the local service and socketless backend path.
3. Add `RemoteExecBackendExecuteSqlCommand()` under the socketless backend command loop, require an attached transaction for pgbench body statements, and execute SQL through the non-SPI executor helper path.
4. Add a no-libpq pgbench-style frontend path, `homer_pgbench`, that drives typed begin, five `SQL_EXECUTE` body commands, and typed commit through the service.

Still recommended next:

1. Replace the current row-count-only `SELECT abalance` client smoke with generic sink-backed tuple-result delivery. `SELECT abalance` should become the first one-row `(int8)` acceptance case for a result sink, not a discarded result checked only through `processedRowCount`.
2. Extend completion metadata with SQL command tag, SQLSTATE/error structure, result sink id/readiness/EOS, and dynamic session state.
3. Add explicit dynamic session state to command results / post-command reporting so the service can distinguish `IDLE`, `COMMAND_ACTIVE`, `IN_TRANSACTION`, `IN_FAILED_TRANSACTION`, and `FAILED`. The current command-session retirement rule treats `FORCE_FRESH` as open-time allocation only and retires after terminal commands/failure; it is a workaround until this state is authoritative.
4. Factor the private `homer_pgbench` frontend helpers into a reusable client-side Homer API.
5. Keep the shared raw replay serializer/executor path grounded in [`BuildCurrentTransactionBeginReplayText()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:1137) and [`RemoteExecBackendReplayTxBeginAttachText()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:297) until there is a clear reason to replace it with a more compact typed representation.
6. Keep distributed locking, basebackup, transparent frontend SQL routing, broad result-streaming features beyond executor-derived tuple sinks, and session/backend reuse out of the already-completed single-client transaction checkpoint.

## Related

- [`../../postgres/client-sql-session/client_sql_session_offload_plan.md`](../../postgres/client-sql-session/client_sql_session_offload_plan.md): companion client-to-PostgreSQL transaction offload plan that reuses the Homer command/session ideas under `REMOTE_EXEC_OP_CLIENT_SQL_SESSION` instead of treating frontend traffic as a separate libpq-shaped stack.
- [tuple_sink_data_plane_completion_plan.md](tuple_sink_data_plane_completion_plan.md): current tuple-sink data-plane plan that now needs a proper command-dispatch replacement for the temporary worker UDF seam.
- [postgres_local_command_dispatch_bridge.md](postgres_local_command_dispatch_bridge.md): long-term PostgreSQL-side dispatcher and executor-pool design beneath the command plane.
- [cross_node_service_to_service_control_path.md](../connection-management/cross_node_service_to_service_control_path.md): completed tuple-sink control-plane direction beneath this future command plane.
- [rdma_transport_control_plane_abstraction.md](../transport/rdma_transport_control_plane_abstraction.md): shared-peer-transport and traffic-class split that the future command plane should reuse.
- [rdma_publication_visibility_and_doorbells.md](../transport/rdma_publication_visibility_and_doorbells.md): responder-visible publication rule the future command channel should also obey.
