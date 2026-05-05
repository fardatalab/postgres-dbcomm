# `printtup` row serialization and transport

## Scope

- **What this doc explains**: How a result tuple in a `TupleTableSlot` becomes a FE/BE `DataRow` message in `printtup`, where allocations happen, which buffers hold which bytes, and how bytes move into the backend transport buffer and socket.
- **What this doc does NOT cover**: COPY protocol, frontend-side parsing of `DataRow`, or a full catalog of all type-specific send/output routines.
- **Primary directory**: `docs/kb/postgres/serialization/`

## Why this exists (context)

The user discussion focused on the networking and communication stack and asked what `MemoryContextSwitchTo()` is doing in `src/backend/access/common/printtup.c:769`, what `Datum` and `bytea` mean on this path, and whether PostgreSQL could reduce the number of intermediate buffers and copies.

This doc is the grounded note for the current path. It intentionally separates:

- what the code does today
- where the memory/context boundaries are
- where bytes are copied today

The forward-looking redesign ideas are kept in the top-level future-directions note `../../future-directions/postgres/serialization/printtup_send_path_future_directions.md`.

## Key code pointers

- `src/backend/tcop/dest.c:113` - `CreateDestReceiver()` selects `printtup_create_DR()` for remote destinations
- `src/backend/tcop/postgres.c:2360` - receiver is created in `MessageContext`
- `src/backend/tcop/pquery.c:746` - `PortalRun()` switches to `PortalContext`
- `src/backend/executor/execUtils.c:97` - `CreateExecutorState()` creates `es_query_cxt` under the current context
- `src/backend/executor/execMain.c:333` - `standard_ExecutorRun()` switches into `es_query_cxt`
- `src/backend/executor/execMain.c:354` - receiver startup hook
- `src/backend/executor/execMain.c:1679` - row delivery through `dest->receiveSlot`
- `src/backend/access/common/printtup.c:536` - `printtup_startup()`
- `src/backend/access/common/printtup.c:554` - `printtup` per-row temporary context creation
- `src/backend/access/common/printtup.c:676` - `printtup_prepare_info()`
- `src/backend/access/common/printtup.c:746` - `printtup()`
- `src/backend/access/common/printtup.c:766` - `slot_getallattrs(slot)`
- `src/backend/access/common/printtup.c:769` - `MemoryContextSwitchTo(myState->tmpcontext)`
- `src/backend/access/common/printtup.c:814` - `OutputFunctionCall(...)`
- `src/backend/access/common/printtup.c:823` - `SendFunctionCall(...)`
- `src/backend/access/common/printtup.c:850` - per-row `MemoryContextReset(myState->tmpcontext)`
- `src/backend/access/common/printtup.c:873` and `:879` - shutdown of reusable row buffer and tmp context
- `src/include/executor/tuptable.h:367` - `slot_getallattrs()`
- `src/backend/executor/execTuples.c:1010` - tuple deformation logic
- `src/backend/executor/execTuples.c:1090` - slot stores `Datum` values via `fetchatt(...)`
- `src/include/access/tupmacs.h:46` - `fetchatt`
- `src/include/postgres.h:64` - `Datum`
- `src/include/c.h:699` - `bytea`
- `src/include/fmgr.h:240`, `:291`, `:331` - detoast and `bytea` access helpers
- `src/include/lib/stringinfo.h:22` - `StringInfoData`
- `src/common/stringinfo.c:59` - `initStringInfo()`
- `src/common/stringinfo.c:78` - `resetStringInfo()`
- `src/common/stringinfo.c:283` - `repalloc` keeps the buffer in the original context
- `src/backend/libpq/pqformat.c:109` - `pq_beginmessage_reuse()`
- `src/backend/libpq/pqformat.c:125` - `pq_sendbytes()`
- `src/backend/libpq/pqformat.c:141` - `pq_sendcountedtext()`
- `src/backend/libpq/pqformat.c:314` - `pq_endmessage_reuse()`
- `src/backend/utils/mb/mbutils.c:738` - `pg_server_to_client()`
- `src/backend/utils/mb/mbutils.c:782` - `perform_default_encoding_conversion()`
- `src/backend/libpq/pqcomm.c:279` - global backend send buffer allocation
- `src/backend/libpq/pqcomm.c:1275` - `internal_putbytes()`
- `src/backend/libpq/pqcomm.c:1488` - `socket_putmessage()`
- `src/backend/libpq/pqcomm.c:1358` - `internal_flush_buffer()`
- `src/backend/libpq/be-secure.c:315` - `secure_write()`

## Current behavior / grounded findings

### Current timing split in this path

The current instrumentation in `printtup()` now distinguishes three levels:

- `Printtup` at `src/backend/access/common/printtup.c:317` is still the coarse parent for one output row.
- `Printtup_Ser` at `src/backend/access/common/printtup.c:323` covers row serialization up to the completed `StringInfo` message buffer.
- `Printtup_Net` at `src/backend/access/common/printtup.c:395` covers the handoff into backend transport through `pq_endmessage_reuse()`.
- `PQ_putmessage` at `src/backend/libpq/pqcomm.c:1502` is the lower transport-staging leaf inside that handoff.
- `PQ_putmessage` now pauses around forced flushes in `internal_putbytes()`, and `PG_BE_SOCK_WRITE` in `secure_write()` now pauses around blocked wait. That makes staging, active socket-write execution, and blocked wait separable instead of nested.

This lets reports separate row serialization, protocol message staging, and lower socket-write execution more clearly than before.

### High-level ownership split

The remote-result path intentionally uses different contexts and buffers for different lifetimes:

- receiver object: created in `MessageContext` in `src/backend/tcop/postgres.c:2360`
- portal execution state: runs in `PortalContext` after `src/backend/tcop/pquery.c:746`
- executor per-query state: `es_query_cxt`, created in `src/backend/executor/execUtils.c:97` and made current in `src/backend/executor/execMain.c:333`
- reusable row message buffer `myState->buf`: initialized in `src/backend/access/common/printtup.c:545`, therefore allocated in `es_query_cxt`
- per-row scratch arena `myState->tmpcontext`: created under the then-current context in `src/backend/access/common/printtup.c:554`, so it is a child of `es_query_cxt`
- backend transport/send buffer `PqSendBuffer`: allocated once in `TopMemoryContext` in `src/backend/libpq/pqcomm.c:279`

The practical consequence is that the receiver object, row assembly buffer, per-row scratch allocations, and transport buffer do not share one lifetime or one allocation domain.

### `slot_getallattrs()` does not serialize the row

`slot_getallattrs()` in `src/include/executor/tuptable.h:367` only guarantees that `tts_values[]` and `tts_isnull[]` are valid for all user columns.

For physical tuple slots:

- `slot_getsomeattrs_int()` in `src/backend/executor/execTuples.c:1990` dispatches to the slot implementation
- `slot_deform_heap_tuple()` in `src/backend/executor/execTuples.c:1010` walks the physical tuple layout
- `values[attnum] = fetchatt(...)` in `src/backend/executor/execTuples.c:1090` stores each attribute as a `Datum`

`fetchatt()` in `src/include/access/tupmacs.h:46` is the key:

- by-value attributes copy the value bits into the `Datum`
- by-reference attributes store a pointer `Datum` pointing into the tuple storage

So after `slot_getallattrs()`, many columns are still backend-internal representations or pointers into tuple memory. They are not yet FE/BE protocol bytes.

### `Datum` and `bytea` on this path

- `Datum` is the generic PostgreSQL value carrier, typedefed as `uintptr_t` in `src/include/postgres.h:64`
- by-value types use the word itself as the value, e.g. `Int32GetDatum()` / `DatumGetInt32()` in `src/include/postgres.h:198` and `:208`
- by-reference types store a pointer, e.g. `DatumGetPointer()` / `PointerGetDatum()` in `src/include/postgres.h:308` and `:318`
- `bytea` is a concrete variable-length data type, typedefed to `struct varlena` in `src/include/c.h:699`

`SendFunctionCall()` returns a `bytea *` in `src/backend/utils/fmgr/fmgr.c:1743`, which is why `printtup` later uses `VARSIZE(outputbytes) - VARHDRSZ` and `VARDATA(outputbytes)` in `src/backend/access/common/printtup.c:824`.

### `slot_getallattrs()` does not automatically detoast

The slot deformation path only exposes each column as a `Datum`. Detoasting happens later only if some caller explicitly requests it, such as:

- `PG_DETOAST_DATUM(...)` in `src/include/fmgr.h:240`
- `DatumGetByteaPP(...)` in `src/include/fmgr.h:291`
- `DatumGetByteaP(...)` in `src/include/fmgr.h:331`

That is why a toasted varlena may still be represented indirectly in the slot and only become fully copied/flattened inside a type output/send function or helper such as `PrinttupBinaryDumpNormalizeDatumBytes(...)` in this branch.

## Architecture / data flow

### Entry points

1. `CreateDestReceiver()` in `src/backend/tcop/dest.c:113` picks `printtup_create_DR()`.
2. `PostgresMain` / `exec_execute_message` creates the receiver in `MessageContext` in `src/backend/tcop/postgres.c:2360`.
3. `PortalRun()` in `src/backend/tcop/pquery.c:686` makes `PortalContext` current at `:746`.
4. `CreateExecutorState()` in `src/backend/executor/execUtils.c:97` creates `es_query_cxt` under the current context.
5. `standard_ExecutorRun()` switches into `es_query_cxt` at `src/backend/executor/execMain.c:333`.
6. `dest->rStartup()` runs `printtup_startup()` at `src/backend/executor/execMain.c:354`.
7. Each output tuple reaches `printtup()` via `dest->receiveSlot` at `src/backend/executor/execMain.c:1679`.

### Control flow for one tuple

1. `printtup()` in `src/backend/access/common/printtup.c:746` ensures per-attribute lookup state with `printtup_prepare_info()`.
2. `slot_getallattrs(slot)` in `src/backend/access/common/printtup.c:766` makes all `Datum` values available.
3. `MemoryContextSwitchTo(myState->tmpcontext)` in `src/backend/access/common/printtup.c:769` moves current allocations into the per-row scratch context.
4. `pq_beginmessage_reuse(buf, PqMsg_DataRow)` in `src/backend/access/common/printtup.c:780` resets the reusable row buffer.
5. For each attribute:
   - text format: `OutputFunctionCall(...)` in `src/backend/access/common/printtup.c:814`, then `pq_sendcountedtext(...)` in `:815`
   - binary format: `SendFunctionCall(...)` in `src/backend/access/common/printtup.c:823`, then `pq_sendint32(...)` and `pq_sendbytes(...)` in `:830` and `:831`
6. `pq_endmessage_reuse(buf)` in `src/backend/access/common/printtup.c:844` hands the completed message to libpq backend transport.
7. `MemoryContextSwitchTo(oldcontext)` and `MemoryContextReset(myState->tmpcontext)` in `src/backend/access/common/printtup.c:848` and `:850` release per-row scratch allocations.

Timing mapping onto those steps:

- steps 1-5 are primarily `Printtup_Ser`
- step 6 is `Printtup_Net`, with lower transport staging in `PQ_putmessage`
- later socket execution is still visible separately through `PG_BE_SOCK_WRITE` in `secure_write()`
- blocked backend wait is no longer counted inside `PG_BE_SOCK_WRITE`; it remains only in `PG_WAIT`

### Data structures

- `TupleTableSlot` keeps `tts_values[]` and `tts_isnull[]` in `src/include/executor/tuptable.h:114`
- `StringInfoData` in `src/include/lib/stringinfo.h:46` is the reusable contiguous row-message buffer abstraction
- `DR_printtup` owns:
  - `buf` for row assembly
  - `tmpcontext` for per-row scratch
  - `myinfo` for attribute serializer lookup state
- `PqSendBuffer` in `src/backend/libpq/pqcomm.c:279` is the backend-global output transport buffer

### State / lifecycle

The actual lifetime split in the current code is:

- receiver struct: survives for the message / portal execution setup, then `printtup_destroy()` frees it in `src/backend/access/common/printtup.c:888`
- `myState->buf.data`: allocated once in `printtup_startup()` via `initStringInfo()` (`src/backend/access/common/printtup.c:545`, `src/common/stringinfo.c:59`) and reused until `printtup_shutdown()` frees it in `src/backend/access/common/printtup.c:873`
- `myState->tmpcontext`: reused per row and reset after each row in `src/backend/access/common/printtup.c:850`, finally deleted in `src/backend/access/common/printtup.c:879`
- `PqSendBuffer`: process-lifetime transport staging buffer allocated once in `src/backend/libpq/pqcomm.c:279`

## Behavior details

### Why `tmpcontext` exists

The per-row temporary context is not needed because the final row message needs a separate home. The final contiguous `DataRow` message is assembled in `myState->buf`.

`tmpcontext` exists because the current type I/O interfaces are value-returning and may allocate internally:

- `OutputFunctionCall()` in `src/backend/utils/fmgr/fmgr.c:1682` returns a `char *`
- `SendFunctionCall()` in `src/backend/utils/fmgr/fmgr.c:1743` returns a `bytea *`
- `pq_sendcountedtext()` in `src/backend/libpq/pqformat.c:141` may call `pg_server_to_client()` in `src/backend/utils/mb/mbutils.c:738`
- `perform_default_encoding_conversion()` allocates its conversion buffer in `CurrentMemoryContext` in `src/backend/utils/mb/mbutils.c:818`

Without the temporary context switch in `src/backend/access/common/printtup.c:769`, those hidden per-row allocations would accumulate in `es_query_cxt` for the whole query, and leaky type output/send functions would leak once per attribute per row.

### What the temporary allocations are

On the normal row-send path, the main per-row temporary allocations are:

- text output strings returned by `OutputFunctionCall(...)`
- binary `bytea *` results returned by `SendFunctionCall(...)`
- encoding-conversion buffers from `pg_server_to_client(...)` when client encoding differs
- in this branch, optional `serializedLengths` / `serializedValues` arrays for the binary-dump side path in `src/backend/access/common/printtup.c:773` and `:774`
- in this branch, optional detoasted or normalized copies used by `PrinttupBinaryDumpNormalizeDatumBytes(...)`

The reusable `myState->buf` is different: it is not per-row scratch output from type functions, it is the assembled contiguous FE/BE protocol message for the row.

### How `StringInfo` reuse works here

`pq_beginmessage_reuse()` in `src/backend/libpq/pqformat.c:109` calls `resetStringInfo()` in `src/common/stringinfo.c:78`, which clears length and cursor but keeps the existing buffer.

If a later row is larger than current capacity:

- `appendBinaryStringInfo(...)` in `src/common/stringinfo.c:233` grows the buffer via `enlargeStringInfo()`
- `enlargeStringInfo()` uses `repalloc` and explicitly documents at `src/common/stringinfo.c:283` that the buffer stays owned by the context that was current when `initStringInfo()` was called

This is why `myState->buf` can be safely appended to while `CurrentMemoryContext` is `tmpcontext`: the buffer itself remains owned by `es_query_cxt`.

### End-to-end byte movement for one tuple

The current path is usually:

1. backend tuple bytes / slot state
   - `slot_getallattrs()` exposes each column as a `Datum`
2. per-attribute serialization result
   - text path produces a temporary C string
   - binary path produces a temporary `bytea *`
3. row assembly
   - `pq_sendint16`, `pq_sendint32`, `pq_sendbytes`, and `pq_sendcountedtext` append into `myState->buf`
4. transport staging
   - `pq_endmessage_reuse()` calls `pq_putmessage(...)` via `src/backend/libpq/pqformat.c:314`
   - `socket_putmessage()` in `src/backend/libpq/pqcomm.c:1488` writes type byte, length word, and payload through `internal_putbytes()`
5. socket write
   - `internal_putbytes()` in `src/backend/libpq/pqcomm.c:1275` either copies into `PqSendBuffer` or sends large remaining payload directly if the send buffer is empty and the remaining chunk is larger than the buffer (`src/backend/libpq/pqcomm.c:1293`)
   - `internal_flush_buffer()` in `src/backend/libpq/pqcomm.c:1358` drives `secure_write()` in `src/backend/libpq/be-secure.c:315`

### What "direct send" actually means

The backend transport path does have a partial bypass optimization in `internal_putbytes()`:

- if the send buffer is empty
- and the remaining chunk length is at least `PqSendBufferSize`

then that remaining chunk can be flushed directly from the source pointer without first copying into `PqSendBuffer` (`src/backend/libpq/pqcomm.c:1293`).

But for a normal `pq_putmessage()` call, the message type byte and 4-byte length header are still pushed first through `socket_putmessage()` in `src/backend/libpq/pqcomm.c:1497` and `:1501`. So this is not "the whole message skips the send buffer from the start". It is an opportunistic partial bypass of a later large body segment.

## Invariants and assumptions

- `myState->buf` must live in a context that outlives one row; `pq_beginmessage_reuse()` explicitly requires a sufficiently long-lived buffer in `src/backend/libpq/pqformat.c:104`.
- `tmpcontext` must be reset only after `pq_endmessage_reuse()` has handed off the row, because text/binary temporaries are still needed while appending bytes to `myState->buf`.
- `SendFunctionCall()` guarantees a non-toasted `bytea *` result in `src/backend/utils/fmgr/fmgr.c:1739`.
- `slot_getallattrs()` guarantees valid `tts_values[]` / `tts_isnull[]`, not client-wire-ready bytes.

## Configuration / flags

- Text-vs-binary choice per attribute comes from `portal->formats` via `printtup_prepare_info()` in `src/backend/access/common/printtup.c:678` and `:697`.
- Client encoding conversion can add extra per-row allocation through `pg_server_to_client()` in `src/backend/utils/mb/mbutils.c:738`.

## Interactions and cross-links

- **Overlapping KB areas**: `../memory-contexts/memory_contexts_and_allocation.md` is the canonical background for the context semantics used here.
- **Shared code / canonical docs**: `../libpq-io/socket_wait_and_io.md` is the canonical note for the lower-level wait and backend/frontend socket instrumentation once bytes are being flushed.

## Future directions / design ideas

- **Status**: `exploratory`
- **Goal**: reduce per-attribute temporaries and row/message copies without breaking FE/BE protocol correctness
- **Grounding in current code**: this path uses value-returning type serializers and a separate transport staging buffer
- **Candidate approaches**: kept in `../../future-directions/postgres/serialization/printtup_send_path_future_directions.md`
- **Risks / tradeoffs**: changes here affect type I/O APIs, extensions, FE/BE protocol framing, and backpressure behavior
- **Next experiments / implementation steps**: see `../../future-directions/postgres/serialization/printtup_send_path_future_directions.md`

## Gotchas

- `myState->buf` is not the per-row scratch arena; `tmpcontext` is.
- `slot_getallattrs()` is not equivalent to detoasting or serializing.
- The current path is not zero-copy. For most rows it is at least:
  - `Datum` to temporary string/`bytea`
  - temporary string/`bytea` to `myState->buf`
  - `myState->buf` to `PqSendBuffer`
  - `PqSendBuffer` to kernel/TLS write
- This branch includes extra binary-dump bookkeeping in `printtup.c`, so measured allocations here are slightly more than upstream `printtup`.

## Open questions / TODO

- Measure how much of `printtup` cost in this branch comes from:
  - per-attribute type output/send allocation
  - encoding conversion
  - row assembly copying into `myState->buf`
  - `PqSendBuffer` copying and flush behavior
- Add a concrete worked example later for one fixed-width by-value column and one toasted varlena column if profiling work needs it.
