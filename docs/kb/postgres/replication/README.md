# Replication

## Purpose

Capture PostgreSQL physical WAL replication behavior relevant to communication-stack work: WAL construction, local durability, sender/receiver message flow, synchronous commit semantics, and the boundary between transport and redo/apply.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [base_backup_archive_stream.md](base_backup_archive_stream.md) - Grounded path for PostgreSQL `BASE_BACKUP`: filesystem reads, tar/archive serialization, single-COPY base-backup message framing, `pg_basebackup` receive processing, and invariants for service offload.
- [physical_wal_streaming_and_synchronous_commit.md](physical_wal_streaming_and_synchronous_commit.md) - Grounded path from backend WAL construction to shared buffers, local flush, walsender transport, walreceiver write/flush, standby replay/apply, and the current verify-copy-verify rule that makes shared-WAL extraction safe without long-lived page pinning.
<!-- kb-docs:end -->

## Related

- `../serialization/`: Row/result serialization notes; useful contrast because physical replication does not serialize tuples into a higher-level object.
- `../../future-directions/postgres/replication/`: Forward-looking notes about offloading replication transport into the external communication service.
