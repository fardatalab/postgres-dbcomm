# PostgreSQL-Local Command Dispatch Bridge

## Scope

- **What this doc explains**: the long-term PostgreSQL-side bridge that should receive typed command dispatches from the external service, start the correct local execution path, and return typed completion without going through SQL UDF or libpq command/result start-wait.
- **What this doc does NOT cover**: the already-landed tuple-sink session control plane, the tuple payload ring details, or replacing every remote SQL execution path in one step.
- **Primary directory**: `docs/kb/future-directions/citus/data-movement/`
- **Doc type**: `future-direction`

## Why this exists

The current tuple-sink prototype has already split:

- session/sink ownership and peer provisioning into the tuple-sink control plane through [`OpenTupleSinkSessionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:947) and the local control mapping in [`MapRemoteExecutionControlRegion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:717)
- tuple payload into the tuple-sink data path through [`MaterializeExperimentalRemoteExecutionSessionTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2892), [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2256), and [`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2414)

But worker execution startup/completion is still bootstrapped through the SQL-callable seam in [`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2597), [`FinishExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2705), and [`worker_tuple_sink_insert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:160).

That is not the final boundary. The missing piece is a **PostgreSQL-local command dispatch bridge**:

- the external service can receive typed peer messages and own transport state
- but it cannot jump directly into PostgreSQL executor code
- PostgreSQL therefore needs its own local dispatcher/executor machinery beneath the new command dispatch/completion plane

## Grounded constraints from the current code

### How Citus currently gets a new worker backend

Current Citus does **not** create worker execution contexts through the PostgreSQL-internal `dsm/shm_mq` worker pattern.

When it needs a new worker-side execution context, it uses the normal PostgreSQL client/backend startup path:

- Citus initiates a nonblocking libpq connection in [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277)
- the underlying `PGconn` is created in [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1353) through `PQconnectStartParams()` at [connection_management.c:1365](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1365)
- PostgreSQL accepts that socket in [`AcceptConnection()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:793)
- the postmaster starts the child through [`BackendStartup()`](/data/dbcomm/postgres-citus/src/backend/postmaster/postmaster.c:3545)
- the child collects the startup packet in [`BackendInitialize()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c:122)
- the child then enters the normal backend loop in [`PostgresMain()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:4411)

So the current “create a worker backend” path is ordinary PostgreSQL connection establishment, not a PostgreSQL-internal worker queue.

### Original Citus COPY semantics are one active placement per worker connection

For the current COPY target, original Citus already serializes one active placement on a worker connection:

- the comment above [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:165) explains the `activePlacementState` and `bufferedPlacementList` model at [multi_copy.c:149](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:149)
- the actual field is [`CopyConnectionState.activePlacementState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:176)
- [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2321) switches or buffers placements at [multi_copy.c:2428](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2428), [multi_copy.c:2442](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2442), and [multi_copy.c:2454](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2454)
- worker COPY startup itself is one command per active placement through [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4616)

So for the current `COPY_INGEST_FROM_TUPLE_SINK` target, one active dispatch per worker execution context is the right first model.

Important correction:

- this is **not** a universal “one command kind per backend forever” rule
- it is only the correct first concurrency assumption for the current worker COPY target

### Current libpq command/result flow is still one protocol stream per connection

Original worker COPY startup still uses:

- [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565)
- followed by [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683)
- in [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4616) at [multi_copy.c:4626](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4626) and [multi_copy.c:4631](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4631)

That fits the `MultiConnection` model itself:

- [`MultiConnection`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:162) is one libpq connection plus one pending-activity stream
- the result-clearing path in [`ClearResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:91) and [`ClearResultsInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:114) is about clearing **pending activity** on that one connection

So the future command plane should assume **one active protocol stream per execution context** first, not one queue per command kind.

### Citus does have many concurrent command/result workflows overall

There is still important concurrency at the system level:

- [`WaitForAllConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:922) exists specifically to wait for many connections with pending commands concurrently
- [`intermediate_results.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:344) sends COPY-start commands to every connection in its `connectionList`, then consumes the corresponding results at [intermediate_results.c:355](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:355)

So the design should stay open for:

- many execution contexts in parallel across the node
- future multiple local sinks or pools if needed

But the currently grounded unit of serialization is still one active command stream per worker connection/backend.

### PostgreSQL background workers are bound to one database/user for their lifetime

Existing code uses:

- [`BackgroundWorkerInitializeConnection()`](/data/dbcomm/postgres-citus/src/backend/postmaster/postmaster.c:4157)
- [`BackgroundWorkerInitializeConnectionByOid()`](/data/dbcomm/postgres-citus/src/backend/postmaster/postmaster.c:4191)

And current Citus/PostgreSQL users of these APIs make that binding explicit:

- [`CitusBackgroundTaskExecutor()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/background_jobs.c:1723) binds to `database/username` at [background_jobs.c:1770](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/background_jobs.c:1770)
- [`ConnectToDatabase()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c:329) binds the maintenance daemon either by database name at [maintenanced.c:347](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c:347) or by `(databaseOid, userOid)` at [maintenanced.c:448](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c:448)
- [`SyncNodeMetadataToNodesMain()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/metadata/metadata_sync.c:3031) also binds by `(databaseOid, extensionOwner)` at [metadata_sync.c:3045](/data/dbcomm/citus-dbcomm/src/backend/distributed/metadata/metadata_sync.c:3045)

This means a long-term dispatcher cannot be modeled as one universal worker that changes identity arbitrarily at runtime. The worker pool must be keyed by execution identity, at minimum `(databaseOid, userOid)`.

### Existing local-dispatch building blocks already exist

The codebase already has the two important local ingredients:

1. **Resident or dynamic background-worker launch**
   - helper wrapper in [`RegisterCitusBackgroundWorker()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/background_worker_utils.c:101)
   - resident per-database daemon trigger in [`InitializeMaintenanceDaemonBackend()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c:217)
   - dynamic worker startup in [`StartCitusBackgroundTaskExecutor()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/background_jobs.c:1649)

2. **Typed shared-memory message passing inside PostgreSQL**
   - logical apply worker setup in [`pa_setup_dsm()`](/data/dbcomm/postgres-citus/src/backend/replication/logical/applyparallelworker.c:327) and launch in [`pa_launch_parallel_worker()`](/data/dbcomm/postgres-citus/src/backend/replication/logical/applyparallelworker.c:404)
   - Citus background-job worker output through [`CitusBackgroundTaskExecutor()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/background_jobs.c:1723), which attaches an `shm_mq` and redirects protocol output at [background_jobs.c:1753](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/background_jobs.c:1753) and [background_jobs.c:1755](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/background_jobs.c:1755)

So the long-term local bridge should be built from these grounded mechanisms, not from SQL UDFs.

## Two local boundaries, not one

An important design correction is that “PostgreSQL-local dispatch” still has **two** distinct local boundaries:

1. **external service -> PostgreSQL local dispatcher**
2. **dispatcher -> executor worker inside PostgreSQL**

These should not be conflated.

Another important correction:

- the external service itself should remain an external process
- it should **not** become a PostgreSQL background worker
- background workers in this note are only candidates for the **PostgreSQL-internal** half of the bridge

## Session-owned sinks are first-class peers

Another design correction from the tuple-sink checkpoint:

- tuple payload sinks and command/completion sinks should be treated as **equal first-class DB-semantics objects** under one compatible `RemoteExecutionSession`
- command/completion must **not** be modeled as state that lives on one exact tuple sink
- their exact reclamation policy is an implementation choice, not a core abstraction boundary

Grounding:

- [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1736) still hard-wires one exact tuple-sink open through [`OpenTupleSinkSessionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1362) and [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1781)
- current close ordering in [`CloseExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3054) closes the sender-side tuple session first at [multi_copy.c:3069](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3069) and only then waits for worker completion at [multi_copy.c:3074](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3074)
- tuple-sink reclamation today is sink-local in [`TupleSinkServiceMaybeReclaimSinkAndSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2095), especially the local-handle / peer-binding gate at [tuple_sink_service_process.c:2105](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2105)

Recommendation:

- keep `RemoteExecutionSession` as the compatible cross-node execution domain
- allow that session to own several independent sinks over time
- for the current COPY target, the first set is:
  - one tuple payload sink
  - one command dispatch sink
  - one command completion sink

The abstraction should only require that the sinks belong to the same compatible session and that ownership is unambiguous. Their reclamation can be eager or lazy later.

## PID must not survive as part of the final sink/session abstraction

The current tuple-sink prototype still bakes sender PID into the exact receive-side rendezvous, but that is bootstrap scaffolding, not the target design.

Grounding:

- the exact tuple-sink identity now intentionally excludes backend PID from open-time rendezvous
- [`BuildTupleSinkKeyFromOperation()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:317) builds that deterministic identity from durable route/transaction fields only
- the temporary worker seam in [`worker_tuple_sink_insert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:173) now receives the PID-free receive contract instead of a backend-derived rendezvous hint

Recommendation:

- keep the existing deterministic exact sink key for open-time rendezvous, but remove backend PID from that key
- after open, attach later consumers by the already-existing service-owned ids such as `serviceSessionId` / `serviceSinkId`, not by reconstructing deterministic sink identity again
- do **not** let command or tuple planes depend on backend PID for correctness

### Boundary 1: external service -> PostgreSQL local dispatcher

The external service is not a postmaster child. That makes it a poor fit for directly depending on PostgreSQL-internal `dsm/shm_mq` allocation as the only handoff mechanism.

The closer grounded precedent is the current backend/service control seam in [`MapRemoteExecutionControlRegion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:717): a stable local shared-memory handoff explicitly designed to cross the process boundary between PostgreSQL backends and the external service.

Recommendation for the long-term command bridge:

- use a **stable local shared-memory command/completion ring** between the external service and PostgreSQL-local dispatcher
- do **not** use SQL/libpq to trigger execution
- do **not** require the external service to participate directly in PostgreSQL dynamic shared memory lifecycles

### Boundary 2: dispatcher -> executor worker inside PostgreSQL

Inside PostgreSQL, `dsm/shm_mq` and background workers are already good building blocks:

- `dsm/shm_mq` are already used for typed intra-Postgres messaging in [`pa_setup_dsm()`](/data/dbcomm/postgres-citus/src/backend/replication/logical/applyparallelworker.c:327)
- Citus already has convenient background-worker registration through [`RegisterCitusBackgroundWorker()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/background_worker_utils.c:101)

Recommendation for the long-term command bridge:

- the PostgreSQL-local dispatcher should hand typed work to executor workers through `dsm/shm_mq` or an equivalent typed shared-memory queue
- executor workers should return typed completion the same way

## Design options

### Option 0: pure relay into an already-existing regular backend

Shape:

- a regular PostgreSQL backend already exists for the target workflow
- that backend blocks on or polls a local command sink
- the external service only relays typed commands into that sink

Pros:

- keeps the external service as a pure external process
- keeps execution in ordinary PostgreSQL backends
- conceptually simple for workflows that already have a backend waiting

Cons:

- not a general replacement for the current worker command start/wait path
- it assumes some other mechanism already created and positioned the backend at the relay point
- once the current worker startup path in [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4616), [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565), and [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683) is removed, that backend no longer appears for free

Assessment:

- valid as a narrow relay model
- insufficient as the general long-term bridge by itself

### Option A: dynamic background worker per dispatch

Shape:

- external service hands one typed dispatch to PostgreSQL
- PostgreSQL launches one dynamic background worker
- the worker binds to `(databaseOid, userOid)` and executes the command
- completion returns when the worker exits or publishes a final result

Grounding:

- [`StartCitusBackgroundTaskExecutor()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/background_jobs.c:1649)
- [`CitusBackgroundTaskExecutor()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/background_jobs.c:1723)

Pros:

- simplest correctness model
- naturally respects database/user binding
- easiest migration away from the current SQL UDF seam

Cons:

- process launch overhead on every command dispatch
- poor long-term fit for hot execution paths
- no amortization for repeated commands on the same `(databaseOid, userOid)`

Assessment:

- good bootstrap fallback
- wrong long-term steady state

### Option B: one resident dispatcher that executes commands itself

Shape:

- one long-lived PostgreSQL worker owns a local command queue
- it receives typed dispatches and executes them directly

Pros:

- very simple control flow
- no per-dispatch process launch

Cons:

- incorrect for mixed `(databaseOid, userOid)` workloads because worker identity is fixed after [`BackgroundWorkerInitializeConnectionByOid()`](/data/dbcomm/postgres-citus/src/backend/postmaster/postmaster.c:4191)
- long-running COPY/query commands would head-of-line block unrelated work
- makes later concurrency domains awkward

Assessment:

- not general enough

### Option C: resident launcher/dispatcher plus executor-worker pools keyed by execution context

Shape:

- a resident PostgreSQL-local launcher/dispatcher receives typed dispatches from the external service
- dispatcher resolves an **execution context class**, at minimum `(databaseOid, userOid, executionClass)`
- dispatcher either reuses an idle executor worker from that pool or launches a new one
- executor worker processes one active command at a time and returns typed completion

Grounding:

- resident per-database daemon pattern in [`InitializeMaintenanceDaemonBackend()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c:217) and [`CitusMaintenanceDaemonMain()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c:461)
- dynamic-worker launch helper in [`RegisterCitusBackgroundWorker()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/background_worker_utils.c:101)
- typed `dsm/shm_mq` intra-Postgres bridge in [`pa_setup_dsm()`](/data/dbcomm/postgres-citus/src/backend/replication/logical/applyparallelworker.c:327)

Pros:

- amortizes worker startup
- respects fixed database/user binding
- keeps one active command per executor worker
- generalizes to more operation families without one queue per command kind

Cons:

- more moving parts now
- requires explicit pool, lifecycle, and failure-state design

Assessment:

- best long-term shape

### Option D: custom direct service -> backend/postmaster execution path

Shape:

- invent a new backend type or postmaster-level direct dispatch facility
- external service triggers execution without an explicit dispatcher/worker-pool layer

Pros:

- potentially lowest overhead in the very long term

Cons:

- most invasive
- least grounded in existing code
- easiest to get lifecycle, transaction, and security semantics wrong

Assessment:

- this is the closest fit to the long-term “no libpq, no worker-local socket bootstrap” goal
- still too large to prototype blindly without a narrower execution-entry design first

Important refinement:

- the external service still should **not** jump directly into executor code
- the real target here is a **PostgreSQL-owned socketless execution-backend launch path** beneath the service bridge
- that launch path is a cold-path control-plane mechanism, not a hot-path relay between service and backend data sinks
- that is different from the rejected worker-local libpq workaround and different from turning the external service into a PostgreSQL background worker

## Recommended long-term shape

### 0. Relationship to `RemoteExecutionSession`

The long-term `RemoteExecutionSession` should remain the **cross-node compatibility and communication abstraction**, not be collapsed blindly into one exact PostgreSQL backend pair.

Grounding:

- [`RemoteExecutionSessionIntentSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:131) is intentionally the reusable compatibility domain
- [`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:111) is the fixed-width compatibility key used by the standalone service

Recommendation:

- keep `RemoteExecutionSession` as the reusable session/channel abstraction between nodes
- let it own several first-class sinks beneath that abstraction
- keep exact local execution contexts beneath that abstraction, selected from a warm pool keyed by compatible execution context
- do **not** make every session identity imply one permanently pinned exact backend pair by default

Why:

- session reuse should stay cheaper than backend pinning
- one compatible session may eventually carry multiple commands and sinks over time
- backend reuse/pooling policy should remain tunable beneath the session abstraction

This still leaves the design space open for a stronger policy later:

- a future session policy may request **eager local execution-context prewarming** when one workload benefits from it
- but that should be a policy under the session abstraction, not the universal meaning of session identity

### 0a. Why warm execution-context pooling is risky if done naively

The main correction is that “session compatibility” is **not** enough to prove a PostgreSQL execution context is safely reusable.

Current Citus connection reuse is already much stricter than that:

- [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:466) rejects connections that are still in a transaction when `OUTSIDE_TRANSACTION` is required, at [connection_management.c:474](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:474)
- it rejects [`claimedExclusively`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:189) connections at [connection_management.c:486](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:486)
- it avoids connections marked `forceCloseAtTransactionEnd` at [connection_management.c:491](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:491)
- it separates metadata use through [`useForMetadataOperations`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:198) at [connection_management.c:513](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:513)

And Citus only keeps a connection cached if [`ShouldShutdownConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1546) says it is safe, meaning:

- no internal-backend restriction
- no forced-close flag
- `PQstatus == CONNECTION_OK`
- [`RemoteTransactionIdle()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1626)
- no replication-origin state
- no exceeded lifetime

Another important correction:

- Citus shutdown is **not** only about one notion of “dirty”
- some shutdown causes are correctness-oriented, such as non-idle transaction state or replication-origin residue
- some are lifecycle or resource policy, such as `MaxCachedConnectionsPerWorker`, maximum lifetime, or the special `IsCitusInternalBackend()` / `IsRebalancerInternalBackend()` rule in [`ShouldShutdownConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1546)

The `IsCitusInternalBackend()` part is easy to misread:

- it does **not** mean ordinary coordinator -> worker connections are never cached
- it means a backend that was itself created to serve an internal Citus connection should not also become a cache owner for more outgoing connections, to avoid connection-count blowup
- the worker-side backend is identified as internal through [`IsCitusInternalBackend()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c:1423), which is driven by the internal application-name prefix [`CITUS_APPLICATION_NAME_PREFIX`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:39) attached in [`GetConnParams()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_configuration.c:258)

The corresponding reset path is also explicit:

- [`ResetRemoteTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1031) clears remote transaction state
- it also calls [`ResetShardPlacementAssociation()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:955) via [remote_transaction.c:1048](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1048)
- replication-origin tracking must be explicitly torn down by [`ResetReplicationOriginRemoteSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/replication_origin_session_utils.c:202)

PostgreSQL itself also makes backend-local statefulness very explicit. Parallel workers are a good grounding example:

- the leader serializes `(database, users, role, temp namespace, snapshots, GUCs, libraries, transaction state)` at [parallel.c:339](/data/dbcomm/postgres-citus/src/backend/access/transam/parallel.c:339), [parallel.c:341](/data/dbcomm/postgres-citus/src/backend/access/transam/parallel.c:341), and [parallel.c:346](/data/dbcomm/postgres-citus/src/backend/access/transam/parallel.c:346)
- the worker then restores that state through [`BackgroundWorkerInitializeConnectionByOid()`](/data/dbcomm/postgres-citus/src/backend/postmaster/postmaster.c:4191), `RestoreGUCState`, `StartParallelWorkerTransaction`, `RestoreTransactionSnapshot`, and `SetTempNamespaceState` at [parallel.c:1428](/data/dbcomm/postgres-citus/src/backend/access/transam/parallel.c:1428) through [parallel.c:1503](/data/dbcomm/postgres-citus/src/backend/access/transam/parallel.c:1503)

So a warm reusable execution-context pool is plausible, but only if reuse is gated by a stronger “execution context compatibility” rule than the current `RemoteExecutionSession` key alone.

The first safe assumption should therefore be:

- execution contexts may be warm and reusable
- but only after they return to a clean idle baseline and still match the required execution-context key
- otherwise the dispatcher should allocate a fresh execution context

### 0b. What warm reuse actually saves, and when the win disappears

The main benefit of reuse is **not** “skip a few small resets.” It is avoiding the whole worker-backend creation path that Citus currently pays through:

- [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277)
- [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1353) and `PQconnectStartParams()` at [connection_management.c:1365](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1365)
- PostgreSQL-side accept/fork/startup in [`AcceptConnection()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:793), [`BackendStartup()`](/data/dbcomm/postgres-citus/src/backend/postmaster/postmaster.c:3545), [`BackendInitialize()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c:122), and [`PostgresMain()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:4411)

Important correction:

- original Citus does **not** necessarily pay that full path on every small transaction
- it already tries to reuse clean remote sessions through its per-backend connection cache
- for example, [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277) first searches the cache through [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:466) at [connection_management.c:356](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:356)
- after transaction end, [`AfterXactConnectionHandling()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:143) calls [`AfterXactHostConnectionHandling()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1474), which either shuts a connection down or resets and unclaims it for reuse through [`ResetRemoteTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1031) at [connection_management.c:1517](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1517)

Another important clarification:

- this is not a separate “backend pool” on the worker node
- the remote PostgreSQL backend stays alive simply because the libpq session/socket stays open
- once started, the worker backend sits in the normal [`PostgresMain()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:4411) command loop and waits for the next command in the `for (;;)` loop at [postgres.c:4728](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:4728), reading from the client via [`ReadCommand()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:514) at [postgres.c:4897](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:4897)
- if Citus decides the session is no longer reusable, it closes the connection, and the corresponding remote backend eventually exits because its client session is gone

So the real baseline comparison for our design is:

- not just “fresh socket + fresh worker every command”
- but also “Citus cached remote session that survived commit and stayed reusable”

So a warm reusable execution context can avoid:

- socket/protocol startup on the worker side
- postmaster fork/child bootstrap
- repeated backend-local initialization and attachment work
- repeated library/GUC/session bootstrap that a fresh execution context would need to reconstruct

That is a real savings opportunity, but there is an important correction to the naive “just reset everything” idea:

- if reuse requires a near-complete session teardown and re-bootstrap every time, the gain collapses toward “fresh worker with more complexity”
- the best win comes only when the reusable execution context is intentionally **sterile** and the allowed mutable session surface is kept narrow enough that reset-to-baseline is much cheaper than fresh startup

So the right comparison is not:

- “fresh worker” vs “arbitrary backend with ad hoc cleanup”

It is:

- **fresh worker every time**
  - safest semantics
  - highest startup overhead
- **controlled internal execution worker with a narrow resettable state surface**
  - likely best long-term tradeoff
- **arbitrary backend reuse with broad session drift**
  - unsafe and hard to reason about

Important implication:

- we should not assume warm reuse is automatically worthwhile
- we should only do it for execution contexts whose reset cost is deliberately engineered to stay far below the fresh backend path
- the actual size of the win needs measurement later; the code today only lets us say *where* the saved work comes from, not *how many microseconds* it will save yet

### 0c. Stronger execution-context compatibility key and reset-to-baseline rule

The current [`RemoteExecutionSessionIntentSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:131) and [`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:111) are the right **channel/session** compatibility boundary, but they are still too weak to prove a PostgreSQL execution context is safely reusable.

The first stronger compatibility key beneath a session should therefore be something like:

- `databaseOid`
- `authenticatedUserOid`
- `executionClass`
- `baselineProfileId`

Where `baselineProfileId` means “this worker is parked at one explicitly supported reusable baseline,” not “whatever session state happened to be left behind.” In the first COPY-ingest target, that profile should stay narrow and boring:

- canonical GUC baseline
- canonical search-path / temp-namespace baseline
- canonical role/security baseline
- no operation-specific placement or replication-origin residue

The compatibility key alone is still **not** enough. Reuse should require both:

1. **key match**
2. **proved reusable idle baseline**

The reusable idle baseline should mean at minimum:

- no active transaction, consistent with [`RemoteTransactionIdle()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1626)
- no exclusive ownership / no command still in progress, analogous to the `claimedExclusively` checks in [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:466)
- no leftover placement association or remote transaction bookkeeping after the explicit reset path in [`ResetRemoteTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1031)
- no leftover replication-origin state after [`ResetReplicationOriginRemoteSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/replication_origin_session_utils.c:202)
- baseline role / GUC / temp-namespace state restored, motivated by the breadth of state PostgreSQL parallel workers must restore in [parallel.c:339](/data/dbcomm/postgres-citus/src/backend/access/transam/parallel.c:339) and [parallel.c:1428](/data/dbcomm/postgres-citus/src/backend/access/transam/parallel.c:1428)

The safest practical rule is therefore not “fully reinitialize arbitrary backends after every command.” That is too close to paying the fresh-start cost anyway. The better rule is:

- design a small class of **controlled internal execution workers**
- constrain what session-level state they are allowed to accumulate
- restore them to one explicit reusable baseline after each command
- retire them instead of reusing them whenever a command escapes that supported baseline

Examples of “escape the baseline, retire the worker” conditions should include:

- unexpected session-level GUC drift that is not part of the profile
- temp-namespace or other session-object state that the pool does not explicitly support
- role/security state not restored to the profile baseline
- command families that intentionally need sticky per-session state outside the supported reusable surface

So the first reuse policy should be conservative:

- reuse only controlled internal execution contexts
- key them by `(databaseOid, authenticatedUserOid, executionClass, baselineProfileId)`
- require explicit reset-to-baseline proof before returning them to the pool
- fall back to fresh creation whenever that proof fails

### 0d. Missing piece: post-command state classification and retire-vs-reuse

This is still a major missing piece in the current prototype design and implementation.

Today, the landed tuple-sink control/data work already reuses the **channel/service** side through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099) and the tuple-sink service/session open path beneath it, but it does **not** yet preserve or reuse a worker execution context beneath that session. The current experimental worker execution is still per-operation scaffolding through [`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2597) and [`FinishExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2705).

So another important correction is:

- current `RemoteExecutionSession` reuse only proves the communication/channel side is still compatible
- it does **not** yet prove a PostgreSQL execution context is reusable
- the missing backend-reuse layer must therefore add an explicit **post-command state classification** step

The design should mirror what Citus already does for `MultiConnection`, but one layer lower:

1. execute command in one worker execution context
2. inspect the post-command state against the supported reusable baseline
3. either:
   - **return-to-pool**
   - or **retire-and-recreate**

That classifier is not optional. Without it, backend reuse is only wishful thinking.

The rationale is exactly the same as the current connection/session distinction:

- `RemoteExecutionSessionIntentSpec` and command dispatch say whether reuse is *eligible*
- observed post-command state says whether reuse is *actually safe*

So this needs to be called out as a first-class implementation target:

- add explicit executor-worker terminal states such as `REUSABLE_IDLE`, `RETIRED_DIRTY`, and `FAILED`
- add the reset-to-baseline verifier that decides between them
- keep the “retire instead of reuse” path cheap and ordinary, not exceptional

Another important boundary:

- the external service can own the **pool policy** and remember which execution contexts are available
- but it cannot by itself prove PostgreSQL-internal reusability
- the PostgreSQL execution side must therefore report terminal classification back to the service

So the retire-vs-reuse decision should be modeled as:

1. backend/executor evaluates its own post-command state against the supported baseline
2. backend/executor reports `REUSABLE_IDLE` vs `RETIRED_DIRTY` vs `FAILED`
3. the external service updates pool ownership and routing policy accordingly

### 0d1. What the current intent/op spec already covers, and what it still misses

The current tuple-sink-facing [`RemoteExecutionSessionIntentSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:131) is already pointing in the right direction. It carries several upper-layer compatibility semantics that mirror the current Citus connection-selection surface:

- target node
- user/database identity
- operation kind / access kind
- transaction policy
- freshness/ownership policy
- placement scope
- transaction-critical bit

That is enough to cover part of the old `MultiConnection` selection space. In particular, it is already trying to abstract the same kind of questions Citus asks in [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277), [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:466), and placement-sensitive connection choice in [`GetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:239).

But another important correction is:

- the current session intent/op spec is **not yet the whole future execution-context contract**

For the broader command/backend-reuse design, it is still missing or only partially covering several semantics the upper layers may need:

- explicit execution context class / baseline profile for safe backend reuse
- replication-style execution class analogous to `REQUIRE_REPLICATION_CONNECTION_PARAM`
- metadata-affinity style semantics analogous to `REQUIRE_METADATA_CONNECTION`
- some “clean enough” constraints that are inherently **dynamic**, such as the placement-history-sensitive behavior in [`GetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:239) when `REQUIRE_CLEAN_CONNECTION` is involved at [placement_connection.c:314](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:314)

So the design rule should be:

- keep static compatibility semantics in the intent/op spec
- keep observed history/baseline safety in the post-command classifier
- do not try to force all dynamic “clean enough” logic into a static session key

### 0d2. Near-term simplification: defer only *additional* backend pooling

An important correction to the earlier simplification:

- we should defer **additional sterile backend pooling / reset-to-baseline reuse**
- but we should still preserve the ordinary Citus-style session/backend reuse semantics that already exist today

Why this distinction matters:

- in original Citus, keeping the libpq session alive also keeps the remote PostgreSQL backend alive in [`PostgresMain()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:4411)
- so if our remote execution session is intended to replace that connection abstraction without changing upper-layer behavior, then a reusable session still implies a reusable live worker execution context in the ordinary Citus sense

So the near-term target should be:

- preserve Citus-equivalent session/backend reuse when the upper-layer semantics say reuse is allowed
- preserve Citus-equivalent “open a fresh one” behavior when the upper-layer semantics say a reused one is not acceptable
- defer only the stronger future idea of *sterile worker pools with explicit reset-to-baseline reuse across a wider set of scenarios*

This means the current control-plane design still needs enough state to uphold the upper-layer reuse semantics that Citus already depends on, even though we are not yet adding a new deeper backend-pool policy of our own.

### 0d3. Which upper-layer semantics should be static key fields vs service-side history

Another important correction is that not every Citus connection-selection behavior belongs in the same bucket.

For the near-term no-backend-reuse design, the useful split is:

1. **Static intent/op-spec semantics**
   These should travel in the request because they describe what the upper layer is asking for:
   - node / user / database identity
   - op kind and access kind
   - transaction policy
   - freshness / ownership policy
   - placement scope
   - transaction-critical bit
   - command-family-specific target identity such as shard / placement / relation ids

2. **Service-side externally derivable history**
   These can be tracked by the external service if that same service is the sole owner of routing and lifetime:
   - whether a session/channel currently has an in-flight command
   - whether a session/channel is pinned exclusively
   - metadata-affinity ownership/promotion, if we model `REQUIRE_METADATA_CONNECTION` the same way current Citus does
   - lightweight per-session scheduling/backpressure state

3. **Backend-private execution state**
   This cannot be safely inferred by the service without explicit backend reporting:
   - PostgreSQL session/GUC/temp-namespace/role state
   - execution-context “returned to baseline” safety
   - any future backend-reuse classifier outcome

This suggests two practical conclusions.

First, some Citus behaviors that look like “history” really do remain relevant as long as we preserve ordinary Citus-style session/backend reuse. The best example is the placement-history-sensitive `REQUIRE_CLEAN_CONNECTION` logic in [`GetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:239), especially the branch at [placement_connection.c:314](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:314). That matters precisely because a **reused PostgreSQL session/backend** may already have touched another placement.

So another important correction is:

- `REQUIRE_CLEAN_CONNECTION` is not something we can dismiss just because we are deferring sterile backend pooling
- if we want to preserve Citus semantics, the request still needs a static “requires clean” meaning, and the implementation still needs enough session history to honor it

The good news is that the current intent surface already has the right direction for the static part: [`RemoteExecFreshnessPolicy`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:56) already includes `REMOTE_EXEC_FRESHNESS_REQUIRE_CLEAN`. What is still missing is the corresponding reusable-session history state in the control plane/service.

For the current COPY/tuple-oriented target, that history is plausibly externalizable because the typed operation specs already identify placement-level targets such as shard/placement/relation in [`RemoteExecTupleSinkOperationSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:89). So for this target, the external service can potentially maintain enough placement-access history to emulate the old Citus decision.

For future generic SQL-command targets, that may not be true unless the command dispatch object also carries richer planner/executor-visible access metadata rather than only opaque SQL text.

Second, some Citus flags are better viewed as scheduler/resource policy than remote-execution semantics. Examples are `OPTIONAL_CONNECTION` and `WAIT_FOR_CONNECTION` described in the Citus README at [README.md:2031](/data/dbcomm/citus-dbcomm/src/backend/distributed/README.md:2031). Those influence connection expansion under pressure, but they do not look like core semantic requirements of one remote execution session. They should stay local scheduler policy, not become part of the canonical compatibility key unless later evidence says otherwise.

### 0d4. Is it worthwhile to offload the classifier to the external service?

For the near-term design, probably not.

If we are not adding **extra sterile backend pooling**, then the remaining per-command “classifier” work is mostly:

- did the command succeed or fail
- is the session/channel still healthy
- is the session/channel still available for another command according to static ownership/in-flight rules

For the Citus-equivalent session/backend reuse case, the external service can likely own more than that, but only for state it can actually derive or is explicitly told:

- in-flight / claimed status
- freshness / force-fresh / require-clean semantics
- placement-access history for typed command families where the service sees the relevant placement metadata
- metadata-affinity ownership if we model that explicitly

The external service does **not** automatically know PostgreSQL-private state, so it still cannot by itself decide anything that depends on hidden backend-local state.

But the stronger backend-state classifier is a different story. Even if we later revisit backend reuse, offloading the whole retire-vs-reuse decision to the external service is only attractive if the relevant state is already externalized. Today it is not. The backend still owns the decisive facts about PostgreSQL-internal state. Mirroring enough of that state into the service just to save a tiny amount of backend-side control logic is unlikely to pay off, especially because this decision happens once per command on the control path, not on the hot tuple data path.

So the practical recommendation is:

- let the external service own routing/pool policy for channel/session objects and externally derivable reuse history
- let the backend report command completion/failure plus any explicit “do not reuse this execution context” disposition that is not externally derivable
- defer any deeper backend-state reuse classifier beyond ordinary Citus-equivalent reuse until there is a strong measured reason to add it

### 0d5. Current implementation checkpoint in the tuple-sink control plane

The current tuple-sink control plane now carries the first concrete pieces of this design, but only for the ordinary Citus-equivalent session/backend reuse surface, not for deeper sterile backend pooling:

- the reusable service-session identity is now intentionally narrow, much closer to current Citus's connection cache key: destination node, user, database, and replication-vs-default lane in [`TupleSinkServiceSessionMatchesBaseCompatibility()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1009)
- metadata affinity is now modeled as request-time policy instead of a separate compatibility lane, via [`RemoteExecMetadataPolicy`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:80), [`BuildControlSessionKeyFromIntent()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:371), and metadata-aware selection in [`TupleSinkServiceFindReusableSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1463)
- the current COPY-target `REQUIRE_CLEAN_CONNECTION` rule is now externalized into the service-owned session state in [`TupleSinkServiceSessionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:81), with placement-access history tracked by [`TupleSinkServiceRecordSessionPlacementHistory()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:939)
- that history now participates in the actual reuse gate through [`TupleSinkServiceSessionAllowsReuse()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1092), [`TupleSinkServiceFindReusableSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1463), [`TupleSinkServiceHandlePeerOpenRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2282), and [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3135)
- the tuple-sink op spec now carries the same placement-routing metadata Citus uses in [`ConnectionAccessedDifferentPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:794): [`partitionMethod`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:119), [`colocationGroupId`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:120), and [`representativeValue`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:121)

The other implemented piece is the future-facing backend-report hook:

- [`RemoteExecPostCommandState`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:188) and [`ReportRemoteExecutionSessionPostCommandState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1348) now exist as backend-facing API
- the local control protocol has a fixed-width [`CitusRemoteExecReportPostCommandStateRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:246)
- the service records that state through [`TupleSinkServiceApplyReportedPostCommandState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1421) and [`TupleSinkServiceHandleReportPostCommandState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3415)

Important current boundary:

- this API is intentionally present before it is required by the tuple-sink prototype
- the current COPY-target reuse decision is still primarily service-derived
- deeper backend-private retire/reuse classification remains deferred until command dispatch/completion replaces the temporary worker command path

### 0e. What this design can realistically improve over current Citus

For the normal case, current Citus already sets a strong baseline:

- it can reuse a cached remote session/worker backend when the libpq connection survives commit, through [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277), [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:466), and [`AfterXactHostConnectionHandling()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1474)

So the honest research question is **not** “can we beat an obviously bad baseline?” The baseline is already decent.

The realistic improvement opportunities are narrower:

- replace SQL/libpq command start/wait with typed fixed-width command dispatch/completion instead of [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565) and [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683)
- replace COPY byte payload with the tuple payload plane, which the current prototype already started doing under [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2256) and [`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2414)
- fuse cold-path session creation with first-command dispatch when no compatible session exists yet
- decouple channel lifetime from execution-context lifetime so a dirty worker can be retired without also tearing down the underlying peer transport every time
- let the external service perform node-local scheduling/backpressure with a global view across many PostgreSQL execution contexts

Important correction:

- if we do **not** implement reusable execution contexts beneath the session layer, then our gains over current Citus hot-path reuse may be modest
- in that case we mainly win on transport/protocol shape, command startup/response shape, and traffic-class separation
- the bigger lifecycle win only appears if controlled execution-context reuse is actually made safe and cheap

So the near-term research posture should stay pragmatic:

- treat channel/session reuse as already valuable and already landed
- treat backend reuse as a separate major design target, not an assumed free benefit
- measure whether controlled execution-context reuse is worth its complexity before making it central to the prototype

### 1. Stable local service <-> PostgreSQL dispatch rings

Add a new local shared-memory command/completion region, conceptually parallel to the current backend/service control region:

- one inbound dispatch ring from external service to PostgreSQL-local dispatcher
- one outbound completion ring back to the external service
- typed fixed-width messages
- responder-visible publication semantics consistent with the RDMA notes, but this boundary is local shared memory rather than RDMA

### 2. One resident launcher/dispatcher per database

The closest grounded long-lived worker pattern is the maintenance daemon:

- [`InitializeMaintenanceDaemonBackend()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c:217) starts or reuses a per-database daemon
- [`ConnectToDatabase()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c:329) handles the per-database lifetime bookkeeping

For the command bridge, the dispatcher should be resident per database, not per command kind.

Why per database first:

- database binding is mandatory
- it is a natural place to maintain worker pools keyed by user and execution class
- it avoids one giant cluster-wide dispatcher process with mixed database concerns

### 3. Executor pools keyed by `(databaseOid, userOid, executionClass)`

This is the key generalization point.

Recommended first key:

- `databaseOid`
- `userOid`
- `executionClass`

Where `executionClass` is **not** command kind. It is a concurrency/scheduling domain such as:

- `USER_EXECUTION`
  - COPY ingest from tuple sink
  - later distributed query execution that produces rows or other payload
- `MAINTENANCE_EXECUTION`
  - metadata/admin/background-task style work

Important decision:

- do **not** split pools/queues by command kind first
- split only when the execution identity or concurrency domain is materially different

### 4. One active command per executor worker

Each executor worker should still process one active command at a time.

Why:

- it matches the current COPY target’s `activePlacementState` model
- it keeps transaction/error semantics easier to debug
- it avoids reintroducing libpq-style multiplexing problems before the command plane is stable

If later workloads need true concurrent in-flight commands under the same `(databaseOid, userOid, executionClass)`, add more executor workers to that pool first rather than making one worker drain several independent command streams simultaneously.

### 5. Typed dispatch/completion handlers inside PostgreSQL

The dispatcher should decode `operationKind` and route to a handler table.

For the first target:

- `COPY_INGEST_FROM_TUPLE_SINK`

The executor handler then:

- opens/attaches the local receive-side session through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099)
- drains batches through [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1290)
- copies tuple views via [`CopyOutRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1363)
- reports `STARTED`, `COMPLETED`, or `FAILED` back through the local completion ring

### 6. Fusing session open with the first command

For reducing round trips, the first typed command can be fused with session establishment on the slow path, but it should be treated as an optimization, not the core semantic split.

Two approaches are reasonable:

1. **Strict split**
   - open or reuse `RemoteExecutionSession`
   - then dispatch the first command

2. **Optimistic fused open+dispatch on new session creation**
   - if no compatible session exists locally, send one combined “open session + first command” request
   - if a compatible session already exists, dispatch normally on the already-open session

Recommendation:

- semantically keep the split between session/channel establishment and command dispatch
- wire-encode a fused “open + first command” fast path when the session is being created anyway

Why:

- it keeps reuse semantics and error reporting cleaner
- it preserves the idea that one compatible session may serve many later commands
- it still allows the first command to piggyback on the initial open when that actually saves an RTT

## Do we really need separate queues/sinks in other scenarios?

Not by command kind alone.

The codebase investigation points to a narrower rule:

- **No**, we do not currently need a separate queue/sink for every command family
- **Yes**, we will likely need separate queues or worker pools when execution identity or concurrency domain differs materially

### Cases that do NOT justify separate queues yet

- `COPY_INGEST_FROM_TUPLE_SINK` vs future query-execution command kinds under the same `(databaseOid, userOid, USER_EXECUTION)` domain
- different typed operations that all run as one active command per executor worker

In these cases, one typed dispatch queue and one typed completion queue per execution context are enough.

### Cases that likely DO justify separation later

1. **Different identity domains**
   - a worker bound to one `(databaseOid, userOid)` cannot safely act as a universal executor for all users/databases

2. **Different execution classes**
   - long-running user execution should not head-of-line block maintenance/admin tasks
   - the existing maintenance daemon pattern in [`CitusMaintenanceDaemonMain()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c:461) already shows that Citus treats maintenance as a separate local execution domain

3. **Different payload/result coupling**
   - some future command families may need a separate result plane or a separate data sink
   - that still does not require a separate dispatch queue **unless** the local execution domain also differs

So the design rule to note down is:

- **separate local queues/pools by execution identity or concurrency domain, not by operation kind**

## What this means for the current COPY prototype

The current COPY prototype is now past the old SQL/libpq bootstrap seam, but only for the narrow `COPY_INGEST_FROM_TUPLE_SINK` target.

Current implementation checkpoint:

- the backend-facing command helpers are now real entry points in [`StartRemoteExecutionCommandThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1795), [`PollRemoteExecutionCommandCompletionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1931), [`StartRemoteExecutionCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2168), and [`WaitForRemoteExecutionCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2252)
- the local service loop now handles `START_COMMAND` / `POLL_COMMAND_COMPLETION` in [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5151), forwarding to [`TupleSinkServiceHandleStartCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4655), [`TupleSinkServiceHandlePollCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4796), [`TupleSinkServiceHandlePeerStartCommandRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4906), and [`TupleSinkServiceHandlePeerPollCommandCompletionRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5054)
- the PostgreSQL-side cold-path spawn bridge is now active for the experimental COPY target in [`CitusInstallRemoteExecutionBackendHooks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:912), [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:582), [`StartRemoteExecBackend()`](/data/dbcomm/postgres-citus/src/backend/postmaster/postmaster.c:3727), and [`RemoteExecBackendMain()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c:156)
- the socketless backend now owns a session-scoped command loop across typed `TX_BEGIN_ATTACH`, `COPY_INGEST_FROM_TUPLE_SINK`, and the later terminal `TX_PREPARE` / `TX_COMMIT` / `TX_ABORT` commands in [`RemoteExecBackendExecuteTxBeginAttachCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:396), [`RemoteExecBackendExecuteTxPrepareCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:465), [`RemoteExecBackendExecuteTxCommitCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:502), and [`RemoteExecBackendExecuteTxAbortCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:535)
- the backend-facing command-state enum now includes an explicit started state in [`RemoteExecCommandState`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:204), mirroring the fixed-width control value [`CITUS_REMOTE_EXEC_COMMAND_STATE_STARTED`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:110)
- the shared worker receive/insert body remains [`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:201); the SQL UDF [`worker_tuple_sink_insert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:394) is now the legacy compatibility shell rather than the active experimental COPY start/wait mechanism

Important implementation correction:

- “removing SQL/libpq” does **not** mean the new backend should keep replaying the old worker SQL strings internally
- the current connection path uses SQL text because the coordinator is talking to a remote backend over libpq, for example [`BeginTransactionCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:365) and [`AssignDistributedTransactionIdCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:407)
- once we are already inside the worker backend through the socketless path, we should prefer backend-local helpers that perform the same state transitions directly
- the existing SQL UDF [`assign_distributed_transaction_id()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c:145) is a good grounding example: the SQL callable entry point is just the transport-facing shell around backend-local shared-memory mutation of the worker transaction identity

So the transaction command family should aim to replace:

- transport of SQL text over libpq

with:

- typed command payload over the command/completion plane
- backend-local helper calls inside the socketless worker backend

That is the right way to preserve Citus semantics while still removing SQL UDF / libpq coupling from the worker start and transaction-control path.

So the first real replacement has now landed for the experimental COPY target:

- a typed command/completion protocol with transaction lifecycle verbs plus `COPY_INGEST_FROM_TUPLE_SINK`
- delivered into one session-scoped local command/completion mailbox pair defined in [`CitusRemoteExecLocalCommandMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:111) and [`CitusRemoteExecLocalCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:122)
- routed to one PostgreSQL-owned socketless execution context in code and active for the experimental COPY target
- with one active command per execution context

The design target for that execution context should now be sharpened:

- **implemented current checkpoint**: one fresh socketless backend per session-owned worker transaction lifecycle, created by postmaster and attached directly to the local command/completion mailboxes; this now covers `TX_BEGIN_ATTACH`, the long-lived COPY work command, and the later terminal transaction commands for the experimental COPY target
- **long-term target**: keep the same socketless normal-backend-style execution context, but add session/backend keep-alive and attach-to-existing-backend reuse where Citus-equivalent semantics justify it
- **rejected for this target**: dynamic background workers as the execution context

That means the design direction is now grounded in real code and is already active for the experimental COPY path. The worker-attach parity gap is now closed through the shared raw replay helper [`BuildCurrentTransactionBeginReplayText()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:1137), the typed serializer [`InitRemoteTransactionBeginAttachCommandSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:870), and the socketless worker-side replayer [`RemoteExecBackendReplayTxBeginAttachText()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:297). The remaining caveat is the fixed-width control-path cap in [`CITUS_REMOTE_EXEC_TX_BEGIN_ATTACH_REPLAY_BYTES`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:37), not a missing replay feature.

## Pitfalls and non-obvious boundaries to preserve

- The current implementation is **transaction-scoped**, not a general reusable execution-context cache. [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2018) currently allocates the coordinator-side session object in `TopTransactionContext`, and terminal worker commands in [`RemoteExecBackendExecuteTxCommitCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:504) and [`RemoteExecBackendExecuteTxAbortCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:537) explicitly retire the socketless backend. So this bridge currently preserves one worker transaction lifecycle, not Citus-style cross-transaction connection caching.
- The external service should stay a pure external process, but there is still one unavoidable PostgreSQL-owned cold-path boundary before a backend exists. The hot path is still direct service-owned shared memory to backend-owned shared memory; the only “middle” step is the cold-path spawn queue through [`TupleSinkServiceSubmitBackendSpawnRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1166), [`CitusRemoteExecPostmasterSigusr1Hook()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:768), and [`StartRemoteExecBackend()`](/data/dbcomm/postgres-citus/src/backend/postmaster/postmaster.c:3727). There is no extra relay in the hot command/tuple data path once the backend exists.
- `SIGUSR1` is only the postmaster wakeup mechanism, not the spawn mechanism itself. The actual child creation still happens through PostgreSQL postmaster child launch in [`StartRemoteExecBackend()`](/data/dbcomm/postgres-citus/src/backend/postmaster/postmaster.c:3727), and the child then enters the socketless shell [`RemoteExecBackendMain()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c:156).
- “Remove libpq/sockets” is currently true only for the experimental COPY worker-start path, not the whole system. The experimental path bypasses [`CopyGetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4482) via [`GetExperimentalSessionOnlyConnectionState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4152) in [`InitializeCopyShardState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4273), but ordinary PostgreSQL client/backend startup still exists elsewhere through [`AcceptConnection()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:793), [`BackendStartup()`](/data/dbcomm/postgres-citus/src/backend/postmaster/postmaster.c:3547), and [`BackendInitialize()`](/data/dbcomm/postgres-citus/src/backend/tcop/backend_startup.c:232).
- The socketless backend still has to use PostgreSQL’s lower-level transaction APIs because it bypasses `PostgresMain()`’s frontend protocol loop. The semantic source of truth is Citus’s old worker-connection behavior, but the implementation mechanism is now PostgreSQL backend calls: per-command wrappers in [`StartTransactionCommand()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:2995) and [`CommitTransactionCommand()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:3093), plus lifecycle transitions such as [`BeginTransactionBlock()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:3873), [`PrepareTransactionBlock()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:3941), [`EndTransactionBlock()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:3993), [`UserAbortTransactionBlock()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:4153), and [`FinishPreparedTransaction()`](/data/dbcomm/postgres-citus/src/backend/access/transam/twophase.c:1487).
- “Use backend-local helpers instead of SQL text” is already the right direction and has one concrete grounding example: the new transaction attach path calls [`SetCurrentDistributedTransactionId()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c:171) directly, while the old SQL UDF [`assign_distributed_transaction_id()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c:145) remains only as the compatibility shell for the legacy libpq path.
- The current bridge does not yet prove that backend/session reuse is cheap or safe. If later work wants cross-transaction keep-alive, the ownership model around [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2018) and the backend exit policy in [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:582) must change; `TopTransactionContext` and “exit on terminal command” are coherent only for the current scoped design.

## Recommended next concrete design steps

1. Generalize `RemoteExecutionSession` from “tuple sink only” to “session plus first-class sinks” more explicitly in the API surface, even though the first implementation already treats command/completion as session-scoped mailbox state.
2. Decide whether the next step is a local attach-by-service-id fast path or continued typed op-spec replay when the worker backend opens the receive-side tuple sink.
3. Keep the shared raw replay path in place until there is a measured reason to replace it with a more compact typed subxact / GUC representation.
4. Treat distributed locking as a later typed command family for wider command-plane replacement, not as part of the first COPY worker-transaction cut.
5. Keep the current fresh-backend-per-session-owned worker transaction lifecycle policy until there is a measured reason to preserve backend lifetime beneath the session layer across transactions.
6. When backend keep-alive is revisited, add an explicit attach-to-existing-backend path plus post-command retire/reuse classification instead of assuming the current fresh-worker implementation is enough.
7. Keep “one active command per executor worker” as the first invariant, and revisit multi-command workers only after the first end-to-end command plane is stable.

## Related

- [command_dispatch_completion_plane.md](command_dispatch_completion_plane.md): canonical command-plane design note that motivates this PostgreSQL-local bridge.
- [tuple_sink_data_plane_completion_plan.md](tuple_sink_data_plane_completion_plan.md): current tuple-sink data-plane progress and remaining integration gaps.
- [rdma_transport_control_plane_abstraction.md](../transport/rdma_transport_control_plane_abstraction.md): peer transport and traffic-class split beneath the local PostgreSQL bridge.
