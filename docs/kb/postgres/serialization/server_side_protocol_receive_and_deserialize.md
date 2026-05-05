# PostgreSQL server-side protocol receive and deserialize

## Scope

- **What this doc explains**: Server-side receive/deserialization for frontend protocol messages, with emphasis on memory contexts, message buffers, parsing in place, and where copies happen.
- **What this doc does NOT cover**: Frontend/libpq client result handling, COPY FROM row parsing internals, or executor expression evaluation.
- **Primary directory**: `docs/kb/postgres/serialization/`

## Why this exists (context)

The send-side `printtup` path is easy to picture because it explicitly builds a `DataRow` buffer and pushes it through backend libpq transport. The receive side is less obvious because it often does not build an equivalent extra staging buffer per field or per row.

The important conclusion is that server-side receive is not a mirror image of send:

- send must materialize FE/BE protocol bytes from backend-internal objects
- receive already has FE/BE protocol bytes and therefore mostly frames and interprets them in place

With the current instrumentation, the receive-side execution is now split into additive lower leaves:

- `PG_BE_SOCK_READ` for active socket recv/copy execution in `secure_read()` (`src/backend/libpq/be-secure.c:176`)
- `PQ_getbyte` for FE/BE message-type byte consumption in `pq_getbyte()` (`src/backend/libpq/pqcomm.c:963`)
- `PQ_getmessage` for receive-side protocol framing / `StringInfo` management in `pq_getmessage()` (`src/backend/libpq/pqcomm.c:1214`)
- `PQ_getbytes` for draining/copying already-buffered bytes out of `PqRecvBuffer` (`src/backend/libpq/pqcomm.c:1062`)

This doc is the canonical reference for the backend receive half, especially the `SocketBackend()` -> `pq_getmessage()` -> `input_message` -> `pq_getmsg*()` -> `exec_bind_message()` path.

## Key code pointers

- `src/backend/tcop/postgres.c:385` - `SocketBackend()`
- `src/backend/tcop/postgres.c:499` - whole-message receive into `input_message`
- `src/backend/tcop/postgres.c:4750` - `MessageContext` reset and `input_message` creation
- `src/backend/libpq/pqcomm.c:126` - static backend receive buffer `PqRecvBuffer`
- `src/backend/libpq/pqcomm.c:897` - `pq_recvbuf()`
- `src/backend/libpq/pqcomm.c:904` - compaction with `memmove`
- `src/backend/libpq/pqcomm.c:963` - `pq_getbyte()`
- `src/backend/libpq/pqcomm.c:1202` - `pq_getmessage()`
- `src/backend/libpq/pqcomm.c:1238` - enlarge `StringInfo` for message body
- `src/backend/libpq/pqcomm.c:1256` - copy message body from `PqRecvBuffer` into destination `StringInfo`
- `src/backend/libpq/pqformat.c:507` - `pq_getmsgbytes()`
- `src/backend/libpq/pqformat.c:545` - `pq_getmsgtext()`
- `src/backend/libpq/pqformat.c:578` - `pq_getmsgstring()`
- `src/backend/libpq/pqformat.c:607` - `pq_getmsgrawstring()`
- `src/backend/tcop/postgres.c:1802` - `exec_bind_message()`
- `src/backend/tcop/postgres.c:1943` - switch into `portal->portalContext`
- `src/backend/tcop/postgres.c:2018` - binary/text parameter bytes taken as a slice of `input_message`
- `src/backend/tcop/postgres.c:2021` - `initReadOnlyStringInfo(&pbuf, ...)`
- `src/backend/tcop/postgres.c:2051` - text parameter client->server conversion
- `src/backend/tcop/postgres.c:2115` - binary parameter receive function call
- `src/backend/utils/mb/mbutils.c:681` - `pg_client_to_server()` can return original pointer when no conversion is needed
- `src/backend/utils/mb/mbutils.c:818` - client->server conversion allocation path
- `src/backend/nodes/params.c:43` - `makeParamList()`

## Current behavior / grounded findings

- Backend socket receive uses a fixed process-level transport buffer, `PqRecvBuffer`, not a memory-context-allocated send-style `StringInfo`.
- `pq_getmessage()` copies one complete FE/BE message body out of `PqRecvBuffer` into a caller-supplied `StringInfo`. In the main loop that destination is `input_message` in `MessageContext`.
- After that whole-message copy, many higher-level parsers avoid further copying:
  - `pq_getmsgbytes()` returns a pointer directly into `input_message`
  - `pq_getmsgrawstring()` returns a pointer directly into `input_message`
  - `pq_getmsgstring()` returns either a direct pointer or an encoding-converted allocation
  - `pq_getmsgtext()` always allocates a fresh string
- `exec_bind_message()` is the clearest typed deserialization example:
  - final bound parameters are allocated in `portal->portalContext`
  - binary parameters are wrapped as read-only `StringInfo` views into `input_message`
  - text parameters may allocate conversion buffers before invoking typinput functions

So the receive path normally has one unavoidable whole-message copy plus selective additional allocation/copy only when text conversion or type input functions require it.

## Architecture / data flow

### Entry points

1. `PostgresMain` clears `MessageContext` and creates `input_message` in `src/backend/tcop/postgres.c:4750`.
2. `ReadCommand()` calls `SocketBackend()` in `src/backend/tcop/postgres.c:514`.
3. `SocketBackend()` begins message reading with `pq_startmsgread()`, reads the type byte with `pq_getbyte()`, and then calls `pq_getmessage(inBuf, maxmsglen)` at `src/backend/tcop/postgres.c:395` and `:499`.
4. Protocol-specific handlers such as `exec_bind_message()` consume fields from `input_message` using `pq_getmsg*()` helpers.

### Buffer/lifetime chain

- transport input buffer: `PqRecvBuffer` in `src/backend/libpq/pqcomm.c:126`
- per-message parsed buffer: `input_message`, a `StringInfo` created in `MessageContext` at `src/backend/tcop/postgres.c:4753`
- longer-lived deserialized state:
  - parse trees / planning scratch often remain in `MessageContext`
  - portal-bound parameter state is copied/allocated in `portal->portalContext` starting at `src/backend/tcop/postgres.c:1943`

### Copy chain

The common server receive chain is:

1. socket/TLS -> `PqRecvBuffer`
2. `PqRecvBuffer` -> `input_message` via `pq_getmessage()`
3. `input_message` -> direct field views where possible
4. direct field views -> final backend objects only if lifetime/type conversion requires it

That is materially different from `printtup`, where the path is dominated by constructing a new protocol message from backend objects and then copying that message into the transport buffer.

## Behavior details

### Transport framing and whole-message copy

`pq_recvbuf()` in `src/backend/libpq/pqcomm.c:897` keeps the transport buffer dense by shifting unread bytes left with `memmove` at `:904`. `pq_getmessage()` then:

- resets the destination `StringInfo` at `src/backend/libpq/pqcomm.c:1208`
- reads the 4-byte length at `:1211`
- enlarges the destination `StringInfo` for the body at `:1240`
- copies the full body with `pq_getbytes(s->data, len)` at `:1256`

So backend protocol receive does pay one whole-message copy up front.

Under the current timing layout, that whole-message receive path decomposes as:

1. `PG_BE_SOCK_READ` for the actual socket refill via `secure_read()`
2. `PQ_getbyte` for the initial protocol message-type byte
3. `PQ_getmessage` for length decoding, `StringInfo` management, and framing
4. `PQ_getbytes` for the buffered-byte copies from `PqRecvBuffer` into the destination message buffer

### Parsing directly from `input_message`

Once the body is in `input_message`, helper behavior diverges:

- `pq_getmsgbytes()` in `src/backend/libpq/pqformat.c:507` returns a pointer into the existing message body
- `pq_copymsgbytes()` in `src/backend/libpq/pqformat.c:527` copies into caller memory
- `pq_getmsgrawstring()` in `src/backend/libpq/pqformat.c:607` returns a direct null-terminated slice in the message buffer
- `pq_getmsgstring()` in `src/backend/libpq/pqformat.c:578` may stay zero-copy if `pg_client_to_server()` returns the same pointer

This is why the receive side usually needs fewer staging buffers than the send side.

### `Bind` is the typed receive analogue

`exec_bind_message()` in `src/backend/tcop/postgres.c:1802` is the closest analogue to “deserialize wire data into backend typed objects”.

Important lifetime split:

- the message itself is in `MessageContext`
- the final bound parameter state is allocated after switching to `portal->portalContext` at `src/backend/tcop/postgres.c:1943`

For each parameter:

- length is read from `input_message` at `src/backend/tcop/postgres.c:2004`
- raw payload is exposed as a pointer into `input_message` via `pq_getmsgbytes()` at `:2018`
- a read-only `StringInfo` view is built at `:2021`

Then:

- text mode:
  - `pg_client_to_server()` at `src/backend/tcop/postgres.c:2051` may allocate a converted string or return the original pointer
  - `OidInputFunctionCall()` at `src/backend/tcop/postgres.c:2056` produces the final backend `Datum`
- binary mode:
  - `OidReceiveFunctionCall()` at `src/backend/tcop/postgres.c:2115` consumes directly from the `StringInfo` view
  - there is no per-parameter contiguous re-staging buffer before the type receive function

### Why this is not a `printtup` mirror

`printtup` needs:

- type output/send temporaries
- a row-assembly `StringInfo`
- then transport buffering

Receive instead needs:

- transport buffering
- one whole-message extraction into `input_message`
- then mostly in-place parsing plus final-object construction

So the hot-path shapes are asymmetric even though both ultimately use FE/BE protocol framing.

## Invariants and assumptions

- `input_message` is per-message and lives in `MessageContext`; callers must not keep raw pointers into it beyond that lifetime unless they copy them elsewhere.
- `portal->portalContext` owns bound parameters that must survive beyond the current message loop iteration.
- Binary parameter receive is only “zero-copy” up to the type receive function boundary; the final backend `Datum` may still allocate depending on type representation.

## Interactions and cross-links

- **Overlapping KB areas**: `printtup_row_serialization_and_transport.md` is the canonical send-side counterpart.
- **Shared code / canonical docs**: `../memory-contexts/memory_contexts_and_allocation.md` explains why `MessageContext` vs `portal->portalContext` matters here.
- **Related Citus usage**: Citus uses this raw backend receive stack for some local COPY protocol handlers, but uses frontend libpq-in-backend for inter-node traffic.

## Future directions / design ideas

- **Status**: `implemented` for documentation only
- **Goal**: keep the send/receive asymmetry explicit so future copy-reduction work does not assume the two directions have the same buffer structure
- **Grounding in current code**: receive performs one whole-message copy plus selective field conversion/copy
- **Candidate approaches**: if receive-side optimizations matter, the likely target is avoiding or shrinking the `PqRecvBuffer` -> `input_message` copy for selected message classes
- **Risks / tradeoffs**: tighter coupling to protocol framing and message-lifetime rules
- **Next experiments / implementation steps**: compare this path with COPY FROM and with Citus raw COPY receive helpers before changing core message handling

## Gotchas

- `exec_bind_message()` is typed deserialization, but `SocketBackend()` itself is just protocol framing.
- `pq_getmsgstring()` is not guaranteed to be zero-copy; encoding conversion can allocate.
- This note is about the backend as a server receiving FE/BE messages, not about frontend libpq receiving query results.

## Open questions / TODO

- Add a sibling note for backend COPY FROM protocol receive if later work shifts from `Bind` toward bulk ingestion.
- If future work measures protocol receive overhead directly, separate transport-copy cost from type-input cost.
