# Adaptive executor: coordinator receiving streaming results (`ReceiveResults_*`)

## Scope

This doc explains how the coordinator receives worker results in the adaptive executor using libpq, and how this maps to:

- `CheckConnectionReady_`
- `ReceiveResults_`
- `ReceiveResults_Net`
- `ReceiveResults_Deserialize`
- `ReceiveResults_BuildTuples`
- `ReceiveResults_HeapFormTuple`

For the canonical meaning + call sites of these spots, see:

- `docs/kb/instrumentation/timing-spots/timing_spots.md`

## Entry point: `ReceiveResults()` (Citus)

`ReceiveResults()` is defined in:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3996`

The timing is structured as:

- post-wakeup libpq processing in `CheckConnectionReady()` is now timed separately by `CheckConnectionReady_` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3716`
- `timing_start(ReceiveResults_)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4009`
- Loop while `!PQisBusy(connection->pgConn)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4019`
- `timing_end(ReceiveResults_)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4247`

The receive-side bucket split also depends on `SendNextQuery()` enabling libpq single-row mode with `PQsetSingleRowMode(connection->pgConn)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3998`. That means a `PGRES_SINGLE_TUPLE` result should contribute at most one row to the later deserialize/materialize stages.

## Important architectural correction: `PGresult` is used server-side in Citus

`PGresult` is not only a psql/frontend concern in this system. Citus backend processes use frontend libpq (`PGconn`, `PGresult`, `PQgetResult`, `PQgetvalue`, `PQgetlength`, `PQgetCopyData`) to communicate with other PostgreSQL nodes.

Concrete evidence:

- `ReceiveResults()` calls `PQgetResult(connection->pgConn)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4026`
- it then reads rows through `PQntuples()`, `PQnfields()`, `PQgetlength()`, `PQgetvalue()`, and `PQfformat()` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4109` … `:4195`

So in Citus inter-node row receive, libpq is embedded inside the server backend process.

## Network vs deserialize vs build-tuples

Within the receive workflow, libpq interaction is split as:

- **Post-wakeup connection processing**:
  - `CheckConnectionReady_` wraps `CheckConnectionReady(session)` in `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3704`
  - inside that region, `PQflush()`, `PQconsumeInput()`, and `PQisBusy()` are called at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3734`, `:3749`, and `:3757`
  - lower-level leaves inside that region are:
    - `PG_FE_SOCK_WRITE` in `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:861`
    - `PG_FE_SOCK_READ` in `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:598`
    - `PG_parseInput` in `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2026`

- **Buffered result extraction**:
  - `timing_start(ReceiveResults_Net)` before `PQgetResult(connection->pgConn)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4042`
  - `timing_end(ReceiveResults_Net)` after `PQgetResult(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4044`
  - `PQ_getResult` is now timed inside `PQgetResult()` itself at `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2075`
  - this region no longer claims to be the full “network receive” path; by the time it runs, readiness and most input/read/parse work have already happened above in `CheckConnectionReady()`
- **Deserialization**:
  - `timing_start(ReceiveResults_Deserialize)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4037`
  - `timing_end(ReceiveResults_Deserialize)` happens on multiple branches, e.g.:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4058` (`PGRES_COMMAND_OK`)
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4073` (`PGRES_TUPLES_OK`)
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4079` (non-`PGRES_SINGLE_TUPLE` error path)
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4090` (`!storeRows` path)
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4203` (normal single-tuple case)
  - The deserialization region includes checking `PQresultStatus(result)` (e.g. `PGRES_COMMAND_OK`, `PGRES_TUPLES_OK`, `PGRES_SINGLE_TUPLE`) and extracting column values via `PQgetlength()`/`PQgetvalue()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4039`–`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4192`).
  - Because adaptive executor uses `PQsetSingleRowMode()`, that region is effectively the per-streamed-row libpq handling bucket for `PGRES_SINGLE_TUPLE` results.
- **Build heap tuples**:
  - `timing_start(ReceiveResults_BuildTuples)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4206`
  - `timing_end(ReceiveResults_BuildTuples)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4235`
  - Tuple construction happens via:
    - Binary results: `BuildTupleFromBytes(...)` call at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4212`
    - Text results: `BuildTupleFromCStrings(...)` call at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4217`

## Memory/buffer and copy chain

The receive path here is not the same as backend `exec_bind_message()`, because it does not use backend `PqRecvBuffer` / `input_message`. Instead it uses frontend libpq buffering inside the backend:

- `PQgetResult()` drives libpq parsing, which ultimately consumes bytes from `PGconn->inBuffer` (`/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:608`, `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-protocol3.c:66`)
- libpq parses `DataRow` by storing temporary `(pointer,len)` slices in `conn->rowBuf` that point into `conn->inBuffer` (`/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-protocol3.c:786` … `:819`)
- `pqRowProcessor()` then copies each field into `PGresult` storage via `pqResultAlloc()` (`/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:1210` … `:1284`, `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:567`)
- `ReceiveResults()` then reads field pointers back out of `PGresult` with `PQgetvalue()` / `PQgetlength()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4176` … `:4195`)
- for binary results, Citus copies each field yet again into a reusable per-column `StringInfoData` in `execution->stringInfoDataArray` via `appendBinaryStringInfo()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4184` … `:4187`)
- final typed values are then produced by `BuildTupleFromBytes()` or `BuildTupleFromCStrings()` and packed into a backend `HeapTuple` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4212` … `:4218`)

So the effective chains are:

- text result:
  - remote socket -> libpq `inBuffer`
  - `inBuffer` -> `PGresult` field storage
  - `PGresult` text pointers -> typinput / `BuildTupleFromCStrings()`
  - typinput results -> `HeapTuple`

- binary result:
  - remote socket -> libpq `inBuffer`
  - `inBuffer` -> `PGresult` field storage
  - `PGresult` field storage -> Citus per-column `StringInfoData`
  - `StringInfoData` -> typreceive / `BuildTupleFromBytes()`
  - typreceive results -> `HeapTuple`

That extra `PGresult` layer is the main difference from core backend receive paths.

## `ReceiveResults_HeapFormTuple` overlap note

`ReceiveResults_HeapFormTuple` times the internal `heap_form_tuple(...)` call used by tuple builders.

Call sites:

- Text path uses PostgreSQL `BuildTupleFromCStrings()` which is instrumented in `/data/dbcomm/postgres-citus/src/backend/executor/execTuples.c:2266`.
- Binary path uses Citus `BuildTupleFromBytes()` which is instrumented in `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tuples.c:120`.

Implication:

- `ReceiveResults_BuildTuples` includes the `ReceiveResults_HeapFormTuple` time (nested), so these two spots are not disjoint.

## Why `rowContext` exists

`ReceiveResults()` creates `rowContext` once at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4016` and switches into it per row at `:4164`, resetting it at `:4230`.

That is the same lifetime-control idea as Postgres `printtup`:

- parsing one row into backend typed values may allocate through typinput/typreceive
- Citus wants those allocations gone after each row
- so it uses a reusable per-row scratch context and bulk-frees it with `MemoryContextReset(rowContext)`

The difference is what feeds that row context:

- Postgres `printtup`: backend tuple -> temporary serializer objects -> protocol message
- Citus `ReceiveResults`: `PGresult` field bytes -> temporary input/receive objects -> backend `HeapTuple`

## Bucket guidance

- For an additive strict transport/protocol bucket, use the low-level leaves:
  - `PG_FE_SOCK_READ`
  - `PG_FE_SOCK_WRITE`
  - `PG_parseInput`
  - `PQ_getResult`
- Treat `CheckConnectionReady_` and `ReceiveResults_Net` as coarse parents/debug aids rather than additive leaves, because they sit above the lower libpq spots.
- For a broader “remote result handling” bucket, add `ReceiveResults_Deserialize`.
- Keep `ReceiveResults_BuildTuples` and `ReceiveResults_HeapFormTuple` in a separate result-materialization bucket, not in the strict transport/protocol bucket.
- Under the current single-row-mode invariant, `ReceiveResults_Deserialize` and `ReceiveResults_BuildTuples` are both effectively one-streamed-row buckets; if single-row mode were removed later, this split would need to be revisited.

## Comparison to core PostgreSQL

- Similarity: the final typed conversion step is still standard PostgreSQL type I/O and tuple formation (`InputFunctionCall`, `ReceiveFunctionCall`, `heap_form_tuple`).
- Difference: Citus inter-node row receive uses frontend libpq in the backend process, so there is an intermediate `PGresult` ownership domain and copy step that core backend receive paths like `exec_bind_message()` do not have.

## Custom stats recorded

`ReceiveResults_` adds stats for throughput/volume:

- Bytes received per tuple: `timing_add_stat(ReceiveResults_, STAT_TOTAL_BYTES_RECEIVED, tupleLibpqSize)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4228`
- Rows received per `PGresult` batch: `timing_add_stat(ReceiveResults_, STAT_TOTAL_ROWS_RECEIVED, rowsProcessed)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4239`
