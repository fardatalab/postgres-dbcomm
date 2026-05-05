# In-query tuple movement (push): coordinator broadcasts intermediate results via COPY “format result”

## Scope

This doc covers the **push/broadcast** data movement mode where the coordinator streams an intermediate result set to one or more workers.

Key idea:

- Tuples are produced during query execution and must be moved while the query is “on the critical path”.
- Citus uses a custom intermediate-results protocol built on PostgreSQL’s COPY framing (`WITH (format result)`).

Primary timers (overview):

- Coordinator-side broadcast DestReceiver:
  - `RemoteFileDestReceiver_Init`
  - `RemoteFileDestReceiver_SerAndSend` → `RemoteFileDestReceiver_Ser` + `RemoteFileDestReceiver_Send` (+ optional `RemoteFileDestReceiver_SerAndSend_WriteLocal`)
- Worker-side receive-to-file:
  - `ProcessCopyStmt_` (dispatch wrapper)
  - `ReceiveAndWriteCopyData_` → `ReceiveAndWriteCopyData_Deser` + `ReceiveAndWriteCopyData_WriteFile`

## Coordinator-side workflow (broadcast)

### 1) Coordinator chooses a DestReceiver that targets intermediate results

The broadcast implementation is in:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c`

The receiver object is created by:

- `CreateRemoteFileDestReceiver(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:239`

### 2) Setup/init: COPY state + connections (`RemoteFileDestReceiver_Init`)

`RemoteFileDestReceiver_Init` spans multiple phases/call sites:

- `RemoteFileDestReceiverStartup(...)` starts at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:264` and ends at `:286`:
  - sets up `CopyOutState` fields, `fe_msgbuf`, and output functions (`ColumnOutputFunctions(...)`).
- `PrepareIntermediateResultBroadcast(...)` starts at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:299` and ends at `:379`:
  - establishes connections (`StartNodeConnection`, `FinishConnectionListEstablishment`)
  - begins remote transactions (`RemoteTransactionsBeginIfNecessary`)
  - sends the COPY IN statement to each worker:
    - statement text is produced by `ConstructCopyResultStatement(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:388`
    - `SendRemoteCommand(...)` is called at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:344`
    - ACK is checked via `GetRemoteCommandResult(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:361`

The protocol command that workers receive is:

- `COPY "<resultId>" FROM STDIN WITH (format result)`

### 3) Per-tuple hot path: serialize + broadcast (`RemoteFileDestReceiver_SerAndSend`)

Each tuple goes through `RemoteFileDestReceiverReceive(...)`:

- Defined at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:404`
- Timed by:
  - `RemoteFileDestReceiver_SerAndSend` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:409` … `:475`)
  - Verbose split:
    - `RemoteFileDestReceiver_Ser` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:431` … `:447`)
    - `RemoteFileDestReceiver_Send` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:450` … `:455`)
  - Optional local persistence:
    - `RemoteFileDestReceiver_SerAndSend_WriteLocal` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:461` … `:463`)

Important nuance (first tuple):

- On the first tuple only (`resultDest->tuplesSent == 0`), `RemoteFileDestReceiverReceive(...)` calls `PrepareIntermediateResultBroadcast(...)` (at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:419`), so `RemoteFileDestReceiver_SerAndSend` can include some of the init cost for the first tuple.

## Worker-side workflow (receive stream -> intermediate file)

### 1) COPY hook dispatch (`ProcessCopyStmt`)

Workers receive the COPY statement over the normal Postgres protocol.

Citus intercepts it in:

- `ProcessCopyStmt(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2988`
- Timed by `ProcessCopyStmt_` (start at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2993`)

When `IsCopyResultStmt(copyStatement)` is true (format == “result”):

- Worker calls `ReceiveQueryResultViaCopy(resultId)` (call site `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3005`)
- Implementation is `ReceiveQueryResultViaCopy(...)` in `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:623`:
  - creates the intermediate-results directory (`CreateIntermediateResultsDirectory`)
  - calls `RedirectCopyDataToRegularFile(resultFileName)`

### 2) Receive & write COPY bytes (`RedirectCopyDataToRegularFile`)

The receive-to-file helper is:

- `RedirectCopyDataToRegularFile(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:51`

Timing breakdown:

- `ReceiveAndWriteCopyData_` wraps the full receive loop (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:55` … `:101`)
- `ReceiveAndWriteCopyData_Deser` wraps the COPY message receive (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:65` … `:67`, and `:90` … `:95`)
- `ReceiveAndWriteCopyData_WriteFile` wraps the file append (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:75` … `:77`)

## Accuracy of the earlier characterization (push)

Accurate:

- The push path serializes tuples **one by one** into COPY framing and transmits them as they are produced.
- Workers persist the stream as intermediate result files.

Refinement:

- The “gather → scatter” description is not always literally true: the **upstream** of the broadcast stream might be a local coordinator plan or a gather from workers.
- The stable fact is the mechanism: **coordinator broadcasts an intermediate result stream** using `RemoteFileDestReceiver` and `COPY ... WITH (format result)`.

## Related

- `docs/kb/citus/intermediate-results/push_pull_intermediate_results.md` (push + pull in one place)
- `docs/kb/instrumentation/timing-spots/timing_spots.md` (canonical spot meanings + call sites)

