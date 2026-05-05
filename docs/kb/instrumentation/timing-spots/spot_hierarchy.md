# Timing spot hierarchy (coverage / nesting)

This document shows **common include/nesting relationships** between timing spots as they appear in the *instrumented code paths*.

Notes:

- “Includes” here means **explicit nesting** (a timer starts, then another timer starts before the first ends).
- A “coarse” timer can also conceptually “cover” smaller timers across function calls (e.g., a transaction-level timer that spans many functions). Those are called out, but the primary focus is explicit nesting.
- Line numbers are for the current repo snapshot.

## PostgreSQL backend socket IO

- `PG_BE_SOCK_READ` (`/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:182`)
  - no longer explicitly nests `PG_WAIT`; the timer is paused around the blocking wait so it measures only active recv/copy work
- `PG_BE_SOCK_WRITE` (`/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:319`)
  - no longer explicitly nests `PG_WAIT`; the timer is paused around the blocking wait so it measures only active send/copy work

## PostgreSQL query active wall vs wait buckets

- `QUERY_ACTIVE_WALL` is started by `BeginQueryTimingCycle()` in
  `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:714`
- `QUERY_ACTIVE_WALL` is stopped by `FinishQueryTimingCycle()` after the shared
  `ReadyForQuery(...)` call in
  `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:4954`
- `QUERY_WAIT_WALL` is not manually started at each wait site; instead it is
  driven centrally by:
  - `pgstat_report_wait_start()` / `pgstat_report_wait_end()` in
    `/data/dbcomm/postgres-citus/src/include/utils/wait_event.h:87` and `:111`
  - explicit FE-libpq wait hooks in `PQsocketPoll()` at
    `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:1222`,
    `:1252`, `:1270`, and `:1313`

With this shape:

- `PG_WAIT` and `PG_FE_WAIT` remain local wait-location timers
- `QUERY_WAIT_WALL` is the top-level excluded-wait total
- `PG_WAIT_DONT_COUNT` is legacy-only and no longer participates in the active
  denominator model

## PostgreSQL COPY

- `CopyTo_` (`/data/dbcomm/postgres-citus/src/backend/commands/copy.c:324`)
  - `CopyTo_GetTuples` (`/data/dbcomm/postgres-citus/src/backend/commands/copyto.c:873`)
  - `CopyTo_CopyOneRowTo` (`/data/dbcomm/postgres-citus/src/backend/commands/copyto.c:886`)
  - `CopyTo_fwrite` (only for server-side file destinations; `/data/dbcomm/postgres-citus/src/backend/commands/copyto.c:208`)
- `Receiver_CopyFrom` (`/data/dbcomm/postgres-citus/src/backend/commands/copy.c:307`)
  - `NextCopyFrom_` (`/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:997`)
    - `CopyFrom_CopyGetData` (`/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c:252`)
  - `CopyFromInsertIntoTable` (`/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:1187`)

## PostgreSQL result tuple send (DestReceiver)

- `Printtup` (`/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:317`)
  - `Printtup_Ser` (`/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:323`)
  - `Printtup_Net` (`/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:395`)
    - `PQ_putmessage` (`/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1502`)

`PG_BE_SOCK_WRITE` can still be reached during a forced flush on this path, but `PQ_putmessage` is paused around that lower transport work so the two leaves are additive rather than nested.

## Frontend libpq query/result/COPY leaves

- Remote command / query submission:
  - `PQ_sendCommand` wraps message assembly in `PQsendQueryInternal()`, `PQsendPrepare()`, `PQsendQueryGuts()`, and `PQsendTypedCommand()` (`/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:1458`, `:1586`, `:1806`, `:2678`)
  - `PG_FE_SOCK_WRITE` is reached separately when `pqFlush()` actually pushes bytes
- Result receive:
  - `PQ_getResult` wraps buffered-result/control work in `PQgetResult()` (`/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2075`)
  - `PG_FE_WAIT`, `PG_FE_SOCK_READ`, `PG_FE_SOCK_WRITE`, and `PG_parseInput` are siblings because `PQ_getResult` is paused around `pqWait()`, `pqReadData()`, `pqFlush()`, and `parseInput()`
- COPY send/receive:
  - `PQ_putCopyData` wraps COPY data message staging in `PQputCopyData()` (`/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2810`)
  - `PQ_putCopyEnd` wraps COPY-end message staging in `PQputCopyEnd()` (`/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2877`)
  - `PQ_getCopyData` wraps buffered COPY-row extraction in `pqGetCopyData3()` (`/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-protocol3.c:1758`)
  - lower wait/read/write remain in `PG_FE_WAIT`, `PG_FE_SOCK_READ`, and `PG_FE_SOCK_WRITE`

## Backend raw receive helper

- `PQ_getbyte` (`/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:963`)
  - `PG_BE_SOCK_READ` can refill `PqRecvBuffer` underneath through `pq_recvbuf()`
  - `PQ_getbyte` is paused around that refill, so it acts as the type-byte framing leaf rather than a parent of socket read
- `PQ_getmessage` (`/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1214`)
  - pauses around nested `pq_getbytes()` calls
  - captures receive-side protocol framing and destination `StringInfo` management
- `PQ_getbytes` (`/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1064`)
  - `PG_BE_SOCK_READ` can still refill `PqRecvBuffer` underneath through `pq_recvbuf()`
  - `PQ_getbytes` is paused around that refill, so it acts as the receive-buffer draining/copy leaf rather than a parent of socket read

## Citus off-path bulk movement (distribute table local data into shards)

This is the “COPY local table into shards” off-path workflow:

- `CreateCitusTable_` (wraps `CreateCitusTable()`; `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1114`)
  - `CopyFromLocalTableIntoDistTable_` (wraps `CopyFromLocalTableIntoDistTable()`; `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2696`)
    - `DoCopyFromLocalTableIntoShards_` (scan loop; `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2792`)
      - `WriteTupleToLocal_` (optional, when local placement exists; `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2335`)
      - `CitusSendTupleToPlacements_` (per tuple; `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2358`)
        - `SerializeAndCopyRow_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2419`)
        - `SendCopyDataToPlacement_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2379`)
    - `WriteTupleToLocal_` (also used during shutdown to finish colocated intermediate files; `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2683`)
    - `DoLocalCopy_` (final flush of remaining buffered local tuples; `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2692`)

## Citus intermediate results: pull-based (worker ↔ worker)

Pull path is driven by the UDF `fetch_intermediate_results()`:

- `FetchIntermediate_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:926`)
  - `FetchIntermediate_CopyAndWrite` (per “COPY chunk” attempt; `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1051`)
    - `FetchIntermediate_FileWrite` (per `FileWriteCompat()` append; `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1123`)
  - `PG_WAIT` (wait for socket readability between chunks; `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1070`)

The send side of pull uses file transfer over COPY:

- `SendViaCopy_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:118`)
  - `SendViaCopy_FileRead` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:134`)
  - `SendViaCopy_Send` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:144`)

## Citus intermediate results: push-based (coordinator broadcast)

Coordinator produces intermediate results into a DestReceiver:

- `ExecutePlanIntoColocatedIntermediateResults_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/insert_select_executor.c:347`)
  - `ExecutePlanIntoDestReceiver_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/insert_select_executor.c:374`)
    - (DestReceiver work depends on receiver; see RemoteFileDestReceiver below)

DestReceiver (coordinator side) broadcasts COPY bytes to worker nodes:

- `RemoteFileDestReceiver_SerAndSend` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:409`)
  - `RemoteFileDestReceiver_Init` (setup/connection establishment; `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:264`, `:299`)
  - `RemoteFileDestReceiver_Ser` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:431`)
  - `RemoteFileDestReceiver_Send` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:450`)
    - `PQ_putCopyData` (via `PutRemoteCopyData()` / `PQputCopyData()`; `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2810`)
  - `RemoteFileDestReceiver_SerAndSend_WriteLocal` (optional; `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:461`)

Worker-side receive path writes the intermediate file:

- `ReceiveAndWriteCopyData_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:55`)
  - `ReceiveAndWriteCopyData_Deser` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:65`, `:90`)
  - `ReceiveAndWriteCopyData_WriteFile` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:75`)

## Citus portal/result streaming (worker -> coordinator)

- `CheckConnectionReady_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3716`)
  - `PG_FE_SOCK_WRITE` (inside `PQflush`; `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:861`)
  - `PG_FE_SOCK_READ` (inside `PQconsumeInput`; `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:598`)
  - `PG_parseInput` (inside `PQisBusy`; `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2026`)
- `ReceiveResults_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4009`)
  - `ReceiveResults_Net` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4042`)
    - `PQ_getResult` (`/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2075`)
  - `ReceiveResults_Deserialize` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4037`)
  - `ReceiveResults_BuildTuples` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4206`)
    - `ReceiveResults_HeapFormTuple` (materialization; `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tuples.c:120` and `/data/dbcomm/postgres-citus/src/backend/executor/execTuples.c:2266`)

`ReceiveResults_BuildTuples` is the start of the separate result-materialization bucket; do not add it into the strict transport/protocol leaf sum.
Because `SendNextQuery()` enables `PQsetSingleRowMode()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3998`), this deserialize/materialize split is currently aligned to one streamed worker row per `PGRES_SINGLE_TUPLE`.

## Connection setup

- `PQ_connectStart` is emitted in `StartConnectionEstablishment()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1370`) for the synchronous start half of connection setup
- `PQ_connectPoll` is emitted at the Citus `PQconnectPoll()` call sites:
  - `MultiConnectionStatePoll()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:836`)
  - adaptive-executor `ConnectionStateMachine()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3054`)

## Distributed transaction timing (coarse)

`XACT_PROCESSING` is designed to cover a full distributed transaction:

- `XACT_PROCESSING` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:88`)
  - `XACT_TS_CoordinatorPrepare` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:347`)
  - `XACT_TS_SendRemoteCommand` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:530`)
  - `XACT_TS_GetRemoteCommandResult` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:689`)
    - `XACT_TS_WaitForConnections` (when `GetRemoteCommandResult` enters `FinishConnectionIO`) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:704`, `:825`)
      - `PG_WAIT` (socket readiness wait) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:876`)
        - `XACT_WAIT` (distributed transaction wait subset) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:877`)
  - `XACT_TS_WaitForConnections` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:926`)
    - `PG_WAIT` (socket readiness wait inside `WaitForAllConnections`) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:993`)
      - `XACT_WAIT` (distributed transaction wait subset) (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:994`)
  - `XACT_TS_coordinated_commit_abort` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1066`)

Separately (backend protocol ACK), often near the end of a command:

- `XACT_TS_EndCommand` (`/data/dbcomm/postgres-citus/src/backend/tcop/dest.c:175`)
