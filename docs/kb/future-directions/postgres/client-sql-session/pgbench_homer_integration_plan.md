# Pgbench Homer Integration Plan

## Scope

- **What this doc explains**: the plan for integrating the persistent Homer client SQL session path into upstream `pgbench` while preserving existing libpq-backed pgbench modes for baseline comparison.
- **What this doc does NOT cover**: prepared-mode Homer commands, concurrent Homer clients, frontend authentication, cancellation, or basebackup/interference runs. Sink-backed tuple result materialization has now landed for the single-client simple-mode pgbench path and is summarized below.
- **Primary directory**: `docs/kb/future-directions/postgres/client-sql-session/`
- **Doc type**: `future-direction`

## Goal

Add an explicit opt-in Homer transport mode to `src/bin/pgbench/pgbench.c` for the already-working single-client persistent-session workload.

The important compatibility rule is: **do not replace or weaken existing pgbench modes**. Existing libpq-backed simple, extended, prepared, pipeline, initialization, and meta-command functionality must remain available unchanged so we can compare:

- stock pgbench simple mode over libpq/TCP or Unix socket
- stock pgbench reconnect mode if needed as a sanity baseline
- Homer simple mode over the external service with one persistent client SQL session

The first Homer integration should be a narrow measured-path mode, not a general pgbench protocol replacement.

## Implementation Status

This first measured-path integration is now implemented in the `separate-comm-stack` Postgres worktree:

- [`homer_mode`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:278) is the opt-in global transport switch. Normal pgbench still uses libpq unless `--homer` is present.
- [`CState.homer_session`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:609) and [`TState.homer_control`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:698) add persistent Homer session/control state beside the original `PGconn` state.
- [`HomerRunCommandAndWait()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3418) adapts pgbench's synchronous simple-query command execution to Homer command start/poll plus result-sink opening/draining when needed.
- [`sendHomerCommand()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3573) maps exact `BEGIN`, `COMMIT` / `END`, and `ROLLBACK` SQL text to typed Homer lifecycle commands, while sending ordinary statements as `CITUS_REMOTE_EXEC_COMMAND_SQL_EXECUTE`.
- [`sendCommand()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3739) branches to the Homer adapter only when `homer_mode` is set; the existing `PQsendQuery()`, `PQsendQueryParams()`, and `PQsendQueryPrepared()` branches remain the non-Homer behavior.
- The pgbench state machine skips libpq socket/result waiting after a completed Homer command at [`CSTATE_START_COMMAND`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:4138), but still uses pgbench's transaction timing/statistics.
- The end-of-transaction check uses [`homer_transaction_attached`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:4829) in Homer mode instead of calling `PQtransactionStatus()` on a nonexistent `PGconn`.
- [`threadRun()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:8215) opens one thread-local Homer control mapping and one persistent client SQL session before measured work.
- [`finishHomerSession()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:8557) aborts any still-attached transaction defensively and closes the Homer session before the control mapping is unmapped.
- [`printResults()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:7107) prints `transport: homer` or `transport: libpq` so result logs identify the path used.
- [`--client-cpu`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1039) pins the pgbench worker thread for comparable Homer/libpq runs.
- [`--latency-percentiles`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1041) reports exact p50/p95/p99/max. [`reserveLatencySamples()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1539) preallocates the sample vector before `-t` benchmark timing.

The build currently links the Homer frontend client library into pgbench:

- [`src/bin/pgbench/Makefile`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/Makefile:10) adds overridable farnet defaults for the installed Homer include directory and `libhomer_client.a`.
- [`src/bin/pgbench/meson.build`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/meson.build:31) adds the same include/link inputs for the Meson build used in this worktree.

The compatibility rule is preserved by validation:

- [`--homer`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:7767), [`--homer-database-oid`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:7771), and [`--homer-user-oid`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:7776) are explicit opt-in options.
- [`validateHomerScriptSupport()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3682) rejects `\gset`, `\aset`, pipeline meta-commands, and scripts without explicit transaction lifecycle commands.
- Option validation rejects non-simple query mode, reconnect mode, multi-client/multi-thread mode, retry mode, and missing Homer OID options at [`pgbench.c:7949`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:7949).

## Grounding in Current Pgbench

Current client state is libpq-centered:

- [`CState.con`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:598) stores the per-client `PGconn *`.
- [`threadRun()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:7431) opens persistent libpq connections before the benchmark loop when `--connect` is not used.
- The connection setup loop calls [`doConnect()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:7481).

Current SQL dispatch is split by pgbench query mode:

- simple mode calls [`PQsendQuery()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3167).
- extended mode calls [`PQsendQueryParams()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3178).
- prepared mode calls [`PQsendQueryPrepared()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3189).

Current result handling is also libpq-centered:

- [`readCommandResponse()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3241) drains `PGresult` objects with [`PQgetResult()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3255).
- `\gset` and `\aset` depend on tuple values from `PGresult` around [`pgbench.c:3278`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3278).
- The main event loop in [`threadRun()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:7507) builds a socket wait set and uses libpq readiness helpers such as [`PQconsumeInput()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3978), [`PQisBusy()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3978), and later [`PQgetResult()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:4127).

The current Homer frontend library has the needed single-session API:

- [`HomerClientOpenControl()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:56)
- [`HomerClientOpenSqlSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:61)
- [`HomerClientStartCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:70)
- [`HomerClientPollCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:78)
- [`HomerClientStartAndWaitCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:84)
- [`HomerClientCloseSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:66)

The standalone proof point is [`homer_pgbench.c`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_pgbench.c:1). Its transaction loop is not enough by itself because it bypasses pgbench's real script machinery, reporting paths, option parsing, and baseline comparison harness.

## Compatibility Rules

Homer integration must be opt-in. A flag such as `--homer` should choose the Homer transport path.

Existing pgbench behavior should remain unchanged unless `--homer` is present:

- no changes to default `PGconn` connection setup
- no changes to libpq simple mode
- no changes to extended or prepared modes
- no changes to pipeline mode
- no changes to initialization mode
- no changes to ordinary `\gset`, `\aset`, and other meta-command semantics outside Homer mode

The first Homer mode should reject unsupported combinations clearly instead of partially emulating them:

- reject initialization mode with `--homer`
- reject `--connect` / reconnect-per-transaction mode with `--homer`
- reject `QUERY_EXTENDED`, `QUERY_PREPARED`, and pipeline mode with `--homer`
- reject `\gset` and `\aset` in Homer mode until received tuple values are converted into pgbench variables; sink-backed tuple delivery itself is now implemented
- initially reject more than one client/thread unless the current run is explicitly intended to test the known unsupported concurrent-client behavior

This keeps the comparison clean: the same pgbench binary can run stock modes and the Homer mode, but the modes do not share transport implementation by accident.

## Original Integration Work Items

This section preserves the design work items that guided the pgbench integration. Most items here are now complete for the single-client simple-mode milestone; the current status is summarized in the implementation-status section above and in the status lines under the implementation plan.

### Pgbench State

`CState` needs Homer state beside `PGconn`:

```c
HomerClientSession homerSession;
bool homerSessionOpen;
```

`TState` should probably own one `HomerClientControl` mapping per thread:

```c
HomerClientControl homerControl;
bool homerControlOpen;
```

For the first single-client patch, a per-client control mapping would work, but per-thread control ownership is closer to the later multi-client shape and avoids redundant shared-memory mappings.

### Options

Add explicit options, initially:

```text
--homer
--homer-database-oid=OID
--homer-user-oid=OID
```

The OID options are a prototype bridge to the current [`HomerClientSessionOptions`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:41). Later work should resolve database/user names through normal auth/session setup, but that is not part of the first integration.

### Build and Link

`src/bin/pgbench` needs to include the installed Homer frontend header:

```c
#include "distributed/homer/remote_execution_client.h"
```

The pgbench build also needs to link `libhomer_client.a` from the installed Citus/Homer tree. Keep this conditional or narrow so the normal pgbench build path is not made fragile for non-Homer builds.

### Connection Lifecycle

In [`threadRun()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:7431):

- normal mode keeps the current [`doConnect()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:7481) path
- Homer mode opens one `HomerClientControl` per thread before the benchmark starts
- Homer mode opens one `HomerClientSession` per client before the benchmark starts
- Homer mode closes each session at client shutdown and closes the thread control mapping at thread shutdown

For the first milestone, preserve persistent-session behavior only. Do not implement Homer reconnect-per-transaction mode yet.

### SQL Dispatch

Add a Homer branch near the simple SQL send path:

- assign variables exactly as simple mode currently does before [`PQsendQuery()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3167)
- submit the substituted SQL string through `HomerClientStartAndWaitCommand(..., CITUS_REMOTE_EXEC_COMMAND_SQL_EXECUTE, ...)`
- use `CITUS_REMOTE_EXEC_SQL_RESULT_NONE` for the first milestone, because row payload materialization is not available yet

The first implementation can use synchronous start-and-wait. That fits the current one-command-at-a-time Homer API and avoids pretending we have a socket/fd that pgbench can wait on. It means the Homer path will not use the same libpq socket readiness state machine, but it can still reuse pgbench's script scheduling and transaction-latency accounting.

### Transaction Lifecycle Commands

There are two possible first implementations:

1. **Pattern-based default-script integration**: intercept the default pgbench transaction shape and map `BEGIN` / `COMMIT` to typed `CLIENT_SQL_TX_BEGIN` / `TX_COMMIT`, while sending the five body statements as `SQL_EXECUTE`.
2. **Script-command integration**: extend pgbench command execution so a Homer mode can treat SQL transaction boundaries as typed lifecycle commands when the command text is exactly `BEGIN`, `COMMIT`, or `ROLLBACK`.

Prefer the second approach if it stays small, because it keeps pgbench's script machinery in charge. It is still acceptable to start with the default transaction script only if that gives a faster correctness checkpoint.

### Result Handling

For first integration, Homer result handling is intentionally narrow but no longer resultless:

- command completion state must be `COMPLETED`
- processed row count should be available for debugging and optional assertions
- row-producing simple SQL opens and drains a Homer tuple-result sink
- `\gset` and `\aset` still error in Homer mode because pgbench variable materialization from tuple values is not implemented

This preserves correctness boundaries. Faking tuple results or fabricating `PGresult` objects would blur the missing sink-backed result work and make later performance comparisons misleading.

### Result Materialization Status

The pgbench-facing result materialization step has landed for the single-client simple-mode workload. It is not a `PGresult` compatibility layer; it is generic tuple-result materialization through Homer result sinks:

- The socketless backend uses sink-backed `DestReceiver` callbacks rooted at [`RemoteExecSqlDestStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:612), [`RemoteExecSqlDestReceiveSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:774), and [`RemoteExecSqlDestShutdown()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:829).
- The executor `TupleDesc` becomes a tuple-view contract at receiver startup.
- The frontend opens and drains the result sink through `HomerClientOpenResultSink()` / `HomerClientDrainResultSink()` from the frontend Homer client library.
- Pgbench currently drains and discards the default `SELECT abalance` row; variable assignment for `\gset` / `\aset` remains future work.

The result sink should be discoverable before command completion for row-producing commands. If the frontend calls a pure `StartAndWaitCommand()` and cannot learn or drain the sink until final completion, a large result can fill the finite sink and stall the backend before it can publish completion. That is an API sequencing issue, not a flaw in sink backpressure itself. The safe shape is:

```text
START SQL command
POLL until result sink ready or command terminal
if result sink ready: map/drain tuple batches
POLL final command completion / EOS
```

For the current pgbench TPC-B workload, `SELECT abalance` is one row, so this sequencing issue is not practically limiting. It still matters for the abstraction because the result path should work for arbitrary tuple result sizes without changing the public API later.

## Implementation Plan

1. Add pgbench options and validation.
   - Add `--homer`, `--homer-database-oid`, and `--homer-user-oid`.
   - Reject unsupported modes early with clear messages.
   - **Status**: complete for the first single-client milestone.

2. Add Homer state to pgbench client/thread state.
   - Extend `CState` beside [`CState.con`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:598).
   - Add per-thread control mapping state to `TState`.
   - **Status**: complete.

3. Add Homer open/close helpers.
   - `openHomerThreadControl(TState *)`
   - `openHomerClientSession(TState *, CState *)`
   - `closeHomerClientSession(CState *)`
   - `closeHomerThreadControl(TState *)`
   - **Status**: complete, implemented as `threadRun()` control open/close plus `openHomerSession()` / `finishHomerSession()`.

4. Wire connection setup in [`threadRun()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:7431).
   - Preserve the existing `doConnect()` branch for normal mode.
   - Add a Homer branch around the current connection setup loop at [`pgbench.c:7476`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:7476).
   - **Status**: complete.

5. Add Homer command execution helpers.
   - `sendHomerSimpleSql(CState *, Command *)`
   - `runHomerLifecycleCommand(CState *, uint32 commandKind, const char *name)`
   - optional `readHomerCommandResponse(...)` if the state machine needs a separate result phase.
   - **Status**: complete as `sendHomerCommand()` and `HomerRunCommandAndWait()`.

6. Wire the simple SQL send path.
   - Branch before [`PQsendQuery()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3167).
   - Keep existing libpq code unchanged in the non-Homer branch.
   - **Status**: complete.

7. Preserve stats and logging.
   - Reuse pgbench's existing transaction start/end timing in the state machine.
   - Do not switch to the standalone `homer_pgbench` timing code.
   - Record failures through pgbench's existing `estatus` / transaction failure path where possible.
   - **Status**: complete for successful runs and non-retry Homer failures. Retry semantics remain intentionally rejected.

8. Verify against the standalone runner.
   - `pgbench --homer -n -c 1 -j 1 -t 1 --homer-database-oid=5 --homer-user-oid=10 postgres`
   - repeat with `-t 10`, `-t 1000`, and `-t 5000`
   - compare `pgbench_history` deltas
   - confirm service logs show one `opened command session`, one backend spawn, many command sequences, and one `client_sql_session_close`
   - confirm no leftover remote-exec backend after close
   - **Status**: complete for 2-, 1000-, and 5000-transaction runs. See implementation checkpoint for exact numbers.

9. Baseline comparison discipline.
   - Run stock pgbench from the same binary without `--homer`.
   - Use the same scale, transaction count, pinning, and table state when comparing.
   - Keep both stock persistent libpq and Homer persistent-session results in notes.
   - **Status**: complete for the current single-client benchmark. The latest repeated pinned runs and p50/p95/p99 reporting are captured in the implementation checkpoint.

## Deferred Work

- service-owned result sink allocation/binding instead of backend-created local POSIX shm result queues
- richer frontend tuple-value accessors and pgbench variable materialization
- `\gset` / `\aset` in Homer mode
- prepared-mode typed commands
- extended-query typed commands
- pipeline mode
- cancellation
- frontend auth/name resolution instead of OID flags
- concurrent clients and session/core placement policy
- basebackup/network-interference benchmark integration

## Related

- [`client_sql_session_offload_plan.md`](client_sql_session_offload_plan.md): target abstraction and current implementation status.
- [`../../../implementations/postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md`](../../../implementations/postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md): current persistent-session standalone checkpoint.
