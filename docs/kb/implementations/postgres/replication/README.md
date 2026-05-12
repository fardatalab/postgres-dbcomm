# PostgreSQL Replication Implementations

## Purpose

Track implementation-progress notes for PostgreSQL replication-facing prototype work, especially service-backed physical backup and replication transport experiments.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [homer_base_backup_target_checkpoint.md](homer_base_backup_target_checkpoint.md) - Current checkpoint for the `TARGET 'homer'` base-backup prototype: server-side `bbsink`, typed basebackup objects, shared queue/RDMA service integration, local blackhole smoke mode, and remaining remote-RDMA caveats.
<!-- kb-docs:end -->

## Related

- `../../../postgres/replication/base_backup_archive_stream.md`: Grounded upstream `BASE_BACKUP` lifecycle and invariants.
- `../../../future-directions/postgres/replication/base_backup_service_abstraction.md`: Design note that this implementation advances.
