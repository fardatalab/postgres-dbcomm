# Transaction Latency Measurement Strategy

## Purpose

Define a practical measurement strategy for transaction latency breakdowns in Citus/Postgres, with emphasis on:

- identifying the major stages in a transaction/query lifecycle
- separating communication-related latency from regular coordinator/backend execution
- clarifying what can already be measured with the current timing spots
- identifying the instrumentation gaps needed for an ordered stage plot

This note focuses on the simple but representative case of single-shard `UPDATE ... RETURNING`, and then generalizes the measurement plan.

## First correction: there are two different plot goals

The requested "stacked horizontal bar" can mean two different things:

1. **Latency composition bar**
   - one bar per transaction/query
   - each segment is an additive bucket total
   - segment order is chosen for readability, not because it is the exact runtime order

2. **Ordered stage timeline**
   - one bar per transaction/query
   - each segment is a real interval in execution order
   - this is much closer to a Gantt chart

These are **not** equivalent.

Current timing reports produced by [`logger_print_timings()`](/data/dbcomm/postgres-citus/src/common/time_instr.c#L565) are aggregated totals per spot, not ordered begin/end intervals. That means:

- the current codebase can already support a useful **composition** plot
- the current codebase cannot yet support a faithful **ordered stage timeline** without additional instrumentation

That distinction matters because otherwise it is easy to over-interpret summed timers as if they were sequential stages.

## What the current instrumentation already gives us

### 1. A good per-query denominator

The correct top-level denominator for one logical query cycle is [`QUERY_ACTIVE_WALL`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L727), started by [`BeginQueryTimingCycle()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L727) and stopped by [`FinishQueryTimingCycle()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L762) after the final [`ReadyForQuery()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L262) flush returns at [`PostgresMain()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L4979).

The excluded wait companion is [`QUERY_WAIT_WALL`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L69), driven by the shared wait hooks described in the existing note [execsimplequery_and_message_loop.md](/data/dbcomm/postgres-citus/docs/kb/postgres/query-lifecycle/execsimplequery_and_message_loop.md).

For explicit transactions, the current scripts already aggregate per-statement `QUERY_ACTIVE_WALL` blocks across the transaction in [`aggregate_transaction_timing.py`](/data/dbcomm/postgres-citus/fdl_utils/aggregate_transaction_timing.py#L1).

### 2. Good communication-side additive buckets

The current timing bucket utility in [`timing_bucket_utils.py`](/data/dbcomm/postgres-citus/fdl_utils/timing_bucket_utils.py#L58) already exposes useful additive communication buckets:

- `transport_protocol_cpu_ns`
- `row_serde_cpu_ns`
- `result_materialization_cpu_ns`
- `file_comm_io_ns`
- optional `optional_connection_setup_cpu_ns`

Those buckets are built from lower-level leaves such as:

- frontend/libpq socket/protocol work:
  - [`PG_FE_SOCK_READ`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L71)
  - [`PG_FE_SOCK_WRITE`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L72)
  - [`PG_parseInput`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L75)
  - [`PQ_getResult`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L104)
  - [`PQ_connectStart`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L106)
  - [`PQ_connectPoll`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L107)
- backend socket/protocol work:
  - [`PG_BE_SOCK_READ`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L73)
  - [`PG_BE_SOCK_WRITE`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L74)
  - [`PQ_getbyte`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L76)
  - [`PQ_getmessage`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L77)
  - [`PQ_getbytes`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L78)
  - [`PQ_putmessage`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L100)
- Citus receive/result handling:
  - [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3710)
  - [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4022)
  - [`ReceiveResults_Deserialize`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4067)
  - [`ReceiveResults_BuildTuples`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4244)
- backend row send to client:
  - [`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L306)
  - [`Printtup_Ser`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L323)
  - [`Printtup_Net`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L395)

The recommended bucket sets are already documented in [communication_bucket_sets.md](/data/dbcomm/postgres-citus/docs/kb/instrumentation/timing-spots/communication_bucket_sets.md).

### 3. Good distributed-transaction control-path timers

The current code already times the major remote-command lifecycle envelopes:

- distributed transaction lifecycle start/stop:
  - [`StartDistributedTransactionTimingIfNeeded()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L79)
  - [`EndDistributedTransactionTimingIfNeeded()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L158)
- coordinator-side coordinated-transaction preparation in [`UseCoordinatedTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L341)
- remote command send in [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L563)
- remote command result wait/drain in [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L683)
- connection-establishment wait in [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L980)
- worker `BEGIN` bootstrap in [`StartRemoteTransactionBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L242)
- worker `COMMIT`/`ROLLBACK`/`PREPARE` phases in [`CoordinatedRemoteTransactionsCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1139) and [`CoordinatedRemoteTransactionsPrepare()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1060)

The exact mapping is already documented in [distributed_transaction_timing.md](/data/dbcomm/postgres-citus/docs/kb/citus/transactions/timing/distributed_transaction_timing.md).

## Biggest current gaps

### Communication-stage coverage added in the ordered trace

The ordered trace now covers the main communication/control-path intervals that
were previously missing:

- backend-side client command ingress in
  [`SocketBackend()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L430)
  via the new `client_command_receive` stage
- coordinator-side post-dispatch socket flush for worker task queries in
  [`SendNextQuery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4097)
  and [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3846)
  via the new `remote_command_flush` stage
- end-of-transaction worker session reset/release/close bookkeeping in
  [`AfterXactHostConnectionHandling()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1586)
  via the new `worker_session_release` stage
- failure-path closure of connection/session ordered stages in
  [`ShutdownConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L792),
  [`MarkConnectionConnected()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1769),
  [`ConnectionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3088),
  and [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3846)

That closes the main communication-stack holes for the ordered transaction
timeline.

The notable remaining limitations are narrower:

- `client_command_receive` starts after the frontend message type byte becomes
  available, so it still does not measure client-side "send-to-first-byte"
  latency
- transaction-control rounds such as `remote_tx_attach` / `remote_tx_commit` /
  `remote_tx_prepare` / `remote_tx_abort` remain composite round-trip envelopes;
  only task-query rounds currently expose a separate flush stage

### 1. The core coordinator-local stage timers exist but are not wired

The enum already defines:

- [`ParseQuery`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L81)
- [`QueryAnalyzeAndRewrite`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L82)
- [`QueryExecution`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L83)
- [`QueryPlanning`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L84)
- [`EndingComms`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L85)

But there are currently no `timing_start(...)` / `timing_end(...)` call sites for them. So the code can already tell us a lot about communication cost, but it cannot yet cleanly separate:

- parse
- analyze/rewrite
- planning
- executor-local work
- end-of-command output tail

from the rest of the query cycle.

### 2. The final client-visible tail is only partially timed

[`EndCommand()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L169) is timed by [`XACT_TS_EndCommand`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L175), but [`ReadyForQuery()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L262) and its final [`pq_flush()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L277) are not separately timed.

That means `QUERY_ACTIVE_WALL` includes the final tail up to `ReadyForQuery`, but there is no dedicated sub-bucket for that tail yet.

### 3. Current transaction timers are totals, not per-phase traces

For example, [`XACT_TS_SendRemoteCommand`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L148) can represent:

- worker `BEGIN`
- worker DML
- worker `COMMIT`
- worker `ROLLBACK`
- 2PC `PREPARE`

depending on the command. The current report only gives the summed total for that timer over the reporting block. It does not tell us which portion belonged to which command kind, nor their ordering.

### 4. Client-to-coordinator startup/auth is not part of the current timing model

The backend-side client session startup path is:

- [`BackendInitialize()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c#L122)
- [`PerformAuthentication()`](/data/dbcomm/postgres-citus/src/backend/utils/init/postinit.c#L193)
- [`ClientAuthentication()`](/data/dbcomm/postgres-citus/src/backend/libpq/auth.c#L390)

There is no corresponding server-side timing breakdown wired there today. So if the desired plot starts from "client initiates a connection", then client-to-coordinator connect/auth must currently be measured either:

- from the client benchmark harness, or
- by adding new connection-lifecycle timers in that startup/auth path

Without that, any current server-side timing plot necessarily begins later, when the backend is already in the query/message loop.

## Stage taxonomy for the target transaction

For a single-shard `UPDATE ... RETURNING`, the stages to plot depend on the scenario.

## Sequential network-stage taxonomy

If the real deliverable is a faithful per-transaction horizontal timeline, then
the stage model should be driven by **communication handoff boundaries**, not by
leaf CPU helpers.

The clean rule is:

- split whenever control is handed to a different network-facing subsystem or
  when the coordinator begins waiting on one
- keep everything else in one coarse **coordinator/backend local execution**
  bucket

For the current Citus/Postgres stack, the useful non-overlapping network-related
categories are:

### 1. Client session establishment

This is the optional client-to-coordinator prefix:

- backend startup in [`BackendInitialize()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c#L122)
- authentication in [`PerformAuthentication()`](/data/dbcomm/postgres-citus/src/backend/utils/init/postinit.c#L193)
- HBA/auth method execution in [`ClientAuthentication()`](/data/dbcomm/postgres-citus/src/backend/libpq/auth.c#L390)

This stage is **session-scoped**, not transaction-scoped. It should only appear
on the timeline when the measured transaction also pays for opening the client
session.

### 2. Client command ingress

This is the backend-side receive/framing stage for frontend messages that
belong to a logical query cycle:

- frontend message classification in
  [`FrontendMessageStartsQueryTimingCycle()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L403)
- message receive in [`SocketBackend()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L430)
- lower buffered protocol reads in
  [`pq_getmessage()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c#L1238)
  and [`pq_getbytes()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c#L1086)

This stage is intentionally narrower than full client-observed request latency:

- it starts after the message type byte is already available to the backend
- it therefore excludes arbitrary client think-time / idle gap before the
  command begins arriving

What it does cover is the backend-side protocol ingress and message-body
receive work before parse/plan/execute begins.

### 3. Worker execution-session acquisition

This is the optional coordinator-to-worker connect/reuse stage:

- cache lookup in [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L466)
- startup front half in [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1362)
- async poll progression in [`ConnectionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L2990) and [`MultiConnectionStatePoll()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L808)
- blocking wait for establishment in [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L980)

This stage is the right home for:

- TCP/TLS/auth/setup to the worker
- worker backend spawn/startup cost
- "warm vs cold" coordinator-to-worker reuse effects

If the connection is already cached and initialized, this stage is zero or near-zero.

### 4. Distributed transaction attach/bootstrap

This is the optional worker-side transaction enrollment stage, paid only when
the current transaction requires remote transaction blocks:

- decision point in [`TransactionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3454)
- send in [`StartRemoteTransactionBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L242)
- state materialization text assembled from [`BeginTransactionCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L365) and [`ActiveSubXactContexts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L1106)
- completion when the state machine clears the bootstrap results in [`TransactionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3500)

This stage covers:

- worker `BEGIN`
- transaction-property replay
- `SET LOCAL` / savepoint replay for late joiners
- `assign_distributed_transaction_id(...)`

This is the correct category for "distributed transaction management" at the
start of worker participation.

### 5. Remote statement dispatch

This is the coordinator-side act of handing the actual shard SQL command to the
worker connection:

- placement/session binding in [`StartPlacementExecutionOnSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3865)
- query send in [`SendNextQuery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3935)
- underlying libpq send in [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L566) or [`SendRemoteCommandParams()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L525)

For a sequential timeline, this should be a short control stage representing
"the coordinator dispatched the remote command". It is distinct from the later
wait/result stages.

### 6. Remote statement flush

This is the coordinator-side post-dispatch socket flush for task-query rounds:

- send-side libpq queueing in
  [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L628)
  or [`SendRemoteCommandParams()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L559)
- flush-to-socket progression in
  [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3846)
- stage transition from flush to wait in
  [`TransitionWorkerSessionFlushToWait()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3064)

This stage exists because `PQsendQuery*()` only guarantees the command is in the
local libpq output buffer. It does **not** mean all bytes have already been
flushed to the socket. For latency analysis, that gap belongs to the
communication stack and should not be folded into backend execution.

### 7. Remote execution / ACK wait

This is the period where the coordinator has already dispatched a remote command
and is waiting for the worker session to become non-busy:

- readiness progression in [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3710)
- blocking wait in [`FinishConnectionIO()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L824)
- multi-connection variant in [`WaitForAllConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L929)
- one-shot command-result wrapper in [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L685)

This stage is where most exposed RTT lives. It should represent:

- network propagation
- worker-side SQL execution while the coordinator is waiting
- remote command completion / ACK visibility

This is the right category for "orchestration commands" and "command completion/ACK"
on the coordinator-to-worker side.

### 8. Worker result return / drain to coordinator

Once the connection is ready, the coordinator drains the returned result objects:

- `PQgetResult()` extraction in [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4022)
- result-status handling and row extraction in [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4067)

For the simple `UPDATE ... RETURNING one_row` case, this stage is usually a
single compact segment after the remote wait stage. For row-heavy queries it can
repeat many times, because Citus enables single-row mode in [`SendNextQuery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4005).

For a general-purpose timeline, it is therefore better modeled as a distinct
"worker result stream" stage rather than merged into the remote wait stage.

### 9. Distributed transaction finish

This is the optional worker-side end-of-transaction stage:

- commit send in [`StartRemoteTransactionCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L500)
- coordinated fan-out in [`CoordinatedRemoteTransactionsCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1139)
- 2PC prepare path in [`CoordinatedRemoteTransactionsPrepare()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1060)

For the simple single-shard explicit transaction, this usually means one remote
`COMMIT` round. In autocommit single-statement fast paths, this stage is absent.

### 10. Worker session release

This is the optional coordinator-side transaction-end cleanup of worker session
state after the remote transaction has finished:

- transaction-end connection handling in
  [`AfterXactHostConnectionHandling()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1586)
- connection reset in
  [`ResetRemoteTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1093)
- connection shutdown decision in
  [`ShouldShutdownConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1666)

This stage is mostly coordinator-local control-plane work, but it still belongs
to the communication stack taxonomy because it is Citus session/connection
bookkeeping, not semantic SQL execution.

### 11. Coordinator response back to the client

This is the coordinator-to-client output side:

- row serialization/send in [`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L306)
- command completion in [`EndCommand()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L169)
- final `ReadyForQuery` message and flush in [`ReadyForQuery()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L262)

This category is distinct from worker-result return. One is worker-to-coordinator
communication; the other is coordinator-to-client communication.

## Recommended coarse timeline shape

If the goal is to keep the timeline interpretable, the coarse stage order for a
single transaction should be:

1. optional client session establishment
2. client command ingress
3. coordinator/backend local execution
4. optional worker execution-session acquisition
5. optional distributed transaction attach/bootstrap
6. remote statement dispatch
7. remote statement flush
8. remote execution / ACK wait
9. worker result return / drain to coordinator
10. coordinator/backend local execution
11. optional distributed transaction finish
12. optional worker session release
13. coordinator response back to client

The two repeated "coordinator/backend local execution" spans intentionally absorb:

- parse/planning/executor work
- coordinator bookkeeping such as [`UseCoordinatedTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L341)
- local tuple materialization such as [`ReceiveResults_BuildTuples`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4244)

That is the right tradeoff for the current goal, because it keeps the network
timeline explicit without overfitting the local PostgreSQL internals.

## All optional stages should still be part of the model

For end-to-end latency as seen by the client, the "optional" stages above are
still real stages. They are only optional in the sense that some scenarios do
not trigger them.

So the measurement model should:

- define **all** stages once
- record zero/absent duration when a scenario does not trigger a stage
- keep the same stage vocabulary across experiments

Examples:

- short-lived client sessions should include the client-to-coordinator session setup stage
- warm sticky client sessions will simply have zero/absent duration there
- cold worker first-touch should include worker execution-session acquisition
- warm cached worker sessions will simply skip that stage
- explicit transactions should include worker transaction attach and worker transaction finish
- autocommit fast paths may skip one or both

That makes cross-scenario comparisons much cleaner.

## Classification boundary: communication stage includes control-plane bookkeeping

The intended boundary for this latency breakdown is:

- **actual work** = backend work that directly performs the semantic task of the
  stage
- **communication stage** = everything else needed to drive remote/session/placement/command
  progress, including local control-plane bookkeeping

That means some local CPU work still belongs to the communication stage when its
purpose is to drive remote execution rather than perform the SQL task itself.

Examples that should count as **communication stage**, not "backend execution":

- placement/session bookkeeping such as [`AssignPlacementListToConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L368)
- remote session state-machine progress in [`ConnectionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L2990)
- remote-command send/drain orchestration in [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L566), [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3710), and [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4022)

Examples that should count as **actual work**:

- worker backend startup/authentication during session establishment
- worker execution of `BEGIN ... assign_distributed_transaction_id(...)`
- worker execution of the actual `UPDATE ... RETURNING`
- worker row/result production
- coordinator-side local planning/executor work before the communication stage starts

So the useful high-level split for each network-related stage is:

- `stage_total`
- `actual_work`
- `communication_side = stage_total - actual_work`

If desired later, `communication_side` can be further split into:

- local control-plane CPU
- residual wait/transport/protocol delay

But for the current research goal, the primary separation is **actual work vs communication side**.

## Two-sided measurement is required to separate worker execution from communication residual

If we only measure from the coordinator, then a remote command phase is inherently
collapsed into one observed span:

- coordinator sends the command
- network carries it to the worker
- worker backend executes it
- worker sends the result/ACK back
- coordinator receives enough of the result to proceed

So a coordinator-only timer cannot honestly answer:

- how much was worker SQL execution
- how much was network/control residual

The only sound approach is **two-sided measurement**:

- coordinator-side spans for the client-visible transaction timeline
- worker-side spans for the backend work that happened inside those remote rounds

Then the communication-side portion can be inferred as the difference between
the coordinator-observed composite span and the worker-side backend work.

This does **not** give pure wire latency. The communication-side portion still
contains protocol, kernel/socket waits, scheduler delay, and any queueing/control
overhead. But that is exactly the category that should grow when network
congestion or control-path contention gets worse.

## Recommended decomposition of each network stage

### A. Client <-> coordinator session establishment

Measure:

1. end-to-end session establishment from the client harness
2. coordinator-local backend startup/auth work in:
   - [`BackendInitialize()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c#L122)
   - [`PerformAuthentication()`](/data/dbcomm/postgres-citus/src/backend/utils/init/postinit.c#L193)
   - [`ClientAuthentication()`](/data/dbcomm/postgres-citus/src/backend/libpq/auth.c#L390)

Interpretation:

- if end-to-end connect grows but backend startup/auth stays stable, the residual
  is likely in network handshake / socket wait / external load
- if backend startup/auth itself grows, then the slowdown is not primarily the wire

### B. Client command ingress

Measure:

1. coordinator-side frontend message ingress span in:
   - [`SocketBackend()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L430)
   - [`pq_getmessage()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c#L1238)
   - [`pq_getbytes()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c#L1086)

Interpretation:

- this is the backend-side protocol/body receive cost for the command itself
- it is not the full client-side send-to-server latency because the stage starts
  only after the message type byte is already available

### C. Coordinator <-> worker execution-session acquisition

Measure:

1. coordinator-observed worker session acquisition span:
   - cache lookup at [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L466)
   - connect start at [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1362)
   - connect poll progression at [`ConnectionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L2990)
2. worker-local backend startup/auth work on the newly created internal backend:
   - [`BackendInitialize()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c#L122)
   - [`PerformAuthentication()`](/data/dbcomm/postgres-citus/src/backend/utils/init/postinit.c#L193)
   - [`ClientAuthentication()`](/data/dbcomm/postgres-citus/src/backend/libpq/auth.c#L390)

Interpretation:

- coordinator total minus worker startup/auth local work is the communication-side
  portion of session establishment
- that communication-side portion is the right place to look for congestion-related increase

### D. Distributed transaction attach/bootstrap

Measure:

1. coordinator composite span:
   - state-machine branch in [`TransactionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3454)
   - send in [`StartRemoteTransactionBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L242)
   - completion after the begin result is cleared in [`TransactionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3500)
2. worker-local execution span for the actual bootstrap command
   - worker receives and executes the `BEGIN ...; SELECT assign_distributed_transaction_id(...)`
   - worker-side distributed transaction identity is established by [`assign_distributed_transaction_id()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L145), which also calls [`SetLoggerDistributedTransactionIdentity()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L186)

Interpretation:

- coordinator attach round minus worker attach execution gives the communication-side
  portion of distributed transaction attach
- if that grows but worker attach execution stays flat, the slowdown is likely not in worker SQL execution itself

### E. Remote work command round

This is the most important case for your workload.

Measure:

1. coordinator composite remote-command span:
   - placement/session bind in [`StartPlacementExecutionOnSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4009)
   - dispatch in [`SendNextQuery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4097)
   - post-dispatch flush in [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3846)
     and [`TransitionWorkerSessionFlushToWait()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3064)
   - wait/progress in [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3846), [`FinishConnectionIO()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L824), or [`WaitForAllConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L929)
   - drain start in [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4209)
2. worker-local backend execution span for the same command
   - the worker backend is just a PostgreSQL backend executing the received SQL
   - its active query-cycle work is already grounded by [`BeginQueryTimingCycle()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L727) / [`FinishQueryTimingCycle()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L762)
   - the local statement core is in [`exec_simple_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1326) or the extended-protocol path in [`PostgresMain()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L5124)
3. worker-local response generation/output span
   - row send in [`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L306)
   - command-complete in [`EndCommand()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L169)
   - final ready/flush in [`ReadyForQuery()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L262)
4. coordinator-local result ingest span
   - result drain in [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4022)
   - local tuple materialization in [`ReceiveResults_BuildTuples`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4244)

Interpretation:

- the worker-local execution/output spans tell you how much time was genuine worker backend work
- the remainder of the coordinator-observed remote round is the communication-side portion

That communication-side portion is the right bucket for:

- network congestion
- socket wait
- kernel transport overhead
- protocol/control overhead
- scheduling delay while either side waits to make progress

It is more honest to call this **communication side** than pure network time.

### F. Worker result return

For result-return-heavy queries, this should remain a separate measured stage rather
than being buried inside the command round.

Measure:

1. worker output work:
   - [`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L306)
2. coordinator receive-side work:
   - [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3710)
   - [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4022)
3. residual between the two

Interpretation:

- if worker row production stays flat but the communication-side portion grows,
  the slowdown is likely in transport/control rather than SQL execution

### G. Distributed transaction finish

Measure:

1. coordinator composite commit/abort round:
   - [`StartRemoteTransactionCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L500)
   - [`CoordinatedRemoteTransactionsCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1139)
2. worker-local commit handling on the internal backend
3. communication-side time

The same logic applies as for transaction attach.

### H. Worker session release

Measure:

1. coordinator-side release/reset span in:
   - [`AfterXactHostConnectionHandling()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1586)
   - [`ResetRemoteTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1093)
2. communication-side cleanup work only

Interpretation:

- this stage is typically coordinator-local control-plane work, not worker SQL
  execution
- if it grows materially, the slowdown is in session/connection lifecycle
  management rather than the semantic transaction work itself

## Placement binding reminder

Placement binding is local Citus bookkeeping that pins placement access to a
specific worker session/connection:

- placement access list built by [`PlacementAccessListForTask()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/placement_access.c#L26)
- binding applied by [`AssignPlacementListToConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L368)
- send-side call site in [`StartPlacementExecutionOnSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3865)

Why it exists:

- preserve read-your-own-writes
- preserve lock visibility
- avoid conflicting access to the same placement from different sessions inside the same logical transaction

Under the intended classification boundary here, this does belong to the
**communication stage**, because it is control-plane bookkeeping whose purpose
is to safely drive remote execution over the chosen worker session.

## Correlation requirement: transaction id alone is not enough

One subtle but important point: a distributed transaction can contain multiple
worker command rounds.

So for measurement correlation, distributed transaction identity alone is not
sufficient. The design should also carry a per-remote-command identifier, for
example something derived from:

- worker session id such as [`session->sessionId`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1784)
- connection id such as [`connection->connectionId`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1389)
- per-session command sequence such as [`session->commandsSent`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3912)

Existing helpers like [`LogRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L346) already expose `connectionId` in logs, but that is still not a complete correlation key by itself.

Without an explicit per-command id, repeated `UPDATE` / `COMMIT` / `BEGIN` rounds
inside one distributed transaction become ambiguous to join across coordinator
and worker traces.

## Correlation feasibility by stage

This section answers a narrower question:

"For each stage, what identifiers already exist to join coordinator and worker
logs, and where are they insufficient?"

The answer is different for each stage.

### 1. Client <-> coordinator session establishment

#### Existing identifiers

- client harness timestamps and client-side connection handle
- coordinator backend PID from the PostgreSQL log prefix
- connection-authorized log line emitted after auth in [`PerformAuthentication()`](/data/dbcomm/postgres-citus/src/backend/utils/init/postinit.c#L256)
- optional `application_name` printed in that same log message at [`postinit.c:270`](/data/dbcomm/postgres-citus/src/backend/utils/init/postinit.c#L270)

#### Correlation quality

This stage is already correlatable with high confidence.

The client can record:

- connect start
- connect established
- first query sent

The coordinator log can identify the server-side backend that authenticated that
session via the backend PID and the `connection authorized` log line.

#### Split feasibility

Good.

- `stage_total`: client-observed connect/auth latency
- `actual_work`: coordinator-local startup/auth timing
- `communication_side`: difference

No cross-node correlation is needed here.

### 2. Coordinator <-> worker execution-session acquisition

#### Existing identifiers

On the coordinator side:

- target host/port in [`GetConnParams()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_configuration.c#L246)
- coordinator-side connection id in [`connection->connectionId`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1389)
- remote command logging includes host/port and `connectionId` in [`LogRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L346)
- coordinator backend global pid is embedded into the worker connection `application_name` by [`GetConnParams()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_configuration.c#L257)

On the worker side:

- the internal backend sees that inherited application name in auth/startup
- Citus identifies that backend type using [`DetermineCitusBackendType()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L1480) and the auth hook in [`CitusAuthHook()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c#L3218)
- PostgreSQL can log `connection authorized ... application_name=...` from [`PerformAuthentication()`](/data/dbcomm/postgres-citus/src/backend/utils/init/postinit.c#L256)

#### Correlation quality

Partially correlatable as-is.

What is already possible:

- identify that a worker internal backend belongs to a given originating coordinator backend, because `application_name` includes the coordinator global pid
- identify the worker node from the log file / node source itself

What is missing:

- there is no worker-visible copy of coordinator `connectionId`
- so if one coordinator backend opens multiple concurrent remote sessions to the same worker node, those worker startup/auth events are ambiguous relative to coordinator connection objects

#### Split feasibility

Possible for simple scenarios, ambiguous under concurrency.

As-is, this is reliable when:

- one coordinator backend opens at most one new remote session to that worker at that time

As-is, this is not robust when:

- the same coordinator backend opens multiple new remote sessions to the same worker concurrently

#### What extra correlation is needed

For robust correlation, worker startup/auth logs need a stable session key that
matches the coordinator-side session object, for example:

- propagate coordinator `connectionId` into worker-visible startup metadata, or
- propagate an explicit `remote_session_id`

### 3. Distributed transaction attach/bootstrap

#### Existing identifiers

- coordinator distributed transaction identity from [`SetLoggerDistributedTransactionIdentity()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L101)
- worker distributed transaction identity becomes available once [`assign_distributed_transaction_id()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L145) executes and calls [`SetLoggerDistributedTransactionIdentity()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L186)
- coordinator remote command logs can show the exact bootstrap SQL through [`LogRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L346)

#### Correlation quality

Better than session establishment, but still not fully robust.

After `assign_distributed_transaction_id()` executes, both sides share the same
distributed transaction id. That is enough to correlate:

- which distributed transaction this bootstrap belongs to
- which worker node it happened on

But it is still insufficient when:

- the same distributed transaction attaches multiple worker sessions on the same worker node
- or multiple bootstrap rounds occur close together on the same node

because there is no explicit per-session or per-command tag on the worker side.

#### Split feasibility

Good for simple one-session-per-node cases.
Ambiguous for multi-session-per-node cases.

#### What extra correlation is needed

To make this stage robust:

- carry a worker-visible session identifier or connection identifier
- or emit an explicit `remote_command_id` for the bootstrap command

### 4. Remote work command round

#### Existing identifiers

Coordinator side currently has:

- distributed transaction identity
- node host/port
- `connectionId` in [`MultiConnection`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h#L174)
- per-session command progression through [`session->commandsSent`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3912)
- `queryIndex` within a task via [`placementExecution->queryIndex`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3947)
- exact remote command text from [`SendNextQuery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3935) and [`LogRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L346)

Worker side currently has:

- distributed transaction identity after attach
- worker backend PID
- logged SQL text via [`log_message(\"Query executed: ...\")`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L5116)
- worker-side query timing blocks keyed by distributed transaction identity in [`logger_print_timings()`](/data/dbcomm/postgres-citus/src/common/time_instr.c#L565)

#### Correlation quality

For simple benchmarks, this is often correlatable heuristically.

For example, if all of the following are true:

- one worker node
- one remote session
- one outstanding worker command at a time
- distinctive SQL text

then the pairing can be inferred from:

- distributed transaction id
- worker node
- SQL text
- temporal order

However, this is **not** robust enough for the general research goal.

It becomes ambiguous when:

- multiple remote commands share the same SQL text in one transaction
- multiple worker sessions on the same node participate in the same distributed transaction
- concurrent transactions execute similar SQL
- retries or internal extra commands are introduced

#### Split feasibility

Only heuristically possible as-is.

For a robust `actual_work` vs `communication_side` split of the remote command
round, an explicit per-command correlation key is needed.

#### What extra correlation is needed

The minimum robust design is:

- coordinator emits `remote_command_id`
- worker logs that same `remote_command_id`

That id should be bound to a single remote round such as:

- worker tx attach
- worker DML
- worker COMMIT

Good ingredients already exist locally:

- [`session->sessionId`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1784)
- [`connection->connectionId`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1389)
- [`session->commandsSent`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3912)
- [`placementExecution->queryIndex`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3947)

But today that composite identity is not propagated to the worker logs.

### 5. Worker result return

#### Existing identifiers

This stage inherits the same identifiers as the remote work command round,
because the result-return phase is logically the tail of that same remote command.

Relevant functions are:

- worker output in [`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L306)
- coordinator receive in [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4022)

#### Correlation quality

Same conclusion as the command round:

- simple cases are inferable
- general cases are ambiguous without a per-command id

#### Split feasibility

A robust split between:

- worker-side actual row/result production
- communication-side transfer/residual
- coordinator-side result ingest

requires the same `remote_command_id` correlation.

### 6. Distributed transaction finish

#### Existing identifiers

- distributed transaction identity exists on both sides by commit time
- coordinator logs and timings cover commit fan-out in [`CoordinatedRemoteTransactionsCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1139)
- worker backends execute the resulting `COMMIT`/`COMMIT PREPARED`

#### Correlation quality

Better than the generic work-command round, but still not perfect.

In simple single-session-per-node cases, `dtxid + worker node + temporal order`
is often enough.

But if the same distributed transaction uses multiple worker sessions on one node,
worker-side commit events are still ambiguous without a session/command key.

#### Split feasibility

Reasonably good for simple cases, not robust in the general case.

The same extra key as above would remove the ambiguity.

## Summary of what is correlatable today vs what needs new identifiers

### Already robust enough today

- client <-> coordinator session establishment

### Correlatable in simple scenarios, but not robust under concurrency or multiple sessions

- worker execution-session acquisition
- distributed transaction attach/bootstrap
- distributed transaction finish

### Only heuristically correlatable today

- remote work command round
- worker result return

### Missing abstraction for robust multi-node joining

The missing abstraction is not another wall timer. It is a propagated
**remote command/session identity**.

Without that, we can still analyze carefully controlled microbenchmarks, but we
cannot claim robust stage-by-stage correlation for general concurrent workloads.

## How to add the necessary ids

There are two ids we actually need:

1. `remote_session_id`
   - stable for the lifetime of one coordinator->worker SQL session
   - used to correlate worker session establishment and all later worker work on
     that backend

2. `remote_command_id`
   - unique per remote command round on that session
   - used to correlate bootstrap / DML / COMMIT / result-return spans

These should be treated separately. Trying to overload one id for both roles
becomes awkward once one remote session executes multiple commands.

## Recommended design

### A. Add `remote_session_id` to internal `application_name`

This is the cleanest place for the session-scoped id.

Why it fits:

- internal worker connections already carry an internal `application_name` from
  [`GetConnParams()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_configuration.c#L246)
- that string already starts with [`CITUS_APPLICATION_NAME_PREFIX`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h#L39)
- Citus already explicitly tolerates suffixes after the gpid in
  [`ExtractGlobalPID()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L1107), because it parses the digits with `strtoul(...)` at [`backend_data.c:1128`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L1128) and then stops
- the changelog explicitly notes that `citus_internal` `application_name` with
  additional suffix is allowed

So a safe format is something like:

- `citus_internal gpid=<origin_gpid> rsid=<remote_session_id> cid=<connection_id>`

This preserves existing behavior:

- [`DetermineCitusBackendType()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L1480) still sees the same prefix
- [`ExtractGlobalPID()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L1107) still extracts the same gpid

#### Where to generate it

Generate `remote_session_id` on the coordinator when a `MultiConnection` is
created or first assigned a stable `connectionId`, i.e. around
[`connection->connectionId = connectionId++`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1389).

Good candidates:

- reuse `connection->connectionId` directly as the per-backend session id, or
- add a new explicit `remoteSessionId` field if you want to keep the concepts separate

#### Why this is robust

- worker auth/startup logs already see `application_name`
- later worker query logs can be attributed to the same worker backend PID
- once the initial worker backend PID <-> `remote_session_id` mapping is known,
  later statements on that session remain attributable

### B. Add `remote_command_id` as a SQL comment prefix

This is the least invasive place for the command-scoped id.

Recommended format:

- `/*cituscmd:rcid=<remote_command_id> rsid=<remote_session_id> kind=<kind>*/ <original SQL>`

Examples:

- `/*cituscmd:rcid=17 rsid=5 kind=tx_attach*/ BEGIN TRANSACTION ...; SELECT assign_distributed_transaction_id(...);`
- `/*cituscmd:rcid=18 rsid=5 kind=dml*/ UPDATE ... RETURNING ...`
- `/*cituscmd:rcid=19 rsid=5 kind=commit*/ COMMIT`

Why this fits:

- SQL comments are semantically ignored by PostgreSQL parsing/execution
- the exact query text already flows through:
  - [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L566)
  - [`SendRemoteCommandParams()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L525)
- worker-side logging already exposes raw query text in:
  - simple protocol [`log_message("Query executed: %s", query_string)`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L5116)
  - parse-phase logging for the extended path at [`postgres.c:5152`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L5152)

That means the same `remote_command_id` can appear in:

- coordinator-side send logs
- worker-side received/executed query logs

#### Why this is better than `SET LOCAL` for command ids

A `SET LOCAL some_id = ...` approach would:

- add extra command work
- distort the very stages being measured
- complicate transaction semantics

The SQL-comment approach adds no extra round trip and no semantic work on the worker.

### C. Optionally mirror `remote_command_id` into timing/log identities

The SQL comment is enough for text-log correlation, but if the goal is robust
joining of timing blocks to command ids, then worker/coordinator timing reports
should also carry the same id.

The existing timing identity path is:

- coordinator/worker timing blocks are labeled by [`logger_set_identity()`](/data/dbcomm/postgres-citus/src/common/time_instr.c#L174)
- distributed transactions use [`SetLoggerDistributedTransactionIdentity()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L101)

So the stronger design is:

- keep the current transaction identity
- augment it for the duration of one remote command round with `remote_command_id`

For example:

- `conn_txn=... dtxid=... rsid=5 rcid=18`

This is not strictly necessary to carry the id over the wire, but it makes the
later log join much cleaner.

## Alternative designs

### Alternative 1: use only `application_name`

This works for `remote_session_id`, but not for `remote_command_id`.

Changing `application_name` per command would be a bad fit because:

- it is session-scoped by nature
- changing it every command is extra control work
- it creates race/ordering concerns around when the worker observes the new value

So `application_name` should be used only for session identity.

### Alternative 2: custom GUC or startup option

You could define:

- startup `options=-c citus.remote_session_id=...`
- per-command `SET LOCAL citus.remote_command_id=...`

Pros:

- structured metadata instead of parsing comments

Cons:

- more invasive
- per-command `SET LOCAL` distorts the measured path
- startup `options` still does not solve per-command identity cleanly

This is a reasonable long-term design only if you are willing to own a dedicated
metadata/control channel for command identity.

### Alternative 3: out-of-band trace events only

Another option is to avoid propagating ids through SQL text entirely and instead
emit explicit trace events on both sides from the communication stack itself.

Pros:

- cleanest eventual tracing model
- no query text modification

Cons:

- larger implementation
- you still need the worker to know which received SQL round corresponds to which
  coordinator-side command id, so some propagation mechanism is still required

So this is complementary, not a replacement for propagating the identity.

## Recommended concrete identity scheme

The most pragmatic robust scheme is:

- `remote_session_id = connectionId` or a dedicated stable id attached to `MultiConnection`
- `remote_command_id = <remote_session_id>:<per-session command sequence>`

Where:

- `connectionId` already exists in [`MultiConnection`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h#L174)
- the coordinator already has a per-session sequence-like notion in [`session->commandsSent`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3912)

For clarity and future-proofing, it is better to materialize a dedicated
`remoteCommandSequence` field rather than relying directly on `commandsSent`,
because:

- `commandsSent` is session bookkeeping, not an externally defined correlation API
- some future command types might need ids without wanting to reuse that exact counter semantics

A concrete string form could be:

- `rsid=<connectionId>`
- `rcid=<connectionId>.<remoteCommandSequence>`

Examples:

- session establishment: `rsid=5`
- tx attach round: `rcid=5.1`
- DML round: `rcid=5.2`
- COMMIT round: `rcid=5.3`

That is easy to read, cheap to generate, and unique within the originating coordinator backend.

## Practical outcome by stage

With the recommended scheme:

- client session establishment:
  - no new propagated id needed beyond normal backend PID / client harness id
- worker session establishment:
  - robustly correlated by `rsid`
- tx attach/bootstrap:
  - robustly correlated by `rcid`
- remote DML / SELECT / utility round:
  - robustly correlated by `rcid`
- worker result return:
  - inherits the same `rcid` as the originating command round
- tx finish:
  - robustly correlated by `rcid`

This is enough to make the later `actual_work` vs `communication_side` split
robust across coordinator and worker log files.

### A. Warm autocommit single statement

Typical path:

1. query-cycle start in [`BeginQueryTimingCycle()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L727)
2. coordinator parse/analyze/plan in [`exec_simple_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1352), [`exec_simple_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1476), and [`exec_simple_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1479)
3. Citus distributed planning / task generation / session selection through the adaptive executor path ending in [`SendNextQuery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3935)
4. worker DML send
5. worker DML wait/result receive via [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3710) and [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4022)
6. coordinator tuple materialization via [`ReceiveResults_BuildTuples`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4244)
7. coordinator row send to SQL client via [`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L306)
8. command-complete send in [`EndCommand()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L169)
9. final ready/flush in [`ReadyForQuery()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L262)

Network/control implication:

- no worker transaction block
- no worker `BEGIN` RTT
- no worker `COMMIT` RTT
- on the warm path, no new worker connection setup

So this case is the cleanest latency baseline.

### B. Warm explicit transaction: `BEGIN; UPDATE ... RETURNING; COMMIT;`

Typical path:

1. coordinator `BEGIN` updates local transaction state and identity; it does **not** contact workers yet
2. first distributed statement triggers coordinated mode in [`UseCoordinatedTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L341)
3. first touched worker session enters [`TransactionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3432)
4. worker bootstrap is sent via [`StartRemoteTransactionBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L242)
5. after the worker `BEGIN` result is cleared, the DML is sent by [`SendNextQuery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3935)
6. result receive/materialization/output proceeds as in the autocommit case
7. transaction commit later sends remote `COMMIT` through [`StartRemoteTransactionCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L500) and [`CoordinatedRemoteTransactionsCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1139)

Network/control implication:

- first distributed statement pays an extra worker attach/begin phase
- explicit `COMMIT` later pays another worker command phase

So this case has more network control latency even when everything is warm.

### C. Cold worker path

If the coordinator cannot reuse an idle session from [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L466), then the first worker touch also pays:

1. connection-parameter lookup in [`FindOrCreateConnParamsEntry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1408)
2. synchronous front half in [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1362)
3. asynchronous poll loop in [`MultiConnectionStatePoll()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L808)
4. blocking wait loop in [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L980)

After transaction end, reuse depends on [`AfterXactHostConnectionHandling()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1494) and [`ShouldShutdownConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1562).

Cold path therefore needs extra buckets beyond the warm transaction stages.

## How to classify the measured time

For the eventual plot, use four semantic classes instead of only "network" vs "not network":

1. **Local coordinator execution**
   - parse
   - analyze/rewrite
   - planning
   - local executor work
   - local tuple materialization

2. **Communication control / protocol CPU**
   - libpq protocol framing
   - row serialization/deserialization
   - command staging
   - `CheckConnectionReady()` post-wakeup libpq work

3. **Communication wait / RTT exposure**
   - waits in [`PG_WAIT`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L66)
   - [`PG_FE_WAIT`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L68)
   - [`XACT_WAIT`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L154)

4. **Lifecycle/setup overhead**
   - worker connection setup
   - client/coordinator startup if measured
   - final ready/flush tail

This is more honest than labeling all communication-related time as "network latency", because several important costs are CPU work in the communication stack rather than socket sleep.

## What you can plot today without changing code

### Immediate output: additive composition bars

Today, the safe plot is an additive composition bar per query or per explicit transaction using:

- denominator: `query_active_wall_ns`
- communication buckets: values produced by [`build_bucket_values()`](/data/dbcomm/postgres-citus/fdl_utils/timing_bucket_utils.py#L203)
- transaction aggregation: [`aggregate_transaction_timing.py`](/data/dbcomm/postgres-citus/fdl_utils/aggregate_transaction_timing.py#L1)

Recommended immediate bar segments:

- `transport_protocol_cpu_ns`
- `row_serde_cpu_ns`
- `result_materialization_cpu_ns`
- `optional_connection_setup_cpu_ns`
- `remaining_local_uninstrumented_ns`

where:

- `remaining_local_uninstrumented_ns = query_active_wall_ns - known_plotted_additive_buckets - explicitly_excluded_wait_if_desired`

This "remaining" bucket will still mix ordinary Postgres/Citus local execution with some currently un-instrumented comms tails, but it is still useful as a first-pass plot.

### Immediate scenario comparison

Even without new code, you can already compare these scenarios cleanly:

- warm autocommit `UPDATE ... RETURNING`
- warm `BEGIN; UPDATE ... RETURNING; COMMIT`
- cold first-touch `UPDATE ... RETURNING`

That will already show:

- whether connection setup dominates the cold path
- how much extra control-path cost explicit transactions add
- how much of the active wall is communication stack vs everything else

## What needs code changes for a real ordered stage plot

There are two reasonable instrumentation approaches.

### Approach 1: wire existing stage timers and add a few new phase timers

This is the lower-risk option.

Add or wire:

- [`ParseQuery`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L81) around [`pg_parse_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1352)
- [`QueryAnalyzeAndRewrite`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L82) around [`pg_analyze_and_rewrite_fixedparams()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1476)
- [`QueryPlanning`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L84) around [`pg_plan_queries()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1479)
- [`QueryExecution`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L83) around [`PortalRun()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1560) and corresponding extended-protocol executor paths
- [`EndingComms`](/data/dbcomm/postgres-citus/src/include/timing_spots.h#L85) around [`EndCommand()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L169) plus [`ReadyForQuery()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L262), or split it into two explicit spots

Then add command-kind-aware Citus timers for:

- worker tx attach/begin
- worker statement send/wait/result
- worker commit/abort

Pros:

- small conceptual change
- compatible with the current logger model
- enough for stacked composition bars and rough stage grouping

Cons:

- still not a true ordered trace
- repeated phases in one transaction are still collapsed into totals
- multiple worker commands of the same kind are still merged

### Approach 2: add explicit phase trace events

This is the right option if the goal is a faithful horizontal timeline.

Instead of only accumulating totals, emit per-event records such as:

- `query_cycle_begin`
- `parse_begin` / `parse_end`
- `plan_begin` / `plan_end`
- `worker_connect_begin` / `worker_connect_end`
- `worker_tx_attach_begin` / `worker_tx_attach_end`
- `worker_dml_send_begin` / `worker_dml_send_end`
- `worker_dml_wait_begin` / `worker_dml_wait_end`
- `worker_commit_begin` / `worker_commit_end`
- `client_row_send_begin` / `client_row_send_end`
- `ready_for_query_begin` / `ready_for_query_end`

Each event should carry:

- backend pid
- distributed transaction identity from [`SetLoggerDistributedTransactionIdentity()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L101)
- optional connection-local identity from [`BeginLoggerConnectionTransactionIdentity()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L179)
- worker node / connection id where applicable
- command kind
- monotonic timestamp

Pros:

- directly supports ordered Gantt-style bars
- can distinguish worker `BEGIN` vs DML vs `COMMIT`
- works even when one transaction has repeated or multi-worker phases

Cons:

- more code and more logging volume
- needs a new parser/visualization path
- needs care to keep overhead acceptable

## Recommended practical plan

### Phase 1: composition bars now

Use the current scripts and timers to answer the first question:

"How much of transaction latency is communication-heavy versus everything else?"

Recommended output:

- one bar per scenario
- additive segments from the existing bucket utilities
- separate color family for:
  - protocol/transport CPU
  - communication wait
  - connection/setup
  - remaining local execution

This can be done immediately.

### Phase 2: wire the missing coordinator-local timers

This is the minimum code change needed to stop overloading the "remaining local execution" bucket.

Priority order:

1. `ParseQuery`
2. `QueryAnalyzeAndRewrite`
3. `QueryPlanning`
4. `QueryExecution`
5. `ReadyForQuery` tail / `EndingComms`

After that, the composition bars become much more honest.

### Phase 3: add a trace path for ordered stage plots

If the real goal is to show the sequential contribution of:

- worker connect
- worker `BEGIN`
- worker DML
- worker `COMMIT`
- client output tail

then per-event trace logging is the correct next step. Trying to reconstruct an exact ordered bar from aggregated timer totals will be misleading.

## Measurement recommendations by scope

### If the scope begins when the client sends a query on an already-open session

Use server-side timings only.

This is the cleanest way to study Citus internal latency sources.

### If the scope begins when the client initiates a brand-new connection

Use two layers:

1. client harness timestamps for:
   - connect start
   - connect established
   - first query sent
   - final result received

2. server-side timings for the in-server breakdown

Optionally add server-side startup/auth timers later, but do not pretend the current server-side query-cycle timers include connection startup. They do not.

## Bottom line

The current codebase is already good enough for **transaction latency composition** plots, especially for comparing warm autocommit, warm explicit transaction, and cold-path cases.

The current codebase is **not** yet good enough for a faithful **ordered stage timeline** because:

- coordinator-local parse/plan/execute timers are not wired
- `ReadyForQuery` tail is not separately timed
- current Citus command timers collapse different command kinds into totals
- timing reports are aggregated counters, not ordered trace spans

So the correct plan is:

1. start with composition bars using the existing bucket utilities and transaction aggregation
2. wire the missing coordinator-local stage timers
3. if ordered horizontal timelines are still needed, add explicit phase trace events instead of trying to infer them from aggregate totals
