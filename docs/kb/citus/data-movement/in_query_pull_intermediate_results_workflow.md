# In-query tuple movement (pull): worker fetches intermediate results from another node via `fetch_intermediate_results()`

## Scope

This doc covers the **pull/fetch** data movement mode where a node that needs an intermediate result file connects to another node and pulls it directly.

Key idea:

- Data does **not** go through the coordinator data plane; the target node pulls from the source node.
- Citus still uses a COPY-framed protocol (`WITH (format result)`), but the payload is an *intermediate result file*.

Primary timers (overview):

- Fetch side:
  - `FetchIntermediate_`
  - `FetchIntermediate_CopyAndWrite` (verbose)
  - `FetchIntermediate_FileWrite`
  - `PG_WAIT` (socket wait between COPY chunks)
- Serve side:
  - `ProcessCopyStmt_` (dispatch wrapper)
  - `SendViaCopy_` → `SendViaCopy_FileRead` + `SendViaCopy_Send` (verbose)

## Target-side workflow (fetcher)

### 1) Entry point: UDF `fetch_intermediate_results(PG_FUNCTION_ARGS)`

Implementation:

- `fetch_intermediate_results(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:926`
- Timed end-to-end by `FetchIntermediate_` (`timing_start` at `:926`, `timing_end` at `:988`).

Important preconditions:

- Requires a distributed transaction (`IsMultiStatementTransaction()` check at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:948`).
- Ensures a distributed transaction ID exists (`EnsureDistributedTransactionId()` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:959`).

### 2) Connect to source + set distributed transaction id on that node

The fetcher:

- Establishes a connection via `GetNodeConnection(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:964`.
- Sends `BeginAndSetDistributedTransactionIdCommand()` and executes it remotely via `ExecuteCriticalRemoteCommand(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:975`.

This is the mechanism that makes intermediate-results directory naming align across nodes.

### 3) Per-result fetch: `FetchRemoteIntermediateResult()`

- `FetchRemoteIntermediateResult(...)` is defined at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1003`.
- It sends the serve request to the source node:
  - builds: `COPY "<resultId>" TO STDOUT WITH (format result)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1027`
  - sends via `SendRemoteCommand(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1030`
  - expects `PGRES_COPY_OUT` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1038`

### 4) COPY chunk loop: read + write, plus wait-for-socket

Inside `FetchRemoteIntermediateResult(...)`, the loop splits into:

- `FetchIntermediate_CopyAndWrite` wraps `CopyDataFromConnection(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1051` … `:1055`).
  - `CopyDataFromConnection(...)` uses `PQgetCopyData(...)` and writes chunks to disk.
- `FetchIntermediate_FileWrite` wraps the per-chunk file append (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1123` … `:1135`).
- When the COPY stream would block, the loop waits on readability:
  - `PG_WAIT` wraps `WaitLatchOrSocket(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1070` … `:1074`).

## Source-side workflow (serving `COPY "<resultId>" TO STDOUT WITH (format result)`)

### 1) COPY hook dispatch (`ProcessCopyStmt`)

On the source node, the incoming COPY statement is intercepted by:

- `ProcessCopyStmt(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2988`
- Timed by `ProcessCopyStmt_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2993`)

When `IsCopyResultStmt(copyStatement)` is true and `copyStatement->is_from` is false, it calls:

- `SendQueryResultViaCopy(resultId)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3009`
- Implementation: `SendQueryResultViaCopy(...)` in `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:605`
  - resolves `QueryResultFileName(resultId)`
  - serves the file via `SendRegularFile(resultFileName)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:609`)

### 2) Send file over COPY (`SendRegularFile` / `SendViaCopy_*`)

`SendRegularFile(...)` is in:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:114`

Timed breakdown:

- `SendViaCopy_` wraps the overall send (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:118` … `:162`)
- `SendViaCopy_FileRead` wraps `FileReadCompat(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:134`, `:151`)
- `SendViaCopy_Send` wraps `SendCopyData(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:144`)

## How this relates to repartitioning / colocation

Repartition/colocation transfers are explicitly implemented as SQL calls to `fetch_intermediate_results()`:

- `QueryStringForFragmentsTransfer(...)` uses `SELECT bytes FROM fetch_intermediate_results(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/distributed_intermediate_results.c:649` … `:654`)

## Accuracy of the earlier characterization (pull)

Accurate:

- Pull-based movement avoids the coordinator data plane by having the consumer fetch directly from the producer.
- The transfer is COPY-framed and writes an intermediate result file on the consumer side.

Refinement:

- The “producer” side is implemented as a special-case COPY handler (`ProcessCopyStmt` + `SendQueryResultViaCopy`), not as a generic `COPY table TO STDOUT`.

## Related

- `docs/kb/citus/intermediate-results/push_pull_intermediate_results.md`
- `docs/kb/instrumentation/timing-spots/timing_spots.md`

