# Client SQL session offload plan

## Scope

- **What this doc explains**: the target abstraction and implementation plan for replacing the simple-mode pgbench transaction workload's libpq/socket communication with Homer typed client-session commands carried by the external service.
- **What this doc does NOT cover**: prepared-mode pgbench, concurrent client/session scaling, PostgreSQL wire-protocol compatibility, basebackup, or production-grade performance conclusions. It records current single-client comparison checkpoints only as implementation evidence.
- **Primary directory**: `docs/kb/future-directions/postgres/client-sql-session/`
- **Doc type**: `future-direction`

## Implementation status

The simple-mode milestone is now implemented and smoke-tested through the
integrated `pgbench --homer` path with persistent Homer client SQL sessions. The
canonical implementation checkpoint is:

- [`../../../implementations/postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md`](../../../implementations/postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md)

Completed pieces:

- `REMOTE_EXEC_OP_CLIENT_SQL_SESSION` exists in the backend-facing enum and fixed-width service protocol.
- A frontend-safe client API, [`remote_execution_client.h`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:1) plus [`homer_client.c`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1), opens client SQL sessions, submits typed commands, observes pushed completions, maps/drains result sinks, and returns errors through caller-owned buffers.
- The earlier standalone no-libpq runner has been removed. Integrated
  `pgbench --homer` is the client SQL benchmark entry point.
- Upstream `pgbench` now has an opt-in `--homer` mode in [`pgbench.c`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:274). It reuses pgbench's script/timing/reporting path while preserving the default libpq modes for baseline comparison.
- The local service accepts client SQL command sessions and routes commands to a local socketless backend without peer RDMA.
- The socketless backend handles typed client begin, generic SQL execute, typed commit, and typed abort.
- `pgbench --homer` opens one Homer client SQL session per pgbench client,
  reuses the socketless backend across transactions, and closes the session with
  a typed internal `CLIENT_SQL_SESSION_CLOSE`.
- Runtime smoke testing completed one transaction and then three more transactions against scale-1 pgbench tables. After the client-library split, follow-up one-transaction smoke tests also succeeded. `pgbench_history` reached 6 rows after the combined runs.
- Persistent-session pinned runs completed 3, 1000, and 5000 transactions. The 5000-transaction run reached about `5259 TPS` / `0.190 ms` average latency while increasing `pgbench_history` by exactly 5000 rows.
- `pgbench --homer` pinned runs completed 1000 and 5000 transactions from the same binary used for the libpq baseline. The 5000-transaction Homer run reached about `5214 TPS` / `0.192 ms` average latency while increasing `pgbench_history` by exactly 5000 rows.
- Sink-backed tuple-result delivery for row-producing client SQL is implemented
  for the pgbench path. The built-in TPC-B-like script now sends
  `SELECT abalance` with `CITUS_REMOTE_EXEC_SQL_RESULT_TUPLE`, opens/drains the
  result sink from the frontend, and uses a service-owned byte-ring tuple sink
  parented under the client SQL service session.
- Code-level benchmark placement is implemented: pgbench `--client-cpu`, service `HOMER_SERVICE_CPU`, and backend `HOMER_REMOTE_EXEC_BACKEND_CPU` replace ad hoc `taskset` as the canonical benchmark controls.
- Exact in-process tail-latency reporting is implemented behind pgbench `--latency-percentiles`. For transaction-count runs, the sample vector is preallocated before timing starts.
- A fresh three-run 10k-transaction single-client comparison with code-level pinning measured Homer around `5076 TPS` average versus libpq around `3622 TPS` average. Treat this as a current prototype checkpoint, not a general performance claim.

Still incomplete:

- command completion fields for SQL command tag, SQLSTATE, and explicit dynamic session state
- event/doorbell-style control wakeups or a DPU-oriented control queue/doorbell design; explicit CPU pinning plus raw `pause` spinning is the current single-client benchmark policy
- prepared-mode pgbench, concurrent clients, auth/user-name mapping, cancellation, and basebackup/interference workload integration

## Directional correction

This project is not trying to tunnel PostgreSQL FE/BE protocol bytes through RDMA. The research direction is to offload the communication stack by replacing low-level socket/libpq traffic with higher-level database-semantic objects, the same way the tuple-copy prototype moved from libpq/COPY bytes toward tuple contracts, tuple views, tuple sinks, and typed lifecycle commands.

For the client-to-single-Postgres transaction workload, the corresponding abstraction should be a typed client SQL session:

```c
REMOTE_EXEC_OP_CLIENT_SQL_SESSION
```

The important point is that this op kind is not a libpq compatibility layer. It should carry typed command objects such as transaction lifecycle commands and SQL statement execution requests. If later prepared-mode pgbench is targeted, parse/bind/execute should become typed semantic commands, not forwarded `PqMsg_Parse`, `PqMsg_Bind`, and `PqMsg_Execute` byte messages.

## Grounded current behavior

In stock PostgreSQL, a frontend connects to the postmaster first, but query traffic is handled by the forked backend process:

- `src/backend/tcop/backend_startup.c:246` `BackendInitialize()` initializes the interactive postmaster-child backend.
- `src/backend/tcop/backend_startup.c:282` calls `pq_init(client_sock)`, which installs socket-backed libpq backend state.
- `src/backend/tcop/backend_startup.c:107` then calls `PostgresMain(...)` after startup/auth has completed.
- `src/backend/tcop/postgres.c:386` `SocketBackend()` reads one frontend protocol message type and body through backend `pq_*` helpers.
- `src/backend/tcop/postgres.c:514` `ReadCommand()` chooses `SocketBackend()` when `whereToSendOutput == DestRemote`.
- `src/backend/tcop/postgres.c:4411` `PostgresMain()` owns the backend command loop and dispatches `Query`, `Parse`, `Bind`, `Execute`, and other protocol messages.

On the pgbench frontend side, the normal workload code is also libpq-oriented:

- `src/bin/pgbench/pgbench.c:598` stores a `PGconn *con` per client state.
- `src/bin/pgbench/pgbench.c:3167` sends simple SQL through `PQsendQuery(...)`.
- `src/bin/pgbench/pgbench.c:3178` sends parameterized extended-query SQL through `PQsendQueryParams(...)`.
- `src/bin/pgbench/pgbench.c:3189` sends prepared executions through `PQsendQueryPrepared(...)`.

The current Homer prototype already has remote-execution semantic operation kinds:

- `src/include/distributed/homer/remote_execution_session.h:30` defines `RemoteExecOpKind`.
- `src/include/distributed/homer/remote_execution_control_protocol.h:93` mirrors those kinds into fixed-width service protocol constants.
- `src/backend/distributed/utils/homer/remote_execution_session.c:784` and `src/backend/distributed/utils/homer/remote_execution_session.c:806` initialize tuple-sink send/receive sessions as `REMOTE_EXEC_OP_TUPLE_SINK`.
- `src/backend/distributed/utils/homer/remote_execution_session.c:829` initializes the current sinkless SQL command session as `REMOTE_EXEC_OP_SQL_COMMAND`.

Historical correction: the first pgbench proof of concept did **not** replace frontend/libpq communication. It sent a normal libpq `SELECT pg_catalog.citus_remote_exec_pgbench_transaction(...)` into the local server, and only that backend-side wrapper opened a Homer SQL command session. That wrapper remains useful as a backend bridge smoke path, but it is no longer the completed client SQL milestone.

The current implementation checkpoint replaces the measured workload path with
integrated `pgbench --homer`; the standalone proof-of-concept runner has been
removed:

- [`HomerClientOpenSqlSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:408) opens `REMOTE_EXEC_OP_CLIENT_SQL_SESSION` through the fixed-width control protocol.
- [`HomerClientStartAndWaitCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:737) sends typed commands and waits on pushed completion state.
- [`HomerRunCommandAndWait()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3731) is the integrated pgbench adapter that sends typed Homer commands, drains result sinks while commands are active, and preserves the normal pgbench timing/reporting path.

The backend bridge still uses:

- `src/backend/distributed/utils/homer/remote_execution_session.c:1097` `InitRemoteSqlExecuteCommandSpec()` to package one SQL string in a fixed-width typed command.
- `src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:798` `RemoteExecBackendExecuteSqlCommand()` to execute that SQL string in the socketless backend.
- `src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1070` typed command dispatch for lifecycle commands, COPY ingest, and SQL execute.

The implementation now validates a reusable client-side Homer API shape for the
simple-mode milestone. It is still a narrow API for local shared-memory control
and one command in flight per session, not the future command-pipelined API,
authentication/session-state layer, or DPU-oriented control transport.

## Why `REMOTE_EXEC_OP_CLIENT_SQL_SESSION` is separate

The current `REMOTE_EXEC_OP_SQL_COMMAND` is a backend-to-backend sinkless command session. It assumes a PostgreSQL/Citus backend already exists and can call the backend-facing remote-execution API.

A client SQL session has different compatibility and lifecycle semantics:

- session identity is frontend-owned: database, user, application/session options, and later client-visible state
- the session maps one client command stream onto one backend execution context
- transaction lifecycle is driven by the frontend workload, not by a Citus coordinator backend's surrounding transaction
- result delivery must be client-facing, even when the first acceptance test only consumes pgbench's one-column `abalance` result
- disconnect/cancel/error boundaries are frontend-session boundaries

The service currently makes the narrower command-session assumption explicit:

- `src/backend/distributed/utils/homer/tuple_sink_service_process.c:5279` `TupleSinkServiceHandleOpenCommandSession()` opens command sessions.
- `src/backend/distributed/utils/homer/tuple_sink_service_process.c:5317` rejects anything except `CITUS_REMOTE_EXEC_OP_SQL_COMMAND`.
- `src/backend/distributed/utils/homer/tuple_sink_service_process.c:5325` documents that current SQL command sessions are fresh because the command plane cannot yet distinguish transaction-idle from transaction-attached reuse state.

`REMOTE_EXEC_OP_CLIENT_SQL_SESSION` should reuse the same service substrate where possible, but its session key, lifecycle, and allowed command set should be explicit rather than hidden inside the older SQL-command op kind.

## Target abstraction

### Session object

The client-side API should expose a Homer client session object, not `PGconn`:

```c
typedef struct RemoteExecutionClientSessionData *RemoteExecutionClientSession;
```

Opening the session should carry at least:

- destination host/node or local service endpoint
- database name
- user name or user OID once auth is scoped
- application name / benchmark label
- protocol version
- single-client/single-command-at-a-time capability flags for the first milestone

On the service/backend side, this maps to a `REMOTE_EXEC_OP_CLIENT_SQL_SESSION` session key. The remote backend can still use the socketless backend initialization helper:

- `src/backend/tcop/backend_startup.c:112` `RemoteExecBackendInitializeConnectionByOid()` gives a remote-exec backend the same database/user initialization entry point without a client socket.

### Command objects

For simple-mode pgbench, the first command set should be deliberately small:

```c
CLIENT_SQL_TX_BEGIN
CLIENT_SQL_EXECUTE
CLIENT_SQL_TX_COMMIT
CLIENT_SQL_TX_ABORT
CLIENT_SQL_SESSION_CLOSE
```

`CLIENT_SQL_EXECUTE` should carry:

- statement text
- statement id / command sequence
- result mode, initially `NONE` or `TUPLE_RESULT`
- optional expected result cardinality for assertions, for example exactly one row for pgbench's `SELECT abalance`
- optional expected command tag or affected-row expectation for debugging

Prepared-mode pgbench is a future step. It should add typed semantic commands such as:

```c
CLIENT_SQL_PREPARE_STATEMENT
CLIENT_SQL_BIND_STATEMENT
CLIENT_SQL_EXECUTE_BOUND
CLIENT_SQL_SYNC
```

These names intentionally describe database semantics, not PostgreSQL wire-message bytes. The backend implementation may reuse or refactor PostgreSQL's existing semantic helpers behind `exec_parse_message()`, `exec_bind_message()`, and `exec_execute_message()`, but Homer should dispatch on its own typed objects.

### Result objects, contracts, and sinks

Target design correction:

- command completion has two parts: a command/control completion header in the mailbox, plus optional tuple payloads in result sinks
- scalar results are tuple results; a scalar `int8` is one tuple with shape `(int8)`
- result streams should use the same tuple-view / sink family as the existing tuple-copy path, with direction reversed when the backend produces rows for a frontend/client
- pgbench's `(abalance int8)` result is only the first acceptance-test shape, not a special result abstraction or a reason to add a scalar-specific fast path

The target result model should be:

```text
RemoteExecutionCommand
  produces
RemoteExecutionCommandCompletion
  over the command/completion mailbox
  optionally names
RemoteExecutionResultContract
  delivered through
RemoteExecutionResultSink
  consumed as
RemoteTupleView / RemoteExecutionResultView
```

For pgbench:

- `UPDATE` / `INSERT` produce command completion metadata in the mailbox, including SQL command tag, processed row count, command state, and dynamic session state. They do not need a result sink unless a higher-level adapter chooses to synthesize a tuple-shaped status row.
- `SELECT abalance ...` produces a data result tuple with shape `(abalance int8)` and expected cardinality 1, using the same generic tuple-result path that should handle arbitrary executor `TupleDesc` shapes.
- Errors produce terminal command-completion metadata with structured status fields plus a bounded detail string for debugging. A structured error result sink can be added later if a client-facing API needs richer diagnostics.

The existing fixed-width completion fields in `RemoteExecutionCommandCompletion` and `CitusRemoteExecCommandCompletion` are the right place for command/control facts, but are currently incomplete for PostgreSQL SQL completion because they do not carry the actual SQL command tag. The target should extend that mailbox completion with PostgreSQL command-tag/row-count/session-state metadata, result sink id/readiness/EOS, and dynamic session state, while moving all row data out of the mailbox and into sinks. The current `scalarInt64` / `scalarIsNull` / `scalarTypeOid` fields and `ONE_INT8` result mode are prototype shortcuts to remove from the client SQL path. They should not survive as public API, internal API, or first-milestone convenience API; the replacement is generic tuple-result sink delivery for every row-producing command, including one-column one-row cases.

#### Sink-backed result delivery

Sinks remain central for row payloads. Tuple-shaped SQL data results, including one-row scalar results, should be delivered through a result sink that reuses the tuple-view contract and tuple batch machinery:

1. the command spec declares whether row output is expected and may include cardinality/debug expectations
2. the backend `DestReceiver` startup receives the executor-provided `TupleDesc`
3. Homer builds or validates a tuple-view contract from that `TupleDesc`
4. the session or command binds a result sink using that contract
5. the backend executor writes result tuples into the sink as tuple batches
6. the client/frontend borrows `RemoteTupleView` objects from received batches

This is the same abstraction family as the tuple-copy path, not an unrelated result-stream API. The difference is direction and ownership: COPY ingest has the coordinator/client side producing tuples and the backend consuming them; query results have the backend producing tuples and the frontend/client consuming them.

For milestone 1, prefer a pre-bound or per-command result sink that can carry any tuple contract derived from the executor `TupleDesc`. The first pgbench verification consumes only one `(abalance int8)` row, but the implementation should not hard-code that shape, field count, or type OID. The caller should always know where to look: command status, SQL command tag, row count, errors, and dynamic session state come from the command completion mailbox; row payloads come from the result sink named by that completion. This preserves the sink abstraction without turning the command mailbox into a generic data plane.

#### Why not command sinks in milestone 1

The first client SQL session should keep commands in the existing one-command-at-a-time mailbox/envelope because the command plane is currently ordered by session lifecycle rather than by payload streaming. That is a deliberate milestone scope, not a claim that command sinks will never be useful.

The distinction is:

- row payloads are streaming data and must use sinks
- command submission and completion are session-control events and can stay mailbox-backed while the milestone is single-client, simple-mode pgbench
- a future command sink becomes justified if we add command pipelining, concurrent in-flight commands on one session, multiple frontend streams sharing one service context, or a different execution context that needs an independent local drainer

So the current plan reuses the sinkless command envelope for commands because it matches the first milestone's one-command-in-flight semantics. It does not weaken the overall sink-centered design: every row-producing command still binds or references a result sink for tuple payloads.

#### Inline result delivery as an internal optimization only

Inline row delivery should not be a public result-location choice. It complicates the clean sink abstraction, forces callers to branch between "inline" and "sink" result locations, and risks turning the command mailbox into a hidden data plane. This does not prohibit command-tag/row-count/session-state metadata in the mailbox; those are command completion facts rather than arbitrary row payloads.

If tiny-result performance later requires an inline fast path, hide it behind the sink interface:

- the command still advertises a result sink / result handle
- the caller still consumes a `RemoteTupleView` from that sink/result handle
- the service may internally back a small-result sink with a fixed slot or inline bytes
- the optimization must not change the public contract or require caller-side inline-vs-sink dispatch

This keeps the control path as command/session control and the result path as the sink abstraction, while preserving room for low-level fast paths inside the sink implementation.

#### Concrete result-sink materialization plan

Status: implemented for the simple-mode pgbench path with service-owned
byte-ring tuple-result queues. The client-SQL result path now uses the same
sink-centered ownership model as Citus backend-backend tuple-copy: the Homer
service allocates/binds the result sink, the socketless backend opens the
send-side descriptor, and the frontend maps/drains the receive descriptor named
by command completion metadata.

The implemented path uses generic sink-backed SQL result materialization and reuses the existing tuple-view contract and tuple-sink mechanics rather than growing the scalar shortcut. The canonical checkpoint is [`../../../implementations/postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md`](../../../implementations/postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md).

Implemented pieces:

- [`RemoteExecOpenServiceOwnedResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:597) requests a service-owned tuple sink under the parent client SQL session once the executor `TupleDesc` is known.
- [`RemoteExecEnsureSessionResultQueue()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:740) binds or reuses that service-owned result sink when the tuple-view contract is compatible, and reopens it when the result shape changes.
- [`RemoteExecSqlDestStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:972) receives the executor `TupleDesc`, builds a tuple-view contract, opens the service-owned send-side sink, and publishes result-sink readiness in command completion metadata.
- [`RemoteExecSqlDestReceiveSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:585) copies executor rows into a receiver-owned virtual slot and appends them to tuple-sink batches.
- [`RemoteExecSqlDestShutdown()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:640) flushes the final batch, marks peer closed/EOS, and closes the backend-local sink handle before executor memory is reset.
- [`HomerClientOpenResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:946) and [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1054) let frontend code map and drain the sink named by completion metadata.
- [`HomerRunCommandAndWait()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3249) opens/drains result sinks for row-producing SQL in `pgbench --homer`.

Existing tuple/sink pieces to reuse:

- [`BuildTupleViewContractFromTupleDesc()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:656) already converts executor `TupleDesc` metadata into a `CitusTupleViewContract`.
- [`TupleSinkServiceCreateSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3645) creates a service-owned exact sink and installs the tuple-view contract.
- [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_service.h:83) maps a local tuple sink handle.
- [`TryAppendTupleViewToCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_service.h:96), [`AppendTupleViewToCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_service.h:99), and [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_service.h:101) already append executor slots as tuple views.
- [`BorrowNextTupleViewFromCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_service.h:108) and [`CopyOutNextTupleViewFromCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_service.h:115) already decode received tuple-view rows into caller-owned `Datum` / null arrays.

#### Completed follow-up: service-owned client SQL result sinks

The target design that client-SQL row results are fully sink-backed in the same
framework sense as Citus backend-backend tuple copy has landed for the local
pgbench path:

1. At row-producing command startup, the backend asks the service to open/bind a
   result sink under the parent client SQL session using
   `parentServiceSessionId`.
2. The service returns byte-ring tuple-view descriptors; descriptors without
   `CITUS_TUPLE_SINK_QUEUE_DESCRIPTOR_FLAG_BYTE_RING` are rejected by the active
   tuple/result path.
3. The backend `DestReceiver` still builds the tuple-view contract from the
   executor `TupleDesc`, but `RemoteExecEnsureSessionResultQueue()` now binds a
   service-owned sink instead of creating an ad hoc backend-owned POSIX shm
   queue.
4. The frontend `HomerClientOpenResultSink()` opens/drains the
   service-provided receive sink. For local host this still maps shared memory,
   but ownership and binding come from the service, not from the backend process.
5. Normal close follows sink lifecycle. The frontend closes its local mapping
   without unlinking; service/backend session cleanup owns the sink.

Acceptance criteria for this follow-up:

- command completion metadata references a service-owned sink identity and
  byte-ring descriptor. **Done.**
- pgbench `SELECT abalance` drains as a tuple-view result with no scalar or
  discard shortcut. **Done.**
- single-client and multi-client `pgbench --homer` performance does not regress
  against the current byte-ring checkpoint. **Done in the May 16 farnet1
  validation: c1/j1 `5838 TPS`, c4/j4 `14983` and `14851 TPS` repeats with zero
  failures.**
- the implementation remains compatible with the later DPU direction: payload
  ownership belongs to the Homer service/substrate, not to socketless backend
  private shm setup. **Done for ownership/binding; the broader control transport
  is still future work.**

Remaining result-path work:

- Harden the sink-backed receiver.
   - The current receiver validates `CITUS_REMOTE_EXEC_SQL_RESULT_TUPLE`, builds the tuple-view contract from the executor `TupleDesc`, and writes tuple batches.
   - On error unwind, close or mark the result sink failed so the frontend does not wait forever for EOS.
   - Preserve the current lifetime rules: the receiver-owned copy slot must use a copied `TupleDesc`, and the send-side tuple sink handle must close in `rShutdown` before `PortalDrop()` resets executor memory.

- Extend command completion/control metadata.
   - Completion now carries command state, processed row count, result sink id/descriptor/contract, and result readiness/EOS state.
   - It still needs SQL command tag, SQLSTATE/error summary, and authoritative dynamic session state.
   - Row payload bytes should remain out of the command mailbox.
   - The existing `scalarInt64` fields can remain temporarily for wire compatibility but should not be used by the client SQL result path.

- Extend frontend-safe result APIs.
   - The current frontend API in [`remote_execution_client.h`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:1) can map and drain the result sink enough for pgbench's row payload.
   - Add richer helpers to borrow/copy typed tuple views into caller-owned values instead of only draining/skipping rows.
   - Keep the public frontend API tuple-shaped: one-column one-row scalar results should still be consumed as a tuple view with one attribute.

- Broaden pgbench result consumption.
   - Plain TPC-B `SELECT abalance` now drains the tuple-result sink.
   - `\gset` / `\aset` should remain rejected until pgbench can convert borrowed tuple values into variable strings correctly; this requires type output functions or a deliberately narrower first implementation for integer values.

#### Sink fill, blocking, and measurement distortion

A full sink is not intrinsically a deadlock. If the producer and consumer are both running and the consumer is draining, backpressure is the expected behavior.

The deadlock risk is specifically an API sequencing risk in the current synchronous command model:

```text
frontend: StartAndWaitCommand(SQL_EXECUTE)
backend:  executes SELECT and appends rows into result sink
frontend: does not know/map/drain result sink until command completion
backend:  cannot complete command because result sink is full
```

In that sequence, both sides can wait forever even though the sink itself is behaving correctly. The fix is to publish result-sink readiness before requiring command completion for row-producing commands, or otherwise provide a frontend API that can start a command, learn the result sink handle, drain rows, and then observe final command completion.

"Distort performance" means a benchmark can accidentally measure the control-plane sequencing artifact rather than the communication stack. For example, if a large `SELECT` is forced to fully materialize into a finite sink before the frontend is allowed to drain, latency includes artificial producer stalls and queue-capacity sensitivity that would not exist in a streaming result protocol. This is acceptable as explicit backpressure once the client is actively draining; it is misleading if caused by the API refusing to expose the sink until after command completion.

For the pgbench TPC-B `SELECT abalance` result, this hazard is practically small because the result is one row. The plan still needs the streaming-ready sequencing because the abstraction should support arbitrary tuple result shapes and sizes without a later redesign.

Current decision: do not make reserved payload slots part of the normal result
path yet. Reserving one slot for EOS/error would make the full-sink case easier
to reason about, but it permanently reduces usable data capacity and adds a
second reservation mode to a hot abstraction. For the current research target,
the better tradeoff is to make full sinks correct but cold:

1. size result sinks generously for the expected workload, especially because
   farnet-scale memory usage is not the limiting factor
2. publish result-sink readiness before final command completion for
   row-producing commands
3. let the frontend drain tuple batches while the command is still active
4. carry command tag, row count, SQLSTATE/error summary, dynamic session state,
   result-sink id, and readiness/EOS state in command/session control metadata
5. keep tuple payloads exclusively in sinks
6. use sink terminal/control state for EOS/error instead of consuming an ordinary
   tuple payload slot

This plan keeps the common path simple: the backend appends tuples into batches,
publishes batches, the frontend drains batches, and command completion observes
the final control state. Full-sink backpressure remains a correctness path, not
the design center for the measured pgbench transaction workload.

The side/control channel must not become a per-tuple branch in the normal data
path. The tuple hot path should not check result readiness, EOS/error state,
inline-vs-sink location, or command completion metadata for every row. Those
facts are checked at command start, result-sink readiness publication, batch
reserve/publish boundaries, frontend command/EOS polling, receiver shutdown, and
error unwind. The unavoidable terminal/capacity checks in
[`TryReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1038)
are already per batch reservation, not per tuple append.

The current tuple-sink implementation already has the useful frequency property
that queue-control traffic is per batch slot, not per tuple. Individual tuple
append happens inside [`TryAppendTupleViewToCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1180)
without touching queue-control head/tail words for each tuple. Queue control is
touched when reserving a batch in [`TryReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1038),
publishing a batch in [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1353),
polling a receive batch in [`PollCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1425),
and releasing a receive batch in [`ReleaseCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1529).

For eventual DPU offload, preserve the semantic producer API but do not preserve
the current host-service polling model as the final architecture. Today the
host-side service maps the same queue control block as the backend through
[`TupleSinkServiceQueueMapping`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:61)
and polls queue-control words in paths such as
[`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6069).
With a DPU, that would become repeated DPU DMA reads of host memory. The target
DPU shape should be event/range based instead: the backend publishes one or more
slots, notifies the local communication substrate, the DPU reads the new tail
once per event or range, drains contiguous slots using locally cached progress,
and writes host-visible credits/completions back in batches or at thresholds.
This keeps DMA-visible control traffic proportional to batches/ranges and keeps
rare full-sink handling off the normal hot path.

### Dynamic session state

The current backend-to-backend SQL command path has a known workaround: command sessions are forced fresh because the service cannot distinguish a session that has no command in flight from one that still has an attached transaction. That gap is documented at `src/backend/distributed/utils/homer/tuple_sink_service_process.c:5325`.

The fix is explicit dynamic session state in the service-owned session record, reported by the socketless backend at every command boundary:

```c
REMOTE_EXEC_SESSION_IDLE
REMOTE_EXEC_SESSION_COMMAND_ACTIVE
REMOTE_EXEC_SESSION_IN_TRANSACTION
REMOTE_EXEC_SESSION_IN_FAILED_TRANSACTION
REMOTE_EXEC_SESSION_CLOSING
REMOTE_EXEC_SESSION_FAILED
```

This should replace the current coarse post-command reuse hint:

- `TX_BEGIN` / `TX_BEGIN_ATTACH` transitions `IDLE -> COMMAND_ACTIVE -> IN_TRANSACTION`.
- transaction-body SQL success keeps `IN_TRANSACTION`.
- transaction-body SQL error transitions to `IN_FAILED_TRANSACTION` or `FAILED`, depending on whether abort/reset remains possible.
- `TX_COMMIT` transitions `IN_TRANSACTION -> COMMAND_ACTIVE -> IDLE` on success.
- `TX_ABORT` transitions `IN_TRANSACTION` or `IN_FAILED_TRANSACTION -> COMMAND_ACTIVE -> IDLE` on success.
- backend crash, peer disconnect, or unrecoverable protocol error transitions to `FAILED`.

The service should use this state when deciding whether a compatible session can be reused:

- `IDLE`: reusable if the static intent key is compatible and freshness/ownership policies allow it
- `COMMAND_ACTIVE`: not reusable and rejects another command unless explicit pipelining is implemented
- `IN_TRANSACTION`: not reusable by another logical client/session
- `IN_FAILED_TRANSACTION`: not reusable except by an abort/reset operation owned by the same logical session
- `FAILED`: retire

This removes the root cause of the fresh-session workaround while preserving safety. For milestone 1 we may still force fresh sessions as a conservative implementation step, but the protocol and service state should be shaped so this workaround can be removed rather than baked in.

## First milestone target

Run the simple-mode pgbench transaction workload through Homer without libpq on the workload path.

Accepted scope:

- single client
- one command in flight per session
- simple-mode pgbench transaction shape
- existing pgbench tables already initialized by ordinary tooling
- no replication/basebackup workload yet
- no prepared-mode query path
- no concurrent client/session solution
- minimal error handling, but enough logging to debug command/session transitions

The workload body is:

1. pick `aid`, `bid`, `tid`, and `delta`
2. open or reuse one `REMOTE_EXEC_OP_CLIENT_SQL_SESSION`
3. send typed begin
4. send `UPDATE pgbench_accounts ...`
5. send `SELECT abalance ...`
6. send `UPDATE pgbench_tellers ...`
7. send `UPDATE pgbench_branches ...`
8. send `INSERT INTO pgbench_history ...`
9. send typed commit
10. on local or remote failure, send typed abort if the session may still be transaction-attached

## Completed milestone: persistent client sessions

The persistent-session milestone is complete for integrated `pgbench --homer`.
One frontend Homer SQL session now survives across many pgbench transactions per
client, closing the earlier lifecycle mismatch against normal `pgbench`, where
one frontend connection and one backend execution context are reused for the
whole run.

Target runtime shape:

1. open one process-level control mapping through [`HomerClientOpenControl()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:136)
2. open one `REMOTE_EXEC_OP_CLIENT_SQL_SESSION` through [`HomerClientOpenSqlSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:418)
3. run every transaction through the same [`HomerClientSession`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:35)
4. close the session once through [`HomerClientCloseSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:496)

The original blocker in the deleted proof-of-concept runner was that it opened
and closed the Homer session inside every transaction, while the service/backend
also treated typed commit/abort as terminal session events. That has been
replaced by persistent frontend session ownership in pgbench's thread/client
lifecycle, client-session-aware backend reuse, and explicit typed close.

Implemented changes:

1. [`threadRun()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:8215) opens Homer client SQL sessions before measured work, and [`finishHomerSession()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:8137) closes them after the run.
2. [`CitusRemoteExecBackendSpawnRequest.sessionOpKind`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_backend_protocol.h:69) and [`CitusRemoteExecBackendStartupData.sessionOpKind`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_backend_protocol.h:59) carry the service session/op kind into the socketless backend. [`TupleSinkServiceSubmitBackendSpawnRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1321) fills it, and [`ProcessSpawnRequestSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1376) copies it into startup data.
3. [`RemoteExecBackendExecuteTxCommitCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:813) and [`RemoteExecBackendExecuteTxAbortCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:858) reset transaction state but keep the backend command loop alive for `REMOTE_EXEC_OP_CLIENT_SQL_SESSION`. Older backend-to-backend command sessions preserve terminal commit/abort behavior.
4. [`TupleSinkServiceSessionShouldRetireWhenIdle()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3320) no longer retires `CITUS_REMOTE_EXEC_OP_CLIENT_SQL_SESSION` merely because the current command is `TX_COMMIT` or `TX_ABORT`.
5. [`CITUS_REMOTE_EXEC_COMMAND_CLIENT_SQL_SESSION_CLOSE`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:139) plus [`RemoteExecBackendExecuteClientSqlSessionCloseCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:904) provide explicit backend close semantics behind the public `HomerClientCloseSession()` API.

Acceptance checks passed:

- one `HomerClientOpenSqlSession()` per run, not per transaction
- one socketless backend spawn for a single-client run
- many command sequences on the same service session id
- one explicit close at the end
- no leftover remote-exec backend after close
- `pgbench_history` grows by the completed transaction count

The observed pinned 3-transaction service log had one `opened command session`, one `spawn request completed`, transaction command sequences `1..21`, then `client_sql_session_close sequence=22`. The 1000- and 5000-transaction runs increased `pgbench_history` by exactly the requested transaction count and left no remote-exec backend process behind.

Do not treat the older standalone `homer_pgbench` 5000-transaction `~5259 TPS`
number as a final apples-to-apples result against stock pgbench. That run
predated full pgbench integration and sink-backed result delivery, and the
binary is now removed. Current comparisons should use `pgbench --homer` versus
libpq from the same pgbench binary with `SELECT abalance` flowing through a
tuple-result sink; see the implementation checkpoint for the latest repeated
measurements and tail-latency numbers.

## Wait policy and busy waiting

The fixed 1 ms sleeps were a measurement-distorting prototype artifact. Replacing them with [`HomerClientCpuRelax()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:70), [`TupleSinkServiceCpuRelax()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:374), and [`RemoteExecBackendCpuRelax()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:125) moved the 1000-transaction result from roughly 36 TPS to roughly 268 TPS in the initial no-sleep/yield experiment. That confirms the fixed sleeps were a major artificial cost.

Raw `pause`-only spinning is the right direction for the pinned single-client benchmark, but it depends on controlling CPU placement. `pause` is only a CPU-local spin-loop hint; it reduces pipeline/memory-order penalties while repeatedly polling a cache line, but it does not create a scheduler handoff, an inter-process wakeup, or any guarantee that the producer has recently run. In this prototype the waiter and the producer are often different Linux processes:

- the frontend runner waits for service control responses
- the service waits for backend spawn/startup and command completion
- the socketless backend may need CPU time to initialize Citus state, execute SQL, and publish the completion

The earlier pure-spin run hung after `CLIENT_SQL_TX_BEGIN` while the remote-exec backend was in first-use Citus backend startup/maintenance-daemon initialization. That did **not** prove the service/frontend/backend were on the same physical core or that raw `pause` was inherently worse than `sched_yield()`. Follow-up explicit pinning refuted that stronger interpretation:

- postmaster/new backend children on CPU 4
- external service on CPU 2
- frontend `homer_pgbench` on CPU 3
- Citus maintenance daemon on CPU 5
- pinned `sched_yield()` run: 1000 transactions, `tps=242.942`, `latency_avg_ms=4.116`, `/usr/bin/time user=0.55 sys=3.56`
- pinned raw `pause` run: 1000 transactions, `tps=246.288`, `latency_avg_ms=4.060`, `/usr/bin/time user=4.05 sys=0.01`
- pinned raw `pause` longer run: 5000 transactions, `tps=246.023`, `latency_avg_ms=4.065`, `/usr/bin/time user=20.30 sys=0.01`

So the stronger statement is:

- pure `pause` spinning removed the cooperative scheduling point that fixed sleeps/yields had provided
- without explicit process placement, the system then depends on normal Linux preemption, migration, and runqueue behavior for another process to make progress
- with explicit placement, raw `pause` is not worse than `sched_yield()` for the current single-client control path; it trades kernel scheduler time for user-space spin time
- if the cold-start hang reappears under pinned placement, the next diagnosis should focus on which producer is not publishing progress, not on assuming the spin primitive itself is the root cause

Before blaming same-core contention specifically, collect affinity and scheduling evidence during a failing run. The current implementation has code-level placement knobs: pgbench [`--client-cpu`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1039), service `HOMER_SERVICE_CPU`, and socketless-backend `HOMER_REMOTE_EXEC_BACKEND_CPU`. The repeatable wrapper is [`src/tools/homer_pgbench_single_client_compare.sh`](/data/dbcomm/postgres-citus-separate-comm-stack/src/tools/homer_pgbench_single_client_compare.sh:1), which records `/proc` affinity and current-CPU snapshots. `taskset -pc` is still useful for observation, but it should not be the primary mechanism for benchmark placement anymore.

`sched_yield()` is not ideal. It avoids the fixed millisecond delay and gives runnable producers a chance to execute, but Linux yield behavior is scheduler-policy dependent and can be unpredictable: the yielding process may resume quickly, or it may be moved behind other runnable work and lose cache locality. In the pinned experiment it also moved most wait cost into kernel time. It was useful as an interim no-sleep primitive, but the intended benchmark wait policy is pinned busy-waiting or explicit wakeups, not repeated scheduler yields.

Better future options, in increasing implementation complexity:

1. **Pinned raw spin**: for the single-client benchmark, pin frontend, service, socketless backend children, and Citus maintenance separately, then use raw `pause` polling on the local control path.
2. **Bounded spin then yield**: for less controlled environments, spin with `pause` for a small fixed iteration budget while checking the mailbox/control word, then call `sched_yield()` only if the producer has not responded. This preserves low latency when producer and waiter are actively running while avoiding indefinite dependence on scheduler preemption.
3. **Adaptive spin/yield policy**: track whether the previous waits completed during the spin window and tune the spin budget per wait site. Control-path waits can tolerate more instrumentation than tuple hot paths.
4. **Event/doorbell wakeups**: replace control-path polling with an explicit wakeup primitive for cold/control transitions, and keep pure polling only where both sides are known to be actively scheduled and the expected wait is extremely short.

For the current persistent-session benchmark, use raw `pause` under explicit CPU pinning. If a pause-based run stalls, diagnose the missing producer progress directly by inspecting process affinity, current CPU (`psr`), remote-exec backend state, service logs, and mailbox epochs.

## Implementation plan

### 1. Add the new op kind and protocol constants

Status: complete for milestone 1.

Add `REMOTE_EXEC_OP_CLIENT_SQL_SESSION` after the existing op kinds in:

- `src/include/distributed/homer/remote_execution_session.h:30`

Mirror it in the fixed-width service protocol near:

- `src/include/distributed/homer/remote_execution_control_protocol.h:93`

Update op-kind formatting and validation in:

- `src/backend/distributed/utils/homer/remote_execution_session.c:167` `RemoteExecutionOpKindName()`
- service open/session validation paths in `src/backend/distributed/utils/homer/tuple_sink_service_process.c`

The first implementation can route client SQL sessions through the existing command-session local control op, but the service should validate `CITUS_REMOTE_EXEC_OP_CLIENT_SQL_SESSION` separately from `CITUS_REMOTE_EXEC_OP_SQL_COMMAND`.

### 2. Add client-session intent and command specs

Status: complete for the first local-control milestone.

Add a backend/service shared intent initializer analogous to `InitRemoteSqlCommandSessionIntent()`:

- `InitRemoteClientSqlSessionIntent(...)`

The intent should set:

- `opKind = REMOTE_EXEC_OP_CLIENT_SQL_SESSION`
- DML access kind for pgbench
- node scope, not placement scope
- fresh session for the first milestone unless explicit idle-safe reuse is implemented

Add fixed-width command payloads to the control protocol:

- `CitusRemoteExecClientSqlExecuteCommandSpec`
- result modes such as `NONE` and `TUPLE_RESULT`, not `ONE_INT8` or `scalarInt64`
- completion fields for processed rows, SQL command tag, result sink id/readiness/EOS, and dynamic session state

The implemented path adds `CLIENT_SQL_TX_BEGIN` and reuses `SQL_EXECUTE`, `TX_COMMIT`, and `TX_ABORT`. It does not yet add a distinct `CitusRemoteExecClientSqlExecuteCommandSpec`; the first runner uses the existing fixed-width `CitusRemoteExecSqlExecuteCommandSpec`. `scalarInt64` is not used by `homer_pgbench`, but the fields still exist in shared completion structs and the older backend wrapper path. Row data still needs result sinks.

### 3. Make the service accept client SQL sessions

Status: complete for the local single-client milestone.

Extend the service open path:

- keep `TupleSinkServiceHandleOpenCommandSession()` for sinkless command-style sessions
- allow both `CITUS_REMOTE_EXEC_OP_SQL_COMMAND` and `CITUS_REMOTE_EXEC_OP_CLIENT_SQL_SESSION`
- record whether the session is a backend-to-backend SQL command session or a client SQL session

Peer open remains a backend-to-backend command-session concern and is not used by the local client SQL milestone. The local service path now bypasses peer RDMA for `CITUS_REMOTE_EXEC_OP_CLIENT_SQL_SESSION`.

Extend peer open similarly for future backend-to-backend command families:

- `src/backend/distributed/utils/homer/tuple_sink_service_process.c:6401` `TupleSinkServiceHandlePeerOpenCommandSessionRequest()`

Do not add a command sink for milestone 1. The first client SQL session is still a sinkless command envelope for command submission/completion, plus separate result sinks for row payloads. This is a reuse of the existing ordered command-envelope semantics, not a statement that command sinks are permanently unnecessary.

### 4. Add a small frontend Homer client library

Status: complete for the integrated pgbench milestone.

The current helper that maps the local control region is backend-facing:

- `src/backend/distributed/utils/homer/remote_execution_session.c:1477` `MapRemoteExecutionControlRegion()`
- `src/backend/distributed/utils/homer/remote_execution_session.c:1802` `OpenCommandSessionThroughLocalService()`
- `src/backend/distributed/utils/homer/remote_execution_session.c:2145` `StartRemoteExecutionCommandThroughLocalService()`
- `src/backend/distributed/utils/homer/remote_execution_session.c:2218` polling command completion

For frontend pgbench replacement, add a small frontend-safe library that uses only standalone/common headers and the fixed-width service protocol:

- no backend memory contexts
- no `ereport`
- no `PGconn`
- no backend transaction globals
- explicit return codes and error buffers

This library should expose:

```c
RemoteExecutionClientSession *OpenRemoteExecutionClientSession(...);
int StartRemoteExecutionClientCommand(RemoteExecutionClientSession *session, ...);
int WaitForRemoteExecutionClientResult(RemoteExecutionClientSession *session, ...);
int CloseRemoteExecutionClientSession(RemoteExecutionClientSession *session);
```

The implementation can share protocol structs from `remote_execution_control_protocol.h`, but should not directly depend on Citus backend code.

The current code implements this as [`remote_execution_client.h`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:1) and [`homer_client.c`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1). [`Makefile`](/data/dbcomm/citus-dbcomm-separate-comm-stack/Makefile:47) builds `libhomer_client.a` and installs both the static library and header. The API names are currently `HomerClient*` rather than the sketch names above, and the API uses caller-owned error buffers instead of backend error machinery.

### 5. Add a pgbench-compatible Homer frontend

Status: complete for integrated simple-mode pgbench testing.

For the first milestone, prefer an isolated single-client runner or an explicitly isolated pgbench mode rather than rewriting the whole pgbench script engine.

Minimum viable shape:

- initialize tables using normal tools outside the measured/offloaded path
- run one client transaction loop with pgbench's default simple transaction statements
- generate `aid`, `bid`, `tid`, and `delta` locally
- call the frontend Homer client API
- report transaction count, failures, average latency, and TPS in a pgbench-like summary

If this is added inside `src/bin/pgbench/pgbench.c`, the Homer mode must bypass the existing `PGconn` workload path around:

- `src/bin/pgbench/pgbench.c:3167` `PQsendQuery(...)`
- `src/bin/pgbench/pgbench.c:3178` `PQsendQueryParams(...)`
- `src/bin/pgbench/pgbench.c:3189` `PQsendQueryPrepared(...)`

The first proof of concept used a separate Homer pgbench-style tool, but that
scaffold has now been deleted. The path that remains is integrated
`pgbench --homer`: [`sendHomerCommand()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3573) maps SQL text to typed Homer lifecycle/execute commands, and [`HomerRunCommandAndWait()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3731) waits on pushed completions while draining tuple-result sinks.

### 6. Dispatch typed client commands in the socketless backend

Status: complete for simple-mode lifecycle and SQL body statements.

Extend the socketless backend bridge command switch at:

- `src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1070`

For the first milestone:

- map `CLIENT_SQL_TX_BEGIN` to the existing typed begin/attach machinery
- map `CLIENT_SQL_TX_COMMIT` to the existing typed commit machinery
- map `CLIENT_SQL_TX_ABORT` to the existing typed abort machinery
- map `CLIENT_SQL_EXECUTE` to a client-session wrapper around `RemoteExecBackendExecuteSqlCommand()`

The wrapper should check that:

- the backend session belongs to `REMOTE_EXEC_OP_CLIENT_SQL_SESSION`
- the transaction-attached state is correct for the command
- row-producing SQL binds a result sink whose contract matches the executor `TupleDesc`
- pgbench's `SELECT abalance` assertion sees exactly one row and one `int8` attribute through the generic tuple-result path
- updates/inserts report processed row counts for debug visibility

This is intentionally similar to Postgres's current dispatch architecture, but it is not tied to FE/BE protocol message types. The dispatch key is the Homer command kind.

Implemented caveat: the backend currently checks transaction attachment and returns processed row counts, but it does not yet verify that the service session op kind is `REMOTE_EXEC_OP_CLIENT_SQL_SESSION` inside the backend command loop. The service routes local client SQL sessions separately before spawn.

### 7. Add sink-backed tuple-result handling

Status: complete for the single-client simple-mode pgbench path; still incomplete for service-owned result-sink binding, rich frontend tuple accessors, and prepared/concurrent modes.

The current completion already has useful fields:

- `src/include/distributed/homer/remote_execution_session.h:358` `RemoteExecutionCommandCompletion`
- `src/include/distributed/homer/remote_execution_control_protocol.h:400` `CitusRemoteExecCommandCompletion`

For milestone 1, completion remains command/control metadata and row payloads use a tuple-result sink path for:

- arbitrary tuple-shaped SQL row outputs described by the executor `TupleDesc`
- pgbench's one-row `(abalance int8)` result as the first acceptance case

Extend completion metadata, not the result sink, for:

- SQL command tag and processed row count for updates/inserts
- SQLSTATE/error detail when available
- dynamic session state after the command
- result sink id/readiness/EOS for row-producing commands

Do not build prepared-mode or broad frontend protocol emulation before milestone 1, but do build row-result delivery as generic tuple-sink delivery rather than a special scalar path. Any inline storage for tiny results should be an internal implementation of that sink, not a separate API surface. The acceptance test currently validates pgbench's `SELECT abalance`, but the backend code path derives its tuple contract from the executor `TupleDesc` and is not hard-coded to that one-column shape.

Implementation reality: `pgbench --homer` now sends row-producing simple SQL with `CITUS_REMOTE_EXEC_SQL_RESULT_TUPLE`, drains the result sink, and then closes/unlinks the frontend mapping. The remaining shortcut is ownership: the backend currently creates a local POSIX shm result queue and publishes its descriptor, instead of asking the service to allocate/bind the sink.

### 8. Smoke-test sequence

Validation should progress in this order:

1. frontend Homer client opens a `REMOTE_EXEC_OP_CLIENT_SQL_SESSION` and closes it
2. typed begin/abort succeeds
3. typed begin/commit succeeds with no SQL body
4. single `SELECT 1` returns one scalar
5. one pgbench `UPDATE pgbench_accounts ...` returns processed row count 1
6. full pgbench transaction body succeeds once
7. single-client timed loop runs for a short duration

The success criterion for this milestone is not performance. It is that the transaction workload no longer sends SQL to PostgreSQL through libpq during the measured/offloaded path.

## Deferred work

- service-owned result-sink allocation/binding for row-producing client SQL
- richer frontend tuple-result accessors beyond drain-and-discard
- prepared-mode pgbench and typed prepare/bind/execute
- concurrent clients and session multiplexing
- explicit transaction-idle reuse state for command sessions
- cancellation
- robust client authentication and user mapping
- basebackup/replication transport, which should likely use a different high-level stream object rather than client SQL commands
- network-interference measurements and basebackup background traffic
- DPU-oriented rework of the current host-shared-memory polling/control substrate

## Design caveats

The tempting shortcut is to keep using a SQL wrapper function from pgbench and call the remote session API from inside a local backend. That is useful as a backend bridge smoke test, but it does not satisfy this milestone because pgbench still talks to the local server through libpq.

Another tempting shortcut is to carry FE/BE protocol messages through Homer. That would reduce Postgres integration work in some places, but it would weaken the research contribution by preserving the socket-level abstraction. The chosen direction is typed database-semantic objects, even when those objects initially map onto existing PostgreSQL semantic execution helpers.
