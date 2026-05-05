# Executor

## Purpose

Document Citus execution paths that are directly tied to timing spots (especially `ReceiveResults_*` and intermediate-result execution).

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [receive_results_streaming.md](receive_results_streaming.md) - How coordinator receives streaming results from workers in adaptive executor (`ReceiveResults_*`).
<!-- kb-docs:end -->

## Related

- `../intermediate-results/`: Intermediate result broadcast/fetch flows.
- `../../instrumentation/timing-spots/timing_spots.md`: Canonical timing spot catalog.

