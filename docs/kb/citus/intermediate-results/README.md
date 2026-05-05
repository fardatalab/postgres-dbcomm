# Intermediate results

## Purpose

Document how Citus produces, stores, and transfers intermediate results across nodes (push + pull), and how those map to timing spots (`FetchIntermediate_*`, `SendViaCopy_*`, `ReceiveAndWriteCopyData_*`, `RemoteFileDestReceiver_*`).

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [push_pull_intermediate_results.md](push_pull_intermediate_results.md) - Concrete push (broadcast) and pull (fetch) workflows with timing spot mapping.
- [why_file_backed_intermediate_results.md](why_file_backed_intermediate_results.md) - Why Citus uses transaction-scoped intermediate-result files instead of keeping shuffle state only in memory.
<!-- kb-docs:end -->

## Related

- `../data-movement/`: Higher-level “why/when” for intermediate results (repartition, MERGE, INSERT..SELECT).
- `../executor/`: Coordinator-side receive path for worker results (`ReceiveResults_*`).
- `../../future-directions/citus/intermediate-results/`: Exploratory redesign notes and APIs for non-file intermediate-result storage.
- `../../instrumentation/timing-spots/timing_spots.md`: Canonical timing spot catalog.
