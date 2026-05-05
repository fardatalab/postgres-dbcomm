# Distributed transaction timing (`XACT_*`)

## Scope

This doc maps distributed transaction lifecycle and remote command execution to the `XACT_*` timing spots defined in:

- `/data/dbcomm/postgres-citus/src/include/timing_spots.h:121`

The key implementation files in Citus are:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c` (coordinator transaction lifecycle + identity tagging)
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c` (send remote commands + wait for results)
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c` (asynchronous connection establishment)
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c` (coordinated commit/abort paths)

## Timer hierarchy (include / coverage relationship)

Primary distributed-transaction hierarchy:

- `XACT_PROCESSING` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:88`)
  - `XACT_TS_CoordinatorPrepare` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:347`)
  - `XACT_TS_SendRemoteCommand` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:530`, `:570`)
  - `XACT_TS_GetRemoteCommandResult` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:689`)
    - `XACT_TS_WaitForConnections` (when `GetRemoteCommandResult(...)` needs `FinishConnectionIO(...)`) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:704`, `:825`)
      - `PG_WAIT` (socket readiness wait in `WaitLatchOrSocket`) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:876`)
        - `XACT_WAIT` (distributed-transaction wait subset) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:877`)
  - `XACT_TS_WaitForConnections` (multi-connection wait loops) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:926`)
    - `PG_WAIT` (socket readiness wait in `WaitEventSetWait`) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:993`)
      - `XACT_WAIT` (distributed-transaction wait subset) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:994`)
  - `XACT_TS_coordinated_commit_abort` (prepare/commit/abort/savepoint remote phases) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1066`, `:1142`, `:1213`, `:1276`, `:1334`, `:1387`)

Related but not strictly nested in every transaction sample:

- `XACT_TS_EndCommand` in Postgres `EndCommand()` (`/data/dbcomm/postgres-citus/src/backend/tcop/dest.c:175`) may occur while a distributed transaction is active, but it is a protocol ACK timer, not a Citus remote-transaction subphase timer.

## Coordinator/worker interaction flow (where timers fire)

1. Coordinator enters coordinated transaction:
   - `UseCoordinatedTransaction()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:341`) starts `XACT_TS_CoordinatorPrepare` and activates `XACT_PROCESSING`.
2. Coordinator sends commands to workers:
   - `SendRemoteCommand*()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:525`, `:565`) uses `XACT_TS_SendRemoteCommand`.
3. Coordinator waits for IO readiness and results:
   - `GetRemoteCommandResult()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683`) uses `XACT_TS_GetRemoteCommandResult`.
   - If busy, `FinishConnectionIO()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:822`) contributes `XACT_TS_WaitForConnections`, `PG_WAIT`, and nested `XACT_WAIT`.
   - Batched waits use `WaitForAllConnections()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:923`) with the same wait timers.
4. Coordinator performs distributed commit/abort/savepoint phases:
   - `CoordinatedRemoteTransactions*` functions in `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c` use `XACT_TS_coordinated_commit_abort`.
5. Backend protocol completion:
   - `EndCommand()` (`/data/dbcomm/postgres-citus/src/backend/tcop/dest.c:168`) uses `XACT_TS_EndCommand` for `CommandComplete` message send.

## Whole-transaction timer: `XACT_PROCESSING`

Transaction-level timing is started and ended by helper functions in:

- `StartDistributedTransactionTimingIfNeeded()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:79`)
  - Calls `timing_start(XACT_PROCESSING)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:88`
  - Also calls `logger_set_distributed_xact_state(true)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:89`
- `EndDistributedTransactionTimingIfNeeded()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:158`)
  - Calls `timing_end(XACT_PROCESSING)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:167`

Why this is a lifecycle timer, not a pure execution denominator:

- `StartDistributedTransactionTimingIfNeeded()` also calls [`logger_set_distributed_xact_state(true)`](/data/dbcomm/postgres-citus/src/common/time_instr.c:155) at [`transaction_management.c:89`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:89).
- While that flag is true, [`logger_print_timings()`](/data/dbcomm/postgres-citus/src/common/time_instr.c:491) suppresses transactional spots from per-command prints at [`time_instr.c:531`](/data/dbcomm/postgres-citus/src/common/time_instr.c:531) and [`time_instr.c:574`](/data/dbcomm/postgres-citus/src/common/time_instr.c:574), then [`logger_reset()`](/data/dbcomm/postgres-citus/src/common/time_instr.c:705) preserves those transactional spots instead of clearing them at [`time_instr.c:712`](/data/dbcomm/postgres-citus/src/common/time_instr.c:712).
- `EndDistributedTransactionTimingIfNeeded()` is only called later from [`CoordinatedTransactionCallback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:486) on commit/abort/prepare paths at [`transaction_management.c:576`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:576), [`transaction_management.c:679`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:679), and [`transaction_management.c:714`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:714).

Consequence:

- `XACT_PROCESSING` can span multiple statements and the gaps between them once the distributed timing starts.
- It is a good distributed-transaction **lifecycle wall-time** context timer, but not an exact "active execution only" denominator.
- The summary scripts therefore expose this as `distributed_tx_lifecycle_wall_ns`
  only as legacy context, not as the primary denominator.
- The active-execution denominator for transaction summaries is now the sum of
  per-statement `QUERY_ACTIVE_WALL` blocks aggregated by
  `aggregate_transaction_timing.py`.

## Coordinator “prepare” / transaction start: `XACT_TS_CoordinatorPrepare`

Coordinated transaction setup is timed in:

- `UseCoordinatedTransaction()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:341`)
  - `timing_start(XACT_TS_CoordinatorPrepare)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:347`
  - `timing_end(XACT_TS_CoordinatorPrepare)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:351` (early return) and at `:386` (normal path)

This function also sets the timing report identity used for attribution:

- `SetLoggerDistributedTransactionIdentity(...)` call site at `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:380`

## Sending remote commands: `XACT_TS_SendRemoteCommand`

Remote command send wrappers time the `PQsendQuery*` calls:

- `SendRemoteCommandParams(...)`:
  - start/end at `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:530` … `:551`
- `SendRemoteCommand(...)`:
  - start/end at `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:570` … `:588`

## Waiting for results: `XACT_TS_GetRemoteCommandResult` + `XACT_TS_WaitForConnections` + `XACT_WAIT`

### `XACT_TS_GetRemoteCommandResult`

`GetRemoteCommandResult(...)` wraps the “finish IO if busy, then `PQgetResult`” path:

- start/end at `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:689` … `:725`

This timing spot is intentionally broad: it can include “waiting for busy connection” via `FinishConnectionIO(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:704`–`:716`) depending on state.

### `XACT_TS_WaitForConnections` (command ACK readiness vs connection establishment)

This timing spot is used in two distinct contexts:

1) **Asynchronous connection establishment**:
   - `FinishConnectionListEstablishment(...)` wraps the overall establish loop with `timing_start(XACT_TS_WaitForConnections)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:976`) and `timing_end(...)` (`:1182`).
   - The blocking wait inside that loop is timed as `PG_WAIT` around `WaitEventSetWait(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1051` … `:1056`).

2) **Waiting for remote command completion across a set of connections**:
   - `WaitForAllConnections(...)` path in `remote_commands.c` uses `XACT_TS_WaitForConnections` with multiple early returns:
     - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:825` … `:912`
     - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:926` … `:1166`

Because of this dual-use, “`XACT_TS_WaitForConnections`” is not exclusively “worker ACK time”; it also includes connection establishment in some flows.

### `XACT_WAIT`

`XACT_WAIT` is used as a narrower “sleep/wait” component within the broader wait logic in `remote_commands.c`:

- start/end at `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:877` … `:882`
- start/end at `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:994` … `:1000`

`XACT_WAIT` is now nested under `PG_WAIT` at those same call sites, so distributed-transaction blocking wait remains identifiable while still rolling up into the global socket-wait timer.

## Coordinated commit/abort: `XACT_TS_coordinated_commit_abort`

This spot wraps multiple coordinated transaction phases:

- `CoordinatedRemoteTransactionsPrepare()` start/end at `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1066` … `:1126`
- `CoordinatedRemoteTransactionsCommit()` start/end at `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1142` … `:1198`
- `CoordinatedRemoteTransactionsAbort()` start/end at `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1213` … (multiple segments further down the file)

## Related Postgres-side timer: `XACT_TS_EndCommand`

The backend protocol “CommandComplete” send timing is implemented in the Postgres tree:

- `EndCommand()` in `/data/dbcomm/postgres-citus/src/backend/tcop/dest.c:168` wraps `pq_putmessage(PqMsg_CommandComplete, ...)` with `XACT_TS_EndCommand` (`/data/dbcomm/postgres-citus/src/backend/tcop/dest.c:175` … `:201`).
