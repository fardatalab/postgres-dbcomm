# `DataRow` vs PostgreSQL `COPY` vs Citus COPY transport

## Scope

- **What this doc explains**: How PostgreSQL `printtup`/`DataRow` sending differs from PostgreSQL `COPY TO` / `COPY FROM`, and how Citus reuses or layers on top of PostgreSQL COPY for distributed transport and intermediate-result files.
- **What this doc does NOT cover**: Frontend `PGresult` internals in detail, or every Citus COPY-to-shard implementation detail.
- **Primary directory**: `docs/kb/postgres/serialization/`

## Why this exists (context)

The recurring confusion in discussion was that:

- `printtup` and `COPY` both serialize rows
- Citus intermediate results also move row bytes around with `COPY`
- therefore the paths can look "the same"

They are related, but not interchangeable. The key distinction is the **format and layer**:

- `printtup` builds FE/BE `DataRow` messages for normal query results
- PostgreSQL `COPY` builds and parses a COPY stream format
- Citus often uses COPY as a transport envelope for inter-node or file-based movement, sometimes on raw backend `pq_*`, sometimes through frontend libpq inside the backend

## Key code pointers

- [`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L746) sends result tuples as `DataRow`
- [`CopyOneRowTo()`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L931) serializes one row for `COPY TO`
- [`CopySendEndOfRow()`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L190) flushes one COPY row/chunk to file, frontend, or callback
- [`CopyGetData()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L247) receives raw COPY bytes
- [`NextCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L862) parses COPY rows into typed values
- [`CopyReadBinaryAttribute()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L1994) turns one binary COPY field into a typed `Datum`
- [`RemoteFileDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L405) serializes executor tuples into Citus intermediate-result COPY bytes
- [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1467) is the Citus row serializer modeled after PostgreSQL COPY
- [`RedirectCopyDataToRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L51) receives COPY protocol bytes on the worker and writes them to a file
- [`SendRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L111) sends file bytes back out over COPY protocol
- [`ReadFileIntoTupleStore()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L447) deserializes an intermediate-result file back into tuples using PostgreSQL COPY parsing
- [`fetch_intermediate_results()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L926) pulls an intermediate-result file over libpq COPY OUT
- Citus README summary for distributed `COPY FROM`: [`README.md`](/data/dbcomm/citus-dbcomm/src/backend/distributed/README.md#L1684)

## Current behavior / grounded findings

### 1. `printtup` and PostgreSQL `COPY TO` are both row serializers, but they target different formats

[`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L746):

- takes a [`TupleTableSlot`](/data/dbcomm/postgres-citus/src/include/executor/tuptable.h#L114)
- converts each `Datum` with [`OutputFunctionCall()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L814) or [`SendFunctionCall()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L823)
- appends them into a FE/BE `DataRow` message buffer with [`pq_beginmessage_reuse()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L780), [`pq_sendint16()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L782), [`pq_sendcountedtext()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L815), [`pq_sendbytes()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L831), and [`pq_endmessage_reuse()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L844)

[`CopyOneRowTo()`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L931):

- also starts from a `TupleTableSlot`
- also uses [`OutputFunctionCall()`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L975) or [`SendFunctionCall()`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L987)
- but appends into a COPY stream buffer with [`CopySendInt16()`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L945), [`CopySendData()`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L990), [`CopyAttributeOutText()`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L981), and [`CopySendEndOfRow()`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L996)

So the answer to "aren't they both row-by-row serialize and send?" is: **yes, but into different on-the-wire/on-disk formats**.

### 2. PostgreSQL `COPY TO` is not the same as backend query-result send

The important differences are:

- **Protocol envelope**
  - `printtup` emits FE/BE result messages, specifically `DataRow`
  - `COPY TO STDOUT` emits `CopyData` messages whose payload is COPY-format row bytes
- **Consumer**
  - `DataRow` is consumed by normal query-result receivers
  - COPY payload is consumed by COPY readers/parsers
- **Buffering**
  - `printtup` uses per-row scratch [`tmpcontext`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L769) plus reusable row buffer [`myState->buf`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L751)
  - `COPY TO` uses per-row scratch [`cstate->rowcontext`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L939) plus reusable COPY output buffer [`cstate->fe_msgbuf`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L73)

Conceptually:

- `printtup`: tuple -> FE/BE `DataRow`
- `COPY TO`: tuple -> COPY row bytes

Both are serializers. They are not the same serializer.

### 3. PostgreSQL `COPY FROM` is also not the same as generic backend protocol receive like `Bind`

Generic backend receive uses:

- backend transport buffer `PqRecvBuffer`
- whole-message extraction with [`pq_getmessage()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c#L1202)
- mostly in-place field parsing with [`pq_getmsgbytes()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqformat.c#L507) and friends
- typed reconstruction in handlers such as [`exec_bind_message()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1802)

PostgreSQL `COPY FROM` is a different parser pipeline:

- raw stream bytes are pulled by [`CopyGetData()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L247)
- text/CSV mode uses reusable buffers such as [`raw_buf`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L598), [`input_buf`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L658), [`line_buf`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L1545), and [`attribute_buf`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L1566)
- binary mode reads one field into [`attribute_buf`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L2015) and then calls [`ReceiveFunctionCall()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L2029)
- row-level parsing is driven by [`NextCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L862) from the outer loop in [`CopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L999)

So when I previously said "that is not the raw backend receive model", the intended contrast was:

- generic FE/BE message receive like [`exec_bind_message()`](/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c#L1802)
- versus COPY stream parsing or frontend-libpq-in-backend COPY fetching

That was **not** meant to claim that Citus COPY is categorically unrelated to PostgreSQL COPY.

### 4. Citus uses two different COPY-related styles

#### A. Citus code that stays on raw backend PostgreSQL COPY/protocol machinery

Examples:

- [`RedirectCopyDataToRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L51)
- [`ReceiveCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L299)
- [`SendRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L111)
- [`SendCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L280)

These use backend `pq_*` directly:

- receive with [`pq_getbyte()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L306) and [`pq_getmessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L314)
- send with [`pq_beginmessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L284), [`pq_sendbytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L285), and [`pq_endmessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L286)

Mechanically, this is close to PostgreSQL backend protocol handling.

#### B. Citus code where a backend acts as a libpq frontend client to another backend

Examples:

- [`fetch_intermediate_results()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L926)
- [`CopyDataFromConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1100)
- adaptive executor row fetching in [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3996)

These use frontend libpq APIs inside the backend process:

- [`PQconsumeInput()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1107)
- [`PQgetCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1116)
- [`PQgetResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4026)

That path follows frontend-libpq buffer ownership rules, not raw backend `PqRecvBuffer` / `input_message` rules.

### 5. Citus intermediate files do contain serialized row bytes

Your recollection is mostly correct, but with an important nuance about **when** deserialization happens.

When Citus produces intermediate results:

- [`RemoteFileDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L405) calls [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1467)
- [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1459) explicitly says it serializes one row and is modeled after PostgreSQL [`CopyOneRowTo()`](/data/dbcomm/postgres-citus/src/backend/commands/copyto.c#L931)
- the resulting bytes are either broadcast with [`BroadcastCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L552) or appended to a local file with [`WriteToLocalFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L488)

So intermediate-result files are **not** raw in-memory tuples. They are serialized COPY-format row bytes.

However, not every path deserializes them immediately:

- [`RedirectCopyDataToRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L51) only receives opaque COPY payload and writes it to disk
- [`SendRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L111) only reads opaque bytes from disk and sends them back out as COPY payload
- [`CopyDataFromConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1100) only pulls opaque COPY chunks from libpq and writes them to disk

The later deserialization point is [`ReadFileIntoTupleStore()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L447), which:

- calls [`BeginCopyFrom()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L471)
- loops on [`NextCopyFrom()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L480)
- stores typed values into a tuplestore with [`tuplestore_putvalues()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L488)

So the precise statement is:

- intermediate files store serialized COPY-format rows
- many transport/file paths treat those bytes opaquely
- deserialization happens later when a PostgreSQL COPY reader path consumes the file

### 6. Citus distributed table `COPY FROM` is explicitly based on PostgreSQL COPY parsing

The Citus design note in [`README.md`](/data/dbcomm/citus-dbcomm/src/backend/distributed/README.md#L1688) is explicit:

- Citus goes through PostgreSQL [`BeginCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L1389) and [`NextCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L862)
- PostgreSQL parses the incoming client/file/socket COPY data into a tuple
- Citus then routes that tuple to shard placements, often using binary COPY on internal connections

So yes, for distributed table ingestion, Citus is fundamentally layered on PostgreSQL COPY parsing rather than inventing a separate parser stack.

## Architecture / data flow

### `printtup` result-send path

- tuple slot -> per-attribute `Datum`
- `Datum` -> text/binary attribute output
- attribute outputs -> contiguous `DataRow` in reusable row buffer
- `DataRow` -> backend transport buffer / socket

### PostgreSQL `COPY TO`

- tuple slot -> per-attribute `Datum`
- `Datum` -> text/binary COPY field bytes
- field bytes -> reusable COPY row buffer
- COPY row buffer -> file / `CopyData` protocol message / callback

### PostgreSQL `COPY FROM`

- file / `CopyData` payload -> reusable raw/input/line/attribute buffers
- COPY row parser -> field slices / field buffer
- field text/binary -> typinput / typreceive
- typed values -> caller-provided `Datum[]` / `bool[]`

### Citus intermediate-result push

- tuple slot -> Citus COPY `format result` bytes through [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1467)
- those bytes -> remote libpq connections and/or local file

### Citus intermediate-result pull

- remote COPY OUT -> libpq `PQgetCopyData()` chunk
- libpq chunk -> local file
- later local file -> PostgreSQL `BeginCopyFrom()` / `NextCopyFrom()` -> tuples

## Invariants and assumptions

- Citus intermediate-result files are serialized row bytes, not raw `HeapTuple` images.
- Byte-forwarding paths such as [`SendRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L111) and [`RedirectCopyDataToRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L51) are transport/storage helpers, not tuple deserializers.
- `PGresult` use in Citus is real but belongs to the libpq-in-backend family, not to PostgreSQL raw backend COPY receive.

## Interactions and cross-links

- **Send-side result protocol**: `printtup_row_serialization_and_transport.md`
- **Generic backend receive path**: `server_side_protocol_receive_and_deserialize.md`
- **Citus intermediate result transport**: `../../citus/intermediate-results/push_pull_intermediate_results.md`
- **Citus executor row receive via libpq**: `../../citus/executor/receive_results_streaming.md`
