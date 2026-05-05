# Single-shard `UPDATE ... RETURNING`: client-to-coordinator-to-worker network lifecycle

## Scope

- Scenario: a client issues a simple balance-style update such as `UPDATE accounts SET balance = balance + $1 WHERE user_id = $2 RETURNING balance`.
- Assumptions for the main path:
  - the table is hash distributed by `user_id`
  - the predicate prunes to one shard on one worker placement
  - Citus uses the router / fast-path router path rather than repartition or multi-shard execution
- Focus: which networked stages happen, when they happen, and which ones add end-to-end latency.
- Non-goals: tuple storage internals on the worker, WAL/fsync cost breakdown, or multi-shard / replicated-table variants except when they materially change the RTT budget.

## Canonical related notes

- Control-plane layering and connection cache semantics: `docs/kb/citus/connection-management/connection_management_control_plane.md`
- Coordinator transaction timing spots: `docs/kb/citus/transactions/timing/distributed_transaction_timing.md`
- PostgreSQL message-loop timing: `docs/kb/postgres/query-lifecycle/execsimplequery_and_message_loop.md`
- PostgreSQL FE/BE wait and socket timing: `docs/kb/postgres/libpq-io/socket_wait_and_io.md`

## Important correction: client `BEGIN` does not immediately fan out `BEGIN` to workers

That is an easy assumption to make, but it is not what current Citus does.

- The coordinator enters distributed-transaction mode in `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:341` via `UseCoordinatedTransaction()`.
- The actual worker-side `BEGIN ...; SELECT assign_distributed_transaction_id(...);` command is only emitted later, lazily, when a worker session first needs to run a task in `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:242` via `StartRemoteTransactionBegin()`.
- The comment in `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:371` explicitly documents that Citus delays remote `BEGIN` until the first query.

Latency consequence:

- a client-side `BEGIN` statement only costs client<->coordinator protocol time
- the first later distributed statement pays the hidden coordinator<->worker `BEGIN` RTT if remote transaction blocks are required

## End-to-end lifecycle

### 1. Client establishes a PostgreSQL session to the coordinator

The connection starts in PostgreSQL, before Citus planning is involved at all.

- `/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c:122` `BackendInitialize()` creates `MyProcPort` with `pq_init()`, performs remote-name lookup, and reads the startup packet.
- `/data/dbcomm/postgres-citus/src/backend/utils/init/postinit.c:193` `PerformAuthentication()` enables the auth timeout and calls `/data/dbcomm/postgres-citus/src/backend/libpq/auth.c:390` `ClientAuthentication()`.
- `/data/dbcomm/postgres-citus/src/backend/libpq/auth.c:684` `sendAuthRequest()` sends auth challenges and flushes them for non-final auth steps.
- `/data/dbcomm/postgres-citus/src/backend/libpq/auth.c:714` `recv_password_packet()` reads password/auth responses from the client.

Latency contribution:

- TCP setup: at least one network RTT before PostgreSQL application auth even starts
- optional TLS/GSS setup: additional RTTs before normal SQL traffic
- PostgreSQL auth method:
  - `trust` is cheap
  - password/SCRAM adds challenge-response RTTs
  - GSS/SSPI can add multiple back-and-forth steps
- reverse DNS in `/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c:187` is synchronous wall time, but not wire RTT on the SQL path

This exact startup/auth stack also applies later when the coordinator opens a libpq connection to a worker, because from the worker's point of view that coordinator connection is just another PostgreSQL client session.

### 2. Coordinator backend enters the PostgreSQL message loop and receives the query

- `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:4521` `PostgresMain()` is the coordinator backend main loop.
- `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:5014` `ReadCommand()` blocks waiting for the next frontend protocol message.
- For simple protocol, `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:5066` handles `PqMsg_Query` and calls `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:1299` `exec_simple_query()`.

Latency contribution:

- if the client sends `BEGIN`, `UPDATE ... RETURNING`, and `COMMIT` as three separate protocol cycles, each cycle has its own client<->coordinator request/response latency
- if the client sends `BEGIN; UPDATE ... RETURNING; COMMIT;` in one simple-query message, those frontend RTTs collapse into a single frontend request/response cycle even though the coordinator still executes the three statements serially

### 3. Coordinator parses, rewrites, and plans the update; Citus intercepts planning

- `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:1476` `pg_analyze_and_rewrite_fixedparams()` produces `Query` trees.
- `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:1479` `pg_plan_queries()` plans them.
- Citus installs the planner hook in `/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c:482` by assigning `planner_hook = distributed_planner`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/planner/distributed_planner.c:150` `distributed_planner()` decides whether distributed planning is needed and whether the query qualifies as fast-path router planning.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/planner/fast_path_router_planner.c:240` `FastPathRouterQuery()` accepts simple single-table `SELECT/UPDATE/DELETE` statements with an equality predicate on the distribution key.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/planner/multi_router_planner.c:213` `CreateModifyPlan()` builds the router-style distributed plan for `UPDATE`/`DELETE`/`MERGE`.

Latency contribution:

- no network yet
- this is pure coordinator CPU / catalog / metadata time
- for the single-shard case, fast-path planning avoids much heavier generic distributed planning

### 4. Executor initialization does deferred shard pruning and builds the single worker task

The distributed plan is not yet a live worker command. The executor finishes the last-mile routing.

- `/data/dbcomm/postgres-citus/src/backend/tcop/pquery.c:1275` `ProcessQuery()` runs the planned statement.
- `/data/dbcomm/postgres-citus/src/backend/executor/execMain.c:299` `ExecutorRun()` dispatches into the executor hook chain.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/citus_custom_scan.c:177` `CitusBeginScan()` chooses the modify path.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/citus_custom_scan.c:372` `CitusBeginModifyScan()` evaluates coordinator-only expressions, handles deferred pruning, and rebuilds task state for fast-path modifications.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/citus_custom_scan.c:643` `RegenerateTaskForFasthPathQuery()` computes the target shard interval, updates shard names, builds placements, and calls `/data/dbcomm/citus-dbcomm/src/backend/distributed/planner/multi_router_planner.c:2258` `GenerateSingleShardRouterTaskList()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/planner/multi_router_planner.c:2397` `SingleShardTaskList()` creates the actual single remote task.

Latency contribution:

- still no network
- more coordinator CPU only

### 5. Citus decides whether this statement needs explicit remote `BEGIN` / `COMMIT`

This is the main branch that changes the hidden worker RTT count.

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:1266` `DecideTransactionPropertiesForTaskList()` decides whether to require remote transaction blocks and 2PC.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:55` `TaskListRequiresRollback()` is the key rule.
- For a single-task, single-placement modifying statement outside a multi-statement transaction, `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:87` through `:116` returns false, so no explicit remote transaction block is required.
- For an explicit multi-statement transaction, `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:87` through `:90` returns true, so the statement will use remote `BEGIN` / `COMMIT`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:1339` `StartDistributedExecution()` calls `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:341` `UseCoordinatedTransaction()` when remote transaction blocks are required.

Latency consequence:

- autocommit, single-shard, single-placement update:
  - no extra worker `BEGIN` RTT
  - no extra worker `COMMIT` RTT from Citus
  - the worker still executes the statement inside its own normal PostgreSQL statement transaction
- explicit `BEGIN ... UPDATE ... COMMIT` transaction:
  - the `UPDATE` statement pays a hidden worker `BEGIN` RTT before the remote DML
  - the later client `COMMIT` pays a hidden worker `COMMIT` RTT

### 6. Coordinator acquires or opens the worker libpq session

The adaptive executor now binds tasks to sessions.

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:1430` `AssignTasksToConnectionsOrWorkerPool()` creates placement executions and tries to reuse an already-associated connection first.
- If prior same-transaction placement access exists, `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:1507` calls `GetConnectionIfPlacementAccessedInXact(...)`.
- Otherwise a new or cached node-level session comes from `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277` `StartNodeUserDatabaseConnection()`.
- Reuse comes from `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:466` `FindAvailableConnection()`.
- New connection startup begins in `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1362` `StartConnectionEstablishment()` via `PQconnectStartParams()`.
- The asynchronous libpq connect loop progresses in `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:2990` `ConnectionStateMachine()` through `PQconnectPoll()` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3055`.
- Citus also has the older bulk-establish helper `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:979` `FinishConnectionListEstablishment()` for other paths.

Important cache scope:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:53` `ConnectionHash` and `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:56` `ConnectionContext` are backend-local globals.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:99` `InitializeConnectionManagement()` creates that cache in `TopMemoryContext`.

Inference:

- worker connection reuse is per coordinator backend process, which in PostgreSQL usually means per client session connected to the coordinator
- a new client session on the coordinator does not inherit another backend's warm worker connections

Latency contribution:

- cold path: worker TCP + worker startup/auth + libpq connect polling can dominate the first statement
- warm path: this stage collapses to a cache lookup with no network

### 7. Placement affinity is pinned before sending the remote command

For writes inside a coordinated transaction, Citus must preserve placement-to-session affinity.

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3865` `StartPlacementExecutionOnSession()` is the send-side entry for a task placement on a worker session.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3875` `PlacementAccessListForTask(...)` builds the placement access description.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3884` `AssignPlacementListToConnection(...)` pins that placement to the chosen connection.
- The placement mapping logic itself lives in `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:281` `StartPlacementListConnection()` and `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:368` `AssignPlacementListToConnection()`.

Latency contribution:

- normally no network here
- this stage matters because it determines whether a warm cached connection can be reused safely or whether Citus must open a different session

### 8. If needed, Citus lazily sends remote `BEGIN ...; SELECT assign_distributed_transaction_id(...)`

When remote transaction blocks are required, the first real worker task pays this extra phase.

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3432` `TransactionStateMachine()` sees `REMOTE_TRANS_NOT_STARTED` and, when blocks are required, calls `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:242` `StartRemoteTransactionBegin()`.
- That function appends the actual `BEGIN TRANSACTION ...` string from `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:365` `BeginTransactionCommand()` plus the distributed transaction ID assignment.
- Completion is handled in `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:439` `FinishRemoteTransactionBegin()`.
- The generic asynchronous send/wait plumbing underneath is `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:566` `SendRemoteCommand()`, `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:685` `GetRemoteCommandResult()`, and `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:928` `WaitForAllConnections()`.

Latency contribution:

- one extra coordinator<->worker command RTT on the critical path
- on a one-worker query there is no parallelism benefit here; parallel begin only helps when multiple worker sessions participate

### 9. Coordinator sends the remote `UPDATE ... RETURNING` to the worker

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3935` `SendNextQuery()` fetches the task query string and sends it with either `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565` `SendRemoteCommand()` or `SendRemoteCommandParams()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4005` immediately enables `PQsetSingleRowMode()` for the worker session.

Worker-side execution then follows ordinary PostgreSQL execution, because the worker is just a PostgreSQL backend receiving a query over libpq:

- `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:1299` `exec_simple_query()` drives parse/rewrite/plan/portal execution for simple protocol.
- `/data/dbcomm/postgres-citus/src/backend/tcop/pquery.c:1188` `PortalRunMulti()` calls `/data/dbcomm/postgres-citus/src/backend/tcop/pquery.c:136` `ProcessQuery()`.
- `/data/dbcomm/postgres-citus/src/backend/tcop/pquery.c:155` `ProcessQuery()` calls `ExecutorStart()` and `/data/dbcomm/postgres-citus/src/backend/tcop/pquery.c:160` `ExecutorRun()`.
- `/data/dbcomm/postgres-citus/src/backend/executor/nodeModifyTable.c:3953` `ExecModifyTable()` performs the actual update and produces `RETURNING` rows.

Latency contribution:

- at minimum, one coordinator<->worker request/first-result RTT for the remote DML itself
- plus worker execution time before the first result row is ready
- plus payload transfer time for the returning row and protocol messages

### 10. Coordinator waits for the worker socket, receives the returning row, and materializes it

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3710` `CheckConnectionReady()` drives the post-wakeup libpq work and socket state changes.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:823` `FinishConnectionIO()` handles `PQflush()`, `PQconsumeInput()`, and `WaitLatchOrSocket()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4022` `ReceiveResults()` loops over `PQgetResult()`.
- Because single-row mode is enabled, `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4106` expects `PGRES_SINGLE_TUPLE` rows and later the terminal `PGRES_TUPLES_OK`.

Latency contribution:

- the socket wait here is the actual coordinator-visible "worker is not done yet" portion
- for a single returned row this is usually still one remote command phase, not an extra command RTT, but the row cannot be forwarded until the coordinator has received and materialized it

### 11. Coordinator returns the row and command completion to the client

After Citus has the worker row, the rest is ordinary PostgreSQL frontend output.

- `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:1560` `PortalRun(...)` returns to `exec_simple_query()`.
- `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:1624` `EndCommand()` sends `CommandComplete`.
- `/data/dbcomm/postgres-citus/src/backend/tcop/dest.c:262` `ReadyForQuery()` emits the transaction-state byte and flushes output with `pq_flush()`.

Latency contribution:

- one coordinator->client response flight for the returned row(s), `CommandComplete`, and final `ReadyForQuery`
- this is the last frontend-visible tail even after worker work is finished

### 12. At commit or transaction end, Citus either commits remotely or just releases the cached session

Explicit transaction case:

- the client's `COMMIT` runs through normal PostgreSQL transaction closeout at `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:3080` `finish_xact_command()` and `/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:3093` `CommitTransactionCommand()`
- Citus hooks transaction end in `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:486` `CoordinatedTransactionCallback()`
- on commit it calls `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1139` `CoordinatedRemoteTransactionsCommit()`
- that sends `COMMIT` or `COMMIT PREPARED` with `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:500` `StartRemoteTransactionCommit()` and waits for completion in `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:573` `FinishRemoteTransactionCommit()`

Connection cache cleanup / reuse:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1494` `AfterXactHostConnectionHandling()` decides whether to keep or close each worker session
- healthy idle sessions are reset with `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1031` `ResetRemoteTransaction()` and unclaimed for reuse
- unhealthy / non-cacheable sessions are shut down according to `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1562` `ShouldShutdownConnection()`

Latency contribution:

- explicit transaction: one extra worker `COMMIT` RTT on the critical path of the client `COMMIT`
- autocommit single statement: no separate worker commit phase from Citus; only cache cleanup remains

## Round-trip budget for the main variants

### Variant A: explicit `BEGIN; UPDATE ... RETURNING; COMMIT;`, warm worker session

Hidden worker-side RTTs on the critical path:

1. remote lazy `BEGIN` during the `UPDATE`
2. remote `UPDATE ... RETURNING`
3. remote `COMMIT` during the client's `COMMIT`

So the steady-state warm explicit transaction is usually at least **3 worker command RTTs**, even though only two client statements (`UPDATE`, `COMMIT`) do meaningful remote work.

### Variant B: explicit transaction, cold worker session

Variant A plus worker connection establishment:

1. worker TCP connect
2. worker startup/auth exchange
3. remote lazy `BEGIN`
4. remote `UPDATE ... RETURNING`
5. remote `COMMIT`

This is why the first distributed statement in a session is often much slower than the second.

### Variant C: autocommit single-shard single-placement `UPDATE ... RETURNING`, warm worker session

This is the cheapest interesting case:

1. remote `UPDATE ... RETURNING`

No extra Citus-managed worker `BEGIN` / `COMMIT` is required because `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:87` through `:116` leaves `useRemoteTransactionBlocks` optional for the single-task single-placement autocommit case.

### Variant D: same SQL text, but the client sends `BEGIN`, `UPDATE`, `COMMIT` as separate frontend cycles

The worker-side cost is unchanged from Variant A/B, but the client<->coordinator cost is worse because the frontend waits three times for `CommandComplete` / `ReadyForQuery` instead of once.

## Where latency usually accumulates

### Cold-path cost: worker connect/auth dominates

The first statement on a new coordinator backend can pay:

- coordinator<->worker TCP setup
- optional TLS/auth RTTs on the worker session
- synchronous DNS/conninfo setup before `PQconnectPoll()`

That entire stage disappears on a warm cached worker session.

### Explicit transaction cost: hidden worker `BEGIN` and `COMMIT`

For single-shard OLTP-style updates, this is the biggest semantic surprise:

- client `BEGIN` is local only
- first distributed statement later pays worker `BEGIN`
- client `COMMIT` later pays worker `COMMIT`

So explicit transaction semantics add worker RTTs even when only one shard and one worker are involved.

### Result wait cost: worker execution + return flight + coordinator receive path

For `RETURNING`, the coordinator cannot answer the client until:

- the worker finishes the update enough to produce the row
- the row arrives over libpq
- the coordinator materializes it and sends it onward

Single-row mode keeps this streaming-friendly, but does not remove the wait.

### Frontend protocol shape matters

The same SQL semantics can have different client-visible latency depending on protocol usage:

- one simple-query message containing multiple statements reduces client<->coordinator RTTs
- separate `BEGIN`, `UPDATE`, `COMMIT` messages increase them
- prepared/extended protocol changes frontend message shape again, though the coordinator<->worker phases above still exist

## Practical latency model for this specific query

For a warmed-up coordinator backend, warmed-up worker session, explicit transaction, one worker, one returned row:

`end_to_end_latency`
`= client_to_coordinator_request`
`+ coordinator_parse_plan_executor_cpu`
`+ worker_BEGIN_RTT`
`+ worker_UPDATE_execution_and_result_RTT`
`+ coordinator_receive_and_forward_row`
`+ client_commit_request`
`+ worker_COMMIT_RTT`
`+ final_CommandComplete_ReadyForQuery_flush`

For the cheapest warmed autocommit single-statement case, that collapses roughly to:

`end_to_end_latency`
`= client_to_coordinator_request`
`+ coordinator_parse_plan_executor_cpu`
`+ worker_UPDATE_execution_and_result_RTT`
`+ coordinator_receive_and_forward_row`
`+ final_CommandComplete_ReadyForQuery_flush`

The delta between those two formulas is exactly why explicit multi-statement transactions are more expensive than autocommit for single-shard single-placement OLTP traffic, even before considering lock hold time or fsync/WAL effects.

## Why Citus does not eagerly send worker `BEGIN`

The short answer is that at client `BEGIN` time Citus usually does not yet know **which** worker sessions should exist, and speculatively opening them would often create extra work rather than hide latency.

### 1. `UseCoordinatedTransaction()` is intentionally cheaper than opening remote sessions

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:341` `UseCoordinatedTransaction()` only marks the coordinator-side distributed transaction state and identity.
- It does **not** have a task list, placement list, or chosen worker session yet.
- The actual worker task/placement/session binding is only derived later in the executor through `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/citus_custom_scan.c:372` `CitusBeginModifyScan()`, `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/citus_custom_scan.c:643` `RegenerateTaskForFasthPathQuery()`, and `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:1430` `AssignTasksToConnectionsOrWorkerPool()`.

So an eager worker `BEGIN` at client `BEGIN` time would have to either:

- guess which workers will participate before shard pruning and placement choice are done, or
- pre-open transactions on far more workers than will actually be used

Both are bad for latency and resource footprint.

### 2. Citus deliberately avoids remote transaction blocks for the cheap single-statement case

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:55` `TaskListRequiresRollback()` only requires remote transaction blocks for cases such as multi-statement transactions, multi-task DML/DDL, multi-placement writes, or sequential multi-query tasks.
- The single-task, single-placement autocommit update falls through that check at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:87` through `:116`.

If Citus eagerly sent worker `BEGIN` whenever a transaction might become distributed, it would destroy the current optimization for the most latency-sensitive OLTP shape: autocommit single-shard single-placement DML.

### 3. Placement-to-session safety is decided after session selection, not before

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:368` `AssignPlacementListToConnection()` records which placement must stay on which session for read-your-own-writes and lock safety.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3865` `StartPlacementExecutionOnSession()` only pins that mapping after a concrete session is chosen.

That means eager remote `BEGIN` would not just be “open transaction earlier”; it would also force Citus to guess the session that later placement-safe execution must reuse. Today the code intentionally separates those decisions.

### 4. The hidden worker `BEGIN` RTT is already deferred to the first statement that proves it is needed

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3432` `TransactionStateMachine()` only calls `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:242` `StartRemoteTransactionBegin()` when a concrete session is about to run a task and remote transaction blocks are actually required.

Inference:

- this is not just an implementation accident
- it is a consequence of Citus keeping “distributed transaction intent” separate from “which concrete worker session is safe and necessary to use”

## What “warm autocommit” means

`warm autocommit` is not a SQL feature or a PostgreSQL keyword. It is shorthand for:

- the client is **not** in an explicit transaction block
- the current statement is a single top-level statement, so PostgreSQL auto-commits it locally
- the coordinator backend already has a reusable cached worker session for the target node

### PostgreSQL-side autocommit

- `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:3052` `start_xact_command()` starts the local coordinator transaction command.
- `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:3080` `finish_xact_command()` closes it with `/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:3093` `CommitTransactionCommand()`.

So even “autocommit” still means PostgreSQL opens and commits a local transaction around the statement. It just is not an explicit user-managed transaction block.

### Explicit `BEGIN; UPDATE ... RETURNING; COMMIT;` is not autocommit even if sent as one SQL text

- `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:1377` `exec_simple_query()` uses an implicit transaction block whenever one simple-query message contains multiple statements.
- `/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:4275` `BeginImplicitTransactionBlock()` enters that implicit block.
- `/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:4915` `IsTransactionBlock()` treats any non-idle block state as a transaction block.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:1119` `IsMultiStatementTransaction()` returns true whenever `IsTransactionBlock()` is true.

Consequences:

- `UPDATE ... RETURNING` by itself: autocommit single statement
- `BEGIN; UPDATE ... RETURNING; COMMIT;` in one simple-query string: still an explicit multi-statement transaction
- `UPDATE ...; SELECT ...;` in one simple-query string without explicit `BEGIN`: still an implicit multi-statement transaction from PostgreSQL’s point of view

So “warm autocommit” means the client sent a single statement like `UPDATE ... RETURNING` outside an explicit or implicit multi-statement block, and the worker session was already cached.

### Why “warm”

At transaction end Citus keeps a healthy worker session cached if `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1562` `ShouldShutdownConnection()` returns false. In that case:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1494` `AfterXactHostConnectionHandling()` calls `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1031` `ResetRemoteTransaction()`
- then it calls `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1315` `UnclaimConnection()`

That leaves the connection reusable for the next statement in the **same** coordinator backend.

## Why worker connection reuse is backend-local today

Yes: in the normal PostgreSQL architecture, current Citus connection reuse is effectively limited to the same coordinator backend lifetime, which in the common direct-connection case means the same client session connected to the coordinator.

### 1. The cached object is a process-private `PGconn *`, not a shareable service object

- `/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:162` `MultiConnection` embeds the raw libpq handle at `/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:171` `pgConn`.
- The connection cache itself is backend-local global state in `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:53` `ConnectionHash` and `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:56` `ConnectionContext`.
- That cache is created per backend in `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:99` `InitializeConnectionManagement()`.

Current PostgreSQL backends are separate processes. A raw libpq `PGconn *` plus its socket, buffers, SSL/GSS state, callbacks, and wait integration is not a reusable cross-process object in the current design.

### 2. `MultiConnection` carries backend-private transaction and placement state, not just a socket

- `/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:208` `MultiConnection.remoteTransaction`
- `/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:218` `MultiConnection.referencedPlacements`
- `/data/dbcomm/citus-dbcomm/src/include/distributed/remote_transaction.h:61` `RemoteTransaction`
- `/data/dbcomm/citus-dbcomm/src/include/distributed/remote_transaction.h:88` `RemoteTransaction.beginSent`

That state is tied to one backend’s current transaction, savepoint history, and placement-affinity bookkeeping. Handing the same live session to another backend is not just a pooling problem; it would also require transferring or sanitizing all of that semantic state correctly.

### 3. The implementation is wired into backend-local memory, latch, and callback machinery

Examples:

- placement references are allocated in `TopTransactionContext` in `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:405` and `:429`
- wait logic uses `MyLatch` in `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:883` and `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:906`
- connection setup installs a backend-local notice receiver in `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1399` `SetCitusNoticeReceiver(connection)`

So even ignoring correctness, the current object model is backend-private all the way down.

### 4. Correct cross-backend reuse would require a different ownership boundary

Inference:

- a truly global/shared pool is not impossible in principle
- but it cannot safely be “the current `MultiConnection` plus a shared hash table”
- it needs a service- or pool-owner that holds the durable remote session and exposes a higher-level compatibility / checkout API

That is exactly why the future-direction work moved toward a backend-visible `RemoteExecutionSession` abstraction in [remote_execution_session_control_plane.md](../future-directions/citus/connection-management/remote_execution_session_control_plane.md) and explicitly records that today’s wrapper still lacks cross-transaction backend/session reuse in [remote_execution_session_wrapper_checkpoint.md](../implementations/citus/connection-management/remote_execution_session_wrapper_checkpoint.md#L177).

## Does the `RemoteExecutionSession` direction still make sense?

Yes, with one important qualifier: it only makes sense if it replaces backend-private raw-session ownership with service-owned semantic session ownership, not if it merely wraps the current `MultiConnection` more prettily.

Grounding from the future-direction note:

- [remote_execution_session_control_plane.md](../future-directions/citus/connection-management/remote_execution_session_control_plane.md#L17) explicitly keeps the original four semantic facets:
  1. remote session compatibility and reuse
  2. placement/co-location safety
  3. remote transaction and command state
  4. operation-specific state

Why that matters:

- cross-backend reuse is only reasonable for a session that is compatibility-clean and idle
- a reusable owner must preserve or explicitly reset transaction state, savepoint state, GUC/session state, placement-affinity constraints, and operation state before handing it to a new backend
- that is a real design problem, but it is the right problem to solve if the goal is reuse across backend lifecycles

So the idea still sounds reasonable, but only if the implementation boundary really moves out of the PostgreSQL backend process.

## Hot/warm path vs cold path

### Warm path

The path is warm when a later statement in the same coordinator backend can reuse a cached worker session:

- same compatibility key (`host`, `port`, `user`, `database`, replication mode) in `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277` `StartNodeUserDatabaseConnection()`
- reusable cached match through `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:466` `FindAvailableConnection()`
- the connection survived previous transaction cleanup because `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1562` `ShouldShutdownConnection()` returned false

This is the normal steady-state OLTP path when a client keeps talking to the same coordinator backend and repeatedly hits the same worker set.

### Cold path

The path is cold when there is no reusable cached session. Typical causes:

- first distributed statement in a fresh coordinator backend
- previous worker session exceeded cache lifetime or cache count limits in `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1572` through `:1580`
- previous session was unhealthy, still in a transaction, replication-mode, or force-closed
- coordinator backend exited, so its private `ConnectionHash` disappeared
- worker restarted / connection failed and the cached session was shut down

### Extra cold-path work

Cold path adds at least:

1. connection parameter lookup and conninfo assembly through `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1408` `FindOrCreateConnParamsEntry()`
2. synchronous connect-start front half in `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1362` `StartConnectionEstablishment()` and `PQconnectStartParams()`
3. asynchronous `PQconnectPoll()` progress in `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:2990` `ConnectionStateMachine()`
4. worker-side PostgreSQL startup/auth path, which mirrors the normal backend startup/auth stack in `/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c:122` `BackendInitialize()` and `/data/dbcomm/postgres-citus/src/backend/utils/init/postinit.c:193` `PerformAuthentication()`
5. only after that, any required worker `BEGIN`

This is why cold-path latency is not just “one extra RTT”; it can include TCP startup, TLS/auth handshake, libpq state machine work, and wait-set setup before the first query even reaches execution.

## Prepared statements and prepared transactions

These are two different topics and they affect different parts of the lifecycle.

### Prepared statements

Prepared statements mainly change coordinator planning/executor behavior, not worker connection ownership.

- PostgreSQL/Citus may keep using custom plans first and only later cache a generic plan; the Citus overview in `/data/dbcomm/citus-dbcomm/src/backend/distributed/README.md:1560` explains the “first five executions” behavior.
- Citus deliberately discourages generic plans for unsupported/multi-shard parameterized cases through `/data/dbcomm/citus-dbcomm/src/backend/distributed/planner/distributed_planner.c:686` `DissuadePlannerFromUsingPlan()`.
- Fast-path prepared statements with a parameter on the distribution key use deferred pruning, documented in `/data/dbcomm/citus-dbcomm/src/backend/distributed/README.md:1581` and implemented by rebuilding the concrete task in `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/citus_custom_scan.c:643` `RegenerateTaskForFasthPathQuery()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/citus_custom_scan.c:579` through `:599` `CopyDistributedPlanWithoutCache()` explicitly preserves the original cached distributed plan and copies it per execution, because prepared statements may reuse it.

Practical consequence:

- prepared statements can reduce coordinator planning overhead on later executions
- they do **not** by themselves make worker connection establishment disappear
- if the worker session is cold, prepared statements still pay the cold-path connection/auth cost
- if the statement is single-shard autocommit, prepared statements still benefit from the no-remote-`BEGIN` fast path
- if the statement is inside an explicit transaction block, prepared statements still pay the same hidden worker `BEGIN` / `COMMIT` semantics as non-prepared execution

### Prepared transactions / 2PC

This is the distributed commit protocol topic, not the client prepared-statement topic.

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:123` `TaskListRequires2PC()` returns false for the single-task single-placement write case at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:138` through `:145`.
- It returns true for multi-task or multi-placement write cases at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:148` through `:153`.
- When 2PC is needed, `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:766` `StartRemoteTransactionPrepare()` sends `PREPARE TRANSACTION`, and commit later goes through `COMMIT PREPARED` in `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:527` through `:543`.

So for the simple one-shard one-placement `UPDATE ... RETURNING` case:

- prepared statements may affect planning
- prepared transactions/2PC usually do **not** apply

## Open edges / follow-up questions

- The worker-side SQL may use simple or extended protocol depending on how Citus sends it in a particular path; the high-level network phases above stay the same, but frontend message granularity changes.
- For replicated/reference-table writes or multi-shard plans, the RTT count increases because `TaskListRequiresRollback()` and `TaskListRequires2PC()` switch the execution into multi-connection transaction handling.
- The final coordinator->client row serialization path is standard PostgreSQL FE/BE output and is documented separately in the PostgreSQL serialization notes rather than duplicated here.
