# Memory Contexts

## Purpose

Capture how PostgreSQL backend allocation works with memory contexts, with emphasis on lifetimes, context hierarchy, allocator behavior, and the parts that matter to tuple serialization and networking.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [memory_contexts_and_allocation.md](memory_contexts_and_allocation.md) - Backend `palloc`/`MemoryContext` model, `AllocSet` block reuse, and the execution-time contexts relevant to `printtup`.
<!-- kb-docs:end -->

## Related

- `../serialization/`: `printtup` serialization path and buffer lifetimes built on top of these contexts.
- `../query-lifecycle/`: Backend outer-loop and message/portal/query lifetime context around these allocations.
