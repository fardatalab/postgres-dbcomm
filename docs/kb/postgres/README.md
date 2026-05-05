# PostgreSQL

## Purpose

Collect PostgreSQL-side workflow docs that are relevant to timing instrumentation and performance/debugging.

## Subdirectories

<!-- kb-subdirs:start -->
- `copy/`: COPY TO / COPY FROM execution flow and timing spot mapping.
- `libpq-io/`: Backend/frontend socket IO + wait paths, and how the timing spots map to them.
- `memory-contexts/`: Backend `palloc`/memory-context lifecycle, allocator behavior, and the execution-lifetime contexts used by output paths.
- `query-lifecycle/`: Backend main loop and message handling (e.g., `ExecSimpleQuery`) with timing spot mapping.
- `replication/`: Physical WAL replication, sender/receiver protocol, local durability ordering, sync-commit wait semantics, and shared-WAL buffer lifetime rules relevant to transport offload.
- `serialization/`: `printtup` row assembly, buffer lifetimes, and copy-reduction design notes for the send path.
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- (none)
<!-- kb-docs:end -->

## Related

- `../instrumentation/timing-spots/`: Spot catalog (cross-links back here for detailed flows).
