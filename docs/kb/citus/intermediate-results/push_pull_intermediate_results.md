# Intermediate results: push (broadcast) vs pull (fetch)

## Scope

This doc ties the conceptual “push vs pull” data movement to concrete code paths and timing spots in Citus:

- Push/broadcast via a `DestReceiver` that serializes tuples into COPY “format result” and sends them to nodes.
- Pull/fetch via `fetch_intermediate_results()` which issues a remote COPY OUT and writes to a local file.

## Push path: broadcast intermediate results via `RemoteFileDestReceiver`

Core implementation lives in:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c`

### Setup/init (`RemoteFileDestReceiver_Init`)

Initialization spans multiple call sites:

- `RemoteFileDestReceiverStartup(...)` wraps serialization setup with `RemoteFileDestReceiver_Init` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:264` … `:286`).
- `PrepareIntermediateResultBroadcast(...)` wraps connection setup + sending COPY command with `RemoteFileDestReceiver_Init` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:299` … `:379`).

The push protocol used is:

- `COPY "<resultId>" FROM STDIN WITH (format result)` constructed by `ConstructCopyResultStatement()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:388` … `:396`).

### Per-tuple serialize + send (`RemoteFileDestReceiver_SerAndSend`, `RemoteFileDestReceiver_Ser`, `RemoteFileDestReceiver_Send`)

The actual per-tuple work is in `RemoteFileDestReceiverReceive(...)`:

- Defined at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:404`
- Timed by:
  - `RemoteFileDestReceiver_SerAndSend` start/end (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:409` … `:475`)
  - Verbose split:
    - `RemoteFileDestReceiver_Ser` around `AppendCopyRowData(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:431` … `:447`)
    - `RemoteFileDestReceiver_Send` around `BroadcastCopyData(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:450` … `:455`)
  - Optional local-file write:
    - `RemoteFileDestReceiver_SerAndSend_WriteLocal` around `WriteToLocalFile(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:461` … `:463`)

Custom stats:

- Rows sent: `timing_add_stat(RemoteFileDestReceiver_SerAndSend, STAT_TOTAL_ROWS_SENT, 1)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:408`)
- Bytes sent: `timing_add_stat(RemoteFileDestReceiver_SerAndSend, STAT_TOTAL_BYTES_SENT, copyData->len)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:470`)

### Buffering / copying shape of push

This path is still server-side backend code, but it is not `printtup`.

- Serialization happens in `AppendCopyRowData(...)` inside `RemoteFileDestReceiverReceive(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:431` … `:447`), producing COPY-format bytes in a `StringInfo`.
- Sending to remote nodes happens through libpq frontend APIs inside the backend process: `BroadcastCopyData(...)` calls `SendCopyDataOverConnection(...)`, which calls `PutRemoteCopyData(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:552` … `:569`).
- `PutRemoteCopyData(...)` itself wraps `PQputCopyData(...)` and applies backpressure with `FinishConnectionIO(...)` when too many bytes have accumulated (`/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:738` … `:775`).
- `PQ_putCopyData` is now timed directly in frontend libpq at `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2810`, so reports can separate COPY message staging from lower `PG_FE_SOCK_WRITE` / `PG_FE_SOCK_READ` execution.
- `PQ_putCopyEnd` now covers the final COPY close/ack message assembly.
- connection setup on this path is also split into `PQ_connectStart` (synchronous `PQconnectStartParams()` front half) and `PQ_connectPoll` (asynchronous poll-loop progression).

So the send chain here is roughly:

- tuple -> COPY-format `StringInfo`
- `StringInfo` -> libpq `PGconn->outBuffer`
- libpq output buffer -> remote socket

That differs from core backend `printtup`, which stays entirely inside backend `pq_*` / `PqSendBuffer` machinery.

### What exactly is stored in the intermediate-result file

The intermediate-result file does contain serialized rows, but not FE/BE `DataRow` messages.

- [`RemoteFileDestReceiverReceive(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L405) calls [`AppendCopyRowData(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1467)
- [`AppendCopyRowData(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1459) says it serializes one row and is modeled after PostgreSQL [`CopyOneRowTo()`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L931)
- the resulting bytes are written to disk with [`WriteToLocalFile(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L488) or forwarded over COPY transport

So the file contents are serialized COPY-format row bytes. They are not backend tuple memory images.

### Receive side of push: worker writes incoming COPY stream to file (`ReceiveAndWriteCopyData_*`)

The worker-side “receive via COPY and write to regular file” helper is:

- `RedirectCopyDataToRegularFile(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:50`

Timing spots:

- `ReceiveAndWriteCopyData_` wraps the whole receive loop (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:55` … `:101`)
- Verbose deserialize/mem-copy split:
  - `ReceiveAndWriteCopyData_Deser` around `ReceiveCopyData(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:65` … `:67`, and again at `:90` … `:95`)
- Verbose file write split:
  - `ReceiveAndWriteCopyData_WriteFile` around `FileWriteCompat(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:75` … `:77`)

Important nuance:

- `ReceiveCopyData(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:298`) uses `pq_getbyte()` + `pq_getmessage()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:306` … `:315`).
- the backend protocol type byte is now visible as `PQ_getbyte` in `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:963`
- receive-side message framing and `StringInfo` management are now visible as `PQ_getmessage` in `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1214`
- the lower backend receive-buffer draining/copy is now visible as `PQ_getbytes` in `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1064`
- the actual socket read execution remains in `PG_BE_SOCK_READ`
- therefore `ReceiveAndWriteCopyData_Deser` should be treated as a coarse parent/debug region, while `PQ_getbyte` + `PQ_getmessage` + `PQ_getbytes` + `PG_BE_SOCK_READ` are the additive backend receive leaves

### Buffering / copying shape of receive

This receive helper is very close to raw PostgreSQL backend protocol receive:

- transport bytes arrive in backend `PqRecvBuffer` through `pq_recvbuf()` in core Postgres (`/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:897`)
- `ReceiveCopyData(...)` calls `pq_getmessage(copyData, ...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:314`), which copies one full CopyData message body into the reusable `StringInfo`
- the outer loop in `RedirectCopyDataToRegularFile(...)` writes `copyData->data` directly to the destination file (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:72` … `:80`)

So for this path the chain is:

- socket -> `PqRecvBuffer`
- `PqRecvBuffer` -> `copyData` `StringInfo`
- `copyData` -> file

There is no tuple deserialization here at all; the payload is opaque intermediate-result bytes.

## Pull path: fetch intermediate results via `fetch_intermediate_results()` (`FetchIntermediate_*`)

Pull path is implemented in:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:926`

### Top-level UDF timing (`FetchIntermediate_`)

`fetch_intermediate_results(PG_FUNCTION_ARGS)` wraps the whole operation:

- `timing_start(FetchIntermediate_)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:926`)
- `timing_end(FetchIntermediate_)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:988`)

It uses a single libpq connection to a source node:

- `GetNodeConnection(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:964`)
- Sends `COPY "<resultId>" TO STDOUT WITH (format result)` in `FetchRemoteIntermediateResult(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1023`–`:1026`)
  - COPY statement is built at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1027`.

### Copy loop split: copy+write (`FetchIntermediate_CopyAndWrite`), socket wait (`PG_WAIT`), file write (`FetchIntermediate_FileWrite`)

The COPY loop in `FetchRemoteIntermediateResult(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:998`) is broken down as:

- `FetchIntermediate_CopyAndWrite` around `CopyDataFromConnection(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1051` … `:1055`)
- `PG_WAIT` around `WaitLatchOrSocket(...)` when COPY would otherwise block (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1070` … `:1074`)
- `FetchIntermediate_FileWrite` around `FileWriteCompat(...)` inside `CopyDataFromConnection(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1123` … `:1135`)

Custom stats:

- `FetchIntermediate_` records total number of resultIds + bytes written (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:981` … `:983`).
- `FetchIntermediate_FileWrite` records per-chunk bytes at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1134`.

### Why pull is architecturally different from push

Pull uses frontend libpq inside a backend process:

- `PQconsumeInput()` is called in `CopyDataFromConnection(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1107`)
- `PQgetCopyData()` returns each COPY chunk as a freshly malloc'd buffer (`/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-protocol3.c:1744` … `:1796`)
- Citus writes that buffer to file and frees it with `PQfreemem()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1125` … `:1132`)

So pull is not just “the reverse of `SendViaCopy_`”. It uses a different stack:

- remote socket -> libpq `conn->inBuffer`
- libpq `conn->inBuffer` -> fresh `receiveBuffer` from `PQgetCopyData()`
- `receiveBuffer` -> file

Compared to raw backend receive, that adds an extra per-chunk allocation/copy boundary because libpq hands the caller an owned buffer.

The additive pull-side leaves are therefore:

- `PG_FE_SOCK_READ` for active libpq socket reads
- `PQ_getCopyData` for buffered COPY-row extraction/copy
- `FetchIntermediate_FileWrite` for the separate file bucket

`FetchIntermediate_CopyAndWrite` remains a coarse parent, and `PG_WAIT` stays a separate wait diagnostic.

## When deserialization actually happens

Intermediate-result transport and file persistence often treat COPY bytes opaquely. The later tuple reconstruction point is [`ReadFileIntoTupleStore(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L447):

- it builds a PostgreSQL COPY reader with [`BeginCopyFrom(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L471)
- loops on [`NextCopyFrom(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L480)
- and stores typed values with [`tuplestore_putvalues(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L488)

So the lifecycle is:

- serialize tuples into COPY-format bytes
- optionally forward or persist those bytes without interpretation
- later deserialize them through PostgreSQL COPY parsing when the file is consumed

## Comparison to core PostgreSQL

- `SendViaCopy_` in `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:111` stays on raw backend `pq_*` and is mechanically closer to core Postgres backend send.
- `ReceiveAndWriteCopyData_` in `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:50` also stays on raw backend `pq_*` and is mechanically closer to core Postgres backend receive.
- `FetchIntermediate_` in `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:930` is different: the backend process acts as a libpq client to another node, so buffer management follows frontend libpq rules (`PGconn`, `PQconsumeInput`, `PQgetCopyData`) rather than backend `PqRecvBuffer` / `input_message`.

## Bucket guidance

- Strict transport/protocol leaves for push:
  - `RemoteFileDestReceiver_Ser`
  - `PQ_putCopyData`
  - `PQ_putCopyEnd`
  - `PG_FE_SOCK_WRITE`
  - `PG_FE_SOCK_READ` when backpressure handling consumes remote input
- Strict transport/protocol leaves for pull:
  - `PG_FE_SOCK_READ`
  - `PQ_getCopyData`
- Backend raw COPY receive leaves on worker-side push:
  - `PQ_getbyte`
  - `PQ_getmessage`
  - `PQ_getbytes`
  - `PG_BE_SOCK_READ`
- Separate file bucket:
  - `ReceiveAndWriteCopyData_WriteFile`
  - `FetchIntermediate_FileWrite`
  - `SendViaCopy_FileRead`

## How this ties to repartitioning and colocation

Repartition/colocation transfers are explicitly implemented as SQL calls to `fetch_intermediate_results()`:

- `ColocateFragmentsWithRelation(...)` comment and call site (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/distributed_intermediate_results.c:472` … `:488`)
- Query constructed in `QueryStringForFragmentsTransfer(...)` uses `SELECT bytes FROM fetch_intermediate_results(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/distributed_intermediate_results.c:649` … `:654`)
- MERGE executor notes these transfers (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/merge_executor.c:162` … `:166`)
