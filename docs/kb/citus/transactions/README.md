# Transactions

## Purpose

Document distributed transaction lifecycle and how remote command execution/waiting/commit paths map to timing spots (`XACT_*`).

## Subdirectories

<!-- kb-subdirs:start -->
- `distributed-deadlock-detection/`: How Citus builds a cluster-wide wait graph and cancels a victim transaction.
- `timing/`: Where `XACT_*` timers start/end across coordinator/workers and remote command paths.
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- (none)
<!-- kb-docs:end -->

## Related

- `../intermediate-results/`: Intermediate results require distributed transactions for correct directory scoping.
- `../../future-directions/citus/`: Research notes that depend on distributed transaction lifetime rules.
- `../../instrumentation/timing-spots/timing_spots.md`: Canonical timing spot catalog.
