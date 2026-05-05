# Transaction Timing

## Purpose

Document where distributed-transaction timing spots (`XACT_*`) start/end across coordinator/workers and related remote command paths.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [distributed_transaction_timing.md](distributed_transaction_timing.md) - Where `XACT_*` timers start/end across coordinator/workers and remote command paths.
- [transaction_latency_measurement_strategy.md](transaction_latency_measurement_strategy.md) - How to measure transaction latency sources, which ordered communication stages are now covered, and what limitations still remain.
<!-- kb-docs:end -->

## Related

- `../`: Transaction lifecycle and other distributed transaction notes.
- `../distributed-deadlock-detection/`: Deadlock detection depends on the same distributed transaction machinery.
- `../../../instrumentation/timing-spots/timing_spots.md`: Canonical timing spot catalog.
