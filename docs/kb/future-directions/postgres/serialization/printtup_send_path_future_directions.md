# `printtup` send-path future directions

## Scope

- **What this doc explains**: The design space we discussed for reducing temporary buffers and copies on the `printtup` and backend send path.
- **What this doc does NOT cover**: A committed implementation plan or benchmark-backed choice.
- **Primary directory**: `docs/kb/future-directions/postgres/serialization/`

## Why this exists (context)

This repo is being used for systems research and rapid prototyping, so the KB needs to preserve not only the current behavior but also the design ideas that emerged from analysis. The user explicitly wanted the copy-reduction discussion and proposed directions recorded as part of the KB.

The grounding doc is [`printtup_row_serialization_and_transport.md`](../../../postgres/serialization/printtup_row_serialization_and_transport.md). This note starts from that established behavior and records what looks technically possible, what is invasive, and what should be measured first.

## Key code pointers

- `src/backend/access/common/printtup.c:769` - current per-row scratch context switch
- `src/backend/access/common/printtup.c:780` - reusable `DataRow` assembly buffer
- `src/backend/access/common/printtup.c:814` - text output path currently returns a temporary `char *`
- `src/backend/access/common/printtup.c:823` - binary output path currently returns a temporary `bytea *`
- `src/backend/utils/fmgr/fmgr.c:1682` - `OutputFunctionCall()` API shape
- `src/backend/utils/fmgr/fmgr.c:1743` - `SendFunctionCall()` API shape
- `src/backend/libpq/pqformat.c:141` - `pq_sendcountedtext()` copies bytes into the row buffer and may allocate conversion output
- `src/backend/libpq/pqcomm.c:1275` - transport-side copy/bypass decision in `internal_putbytes()`
- `src/backend/libpq/pqcomm.c:1488` - FE/BE message framing boundary in `socket_putmessage()`
- `src/backend/libpq/pqcomm.c:1521` - nonblocking path can enlarge `PqSendBuffer`
- `src/common/stringinfo.c:283` - row buffer remains tied to original context despite appends while another context is current

## Current behavior / grounded findings

The current steady-state copy chain is usually:

1. `Datum` to a temporary per-attribute text string or `bytea *`
2. per-attribute temporary to contiguous row payload in `myState->buf`
3. `myState->buf` payload to `PqSendBuffer`
4. `PqSendBuffer` to socket/TLS write

The two dominant structural reasons are:

- type serializers are currently value-returning (`char *` / `bytea *`) rather than append/stream based
- transport framing is separated from row assembly, with `socket_putmessage()` owning the message type and length header write

So yes, some current copying is policy, not physical necessity. But it is also bound up with long-standing interfaces and protocol framing rules.

## Architecture / data flow

### Current constraints that any redesign must respect

- FE/BE protocol messages require a message type byte and a 4-byte length word before the payload (`src/backend/libpq/pqcomm.c:1470`)
- blocking `socket_putmessage()` currently uses the fixed `PqSendBuffer` / direct-flush behavior of `internal_putbytes()`
- type send/output functions can allocate internally and may rely on `CurrentMemoryContext`
- extension-defined type I/O functions participate in the same API contract as built-in types

## Behavior details

### Direction 1: add direct-append fast paths for selected hot types

- **Status**: `exploratory`
- **Goal**: reduce temporary `char *` / `bytea *` creation for a targeted subset of common types without redesigning the general API
- **Grounding in current code**: `printtup()` already branches per attribute format in `src/backend/access/common/printtup.c:809` and `:817`, so type-specific fast paths could be inserted there
- **Candidate approaches**:
  - add direct append helpers for fixed-width binary types where the external representation is straightforward
  - optionally add text fast paths for a few numeric types if the formatting cost and compatibility story are clear
  - fall back to `OutputFunctionCall()` / `SendFunctionCall()` for everything else
- **Risks / tradeoffs**:
  - code duplication against type I/O routines
  - correctness burden for endianness, protocol format, and type-specific corner cases
  - only helps selected workloads
- **Next experiments / implementation steps**:
  - profile which output types dominate row-send cost
  - prototype one or two binary fixed-width types first, because those are easier to reason about than general text formatting

This is the lowest-risk performance experiment because it does not require changing extension-visible type I/O APIs.

### Direction 2: introduce streaming/append-oriented type serializers

- **Status**: `exploratory`
- **Goal**: remove the intermediate per-attribute `char *` / `bytea *` allocation model entirely for supported serializers
- **Grounding in current code**:
  - `OutputFunctionCall()` in `src/backend/utils/fmgr/fmgr.c:1682` returns a `char *`
  - `SendFunctionCall()` in `src/backend/utils/fmgr/fmgr.c:1743` returns a `bytea *`
  - `printtup` then copies those temporary results into `myState->buf`
- **Candidate approaches**:
  - define new append-oriented APIs that write directly into a `StringInfo`
  - reserve 4 bytes for the field length, serialize into the buffer, and backpatch the actual field length after serialization completes
  - allow type functions to return length-only metadata first if backpatching is undesirable
- **Risks / tradeoffs**:
  - invasive API change across built-in types and extensions
  - current code assumes value-returning semantics in many places beyond `printtup`
  - text encoding conversion still complicates direct append unless conversion itself becomes append-oriented
- **Next experiments / implementation steps**:
  - inventory which built-in send/output functions are on the hot path for target workloads
  - decide whether a dual API is acceptable: old value-returning API plus new append API

This direction offers the cleanest conceptual removal of `tmpcontext` pressure for serialization intermediates, but it is the most invasive.

### Direction 3: build FE/BE messages directly in the transport buffer

- **Status**: `exploratory`
- **Goal**: remove or shrink the `myState->buf` to `PqSendBuffer` copy
- **Grounding in current code**:
  - `printtup` currently assembles a contiguous row in `myState->buf`
  - `socket_putmessage()` then frames the message and pushes it through `internal_putbytes()`
- **Candidate approaches**:
  - reserve header space in `PqSendBuffer`, serialize the row payload directly behind it, then backpatch the message length before flush
  - pre-grow `PqSendBuffer` for the whole message, similar in spirit to `socket_putmessage_noblock()` in `src/backend/libpq/pqcomm.c:1521`
  - redesign transport to support gather/scatter output so the header and payload do not have to be physically contiguous in one staging buffer
- **Risks / tradeoffs**:
  - the current blocking send path does not simply "own a growable row buffer"; it owns a batching transport buffer with partial flush semantics
  - once flushing begins, header backpatching becomes unsafe
  - transport code complexity rises quickly if serialization and batching are fused
  - this still does not remove per-attribute temporary allocations if type APIs remain value-returning
- **Next experiments / implementation steps**:
  - measure how often rows are large enough for the existing `internal_putbytes()` direct-send path to matter
  - prototype a transport-side reserved-header/backpatch design only after quantifying the row-buffer to send-buffer copy cost

This direction is technically possible, but it should not be the first experiment. It is structurally coupled to FE/BE framing and flush/backpressure behavior.

### Direction 4: combine directions selectively rather than chasing a full zero-copy design

- **Status**: `recommended`
- **Goal**: improve the hot path without destabilizing the control path or the extension surface
- **Grounding in current code**: both temporary type-output allocation and row/transport copying contribute cost, but they have different engineering blast radii
- **Candidate approaches**:
  - first attack per-attribute temporary creation for a narrow set of hot types
  - keep `tmpcontext` and transport separation initially
  - only revisit send-buffer fusion if measurement shows the `myState->buf` to `PqSendBuffer` copy is material after serializer improvements
- **Risks / tradeoffs**:
  - may leave some copy costs in place
  - optimization becomes workload-specific
- **Next experiments / implementation steps**:
  - benchmark before/after for selected result schemas
  - separate metrics for type I/O allocation count, bytes copied into `myState->buf`, bytes copied into `PqSendBuffer`, and flush/write sizes

This is the most pragmatic sequencing given the current code and the extension compatibility cost of deeper API changes.

## Invariants and assumptions

- Removing `tmpcontext` outright is not a safe local cleanup while `OutputFunctionCall()` / `SendFunctionCall()` remain value-returning and arbitrary type code may leak or allocate.
- Removing `myState->buf` is not just a serialization cleanup; it changes the boundary between row assembly and transport framing.
- "Direct to socket" is not equivalent to "better" unless batching, partial flush, and protocol header handling are still correct.

## Configuration / flags

- No dedicated configuration exists for these design directions today.
- Any future prototype should consider adding instrumentation toggles rather than silently changing the hot path.

## Interactions and cross-links

- **Grounded behavior**: [`printtup_row_serialization_and_transport.md`](../../../postgres/serialization/printtup_row_serialization_and_transport.md)
- **Socket flush/wait behavior**: [`socket_wait_and_io.md`](../../../postgres/libpq-io/socket_wait_and_io.md)

## Gotchas

- The current code can partially bypass `PqSendBuffer` for large remaining payload chunks, but that is not the same thing as eliminating the row assembly buffer.
- A fully general zero-copy story is unlikely without either changing type I/O APIs or using scatter/gather transport.
- For this branch specifically, extra binary-dump code in `printtup.c` can distort measurements if not disabled.

## Open questions / TODO

- Quantify, for representative workloads, whether per-attribute temporary allocation or row-to-transport copying is the bigger bottleneck.
- Investigate whether a binary-only fast path is enough to cover the target research workloads.
- Decide whether extension compatibility matters for the intended prototype, or whether an internal-only experimental serializer API is acceptable.
