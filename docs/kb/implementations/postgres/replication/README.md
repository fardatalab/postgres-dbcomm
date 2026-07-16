# PostgreSQL Replication Implementations

<!-- kb-summary: Current Homer replication and basebackup implementation checkpoints and lifecycle contracts. -->

## Purpose

Track implementation-progress notes for PostgreSQL replication-facing prototype work, especially service-backed physical backup and replication transport experiments.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [CONTRACTS.md](CONTRACTS.md) - **READ BEFORE CHANGING THIS IMPLEMENTATION AREA.** Current graceful/abort close and exact-record sizing contracts for the Homer basebackup sink.
- [homer_base_backup_target_checkpoint.md](homer_base_backup_target_checkpoint.md) - Current PostgreSQL and Homer implementation, lifecycle, validation, and limitations for the TARGET homer basebackup path.
<!-- kb-docs:end -->

## Related

- `../../../postgres/replication/base_backup_archive_stream.md`: Grounded upstream `BASE_BACKUP` lifecycle and invariants.
- `../../../future-directions/postgres/replication/base_backup_service_abstraction.md`: Design note that this implementation advances.
