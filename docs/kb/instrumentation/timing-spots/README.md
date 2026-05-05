# Timing Spots

## Purpose

Document and maintain the canonical meaning of every timing spot in `src/include/timing_spots.h`, including how spots relate (coverage/nesting) and where they are instrumented in the codebase.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [communication_bucket_sets.md](communication_bucket_sets.md) - Recommended additive bucket sets for each communication workflow, including separate result-materialization and file-I/O buckets.
- [spot_hierarchy.md](spot_hierarchy.md) - Indented include/nesting relationships between timing spots (coverage trees).
- [timing_spots.md](timing_spots.md) - Full catalog of timing spots with purpose + primary instrumentation call sites.
<!-- kb-docs:end -->

## Related

- `../../postgres/libpq-io/`: FE/BE socket timing details.
- `../../postgres/copy/`: COPY instrumentation details.
- `../../postgres/query-lifecycle/`: Query loop timing details (e.g., `ExecSimpleQuery`).
