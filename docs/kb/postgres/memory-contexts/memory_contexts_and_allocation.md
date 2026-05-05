# PostgreSQL memory contexts and allocation

## Scope

- **What this doc explains**: Backend memory-context lifecycle, the `palloc`/`MemoryContext` API, `AllocSet` reuse behavior, and the concrete contexts involved in the remote tuple-send path.
- **What this doc does NOT cover**: Frontend `libpq` memory management, shared-memory allocators, or every specialized backend context implementation.
- **Primary directory**: `docs/kb/postgres/memory-contexts/`

## Why this exists (context)

The user question started from the common shorthand that PostgreSQL uses "scope-like" memory management rather than raw `malloc`/`free`. That shorthand is directionally right, but it hides important details:

- backend code normally uses `palloc` and `CurrentMemoryContext`, not `pg_malloc`
- a memory context is an explicit runtime object, not a C lexical scope
- resetting a context is the primary cleanup primitive, and allocator reuse depends on the context implementation
- the networking/serialization path (`printtup`) relies on several different contexts with different lifetimes

This doc captures the grounded behavior so later optimization notes can refer back to one canonical description.

## Key code pointers

- `src/backend/utils/mmgr/README:9` - design overview and the stated purpose of memory contexts
- `src/backend/utils/mmgr/README:36` - `CurrentMemoryContext` and `MemoryContextSwitchTo()`
- `src/backend/utils/mmgr/README:42` - why bulk reset/delete is the main advantage over raw `malloc`/`free`
- `src/backend/utils/mmgr/README:99` - `pfree`/`repalloc` do not depend on `CurrentMemoryContext`
- `src/backend/utils/mmgr/README:115` - forest-in-principle note; current practice is effectively rooted at `TopMemoryContext`
- `src/backend/utils/mmgr/README:185` - `TopMemoryContext`
- `src/backend/utils/mmgr/README:214` - `MessageContext`
- `src/backend/utils/mmgr/README:248` - `PortalContext`
- `src/backend/utils/mmgr/README:299` - executor top context is a child of `PortalContext`
- `src/backend/utils/mmgr/mcxt.c:339` - `MemoryContextInit()`
- `src/backend/utils/mmgr/mcxt.c:383` - `MemoryContextReset()`
- `src/backend/utils/mmgr/mcxt.c:454` - `MemoryContextDelete()`
- `src/backend/utils/mmgr/mcxt.c:1100` - `MemoryContextCreate()`
- `src/backend/utils/mmgr/mcxt.c:1180` - `MemoryContextAlloc()`
- `src/backend/utils/mmgr/mcxt.c:1315` - `palloc()`
- `src/include/utils/palloc.h:123` - inline `MemoryContextSwitchTo()`
- `src/backend/utils/mmgr/aset.c:17` - `AllocSet` overview comment
- `src/backend/utils/mmgr/aset.c:55` - chunk freelists and large-allocation split
- `src/backend/utils/mmgr/aset.c:537` - `AllocSetReset()` keeper-block reuse behavior
- `src/backend/utils/mmgr/aset.c:696` - dedicated large-allocation path
- `src/include/common/fe_memutils.h:22` and `src/common/fe_memutils.c:47` - `pg_malloc()` is a frontend/common wrapper, not the backend allocation API

## Current behavior / grounded findings

- Backend code normally allocates with `palloc()` in `src/backend/utils/mmgr/mcxt.c:1315`, which routes the allocation to `CurrentMemoryContext`.
- `MemoryContextSwitchTo()` in `src/include/utils/palloc.h:123` is intentionally tiny: it swaps `CurrentMemoryContext` and returns the previous context so callers can restore it.
- The API is explicit: create a context, switch into it if needed, allocate with `palloc`, later `MemoryContextReset()` or `MemoryContextDelete()` it.
- The main purpose is lifetime-based cleanup, not merely faster allocation. `src/backend/utils/mmgr/README:42` states the win directly: whole groups of transient allocations can be reclaimed in bulk at transaction end, query end, or tuple end.
- In theory contexts form a forest (`src/backend/utils/mmgr/README:115`), but in current backend practice they are effectively rooted at `TopMemoryContext` (`src/backend/utils/mmgr/README:185`, `src/backend/utils/mmgr/mcxt.c:346`).
- `pfree` and `repalloc` are chunk-owner based, not current-context based (`src/backend/utils/mmgr/README:99`). This matters for reusable buffers such as `StringInfo` whose buffer may be grown while some other context is current.
- `pg_malloc()` is not the backend replacement for `malloc`. It is declared in `src/include/common/fe_memutils.h:22` and defined in `src/common/fe_memutils.c:47` for frontend/common code. The backend hot path uses `palloc` and memory contexts instead.

### Context lifecycle in one sentence

Create with a parent, allocate inside it, optionally switch into it, then either:

- `MemoryContextReset()` in `src/backend/utils/mmgr/mcxt.c:383` to free its allocations and delete children but keep the context object
- `MemoryContextDelete()` in `src/backend/utils/mmgr/mcxt.c:454` to remove the whole subtree

## Architecture / data flow

### Entry points

- `MemoryContextInit()` in `src/backend/utils/mmgr/mcxt.c:339` creates `TopMemoryContext` and `ErrorContext`
- `AllocSetContextCreate(...)` eventually calls `MemoryContextCreate()` in `src/backend/utils/mmgr/mcxt.c:1100`
- `palloc()` in `src/backend/utils/mmgr/mcxt.c:1315` allocates in `CurrentMemoryContext`
- `MemoryContextReset()` in `src/backend/utils/mmgr/mcxt.c:383` and `MemoryContextDelete()` in `src/backend/utils/mmgr/mcxt.c:454` reclaim memory by lifetime group

### Control flow

1. A parent context is chosen explicitly.
2. A child context is created and linked into the parent's child list in `src/backend/utils/mmgr/mcxt.c:1121`.
3. Callers either allocate directly with `MemoryContextAlloc(context, ...)` or switch current context and use `palloc()`.
4. Cleanup is usually coarse-grained via `MemoryContextReset()` or `MemoryContextDelete()`.

### Data structures

- `MemoryContextData` is the common header referenced throughout `src/backend/utils/mmgr/mcxt.c`.
- `AllocSetContext` in `src/backend/utils/mmgr/aset.c:152` is the default backend implementation.
- `AllocBlockData` in `src/backend/utils/mmgr/aset.c:181` is the OS-allocated block.
- `MemoryChunk` headers live inside blocks and let `pfree`/`repalloc` find the owning context.

### State / lifecycle

For the remote query output path discussed with the user, the important context chain is:

- `TopMemoryContext`
- `MessageContext` for per-client-message objects (`src/backend/utils/mmgr/README:214`, reset in `src/backend/tcop/postgres.c:4750`)
- `TopPortalContext` and each `portal->portalContext` (`src/backend/utils/mmgr/portalmem.c:110`, `src/backend/utils/mmgr/portalmem.c:200`)
- executor per-query context `es_query_cxt`, created under the current context in `src/backend/executor/execUtils.c:97`
- the `printtup` per-row scratch child context, created in `src/backend/access/common/printtup.c:554`

That means the specific `printtup` path is not "PortalContext is a child of MessageContext". They are separate branches under longer-lived parents; the receiver object and the execution state intentionally live in different branches because their lifetimes differ.

## Behavior details

### What `AllocSet` is actually doing

`AllocSet` is not one fixed preallocated arena. The behavior in `src/backend/utils/mmgr/aset.c` is:

- small allocations come from blocks owned by the context and are recycled through size-segregated freelists (`src/backend/utils/mmgr/aset.c:55`)
- very large allocations use a dedicated malloc block (`src/backend/utils/mmgr/aset.c:63`, `src/backend/utils/mmgr/aset.c:696`)
- `pfree()` of small chunks usually just returns the chunk to a freelist; it does not necessarily hand memory back to the OS immediately (`src/backend/utils/mmgr/aset.c:19`)
- `AllocSetReset()` keeps the keeper block but releases extra blocks (`src/backend/utils/mmgr/aset.c:531`)

So the design does provide pooling and reuse, but the primary abstraction is still lifetime-based reclamation and failure containment.

### What reset means for reuse

Resetting a context does not mean "all future allocations are free". It means:

- the context object survives
- its keeper block survives for `AllocSet`
- any extra blocks are usually released
- subsequent `palloc`s can often reuse the keeper block without fresh `malloc`, but larger later activity may still allocate new blocks

This distinction matters for per-row contexts such as `printtup`'s scratch context: repeated rows can be cheap without implying zero allocation activity inside the allocator.

### `CurrentMemoryContext` is intentionally short-lived

The README explicitly warns that `CurrentMemoryContext` should usually point at short-lived contexts during execution (`src/backend/utils/mmgr/README:91`). That is exactly why executor expression evaluation and tuple output paths often switch into temporary contexts before invoking code that may allocate internally.

## Invariants and assumptions

- `TopMemoryContext` should not be deleted (`src/backend/utils/mmgr/mcxt.c:499`).
- `CurrentMemoryContext` must not be deleted (`src/backend/utils/mmgr/mcxt.c:501`).
- New contexts are linked into the tree when created (`src/backend/utils/mmgr/mcxt.c:1121`).
- Reset deletes child contexts by default (`src/backend/utils/mmgr/README:119`, `src/backend/utils/mmgr/mcxt.c:388`).
- `palloc` failure is normally ERROR, not NULL (`src/backend/utils/mmgr/README:59`).

## Configuration / flags

- `MCXT_ALLOC_NO_OOM`, `MCXT_ALLOC_ZERO`, and related flags flow through `MemoryContextAllocExtended()` in `src/backend/utils/mmgr/mcxt.c:1236`.
- `ErrorContext` is specially configured to retain a minimum amount of memory for error recovery in `src/backend/utils/mmgr/mcxt.c:357`.

## Interactions and cross-links

- **Overlapping KB areas**: `../serialization/printtup_row_serialization_and_transport.md` is the canonical note for how these contexts are used by tuple formatting and send.
- **Shared code / canonical docs**: `src/backend/utils/mmgr/README` is the canonical upstream design note for the memory-context subsystem.

## Future directions / design ideas

- **Status**: `implemented` for the documentation only; substantive design exploration lives elsewhere
- **Goal**: keep this doc factual and use it as grounding for serialization/copy-reduction work
- **Grounding in current code**: `printtup` and libpq output rely on the context split documented here
- **Candidate approaches**: keep proposals in the top-level future-directions tree under `../../future-directions/postgres/serialization/`
- **Risks / tradeoffs**: mixing proposals into this doc would blur facts with speculative work
- **Next experiments / implementation steps**: refer to `../../future-directions/postgres/serialization/printtup_send_path_future_directions.md`

## Gotchas

- "scope-based" is only an approximation. A memory context is an explicit object with a manually controlled lifetime.
- `slot_getallattrs()` and similar APIs do not imply detoasting or final serialization; they only prepare datum/isnull arrays. That distinction matters for understanding later allocations in `printtup`.
- Context hierarchy in the backend is not "one stack". `MessageContext`, `PortalContext`, executor contexts, and row scratch contexts can live on different branches.

## Open questions / TODO

- Add a follow-up note on `ExprContext` per-tuple contexts if later work moves from `printtup` into executor expression-eval costs.
- If future work touches `GenerationContext` or slab allocators, add a sibling doc comparing why they are or are not suitable for output-path allocations.
