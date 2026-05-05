# Replication

## Purpose

Capture the remaining Citus-side replication semantics that still matter in this tree: legacy shard-replication metadata, reference-table replication, and distributed 2PC coordination that sits above ordinary PostgreSQL physical replication.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [shard_replication_reference_tables_and_2pc.md](shard_replication_reference_tables_and_2pc.md) - Explains the deprecated/public shard-replication surface, the still-live internal `REPLICATION_MODEL_*` metadata, reference-table replication, and why distributed 2PC still matters even when worker-node HA uses PostgreSQL streaming replication.
<!-- kb-docs:end -->

## Related

- `../transactions/`: Distributed transaction timing and deadlock notes.
- `../../postgres/replication/`: Canonical PostgreSQL physical WAL replication note that Citus worker-node HA now leans on.
- `../../future-directions/citus/data-movement/command_dispatch_completion_plane.md`: Current prototype control/command plane that must preserve Citus transaction ordering semantics.
