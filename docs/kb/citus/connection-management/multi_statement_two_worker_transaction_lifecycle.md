# Explicit Multi-Statement Transaction Lifecycle Across Coordinator And Two Workers

## Scope

- Scenario: a client opens a fresh PostgreSQL session to the Citus coordinator and sends one simple-query message containing an explicit multi-statement transaction, for example:

  ```sql
  BEGIN;
  UPDATE accounts SET balance = balance + 10 WHERE user_id = 1 RETURNING balance;
  UPDATE accounts SET balance = balance - 10 WHERE user_id = 2 RETURNING balance;
  COMMIT;
  ```

- Assumptions for the concrete walkthrough:
  - `user_id = 1` routes to a shard on worker A
  - `user_id = 2` routes to a shard on worker B
  - the client session, worker-A session, and worker-B session are all cold
  - both `UPDATE`s modify data, so the transaction expands from one modified worker to two modified workers
- Focus: exact chronology, which phases are sequential vs parallel, and where communication latency is paid.

## Important correction

Client `BEGIN` does **not** immediately fan out worker `BEGIN`s.

- PostgreSQL handles the top-level `BEGIN` locally in [`BeginTransactionBlock()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c#L3873).
- Citus only records BEGIN options in [`SaveBeginCommandProperties()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/begin.c#L27), invoked from [`citus_ProcessUtility()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/utility_hook.c#L151).
- The coordinator only enters distributed-transaction mode when a later distributed statement requires it, via [`StartDistributedExecution()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1346) calling [`UseCoordinatedTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L341).
- The worker-side `BEGIN TRANSACTION ...; SELECT assign_distributed_transaction_id(...);` is emitted lazily by [`StartRemoteTransactionBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L278), normally from [`TransactionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3445).

That means the client-visible `BEGIN` statement only pays client<->coordinator cost. The first worker touched later pays the hidden coordinator<->worker bootstrap.

## Concrete chronological lifecycle

### 1. Client connects to the coordinator

The coordinator backend is created and finishes PostgreSQL startup/authentication before any SQL runs.

- postmaster-to-backend handoff enters [`BackendInitialize()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c#L131)
- libpq/backend startup packet setup also happens in [`BackendInitialize()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c#L131)
- authentication is performed by [`PerformAuthentication()`](/data/dbcomm/postgres-citus/src/backend/utils/init/postinit.c#L193)
- auth method dispatch/challenge-response runs in [`ClientAuthentication()`](/data/dbcomm/postgres-citus/src/backend/libpq/auth.c#L390)
- challenge packets are sent by [`sendAuthRequest()`](/data/dbcomm/postgres-citus/src/backend/libpq/auth.c#L684)
- password/SASL responses are read by [`recv_password_packet()`](/data/dbcomm/postgres-citus/src/backend/libpq/auth.c#L714)

Latency type:

- communication stack:
  - TCP connect
  - optional TLS / GSS handshakes
  - auth challenge/response RTTs
- backend work:
  - backend spawn/fork+init
  - HBA lookup and auth method logic
  - optional reverse DNS / conninfo setup

### 2. Coordinator enters the message loop and receives one `Query` message

- coordinator main loop is [`PostgresMain()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L4521)
- it blocks for the next frontend message in [`ReadCommand()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L5014)
- simple-protocol `Query` dispatch enters [`exec_simple_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1301)

Latency type:

- communication stack:
  - one client->coordinator request flight carrying the whole SQL text
- backend work:
  - backend wakeup, message framing, command dispatch

### 3. PostgreSQL parses the whole SQL text into raw statements

Inside [`exec_simple_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1301), PostgreSQL parses the full text once:

- parsing all raw statements happens in [`pg_parse_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1357)
- because there are multiple raw statements, [`use_implicit_block`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1384) is set
- the raw statements are then executed one-by-one in the [`foreach(parsetree_item, parsetree_list)`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1389) loop

Sequentiality:

- the statements inside the one frontend message are still **sequential** at this level
- there is no overlap between raw statements just because they were delivered in one message

Important clarification:

- this is **not** because Citus cannot see the whole SQL text
- PostgreSQL already parsed the whole text up front in [`pg_parse_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1357)
- the reason later statements do not overlap with earlier ones is that [`exec_simple_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1301) then executes the resulting raw statements one at a time through [`PortalRun(...)`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1577)
- Citus planning/execution is therefore invoked **per statement**, not once for the entire transaction text

So this is not a simple "missed planner optimization" inside current Citus. The current execution model inherited from PostgreSQL is statement-by-statement.

### 3a. Why the two `UPDATE`s are still sequential even though Citus saw the whole SQL text

For this concrete example, the first `UPDATE` on worker A and the second `UPDATE` on worker B do not overlap because PostgreSQL preserves statement boundaries and statement visibility rules.

- statement execution order is fixed by the raw-statement loop in [`exec_simple_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1389)
- each statement is run to completion via [`PortalRun(...)`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1577) before the next raw statement is processed
- after a non-transaction-control statement, PostgreSQL performs [`CommandCounterIncrement()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1625), which is part of the normal "later statements can see earlier statement effects" contract
- if an earlier statement aborts the transaction block, the next statement is rejected by the [`IsAbortedTransactionBlockState()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1427) check
- transaction-control statements also force [`finish_xact_command()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1602) or [`finish_xact_command()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1610) boundaries between loop iterations

Implication:

- Citus could only parallelize separate statements in one SQL text if it built a new cross-statement execution/speculation layer and proved that those statements are independent
- for general SQL, that is not a safe local optimization because later statements may depend on earlier writes, locks, savepoints, GUC changes, temp objects, or even just error ordering
- therefore the current sequentiality is primarily a PostgreSQL statement-execution semantic boundary, not merely an arbitrary Citus planner limitation

### 4. Statement 1: local `BEGIN`

The first raw statement is the client's `BEGIN`.

- Citus intercepts the utility path in [`citus_ProcessUtility()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/utility_hook.c#L151)
- `BEGIN` options are saved in [`SaveBeginCommandProperties()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/begin.c#L27)
- PostgreSQL marks the local transaction block in [`BeginTransactionBlock()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c#L3873)
- because this raw statement is a transaction-control statement, [`finish_xact_command()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1610) is called before moving to the next raw statement
- the coordinator emits `CommandComplete` for this statement through [`EndCommand()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L170)

Latency type:

- communication stack:
  - only coordinator->client protocol output for this statement's completion tag
- backend work:
  - transaction state transition on the coordinator
  - Citus bookkeeping of BEGIN properties

No worker traffic happens yet.

### 5. Statement 2: coordinator parse/analyze/plan for the first `UPDATE ... RETURNING`

For the first `UPDATE`, PostgreSQL and Citus perform local planning first:

- PostgreSQL analyze/rewrite runs in [`pg_analyze_and_rewrite_fixedparams()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1491)
- PostgreSQL planning runs in [`pg_plan_queries()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1494)
- Citus installed the planner hook in [`shared_library_init.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c#L481) by setting [`planner_hook = distributed_planner`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c#L482)
- distributed planning enters [`distributed_planner()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/planner/distributed_planner.c#L150)
- router modify planning happens through [`CreateModifyPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/planner/multi_router_planner.c#L213)

Latency type:

- backend work only

### 6. Statement 2: executor finalizes routing to worker A

The executor resolves the actual task/placement for this execution:

- the plan is run via [`ProcessQuery()`](/data/dbcomm/postgres-citus/src/backend/tcop/pquery.c#L136), [`ExecutorStart()`](/data/dbcomm/postgres-citus/src/backend/tcop/pquery.c#L155), and [`ExecutorRun()`](/data/dbcomm/postgres-citus/src/backend/tcop/pquery.c#L160)
- Citus custom scan initialization goes through [`CitusBeginScan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/citus_custom_scan.c#L177)
- modify-path setup happens in [`CitusBeginModifyScan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/citus_custom_scan.c#L372)
- deferred pruning / single-shard task generation happens in [`RegenerateTaskForFasthPathQuery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/citus_custom_scan.c#L643)
- the actual remote execution is entered by [`AdaptiveExecutor()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L786)

Latency type:

- backend work only

### 7. Statement 2: Citus decides that remote transaction blocks are required

Because the current statement is inside an explicit transaction block:

- transaction policy is chosen by [`DecideTransactionPropertiesForTaskList()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1274)
- [`TaskListRequiresRollback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c#L55) returns true because [`IsMultiStatementTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L1119) is true
- distributed execution setup then calls [`StartDistributedExecution()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1346)
- which activates [`UseCoordinatedTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L341)

Latency type:

- backend work only

At this point the coordinator has a distributed transaction id locally, but worker A still has not seen a remote `BEGIN`.

### 8. Statement 2: coordinator acquires a worker-A session

The first worker is cold, so the coordinator must open a libpq session to worker A.

- task-to-session assignment starts in [`AssignTasksToConnectionsOrWorkerPool()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1437)
- a node-level session is started with [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L277)
- asynchronous connection setup begins in [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1363)
- the adaptive executor progresses the libpq connect state machine in [`ConnectionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L2997)
- the nonblocking poll is [`PQconnectPoll()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3063)

On worker A, this connection looks like a normal fresh PostgreSQL client session:

- worker backend startup again goes through [`BackendInitialize()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c#L131)
- worker auth again goes through [`PerformAuthentication()`](/data/dbcomm/postgres-citus/src/backend/utils/init/postinit.c#L193) and [`ClientAuthentication()`](/data/dbcomm/postgres-citus/src/backend/libpq/auth.c#L390)

Latency type:

- communication stack:
  - coordinator->worker TCP connect
  - optional TLS / auth RTTs
- backend work:
  - worker backend spawn/startup
  - worker auth processing
  - coordinator libpq conninfo / DNS / state-machine CPU

### 9. Statement 2: placement binding and lazy remote `BEGIN` on worker A

Before sending the actual DML, Citus binds the placement and then lazily starts the remote transaction.

- placement affinity is recorded by [`AssignPlacementListToConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L368), called from [`StartPlacementExecutionOnSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3905)
- the per-session remote transaction state machine is [`TransactionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3445)
- because the connection is still in [`REMOTE_TRANS_NOT_STARTED`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3467) and blocks are required, it sends [`StartRemoteTransactionBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L278)
- the text of the worker bootstrap command is assembled by [`BeginTransactionCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L404) plus [`AssignDistributedTransactionIdCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L446)
- if there are active savepoints / `SET LOCAL`s, they are appended in the loop over [`ActiveSubXactContexts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L301)

Latency type:

- communication stack:
  - one coordinator<->worker-A command RTT for `BEGIN ...; SELECT assign_distributed_transaction_id(...);`
- backend work:
  - coordinator building the command text
  - worker executing `BEGIN`, any replayed `SAVEPOINT` / `SET LOCAL`, and `assign_distributed_transaction_id`

### 10. Statement 2: coordinator sends the first remote `UPDATE ... RETURNING` to worker A

- task dispatch is started in [`SendNextQuery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3993)
- query text is shipped through [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L636) or [`SendRemoteCommandParams()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L573)
- the worker-A backend receives and executes the SQL through PostgreSQL's normal simple-query path, including [`exec_simple_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1301), [`ProcessQuery()`](/data/dbcomm/postgres-citus/src/backend/tcop/pquery.c#L136), and [`ExecModifyTable()`](/data/dbcomm/postgres-citus/src/backend/executor/nodeModifyTable.c#L3953)
- the returning row is serialized by [`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L306)
- the coordinator observes readiness and drains results via [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3750) and [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4109)

Latency type:

- communication stack:
  - outbound request flight to worker A
  - response flight carrying the `RETURNING` row and command completion
- backend work:
  - worker-A planning/execution/storage/WAL/locking
  - worker-A row formatting
  - coordinator libpq result parsing and tuple materialization

### 11. Coordinator returns statement-2 output to the client

- the `RETURNING` row is emitted to the frontend through PostgreSQL destination receivers
- `CommandComplete` for the statement is sent by [`EndCommand()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L170)

Latency type:

- communication stack:
  - coordinator->client row and completion-tag output
- backend work:
  - coordinator row materialization / formatting

### 12. Statement 3: second `UPDATE ... RETURNING` repeats the same local planning/executor path, but now worker B joins late

The next raw statement in the same SQL text is planned and executed **after** statement 2 finishes.

- statement sequencing is still enforced by the raw-statement loop in [`exec_simple_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1389)
- the same analyze/plan path repeats locally
- the same task/session assignment path repeats in [`AssignTasksToConnectionsOrWorkerPool()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1437)
- because worker B has not yet been used in this transaction, the coordinator opens a fresh session to B through [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L277) and [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1363)
- then it performs the same lazy remote bootstrap with [`StartRemoteTransactionBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L278)
- then it sends the statement with [`SendNextQuery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3993)

Important state change:

- when the transaction expands from the already-modified worker A to newly-added worker B, [`Activate2PCIfModifyingTransactionExpandsToNewNode()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3404) can enable 2PC for the overall transaction

Latency type:

- same kinds as stages 8-11, but now for worker B

### 13. Statement 4: client `COMMIT`

The raw `COMMIT` statement is still local from the frontend point of view, but it triggers distributed closeout through transaction callbacks.

- PostgreSQL transaction closeout enters [`finish_xact_command()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1602) and the transaction machinery
- Citus hooks the lifecycle in [`CoordinatedTransactionCallback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L486)

For this concrete two-worker modifying transaction, the interesting path is:

- in [`XACT_EVENT_PRE_COMMIT`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L721), if 2PC is enabled, Citus runs [`CoordinatedRemoteTransactionsPrepare()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1122)
- that function sends `PREPARE TRANSACTION` to all relevant in-progress worker transactions using [`StartRemoteTransactionPrepare()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L824)
- it then waits for them together using [`WaitForAllConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L1020)
- it then drains each result with [`FinishRemoteTransactionPrepare()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L877)
- later, in [`XACT_EVENT_COMMIT`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c#L490), Citus finishes the distributed commit with [`CoordinatedRemoteTransactionsCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1201)
- that function sends `COMMIT PREPARED` (or plain `COMMIT` in 1PC cases) using [`StartRemoteTransactionCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L541), waits together with [`WaitForAllConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L1020), and drains results with [`FinishRemoteTransactionCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L623)

Latency type:

- communication stack:
  - one parallel fan-out RTT envelope for worker prepares
  - one parallel fan-out RTT envelope for worker final commits
- backend work:
  - worker prepare/commit processing
  - coordinator transaction-callback bookkeeping

### 14. Coordinator sends final frontend completion and `ReadyForQuery`

After the raw statements are done:

- each raw statement already emitted its own `CommandComplete` via [`EndCommand()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L170)
- after the whole simple-query cycle is done, PostgreSQL emits [`ReadyForQuery()`](/data/dbcomm/postgres-citus/src/backend/tcop/dest.c#L267) from [`PostgresMain()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L4996)

Latency type:

- communication stack:
  - final coordinator->client response tail containing the last completion and `ReadyForQuery`

### 15. Client disconnects; coordinator and worker sessions are torn down

If the client disconnects immediately after receiving the results:

- the frontend disconnect is detected in [`SocketBackend()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L403)
- PostgreSQL exits on `EOF` / `Terminate` in [`PostgresMain()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L5386)
- Citus registered session cleanup in [`RegisterConnectionCleanup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c#L852)
- backend exit then calls [`CitusCleanupConnectionsAtExit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c#L904), which closes cached worker sessions via [`ShutdownAllConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L751)

Latency type:

- communication stack:
  - client close / terminate
  - coordinator closing worker sockets
- backend work:
  - backend exit cleanup on all three nodes

## What is sequential and what can overlap

### Globally sequential in this concrete example

- client session establishment to the coordinator
- the raw statements inside one simple-query message, because [`exec_simple_query()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1389) executes them one-by-one
- within each raw statement:
  - coordinator planning happens before remote dispatch
  - worker result must return before the coordinator can finish that statement
- the second worker B only joins after the second distributed statement starts

### Potentially parallel inside Citus

- if one distributed statement needs multiple worker sessions at once, connection establishment can progress concurrently because each session is stepped by [`ConnectionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L2997) under one adaptive wait loop in [`RunDistributedExecution()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1909)
- multi-connection waits for remote bootstrap / prepare / commit fan out together via [`WaitForAllConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L1020)
- for such a multi-worker statement, worker-A and worker-B execution can overlap while the coordinator waits on multiple sockets

### Important nuance

This note's concrete SQL text touches A then B in different raw statements, so the two worker-join phases are sequential in time. If instead a **single** distributed statement targeted both A and B, then worker-A and worker-B connection acquisition, remote `BEGIN`, remote query execution, and commit/prepare fan-out could overlap within that statement.

### 15a. How a single statement can legitimately require multiple workers

The single-statement multi-worker case is already a normal part of Citus' execution model. One statement does not have to map to one worker.

At the executor level:

- [`AdaptiveExecutor()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L786) executes one statement's [`distributedPlan->workerJob->taskList`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L798)
- [`TaskListRequiresRollback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c#L55) explicitly treats `list_length(taskList) > 1` at [`executor_util_tasks.c:92`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c#L92) as a normal distributed case
- the same function also treats `list_length(task->taskPlacementList) > 1` at [`executor_util_tasks.c:97`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c#L97) as another reason one statement may involve more than one remote participant
- [`AssignTasksToConnectionsOrWorkerPool()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1437) iterates over every task in the statement at [`adaptive_executor.c:1443`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1443) and every placement for each task at [`adaptive_executor.c:1466`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1466)
- worker pools are created per node at the [`FindOrCreateWorkerPool(...)` call site](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1473)
- then [`RunDistributedExecution()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1909) drives all participating sessions together under one wait-event loop

Typical examples of one statement involving multiple workers:

- a multi-shard `SELECT`, `UPDATE`, or `DELETE` whose predicate does not prune to one shard
- a distributed join
- an aggregate such as `SELECT count(*) FROM dist_table`
- a single task that must fan out to multiple placements / replicas

That is the scenario in which the earlier "connection establishment can overlap" statement applies. It does **not** apply to this note's concrete `UPDATE-on-A; UPDATE-on-B` transaction, because there the two workers are required by two different raw statements rather than by one distributed statement.

## Communication-stack latency sources vs backend-work latency sources

### Communication-stack latency sources

- client<->coordinator TCP / TLS / authentication RTTs
- client->coordinator request flight for the `Query` message
- coordinator->worker-A and coordinator->worker-B cold connect/auth RTTs
- worker-A and worker-B lazy remote `BEGIN` RTT envelopes
- worker-A and worker-B statement dispatch / result-return RTT envelopes
- worker-A and worker-B prepare / commit RTT envelopes
- coordinator->client result rows / completion tags / `ReadyForQuery`
- disconnect / session teardown socket close path

### Backend-work latency sources

- coordinator backend spawn/startup
- coordinator parse/analyze/rewrite/plan
- Citus routing, pruning, task generation, placement bookkeeping
- worker backend spawn/startup
- worker execution of `BEGIN`, `assign_distributed_transaction_id`, `PREPARE`, `COMMIT`
- worker execution of the actual `UPDATE`
- worker row serialization in [`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L306)
- coordinator result parsing / tuple materialization

## Mermaid sequence diagram

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant Q as Coordinator backend
    participant A as Worker A backend
    participant B as Worker B backend

    Note over C,Q: Fresh client session
    C->>Q: TCP connect + startup packet + auth
    Q-->>C: auth challenges / auth ok

    C->>Q: Query("BEGIN; UPDATE(user_id=1)...; UPDATE(user_id=2)...; COMMIT;")
    Note over Q: exec_simple_query() parses all raw statements once

    Note over Q: Raw stmt 1 = BEGIN
    Q->>Q: SaveBeginCommandProperties()
    Q->>Q: BeginTransactionBlock()
    Q-->>C: CommandComplete(BEGIN)

    Note over Q: Raw stmt 2 = UPDATE for worker A
    Q->>Q: analyze/rewrite/plan + Citus routing
    Q->>Q: UseCoordinatedTransaction()
    Q->>A: cold libpq connect + worker startup/auth
    A-->>Q: auth ok / session ready
    Q->>A: BEGIN ...; SELECT assign_distributed_transaction_id(...)
    A-->>Q: BEGIN/assign result
    Q->>A: UPDATE ... RETURNING ...
    A->>A: execute update + produce RETURNING row
    A-->>Q: row + command completion
    Q-->>C: row + CommandComplete(UPDATE)

    Note over Q: Raw stmt 3 = UPDATE for worker B
    Q->>Q: analyze/rewrite/plan + Citus routing
    Q->>B: cold libpq connect + worker startup/auth
    B-->>Q: auth ok / session ready
    Q->>Q: Activate2PCIfModifyingTransactionExpandsToNewNode()
    Q->>B: BEGIN ...; SELECT assign_distributed_transaction_id(...)
    B-->>Q: BEGIN/assign result
    Q->>B: UPDATE ... RETURNING ...
    B->>B: execute update + produce RETURNING row
    B-->>Q: row + command completion
    Q-->>C: row + CommandComplete(UPDATE)

    Note over Q: Raw stmt 4 = COMMIT
    par PRE_COMMIT prepare fan-out
        Q->>A: PREPARE TRANSACTION ...
        Q->>B: PREPARE TRANSACTION ...
    end
    A-->>Q: PREPARE ok
    B-->>Q: PREPARE ok

    Note over Q: local commit reaches XACT_EVENT_COMMIT

    par COMMIT callback final fan-out
        Q->>A: COMMIT PREPARED ...
        Q->>B: COMMIT PREPARED ...
    end
    A-->>Q: COMMIT PREPARED ok
    B-->>Q: COMMIT PREPARED ok
    Q-->>C: CommandComplete(COMMIT)
    Q-->>C: ReadyForQuery

    Note over C,Q: Client disconnects
    C-xQ: Terminate / close
    Q-xA: close cached worker session
    Q-xB: close cached worker session
```

## Implications for a future waterfall plot

For this concrete scenario, the major ordered envelopes are:

1. client session setup to coordinator
2. client request flight to coordinator
3. coordinator local statement-1 work
4. statement-1 frontend completion
5. coordinator local statement-2 work
6. worker-A session acquisition
7. worker-A remote transaction attach
8. worker-A remote execution / result return
9. statement-2 frontend completion
10. coordinator local statement-3 work
11. worker-B session acquisition
12. worker-B remote transaction attach
13. worker-B remote execution / result return
14. statement-3 frontend completion
15. distributed prepare fan-out
16. distributed final commit fan-out
17. statement-4/frontend final completion and `ReadyForQuery`
18. optional client disconnect / backend exit cleanup

That is the right abstraction level for a faithful latency waterfall. Finer CPU buckets can still be nested under these envelopes, but the envelopes themselves are the chronology that the client ultimately experiences.
