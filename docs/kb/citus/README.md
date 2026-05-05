# Citus

## Purpose

Maintain Citus-specific architectural/workflow notes and how they map to the Citus-oriented timing spots in `src/include/timing_spots.h`.

This KB lives in the Postgres tree (`/data/dbcomm/postgres-citus`), but the Citus source code is located at:

- `/data/dbcomm/citus-dbcomm`

All Citus code pointers in this KB use absolute paths into `/data/dbcomm/citus-dbcomm/...:line`.

## Subdirectories

<!-- kb-subdirs:start -->
- `connection-management/`: Connection cache, transport lifecycle, remote-command IO, and higher-layer routing state above raw connections.
- `data-movement/`: Off-path (bulk COPY-based) vs in-query tuple movement (push/pull) concepts and intended timing spot mapping.
- `executor/`: Adaptive executor receive loop, intermediate result execution, and result materialization on coordinator.
- `intermediate-results/`: Intermediate results storage and transfer (push via DestReceiver + pull via fetch_intermediate_results).
- `overview/`: High-level overview of Citus architecture (coordinator/workers, recursive planning, etc.).
- `replication/`: Legacy shard-replication metadata, reference-table replication, and distributed 2PC distinctions.
- `transactions/`: Distributed transaction lifecycle and remote command timing (`XACT_*` spots).
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- (none)
<!-- kb-docs:end -->

## Related

- `../instrumentation/timing-spots/timing_spots.md`: Citus-oriented timing spots are defined here but not wired in this repo snapshot.
- `../implementations/citus/`: Current implementation-progress notes for Citus prototypes.
