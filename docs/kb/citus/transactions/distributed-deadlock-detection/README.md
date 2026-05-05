# Distributed Deadlock Detection

## Purpose

Document how Citus builds a cluster-wide wait graph for distributed transactions, how it maps backend waits to distributed transaction ids, and how it chooses a victim to cancel.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [distributed_deadlock_detection.md](distributed_deadlock_detection.md) - Grounded implementation notes for Citus distributed deadlock detection and its PostgreSQL lock-manager dependencies.
<!-- kb-docs:end -->

## Related

- `../timing/distributed_transaction_timing.md`: Distributed transaction lifecycle and callbacks.
- `../../intermediate-results/why_file_backed_intermediate_results.md`: Cross-backend distributed-transaction scoping that also affects intermediate-result lifetime.
- `../../../../postgres/`: PostgreSQL-side lock and backend machinery that Citus queries and annotates.
