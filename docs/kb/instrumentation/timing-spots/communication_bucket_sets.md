# Communication bucket sets

## Scope

This note defines the recommended timing-spot bucket sets for communication-stack analysis after the newer leaf-oriented instrumentation landed in:

- [`PQgetResult()`](/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c#L2075)
- [`PQputCopyData()`](/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c#L2793)
- [`PQputCopyEnd()`](/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c#L2868)
- [`pqGetCopyData3()`](/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-protocol3.c#L1748)
- [`secure_read()`](/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c#L145)
- [`secure_write()`](/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c#L306)
- [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3704)
- [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3996)

The main reporting rule is:

- pick **one level** for a given workflow
- do **not** add a coarse parent together with the lower children it encloses
- keep [`ReceiveResults_BuildTuples`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4229) in a separate **result materialization** bucket
- keep file spots such as [`FetchIntermediate_FileWrite`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1123), [`ReceiveAndWriteCopyData_WriteFile`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L75), and [`SendViaCopy_FileRead`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L134) in a separate **file I/O** bucket

`PG_WAIT` / `PG_WAIT_DONT_COUNT` are no longer required for the main
communication-stack estimate.

The summary scripts now use the additive leaf buckets directly:

- `transport_protocol_cpu_ns`
- `row_serde_cpu_ns`
- `result_materialization_cpu_ns`
- `file_comm_io_ns`
- optional `optional_connection_setup_cpu_ns`

The primary denominator is now:

- `query_active_wall_ns` from `QUERY_ACTIVE_WALL`

and the optional excluded-wait companion is:

- `query_wait_wall_ns` from `QUERY_WAIT_WALL`

Legacy context timers still exposed by the scripts:

- `query_exec_core_ns` from [`ExecSimpleQuery`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:5064)
- `distributed_tx_lifecycle_wall_ns` from [`XACT_PROCESSING`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:88)

Use those legacy timers only for context or comparison against older runs, not
as the primary denominator.

## Two reporting modes

### 1) Scenario-complete buckets

Use this mode for the spreadsheet if the goal is “account for the whole communication-side execution of this workflow” with minimal interpretation.

This mode intentionally uses some coarse parents, but only when you **do not** add their lower children.

### 2) Low-level transport leaves

Use this mode only when the goal is to compare transport internals across workflows, for example socket-read vs socket-write vs libpq parse.

This mode is more normalized, but it may leave some higher-level envelope/control work outside the chosen leaf set unless you add the corresponding higher-level deserialize/control spots explicitly.

## Worker query results streamed to coordinator

Code path:

- readiness/control in [`CheckConnectionReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3704)
- buffered result extraction in [`PQgetResult()`](/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c#L2075)
- Citus-side row extraction in [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3996)
- local tuple construction in [`ReceiveResults_BuildTuples`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4229)

Recommended scenario-complete communication buckets:

- transport/protocol/control: `CheckConnectionReady_ + PQ_getResult + ReceiveResults_Deserialize`
- result materialization: `ReceiveResults_BuildTuples`

Do not add:

- `PG_FE_SOCK_READ`
- `PG_FE_SOCK_WRITE`
- `PG_parseInput`
- `ReceiveResults_`
- `ReceiveResults_Net`

when using the scenario-complete bucket above, because [`CheckConnectionReady_`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3722) already encloses the post-wakeup libpq progression work.

Low-level alternative:

- `PG_FE_SOCK_READ + PG_FE_SOCK_WRITE + PG_parseInput + PQ_getResult + ReceiveResults_Deserialize`
- keep `ReceiveResults_BuildTuples` separate

## Worker/coordinator sending query results to the SQL client

Code path:

- row formatting in [`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L306)
- backend message staging in [`socket_putmessage()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c#L1488)
- active socket send in [`secure_write()`](/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c#L306)

Recommended scenario-complete communication buckets:

- row serialization: `Printtup_Ser`
- transport/protocol send: `Printtup_Net`

Low-level alternative:

- row serialization: `Printtup_Ser`
- transport/protocol send: `PQ_putmessage + PG_BE_SOCK_WRITE`

Do not add `Printtup_Net` together with `PQ_putmessage` / `PG_BE_SOCK_WRITE`.

## Push intermediate results: producer side

Code path:

- per-row serialization/send in [`RemoteFileDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L405)
- lower COPY message staging in [`PQputCopyData()`](/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c#L2793)
- final COPY close in [`PutRemoteCopyEnd()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L782)

Recommended scenario-complete communication buckets:

- row serialization: `RemoteFileDestReceiver_Ser`
- row transport/protocol send: `RemoteFileDestReceiver_Send`
- shutdown/control send: `PQ_putCopyEnd`
- optional file bucket on the producer: `RemoteFileDestReceiver_SerAndSend_WriteLocal`

Low-level alternative:

- `PQ_putCopyData + PQ_putCopyEnd + PG_FE_SOCK_WRITE + PG_FE_SOCK_READ + PG_parseInput`
- keep `RemoteFileDestReceiver_Ser` separate as the row-serialization bucket

Do not add `RemoteFileDestReceiver_Send` together with `PQ_putCopyData` / `PG_FE_*` if you want a non-overlapping per-row producer total.

## Push intermediate results: worker receive side

Code path:

- raw backend receive in [`ReceiveCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L298)
- outer loop in [`RedirectCopyDataToRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L44)

Recommended scenario-complete communication buckets:

- receive/deser/copy envelope: `ReceiveAndWriteCopyData_Deser`
- file bucket: `ReceiveAndWriteCopyData_WriteFile`

Low-level alternative:

- `PQ_getbytes + PG_BE_SOCK_READ`
- file bucket: `ReceiveAndWriteCopyData_WriteFile`

Use the low-level alternative only if you explicitly want backend receive-buffer draining split from backend socket-read execution. Otherwise `ReceiveAndWriteCopyData_Deser` is the simpler scenario-complete receive bucket.

## Pull intermediate results: consumer side

Code path:

- command send in [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L563)
- COPY chunk receive in [`CopyDataFromConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1100)
- file write in [`FetchRemoteIntermediateResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L998)

Recommended scenario-complete communication buckets:

- command/control setup: `PQ_sendCommand + PQ_getResult`
- chunk receive/copy envelope: `FetchIntermediate_CopyAndWrite`
- file bucket: `FetchIntermediate_FileWrite`

Low-level alternative:

- command/control setup: `PQ_sendCommand + PQ_getResult`
- data transfer: `PG_FE_SOCK_READ + PQ_getCopyData`
- file bucket: `FetchIntermediate_FileWrite`

Do not add `FetchIntermediate_CopyAndWrite` together with `PQ_getCopyData` / `PG_FE_SOCK_READ` / `FetchIntermediate_FileWrite`.

## Pull intermediate results: source-side COPY sender

Code path:

- file send loop in [`SendRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L104)

Recommended scenario-complete communication buckets:

- transport/protocol send: `SendViaCopy_Send`
- file bucket: `SendViaCopy_FileRead`

Low-level alternative:

- transport/protocol send: `PQ_putmessage + PG_BE_SOCK_WRITE`
- file bucket: `SendViaCopy_FileRead`

`SendViaCopy_` stays as a coarse end-to-end wrapper, not an additive bucket.

## Off-path local-table distribution into shard placements

Code path:

- tuple routing/send in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2274)
- lower libpq send helpers in [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L563) and [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L742)

Recommended scenario-complete communication buckets:

- row serialization: `SerializeAndCopyRow_`
- row/control send: `SendCopyDataToPlacement_`
- local-path/file-adjacent bucket: `WriteTupleToLocal_ + DoLocalCopy_`

Low-level alternative:

- `PQ_sendCommand + PQ_putCopyData + PQ_putCopyEnd + PG_FE_SOCK_WRITE + PG_FE_SOCK_READ + PG_parseInput`
- keep `SerializeAndCopyRow_` separate

`CitusSendTupleToPlacements_` stays as a coarse per-tuple wrapper and should not be added into the additive sum when the child buckets above are used.

## Connection setup

Code path:

- synchronous setup in [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1361)
- asynchronous poll progression in [`MultiConnectionStatePoll()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L809) and [`ConnectionStateMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L2990)

Recommended setup bucket:

- `PQ_connectStart + PQ_connectPoll`

Treat this as a separate setup/control bucket rather than mixing it into steady-state transport totals.

## Bottom line

If the goal is the spreadsheet-friendly “communication-stack execution during query/distributed-transaction execution”, prefer the scenario-complete bucket sets above and keep:

- result materialization separate
- file I/O separate
- wait separate or ignored

Only drop down to the lower `PG_FE_*`, `PG_BE_*`, and `PQ_*` leaf sets when you specifically want transport internals rather than the full workflow-level communication bucket.

Current script interpretation rule:

- `communication_stack_core_total_ns` is the recommended strict additive communication total.
- `communication_stack_extended_total_ns` adds the separate result-materialization and file-backed communication buckets.
- `communication_stack_extended_with_optional_setup_total_ns` additionally includes the optional connection-setup bucket.
- `nonadditive_debug_total_ns` is diagnostic only and must never be added into any additive total.
- `query_active_wall_ns` is the denominator used for communication percentages.
- `query_wait_wall_ns` is the matching excluded wait total and is optional/debug.
- `query_exec_core_ns` and `distributed_tx_lifecycle_wall_ns` remain legacy context
  columns only.
